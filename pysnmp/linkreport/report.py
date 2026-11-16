#
# This file is part of pysnmp software.
#
# Copyright (c) 2005-2020, Ilya Etingof <etingof@gmail.com>
# License: https://www.pysnmp.com/pysnmp/license.html
#
"""Turn a day of poll snapshots into a link report.

The report answers, per device and per interface: how many octets certainly
crossed the link, how much slack the evidence leaves, and whether each
anomaly was a counter wrap or a counter reset.

Grouping happens in two dimensions.

*By device generation.*  ``sysUpTime`` resets on reboot, so a drop in uptime
that is too large to be a 32-bit TimeTicks wrap starts a new generation.  A
generation boundary is a discontinuity for every counter on the device; no
window ever spans it.

*By interface, then by sampling window.*  Every interface is compared only
against its own previous reading.  An interface that disappears mid-day, or
whose counters reset, contributes a reset boundary to itself alone; other
interfaces that were sampled normally keep their continuous arithmetic.  This
is why windows live under the interface rather than under the device.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Iterable, Sequence

from pysnmp.linkreport.counters import (
    capacity_bytes,
    compare_counters,
    cross_check_32bit,
    format_bytes,
)
from pysnmp.linkreport.model import (
    COUNTER32_MODULUS,
    UPTIME_MODULUS,
    STATUS_ABSENT,
    STATUS_RESET,
    STATUS_UNAVAILABLE,
    DailyReport,
    DeviceReport,
    DirectionSummary,
    Event,
    Generation,
    InterfaceSample,
    InterfaceSummary,
    ReportConfig,
    Snapshot,
    WindowResult,
)

__all__ = ["build_report"]


def _seconds_between(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds()


def _uptime_seconds(ticks: int | None) -> float | None:
    if ticks is None:
        return None
    return ticks / 100.0


def _uptime_went_backwards(
    previous_ticks: int | None,
    current_ticks: int | None,
    elapsed_seconds: float,
    config: ReportConfig,
) -> bool:
    """Decide whether uptime dropped because of a reboot, not a TimeTicks wrap.

    A 32-bit TimeTicks counter wraps after ~497 days, far longer than a daily
    sampling window.  A decrease is therefore a reboot unless the window is
    long enough that the wrap is plausible; the tolerance protects against
    clock jitter and agents that round their uptime.
    """
    if previous_ticks is None or current_ticks is None:
        return False
    if current_ticks >= previous_ticks:
        return False

    elapsed_ticks = max(elapsed_seconds, 0.0) * 100.0
    tolerance = max(
        config.uptime_tolerance_seconds * 100.0,
        elapsed_ticks * config.uptime_tolerance_ratio,
    )
    # A wrap needs the counter to pass 2**32, i.e. the drop plus the elapsed
    # time must reach the modulus.
    return (previous_ticks + elapsed_ticks + tolerance) < UPTIME_MODULUS


def _split_generations(
    snapshots: Sequence[Snapshot], config: ReportConfig
) -> list[tuple[str, list[Snapshot], float | None]]:
    """Split one device's ordered snapshots into uptime-continuous runs.

    Returns ``(reason, snapshots, gap)`` triples.  ``gap`` is the wall-clock
    distance from the previous run's last sample to this run's first sample;
    it is ``None`` for the first run.
    """
    runs: list[tuple[str, list[Snapshot], float | None]] = []
    current: list[Snapshot] = []
    reason = "start of reporting period"
    gap: float | None = None

    for snapshot in snapshots:
        if not current:
            current = [snapshot]
            continue

        previous = current[-1]
        elapsed = _seconds_between(previous.sampled_at, snapshot.sampled_at)
        if _uptime_went_backwards(
            previous.sys_uptime_ticks, snapshot.sys_uptime_ticks, elapsed, config
        ):
            runs.append((reason, current, gap))
            gap = elapsed
            reason = "sysUpTime decreased (device restart)"
            current = [snapshot]
            continue

        current.append(snapshot)

    if current:
        runs.append((reason, current, gap))
    return runs


def _counter_pair(
    previous: InterfaceSample,
    current: InterfaceSample,
    direction: str,
) -> tuple[str, int | None, int | None]:
    """Choose the 64-bit counter for one direction when both ends have it.

    Selection is per direction: a device that publishes ``ifHCInOctets`` but
    not ``ifHCOutOctets`` still gets the wrap-free counter inbound.
    """
    if direction == "in":
        old_high, new_high = previous.hc_in_octets, current.hc_in_octets
        old_low, new_low = previous.in_octets, current.in_octets
    else:
        old_high, new_high = previous.hc_out_octets, current.hc_out_octets
        old_low, new_low = previous.out_octets, current.out_octets

    if old_high is not None and new_high is not None:
        return "ifHC*", old_high, new_high
    return "if*", old_low, new_low


def _discontinuity_changed(
    previous: InterfaceSample,
    current: InterfaceSample,
    elapsed_seconds: float,
    config: ReportConfig,
) -> bool | None:
    """Compare ``ifCounterDiscontinuityTime`` at both ends of a window.

    ``True`` proves a reset, ``False`` proves no reset, and ``None`` means the
    agent did not supply the object.  The object holds the ``sysUpTime`` at
    the last re-initialisation, so it is constant between discontinuities;
    it also wraps with the TimeTicks clock, which must not be mistaken for a
    reset.
    """
    old = previous.discontinuity_ticks
    new = current.discontinuity_ticks
    if old is None or new is None:
        return None
    if not 0 <= old < UPTIME_MODULUS or not 0 <= new < UPTIME_MODULUS:
        return None
    if old == 0 and new == 0:
        # Zero is the value an agent reports between boot and its first
        # counter discontinuity -- but it is also what agents that simply do
        # not implement the object return.  The two are indistinguishable, so
        # this is not proof that no reset happened.
        return None
    if new == old:
        return False

    elapsed_ticks = max(elapsed_seconds, 0.0) * 100.0
    tolerance = max(
        config.uptime_tolerance_seconds * 100.0,
        elapsed_ticks * config.uptime_tolerance_ratio,
    )

    if old <= new:
        if new - old <= tolerance:
            # The stamp moved forward by no more than clock jitter, or the
            # interface was re-initialised moments before the first read.
            # Either way it does not mark a discontinuity inside the window.
            return False
        return True

    # The stamp went backwards.  That is the TimeTicks clock wrapping, not a
    # reset: the stored value is an absolute instant, so a smaller one can
    # only come from the epoch rolling over.
    if UPTIME_MODULUS - old + new <= max(tolerance, elapsed_ticks * 2):
        return False
    # A large backwards jump without a wrap is contradictory evidence.
    return None


def _effective_capacity(
    speed_bps: int | None, elapsed_seconds: float, config: ReportConfig
) -> int | None:
    capacity = capacity_bytes(speed_bps, elapsed_seconds, config.rate_tolerance)
    if capacity is None and config.max_rate_bps:
        capacity = capacity_bytes(
            config.max_rate_bps, elapsed_seconds, config.rate_tolerance
        )
    return capacity


def _build_direction(
    *,
    direction: str,
    previous: InterfaceSample,
    current: InterfaceSample,
    elapsed_seconds: float,
    capacity: int | None,
    discontinuity_changed: bool | None,
    config: ReportConfig,
):
    source, previous_value, current_value = _counter_pair(
        previous, current, direction
    )
    return compare_counters(
        direction=direction,
        previous=previous_value,
        current=current_value,
        bits=64 if source == "ifHC*" else 32,
        elapsed_seconds=elapsed_seconds,
        capacity=capacity,
        source=source,
        discontinuity_changed=discontinuity_changed,
        counter64_wrap_min_seconds=config.counter64_wrap_min_seconds,
    )


def _merge_speed(
    previous: InterfaceSample, current: InterfaceSample
) -> int | None:
    for candidate in (current.speed_bps, previous.speed_bps):
        if candidate:
            return candidate
    return None


def _note_counter_disagreement(
    window: WindowResult, previous: InterfaceSample, current: InterfaceSample
) -> None:
    """Flag a 32-bit counter that contradicts the trusted 64-bit one."""
    for direction, delta, old_low, new_low in (
        ("in", window.inbound, previous.in_octets, current.in_octets),
        ("out", window.outbound, previous.out_octets, current.out_octets),
    ):
        if delta.source != "ifHC*":
            continue
        agreed = cross_check_32bit(
            previous32=old_low, current32=new_low, delta=delta
        )
        if agreed is not False:
            continue
        observed = (new_low - old_low) % COUNTER32_MODULUS
        window.events.append(
            Event(
                kind="counter_mismatch",
                at=window.end,
                detail=(
                    f"if{'In' if direction == 'in' else 'Out'}Octets moved "
                    f"{observed} but the high-capacity counter moved "
                    f"{delta.minimum} (mod 2**32); the 64-bit value is used"
                ),
                if_index=window.if_index,
                direction=direction,
                severity="warning",
            )
        )


def _build_window(
    *,
    index: int,
    if_index: int,
    previous: InterfaceSample,
    current: InterfaceSample,
    start: datetime,
    end: datetime,
    elapsed_seconds: float,
    config: ReportConfig,
    force_reset: bool,
) -> WindowResult:
    speed = _merge_speed(previous, current)
    capacity = _effective_capacity(speed, elapsed_seconds, config)
    discontinuity = _discontinuity_changed(
        previous, current, elapsed_seconds, config
    )

    window = WindowResult(
        index=index,
        if_index=if_index,
        start=start,
        end=end,
        elapsed_seconds=elapsed_seconds,
        description=current.description or previous.description,
        speed_bps=speed,
        inbound=_build_direction(
            direction="in",
            previous=previous,
            current=current,
            elapsed_seconds=elapsed_seconds,
            capacity=capacity,
            discontinuity_changed=discontinuity,
            config=config,
        ),
        outbound=_build_direction(
            direction="out",
            previous=previous,
            current=current,
            elapsed_seconds=elapsed_seconds,
            capacity=capacity,
            discontinuity_changed=discontinuity,
            config=config,
        ),
    )

    if force_reset:
        # The window straddles a device restart, so every counter was
        # re-initialised even if the values happen to look continuous.
        for delta in (window.inbound, window.outbound):
            delta.status = STATUS_RESET
            delta.minimum = 0
            delta.maximum = delta.capacity_bytes
            delta.wrap_count_min = 0
            delta.wrap_count_max = 0
            delta.note = "device restarted between samples"
        window.events.append(
            Event(
                kind="device_restart",
                at=end,
                detail="counters re-initialised by device restart",
                if_index=if_index,
                severity="warning",
            )
        )
    else:
        _note_counter_disagreement(window, previous, current)

    return window


def _window_events(window: WindowResult, restart_boundary: bool = False) -> None:
    """Record wrap and reset events for both directions of a window."""
    for direction, delta in (("in", window.inbound), ("out", window.outbound)):
        if delta.status == STATUS_RESET:
            if not restart_boundary:
                window.events.append(
                    Event(
                        kind="reset",
                        at=window.end,
                        detail=delta.note or "counter re-initialised",
                        if_index=window.if_index,
                        direction=direction,
                        severity="warning",
                    )
                )
            continue

        if delta.wrap_count_max is None:
            window.events.append(
                Event(
                    kind="ambiguous_wraps",
                    at=window.end,
                    detail=(
                        f"at least {delta.wrap_count_min} whole counter cycle(s); "
                        "link rate unknown, so the count cannot be bounded"
                    ),
                    if_index=window.if_index,
                    direction=direction,
                    wraps=delta.wrap_count_min,
                    severity="warning",
                )
            )
        elif delta.wrap_count_max > delta.wrap_count_min:
            lost = (delta.wrap_count_max - delta.wrap_count_min) * (1 << delta.bits)
            window.events.append(
                Event(
                    kind="ambiguous_wraps",
                    at=window.end,
                    detail=(
                        f"{delta.wrap_count_min}..{delta.wrap_count_max} whole "
                        f"cycles possible; up to {format_bytes(lost)} unresolvable"
                    ),
                    if_index=window.if_index,
                    direction=direction,
                    wraps=delta.wrap_count_max,
                    lost_max_bytes=lost,
                    severity="warning",
                )
            )
        elif delta.wrap_count_min:
            window.events.append(
                Event(
                    kind="wrap",
                    at=window.end,
                    detail=delta.note
                    or f"{delta.wrap_count_min} whole counter cycle(s) unwrapped",
                    if_index=window.if_index,
                    direction=direction,
                    wraps=delta.wrap_count_min,
                    severity="info",
                )
            )


def _dedupe_events(events: Iterable[Event]) -> list[Event]:
    """Keep the first occurrence of each logically identical event."""
    seen = set()
    unique = []
    for event in events:
        key = (
            event.at,
            event.kind,
            event.if_index,
            event.direction,
            event.detail,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(event)
    return unique


def _accumulate(summary: DirectionSummary, delta, window: WindowResult) -> None:
    summary.windows += 1
    if delta.source:
        summary.sources.add(delta.source)

    if delta.status == STATUS_RESET:
        summary.reset_windows += 1
        if delta.maximum is None or summary.reset_lost_max_bytes is None:
            summary.reset_lost_max_bytes = None
        else:
            summary.reset_lost_max_bytes += delta.maximum
        return

    if delta.status == STATUS_ABSENT:
        summary.absent_windows += 1
        return

    if delta.status == STATUS_UNAVAILABLE:
        summary.unavailable_windows += 1
        return

    summary.covered_seconds += window.elapsed_seconds
    summary.minimum_bytes += delta.minimum
    if delta.maximum is None or summary.maximum_bytes is None:
        summary.maximum_bytes = None
    else:
        summary.maximum_bytes += delta.maximum

    summary.wraps_confirmed += delta.wrap_count_min
    if delta.wrap_count_max is None or summary.wraps_possible_extra is None:
        summary.wraps_possible_extra = None
    else:
        summary.wraps_possible_extra += delta.wrap_count_max - delta.wrap_count_min


def _summarise_interface(
    if_index: int, windows: list[WindowResult]
) -> InterfaceSummary:
    summary = InterfaceSummary(if_index=if_index)
    for window in windows:
        summary.windows.append(window)
        summary.events.extend(window.events)
        if summary.first_seen is None or window.start < summary.first_seen:
            summary.first_seen = window.start
        if summary.last_seen is None or window.end > summary.last_seen:
            summary.last_seen = window.end
        if window.description:
            summary.description = window.description
        if window.speed_bps:
            summary.speed_bps = window.speed_bps
        _accumulate(summary.inbound, window.inbound, window)
        _accumulate(summary.outbound, window.outbound, window)

    if summary.description is None:
        summary.description = f"ifIndex {if_index}"
    return summary


def _clone_direction(source: DirectionSummary) -> DirectionSummary:
    clone = DirectionSummary(
        minimum_bytes=source.minimum_bytes,
        maximum_bytes=source.maximum_bytes,
        covered_seconds=source.covered_seconds,
        windows=source.windows,
        reset_windows=source.reset_windows,
        absent_windows=source.absent_windows,
        unavailable_windows=source.unavailable_windows,
        wraps_confirmed=source.wraps_confirmed,
        wraps_possible_extra=source.wraps_possible_extra,
        reset_lost_max_bytes=source.reset_lost_max_bytes,
    )
    clone.sources = set(source.sources)
    return clone


def _merge_direction(target: DirectionSummary, source: DirectionSummary) -> None:
    target.minimum_bytes += source.minimum_bytes
    if source.maximum_bytes is None or target.maximum_bytes is None:
        target.maximum_bytes = None
    else:
        target.maximum_bytes += source.maximum_bytes
    target.covered_seconds += source.covered_seconds
    target.windows += source.windows
    target.reset_windows += source.reset_windows
    target.absent_windows += source.absent_windows
    target.unavailable_windows += source.unavailable_windows
    target.wraps_confirmed += source.wraps_confirmed
    if source.wraps_possible_extra is None or target.wraps_possible_extra is None:
        target.wraps_possible_extra = None
    else:
        target.wraps_possible_extra += source.wraps_possible_extra
    if (
        source.reset_lost_max_bytes is None
        or target.reset_lost_max_bytes is None
    ):
        target.reset_lost_max_bytes = None
    else:
        target.reset_lost_max_bytes += source.reset_lost_max_bytes
    target.sources.update(source.sources)


def _merge_summaries(
    summaries: Iterable[InterfaceSummary],
) -> dict[int, InterfaceSummary]:
    """Combine the per-generation summaries of one interface."""
    merged: dict[int, InterfaceSummary] = {}
    for summary in summaries:
        target = merged.get(summary.if_index)
        if target is None:
            merged[summary.if_index] = InterfaceSummary(
                if_index=summary.if_index,
                description=summary.description,
                speed_bps=summary.speed_bps,
                inbound=_clone_direction(summary.inbound),
                outbound=_clone_direction(summary.outbound),
                first_seen=summary.first_seen,
                last_seen=summary.last_seen,
                windows=list(summary.windows),
                events=list(summary.events),
            )
            continue

        target.description = summary.description or target.description
        target.speed_bps = summary.speed_bps or target.speed_bps
        if summary.first_seen is not None and (
            target.first_seen is None or summary.first_seen < target.first_seen
        ):
            target.first_seen = summary.first_seen
        if summary.last_seen is not None and (
            target.last_seen is None or summary.last_seen > target.last_seen
        ):
            target.last_seen = summary.last_seen
        target.windows.extend(summary.windows)
        target.events.extend(summary.events)
        _merge_direction(target.inbound, summary.inbound)
        _merge_direction(target.outbound, summary.outbound)
    return merged


def _build_generation_windows(
    run: Sequence[Snapshot],
    previous_sample: Snapshot | None,
    config: ReportConfig,
) -> tuple[dict[int, list[WindowResult]], dict[int, datetime]]:
    """Build every interface's windows for one generation.

    Each interface is walked over *its own* sightings, so a poll that omitted
    one interface only lengthens that interface's window; it does not skip a
    window for anybody else.

    The optional *previous_sample* is the last poll before this generation
    began.  When present it supplies a leading reset window, so traffic lost
    at the restart is bounded rather than silently dropped.

    Returns the windows per interface and the first timestamp each interface
    was seen at (needed to report interfaces that appear mid-period).
    """
    per_interface: dict[int, list[WindowResult]] = defaultdict(list)
    first_seen: dict[int, datetime] = {}

    sightings: dict[int, list[tuple[Snapshot, InterfaceSample]]] = defaultdict(list)
    for snapshot in run:
        for if_index, sample in snapshot.interfaces.items():
            sightings[if_index].append((snapshot, sample))
            first_seen[if_index] = snapshot.sampled_at

    restart_boundary = previous_sample is not None
    for if_index, seen in sorted(sightings.items()):
        baseline: tuple[Snapshot, InterfaceSample] | None = None
        if restart_boundary:
            before = previous_sample.interfaces.get(if_index)
            if before is not None:
                baseline = (previous_sample, before)

        # Only the first window of this generation can straddle the restart.
        force_reset = baseline is not None
        for snapshot, sample in seen:
            if baseline is None:
                baseline = (snapshot, sample)
                continue

            previous_snapshot, previous_sample_data = baseline
            elapsed = _seconds_between(
                previous_snapshot.sampled_at, snapshot.sampled_at
            )
            window = _build_window(
                index=len(per_interface[if_index]) + 1,
                if_index=if_index,
                previous=previous_sample_data,
                current=sample,
                start=previous_snapshot.sampled_at,
                end=snapshot.sampled_at,
                elapsed_seconds=elapsed,
                config=config,
                # The first window after a restart is a reset boundary for
                # every interface, because the device re-initialised them all.
                force_reset=force_reset,
            )
            _window_events(window, restart_boundary=force_reset)
            per_interface[if_index].append(window)
            baseline = (snapshot, sample)
            force_reset = False

    return per_interface, first_seen


def _build_device(
    device: str, snapshots: Sequence[Snapshot], config: ReportConfig
) -> DeviceReport:
    ordered = sorted(snapshots, key=lambda s: s.sampled_at)

    report = DeviceReport(
        device=device,
        period_start=ordered[0].sampled_at,
        period_end=ordered[-1].sampled_at,
        sample_count=len(ordered),
    )

    # Two polls at the same instant give a zero-length window, which cannot
    # say anything about a counter.  Collapse them with an explicit note
    # instead of silently emitting an "unavailable" window.
    collapsed: list[Snapshot] = []
    for snapshot in ordered:
        if collapsed and collapsed[-1].sampled_at == snapshot.sampled_at:
            report.notes.append(
                f"duplicate sample at {snapshot.sampled_at.isoformat()}; "
                "the later one was used"
            )
            collapsed[-1] = snapshot
        else:
            collapsed.append(snapshot)
    ordered = collapsed

    generated = _split_generations(ordered, config)

    if any(s.sys_uptime_ticks is None for s in ordered):
        report.notes.append(
            "sysUpTime is missing from at least one sample; a restart that "
            "happened between samples may go undetected"
        )

    previous_run: list[Snapshot] | None = None
    for gen_index, (reason, run, gap) in enumerate(generated):
        generation = Generation(
            index=gen_index,
            started_at=run[0].sampled_at,
            ended_at=run[-1].sampled_at,
            start_reason=reason,
            sample_count=len(run),
            restart_gap_seconds=gap,
            uptime_start_seconds=_uptime_seconds(run[0].sys_uptime_ticks),
            uptime_end_seconds=_uptime_seconds(run[-1].sys_uptime_ticks),
        )
        if gen_index:
            generation.events.append(
                Event(
                    kind="generation_boundary",
                    at=run[0].sampled_at,
                    detail=(
                        f"{reason}"
                        + (
                            f"; samples {gap:.1f}s apart"
                            if gap is not None
                            else ""
                        )
                    ),
                    severity="warning",
                )
            )

        per_interface, first_seen = _build_generation_windows(
            run,
            previous_run[-1] if previous_run else None,
            config,
        )
        for if_index, windows in per_interface.items():
            generation.summaries[if_index] = _summarise_interface(if_index, windows)
            for window in windows:
                generation.events.extend(window.events)
        for if_index, seen_at in first_seen.items():
            generation.interface_first_seen.setdefault(if_index, seen_at)
        generation.events = _dedupe_events(generation.events)

        report.generations.append(generation)
        previous_run = run

    report.summaries = _merge_summaries(
        summary
        for generation in report.generations
        for summary in generation.summaries.values()
    )

    # An interface that only appears part-way through has no baseline before
    # its first reading, so its first window is legitimately missing.  Say so
    # rather than letting the silence look like an error.
    for generation in report.generations:
        for if_index, seen_at in generation.interface_first_seen.items():
            if if_index in generation.summaries:
                continue
            report.notes.append(
                f"ifIndex {if_index}: first seen at "
                f"{seen_at.strftime('%H:%M:%S')}; no baseline in generation "
                f"{generation.index + 1}, so no increment can be claimed"
            )

    for summary in report.summaries.values():
        missing = summary.inbound.absent_windows + summary.outbound.absent_windows
        if missing:
            report.notes.append(
                f"ifIndex {summary.if_index}: counter missing at one end of "
                f"{missing} direction-window(s)"
            )

    return report


def build_report(
    snapshots: Iterable[Snapshot], config: ReportConfig | None = None
) -> DailyReport:
    """Build a :class:`DailyReport` from *snapshots*.

    Snapshots may arrive in any order; they are grouped by device and sorted
    by timestamp before analysis.
    """
    config = config or ReportConfig()
    by_device: dict[str, list[Snapshot]] = defaultdict(list)
    for snapshot in snapshots:
        by_device[snapshot.device].append(snapshot)

    report = DailyReport(config=config)
    for device in sorted(by_device):
        report.devices.append(_build_device(device, by_device[device], config))
    return report
