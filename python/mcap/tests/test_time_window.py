"""Tests for composable log-time / publish-time queries and their ordering."""


from io import BytesIO
from pathlib import Path
from typing import IO, Any, Dict, List, Optional, Tuple

import pytest

from mcap.reader import NonSeekingReader, SeekingReader
from mcap.records import Channel, Message, Schema
from mcap.time_window import (
    MAX_TIMESTAMP,
    MessageOrder,
    MessageQuery,
    TimeRange,
    TimeWindow,
)
from mcap.writer import CompressionType, IndexType, Writer

MessageRow = Tuple[Optional[Schema], Channel, Message]


def write_mcap(
    filepath: Optional[Path] = None,
    rows: Optional[List[Tuple[str, int, int, bytes]]] = None,
    *,
    chunk_size: int = 1,
    index_types: IndexType = IndexType.ALL,
    compression: CompressionType = CompressionType.NONE,
) -> IO[bytes]:
    """Write ``(topic, log_time, publish_time, data)`` rows.

    ``chunk_size=1`` puts every message in its own chunk, so tests exercise
    cross-chunk merging as well as per-chunk ordering.
    """
    assert rows is not None
    buffer = BytesIO()
    writer = Writer(
        buffer, chunk_size=chunk_size, index_types=index_types, compression=compression
    )
    writer.start()
    channels: Dict[str, int] = {}
    for topic, log_time, publish_time, data in rows:
        if topic not in channels:
            channels[topic] = writer.register_channel(topic, "json", 0)
        writer.add_message(
            channels[topic],
            log_time=log_time,
            data=data,
            publish_time=publish_time,
            sequence=len(data),
        )
    writer.finish()
    if filepath is not None:
        filepath.write_bytes(buffer.getvalue())
    return buffer


def read_all(
    source: IO[bytes],
    reader_cls: Any = SeekingReader,
    **kwargs: Any,
) -> List[Tuple[str, int, int, bytes]]:
    source.seek(0)
    reader = reader_cls(source)
    return [
        (channel.topic, message.log_time, message.publish_time, message.data)
        for _, channel, message in reader.iter_messages(**kwargs)
    ]


# --- query primitives -------------------------------------------------------


def test_time_range_bounds_are_half_open_and_keep_nanoseconds():
    value = 2**62 + 12345
    assert TimeRange.starting_at(value).includes(value)
    assert not TimeRange.starting_at(value).includes(value - 1)
    assert TimeRange.starting_after(value).includes(value + 1)
    assert not TimeRange.starting_after(value).includes(value)
    assert TimeRange.ending_before(value).includes(value - 1)
    assert not TimeRange.ending_before(value).includes(value)
    assert TimeRange.ending_at(value).includes(value)
    assert not TimeRange.ending_at(value).includes(value + 1)
    # No float conversion happens anywhere: the value round-trips exactly.
    assert TimeRange.starting_at(value).start == value


def test_time_range_rejects_crossed_bounds_but_allows_empty():
    with pytest.raises(ValueError, match="end time cannot come before start"):
        TimeRange(10, 5)
    empty = TimeRange(5, 5)
    assert empty.is_empty
    assert not empty.includes(5)
    assert not empty.intersects(0, 100)


def test_time_range_rejects_floats_and_normalizes_integral_types():
    with pytest.raises(TypeError):
        TimeRange(1.5, 2)
    with pytest.raises(TypeError):
        TimeRange(True, 2)
    with pytest.raises(ValueError):
        TimeRange(-1, 2)

    class IntLike:
        def __init__(self, value: int):
            self._value = value

        def __index__(self) -> int:
            return self._value

    # An integral type outside the exact int range still round-trips exactly.
    large = 2**63 + 7
    assert TimeRange.starting_at(IntLike(large)).start == large
    assert TimeRange.ending_at(IntLike(large)).end == large + 1


