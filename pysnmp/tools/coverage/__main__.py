"""Command line entry point for compliance coverage analysis.

Example:
    python -m pysnmp.tools.coverage \
        --policy policy.json --device device.json \
        --metrics metrics.json --output-dir build/coverage
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from pysnmp.tools.coverage.engine import (
    MibCatalog,
    build_coverage,
    load_json,
    parse_device,
    parse_metrics,
    parse_policy,
)
from pysnmp.tools.coverage.report import (
    build_object_report,
    build_polling_plan,
    render_text_report,
    write_json_outputs,
)

REF_KEYS = {"group", "object"}


def _collect_modules(payload: Any, found: set[str]) -> None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in REF_KEYS and isinstance(value, str) and "::" in value:
                found.add(value.split("::", 1)[0])
            else:
                _collect_modules(value, found)
    elif isinstance(payload, list):
        for value in payload:
            _collect_modules(value, found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a compliance-driven SNMP polling set and "
        "coverage report."
    )
    parser.add_argument("--policy", required=True, type=Path)
    parser.add_argument("--device", required=True, type=Path)
    parser.add_argument("--metrics", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)

    policy_data = load_json(args.policy)
    device_data = load_json(args.device)
    metrics_data = load_json(args.metrics)

    modules: set[str] = set()
    for payload in (policy_data, device_data, metrics_data):
        _collect_modules(payload, modules)
    catalog = MibCatalog(sorted(modules))

    policy = parse_policy(policy_data, catalog)
    device = parse_device(device_data, catalog)
    metrics = parse_metrics(metrics_data, catalog)

    coverage = build_coverage(catalog, policy, device, metrics)
    plan = build_polling_plan(coverage)
    object_report = build_object_report(coverage)

    write_json_outputs(
        args.output_dir, plan, object_report, policy, device
    )
    text = render_text_report(
        catalog, policy, device, coverage, plan, object_report
    )
    report_path = args.output_dir / "coverage-report.md"
    report_path.write_text(text, encoding="utf-8")
    print(text)
    print(f"Artifacts written to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
