"""Command line entry point: ``python -m ftp_capture``."""

import argparse
import json
import sys

from .correlate import analyze_file
from .pcap import CaptureError
from .report import summarize_text
from .report import to_json
from .report import to_text


def build_parser():
    parser = argparse.ArgumentParser(
        prog="ftp_capture",
        description=(
            "Replay a multi-session FTP capture and attribute every data "
            "connection to the control session and transfer that "
            "negotiated it. Ambiguous links stay candidates."
        ),
    )
    parser.add_argument("capture", help="pcap or pcapng file to analyze")
    parser.add_argument(
        "-f",
        "--format",
        choices=("text", "json"),
        default="text",
        help="report format (default: text)",
    )
    parser.add_argument(
        "-o",
        "--output",
        help="write the report to this file instead of stdout",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="suppress the one-line summary on stderr",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=(
            "exit 2 when any data connection stays candidate or "
            "unattributed, or when a negotiation failed"
        ),
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        report = analyze_file(args.capture)
    except (OSError, CaptureError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    payload = to_json(report) if args.format == "json" else to_text(report)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as stream:
            stream.write(payload)
    else:
        sys.stdout.write(payload)

    if not args.quiet:
        print(summarize_text(report), file=sys.stderr)

    if args.strict:
        totals = report["totals"]
        if (
            totals["candidate_data_connections"]
            or totals["unattributed_data_connections"]
            or totals["negotiation_failures"]
        ):
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
