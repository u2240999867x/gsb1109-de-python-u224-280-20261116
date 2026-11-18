from __future__ import annotations

import struct
from io import BytesIO

from dissect.ntfs.usnjrnl import UsnJrnl, UsnRecord

USN_REASON_DATA_OVERWRITE = 0x00000001
USN_REASON_FILE_CREATE = 0x00000100
USN_REASON_RENAME_OLD_NAME = 0x00001000
USN_REASON_RENAME_NEW_NAME = 0x00002000
USN_REASON_CLOSE = 0x80000000


def usn_record_v2(
    filename: str,
    usn: int,
    reason: int,
    segment: int = 0x100,
    sequence_number: int = 1,
    parent_segment: int = 0x5,
    parent_sequence_number: int = 1,
    timestamp: int = 0x01D7A4861A645262,
) -> bytes:
    name = filename.encode("utf-16-le")
    record = struct.pack("<IHH", 0, 2, 0)
    record += struct.pack("<IHH", segment & 0xFFFFFFFF, (segment >> 32) & 0xFFFF, sequence_number)
    record += struct.pack(
        "<IHH", parent_segment & 0xFFFFFFFF, (parent_segment >> 32) & 0xFFFF, parent_sequence_number
    )
    record += struct.pack("<QQIIIIHH", usn, timestamp, reason, 0, 0, 0x20, len(name), 0x3C)
    record += name

    length = len(record)
    if length % 8:
        length += 8 - (length % 8)
    record = record.ljust(length, b"\x00")

    return struct.pack("<I", length) + record[4:]


def usn_journal(*records: bytes) -> UsnJrnl:
    return UsnJrnl(BytesIO(b"".join(records)))


def test_usnjrnl_record_v4() -> None:
    data = bytes.fromhex(
        "5000000004000000c1000000000001000000000000000000bf00000000000100"
        "0000000000000000d00201000000000003810080000000000000000001001000"
        "00000000000000000040280000000000"
    )
    record = UsnRecord(None, BytesIO(data), 0)
    assert len(record.extents) == 1
    assert record.extents[0].Offset == 0
    assert record.extents[0].Length == 0x284000


def test_usnjrnl_record_v2() -> None:
    data = bytes.fromhex(
        "5800000002000000c100000000000100bf000000000001002003010000000000"
        "6252641a86a4d7010381008000000000000000002000000018003c0069007300"
        "2d00310035005000320036002e0074006d00700000000000"
    )
    record = UsnRecord(None, BytesIO(data), 0)
    assert record.filename == "is-15P26.tmp"
    assert str(record.timestamp) == "2021-09-08 07:49:50.607420+00:00"


def test_usnjrnl_migrations_pair_across_events() -> None:
    # Old-name and new-name records may be separated by other file events
    journal = usn_journal(
        usn_record_v2("old.txt", usn=10, reason=USN_REASON_RENAME_OLD_NAME, segment=0x100),
        usn_record_v2("unrelated.txt", usn=11, reason=USN_REASON_FILE_CREATE, segment=0x200),
        usn_record_v2("other.txt", usn=12, reason=USN_REASON_DATA_OVERWRITE, segment=0x300),
        usn_record_v2("new.txt", usn=13, reason=USN_REASON_RENAME_NEW_NAME, segment=0x100),
    )

    migrations = list(journal.migrations())
    assert len(migrations) == 1

    migration = migrations[0]
    assert migration.complete
    assert migration.old_filename == "old.txt"
    assert migration.new_filename == "new.txt"
    assert migration.old_path == "<unavailable_reference_0x5#1>\\old.txt"
    assert migration.new_path == "<unavailable_reference_0x5#1>\\new.txt"
    assert migration.usn == 13


def test_usnjrnl_migrations_full_file_reference() -> None:
    # The same segment number with a different sequence number is a different file
    journal = usn_journal(
        usn_record_v2("old.txt", usn=10, reason=USN_REASON_RENAME_OLD_NAME, segment=0x100, sequence_number=1),
        usn_record_v2("new.txt", usn=11, reason=USN_REASON_RENAME_NEW_NAME, segment=0x100, sequence_number=2),
    )

    migrations = list(journal.migrations())
    assert len(migrations) == 2
    assert all(not migration.complete for migration in migrations)

    assert migrations[0].old_record is None
    assert migrations[0].new_filename == "new.txt"
    assert migrations[1].new_record is None
    assert migrations[1].old_filename == "old.txt"


