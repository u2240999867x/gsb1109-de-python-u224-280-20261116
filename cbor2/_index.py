"""
Random-access indexing for archives of concatenated CBOR objects that use
string references (tags 25 and 256).

Acquisition pipelines often append one CBOR object after another to a file
while keeping a single running string reference table (the encoder side
equivalent of ``string_referencing=True`` with namespacing disabled). Reading
one object out of such an archive normally requires decoding everything from
the start of the file so that the string table is populated.

This module scans an archive once and builds a :class:`StringRefArchiveIndex`
which records, for every top-level object, its byte offset and the exact
portion of the running string table that was visible when the object started,
along with the source (object and offset) of every table entry. Any object can
then be decoded on its own by seeking to its offset and pre-seeding the
decoder with that local table, producing exactly the same values and reference
sources as one sequential pass over the file.

The index is bound to the file it was built from: every random access first
verifies a fingerprint of the file so that a stale table is never used to
interpret new bytes.
"""

from __future__ import annotations

import os
import struct
import sys
from hashlib import blake2b
from typing import IO, Any, Callable, Literal, NamedTuple, cast, overload

from ._decoder import (
    CBORDecoder,
    _stringref_is_referenced,
    major_decoders,
    semantic_decoders,
)
from ._decoder import loads as _payload_loads
from ._encoder import dumps as _payload_dumps
from ._types import CBORDecodeEOF, CBORDecodeValueError, CBORError, CBORTag

__all__ = [
    "FileFingerprint",
    "IndexFileChangedError",
    "ObjectInfo",
    "ResolvedReference",
    "StringRefArchiveIndex",
    "StringRefContextError",
    "StringRefIndexError",
    "StringRefRangeError",
    "StringRefSource",
]


class StringRefIndexError(CBORError):
    """Base class for errors raised by the string reference archive index."""


class StringRefContextError(StringRefIndexError):
    """
    A string reference was resolved while no string table context was active
    for the object in question.
    """


class StringRefRangeError(StringRefIndexError, IndexError):
    """A string reference pointed beyond the bounds of the local string table."""


class IndexFileChangedError(StringRefIndexError):
    """
    The file no longer matches the fingerprint stored in the index, so the
    index must not be used to interpret its bytes.
    """


class StringRefSource(NamedTuple):
    """Describes where one entry of the running string table was introduced."""

    table_index: int  #: index of the entry in the running string table
    object_index: int  #: ordinal of the top-level object that introduced it
    offset: int  #: file offset of the encoded string item
    string_length: int  #: byte length of the string content


class ObjectInfo(NamedTuple):
    """Index record for one top-level object in the archive."""

    ordinal: int  #: ordinal of the object in the archive
    offset: int  #: file offset at which the encoded object starts
    length: int  #: byte length of the encoded object
    table_size: int  #: number of outer table entries visible when the object starts
    has_context: bool  #: whether a string table context is active for this object


class FileFingerprint(NamedTuple):
    """Identifies the exact file version an index was built from."""

    size: int  #: file size in bytes at index time
    mtime_ns: int | None  #: modification time in nanoseconds, if the file has one
    digest: str  #: blake2b hex digest of the scanned bytes


class ResolvedReference(NamedTuple):
    """One string reference resolved while replaying an object."""

    table_index: int  #: index the reference pointed to
    value: str | bytes  #: the resolved value
    source: StringRefSource | None  #: where it came from (None = the object itself)


