"""Link/IP/TCP decoding and per-direction stream reassembly.

Segments are grouped by connection instance, not merely by 4-tuple: a
SYN without ACK opens a new instance even when an endpoint reuses the
same port for a later transfer.  Reassembly keeps holes explicit so no
byte from a neighbouring session can silently stand in for a missing
range.

Connections are described by TCP roles (``initiator`` / ``responder``),
not FTP roles: in active mode the server is the TCP initiator.
"""

import struct
from dataclasses import dataclass
from dataclasses import field

from .pcap import ETHERTYPE_IPV4
from .pcap import ETHERTYPE_IPV6
from .pcap import LINKTYPE_ETHERNET
from .pcap import LINKTYPE_LINUX_SLL
from .pcap import LINKTYPE_LINUX_SLL2
from .pcap import LINKTYPE_LOOP
from .pcap import LINKTYPE_NULL
from .pcap import LINKTYPE_RAW

TCP_FIN = 0x01
TCP_SYN = 0x02
TCP_RST = 0x04
TCP_PSH = 0x08
TCP_ACK = 0x10

_ETHERTYPE_VLAN = 0x8100
_ETHERTYPE_QINQ = 0x88A8
_SEQ_MOD = 1 << 32


class DecodeError(Exception):
    """Raised when a frame cannot be decoded into a TCP segment."""


@dataclass
class Segment:
    ts: float
    frame: int
    src: tuple
    dst: tuple
    seq: int
    ack: int
    flags: int
    payload: bytes
    truncated: bool = False

    @property
    def syn(self):
        return bool(self.flags & TCP_SYN)

    @property
    def fin(self):
        return bool(self.flags & TCP_FIN)

    @property
    def rst(self):
        return bool(self.flags & TCP_RST)


@dataclass
class Gap:
    """Missing byte range inside a reassembled direction."""

    offset: int
    length: int
    reason: str

    def as_dict(self):
        return {
            "offset": self.offset,
            "length": self.length,
            "reason": self.reason,
        }


@dataclass
class Direction:
    """One half of a TCP connection, reassembled by sequence number."""

    src: tuple
    dst: tuple
    segments: list = field(default_factory=list)
    isn: int = None
    syn_seen: bool = False
    fin_seen: bool = False
    rst_seen: bool = False

    data: bytes = b""
    gaps: list = field(default_factory=list)
    duplicate_bytes: int = 0
    overlapping_segments: int = 0
    conflicting_retransmissions: int = 0
    out_of_order_segments: int = 0
    truncated_segments: int = 0
    payload_bytes: int = 0
    first_ts: float = None
    last_ts: float = None
    offset_map: list = field(default_factory=list)

    def add(self, seg):
        self.segments.append(seg)
        if seg.syn and not self.syn_seen:
            self.syn_seen = True
            self.isn = seg.seq
        if seg.fin:
            self.fin_seen = True
        if seg.rst:
            self.rst_seen = True
        if self.first_ts is None:
            self.first_ts = seg.ts
        self.last_ts = seg.ts

    @property
    def base(self):
        """Sequence number of the first data byte."""
        if self.isn is not None:
            return (self.isn + 1) % _SEQ_MOD
        for seg in self.segments:
            if seg.payload:
                return seg.seq
        return None

    def reassemble(self):
        base = self.base
        self.payload_bytes = sum(len(s.payload) for s in self.segments)
        if base is None:
            return

        ordered = [s for s in self.segments if s.payload]
        if not ordered:
            return
        highest = 0
        for seg in sorted(ordered, key=lambda s: (s.ts, s.frame)):
            if seg.truncated:
                self.truncated_segments += 1
            rel = (seg.seq - base) % _SEQ_MOD
            if rel < highest:
                self.out_of_order_segments += 1
            highest = max(highest, rel + len(seg.payload))

        limit = max(
            (seg.seq - base) % _SEQ_MOD + len(seg.payload) for seg in ordered
        )
        data = bytearray(limit)
        filled = bytearray(limit)
        self.offset_map = []

        # First arrival wins on overlap, as a real TCP stack would deliver
        # bytes it already held; conflicting retransmissions are flagged.
        for seg in sorted(ordered, key=lambda s: (s.ts, s.frame)):
            rel = (seg.seq - base) % _SEQ_MOD
            end = rel + len(seg.payload)
            already = 0
            pos = rel
            while pos < end:
                start = filled.find(1, pos, end)
                if start == -1:
                    start = end
                if start > pos:
                    data[pos:start] = seg.payload[pos - rel : start - rel]
                    filled[pos:start] = b"\x01" * (start - pos)
                    self.offset_map.append(
                        (pos, start - pos, seg.ts, seg.frame)
                    )
                if start == end:
                    break
                stop = filled.find(0, start, end)
                if stop == -1:
                    stop = end
                already += stop - start
                if data[start:stop] != seg.payload[start - rel : stop - rel]:
                    self.conflicting_retransmissions += 1
                pos = stop
            if already:
                self.overlapping_segments += 1
                self.duplicate_bytes += already

        self.data = bytes(data)
        cursor = 0
        while cursor < limit:
            start = filled.find(0, cursor)
            if start == -1:
                break
            stop = filled.find(1, start)
            if stop == -1:
                stop = limit
            self.gaps.append(
                Gap(start, stop - start, "sequence gap in capture")
            )
            cursor = stop

        # Entries never overlap (each covers previously unfilled bytes);
        # sorting makes offset lookups linear and correct.
        self.offset_map.sort(key=lambda item: item[0])

    def ts_at(self, offset):
        """Timestamp of the segment carrying ``offset`` (or nearest before)."""

        if not self.offset_map:
            return self.first_ts
        best = self.first_ts
        for start, length, ts, _frame in self.offset_map:
            if start > offset:
                break
            if start <= offset < start + length:
                return ts
            best = ts
        return best

    def frame_at(self, offset):
        if not self.offset_map:
            return None
        best = None
        for start, length, _ts, frame in self.offset_map:
            if start > offset:
                break
            best = frame
        return best


