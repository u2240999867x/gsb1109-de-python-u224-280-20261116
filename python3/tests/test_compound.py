import base64
import json
import struct

import pytest

from smb.compound import (
    FILE_ID_PLACEHOLDER,
    SMB2CompoundRequest,
    SMB2CompoundResponse,
    analyze_operations,
    create_compound_file_operations,
    decode_compound_messages,
    export_compound_operations,
    is_placeholder,
    parse_compound_report,
)
from smb.smb2_constants import (
    SMB2_COM_CLOSE,
    SMB2_COM_CREATE,
    SMB2_COM_QUERY_INFO,
    SMB2_COM_READ,
)
from smb.smb2_structs import SMB2Message


FID = bytes.fromhex('00112233445566778899aabbccddeeff')


def _header(command, mid, status = 0, next_command_offset = 0, tid = 1):
    return struct.pack('<4sHHIHHI', b'\xfeSMB', 64, 0, status, command, 0, 1) + \
           struct.pack('<IQIIQ16s', next_command_offset, mid, 1, tid, 1, b'\0' * 16)


def _create_response(mid, status = 0, file_id = FID):
    body = struct.pack('<HBBIQQQQQQII16sII', 89, 1, 0, 1,
                       0, 0, 0, 0, 5, 5, 0x20, 0, file_id, 0, 0)
    return _header(SMB2_COM_CREATE, mid, status) + body


def _query_response(mid, status = 0):
    data = struct.pack('<QQ', 5, 5)
    return _header(SMB2_COM_QUERY_INFO, mid, status) + \
           struct.pack('<HHI', 9, 80, len(data)) + data


def _read_response(mid, data = b'', status = 0, offset = 0):
    body_offset = 80
    return _header(SMB2_COM_READ, mid, status) + \
           struct.pack('<HBBIII', 17, body_offset, 0, len(data), 0, 0) + data


def _close_response(mid, status = 0):
    return _header(SMB2_COM_CLOSE, mid, status) + struct.pack('<HHI', 60, 0, 0)


def _chain(parts):
    parts = list(parts)
    for index in range(len(parts) - 1):
        next_offset = (len(parts[index]) + 7) & ~7
        parts[index] = parts[index][:20] + struct.pack('<I', next_offset) + parts[index][24:]
        if len(parts[index]) % 8:
            parts[index] += b'\0' * ( 8 - len(parts[index]) % 8 )
    return b''.join(parts)


def test_compound_request_links_offsets_and_uses_placeholder():
    requests = create_compound_file_operations('folder/file.txt', read_length = 5)
    raw = SMB2CompoundRequest(requests).encode()

    assert len(raw) % 8 == 0
    assert requests[0].next_command_offset > 0
    assert requests[1].next_command_offset > 0
    assert requests[2].next_command_offset > 0
    assert requests[3].next_command_offset == 0

    assert requests[0].payload.filename == 'folder/file.txt'
    for request in requests[1:]:
        assert request.payload.fid == FILE_ID_PLACEHOLDER
        assert is_placeholder(request.payload.fid)


def test_parse_successful_chain_exports_context_and_archives_reads():
    requests = create_compound_file_operations('folder/file.txt', read_length = 5)
    raw = _chain([
        _create_response(1),
        _query_response(2),
        _read_response(3, b'hello', offset = 7),
        _close_response(4),
    ])

    report = parse_compound_report(requests, raw, service_name = 'share', path = 'folder/file.txt')
    result = report.to_dict()

    assert [ op['operation'] for op in result['sub_operations'] ] == [ 'open', 'query', 'read', 'close' ]
    assert [ op['status_name'] for op in result['sub_operations'] ] == [ 'STATUS_SUCCESS' ] * 4
    assert result['sub_operations'][0]['file_id'] == FID.hex()
    assert result['sub_operations'][1]['inherited'] is True
    assert result['sub_operations'][1]['file_id'] == FID.hex()
    assert result['sub_operations'][1]['inheritance_source'] == 'create[0]'
    assert result['sub_operations'][2]['bytes_read'] == 5
    assert result['sub_operations'][2]['archived'] is True
    assert result['read_archive']['total_bytes'] == 5
    assert base64.b64decode(result['read_archive']['data_base64']) == b'hello'
    assert result['resource_status']['state'] == 'closed'
    assert result['resource_status']['close_ok'] is True
    assert result['success'] is True


def test_open_failure_never_promotes_placeholder_to_real_file_id():
    requests = create_compound_file_operations('missing.txt')
    raw = _chain([
        _create_response(1, status = 0xC0000034),
        _query_response(2, status = 0xC0000001),
        _read_response(3, b'unread', status = 0xC0000001),
        _close_response(4, status = 0xC0000001),
    ])

    report = parse_compound_report(requests, raw)
    result = report.to_dict()

    assert result['resource_status']['state'] == 'not_opened'
    assert result['resource_status']['file_id'] is None
    assert result['read_archive']['total_bytes'] == 0
    for operation in result['sub_operations'][1:]:
        assert operation['uses_placeholder'] is True
        assert operation['file_id'] is None
        assert operation['inherited'] is False
        assert operation['file_context']['unresolved'] is True
        assert operation['after_failure'] is True


