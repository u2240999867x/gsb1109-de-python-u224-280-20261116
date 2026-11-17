"""
In-memory SMB2 request identity/credit ledger tests.

These tests never touch a real shared folder. They use synthetic SMB2 messages
with the real client state machine to verify:

  * MessageId ranges are allocated by actual CreditCharge, not by task count.
  * Pending -> final async transitions stay associated with the same identity.
  * Success, failure and incomplete requests are individually queryable.
  * Cancel/timeout only affect their own request; late completions are inert.
  * Reconnect cannot let a reused MessageId claim an old response.
  * Duplicate completions never release a second credit reservation.
"""

import struct

import pytest

from smb import smb2_constants as sc
from smb.request_ledger import (SMB2RequestLedger, RequestStatus,
                                MessageIdAllocator, MessageIdRangeConflict,
                                InsufficientCredits, credit_charge_for_size)
from tests.in_memory_smb2 import (InMemorySMB, _mount_file, _message_by_command,
                                  _SyntheticResponse)


SESSION_ID = 0x1122334455667788
FILE_ID = b'\xAA' * 16


def _boot(retrieved=b'hello world', max_read_size=64 * 1024):
    smb = InMemorySMB()
    out = _mount_file(smb, retrieved, max_read_size=max_read_size)
    _respond(smb, _message_by_command(smb, sc.SMB2_COM_CREATE), status=0)
    _handle_query_info(smb, len(retrieved))
    return smb, out


def _respond(smb, message, status=0, async_id=0, data=b''):
    smb._updateState_SMB2(_SyntheticResponse(message, status=status,
                                             async_id=async_id, data=data))


def _handle_query_info(smb, file_size):
    query = _message_by_command(smb, sc.SMB2_COM_QUERY_INFO)
    response = _SyntheticResponse(query, status=0)
    response.payload = _QueryInfoPayload(file_size)
    smb._updateState_SMB2(response)


class _QueryInfoPayload:
    def __init__(self, file_size):
        self.data = struct.pack('<QQ', file_size, file_size)
        self.data_length = len(self.data)
        self.fid = FILE_ID
        self.file_attributes = 0x80


def _read_requests(smb):
    return [message for message in smb.messages
            if message.command == sc.SMB2_COM_READ]


def _drive_reads(smb, data, chunk_sizes=None):
    """Complete all READ requests, asserting per-segment delivery."""
    delivered = 0
    index = 0
    while True:
        reads = _read_requests(smb)
        if index >= len(reads):
            break
        message = reads[index]
        requested = message.payload.read_len
        chunk = data[delivered:delivered + requested]
        _respond(smb, message, status=0, data=chunk)
        delivered += len(chunk)
        index += 1
        if delivered >= len(data):
            break
    return delivered


def test_message_ids_follow_credit_charge_not_task_count():
    allocator = MessageIdAllocator(start_mid=1, max_mid=0xFFFFFFFF)

    first = allocator.allocate(credit_charge_for_size(64 * 1024))
    second = allocator.allocate(credit_charge_for_size(3 * 64 * 1024))
    third = allocator.allocate(credit_charge_for_size(64 * 1024 + 1))

    assert first == 1
    assert second == 2
    assert third == 5           # 2..4 used by the 3-credit request
    assert (third, third + 1) == (5, 6)

    ranges = [(start, start + charge - 1) for start, end, charge, _ in allocator.reserved_ranges]
    assert ranges == [(1, 1), (2, 4), (5, 6)]


def test_in_flight_ranges_never_overlap():
    ledger = SMB2RequestLedger(initial_credits=64)
    requests = [
        ledger.register(size=1),
        ledger.register(size=64 * 1024 * 2),
        ledger.register(size=64 * 1024 * 4),
        ledger.register(size=7),
    ]

    intervals = sorted(record.mid_range for record in requests)
    for (start, end), (next_start, next_end) in zip(intervals, intervals[1:]):
        assert end < next_start

    assert requests[1].mid_range == (2, 3)
    assert requests[2].mid_range == (4, 7)


