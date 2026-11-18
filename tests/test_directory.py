from __future__ import annotations

import io
import struct
from types import SimpleNamespace
from typing import Any, BinaryIO

from dissect.ntfs.c_ntfs import ATTRIBUTE_TYPE_CODE, COLLATION, INDEX_ENTRY_END, c_ntfs
from dissect.ntfs.directory import Source, catalog
from dissect.ntfs.exceptions import BrokenIndexError, BrokenMftError
from dissect.ntfs.ntfs import NTFS


def _reference(segment: int, sequence: int) -> bytes:
    return struct.pack("<I", segment) + b"\x00\x00" + struct.pack("<H", sequence)


def _file_name(name: str, parent: int, parent_sequence: int) -> bytes:
    return (
        _reference(parent, parent_sequence)
        + b"\x00" * 8 * 4
        + b"\x00" * 8 * 2
        + b"\x00" * 4
        + b"\x00" * 4
        + bytes([len(name), 0])
        + name.encode("utf-16-le")
    )


def _index_entry(file_name: bytes, segment: int, sequence: int, flags: int = 0) -> bytes:
    header = (
        _reference(segment, sequence)
        + struct.pack("<HHHH", c_ntfs._INDEX_ENTRY.size + len(file_name), len(file_name), flags, 0)
    )
    return header + file_name


def _end_entry() -> bytes:
    return _reference(0, 0) + struct.pack("<HHHH", c_ntfs._INDEX_ENTRY.size, 0, INDEX_ENTRY_END, 0)


def mock_index(entries: list[bytes], attribute_type: int = ATTRIBUTE_TYPE_CODE.FILE_NAME) -> Any:
    body = b"".join(entries)
    root_header = c_ntfs._INDEX_HEADER(
        FirstEntryOffset=c_ntfs._INDEX_ROOT.size - c_ntfs._INDEX_HEADER.size,
        TotalSizeOfEntries=len(body),
        AllocatedSize=len(body),
        Flags=0,
    )
    root = c_ntfs._INDEX_ROOT(
        AttributeType=attribute_type,
        CollationRule=COLLATION.FILE_NAME,
        BytesPerIndexBuffer=0x1000,
        ClustersPerIndexBuffer=1,
        IndexHeader=root_header,
    )
    data = root.dumps() + body

    return SimpleNamespace(
        root=SimpleNamespace(fh=io.BytesIO(data), attribute_type=attribute_type),
        record=None,
        has_allocation=False,
        allocation=None,
    )


def _reference_object(segment: int, sequence: int) -> Any:
    return c_ntfs._MFT_SEGMENT_REFERENCE(_reference(segment, sequence))


def mock_record(
    segment: int,
    sequence: int = 1,
    file_names: list[tuple[str, int, int]] | None = None,
    index: Any | None = None,
    mft: Any | None = None,
) -> Any:
    attributes = {}
    if file_names is not None:
        attributes[ATTRIBUTE_TYPE_CODE.FILE_NAME] = [
            SimpleNamespace(
                attribute=SimpleNamespace(
                    file_name=name,
                    attr=SimpleNamespace(ParentDirectory=_reference_object(parent, parent_sequence)),
                ),
                flags=0,
            )
            for name, parent, parent_sequence in file_names
        ]

    return SimpleNamespace(
        segment=segment,
        header=SimpleNamespace(SequenceNumber=sequence, Flags=1),
        attributes=attributes,
        ntfs=SimpleNamespace(mft=mft),
        index=lambda name: index,
        is_dir=lambda: True,
    )


def mock_mft(records: dict[int, Any], children: list[tuple[Any, Any]]) -> Any:
    return SimpleNamespace(
        get=lambda segment: records[segment],
        children=lambda segment, allocated_only=False: children,
    )


def test_catalog_fixture_sources(ntfs_bin: BinaryIO) -> None:
    fs = NTFS(ntfs_bin)

    root_catalog = fs.mft.root.catalog()
    assert len(root_catalog) == 19
    assert all(entry.source == Source.BOTH for entry in root_catalog)
    assert root_catalog.index_only == []
    assert root_catalog.attribute_only == []

    directory_catalog = fs.mft.get("Directory").catalog()
    assert list(directory_catalog.keys()) == ["File 1.txt", "File 2.txt"]
    assert directory_catalog["File 1.txt"].source == Source.BOTH

    large_catalog = fs.mft.get("Large Directory").catalog()
    assert len(large_catalog) == 2048
    assert large_catalog["Directory 0"].source == Source.BOTH
    assert large_catalog["Directory 0"].allocated is True


def test_catalog_attribute_only(ntfs_bin: BinaryIO) -> None:
    fs = NTFS(ntfs_bin)

    directory = fs.mft.get("Large Directory/Directory 0")
    assert directory.index("$I30")
    assert directory.listdir() == {}

    result = directory.catalog()
    assert [entry.name for entry in result.attribute_only] == ["File 0.txt"]
    assert result["File 0.txt"].source == Source.ATTRIBUTE
    assert result["File 0.txt"].record.segment == 1646
    assert result["File 0.txt"].allocated is False

    assert directory.catalog(allocated_only=True).attribute_only == []


