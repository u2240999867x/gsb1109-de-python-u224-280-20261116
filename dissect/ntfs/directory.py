from __future__ import annotations

import io
from dataclasses import dataclass, field
from enum import Flag, auto
from typing import TYPE_CHECKING

from dissect.ntfs.c_ntfs import ATTRIBUTE_TYPE_CODE, FILE_NAME_DOS, FILE_RECORD_SEGMENT_IN_USE, c_ntfs
from dissect.ntfs.exceptions import FileNotFoundError, MftNotAvailableError, NotADirectoryError
from dissect.ntfs.index import IndexEntry
from dissect.ntfs.util import segment_reference

if TYPE_CHECKING:
    from collections.abc import ItemsView, Iterator, KeysView, ValuesView
    from typing import BinaryIO

    from dissect.ntfs.attr import Attribute, FileName
    from dissect.ntfs.index import Index
    from dissect.ntfs.mft import MftRecord


class Source(Flag):
    """The source(s) that support a directory path.

    A path can be present in the directory index, in a ``$FILE_NAME`` attribute, or in both. Keeping the source
    explicit prevents one source from silently replacing the other when they disagree.
    """

    INDEX = auto()
    ATTRIBUTE = auto()
    BOTH = INDEX | ATTRIBUTE


@dataclass(frozen=True)
class GenerationMismatch:
    """A file reference whose sequence number (generation) does not match the record it points to.

    Both the file reference of an index entry and the parent reference of a ``$FILE_NAME`` attribute are checked.
    """

    name: str
    source: Source
    reference: str
    segment: int
    expected: int
    actual: int
    entry: IndexEntry | None = None
    attribute: Attribute | None = None


@dataclass(frozen=True)
class ParentMismatch:
    """A path whose parent directory number contradicts the directory it was found in."""

    name: str
    source: Source
    segment: int
    expected: int
    entry: IndexEntry | None = None
    attribute: Attribute | None = None


@dataclass(frozen=True)
class BrokenIndexEntry:
    """An index entry that could not be parsed or interpreted."""

    error: Exception
    entry: IndexEntry | None = None


@dataclass(frozen=True)
class BrokenIndexBuffer:
    """An index buffer that could not be parsed."""

    vcn: int
    error: Exception


@dataclass(frozen=True)
class BrokenFileName:
    """A ``$FILE_NAME`` attribute that could not be parsed."""

    error: Exception
    attribute: Attribute | None = None


@dataclass(frozen=True)
class MissingRecord:
    """A file reference that could not be dereferenced to an MFT record."""

    name: str
    source: Source
    segment: int
    error: Exception


@dataclass
class DirectoryEntry:
    """A single path in a directory, together with the evidence that supports it.

    Index entries and ``$FILE_NAME`` attributes are kept as separate lists. Duplicate names therefore never
    overwrite each other, and callers can see exactly which source contributed which path.
    """

    name: str
    index_entries: list[IndexEntry] = field(default_factory=list)
    file_names: list[FileName] = field(default_factory=list)
    records: list[MftRecord] = field(default_factory=list)

    def __repr__(self) -> str:
        return f"<DirectoryEntry {self.name!r} source={self.source.name}>"

    @property
    def source(self) -> Source:
        """Return which source(s) support this path."""
        source = Source(0)
        if self.index_entries:
            source |= Source.INDEX
        if self.file_names:
            source |= Source.ATTRIBUTE
        return source

    @property
    def in_index(self) -> bool:
        """Return whether the directory index supports this path."""
        return bool(self.index_entries)

    @property
    def in_attributes(self) -> bool:
        """Return whether a ``$FILE_NAME`` attribute supports this path."""
        return bool(self.file_names)

    @property
    def index_entry(self) -> IndexEntry | None:
        """Return the first index entry for this path, if any."""
        return self.index_entries[0] if self.index_entries else None

    @property
    def file_name(self) -> FileName | None:
        """Return the first ``$FILE_NAME`` attribute for this path, if any."""
        return self.file_names[0] if self.file_names else None

    @property
    def record(self) -> MftRecord | None:
        """Return the first MFT record referenced by this path, if any."""
        return self.records[0] if self.records else None

    @property
    def allocated(self) -> bool | None:
        """Return whether any referenced MFT record is allocated, or ``None`` if no record was resolved."""
        if not self.records:
            return None
        return any(record.header.Flags & FILE_RECORD_SEGMENT_IN_USE for record in self.records)

    def add_record(self, record: MftRecord | None) -> None:
        """Add a referenced MFT record without creating duplicates."""
        if record is not None and record not in self.records:
            self.records.append(record)


