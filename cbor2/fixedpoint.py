"""
Plan a common, lossless fixed-point representation for a column of CBOR numbers.

A warehouse column can only use one fixed-point scale.  Given the allowed
bases, the allowed scale range and the candidate signed bit widths, this
module analyses CBOR integers (major types 0/1 and tags 2/3), decimal
fractions (tag 4), binary bigfloats (tag 5) and rationals (tag 30) and either

* picks a common ``(base, scale, bit_width)`` plan - the smallest usable bit
  width first, ties broken by the order the bases and scales were given in -
  together with the integer mantissa of every original value, or
* reports the blocking values with verifiable reasons, without ever rounding
  a value to make it fit.

Illegal denominators (e.g. a zero denominator in a rational) and values that
overflow every candidate bit width are reported as separate categories.
Values with extreme exponents are never expanded into giant integers: the
original type, exponent and numerator/denominator are preserved throughout
and all decisions are made with exponent arithmetic.
"""

from __future__ import annotations

import math
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from fractions import Fraction
from math import gcd
from typing import IO, Any, NamedTuple, cast

from ._decoder import CBORDecoder, major_decoders, semantic_decoders
from ._types import CBORDecodeValueError, CBORTag

# The C extension (loaded by the cbor2 package initializer, which always runs
# before this submodule is imported) provides its own CBORTag class
_c_cbortag = getattr(sys.modules.get("_cbor2"), "CBORTag", None)
_CBOR_TAG_TYPES: tuple[type, ...] = (CBORTag,) if _c_cbortag is None else (CBORTag, _c_cbortag)

__all__ = [
    "BigFloat",
    "Blocker",
    "BaseFailure",
    "ColumnEntry",
    "ColumnReport",
    "DecimalFraction",
    "ExactDecoder",
    "FixedPointPlan",
    "NoCommonRepresentationError",
    "RawRational",
    "load_exact",
    "loads_exact",
    "plan_fixed_point_column",
]

#: Blocker category: a rational with an illegal (zero or non-integer) denominator
ILLEGAL_DENOMINATOR = "illegal_denominator"
#: Blocker category: a non-finite decimal (NaN or infinity)
NON_FINITE = "non_finite"
#: Blocker category: no allowed base/scale represents the value without rounding
NO_COMMON_SCALE = "no_common_scale"
#: Blocker category: the mantissa overflows every candidate bit width
OUT_OF_RANGE = "out_of_range"

_KIND_INTEGER = "integer"
_KIND_DECIMAL = "decimal_fraction"
_KIND_BIGFLOAT = "bigfloat"
_KIND_RATIONAL = "rational"

# Safety margin (in bits) around the float estimate of a mantissa bit length.
# Estimates inside the margin are resolved by computing the mantissa exactly,
# which is then guaranteed to be small.
_MARGIN = 3.0


class DecimalFraction(NamedTuple):
    """
    Exact value of a CBOR decimal fraction (tag 4): ``mantissa * 10**exponent``.

    Unlike :class:`decimal.Decimal`, this preserves arbitrarily large
    exponents without expanding them into giant integers.
    """

    mantissa: int
    exponent: int


class BigFloat(NamedTuple):
    """
    Exact value of a CBOR bigfloat (tag 5): ``mantissa * 2**exponent``.

    Unlike the default decoder (which multiplies the value out as a rounded
    :class:`decimal.Decimal`), this preserves the mantissa and the binary
    exponent exactly, however large the exponent is.
    """

    mantissa: int
    exponent: int


class RawRational(NamedTuple):
    """
    Payload of a CBOR rational (tag 30) preserved even when illegal.

    :class:`fractions.Fraction` cannot be constructed with a zero or
    non-integer denominator; this type carries such payloads to the analyzer
    so they can be reported in the ``illegal_denominator`` category instead of
    crashing the decode.
    """

    numerator: Any
    denominator: Any


