"""Wave (波次) analysis over imprecise announcement timestamps.

Announcement times are given with mixed precision (day / minute / second ...)
and may carry an ambiguous timezone.  Business rule: after sorting the records
by their *actual* time, a *wave* is a maximal consecutive run whose adjacent
gaps do not exceed a given threshold.

Because the true instants are only constrained to lie inside each record's
feasible window (one window per timezone interpretation), the ordering and
the grouping are generally not fixed.  This module quantifies over the set of
*all legal assignments* -- one shared schedule choosing one instant per
record -- and computes:

* certain same-wave relation  -- pairs together in every legal schedule;
* certain split relation      -- pairs separated in every legal schedule;
* ambiguous pairs             -- pairs with more than one legal grouping;
* wave-count lower/upper bounds, each witnessed by a concrete schedule;
* contradictory evidence      -- precedence constraints no schedule meets.

Relations are never derived by merging results from different schedules:
every verdict is quantified over the same global assignment space, so two
pairwise closeness facts cannot be chained transitively unless a single
schedule realizes both.

The original announcement text is kept verbatim on every record so the
source evidence remains individually auditable.

Known precedence relations are interpreted as strict time order
(``t_a < t_b``); two records pinned to the same exact instant therefore
contradict a precedence claim between them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Callable, Iterable, Optional, Sequence

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.9+ always has zoneinfo
    ZoneInfo = None  # type: ignore[assignment]


# Strictly-greater comparisons on datetimes use one microsecond as the
# smallest representable time step.
_EPSILON = timedelta(microseconds=1)


# ---------------------------------------------------------------------------
# Feasible windows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Interval:
    """Closed interval of absolute instants, both endpoints inclusive."""

    lower: datetime
    upper: datetime

    def __post_init__(self) -> None:
        if self.lower.tzinfo is None or self.upper.tzinfo is None:
            raise ValueError("interval endpoints must be timezone-aware")
        if self.lower > self.upper:
            raise ValueError("empty interval: lower > upper")

    @property
    def width(self) -> timedelta:
        return self.upper - self.lower

    def contains(self, value: datetime) -> bool:
        return self.lower <= value <= self.upper


def _earliest_in_union(
    windows: Sequence[Interval], target: datetime
) -> Optional[datetime]:
    """Smallest instant >= ``target`` lying in some window (or ``None``)."""
    best: Optional[datetime] = None
    for win in windows:
        if win.upper < target:
            continue
        candidate = max(win.lower, target)
        if candidate <= win.upper and (best is None or candidate < best):
            best = candidate
    return best


def _latest_in_union(
    windows: Sequence[Interval], target: datetime
) -> Optional[datetime]:
    """Largest instant <= ``target`` lying in some window (or ``None``)."""
    best: Optional[datetime] = None
    for win in windows:
        if win.lower > target:
            continue
        candidate = min(win.upper, target)
        if candidate >= win.lower and (best is None or candidate > best):
            best = candidate
    return best


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class Announcement:
    """One announcement with its original text and all ambiguity preserved.

    ``windows`` holds one feasible :class:`Interval` per interpretation.  A
    precise timestamp is a singleton window; a day-precision timestamp with
    an ambiguous timezone yields one day-long window per candidate zone.
    """

    record_id: str
    raw: str
    windows: Sequence[Interval]
    precision: str = "unknown"
    tz_note: str = ""

    def __post_init__(self) -> None:
        if not self.windows:
            raise ValueError(f"record {self.record_id!r} has no feasible window")
        self.windows = tuple(
            sorted(self.windows, key=lambda iv: (iv.lower, iv.upper))
        )

    @property
    def lower(self) -> datetime:
        return self.windows[0].lower

    @property
    def upper(self) -> datetime:
        return self.windows[-1].upper

    def earliest_at_least(self, target: datetime) -> Optional[datetime]:
        return _earliest_in_union(self.windows, target)

    def latest_at_most(self, target: datetime) -> Optional[datetime]:
        return _latest_in_union(self.windows, target)


# ---------------------------------------------------------------------------
# Schedules and result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Assignment:
    """One fully resolved choice of UTC instants."""

    times: dict[str, datetime]

    def ordered_ids(self) -> list[str]:
        return sorted(self.times, key=lambda rid: (self.times[rid], rid))


@dataclass
class Schedule:
    """A legal assignment with the wave partition it induces."""

    assignment: Assignment
    order: list[str]
    waves: list[list[str]]
    gaps: dict[tuple[str, str], timedelta]
    threshold: timedelta

    @property
    def wave_count(self) -> int:
        return len(self.waves)

    def same_wave(self, a: str, b: str) -> bool:
        for wave in self.waves:
            if a in wave:
                return b in wave
            if b in wave:
                return a in wave
        raise KeyError((a, b))


@dataclass
class PairVerdict:
    a: str
    b: str
    always_together: bool
    always_separated: bool
    note: str = ""


@dataclass
class Contradiction:
    """Evidence that no legal schedule can satisfy."""

    message: str
    records: tuple[str, ...] = ()


@dataclass
class AnalysisResult:
    records: tuple[Announcement, ...]
    threshold: timedelta
    contradictions: list[Contradiction]
    certain_together: list[tuple[str, str]]
    certain_separated: list[tuple[str, str]]
    ambiguous_pairs: list[tuple[str, str]]
    pair_verdicts: dict[tuple[str, str], PairVerdict]
    min_waves: int
    max_waves: int
    min_schedule: Optional[Schedule]
    max_schedule: Optional[Schedule]
    feasible_orders: int
    feasible: bool

    def report(self) -> str:
        lines: list[str] = []
        lines.append("Wave analysis report")
        lines.append("=" * 44)
        lines.append(
            f"records: {len(self.records)}  threshold: {self.threshold}  "
            f"feasible orders: {self.feasible_orders}"
        )
        lines.append("")
        lines.append("Original announcements (verbatim, each auditable):")
        for rec in self.records:
            lines.append(f"  [{rec.record_id}] {rec.raw!r}")
            lines.append(f"       precision={rec.precision} tz={rec.tz_note or '-'}")
            for win in rec.windows:
                lines.append(
                    f"       feasible UTC: [{win.lower.isoformat()} .. "
                    f"{win.upper.isoformat()}]"
                )
        lines.append("")
        if self.contradictions:
            lines.append("Contradictory evidence:")
            for con in self.contradictions:
                lines.append(f"  - {con.message}")
        else:
            lines.append("Contradictory evidence: none")
        lines.append("")
        if not self.feasible:
            lines.append("No legal schedule exists; wave bounds undefined.")
            return "\n".join(lines)
        lines.append(f"Wave count bounds: min={self.min_waves}  max={self.max_waves}")
        lines.append("")
        lines.append("Certain same-wave pairs (every legal schedule):")
        if self.certain_together:
            lines.extend(f"  {a} ~ {b}" for a, b in self.certain_together)
        else:
            lines.append("  (none)")
        lines.append("")
        lines.append("Certain split pairs (every legal schedule):")
        if self.certain_separated:
            lines.extend(f"  {a} | {b}" for a, b in self.certain_separated)
        else:
            lines.append("  (none)")
        lines.append("")
        lines.append("Ambiguous pairs (multiple legal groupings):")
        if self.ambiguous_pairs:
            lines.extend(f"  {a} ? {b}" for a, b in self.ambiguous_pairs)
        else:
            lines.append("  (none)")
        lines.append("")
        lines.append(f"Witness for lower bound ({self.min_waves} wave(s)):")
        lines.extend(_format_schedule(self.min_schedule))
        lines.append("")
        lines.append(f"Witness for upper bound ({self.max_waves} wave(s)):")
        lines.extend(_format_schedule(self.max_schedule))
        return "\n".join(lines)


def _format_schedule(schedule: Optional[Schedule]) -> list[str]:
    if schedule is None:
        return ["  (unavailable)"]
    out: list[str] = []
    for idx, wave in enumerate(schedule.waves, start=1):
        out.append(f"  wave {idx}:")
        for rid in wave:
            out.append(f"    {rid}  {schedule.assignment.times[rid].isoformat()}")
    for (a, b), gap in schedule.gaps.items():
        marker = "BREAK" if gap > schedule.threshold else "within"
        out.append(
            f"  gap {a} -> {b}: {gap} ({marker}, threshold {schedule.threshold})"
        )
    return out


def _must_precede(earlier: Announcement, later: Announcement) -> bool:
    return earlier.upper < later.lower


def _merge_parts(parts: Iterable[Interval]) -> tuple[Interval, ...]:
    ordered = sorted(parts, key=lambda iv: (iv.lower, iv.upper))
    merged: list[Interval] = []
    for part in ordered:
        if merged and part.lower <= merged[-1].upper + _EPSILON:
            merged[-1] = Interval(
                merged[-1].lower, max(merged[-1].upper, part.upper)
            )
        else:
            merged.append(part)
    return tuple(merged)


def _intersect_unions(
    left: Sequence[Interval], right: Sequence[Interval]
) -> tuple[Interval, ...]:
    parts: list[Interval] = []
    for a in left:
        for b in right:
            lo = max(a.lower, b.lower)
            hi = min(a.upper, b.upper)
            if lo <= hi:
                parts.append(Interval(lo, hi))
    return _merge_parts(parts)


@dataclass
class _OrderSolution:
    min_count: int
    max_count: int
    min_indices: list[int]
    max_indices: list[int]


class WaveAnalysisLimitError(RuntimeError):
    """Raised when too many distinct orderings would have to be explored.

    This happens when nearly every feasible window overlaps every other one;
    the exact semantics are factorial in that case.  Raise the configured
    limit (or narrow the windows / supply more known order) and retry.
    """


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------


class WaveAnalyzer:
    """Exact solver for guaranteed wave relations and wave-count bounds.

    The solver enumerates every distinct total order compatible with the
    windows, then reduces each order to a finite constraint problem on the
    *breakpoint grid*: wave partitions and ordering only change at window
    endpoints or at endpoints shifted by the threshold, so sampling those
    instants loses no feasible partition.  A memoized DFS over the grid
    solves wave-count extrema, pair relations and strict precedence
    constraints uniformly, and every reported bound is materialized into a
    concrete schedule.
    """

    def __init__(
        self,
        records: Iterable[Announcement],
        threshold: timedelta,
        known_order: Sequence[tuple[str, str]] = (),
        max_orders: Optional[int] = 5000,
    ) -> None:
        self.records: tuple[Announcement, ...] = tuple(records)
        if not self.records:
            raise ValueError("at least one record is required")
        self.ids: tuple[str, ...] = tuple(r.record_id for r in self.records)
        if len(set(self.ids)) != len(self.ids):
            raise ValueError("record ids must be unique")
        self.by_id: dict[str, Announcement] = {r.record_id: r for r in self.records}
        if threshold < timedelta(0):
            raise ValueError("threshold must be non-negative")
        if max_orders is not None and max_orders <= 0:
            raise ValueError("max_orders must be positive or None")
        self.threshold = threshold
        self.known_order: tuple[tuple[str, str], ...] = tuple(known_order)
        self.max_orders = max_orders
        self.contradictions: list[Contradiction] = []
        self._grid_cache: dict[tuple[str, ...], list[list[datetime]]] = {}

    # -- public entry point -------------------------------------------------

    def analyze(self) -> AnalysisResult:
        self.contradictions = []
        self._grid_cache = {}
        self._check_known_order()

        orders = self._enumerate_feasible_orders()
        feasible_orders: list[tuple[str, ...]] = []
        solutions: dict[tuple[str, ...], _OrderSolution] = {}
        breakable_edges: dict[tuple[str, ...], set[int]] = {}
        for order in orders:
            grids = self._grids_for(order)
            strict_preds = self._strict_predecessors_by_position(order)
            min_indices = self._extreme_assignment(
                order, grids, strict_preds, maximize=False
            )
            if min_indices is None:
                continue
            max_indices = self._extreme_assignment(
                order, grids, strict_preds, maximize=True
            )
            assert max_indices is not None
            min_count = self._wave_count(order, grids, min_indices)
            max_count = self._wave_count(order, grids, max_indices)
            feasible_orders.append(order)
            solutions[order] = _OrderSolution(
                min_count=min_count,
                max_count=max_count,
                min_indices=min_indices,
                max_indices=max_indices,
            )
            breakable_edges[order] = self._breakable_edge_set(
                order, grids, strict_preds
            )

        if not feasible_orders:
            if not self.contradictions:
                self.contradictions.append(
                    Contradiction(
                        message="no schedule satisfies the windows and known order"
                    )
                )
            return AnalysisResult(
                records=self.records,
                threshold=self.threshold,
                contradictions=list(self.contradictions),
                certain_together=[],
                certain_separated=[],
                ambiguous_pairs=[],
                pair_verdicts={},
                min_waves=0,
                max_waves=0,
                min_schedule=None,
                max_schedule=None,
                feasible_orders=0,
                feasible=False,
            )

        pair_verdicts = self._pair_verdicts(feasible_orders, breakable_edges)
        certain_together = sorted(
            pair for pair, v in pair_verdicts.items() if v.always_together
        )
        certain_separated = sorted(
            pair for pair, v in pair_verdicts.items() if v.always_separated
        )
        ambiguous_pairs = sorted(
            pair
            for pair, v in pair_verdicts.items()
            if not v.always_together and not v.always_separated
        )

        min_order = min(feasible_orders, key=lambda o: solutions[o].min_count)
        max_order = max(feasible_orders, key=lambda o: solutions[o].max_count)
        min_schedule = self._build_schedule(
            min_order,
            self._grids_for(min_order),
            solutions[min_order].min_indices,
        )
        max_schedule = self._build_schedule(
            max_order,
            self._grids_for(max_order),
            solutions[max_order].max_indices,
        )

        return AnalysisResult(
            records=self.records,
            threshold=self.threshold,
            contradictions=list(self.contradictions),
            certain_together=certain_together,
            certain_separated=certain_separated,
            ambiguous_pairs=ambiguous_pairs,
            pair_verdicts=pair_verdicts,
            min_waves=solutions[min_order].min_count,
            max_waves=solutions[max_order].max_count,
            min_schedule=min_schedule,
            max_schedule=max_schedule,
            feasible_orders=len(feasible_orders),
            feasible=True,
        )

    # -- contradiction checks ----------------------------------------------

    def _check_known_order(self) -> None:
        record_set = set(self.ids)
        for earlier_id, later_id in self.known_order:
            if earlier_id not in record_set or later_id not in record_set:
                self.contradictions.append(
                    Contradiction(
                        message=f"known order references unknown record "
                        f"{earlier_id!r} or {later_id!r}",
                        records=(earlier_id, later_id),
                    )
                )
                continue
            earlier = self.by_id[earlier_id]
            later = self.by_id[later_id]
            # Strict order e < l is impossible iff no later instant is
            # strictly above any earlier instant, i.e. latest(l) <= earliest(e).
            if later.upper <= earlier.lower:
                self.contradictions.append(
                    Contradiction(
                        message=(
                            f"known order {earlier_id} strictly before "
                            f"{later_id} is impossible: latest feasible "
                            f"{later_id} ({later.upper.isoformat()}) is not "
                            f"after earliest feasible {earlier_id} "
                            f"({earlier.lower.isoformat()})"
                        ),
                        records=(earlier_id, later_id),
                    )
                )
        seen: set[tuple[str, str]] = set()
        for a, b in self.known_order:
            if (b, a) in seen:
                self.contradictions.append(
                    Contradiction(
                        message=f"known order cycle: both {a} < {b} and {b} < {a}",
                        records=(a, b),
                    )
                )
            seen.add((a, b))

    # -- order enumeration --------------------------------------------------

    def _enumerate_feasible_orders(self) -> list[tuple[str, ...]]:
        n = len(self.records)
        orders: list[tuple[str, ...]] = []
        position_prune = self._forced_position_bounds()

        def backtrack(
            prefix: list[str],
            used: int,
            earliest_so_far: Optional[datetime],
        ) -> None:
            if used == (1 << n) - 1:
                if (
                    self.max_orders is not None
                    and len(orders) >= self.max_orders
                ):
                    raise WaveAnalysisLimitError(
                        f"more than {self.max_orders} distinct orderings are "
                        "feasible; supply more known order or raise max_orders"
                    )
                orders.append(tuple(prefix))
                return
            for idx, rec in enumerate(self.records):
                bit = 1 << idx
                if used & bit:
                    continue
                rid = rec.record_id
                lo_pos, hi_pos = position_prune[rid]
                if not (lo_pos <= len(prefix) <= hi_pos):
                    continue
                if not self._known_order_position_ok(rid, used):
                    continue
                if earliest_so_far is None:
                    chosen = rec.lower
                else:
                    picked = rec.earliest_at_least(earliest_so_far)
                    if picked is None:
                        continue
                    chosen = picked
                backtrack(prefix + [rid], used | bit, chosen)

        backtrack([], 0, None)
        return self._deduplicate_orders(orders)

    def _forced_position_bounds(self) -> dict[str, tuple[int, int]]:
        n = len(self.records)
        bounds: dict[str, tuple[int, int]] = {}
        for rec in self.records:
            after = sum(
                1
                for other in self.records
                if other.record_id != rec.record_id
                and _must_precede(other, rec)
            )
            before = sum(
                1
                for other in self.records
                if other.record_id != rec.record_id
                and _must_precede(rec, other)
            )
            bounds[rec.record_id] = (after, n - 1 - before)
        return bounds

    def _known_order_position_ok(self, rid: str, used: int) -> bool:
        for earlier, later in self.known_order:
            if later == rid and not (used & (1 << self._index_of(earlier))):
                return False
        return True

    def _index_of(self, rid: str) -> int:
        for idx, rec in enumerate(self.records):
            if rec.record_id == rid:
                return idx
        raise KeyError(rid)

    def _deduplicate_orders(
        self, orders: list[tuple[str, ...]]
    ) -> list[tuple[str, ...]]:
        """Collapse permutations of records with identical feasible sets."""
        signatures: dict[frozenset, list[str]] = {}
        for rec in self.records:
            key = frozenset((iv.lower, iv.upper) for iv in rec.windows)
            signatures.setdefault(key, []).append(rec.record_id)
        groups = [
            frozenset(ids)
            for ids in signatures.values()
            if len(ids) > 1 and not self._internal_known_order(frozenset(ids))
        ]
        if not groups:
            return orders

        unique: list[tuple[str, ...]] = []
        seen: set[tuple[str, ...]] = set()
        for order in orders:
            canonical = self._canonicalize(order, groups)
            if canonical not in seen:
                seen.add(canonical)
                unique.append(canonical)
        return unique

    def _internal_known_order(self, group: frozenset[str]) -> bool:
        return any(a in group and b in group for a, b in self.known_order)

    @staticmethod
    def _canonicalize(
        order: tuple[str, ...], groups: list[frozenset[str]]
    ) -> tuple[str, ...]:
        slots = list(order)
        for group in groups:
            idxs = [i for i, rid in enumerate(slots) if rid in group]
            for slot, rid in zip(idxs, sorted(slots[i] for i in idxs)):
                slots[slot] = rid
        return tuple(slots)

    # -- breakpoint grid ----------------------------------------------------

    def _grids_for(self, order: tuple[str, ...]) -> list[list[datetime]]:
        if order not in self._grid_cache:
            self._grid_cache[order] = self._breakpoint_grids(order)
        return self._grid_cache[order]

    def _breakpoint_grids(
        self, order: tuple[str, ...]
    ) -> list[list[datetime]]:
        """Exact candidate instants for every record in this order.

        Ordering comparisons flip at window endpoints; gap comparisons flip
        at endpoints shifted by +/- T; strict comparisons need one extra
        epsilon step.  Any schedule's induced partition is therefore also
        induced by some grid assignment.
        """
        grid: list[list[datetime]] = []
        for rid in order:
            record = self.by_id[rid]
            points: set[datetime] = set()
            for window in record.windows:
                points.add(window.lower)
                points.add(window.upper)
            for other_id in order:
                for window in self.by_id[other_id].windows:
                    if any(
                        w.lower <= window.lower <= w.upper for w in record.windows
                    ):
                        points.add(window.lower)
                    if any(
                        w.lower <= window.upper <= w.upper for w in record.windows
                    ):
                        points.add(window.upper)
                    for shifted in (
                        window.lower - self.threshold,
                        window.lower + self.threshold,
                        window.upper - self.threshold,
                        window.upper + self.threshold,
                    ):
                        if any(
                            w.lower <= shifted <= w.upper for w in record.windows
                        ):
                            points.add(shifted)
            for point in list(points):
                bumped = point + _EPSILON
                if any(w.lower <= bumped <= w.upper for w in record.windows):
                    points.add(bumped)
            grid.append(sorted(points))
        return grid

    # -- strict precedence --------------------------------------------------

    def _strict_predecessors_by_position(
        self, order: tuple[str, ...]
    ) -> dict[int, tuple[int, ...]]:
        position = {rid: idx for idx, rid in enumerate(order)}
        preds: dict[int, list[int]] = {}
        for earlier, later in self.known_order:
            if earlier in position and later in position:
                preds.setdefault(position[later], []).append(position[earlier])
        return {pos: tuple(sorted(set(p))) for pos, p in preds.items()}

    # -- exact grid DFS -----------------------------------------------------

    def _extreme_assignment(
        self,
        order: tuple[str, ...],
        grids: list[list[datetime]],
        strict_preds: dict[int, tuple[int, ...]],
        maximize: bool,
    ) -> Optional[list[int]]:
        """Grid assignment minimizing (or maximizing) the wave count.

        DP over positions and the chosen grid index.  Strict precedence
        predecessors are watched: their chosen indices travel in the memo
        state so the strict inequalities are checked against concrete
        instants of one single shared assignment.
        """
        n = len(order)
        watched = sorted({p for preds in strict_preds.values() for p in preds})
        sentinel = -1

        if not watched:
            @lru_cache(maxsize=None)
            def dp_simple(
                pos: int, prev_index: int
            ) -> Optional[tuple[int, tuple[int, ...]]]:
                if pos == n:
                    return (0, ())
                best: Optional[tuple[int, tuple[int, ...]]] = None
                previous = grids[pos - 1][prev_index] if pos > 0 else None
                for index, candidate in enumerate(grids[pos]):
                    if previous is not None:
                        if candidate < previous:
                            continue
                        added = 1 if candidate - previous > self.threshold else 0
                    else:
                        added = 0
                    tail = dp_simple(pos + 1, index)
                    if tail is None:
                        continue
                    candidate_result = (added + tail[0], (index,) + tail[1])
                    if best is None or (
                        candidate_result[0] > best[0]
                        if maximize
                        else candidate_result[0] < best[0]
                    ):
                        best = candidate_result
                return best

            result = dp_simple(0, sentinel)
            dp_simple.cache_clear()
            return None if result is None else list(result[1])

        watch_slot = {position: slot for slot, position in enumerate(watched)}
        pred_slots: dict[int, tuple[int, ...]] = {
            pos: tuple(watch_slot[pred] for pred in preds)
            for pos, preds in strict_preds.items()
        }

        @lru_cache(maxsize=None)
        def dp(
            pos: int, prev_index: int, watch: tuple[int, ...]
        ) -> Optional[tuple[int, tuple[int, ...]]]:
            if pos == n:
                return (0, ())
            best: Optional[tuple[int, tuple[int, ...]]] = None
            previous = grids[pos - 1][prev_index] if pos > 0 else None
            for index, candidate in enumerate(grids[pos]):
                if previous is not None and candidate < previous:
                    continue
                if any(
                    candidate <= grids[watched[slot]][watch[slot]]
                    for slot in pred_slots.get(pos, ())
                ):
                    continue
                added = 0
                if previous is not None and candidate - previous > self.threshold:
                    added = 1
                next_watch = self._advance_watch(watched, watch, pos, index)
                tail = dp(pos + 1, index, next_watch)
                if tail is None:
                    continue
                candidate_result = (added + tail[0], (index,) + tail[1])
                if best is None or (
                    candidate_result[0] > best[0]
                    if maximize
                    else candidate_result[0] < best[0]
                ):
                    best = candidate_result
            return best

        initial_watch = tuple(sentinel for _ in watched)
        result = dp(0, sentinel, initial_watch)
        dp.cache_clear()
        if result is None:
            return None
        return list(result[1])

    @staticmethod
    def _advance_watch(
        watched: list[int],
        watch: tuple[int, ...],
        pos: int,
        index: int,
    ) -> tuple[int, ...]:
        if pos not in watched:
            return watch
        updated = list(watch)
        updated[watched.index(pos)] = index
        return tuple(updated)

    def _restricted_assignment(
        self,
        order: tuple[str, ...],
        grids: list[list[datetime]],
        strict_preds: dict[int, tuple[int, ...]],
        edge_rule: Callable[[int, timedelta], bool],
    ) -> Optional[list[int]]:
        """Feasibility DFS with an extra predicate on every adjacent edge."""
        n = len(order)
        watched = sorted({p for preds in strict_preds.values() for p in preds})
        sentinel = -1

        if not watched:
            @lru_cache(maxsize=None)
            def dfs_simple(pos: int, prev_index: int) -> Optional[tuple[int, ...]]:
                if pos == n:
                    return ()
                previous = grids[pos - 1][prev_index] if pos > 0 else None
                for index, candidate in enumerate(grids[pos]):
                    if previous is not None:
                        if candidate < previous:
                            continue
                        if not edge_rule(pos, candidate - previous):
                            continue
                    tail = dfs_simple(pos + 1, index)
                    if tail is not None:
                        return (index,) + tail
                return None

            result = dfs_simple(0, sentinel)
            dfs_simple.cache_clear()
            return None if result is None else list(result)

        watch_slot = {position: slot for slot, position in enumerate(watched)}
        pred_slots: dict[int, tuple[int, ...]] = {
            pos: tuple(watch_slot[pred] for pred in preds)
            for pos, preds in strict_preds.items()
        }

        @lru_cache(maxsize=None)
        def dfs(
            pos: int, prev_index: int, watch: tuple[int, ...]
        ) -> Optional[tuple[int, ...]]:
            if pos == n:
                return ()
            previous = grids[pos - 1][prev_index] if pos > 0 else None
            for index, candidate in enumerate(grids[pos]):
                if previous is not None:
                    if candidate < previous:
                        continue
                    if not edge_rule(pos, candidate - previous):
                        continue
                if any(
                    candidate <= grids[watched[slot]][watch[slot]]
                    for slot in pred_slots.get(pos, ())
                ):
                    continue
                tail = dfs(
                    pos + 1, index, self._advance_watch(watched, watch, pos, index)
                )
                if tail is not None:
                    return (index,) + tail
            return None

        initial_watch = tuple(sentinel for _ in watched)
        result = dfs(0, sentinel, initial_watch)
        dfs.cache_clear()
        return None if result is None else list(result)

    # -- pair relation quantification --------------------------------------

    def _breakable_edge_set(
        self,
        order: tuple[str, ...],
        grids: list[list[datetime]],
        strict_preds: dict[int, tuple[int, ...]],
    ) -> set[int]:
        """Edges pos (between pos-1 and pos) where a break is realizable.

        One restricted DFS per edge, reused by every pair query.
        """
        breakable: set[int] = set()
        for edge in range(1, len(order)):
            def rule(pos: int, gap: timedelta, edge: int = edge) -> bool:
                if pos == edge:
                    return gap > self.threshold
                return True

            if (
                self._restricted_assignment(order, grids, strict_preds, rule)
                is not None
            ):
                breakable.add(edge)
        return breakable

    def _joinable_pairs(
        self,
        order: tuple[str, ...],
        grids: list[list[datetime]],
        strict_preds: dict[int, tuple[int, ...]],
    ) -> set[tuple[int, int]]:
        """Position pairs (left, right) joinable inside one wave.

        With no strict precedence inside the span, exact reachable-set
        propagation decides each start position in linear time: dilate the
        reachable instants by [0, T] and intersect with the next record's
        window union.  Strict precedence requires concrete cross-position
        checks, so those cases fall back to the exact grid DFS.
        """
        joinable: set[tuple[int, int]] = set()
        n = len(order)
        # Any strict precedence anywhere in the order constrains the whole
        # shared assignment (predecessor/successor may lie outside the queried
        # span), so only use interval propagation when no precedence exists.
        has_strict = bool(strict_preds)
        for left in range(n):
            reach: tuple[Interval, ...] = tuple(
                self.by_id[order[left]].windows
            )
            joinable.add((left, left))
            for right in range(left + 1, n):
                if has_strict:
                    if self._dfs_joinable(order, grids, strict_preds, left, right):
                        joinable.add((left, right))
                    continue
                dilated = tuple(
                    Interval(part.lower, part.upper + self.threshold)
                    for part in reach
                )
                reach = _intersect_unions(
                    _merge_parts(dilated), self.by_id[order[right]].windows
                )
                if not reach:
                    break
                joinable.add((left, right))
        return joinable

    def _dfs_joinable(
        self,
        order: tuple[str, ...],
        grids: list[list[datetime]],
        strict_preds: dict[int, tuple[int, ...]],
        left: int,
        right: int,
    ) -> bool:
        def rule(pos: int, gap: timedelta) -> bool:
            if left < pos <= right:
                return gap <= self.threshold
            return True

        return (
            self._restricted_assignment(order, grids, strict_preds, rule)
            is not None
        )

    def _pair_verdicts(
        self,
        orders: list[tuple[str, ...]],
        breakable_edges: dict[tuple[str, ...], set[int]],
    ) -> dict[tuple[str, str], PairVerdict]:
        n = len(self.records)
        possibly_together: set[tuple[str, str]] = set()
        possibly_separated: set[tuple[str, str]] = set()
        for order in orders:
            grids = self._grids_for(order)
            strict_preds = self._strict_predecessors_by_position(order)
            joinable = self._joinable_pairs(order, grids, strict_preds)
            edges = breakable_edges[order]
            for left in range(n):
                for right in range(left + 1, n):
                    key = self._canonical_pair(order[left], order[right])
                    if (left, right) in joinable:
                        possibly_together.add(key)
                    if any(left < edge <= right for edge in edges):
                        possibly_separated.add(key)
        verdicts: dict[tuple[str, str], PairVerdict] = {}
        for i in range(n):
            for j in range(i + 1, n):
                key = (self.ids[i], self.ids[j])
                together = key in possibly_together
                separated = key in possibly_separated
                verdicts[key] = PairVerdict(
                    a=key[0],
                    b=key[1],
                    always_together=together and not separated,
                    always_separated=separated and not together,
                    note=self._pair_note(together, separated),
                )
        return verdicts

    def _canonical_pair(self, a: str, b: str) -> tuple[str, str]:
        """Order a pair by record definition index, matching verdict keys."""
        ia, ib = self.ids.index(a), self.ids.index(b)
        return (a, b) if ia < ib else (b, a)

    @staticmethod
    def _pair_note(together: bool, separated: bool) -> str:
        if together and not separated:
            return "same wave in every legal schedule"
        if separated and not together:
            return "split by a wave boundary in every legal schedule"
        return "grouping depends on the chosen legal schedule"

    # -- schedule materialization ------------------------------------------

    def _wave_count(
        self,
        order: tuple[str, ...],
        grids: list[list[datetime]],
        indices: list[int],
    ) -> int:
        count = 1
        for pos in range(1, len(order)):
            gap = grids[pos][indices[pos]] - grids[pos - 1][indices[pos - 1]]
            if gap > self.threshold:
                count += 1
        return count

    def _build_schedule(
        self,
        order: tuple[str, ...],
        grids: list[list[datetime]],
        indices: list[int],
    ) -> Schedule:
        times = {
            order[pos]: grids[pos][indices[pos]] for pos in range(len(order))
        }
        assignment = Assignment(times)
        gaps: dict[tuple[str, str], timedelta] = {}
        waves: list[list[str]] = [[order[0]]]
        for prev_id, rid in zip(order, order[1:]):
            gap = times[rid] - times[prev_id]
            gaps[(prev_id, rid)] = gap
            if gap <= self.threshold:
                waves[-1].append(rid)
            else:
                waves.append([rid])
        return Schedule(
            assignment=assignment,
            order=list(order),
            waves=waves,
            gaps=gaps,
            threshold=self.threshold,
        )


# ---------------------------------------------------------------------------
# Constructors for common timestamp shapes
# ---------------------------------------------------------------------------


def day_window(day: datetime, tz) -> Interval:
    """Feasible interval of a day-precision timestamp interpreted in ``tz``."""
    if isinstance(tz, str):
        if ZoneInfo is None:  # pragma: no cover
            raise RuntimeError("zoneinfo unavailable")
        tz = ZoneInfo(tz)
    start = day.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz)
    end = day.replace(hour=23, minute=59, second=59, microsecond=999999, tzinfo=tz)
    return Interval(start.astimezone(timezone.utc), end.astimezone(timezone.utc))


def precise_window(value: datetime) -> Interval:
    """Singleton window for a fully precise timestamp."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return Interval(value, value)


