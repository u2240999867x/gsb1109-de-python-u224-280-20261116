"""A single sequential MCAP scan shared by several independent topic subscriptions.

A non-rewindable stream (a pipe or socket, say) cannot be read once per consumer. This
module reads it once and fans the records out: schemas and channels are tracked
centrally, and every message that matches a subscription is queued into that
subscription's own bounded queue. Each compressed chunk is decompressed exactly once and
the decompressed bytes are shared through a :class:`ChunkWindow`; message payloads are
read-only memoryview slices of those bytes, so nothing is copied or decompressed twice.

Every subscription owns its message cursor and its cancellation. A slow subscription is
handled per subscription, according to the policy given at subscribe time, and never
stalls the others unless it was opened lossless, in which case the *producer* blocks at a
documented back-pressure boundary (see :attr:`SharedStreamReader.blocked_on`) rather than
silently dropping data and pretending every consumer kept up.

A chunk window stays alive for as long as any queued or delivered message references it
and is reclaimed by normal object lifetime once the last reference drops; chunks are
never recycled while still referenced.
"""

import threading
import time
from dataclasses import dataclass
from enum import Enum, auto
from io import BufferedReader, BytesIO, RawIOBase
from typing import (
    IO,
    Any,
    Dict,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Set,
    Tuple,
    Union,
    cast,
)

from .data_stream import ReadDataStream
from .exceptions import EndOfFile, McapError
from .records import (
    MESSAGE_FIELDS,
    MESSAGE_FIELDS_SIZE,
    Channel,
    Chunk,
    McapRecord,
    Message,
    Schema,
)
from .stream_reader import (
    _OPCODE_CHANNEL,
    _OPCODE_MESSAGE,
    _OPCODE_SCHEMA,
    _RECORD_PREFIX,
    _RECORD_PREFIX_SIZE,
    StreamReader,
    _get_chunk_data,
)

__all__ = [
    "BackpressurePolicy",
    "BackpressureError",
    "MessageGap",
    "FanoutMessage",
    "OversizeMessageError",
    "SharedStreamReader",
    "Subscription",
    "ChunkWindow",
]


class BackpressurePolicy(Enum):
    """What happens when a subscription's bounded queue is full.

    BLOCK
        The producer waits for that subscription to drain. Because the stream is read
        once and sequentially, every other subscription is held back as well while the
        wait lasts; :attr:`SharedStreamReader.blocked_on` reports which subscription owns
        the wait. This is the lossless choice: nothing past the stalled message is
        treated as consumed.
    DROP_OLDEST
        The oldest buffered message is discarded and a :class:`MessageGap` is recorded,
        then the new message is enqueued. Only this subscription is affected.
    DROP_LATEST
        The new message is discarded and a :class:`MessageGap` is recorded. Only this
        subscription is affected.
    ERROR
        That subscription is closed with a :class:`BackpressureError`; every other
        subscription keeps reading.
    """

    BLOCK = auto()
    DROP_OLDEST = auto()
    DROP_LATEST = auto()
    ERROR = auto()


class BackpressureError(McapError):
    """Raised inside a subscription's iterator once its queue overflowed under the
    :attr:`BackpressurePolicy.ERROR` policy."""


class OversizeMessageError(McapError):
    """Raised to a subscription when a matching message exceeds the reader's
    ``max_message_size``. Failing the affected subscriptions explicitly rather than
    buffering an unbounded message keeps one oversized record from permanently wedging
    every lossless waiter."""


@dataclass
class MessageGap:
    """A run of messages a subscription did not receive because its buffer overflowed."""

    count: int
    first_log_time: int
    last_log_time: int
    first_channel_id: int
    last_channel_id: int

    def extend(self, message: Message) -> None:
        self.count += 1
        self.last_log_time = message.log_time
        self.last_channel_id = message.channel_id


class FanoutMessage(NamedTuple):
    """A message handed to a subscriber, with the records needed to interpret it.

    ``message.data`` is a read-only memoryview over the shared chunk buffer for chunked
    messages (zero copy); pass it to ``bytes()`` if an owning copy is needed."""

    schema: Optional[Schema]
    channel: Channel
    message: Message


@dataclass
class ChunkWindow:
    """One chunk's decompressed bytes, shared by every subscription the chunk matched.

    Queued messages hold memoryview slices of :attr:`data` and reference this window, so
    the buffer is reclaimed only after the last referencing message is consumed,
    cancelled, or dropped. The sequence number identifies the chunk in stream order.
    """

    sequence: int
    data: bytes


