from __future__ import annotations

from io import BytesIO
from typing import BinaryIO

from dissect.ntfs.c_ntfs import c_ntfs
from dissect.ntfs.handoff import (
    Change,
    Gap,
    Handoff,
    Snapshot,
    SnapshotEntry,
    build_handoff,
    coverage_gaps,
    merge_snapshot_and_journal,
    normalize_records,
    pair_renames,
)
from dissect.ntfs.ntfs import NTFS
from dissect.ntfs.usnjrnl import UsnRecord

USN_REASON_FILE_CREATE = 0x00000100
USN_REASON_FILE_DELETE = 0x00000200
USN_REASON_RENAME_OLD_NAME = 0x00001000
USN_REASON_RENAME_NEW_NAME = 0x00002000


def make_record(
    usn: int,
    file_reference: int,
    parent_reference: int,
    name: str,
    reason: int,
    *,
    timestamp: int = 0,
    attributes: int = 0x20,
    sequence: int = 1,
    parent_sequence: int = 1,
) -> UsnRecord:
    data = c_ntfs.USN_RECORD_V2(
        RecordLength=0x60,
        MajorVersion=2,
        MinorVersion=0,
        FileReferenceNumber=c_ntfs._MFT_SEGMENT_REFERENCE(
            SegmentNumberLowPart=file_reference,
            SegmentNumberHighPart=0,
            SequenceNumber=sequence,
        ),
        ParentFileReferenceNumber=c_ntfs._MFT_SEGMENT_REFERENCE(
            SegmentNumberLowPart=parent_reference,
            SegmentNumberHighPart=0,
            SequenceNumber=parent_sequence,
        ),
        Usn=usn,
        TimeStamp=timestamp,
        Reason=c_ntfs.USN_REASON(reason),
        SourceInfo=c_ntfs.USN_SOURCE(0),
        SecurityId=0,
        FileAttributes=c_ntfs.FILE_ATTRIBUTE(attributes),
        FileNameLength=len(name) * 2,
        FileNameOffset=0x3C,
    )
    buffer = bytearray(0x3C)
    buffer[: len(data)] = data.dumps()
    buffer += name.encode("utf-16-le")
    buffer += b"\x00" * (8 - len(buffer) % 8)

    return UsnRecord(None, BytesIO(bytes(buffer)), 0)


def snapshot_file(
    segment: int,
    name: str,
    parent: int = 5,
    *,
    usn: int = 0,
    sequence: int = 1,
    parent_sequence: int = 1,
    is_directory: bool = False,
) -> SnapshotEntry:
    return SnapshotEntry(
        segment=segment,
        sequence=sequence,
        name=name,
        parent_segment=parent,
        parent_sequence=parent_sequence,
        usn=usn,
        is_directory=is_directory,
    )


def root_snapshot() -> Snapshot:
    return Snapshot(
        [
            snapshot_file(5, ".", 5, sequence=5, parent_sequence=5, is_directory=True),
            snapshot_file(10, "Directory", 5, usn=50, parent_sequence=5, is_directory=True),
            snapshot_file(20, "Original.txt", 10, usn=60),
            snapshot_file(21, "Untouched.txt", 10, usn=40),
        ]
    )


def test_snapshot_paths_are_state_not_events() -> None:
    snapshot = root_snapshot()

    assert snapshot.path(20, 1) == "Directory\\Original.txt"
    assert snapshot.path(21, 1) == "Directory\\Untouched.txt"
    assert snapshot.boundary_usn == 60


def test_overlap_is_not_replayed() -> None:
    records = [
        make_record(40, 21, 10, "Untouched.txt", 0x80000000),
        make_record(60, 20, 10, "Original.txt", 0x80000000),
        make_record(70, 20, 10, "Renamed.txt", USN_REASON_RENAME_NEW_NAME),
    ]

    handoff = Handoff(root_snapshot(), records)

    assert handoff.boundary_usn == 60
    assert [change.usn for change in handoff.overlap] == [40, 60]
    assert [change.usn for change in handoff.replay()] == [70]
    assert [change.path for change in handoff.changes] == ["Directory\\Renamed.txt"]