@dataclass(frozen=True)
class BaseFailure:
    """Why one value cannot be represented in one particular base."""

    base: int
    reason: str


@dataclass(frozen=True)
class Blocker:
    """
    One value that prevents the column from being stored losslessly.

    ``category`` is one of ``illegal_denominator``, ``non_finite``,
    ``no_common_scale`` or ``out_of_range``; illegal denominators and
    out-of-range values are always reported separately.
    """

    index: int
    kind: str
    value: Any
    category: str
    reason: str
    base_failures: tuple[BaseFailure, ...] = ()


@dataclass(frozen=True)
class ColumnEntry:
    """
    Mapping between one original value and its fixed-point mantissa.

    The original type, exponent (decimals/bigfloats) and
    numerator/denominator (rationals) are preserved alongside the mantissa.
    """

    index: int
    kind: str
    original: Any
    mantissa: int
    exponent: int | None = None
    numerator: int | None = None
    denominator: int | None = None


@dataclass(frozen=True)
class FixedPointPlan:
    """
    A common lossless representation: ``value == mantissa * base**-scale``
    with the mantissa stored as a signed integer of ``bit_width`` bits.
    """

    base: int
    scale: int
    bit_width: int
    entries: tuple[ColumnEntry, ...]

    @property
    def signed(self) -> bool:
        return True

    def mantissas(self) -> list[int]:
        return [entry.mantissa for entry in self.entries]

    def value_of(self, entry: ColumnEntry) -> Fraction:
        """
        Reconstruct the exact value of an entry from its mantissa.

        .. note:: this materializes ``base**scale``; for extreme scales the
            preserved ``original`` should be consulted instead.
        """
        if self.scale >= 0:
            return Fraction(entry.mantissa, self.base**self.scale)
        return Fraction(entry.mantissa * self.base**-self.scale)

    def verify(self) -> bool:
        """Check that every entry's mantissa still matches its original value."""
        primes = _factor_base(self.base)
        for entry in self.entries:
            exact = _normalize(entry.original, entry.index)
            if isinstance(exact, Blocker):
                return False
            fit = _fit_for_base(exact, self.base, primes)
            if not fit.ok:
                return False
            if _exact_mantissa(fit, primes, self.scale) != entry.mantissa:
                return False
        return True


@dataclass(frozen=True)
class ColumnReport:
    """Result of :func:`plan_fixed_point_column`: a plan, or the blockers."""

    plan: FixedPointPlan | None
    blockers: tuple[Blocker, ...] = ()

    @property
    def ok(self) -> bool:
        return self.plan is not None

    def unwrap(self) -> FixedPointPlan:
        """Return the plan, or raise :exc:`NoCommonRepresentationError`."""
        if self.plan is None:
            raise NoCommonRepresentationError(self.blockers)
        return self.plan


class NoCommonRepresentationError(ValueError):
    """Raised when no common lossless fixed-point representation exists."""

    def __init__(self, blockers: Iterable[Blocker]):
        self.blockers = tuple(blockers)
        summary = "; ".join(
            f"value #{b.index} ({b.kind}, {b.category}): {b.reason}" for b in self.blockers
        )
        super().__init__(f"no common lossless fixed-point representation: {summary}")


#
# Exact internal representation
#


@dataclass
class _Exact:
    """value == coeff * prod(p**factors[p]) / den, with den >= 1"""

    index: int
    kind: str
    original: Any
    coeff: int
    den: int = 1
    factors: dict[int, int] = field(default_factory=dict)
    exponent: int | None = None
    numerator: int | None = None
    denominator: int | None = None


def _require_int(value: Any, what: str, index: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"value #{index}: {what} must be an int, got {value!r}")
    return value


