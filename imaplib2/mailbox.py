"""Mailbox-name decoding and catalogues for IMAP4.

A collector may hand mailbox paths to a directory that stores only Unicode
and later want to use directory results to address the original mailboxes
again.  This module keeps exactly the information that round trip needs:

* the original protocol name, byte for byte;
* how that name was decoded (the connection's negotiated convention);
* the server-declared namespace prefix and hierarchy delimiter;
* whether re-encoding the text reproduces the original bytes.

Two different protocol names may decode, or normalize, to the same
Unicode text.  They receive *different* :class:`MailboxEntry` identities
and are reported as a conflict; neither is chosen to replace the other.  A
name that cannot round-trip may still be archived, but it cannot be
addressed through its text.

Public objects::

    MailboxName              one protocol name and its decode state
    Namespace                one RFC 2342 namespace (prefix, delimiter)
    NamespaceSet             personal/other/shared namespace lists
    MailboxEntry             one archived mailbox with a stable identity
    MailboxRecord            Unicode-only export record
    MailboxCatalog           archive and back-mapping API
    decode_mailbox_name      decode one name under a convention
    encode_mailbox_name      encode one name for a connection
    decode_modified_utf7     modified UTF-7 -> str
    encode_modified_utf7     str -> modified UTF-7
    parse_namespace_response RFC 2342 NAMESPACE -> NamespaceSet
    parse_list_response      LIST/LSUB data -> ListEntry objects
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


__all__ = (
    "MODIFIED_UTF7",
    "UTF8",
    "MailboxDecodeState",
    "MailboxName",
    "MailboxCatalogError",
    "MailboxNameError",
    "MailboxNotFound",
    "MailboxConflictError",
    "MailboxNotAddressableError",
    "Namespace",
    "NamespaceSet",
    "ListEntry",
    "MailboxListing",
    "MailboxEntry",
    "MailboxRecord",
    "MailboxCatalog",
    "MailboxConflictWarning",
    "decode_mailbox_name",
    "encode_mailbox_name",
    "decode_modified_utf7",
    "encode_modified_utf7",
    "parse_namespace_response",
    "parse_list_response",
    "catalog_from_connection",
    "restore_catalog",
)


#: IMAP mailbox names are modified UTF-7 unless UTF8=ACCEPT was enabled.
MODIFIED_UTF7 = "modified-utf7"
#: RFC 6855 mailbox names after a successful ``ENABLE UTF8=ACCEPT``.
UTF8 = "utf-8"

_PRINTABLE_ASCII = frozenset(range(0x20, 0x7F))
_MODIFIED_UTF7_B64 = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+,"
)
_UTF7_ALIASES = frozenset((
    "modified-utf7", "modified-utf-7", "mutf-7", "mutf7",
    "imap4-utf-7", "imap4utf7",
))
_UTF8_ALIASES = frozenset(("utf-8", "utf8"))


class MailboxDecodeState(str, Enum):
    """How a protocol mailbox name was turned into text."""

    UTF8 = UTF8
    MODIFIED_UTF7 = MODIFIED_UTF7
    UTF8_FALLBACK = "utf-8-fallback"
    INVALID = "invalid"

    def __str__(self):                      # pragma: no cover - cosmetic
        return self.value


# ---------------------------------------------------------------------------
# Errors


class MailboxCatalogError(ValueError):
    """Base class for mailbox catalogue errors."""


class MailboxNameError(MailboxCatalogError):
    """A name cannot be represented in the requested encoding."""


class MailboxNotFound(MailboxCatalogError):
    """No archived mailbox matches the supplied identity or text."""


class MailboxConflictError(MailboxCatalogError):
    """Several archived mailboxes share the requested Unicode text.

    ``directory_ids`` lists every candidate.  Callers must choose by id;
    no candidate is preferred.
    """

    def __init__(self, name, entries):
        self.name = name
        self.entries = tuple(entries)
        self.directory_ids = tuple(entry.directory_id for entry in self.entries)
        super().__init__(
            "mailbox name %r is ambiguous; choose a directory id: %s"
            % (name, ", ".join(self.directory_ids))
        )


class MailboxNotAddressableError(MailboxCatalogError):
    """A text name cannot safely address its archived mailbox."""

    def __init__(self, entry, reason):
        self.entry = entry
        self.directory_id = entry.directory_id
        super().__init__(
            "mailbox %s (%r) cannot be addressed by name: %s"
            % (entry.directory_id, entry.display_name, reason)
        )


# ---------------------------------------------------------------------------
# Encoding


def _canonical_encoding(encoding):
    if encoding is None:
        return MODIFIED_UTF7
    value = str(encoding).lower().replace("_", "-")
    if value in _UTF7_ALIASES:
        return MODIFIED_UTF7
    if value in _UTF8_ALIASES:
        return UTF8
    raise MailboxNameError("unsupported mailbox encoding %r" % (encoding,))


def _encode_modified_utf7(text):
    if not isinstance(text, str):
        raise TypeError("text must be str")
    out = bytearray()
    pending = []

    def flush():
        if not pending:
            return
        data = "".join(pending).encode("utf-16-be")
        encoded = base64.b64encode(data).decode("ascii")
        encoded = encoded.rstrip("=").replace("/", ",")
        out.extend(b"&" + encoded.encode("ascii") + b"-")
        del pending[:]

    for char in text:
        code = ord(char)
        if 0xD800 <= code <= 0xDFFF:
            raise MailboxNameError(
                "U+%04X is a lone surrogate and cannot be encoded" % code)
        if code in _PRINTABLE_ASCII:
            flush()
            if char == "&":
                out.extend(b"&-")
            else:
                out.append(code)
        else:
            pending.append(char)
    flush()
    return bytes(out)


def _decode_modified_utf7(raw):
    """Return ``(text, noncanonical)`` or raise :class:`MailboxNameError`."""

    if not isinstance(raw, (bytes, bytearray)):
        raise TypeError("raw must be bytes")
    raw = bytes(raw)
    out = []
    index = 0
    length = len(raw)
    noncanonical = False

    while index < length:
        byte = raw[index]
        if byte == 0x26:                            # '&'
            end = raw.find(b"-", index + 1)
            if end < 0:
                raise MailboxNameError(
                    "unterminated modified UTF-7 shift at byte %d" % index)
            chunk = raw[index + 1:end]
            if not chunk:
                out.append("&")
                index = end + 1
                continue
            if b"=" in chunk:
                noncanonical = True
                chunk = chunk.rstrip(b"=")
            if b"/" in chunk:
                noncanonical = True
                chunk = chunk.replace(b"/", b",")
            if not chunk or any(byte not in _MODIFIED_UTF7_B64 for byte in chunk):
                raise MailboxNameError(
                    "invalid modified UTF-7 base64 at byte %d" % index)
            if len(chunk) % 4 == 1:
                raise MailboxNameError(
                    "invalid modified UTF-7 base64 length at byte %d" % index)
            padded = chunk.replace(b",", b"/") + b"=" * ((4 - len(chunk) % 4) % 4)
            try:
                data = base64.b64decode(padded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise MailboxNameError(
                    "invalid modified UTF-7 base64 at byte %d: %s" % (index, exc))
            if not data or len(data) % 2:
                raise MailboxNameError(
                    "modified UTF-7 run at byte %d is not UTF-16BE" % index)
            try:
                decoded = data.decode("utf-16-be")
            except UnicodeDecodeError as exc:
                raise MailboxNameError(
                    "modified UTF-7 run at byte %d is not valid UTF-16BE: %s"
                    % (index, exc))
            out.append(decoded)
            index = end + 1
        elif byte in _PRINTABLE_ASCII:
            out.append(chr(byte))
            index += 1
        else:
            raise MailboxNameError(
                "literal byte 0x%02X is not allowed in a mailbox name" % byte)

    text = "".join(out)
    if _encode_modified_utf7(text) != raw:
        noncanonical = True
    return text, noncanonical


def encode_modified_utf7(text):
    """Encode *text* as an IMAP modified UTF-7 mailbox name."""

    return _encode_modified_utf7(text)


def decode_modified_utf7(raw):
    """Decode a modified UTF-7 mailbox name to text."""

    return _decode_modified_utf7(raw)[0]


@dataclass(frozen=True)
class MailboxName:
    """One protocol mailbox name and its decode result.

    ``raw`` is always the exact wire bytes.  ``text`` is ``None`` when the
    name could not be decoded under the connection convention.
    ``noncanonical`` means decoding succeeded but re-encoding would not
    reproduce ``raw`` (for example an encoded printable ASCII character or
    non-canonical base64 padding).
    """

    raw: bytes
    text: Optional[str]
    decode_state: MailboxDecodeState
    encoding: str
    error: Optional[str] = None
    noncanonical: bool = False
    fell_back: bool = False

    @property
    def round_trip(self):
        """True when re-encoding ``text`` reproduces ``raw`` exactly.

        A name decoded through the UTF-8 fallback is reported as not
        round-tripping: the connection convention is modified UTF-7, so
        encoding the text for this connection would not reproduce it.
        """

        if self.text is None or self.fell_back:
            return False
        try:
            return encode_mailbox_name(self.text, self.encoding) == self.raw
        except MailboxNameError:
            return False

    @property
    def normalized(self):
        if self.text is None:
            return None
        return unicodedata.normalize("NFC", self.text)

    @property
    def display_name(self):
        if self.text is not None:
            return self.text
        detail = self.error or "undecodable"
        return "<invalid %s: %s: %s>" % (self.encoding, self.raw.hex(), detail)

    @property
    def protocol_name(self):
        """The exact bytes to put on the wire."""

        return self.raw


def decode_mailbox_name(raw, encoding=MODIFIED_UTF7, *, fallback_utf8=False):
    """Decode one protocol name under the connection's convention.

    With ``fallback_utf8`` a name that is not valid modified UTF-7 is
    retried as UTF-8; the result is marked ``UTF8_FALLBACK`` and is never
    treated as round-trippable.  Without the fallback an undecodable name
    is preserved with state ``INVALID`` and ``text=None``.
    """

    if isinstance(raw, str):
        raw = raw.encode("ascii")
    raw = bytes(raw)
    encoding = _canonical_encoding(encoding)

    if encoding == UTF8:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            return MailboxName(raw, None, MailboxDecodeState.INVALID,
                               encoding, error=str(exc))
        return MailboxName(raw, text, MailboxDecodeState.UTF8, encoding)

    try:
        text, noncanonical = _decode_modified_utf7(raw)
    except MailboxNameError as exc:
        if fallback_utf8:
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return MailboxName(raw, None, MailboxDecodeState.INVALID,
                                   encoding, error=str(exc))
            return MailboxName(raw, text, MailboxDecodeState.UTF8_FALLBACK, UTF8,
                               error=str(exc), fell_back=True)
        return MailboxName(raw, None, MailboxDecodeState.INVALID, encoding,
                           error=str(exc))
    return MailboxName(raw, text, MailboxDecodeState.MODIFIED_UTF7, encoding,
                       noncanonical=noncanonical)


def encode_mailbox_name(text, encoding=MODIFIED_UTF7):
    """Encode one Unicode mailbox name for a connection's convention."""

    if not isinstance(text, str):
        raise TypeError("text must be str")
    encoding = _canonical_encoding(encoding)
    if encoding == UTF8:
        try:
            return text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise MailboxNameError("mailbox name is not valid Unicode: %s" % exc)
    return _encode_modified_utf7(text)


