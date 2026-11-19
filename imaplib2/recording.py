"""Parse recorded IMAP4 server responses.

This module is for replaying a captured server-to-client byte stream.  It is
deliberately independent of the socket and threading code in
:mod:`imaplib2.imaplib2`: a recording can be supplied in arbitrary chunks and
the parser keeps all offsets relative to the original recording.

The important rule is that an IMAP literal is framed by its declared length,
not by looking for line endings or protocol-looking text in its payload.  A
literal may therefore contain CRLF, ``*`` responses, a tag that resembles the
tag of the command being answered, or another literal-looking ``{N}``.  None
of those bytes are interpreted until exactly ``N`` payload bytes have been
consumed.

The public API is:

``IMAPRecordingParser``
    Incremental parser.  Call :meth:`IMAPRecordingParser.feed` with bytes and
    call :meth:`IMAPRecordingParser.finish` at the end of the recording.

``parse_recording``
    Convenience function for a bytes object or an iterable of byte chunks.

``Response``, ``Literal``, ``Pending`` and ``ParseResult``
    Results.  Every offset is absolute in the original recording; ``end``
    offsets are exclusive.  ``Response.payload_ranges`` gives the exact
    ``(start, end)`` span of each literal payload.
"""

from __future__ import annotations

import base64
import json
import re
import sys
from dataclasses import dataclass
from typing import Iterable

__all__ = (
    "IMAPRecordingParser",
    "parse_recording",
    "Response",
    "Literal",
    "Pending",
    "ParseResult",
    "ParseError",
)

# A literal marker is only recognised at the very end of a response line and
# outside a quoted string.  The optional ``~`` and ``+`` forms are RFC 3516
# binary literals and RFC 2088 non-synchronising literals respectively.
_LITERAL_END = re.compile(
    rb"(?P<binary>~?)\{(?P<size>[0-9]+)(?P<non_sync>\+)?\}$"
)

_TAGGED = re.compile(
    rb"(?P<tag>\S+) +(?P<status>[A-Za-z]+)(?: +(?P<text>.*))?$", re.S
)
_UNTAGGED_NUMBER = re.compile(
    rb"(?P<number>[0-9]+) +(?P<status>[A-Za-z][A-Za-z0-9-]*)(?: +(?P<text>.*))?$",
    re.S,
)
_UNTAGGED = re.compile(
    rb"(?P<status>[A-Za-z][A-Za-z0-9-]*)(?: +(?P<text>.*))?$", re.S
)
_RESPONSE_CODE = re.compile(
    rb"\[(?P<code>[A-Za-z][A-Za-z0-9-]*)(?: +(?P<data>[^\]]*))?\]"
)


class ParseError(ValueError):
    """The recording contains bytes that cannot begin an IMAP response."""

    def __init__(self, offset: int, message: str):
        self.offset = offset
        self.message = message
        super().__init__("%s at offset %d" % (message, offset))


@dataclass(frozen=True)
class Literal:
    """One literal in a response.

    ``marker_start``/``marker_end`` cover the literal marker itself (for
    example ``{42}``).  ``payload_start``/``payload_end`` cover exactly the
    declared number of payload bytes; ``payload_end - payload_start`` is
    always ``size``.  Offsets are absolute and end offsets are exclusive.
    ``payload`` is the same span of bytes (including any CRLF or
    protocol-looking text it contains).
    """

    marker: bytes
    size: int
    marker_start: int
    marker_end: int
    payload_start: int
    payload_end: int
    payload: bytes

    @property
    def non_synchronizing(self) -> bool:
        return self.marker.endswith(b"+}")

    @property
    def binary(self) -> bool:
        return self.marker.startswith(b"~")

    @property
    def range(self) -> tuple[int, int]:
        return self.payload_start, self.payload_end

    def to_dict(self) -> dict[str, object]:
        return {
            "size": self.size,
            "marker": _decode(self.marker),
            "marker_start": self.marker_start,
            "marker_end": self.marker_end,
            "payload_start": self.payload_start,
            "payload_end": self.payload_end,
            "payload": _decode(self.payload),
            "payload_base64": base64.b64encode(self.payload).decode("ascii"),
        }


