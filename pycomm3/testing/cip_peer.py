"""An in-memory, scenario-driven CIP peer for pycomm3 regression tests.

The peer is intentionally *not* a byte-script recorder.  Responses are
generated from the identity and contents of each request:

* the encapsulation command, session handle and sender context are checked;
* the CIP connection ID used for ``SendUnitData`` is checked against the
  connection opened by the last successful Forward Open in the same session;
* tag reads are matched by their decoded request path (instance ID or symbolic
  name) and element count, not by arrival order;
* the configured expected steps may declare independent reads, which are
  allowed to complete in any order without consuming each other's steps;
* a late packet from a closed session can never satisfy a request made in a
  new connection, even if the old and new requests use the same sequence
  number.

Scenarios
=========

A scenario is a sequence of :class:`Step` objects.  Each step has an
``expect`` describing the request and a ``reply`` describing the response::

    Scenario([
        RegisterSession(),
        ForwardOpen(reply=Reply.forward_open(connection_id=1)),
        Read(tag="MyTag", elements=2,
             reply=Reply.read(dint(1, 2))),
        ForwardClose(),   # also expands to UnRegisterSession
    ])

The built-in step helpers cover session registration/unregistration,
identity discovery, forward open/close, reads, fragmented reads, writes and
generic messages.  For uncommon services use :class:`Generic` or
:class:`Unconnected` with a :class:`Expect` that describes the service code,
class, instance, attribute and request data.

Matching rules
--------------

Every incoming request is normalized into a :class:`Request`.  The peer scans
the *available* steps for one whose expectation matches the request's decoded
service, path, tag, element count and offset.

A step is available if it is the next un-consumed step, or if it is an
independent read and every earlier non-independent step has been consumed.
``independent=True`` on a read means the read may be sent at any point after
the connection is open, and several independent reads may be combined in a
single MultipleServiceRequest in any order.

Requests are matched by identity, never by position.  If two reads of the same
tag are declared, declaration order is the tiebreaker.  A request whose
identity does not match any available step is a scenario mismatch.

Multi-service packets
---------------------

The driver groups adjacent reads into a MultipleServiceRequest.  The peer
maps each embedded request to a distinct declared read step: in declaration
order when the reads are not independent, or by identity in any order when
they are.  Each embedded reply is built from the step matched to *that*
embedded request, so recovered tags keep their type and original request
relationship.

Fragment replies
----------------

``Reply.fragmented(value, count=...)`` slices the encoded value using the byte
offset carried in the matched ReadTagFragmented request.  More fragments are
indicated by ``more=True`` (service status ``0x06``); the final fragment must
set ``more=False``.  The peer does not advance offsets on its own.

Mismatch diagnostics
--------------------

If no step matches, the peer raises :class:`ScenarioMismatch` and refuses all
further frames.  The exception exposes:

* ``first_divergence`` - a description of the first unexpected request;
* ``consumed`` / ``consumed_indexes`` - steps already consumed and their
  original scenario indexes;
* ``attempted`` - the decoded offending :class:`Request`;
* ``remaining`` / ``remaining_indexes`` - steps that were still expected.

``peer.assert_complete()`` verifies every declared step was consumed and
re-raises a recorded mismatch.  ``peer.assert_no_mismatch()`` re-raises only
the mismatch; this is useful after the driver has wrapped the transport error
in ``CommError``.

Stale sessions and connections
------------------------------

Session handles are allocated by the peer and tracked as alive/dead.  Each
registration receives a new handle.  When a session is unregistered or
explicitly closed with :meth:`CIPPeer.close_session`, its handle is marked
dead.  Any later packet with that handle is answered with encapsulation status
``0x64`` (invalid session handle) and recorded in :attr:`CIPPeer.rejections`.
It does not consume a scenario step.

Only the active session may satisfy a CIP request.  A ``SendUnitData`` frame
using the previous connection ID (or any session other than the active one)
is answered with a CIP error and also recorded as a rejection.  A late packet
from an old connection therefore cannot complete a request in a new
connection, even when the sequence numbers are identical.

Sockets
--------

:class:`CIPSocket` is a drop-in replacement for :class:`pycomm3.socket_.Socket`.
It does not use the network: ``connect()`` records the destination and
``send()``/``receive()`` pass frames through the peer.  ``peer.socket()``
returns one of these objects.  Tests can simply do::

    peer = CIPPeer(Scenario([...]))
    driver = CIPDriver("192.168.1.10")
    driver._sock = peer.socket()
    driver.open()

"""

from __future__ import annotations

import ipaddress
import struct
import threading
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from ..cip.data_types import UDINT, UINT, USINT
from ..exceptions import CommError

__all__ = [
    "CIPPeer",
    "CIPSocket",
    "ScenarioMismatch",
    "Scenario",
    "Step",
    "Expect",
    "RegisterSession",
    "ListIdentity",
    "ForwardOpen",
    "ForwardClose",
    "UnRegisterSession",
    "Disconnect",
    "Read",
    "ReadFragmented",
    "Write",
    "Generic",
    "Unconnected",
    "Reply",
    "dint",
    "sint",
    "int16",
    "udint",
    "uint16",
    "real",
    "string",
    "decode_path",
]

HEADER_SIZE = 24

# Encapsulation commands
NOP = 0x0000
LIST_TARGETS = 0x0001
LIST_SERVICES = 0x0004
LIST_IDENTITY = 0x0063
LIST_INTERFACES = 0x0064
REGISTER_SESSION = 0x0065
UNREGISTER_SESSION = 0x0066
SEND_RR_DATA = 0x006F
SEND_UNIT_DATA = 0x0070

COMMAND_NAMES = {
    NOP: "NOP",
    LIST_TARGETS: "ListTargets",
    LIST_SERVICES: "ListServices",
    LIST_IDENTITY: "ListIdentity",
    LIST_INTERFACES: "ListInterfaces",
    REGISTER_SESSION: "RegisterSession",
    UNREGISTER_SESSION: "UnRegisterSession",
    SEND_RR_DATA: "SendRRData",
    SEND_UNIT_DATA: "SendUnitData",
}

# CIP services
SERVICE_GET_ATTRIBUTES_ALL = 0x01
SERVICE_GET_ATTRIBUTE_LIST = 0x03
SERVICE_READ_TAG = 0x4C
SERVICE_READ_TAG_FRAGMENTED = 0x52
SERVICE_WRITE_TAG = 0x4D
SERVICE_WRITE_TAG_FRAGMENTED = 0x53
SERVICE_MULTI = 0x0A
SERVICE_FORWARD_OPEN = 0x54
SERVICE_LARGE_FORWARD_OPEN = 0x5B
SERVICE_FORWARD_CLOSE = 0x4E
SERVICE_UNCONNECTED_SEND = 0x52

SERVICE_NAMES = {
    SERVICE_GET_ATTRIBUTES_ALL: "GetAttributesAll",
    SERVICE_GET_ATTRIBUTE_LIST: "GetAttributeList",
    SERVICE_READ_TAG: "ReadTag",
    SERVICE_READ_TAG_FRAGMENTED: "ReadTagFragmented",
    SERVICE_WRITE_TAG: "WriteTag",
    SERVICE_WRITE_TAG_FRAGMENTED: "WriteTagFragmented",
    SERVICE_MULTI: "MultipleServiceRequest",
    SERVICE_FORWARD_OPEN: "ForwardOpen",
    SERVICE_LARGE_FORWARD_OPEN: "LargeForwardOpen",
    SERVICE_FORWARD_CLOSE: "ForwardClose",
    SERVICE_UNCONNECTED_SEND: "UnconnectedSend",
}

STATUS_SUCCESS = 0x00
STATUS_INSUFFICIENT_PACKETS = 0x06
STATUS_INVALID_SESSION = 0x0064
STATUS_INVALID_LENGTH = 0x0065

# Data type codes
TYPE_BOOL = 0xC1
TYPE_SINT = 0xC2
TYPE_INT = 0xC3
TYPE_DINT = 0xC4
TYPE_LINT = 0xC5
TYPE_USINT = 0xC6
TYPE_UINT = 0xC7
TYPE_UDINT = 0xC8
TYPE_ULINT = 0xC9
TYPE_REAL = 0xCA
TYPE_LREAL = 0xCB
TYPE_STRING = 0xD0
TYPE_DWORD = 0xD3

TYPE_NAMES = {
    TYPE_BOOL: "BOOL",
    TYPE_SINT: "SINT",
    TYPE_INT: "INT",
    TYPE_DINT: "DINT",
    TYPE_LINT: "LINT",
    TYPE_USINT: "USINT",
    TYPE_UINT: "UINT",
    TYPE_UDINT: "UDINT",
    TYPE_ULINT: "ULINT",
    TYPE_REAL: "REAL",
    TYPE_LREAL: "LREAL",
    TYPE_STRING: "STRING",
    TYPE_DWORD: "DWORD",
}

