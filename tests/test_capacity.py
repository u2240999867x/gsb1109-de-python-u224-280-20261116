# -*- coding: utf-8 -*-
# Copyright: (c) 2021, Jordan Borean (@jborean93) <jborean93@gmail.com>
# MIT License (see LICENSE or https://opensource.org/licenses/MIT)

import uuid

import pytest

import psrpcore
from psrpcore.types import PSInvocationState

from .conftest import get_runspace_pair


def _start_server_pipeline(
    server: psrpcore.ServerRunspacePool,
    client_pipeline: psrpcore.ClientPowerShell,
    pipeline_id=None,
) -> psrpcore.ServerPipeline:
    pipeline = psrpcore.ServerPipeline(server, pipeline_id or client_pipeline.pipeline_id)
    server.receive_data(client_pipeline.runspace_pool.data_to_send())
    server.next_event()
    return pipeline


def test_capacity_view_confirmed_pending_and_admitted():
    client, server = get_runspace_pair(max_runspaces=2)

    assert client.capacity.confirmed_max_runspaces == 2
    assert client.capacity.pending_max_runspaces is None
    assert client.capacity.admitted_pipeline_count == 0

    ci = client.set_max_runspaces(4)
    assert ci is not None
    assert client.max_runspaces == 2
    assert client.capacity.pending_max_runspaces == 4
    assert client.capacity.pending_min_runspaces is None
    assert client.capacity.confirmed_max_runspaces == 2
    assert [c.ci for c in client.pending_capacity_changes] == [ci]

    # A snapshot is a point in time copy.
    snapshot = client.capacity
    server.receive_data(client.data_to_send())
    set_max = server.next_event()
    assert isinstance(set_max, psrpcore.SetMaxRunspacesEvent)
    assert server.capacity.pending_max_runspaces == 4
    assert server.capacity.confirmed_max_runspaces == 2

    server.runspace_availability_response(set_max.ci, True)
    assert server.max_runspaces == 4
    assert server.capacity.pending_max_runspaces is None
    assert client.capacity.pending_max_runspaces == 4
    assert snapshot.pending_max_runspaces == 4
    assert snapshot.max_runspaces == 2

    client.receive_data(server.data_to_send())
    response = client.next_event()
    assert isinstance(response, psrpcore.SetRunspaceAvailabilityEvent)
    assert response.success is True
    assert client.max_runspaces == 4
    assert client.capacity.pending_max_runspaces is None
    assert client.capacity.confirmed_max_runspaces == 4


def test_capacity_change_rejected_keeps_other_change():
    client, server = get_runspace_pair(min_runspaces=2, max_runspaces=4)

    ci_min = client.set_min_runspaces(3)
    ci_max = client.set_max_runspaces(5)
    assert ci_min is not None and ci_max is not None

    server.receive_data(client.data_to_send())
    set_min = server.next_event()
    set_max = server.next_event()
    assert isinstance(set_min, psrpcore.SetMinRunspacesEvent)
    assert isinstance(set_max, psrpcore.SetMaxRunspacesEvent)

    # Rejecting one side must not withdraw the other request.
    server.runspace_availability_response(set_min.ci, False)
    assert server.min_runspaces == 2
    assert server.capacity.pending_min_runspaces is None
    assert server.capacity.pending_max_runspaces == 5

    server.runspace_availability_response(set_max.ci, True)
    assert server.max_runspaces == 5
    assert server.min_runspaces == 2

    client.receive_data(server.data_to_send())
    rejected = client.next_event()
    confirmed = client.next_event()
    assert isinstance(rejected, psrpcore.SetRunspaceAvailabilityEvent)
    assert rejected.success is False
    assert isinstance(confirmed, psrpcore.SetRunspaceAvailabilityEvent)
    assert confirmed.success is True
    assert client.min_runspaces == 2
    assert client.max_runspaces == 5


def test_capacity_interleaved_responses_use_latest_value():
    client, server = get_runspace_pair(max_runspaces=1)

    first = client.set_max_runspaces(5)
    second = client.set_max_runspaces(7)
    assert first is not None and second is not None
    assert client.max_runspaces == 1

    server.receive_data(client.data_to_send())
    set_first = server.next_event()
    set_second = server.next_event()

    # Respond to the older request first, then the newer one.
    server.runspace_availability_response(set_first.ci, True)
    assert server.max_runspaces == 5
    server.runspace_availability_response(set_second.ci, True)
    assert server.max_runspaces == 7

    client.receive_data(server.data_to_send())
    older = client.next_event()
    newer = client.next_event()
    assert older.success is True and newer.success is True
    assert client.max_runspaces == 7
    assert client.capacity.pending_changes == ()


def test_capacity_stale_response_does_not_override_newer():
    client, server = get_runspace_pair(max_runspaces=1)

    first = client.set_max_runspaces(5)
    second = client.set_max_runspaces(7)
    assert first is not None and second is not None

    server.receive_data(client.data_to_send())
    set_first = server.next_event()
    set_second = server.next_event()

    # The newer request is responded to first. The late response for the
    # older request must not move the confirmed value backwards.
    server.runspace_availability_response(set_second.ci, True)
    server.runspace_availability_response(set_first.ci, True)
    assert server.max_runspaces == 7

    client.receive_data(server.data_to_send())
    client.next_event()
    client.next_event()
    assert client.max_runspaces == 7