class _StreamReader:
    """Reads from a binary stream while tracking position and a running digest."""

    __slots__ = ("_fp_read", "pos", "_hasher")

    def __init__(self, fp: IO[bytes]):
        self._fp_read: Callable[[int], bytes] = fp.read
        self.pos = 0
        self._hasher = blake2b()

    @property
    def digest(self) -> str:
        return self._hasher.hexdigest()

    def read(self, amount: int) -> bytes:
        """Read exactly *amount* bytes, raising :exc:`CBORDecodeEOF` if the
        stream ends first."""
        chunks: list[bytes] = []
        left = amount
        while left > 0:
            data = self._fp_read(min(left, 65536))
            if not data:
                raise CBORDecodeEOF(
                    f"premature end of stream (expected to read {amount} bytes, got "
                    f"{amount - left} instead)"
                )

            chunks.append(data)
            self.pos += len(data)
            self._hasher.update(data)
            left -= len(data)

        return chunks[0] if len(chunks) == 1 else b"".join(chunks)

    def read_upto(self, amount: int) -> bytes:
        """Read up to *amount* bytes, returning fewer only at end of stream."""
        data = self._fp_read(amount)
        self.pos += len(data)
        self._hasher.update(data)
        return data

    def skip(self, amount: int) -> None:
        """Skip exactly *amount* bytes without materializing them."""
        left = amount
        while left > 0:
            data = self._fp_read(min(left, 65536))
            if not data:
                raise CBORDecodeEOF(
                    f"premature end of stream (expected to skip {amount} bytes, got "
                    f"{amount - left} instead)"
                )

            self.pos += len(data)
            self._hasher.update(data)
            left -= len(data)


