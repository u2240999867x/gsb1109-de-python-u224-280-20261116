from __future__ import annotations

import calendar as _calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .calendar import BusinessCalendar, add_calendar_days, end_of_day, parse_datetime
from .errors import RetentionInputError

RULE_MONTH_END = "next_month_end"
RULE_CALENDAR_DAYS = "calendar_days"
RULE_SETTLEMENT_BUSINESS_DAYS = "settlement_business_days"

_MONTH_END_ALIASES = {
    "next_month_end",
    "month_end_after_next",
    "end_of_next_month",
    "monthly_month_end",
}
_CALENDAR_DAY_ALIASES = {
    "calendar_days",
    "collection_calendar_days",
    "days_after_collection",
}
_SETTLEMENT_ALIASES = {
    "settlement_business_days",
    "business_days_after_settlement",
    "settlement_workdays",
}


def _last_day_of_month(year: int, month: int) -> date:
    return date(year, month, _calendar.monthrange(year, month)[1])


@dataclass(frozen=True)
class RetentionRule:
    """A parsed retention policy rule.

    ``anchor`` names the event the window is measured from. Rules that need the
    settlement anchor stay :attr:`is_pending` while that anchor is unknown;
    they never fall back to the collection time.
    """

    id: str
    raw_text: str
    kind: str
    anchor: str
    days: int | None = None
    pending_reason: str | None = None

    @property
    def is_pending(self) -> bool:
        return self.pending_reason is not None

    @property
    def needs_settlement(self) -> bool:
        return self.kind == RULE_SETTLEMENT_BUSINESS_DAYS

    @property
    def is_shared_anchor(self) -> bool:
        return self.kind == RULE_MONTH_END

    @classmethod
    def from_dict(cls, data: Any, *, index: int = 0) -> RetentionRule:
        if isinstance(data, str):
            data = {"rule": data}
        if not isinstance(data, dict):
            raise RetentionInputError(
                f"policy rule {index} must be an object or string"
            )

        rule_id = data.get("id", f"rule-{index + 1}")
        raw_text = data.get("raw_text", data.get("text", data.get("rule", "")))
        if not isinstance(rule_id, str) or not rule_id:
            raise RetentionInputError(f"policy rule {index} has an invalid id")
        if not isinstance(raw_text, str):
            raise RetentionInputError(
                f"policy rule {rule_id!r} raw_text must be a string"
            )

        raw_kind = data.get("kind", data.get("type", data.get("rule", "")))
        if not isinstance(raw_kind, str):
            raise RetentionInputError(f"policy rule {rule_id!r} kind must be a string")
        kind = _normalize_kind(raw_kind, data)

        if kind == RULE_MONTH_END:
            return cls(
                id=rule_id,
                raw_text=raw_text,
                kind=kind,
                anchor="collection_period",
            )

        if kind == RULE_CALENDAR_DAYS:
            days = _parse_days(data, rule_id)
            return cls(
                id=rule_id,
                raw_text=raw_text,
                kind=kind,
                anchor="collection",
                days=days,
            )

        if kind == RULE_SETTLEMENT_BUSINESS_DAYS:
            days = _parse_days(data, rule_id)
            anchor = data.get("anchor", "settlement")
            if anchor not in {"settlement", "settlement_date"}:
                raise RetentionInputError(
                    f"policy rule {rule_id!r} uses business days but its anchor "
                    f"is {anchor!r}; only the settlement anchor is supported"
                )
            return cls(
                id=rule_id,
                raw_text=raw_text,
                kind=kind,
                anchor="settlement",
                days=days,
            )

        raise RetentionInputError(
            f"policy rule {rule_id!r} has unknown kind {raw_kind!r}; expected one of "
            f"{RULE_MONTH_END!r}, {RULE_CALENDAR_DAYS!r}, "
            f"{RULE_SETTLEMENT_BUSINESS_DAYS!r}"
        )


def _normalize_kind(raw_kind: str, data: dict[str, Any]) -> str:
    key = raw_kind.strip().lower()
    if key in _MONTH_END_ALIASES:
        return RULE_MONTH_END
    if key in _CALENDAR_DAY_ALIASES:
        return RULE_CALENDAR_DAYS
    if key in _SETTLEMENT_ALIASES:
        return RULE_SETTLEMENT_BUSINESS_DAYS
    if key in {"month_end", "next_month"}:
        return RULE_MONTH_END
    raise RetentionInputError(f"unknown retention rule kind {raw_kind!r}")


