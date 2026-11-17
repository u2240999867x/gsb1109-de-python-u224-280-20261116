"""Tests for wave analysis, including brute-force cross validation."""

from __future__ import annotations

import itertools
import random
from datetime import datetime, timedelta, timezone

import pytest

from wave_analysis import (
    AnalysisResult,
    Announcement,
    Interval,
    WaveAnalysisLimitError,
    WaveAnalyzer,
    ambiguous_day_record,
    analyze,
    day_record,
    day_window,
    precise_record,
    precise_window,
)

UTC = timezone.utc
T = timedelta(minutes=10)


def dt(year, month, day, hour=0, minute=0, second=0, microsecond=0):
    return datetime(year, month, day, hour, minute, second, microsecond, tzinfo=UTC)


def assert_schedule_legal(result: AnalysisResult, schedule) -> None:
    by_id = {r.record_id: r for r in result.records}
    prev_time = None
    prev_id = None
    for rid in schedule.order:
        instant = schedule.assignment.times[rid]
        record = by_id[rid]
        assert any(w.contains(instant) for w in record.windows), (
            f"{rid} scheduled outside its feasible windows"
        )
        if prev_time is not None:
            assert instant >= prev_time, "schedule order violated"
            gap = instant - prev_time
            assert schedule.gaps[(prev_id, rid)] == gap
            in_same_wave = gap <= result.threshold
            wave_ids = [w for w in schedule.waves if rid in w][0]
            if in_same_wave:
                assert prev_id in wave_ids
            else:
                assert prev_id not in wave_ids
        prev_time = instant
        prev_id = rid
    assert schedule.wave_count == len(schedule.waves)


# ---------------------------------------------------------------------------
# Basic behavior
# ---------------------------------------------------------------------------


def test_precise_records_fixed_partition():
    records = [
        precise_record("a", "公告A 10:00", dt(2026, 1, 1, 10, 0)),
        precise_record("b", "公告B 10:05", dt(2026, 1, 1, 10, 5)),
        precise_record("c", "公告C 11:00", dt(2026, 1, 1, 11, 0)),
    ]
    result = analyze(records, T)
    assert result.feasible
    assert result.min_waves == result.max_waves == 2
    assert result.certain_together == [("a", "b")]
    assert result.certain_separated == [("a", "c"), ("b", "c")]
    assert result.ambiguous_pairs == []
    assert_schedule_legal(result, result.min_schedule)
    assert_schedule_legal(result, result.max_schedule)


def test_single_record_is_one_wave():
    result = analyze(
        [precise_record("solo", "唯一公告", dt(2026, 3, 3, 9))], T
    )
    assert result.min_waves == result.max_waves == 1
    assert result.certain_together == []


def test_day_precision_pair_is_ambiguous():
    # Same calendar day, 24h window each; a 10-minute threshold allows both
    # one wave (pick close instants) and two waves (pick far instants).
    records = [
        day_record("d1", "公告一 2026-01-05", dt(2026, 1, 5)),
        day_record("d2", "公告二 2026-01-05", dt(2026, 1, 5)),
    ]
    result = analyze(records, T)
    assert result.min_waves == 1
    assert result.max_waves == 2
    assert result.certain_together == []
    assert result.certain_separated == []
    assert ("d1", "d2") in result.ambiguous_pairs
    assert_schedule_legal(result, result.min_schedule)
    assert_schedule_legal(result, result.max_schedule)
    assert result.min_schedule.wave_count == 1
    assert result.max_schedule.wave_count == 2


def test_wide_threshold_forces_single_wave():
    records = [
        day_record("d1", "公告一", dt(2026, 1, 5)),
        day_record("d2", "公告二", dt(2026, 1, 5)),
    ]
    result = analyze(records, timedelta(hours=48))
    assert result.min_waves == result.max_waves == 1
    assert result.certain_together == [("d1", "d2")]


