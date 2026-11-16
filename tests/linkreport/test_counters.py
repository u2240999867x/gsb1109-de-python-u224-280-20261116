"""Tests for the link report counter arithmetic.

These pin the behaviour the daily report depends on:

* Counter32 and Counter64 increments are computed from the widest counter
  the device publishes.
* A wrap is separated from a reset; wraps that cannot be counted exactly are
  reported as a range rather than a point value.
* A reset on one interface never invalidates another interface's window.
"""

import pytest

from pysnmp.linkreport import ReportConfig
from pysnmp.linkreport.counters import (
    capacity_bytes,
    compare_counters,
    cross_check_32bit,
)
from pysnmp.linkreport.model import (
    COUNTER32_MODULUS,
    DeltaResult,
    STATUS_ABSENT,
    STATUS_CONFIRMED,
    STATUS_LOWER_BOUND,
    STATUS_RANGE,
    STATUS_RESET,
    STATUS_UNAVAILABLE,
)

DAY = 86400.0
Gb = 1_000_000_000


def _compare(previous, current, *, bits=32, seconds=300.0, capacity=None, disc=None):
    return compare_counters(
        direction="in",
        previous=previous,
        current=current,
        bits=bits,
        elapsed_seconds=seconds,
        capacity=capacity,
        source="ifHC*" if bits == 64 else "if*",
        discontinuity_changed=disc,
        counter64_wrap_min_seconds=365 * DAY,
    )


# --------------------------------------------------------------------------
# Capacity
# --------------------------------------------------------------------------


def test_capacity_is_rate_times_time_with_tolerance():
    # 1 Gbit/s for 8 s is 1 GB; 10% tolerance adds 100 MB.
    assert capacity_bytes(Gb, 8.0, 0.10) == 1_100_000_000


def test_capacity_unknown_or_degenerate_rate():
    assert capacity_bytes(None, 300.0, 0.1) is None
    assert capacity_bytes(0, 300.0, 0.1) is None
    assert capacity_bytes(Gb, 0, 0.1) is None


# --------------------------------------------------------------------------
# Monotonic counters
# --------------------------------------------------------------------------


def test_plain_increase_is_confirmed():
    # 50 kB over 300 s on a 1 Mbit/s link: no hidden cycle can fit.
    delta = _compare(1_000, 51_000, capacity=capacity_bytes(1_000_000, 300, 0.1))
    assert delta.status == STATUS_CONFIRMED
    assert delta.minimum == delta.maximum == 50_000
    assert delta.wrap_count_min == delta.wrap_count_max == 0


def test_64bit_increase_is_confirmed_without_rate():
    delta = _compare(10**15, 10**15 + 12345, bits=64)
    assert delta.status == STATUS_CONFIRMED
    assert delta.minimum == 12345
    assert delta.maximum == 12345


def test_unbounded_fast_counter_reports_range_or_lower_bound():
    # A 32-bit counter on a 100 Gbit/s link can wrap several times a minute.
    delta = _compare(1_000, 2_000, capacity=capacity_bytes(100 * Gb, 300, 0.1))
    assert delta.status == STATUS_RANGE
    assert delta.minimum == 1_000
    assert delta.maximum > COUNTER32_MODULUS
    assert delta.wrap_count_max > 0


def test_missing_counter_is_absent_not_zero():
    delta = _compare(None, 500, capacity=capacity_bytes(Gb, 300, 0.1))
    assert delta.status == STATUS_ABSENT
    assert delta.minimum == 0
    assert delta.maximum is None


# --------------------------------------------------------------------------
# Wrap
# --------------------------------------------------------------------------


def test_single_wrap_is_unwrapped_exactly():
    # A 32-bit counter passes 2**32 and lands on 500.  A 1 Mbit/s link
    # carries too little in 300 s to fit a second cycle.
    previous = COUNTER32_MODULUS - 500
    delta = _compare(previous, 500, capacity=capacity_bytes(1_000_000, 300, 0.1))
    assert delta.status == STATUS_CONFIRMED
    assert delta.minimum == 1000
    assert delta.wrap_count_min == delta.wrap_count_max == 1


def test_wrap_with_unknown_rate_gives_lower_bound_not_point():
    # A 1000-byte increase whose ceiling is unknown: one or more hidden
    # cycles are possible, so the increment is only bounded from below.
    delta = _compare(1_000, 2_000)
    assert delta.status == STATUS_LOWER_BOUND
    assert delta.minimum == 1_000
    assert delta.maximum is None
    assert delta.wrap_count_max is None


