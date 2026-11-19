"""Feasibility engine.

A *scenario* is a set of true start/end times for every event.  Two
scenarios are evaluated:

- the **baseline** scenario, which keeps every recorded wait;
- the **counterfactual** scenario, in which the allowed throttle waits
  are shortened according to the evidence (see
  :func:`counterfactual_duration`).

Every scenario is a system of difference constraints:

- clock readings bound each true start and end to a window of half
  width ``clock_skew``;
- intrinsic durations bound ``end - start`` to a range;
- events chained in a material satisfy ``start(next) >= end(prev)``;
- events sharing a resource cannot overlap; since the recording does
  not state their order, every window-compatible ordering is
  enumerated and solved separately.

The longest-path closure of the difference system gives the tightest
lower bound of every variable (the earliest achievable time) and the
mirrored shortest-path closure gives its tightest upper bound (the
latest achievable time).  Taking the min / max over all feasible
resource orderings yields bounds that hold under *every* schedule
compatible with the evidence, not just one guessed timeline.
"""

from itertools import permutations
from itertools import product
from math import comb
from math import factorial

from .model import EVENT_IO
from .model import REASON_THROTTLE

# Guardrail against combinatorial explosion on large resources.
MAX_ORDERINGS = 5000
EPS = 1e-9


def counterfactual_duration(event, removed_waits):
    """Return the intrinsic duration range of a wait after the allowed
    throttle waits have been removed.

    The same waiting interval is never deducted twice: the duration
    range is adjusted once, regardless of how many reasons it carries.

    - waits without a throttle reason, throttle waits that are not
      removable and removable waits that are not in ``removed_waits``
      keep their full recorded range;
    - a removable wait whose only reason is throttle disappears: the
      whole interval is attributable to throttling;
    - a removable wait with additional reasons keeps the range
      ``[0, duration_max]``: the split between concurrent causes is
      not observed, so the other reasons may still need the entire
      interval, or none of it once throttling no longer gates them.
    """
    if event.kind != "wait":
        return event.duration_min, event.duration_max
    if REASON_THROTTLE not in event.reasons:
        return event.duration_min, event.duration_max
    if not event.removable_throttle or event.name not in removed_waits:
        return event.duration_min, event.duration_max
    if event.has_other_reason:
        return 0.0, event.duration_max
    return 0.0, 0.0


class _System:
    """Difference-constraint system over numbered variables."""

    def __init__(self, count):
        self.count = count
        self.edges = []  # (from_var, to_var, weight): to >= from + weight
        self.lower = [0.0] * count
        self.upper = [float("inf")] * count

    def set_domain(self, var, low, high):
        self.lower[var] = low
        self.upper[var] = high

    def add_edge(self, src, dst, weight):
        self.edges.append((src, dst, weight))

    def solve(self):
        """Return ``(feasible, lower_bounds, upper_bounds)``.

        ``lower_bounds`` are the greatest fixed-point lower bounds
        (earliest achievable times); ``upper_bounds`` are the least
        fixed-point upper bounds (latest achievable times).
        """
        low = list(self.lower)
        for _ in range(self.count + 1):
            changed = False
            for src, dst, weight in self.edges:
                candidate = low[src] + weight
                if candidate > low[dst] + EPS:
                    low[dst] = candidate
                    changed = True
            if not changed:
                break
        else:
            # A relaxation was still possible after n+1 passes: the
            # system contains a positive cycle and has no solution.
            return False, None, None
        if any(low[i] > self.upper[i] + EPS for i in range(self.count)):
            return False, None, None
        high = list(self.upper)
        for _ in range(self.count + 1):
            changed = False
            for src, dst, weight in self.edges:
                # dst >= src + w  <=>  src <= dst - w
                candidate = high[dst] - weight
                if candidate < high[src] - EPS:
                    high[src] = candidate
                    changed = True
            if not changed:
                break
        if any(high[i] < self.lower[i] - EPS for i in range(self.count)):
            return False, None, None
        return True, low, high