class DirectoryCatalog:
    """Reconcile a directory's index with the ``$FILE_NAME`` attributes in the MFT.

    The catalog is built from both sources. Every path is represented by a :class:`DirectoryEntry` that records
    whether the index, the attributes, or both support it. Disagreements are collected in separate lists:

    - :attr:`generation_mismatches`: file reference generation (sequence number) mismatches
    - :attr:`parent_mismatches`: parent directory number contradictions
    - :attr:`index_only`: paths only supported by the directory index
    - :attr:`attribute_only`: paths only supported by a ``$FILE_NAME`` attribute

    Paths supported by both sources remain in :attr:`entries`, even if one of the sources has a mismatch. A broken
    index entry is reported in :attr:`broken_index_entries` and does not stop the rest of the directory from being
    enumerated.

    Args:
        record: The directory record this catalog belongs to.
    """

    def __init__(self, record: MftRecord):
        self.record = record
        self.entries: dict[str, DirectoryEntry] = {}
        self.broken_index_entries: list[BrokenIndexEntry] = []
        self.broken_index_buffers: list[BrokenIndexBuffer] = []
        self.broken_file_names: list[BrokenFileName] = []
        self.generation_mismatches: list[GenerationMismatch] = []
        self.parent_mismatches: list[ParentMismatch] = []
        self.missing_records: list[MissingRecord] = []
        self.index_available = True
        self.index_error: Exception | None = None

    def __repr__(self) -> str:
        return f"<DirectoryCatalog {self.record!r} entries={len(self.entries)}>"

    def __iter__(self) -> Iterator[DirectoryEntry]:
        return iter(self.entries.values())

    def __len__(self) -> int:
        return len(self.entries)

    def __getitem__(self, name: str) -> DirectoryEntry:
        return self.entries[name]

    def __contains__(self, name: object) -> bool:
        return name in self.entries

    def keys(self) -> KeysView[str]:
        """Return the path names in this catalog."""
        return self.entries.keys()

    def values(self) -> ValuesView[DirectoryEntry]:
        """Return the entries in this catalog."""
        return self.entries.values()

    def items(self) -> ItemsView[str, DirectoryEntry]:
        """Return the path names and entries in this catalog."""
        return self.entries.items()

    @property
    def index_only(self) -> list[DirectoryEntry]:
        """Return paths only supported by the directory index."""
        return [entry for entry in self.entries.values() if entry.in_index and not entry.in_attributes]

    @property
    def attribute_only(self) -> list[DirectoryEntry]:
        """Return paths only supported by a ``$FILE_NAME`` attribute."""
        return [entry for entry in self.entries.values() if entry.in_attributes and not entry.in_index]

    @property
    def one_sided(self) -> list[DirectoryEntry]:
        """Return all paths only supported by one of the two sources."""
        return self.index_only + self.attribute_only

    @property
    def sources(self) -> dict[str, Source]:
        """Return the supporting source(s) for every path."""
        return {name: entry.source for name, entry in self.entries.items()}

    def _entry(self, name: str) -> DirectoryEntry:
        entry = self.entries.get(name)
        if entry is None:
            entry = self.entries[name] = DirectoryEntry(name)
        return entry

    def _add_generation_mismatch(self, mismatch: GenerationMismatch) -> None:
        if mismatch not in self.generation_mismatches:
            self.generation_mismatches.append(mismatch)

    def _add_parent_mismatch(self, mismatch: ParentMismatch) -> None:
        if mismatch not in self.parent_mismatches:
            self.parent_mismatches.append(mismatch)