class _StringTableScanner:
    """
    Walks a stream of concatenated CBOR objects, tracking the byte extent of
    every top-level object and the evolution of the string reference table.

    The scanner applies the exact same table admission rules as the decoder
    (only definite-length strings, only when referencing pays off for the
    table index they would receive) and maintains a namespace stack so that
    entering and leaving a nested stringref-namespace (tag 256) never affects
    the outer table.
    """

    def __init__(
        self,
        fp: IO[bytes],
        *,
        outer_namespace: bool,
        str_errors: Literal["strict", "error", "replace"],
    ):
        self._reader = _StreamReader(fp)
        self._str_errors = str_errors
        self._outer: list[str | bytes] | None = [] if outer_namespace else None
        self._stack: list[list[str | bytes]] = []
        # Log of the outer table; aliases self._outer when there is one
        self.strings: list[str | bytes] = self._outer if self._outer is not None else []
        self.sources: list[StringRefSource] = []
        self.objects: list[ObjectInfo] = []
        self._current_object = 0

    @property
    def position(self) -> int:
        return self._reader.pos

    @property
    def digest(self) -> str:
        return self._reader.digest

    @property
    def _namespace(self) -> list[str | bytes] | None:
        return self._stack[-1] if self._stack else self._outer

    def scan(self) -> None:
        """Scan the stream from its current position to the end."""
        while True:
            initial = self._reader.read_upto(1)
            if not initial:
                return

            start = self._reader.pos - 1
            table_size = len(self._outer) if self._outer is not None else 0
            self._current_object = len(self.objects)
            self._scan_item(initial[0])
            self.objects.append(
                ObjectInfo(
                    ordinal=self._current_object,
                    offset=start,
                    length=self._reader.pos - start,
                    table_size=table_size,
                    has_context=self._outer is not None,
                )
            )

    def _read_initial(self) -> int:
        return self._reader.read(1)[0]

    @overload
    def _decode_length(self, subtype: int) -> int: ...

    @overload
    def _decode_length(self, subtype: int, allow_indefinite: Literal[True]) -> int | None: ...

    def _decode_length(self, subtype: int, allow_indefinite: bool = False) -> int | None:
        if subtype < 24:
            return subtype
        elif subtype == 24:
            return self._reader.read(1)[0]
        elif subtype == 25:
            return cast(int, struct.unpack(">H", self._reader.read(2))[0])
        elif subtype == 26:
            return cast(int, struct.unpack(">L", self._reader.read(4))[0])
        elif subtype == 27:
            return cast(int, struct.unpack(">Q", self._reader.read(8))[0])
        elif subtype == 31 and allow_indefinite:
            return None
        else:
            raise CBORDecodeValueError(f"unknown unsigned integer subtype 0x{subtype:x}")

    def _scan_item(self, initial_byte: int) -> Any:
        """Scan one item whose initial byte has just been consumed.

        Returns the value of unsigned/negative integers (needed to validate
        string reference indexes); anything else returns ``None``.
        """
        major_type = initial_byte >> 5
        subtype = initial_byte & 31
        if major_type == 0:
            return self._decode_length(subtype)
        elif major_type == 1:
            return -1 - self._decode_length(subtype)
        elif major_type in (2, 3):
            self._scan_string(subtype, is_bytes=major_type == 2, start=self._reader.pos - 1)
        elif major_type == 4:
            length = self._decode_length(subtype, allow_indefinite=True)
            if length is None:
                while True:
                    initial = self._read_initial()
                    if initial == 0xFF:
                        break

                    self._scan_item(initial)
            else:
                if length > sys.maxsize:
                    raise CBORDecodeValueError(f"invalid length for array 0x{length:x}")

                for _ in range(length):
                    self._scan_item(self._read_initial())
        elif major_type == 5:
            length = self._decode_length(subtype, allow_indefinite=True)
            if length is None:
                while True:
                    initial = self._read_initial()
                    if initial == 0xFF:
                        break

                    self._scan_item(initial)
                    self._scan_item(self._read_initial())
            else:
                if length > sys.maxsize:
                    raise CBORDecodeValueError(f"invalid length for map 0x{length:x}")

                for _ in range(length * 2):
                    self._scan_item(self._read_initial())
        elif major_type == 6:
            tagnum = self._decode_length(subtype)
            if tagnum == 256:
                # A new string reference namespace: it starts empty and is
                # discarded when the tag content ends, so it can never affect
                # the outer table.
                self._stack.append([])
                try:
                    self._scan_item(self._read_initial())
                finally:
                    self._stack.pop()
            elif tagnum == 25:
                self._check_stringref(self._scan_item(self._read_initial()))
            else:
                self._scan_item(self._read_initial())
        else:
            # Major type 7 (simple values and floats)
            if subtype == 31:
                pass  # break marker: a single byte, like the decoder treats it
            elif subtype < 20:
                pass
            elif subtype <= 23:
                pass
            elif subtype == 24:
                self._reader.skip(1)
            elif subtype == 25:
                self._reader.skip(2)
            elif subtype == 26:
                self._reader.skip(4)
            elif subtype == 27:
                self._reader.skip(8)
            else:
                raise CBORDecodeValueError(
                    f"Undefined Reserved major type 7 subtype 0x{subtype:x}"
                )

        return None

    def _scan_string(self, subtype: int, *, is_bytes: bool, start: int) -> None:
        length = self._decode_length(subtype, allow_indefinite=True)
        if length is None:
            # Indefinite length string: neither the chunks nor the assembled
            # value are ever entered into the string table, so the bytes are
            # consumed and discarded.
            expected_major = 2 if is_bytes else 3
            while True:
                initial = self._read_initial()
                if initial == 0xFF:
                    return
                elif initial >> 5 != expected_major:
                    if is_bytes:
                        raise CBORDecodeValueError(
                            "non-bytestring found in indefinite length bytestring"
                        )
                    else:
                        raise CBORDecodeValueError("non-string found in indefinite length string")

                chunk_length = self._decode_length(initial & 31)
                if chunk_length is None or chunk_length > sys.maxsize:
                    raise CBORDecodeValueError(
                        f"invalid length for indefinite string chunk 0x{chunk_length:x}"
                    )

                self._reader.skip(chunk_length)

        if length > sys.maxsize:
            kind = "bytestring" if is_bytes else "string"
            raise CBORDecodeValueError(f"invalid length for {kind} 0x{length:x}")

        namespace = self._namespace
        if namespace is None or not _stringref_is_referenced(len(namespace), length):
            self._reader.skip(length)
            return

        data = self._reader.read(length)
        if is_bytes:
            value: str | bytes = data
        else:
            try:
                value = data.decode("utf-8", self._str_errors)
            except UnicodeDecodeError as exc:
                raise CBORDecodeValueError("error decoding unicode string") from exc

        namespace.append(value)
        if namespace is self._outer:
            self.sources.append(
                StringRefSource(
                    table_index=len(self.strings) - 1,
                    object_index=self._current_object,
                    offset=start,
                    string_length=length,
                )
            )

    def _check_stringref(self, index: Any) -> None:
        namespace = self._namespace
        if namespace is None:
            raise CBORDecodeValueError("string reference outside of namespace")

        if not isinstance(index, int):
            raise CBORDecodeValueError("string reference index is not an integer")

        try:
            namespace[index]
        except IndexError:
            raise CBORDecodeValueError(f"string reference {index} not found")


