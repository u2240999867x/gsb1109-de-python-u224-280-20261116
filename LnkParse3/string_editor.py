"""Strict, read-only-input rewriting of Shell Link StringData values.

Only the requested StringData field is re-encoded.  Every other byte of the
original file is copied verbatim, including the LinkTargetIDList, LinkInfo,
the remaining StringData fields and all ExtraData blocks (plus any bytes after
the TerminalBlock).  StringData is the last structure before ExtraData, so
changing its size only moves later bytes.  No stored offset points forward
into ExtraData; the SpecialFolderDataBlock and KnownFolderDataBlock offsets
point back to the LinkTargetIDList, whose position does not change.

If the structure boundaries cannot be established without guessing, no output
is produced at all: a partial or best-effort copy would silently reinterpret
bytes belonging to a following field.
"""

import os
import struct
from contextlib import suppress
from pathlib import Path

from LnkParse3.exceptions import LnkParserError


LINK_CLS_ID = bytes.fromhex("0114020000000000c000000000000046")

# LinkFlags mask and canonical name, in their on-disk order.
STRING_DATA_FIELDS = (
    (0x00000004, "description"),
    (0x00000008, "relative_path"),
    (0x00000010, "working_directory"),
    (0x00000020, "command_line_arguments"),
    (0x00000040, "icon_location"),
)

STRING_DATA_NAMES = tuple(name for _, name in STRING_DATA_FIELDS)

STRING_DATA_ALIASES = {
    "name": "description",
    "description": "description",
    "description_string": "description",
    "relative_path": "relative_path",
    "relativepath": "relative_path",
    "working_dir": "working_directory",
    "working_directory": "working_directory",
    "workingdirectory": "working_directory",
    "arguments": "command_line_arguments",
    "args": "command_line_arguments",
    "command_line_arguments": "command_line_arguments",
    "commandlinearguments": "command_line_arguments",
    "icon_location": "icon_location",
    "iconlocation": "icon_location",
}

HEADER_SIZE = 0x4C
MAX_COUNT = 0xFFFF

# Name/RelativePath/WorkingDir are read with the 260-character UI limit by
# LnkParse3.  Writing more would make the rest of the copy parse differently,
# so these fields are kept within the limit that can be re-read faithfully.
READABLE_LIMITS = {
    "description": 260,
    "relative_path": 260,
    "working_directory": 260,
}

ERROR_HANDLER_ALIASES = {
    "error": "strict",
    "strict": "strict",
    "fail": "strict",
    "raise": "strict",
    "replace": "replace",
    "substitute": "replace",
    "?": "replace",
    "ignore": "ignore",
    "drop": "ignore",
    "skip": "ignore",
    "xmlcharrefreplace": "xmlcharrefreplace",
    "xml_char_ref_replace": "xmlcharrefreplace",
    "backslashreplace": "backslashreplace",
    "backslash_replace": "backslashreplace",
    "surrogateescape": "surrogateescape",
    "surrogate_escape": "surrogateescape",
    "namereplace": "namereplace",
    "name_replace": "namereplace",
}


class LnkEditError(LnkParserError):
    """A safe, verifiable rewrite cannot be produced."""


class UnsupportedCharacterError(LnkEditError, ValueError):
    """The new text cannot be represented in the requested encoding."""


class _Field:
    __slots__ = ("count", "end", "start")

    def __init__(self, start, end, count):
        self.start = start
        self.end = end
        self.count = count

    @property
    def payload_start(self):
        return self.start + 2

    @property
    def size(self):
        return self.end - self.start


class _ExtraBlock:
    __slots__ = ("end", "signature", "start")

    def __init__(self, start, end, signature):
        self.start = start
        self.end = end
        self.signature = signature


