import struct

from smb import smb2_credit as sc
from smb import smb2_constants as c
from smb.smb2_structs import SMB2Message, SMB2ReadRequest


def make_read(mid, length, credit_charge=0, tid=1):
    message = SMB2Message(SMB2ReadRequest(b'\0' * 16, 0, length))
    message.mid = mid
    message.credit_charge = credit_charge
    message.tid = tid
    return message


def test_credit_charge_rounds_up_at_64k_boundaries():
    assert sc.calculate_credit_charge(0, c.SMB2_COM_READ) == 1
    assert sc.calculate_credit_charge(1, c.SMB2_COM_READ) == 1
    assert sc.calculate_credit_charge(65536, c.SMB2_COM_READ) == 1
    assert sc.calculate_credit_charge(65537, c.SMB2_COM_READ) == 2
    assert sc.calculate_credit_charge(131072, c.SMB2_COM_READ) == 2
    assert sc.calculate_credit_charge(131073, c.SMB2_COM_READ) == 3
    assert sc.credit_charge_for_read(1048576) == 16
    assert sc.calculate_credit_charge(0, c.SMB2_COM_ECHO) == 1


def test_credit_charge_from_message_uses_read_length():
    assert sc.calculate_credit_charge(make_read(1, 131072)) == 2


def test_multi_credit_request_consumes_all_credits():
    ledger = sc.CreditLedger(initial_credits=3)
    result = ledger.submit_read(1, 131072, at=1.0)
    assert result.accepted
    assert result.request.charge == 2
    assert ledger.available == 1
    assert ledger.in_flight == 2


def test_request_queues_until_enough_credits_and_records_dispatch_time():
    ledger = sc.CreditLedger(initial_credits=1)
    first = ledger.submit_read(1, 131072, at=1.0)
    assert first.reason == 'queued'
    assert first.request.state == sc.CreditRequest.QUEUED
    assert first.request.dispatched_at is None

    later = ledger.submit_read(2, 65536, at=2.0)
    assert later.accepted
    assert later.request.state == sc.CreditRequest.IN_FLIGHT

    ledger.grant(2, at=3.0)
    assert first.request.state == sc.CreditRequest.IN_FLIGHT
    assert first.request.dispatched_at == 3.0
    assert ledger.available == 0
    assert ledger.in_flight == 3


def test_small_request_can_use_credits_while_larger_request_waits():
    ledger = sc.CreditLedger(initial_credits=1)
    big = ledger.submit_read(1, 262144, at=1.0)   # 4 credits
    small = ledger.submit_read(2, 65536, at=2.0)  # 1 credit
    assert big.reason == 'queued'
    assert small.reason == 'dispatched'
    assert small.request.dispatched_at == 2.0
    assert big.request.state == sc.CreditRequest.QUEUED

    ledger.grant(3, at=4.0)
    assert big.request.state == sc.CreditRequest.QUEUED
    ledger.grant(1, at=5.0)
    assert big.request.state == sc.CreditRequest.IN_FLIGHT
    assert big.request.dispatched_at == 5.0


def test_response_releases_charge_and_applies_server_credit_response_once():
    ledger = sc.CreditLedger(initial_credits=4)
    ledger.submit_read(1, 131072, at=1.0)
    assert ledger.available == 2 and ledger.in_flight == 2

    first = ledger.response(mid=1, status=0, credit_response=1, at=2.0)
    assert first.accepted
    assert ledger.available == 3
    assert ledger.in_flight == 0

    duplicate = ledger.response(mid=1, status=0, credit_response=1, at=3.0)
    assert not duplicate.accepted
    assert duplicate.duplicate
    assert ledger.available == 3
    assert ledger.in_flight == 0


def test_late_duplicate_response_after_completion_does_not_restore_credits():
    ledger = sc.CreditLedger(initial_credits=2)
    ledger.submit_read(7, 65536, at=1.0)
    ledger.response(mid=7, status=0, at=2.0)
    assert ledger.available == 2
    for t in (3.0, 4.0, 5.0):
        result = ledger.response(mid=7, status=0, credit_response=8, at=t)
        assert not result.accepted
        assert ledger.available == 2
    assert ledger.requests[7].duplicate_responses == 3


def test_async_pending_keeps_charge_then_final_response_completes_by_async_id():
    ledger = sc.CreditLedger(initial_credits=4)
    ledger.submit_read(1, 131072, at=1.0)
    pending = ledger.pending(mid=1, async_id=0xA1, at=2.0)
    assert pending.accepted
    assert ledger.in_flight == 2
    assert ledger.available == 2

    duplicate_pending = ledger.pending(mid=1, async_id=0xA1,
                                       credit_response=3, at=2.5)
    assert not duplicate_pending.accepted
    assert ledger.available == 2

    final = ledger.response(async_id=0xA1, status=0, credit_response=1,
                           at=3.0)
    assert final.accepted
    assert ledger.in_flight == 0
    assert ledger.available == 3
    assert ledger.requests[1].state == sc.CreditRequest.COMPLETED

    duplicate_final = ledger.response(async_id=0xA1, status=0,
                                      credit_response=5, at=4.0)
    assert not duplicate_final.accepted
    assert ledger.available == 3