def test_windows_compose_by_intersection_and_union():
    log_window = TimeWindow(log_time=TimeRange(100, 200))
    publish_window = TimeWindow(publish_time=TimeRange(500, 600))

    both = log_window & publish_window
    assert isinstance(both, TimeWindow)
    assert both.includes_times(150, 550)
    assert not both.includes_times(150, 700)
    assert not both.includes_times(250, 550)

    either = log_window | publish_window
    assert isinstance(either, MessageQuery)
    assert either.includes_times(150, 0)
    assert either.includes_times(0, 550)
    assert not either.includes_times(0, 0)

    # Intersecting a disjunction distributes across its windows.
    narrowed = either & TimeWindow(publish_time=TimeRange(500, 600))
    assert narrowed.includes_times(150, 0) is False
    assert narrowed.includes_times(150, 550) is True
    assert narrowed.includes_times(0, 550) is True


def test_query_accepts_tuples_and_range_aliases():
    window = TimeWindow(log_time=(10, 20), publish_time_range=(30, 40))
    assert window.log_time == TimeRange(10, 20)
    assert window.publish_time == TimeRange(30, 40)
    with pytest.raises(TypeError):
        TimeWindow(log_time=(1, 2), log_time_range=(3, 4))


def test_message_order_coercion():
    assert MessageOrder.coerce("log_time") is MessageOrder.LOG_TIME
    assert MessageOrder.coerce("file-order") is MessageOrder.FILE_ORDER
    with pytest.raises(ValueError):
        MessageOrder.coerce("wall_clock")
    # Publish time is a query condition, not an ordering criterion.
    with pytest.raises(ValueError, match="publish_time is a query"):
        MessageOrder.coerce("publish_time")


# --- filtering --------------------------------------------------------------


def test_publish_time_filter_survives_backwards_publish_clock(tmp_path: Path):
    """A publish-time query must find messages whose log time lies outside the window.

    The chunk index only proves the log-time span. A publish clock that runs
    backwards puts a matching message in a chunk the log-time index would prune.
    """
    filepath = tmp_path / "backwards.mcap"
    # Log times ascend with file order; publish times descend.
    rows = [
        (f"/t{i % 2}", i, 1000 - i, f"m{i}".encode())
        for i in range(20)
    ]
    source = write_mcap(filepath, rows, chunk_size=1)

    window = TimeWindow(publish_time=TimeRange(990, 1000))
    expected = [row for row in rows if 990 <= row[2] < 1000]
    assert len(expected) == 10

    for reader_cls in (SeekingReader, NonSeekingReader):
        got = read_all(source, reader_cls, query=window)
        assert {(log_time, publish_time) for _, log_time, publish_time, _ in got} == {
            (log_time, publish_time) for _, log_time, publish_time, _ in expected
        }


def test_publish_time_bounds_match_query_form(tmp_path: Path):
    filepath = tmp_path / "bounds.mcap"
    rows = [(f"/t{i % 3}", i, 500 + i, f"m{i}".encode()) for i in range(30)]
    source = write_mcap(filepath, rows, chunk_size=2)

    by_query = read_all(
        source, query=TimeWindow(publish_time=TimeRange.ending_at(510))
    )
    by_bounds = read_all(source, publish_ending_at=510)
    assert by_query == by_bounds
    assert all(publish_time <= 510 for _, _, publish_time, _ in by_query)

    # An exclusive lower bound drops exactly the message at the bound.
    exclusive = read_all(source, publish_starting_after=500)
    assert all(publish_time > 500 for _, _, publish_time, _ in exclusive)
    inclusive = read_all(source, publish_starting_at=500)
    assert len(inclusive) == len(exclusive) + 1


def test_conjunctive_log_and_publish_window(tmp_path: Path):
    filepath = tmp_path / "conjunction.mcap"
    rows = [(f"/t{i % 2}", i, (i * 7) % 20, f"m{i}".encode()) for i in range(40)]
    source = write_mcap(filepath, rows, chunk_size=1)

    window = TimeWindow(
        log_time=TimeRange(10, 30), publish_time=TimeRange(5, 15)
    )
    expected = [
        row for row in rows if 10 <= row[1] < 30 and 5 <= row[2] < 15
    ]
    for reader_cls in (SeekingReader, NonSeekingReader):
        got = read_all(source, reader_cls, query=window)
        assert sorted(got) == sorted(
            (topic, log_time, publish_time, data)
            for topic, log_time, publish_time, data in expected
        )