class LnkStringLayout:
    """Byte-accurate map of header, IDList, LinkInfo, StringData and ExtraData."""

    def __init__(self, data):
        self.data = bytes(data)
        self._parse()

    def _need(self, end, what):
        if end > len(self.data):
            raise LnkEditError(f"Truncated LNK file while reading {what}")

    def _parse(self):
        data = self.data
        if len(data) < HEADER_SIZE:
            raise LnkEditError("Truncated ShellLinkHeader")
        self._need(HEADER_SIZE, "ShellLinkHeader")

        header_size = struct.unpack_from("<I", data, 0)[0]
        if header_size != HEADER_SIZE:
            raise LnkEditError(f"Unexpected ShellLinkHeader size {header_size:#x}")
        if data[4:20] != LINK_CLS_ID:
            raise LnkEditError("Unexpected LinkCLSID")

        self.link_flags = struct.unpack_from("<I", data, 20)[0]
        self.is_unicode = bool(self.link_flags & 0x00000080)
        self.width = 2 if self.is_unicode else 1

        position = HEADER_SIZE

        self.target_id_list_start = None
        self.target_id_list_end = None
        if self.link_flags & 0x00000001:
            self._need(position + 2, "IDListSize")
            size = struct.unpack_from("<H", data, position)[0]
            end = position + 2 + size
            self._need(end, "LinkTargetIDList")
            self.target_id_list_start = position
            self.target_id_list_end = end
            position = end

        self.link_info_start = None
        self.link_info_end = None
        self.link_info_header_size = None
        self.link_info_flags = None
        # ForceNoLinkInfo only says the structure is ignored, it does not make
        # it absent; HasLinkInfo alone controls its presence.  LnkParse3 skips
        # the structure in this combination, so StringData offsets would be
        # read differently by the parser and by the file format.  Such a file
        # has no single trustworthy layout.
        if self.link_flags & 0x00000002 and self.link_flags & 0x00000100:
            raise LnkEditError(
                "HasLinkInfo and ForceNoLinkInfo are both set; the position of "
                "StringData cannot be established unambiguously"
            )
        if self.link_flags & 0x00000002:
            self._need(position + 0x1C, "LinkInfo header")
            size = struct.unpack_from("<I", data, position)[0]
            if size < 0x1C:
                raise LnkEditError(f"Invalid LinkInfoSize {size}")
            end = position + size
            self._need(end, "LinkInfo")
            header_size = struct.unpack_from("<I", data, position + 4)[0]
            if header_size < 0x1C or header_size > size:
                raise LnkEditError(f"Invalid LinkInfoHeaderSize {header_size:#x}")
            self.link_info_start = position
            self.link_info_end = end
            self.link_info_header_size = header_size
            self.link_info_flags = struct.unpack_from("<I", data, position + 8)[0]
            position = end

        self.string_data_start = position
        self.fields = {}
        self.order = []
        for mask, name in STRING_DATA_FIELDS:
            if not self.link_flags & mask:
                continue
            self._need(position + 2, f"{name} CountCharacters")
            count = struct.unpack_from("<H", data, position)[0]
            end = position + 2 + count * self.width
            self._need(end, f"{name} String")
            raw = data[position + 2 : end]
            # StringData.String is defined without a terminating NUL and
            # CountCharacters covers every character.  A trailing NUL therefore
            # either belongs to the value (and LnkParse3 would truncate it) or
            # indicates that the count does not describe this field.  Neither
            # can be rewritten faithfully.
            if self._contains_nul(raw):
                raise LnkEditError(
                    f"{name} contains a NUL within its declared {count} characters; "
                    f"the StringData boundary is not trustworthy"
                )
            limit = READABLE_LIMITS.get(name)
            if limit is not None and count > limit:
                # LnkParse3 only reads ``limit`` characters of this field and
                # then treats the remainder as the next structure.  Rewriting
                # such a file could not produce a faithfully re-readable copy.
                raise LnkEditError(
                    f"{name} declares {count} characters, beyond the {limit}-character "
                    f"limit that can be re-read unambiguously"
                )
            self.fields[name] = _Field(position, end, count)
            self.order.append(name)
            position = end
        self.string_data_end = position

        self.extra_data_start = position
        self.extra_blocks = []
        self.terminal_start = None
        self.appended_start = None
        limit = len(data)
        while position < limit:
            remaining = limit - position
            if remaining < 4:
                raise LnkEditError(
                    f"{remaining} trailing byte(s) after ExtraData cannot be verified"
                )
            size = struct.unpack_from("<I", data, position)[0]
            if size < 4:
                # TerminalBlock signature.  Everything after it is opaque and
                # is preserved verbatim, but nothing further is parsed.
                self.terminal_start = position
                self.appended_start = position + 4
                position = limit
                break
            if size > remaining:
                raise LnkEditError(
                    f"ExtraData block at {position} declares {size} bytes but only "
                    f"{remaining} remain"
                )
            if size < 8:
                raise LnkEditError(f"ExtraData block at {position} is too short")
            signature = struct.unpack_from("<I", data, position + 4)[0]
            self.extra_blocks.append(_ExtraBlock(position, position + size, signature))
            position += size
        if position != limit:
            raise LnkEditError("ExtraData does not end at the end of the file")

    def _contains_nul(self, payload):
        # Only aligned code units are meaningful; an odd-offset 00 00 arises
        # from two ordinary code units and is not a NUL character.
        return any(
            payload[index : index + self.width] == b"\x00" * self.width
            for index in range(0, len(payload), self.width)
        )

    def field(self, name):
        try:
            return self.fields[name]
        except KeyError:
            raise LnkEditError(f"Field {name!r} is not present in this link") from None


