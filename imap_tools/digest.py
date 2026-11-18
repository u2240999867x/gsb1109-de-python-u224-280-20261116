"""
Indexing of inner messages contained in summary/digest emails.

A customer-service archive may receive a summary email that bundles several
original messages. Each original message must be indexed separately while the
outer delivery message stays available as-is. Three encapsulation forms are
supported:

* ``message/rfc822`` parts - a single encapsulated message
* ``multipart/digest`` parts - several encapsulated messages, one per child
  part (a child without an explicit Content-Type is treated as rfc822)
* plain text summaries split by a caller-provided separator, where each chunk
  is itself an rfc2822 message

A chunk that cannot be parsed as a complete message is skipped instead of
aborting the run, so the remaining summary content is never swallowed.
"""

import base64
import binascii
import quopri
import re
from email.errors import MessageError
from email.message import Message
from email.parser import BytesParser, Parser
from typing import Iterator, List, Optional, Tuple, Union

StrOrBytes = Union[str, bytes]


# Fatal parser defects mark text that does not form a complete rfc2822 message.
# They are looked up by name so the module keeps working on builds where some
# defect classes are absent.
def _fatal_defect_classes() -> Tuple[type, ...]:
    import email.errors as _errors

    names = (
        'MissingHeaderBodySeparatorDefect',
        'BoundaryError',
        'StartBoundaryNotFoundDefect',
        'CloseBoundaryNotFoundDefect',
        'MultipartInvariantViolationDefect',
        'MalformedHeaderDefect',
    )
    return tuple(getattr(_errors, name) for name in names if hasattr(_errors, name))


FATAL_DEFECT_CLASSES = _fatal_defect_classes()


class InnerMessage:
    """
    One message extracted from inside a summary email.

    ``message`` is the standalone inner ``email.message.Message``; it carries
    only the inner message's own headers - outer From/Date values are never
    copied into it, so a missing inner sender or date stays missing.
    ``index`` is the zero-based occurrence position across the whole summary,
    which keeps duplicate Message-ID messages as independent occurrences.
    """

    __slots__ = 'message', 'index', 'origin'

    def __init__(self, message: Message, index: int, origin: str) -> None:
        self.message = message
        self.index = index
        self.origin = origin

    def __repr__(self):
        return f'{self.__class__.__name__}(index={self.index!r}, origin={self.origin!r})'


def _has_fatal_defect(message: Message) -> bool:
    # Only the message's own defects count: a malformed sub-part must not
    # invalidate an otherwise complete top-level inner message, and nested
    # message/rfc822 parts are validated separately during traversal.
    for defect in getattr(message, 'defects', ()):
        if isinstance(defect, FATAL_DEFECT_CLASSES):
            return True
    return False


def _decode_part_body(part: Message) -> Optional[bytes]:
    r"""
    Get the transfer-decoded raw body of a ``message/rfc822`` part.

    The stdlib leaves transfer-encoded ``message/rfc822`` bodies undecoded, so
    the part Content-Transfer-Encoding is handled here. ``None`` means the body
    could not be decoded (e.g. broken base64) and must not be treated as a
    message.
    """
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload
    raw = part.as_bytes()
    match = re.search(rb'\r?\n\r?\n', raw)
    body = raw[match.end():] if match else raw
    cte = str(part.get('Content-Transfer-Encoding', '')).lower().strip()
    try:
        if cte == 'base64':
            return base64.b64decode(b''.join(body.split()), validate=False)
        if cte == 'quoted-printable':
            return quopri.decodestring(body)
    except (binascii.Error, ValueError):
        return None
    return body


def _parse_message_bytes(data: bytes) -> Optional[Message]:
    """Parse bytes as a standalone message, rejecting incomplete ones."""
    data = data.lstrip(b'\r\n')
    if not data.strip():
        return None
    try:
        message = BytesParser().parsebytes(data)
    except (MessageError, ValueError, TypeError):
        return None
    if _has_fatal_defect(message):
        return None
    return message


def _parse_message_str(data: str) -> Optional[Message]:
    """Parse text as a standalone message, rejecting incomplete ones."""
    data = data.lstrip('\r\n')
    if not data.strip():
        return None
    try:
        message = Parser().parsestr(data)
    except (MessageError, ValueError, TypeError):
        return None
    if _has_fatal_defect(message):
        return None
    return message


def _message_from_rfc822_part(part: Message) -> Optional[Message]:
    """Extract the single message encapsulated by a message/rfc822 part."""
    payload = part.get_payload()
    if isinstance(payload, list):
        inner = payload[0] if payload and isinstance(payload[0], Message) else None
    else:
        inner = payload if isinstance(payload, Message) else None
    if inner is not None and not _has_fatal_defect(inner):
        return inner
    # Recovery for parts the stdlib could not turn into a message.
    if part.get('Content-Type') is None:
        # An untyped multipart/digest child: its own raw bytes are the inner
        # message (the parser may have left a blank line in front).
        return _parse_message_bytes(part.as_bytes().lstrip(b'\r\n'))
    # An explicit message/rfc822 wrapper with an undecodable/encoded body.
    body = _decode_part_body(part)
    if body is None:
        return None
    return _parse_message_bytes(body)


