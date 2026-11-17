from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .errors import RetentionInputError
from .model import parse_retention_input
from .plan import MaterialPlan, RetentionPlan, build_plan

_CORRECTABLE_MATERIAL_FIELDS = {"id", "rule_ids", "rules"}
_TIMESTAMP_FIELDS = {"collected_at", "collection_time", "settlement_at", "settlement"}


@dataclass
class CorrectionChange:
    material_id: str
    project: str
    before_status: str
    after_status: str
    before_reclaim_from: datetime | None
    after_reclaim_from: datetime | None
    before_rules: list[str]
    after_rules: list[str]
    reasons: list[str] = field(default_factory=list)

    @property
    def moved_later(self) -> bool:
        if self.before_reclaim_from is None or self.after_reclaim_from is None:
            return False
        return self.after_reclaim_from > self.before_reclaim_from

    @property
    def moved_earlier(self) -> bool:
        if self.before_reclaim_from is None or self.after_reclaim_from is None:
            return False
        return self.after_reclaim_from < self.before_reclaim_from

    def to_dict(self) -> dict[str, Any]:
        return {
            "material_id": self.material_id,
            "project": self.project,
            "before": {
                "status": self.before_status,
                "reclaim_from": _iso(self.before_reclaim_from),
                "rule_ids": list(self.before_rules),
            },
            "after": {
                "status": self.after_status,
                "reclaim_from": _iso(self.after_reclaim_from),
                "rule_ids": list(self.after_rules),
            },
            "change": _classify(self),
            "reasons": list(self.reasons),
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _classify(change: CorrectionChange) -> str:
    if change.before_status != change.after_status:
        return f"{change.before_status}_to_{change.after_status}"
    if change.moved_later:
        return "reclaim_later"
    if change.moved_earlier:
        return "reclaim_earlier"
    return "unchanged"


@dataclass
class CorrectionReport:
    generated_at: datetime
    as_of: datetime
    before: RetentionPlan
    after: RetentionPlan
    changes: list[CorrectionChange]
    rejected_changes: list[dict[str, Any]]
    collection_times_unchanged: bool

    def affected_materials(self) -> list[str]:
        return [change.material_id for change in self.changes]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "retention_policy_correction_plan",
            "generated_at": self.generated_at.isoformat(),
            "as_of": self.as_of.isoformat(),
            "collection_times_unchanged": self.collection_times_unchanged,
            "rejected_changes": list(self.rejected_changes),
            "affected_materials": self.affected_materials(),
            "changes": [change.to_dict() for change in self.changes],
            "before": {
                "calendar": {
                    "id": self.before.calendar_id,
                    "fingerprint": self.before.calendar_fingerprint,
                },
                "reclaimable": list(self.before.reclaimable()),
                "held": list(self.before.held()),
                "pending": list(self.before.pending()),
            },
            "after": {
                "calendar": {
                    "id": self.after.calendar_id,
                    "fingerprint": self.after.calendar_fingerprint,
                },
                "reclaimable": list(self.after.reclaimable()),
                "held": list(self.after.held()),
                "pending": list(self.after.pending()),
            },
        }


def plan_correction(
    data: Any,
    *,
    policy_changes: Any,
    as_of: datetime,
    generated_at: datetime,
) -> CorrectionReport:
    """Compare the current plan with a corrected policy without mutating input.

    Policy corrections are prospective: the report lists affected materials and
    their new windows, but the collection timestamps stored on the materials are
    carried over verbatim. A correction that tries to change a timestamp is
    rejected and reported instead of applied.
    """
    if not isinstance(data, dict):
        raise RetentionInputError("input must be a JSON object")
    if not isinstance(policy_changes, dict):
        raise RetentionInputError("policy_changes must be an object")
    if not isinstance(as_of, datetime) or not isinstance(generated_at, datetime):
        raise RetentionInputError(
            "as_of and generated_at must be timezone-aware datetime objects; "
            "use correction_from_dict() to pass ISO 8601 strings"
        )
    if as_of.tzinfo is None or generated_at.tzinfo is None:
        raise RetentionInputError("as_of and generated_at must be timezone-aware")

    original = copy.deepcopy(data)
    corrected = copy.deepcopy(data)
    rejected: list[dict[str, Any]] = []
    _apply_policy_changes(corrected, policy_changes, rejected)

    before_input = parse_retention_input(original)
    after_input = parse_retention_input(corrected)
    before_plan = build_plan(before_input, as_of=as_of, generated_at=generated_at)
    after_plan = build_plan(after_input, as_of=as_of, generated_at=generated_at)

    collection_times_unchanged = _collection_times(before_input) == _collection_times(
        after_input
    )
    if not collection_times_unchanged:
        raise RetentionInputError(
            "policy correction must not rewrite original collection times"
        )

    changes: list[CorrectionChange] = []
    for material_id, after in after_plan.materials.items():
        before = before_plan.materials.get(material_id)
        if before is None:
            continue
        change = _diff_material(before, after)
        if change is not None:
            changes.append(change)

    return CorrectionReport(
        generated_at=generated_at,
        as_of=as_of,
        before=before_plan,
        after=after_plan,
        changes=changes,
        rejected_changes=rejected,
        collection_times_unchanged=collection_times_unchanged,
    )