TYPE_SIZES = {
    TYPE_BOOL: 1,
    TYPE_SINT: 1,
    TYPE_INT: 2,
    TYPE_DINT: 4,
    TYPE_LINT: 8,
    TYPE_USINT: 1,
    TYPE_UINT: 2,
    TYPE_UDINT: 4,
    TYPE_ULINT: 8,
    TYPE_REAL: 4,
    TYPE_LREAL: 8,
    TYPE_DWORD: 4,
}

TYPE_FIELDS = {
    TYPE_SINT: "<b",
    TYPE_INT: "<h",
    TYPE_DINT: "<i",
    TYPE_LINT: "<q",
    TYPE_USINT: "<B",
    TYPE_UINT: "<H",
    TYPE_UDINT: "<I",
    TYPE_ULINT: "<Q",
    TYPE_REAL: "<f",
    TYPE_LREAL: "<d",
    TYPE_DWORD: "<I",
}

# Connection Manager class
CLASS_CONNECTION_MANAGER = 0x06


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _hex(data: Optional[bytes]) -> str:
    return "" if data is None else data.hex(" ")


# ---------------------------------------------------------------------------
# Request path decoding
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    """One decoded logical or symbolic path segment."""

    kind: str
    value: Union[int, str, bytes]

    def __str__(self) -> str:
        if isinstance(self.value, bytes):
            return f"{self.kind}({self.value!r})"
        return f"{self.kind}({self.value!r})"


@dataclass(frozen=True)
class CIPPath:
    """A decoded CIP request path.

    ``logical`` holds ``(kind, value)`` pairs for logical segments, ``symbol``
    is set when the path is a symbolic data segment, and ``raw`` is the
    original bytes (with the leading length byte included).
    """

    segments: Tuple[Segment, ...]
    raw: bytes
    truncated: bool = False

    @property
    def logical(self) -> List[Tuple[str, int]]:
        return [
            (s.kind, int(s.value))
            for s in self.segments
            if s.kind != "symbol" and isinstance(s.value, int)
        ]

    @property
    def symbol(self) -> Optional[str]:
        for segment in self.segments:
            if segment.kind == "symbol":
                return str(segment.value)
        return None

    def get(self, kind: str, default: Optional[int] = None) -> Optional[int]:
        for seg_kind, value in self.logical:
            if seg_kind == kind:
                return value
        return default

    @property
    def class_id(self) -> Optional[int]:
        return self.get("class")

    @property
    def instance_id(self) -> Optional[int]:
        return self.get("instance")

    @property
    def attribute_id(self) -> Optional[int]:
        return self.get("attribute")

    def describe(self) -> str:
        if not self.segments:
            return "<empty>"
        return "/".join(str(s) for s in self.segments)


def _logical_type(segment_type: int) -> str:
    return {
        0b000: "class",
        0b001: "instance",
        0b010: "member",
        0b011: "connection_point",
        0b100: "attribute",
        0b101: "special",
        0b110: "service",
    }.get((segment_type >> 2) & 0b111, f"logical_{segment_type:02x}")


def _logical_size(fmt: int, kind: str) -> int:
    # 0 -> 8-bit, 1 -> 16-bit, 3 -> 32-bit
    if fmt == 0:
        return 1
    if fmt == 1:
        return 2
    if fmt == 3:
        return 4
    raise ValueError(f"invalid logical format {fmt} for {kind}")


def decode_path(data: bytes, with_length: bool = True, padded: bool = True) -> CIPPath:
    """Decode a CIP request path into logical/symbolic segments.

    ``data`` may include the leading length byte (as produced by
    ``PADDED_EPATH.encode(..., length=True)``) or be the raw segment bytes.
    ``truncated`` is set if the declared path length is longer than the
    available data.

    pycomm3's ``PADDED_EPATH`` inserts the pad byte *after* a logical segment
    header when the segment size is even (e.g. ``25 00 44 46`` for a 16-bit
    instance ID).  That is what real devices receive from this library, so the
    default ``padded=True`` follows the same layout.
    """

    raw = bytes(data)
    if with_length:
        if not raw:
            return CIPPath((), b"", False)
        declared_words = raw[0]
        body_len = declared_words * 2
        body = raw[1 : 1 + body_len]
        truncated = len(raw) - 1 < body_len
        raw = raw[: 1 + min(body_len, len(raw) - 1)]
    else:
        body = raw
        truncated = False

    segments: List[Segment] = []
    offset = 0
    while offset < len(body):
        segment_type = body[offset]
        offset += 1
        upper = segment_type & 0b1110_0000
        if upper == 0b0010_0000:  # logical
            kind = _logical_type(segment_type)
            fmt = segment_type & 0b11
            try:
                size = _logical_size(fmt, kind)
            except ValueError:
                segments.append(Segment("unknown", segment_type))
                break
            if offset + size > len(body):
                truncated = True
                break
            if padded and (1 + size) % 2:
                offset += 1  # pad between the segment header and its value
            if offset + size > len(body):
                truncated = True
                break
            value = int.from_bytes(body[offset : offset + size], "little")
            offset += size
            segments.append(Segment(kind, value))
        elif upper == 0b1000_0000:  # data segment
            if segment_type & 0x1F == 0x11:
                # extended symbolic segment: 1-byte length, then ASCII
                if offset >= len(body):
                    truncated = True
                    break
                length = body[offset]
                offset += 1
                text = body[offset : offset + length]
                offset += length
                if length % 2:
                    offset += 1  # word padding
                segments.append(Segment("symbol", text.rstrip(b"\x00").decode("latin-1")))
            else:
                if offset >= len(body):
                    truncated = True
                    break
                length = body[offset]
                offset += 1
                segments.append(Segment("data", body[offset : offset + length]))
                offset += length
                if length % 2:
                    offset += 1
        elif upper == 0b0010_0000:
            segments.append(Segment("logical", segment_type))
        else:
            segments.append(Segment("unknown", segment_type))
            break

    return CIPPath(tuple(segments), raw, truncated)


# ---------------------------------------------------------------------------
# Elementary values and payload helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Value:
    """An encoded elementary value (or array of values)."""

    type_code: int
    values: Tuple[Any, ...]
    element_count: int = 0

    @property
    def type_name(self) -> str:
        return TYPE_NAMES.get(self.type_code, f"0x{self.type_code:02x}")

    @property
    def size(self) -> int:
        return TYPE_SIZES.get(self.type_code, 0)

    def encode(self) -> bytes:
        return _encode_value(self.type_code, list(self.values))

    def __repr__(self) -> str:
        return f"{self.type_name}{list(self.values)!r}"

    def __bytes__(self) -> bytes:
        return self.encode()


def _encode_value(type_code: int, values: Sequence[Any]) -> bytes:
    if type_code == TYPE_BOOL:
        return b"\xff" if values[0] else b"\x00"
    if type_code == TYPE_STRING:
        text = str(values[0]).encode("latin-1")
        return UINT.encode(len(text)) + text
    fmt = TYPE_FIELDS[type_code]
    return b"".join(struct.pack(fmt, value) for value in values)



def dint(*values: int) -> Value:
    return Value(TYPE_DINT, tuple(values))


def sint(*values: int) -> Value:
    return Value(TYPE_SINT, tuple(values))


def int16(*values: int) -> Value:
    return Value(TYPE_INT, tuple(values))


def udint(*values: int) -> Value:
    return Value(TYPE_UDINT, tuple(values))


def uint16(*values: int) -> Value:
    return Value(TYPE_UINT, tuple(values))


def real(*values: float) -> Value:
    return Value(TYPE_REAL, tuple(values))


def string(value: str) -> Value:
    return Value(TYPE_STRING, (value,))


