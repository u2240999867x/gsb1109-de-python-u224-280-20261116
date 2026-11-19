"""Capture file readers for classic pcap and pcapng.

Only the pieces needed by the FTP correlator are implemented: frame
timestamps, per-interface link types and raw frame bytes.  Parsing is
strict about truncation because a silently short frame would make the
byte accounting wrong later on.
"""

import struct
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone

PCAP_MAGIC_LE = 0xA1B2C3D4
PCAP_MAGIC_BE = 0xD4C3B2A1
PCAP_MAGIC_NS_LE = 0xA1B23C4D
PCAP_MAGIC_NS_BE = 0x4D3CB2A1
PCAPNG_MAGIC = 0x0A0D0D0A

LINKTYPE_NULL = 0
LINKTYPE_ETHERNET = 1
LINKTYPE_RAW = 101
LINKTYPE_LOOP = 108
LINKTYPE_LINUX_SLL = 113
LINKTYPE_LINUX_SLL2 = 276

ETHERTYPE_IPV4 = 0x0800
ETHERTYPE_IPV6 = 0x86DD

PCAPNG_SHB = 0x0A0D0D0A
PCAPNG_IDB = 0x00000001
PCAPNG_OPB = 0x00000002
PCAPNG_SPB = 0x00000003
PCAPNG_EPB = 0x00000006


class CaptureError(Exception):
    """Raised when a capture file cannot be parsed."""


@dataclass
class Frame:
    """One captured frame in file order."""

    index: int
    ts: float
    linktype: int
    interface: int
    data: bytes
    wirelen: int

    @property
    def truncated(self):
        # Snap length shorter than the original frame means payload bytes
        # are missing, which makes any later byte count a lower bound only.
        return len(self.data) < self.wirelen

    @property
    def timestamp(self):
        return datetime.fromtimestamp(self.ts, tz=timezone.utc)


class _Reader:
    def __init__(self, stream, path):
        self.stream = stream
        self.path = path

    def read(self, size):
        data = self.stream.read(size)
        if len(data) != size:
            raise CaptureError(
                f"{self.path}: truncated capture, wanted {size} bytes, "
                f"got {len(data)}"
            )
        return data

    def maybe_read(self, size):
        return self.stream.read(size)


def _pcap_frames(reader):
    header = reader.read(24)
    magic = struct.unpack("<I", header[:4])[0]
    if magic in (PCAP_MAGIC_LE, PCAP_MAGIC_NS_LE):
        endian = "<"
        nanoseconds = magic == PCAP_MAGIC_NS_LE
    elif magic in (PCAP_MAGIC_BE, PCAP_MAGIC_NS_BE):
        endian = ">"
        nanoseconds = magic == PCAP_MAGIC_NS_BE
    else:
        raise CaptureError(f"{reader.path}: not a pcap file")
    linktype = struct.unpack(endian + "HHiIII", header[4:24])[5] & 0xFFFF
    scale = 1e-9 if nanoseconds else 1e-6
    index = 0
    while True:
        record = reader.maybe_read(16)
        if not record:
            return
        if len(record) != 16:
            raise CaptureError(f"{reader.path}: truncated packet header")
        sec, frac, caplen, wirelen = struct.unpack(endian + "IIII", record)
        if frac >= (1_000_000_000 if nanoseconds else 1_000_000):
            raise CaptureError(f"{reader.path}: invalid timestamp fraction")
        payload = reader.read(caplen)
        yield Frame(
            index=index,
            ts=sec + frac * scale,
            linktype=linktype,
            interface=0,
            data=payload,
            wirelen=max(wirelen, caplen),
        )
        index += 1


def _tsresol_scale(value):
    if value & 0x80:
        return 2.0 ** -(value & 0x7F)
    return 10.0 ** -value


