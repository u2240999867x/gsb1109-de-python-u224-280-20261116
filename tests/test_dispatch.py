# -*- coding: utf-8 -*-
# Copyright: (c) 2021, Jordan Borean (@jborean93) <jborean93@gmail.com>
# MIT License (see LICENSE or https://opensource.org/licenses/MIT)

import uuid

import pytest

import psrpcore
from psrpcore._payload import StreamType, unpack_fragment
from psrpcore.types import PSRPMessageType


def _payload_fragments(payload):
    data = payload.data
    fragments = []
    while data:
        fragment = unpack_fragment(bytearray(data))
        fragments.append(fragment)
        data = data[21 + len(fragment.data) :]

    return fragments


def _queue(
    pool,
    pipeline_id,
    count,
    stream_type=StreamType.default,
    pad=100,
):
    for idx in range(count):
        pool.prepare_message(
            f"{idx}-" + ("x" * pad),
            message_type=PSRPMessageType.PipelineOutput,
            pipeline_id=pipeline_id,
            stream_type=stream_type,
        )


def test_dispatch_policy_defaults_to_fifo():
    client = psrpcore.ClientRunspacePool()
    assert client.dispatch_policy == psrpcore.DispatchPolicy.fifo
    assert client.prompt_response_burst == 4

    client.set_dispatch_policy("prompt_response_first", prompt_response_burst=2)
    assert client.dispatch_policy == psrpcore.DispatchPolicy.prompt_response_first
    assert client.prompt_response_burst == 2

    client.dispatch_policy = "priority"
    assert client.dispatch_policy == psrpcore.DispatchPolicy.prompt_response_first

    client.dispatch_policy = "default"
    assert client.dispatch_policy == psrpcore.DispatchPolicy.fifo

    with pytest.raises(ValueError, match="must be 1 or larger"):
        client.prompt_response_burst = 0

    with pytest.raises(ValueError, match="is not a valid DispatchPolicy"):
        client.dispatch_policy = "invalid"


def test_prompt_response_prioritised_over_earlier_default():
    client = psrpcore.ClientRunspacePool(dispatch_policy=psrpcore.DispatchPolicy.prompt_response_first)
    pipeline_id = uuid.uuid4()
    _queue(client, pipeline_id, 1, StreamType.default)
    _queue(client, None, 1, StreamType.prompt_response)

    first = client.data_to_send()
    assert first is not None
    assert first.stream_type == StreamType.prompt_response
    assert first.pipeline_id is None

    second = client.data_to_send()
    assert second is not None
    assert second.stream_type == StreamType.default
    assert second.pipeline_id == pipeline_id


def test_payload_does_not_mix_targets():
    client = psrpcore.ClientRunspacePool()
    pipeline_id = uuid.uuid4()
    other_id = uuid.uuid4()

    _queue(client, pipeline_id, 1, StreamType.default, pad=300)
    _queue(client, pipeline_id, 1, StreamType.prompt_response, pad=300)
    _queue(client, other_id, 1, StreamType.default, pad=300)

    payload = client.data_to_send()
    assert payload is not None
    assert payload.pipeline_id == pipeline_id
    assert payload.stream_type == StreamType.default
    assert all(f.object_id for f in _payload_fragments(payload))

    payload = client.data_to_send()
    assert payload is not None
    assert payload.pipeline_id == pipeline_id
    assert payload.stream_type == StreamType.prompt_response

    payload = client.data_to_send()
    assert payload is not None
    assert payload.pipeline_id == other_id
    assert payload.stream_type == StreamType.default


@pytest.mark.parametrize("amount", [22, 23, 60, 120, 500])
def test_payload_respects_byte_budget_and_fragment_sequence(amount):
    client = psrpcore.ClientRunspacePool()
    pipeline_id = uuid.uuid4()
    _queue(client, pipeline_id, 2, StreamType.default, pad=200)

    received = {}
    while True:
        payload = client.data_to_send(amount)
        if payload is None:
            break

        assert len(payload.data) <= amount
        for fragment in _payload_fragments(payload):
            fragments = received.setdefault(fragment.object_id, [])
            assert fragment.fragment_id == len(fragments)
            fragments.append(fragment)

    assert len(received) == 2
    for fragments in received.values():
        assert [f.fragment_id for f in fragments] == list(range(len(fragments)))
        assert fragments[0].start
        assert fragments[-1].end
        assert b"".join(f.data for f in fragments)


def test_prompt_response_does_not_starve_default():
    client = psrpcore.ClientRunspacePool(
        dispatch_policy=psrpcore.DispatchPolicy.prompt_response_first,
        prompt_response_burst=2,
    )
    pipeline_id = uuid.uuid4()

    _queue(client, pipeline_id, 3, StreamType.default)
    _queue(client, None, 5, StreamType.prompt_response)

    stream_order = []
    while True:
        payload = client.data_to_send(60)
        if payload is None:
            break

        if not stream_order or stream_order[-1] != payload.stream_type:
            stream_order.append(payload.stream_type)

    assert StreamType.prompt_response in stream_order
    assert StreamType.default in stream_order

    # Normal data must be interleaved, not sent only after every host response.
    first_default = stream_order.index(StreamType.default)
    assert first_default <= 2


