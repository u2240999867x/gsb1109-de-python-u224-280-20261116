"""Parse a batch of dates that share one language and date-order convention.

A batch may come from a supplier that guarantees every date in it is written
in the same language and follows the same date order, but does not say which
language that is.  The batch is described by:

* the languages that are allowed,
* rows whose date is already known (verified rows), and
* texts whose date is not known yet.

Every ``(language, date order)`` combination is a convention.  A convention
explains the batch when it reads every verified row as its known date.  The
conventions that explain the batch are its explanations; a row is given a
definite date only when every explanation reads it as the same date.  Rows
that the explanations read differently keep their candidates together with
the conventions that support them, instead of settling on one date because of
where the row happens to appear.

Date order is treated as a property of the convention rather than of the
batch file.  In particular, an ambiguous numeric date such as ``05/03/2021``
does not make ``DMY`` the convention merely because it appears first; both
``DMY`` and ``MDY`` remain possible until the verified rows rule one out.

When the verified rows have no common explanation, the rows that have to be
left out to make the rest explainable are reported as conflicts.  No language
is switched per row to manufacture an explanation.

Batches with an explicit language configuration (``languages=...``) are not
inferred: their languages, and the date order their configuration gives them,
are used as they are.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from datetime import timezone as dt_timezone
from functools import cache
from importlib import import_module
from itertools import combinations
from typing import Any, TypeAlias

from dateparser.conf import Settings, check_settings
from dateparser.data.languages_info import language_order
from dateparser.date import DateDataParser
from dateparser.parser import date_order_chart
from dateparser.utils import _now

DateLike: TypeAlias = datetime | date | str

_ORDER_RANK = {order: position for position, order in enumerate(date_order_chart)}
_DEFAULT_ORDERS = tuple(date_order_chart)


@dataclass(frozen=True)
class Convention:
    """A language together with the date order it uses in a batch."""

    language: str
    date_order: str

    def __str__(self) -> str:
        return f"{self.language}:{self.date_order}"


@dataclass(frozen=True)
class Candidate:
    """A date for a row and the conventions that read the row that way."""

    date: datetime
    conventions: tuple[Convention, ...]

    @property
    def languages(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(convention.language for convention in self.conventions)
        )

    def __getitem__(self, key: str) -> Any:
        if not hasattr(self, key):
            raise KeyError(key)
        return getattr(self, key)


@dataclass(frozen=True)
class BatchRow:
    """The result of one text in the batch."""

    text: str
    date: datetime | None
    candidates: tuple[Candidate, ...] = ()
    verified: bool = False
    expected: datetime | None = None
    matches_expected: bool | None = None

    @property
    def ambiguous(self) -> bool:
        """Whether the explanations read this row as more than one date."""
        return len(self.candidates) > 1

    @property
    def languages(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                language
                for candidate in self.candidates
                for language in candidate.languages
            )
        )

    def __getitem__(self, key: str) -> Any:
        if not hasattr(self, key):
            raise KeyError(key)
        return getattr(self, key)


@dataclass(frozen=True)
class Conflict:
    """A verified row that cannot be explained together with the others."""

    index: int
    text: str
    expected: datetime
    supported: tuple[Convention, ...]
    reason: str

    def __getitem__(self, key: str) -> Any:
        if not hasattr(self, key):
            raise KeyError(key)
        return getattr(self, key)


@dataclass(frozen=True)
class VerifiedRow:
    """A row whose date is already known, used to explain the batch."""

    text: str
    date: DateLike

    @property
    def expected(self) -> DateLike:
        return self.date


@dataclass(frozen=True)
class BatchResult:
    """The explanations for a batch and the result of each row in it."""

    inferred: bool
    conventions: tuple[Convention, ...]
    rows: tuple[BatchRow, ...]
    verified_rows: tuple[BatchRow, ...] = ()
    conflicts: tuple[Conflict, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether a common explanation was found (or the batch is explicit)."""
        return bool(self.conventions) and not self.conflicts

    @property
    def languages(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(convention.language for convention in self.conventions)
        )

    @property
    def combinations(self) -> tuple[Convention, ...]:
        return self.conventions

    def __getitem__(self, key: str) -> Any:
        if not hasattr(self, key):
            raise KeyError(key)
        return getattr(self, key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "inferred": self.inferred,
            "conventions": [
                {"language": c.language, "date_order": c.date_order}
                for c in self.conventions
            ],
            "rows": [
                {
                    "text": row.text,
                    "date": row.date,
                    "ambiguous": row.ambiguous,
                    "candidates": [
                        {
                            "date": candidate.date,
                            "conventions": [
                                {
                                    "language": c.language,
                                    "date_order": c.date_order,
                                }
                                for c in candidate.conventions
                            ],
                        }
                        for candidate in row.candidates
                    ],
                }
                for row in self.rows
            ],
            "conflicts": [
                {
                    "index": conflict.index,
                    "text": conflict.text,
                    "expected": conflict.expected,
                    "supported": [
                        {"language": c.language, "date_order": c.date_order}
                        for c in conflict.supported
                    ],
                }
                for conflict in self.conflicts
            ],
        }


