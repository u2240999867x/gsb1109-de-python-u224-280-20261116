"""Tests for the independent MCAP acceptance auditor in ``mcap.audit``.

The auditor rebuilds its ledger from the bytes on disk and treats Statistics and
ChunkIndex records purely as claims, so these tests corrupt or rewrite those
claims and check that the verdicts distinguish confirmed, contradicted and
indeterminate evidence.
"""

import io
import os
import struct
from pathlib import Path
from typing import Callable, List, Tuple

import pytest

from mcap.audit import (
    AuditError,
    VERDICT_CONFIRMED,
    VERDICT_CONTRADICTED,
    VERDICT_INDETERMINATE,
    VERDICT_UNVERIFIABLE,
    AuditReport,
    audit_mcap,
)
from mcap.data_stream import RecordBuilder
from mcap.opcode import Opcode
from mcap.records import (
    Chunk,
    ChunkIndex,
    MessageIndex,
    Statistics,
)
from mcap.stream_reader import StreamReader
from mcap.writer import (
    MCAP0_MAGIC,
    CompressionType,
    IndexType,
    Writer,
)

DEMO_MCAP = (
    Path(__file__).parent.parent.parent.parent / "testdata" / "mcap" / "demo.mcap"
)
CONFORMANCE_DIR = (
    Path(__file__).parent.parent.parent.parent
    / "tests"
    / "conformance"
    / "data"
)


def write_mcap(
    *,
    chunk_size: int = 60,
    n: int = 4,
    compression: CompressionType = CompressionType.NONE,
    index_types: IndexType = IndexType.ALL,
    use_chunking: bool = True,
    use_statistics: bool = True,
) -> bytearray:
    buffer = io.BytesIO()
    writer = Writer(
        buffer,
        chunk_size=chunk_size,
        compression=compression,
        index_types=index_types,
        use_chunking=use_chunking,
        use_statistics=use_statistics,
    )
    writer.start()
    channel_a = writer.register_channel("/a", "json", 0)
    channel_b = writer.register_channel("/b", "json", 0)
    for index in range(n):
        writer.add_message(
            channel_a, log_time=100 + index, data=b"aaaa", publish_time=100 + index
        )
        writer.add_message(
            channel_b, log_time=200 + index, data=b"bbbb", publish_time=200 + index
        )
    writer.finish()
    return bytearray(buffer.getvalue())


def rewrite(raw: bytes, mutator: Callable) -> bytearray:
    """Re-serialize an MCAP, letting the mutator alter parsed records first."""
    builder = RecordBuilder()
    reader = StreamReader(io.BytesIO(bytes(raw)), emit_chunks=True)
    builder.write(MCAP0_MAGIC)
    for record in reader.records:
        mutator(record)
        record.write(builder)
    builder.write(MCAP0_MAGIC)
    return builder.end()


def record_offsets(raw: bytes, opcode: Opcode) -> List[Tuple[int, int]]:
    stream = io.BytesIO(bytes(raw))
    stream.seek(8)
    matches: List[Tuple[int, int]] = []
    while True:
        offset = stream.tell()
        prefix = stream.read(9)
        if len(prefix) < 9:
            break
        record_opcode, length = struct.unpack_from("<BQ", prefix, 0)
        if record_opcode == int(opcode):
            matches.append((offset, length))
        stream.seek(offset + 9 + length)
    return matches


def finding_codes(report: AuditReport) -> List[str]:
    return [finding.code for finding in report.findings]


# --------------------------------------------------------------------- clean


def test_demo_mcap_audits_clean():
    report = audit_mcap(str(DEMO_MCAP))
    assert report.is_clean()
    assert report.readable
    assert report.observed_message_count == 3
    assert report.observed_channel_counts == {0: 2, 1: 1}
    assert report.observed_min_log_time == 42
    assert report.observed_max_log_time == 43
    assert report.proven_message_count_lower_bound == 3
    assert report.proven_max_log_time_bound == 43
    assert all(
        verdict.verdict == VERDICT_CONFIRMED
        for verdict in report.statistics_verdicts
    )
    assert all(
        chunk.readable and chunk.index_verdict == VERDICT_CONFIRMED
        for chunk in report.chunks
    )
    assert all(
        audit.verdict == VERDICT_CONFIRMED
        for audits in report.message_index_audits.values()
        for audit in audits
    )


