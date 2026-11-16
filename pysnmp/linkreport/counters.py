#
# This file is part of pysnmp software.
#
# Copyright (c) 2005-2020, Ilya Etingof <etingof@gmail.com>
# License: https://www.pysnmp.com/pysnmp/license.html
#
"""Counter arithmetic for link reports.

SNMP counters wrap and reset; a raw ``cur - prev`` is not an increment.  This
module derives, for one counter and one sampling window, the increment that is
certain (the lower bound), the largest increment still compatible with the
evidence (the upper bound), and how many full counter cycles may be hidden in
between.

The evidence used is:

* the two counter values,
* the width of the counter (32 or 64 bit),
* the length of the sampling window,
* the link speed, which caps how many bytes could have passed,
* ``ifCounterDiscontinuityTime``, which *proves* an interface reset when it
  changes and (when present) proves the counter was re-initialised otherwise.

Wrap and reset are kept distinct: a wrap is a counter that kept counting past
its modulus, a reset is a counter that was re-initialised.  They produce
different bounds and different report events.
"""

from __future__ import annotations

import math

from pysnmp.linkreport.model import (
    COUNTER32_MODULUS,
    STATUS_ABSENT,
    STATUS_CONFIRMED,
    STATUS_LOWER_BOUND,
    STATUS_RANGE,
    STATUS_RESET,
    STATUS_UNAVAILABLE,
    DeltaResult,
)

__all__ = [
    "capacity_bytes",
    "compare_counters",
    "describe_delta",
    "format_bytes",
]


def capacity_bytes(speed_bps: int | None, seconds: float, tolerance: float) -> int | None:
    """Return the most bytes a link of *speed_bps* can carry in *seconds*.

    ``None`` means no usable ceiling is known.  The tolerance widens the
    ceiling so that ifSpeed rounding and poll jitter cannot make a genuine
    wrap look impossible.
    """
    if not speed_bps or speed_bps <= 0 or seconds <= 0:
        return None
    return int(math.floor(speed_bps * seconds / 8.0 * (1.0 + tolerance)))


def format_bytes(value: int | None) -> str:
    """Format a byte count with SI units.  ``None`` renders as "unknown"."""
    if value is None:
        return "unknown"
    scaled = float(value)
    if scaled < 1000:
        return f"{value} B"
    for unit in ("kB", "MB", "GB", "TB", "PB", "EB"):
        scaled /= 1000.0
        if scaled < 1000 or unit == "EB":
            return f"{scaled:.3f} {unit}"
    return f"{value} B"


def describe_delta(delta: DeltaResult) -> str:
    """Render a :class:`DeltaResult` as a short human readable phrase."""
    if delta.status == STATUS_UNAVAILABLE:
        return f"unavailable ({delta.note})" if delta.note else "unavailable"
    if delta.status == STATUS_ABSENT:
        return f"not observed ({delta.note})" if delta.note else "not observed"
    if delta.status == STATUS_RESET:
        upper = (
            "unknown"
            if delta.maximum is None
            else format_bytes(delta.maximum)
        )
        return f"reset; 0..{upper} since re-initialisation"
    if delta.status == STATUS_CONFIRMED:
        return f"{format_bytes(delta.minimum)} confirmed"
    if delta.maximum is None:
        return f"at least {format_bytes(delta.minimum)} (upper bound unknown)"
    return f"{format_bytes(delta.minimum)}..{format_bytes(delta.maximum)}"