def _resource_orderings(trace, free_events=frozenset()):
    """Enumerate window-compatible lane assignments per resource.

    A resource of capacity ``c`` is modelled as ``c`` lanes; events in
    the same lane are serialized in the lane's order.  Every feasible
    real schedule corresponds to at least one such assignment, so the
    union of their bounds is exact.

    Events in ``free_events`` have no clock upper bound in the relaxed
    counterfactual system, so lane adjacencies touching them skip the
    window pre-filter.
    """
    choices_per_resource = []
    for resource in trace.resources.values():
        users = [
            event
            for event in trace.all_events
            if resource.name in event.resources
        ]
        if len(users) <= 1:
            choices_per_resource.append([()])
            continue
        windows = {
            event.name: (
                event.observed_start - trace.clock_skew,
                event.observed_start + trace.clock_skew,
                event.observed_end - trace.clock_skew,
                event.observed_end + trace.clock_skew,
            )
            for event in users
        }
        capacity = min(resource.capacity, len(users))
        n_users = len(users)
        if _distinct_lane_keys(n_users, capacity) > MAX_ORDERINGS:
            raise ValueError(
                f"resource {resource.name!r} alone admits more than "
                f"{MAX_ORDERINGS} orderings; refine the trace (narrow "
                "the clock skew or split the resource)"
            )
        seen = set()
        candidates = []
        # Every weak ordering of the users is reachable as a
        # permutation of the events plus a lane assignment: the
        # permutation fixes the order inside each lane, the assignment
        # decides which events share a lane.
        for permutation in permutations(users):
            for assignment in product(
                range(capacity), repeat=len(permutation)
            ):
                lanes = [[] for _ in range(capacity)]
                for event, lane in zip(permutation, assignment):
                    lanes[lane].append(event.name)
                key = tuple(
                    sorted(tuple(lane) for lane in lanes if lane)
                )
                if key in seen:
                    continue
                if _ordering_cheaply_possible(
                    key, windows, free_events
                ):
                    seen.add(key)
                    candidates.append(key)
        if not candidates:
            return []
        choices_per_resource.append(candidates)
    combined = []
    for combo in product(*choices_per_resource):
        order = {}
        for resource, lanes in zip(trace.resources.values(), combo):
            order[resource.name] = lanes
        combined.append(order)
    if len(combined) > MAX_ORDERINGS:
        raise ValueError(
            "resource constraints admit more than "
            f"{MAX_ORDERINGS} orderings; refine the trace (narrow the "
            "clock skew or split resources)"
        )
    return combined


def _ordering_cheaply_possible(lanes, windows, free_events):
    for lane in lanes:
        for earlier, later in zip(lane, lane[1:]):
            if earlier in free_events or later in free_events:
                continue
            _, _, end_lo, _ = windows[earlier]
            _, start_hi, _, _ = windows[later]
            if end_lo > start_hi + EPS:
                return False
    return True


def _distinct_lane_keys(n_users, capacity):
    """Number of distinct ordered-block partitions of ``n_users``
    labeled events into at most ``capacity`` unordered lanes.

    For exactly ``k`` nonempty lanes there are
    ``n! * C(n-1, k-1) / k!`` distinct keys (a permutation cut into
    ``k`` ordered blocks, with the lane labels ignored).  The total is
    the ordered Bell (Fubini) number for ``capacity >= n_users``.
    """
    total = 0
    for lanes in range(1, min(capacity, n_users) + 1):
        total += (
            factorial(n_users)
            * comb(n_users - 1, lanes - 1)
            // factorial(lanes)
        )
    return total