"""A verified row as accepted by :meth:`BatchParser.parse` and
:func:`parse_batch`."""
VerifiedItem: TypeAlias = tuple[str, DateLike] | Mapping[str, Any] | VerifiedRow


def _check_languages(value: object, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Iterable):
        raise TypeError(f"{name} must be an iterable of language codes, not {value!r}")
    languages: tuple[Any, ...] = tuple(value)
    if not languages:
        raise ValueError(f"{name} must not be empty")
    if any(not isinstance(language, str) for language in languages):
        raise TypeError(f"{name} must contain only strings")
    return tuple(dict.fromkeys(languages))


def _check_orders(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str) or not isinstance(value, Iterable):
        raise TypeError("date_orders must be an iterable of date order strings")
    orders: tuple[Any, ...] = tuple(value)
    if not orders:
        raise ValueError("date_orders must not be empty")
    unknown = [order for order in orders if order not in date_order_chart]
    if unknown:
        raise ValueError(
            "Unknown date order(s): "
            + ", ".join(map(repr, unknown))
            + ". Valid orders are: "
            + ", ".join(_DEFAULT_ORDERS)
        )
    return tuple(dict.fromkeys(orders))


def _prepare_settings(
    settings: Settings | Mapping[str, Any] | None,
) -> Settings:
    if settings is None:
        from dateparser.conf import settings as default_settings  # noqa: PLC0415

        return default_settings.replace()
    if isinstance(settings, Settings):
        check_settings(settings)
        return settings.replace()
    if isinstance(settings, Mapping):
        from dateparser.conf import settings as default_settings  # noqa: PLC0415

        prepared = default_settings.replace(
            mod_settings=dict(settings), **dict(settings)
        )
        check_settings(prepared)
        return prepared
    raise TypeError(
        "settings can only be either a dict or an instance of Settings class"
    )


@cache
def _language_info(language: str) -> Mapping[str, Any]:
    try:
        module = import_module(f"dateparser.data.date_translation_data.{language}")
    except ModuleNotFoundError as error:
        raise ValueError(f"Unknown language(s): {language!r}") from error
    return module.info  # type: ignore[no-any-return]


def _declared_orders(language: str) -> tuple[str, ...]:
    """The date order that the language data of *language* declares for the
    language itself.

    A language can declare a primary order here and other orders on some of
    its regional variants, e.g. English is month first while ``en-GB`` is day
    first.  Those variants are different locales, not the language's own
    convention, so they are not inferred; pass ``date_orders`` to consider
    them."""
    order = _language_info(language).get("date_order")
    if order in date_order_chart:
        return (str(order),)
    return ("MDY",)