@dataclass(frozen=True)
class Response:
    """A complete tagged, untagged, or continuation response.

    ``header`` is the first raw line without its line terminator, including
    the first literal marker.  ``text`` is the same header with the marker and
    its separator removed.  ``tails`` are the raw line fragments following
    each literal payload (the last one closes the response).
    """

    start: int
    end: int
    kind: str
    marker: bytes | None
    tag: bytes | None
    number: int | None
    status: bytes | None
    text: bytes
    code: bytes | None
    code_data: bytes | None
    header: bytes
    literals: tuple[Literal, ...]
    tails: tuple[bytes, ...]
    tail_ranges: tuple[tuple[int, int], ...]

    @property
    def header_range(self) -> tuple[int, int]:
        """The absolute span of the first structural line, without terminator."""

        return self.start, self.start + len(self.header)

    @property
    def payload_ranges(self) -> tuple[tuple[int, int], ...]:
        """The absolute ``(start, end)`` span of every literal payload."""

        return tuple((lit.payload_start, lit.payload_end) for lit in self.literals)

    @property
    def fields(self) -> dict[str, object]:
        """The structural fields of the response header.

        Literal payloads are intentionally absent; use ``payload_ranges`` or
        ``literals`` for those.
        """

        return {
            "kind": self.kind,
            "marker": self.marker,
            "tag": self.tag,
            "number": self.number,
            "status": self.status,
            "text": self.text,
            "code": self.code,
            "code_data": self.code_data,
        }

    @property
    def field_ranges(self) -> dict[str, tuple[int, int]]:
        """Absolute spans of the structural fields in the first header line.

        Fields that are not present are omitted.  ``text`` excludes the
        literal marker and the space separating it from the text; the marker
        itself is reported by the corresponding :class:`Literal`.
        """

        ranges: dict[str, tuple[int, int]] = {}
        line = self.header
        length = len(line)
        pos = 0

        if self.marker in (b"*", b"+"):
            ranges["marker"] = (self.start, self.start + 1)
            pos = 1
            while pos < length and line[pos : pos + 1] == b" ":
                pos += 1

        def take(start: int) -> tuple[tuple[int, int], int]:
            while start < length and line[start : start + 1] == b" ":
                start += 1
            end = start
            while end < length and line[end : end + 1] != b" ":
                end += 1
            return (self.start + start, self.start + end), end

        if self.kind == "tagged":
            span, pos = take(pos)
            ranges["tag"] = span
        elif self.number is not None:
            span, pos = take(pos)
            ranges["number"] = span

        if self.status is not None:
            span, pos = take(pos)
            ranges["status"] = span

        if self.kind == "continuation":
            while pos < length and line[pos : pos + 1] == b" ":
                pos += 1
            if pos < length:
                ranges["text"] = (self.start + pos, self.start + length)
        elif self.text:
            while pos < length and line[pos : pos + 1] == b" ":
                pos += 1
            ranges["text"] = (self.start + pos, self.start + pos + len(self.text))

        return ranges

    @property
    def segments(self) -> tuple[tuple[str, int, int], ...]:
        """Contiguous ``(role, start, end)`` pieces covering the response.

        Roles are ``"header"``, ``"payload"``, ``"tail"`` and
        ``"separator"`` (line terminators).  The pieces are ordered, do not
        overlap, and together cover ``start`` through ``end`` exactly.  This
        makes it possible to hand the literal payload span to a decoder while
        preserving every structural byte around it.
        """

        segments: list[tuple[str, int, int]] = []
        position = self.start
        header_end = self.header_range[1]
        segments.append(("header", self.start, header_end))
        position = header_end

        for literal, (tail_start, tail_end) in zip(self.literals, self.tail_ranges):
            if literal.payload_start > position:
                segments.append(
                    ("separator", position, literal.payload_start)
                )
            segments.append(("payload", literal.payload_start, literal.payload_end))
            if tail_start > literal.payload_end:
                segments.append(("separator", literal.payload_end, tail_start))
            segments.append(("tail", tail_start, tail_end))
            position = tail_end

        if self.end > position:
            segments.append(("separator", position, self.end))

        return tuple(segments)

    def to_dict(self) -> dict[str, object]:
        """Serialise the response for JSON output or test fixtures."""

        return {
            "start": self.start,
            "end": self.end,
            "kind": self.kind,
            "marker": _decode(self.marker),
            "tag": _decode(self.tag),
            "number": self.number,
            "status": _decode(self.status),
            "text": _decode(self.text),
            "code": _decode(self.code),
            "code_data": _decode(self.code_data),
            "header": _decode(self.header),
            "literals": [literal.to_dict() for literal in self.literals],
            "header_range": list(self.header_range),
            "field_ranges": {
                key: list(span) for key, span in self.field_ranges.items()
            },
            "payload_ranges": [list(span) for span in self.payload_ranges],
            "tail_ranges": [list(span) for span in self.tail_ranges],
            "segments": [list(segment) for segment in self.segments],
        }


