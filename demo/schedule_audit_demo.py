#!/usr/bin/env python3
# Copyright (C) 2026.
"""Audit a recorded trace of control events, data IO and throttle waits.

The scenario mirrors a throttled FTP transfer recording:

- two materials (uploads) share one worker resource of capacity 1;
- clock readings carry an explicit uncertainty;
- one wait is a pure, removable throttle wait;
- one wait has throttle and network reasons at the same time, so it is
  only shortened up to the share the other reasons may still need;
- each IO event keeps wire, payload and business-retransmit bytes in
  separate accounts;
- deadlines let the audit decide which timeouts can be attributed to
  throttling at all.

Run with:  python demo/schedule_audit_demo.py
"""

import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from schedule_audit import (
    EVENT_IO,
    EVENT_WAIT,
    EVENT_WORK,
    Event,
    Material,
    REASON_NETWORK,
    REASON_THROTTLE,
    Resource,
    Trace,
    Transfer,
    audit_trace,
    report_as_dict,
    report_as_text,
)


def build_trace():
    worker = Resource("worker", capacity=1)

    material_a = Material(
        name="upload-A",
        deadline=8.5,
        events=[
            Event(
                "a-control",
                EVENT_WORK,
                observed_start=0.0,
                observed_end=2.0,
                duration_min=2.0,
                duration_max=2.0,
                resources=("worker",),
            ),
            Event(
                "a-throttle-wait",
                EVENT_WAIT,
                observed_start=2.0,
                observed_end=5.0,
                duration_min=3.0,
                duration_max=3.0,
                reasons=(REASON_THROTTLE,),
                removable_throttle=True,
            ),
            Event(
                "a-data-write",
                EVENT_IO,
                observed_start=5.0,
                observed_end=9.0,
                duration_min=4.0,
                duration_max=4.0,
                resources=("worker",),
                transfer=Transfer(
                    wire_bytes=100,
                    payload_bytes=90,
                    retransmit_bytes=5,
                ),
            ),
        ],
    )

    material_b = Material(
        name="upload-B",
        deadline=20.0,
        events=[
            Event(
                "b-control",
                EVENT_WORK,
                observed_start=9.0,
                observed_end=11.0,
                duration_min=2.0,
                duration_max=2.0,
                resources=("worker",),
            ),
            Event(
                "b-mixed-wait",
                EVENT_WAIT,
                observed_start=11.0,
                observed_end=14.0,
                duration_min=3.0,
                duration_max=3.0,
                reasons=(REASON_THROTTLE, REASON_NETWORK),
                removable_throttle=True,
            ),
            Event(
                "b-data-read",
                EVENT_IO,
                observed_start=14.0,
                observed_end=17.0,
                duration_min=3.0,
                duration_max=3.0,
                resources=("worker",),
                transfer=Transfer(
                    wire_bytes=60,
                    payload_bytes=60,
                    retransmit_bytes=0,
                ),
            ),
        ],
    )

    return Trace(
        materials=[material_a, material_b],
        resources={"worker": worker},
        clock_skew=0.1,
        removed_waits=frozenset(
            ("a-throttle-wait", "b-mixed-wait")
        ),
    )


def main():
    trace = build_trace()
    result = audit_trace(trace)
    print(report_as_text(result))
    print()
    print("JSON summary:")
    print(json.dumps(report_as_dict(result), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
