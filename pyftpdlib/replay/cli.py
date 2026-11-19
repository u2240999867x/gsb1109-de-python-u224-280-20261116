# Copyright (C) 2007 Giampaolo Rodola' <g.rodola@gmail.com>.
# Use of this source code is governed by MIT license that can be
# found in the LICENSE file.

"""Command line entry point for offline FTP download verification."""

import argparse
import json
import sys

from .analyzer import analyze_events
from .capture import replay_capture
from .events import load_events

__all__ = ["main", "render_text"]

_EXIT_OK = 0
_EXIT_INCOMPLETE = 1
_EXIT_UNKNOWN = 2
_EXIT_ERROR = 3

_STATE_EXIT = {
    "complete": _EXIT_OK,
    "incomplete": _EXIT_INCOMPLETE,
    "unknown": _EXIT_UNKNOWN,
}


def parse_args(args=None):
    parser = argparse.ArgumentParser(
        prog="python -m pyftpdlib.replay",
        description=(
            "Replay an offline FTP capture and report the observed byte "
            "ranges, data-channel close and final reply for one download."
        ),
    )
    parser.add_argument(
        "capture",
        help="pcap/pcapng capture, or a JSON/JSON Lines event file",
    )
    parser.add_argument(
        "--format",
        choices=("auto", "pcap", "events"),
        default="auto",
        help="input format (default: auto-detect)",
    )
    parser.add_argument(
        "--size",
        type=int,
        default=None,
        help="expected file size when the capture has no SIZE reply",
    )
    parser.add_argument(
        "--restart",
        type=int,
        default=None,
        help="expected REST offset when the capture has no REST command",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the full analysis as JSON",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat warnings as failures",
    )
    return parser.parse_args(args)


def _detect_format(path, requested):
    if requested != "auto":
        return requested
    with open(path, "rb") as file:
        magic = file.read(4)
    if magic in (
        b"\xd4\xc3\xb2\xa1",
        b"\xa1\xb2\xc3\xd4",
        b"\x4d\x3c\xb2\xa1",
        b"\xa1\xb2\x3c\x4d",
        b"\x0a\x0d\x0d\x0a",
    ):
        return "pcap"
    return "events"


def _load(path, fmt):
    if fmt == "pcap":
        replay, segments = replay_capture(path)
        return replay.events, replay.warnings, len(segments)
    events = load_events(path)
    return events, [], 0


def _fmt_range(pair):
    return f"{pair[0]}-{pair[1]}"


def render_text(result):
    lines = []
    primary = result.primary
    summary = primary.as_dict() if primary is not None else None
    if summary is None:
        lines.append("no RETR download found")
        if result.warnings:
            lines.append("warnings:")
            lines.extend(f"  - {warning}" for warning in result.warnings)
        return "\n".join(lines)

    lines.append(f"transfer: {summary['id']} ({summary['file']})")
    lines.append(f"state: {summary['state']} ({summary['outcome']})")
    expected = summary["expected_range"]
    if expected:
        lines.append(
            "expected: "
            f"{expected[0]}-{expected[1]} ({summary['expected_size']} bytes, "
            f"from {summary['expected_size_source']})"
        )
    else:
        lines.append("expected: unknown (no SIZE reply or --size)")
    if summary["received_ranges"]:
        ranges = ", ".join(
            _fmt_range(pair) for pair in summary["received_ranges"]
        )
    else:
        ranges = "(none)"
    lines.append(
        f"received: {ranges} ({summary['received_bytes']} bytes, "
        f"offset basis={summary['offset_basis']})"
    )
    if summary["gaps"] is not None:
        gaps = ", ".join(_fmt_range(pair) for pair in summary["gaps"])
        lines.append(
            f"gaps: {gaps or '(none)'} ({summary['missing_bytes']} missing)"
        )
    if summary["duplicate_bytes"]:
        lines.append(f"duplicates: {summary['duplicate_bytes']} bytes")
    close = summary["data_close"]
    if close["closed"]:
        by = "/".join(close["closed_by"]) or "unknown"
        kind = "reset" if close["reset"] else "fin"
        lines.append(f"data close: {kind} by {by} (clean={close['clean']})")
    else:
        lines.append("data close: not observed")
    terminal = summary["terminal_reply"]
    if terminal:
        lines.append(f"terminal reply: {terminal['text']}")
    else:
        lines.append("terminal reply: missing")
    final = summary["final_reply"]
    if final and final["text"] != terminal["text"]:
        lines.append(f"final reply: {final['text']}")
    if summary["trailing_replies"]:
        codes = ", ".join(
            str(reply["code"]) for reply in summary["trailing_replies"]
        )
        lines.append(f"trailing replies: {codes}")
    if summary["late_replies"]:
        codes = ", ".join(
            str(reply["code"]) for reply in summary["late_replies"]
        )
        lines.append(f"late replies kept with this transfer: {codes}")
    if summary["reasons"]:
        lines.append("reasons: " + ", ".join(summary["reasons"]))
    for warning in result.warnings + primary.warnings:
        lines.append(f"warning: {warning}")
    return "\n".join(lines)


def main(args=None):
    opts = parse_args(args=args)
    try:
        fmt = _detect_format(opts.capture, opts.format)
        events, warnings, _segment_count = _load(opts.capture, fmt)
        result = analyze_events(
            events,
            expected_size=opts.size,
        )
        if opts.restart is not None:
            for transfer in result.transfers:
                if transfer.restart_source is None:
                    transfer.restart_offset = opts.restart
                    transfer.restart_source = "option"
        for transfer in result.transfers:
            transfer.finalize()
        result.warnings[:0] = warnings
    except (OSError, ValueError) as err:
        print(f"error: {err}", file=sys.stderr)
        return _EXIT_ERROR

    if opts.json:
        print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    else:
        print(render_text(result))

    if opts.strict and (
        result.warnings or any(item.warnings for item in result.transfers)
    ):
        return _EXIT_ERROR
    if result.primary is None:
        return _EXIT_UNKNOWN
    return _STATE_EXIT.get(result.primary.state, _EXIT_UNKNOWN)