# ---------------------------------------------------------------------------
# RFC 2342 namespaces


def _decode_delimiter(raw):
    if raw is None or raw == b"":
        return None
    try:
        value = raw.decode("ascii")
    except UnicodeDecodeError:
        raise MailboxCatalogError(
            "namespace delimiter %r is not ASCII" % (raw,))
    if len(value) != 1:
        raise MailboxCatalogError(
            "namespace delimiter %r is not one character" % (raw,))
    return value


@dataclass(frozen=True)
class Namespace:
    """One RFC 2342 namespace declaration.

    ``prefix`` is the protocol-encoded prefix including any trailing
    delimiter, and ``delimiter`` is the hierarchy delimiter the *server*
    declared (``None`` when the namespace is flat).
    """

    kind: str                               # 'personal', 'other', 'shared'
    prefix: bytes
    delimiter: Optional[str]
    prefix_text: Optional[str] = None
    order: int = 0

    @property
    def delimits(self):
        return self.delimiter is not None


@dataclass(frozen=True)
class NamespaceSet:
    """The personal, other and shared lists from NAMESPACE."""

    personal: tuple = ()
    other: tuple = ()
    shared: tuple = ()

    def all(self):
        return tuple(self.personal) + tuple(self.other) + tuple(self.shared)

    def __bool__(self):
        return bool(self.all())

    def match(self, raw):
        """Return every namespace with the longest matching prefix.

        Matching is a plain byte-prefix test: ``b"Shared"`` does not match
        prefix ``b"Shared/"``.  When several declarations share the longest
        prefix they are all returned; callers must not pick one silently.
        """

        raw = bytes(raw)
        matches = [ns for ns in self.all() if raw.startswith(ns.prefix)]
        if not matches:
            return ()
        longest = max(len(ns.prefix) for ns in matches)
        return tuple(ns for ns in matches if len(ns.prefix) == longest)

    def delimiters(self):
        return tuple(ns.delimiter for ns in self.all())