@pytest.mark.parametrize(
    "compression",
    [CompressionType.NONE, CompressionType.LZ4, CompressionType.ZSTD],
)
def test_clean_ledger_matches_written_messages(compression):
    raw = write_mcap(compression=compression)
    report = audit_mcap(io.BytesIO(raw))
    assert report.is_clean()
    assert report.observed_message_count == 8
    assert report.observed_channel_counts == {1: 4, 2: 4}
    assert report.observed_min_log_time == 100
    assert report.observed_max_log_time == 203
    for chunk in report.chunks:
        assert chunk.observed_message_count == sum(
            chunk.observed_channel_counts.values()
        )
        assert chunk.observed_min_log_time <= chunk.observed_max_log_time


def test_conformance_files_audit_clean():
    for name in ("OneMessage", "TenMessages", "OneSchemalessMessage"):
        path = CONFORMANCE_DIR / name / f"{name}.mcap"
        report = audit_mcap(str(path))
        assert report.is_clean(), (name, [str(f) for f in report.findings])


def test_every_conformance_variant_audits_without_contradictions():
    # The corpus includes padding, compression and every index omission/
    # repetition combination; none of them should produce an error finding.
    paths = sorted(CONFORMANCE_DIR.glob("*/*.mcap"))
    assert len(paths) > 100
    for path in paths:
        report = audit_mcap(str(path))
        assert not report.errors(), (path.name, [str(f) for f in report.errors()])


def test_unchunked_file_without_summary_is_fully_verified():
    raw = write_mcap(
        chunk_size=10**9,
        n=5,
        use_chunking=False,
        index_types=IndexType.NONE,
        use_statistics=False,
    )
    report = audit_mcap(io.BytesIO(raw))
    assert report.is_clean()
    assert report.observed_message_count == 10
    assert not report.has_statistics
    assert report.statistics_verdicts == []
    assert report.chunks == []


def test_chunked_file_without_indexes_is_clean_but_unverifiable():
    raw = write_mcap(
        chunk_size=40,
        n=3,
        index_types=IndexType.NONE,
        use_statistics=False,
    )
    report = audit_mcap(io.BytesIO(raw))
    # Omitting optional indexes is not a contradiction: no error findings.
    assert report.is_clean()
    assert not report.errors()
    assert len(report.chunks) > 1
    assert all(
        chunk.index_verdict == VERDICT_UNVERIFIABLE for chunk in report.chunks
    )
    assert any(f.code == "missing_chunk_index" for f in report.warnings())
    # The ledger itself is still exact without any index.
    assert report.observed_message_count == 6


# ------------------------------------------------------------- unreadable