def ambiguous_day_record(
    record_id: str,
    raw: str,
    day: datetime,
    candidate_tzs: Sequence[str],
    precision: str = "day",
) -> Announcement:
    """Day-precision announcement retaining every candidate timezone."""
    windows = tuple(day_window(day, tz) for tz in candidate_tzs)
    return Announcement(
        record_id=record_id,
        raw=raw,
        windows=windows,
        precision=precision,
        tz_note=f"ambiguous timezone, candidates: {', '.join(candidate_tzs)}",
    )


def precise_record(
    record_id: str, raw: str, value: datetime, tz_note: str = "exact"
) -> Announcement:
    return Announcement(
        record_id=record_id,
        raw=raw,
        windows=(precise_window(value),),
        precision=_precision_of(value),
        tz_note=tz_note,
    )


def day_record(
    record_id: str, raw: str, day: datetime, tz: str = "UTC"
) -> Announcement:
    return Announcement(
        record_id=record_id,
        raw=raw,
        windows=(day_window(day, tz),),
        precision="day",
        tz_note=f"day precision, zone {tz}",
    )


def _precision_of(value: datetime) -> str:
    if value.microsecond:
        return "microsecond"
    if value.second:
        return "second"
    if value.minute:
        return "minute"
    if value.hour:
        return "hour"
    return "day"


def analyze(
    records: Iterable[Announcement],
    threshold: timedelta,
    known_order: Sequence[tuple[str, str]] = (),
    max_orders: Optional[int] = 5000,
) -> AnalysisResult:
    """Convenience wrapper around :class:`WaveAnalyzer`."""
    return WaveAnalyzer(records, threshold, known_order, max_orders).analyze()


__all__ = [
    "Interval",
    "Announcement",
    "Assignment",
    "Schedule",
    "PairVerdict",
    "Contradiction",
    "AnalysisResult",
    "WaveAnalyzer",
    "WaveAnalysisLimitError",
    "day_window",
    "precise_window",
    "ambiguous_day_record",
    "precise_record",
    "day_record",
    "analyze",
]
