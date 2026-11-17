"""Offline tests for multi-service (CIP service 0x0A) request/response packets.

A multi-service reply can contain a mix of successful and failed embedded
sub-replies.  These tests pin down that each embedded reply is attributed to
its original request, that reply boundaries come only from the validated
offset table, and that count mismatches are reported instead of being silently
turned into empty successes.
"""
from itertools import cycle
from unittest import mock

import pytest

from pycomm3.cip import UINT, DataTypes
from pycomm3.exceptions import ResponseError
from pycomm3.logix_driver import LogixDriver
from pycomm3.packets import (
    MultiServiceRequestPacket,
    MultiServiceResponsePacket,
    ReadTagRequestPacket,
)
from pycomm3.packets.util import tag_request_path

CONNECT_PATH = "192.168.1.100/1"
HEADER = bytes(46)  # padding used so embedded replies parse like standalone responses


def _tag_info():
    return {
        "data_type_name": "DINT",
        "type_class": DataTypes["DINT"],
        "tag_type": "atomic",
        "instance_id": 1,
    }


def _read_request(sequence, name, request_id):
    request = ReadTagRequestPacket(sequence, name, 1, _tag_info(), request_id, True)
    request.request_path = tag_request_path(name, _tag_info(), True)
    return request


def _multi_request(num_requests):
    sequence = cycle(range(1, 65535))
    requests = [
        _read_request(sequence, f"tag{index}", index)
        for index in range(num_requests)
    ]
    return MultiServiceRequestPacket(sequence, requests)


def _dint_reply(value):
    # service reply, reserved, general status 0, extended size 0, DINT type + value
    return bytes([0xCC, 0x00, 0x00, 0x00]) + b"\xc4\x00" + value.to_bytes(
        4, "little", signed=True
    )


def _failed_reply(status=0x05, extended=b"\x00\x00"):
    # service reply, reserved, general status, extended size (1 word), extended word
    return bytes([0xCC, 0x00, status, 0x01]) + extended


def _raw_packet(embeds, count=None, offsets=None):
    num_replies = len(embeds) if count is None else count
    if offsets is None:
        offsets = []
        offset = 2 + (2 * num_replies)
        for embed in embeds:
            offsets.append(offset)
            offset += len(embed)

    data = (
        UINT.encode(num_replies)
        + b"".join(UINT.encode(offset) for offset in offsets)
        + b"".join(embeds)
    )
    # outer multi-service reply, reserved, status 0, extended size 0
    return HEADER + b"\xCC\x00\x00\x00" + data


def _parse(multi_request, embeds, **kwargs):
    return MultiServiceResponsePacket(
        multi_request, _raw_packet(embeds, **kwargs)
    )


def test_mixed_success_and_failure_items_are_associated_to_requests():
    multi = _multi_request(3)
    response = _parse(multi, [_dint_reply(10), _failed_reply(), _dint_reply(30)])

    assert response.is_valid()
    assert len(response.responses) == 3
    assert response.missing_reply_count == 0
    assert response.unmatched_responses == []

    first, second, third = response.responses
    assert first.request.request_id == 0
    assert first.tag == "tag0"
    assert first.is_valid()
    assert first.value == 10

    assert second.request.request_id == 1
    assert not second.is_valid()
    assert second.service_status == 0x05
    assert second.value is None
    # the additional (extended) error code is preserved, not swallowed
    assert "(05, 00)" in second.error

    assert third.request.request_id == 2
    assert third.is_valid()
    assert third.value == 30


def test_truncated_subreply_does_not_swallow_neighbor_bytes():
    multi = _multi_request(2)
    truncated = bytes([0xCC, 0x00, 0x00, 0x00]) + b"\xc4\x00" + b"\xff\xff"
    response = _parse(multi, [truncated, _dint_reply(77)])

    broken, intact = response.responses
    assert not broken.is_valid()
    assert intact.is_valid()
    assert intact.value == 77
    assert intact.request.request_id == 1


def test_first_offset_must_point_at_start_of_reply_data():
    multi = _multi_request(2)
    embeds = [_dint_reply(1), _dint_reply(2)]
    response = _parse(
        multi, embeds, offsets=[99, 99 + len(embeds[0])]
    )

    assert not response.is_valid()
    assert response.responses == []
    assert "first embedded reply" in response.error


