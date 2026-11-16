"""Bridge recorded DBus messages onto an isolated bus.

Recorded messages carry bus names, object paths, serials and UNIX_FD values
from the connection on which they were recorded.  :class:`MessageBridge`
translates those values into the namespace of an isolated bus and returns a
:class:`Message <dbus_next.Message>` ready to be sent there.

Body translation is signature driven.  Values of DBus type ``o`` are rewritten
wherever they appear: in nested arrays, dictionary keys and values, structs,
and variants.  Values of type ``s`` and ``g`` are never rewritten, even when
their contents look like object paths.

Bridging is all-or-nothing.  A missing connection or object path mapping, or
an unavailable UNIX_FD handle, rejects the whole message; no partial message
is returned for the caller to send.

The bridge remembers the serial assigned to every bridged method call.  A
reply received on the isolated bus can then be restored with
:meth:`MessageBridge.restore_reply` so it carries the original call's serial
and can be delivered to the recorded caller.
"""

from .constants import MessageFlag, MessageType
from .errors import (InvalidBusNameError, InvalidInterfaceNameError, InvalidMemberNameError,
                     InvalidMessageError, InvalidObjectPathError, SignatureBodyMismatchError)
from .message import Message
from .signature import Variant
from .validators import is_object_path_valid

from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import io


class BridgeError(Exception):
    """Base class for message bridge failures."""


class MissingMappingError(BridgeError):
    """A required connection or object path mapping is missing."""


class MissingUnixFdError(BridgeError):
    """A referenced UNIX_FD handle or index is missing."""


class InvalidBridgeBodyError(BridgeError):
    """The recorded body does not match its signature."""


_CONNECTION_KEYS = (
    'connections',
    'connection_map',
    'connections_map',
    'connection_mapping',
    'new_connections',
    'connection',
)
_PATH_KEYS = (
    'paths',
    'path_map',
    'path_mapping',
    'object_paths',
    'object_path_map',
    'object_path_mapping',
    'new_paths',
    'path',
    'object_path',
)
_FD_KEYS = ('fds', 'fd_map', 'fd_mapping', 'unix_fds', 'unix_fd_map', 'handles', 'handle_map')
_SERIAL_KEYS = ('serials', 'serial_map', 'serial_mapping', 'request_serials',
                'request_serial_map')
_REPLY_KEYS = ('reply_serials', 'reply_serial_map', 'reply_mapping', 'replies')
_ROUTE_KEYS = ('routes', 'route_table', 'routing', 'bridge_map', 'entries', 'mapping',
               'mappings')
_SETTING_KEYS = ('target_bus', 'next_serial', 'serial_source', 'start_serial', 'fd_lookup',
                 'fd_source')
_NAMED_KEYS = frozenset(_CONNECTION_KEYS + _PATH_KEYS + _FD_KEYS + _SERIAL_KEYS + _REPLY_KEYS +
                        _ROUTE_KEYS + _SETTING_KEYS)
_OLD_CONNECTION_KEYS = ('old_connection', 'original_connection', 'source_connection',
                        'from_connection')
_NEW_CONNECTION_KEYS = ('new_connection', 'target_connection', 'to_connection', 'new_bus')
_OLD_PATH_KEYS = ('object_path', 'old_path', 'original_path', 'source_path')
_NEW_PATH_KEYS = ('new_path', 'target_path', 'to_path')


def _connection_name(value: Any) -> Any:
    """Return the bus name represented by a connection-like value."""
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ('unique_name', 'bus_name', 'name', 'connection', 'destination'):
            if key in value:
                return _connection_name(value[key])
        return value
    for attribute in ('unique_name', 'bus_name', 'name'):
        candidate = getattr(value, attribute, None)
        if isinstance(candidate, str):
            return candidate
    return value


def _is_path(value: Any) -> bool:
    return isinstance(value, str) and value.startswith('/')


def _pairs(value: Any) -> List[Tuple[Any, Any]]:
    """Return a mapping-like value as key/value pairs."""
    if not value:
        return []
    if isinstance(value, Mapping):
        return list(value.items())
    if isinstance(value, (list, tuple)):
        result = []
        for index, item in enumerate(value):
            if isinstance(item, (list, tuple)) and len(item) == 2:
                result.append((item[0], item[1]))
            else:
                result.append((index, item))
        return result
    raise TypeError('a mapping or a sequence of pairs is required')


def _path_entry(value: Any, default_connection: Any = None):
    """Normalize an object path table value to (new_connection, new_path)."""
    if isinstance(value, Mapping):
        connection = value.get('connection', value.get('new_connection'))
        if connection is None:
            connection = value.get('bus', value.get('target'))
        path = value.get('path', value.get('new_path', value.get('object_path')))
        return connection, path
    if isinstance(value, (list, tuple)):
        if len(value) >= 2:
            first, second = value[0], value[1]
            if _is_path(first) and not _is_path(second):
                return second, first
            if _is_path(second):
                return first, second
            return first, second
        if len(value) == 1:
            if _is_path(value[0]):
                return default_connection, value[0]
            return value[0], None
    if _is_path(value):
        return default_connection, value
    if isinstance(value, str):
        return value, None
    return None, None


