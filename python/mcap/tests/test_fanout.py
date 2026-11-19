"""Tests for the shared, single-pass fan-out stream reader."""

import gc
import os
import threading
import weakref
from io import BytesIO
from typing import List

import pytest

from mcap.fanout import (
    BackpressureError,
    BackpressurePolicy,
    ChunkWindow,
    FanoutMessage,
    MessageGap,
    OversizeMessageError,
    SharedStreamReader,
)
from mcap.writer import CompressionType, Writer


def write_mcap(
    buffer,
    topics=("/a", "/b"),
    messages_per_topic=5,
    *,
    compression=CompressionType.ZSTD,
    chunk_size=256,
    use_chunking=True,
    payload_size=10,
):
    """Writes a small MCAP with one channel per topic and numbered messages."""
    writer = Writer(
        buffer,
        compression=compression,
        chunk_size=chunk_size,
        use_chunking=use_chunking,
    )
    writer.start()
    channels = {}
    for topic in topics:
        channels[topic] = writer.register_channel(
            topic=topic, message_encoding="raw", schema_id=0
        )
    for index in range(messages_per_topic):
        for topic in topics:
            writer.add_message(
                channels[topic],
                log_time=index + 1,
                data=(topic.encode() + b"-" + str(index).encode()).ljust(
                    payload_size, b"0"
                ),
                publish_time=index + 1,
            )
    writer.finish()
    return buffer


def make_reader(buffer, **kwargs):
    buffer.seek(0)
    return SharedStreamReader(buffer, **kwargs)


def drain(subscription, expected=None, timeout=5.0):
    """Collects messages from a subscription driven by a background thread."""
    out: List[FanoutMessage] = []
    while True:
        item = next_with_timeout(subscription, timeout)
        if item is None:
            break
        out.append(item)
        if expected is not None and len(out) == expected:
            break
    return out


def next_with_timeout(subscription, timeout):
    result = []
    error = []

    def work():
        try:
            result.append(next(subscription))
        except StopIteration:
            pass
        except BaseException as exc:  # noqa: BLE001 - re-raised on the test thread
            error.append(exc)

    thread = threading.Thread(target=work)
    thread.daemon = True
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        pytest.fail("subscription did not produce a message within the timeout")
    if error:
        raise error[0]
    return result[0] if result else None


def wait_until(predicate, timeout=5.0):
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_fanout_routes_topics_to_independent_subscriptions():
    reader = make_reader(write_mcap(BytesIO()))
    sub_a = reader.subscribe("/a", max_messages=None)
    sub_b = reader.subscribe("/b", max_messages=None)
    reader.run()

    messages_a = drain(sub_a, expected=5)
    # Cursors are independent: draining one left the other fully buffered.
    assert sub_b.buffered_count == 5
    messages_b = drain(sub_b, expected=5)
    assert [m.channel.topic for m in messages_a] == ["/a"] * 5
    assert [m.channel.topic for m in messages_b] == ["/b"] * 5
    assert [m.message.log_time for m in messages_a] == [1, 2, 3, 4, 5]


def test_wildcard_subscription_receives_every_topic():
    reader = make_reader(write_mcap(BytesIO()))
    subscription = reader.subscribe(None, max_messages=None)
    reader.run()
    messages = drain(subscription, expected=10)
    assert len(messages) == 10
    assert {m.channel.topic for m in messages} == {"/a", "/b"}


def test_each_compressed_chunk_is_decompressed_only_once(monkeypatch):
    import mcap.fanout as fanout
    from mcap.records import Chunk as ChunkRecord
    from mcap.stream_reader import StreamReader

    buffer = write_mcap(BytesIO(), messages_per_topic=8, chunk_size=128)
    buffer.seek(0)
    chunk_count = sum(
        1 for record in StreamReader(buffer, emit_chunks=True).records
        if isinstance(record, ChunkRecord)
    )
    assert chunk_count > 1  # the fixture really spans multiple chunks

    original = fanout._get_chunk_data
    calls = []

    def counting(chunk, validate_crc=False):
        calls.append(chunk)
        return original(chunk, validate_crc=validate_crc)
    monkeypatch.setattr(fanout, "_get_chunk_data", counting)

    reader = make_reader(buffer)
    reader.subscribe("/a", max_messages=None)
    reader.subscribe("/b", max_messages=None)
    reader.subscribe(None, max_messages=None)
    reader.run()

    # Three subscriptions, but exactly one decompression per chunk.
    assert len(calls) == chunk_count
    assert len({id(chunk) for chunk in calls}) == chunk_count


