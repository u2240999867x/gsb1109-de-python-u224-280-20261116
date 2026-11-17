# -*- coding: utf-8 -*-
# Copyright: (c) 2021, Jordan Borean (@jborean93) <jborean93@gmail.com>
# MIT License (see LICENSE or https://opensource.org/licenses/MIT)

import uuid

import psrpcore
from psrpcore import _events as events
from psrpcore.types import (
    EndOfPipelineInput,
    HostMethodIdentifier,
    PipelineHostCall,
    PipelineHostResponse,
    PipelineState,
    PSInvocationState,
    PSRPMessageType,
    RunspacePoolHostCall,
    RunspacePoolHostResponse,
    RunspacePoolState,
    RunspacePoolStateMsg,
)

POOL_ID = uuid.UUID(int=1)
PIPELINE_ID = uuid.UUID(int=2)


def pool_state_event(state):
    msg = RunspacePoolStateMsg(RunspaceState=state)
    return events.PSRPEvent.create(PSRPMessageType.RunspacePoolState, msg, POOL_ID, None)


def pipeline_state_event(state, pipeline_id=PIPELINE_ID):
    msg = PipelineState(PipelineState=state)
    return events.PSRPEvent.create(PSRPMessageType.PipelineState, msg, POOL_ID, pipeline_id)


def pipeline_host_call_event(ci, mi=HostMethodIdentifier.WriteLine1, pipeline_id=PIPELINE_ID):
    msg = PipelineHostCall(ci=ci, mi=mi, mp=["input"])
    return events.PSRPEvent.create(PSRPMessageType.PipelineHostCall, msg, POOL_ID, pipeline_id)


def pipeline_host_response_event(ci, mi=HostMethodIdentifier.WriteLine1, pipeline_id=PIPELINE_ID):
    msg = PipelineHostResponse(ci=ci, mi=mi)
    return events.PSRPEvent.create(PSRPMessageType.PipelineHostResponse, msg, POOL_ID, pipeline_id)


def pool_host_call_event(ci, mi=HostMethodIdentifier.GetBufferSize):
    msg = RunspacePoolHostCall(ci=ci, mi=mi, mp=[])
    return events.PSRPEvent.create(PSRPMessageType.RunspacePoolHostCall, msg, POOL_ID, None)


def pool_host_response_event(ci, mi=HostMethodIdentifier.GetBufferSize):
    msg = RunspacePoolHostResponse(ci=ci, mi=mi)
    return events.PSRPEvent.create(PSRPMessageType.RunspacePoolHostResponse, msg, POOL_ID, None)


def end_of_input_event(pipeline_id=PIPELINE_ID):
    msg = EndOfPipelineInput()
    return events.PSRPEvent.create(PSRPMessageType.EndOfPipelineInput, msg, POOL_ID, pipeline_id)


def record(events_iter, connection="conn-1"):
    return [psrpcore.RecordedEvent(connection, e) for e in events_iter]


def test_normal_stop_recording():
    records = record(
        [
            pool_state_event(RunspacePoolState.Opening),
            pool_state_event(RunspacePoolState.Opened),
            pipeline_state_event(PSInvocationState.Running),
            pipeline_host_call_event(1),
            pipeline_host_response_event(1),
            end_of_input_event(),
            pipeline_state_event(PSInvocationState.Completed),
            pool_state_event(RunspacePoolState.Closing),
            pool_state_event(RunspacePoolState.Closed),
        ]
    )

    report = psrpcore.check_recording(records)

    assert report.is_consistent
    assert report.outcome == psrpcore.RecordingOutcome.NormalStop
    assert report.gaps == []
    assert report.earliest_contradiction is None
    assert "Normal stop" in report.explain()


def test_failed_pool_recording():
    records = record(
        [
            pool_state_event(RunspacePoolState.Opened),
            pipeline_state_event(PSInvocationState.Running),
            pool_state_event(RunspacePoolState.Broken),
        ]
    )

    report = psrpcore.check_recording(records)

    assert report.is_consistent
    assert report.outcome == psrpcore.RecordingOutcome.Failed
    # The in-flight pipeline never got a terminal state - evidence gap.
    assert len(report.gaps) == 1
    assert "no terminal state was recorded" in report.gaps[0].message
    assert "Failure" in report.explain()


