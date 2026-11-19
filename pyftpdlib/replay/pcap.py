# Copyright (C) 2007 Giampaolo Rodola' <g.rodola@gmail.com>.
# Use of this source code is governed by MIT license that can be
# found in the LICENSE file.

"""Minimal, dependency-free pcap / pcapng reader for FTP replay.

Only the parts needed to rebuild TCP streams are implemented: classic
pcap (microsecond and nanosecond variants), pcapng (SHB/IDB/EPB), and
Ethernet, Linux cooked, loopback, raw IPv4 and raw IPv6 link types.
IPv4 fragments other than the first are skipped with a warning because
their TCP payload cannot be interpreted on its own.
"""

import struct
from dataclasses import dataclass, field
from typing import List

__all__ = [
    "CaptureError",
    "Capture",
    "Packet",
    "TcpSegment",
    "read_capture",
]

PCAP_MAGIC_US = 0xA1B2C3D4
PCAP_MAGIC_NS = 0xA1B23C4D
PCAPNG_SHB = 0x0A0D0D0A

LINKTYPE_NULL = 0
LINKTYPE_ETHERNET = 1
LINKTYPE_RAW = 101
LINKTYPE_LOOP = 108
LINKTYPE_LINUX_SLL = 113
LINKTYPE_IPV4 = 228
LINKTYPE_IPV6 = 229


class CaptureError(ValueError):
    """Raised when a capture file cannot be parsed."""


@dataclass
class Packet:
    ts: float
    data: bytes
    linktype: int


@dataclass
class Capture:
    packets: List[Packet] = field(default_factory=list)
    linktype: int = LINKTYPE_ETHERNET
    warnings: List[str] = field(default_factory=list)

    def __len__(self):
        return len(self.packets)


@dataclass
class TcpSegment:
    ts: float
    src: str
    sport: int
    dst: str
    dport: int
    seq: int
    ack: int
    payload: bytes
    flags: int
    payload_offset: int

    @property
    def syn(self):
        return bool(self.flags & 0x02)

    @property
    def fin(self):
        return bool(self.flags & 0x01)

    @property
    def rst(self):
        return bool(self.flags & 0x04)

    @property
    def key(self):
        return (self.src, self.sport, self.dst, self.dport)


def _u32(data, offset, endian):
    return struct.unpack_from(endian + "I", data, offset)[0]


def _read_pcap(data):
    if len(data) < 24:
        raise CaptureError("truncated pcap header")
    magic = struct.unpack_from("<I", data, 0)[0]
    if magic in (PCAP_MAGIC_US, PCAP_MAGIC_NS):
        endian = "<"
        nano = magic == PCAP_MAGIC_NS
    else:
        magic = struct.unpack_from(">I", data, 0)[0]
        if magic in (PCAP_MAGIC_US, PCAP_MAGIC_NS):
            endian = ">"
            nano = magic == PCAP_MAGIC_NS
        else:
            raise CaptureError("not a pcap file")
    linktype = _u32(data, 20, endian)
    capture = Capture(linktype=linktype)
    offset = 24
    while offset + 16 <= len(data):
        ts_sec, ts_frac, incl_len, _orig_len = struct.unpack_from(
            endian + "IIII", data, offset
        )
        offset += 16
        if offset + incl_len > len(data):
            capture.warnings.append(
                f"truncated packet at offset {offset - 16}"
            )
            break
        scale = 1e9 if nano else 1e6
        capture.packets.append(
            Packet(
                ts=ts_sec + ts_frac / scale,
                data=data[offset : offset + incl_len],
                linktype=linktype,
            )
        )
        offset += incl_len
    return capture


def _read_pcapng(data):
    capture = Capture()
    offset = 0
    interfaces = []
    endian = "<"
    while offset + 12 <= len(data):
        block_type = struct.unpack_from("<I", data, offset)[0]
        if block_type == PCAPNG_SHB:
            if offset + 12 > len(data):
                break
            bom = data[offset + 8 : offset + 12]
            if bom == b"\x4d\x3c\x2b\x1a":
                endian = "<"
            elif bom == b"\x1a\x2b\x3c\x4d":
                endian = ">"
            else:
                raise CaptureError("invalid pcapng byte-order magic")
        block_type = struct.unpack_from(endian + "I", data, offset)[0]
        block_len = struct.unpack_from(endian + "I", data, offset + 4)[0]
        if block_len < 12 or offset + block_len > len(data):
            capture.warnings.append(
                f"truncated pcapng block at offset {offset}"
            )
            break
        body = data[offset + 8 : offset + block_len - 4]
        if block_type == 0x00000001:  # IDB
            if len(body) < 8:
                raise CaptureError("truncated pcapng interface block")
            linktype = struct.unpack_from(endian + "H", body, 0)[0]
            tsresol = 6
            pos = 8
            while pos + 4 <= len(body):
                option_code, option_len = struct.unpack_from(
                    endian + "HH", body, pos
                )
                pos += 4
                if option_code == 0:
                    break
                value = body[pos : pos + option_len]
                if option_code == 9 and value:
                    tsresol = value[0]
                    if tsresol & 0x80:
                        tsresol = 1 << (tsresol & 0x7F)
                    else:
                        tsresol = 10**tsresol
                pos += option_len + ((4 - option_len % 4) % 4)
            interfaces.append((linktype, tsresol))
        elif block_type == 0x00000006:  # EPB
            if len(body) < 20:
                raise CaptureError("truncated pcapng packet block")
            iface_id, ts_high, ts_low, caplen, _origlen = struct.unpack_from(
                endian + "IIIII", body, 0
            )
            if iface_id >= len(interfaces):
                capture.warnings.append(
                    f"packet references unknown interface {iface_id}"
                )
            else:
                linktype, tsresol = interfaces[iface_id]
                ts = ((ts_high << 32) | ts_low) / tsresol
                payload = body[20 : 20 + caplen]
                capture.packets.append(
                    Packet(ts=ts, data=payload, linktype=linktype)
                )
        offset += block_len
    if not interfaces:
        raise CaptureError("pcapng has no interface description block")
    capture.linktype = interfaces[0][0]
    return capture