def test_one_unreadable_chunk_keeps_other_chunks_exact():
    raw = write_mcap(compression=CompressionType.ZSTD)
    baseline = audit_mcap(io.BytesIO(raw))
    assert len(baseline.chunks) > 2

    seen = {"chunks": 0}

    def corrupt(record):
        if isinstance(record, Chunk):
            if seen["chunks"] == 1:
                record.data = bytes([record.data[0] ^ 0x5A]) + record.data[1:]
            seen["chunks"] += 1

    report = audit_mcap(io.BytesIO(rewrite(raw, corrupt)))
    assert not report.readable
    assert not report.is_clean()

    unreadable = [chunk for chunk in report.chunks if not chunk.readable]
    readable = [chunk for chunk in report.chunks if chunk.readable]
    assert len(unreadable) == 1
    assert unreadable[0].ordinal == 1
    assert unreadable[0].error is not None
    # The intact chunks keep exact, independently proven numbers.
    assert all(chunk.index_verdict == VERDICT_CONFIRMED for chunk in readable)
    assert sum(chunk.observed_message_count for chunk in readable) == 6
    # The corrupt chunk contributes no fabricated message rows.
    assert unreadable[0].observed_message_count == 0
    assert unreadable[0].observed_channel_counts == {}

    # Whole-file totals survive as proven bounds.
    assert report.proven_message_count_lower_bound == 6
    assert report.proven_min_log_time_bound == 100
    assert report.proven_max_log_time_bound is None

    # The declaration can only constrain, never confirm, while bytes are missing.
    count_verdict = next(
        v for v in report.statistics_verdicts if v.name == "message_count"
    )
    assert count_verdict.verdict == VERDICT_INDETERMINATE
    assert count_verdict.observed == 6 and count_verdict.declared == 8
    assert "unreadable_chunk" in finding_codes(report)


def test_truncated_chunk_record_is_unreadable_but_earlier_chunks_stand():
    raw = write_mcap()
    offset, length = record_offsets(raw, Opcode.CHUNK)[1]
    cut = offset + 9 + length // 2
    report = audit_mcap(io.BytesIO(bytes(raw[:cut])))

    assert not report.readable
    assert len(report.chunks) == 2
    assert report.chunks[0].readable
    assert not report.chunks[1].readable
    assert report.chunks[1].index_verdict == VERDICT_UNVERIFIABLE
    assert report.proven_message_count_lower_bound >= 1
    assert report.proven_max_log_time_bound is None
    assert "unreadable_chunk" in finding_codes(report)


def test_crc_failure_is_lazy_controllable():
    def break_crc(record):
        if isinstance(record, Chunk):
            record.uncompressed_crc += 1

    raw = rewrite(write_mcap(), break_crc)
    strict = audit_mcap(io.BytesIO(raw), validate_crcs=True)
    lax = audit_mcap(io.BytesIO(raw), validate_crcs=False)

    assert not strict.readable
    assert any(
        "crc mismatch" in (chunk.error or "") for chunk in strict.chunks
    )
    assert lax.readable
    assert lax.observed_message_count == 8
    assert lax.is_clean()


# ---------------------------------------------------------- contradictions


def test_falsified_statistics_are_contradicted_not_trusted():
    def lie(record):
        if isinstance(record, Statistics):
            record.message_count += 5
            record.message_end_time = 999
            record.channel_message_counts[1] = 99

    report = audit_mcap(io.BytesIO(rewrite(write_mcap(), lie)))
    by_name = {v.name: v for v in report.statistics_verdicts}
    assert by_name["message_count"].verdict == VERDICT_CONTRADICTED
    assert by_name["message_end_time"].verdict == VERDICT_CONTRADICTED
    assert by_name["message_start_time"].verdict == VERDICT_CONFIRMED

    channel_verdict = next(
        v for v in report.channel_verdicts if v.channel_id == 1
    )
    assert channel_verdict.verdict == VERDICT_CONTRADICTED
    assert channel_verdict.observed == 4 and channel_verdict.declared == 99
    codes = finding_codes(report)
    assert "bad_message_count" in codes
    assert "bad_message_end_time" in codes
    assert "bad_channel_message_count" in codes
    assert "channel_counts_sum_mismatch" in codes
    # Every contradiction points back at the statistics record range.
    statistics_range = report.statistics_byte_range
    assert statistics_range is not None
    assert all(
        finding.byte_range == statistics_range
        for finding in report.findings
        if finding.code
        in {"bad_message_count", "bad_message_end_time", "channel_counts_sum_mismatch"}
    )