@dataclass(frozen=True)
class Pending:
    """The unconfirmed tail left after a truncated recording.

    ``data`` starts at the beginning of the incomplete response, not at the
    point where parsing stopped.  This makes it possible to append more bytes
    later (or to inspect exactly what was not confirmed) without losing the
    response header or earlier literal markers.

    When ``reason`` is ``"literal"``, ``literal_size``, ``payload_start`` and
    ``received`` describe the payload still being awaited; ``missing`` is the
    number of payload bytes not present in the recording.
    """

    start: int
    end: int
    data: bytes
    reason: str
    literal_size: int | None = None
    payload_start: int | None = None
    received: int | None = None

    @property
    def length(self) -> int:
        return len(self.data)

    @property
    def missing(self) -> int | None:
        """Payload bytes still needed, when truncation happened in a literal."""

        if self.literal_size is None or self.received is None:
            return None
        return self.literal_size - self.received

    def to_dict(self) -> dict[str, object]:
        return {
            "start": self.start,
            "end": self.end,
            "reason": self.reason,
            "length": self.length,
            "data": _decode(self.data),
            "data_base64": base64.b64encode(self.data).decode("ascii"),
            "literal_size": self.literal_size,
            "payload_start": self.payload_start,
            "received": self.received,
            "missing": self.missing,
        }


@dataclass(frozen=True)
class ParseResult:
    """The complete responses plus any unconfirmed tail."""

    responses: tuple[Response, ...]
    pending: Pending | None

    def to_dict(self) -> dict[str, object]:
        return {
            "responses": [response.to_dict() for response in self.responses],
            "pending": None if self.pending is None else self.pending.to_dict(),
        }


class _Incomplete(Exception):
    def __init__(
        self,
        reason: str,
        *,
        literal_size: int | None = None,
        payload_start: int | None = None,
        received: int | None = None,
    ):
        self.reason = reason
        self.literal_size = literal_size
        self.payload_start = payload_start
        self.received = received
        super().__init__(reason)


def _line_at(buf: bytes | bytearray, start: int) -> tuple[int, int] | None:
    """Return ``(content_end, terminator_length)`` for the line at ``start``.

    Recorded IMAP should use CRLF.  A bare LF is also accepted so that a
    text-form transcript can be parsed; the terminator length is needed to
    compute the first literal payload byte exactly.
    """

    lf = buf.find(b"\n", start)
    if lf < 0:
        return None
    if lf > start and buf[lf - 1] == 0x0D:
        return lf - 1, 2
    return lf, 1


def _inside_quotes(line: bytes, position: int) -> bool:
    """Return whether ``position`` is inside an IMAP quoted string."""

    in_quote = False
    escaped = False
    for byte in line[:position]:
        if in_quote:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # double quote
                in_quote = False
        elif byte == 0x22:
            in_quote = True
    return in_quote


def _literal_marker(line: bytes) -> tuple[int, int, int] | None:
    """Find a literal marker at the end of ``line``.

    Returns ``(start, end, size)`` relative to ``line`` or ``None``.  A brace
    sequence inside a quoted string is a string character, not a marker.
    Elsewhere ``{`` cannot occur inside an atom, so a marker at the end of the
    line is a literal even without a separating space.
    """

    match = _LITERAL_END.search(line)
    if match is None:
        return None

    start = match.start()
    if _inside_quotes(line, start):
        return None

    return start, match.end(), int(match.group("size"))


