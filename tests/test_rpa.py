# -*- coding: utf-8 -*-
# Copyright: (c) 2021, Jordan Borean (@jborean93) <jborean93@gmail.com>
# MIT License (see LICENSE or https://opensource.org/licenses/MIT)

import json
import uuid

import psrpcore
from psrpcore._events import PSRPEvent
from psrpcore.types import (
    ErrorCategoryInfo,
    ErrorRecord,
    ErrorRecordMsg,
    NETException,
    PSInvocationState,
    PSObject,
    PSRPMessageType,
    PipelineResultTypes,
    PipelineState,
    WarningRecordMsg,
)

from .conftest import get_runspace_pair

RPID = uuid.UUID(int=0)
PID = uuid.UUID(int=1)


def _event(message_type, ps_object, pipeline_id=PID):
    return PSRPEvent.create(message_type, ps_object, RPID, pipeline_id)


def _output_event(payload, pipeline_id=PID):
    return _event(PSRPMessageType.PipelineOutput, payload, pipeline_id)


def _error_event(pipeline_id=PID):
    record = ErrorRecordMsg(Exception=NETException("boom"), CategoryInfo=ErrorCategoryInfo())
    return _event(PSRPMessageType.ErrorRecord, record, pipeline_id)


def _warning_event(pipeline_id=PID):
    return _event(PSRPMessageType.WarningRecord, WarningRecordMsg("careful"), pipeline_id)


def _state_event(state, reason=None, pipeline_id=PID):
    kwargs = {"PipelineState": state}
    if reason is not None:
        kwargs["ExceptionAsErrorRecord"] = reason
    return _event(PSRPMessageType.PipelineState, PipelineState(**kwargs), pipeline_id)


def _error_record():
    return ErrorRecord(Exception=NETException("boom"), CategoryInfo=ErrorCategoryInfo())


def _powershell(*commands):
    pipeline = psrpcore.PowerShell()
    pipeline.commands = list(commands)
    return pipeline


def test_declaration_distinguishes_native_merged_and_discarded():
    native = psrpcore.Command("Get-Native")
    merged = psrpcore.Command("Get-Merged")
    merged.redirect_error(PipelineResultTypes.Output)
    discarded = psrpcore.Command("Get-Discarded")
    discarded.redirect_warning(PipelineResultTypes.Null)

    declaration = psrpcore.PipelineDeclaration.from_powershell(PID, _powershell(native, merged, discarded))

    assert len(declaration.statements) == 1
    command_decls = declaration.commands
    assert command_decls[0].streams[psrpcore.ObservedStream.Error].target == PipelineResultTypes.none
    assert not command_decls[0].streams[psrpcore.ObservedStream.Error].merged_to_output
    assert not command_decls[0].streams[psrpcore.ObservedStream.Error].discarded

    error_decl = command_decls[1].streams[psrpcore.ObservedStream.Error]
    assert error_decl.merged_to_output and not error_decl.discarded
    assert psrpcore.ObservedStream.Error in declaration.merged_streams

    warning_decl = command_decls[2].streams[psrpcore.ObservedStream.Warning]
    assert warning_decl.discarded and not warning_decl.merged_to_output
    assert psrpcore.ObservedStream.Warning in declaration.discarded_streams


def test_declaration_scopes_redirection_per_statement():
    first = psrpcore.Command("First")
    first.redirect_error(PipelineResultTypes.Null)
    first.end_of_statement = True
    second = psrpcore.Command("Second")

    declaration = psrpcore.PipelineDeclaration.from_powershell(PID, _powershell(first, second))

    assert len(declaration.statements) == 2
    assert psrpcore.ObservedStream.Error in declaration.statements[0].discarded_streams
    assert declaration.statements[1].discarded_streams == frozenset()
    assert declaration.statements[1].merged_streams == frozenset()