# ---------------------------------------------------------------------------
# Reply descriptions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Reply:
    """A response template.

    ``kind`` selects how the peer builds the response.  ``payload`` is the raw
    CIP response data for ``kind="raw"``; for structured replies it is a
    :class:`Value`, bytes, or a list of embedded service responses.
    """

    kind: str
    payload: Any = None
    service_status: int = STATUS_SUCCESS
    extended_status: Tuple[int, ...] = ()
    extra: Dict[str, Any] = field(default_factory=dict)

    # -- convenience constructors -------------------------------------------

    @classmethod
    def success(cls, data: bytes = b"", **extra) -> "Reply":
        return cls("raw", data, STATUS_SUCCESS, (), dict(extra))

    @classmethod
    def error(
        cls, service_status: int, extended_status: Sequence[int] = (), data: bytes = b""
    ) -> "Reply":
        return cls("raw", data, service_status, tuple(extended_status), {})

    @classmethod
    def register_session(cls, session: int) -> "Reply":
        return cls("register_session", session)

    @classmethod
    def list_identity(
        cls,
        vendor: int = 1,
        product_type: int = 14,
        product_code: int = 89,
        revision: Tuple[int, int] = (20, 11),
        status: bytes = b"\x00\x00",
        serial: int = 0xC00FA09B,
        product_name: str = "1756-L61",
        state: int = 3,
        ip_address: str = "192.168.1.10",
        encap_protocol_version: int = 1,
    ) -> "Reply":
        return cls(
            "list_identity",
            {
                "vendor": vendor,
                "product_type": product_type,
                "product_code": product_code,
                "revision": revision,
                "status": status,
                "serial": serial,
                "product_name": product_name,
                "state": state,
                "ip_address": ip_address,
                "encap_protocol_version": encap_protocol_version,
            },
        )

    @classmethod
    def identity(
        cls,
        vendor: int = 1,
        product_type: int = 14,
        product_code: int = 89,
        revision: Tuple[int, int] = (20, 11),
        status: bytes = b"\x00\x00",
        serial: int = 0xC00FA09B,
        product_name: str = "1756-L61",
    ) -> "Reply":
        return cls(
            "identity",
            {
                "vendor": vendor,
                "product_type": product_type,
                "product_code": product_code,
                "revision": revision,
                "status": status,
                "serial": serial,
                "product_name": product_name,
            },
        )

    @classmethod
    def forward_open(cls, connection_id: int = 1, **extra) -> "Reply":
        return cls("forward_open", connection_id, extra=dict(extra))

    @classmethod
    def forward_close(cls) -> "Reply":
        return cls("forward_close")

    @classmethod
    def read(cls, value: Value, service_status: int = STATUS_SUCCESS) -> "Reply":
        return cls("read", value, service_status)

    @classmethod
    def fragmented(
        cls,
        value: Value,
        count: Optional[int] = None,
        more: bool = True,
        service_status: int = STATUS_SUCCESS,
    ) -> "Reply":
        """One fragment of ``value``.

        ``count`` is the number of elements in this fragment (``None`` for all
        remaining elements).  The slice is taken at the byte offset carried by
        the matched ``ReadTagFragmented`` request, matching the driver's
        offset accounting.
        """

        return cls(
            "fragmented",
            value,
            service_status,
            (),
            {"count": count, "more": more},
        )

    @classmethod
    def write(cls, value: Optional[Value] = None) -> "Reply":
        return cls("write", value)

    @classmethod
    def raw_service(
        cls,
        service: int,
        data: bytes = b"",
        service_status: int = STATUS_SUCCESS,
        extended_status: Sequence[int] = (),
    ) -> "Reply":
        return cls("raw_service", (service, data), service_status, tuple(extended_status))

    @classmethod
    def multi(cls, replies: Sequence["Reply"]) -> "Reply":
        return cls("multi", list(replies))

    @classmethod
    def disconnect(cls) -> "Reply":
        """Close the peer connection immediately without sending a reply."""

        return cls("disconnect")


# ---------------------------------------------------------------------------
# Expected requests
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Expect:
    """Match criteria for one request.

    ``command`` is the encapsulation command name (``"SendUnitData"`` etc.),
    ``service`` the CIP service code/name, and ``path``/``class_id``/
    ``instance``/``attribute`` constrain the decoded request path.  ``tag``
    matches symbolic tag reads, ``elements`` checks the requested element
    count, and ``request_data`` checks the raw request bytes.  ``session`` and
    ``connection_id`` can be used for extra assertions.  ``any_of`` allows a
    set of alternative expectations for a single step.
    """

    command: Optional[Union[int, str]] = None
    service: Optional[Union[int, str]] = None
    path: Optional[Union[str, bytes, CIPPath]] = None
    class_id: Optional[int] = None
    instance: Optional[int] = None
    attribute: Optional[int] = None
    tag: Optional[str] = None
    elements: Optional[int] = None
    offset: Optional[int] = None
    request_data: Optional[bytes] = None
    payload: Optional[bytes] = None
    session: Optional[int] = None
    connection_id: Optional[int] = None
    any_of: Tuple["Expect", ...] = ()
    name: Optional[str] = None

    def label(self) -> str:
        if self.name:
            return self.name
        bits: List[str] = []
        if self.command is not None:
            bits.append(_command_name(self.command))
        if isinstance(self.path, CIPPath):
            bits.append(self.path.describe())
        elif self.path is not None:
            bits.append(str(self.path))
        if self.tag is not None:
            bits.append(f"tag={self.tag!r}")
        if self.service is not None:
            bits.append(f"service={_service_name(self.service)}")
        if self.class_id is not None:
            bits.append(f"class=0x{self.class_id:02x}")
        if self.instance is not None:
            bits.append(f"instance={self.instance}")
        if self.attribute is not None:
            bits.append(f"attribute={self.attribute}")
        if self.elements is not None:
            bits.append(f"elements={self.elements}")
        if self.offset is not None:
            bits.append(f"offset={self.offset}")
        if self.any_of:
            bits.append(" | ".join(x.label() for x in self.any_of))
        return " ".join(bits) or "request"

    def matches(self, request: "Request") -> bool:
        if self.any_of:
            return any(alt.matches(request) for alt in self.any_of)
        return all(
            (
                _match_command(self.command, request),
                _match_service(self.service, request),
                _match_path(self, request),
                _match_value(self.tag, request.target.tag),
                _match_value(self.elements, request.target_elements),
                _match_value(self.offset, request.target_offset),
                _match_value(self.request_data, request.target.request_data),
                _match_value(self.payload, request.target.request_data),
                _match_value(self.session, request.session),
                _match_value(self.connection_id, request.connection_id),
            )
        )



def _match_command(expected: Optional[Union[int, str]], request: "Request") -> bool:
    if expected is None:
        return True
    if isinstance(expected, str):
        return COMMAND_NAMES.get(request.command, "").lower() == expected.lower()
    return request.command == expected


def _match_service(expected: Optional[Union[int, str]], request: "Request") -> bool:
    if expected is None:
        return True
    actual = request.target_service
    if isinstance(expected, str):
        wanted = expected.lower()
        if actual == SERVICE_UNCONNECTED_SEND:
            # 0x52 is UnconnectedSend in SendRRData and ReadTagFragmented in
            # SendUnitData; accept either name for the matching command.
            if wanted == "unconnectedsend":
                return request.command == SEND_RR_DATA
            if wanted == "readtagfragmented":
                return request.command == SEND_UNIT_DATA
        return SERVICE_NAMES.get(actual if actual is not None else -1, "").lower() == wanted
    return actual == expected


def _service_name_for(service: Optional[int], command: int) -> str:
    if service is None:
        return ""
    if service == SERVICE_UNCONNECTED_SEND:
        return "UnconnectedSend" if command == SEND_RR_DATA else "ReadTagFragmented"
    return SERVICE_NAMES.get(service, f"0x{service:02x}")


def _match_path(expected: Expect, request: "Request") -> bool:
    target = request.target
    if expected.path is not None:
        wanted = expected.path
        if isinstance(wanted, CIPPath):
            return wanted == target.path
        if isinstance(wanted, bytes):
            return wanted == target.path.raw
        return wanted == target.path.symbol or wanted == target.path.describe()
    if expected.class_id is not None and target.path.class_id != expected.class_id:
        return False
    if expected.instance is not None and target.path.instance_id != expected.instance:
        return False
    if expected.attribute is not None and target.path.attribute_id != expected.attribute:
        return False
    return True


def _match_value(expected: Any, actual: Any) -> bool:
    if expected is None:
        return True
    if callable(expected) and not isinstance(expected, (bytes, bytearray)):
        return bool(expected(actual))
    return expected == actual


def _command_name(value: Union[int, str]) -> str:
    if isinstance(value, int):
        return COMMAND_NAMES.get(value, f"0x{value:04x}")
    return value


def _service_name(value: Union[int, str]) -> str:
    if isinstance(value, int):
        return SERVICE_NAMES.get(value, f"0x{value:02x}")
    return value


@dataclass
class Step:
    """One expected request and its reply."""

    expect: Expect
    reply: Optional[Reply] = None
    independent: bool = False
    consumed: int = 0
    description: Optional[str] = None

    def label(self) -> str:
        return self.description or self.expect.label()


def RegisterSession(reply: Optional[Reply] = None, **kwargs) -> Step:
    """Expect ``RegisterSession`` (no reply needed; a handle is allocated)."""

    return Step(
        Expect(command=REGISTER_SESSION, name="RegisterSession", **kwargs),
        reply,
    )


def ListIdentity(reply: Optional[Reply] = None, **kwargs) -> Step:
    """Expect a ListIdentity request."""

    return Step(
        Expect(command=LIST_IDENTITY, name="ListIdentity", **kwargs),
        reply or Reply.list_identity(),
    )


def ForwardOpen(reply: Optional[Reply] = None, **kwargs) -> Step:
    """Expect a Forward Open or Large Forward Open request.

    pycomm3 sends these unconnected, but the peer accepts either
    encapsulation to keep scenarios readable.
    """

    alt = (
        Expect(
            service=SERVICE_LARGE_FORWARD_OPEN,
            class_id=CLASS_CONNECTION_MANAGER,
            instance=1,
            **kwargs,
        ),
        Expect(
            service=SERVICE_FORWARD_OPEN,
            class_id=CLASS_CONNECTION_MANAGER,
            instance=1,
            **kwargs,
        ),
    )
    return Step(
        Expect(any_of=alt, name="ForwardOpen"),
        reply or Reply.forward_open(),
    )