def catalog(record: MftRecord, ignore_dos: bool = False, allocated_only: bool = False) -> DirectoryCatalog:
    """Build a :class:`DirectoryCatalog` for the given directory record.

    Args:
        record: The directory record to catalog.
        ignore_dos: Ignore DOS file name entries.
        allocated_only: Only consider ``$FILE_NAME`` attributes of allocated MFT records. By default all records
            are included, so paths that only exist as a stale attribute (e.g. deleted files) are still reported.

    Raises:
        NotADirectoryError: If the record is not a directory.
        MftNotAvailableError: If no MFT is available to resolve file references or scan for attributes.
    """
    if not record.is_dir():
        raise NotADirectoryError(f"{record!r} is not a directory")

    if not record.ntfs or not record.ntfs.mft:
        raise MftNotAvailableError

    result = DirectoryCatalog(record)

    try:
        index = record.index("$I30")
    except FileNotFoundError as e:
        index = None
        result.index_available = False
        result.index_error = e

    if index is not None:
        _add_index_entries(result, index, ignore_dos)

    _add_attribute_entries(result, ignore_dos, allocated_only)

    return result


def reconcile(record: MftRecord, ignore_dos: bool = False, allocated_only: bool = False) -> DirectoryCatalog:
    """Alias for :func:`catalog`."""
    return catalog(record, ignore_dos, allocated_only)


def _iter_index_entries(result: DirectoryCatalog, index: Index) -> Iterator[IndexEntry]:
    """Yield index entries from the index root and every index buffer.

    Each structure is enumerated independently, so a broken buffer cannot erase entries from the index root or
    from other buffers. Broken entries within a structure are skipped using their declared length.
    """
    root = index.root
    yield from _iter_structure(result, index, root.fh, c_ntfs._INDEX_ROOT.size - c_ntfs._INDEX_HEADER.size)

    if not index.has_allocation:
        return
    vcn = 0
    while True:
        try:
            buffer = index.index_buffer(vcn)
        except EOFError:
            break
        except Exception as e:  # noqa: BLE001
            if _is_empty_buffer(index, vcn):
                break

            result.broken_index_buffers.append(BrokenIndexBuffer(vcn=vcn, error=e))
            vcn += 1
            continue

        header_offset = c_ntfs._INDEX_ALLOCATION_BUFFER.size - c_ntfs._INDEX_HEADER.size
        yield from _iter_structure(result, index, io.BytesIO(buffer.data), header_offset)
        vcn += 1


def _is_empty_buffer(index: Index, vcn: int) -> bool:
    """Return whether the index allocation region at ``vcn`` is zero-filled (i.e. never allocated)."""
    stream = index.allocation
    if stream is None:
        return True

    offset = vcn << index.vcn_size_shift
    stream.seek(offset)
    return not stream.read(index.root.bytes_per_index_buffer).strip(b"\x00")


def _iter_structure(
    result: DirectoryCatalog,
    index: Index,
    fh: BinaryIO,
    header_offset: int,
) -> Iterator[IndexEntry]:
    try:
        fh.seek(header_offset)
        index_header = c_ntfs._INDEX_HEADER(fh)
    except Exception as e:  # noqa: BLE001
        result.broken_index_entries.append(BrokenIndexEntry(error=e))
        return

    offset = header_offset + index_header.FirstEntryOffset
    max_offset = header_offset + index_header.TotalSizeOfEntries

    while offset < max_offset:
        try:
            fh.seek(offset)
            header = c_ntfs._INDEX_ENTRY(fh)
        except EOFError:
            break
        except Exception as e:  # noqa: BLE001
            result.broken_index_entries.append(BrokenIndexEntry(error=e))
            break

        try:
            entry = IndexEntry(index, fh, offset)
        except EOFError:
            break
        except Exception as e:  # noqa: BLE001
            result.broken_index_entries.append(BrokenIndexEntry(error=e))
            if header.Length == 0:
                break
            offset += header.Length
            continue

        yield entry

        if entry.is_end:
            break

        if entry.length == 0:
            error = ValueError("Zero length index entry")
            result.broken_index_entries.append(BrokenIndexEntry(error=error, entry=entry))
            break

        offset += entry.length


