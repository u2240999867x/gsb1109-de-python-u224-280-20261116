# Copyright (C) 2007 Giampaolo Rodola' <g.rodola@gmail.com>.
# Use of this source code is governed by MIT license that can be
# found in the LICENSE file.

"""Turn a TCP capture into normalized replay events.

The adapter is intentionally conservative.  It identifies FTP control
connections by looking at actual command/reply text, then assigns data
connections to transfer windows using advertised passive/active ports
and timing.  A connection it cannot attribute is left out instead of
being forced into a transfer.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from .events import Event
from .pcap import read_segments

__all__ = [
    "CaptureReplay",
    "Connection",
    "build_events",
    "replay_capture",
]

_REPLY_START = re.compile(r"^[1-5]\d\d[ -]")
_COMMAND_START = re.compile(r"^[A-Za-z]{3,5}(?:[ ][^\r\n]*)?$")
_FTP_COMMANDS = {
    "ABOR",
    "ALLO",
    "APPE",
    "CDUP",
    "CWD",
    "DELE",
    "EPRT",
    "EPSV",
    "FEAT",
    "HELP",
    "LIST",
    "MDTM",
    "MFMT",
    "MKD",
    "MLSD",
    "MLST",
    "MODE",
    "NLST",
    "NOOP",
    "OPTS",
    "PASS",
    "PASV",
    "PORT",
    "PWD",
    "QUIT",
    "REIN",
    "REST",
    "RETR",
    "RMD",
    "RNFR",
    "RNTO",
    "SITE",
    "SIZE",
    "STAT",
    "STOR",
    "STOU",
    "STRU",
    "SYST",
    "TYPE",
    "USER",
    "XCUP",
    "XCWD",
    "XMKD",
    "XPWD",
    "XRMD",
}
TRANSFER_COMMANDS = {"RETR", "STOR", "STOU", "APPE", "LIST", "NLST", "MLSD"}
TRANSFER_TERMINAL = {
    226,
    250,
    425,
    426,
    450,
    451,
    452,
    550,
    551,
    552,
    553,
    554,
}

_PASV_RE = re.compile(r"\((\d+),(\d+),(\d+),(\d+),(\d+),(\d+)\)")
_EPSV_RE = re.compile(r"\(\|\|\|(\d+)\|\)")
_PORT_RE = re.compile(r"^(\d+),(\d+),(\d+),(\d+),(\d+),(\d+)$")


@dataclass
class Connection:
    id: str
    client: Tuple[str, int]
    server: Tuple[str, int]
    first_ts: float
    last_ts: float
    c2s: list = field(default_factory=list)
    s2c: list = field(default_factory=list)
    fin_c2s: bool = False
    fin_s2c: bool = False
    rst_c2s: bool = False
    rst_s2c: bool = False
    payload_c2s: int = 0
    payload_s2c: int = 0
    close_ts: Dict[str, float] = field(default_factory=dict)

    @property
    def key(self):
        return (self.client, self.server)


def _conn_key(segment):
    a = (segment.src, segment.sport)
    b = (segment.dst, segment.dport)
    return frozenset((a, b))


def _collect_connections(segments):
    conns = {}
    for segment in segments:
        key = _conn_key(segment)
        conn = conns.get(key)
        if conn is None:
            src = (segment.src, segment.sport)
            dst = (segment.dst, segment.dport)
            conn = Connection(
                id="",
                client=src,
                server=dst,
                first_ts=segment.ts,
                last_ts=segment.ts,
            )
            conns[key] = conn
        conn.first_ts = min(conn.first_ts, segment.ts)
        conn.last_ts = max(conn.last_ts, segment.ts)
        forward = (segment.src, segment.sport) == conn.client
        if forward:
            conn.c2s.append(segment)
            conn.payload_c2s += len(segment.payload)
            if segment.fin:
                conn.fin_c2s = True
                conn.close_ts.setdefault("client_fin", segment.ts)
            if segment.rst:
                conn.rst_c2s = True
                conn.close_ts.setdefault("client_reset", segment.ts)
        else:
            conn.s2c.append(segment)
            conn.payload_s2c += len(segment.payload)
            if segment.fin:
                conn.fin_s2c = True
                conn.close_ts.setdefault("server_fin", segment.ts)
            if segment.rst:
                conn.rst_s2c = True
                conn.close_ts.setdefault("server_reset", segment.ts)
    return list(conns.values())


def _swap_connection(conn):
    conn.client, conn.server = conn.server, conn.client
    conn.c2s, conn.s2c = conn.s2c, conn.c2s
    conn.payload_c2s, conn.payload_s2c = (
        conn.payload_s2c,
        conn.payload_c2s,
    )
    conn.fin_c2s, conn.fin_s2c = conn.fin_s2c, conn.fin_c2s
    conn.rst_c2s, conn.rst_s2c = conn.rst_s2c, conn.rst_c2s
    remap = {
        "client_fin": "server_fin",
        "client_reset": "server_reset",
        "server_fin": "client_fin",
        "server_reset": "client_reset",
    }
    conn.close_ts = {
        remap.get(key, key): value for key, value in conn.close_ts.items()
    }


def _orient(conn, server_ip):
    """Make ``conn.server`` the endpoint belonging to the FTP server."""
    if conn.client[0] == server_ip and conn.server[0] != server_ip:
        _swap_connection(conn)


def _reassemble(segments):
    """Return ``[(offset, ts, data)]`` in sequence order.

    Offsets are relative to the first payload byte.  Retransmitted
    bytes keep the timestamp of their first occurrence.
    """
    payloads = [seg for seg in segments if seg.payload]
    if not payloads:
        return []
    first_seq = min(seg.payload_offset for seg in payloads)
    pieces = []
    for segment in payloads:
        rel = (segment.payload_offset - first_seq) % (1 << 32)
        pieces.append((rel, segment.ts, segment.payload))
    pieces.sort(key=lambda row: (row[0], row[1]))
    return pieces


def _text_stream(pieces):
    """Assemble contiguous text and return ``[(ts, text)]`` lines.

    Retransmitted bytes are written once, keeping the earliest copy.
    A capture gap terminates the current line; lines are never joined
    across missing bytes.
    """
    if not pieces:
        return []
    start = min(offset for offset, _ts, _data in pieces)
    end = max(offset + len(data) for offset, _ts, data in pieces)
    buffer = bytearray(b"\x00" * (end - start))
    src_ts = [None] * (end - start)
    for offset, ts, data in sorted(pieces, key=lambda row: (row[1], row[0])):
        rel = offset - start
        for index in range(len(data)):
            pos = rel + index
            if src_ts[pos] is None:
                src_ts[pos] = ts
                buffer[pos] = data[index]

    lines = []
    line_start = None
    for pos in range(len(buffer)):
        ts = src_ts[pos]
        if ts is None:
            if line_start is not None:
                lines.append(
                    (
                        src_ts[line_start],
                        bytes(buffer[line_start:pos]).decode(
                            "utf-8", "replace"
                        ),
                    )
                )
            line_start = None
            continue
        if line_start is None:
            line_start = pos
        if (
            buffer[pos] == 0x0A
            and pos > line_start
            and buffer[pos - 1] == 0x0D
        ):
            lines.append(
                (
                    src_ts[line_start],
                    bytes(buffer[line_start : pos - 1]).decode(
                        "utf-8", "replace"
                    ),
                )
            )
            line_start = None
    if line_start is not None:
        _append_line(lines, buffer, src_ts, line_start, len(buffer))
    return lines


def _append_line(lines, buffer, src_ts, start, end):
    tail = bytes(buffer[start:end]).decode("utf-8", "replace")
    if tail.strip():
        lines.append((src_ts[start], tail))


def _looks_like_ftp(conn):
    lines = _text_stream(_reassemble(conn.c2s))
    commands = 0
    for _ts, line in lines:
        name = line.strip().split(" ", 1)[0].upper()
        if name in _FTP_COMMANDS:
            commands += 1
    replies = _text_stream(_reassemble(conn.s2c))
    reply_lines = sum(1 for _ts, line in replies if _REPLY_START.match(line))
    return commands, reply_lines


def _guess_server(conns):
    # The control connection's server port is the lowest well-known
    # port among the flows; fall back to the port with most reply text.
    best = None
    for conn in conns:
        commands, replies = _looks_like_ftp(conn)
        if commands == 0 and replies == 0:
            continue
        score = commands * 2 + replies
        if conn.server[1] in (21, 2121):
            score += 10
        if best is None or score > best[0]:
            best = (score, conn)
    return best[1] if best else None


def _parse_control(conn, session):
    events = []
    for ts, line in _text_stream(_reassemble(conn.c2s)):
        line = line.strip()
        if not line:
            continue
        name = line.split(" ", 1)[0].upper()
        if name not in _FTP_COMMANDS and not name.startswith("SITE"):
            continue
        events.append(
            Event(
                ts=ts,
                channel="control",
                direction="c2s",
                kind="command",
                data=line,
                session=session,
                line=len(events) + 1,
            )
        )
    for ts, line in _text_stream(_reassemble(conn.s2c)):
        line = line.strip()
        if not line:
            continue
        if not _REPLY_START.match(line):
            continue
        events.append(
            Event(
                ts=ts,
                channel="control",
                direction="s2c",
                kind="reply",
                data=line,
                session=session,
                line=len(events) + 1,
            )
        )
    return events


def _transfer_windows(control_events):
    """Return ``[(start_ts, end_ts, command_event)]`` for transfers."""
    windows = []
    current = None
    for event in sorted(control_events, key=lambda e: (e.ts, e.line)):
        if event.kind == "command":
            name = event.data.split(" ", 1)[0].upper()
            if name in TRANSFER_COMMANDS:
                if current is not None:
                    windows.append((current[0], event.ts, current[2]))
                current = [event.ts, None, event]
        elif event.kind == "reply" and current is not None:
            code = event.code()
            if code in (125, 150):
                current[0] = min(current[0], event.ts)
            elif code in TRANSFER_TERMINAL:
                windows.append((current[0], event.ts, current[2]))
                current = None
    if current is not None:
        windows.append((current[0], current[1], current[2]))
    return windows


def _advertised_ports(control_events):
    """Best-effort passive/active data ports advertised on control."""
    ports = []
    for event in control_events:
        if event.kind != "reply":
            continue
        match = _PASV_RE.search(event.data)
        if match:
            port = int(match.group(5)) * 256 + int(match.group(6))
            ports.append(port)
            continue
        match = _EPSV_RE.search(event.data)
        if match:
            ports.append(int(match.group(1)))
    for event in control_events:
        if event.kind != "command":
            continue
        if event.data.upper().startswith("PORT "):
            match = _PORT_RE.match(event.data.split(" ", 1)[1].strip())
            if match:
                ports.append(int(match.group(5)) * 256 + int(match.group(6)))
        elif event.data.upper().startswith("EPRT "):
            parts = event.data.split("|")
            if len(parts) >= 4:
                try:
                    ports.append(int(parts[3]))
                except ValueError:
                    pass
    return set(ports)


@dataclass
class CaptureReplay:
    events: List[Event] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    control_conns: List[Connection] = field(default_factory=list)
    data_conns: List[Connection] = field(default_factory=list)

    def as_dict(self):
        return {
            "events": len(self.events),
            "warnings": self.warnings,
            "control_connections": len(self.control_conns),
            "data_connections": len(self.data_conns),
        }


def build_events(segments, warnings=None):
    """Convert TCP segments into replay events and diagnostics."""
    warnings = list(warnings or [])
    conns = _collect_connections(segments)
    control = _guess_server(conns)
    if control is None:
        raise ValueError("no FTP control connection found in capture")

    server_ip = control.server[0]
    client_ip = control.client[0]
    candidates = []
    for conn in conns:
        if conn is control:
            continue
        _orient(conn, server_ip)
        if conn.server[0] != server_ip or conn.client[0] != client_ip:
            continue
        candidates.append(conn)

    # Identify control connections: any candidate which itself looks
    # like FTP control (e.g. a second session from the same host).
    control_conns = [control]
    data_candidates = []
    for conn in candidates:
        commands, replies = _looks_like_ftp(conn)
        if commands and replies:
            control_conns.append(conn)
        else:
            data_candidates.append(conn)

    events = []
    for index, conn in enumerate(control_conns, 1):
        session = f"s{index}" if len(control_conns) > 1 else "default"
        events.extend(_parse_control(conn, session))

    control_events = [event for event in events if event.channel == "control"]
    windows = []
    for session in {event.session for event in control_events}:
        session_events = [e for e in control_events if e.session == session]
        ports = _advertised_ports(session_events)
        for window in _transfer_windows(session_events):
            windows.append((session, window[0], window[1], window[2], ports))

    assigned = set()
    data_conns = []
    for conn in sorted(data_candidates, key=lambda c: c.first_ts):
        if conn.payload_s2c == 0 and conn.payload_c2s == 0:
            continue
        matches = []
        for session, start, end, command, ports in windows:
            if conn.first_ts < start - 2.0:
                continue
            if end is not None and conn.first_ts > end + 2.0:
                continue
            score = 0
            if conn.server[1] in ports or conn.client[1] in ports:
                score += 5
            if end is not None and start <= conn.first_ts <= end:
                score += 3
            elif conn.first_ts >= start:
                score += 1
            matches.append((score, session, command))
        if not matches:
            warnings.append(
                "data connection without a transfer window: "
                f"{conn.client} -> {conn.server}"
            )
            continue
        matches.sort(key=lambda row: (row[0], row[1]), reverse=True)
        _score, session, command = matches[0]
        conn_id = f"d{len(data_conns) + 1}"
        assigned.add(conn_id)
        data_conns.append(conn)
        _emit_data_events(conn, conn_id, session, events)

    return CaptureReplay(
        events=sorted(events, key=lambda event: (event.ts, event.line)),
        warnings=warnings,
        control_conns=control_conns,
        data_conns=data_conns,
    )


def _emit_data_events(conn, conn_id, session, events):
    pieces = _reassemble(conn.s2c)
    if pieces:
        base = min(offset for offset, _ts, _data in pieces)
        for offset, ts, data in pieces:
            if not data:
                continue
            events.append(
                Event(
                    ts=ts,
                    channel="data",
                    direction="s2c",
                    kind="segment",
                    conn=conn_id,
                    length=len(data),
                    offset=offset - base,
                    session=session,
                    extra={"offset_basis": "stream"},
                )
            )
    # Client-to-server payload is kept for uploads and diagnostics.
    pieces = _reassemble(conn.c2s)
    if pieces:
        base = min(offset for offset, _ts, _data in pieces)
        for offset, ts, data in pieces:
            if not data:
                continue
            events.append(
                Event(
                    ts=ts,
                    channel="data",
                    direction="c2s",
                    kind="segment",
                    conn=conn_id,
                    length=len(data),
                    offset=offset - base,
                    session=session,
                    extra={"offset_basis": "stream"},
                )
            )
    for key, who, how in (
        ("server_fin", "server", "fin"),
        ("server_reset", "server", "reset"),
        ("client_fin", "client", "fin"),
        ("client_reset", "client", "reset"),
    ):
        if key in conn.close_ts:
            events.append(
                Event(
                    ts=conn.close_ts[key],
                    channel="data",
                    direction="s2c" if who == "server" else "c2s",
                    kind="close",
                    conn=conn_id,
                    who=who,
                    how=how,
                    session=session,
                )
            )


def replay_capture(path):
    """Read a pcap/pcapng file and return ``(CaptureReplay, segments)``."""
    segments, warnings = read_segments(path)
    if not segments:
        raise ValueError("capture contains no TCP segments")
    return build_events(segments, warnings), segments
