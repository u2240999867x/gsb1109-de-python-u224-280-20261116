from __future__ import annotations

import struct
from typing import BinaryIO

from dissect.ntfs.c_ntfs import (
    ATTRIBUTE_FLAG_SPARSE,
    ATTRIBUTE_TYPE_CODE,
    FILE_RECORD_SEGMENT_IN_USE,
)
from dissect.ntfs.capacity import CapacityReport, IntervalSet
from dissect.ntfs.mft import MftRecord
from dissect.ntfs.ntfs import NTFS


def _varint(value: int) -> bytes:
    for length in range(1, 9):
        try:
            buf = value.to_bytes(length, "little", signed=True)
        except OverflowError:
            continue
        if int.from_bytes(buf, "little", signed=True) == value:
            return buf
    raise ValueError(value)


def _dataruns(runs: list[tuple[int | None, int]]) -> bytes:
    result = bytearray()
    previous_lcn = 0
    for lcn, length in runs:
        size_bytes = _varint(length)
        if lcn is None:
            # Sparse run: no offset part
            result.append(len(size_bytes))
            result += size_bytes
        else:
            offset_bytes = _varint(lcn - previous_lcn)
            previous_lcn = lcn
            result.append((len(offset_bytes) << 4) | len(size_bytes))
            result += size_bytes
            result += offset_bytes
    result.append(0)
    return bytes(result)


def _data_attribute(
    name: str = "",
    runs: list[tuple[int | None, int]] = (),
    flags: int = 0,
    lowest_vcn: int = 0,
    highest_vcn: int = 0,
    compression_unit: int = 0,
    allocated: int = 0,
    size: int = 0,
    valid: int = 0,
    instance: int = 0,
) -> bytes:
    name_bytes = name.encode("utf-16-le")
    name_offset = 0x48 if name else 0
    mapping_offset = 0x48 + len(name_bytes)
    run_bytes = _dataruns(runs)
    record_length = (mapping_offset + len(run_bytes) + 7) & ~7

    header = struct.pack(
        "<IIBBHHH", ATTRIBUTE_TYPE_CODE.DATA, record_length, 1, len(name), name_offset, flags, instance
    )
    nonresident = struct.pack(
        "<QQHB5sqqqq",
        lowest_vcn,
        highest_vcn,
        mapping_offset,
        compression_unit,
        b"\x00" * 5,
        allocated,
        size,
        valid,
        allocated,
    )
    return (header + nonresident + name_bytes + run_bytes).ljust(record_length, b"\x00")


def _mft_record(
    segment: int, attributes: list[bytes], flags: int = FILE_RECORD_SEGMENT_IN_USE, base: int = 0
) -> MftRecord:
    record_size = 1024
    data = bytearray(record_size)
    data[0:4] = b"FILE"
    struct.pack_into("<H", data, 4, 0x30)  # UpdateSequenceArrayOffset
    struct.pack_into("<H", data, 6, 3)  # UpdateSequenceArraySize
    data[0x30:0x36] = b"\xaa\xbb\x00\x00\x00\x00"  # Update sequence array
    data[510:512] = b"\xaa\xbb"
    data[1022:1024] = b"\xaa\xbb"
    struct.pack_into("<QHHHHII", data, 8, 0, 1, 1, 0x38, flags, 0, record_size)
    struct.pack_into("<Q", data, 32, base)  # BaseFileRecordSegment

    offset = 0x38
    for attr in attributes:
        data[offset : offset + len(attr)] = attr
        offset += len(attr)
    struct.pack_into("<I", data, offset, ATTRIBUTE_TYPE_CODE.END)
    struct.pack_into("<I", data, 24, offset + 8)  # BytesInUse

    record = MftRecord.from_bytes(bytes(data))
    record.segment = segment
    return record