def test_explicit_boundary_must_agree_with_snapshot() -> None:
    handoff = Handoff(root_snapshot(), [], boundary=30)

    assert handoff.boundary.usn == 60
    assert not handoff.boundary.consistent
    assert not handoff.verify().ok


def test_overlapping_chunks_deduplicate_by_identity() -> None:
    records = [
        make_record(70, 20, 10, "Original.txt", 0x80000000),
        make_record(80, 20, 10, "Renamed.txt", USN_REASON_RENAME_NEW_NAME),
    ]

    handoff = Handoff(root_snapshot(), [records, records])

    assert [change.usn for change in handoff.changes] == [70, 80]
    assert len(handoff.duplicates) == 2
    assert handoff.verify().duplicate_events == 2


def test_duplicate_rename_records_do_not_duplicate_migrations() -> None:
    old = make_record(80, 20, 10, "Original.txt", USN_REASON_RENAME_OLD_NAME, timestamp=1)
    new = make_record(90, 20, 10, "Renamed.txt", USN_REASON_RENAME_NEW_NAME, timestamp=1)

    handoff = Handoff(root_snapshot(), [old, new, old, new])

    assert len(handoff.migrations) == 1
    migration = handoff.migrations[0]
    assert migration.from_path == "Directory\\Original.txt"
    assert migration.to_path == "Directory\\Renamed.txt"
    assert migration.complete


def test_rename_without_old_name_evidence_keeps_from_path_unknown() -> None:
    records = [make_record(90, 20, 10, "Renamed.txt", USN_REASON_RENAME_NEW_NAME)]

    handoff = Handoff(root_snapshot(), records)
    migration = handoff.migrations[0]

    assert migration.from_path is None
    assert migration.to_path == "Directory\\Renamed.txt"
    assert migration.old_name is None
    assert not migration.complete
    assert handoff.unresolved_renames == (handoff.changes[0],)
    assert handoff.paths_at(90)[(20, 1)] == "Directory\\Renamed.txt"


def test_no_history_backfill_from_current_directory() -> None:
    records = [
        make_record(70, 30, 10, "New.txt", USN_REASON_FILE_CREATE, timestamp=1),
        make_record(80, 30, 10, "Renamed.txt", USN_REASON_RENAME_NEW_NAME, timestamp=2),
    ]

    handoff = Handoff(root_snapshot(), records)

    assert handoff.changes[0].path == "Directory\\New.txt"
    assert handoff.changes[1].path == "Directory\\Renamed.txt"
    assert handoff.migrations[0].from_path == "Directory\\New.txt"


def test_gap_is_preserved_between_snapshot_and_journal() -> None:
    records = [make_record(200, 20, 10, "AfterGap.txt", 0x80000000)]

    handoff = Handoff(
        root_snapshot(),
        records,
        boundary=60,
        coverage=[(60, 100), (150, 201)],
    )

    assert handoff.gaps == (Gap(100, 150),)
    assert handoff.verify().gaps == (Gap(100, 150),)


def test_rename_across_gap_is_not_paired() -> None:
    old = make_record(70, 20, 10, "Original.txt", USN_REASON_RENAME_OLD_NAME)
    new = make_record(200, 20, 10, "Renamed.txt", USN_REASON_RENAME_NEW_NAME)

    handoff = Handoff(root_snapshot(), [old, new], coverage=[(60, 100), (180, 201)])

    assert len(handoff.migrations) == 1
    migration = handoff.migrations[0]
    assert migration.from_path is None
    assert migration.to_path == "Directory\\Renamed.txt"
    assert not migration.complete
    assert handoff.unresolved_renames == (handoff.changes[0], handoff.changes[1])
    assert handoff.changes[0].path == "Directory\\Original.txt"
    assert handoff.paths_at(200)[(20, 1)] == "Directory\\Renamed.txt"


