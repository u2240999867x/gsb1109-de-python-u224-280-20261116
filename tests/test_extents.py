from __future__ import annotations

import io
import struct
from types import SimpleNamespace
from typing import BinaryIO

import pytest

from dissect.ntfs.attr import Attribute, AttributeHeader
from dissect.ntfs.c_ntfs import ATTRIBUTE_FLAG_SPARSE, ATTRIBUTE_TYPE_CODE
from dissect.ntfs.exceptions import DataRunError
from dissect.ntfs.util import AttributeCollection, DataExtent

CLUSTER_SIZE = 4096
HEADER_SIZE = 0x48


def encode_varint(value: int) -> bytes:
    buf = struct.pack("<q", value)
    for length in range(1, 9):
        chunk = buf[:length]
        pad = b"\xff" if chunk[-1] & 0x80 else b"\x00"
        if struct.unpack("<q", chunk + pad * (8 - length))[0] == value:
            return chunk
    return buf


def nonresident_attribute(
    runs: list[tuple[int | None, int]],
    lowest_vcn: int,
    highest_vcn: int,
    file_size: int,
    allocated: int,
    flags: int = 0,
) -> Attribute:
    mapping = bytearray()
    previous_lcn = 0
    for lcn, cluster_count in runs:
        size_buf = encode_varint(cluster_count)
        if lcn is None:
            mapping.append(len(size_buf))
            mapping += size_buf
        else:
            offset_buf = encode_varint(lcn - previous_lcn)
            previous_lcn = lcn
            mapping.append((len(offset_buf) << 4) | len(size_buf))
            mapping += size_buf + offset_buf
    mapping.append(0)

    record_length = (HEADER_SIZE + len(mapping) + 7) & ~7
    header = struct.pack(
        "<IIBBHHHQQHB5sQQQQ",
        ATTRIBUTE_TYPE_CODE.DATA,
        record_length,
        1,
        0,
        0,
        flags,
        6,
        lowest_vcn,
        highest_vcn,
        HEADER_SIZE,
        0,
        b"\x00" * 5,
        allocated,
        file_size,
        file_size,
        allocated,
    )
    return Attribute(AttributeHeader.from_bytes((header + bytes(mapping)).ljust(record_length, b"\x00")))


def resident_attribute(data: bytes) -> Attribute:
    value_offset = 0x18
    # cstruct reads the full union, so the record must always be at least as large as the non-resident header
    record_length = max(0x48, (value_offset + len(data) + 7) & ~7)
    header = struct.pack(
        "<IIBBHHHIHBB",
        ATTRIBUTE_TYPE_CODE.DATA,
        record_length,
        0,
        0,
        0,
        0,
        6,
        len(data),
        value_offset,
        0,
        0,
    )
    return Attribute(AttributeHeader.from_bytes((header + data).ljust(record_length, b"\x00")))


def bind_volume(attr: Attribute, volume: BinaryIO) -> Attribute:
    record = SimpleNamespace(ntfs=SimpleNamespace(fh=volume, cluster_size=CLUSTER_SIZE))
    attr.record = record
    attr.header.record = record
    return attr


def test_resident_extent() -> None:
    attr = resident_attribute(b"hello")
    collection = AttributeCollection([attr])

    extents = collection.extents()
    assert len(extents) == 1
    assert extents[0].resident
    assert not extents[0].sparse
    assert collection.open().read() == b"hello"


def test_nonresident_dataruns_carry_vcn_and_lcn() -> None:
    attr = nonresident_attribute([(100, 8), (140, 4)], 0, 11, 12 * CLUSTER_SIZE, 12 * CLUSTER_SIZE)

    assert attr.header.dataruns_vcn() == [(0, 100, 8), (8, 140, 4)]
    # The legacy two-tuple interface keeps returning (lcn, cluster_count) pairs
    assert attr.dataruns() == [(100, 8), (140, 4)]

    extents = AttributeCollection([attr]).extents()
    assert [(e.start_vcn, e.end_vcn, e.lcn, e.cluster_count) for e in extents] == [
        (0, 8, 100, 8),
        (8, 12, 140, 4),
    ]
    assert all(isinstance(extent, DataExtent) for extent in extents)
    assert all(extent.attribute is attr for extent in extents)