@dataclass
class Connection:
    """A single TCP connection instance."""

    cid: int
    first_ts: float
    initiator: tuple
    responder: tuple
    midstream: bool
    directions: dict = field(default_factory=dict)
    close_ts: float = None
    close_reason: str = "capture-end"

    @property
    def endpoints(self):
        return {self.initiator, self.responder}

    @property
    def fwd(self):
        """Initiator to responder bytes."""

        return self.directions.get("fwd")

    @property
    def rev(self):
        """Responder to initiator bytes."""

        return self.directions.get("rev")

    def direction_name(self, src):
        return "fwd" if src == self.initiator else "rev"

    def direction(self, name):
        return self.directions.get(name)

    def flags_summary(self):
        flags = []
        for direction in self.directions.values():
            if direction.syn_seen:
                flags.append("SYN")
            if direction.fin_seen:
                flags.append("FIN")
            if direction.rst_seen:
                flags.append("RST")
        return sorted(set(flags))


def _strip_link(frame):
    data = frame.data
    linktype = frame.linktype
    if linktype == LINKTYPE_ETHERNET:
        if len(data) < 14:
            raise DecodeError("short ethernet header")
        ethertype = struct.unpack("!H", data[12:14])[0]
        offset = 14
        while ethertype in (_ETHERTYPE_VLAN, _ETHERTYPE_QINQ):
            if len(data) < offset + 4:
                raise DecodeError("short vlan header")
            ethertype = struct.unpack("!H", data[offset + 2 : offset + 4])[0]
            offset += 4
        return ethertype, data[offset:]
    if linktype == LINKTYPE_LINUX_SLL:
        if len(data) < 16:
            raise DecodeError("short linux sll header")
        return struct.unpack("!H", data[14:16])[0], data[16:]
    if linktype == LINKTYPE_LINUX_SLL2:
        if len(data) < 20:
            raise DecodeError("short linux sll2 header")
        return struct.unpack("!H", data[0:2])[0], data[20:]
    if linktype in (LINKTYPE_NULL, LINKTYPE_LOOP):
        if len(data) < 4:
            raise DecodeError("short loopback header")
        family = struct.unpack("<I", data[:4])[0]
        if family == 2:
            return ETHERTYPE_IPV4, data[4:]
        if family in (24, 28, 30):
            return ETHERTYPE_IPV6, data[4:]
        # BSD loopback may store the family in host byte order.
        family = struct.unpack(">I", data[:4])[0]
        if family == 2:
            return ETHERTYPE_IPV4, data[4:]
        raise DecodeError(f"unsupported loopback family {family}")
    if linktype == LINKTYPE_RAW:
        if not data:
            raise DecodeError("empty raw frame")
        version = data[0] >> 4
        if version == 4:
            return ETHERTYPE_IPV4, data
        if version == 6:
            return ETHERTYPE_IPV6, data
        raise DecodeError("raw frame is neither IPv4 nor IPv6")
    raise DecodeError(f"unsupported link type {linktype}")


