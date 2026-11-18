from __future__ import annotations

import io
import struct
from typing import BinaryIO
from unittest.mock import Mock

import pytest

from dissect.ntfs.c_ntfs import ATTRIBUTE_TYPE_CODE, c_ntfs
from dissect.ntfs.exceptions import DanglingReferenceError
from dissect.ntfs.index import IndexEntry
from dissect.ntfs.mft import MftRecord
from dissect.ntfs.ntfs import NTFS
from dissect.ntfs.util import FileReference


def make_record(segment: int, sequence: int) -> MftRecord:
    record = MftRecord()
    record.segment = segment
    record.header = Mock(SequenceNumber=sequence)
    return record


def make_mft(records: dict[int, MftRecord]) -> Mock:
    mft = Mock()

    def _get(ref: object, *args: object, **kwargs: object) -> MftRecord:
        if isinstance(ref, c_ntfs._MFT_SEGMENT_REFERENCE):
            ref = FileReference.from_reference(ref)

        if isinstance(ref, FileReference):
            record = records[ref.segment]
            if record.header.SequenceNumber != ref.sequence:
                raise DanglingReferenceError
            return record

        return records[ref]

    mft.get = _get
    mft.side_effect = _get
    return mft


def test_file_reference_identity() -> None:
    reference = c_ntfs._MFT_SEGMENT_REFERENCE(SegmentNumberLowPart=42, SequenceNumber=3)
    full = FileReference.from_reference(reference)

    assert full == FileReference(42, 3)
    assert full == FileReference.from_reference(reference)
    assert full != FileReference(42, 4)
    assert str(full) == "0x2a#3"


def test_mft_get_validates_sequence_number() -> None:
    record = make_record(42, 3)
    mft = make_mft({42: record})
    assert mft.get(FileReference(42, 3)) is record
    assert record.reference == FileReference(42, 3)

    with pytest.raises(DanglingReferenceError):
        mft.get(FileReference(42, 4))


def test_index_entry_reference_is_not_rebound() -> None:
    data = c_ntfs._MFT_SEGMENT_REFERENCE(SegmentNumberLowPart=42, SequenceNumber=3).dumps() + struct.pack(
        "<HHHH", len(c_ntfs._INDEX_ENTRY), 0, 0, 0
    )
    entry = IndexEntry(Mock(), io.BytesIO(data), 0)

    assert entry.reference.segment == 42
    assert entry.reference.sequence == 3


def test_attribute_type_import() -> None:
    assert ATTRIBUTE_TYPE_CODE.FILE_NAME


def test_mft_get_rejects_reused_record(mft_bin: BinaryIO) -> None:
    fs = NTFS(mft=mft_bin)
    record = fs.mft.get(FileReference(33, 2))

    assert record.filename == "Random.bin"
    assert record.reference == FileReference(33, 2)

    with pytest.raises(DanglingReferenceError):
        fs.mft.get(FileReference(33, 1))
