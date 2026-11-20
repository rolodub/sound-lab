"""Phase 0: inventory of a sound library on (slow, read-only) network shares.

Walks the roots, classifies files, reads header metadata (soundlab.headers) and a quick
fingerprint (size + first and last 64 KiB) into a SQLite catalog. Resumable: a file already in
the catalog with the same size and modification time is not read again. Nothing is written
outside the catalog.
"""
import hashlib
import logging
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from struct import error as struct_error
from dataclasses import dataclass

from .headers import CHANNEL_SUFFIX, HeaderError, read_headers, read_unknown

log = logging.getLogger("soundlab.inventory")

AUDIO = {".wav", ".bwf", ".rf64", ".w64", ".aif", ".aiff", ".aifc", ".sd2", ".mp3", ".flac", ".ogg", ".oga", ".opus",
         ".m4a", ".aac", ".wma", ".caf", ".ac3", ".dts", ".mka", ".ape", ".wv", ".au", ".snd"}
VIDEO = {".mov", ".mp4", ".m4v", ".mxf", ".avi", ".mkv", ".mpg", ".mpeg", ".mts", ".m2ts", ".wmv", ".dv", ".webm"}
# Editing sessions and exchange files: the potential examples of spotting (phase 4).
SESSION = {".ptx": "protools", ".ptf": "protools", ".pts": "protools", ".pt5": "protools", ".rpp": "reaper",
           ".sesx": "audition", ".logicx": "logic", ".cpr": "cubase", ".npr": "nuendo", ".aaf": "aaf", ".omf": "omf",
           ".omfi": "omf", ".edl": "edl", ".fcpxml": "fcpxml", ".otio": "otio", ".als": "ableton"}
# System folders of QNAP, macOS and Windows: never walked.
SKIP_DIRS = {"@recycle", "@recently-snapshot", ".@__thumb", ".appledouble", ".spotlight-v100", ".trashes", ".fseventsd",
             "$recycle.bin", "system volume information", ".temporaryitems", "network trash folder", "@eadir"}
SKIP_FILES = {".ds_store", "thumbs.db", "desktop.ini", ".localized"}
# Extensions that are surely not audio: not sniffed. Anything else without a known extension
# (classic Mac files have none, or a channel suffix like ".L") is sniffed from its first bytes.
NOT_AUDIO = {".txt", ".pdf", ".doc", ".docx", ".rtf", ".xls", ".xlsx", ".csv", ".jpg", ".jpeg", ".png", ".gif",
             ".tif", ".tiff", ".bmp", ".psd", ".zip", ".rar", ".7z", ".sit", ".sitx", ".hqx", ".dmg", ".iso", ".exe",
             ".app", ".html", ".htm", ".xml", ".ini", ".log", ".db", ".plist", ".url", ".webloc", ".pages", ".key",
             ".numbers", ".ttf", ".otf", ".dll", ".pkg", ".nfo", ".md", ".json", ".srt", ".pfl", ".wfm", ".pk", ".peak",
             ".reapeaks", ".asd", ".pkf", ".ovw", ".sfk", ".mrk", ".dat"}
QUICK = 65536
# Names written by an AFP server (netatalk) encode some characters as ':' + hex: ':2e' is a
# leading '.', so ':2eDS_Store' is a hidden '.DS_Store'.
AFP_DOT = ":2e"


def _hidden(name: str) -> bool:
    return name.startswith(".") or name.lower().startswith(AFP_DOT)

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  root TEXT NOT NULL, relpath TEXT NOT NULL, folder TEXT NOT NULL, ext TEXT, kind TEXT NOT NULL,
  size INTEGER, mtime REAL, quick_id TEXT,
  container TEXT, codec TEXT, sample_rate INTEGER, bits INTEGER, channels INTEGER, frames INTEGER,
  duration_s REAL, audio_streams INTEGER, video_codec TEXT, width INTEGER, height INTEGER, frame_rate TEXT,
  bext_description TEXT, bext_originator TEXT, bext_date TEXT, time_reference INTEGER, markers INTEGER,
  has_ixml INTEGER, ixml_project TEXT, ixml_scene TEXT, ixml_take TEXT, ixml_tape TEXT, ixml_note TEXT,
  ixml_circled TEXT, ixml_file_uid TEXT, channel_suffix TEXT, sniffed INTEGER,
  error TEXT, scanned_at REAL NOT NULL,
  PRIMARY KEY (root, relpath));
