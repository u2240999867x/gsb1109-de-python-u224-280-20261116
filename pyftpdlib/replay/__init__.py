# Copyright (C) 2007 Giampaolo Rodola' <g.rodola@gmail.com>.
# Use of this source code is governed by MIT license that can be
# found in the LICENSE file.

"""Offline replay of FTP control/data captures.

Public entry points:

>>> from pyftpdlib.replay import analyze_events, load_events
>>> events = load_events("trace.jsonl")
>>> result = analyze_events(events, expected_size=1024)
>>> result.primary.state
'complete'
"""

from .analyzer import (
    Analysis,  # noqa: F401
    Analyzer,  # noqa: F401
    TransferAnalysis,  # noqa: F401
    analyze_events,  # noqa: F401
)
from .capture import (
    CaptureReplay,  # noqa: F401
    build_events,  # noqa: F401
    replay_capture,  # noqa: F401
)
from .events import (
    Event,  # noqa: F401
    EventError,  # noqa: F401
    events_from_records,  # noqa: F401
    iter_events,  # noqa: F401
    load_events,  # noqa: F401
)
from .pcap import (
    CaptureError,  # noqa: F401
    read_capture,  # noqa: F401
    read_segments,  # noqa: F401
)

__all__ = [
    "Analysis",
    "Analyzer",
    "CaptureError",
    "CaptureReplay",
    "Event",
    "EventError",
    "TransferAnalysis",
    "analyze_events",
    "build_events",
    "events_from_records",
    "iter_events",
    "load_events",
    "read_capture",
    "read_segments",
    "replay_capture",
]