def test_pending_final_share_identity_and_bytes():
    ledger = SMB2RequestLedger(initial_credits=32)
    record = ledger.register(command=sc.SMB2_COM_READ, size=64 * 1024,
                             session_id=SESSION_ID)

    ledger.mark_pending(record, async_id=0xCAFE, session_id=SESSION_ID)
    assert record.status == RequestStatus.ASYNC_PENDING
    assert record.async_id == 0xCAFE
    assert ledger.available_credits == 31

    ledger.complete(record, status=0, delivered_bytes=4096,
                    async_id=0xCAFE, session_id=SESSION_ID)
    assert record.status == RequestStatus.SUCCESS
    assert record.delivered_bytes == 4096
    assert record.async_id == 0xCAFE
    assert ledger.available_credits == 32


def test_success_failure_and_incomplete_are_queryable():
    ledger = SMB2RequestLedger(initial_credits=16)
    ok = ledger.register(size=1)
    failed = ledger.register(size=1)
    waiting = ledger.register(size=1)

    ledger.complete(ok, status=0, delivered_bytes=10)
    ledger.complete(failed, status=0xC0000022, delivered_bytes=0)

    assert ledger.successful_requests() == [ok]
    assert ledger.failed_requests() == [failed]
    assert ledger.by_status('success') == [ok]
    assert ledger.by_status('failed') == [failed]
    assert ledger.by_status(RequestStatus.PENDING) == [waiting]
    assert ledger.by_status('incomplete') == [waiting]
    assert ok.to_dict()['mid_range'] == ok.mid_range
    assert failed.response_status == 0xC0000022


def test_cancel_and_timeout_only_affect_their_own_request():
    ledger = SMB2RequestLedger(initial_credits=16)
    cancelled = ledger.register(size=64 * 1024)
    timed_out = ledger.register(size=64 * 1024)
    survivor = ledger.register(size=64 * 1024)

    ledger.cancel(cancelled)
    ledger.timeout(timed_out)

    assert cancelled.status == RequestStatus.CANCELLED
    assert timed_out.status == RequestStatus.TIMED_OUT
    assert survivor.status == RequestStatus.PENDING

    # Each terminal transition released exactly one reservation.
    assert ledger.available_credits == 15
    assert len(ledger.allocator) == 1
    assert ledger.allocator.is_free(cancelled.mid, cancelled.credit_charge)
    assert ledger.allocator.is_free(timed_out.mid, timed_out.credit_charge)
    assert not ledger.allocator.is_free(survivor.mid, survivor.credit_charge)

    # Late completion of the cancelled request is inert.
    assert ledger.complete(cancelled, status=0, delivered_bytes=99) is None
    assert cancelled.delivered_bytes == 0
    assert ledger.available_credits == 15


def test_duplicate_completion_does_not_release_a_second_reservation():
    ledger = SMB2RequestLedger(initial_credits=8)
    record = ledger.register(size=64 * 1024)   # 1 credit

    ledger.complete(record, status=0, delivered_bytes=100)
    assert ledger.available_credits == 8

    ledger.complete(record, status=0, delivered_bytes=100)
    ledger.complete(record, status=0xC0000001)

    assert record.duplicate_completions == 2
    assert ledger.available_credits == 8
    assert record.delivered_bytes == 100
    assert record.status == RequestStatus.SUCCESS


def test_budget_is_enforced_by_actual_charge():
    ledger = SMB2RequestLedger(initial_credits=3)
    ledger.register(size=64 * 1024 * 2)

    with pytest.raises(InsufficientCredits):
        ledger.register(size=64 * 1024 * 2)

    assert ledger.available_credits == 1


def test_explicit_mid_range_cannot_overlap():
    ledger = SMB2RequestLedger(initial_credits=64)
    ledger.register(mid=10, credit_charge=4)

    with pytest.raises(MessageIdRangeConflict):
        ledger.register(mid=12, credit_charge=2)


def test_reconnect_same_mid_cannot_claim_old_response():
    ledger = SMB2RequestLedger(initial_credits=16)
    old = ledger.register(mid=5, credit_charge=2, session_id=111)
    old_mid, old_generation = old.mid, old.generation

    ledger.begin_generation(reset_mid=True, abort_active=True)
    assert old.status == RequestStatus.ABORTED
    assert ledger.available_credits == 16

    fresh = ledger.register(mid=old_mid, credit_charge=2, session_id=222)
    assert fresh.generation != old_generation

    # Delivers only to the new generation's record.
    applied = ledger.handle_message(mid=old_mid, status=0x0, session_id=222,
                                    generation=ledger.generation,
                                    delivered_bytes=64)
    assert applied is fresh
    assert fresh.delivered_bytes == 64
    assert old.delivered_bytes == 0
    assert old.duplicate_completions == 0

    # A stale response stamped with the old generation is rejected outright.
    stale = ledger.handle_message(mid=old_mid, status=0x0, session_id=111,
                                  generation=old_generation,
                                  delivered_bytes=64)
    assert stale is None
    assert old.delivered_bytes == 0