def test_usnjrnl_migrations_sequence_evidence() -> None:
    # Pairing requires the old-name half to explicitly precede the new-name half in USN order
    journal = usn_journal(
        usn_record_v2("new.txt", usn=10, reason=USN_REASON_RENAME_NEW_NAME, segment=0x100),
        usn_record_v2("old.txt", usn=11, reason=USN_REASON_RENAME_OLD_NAME, segment=0x100),
    )

    migrations = list(journal.migrations())
    assert len(migrations) == 2
    assert all(not migration.complete for migration in migrations)
    assert migrations[0].new_filename == "new.txt"
    assert migrations[1].old_filename == "old.txt"


def test_usnjrnl_migrations_single_sided() -> None:
    # Missing halves are kept as single-sided changes
    journal = usn_journal(
        usn_record_v2("gone.txt", usn=10, reason=USN_REASON_RENAME_OLD_NAME, segment=0x100),
        usn_record_v2("appeared.txt", usn=11, reason=USN_REASON_RENAME_NEW_NAME, segment=0x200),
    )

    migrations = list(journal.migrations())
    assert len(migrations) == 2

    assert migrations[0].old_record is None
    assert migrations[0].new_filename == "appeared.txt"
    assert migrations[0].old_path is None

    assert migrations[1].new_record is None
    assert migrations[1].old_filename == "gone.txt"
    assert migrations[1].new_path is None


def test_usnjrnl_migrations_wraparound_not_deletion() -> None:
    # A gap from journal wraparound is not inferred as a deletion and does not pair across the gap
    journal = usn_journal(
        usn_record_v2("old.txt", usn=100, reason=USN_REASON_RENAME_OLD_NAME, segment=0x100),
        usn_record_v2("unrelated.txt", usn=5, reason=USN_REASON_FILE_CREATE, segment=0x200),
        usn_record_v2("new.txt", usn=6, reason=USN_REASON_RENAME_NEW_NAME, segment=0x100),
    )

    migrations = list(journal.migrations())
    assert len(migrations) == 2
    assert all(not migration.complete for migration in migrations)

    assert migrations[0].old_filename == "old.txt"
    assert migrations[0].new_record is None
    assert migrations[1].new_filename == "new.txt"
    assert migrations[1].old_record is None


def test_usnjrnl_migrations_multiple_flags_single_event() -> None:
    # A record with multiple reason flags is stored once as the original event
    journal = usn_journal(
        usn_record_v2("old.txt", usn=10, reason=USN_REASON_RENAME_OLD_NAME | USN_REASON_CLOSE, segment=0x100),
        usn_record_v2("new.txt", usn=11, reason=USN_REASON_RENAME_NEW_NAME | USN_REASON_CLOSE, segment=0x100),
    )

    migrations = list(journal.migrations())
    assert len(migrations) == 1

    migration = migrations[0]
    assert migration.complete
    assert migration.old_record.record.Reason == USN_REASON_RENAME_OLD_NAME | USN_REASON_CLOSE
    assert migration.new_record.record.Reason == USN_REASON_RENAME_NEW_NAME | USN_REASON_CLOSE


def test_usnjrnl_migrations_multiple_renames_same_file() -> None:
    journal = usn_journal(
        usn_record_v2("a.txt", usn=10, reason=USN_REASON_RENAME_OLD_NAME, segment=0x100),
        usn_record_v2("b.txt", usn=11, reason=USN_REASON_RENAME_NEW_NAME, segment=0x100),
        usn_record_v2("b.txt", usn=12, reason=USN_REASON_RENAME_OLD_NAME, segment=0x100),
        usn_record_v2("c.txt", usn=13, reason=USN_REASON_RENAME_NEW_NAME, segment=0x100),
    )

    migrations = list(journal.migrations())
    assert len(migrations) == 2
    assert all(migration.complete for migration in migrations)
    assert (migrations[0].old_filename, migrations[0].new_filename) == ("a.txt", "b.txt")
    assert (migrations[1].old_filename, migrations[1].new_filename) == ("b.txt", "c.txt")