class _Reader:
    """Tokenizer for the argument part of IMAP response data."""

    def __init__(self, data):
        self.data = bytes(data)
        self.pos = 0

    def eof(self):
        return self.pos >= len(self.data)

    def peek(self, value=None):
        if self.eof():
            return None if value is None else False
        if value is None:
            return self.data[self.pos:self.pos + 1]
        return self.data.startswith(value, self.pos)

    def skip_spaces(self):
        while self.data[self.pos:self.pos + 1] == b" ":
            self.pos += 1

    def expect(self, value):
        self.skip_spaces()
        if not self.data.startswith(value, self.pos):
            raise MailboxCatalogError(
                "expected %r at offset %d in %r"
                % (value, self.pos, self.data))
        self.pos += len(value)

    def atom(self):
        self.skip_spaces()
        start = self.pos
        while not self.eof() and self.data[self.pos:self.pos + 1] not in b" ()":
            self.pos += 1
        return self.data[start:self.pos]

    def value(self):
        """Parse NIL, a quoted string, or an atom; return bytes or None."""

        self.skip_spaces()
        if self.eof():
            raise MailboxCatalogError("unexpected end of response")
        if self.peek(b'"'):
            return self.quoted()
        start = self.pos
        atom = self.atom()
        if not atom:
            raise MailboxCatalogError("empty value at offset %d" % start)
        if atom.upper() == b"NIL":
            return None
        return atom

    def quoted(self):
        self.expect(b'"')
        out = bytearray()
        while not self.eof():
            char = self.data[self.pos]
            self.pos += 1
            if char == 0x22:                    # '"'
                return bytes(out)
            if char == 0x5C:                    # '\\'
                if self.eof():
                    break
                out.append(self.data[self.pos])
                self.pos += 1
            else:
                out.append(char)
        raise MailboxCatalogError(
            "unterminated quoted string in %r" % self.data)


def _apply_literal(header, literal):
    """Replace a trailing ``{n}`` marker in *header* with *literal*."""

    header = bytes(header)
    if literal is None:
        return header
    literal = bytes(literal)
    end = header.rfind(b"}")
    start = header.rfind(b"{", 0, end)
    if end == len(header) - 1 and start >= 0 and header[start + 1:end].isdigit():
        return header[:start] + literal
    return header + literal