def test_full_multisegment_read_delivers_each_slice_and_balanced_credits():
    data = bytes((index * 7) & 0xFF for index in range(300 * 1024))
    smb, out = _boot(retrieved=data, max_read_size=64 * 1024)

    initial = smb.request_ledger.initial_credits
    reads = _read_requests(smb)
    assert reads, 'expected at least one READ request'

    delivered = 0
    seen = []
    while delivered < len(data):
        reads = _read_requests(smb)
        message = reads[len(seen)]
        seen.append((message.mid, message.credit_charge, message.payload.read_len))

        assert len(smb.request_ledger.records) >= 3 + len(seen) - 1
        pending = smb.request_ledger.find(mid=message.mid, active_only=True)
        assert pending is not None
        assert pending.credit_charge == message.credit_charge

        chunk = data[delivered:delivered + message.payload.read_len]
        _respond(smb, message, status=0, data=chunk)
        delivered += len(chunk)

        record = smb.request_ledger.find(mid=message.mid)
        assert record.status == RequestStatus.SUCCESS
        assert record.delivered_bytes == len(chunk)

    assert delivered == len(data)
    assert out.getvalue() == data
    assert [length for _, _, length in seen] == [64 * 1024] * 4 + [300 * 1024 - 4 * 64 * 1024]

    # Only the trailing CLOSE request may still be outstanding.
    outstanding = smb.request_ledger.pending_requests()
    assert outstanding and all(record.command == sc.SMB2_COM_CLOSE
                               for record in outstanding)

    # While CLOSE is in flight, exactly its one credit is still reserved.
    assert smb.request_ledger.available_credits == initial - len(outstanding)

    close = _message_by_command(smb, sc.SMB2_COM_CLOSE)
    _respond(smb, close, status=0)

    assert smb.request_ledger.available_credits == initial
    assert smb.request_ledger.pending_requests() == []


def test_async_pending_then_final_in_real_read_path():
    data = b'0123456789' * 100
    smb, out = _boot(retrieved=data)

    read = _read_requests(smb)[0]
    _respond(smb, read, status=0x00000103, async_id=0x1234)

    record = smb.request_ledger.find(mid=read.mid)
    assert record.status == RequestStatus.ASYNC_PENDING
    assert record.async_id == 0x1234
    assert read.mid not in smb.pending_requests
    assert smb.async_requests[0x1234].ledger_record is record

    _respond(smb, read, status=0, async_id=0x1234, data=data)

    record = smb.request_ledger.find(mid=read.mid)
    assert record.status == RequestStatus.SUCCESS
    assert record.delivered_bytes == len(data)
    assert out.getvalue() == data


def test_multi_credit_read_reserves_contiguous_range_on_wire():
    # Negotiate a read size above one credit to exercise multi-MID allocation.
    big_read = 5 * 64 * 1024
    data = b'Z' * big_read
    smb, out = _boot(retrieved=data, max_read_size=big_read)

    read = _read_requests(smb)[0]
    assert read.credit_charge == 5

    record = smb.request_ledger.find(mid=read.mid, active_only=True)
    assert record is not None
    assert record.mid_range == (read.mid, read.mid + 4)
    assert record.message_ids == range(read.mid, read.mid + 5)

    # The CreditCharge field is actually present in the encoded SMB2 header.
    raw = read.encode()
    _, _, charge, _, command, _, _ = struct.unpack('<4sHHIHHI', raw[:20])
    assert charge == 5
    assert command == sc.SMB2_COM_READ

    # No other in-flight request overlaps the range.
    for start, end, charge_value, _ in smb.request_ledger.in_flight_ranges:
        if start == record.mid_start:
            continue
        assert end < record.mid_start or start > record.mid_end

    _respond(smb, read, status=0, data=data)
    assert out.getvalue() == data
    assert smb.request_ledger.find(mid=read.mid).delivered_bytes == big_read