def compare_counters(
    *,
    direction: str,
    previous: int | None,
    current: int | None,
    bits: int,
    elapsed_seconds: float,
    capacity: int | None,
    source: str | None,
    discontinuity_changed: bool | None,
    counter64_wrap_min_seconds: float,
) -> DeltaResult:
    """Compare one counter across one sampling window.

    Parameters
    ----------
    direction:
        ``"in"`` or ``"out"``; carried through for reporting only.
    previous, current:
        Counter readings.  ``None`` means the counter was not present.
    bits:
        32 or 64.
    elapsed_seconds:
        Wall-clock length of the window.
    capacity:
        Maximum bytes that could have crossed the interface in the window,
        from :func:`capacity_bytes`.  ``None`` when the link speed is unknown.
    source:
        ``"ifHC*"`` (64 bit) or ``"if*"`` (32 bit); reporting only.
    discontinuity_changed:
        ``True`` when ``ifCounterDiscontinuityTime`` changed inside the
        window (proof of a reset), ``False`` when the object was read at both
        ends and did not change (proof of no reset), ``None`` when the
        evidence is not available.
    counter64_wrap_min_seconds:
        Shortest window in which a 64-bit decrease may be read as a wrap.

    Returns
    -------
    DeltaResult
        ``minimum`` is always certain; ``maximum`` is the tightest ceiling the
        evidence supports, or ``None`` when unbounded.
    """
    base = DeltaResult(
        direction=direction,
        source=source,
        bits=bits,
        previous=previous,
        current=current,
        elapsed_seconds=elapsed_seconds,
        capacity_bytes=capacity,
    )

    if previous is None or current is None:
        base.status = STATUS_ABSENT
        base.note = "counter missing at one end of the window"
        base.maximum = None
        return base

    if elapsed_seconds <= 0:
        base.status = STATUS_UNAVAILABLE
        base.note = "sampling window has non-positive duration"
        base.maximum = None
        return base

    modulus = 1 << bits
    if not 0 <= previous < modulus or not 0 <= current < modulus:
        base.status = STATUS_UNAVAILABLE
        base.note = f"reading outside Counter{bits} range"
        base.maximum = None
        return base

    # A changed discontinuity timestamp is direct evidence of a reset.
    if discontinuity_changed:
        return _reset_result(base, capacity, "ifCounterDiscontinuityTime advanced")

    raw = current - previous

    if bits == 64 and raw < 0:
        # A 64-bit counter cannot wrap in a short window: 2**64 octets in a
        # day would need >1.7 Pbit/s.  Treat that as a reset.
        if discontinuity_changed is False:
            return _wrap_result(
                base,
                modulus,
                raw,
                capacity,
                min_wraps=1,
                note=(
                    "64-bit counter decreased while ifCounterDiscontinuityTime "
                    "stayed put; treating the decrease as a wrap"
                ),
            )
        if elapsed_seconds < counter64_wrap_min_seconds:
            return _reset_result(
                base,
                capacity,
                "64-bit counter decreased inside a window too short for a wrap",
            )
        return _wrap_result(
            base,
            modulus,
            raw,
            capacity,
            min_wraps=1,
        )

    if raw < 0 and discontinuity_changed is False:
        # The agent says the counter was not re-initialised, so the decrease
        # must be a wrap even if the stated speed cannot explain it.
        return _wrap_result(
            base,
            modulus,
            raw,
            capacity,
            min_wraps=1,
            note=(
                "counter decreased while ifCounterDiscontinuityTime stayed "
                "put; treating the decrease as a wrap"
            ),
        )

    if raw < 0:
        # No discontinuity evidence: decide between wrap and reset from the
        # link capacity.  A 32-bit counter on a 1 Gbit/s link cannot wrap in
        # a five minute window, so a decrease there is a reset.
        if capacity is None:
            # Without a ceiling the decrease is equally consistent with a
            # wrap and with a reset.  A reset re-initialises the counter, so
            # nothing can be confirmed: claim zero, bound nothing.
            base.minimum = 0
            base.maximum = None
            base.wrap_count_min = 0
            base.wrap_count_max = None
            base.status = STATUS_RANGE
            base.note = (
                "counter decreased with no link rate and no discontinuity "
                "evidence; a wrap and a reset are both possible"
            )
            return base
        fits = _wrap_range(modulus, raw, capacity, min_increase=1)
        if fits is None or fits[1] < 1:
            return _reset_result(
                base,
                capacity,
                "decrease cannot be explained by a wrap at the known link rate",
            )
        return _wrap_result(
            base,
            modulus,
            raw,
            capacity,
            min_wraps=1,
        )

    # Counter did not decrease.  Zero or more whole cycles may still be
    # hidden if the link is fast enough to have wrapped and come back above
    # the previous reading.  This is the "cannot tell whether it wrapped more
    # than once" case that has to be reported as a range.
    return _wrap_result(
        base,
        modulus,
        raw,
        capacity,
        min_wraps=0,
    )