def test_disjoint_days_always_separated():
    records = [
        day_record("d1", "公告一 2026-01-05", dt(2026, 1, 5)),
        day_record("d2", "公告二 2026-01-09", dt(2026, 1, 9)),
    ]
    result = analyze(records, T)
    assert result.min_waves == result.max_waves == 2
    assert result.certain_separated == [("d1", "d2")]


# ---------------------------------------------------------------------------
# The anti-transitivity requirement
# ---------------------------------------------------------------------------


def test_pairwise_closeness_is_not_chained_across_schedules():
    # A at 10:00, B somewhere in [10:05, 10:55], C at 11:00, T = 10 min.
    # A~B is possible and B~C is possible, but no single schedule has A~B~C,
    # so (A, C) must never be reported as a certain pair.
    records = [
        precise_record("a", "公告A", dt(2026, 1, 1, 10, 0)),
        Announcement(
            "b",
            "公告B 时间未定",
            [Interval(dt(2026, 1, 1, 10, 5), dt(2026, 1, 1, 10, 55))],
            precision="minute-window",
        ),
        precise_record("c", "公告C", dt(2026, 1, 1, 11, 0)),
    ]
    result = analyze(records, T)
    assert ("a", "c") not in result.certain_together
    assert ("a", "b") in result.ambiguous_pairs
    assert ("b", "c") in result.ambiguous_pairs
    assert result.max_waves >= 2
    # No witness schedule may ever place all three inside one wave.
    assert result.min_waves == 2
    for schedule in (result.min_schedule, result.max_schedule):
        assert_schedule_legal(result, schedule)
        assert not schedule.same_wave("a", "c")


def test_certain_together_requires_one_shared_schedule_chain():
    # Three records where the whole chain is simultaneously realizable.
    records = [
        precise_record("a", "公告A", dt(2026, 1, 1, 10, 0)),
        Announcement(
            "b",
            "公告B",
            [Interval(dt(2026, 1, 1, 10, 4), dt(2026, 1, 1, 10, 6))],
        ),
        precise_record("c", "公告C", dt(2026, 1, 1, 10, 9)),
    ]
    result = analyze(records, T)
    assert result.min_waves == 1
    assert result.certain_together == [("a", "b"), ("a", "c"), ("b", "c")]


# ---------------------------------------------------------------------------
# Timezone ambiguity
# ---------------------------------------------------------------------------


def test_ambiguous_timezone_windows_preserved():
    record = ambiguous_day_record(
        "z",
        "公告Z 2026-01-05 (时区不详)",
        dt(2026, 1, 5),
        ["Asia/Shanghai", "UTC"],
    )
    assert len(record.windows) == 2
    shanghai_window = day_window(dt(2026, 1, 5), "Asia/Shanghai")
    utc_window = day_window(dt(2026, 1, 5), "UTC")
    assert shanghai_window in record.windows
    assert utc_window in record.windows
    # Shanghai day starts 8 hours earlier in UTC.
    assert shanghai_window.lower == dt(2026, 1, 4, 16, 0)
    assert record.tz_note.startswith("ambiguous")


def test_timezone_ambiguity_changes_bounds():
    # A precise 2026-01-05 20:00 UTC announcement versus a day-precision
    # announcement whose zone could be Shanghai (day already over by then).
    records = [
        ambiguous_day_record(
            "z", "公告Z 日期 only", dt(2026, 1, 5), ["UTC", "Asia/Shanghai"]
        ),
        precise_record("p", "公告P", dt(2026, 1, 5, 20, 0)),
    ]
    result = analyze(records, timedelta(minutes=30))
    # Under UTC the pair can be one wave; under Shanghai they are far apart.
    assert result.min_waves == 1
    assert result.max_waves == 2
    assert ("z", "p") in result.ambiguous_pairs
    assert_schedule_legal(result, result.min_schedule)
    assert_schedule_legal(result, result.max_schedule)