def test_failed_pipeline_recording():
    records = record(
        [
            pool_state_event(RunspacePoolState.Opened),
            pipeline_state_event(PSInvocationState.Running),
            end_of_input_event(),
            pipeline_state_event(PSInvocationState.Failed),
            pool_state_event(RunspacePoolState.Closing),
            pool_state_event(RunspacePoolState.Closed),
        ]
    )

    report = psrpcore.check_recording(records)

    assert report.is_consistent
    assert report.outcome == psrpcore.RecordingOutcome.Failed
    assert report.gaps == []


def test_disconnected_undetermined_recording():
    records = record(
        [
            pool_state_event(RunspacePoolState.Opened),
            pipeline_state_event(PSInvocationState.Running),
            pool_state_event(RunspacePoolState.Disconnecting),
            pool_state_event(RunspacePoolState.Disconnected),
        ]
    )

    report = psrpcore.check_recording(records)

    assert report.is_consistent
    assert report.outcome == psrpcore.RecordingOutcome.DisconnectedUndetermined
    assert "Disconnected/undetermined" in report.explain()


def test_truncated_recording_is_undetermined():
    records = record([pool_state_event(RunspacePoolState.Opened)])

    report = psrpcore.check_recording(records)

    assert report.is_consistent
    assert report.outcome == psrpcore.RecordingOutcome.DisconnectedUndetermined


def test_reconnect_recording():
    records = record(
        [
            pool_state_event(RunspacePoolState.Opened),
            pool_state_event(RunspacePoolState.Disconnecting),
            pool_state_event(RunspacePoolState.Disconnected),
        ]
    )
    records += record(
        [
            pool_state_event(RunspacePoolState.Connecting),
            pool_state_event(RunspacePoolState.Opened),
            pool_state_event(RunspacePoolState.Closing),
            pool_state_event(RunspacePoolState.Closed),
        ],
        connection="conn-2",
    )

    report = psrpcore.check_recording(records)

    assert report.is_consistent
    assert report.outcome == psrpcore.RecordingOutcome.NormalStop
    assert report.gaps == []


def test_reconnect_without_disconnect_is_evidence_gap():
    records = record([pool_state_event(RunspacePoolState.Opened)])
    records += record(
        [
            pool_state_event(RunspacePoolState.Connecting),
            pool_state_event(RunspacePoolState.Opened),
            pool_state_event(RunspacePoolState.Closing),
            pool_state_event(RunspacePoolState.Closed),
        ],
        connection="conn-2",
    )

    report = psrpcore.check_recording(records)

    assert report.is_consistent
    assert report.outcome == psrpcore.RecordingOutcome.NormalStop
    assert len(report.gaps) == 1
    assert "no disconnect of connection 'conn-1' was recorded" in report.gaps[0].message
    assert "not assumed to be a successful history" in report.explain()


def test_reconnect_opened_without_connecting_is_evidence_gap():
    records = record(
        [
            pool_state_event(RunspacePoolState.Opened),
            pool_state_event(RunspacePoolState.Disconnecting),
            pool_state_event(RunspacePoolState.Disconnected),
        ]
    )
    records += record([pool_state_event(RunspacePoolState.Opened)], connection="conn-2")

    report = psrpcore.check_recording(records)

    assert report.is_consistent
    assert len(report.gaps) == 1
    assert "without a recorded Connecting state" in report.gaps[0].message


def test_event_on_new_connection_after_pool_closed():
    records = record(
        [
            pool_state_event(RunspacePoolState.Opened),
            pool_state_event(RunspacePoolState.Closing),
            pool_state_event(RunspacePoolState.Closed),
        ]
    )
    records += record([pool_state_event(RunspacePoolState.Connecting)], connection="conn-2")

    report = psrpcore.check_recording(records)

    assert not report.is_consistent
    assert report.outcome is None
    assert "terminal state Closed" in report.earliest_contradiction.message


def test_pool_closed_with_in_flight_pipeline():
    records = record(
        [
            pool_state_event(RunspacePoolState.Opened),
            pipeline_state_event(PSInvocationState.Running),
            pool_state_event(RunspacePoolState.Closing),
            pool_state_event(RunspacePoolState.Closed),
        ]
    )

    report = psrpcore.check_recording(records)

    assert not report.is_consistent
    earliest = report.earliest_contradiction
    assert "was still Running" in earliest.message
    assert "input never ended" in earliest.message
    # The contradiction points at the close event and the pipeline's last
    # state event as the conflicting original events.
    assert earliest.events[0] is records[3]
    assert earliest.events[1] is records[1]
    assert "record #3" in report.explain()


