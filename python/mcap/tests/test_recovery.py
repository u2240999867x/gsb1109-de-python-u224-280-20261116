import struct
from io import BytesIO
from typing import Dict, List, Tuple

import pytest

from mcap.exceptions import McapError
from mcap.reader import make_reader
from mcap.records import DataEnd, Message, MessageIndex, Schema, Channel, Statistics
from mcap.recovery import ConfirmedMessage, RecoveryError, recover
from mcap.stream_reader import StreamReader, breakup_chunk
from mcap.writer import CompressionType, Writer

SCHEMA_DATA = b'{"type":"object","properties":{"n":{"type":"integer"}}}'


def build_file(
    messages: List[Tuple[int, int, bytes]],
    *,
    chunk_size: int = 1024,
    use_chunking: bool = True,
    enable_data_crcs: bool = False,
) -> bytes:
    buffer = BytesIO()
    writer = Writer(
        buffer,
        chunk_size=chunk_size,
        compression=CompressionType.NONE,
        use_chunking=use_chunking,
        enable_data_crcs=enable_data_crcs,
    )
    writer.start(library="recovery-test")
    schema_id = writer.register_schema(
        name="schema", encoding="jsonschema", data=SCHEMA_DATA
    )
    writer.register_channel(topic="/a", message_encoding="json", schema_id=schema_id)
    writer.register_channel(topic="/b", message_encoding="json", schema_id=schema_id)
    for channel_id, log_time, data in messages:
        writer.add_message(
            channel_id=channel_id,
            log_time=log_time,
            data=data,
            publish_time=log_time + 1,
            sequence=log_time,
        )
    writer.finish()
    return buffer.getvalue()


def make_receipt(messages: List[Tuple[int, int, bytes]]) -> List[ConfirmedMessage]:
    return [
        ConfirmedMessage(
            channel_id=channel_id,
            log_time=log_time,
            publish_time=log_time + 1,
            sequence=log_time,
            data=data,
        )
        for channel_id, log_time, data in messages
    ]


def sample_messages(count: int = 40) -> List[Tuple[int, int, bytes]]:
    return [
        (1 + index % 2, index * 10, f'{{"n":{index}}}'.encode())
        for index in range(count)
    ]


def record_offsets(data: bytes) -> List[Tuple[int, int, int]]:
    """(offset, opcode, length) of every complete top-level record."""
    records = []
    pos = 8  # leading magic
    while pos + 9 <= len(data):
        opcode, length = struct.unpack_from("<BQ", data, pos)
        if pos + 9 + length > len(data):
            break
        records.append((pos, opcode, length))
        pos += 9 + length
    return records


def record_starts(data: bytes, opcode: int) -> List[int]:
    return [offset for offset, op, _ in record_offsets(data) if op == opcode]


def read_message_keys(path) -> List[Tuple[int, int, int, int, bytes]]:
    with open(path, "rb") as stream:
        return [
            (
                record.channel_id,
                record.log_time,
                record.publish_time,
                record.sequence,
                record.data,
            )
            for record in StreamReader(stream, validate_crcs=True).records
            if isinstance(record, Message)
        ]


def receipt_keys(
    receipt: List[ConfirmedMessage],
) -> List[Tuple[int, int, int, int, bytes]]:
    return [message.key() for message in receipt]


def test_recover_truncated_chunk(tmp_path):
    messages = sample_messages()
    receipt = make_receipt(messages)
    data = build_file(messages)
    chunk_starts = record_starts(data, 0x06)
    assert len(chunk_starts) > 1
    cut = chunk_starts[-1] + 20  # inside the last chunk record

    source = tmp_path / "damaged.mcap"
    source.write_bytes(data[:cut])
    output = tmp_path / "completed.mcap"
    report = recover(source, receipt, output)

    assert report.messages_kept < len(receipt)
    assert report.messages_kept + report.messages_appended == len(receipt)
    assert report.kept_offset <= chunk_starts[-1]
    assert any("truncated" in region.reason for region in report.discarded)
    assert read_message_keys(output) == receipt_keys(receipt)
    # The damaged original is preserved untouched.
    assert source.read_bytes() == data[:cut]