def ForwardClose(
    reply: Optional[Reply] = None, unregister: bool = True, **kwargs
) -> Union[Step, List[Step]]:
    """Expect a Forward Close and, by default, the following UnRegisterSession.

    Disconnect scenarios almost always include both frames, so the helper
    returns a small list of steps that :class:`Scenario` flattens.  Pass
    ``unregister=False`` for a connection-only close.
    """

    steps: List[Step] = [
        Step(
            Expect(
                service=SERVICE_FORWARD_CLOSE,
                class_id=CLASS_CONNECTION_MANAGER,
                instance=1,
                name="ForwardClose",
                **kwargs,
            ),
            reply or Reply.forward_close(),
        )
    ]
    if unregister:
        steps.append(UnRegisterSession())
    return steps


def UnRegisterSession(**kwargs) -> Step:
    """Expect an UnRegisterSession frame (which has no response)."""

    return Step(
        Expect(command=UNREGISTER_SESSION, name="UnRegisterSession", **kwargs)
    )


def Disconnect() -> List[Step]:
    """Alias for a ForwardClose + UnRegisterSession disconnect sequence."""

    return ForwardClose()


def Read(
    tag: Optional[str] = None,
    elements: Optional[int] = None,
    reply: Optional[Reply] = None,
    independent: bool = False,
    **kwargs,
) -> Step:
    """Expect a ReadTag request for ``tag`` (symbolic or instance path)."""

    return Step(
        Expect(
            command=SEND_UNIT_DATA,
            service=SERVICE_READ_TAG,
            tag=tag,
            elements=elements,
            name=f"Read {tag}" if tag else "Read",
            **kwargs,
        ),
        reply,
        independent=independent,
    )


def ReadFragmented(
    tag: Optional[str] = None,
    elements: Optional[int] = None,
    offset: Optional[int] = None,
    reply: Optional[Reply] = None,
    independent: bool = False,
    **kwargs,
) -> Step:
    """Expect a ReadTagFragmented request, optionally at a specific offset."""

    return Step(
        Expect(
            command=SEND_UNIT_DATA,
            service=SERVICE_READ_TAG_FRAGMENTED,
            tag=tag,
            elements=elements,
            offset=offset,
            name=f"ReadFragmented {tag}@{offset}" if tag else "ReadFragmented",
            **kwargs,
        ),
        reply,
        independent=independent,
    )


def Write(
    tag: Optional[str] = None,
    elements: Optional[int] = None,
    reply: Optional[Reply] = None,
    **kwargs,
) -> Step:
    return Step(
        Expect(
            command=SEND_UNIT_DATA,
            service=SERVICE_WRITE_TAG,
            tag=tag,
            elements=elements,
            name=f"Write {tag}" if tag else "Write",
            **kwargs,
        ),
        reply or Reply.write(),
    )


def Generic(
    service: Union[int, str],
    class_id: Optional[int] = None,
    instance: Optional[int] = None,
    attribute: Optional[int] = None,
    reply: Optional[Reply] = None,
    connected: bool = True,
    **kwargs,
) -> Step:
    """Expect a generic CIP service (connected by default)."""

    command = SEND_UNIT_DATA if connected else SEND_RR_DATA
    return Step(
        Expect(
            command=command,
            service=service,
            class_id=class_id,
            instance=instance,
            attribute=attribute,
            **kwargs,
        ),
        reply,
    )


def Unconnected(
    service: Union[int, str],
    class_id: Optional[int] = None,
    instance: Optional[int] = None,
    attribute: Optional[int] = None,
    reply: Optional[Reply] = None,
    **kwargs,
) -> Step:
    """Expect an ``UnconnectedSend`` wrapping the given service.

    ``Expect`` resolves matching against the wrapped service and its path;
    the ``SendRRData`` command distinguishes it from the connected form.
    """

    return Step(
        Expect(
            command=SEND_RR_DATA,
            service=service,
            class_id=class_id,
            instance=instance,
            attribute=attribute,
            name=kwargs.pop("name", "UnconnectedSend"),
            **kwargs,
        ),
        reply,
    )


# ---------------------------------------------------------------------------
# Parsed requests
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Request:
    command: int
    session: int
    context: bytes
    option: int
    raw: bytes
    body: bytes = b""
    service: Optional[int] = None
    service_data: bytes = b""
    path: CIPPath = field(default_factory=lambda: CIPPath((), b""))
    request_data: bytes = b""
    elements: Optional[int] = None
    offset: Optional[int] = None
    connection_id: Optional[int] = None
    sequence: Optional[int] = None
    address_item: Optional[bytes] = None
    embedded: Tuple["Request", ...] = ()
    parse_error: Optional[str] = None

    @property
    def tag(self) -> Optional[str]:
        return self.path.symbol

    @property
    def target(self) -> "Request":
        """The request that carries the service semantics.

        ``UnconnectedSend`` wraps a single inner request, so expectations and
        replies are expressed against the wrapped service while the
        encapsulation still applies to the outer frame.  A
        ``MultipleServiceRequest`` keeps its own service; the embedded
        requests are available via :attr:`multi_requests`.
        """

        if self.service == SERVICE_UNCONNECTED_SEND and self.embedded:
            return self.embedded[0]
        return self

    @property
    def multi_requests(self) -> Tuple["Request", ...]:
        if self.service == SERVICE_MULTI:
            return self.embedded
        return ()

    @property
    def target_service(self) -> Optional[int]:
        return self.target.service

    @property
    def target_elements(self) -> Optional[int]:
        return self.target.elements

    @property
    def target_offset(self) -> Optional[int]:
        return self.target.offset

    @property
    def target_request_data(self) -> bytes:
        return self.target.request_data

    @property
    def target_service_name(self) -> str:
        return _service_name_for(self.target_service, self.command)

    @property
    def command_name(self) -> str:
        return COMMAND_NAMES.get(self.command, f"0x{self.command:04x}")

    @property
    def service_name(self) -> str:
        return _service_name_for(self.service, self.command)

    def describe(self) -> str:
        target = self.target
        details = [self.command_name]
        if self.session:
            details.append(f"session=0x{self.session:08x}")
        if self.connection_id is not None:
            details.append(f"connection=0x{self.connection_id:08x}")
        if self.sequence is not None:
            details.append(f"sequence={self.sequence}")
        if target.service is not None:
            details.append(f"service={self.target_service_name}")
        if target.path.segments:
            details.append(f"path={target.path.describe()}")
        if target.elements is not None:
            details.append(f"elements={target.elements}")
        if target.offset is not None:
            details.append(f"offset={target.offset}")
        if target.request_data:
            details.append(f"data=({_hex(target.request_data)})")
        if self.parse_error:
            details.append(f"parse_error={self.parse_error}")
        return " ".join(details)

def _parse_request(raw: bytes) -> Union[Request, Tuple[int, int]]:
    """Parse an encapsulation frame.

    Returns a :class:`Request` or ``(command, status)`` for frames that cannot
    be parsed well enough to dispatch.
    """

    if len(raw) < HEADER_SIZE:
        return 0, STATUS_INVALID_LENGTH
    command = _u16(raw, 0)
    length = _u16(raw, 2)
    session = _u32(raw, 4)
    status = _u32(raw, 8)
    context = raw[12:20]
    option = _u32(raw, 20)
    body = raw[HEADER_SIZE : HEADER_SIZE + length]
    if status != 0:
        return command, status
    if command in (NOP, LIST_TARGETS, LIST_SERVICES, LIST_IDENTITY, LIST_INTERFACES):
        return Request(command, session, context, option, raw, body=body)

    if command == REGISTER_SESSION:
        return Request(command, session, context, option, raw, body=body)

    if command == UNREGISTER_SESSION:
        return Request(command, session, context, option, raw, body=body)

    if command == SEND_RR_DATA:
        return _parse_cpf(raw, body, command, session, context, option, connected=False)
    if command == SEND_UNIT_DATA:
        return _parse_cpf(raw, body, command, session, context, option, connected=True)

    return command, 0x0001


def _parse_cpf(
    raw: bytes,
    body: bytes,
    command: int,
    session: int,
    context: bytes,
    option: int,
    connected: bool,
) -> Request:
    try:
        offset = 0
        interface = _u32(body, offset)
        offset += 4
        timeout = _u16(body, offset)
        offset += 2
        item_count = _u16(body, offset)
        offset += 2
        if interface != 0:
            return Request(command, session, context, option, raw, body=body, parse_error="interface handle != 0")
        address_item = None
        data_item = None
        for _ in range(item_count):
            item_type = _u16(body, offset)
            item_length = _u16(body, offset + 2)
            offset += 4
            item_data = body[offset : offset + item_length]
            offset += item_length
            if item_type in (0x00A1, 0x0000) and address_item is None:
                address_item = item_data
            elif item_type in (0x00B1, 0x00B2) and data_item is None:
                data_item = item_data
        if data_item is None:
            return Request(
                command, session, context, option, raw, body=body,
                address_item=address_item, parse_error="missing data item",
            )
        if len(data_item) < 1:
            return Request(
                command, session, context, option, raw, body=body,
                address_item=address_item, parse_error="short data item",
            )
        # SendUnitData prepends the connection sequence to the CIP message;
        # SendRRData (unconnected) does not.
        if connected:
            if len(data_item) < 3:
                return Request(
                    command, session, context, option, raw, body=body,
                    address_item=address_item, parse_error="short data item",
                )
            sequence = _u16(data_item, 0)
            message = data_item[2:]
        else:
            sequence = None
            message = data_item
        if not message:
            return Request(
                command, session, context, option, raw, body=body,
                address_item=address_item, sequence=sequence,
                parse_error="empty CIP message",
            )
        service = message[0]
        request = _parse_cip_message(
            raw, body, command, session, context, option, service, message[1:], address_item, sequence,
            connected,
        )
        return request
    except Exception as err:  # malformed item list
        return Request(command, session, context, option, raw, body=body, parse_error=str(err))