def _flatten_item(item):
    if isinstance(item, tuple):
        if len(item) < 2:
            return bytes(item[0])
        return _apply_literal(item[0], item[1])
    if item is None:
        return None
    return bytes(item)


def _strip_untagged(body, name):
    if body is None:
        return None
    body = bytes(body)
    name = name.encode("ascii") if isinstance(name, str) else bytes(name)
    if body.startswith(b"* "):
        head, _, rest = body[2:].partition(b" ")
        if head.upper() == name.upper():
            return rest
    return body


def _parse_namespace_list(reader):
    reader.skip_spaces()
    head = reader.peek()
    if head is None:
        return ()
    if head.upper() == b"N":
        value = reader.atom()
        if value.upper() != b"NIL":
            raise MailboxCatalogError("invalid namespace list %r" % value)
        return ()
    reader.expect(b"(")
    out = []
    while True:
        reader.skip_spaces()
        if reader.peek(b")"):
            reader.pos += 1
            return tuple(out)
        head = reader.peek()
        if head is None:
            raise MailboxCatalogError("unterminated namespace list")
        if head.upper() == b"N":
            value = reader.atom()
            if value.upper() != b"NIL":
                raise MailboxCatalogError("invalid namespace item %r" % value)
            continue
        reader.expect(b"(")
        prefix = reader.value()
        delimiter = reader.value()
        reader.expect(b")")
        out.append((prefix, delimiter))


def parse_namespace_response(data, encoding=MODIFIED_UTF7):
    """Parse an RFC 2342 NAMESPACE response into a :class:`NamespaceSet`.

    *data* may be the list returned by ``IMAP4.namespace()``, a raw
    response line, or the bare argument bytes.  NIL namespace lists become
    empty tuples.  Prefixes are decoded with *encoding*; an undecodable
    prefix keeps ``prefix_text=None`` rather than inventing text.
    """

    encoding = _canonical_encoding(encoding)
    if isinstance(data, (bytes, bytearray)):
        items = [bytes(data)]
    elif isinstance(data, tuple):
        items = [data]
    else:
        items = list(data or ())

    bodies = []
    for item in items:
        body = _flatten_item(item)
        if body is None:
            continue
        body = _strip_untagged(body, "NAMESPACE")
        if body.upper().startswith(b"NAMESPACE "):
            body = body[len(b"NAMESPACE "):]
        if body:
            bodies.append(body)
    if not bodies:
        return NamespaceSet()

    reader = _Reader(bodies[0])
    personal = _parse_namespace_list(reader)
    other = _parse_namespace_list(reader)
    shared = _parse_namespace_list(reader)
    counter = [0]

    def build(values, kind):
        result = []
        for prefix, delimiter in values:
            prefix = b"" if prefix is None else bytes(prefix)
            if prefix:
                prefix_text = decode_mailbox_name(prefix, encoding).text
            else:
                prefix_text = ""
            result.append(Namespace(kind=kind, prefix=prefix,
                                    delimiter=_decode_delimiter(delimiter),
                                    prefix_text=prefix_text,
                                    order=counter[0]))
            counter[0] += 1
        return tuple(result)

    return NamespaceSet(build(personal, "personal"),
                        build(other, "other"),
                        build(shared, "shared"))


# ---------------------------------------------------------------------------
# LIST / LSUB


@dataclass(frozen=True)
class ListEntry:
    """One parsed LIST/LSUB response, before decoding."""

    raw_name: bytes
    delimiter: Optional[str]
    flags: tuple = ()
    response: bytes = b""

    @property
    def subscribed(self):
        return any(flag.lower() == "\\subscribed" for flag in self.flags)


@dataclass(frozen=True)
class MailboxListing:
    """A decoded LIST name and the delimiter that server declared."""

    name: MailboxName
    delimiter: Optional[str] = None
    flags: tuple = ()

    @property
    def raw(self):
        return self.name.raw

    @property
    def text(self):
        return self.name.text

    @property
    def display_name(self):
        return self.name.display_name

    @property
    def decode_state(self):
        return self.name.decode_state

    @property
    def round_trip(self):
        return self.name.round_trip

    def segments(self):
        if self.text is None:
            return ()
        if self.delimiter is None:
            return (self.text,)
        return tuple(self.text.split(self.delimiter))