def _parse_header(
    prefix: bytes, offset: int
) -> tuple[str, bytes | None, bytes | None, int | None, bytes | None, bytes]:
    """Parse the non-literal part of a response line.

    Returns ``(kind, marker, tag, number, status, text)``.
    """

    if prefix.startswith(b"+") and (len(prefix) == 1 or prefix[1:2] == b" "):
        text = prefix[1:]
        if text.startswith(b" "):
            text = text[1:]
        return "continuation", b"+", None, None, None, text

    if prefix.startswith(b"*") and (len(prefix) == 1 or prefix[1:2] == b" "):
        rest = prefix[1:]
        if rest.startswith(b" "):
            rest = rest[1:]

        match = _UNTAGGED_NUMBER.match(rest)
        if match is not None:
            return (
                "untagged",
                b"*",
                None,
                int(match.group("number")),
                match.group("status"),
                match.group("text") or b"",
            )

        match = _UNTAGGED.match(rest)
        if match is not None:
            return (
                "untagged",
                b"*",
                None,
                None,
                match.group("status"),
                match.group("text") or b"",
            )

        raise ParseError(offset, "invalid untagged response")

    match = _TAGGED.match(prefix)
    if match is None:
        raise ParseError(offset, "invalid tagged or continuation response")

    return (
        "tagged",
        None,
        match.group("tag"),
        None,
        match.group("status"),
        match.group("text") or b"",
    )


def _response_code(text: bytes) -> tuple[bytes | None, bytes | None]:
    match = _RESPONSE_CODE.match(text)
    if match is None:
        return None, None
    return match.group("code"), match.group("data")