def test_messages_share_chunk_bytes_without_copying():
    reader = make_reader(write_mcap(BytesIO()))
    subscription = reader.subscribe("/a", max_messages=None)
    reader.run()
    first = next(subscription)
    assert isinstance(first.message.data, memoryview)
    assert first.message.data.readonly
    assert bytes(first.message.data).startswith(b"/a-")


def test_chunk_window_is_retained_until_last_reference_drops():
    reader = make_reader(
        write_mcap(BytesIO(), messages_per_topic=3, chunk_size=64)
    )
    subscription = reader.subscribe(None, max_messages=None)
    reader.run()
    assert subscription.buffered_count > 0

    window_ref = weakref.ref(subscription._queue[0].window)
    assert isinstance(window_ref(), ChunkWindow)

    held = subscription._queue[0]
    held_ref = weakref.ref(held.window)
    subscription._queue.clear()
    gc.collect()
    assert held_ref() is not None  # the held entry keeps the chunk alive

    del held
    gc.collect()
    assert held_ref() is None
    assert window_ref() is None


def test_block_policy_is_lossless_and_reports_the_blocking_boundary():
    reader = make_reader(
        write_mcap(BytesIO(), topics=("/a",), messages_per_topic=20, chunk_size=64)
    )
    slow = reader.subscribe("/a", max_messages=2, policy=BackpressurePolicy.BLOCK)
    fast = reader.subscribe("/a", max_messages=None, policy=BackpressurePolicy.BLOCK)
    thread = reader.run_in_thread()

    assert wait_until(lambda: reader.blocked_on is slow)
    assert slow.buffered_count == 2
    assert slow.dropped_message_count == 0

    received = []
    for _ in range(20):
        message = next_with_timeout(slow, 5.0)
        assert message is not None
        received.append(message)
    assert len(received) == 20
    assert slow.dropped_message_count == 0
    assert slow.blocked_nanoseconds > 0

    fast_messages = drain(fast, expected=20)
    assert len(fast_messages) == 20
    thread.join(5.0)
    assert not thread.is_alive()
    assert reader.blocked_on is None


def test_drop_oldest_only_affects_the_lagging_subscription():
    reader = make_reader(write_mcap(BytesIO(), messages_per_topic=5))
    laggard = reader.subscribe(
        "/a", max_messages=2, policy=BackpressurePolicy.DROP_OLDEST
    )
    healthy = reader.subscribe("/a", max_messages=None)
    reader.run()

    kept = drain(laggard, expected=2)
    assert [m.message.log_time for m in kept] == [4, 5]
    assert laggard.dropped_message_count == 3
    gaps = laggard.gaps
    assert len(gaps) == 1
    gap = gaps[0]
    assert isinstance(gap, MessageGap)
    assert gap.count == 3
    # The dropped messages are the evicted heads, timestamps 1 through 3.
    assert gap.first_log_time == 1
    assert gap.last_log_time == 3

    assert len(drain(healthy, expected=5)) == 5
    assert healthy.dropped_message_count == 0


def test_drop_latest_keeps_the_head_of_the_queue():
    reader = make_reader(write_mcap(BytesIO(), topics=("/a",), messages_per_topic=6))
    laggard = reader.subscribe(
        "/a", max_messages=2, policy=BackpressurePolicy.DROP_LATEST
    )
    reader.run()

    kept = drain(laggard, expected=2)
    assert [m.message.log_time for m in kept] == [1, 2]
    assert laggard.dropped_message_count == 4
    assert laggard.gaps[0].count == 4