class _Mapping:
    """Normalized mapping tables."""

    def __init__(self):
        self.connections: Dict[Any, Any] = {}
        self.connection_targets: Dict[Any, Any] = {}
        self.connection_sources: Dict[Any, Any] = {}
        self.paths: Dict[str, str] = {}
        self.path_targets: Dict[str, Any] = {}
        self.fds: Dict[Any, Any] = {}
        self.serials: Dict[Any, int] = {}
        self.replies: Dict[Any, int] = {}
        self.target_bus: Any = None
        self.serial_source: Any = None
        self.start_serial: Optional[int] = None
        self.fd_lookup: Any = None

    def add_connection(self, old: Any, new: Any):
        old_name = _connection_name(old)
        new_name = _connection_name(new)
        if not isinstance(old_name, str) or not old_name:
            return
        if not isinstance(new_name, str) or not new_name:
            return
        self.connections[old_name] = new_name
        self.connection_targets[old_name] = new
        self.connection_sources[old_name] = old

    def add_path(self, old: Any, new: Any, connection: Any = None):
        if isinstance(old, str) and isinstance(new, str):
            self.paths[old] = new
        if connection is not None and isinstance(old, str):
            self.path_targets[old] = connection

    def add_serial(self, old: Any, new: Any, reply: bool = False):
        try:
            value = int(new)
        except (TypeError, ValueError):
            return
        table = self.replies if reply else self.serials
        if isinstance(old, (tuple, list)) and len(old) == 2:
            try:
                table[(_connection_name(old[0]), int(old[1]))] = value
            except (TypeError, ValueError):
                return
        else:
            try:
                table[int(old)] = value
            except (TypeError, ValueError):
                return

    def consume_named(self, table: Mapping):
        for key in _CONNECTION_KEYS:
            if table.get(key):
                for old, new in _pairs(table[key]):
                    self.add_connection(old, new)

        for key in _PATH_KEYS:
            if table.get(key):
                for old_path, value in _pairs(table[key]):
                    new_connection, new_path = _path_entry(value)
                    self.add_path(old_path, new_path if new_path is not None else old_path,
                                  new_connection)

        for key in _FD_KEYS:
            if table.get(key):
                for old, new in _pairs(table[key]):
                    self.fds[old] = new

        for key in _SERIAL_KEYS:
            if table.get(key):
                for old, new in _pairs(table[key]):
                    self.add_serial(old, new)
        for key in _REPLY_KEYS:
            if table.get(key):
                for old, new in _pairs(table[key]):
                    self.add_serial(old, new, reply=True)
        for key in _ROUTE_KEYS:
            if table.get(key):
                self.consume_routes(table[key])

        if table.get('target_bus') is not None:
            self.target_bus = table['target_bus']
        if table.get('next_serial') is not None:
            self.serial_source = table['next_serial']
        if table.get('serial_source') is not None:
            self.serial_source = table['serial_source']
        if table.get('start_serial') is not None:
            self.start_serial = int(table['start_serial'])
        if table.get('fd_lookup') is not None:
            self.fd_lookup = table['fd_lookup']
        if table.get('fd_source') is not None:
            self.fd_lookup = table['fd_source']

    def consume_routes(self, routes: Any):
        entries = routes.items() if isinstance(routes, Mapping) else routes
        for key, value in entries:
            if isinstance(key, (list, tuple)) and len(key) >= 2:
                old_connection, old_path = key[0], key[1]
                if len(key) > 2:
                    value = key[2:] if len(key) == 4 else key[2]
            elif _is_path(key):
                old_connection, old_path = None, key
            else:
                old_connection, old_path = key, None

            new_connection, new_path = _path_entry(value)
            if old_connection is not None and new_connection is None:
                new_connection = old_connection
            if old_connection is not None:
                self.add_connection(old_connection, new_connection)
            if old_path is not None:
                self.add_path(old_path, new_path if new_path is not None else old_path,
                              new_connection)

    def consume_nested(self, table: Mapping):
        for old_connection, entries in table.items():
            if not isinstance(entries, Mapping):
                self.add_connection(old_connection, entries)
                continue
            for old_path, value in entries.items():
                new_connection, new_path = _path_entry(value, old_connection)
                self.add_connection(old_connection, new_connection)
                self.add_path(old_path, new_path if new_path is not None else old_path,
                              new_connection)

    def consume_record(self, record: Mapping):
        old_connection = next(
            (record[key] for key in _OLD_CONNECTION_KEYS if key in record), None)
        new_connection = next(
            (record[key] for key in _NEW_CONNECTION_KEYS if key in record), None)
        old_path = next((record[key] for key in _OLD_PATH_KEYS if key in record), None)
        new_path = next((record[key] for key in _NEW_PATH_KEYS if key in record), None)

        if old_connection is not None:
            self.add_connection(old_connection, new_connection or old_connection)
        if old_path is not None:
            self.add_path(old_path, new_path if new_path is not None else old_path,
                          new_connection)
        if record.get('target_bus') is not None:
            self.target_bus = record['target_bus']
        if record.get('next_serial') is not None:
            self.serial_source = record['next_serial']
        if record.get('serial_source') is not None:
            self.serial_source = record['serial_source']

    @classmethod
    def from_value(cls, value: Any) -> '_Mapping':
        result = cls()
        if value is None:
            return result
        if isinstance(value, cls):
            return value
        if isinstance(value, MessageBridge):
            result.consume_named({
                'connections': value.connection_map,
                'paths': value.path_map,
                'fds': value.fd_map,
                'serials': value.serial_map,
                'reply_serials': value.reply_serial_map,
            })
            result.target_bus = value.target_bus
            result.serial_source = value.serial_source
            result.fd_lookup = value.fd_lookup
            return result

        if isinstance(value, Mapping):
            if any(key in _OLD_CONNECTION_KEYS + _NEW_CONNECTION_KEYS + _OLD_PATH_KEYS +
                   _NEW_PATH_KEYS for key in value):
                result.consume_record(value)
                return result

            if value and any(key in _NAMED_KEYS for key in value):
                result.consume_named(value)
                return result

            if value and all(isinstance(key, int) and not isinstance(key, bool)
                             for key in value):
                for old, new in value.items():
                    result.fds[old] = new
                return result

            nested = True
            flat_connections = True
            for key, item in value.items():
                if isinstance(key, (list, tuple)):
                    nested = flat_connections = False
                    break
                if _is_path(key):
                    flat_connections = False
                if isinstance(item, (list, tuple)):
                    flat_connections = False
                if not isinstance(item, Mapping) or not item:
                    nested = False
                elif not all(_is_path(path) for path in item):
                    nested = False
                elif any(not isinstance(path, str) for path in item):
                    nested = False

            if nested and value:
                result.consume_nested(value)
                return result
            if flat_connections and value:
                for old, new in value.items():
                    result.add_connection(old, new)
                return result
            result.consume_routes(value)
            return result

        if isinstance(value, (list, tuple)):
            if value and all(isinstance(item, Mapping) for item in value):
                for item in value:
                    result.consume_record(item)
                return result
            if value and all(isinstance(item, (list, tuple)) for item in value):
                for item in value:
                    if len(item) == 2:
                        result.consume_routes([item])
                    elif len(item) == 4:
                        result.consume_routes([((item[0], item[1]), (item[2], item[3]))])
                    else:
                        raise TypeError('route sequences must have 2 or 4 members')
                return result
            for index, item in enumerate(value):
                result.fds[index] = item
            return result

        raise TypeError('could not understand the bridge mapping table')


