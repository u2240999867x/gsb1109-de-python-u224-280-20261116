from __future__ import annotations

import io
from unittest.mock import Mock

import pytest

from dissect.ntfs.c_ntfs import ATTRIBUTE_TYPE_CODE, c_ntfs
from dissect.ntfs.exceptions import DanglingReferenceError
from dissect.ntfs.index import IndexEntry
from dissect.ntfs.mft import MftRecord
from dissect.ntfs.util import FileReference


def _filename(name: str, parent: tuple[int, int] = (5, 5)) -> bytes:
    record = c_ntfs._FILE_NAME(
        ParentDirectory=c_ntfs._MFT_SEGMENT_REFERENCE(SegmentNumberLowPart=parent[0], SequenceNumber=parent[1]),
        FileNameLength=len(name),
        FileName=name,
        FileAttributes=0x20,
    )
    return record.dumps()


def make_entry(name: str, reference: tuple[int, int], parent: tuple[int, int] = (5, 5)) -> IndexEntry:
    attribute = _filename(name, parent)
    header = c_ntfs._INDEX_ENTRY(
        Length=len(c_ntfs._INDEX_ENTRY) + len(attribute),
        KeyLength=len(attribute),
    )
    data = (
        c_ntfs._MFT_SEGMENT_REFERENCE(SegmentNumberLowPart=reference[0], SequenceNumber=reference[1]).dumps()
        + header.dumps()[8:]
        + attribute
    )

    record = Mock()
    record.segment = 5
    record.header = Mock(SequenceNumber=5)
    record.reference = FileReference(5, 5)
    root = Mock(attribute_type=ATTRIBUTE_TYPE_CODE.FILE_NAME)
    index = Mock(record=record, root=root)
    return IndexEntry(index, io.BytesIO(data), 0)


def test_index_entry_keeps_reference_and_parent() -> None:
    entry = make_entry("name.txt", (42, 3), (37, 1))

    assert entry.name == "name.txt"
    assert entry.reference == FileReference(42, 3)
    assert entry.parent == FileReference(37, 1)
    assert entry.source == FileReference(5, 5)


def test_dangling_reference_is_not_followed() -> None:
    entry = make_entry("old.txt", (42, 3))
    mft = Mock()
    mft.get = Mock(side_effect=DanglingReferenceError)
    entry.index.record.ntfs = Mock(mft=mft)

    assert entry.is_dangling
    assert entry.record is None
    with pytest.raises(DanglingReferenceError):
        entry.dereference()
    assert mft.get.call_args.args[0] == FileReference(42, 3)


def test_listdir_keeps_duplicate_names_and_entry_parent() -> None:
    root = Mock()
    root.is_dir = Mock(return_value=True)

    entries = [
        make_entry("same.txt", (42, 1)),
        make_entry("same.txt", (43, 1)),
        make_entry("other.txt", (44, 1)),
    ]
    index = Mock()
    index.entries = Mock(return_value=iter(entries))
    root.index = Mock(return_value=index)
    root.iterdir = Mock(side_effect=lambda *args, **kwargs: iter(entries))

    result = MftRecord.listdir(root)

    assert list(result) == ["same.txt", "other.txt"]
    assert result["same.txt"] is entries[1]