def test_catalog_generation_mismatch() -> None:
    target = mock_record(42, sequence=7, file_names=[("File 1.txt", 5, 5)])
    mft = mock_mft({42: target}, children=[])
    record = mock_record(5, sequence=5, mft=mft)
    record.ntfs.mft = mft

    index = mock_index([_index_entry(_file_name("File 1.txt", 5, 5), 42, 2), _end_entry()])
    record.index = lambda name: index

    result = catalog(record)

    assert "File 1.txt" in result
    assert result["File 1.txt"].source == Source.INDEX
    assert len(result.generation_mismatches) == 1
    mismatch = result.generation_mismatches[0]
    assert mismatch.name == "File 1.txt"
    assert mismatch.reference == "FileReference"
    assert (mismatch.segment, mismatch.expected, mismatch.actual) == (42, 7, 2)


def test_catalog_parent_mismatch() -> None:
    target = mock_record(42, sequence=1, file_names=[("File 1.txt", 5, 5)])
    mft = mock_mft({42: target}, children=[])
    record = mock_record(5, sequence=5, mft=mft)

    index = mock_index([_index_entry(_file_name("File 1.txt", 99, 9), 42, 1), _end_entry()])
    record.index = lambda name: index

    result = catalog(record)

    assert result["File 1.txt"].source == Source.INDEX
    assert len(result.parent_mismatches) == 1
    mismatch = result.parent_mismatches[0]
    assert mismatch.name == "File 1.txt"
    assert (mismatch.segment, mismatch.expected) == (99, 5)


def test_catalog_broken_index_entry_isolated() -> None:
    good_one = mock_record(41, sequence=1, file_names=[("File 1.txt", 5, 5)])
    good_two = mock_record(43, sequence=1, file_names=[("File 3.txt", 5, 5)])
    mft = mock_mft({41: good_one, 43: good_two}, children=[])

    record = mock_record(5, sequence=5, mft=mft)
    index = mock_index(
        [
            _index_entry(_file_name("File 1.txt", 5, 5), 41, 1),
            _index_entry(b"", 42, 1),
            _index_entry(_file_name("File 3.txt", 5, 5), 43, 1),
            _end_entry(),
        ]
    )
    record.index = lambda name: index

    result = catalog(record)

    assert list(result.keys()) == ["File 1.txt", "File 3.txt"]
    assert len(result.broken_index_entries) == 1
    assert isinstance(result.broken_index_entries[0].error, ValueError)


def test_catalog_missing_record() -> None:

    def get(segment: int) -> Any:
        if segment == 41:
            return mock_record(41, sequence=1, file_names=[("File 1.txt", 5, 5)])
        raise BrokenMftError("Invalid MFT record Signature")

    mft = SimpleNamespace(get=get, children=lambda segment, allocated_only=True: [])
    record = mock_record(5, sequence=5, mft=mft)
    index = mock_index(
        [
            _index_entry(_file_name("File 1.txt", 5, 5), 41, 1),
            _index_entry(_file_name("Missing.txt", 5, 5), 42, 1),
            _end_entry(),
        ]
    )
    record.index = lambda name: index

    result = catalog(record)

    assert list(result.keys()) == ["File 1.txt", "Missing.txt"]
    assert result["File 1.txt"].source == Source.INDEX
    assert result["Missing.txt"].source == Source.INDEX
    assert [mismatch.segment for mismatch in result.missing_records] == [42]
    assert result.generation_mismatches == []


def mock_allocated_index(allocation: bytes) -> Any:
    root = SimpleNamespace(
        fh=io.BytesIO(b""),
        attribute_type=ATTRIBUTE_TYPE_CODE.FILE_NAME,
        bytes_per_index_buffer=0x1000,
    )
    index = SimpleNamespace(
        root=root,
        record=None,
        has_allocation=True,
        allocation=io.BytesIO(allocation),
        vcn_size_shift=12,
    )
    index.index_buffer = _broken_index_buffer
    return index


def _broken_index_buffer(vcn: int) -> Any:
    raise BrokenIndexError("Broken INDX header")


def test_catalog_broken_index_buffer_isolated() -> None:
    record = mock_record(5, sequence=5, mft=SimpleNamespace(get=lambda segment: None, children=lambda *a, **kw: []))
    record.index = lambda name: mock_allocated_index(b"\x00" * 0x2000)

    result = catalog(record)

    assert result.entries == {}
    # Zero-filled regions mark the end of the index allocation instead of being reported as broken buffers.
    assert result.broken_index_buffers == []


def test_catalog_broken_index_buffer_reported() -> None:
    record = mock_record(5, sequence=5, mft=SimpleNamespace(get=lambda segment: None, children=lambda *a, **kw: []))
    record.index = lambda name: mock_allocated_index(b"\xff" * 0x3000)

    result = catalog(record)

    assert result.entries == {}
    assert [buffer.vcn for buffer in result.broken_index_buffers] == [0, 1, 2]


def test_catalog_one_sided() -> None:
    index_target = mock_record(42, sequence=1, file_names=[("File 1.txt", 5, 5)])
    attribute_target = mock_record(43, sequence=1, file_names=[("File 2.txt", 5, 5)])
    attribute = attribute_target.attributes[ATTRIBUTE_TYPE_CODE.FILE_NAME][0]
    mft = mock_mft({42: index_target}, children=[(attribute_target, attribute)])

    record = mock_record(5, sequence=5, mft=mft)
    index = mock_index([_index_entry(_file_name("File 1.txt", 5, 5), 42, 1), _end_entry()])
    record.index = lambda name: index

    result = catalog(record)

    assert [entry.name for entry in result.index_only] == ["File 1.txt"]
    assert [entry.name for entry in result.attribute_only] == ["File 2.txt"]
    assert result.sources == {"File 1.txt": Source.INDEX, "File 2.txt": Source.ATTRIBUTE}
