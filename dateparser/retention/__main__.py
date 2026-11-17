from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import correction_from_file, plan_from_file, plan_to_json
from .errors import RetentionError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m dateparser.retention",
        description="Build auditable retention/reclaim plans from a JSON policy.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="Build a retention plan.")
    plan.add_argument("--input", required=True, type=Path, help="Input JSON file.")
    plan.add_argument(
        "--as-of",
        default=None,
        help="Moment used to decide released windows (ISO 8601, with UTC offset).",
    )
    plan.add_argument(
        "--generated-at",
        default=None,
        help="Plan generation moment (ISO 8601, with UTC offset).",
    )
    plan.add_argument("--output", type=Path, default=None, help="Write JSON here.")
    plan.add_argument("--indent", type=int, default=2, help="JSON indentation.")

    correct = subparsers.add_parser(
        "correct", help="Plan the effect of a policy correction."
    )
    correct.add_argument("--input", required=True, type=Path, help="Input JSON file.")
    correct.add_argument(
        "--changes", required=True, type=Path, help="Policy correction JSON file."
    )
    correct.add_argument(
        "--as-of",
        default=None,
        help="Moment used to decide released windows (ISO 8601, with UTC offset).",
    )
    correct.add_argument(
        "--generated-at",
        default=None,
        help="Plan generation moment (ISO 8601, with UTC offset).",
    )
    correct.add_argument("--output", type=Path, default=None, help="Write JSON here.")
    correct.add_argument("--indent", type=int, default=2, help="JSON indentation.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            plan_payload = plan_from_file(
                args.input, as_of=args.as_of, generated_at=args.generated_at
            )
            rendered = plan_to_json(plan_payload, indent=args.indent)
        else:
            correction_payload = correction_from_file(
                args.input,
                args.changes,
                as_of=args.as_of,
                generated_at=args.generated_at,
            )
            rendered = plan_to_json(correction_payload, indent=args.indent)
    except RetentionError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    if args.output is not None:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
