"""Retention-window planning for collected material.

This module turns a declarative retention policy into auditable reclaim
windows. It supports three rule families:

* ``next_month_end`` -- the end of the month after the collection month;
* ``calendar_days`` -- a number of calendar days after collection, added to the
  collection timestamp (never normalized to 24-hour spans);
* ``settlement_business_days`` -- a number of business days after settlement,
  counted on an explicit business calendar; such a rule stays pending when the
  settlement anchor is unknown and never falls back to the collection time.

A plan also carries the requirements of derived materials and of every project
that shares an original. A shared original only enters the reclaimable list once
every effective requirement has been released.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .calendar import BusinessCalendar
from .correction import CorrectionReport, plan_correction
from .errors import RetentionError, RetentionInputError
from .model import (
    MATERIAL_DERIVED,
    MATERIAL_ORIGINAL,
    Material,
    Project,
    RetentionInput,
    parse_retention_input,
)
from .plan import (
    STATUS_HELD,
    STATUS_PENDING,
    STATUS_RECLAIMABLE,
    MaterialPlan,
    ProjectRetention,
    Requirement,
    RetentionPlan,
    build_plan,
)
from .policy import (
    RULE_CALENDAR_DAYS,
    RULE_MONTH_END,
    RULE_SETTLEMENT_BUSINESS_DAYS,
    RetentionRule,
    Window,
    describe_skipped_days,
    evaluate_rule,
    parse_policy,
)

__all__ = [
    "MATERIAL_DERIVED",
    "MATERIAL_ORIGINAL",
    "RULE_CALENDAR_DAYS",
    "RULE_MONTH_END",
    "RULE_SETTLEMENT_BUSINESS_DAYS",
    "STATUS_HELD",
    "STATUS_PENDING",
    "STATUS_RECLAIMABLE",
    "BusinessCalendar",
    "CorrectionReport",
    "Material",
    "MaterialPlan",
    "Project",
    "ProjectRetention",
    "Requirement",
    "RetentionError",
    "RetentionInput",
    "RetentionInputError",
    "RetentionPlan",
    "RetentionRule",
    "Window",
    "build_plan",
    "correction_from_dict",
    "correction_from_file",
    "describe_skipped_days",
    "evaluate_rule",
    "load_json",
    "parse_policy",
    "parse_retention_input",
    "plan_correction",
    "plan_from_dict",
    "plan_from_file",
    "plan_to_json",
]


def plan_from_dict(
    data: Any,
    *,
    as_of: Any = None,
    generated_at: Any = None,
) -> RetentionPlan:
    """Build a retention plan from a parsed JSON object.

    ``as_of`` decides which released windows are reclaimable; it defaults to
    ``generated_at``, which defaults to the current time in the calendar
    timezone.
    """
    retention_input = parse_retention_input(data)
    timezone = retention_input.calendar.timezone
    generated = (
        _coerce_moment(generated_at, timezone, "generated_at")
        if generated_at is not None
        else datetime.now(timezone)
    )
    moment = (
        _coerce_moment(as_of, timezone, "as_of") if as_of is not None else generated
    )
    return build_plan(retention_input, as_of=moment, generated_at=generated)


def _coerce_moment(value: Any, timezone: Any, field_name: str) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise RetentionInputError(f"{field_name} must be timezone-aware")
        return value.astimezone(timezone)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as error:
            raise RetentionInputError(
                f"{field_name} must be an ISO 8601 datetime, got {value!r}"
            ) from error
        if parsed.tzinfo is None:
            raise RetentionInputError(f"{field_name} must include a UTC offset")
        return parsed.astimezone(timezone)
    raise RetentionInputError(f"{field_name} must be a datetime or ISO 8601 string")


def plan_to_json(plan: RetentionPlan | CorrectionReport, *, indent: int = 2) -> str:
    """Serialize a plan or correction report to JSON."""
    return json.dumps(
        plan.to_dict(), ensure_ascii=False, indent=indent, sort_keys=False
    )


def load_json(path: str | Path) -> Any:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def plan_from_file(
    path: str | Path,
    *,
    as_of: Any = None,
    generated_at: Any = None,
) -> RetentionPlan:
    return plan_from_dict(load_json(path), as_of=as_of, generated_at=generated_at)


def correction_from_file(
    path: str | Path,
    changes_path: str | Path,
    *,
    as_of: Any = None,
    generated_at: Any = None,
) -> CorrectionReport:
    data = load_json(path)
    changes = load_json(changes_path)
    retention_input = parse_retention_input(data)
    timezone = retention_input.calendar.timezone
    generated = (
        _coerce_moment(generated_at, timezone, "generated_at")
        if generated_at is not None
        else datetime.now(timezone)
    )
    moment = (
        _coerce_moment(as_of, timezone, "as_of") if as_of is not None else generated
    )
    return plan_correction(
        data, policy_changes=changes, as_of=moment, generated_at=generated
    )


def correction_from_dict(
    data: Any,
    changes: Any,
    *,
    as_of: Any = None,
    generated_at: Any = None,
) -> CorrectionReport:
    retention_input = parse_retention_input(data)
    timezone = retention_input.calendar.timezone
    generated = (
        _coerce_moment(generated_at, timezone, "generated_at")
        if generated_at is not None
        else datetime.now(timezone)
    )
    moment = (
        _coerce_moment(as_of, timezone, "as_of") if as_of is not None else generated
    )
    return plan_correction(
        data, policy_changes=changes, as_of=moment, generated_at=generated
    )