def test_cancel_releases_charge_and_late_response_is_ignored():
    ledger = sc.CreditLedger(initial_credits=4)
    ledger.submit_read(1, 131072, at=1.0)
    result = ledger.cancel(mid=1, at=2.0)
    assert result.accepted
    assert ledger.available == 4
    assert ledger.in_flight == 0

    late = ledger.response(mid=1, status=sc.STATUS_CANCELLED,
                           credit_response=1, at=3.0)
    assert not late.accepted
    assert ledger.available == 4


def test_cancel_can_keep_charge_until_final_cancelled_response():
    ledger = sc.CreditLedger(initial_credits=2)
    ledger.submit_read(1, 131072, at=1.0)
    result = ledger.cancel(mid=1, at=2.0, release=False)
    assert result.accepted
    assert result.reason == 'cancel_pending'
    assert ledger.in_flight == 2
    assert ledger.available == 0

    final = ledger.response(mid=1, status=sc.STATUS_CANCELLED,
                            credit_response=0, at=3.0)
    assert final.accepted
    assert ledger.in_flight == 0
    assert ledger.available == 0
    assert ledger.requests[1].state == sc.CreditRequest.CANCELLED


def test_explicit_charge_cannot_undercharge_multi_credit_read():
    ledger = sc.CreditLedger(initial_credits=4)
    result = ledger.submit_read(1, 262144, charge=1, at=1.0)
    assert result.charge == 4
    assert ledger.available == 0
    assert ledger.in_flight == 4


def test_cancel_of_queued_request_does_not_release_unspent_credits():
    ledger = sc.CreditLedger(initial_credits=1)
    ledger.submit_read(1, 131072, at=1.0)  # queued, 2 credits needed
    result = ledger.cancel(mid=1, at=2.0)
    assert result.accepted
    assert result.reason == 'cancelled_queued'
    assert ledger.available == 1
    assert ledger.in_flight == 0


def test_disconnect_fails_in_flight_and_ignores_late_responses_and_grants():
    ledger = sc.CreditLedger(initial_credits=4)
    ledger.submit_read(1, 131072, at=1.0)
    ledger.submit_read(2, 65536, at=1.5)
    assert ledger.in_flight == 3
    assert ledger.available == 1

    ledger.disconnect(at=2.0)
    assert ledger.closed
    assert ledger.available == 0
    assert ledger.in_flight == 0
    assert ledger.requests[1].state == sc.CreditRequest.FAILED
    assert ledger.requests[2].state == sc.CreditRequest.FAILED

    late = ledger.response(mid=1, status=0, credit_response=9, at=3.0)
    assert not late.accepted
    assert late.reason == 'disconnected'
    assert ledger.available == 0

    ledger.grant(10, at=4.0)
    assert ledger.available == 0


def test_replay_capture_json_events():
    capture = {
        'initial_credits': 1,
        'events': [
            {'time': 1.0, 'type': 'read', 'mid': 1, 'length': 131072},
            {'time': 1.5, 'type': 'read', 'mid': 2, 'length': 65536},
            {'time': 2.0, 'type': 'pending', 'mid': 1, 'async_id': 99},
            {'time': 2.5, 'type': 'response', 'mid': 2,
             'credits': 1, 'status': 0},
            {'time': 3.0, 'type': 'grant', 'credits': 2},
            {'time': 3.5, 'type': 'response', 'async_id': 99,
             'status': 0, 'credits': 1},
            {'time': 4.0, 'type': 'response', 'mid': 2,
             'credits': 5, 'status': 0},
        ],
    }
    result = sc.replay_capture(capture)
    requests = dict((req['mid'], req) for req in result['requests'])
    assert requests[1]['charge'] == 2
    assert requests[1]['dispatchable_at'] == 3.0
    assert requests[1]['finished_at'] == 3.5
    assert requests[2]['dispatchable_at'] == 1.5
    assert requests[2]['finished_at'] == 2.5
    assert result['summary']['available'] == 2
    assert result['summary']['in_flight'] == 0


def test_replay_compact_capture_shape():
    capture = {
        'initial_credits': 2,
        'reads': [
            {'mid': 1, 'length': 65536, 'at': 0.1},
            {'mid': 2, 'length': 131072, 'at': 0.2},
        ],
        'responses': [
            {'mid': 1, 'at': 0.3, 'credits': 1},
        ],
        'grants': [{'credits': 2, 'at': 0.4}],
    }
    result = sc.replay_capture(capture)
    requests = dict((req['mid'], req) for req in result['requests'])
    assert requests[1]['dispatchable_at'] == 0.1
    # The response returns one credit at 0.3, which is exactly enough for the
    # queued two-credit read.
    assert requests[2]['dispatchable_at'] == 0.3
    assert result['available'] == 2