def _base_order(language: str) -> str:
    return _declared_orders(language)[0]


def _convention_key(convention: Convention) -> tuple[str, int, str]:
    return (convention.language, _ORDER_RANK[convention.date_order], convention.date_order)


def _comparable(moment: datetime) -> datetime:
    if moment.tzinfo is not None:
        return moment.astimezone(dt_timezone.utc).replace(tzinfo=None)
    return moment


def _coerce_expected(value: DateLike) -> tuple[datetime, bool]:
    """Return the expected moment and whether only its date is significant."""
    if isinstance(value, datetime):
        return value, False
    if isinstance(value, date):
        return datetime.combine(value, time.min), True
    if isinstance(value, str):
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as error:
            raise ValueError(
                f"Expected date {value!r} is not an ISO date or datetime"
            ) from error
        date_only = "T" not in text and " " not in text and ":" not in text
        return parsed, date_only
    raise TypeError(
        "Expected dates must be datetime, date or ISO date/datetime strings, "
        f"not {type(value).__name__}"
    )


def _matches(
    actual: datetime | None, expected: datetime, date_only: bool
) -> bool:
    if actual is None:
        return False
    actual_moment = _comparable(actual)
    expected_moment = _comparable(expected)
    if date_only:
        return actual_moment.date() == expected_moment.date()
    return actual_moment == expected_moment


def _normalise_verified(
    verified: Iterable[VerifiedItem] | Mapping[str, DateLike] | None,
) -> tuple[VerifiedRow, ...]:
    if verified is None:
        return ()
    items: Iterable[VerifiedItem]
    if isinstance(verified, Mapping):
        items = [(text, expected) for text, expected in verified.items()]
    else:
        if isinstance(verified, (str, bytes)) or not isinstance(verified, Iterable):
            raise TypeError("verified rows must be an iterable or a mapping")
        items = verified

    rows: list[VerifiedRow] = []
    for item in items:
        if isinstance(item, VerifiedRow):
            rows.append(item)
        elif isinstance(item, Mapping):
            text = item.get("text", item.get("date_string"))
            expected = item.get("date", item.get("expected"))
            if text is None or expected is None:
                raise ValueError(
                    "A verified row mapping needs a 'text' and a 'date'/'expected'"
                )
            rows.append(VerifiedRow(text=str(text), date=expected))
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            text, expected = item
            rows.append(VerifiedRow(text=str(text), date=expected))
        else:
            raise TypeError(
                "A verified row must be (text, date), a mapping or VerifiedRow, "
                f"not {item!r}"
            )
    return tuple(rows)


def _check_texts(texts: object) -> tuple[str, ...]:
    if texts is None:
        return ()
    if isinstance(texts, str) or not isinstance(texts, Iterable):
        raise TypeError("texts must be an iterable of strings")
    result: tuple[Any, ...] = tuple(texts)
    if any(not isinstance(text, str) for text in result):
        raise TypeError("texts must contain only strings")
    return result


def _pinned_settings(
    base: Settings,
    order: str,
    relative_base: datetime,
    *,
    strict: bool,
) -> Settings:
    modifications = dict(base._mod_settings)
    modifications.update(
        {
            "DATE_ORDER": order,
            "PREFER_LOCALE_DATE_ORDER": False,
            "DEFAULT_LANGUAGES": [],
            "RELATIVE_BASE": relative_base,
            "USE_GIVEN_LANGUAGE_ORDER": True,
        }
    )
    if strict:
        # Each convention is a hypothesis: a row either fits it or does not.
        # Without this, DATE_ORDER would be a preference that reads a row in
        # another order, which would make incompatible conventions look equal.
        modifications["STRICT_DATE_ORDER"] = "all"
    return base.replace(mod_settings=modifications, **modifications)