def _normalize_rational(num: Any, den: Any, original: Any, index: int) -> _Exact | Blocker:
    if not isinstance(num, int) or not isinstance(den, int):
        return Blocker(
            index,
            _KIND_RATIONAL,
            original,
            ILLEGAL_DENOMINATOR,
            f"rational has a non-integer numerator/denominator: {num!r}/{den!r}",
        )
    if den == 0:
        return Blocker(
            index,
            _KIND_RATIONAL,
            original,
            ILLEGAL_DENOMINATOR,
            f"rational {num}/0 has a zero denominator",
        )
    if den < 0:
        num, den = -num, -den
    return _Exact(index, _KIND_RATIONAL, original, num, den, numerator=num, denominator=den)


def _normalize_tag(tag: CBORTag, index: int) -> _Exact | Blocker:
    payload = tag.value
    if tag.tag == 2:
        if not isinstance(payload, (bytes, bytearray)):
            raise ValueError(f"value #{index}: malformed tag 2 payload {payload!r}")
        return _Exact(index, _KIND_INTEGER, tag, int.from_bytes(payload, "big"))
    if tag.tag == 3:
        if not isinstance(payload, (bytes, bytearray)):
            raise ValueError(f"value #{index}: malformed tag 3 payload {payload!r}")
        return _Exact(index, _KIND_INTEGER, tag, -1 - int.from_bytes(payload, "big"))
    if tag.tag in (4, 5):
        try:
            exp, sig = payload
        except (TypeError, ValueError):
            raise ValueError(
                f"value #{index}: malformed tag {tag.tag} payload {payload!r}"
            ) from None
        _require_int(sig, f"tag {tag.tag} mantissa", index)
        _require_int(exp, f"tag {tag.tag} exponent", index)
        if tag.tag == 4:
            factors = {2: exp, 5: exp} if exp else {}
            return _Exact(index, _KIND_DECIMAL, tag, sig, 1, factors, exponent=exp)
        factors = {2: exp} if exp else {}
        return _Exact(index, _KIND_BIGFLOAT, tag, sig, 1, factors, exponent=exp)
    if tag.tag == 30:
        try:
            num, den = payload
        except (TypeError, ValueError):
            raise ValueError(f"value #{index}: malformed tag 30 payload {payload!r}") from None
        return _normalize_rational(num, den, tag, index)
    raise TypeError(f"value #{index}: unsupported CBOR tag {tag.tag}")


