"""tests for cost-based remote ranged-read planning (mcap.range_plan)."""

from io import BytesIO
from typing import List, Optional, Tuple

import pytest

from mcap.range_plan import (
    RangeReadExecutor,
    TopicTimeWindow,
    build_read_plan,
    filter_messages,
)
from mcap.reader import FOOTER_SIZE, SeekingReader
from mcap.records import Channel, ChunkIndex, Message, Schema
from mcap.stream_reader import MAGIC_SIZE
from mcap.summary import Summary
from mcap.writer import CompressionType, IndexType, Writer


class FakeRemoteFile:
    """Simulates a remote file that only supports explicit byte-range reads."""

    def __init__(self, data: bytes):
        self.data = data
        self.calls: List[Tuple[int, int]] = []

    def read_range(self, start: int, end: int) -> bytes:
        assert start >= 0 and end <= len(self.data) and end > start
        self.calls.append((start, end))
        return self.data[start:end]

    @property
    def bytes_transferred(self) -> int:
        return sum(end - start for start, end in self.calls)


def make_channel(channel_id: int, topic: str, schema_id: int = 1) -> Channel:
    return Channel(
        id=channel_id,
        topic=topic,
        message_encoding="raw",
        metadata={},
        schema_id=schema_id,
    )


def make_chunk_index(
    position: int,
    start: int,
    length: int,
    start_time: int,
    end_time: int,
    channel_ids: List[int],
    *,
    indexed: bool = True,
) -> ChunkIndex:
    return ChunkIndex(
        chunk_length=length,
        chunk_start_offset=start,
        compression="zstd",
        compressed_size=length,
        message_end_time=end_time,
        message_index_length=16 * len(channel_ids) if indexed else 0,
        message_index_offsets={
            channel_id: start + length + 10 * channel_id
            for channel_id in channel_ids
        }
        if indexed
        else {},
        message_start_time=start_time,
        uncompressed_size=length * 3,
    )


def make_summary(
    channels: List[Channel],
    chunk_indexes: List[ChunkIndex],
    schemas: Optional[List[Schema]] = None,
) -> Summary:
    summary = Summary()
    summary.channels = {channel.id: channel for channel in channels}
    summary.chunk_indexes = chunk_indexes
    if schemas is not None:
        summary.schemas = {schema.id: schema for schema in schemas}
    return summary


def test_merge_chooses_separate_requests_when_gap_costs_more_than_overhead():
    summary = make_summary(
        [make_channel(1, "/a"), make_channel(2, "/b")],
        [
            make_chunk_index(0, 1000, 100, 0, 100, [], indexed=False),
            make_chunk_index(1, 2000, 100, 200, 300, [], indexed=False),
        ],
    )
    windows = [
        TopicTimeWindow("/a", 0, 50),
        TopicTimeWindow("/b", 250, 300),
    ]

    cheap_requests = build_read_plan(
        summary, windows, max_range_size=10_000, request_overhead=100
    )
    assert len(cheap_requests.requests) == 2
    assert cheap_requests.estimated_cost == 2 * 100 + 200
    assert cheap_requests.total_bytes == 200

    expensive_requests = build_read_plan(
        summary, windows, max_range_size=10_000, request_overhead=2000
    )
    assert len(expensive_requests.requests) == 1
    assert expensive_requests.requests[0].start == 1000
    assert expensive_requests.requests[0].end == 2100
    # The 900-byte gap is downloaded as part of the merged request.
    assert expensive_requests.total_bytes == 1100
    assert expensive_requests.estimated_cost == 2000 + 1100


def test_adjacent_ranges_merge_for_free():
    summary = make_summary(
        [make_channel(1, "/a"), make_channel(2, "/b")],
        [
            make_chunk_index(0, 1000, 100, 0, 100, [], indexed=False),
            make_chunk_index(1, 1100, 100, 200, 300, [], indexed=False),
        ],
    )
    plan = build_read_plan(
        summary,
        [TopicTimeWindow("/a", 0, 50), TopicTimeWindow("/b", 250, 300)],
        max_range_size=10_000,
        request_overhead=0,
    )
    assert len(plan.requests) == 1
    assert (plan.requests[0].start, plan.requests[0].end) == (1000, 1200)


