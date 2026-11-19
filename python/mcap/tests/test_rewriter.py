from io import BytesIO
from typing import List, Tuple

import pytest

from mcap.data_stream import ReadDataStream
from mcap.exceptions import McapError
from mcap.reader import make_reader
from mcap.records import (
    Attachment,
    Channel,
    Chunk,
    ChunkIndex,
    Message,
    MessageIndex,
    Metadata,
    Schema,
)
from mcap.rewriter import Rewriter
from mcap.stream_reader import StreamReader, breakup_chunk
from mcap.writer import CompressionType, Writer


def build_input(
    chunk_size: int = 1024 * 1024,
    compression: CompressionType = CompressionType.ZSTD,
    use_chunking: bool = True,
) -> BytesIO:
    """A multi-schema, multi-channel recording with backwards log times and ties."""
    output = BytesIO()
    writer = Writer(
        output,
        chunk_size=chunk_size,
        compression=compression,
        use_chunking=use_chunking,
    )
    writer.start(library="test-recorder")
    schema_a = writer.register_schema("schema/a", "jsonschema", b'{"type": "object"}')
    schema_b = writer.register_schema("schema/b", "jsonschema", b'{"type": "string"}')
    alpha = writer.register_channel("/alpha", "json", schema_a)
    beta = writer.register_channel("/beta", "json", schema_a)
    gamma = writer.register_channel("/gamma", "raw", 0)
    # Timestamps deliberately go backwards and repeat, so any re-sorting would be visible.
    timeline = [
        (alpha, 30, b"a-30"),
        (beta, 10, b"b-10"),
        (alpha, 10, b"a-10"),
        (gamma, 10, b"g-10"),
        (beta, 5, b"b-5"),
        (alpha, 25, b"a-25"),
        (gamma, 5, b"g-5"),
    ]
    for sequence, (channel_id, log_time, data) in enumerate(timeline):
        writer.add_message(channel_id, log_time, data, log_time + 1, sequence)
    writer.add_attachment(10, 9, "notes.txt", "text/plain", b"attachment-bytes")
    writer.add_metadata("run", {"venue": "test"})
    # Reference schema_b through a late channel and message as well.
    delta = writer.register_channel("/delta", "text", schema_b)
    writer.add_message(delta, 3, b"d-3", 3, 7)
    writer.finish()
    output.seek(0)
    return output


def message_identities(stream) -> List[Tuple]:
    """(topic, sequence, log_time, publish_time, payload) in physical read order."""
    reader = make_reader(stream)
    return [
        (
            channel.topic,
            message.sequence,
            message.log_time,
            message.publish_time,
            bytes(message.data),
        )
        for _, channel, message in reader.iter_messages(log_time_order=False)
    ]


def top_level_records(stream, emit_chunks: bool):
    stream.seek(0)
    return list(StreamReader(stream, emit_chunks=emit_chunks).records)


def rewrite(source: BytesIO, **kwargs) -> BytesIO:
    source.seek(0)
    output = BytesIO()
    Rewriter(source, output, **kwargs).run()
    output.seek(0)
    return output


@pytest.mark.parametrize(
    "compression,expected_name",
    [
        (CompressionType.ZSTD, "zstd"),
        (CompressionType.LZ4, "lz4"),
        (CompressionType.NONE, ""),
    ],
)
def test_rewrite_preserves_message_stream_and_definitions(compression, expected_name):
    source = build_input(compression=CompressionType.LZ4)
    expected_messages = message_identities(source)

    result = rewrite(source, chunk_size=256, compression=compression)

    assert message_identities(result) == expected_messages

    records = top_level_records(result, emit_chunks=False)
    schemas = {record.id: record for record in records if isinstance(record, Schema)}
    channels = {record.id: record for record in records if isinstance(record, Channel)}
    assert set(schemas) == {1, 2}
    assert schemas[1].name == "schema/a"
    assert schemas[2].data == b'{"type": "string"}'
    assert {channel.topic for channel in channels.values()} == {
        "/alpha",
        "/beta",
        "/gamma",
        "/delta",
    }
    # Channel -> schema relationships survive with their original ids.
    assert channels[1].schema_id == 1
    assert channels[2].schema_id == 1
    assert channels[3].schema_id == 0
    assert channels[4].schema_id == 2

    chunks = [
        record
        for record in top_level_records(result, True)
        if isinstance(record, Chunk)
    ]
    assert chunks
    assert {chunk.compression for chunk in chunks} == {expected_name}