def parse_list_response(data, encoding=MODIFIED_UTF7):
    """Parse LIST/LSUB data into :class:`ListEntry` objects.

    Quoted names, atoms, and names delivered as literals all work.  Names
    are returned undecoded; use :func:`decode_mailbox_name`.
    """

    del encoding                          # names stay raw until decoded
    if isinstance(data, (bytes, bytearray, tuple)):
        items = [data]
    else:
        items = list(data or ())

    result = []
    for item in items:
        if item is None:
            continue
        literal_name = None
        if isinstance(item, tuple):
            if len(item) < 2:
                continue
            body = _apply_literal(item[0], item[1])
            literal_name = bytes(item[1])
        else:
            body = bytes(item)
        body = _strip_untagged(_strip_untagged(body, "LIST"), "LSUB")
        if body.upper().startswith(b"LIST "):
            body = body[len(b"LIST "):]
        elif body.upper().startswith(b"LSUB "):
            body = body[len(b"LSUB "):]
        if not body:
            continue
        reader = _Reader(body)
        reader.skip_spaces()
        if not reader.peek(b"("):
            raise MailboxCatalogError("LIST response lacks flags: %r" % body)
        reader.pos += 1
        flags = []
        while True:
            reader.skip_spaces()
            if reader.peek(b")"):
                reader.pos += 1
                break
            token = reader.atom()
            if not token:
                raise MailboxCatalogError("unterminated flags in %r" % body)
            flags.append(token.decode("ascii", "replace"))
        delimiter = reader.value()
        if literal_name is not None:
            raw_name = literal_name
        else:
            raw_name = reader.value()
            if raw_name is None:
                raise MailboxCatalogError(
                    "LIST response has a NIL name: %r" % body)
        result.append(ListEntry(raw_name=bytes(raw_name),
                                delimiter=_decode_delimiter(delimiter),
                                flags=tuple(flags), response=body))
    return result


# ---------------------------------------------------------------------------
# Catalogue


@dataclass
class MailboxEntry:
    """One archived mailbox with a stable directory identity.

    The identity is derived from the connection convention and the exact
    protocol bytes.  It is therefore stable across re-exports, and two
    distinct protocol names can never share it.  Namespace assignment is
    kept as metadata and does not alter the identity.
    """

    directory_id: str
    name: MailboxName
    namespace: Optional[Namespace]
    namespaces: tuple = ()
    namespace_text: Optional[str] = None
    namespace_ambiguous: bool = False
    delimiter: Optional[str] = None
    relative_name: Optional[str] = None
    segments: tuple = ()
    flags: tuple = ()
    warnings: tuple = ()
    conflict_group: Optional[str] = None
    fallback_namespace: bool = False
    _catalog: Optional["MailboxCatalog"] = field(default=None, repr=False,
                                                 compare=False)

    @property
    def raw(self):
        return self.name.raw

    @property
    def text(self):
        return self.name.text

    @property
    def display_name(self):
        return self.name.display_name

    @property
    def normalized(self):
        return self.name.normalized

    @property
    def decode_state(self):
        return self.name.decode_state

    @property
    def encoding(self):
        return self.name.encoding

    @property
    def round_trip(self):
        return self.name.round_trip

    @property
    def reversible(self):
        return self.name.round_trip

    @property
    def is_conflict(self):
        return self.conflict_group is not None

    @property
    def namespace_kind(self):
        return self.namespace.kind if self.namespace is not None else "unknown"

    @property
    def namespace_prefix(self):
        return self.namespace.prefix if self.namespace is not None else b""

    @property
    def addressable_by_name(self):
        """Only a unique, round-tripping entry may be found by its text."""

        return self.round_trip and not self.is_conflict

    def protocol_name(self):
        """The exact bytes to send in an IMAP command."""

        return self.raw

    def export(self):
        return MailboxRecord.from_entry(self)

    def __repr__(self):                     # pragma: no cover - cosmetic
        return "MailboxEntry(%r, %r, %s)" % (
            self.directory_id, self.display_name, self.decode_state.value)


@dataclass(frozen=True)
class MailboxRecord:
    """Unicode-only export record for one archived mailbox.

    ``raw_hex`` is retained for audit and reconstructing a protocol name if
    the directory operator explicitly needs it; the ordinary address path
    uses ``directory_id``.
    """

    directory_id: str
    name: Optional[str]
    display_name: str
    normalized: Optional[str]
    namespace: str
    namespace_prefix: Optional[str]
    delimiter: Optional[str]
    relative_name: Optional[str]
    segments: tuple
    decode_state: str
    encoding: str
    round_trip: bool
    addressable_by_name: bool
    conflict_group: Optional[str]
    raw_hex: str
    flags: tuple
    warnings: tuple

    @classmethod
    def from_entry(cls, entry):
        return cls(
            directory_id=entry.directory_id,
            name=entry.text,
            display_name=entry.display_name,
            normalized=entry.normalized,
            namespace=entry.namespace_kind,
            namespace_prefix=entry.namespace_text,
            delimiter=entry.delimiter,
            relative_name=entry.relative_name,
            segments=tuple(entry.segments),
            decode_state=entry.decode_state.value,
            encoding=entry.encoding,
            round_trip=entry.round_trip,
            addressable_by_name=entry.addressable_by_name,
            conflict_group=entry.conflict_group,
            raw_hex=entry.raw.hex(),
            flags=tuple(entry.flags),
            warnings=tuple(entry.warnings),
        )

    def to_dict(self):
        return {
            "directory_id": self.directory_id,
            "name": self.name,
            "display_name": self.display_name,
            "normalized": self.normalized,
            "namespace": self.namespace,
            "namespace_prefix": self.namespace_prefix,
            "delimiter": self.delimiter,
            "relative_name": self.relative_name,
            "segments": list(self.segments),
            "decode_state": self.decode_state,
            "encoding": self.encoding,
            "round_trip": self.round_trip,
            "addressable_by_name": self.addressable_by_name,
            "conflict_group": self.conflict_group,
            "raw_hex": self.raw_hex,
            "flags": list(self.flags),
            "warnings": list(self.warnings),
        }