def test_replay_raw_smb2_read_and_response_frames():
    request = make_read(1, 131072)
    request.raw_data = request.encode()
    request_frame = request.raw_data

    # Build a final response header with MID=1, CreditResponse=2.
    header = struct.pack('<4sHHIHHI', b'\xfeSMB', 64, 0, 0,
                         c.SMB2_COM_READ, 2, 0x01)
    body = struct.pack('<IQIIQ16s', 0, 1, 1234, 1, 0, b'\0' * 16)
    payload = struct.pack('<HBBIII', 17, 80, 0, 0, 0, 0)
    response_frame = header + body + payload

    stream = request_frame + response_frame
    result = sc.replay_capture(stream, initial_credits=2)
    assert result['requests'][0]['mid'] == 1
    assert result['requests'][0]['charge'] == 2
    assert result['requests'][0]['dispatchable_at'] == 0.0
    assert result['requests'][0]['state'] == sc.CreditRequest.COMPLETED
    assert result['available'] == 2
    assert result['in_flight'] == 0


def test_netbios_wrapped_raw_frames_are_replayed():
    request = make_read(1, 131072)
    request.raw_data = request.encode()
    frame = request.raw_data
    session = b'\x00' + struct.pack('>I', len(frame))[1:] + frame
    result = sc.replay_capture(session, initial_credits=2)
    assert result['requests'][0]['charge'] == 2
    assert result['requests'][0]['mid'] == 1
    assert result['available'] == 0
    assert result['in_flight'] == 2


def _pcap_with_packets(packets):
    out = [struct.pack('<IHHIIII', 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1)]
    for at, payload in packets:
        out.append(struct.pack('<IIII', int(at), int((at % 1) * 1e6),
                               len(payload), len(payload)))
        out.append(payload)
    return b''.join(out)


def _ethernet_tcp_packet(src_port, dst_port, payload):
    tcp_header = struct.pack('>HHIIBBHHH', src_port, dst_port, 1, 0,
                             5 << 4, 0x18, 65535, 0, 0)
    ip_total = 20 + len(tcp_header) + len(payload)
    ip_header = struct.pack('>BBHHHBBH4s4s', 0x45, 0, ip_total, 0, 0, 64, 6,
                            0, b'\x0a\x00\x00\x01', b'\x0a\x00\x00\x02')
    ethernet = b'\x00' * 12 + b'\x08\x00'
    return ethernet + ip_header + tcp_header + payload


def test_pcap_with_netbios_stream_is_replayed():
    request = make_read(1, 131072)
    request.raw_data = request.encode()
    frame = request.raw_data
    session = b'\x00' + struct.pack('>I', len(frame))[1:] + frame
    header = struct.pack('<4sHHIHHI', b'\xfeSMB', 64, 0, 0,
                         c.SMB2_COM_READ, 2, 0x01)
    body = struct.pack('<IQIIQ16s', 0, 1, 1234, 1, 0, b'\0' * 16)
    payload = struct.pack('<HBBIII', 17, 80, 0, 0, 0, 0)
    response = b'\x00' + struct.pack('>I', 80)[1:] + header + body + payload

    pcap = _pcap_with_packets([
        (1.0, _ethernet_tcp_packet(50000, 445, session)),
        (1.5, _ethernet_tcp_packet(445, 50000, response)),
    ])
    result = sc.replay_capture(pcap, initial_credits=2)
    assert result['requests'][0]['mid'] == 1
    assert result['requests'][0]['charge'] == 2
    assert result['requests'][0]['finished_at'] == 1.5
    assert result['available'] == 2
    assert result['in_flight'] == 0


def test_fifo_policy_blocks_smaller_request_behind_larger_one():
    ledger = sc.CreditLedger(initial_credits=1, dispatch_policy='fifo')
    big = ledger.submit_read(1, 262144, at=1.0)
    small = ledger.submit_read(2, 65536, at=2.0)
    assert big.reason == 'queued'
    assert small.reason == 'queued'

    ledger.grant(3, at=3.0)
    assert big.request.dispatched_at == 3.0
    assert small.request.dispatched_at is None

    ledger.grant(1, at=4.0)
    assert small.request.dispatched_at == 4.0


def test_header_encoding_carries_credit_charge_and_response():
    message = make_read(1, 131072, credit_charge=2)
    message.credit_re = 7
    raw = message.encode()
    assert struct.unpack_from('<H', raw, 6)[0] == 2
    assert struct.unpack_from('<H', raw, 14)[0] == 7
    decoded = SMB2Message()
    decoded.decode(raw)
    assert decoded.credit_charge == 2
    assert decoded.credit_re == 7


def test_disconnect_in_replay_ignores_late_duplicate_response():
    capture = {
        'initial_credits': 1,
        'events': [
            {'time': 1.0, 'type': 'read', 'mid': 1, 'length': 65536},
            {'time': 2.0, 'type': 'disconnect'},
            {'time': 3.0, 'type': 'response', 'mid': 1, 'credits': 1},
        ],
    }
    result = sc.replay_capture(capture)
    assert result['requests'][0]['state'] == sc.CreditRequest.FAILED
    assert result['available'] == 0
    assert result['in_flight'] == 0
    assert result['timeline'][-1]['accepted'] is False