def test_error_policy_closes_only_the_overflowing_subscription():
    reader = make_reader(write_mcap(BytesIO(), messages_per_topic=4))
    failing = reader.subscribe(
        "/a", max_messages=2, policy=BackpressurePolicy.ERROR
    )
    other = reader.subscribe("/b", max_messages=None)
    reader.run()

    with pytest.raises(BackpressureError):
        next(failing)
    # The terminal error is sticky rather than turning into a silent recovery.
    with pytest.raises(BackpressureError):
        next(failing)
    assert len(drain(other, expected=4)) == 4


def test_cancel_unblocks_a_lossless_producer_and_frees_buffered_chunks():
    reader = make_reader(
        write_mcap(BytesIO(), topics=("/a",), messages_per_topic=30, chunk_size=64)
    )
    slow = reader.subscribe("/a", max_messages=1)
    thread = reader.run_in_thread()

    assert wait_until(lambda: reader.blocked_on is slow)
    window_ref = weakref.ref(slow._queue[0].window)
    slow.cancel()
    thread.join(5.0)
    assert not thread.is_alive()
    gc.collect()
    # Nothing references the cancelled subscription's chunk anymore.
    assert window_ref() is None
    with pytest.raises(StopIteration):
        next(slow)


def test_cancelling_one_subscription_leaves_the_others_running():
    reader = make_reader(
        write_mcap(BytesIO(), topics=("/a",), messages_per_topic=20, chunk_size=64)
    )
    slow = reader.subscribe("/a", max_messages=1)
    survivor = reader.subscribe("/a", max_messages=None)
    thread = reader.run_in_thread()

    assert wait_until(lambda: reader.blocked_on is slow)
    slow.cancel()
    thread.join(5.0)
    assert not thread.is_alive()

    # The surviving subscription buffered every message independently.
    surviving = drain(survivor, expected=20)
    assert [m.message.log_time for m in surviving] == list(range(1, 21))
    assert survivor.dropped_message_count == 0
    with pytest.raises(StopIteration):
        next(slow)


def test_cancel_is_idempotent_and_supports_context_manager():
    reader = make_reader(write_mcap(BytesIO()))
    with reader.subscribe("/a", max_messages=None) as subscription:
        pass
    reader.run()
    with pytest.raises(StopIteration):
        next(subscription)
    subscription.cancel()  # must not raise


def test_oversize_message_fails_only_matching_subscriptions():
    buffer = BytesIO()
    writer = Writer(buffer, chunk_size=1024)
    writer.start()
    big = writer.register_channel("/big", "raw", schema_id=0)
    small = writer.register_channel("/small", "raw", schema_id=0)
    for index in range(3):
        writer.add_message(
            big, log_time=index + 1, data=b"x" * 200, publish_time=index + 1
        )
        writer.add_message(small, log_time=index + 1, data=b"y", publish_time=index + 1)
    writer.finish()

    reader = make_reader(buffer, max_message_size=50)
    big_sub = reader.subscribe("/big", max_messages=None)
    small_sub = reader.subscribe("/small", max_messages=None)
    reader.run()

    with pytest.raises(OversizeMessageError):
        next(big_sub)
    small_messages = drain(small_sub, expected=3)
    assert len(small_messages) == 3
    assert all(m.channel.topic == "/small" for m in small_messages)


def test_oversize_message_in_unchunked_stream_does_not_hang():
    buffer = BytesIO()
    write_mcap(
        buffer,
        topics=("/a",),
        messages_per_topic=2,
        use_chunking=False,
        payload_size=120,
    )
    reader = make_reader(buffer, max_message_size=50)
    subscription = reader.subscribe("/a", max_messages=None)
    reader.run()
    with pytest.raises(OversizeMessageError):
        next(subscription)


def test_unchunked_stream_is_supported():
    reader = make_reader(
        write_mcap(BytesIO(), use_chunking=False),
    )
    subscription = reader.subscribe(None, max_messages=None)
    reader.run()
    messages = drain(subscription, expected=10)
    assert len(messages) == 10
    assert all(m.message.data for m in messages)


def test_reads_from_a_non_seekable_pipe_in_one_pass():
    payload = write_mcap(BytesIO()).getvalue()
    read_fd, write_fd = os.pipe()

    def feed():
        with os.fdopen(write_fd, "wb") as write_end:
            write_end.write(payload)

    feeder = threading.Thread(target=feed)
    feeder.start()
    with os.fdopen(read_fd, "rb") as read_end:
        reader = SharedStreamReader(read_end)
        subscription = reader.subscribe(None, max_messages=None)
        thread = reader.run_in_thread()
        messages = drain(subscription, expected=10)
        thread.join(5.0)
        assert not thread.is_alive()
    feeder.join(5.0)
    assert len(messages) == 10