# ---------------------------------------------------------------------------
# Known ordering and contradictions
# ---------------------------------------------------------------------------


def test_contradictory_known_order_is_reported_separately():
    records = [
        precise_record("a", "公告A", dt(2026, 1, 1, 12)),
        precise_record("b", "公告B", dt(2026, 1, 1, 9)),
    ]
    result = analyze(records, T, known_order=[("a", "b")])
    assert not result.feasible
    assert result.contradictions
    assert result.contradictions[0].records == ("a", "b")
    assert result.min_schedule is None
    # Contradictions are distinct from ambiguous pair output.
    assert result.ambiguous_pairs == []


def test_known_order_restricts_enumeration():
    records = [
        day_record("a", "公告A", dt(2026, 1, 5)),
        day_record("b", "公告B", dt(2026, 1, 5)),
    ]
    result = analyze(records, timedelta(hours=2), known_order=[("a", "b")])
    assert result.feasible
    for schedule in (result.min_schedule, result.max_schedule):
        assert schedule.order.index("a") < schedule.order.index("b")


def test_known_order_cycle_contradiction():
    records = [
        day_record("a", "公告A", dt(2026, 1, 5)),
        day_record("b", "公告B", dt(2026, 1, 5)),
    ]
    result = analyze(
        records, T, known_order=[("a", "b"), ("b", "a")]
    )
    assert not result.feasible
    assert any("cycle" in c.message for c in result.contradictions)


def test_strict_known_order_contradicts_identical_instants():
    # Both records are pinned to the same exact instant; a strict precedence
    # claim between them cannot be satisfied.
    records = [
        precise_record("a", "公告A", dt(2026, 1, 1, 12)),
        precise_record("b", "公告B", dt(2026, 1, 1, 12)),
    ]
    result = analyze(records, T, known_order=[("a", "b")])
    assert not result.feasible
    assert result.contradictions
    assert result.contradictions[0].records == ("a", "b")


def test_order_limit_guard_raises():
    # Distinct but fully overlapping windows make every permutation feasible
    # while preventing identical-window deduplication.
    records = [
        Announcement(
            f"r{i}",
            f"公告{i}",
            [
                Interval(
                    dt(2026, 1, 1, 8) + timedelta(minutes=i),
                    dt(2026, 1, 1, 20) + timedelta(minutes=i),
                )
            ],
        )
        for i in range(6)
    ]
    with pytest.raises(WaveAnalysisLimitError):
        analyze(records, T, max_orders=10)
    # Raising the limit makes the same instance solvable.
    result = analyze(records, T, max_orders=None)
    assert result.feasible
    assert result.feasible_orders == 720


# ---------------------------------------------------------------------------
# Auditability
# ---------------------------------------------------------------------------


def test_report_keeps_every_original_announcement():
    raws = ["原始公告甲 2026-01-05", "原始公告乙 2026-01-05 12:30:45"]
    records = [
        day_record("a", raws[0], dt(2026, 1, 5)),
        precise_record("b", raws[1], dt(2026, 1, 5, 12, 30, 45)),
    ]
    result = analyze(records, T)
    report = result.report()
    for raw in raws:
        assert raw in report
    assert f"min={result.min_waves}" in report
    assert f"max={result.max_waves}" in report


# ---------------------------------------------------------------------------
# Brute-force cross validation
# ---------------------------------------------------------------------------


