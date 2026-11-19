# python-mpd2: Python MPD client library
#
# Copyright (C) 2008-2010  J. Alexander Treuman <jat@spatialrift.net>
# Copyright (C) 2012  J. Thalheim <jthalheim@gmail.com>
# Copyright (C) 2016  Robert Niederreiter <rnix@squarewave.at>
#
# python-mpd2 is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# python-mpd2 is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with python-mpd2.  If not, see <http://www.gnu.org/licenses/>.

import logging
import re
import socket
import sys
import warnings
from enum import Enum
from logging import NullHandler
from typing import (
    IO,
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Tuple,
    Iterable,
    Type,
    Union,
)

VERSION = (3, 1, 2)
HELLO_PREFIX = "OK MPD "
ERROR_PREFIX = "ACK "
ERROR_PATTERN = re.compile(
    r"\[(?P<errno>\d+)@(?P<offset>\d+)\]\s+{(?P<command>\w+)}\s+(?P<msg>.*)"
)
SUCCESS = "OK"
NEXT = "list_OK"

logger = logging.getLogger(__name__)
logger.addHandler(NullHandler())


def escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


# MPD Protocol errors as found in CommandError exceptions
# https://github.com/MusicPlayerDaemon/MPD/blob/master/src/protocol/Ack.hxx
class FailureResponseCode(Enum):
    NOT_LIST = 1
    ARG = 2
    PASSWORD = 3
    PERMISSION = 4
    UNKNOWN = 5

    NO_EXIST = 50
    PLAYLIST_MAX = 51
    SYSTEM = 52
    PLAYLIST_LOAD = 53
    UPDATE_ALREADY = 54
    PLAYER_SYNC = 55
    EXIST = 56


class MPDError(Exception):
    pass


class ConnectionError(MPDError):
    pass


class ProtocolError(MPDError):
    pass


class CommandError(MPDError):
    def __init__(self, error: str) -> None:
        self.errno = None
        self.offset = None
        self.command = None
        self.msg = None

        match = ERROR_PATTERN.match(error)
        if match:
            self.errno = FailureResponseCode(int(match.group("errno")))
            self.offset = int(match.group("offset"))
            self.command = match.group("command")
            self.msg = match.group("msg")


class CommandListError(MPDError):
    pass


class PendingCommandError(MPDError):
    pass


class IteratingError(MPDError):
    pass


class CallableWithCommands:
    mpd_commands: Tuple[str, ...] = ()
    mpd_commands_direct: bool = False
    mpd_commands_binary: bool = False

    def __call__(self, *args: Any) -> Any:
        pass


class mpd_commands:
    """Decorator for registering MPD commands with it's corresponding result
    callback.
    """

    def __init__(self, *commands: str, **kwargs: bool) -> None:
        self.commands = commands
        self.is_direct = kwargs.pop("is_direct", False)
        self.is_binary = kwargs.pop("is_binary", False)
        if kwargs:
            raise AttributeError(
                "mpd_commands() got unexpected keyword arguments %s" % ",".join(kwargs)
            )

    def __call__(self, ob: Any) -> CallableWithCommands:
        ob.mpd_commands = self.commands
        ob.mpd_commands_direct = self.is_direct
        ob.mpd_commands_binary = self.is_binary
        return ob


class Noop(object):
    """An instance of this class represents a MPD command callback which
    does nothing.
    """

    mpd_commands = None