def _parse_cip_message(
    raw: bytes,
    body: bytes,
    command: int,
    session: int,
    context: bytes,
    option: int,
    service: int,
    data: bytes,
    address_item: Optional[bytes],
    sequence: int,
    connected: bool,
) -> Request:
    connection_id = None
    if connected and address_item is not None and len(address_item) >= 4:
        connection_id = _u32(address_item, 0)

    if service == SERVICE_UNCONNECTED_SEND and command == SEND_RR_DATA:
        # UnconnectedSend: path, priority/ticks, embedded length, message, route
        # Note: service 0x52 is also ReadTagFragmented; only an unconnected
        # (SendRRData) frame can carry UnconnectedSend.
        try:
            path = decode_path(data)
            offset = 1 + data[0] * 2 if data else 0
            offset += 2  # priority / ticks
            embedded_len = _u16(data, offset)
            offset += 2
            embedded_raw = data[offset : offset + embedded_len]
            if len(embedded_raw) < 1:
                err = "empty embedded message"
                return Request(
                    command, session, context, option, raw, body=body, service=service,
                    service_data=data, path=path, address_item=address_item,
                    sequence=sequence, connection_id=connection_id, parse_error=err,
                )
            embedded_service = embedded_raw[0]
            embedded = _parse_cip_message(
                raw, body, command, session, context, option, embedded_service,
                embedded_raw[1:], address_item, sequence, connected,
            )
            embedded = replace(
                embedded,
                embedded=(),
                service=embedded_service,
                connection_id=connection_id,
                address_item=address_item,
            )
            return Request(
                command, session, context, option, raw, body=body, service=service,
                service_data=data, path=path, address_item=address_item,
                sequence=sequence, connection_id=connection_id,
                embedded=(embedded,), request_data=data,
            )
        except Exception as err:
            return Request(
                command, session, context, option, raw, body=body, service=service,
                service_data=data, address_item=address_item, sequence=sequence,
                connection_id=connection_id, parse_error=str(err),
            )

    if service == SERVICE_FORWARD_CLOSE:
        path = decode_path(data) if data else CIPPath((), b"")
        try:
            path_len = 1 + (data[0] * 2 if data else 0)
            rest = data[path_len:]
            request_data = rest
            class_ = path.class_id
            instance = path.instance_id
        except Exception:
            request_data = b""
        return Request(
            command, session, context, option, raw, body=body, service=service,
            service_data=data, path=path, request_data=request_data,
            address_item=address_item, sequence=sequence, connection_id=connection_id,
        )

    # All remaining services start with a request path.
    try:
        path = decode_path(data)
    except Exception as err:
        return Request(
            command, session, context, option, raw, body=body, service=service,
            service_data=data, address_item=address_item, sequence=sequence,
            connection_id=connection_id, parse_error=str(err),
        )
    path_bytes = 1 + (data[0] * 2) if data else 0
    rest = data[path_bytes:]

    if service == SERVICE_MULTI:
        embedded: List[Request] = []
        try:
            count = _u16(rest, 0)
            offsets = [_u16(rest, 2 + i * 2) for i in range(count)]
            for index, start in enumerate(offsets):
                end = offsets[index + 1] if index + 1 < len(offsets) else len(rest)
                segment = rest[start:end]
                if not segment:
                    continue
                embedded.append(
                    _parse_cip_message(
                        raw, body, command, session, context, option,
                        segment[0], segment[1:], address_item, sequence, connected,
                    )
                )
        except Exception as err:
            return Request(
                command, session, context, option, raw, body=body, service=service,
                service_data=data, path=path, request_data=rest,
                address_item=address_item, sequence=sequence,
                connection_id=connection_id, parse_error=f"multiple service: {err}",
            )
        return Request(
            command, session, context, option, raw, body=body, service=service,
            service_data=data, path=path, request_data=rest,
            address_item=address_item, sequence=sequence,
            connection_id=connection_id, embedded=tuple(embedded),
        )

    elements = None
    offset = None
    request_data = rest
    if service in (SERVICE_READ_TAG, SERVICE_READ_TAG_FRAGMENTED, SERVICE_WRITE_TAG, SERVICE_WRITE_TAG_FRAGMENTED):
        if service == SERVICE_READ_TAG:
            elements = _u16(rest, 0) if len(rest) >= 2 else None
            request_data = rest[2:]
        elif service == SERVICE_READ_TAG_FRAGMENTED:
            elements = _u16(rest, 0) if len(rest) >= 2 else None
            offset = _u32(rest, 2) if len(rest) >= 6 else None
            request_data = rest
        elif service == SERVICE_WRITE_TAG:
            elements = _u16(rest, 2) if len(rest) >= 4 else None
            request_data = rest
        else:
            elements = _u16(rest, 2) if len(rest) >= 4 else None
            offset = _u32(rest, 4) if len(rest) >= 8 else None
            request_data = rest
    elif service == SERVICE_GET_ATTRIBUTE_LIST:
        # attribute count followed by attribute IDs
        request_data = rest
    elif service == SERVICE_GET_ATTRIBUTES_ALL:
        request_data = rest
    elif service in (SERVICE_FORWARD_OPEN, SERVICE_LARGE_FORWARD_OPEN):
        request_data = rest

    return Request(
        command, session, context, option, raw, body=body, service=service,
        service_data=data, path=path, request_data=request_data,
        elements=elements, offset=offset, address_item=address_item,
        sequence=sequence, connection_id=connection_id,
    )


# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------


def _encapsulation_header(
    command: int, length: int, session: int, status: int, context: bytes, option: int = 0
) -> bytes:
    return b"".join(
        (
            UINT.encode(command),
            UINT.encode(length),
            UDINT.encode(session),
            UDINT.encode(status),
            context[:8].ljust(8, b"\x00"),
            UDINT.encode(option),
        )
    )


def _cpf_item(item_type: int, data: bytes) -> bytes:
    return UINT.encode(item_type) + UINT.encode(len(data)) + data


def _cip_reply_header(
    service: int,
    service_status: int = STATUS_SUCCESS,
    extended_status: Sequence[int] = (),
    data: bytes = b"",
) -> bytes:
    """CIP reply: service, reserved, status, extended status size/words, data."""

    header = bytes(
        (
            (service + 0x80) & 0xFF,
            0x00,
            service_status & 0xFF,
            len(extended_status),
        )
    )
    return header + b"".join(UINT.encode(word) for word in extended_status) + data


def _send_unit_data_response(request: Request, cip: bytes) -> bytes:
    """Wrap a complete CIP reply in a SendUnitData encapsulation frame."""

    connection_id = request.connection_id or 0
    body = b"".join(
        (
            b"\x00\x00\x00\x00",
            b"\x0a\x00",
            UINT.encode(2),
            _cpf_item(0x00A1, UDINT.encode(connection_id)),
            _cpf_item(0x00B1, UINT.encode(request.sequence or 0) + cip),
        )
    )
    return (
        _encapsulation_header(SEND_UNIT_DATA, len(body), request.session, 0, request.context)
        + body
    )


def _send_rr_data_response(request: Request, cip: bytes) -> bytes:
    """Wrap a complete CIP reply in a SendRRData encapsulation frame."""

    body = b"".join(
        (
            b"\x00\x00\x00\x00",
            b"\x0a\x00",
            UINT.encode(2),
            _cpf_item(0x0000, b""),
            _cpf_item(0x00B2, cip),
        )
    )
    return (
        _encapsulation_header(SEND_RR_DATA, len(body), request.session, 0, request.context)
        + body
    )



def _forward_open_value(reply: Reply) -> bytes:
    # Standard / extended Forward Open success reply:
    # O->T connection ID (UDINT), T->O connection ID (UDINT), API (UDINT),
    # application reply size (USINT), reserved (USINT), application reply.
    # pycomm3 only uses the first 4 bytes (the target CID).
    target_cid = int(reply.payload or 0)
    return (
        UDINT.encode(target_cid)
        + UDINT.encode(target_cid)
        + b"\x00\x00\x00\x00"
        + b"\x00\x00"
    )