class _QueuedMessage:
    __slots__ = ("channel", "schema_id", "message", "window")

    def __init__(
        self,
        channel: Channel,
        message: Message,
        window: Optional[ChunkWindow],
    ):
        self.channel = channel
        self.schema_id = channel.schema_id
        self.message = message
        # Referencing the window keeps the chunk's decompressed bytes alive until this
        # queue entry is consumed, dropped, or cancelled.
        self.window = window


class Subscription:
    """An independent cursor over one shared stream, filtered to a set of topics.

    Iterate it to receive :class:`FanoutMessage` values in stream order; overflow gaps
    are available from :attr:`gaps`. Each subscription has its own bounded buffer, so one
    subscriber's pace never advances or rewinds another's cursor, and cancelling one
    never disturbs the others. Use :meth:`cancel` or the context-manager form to detach.
    """

    def __init__(
        self,
        reader: "SharedStreamReader",
        topics: Optional[Set[str]],
        max_messages: Optional[int],
        policy: BackpressurePolicy,
    ):
        self._reader = reader
        self._topics = topics
        self._max_messages = max_messages
        self._policy = policy
        self._condition = threading.Condition()
        self._queue: List[_QueuedMessage] = []
        self._cancelled = False
        self._closed = False
        self._terminal_error: Optional[BaseException] = None
        self._gaps: List[MessageGap] = []
        self._open_gap: Optional[MessageGap] = None
        self._dropped = 0
        self._blocked_ns = 0

    @property
    def topics(self) -> Optional[Set[str]]:
        return self._topics

    @property
    def policy(self) -> BackpressurePolicy:
        return self._policy

    @property
    def buffered_count(self) -> int:
        """Messages currently held in this subscription's bounded buffer."""
        with self._condition:
            return len(self._queue)

    @property
    def dropped_message_count(self) -> int:
        """Messages this subscription has lost to buffer overflow."""
        with self._condition:
            return self._dropped

    @property
    def blocked_nanoseconds(self) -> int:
        """Time the producer spent blocked waiting for this subscription to drain."""
        with self._condition:
            return self._blocked_ns

    @property
    def gaps(self) -> List[MessageGap]:
        """Snapshot of the overflow gaps recorded so far."""
        with self._condition:
            return list(self._gaps)

    def cancel(self) -> None:
        """Detaches this subscription. Buffered messages are discarded, releasing the
        chunk windows they alone referenced. A cancelled iterator ends at once."""
        self._reader._remove_subscription(self)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.cancel()

    def __iter__(self) -> Iterator[FanoutMessage]:
        return self

    def __next__(self) -> FanoutMessage:
        with self._condition:
            while not self._queue:
                if self._terminal_error is not None:
                    raise self._terminal_error
                if self._closed or self._cancelled:
                    raise StopIteration
                self._condition.wait()
            item = self._queue.pop(0)
            schema_id = item.schema_id
            channel = item.channel
            message = item.message
            # Taking a message ends any contiguous overflow run; a later overflow opens
            # a new gap rather than extending one the consumer has moved past.
            self._open_gap = None
            # Wake a producer blocked on a full lossless buffer.
            self._condition.notify()
        # Resolve the schema outside the subscription lock so the lock ordering stays
        # reader-lock -> subscription-lock, never the reverse.
        schema = self._reader._schema_for(schema_id)
        return FanoutMessage(schema, channel, message)

    def _matches(self, channel: Channel) -> bool:
        return self._topics is None or channel.topic in self._topics

    def _offer(
        self,
        channel: Channel,
        message: Message,
        window: Optional[ChunkWindow],
    ) -> None:
        """Enqueues one message, applying the configured back-pressure policy.

        Only this subscription's lock is held, so cancellation can always make progress,
        even while the producer is blocked inside this method on a lossless queue.
        """
        with self._condition:
            if self._cancelled or self._closed:
                return
            item = _QueuedMessage(channel, message, window)
            if self._has_room_locked():
                self._queue.append(item)
                self._condition.notify()
                return
            self._handle_overflow_locked(message, item)

    def _fail(self, error: BaseException) -> None:
        """Closes only this subscription with an error; other subscriptions continue."""
        with self._condition:
            if self._cancelled or self._closed:
                return
            self._queue.clear()
            self._cancelled = True
            self._closed = True
            self._terminal_error = error
            self._condition.notify_all()

    def _has_room_locked(self) -> bool:
        return self._max_messages is None or len(self._queue) < self._max_messages

    def _handle_overflow_locked(
        self,
        message: Message,
        item: _QueuedMessage,
    ) -> None:
        if self._policy is BackpressurePolicy.BLOCK:
            start = time.monotonic_ns()
            self._condition.wait_for(
                lambda: self._cancelled or self._has_room_locked()
            )
            self._blocked_ns += time.monotonic_ns() - start
            if self._cancelled or self._closed:
                return
            self._queue.append(item)
            self._condition.notify()
            return

        self._dropped += 1
        # DROP_OLDEST discards the queued head; DROP_LATEST and ERROR discard the
        # incoming message. Record whichever one is actually lost.
        dropped_message = (
            self._queue[0].message
            if self._policy is BackpressurePolicy.DROP_OLDEST
            else message
        )
        gap = self._open_gap
        if gap is None:
            gap = MessageGap(
                count=1,
                first_log_time=dropped_message.log_time,
                last_log_time=dropped_message.log_time,
                first_channel_id=dropped_message.channel_id,
                last_channel_id=dropped_message.channel_id,
            )
            self._open_gap = gap
            self._gaps.append(gap)
        else:
            gap.extend(dropped_message)

        if self._policy is BackpressurePolicy.ERROR:
            self._fail_locked(
                BackpressureError(
                    f"subscription buffer for topics {sorted(self._topics or [])} "
                    f"overflowed ({self._max_messages} messages)"
                )
            )
            return

        if self._policy is BackpressurePolicy.DROP_LATEST:
            self._condition.notify()
            return

        # DROP_OLDEST: evict the head to make room, then append the new message.
        self._queue.pop(0)
        self._queue.append(item)
        self._condition.notify()

    def _fail_locked(self, error: BaseException) -> None:
        self._queue.clear()
        self._cancelled = True
        self._closed = True
        self._terminal_error = error
        self._condition.notify_all()

    def _mark_finished_locked(self, error: Optional[BaseException]) -> None:
        self._closed = True
        if error is not None:
            self._terminal_error = error
        self._condition.notify_all()

    def _cancel_locked(self) -> None:
        self._queue.clear()
        self._cancelled = True
        self._condition.notify_all()


