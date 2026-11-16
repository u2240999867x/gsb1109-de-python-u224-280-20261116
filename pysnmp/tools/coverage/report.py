"""Render coverage verdicts into a polling set and readable reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pysnmp.tools.coverage.engine import (
    ACCESS_RANK,
    CompliancePolicy,
    DeviceDeclaration,
    MibCatalog,
    ObjectCoverage,
    ObjectRef,
)

VERDICT_LABEL = {
    "satisfied": "satisfied",
    "partial": "partially satisfied",
    "unsatisfied": "not satisfiable",
    "waived": "waived (inactive condition)",
}


def build_polling_plan(
    coverage: dict[ObjectRef, ObjectCoverage]
) -> list[dict[str, Any]]:
    """One polling entry per object, no matter how many groups require it."""
    plan: list[dict[str, Any]] = []
    for ref in sorted(coverage, key=lambda item: coverage[item].node.oid):
        item = coverage[ref]
        if item.polling is None:
            continue
        entry = dict(item.polling)
        entry["object"] = str(ref)
        entry["verdict"] = item.verdict
        entry["required_min_access"] = item.effective_min_access
        entry["device_access"] = item.device_access
        plan.append(entry)
    return plan


def build_object_report(
    coverage: dict[ObjectRef, ObjectCoverage]
) -> list[dict[str, Any]]:
    report: list[dict[str, Any]] = []
    for ref in sorted(coverage, key=lambda item: coverage[item].node.oid):
        item = coverage[ref]
        report.append(
            {
                "object": str(ref),
                "oid": ".".join(str(part) for part in item.node.oid),
                "kind": item.node.kind,
                "required_min_access": item.effective_min_access,
                "device_access": item.device_access,
                "verdict": item.verdict,
                "reasons": list(item.reasons),
                "evidence": [
                    {
                        "source": evidence.source,
                        "basis": evidence.ref,
                        "requirement": evidence.requirement,
                        "min_access": evidence.min_access,
                        "condition": evidence.condition,
                        "condition_active": evidence.condition_active,
                        "group": str(evidence.group) if evidence.group else None,
                        "verdict": evidence.verdict,
                        "detail": evidence.detail,
                    }
                    for evidence in item.evidences
                ],
            }
        )
    return report


def write_json_outputs(
    out_dir: Path,
    plan: list[dict[str, Any]],
    object_report: list[dict[str, Any]],
    policy: CompliancePolicy,
    device: DeviceDeclaration,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "polling-set.json").write_text(
        json.dumps(plan, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    payload = {
        "policy": policy.name,
        "device": device.name,
        "summary": _summary(object_report),
        "objects": object_report,
    }
    (out_dir / "coverage-report.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _summary(object_report: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {
        "satisfied": 0,
        "partial": 0,
        "unsatisfied": 0,
        "waived": 0,
    }
    for entry in object_report:
        counts[entry["verdict"]] += 1
    return {
        "total_objects": len(object_report),
        **counts,
    }


def render_text_report(
    catalog: MibCatalog,
    policy: CompliancePolicy,
    device: DeviceDeclaration,
    coverage: dict[ObjectRef, ObjectCoverage],
    plan: list[dict[str, Any]],
    object_report: list[dict[str, Any]],
) -> str:
    lines: list[str] = []
    lines.append(f"# Collection coverage report: {device.name}")
    lines.append("")
    lines.append(f"Policy: {policy.name}")
    lines.append(
        "Locked MIB fact source: compiled modules "
        f"{', '.join(sorted(set(catalog.builder.mibSymbols)))}"
    )
    lines.append("")
    summary = _summary(object_report)
    lines.append("## Summary")
    lines.append("")
    lines.append(
        f"Objects in scope: {summary['total_objects']} | satisfied: "
        f"{summary['satisfied']} | partial: {summary['partial']} | "
        f"not satisfiable: {summary['unsatisfied']} | waived: "
        f"{summary['waived']}"
    )
    lines.append(f"De-duplicated polling entries: {len(plan)}")
    lines.append("")

    lines.extend(_render_group_section(policy, device, catalog))
    lines.extend(_render_min_access_section(policy, coverage))
    lines.extend(_render_polling_section(plan))
    lines.extend(_render_partial_section(object_report))
    lines.extend(_render_trace_section(object_report))
    return "\n".join(lines) + "\n"


def _render_group_section(
    policy: CompliancePolicy,
    device: DeviceDeclaration,
    catalog: MibCatalog,
) -> list[str]:
    lines = [
        "## Requirement classes",
        "",
        "### Mandatory groups",
        "",
        "Mandatory groups come from un-conditional GROUP clauses of the "
        "claimed MODULE-COMPLIANCE. Every member object must be pollable at "
        "at least read-only; a single unsupported member fails the group.",
        "",
    ]
    mandatory = [g for g in policy.groups.values() if g.requirement == "mandatory"]
    if not mandatory:
        lines.append("- (none)")
    for group in mandatory:
        declaration = device.groups[group.ref]
        members = catalog.group_members(group.ref)
        lines.append(
            f"- `{group.ref}` -> {len(members)} objects; device "
            f"supported={declaration.supported}"
            + (f" ({declaration.reason})" if declaration.reason else "")
        )
    lines.extend(
        [
            "",
            "### Conditional optional groups",
            "",
            "Conditional groups only bind the device when their stated "
            "condition is active. The device declaration must answer each "
            "condition explicitly; an inactive condition is recorded as a "
            "traceable waiver, not as coverage.",
            "",
        ]
    )
    conditional = [
        g for g in policy.groups.values() if g.requirement == "conditional"
    ]
    if not conditional:
        lines.append("- (none)")
    for group in conditional:
        declaration = device.groups[group.ref]
        state = declaration.condition_active
        lines.append(
            f"- `{group.ref}` condition: {group.condition}; device "
            f"condition_active={state}, supported={declaration.supported}"
            + (f" ({declaration.reason})" if declaration.reason else "")
        )
    lines.append("")
    return lines


def _render_min_access_section(
    policy: CompliancePolicy,
    coverage: dict[ObjectRef, ObjectCoverage],
) -> list[str]:
    lines = [
        "### Minimum access levels (MIN-ACCESS)",
        "",
        "Each compliance requirement carries a minimum access level. The "
        "default for a read-only collection is read-only; explicit "
        "MIN-ACCESS clauses can raise it (e.g. read-write, read-create). "
        "The object's effective requirement is the highest minimum across "
        "all groups/clauses that require it. The locked MIB MAX-ACCESS, "
        "adjusted by device access refinements, must meet that minimum.",
        "",
        "Access ordering: "
        + " < ".join(ACCESS_RANK),
        "",
    ]
    touched = False
    for ref, item in sorted(
        coverage.items(), key=lambda pair: pair[1].node.oid
    ):
        explicit = [
            evidence
            for evidence in item.evidences
            if evidence.min_access != "read-only"
            and evidence.verdict != "condition-inactive"
        ]
        if not explicit:
            continue
        touched = True
        lines.append(
            f"- `{ref}` effective minimum: {item.effective_min_access}; "
            f"device offers: {item.device_access}"
        )
    if not touched:
        lines.append("- (no explicit MIN-ACCESS overrides; all default to read-only)")
    lines.append("")
    return lines


def _render_polling_section(plan: list[dict[str, Any]]) -> list[str]:
    lines = [
        "## Polling set (de-duplicated)",
        "",
        "Scalars use GET on the `.0` instance. Table columns use a subtree "
        "walk (GETNEXT/GETBulk): a column OID is a schema node, so one GET "
        "against it returns noSuchInstance and can never prove the table is "
        "fully populated. Each entry lists every declaration that requires "
        "it, while the object is polled only once.",
        "",
    ]
    for entry in plan:
        lines.append(
            f"- [{entry['operation']}] `{entry['target']}` "
            f"({entry['oid']}) verdict={entry['verdict']} "
            f"min-access={entry['required_min_access']} "
            f"device={entry['device_access']}"
        )
        lines.append(f"  basis: {', '.join(entry['dedup_sources'])}")
        if entry["operation"] == "WALK":
            lines.append(
                "  index encoded in instance OID suffix: "
                + ", ".join(entry["index_columns"])
            )
    lines.append("")
    return lines


def _render_partial_section(object_report: list[dict[str, Any]]) -> list[str]:
    lines = [
        "## Requirements only partially satisfiable",
        "",
        "Partially satisfied means an active requirement is weakened "
        "(unsupported group, access deficit or no pollable instance). "
        "Waived objects carry only inactive conditional requirements and "
        "are excluded from the polling set by design.",
        "",
    ]
    flagged = [
        entry
        for entry in object_report
        if entry["verdict"] in ("partial", "unsatisfied", "waived")
    ]
    if not flagged:
        lines.append("- None: every in-scope requirement is fully satisfiable.")
        lines.append("")
        return lines
    for entry in flagged:
        lines.append(
            f"- `{entry['object']}` ({VERDICT_LABEL[entry['verdict']]}):"
        )
        for reason in entry["reasons"]:
            lines.append(f"  - {reason}")
    lines.append("")
    return lines


def _render_trace_section(object_report: list[dict[str, Any]]) -> list[str]:
    lines = [
        "## Evidence trace",
        "",
        "Every verdict below is derived only from the locked MIB "
        "definition, the compliance policy and the device declaration. "
        "When an object is required by several groups, each group keeps its "
        "own evidence row while the object still appears once in the polling "
        "set.",
        "",
    ]
    for entry in object_report:
        lines.append(
            f"- `{entry['object']}` [{entry['kind']}] => {entry['verdict']}"
        )
        for evidence in entry["evidence"]:
            condition = (
                f", condition={evidence['condition']} "
                f"active={evidence['condition_active']}"
                if evidence["condition"]
                else ""
            )
            lines.append(
                f"  - {evidence['source']} `{evidence['basis']}` "
                f"requirement={evidence['requirement']}, "
                f"min-access={evidence['min_access']}{condition} "
                f"-> {evidence['verdict']}"
                + (f" ({evidence['detail']})" if evidence["detail"] else "")
            )
    lines.append("")
    return lines