def test_nonresident_extent_reads_from_source_volume() -> None:
    volume = io.BytesIO(b"\x00" * (200 * CLUSTER_SIZE))
    volume.seek(100 * CLUSTER_SIZE)
    volume.write(b"A" * (8 * CLUSTER_SIZE))
    volume.seek(140 * CLUSTER_SIZE)
    volume.write(b"B" * (4 * CLUSTER_SIZE))

    attr = bind_volume(
        nonresident_attribute([(100, 8), (140, 4)], 0, 11, 12 * CLUSTER_SIZE, 12 * CLUSTER_SIZE),
        volume,
    )

    data = AttributeCollection([attr]).open().read()
    assert data[: 8 * CLUSTER_SIZE] == b"A" * (8 * CLUSTER_SIZE)
    assert data[8 * CLUSTER_SIZE :] == b"B" * (4 * CLUSTER_SIZE)


def test_explicit_sparse_run_is_zero_filled() -> None:
    volume = io.BytesIO(b"\x00" * (200 * CLUSTER_SIZE))
    volume.seek(140 * CLUSTER_SIZE)
    volume.write(b"B" * (4 * CLUSTER_SIZE))

    attr = bind_volume(
        nonresident_attribute(
            [(None, 4), (140, 4)], 0, 7, 8 * CLUSTER_SIZE, 4 * CLUSTER_SIZE, flags=ATTRIBUTE_FLAG_SPARSE
        ),
        volume,
    )

    extents = AttributeCollection([attr]).extents()
    assert extents[0].sparse
    assert extents[0].lcn is None
    assert not extents[1].sparse
    assert extents[1].lcn == 140

    data = AttributeCollection([attr]).open().read()
    assert data[: 4 * CLUSTER_SIZE] == b"\x00" * (4 * CLUSTER_SIZE)
    assert data[4 * CLUSTER_SIZE :] == b"B" * (4 * CLUSTER_SIZE)


def test_extension_extents_are_stitched_by_vcn() -> None:
    volume = io.BytesIO(b"\x00" * (200 * CLUSTER_SIZE))
    first = bind_volume(nonresident_attribute([(100, 8)], 0, 7, 16 * CLUSTER_SIZE, 16 * CLUSTER_SIZE), volume)
    second = bind_volume(nonresident_attribute([(140, 8)], 8, 15, 16 * CLUSTER_SIZE, 16 * CLUSTER_SIZE), volume)

    # Attributes are deliberately supplied out of order; LowestVcn must drive the ordering
    collection = AttributeCollection([second, first])
    assert [extent.start_vcn for extent in collection.extents()] == [0, 8]
    assert collection.dataruns() == [(100, 8), (140, 8)]


def test_missing_extension_extent_leaves_reported_gap() -> None:
    volume = io.BytesIO()
    first = bind_volume(nonresident_attribute([(100, 8)], 0, 7, 24 * CLUSTER_SIZE, 24 * CLUSTER_SIZE), volume)
    second = bind_volume(nonresident_attribute([(140, 8)], 16, 23, 24 * CLUSTER_SIZE, 24 * CLUSTER_SIZE), volume)

    collection = AttributeCollection([first, second])
    extents = collection.extents()

    gap = collection.gaps()
    assert len(gap) == 1
    assert gap[0].gap
    assert (gap[0].start_vcn, gap[0].end_vcn) == (8, 16)
    # The hole stays between the two backed extents and is never marked sparse
    assert [extent.start_vcn for extent in extents] == [0, 8, 16]
    assert not any(extent.sparse for extent in extents)

    # The unbacked hole cannot be turned into runs or read as zeroes
    with pytest.raises(DataRunError, match=r"unbacked VCN gap 8\.\.15"):
        collection.dataruns()
    with pytest.raises(DataRunError, match=r"unbacked VCN gap 8\.\.15"):
        collection.open()


def test_missing_base_extent_is_reported_gap() -> None:
    late = nonresident_attribute([(140, 4)], 8, 11, 12 * CLUSTER_SIZE, 12 * CLUSTER_SIZE)

    collection = AttributeCollection([late])
    gap = collection.gaps()
    assert len(gap) == 1
    assert gap[0].gap
    assert (gap[0].start_vcn, gap[0].end_vcn) == (0, 8)

    with pytest.raises(DataRunError, match=r"unbacked VCN gap 0\.\.7"):
        collection.dataruns()


def test_overlapping_extents_are_rejected() -> None:
    first = nonresident_attribute([(100, 8)], 0, 7, 12 * CLUSTER_SIZE, 12 * CLUSTER_SIZE)
    second = nonresident_attribute([(140, 4)], 4, 11, 12 * CLUSTER_SIZE, 12 * CLUSTER_SIZE)

    with pytest.raises(DataRunError, match="Overlapping attribute extents at VCN 4"):
        AttributeCollection([first, second]).extents()