def _parse_days(data: dict[str, Any], rule_id: str) -> int:
    raw = data.get("days")
    if raw is None:
        raise RetentionInputError(f"policy rule {rule_id!r} must define days")
    if isinstance(raw, bool) or not isinstance(raw, int):
        if isinstance(raw, str) and raw.strip().isdigit():
            raw = int(raw.strip())
        else:
            raise RetentionInputError(
                f"policy rule {rule_id!r} days must be a positive integer"
            )
    if raw < 1:
        raise RetentionInputError(
            f"policy rule {rule_id!r} days must be a positive integer"
        )
    return raw


@dataclass(frozen=True)
class Window:
    """A concrete, auditable retention window.

    ``start`` and ``end`` are half-open: the material may be reclaimed from
    ``end`` onwards. ``pending`` windows keep the bounds of the anchors that
    are already known, and explain what is still missing.
    """

    rule_id: str
    raw_text: str
    kind: str
    anchor: str
    start: datetime
    end: datetime | None
    anchor_value: datetime | None
    basis: str
    notes: tuple[str, ...] = ()
    pending: str | None = None

    @property
    def is_pending(self) -> bool:
        return self.pending is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "raw_text": self.raw_text,
            "kind": self.kind,
            "anchor": self.anchor,
            "anchor_value": _iso(self.anchor_value),
            "reclaim_from": _iso(self.end),
            "window": {
                "start": _iso(self.start),
                "end": _iso(self.end),
            },
            "basis": self.basis,
            "notes": list(self.notes),
            "status": "pending" if self.is_pending else "resolved",
            "pending": self.pending,
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def evaluate_rule(
    rule: RetentionRule,
    *,
    collected_at: datetime,
    calendar: BusinessCalendar,
    settlement_at: datetime | None = None,
    collection_period: str | None = None,
) -> Window:
    """Evaluate *rule* against the supplied anchors.

    Calendar-day rules add calendar days to the collection timestamp; they are
    never converted to fixed 24-hour spans. Settlement rules count business
    days on *calendar* and stay pending when no settlement anchor is given.
    """
    if rule.kind == RULE_MONTH_END:
        return _evaluate_month_end(
            rule,
            collected_at=collected_at,
            calendar=calendar,
            collection_period=collection_period,
        )
    if rule.kind == RULE_CALENDAR_DAYS:
        return _evaluate_calendar_days(
            rule, collected_at=collected_at, calendar=calendar
        )
    if rule.kind == RULE_SETTLEMENT_BUSINESS_DAYS:
        return _evaluate_settlement(
            rule,
            collected_at=collected_at,
            calendar=calendar,
            settlement_at=settlement_at,
        )
    raise RetentionInputError(f"unknown retention rule kind {rule.kind!r}")


def _collection_day(collected_at: datetime, calendar: BusinessCalendar) -> date:
    return collected_at.astimezone(calendar.timezone).date()


def _evaluate_month_end(
    rule: RetentionRule,
    *,
    collected_at: datetime,
    calendar: BusinessCalendar,
    collection_period: str | None,
) -> Window:
    day = _collection_day(collected_at, calendar)
    if collection_period is not None:
        period_start = _parse_month(collection_period)
        notes = (
            f"collection period supplied as {period_start.year:04d}-{period_start.month:02d}",
        )
    else:
        period_start = day.replace(day=1)
        notes = ("collection period derived from the collection date",)

    if period_start.month == 12:
        next_month_start = date(period_start.year + 1, 1, 1)
    else:
        next_month_start = date(period_start.year, period_start.month + 1, 1)
    window_end_day = _last_day_of_month(next_month_start.year, next_month_start.month)

    next_month = f"{next_month_start.year:04d}-{next_month_start.month:02d}"
    basis = (
        f"end of the month after {next_month} ({next_month_start.year:04d}-"
        f"{next_month_start.month:02d}-{window_end_day.day:02d}), as a calendar date"
    )
    return Window(
        rule_id=rule.id,
        raw_text=rule.raw_text,
        kind=rule.kind,
        anchor=rule.anchor,
        start=collected_at,
        end=end_of_day(window_end_day, calendar.timezone),
        anchor_value=collected_at,
        basis=basis,
        notes=(
            *notes,
            "the last day of the month is kept as a calendar date, not as a 24-hour span",
            "reclaim_from is the first instant after that day in the calendar timezone",
        ),
    )


def _parse_month(value: str) -> date:
    text = value.strip()
    try:
        parsed = date.fromisoformat(f"{text}-01")
    except ValueError as error:
        raise RetentionInputError(
            f"collection period must be YYYY-MM, got {value!r}"
        ) from error
    return parsed.replace(day=1)