def test_prompt_response_keeps_queue_order_within_pipeline():
    client = psrpcore.ClientRunspacePool(dispatch_policy=psrpcore.DispatchPolicy.prompt_response_first)
    pipeline_id = uuid.uuid4()
    for idx in range(4):
        client.prepare_message(
            f"prompt-{idx}",
            message_type=PSRPMessageType.PipelineHostResponse,
            pipeline_id=pipeline_id,
            stream_type=StreamType.prompt_response,
        )

    seen = []
    while True:
        payload = client.data_to_send()
        if payload is None:
            break

        for fragment in _payload_fragments(payload):
            seen.append(fragment.object_id)

    # Object ids are allocated in queue order so the first fragment of each
    # object must preserve the order the messages were queued in.
    starts = [f.object_id for f in client.sent_fragments if f.start]
    assert starts == seen
    assert starts == sorted(starts)


def test_cancel_pipeline_removes_only_unsent_data():
    client = psrpcore.ClientRunspacePool()
    pipeline_id = uuid.uuid4()
    other_id = uuid.uuid4()

    _queue(client, pipeline_id, 1, StreamType.default, pad=400)
    _queue(client, other_id, 1, StreamType.default, pad=10)

    first = client.data_to_send(60)
    assert first is not None
    assert first.pipeline_id == pipeline_id
    sent = client.get_sent_fragments(pipeline_id)
    assert len(sent) == 1
    assert sent[0].start
    assert not sent[0].end

    removed = client.cancel_pipeline(pipeline_id)
    assert len(removed) == 1
    assert removed[0].pipeline_id == pipeline_id
    assert all(m.pipeline_id != pipeline_id for m in client._send_buffer)

    # The already sent fragment identity is still available for accounting.
    sent_after = client.get_sent_fragments(pipeline_id)
    assert sent_after == sent

    # Other pipelines are untouched.
    payload = client.data_to_send()
    assert payload is not None
    assert payload.pipeline_id == other_id


def test_cancel_via_pipeline_object():
    client = psrpcore.ClientRunspacePool()
    pipeline = psrpcore.ClientPowerShell(client)
    pipeline.add_script("test")
    pipeline.start()
    assert client._send_buffer

    removed = pipeline.cancel()
    assert removed
    assert all(m.pipeline_id == pipeline.pipeline_id for m in removed)
    assert client.data_to_send() is None
    assert pipeline.runspace_pool.get_sent_fragments(pipeline.pipeline_id) == []


def test_partially_sent_message_retains_identity_after_cancel():
    client = psrpcore.ClientRunspacePool()
    pipeline_id = uuid.uuid4()
    _queue(client, pipeline_id, 1, StreamType.default, pad=500)

    payload = client.data_to_send(80)
    assert payload is not None
    fragments = _payload_fragments(payload)
    assert len(fragments) == 1
    object_id = fragments[0].object_id

    client.cancel_pipeline(pipeline_id)
    assert client.data_to_send() is None

    sent = client.get_sent_fragments(pipeline_id)
    assert len(sent) == 1
    assert sent[0].object_id == object_id
    assert sent[0].fragment_id == 0
    assert sent[0].length == 80 - 21
    assert sent[0].stream_type == StreamType.default
    assert client.sent_fragments == sent


def test_prompt_response_waits_for_message_boundary():
    client = psrpcore.ClientRunspacePool(dispatch_policy=psrpcore.DispatchPolicy.prompt_response_first)
    pipeline_id = uuid.uuid4()
    _queue(client, pipeline_id, 1, StreamType.default, pad=1000)

    first = client.data_to_send(80)
    assert first is not None
    assert first.stream_type == StreamType.default
    object_id = _payload_fragments(first)[0].object_id

    _queue(client, pipeline_id, 1, StreamType.default, pad=10)
    _queue(client, None, 1, StreamType.prompt_response, pad=10)

    streams = []
    fragment_objects = []
    while True:
        payload = client.data_to_send(80)
        if payload is None:
            break
        streams.append(payload.stream_type)
        fragment_objects.extend(f.object_id for f in _payload_fragments(payload))

    # The large message is completed with contiguous fragments. Only then is
    # the prompt response dispatched, ahead of the other normal message.
    end_idx = max(i for i, oid in enumerate(fragment_objects) if oid == object_id)
    assert fragment_objects[: end_idx + 1] == [object_id] * (end_idx + 1)
    assert streams[0] == StreamType.default
    assert StreamType.prompt_response in streams
    assert streams[-1] == StreamType.default

    fragments = [f for f in client.sent_fragments if f.object_id == object_id]
    assert len(fragments) > 1
    assert [f.fragment_id for f in fragments] == list(range(len(fragments)))
    assert fragments[-1].end