def test_rewrite_rebuilds_chunks_and_indexes_at_new_offsets():
    source = build_input(chunk_size=4096)
    result = rewrite(source, chunk_size=128, compression=CompressionType.NONE)

    emitted = top_level_records(result, emit_chunks=True)
    chunks = [record for record in emitted if isinstance(record, Chunk)]
    chunk_indexes = [record for record in emitted if isinstance(record, ChunkIndex)]
    message_indexes = [record for record in emitted if isinstance(record, MessageIndex)]
    # The tiny target chunk size forces several chunks, each with a rebuilt index pair.
    assert len(chunks) > 1
    assert len(chunk_indexes) == len(chunks)
    assert message_indexes

    result.seek(0)
    for chunk_index in chunk_indexes:
        # Chunk indexes point at chunk records in the new file, not stale source offsets.
        # Seek past the opcode and record-length prefix to the Chunk fields.
        result.seek(chunk_index.chunk_start_offset + 9)
        chunk = Chunk.read(ReadDataStream(result))
        assert chunk.message_start_time <= chunk.message_end_time
        assert chunk_index.chunk_length > 0
        assert chunk_index.uncompressed_size == chunk.uncompressed_size
        # Every indexed offset lands on a Message Index record.
        for offset in chunk_index.message_index_offsets.values():
            result.seek(offset)
            indexed = next(StreamReader(result, skip_magic=True).records)
            assert isinstance(indexed, MessageIndex)


def test_rewrite_keeps_physical_order_with_backwards_and_equal_timestamps():
    source = build_input()
    expected = [
        (30, b"a-30"),
        (10, b"b-10"),
        (10, b"a-10"),
        (10, b"g-10"),
        (5, b"b-5"),
        (25, b"a-25"),
        (5, b"g-5"),
        (3, b"d-3"),
    ]
    result = rewrite(source, chunk_size=64, compression=CompressionType.NONE)

    reader = make_reader(result)
    observed = [
        (message.log_time, bytes(message.data))
        for _, _, message in reader.iter_messages(log_time_order=False)
    ]
    assert observed == expected


def test_random_topic_time_queries_match_source_across_ties():
    source = build_input()
    result = rewrite(source, chunk_size=96)
    queries = [
        (None, None, None),
        (["/alpha"], None, None),
        (["/alpha", "/gamma"], 10, 26),
        (None, 10, 11),  # exact-timestamp window: three messages share log_time 10
        (["/beta"], 0, 6),
        (["/delta"], 0, 100),
        (["/gamma"], 6, None),
    ]
    for topics, start, end in queries:
        source.seek(0)
        result.seek(0)
        source_hits = list(
            make_reader(source).iter_messages(
                topics=topics, starting_at=start, ending_before=end
            )
        )
        result_hits = list(
            make_reader(result).iter_messages(
                topics=topics, starting_at=start, ending_before=end
            )
        )
        assert [(m.channel_id, m.log_time, m.sequence) for _, _, m in source_hits] == [
            (m.channel_id, m.log_time, m.sequence) for _, _, m in result_hits
        ]
        # Default iteration is log-time ordered with a stable tie-break, so the full
        # triples (including payloads) must line up in the same order too.
        assert [bytes(m.data) for _, _, m in source_hits] == [
            bytes(m.data) for _, _, m in result_hits
        ]


def test_oversized_message_forms_its_own_chunk_without_truncation():
    big_payload = bytes([7]) * 500
    output = BytesIO()
    writer = Writer(output, chunk_size=4096)
    writer.start()
    channel_id = writer.register_channel("/big", "raw", 0)
    writer.add_message(channel_id, 1, b"small-1", 1)
    writer.add_message(channel_id, 2, big_payload, 2)
    writer.add_message(channel_id, 3, b"small-3", 3)
    writer.finish()
    output.seek(0)

    result = rewrite(output, chunk_size=128, compression=CompressionType.NONE)
    chunks = [
        record
        for record in top_level_records(result, True)
        if isinstance(record, Chunk)
    ]
    messages_per_chunk = [
        [record for record in breakup_chunk(chunk) if isinstance(record, Message)]
        for chunk in chunks
    ]
    all_messages = [message for messages in messages_per_chunk for message in messages]
    assert len(all_messages) == 3

    big_index = next(
        index
        for index, messages in enumerate(messages_per_chunk)
        if any(message.data == big_payload for message in messages)
    )
    # The oversized record sits alone in a chunk that is allowed to pass the target;
    # it is neither split nor held back waiting for the chunk to fill up.
    assert len(messages_per_chunk[big_index]) == 1
    assert chunks[big_index].uncompressed_size > 128
    assert messages_per_chunk[big_index][0].data == big_payload

    result.seek(0)
    payloads = [
        bytes(m.data)
        for _, _, m in make_reader(result).iter_messages(log_time_order=False)
    ]
    assert payloads == [b"small-1", big_payload, b"small-3"]