def _decode_ipv4(data):
    if len(data) < 20:
        raise DecodeError("short ipv4 header")
    ihl = (data[0] & 0x0F) * 4
    if ihl < 20 or len(data) < ihl:
        raise DecodeError("bad ipv4 header length")
    total_len = struct.unpack("!H", data[2:4])[0]
    frag = struct.unpack("!H", data[6:8])[0]
    proto = data[9]
    src = ".".join(str(b) for b in data[12:16])
    dst = ".".join(str(b) for b in data[16:20])
    if frag & 0x2000 or frag & 0x1FFF:
        raise DecodeError("ipv4 fragment not supported")
    if total_len and total_len < ihl:
        raise DecodeError("bad ipv4 total length")
    payload = data[ihl:total_len] if total_len else data[ihl:]
    return src, dst, proto, payload


def _decode_ipv6(data):
    if len(data) < 40:
        raise DecodeError("short ipv6 header")
    payload_len = struct.unpack("!H", data[4:6])[0]
    next_header = data[6]
    src = _ipv6_str(data[8:24])
    dst = _ipv6_str(data[24:40])
    offset = 40
    end = 40 + payload_len if payload_len else len(data)
    while next_header in (0, 43, 44, 51, 60):
        if next_header == 44:
            if len(data) < offset + 8:
                raise DecodeError("short ipv6 fragment header")
            frag = struct.unpack("!H", data[offset + 2 : offset + 4])[0]
            if frag & 0xFFF9:
                raise DecodeError("ipv6 fragment not supported")
        if len(data) < offset + 2:
            raise DecodeError("short ipv6 extension header")
        next_header = data[offset]
        offset += (data[offset + 1] + 1) * 8
    return src, dst, next_header, data[offset:end]


def _ipv6_str(raw):
    words = struct.unpack("!8H", raw)
    best_start, best_len, start = -1, 0, None
    for i, word in enumerate(words + (1,)):
        if word == 0 and start is None:
            start = i
        elif word != 0 and start is not None:
            if i - start > best_len:
                best_start, best_len = start, i - start
            start = None
    parts = [f"{w:x}" for w in words]
    if best_len >= 2:
        head = ":".join(parts[:best_start])
        tail = ":".join(parts[best_start + best_len :])
        return head + "::" + tail
    return ":".join(parts)


def _decode_tcp(data):
    if len(data) < 20:
        raise DecodeError("short tcp header")
    sport, dport, seq, ack, offset_flags = struct.unpack("!HHIIH", data[:14])
    header_len = (offset_flags >> 12) * 4
    if header_len < 20 or len(data) < header_len:
        raise DecodeError("bad tcp header length")
    flags = offset_flags & 0x3F
    return sport, dport, seq, ack, flags, data[header_len:]


