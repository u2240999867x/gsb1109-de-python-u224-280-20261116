from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING, Any, BinaryIO

from dissect.util.stream import RunlistStream
from dissect.util.ts import wintimestamp

from dissect.ntfs.c_ntfs import USN_PAGE_SIZE, c_ntfs
from dissect.ntfs.exceptions import Error
from dissect.ntfs.util import segment_reference, ts_to_ns

if TYPE_CHECKING:
    from collections.abc import Hashable, Iterator
    from datetime import datetime

    from dissect.ntfs.mft import MftRecord
    from dissect.ntfs.ntfs import NTFS


class UsnJrnl:
    """Parse the USN journal from a file-like object of the ``$UsnJrnl:$J`` stream.

    Args:
        fh: A file-like object of the $UsnJrnl:$J stream.
        ntfs: An optional :class:`~dissect.ntfs.ntfs.NTFS` class instance, used for resolving file paths.
    """

    def __init__(self, fh: BinaryIO, ntfs: NTFS | None = None):
        self.fh = fh
        self.ntfs = ntfs

    def records(self) -> Iterator[UsnRecord]:
        """Yield all parsed USN records.

        Only yields version 2 USN records, other record versions are ignored.
        """
        fh = self.fh
        offset = 0

        if isinstance(fh, RunlistStream):
            for run_offset, run_size in fh.runlist:
                if run_offset is not None:
                    break

                offset += run_size * fh.block_size

        while True:
            fh.seek(offset)

            if fh.read(4) == b"\x00" * 4:
                # Increment to USN_PAGE_SIZE
                offset += USN_PAGE_SIZE - (offset % USN_PAGE_SIZE)
                continue

            try:
                record = UsnRecord(self, fh, offset)
                if record.header.MajorVersion == 2:
                    yield record
            except EOFError:
                break

            offset += record.record.RecordLength
            if offset % 8:
                offset += -(offset) & (8 - 1)

    def migrations(self) -> Iterator[UsnMigration]:
        """Yield reconstructed path migrations (renames) from the USN records.

        A rename is recorded in the USN journal as a record with the ``RENAME_OLD_NAME`` reason,
        followed by a record with the ``RENAME_NEW_NAME`` reason. Both halves do not have to be
        adjacent, other file events may be recorded in between. Halves are paired by their complete
        file reference (segment number and sequence number) and only when the old-name half
        explicitly precedes the new-name half in USN order.

        If either half is missing, the migration is kept as a single-sided change. A gap in USN
        order, e.g. caused by journal wraparound, is not inferred as a deletion; pending old-name
        halves from before the gap are yielded as single-sided migrations instead.

        A single record can carry multiple reason flags (e.g. ``RENAME_OLD_NAME | CLOSE``); every
        record is only stored once as the original event and contributes to at most one migration.
        """
        pending: dict[Hashable, list[UsnRecord]] = {}
        last_usn: int | None = None

        def flush_pending() -> Iterator[UsnMigration]:
            remaining = [record for half in pending.values() for record in half]
            pending.clear()
            for record in sorted(remaining, key=lambda record: record.record.Usn):
                yield UsnMigration(old_record=record)

        for record in self.records():
            usn = record.record.Usn
            if last_usn is not None and usn < last_usn:
                # The journal wrapped around, sequence evidence across the gap is unreliable
                yield from flush_pending()
            last_usn = usn

            reason = record.record.Reason
            key = _file_reference_key(record.record.FileReferenceNumber)

            if reason & c_ntfs.USN_REASON.RENAME_NEW_NAME:
                old_record = None
                if halves := pending.get(key):
                    if halves[-1].record.Usn < usn:
                        old_record = halves.pop()
                    if not halves:
                        del pending[key]
                yield UsnMigration(old_record=old_record, new_record=record)
            elif reason & c_ntfs.USN_REASON.RENAME_OLD_NAME:
                pending.setdefault(key, []).append(record)

        yield from flush_pending()


