"""Tests for the RPA ring-call blocking analysis."""

from dbus_next.rpa import (
    CallRecord,
    CallType,
    EventRecord,
    Recording,
    SchedulingModel,
    ServiceModel,
    Verdict,
    analyze_recording,
    summarize,
)
from dbus_next.rpa.deadlock import _build_graph, _event_node, _slot_node


def _model(services):
    return SchedulingModel(services={name: spec for name, spec in services})


def _single_slot_services(*names, reentrant=()):
    return _model(
        [
            (
                name,
                ServiceModel(
                    name=name,
                    slots=1,
                    reentrant=name in reentrant,
                    clock_domain='m1',
                ),
            )
            for name in names
        ]
    )


def _verdicts(results):
    return [
        (time_slice.start_tick, time_slice.end_tick, group.verdict)
        for time_slice in results
        for group in time_slice.groups
    ]


def test_proven_deadlock_ring_through_local_events():
    recording = Recording(
        calls=[
            CallRecord('c1', caller='A', target='B', start_tick=1,
                       waits_on_event='e1', clock_domain='m1'),
            CallRecord('c2', caller='B', target='A', start_tick=2,
                       waits_on_event='e2', clock_domain='m1'),
        ],
        events=[
            EventRecord('e1', service='A', holder='c2', clock_domain='m1'),
            EventRecord('e2', service='B', holder='c1', clock_domain='m1'),
        ],
    )
    results = analyze_recording(recording, _single_slot_services('A', 'B'))

    deadlocked = [
        group
        for time_slice in results
        for group in time_slice.groups
        if group.verdict is Verdict.DEADLOCK
    ]
    assert len(deadlocked) == 1
    members = {node for node in deadlocked[0].members if node.startswith('call:')}
    assert members == {'call:c1', 'call:c2'}
    assert all(edge.blocking for edge in deadlocked[0].chain)


def test_async_ring_is_not_deadlock():
    recording = Recording(
        calls=[
            CallRecord('c1', caller='A', target='B', call_type=CallType.ASYNC,
                       start_tick=1, waits_on_event='e1', clock_domain='m1'),
            CallRecord('c2', caller='B', target='A', call_type=CallType.ASYNC,
                       start_tick=2, waits_on_event='e2', clock_domain='m1'),
        ],
        events=[
            EventRecord('e1', service='A', holder='c2', clock_domain='m1'),
            EventRecord('e2', service='B', holder='c1', clock_domain='m1'),
        ],
    )
    results = analyze_recording(recording, _single_slot_services('A', 'B'))

    assert all(
        group.verdict is not Verdict.DEADLOCK
        for time_slice in results
        for group in time_slice.groups
    )
    released = [
        group
        for time_slice in results
        for group in time_slice.groups
        if group.verdict is Verdict.RELEASED
    ]
    assert released
    assert any(not edge.blocking for edge in released[0].chain)


def test_reply_revokes_the_deadlock_conclusion():
    recording = Recording(
        calls=[
            CallRecord('c1', caller='A', target='B', start_tick=1,
                       reply_tick=5, waits_on_event='e1', clock_domain='m1'),
            CallRecord('c2', caller='B', target='A', start_tick=2,
                       waits_on_event='e2', clock_domain='m1'),
        ],
        events=[
            EventRecord('e1', service='A', holder='c2', clock_domain='m1'),
            EventRecord('e2', service='B', holder='c1', clock_domain='m1'),
        ],
    )
    results = analyze_recording(recording, _single_slot_services('A', 'B'))
    verdicts = _verdicts(results)

    assert any(verdict is Verdict.RELEASED for _, _, verdict in verdicts)
    released_group = next(
        group
        for time_slice in results
        for group in time_slice.groups
        if group.verdict is Verdict.RELEASED
    )
    assert released_group.resolved_by is not None
    assert 'c1' in released_group.resolved_by
    assert 'reply' in released_group.resolved_by

    later_slices = [time_slice for time_slice in results if time_slice.start_tick == 5]
    assert later_slices
    assert all(
        group.verdict is Verdict.RELEASED for group in later_slices[0].groups
    )


