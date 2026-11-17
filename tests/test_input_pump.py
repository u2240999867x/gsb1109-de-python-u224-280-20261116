# -*- coding: utf-8 -*-
# Copyright: (c) 2021, Jordan Borean (@jborean93) <jborean93@gmail.com>
# MIT License (see LICENSE or https://opensource.org/licenses/MIT)

import typing

import pytest

import psrpcore
from psrpcore.types import PSInvocationState, PSRPMessageType

from .conftest import get_runspace_pair


def _make_pipeline(
    client: psrpcore.ClientRunspacePool,
) -> psrpcore.ClientPowerShell:
    ps = psrpcore.ClientPowerShell(client, no_input=False)
    ps.add_script("Get-Thing")
    ps.start()
    return ps


def _counting_source(
    count: int,
) -> typing.Tuple[typing.Iterator[int], typing.List[int]]:
    pulled: typing.List[int] = []

    def source() -> typing.Iterator[int]:
        for i in range(count):
            pulled.append(i)
            yield i

    return source(), pulled


def _drain(
    client: psrpcore.ClientRunspacePool,
    server: typing.Optional[psrpcore.ServerRunspacePool] = None,
) -> typing.List[psrpcore.PSRPEvent]:
    events = []
    while True:
        payload = client.data_to_send()
        if payload is None:
            break

        if server is not None:
            server.receive_data(payload)
            while True:
                event = server.next_event()
                if event is None:
                    break
                events.append(event)

    return events


def test_pump_requires_positive_budget() -> None:
    client, _ = get_runspace_pair()
    ps = _make_pipeline(client)

    with pytest.raises(ValueError, match="max_outstanding must be 1 or larger"):
        psrpcore.PipelineInputPump(ps, iter([]), max_outstanding=0)


def test_pump_is_lazy_and_follows_send_budget() -> None:
    client, _ = get_runspace_pair()
    ps = _make_pipeline(client)
    source, pulled = _counting_source(1000)

    sent_types: typing.List[PSRPMessageType] = []
    client.add_send_listener(lambda m: sent_types.append(m.message_type), ps.pipeline_id)

    pump = psrpcore.PipelineInputPump(ps, source, max_outstanding=2048)
    prepared = pump.refill()

    # The source is only read as the send budget allows, never up front.
    assert 0 < prepared == pump.pulled_count == len(pulled) < 1000
    assert pump.prepared_count == pump.pulled_count
    assert pump.sent_count == 0
    assert pump.outstanding >= 2048
    assert not pump.eof_queued

    # As the transport takes data the budget is credited and the pump pulls
    # more objects until the source is exhausted.
    _drain(client)

    assert pump.pulled_count == 1000
    assert pump.prepared_count == 1000
    assert pump.sent_count == 1000
    assert pump.outstanding == 0
    assert pump.eof_queued
    assert pump.eof_delivered
    assert pump.state == psrpcore.InputPumpState.complete
    assert pump.done

    # Exactly one EOF went out behind the input objects.
    assert sent_types.count(PSRPMessageType.PipelineInput) == 1000
    assert sent_types.count(PSRPMessageType.EndOfPipelineInput) == 1


def test_pump_stages_are_tracked_independently() -> None:
    client, _ = get_runspace_pair()
    ps = _make_pipeline(client)
    source, _ = _counting_source(100)

    # Flush the CreatePipeline message so the partial send below only
    # covers pipeline input prepared by the pump.
    client.data_to_send()

    pump = psrpcore.PipelineInputPump(ps, source, max_outstanding=4096)
    pump.refill()
    assert pump.pulled_count == pump.prepared_count
    assert pump.sent_count == 0

    # Hand only part of the buffer to the transport.
    client.data_to_send(1024)
    assert 0 < pump.sent_count < pump.prepared_count
    assert pump.outstanding > 0

    _drain(client)
    assert pump.sent_count == pump.prepared_count == pump.pulled_count