def _pcapng_frames(reader):
    interfaces = []
    index = 0
    endian = "<"
    while True:
        head = reader.maybe_read(12)
        if not head:
            return
        if len(head) < 12:
            raise CaptureError(f"{reader.path}: truncated block header")
        block_type = struct.unpack(endian + "I", head[:4])[0]
        big_endian_shb = struct.unpack(">I", head[:4])[0] == PCAPNG_SHB
        if block_type == PCAPNG_SHB or big_endian_shb:
            bom = struct.unpack("<I", head[8:12])[0]
            if bom == 0x1A2B3C4D:
                endian = "<"
            elif bom == 0x4D3C2B1A:
                endian = ">"
            else:
                raise CaptureError(f"{reader.path}: bad pcapng byte order")
            block_len = struct.unpack(endian + "I", head[4:8])[0]
            if block_len < 12 or block_len % 4:
                raise CaptureError(f"{reader.path}: bad section header length")
            reader.read(block_len - 12)
            interfaces = []
            continue

        block_len = struct.unpack(endian + "I", head[4:8])[0]
        if block_len < 12 or block_len % 4:
            raise CaptureError(
                f"{reader.path}: invalid pcapng block length {block_len}"
            )
        rest = reader.read(block_len - 12)
        trailing_length = struct.unpack(endian + "I", rest[-4:])[0]
        if trailing_length != block_len:
            raise CaptureError(f"{reader.path}: pcapng block length mismatch")
        # The first four body bytes arrived with the 12-byte header.
        body = head[8:12] + rest[:-4]

        if block_type == PCAPNG_IDB:
            linktype = struct.unpack(endian + "H", body[:2])[0]
            tsresol = 6
            offset = 8
            while offset + 4 <= len(body):
                code, length = struct.unpack(
                    endian + "HH", body[offset : offset + 4]
                )
                value = body[offset + 4 : offset + 4 + length]
                if code == 9 and length >= 1:
                    tsresol = value[0]
                if code == 0:
                    break
                offset += 4 + length + (-length % 4)
            interfaces.append((linktype, _tsresol_scale(tsresol)))
        elif block_type == PCAPNG_EPB:
            if len(body) < 20:
                raise CaptureError(f"{reader.path}: short enhanced packet")
            iface, high, low, caplen, wirelen = struct.unpack(
                endian + "IIIII", body[:20]
            )
            payload = body[20 : 20 + caplen]
            if len(payload) != caplen:
                raise CaptureError(f"{reader.path}: truncated captured frame")
            if iface >= len(interfaces):
                raise CaptureError(
                    f"{reader.path}: frame refers to unknown interface {iface}"
                )
            linktype, scale = interfaces[iface]
            yield Frame(
                index=index,
                ts=((high << 32) | low) * scale,
                linktype=linktype,
                interface=iface,
                data=payload,
                wirelen=max(wirelen, caplen),
            )
            index += 1
        elif block_type == PCAPNG_SPB:
            if not interfaces:
                raise CaptureError(f"{reader.path}: SPB before any interface")
            wirelen, caplen = struct.unpack(endian + "II", body[:8])
            payload = body[8 : 8 + caplen]
            linktype, scale = interfaces[0]
            yield Frame(
                index=index,
                ts=0.0,
                linktype=linktype,
                interface=0,
                data=payload,
                wirelen=max(wirelen, caplen),
            )
            index += 1
        elif block_type == PCAPNG_OPB:
            if len(body) < 12:
                raise CaptureError(f"{reader.path}: short original packet")
            iface, high, low = struct.unpack(endian + "III", body[:12])
            payload = body[12:]
            linktype, scale = interfaces[iface]
            yield Frame(
                index=index,
                ts=((high << 32) | low) * scale,
                linktype=linktype,
                interface=iface,
                data=payload,
                wirelen=len(payload),
            )
            index += 1


def read_capture(path):
    """Yield :class:`Frame` objects from a pcap or pcapng file."""

    with open(path, "rb") as stream:
        magic = stream.read(4)
        if len(magic) < 4:
            raise CaptureError(f"{path}: file is too short to be a capture")
        stream.seek(0)
        if struct.unpack("<I", magic)[0] == PCAPNG_MAGIC:
            yield from _pcapng_frames(_Reader(stream, path))
        else:
            yield from _pcap_frames(_Reader(stream, path))


def write_pcap(path, frames, linktype=LINKTYPE_ETHERNET):
    """Write ``(timestamp, raw bytes)`` pairs as a little-endian pcap."""

    with open(path, "wb") as stream:
        stream.write(
            struct.pack(
                "<IHHiIII", PCAP_MAGIC_LE, 2, 4, 0, 0, 262144, linktype
            )
        )
        for ts, data in frames:
            sec = int(ts)
            usec = int(round((ts - sec) * 1_000_000))
            if usec >= 1_000_000:
                sec += 1
                usec -= 1_000_000
            stream.write(
                struct.pack("<IIII", sec, usec, len(data), len(data))
            )
            stream.write(data)