def _evaluate_calendar_days(
    rule: RetentionRule, *, collected_at: datetime, calendar: BusinessCalendar
) -> Window:
    assert rule.days is not None
    end, drift_notes = add_calendar_days(collected_at, rule.days, calendar.timezone)
    basis = (
        f"{rule.days} calendar days after collection "
        f"({collected_at.isoformat()} -> {end.isoformat()})"
    )
    notes = (
        "calendar days are added to the collection timestamp; no 24-hour "
        "normalization or business-day adjustment applies",
        f"reclaim_from is {end.isoformat()} in {calendar.timezone_name}",
        *drift_notes,
    )
    return Window(
        rule_id=rule.id,
        raw_text=rule.raw_text,
        kind=rule.kind,
        anchor=rule.anchor,
        start=collected_at,
        end=end,
        anchor_value=collected_at,
        basis=basis,
        notes=notes,
    )


def _evaluate_settlement(
    rule: RetentionRule,
    *,
    collected_at: datetime,
    calendar: BusinessCalendar,
    settlement_at: datetime | None,
) -> Window:
    assert rule.days is not None
    if settlement_at is None:
        return Window(
            rule_id=rule.id,
            raw_text=rule.raw_text,
            kind=rule.kind,
            anchor=rule.anchor,
            start=collected_at,
            end=None,
            anchor_value=None,
            basis=(
                f"{rule.days} business days after settlement, but the settlement "
                "anchor is not available"
            ),
            notes=(
                "the rule is anchored to settlement, not to collection",
                "no collection-relative fallback is applied",
            ),
            pending=(
                "missing settlement anchor: provide settlement_at (or an "
                "equivalent settlement event) to resolve this window"
            ),
        )

    settlement_day = settlement_at.astimezone(calendar.timezone).date()
    end_day = calendar.add_business_days(settlement_day, rule.days)
    trace = _business_day_trace(calendar, settlement_day, rule.days)
    skipped = describe_skipped_days(calendar, settlement_day, end_day)
    basis = (
        f"{rule.days} business days after settlement on "
        f"{calendar.business_day_label(settlement_day)} -> "
        f"{calendar.business_day_label(end_day)}"
    )
    notes = [
        "business days are counted on the supplied business calendar; "
        "weekends, holidays and extra working days are honoured",
        f"counted business days: {'; '.join(trace)}",
    ]
    if skipped:
        notes.append(f"skipped closed days: {'; '.join(skipped)}")
    notes.append(
        f"reclaim_from is the start of {end_day + timedelta(days=1)} in "
        f"{calendar.timezone_name}, the first instant after the {end_day.isoformat()} "
        "business day"
    )
    return Window(
        rule_id=rule.id,
        raw_text=rule.raw_text,
        kind=rule.kind,
        anchor=rule.anchor,
        start=collected_at,
        end=end_of_day(end_day, calendar.timezone),
        anchor_value=settlement_at,
        basis=basis,
        notes=tuple(notes),
    )


def _business_day_trace(
    calendar: BusinessCalendar, start: date, days: int
) -> list[str]:
    current = start
    trace: list[str] = []
    for _ in range(days):
        current = calendar.add_business_days(current, 1)
        trace.append(f"#{len(trace) + 1} {calendar.business_day_label(current)}")
    return trace


def describe_skipped_days(
    calendar: BusinessCalendar, start: date, end: date
) -> list[str]:
    """Explain which calendar days between *start* and *end* are not business days."""
    skipped: list[str] = []
    current = start + timedelta(days=1)
    while current < end:
        reason = calendar.closed_reason(current)
        if reason is not None:
            skipped.append(reason)
        current += timedelta(days=1)
    return skipped


def parse_policy(data: Any) -> list[RetentionRule]:
    if isinstance(data, dict):
        raw_rules = data.get("rules")
        if raw_rules is None:
            raw_rules = data.get("policies")
        if raw_rules is None:
            raise RetentionInputError("policy must define rules")
    else:
        raw_rules = data
    if not isinstance(raw_rules, (list, tuple)):
        raise RetentionInputError("policy rules must be a list")
    if not raw_rules:
        raise RetentionInputError("policy must define at least one rule")
    rules = [
        RetentionRule.from_dict(item, index=index)
        for index, item in enumerate(raw_rules)
    ]
    seen: set[str] = set()
    for rule in rules:
        if rule.id in seen:
            raise RetentionInputError(f"duplicate policy rule id {rule.id!r}")
        seen.add(rule.id)
    return rules


__all__ = [
    "RULE_CALENDAR_DAYS",
    "RULE_MONTH_END",
    "RULE_SETTLEMENT_BUSINESS_DAYS",
    "RetentionRule",
    "Window",
    "describe_skipped_days",
    "evaluate_rule",
    "parse_datetime",
    "parse_policy",
]
