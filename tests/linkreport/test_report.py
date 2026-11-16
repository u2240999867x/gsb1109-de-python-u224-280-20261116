"""Tests for building a daily report from snapshots.

The scenarios encode the operational rules:

* a device reboot starts a new generation and no counter delta bridges it;
* a wrap is unwrapped, a reset is not;
* an interface reset stays local and cannot invalidate the deltas of the
  other interfaces on the same device.
"""

from datetime import datetime, timedelta, timezone

import pytest

from pysnmp.linkreport import (
    InterfaceSample,
    ReportConfig,
    Snapshot,
    build_report,
    render_text,
)
from pysnmp.linkreport.model import (
    COUNTER32_MODULUS,
    STATUS_CONFIRMED,
    STATUS_RESET,
)

T0 = datetime(2026, 11, 16, 0, 0, 0, tzinfo=timezone.utc)
Mbit = 1_000_000
Gbit = 1_000_000_000


def ts(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def iface(if_index, **kwargs) -> InterfaceSample:
    kwargs.setdefault("description", f"port{if_index}")
    return InterfaceSample(if_index=if_index, **kwargs)


def snapshot(minutes, interfaces, uptime_seconds=1000.0, device="r1") -> Snapshot:
    return Snapshot(
        device=device,
        sampled_at=ts(minutes),
        interfaces={sample.if_index: sample for sample in interfaces},
        sys_uptime_ticks=int(round(uptime_seconds * 100)),
    )


def _single_device(report):
    assert len(report.devices) == 1
    return report.devices[0]


# --------------------------------------------------------------------------
# Normal growth
# --------------------------------------------------------------------------


def test_continuous_counters_keep_every_byte():
    # A 1 Mbit/s link cannot wrap a 32-bit counter in five minutes, so the
    # increments are exact rather than a range.
    snapshots = [
        snapshot(0, [iface(1, speed_bps=Mbit, in_octets=1000, out_octets=500)]),
        snapshot(5, [iface(1, speed_bps=Mbit, in_octets=2000, out_octets=1500)], 1300),
        snapshot(10, [iface(1, speed_bps=Mbit, in_octets=4000, out_octets=2500)], 1600),
    ]
    device = _single_device(build_report(snapshots))

    assert len(device.generations) == 1
    summary = device.summaries[1]
    assert summary.inbound.minimum_bytes == 3000
    assert summary.inbound.maximum_bytes == 3000
    assert summary.outbound.minimum_bytes == 2000
    assert summary.inbound.is_confirmed


def test_multiple_interfaces_are_accounted_separately():
    snapshots = [
        snapshot(
            0,
            [
                iface(1, speed_bps=Mbit, in_octets=0, out_octets=0),
                iface(2, speed_bps=Mbit, in_octets=0, out_octets=0),
            ],
        ),
        snapshot(
            5,
            [
                iface(1, speed_bps=Mbit, in_octets=100, out_octets=200),
                iface(2, speed_bps=Mbit, in_octets=300, out_octets=400),
            ],
            1300,
        ),
    ]
    device = _single_device(build_report(snapshots))

    assert device.summaries[1].inbound.minimum_bytes == 100
    assert device.summaries[2].inbound.minimum_bytes == 300
    assert device.summaries[1].outbound.minimum_bytes == 200
    assert device.summaries[2].outbound.minimum_bytes == 400


# --------------------------------------------------------------------------
# Wrap
# --------------------------------------------------------------------------


def test_32bit_wrap_is_unwrapped_using_32bit_counters():
    # 500 bytes short of the modulus, then 500 bytes: exactly 1000 crossed.
    snapshots = [
        snapshot(
            0,
            [iface(3, speed_bps=Mbit, in_octets=COUNTER32_MODULUS - 500)],
        ),
        snapshot(
            5,
            [iface(3, speed_bps=Mbit, in_octets=500)],
            1300,
        ),
    ]
    device = _single_device(build_report(snapshots))
    summary = device.summaries[3]

    assert summary.inbound.minimum_bytes == 1000
    assert summary.inbound.maximum_bytes == 1000
    assert summary.inbound.wraps_confirmed == 1


def test_64bit_counters_are_preferred_and_need_no_unwrap():
    huge = 2**40
    snapshots = [
        snapshot(
            0,
            [iface(4, speed_bps=Gbit, in_octets=0, hc_in_octets=huge)],
        ),
        snapshot(
            5,
            [iface(4, speed_bps=Gbit, in_octets=0, hc_in_octets=huge + 10**9)],
            1300,
        ),
    ]
    device = _single_device(build_report(snapshots))
    summary = device.summaries[4]

    assert summary.inbound.minimum_bytes == 10**9
    assert summary.inbound.maximum_bytes == 10**9
    assert summary.inbound.wraps_confirmed == 0
    assert summary.inbound.sources == {"ifHC*"}


def test_ambiguous_wraps_are_reported_as_a_range():
    # A 10 Gbit/s port can move 412.5 GB in 300 s, which is 96 whole 32-bit
    # cycles.  An unchanged reading therefore hides 0..96 wraps: the report
    # must give the range rather than pick a value.
    snapshots = [
        snapshot(0, [iface(5, speed_bps=10 * Gbit, in_octets=7)]),
        snapshot(5, [iface(5, speed_bps=10 * Gbit, in_octets=7)], 1300),
    ]
    device = _single_device(build_report(snapshots))
    summary = device.summaries[5]

    assert summary.inbound.minimum_bytes == 0
    # The upper bound is whole cycles only: 96 * 2**32 fits in the 412.5 GB
    # the port could carry, 97 does not.
    assert summary.inbound.maximum_bytes == 96 * COUNTER32_MODULUS
    assert summary.inbound.wraps_possible_extra == 96
    assert any(
        event.kind == "ambiguous_wraps" for event in device.generations[0].events
    )


# --------------------------------------------------------------------------
# Reset locality - the key requirement
# --------------------------------------------------------------------------


def test_reset_on_one_interface_does_not_invalidate_the_others():
    snapshots = [
        snapshot(
            0,
            [
                iface(1, speed_bps=Mbit, in_octets=1_000_000, out_octets=0),
                iface(2, speed_bps=Mbit, in_octets=1_000_000, out_octets=0),
            ],
        ),
        snapshot(
            5,
            [
                # if1 re-initialised: counter dropped to a small value and
                # ifCounterDiscontinuityTime advanced.
                iface(
                    1,
                    speed_bps=Mbit,
                    in_octets=10,
                    out_octets=0,
                    discontinuity_ticks=1000,
                ),
                # if2 kept counting normally.
                iface(
                    2,
                    speed_bps=Mbit,
                    in_octets=1_100_000,
                    out_octets=0,
                    discontinuity_ticks=0,
                ),
            ],
            1300,
        ),
    ]
    device = _single_device(build_report(snapshots))

    # The reset interface is reported as a reset with a bounded loss,
    # never as a negative or guessed increment.
    reset_summary = device.summaries[1]
    assert reset_summary.inbound.reset_windows == 1
    assert reset_summary.inbound.minimum_bytes == 0
    assert reset_summary.inbound.maximum_bytes is not None
    assert any(
        event.kind == "reset" and event.if_index == 1
        for event in device.generations[0].events
    )

    # The untouched interface keeps its full, exact increment.
    intact = device.summaries[2]
    assert intact.inbound.reset_windows == 0
    assert intact.inbound.minimum_bytes == 100_000
    assert intact.inbound.maximum_bytes == 100_000
    assert intact.inbound.is_confirmed


def test_interface_missing_from_one_poll_does_not_break_others():
    snapshots = [
        snapshot(
            0,
            [
                iface(1, speed_bps=Mbit, in_octets=0),
                iface(2, speed_bps=Mbit, in_octets=0),
            ],
        ),
        # if2 was not returned by this walk.
        snapshot(5, [iface(1, speed_bps=Mbit, in_octets=1000)], 1300),
        # and it comes back later.
        snapshot(10, [iface(1, in_octets=2000), iface(2, in_octets=500)], 1600),
    ]
    device = _single_device(build_report(snapshots))

    assert device.summaries[1].inbound.minimum_bytes == 2000
    assert device.summaries[1].inbound.absent_windows == 0
    # if2 is bridged across its own gap: the minute-0 and minute-10 readings
    # are compared directly, over a ten minute window.
    assert device.summaries[2].inbound.windows == 1
    assert device.summaries[2].inbound.minimum_bytes == 500
    assert device.summaries[2].inbound.covered_seconds == 600.0


def test_new_interface_appearing_midday_has_no_fabricated_baseline():
    snapshots = [
        snapshot(0, [iface(1, speed_bps=Mbit, in_octets=0)]),
        snapshot(
            5,
            [
                iface(1, in_octets=100),
                iface(9, speed_bps=Mbit, in_octets=10**6),
            ],
            1300,
        ),
        snapshot(
            10,
            [
                iface(1, in_octets=200),
                iface(9, speed_bps=Mbit, in_octets=10**6 + 500),
            ],
            1600,
        ),
    ]
    device = _single_device(build_report(snapshots))

    assert device.summaries[1].inbound.minimum_bytes == 200
    assert device.summaries[9].inbound.minimum_bytes == 500
    assert device.summaries[9].inbound.maximum_bytes == 500


# --------------------------------------------------------------------------
# Device generations
# --------------------------------------------------------------------------


def test_reboot_starts_a_new_generation_and_is_not_bridged():
    snapshots = [
        snapshot(
            0,
            [iface(1, speed_bps=Mbit, in_octets=1_000_000)],
            uptime_seconds=50_000,
        ),
        snapshot(
            5,
            [iface(1, speed_bps=Mbit, in_octets=1_000_100)],
            uptime_seconds=50_300,
        ),
        # Reboot: uptime falls to 10 s and the counter restarts low.
        snapshot(
            10,
            [iface(1, speed_bps=Mbit, in_octets=20)],
            uptime_seconds=10,
        ),
        snapshot(
            15,
            [iface(1, speed_bps=Mbit, in_octets=120)],
            uptime_seconds=310,
        ),
    ]
    device = _single_device(build_report(snapshots))

    assert len(device.generations) == 2
    assert device.generations[1].start_reason.startswith("sysUpTime decreased")

    # The restart window is a reset, not a wrap and not a negative delta.
    restart_window = device.generations[1].summaries[1].windows[0]
    assert restart_window.inbound.status == STATUS_RESET

    # Within the second generation the counters are continuous again.
    assert device.generations[1].summaries[1].inbound.reset_windows == 1
    assert device.generations[0].summaries[1].inbound.minimum_bytes == 100


def test_time_ticks_wrap_alone_does_not_start_a_generation():
    # sysUpTime moving across the 2**32 boundary is a clock wrap, not a
    # reboot, and must not split the day.
    snapshots = [
        Snapshot(
            device="r1",
            sampled_at=ts(0),
            interfaces={1: iface(1, speed_bps=Mbit, in_octets=0)},
            sys_uptime_ticks=2**32 - 100,
        ),
        Snapshot(
            device="r1",
            sampled_at=ts(5),
            interfaces={1: iface(1, speed_bps=Mbit, in_octets=100)},
            sys_uptime_ticks=100,
        ),
    ]
    report = build_report(snapshots, ReportConfig())
    device = _single_device(report)

    assert len(device.generations) == 1
    assert device.summaries[1].inbound.minimum_bytes == 100


def test_missing_uptime_is_called_out():
    snapshots = [
        Snapshot(
            device="r1",
            sampled_at=ts(0),
            interfaces={1: iface(1, in_octets=0)},
            sys_uptime_ticks=None,
        ),
        Snapshot(
            device="r1",
            sampled_at=ts(5),
            interfaces={1: iface(1, in_octets=100)},
            sys_uptime_ticks=None,
        ),
    ]
    device = _single_device(build_report(snapshots))
    assert any("sysUpTime" in note for note in device.notes)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def test_text_report_mentions_wrap_and_reset_separately():
    snapshots = [
        snapshot(0, [iface(1, speed_bps=Mbit, in_octets=COUNTER32_MODULUS - 10)]),
        snapshot(5, [iface(1, speed_bps=Mbit, in_octets=10)], 1300),
    ]
    text = render_text(build_report(snapshots))
    assert "wrap" in text
    assert "Generation 1" in text


def test_devices_are_reported_independently():
    snapshots = [
        snapshot(0, [iface(1, in_octets=0)], device="a"),
        snapshot(5, [iface(1, in_octets=100)], 1300, device="a"),
        snapshot(0, [iface(1, in_octets=0)], device="b"),
        snapshot(5, [iface(1, in_octets=900)], 1300, device="b"),
    ]
    report = build_report(snapshots)
    assert [d.device for d in report.devices] == ["a", "b"]
    assert report.devices[0].summaries[1].inbound.minimum_bytes == 100
    assert report.devices[1].summaries[1].inbound.minimum_bytes == 900


def test_unsorted_input_is_ordered_by_timestamp():
    snapshots = [
        snapshot(10, [iface(1, in_octets=300)], 1600),
        snapshot(0, [iface(1, in_octets=100)]),
        snapshot(5, [iface(1, in_octets=200)], 1300),
    ]
    device = _single_device(build_report(snapshots))
    assert device.period_start == ts(0)
    assert device.period_end == ts(10)
    assert device.summaries[1].inbound.minimum_bytes == 200


def test_duplicate_timestamps_are_collapsed_with_a_note():
    first = snapshot(0, [iface(1, speed_bps=Mbit, in_octets=0)])
    # A second poll at the same instant (e.g. two collectors) must not create
    # a zero-length window.
    duplicate = Snapshot(
        device="r1",
        sampled_at=first.sampled_at,
        interfaces={1: iface(1, speed_bps=Mbit, in_octets=50)},
        sys_uptime_ticks=first.sys_uptime_ticks,
    )
    later = snapshot(5, [iface(1, speed_bps=Mbit, in_octets=150)], 1300)

    device = _single_device(build_report([first, duplicate, later]))

    assert any("duplicate sample" in note for note in device.notes)
    assert device.summaries[1].inbound.windows == 1
    assert device.summaries[1].inbound.minimum_bytes == 100


def test_counter_source_can_differ_per_direction():
    # The device publishes high-capacity inbound but only 32-bit outbound.
    snapshots = [
        snapshot(
            0,
            [
                iface(
                    1,
                    speed_bps=Mbit,
                    in_octets=0,
                    hc_in_octets=2**40,
                    out_octets=0,
                )
            ],
        ),
        snapshot(
            5,
            [
                iface(
                    1,
                    speed_bps=Mbit,
                    in_octets=500,
                    hc_in_octets=2**40 + 1000,
                    out_octets=500,
                )
            ],
            1300,
        ),
    ]
    summary = _single_device(build_report(snapshots)).summaries[1]
    assert summary.inbound.sources == {"ifHC*"}
    assert summary.outbound.sources == {"if*"}
    assert summary.inbound.minimum_bytes == 1000
    assert summary.outbound.minimum_bytes == 500


def test_wrap_and_reset_are_worded_differently_in_text():
    wrap_day = [
        snapshot(0, [iface(1, speed_bps=Mbit, in_octets=COUNTER32_MODULUS - 10)]),
        snapshot(5, [iface(1, speed_bps=Mbit, in_octets=10)], 1300),
    ]
    reset_day = [
        snapshot(0, [iface(1, speed_bps=Mbit, in_octets=5_000_000)]),
        snapshot(5, [iface(1, speed_bps=Mbit, in_octets=10)], 1300),
    ]
    wrap_report = build_report(wrap_day)
    reset_report = build_report(reset_day)
    wrap_text = render_text(wrap_report)
    reset_text = render_text(reset_report)

    # The rendered report names the wrap and the reset explicitly...
    assert "wrap" in wrap_text
    assert "reset" in reset_text

    # ... and the structured events keep the two kinds apart, whichever
    # wording the note uses.
    wrap_kinds = {event.kind for event in wrap_report.devices[0].generations[0].events}
    reset_kinds = {event.kind for event in reset_report.devices[0].generations[0].events}
    assert "wrap" in wrap_kinds and "reset" not in wrap_kinds
    assert "reset" in reset_kinds and "wrap" not in reset_kinds


# --------------------------------------------------------------------------
# ifCounterDiscontinuityTime interpretation
# --------------------------------------------------------------------------


def _two_polls(first_tick, second_tick, counters=(1000, 2_000)):
    return [
        snapshot(
            0,
            [
                iface(
                    1,
                    speed_bps=Mbit,
                    in_octets=counters[0],
                    discontinuity_ticks=first_tick,
                )
            ],
        ),
        snapshot(
            5,
            [
                iface(
                    1,
                    speed_bps=Mbit,
                    in_octets=counters[1],
                    discontinuity_ticks=second_tick,
                )
            ],
            1300,
        ),
    ]


def test_recent_reset_before_the_first_poll_is_not_a_new_reset():
    # The interface restarted 30 s before the first sample.  The stamp moves
    # forward by less than the tolerance, so the window had no discontinuity.
    device = _single_device(build_report(_two_polls(10_000, 13_000)))
    assert device.summaries[1].inbound.minimum_bytes == 1000
    assert device.summaries[1].inbound.reset_windows == 0


def test_reset_inside_the_window_is_detected_however_recent():
    # The stamp jumps from 5 s to 20 minutes into the window.
    device = _single_device(build_report(_two_polls(500, 120_000)))
    assert device.summaries[1].inbound.reset_windows == 1


def test_discontinuity_stamp_wrapping_with_the_clock_is_not_a_reset():
    # 2**32-10 -> 290 is the TimeTicks epoch rolling over during the window.
    ticks_before = 2**32 - 10
    ticks_after = 290
    device = _single_device(build_report(_two_polls(ticks_before, ticks_after)))
    assert device.summaries[1].inbound.reset_windows == 0
    assert device.summaries[1].inbound.minimum_bytes == 1000


def test_zero_discontinuity_stamp_proves_nothing():
    # Agents that do not implement the object return 0 forever; that must not
    # be read as "no reset happened", so a decrease falls back to the rate.
    snapshots = _two_polls(0, 0, counters=(5_000_000, 10))
    device = _single_device(build_report(snapshots))
    assert device.summaries[1].inbound.reset_windows == 1


def test_discontinuity_stamp_absent_falls_back_to_rate():
    snapshots = [
        snapshot(0, [iface(1, speed_bps=Mbit, in_octets=5_000_000)]),
        snapshot(5, [iface(1, speed_bps=Mbit, in_octets=10)], 1300),
    ]
    device = _single_device(build_report(snapshots))
    assert device.summaries[1].inbound.reset_windows == 1


def test_restart_window_never_bridges_even_when_counters_look_continuous():
    # The raw values rise 1000 -> 2000 -> 3000 across a reboot, so a naive
    # subtraction would claim 2000 bytes over the restart.  The uptime drop
    # proves re-initialisation, so that window must be a reset.
    snapshots = [
        snapshot(0, [iface(1, speed_bps=Gbit, in_octets=1000)], uptime_seconds=50_000),
        snapshot(5, [iface(1, speed_bps=Gbit, in_octets=2000)], uptime_seconds=50_300),
        snapshot(10, [iface(1, speed_bps=Gbit, in_octets=3000)], uptime_seconds=10),
    ]
    device = _single_device(build_report(snapshots))
    restart_window = device.generations[1].summaries[1].windows[0]

    assert restart_window.inbound.status == STATUS_RESET
    assert restart_window.inbound.minimum == 0
    assert restart_window.inbound.maximum is not None
