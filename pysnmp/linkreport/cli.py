#
# This file is part of pysnmp software.
#
# Copyright (c) 2005-2020, Ilya Etingof <etingof@gmail.com>
# License: https://www.pysnmp.com/pysnmp/license.html
#
"""Command line entry point: ``python -m pysnmp.linkreport``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pysnmp.linkreport.model import ReportConfig
from pysnmp.linkreport.reader import SnapshotFormatError, load_snapshots
from pysnmp.linkreport.render import render_json, render_text
from pysnmp.linkreport.report import build_report

__all__ = ["main"]


def _rate(value: str) -> int:
    """Parse a bit rate with an optional SI suffix."""
    text = value.strip().lower().replace("bit/s", "").replace("bps", "")
    multipliers = {
        "k": 1000,
        "m": 1000**2,
        "g": 1000**3,
        "t": 1000**4,
        "p": 1000**5,
    }
    if text and text[-1] in multipliers:
        try:
            return int(float(text[:-1]) * multipliers[text[-1]])
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"bad rate {value!r}") from exc
    try:
        return int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"bad rate {value!r}") from exc


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pysnmp.linkreport",
        description=(
            "Build a daily link report from SNMP counter snapshots. "
            "Counters are reconciled per device generation and per sampling "
            "window; wraps and resets are reported separately."
        ),
    )
    parser.add_argument(
        "path",
        type=Path,
        help="snapshot file (.json, .jsonl or .csv)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="write the report to this file instead of stdout",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit JSON instead of the text report",
    )
    parser.add_argument(
        "--max-rate",
        type=_rate,
        help=(
            "fallback ceiling (bit/s) for interfaces whose speed is unknown; "
            "without it such wrap counts are reported as unbounded"
        ),
    )
    parser.add_argument(
        "--rate-tolerance",
        type=float,
        default=0.10,
        help="fraction by which observed traffic may exceed the link rate "
        "(default: 0.10)",
    )
    parser.add_argument(
        "--counter64-wrap-min-days",
        type=float,
        default=365.0,
        help="shortest window (days) in which a 64-bit decrease is read as a "
        "wrap rather than a reset (default: 365)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the command line tool.  Returns a process exit status."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.rate_tolerance < 0:
        parser.error("--rate-tolerance must not be negative")
    if args.counter64_wrap_min_days < 0:
        parser.error("--counter64-wrap-min-days must not be negative")

    config = ReportConfig(
        max_rate_bps=args.max_rate,
        rate_tolerance=args.rate_tolerance,
        counter64_wrap_min_seconds=args.counter64_wrap_min_days * 86400.0,
    )

    try:
        snapshots = load_snapshots(args.path)
    except SnapshotFormatError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report = build_report(snapshots, config)
    rendered = render_json(report) if args.json else render_text(report)

    if args.output:
        try:
            args.output.write_text(rendered + "\n", encoding="utf-8")
        except OSError as exc:
            print(f"error: cannot write {args.output}: {exc}", file=sys.stderr)
            return 2
    else:
        print(rendered)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
