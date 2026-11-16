"""Ring-call blocking analysis for RPA D-Bus recordings.

The RPA backend records D-Bus method traffic between several services. When
the recorded calls form a ring, the ring alone is not proof of a deadlock:

* asynchronous calls return immediately and never make the caller wait;
* callbacks that the target explicitly allows to re-enter can be dispatched on
  top of an in-flight call without needing a free worker slot;
* a saturated slot pool still drains if any holder has a reply, cancellation
  or local event that will release it;
* conclusions are only valid for a time slice and must be revoked once the
  resolving reply/cancel is observed;
* timestamps produced by different machines are not calibrated, so ordering
  between two clock domains may only be inferred from explicit causal edges
  (request/reply pairing, wait/set on the same machine, ``CallRecord.causes``).

This module implements the wait-for graph analysis behind those rules. All
inputs are plain data structures so the module is independent of the bus
runtime and can be used on offline recordings.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple


class CallType(str, Enum):
    SYNC = 'sync'
    ASYNC = 'async'


class Verdict(str, Enum):
    """Proven blocking state of a candidate group inside one time slice."""

    DEADLOCK = 'deadlock'
    BLOCKED = 'blocked'
    PENDING = 'pending'
    RELEASED = 'released'


@dataclass(frozen=True)
class ServiceModel:
    """Scheduling information known for one recorded service.

    ``slots`` is the number of concurrent method executions the service can
    host. ``reentrant`` declares whether incoming calls may re-enter the
    service while one of its calls is still in flight (for example a nested
    callback). Per-method overrides can be supplied through
    ``reentrant_methods``.
    """

    name: str
    slots: Optional[int] = None
    reentrant: bool = False
    reentrant_methods: FrozenSet[str] = frozenset()
    clock_domain: str = ''

    def allows_reentry(self, method: Optional[str]) -> bool:
        if method is not None and method in self.reentrant_methods:
            return True
        return self.reentrant


@dataclass(frozen=True)
class SchedulingModel:
    """Everything the analyser is allowed to assume about the services."""

    services: Dict[str, ServiceModel] = field(default_factory=dict)

    def get(self, service_name: str) -> Optional[ServiceModel]:
        return self.services.get(service_name)


@dataclass(frozen=True)
class CallRecord:
    """One recorded method invocation.

    Ticks are monotonic on the recording machine (``clock_domain``). Ticks from
    different domains must never be compared directly. ``causes`` lists the
    explicit cross-domain ordering edges that are safe to use, as pairs of
    event identifiers (``"<call_id>:start"`` / ``"<call_id>:end"`` /
    ``"<event_id>:set"``).
    """

    call_id: str
    caller: str
    target: str
    method: Optional[str] = None
    call_type: CallType = CallType.SYNC
    start_tick: float = 0
    reply_tick: Optional[float] = None
    cancel_tick: Optional[float] = None
    waits_on_event: Optional[str] = None
    clock_domain: str = ''
    causes: FrozenSet[Tuple[str, str]] = frozenset()

    @property
    def end_tick(self) -> Optional[float]:
        for tick in (self.reply_tick, self.cancel_tick):
            if tick is not None:
                return tick
        return None

    def start_event(self) -> str:
        return f'{self.call_id}:start'

    def end_event(self) -> str:
        return f'{self.call_id}:end'


@dataclass(frozen=True)
class EventRecord:
    """A local wait object (lock, condition, pending job slot, ...).

    ``holder`` is the call currently owning the event while it is unset. When
    the event is set the wait is released at ``set_tick``; an unset event with
    no known holder means the evidence is incomplete.
    """

    event_id: str
    service: str
    holder: Optional[str] = None
    set_tick: Optional[float] = None
    clock_domain: str = ''

    def set_event(self) -> str:
        return f'{self.event_id}:set'


@dataclass(frozen=True)
class Recording:
    calls: Sequence[CallRecord] = ()
    events: Sequence[EventRecord] = ()


@dataclass(frozen=True)
class WaitEdge:
    """A directed ``waiter -> blocker`` dependency inside a wait chain."""

    waiter: str
    blocker: str
    reason: str
    blocking: bool


@dataclass(frozen=True)
class BlockingGroup:
    """A candidate mutually-blocked group and its proven verdict."""

    group_id: str
    members: FrozenSet[str]
    chain: Tuple[WaitEdge, ...]
    verdict: Verdict
    reason: str
    resolved_by: Optional[str] = None


@dataclass(frozen=True)
class TimeSlice:
    """Analysis result for the interval ``[start_tick, end_tick)``.

    A slice ends whenever an observed event changes the wait-for graph: a call
    starts, a reply/cancel arrives or a local event is set. Ticks from
    different machines are merged into one ordered boundary list only for
    windowing; any blocking conclusion that needs cross-machine ordering is
    kept in ``pending_reasons`` until an explicit causal edge proves it.
    """

    slice_id: int
    start_tick: Optional[float]
    end_tick: Optional[float]
    clock_domain: str
    active_calls: FrozenSet[str]
    groups: Tuple[BlockingGroup, ...]
    pending_reasons: Tuple[str, ...] = ()


def _call_node(call_id: str) -> str:
    return f'call:{call_id}'


def _slot_node(service_name: str) -> str:
    return f'slots:{service_name}'


def _event_node(event_id: str) -> str:
    return f'event:{event_id}'


def _node_call_id(node: str) -> Optional[str]:
    prefix = 'call:'
    return node[len(prefix):] if node.startswith(prefix) else None


class _Graph:
    """Wait-for graph with labelled edges and SCC/connectivity search."""

    def __init__(self) -> None:
        self.adj: Dict[str, List[WaitEdge]] = {}

    def add_node(self, node: str) -> None:
        self.adj.setdefault(node, [])

    def add_edge(self, waiter: str, blocker: str, reason: str, blocking: bool) -> None:
        self.add_node(waiter)
        self.add_node(blocker)
        self.adj[waiter].append(WaitEdge(waiter, blocker, reason, blocking))

    def _tarjan(self, edges_only_blocking: bool) -> List[List[str]]:
        index: Dict[str, int] = {}
        low: Dict[str, int] = {}
        stack: List[str] = []
        on_stack: set = set()
        components: List[List[str]] = []
        counter = [0]

        def visit(node: str) -> None:
            index[node] = counter[0]
            low[node] = counter[0]
            counter[0] += 1
            stack.append(node)
            on_stack.add(node)
            for edge in self.adj[node]:
                if edges_only_blocking and not edge.blocking:
                    continue
                nxt = edge.blocker
                if nxt not in index:
                    visit(nxt)
                    low[node] = min(low[node], low[nxt])
                elif nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if low[node] == index[node]:
                component = []
                while True:
                    popped = stack.pop()
                    on_stack.discard(popped)
                    component.append(popped)
                    if popped == node:
                        break
                components.append(component)

        for node in list(self.adj):
            if node not in index:
                visit(node)
        return components

    def _has_self_loop(self, node: str, blocking_only: bool) -> bool:
        for edge in self.adj.get(node, ()):
            if edge.blocker == node and (not blocking_only or edge.blocking):
                return True
        return False

    def cyclic_components(self, blocking_only: bool) -> List[List[str]]:
        components = []
        for component in self._tarjan(blocking_only):
            if len(component) > 1 or self._has_self_loop(component[0], blocking_only):
                components.append(component)
        return components

    def connected_components(self, blocking_only: bool) -> List[List[str]]:
        undirected: Dict[str, set] = {node: set() for node in self.adj}
        for node, edges in self.adj.items():
            for edge in edges:
                if blocking_only and not edge.blocking:
                    continue
                undirected.setdefault(node, set()).add(edge.blocker)
                undirected.setdefault(edge.blocker, set()).add(node)
        seen: set = set()
        components: List[List[str]] = []
        for root in undirected:
            if root in seen:
                continue
            stack = [root]
            component = []
            seen.add(root)
            while stack:
                node = stack.pop()
                component.append(node)
                for nxt in undirected[node]:
                    if nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
            components.append(component)
        return components


def _causally_ordered(recording: Recording, first: str, second: str) -> bool:
    """True when ``first`` is explicitly proven to happen before ``second``."""

    edges: Dict[str, List[str]] = {}
    for call in recording.calls:
        for src, dst in call.causes:
            edges.setdefault(src, []).append(dst)
    reachable = {first}
    frontier = [first]
    while frontier:
        current = frontier.pop()
        for nxt in edges.get(current, ()):
            if nxt not in reachable:
                reachable.add(nxt)
                frontier.append(nxt)
    return second in reachable


def _ordering(
    recording: Recording, holder: CallRecord, waiter: CallRecord
) -> str:
    """Tri-state ordering of ``holder`` relative to ``waiter``.

    Returns ``'proven'`` when the holder is proven to occupy the slot first,
    ``'later'`` when the same-domain clocks conclusively place it after the
    waiter, and ``'unknown'`` when cross-domain evidence is missing.
    """

    if holder.clock_domain and waiter.clock_domain:
        if holder.clock_domain == waiter.clock_domain:
            return 'proven' if holder.start_tick <= waiter.start_tick else 'later'
    if _causally_ordered(recording, holder.start_event(), waiter.start_event()):
        return 'proven'
    return 'unknown'


def _active_in_slice(call: CallRecord, start: Optional[float], end: Optional[float]) -> bool:
    # Started before the slice closes and has not finished before it opens.
    if end is not None and call.start_tick >= end:
        return False
    if start is not None and call.end_tick is not None and call.end_tick <= start:
        return False
    return True


def _build_graph(
    recording: Recording,
    model: SchedulingModel,
    active: Dict[str, CallRecord],
    events: Dict[str, EventRecord],
    start: Optional[float],
    end: Optional[float],
) -> Tuple[_Graph, List[Tuple[str, FrozenSet[str]]]]:
    graph = _Graph()
    pending_items: List[Tuple[str, FrozenSet[str]]] = []
    all_calls = {call.call_id: call for call in recording.calls}

    def pending(reason: str, *nodes: str) -> None:
        pending_items.append((reason, frozenset(nodes)))

    for call in active.values():
        graph.add_node(_call_node(call.call_id))

    # An in-flight call occupies one slot of its target service while its
    # handler executes there. Both sync and async calls hold the target slot;
    # only a sync caller blocks waiting for dispatch/reply.
    holders_by_service: Dict[str, List[str]] = {}
    for call in active.values():
        holders_by_service.setdefault(call.target, []).append(call.call_id)

    for call in active.values():
        call_node = _call_node(call.call_id)
        target = model.get(call.target)

        if target is None:
            pending(
                f'no scheduling model for target service {call.target!r} '
                f'(call {call.call_id})',
                call_node,
            )
            continue

        if target.slots is None:
            pending(
                f'slot count unknown for service {call.target!r} (call {call.call_id})',
                call_node,
            )
            continue

        other_holders = [
            active[holder_id]
            for holder_id in holders_by_service.get(call.target, [])
            if holder_id != call.call_id
        ]
        if target.allows_reentry(call.method):
            # Re-entrant dispatch does not need a free slot, so this edge can
            # never block. The call may still wait on a local event below.
            graph.add_edge(
                call_node,
                _slot_node(call.target),
                f'reentrant callback {call.call_id} allowed into {call.target}',
                blocking=False,
            )
        elif len(other_holders) + 1 > target.slots:
            slot_node = _slot_node(call.target)
            graph.add_node(slot_node)
            ordering = {
                holder.call_id: _ordering(recording, holder, call)
                for holder in other_holders
            }
            proven_holders = [
                holder
                for holder in other_holders
                if ordering[holder.call_id] == 'proven'
            ]
            has_unknown_order = False
            for holder in other_holders:
                if ordering[holder.call_id] == 'unknown':
                    has_unknown_order = True
                    pending(
                        f'slot ordering between {call.call_id} and {holder.call_id} '
                        'spans uncalibrated clocks without a causal edge',
                        call_node,
                        slot_node,
                        _call_node(holder.call_id),
                    )
            if call.call_type is CallType.ASYNC:
                graph.add_edge(
                    call_node,
                    slot_node,
                    f'async call {call.call_id} queues for {call.target} '
                    'slot but never waits',
                    blocking=False,
                )
            elif proven_holders:
                graph.add_edge(
                    call_node,
                    slot_node,
                    f'waiting for free slot on {call.target}',
                    blocking=True,
                )
            elif not has_unknown_order:
                # Every other holder is conclusively later: this call got the
                # slot first and is not queued behind anything.
                continue
            for holder in other_holders:
                if ordering[holder.call_id] != 'proven':
                    continue
                graph.add_edge(
                    slot_node,
                    _call_node(holder.call_id),
                    f'slot held by in-flight call {holder.call_id}',
                    blocking=call.call_type is CallType.SYNC,
                )

        if call.waits_on_event is not None:
            event = events.get(call.waits_on_event)
            event_node = _event_node(call.waits_on_event)
            graph.add_node(event_node)
            wait_is_blocking = True
            if event is None:
                pending(
                    f'call {call.call_id} waits on unknown event {call.waits_on_event}',
                    call_node,
                    event_node,
                )
                continue
            holder_call = (
                all_calls.get(event.holder) if event.holder is not None else None
            )
            holder_ends_inside = holder_call is not None and holder_call.end_tick is not None
            holder_releases_here = holder_ends_inside and _observed_within(
                holder_call.end_tick, start, end
            )
            wait_resolved = event.set_tick is not None or holder_releases_here
            if wait_resolved or call.call_type is CallType.ASYNC:
                wait_is_blocking = False
                graph.add_edge(
                    call_node,
                    event_node,
                    f'waiting on event {event.event_id} (non-blocking for this call)',
                    blocking=False,
                )
            else:
                graph.add_edge(
                    call_node,
                    event_node,
                    f'blocked on local event {event.event_id}',
                    blocking=True,
                )
            if event.holder is None:
                if wait_is_blocking:
                    pending(
                        f'event {event.event_id} has no recorded holder',
                        call_node,
                        event_node,
                    )
                continue
            if holder_call is None:
                graph.add_edge(
                    event_node,
                    _call_node(event.holder),
                    f'event held by {event.holder} (unknown call)',
                    blocking=False,
                )
                continue
            if holder_call.call_id not in active:
                graph.add_edge(
                    event_node,
                    _call_node(event.holder),
                    f'event held by {event.holder} (not active in slice)',
                    blocking=False,
                )
                continue
            graph.add_edge(
                event_node,
                _call_node(event.holder),
                f'event held by call {event.holder}',
                blocking=wait_is_blocking,
            )

    return graph, pending_items


def _chain_for_nodes(graph: _Graph, nodes: FrozenSet[str]) -> Tuple[WaitEdge, ...]:
    chain: List[WaitEdge] = []
    for node in nodes:
        for edge in graph.adj.get(node, ()):
            if edge.blocker in nodes:
                chain.append(edge)
    return tuple(sorted(chain, key=lambda edge: (edge.waiter, edge.blocker, edge.reason)))


def _observed_within(tick: float, start: Optional[float], end: Optional[float]) -> bool:
    """Whether an event at ``tick`` is visible inside slice ``[start, end)``.

    Slices are half-open: a reply/cancel/set at the closing boundary does not
    dissolve the group yet; it changes the verdict starting at the next slice.
    """

    if start is not None and tick < start:
        return False
    if end is not None and tick >= end:
        return False
    return True


def _release_paths(
    nodes: FrozenSet[str],
    active: Dict[str, CallRecord],
    all_calls: Dict[str, CallRecord],
    events: Dict[str, EventRecord],
    start: Optional[float],
    end: Optional[float],
) -> List[str]:
    release_paths: List[str] = []
    for node in nodes:
        call_id = _node_call_id(node)
        if call_id is not None:
            call = active.get(call_id) or all_calls.get(call_id)
            if call is None:
                continue
            if call.reply_tick is not None and _observed_within(call.reply_tick, start, end):
                release_paths.append(f'{call_id}: reply at tick {call.reply_tick}')
            if call.cancel_tick is not None and _observed_within(call.cancel_tick, start, end):
                release_paths.append(f'{call_id}: cancel at tick {call.cancel_tick}')
        if node.startswith('event:'):
            event_id = node[len('event:'):]
            event = events.get(event_id)
            event_is_set = event is not None and event.set_tick is not None
            if event_is_set and _observed_within(event.set_tick, start, end):
                release_paths.append(f'event {event_id} set at tick {event.set_tick}')
            if event is not None and event.holder is not None:
                holder = all_calls.get(event.holder)
                holder_ends = holder is not None and holder.end_tick is not None
                if holder_ends and _observed_within(holder.end_tick, start, end):
                    kind = 'reply' if holder.reply_tick is not None else 'cancel'
                    release_paths.append(
                        f'event {event_id} released when holder {event.holder} '
                        f'{kind} at tick {holder.end_tick}'
                    )
    return release_paths


def _make_group(
    group_seq: int,
    nodes: FrozenSet[str],
    graph: _Graph,
    active: Dict[str, CallRecord],
    all_calls: Dict[str, CallRecord],
    events: Dict[str, EventRecord],
    pending_items: Sequence[Tuple[str, FrozenSet[str]]],
    start: Optional[float],
    end: Optional[float],
    cyclic: bool,
    fully_proven: bool = False,
) -> BlockingGroup:
    group_id = f'G{group_seq:02d}'
    chain = _chain_for_nodes(graph, nodes)
    release_paths = _release_paths(
        nodes, active, all_calls, events, start, end
    )

    non_blocking = [edge for edge in chain if not edge.blocking]
    if non_blocking:
        reasons = '; '.join(sorted({edge.reason for edge in non_blocking}))
        return BlockingGroup(
            group_id,
            nodes,
            chain,
            Verdict.RELEASED,
            f'ring contains non-blocking edges ({reasons}); it cannot hold',
            resolved_by=sorted(release_paths)[0] if release_paths else None,
        )

    if release_paths:
        return BlockingGroup(
            group_id,
            nodes,
            chain,
            Verdict.RELEASED,
            'capacity can be released: ' + '; '.join(sorted(release_paths)),
            resolved_by=sorted(release_paths)[0],
        )

    if fully_proven and cyclic:
        wait_chain = ' -> '.join(
            f'{edge.waiter}[{edge.reason}]{edge.blocker}' for edge in chain
        )
        return BlockingGroup(
            group_id,
            nodes,
            chain,
            Verdict.DEADLOCK,
            f'proven mutual wait with no release branch: {wait_chain}',
        )

    relevant_pending = sorted(
        {reason for reason, item_nodes in pending_items if item_nodes & nodes}
    )
    if relevant_pending:
        return BlockingGroup(
            group_id,
            nodes,
            chain,
            Verdict.PENDING,
            'insufficient scheduling evidence: ' + '; '.join(relevant_pending),
        )

    if cyclic:
        wait_chain = ' -> '.join(
            f'{edge.waiter}[{edge.reason}]{edge.blocker}' for edge in chain
        )
        return BlockingGroup(
            group_id,
            nodes,
            chain,
            Verdict.DEADLOCK,
            f'proven mutual wait with no release branch: {wait_chain}',
        )

    chain_text = ' -> '.join(edge.waiter + '->' + edge.blocker for edge in chain)
    return BlockingGroup(
        group_id,
        nodes,
        chain,
        Verdict.BLOCKED,
        f'proven blocking wait chain (no cycle): {chain_text}',
    )


SliceWindows = List[Tuple[Optional[float], Optional[float], str, FrozenSet[str]]]


def build_slices(recording: Recording) -> SliceWindows:
    """Slice the recording at every graph-changing event.

    Boundaries from every clock domain are merged into one windowing axis so
    that rings spanning machines stay visible. The merged tick order is never
    used as an ordering proof: cross-domain blocking edges carry a pending
    reason unless an explicit causal edge (``CallRecord.causes``) backs them.
    """

    boundaries = set()
    for call in recording.calls:
        boundaries.add(call.start_tick)
        if call.end_tick is not None:
            boundaries.add(call.end_tick)
    for event in recording.events:
        if event.set_tick is not None:
            boundaries.add(event.set_tick)

    ticks = sorted(boundaries)
    if not ticks:
        intervals: List[Tuple[Optional[float], Optional[float]]] = [(None, None)]
    else:
        intervals = [(None, ticks[0])]
        intervals.extend((ticks[idx], ticks[idx + 1]) for idx in range(len(ticks) - 1))
        intervals.append((ticks[-1], None))

    slices: List[Tuple[Optional[float], Optional[float], str, FrozenSet[str]]] = []
    for start, end in intervals:
        active_ids = frozenset(
            call.call_id
            for call in recording.calls
            if _active_in_slice(call, start, end)
        )
        slices.append((start, end, 'global', active_ids))
    return slices


def analyze_recording(
    recording: Recording, model: SchedulingModel
) -> List[TimeSlice]:
    """Analyse a recording and return the proven blocking group per slice."""

    events = {event.event_id: event for event in recording.events}
    all_calls = {call.call_id: call for call in recording.calls}
    results: List[TimeSlice] = []
    group_seq = 0

    for slice_id, (start, end, domain, active_ids) in enumerate(build_slices(recording)):
        active = {
            call.call_id: call
            for call in recording.calls
            if call.call_id in active_ids
        }
        graph, pending_items = _build_graph(
            recording, model, active, events, start, end
        )

        groups: List[BlockingGroup] = []
        covered: set = set()

        proven_cycles = [
            frozenset(component)
            for component in graph.cyclic_components(blocking_only=True)
        ]
        for nodes in proven_cycles:
            group_seq += 1
            group = _make_group(
                group_seq, nodes, graph, active, all_calls, events,
                pending_items, start, end, cyclic=True, fully_proven=True,
            )
            groups.append(group)
            covered.update(nodes)

        full_cycles = [
            frozenset(component)
            for component in graph.cyclic_components(blocking_only=False)
            if not (frozenset(component) <= covered)
        ]
        for nodes in full_cycles:
            group_seq += 1
            group = _make_group(
                group_seq, nodes, graph, active, all_calls, events,
                pending_items, start, end, cyclic=True,
            )
            groups.append(group)
            covered.update(nodes)

        for nodes in graph.connected_components(blocking_only=True):
            node_set = frozenset(nodes)
            if node_set <= covered or len(node_set) < 2:
                continue
            group_seq += 1
            group = _make_group(
                group_seq, node_set, graph, active, all_calls, events,
                pending_items, start, end, cyclic=False,
            )
            groups.append(group)
            covered.update(node_set)

        # A dissolved ring leaves only non-blocking edges. Emit it as a
        # RELEASED group when a reply/cancel/set is visible in this slice so
        # the report states what revoked the previous conclusion.
        for nodes in graph.connected_components(blocking_only=False):
            node_set = frozenset(nodes)
            if node_set <= covered or len(node_set) < 2:
                continue
            release_paths = _release_paths(
                node_set, active, all_calls, events, start, end
            )
            if not release_paths:
                continue
            group_seq += 1
            group = _make_group(
                group_seq, node_set, graph, active, all_calls, events,
                pending_items, start, end, cyclic=False,
            )
            groups.append(group)
            covered.update(node_set)

        for reason, nodes in pending_items:
            if nodes <= covered:
                continue
            group_seq += 1
            groups.append(
                BlockingGroup(
                    f'G{group_seq:02d}',
                    nodes,
                    _chain_for_nodes(graph, nodes),
                    Verdict.PENDING,
                    f'insufficient scheduling evidence: {reason}',
                )
            )
            covered.update(nodes)

        results.append(
            TimeSlice(
                slice_id=slice_id,
                start_tick=start,
                end_tick=end,
                clock_domain=domain,
                active_calls=active_ids,
                groups=tuple(groups),
                pending_reasons=tuple(sorted({reason for reason, _ in pending_items})),
            )
        )
    return results


def summarize(results: Sequence[TimeSlice]) -> str:
    """Render a human-readable per-slice report."""

    lines: List[str] = []
    for time_slice in results:
        interval = f'[{time_slice.start_tick}, {time_slice.end_tick})'
        lines.append(
            f'slice {time_slice.slice_id} domain={time_slice.clock_domain} '
            f'interval={interval} active={sorted(time_slice.active_calls)}'
        )
        for reason in time_slice.pending_reasons:
            lines.append(f'  pending evidence: {reason}')
        if not time_slice.groups:
            lines.append('  no blocking group')
            continue
        for group in time_slice.groups:
            members = ', '.join(sorted(group.members))
            lines.append(
                f'  {group.group_id} {group.verdict.value}: {members} -- {group.reason}'
            )
            for edge in group.chain:
                flag = 'blocking' if edge.blocking else 'non-blocking'
                lines.append(
                    f'    chain: {edge.waiter} -> {edge.blocker} [{flag}] {edge.reason}'
                )
    return '\n'.join(lines)