def test_cancel_releases_blocking_group():
    recording = Recording(
        calls=[
            CallRecord('c1', caller='A', target='B', start_tick=1,
                       waits_on_event='e1', clock_domain='m1'),
            CallRecord('c2', caller='B', target='A', start_tick=2,
                       cancel_tick=4, waits_on_event='e2', clock_domain='m1'),
        ],
        events=[
            EventRecord('e1', service='A', holder='c2', clock_domain='m1'),
            EventRecord('e2', service='B', holder='c1', clock_domain='m1'),
        ],
    )
    results = analyze_recording(recording, _single_slot_services('A', 'B'))
    released = [
        group
        for time_slice in results
        for group in time_slice.groups
        if group.verdict is Verdict.RELEASED
    ]
    assert released and 'cancel' in released[0].resolved_by


def test_missing_scheduling_model_is_pending():
    recording = Recording(
        calls=[
            CallRecord('c1', caller='A', target='B', start_tick=1, clock_domain='m1'),
        ],
    )
    results = analyze_recording(recording, _single_slot_services('A'))
    assert all(
        group.verdict is Verdict.PENDING
        for time_slice in results
        for group in time_slice.groups
    )


def test_unknown_event_holder_is_pending():
    recording = Recording(
        calls=[
            CallRecord('c1', caller='A', target='B', start_tick=1,
                       waits_on_event='e1', clock_domain='m1'),
        ],
        events=[
            EventRecord('e1', service='A', holder=None, clock_domain='m1'),
        ],
    )
    results = analyze_recording(recording, _single_slot_services('A', 'B'))
    pending = [
        group
        for time_slice in results
        for group in time_slice.groups
        if group.verdict is Verdict.PENDING
    ]
    assert pending
    assert any('e1' in group.reason for group in pending)


def test_uncalibrated_clocks_are_pending_without_causal_edge():
    recording = Recording(
        calls=[
            CallRecord('c1', caller='A', target='B', start_tick=1,
                       waits_on_event='e9', clock_domain='d1'),
            CallRecord('c4', caller='A', target='B', start_tick=9,
                       clock_domain='d2'),
        ],
        events=[
            EventRecord('e9', service='A', holder='c4', clock_domain='d1'),
        ],
    )
    results = analyze_recording(recording, _single_slot_services('A', 'B'))
    pending_reasons = [
        group.reason
        for time_slice in results
        for group in time_slice.groups
        if group.verdict is Verdict.PENDING
    ]
    assert any('uncalibrated clocks' in reason for reason in pending_reasons)


def test_explicit_causal_edge_allows_deadlock_verdict_across_domains():
    recording = Recording(
        calls=[
            CallRecord('c1', caller='A', target='B', start_tick=1,
                       waits_on_event='e9', clock_domain='d1'),
            CallRecord(
                'c4',
                caller='A',
                target='B',
                start_tick=9,
                clock_domain='d2',
                causes=frozenset([('c1:start', 'c4:start')]),
            ),
        ],
        events=[
            EventRecord('e9', service='A', holder='c4', clock_domain='d1'),
        ],
    )
    results = analyze_recording(recording, _single_slot_services('A', 'B'))
    assert any(
        group.verdict is Verdict.DEADLOCK
        for time_slice in results
        for group in time_slice.groups
    )


