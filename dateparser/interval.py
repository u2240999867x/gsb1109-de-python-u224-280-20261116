"""Parsing of date ranges, such as "1月31日至2月2日" or "December 30, 2024 -
January 3, 2025", into a pair of endpoints.

The two endpoints of a range share the parts that the shorter one omits: a
year, month or day written only once is applied to both endpoints, instead of
each endpoint being completed on its own with unrelated defaults.
"""

from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

import regex as re
from dateutil.relativedelta import relativedelta

from dateparser.conf import Settings, apply_settings
from dateparser.date import DateData, DateDataParser, sanitize_date
from dateparser.utils import _now

# Words that separate the two endpoints in the languages dateparser ships.
# CJK connectors and dashes are handled separately, since they need no word
# boundaries and dash characters are already normalized to "-".
_RANGE_WORD_CONNECTORS = (
    "till",
    "until",
    "through",
    "jusqu'a",
    "jusqu'au",
    "jusqu'aux",
    "fino al",
    "fino a",
    "hasta",
    "bis",
    "ate",
    "to",
    "al",
    "au",
    "do",
)
_RANGE_CJK_CONNECTORS = ("至", "到", "～", "~")

_RE_RANGE_WORD = re.compile(
    r"(?<![\w'])(?:{})(?![\w'])".format(
        "|".join(
            sorted(
                (re.escape(word) for word in _RANGE_WORD_CONNECTORS),
                key=len,
                reverse=True,
            )
        )
    ),
    flags=re.IGNORECASE,
)
_RE_RANGE_CJK = re.compile("(?:{})".format("|".join(map(re.escape, _RANGE_CJK_CONNECTORS))))
# A dash that stands on its own, rather than being part of a date like
# "2024-12-30": it is either bordered by whitespace or adjacent to a non-ASCII
# letter such as a Han character, where the two endpoints have no spaces.
_NON_ASCII_LETTER = r"[^\P{L}a-zA-Z]"
_RE_RANGE_DASH = re.compile(
    rf"(?<!\S)-(?!\S)|(?<={_NON_ASCII_LETTER})-|-(?={_NON_ASCII_LETTER})"
)

_PART_ORDER = ("year", "month", "day")
_ROLL = {"year": relativedelta(years=1), "month": relativedelta(months=1)}
_PRECISION_RANK = {"year": 1, "month": 2, "day": 3}


def _precision(data: DateData) -> str:
    """The granularity an endpoint itself states, so a phrase precise only to
    months is not given a day it never mentioned."""
    if "day" in data.parts:
        return "day"
    if "month" in data.parts:
        return "month"
    if "year" in data.parts:
        return "year"
    return "day"

# An endpoint that is only a day number: "10日", "5号", "31號" or an ordinal
# such as "5th". The generic parser reads the first ones as relative dates and
# the ordinals as months, neither of which states only a day, so they are
# recognized here before the missing year and month are shared in. "天" is a
# duration unit ("3天" = 3 days), not a day of the month, so it is excluded.
_RE_BARE_DAY = re.compile(
    r"^(?:\d{1,2}(?:日|号|號)|\d{1,2}(?:st|nd|rd|th))$",
    flags=re.IGNORECASE,
)
_RE_DIGITS = re.compile(r"\d{1,2}")