class _FdTransfer:
    """Map the recorded handle list to the isolated handle list."""

    def __init__(self, source_fds: Optional[Iterable[Any]], fd_map: Mapping,
                 fd_lookup: Any, mode: str, strict: bool):
        self.source_fds = list(source_fds or [])
        self.fd_map = dict(fd_map or {})
        self.fd_lookup = fd_lookup
        self.mode = mode
        self.strict = strict
        self.target_fds: List[Any] = []
        self.index_map: Dict[int, int] = {}

    def _append(self, handle: Any) -> int:
        if handle is None:
            raise MissingUnixFdError('file descriptor handle is null')
        index = len(self.target_fds)
        self.target_fds.append(handle)
        return index

    def _lookup_handle(self, index: int, source_fd: Any) -> Any:
        if self.fd_lookup is not None:
            if callable(self.fd_lookup):
                try:
                    return self.fd_lookup(index, source_fd)
                except TypeError:
                    return self.fd_lookup(index)
            if isinstance(self.fd_lookup, Mapping):
                if source_fd in self.fd_lookup:
                    return self.fd_lookup[source_fd]
                if index in self.fd_lookup:
                    return self.fd_lookup[index]
                raise MissingUnixFdError(
                    f'no file descriptor mapping for index {index} (fd {source_fd})')
            try:
                return self.fd_lookup[index]
            except (IndexError, KeyError, TypeError):
                raise MissingUnixFdError(f'no file descriptor mapping for index {index}')

        if not self.fd_map:
            return source_fd

        if self.mode == 'fd':
            if source_fd not in self.fd_map:
                raise MissingUnixFdError(f'no file descriptor mapping for fd {source_fd}')
            return self.fd_map[source_fd]
        if self.mode == 'index':
            if index not in self.fd_map:
                raise MissingUnixFdError(f'no file descriptor mapping for index {index}')
            return self.fd_map[index]
        if source_fd in self.fd_map:
            return self.fd_map[source_fd]
        if index in self.fd_map:
            return self.fd_map[index]
        raise MissingUnixFdError(
            f'no file descriptor mapping for index {index} (fd {source_fd})')

    def build(self) -> List[Any]:
        if not self.fd_map and self.fd_lookup is None:
            self.index_map = {index: index for index in range(len(self.source_fds))}
            return list(self.source_fds)

        for index, source_fd in enumerate(self.source_fds):
            try:
                handle = self._lookup_handle(index, source_fd)
            except MissingUnixFdError:
                if self.strict:
                    raise
                continue
            self.index_map[index] = self._append(handle)
        return list(self.target_fds)

    def translate_index(self, index: Any) -> int:
        if not isinstance(index, int) or isinstance(index, bool):
            raise MissingUnixFdError(f'UNIX_FD index must be an integer, got {type(index)}')
        if index < 0 or index >= len(self.source_fds):
            raise MissingUnixFdError(
                f'UNIX_FD index {index} is out of range for {len(self.source_fds)} handles')
        if index not in self.index_map:
            raise MissingUnixFdError(f'UNIX_FD index {index} has no handle')
        return self.index_map[index]