class _TracingDecoder(CBORDecoder):
    """Decoder that records every string reference it resolves."""

    def __init__(
        self,
        *args: Any,
        references: list[tuple[int, str | bytes]],
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.references = references

    def _decode(self, immutable: bool = False, unshared: bool = False) -> Any:
        # The decoder dispatches through module level function tables bound to
        # the base class, so semantic tags have to be intercepted here for the
        # decode_stringref override below to take effect
        if immutable:
            old_immutable = self._immutable
            self._immutable = True
        if unshared:
            old_index = self._share_index
            self._share_index = None
        try:
            initial_byte = self.read(1)[0]
            major_type = initial_byte >> 5
            subtype = initial_byte & 31
            if major_type == 6:
                return self.decode_semantic(subtype)

            return major_decoders[major_type](self, subtype)
        finally:
            if immutable:
                self._immutable = old_immutable
            if unshared:
                self._share_index = old_index

    def decode_semantic(self, subtype: int) -> Any:
        tagnum = self._decode_length(subtype)
        if tagnum == 25:
            return self.decode_stringref()

        if semantic_decoder := semantic_decoders.get(tagnum):
            return semantic_decoder(self)

        tag = CBORTag(tagnum, None)
        self.set_shareable(tag)
        tag.value = self._decode(unshared=True)
        if self._tag_hook:
            tag = self._tag_hook(self, tag)

        return self.set_shareable(tag)

    def decode_stringref(self) -> str | bytes:
        # Semantic tag 25
        if self._stringref_namespace is None:
            raise CBORDecodeValueError("string reference outside of namespace")

        index: int = self._decode()
        try:
            value = self._stringref_namespace[index]
        except IndexError:
            raise CBORDecodeValueError(f"string reference {index} not found")

        self.references.append((index, value))
        return value


class StringRefArchiveIndex:
    """
    Index over an archive of concatenated CBOR objects sharing one running
    string reference table.

    Use :meth:`build` to scan an archive and create the index. Each top-level
    object can then be read on its own with :meth:`read_object`, which seeks
    directly to the object and replays it against the local string table that
    was in effect when the object started, yielding the same values and
    reference sources as one sequential pass over the file.

    The index is bound to the file version it was built from; if the file
    changes afterwards, :meth:`check_file` (and therefore :meth:`read_object`)
    raises :exc:`IndexFileChangedError` instead of interpreting new bytes with
    a stale table.
    """

    __slots__ = (
        "outer_namespace",
        "strings",
        "sources",
        "objects",
        "fingerprint",
        "_start_offset",
        "_scanned_length",
    )

    def __init__(
        self,
        *,
        outer_namespace: bool,
        strings: tuple[str | bytes, ...],
        sources: tuple[StringRefSource, ...],
        objects: tuple[ObjectInfo, ...],
        fingerprint: FileFingerprint,
        start_offset: int,
        scanned_length: int,
    ):
        #: whether the archive maintains a running table across top-level objects
        self.outer_namespace = outer_namespace
        #: every string entered into the running (outer) table, in table order
        self.strings = strings
        #: where each entry of ``strings`` was introduced
        self.sources = sources
        #: one record per top-level object in the archive
        self.objects = objects
        #: fingerprint of the file version this index was built from
        self.fingerprint = fingerprint
        self._start_offset = start_offset
        self._scanned_length = scanned_length

    def __len__(self) -> int:
        return len(self.objects)

    @classmethod
    def build(
        cls,
        source: str | os.PathLike[str] | IO[bytes],
        *,
        outer_namespace: bool = True,
        str_errors: Literal["strict", "error", "replace"] = "strict",
    ) -> StringRefArchiveIndex:
        """
        Scan an archive from the current stream position to the end of the
        stream and build an index over its top-level objects.

        :param source:
            a path, or a binary file-like object positioned at the start of
            the archive
        :param outer_namespace:
            whether the archive keeps one running string table across
            top-level objects (the layout produced by an encoder with string
            referencing enabled and namespacing disabled). When ``False``,
            only namespaces explicitly opened with tag 256 provide string
            table context.
        :param str_errors:
            how to handle unicode decoding errors while scanning (same values
            as for :class:`~cbor2.CBORDecoder`)
        :raises CBORDecodeError:
            if the archive is malformed, for example if it contains a string
            reference with no active namespace or one that points beyond the
            table built so far
        """
        if isinstance(source, (str, os.PathLike)):
            with open(source, "rb") as file_obj:
                return cls.build(file_obj, outer_namespace=outer_namespace, str_errors=str_errors)

        fp = source
        try:
            start_offset = fp.tell()
        except (AttributeError, OSError):
            start_offset = 0

        try:
            stat_result = os.fstat(fp.fileno())
        except (AttributeError, OSError):
            stat_result = None

        scanner = _StringTableScanner(fp, outer_namespace=outer_namespace, str_errors=str_errors)
        scanner.scan()

        scanned_length = scanner.position - start_offset
        fingerprint = FileFingerprint(
            size=stat_result.st_size if stat_result is not None else scanner.position,
            mtime_ns=stat_result.st_mtime_ns if stat_result is not None else None,
            digest=scanner.digest,
        )
        return cls(
            outer_namespace=outer_namespace,
            strings=tuple(scanner.strings),
            sources=tuple(scanner.sources),
            objects=tuple(scanner.objects),
            fingerprint=fingerprint,
            start_offset=start_offset,
            scanned_length=scanned_length,
        )

    def local_table(self, object_index: int) -> list[str | bytes] | None:
        """
        Return the local string table that was in effect when the given object
        started, i.e. the prefix of the running table needed to decode it.

        Returns ``None`` if the object has no string table context at all
        (context missing); an empty list means a context exists but no strings
        had been entered yet — a valid state, distinct from a missing context.
        """
        info = self.objects[object_index]
        if not info.has_context:
            return None

        return list(self.strings[: info.table_size])

    def resolve(self, object_index: int, table_index: int) -> str | bytes:
        """
        Resolve a string reference made by the given object against its local
        string table.

        :raises StringRefContextError: if the object has no string table context
        :raises StringRefRangeError: if the reference points beyond the local table
        :return: the referenced string (a complete value, returned as-is even
            if it is an empty string)
        """
        info = self.objects[object_index]
        if not info.has_context:
            raise StringRefContextError(f"object {object_index} has no string table context")

        table = self.strings[: info.table_size]
        try:
            return table[table_index]
        except IndexError:
            raise StringRefRangeError(
                f"string reference {table_index} not found in local table of "
                f"size {info.table_size}"
            ) from None

    def check_file(self, fp: IO[bytes], *, deep: bool = False) -> None:
        """
        Verify that *fp* still is the file version this index was built from.

        When the file has stat information and its size and modification time
        still match the fingerprint, the check passes immediately (unless
        ``deep=True``). Otherwise the content from the scanned position to the
        end of the stream is re-hashed and compared against the stored digest,
        which is the authoritative check.

        :raises IndexFileChangedError: if the file does not match the fingerprint
        :raises StringRefIndexError: if the file identity cannot be verified
        """
        try:
            stat_result = os.fstat(fp.fileno())
        except (AttributeError, OSError):
            stat_result = None

        stat_matches = (
            stat_result is not None
            and self.fingerprint.mtime_ns is not None
            and stat_result.st_size == self.fingerprint.size
            and stat_result.st_mtime_ns == self.fingerprint.mtime_ns
        )
        if stat_matches and not deep:
            return

        try:
            position = fp.tell()
            fp.seek(self._start_offset)
        except (AttributeError, OSError) as exc:
            if stat_matches:
                return  # the stat check passed; this stream cannot be hashed

            raise StringRefIndexError(
                "cannot verify the file against the index: the stream is not seekable"
            ) from exc

        hasher = blake2b()
        while True:
            chunk = fp.read(65536)
            if not chunk:
                break

            hasher.update(chunk)

        fp.seek(position)
        if hasher.hexdigest() != self.fingerprint.digest:
            raise IndexFileChangedError(
                "the file has changed since the index was built; rebuild the "
                "index instead of interpreting new bytes with the old table"
            )

    def read_object(
        self,
        source: str | os.PathLike[str] | IO[bytes],
        object_index: int,
        *,
        tag_hook: Callable[[CBORDecoder, CBORTag], Any] | None = None,
        object_hook: Callable[[CBORDecoder, dict[Any, Any]], Any] | None = None,
        str_errors: Literal["strict", "error", "replace"] = "strict",
        trace: list[ResolvedReference] | None = None,
    ) -> Any:
        """
        Decode a single top-level object from the archive without decoding the
        objects before it.

        The file is verified against the index fingerprint, the stream is
        positioned directly at the object, and the decoder is seeded with the
        local string table recorded for it, so the returned value — and the
        source of every string reference in it — matches one sequential pass
        over the file.

        :param source: a path, or a seekable binary file-like object
        :param object_index: ordinal of the object to decode
        :param trace:
            if a list is given, every string reference resolved while decoding
            is appended to it as a :class:`ResolvedReference` (whose ``source``
            is ``None`` for strings introduced by the object itself)
        :raises IndexFileChangedError: if the file changed since the index was built
        """
        if isinstance(source, (str, os.PathLike)):
            with open(source, "rb") as file_obj:
                return self.read_object(
                    file_obj,
                    object_index,
                    tag_hook=tag_hook,
                    object_hook=object_hook,
                    str_errors=str_errors,
                    trace=trace,
                )

        fp = source
        self.check_file(fp)
        info = self.objects[object_index]
        fp.seek(info.offset)

        references: list[tuple[int, str | bytes]] | None = [] if trace is not None else None
        decoder: CBORDecoder
        if references is None:
            decoder = CBORDecoder(
                fp, tag_hook=tag_hook, object_hook=object_hook, str_errors=str_errors
            )
        else:
            decoder = _TracingDecoder(
                fp,
                tag_hook=tag_hook,
                object_hook=object_hook,
                str_errors=str_errors,
                references=references,
            )

        if info.has_context:
            decoder._stringref_namespace = list(self.strings[: info.table_size])

        value = decoder.decode()
        if fp.tell() != info.offset + info.length:
            raise StringRefIndexError(
                "the decoded object does not match the indexed extent; the file "
                "may have been modified since the index was built"
            )

        if trace is not None and references is not None:
            for table_index, ref_value in references:
                if 0 <= table_index < info.table_size:
                    ref_source: StringRefSource | None = self.sources[table_index]
                else:
                    ref_source = None

                trace.append(ResolvedReference(table_index, ref_value, ref_source))

        return value

    def dumps(self) -> bytes:
        """Serialize the index to bytes (see :meth:`loads`)."""
        payload = {
            "version": 1,
            "outer_namespace": self.outer_namespace,
            "start_offset": self._start_offset,
            "scanned_length": self._scanned_length,
            "fingerprint": {
                "size": self.fingerprint.size,
                "mtime_ns": self.fingerprint.mtime_ns,
                "digest": self.fingerprint.digest,
            },
            "strings": list(self.strings),
            "sources": [list(source) for source in self.sources],
            "objects": [list(info) for info in self.objects],
        }
        return _payload_dumps(payload)

    @classmethod
    def loads(cls, data: bytes) -> StringRefArchiveIndex:
        """Rebuild an index previously serialized with :meth:`dumps`."""
        payload = _payload_loads(data)
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise StringRefIndexError("not a version 1 string reference archive index")

        fingerprint = payload["fingerprint"]
        return cls(
            outer_namespace=payload["outer_namespace"],
            strings=tuple(payload["strings"]),
            sources=tuple(StringRefSource(*source) for source in payload["sources"]),
            objects=tuple(ObjectInfo(*info) for info in payload["objects"]),
            fingerprint=FileFingerprint(
                size=fingerprint["size"],
                mtime_ns=fingerprint["mtime_ns"],
                digest=fingerprint["digest"],
            ),
            start_offset=payload["start_offset"],
            scanned_length=payload["scanned_length"],
        )