def _add_index_entries(result: DirectoryCatalog, index: Index, ignore_dos: bool) -> None:
    record = result.record
    mft = record.ntfs.mft

    for index_entry in _iter_index_entries(result, index):
        if index_entry.is_end:
            continue
        try:
            file_name = index_entry.attribute
            if file_name is None:
                raise ValueError("Index entry has no $FILE_NAME key")

            name = file_name.file_name
            parent_reference = file_name.attr.ParentDirectory
            file_reference = index_entry.header.FileReference
        except Exception as e:  # noqa: BLE001
            result.broken_index_entries.append(BrokenIndexEntry(error=e, entry=index_entry))
            continue

        if ignore_dos and file_name.flags == FILE_NAME_DOS:
            continue

        entry = result._entry(name)
        entry.index_entries.append(index_entry)

        segment = segment_reference(file_reference)
        try:
            target = mft.get(segment)
        except Exception as e:  # noqa: BLE001
            result.missing_records.append(MissingRecord(name=name, source=Source.INDEX, segment=segment, error=e))
        else:
            entry.add_record(target)

            if file_reference.SequenceNumber != target.header.SequenceNumber:
                result._add_generation_mismatch(
                    GenerationMismatch(
                        name=name,
                        source=Source.INDEX,
                        reference="FileReference",
                        segment=segment,
                        expected=target.header.SequenceNumber,
                        actual=file_reference.SequenceNumber,
                        entry=index_entry,
                    )
                )

            _check_target_parent(result, name, target, index_entry)

        parent_segment = segment_reference(parent_reference)
        if parent_segment != record.segment:
            result._add_parent_mismatch(
                ParentMismatch(
                    name=name,
                    source=Source.INDEX,
                    segment=parent_segment,
                    expected=record.segment,
                    entry=index_entry,
                )
            )
        elif parent_reference.SequenceNumber != record.header.SequenceNumber:
            result._add_generation_mismatch(
                GenerationMismatch(
                    name=name,
                    source=Source.INDEX,
                    reference="ParentDirectory",
                    segment=parent_segment,
                    expected=record.header.SequenceNumber,
                    actual=parent_reference.SequenceNumber,
                    entry=index_entry,
                )
            )


def _check_target_parent(result: DirectoryCatalog, name: str, target: MftRecord, index_entry: IndexEntry) -> None:
    """Compare the index key with the target record's own ``$FILE_NAME`` attributes."""
    try:
        attributes = target.attributes[ATTRIBUTE_TYPE_CODE.FILE_NAME]
    except Exception:  # noqa: BLE001
        return

    for attribute in attributes:
        try:
            file_name = attribute.attribute
            if file_name is None or file_name.file_name.upper() != name.upper():
                continue

            parent_segment = segment_reference(file_name.attr.ParentDirectory)
        except Exception:  # noqa: BLE001
            continue

        if parent_segment != result.record.segment:
            result._add_parent_mismatch(
                ParentMismatch(
                    name=name,
                    source=Source.INDEX,
                    segment=parent_segment,
                    expected=result.record.segment,
                    entry=index_entry,
                    attribute=attribute,
                )
            )


def _add_attribute_entries(result: DirectoryCatalog, ignore_dos: bool, allocated_only: bool) -> None:
    record = result.record

    for owner, attribute in record.ntfs.mft.children(record.segment, allocated_only=allocated_only):
        try:
            file_name = attribute.attribute
            if file_name is None:
                raise ValueError("Attribute has no $FILE_NAME value")

            name = file_name.file_name
            parent_reference = file_name.attr.ParentDirectory
        except Exception as e:  # noqa: BLE001
            result.broken_file_names.append(BrokenFileName(error=e, attribute=attribute))
            continue

        if ignore_dos and attribute.flags == FILE_NAME_DOS:
            continue

        entry = result._entry(name)
        entry.file_names.append(file_name)
        entry.add_record(owner)

        if parent_reference.SequenceNumber != record.header.SequenceNumber:
            result._add_generation_mismatch(
                GenerationMismatch(
                    name=name,
                    source=Source.ATTRIBUTE,
                    reference="ParentDirectory",
                    segment=record.segment,
                    expected=record.header.SequenceNumber,
                    actual=parent_reference.SequenceNumber,
                    attribute=attribute,
                )
            )