class MailboxConflictWarning:
    """Warning describing a decoded/normalized name collision."""

    def __init__(self, name, group_id, entries):
        self.name = name
        self.group_id = group_id
        self.directory_ids = tuple(entry.directory_id for entry in entries)

    def __repr__(self):                     # pragma: no cover - cosmetic
        return "MailboxConflictWarning(%r, %r, %r)" % (
            self.name, self.group_id, self.directory_ids)

    def __str__(self):
        return "mailbox name %r collides for %s" % (
            self.name, ", ".join(self.directory_ids))


@dataclass(frozen=True)
class _ResolvedNamespace:
    namespace: Optional[Namespace]
    candidates: tuple
    ambiguous: bool
    fallback: bool
    warnings: tuple


class MailboxCatalog:
    """Archive protocol names for a Unicode-only directory.

    ``encoding`` is the mailbox convention negotiated for the connection:
    :data:`MODIFIED_UTF7` (default) or :data:`UTF8`.  ``normalization`` is
    the Unicode form used for collision detection; ``None`` compares only
    the decoded text.

    Adding two distinct protocol names with equal normalized text yields
    two entries with distinct ``directory_id`` values and a shared
    ``conflict_group``.  Text lookup then raises
    :class:`MailboxConflictError`; it never picks one.
    """

    def __init__(self, namespaces=None, *, encoding=MODIFIED_UTF7,
                 normalization="NFC", fallback_utf8=False):
        if namespaces is None:
            namespaces = NamespaceSet()
        if not isinstance(namespaces, NamespaceSet):
            raise TypeError("namespaces must be a NamespaceSet")
        if normalization not in (None, "NFC", "NFD", "NFKC", "NFKD"):
            raise ValueError(
                "unsupported normalization form %r" % (normalization,))
        self.namespaces = namespaces
        self.encoding = _canonical_encoding(encoding)
        self.normalization = normalization
        self.fallback_utf8 = fallback_utf8
        self.warnings: List[MailboxConflictWarning] = []
        self._entries: List[MailboxEntry] = []
        self._by_id = {}
        self._by_key = {}

    # -- construction -----------------------------------------------------

    @classmethod
    def from_imap(cls, imap, *, directory='""', pattern="*",
                  include_lsub=False, namespaces=None, normalization="NFC",
                  fallback_utf8=False):
        """Build a catalogue from a connected ``IMAP4`` instance.

        The encoding follows the connection: UTF-8 once ``ENABLE
        UTF8=ACCEPT`` succeeded, modified UTF-7 otherwise.  NAMESPACE
        supplies prefixes and delimiters; LIST supplies names, and a LIST
        delimiter overrides the namespace one when they disagree.
        """

        encoding = UTF8 if getattr(imap, "utf8_enabled", False) else MODIFIED_UTF7
        if namespaces is None:
            namespaces = NamespaceSet()
            try:
                typ, data = imap.namespace()
            except Exception:
                typ, data = "NO", ()
            if typ == "OK":
                namespaces = parse_namespace_response(data, encoding)

        catalog = cls(namespaces, encoding=encoding,
                      normalization=normalization, fallback_utf8=fallback_utf8)

        typ, data = imap.list(directory, pattern)
        if typ != "OK":
            raise MailboxCatalogError("LIST failed: %s %r" % (typ, data))
        for item in parse_list_response(data, encoding):
            catalog.add(item.raw_name, delimiter=item.delimiter,
                        flags=item.flags)

        if include_lsub:
            typ, data = imap.lsub(directory, pattern)
            if typ == "OK":
                for item in parse_list_response(data, encoding):
                    entry = catalog.add(item.raw_name, delimiter=item.delimiter,
                                        flags=item.flags)
                    if item.subscribed:
                        entry.flags = tuple(sorted(
                            set(entry.flags) | {"\\Subscribed"}))
        return catalog

    # -- identity ---------------------------------------------------------

    @staticmethod
    def _identity(raw, encoding):
        """Identity from the connection convention and exact wire bytes.

        Namespace assignment is metadata: the same wire name under an
        ambiguous prefix is still the same mailbox.  Two different wire
        names always receive different identities, even when their decoded
        or normalized text collides.
        """

        digest = hashlib.sha256()
        digest.update(encoding.encode("ascii"))
        digest.update(b"\x00")
        digest.update(bytes(raw))
        return "mbx_" + digest.hexdigest()[:24]

    def _resolve_namespace(self, raw, namespace, delimiter):
        warnings = []
        explicit_kind = None
        if isinstance(namespace, Namespace):
            candidates = (namespace,)
        elif namespace is not None:
            if not isinstance(namespace, str):
                raise TypeError(
                    "namespace must be a Namespace, a kind string, or None")
            explicit_kind = namespace
            candidates = tuple(ns for ns in self.namespaces.match(raw)
                               if ns.kind == namespace)
            if not candidates:
                warnings.append("no %s namespace matches this name" % namespace)
        else:
            candidates = self.namespaces.match(raw)

        ambiguous = False
        fallback = False
        chosen = None
        if len(candidates) == 1:
            chosen = candidates[0]
        elif len(candidates) > 1:
            profiles = {(ns.prefix, ns.delimiter) for ns in candidates}
            kinds = {ns.kind for ns in candidates}
            if explicit_kind is not None or (len(profiles) == 1
                                             and len(kinds) == 1):
                chosen = candidates[0]
            else:
                ambiguous = True
                warnings.append(
                    "name matches several namespaces; keeping it unqualified: %s"
                    % ", ".join(sorted(kinds)))
        elif explicit_kind is not None:
            fallback = True
            warnings.append(
                "no declared %s namespace; treating the name as unqualified"
                % explicit_kind)
            chosen = Namespace(kind=explicit_kind, prefix=b"",
                               delimiter=delimiter, prefix_text="")
        elif not self.namespaces:
            fallback = True
            chosen = Namespace(kind="personal", prefix=b"",
                               delimiter=delimiter, prefix_text="")
        else:
            warnings.append("name is outside every declared namespace")

        if chosen is not None and not raw.startswith(chosen.prefix):
            warnings.append("name does not start with its namespace prefix")
            chosen = None

        effective = delimiter
        if effective is None and chosen is not None:
            effective = chosen.delimiter
        if (delimiter is not None and chosen is not None
                and chosen.delimiter is not None
                and delimiter != chosen.delimiter):
            warnings.append(
                "LIST delimiter %r differs from the namespace delimiter %r; "
                "using the LIST value" % (delimiter, chosen.delimiter))
            effective = delimiter

        return _ResolvedNamespace(chosen, tuple(candidates), ambiguous,
                                  fallback, tuple(warnings)), effective

    # -- adding -----------------------------------------------------------

    def add(self, raw, *, namespace=None, delimiter=None, flags=()):
        """Archive one protocol name and return its entry.

        Re-adding the same protocol name in the same namespace is
        idempotent and merges flags.  A different protocol name is never
        allowed to overwrite an existing entry, even when its text matches.
        """

        if isinstance(raw, str):
            try:
                raw = raw.encode("ascii")
            except UnicodeEncodeError:
                raise MailboxNameError(
                    "add() expects a protocol name; use encode_mailbox_name() "
                    "for Unicode text")
        raw = bytes(raw)
        resolved, effective = self._resolve_namespace(raw, namespace, delimiter)
        identity = self._identity(raw, self.encoding)

        existing = self._by_id.get(identity)
        if existing is not None:
            existing.flags = tuple(sorted(set(existing.flags) | set(flags)))
            return existing

        name = decode_mailbox_name(raw, self.encoding,
                                   fallback_utf8=self.fallback_utf8)
        warnings = list(resolved.warnings)
        if name.fell_back:
            warnings.append(
                "not valid %s; decoded as UTF-8" % self.encoding)

        chosen = resolved.namespace
        prefix_raw = chosen.prefix if chosen is not None else b""
        relative_raw = raw[len(prefix_raw):] if raw.startswith(prefix_raw) else raw
        relative = decode_mailbox_name(relative_raw, self.encoding,
                                       fallback_utf8=self.fallback_utf8)
        relative_name = relative.text

        if relative_name is None:
            segments = ()
            warnings.append(
                "cannot split the hierarchy because the name is not valid Unicode")
        elif effective is None:
            segments = (relative_name,) if relative_name else ()
        else:
            segments = tuple(relative_name.split(effective)) \
                if relative_name else ()

        entry = MailboxEntry(
            directory_id=identity,
            name=name,
            namespace=chosen,
            namespaces=resolved.candidates,
            namespace_text=(chosen.prefix_text if chosen is not None else None),
            namespace_ambiguous=resolved.ambiguous,
            delimiter=effective,
            relative_name=relative_name,
            segments=segments,
            flags=tuple(sorted(set(flags))),
            warnings=tuple(warnings),
            fallback_namespace=resolved.fallback,
            _catalog=self,
        )
        self._entries.append(entry)
        self._by_id[identity] = entry
        self._index(entry)
        return entry

    def _key(self, text):
        return unicodedata.normalize(self.normalization, text) \
            if self.normalization else text

    def _index(self, entry):
        if entry.text is None:
            return
        key = self._key(entry.text)
        group = self._by_key.setdefault(key, [])
        if all(item.directory_id != entry.directory_id for item in group):
            group.append(entry)
        if len(group) > 1:
            group_id = "conflict_" + hashlib.sha256(
                key.encode("utf-8")).hexdigest()[:16]
            for item in group:
                item.conflict_group = group_id
            if group_id not in [w.group_id for w in self.warnings]:
                self.warnings.append(
                    MailboxConflictWarning(key, group_id, group))

    # -- lookup -----------------------------------------------------------

    def __len__(self):
        return len(self._entries)

    def __iter__(self):
        return iter(self._entries)

    def __contains__(self, identity):
        return identity in self._by_id

    def __getitem__(self, identity):
        try:
            return self._by_id[identity]
        except KeyError:
            raise MailboxNotFound(
                "unknown mailbox directory id %r" % (identity,))

    def entries(self):
        return tuple(self._entries)

    def matches(self, text):
        """Return every entry whose normalized decoded text equals *text*."""

        if not isinstance(text, str):
            raise TypeError("text must be str")
        return tuple(self._by_key.get(self._key(text), ()))

    def conflicts(self):
        """Return ``{group_id: (entry, ...)}`` for text collisions."""

        result = {}
        for entry in self._entries:
            if entry.conflict_group is not None:
                result.setdefault(entry.conflict_group, []).append(entry)
        return {key: tuple(value) for key, value in result.items()}

    def protocol_name(self, target):
        """Return exact protocol bytes for an entry or directory id."""

        if isinstance(target, MailboxEntry):
            return target.raw
        if isinstance(target, str):
            try:
                return self._by_id[target].raw
            except KeyError:
                pass
        raise MailboxNotFound("unknown mailbox directory id %r" % (target,))

    def protocol_name_for_text(self, text):
        """Return protocol bytes for a Unicode spelling, or refuse.

        Refuses when the spelling matches several archived names, when the
        single match does not round-trip, or when re-encoding *text* would
        produce different bytes from the archived protocol name.
        """

        matches = self.matches(text)
        if not matches:
            raise MailboxNotFound("no mailbox named %r" % (text,))
        if len(matches) > 1:
            raise MailboxConflictError(text, matches)
        entry = matches[0]
        if entry.text is None:
            raise MailboxNotAddressableError(
                entry, "the name is not valid Unicode")
        if not entry.round_trip:
            raise MailboxNotAddressableError(
                entry, "the original protocol name does not round-trip")
        try:
            encoded = encode_mailbox_name(text, entry.encoding)
        except MailboxNameError as exc:
            raise MailboxNotAddressableError(entry, str(exc))
        if encoded != entry.raw:
            raise MailboxNotAddressableError(
                entry,
                "encoding %r yields %r, not the archived %r"
                % (text, encoded, entry.raw))
        return entry.raw

    def resolve(self, target):
        """Return one entry for an entry, id, or safe text spelling."""

        if isinstance(target, MailboxEntry):
            return target
        if isinstance(target, str):
            entry = self._by_id.get(target)
            if entry is not None:
                return entry
        matches = self.matches(target)
        if not matches:
            raise MailboxNotFound("no mailbox named %r" % (target,))
        if len(matches) > 1:
            raise MailboxConflictError(target, matches)
        self.protocol_name_for_text(target)         # enforce addressability
        return matches[0]

    def command_name(self, target):
        """Return the exact wire name for an entry or directory id."""

        return self.protocol_name(target)

    # -- export -----------------------------------------------------------

    def export(self):
        """Return every entry as a Unicode-only record, conflicts included."""

        return tuple(entry.export() for entry in self._entries)

    def export_dicts(self):
        return [record.to_dict() for record in self.export()]


