"""High-level audit: completion windows, timeout attribution and bytes.

The audit combines the two scenarios solved by :mod:`schedule_audit.engine`
and separates statements into two evidence levels:

- **provable**: holds under every schedule compatible with the
  recorded evidence (clock skew, durations, resources and chains);
- **possible**: supported by at least one compatible schedule but not
  guaranteed under all of them.

Nothing here treats removing a throttle wait as a guarantee that
network transfers or predecessor work finish immediately: IO and work
durations are untouched by the counterfactual run, and mixed-cause
waits keep the time their other reasons may still need.
"""

from dataclasses import dataclass
from dataclasses import field

from .constraints import validate_trace
from .engine import clock_window
from .engine import counterfactual_duration
from .engine import critical_path_guarantee
from .engine import io_events
from .engine import solve_scenario

EPS = 1e-9


@dataclass
class WaitAttribution:
    event: str
    material: str
    reasons: tuple
    removable: bool
    duration_min: float
    duration_max: float
    counterfactual_min: float
    counterfactual_max: float
    # Provable throttle-attributable share under the counterfactual
    # rules: 0 unless the wait is a pure removable throttle wait.
    provable_min: float
    provable_max: float
    classification: str


@dataclass
class MaterialAudit:
    name: str
    deadline: "float | None"
    baseline: dict
    counterfactual: dict
    timeout_state: str
    improvement_state: str
    guaranteed_reduction: float
    max_possible_reduction: float
    counterfactual_relaxed: bool
    timeout_relief_state: str
    counterfactual_anchored_window: "dict | None"
    counterfactual_relaxed_window: dict


@dataclass
class ByteLedger:
    wire_bytes: int = 0
    payload_bytes: int = 0
    retransmit_bytes: int = 0
    per_event: list = field(default_factory=list)


@dataclass
class AuditResult:
    feasible: bool
    warnings: list
    baseline: dict
    counterfactual: dict
    materials: list
    waits: list
    bytes: "ByteLedger"
    clock_skew: float

    def as_dict(self):
        return {
            "feasible": self.feasible,
            "warnings": self.warnings,
            "clock_skew": self.clock_skew,
            "baseline": _jsonable(self.baseline),
            "counterfactual": _jsonable(self.counterfactual),
            "materials": [
                _jsonable(_material_to_dict(item))
                for item in self.materials
            ],
            "waits": [_jsonable(item.__dict__.copy()) for item in self.waits],
            "bytes": {
                "wire_bytes": self.bytes.wire_bytes,
                "payload_bytes": self.bytes.payload_bytes,
                "retransmit_bytes": self.bytes.retransmit_bytes,
                "per_event": list(self.bytes.per_event),
            },
        }


def _jsonable(value):
    """Replace non-JSON floats (infinity) with ``None`` recursively."""
    if isinstance(value, float):
        return None if value == float("inf") else value
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _material_to_dict(item):
    data = item.__dict__.copy()
    return data


def audit_trace(trace):
    """Run the full audit and return an :class:`AuditResult`."""
    warnings = validate_trace(trace)
    baseline = solve_scenario(trace, counterfactual=False)
    counterfactual = solve_scenario(trace, counterfactual=True)

    if not baseline["feasible"]:
        return AuditResult(
            feasible=False,
            warnings=warnings,
            baseline=baseline,
            counterfactual=counterfactual,
            materials=[],
            waits=[],
            bytes=_byte_ledger(trace),
            clock_skew=trace.clock_skew,
        )
    if not counterfactual["feasible"]:  # pragma: no cover - defensive
        raise RuntimeError(
            "counterfactual scenario is infeasible even though the "
            "baseline is feasible; the trace model is inconsistent"
        )

    guarantees = critical_path_guarantee(trace)
    material_audits = []
    relaxed_cf = counterfactual.get("relaxed_envelope", counterfactual)
    for material in trace.materials:
        base = baseline["materials"][material.name]
        cf = counterfactual["materials"][material.name]
        cf_relaxed = relaxed_cf["materials"][material.name]
        timeout_state = _classify_timeout(material, base, trace.clock_skew)
        guaranteed = guarantees[material.name]
        max_reduction = max(
            0.0, base["latest_end"] - cf_relaxed["earliest_end"]
        )
        improvement_state = _classify_improvement(
            guaranteed, max_reduction
        )
        relief_state = _classify_relief(
            material, cf_relaxed, trace.clock_skew
        )
        material_audits.append(
            MaterialAudit(
                name=material.name,
                deadline=material.deadline,
                baseline=dict(base),
                counterfactual=dict(cf),
                timeout_state=timeout_state,
                improvement_state=improvement_state,
                guaranteed_reduction=round(guaranteed, 9),
                max_possible_reduction=round(max_reduction, 9),
                counterfactual_relaxed=counterfactual.get(
                    "relaxed", False
                ),
                timeout_relief_state=relief_state,
                counterfactual_anchored_window=(
                    dict(cf) if counterfactual.get("anchored_feasible")
                    else None
                ),
                counterfactual_relaxed_window=dict(cf_relaxed),
            )
        )

    waits = [
        _attribute_wait(event, trace)
        for event in trace.all_events
        if event.kind == "wait"
    ]

    return AuditResult(
        feasible=True,
        warnings=warnings,
        baseline=baseline,
        counterfactual=counterfactual,
        materials=material_audits,
        waits=waits,
        bytes=_byte_ledger(trace),
        clock_skew=trace.clock_skew,
    )