class LnkStringEditor:
    """Stage StringData replacements and serialize a verifiable copy.

    ``indata`` is only read.  ``cp`` (or its alias ``encoding``) names the ANSI
    code page, and ``unrepresentable`` (or ``errors``) selects what happens to
    characters the ANSI code page cannot encode: ``error``/``strict``
    (default), ``replace``, ``ignore``, ``xmlcharrefreplace``,
    ``backslashreplace``, ``surrogateescape`` or ``namereplace``.
    """

    def __init__(
        self,
        indata=None,
        cp=None,
        unrepresentable="error",
        *,
        encoding=None,
        errors=None,
    ):
        if indata is None:
            raise LnkEditError("Original LNK bytes are required")
        if encoding is not None:
            if cp is not None and str(cp).lower() != str(encoding).lower():
                raise ValueError("Specify either cp or encoding, not both")
            cp = encoding
        if errors is not None:
            if unrepresentable not in (None, "error"):
                raise ValueError(
                    "Specify either unrepresentable or errors, not both"
                )
            unrepresentable = errors

        if hasattr(indata, "read"):
            indata = indata.read()
        elif hasattr(indata, "indata"):
            # An already parsed LnkFile; its bytes are the authoritative copy.
            indata = indata.indata
        elif isinstance(indata, (str, os.PathLike)):
            with open(indata, "rb") as fh:
                indata = fh.read()

        self.indata = bytes(indata)
        self.cp = self._normalize_codec(cp)
        self.unrepresentable = self._normalize_handler(unrepresentable)
        self.layout = LnkStringLayout(self.indata)
        self._changes = {}
        self._codecs = {}
        self.source_path = None

        # Fail early on an unknown code page instead of at write time.
        if not self.layout.is_unicode:
            for candidate in self.cp:
                try:
                    "".encode(candidate)
                except LookupError as e:
                    raise LnkEditError(f"Unknown code page {candidate!r}") from e

    @staticmethod
    def _normalize_codec(cp):
        if cp is None:
            return ["cp1252"]
        if isinstance(cp, str):
            return [cp]
        codecs = list(cp)
        if not codecs:
            raise LnkEditError("At least one allowed code page must be given")
        if not all(isinstance(item, str) for item in codecs):
            raise TypeError("Code page names must be strings")
        return codecs

    @staticmethod
    def _normalize_handler(value):
        if value is None:
            value = "error"
        try:
            return ERROR_HANDLER_ALIASES[value]
        except KeyError:
            raise ValueError(
                f"Unsupported unrepresentable-character policy {value!r}; "
                f"expected one of {sorted(ERROR_HANDLER_ALIASES)}"
            ) from None

    # -- inspection -----------------------------------------------------
    def is_unicode(self):
        return self.layout.is_unicode

    def encoding(self):
        """The encoding used for new values (``utf-16-le`` for Unicode links)."""
        if self.layout.is_unicode:
            return "utf-16-le"
        return self.cp[0]

    def _active_codec(self, text):
        """Pick the first allowed ANSI code page that can encode ``text``."""
        for candidate in self.cp:
            try:
                text.encode(candidate, errors="strict")
            except LookupError as e:
                raise LnkEditError(f"Unknown code page {candidate!r}") from e
            except UnicodeEncodeError:
                continue
            return candidate
        return self.cp[0]

    def fields(self):
        return tuple(self.layout.order)

    def original_bytes(self, name):
        field = self.layout.field(self._canonical(name))
        return self.indata[field.start : field.end]

    def value(self, name):
        field = self.layout.field(self._canonical(name))
        return self._decode(self.indata[field.payload_start : field.end], name)

    def changed(self):
        return dict(self._changes)

    # -- editing --------------------------------------------------------
    def set(self, name, text):
        """Stage ``text`` for one StringData field."""
        name = self._canonical(name)
        self.layout.field(name)
        if not isinstance(text, str):
            raise TypeError("The replacement value must be a str")
        self._validate_text(name, text)
        self._changes[name] = text
        # Validate the concrete encoding now so an unknown or unsuitable code
        # page fails before anything is written.
        self.encoded_value(name)
        return self

    replace = set

    def _canonical(self, name):
        try:
            return STRING_DATA_ALIASES[name]
        except (KeyError, TypeError):
            raise LnkEditError(
                f"Unknown StringData field {name!r}; expected one of "
                f"{sorted(set(STRING_DATA_ALIASES.values()))}"
            ) from None

    def _validate_text(self, name, text):
        if "\x00" in text:
            raise LnkEditError(
                f"{name} cannot contain a NUL character; CountCharacters covers "
                f"only the characters before the end of the field"
            )
        if self.layout.is_unicode:
            try:
                encoded = text.encode("utf-16-le")
            except UnicodeEncodeError as e:
                raise UnsupportedCharacterError(
                    f"{name} contains an unpaired surrogate at character {e.start}"
                ) from e
        else:
            encoded = self._encode_ansi(name, text)
        count = len(encoded) // self.layout.width
        if count > MAX_COUNT:
            raise LnkEditError(
                f"{name} would need {count} characters, beyond the 16-bit "
                f"CountCharacters limit of {MAX_COUNT}"
            )
        limit = READABLE_LIMITS.get(name)
        if limit is not None and count > limit:
            raise LnkEditError(
                f"{name} would need {count} characters, beyond the {limit}-character "
                f"limit that can be re-read unambiguously"
            )

    def _encode_ansi(self, name, text):
        codec = self._active_codec(text)
        try:
            encoded = text.encode(codec, errors=self.unrepresentable)
        except UnicodeEncodeError as e:
            raise UnsupportedCharacterError(
                f"Character {text[e.start:e.end]!r} at position {e.start} of "
                f"{name} cannot be represented in {self.encoding()}"
            ) from e
        self._codecs[name] = codec
        return encoded

    def _decode(self, payload, name=None):
        if self.layout.is_unicode:
            return payload.decode("utf-16-le", errors="replace")
        codec = self._codecs.get(name, self.encoding())
        return payload.decode(codec, errors="replace")

    def encoded_value(self, name):
        name = self._canonical(name)
        if name in self._changes:
            text = self._changes[name]
            if self.layout.is_unicode:
                return text.encode("utf-16-le")
            return self._encode_ansi(name, text)
        field = self.layout.fields[name]
        return self.indata[field.payload_start : field.end]

    def staged_text(self, name):
        """The text that will actually be stored, after applying the policy."""
        return self._decode(self.encoded_value(name), name)

    def replacement_bytes(self, name):
        """Complete on-disk field (CountCharacters + String) for a value."""
        name = self._canonical(name)
        self.layout.field(name)
        encoded = self.encoded_value(name)
        count = len(encoded) // self.layout.width
        if count > MAX_COUNT:
            raise LnkEditError(f"{name} exceeds the CountCharacters limit")
        # String is not NULL-terminated; an empty value is CountCharacters=0.
        return struct.pack("<H", count) + encoded

    # -- serialization --------------------------------------------------
    def apply(self, replacements):
        """Stage a mapping of field names to values."""
        for name, text in replacements.items():
            self.set(name, text)
        return self

    def copy(self):
        """Return a new editor with the same options and no staged changes."""
        return type(self)(
            self.indata,
            cp=list(self.cp),
            unrepresentable=self.unrepresentable,
        )

    def to_bytes(self):
        """Return the rewritten LNK (the original bytes when nothing changed)."""
        if not self._changes:
            return self.indata

        pieces = []
        for name in self.layout.order:
            if name in self._changes:
                pieces.append(self.replacement_bytes(name))
            else:
                field = self.layout.fields[name]
                pieces.append(self.indata[field.start : field.end])
        new_string_data = b"".join(pieces)
        old_size = self.layout.string_data_end - self.layout.string_data_start
        delta = len(new_string_data) - old_size

        result = (
            self.indata[: self.layout.string_data_start]
            + new_string_data
            + self.indata[self.layout.string_data_end :]
        )
        self._verify(result, delta)
        return result

    def _verify(self, result, delta):
        rebuilt = LnkStringLayout(result)

        if rebuilt.link_flags != self.layout.link_flags:
            raise LnkEditError("LinkFlags changed during rewrite")
        if rebuilt.is_unicode != self.layout.is_unicode:
            raise LnkEditError("Encoding flag changed during rewrite")
        if rebuilt.order != self.layout.order:
            raise LnkEditError("StringData field set changed during rewrite")

        expected_start = self.layout.extra_data_start + delta
        if rebuilt.extra_data_start != expected_start:
            raise LnkEditError("StringData size change is inconsistent")
        if (
            rebuilt.terminal_start is not None
            and self.layout.terminal_start is not None
            and rebuilt.terminal_start - self.layout.terminal_start != delta
        ):
            raise LnkEditError("TerminalBlock offset is inconsistent")

        for name in self.layout.order:
            old_field = self.layout.fields[name]
            new_field = rebuilt.fields[name]
            old_raw = self.indata[old_field.start : old_field.end]
            new_raw = result[new_field.start : new_field.end]
            if name in self._changes:
                if new_raw != self.replacement_bytes(name):
                    raise LnkEditError(f"{name} was not encoded as requested")
                decoded = self._decode(new_raw[2:], name)
                if decoded != self.staged_text(name):
                    raise LnkEditError(
                        f"{name} does not round-trip to the new value"
                    )
            elif old_raw != new_raw:
                raise LnkEditError(f"Unchanged field {name!r} was modified")

        # Structures after StringData must be byte-identical.  There is no
        # stored offset in them that points at or beyond StringData:
        # SpecialFolder/KnownFolder offsets refer back to the LinkTargetIDList.
        old_tail = self.indata[self.layout.extra_data_start :]
        new_tail = result[rebuilt.extra_data_start :]
        if old_tail != new_tail:
            raise LnkEditError("ExtraData or appended bytes were not preserved")

        # Header, IDList and LinkInfo must be untouched.
        if result[: self.layout.string_data_start] != self.indata[: self.layout.string_data_start]:
            raise LnkEditError("Data before StringData was modified")

    def write(self, path):
        """Atomically write the copy to ``path``; never touches the original."""
        destination = Path(path)
        if self.source_path is not None:
            source = Path(self.source_path)
            if destination.resolve() == source.resolve():
                raise LnkEditError("Refusing to overwrite the original LNK")
            if destination.exists() and destination.samefile(source):
                raise LnkEditError("Refusing to overwrite the original LNK")

        data = self.to_bytes()
        if not destination.parent.is_dir():
            raise LnkEditError(
                f"Output directory does not exist: {destination.parent}"
            )
        tmp = destination.with_name(f"{destination.name}.tmp-{os.getpid()}")
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
            tmp.replace(destination)
        except BaseException:
            with suppress(OSError):
                tmp.unlink()
            raise
        return len(data)

    save = write


