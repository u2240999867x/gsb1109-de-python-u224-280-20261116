"""Timezone declarations found in date strings, and how they are interpreted.

A date string can carry a timezone in two ways: a UTC offset such as
``+05:30``, which fixes the instant, or an abbreviation such as ``EST``, which
names a rule whose offset may change with daylight saving time. Either way the
text is a *declaration*. A source can also declare the timezone of its local
times outside the string, through the ``SOURCE_TIMEZONE`` setting.

The two declarations can disagree. ``TIMEZONE_CONFLICT`` says what to do then,
and the disagreement is recorded in the result either way, so it is never
silently resolved.

Some wall times have no single reading because of daylight saving time: they
occur twice when the clock goes back (*ambiguous*) or not at all when it goes
forward (*nonexistent*). The parser does not choose one on its own;
``AMBIGUOUS_TIME`` says what to do, and its default is to raise.

:class:`TimezoneResolution` keeps the declaration as written, the zone that was
actually used, the conflict if there was one, and the original wall time. The
last one matters after ``TO_TIMEZONE``: the returned datetime is in the target
zone, but the local date the source wrote is still available.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Literal

from pytz import UTC

from .timezone_parser import StaticTzInfo

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .conf import Settings

TzDeclarationKind = Literal["offset", "abbreviation", "zone"]
DstStatus = Literal["unique", "ambiguous", "nonexistent", "not-applicable"]

_TIMEZONE_CONFLICT_VALUES = ("date", "settings", "error")
_AMBIGUOUS_TIME_VALUES = ("error", "earlier", "later")
TIMEZONE_CONFLICT_POLICIES = _TIMEZONE_CONFLICT_VALUES
AMBIGUOUS_TIME_POLICIES = _AMBIGUOUS_TIME_VALUES
_TIMEZONE_CONFLICT_ALIASES = {
    "date": "date",
    "string": "date",
    "prefer_date": "date",
    "prefer_string": "date",
    "settings": "settings",
    "source": "settings",
    "prefer_settings": "settings",
    "prefer_source": "settings",
    "error": "error",
    "raise": "error",
    "strict": "error",
}
_AMBIGUOUS_TIME_ALIASES = {
    "error": "error",
    "raise": "error",
    "strict": "error",
    "earlier": "earlier",
    "first": "earlier",
    "later": "later",
    "second": "later",
}

_OFFSET_TOKEN = re.compile(r"^(?:UTC|GMT)?\\?([+-])(\d{2}):?(\d{2})$")

_Datetime = datetime


class TimezoneConflictError(ValueError):
    """The date string and the source settings declare conflicting timezones.

    Raised with ``TIMEZONE_CONFLICT="error"``.
    """


class AmbiguousTimeError(ValueError):
    """A wall time is ambiguous or nonexistent and ``AMBIGUOUS_TIME`` is ``"error"``.

    Both readings are kept in :attr:`candidates`, so a caller can present them
    or choose one explicitly instead of guessing.
    """

    def __init__(
        self,
        message: str,
        candidates: Sequence[LocalTimeCandidate] = (),
        status: DstStatus = "ambiguous",
    ) -> None:
        super().__init__(message)
        self.candidates = tuple(candidates)
        self.status = status


@dataclass(frozen=True)
class TimezoneDeclaration:
    """A timezone declared for a date, as written at its source.

    :param value: the declaration as it appears, e.g. ``"EST"``, ``"-0500"``
        or ``"America/New_York"``.
    :param kind: ``"offset"`` for a UTC offset, ``"abbreviation"`` for a
        timezone abbreviation, or ``"zone"`` for a tz database name.
    :param source: ``"date"`` when the declaration is part of the date string,
        ``"settings"`` when it comes from ``SOURCE_TIMEZONE`` or ``TIMEZONE``.
    :param offset: the UTC offset of the declaration. For an offset it is the
        offset written; for an abbreviation the offset dateparser assigns to
        it; for a zone name it is informational, since the offset depends on
        the date.
    """

    value: str
    kind: TzDeclarationKind
    source: Literal["date", "settings"]
    offset: timedelta | None = None
    name: str | None = None
    """The name to give the tzinfo applied for a fixed declaration. Offset
    tokens keep their escaped form, as ``StaticTzInfo`` always has."""

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class LocalTimeCandidate:
    """One reading of a wall time that a zone maps to two instants."""

    datetime: _Datetime
    offset: timedelta
    abbreviation: str | None

    @property
    def utc(self) -> _Datetime:
        """The instant this reading stands for, in UTC."""
        return self.datetime.astimezone(UTC)

    @property
    def is_dst(self) -> bool:
        return bool(self.datetime.dst())

    def __str__(self) -> str:
        return f"{self.datetime.isoformat()} (UTC{offset_name(self.offset)})"

    def __repr__(self) -> str:
        return f"LocalTimeCandidate({self}, abbreviation={self.abbreviation!r})"


@dataclass(frozen=True)
class TimezoneResolution:
    """How the timezone of a parsed date was decided.

    ``declaration`` is the declaration as written, kept verbatim.
    ``applied`` is the zone used to turn the wall time into an instant; it can
    differ from the declaration when the conflict policy prefers the settings.
    ``output`` is the zone of the returned datetime, after ``TIMEZONE`` and
    ``TO_TIMEZONE``. ``local`` is the wall time as the source wrote it, before
    any conversion.
    """

    declaration: TimezoneDeclaration | None = None
    applied: tzinfo | None = None
    applied_offset: timedelta | None = None
    output: tzinfo | None = None
    output_offset: timedelta | None = None
    conflict: str | None = None
    policy: str | None = None
    local: datetime | None = None
    candidates: Sequence[LocalTimeCandidate] = field(default_factory=tuple)
    dst_status: DstStatus = "not-applicable"

    @property
    def declared(self) -> str | None:
        """The declaration as written, or ``None`` if the source made none."""
        return None if self.declaration is None else self.declaration.value

    @property
    def applied_name(self) -> str | None:
        return _zone_name(self.applied)

    @property
    def output_name(self) -> str | None:
        return _zone_name(self.output)

    @property
    def original_local_datetime(self) -> datetime | None:
        """The wall time as written, before any timezone conversion."""
        return self.local

    @property
    def original_local_date(self) -> object | None:
        """The calendar date the source wrote, before any conversion."""
        return None if self.local is None else self.local.date()

    @property
    def is_ambiguous(self) -> bool:
        return self.dst_status == "ambiguous"

    @property
    def is_nonexistent(self) -> bool:
        return self.dst_status == "nonexistent"

    def __str__(self) -> str:
        parts = []
        if self.declared is not None:
            parts.append(f"declared {self.declared}")
        if self.applied_name is not None:
            parts.append(f"applied {self.applied_name}")
        if self.conflict is not None:
            parts.append(f"conflict: {self.conflict}")
        if self.dst_status in ("ambiguous", "nonexistent"):
            parts.append(self.dst_status)
        return "; ".join(parts) if parts else "no timezone"


def offset_name(offset: timedelta) -> str:
    """Format *offset* as ``+HH:MM``."""
    total = int(offset.total_seconds())
    sign = "-" if total < 0 else "+"
    hours, remainder = divmod(abs(total), 3600)
    return f"{sign}{hours:02d}:{remainder // 60:02d}"


def _zone_name(zone: tzinfo | None) -> str | None:
    if zone is None:
        return None
    name = getattr(zone, "zone", None)
    if isinstance(name, str):
        return name
    name = zone.tzname(None)
    return name if isinstance(name, str) else str(zone)


def _offset_from_token(name: str) -> timedelta | None:
    match = _OFFSET_TOKEN.fullmatch(name)
    if not match:
        return None
    sign = -1 if match[1] == "-" else 1
    return sign * timedelta(hours=int(match[2]), minutes=int(match[3]))


def declaration_from_token(name: str, offset: timedelta) -> TimezoneDeclaration:
    """Build the declaration for a timezone token found in a date string."""
    parsed = _offset_from_token(name)
    if parsed is not None:
        return TimezoneDeclaration(
            value=name.replace("\\", ""),
            kind="offset",
            source="date",
            offset=parsed,
            name=name,
        )
    return TimezoneDeclaration(
        value=name, kind="abbreviation", source="date", offset=offset, name=name
    )


def declaration_from_offset(
    offset: timedelta, value: str | None = None
) -> TimezoneDeclaration:
    """Build a date declaration from a UTC offset."""
    return TimezoneDeclaration(
        value=value or f"UTC{offset_name(offset)}",
        kind="offset",
        source="date",
        offset=offset,
    )


def declaration_from_setting_value(value: str) -> TimezoneDeclaration | None:
    """Build a declaration for a timezone setting value."""
    from .utils import get_timezone_from_tz_string  # noqa: PLC0415

    if not value or "local" in value.lower():
        return None
    zone = get_timezone_from_tz_string(value)
    if getattr(zone, "zone", None):
        kind: TzDeclarationKind = "zone"
    elif _offset_from_token(value) is not None:
        kind = "offset"
    else:
        kind = "abbreviation"
    return TimezoneDeclaration(
        value=value, kind=kind, source="settings", offset=zone.utcoffset(None)
    )


def declaration_from_setting(settings: Settings) -> TimezoneDeclaration | None:
    """Return the declaration made by the timezone settings, if any.

    ``SOURCE_TIMEZONE`` takes precedence, since it says explicitly in which
    zone the source wrote its local times. Without it, a fixed ``TIMEZONE``
    keeps its historical meaning as the zone of a string that carries none.
    ``TIMEZONE="local"`` names no fixed zone, so it declares nothing.
    """
    source = getattr(settings, "SOURCE_TIMEZONE", False)
    if isinstance(source, str) and source:
        return declaration_from_setting_value(source)
    return declaration_from_setting_value(settings.TIMEZONE)


def display_declaration(settings: Settings) -> TimezoneDeclaration | None:
    """Return the declaration that decides how the result is displayed.

    This is ``TIMEZONE``, which dateparser has always used to localize the
    result, if it names a fixed zone.
    """
    return declaration_from_setting_value(settings.TIMEZONE)


def normalize_conflict_policy(value: object) -> str:
    """Return the canonical form of a ``TIMEZONE_CONFLICT`` value."""
    if isinstance(value, str):
        normalized = _TIMEZONE_CONFLICT_ALIASES.get(value.lower())
        if normalized is not None:
            return normalized
    return "date"


def normalize_ambiguous_policy(value: object) -> str:
    """Return the canonical form of an ``AMBIGUOUS_TIME`` value."""
    if isinstance(value, str):
        normalized = _AMBIGUOUS_TIME_ALIASES.get(value.lower())
        if normalized is not None:
            return normalized
    return "error"


def _aware_offset(aware: datetime) -> timedelta:
    """Return the UTC offset of an aware datetime, which always has one."""
    offset = aware.utcoffset()
    assert offset is not None
    return offset


def _readings(naive: datetime, zone: tzinfo) -> tuple[list[datetime] | None, DstStatus]:
    """Return the two readings of *naive* in *zone*, or ``None`` if unique.

    Works with ``pytz`` zones (which have ``localize``) and with standard
    ``tzinfo`` objects (which use ``fold``).
    """
    if hasattr(zone, "localize"):
        localize_zone = zone.localize
        try:
            localize_zone(naive, is_dst=None)
        except Exception as error:
            if type(error).__name__ == "AmbiguousTimeError":
                # is_dst=True is the first occurrence, is_dst=False the second.
                return [
                    localize_zone(naive, is_dst=True),
                    localize_zone(naive, is_dst=False),
                ], "ambiguous"
            if type(error).__name__ == "NonExistentTimeError":
                return [
                    localize_zone(naive, is_dst=False),
                    localize_zone(naive, is_dst=True),
                ], "nonexistent"
            raise
        return None, "unique"

    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    if first.utcoffset() == second.utcoffset():
        return None, "unique"
    round_trip = first.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    if round_trip != naive:
        # Neither reading is the wall time itself: the clock skipped it.
        return [first, second], "nonexistent"
    # fold=0 is the first occurrence, fold=1 the second.
    return [first, second], "ambiguous"


def _unique(naive: datetime, zone: tzinfo) -> datetime:
    if hasattr(zone, "localize"):
        localized: datetime = zone.localize(naive)
        return localized
    return naive.replace(tzinfo=zone)


def localize(
    naive: datetime, zone: tzinfo, policy: str = "error"
) -> tuple[datetime, tuple[LocalTimeCandidate, ...], DstStatus]:
    """Attach *zone* to *naive*, refusing to guess across a DST change.

    :param policy: ``"error"`` raises :class:`AmbiguousTimeError` for an
        ambiguous or nonexistent wall time; ``"earlier"`` and ``"later"``
        choose the earlier or later reading explicitly.
    """
    policy = normalize_ambiguous_policy(policy)
    readings, status = _readings(naive, zone)
    if readings is None:
        return _unique(naive, zone), (), "unique"

    if status == "ambiguous":
        # Order by instant: the pre-transition reading is the earlier one.
        readings = sorted(readings, key=lambda reading: reading.astimezone(UTC))
    else:
        # A gap has no instant; order by offset, i.e. the offset before and
        # after the clock moved.
        readings = sorted(readings, key=_aware_offset)

    candidates = tuple(
        LocalTimeCandidate(reading, _aware_offset(reading), reading.tzname())
        for reading in readings
    )

    if policy == "error":
        if status == "ambiguous":
            raise AmbiguousTimeError(
                f"{naive.isoformat()} is ambiguous in {_zone_name(zone)!r}: it "
                f"can be {candidates[0]} or {candidates[1]}; set AMBIGUOUS_TIME "
                f'to "earlier" or "later" to choose one',
                candidates,
                status,
            )
        raise AmbiguousTimeError(
            f"{naive.isoformat()} does not exist in {_zone_name(zone)!r}: the "
            f"clock moves from {candidates[0]} to {candidates[1]}; set "
            f'AMBIGUOUS_TIME to "earlier" or "later" to choose an offset',
            candidates,
            status,
        )

    chosen = candidates[0].datetime if policy == "earlier" else candidates[-1].datetime
    return chosen, candidates, status


def _zone_for_declaration(declaration: TimezoneDeclaration) -> tzinfo:
    """Return the tzinfo that pins the instant for *declaration*.

    A zone name follows the tz database, including its daylight-saving rules.
    An offset or abbreviation is fixed: looking an abbreviation up as a zone
    name would give that zone's local mean time for old dates, not the offset
    the abbreviation stands for.
    """
    if declaration.kind == "zone":
        from .utils import get_timezone_from_tz_string  # noqa: PLC0415

        return get_timezone_from_tz_string(declaration.value)
    offset = declaration.offset or timedelta(0)
    return StaticTzInfo(declaration.name or declaration.value, offset)


def _zone(declaration: TimezoneDeclaration) -> tzinfo:
    return _zone_for_declaration(declaration)


def _declaration_offsets(
    declaration: TimezoneDeclaration, naive: datetime
) -> set[timedelta]:
    """Return the offsets *declaration* can mean for the wall time *naive*."""
    if declaration.kind != "zone":
        return {declaration.offset} if declaration.offset is not None else set()

    zone = _zone(declaration)
    readings, _ = _readings(naive, zone)
    if readings is None:
        offset = _unique(naive, zone).utcoffset()
        return {offset} if offset is not None else set()
    return {
        offset
        for offset in (reading.utcoffset() for reading in readings)
        if offset is not None
    }


def conflict_between(
    date: TimezoneDeclaration,
    settings_declaration: TimezoneDeclaration,
    naive: datetime,
    source_setting: str = "SOURCE_TIMEZONE",
) -> str | None:
    """Describe how *date* and the settings disagree about the offset.

    A zone name is compared by the offsets it has for the date in question, so
    an offset that matches one side of a DST change is not a conflict.
    """
    if date.value.lower() == settings_declaration.value.lower():
        return None
    date_offsets = _declaration_offsets(date, naive)
    settings_offsets = _declaration_offsets(settings_declaration, naive)
    if not date_offsets or not settings_offsets:
        return None
    if date_offsets & settings_offsets:
        return None
    return (
        f"the date string declares {date.value!r} "
        f"(UTC{_format_offsets(date_offsets)}), but {source_setting} gives "
        f"{settings_declaration.value!r} (UTC{_format_offsets(settings_offsets)})"
    )


def _format_offsets(offsets: set[timedelta]) -> str:
    return " or ".join(offset_name(offset) for offset in sorted(offsets))


def _resolve_wall_time(
    naive: datetime,
    settings: Settings,
    date_declaration: TimezoneDeclaration | None,
) -> tuple[datetime, TimezoneResolution]:
    from .utils import _get_localzone  # noqa: PLC0415

    conflict_policy = normalize_conflict_policy(
        getattr(settings, "TIMEZONE_CONFLICT", "date")
    )
    dst_policy = normalize_ambiguous_policy(
        getattr(settings, "AMBIGUOUS_TIME", "error")
    )

    source_declaration = declaration_from_setting(settings)
    display = display_declaration(settings)
    if display is None:
        display = source_declaration

    conflict = None
    chosen = date_declaration or source_declaration
    if date_declaration is not None and source_declaration is not None:
        source_setting = (
            "SOURCE_TIMEZONE"
            if getattr(settings, "SOURCE_TIMEZONE", False)
            else "TIMEZONE"
        )
        conflict = conflict_between(
            date_declaration, source_declaration, naive, source_setting
        )
        if conflict is not None:
            if conflict_policy == "error":
                raise TimezoneConflictError(conflict)
            if conflict_policy == "settings":
                chosen = source_declaration

    if chosen is None:
        source_zone: tzinfo = _get_localzone()
        aware, candidates, status = localize(naive, source_zone, dst_policy)
    else:
        source_zone = _zone(chosen)
        if chosen.kind == "zone":
            aware, candidates, status = localize(naive, source_zone, dst_policy)
        else:
            aware = naive.replace(tzinfo=source_zone)
            candidates, status = (), "unique"

    applied_offset = aware.utcoffset()

    output_zone = source_zone
    if display is not None and _zone(display) is not source_zone:
        output_zone = _zone(display)
        aware = aware.astimezone(output_zone)

    if settings.TO_TIMEZONE:
        from .utils import get_timezone_from_tz_string  # noqa: PLC0415

        output_zone = get_timezone_from_tz_string(settings.TO_TIMEZONE)
        aware = aware.astimezone(output_zone)

    resolution = TimezoneResolution(
        declaration=date_declaration or source_declaration,
        applied=source_zone,
        applied_offset=applied_offset,
        output=output_zone,
        output_offset=aware.utcoffset(),
        conflict=conflict,
        policy=conflict_policy if conflict else None,
        local=naive,
        candidates=candidates,
        dst_status=status,
    )
    return aware, resolution


def build_resolution(
    *,
    date: datetime | None,
    local: datetime | None,
    declaration: TimezoneDeclaration | None,
    settings: Settings,
    conflict: str | None = None,
    source_zone: tzinfo | None = None,
) -> TimezoneResolution:
    """Describe an already interpreted date for the caller.

    *date* is the returned datetime, after any conversion. *source_zone*, when
    given, is the zone that pinned the instant, which differs from *date*'s
    zone after ``TO_TIMEZONE``. Without it, *date*'s zone is taken to be both.
    """
    source_zone = source_zone or (date.tzinfo if date is not None else None)
    return TimezoneResolution(
        declaration=declaration,
        applied=source_zone,
        applied_offset=(
            None
            if local is None or source_zone is None
            else local.replace(tzinfo=source_zone).utcoffset()
        ),
        output=date.tzinfo if date is not None else None,
        output_offset=None if date is None else date.utcoffset(),
        conflict=conflict,
        policy=(
            normalize_conflict_policy(getattr(settings, "TIMEZONE_CONFLICT", "date"))
            if conflict
            else None
        ),
        local=local,
    )


def timezone_for_declaration(
    declaration: TimezoneDeclaration | None,
) -> tzinfo | None:
    """Return the tzinfo a declaration pins its instant with, if any."""
    return None if declaration is None else _zone_for_declaration(declaration)


def resolve_wall_time(
    naive: datetime,
    settings: Settings,
    date_declaration: TimezoneDeclaration | None = None,
) -> tuple[datetime, TimezoneResolution]:
    """Turn a source wall time into an aware datetime.

    The instant comes from the date string's declaration, unless
    ``TIMEZONE_CONFLICT`` says the source settings win. The result is shown in
    ``TIMEZONE`` when that names a fixed zone, and in ``TO_TIMEZONE`` after
    conversion.
    """
    return _resolve_wall_time(naive, settings, date_declaration)


def resolve_aware(
    aware: datetime,
    settings: Settings,
    date_declaration: TimezoneDeclaration | None = None,
) -> tuple[datetime, TimezoneResolution]:
    """Interpret an already aware datetime, preserving its instant.

    Used for ``%z`` formats and timestamps, where the parsed value already
    fixes the instant. The wall time is kept as the original local time, and
    the conflict policy still applies to the declarations.
    """
    if date_declaration is None and aware.tzinfo is not None:
        offset = aware.utcoffset()
        if offset is not None:
            date_declaration = declaration_from_offset(offset)
    naive = aware.replace(tzinfo=None)
    return _resolve_wall_time(naive, settings, date_declaration)


def validate_policy(value: object, setting_name: str, allowed: Sequence[str]) -> None:
    """Raise ``SettingValidationError`` if *value* is not one of *allowed*."""
    if value in allowed:
        return
    from .conf import SettingValidationError  # noqa: PLC0415

    quoted = '", "'.join(str(option) for option in allowed)
    raise SettingValidationError(
        f'"{value}" is not a valid value for "{setting_name}", it should be: "{quoted}"'
    )


__all__ = [
    "AmbiguousTimeError",
    "DstStatus",
    "LocalTimeCandidate",
    "TimezoneConflictError",
    "TimezoneDeclaration",
    "TimezoneResolution",
    "build_resolution",
    "conflict_between",
    "declaration_from_offset",
    "declaration_from_setting",
    "declaration_from_token",
    "display_declaration",
    "localize",
    "normalize_ambiguous_policy",
    "normalize_conflict_policy",
    "offset_name",
    "resolve_aware",
    "resolve_wall_time",
    "validate_policy",
]