class MPDClientBase:
    """Abstract MPD client.

    This class defines a general client contract, provides MPD protocol parsers
    and defines all available MPD commands and it's corresponding result
    parsing callbacks. There might be the need of overriding some callbacks on
    subclasses.
    """

    def __init__(self, use_unicode: Optional[bool] = None) -> None:
        self.iterate = False
        if use_unicode is not None:
            warnings.warn(
                "use_unicode parameter to ``MPDClientBase`` constructor is deprecated",
                DeprecationWarning,
                stacklevel=2,
            )
        self._reset()

    @property
    def use_unicode(self) -> bool:
        warnings.warn(
            "``use_unicode`` is deprecated: python-mpd 2.x always uses Unicode",
            DeprecationWarning,
            stacklevel=2,
        )
        return True

    @classmethod
    def add_command(cls, name: str, callback: Any) -> None:
        raise NotImplementedError(
            "Abstract ``MPDClientBase`` does not implement ``add_command``"
        )

    def noidle(self) -> None:
        raise NotImplementedError(
            "Abstract ``MPDClientBase`` does not implement ``noidle``"
        )

    def command_list_ok_begin(self) -> None:
        raise NotImplementedError(
            "Abstract ``MPDClientBase`` does not implement ``command_list_ok_begin``"
        )

    def command_list_end(self) -> None:
        raise NotImplementedError(
            "Abstract ``MPDClientBase`` does not implement ``command_list_end``"
        )

    def _reset(self) -> None:
        self.mpd_version: Optional[str] = None
        self._command_list: Optional[list[Any]] = None
        self._binary_stream: Optional[Any] = None

    def _begin_binary_stream(self, stream: Any) -> None:
        if getattr(self, "_iterating", False) or self._command_list is not None:
            raise IteratingError(
                "Cannot execute '{}' with command lists".format(stream._command)
            )
        previous = self._binary_stream
        if previous is not None and previous is not stream:
            previous.close()
        self._binary_stream = stream

    def _end_binary_stream(self, stream: Any) -> None:
        if self._binary_stream is stream:
            self._binary_stream = None

    def _close_binary_stream(self) -> None:
        stream = self._binary_stream
        if stream is not None:
            stream.close()

    def _parse_pair(self, line: str, separator: str = ": ") -> List[str]:
        pair = line.split(separator, 1)
        if len(pair) < 2:
            raise ProtocolError("Could not parse pair: '{}'".format(line))
        return pair

    def _parse_pairs(
        self, lines: Iterable[str], separator: str = ": "
    ) -> Iterator[List[str]]:
        for line in lines:
            yield self._parse_pair(line, separator)

    def _parse_objects(
        self,
        lines: Iterable[str],
        delimiters: List[str] = [],
        lookup_delimiter: bool = False,
    ) -> Iterator[Dict[str, str]]:
        obj: Dict[str, Any] = {}
        for key, value in self._parse_pairs(lines):
            key = key.lower()
            if lookup_delimiter and key not in delimiters:
                delimiters = delimiters + [key]
            if obj:
                if key in delimiters:
                    if lookup_delimiter:
                        if key in obj:
                            yield obj
                            obj = obj.copy()
                            while delimiters[-1] != key:
                                obj.pop(delimiters[-1], None)
                                delimiters.pop()
                    else:
                        yield obj
                        obj = {}
                elif key in obj:
                    if not isinstance(obj[key], list):
                        obj[key] = [obj[key], value]
                    else:
                        obj[key].append(value)
                    continue
            obj[key] = value
        if obj:
            yield obj

    # Use this instead of _parse_objects whenever the result is returned
    # immediately in a command implementation
    _parse_objects_direct = _parse_objects

    def _parse_raw_stickers(self, lines: Iterable[str]) -> Iterator[Tuple[str, str]]:
        for _, sticker in self._parse_pairs(lines):
            value = sticker.split("=", 1)
            if len(value) != 2:
                raise ProtocolError("Could not parse sticker: {}".format(repr(sticker)))
            yield value[0], value[1]

    NOOP = mpd_commands("close", "kill")(Noop())

    @mpd_commands("plchangesposid", is_direct=True)
    def _parse_changes(self, lines: List[str]) -> Iterator[Dict[str, str]]:
        return self._parse_objects_direct(lines, ["cpos"])

    @mpd_commands("listall", "listallinfo", "listfiles", "lsinfo", is_direct=True)
    def _parse_database(self, lines: List[str]) -> Iterator[Dict[str, str]]:
        return self._parse_objects_direct(lines, ["file", "directory", "playlist"])

    @mpd_commands("idle")
    def _parse_idle(self, lines: List[str]) -> Iterator[str]:
        return self._parse_list(lines)

    @mpd_commands("addid", "replay_gain_status", "rescan", "update")
    def _parse_item(self, lines: List[str]) -> Optional[str]:
        pairs = list(self._parse_pairs(lines))
        if len(pairs) != 1:
            return None
        return pairs[0][1]

    @mpd_commands(
        "channels", "commands", "listplaylist", "notcommands", "tagtypes", "urlhandlers"
    )
    def _parse_list(self, lines: List[str]) -> Iterator[str]:
        seen = None
        for key, value in self._parse_pairs(lines):
            if key != seen:
                if seen is not None:
                    raise ProtocolError("Expected key '{}', got '{}'".format(seen, key))
                seen = key
            yield value

    @mpd_commands("list", is_direct=True)
    def _parse_list_groups(self, lines: List[str]) -> Iterator[Dict[str, str]]:
        return self._parse_objects_direct(lines, lookup_delimiter=True)

    @mpd_commands("readmessages", is_direct=True)
    def _parse_messages(self, lines: List[str]) -> Iterator[Dict[str, str]]:
        return self._parse_objects_direct(lines, ["channel"])

    @mpd_commands("listmounts", is_direct=True)
    def _parse_mounts(self, lines: List[str]) -> Iterator[Dict[str, str]]:
        return self._parse_objects_direct(lines, ["mount"])

    @mpd_commands("listneighbors", is_direct=True)
    def _parse_neighbors(self, lines: List[str]) -> Iterator[Dict[str, str]]:
        return self._parse_objects_direct(lines, ["neighbor"])

    @mpd_commands("listpartitions", is_direct=True)
    def _parse_partitions(self, lines: List[str]) -> Iterator[Dict[str, str]]:
        return self._parse_objects_direct(lines, ["partition"])

    @mpd_commands(
        "add",
        "addtagid",
        "binarylimit",
        "clear",
        "clearerror",
        "cleartagid",
        "consume",
        "crossfade",
        "delete",
        "deleteid",
        "delpartition",
        "disableoutput",
        "enableoutput",
        "findadd",
        "load",
        "mixrampdb",
        "mixrampdelay",
        "mount",
        "move",
        "moveid",
        "moveoutput",
        "newpartition",
        "next",
        "outputvolume",
        "partition",
        "password",
        "pause",
        "ping",
        "play",
        "playid",
        "playlistadd",
        "playlistclear",
        "playlistdelete",
        "playlistmove",
        "previous",
        "prio",
        "prioid",
        "random",
        "rangeid",
        "rename",
        "repeat",
        "replay_gain_mode",
        "rm",
        "save",
        "searchadd",
        "searchaddpl",
        "seek",
        "seekcur",
        "seekid",
        "sendmessage",
        "setvol",
        "shuffle",
        "single",
        "sticker delete",
        "sticker set",
        "stop",
        "subscribe",
        "swap",
        "swapid",
        "toggleoutput",
        "unmount",
        "unsubscribe",
        "volume",
    )
    def _parse_nothing(self, lines: List[str]) -> None:
        for line in lines:
            raise ProtocolError(
                "Got unexpected return value: '{}'".format(", ".join(lines))
            )

    @mpd_commands("config", "count", "currentsong", "readcomments", "stats", "status")
    def _parse_object(self, lines: List[str]) -> Dict[str, str]:
        try:
            return next(self._parse_objects(lines))
        except StopIteration:
            return {}

    @mpd_commands("outputs", is_direct=True)
    def _parse_outputs(self, lines: List[str]) -> Iterator[Dict[str, str]]:
        return self._parse_objects_direct(lines, ["outputid"])

    @mpd_commands("playlist")
    def _parse_playlist(self, lines: List[str]) -> Iterator[str]:
        for key, value in self._parse_pairs(lines, ":"):
            yield value

    @mpd_commands("listplaylists", is_direct=True)
    def _parse_playlists(self, lines: List[str]) -> Iterator[Dict[str, str]]:
        return self._parse_objects_direct(lines, ["playlist"])

    @mpd_commands("decoders", is_direct=True)
    def _parse_plugins(self, lines: List[str]) -> Iterator[Dict[str, str]]:
        return self._parse_objects_direct(lines, ["plugin"])

    @mpd_commands(
        "find",
        "listplaylistinfo",
        "playlistfind",
        "playlistid",
        "playlistinfo",
        "playlistsearch",
        "plchanges",
        "search",
        "sticker find",
        is_direct=True,
    )
    def _parse_songs(self, lines: List[str]) -> Iterator[Dict[str, str]]:
        return self._parse_objects_direct(lines, ["file"])

    @mpd_commands("sticker get")
    def _parse_sticker(self, lines: List[str]) -> str:
        key, value = list(self._parse_raw_stickers(lines))[0]
        return value

    @mpd_commands("sticker list")
    def _parse_stickers(self, lines: List[str]) -> Dict[str, str]:
        return dict(self._parse_raw_stickers(lines))

    @mpd_commands("albumart", "readpicture", is_binary=True)
    def _parse_plain_binary(self, structure: Any) -> Any:
        return structure