def test_declaration_records_merge_unclaimed_as_error_merge():
    command = psrpcore.Command("MergePoint")
    command.merge_unclaimed = True

    declaration = psrpcore.PipelineDeclaration.from_powershell(PID, _powershell(command))

    assert psrpcore.ObservedStream.Error in declaration.statements[0].merged_streams
    assert declaration.output_origin_candidates() == [
        psrpcore.ObservedStream.Output,
        psrpcore.ObservedStream.Error,
    ]


def test_declaration_notes_script_and_shell_redirection_limits():
    script = psrpcore.Command("$ErrorActionPreference = 'Stop'; Get-Thing", is_script=True)
    pipeline = _powershell(script)
    pipeline.redirect_shell_error_to_out = True

    declaration = psrpcore.PipelineDeclaration.from_powershell(PID, pipeline)

    assert any("script" in note for note in declaration.declaration_limits)
    assert any("RedirectShellErrorOutputPipe" in note for note in declaration.declaration_limits)


def test_dedicated_record_event_proves_origin():
    collector = psrpcore.StreamCollector()
    collector.register_declaration(PID, _powershell(psrpcore.Command("Get-Thing")))

    record = collector.handle_event(_error_event())

    assert record is not None
    assert record.observed_stream == psrpcore.ObservedStream.Error
    assert record.proven_origin == psrpcore.ObservedStream.Error
    assert record.origin_is_proven
    assert record.uncertain_origins == tuple()
    assert record.shape_hint == psrpcore.ObservedStream.Error
    assert isinstance(record.payload, ErrorRecord)


def test_error_shaped_output_is_not_proven_error():
    collector = psrpcore.StreamCollector()
    collector.register_declaration(PID, _powershell(psrpcore.Command("Get-Thing")))

    record = collector.handle_event(_output_event(_error_record()))

    assert record is not None
    assert record.observed_stream == psrpcore.ObservedStream.Output
    assert record.shape_hint == psrpcore.ObservedStream.Error
    assert record.proven_origin == psrpcore.ObservedStream.Output
    assert record.uncertain_origins == tuple()
    assert any("shape is not proof" in note for note in record.notes)


def test_merged_stream_makes_output_origin_ambiguous():
    command = psrpcore.Command("Get-Merged")
    command.redirect_error(PipelineResultTypes.Output)
    collector = psrpcore.StreamCollector()
    collector.register_declaration(PID, _powershell(command))

    plain_record = collector.handle_event(_output_event("plain value"))
    error_record = collector.handle_event(_output_event(_error_record()))

    for record in [plain_record, error_record]:
        assert record.observed_stream == psrpcore.ObservedStream.Output
        assert record.proven_origin is None
        assert not record.origin_is_proven
        assert set(record.uncertain_origins) == {
            psrpcore.ObservedStream.Output,
            psrpcore.ObservedStream.Error,
        }

    # Even an explicit ErrorRecord shape cannot upgrade the ambiguous origin.
    assert error_record.shape_hint == psrpcore.ObservedStream.Error
    assert error_record.proven_origin is None


def test_output_without_declaration_is_unknown_with_all_candidates():
    collector = psrpcore.StreamCollector()

    record = collector.handle_event(_output_event(_error_record()))

    assert record is not None
    assert record.proven_origin is None
    assert psrpcore.ObservedStream.Output in record.uncertain_origins
    assert psrpcore.ObservedStream.Error in record.uncertain_origins
    assert any("No pipeline declaration" in note for note in record.notes)


def test_discarded_stream_is_not_fabricated_but_received_events_remain_evidence():
    command = psrpcore.Command("Get-Discarded")
    command.redirect_error(PipelineResultTypes.Null)
    collector = psrpcore.StreamCollector()
    collector.register_declaration(PID, _powershell(command))

    # No error event was received so the collector must not invent an error row.
    assert collector.records(PID, psrpcore.ObservedStream.Error) == []
    assert psrpcore.ObservedStream.Error in collector.declarations[PID].discarded_streams

    # If an error event does arrive (another scope/command did not discard it) it stays hard evidence.
    record = collector.handle_event(_error_event())
    assert record is not None
    assert record.proven_origin == psrpcore.ObservedStream.Error
    assert any("discarded" in note for note in record.notes)


