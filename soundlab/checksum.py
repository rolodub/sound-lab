"""Full-content fingerprint (BLAKE2b-256) of every file in the catalog, read-only.

Two uses: prove the NAS migration bit for bit (the new copy is hashed and compared), and turn the
quick-fingerprint duplicates into certain ones. Resumable: a file already hashed with the same size
and modification time is skipped. Slow by nature (the whole library is read once).
"""
import hashlib
import logging
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("soundlab.checksum")
CHUNK = 4 << 20
SCHEMA = """
CREATE TABLE IF NOT EXISTS checksums (
  root TEXT NOT NULL, relpath TEXT NOT NULL, size INTEGER, mtime REAL, blake2b TEXT, error TEXT,
  hashed_at REAL NOT NULL, PRIMARY KEY (root, relpath));
CREATE INDEX IF NOT EXISTS checksums_hash ON checksums (blake2b);
"""


def digest(path: str) -> str:
    h = hashlib.blake2b(digest_size=32)
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def run(roots: list[str], catalog: str, workers: int = 2, commit_every: int = 200) -> dict:
    db = sqlite3.connect(catalog)
    db.executescript(SCHEMA)
    mounts = {os.path.basename(os.path.abspath(r).rstrip("/")): os.path.abspath(r) for r in roots}
    done = {(r, p): (s, m) for r, p, s, m in db.execute(
        "SELECT root, relpath, size, mtime FROM checksums WHERE error IS NULL")}
    todo = [(r, p, s, m) for r, p, s, m in db.execute(
                "SELECT root, relpath, size, mtime FROM files WHERE root IN (%s) ORDER BY root, relpath"
                % ",".join("?" * len(mounts)), list(mounts))
            if done.get((r, p)) != (s, m)]
    total = sum(t[2] or 0 for t in todo)
    log.info("%d files to hash, %.2f TB", len(todo), total / 1e12)

    def work(t):
        r, p, s, m = t
        try:
            return t, digest(os.path.join(mounts[r], p)), None
        except OSError as e:
            return t, None, f"{e.strerror or e}"[:300]

    hashed = errors = 0
    nbytes, t0 = 0, time.time()
    with ThreadPoolExecutor(workers) as pool:
        for i, ((r, p, s, m), h, err) in enumerate(pool.map(work, todo), 1):
            db.execute("INSERT OR REPLACE INTO checksums VALUES (?, ?, ?, ?, ?, ?, ?)", (r, p, s, m, h, err, time.time()))
            hashed += h is not None
            errors += err is not None
            nbytes += s or 0
            if i % commit_every == 0:
                db.commit()
                rate = nbytes / max(1, time.time() - t0)
                log.info("%d / %d files, %.0f / %.0f GB, %.0f MB/s, %.1f h left (%d errors)", i, len(todo),
                         nbytes / 1e9, total / 1e9, rate / 1e6, (total - nbytes) / max(rate, 1) / 3600, errors)
    db.commit()
    db.close()
    return {"hashed": hashed, "errors": errors}
