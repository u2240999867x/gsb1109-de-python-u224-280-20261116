#
# This file is part of pysnmp software.
#
# Copyright (c) 2005-2020, Ilya Etingof <etingof@gmail.com>
# License: https://www.pysnmp.com/pysnmp/license.html
#
"""Data model shared by the link report builder and its renderers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

__all__ = [
    "COUNTER32_MODULUS",
    "COUNTER64_MODULUS",
    "UPTIME_MODULUS",
    "DailyReport",
    "DeltaResult",
    "DeviceReport",
    "DirectionSummary",
    "Event",
    "Generation",
    "InterfaceSample",
    "InterfaceSummary",
    "ReportConfig",
    "Snapshot",
    "WindowResult",
]

#: Largest value a Counter32 may hold before wrapping to zero (RFC 2578).
COUNTER32_MODULUS = 1 << 32

#: Largest value a Counter64 may hold before wrapping to zero (RFC 2578).
COUNTER64_MODULUS = 1 << 64

#: ``sysUpTime``/``ifCounterDiscontinuityTime`` are TimeTicks, 1/100 s, 32 bit.
UPTIME_MODULUS = 1 << 32

# Delta statuses.  They are stable strings so that JSON consumers can rely
# on them.
STATUS_CONFIRMED = "confirmed"
STATUS_RANGE = "range"
STATUS_LOWER_BOUND = "lower_bound"
STATUS_RESET = "reset"
STATUS_ABSENT = "absent"
STATUS_UNAVAILABLE = "unavailable"

USABLE_STATUSES = (STATUS_CONFIRMED, STATUS_RANGE, STATUS_LOWER_BOUND)


@dataclass(frozen=True)
class InterfaceSample:
    """One interface as read in a single poll.

    Counter values are plain integers.  ``in_octets``/``out_octets`` are the
    32-bit counters (``ifInOctets``/``ifOutOctets``), the ``hc_*`` fields are
    the 64-bit counters (``ifHCInOctets``/``ifHCOutOctets``).  ``speed_bps``
    is already normalised from ``ifSpeed``/``ifHighSpeed``.
    """

    if_index: int
    description: str | None = None
    speed_bps: int | None = None
    in_octets: int | None = None
    out_octets: int | None = None
    hc_in_octets: int | None = None
    hc_out_octets: int | None = None
    discontinuity_ticks: int | None = None


@dataclass(frozen=True)
class Snapshot:
    """A poll of one device at one instant."""

    device: str
    sampled_at: datetime
    interfaces: dict[int, InterfaceSample]
    sys_uptime_ticks: int | None = None


@dataclass(frozen=True)
class ReportConfig:
    """Tuning knobs for :func:`pysnmp.linkreport.build_report`.

    ``max_rate_bps`` is a fallback ceiling used only for interfaces whose
    speed is unknown.  It bounds how many counter wraps can have happened in
    one sampling window, which is what turns an otherwise open-ended count
    into a numeric range.

    ``rate_tolerance`` allows the observed traffic to exceed the nominal link
    rate by this fraction.  It accounts for ifSpeed rounding and sampling
    jitter; it only widens the wrap search, never the confirmed delta.

    ``uptime_tolerance_seconds``/``uptime_tolerance_ratio`` decide whether a
    drop in ``sysUpTime`` is a TimeTicks wrap (same boot generation) or a
    device restart (new generation).

    ``counter64_wrap_min_seconds`` is the shortest window in which a 64-bit
    counter decrease may be believed to be a wrap rather than a reset when
    no link speed is known.  Wrapping 2**64 octets in less than a year needs
    a sustained rate above 4 Tbit/s, so a daily window cannot hide one.
    """

    max_rate_bps: int | None = None
    rate_tolerance: float = 0.10
    uptime_tolerance_seconds: float = 5.0
    uptime_tolerance_ratio: float = 0.10
    counter64_wrap_min_seconds: float = 365 * 86400.0


@dataclass
class DeltaResult:
    """Outcome of comparing one counter across one sampling window.

    ``minimum`` is the increment that is certain.  ``maximum`` is the largest
    increment still compatible with the counter values, the elapsed time and
    the link capacity; ``None`` means no upper bound could be established.
    ``wrap_count_min``/``wrap_count_max`` describe how many full counter
    cycles are hidden in the increment (``None`` max = unbounded).
    """

    direction: str
    source: str | None
    bits: int | None
    previous: int | None
    current: int | None
    elapsed_seconds: float
    minimum: int = 0
    maximum: int | None = 0
    status: str = STATUS_UNAVAILABLE
    wrap_count_min: int = 0
    wrap_count_max: int | None = 0
    capacity_bytes: int | None = None
    note: str = ""


@dataclass
class Event:
    """Something noteworthy found while comparing two samples."""

    kind: str
    at: datetime
    detail: str
    if_index: int | None = None
    direction: str | None = None
    wraps: int | None = None
    lost_max_bytes: int | None = None
    severity: str = "info"


@dataclass
class WindowResult:
    """One interface's sampling window between two consecutive polls.

    Windows are kept per interface, never per device, so that a reset or a
    missing row on one interface cannot invalidate the deltas of the others.
    """

    index: int
    if_index: int
    start: datetime
    end: datetime
    elapsed_seconds: float
    inbound: DeltaResult
    outbound: DeltaResult
    description: str | None = None
    speed_bps: int | None = None
    events: list[Event] = field(default_factory=list)


@dataclass
class DirectionSummary:
    """Aggregate of one counter direction over a set of windows."""

    minimum_bytes: int = 0
    maximum_bytes: int | None = 0
    covered_seconds: float = 0.0
    windows: int = 0
    reset_windows: int = 0
    absent_windows: int = 0
    unavailable_windows: int = 0
    wraps_confirmed: int = 0
    wraps_possible_extra: int | None = 0
    reset_lost_max_bytes: int | None = 0
    sources: set[str] = field(default_factory=set)

    @property
    def is_confirmed(self) -> bool:
        """True when the aggregate has a single possible value.

        A reset, a missing reading or an unusable window leaves traffic
        unaccounted for even if the bounded windows happen to sum to the same
        number, so none of those count as confirmed.
        """
        return (
            self.maximum_bytes is not None
            and self.maximum_bytes == self.minimum_bytes
            and not self.reset_windows
            and not self.absent_windows
            and not self.unavailable_windows
            and not self.wraps_possible_extra
        )

    @property
    def average_bps(self) -> float | None:
        """Certain average bit rate over the covered windows."""
        if not self.covered_seconds:
            return None
        return self.minimum_bytes * 8.0 / self.covered_seconds

    @property
    def maximum_average_bps(self) -> float | None:
        """Upper-bound average bit rate, or ``None`` when unbounded."""
        if self.maximum_bytes is None or not self.covered_seconds:
            return None
        return self.maximum_bytes * 8.0 / self.covered_seconds


@dataclass
class InterfaceSummary:
    """Per-interface roll-up, with the windows it was computed from."""

    if_index: int
    description: str | None = None
    speed_bps: int | None = None
    inbound: DirectionSummary = field(default_factory=DirectionSummary)
    outbound: DirectionSummary = field(default_factory=DirectionSummary)
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    windows: list[WindowResult] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)


@dataclass
class Generation:
    """One device boot generation: a span with a continuous ``sysUpTime``."""

    index: int
    started_at: datetime
    ended_at: datetime
    start_reason: str
    sample_count: int
    restart_gap_seconds: float | None = None
    uptime_start_seconds: float | None = None
    uptime_end_seconds: float | None = None
    summaries: dict[int, InterfaceSummary] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    interface_first_seen: dict[int, datetime] = field(default_factory=dict)

    @property
    def window_count(self) -> int:
        """Number of sampling windows taken in this generation."""
        if not self.summaries:
            return 0
        return max(len(s.windows) for s in self.summaries.values())


@dataclass
class DeviceReport:
    """Everything the report knows about one device.

    ``summaries`` aggregates every generation, which is what "the whole day"
    means for a device that rebooted in the middle of it.  Note that when a
    device restarts, the interface counters reset too, so the cross-generation
    aggregate carries a reset boundary rather than a false continuity.
    """

    device: str
    period_start: datetime
    period_end: datetime
    sample_count: int
    generations: list[Generation] = field(default_factory=list)
    summaries: dict[int, InterfaceSummary] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass
class DailyReport:
    """Top-level report for one input file."""

    devices: list[DeviceReport] = field(default_factory=list)
    config: ReportConfig = field(default_factory=ReportConfig)

    @property
    def period_start(self) -> datetime | None:
        """Earliest sample timestamp across all devices."""
        starts = [d.period_start for d in self.devices]
        return min(starts) if starts else None

    @property
    def period_end(self) -> datetime | None:
        """Latest sample timestamp across all devices."""
        ends = [d.period_end for d in self.devices]
        return max(ends) if ends else None