def mpd_command_provider(cls: Type[MPDClientBase]) -> Type[MPDClientBase]:
    """Decorator hooking up registered MPD commands to concrete client
    implementation.

    A class using this decorator must inherit from ``MPDClientBase`` and
    implement it's ``add_command`` function.
    """

    def collect(
        cls: Any, callbacks: Dict[str, Tuple[Any, Any]] = {}
    ) -> Dict[str, Tuple[Any, Any]]:
        """Collect MPD command callbacks from given class.

        Searches class __dict__ on given class and all it's bases for functions
        which have been decorated with @mpd_commands and returns a dict
        containing callback name as keys and
        (callback, callback implementing class) tuples as values.
        """
        for name, ob in cls.__dict__.items():
            if hasattr(ob, "mpd_commands") and name not in callbacks:
                callbacks[name] = (ob, cls)
        for base in cls.__bases__:
            callbacks = collect(base, callbacks)
        return callbacks

    for name, value in collect(cls).items():
        callback, from_ = value
        for command in callback.mpd_commands:
            cls.add_command(command, callback)
    return cls


def _create_callback(
    self: Any, function: Callable[[Any, Any], Any], wrap_result: bool
) -> Optional[Callable[[], Any]]:
    """Create MPD command related response callback."""
    if not callable(function):
        return None

    def command_callback() -> Any:
        # command result callback expects response from MPD as iterable lines,
        # thus read available lines from socket
        res = function(self, self._read_lines())
        # wrap result in iterator helper if desired
        if wrap_result:
            res = self._wrap_iterator(res)
        return res

    return command_callback


def _create_command(
    wrapper: Callable, name: str, return_value: Any, wrap_result: bool
) -> Callable:
    """Create MPD command related function."""

    def mpd_command(self: Any, *args: Any) -> Any:
        callback = _create_callback(self, return_value, wrap_result)
        return wrapper(self, name, args, callback)

    return mpd_command


class _NotConnected:
    def __getattr__(self, attr: str) -> Callable:
        return self._dummy

    def _dummy(*args: Any) -> None:
        raise ConnectionError("Not connected")


class Delivered(tuple):
    """Half-open byte range ``(start, stop)``.

    It is a tuple (so unpacking and tuple comparisons work) and also compares
    equal to an equivalent :class:`range` or two-element list.
    """

    __slots__ = ()

    def __new__(cls, start: int, stop: int) -> "Delivered":
        return super().__new__(cls, (start, stop))

    @property
    def start(self) -> int:
        return self[0]

    @property
    def stop(self) -> int:
        return self[1]

    def __eq__(self, other: Any) -> bool:
        if isinstance(other, range):
            return (self.start, self.stop) == (other.start, other.stop)
        if isinstance(other, tuple):
            return tuple.__eq__(self, other)
        if isinstance(other, list) and len(other) == 2:
            return self.start == other[0] and self.stop == other[1]
        return NotImplemented

    def __ne__(self, other: Any) -> bool:
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    __hash__ = tuple.__hash__