def test_recover_interrupted_finish(tmp_path):
    messages = sample_messages()
    receipt = make_receipt(messages)
    data = build_file(messages)
    (data_end_offset, _, data_end_length) = next(
        record for record in record_offsets(data) if record[1] == 0x0F
    )
    # A crash a few records into finish(): DataEnd is complete, the summary is torn.
    cut = data_end_offset + 9 + data_end_length + 5

    source = tmp_path / "damaged.mcap"
    source.write_bytes(data[:cut])
    output = tmp_path / "completed.mcap"
    report = recover(source, receipt, output)

    reasons = [region.reason for region in report.discarded]
    assert any("data end record" in reason for reason in reasons)
    assert any("summary section truncated" in reason for reason in reasons)
    assert report.kept_offset == data_end_offset
    assert report.messages_kept == len(receipt)
    assert report.messages_appended == 0
    assert read_message_keys(output) == receipt_keys(receipt)


def test_recover_drops_unconfirmed_tail(tmp_path):
    messages = sample_messages(40)
    confirmed = messages[:30]
    receipt = make_receipt(confirmed)
    data = build_file(messages, chunk_size=256)

    source = tmp_path / "damaged.mcap"
    source.write_bytes(data)  # complete file, but the tail was never confirmed
    output = tmp_path / "completed.mcap"
    report = recover(source, receipt, output)

    assert any(
        "beyond the confirmed receipt" in region.reason
        for region in report.discarded
    )
    assert report.message_count == len(receipt)
    assert read_message_keys(output) == receipt_keys(receipt)


def test_out_of_order_log_times_are_not_sorted(tmp_path):
    log_times = [100, 5, 50, 1, 200, 42, 7, 150]
    messages = [
        (1 + index % 2, t, f'{{"n":{t}}}'.encode())
        for index, t in enumerate(log_times)
    ]
    receipt = make_receipt(messages)
    data = build_file(messages, chunk_size=128)
    chunk_starts = record_starts(data, 0x06)
    cut = chunk_starts[-1] + 16

    source = tmp_path / "damaged.mcap"
    source.write_bytes(data[:cut])
    output = tmp_path / "completed.mcap"
    recover(source, receipt, output)

    # Sequential read yields the receipt order, backwards timestamps included.
    assert read_message_keys(output) == receipt_keys(receipt)
    # Statistics bracket the actual timestamps instead of assuming order.
    with open(output, "rb") as stream:
        statistics = next(
            record
            for record in StreamReader(stream).records
            if isinstance(record, Statistics)
        )
    assert statistics.message_start_time == 1
    assert statistics.message_end_time == 200
    # Message indexes record the write order, not a sorted order.
    with open(output, "rb") as stream:
        indexes = [
            record
            for record in StreamReader(stream).records
            if isinstance(record, MessageIndex)
        ]
    assert indexes
    per_channel: Dict[int, List[int]] = {}
    for channel_id, log_time, _ in messages:
        per_channel.setdefault(channel_id, []).append(log_time)
    seen: Dict[int, List[int]] = {}
    for index in indexes:
        seen.setdefault(index.channel_id, []).extend(
            log_time for log_time, _ in index.records
        )
    assert seen == per_channel


def test_recover_unchunked_file(tmp_path):
    messages = sample_messages(20)
    receipt = make_receipt(messages)
    data = build_file(messages, use_chunking=False)
    message_starts = record_starts(data, 0x05)
    cut = message_starts[-1] + 3  # inside the last message record

    source = tmp_path / "damaged.mcap"
    source.write_bytes(data[:cut])
    output = tmp_path / "completed.mcap"
    report = recover(source, receipt, output)

    assert report.messages_kept == len(receipt) - 1
    assert report.messages_appended == 1
    assert read_message_keys(output) == receipt_keys(receipt)


