"""python -m soundlab inventory --root DIR [--root DIR ...] --catalog FILE
python -m soundlab report --catalog FILE
python -m soundlab checksum --root DIR [--root DIR ...] --catalog FILE   (full BLAKE2b)
python -m soundlab pmf2wav IN.pmf OUT.wav   (Pyramix media to WAV, same rate, bits and tracks)"""
import argparse
import logging
import sys

from . import checksum, inventory, report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m soundlab")
    sub = ap.add_subparsers(dest="cmd", required=True)
    inv = sub.add_parser("inventory", help="read-only inventory of shares into the catalog")
    inv.add_argument("--root", action="append", required=True, help="a mounted share (repeatable)")
    inv.add_argument("--catalog", required=True)
    inv.add_argument("--workers", type=int, default=8)
    rep = sub.add_parser("report", help="Markdown report from the catalog")
    rep.add_argument("--catalog", required=True)
    rep.add_argument("--top", type=int, default=25)
    ck = sub.add_parser("checksum", help="full-content fingerprint of every catalogued file (slow, resumable)")
    ck.add_argument("--root", action="append", required=True)
    ck.add_argument("--catalog", required=True)
    ck.add_argument("--workers", type=int, default=2)
    pw = sub.add_parser("pmf2wav", help="convert a Pyramix .pmf to WAV (the original is only read)")
    pw.add_argument("src")
    pw.add_argument("dst")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    if a.cmd == "inventory":
        totals = inventory.run(a.root, a.catalog, workers=a.workers)
        logging.info("done: %s", totals)
    elif a.cmd == "checksum":
        logging.info("done: %s", checksum.run(a.root, a.catalog, workers=a.workers))
    elif a.cmd == "pmf2wav":
        from .pyramix import to_wav
        info = to_wav(a.src, a.dst)
        logging.info("written %s: %s", a.dst, info)
    else:
        print(report.build(a.catalog, a.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