def _classify_timeout(material, window, skew):
    if material.deadline is None:
        return "no_deadline"
    deadline_lo = material.deadline - skew
    deadline_hi = material.deadline + skew
    if window["earliest_end"] > deadline_hi + EPS:
        # Even the earliest feasible completion lands after the latest
        # reading the true deadline could correspond to.
        return "timeout_proven"
    if window["latest_end"] <= deadline_lo - EPS:
        return "within_deadline_proven"
    return "timeout_possible"


def _classify_improvement(guaranteed, max_reduction):
    """Separate provable from possible improvement.

    ``guaranteed`` is the critical-path reduction that survives every
    resource ordering and assumes nothing about mixed-cause waits.  A
    positive value means the improvement is provable; ``max_reduction``
    bounds what an optimistic schedule could achieve.
    """
    if guaranteed > EPS:
        return "improvement_proven"
    if max_reduction > EPS:
        return "improvement_possible"
    return "no_improvement_possible"


def _classify_relief(material, cf_relaxed, skew):
    """Whether removing the allowed waits can clear a proven timeout.

    Only the earliest achievable no-throttle completion can rescue a
    timeout; if even that stays past the latest reading the true
    deadline could correspond to, the timeout cannot be attributed to
    throttling.  Otherwise relief is possible, but the relaxed
    counterfactual suffix has no clock upper bound, so meeting the
    deadline can never be proven for every schedule.
    """
    if material.deadline is None:
        return "no_deadline"
    deadline_hi = material.deadline + skew
    if cf_relaxed["earliest_end"] > deadline_hi + EPS:
        return "timeout_remains_in_every_schedule"
    return "relief_possible_not_proven"


def _attribute_wait(event, trace):
    cf_min, cf_max = counterfactual_duration(
        event, trace.removed_waits
    )
    provable_min = event.duration_min - cf_max
    provable_max = event.duration_max - cf_min
    provable_min = max(0.0, provable_min)
    provable_max = max(0.0, provable_max)

    if event.removable_throttle and event.name in trace.removed_waits:
        if event.has_other_reason:
            classification = "mixed_cause_partial"
        else:
            classification = "throttle_fully_attributable"
    elif event.is_throttled:
        classification = "throttle_not_removable"
    else:
        classification = "non_throttle_wait"

    return WaitAttribution(
        event=event.name,
        material=event.material,
        reasons=event.reasons,
        removable=event.removable_throttle
        and event.name in trace.removed_waits,
        duration_min=event.duration_min,
        duration_max=event.duration_max,
        counterfactual_min=cf_min,
        counterfactual_max=cf_max,
        provable_min=round(provable_min, 9),
        provable_max=round(provable_max, 9),
        classification=classification,
    )


def _byte_ledger(trace):
    ledger = ByteLedger()
    for event in io_events(trace):
        transfer = event.transfer
        ledger.wire_bytes += transfer.wire_bytes
        ledger.payload_bytes += transfer.payload_bytes
        ledger.retransmit_bytes += transfer.retransmit_bytes
        ledger.per_event.append(
            {
                "event": event.name,
                "material": event.material,
                "wire_bytes": transfer.wire_bytes,
                "payload_bytes": transfer.payload_bytes,
                "retransmit_bytes": transfer.retransmit_bytes,
            }
        )
    return ledger


def event_time_windows(trace):
    """Clock-reading-based true-time windows for every event, shown on
    the same time basis as the rest of the report.
    """
    return {
        event.name: clock_window(event, trace.clock_skew)
        for event in trace.all_events
    }
