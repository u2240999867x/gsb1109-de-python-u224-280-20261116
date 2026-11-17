# Wave analysis over imprecise announcement timestamps

Business rule: after sorting announcements by their *actual* time, a **wave**
is a maximal consecutive run whose adjacent gaps do not exceed a threshold.
Timestamps have mixed precision (day, minute, second, ...) and may carry an
ambiguous timezone, so neither the ordering nor the grouping is fixed.

This package quantifies over **all legal assignments** — one shared schedule
that picks a single concrete instant inside every record's feasible windows —
and reports only relations that hold (or fail) in every such schedule.

## What it computes

- **Certain same-wave pairs** — together in every legal schedule.
- **Certain split pairs** — a wave boundary always separates them.
- **Ambiguous pairs** — both groupings are achievable (listed separately).
- **Wave-count lower/upper bounds**, each with a concrete witness schedule.
- **Contradictory evidence** — precedence constraints no schedule satisfies.
- The original announcement text is retained verbatim on every record.

Pairwise closeness is **never** chained across different schedules: a pair is
only "certain" when one single shared assignment realizes the whole relation.

## Model

- `Interval` — closed feasible range of absolute (UTC) instants.
- `Announcement` — one record; one `Interval` per interpretation, so a
  day-precision stamp with an unknown timezone keeps one day-long window per
  candidate zone instead of silently choosing one.
- Known precedence `(a, b)` means strict order `t_a < t_b`.
- Wave break condition is the strict gap `t_next - t_prev > threshold`.

## Exactness

For each feasible total order, candidate instants are restricted to a
**breakpoint grid** (window endpoints and endpoints shifted by ±threshold,
plus one-microsecond strictness steps). Ordering and gap comparisons only
change at those values, so grid assignments reproduce every achievable wave
partition. A memoized DFS over the grid then solves wave-count extrema and
pair relations exactly, and every bound is materialized into a concrete
schedule. The randomized test suite cross-checks the solver against an
exhaustive grid brute-force oracle.

When nearly all windows overlap, the number of distinct orders is factorial;
`max_orders` (default 5000) raises `WaveAnalysisLimitError` instead of
hanging. Pass `max_orders=None` to remove the guard.

## Usage

```python
from datetime import datetime, timedelta, timezone
from wave_analysis import analyze, day_record, precise_record

records = [
    precise_record("a", "公告A 09:00:00",
                   datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)),
    day_record("b", "公告B 2026-01-05", datetime(2026, 1, 5, tzinfo=timezone.utc)),
]

result = analyze(records, threshold=timedelta(minutes=30),
                 known_order=[("a", "b")])
print(result.report())
```

Run the worked example with `python -m wave_analysis.demo` and the tests with
`python -m pytest wave_analysis/tests/`.