def read_capture(path):
    """Read a pcap or pcapng file into a :class:`Capture`."""
    with open(path, "rb") as file:
        data = file.read()
    if len(data) < 4:
        raise CaptureError("capture file is empty")
    magic = struct.unpack_from("<I", data, 0)[0]
    if magic == PCAPNG_SHB:
        return _read_pcapng(data)
    return _read_pcap(data)


def _strip_link_layer(packet):
    data = packet.data
    linktype = packet.linktype
    if linktype == LINKTYPE_ETHERNET:
        if len(data) < 14:
            return None
        ethertype = struct.unpack_from(">H", data, 12)[0]
        offset = 14
        while ethertype in (0x8100, 0x88A8, 0x9100):
            if len(data) < offset + 4:
                return None
            ethertype = struct.unpack_from(">H", data, offset + 2)[0]
            offset += 4
        return ethertype, data[offset:]
    if linktype == LINKTYPE_LINUX_SLL:
        if len(data) < 16:
            return None
        ethertype = struct.unpack_from(">H", data, 14)[0]
        return ethertype, data[16:]
    if linktype in (LINKTYPE_NULL, LINKTYPE_LOOP):
        if len(data) < 4:
            return None
        family = struct.unpack_from("<I", data, 0)[0]
        if family in (2,):
            return 0x0800, data[4:]
        if family in (24, 28, 30):
            return 0x86DD, data[4:]
        # BSD loopback stores the address family in host byte order.
        family = struct.unpack_from(">I", data, 0)[0]
        if family == 2:
            return 0x0800, data[4:]
        if family in (24, 28, 30):
            return 0x86DD, data[4:]
        return None
    if linktype == LINKTYPE_RAW:
        if not data:
            return None
        version = data[0] >> 4
        if version == 4:
            return 0x0800, data
        if version == 6:
            return 0x86DD, data
        return None
    if linktype == LINKTYPE_IPV4:
        return 0x0800, data
    if linktype == LINKTYPE_IPV6:
        return 0x86DD, data
    return None


def _parse_tcp(packet, ip_payload, ethertype):
    if ethertype == 0x0800:
        if len(ip_payload) < 20:
            return None
        version = ip_payload[0] >> 4
        if version != 4:
            return None
        ihl = (ip_payload[0] & 0x0F) * 4
        if ihl < 20 or len(ip_payload) < ihl:
            return None
        total_len = struct.unpack_from(">H", ip_payload, 2)[0]
        flags_frag = struct.unpack_from(">H", ip_payload, 6)[0]
        frag_offset = flags_frag & 0x1FFF
        more_fragments = bool(flags_frag & 0x2000)
        if frag_offset or more_fragments:
            return None
        protocol = ip_payload[9]
        if protocol != 6:
            return None
        src = ".".join(str(b) for b in ip_payload[12:16])
        dst = ".".join(str(b) for b in ip_payload[16:20])
        end = total_len if total_len >= ihl else len(ip_payload)
        transport = ip_payload[ihl:end]
    elif ethertype == 0x86DD:
        if len(ip_payload) < 40:
            return None
        if ip_payload[0] >> 4 != 6:
            return None
        next_header = ip_payload[6]
        if next_header != 6:
            return None
        src = _ipv6(ip_payload[8:24])
        dst = _ipv6(ip_payload[24:40])
        payload_len = struct.unpack_from(">H", ip_payload, 4)[0]
        transport = ip_payload[40 : 40 + payload_len]
    else:
        return None

    if len(transport) < 20:
        return None
    sport, dport = struct.unpack_from(">HH", transport, 0)
    seq, ack = struct.unpack_from(">II", transport, 4)
    data_offset = (transport[12] >> 4) * 4
    if data_offset < 20 or len(transport) < data_offset:
        return None
    flags = transport[13]
    payload = transport[data_offset:]
    if flags & 0x02:  # SYN consumes one sequence number
        payload_start = (seq + 1) & 0xFFFFFFFF
    else:
        payload_start = seq
    return TcpSegment(
        ts=packet.ts,
        src=src,
        sport=sport,
        dst=dst,
        dport=dport,
        seq=seq,
        ack=ack,
        payload=payload,
        flags=flags,
        payload_offset=payload_start,
    )


def _ipv6(raw):
    parts = struct.unpack(">8H", raw)
    return ":".join(f"{part:x}" for part in parts)


def iter_tcp_segments(capture):
    """Yield TCP segments, warning about undecodable packets."""
    for packet in capture.packets:
        stripped = _strip_link_layer(packet)
        if stripped is None:
            continue
        ethertype, payload = stripped
        segment = _parse_tcp(packet, payload, ethertype)
        if segment is not None:
            yield segment


def read_segments(path):
    """Read a capture and return ``(segments, warnings)``."""
    capture = read_capture(path)
    return list(iter_tcp_segments(capture)), capture.warnings