class UsnRecord:
    """Parse a USN record from a file-like object and offset.

    Args:
        usnjrnl: The :class:`UsnJrnl` class this record is parsed from.
        fh: The file-like object to parse a USN record from.
        offset: The offset to parse a USN record at.
    """

    def __init__(self, usnjrnl: UsnJrnl, fh: BinaryIO, offset: int):
        self.usnjrnl = usnjrnl
        self.offset = offset
        self.extents = []

        fh.seek(offset)
        self.header = c_ntfs.USN_RECORD_COMMON_HEADER(fh)

        fh.seek(offset)
        if self.header.MajorVersion == 2:
            self.record = c_ntfs.USN_RECORD_V2(fh)
        elif self.header.MajorVersion == 3:
            self.record = c_ntfs.USN_RECORD_V3(fh)
        elif self.header.MajorVersion == 4:
            self.record = c_ntfs.USN_RECORD_V4(fh)
            for _ in range(self.record.NumberOfExtents):
                self.extents.append(c_ntfs.USN_RECORD_EXTENT(fh))
        else:
            raise ValueError(f"Unsupported USN_RECORD version: {self.header.MajorVersion}")

        if self.header.MajorVersion in (2, 3):
            fh.seek(offset + self.record.FileNameOffset)
            self.filename = fh.read(self.record.FileNameLength).decode("utf-16-le")
        else:
            self.filename = None

    def __repr__(self) -> str:
        return f"<UsnRecord {self.record.Usn}>"

    def __getattr__(self, attr: str) -> Any:
        return getattr(self.record, attr)

    @cached_property
    def file(self) -> MftRecord | None:
        if self.usnjrnl.ntfs and self.usnjrnl.ntfs.mft:
            return self.usnjrnl.ntfs.mft(self.record.FileReferenceNumber)
        return None

    @cached_property
    def parent(self) -> MftRecord | None:
        if self.usnjrnl.ntfs and self.usnjrnl.ntfs.mft:
            return self.usnjrnl.ntfs.mft(self.record.ParentFileReferenceNumber)
        return None

    @property
    def timestamp(self) -> datetime:
        return wintimestamp(self.record.TimeStamp)

    @property
    def timestamp_ns(self) -> int:
        return ts_to_ns(self.record.TimeStamp)

    @cached_property
    def full_path(self) -> str:
        try:
            parent = self.parent
        except Error:
            parent = None

        ref = segment_reference(self.record.ParentFileReferenceNumber)
        if parent is None:
            parent_path = f"<unavailable_reference_0x{ref:x}#{self.record.ParentFileReferenceNumber.SequenceNumber}>"
        elif parent.header.SequenceNumber == self.record.ParentFileReferenceNumber.SequenceNumber:
            parent_path = parent.full_path()
        else:
            parent_path = f"<broken_reference_0x{ref:x}#{self.record.ParentFileReferenceNumber.SequenceNumber}>"

        return f"{parent_path}\\{self.filename}"


class UsnMigration:
    """A path migration of a file, reconstructed from USN journal rename records.

    Args:
        old_record: The USN record with the ``RENAME_OLD_NAME`` reason, if available.
        new_record: The USN record with the ``RENAME_NEW_NAME`` reason, if available.
    """

    def __init__(self, old_record: UsnRecord | None = None, new_record: UsnRecord | None = None):
        self.old_record = old_record
        self.new_record = new_record

    def __repr__(self) -> str:
        return f"<UsnMigration {self.old_path} -> {self.new_path}>"

    @property
    def record(self) -> UsnRecord:
        """The most recent USN record of this migration."""
        return self.new_record or self.old_record

    @property
    def complete(self) -> bool:
        """Whether both the old-name and new-name half of this migration were found."""
        return self.old_record is not None and self.new_record is not None

    @property
    def file_reference(self) -> c_ntfs.MFT_SEGMENT_REFERENCE:
        """The complete file reference of the migrated file."""
        return self.record.record.FileReferenceNumber

    @property
    def old_filename(self) -> str | None:
        """The filename before the migration, if the old-name half was found."""
        return self.old_record.filename if self.old_record else None

    @property
    def new_filename(self) -> str | None:
        """The filename after the migration, if the new-name half was found."""
        return self.new_record.filename if self.new_record else None

    @property
    def old_path(self) -> str | None:
        """The full path before the migration, if the old-name half was found."""
        return self.old_record.full_path if self.old_record else None

    @property
    def new_path(self) -> str | None:
        """The full path after the migration, if the new-name half was found."""
        return self.new_record.full_path if self.new_record else None

    @property
    def timestamp(self) -> datetime:
        return self.record.timestamp

    @property
    def timestamp_ns(self) -> int:
        return self.record.timestamp_ns

    @property
    def usn(self) -> int:
        return self.record.record.Usn


def _file_reference_key(reference: c_ntfs.MFT_SEGMENT_REFERENCE | c_ntfs.FILE_ID_128) -> Hashable:
    """Return a hashable key of the complete file reference, including the sequence number."""
    if hasattr(reference, "SequenceNumber"):
        return (segment_reference(reference), reference.SequenceNumber)
    return (bytes(reference.Identifier),)