def test_pool_closed_with_unanswered_host_call():
    records = record(
        [
            pool_state_event(RunspacePoolState.Opened),
            pipeline_state_event(PSInvocationState.Running),
            pipeline_host_call_event(1),
            end_of_input_event(),
            pipeline_state_event(PSInvocationState.Completed),
            pool_state_event(RunspacePoolState.Closing),
            pool_state_event(RunspacePoolState.Closed),
        ]
    )

    report = psrpcore.check_recording(records)

    assert not report.is_consistent
    earliest = report.earliest_contradiction
    assert "1 host call(s) still unanswered" in earliest.message
    assert records[2] in earliest.events


def test_pool_closed_with_unanswered_pool_host_call():
    records = record(
        [
            pool_state_event(RunspacePoolState.Opened),
            pool_host_call_event(1),
            pool_state_event(RunspacePoolState.Closing),
            pool_state_event(RunspacePoolState.Closed),
        ]
    )

    report = psrpcore.check_recording(records)

    assert not report.is_consistent
    assert "host call(s) still unanswered" in report.earliest_contradiction.message


def test_earliest_contradiction_is_reported_first():
    records = record(
        [
            pool_state_event(RunspacePoolState.Opened),
            pipeline_state_event(PSInvocationState.Running),
            pool_state_event(RunspacePoolState.Closing),
            pool_state_event(RunspacePoolState.Closed),
            pipeline_state_event(PSInvocationState.Completed),
        ]
    )

    report = psrpcore.check_recording(records)

    assert len(report.contradictions) >= 2
    assert report.earliest_contradiction is report.contradictions[0]
    assert report.earliest_contradiction.events[0] is records[3]


def test_backward_pool_transition():
    records = record(
        [
            pool_state_event(RunspacePoolState.Opened),
            pool_state_event(RunspacePoolState.Opening),
        ]
    )

    report = psrpcore.check_recording(records)

    assert not report.is_consistent
    assert "cannot move from Opened to Opening" in report.earliest_contradiction.message


def test_skipped_pool_transition_is_evidence_gap():
    records = record(
        [
            pool_state_event(RunspacePoolState.Opened),
            pool_state_event(RunspacePoolState.Closed),
        ]
    )

    report = psrpcore.check_recording(records)

    assert report.is_consistent
    assert report.outcome == psrpcore.RecordingOutcome.NormalStop
    assert len(report.gaps) == 1
    assert "without the intermediate state(s)" in report.gaps[0].message


def test_pipeline_event_after_terminal_state():
    records = record(
        [
            pool_state_event(RunspacePoolState.Opened),
            pipeline_state_event(PSInvocationState.Running),
            end_of_input_event(),
            pipeline_state_event(PSInvocationState.Completed),
            pipeline_state_event(PSInvocationState.Running),
        ]
    )

    report = psrpcore.check_recording(records)

    assert not report.is_consistent
    assert "after it had already reached Completed" in report.earliest_contradiction.message


def test_duplicate_end_of_input():
    records = record(
        [
            pool_state_event(RunspacePoolState.Opened),
            pipeline_state_event(PSInvocationState.Running),
            end_of_input_event(),
            end_of_input_event(),
        ]
    )

    report = psrpcore.check_recording(records)

    assert not report.is_consistent
    earliest = report.earliest_contradiction
    assert "input was ended more than once" in earliest.message
    assert earliest.events[1] is records[2]


def test_end_of_input_after_terminal_state():
    records = record(
        [
            pool_state_event(RunspacePoolState.Opened),
            pipeline_state_event(PSInvocationState.Running),
            pipeline_state_event(PSInvocationState.Failed),
            end_of_input_event(),
        ]
    )

    report = psrpcore.check_recording(records)

    assert not report.is_consistent
    assert "input ended after it had already reached Failed" in report.earliest_contradiction.message


def test_terminal_pipeline_without_end_of_input_is_evidence_gap():
    records = record(
        [
            pool_state_event(RunspacePoolState.Opened),
            pipeline_state_event(PSInvocationState.Running),
            pipeline_state_event(PSInvocationState.Completed),
            pool_state_event(RunspacePoolState.Closing),
            pool_state_event(RunspacePoolState.Closed),
        ]
    )

    report = psrpcore.check_recording(records)

    assert report.is_consistent
    assert report.outcome == psrpcore.RecordingOutcome.NormalStop
    assert len(report.gaps) == 1
    assert "no end of input was ever recorded" in report.gaps[0].message


