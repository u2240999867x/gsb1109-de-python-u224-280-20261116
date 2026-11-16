"""RPA D-Bus recording analysis helpers.

This package hosts offline analysis tools for D-Bus call recordings made by
the RPA backend. It is deliberately independent of the message bus runtime and
only operates on plain recording data structures.
"""

from dbus_next.rpa.deadlock import (
    BlockingGroup,
    CallRecord,
    CallType,
    EventRecord,
    Recording,
    SchedulingModel,
    ServiceModel,
    TimeSlice,
    Verdict,
    WaitEdge,
    analyze_recording,
    build_slices,
    summarize,
)

__all__ = [
    'BlockingGroup',
    'CallRecord',
    'CallType',
    'EventRecord',
    'Recording',
    'SchedulingModel',
    'ServiceModel',
    'TimeSlice',
    'Verdict',
    'WaitEdge',
    'analyze_recording',
    'build_slices',
    'summarize',
]
