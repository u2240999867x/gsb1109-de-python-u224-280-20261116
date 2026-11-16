
import struct

from smb.base import SMB
from smb.smb2_constants import (
    SMB2_COM_CREATE,
    SMB2_COM_OPLOCK_BREAK,
    SMB2_FLAGS_SERVER_TO_REDIR,
    SMB2_LEASE_READ_CACHING,
    SMB2_LEASE_WRITE_CACHING,
    SMB2_NOTIFY_BREAK_LEASE_FLAG_ACK_REQUIRED,
    SMB2_OPLOCK_LEVEL_BATCH,
    SMB2_OPLOCK_LEVEL_NONE,
)
from smb.smb2_structs import (
    SMB2Message,
    SMB2LeaseBreakAcknowledgment,
    SMB2LeaseBreakNotification,
    SMB2LeaseBreakResponse,
    SMB2OplockBreakNotification,
    _decodeCreateContexts,
    buildLeaseRequestCreateContext,
)


LEASE_KEY = bytes(range(16))
LEASE_STATE = SMB2_LEASE_READ_CACHING | SMB2_LEASE_WRITE_CACHING


def build_message(command, data, mid = 0xFFFFFFFFFFFFFFFF, flags = SMB2_FLAGS_SERVER_TO_REDIR):
    header = struct.pack('<4sHHIHHI', b'\xFESMB', 64, 0, 0, command, 0, flags)
    header += struct.pack('<IQIIQ16s', 0, mid, 0, 0, 1, b'\0' * 16)
    return header + data


def build_lease_break_notification(flags = SMB2_NOTIFY_BREAK_LEASE_FLAG_ACK_REQUIRED,
                                   lease_key = LEASE_KEY,
                                   current_state = LEASE_STATE,
                                   new_state = SMB2_LEASE_READ_CACHING):
    data = struct.pack('<HH16sIIIII', 44, flags, lease_key, current_state, new_state, 0, 0, 0) + b'\0' * 4
    return build_message(SMB2_COM_OPLOCK_BREAK, data)


def decode(raw):
    message = SMB2Message()
    message.decode(raw)
    return message


def make_smb():
    smb = SMB('user', 'pass', 'MYPC', 'SERVER')
    smb._setupSMB2Methods()
    smb.is_using_smb2 = True
    smb.session_id = 1
    return smb


def test_lease_break_notification_decoding():
    message = decode(build_lease_break_notification())
    payload = message.payload
    assert isinstance(payload, SMB2LeaseBreakNotification)
    assert payload.flags == SMB2_NOTIFY_BREAK_LEASE_FLAG_ACK_REQUIRED
    assert payload.lease_key == LEASE_KEY
    assert payload.current_lease_state == LEASE_STATE
    assert payload.new_lease_state == SMB2_LEASE_READ_CACHING
    assert payload.isAckRequired


def test_lease_break_notification_without_ack_requirement():
    message = decode(build_lease_break_notification(flags = 0))
    assert isinstance(message.payload, SMB2LeaseBreakNotification)
    assert not message.payload.isAckRequired


def test_lease_break_acknowledgment_encoding():
    message = SMB2Message(SMB2LeaseBreakAcknowledgment(LEASE_KEY, SMB2_LEASE_READ_CACHING))
    raw = message.encode()
    assert len(raw) == 64 + 36
    assert message.command == SMB2_COM_OPLOCK_BREAK

    struct_size, flags, lease_key, lease_state, lease_duration = struct.unpack('<HH16sIQ', raw[64:64+32])
    assert struct_size == 36
    assert flags == 0
    assert lease_key == LEASE_KEY
    assert lease_state == SMB2_LEASE_READ_CACHING
    assert lease_duration == 0
    assert raw[64+32:] == b'\0' * 4


def test_lease_break_response_decoding():
    data = struct.pack('<HH16sIQ', 36, 0, LEASE_KEY, SMB2_LEASE_READ_CACHING, 0) + b'\0' * 4
    message = decode(build_message(SMB2_COM_OPLOCK_BREAK, data, mid = 7))
    payload = message.payload
    assert isinstance(payload, SMB2LeaseBreakResponse)
    assert payload.lease_key == LEASE_KEY
    assert payload.lease_state == SMB2_LEASE_READ_CACHING