def test_pump_waits_for_started_pipeline() -> None:
    client, _ = get_runspace_pair()
    ps = psrpcore.ClientPowerShell(client, no_input=False)
    ps.add_script("Get-Thing")
    source, pulled = _counting_source(10)

    pump = psrpcore.PipelineInputPump(ps, source)
    assert pump.refill() == 0
    assert len(pulled) == 0
    assert pump.state == psrpcore.InputPumpState.running

    ps.start()
    assert pump.refill() > 0
    assert len(pulled) == pump.pulled_count


def test_pump_pause_and_resume() -> None:
    client, _ = get_runspace_pair()
    ps = _make_pipeline(client)
    source, pulled = _counting_source(100)

    pump = psrpcore.PipelineInputPump(ps, source, max_outstanding=1024)
    pump.refill()
    pulled_before_pause = len(pulled)

    pump.pause()
    assert pump.state == psrpcore.InputPumpState.paused
    assert pump.refill() == 0
    assert len(pulled) == pulled_before_pause

    # Even freeing the transport budget does not pull while paused.
    _drain(client)
    assert len(pulled) == pulled_before_pause

    pump.resume()
    assert pump.state != psrpcore.InputPumpState.paused
    assert len(pulled) > pulled_before_pause

    _drain(client)
    assert pump.state == psrpcore.InputPumpState.complete


def test_pump_eof_is_queued_exactly_once() -> None:
    client, _ = get_runspace_pair()
    ps = _make_pipeline(client)

    pump = psrpcore.PipelineInputPump(ps, iter([1, 2, 3]))
    pump.refill()
    assert pump.eof_queued
    assert pump.state == psrpcore.InputPumpState.drained

    # Further refills do not queue another EOF.
    assert pump.refill() == 0
    eof_messages = [m for m in client._send_buffer if m.message_type == PSRPMessageType.EndOfPipelineInput]
    assert len(eof_messages) == 1

    _drain(client)
    assert pump.eof_delivered
    assert pump.state == psrpcore.InputPumpState.complete
    assert pump.refill() == 0


def test_pump_remote_finished_early() -> None:
    client, _ = get_runspace_pair()
    ps = _make_pipeline(client)
    source, pulled = _counting_source(1000)

    pump = psrpcore.PipelineInputPump(ps, source, max_outstanding=1024)
    pump.refill()
    pulled_before = len(pulled)

    pump.on_pipeline_state(PSInvocationState.Completed)
    assert pump.state == psrpcore.InputPumpState.closed
    assert pump.done
    assert pump.close_reason == "pipeline completed"

    # No further objects are pulled and no EOF is queued, the pulled but
    # unsent state is preserved for reconciliation.
    assert pump.refill() == 0
    assert len(pulled) == pulled_before == pump.pulled_count
    assert not pump.eof_queued
    assert pump.prepared_count <= pump.pulled_count


def test_pump_observes_pipeline_state_change() -> None:
    client, _ = get_runspace_pair()
    ps = _make_pipeline(client)
    source, pulled = _counting_source(1000)

    pump = psrpcore.PipelineInputPump(ps, source, max_outstanding=1024)
    pump.refill()
    pulled_before = len(pulled)

    # The pipeline state is updated as PipelineStateEvents are processed,
    # the pump picks it up on the next refill.
    ps.state = PSInvocationState.Stopped
    assert pump.refill() == 0
    assert pump.state == psrpcore.InputPumpState.closed
    assert len(pulled) == pulled_before


def test_pump_cancel() -> None:
    client, _ = get_runspace_pair()
    ps = _make_pipeline(client)
    source, pulled = _counting_source(1000)

    pump = psrpcore.PipelineInputPump(ps, source, max_outstanding=1024)
    pump.refill()
    pulled_before = len(pulled)

    pump.cancel()
    assert pump.state == psrpcore.InputPumpState.closed
    assert pump.close_reason == "cancelled"
    assert pump.refill() == 0
    assert len(pulled) == pulled_before
    assert not pump.eof_queued