def _grid_instants(record: Announcement, all_records, threshold):
    """Exact sampling grid for gap/order comparisons.

    Wave partitions depend only on ordering (t_i <= t_j) and gap tests
    (t_j - t_i <= T); both flip only at window endpoints or at another
    window's endpoint shifted by +/- T.  Sampling every such breakpoint
    inside each record's windows reproduces every achievable partition, so
    the brute-force enumeration is exact rather than heuristic.
    """
    points = set()
    for window in record.windows:
        points.add(window.lower)
        points.add(window.upper)
    for other in all_records:
        for window in other.windows:
            # Ordering comparisons flip at raw endpoints ...
            if any(w.lower <= window.lower <= w.upper for w in record.windows):
                points.add(window.lower)
            if any(w.lower <= window.upper <= w.upper for w in record.windows):
                points.add(window.upper)
            # ... gap comparisons flip at threshold-shifted endpoints.
            for candidate in (
                window.lower - threshold,
                window.lower + threshold,
                window.upper - threshold,
                window.upper + threshold,
            ):
                if any(w.lower <= candidate <= w.upper for w in record.windows):
                    points.add(candidate)
    # Strict gap (> T) first becomes satisfiable one epsilon past a boundary.
    for point in list(points):
        bumped = point + timedelta(microseconds=1)
        if any(w.lower <= bumped <= w.upper for w in record.windows):
            points.add(bumped)
    return sorted(points)


def _brute_force(records, threshold, known_order):
    pools = [_grid_instants(record, records, threshold) for record in records]
    ids = [r.record_id for r in records]
    min_waves = None
    max_waves = 0
    pair_together = {}
    pair_separated = {}
    for combo in itertools.product(*pools):
        times = dict(zip(ids, combo))
        order = sorted(ids, key=lambda rid: (times[rid], rid))
        position = {rid: idx for idx, rid in enumerate(order)}
        if any(position[a] >= position[b] for a, b in known_order):
            continue
        waves = [[order[0]]]
        for prev_id, rid in zip(order, order[1:]):
            if times[rid] - times[prev_id] <= threshold:
                waves[-1].append(rid)
            else:
                waves.append([rid])
        count = len(waves)
        min_waves = count if min_waves is None else min(min_waves, count)
        max_waves = max(max_waves, count)
        for i, a in enumerate(ids):
            for b in ids[i + 1 :]:
                key = (a, b)
                together = any(a in w and b in w for w in waves)
                pair_together.setdefault(key, set()).add(together)
                pair_separated.setdefault(key, set()).add(not together)
    return {
        "min_waves": min_waves,
        "max_waves": max_waves,
        "always_together": sorted(
            pair for pair, flags in pair_together.items() if flags == {True}
        ),
        "always_separated": sorted(
            pair for pair, flags in pair_separated.items() if flags == {True}
        ),
    }


def test_random_instances_match_brute_force():
    rng = random.Random(20261118)
    base = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    for trial in range(150):
        n = rng.randint(2, 4)
        records = []
        for idx in range(n):
            start = base + timedelta(minutes=rng.randint(0, 300))
            if rng.random() < 0.4:
                width_minutes = rng.choice([0, 5, 30, 120, 1440])
            else:
                width_minutes = 0
            if width_minutes == 0:
                window = Interval(start, start)
                windows = [window]
            else:
                window = Interval(start, start + timedelta(minutes=width_minutes))
                windows = [window]
                if rng.random() < 0.3:
                    shifted = Interval(
                        start - timedelta(hours=8),
                        start + timedelta(minutes=width_minutes) - timedelta(hours=8),
                    )
                    windows.append(shifted)
            records.append(
                Announcement(
                    record_id=f"r{idx}",
                    raw=f"公告{idx} 原文",
                    windows=windows,
                    precision="day" if width_minutes >= 1440 else "second",
                )
            )
        threshold = timedelta(minutes=rng.choice([1, 10, 30, 120, 300]))
        known_order = []
        if rng.random() < 0.3:
            i, j = rng.sample(range(n), 2)
            known_order.append((f"r{i}", f"r{j}"))

        expected = _brute_force(records, threshold, known_order)
        if expected["min_waves"] is None:
            infeasible = analyze(records, threshold, known_order)
            assert not infeasible.feasible
            assert infeasible.contradictions
            continue

        result = analyze(records, threshold, known_order)
        assert result.feasible, trial
        assert result.min_waves == expected["min_waves"], trial
        assert result.max_waves == expected["max_waves"], trial
        assert result.certain_together == expected["always_together"], trial
        assert result.certain_separated == expected["always_separated"], trial
        assert_schedule_legal(result, result.min_schedule)
        assert_schedule_legal(result, result.max_schedule)
        assert result.min_schedule.wave_count == result.min_waves
        assert result.max_schedule.wave_count == result.max_waves


