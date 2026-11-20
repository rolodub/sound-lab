"""python -m soundlab inventory --root DIR [--root DIR ...] --catalog FILE
python -m soundlab report --catalog FILE"""
import argparse
import logging
import sys

from . import inventory, report


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
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    if a.cmd == "inventory":
        totals = inventory.run(a.root, a.catalog, workers=a.workers)
        logging.info("done: %s", totals)
    else:
        print(report.build(a.catalog, a.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