def test_host_response_without_call_is_evidence_gap():
    records = record(
        [
            pool_state_event(RunspacePoolState.Opened),
            pipeline_state_event(PSInvocationState.Running),
            pipeline_host_response_event(1),
        ]
    )

    report = psrpcore.check_recording(records)

    assert report.is_consistent
    assert len(report.gaps) == 1
    assert "no matching host call" in report.gaps[0].message


def test_duplicate_host_response():
    records = record(
        [
            pool_state_event(RunspacePoolState.Opened),
            pipeline_state_event(PSInvocationState.Running),
            pipeline_host_call_event(1),
            pipeline_host_response_event(1),
            pipeline_host_response_event(1),
        ]
    )

    report = psrpcore.check_recording(records)

    assert not report.is_consistent
    assert "duplicate pipeline host response" in report.earliest_contradiction.message


def test_host_response_method_mismatch():
    records = record(
        [
            pool_state_event(RunspacePoolState.Opened),
            pipeline_state_event(PSInvocationState.Running),
            pipeline_host_call_event(1, mi=HostMethodIdentifier.WriteLine1),
            pipeline_host_response_event(1, mi=HostMethodIdentifier.ReadLine),
        ]
    )

    report = psrpcore.check_recording(records)

    assert not report.is_consistent
    assert "but the call was for" in report.earliest_contradiction.message


def test_unanswered_host_call_at_end_is_evidence_gap():
    records = record(
        [
            pool_state_event(RunspacePoolState.Opened),
            pipeline_state_event(PSInvocationState.Running),
            pipeline_host_call_event(7),
        ]
    )

    report = psrpcore.check_recording(records)

    assert report.is_consistent
    assert report.outcome == psrpcore.RecordingOutcome.DisconnectedUndetermined
    assert any("ci=7" in g.message and "never answered" in g.message for g in report.gaps)


def test_event_after_pool_closed_on_same_connection():
    records = record(
        [
            pool_state_event(RunspacePoolState.Opened),
            pool_state_event(RunspacePoolState.Closing),
            pool_state_event(RunspacePoolState.Closed),
            pool_host_response_event(1),
        ]
    )

    report = psrpcore.check_recording(records)

    assert not report.is_consistent
    assert "after the pool reached Closed on that connection" in report.earliest_contradiction.message


def test_pipeline_disconnected_and_resumed_on_reconnect():
    records = record(
        [
            pool_state_event(RunspacePoolState.Opened),
            pipeline_state_event(PSInvocationState.Running),
            pipeline_state_event(PSInvocationState.Disconnected),
            pool_state_event(RunspacePoolState.Disconnecting),
            pool_state_event(RunspacePoolState.Disconnected),
        ]
    )
    records += record(
        [
            pool_state_event(RunspacePoolState.Connecting),
            pool_state_event(RunspacePoolState.Opened),
            pipeline_state_event(PSInvocationState.Running),
            end_of_input_event(),
            pipeline_state_event(PSInvocationState.Completed),
            pool_state_event(RunspacePoolState.Closing),
            pool_state_event(RunspacePoolState.Closed),
        ],
        connection="conn-2",
    )

    report = psrpcore.check_recording(records)

    assert report.is_consistent
    assert report.outcome == psrpcore.RecordingOutcome.NormalStop
    assert report.gaps == []


def test_pipeline_resume_without_disconnect_is_evidence_gap():
    records = record(
        [
            pool_state_event(RunspacePoolState.Opened),
            pipeline_state_event(PSInvocationState.Running),
            pool_state_event(RunspacePoolState.Disconnecting),
            pool_state_event(RunspacePoolState.Disconnected),
        ]
    )
    records += record(
        [
            pool_state_event(RunspacePoolState.Connecting),
            pool_state_event(RunspacePoolState.Opened),
            pipeline_state_event(PSInvocationState.Running),
        ],
        connection="conn-2",
    )

    report = psrpcore.check_recording(records)

    assert report.is_consistent
    assert any("no Disconnected state was recorded" in g.message for g in report.gaps)


def test_recorded_event_attributes():
    event = pool_state_event(RunspacePoolState.Opened)
    record_entry = psrpcore.RecordedEvent("conn-1", event)

    assert record_entry.connection == "conn-1"
    assert record_entry.event is event
    assert record_entry.index == -1

    report = psrpcore.check_recording([record_entry])
    assert record_entry.index == 0
    assert report.is_consistent