def test_recover_corrupt_chunk_crc(tmp_path):
    messages = sample_messages(30)
    receipt = make_receipt(messages)
    data = bytearray(build_file(messages, chunk_size=200))
    first_chunk = record_starts(data, 0x06)[0]
    # Chunk header is 9 (record prefix) + 8+8+8+4+4+0+8 (fields, empty compression).
    data[first_chunk + 9 + 40 + 5] ^= 0xFF

    source = tmp_path / "damaged.mcap"
    source.write_bytes(bytes(data))
    output = tmp_path / "completed.mcap"
    report = recover(source, receipt, output)

    assert any("chunk" in region.reason for region in report.discarded)
    assert report.kept_offset <= first_chunk
    assert read_message_keys(output) == receipt_keys(receipt)


def test_recover_chunk_with_lying_time_range(tmp_path):
    messages = sample_messages(30)
    receipt = make_receipt(messages)
    data = bytearray(build_file(messages, chunk_size=200))
    first_chunk = record_starts(data, 0x06)[0]
    # Corrupt the chunk header's message start time; the chunk data is intact.
    struct.pack_into("<Q", data, first_chunk + 9, 999_999_999)

    source = tmp_path / "damaged.mcap"
    source.write_bytes(bytes(data))
    output = tmp_path / "completed.mcap"
    report = recover(source, receipt, output)

    assert any("time range" in region.reason for region in report.discarded)
    assert read_message_keys(output) == receipt_keys(receipt)


def test_verified_definitions_are_not_rewritten(tmp_path):
    messages = sample_messages(30)
    receipt = make_receipt(messages)
    data = build_file(messages, chunk_size=200)
    chunk_starts = record_starts(data, 0x06)
    cut = chunk_starts[-1] + 10

    source = tmp_path / "damaged.mcap"
    source.write_bytes(data[:cut])
    output = tmp_path / "completed.mcap"
    recover(source, receipt, output)

    # In the data section (before DataEnd) every schema and channel is defined
    # exactly once: the verified definitions were kept, not written again.
    schema_ids: List[int] = []
    channel_ids: List[int] = []
    with open(output, "rb") as stream:
        for record in StreamReader(stream, emit_chunks=True).records:
            if isinstance(record, DataEnd):
                break
            if isinstance(record, (Schema, Channel)):
                raise AssertionError("unexpected top-level definition")
            if hasattr(record, "data") and type(record).__name__ == "Chunk":
                for inner in breakup_chunk(record):
                    if isinstance(inner, Schema):
                        schema_ids.append(inner.id)
                    elif isinstance(inner, Channel):
                        channel_ids.append(inner.id)
    assert sorted(schema_ids) == [1]
    assert sorted(channel_ids) == [1, 2]

    # The rebuilt summary still carries the definitions and statistics.
    with open(output, "rb") as stream:
        summary = make_reader(stream).get_summary()
    assert summary is not None
    assert sorted(summary.schemas) == [1]
    assert sorted(summary.channels) == [1, 2]
    assert summary.statistics is not None
    assert summary.statistics.message_count == len(receipt)


def test_receipt_with_unknown_channel_fails(tmp_path):
    messages = sample_messages(10)
    data = build_file(messages)
    receipt = make_receipt(messages) + [
        ConfirmedMessage(
            channel_id=99, log_time=1000, publish_time=1000, sequence=0, data=b"{}"
        )
    ]

    source = tmp_path / "damaged.mcap"
    source.write_bytes(data)
    with pytest.raises(RecoveryError, match="channel id 99"):
        recover(source, receipt, tmp_path / "completed.mcap")


def test_data_end_crc_mismatch_fails(tmp_path):
    messages = sample_messages(10)
    receipt = make_receipt(messages)
    data = bytearray(build_file(messages, use_chunking=False, enable_data_crcs=True))
    schema_offset, _, schema_length = next(
        record for record in record_offsets(data) if record[1] == 0x03
    )
    # Corrupt one opaque schema data byte; the record still parses, but the
    # data section CRC recorded in DataEnd no longer matches.
    data[schema_offset + 9 + schema_length - 1] ^= 0xFF

    source = tmp_path / "damaged.mcap"
    source.write_bytes(bytes(data))
    with pytest.raises(RecoveryError, match="data section CRC"):
        recover(source, receipt, tmp_path / "completed.mcap")


