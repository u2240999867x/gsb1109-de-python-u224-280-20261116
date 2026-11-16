"""
Service-line normalization driven by the X12 DE 355 measurement qualifier.

Billing dimensions come only from the source qualifier (SV103/SV204): a count
of units (UN), elapsed minutes (MJ) or days (DA). Quantities of different
dimensions are never added or converted into one another, and nothing is
inferred from a charge amount or unit rate — a missing qualifier or an
incomplete date is reported, not reverse-engineered from price.

Pure functions over plain values; no X12 segment objects or I/O here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final, Iterable, Literal, NotRequired, TypedDict

Dimension = Literal["count", "time", "day"]
DateKind = Literal["single", "range"]

ERR_MISSING_UNIT: Final = "missing_unit"
ERR_UNKNOWN_QUALIFIER: Final = "unknown_qualifier"
ERR_UNKNOWN_TARGET_QUALIFIER: Final = "unknown_target_qualifier"
ERR_INVALID_QUANTITY: Final = "invalid_quantity"
ERR_INCOMPLETE_DATE: Final = "incomplete_date"
ERR_DATE_ORDER: Final = "date_order"
ERR_CROSS_DIMENSION: Final = "cross_dimension_conversion"

RULE_IDENTITY: Final = "identity: source qualifier retained as the billing dimension"
RULE_PRORATE: Final = "inclusive calendar-day split; remainder carried on the first day"


@dataclass(frozen=True, slots=True)
class UnitSpec:
    code: str
    dimension: Dimension
    name: str
    # Conversion is defined only between specs sharing the "time" dimension.
    # None marks a dimension that must never be converted (count, day).
    seconds: int | None


# UN/MJ/DA are the qualifiers admitted by the 837 professional (SV103) and
# institutional (SV204) service-line segments; HR is the generic DE 355 hour
# code, kept so minute<->hour conversion has an explicit, same-dimension basis.
UNIT_SPECS: Final[dict[str, UnitSpec]] = {
    "UN": UnitSpec("UN", "count", "Unit", None),
    "MJ": UnitSpec("MJ", "time", "Minute", 60),
    "HR": UnitSpec("HR", "time", "Hour", 3600),
    "DA": UnitSpec("DA", "day", "Day", None),
}

DIMENSIONS: Final[frozenset[Dimension]] = frozenset({"count", "time", "day"})


@dataclass(frozen=True, slots=True)
class ConversionBasis:
    factor: Decimal
    numerator: int
    denominator: int
    from_code: str
    to_code: str
    rule: str


@dataclass(frozen=True, slots=True)
class ProratedLine:
    source: str
    service_date: date
    day_index: int
    qualifier: str
    quantity: Decimal
    unit: str
    rule: str


@dataclass(frozen=True, slots=True)
class NormalizedLine:
    source: str
    procedure: str | None
    qualifier: str | None
    dimension: Dimension | None
    source_quantity: Decimal | None
    normalized_quantity: Decimal | None
    normalized_unit: str | None
    conversion: ConversionBasis | None
    date_kind: DateKind | None
    service_date: date | None
    end_date: date | None
    calendar_days: int | None
    daily_lines: tuple[ProratedLine, ...]
    errors: tuple[str, ...]


def get_unit_spec(qualifier: str) -> UnitSpec | None:
    return UNIT_SPECS.get(qualifier)


def convert_quantity(
    qualifier: str, target_qualifier: str, quantity: Decimal
) -> tuple[Decimal, ConversionBasis]:
    """Convert within one dimension; raise ValueError across dimensions."""
    source_spec = UNIT_SPECS.get(qualifier)
    target_spec = UNIT_SPECS.get(target_qualifier)
    if source_spec is None:
        raise ValueError(f"unknown source qualifier {qualifier!r}")
    if target_spec is None:
        raise ValueError(f"unknown target qualifier {target_qualifier!r}")
    if source_spec.dimension != target_spec.dimension:
        raise ValueError(
            f"cannot convert {source_spec.dimension} ({qualifier}) to "
            f"{target_spec.dimension} ({target_qualifier})"
        )
    if qualifier == target_qualifier:
        num = den = 1
    elif source_spec.seconds is None or target_spec.seconds is None:
        raise ValueError(f"qualifier {qualifier!r} has no convertible basis")
    else:
        num = source_spec.seconds
        den = target_spec.seconds
    factor = Decimal(num) / Decimal(den)
    normalized = quantity * num / den
    basis = ConversionBasis(
        factor=factor,
        numerator=num,
        denominator=den,
        from_code=qualifier,
        to_code=target_qualifier,
        rule=f"time-unit conversion: multiply by {num}/{den}",
    )
    return normalized, basis


def calendar_days(start: date, end: date) -> int:
    """Inclusive day count of a closed date range."""
    if end < start:
        raise ValueError("end_date precedes service_date")
    return (end - start).days + 1


def prorate_quantity(quantity: Decimal, days: int) -> list[Decimal]:
    """Split a quantity across ``days`` inclusive calendar days.

    The first day carries the remainder so the pieces always sum back to the
    original quantity exactly.
    """
    if days < 1:
        raise ValueError("days must be >= 1")
    base = quantity // days
    remainder = quantity - base * days
    return [base + remainder] + [base] * (days - 1)


def normalize_service_line(
    source: str,
    *,
    qualifier: str | None,
    quantity: Decimal | int | str | None,
    service_date: date | str | None = None,
    end_date: date | str | None = None,
    procedure: str | None = None,
    target_qualifier: str | None = None,
    prorate: bool = True,
) -> NormalizedLine:
    errors: list[str] = []

    unit_code = _clean_code(qualifier)
    if unit_code is None:
        errors.append(ERR_MISSING_UNIT)
    spec = UNIT_SPECS.get(unit_code) if unit_code is not None else None
    if unit_code is not None and spec is None:
        errors.append(ERR_UNKNOWN_QUALIFIER)

    parsed_quantity = _parse_quantity(quantity)
    if parsed_quantity is None:
        errors.append(ERR_INVALID_QUANTITY)

    start = _parse_date(service_date)
    finish = _parse_date(end_date)
    if (
        (service_date is not None and start is None)
        or (end_date is not None and finish is None)
        or (finish is not None and start is None)
    ):
        errors.append(ERR_INCOMPLETE_DATE)
    if start is not None and finish is not None and finish < start:
        errors.append(ERR_DATE_ORDER)

    if start is not None and finish is not None:
        date_kind: DateKind | None = "range"
        day_count = calendar_days(start, finish) if finish >= start else None
    elif start is not None:
        date_kind = "single"
        day_count = 1
    else:
        date_kind = None
        day_count = None

    normalized: Decimal | None = None
    basis: ConversionBasis | None = None
    normalized_code = unit_code
    if not errors:
        assert unit_code is not None and spec is not None and parsed_quantity is not None
        if target_qualifier is not None:
            target_code = _clean_code(target_qualifier)
            if target_code is None or target_code not in UNIT_SPECS:
                errors.append(ERR_UNKNOWN_TARGET_QUALIFIER)
            elif UNIT_SPECS[target_code].dimension != spec.dimension:
                errors.append(ERR_CROSS_DIMENSION)
            else:
                normalized, basis = convert_quantity(
                    unit_code, target_code, parsed_quantity
                )
                normalized_code = target_code
        if normalized is None and not errors:
            normalized = parsed_quantity
            basis = ConversionBasis(
                factor=Decimal(1),
                numerator=1,
                denominator=1,
                from_code=unit_code,
                to_code=unit_code,
                rule=RULE_IDENTITY,
            )

    daily: list[ProratedLine] = []
    if (
        prorate
        and not errors
        and start is not None
        and day_count is not None
        and normalized is not None
        and normalized_code is not None
    ):
        normalized_spec = UNIT_SPECS[normalized_code]
        pieces = prorate_quantity(normalized, day_count)
        for index, piece in enumerate(pieces):
            daily.append(
                ProratedLine(
                    source=source,
                    service_date=date.fromordinal(start.toordinal() + index),
                    day_index=index + 1,
                    qualifier=normalized_code,
                    quantity=piece,
                    unit=normalized_spec.name,
                    rule=RULE_PRORATE if day_count > 1 else RULE_IDENTITY,
                )
            )

    return NormalizedLine(
        source=source,
        procedure=procedure,
        qualifier=unit_code,
        dimension=spec.dimension if spec is not None else None,
        source_quantity=parsed_quantity,
        normalized_quantity=normalized,
        normalized_unit=normalized_code,
        conversion=basis,
        date_kind=date_kind,
        service_date=start,
        end_date=finish,
        calendar_days=day_count,
        daily_lines=tuple(daily),
        errors=tuple(errors),
    )


class ServiceLineInput(TypedDict):
    source: str
    qualifier: str | None
    quantity: Decimal | int | str | None
    service_date: NotRequired[date | str | None]
    end_date: NotRequired[date | str | None]
    procedure: NotRequired[str | None]
    target_qualifier: NotRequired[str | None]
    prorate: NotRequired[bool]


def normalize_service_lines(lines: Iterable[ServiceLineInput]) -> list[NormalizedLine]:
    return [normalize_service_line(**line) for line in lines]


def _clean_code(value: str | None) -> str | None:
    if value is None:
        return None
    code = value.strip().upper()
    return code or None


def _parse_quantity(value: Decimal | int | str | None) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return parsed


def _parse_date(value: date | str | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    text = value.strip()
    if len(text) != 8 or not text.isdigit():
        return None
    try:
        return date(int(text[0:4]), int(text[4:6]), int(text[6:8]))
    except ValueError:
        return None


__all__ = [
    "DIMENSIONS",
    "RULE_IDENTITY",
    "RULE_PRORATE",
    "ServiceLineInput",
    "UNIT_SPECS",
    "ConversionBasis",
    "NormalizedLine",
    "ProratedLine",
    "UnitSpec",
    "calendar_days",
    "convert_quantity",
    "get_unit_spec",
    "normalize_service_line",
    "normalize_service_lines",
    "prorate_quantity",
]