def _identity_value(extra: Dict[str, Any]) -> bytes:
    revision_major, revision_minor = extra.get("revision", (20, 11))
    status = extra.get("status", b"\x00\x00")
    product_name = str(extra.get("product_name", "1756-L61")).encode("latin-1")
    return b"".join(
        (
            UINT.encode(int(extra.get("vendor", 1))),
            UINT.encode(int(extra.get("product_type", 14))),
            UINT.encode(int(extra.get("product_code", 89))),
            USINT.encode(int(revision_major)),
            USINT.encode(int(revision_minor)),
            status[:2].ljust(2, b"\x00"),
            UDINT.encode(int(extra.get("serial", 0xC00FA09B))),
            USINT.encode(len(product_name)),
            product_name,
        )
    )


def _list_identity_payload(extra: Dict[str, Any]) -> bytes:
    """Build the ListIdentity item (item type, length, identity fields)."""

    revision_major, revision_minor = extra.get("revision", (20, 11))
    status = extra.get("status", b"\x00\x00")
    product_name = str(extra.get("product_name", "1756-L61")).encode("latin-1")
    address = ipaddress.IPv4Address(extra.get("ip_address", "192.168.1.10")).packed
    identity = b"".join(
        (
            UINT.encode(int(extra.get("encap_protocol_version", 1))),
            UINT.encode(2),  # socket address family (AF_INET)
            UINT.encode(int(extra.get("port", 44818))),
            address,
            b"\x00" * 8,  # socket address padding
            UINT.encode(int(extra.get("vendor", 1))),
            UINT.encode(int(extra.get("product_type", 14))),
            UINT.encode(int(extra.get("product_code", 89))),
            USINT.encode(int(revision_major)),
            USINT.encode(int(revision_minor)),
            status[:2].ljust(2, b"\x00"),
            UDINT.encode(int(extra.get("serial", 0xC00FA09B))),
            USINT.encode(len(product_name)),
            product_name,
            USINT.encode(int(extra.get("state", 3))),
        )
    )
    return UINT.encode(0x000C) + UINT.encode(len(identity)) + identity


def _read_response_data(reply: Reply) -> bytes:
    value: Value = reply.payload
    return UINT.encode(value.type_code) + value.encode()


def _fragmented_response_data(reply: Reply, request: Request) -> Tuple[bytes, int]:
    value: Value = reply.payload
    count = reply.extra.get("count")
    more = bool(reply.extra.get("more", True))
    status = STATUS_INSUFFICIENT_PACKETS if more else reply.service_status
    element_size = value.size
    encoded = value.encode()
    # ReadTagFragmented offsets are byte offsets into the encoded value.
    start_byte = request.target_offset or 0
    if count is None:
        end_byte = len(encoded)
    else:
        end_byte = start_byte + int(count) * element_size
    return UINT.encode(value.type_code) + encoded[start_byte:end_byte], status


def _encode_embedded_reply(reply: Reply, request: "Request") -> bytes:
    service = reply.extra.get("service")
    if service is None and isinstance(reply.payload, tuple) and reply.kind == "raw_service":
        service = reply.payload[0]
    service = service if service is not None else (request.target_service or SERVICE_READ_TAG)
    if reply.kind == "read":
        data = _read_response_data(reply)
    elif reply.kind == "write":
        data = b""
    elif reply.kind in ("raw", "raw_service"):
        data = bytes(reply.payload[1] if reply.kind == "raw_service" else reply.payload or b"")
    elif reply.kind == "fragmented":
        data, _status = _fragmented_response_data(reply, request)
    elif reply.kind == "multi":
        data = _encode_reply(reply, request)
    else:
        data = b""
    return _cip_reply_header(service, reply.service_status, reply.extended_status, data)


def _encode_reply(reply: Reply, request: Request) -> Optional[bytes]:
    """Build the complete encapsulation frame for a matched request."""

    if reply.kind == "disconnect":
        return None

    service = request.service or 0
    if request.service == SERVICE_UNCONNECTED_SEND and request.embedded:
        # pycomm3 treats the UnconnectedSend reply as the embedded service's
        # reply (Rockwell's devices reply this way), so do not wrap it again.
        service = request.target_service or service
    if request.command == REGISTER_SESSION:
        session = int(reply.payload or 0)
        body = b"\x01\x00\x00\x00" + UDINT.encode(session)
        return _encapsulation_header(REGISTER_SESSION, len(body), session, 0, request.context) + body

    if request.command == UNREGISTER_SESSION:
        # No response is defined for UnRegisterSession.
        return None

    if request.command == LIST_IDENTITY:
        payload = reply.payload if isinstance(reply.payload, dict) else {}
        item = payload if isinstance(payload, bytes) else _list_identity_payload(payload)
        body = UINT.encode(1) + item  # item count + identity item
        return _encapsulation_header(LIST_IDENTITY, len(body), request.session, 0, request.context) + body

    cip = b""
    service_status = reply.service_status
    extended_status = reply.extended_status

    if reply.kind == "identity":
        cip = _cip_reply_header(
            service,
            reply.service_status,
            reply.extended_status,
            _identity_value(reply.payload if isinstance(reply.payload, dict) else {}),
        )
    elif reply.kind == "forward_open":
        cip = _cip_reply_header(
            service, reply.service_status, reply.extended_status, _forward_open_value(reply)
        )
    elif reply.kind == "forward_close":
        cip = _cip_reply_header(service, reply.service_status, reply.extended_status, b"")
    elif reply.kind == "read":
        cip = _cip_reply_header(
            service, reply.service_status, reply.extended_status, _read_response_data(reply)
        )
    elif reply.kind == "fragmented":
        data, service_status = _fragmented_response_data(reply, request)
        cip = _cip_reply_header(service, service_status, reply.extended_status, data)
    elif reply.kind == "write":
        cip = _cip_reply_header(service, reply.service_status, reply.extended_status, b"")
    elif reply.kind == "raw":
        cip = _cip_reply_header(
            service, reply.service_status, reply.extended_status, bytes(reply.payload or b"")
        )
    elif reply.kind == "raw_service":
        embedded_service, data = reply.payload
        cip = _cip_reply_header(
            embedded_service, reply.service_status, reply.extended_status, data
        )
    elif reply.kind == "multi":
        bodies = []
        for index, inner_reply in enumerate(reply.payload):
            inner_request = (
                request.target.embedded[index]
                if index < len(request.target.embedded)
                else request
            )
            bodies.append(_encode_embedded_reply(inner_reply, inner_request))
        count = len(bodies)
        header_size = 2 + count * 2
        offsets = b""
        payload = b""
        for body in bodies:
            offsets += UINT.encode(header_size + len(payload))
            payload += body
        cip = _cip_reply_header(
            service,
            reply.service_status,
            reply.extended_status,
            UINT.encode(count) + offsets + payload,
        )
    else:
        cip = _cip_reply_header(service, reply.service_status, reply.extended_status, b"")

    if request.command == SEND_UNIT_DATA:
        return _send_unit_data_response(request, cip)
    return _send_rr_data_response(request, cip)


# ---------------------------------------------------------------------------
# Scenario and mismatch reporting
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Exchange:
    """One processed frame, correlated with the scenario step it satisfied.

    ``request`` is the decoded request, ``step`` the scenario step that was
    consumed (``None`` for protocol rejections or frames with no declared
    reply), and ``response`` the raw response bytes (``None`` for frames with
    no response).
    """

    index: int
    request: Request
    step: Optional[Step]
    response: Optional[bytes]


@dataclass(frozen=True)
class Rejection:
    """A protocol-invalid frame that did not consume any scenario step."""

    index: int
    request: Request
    reason: str
    response: Optional[bytes]


class ScenarioMismatch(CommError, AssertionError):
    """Raised when an incoming request does not match the next scenario step.

    ``first_divergence`` is a human-readable description of the offending
    request, ``consumed`` lists the steps that were already used (with their
    original scenario index in ``consumed_indexes``), and ``remaining`` lists
    the steps that were still outstanding.  ``attempted`` is the normalized
    :class:`Request`.

    The peer stops processing after the first mismatch; it will not send any
    of the remaining declared replies.
    """

    def __init__(
        self,
        message: str,
        first_divergence: str,
        consumed: Sequence[Step],
        remaining: Sequence[Step],
        attempted: Optional[Request] = None,
        consumed_indexes: Sequence[int] = (),
        remaining_indexes: Sequence[int] = (),
    ):
        super().__init__(message)
        self.first_divergence = first_divergence
        self.consumed = tuple(consumed)
        self.remaining = tuple(remaining)
        self.attempted = attempted
        self.consumed_indexes = tuple(consumed_indexes)
        self.remaining_indexes = tuple(remaining_indexes)

    @property
    def consumed_range(self) -> Optional[Tuple[int, int]]:
        """Inclusive range of original scenario indexes already consumed."""

        if not self.consumed_indexes:
            return None
        return min(self.consumed_indexes), max(self.consumed_indexes)

    def __str__(self) -> str:
        lines = [str(self.args[0]) if self.args else "scenario mismatch"]
        lines.append(f"first divergence: {self.first_divergence}")
        if self.consumed:
            span = self.consumed_range
            lines.append(f"consumed steps (scenario range {span[0]}..{span[1]}):")
            lines.extend(
                f"  [{index}] {step.label()}"
                for index, step in zip(self.consumed_indexes, self.consumed)
            )
        else:
            lines.append("consumed steps: none")
        if self.remaining:
            lines.append("remaining steps:")
            lines.extend(
                f"  [{index}] {step.label()}"
                for index, step in zip(self.remaining_indexes, self.remaining)
            )
        else:
            lines.append("remaining steps: none")
        return "\n".join(lines)