def _wrap_range(
    modulus: int, raw: int, capacity: int, min_increase: int = 0
) -> tuple[int, int] | None:
    """Return the inclusive range of whole wraps that fit within capacity.

    Every candidate increment is ``raw + n * modulus`` for ``n >= 0``.  Those
    that fall between *min_increase* and *capacity* are physically possible;
    the returned pair is the smallest and largest such ``n``.  ``None`` means
    no wrap count can explain the reading within the ceiling.
    """
    lower = math.ceil((min_increase - raw) / modulus)
    upper = math.floor((capacity - raw) / modulus)
    if upper < lower:
        return None
    return max(lower, 0), upper


def _reset_result(base: DeltaResult, capacity: int | None, reason: str) -> DeltaResult:
    """Build a reset outcome.

    A reset re-initialises the counter, so nothing before the reset can be
    attributed and nothing after it can be bounded from the two readings
    alone.  The confirmed increment is therefore zero; the ceiling is the
    total link capacity across the window (``None`` when unknown).
    """
    base.status = STATUS_RESET
    base.minimum = 0
    base.maximum = capacity
    base.wrap_count_min = 0
    base.wrap_count_max = 0
    base.note = reason
    return base


def _wrap_result(
    base: DeltaResult,
    modulus: int,
    raw: int,
    capacity: int | None,
    *,
    min_wraps: int,
    note: str = "",
) -> DeltaResult:
    """Build a wrap outcome, narrowing to a confirmed value when possible."""
    contradiction = ""
    if capacity is None:
        # Without a rate ceiling a 32-bit counter could have wrapped any
        # number of times.  A 64-bit counter cannot wrap inside any window a
        # link report covers, so the single cycle the decrease forces is the
        # only one on the table.
        wrap_min = min_wraps
        wrap_max = min_wraps if modulus > COUNTER32_MODULUS else None
    else:
        bounds = _wrap_range(
            modulus, raw, capacity, min_increase=1 if raw < 0 else 0
        )
        if bounds is None:
            # The stated link rate cannot explain the reading at all.  Keep
            # the wrap the counter forces and flag the contradiction rather
            # than inventing a smaller number.
            wrap_min = min_wraps
            wrap_max = wrap_min
            contradiction = (
                "; the stated link rate is too low to carry this increment"
            )
        else:
            wrap_min = max(bounds[0], min_wraps)
            wrap_max = max(bounds[1], wrap_min)

    base.wrap_count_min = wrap_min
    base.wrap_count_max = wrap_max

    low = raw + wrap_min * modulus
    if low < 0:
        # Only reachable for a decrease plus a wrap count that does not
        # restore positivity; treat as a reset rather than reporting nonsense.
        return _reset_result(base, capacity, "counter value inconsistent with a wrap")

    base.minimum = low

    if wrap_max is None:
        base.maximum = None
        base.status = STATUS_LOWER_BOUND
        base.note = note or (
            "link rate unknown, so further whole wraps cannot be ruled out"
        )
        return base

    high = raw + wrap_max * modulus
    base.maximum = high
    if high == low:
        base.status = STATUS_CONFIRMED
        if wrap_min:
            base.note = (note or f"{wrap_min} whole counter cycle(s) unwrapped") + contradiction
        else:
            base.note = note + contradiction
    else:
        base.status = STATUS_RANGE
        if note:
            base.note = note + contradiction
        else:
            base.note = (
                f"{wrap_min}..{wrap_max} whole counter cycle(s) possible"
                + contradiction
            )
    return base


def cross_check_32bit(
    *,
    previous32: int | None,
    current32: int | None,
    delta: DeltaResult,
) -> bool | None:
    """Check a 64-bit derived delta against the matching 32-bit counter.

    Returns ``None`` when the 32-bit readings are missing, ``True`` when the
    observed 32-bit change is exactly the derived delta modulo 2**32, and
    ``False`` otherwise.  A ``False`` result means the two counters disagree;
    the 64-bit value is still the trustworthy one.
    """
    if previous32 is None or current32 is None:
        return None
    if delta.status in (STATUS_UNAVAILABLE, STATUS_ABSENT, STATUS_RESET):
        return None

    low_residue = delta.minimum % COUNTER32_MODULUS
    if delta.maximum is not None and delta.maximum % COUNTER32_MODULUS != low_residue:
        # The admissible range spans a 32-bit cycle boundary, so every
        # residue is possible and the comparison proves nothing.
        return None

    observed = (current32 - previous32) % COUNTER32_MODULUS
    return observed == low_residue