def test_capacity_min_and_max_must_be_valid_together():
    client, server = get_runspace_pair(min_runspaces=1, max_runspaces=2)

    ci_min = client.set_min_runspaces(4)
    assert ci_min is not None
    server.receive_data(client.data_to_send())
    set_min = server.next_event()

    # Acknowledged, but 4 > 2 so it cannot be adopted yet.
    server.runspace_availability_response(set_min.ci, True)
    assert server.min_runspaces == 1
    assert server.capacity.deferred_min_runspaces == 4
    assert server.capacity.pending_min_runspaces is None

    client.receive_data(server.data_to_send())
    response = client.next_event()
    assert response.success is True
    assert client.min_runspaces == 1
    assert client.capacity.deferred_min_runspaces == 4

    # Raising the maximum makes the pair legal and adopts both sides.
    ci_max = client.set_max_runspaces(5)
    assert ci_max is not None
    server.receive_data(client.data_to_send())
    set_max = server.next_event()
    server.runspace_availability_response(set_max.ci, True)

    assert server.max_runspaces == 5
    assert server.min_runspaces == 4
    assert server.capacity.deferred_changes == ()

    client.receive_data(server.data_to_send())
    client.next_event()
    assert client.max_runspaces == 5
    assert client.min_runspaces == 4
    assert client.capacity.deferred_changes == ()


def test_capacity_scaling_down_keeps_admitted_pipelines():
    client, server = get_runspace_pair(min_runspaces=1, max_runspaces=2)

    first = psrpcore.ClientPowerShell(client)
    first.add_script("first")
    first.start()
    server_pipeline = _start_server_pipeline(server, first)
    server_pipeline.start()

    assert server.capacity.admitted_pipeline_count == 1
    assert server.capacity.available_runspaces == 1

    ci = client.set_max_runspaces(1)
    assert ci is not None
    server.receive_data(client.data_to_send())
    set_max = server.next_event()
    server.runspace_availability_response(set_max.ci, True)

    # Lowering the max does not stop or evict the running pipeline.
    assert server.max_runspaces == 1
    assert server_pipeline.state == PSInvocationState.Running
    assert server.capacity.admitted_pipeline_ids == (first.pipeline_id,)
    assert server.capacity.available_runspaces == 0

    # Only new pipelines are refused.
    second = psrpcore.ClientPowerShell(client)
    second.add_script("second")
    second.start()
    second_server_pipeline = _start_server_pipeline(server, second)
    with pytest.raises(psrpcore.PSRPCoreError, match="capacity is exhausted"):
        second_server_pipeline.start()
    assert second_server_pipeline.state == PSInvocationState.NotStarted
    assert server.capacity.admitted_pipeline_count == 1

    # Once the admitted pipeline finishes the runspace is available again.
    server_pipeline.complete()
    assert server_pipeline.state == PSInvocationState.Completed
    assert server.capacity.admitted_pipeline_count == 0
    second_server_pipeline.start()
    assert second_server_pipeline.state == PSInvocationState.Running


def test_capacity_response_for_other_pool_is_ignored():
    client, server = get_runspace_pair()
    ci = client.set_max_runspaces(5)
    assert ci is not None

    server.receive_data(client.data_to_send())
    set_max = server.next_event()
    assert isinstance(set_max, psrpcore.SetMaxRunspacesEvent)

    # A response addressed to a different Runspace Pool must not update this
    # pool even when the call id matches.
    original_id = server.runspace_pool_id
    try:
        server.runspace_pool_id = uuid.uuid4()
        server.prepare_message(
            psrpcore.types.RunspaceAvailability(SetMinMaxRunspacesResponse=True, ci=set_max.ci)
        )
    finally:
        server.runspace_pool_id = original_id

    client.receive_data(server.data_to_send())
    response = client.next_event()
    assert isinstance(response, psrpcore.SetRunspaceAvailabilityEvent)
    assert response.success is True
    assert client.max_runspaces == 1
    assert client.capacity.pending_max_runspaces == 5


def test_capacity_response_after_reconnect_is_ignored():
    client = psrpcore.ClientRunspacePool(min_runspaces=1, max_runspaces=1)
    server = psrpcore.ServerRunspacePool()
    client.open()
    server.receive_data(client.data_to_send())
    server.next_event()
    server.next_event()
    client.receive_data(server.data_to_send())
    client.next_event()
    client.next_event()
    client.next_event()

    ci = client.set_max_runspaces(5)
    assert ci is not None
    server.receive_data(client.data_to_send())
    set_max = server.next_event()
    assert isinstance(set_max, psrpcore.SetMaxRunspacesEvent)

    # A new connection restarts the call id sequence. Requests for the
    # previous connection must not survive into the new one.
    client.disconnect()
    server.disconnect()
    client.connect()
    server.connect()
    assert client.capacity.pending_changes == ()
    assert client.max_runspaces == 1

    # A late response for the previous connection carries a call id that is
    # not outstanding on this connection and must not change the pool.
    server.prepare_message(
        psrpcore.types.RunspaceAvailability(SetMinMaxRunspacesResponse=True, ci=set_max.ci)
    )
    client.receive_data(server.data_to_send())
    response = client.next_event()
    assert isinstance(response, psrpcore.SetRunspaceAvailabilityEvent)
    assert response.success is True
    assert client.max_runspaces == 1
    assert client.capacity.pending_changes == ()
