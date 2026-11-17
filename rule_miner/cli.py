"""Command line entry point: python -m rule_miner problem.json."""

import argparse
import json
import sys

from .io import load_problem
from .miner import RuleMinerError, mine_rules


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mine one minimum-cost common CSS extraction rule set."
    )
    parser.add_argument("problem", help="path to the annotated JSON problem")
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default="text",
        help="output format (default: text)",
    )
    args = parser.parse_args(argv)

    try:
        pages, candidates, root_candidates = load_problem(args.problem)
        result = mine_rules(pages, candidates, root_candidates)
    except RuleMinerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.format == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(_render_text(result))
    return 0 if result["status"] == "optimal" else 1


def _render_text(result: dict) -> str:
    if result["status"] != "optimal":
        lines = ["No common rule set exists. Conflicting annotations:"]
        for conflict in result["conflicts"]:
            lines.append(" - " + json.dumps(conflict, ensure_ascii=False, sort_keys=True))
        if not result["conflicts"]:
            lines.append(" (no diagnostic available)")
        return "\n".join(lines)

    header = (
        "Tie:"
        if result["ties"]
        else "Unique optimum:"
    )
    lines = [header, ""]
    for index, solution in enumerate(result["solutions"], start=1):
        lines.append(f"[solution {index}] total_cost={solution['total_cost']}")
        if solution["root"] is not None:
            lines.append(
                f"  entity_root: {solution['root']} (cost {solution['root_cost']})"
            )
        for field_name, selector in solution["rules"].items():
            cost = solution["field_costs"][field_name]
            lines.append(f"  {field_name}: {selector} (cost {cost})")
        lines.append("")
    return "\n".join(lines).rstrip()


if __name__ == "__main__":
    raise SystemExit(main())