def _normalize(value: Any, index: int) -> _Exact | Blocker:
    if isinstance(value, bool):
        return _Exact(index, _KIND_INTEGER, value, int(value))
    if isinstance(value, int):
        return _Exact(index, _KIND_INTEGER, value, value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            return Blocker(
                index,
                _KIND_DECIMAL,
                value,
                NON_FINITE,
                f"non-finite decimal {value} cannot be represented in fixed point",
            )
        sign, digits, exp = value.as_tuple()
        coeff = 0
        for digit in digits:
            coeff = coeff * 10 + digit
        if sign:
            coeff = -coeff
        assert isinstance(exp, int)
        factors = {2: exp, 5: exp} if exp else {}
        return _Exact(index, _KIND_DECIMAL, value, coeff, 1, factors, exponent=exp)
    if isinstance(value, DecimalFraction):
        mantissa = _require_int(value.mantissa, "decimal fraction mantissa", index)
        exponent = _require_int(value.exponent, "decimal fraction exponent", index)
        factors = {2: exponent, 5: exponent} if exponent else {}
        return _Exact(index, _KIND_DECIMAL, value, mantissa, 1, factors, exponent=exponent)
    if isinstance(value, BigFloat):
        mantissa = _require_int(value.mantissa, "bigfloat mantissa", index)
        exponent = _require_int(value.exponent, "bigfloat exponent", index)
        factors = {2: exponent} if exponent else {}
        return _Exact(index, _KIND_BIGFLOAT, value, mantissa, 1, factors, exponent=exponent)
    if isinstance(value, Fraction):
        return _Exact(
            index,
            _KIND_RATIONAL,
            value,
            value.numerator,
            value.denominator,
            numerator=value.numerator,
            denominator=value.denominator,
        )
    if isinstance(value, RawRational):
        return _normalize_rational(value.numerator, value.denominator, value, index)
    if isinstance(value, _CBOR_TAG_TYPES):
        return _normalize_tag(cast("CBORTag", value), index)
    raise TypeError(
        f"value #{index}: unsupported type {type(value).__name__}; expected int, "
        "Decimal, DecimalFraction, BigFloat, Fraction, RawRational or CBORTag"
    )


#
# Integer/exponent arithmetic helpers (no giant-integer expansion)
#


def _factor_base(base: int) -> dict[int, int]:
    factors: dict[int, int] = {}
    n = base
    p = 2
    while p * p <= n:
        while n % p == 0:
            factors[p] = factors.get(p, 0) + 1
            n //= p
        p += 1 if p == 2 else 2
    if n > 1:
        factors[n] = factors.get(n, 0) + 1
    return factors


def _vp(n: int, p: int) -> int:
    """The p-adic valuation of a non-zero integer."""
    n = abs(n)
    valuation = 0
    while n % p == 0:
        n //= p
        valuation += 1
    return valuation


def _strip_to_base(n: int, base: int) -> int:
    """Remove every factor shared with ``base``; >1 means a foreign factor."""
    while n > 1:
        common = gcd(n, base)
        if common == 1:
            break
        n //= common
    return n


def _fmt_int(n: int) -> str:
    """Format an integer for a reason string without dumping huge values."""
    if n.bit_length() <= 64:
        return str(n)
    text = hex(n)
    return f"{n.bit_length()}-bit integer {text[:18]}...{text[-8:]}"


@dataclass
class _Fit:
    """Feasibility of one value in one base; caches per-value factor data."""

    ok: bool
    reason: str = ""
    any_scale: bool = False  # the value is zero: every scale works
    s_min: int = 0  # smallest scale at which the mantissa is an integer
    c0: int = 0  # coefficient with all tracked primes stripped
    u_static: dict[int, int] = field(default_factory=dict)  # exponent per prime


def _fit_for_base(exact: _Exact, base: int, primes: dict[int, int]) -> _Fit:
    if exact.coeff == 0:
        return _Fit(ok=True, any_scale=True)
    # Cofactor of the reduced denominator that is coprime to the base
    reduced_den = exact.den // gcd(exact.coeff, exact.den)
    residual = _strip_to_base(reduced_den, base)
    tracked = sorted(set(primes) | set(exact.factors))
    c0 = exact.coeff
    u_static: dict[int, int] = {}
    bad_prime = None
    for p in tracked:
        valuation = 0
        while c0 % p == 0:
            c0 //= p
            valuation += 1
        net = valuation + exact.factors.get(p, 0) - _vp(exact.den, p)
        u_static[p] = net
        if net < 0 and p not in primes:
            bad_prime = p
    if bad_prime is not None:
        return _Fit(
            ok=False,
            reason=(
                f"value carries denominator prime {bad_prime} which does not divide base {base}"
            ),
        )
    if residual != 1:
        return _Fit(
            ok=False,
            reason=(
                f"reduced denominator keeps cofactor {_fmt_int(residual)} coprime to base {base}"
            ),
        )
    s_min: int | None = None
    for p, a in primes.items():
        # need s such that net + s * a >= 0  <=>  s >= ceil(-net / a)
        need = -(u_static[p] // a)
        if s_min is None or need > s_min:
            s_min = need
    assert s_min is not None  # every base >= 2 has at least one prime
    return _Fit(ok=True, s_min=s_min, c0=c0, u_static=u_static)


def _mantissa_bits_estimate(fit: _Fit, primes: dict[int, int], scale: int) -> float:
    """Float estimate of the mantissa bit length; exact values stay symbolic."""
    if fit.any_scale:
        return 0.0
    estimate = abs(fit.c0).bit_length() - 1.0
    for p, net in fit.u_static.items():
        u = net + scale * primes.get(p, 0)
        if u:
            estimate += u * math.log2(p)
    return estimate + 1.0


def _exact_mantissa(fit: _Fit, primes: dict[int, int], scale: int) -> int:
    """
    The mantissa ``value * base**scale`` as a plain integer.

    Only called when the estimate says the result is small, so the net prime
    exponents are small even if the original exponents were astronomical.
    """
    if fit.any_scale:
        return 0
    mantissa = fit.c0
    for p, net in fit.u_static.items():
        u = net + scale * primes.get(p, 0)
        if u < 0:
            raise AssertionError("mantissa requested at an infeasible scale")
        if u:
            mantissa *= p**u
    return mantissa


def _fits_width(fit: _Fit, primes: dict[int, int], scale: int, width: int) -> bool:
    if fit.any_scale:
        return True
    estimate = _mantissa_bits_estimate(fit, primes, scale)
    if estimate <= width - 1 - _MARGIN:
        return True
    if estimate >= width + _MARGIN:
        return False
    mantissa = _exact_mantissa(fit, primes, scale)
    return -(1 << (width - 1)) <= mantissa <= (1 << (width - 1)) - 1


def _signed_width(mantissa: int) -> int:
    if mantissa >= 0:
        return mantissa.bit_length() + 1
    magnitude = -mantissa
    if magnitude & (magnitude - 1) == 0:  # exact power of two: -2**(w-1) fits
        return magnitude.bit_length()
    return magnitude.bit_length() + 1


_LOG2_DECIMAL: dict[int, Decimal] = {}


def _log2_decimal(p: int) -> Decimal:
    if p not in _LOG2_DECIMAL:
        with localcontext() as ctx:
            ctx.prec = 80
            _LOG2_DECIMAL[p] = +(Decimal(p).ln() / Decimal(2).ln())
    return _LOG2_DECIMAL[p]


def _precise_bit_length(fit: _Fit, primes: dict[int, int], scale: int) -> tuple[int, bool]:
    """
    Bit length of a mantissa too large to materialize, computed from the
    exponents with high-precision logarithms.  Returns ``(bits, exact)`` where
    ``exact`` is False if the value landed too close to a power of two to
    decide its bit length with certainty.
    """
    with localcontext() as ctx:
        ctx.prec = 80
        total = Decimal(abs(fit.c0)).ln() / _log2_decimal(2)
        for p, net in fit.u_static.items():
            u = net + scale * primes.get(p, 0)
            if u:
                total += u * _log2_decimal(p)
        floor = total.to_integral_value(rounding="ROUND_FLOOR")
        fraction = total - floor
        exact = min(fraction, 1 - fraction) > Decimal("1e-30")
    return int(floor) + 1, exact


def _required_width(fit: _Fit, primes: dict[int, int], scale: int) -> tuple[int, bool]:
    """Smallest signed width holding the mantissa; ``(width, exact)``."""
    if fit.any_scale:
        return 1, True
    estimate = _mantissa_bits_estimate(fit, primes, scale)
    if estimate < 4096:
        return _signed_width(_exact_mantissa(fit, primes, scale)), True
    bits, exact = _precise_bit_length(fit, primes, scale)
    if fit.c0 < 0 and abs(fit.c0) == 1:
        # The mantissa is an exact power of two only if no odd prime remains
        if all(net + scale * primes.get(p, 0) == 0 for p, net in fit.u_static.items() if p != 2):
            return bits, exact
    return bits + 1, exact


#
# Column planning
#


def plan_fixed_point_column(
    values: Iterable[Any],
    *,
    bases: Sequence[int],
    min_scale: int = 0,
    max_scale: int,
    bit_widths: Sequence[int],
) -> ColumnReport:
    """
    Find a common lossless fixed-point representation for a column of values.

    :param values: CBOR integers (``int``), decimal fractions (``Decimal``,
        :class:`DecimalFraction` or tag 4 :class:`~cbor2.CBORTag`), binary
        bigfloats (:class:`BigFloat` or tag 5 tag) and rationals
        (:class:`~fractions.Fraction`, :class:`RawRational` or tag 30 tag).
    :param bases: allowed fixed-point bases, in order of preference
    :param min_scale: smallest allowed scale (may be negative)
    :param max_scale: largest allowed scale
    :param bit_widths: candidate signed bit widths
    :return: a :class:`ColumnReport` whose plan uses the smallest usable bit
        width; ties are broken by the given base order, then by ascending
        scale.  If no common scale exists, the report lists the blocking
        values with verifiable reasons; nothing is ever rounded to fit.
    """
    values = list(values)
    bases = list(bases)
    widths = sorted(set(bit_widths))
    if not bases:
        raise ValueError("at least one base is required")
    for base in bases:
        if not isinstance(base, int) or isinstance(base, bool) or base < 2:
            raise ValueError(f"bases must be integers >= 2, got {base!r}")
    if not widths:
        raise ValueError("at least one candidate bit width is required")
    for width in widths:
        if not isinstance(width, int) or isinstance(width, bool) or width < 1:
            raise ValueError(f"bit widths must be positive integers, got {width!r}")
    if min_scale > max_scale:
        raise ValueError(f"min_scale {min_scale} exceeds max_scale {max_scale}")

    exacts: list[_Exact] = []
    blockers: list[Blocker] = []
    for index, value in enumerate(values):
        normalized = _normalize(value, index)
        if isinstance(normalized, Blocker):
            blockers.append(normalized)
        else:
            exacts.append(normalized)

    primes = {base: _factor_base(base) for base in bases}
    fits = {base: [_fit_for_base(exact, base, primes[base]) for exact in exacts] for base in bases}

    # Bases viable for the whole column, in the given order
    viable: list[tuple[int, int]] = []
    for base in bases:
        if all(fit.ok for fit in fits[base]):
            s_needed = max((fit.s_min for fit in fits[base] if not fit.any_scale), default=0)
            s_low = max(min_scale, s_needed)
            if s_low <= max_scale:
                viable.append((base, s_low))

    # Smallest usable width first, then base order, then ascending scale
    # (the mantissa only grows with the scale, so the lowest feasible scale
    # of each base is also its best scale)
    chosen: tuple[int, int, int] | None = None
    for width in widths:
        for base, s_low in viable:
            if all(_fits_width(fit, primes[base], s_low, width) for fit in fits[base]):
                chosen = (base, s_low, width)
                break
        if chosen is not None:
            break

    if chosen is not None and not blockers:
        base, scale, width = chosen
        entries = tuple(
            ColumnEntry(
                index=exact.index,
                kind=exact.kind,
                original=exact.original,
                mantissa=_exact_mantissa(fit, primes[base], scale),
                exponent=exact.exponent,
                numerator=exact.numerator,
                denominator=exact.denominator,
            )
            for exact, fit in zip(exacts, fits[base])
        )
        return ColumnReport(FixedPointPlan(base, scale, width, entries))

    if not viable and exacts:
        # No common scale: list every value that blocks at least one base,
        # with a verifiable reason per base
        for position, exact in enumerate(exacts):
            failures = []
            for base in bases:
                fit = fits[base][position]
                if not fit.ok:
                    failures.append(BaseFailure(base, fit.reason))
                elif not fit.any_scale and max(min_scale, fit.s_min) > max_scale:
                    failures.append(
                        BaseFailure(
                            base,
                            f"requires scale >= {fit.s_min} in base {base} but "
                            f"the allowed scale range is [{min_scale}, {max_scale}]",
                        )
                    )
            if failures:
                blockers.append(
                    Blocker(
                        exact.index,
                        exact.kind,
                        exact.original,
                        NO_COMMON_SCALE,
                        "no allowed base and scale represents this value without rounding",
                        tuple(failures),
                    )
                )
    elif chosen is None:
        # A base/scale exists but no candidate width holds every mantissa
        max_width = widths[-1]
        for position, exact in enumerate(exacts):
            failures = []
            for base, s_low in viable:
                fit = fits[base][position]
                required, exact_width = _required_width(fit, primes[base], s_low)
                if required > max_width:
                    qualifier = "" if exact_width else "approximately "
                    failures.append(
                        BaseFailure(
                            base,
                            f"mantissa at base {base}, scale {s_low} needs a "
                            f"signed width of {qualifier}{required} bits, more "
                            f"than the largest candidate {max_width}",
                        )
                    )
            if failures:
                blockers.append(
                    Blocker(
                        exact.index,
                        exact.kind,
                        exact.original,
                        OUT_OF_RANGE,
                        "mantissa overflows every candidate bit width",
                        tuple(failures),
                    )
                )

    return ColumnReport(None, tuple(blockers))


#
# Lossless decoding of the exact input types
#


class ExactDecoder(CBORDecoder):
    """
    Pure-Python decoder that decodes tags 4, 5 and 30 losslessly.

    Decimal fractions become :class:`DecimalFraction`, bigfloats become
    :class:`BigFloat` and rationals become :class:`~fractions.Fraction` (or
    :class:`RawRational` when the denominator is illegal), so extreme
    exponents are never expanded and illegal denominators are preserved for
    the analyzer instead of raising.
    """

    def _decode(self, immutable: bool = False, unshared: bool = False) -> Any:
        if immutable:
            old_immutable = self._immutable
            self._immutable = True
        if unshared:
            old_index = self._share_index
            self._share_index = None
        try:
            initial_byte = self.read(1)[0]
            major_type = initial_byte >> 5
            subtype = initial_byte & 31
            if major_type == 6:
                return self.decode_semantic(subtype)
            decoder = major_decoders[major_type]
            return decoder(self, subtype)
        finally:
            if immutable:
                self._immutable = old_immutable
            if unshared:
                self._share_index = old_index

    def decode_semantic(self, subtype: int) -> Any:
        tagnum = self._decode_length(subtype)
        if tagnum == 4:
            try:
                exp, sig = self._decode()
            except (TypeError, ValueError) as exc:
                raise CBORDecodeValueError("Incorrect tag 4 payload") from exc
            return self.set_shareable(DecimalFraction(sig, exp))
        if tagnum == 5:
            try:
                exp, sig = self._decode()
            except (TypeError, ValueError) as exc:
                raise CBORDecodeValueError("Incorrect tag 5 payload") from exc
            return self.set_shareable(BigFloat(sig, exp))
        if tagnum == 30:
            payload = self._decode(immutable=True, unshared=True)
            try:
                num, den = payload
            except (TypeError, ValueError):
                raise CBORDecodeValueError(
                    "error decoding rational: input value was not a tuple"
                ) from None
            if isinstance(num, int) and isinstance(den, int) and den != 0:
                return self.set_shareable(Fraction(num, den))
            return self.set_shareable(RawRational(num, den))
        if semantic_decoder := semantic_decoders.get(tagnum):
            return semantic_decoder(self)
        tag = CBORTag(tagnum, None)
        self.set_shareable(tag)
        tag.value = self._decode(unshared=True)
        if self._tag_hook:
            tag = self._tag_hook(self, tag)
        return self.set_shareable(tag)


def loads_exact(s: bytes | bytearray | memoryview) -> Any:
    """Decode a single CBOR value losslessly with :class:`ExactDecoder`."""
    from io import BytesIO

    with BytesIO(bytes(s)) as fp:
        return ExactDecoder(fp).decode()


def load_exact(fp: IO[bytes]) -> Any:
    """Decode a single CBOR value from a file losslessly."""
    return ExactDecoder(fp).decode()