def test_disconnected_windows_still_allow_one_wave():
    # Regression: the second timezone window is the only interpretation close
    # enough to the precise record.  Bounds-only reasoning wrongly reported
    # two forced waves and could not reconstruct the close schedule.
    records = [
        Announcement(
            "z",
            "公告Z 时区不详",
            [
                Interval(dt(2026, 1, 1, 1, 58), dt(2026, 1, 1, 3, 58)),
                Interval(dt(2026, 1, 1, 9, 58), dt(2026, 1, 1, 11, 58)),
            ],
            precision="day",
        ),
        precise_record("p", "公告P", dt(2026, 1, 1, 12, 31)),
    ]
    result = analyze(records, timedelta(hours=5))
    assert result.min_waves == 1
    assert result.max_waves == 2
    assert_schedule_legal(result, result.min_schedule)
    assert result.min_schedule.wave_count == 1
    assert_schedule_legal(result, result.max_schedule)
    assert result.max_schedule.wave_count == 2


def test_disconnected_windows_chain_through_late_window():
    # Regression: ordering requires the wide-window record to use its late
    # window to sit between two precise records inside one wave.
    records = [
        precise_record("a", "公告A", dt(2026, 1, 1, 9, 46)),
        Announcement(
            "b",
            "公告B 时区不详",
            [
                Interval(dt(2026, 1, 1, 3, 13), dt(2026, 1, 2, 3, 13)),
                Interval(dt(2026, 1, 1, 11, 13), dt(2026, 1, 2, 11, 13)),
            ],
        ),
        precise_record("c", "公告C", dt(2026, 1, 1, 12, 35)),
        precise_record("d", "公告D", dt(2026, 1, 1, 12, 39)),
    ]
    result = analyze(records, timedelta(hours=5), known_order=[("a", "d")])
    assert result.min_waves == 1
    assert_schedule_legal(result, result.min_schedule)
    assert result.min_schedule.wave_count == 1
    assert result.min_schedule.same_wave("a", "d")


def test_threshold_boundary_is_inclusive():
    records = [
        precise_record("a", "公告A", dt(2026, 1, 1, 10, 0)),
        precise_record("b", "公告B", dt(2026, 1, 1, 10, 10)),
    ]
    result = analyze(records, timedelta(minutes=10))
    assert result.min_waves == result.max_waves == 1
    strict = analyze(records, timedelta(minutes=10) - timedelta(microseconds=1))
    assert strict.max_waves == 2


def test_invalid_inputs_rejected():
    with pytest.raises(ValueError):
        Interval(dt(2026, 1, 2), dt(2026, 1, 1))
    with pytest.raises(ValueError):
        WaveAnalyzer([], T)
    with pytest.raises(ValueError):
        analyze(
            [
                precise_record("x", "x", dt(2026, 1, 1)),
                precise_record("x", "x", dt(2026, 1, 1)),
            ],
            T,
        )
    with pytest.raises(ValueError):
        analyze([precise_record("x", "x", dt(2026, 1, 1))], timedelta(minutes=-1))
    with pytest.raises(ValueError):
        WaveAnalyzer(
            [precise_record("x", "x", dt(2026, 1, 1))], T, max_orders=0
        )


def test_precise_window_handles_naive_as_utc():
    naive = datetime(2026, 1, 1, 12, 0)
    window = precise_window(naive)
    assert window.lower.tzinfo == UTC
    assert window.lower == dt(2026, 1, 1, 12, 0)