def _iter_rfc822_parts(part: Message) -> Iterator[Message]:
    """Yield message/rfc822 parts, descending into nested containers."""
    content_type = part.get_content_type()
    if content_type == 'message/rfc822':
        yield part
        # An encapsulated message may itself encapsulate further messages.
        payload = part.get_payload()
        inner = payload[0] if isinstance(payload, list) and payload else payload
        if isinstance(inner, Message):
            yield from _iter_rfc822_parts(inner)
    elif part.get_content_maintype() == 'multipart':
        # multipart/digest children default to message/rfc822; ordinary
        # multipart containers are traversed to reach nested summaries.
        for child in part.get_payload():
            if isinstance(child, Message):
                yield from _iter_rfc822_parts(child)


def _split_text_chunks(text: str, separator: str) -> List[str]:
    r"""
    Split a plain text summary on whole-line separator occurrences.

    The separator only splits when it forms an entire line by itself, so the
    same text appearing inside quoted message bodies never cuts a message.
    Lines may be separated by ``\n`` or ``\r\n``.
    """
    if separator == '':
        return [text]
    pattern = re.compile(
        r'(?:^|(?<=\r\n)|(?<=\n))' + re.escape(separator) + r'(?=\r\n|\n|$)'
    )
    chunks = pattern.split(text)
    # The zero-width match keeps the boundary line break on the next chunk.
    result = [chunks[0]]
    for chunk in chunks[1:]:
        if chunk.startswith('\r\n'):
            chunk = chunk[2:]
        elif chunk.startswith('\n'):
            chunk = chunk[1:]
        result.append(chunk)
    return result


def _normalize_separator(separator: StrOrBytes) -> str:
    if isinstance(separator, bytes):
        return separator.decode('utf-8', 'surrogateescape')
    return separator


def _text_part_decoded(part: Message) -> Optional[str]:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return None
    charset = part.get_content_charset() or 'utf-8'
    try:
        return payload.decode(charset, 'replace')
    except LookupError:
        return payload.decode('utf-8', 'replace')


def _iter_outer_text_parts(part: Message) -> Iterator[Message]:
    """
    Yield the outer message's own text/plain parts.

    Traversal stops at ``message/*`` parts: text inside an encapsulated
    message belongs to that inner message and is not split on its own.
    """
    maintype = part.get_content_maintype()
    if maintype == 'multipart':
        for child in part.get_payload():
            if isinstance(child, Message):
                yield from _iter_outer_text_parts(child)
    elif maintype == 'text' and part.get_content_type() in ('text/plain', 'text/'):
        yield part


def _inner_from_text(part: Message, separator: str) -> Iterator[Message]:
    text = _text_part_decoded(part)
    if text is None or separator not in text:
        return
    for chunk in _split_text_chunks(text, separator):
        message = _parse_message_str(chunk)
        if message is not None:
            yield message


def parse_inner_messages(
    message: Message,
    text_separator: Optional[StrOrBytes] = None,
) -> List[InnerMessage]:
    """
    Index the inner messages of a summary email.

    :param message: the outer (delivery) message; it is not modified and stays
        available to the caller as the original delivery record
    :param text_separator: whole-line separator that delimits inner rfc2822
        messages inside plain text summary parts. ``None`` disables plain text
        splitting; bytes are decoded as UTF-8
    :returns: inner messages in occurrence order. Each parseable inner message
        appears exactly once, even when two of them share a Message-ID. An
        unparseable segment is skipped without dropping the segments after it.

    Inner messages keep their own headers only: when an inner message lacks a
    From or Date header, the corresponding value stays missing instead of
    inheriting the outer message's value.
    """
    separator = _normalize_separator(text_separator) if text_separator is not None else None
    results: List[InnerMessage] = []

    def add_message(inner: Message, origin: str) -> None:
        results.append(InnerMessage(inner, len(results), origin))

    for part in _iter_rfc822_parts(message):
        inner = _message_from_rfc822_part(part)
        if inner is not None:
            add_message(inner, part.get_content_type())

    if separator is not None:
        for part in _iter_outer_text_parts(message):
            for inner in _inner_from_text(part, separator):
                add_message(inner, 'text/plain')

    return results


def parse_text_summary(text: StrOrBytes, separator: StrOrBytes) -> List[InnerMessage]:
    """
    Parse a standalone plain text summary into its inner messages.

    :param text: the summary text; bytes are decoded as UTF-8
    :param separator: whole-line separator between inner messages
    :returns: parseable inner messages in order, skipping malformed segments
        without losing the rest of the summary
    """
    if isinstance(text, bytes):
        text = text.decode('utf-8', 'surrogateescape')
    separator = _normalize_separator(separator)
    results: List[InnerMessage] = []
    for chunk in _split_text_chunks(text, separator):
        message = _parse_message_str(chunk)
        if message is not None:
            results.append(InnerMessage(message, len(results), 'text/plain'))
    return results