def test_oplock_break_notification_decoding():
    fid = b'\xAB' * 16
    data = struct.pack('<HBBI16s', 24, SMB2_OPLOCK_LEVEL_BATCH, 0, 0, fid)
    message = decode(build_message(SMB2_COM_OPLOCK_BREAK, data))
    payload = message.payload
    assert isinstance(payload, SMB2OplockBreakNotification)
    assert payload.oplock == SMB2_OPLOCK_LEVEL_BATCH
    assert payload.fid == fid


def test_lease_request_create_context_roundtrip():
    ctx = buildLeaseRequestCreateContext(LEASE_KEY, LEASE_STATE)
    contexts = _decodeCreateContexts(ctx)
    assert len(contexts) == 1
    name, data = contexts[0]
    assert name == b'RqLs'
    lease_key, lease_state, lease_flags, lease_duration = struct.unpack('<16sIIQ', data)
    assert lease_key == LEASE_KEY
    assert lease_state == LEASE_STATE


def test_create_response_with_lease_context_decoding():
    ctx = buildLeaseRequestCreateContext(LEASE_KEY, LEASE_STATE)
    fid = b'\x01' * 16
    create_struct = struct.pack('<HBBIQQQQQQII16sII',
                                89, 0xFF, 0, 1,
                                0, 0, 0, 0,  # timestamps
                                0, 100, 0, 0, fid,
                                64 + 88, len(ctx))
    message = decode(build_message(SMB2_COM_CREATE, create_struct + ctx, mid = 9))
    payload = message.payload
    assert payload.lease_key == LEASE_KEY
    assert payload.lease_state == LEASE_STATE
    assert payload.fid == fid


def test_break_notification_revokes_cached_observations():
    smb = make_smb()
    smb.lease_cache.lease_acquired(LEASE_KEY, LEASE_STATE)
    smb.lease_cache.record_attributes(LEASE_KEY, '/dir/file.txt', { 'size': 10 })
    smb.lease_cache.record_data(LEASE_KEY, '/dir/file.txt', 0, b'0123')

    message = decode(build_lease_break_notification(flags = 0))
    assert smb._updateState(message)

    # Old observations are archived and no longer served as current
    assert smb.lease_cache.lookup_attributes(LEASE_KEY, '/dir/file.txt') is None
    assert smb.lease_cache.lookup_data(LEASE_KEY, '/dir/file.txt', 0, 4) is None
    assert len(smb.lease_cache.archived_entries(LEASE_KEY)) == 2


def test_break_notification_with_ack_required_sends_acknowledgment():
    smb = make_smb()
    sent = [ ]
    smb.sendNMBMessage = sent.append

    message = decode(build_lease_break_notification())
    smb._updateState(message)

    assert len(sent) == 1
    ack = decode(sent[0])
    assert ack.command == SMB2_COM_OPLOCK_BREAK
    assert isinstance(ack.payload, SMB2LeaseBreakResponse)  # same wire layout as the acknowledgment
    assert ack.payload.lease_key == LEASE_KEY
    assert ack.payload.lease_state == SMB2_LEASE_READ_CACHING


def test_break_notification_without_ack_requirement_sends_nothing():
    smb = make_smb()
    sent = [ ]
    smb.sendNMBMessage = sent.append

    message = decode(build_lease_break_notification(flags = 0))
    smb._updateState(message)

    assert sent == [ ]


def test_create_response_registers_lease_with_cache():
    smb = make_smb()
    ctx = buildLeaseRequestCreateContext(LEASE_KEY, LEASE_STATE)
    create_struct = struct.pack('<HBBIQQQQQQII16sII',
                                89, 0xFF, 0, 1,
                                0, 0, 0, 0,
                                0, 100, 0, 0, b'\x01' * 16,
                                64 + 88, len(ctx))
    message = decode(build_message(SMB2_COM_CREATE, create_struct + ctx, mid = 9))
    smb._updateState(message)

    assert smb.lease_cache.is_caching(LEASE_KEY)

    # Re-acquiring the lease must not re-validate older observations
    smb.lease_cache.record_attributes(LEASE_KEY, '/f', { 'size': 1 })
    smb.lease_cache.lease_revoked(LEASE_KEY)
    smb._updateState(message)
    assert smb.lease_cache.lookup_attributes(LEASE_KEY, '/f') is None