def test_disjunctive_query_reads_each_message_once(tmp_path: Path):
    filepath = tmp_path / "union.mcap"
    rows = [(f"/t{i % 2}", i, i, f"m{i}".encode()) for i in range(40)]
    source = write_mcap(filepath, rows, chunk_size=3)

    query = TimeWindow(log_time=TimeRange(0, 20)) | TimeWindow(
        log_time=TimeRange(10, 30)
    )
    for reader_cls in (SeekingReader, NonSeekingReader):
        got = read_all(source, reader_cls, query=query, order="file_order")
        assert len(got) == 30
        assert len({data for _, _, _, data in got}) == 30


def test_shared_chunk_across_windows_is_read_once(tmp_path: Path, monkeypatch: Any):
    """When two windows select the same chunk, the chunk is read once and its
    messages are deduplicated, not emitted once per window."""
    filepath = tmp_path / "shared_chunk.mcap"
    rows = [("/t", i, i, f"m{i}".encode()) for i in range(12)]
    source = write_mcap(filepath, rows, chunk_size=90)

    source.seek(0)
    summary = SeekingReader(source).get_summary()
    assert summary is not None
    chunk_count = len(summary.chunk_indexes)
    assert chunk_count > 1

    reads: List[int] = []
    original = SeekingReader._read_chunk_messages

    def counting(self: SeekingReader, chunk_index: Any, *args: Any, **kwargs: Any):
        reads.append(chunk_index.chunk_start_offset)
        return original(self, chunk_index, *args, **kwargs)

    monkeypatch.setattr(SeekingReader, "_read_chunk_messages", counting)

    # Both windows overlap at log time 5, so the chunk holding it is selected by
    # both; together they select every chunk.
    query = TimeWindow(log_time=TimeRange(0, 6)) | TimeWindow(
        log_time=TimeRange(5, 12)
    )
    got = read_all(source, SeekingReader, query=query, order="file_order")
    assert [data for _, _, _, data in got] == [f"m{i}".encode() for i in range(12)]
    # Every chunk is read exactly once even though two windows select some of them.
    assert len(reads) == len(set(reads)) == chunk_count


# --- ordering ---------------------------------------------------------------


def test_publish_time_order_is_rejected(tmp_path: Path):
    """Ordering by a clock the chunk index cannot bound is refused, not silently
    buffered: publish_time is a query condition, not an ordering criterion."""
    filepath = tmp_path / "publish_order.mcap"
    rows = [("/t", i, 100 - i, f"m{i}".encode()) for i in range(8)]
    source = write_mcap(filepath, rows, chunk_size=1)

    with pytest.raises(ValueError, match="not an ordering criterion"):
        read_all(source, order="publish_time")


def test_order_argument_wins_over_legacy_flag(tmp_path: Path):
    filepath = tmp_path / "order_flag.mcap"
    # Written so log time and file order differ: the file order is the row order.
    rows = [("/t", (i * 7) % 11, i, f"m{i}".encode()) for i in range(11)]
    source = write_mcap(filepath, rows, chunk_size=1)

    by_log_time = read_all(source, order="log_time")
    assert [log_time for _, log_time, _, _ in by_log_time] == sorted(
        row[1] for row in rows
    )
    by_file = read_all(source, order="file_order")
    assert [data for _, _, _, data in by_file] == [row[3] for row in rows]
    # log_time_order=False no longer applies once order is explicit.
    assert read_all(source, order="log_time", log_time_order=False) == by_log_time
    with pytest.raises(ValueError, match="reverse is only valid"):
        read_all(source, order="file_order", reverse=True)


def test_indexed_and_linear_reads_agree(tmp_path: Path):
    """A sequential scan and the index query must yield the same messages in the
    same order for the same query."""
    filepath = tmp_path / "parity.mcap"
    rows = [
        (f"/t{(i * 3) % 4}", (i * 13) % 50, (i * 29) % 40, f"m{i}".encode())
        for i in range(120)
    ]
    source = write_mcap(filepath, rows, chunk_size=7)
    queries = [
        TimeWindow(log_time=TimeRange(5, 45), publish_time=TimeRange(3, 35)),
        # A publish-only window must also agree: the index cannot prune on it.
        TimeWindow(publish_time=TimeRange(0, 20)),
    ]

    for query in queries:
        for order in ("log_time", "file_order"):
            forward_indexed = read_all(
                source, SeekingReader, query=query, order=order
            )
            forward_linear = read_all(
                source, NonSeekingReader, query=query, order=order
            )
            assert forward_indexed == forward_linear, (query, order)
            assert forward_indexed
            if order == "file_order":
                continue
            # Reverse is exactly the reverse of forward in both readers, so tied
            # messages keep the same stable appearance.
            assert (
                read_all(source, SeekingReader, query=query, order=order, reverse=True)
                == forward_indexed[::-1]
            )
            assert (
                read_all(
                    source, NonSeekingReader, query=query, order=order, reverse=True
                )
                == forward_linear[::-1]
            )