class _RowParser:
    """Reads texts under conventions, sharing settings and parsers per order."""

    def __init__(
        self, settings: Settings, relative_base: datetime, *, strict: bool = True
    ) -> None:
        self._settings = settings
        self._relative_base = relative_base
        self._strict = strict
        self._parsers: dict[tuple[str, str], DateDataParser] = {}
        self._cache: dict[tuple[str, str, str], datetime | None] = {}
        self._settings_by_order: dict[str, Settings] = {}

    def _settings_for(self, order: str) -> Settings:
        if order not in self._settings_by_order:
            self._settings_by_order[order] = _pinned_settings(
                self._settings, order, self._relative_base, strict=self._strict
            )
        return self._settings_by_order[order]

    def _parser(self, convention: Convention) -> DateDataParser:
        key = (convention.language, convention.date_order)
        if key not in self._parsers:
            self._parsers[key] = DateDataParser(
                languages=[convention.language],
                settings=self._settings_for(convention.date_order),
            )
        return self._parsers[key]

    def parse(self, convention: Convention, text: str) -> datetime | None:
        key = (convention.language, convention.date_order, text)
        if key not in self._cache:
            try:
                self._cache[key] = self._parser(convention).get_date_data(
                    text
                ).date_obj
            except (TypeError, ValueError, OverflowError):
                self._cache[key] = None
        return self._cache[key]

    def candidates(
        self, text: str, conventions: Iterable[Convention]
    ) -> BatchRow:
        groups: dict[datetime, list[tuple[Convention, datetime]]] = {}
        for convention in conventions:
            parsed = self.parse(convention, text)
            if parsed is None:
                continue
            groups.setdefault(_comparable(parsed), []).append((convention, parsed))

        candidates: list[Candidate] = []
        for moment in sorted(groups):
            entries = sorted(
                groups[moment],
                key=lambda entry: (
                    _convention_key(entry[0]),
                    entry[1].isoformat(),
                ),
            )
            actual = min((entry[1] for entry in entries), key=lambda d: d.isoformat())
            supporting = tuple(
                sorted(
                    {convention for convention, _ in entries},
                    key=_convention_key,
                )
            )
            candidates.append(Candidate(date=actual, conventions=supporting))

        # Candidates are ordered by date, not by the order the conventions were
        # tried in, so the file order cannot decide which reading comes first.
        candidates.sort(key=lambda candidate: candidate.date.isoformat())
        if len(candidates) == 1:
            return BatchRow(
                text=text, date=candidates[0].date, candidates=tuple(candidates)
            )
        return BatchRow(text=text, date=None, candidates=tuple(candidates))

    def first(
        self, text: str, conventions: Iterable[Convention]
    ) -> tuple[BatchRow, Convention | None]:
        for convention in conventions:
            parsed = self.parse(convention, text)
            if parsed is not None:
                return (
                    BatchRow(
                        text=text,
                        date=parsed,
                        candidates=(
                            Candidate(date=parsed, conventions=(convention,)),
                        ),
                    ),
                    convention,
                )
        return BatchRow(text=text, date=None, candidates=()), None


def _supports(
    parser: _RowParser,
    rows: Iterable[VerifiedRow],
    conventions: Iterable[Convention],
) -> dict[int, set[Convention]]:
    conventions = tuple(conventions)
    support: dict[int, set[Convention]] = {}
    for index, row in enumerate(rows):
        expected, date_only = _coerce_expected(row.date)
        support[index] = {
            convention
            for convention in conventions
            if _matches(parser.parse(convention, row.text), expected, date_only)
        }
    return support