def test_falsified_chunk_index_times_are_attributed_to_that_index():
    def lie(record):
        if isinstance(record, ChunkIndex):
            record.message_end_time = 7777

    report = audit_mcap(io.BytesIO(rewrite(write_mcap(), lie)))
    bad = [f for f in report.findings if f.code == "bad_chunk_end_time"]
    assert len(bad) == len(report.chunks)
    # The finding range is the chunk index record, not the chunk itself.
    for finding, chunk in zip(bad, report.chunks):
        assert finding.byte_range == chunk.index_byte_range
        assert finding.chunk_index == chunk.ordinal
    assert all(chunk.index_verdict == VERDICT_CONTRADICTED for chunk in report.chunks)


def test_falsified_chunk_size_and_compression_claims_are_contradicted():
    def lie(record):
        if isinstance(record, ChunkIndex):
            record.chunk_length += 1
            record.compressed_size += 1
            record.uncompressed_size += 1
            record.compression = "lz4"

    report = audit_mcap(io.BytesIO(rewrite(write_mcap(), lie)))
    codes = {finding.code for finding in report.findings}
    assert {
        "bad_chunk_length",
        "bad_compressed_size",
        "bad_uncompressed_size",
        "bad_compression",
    } <= codes


def test_message_index_timestamps_must_match_recorded_messages():
    def lie(record):
        if isinstance(record, MessageIndex):
            record.records = [
                (timestamp + 10_000, offset)
                for timestamp, offset in record.records
            ]

    report = audit_mcap(io.BytesIO(rewrite(write_mcap(), lie)))
    audits = [
        audit
        for audits in report.message_index_audits.values()
        for audit in audits
    ]
    assert audits
    assert all(audit.verdict == VERDICT_CONTRADICTED for audit in audits)


def test_message_index_entry_count_mismatch_is_contradicted():
    def lie(record):
        if isinstance(record, MessageIndex) and record.records:
            record.records = record.records[:-1]

    report = audit_mcap(io.BytesIO(rewrite(write_mcap(), lie)))
    audits = [
        audit
        for audits in report.message_index_audits.values()
        for audit in audits
    ]
    assert any(audit.verdict == VERDICT_CONTRADICTED for audit in audits)
    bad = [
        audit
        for audit in audits
        if audit.verdict == VERDICT_CONTRADICTED
        and audit.declared_entry_count != audit.observed_message_count
    ]
    assert bad


def test_unknown_channel_in_statistics_is_flagged():
    def lie(record):
        if isinstance(record, Statistics):
            record.channel_message_counts[42] = 3

    report = audit_mcap(io.BytesIO(rewrite(write_mcap(), lie)))
    unknown = [f for f in report.findings if f.code == "unknown_channel"]
    assert len(unknown) == 1 and unknown[0].channel_id == 42
    assert "channel_counts_sum_mismatch" in finding_codes(report)


def test_dangling_and_duplicate_chunk_indexes_are_structural_errors():
    def dangle(record):
        if isinstance(record, ChunkIndex):
            record.chunk_start_offset = 999_999

    report = audit_mcap(io.BytesIO(rewrite(write_mcap(), dangle)))
    assert "dangling_chunk_index" in finding_codes(report)
    assert all(
        chunk.index_verdict == VERDICT_UNVERIFIABLE for chunk in report.chunks
    )

    duplicated = RecordBuilder()
    reader = StreamReader(
        io.BytesIO(bytes(write_mcap())), emit_chunks=True
    )
    duplicated.write(MCAP0_MAGIC)
    emitted = False
    for record in reader.records:
        record.write(duplicated)
        if isinstance(record, ChunkIndex) and not emitted:
            record.write(duplicated)
            emitted = True
    duplicated.write(MCAP0_MAGIC)
    report = audit_mcap(io.BytesIO(duplicated.end()))
    assert "duplicate_chunk_index" in finding_codes(report)


