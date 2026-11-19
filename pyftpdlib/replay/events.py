# Copyright (C) 2007 Giampaolo Rodola' <g.rodola@gmail.com>.
# Use of this source code is governed by MIT license that can be
# found in the LICENSE file.

"""Normalized events used to replay an FTP control/data capture offline.

A capture is represented as a stream of :class:`Event` objects.  The
schema is deliberately small so that both packet decoders and hand
written fixtures can produce it:

    {"ts": 1.0, "channel": "control", "direction": "c2s",
     "kind": "command", "data": "RETR file.bin"}
    {"ts": 1.1, "channel": "control", "direction": "s2c",
     "kind": "reply", "data": "150 Opening data connection."}
    {"ts": 1.2, "channel": "data", "direction": "s2c",
     "kind": "segment", "conn": "d1", "offset": 0, "length": 1024}
    {"ts": 1.3, "channel": "data", "direction": "s2c",
     "kind": "close", "conn": "d1", "who": "server", "how": "fin"}

Offsets are absolute in the file being downloaded (``REST`` offsets are
included).  When ``offset`` is omitted a segment is appended after the
highest offset seen so far, which is only safe for in-order captures.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Iterable, Iterator, List, Optional

__all__ = [
    "Event",
    "EventError",
    "load_events",
    "parse_event",
    "read_event_stream",
]

CHANNELS = ("control", "data")
DIRECTIONS = ("c2s", "s2c")
KINDS = ("command", "reply", "segment", "open", "close")

_REPLY_RE = re.compile(r"^(\d{3})([ -])(.*)$")


class EventError(ValueError):
    """Raised when an offline replay event is malformed."""


@dataclass
class Event:
    """One observable fact from an offline capture.

    - ts: capture timestamp in seconds.
    - channel: "control" or "data".
    - direction: "c2s" (client to server) or "s2c" (server to client).
    - kind: "command", "reply", "segment", "open" or "close".
    - data: command/reply text, or raw payload length for segment events.
    - conn: opaque data connection identifier.
    - seq: TCP sequence number, when known.
    - length: payload length for a segment.
    - offset: absolute file offset for a segment, when known.
    - who: for close events, "client" or "server" (the sender of the
      FIN/RST).
    - how: for close events, "fin" or "reset".
    """

    ts: float
    channel: str
    direction: str
    kind: str
    data: str = ""
    conn: str = ""
    seq: Optional[int] = None
    length: Optional[int] = None
    offset: Optional[int] = None
    who: str = ""
    how: str = ""
    session: str = "default"
    line: int = 0
    extra: dict = field(default_factory=dict)

    def code(self) -> Optional[int]:
        """Return the three digit reply code, if this is a reply."""
        if self.kind != "reply":
            return None
        match = _REPLY_RE.match(self.data)
        if not match:
            return None
        return int(match.group(1))

    def is_multiline_start(self) -> bool:
        match = _REPLY_RE.match(self.data)
        return bool(match and match.group(2) == "-")

    def is_multiline_end(self, code: int) -> bool:
        match = _REPLY_RE.match(self.data)
        return bool(
            match and match.group(2) == " " and int(match.group(1)) == code
        )


def _first(mapping, names, default=None):
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return default


def _as_float(value, what, line):
    try:
        return float(value)
    except (TypeError, ValueError):
        raise EventError(f"line {line}: invalid {what}: {value!r}") from None


def _as_int(value, what, line):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise EventError(f"line {line}: invalid {what}: {value!r}") from None


def _normalize_direction(value, line):
    aliases = {
        "c2s": "c2s",
        "client->server": "c2s",
        "client-to-server": "c2s",
        "client": "c2s",
        "request": "c2s",
        "s2c": "s2c",
        "server->client": "s2c",
        "server-to-client": "s2c",
        "server": "s2c",
        "response": "s2c",
    }
    key = str(value).strip().lower()
    try:
        return aliases[key]
    except KeyError:
        raise EventError(
            f"line {line}: invalid direction: {value!r}"
        ) from None


def parse_event(obj, line=0, session="default") -> Event:
    """Build an :class:`Event` from a decoded JSON object.

    Unknown keys are preserved in ``Event.extra`` so a capture producer
    can attach annotations without losing them.
    """
    if not isinstance(obj, dict):
        raise EventError(f"line {line}: event must be a JSON object")

    ts = _as_float(_first(obj, ("ts", "time", "timestamp")), "timestamp", line)
    channel = str(_first(obj, ("channel", "stream"), "")).strip().lower()
    if channel not in CHANNELS:
        raise EventError(
            f"line {line}: channel must be one of {CHANNELS}, got {channel!r}"
        )
    direction = _normalize_direction(
        _first(obj, ("direction", "dir"), ""), line
    )

    text = _first(obj, ("data", "payload", "text", "line"), "")
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", "replace")
    text = str(text)
    if text.endswith("\r\n"):
        text = text[:-2]
    elif text.endswith("\n") or text.endswith("\r"):
        text = text[:-1]

    kind = str(_first(obj, ("kind", "type"), "")).strip().lower()
    if not kind:
        kind = _infer_kind(channel, direction, obj, text)
    if kind not in KINDS:
        raise EventError(
            f"line {line}: kind must be one of {KINDS}, got {kind!r}"
        )

    conn = str(_first(obj, ("conn", "connection", "stream_id", "flow"), ""))
    seq_val = _first(obj, ("seq", "tcp_seq", "sequence"))
    len_val = _first(obj, ("length", "len", "payload_len", "size"), None)
    off_val = _first(obj, ("offset", "file_offset"), None)
    who = str(_first(obj, ("who", "closed_by", "sender"), "")).strip().lower()
    how = str(_first(obj, ("how", "close_kind", "close"), "")).strip().lower()
    if how in ("rst", "reset"):
        how = "reset"
    elif how in ("fin", "close", "eof"):
        how = "fin"

    if kind == "segment":
        if len_val is None:
            raise EventError(f"line {line}: segment event needs a length")
        length = _as_int(len_val, "segment length", line)
        if length < 0:
            raise EventError(f"line {line}: segment length must be >= 0")
        seq = None if seq_val is None else _as_int(seq_val, "seq", line)
        offset = None if off_val is None else _as_int(off_val, "offset", line)
    elif kind == "close":
        if who and who not in ("client", "server", "unknown"):
            raise EventError(
                f"line {line}: close who must be client/server, got {who!r}"
            )
        if how and how not in ("fin", "reset"):
            raise EventError(
                f"line {line}: close how must be fin/reset, got {how!r}"
            )
        length = None if len_val is None else _as_int(len_val, "length", line)
        seq = None if seq_val is None else _as_int(seq_val, "seq", line)
        offset = None
    else:
        length = None if len_val is None else _as_int(len_val, "length", line)
        seq = None if seq_val is None else _as_int(seq_val, "seq", line)
        offset = None if off_val is None else _as_int(off_val, "offset", line)

    if kind == "command" and channel != "control":
        raise EventError(f"line {line}: command must use the control channel")
    if kind == "reply" and channel != "control":
        raise EventError(f"line {line}: reply must use the control channel")
    if kind == "segment" and channel != "data":
        raise EventError(f"line {line}: segment must use the data channel")
    if kind == "close" and channel != "data":
        raise EventError(f"line {line}: close must use the data channel")
    if kind == "reply" and not _REPLY_RE.match(text):
        raise EventError(f"line {line}: malformed reply: {text!r}")
    if kind == "command" and not text:
        raise EventError(f"line {line}: empty command")

    known = {
        "ts",
        "time",
        "timestamp",
        "channel",
        "stream",
        "direction",
        "dir",
        "kind",
        "type",
        "data",
        "payload",
        "text",
        "line",
        "conn",
        "connection",
        "stream_id",
        "flow",
        "seq",
        "tcp_seq",
        "sequence",
        "length",
        "len",
        "payload_len",
        "size",
        "offset",
        "file_offset",
        "who",
        "closed_by",
        "sender",
        "how",
        "close_kind",
        "close",
        "session",
    }
    extra = {k: v for k, v in obj.items() if k not in known}
    obj_session = obj.get("session")
    if obj_session is not None:
        session = str(obj_session)

    return Event(
        ts=ts,
        channel=channel,
        direction=direction,
        kind=kind,
        data=text,
        conn=conn,
        seq=seq,
        length=length,
        offset=offset,
        who=who,
        how=how,
        session=session,
        line=line,
        extra=extra,
    )


def _infer_kind(channel, direction, obj, text):
    if channel == "control":
        if direction == "c2s":
            return "command"
        return "reply"
    if any(key in obj for key in ("who", "closed_by", "sender")) or str(
        obj.get("close", "")
    ).lower() in ("fin", "reset", "close", "eof"):
        return "close"
    if any(
        key in obj
        for key in ("length", "len", "payload_len", "offset", "file_offset")
    ):
        return "segment"
    if not text:
        return "segment"
    return "segment"


def _iter_json_objects(text, source):
    stripped = text.lstrip()
    if not stripped:
        return
    if stripped.startswith("["):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as err:
            raise EventError(f"{source}: invalid JSON: {err}") from None
        if not isinstance(payload, list):
            raise EventError(f"{source}: top level JSON must be a list")
        for index, obj in enumerate(payload, 1):
            yield index, obj
        return
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as err:
            raise EventError(
                f"{source}:{lineno}: invalid JSON: {err}"
            ) from None
        yield lineno, obj


def iter_events(text, source="<input>") -> Iterator[Event]:
    """Yield events parsed from a JSON array or JSON Lines document."""
    for lineno, obj in _iter_json_objects(text, source):
        if isinstance(obj, list):
            raise EventError(
                f"{source}:{lineno}: nested arrays are not valid events"
            )
        yield parse_event(obj, line=lineno)


def load_events(path) -> List[Event]:
    """Load and sort events from a JSON/JSON Lines file."""
    with open(path, encoding="utf-8") as file:
        text = file.read()
    events = list(iter_events(text, source=str(path)))
    events.sort(key=lambda event: (event.ts, event.line))
    return events


def read_event_stream(fileobj, source="<input>") -> List[Event]:
    """Read events from a text file object and sort them by timestamp."""
    events = list(iter_events(fileobj.read(), source=source))
    events.sort(key=lambda event: (event.ts, event.line))
    return events


def events_from_records(records: Iterable[dict]) -> List[Event]:
    """Convenience helper for tests and embedders."""
    events = [
        parse_event(record, line=index)
        for index, record in enumerate(records, 1)
    ]
    events.sort(key=lambda event: (event.ts, event.line))
    return events