def _conflicting_indices(
    rows: tuple[VerifiedRow, ...],
    support: Mapping[int, set[Convention]],
    conventions: tuple[Convention, ...],
) -> list[int]:
    """Return the smallest set of rows that has to be left out for the other
    verified rows to share a convention.

    The search is exhaustive over the conventions, smallest removals first,
    and candidate rows are tried in an order derived from the row itself.
    Neither the result nor the choice between equally small sets depends on
    the position of the rows in the input."""
    row_keys = {
        index: (rows[index].text, _coerce_expected(rows[index].date)[0].isoformat())
        for index in range(len(rows))
    }
    indices = tuple(sorted(range(len(rows)), key=row_keys.__getitem__))

    def supported_by(convention: Convention, index: int) -> bool:
        return convention in support[index]

    for convention in conventions:
        if all(supported_by(convention, index) for index in indices):
            return []

    # No convention explains every row.  Find the smallest set of rows whose
    # removal leaves at least one convention explaining the rest, and report
    # every row that takes part in any such minimal set.  Reporting one of two
    # equally valid alternatives would make the result depend on which row
    # happened to be picked, not on the data.
    for size in range(1, len(indices) + 1):
        conflicting: set[int] = set()
        for removed in combinations(indices, size):
            remaining = [index for index in indices if index not in removed]
            if not remaining:
                continue
            if any(
                all(supported_by(convention, index) for index in remaining)
                for convention in conventions
            ):
                conflicting.update(removed)
        if conflicting:
            return sorted(conflicting)
    return list(indices)


def _diagnostic_row(
    parser: _RowParser,
    row: VerifiedRow,
    conventions: Iterable[Convention],
) -> BatchRow:
    expected, date_only = _coerce_expected(row.date)
    parsed = parser.candidates(row.text, conventions)
    supporting = tuple(
        candidate
        for candidate in parsed.candidates
        if _matches(candidate.date, expected, date_only)
    )
    return BatchRow(
        text=row.text,
        date=expected if supporting else None,
        candidates=parsed.candidates,
        verified=True,
        expected=expected,
        matches_expected=bool(supporting),
    )