def test_non_stream_events_are_ignored():
    collector = psrpcore.StreamCollector()
    state = _state_event(PSInvocationState.Running)

    assert collector.handle_event(state) is None
    assert collector.records(PID) == []
    assert collector.state_events[PID] == [state]


def test_classification_explicit_failure_beats_error_shaped_output():
    collector = psrpcore.StreamCollector()
    collector.register_declaration(PID, _powershell(psrpcore.Command("Get-Thing")))
    collector.handle_event(_output_event(_error_record()))
    reason = _error_record()
    collector.handle_event(_state_event(PSInvocationState.Failed, reason=reason))

    result = collector.result(PID)

    assert result.failed
    assert result.outcome == psrpcore.CollectionOutcome.Failed
    assert result.failure_reason is reason
    assert len(result.error_shaped_output) == 1
    assert result.classify_failure() == psrpcore.FailureClassification.ExplicitFailure


def test_classification_error_shaped_output_only_is_not_failure():
    collector = psrpcore.StreamCollector()
    collector.register_declaration(PID, _powershell(psrpcore.Command("Get-Thing")))
    collector.handle_event(_output_event(_error_record()))
    collector.handle_event(_state_event(PSInvocationState.Completed))

    result = collector.result(PID)

    assert not result.failed
    assert result.classify_failure() == psrpcore.FailureClassification.ErrorShapedOutputOnly


def test_classification_real_error_records_without_failure():
    collector = psrpcore.StreamCollector()
    collector.register_declaration(PID, _powershell(psrpcore.Command("Get-Thing")))
    collector.handle_event(_error_event())
    collector.handle_event(_state_event(PSInvocationState.Completed))

    result = collector.result(PID)

    assert not result.failed
    assert result.has_error_stream_records
    assert result.classify_failure() == psrpcore.FailureClassification.ErrorRecordsWithoutFailure


def test_classification_discarded_error_stream():
    command = psrpcore.Command("Get-Discarded")
    command.redirect_error(PipelineResultTypes.Null)
    collector = psrpcore.StreamCollector()
    collector.register_declaration(PID, _powershell(command))
    collector.handle_event(_output_event("all good"))
    collector.handle_event(_state_event(PSInvocationState.Completed))

    result = collector.result(PID)

    assert result.observed_counts.get(psrpcore.ObservedStream.Error, 0) == 0
    assert psrpcore.ObservedStream.Error in result.discarded_streams
    assert result.classify_failure() == psrpcore.FailureClassification.Discarded


def test_classification_clean_and_incomplete():
    collector = psrpcore.StreamCollector()
    collector.register_declaration(PID, _powershell(psrpcore.Command("Get-Thing")))

    assert collector.result(PID).classify_failure() == psrpcore.FailureClassification.Incomplete

    collector.handle_event(_state_event(PSInvocationState.Running))
    assert collector.result(PID).classify_failure() == psrpcore.FailureClassification.Incomplete

    collector.handle_event(_state_event(PSInvocationState.Completed))
    result = collector.result(PID)
    assert result.classify_failure() == psrpcore.FailureClassification.Clean
    assert result.outcome == psrpcore.CollectionOutcome.Completed


def test_records_filter_and_sequence():
    collector = psrpcore.StreamCollector()
    collector.register_declaration(PID, _powershell(psrpcore.Command("Get-Thing")))
    first = collector.handle_event(_output_event("one"))
    second = collector.handle_event(_warning_event())
    third = collector.handle_event(_output_event("two"))

    assert [record.sequence for record in collector.records(PID)] == [0, 1, 2]
    assert first.sequence == 0 and second.sequence == 1 and third.sequence == 2
    assert collector.records(PID, psrpcore.ObservedStream.Output) == [first, third]
    assert collector.records(observed_stream=psrpcore.ObservedStream.Warning) == [second]