def test_duplicate_channel_id_in_chunk_index_wire_map_is_detected():
    raw = write_mcap()
    offset, length = record_offsets(raw, Opcode.CHUNK_INDEX)[0]
    body_start = offset + 9
    map_length = struct.unpack_from("<I", raw, body_start + 32)[0]
    entry = bytes(raw[body_start + 36 : body_start + 36 + 10])
    new_map = entry + entry
    head = (
        bytearray(raw[body_start : body_start + 32])
        + struct.pack("<I", len(new_map))
        + new_map
    )
    tail = raw[body_start + 36 + map_length : offset + 9 + length]
    new_record = (
        bytes([int(Opcode.CHUNK_INDEX)])
        + struct.pack("<Q", len(head) + len(tail))
        + bytes(head)
        + bytes(tail)
    )
    patched = raw[:offset] + new_record + raw[offset + 9 + length :]

    report = audit_mcap(io.BytesIO(patched))
    assert "duplicate_message_index_mapping" in finding_codes(report)
    duplicate = next(
        f
        for f in report.findings
        if f.code == "duplicate_message_index_mapping"
    )
    assert duplicate.byte_range is not None
    assert duplicate.byte_range.start >= offset


# ------------------------------------------------------------- constraints


def test_declaration_inside_proven_bounds_is_indeterminate_when_bytes_missing():
    raw = write_mcap(compression=CompressionType.ZSTD)
    seen = {"chunks": 0}

    def corrupt(record):
        if isinstance(record, Chunk):
            if seen["chunks"] == 1:
                record.data = bytes([record.data[0] ^ 0x5A]) + record.data[1:]
            seen["chunks"] += 1

    report = audit_mcap(io.BytesIO(rewrite(raw, corrupt)))
    # The declared count (8) stays above the proven 6, so it is not contradicted.
    verdicts = {v.name: v for v in report.statistics_verdicts}
    assert verdicts["message_count"].verdict == VERDICT_INDETERMINATE
    assert all(
        verdict.verdict != VERDICT_CONTRADICTED
        for verdict in report.channel_verdicts
    )


def test_impossible_declaration_is_contradicted_even_with_missing_bytes():
    raw = write_mcap(compression=CompressionType.ZSTD)
    seen = {"chunks": 0}

    def corrupt(record):
        if isinstance(record, Chunk):
            if seen["chunks"] == 1:
                record.data = bytes([record.data[0] ^ 0x5A]) + record.data[1:]
            seen["chunks"] += 1

    def undercount(record):
        if isinstance(record, Statistics):
            record.message_count = 1  # below the 6 provably present messages
            record.channel_message_counts = {1: 0, 2: 1}

    def combined(record):
        corrupt(record)
        undercount(record)

    report = audit_mcap(io.BytesIO(rewrite(raw, combined)))
    verdicts = {v.name: v for v in report.statistics_verdicts}
    assert verdicts["message_count"].verdict == VERDICT_CONTRADICTED
    assert any(
        verdict.verdict == VERDICT_CONTRADICTED
        for verdict in report.channel_verdicts
    )


# ------------------------------------------------------------ input errors


def test_bad_magic_raises_audit_error():
    with pytest.raises(AuditError, match="magic"):
        audit_mcap(io.BytesIO(b"definitely not an mcap"))


def test_non_seekable_stream_raises_audit_error():
    read_end, write_end = os.pipe()
    reader = os.fdopen(read_end, "rb")
    try:
        with pytest.raises(AuditError, match="seekable"):
            audit_mcap(reader)
    finally:
        reader.close()
        os.close(write_end)


def test_findings_always_carry_locations_when_a_range_exists():
    report = audit_mcap(str(DEMO_MCAP))
    assert report.findings == []  # clean file has nothing to report

    def lie(record):
        if isinstance(record, Statistics):
            record.message_count += 1

    dirty = audit_mcap(io.BytesIO(rewrite(write_mcap(), lie)))
    located = [
        finding
        for finding in dirty.findings
        if finding.code in {"bad_message_count", "channel_counts_sum_mismatch"}
    ]
    assert located
    assert all(finding.byte_range is not None for finding in located)


