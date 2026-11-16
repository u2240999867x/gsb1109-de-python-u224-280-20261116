"""Helpers for SMB2 compound file operations.

An SMB2 compound request carries several commands in one message chain. A
typical file batch is CREATE (open), QUERY_INFO, READ and CLOSE. Commands
after CREATE refer to the newly opened file with the all-ones file id
placeholder and the SMB2_FLAGS_RELATED_OPERATIONS flag.

This module builds such a chain, decodes a response chain and exports a
JSON-serialisable report. The report keeps three things independent:

* the status of every sub-operation;
* the file context that each sub-operation actually inherited;
* the state of the opened resource (including a failed CLOSE).

A placeholder is never promoted to a real file id when the preceding CREATE
failed. Once a step fails, later placeholders no longer inherit the file
context; any bytes read before that failure stay archived, and a failed CLOSE
is recorded on the resource rather than hidden by later operation statuses.
"""

import base64
import binascii
import json
import struct
import sys

from .smb2_constants import (
    SMB2_COM_CLOSE,
    SMB2_COM_CREATE,
    SMB2_COM_QUERY_INFO,
    SMB2_COM_READ,
    SMB2_COMMAND_NAMES,
    SMB2_FLAGS_RELATED_OPERATIONS,
    SMB2_FLAGS_SERVER_TO_REDIR,
)
from .smb2_structs import SMB2Message


FILE_ID_PLACEHOLDER = b'\xff' * 16
FILE_ID_PLACEHOLDER_HEX = 'ff' * 16


STATUS_NAMES = {
    0x00000000: 'STATUS_SUCCESS',
    0x00000103: 'STATUS_PENDING',
    0x80000006: 'STATUS_NO_MORE_FILES',
    0xC0000001: 'STATUS_UNSUCCESSFUL',
    0xC0000002: 'STATUS_NOT_IMPLEMENTED',
    0xC0000004: 'STATUS_INFO_LENGTH_MISMATCH',
    0xC0000006: 'STATUS_IN_PAGE_ERROR',
    0xC0000008: 'STATUS_INVALID_HANDLE',
    0xC000000D: 'STATUS_INVALID_PARAMETER',
    0xC000000F: 'STATUS_NO_SUCH_FILE',
    0xC0000011: 'STATUS_END_OF_FILE',
    0xC0000022: 'STATUS_ACCESS_DENIED',
    0xC0000023: 'STATUS_BUFFER_TOO_SMALL',
    0xC0000034: 'STATUS_OBJECT_NAME_NOT_FOUND',
    0xC0000035: 'STATUS_OBJECT_NAME_COLLISION',
    0xC0000043: 'STATUS_SHARING_VIOLATION',
    0xC0000054: 'STATUS_FILE_LOCK_CONFLICT',
    0xC00000BA: 'STATUS_FILE_IS_A_DIRECTORY',
    0xC00000C9: 'STATUS_NETWORK_NAME_DELETED',
    0xC00000CC: 'STATUS_BAD_NETWORK_NAME',
    0xC0000120: 'STATUS_CANCELLED',
}

STATUS_NAMES_BY_NAME = dict((name, code) for code, name in STATUS_NAMES.items())

OPERATION_NAMES = {
    SMB2_COM_CREATE: 'open',
    SMB2_COM_QUERY_INFO: 'query',
    SMB2_COM_READ: 'read',
    SMB2_COM_CLOSE: 'close',
}

OPERATION_CODES = {
    'open': SMB2_COM_CREATE,
    'create': SMB2_COM_CREATE,
    'open_file': SMB2_COM_CREATE,
    'create_file': SMB2_COM_CREATE,
    'query': SMB2_COM_QUERY_INFO,
    'query_file': SMB2_COM_QUERY_INFO,
    'query_info': SMB2_COM_QUERY_INFO,
    'query_file_info': SMB2_COM_QUERY_INFO,
    'queryinfo': SMB2_COM_QUERY_INFO,
    'read': SMB2_COM_READ,
    'read_file': SMB2_COM_READ,
    'close': SMB2_COM_CLOSE,
    'close_file': SMB2_COM_CLOSE,
    'release': SMB2_COM_CLOSE,
}

PAYLOAD_OPERATIONS = {
    'SMB2CreateRequest': ( 'open', SMB2_COM_CREATE ),
    'SMB2QueryInfoRequest': ( 'query', SMB2_COM_QUERY_INFO ),
    'SMB2ReadRequest': ( 'read', SMB2_COM_READ ),
    'SMB2CloseRequest': ( 'close', SMB2_COM_CLOSE ),
}

PAYLOAD_RESPONSE_OPERATIONS = {
    'SMB2CreateResponse': ( 'open', SMB2_COM_CREATE ),
    'SMB2QueryInfoResponse': ( 'query', SMB2_COM_QUERY_INFO ),
    'SMB2ReadResponse': ( 'read', SMB2_COM_READ ),
    'SMB2CloseResponse': ( 'close', SMB2_COM_CLOSE ),
}


class CompoundOperationError(Exception):
    """Raised when a compound request cannot be built or parsed."""
    pass


def status_name(status):
    """Return a readable NTSTATUS name for *status*."""
    if status is None:
        return None
    return STATUS_NAMES.get(status, '0x%08X' % status)