def test_highest_vcn_must_match_mapping_pairs() -> None:
    attr = nonresident_attribute([(100, 8)], 0, 99, 8 * CLUSTER_SIZE, 8 * CLUSTER_SIZE)

    with pytest.raises(DataRunError, match="declares HighestVcn 99"):
        AttributeCollection([attr]).extents()


def test_extent_declared_lengths_must_be_consistent() -> None:
    first = nonresident_attribute([(100, 8)], 0, 7, 16 * CLUSTER_SIZE, 16 * CLUSTER_SIZE)
    second = nonresident_attribute([(140, 8)], 8, 15, 12 * CLUSTER_SIZE, 16 * CLUSTER_SIZE)

    with pytest.raises(DataRunError, match="inconsistent file or allocated lengths"):
        AttributeCollection([first, second]).extents()


def test_mapping_pairs_cannot_claim_more_than_allocated() -> None:
    attr = bind_volume(nonresident_attribute([(100, 8)], 0, 7, 4 * CLUSTER_SIZE, 4 * CLUSTER_SIZE), io.BytesIO())

    with pytest.raises(DataRunError, match="mapping pairs claim 8 clusters"):
        AttributeCollection([attr]).extents()


def test_negative_lcn_is_a_bad_pointer_not_a_hole() -> None:
    mapping = bytes([0x11, 0x04, 0xFF, 0x00])  # 4 clusters, signed offset delta of -1 from LCN 0
    record_length = (HEADER_SIZE + len(mapping) + 7) & ~7
    header = struct.pack(
        "<IIBBHHHQQHB5sQQQQ",
        ATTRIBUTE_TYPE_CODE.DATA,
        record_length,
        1,
        0,
        0,
        0,
        6,
        0,
        3,
        HEADER_SIZE,
        0,
        b"\x00" * 5,
        4 * CLUSTER_SIZE,
        4 * CLUSTER_SIZE,
        4 * CLUSTER_SIZE,
        4 * CLUSTER_SIZE,
    )
    attr = Attribute(AttributeHeader.from_bytes((header + mapping).ljust(record_length, b"\x00")))

    with pytest.raises(DataRunError, match="negative cluster offset"):
        attr.header.dataruns_vcn()


def test_truncated_mapping_pairs_are_rejected() -> None:
    # A valid non-resident header whose mapping pair promises one size byte that never arrives.
    # The pair header byte is placed as the final byte of the record, so the next read hits EOF.
    record_length = HEADER_SIZE + 1
    header = struct.pack(
        "<IIBBHHHQQHB5sQQQQ",
        ATTRIBUTE_TYPE_CODE.DATA,
        record_length,
        1,
        0,
        0,
        0,
        6,
        0,
        3,
        HEADER_SIZE,
        0,
        b"\x00" * 5,
        4 * CLUSTER_SIZE,
        4 * CLUSTER_SIZE,
        4 * CLUSTER_SIZE,
        4 * CLUSTER_SIZE,
    )
    truncated = (header + b"\x31").ljust(record_length, b"\x00")
    with pytest.raises(DataRunError, match="Truncated"):
        AttributeHeader.from_bytes(truncated).dataruns_vcn()


def test_zero_cluster_count_is_rejected() -> None:
    mapping = bytes([0x11, 0x00, 0x64, 0x00])  # zero-sized run followed by a real offset
    record_length = (HEADER_SIZE + len(mapping) + 7) & ~7
    header = struct.pack(
        "<IIBBHHHQQHB5sQQQQ",
        ATTRIBUTE_TYPE_CODE.DATA,
        record_length,
        1,
        0,
        0,
        0,
        6,
        0,
        3,
        HEADER_SIZE,
        0,
        b"\x00" * 5,
        4 * CLUSTER_SIZE,
        4 * CLUSTER_SIZE,
        4 * CLUSTER_SIZE,
        4 * CLUSTER_SIZE,
    )
    with pytest.raises(DataRunError, match="Invalid cluster count"):
        Attribute(AttributeHeader.from_bytes((header + mapping).ljust(record_length, b"\x00"))).header.dataruns_vcn()


def test_zero_length_nonresident_attribute_has_no_extents() -> None:
    attr = bind_volume(nonresident_attribute([], 0, 0, 0, 0), io.BytesIO())
    collection = AttributeCollection([attr])

    assert collection.extents() == []
    assert collection.dataruns() == []
    assert collection.open().read() == b""


def test_dataruns_vcn_requires_nonresident() -> None:
    attr = resident_attribute(b"hello")
    with pytest.raises(TypeError):
        attr.header.dataruns_vcn()
