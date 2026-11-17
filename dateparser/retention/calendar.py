from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import RetentionInputError

_WEEKDAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_WEEKDAY_LABELS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def _parse_date(value: Any, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as error:
            raise RetentionInputError(
                f"{field_name} must be an ISO date (YYYY-MM-DD), got {value!r}"
            ) from error
    raise RetentionInputError(f"{field_name} must be an ISO date, got {value!r}")


def _parse_weekday(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise RetentionInputError(f"{field_name} must be a weekday, got {value!r}")
    if isinstance(value, int):
        if 0 <= value <= 6:
            return value
        raise RetentionInputError(
            f"{field_name} must be between 0 (Monday) and 6 (Sunday), got {value!r}"
        )
    if isinstance(value, str):
        name = value.strip().lower()
        if name in _WEEKDAY_NAMES:
            return _WEEKDAY_NAMES[name]
    raise RetentionInputError(f"{field_name} must be a weekday, got {value!r}")


def parse_datetime(
    value: Any, timezone: ZoneInfo, field_name: str
) -> tuple[datetime, str | None]:
    """Return *value* as an aware datetime in *timezone* and an audit note."""
    note = None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
        note = "date-only value interpreted as 00:00 in the calendar timezone"
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as error:
            raise RetentionInputError(
                f"{field_name} must be an ISO 8601 datetime, got {value!r}"
            ) from error
        if isinstance(parsed, datetime) and parsed.tzinfo is None:
            note = "naive value interpreted in the calendar timezone"
    else:
        raise RetentionInputError(
            f"{field_name} must be an ISO 8601 datetime, got {value!r}"
        )

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
        note = note or "naive value interpreted in the calendar timezone"
    return parsed.astimezone(timezone), note


def start_of_day(value: date, timezone: ZoneInfo) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone)


def end_of_day(value: date, timezone: ZoneInfo) -> datetime:
    return start_of_day(value + timedelta(days=1), timezone)


def add_calendar_days(
    value: datetime, days: int, timezone: ZoneInfo
) -> tuple[datetime, list[str]]:
    """Add calendar days while keeping the local wall-clock time.

    Adding calendar days is not the same as adding ``days * 24`` hours: a
    +30 day window that crosses a daylight-saving change must end at the same
    local time, so the offset may differ between the two ends.
    """
    local = value.astimezone(timezone)
    target_date = local.date() + timedelta(days=days)
    naive = datetime.combine(target_date, local.timetz().replace(tzinfo=None))
    notes: list[str] = []
    candidate = naive.replace(tzinfo=timezone, fold=0)
    round_trip = candidate.astimezone(ZoneInfo("UTC")).astimezone(timezone)
    if round_trip.replace(tzinfo=None) != naive:
        shifted = round_trip.replace(tzinfo=None)
        notes.append(
            f"local time {naive.isoformat()} does not exist on {target_date.isoformat()} "
            f"in {timezone.key}; used {shifted.isoformat()} instead"
        )
        return round_trip, notes
    if local.utcoffset() != candidate.utcoffset():
        notes.append(
            f"UTC offset changed from {local.utcoffset()} to "
            f"{candidate.utcoffset()} because of a daylight-saving transition; "
            "the local wall-clock time was preserved"
        )
    second = naive.replace(tzinfo=timezone, fold=1)
    if second.utcoffset() != candidate.utcoffset():
        notes.append(
            f"local time {naive.isoformat()} is ambiguous in {timezone.key}; "
            "the first occurrence (fold=0) was used"
        )
    return candidate, notes


@dataclass(frozen=True)
class BusinessCalendar:
    """Business calendar used by settlement-relative retention rules.

    The calendar is part of the audit trail: every plan records its id and a
    fingerprint over the complete opening rules.
    """

    id: str
    timezone: ZoneInfo
    timezone_name: str
    working_weekdays: frozenset[int]
    holidays: frozenset[date]
    extra_working_days: frozenset[date]
    holiday_labels: dict[date, str]
    extra_working_labels: dict[date, str]

    @classmethod
    def from_dict(cls, data: Any) -> BusinessCalendar:
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise RetentionInputError("calendar must be an object")
        timezone_name = data.get("timezone", "UTC")
        if not isinstance(timezone_name, str):
            raise RetentionInputError("calendar.timezone must be a string")
        try:
            timezone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise RetentionInputError(
                f"unknown calendar timezone {timezone_name!r}"
            ) from error

        raw_weekdays = data.get("working_weekdays", [0, 1, 2, 3, 4])
        if not isinstance(raw_weekdays, (list, tuple, set, frozenset)):
            raise RetentionInputError("calendar.working_weekdays must be a list")
        working_weekdays = frozenset(
            _parse_weekday(item, "calendar.working_weekdays") for item in raw_weekdays
        )
        if not working_weekdays:
            raise RetentionInputError("calendar.working_weekdays cannot be empty")

        holidays, holiday_labels = _parse_day_map(
            data.get("holidays", []), "calendar.holidays"
        )
        extra_working_days, extra_working_labels = _parse_day_map(
            data.get("working_days", data.get("extra_working_days", [])),
            "calendar.working_days",
        )
        overlap = holidays & extra_working_days
        if overlap:
            names = ", ".join(sorted(day.isoformat() for day in overlap))
            raise RetentionInputError(
                f"calendar dates cannot be both holidays and working days: {names}"
            )

        calendar_id = data.get("id", "calendar")
        if not isinstance(calendar_id, str):
            raise RetentionInputError("calendar.id must be a string")
        return cls(
            id=calendar_id,
            timezone=timezone,
            timezone_name=timezone_name,
            working_weekdays=working_weekdays,
            holidays=holidays,
            extra_working_days=extra_working_days,
            holiday_labels=holiday_labels,
            extra_working_labels=extra_working_labels,
        )

    def is_business_day(self, day: date) -> bool:
        if day in self.extra_working_days:
            return True
        if day in self.holidays:
            return False
        return day.weekday() in self.working_weekdays

    def business_day_label(self, day: date) -> str:
        if day in self.holiday_labels:
            return f"{day.isoformat()} ({self.holiday_labels[day]})"
        if day in self.extra_working_labels:
            return f"{day.isoformat()} ({self.extra_working_labels[day]})"
        return f"{day.isoformat()} ({_WEEKDAY_LABELS[day.weekday()]})"

    def next_business_day(self, day: date) -> date:
        return self.add_business_days(day, 1)

    def add_business_days(self, start: date, days: int) -> date:
        """Return the *days*-th business day strictly after *start*."""
        if days < 1:
            raise RetentionInputError("business day offset must be at least 1")
        current = start
        counted = 0
        inspected = 0
        while counted < days:
            current += timedelta(days=1)
            inspected += 1
            if inspected > 10000:
                raise RetentionInputError(
                    "cannot find a business day within 10000 calendar days; "
                    "check the business calendar"
                )
            if self.is_business_day(current):
                counted += 1
        return current

    def closed_reason(self, day: date) -> str | None:
        if self.is_business_day(day):
            return None
        if day in self.holidays:
            label = self.holiday_labels.get(day)
            return f"holiday {day.isoformat()}" + (f" ({label})" if label else "")
        return f"non-working weekday {_WEEKDAY_LABELS[day.weekday()]}"

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "timezone": self.timezone_name,
            "working_weekdays": sorted(self.working_weekdays),
            "holidays": {
                day.isoformat(): self.holiday_labels.get(day, "")
                for day in sorted(self.holidays)
            },
            "working_days": {
                day.isoformat(): self.extra_working_labels.get(day, "")
                for day in sorted(self.extra_working_days)
            },
        }

    @property
    def fingerprint(self) -> str:
        canonical = json.dumps(
            self.snapshot(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_day_map(
    value: Any, field_name: str
) -> tuple[frozenset[date], dict[date, str]]:
    if value is None:
        return frozenset(), {}
    days: set[date] = set()
    labels: dict[date, str] = {}
    if isinstance(value, dict):
        items = value.items()
        for raw_day, raw_label in items:
            day = _parse_date(raw_day, field_name)
            if day in days:
                raise RetentionInputError(
                    f"duplicate date {day.isoformat()} in {field_name}"
                )
            days.add(day)
            if raw_label:
                if not isinstance(raw_label, str):
                    raise RetentionInputError(f"{field_name} labels must be strings")
                labels[day] = raw_label
        return frozenset(days), labels
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise RetentionInputError(f"{field_name} must be a list or object")
    for item in value:
        day = _parse_date(item, field_name)
        if day in days:
            raise RetentionInputError(
                f"duplicate date {day.isoformat()} in {field_name}"
            )
        days.add(day)
    return frozenset(days), labels
