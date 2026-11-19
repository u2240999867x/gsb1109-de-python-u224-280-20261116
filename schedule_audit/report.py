"""Report rendering for :class:`schedule_audit.audit.AuditResult`."""

from .model import REASON_THROTTLE

_TIMEOUT_LABELS = {
    "no_deadline": "no deadline declared",
    "timeout_proven": "TIMEOUT provable in every schedule",
    "within_deadline_proven": "within deadline in every schedule",
    "timeout_possible": "timeout possible, not provable",
}

_IMPROVEMENT_LABELS = {
    "improvement_proven": "improvement provable in every schedule",
    "improvement_possible": "improvement possible, not provable",
    "no_improvement_possible": "no supported schedule improves",
}

_RELIEF_LABELS = {
    "no_deadline": "no deadline declared",
    "timeout_remains_in_every_schedule":
        "TIMEOUT remains in every no-throttle schedule: not "
        "attributable to throttling",
    "relief_possible_not_proven":
        "deadline can be met in some no-throttle schedule, not "
        "provable for all",
}


def _fmt(value):
    if value == float("inf"):
        return "inf"
    if value == float("-inf"):
        return "-inf"
    return f"{value:.3f}"


def _window(bounds, low_key, high_key):
    return f"[{_fmt(bounds[low_key])}, {_fmt(bounds[high_key])}]"


def report_as_dict(result):
    """JSON-serialisable view of the audit result."""
    return result.as_dict()


def report_as_text(result):
    """Human-readable report."""
    lines = []
    lines.append("SCHEDULE AUDIT REPORT")
    lines.append("=" * 72)
    lines.append(f"clock uncertainty per reading: +/- {result.clock_skew}")
    if not result.feasible:
        lines.append(
            "RESULT: the recorded evidence admits no feasible schedule"
        )
        return "\n".join(lines)
    lines.append(
        "feasible resource orderings (baseline): "
        f"{result.baseline['orderings_considered']} / "
        f"{result.baseline['orderings_enumerated']} enumerated"
    )
    cf = result.counterfactual
    if cf.get("anchored_feasible"):
        lines.append(
            "feasible resource orderings (no-throttle, anchored): "
            f"{cf['orderings_considered']} / "
            f"{cf['orderings_enumerated']} enumerated"
        )
    relaxed = cf.get("relaxed_envelope", cf)
    lines.append(
        "feasible resource orderings (no-throttle, relaxed): "
        f"{relaxed['orderings_considered']} / "
        f"{relaxed['orderings_enumerated']} enumerated"
    )
    if result.counterfactual.get("relaxed"):
        lines.append(
            "note: the anchored no-throttle system is infeasible, so "
            "the clock windows of "
            f"{', '.join(relaxed['relaxed_events'])} are relaxed; "
            "relaxed bounds are 'possible' evidence, not provable"
        )
    if result.warnings:
        lines.append("")
        lines.append("evidence warnings:")
        lines.extend(f"  - {msg}" for msg in result.warnings)

    lines.append("")
    lines.append("MATERIAL COMPLETION WINDOWS (true time)")
    lines.append("-" * 72)
    header = (
        f"{'material':<16}{'start window':<24}{'completion window':<24}"
        "verdict"
    )
    lines.append(header)
    for item in result.materials:
        start = _window(item.baseline, "earliest_start", "latest_start")
        end = _window(item.baseline, "earliest_end", "latest_end")
        lines.append(
            f"{item.name:<16}{start:<24}{end:<24}"
            + _TIMEOUT_LABELS[item.timeout_state]
        )

    lines.append("")
    lines.append("COUNTERFACTUAL: ALLOWED THROTTLE WAITS REMOVED")
    lines.append("-" * 72)
    for item in result.materials:
        end = _window(
            item.counterfactual, "earliest_end", "latest_end"
        )
        lines.append(
            f"{item.name:<16} completion {end:<24}"
            + _IMPROVEMENT_LABELS[item.improvement_state]
        )
        if item.counterfactual_anchored_window is not None:
            anchored_end = _window(
                item.counterfactual_anchored_window,
                "earliest_end",
                "latest_end",
            )
            lines.append(
                f"{'':<16} anchored envelope {anchored_end:<24}"
                "(provable evidence)"
            )
        relaxed_end = _window(
            item.counterfactual_relaxed_window,
            "earliest_end",
            "latest_end",
        )
        lines.append(
            f"{'':<16} relaxed envelope {relaxed_end:<24}"
            "(possible evidence only)"
        )
        guaranteed = item.guaranteed_reduction
        max_reduction = item.max_possible_reduction
        lines.append(
            f"{'':<16} guaranteed reduction >= {_fmt(guaranteed)}; "
            f"max possible reduction <= {_fmt(max_reduction)}"
        )
        lines.append(
            f"{'':<16} timeout attribution: "
            + _RELIEF_LABELS[item.timeout_relief_state]
        )

    lines.append("")
    lines.append("WAIT ATTRIBUTION (each interval counted once)")
    lines.append("-" * 72)
    for wait in result.waits:
        reasons = ",".join(wait.reasons)
        lines.append(
            f"{wait.event:<16} reasons={reasons:<24} "
            f"recorded [{wait.duration_min:.3f}, {wait.duration_max:.3f}]"
        )
        lines.append(
            f"{'':<16} no-throttle [{wait.counterfactual_min:.3f}, "
            f"{wait.counterfactual_max:.3f}]  "
            f"throttle-attributable [{wait.provable_min:.3f}, "
            f"{wait.provable_max:.3f}]  {wait.classification}"
        )

    lines.append("")
    lines.append("BYTE LEDGER (accounts kept separate)")
    lines.append("-" * 72)
    ledger = result.bytes
    lines.append(
        f"totals: wire={ledger.wire_bytes}  payload="
        f"{ledger.payload_bytes}  business_retransmit="
        f"{ledger.retransmit_bytes}"
    )
    for row in ledger.per_event:
        lines.append(
            f"  {row['event']:<14} wire={row['wire_bytes']:<8} "
            f"payload={row['payload_bytes']:<8} retransmit="
            f"{row['retransmit_bytes']}"
        )
    return "\n".join(lines)


def throttle_waits(result):
    """Convenience: all waits that name throttling as a reason."""
    return [
        wait
        for wait in result.waits
        if REASON_THROTTLE in wait.reasons
    ]