class BatchParser:
    """Parse batches of dates that share a language and date-order convention.

    :param allowed_languages:
        Language codes to infer the batch convention from.  A batch either
        uses ``allowed_languages`` (inference) or ``languages`` (an explicit
        configuration), not both.

    :param languages:
        Explicit language codes for a batch whose language is already known.
        The batch is then read with those languages and the date order their
        configuration gives them, without inference.

    :param date_orders:
        Date orders to consider instead of the ones declared by the language
        data, e.g. ``("DMY", "MDY", "YMD")``.  This is useful for testing a
        convention that is not in the language data.

    :param settings:
        Settings for parsing, as for :class:`~dateparser.date.DateDataParser`.
        They are copied, so a batch cannot change them for another batch.
    """

    def __init__(
        self,
        allowed_languages: Iterable[str] | None = None,
        *,
        languages: Iterable[str] | None = None,
        date_orders: Iterable[str] | None = None,
        settings: Settings | Mapping[str, Any] | None = None,
    ) -> None:
        if allowed_languages is not None and languages is not None:
            raise ValueError(
                "Use allowed_languages to infer the batch language, or "
                "languages to configure it explicitly, not both."
            )
        if allowed_languages is None and languages is None:
            raise ValueError("Either allowed_languages or languages must be given")

        self.inferred = languages is None
        if self.inferred:
            self.allowed_languages = _check_languages(
                allowed_languages, "allowed_languages"
            )
            self.languages: tuple[str, ...] = ()
        else:
            self.allowed_languages = ()
            self.languages = _check_languages(languages, "languages")
        self.date_orders = _check_orders(date_orders)
        self._settings = _prepare_settings(settings)

        # Fail before the first row when a language is unknown.
        for language in self.allowed_languages or self.languages:
            _language_info(language)

    def parse(
        self,
        texts: Iterable[str] | None = None,
        verified: Iterable[VerifiedItem] | Mapping[str, DateLike] | None = None,
    ) -> BatchResult:
        unverified = _check_texts(texts)
        verified_rows = _normalise_verified(verified)
        relative_base = self._settings.RELATIVE_BASE or _now(self._settings)
        parser = _RowParser(self._settings, relative_base, strict=self.inferred)
        if self.inferred:
            return self._parse_inferred(parser, unverified, verified_rows)
        return self._parse_explicit(parser, unverified, verified_rows)

    # -- inference ---------------------------------------------------------

    def _conventions(self) -> tuple[Convention, ...]:
        conventions: list[Convention] = []
        for language in sorted(self.allowed_languages):
            orders = self.date_orders or _declared_orders(language)
            conventions.extend(
                Convention(language, order) for order in orders
            )
        return tuple(sorted(set(conventions), key=_convention_key))

    def _parse_inferred(
        self,
        parser: _RowParser,
        texts: tuple[str, ...],
        verified_rows: tuple[VerifiedRow, ...],
    ) -> BatchResult:
        conventions = self._conventions()
        support = _supports(parser, verified_rows, conventions)

        if not verified_rows:
            explanations = set(conventions)
            conflicts: tuple[Conflict, ...] = ()
        else:
            common = set(conventions)
            for supported in support.values():
                common.intersection_update(supported)
            if common:
                explanations = common
                conflicts = ()
            else:
                explanations = set()
                conflicting = _conflicting_indices(
                    verified_rows, support, conventions
                )
                conflicts = tuple(
                    sorted(
                        (
                            Conflict(
                                index=index,
                                text=verified_rows[index].text,
                                expected=_coerce_expected(
                                    verified_rows[index].date
                                )[0],
                                supported=tuple(
                                    sorted(support[index], key=_convention_key)
                                ),
                                reason=(
                                    "No single language/date-order convention "
                                    "explains this row together with the other "
                                    "verified rows."
                                ),
                            )
                            for index in conflicting
                        ),
                        key=lambda conflict: (
                            conflict.text,
                            conflict.expected.isoformat(),
                            conflict.index,
                        ),
                    )
                )
                # The rows that remain explainable still narrow the batch, so
                # the unverified rows keep the candidates they support.
                remaining = [
                    index
                    for index in range(len(verified_rows))
                    if index not in set(conflicting)
                ]
                if remaining:
                    explanations = set(conventions)
                    for index in remaining:
                        explanations.intersection_update(support[index])

        explanations = {
            convention
            for convention in explanations
            if any(
                parser.parse(convention, row.text) is not None
                for row in verified_rows
            )
        } or explanations
        ordered_explanations = tuple(sorted(explanations, key=_convention_key))

        if conflicts:
            # No convention explains the batch as a whole, so no unverified row
            # has a definite date.  ``conventions`` lists the ones that explain
            # the rows left after removing the conflicts, which are also the
            # only ones the candidates below can refer to.
            rows = tuple(
                replace(
                    parser.candidates(text, ordered_explanations), date=None
                )
                for text in texts
            )
            return BatchResult(
                inferred=True,
                conventions=ordered_explanations,
                rows=rows,
                verified_rows=tuple(
                    _diagnostic_row(parser, row, conventions)
                    for row in verified_rows
                ),
                conflicts=conflicts,
            )

        rows = tuple(
            parser.candidates(text, ordered_explanations) for text in texts
        )
        explained_rows = tuple(
            BatchRow(
                text=row.text,
                date=_coerce_expected(row.date)[0],
                candidates=parser.candidates(
                    row.text, ordered_explanations
                ).candidates,
                verified=True,
                expected=_coerce_expected(row.date)[0],
                matches_expected=True,
            )
            for row in verified_rows
        )
        return BatchResult(
            inferred=True,
            conventions=ordered_explanations,
            rows=rows,
            verified_rows=explained_rows,
        )

    # -- explicit configuration -------------------------------------------

    def _ordered_languages(self) -> tuple[str, ...]:
        languages = list(self.languages)
        if not self._settings.USE_GIVEN_LANGUAGE_ORDER:
            positions = {language: i for i, language in enumerate(language_order)}
            languages.sort(key=lambda language: positions.get(language, len(positions)))
        return tuple(languages)

    def _explicit_conventions(self) -> tuple[Convention, ...]:
        conventions: list[Convention] = []
        for language in self._ordered_languages():
            if self.date_orders is not None:
                orders = self.date_orders
            elif (
                self._settings.PREFER_LOCALE_DATE_ORDER
                and "DATE_ORDER" not in self._settings._mod_settings
            ):
                orders = (_base_order(language),)
            else:
                orders = (self._settings.DATE_ORDER,)
            conventions.extend(Convention(language, order) for order in orders)
        return tuple(conventions)

    def _parse_explicit(
        self,
        parser: _RowParser,
        texts: tuple[str, ...],
        verified_rows: tuple[VerifiedRow, ...],
    ) -> BatchResult:
        conventions = self._explicit_conventions()
        rows = tuple(parser.first(text, conventions)[0] for text in texts)

        verified_results: list[BatchRow] = []
        conflicts: list[Conflict] = []
        for index, row in enumerate(verified_rows):
            result, _chosen = parser.first(row.text, conventions)
            expected, date_only = _coerce_expected(row.date)
            supported = tuple(
                convention
                for convention in conventions
                if _matches(parser.parse(convention, row.text), expected, date_only)
            )
            matches = _matches(result.date, expected, date_only)
            verified_results.append(
                BatchRow(
                    text=row.text,
                    date=result.date,
                    candidates=result.candidates,
                    verified=True,
                    expected=expected,
                    matches_expected=matches,
                )
            )
            if not matches:
                conflicts.append(
                    Conflict(
                        index=index,
                        text=row.text,
                        expected=expected,
                        supported=supported,
                        reason=(
                            "The configured language/date order does not read "
                            "this verified row as its expected date."
                        ),
                    )
                )

        return BatchResult(
            inferred=False,
            conventions=conventions,
            rows=rows,
            verified_rows=tuple(verified_results),
            conflicts=tuple(conflicts),
        )