def status_value(value):
    """Convert an integer, ``0x...`` string or NTSTATUS name to an integer."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 0 if value else 0xC0000001
    if isinstance(value, int):
        return value
    if isinstance(value, bytes):
        value = value.decode('ascii')
    text = str(value).strip()
    if text in STATUS_NAMES_BY_NAME:
        return STATUS_NAMES_BY_NAME[text]
    if text.lower().startswith('0x'):
        return int(text, 16)
    try:
        return int(text, 10)
    except ValueError:
        raise ValueError('Unknown SMB status: %r' % ( value, ))


def file_id_bytes(value):
    """Normalise a file id to its 16-byte representation."""
    if value is None:
        return None
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytearray):
        value = bytes(value)
    if isinstance(value, bytes):
        if len(value) == 16:
            return value
        if len(value) == 32:
            return binascii.unhexlify(value)
    if isinstance(value, str):
        text = value.strip()
        lowered = text.lower()
        if lowered in ( 'none', 'null', '' ):
            return None
        if lowered in ( 'placeholder', 'fileid_placeholder', 'file_id_placeholder',
                        'smb2_fileid_placeholder', '<placeholder>',
                        '<fileid_placeholder>', 'fileidplaceholder' ):
            return FILE_ID_PLACEHOLDER
        if lowered.startswith('0x'):
            text = text[2:]
        text = text.replace('-', '').replace(' ', '')
        if len(text) == 32:
            return binascii.unhexlify(text)
    raise ValueError('Invalid SMB file id: %r' % ( value, ))


def file_id_hex(value):
    """Return a 32-character lower-case hex string for *value*."""
    raw = file_id_bytes(value)
    if raw is None:
        return None
    return binascii.hexlify(raw).decode('ascii')


def is_placeholder(value):
    """Return True if *value* is the SMB2 all-ones file id placeholder."""
    try:
        return file_id_bytes(value) == FILE_ID_PLACEHOLDER
    except ValueError:
        return False


def command_name(command):
    if command is None:
        return None
    return SMB2_COMMAND_NAMES.get(command, '0x%04X' % command)


def is_related_operation(message):
    return bool(getattr(message, 'flags', 0) & SMB2_FLAGS_RELATED_OPERATIONS)


def describe_operation(message):
    operation, command = _request_operation(message)
    return {
        'operation': operation,
        'command': command_name(command),
        'command_code': command,
        'related': is_related_operation(message),
        'file_id': file_id_hex(_request_file_id(message)),
        'uses_placeholder': is_placeholder(_request_file_id(message)),
    }


def operation_for_command(command):
    return OPERATION_NAMES.get(command, 'command_0x%04X' % command)


def operation_code(operation):
    if isinstance(operation, int):
        return operation
    key = str(operation).strip().lower().replace('-', '_').replace(' ', '_')
    if key.startswith('smb2_com_'):
        key = key[len('smb2_com_'):]
    if key in OPERATION_CODES:
        return OPERATION_CODES[key]
    raise ValueError('Unknown compound operation: %r' % ( operation, ))


def _payload_operation(payload, response = False):
    if payload is None:
        return None, None
    table = PAYLOAD_RESPONSE_OPERATIONS if response else PAYLOAD_OPERATIONS
    return table.get(type(payload).__name__, ( None, None ))


def _request_file_id(message):
    payload = getattr(message, 'payload', None)
    if payload is not None:
        value = getattr(payload, 'fid', None)
        if value is not None:
            return file_id_bytes(value)
    value = getattr(message, 'fid', None)
    if value is not None:
        return file_id_bytes(value)
    return None


def _result_file_id(message):
    payload = getattr(message, 'payload', None)
    if payload is not None:
        value = getattr(payload, 'fid', None)
        if value is not None:
            return file_id_bytes(value)
    return None


def _message_status(message):
    value = getattr(message, 'status', 0)
    if hasattr(value, 'internal_value'):
        return value.internal_value
    return int(value)


def _read_response_data(message):
    payload = getattr(message, 'payload', None)
    if payload is None:
        return b''
    return getattr(payload, 'data', b'') or b''


def _read_response_offset(message):
    payload = getattr(message, 'payload', None)
    for name in ( 'offset', 'read_offset' ):
        value = getattr(payload, name, None)
        if value is not None:
            return value
    return None


def _request_read_offset(message):
    payload = getattr(message, 'payload', None)
    for name in ( 'read_offset', 'offset' ):
        value = getattr(payload, name, None)
        if value is not None:
            return value
    return None


def _decode_data(value):
    if value is None:
        return b''
    if isinstance(value, ( bytes, bytearray )):
        return bytes(value)
    text = str(value).strip()
    if text.startswith('0x'):
        return binascii.unhexlify(text[2:])
    try:
        return base64.b64decode(text, validate = True)
    except ( binascii.Error, ValueError ):
        return binascii.unhexlify(text)


class FileContext(object):
    """The file context inherited by a compound sub-operation."""

    def __init__(self, file_id = None, source = None, source_index = None,
                 inherited = False, placeholder = False, valid = False,
                 unresolved = False, after_failure = False,
                 session_id = None, tid = None, service_name = None, path = None):
        self.file_id = file_id_bytes(file_id)
        self.session_id = session_id
        self.tid = tid
        self.service_name = service_name
        self.path = path
        self.source = source
        self.source_index = source_index
        self.inherited = inherited
        self.placeholder = placeholder
        self.valid = valid
        self.unresolved = unresolved
        self.after_failure = after_failure

    @property
    def hex(self):
        return file_id_hex(self.file_id)

    def to_dict(self):
        return {
            'file_id': self.hex,
            'file_id_hex': self.hex,
            'session_id': self.session_id,
            'tree_id': self.tid,
            'tid': self.tid,
            'service_name': self.service_name,
            'path': self.path,
            'source': self.source,
            'source_operation_index': self.source_index,
            'inherited': self.inherited,
            'placeholder': self.placeholder,
            'valid': self.valid,
            'unresolved': self.unresolved,
            'after_failure': self.after_failure,
        }

    def copy(self):
        return FileContext(self.file_id, self.source, self.source_index,
                           self.inherited, self.placeholder, self.valid,
                           self.unresolved, self.after_failure,
                           self.session_id, self.tid, self.service_name, self.path)


class ReadArchive(object):
    """Bytes successfully read by READ sub-operations."""

    def __init__(self, keep_data = True):
        self.keep_data = keep_data
        self.chunks = [ ]

    def append(self, operation_index, offset, data):
        data = bytes(data or b'')
        chunk = {
            'operation_index': operation_index,
            'offset': offset,
            'length': len(data),
            'data': data if self.keep_data else None,
        }
        self.chunks.append(chunk)
        return chunk

    @property
    def total_bytes(self):
        return sum(chunk['length'] for chunk in self.chunks)

    def bytes(self):
        return b''.join(chunk['data'] or b'' for chunk in self.chunks)

    def save(self, file_obj_or_path):
        data = self.bytes()
        if hasattr(file_obj_or_path, 'write'):
            file_obj_or_path.write(data)
            return len(data)
        with open(file_obj_or_path, 'wb') as f:
            f.write(data)
        return len(data)

    def to_dict(self, include_data = True):
        chunks = [ ]
        for chunk in self.chunks:
            item = {
                'operation_index': chunk['operation_index'],
                'offset': chunk['offset'],
                'length': chunk['length'],
                'archived': True,
            }
            if include_data and self.keep_data and chunk['data'] is not None:
                item['data_base64'] = base64.b64encode(chunk['data']).decode('ascii')
                item['data_hex'] = binascii.hexlify(chunk['data']).decode('ascii')
            chunks.append(item)
        result = {
            'chunks': chunks,
            'total_bytes': self.total_bytes,
            'archived': self.total_bytes > 0,
        }
        if include_data and self.keep_data:
            data = self.bytes()
            result['data'] = base64.b64encode(data).decode('ascii')
            result['data_base64'] = result['data']
            result['data_hex'] = binascii.hexlify(data).decode('ascii')
        return result


class SubOperationResult(object):
    """One sub-operation in a compound batch."""

    def __init__(self, index, operation, command = None, mid = 0,
                 request_file_id = None, uses_placeholder = False):
        self.index = index
        self.operation = operation
        self.command = command
        self.mid = mid
        self.request_file_id = file_id_bytes(request_file_id)
        self.uses_placeholder = uses_placeholder
        self.status = None
        self.executed = False
        self.ok = False
        self.skip_reason = None
        self.context = FileContext()
        self.bytes_read = 0
        self.archived = False
        self.data = None
        self.error = None
        self.after_failure = False

    @property
    def file_id(self):
        return self.context.file_id

    @property
    def inherited_file_id(self):
        return self.context.file_id if self.context.inherited else None

    @property
    def resolved_file_id(self):
        return self.context.file_id

    @property
    def status_name(self):
        return status_name(self.status)

    def skip(self, reason):
        self.executed = False
        self.ok = False
        self.skip_reason = reason

    def to_dict(self, include_data = False):
        state = 'ok' if self.ok else ( 'skipped' if not self.executed else 'failed' )
        result = {
            'index': self.index,
            'operation': self.operation,
            'command': command_name(self.command) if self.command is not None else None,
            'command_code': self.command,
            'mid': self.mid,
            'state': state,
            'status': self.status,
            'status_hex': None if self.status is None else '0x%08X' % self.status,
            'status_name': status_name(self.status),
            'ok': bool(self.ok),
            'executed': bool(self.executed),
            'skipped': not self.executed,
            'skip_reason': self.skip_reason,
            'request_file_id': file_id_hex(self.request_file_id),
            'uses_placeholder': bool(self.uses_placeholder),
            'file_id': self.context.hex,
            'file_id_hex': self.context.hex,
            'inherited_file_id': self.context.hex if self.context.inherited else None,
            'resolved_file_id': self.context.hex,
            'handle': self.context.hex,
            'valid_file_id': bool(self.context.valid),
            'placeholder': bool(self.uses_placeholder),
            'inherited': bool(self.context.inherited),
            'inheritance_source': self.context.source,
            'after_failure': bool(self.after_failure),
            'file_context': self.context.to_dict(),
            'bytes_read': self.bytes_read,
            'archived': bool(self.archived),
            'error': self.error,
        }
        if include_data and self.data is not None:
            result['data_base64'] = base64.b64encode(self.data).decode('ascii')
        return result


class CompoundOperationReport(object):
    """The exported state of a compound file-operation batch."""

    def __init__(self, service_name = None, path = None, keep_data = True):
        self.protocol = 'SMB2'
        self.service_name = service_name
        self.path = path
        self.sub_operations = [ ]
        self.read_archive = ReadArchive(keep_data = keep_data)
        self.resource_status = {
            'state': 'unknown',
            'file_id': None,
            'file_id_hex': None,
            'close_ok': None,
            'close_status': None,
            'close_status_name': None,
            'close_operation_index': None,
            'close_state': 'unknown',
            'leaked': False,
            'opened': False,
            'closed': False,
            'failed': False,
        }
        self.file_context = FileContext()

    @property
    def operations(self):
        return self.sub_operations

    @property
    def resource(self):
        return self.resource_status

    @property
    def status(self):
        return self.resource_status.get('state')

    @property
    def archive(self):
        return self.read_archive

    @property
    def inherited_contexts(self):
        return [ op.context for op in self.sub_operations if op.context.inherited ]

    @property
    def success(self):
        return all(op.ok for op in self.sub_operations) and self.resource_status['state'] == 'closed'

    @property
    def statuses(self):
        return [ {
            'index': op.index,
            'operation': op.operation,
            'status': op.status,
            'status_name': status_name(op.status),
            'state': 'ok' if op.ok else ( 'skipped' if not op.executed else 'failed' ),
        } for op in self.sub_operations ]

    @property
    def by_operation(self):
        result = { }
        for op in self.sub_operations:
            result.setdefault(op.operation, [ ]).append(op)
        return result

    @property
    def status_counts(self):
        counts = { }
        for op in self.sub_operations:
            counts.setdefault(op.operation, { })
            state = 'ok' if op.ok else ( 'skipped' if not op.executed else 'failed' )
            counts[op.operation][state] = counts[op.operation].get(state, 0) + 1
        return counts

    def set_resource(self, state, file_id = None, close_op = None, close_status = None,
                     close_resolved = True):
        close_ok = None
        if close_status is not None:
            close_ok = close_status == 0
        if state == 'closed':
            close_state = 'closed'
        elif state == 'close_failed':
            close_state = 'failed'
        elif state == 'not_opened':
            close_state = 'not_opened' if close_op is None else 'unresolved'
        elif state == 'open':
            close_state = 'unresolved' if ( close_op is not None and not close_op.context.valid ) else 'not_closed'
        else:
            close_state = state
        self.resource_status = {
            'state': state,
            'file_id': file_id_hex(file_id),
            'file_id_hex': file_id_hex(file_id),
            'close_ok': close_ok,
            'close_status': close_status,
            'close_status_name': status_name(close_status),
            'close_operation_index': None if close_op is None else close_op.index,
            'close_state': close_state,
            'close_resolved': bool(close_resolved),
            'leaked': state in ( 'open', 'close_failed' ),
            'opened': state != 'not_opened',
            'closed': state == 'closed',
            'failed': state == 'close_failed',
        }

    def to_dict(self, include_data = True):
        operations = [ op.to_dict(include_data = include_data) for op in self.sub_operations ]
        result = {
            'protocol': self.protocol,
            'service_name': self.service_name,
            'path': self.path,
            'success': self.success,
            'file_context': self.file_context.to_dict(),
            'sub_operations': operations,
            'operations': operations,
            'statuses': self.statuses,
            'status_counts': self.status_counts,
            'summary': {
                'total': len(operations),
                'ok': sum(1 for op in self.sub_operations if op.ok),
                'failed': sum(1 for op in self.sub_operations if op.executed and not op.ok),
                'skipped': sum(1 for op in self.sub_operations if not op.executed),
                'bytes_read': self.read_archive.total_bytes,
                'resource_state': self.resource_status['state'],
                'success': self.success,
            },
            'read_archive': self.read_archive.to_dict(include_data = include_data),
            'archive': self.read_archive.to_dict(include_data = include_data),
            'resource_status': dict(self.resource_status),
            'resource': dict(self.resource_status),
        }
        return result

    def export(self, file_obj = None, include_data = True, indent = 2):
        result = self.to_dict(include_data = include_data)
        if file_obj is None:
            return result
        text = json.dumps(result, indent = indent, sort_keys = True)
        if hasattr(file_obj, 'write'):
            file_obj.write(text)
            return result
        with open(file_obj, 'w') as f:
            f.write(text)
        return result

    def export_text(self):
        lines = [ ]
        for op in self.sub_operations:
            state = 'ok' if op.ok else ( 'skipped' if not op.executed else 'failed' )
            fid = op.context.hex or '-'
            source = op.context.source or '-'
            lines.append('%d %-5s %-7s status=%s file_id=%s inherited=%s source=%s' % (
                op.index, op.operation, state,
                status_name(op.status) or '-',
                fid, 'yes' if op.context.inherited else 'no', source))
        resource = self.resource_status
        lines.append('resource state=%s file_id=%s close_status=%s' % (
            resource['state'], resource['file_id'] or '-',
            resource['close_status_name'] or '-'))
        lines.append('read bytes archived=%d' % self.read_archive.total_bytes)
        return '\n'.join(lines)


def create_compound_file_operations(path, read_length = 0xFFFF, query_output_length = 4096,
                                    file_attributes = 0, access_mask = None, share_access = None,
                                    create_options = None, create_disp = None):
    """Build CREATE, QUERY_INFO, READ and CLOSE request messages.

    The returned messages are suitable for :class:`SMB2CompoundRequest`.
    QUERY_INFO, READ and CLOSE carry the all-ones file id placeholder.
    """
    from .smb_constants import (
        FILE_NON_DIRECTORY_FILE, FILE_OPEN, FILE_READ_ATTRIBUTES,
        FILE_READ_DATA, FILE_SHARE_READ, FILE_SHARE_WRITE,
        READ_CONTROL, SYNCHRONIZE,
    )
    from .smb2_constants import SMB2_INFO_FILE
    from .smb2_structs import (
        SMB2CloseRequest, SMB2CreateRequest, SMB2QueryInfoRequest,
        SMB2ReadRequest,
    )

    if access_mask is None:
        access_mask = FILE_READ_DATA | FILE_READ_ATTRIBUTES | READ_CONTROL | SYNCHRONIZE
    if share_access is None:
        share_access = FILE_SHARE_READ | FILE_SHARE_WRITE
    if create_options is None:
        create_options = FILE_NON_DIRECTORY_FILE
    if create_disp is None:
        create_disp = FILE_OPEN

    create = SMB2Message(SMB2CreateRequest(path,
                                           file_attributes = file_attributes,
                                           access_mask = access_mask,
                                           share_access = share_access,
                                           create_disp = create_disp,
                                           create_options = create_options))
    query = SMB2Message(SMB2QueryInfoRequest(FILE_ID_PLACEHOLDER,
                                             flags = 0,
                                             additional_info = 0,
                                             info_type = SMB2_INFO_FILE,
                                             file_info_class = 0x05,
                                             input_buf = b'',
                                             output_buf_len = query_output_length))
    read = SMB2Message(SMB2ReadRequest(FILE_ID_PLACEHOLDER, 0, read_length))
    close = SMB2Message(SMB2CloseRequest(FILE_ID_PLACEHOLDER))

    for message in ( query, read, close ):
        message.flags |= SMB2_FLAGS_RELATED_OPERATIONS
    return [ create, query, read, close ]


def _encode_message(message):
    data = message.encode()
    if len(data) % 8:
        data += b'\0' * ( 8 - len(data) % 8 )
    return data


build_compound_file_operations = create_compound_file_operations


class SMB2CompoundRequest(object):
    """An SMB2 message chain with linked NextCommand offsets."""

    def __init__(self, messages = None):
        self.messages = list(messages or [ ])

    def add(self, message):
        self.messages.append(message)
        return message

    def encode(self):
        if not self.messages:
            return b''

        encoded = [ ]
        for index, message in enumerate(self.messages):
            message.next_command_offset = 0
            length = len(message.encode())
            if index < len(self.messages) - 1:
                message.next_command_offset = ( length + 7 ) & ~7
            encoded.append(_encode_message(message))
        return b''.join(encoded)

    def __iter__(self):
        return iter(self.messages)

    def __len__(self):
        return len(self.messages)


class SMB2CompoundResponse(object):
    """A decoded SMB2 compound response chain."""

    def __init__(self, messages = None):
        self.messages = list(messages or [ ])

    @classmethod
    def decode(cls, data):
        return cls(decode_compound_messages(data))

    def report(self, requests = None, service_name = None, path = None, keep_data = True):
        return parse_compound_report(requests, self.messages, service_name = service_name,
                                     path = path, keep_data = keep_data)

    def export(self, requests = None, service_name = None, path = None, keep_data = True,
               file_obj = None):
        return self.report(requests, service_name, path, keep_data).export(file_obj,
                                                                           include_data = keep_data)

    def __iter__(self):
        return iter(self.messages)

    def __len__(self):
        return len(self.messages)


class _RawRequestPayload(object):
    #: Fields decoded from a request message's fixed structures.

    def __init__(self, command, fid = None, filename = None, read_offset = None,
                 read_length = None):
        self.command = command
        self.fid = fid
        self.filename = filename
        self.read_offset = read_offset
        self.read_length = read_length


def _decode_single_message(data):
    """Decode one SMB2 message, including request messages.

    ``SMB2Message.decode`` expects response payloads, so request commands are
    parsed from their fixed structures with a lightweight payload.
    """
    if len(data) < SMB2Message.HEADER_SIZE:
        raise CompoundOperationError('Not enough data to decode an SMB2 message header')

    protocol, struct_size, credit_charge, status, command, credit_re, flags = \
        struct.unpack(SMB2Message.HEADER_STRUCT_FORMAT, data[:SMB2Message.HEADER_STRUCT_SIZE])
    if protocol != b'\xfeSMB' or struct_size != SMB2Message.HEADER_SIZE:
        raise CompoundOperationError('Invalid SMB2 message header')

    message = None
    if flags & SMB2_FLAGS_SERVER_TO_REDIR:
        try:
            candidate = SMB2Message()
            consumed = candidate.decode(data)
            return candidate, consumed
        except Exception:
            message = None

    if message is None:
        message = SMB2Message()

    if flags & 0x02:
        next_command_offset, mid, async_id, session_id, signature = \
            struct.unpack(SMB2Message.ASYNC_HEADER_STRUCT_FORMAT,
                          data[SMB2Message.HEADER_STRUCT_SIZE:SMB2Message.HEADER_SIZE])
        tid = 0
    else:
        next_command_offset, mid, pid, tid, session_id, signature = \
            struct.unpack(SMB2Message.SYNC_HEADER_STRUCT_FORMAT,
                          data[SMB2Message.HEADER_STRUCT_SIZE:SMB2Message.HEADER_SIZE])

    raw_length = next_command_offset if next_command_offset > 0 else len(data)
    body = data[SMB2Message.HEADER_SIZE:raw_length]

    fid = None
    filename = None
    read_offset = None
    read_length = None
    if command == SMB2_COM_CREATE and len(body) >= 48:
        name_offset, name_length = struct.unpack('<HH', body[44:48])
        if name_length:
            filename = body[name_offset - SMB2Message.HEADER_SIZE:
                            name_offset - SMB2Message.HEADER_SIZE + name_length].decode('UTF-16LE', 'replace')
    elif command == SMB2_COM_READ and len(body) >= 32:
        read_length = struct.unpack('<I', body[4:8])[0]
        read_offset = struct.unpack('<Q', body[8:16])[0]
        fid = body[16:32]
    elif command == SMB2_COM_CLOSE and len(body) >= 24:
        fid = body[8:24]
    elif command == SMB2_COM_QUERY_INFO and len(body) >= 40:
        fid = body[24:40]

    message.command = command
    message.status = status
    message.flags = flags
    message.next_command_offset = next_command_offset
    message.mid = mid
    message.tid = tid
    message.session_id = session_id
    message.raw_data = data[:raw_length]
    message.data = body
    message.payload = _RawRequestPayload(command, fid, filename, read_offset, read_length)
    return message, raw_length


def decode_compound_messages(data):
    """Decode every SMB2 message in a compound buffer."""
    messages = [ ]
    offset = 0
    total = len(data)
    while offset < total:
        message, consumed = _decode_single_message(data[offset:])
        if consumed <= 0:
            break
        messages.append(message)
        offset += consumed
    return messages


def decode_compound_requests(data):
    """Decode every SMB2 request message in a compound request buffer."""
    return decode_compound_messages(data)


def _as_message_list(messages):
    if messages is None:
        return [ ]
    if isinstance(messages, ( SMB2CompoundRequest, SMB2CompoundResponse )):
        return list(messages.messages)
    if hasattr(messages, 'messages'):
        return list(messages.messages)
    return list(messages)


def _request_operation(message):
    command = getattr(message, 'command', None)
    payload = getattr(message, 'payload', None)
    operation, payload_command = _payload_operation(payload)
    if operation is None:
        operation, payload_command = _payload_operation(message)
    if command is None:
        command = payload_command
    if operation is None:
        operation = operation_for_command(command)
    return operation, command


def _response_operation(message):
    command = getattr(message, 'command', None)
    payload = getattr(message, 'payload', None)
    operation, payload_command = _payload_operation(payload, response = True)
    if operation is None:
        operation, payload_command = _payload_operation(message, response = True)
    if command is None:
        command = payload_command
    if operation is None:
        operation = operation_for_command(command)
    return operation, command


def _match_responses(requests, responses):
    """Match responses to requests by MID or compound chain order.

    Related compound sub-requests may share the first request's MID, so a
    one-to-one MID map is only used when the MIDs are unique. Otherwise the
    chain order is authoritative, which is how SMB2 compound responses arrive.
    """
    if not responses:
        return [ None ] * len(requests)
    response_mids = [ getattr(item, 'mid', 0) for item in responses ]
    request_mids = [ getattr(item, 'mid', 0) for item in requests ]
    if ( all(mid for mid in response_mids) and all(mid for mid in request_mids)
         and len(set(request_mids)) == len(request_mids)
         and len(set(response_mids)) == len(response_mids) ):
        by_mid = dict(( getattr(item, 'mid', 0), item ) for item in responses)
        return [ by_mid.get(mid) for mid in request_mids ]
    return [ responses[index] if index < len(responses) else None
             for index in range(len(requests)) ]


def parse_compound_report(requests, responses, service_name = None, path = None,
                          keep_data = True):
    """Build a :class:`CompoundOperationReport` from request/response messages.

    *requests* may be messages, payloads, an :class:`SMB2CompoundRequest`, a
    raw SMB2 buffer, or ``None``. *responses* may be messages, an
    :class:`SMB2CompoundResponse` or a raw SMB2 buffer. When *requests* is
    ``None``, operations are inferred from the responses and any non-CREATE
    operation follows the current CREATE context.
    """
    if isinstance(responses, ( bytes, bytearray, memoryview )):
        responses = decode_compound_messages(bytes(responses))
    else:
        responses = _as_message_list(responses)

    if requests is None:
        requests = [ None ] * len(responses)
    elif isinstance(requests, ( bytes, bytearray, memoryview )):
        requests = decode_compound_messages(bytes(requests))
    else:
        requests = _as_message_list(requests)

    matched = _match_responses(requests, responses) if requests else [ ]

    report = CompoundOperationReport(service_name = service_name, path = path, keep_data = keep_data)

    opened_context = FileContext()
    inheritable_context = FileContext()
    failure_index = None
    close_seen = False
    close_resolved = False
    close_ok = None
    close_status = None
    close_operation = None

    for index, request in enumerate(requests):
        response = matched[index] if index < len(matched) else None

        related = False
        if request is not None:
            operation, command = _request_operation(request)
            mid = getattr(request, 'mid', 0)
            request_fid = _request_file_id(request)
            related = bool(getattr(request, 'flags', 0) & SMB2_FLAGS_RELATED_OPERATIONS)
        else:
            operation, command = _response_operation(response)
            mid = getattr(response, 'mid', 0) if response is not None else 0
            request_fid = None

        if response is not None:
            operation, command = _response_operation(response)
            mid = getattr(response, 'mid', mid)

        placeholder = request_fid is not None and is_placeholder(request_fid)
        if request_fid is None and related and inheritable_context.valid and operation != 'open':
            request_fid = FILE_ID_PLACEHOLDER
            placeholder = True
        after_failure = failure_index is not None

        if placeholder:
            if inheritable_context.valid:
                effective_fid = inheritable_context.file_id
                inherited = True
                source = inheritable_context.source
                source_index = inheritable_context.source_index
            else:
                effective_fid = None
                inherited = False
                source = None
                source_index = None
        elif request_fid is not None:
            effective_fid = request_fid
            inherited = False
            source = 'explicit'
            source_index = None
        elif inheritable_context.valid and operation != 'open':
            effective_fid = inheritable_context.file_id
            inherited = True
            source = inheritable_context.source
            source_index = inheritable_context.source_index
        else:
            effective_fid = None
            inherited = False
            source = None
            source_index = None

        sub = SubOperationResult(index, operation, command, mid,
                                 request_file_id = request_fid,
                                 uses_placeholder = placeholder)
        message_for_ids = response if response is not None else request
        session_id = getattr(message_for_ids, 'session_id', None)
        tid = getattr(message_for_ids, 'tid', None)
        sub.context = FileContext(effective_fid, source, source_index,
                                  inherited = inherited, placeholder = placeholder,
                                  valid = effective_fid is not None,
                                  unresolved = placeholder and effective_fid is None,
                                  after_failure = after_failure,
                                  session_id = session_id, tid = tid,
                                  service_name = service_name, path = path)
        sub.after_failure = after_failure

        if placeholder and not inheritable_context.valid:
            sub.status = _message_status(response) if response is not None else None
            sub.executed = response is not None
            sub.ok = sub.status == 0 if sub.executed else False
            sub.skip_reason = 'unresolved_placeholder'
            sub.error = None if sub.ok else ( status_name(sub.status) or 'unresolved_placeholder' )
        elif response is None:
            sub.skip('no_response' if failure_index is None else 'previous_failure')
        else:
            sub.executed = True
            sub.status = _message_status(response)
            sub.ok = sub.status == 0
            if not sub.ok:
                sub.error = status_name(sub.status)

        if operation == 'open':
            if sub.executed and sub.ok:
                result_fid = _result_file_id(response)
                if result_fid is None or is_placeholder(result_fid):
                    opened_context = FileContext()
                    inheritable_context = FileContext()
                else:
                    opened_context = FileContext(result_fid, 'create[%d]' % index, index,
                                                 inherited = False, placeholder = False,
                                                 valid = True, session_id = session_id,
                                                 tid = tid, service_name = service_name,
                                                 path = path)
                    inheritable_context = opened_context.copy()
                    sub.context = opened_context.copy()
                report.file_context = opened_context.copy()
            else:
                opened_context = FileContext()
                inheritable_context = FileContext()
                report.file_context = FileContext()
                if sub.executed and failure_index is None:
                    failure_index = index
        elif sub.executed and sub.ok and sub.context.valid:
            report.file_context = sub.context.copy()

        if sub.executed and not sub.ok:
            if failure_index is None:
                failure_index = index
            inheritable_context = FileContext()

        if operation == 'read' and sub.executed and sub.ok:
            data = _read_response_data(response)
            offset = _read_response_offset(response)
            if offset is None and request is not None:
                offset = _request_read_offset(request)
            if offset is None:
                offset = 0
            sub.data = data
            sub.bytes_read = len(data)
            if data:
                report.read_archive.append(index, offset, data)
                sub.archived = True

        if operation == 'close':
            close_seen = True
            close_operation = sub
            close_resolved = sub.executed and sub.context.valid
            if sub.executed:
                close_status = sub.status
                close_ok = sub.ok
            else:
                close_status = None
                close_ok = None

        report.sub_operations.append(sub)

    if opened_context.valid:
        report.file_context = opened_context.copy()

    if not opened_context.valid:
        state = 'not_opened'
        resource_fid = None
    elif close_seen and close_resolved:
        state = 'closed' if close_ok is True else 'close_failed'
        resource_fid = opened_context.file_id
    else:
        state = 'open'
        resource_fid = opened_context.file_id
    report.set_resource(state, resource_fid, close_operation, close_status,
                        close_resolved = close_resolved)

    return report


def export_compound_operations(requests, responses, service_name = None, path = None,
                               keep_data = True, file_obj = None, indent = 2):
    """Return or write a JSON-serialisable report for a message chain."""
    report = parse_compound_report(requests, responses, service_name = service_name,
                                   path = path, keep_data = keep_data)
    return report.export(file_obj, include_data = keep_data, indent = indent)


def make_report(operations = None, responses = None, requests = None, service_name = None,
                path = None, keep_data = True):
    """Build a report from a JSON batch, message list or raw buffer."""
    if responses is not None:
        return parse_compound_report(requests, responses, service_name = service_name,
                                     path = path, keep_data = keep_data)
    if operations is None:
        return parse_compound_report(requests, None, service_name = service_name,
                                     path = path, keep_data = keep_data)
    if isinstance(operations, dict):
        if 'responses' in operations:
            return parse_compound_report(requests, _decode_data(operations['responses']),
                                         service_name = operations.get('service_name', service_name),
                                         path = operations.get('path', path),
                                         keep_data = keep_data)
        return analyze_operations(operations, service_name = service_name,
                                  path = path, keep_data = keep_data)
    operations = list(operations)
    if operations and isinstance(operations[0], dict):
        return analyze_operations(operations, service_name = service_name,
                                  path = path, keep_data = keep_data)
    return parse_compound_report(requests, operations, service_name = service_name,
                                 path = path, keep_data = keep_data)


def export_batch(operations, service_name = None, path = None, keep_data = True, file_obj = None):
    """Analyse a JSON-style batch and optionally write its report."""
    return analyze_operations(operations, service_name = service_name, path = path,
                              keep_data = keep_data).export(file_obj, include_data = keep_data)


def compound_report(operations, service_name = None, path = None, keep_data = True):
    """Return a report for a JSON-style batch of operations."""
    return analyze_operations(operations, service_name = service_name, path = path,
                              keep_data = keep_data)


def export_compound_report(operations, service_name = None, path = None, keep_data = True,
                           file_obj = None):
    return analyze_operations(operations, service_name = service_name, path = path,
                              keep_data = keep_data).export(file_obj, include_data = keep_data)


def analyze_operations(operations, service_name = None, path = None, keep_data = True):
    """Analyse a JSON-style batch of sub-operations.

    Each record may contain ``operation``, ``status``, ``executed``,
    ``request_file_id``/``request_fid``, ``file_id``/``fid`` and
    ``data``/``data_base64``/``data_hex``. A missing request file id for a
    non-open operation means the all-ones placeholder. Records may also nest
    ``request`` and ``response`` dictionaries.
    """
    records = operations
    session_id = None
    tree_id = None
    if isinstance(operations, dict):
        records = operations.get('operations', operations.get('sub_operations', [ ]))
        if service_name is None:
            service_name = operations.get('service_name')
        if path is None:
            path = operations.get('path')
        session_id = operations.get('session_id')
        tree_id = operations.get('tree_id', operations.get('tid'))

    requests = [ ]
    responses = [ ]

    for record in records:
        if not isinstance(record, dict):
            record = { 'operation': str(record) }
        if 'request' in record or 'response' in record:
            top = dict(record)
            request_record = dict(record.get('request') or { })
            response_record = dict(record.get('response') or { })
            merged = dict(request_record)
            merged.update(response_record)
            for key in ( 'operation', 'op', 'command', 'executed', 'status', 'data',
                         'data_base64', 'data_hex', 'offset', 'file_id', 'fid',
                         'request_file_id', 'request_fid', 'mid' ):
                if key in top:
                    merged[key] = top[key]
                elif key in request_record and key not in merged:
                    merged[key] = request_record[key]
                elif key in response_record and key not in merged:
                    merged[key] = response_record[key]
            record = merged

        operation = record.get('operation', record.get('op', record.get('command')))
        command = operation_code(operation)
        normalized = OPERATION_NAMES.get(command, str(operation))
        executed = record.get('executed', True)
        status = record.get('status', None)
        if status is None and 'ok' in record:
            status = 0 if record.get('ok') else 0xC0000001
        if status is None and executed:
            status = 0
        if status is not None:
            status = status_value(status)

        if command == SMB2_COM_CREATE:
            request_fid = None
            result_fid = record.get('file_id', record.get('fid'))
        else:
            request_fid = record.get('request_file_id', record.get('request_fid'))
            result_fid = record.get('result_file_id', None)
            if request_fid is None:
                request_fid = record.get('fid')
            if request_fid is None:
                request_fid = FILE_ID_PLACEHOLDER

        request = {
            'command': command,
            'operation': normalized,
            'fid': request_fid,
            'mid': record.get('mid', 0),
        }
        if executed and status is not None:
            data_value = record.get('data', record.get('data_base64'))
            if data_value is None and record.get('data_hex') is not None:
                data_value = '0x' + str(record.get('data_hex')).strip()
            response = {
                'command': command,
                'operation': normalized,
                'status': status,
                'fid': result_fid,
                'mid': record.get('mid', 0),
                'offset': record.get('offset', 0),
                'data': _decode_data(data_value),
            }
        else:
            response = None
        requests.append(request)
        responses.append(response)

    report = parse_compound_report(_dict_requests(requests), _dict_responses(responses),
                                   service_name = service_name, path = path,
                                   keep_data = keep_data)
    if session_id is not None or tree_id is not None:
        for op in report.sub_operations:
            if op.context.session_id is None:
                op.context.session_id = session_id
            if op.context.tid is None:
                op.context.tid = tree_id
        if report.file_context.session_id is None:
            report.file_context.session_id = session_id
        if report.file_context.tid is None:
            report.file_context.tid = tree_id
    return report


class _DictRequest(object):
    def __init__(self, record):
        self.command = record.get('command')
        self.mid = record.get('mid', 0)
        self.payload = None
        self.fid = file_id_bytes(record.get('fid'))


class _DictResponse(object):
    def __init__(self, record):
        self.command = record.get('command')
        self.mid = record.get('mid', 0)
        self.status = record.get('status', 0)
        self.payload = _DictPayload(record)


class _DictPayload(object):
    def __init__(self, record):
        self.fid = record.get('fid')
        self.data = record.get('data', b'')
        self.offset = record.get('offset', 0)


def _dict_requests(records):
    return [ _DictRequest(record) for record in records ]


def _dict_responses(records):
    return [ None if record is None else _DictResponse(record) for record in records ]


def export_status_report(operations, service_name = None, path = None, keep_data = True):
    """Return the concise status/context rows for a batch."""
    return analyze_operations(operations, service_name = service_name, path = path,
                              keep_data = keep_data).statuses


def sub_operation_statuses(operations, service_name = None, path = None, keep_data = True):
    return export_status_report(operations, service_name = service_name, path = path,
                                keep_data = keep_data)


def export_sub_operation_statuses(operations, service_name = None, path = None, keep_data = True):
    return export_status_report(operations, service_name = service_name, path = path,
                                keep_data = keep_data)


def export_file_contexts(operations, service_name = None, path = None, keep_data = True):
    """Return only the effective file context for every sub-operation."""
    report = analyze_operations(operations, service_name = service_name, path = path,
                                keep_data = keep_data)
    return [ {
        'index': op.index,
        'operation': op.operation,
        'request_file_id': file_id_hex(op.request_file_id),
        'uses_placeholder': op.uses_placeholder,
        'effective_file_id': op.context.hex,
        'inherited': op.context.inherited,
        'source': op.context.source,
        'valid': op.context.valid,
        'unresolved': op.context.unresolved,
        'after_failure': op.after_failure,
    } for op in report.sub_operations ]


def export_resource_status(operations, service_name = None, path = None, keep_data = True):
    return analyze_operations(operations, service_name = service_name, path = path,
                              keep_data = keep_data).resource_status


def _main(argv = None):
    import argparse

    parser = argparse.ArgumentParser(
        prog = 'python -m smb.compound',
        description = 'Export SMB2 compound sub-operation status and file contexts.')
    parser.add_argument('input', nargs = '?',
                        help = 'JSON batch file or SMB2 response file; use - for stdin')
    parser.add_argument('-o', '--output', help = 'write the JSON report to this file')
    parser.add_argument('--format', choices = ( 'json', 'text' ), default = 'json')
    parser.add_argument('--no-data', action = 'store_true',
                        help = 'omit archived read bytes from the report')
    parser.add_argument('--save-reads', help = 'write successfully read bytes to this file')
    parser.add_argument('--requests', help = 'JSON request chain description')
    parser.add_argument('--responses', help = 'binary SMB2 compound response file')
    args = parser.parse_args(argv)

    if args.responses:
        with open(args.responses, 'rb') as f:
            response_data = f.read()
        requests = None
        if args.requests:
            with open(args.requests, 'r') as f:
                specs = json.load(f)
            requests = _dict_requests([ {
                'command': operation_code(item.get('operation', item.get('op', 'open'))),
                'mid': item.get('mid', 0),
                'fid': item.get('request_file_id', item.get('fid')),
            } for item in specs ])
        report = parse_compound_report(requests, response_data,
                                       keep_data = not args.no_data)
    else:
        if args.input in ( None, '-' ):
            payload = json.load(sys.stdin)
        else:
            with open(args.input, 'rb') as f:
                raw = f.read()
            try:
                payload = json.loads(raw.decode('utf-8'))
            except ( UnicodeDecodeError, ValueError ):
                payload = raw

        if isinstance(payload, dict) and 'responses' in payload:
            report = make_report(payload, keep_data = not args.no_data)
        elif isinstance(payload, ( bytes, bytearray )):
            report = parse_compound_report(None, payload, keep_data = not args.no_data)
        else:
            report = analyze_operations(payload, keep_data = not args.no_data)

    if args.save_reads:
        report.read_archive.save(args.save_reads)

    if args.format == 'text':
        text = report.export_text()
        if args.output:
            with open(args.output, 'w') as f:
                f.write(text)
        else:
            print(text)
        return 0

    if args.output:
        report.export(args.output, include_data = not args.no_data)
    else:
        print(json.dumps(report.to_dict(include_data = not args.no_data),
                         indent = 2, sort_keys = True))
    return 0


if __name__ == '__main__':
    sys.exit(_main())