def test_rows_are_json_serializable():
    command = psrpcore.Command("Get-Merged")
    command.redirect_error(PipelineResultTypes.Output)
    collector = psrpcore.StreamCollector()
    collector.register_declaration(PID, _powershell(command))
    collector.handle_event(_output_event(_error_record()))

    json.dumps(collector.declarations[PID].to_row())
    json.dumps(collector.records(PID)[0].to_row())
    json.dumps(collector.result(PID).error_shaped_output[0].to_row())


def _create_pipeline_event(command):
    client, server = get_runspace_pair()
    c_pipeline = psrpcore.ClientPowerShell(client)
    c_pipeline.add_command(command)
    c_pipeline.start()

    psrpcore.ServerPipeline(server, c_pipeline.pipeline_id)
    server.receive_data(client.data_to_send())
    event = server.next_event()
    assert isinstance(event, psrpcore.CreatePipelineEvent)
    return event


def test_create_pipeline_event_auto_registers_declaration():
    command = psrpcore.Command("Get-Merged")
    command.redirect_error(PipelineResultTypes.Output)
    create_event = _create_pipeline_event(command)

    collector = psrpcore.StreamCollector()
    assert collector.handle_event(create_event) is None
    assert create_event.pipeline_id in collector.declarations
    assert psrpcore.ObservedStream.Error in collector.declarations[create_event.pipeline_id].merged_streams


def test_auto_registration_can_be_disabled():
    create_event = _create_pipeline_event(psrpcore.Command("Get-Thing"))

    collector = psrpcore.StreamCollector(register_create_pipeline=False)
    collector.handle_event(create_event)

    assert collector.declarations == {}


def test_end_to_end_roundtrip_preserves_provenance():
    client, server = get_runspace_pair()
    merged_command = psrpcore.Command("Get-Merged")
    merged_command.redirect_error(PipelineResultTypes.Output)

    c_pipeline = psrpcore.ClientPowerShell(client)
    c_pipeline.add_command(merged_command)
    c_pipeline.start()

    s_pipeline = psrpcore.ServerPipeline(server, c_pipeline.pipeline_id)
    server.receive_data(client.data_to_send())
    create_event = server.next_event()

    collector = psrpcore.StreamCollector()
    collector.handle_event(create_event)

    s_pipeline.start()
    client.receive_data(server.data_to_send())
    client.next_event()  # Running state

    # An ErrorRecord written as ordinary output (e.g. merged via 2>&1) and a native warning.
    s_pipeline.write_output(_error_record())
    s_pipeline.write_warning("careful")
    s_pipeline.complete()

    client.receive_data(server.data_to_send())
    observed = []
    while True:
        event = client.next_event()
        if event is None:
            break
        row = collector.handle_event(event)
        if row is not None:
            observed.append(row)

    output_rows = collector.records(c_pipeline.pipeline_id, psrpcore.ObservedStream.Output)
    warning_rows = collector.records(c_pipeline.pipeline_id, psrpcore.ObservedStream.Warning)

    assert len(output_rows) == 1
    assert output_rows[0].shape_hint == psrpcore.ObservedStream.Error
    assert output_rows[0].proven_origin is None
    assert set(output_rows[0].uncertain_origins) == {
        psrpcore.ObservedStream.Output,
        psrpcore.ObservedStream.Error,
    }
    assert len(warning_rows) == 1
    assert warning_rows[0].proven_origin == psrpcore.ObservedStream.Warning
    assert len(observed) == 2

    result = collector.result(c_pipeline.pipeline_id)
    assert not result.failed
    assert result.classify_failure() == psrpcore.FailureClassification.ErrorShapedOutputOnly


def test_plain_output_has_no_shape_hint():
    collector = psrpcore.StreamCollector()
    collector.register_declaration(PID, _powershell(psrpcore.Command("Get-Thing")))

    string_record = collector.handle_event(_output_event("just text"))
    object_record = collector.handle_event(_output_event(PSObject()))

    assert string_record.shape_hint is None
    assert object_record.shape_hint is None
    assert string_record.proven_origin == psrpcore.ObservedStream.Output
    assert object_record.proven_origin == psrpcore.ObservedStream.Output