class Scenario:
    """An ordered collection of :class:`Step` objects.

    Iterables of steps are flattened one level, so helpers such as
    :func:`ForwardClose` can return a short list (close + unregister) without
    the caller having to splice it in.
    """

    def __init__(self, steps: Iterable[Union[Step, Expect, "Reply", List[Step]]], name: str = ""):
        self.name = name
        self.steps: List[Step] = []
        for item in steps:
            if isinstance(item, Step):
                self.steps.append(item)
            elif isinstance(item, Expect):
                self.steps.append(Step(item))
            elif isinstance(item, Reply):
                self.steps.append(Step(Expect(), item))
            elif isinstance(item, (list, tuple)):
                self.steps.extend(item)
            else:
                raise TypeError(f"invalid scenario step: {item!r}")

    def __iter__(self):
        return iter(self.steps)

    def __len__(self):
        return len(self.steps)

    def reset(self):
        for step in self.steps:
            step.consumed = 0


class _Session:
    def __init__(self, handle: int):
        self.handle = handle
        self.alive = True
        self.connection_id: Optional[int] = None
        self.forward_open_count = 0


# ---------------------------------------------------------------------------
# The peer
# ---------------------------------------------------------------------------


class CIPPeer:
    """An in-memory CIP target driven by a :class:`Scenario`.

    Parameters
    ----------
    scenario:
        The expected sequence of requests and their replies.
    session_id:
        Value used for the first successful RegisterSession (subsequent
        registrations receive incrementing handles).
    name:
        Optional scenario name, used in assertion messages.
    """

    def __init__(
        self,
        scenario: Union[Scenario, Iterable[Union[Step, Expect, Reply]]],
        session_id: int = 0x1234_5678,
        name: str = "",
    ):
        self.scenario = scenario if isinstance(scenario, Scenario) else Scenario(scenario)
        if name and not self.scenario.name:
            self.scenario.name = name
        self._session_id = session_id
        self._next_session_id = session_id
        self._sessions: Dict[int, _Session] = {}
        self._active_session: Optional[int] = None
        self._requests: List[Request] = []
        self._frames: List[Tuple[bytes, Optional[bytes]]] = []
        self._exchanges: List[Exchange] = []
        self._rejections: List[Rejection] = []
        self._mismatch: Optional[ScenarioMismatch] = None
        self._lock = threading.RLock()

    # -- public API --------------------------------------------------------

    @property
    def requests(self) -> Tuple[Request, ...]:
        """Every parsed request received so far, in arrival order."""

        return tuple(self._requests)

    @property
    def frames(self) -> Tuple[Tuple[bytes, Optional[bytes]], ...]:
        """Raw ``(request, response)`` byte pairs, in arrival order."""

        return tuple(self._frames)

    @property
    def exchanges(self) -> Tuple[Exchange, ...]:
        """Every processed frame with the scenario step it satisfied."""

        return tuple(self._exchanges)

    @property
    def rejections(self) -> Tuple[Rejection, ...]:
        """Protocol-invalid frames (stale session/connection) and their errors."""

        return tuple(self._rejections)

    def step_for(self, request: Request) -> Optional[Step]:
        """Return the scenario step consumed by ``request``, if any."""

        for exchange in self._exchanges:
            if exchange.request is request:
                return exchange.step
        return None

    @property
    def consumed(self) -> Tuple[Step, ...]:
        return tuple(step for step in self.scenario.steps if step.consumed)

    @property
    def remaining(self) -> Tuple[Step, ...]:
        return tuple(step for step in self.scenario.steps if not step.consumed)

    @property
    def mismatch(self) -> Optional[ScenarioMismatch]:
        return self._mismatch

    @property
    def active_session(self) -> Optional[int]:
        """Handle of the most recently registered session, if alive."""

        if self._active_session is None:
            return None
        session = self._sessions.get(self._active_session)
        if session is None or not session.alive:
            return None
        return self._active_session

    @property
    def connection_id(self) -> Optional[int]:
        """CIP connection ID opened in the active session, if any."""

        if self._active_session is None:
            return None
        session = self._sessions.get(self._active_session)
        return None if session is None else session.connection_id

    def reset(self):
        """Reset consumption state and logs, leaving the scenario intact."""

        with self._lock:
            self.scenario.reset()
            self._sessions.clear()
            self._active_session = None
            self._requests.clear()
            self._frames.clear()
            self._exchanges.clear()
            self._rejections.clear()
            self._mismatch = None
            self._next_session_id = self._session_id

    def socket(self) -> "CIPSocket":
        """Return a socket-compatible transport connected to this peer.

        Each call returns an independent transport sharing the same peer and
        scenario, which lets a test simulate a reconnect or a second client.
        """

        return CIPSocket(self)

    def close_session(self, session: Optional[int] = None):
        """Mark a session (default: the active one) as dead.

        Any packet using the handle afterwards is rejected with
        ``0x64`` (invalid session handle).
        """

        with self._lock:
            handle = self._active_session if session is None else session
            if handle is not None and handle in self._sessions:
                self._sessions[handle].alive = False
            if self._active_session == handle:
                self._active_session = None

    def disconnect(self):
        """Mark the active session dead, as if the network dropped."""

        with self._lock:
            if self._active_session is not None and self._active_session in self._sessions:
                self._sessions[self._active_session].alive = False
            self._active_session = None

    def assert_complete(self):
        """Assert that every declared step was consumed exactly once."""

        self.assert_no_mismatch()
        pending = self.remaining
        if pending:
            labels = "\n".join(f"  [{i}] {step.label()}" for i, step in enumerate(pending))
            raise AssertionError(f"scenario not complete, {len(pending)} step(s) remaining:\n{labels}")

    def assert_no_mismatch(self):
        """Re-raise the recorded :class:`ScenarioMismatch`, if any.

        The drivers wrap transport exceptions in ``CommError`` when sending.
        A test that received ``CommError`` can call this method to get the
        detailed scenario mismatch (first divergence, consumed range, and
        remaining steps) as the original exception.
        """

        if self._mismatch is not None:
            raise self._mismatch

    def assert_consumed(self, *labels: str):
        """Assert that a step with the given label was consumed."""

        consumed = {step.label() for step in self.consumed}
        missing = [label for label in labels if label not in consumed]
        if missing:
            raise AssertionError(f"steps not consumed: {', '.join(missing)}")

    def request(self, index: int) -> Request:
        return self._requests[index]

    # -- frame handling ----------------------------------------------------

    def handle_frame(self, frame: bytes) -> Optional[bytes]:
        """Process one complete encapsulation frame.

        Returns the response bytes, or ``None`` when the protocol dictates no
        response (UnRegisterSession, or a step with a ``disconnect`` reply).
        Raises :class:`ScenarioMismatch` for an unexpected request.

        Protocol-invalid frames (stale session, stale connection ID) are
        answered with an error and recorded in :attr:`rejections`; they never
        consume a scenario step.
        """

        with self._lock:
            if self._mismatch is not None:
                raise self._mismatch
            parsed = _parse_request(frame)
            if isinstance(parsed, tuple):
                command, status = parsed
                request = Request(command, 0, b"\x00" * 8, 0, frame)
                self._requests.append(request)
                response = self._encapsulation_error(
                    command, status, b"\x00" * 8, 0
                )
                self._frames.append((frame, response))
                self._record_exchange(request, None, response)
                self._rejections.append(
                    Rejection(
                        len(self._exchanges) - 1,
                        request,
                        f"malformed encapsulation frame (status 0x{status:04x})",
                        response,
                    )
                )
                return response

            self._requests.append(parsed)
            response, step, rejection = self._dispatch(parsed)
            self._frames.append((frame, response))
            self._record_exchange(parsed, step, response)
            if rejection:
                self._rejections.append(
                    Rejection(len(self._exchanges) - 1, parsed, rejection, response)
                )
            return response

    def _record_exchange(
        self, request: Request, step: Optional[Step], response: Optional[bytes]
    ):
        self._exchanges.append(Exchange(len(self._exchanges), request, step, response))

    def _encapsulation_error(
        self, command: int, status: int, context: bytes, session: int
    ) -> bytes:
        header = _encapsulation_header(command, 0, session, status, context)
        return header

    def _dispatch(
        self, request: Request
    ) -> Tuple[Optional[bytes], Optional[Step], Optional[str]]:
        command = request.command

        if command == REGISTER_SESSION:
            return self._handle_register(request)

        if command == UNREGISTER_SESSION:
            return self._handle_unregister(request)

        session = self._sessions.get(request.session)
        if session is None or not session.alive:
            return self._invalid_session(request)

        if command in (SEND_RR_DATA, SEND_UNIT_DATA):
            if request.session != self._active_session:
                # A late packet from an older session must never satisfy a
                # request belonging to the current connection, even if the
                # old session was left registered.
                return self._invalid_session(request)
            if command == SEND_UNIT_DATA:
                if session.connection_id is None:
                    return self._service_error(
                        request, 0x01, "SendUnitData without an open connection"
                    )
                if request.connection_id != session.connection_id:
                    return self._service_error(
                        request, 0x07,
                        "SendUnitData uses a stale connection ID",
                    )
            step = self._match(request)
            return self._apply_reply(step, request, session), step, None

        # ListIdentity and friends are stateless.
        step = self._match(request)
        return self._apply_reply(step, request, session), step, None

    def _handle_register(
        self, request: Request
    ) -> Tuple[Optional[bytes], Optional[Step], Optional[str]]:
        step = self._match(request)
        if step.reply is None:
            # Allocate a fresh handle without modifying the scenario, so
            # ``peer.reset()`` starts allocating from ``session_id`` again.
            reply = Reply.register_session(self._next_session_id)
            self._next_session_id += 1
        else:
            reply = step.reply
        handle = int(reply.payload or self._next_session_id)
        self._sessions[handle] = _Session(handle)
        self._active_session = handle
        step.consumed += 1
        return _encode_reply(reply, request), step, None

    def _handle_unregister(
        self, request: Request
    ) -> Tuple[Optional[bytes], Optional[Step], Optional[str]]:
        step = self._match(request)
        session = self._sessions.get(request.session)
        if session is not None:
            session.alive = False
        if self._active_session == request.session:
            self._active_session = None
        step.consumed += 1
        # No response is sent for UnRegisterSession.
        return None, step, None

    def _invalid_session(
        self, request: Request
    ) -> Tuple[Optional[bytes], Optional[Step], Optional[str]]:
        message = (
            f"stale or unknown session handle 0x{request.session:08x} for "
            f"{request.describe()}"
        )
        response = self._encapsulation_error(
            request.command, STATUS_INVALID_SESSION, request.context, request.session
        )
        return response, None, message

    def _service_error(
        self, request: Request, status: int, name: str
    ) -> Tuple[Optional[bytes], Optional[Step], Optional[str]]:
        service = request.target_service or 0
        cip = _cip_reply_header(service, status, (), b"")
        response = (
            _send_unit_data_response(request, cip)
            if request.command == SEND_UNIT_DATA
            else _send_rr_data_response(request, cip)
        )
        return response, None, f"{name}: {request.describe()}"

    def _raise_mismatch(self, request: Request, message: str):
        consumed = []
        consumed_indexes = []
        remaining = []
        remaining_indexes = []
        for index, step in enumerate(self.scenario.steps):
            if step.consumed:
                consumed.append(step)
                consumed_indexes.append(index)
            else:
                remaining.append(step)
                remaining_indexes.append(index)
        mismatch = ScenarioMismatch(
            message,
            first_divergence=message,
            consumed=consumed,
            remaining=remaining,
            attempted=request,
            consumed_indexes=consumed_indexes,
            remaining_indexes=remaining_indexes,
        )
        self._mismatch = mismatch
        raise mismatch

    def _match(self, request: Request) -> Step:
        if request.multi_requests:
            return self._match_multi_request(request)

        steps = self.scenario.steps
        if not steps:
            self._raise_mismatch(request, f"no steps remain for {request.describe()}")
        for index, step in enumerate(steps):
            if step.consumed:
                continue
            if not self._available(index, request):
                continue
            if step.expect.matches(request):
                return step
        # The request did not match any available step.  Point at the first
        # unconsumed step as the first divergence where possible.
        first_pending = next((step for step in steps if not step.consumed), None)
        if first_pending is None:
            self._raise_mismatch(request, f"all steps consumed, unexpected {request.describe()}")
        self._raise_mismatch(
            request,
            f"{request.describe()} did not match next expected step {first_pending.label()!r}",
        )

    def _match_multi_request(self, request: Request) -> Step:
        """Match a MultipleServiceRequest against declared read steps.

        The contiguous run of pending read steps starting at the first
        unconsumed step is taken as the candidate window.  If every candidate
        is declared independent, the embedded requests may appear in any
        order; otherwise they must appear in the declared order.  An
        unexpected embedded request is reported as the first divergence
        without consuming anything.
        """

        window = self._multi_window()
        embedded = request.multi_requests
        if not window:
            self._raise_mismatch(
                request,
                f"{request.describe()} has no declared read steps to match",
            )
        if len(embedded) > len(window):
            self._raise_mismatch(
                request,
                f"{request.describe()} has {len(embedded)} embedded requests but only "
                f"{len(window)} declared read step(s) remain",
            )
        candidates = window[: len(embedded)]
        chosen: List[Step] = []
        if all(step.independent for step in candidates):
            remaining = list(candidates)
            for inner in embedded:
                match = next(
                    (step for step in remaining if step.expect.matches(inner)), None
                )
                if match is None:
                    self._raise_mismatch(
                        request,
                        f"embedded request {inner.describe()} did not match any "
                        "declared independent read",
                    )
                chosen.append(match)
                remaining.remove(match)
        else:
            for inner, step in zip(embedded, candidates):
                if not step.expect.matches(inner):
                    self._raise_mismatch(
                        request,
                        f"embedded request {inner.describe()} did not match declared "
                        f"step {step.label()!r} in order",
                    )
                chosen.append(step)

        for step in chosen:
            step.consumed += 1

        replies = []
        for inner, step in zip(embedded, chosen):
            if step.reply is None:
                self._raise_mismatch(
                    request, f"no reply declared for step {step.label()!r}"
                )
            replies.append(step.reply)
        multi = Step(
            Expect(command=request.command, service=SERVICE_MULTI, name="MultiServiceRead"),
            Reply.multi(replies),
        )
        multi.consumed = 1
        return multi

    def _multi_window(self) -> List[Step]:
        """Contiguous pending read steps beginning at the first pending step."""

        window: List[Step] = []
        for step in self.scenario.steps:
            if step.consumed:
                continue
            if step.expect.service not in (SERVICE_READ_TAG, SERVICE_READ_TAG_FRAGMENTED):
                break
            window.append(step)
        return window

    def _available(self, index: int, request: Request) -> bool:
        step = self.scenario.steps[index]
        if step.independent:
            # Independent reads may overtake only other independent reads.
            for before in self.scenario.steps[:index]:
                if before.consumed:
                    continue
                if not before.independent:
                    return False
            return True
        # A non-independent step can only be used if every earlier step has
        # been consumed.
        return all(s.consumed for s in self.scenario.steps[:index])

    def _apply_reply(self, step: Step, request: Request, session: _Session) -> Optional[bytes]:
        reply = step.reply
        if reply is None:
            self._raise_mismatch(request, f"no reply declared for step {step.label()!r}")
        step.consumed += 1
        if reply.kind == "forward_open":
            session.connection_id = int(reply.payload or 0)
            session.forward_open_count += 1
        elif reply.kind == "forward_close":
            session.connection_id = None
        return _encode_reply(reply, request)