def test_complete_file_with_matching_receipt(tmp_path):
    messages = sample_messages(25)
    receipt = make_receipt(messages)
    data = build_file(messages)
    (data_end_offset, _, _) = next(
        record for record in record_offsets(data) if record[1] == 0x0F
    )

    source = tmp_path / "damaged.mcap"
    source.write_bytes(data)
    output = tmp_path / "completed.mcap"
    report = recover(source, receipt, output)

    assert report.messages_kept == len(receipt)
    assert report.messages_appended == 0
    assert report.kept_offset == data_end_offset
    reasons = [region.reason for region in report.discarded]
    assert any("data end record" in reason for reason in reasons)
    assert any("old summary is not reused" in reason for reason in reasons)
    assert any("footer record (valid)" in reason for reason in reasons)
    assert read_message_keys(output) == receipt_keys(receipt)


def test_random_topic_time_queries(tmp_path):
    log_times = [100, 5, 50, 1, 200, 42, 7, 150, 60, 5]
    messages = [
        (1 + index % 2, log_time, f'{{"n":{index}}}'.encode())
        for index, log_time in enumerate(log_times)
    ]
    receipt = make_receipt(messages)
    data = build_file(messages, chunk_size=128)
    chunk_starts = record_starts(data, 0x06)
    cut = chunk_starts[-1] + 8

    source = tmp_path / "damaged.mcap"
    source.write_bytes(data[:cut])
    output = tmp_path / "completed.mcap"
    recover(source, receipt, output)

    with open(output, "rb") as stream:
        reader = make_reader(stream)
        for topic, channel_id in (("/a", 1), ("/b", 2)):
            got = list(
                reader.iter_messages(
                    topics=[topic],
                    starting_at=6,
                    ending_before=150,
                    log_time_order=False,
                )
            )
            expected = [
                message for message in receipt
                if message.channel_id == channel_id and 6 <= message.log_time < 150
            ]
            assert [message.key() for message in expected] == [
                (m.channel_id, m.log_time, m.publish_time, m.sequence, m.data)
                for _, _, m in got
            ]


def test_output_must_differ_from_source(tmp_path):
    messages = sample_messages(5)
    data = build_file(messages)
    source = tmp_path / "damaged.mcap"
    source.write_bytes(data)
    with pytest.raises(ValueError):
        recover(source, make_receipt(messages), source)


def test_recover_from_stream(tmp_path):
    messages = sample_messages(15)
    receipt = make_receipt(messages)
    data = build_file(messages, chunk_size=200)
    chunk_starts = record_starts(data, 0x06)
    assert len(chunk_starts) > 1
    cut = chunk_starts[-1] + 12

    output = tmp_path / "completed.mcap"
    report = recover(BytesIO(data[:cut]), receipt, output)

    assert report.source == "<stream>"
    assert read_message_keys(output) == receipt_keys(receipt)


def test_resumed_writer_must_not_start(tmp_path):
    messages = sample_messages(5)
    data = build_file(messages)
    source = tmp_path / "damaged.mcap"
    source.write_bytes(data)
    output = tmp_path / "completed.mcap"
    recover(source, make_receipt(messages), output)

    resumed = Writer.resume(BytesIO(), compression=CompressionType.NONE)
    with pytest.raises(McapError, match="start"):
        resumed.start()


def test_footer_summary_crc_mismatch_is_evidence(tmp_path):
    messages = sample_messages(10)
    receipt = make_receipt(messages)
    data = bytearray(build_file(messages))
    # Corrupt one opaque byte inside the summary's repeated schema record; the
    # records still parse, but the footer's summary CRC no longer matches.
    (data_end_offset, _, _) = next(
        record for record in record_offsets(data) if record[1] == 0x0F
    )
    summary_schema = next(
        record for record in record_offsets(data) if record[0] > data_end_offset
    )
    schema_offset, _, schema_length = summary_schema
    data[schema_offset + 9 + schema_length - 1] ^= 0xFF

    source = tmp_path / "damaged.mcap"
    source.write_bytes(bytes(data))
    output = tmp_path / "completed.mcap"
    report = recover(source, receipt, output)

    assert any(
        "summary CRC mismatch" in region.reason for region in report.discarded
    )
    assert read_message_keys(output) == receipt_keys(receipt)