def test_max_range_size_blocks_merges_and_splits_oversized_records():
    summary = make_summary(
        [make_channel(1, "/a"), make_channel(2, "/b")],
        [
            make_chunk_index(0, 1000, 100, 0, 100, [], indexed=False),
            make_chunk_index(1, 2000, 100, 200, 300, [], indexed=False),
        ],
    )
    windows = [
        TopicTimeWindow("/a", 0, 50),
        TopicTimeWindow("/b", 250, 300),
    ]
    plan = build_read_plan(
        summary, windows, max_range_size=500, request_overhead=10_000
    )
    assert len(plan.requests) == 2
    assert all(request.length <= 500 for request in plan.requests)

    oversized_summary = make_summary(
        [make_channel(1, "/a")],
        [make_chunk_index(0, 0, 1000, 0, 100, [1])],
    )
    split_plan = build_read_plan(
        oversized_summary,
        [TopicTimeWindow("/a", 0, 50)],
        max_range_size=300,
        request_overhead=0,
    )
    # Four cap-sized pieces for the chunk plus one request for its message index block.
    assert len(split_plan.requests) == 5
    assert all(request.length <= 300 for request in split_plan.requests)
    assert split_plan.total_bytes == 1000 + 16
    assert any("larger than the per-request cap" in w for w in split_plan.warnings)


def test_bbox_overlap_only_makes_a_chunk_a_candidate():
    summary = make_summary(
        [make_channel(1, "/a"), make_channel(2, "/b")],
        [
            # Chunk 1's bbox overlaps [40, 60) but it holds no /a message in that
            # window: its /a message is at t=70.
            make_chunk_index(0, 0, 100, 0, 30, [1]),
            make_chunk_index(1, 500, 100, 40, 80, [1, 2]),
            make_chunk_index(2, 1000, 100, 90, 120, [1]),
        ],
    )
    plan = build_read_plan(
        summary,
        [TopicTimeWindow("/a", 40, 60)],
        max_range_size=10_000,
        request_overhead=0,
    )
    # Bbox pruning removes chunk 0 and chunk 2; chunk 1 stays as a candidate only.
    assert [download.chunk_index_position for download in plan.chunk_downloads] == [1]
    candidate = plan.chunk_downloads[0]
    assert candidate.requires_message_filtering is True
    # The candidate bbox does not claim a hit: filtering must still happen downstream.
    messages: List[Message] = [
        Message(1, 70, b"outside", 70, 0),
        Message(2, 45, b"other-topic", 45, 0),
    ]
    assert filter_messages(messages, summary.channels, plan.windows) == []


def test_unindexed_chunks_are_scanned_not_assumed_empty():
    summary = make_summary(
        [make_channel(1, "/a")],
        [
            make_chunk_index(0, 0, 100, 0, 100, [], indexed=False),
            make_chunk_index(1, 500, 100, 200, 300, [], indexed=False),
        ],
    )
    plan = build_read_plan(
        summary,
        [TopicTimeWindow("/a", 50, 150)],
        max_range_size=10_000,
        request_overhead=0,
    )
    # The bbox-overlapping chunk is a candidate even though no index proves topic
    # membership; the bbox-disjoint chunk is still prunable.
    assert [download.chunk_index_position for download in plan.chunk_downloads] == [0]
    assert plan.unindexed_regions == [(0, 100)]
    missing = [
        dependency
        for dependency in plan.dependencies
        if dependency.kind == "message_index" and not dependency.available
    ]
    assert missing and "scan" in missing[0].reason


def test_shared_chunk_is_downloaded_and_decompressed_once():
    summary = make_summary(
        [make_channel(1, "/a")],
        [make_chunk_index(0, 1000, 100, 0, 1000, [1])],
    )
    plan = build_read_plan(
        summary,
        [
            TopicTimeWindow("/a", 100, 200),
            TopicTimeWindow("/a", 300, 400),
            TopicTimeWindow("/a", 500, 600),
        ],
        max_range_size=10_000,
        request_overhead=500,
    )
    assert len(plan.chunk_downloads) == 1
    download = plan.chunk_downloads[0]
    assert download.serves_windows == (0, 1, 2)
    chunk_requests = [
        request
        for request in plan.requests
        if any("chunk[0]" in tag for tag in request.purpose)
    ]
    assert len(chunk_requests) == 1