CREATE INDEX IF NOT EXISTS files_quick ON files (quick_id);
CREATE INDEX IF NOT EXISTS files_folder ON files (root, folder);
CREATE TABLE IF NOT EXISTS walk_errors (root TEXT NOT NULL, path TEXT NOT NULL, error TEXT, at REAL);
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, root TEXT, started_at REAL, finished_at REAL,
  seen INTEGER, read INTEGER, errors INTEGER);
"""
COLUMNS = ("container", "codec", "sample_rate", "bits", "channels", "frames", "duration_s", "audio_streams",
           "video_codec", "width", "height", "frame_rate", "bext_description", "bext_originator", "bext_date",
           "time_reference", "markers", "has_ixml", "ixml_project", "ixml_scene", "ixml_take", "ixml_tape",
           "ixml_note", "ixml_circled", "ixml_file_uid", "channel_suffix", "sniffed")


INT64 = 2 ** 63 - 1


def _fit(v):
    """A corrupt header can hold a number SQLite cannot store: kept empty instead of failing the run."""
    return None if isinstance(v, int) and not -INT64 - 1 <= v <= INT64 else v


def kind_of(ext: str) -> str:
    if ext in AUDIO:
        return "audio"
    if ext in VIDEO:
        return "video"
    if ext in SESSION:
        return "session"
    return "other"


def quick_id(path: str, size: int) -> str:
    """Size + first and last 64 KiB: equal files share it; a full checksum confirms later."""
    h = hashlib.blake2b(str(size).encode(), digest_size=16)
    with open(path, "rb") as f:
        h.update(f.read(QUICK))
        if size > 2 * QUICK:
            f.seek(size - QUICK)
            h.update(f.read(QUICK))
    return h.hexdigest()


def walk(root: str, errors: list | None = None):
    """(relative path, size, mtime) of every file, system folders and AppleDouble files skipped.
    Folders or files that cannot be read are appended to `errors` (path, reason)."""
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError as e:
            log.warning("cannot list %s: %s", d, e)
            if errors is not None:
                errors.append((os.path.relpath(d, root), f"list: {e.strerror or e}"))
            continue
        for e in entries:
            name = e.name
            try:
                if e.is_dir(follow_symlinks=False):
                    if name.lower() not in SKIP_DIRS and not _hidden(name):
                        stack.append(e.path)
                elif e.is_file(follow_symlinks=False):
                    low = name.lower()
                    if low in SKIP_FILES or name.startswith("._") or (low.startswith(AFP_DOT) and
                                                                     "." + low[3:] in SKIP_FILES):
                        continue
                    st = e.stat(follow_symlinks=False)
                    yield os.path.relpath(e.path, root), st.st_size, st.st_mtime
            except OSError as err:
                log.warning("cannot stat %s: %s", e.path, err)
                if errors is not None:
                    errors.append((os.path.relpath(e.path, root), f"stat: {err.strerror or err}"))


@dataclass
class Result:
    relpath: str
    size: int
    mtime: float
    kind: str
    ext: str
    info: dict
    qid: str | None
    error: str | None


def examine(root: str, relpath: str, size: int, mtime: float) -> Result:
    ext = os.path.splitext(relpath)[1].lower()
    kind = kind_of(ext)
    info, qid, error = {}, None, None
    path = os.path.join(root, relpath)
    if kind in ("audio", "video", "session"):
        try:
            qid = quick_id(path, size)
        except OSError as e:
            error = f"read: {e}"[:300]
    if kind in ("audio", "video") and not error:
        try:
            info = read_headers(path)
        except (HeaderError, OSError, ValueError) as e:
            error = f"headers: {e}"[:300]
        except Exception as e:                       # e.g. ffprobe timeout: recorded, the run goes on
            error = f"headers: {type(e).__name__}: {e}"[:300]
    elif kind == "other" and ext not in NOT_AUDIO and size >= 64:
        # Classic Mac files: no extension, or a channel suffix (".L", ".R"): sniff the content,
        # or read a Sound Designer II resource fork kept by the file server.
        try:
            found = read_unknown(path, size)
        except (HeaderError, OSError, ValueError, IndexError, struct_error) as e:
            found, error = None, f"sniff: {e}"[:300]
        except Exception as e:
            found, error = None, f"sniff: {type(e).__name__}: {e}"[:300]
        if found:
            kind, info, error = "audio", {**found, "sniffed": 1}, None
            try:
                qid = quick_id(path, size)
            except OSError as e:
                error = f"read: {e}"[:300]
    if kind == "audio" and ext in CHANNEL_SUFFIX:
        info["channel_suffix"] = ext[1:].upper()
    return Result(relpath, size, mtime, kind, ext, info, qid, error)


def connect(catalog: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(catalog)), exist_ok=True)
    db = sqlite3.connect(catalog)
    db.executescript(SCHEMA)
    return db


def run(roots: list[str], catalog: str, workers: int = 8, progress_every: int = 2000) -> dict:
    """Inventory every root into the catalog. Returns counts."""
    db = connect(catalog)
    totals = {"seen": 0, "read": 0, "errors": 0}
    for root in roots:
        root = os.path.abspath(root)
        name = os.path.basename(root.rstrip("/"))
        run_id = db.execute("INSERT INTO runs (root, started_at) VALUES (?, ?)", (name, time.time())).lastrowid
        # Files that failed last time (e.g. the share dropped for a moment) are read again.
        known = {r: (s, m, e) for r, s, m, e in db.execute("SELECT relpath, size, mtime, error FROM files WHERE root = ?",
                                                             (name,))}
        todo, seen = [], 0
        present = set()
        walk_errors: list = []
        for relpath, size, mtime in walk(root, walk_errors):
            seen += 1
            present.add(relpath)
            k = known.get(relpath)
            if k and k[:2] == (size, mtime) and k[2] is None:
                continue
            todo.append((relpath, size, mtime))
        log.info("%s: %d files, %d to read", name, seen, len(todo))
        read = errors = 0
        with ThreadPoolExecutor(workers) as pool:
            sql = (f"INSERT OR REPLACE INTO files (root, relpath, folder, ext, kind, size, mtime, quick_id, "
                   f"{', '.join(COLUMNS)}, error, scanned_at) VALUES ({', '.join('?' * (len(COLUMNS) + 10))})")
            for i, res in enumerate(pool.map(lambda t: examine(root, *t), todo), 1):
                values = [_fit(res.info.get(c)) for c in COLUMNS]
                values[COLUMNS.index("has_ixml")] = 1 if res.info.get("has_ixml") else 0
                head = [name, res.relpath, os.path.dirname(res.relpath), res.ext, res.kind, res.size, res.mtime, res.qid]
                try:
                    db.execute(sql, [*head, *values, res.error, time.time()])
                except (sqlite3.Error, OverflowError, ValueError) as e:     # one bad row never stops the run
                    log.warning("cannot record %s: %s", res.relpath, e)
                    res.error = f"record: {e}"[:300]
                    db.execute(sql, [*head, *[None] * len(COLUMNS), res.error, time.time()])
                read += 1
                errors += bool(res.error)
                if i % progress_every == 0:
                    db.commit()
                    log.info("%s: %d / %d read (%d errors)", name, i, len(todo), errors)
        db.execute("DELETE FROM walk_errors WHERE root = ?", (name,))
        db.executemany("INSERT INTO walk_errors (root, path, error, at) VALUES (?, ?, ?, ?)",
                       [(name, p, e, time.time()) for p, e in walk_errors])
        # A folder that could not be listed keeps its files in the catalog (not "gone").
        unreadable = tuple(p + "/" for p, e in walk_errors if e.startswith("list:"))
        gone = [r for r in known if r not in present and not r.startswith(unreadable)]
        if gone:
            db.executemany("DELETE FROM files WHERE root = ? AND relpath = ?", [(name, r) for r in gone])
        db.execute("UPDATE runs SET finished_at = ?, seen = ?, read = ?, errors = ? WHERE id = ?",
                   (time.time(), seen, read, errors, run_id))
        db.commit()
        totals["seen"] += seen
        totals["read"] += read
        totals["errors"] += errors
    db.close()
    return totals