def test_publish_only_query_does_not_trust_log_time_index(tmp_path: Path):
    """A publish-time-only window keeps chunks whose log-time span is disjoint.

    The publish clock may run backwards, so the log-time chunk index must not be
    used to prune chunks for a publish-only condition.
    """
    filepath = tmp_path / "publish_only.mcap"
    # Log times 0..9 ascend with file order; publish times descend from 100.
    rows = [("/t", i, 100 - i, f"m{i}".encode()) for i in range(10)]
    source = write_mcap(filepath, rows, chunk_size=1)

    # publish_time in [90, 95) selects log times 6..9. Reading those publish
    # bounds as a log-time window would select nothing at all.
    window = TimeWindow(publish_time=TimeRange(90, 95))
    assert read_all(source, query=window, order="file_order") == [
        ("/t", i, 100 - i, f"m{i}".encode()) for i in range(6, 10)
    ]
    for reader_cls in (SeekingReader, NonSeekingReader):
        got = read_all(source, reader_cls, query=window)
        expected = read_all(source, SeekingReader, query=window, order="log_time")
        assert got == expected


def test_missing_index_scans_necessary_range(tmp_path: Path, monkeypatch: Any):
    """Without usable chunk indexes the seeking reader falls back to a full scan,
    and a publish-time query still finds everything it should."""
    filepath = tmp_path / "no_index.mcap"
    rows = [(f"/t{i % 2}", i, 100 - i, f"m{i}".encode()) for i in range(10)]
    source = write_mcap(
        filepath, rows, chunk_size=2, index_types=IndexType.NONE
    )

    window = TimeWindow(publish_time=TimeRange(95, 100))
    got = read_all(source, SeekingReader, query=window)
    assert len(got) == 5
    assert all(95 <= publish_time < 100 for _, _, publish_time, _ in got)


def test_empty_query_reads_nothing(tmp_path: Path):
    filepath = tmp_path / "empty.mcap"
    rows = [("/t", i, i, f"m{i}".encode()) for i in range(5)]
    source = write_mcap(filepath, rows, chunk_size=1)

    assert read_all(source, query=TimeWindow(log_time=TimeRange(5, 5))) == []
    # A publish bound that admits nothing.
    assert read_all(source, publish_starting_at=MAX_TIMESTAMP + 1) == []


def test_max_timestamp_publish_bound(tmp_path: Path):
    filepath = tmp_path / "max.mcap"
    rows = [("/t", 1, 2**64 - 1, b"last"), ("/t", 2, 3, b"early")]
    source = write_mcap(filepath, rows, chunk_size=1)

    got = read_all(source, publish_ending_at=2**64 - 1)
    assert [data for _, _, _, data in got] == [b"last", b"early"]
    got = read_all(source, publish_starting_after=2**64 - 2)
    assert [data for _, _, _, data in got] == [b"last"]


def test_iter_decoded_messages_accepts_query(tmp_path: Path):
    filepath = tmp_path / "decoded.mcap"
    rows = [("/t", i, 100 + i, f"m{i}".encode()) for i in range(6)]
    source = write_mcap(filepath, rows, chunk_size=1)

    class RawDecoderFactory:
        def decoder_for(self, message_encoding: str, schema: Optional[Schema]):
            return lambda data: data

    source.seek(0)
    reader = SeekingReader(source, decoder_factories=[RawDecoderFactory()])
    values = [
        decoded
        for _, _, _, decoded in reader.iter_decoded_messages(
            query=TimeWindow(publish_time=TimeRange(102, 104))
        )
    ]
    assert values == [b"m2", b"m3"]