def test_orphan_message_index_is_flagged_at_its_own_range():
    builder = RecordBuilder()
    reader = StreamReader(io.BytesIO(bytes(write_mcap())), emit_chunks=True)
    builder.write(MCAP0_MAGIC)
    extra_written = False
    for record in reader.records:
        record.write(builder)
        if isinstance(record, MessageIndex) and not extra_written:
            MessageIndex(
                channel_id=record.channel_id,
                records=record.records + [(999, 7)],
            ).write(builder)
            extra_written = True
    builder.write(MCAP0_MAGIC)

    report = audit_mcap(io.BytesIO(builder.end()))
    orphans = [f for f in report.findings if f.code == "orphan_message_index"]
    assert orphans
    assert all(finding.byte_range is not None for finding in orphans)


def test_message_index_offset_reused_across_chunks_is_flagged():
    raw = write_mcap()
    parsed = list(StreamReader(io.BytesIO(bytes(raw)), emit_chunks=True).records)
    shared_offset = next(
        iter(next(r for r in parsed if isinstance(r, ChunkIndex)).message_index_offsets.values())
    )

    builder = RecordBuilder()
    reader = StreamReader(io.BytesIO(bytes(raw)), emit_chunks=True)
    builder.write(MCAP0_MAGIC)
    chunk_indexes_seen = 0
    for record in reader.records:
        if isinstance(record, ChunkIndex):
            if chunk_indexes_seen == 1:
                record.message_index_offsets = {1: shared_offset}
            chunk_indexes_seen += 1
        record.write(builder)
    builder.write(MCAP0_MAGIC)

    report = audit_mcap(io.BytesIO(builder.end()))
    reused = [f for f in report.findings if f.code == "duplicate_message_index_offset"]
    assert reused
    assert reused[0].byte_range is not None


def test_channel_findings_point_at_the_exact_statistics_mapping_entry():
    def lie(record):
        if isinstance(record, Statistics):
            record.channel_message_counts[1] = 99

    report = audit_mcap(io.BytesIO(rewrite(write_mcap(), lie)))
    bad = next(
        finding
        for finding in report.findings
        if finding.code == "bad_channel_message_count" and finding.channel_id == 1
    )
    assert bad.byte_range is not None
    # A channel mapping entry is a uint16 id plus uint64 count: exactly 10 bytes.
    assert bad.byte_range.end - bad.byte_range.start == 10


def test_unknown_channel_finding_points_back_at_its_first_message():
    # In an unchunked file with no summary repeats, the Channel record is a
    # top-level record that can be removed while the messages remain.
    raw = _write_unchunked_no_repeats()
    builder = RecordBuilder()
    reader = StreamReader(io.BytesIO(bytes(raw)), emit_chunks=True)
    builder.write(MCAP0_MAGIC)
    for record in reader.records:
        if type(record).__name__ == "Channel" and record.id == 1:
            continue
        record.write(builder)
    builder.write(MCAP0_MAGIC)
    dirty = audit_mcap(io.BytesIO(builder.end()))
    unknown = [f for f in dirty.findings if f.code == "unknown_channel"]
    assert unknown
    assert all(finding.byte_range is not None for finding in unknown)


def _write_unchunked_no_repeats() -> bytearray:
    buffer = io.BytesIO()
    writer = Writer(
        buffer,
        chunk_size=10**9,
        compression=CompressionType.NONE,
        index_types=IndexType.NONE,
        use_chunking=False,
        use_statistics=False,
        use_summary_offsets=False,
        repeat_channels=False,
        repeat_schemas=False,
    )
    writer.start()
    channel_a = writer.register_channel("/a", "json", 0)
    channel_b = writer.register_channel("/b", "json", 0)
    for index in range(2):
        writer.add_message(
            channel_a, log_time=100 + index, data=b"aaaa", publish_time=100 + index
        )
        writer.add_message(
            channel_b, log_time=200 + index, data=b"bbbb", publish_time=200 + index
        )
    writer.finish()
    return bytearray(buffer.getvalue())