def load(path, cp=None, unrepresentable="error", *, encoding=None, errors=None):
    """Open ``path`` read-only and return an editor bound to a separate output."""
    with open(path, "rb") as fh:
        data = fh.read()
    editor = LnkStringEditor(
        data,
        cp=cp,
        unrepresentable=unrepresentable,
        encoding=encoding,
        errors=errors,
    )
    editor.source_path = os.fspath(path)
    return editor


def replace_strings(indata, replacements, cp=None, unrepresentable="error", **kwargs):
    """Return rewritten bytes for a mapping of field names to new values."""
    editor = LnkStringEditor(
        indata, cp=cp, unrepresentable=unrepresentable, **kwargs
    )
    for name, text in replacements.items():
        editor.set(name, text)
    return editor.to_bytes()


def rewrite(
    source,
    destination,
    replacements=None,
    *,
    working_directory=None,
    arguments=None,
    description=None,
    relative_path=None,
    icon_location=None,
    cp=None,
    unrepresentable="error",
    encoding=None,
    errors=None,
):
    """Edit named StringData fields of ``source`` into ``destination``.

    ``source`` and ``destination`` may be paths or raw bytes for ``source``.
    The original path is opened read-only.  Returns the number of bytes
    written.
    """
    values = dict(replacements or {})
    explicit = {
        "working_directory": working_directory,
        "command_line_arguments": arguments,
        "description": description,
        "relative_path": relative_path,
        "icon_location": icon_location,
    }
    values.update({name: value for name, value in explicit.items() if value is not None})

    if isinstance(source, (bytes, bytearray, memoryview)):
        data = bytes(source)
        editor = LnkStringEditor(
            data,
            cp=cp,
            unrepresentable=unrepresentable,
            encoding=encoding,
            errors=errors,
        )
    else:
        editor = load(
            source,
            cp=cp,
            unrepresentable=unrepresentable,
            encoding=encoding,
            errors=errors,
        )
    for name, text in values.items():
        editor.set(name, text)
    return editor.write(destination)