def test_stream_error_is_forwarded_to_every_subscription():
    payload = write_mcap(BytesIO()).getvalue()
    reader = make_reader(BytesIO(payload[: len(payload) - 40]))
    first = reader.subscribe("/a", max_messages=None)
    second = reader.subscribe("/b", max_messages=None)
    reader.run()
    with pytest.raises(Exception):
        drain(first)
    with pytest.raises(Exception):
        drain(second)


def test_subscribe_after_start_is_rejected():
    reader = make_reader(write_mcap(BytesIO()))
    reader.subscribe("/a")
    reader.run_in_thread().join(5.0)
    with pytest.raises(RuntimeError):
        reader.subscribe("/b")


def test_run_cannot_be_called_twice():
    reader = make_reader(write_mcap(BytesIO()))
    reader.run()
    with pytest.raises(RuntimeError):
        reader.run()


def test_gap_opens_anew_after_the_consumer_catches_up():
    from mcap.records import Channel as ChannelRecord
    from mcap.records import Message as MessageRecord

    reader = make_reader(write_mcap(BytesIO()))
    subscription = reader.subscribe(
        "/a", max_messages=2, policy=BackpressurePolicy.DROP_OLDEST
    )
    channel = ChannelRecord(1, "/a", "raw", {}, 0)

    def message(log_time):
        return MessageRecord(1, log_time, b"x", log_time, 0)

    # Fill the buffer, then overflow twice: evicted heads 1 and 2 form one gap run.
    for log_time in range(1, 5):
        subscription._offer(channel, message(log_time), None)
    assert [m.message.log_time for m in subscription._queue] == [3, 4]

    # The consumer catches up, which closes the open gap run.
    assert next(subscription).message.log_time == 3
    assert next(subscription).message.log_time == 4

    # A later overflow evicts message 5 and opens a distinct gap.
    subscription._offer(channel, message(5), None)
    subscription._offer(channel, message(6), None)
    subscription._offer(channel, message(7), None)
    assert [m.message.log_time for m in subscription._queue] == [6, 7]

    assert len(subscription.gaps) == 2
    first_gap, second_gap = subscription.gaps
    assert (first_gap.count, first_gap.first_log_time, first_gap.last_log_time) == (
        2,
        1,
        2,
    )
    assert (second_gap.count, second_gap.first_log_time) == (1, 5)
    assert subscription.dropped_message_count == 3


def test_none_compression_and_lz4_share_the_same_path():
    for compression in (CompressionType.NONE, CompressionType.LZ4):
        reader = make_reader(write_mcap(BytesIO(), compression=compression))
        subscription = reader.subscribe("/a", max_messages=None)
        reader.run()
        assert len(drain(subscription, expected=5)) == 5


def test_close_unblocks_a_blocked_producer_and_ends_subscriptions():
    reader = make_reader(
        write_mcap(BytesIO(), topics=("/a",), messages_per_topic=20, chunk_size=64)
    )
    slow = reader.subscribe("/a", max_messages=1)
    thread = reader.run_in_thread()
    assert wait_until(lambda: reader.blocked_on is slow)

    reader.close()
    thread.join(5.0)
    assert not thread.is_alive()
    with pytest.raises(StopIteration):
        next(slow)
    with pytest.raises(RuntimeError):
        reader.subscribe("/a")


def test_context_manager_closes_a_stream_it_opened(tmp_path):
    path = tmp_path / "shared.mcap"
    with open(path, "wb") as handle:
        write_mcap(handle)

    with SharedStreamReader(str(path)) as reader:
        subscription = reader.subscribe("/a", max_messages=None)
        thread = reader.run_in_thread()
        messages = drain(subscription, expected=5)
        thread.join(5.0)
        assert not thread.is_alive()
        assert len(messages) == 5

    assert reader._raw_stream.closed
    with pytest.raises(RuntimeError):
        reader.run()