def test_dependencies_include_bootstrap_definitions_and_indexes():
    schema = Schema(id=7, data=b"{}", encoding="raw", name="demo")
    summary = make_summary(
        [make_channel(1, "/a", schema_id=7)],
        [make_chunk_index(0, 1000, 100, 0, 100, [1])],
        schemas=[schema],
    )
    plan = build_read_plan(
        summary,
        [TopicTimeWindow("/a", 10, 90)],
        max_range_size=10_000,
        request_overhead=0,
        file_size=5000,
        summary_section=(3000, 4800),
    )
    kinds = {dependency.kind for dependency in plan.dependencies}
    assert {"footer", "summary_section", "schema", "channel", "message_index"} <= kinds
    footer = next(d for d in plan.dependencies if d.kind == "footer")
    assert footer.start == 5000 - (FOOTER_SIZE + MAGIC_SIZE)
    assert footer.end == 5000
    assert any(
        "summary-section" in tag
        for request in plan.requests
        for tag in request.purpose
    )
    message_index_dependency = next(
        d for d in plan.dependencies if d.kind == "message_index"
    )
    assert message_index_dependency.available is True
    assert message_index_dependency.start is not None

    ordered_kinds = [
        dependency.kind for dependency in plan.dependencies_in_order()
    ]
    assert ordered_kinds.index("footer") < ordered_kinds.index("summary_section")
    assert ordered_kinds.index("summary_section") < ordered_kinds.index("channel")
    assert ordered_kinds.index("channel") < ordered_kinds.index("message_index")


def test_invalid_arguments_are_rejected():
    summary = make_summary([make_channel(1, "/a")], [])
    with pytest.raises(ValueError, match="max_range_size"):
        build_read_plan(
            summary,
            [TopicTimeWindow("/a")],
            max_range_size=0,
            request_overhead=0,
        )
    with pytest.raises(ValueError, match="request_overhead"):
        build_read_plan(
            summary,
            [TopicTimeWindow("/a")],
            max_range_size=100,
            request_overhead=-1,
        )
    with pytest.raises(ValueError, match="no channel"):
        build_read_plan(
            summary,
            [TopicTimeWindow("/missing")],
            max_range_size=100,
            request_overhead=0,
        )
    with pytest.raises(ValueError, match="starts after"):
        build_read_plan(
            summary,
            [TopicTimeWindow("/a", 100, 50)],
            max_range_size=100,
            request_overhead=0,
        )
    with pytest.raises(ValueError, match="at least one"):
        build_read_plan(summary, [], max_range_size=100, request_overhead=0)


def write_demo_mcap(buffer: BytesIO, compression: CompressionType) -> None:
    writer = Writer(
        buffer,
        chunk_size=4 * 1024 * 1024,
        compression=compression,
        index_types=IndexType.ALL,
    )
    writer.start()
    schema_id = writer.register_schema("demo", "raw", b"{}")
    channel_a = writer.register_channel("/a", "raw", schema_id)
    channel_b = writer.register_channel("/b", "raw", schema_id)
    for log_time in range(0, 10):
        writer.add_message(
            channel_a, log_time * 100, f"a-{log_time}".encode(), log_time
        )
    writer.flush()
    for log_time in range(10, 20):
        writer.add_message(
            channel_b, log_time * 100, f"b-{log_time}".encode(), log_time
        )
    writer.flush()
    for log_time in range(20, 30):
        writer.add_message(
            channel_a, log_time * 100, f"a-{log_time}".encode(), log_time
        )
        writer.add_message(
            channel_b, log_time * 100, f"b-{log_time}".encode(), log_time
        )
    writer.finish()


def write_unindexed_mcap(buffer: BytesIO) -> None:
    writer = Writer(
        buffer,
        chunk_size=4 * 1024 * 1024,
        compression=CompressionType.NONE,
        index_types=IndexType.CHUNK,
    )
    writer.start()
    schema_id = writer.register_schema("demo", "raw", b"{}")
    channel_a = writer.register_channel("/a", "raw", schema_id)
    for log_time in range(0, 10):
        writer.add_message(
            channel_a, log_time * 100, f"a-{log_time}".encode(), log_time
        )
    writer.finish()