def parse_batch(
    texts: Iterable[str] | None = None,
    allowed_languages: Iterable[str] | None = None,
    verified: Iterable[VerifiedItem] | Mapping[str, DateLike] | None = None,
    *,
    unverified_texts: Iterable[str] | None = None,
    verified_rows: Iterable[VerifiedItem] | Mapping[str, DateLike] | None = None,
    languages: Iterable[str] | None = None,
    date_orders: Iterable[str] | None = None,
    settings: Settings | Mapping[str, Any] | None = None,
) -> BatchResult:
    """Parse a batch of dates that share a language and date-order convention.

    :param texts:
        Texts whose dates are not known yet (the unverified rows).

    :param unverified_texts:
        Alias for *texts*.

    :param allowed_languages:
        The languages the batch may be written in.  The convention is the
        ``(language, date order)`` pair that reads every verified row as its
        known date.

    :param verified:
        Rows whose dates are known, as ``(text, date)`` pairs, mappings with
        ``text`` and ``date``/``expected`` keys, or :class:`VerifiedRow`
        objects.  A mapping of text to date is accepted too.

    :param verified_rows:
        Alias for *verified*.

    :param languages:
        Explicit language configuration.  A batch with explicit languages is
        not inferred: it is read with those languages and their configured
        date order, and the languages are never widened to make a row fit.

    :param date_orders:
        Date orders to try instead of the ones declared by the language data.

    :param settings:
        Parsing settings, copied for the batch.

    :return: A :class:`BatchResult` with the conventions that explain the
        batch, one row per unverified text, and any conflicting verified rows.
    """
    if texts is not None and unverified_texts is not None:
        raise ValueError("Use either texts or unverified_texts, not both")
    if verified is not None and verified_rows is not None:
        raise ValueError("Use either verified or verified_rows, not both")
    if texts is None:
        texts = unverified_texts
    if verified is None:
        verified = verified_rows
    parser = BatchParser(
        allowed_languages,
        languages=languages,
        date_orders=date_orders,
        settings=settings,
    )
    return parser.parse(texts, verified)