def test_reentrant_callback_cannot_be_called_deadlock():
    recording = Recording(
        calls=[
            CallRecord('c1', caller='A', target='B', start_tick=1, clock_domain='m1'),
            CallRecord('c3', caller='A', target='B', start_tick=2, clock_domain='m1'),
        ],
    )
    results = analyze_recording(
        recording, _single_slot_services('A', 'B', reentrant=('B',))
    )
    assert all(
        group.verdict is not Verdict.DEADLOCK
        for time_slice in results
        for group in time_slice.groups
    )
    edges = [
        edge
        for time_slice in results
        for group in time_slice.groups
        for edge in group.chain
    ]
    if edges:
        assert any(not edge.blocking and 'reentrant' in edge.reason for edge in edges)


def test_reentrant_service_can_still_deadlock_on_local_events():
    recording = Recording(
        calls=[
            CallRecord('c1', caller='A', target='B', start_tick=1,
                       waits_on_event='e5', clock_domain='m1'),
            CallRecord('c3', caller='A', target='B', start_tick=2,
                       waits_on_event='e6', clock_domain='m1'),
        ],
        events=[
            EventRecord('e5', service='B', holder='c3', clock_domain='m1'),
            EventRecord('e6', service='B', holder='c1', clock_domain='m1'),
        ],
    )
    results = analyze_recording(
        recording, _single_slot_services('A', 'B', reentrant=('B',))
    )
    deadlocked = [
        group
        for time_slice in results
        for group in time_slice.groups
        if group.verdict is Verdict.DEADLOCK
    ]
    assert deadlocked
    event_edges = [edge for edge in deadlocked[0].chain if 'event' in edge.reason]
    assert event_edges and all(edge.blocking for edge in event_edges)

    model = _single_slot_services('A', 'B', reentrant=('B',))
    events = {event.event_id: event for event in recording.events}
    active = {call.call_id: call for call in recording.calls}
    graph, _ = _build_graph(recording, model, active, events, None, None)
    slot_edges = [
        edge
        for edges in graph.adj.values()
        for edge in edges
        if edge.blocker == _slot_node('B')
    ]
    assert slot_edges and all(not edge.blocking for edge in slot_edges)
    assert _event_node('e5') in graph.adj and _event_node('e6') in graph.adj


def test_saturated_non_reentrant_service_blocks_queued_call():
    recording = Recording(
        calls=[
            CallRecord('c1', caller='A', target='B', start_tick=1, clock_domain='m1'),
            CallRecord('c3', caller='A', target='B', start_tick=2, clock_domain='m1'),
        ],
    )
    results = analyze_recording(recording, _single_slot_services('A', 'B'))
    saturated_slice = next(
        time_slice for time_slice in results if time_slice.active_calls == frozenset({'c1', 'c3'})
    )
    assert saturated_slice.groups
    group = saturated_slice.groups[0]
    assert any('free slot' in edge.reason and edge.blocking for edge in group.chain)


def test_set_event_releases_wait():
    recording = Recording(
        calls=[
            CallRecord('c1', caller='A', target='B', start_tick=1,
                       waits_on_event='e1', clock_domain='m1'),
            CallRecord('c2', caller='B', target='A', start_tick=2,
                       waits_on_event='e2', clock_domain='m1'),
        ],
        events=[
            EventRecord('e1', service='A', holder='c2', set_tick=4, clock_domain='m1'),
            EventRecord('e2', service='B', holder='c1', clock_domain='m1'),
        ],
    )
    results = analyze_recording(recording, _single_slot_services('A', 'B'))
    assert all(
        group.verdict is not Verdict.DEADLOCK
        for time_slice in results
        for group in time_slice.groups
    )


def test_summarize_lists_every_slice_and_chain():
    recording = Recording(
        calls=[
            CallRecord('c1', caller='A', target='B', start_tick=1,
                       waits_on_event='e1', clock_domain='m1'),
            CallRecord('c2', caller='B', target='A', start_tick=2,
                       waits_on_event='e2', clock_domain='m1'),
        ],
        events=[
            EventRecord('e1', service='A', holder='c2', clock_domain='m1'),
            EventRecord('e2', service='B', holder='c1', clock_domain='m1'),
        ],
    )
    results = analyze_recording(recording, _single_slot_services('A', 'B'))
    report = summarize(results)
    assert 'slice 0' in report
    assert 'chain:' in report
    assert 'deadlock' in report