def test_pump_source_error_fails_all_waiters() -> None:
    client, _ = get_runspace_pair()
    ps = _make_pipeline(client)

    def source() -> typing.Iterator[int]:
        yield 1
        raise RuntimeError("source failed")

    pump = psrpcore.PipelineInputPump(ps, source())
    done: typing.List[psrpcore.PipelineInputPump] = []
    pump.add_done_callback(done.append)

    with pytest.raises(RuntimeError, match="source failed"):
        pump.refill()

    assert pump.state == psrpcore.InputPumpState.failed
    assert isinstance(pump.error, RuntimeError)
    assert done == [pump]

    # Late waiters still get a definitive result and the pump stays failed.
    late: typing.List[psrpcore.PipelineInputPump] = []
    pump.add_done_callback(late.append)
    assert late == [pump]
    assert pump.refill() == 0


def test_pump_prepare_error_keeps_pulled_object() -> None:
    client, _ = get_runspace_pair()
    ps = _make_pipeline(client)

    def failing_send(data: typing.Any) -> typing.Any:
        raise ValueError("cannot serialize")

    ps.send = failing_send  # type: ignore[assignment]

    pump = psrpcore.PipelineInputPump(ps, iter(["a", "b", "c"]))
    with pytest.raises(ValueError, match="cannot serialize"):
        pump.refill()

    assert pump.state == psrpcore.InputPumpState.failed
    # The object was pulled from the source but never prepared, it stays
    # available for reconciliation.
    assert pump.pulled_count == 1
    assert pump.prepared_count == 0
    assert pump.unsent == ("a",)


def test_pump_done_callback_fires_exactly_once() -> None:
    client, _ = get_runspace_pair()
    ps = _make_pipeline(client)

    pump = psrpcore.PipelineInputPump(ps, iter(range(100)))
    done: typing.List[psrpcore.PipelineInputPump] = []
    pump.add_done_callback(done.append)
    pump.add_done_callback(done.append)

    pump.cancel()
    pump.cancel()
    pump.close()

    assert done == [pump, pump]


def test_pump_stalled_pipeline_does_not_block_others() -> None:
    client, _ = get_runspace_pair()
    ps_a = _make_pipeline(client)
    ps_b = _make_pipeline(client)
    source_a, pulled_a = _counting_source(100)
    source_b, _ = _counting_source(100)

    pump_a = psrpcore.PipelineInputPump(ps_a, source_a, max_outstanding=1024)
    pump_b = psrpcore.PipelineInputPump(ps_b, source_b, max_outstanding=1024)

    # Pipeline A is stalled but B still makes progress on the same pool.
    pump_a.pause()
    assert pump_b.refill() > 0
    _drain(client)

    assert pump_b.state == psrpcore.InputPumpState.complete
    assert pump_b.sent_count == 100
    assert pump_a.state == psrpcore.InputPumpState.paused
    assert len(pulled_a) == 0

    pump_a.resume()
    _drain(client)
    assert pump_a.state == psrpcore.InputPumpState.complete
    assert pump_a.sent_count == 100


def test_pump_context_manager_closes_pump() -> None:
    client, _ = get_runspace_pair()
    ps = _make_pipeline(client)

    with psrpcore.PipelineInputPump(ps, iter(range(100))) as pump:
        pump.refill()

    assert pump.state == psrpcore.InputPumpState.closed
    assert pump.done


def test_pump_end_to_end_with_server() -> None:
    client, server = get_runspace_pair()
    ps = _make_pipeline(client)
    psrpcore.ServerPipeline(server, ps.pipeline_id)

    pump = psrpcore.PipelineInputPump(ps, iter(range(50)), max_outstanding=512)
    pump.refill()
    events = _drain(client, server)

    inputs = [e for e in events if isinstance(e, psrpcore.PipelineInputEvent)]
    eofs = [e for e in events if isinstance(e, psrpcore.EndOfPipelineInputEvent)]
    assert [int(e.data) for e in inputs] == list(range(50))
    assert len(eofs) == 1
    assert pump.state == psrpcore.InputPumpState.complete