def _build_system(trace, duration_ranges, ordering, free_events):
    """Build the difference system.

    Events named in ``free_events`` lose their clock upper bounds:
    their true times are only floored at the earliest release of their
    material.  This models a counterfactual run in which events after a
    removed throttle wait are allowed to happen earlier than the clock
    recorded.  All other events stay anchored to their readings, which
    is what makes bounds derived from an anchored system provable.
    """
    events = list(trace.all_events)
    index = {event.name: idx for idx, event in enumerate(events)}
    system = _System(2 * len(events))

    def s_var(event):
        return 2 * index[event.name]

    def e_var(event):
        return 2 * index[event.name] + 1

    skew = trace.clock_skew
    release_floor = {
        material.name: material.events[0].observed_start - skew
        for material in trace.materials
    }
    for event in events:
        sidx = s_var(event)
        eidx = e_var(event)
        if event.name in free_events:
            floor = release_floor[event.material]
            system.set_domain(sidx, floor, float("inf"))
            system.set_domain(eidx, floor, float("inf"))
        else:
            system.set_domain(
                sidx,
                event.observed_start - skew,
                event.observed_start + skew,
            )
            system.set_domain(
                eidx,
                event.observed_end - skew,
                event.observed_end + skew,
            )
        d_lo, d_hi = duration_ranges[event.name]
        system.add_edge(sidx, eidx, d_lo)  # end >= start + d_lo
        system.add_edge(eidx, sidx, -d_hi)  # start >= end - d_hi

    for material in trace.materials:
        for previous, current in zip(
            material.events, material.events[1:]
        ):
            system.add_edge(
                e_var(previous), s_var(current), 0.0
            )

    for resource_name, lanes in ordering.items():
        for lane in lanes:
            for earlier, later in zip(lane, lane[1:]):
                event_a = trace.event_by_name(earlier)
                event_b = trace.event_by_name(later)
                system.add_edge(
                    e_var(event_a), s_var(event_b), 0.0
                )
    return system, s_var, e_var


def _duration_ranges(trace, counterfactual):
    ranges = {}
    for event in trace.all_events:
        if counterfactual:
            ranges[event.name] = counterfactual_duration(
                event, trace.removed_waits
            )
        else:
            ranges[event.name] = (
                event.duration_min,
                event.duration_max,
            )
    return ranges


def _counterfactual_free_events(trace):
    """Events that must leave their clock windows when the allowed
    throttle waits disappear: each removed wait itself and every event
    chained after the first removed wait of its material.
    """
    free = set()
    for material in trace.materials:
        cut = None
        for idx, event in enumerate(material.events):
            if (
                event.kind == "wait"
                and event.name in trace.removed_waits
            ):
                cut = idx
                break
        if cut is not None:
            for event in material.events[cut:]:
                free.add(event.name)
    return frozenset(free)


def solve_scenario(trace, counterfactual):
    """Solve one scenario across every feasible resource ordering.

    The baseline keeps every clock window anchored.  The
    counterfactual computes two envelopes:

    - an *anchored* solve, kept tight to the clock readings;
    - a *relaxed* solve in which the removed waits and their suffix
      events are only floored at their material release, so they can
      move earlier than the clock recorded.

    The anchored envelope backs provable statements; the relaxed
    envelope backs "possible" statements (maximum achievable reduction
    and whether a timeout can be escaped).  Both are returned so the
    audit layer never confuses the two evidence levels.
    """
    duration_ranges = _duration_ranges(trace, counterfactual)
    if not counterfactual:
        orderings = _resource_orderings(trace)
        if not orderings:
            return {"feasible": False, "orderings_considered": 0}
        result = _solve_with_orderings(
            trace, duration_ranges, orderings, frozenset()
        )
        result["relaxed"] = False
        return result

    free_events = _counterfactual_free_events(trace)
    anchored_orderings = _resource_orderings(trace)
    anchored = (
        _solve_with_orderings(
            trace, duration_ranges, anchored_orderings, frozenset()
        )
        if anchored_orderings
        else {"feasible": False, "orderings_considered": 0}
    )

    relaxed_orderings = _resource_orderings(trace, free_events)
    if not relaxed_orderings:
        return {"feasible": False, "orderings_considered": 0}
    relaxed = _solve_with_orderings(
        trace, duration_ranges, relaxed_orderings, free_events
    )
    relaxed["relaxed_events"] = sorted(free_events)

    if anchored["feasible"]:
        merged = dict(anchored)
        merged["relaxed"] = False
    else:
        merged = dict(relaxed)
        merged["relaxed"] = True
    merged["anchored_feasible"] = anchored["feasible"]
    merged["anchored_envelope"] = anchored
    merged["relaxed_envelope"] = relaxed
    merged["relaxed_events"] = relaxed["relaxed_events"]
    return merged