def test_linear_wait_chain_is_blocked_not_deadlock():
    recording = Recording(
        calls=[
            CallRecord('c7', caller='A', target='B', start_tick=0, clock_domain='m1'),
            CallRecord('c1', caller='A', target='B', start_tick=1,
                       waits_on_event='e1', clock_domain='m1'),
        ],
        events=[
            EventRecord('e1', service='A', holder='c7', clock_domain='m1'),
        ],
    )
    results = analyze_recording(recording, _single_slot_services('A', 'B'))
    blocked = [
        group
        for time_slice in results
        for group in time_slice.groups
        if group.verdict is Verdict.BLOCKED
    ]
    assert blocked
    assert not any(
        group.verdict is Verdict.DEADLOCK
        for time_slice in results
        for group in time_slice.groups
    )


def test_slices_follow_graph_changing_boundaries():
    recording = Recording(
        calls=[
            CallRecord('c1', caller='A', target='B', start_tick=1,
                       reply_tick=4, clock_domain='m1'),
            CallRecord('c2', caller='B', target='A', start_tick=2,
                       cancel_tick=3, clock_domain='m1'),
        ],
    )
    results = analyze_recording(recording, _single_slot_services('A', 'B'))
    boundaries = sorted(
        {
            tick
            for time_slice in results
            for tick in (time_slice.start_tick, time_slice.end_tick)
            if tick is not None
        }
    )
    assert boundaries == [1, 2, 3, 4]
    assert len(results) == 5


def test_capacity_release_dissolves_group_in_later_slice():
    recording = Recording(
        calls=[
            CallRecord('c1', caller='A', target='B', start_tick=1,
                       reply_tick=6, waits_on_event='e1', clock_domain='m1'),
            CallRecord('c2', caller='B', target='A', start_tick=2,
                       waits_on_event='e2', clock_domain='m1'),
        ],
        events=[
            EventRecord('e1', service='A', holder='c2', clock_domain='m1'),
            EventRecord('e2', service='B', holder='c1', clock_domain='m1'),
        ],
    )
    results = analyze_recording(recording, _single_slot_services('A', 'B'))
    before_release = next(
        time_slice
        for time_slice in results
        if time_slice.start_tick == 2 and time_slice.end_tick == 6
    )
    after_release = next(time_slice for time_slice in results if time_slice.start_tick == 6)
    assert any(group.verdict is Verdict.DEADLOCK for group in before_release.groups)
    assert all(group.verdict is Verdict.RELEASED for group in after_release.groups)


def test_cross_domain_pending_becomes_deadlock_with_causal_edge():
    events = [EventRecord('e9', service='A', holder='c4', clock_domain='d1')]
    base_calls = [
        CallRecord('c1', caller='A', target='B', start_tick=1,
                   waits_on_event='e9', clock_domain='d1'),
    ]

    unproven_call = CallRecord(
        'c4', caller='A', target='B', start_tick=9, clock_domain='d2'
    )
    without_edge = Recording(
        calls=base_calls + [unproven_call],
        events=events,
    )
    with_edge = Recording(
        calls=base_calls + [
            CallRecord(
                'c4',
                caller='A',
                target='B',
                start_tick=9,
                clock_domain='d2',
                causes=frozenset([('c1:start', 'c4:start')]),
            )
        ],
        events=events,
    )
    model = _single_slot_services('A', 'B')

    assert all(
        group.verdict is not Verdict.DEADLOCK
        for time_slice in analyze_recording(without_edge, model)
        for group in time_slice.groups
    )
    assert any(
        group.verdict is Verdict.DEADLOCK
        for time_slice in analyze_recording(with_edge, model)
        for group in time_slice.groups
    )
