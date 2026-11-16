#
# This file is part of pysnmp software.
#
# Copyright (c) 2005-2020, Ilya Etingof <etingof@gmail.com>
# License: https://www.pysnmp.com/pysnmp/license.html
#
"""Daily link reports built from SNMP counter snapshots.

A day of polls is read from JSON, JSONL or CSV, split into device
generations by ``sysUpTime``, and compared interface by interface using the
32-bit and 64-bit octet counters together with
``ifCounterDiscontinuityTime``.  Every delta is reported as the increment
that is certain, with an explicit upper bound when wraps cannot be counted
exactly, and with wraps and resets explained separately.

The package performs no I/O on its own: feed it snapshots you already
collected, or use :mod:`pysnmp.linkreport.cli` to read an export file.

Example
-------
>>> from pysnmp.linkreport import ReportConfig, build_report, render_text
>>> report = build_report(snapshots, ReportConfig(max_rate_bps=10_000_000_000))
>>> print(render_text(report))

See :mod:`pysnmp.linkreport.counters` for the arithmetic itself.
"""

from pysnmp.linkreport.counters import (
    capacity_bytes,
    compare_counters,
    cross_check_32bit,
    describe_delta,
)
from pysnmp.linkreport.model import (
    COUNTER32_MODULUS,
    COUNTER64_MODULUS,
    UPTIME_MODULUS,
    DailyReport,
    DeltaResult,
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
from pysnmp.linkreport.reader import SnapshotFormatError, load_snapshots
from pysnmp.linkreport.render import render_json, render_text, report_to_dict
from pysnmp.linkreport.report import build_report

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
    "SnapshotFormatError",
    "WindowResult",
    "build_report",
    "capacity_bytes",
    "compare_counters",
    "cross_check_32bit",
    "describe_delta",
    "load_snapshots",
    "render_json",
    "render_text",
    "report_to_dict",
]