class DateRangeData:
    """The two endpoints of a parsed date range.

    :ivar start: endpoint that comes first in the text, as a
        :class:`~dateparser.date.DateData`.
    :ivar end: endpoint that comes last in the text, as a
        :class:`~dateparser.date.DateData`. Its
        :attr:`~dateparser.date.DateData.period` is the precision of the whole
        range, e.g. ``"month"`` when the phrase only names months.
    :ivar locale: shortname of the locale that parsed the endpoints, or
        ``None``.
    """

    def __init__(self, start: DateData, end: DateData, locale: str | None = None) -> None:
        self.start = start
        self.end = end
        self.locale = locale

    def __getitem__(self, key: str) -> Any:
        if not hasattr(self, key):
            raise KeyError(key)
        return getattr(self, key)

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(start={self.start!r}, end={self.end!r}, "
            f"locale={self.locale!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DateRangeData):
            return NotImplemented
        return (
            self.start.date_obj == other.start.date_obj
            and self.end.date_obj == other.end.date_obj
            and self.start.period == other.start.period
            and self.end.period == other.end.period
            and self.locale == other.locale
        )

    def __hash__(self) -> int:
        return hash(
            (
                self.start.date_obj,
                self.end.date_obj,
                self.start.period,
                self.end.period,
                self.locale,
            )
        )


def _split_on_connector(date_string: str) -> list[tuple[str, str]]:
    """Return ``(left, right)`` pairs for every range connector found in
    *date_string*; an empty list when it has none."""
    spans: list[tuple[int, int]] = []
    for pattern in (_RE_RANGE_CJK, _RE_RANGE_WORD, _RE_RANGE_DASH):
        for match in pattern.finditer(date_string):
            spans.append((match.start(), match.end()))
    spans.sort(key=lambda span: span[0])

    splits: list[tuple[str, str]] = []
    for start, end in spans:
        left = date_string[:start].strip()
        right = date_string[end:].strip()
        if left and right:
            splits.append((left, right))
    return splits


def _make_endpoint(
    parsed: DateData,
    date_obj: datetime,
    locale: str | None,
) -> DateData:
    endpoint = DateData(
        date_obj=date_obj,
        period=_precision(parsed),
        locale=locale,
        parts=tuple(part for part in _PART_ORDER if part in parsed.parts)
        + (("time",) if "time" in parsed.parts else ()),
    )
    if parsed.part_of_day is not None:
        endpoint.part_of_day = parsed.part_of_day
    return endpoint


def _fill_endpoint(
    data: DateData, other: DateData, base: datetime
) -> datetime:
    """Fill the parts *data* omits at its own precision, borrowing a value from
    *other* when it states that part, otherwise from *base*. Finer parts than
    the endpoint's own precision are never forced onto it, so a month-only
    endpoint keeps the day its parser already chose rather than another one."""
    date_obj = data.date_obj
    other_obj = other.date_obj
    assert date_obj is not None and other_obj is not None
    rank = _PRECISION_RANK[_precision(data)]

    def value(part: str) -> int:
        source: datetime = other_obj if part in other.parts else base
        return int(getattr(source, part))

    year = value("year") if rank >= _PRECISION_RANK["year"] and "year" not in data.parts else date_obj.year
    month = (
        value("month")
        if rank >= _PRECISION_RANK["month"] and "month" not in data.parts
        else date_obj.month
    )
    day = date_obj.day
    return date_obj.replace(year=year, month=month, day=day)


def _complete_endpoints(
    start: DateData,
    end: DateData,
    settings: Settings,
) -> list[DateRangeData]:
    """Complete the parts either endpoint omits with the ones the other
    endpoint gives, and return every jointly consistent range.

    The two endpoints never get incompatible readings: a part is shared only
    when it is explicit on one side and missing on the other, and only down to
    the precision of the endpoint that omits it. An inverted range is never
    swapped; it is kept only when a missing coarse part can legitimately roll
    into the next or previous period.
    """
    start_date = start.date_obj
    end_date = end.date_obj
    assert start_date is not None and end_date is not None

    base = settings.RELATIVE_BASE or _now(settings)
    locale = start.locale or end.locale

    try:
        filled_start = _fill_endpoint(start, end, base)
        filled_end = _fill_endpoint(end, start, base)
    except ValueError:
        # A shared part lands on an impossible date, e.g. a shared day 31 in
        # February: no compatible reading exists, so none is invented.
        return []

    candidates: list[DateRangeData] = []
    seen: set[tuple[datetime, datetime]] = set()

    def add(candidate_start: datetime, candidate_end: datetime) -> None:
        if candidate_start > candidate_end:
            return
        key = (candidate_start, candidate_end)
        if key in seen:
            return
        seen.add(key)
        candidates.append(
            DateRangeData(
                _make_endpoint(start, candidate_start, locale),
                _make_endpoint(end, candidate_end, locale),
                locale,
            )
        )

    # The natural reading: both endpoints share the same filled-in parts.
    add(filled_start, filled_end)

    if filled_start > filled_end:
        # The end continues into the next period, e.g. "December 30 - January
        # 3" where the end names no year. Only parts the endpoint itself omits
        # are rolled, so an explicit year or month is never reordered. The
        # closer month crossing is listed before the year crossing.
        end_rank = _PRECISION_RANK[_precision(end)]
        if end_rank >= _PRECISION_RANK["month"] and "month" not in end.parts:
            add(filled_start, filled_end + _ROLL["month"])
        if "year" not in end.parts:
            add(filled_start, filled_end + _ROLL["year"])

        # The mirror readings, where the start belongs to the previous period.
        start_rank = _PRECISION_RANK[_precision(start)]
        if start_rank >= _PRECISION_RANK["month"] and "month" not in start.parts:
            add(filled_start - _ROLL["month"], filled_end)
        if "year" not in start.parts:
            add(filled_start - _ROLL["year"], filled_end)

    return candidates


class DateRangeParser:
    """Parse strings representing a date range into a :class:`DateRangeData`.

    It accepts the same language and settings arguments as
    :class:`~dateparser.date.DateDataParser`, reusing it to parse each
    endpoint on its own before joining the endpoints into one consistent
    range.
    """

    @apply_settings
    def __init__(
        self,
        languages: Iterable[str] | None = None,
        locales: Iterable[str] | None = None,
        region: str | None = None,
        try_previous_locales: bool = False,
        use_given_order: bool = False,
        settings: Settings | dict[str, Any] | None = None,
        detect_languages_function: Callable[..., list[str]] | None = None,
    ) -> None:
        assert isinstance(settings, Settings)
        self._settings = settings
        self._parser = DateDataParser(
            languages=languages,
            locales=locales,
            region=region,
            try_previous_locales=try_previous_locales,
            use_given_order=use_given_order,
            settings=settings,
            detect_languages_function=detect_languages_function,
        )

    def _parse_endpoint(
        self, endpoint: str, date_formats: Iterable[str] | None
    ) -> DateData | None:
        bare_day = _RE_BARE_DAY.fullmatch(endpoint)
        if bare_day:
            digits = _RE_DIGITS.search(endpoint)
            assert digits is not None
            day = int(digits.group())
            base = self._settings.RELATIVE_BASE or _now(self._settings)
            if not 1 <= day <= 31:
                return None
            # January always has the day, so this placeholder stays valid while
            # its year and month are intentionally absent: they are shared from
            # the other endpoint rather than read off the base date.
            return DateData(
                date_obj=base.replace(month=1, day=day),
                period="day",
                parts=("day",),
            )
        return self._parser.get_date_data(endpoint, date_formats)

    def get_date_range_data(
        self, date_string: str, date_formats: Iterable[str] | None = None
    ) -> list[DateRangeData]:
        """Parse *date_string* into every jointly consistent date range.

        :return: the complete range candidates, most natural reading first,
            or an empty list when the text is not a range, an endpoint cannot
            be parsed, or the endpoints are inverted with no legal reading.
        """
        if not isinstance(date_string, str):
            raise TypeError("Input type must be str")

        sanitized = sanitize_date(date_string)
        candidates: list[DateRangeData] = []
        seen: set[tuple[datetime, datetime]] = set()
        for left, right in _split_on_connector(sanitized):
            start_data = self._parse_endpoint(left, date_formats)
            end_data = self._parse_endpoint(right, date_formats)
            if (
                start_data is None
                or end_data is None
                or not start_data.date_obj
                or not end_data.date_obj
            ):
                continue
            for range_data in _complete_endpoints(
                start_data, end_data, self._settings
            ):
                start_obj = range_data.start.date_obj
                end_obj = range_data.end.date_obj
                assert start_obj is not None and end_obj is not None
                key = (start_obj, end_obj)
                if key not in seen:
                    seen.add(key)
                    candidates.append(range_data)
        return candidates

    def parse(
        self, date_string: str, date_formats: Iterable[str] | None = None
    ) -> DateRangeData | None:
        """Return the first candidate of :meth:`get_date_range_data`, or
        ``None`` when there is none."""
        candidates = self.get_date_range_data(date_string, date_formats)
        return candidates[0] if candidates else None


_default_range_parser = DateRangeParser()


@apply_settings
def parse_date_range(
    date_string: str,
    date_formats: Iterable[str] | None = None,
    languages: Iterable[str] | None = None,
    locales: Iterable[str] | None = None,
    region: str | None = None,
    settings: Settings | dict[str, Any] | None = None,
    detect_languages_function: Callable[..., list[str]] | None = None,
) -> list[DateRangeData]:
    """Parse a string representing a date range.

    :param date_string: a string containing two dates joined by a range
        connector, e.g. ``"1月31日至2月2日"`` or
        ``"2024-12-30 - 2025-01-03"``.
    :return: all complete, jointly consistent ranges as
        :class:`DateRangeData` objects, or an empty list.
    """
    assert isinstance(settings, Settings)
    parser = _default_range_parser

    if (
        languages
        or locales
        or region
        or detect_languages_function
        or not settings._default
    ):
        parser = DateRangeParser(
            languages=languages,
            locales=locales,
            region=region,
            settings=settings,
            detect_languages_function=detect_languages_function,
        )

    return parser.get_date_range_data(date_string, date_formats)