class IMAPRecordingParser:
    """Incremental parser for a recorded server-to-client IMAP byte stream.

    The parser never searches within a literal payload for a line ending or a
    tag.  A response is emitted only after its final structural line has been
    terminated.  If the input ends inside a line or a literal, the bytes from
    the start of that response are kept in :attr:`pending`.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._base = 0
        self._pending: Pending | None = None
        self._finished = False

    @property
    def pending(self) -> Pending | None:
        """The current unconfirmed tail, if any."""

        return self._pending

    @property
    def offset(self) -> int:
        """Absolute offset of the first byte currently buffered."""

        return self._base

    @property
    def buffered(self) -> int:
        """Number of bytes currently retained but not yet confirmed."""

        return len(self._buffer)

    def feed(self, data: bytes | bytearray | memoryview) -> list[Response]:
        """Add bytes to the recording and return newly completed responses."""

        if self._finished:
            raise RuntimeError("cannot feed a finished recording parser")
        if isinstance(data, str):
            raise TypeError("recording chunks must be bytes, not str")

        self._buffer.extend(bytes(data))
        return self._drain()

    def finish(self) -> Pending | None:
        """Mark the recording as complete and return the unconfirmed tail."""

        self._finished = True
        if self._pending is None and self._buffer:
            self._pending = self._make_pending()
        return self._pending

    def _drain(self) -> list[Response]:
        responses: list[Response] = []

        while self._buffer:
            try:
                parsed = self._parse_one()
            except _Incomplete as incomplete:
                self._pending = self._make_pending(incomplete)
                break

            response, consumed = parsed
            if response is not None:
                responses.append(response)
            del self._buffer[:consumed]
            self._base += consumed
            self._pending = None

        return responses

    def _make_pending(self, incomplete: _Incomplete | None = None) -> Pending | None:
        if not self._buffer:
            return None

        if incomplete is None:
            return Pending(
                start=self._base,
                end=self._base + len(self._buffer),
                data=bytes(self._buffer),
                reason="line",
            )

        return Pending(
            start=self._base,
            end=self._base + len(self._buffer),
            data=bytes(self._buffer),
            reason=incomplete.reason,
            literal_size=incomplete.literal_size,
            payload_start=incomplete.payload_start,
            received=incomplete.received,
        )

    def _parse_one(self) -> tuple[Response | None, int]:
        buf = self._buffer
        first = _line_at(buf, 0)
        if first is None:
            raise _Incomplete("line")

        first_end, first_term = first
        first_line = bytes(buf[:first_end])
        if not first_line.strip(b" "):
            # An empty line (for example the CRLF ending the preamble of a
            # recording) carries no response.
            return None, first_end + first_term
        marker = _literal_marker(first_line)

        if marker is None:
            kind, resp_marker, tag, number, status, text = _parse_header(
                first_line, self._base
            )
            code, code_data = _response_code(text)
            return (
                Response(
                    start=self._base,
                    end=self._base + first_end + first_term,
                    kind=kind,
                    marker=resp_marker,
                    tag=tag,
                    number=number,
                    status=status,
                    text=text,
                    code=code,
                    code_data=code_data,
                    header=first_line,
                    literals=(),
                    tails=(),
                    tail_ranges=(),
                ),
                first_end + first_term,
            )

        marker_start, marker_end, size = marker
        prefix = first_line[:marker_start]
        if prefix.endswith(b" "):
            # Drop the separator before the literal marker; the marker is
            # reported separately by Literal.
            prefix = prefix[:-1]
        kind, resp_marker, tag, number, status, text = _parse_header(prefix, self._base)
        code, code_data = _response_code(text)

        literals: list[Literal] = []
        tails: list[bytes] = []
        tail_ranges: list[tuple[int, int]] = []
        line_start = 0
        line_end = first_end
        line_term = first_term
        line = first_line

        while True:
            payload_start = line_end + line_term
            payload_end = payload_start + size
            if len(buf) < payload_end:
                raise _Incomplete(
                    "literal",
                    literal_size=size,
                    payload_start=self._base + payload_start,
                    received=max(0, len(buf) - payload_start),
                )

            literals.append(
                Literal(
                    marker=line[marker_start:marker_end],
                    size=size,
                    marker_start=self._base + line_start + marker_start,
                    marker_end=self._base + line_start + marker_end,
                    payload_start=self._base + payload_start,
                    payload_end=self._base + payload_end,
                    payload=bytes(buf[payload_start:payload_end]),
                )
            )

            following = _line_at(buf, payload_end)
            if following is None:
                raise _Incomplete("line")

            tail_end, tail_term = following
            tail = bytes(buf[payload_end:tail_end])
            tails.append(tail)
            tail_ranges.append(
                (self._base + payload_end, self._base + tail_end)
            )

            next_marker = _literal_marker(tail)
            if next_marker is None:
                end = tail_end + tail_term
                return (
                    Response(
                        start=self._base,
                        end=self._base + end,
                        kind=kind,
                        marker=resp_marker,
                        tag=tag,
                        number=number,
                        status=status,
                        text=text,
                        code=code,
                        code_data=code_data,
                        header=first_line,
                        literals=tuple(literals),
                        tails=tuple(tails),
                        tail_ranges=tuple(tail_ranges),
                    ),
                    end,
                )

            line_start = payload_end
            line_end = tail_end
            line_term = tail_term
            line = tail
            marker_start, marker_end, size = next_marker


def parse_recording(
    data: bytes | bytearray | memoryview | Iterable[bytes],
) -> ParseResult:
    """Parse a complete recording (or an iterable of byte chunks).

    Truncation is not an error: the unconfirmed tail is returned in
    :attr:`ParseResult.pending` and no partial :class:`Response` is emitted.
    """

    if isinstance(data, str):
        raise TypeError("recording data must be bytes, not str")

    parser = IMAPRecordingParser()
    if isinstance(data, (bytes, bytearray, memoryview)):
        chunks: Iterable[bytes] = (bytes(data),)
    else:
        chunks = data

    responses: list[Response] = []
    for chunk in chunks:
        if isinstance(chunk, str):
            raise TypeError("recording chunks must be bytes, not str")
        responses.extend(parser.feed(chunk))

    parser.finish()
    return ParseResult(tuple(responses), parser.pending)


def _decode(value: bytes | None) -> str | None:
    if value is None:
        return None
    return value.decode("utf-8", "replace")


def main(argv: list[str] | None = None) -> int:
    import argparse

    argument_parser = argparse.ArgumentParser(
        description="Show the structure and literal payload ranges of an IMAP recording."
    )
    argument_parser.add_argument(
        "recording", help="file containing server-to-client bytes"
    )
    argument_parser.add_argument(
        "--json", action="store_true", help="emit one JSON object instead of text"
    )
    args = argument_parser.parse_args(argv)

    with open(args.recording, "rb") as recording_file:
        data = recording_file.read()

    result = parse_recording(data)

    if args.json:
        payload = result.to_dict()
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0

    for index, response in enumerate(result.responses):
        status = _decode(response.status) or ""
        marker = _decode(response.marker) or ""
        tag = _decode(response.tag) or ""
        print(
            "%d: [%d:%d] %s %s %s payloads=%s"
            % (
                index,
                response.start,
                response.end,
                marker or tag,
                status,
                _decode(response.text) or "",
                list(response.payload_ranges),
            )
        )

    if result.pending is not None:
        print(
            "pending: [%d:%d] %s length=%d missing=%s"
            % (
                result.pending.start,
                result.pending.end,
                result.pending.reason,
                result.pending.length,
                result.pending.missing,
            )
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