# ---------------------------------------------------------------------------
# Socket-compatible transport
# ---------------------------------------------------------------------------


class CIPSocket:
    """A drop-in replacement for :class:`pycomm3.socket_.Socket`.

    Frames are passed to :class:`CIPPeer` in memory.  ``receive`` returns
    exactly one complete encapsulation frame, mirroring the real socket
    wrapper's behavior.
    """

    def __init__(self, peer: CIPPeer, timeout: float = 5.0):
        self.peer = peer
        self.timeout = timeout
        self._buffer = bytearray()
        self._queue: List[bytes] = []
        self._connected = False
        self._peer_host: Optional[str] = None
        self._peer_port: Optional[int] = None
        self.closed = False

    def connect(self, host: str, port: int):
        self._peer_host = host
        self._peer_port = port
        self._connected = True

    def send(self, msg: bytes, timeout: float = 0) -> int:
        if self.closed:
            raise CommError("socket connection broken.")
        if not self._connected:
            raise CommError("socket connection broken.")
        if timeout:
            self.timeout = timeout
        self._buffer.extend(msg)
        self._process_buffer()
        return len(msg)

    def receive(self, timeout: float = 0) -> bytes:
        if timeout:
            self.timeout = timeout
        if self.closed:
            raise CommError("socket connection broken")
        if not self._queue:
            raise CommError("socket connection broken")
        return self._queue.pop(0)

    def close(self):
        self.closed = True
        self._connected = False
        self._queue.clear()
        self._buffer.clear()

    def _process_buffer(self):
        while len(self._buffer) >= HEADER_SIZE:
            length = _u16(self._buffer, 2)
            total = HEADER_SIZE + length
            if len(self._buffer) < total:
                return
            frame = bytes(self._buffer[:total])
            del self._buffer[:total]
            response = self.peer.handle_frame(frame)
            if response is not None:
                self._queue.append(response)


