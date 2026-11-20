"""Phase 0 report (Markdown) from the catalog: what the library holds, to size the new NAS and
plan the labelling. Counts only; file names appear only in the 'largest folders' tables."""
import sqlite3

TB, GB = 1e12, 1e9
RUSH_SECONDS = 120          # long recordings: candidates for derushing


def _rows(db, sql, params=()):
    return db.execute(sql, params).fetchall()


def _table(head: list[str], rows: list) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join("" if v is None else str(v) for v in r) + " |" for r in rows]
    return out + [""]


def _h(seconds) -> str:
    return f"{(seconds or 0) / 3600:,.1f} h"


def build(catalog: str, top: int = 25) -> str:
    db = sqlite3.connect(catalog)
    md = ["# Sound Lab — inventory report", ""]
    total = _rows(db, "SELECT count(*), sum(size), sum(CASE WHEN kind='audio' THEN duration_s END), count(DISTINCT root) FROM files")[0]
    md += [f"- **{total[0]:,} files**, **{(total[1] or 0) / TB:.2f} TB**, **{_h(total[2])} of audio**, {total[3]} share(s).", ""]

    md += ["## By share", ""]
    md += _table(["Share", "Files", "TB", "Audio files", "Audio hours", "Video files", "Sessions / exchange", "Unreadable"],
                 [(r[0], f"{r[1]:,}", f"{(r[2] or 0) / TB:.2f}", f"{r[3]:,}", _h(r[4]), f"{r[5]:,}", f"{r[6]:,}", f"{r[7]:,}")
                  for r in _rows(db, """SELECT root, count(*), sum(size), sum(kind='audio'),
                                               sum(CASE WHEN kind='audio' THEN duration_s END), sum(kind='video'),
                                               sum(kind='session'), sum(error IS NOT NULL)
                                          FROM files GROUP BY root ORDER BY sum(size) DESC""")])

    md += ["## Audio formats", ""]
    md += _table(["Container", "Codec", "Sample rate", "Bits", "Files", "Hours", "TB"],
                 [(r[0], r[1], r[2], r[3], f"{r[4]:,}", _h(r[5]), f"{(r[6] or 0) / TB:.2f}")
                  for r in _rows(db, """SELECT container, codec, sample_rate, bits, count(*), sum(duration_s), sum(size)
                                          FROM files WHERE kind='audio' GROUP BY 1, 2, 3, 4 ORDER BY 5 DESC LIMIT 25""")])
    md += ["## Channel layouts", ""]
    md += _table(["Channels", "Files", "Hours"],
                 [(r[0], f"{r[1]:,}", _h(r[2])) for r in _rows(db, """SELECT channels, count(*), sum(duration_s) FROM files
                                                                      WHERE kind='audio' GROUP BY 1 ORDER BY 2 DESC""")])

    md += ["## Durations (audio)", ""]
    md += _table(["Length", "Files", "Hours"],
                 [(r[0], f"{r[1]:,}", _h(r[2])) for r in _rows(db, f"""
                    SELECT CASE WHEN duration_s IS NULL THEN '? (unread)' WHEN duration_s < 2 THEN '< 2 s'
                                WHEN duration_s < 10 THEN '2-10 s' WHEN duration_s < 60 THEN '10-60 s'
                                WHEN duration_s < {RUSH_SECONDS} THEN '1-2 min'
                                WHEN duration_s < 600 THEN '2-10 min (rush candidates)' ELSE '> 10 min (rush candidates)' END,
                           count(*), sum(duration_s)
                      FROM files WHERE kind='audio' GROUP BY 1 ORDER BY min(coalesce(duration_s, -1))""")])

    meta = _rows(db, """SELECT sum(bext_description IS NOT NULL AND bext_description <> ''), sum(has_ixml),
                               sum(ixml_scene IS NOT NULL), sum(markers > 0), count(*)
                          FROM files WHERE kind='audio'""")[0]
    md += ["## Embedded metadata (audio)", "",
           f"- With a description (BWF / AIFF annotation / tags): **{meta[0] or 0:,}** of {meta[4]:,}",
           f"- With iXML: **{meta[1] or 0:,}** (with a scene: {meta[2] or 0:,} — production sound recorders)",
           f"- With markers / cues: **{meta[3] or 0:,}**", ""]

    legacy = _rows(db, """SELECT sum(sniffed = 1), sum(container = 'sd2'), sum(channel_suffix IS NOT NULL),
                                 count(DISTINCT CASE WHEN channel_suffix IS NOT NULL THEN root || '/' || folder || '/' ||
                                       substr(relpath, 1, length(relpath) - length(channel_suffix) - 1) END)
                            FROM files WHERE kind='audio'""")[0]
    md += ["## Classic Mac files", "",
           f"- Recognised from their content (no usable extension): **{legacy[0] or 0:,}**",
           f"- Sound Designer II (format read from the resource fork): **{legacy[1] or 0:,}**",
           f"- Split-mono channel files (.L, .R, .C…): **{legacy[2] or 0:,}**, in **{legacy[3] or 0:,}** multichannel sounds", ""]
    other = _rows(db, """SELECT CASE WHEN ext = '' THEN '(none)' ELSE ext END, count(*), sum(size)
                           FROM files WHERE kind='other' GROUP BY 1 ORDER BY 3 DESC LIMIT 12""")
    if other:
        md += ["## Other files (not audio, video or session; by size)", ""]
        md += _table(["Extension", "Files", "GB"], [(r[0], f"{r[1]:,}", f"{(r[2] or 0) / GB:,.1f}") for r in other])

    md += ["## Sessions and exchange files (examples for spotting, phase 4)", ""]
    md += _table(["Extension", "Files", "Shares"],
                 [(r[0], f"{r[1]:,}", r[2]) for r in _rows(db, """SELECT ext, count(*), group_concat(DISTINCT root) FROM files
                                                                  WHERE kind='session' GROUP BY 1 ORDER BY 2 DESC""")])

    dup = _rows(db, """SELECT count(*), sum(n - 1), sum((n - 1) * size) FROM (
                         SELECT quick_id, count(*) AS n, max(size) AS size FROM files
                          WHERE quick_id IS NOT NULL GROUP BY quick_id HAVING count(*) > 1)""")[0]
    md += ["## Duplicates (quick fingerprint; to confirm with a full checksum)", "",
           f"- {dup[0] or 0:,} groups, {dup[1] or 0:,} extra copies, **{(dup[2] or 0) / GB:,.1f} GB** reclaimable.", ""]

    if db.execute("SELECT name FROM sqlite_master WHERE name = 'checksums'").fetchone():
        ck = _rows(db, """SELECT count(*), sum(error IS NOT NULL), (SELECT count(*) FROM files) FROM checksums""")[0]
        full = _rows(db, """SELECT count(*), sum(n - 1), sum((n - 1) * size) FROM (
                              SELECT blake2b, count(*) AS n, max(size) AS size FROM checksums
                               WHERE blake2b IS NOT NULL GROUP BY blake2b HAVING count(*) > 1)""")[0]
        md += ["## Duplicates (full checksum, certain)", "",
               f"- Hashed: {ck[0]:,} of {ck[2]:,} files ({ck[1] or 0:,} unreadable).",
               f"- {full[0] or 0:,} groups, {full[1] or 0:,} extra copies, **{(full[2] or 0) / GB:,.1f} GB** reclaimable.", ""]

    md += [f"## Largest folders (audio hours, top {top}, two levels deep)", ""]
    md += _table(["Share", "Folder", "Audio files", "Hours", "GB"],
                 [(r[0], r[1] or "(root)", f"{r[2]:,}", _h(r[3]), f"{(r[4] or 0) / GB:,.1f}")
                  for r in _rows(db, """SELECT root,
                                               CASE WHEN instr(folder, '/') = 0 THEN folder
                                                    ELSE substr(folder, 1, instr(folder, '/') - 1) || '/' ||
                                                         substr(substr(folder, instr(folder, '/') + 1), 1,
                                                                CASE WHEN instr(substr(folder, instr(folder, '/') + 1), '/') = 0
                                                                     THEN 999 ELSE instr(substr(folder, instr(folder, '/') + 1), '/') - 1 END) END AS f,
                                               count(*), sum(duration_s), sum(size)
                                          FROM files WHERE kind='audio' GROUP BY 1, 2 ORDER BY 4 DESC LIMIT ?""", (top,))])

    walk_err = _rows(db, "SELECT root, path, error FROM walk_errors ORDER BY root, path LIMIT 50")
    if walk_err:
        md += ["## Folders and files that could not be reached (fix on the NAS, e.g. rename)", ""]
        md += _table(["Share", "Path", "Reason"], walk_err)
    err = _rows(db, "SELECT substr(error, 1, 60), count(*) FROM files WHERE error IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 10")
    if err:
        md += ["## Unreadable files (by reason)", ""] + _table(["Reason", "Files"], [(r[0], f"{r[1]:,}") for r in err])
    db.close()
    return "\n".join(md)