class BinaryChunk(bytes):
    """A single piece of a binary response.

    The object is a :class:`bytes` instance and can be used anywhere a bytes
    object is expected.  In addition it carries the position at which the
    piece was delivered and the total length announced by MPD (if any).
    """
    def __new__(
        cls,
        data: bytes,
        offset: int,
        total: Optional[int],
        *,
        mime_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        complete: bool = False,
    ) -> "BinaryChunk":
        chunk = super().__new__(cls, data)
        chunk._offset = offset
        chunk._total = total
        chunk._mime_type = mime_type
        chunk._metadata = dict(metadata or {})
        chunk._complete = complete
        return chunk

    @property
    def data(self) -> bytes:
        """The delivered bytes."""
        return bytes(self)

    @property
    def binary(self) -> bytes:
        """Alias of :attr:`data`, matching the one-shot response key."""
        return bytes(self)

    @property
    def payload(self) -> bytes:
        """Alias of :attr:`data`."""
        return bytes(self)

    @property
    def offset(self) -> int:
        """Offset of the first delivered byte."""
        return self._offset

    start = offset

    @property
    def end(self) -> int:
        """Offset just after the last delivered byte."""
        return self._offset + len(self)

    stop = end

    @property
    def delivered(self) -> Delivered:
        """The half-open range covered by this chunk: ``(start, end)``."""
        return Delivered(self._offset, self.end)

    @property
    def delivered_range(self) -> range:
        """The delivered range as a :class:`range` object."""
        return range(self._offset, self.end)

    @property
    def range(self) -> range:
        """Alias of :attr:`delivered_range`."""
        return self.delivered_range

    @property
    def length(self) -> int:
        """Number of bytes in this chunk."""
        return len(self)

    @property
    def total(self) -> Optional[int]:
        """Declared total length of the binary payload, if MPD supplied it."""
        return self._total

    @property
    def declared_total(self) -> Optional[int]:
        """Alias of :attr:`total`."""
        return self._total

    @property
    def total_length(self) -> Optional[int]:
        """Alias of :attr:`total`."""
        return self._total

    @property
    def declared_length(self) -> Optional[int]:
        """Alias of :attr:`total`."""
        return self._total

    @property
    def size(self) -> Optional[int]:
        """Alias of :attr:`total` (the ``size`` metadata value)."""
        return self._total

    @property
    def remaining(self) -> Optional[int]:
        """Number of bytes still to be delivered after this chunk."""
        if self._total is None:
            return None
        return max(0, self._total - self.end)

    @property
    def complete(self) -> bool:
        """Whether the payload is complete at the end of this chunk."""
        if self._complete:
            return True
        return self._total is not None and self.end >= self._total

    @property
    def type(self) -> Optional[str]:
        """MIME type reported by MPD, if any."""
        return self._mime_type

    @property
    def mime_type(self) -> Optional[str]:
        """Alias of :attr:`type`."""
        return self._mime_type

    @property
    def metadata(self) -> Dict[str, Any]:
        """Metadata that accompanied this chunk (without the binary value)."""
        return dict(self._metadata)

    def as_dict(self) -> Dict[str, Any]:
        """Return a dictionary representation of the chunk."""
        return {
            "binary": self.data,
            "data": self.data,
            "offset": self.offset,
            "start": self.offset,
            "end": self.end,
            "delivered": self.delivered,
            "delivered_range": self.delivered_range,
            "total": self.total,
            "declared_total": self.total,
            "total_length": self.total,
            "declared_length": self.total,
            "size": self.total,
            "length": self.length,
            "remaining": self.remaining,
            "type": self.type,
            "mime_type": self.type,
            "complete": self.complete,
            "metadata": self.metadata,
        }

    def _field(self, key: str) -> Any:
        fields = {
            "binary": self.data,
            "data": self.data,
            "offset": self.offset,
            "start": self.start,
            "end": self.end,
            "stop": self.stop,
            "delivered": self.delivered,
            "delivered_range": self.delivered_range,
            "range": self.delivered_range,
            "total": self.total,
            "declared_total": self.declared_total,
            "total_length": self.total_length,
            "declared_length": self.declared_length,
            "size": self.size,
            "length": self.length,
            "remaining": self.remaining,
            "type": self.type,
            "mime_type": self.mime_type,
            "complete": self.complete,
            "metadata": self.metadata,
        }
        try:
            return fields[key]
        except KeyError:
            raise KeyError(key)

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            return self._field(key)
        return bytes.__getitem__(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self._field(key)
        except KeyError:
            return default

    def keys(self) -> Tuple[str, ...]:
        return (
            "binary",
            "data",
            "offset",
            "start",
            "end",
            "delivered",
            "delivered_range",
            "total",
            "declared_total",
            "size",
            "length",
            "remaining",
            "type",
            "mime_type",
            "complete",
        )

    def items(self) -> Iterable[Tuple[str, Any]]:
        for key in self.keys():
            yield key, self._field(key)

    def __contains__(self, key: Any) -> bool:
        if isinstance(key, str):
            return key in self.keys()
        return bytes.__contains__(self, key)

    def __repr__(self) -> str:
        return (
            "BinaryChunk(data={!r}, offset={}, total={!r}, complete={})".format(
                self.data, self.offset, self.total, self.complete
            )
        )


class BinaryStream:
    """Iterate over an ``albumart`` or ``readpicture`` response piece by piece.

    Each iteration yields a :class:`BinaryChunk` as soon as bytes arrive, even
    while a single server-side ``binary:`` response is still being read.  The
    payload length announced by MPD is always consumed exactly, so newlines and
    ``OK``-looking bytes inside the image are never mistaken for protocol
    boundaries.

    A consumer may stop iterating at any time.  :meth:`close` drains the
    remainder of the response in flight so the connection can be reused; if
    that boundary cannot be established, the connection is disconnected.
    """

    _DEFAULT_PIECE = 65536

    def __init__(
        self,
        client: Any,
        command: str,
        uri: str,
        chunk_size: Optional[int] = None,
    ) -> None:
        if chunk_size is not None and chunk_size <= 0:
            raise ValueError("chunk_size must be greater than zero")
        self._client = client
        self._command = command
        self._uri = uri
        self._chunk_size = chunk_size or self._DEFAULT_PIECE
        self._offset = 0
        self._total: Optional[int] = None
        self._metadata: Optional[Dict[str, Any]] = None
        self._payload_remaining = 0
        self._phase = "idle"
        self._closed = False
        self._abandoned = False
        self._boundary_unknown = False
        client._begin_binary_stream(self)

    @property
    def uri(self) -> str:
        return self._uri

    @property
    def offset(self) -> int:
        """Number of bytes yielded so far (the end of :attr:`delivered`)."""
        return self._offset

    @property
    def delivered(self) -> Delivered:
        """The range yielded so far: ``(0, offset)``."""
        return Delivered(0, self._offset)

    @property
    def delivered_range(self) -> range:
        """The range yielded so far as a :class:`range` object."""
        return range(0, self._offset)

    @property
    def delivered_length(self) -> int:
        """Number of bytes yielded so far."""
        return self._offset

    delivered_bytes = delivered_length

    @property
    def total(self) -> Optional[int]:
        """The total length declared by MPD, if it has been announced yet."""
        return self._total

    @property
    def declared_total(self) -> Optional[int]:
        """Alias of :attr:`total`."""
        return self._total

    total_length = declared_total
    declared_length = declared_total
    size = declared_total

    @property
    def remaining(self) -> Optional[int]:
        if self._total is None:
            return None
        return max(0, self._total - self._offset)

    @property
    def metadata(self) -> Dict[str, Any]:
        return dict(self._metadata or {})

    @property
    def type(self) -> Optional[str]:
        if self._metadata is None:
            return None
        value = self._metadata.get("type")
        return value if isinstance(value, str) else None

    @property
    def mime_type(self) -> Optional[str]:
        return self.type

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def has_picture(self) -> bool:
        """Whether MPD reported a picture at all (False for empty readpicture)."""
        return self._total is not None

    @property
    def complete(self) -> bool:
        return (
            self._closed
            and not self._abandoned
            and self._total is not None
            and self._offset >= self._total
        )

    def __iter__(self) -> "BinaryStream":
        return self

    def __next__(self) -> BinaryChunk:
        if self._closed:
            raise StopIteration
        try:
            while True:
                if self._phase == "idle":
                    if self._total is not None and self._offset >= self._total:
                        self._finish()
                        raise StopIteration
                    self._phase = "header"
                    self._request()
                elif self._phase == "header":
                    metadata, length = self._read_header()
                    if length is None:
                        if self._metadata is None and not metadata:
                            # readpicture: the song exists but has no picture.
                            self._finish()
                            raise StopIteration
                        raise CommandError(
                            "Binary field vanished during binary transfer"
                        )
                    self._apply_metadata(metadata, length)
                    self._payload_remaining = length
                    self._phase = "payload"
                    if length == 0:
                        offset = self._offset
                        self._finish_tail()
                        self._phase = "idle"
                        if self._total is None:
                            self._total = 0
                        self._finish()
                        return self._make_chunk(b"", offset, self._total)
                elif self._phase == "payload":
                    amount = min(self._chunk_size, self._payload_remaining)
                    data = self._client._read_chunk(amount)
                    if len(data) != amount:
                        raise ConnectionError(
                            "Connection lost while reading binary data: "
                            "expected %d bytes, got %d" % (amount, len(data))
                        )
                    start = self._offset
                    self._offset += amount
                    self._payload_remaining -= amount
                    if self._payload_remaining == 0:
                        self._phase = "tail"
                    return self._make_chunk(data, start, self._total)
                elif self._phase == "tail":
                    self._finish_tail()
                    self._phase = "idle"
                    if self._total is None:
                        # No size header: the one response is the whole image.
                        self._total = self._offset
                        self._finish()
                        raise StopIteration
        except (CommandError, CommandListError):
            if self._boundary_unknown:
                self._abandon()
                self._client.disconnect()
            else:
                # The full error line has been consumed; boundary is known.
                self._finish()
            raise
        except StopIteration:
            raise
        except BaseException:
            # Anything else may leave unread binary data in the stream.
            self._abandon()
            self._client.disconnect()
            raise

    def _request(self) -> None:
        self._client._write_command(
            self._command, [self._uri, self._offset]
        )

    def _read_header(self) -> Tuple[Dict[str, Any], Optional[int]]:
        metadata: Dict[str, Any] = {}
        while True:
            line = self._client._read_line()
            if line is None:
                return metadata, None
            key, value = self._client._parse_pair(line, ": ")
            if key == "binary":
                return metadata, int(value)
            metadata[key] = value

    def _apply_metadata(self, metadata: Dict[str, Any], length: int) -> None:
        if self._metadata is None:
            self._metadata = metadata
            value = metadata.get("size")
            if value is None:
                # Older MPD versions return the whole image in one response
                # without a size header; the announced payload is the total.
                self._total = self._offset + length
            else:
                try:
                    self._total = int(value)
                except (TypeError, ValueError):
                    self._boundary_unknown = True
                    raise CommandError("Size data unsuitable for binary transfer")
                if self._total < 0:
                    self._boundary_unknown = True
                    raise CommandError("Size data unsuitable for binary transfer")
        elif metadata != self._metadata:
            self._boundary_unknown = True
            raise CommandError("Metadata of binary data changed during transfer")
        if self._total is not None and self._offset + length > self._total:
            self._boundary_unknown = True
            raise CommandListError("Binary data announced size exceeded")
        if (
            self._total is not None
            and self._offset + length < self._total
            and length == 0
        ):
            self._boundary_unknown = True
            raise CommandError("Binary transfer made no progress")

    def _finish_tail(self) -> None:
        if self._client._read_chunk(1) != b"\n":
            raise ConnectionError("Connection lost while reading binary data")
        # Consume the terminating OK line (and any trailing metadata lines).
        while True:
            line = self._client._read_line()
            if line is None:
                return

    def _make_chunk(
        self, data: bytes, offset: int, total: Optional[int]
    ) -> BinaryChunk:
        metadata = dict(self._metadata or {})
        metadata.pop("binary", None)
        mime_type = metadata.get("type")
        if not isinstance(mime_type, str):
            mime_type = None
        return BinaryChunk(
            data,
            offset,
            total,
            mime_type=mime_type,
            metadata=metadata,
            complete=total is not None and offset + len(data) >= total,
        )

    def _drain(self) -> bool:
        """Consume the rest of the response in flight, if that is safe."""
        try:
            if self._phase == "header":
                metadata, length = self._read_header()
                if length is None:
                    self._phase = "idle"
                    return True
                if self._metadata is None:
                    self._metadata = metadata
                self._payload_remaining = length
                self._phase = "payload"
            if self._phase == "payload":
                while self._payload_remaining > 0:
                    amount = min(self._chunk_size, self._payload_remaining)
                    data = self._client._read_chunk(amount)
                    if len(data) != amount:
                        return False
                    self._payload_remaining -= amount
                self._phase = "tail"
            if self._phase == "tail":
                self._finish_tail()
                self._phase = "idle"
        except CommandError:
            # A complete ACK line ends the response; the boundary is known.
            self._phase = "idle"
            return True
        except BaseException:
            return False
        return True

    def _finish(self) -> None:
        self._closed = True
        self._abandoned = False
        self._client._end_binary_stream(self)

    def _abandon(self) -> None:
        """Mark the stream as unusable without claiming completeness."""
        self._closed = True
        self._abandoned = True
        self._client._end_binary_stream(self)

    def close(self) -> None:
        """Stop the stream and establish a reusable protocol boundary.

        A response that is still in flight is drained (by exact announced
        lengths).  If it cannot be drained, the connection is disconnected
        rather than leaving unknown bytes for the next command.
        """
        if self._closed:
            return
        drained = True
        if self._phase != "idle":
            drained = self._drain()
        if (
            drained
            and not self._abandoned
            and self._total is not None
            and self._offset >= self._total
        ):
            self._finish()
        else:
            # A partially read image never counts as complete.
            self._abandon()
        if not drained:
            self._client.disconnect()

    def __enter__(self) -> "BinaryStream":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def __del__(self) -> None:
        # Do not perform blocking I/O during finalization.  While the stream is
        # still registered with the client it is kept alive by that reference;
        # once replaced or explicitly closed, the boundary has been handled.
        try:
            if not self._closed:
                self._abandon()
        except Exception:
            pass


@mpd_command_provider
class MPDClient(MPDClientBase):
    idletimeout = None
    _timeout = None
    _wrap_iterator_parsers = [
        MPDClientBase._parse_list,
        MPDClientBase._parse_list_groups,
        MPDClientBase._parse_playlist,
        MPDClientBase._parse_changes,
        MPDClientBase._parse_songs,
        MPDClientBase._parse_mounts,
        MPDClientBase._parse_neighbors,
        MPDClientBase._parse_partitions,
        MPDClientBase._parse_playlists,
        MPDClientBase._parse_database,
        MPDClientBase._parse_messages,
        MPDClientBase._parse_outputs,
        MPDClientBase._parse_plugins,
    ]

    def __init__(self, use_unicode: Optional[bool] = None) -> None:
        if use_unicode is not None:
            warnings.warn(
                "use_unicode parameter to ``MPDClient`` constructor is deprecated",
                DeprecationWarning,
                stacklevel=2,
            )
        super().__init__()

    def _reset(self) -> None:
        stream = getattr(self, "_binary_stream", None)
        super()._reset()
        if stream is not None:
            stream._abandon()
        self._iterating = False
        self._sock: Optional[socket.socket] = None
        self._rbfile: Union[IO[bytes], _NotConnected] = _NotConnected()
        self._wfile: Union[IO[str], _NotConnected] = _NotConnected()

    def _end_binary_stream(self, stream: "BinaryStream") -> None:
        if self._binary_stream is stream:
            self._binary_stream = None

    def _close_binary_stream(self) -> None:
        stream = self._binary_stream
        if stream is not None:
            stream.close()

    def albumart_stream(
        self, uri: str, chunk_size: Optional[int] = None
    ) -> "BinaryStream":
        """Return album art for *uri* as a chunked iterator.

        Each iteration yields a :class:`BinaryChunk` carrying the delivered
        byte range and MPD's declared total length.  Pieces are delivered while
        a server response is still being read, so a large image can be consumed
        without waiting for the complete picture.
        """
        return BinaryStream(self, "albumart", uri, chunk_size)

    def readpicture_stream(
        self, uri: str, chunk_size: Optional[int] = None
    ) -> "BinaryStream":
        """Return the embedded picture for *uri* as a chunked iterator."""
        return BinaryStream(self, "readpicture", uri, chunk_size)

    # Convenience aliases for the chunked entry points.
    def binary_stream(
        self, command: str, uri: str, chunk_size: Optional[int] = None
    ) -> "BinaryStream":
        """Chunked iterator for a binary command (``albumart``/``readpicture``)."""
        return BinaryStream(self, command, uri, chunk_size)

    albumart_chunks = albumart_stream
    albumart_iter = albumart_stream
    iter_albumart = albumart_stream
    stream_albumart = albumart_stream
    readpicture_chunks = readpicture_stream
    readpicture_iter = readpicture_stream
    iter_readpicture = readpicture_stream
    stream_readpicture = readpicture_stream

    def _execute(self, command: str, args: List[Any], retval: Any) -> Any:
        # Yield points of a binary stream are response boundaries, so an
        # abandoned stream can simply be closed before the next command.
        self._close_binary_stream()
        if self._iterating:
            raise IteratingError("Cannot execute '{}' while iterating".format(command))
        if self._command_list is not None:
            if not callable(retval):
                raise CommandListError(
                    "'{}' not allowed in command list".format(command)
                )
            self._write_command(command, args)
            self._command_list.append(retval)
        else:
            self._write_command(command, args)
            if callable(retval):
                return retval()
            return retval

    def _write_line(self, line: str) -> None:
        try:
            if self._wfile is _NotConnected:
                raise ConnectionError("Not connected")
            self._wfile.write("{}\n".format(line))
            self._wfile.flush()
        except socket.error:
            error_message = "Connection to server was reset"
            logger.info(error_message)
            self._reset()
            e = ConnectionError(error_message)
            raise e.with_traceback(sys.exc_info()[2])

    def _write_command(self, command: str, args: List[Any] = []) -> None:
        parts = [command]
        for arg in args:
            if type(arg) is tuple:
                if len(arg) == 0:
                    parts.append('":"')
                elif len(arg) == 1:
                    parts.append('"{}:"'.format(int(arg[0])))
                else:
                    parts.append('"{}:{}"'.format(int(arg[0]), int(arg[1])))
            else:
                parts.append('"{}"'.format(escape(str(arg))))
        # Minimize logging cost if the logging is not activated.
        if logger.isEnabledFor(logging.DEBUG):
            if command == "password":
                logger.debug("Calling MPD password(******)")
            else:
                logger.debug("Calling MPD %s%r", command, args)
        cmd = " ".join(parts)
        self._write_line(cmd)

    def _read_line(self) -> Optional[str]:
        line = self._rbfile.readline().decode("utf-8")
        if not line.endswith("\n"):
            self.disconnect()
            raise ConnectionError("Connection lost while reading line")
        line = line.rstrip("\n")
        if line.startswith(ERROR_PREFIX):
            error = line[len(ERROR_PREFIX) :].strip()
            raise CommandError(error)
        if self._command_list is not None:
            if line == NEXT:
                return None
            if line == SUCCESS:
                raise ProtocolError("Got unexpected '{}'".format(SUCCESS))
        elif line == SUCCESS:
            return None
        return line

    def _read_lines(self) -> Iterator[str]:
        line = self._read_line()
        while line is not None:
            yield line
            line = self._read_line()

    def _read_chunk(self, amount: int) -> bytes:
        chunk = bytearray()
        while amount > 0:
            result = self._rbfile.read(amount)
            if len(result) == 0:
                break
            chunk.extend(result)
            amount -= len(result)
        return bytes(chunk)

    def _read_binary(self) -> Dict[str, Union[str, bytes]]:
        """From the data stream, read Unicode lines until one says "binary:
        <number>\\n"; at that point, read binary data of the given length.

        This behaves like _parse_objects (with empty set of delimiters; even
        returning only a single result), but rather than feeding from a lines
        iterable (which would be preprocessed too far), it reads directly off
        the stream."""

        obj = {}

        while True:
            line = self._read_line()
            if line is None:
                break

            value: Union[str, bytes]
            key, value = self._parse_pair(line, ": ")

            if key == "binary":
                chunk_size = int(value)
                value = self._read_chunk(chunk_size)

                if len(value) != chunk_size:
                    self.disconnect()
                    raise ConnectionError(
                        "Connection lost while reading binary data: "
                        "expected %d bytes, got %d" % (chunk_size, len(value))
                    )

                if self._rbfile.read(1) != b"\n":
                    # newline after binary content
                    self.disconnect()
                    raise ConnectionError("Connection lost while reading line")

            obj[key] = value
        return obj

    def _execute_binary(
        self, command: str, args: List[Any]
    ) -> Dict[str, Union[str, bytes]]:
        """Execute a binary command and collect all chunks into one response.

        The chunked :class:`BinaryStream` is used underneath; this keeps the
        historic one-shot behavior (a ``binary`` entry holding the complete
        image) while sharing the protocol handling with the streaming API."""
        if self._iterating or self._command_list is not None:
            raise IteratingError(
                "Cannot execute '{}' with command lists".format(command)
            )
        args = list(args)
        assert len(args) == 1
        stream = BinaryStream(self, command, args[0])
        data: Optional[bytes] = None
        try:
            for chunk in stream:
                if data is None:
                    data = chunk.data
                else:
                    data += chunk.data
        finally:
            stream.close()
        metadata = stream.metadata
        metadata.pop("size", None)
        if data is not None:
            metadata["binary"] = data
        return metadata

    def _read_command_list(self) -> Iterator[Dict[str, str]]:
        try:
            if self._command_list is None:
                raise CommandListError("Not in command list")
            for retval in self._command_list:
                yield retval()
        finally:
            self._command_list = None
        self._parse_nothing(self._read_lines())

    def _iterator_wrapper(
        self, iterator: Iterator[Dict[str, str]]
    ) -> Iterator[Dict[str, str]]:
        try:
            for item in iterator:
                yield item
        finally:
            self._iterating = False

    def _wrap_iterator(
        self, iterator: Iterator[Dict[str, str]]
    ) -> Iterator[Union[Dict[str, str], List[Dict[str, str]]]]:
        if not self.iterate:
            return list(iterator)
        self._iterating = True
        return self._iterator_wrapper(iterator)

    def _hello(self, line: str) -> None:
        if not line.endswith("\n"):
            self.disconnect()
            raise ConnectionError("Connection lost while reading MPD hello")
        line = line.rstrip("\n")
        if not line.startswith(HELLO_PREFIX):
            raise ProtocolError("Got invalid MPD hello: '{}'".format(line))
        self.mpd_version = line[len(HELLO_PREFIX) :].strip()

    def _connect_unix(self, path: str) -> socket.socket:
        if not hasattr(socket, "AF_UNIX"):
            raise ConnectionError("Unix domain sockets not supported on this platform")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(path)
        return sock

    def _connect_tcp(self, host: str, port: int) -> socket.socket:
        err = None
        for res in socket.getaddrinfo(
            host,
            port,
            socket.AF_UNSPEC,
            socket.SOCK_STREAM,
            socket.IPPROTO_TCP,
            socket.AI_ADDRCONFIG,
        ):
            af, socktype, proto, canonname, sa = res
            sock = None
            try:
                sock = socket.socket(af, socktype, proto)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                sock.settimeout(self.timeout)
                sock.connect(sa)
                return sock
            except socket.error as e:
                err = e
                if sock is not None:
                    sock.close()
        if err is not None:
            raise err
        else:
            raise ConnectionError("getaddrinfo returns an empty list")

    @mpd_commands("idle")
    def _parse_idle(
        self, lines: List[str]
    ) -> Iterator[Union[Dict[str, str], List[Dict[str, str]]]]:
        if self._sock is not None:
            self._sock.settimeout(self.idletimeout)
        ret = self._wrap_iterator(self._parse_list(lines))
        if self._sock is not None:
            self._sock.settimeout(self._timeout)
        return ret

    @property
    def timeout(self) -> Optional[float]:
        return self._timeout

    @timeout.setter
    def timeout(self, timeout: Optional[float]) -> None:
        self._timeout = timeout
        if self._sock is not None:
            self._sock.settimeout(timeout)

    def connect(
        self, host: str, port: Optional[int] = None, timeout: Optional[float] = None
    ) -> None:
        logger.info("Calling MPD connect(%r, %r, timeout=%r)", host, port, timeout)
        if self._sock is not None:
            raise ConnectionError("Already connected")
        if timeout is not None:
            warnings.warn(
                "The timeout parameter in connect() is deprecated! "
                "Use MPDClient.timeout = yourtimeout instead.",
                DeprecationWarning,
            )
            self.timeout = timeout
        if host.startswith("@"):
            host = "\0" + host[1:]
        if host.startswith(("/", "\0")):
            self._sock = self._connect_unix(host)
        else:
            if port is None:
                raise ValueError(
                    "port argument must be specified when connecting via tcp"
                )
            self._sock = self._connect_tcp(host, port)

        # - Force UTF-8 encoding, since this is dependant from the LC_CTYPE
        #   locale.
        # - by setting newline explicit, we force to send '\n' also on
        #   windows
        self._rbfile = self._sock.makefile("rb", newline="\n")
        self._wfile = self._sock.makefile("w", encoding="utf-8", newline="\n")

        try:
            helloline = self._rbfile.readline().decode("utf-8")
            self._hello(helloline)
        except Exception:
            self.disconnect()
            raise

    def disconnect(self) -> None:
        logger.info("Calling MPD disconnect()")
        if self._rbfile is not None and not isinstance(self._rbfile, _NotConnected):
            self._rbfile.close()
        if self._wfile is not None and not isinstance(self._wfile, _NotConnected):
            self._wfile.close()
        if self._sock is not None:
            self._sock.close()
        self._reset()

    def fileno(self) -> int:
        if self._sock is None:
            raise ConnectionError("Not connected")
        return self._sock.fileno()

    def command_list_ok_begin(self) -> None:
        if self._command_list is not None:
            raise CommandListError("Already in command list")
        if self._iterating:
            raise IteratingError("Cannot begin command list while iterating")
        self._write_command("command_list_ok_begin")
        self._command_list = []

    def command_list_end(self) -> Any:
        if self._command_list is None:
            raise CommandListError("Not in command list")
        if self._iterating:
            raise IteratingError("Already iterating over a command list")
        self._write_command("command_list_end")
        return self._wrap_iterator(self._read_command_list())

    @classmethod
    def add_command(cls, name: str, callback: Any) -> None:
        wrap_result = callback in cls._wrap_iterator_parsers
        if callback.mpd_commands_binary:

            def method(self, *args):
                return callback(self, cls._execute_binary(self, name, args))
        else:
            method = _create_command(cls._execute, name, callback, wrap_result)
        # create new mpd commands as function:
        escaped_name = name.replace(" ", "_")
        setattr(cls, escaped_name, method)

    @classmethod
    def remove_command(cls, name: str) -> None:
        if not hasattr(cls, name):
            raise ValueError("Can't remove not existent '{}' command".format(name))
        name = name.replace(" ", "_")
        delattr(cls, str(name))


# vim: set expandtab shiftwidth=4 softtabstop=4 textwidth=79:
