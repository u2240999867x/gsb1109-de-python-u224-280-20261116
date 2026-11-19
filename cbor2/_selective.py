from __future__ import annotations

import re
import struct
import sys
from binascii import hexlify
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.parser import Parser
from fractions import Fraction
from io import BytesIO
from ipaddress import ip_address, ip_network
from typing import IO, Any, cast
from uuid import UUID

from ._types import (
    CBORDecodeEOF,
    CBORDecodeError,
    CBORDecodeValueError,
    FrozenDict,
    undefined,
)

try:  # prefer the objects exposed by the active C extension
    import _cbor2 as _cext
except ImportError:  # pragma: no cover - covered by the pure Python test pass
    _cext = None

if _cext is not None:
    CBORTag = _cext.CBORTag
    CBORSimpleValue = _cext.CBORSimpleValue
    CBORDecodeError = _cext.CBORDecodeError
    CBORDecodeValueError = _cext.CBORDecodeValueError
    CBORDecodeEOF = _cext.CBORDecodeEOF
else:  # pragma: no cover
    from ._types import CBORSimpleValue as CBORSimpleValue
    from ._types import CBORTag as CBORTag

timestamp_re = re.compile(
    r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)"
    r"(?:\.(\d{1,6})\d*)?(?:Z|([+-])(\d\d):(\d\d))$"
)

_KNOWN_TAGS = frozenset(
    {0, 1, 2, 3, 4, 5, 25, 28, 29, 30, 35, 36, 37, 100, 256, 258, 260, 261, 1004, 55799}
)
_MISSING = object()
_UNFINISHED = object()
_NO_CACHE = object()


class TagKey:
    """
    A typed path component selecting the payload of a tag.

    Unknown tags are kept as :class:`~cbor2.CBORTag` values; their payloads are
    never implicitly interpreted as maps.  ``TagKey(4242)`` opts in to
    descending into the payload of tag 4242.
    """

    __slots__ = ("tag",)

    def __init__(self, tag: int) -> None:
        if type(tag) is not int or not 0 <= tag < 2**64:
            raise TypeError("tag must be a non-negative integer below 2**64")
        self.tag = tag

    def __eq__(self, other: object) -> bool:
        return isinstance(other, TagKey) and self.tag == other.tag

    def __hash__(self) -> int:
        return hash((TagKey, self.tag))

    def __repr__(self) -> str:
        return f"TagKey({self.tag})"


@dataclass(frozen=True)
class SelectiveSource:
    """
    A definition fetched from outside the selected payload.

    :ivar kind: ``"shared"`` or ``"stringref"``
    :ivar index: the value-sharing or string-table index
    :ivar offset: byte offset of the definition in the source
    """

    kind: str
    index: int
    offset: int


@dataclass
class SelectiveResult:
    """
    The result of a selective read.

    ``values`` contains only fully expanded values.  Paths that could not be
    proven are reported in ``unfinished`` together with an explanation in
    ``errors``; a partial value is never published as if it were complete.
    ``missing`` contains paths that are definitively absent.
    """

    values: _PathMapping
    unfinished: tuple[tuple[Any, ...], ...] = ()
    missing: tuple[tuple[Any, ...], ...] = ()
    errors: dict[tuple[Any, ...], str] = field(default_factory=dict)
    sources: tuple[SelectiveSource, ...] = ()
    complete: bool = True
    scan_error: str | None = None
    exhausted: tuple[str, ...] = ()

    def __getitem__(self, path: Sequence[Any]) -> Any:
        return self.values[path]

    def get(self, path: Sequence[Any], default: Any = None) -> Any:
        return self.values.get(tuple(path), default)

    @property
    def incomplete(self) -> tuple[tuple[Any, ...], ...]:
        """Alias for :attr:`unfinished`."""
        return self.unfinished

    @property
    def value(self) -> Any:
        """The first selected value, or :data:`MISSING` if none was expanded."""
        return next(iter(self.values.values()), MISSING)


class _PathMapping(Mapping[tuple[Any, ...], Any]):
    """A mapping of typed paths that does not conflate ``True`` with ``1``."""

    def __init__(self, items: Iterable[tuple[tuple[Any, ...], Any]]) -> None:
        self._items = list(items)

    def _find(self, key: tuple[Any, ...]) -> tuple[int, Any] | None:
        for index, (candidate, value) in enumerate(self._items):
            if _paths_equal(candidate, key):
                return index, value
        return None

    def __getitem__(self, key: Sequence[Any]) -> Any:
        found = self._find(tuple(key))
        if found is None:
            raise KeyError(key)
        return found[1]

    def __iter__(self) -> Iterator[tuple[Any, ...]]:
        return (path for path, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, (tuple, list)) and self._find(tuple(key)) is not None

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping) and len(other) == len(self):
            return all(
                any(
                    _paths_equal(path, tuple(other_path)) and _typed_equal(value, other_value)
                    for other_path, other_value in other.items()
                )
                for path, value in self._items
            )
        return NotImplemented

    def __repr__(self) -> str:
        # Do not render through dict(): that would collapse True with 1 and
        # 1 with 1.0, hiding the typed paths this mapping exists to preserve.
        return "{" + ", ".join(f"{path!r}: {value!r}" for path, value in self._items) + "}"

    def get(self, key: Sequence[Any], default: Any = None) -> Any:
        found = self._find(tuple(key))
        return default if found is None else found[1]

    def items(self) -> Iterator[tuple[tuple[Any, ...], Any]]:  # type: ignore[override]
        return iter(self._items)


# Sentinel for an absent result value, distinct from CBOR's ``undefined``.
MISSING = object()


class _BudgetExceeded(Exception):
    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind


class _Reader:
    """A byte cursor that can skip payloads on seekable sources."""

    def __init__(
        self,
        source: bytes | bytearray | memoryview | IO[bytes],
        read_budget: int | None = None,
    ) -> None:
        self._buffer: bytes | None = None
        self._view: memoryview | None = None
        self._fp: IO[bytes] | None = None
        self._pos = 0
        self._length: int | None = None
        self.read_budget = read_budget
        self.bytes_read = 0
        # Non-seekable input is charged while it is buffered, so later reads
        # from that buffer are free.  If the budget cut the buffer short, a
        # read past the buffered prefix is a budget failure, not corruption.
        self._precharged = False
        self._truncated = False

        if isinstance(source, (bytes, bytearray, memoryview)):
            self._buffer = bytes(source)
            self._view = memoryview(self._buffer)
            self._length = len(self._buffer)
        else:
            self._fp = source
            try:
                start = source.tell()
                source.seek(0, 2)
                end = source.tell()
                source.seek(start)
            except (AttributeError, OSError, ValueError):
                # Buffer a non-seekable stream so that referenced definitions
                # can still be fetched later.  The cap here only protects
                # against over-reading; actual reads are charged by read(),
                # read_at() and skip().
                chunks: list[bytes] = []
                remaining_limit = read_budget
                while True:
                    chunk_size = 65536
                    if remaining_limit is not None:
                        if remaining_limit <= 0:
                            self._truncated = True
                            break
                        chunk_size = min(chunk_size, remaining_limit)
                    try:
                        chunk = source.read(chunk_size)
                    except AttributeError:
                        raise ValueError("source has no read method") from None
                    if not chunk:
                        break
                    if not isinstance(chunk, (bytes, bytearray)):
                        raise TypeError("source read() must return bytes")
                    chunks.append(bytes(chunk))
                    self.bytes_read += len(chunk)
                    if remaining_limit is not None:
                        remaining_limit -= len(chunk)
                    if len(chunk) < chunk_size:
                        break
                self._buffer = b"".join(chunks)
                self._view = memoryview(self._buffer)
                self._length = len(self._buffer)
                self._fp = None
                self._precharged = True
            else:
                self._length = end
                self._pos = start

    @property
    def position(self) -> int:
        return self._pos

    @property
    def length(self) -> int | None:
        return self._length

    @property
    def exhausted(self) -> bool:
        if self._truncated:
            return True
        return self.read_budget is not None and self.bytes_read >= self.read_budget

    def _charge(self, amount: int) -> None:
        if amount < 0:
            raise ValueError("cannot read a negative amount")
        if self._precharged:
            return
        if self.read_budget is not None:
            if amount > self.read_budget - self.bytes_read:
                raise _BudgetExceeded("read")
        self.bytes_read += amount

    def read(self, amount: int) -> bytes:
        if amount == 0:
            return b""
        self._charge(amount)
        if self._view is not None:
            end = self._pos + amount
            if end > len(self._view):
                if self._truncated:
                    raise _BudgetExceeded("read")
                available = max(len(self._view) - self._pos, 0)
                raise CBORDecodeEOF(
                    f"premature end of stream (expected to read {amount} bytes, got "
                    f"{available} instead)"
                )
            data = bytes(self._view[self._pos : end])
            self._pos = end
            return data

        assert self._fp is not None
        data = self._fp.read(amount)
        if not isinstance(data, (bytes, bytearray)):
            raise CBORDecodeEOF("input stream returned a non-bytes value")
        if len(data) < amount:
            raise CBORDecodeEOF(
                f"premature end of stream (expected to read {amount} bytes, got "
                f"{len(data)} instead)"
            )
        self._pos += amount
        return bytes(data)

    def skip(self, amount: int) -> None:
        if amount < 0:
            raise ValueError("cannot skip a negative amount")
        if self._view is not None:
            end = self._pos + amount
            if end > len(self._view):
                if self._truncated:
                    raise _BudgetExceeded("read")
                raise CBORDecodeEOF("premature end of stream while skipping data")
            self._pos = end
            return

        assert self._fp is not None
        end = self._pos + amount
        if self._length is not None and end > self._length:
            raise CBORDecodeEOF("premature end of stream while skipping data")
        try:
            self._fp.seek(amount, 1)
        except (AttributeError, OSError, ValueError):
            self.read(amount)
        else:
            self._pos = end

    def read_at(self, offset: int, amount: int) -> bytes:
        if offset < 0 or amount < 0:
            raise ValueError("offset and amount must be non-negative")
        self._charge(amount)
        if self._view is not None:
            end = offset + amount
            if end > len(self._view):
                if self._truncated:
                    raise _BudgetExceeded("read")
                raise CBORDecodeEOF("premature end of stream while reading data")
            return bytes(self._view[offset:end])

        assert self._fp is not None
        current = self._fp.tell()
        self._fp.seek(offset)
        try:
            data = self._fp.read(amount)
        finally:
            self._fp.seek(current)
        if len(data) < amount:
            raise CBORDecodeEOF("premature end of stream while reading data")
        return bytes(data)

    def _at_end(self) -> bool:
        return self._length is not None and self._pos >= self._length


@dataclass
class _StringEntry:
    offset: int
    length: int
    is_bytes: bool
    value: Any = None
    loaded: bool = False


class _StringTable:
    __slots__ = ("entries", "complete")

    def __init__(self) -> None:
        self.entries: list[_StringEntry] = []
        self.complete = True

    def add(self, offset: int, length: int, is_bytes: bool) -> None:
        next_index = len(self.entries)
        if next_index < 24:
            is_referenced = length >= 3
        elif next_index < 256:
            is_referenced = length >= 4
        elif next_index < 65536:
            is_referenced = length >= 5
        elif next_index < 4294967296:
            is_referenced = length >= 7
        else:
            is_referenced = length >= 11

        if is_referenced:
            self.entries.append(_StringEntry(offset, length, is_bytes))


@dataclass
class _Node:
    offset: int
    major: int
    subtype: int
    complete: bool = True
    payload_offset: int | None = None
    length: int | None = None
    indefinite: bool = False
    chunks: list[tuple[int, int, bool]] = field(default_factory=list)
    children: list[_Node] = field(default_factory=list)
    entries: list[tuple[_Node, _Node]] = field(default_factory=list)
    value: Any = None
    tag: int | None = None
    share_index: int | None = None
    string_table: _StringTable | None = None


class _Scanner:
    def __init__(self, reader: _Reader) -> None:
        self.reader = reader
        self.share_defs: list[_Node] = []
        self.error: str | None = None
        self.budget_exhausted: str | None = None
        self._namespace_stack: list[_StringTable] = []

    def scan_document(self) -> _Node | None:
        if self.reader._at_end():
            return None
        return self._scan_item()

    def _scan_length(self, subtype: int, allow_indefinite: bool = False) -> int | None:
        if subtype < 24:
            return subtype
        if subtype == 24:
            return self.reader.read(1)[0]
        if subtype == 25:
            return cast(int, struct.unpack(">H", self.reader.read(2))[0])
        if subtype == 26:
            return cast(int, struct.unpack(">L", self.reader.read(4))[0])
        if subtype == 27:
            return cast(int, struct.unpack(">Q", self.reader.read(8))[0])
        if subtype == 31 and allow_indefinite:
            return None
        raise CBORDecodeValueError(f"unknown unsigned integer subtype 0x{subtype:x}")

    def _fail(self, message: str, offset: int) -> None:
        if self.error is None:
            self.error = message

    def _scan_item(self, initial: int | None = None) -> _Node:
        start = self.reader.position
        if initial is not None:
            start -= 1
        try:
            if initial is None:
                initial = self.reader.read(1)[0]
        except _BudgetExceeded as exc:
            self.budget_exhausted = exc.kind
            self._fail(f"{exc.kind} budget exceeded during structural indexing", start)
            return _Node(start, 7, 0, complete=False)
        except CBORDecodeError as exc:
            self._fail(str(exc), start)
            return _Node(start, 7, 0, complete=False)

        major = initial >> 5
        subtype = initial & 0x1F
        node = _Node(start, major, subtype)
        try:
            self._scan_node(node, subtype)
        except _BudgetExceeded as exc:
            node.complete = False
            self.budget_exhausted = exc.kind
            self._fail(f"{exc.kind} budget exceeded during structural indexing", start)
        except CBORDecodeError as exc:
            node.complete = False
            self._fail(str(exc), start)
        except (ValueError, TypeError, OverflowError) as exc:
            node.complete = False
            self._fail(str(exc), start)
        return node

    def _scan_node(self, node: _Node, subtype: int) -> None:
        major = node.major
        if major in (0, 1):
            node.value = self._scan_length(subtype)
        elif major in (2, 3):
            length = self._scan_length(subtype, allow_indefinite=True)
            if length is None:
                node.indefinite = True
                while True:
                    if self.reader._at_end():
                        raise CBORDecodeEOF("premature end of stream in indefinite string")
                    first = self.reader.read(1)[0]
                    if first == 0xFF:
                        break
                    chunk_major = first >> 5
                    what = "bytestring" if major == 2 else "string"
                    if chunk_major != major:
                        raise CBORDecodeValueError(
                            f"non-{what} found in indefinite length {what}"
                        )
                    chunk_length = self._scan_length(first & 0x1F)
                    if chunk_length is None or chunk_length > sys.maxsize:
                        raise CBORDecodeValueError(
                            f"invalid length for indefinite chunk 0x{chunk_length:x}"
                        )
                    payload_offset = self.reader.position
                    if chunk_length:
                        self.reader.skip(chunk_length)
                    node.chunks.append((payload_offset, chunk_length, major == 2))
            else:
                if length > sys.maxsize:
                    raise CBORDecodeValueError(f"invalid length 0x{length:x}")
                node.length = length
                node.payload_offset = self.reader.position
                if self._namespace_stack:
                    self._namespace_stack[-1].add(
                        self.reader.position, length, major == 2
                    )
                self.reader.skip(length)
        elif major == 4:
            length = self._scan_length(subtype, allow_indefinite=True)
            if length is None:
                node.indefinite = True
                while True:
                    if self.reader._at_end():
                        raise CBORDecodeEOF("premature end of stream in array")
                    first = self.reader.read(1)[0]
                    if first == 0xFF:
                        break
                    child = self._scan_item(first)
                    node.children.append(child)
                    if not child.complete:
                        node.complete = False
                        break
            else:
                if length > sys.maxsize:
                    raise CBORDecodeValueError(f"invalid length for array 0x{length:x}")
                node.length = length
                for _ in range(length):
                    child = self._scan_item()
                    node.children.append(child)
                    if not child.complete:
                        node.complete = False
                        break
        elif major == 5:
            length = self._scan_length(subtype, allow_indefinite=True)
            if length is None:
                node.indefinite = True
                while True:
                    if self.reader._at_end():
                        raise CBORDecodeEOF("premature end of stream in map")
                    first = self.reader.read(1)[0]
                    if first == 0xFF:
                        break
                    key = self._scan_item(first)
                    if not key.complete:
                        node.entries.append(
                            (key, _Node(self.reader.position, 7, 0, complete=False))
                        )
                        node.complete = False
                        break
                    value = self._scan_item()
                    node.entries.append((key, value))
                    if not value.complete:
                        node.complete = False
                        break
            else:
                if length > sys.maxsize:
                    raise CBORDecodeValueError(f"invalid length for map 0x{length:x}")
                node.length = length
                for _ in range(length):
                    key = self._scan_item()
                    if not key.complete:
                        node.entries.append(
                            (key, _Node(self.reader.position, 7, 0, complete=False))
                        )
                        node.complete = False
                        break
                    value = self._scan_item()
                    node.entries.append((key, value))
                    if not value.complete:
                        node.complete = False
                        break
        elif major == 6:
            tagnum = self._scan_length(subtype)
            node.tag = cast(int, tagnum)
            if tagnum == 28:
                node.share_index = len(self.share_defs)
                self.share_defs.append(node)
            elif tagnum == 256:
                table = _StringTable()
                node.string_table = table
                self._namespace_stack.append(table)
                try:
                    child = self._scan_item()
                finally:
                    self._namespace_stack.pop()
                node.children.append(child)
                if not child.complete:
                    node.complete = False
                    table.complete = False
                return
            elif tagnum == 25:
                node.string_table = (
                    self._namespace_stack[-1] if self._namespace_stack else None
                )
            child = self._scan_item()
            node.children.append(child)
            if not child.complete:
                node.complete = False
        else:  # major == 7
            if subtype < 20:
                node.value = CBORSimpleValue(subtype)
            elif subtype == 20:
                node.value = False
            elif subtype == 21:
                node.value = True
            elif subtype == 22:
                node.value = None
            elif subtype == 23:
                node.value = undefined
            elif subtype == 24:
                node.value = CBORSimpleValue(self.reader.read(1)[0])
            elif subtype == 25:
                node.payload_offset = self.reader.position
                node.length = 2
                node.value = struct.unpack(">e", self.reader.read(2))[0]
            elif subtype == 26:
                node.payload_offset = self.reader.position
                node.length = 4
                node.value = struct.unpack(">f", self.reader.read(4))[0]
            elif subtype == 27:
                node.payload_offset = self.reader.position
                node.length = 8
                node.value = struct.unpack(">d", self.reader.read(8))[0]
            elif subtype == 31:
                raise CBORDecodeValueError("unexpected break marker")
            else:
                raise CBORDecodeValueError(
                    f"Undefined Reserved major type 7 subtype 0x{subtype:x}"
                )

    def share_definition(self, index: int, reference: _Node) -> _Node:
        if index < 0:
            raise CBORDecodeValueError(f"shared reference {index} not found")
        try:
            definition = self.share_defs[index]
        except IndexError:
            raise CBORDecodeValueError(f"shared reference {index} not found") from None

        if definition.offset > reference.offset and not _contains(definition, reference):
            raise CBORDecodeValueError(f"shared value {index} has not been initialized")
        return definition


def _contains(outer: _Node, inner: _Node) -> bool:
    """Whether ``inner`` lies inside the still-unscanned part of ``outer``."""
    return outer.offset <= inner.offset


def _typed_equal(left: Any, right: Any) -> bool:
    """
    Compare values with their CBOR data model types intact.

    Mapping, sequence and set containers compare structurally because their
    immutable Python representations (``FrozenDict``, ``tuple``,
    ``frozenset``) may legitimately be compared against mutable path keys.
    All other values must have identical types, so ``True`` does not match
    ``1``, ``1`` does not match ``1.0``, and text does not match bytes.
    """
    if isinstance(left, CBORTag) and isinstance(right, CBORTag):
        return left.tag == right.tag and _typed_equal(left.value, right.value)
    if left is right:
        # Container identity is meaningful for shared values and cycles; this
        # also short-circuits a recursive comparison before it recurses.
        return True
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        if len(left) != len(right):
            return False
        unmatched = list(right.items())
        for key, value in left.items():
            for index, (other_key, other_value) in enumerate(unmatched):
                if _typed_equal(key, other_key) and _typed_equal(value, other_value):
                    del unmatched[index]
                    break
            else:
                return False
        return True
    if (
        isinstance(left, (list, tuple))
        and isinstance(right, (list, tuple))
        and not isinstance(left, (str, bytes, CBORSimpleValue))
        and not isinstance(right, (str, bytes, CBORSimpleValue))
    ):
        return len(left) == len(right) and all(
            _typed_equal(a, b) for a, b in zip(left, right)
        )
    if isinstance(left, (set, frozenset)) and isinstance(right, (set, frozenset)):
        return all(
            any(_typed_equal(item, other) for other in right) for item in left
        ) and len(left) == len(right)

    if type(left) is not type(right):
        return False
    try:
        return bool(left == right)
    except Exception:
        return False


def _paths_equal(left: Sequence[Any], right: Sequence[Any]) -> bool:
    return len(left) == len(right) and all(
        _typed_equal(a, b) for a, b in zip(left, right)
    )


class _Incomplete(Exception):
    pass


class _SourceUnavailable(Exception):
    pass


class _Materializer:
    def __init__(
        self,
        scanner: _Scanner,
        reader: _Reader,
        tag_hook: Any = None,
        object_hook: Any = None,
        str_errors: str = "strict",
        expansion_budget: int | None = None,
    ) -> None:
        self.scanner = scanner
        self.reader = reader
        self.tag_hook = tag_hook
        self.object_hook = object_hook
        self.str_errors = str_errors
        self.expansion_budget = expansion_budget
        self.expanded = 0
        self.cache: dict[int, Any] = {}
        self._immutable_key_cache: dict[int, Any] = {}
        self.sources: list[SelectiveSource] = []
        self._source_ids: set[tuple[str, int, int]] = set()
        self._active_shared: dict[int, Any] = {}
        self._immutable_depth = 0
        self._immutable_active: set[int] = set()

    @property
    def immutable(self) -> bool:
        """Whether the current expansion context requires immutable values."""
        return self._immutable_depth > 0

    def _add_source(self, kind: str, index: int, offset: int) -> None:
        key = (kind, index, offset)
        if key not in self._source_ids:
            self._source_ids.add(key)
            self.sources.append(SelectiveSource(kind, index, offset))

    def _charge(self) -> None:
        if self.expansion_budget is not None and self.expanded >= self.expansion_budget:
            raise _BudgetExceeded("expansion")
        self.expanded += 1

    def materialize(self, node: _Node, immutable: bool = False) -> Any:
        if not node.complete:
            raise _Incomplete("selected value depends on a region that could not be indexed")
        if immutable:
            self._immutable_depth += 1
            try:
                return self._materialize_immutable(node)
            finally:
                self._immutable_depth -= 1

        cached = self.cache.get(node.offset, _NO_CACHE)
        if cached is not _NO_CACHE:
            return cached

        self._charge()
        major = node.major
        if major == 0:
            result: Any = node.value
        elif major == 1:
            result = -cast(int, node.value) - 1
        elif major == 2:
            result = self._read_binary(node)
        elif major == 3:
            result = self._read_string(node)
        elif major == 4:
            result = self._array(node)
        elif major == 5:
            result = self._map(node)
        elif major == 6:
            result = self._tag(node)
        else:
            result = node.value

        if major != 4 and major != 5:
            self.cache[node.offset] = result
        return result

    def _materialize_immutable(self, node: _Node) -> Any:
        if node.offset in self._immutable_active:
            # The full decoder cannot represent a recursive tuple/frozenset
            # either, so the value has no finite immutable expansion.
            raise _Incomplete("recursive value has no finite immutable expansion")

        self._charge()
        major = node.major
        if major == 4:
            self._immutable_active.add(node.offset)
            try:
                return tuple(
                    self._materialize_key(child) for child in node.children
                )
            finally:
                self._immutable_active.discard(node.offset)
        if major == 5:
            self._immutable_active.add(node.offset)
            try:
                dictionary: dict[Any, Any] = {}
                for key_node, value_node in node.entries:
                    key = self._materialize_key(key_node)
                    dictionary[key] = self.materialize(value_node, immutable=True)
            finally:
                self._immutable_active.discard(node.offset)
            if self.object_hook:
                return self.object_hook(self, dictionary)
            return FrozenDict(dictionary)
        if major == 6:
            tagnum = cast(int, node.tag)
            if tagnum == 28:
                return self._materialize_key(node.children[0])
            if tagnum == 29:
                index = cast(int, self.materialize(node.children[0], immutable=True))
                definition = self._shared_reference_node(index, node)
                if definition.share_index in self._active_shared:
                    raise _Incomplete("recursive value has no finite immutable expansion")
                if not definition.children:
                    raise _Incomplete("shared value has no payload")
                return self._materialize_key(definition.children[0])
            if tagnum in (256, 55799):
                return self._materialize_key(node.children[0])
            return self._tag(node, immutable=True)
        return self._materialize_key(node)

    def _materialize_key(self, node: _Node) -> Any:
        """
        Expand an immutable value without applying ``object_hook``.

        Mapping keys are not passed through the object hook by the full
        decoder, so the selective reader must match that behaviour when it
        evaluates a typed key.  Nested containers become ``FrozenDict`` /
        ``tuple`` / ``frozenset`` just as they do inside a key.
        """
        if not node.complete:
            raise _Incomplete("key depends on a region that could not be indexed")
        cached = self._immutable_key_cache.get(node.offset, _NO_CACHE)
        if cached is not _NO_CACHE:
            return cached
        if node.offset in self._immutable_active:
            raise _Incomplete("recursive key has no finite expansion")

        self._charge()
        major = node.major
        if major == 4:
            self._immutable_active.add(node.offset)
            try:
                result = tuple(self._materialize_key(child) for child in node.children)
            finally:
                self._immutable_active.discard(node.offset)
            self._immutable_key_cache[node.offset] = result
            return result
        if major == 5:
            self._immutable_active.add(node.offset)
            try:
                dictionary: dict[Any, Any] = {}
                for key_node, value_node in node.entries:
                    dictionary[self._materialize_key(key_node)] = self._materialize_key(
                        value_node
                    )
            finally:
                self._immutable_active.discard(node.offset)
            # This mirrors the full decoder: object_hook is invoked in the
            # immutable context, and a non-hashable replacement makes the
            # enclosing map key unhashable.
            result: Any = (
                self.object_hook(self, dictionary) if self.object_hook else FrozenDict(dictionary)
            )
            self._immutable_key_cache[node.offset] = result
            return result
        if major == 6:
            tagnum = cast(int, node.tag)
            if tagnum == 28:
                return self._materialize_key(node.children[0])
            if tagnum == 29:
                index = cast(int, self._materialize_key(node.children[0]))
                definition = self._shared_reference_node(index, node)
                if definition.share_index in self._active_shared:
                    raise _Incomplete("recursive key has no finite expansion")
                if not definition.children:
                    raise _Incomplete("shared value has no payload")
                return self._materialize_key(definition.children[0])
            if tagnum in (256, 55799):
                return self._materialize_key(node.children[0])
            if tagnum == 258:
                return frozenset(self._materialize_key(node.children[0]))
            if tagnum == 25:
                return self._string_reference(node)
            return self._tag(node, immutable=True)
        if major == 2:
            return self._read_binary(node)
        if major == 3:
            return self._read_string(node)
        if major == 1:
            return -cast(int, node.value) - 1
        return node.value

    def _read_binary(self, node: _Node) -> bytes:
        if not node.indefinite:
            assert node.payload_offset is not None and node.length is not None
            return self.reader.read_at(node.payload_offset, node.length)
        return b"".join(
            self.reader.read_at(offset, length) for offset, length, _ in node.chunks
        )

    def _read_string(self, node: _Node) -> str:
        if not node.indefinite:
            assert node.payload_offset is not None and node.length is not None
            try:
                return self.reader.read_at(node.payload_offset, node.length).decode(
                    "utf-8", self.str_errors
                )
            except UnicodeDecodeError as exc:
                raise CBORDecodeValueError("error decoding unicode string") from exc

        parts: list[str] = []
        for offset, length, _ in node.chunks:
            try:
                parts.append(
                    self.reader.read_at(offset, length).decode("utf-8", self.str_errors)
                )
            except UnicodeDecodeError as exc:
                raise CBORDecodeValueError("error decoding unicode string") from exc
        return "".join(parts)

    def _array(self, node: _Node) -> Any:
        if not node.complete:
            raise _Incomplete("array is incomplete")
        values = [self.materialize(child) for child in node.children]
        result: Any = tuple(values) if self.immutable else values
        self.cache[node.offset] = result
        return result

    def _map(self, node: _Node) -> Any:
        if not node.complete:
            raise _Incomplete("map is incomplete")
        dictionary: dict[Any, Any] = {}
        for key_node, value_node in node.entries:
            key = self.materialize(key_node, immutable=True)
            dictionary[key] = self.materialize(value_node)

        if self.object_hook:
            result: Any = self.object_hook(self, dictionary)
        elif self.immutable:
            result = FrozenDict(dictionary)
        else:
            result = dictionary

        if not self.immutable:
            self.cache[node.offset] = result
        return result

    def _tag(self, node: _Node, immutable: bool = False) -> Any:
        tagnum = cast(int, node.tag)
        if tagnum == 28:
            return self._shared_definition(node, immutable)
        if tagnum == 29:
            index = cast(int, self.materialize(node.children[0], immutable=True))
            if immutable:
                definition = self._shared_reference_node(index, node)
                if definition.share_index in self._active_shared:
                    raise _Incomplete(
                        "recursive value has no finite immutable expansion"
                    )
                if not definition.children:
                    raise _Incomplete("shared value has no payload")
                return self._materialize_immutable(definition.children[0])
            return self._shared_reference(index, node)
        if tagnum == 25:
            return self._string_reference(node)
        if tagnum == 256:
            return self.materialize(node.children[0], immutable)
        if tagnum == 55799:
            return self.materialize(node.children[0], immutable)

        child = node.children[0]
        if tagnum == 258:
            value = self.materialize(child, immutable=True)
            result: Any = frozenset(value) if immutable else set(value)
        elif tagnum == 0:
            result = self._datetime_string(self.materialize(child))
        elif tagnum == 1:
            value = self.materialize(child)
            try:
                result = datetime.fromtimestamp(value, timezone.utc)
            except (OverflowError, OSError, ValueError) as exc:
                raise CBORDecodeValueError("error decoding datetime from epoch") from exc
        elif tagnum == 2:
            value = self.materialize(child)
            if not isinstance(value, bytes):
                raise CBORDecodeValueError("invalid bignum value " + str(value))
            result = int(hexlify(value), 16)
        elif tagnum == 3:
            value = self.materialize(child)
            if not isinstance(value, bytes):
                raise CBORDecodeValueError("invalid bignum value " + str(value))
            result = -int(hexlify(value), 16) - 1
        elif tagnum == 4:
            try:
                exp, sig = self.materialize(child)
            except (TypeError, ValueError) as exc:
                raise CBORDecodeValueError("Incorrect tag 4 payload") from exc
            tmp = Decimal(sig).as_tuple()
            result = Decimal((tmp.sign, tmp.digits, exp))
        elif tagnum == 5:
            try:
                exp, sig = self.materialize(child)
            except (TypeError, ValueError) as exc:
                raise CBORDecodeValueError("Incorrect tag 5 payload") from exc
            result = Decimal(sig) * (2 ** Decimal(exp))
        elif tagnum == 30:
            payload = self.materialize(child, immutable=True)
            try:
                result = Fraction(*payload)
            except (TypeError, ZeroDivisionError) as exc:
                if not isinstance(payload, tuple):
                    raise CBORDecodeValueError(
                        "error decoding rational: input value was not a tuple"
                    ) from None
                raise CBORDecodeValueError("error decoding rational") from exc
        elif tagnum == 35:
            try:
                result = re.compile(self.materialize(child))
            except re.error as exc:
                raise CBORDecodeValueError("error decoding regular expression") from exc
        elif tagnum == 36:
            try:
                result = Parser().parsestr(self.materialize(child))
            except TypeError as exc:
                raise CBORDecodeValueError("error decoding MIME message") from exc
        elif tagnum == 37:
            try:
                result = UUID(bytes=self.materialize(child))
            except (TypeError, ValueError) as exc:
                raise CBORDecodeValueError("error decoding UUID value") from exc
        elif tagnum == 100:
            result = date.fromordinal(self.materialize(child) + 719163)
        elif tagnum == 1004:
            result = date.fromisoformat(self.materialize(child))
        elif tagnum == 260:
            value = self.materialize(child)
            if not isinstance(value, bytes) or len(value) not in (4, 6, 16):
                raise CBORDecodeValueError(f"invalid ipaddress value {value!r}")
            result = ip_address(value) if len(value) in (4, 16) else CBORTag(260, value)
        elif tagnum == 261:
            net_map = self.materialize(child)
            if isinstance(net_map, Mapping) and len(net_map) == 1:
                for net in net_map.items():
                    try:
                        result = ip_network(net, strict=False)
                        break
                    except (TypeError, ValueError):
                        continue
                else:
                    raise CBORDecodeValueError(f"invalid ipnetwork value {net_map!r}")
            else:
                raise CBORDecodeValueError(f"invalid ipnetwork value {net_map!r}")
        else:
            placeholder = CBORTag(tagnum, None)
            self.cache[node.offset] = placeholder
            try:
                placeholder.value = self.materialize(child, immutable)
                result = placeholder
                if self.tag_hook:
                    result = self.tag_hook(self, result)
            except BaseException:
                self.cache.pop(node.offset, None)
                raise

        if not immutable:
            # Immutable expansions (used for map keys) must not leak into the
            # mutable value cache.
            self.cache[node.offset] = result
        else:
            # A shared tag key must still keep its identity after placeholder
            # construction; later mutable reads will build their own wrapper.
            self._immutable_key_cache[node.offset] = result
        return result

    def _shared_definition(self, node: _Node, immutable: bool = False) -> Any:
        cached = self.cache.get(node.offset, _NO_CACHE)
        if cached is not _NO_CACHE:
            return cached

        index = cast(int, node.share_index)
        child = node.children[0]
        if not child.complete:
            raise _Incomplete("shared value definition is incomplete")

        if index in self._active_shared:
            active = self._active_shared[index]
            if active is _UNFINISHED:
                raise _Incomplete("shared value is still being decoded")
            if immutable:
                raise _Incomplete("recursive value has no finite immutable expansion")
            return active

        self._charge()
        if immutable:
            return self.materialize(child, immutable=True)

        if child.major == 4:
            placeholder: Any = []
            self.cache[node.offset] = placeholder
            self.cache[child.offset] = placeholder
            self._active_shared[index] = placeholder
            try:
                self._charge()
                placeholder.extend(self.materialize(item) for item in child.children)
                if not child.complete:
                    raise _Incomplete("shared array is incomplete")
            except BaseException:
                self._forget_shared(node, child, index)
                raise
            finally:
                self._active_shared.pop(index, None)
            return placeholder

        if child.major == 5:
            placeholder = {}
            self.cache[node.offset] = placeholder
            self.cache[child.offset] = placeholder
            self._active_shared[index] = placeholder
            try:
                self._charge()
                if not child.complete:
                    raise _Incomplete("shared map is incomplete")
                for key_node, value_node in child.entries:
                    key = self.materialize(key_node, immutable=True)
                    placeholder[key] = self.materialize(value_node)
                # The full decoder applies object_hook to a shared map's
                # payload as well.  Apply it to the placeholder in place so
                # references that were already handed out stay valid.
                if self.object_hook:
                    replacement = self.object_hook(self, placeholder)
                    if replacement is not placeholder:
                        self._active_shared[index] = replacement
                        self.cache[node.offset] = replacement
                        self.cache[child.offset] = replacement
                        return replacement
            except BaseException:
                self._forget_shared(node, child, index)
                raise
            finally:
                self._active_shared.pop(index, None)
            return placeholder

        if child.major == 6 and cast(int, child.tag) not in _KNOWN_TAGS:
            placeholder = CBORTag(cast(int, child.tag), None)
            self.cache[node.offset] = placeholder
            self.cache[child.offset] = placeholder
            self._active_shared[index] = placeholder
            try:
                placeholder.value = self.materialize(child.children[0])
            except BaseException:
                self._forget_shared(node, child, index)
                raise
            finally:
                self._active_shared.pop(index, None)
            return placeholder

        self._active_shared[index] = _UNFINISHED
        try:
            self._charge()
            value = self.materialize(child)
        except BaseException:
            self._active_shared.pop(index, None)
            raise
        self._active_shared.pop(index, None)
        self.cache[node.offset] = value
        return value

    def _forget_shared(self, node: _Node, child: _Node, index: int) -> None:
        self.cache.pop(node.offset, None)
        self.cache.pop(child.offset, None)
        self._active_shared.pop(index, None)

    def _shared_reference_node(self, index: int, reference: _Node) -> _Node:
        try:
            definition = self.scanner.share_definition(index, reference)
        except CBORDecodeError as exc:
            raise _SourceUnavailable(str(exc)) from exc

        self._add_source("shared", index, definition.offset)
        if not definition.complete:
            raise _Incomplete("shared value depends on an unindexed region")
        return definition

    def _shared_reference(self, index: int, reference: _Node) -> Any:
        definition = self._shared_reference_node(index, reference)
        active = self._active_shared.get(definition.share_index)
        if active is not None:
            if active is _UNFINISHED:
                raise _Incomplete("shared value is still being decoded")
            return active
        return self._shared_definition(definition)

    def _string_reference(self, node: _Node) -> Any:
        table = node.string_table
        if table is None:
            raise CBORDecodeValueError("string reference outside of namespace")

        index = self.materialize(node.children[0], immutable=True)
        if type(index) is not int:
            raise CBORDecodeValueError(f"invalid string reference {index!r}")
        if not 0 <= index < len(table.entries):
            if not table.complete:
                raise _Incomplete(
                    "string reference may depend on an unindexed part of the table"
                )
            raise CBORDecodeValueError(f"string reference {index} not found")
        entry = table.entries[index]

        self._add_source("stringref", index, entry.offset)
        if not entry.loaded:
            data = self.reader.read_at(entry.offset, entry.length)
            if entry.is_bytes:
                entry.value = data
            else:
                try:
                    entry.value = data.decode("utf-8", self.str_errors)
                except UnicodeDecodeError as exc:
                    raise CBORDecodeValueError("error decoding unicode string") from exc
            entry.loaded = True
        return entry.value

    def _datetime_string(self, value: Any) -> datetime:
        match = timestamp_re.match(value)
        if not match:
            raise CBORDecodeValueError(f"invalid datetime string: {value!r}")
        (
            year,
            month,
            day,
            hour,
            minute,
            second,
            secfrac,
            offset_sign,
            offset_h,
            offset_m,
        ) = match.groups()
        microsecond = 0 if secfrac is None else int(f"{secfrac:<6}")
        if offset_h:
            sign = -1 if offset_sign == "-" else 1
            hours = int(offset_h) * sign
            minutes = int(offset_m) * sign
            tz = timezone(timedelta(hours=hours, minutes=minutes))
        else:
            tz = timezone.utc
        return datetime(
            int(year),
            int(month),
            int(day),
            int(hour),
            int(minute),
            int(second),
            microsecond,
            tz,
        )


def _validate_budget(name: str, value: int | None) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer or None")
    return value


def _normalize_paths(paths: Iterable[Sequence[Any]]) -> list[tuple[Any, ...]]:
    if isinstance(paths, (str, bytes)):
        raise TypeError("paths must be an iterable of path sequences")
    try:
        iterator = iter(paths)
    except TypeError:
        raise TypeError("paths must be an iterable of path sequences") from None

    normalized: list[tuple[Any, ...]] = []
    for path in iterator:
        if isinstance(path, (str, bytes)) or not isinstance(path, Sequence):
            raise TypeError("each path must be a sequence of path components")
        normalized.append(tuple(path))
    return normalized


def _select(
    root: _Node | None,
    paths: list[tuple[Any, ...]],
    materializer: _Materializer,
    scan_error: str | None,
) -> SelectiveResult:
    values: list[tuple[tuple[Any, ...], Any]] = []
    unfinished: list[tuple[Any, ...]] = []
    missing: list[tuple[Any, ...]] = []
    errors: dict[tuple[Any, ...], str] = {}
    exhausted: set[str] = set()

    if root is None and scan_error is not None:
        # Structural indexing failed before a usable root existed.
        for path in paths:
            unfinished.append(path)
            errors[path] = scan_error
        return SelectiveResult(
            values=_PathMapping(values),
            unfinished=tuple(unfinished),
            missing=tuple(missing),
            errors=errors,
            sources=tuple(materializer.sources),
            complete=False,
            scan_error=scan_error,
            exhausted=tuple(sorted(exhausted)),
        )

    for path in paths:
        try:
            node = _resolve_path(root, path, materializer)
            if node is _MISSING:
                missing.append(path)
            elif node is _UNFINISHED:
                unfinished.append(path)
                errors[path] = (
                    scan_error
                    or "path could not be proven from the available structure"
                )
            else:
                value = materializer.materialize(node)
                values.append((path, value))
        except _BudgetExceeded as exc:
            unfinished.append(path)
            errors[path] = f"{exc.kind} budget exceeded"
            exhausted.add(exc.kind)
        except _Incomplete as exc:
            unfinished.append(path)
            errors[path] = str(exc)
        except _SourceUnavailable as exc:
            unfinished.append(path)
            errors[path] = str(exc)
        except (CBORDecodeError, ValueError, TypeError, OverflowError) as exc:
            unfinished.append(path)
            errors[path] = str(exc)

    if materializer.reader.exhausted or materializer.scanner.budget_exhausted == "read":
        exhausted.add("read")

    return SelectiveResult(
        values=_PathMapping(values),
        unfinished=tuple(unfinished),
        missing=tuple(missing),
        errors=errors,
        sources=tuple(materializer.sources),
        complete=not unfinished,
        scan_error=scan_error,
        exhausted=tuple(sorted(exhausted)),
    )