def catalog_from_connection(imap, **kw):
    """Convenience wrapper for :meth:`MailboxCatalog.from_imap`."""

    return MailboxCatalog.from_imap(imap, **kw)


# ---------------------------------------------------------------------------
# Restoring a catalogue from Unicode-only export records


def _namespace_from_record(record):
    kind = record.get("namespace") or "personal"
    prefix_text = record.get("namespace_prefix")
    if prefix_text is None:
        return None
    encoding = _canonical_encoding(record.get("encoding"))
    prefix = encode_mailbox_name(prefix_text, encoding)
    return Namespace(kind=kind, prefix=prefix,
                     delimiter=record.get("delimiter"),
                     prefix_text=prefix_text)


def _record_key(record, normalization):
    text = record.get("name")
    if text is None:
        return None
    if normalization is None:
        return text
    return unicodedata.normalize(normalization, text)


def restore_catalog(records, *, namespaces=None, encoding=MODIFIED_UTF7,
                    normalization="NFC", verify=True):
    """Rebuild a :class:`MailboxCatalog` from exported records.

    The export is Unicode-only, so the protocol bytes are recovered from
    ``raw_hex``.  With ``verify`` the archived ``directory_id`` must match
    the identity recomputed under the supplied connection convention; a
    mismatch (a record exported for a different convention or namespace
    set) raises :class:`MailboxCatalogError` rather than producing an entry
    that would address a different mailbox.
    """

    encoding = _canonical_encoding(encoding)
    if namespaces is None:
        namespaces = NamespaceSet()
    catalog = MailboxCatalog(namespaces, encoding=encoding,
                             normalization=normalization)

    for record in records:
        try:
            raw = bytes.fromhex(record["raw_hex"])
        except (KeyError, TypeError, ValueError):
            raise MailboxCatalogError(
                "record lacks a valid raw_hex: %r" % (record,))
        namespace = _namespace_from_record(record)
        entry = catalog.add(raw, namespace=namespace,
                            delimiter=record.get("delimiter"),
                            flags=tuple(record.get("flags") or ()))

        declared_id = record.get("directory_id")
        if verify and declared_id and declared_id != entry.directory_id:
            raise MailboxCatalogError(
                "record %r identifies %r, but the archived bytes belong to %r"
                % (declared_id, declared_id, entry.directory_id))

        declared_key = _record_key(record, normalization)
        actual_key = _record_key({"name": entry.text}, normalization)
        if verify and declared_key != actual_key:
            raise MailboxCatalogError(
                "record %r text %r does not match re-decoded text %r"
                % (declared_id, record.get("name"), entry.text))
    return catalog
