"""CLI: build a metering report, or diff two reports across a layer switch.

Usage:
    python -m ifstack_metering plan input.json
    python -m ifstack_metering diff old.json new.json
"""

from __future__ import annotations

import argparse
import json
import sys

from .analysis import build_report, diff_reports
from .model import load_plan


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ifstack_metering")
    sub = parser.add_subparsers(dest="command", required=True)

    plan_parser = sub.add_parser("plan", help="build a metering report for one plan")
    plan_parser.add_argument("input", help="plan JSON file")

    diff_parser = sub.add_parser(
        "diff", help="audit re-attribution after switching the metering layer"
    )
    diff_parser.add_argument("old", help="plan JSON with the old metering layer")
    diff_parser.add_argument("new", help="plan JSON with the new metering layer")

    args = parser.parse_args(argv)

    if args.command == "plan":
        report = build_report(load_plan(args.input))
    else:
        report = diff_reports(
            build_report(load_plan(args.old)),
            build_report(load_plan(args.new)),
        )
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