def test_failed_middle_step_stops_later_placeholder_inheritance():
    requests = create_compound_file_operations('file.txt')
    raw = _chain([
        _create_response(1),
        _query_response(2, status = 0xC0000022),
        _read_response(3, b'late-data', status = 0xC0000001),
        _close_response(4, status = 0xC0000001),
    ])

    report = parse_compound_report(requests, raw)
    result = report.to_dict()

    assert result['sub_operations'][1]['ok'] is False
    assert result['sub_operations'][1]['file_id'] == FID.hex()
    for operation in result['sub_operations'][2:]:
        assert operation['file_id'] is None
        assert operation['inherited'] is False
        assert operation['after_failure'] is True
    assert result['read_archive']['total_bytes'] == 0


def test_close_failure_keeps_read_archive_and_records_resource_state():
    requests = create_compound_file_operations('file.txt')
    raw = _chain([
        _create_response(1),
        _query_response(2),
        _read_response(3, b'abc'),
        _close_response(4, status = 0xC0000008),
    ])

    report = parse_compound_report(requests, raw)
    result = report.to_dict()

    assert result['sub_operations'][2]['archived'] is True
    assert result['read_archive']['total_bytes'] == 3
    assert result['sub_operations'][3]['ok'] is False
    assert result['sub_operations'][3]['file_id'] == FID.hex()
    assert result['resource_status']['state'] == 'close_failed'
    assert result['resource_status']['close_status'] == 0xC0000008
    assert result['resource_status']['leaked'] is True
    assert result['success'] is False


def test_placeholder_after_failed_open_stays_unresolved_even_if_close_succeeds():
    operations = [
        { 'operation': 'open', 'status': 'STATUS_OBJECT_NAME_NOT_FOUND' },
        { 'operation': 'query' },
        { 'operation': 'read', 'status': 'STATUS_UNSUCCESSFUL' },
        { 'operation': 'close', 'status': 'STATUS_INVALID_HANDLE' },
    ]

    report = analyze_operations(operations)
    result = report.to_dict()

    assert result['resource_status']['state'] == 'not_opened'
    for operation in result['sub_operations'][1:]:
        assert operation['file_id'] is None
        assert operation['inherited'] is False
        assert operation['uses_placeholder'] is True
    assert result['read_archive']['total_bytes'] == 0


def test_explicit_real_handle_survives_a_prior_failure():
    operations = [
        { 'operation': 'open', 'status': 'STATUS_ACCESS_DENIED' },
        { 'operation': 'close', 'request_file_id': FID.hex(), 'status': 0 },
    ]

    report = analyze_operations(operations)
    result = report.to_dict()

    close = result['sub_operations'][1]
    assert close['uses_placeholder'] is False
    assert close['file_id'] == FID.hex()
    assert close['inherited'] is False
    assert close['ok'] is True


def test_export_writes_json_and_read_bytes():
    operations = [
        { 'operation': 'open', 'file_id': FID.hex() },
        { 'operation': 'query' },
        { 'operation': 'read', 'data': base64.b64encode(b'payload').decode('ascii') },
        { 'operation': 'close', 'status': 'STATUS_INVALID_HANDLE' },
    ]

    report = analyze_operations(operations, service_name = 'share', path = 'x.bin')
    text = json.dumps(report.to_dict())
    parsed = json.loads(text)
    assert parsed['resource_status']['state'] == 'close_failed'
    assert parsed['read_archive']['total_bytes'] == 7

    import io
    buf = io.BytesIO()
    report.read_archive.save(buf)
    assert buf.getvalue() == b'payload'


def test_response_wrapper_decodes_and_exports():
    requests = create_compound_file_operations('file.txt')
    raw = _chain([
        _create_response(1),
        _query_response(2),
        _read_response(3, b'x'),
        _close_response(4),
    ])

    response = SMB2CompoundResponse.decode(raw)
    assert len(response) == 4
    report = response.report(requests)
    assert report.resource_status['state'] == 'closed'
    assert report.read_archive.total_bytes == 1

def test_successful_read_after_failure_archives_bytes_but_keeps_handle_unresolved():
    operations = [
        { 'operation': 'open', 'status': 'STATUS_OBJECT_NAME_NOT_FOUND' },
        { 'operation': 'read', 'status': 0,
          'data': base64.b64encode(b'bytes').decode('ascii'), 'offset': 0 },
        { 'operation': 'close', 'status': 0 },
    ]

    report = analyze_operations(operations)
    result = report.to_dict()

    read = result['sub_operations'][1]
    assert read['ok'] is True
    assert read['status_name'] == 'STATUS_SUCCESS'
    assert read['archived'] is True
    assert read['file_id'] is None
    assert read['inherited'] is False
    assert result['read_archive']['total_bytes'] == 5

def test_unresolved_close_records_status_without_inventing_a_file_id():
    operations = [
        { 'operation': 'open', 'status': 'STATUS_OBJECT_NAME_NOT_FOUND' },
        { 'operation': 'close', 'status': 'STATUS_INVALID_HANDLE' },
    ]

    result = analyze_operations(operations).to_dict()
    resource = result['resource_status']

    assert resource['state'] == 'not_opened'
    assert resource['file_id'] is None
    assert resource['close_status'] == 0xC0000008
    assert resource['close_state'] == 'unresolved'
    assert resource['close_resolved'] is False