def test_every_oversized_message_makes_progress_even_when_smaller_than_none_fit():
    # Target smaller than every message: each one must still land in its own chunk
    # immediately, instead of buffering forever trying to fill the target.
    output = BytesIO()
    writer = Writer(output, chunk_size=4096)
    writer.start()
    channel_id = writer.register_channel("/big", "raw", 0)
    payloads = [bytes([index]) * 100 for index in range(4)]
    for index, payload in enumerate(payloads):
        writer.add_message(channel_id, index, payload, index)
    writer.finish()
    output.seek(0)

    result = rewrite(output, chunk_size=32, compression=CompressionType.NONE)
    chunks = [
        record
        for record in top_level_records(result, True)
        if isinstance(record, Chunk)
    ]
    assert len(chunks) == 4
    for chunk, payload in zip(chunks, payloads):
        messages = [
            record for record in breakup_chunk(chunk) if isinstance(record, Message)
        ]
        assert len(messages) == 1
        assert messages[0].data == payload
        assert chunk.uncompressed_size > 32


def test_attachments_and_metadata_are_carried_over():
    source = build_input()
    result = rewrite(source, chunk_size=256)

    records = top_level_records(result, emit_chunks=True)
    attachments = [record for record in records if isinstance(record, Attachment)]
    metadata = [record for record in records if isinstance(record, Metadata)]
    assert len(attachments) == 1
    assert attachments[0].name == "notes.txt"
    assert attachments[0].data == b"attachment-bytes"
    assert len(metadata) == 1
    assert metadata[0].name == "run"
    assert metadata[0].metadata == {"venue": "test"}


def test_unchunked_input_rewrites_into_chunks_losslessly():
    source = build_input(use_chunking=False)
    expected = message_identities(source)
    result = rewrite(source, chunk_size=128, compression=CompressionType.LZ4)

    assert message_identities(result) == expected
    assert [
        record
        for record in top_level_records(result, True)
        if isinstance(record, Chunk)
    ]


def test_rewrite_rejects_same_input_and_output_path(tmp_path):
    path = tmp_path / "same.mcap"
    source = build_input()
    path.write_bytes(source.getvalue())

    with pytest.raises(McapError, match="different files"):
        Rewriter(str(path), str(path), compression=CompressionType.NONE).run()
    # The input must be left untouched.
    assert path.read_bytes() == source.getvalue()


def test_rewrite_rejects_non_positive_chunk_size():
    with pytest.raises(ValueError, match="chunk_size"):
        Rewriter(BytesIO(), BytesIO(), chunk_size=0)


def test_rewrite_validates_input_crcs_when_requested():
    source = build_input(compression=CompressionType.NONE)
    raw = bytearray(source.getvalue())

    # Locate the first Chunk record by walking top-level record prefixes.
    position = 8
    chunk_offset = None
    chunk_length = None
    while position < len(raw):
        opcode = raw[position]
        length = int.from_bytes(raw[position + 1 : position + 9], "little")
        if opcode == 0x06:  # Opcode.CHUNK
            chunk_offset = position
            chunk_length = length
            break
        position += 9 + length
    assert chunk_offset is not None and chunk_length is not None

    # Flip the last byte of the chunk's uncompressed records, invalidating its CRC.
    raw[chunk_offset + 9 + chunk_length - 1] ^= 0xFF

    with pytest.raises(Exception):
        rewrite(
            BytesIO(bytes(raw)),
            validate_crcs=True,
            compression=CompressionType.NONE,
        )
    # Without validation the same input rewrites, recomputing fresh CRCs.
    result = rewrite(
        BytesIO(bytes(raw)), validate_crcs=False, compression=CompressionType.NONE
    )
    assert make_reader(result).get_summary() is not None


def test_explicit_id_writer_methods_reject_duplicate_registrations():
    output = BytesIO()
    writer = Writer(output)
    writer.start()
    writer.add_schema_record(Schema(id=9, data=b"x", encoding="raw", name="s"))
    with pytest.raises(McapError, match="schema id 9"):
        writer.add_schema_record(Schema(id=9, data=b"y", encoding="raw", name="s2"))
    writer.add_channel_record(
        Channel(id=7, topic="/t", message_encoding="raw", metadata={}, schema_id=0)
    )
    with pytest.raises(McapError, match="channel id 7"):
        writer.add_channel_record(
            Channel(id=7, topic="/t2", message_encoding="raw", metadata={}, schema_id=0)
        )
    writer.finish()


def test_rewritten_summary_statistics_cover_every_message():
    source = build_input()
    result = rewrite(source, chunk_size=96)
    summary = make_reader(result).get_summary()
    assert summary is not None
    assert summary.statistics is not None
    assert summary.statistics.message_count == 8
    assert summary.statistics.message_start_time == 3
    assert summary.statistics.message_end_time == 30
    assert sum(summary.statistics.channel_message_counts.values()) == 8