class SharedStreamReader:
    """Reads one non-rewindable MCAP stream once and fans it out to subscriptions.

    Create subscriptions with :meth:`subscribe` *before* starting the producer with
    :meth:`run` or :meth:`run_in_thread`. The producer walks the stream sequentially,
    decompresses each chunk exactly once, and routes each matching message to the
    subscription's bounded queue. Subscriptions iterate independently with their own
    cursors and cancellation.

    :param input: a filename or binary file-like object holding one MCAP stream.
    :param validate_crcs: if ``True``, chunk CRCs are validated when decompressing.
    :param record_size_limit: upper bound on record sizes accepted from the stream,
        defaulting to 4 GiB.
    :param max_message_size: hard upper bound on a single message's uncompressed size,
        defaulting to 64 MiB. A matching message that exceeds it fails only the
        subscriptions whose topics it belongs to, with :class:`OversizeMessageError`, and
        the producer keeps reading; ``None`` removes the bound.
    """

    def __init__(
        self,
        input: Union[str, BytesIO, RawIOBase, BufferedReader, IO[bytes]],
        validate_crcs: bool = False,
        record_size_limit: Optional[int] = 4 * 2**30,
        max_message_size: Optional[int] = 64 * 2**20,
    ):
        owns_stream = False
        if isinstance(input, str):
            raw_stream: IO[bytes] = open(input, "rb")
            owns_stream = True
        elif isinstance(input, RawIOBase):
            raw_stream = BufferedReader(input)
        else:
            raw_stream = input
        self._stream_reader = StreamReader(
            raw_stream,
            validate_crcs=validate_crcs,
            emit_chunks=True,
            record_size_limit=record_size_limit,
        )
        self._raw_stream = raw_stream
        self._owns_stream = owns_stream
        self._validate_crcs = validate_crcs
        self._max_message_size = max_message_size
        self._lock = threading.Lock()
        self._subscriptions: List[Subscription] = []
        self._schemas: Dict[int, Schema] = {}
        self._channels: Dict[int, Channel] = {}
        self._chunk_sequence = 0
        self._started = False
        self._closed = False
        self._thread: Optional[threading.Thread] = None
        self._blocked_on: Optional[Subscription] = None

    @property
    def blocked_on(self) -> Optional[Subscription]:
        """The subscription currently stalling a lossless producer, or ``None``.

        This is the explicit blocking boundary: while set, the producer has not advanced
        past the stalled message and no subscriber has implicitly consumed it."""
        with self._lock:
            return self._blocked_on

    def subscribe(
        self,
        topics: Union[str, List[str], Tuple[str, ...], None] = None,
        *,
        max_messages: Optional[int] = 1000,
        policy: BackpressurePolicy = BackpressurePolicy.BLOCK,
    ) -> Subscription:
        """Adds a subscription filtered to ``topics`` (``None`` means every topic).

        :param max_messages: per-subscription buffer budget measured in messages.
            ``None`` makes the subscription unbounded; prefer an explicit bound so a slow
            consumer cannot grow memory without limit.
        :param policy: how an overflow of this subscription's buffer is handled. Drop
            and error policies affect this subscription only.
        """
        with self._lock:
            if self._closed:
                raise RuntimeError("cannot subscribe() to a closed SharedStreamReader")
            if self._started:
                raise RuntimeError(
                    "subscribe() after run(): a non-rewindable stream cannot replay "
                    "earlier records for a late subscriber"
                )
            if isinstance(topics, str):
                topic_set: Optional[Set[str]] = {topics}
            elif topics is None:
                topic_set = None
            else:
                topic_set = set(topics)
            subscription = Subscription(self, topic_set, max_messages, policy)
            self._subscriptions.append(subscription)
            return subscription

    def run_in_thread(self) -> threading.Thread:
        """Starts :meth:`run` on a daemon thread and returns it."""
        thread = threading.Thread(target=self.run, name="mcap-shared-stream-reader")
        thread.daemon = True
        self._thread = thread
        thread.start()
        return thread

    def run(self) -> None:
        """Drives the stream to completion. Blocks the calling thread; use
        :meth:`run_in_thread` to run it alongside consumers. Stream-level errors (a
        truncated file, bad magic, or a record over the size limit) do not escape here:
        they are forwarded to every subscription and raised from its iterator."""
        with self._lock:
            if self._closed:
                raise RuntimeError("run() called on a closed SharedStreamReader")
            if self._started:
                raise RuntimeError("run() called more than once")
            self._started = True
            subscriptions = list(self._subscriptions)
        error: Optional[BaseException] = None
        try:
            for record in self._stream_reader.records:
                self._dispatch(record)
        except BaseException as exc:  # forwarded to subscribers
            error = exc
        finally:
            self._finish(subscriptions, error)

    def close(self) -> None:
        """Cancels every subscription and closes a stream this reader opened itself.

        If the producer is running on its own thread, this waits for it to unwind first,
        so the underlying stream is never closed while still being read."""
        with self._lock:
            subscriptions = list(self._subscriptions)
        for subscription in subscriptions:
            subscription.cancel()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        if self._owns_stream:
            self._raw_stream.close()
        with self._lock:
            self._closed = True

    def __enter__(self) -> "SharedStreamReader":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _dispatch(self, record: McapRecord) -> None:
        if isinstance(record, Schema):
            with self._lock:
                self._schemas[record.id] = record
        elif isinstance(record, Channel):
            with self._lock:
                self._channels[record.id] = record
        elif isinstance(record, Chunk):
            self._dispatch_chunk(record)
        elif isinstance(record, Message):
            channel = self._require_channel(record.channel_id)
            if self._within_message_size(channel.id, record.data):
                self._route_message(record, None, channel)

    def _dispatch_chunk(self, chunk: Chunk) -> None:
        # The single decompression point shared by every subscription.
        data = _get_chunk_data(chunk, validate_crc=self._validate_crcs)
        window = ChunkWindow(sequence=self._chunk_sequence, data=data)
        self._chunk_sequence += 1
        pos = 0
        end = len(data)
        while pos < end:
            if end - pos < _RECORD_PREFIX_SIZE:
                raise EndOfFile()
            opcode, length = _RECORD_PREFIX.unpack_from(data, pos)
            pos += _RECORD_PREFIX_SIZE
            record_end = pos + length
            if record_end > end:
                raise EndOfFile()
            if opcode == _OPCODE_MESSAGE:
                channel_id, sequence, log_time, publish_time = (
                    MESSAGE_FIELDS.unpack_from(data, pos)
                )
                payload = memoryview(data)[pos + MESSAGE_FIELDS_SIZE : record_end]
                channel = self._require_channel(channel_id)
                if not self._within_message_size(channel_id, payload):
                    # Matching subscriptions have already failed; do not route the
                    # oversized record. The producer keeps scanning the chunk.
                    pos = record_end
                    continue
                message = Message(
                    channel_id=channel_id,
                    log_time=log_time,
                    # Message.data is bytes at the type level; the runtime view shares
                    # the chunk buffer and is read-only because the source is bytes.
                    data=cast(Any, payload),
                    publish_time=publish_time,
                    sequence=sequence,
                )
                self._route_message(message, window, channel)
            elif opcode == _OPCODE_CHANNEL:
                channel = Channel.read(ReadDataStream(BytesIO(data[pos:record_end])))
                with self._lock:
                    self._channels[channel.id] = channel
            elif opcode == _OPCODE_SCHEMA:
                schema = Schema.read(ReadDataStream(BytesIO(data[pos:record_end])))
                with self._lock:
                    self._schemas[schema.id] = schema
            # Unknown chunk record types are skipped.
            pos = record_end

    def _route_message(
        self,
        message: Message,
        window: Optional[ChunkWindow],
        channel: Channel,
    ) -> None:
        with self._lock:
            # Drop subscriptions that failed or were cancelled on another thread.
            self._subscriptions = [
                subscription
                for subscription in self._subscriptions
                if not subscription._cancelled
            ]
            targets = [
                subscription
                for subscription in self._subscriptions
                if subscription._matches(channel)
            ]
        # The reader lock is not held across delivery: a blocking subscription can
        # always be cancelled, and blocked_on marks the explicit wait boundary.
        for subscription in targets:
            blocks = subscription.policy is BackpressurePolicy.BLOCK
            if blocks:
                with self._lock:
                    self._blocked_on = subscription
            try:
                subscription._offer(channel, message, window)
            finally:
                if blocks:
                    with self._lock:
                        if self._blocked_on is subscription:
                            self._blocked_on = None

    def _within_message_size(self, channel_id: int, data: Any) -> bool:
        """Returns False (after failing the matching subscriptions) when a message
        exceeds the hard size limit; True when it is safe to route."""
        if self._max_message_size is None or len(data) <= self._max_message_size:
            return True
        error = OversizeMessageError(
            f"message on channel {channel_id} is {len(data)} bytes, exceeding "
            f"max_message_size {self._max_message_size}"
        )
        with self._lock:
            channel = self._channels.get(channel_id)
            victims = [
                subscription
                for subscription in self._subscriptions
                if not subscription._cancelled
                and channel is not None
                and subscription._matches(channel)
            ]
        # Hard safety limit: fail only the subscriptions this message belongs to.
        for victim in victims:
            victim._fail(error)
        return False

    def _require_channel(self, channel_id: int) -> Channel:
        with self._lock:
            channel = self._channels.get(channel_id)
        if channel is None:
            raise McapError(f"no channel record found with id {channel_id}")
        return channel

    def _schema_for(self, schema_id: int) -> Optional[Schema]:
        if schema_id == 0:
            return None
        with self._lock:
            return self._schemas.get(schema_id)

    def _remove_subscription(self, subscription: Subscription) -> None:
        # Reader lock first, subscription lock second: the only allowed lock ordering.
        with self._lock:
            if subscription in self._subscriptions:
                self._subscriptions.remove(subscription)
        with subscription._condition:
            if not subscription._cancelled and not subscription._closed:
                subscription._cancel_locked()
            elif not subscription._cancelled:
                subscription._cancelled = True

    def _finish(
        self,
        subscriptions: List[Subscription],
        error: Optional[BaseException],
    ) -> None:
        with self._lock:
            self._blocked_on = None
        for subscription in subscriptions:
            with subscription._condition:
                if not subscription._cancelled:
                    subscription._mark_finished_locked(error)
        if self._owns_stream:
            self._raw_stream.close()