def _resolve_path(
    root: _Node | None, path: tuple[Any, ...], materializer: _Materializer
) -> Any:
    if root is None:
        return _MISSING

    node: Any = root
    for component in path:
        if node is _MISSING or node is _UNFINISHED:
            return node
        if not isinstance(node, _Node):
            return _MISSING

        if isinstance(component, TagKey):
            while True:
                if not isinstance(node, _Node):
                    return _MISSING
                if node.major != 6:
                    return _MISSING
                if node.tag == component.tag:
                    if not node.children:
                        return _UNFINISHED
                    node = node.children[0]
                    break
                unwrapped = _unwrap_transparent(node, materializer)
                if unwrapped is node:
                    return _MISSING
                if unwrapped is _UNFINISHED or unwrapped is _MISSING:
                    return unwrapped
                node = unwrapped
            continue

        node = _unwrap_transparent(node, materializer)
        if node is _MISSING or node is _UNFINISHED:
            return node
        if not isinstance(node, _Node):
            return _MISSING
        if node.major == 4:
            if type(component) is not int or component < 0:
                return _MISSING
            if node.length is not None and component >= node.length:
                return _MISSING
            if component >= len(node.children):
                return _UNFINISHED
            node = node.children[component]
        elif node.major == 5:
            node = _map_lookup(node, component, materializer)
        else:
            return _MISSING

    return node