def test_offsets_must_be_strictly_monotonic():
    multi = _multi_request(2)
    embeds = [_dint_reply(1), _dint_reply(2)]
    response = _parse(multi, embeds, offsets=[6, 6])

    assert not response.is_valid()
    assert "monotonic" in response.error


def test_offset_cannot_exceed_response_length():
    multi = _multi_request(2)
    embeds = [_dint_reply(1), _dint_reply(2)]
    response = _parse(multi, embeds, offsets=[6, 400])

    assert not response.is_valid()
    assert "exceeds response length" in response.error


def test_truncated_offset_table_is_rejected():
    multi = _multi_request(3)
    raw = HEADER + b"\xCC\x00\x00\x00" + UINT.encode(3) + UINT.encode(8)
    response = MultiServiceResponsePacket(multi, raw)

    assert not response.is_valid()
    assert "does not fit" in response.error


def test_fewer_replies_than_requests_leaves_unanswered_items():
    multi = _multi_request(3)
    response = _parse(multi, [_dint_reply(10), _failed_reply()])

    assert response.missing_reply_count == 1
    assert len(response.responses) == 2
    assert [r.request.request_id for r in response.responses] == [0, 1]


def test_more_replies_than_requests_keeps_unmatched_bytes_out_of_results():
    multi = _multi_request(2)
    response = _parse(
        multi, [_dint_reply(1), _dint_reply(2), _dint_reply(3)]
    )

    # outer multi-service still succeeded: parent status is preserved
    assert response.is_valid()
    assert response.error is None
    assert len(response.responses) == 2
    assert len(response.unmatched_responses) == 1
    assert response.missing_reply_count == 0
    assert "could not be matched" in response.attribution_error
    assert [r.value for r in response.responses] == [1, 2]


def test_zero_replies_reports_every_request_as_missing():
    multi = _multi_request(2)
    response = MultiServiceResponsePacket(
        multi, HEADER + b"\xCC\x00\x00\x00" + UINT.encode(0)
    )

    assert response.is_valid()
    assert response.responses == []
    assert response.missing_reply_count == 2


def test_driver_marks_missing_subrequests_without_fabricating_success():
    plc = LogixDriver(CONNECT_PATH, init_info=False, init_tags=False)
    multi = _multi_request(3)
    response = _parse(multi, [_dint_reply(10), _failed_reply()])

    with mock.patch.object(LogixDriver, "send", return_value=response):
        results = plc._send_requests([multi])

    assert results[0].value == 10
    assert results[0].error is None
    assert results[1].value is None
    assert results[1].error is not None
    # no reply ever arrived for request 2: it must not become an empty success
    assert results[2].value is None
    assert results[2].error is not None


def test_driver_extra_replies_do_not_corrupt_delivered_results():
    plc = LogixDriver(CONNECT_PATH, init_info=False, init_tags=False)
    multi = _multi_request(2)
    response = _parse(
        multi, [_dint_reply(1), _dint_reply(2), _dint_reply(3)]
    )

    with mock.patch.object(LogixDriver, "send", return_value=response):
        results = plc._send_requests([multi])

    assert set(results) == {0, 1}
    assert results[0].value == 1
    assert results[1].value == 2


def test_driver_send_exception_marks_each_subrequest():
    plc = LogixDriver(CONNECT_PATH, init_info=False, init_tags=False)
    multi = _multi_request(2)

    with mock.patch.object(LogixDriver, "send", side_effect=ResponseError("boom")):
        results = plc._send_requests([multi])

    assert set(results) == {0, 1}
    assert all(tag.value is None for tag in results.values())
    assert all(tag.error == "boom" for tag in results.values())


def test_driver_unparseable_parent_propagates_error_to_every_subrequest():
    plc = LogixDriver(CONNECT_PATH, init_info=False, init_tags=False)
    multi = _multi_request(2)
    embeds = [_dint_reply(1), _dint_reply(2)]
    response = MultiServiceResponsePacket(
        multi, _raw_packet(embeds, offsets=[99, 99 + len(embeds[0])])
    )

    assert not response.is_valid()
    with mock.patch.object(LogixDriver, "send", return_value=response):
        results = plc._send_requests([multi])

    assert set(results) == {0, 1}
    assert all(tag.value is None for tag in results.values())
    assert all("first embedded reply" in tag.error for tag in results.values())