@pytest.mark.parametrize(
    "compression", [CompressionType.ZSTD, CompressionType.LZ4, CompressionType.NONE]
)
def test_executor_returns_only_filtered_hits(compression: CompressionType):
    buffer = BytesIO()
    write_demo_mcap(buffer, compression)
    data = buffer.getvalue()

    summary = SeekingReader(BytesIO(data)).get_summary()
    assert summary is not None
    windows = [
        TopicTimeWindow("/a", 300, 900),  # chunk 0: t=300..800
        TopicTimeWindow("/b", 1500, 2600),  # chunk 1 and chunk 2 candidates
    ]
    plan = build_read_plan(
        summary,
        windows,
        max_range_size=4096,
        request_overhead=256,
        file_size=len(data),
    )
    remote = FakeRemoteFile(data)
    executor = RangeReadExecutor(plan, summary, remote.read_range)
    hits = executor.iter_messages()

    got = sorted(
        (channel.topic, message.log_time, message.data) for _, channel, message in hits
    )
    expected = []
    reference = SeekingReader(BytesIO(data))
    for topic, start, end in windows:
        for _, channel, message in reference.iter_messages(
            topics=[topic], starting_at=start, ending_before=end
        ):
            expected.append((channel.topic, message.log_time, message.data))
    assert got == sorted(expected)
    # Half-open upper bound: no message at an excluded boundary timestamp leaks in.
    for _, channel, message in hits:
        window = next(window for window in windows if window.topic == channel.topic)
        assert window.start_time is not None and window.end_time is not None
        assert window.start_time <= message.log_time < window.end_time
    # The executor only issues requests that cover a chunk, each one a single time;
    # footer/summary bootstrap ranges are planning dependencies, not chunk fetches.
    expected_request_indexes = {
        index
        for download in plan.chunk_downloads
        for index in download.request_indexes
    }
    assert len(remote.calls) == len(expected_request_indexes)
    assert remote.bytes_transferred == sum(
        plan.requests[index].length for index in expected_request_indexes
    )


def test_executor_deduplicates_shared_chunk_fetches():
    buffer = BytesIO()
    write_demo_mcap(buffer, CompressionType.ZSTD)
    data = buffer.getvalue()
    summary = SeekingReader(BytesIO(data)).get_summary()
    assert summary is not None
    plan = build_read_plan(
        summary,
        [
            TopicTimeWindow("/a", 100, 400),
            TopicTimeWindow("/a", 500, 800),
        ],
        max_range_size=10_000_000,
        request_overhead=10_000,
        file_size=len(data),
    )
    remote = FakeRemoteFile(data)
    executor = RangeReadExecutor(plan, summary, remote.read_range)
    hits = executor.iter_messages()
    timestamps = sorted(message.log_time for _, _, message in hits)
    assert timestamps == [100, 200, 300, 500, 600, 700]
    # One chunk download serves all three windows: a single request is issued once and
    # the chunk is decompressed once, not once per window.
    assert len(plan.chunk_downloads) == 1
    assert len(plan.chunk_downloads[0].request_indexes) == 1
    assert len(remote.calls) == 1
    # Buffers are released once their last referencing chunk has been processed.
    assert executor._request_cache == {}
    assert executor._request_refcounts == {}

    # Streaming execution does not buffer every hit before returning.
    streamed = list(
        RangeReadExecutor(plan, summary, FakeRemoteFile(data).read_range)
        .iter_filtered_messages()
    )
    assert sorted(message.log_time for _, _, message in streamed) == timestamps


def test_executor_scans_unindexed_chunks_and_still_finds_messages():
    buffer = BytesIO()
    write_unindexed_mcap(buffer)
    data = buffer.getvalue()
    summary = SeekingReader(BytesIO(data)).get_summary()
    assert summary is not None
    assert all(
        not chunk_index.message_index_offsets
        for chunk_index in summary.chunk_indexes
    )
    plan = build_read_plan(
        summary,
        [TopicTimeWindow("/a", 200, 600)],
        max_range_size=256,
        request_overhead=0,
        file_size=len(data),
    )
    assert plan.unindexed_regions
    remote = FakeRemoteFile(data)
    hits = RangeReadExecutor(plan, summary, remote.read_range).iter_messages()
    assert sorted(message.log_time for _, _, message in hits) == [200, 300, 400, 500]
    # The cap split the unindexed chunk across several requests, reassembled exactly.
    assert all(end - start <= 256 for start, end in remote.calls)


def test_filter_messages_respects_half_open_windows_and_topics():
    summary = make_summary(
        [make_channel(1, "/a"), make_channel(2, "/b")], []
    )
    records = [
        Message(1, 100, b"1", 100, 0),
        Message(1, 200, b"2", 200, 0),
        Message(2, 150, b"3", 150, 0),
    ]
    hits = filter_messages(
        records,
        summary.channels,
        [TopicTimeWindow("/a", 100, 200)],
    )
    assert [(message.log_time, message.data) for _, message in hits] == [(100, b"1")]

    multi = filter_messages(
        records,
        summary.channels,
        [TopicTimeWindow("/a", None, 150), TopicTimeWindow("/b", 150, None)],
    )
    assert sorted((channel.topic, message.log_time) for channel, message in multi) == [
        ("/a", 100),
        ("/b", 150),
    ]
