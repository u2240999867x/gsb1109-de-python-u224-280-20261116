"""End-to-end demonstration of the wave analysis on mixed-precision data.

Run with::

    python -m wave_analysis.demo
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .core import (
    Announcement,
    Interval,
    analyze,
    ambiguous_day_record,
    day_record,
    precise_record,
)

UTC = timezone.utc


def build_demo_records() -> list[Announcement]:
    return [
        precise_record(
            "a",
            "【公告A】2026-01-05 09:00:00 系统升级完成",
            datetime(2026, 1, 5, 9, 0, 0, tzinfo=UTC),
            tz_note="second precision, UTC",
        ),
        day_record(
            "b",
            "【公告B】2026-01-05 新版规则上线（仅标注日期）",
            datetime(2026, 1, 5, tzinfo=UTC),
        ),
        ambiguous_day_record(
            "c",
            "【公告C】2026-01-05 海外节点切换（时区未注明）",
            datetime(2026, 1, 5, tzinfo=UTC),
            candidate_tzs=["UTC", "Asia/Shanghai"],
        ),
        precise_record(
            "d",
            "【公告D】2026-01-09 10:00:00 例行维护",
            datetime(2026, 1, 9, 10, 0, 0, tzinfo=UTC),
            tz_note="second precision, UTC",
        ),
        Announcement(
            "e",
            "【公告E】运维窗口 2026-01-05 18:00 至 19:00",
            [
                Interval(
                    datetime(2026, 1, 5, 18, 0, tzinfo=UTC),
                    datetime(2026, 1, 5, 19, 0, tzinfo=UTC),
                )
            ],
            precision="minute-window",
            tz_note="exact zone, imprecise minute",
        ),
    ]


def main() -> None:
    records = build_demo_records()
    result = analyze(
        records,
        threshold=timedelta(minutes=30),
        known_order=[("a", "b")],
    )
    print(result.report())
    print()
    print("Summary:")
    print(
        f"  waves in [{result.min_waves}, {result.max_waves}], "
        f"{result.feasible_orders} distinct feasible order(s)"
    )
    print(f"  certain same-wave pairs: {result.certain_together}")
    print(f"  certain split pairs:     {result.certain_separated}")
    print(f"  ambiguous pairs:         {result.ambiguous_pairs}")


if __name__ == "__main__":
    main()