def test_partial_message_continues_after_policy_change():
    client = psrpcore.ClientRunspacePool()
    pipeline_id = uuid.uuid4()
    _queue(client, pipeline_id, 1, StreamType.default, pad=500)

    first = client.data_to_send(60)
    assert first is not None
    assert first.stream_type == StreamType.default

    client.dispatch_policy = "prompt_response_first"
    second = client.data_to_send(60)
    assert second is not None
    assert second.stream_type == StreamType.default
    fragments = _payload_fragments(second)
    assert fragments[0].fragment_id == 1


def test_cancel_partially_sent_message():
    client = psrpcore.ClientRunspacePool()
    pipeline_id = uuid.uuid4()
    _queue(client, pipeline_id, 1, StreamType.default, pad=500)

    first = client.data_to_send(60)
    assert first is not None
    sent = client.get_sent_fragments(pipeline_id)
    assert len(sent) == 1

    removed = client.cancel_pipeline(pipeline_id)
    assert len(removed) == 1
    assert client.data_to_send() is None
    # The fragment handed to the transport is still accounted for.
    assert client.get_sent_fragments(pipeline_id) == sent


def test_data_to_send_policy_override():
    client = psrpcore.ClientRunspacePool()
    pipeline_id = uuid.uuid4()
    _queue(client, pipeline_id, 1, StreamType.default)
    _queue(client, None, 1, StreamType.prompt_response)

    first = client.data_to_send(policy="prompt_response_first")
    assert first is not None
    assert first.stream_type == StreamType.prompt_response

    # The override is for a single call, the configured FIFO policy remains.
    assert client.dispatch_policy == psrpcore.DispatchPolicy.fifo
    second = client.data_to_send()
    assert second is not None
    assert second.stream_type == StreamType.default


def test_host_call_does_not_wait_for_buffered_output():
    from tests.conftest import get_runspace_pair

    client, server = get_runspace_pair()
    server.dispatch_policy = "prompt_response_first"

    c_pipeline = psrpcore.ClientPowerShell(client)
    c_pipeline.add_script("test")
    c_pipeline.start()
    s_pipeline = psrpcore.ServerPipeline(server, c_pipeline.pipeline_id)
    server.receive_data(client.data_to_send())
    server.next_event()
    s_pipeline.start()
    server.data_to_send()

    for idx in range(10):
        s_pipeline.write_output("x" * 200)
    s_pipeline.host_call(psrpcore.types.HostMethodIdentifier.ReadLine)
    for idx in range(10):
        s_pipeline.write_output("y" * 200)

    events = []
    while True:
        payload = server.data_to_send(120)
        if payload is None:
            break
        client.receive_data(payload)
        while True:
            event = client.next_event()
            if event is None:
                break
            events.append(event)

    host_idx = next(i for i, e in enumerate(events) if isinstance(e, psrpcore.PipelineHostCallEvent))
    output_idx = [i for i, e in enumerate(events) if isinstance(e, psrpcore.PipelineOutputEvent)]

    # The host call must be delivered before the output that was queued after
    # it, and must not wait for every buffered output object.
    assert output_idx[-1] > host_idx
    assert sum(1 for i in output_idx if i > host_idx) >= 1
    assert host_idx < len(events) - 1


def test_prompt_response_burst_enforced():
    client = psrpcore.ClientRunspacePool(
        dispatch_policy=psrpcore.DispatchPolicy.prompt_response_first,
        prompt_response_burst=2,
    )
    pipeline_id = uuid.uuid4()
    _queue(client, pipeline_id, 3, StreamType.default, pad=10)
    _queue(client, None, 6, StreamType.prompt_response, pad=10)

    order = []
    while True:
        payload = client.data_to_send()
        if payload is None:
            break
        order.append(payload.stream_type)

    assert order[:3] == [
        StreamType.prompt_response,
        StreamType.prompt_response,
        StreamType.default,
    ]
    assert order.count(StreamType.prompt_response) == 6
    assert order.count(StreamType.default) == 3


def test_sent_fragment_fields():
    client = psrpcore.ClientRunspacePool()
    pipeline_id = uuid.uuid4()
    _queue(client, pipeline_id, 1, StreamType.prompt_response, pad=10)

    payload = client.data_to_send()
    fragment = _payload_fragments(payload)[0]
    sent = client.sent_fragments[0]

    assert sent.object_id == fragment.object_id
    assert sent.fragment_id == fragment.fragment_id
    assert sent.pipeline_id == pipeline_id
    assert sent.stream_type == StreamType.prompt_response
    assert sent.start and sent.end
    assert sent.length == len(fragment.data)

    client.clear_sent_fragments()
    assert client.sent_fragments == []


def test_payload_empty_message_does_not_advance_budget_incorrectly():
    # EndOfPipelineInput has no serialized data but still gets a message
    # header. Sending it must not consume more than the given budget.
    client = psrpcore.ClientRunspacePool()
    pipeline_id = uuid.uuid4()
    client.prepare_message(
        psrpcore.types.EndOfPipelineInput(),
        pipeline_id=pipeline_id,
    )

    payload = client.data_to_send(22)
    assert payload is not None
    assert len(payload.data) == 22
    payload = client.data_to_send(22)
    assert payload is not None
    assert len(payload.data) == 22