def test_multiple_wraps_are_a_range_not_a_guess():
    # Unchanged readings on a 100 Gbit/s link: 0, 1, 2 or 3 whole cycles fit
    # in the 3750 GB the window can carry.
    capacity = capacity_bytes(100 * Gb, 300, 0.1)
    delta = _compare(1_000, 1_000, capacity=capacity)
    assert delta.status == STATUS_RANGE
    assert delta.minimum == 0
    assert delta.maximum == COUNTER32_MODULUS * (capacity // COUNTER32_MODULUS)
    assert delta.wrap_count_max == capacity // COUNTER32_MODULUS


def test_decrease_with_no_evidence_claims_nothing():
    # With neither a link rate nor ifCounterDiscontinuityTime, a 1000-byte
    # drop could be a wrap of 4.29 GB or a reset to 4000.  Claiming the wrap
    # would invent traffic, so the report keeps the floor at zero and leaves
    # the ceiling open.
    delta = _compare(5_000, 4_000)
    assert delta.status == STATUS_RANGE
    assert delta.minimum == 0
    assert delta.maximum is None
    assert delta.wrap_count_max is None


# --------------------------------------------------------------------------
# Reset
# --------------------------------------------------------------------------


def test_decrease_that_cannot_wrap_at_known_rate_is_a_reset():
    # A 1 Mbit/s link carries at most ~41 MB in 300 s, so a drop of 4 GB
    # cannot be a wrap: it is a re-initialisation.
    capacity = capacity_bytes(1_000_000, 300, 0.1)
    delta = _compare(4_000_000_000, 1_000, capacity=capacity)
    assert delta.status == STATUS_RESET
    assert delta.minimum == 0
    assert delta.maximum == capacity


def test_discontinuity_change_forces_reset_even_when_values_rose():
    delta = _compare(1_000, 2_000, capacity=capacity_bytes(Gb, 300, 0.1), disc=True)
    assert delta.status == STATUS_RESET
    assert delta.minimum == 0


def test_discontinuity_unchanged_forces_wrap_even_beyond_rated_capacity():
    # The agent asserts the counter was not re-initialised, so the drop is a
    # wrap even though the stated rate cannot explain it.
    capacity = capacity_bytes(1_000_000, 300, 0.1)
    delta = _compare(4_000_000_000, 1_000, capacity=capacity, disc=False)
    assert delta.status == STATUS_CONFIRMED
    assert delta.wrap_count_min == delta.wrap_count_max == 1
    assert "link rate is too low" in delta.note


def test_64bit_decrease_in_a_short_window_is_a_reset():
    delta = _compare(5_000_000, 1_000, bits=64, seconds=300.0)
    assert delta.status == STATUS_RESET


def test_64bit_decrease_across_a_year_is_a_wrap():
    delta = _compare(5_000_000, 1_000, bits=64, seconds=400 * DAY)
    assert delta.status == STATUS_CONFIRMED
    assert delta.minimum == (1_000 - 5_000_000) % (1 << 64)


def test_reset_ceiling_is_link_capacity_when_known():
    capacity = capacity_bytes(1_000_000, 300, 0.1)
    delta = _compare(9_000, 10, capacity=capacity, disc=True)
    assert delta.maximum == capacity


# --------------------------------------------------------------------------
# Degenerate input
# --------------------------------------------------------------------------


def test_zero_length_window_is_unavailable():
    delta = _compare(1, 2, seconds=0.0)
    assert delta.status == STATUS_UNAVAILABLE
    assert delta.maximum is None


def test_reading_outside_counter_range_is_unavailable():
    delta = _compare(COUNTER32_MODULUS, 5)
    assert delta.status == STATUS_UNAVAILABLE


def test_counter64_wrap_boundary_is_inclusive():
    modulus = 1 << 64
    delta = _compare(modulus - 10, 5, bits=64, seconds=400 * DAY)
    assert delta.status == STATUS_CONFIRMED
    assert delta.minimum == 15


@pytest.mark.parametrize(
    "previous,current,expected",
    [
        (0, 0, 0),
        (10, 10, 0),
    ],
)
def test_32bit_edge_values(previous, current, expected):
    delta = _compare(previous, current)
    assert delta.minimum == expected


def test_max_to_zero_without_evidence_claims_nothing():
    # Counter 2**32-1 -> 0 may be a one-byte wrap or a reset to zero; with no
    # rate and no discontinuities the floor stays at zero.
    delta = _compare(COUNTER32_MODULUS - 1, 0)
    assert delta.minimum == 0
    assert delta.maximum is None


def test_default_config_is_usable():
    config = ReportConfig()
    assert config.counter64_wrap_min_seconds == 365 * DAY


# --------------------------------------------------------------------------
# 32-bit cross-check of a 64-bit delta
# --------------------------------------------------------------------------


def test_cross_check_accepts_matching_32bit_delta():
    delta = _compare(0, 2**32 + 500, bits=64, capacity=capacity_bytes(Gb, 300, 0.1))
    assert cross_check_32bit(previous32=0, current32=500, delta=delta) is True


def test_cross_check_rejects_disagreeing_32bit_delta():
    delta = _compare(0, 2**32 + 500, bits=64, capacity=capacity_bytes(Gb, 300, 0.1))
    assert cross_check_32bit(previous32=0, current32=900, delta=delta) is False


def test_cross_check_is_silent_without_both_readings():
    delta = _compare(0, 100, bits=64, capacity=capacity_bytes(Gb, 300, 0.1))
    assert cross_check_32bit(previous32=None, current32=100, delta=delta) is None


def test_cross_check_ignores_range_spanning_a_32bit_boundary():
    # A 64-bit counter cannot wrap in a daily window, so a range only arises
    # for a hand-built result; the low 32 bits of such a range cannot be
    # compared meaningfully.
    delta = DeltaResult(
        direction="in",
        source="ifHC*",
        bits=64,
        previous=0,
        current=5_000_000_000,
        elapsed_seconds=300.0,
        minimum=5_000_000_000,
        maximum=9_000_000_000,
        status=STATUS_RANGE,
    )
    assert cross_check_32bit(previous32=0, current32=0, delta=delta) is None