def decode_frame(frame):
    """Decode one captured frame into a :class:`Segment` or ``None``."""

    ethertype, l3 = _strip_link(frame)
    if ethertype == ETHERTYPE_IPV4:
        src, dst, proto, payload = _decode_ipv4(l3)
    elif ethertype == ETHERTYPE_IPV6:
        src, dst, proto, payload = _decode_ipv6(l3)
    else:
        return None
    if proto != 6:
        return None
    sport, dport, seq, ack, flags, tcp_payload = _decode_tcp(payload)
    if not tcp_payload and not flags:
        return None
    return Segment(
        ts=frame.ts,
        frame=frame.index,
        src=(src, sport),
        dst=(dst, dport),
        seq=seq,
        ack=ack,
        flags=flags,
        payload=tcp_payload,
        truncated=frame.truncated,
    )


class ConnectionTable:
    """Builds connection instances from a stream of TCP segments."""

    def __init__(self):
        self.connections = []
        self._open = {}
        self._next_cid = 1
        self.unordered_frames = 0
        self._last_ts = None

    def feed(self, segment):
        key = frozenset((segment.src, segment.dst))
        conn = self._open.get(key)
        starts = segment.syn and not segment.flags & TCP_ACK

        if starts and conn is not None:
            # A fresh SYN on a live 4-tuple is a reused port, not more
            # traffic for the old connection instance.
            if conn.close_ts is None:
                conn.close_ts = segment.ts
                conn.close_reason = "port-reuse"
            conn = None

        if (
            conn is not None
            and conn.close_ts is not None
            and segment.payload
            and segment.ts > conn.close_ts
        ):
            # Payload after a completed close means the port was reused by
            # a new connection whose handshake was not captured.
            conn = None

        if conn is None:
            if starts:
                initiator = segment.src
            else:
                initiator = self._guess_initiator(segment)
            responder = (
                segment.dst if initiator == segment.src else segment.src
            )
            conn = Connection(
                cid=self._next_cid,
                first_ts=segment.ts,
                initiator=initiator,
                responder=responder,
                midstream=not starts,
            )
            self._next_cid += 1
            self.connections.append(conn)
            self._open[key] = conn

        name = conn.direction_name(segment.src)
        if name not in conn.directions:
            conn.directions[name] = Direction(src=segment.src, dst=segment.dst)
        conn.directions[name].add(segment)

        if segment.rst and conn.close_ts is None:
            conn.close_ts = segment.ts
            conn.close_reason = "rst"
        elif segment.fin and conn.close_ts is None:
            both_closed = all(
                d.fin_seen or d.rst_seen for d in conn.directions.values()
            )
            if both_closed:
                conn.close_ts = segment.ts
                conn.close_reason = "fin"
        if self._last_ts is not None and segment.ts < self._last_ts:
            self.unordered_frames += 1
        self._last_ts = segment.ts

    @staticmethod
    def _guess_initiator(segment):
        sport, dport = segment.src[1], segment.dst[1]
        if sport < dport:
            return segment.src
        if dport < sport:
            return segment.dst
        return segment.src

    def finish(self):
        for conn in self.connections:
            for direction in conn.directions.values():
                direction.reassemble()
            if conn.close_ts is None and conn.directions:
                conn.close_ts = max(
                    d.last_ts for d in conn.directions.values() if d.last_ts
                )


def build_connections(frames):
    """Decode frames and return ``(connections, stats)``."""

    table = ConnectionTable()
    stats = {
        "frames_total": 0,
        "frames_decoded": 0,
        "frames_ignored": 0,
        "frames_truncated": 0,
        "decode_error_samples": [],
        "unordered_frames": 0,
        "connections": 0,
    }
    for frame in frames:
        stats["frames_total"] += 1
        if frame.truncated:
            stats["frames_truncated"] += 1
        try:
            segment = decode_frame(frame)
        except DecodeError as err:
            stats["frames_ignored"] += 1
            if len(stats["decode_error_samples"]) < 5:
                stats["decode_error_samples"].append(
                    {"frame": frame.index, "reason": str(err)}
                )
            continue
        if segment is None:
            stats["frames_ignored"] += 1
            continue
        stats["frames_decoded"] += 1
        table.feed(segment)

    table.finish()
    stats["unordered_frames"] = table.unordered_frames
    stats["connections"] = len(table.connections)
    return table.connections, stats