def _bitmap(allocated: set[int], total_clusters: int) -> bytes:
    data = bytearray((total_clusters + 7) // 8)
    for cluster in allocated:
        data[cluster // 8] |= 1 << (cluster % 8)
    return bytes(data)


def test_interval_set() -> None:
    ranges = IntervalSet([(10, 12), (20, 25)])
    ranges.add(12, 15)
    ranges.add(0, 10)
    ranges.add(14, 20)
    assert list(ranges) == [(0, 25)]
    assert ranges.total() == 25
    assert 0 in ranges
    assert 24 in ranges
    assert 25 not in ranges

    other = IntervalSet([(5, 10), (30, 40)])
    assert list(ranges.intersection(other)) == [(5, 10)]
    assert list(ranges.difference(other)) == [(0, 5), (10, 25)]


def test_capacity_report_synthetic() -> None:
    report = CapacityReport(cluster_size=4096, total_clusters=512)

    # Regular stream: 4 physical clusters at LCN 100, sparse tail of 4 clusters
    report.add_record(
        _mft_record(
            36,
            [
                _data_attribute(
                    runs=[(100, 4), (None, 4)],
                    flags=ATTRIBUTE_FLAG_SPARSE,
                    highest_vcn=7,
                    allocated=0x4000,
                    size=0x5000,
                    valid=0x1234,
                    instance=1,
                )
            ],
        )
    )
    # Named stream (ADS) on another record, claiming LCN 102-105 (overlapping LCN 102-103)
    report.add_record(
        _mft_record(
            37,
            [_data_attribute(name="ads", runs=[(102, 4)], highest_vcn=3, allocated=0x4000, size=0x4000, valid=0x4000)],
        )
    )
    # Compressed stream: a 16 cluster compression unit with only 2 physical clusters
    report.add_record(
        _mft_record(
            38,
            [
                _data_attribute(
                    runs=[(200, 2), (None, 14)],
                    flags=0x0001,
                    highest_vcn=15,
                    compression_unit=4,
                    allocated=0x2000,
                    size=0x10000,
                    valid=0x10000,
                )
            ],
        )
    )
    # Deleted record: must not claim any clusters
    report.add_record(_mft_record(39, [_data_attribute(runs=[(400, 4)], highest_vcn=3)], flags=0))
    # Extension record: accounted for through its base record, must not claim clusters itself
    report.add_record(_mft_record(40, [_data_attribute(runs=[(404, 4)], highest_vcn=3)], base=36))
    # Broken record (attribute RecordLength 0): incomplete, ownership of its clusters unknown
    report.add_record(_mft_record(41, [struct.pack("<II", ATTRIBUTE_TYPE_CODE.DATA, 0).ljust(72, b"\x00")]))

    report.set_bitmap(_bitmap({100, 101, 103, 200, 201, 300}, 512))
    report.finalize()

    assert report.deleted_segments == 1
    assert report.extension_segments == 1
    assert report.incomplete_segments == [41]
    assert not report.complete

    streams = {(stream.segment, stream.name): stream for stream in report.streams}
    stream_a = streams[(36, "")]
    assert stream_a.logical_size == 0x5000
    assert stream_a.valid_data_length == 0x1234
    assert stream_a.allocated_size == 0x4000
    assert stream_a.physical_clusters == 4
    assert stream_a.sparse_clusters == 4
    assert stream_a.sparse

    # The ADS is accounted for independently
    stream_b = streams[(37, "ads")]
    assert stream_b.logical_size == 0x4000
    assert stream_b.physical_clusters == 4

    # Compression and sparse tails don't inflate physical clusters to the logical size
    stream_c = streams[(38, "")]
    assert stream_c.compressed
    assert stream_c.compression_unit == 4
    assert stream_c.logical_size == 0x10000
    assert stream_c.physical_clusters == 2
    assert stream_c.sparse_clusters == 14

    # The conflicted clusters are counted once on disk, but both claimants are kept
    assert len(report.conflicts) == 1
    conflict = report.conflicts[0]
    assert conflict.lcn == 102
    assert conflict.length == 2
    assert {claim.segment for claim in conflict.claims} == {36, 37}
    assert report.physical_union.total() == 4 + 4 + 2 - 2
    assert sum(stream.physical_clusters for stream in report.streams) == 4 + 4 + 2

    # Bitmap comparison
    assert list(report.claimed_not_allocated) == [(102, 103), (104, 106)]
    assert list(report.allocated_not_claimed) == [(300, 301)]
    assert report.free.total() == 512 - 6

    # A conflicting range traces back to the original attributes and the bitmap position
    result = report.lookup(102)
    assert result.conflict
    assert {claim.segment for claim in result.claims} == {36, 37}
    claim_a = next(claim for claim in result.claims if claim.segment == 36)
    assert (claim_a.instance, claim_a.vcn, claim_a.lcn, claim_a.length) == (1, 0, 100, 4)
    assert claim_a.lcn <= 102 < claim_a.end
    assert result.bitmap[0].lcn == 102
    assert not result.bitmap[0].allocated
    assert (result.bitmap[0].byte_offset, result.bitmap[0].bit_offset) == (12, 6)

    # An allocated-but-unclaimed cluster has no owner but is set in the bitmap
    result = report.lookup(300)
    assert not result.claims
    assert result.bitmap[0].allocated


def test_capacity_report_no_bitmap() -> None:
    report = CapacityReport(cluster_size=4096, total_clusters=512)
    report.add_record(_mft_record(36, [_data_attribute(runs=[(100, 4)], highest_vcn=3, size=0x4000)]))
    report.finalize()

    assert report.bitmap_allocated is None
    assert report.free is None
    assert not report.complete
    assert report.summary()["free_clusters"] is None


def test_capacity_report_ntfs(ntfs_bin: BinaryIO) -> None:
    fs = NTFS(ntfs_bin)
    report = CapacityReport.from_ntfs(fs)

    assert report.complete
    assert report.total_clusters == 1791
    assert not report.conflicts
    assert not report.claimed_not_allocated
    assert not report.allocated_not_claimed
    assert report.bitmap_allocated.total() == 1755
    assert report.physical_union.total() == 1755
    assert report.free.total() == 1791 - 1755

    # Large.txt is compressed: its physical footprint is (much) smaller than its logical size
    stream = next(stream for stream in report.streams if stream.segment == 44 and stream.name == "")
    assert stream.logical_size == 2097152
    assert stream.compressed
    assert 0 < stream.physical_clusters < 2097152 // report.cluster_size

    # The unnamed $Bad stream of $BadClus is fully sparse: logical content, no physical clusters
    bad = next(stream for stream in report.streams if stream.segment == 8 and stream.name == "$Bad")
    assert bad.logical_size == 1791 * 4096
    assert bad.physical_clusters == 0
    assert bad.sparse_clusters == 1791

    # Looking up a claimed cluster traces back to the attribute and the bitmap position
    claim = stream.claims[0]
    result = report.lookup(claim.lcn)
    assert result.claims == [claim]
    assert not result.conflict
    assert result.bitmap[0].allocated
    assert (result.bitmap[0].byte_offset, result.bitmap[0].bit_offset) == divmod(claim.lcn, 8)

    summary = report.summary()
    assert summary["free_clusters"] == 36
    assert summary["claimed_not_allocated_clusters"] == 0
    assert summary["allocated_not_claimed_clusters"] == 0
    assert "Conflicting claims: none" in report.render()


def test_capacity_report_mft_only(mft_bin: BinaryIO) -> None:
    # A stand-alone $MFT has no volume: no $Bitmap, so no exact free space can be derived
    fs = NTFS(mft=mft_bin)
    report = CapacityReport.from_ntfs(fs)

    assert report.bitmap is None
    assert report.total_clusters is None
    assert report.free is None
    assert not report.complete
    assert report.claims