def _apply_policy_changes(
    corrected: dict[str, Any],
    policy_changes: dict[str, Any],
    rejected: list[dict[str, Any]],
) -> None:
    allowed = {
        "rules",
        "calendar",
        "materials",
        "projects",
        "reason",
        "note",
        "description",
    }
    unknown = sorted(set(policy_changes) - allowed)
    if unknown:
        raise RetentionInputError(f"policy_changes has unsupported fields: {unknown}")

    if "calendar" in policy_changes:
        corrected["calendar"] = copy.deepcopy(policy_changes["calendar"])

    if "rules" in policy_changes:
        if not isinstance(policy_changes["rules"], (list, tuple)):
            raise RetentionInputError("policy_changes.rules must be a list")
        corrected["policy"] = copy.deepcopy(policy_changes["rules"])
        corrected.pop("policies", None)

    raw_materials = policy_changes.get("materials", [])
    if not isinstance(raw_materials, (list, tuple)):
        raise RetentionInputError("policy_changes.materials must be a list")
    corrections = _collect_material_corrections(raw_materials, rejected)
    if not corrections:
        return

    materials = corrected.get("materials", [])
    if not isinstance(materials, list):
        raise RetentionInputError("input materials must be a list")
    for entry in materials:
        if not isinstance(entry, dict) or entry.get("id") not in corrections:
            continue
        correction = corrections[entry["id"]]
        for key, value in correction.items():
            if key != "id":
                entry[key] = copy.deepcopy(value)


def _collect_material_corrections(
    raw_materials: Any, rejected: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    corrections: dict[str, dict[str, Any]] = {}
    for item in raw_materials:
        if not isinstance(item, dict):
            raise RetentionInputError(
                "policy_changes.materials entries must be objects"
            )
        material_id = item.get("id")
        if not isinstance(material_id, str) or not material_id:
            raise RetentionInputError(
                "policy_changes.materials entries must define a string id"
            )
        if material_id in corrections:
            raise RetentionInputError(
                f"duplicate correction for material {material_id!r}"
            )
        timestamp_fields = sorted(set(item) & _TIMESTAMP_FIELDS)
        if timestamp_fields:
            rejected.append(
                {
                    "material_id": material_id,
                    "fields": timestamp_fields,
                    "reason": "policy corrections cannot rewrite collection or "
                    "settlement timestamps",
                }
            )
        unsupported = sorted(
            set(item) - _CORRECTABLE_MATERIAL_FIELDS - _TIMESTAMP_FIELDS
        )
        if unsupported:
            raise RetentionInputError(
                f"correction for material {material_id!r} has unsupported fields: "
                f"{unsupported}"
            )
        corrections[material_id] = {
            key: value
            for key, value in item.items()
            if key in _CORRECTABLE_MATERIAL_FIELDS and key != "id"
        }
    return corrections


def _collection_times(data: Any) -> dict[str, datetime]:
    return {material.id: material.collected_at for material in data.materials.values()}


def _diff_material(
    before: MaterialPlan, after: MaterialPlan
) -> CorrectionChange | None:
    before_rules = _rule_ids(before)
    after_rules = _rule_ids(after)
    reasons: list[str] = []
    if before.status != after.status:
        reasons.append(f"status {before.status} -> {after.status}")
    if before.reclaim_from != after.reclaim_from:
        reasons.append(
            f"reclaim_from {_iso(before.reclaim_from)} -> {_iso(after.reclaim_from)}"
        )
    if before_rules != after_rules:
        reasons.append(f"effective rules {before_rules} -> {after_rules}")
    if before.final_rule_id != after.final_rule_id:
        reasons.append(
            f"governing rule {before.final_rule_id} -> {after.final_rule_id}"
        )
    if not reasons:
        return None
    return CorrectionChange(
        material_id=after.material.id,
        project=after.material.project,
        before_status=before.status,
        after_status=after.status,
        before_reclaim_from=before.reclaim_from,
        after_reclaim_from=after.reclaim_from,
        before_rules=before_rules,
        after_rules=after_rules,
        reasons=reasons,
    )


def _rule_ids(plan: MaterialPlan) -> list[str]:
    seen: list[str] = []
    for requirement in plan.requirements:
        window = requirement.window
        if window is not None and window.rule_id not in seen:
            seen.append(window.rule_id)
    return seen


__all__ = ["CorrectionChange", "CorrectionReport", "plan_correction"]