class _Transfer:
    def __init__(self, paths: Mapping, connections: Mapping, path_targets: Mapping,
                 fd_transfer: _FdTransfer, require_paths: bool, require_connections: bool):
        self.paths = paths
        self.connections = connections
        self.path_targets = path_targets
        self.fd_transfer = fd_transfer
        self.require_paths = require_paths
        self.require_connections = require_connections

    def path(self, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            raise InvalidBridgeBodyError(f'object path must be a string, got {type(value)}')
        if value in self.paths:
            mapped = self.paths[value]
            if not is_object_path_valid(mapped):
                raise MissingMappingError(f'mapped object path is invalid: {mapped}')
            return mapped
        if self.require_paths:
            raise MissingMappingError(f'no object path mapping for "{value}"')
        if not is_object_path_valid(value):
            raise InvalidBridgeBodyError(f'object path value is invalid: {value}')
        return value

    def connection(self, value: Any, path: Any = None, required: Optional[bool] = None) -> Any:
        name = _connection_name(value)
        if name is not None and name in self.connections:
            return _connection_name(self.connections[name])
        if path is not None and path in self.path_targets:
            target = _connection_name(self.path_targets[path])
            if isinstance(target, str) and target:
                return target
        if name is None:
            return None
        if required is None:
            required = self.require_connections
        if required:
            raise MissingMappingError(f'no connection mapping for "{name}"')
        return name

    def fd(self, value: Any) -> int:
        return self.fd_transfer.translate_index(value)


def _map_value(type_, value: Any, transfer: _Transfer) -> Any:
    token = type_.token

    if token == 'o':
        return transfer.path(value)
    if token in ('s', 'g'):
        return value
    if token == 'h':
        return transfer.fd(value)
    if token in 'ybnqiuxtd' or value is None:
        return value

    if token == 'v':
        if not isinstance(value, Variant):
            raise InvalidBridgeBodyError(f'variant body must be a Variant, got {type(value)}')
        return Variant(value.type, _map_value(value.type, value.value, transfer), verify=False)

    if token == '(':
        if not isinstance(value, (list, tuple)):
            raise InvalidBridgeBodyError(f'struct body must be a list, got {type(value)}')
        if len(value) != len(type_.children):
            raise InvalidBridgeBodyError(
                f'struct has {len(value)} members, expected {len(type_.children)}')
        return [_map_value(child, item, transfer) for child, item in zip(type_.children, value)]

    if token == '{':
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise InvalidBridgeBodyError(f'dict entry must have two members, got {value!r}')
        return [
            _map_value(type_.children[0], value[0], transfer),
            _map_value(type_.children[1], value[1], transfer),
        ]

    if token == 'a':
        child = type_.children[0]
        if child.token == '{':
            if not isinstance(value, Mapping):
                raise InvalidBridgeBodyError(
                    f'array of dict entries must be a mapping, got {type(value)}')
            key_type, value_type = child.children
            result = {}
            for key, item in value.items():
                mapped_key = _map_value(key_type, key, transfer)
                if mapped_key in result:
                    raise BridgeError(
                        f'object path mapping produced a duplicate key: {mapped_key}')
                result[mapped_key] = _map_value(value_type, item, transfer)
            return result

        if child.token == 'y':
            return value

        if not isinstance(value, (list, tuple)):
            raise InvalidBridgeBodyError(f'array body must be a list, got {type(value)}')
        return [_map_value(child, item, transfer) for item in value]

    return value


def _map_body(body: Any, types, transfer: _Transfer) -> List[Any]:
    if not isinstance(body, (list, tuple)):
        raise InvalidBridgeBodyError(f'message body must be a list, got {type(body)}')
    if len(body) != len(types):
        raise InvalidBridgeBodyError(
            f'message body has {len(body)} members, expected {len(types)}')
    try:
        return [_map_value(type_, value, transfer) for type_, value in zip(types, body)]
    except SignatureBodyMismatchError as exc:
        raise InvalidBridgeBodyError(str(exc))


def _coerce_message(value: Any) -> Message:
    """Accept a Message, recorded message bytes, or a recorded message dict."""
    if isinstance(value, Message):
        return value

    if isinstance(value, (bytes, bytearray, memoryview)):
        from ._private.unmarshaller import Unmarshaller

        message = Unmarshaller(io.BytesIO(bytes(value))).unmarshall()
        if message is None:
            raise BridgeError('recorded message data is incomplete')
        return message

    if isinstance(value, Mapping):
        if 'message' in value:
            return _coerce_message(value['message'])
        data = dict(value)
        message_type = data.get('message_type', data.get('type', MessageType.METHOD_CALL))
        if isinstance(message_type, int):
            message_type = MessageType(message_type)
        flags = data.get('flags', MessageFlag.NONE)
        if isinstance(flags, int):
            flags = MessageFlag(flags)
        return Message(destination=data.get('destination'),
                       path=data.get('path'),
                       interface=data.get('interface'),
                       member=data.get('member'),
                       message_type=message_type,
                       flags=flags,
                       error_name=data.get('error_name'),
                       reply_serial=data.get('reply_serial'),
                       sender=data.get('sender'),
                       unix_fds=list(data.get('unix_fds') or []),
                       signature=data.get('signature', ''),
                       body=list(data.get('body') or []),
                       serial=data.get('serial', 0))

    if isinstance(value, (tuple, list)) and len(value) == 2:
        if isinstance(value[0], Message):
            return value[0]
        if isinstance(value[1], Message):
            return value[1]

    raise TypeError(f'cannot bridge message of type {type(value)}')


class _PendingCall:
    def __init__(self, original: Message, bridged_serial: int, original_connection: Any,
                 target_name: Any, target_scope: Iterable[Any] = ()):
        self.original = original
        self.bridged_serial = bridged_serial
        self.original_serial = original.serial
        self.original_connection = original_connection
        self.target_name = target_name
        self.target_scope = tuple(target_scope)


class MessageBridge:
    """Translate recorded messages into messages for an isolated bus.

    The mapping table may be given as the first argument.  It accepts flat
    connection maps, flat object path maps, named sections, nested
    connection/path tables, and route tables keyed by
    ``(old_connection, old_path)``.  Individual tables can also be passed by
    keyword.

    :param mapping: Combined mapping table.
    :param path_map: Recorded object path -> isolated object path.
    :param fd_map: Recorded UNIX_FD handle (or index) -> isolated handle.
    :param serial_map: Recorded serial -> isolated serial.
    :param reply_serial_map: Explicit reply serial mapping.
    :param connection_map: Recorded connection -> isolated connection.
    :param target_bus: Bus used for sending when a mapping value is a name.
    :param next_serial: Callable, iterator, or object with ``next_serial()``.
    :param strict: Reject a message when a required mapping is absent.
    :param raise_on_missing: Raise bridge errors instead of returning ``None``.
    """

    def __init__(self,
                 mapping: Any = None,
                 path_map: Optional[Mapping] = None,
                 fd_map: Optional[Mapping] = None,
                 serial_map: Optional[Mapping] = None,
                 reply_serial_map: Optional[Mapping] = None,
                 *,
                 connection_map: Optional[Mapping] = None,
                 connections: Optional[Mapping] = None,
                 connection_mapping: Optional[Mapping] = None,
                 paths: Optional[Mapping] = None,
                 object_paths: Optional[Mapping] = None,
                 object_path_map: Optional[Mapping] = None,
                 object_path_mapping: Optional[Mapping] = None,
                 fds: Optional[Mapping] = None,
                 unix_fds: Optional[Mapping] = None,
                 handles: Optional[Mapping] = None,
                 handle_map: Optional[Mapping] = None,
                 serials: Optional[Mapping] = None,
                 request_serials: Optional[Mapping] = None,
                 replies: Optional[Mapping] = None,
                 reply_serials: Optional[Mapping] = None,
                 next_serial: Any = None,
                 serial_source: Any = None,
                 allocate_serials: Optional[bool] = None,
                 start_serial: int = 1,
                 target_bus: Any = None,
                 strict: bool = True,
                 require_path_mapping: Optional[bool] = None,
                 require_connection_mapping: Optional[bool] = None,
                 fd_mode: str = 'auto',
                 fd_lookup: Any = None,
                 raise_on_missing: bool = False):
        self.strict = strict
        self.raise_on_missing = raise_on_missing
        self.fd_mode = fd_mode

        maps = _Mapping.from_value(mapping)

        for table in (connection_map, connections, connection_mapping):
            for old, value in _pairs(table):
                if _is_path(old):
                    new_connection, new_path = _path_entry(value)
                    maps.add_path(old, new_path if new_path is not None else old,
                                  new_connection)
                else:
                    maps.add_connection(old, value)
        for table in (path_map, paths, object_paths, object_path_map, object_path_mapping):
            for old_path, value in _pairs(table):
                if not _is_path(old_path) and not _is_path(value) and not isinstance(
                        value, (Mapping, list, tuple)):
                    # A connection table passed in an object path position.
                    maps.add_connection(old_path, value)
                    continue
                new_connection, new_path = _path_entry(value)
                maps.add_path(old_path, new_path if new_path is not None else old_path,
                              new_connection)
        for table in (fd_map, fds, unix_fds, handles, handle_map):
            for old, new in _pairs(table):
                maps.fds[old] = new
        for table in (serial_map, serials, request_serials):
            for old, new in _pairs(table):
                maps.add_serial(old, new)
        for table in (reply_serial_map, replies, reply_serials):
            for old, new in _pairs(table):
                maps.add_serial(old, new, reply=True)

        if next_serial is not None and serial_source is not None:
            raise TypeError('give only one of next_serial or serial_source')
        if serial_source is not None:
            maps.serial_source = serial_source
        elif next_serial is not None:
            maps.serial_source = next_serial
        if target_bus is not None:
            maps.target_bus = target_bus
        if fd_lookup is not None:
            maps.fd_lookup = fd_lookup
        if maps.start_serial is not None:
            start_serial = maps.start_serial

        self.connection_map: Dict[Any, Any] = dict(maps.connections)
        self.connection_targets: Dict[Any, Any] = dict(maps.connection_targets)
        self.connection_sources: Dict[Any, Any] = dict(maps.connection_sources)
        self.path_map: Dict[str, str] = dict(maps.paths)
        self.path_targets: Dict[str, Any] = dict(maps.path_targets)
        self.fd_map: Dict[Any, Any] = dict(maps.fds)
        self.serial_map: Dict[Any, int] = dict(maps.serials)
        self.reply_serial_map: Dict[Any, int] = dict(maps.replies)
        self.target_bus = maps.target_bus
        self.serial_source = maps.serial_source
        self.fd_lookup = maps.fd_lookup
        self.start_serial = int(start_serial)
        self.allocate_serials = allocate_serials

        self.require_path_mapping = (bool(self.path_map)
                                     if require_path_mapping is None else require_path_mapping)
        self.require_connection_mapping = (
            bool(self.connection_map)
            if require_connection_mapping is None else require_connection_mapping)

        self._counter = self.start_serial - 1
        self._serial_iterator = None
        self._allocated_serials: Dict[Any, int] = {}
        self._pending_by_new_serial: Dict[int, List[_PendingCall]] = {}
        self._pending_by_original_serial: Dict[int, List[_PendingCall]] = {}

        self._inverse_paths = self._reverse(self.path_map)
        self._inverse_connections = self._reverse(self.connection_map)
        self._inverse_fds = self._reverse(self.fd_map)
        self._inverse_serials = self._reverse(self.serial_map)
        self._inverse_replies = self._reverse(self.reply_serial_map)

    @staticmethod
    def _reverse(table: Mapping) -> Dict[Any, Any]:
        result = {}
        for key, value in table.items():
            try:
                if value not in result:
                    result[value] = key
            except TypeError:
                continue
        return result

    def add_connection(self, old: Any, new: Any):
        """Add or replace a connection mapping."""
        maps = _Mapping()
        maps.add_connection(old, new)
        self.connection_map.update(maps.connections)
        self.connection_targets.update(maps.connection_targets)
        self.connection_sources.update(maps.connection_sources)
        self._inverse_connections = self._reverse(self.connection_map)
        self.require_connection_mapping = True

    def add_path(self, old: str, new: str, connection: Any = None):
        """Add or replace an object path mapping."""
        self.path_map[old] = new
        self._inverse_paths = self._reverse(self.path_map)
        if connection is not None:
            self.path_targets[old] = connection
        self.require_path_mapping = True

    def add_fd(self, old: Any, new: Any):
        """Add or replace a UNIX_FD mapping."""
        self.fd_map[old] = new
        self._inverse_fds = self._reverse(self.fd_map)

    def add_serial(self, old: Any, new: int):
        """Add or replace a serial mapping."""
        maps = _Mapping()
        maps.add_serial(old, new)
        self.serial_map.update(maps.serials)
        self._inverse_serials = self._reverse(self.serial_map)

    def add_reply_serial(self, old: Any, new: int):
        """Add or replace a reply serial mapping."""
        maps = _Mapping()
        maps.add_serial(old, new, reply=True)
        self.reply_serial_map.update(maps.replies)
        self._inverse_replies = self._reverse(self.reply_serial_map)

    def _scope_names(self, message: Message) -> Tuple[Any, ...]:
        names = []
        for value in (message.sender, message.destination):
            name = _connection_name(value)
            if name is not None and name not in names:
                names.append(name)
        target = _connection_name(self.connection_targets.get(_connection_name(
            message.destination), self.target_bus))
        if isinstance(target, str) and target and target not in names:
            names.append(target)
        return tuple(names)

    def _table_get(self, table: Mapping, message: Message, serial: int) -> Optional[int]:
        for name in self._scope_names(message):
            key = (name, serial)
            if key in table:
                value = table[key]
                return int(value[1]) if isinstance(value, tuple) else int(value)
        if serial in table:
            value = table[serial]
            return int(value[1]) if isinstance(value, tuple) else int(value)
        return None

    def _target_for(self, message: Message) -> Any:
        for value in (message.destination, message.sender):
            name = _connection_name(value)
            if name in self.connection_targets:
                return self.connection_targets[name]
        if message.path in self.path_targets:
            return self.path_targets[message.path]
        return self.target_bus

    def _allocator(self, message: Message) -> Any:
        if self.serial_source is not None:
            return self.serial_source
        target = self._target_for(message)
        if target is not None and hasattr(target, 'next_serial'):
            return target
        return None

    def _next_serial(self, message: Message) -> int:
        allocator = self._allocator(message)
        if allocator is None:
            self._counter += 1
            return self._counter
        try:
            if hasattr(allocator, 'next_serial'):
                return int(allocator.next_serial())
            if callable(allocator):
                return int(allocator())
            if hasattr(allocator, '__next__'):
                return int(next(allocator))
            if hasattr(allocator, '__iter__'):
                if self._serial_iterator is None:
                    self._serial_iterator = iter(allocator)
                return int(next(self._serial_iterator))
        except StopIteration:
            raise BridgeError('serial source is exhausted')
        raise BridgeError('serial source must be callable, iterable, or have next_serial()')

    def _serial_for(self, message: Message) -> int:
        old_serial = message.serial
        mapped = self._table_get(self.serial_map, message, old_serial) if old_serial else None
        if mapped is not None:
            return mapped

        if self.allocate_serials is False:
            if not old_serial:
                self._counter += 1
                return self._counter
            return old_serial

        scope = self._scope_names(message)
        if old_serial and (scope, old_serial) in self._allocated_serials:
            return self._allocated_serials[(scope, old_serial)]

        serial = self._next_serial(message)
        if not serial:
            raise BridgeError('serial source returned an invalid serial')
        if old_serial:
            self._allocated_serials[(scope, old_serial)] = serial
        return serial

    def _pending_for(self, reply_serial: Optional[int],
                     message: Optional[Message] = None) -> Optional[_PendingCall]:
        if reply_serial is None:
            return None
        candidates = self._pending_by_new_serial.get(reply_serial)
        if not candidates:
            candidates = self._pending_by_original_serial.get(reply_serial)
        if not candidates:
            return None
        if len(candidates) == 1 or message is None:
            return candidates[-1]

        sender = _connection_name(message.sender)
        destination = _connection_name(message.destination)
        for candidate in reversed(candidates):
            if (candidate.original_serial == reply_serial and
                    candidate.bridged_serial == reply_serial):
                if sender in candidate.target_scope or destination in candidate.target_scope:
                    return candidate
            if candidate.target_name in (sender, destination):
                return candidate
        return candidates[-1]

    def _reply_serial_for(self, message: Message) -> Optional[int]:
        if message.message_type not in (MessageType.METHOD_RETURN, MessageType.ERROR):
            return message.reply_serial
        reply_serial = message.reply_serial
        if reply_serial is None:
            return None
        pending = self._pending_for(reply_serial, message)
        if pending is not None:
            return pending.bridged_serial
        mapped = self._table_get(self.reply_serial_map, message, reply_serial)
        if mapped is not None:
            return mapped
        mapped = self._table_get(self.serial_map, message, reply_serial)
        if mapped is not None:
            return mapped
        if (self.strict and (self.serial_map or self.reply_serial_map)):
            raise MissingMappingError(
                f'no bridged request found for reply serial {reply_serial}')
        return reply_serial

    def _remember(self, original: Message, bridged: Message):
        original_connection = None
        for value in (original.sender, original.destination):
            name = _connection_name(value)
            if name in self.connection_sources:
                candidate = self.connection_sources[name]
                if original_connection is None or not isinstance(candidate, str):
                    original_connection = candidate
                if not isinstance(candidate, str):
                    break

        pending = _PendingCall(original, bridged.serial, original_connection,
                               _connection_name(bridged.destination),
                               self._scope_names(original))
        self._pending_by_new_serial.setdefault(bridged.serial, []).append(pending)
        self._pending_by_original_serial.setdefault(original.serial, []).append(pending)
        return pending

    def _transfer(self,
                  message: Message,
                  paths: Mapping,
                  connections: Mapping,
                  path_targets: Mapping,
                  fds: Mapping,
                  fd_lookup: Any,
                  require_paths: bool,
                  require_connections: bool,
                  serial: Optional[int] = None,
                  reply_serial: Optional[int] = None) -> Message:
        fd_transfer = _FdTransfer(message.unix_fds, fds, fd_lookup, self.fd_mode, self.strict)
        # Build the target handle list first so body indices refer to it.
        unix_fds = fd_transfer.build()
        transfer = _Transfer(paths, connections, path_targets, fd_transfer, require_paths,
                             require_connections)

        body = _map_body(list(message.body), message.signature_tree.types, transfer)
        path = transfer.path(message.path)
        destination = transfer.connection(message.destination, message.path)
        sender = transfer.connection(message.sender, required=False)

        if serial is None:
            serial = message.serial
        if reply_serial is None and message.message_type in (MessageType.METHOD_RETURN,
                                                             MessageType.ERROR):
            reply_serial = message.reply_serial

        return Message(destination=destination,
                       path=path,
                       interface=message.interface,
                       member=message.member,
                       message_type=message.message_type,
                       flags=message.flags,
                       error_name=message.error_name,
                       reply_serial=reply_serial,
                       sender=sender,
                       unix_fds=unix_fds,
                       signature=message.signature,
                       body=body,
                       serial=serial)

    def _drop_or_raise(self, exc: Exception):
        if self.raise_on_missing:
            raise exc
        return None

    def bridge_message(self, message: Any) -> Optional[Message]:
        """Return a sendable message in the isolated bus scope.

        ``None`` means the message must not be sent because a mapping or
        handle was missing.
        """
        try:
            record = _coerce_message(message)
        except TypeError:
            raise
        except Exception as exc:
            return self._drop_or_raise(BridgeError(f'invalid recorded message: {exc}'))

        try:
            serial = self._serial_for(record)
            bridged = self._transfer(record,
                                     self.path_map,
                                     self.connection_map,
                                     self.path_targets,
                                     self.fd_map,
                                     self.fd_lookup,
                                     self.require_path_mapping and self.strict,
                                     self.require_connection_mapping and self.strict,
                                     serial=serial,
                                     reply_serial=self._reply_serial_for(record))
        except (BridgeError, InvalidMessageError, InvalidBusNameError, InvalidObjectPathError,
                InvalidInterfaceNameError, InvalidMemberNameError,
                SignatureBodyMismatchError) as exc:
            return self._drop_or_raise(exc)

        if record.message_type == MessageType.METHOD_CALL:
            self._remember(record, bridged)
        return bridged

    # The operation has several natural names; keep them consistent.
    translate_message = bridge_message
    bridge = bridge_message
    map_message = bridge_message
    remap_message = bridge_message
    convert_message = bridge_message
    process_message = bridge_message

    def connection_for(self, message: Any) -> Any:
        """Return the isolated-side connection for a recorded message."""
        record = _coerce_message(message)
        for value in (record.destination, record.sender):
            name = _connection_name(value)
            if name in self.connection_targets:
                target = self.connection_targets[name]
                if not isinstance(target, str):
                    return target
        if record.path in self.path_targets:
            target = self.path_targets[record.path]
            if not isinstance(target, str):
                return target
        return self.target_bus

    def send(self, message: Any, connection: Any = None) -> bool:
        """Bridge and send a message.  Returns true only if it was sent."""
        bridged = self.bridge_message(message)
        if bridged is None:
            return False
        target = connection if connection is not None else self.connection_for(message)
        if target is None or not hasattr(target, 'send'):
            return False
        target.send(bridged)
        return True

    bridge_and_send = send

    def send_all(self, messages: Iterable[Any], connection: Any = None) -> int:
        """Bridge and send every message with complete mappings."""
        return sum(1 for message in messages if self.send(message, connection))

    def restore_reply(self, message: Any) -> Optional[Message]:
        """Restore a reply from the isolated bus to its recorded call.

        The result has the original call's serial in ``reply_serial`` and is
        addressed to the recorded caller when that information is available.
        """
        try:
            record = _coerce_message(message)
        except Exception as exc:
            return self._drop_or_raise(BridgeError(f'invalid reply message: {exc}'))

        pending = self._pending_for(record.reply_serial, record)
        if pending is not None:
            reply_serial = pending.original_serial
        else:
            reply_serial = None
            for table, inverse in ((self.reply_serial_map, self._inverse_replies),
                                   (self.serial_map, self._inverse_serials)):
                mapped = self._table_get(table, record, record.reply_serial)
                if mapped is not None:
                    reply_serial = mapped
                    break
                if record.reply_serial in inverse:
                    value = inverse[record.reply_serial]
                    reply_serial = int(value[1]) if isinstance(value, tuple) else int(value)
                    break
            if reply_serial is None:
                return None

        try:
            restored = self._transfer(record,
                                      self._inverse_paths,
                                      self._inverse_connections,
                                      {},
                                      self._inverse_fds,
                                      None,
                                      self.require_path_mapping and self.strict,
                                      self.require_connection_mapping and self.strict,
                                      serial=0,
                                      reply_serial=reply_serial)
        except (BridgeError, InvalidMessageError, InvalidBusNameError, InvalidObjectPathError,
                InvalidInterfaceNameError, InvalidMemberNameError,
                SignatureBodyMismatchError) as exc:
            return self._drop_or_raise(exc)

        if pending is not None:
            destination = pending.original.sender
            if destination is None:
                destination = _connection_name(pending.original_connection)
            if destination is not None:
                restored.destination = destination
            if pending.original.destination is not None:
                restored.sender = pending.original.destination
        return restored

    unbridge_message = restore_reply
    bridge_reply = restore_reply
    translate_reply = restore_reply
    map_reply = restore_reply
    route_reply = restore_reply
    reply_to_original = restore_reply

    def send_reply(self, message: Any, connection: Any = None) -> bool:
        """Restore a reply and send it to the recorded caller's connection."""
        record = _coerce_message(message)
        pending = self._pending_for(record.reply_serial, record)
        restored = self.restore_reply(record)
        if restored is None or pending is None:
            return False
        target = connection if connection is not None else pending.original_connection
        if target is None or not hasattr(target, 'send'):
            return False
        target.send(restored)
        return True

    @property
    def pending_replies(self) -> Dict[int, Message]:
        """Isolated serials awaiting replies, mapped to recorded calls."""
        result = {}
        for serial, candidates in self._pending_by_new_serial.items():
            if candidates:
                result[serial] = candidates[-1].original
        return result

    pending = pending_replies
    reply_map = pending_replies

    def original_message(self, serial: int) -> Optional[Message]:
        """Return the recorded call associated with a bridged serial."""
        pending = self._pending_for(serial)
        return pending.original if pending is not None else None

    def original_serial(self, serial: int) -> Optional[int]:
        """Return the recorded serial associated with a bridged serial."""
        pending = self._pending_for(serial)
        return pending.original_serial if pending is not None else None

    def forget_pending(self, serial: int):
        """Forget pending calls associated with an isolated serial."""
        candidates = self._pending_by_new_serial.pop(serial, [])
        for pending in candidates:
            remaining = self._pending_by_original_serial.get(pending.original_serial)
            if remaining and pending in remaining:
                remaining.remove(pending)


Bridge = MessageBridge
MessageTranslator = MessageBridge
MessageRouter = MessageBridge
RecordedMessageBridge = MessageBridge


def _build_bridge(args, kwargs) -> MessageBridge:
    if args and isinstance(args[0], MessageBridge):
        bridge = args[0]
        if len(args) > 1 or kwargs:
            raise TypeError('unexpected arguments given with a bridge instance')
        return bridge

    mapping = kwargs.pop('mapping', kwargs.pop('table', None))
    if mapping is None and args:
        mapping = args[0]
        args = args[1:]

    for name, value in zip(('path_map', 'fd_map', 'serial_map'), args):
        kwargs[name] = value
    args = args[3:]
    if args:
        raise TypeError('too many positional mapping arguments')
    return MessageBridge(mapping, **kwargs)


def bridge_message(message: Any = None, *args, **kwargs) -> Optional[Message]:
    """Bridge one recorded message.

    The message may be followed by a :class:`MessageBridge` instance, or by
    mapping arguments accepted by :class:`MessageBridge`.
    """
    if isinstance(message, MessageBridge):
        if not args:
            raise TypeError('a message is required after the bridge')
        return message.bridge_message(args[0])
    return _build_bridge(args, kwargs).bridge_message(message)


def bridge_messages(messages: Iterable[Any], *args, **kwargs) -> List[Message]:
    """Bridge messages, omitting those with missing mappings."""
    bridge = kwargs.pop('bridge', None)
    if bridge is None and args and isinstance(args[0], MessageBridge):
        bridge = args[0]
        args = args[1:]
    if bridge is None:
        bridge = _build_bridge(args, kwargs)

    result = []
    for message in messages:
        bridged = bridge.bridge_message(message)
        if bridged is not None:
            result.append(bridged)
    return result


# Common aliases for the module-level operation.
translate_message = bridge_message
remap_message = bridge_message
map_message = bridge_message
convert_message = bridge_message
build_message = bridge_message
recorded_message_to_message = bridge_message
bridge_recorded_message = bridge_message
message_for_isolated_bus = bridge_message
bridge_recorded_messages = bridge_messages
map_messages = bridge_messages
translate_bridge = bridge_messages


__all__ = [
    'Bridge',
    'BridgeError',
    'InvalidBridgeBodyError',
    'MessageBridge',
    'MessageRouter',
    'MessageTranslator',
    'MissingMappingError',
    'MissingUnixFdError',
    'RecordedMessageBridge',
    'bridge_message',
    'bridge_messages',
    'bridge_recorded_message',
    'bridge_recorded_messages',
    'build_message',
    'convert_message',
    'map_message',
    'map_messages',
    'message_for_isolated_bus',
    'recorded_message_to_message',
    'remap_message',
    'translate_message',
]