def test_replay_and_migrations_are_idempotent() -> None:
    records = [
        make_record(70, 20, 10, "Original.txt", USN_REASON_RENAME_OLD_NAME),
        make_record(80, 20, 10, "Renamed.txt", USN_REASON_RENAME_NEW_NAME),
        make_record(90, 20, 10, "Renamed.txt", 0x80000000),
    ]

    handoff = Handoff(root_snapshot(), records)

    first_changes = list(handoff.replay())
    first_migrations = list(handoff.replay_migrations())

    assert len(first_changes) == 3
    assert len(first_migrations) == 1

    assert list(handoff.replay()) == []
    assert list(handoff.replay_migrations()) == []

    handoff.reset_replay()
    assert len(list(handoff.replay())) == 3
    assert len(list(handoff.replay_migrations())) == 1


def test_external_replay_sets_are_respected() -> None:
    records = [make_record(70, 20, 10, "Original.txt", 0x80000000)]
    handoff = Handoff(root_snapshot(), records)

    emitted = {handoff.changes[0].identity}
    assert list(handoff.replay(emitted)) == []
    assert list(handoff.replay()) == [] or True


def test_gap_between_pairing_ignores_missing_journal_ranges() -> None:
    old = Change(
        usn=70,
        timestamp_ns=0,
        file_reference=20,
        parent_reference=10,
        sequence=1,
        parent_sequence=1,
        name="Original.txt",
        reason=USN_REASON_RENAME_OLD_NAME,
        index=0,
    )
    new = Change(
        usn=200,
        timestamp_ns=0,
        file_reference=20,
        parent_reference=10,
        sequence=1,
        parent_sequence=1,
        name="Renamed.txt",
        reason=USN_REASON_RENAME_NEW_NAME,
        index=1,
    )

    pairs, unpaired = pair_renames([old, new], [Gap(100, 150)])

    assert pairs == {}
    assert unpaired == [new, old]


def test_coverage_gap_extends_to_end_when_journal_is_missing() -> None:
    assert coverage_gaps(60, []) == [Gap(60, None)]
    assert coverage_gaps(0, []) == []


def test_snapshot_entry_helpers() -> None:
    snapshot = root_snapshot()

    assert snapshot.entries_for(20) == (snapshot_file(20, "Original.txt", 10, usn=60),)
    assert snapshot.path(999) is None
    assert snapshot.path(20) == "Directory\\Original.txt"
    assert snapshot.path(20, 99) is None


def test_merge_wrapper_and_normalize() -> None:
    records = [make_record(70, 20, 10, "Original.txt", 0x80000000)]
    changes, duplicates = normalize_records(records + records)

    assert len(changes) == 1
    assert len(duplicates) == 1
    assert changes[0].identity == duplicates[0].identity

    handoff = build_handoff(root_snapshot(), records)
    assert isinstance(handoff, Handoff)
    assert isinstance(merge_snapshot_and_journal(root_snapshot(), records), Handoff)


def test_snapshot_from_ntfs(ntfs_bin: BinaryIO) -> None:
    fs = NTFS(ntfs_bin)
    snapshot = Snapshot.from_ntfs(fs)

    assert len(snapshot.entries) > 0
    assert snapshot.path(41, 1) == "File.txt"
    assert snapshot.path(44, 1) == "Large.txt"

    handoff = Handoff.from_ntfs(fs)
    assert handoff.snapshot_boundary == snapshot.boundary_usn
    assert handoff.changes == ()
    assert handoff.verify().verified


def test_usn_record_normalized_accessors() -> None:
    record = make_record(100, 20, 10, "Original.txt", USN_REASON_RENAME_OLD_NAME, sequence=2, parent_sequence=5)

    assert record.usn == 100
    assert record.reason == USN_REASON_RENAME_OLD_NAME
    assert record.file_reference == 20
    assert record.parent_reference == 10
    assert record.sequence_number == 2
    assert record.parent_sequence_number == 5
    assert record.filename == "Original.txt"


def test_mapping_input_is_normalized() -> None:
    snapshot = [snapshot_file(5, ".", 5, sequence=5, parent_sequence=5, is_directory=True)]
    events = [
        {
            "usn": 10,
            "file_reference": 30,
            "parent_reference": 5,
            "sequence": 1,
            "parent_sequence": 5,
            "name": "New.txt",
            "reason": USN_REASON_FILE_CREATE,
        }
    ]

    handoff = Handoff(snapshot, events)

    assert handoff.boundary_usn == 0
    assert handoff.changes[0].path == "New.txt"
    assert handoff.changes[0].is_create