def _unwrap_transparent(node: _Node, materializer: _Materializer) -> Any:
    """
    Follow shareable/stringref-namespace/self-describe wrappers.

    The returned node is never one of those transparent tags.  A cycle that
    consists only of wrappers has no finite payload and is reported as
    unfinished rather than followed forever.
    """
    seen: set[int] = set()
    while isinstance(node, _Node) and node.major == 6:
        if node.offset in seen:
            return _UNFINISHED
        seen.add(node.offset)

        tagnum = cast(int, node.tag)
        if tagnum == 28:
            if not node.children:
                return _UNFINISHED
            node = node.children[0]
        elif tagnum == 29:
            try:
                index = cast(int, materializer.materialize(node.children[0], immutable=True))
                definition = materializer.scanner.share_definition(index, node)
            except (CBORDecodeError, _BudgetExceeded, _Incomplete, ValueError, TypeError):
                return _UNFINISHED
            materializer._add_source("shared", index, definition.offset)
            active = materializer._active_shared.get(definition.share_index)
            if active is not None:
                if active is _UNFINISHED:
                    return _UNFINISHED
                # An enclosing container that is still being built can be
                # followed for the remaining (finite) path components.
                if not definition.children:
                    return _UNFINISHED
                node = definition.children[0]
            else:
                if not definition.complete:
                    return _UNFINISHED
                if not definition.children:
                    return _UNFINISHED
                node = definition.children[0]
        elif tagnum in (256, 55799):
            if not node.children:
                return _UNFINISHED
            node = node.children[0]
        else:
            break
    return node


def _map_lookup(node: _Node, component: Any, materializer: _Materializer) -> Any:
    incomplete = not node.complete
    key_error = False
    matched: _Node | None = None

    for key_node, value_node in node.entries:
        if not key_node.complete:
            incomplete = True
            continue
        try:
            key = materializer.materialize(key_node, immutable=True)
        except _BudgetExceeded:
            raise
        except (CBORDecodeError, ValueError, TypeError, OverflowError):
            key_error = True
            continue
        if _typed_equal(key, component):
            matched = value_node

    if matched is not None and not incomplete and not key_error:
        return matched
    if incomplete or key_error:
        # A later, unindexed pair could override an earlier match, so the
        # key's final value is not yet provable.
        return _UNFINISHED
    return _MISSING


def loads_selective(
    s: bytes | bytearray | memoryview,
    paths: Iterable[Sequence[Any]],
    *,
    read_budget: int | None = None,
    expansion_budget: int | None = None,
    tag_hook: Any = None,
    object_hook: Any = None,
    str_errors: str = "strict",
) -> SelectiveResult:
    """
    Decode only the requested paths from a CBOR bytestring.

    Path components keep their CBOR type: ``True`` never matches the integer
    key ``1``, and text and byte-string keys never match each other.  Array
    elements are addressed by non-negative integers.  Use :class:`TagKey` to
    enter the payload of an otherwise opaque tag.

    Unselected definite-length strings and byte strings are skipped rather
    than read, provided the source is seekable.  Definitions still required by
    a selected value (shared values and string-reference entries) are fetched
    on demand and reported in :attr:`SelectiveResult.sources`.

    :param s: the CBOR document
    :param paths: an iterable of paths, each an iterable of keys or indexes
    :param read_budget: maximum number of bytes to read, or ``None`` for no limit
    :param expansion_budget:
        maximum number of CBOR values expanded, or ``None`` for no limit
    :param tag_hook: hook for unknown tags, as in :func:`~cbor2.loads`
    :param object_hook: hook for maps, as in :func:`~cbor2.loads`
    :param str_errors: UTF-8 error handling mode
    """
    return load_selective(
        BytesIO(s),
        paths,
        read_budget=read_budget,
        expansion_budget=expansion_budget,
        tag_hook=tag_hook,
        object_hook=object_hook,
        str_errors=str_errors,
    )


def load_selective(
    fp: IO[bytes],
    paths: Iterable[Sequence[Any]],
    *,
    read_budget: int | None = None,
    expansion_budget: int | None = None,
    tag_hook: Any = None,
    object_hook: Any = None,
    str_errors: str = "strict",
) -> SelectiveResult:
    """Decode only the requested paths from an open CBOR stream."""
    read_budget = _validate_budget("read_budget", read_budget)
    expansion_budget = _validate_budget("expansion_budget", expansion_budget)
    normalized = _normalize_paths(paths)

    reader = _Reader(fp, read_budget)
    scanner = _Scanner(reader)
    root = scanner.scan_document()

    materializer = _Materializer(
        scanner,
        reader,
        tag_hook=tag_hook,
        object_hook=object_hook,
        str_errors=str_errors,
        expansion_budget=expansion_budget,
    )
    result = _select(root, normalized, materializer, scanner.error)
    if reader.exhausted:
        names = set(result.exhausted)
        names.add("read")
        result.exhausted = tuple(sorted(names))
    return result


# Alternative spellings kept intentionally small and consistent with the
# package's ``load``/``loads`` naming.
selective_load = load_selective
selective_loads = loads_selective
load_paths = load_selective
loads_paths = loads_selective