def _solve_with_orderings(
    trace, duration_ranges, orderings, free_events
):
    per_material = {
        material.name: {
            "earliest_start": float("inf"),
            "latest_start": float("-inf"),
            "earliest_end": float("inf"),
            "latest_end": float("-inf"),
        }
        for material in trace.materials
    }
    feasible_count = 0
    witness = None

    for ordering in orderings:
        system, s_var, e_var = _build_system(
            trace, duration_ranges, ordering, free_events
        )
        feasible, low, high = system.solve()
        if not feasible:
            continue
        feasible_count += 1
        for material in trace.materials:
            first = material.events[0]
            last = material.events[-1]
            bucket = per_material[material.name]
            bucket["earliest_start"] = min(
                bucket["earliest_start"], low[s_var(first)]
            )
            bucket["latest_start"] = max(
                bucket["latest_start"], high[s_var(first)]
            )
            bucket["earliest_end"] = min(
                bucket["earliest_end"], low[e_var(last)]
            )
            bucket["latest_end"] = max(
                bucket["latest_end"], high[e_var(last)]
            )
        if witness is None:
            witness = _witness(trace, low, high, s_var, e_var)

    if feasible_count == 0:
        return {"feasible": False, "orderings_considered": len(orderings)}

    return {
        "feasible": True,
        "orderings_considered": feasible_count,
        "orderings_enumerated": len(orderings),
        "materials": per_material,
        "witness": witness,
    }


def _witness(trace, low, high, s_var, e_var):
    """A single timeline (the earliest-bound fixed point) that
    satisfies every constraint, shown with the admissible slack.
    """
    rows = []
    for event in trace.all_events:
        rows.append(
            {
                "event": event.name,
                "material": event.material,
                "start": low[s_var(event)],
                "end": low[e_var(event)],
                "start_latest": high[s_var(event)],
                "end_latest": high[e_var(event)],
            }
        )
    return rows


def critical_path_guarantee(trace):
    """Provable reduction of the *earliest achievable* completion when
    only pure throttle waits are removed.

    Mixed-cause waits keep their full recorded duration in this
    conservative run, so the resulting reduction cannot rely on their
    other reasons disappearing.  Only resource orderings compatible
    with the recorded evidence are considered, and the value is
    minimized over all of them, which makes it hold under every
    schedule; a wait not on the binding critical path simply adds
    nothing.  Returns ``{material: reduction}``.
    """
    if not trace.removed_waits:
        return {material.name: 0.0 for material in trace.materials}

    baseline_ranges = _duration_ranges(trace, counterfactual=False)
    conservative = {}
    free = set()
    for event in trace.all_events:
        pure_removed = (
            event.kind == "wait"
            and event.name in trace.removed_waits
            and not event.has_other_reason
        )
        if pure_removed:
            conservative[event.name] = (0.0, 0.0)
        else:
            conservative[event.name] = baseline_ranges[event.name]

    for material in trace.materials:
        for idx, event in enumerate(material.events):
            pure_removed = (
                event.kind == "wait"
                and event.name in trace.removed_waits
                and not event.has_other_reason
            )
            if pure_removed:
                for suffix_event in material.events[idx:]:
                    free.add(suffix_event.name)
                break
    free = frozenset(free)

    orderings = _resource_orderings(trace)
    if not orderings:
        return {material.name: 0.0 for material in trace.materials}

    best = {
        material.name: float("inf") for material in trace.materials
    }
    for ordering in orderings:
        base_system, _, base_e = _build_system(
            trace, baseline_ranges, ordering, frozenset()
        )
        cons_system, _, cons_e = _build_system(
            trace, conservative, ordering, free
        )
        base_ok, base_low, _ = base_system.solve()
        cons_ok, cons_low, _ = cons_system.solve()
        if not base_ok or not cons_ok:
            continue
        for material in trace.materials:
            last = material.events[-1]
            reduction = (
                base_low[base_e(last)] - cons_low[cons_e(last)]
            )
            best[material.name] = min(
                best[material.name], max(0.0, reduction)
            )
    return {
        name: (0.0 if value == float("inf") else round(value, 9))
        for name, value in best.items()
    }


def clock_window(event, clock_skew):
    """Raw true-time window implied by the clock readings alone."""
    return {
        "start_lo": event.observed_start - clock_skew,
        "start_hi": event.observed_start + clock_skew,
        "end_lo": event.observed_end - clock_skew,
        "end_hi": event.observed_end + clock_skew,
    }


def io_events(trace):
    return [
        event for event in trace.all_events if event.kind == EVENT_IO
    ]
