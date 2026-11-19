"""High-level classes for reading content out of MCAP data sources."""

import io
import warnings
from abc import ABC, abstractmethod
from typing import (
    IO,
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Tuple,
    Union,
)

from ._message_queue import KeyedMessage, LogTimeOrderQueue
from .data_stream import ReadDataStream, RecordBuilder
from .decoder import DecoderFactory
from .exceptions import DecoderNotFoundError, McapError
from .records import (
    Attachment,
    AttachmentIndex,
    Channel,
    Chunk,
    ChunkIndex,
    Footer,
    Header,
    McapRecord,
    Message,
    Metadata,
    MetadataIndex,
    Schema,
    Statistics,
)
from .stream_reader import (
    MAGIC_SIZE,
    StreamReader,
    _get_chunk_data,
    _parse_chunk_records,
    read_magic,
)
from .summary import Summary
from .time_window import (
    MessageOrder,
    MessageQuery,
    TimeRange,
    TimeWindow,
    coerce_query,
)


def _resolve_time_range(
    start_time: Optional[int],
    end_time: Optional[int],
    starting_at: Optional[int],
    starting_after: Optional[int],
    ending_at: Optional[int],
    ending_before: Optional[int],
) -> Tuple[Optional[int], Optional[int]]:
    """Resolve the explicit and deprecated time bounds to an inclusive-start /
    exclusive-end pair, warning on deprecated names and rejecting conflicting bounds
    and crossed ranges. A start bound past every possible end bound (for example
    ``starting_after`` at the maximum timestamp) is an empty query, not an error,
    unless an end bound is also given below it.
    """
    if start_time is not None:
        warnings.warn(
            "start_time is deprecated; use starting_at (inclusive) or starting_after"
            " (exclusive) instead",
            DeprecationWarning,
            stacklevel=3,
        )
    if end_time is not None:
        warnings.warn(
            "end_time is deprecated; use ending_before (exclusive) or ending_at"
            " (inclusive) instead",
            DeprecationWarning,
            stacklevel=3,
        )
    if (
        sum(bound is not None for bound in (start_time, starting_at, starting_after))
        > 1
    ):
        raise ValueError(
            "provide at most one of start_time, starting_at, starting_after"
        )
    if sum(bound is not None for bound in (end_time, ending_at, ending_before)) > 1:
        raise ValueError("provide at most one of end_time, ending_at, ending_before")
    start = starting_at if starting_at is not None else start_time
    if starting_after is not None:
        start = starting_after + 1
    end = ending_before if ending_before is not None else end_time
    if ending_at is not None:
        end = ending_at + 1
    if start is not None and end is not None and start > end:
        raise ValueError("end time cannot come before start time")
    return start, end


def _get_record_size(record: McapRecord):
    """utility for counting the number of bytes a given record occupies in an MCAP."""
    rb = RecordBuilder()
    record.write(rb)
    return rb.count


FOOTER_SIZE = _get_record_size(Footer(0, 0, 0))


def _resolve_publish_bounds(
    starting_at: Optional[int],
    starting_after: Optional[int],
    ending_at: Optional[int],
    ending_before: Optional[int],
) -> Tuple[Optional[int], Optional[int]]:
    """Resolve publish-time bounds to the half-open pair ``[start, end)``.

    The publish clock is kept as its original integer nanoseconds. Bounds are
    validated by :class:`~mcap.time_window.TimeRange`, which normalizes integral
    types to exact ints and rejects floats and bools.
    """
    if sum(bound is not None for bound in (starting_at, starting_after)) > 1:
        raise ValueError(
            "provide at most one of publish_starting_at and"
            " publish_starting_after"
        )
    if sum(bound is not None for bound in (ending_at, ending_before)) > 1:
        raise ValueError(
            "provide at most one of publish_ending_at and publish_ending_before"
        )
    if starting_at is not None:
        lower = TimeRange.starting_at(starting_at)
    elif starting_after is not None:
        lower = TimeRange.starting_after(starting_after)
    else:
        lower = TimeRange()
    if ending_before is not None:
        upper = TimeRange.ending_before(ending_before)
    elif ending_at is not None:
        upper = TimeRange.ending_at(ending_at)
    else:
        upper = TimeRange()
    try:
        combined = TimeRange(lower.start, upper.end)
    except ValueError as error:
        raise ValueError("publish end time cannot come before start time") from error
    return combined.start, combined.end


def _resolve_query(
    query: Optional[Union[TimeRange, TimeWindow, MessageQuery]],
    start: Optional[int],
    end: Optional[int],
    publish_start: Optional[int] = None,
    publish_end: Optional[int] = None,
) -> MessageQuery:
    """Fold a caller's window and legacy bounds into one normalized query.

    A bare ``TimeRange`` passed as ``query`` constrains ``log_time``, matching
    the historical meaning of the bound arguments. Explicit ``starting_*`` /
    ``ending_*`` bounds are an additional log-time constraint, so they
    intersect with any supplied query rather than replacing it.
    """
    resolved = coerce_query(query)
    if start is not None or end is not None:
        resolved = resolved & TimeWindow(log_time=TimeRange(start, end))
    if publish_start is not None or publish_end is not None:
        resolved = resolved & TimeWindow(
            publish_time=TimeRange(publish_start, publish_end)
        )
    return resolved


def _resolve_order(
    log_time_order: bool,
    reverse: bool,
    order: Optional[Union[MessageOrder, str]],
) -> Tuple[MessageOrder, bool]:
    """Resolve the historical flags and the explicit ``order`` argument.

    ``order`` wins when given. Otherwise ``log_time_order=False`` selects file
    order and the default selects log-time order.
    """
    if order is None:
        resolved = MessageOrder.LOG_TIME if log_time_order else MessageOrder.FILE_ORDER
    else:
        resolved = MessageOrder.coerce(order)
    if reverse and resolved is MessageOrder.FILE_ORDER:
        raise ValueError("reverse is only valid when ordering by a timestamp")
    return resolved, reverse


def _query_may_match_chunk(
    query: MessageQuery, message_start_time: int, message_end_time: int
) -> bool:
    """Whether a chunk index's proven time span leaves any window possible.

    A chunk index proves only the ``log_time`` span of the messages it indexes.
    ``publish_time`` is independent of it and may run backwards, so a window
    with an unbounded log-time condition (for example one that constrains only
    ``publish_time``) intersects every span and keeps every chunk a candidate.
    The log-time index therefore never prunes a message that a publish-time
    condition alone selects.
    """
    for window in query.windows:
        if window.is_empty:
            continue
        if window.log_time.intersects(message_start_time, message_end_time):
            return True
    return False


def _chunks_matching_query(
    summary: Summary,
    topics: Optional[Iterable[str]],
    query: MessageQuery,
) -> List[ChunkIndex]:
    """Return candidate ChunkIndex records for a topic and time-window query.

    The result is a list of distinct chunk indexes, so a chunk shared by several
    windows of a disjunctive query is read once.
    """
    out: List[ChunkIndex] = []
    for chunk_index in summary.chunk_indexes:
        if not _query_may_match_chunk(
            query,
            chunk_index.message_start_time,
            chunk_index.message_end_time,
        ):
            continue
        if topics is None:
            out.append(chunk_index)
            continue
        if len(chunk_index.message_index_offsets) == 0:
            # Without message indexes, topic membership is unknown until the chunk is scanned.
            out.append(chunk_index)
            continue
        for channel_id in chunk_index.message_index_offsets.keys():
            channel = summary.channels.get(channel_id)
            if channel is not None and channel.topic in topics:
                out.append(chunk_index)
                break
    # Return chunks in file order, so a sequential scan and an indexed scan visit
    # their messages in the same order before any timestamp sort is applied.
    out.sort(key=lambda chunk_index: chunk_index.chunk_start_offset)
    return out


def _read_summary_from_stream_reader(stream_reader: StreamReader) -> Optional[Summary]:
    """read summary records from an MCAP stream reader, collecting them into a Summary."""
    summary = Summary()
    for record in stream_reader.records:
        if isinstance(record, Statistics):
            summary.statistics = record
        elif isinstance(record, Schema):
            summary.schemas[record.id] = record
        elif isinstance(record, Channel):
            summary.channels[record.id] = record
        elif isinstance(record, AttachmentIndex):
            summary.attachment_indexes.append(record)
        elif isinstance(record, ChunkIndex):
            summary.chunk_indexes.append(record)
        elif isinstance(record, MetadataIndex):
            summary.metadata_indexes.append(record)
        elif isinstance(record, Footer):
            # There is no summary!
            if record.summary_start == 0:
                return None
            else:
                return summary
    return summary


class DecodedMessageTuple(NamedTuple):
    """Yielded from every iteration of :py:meth:`~mcap.reader.McapReader.iter_decoded_messages`."""

    schema: Optional[Schema]
    channel: Channel
    message: Message
    decoded_message: Any


class McapReader(ABC):
    """Reads data out of an MCAP file, using the summary section where available to efficiently
    read only the parts of the file that are needed.

    :param decoder_factories: An iterable of :py:class:`~mcap.decoder.DecoderFactory`
        instances which can provide decoding functionality to
        :py:meth:`~mcap.reader.McapReader.iter_decoded_messages`.
    """

    def __init__(
        self,
        decoder_factories: Iterable[DecoderFactory] = (),
    ):
        self._decoder_factories = decoder_factories
        self._decoders: dict[int, Callable[[bytes], Any]] = {}

    @abstractmethod
    def iter_messages(
        self,
        topics: Union[Iterable[str], str, None] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        log_time_order: bool = True,
        reverse: bool = False,
        *,
        starting_at: Optional[int] = None,
        starting_after: Optional[int] = None,
        ending_at: Optional[int] = None,
        ending_before: Optional[int] = None,
        query: Optional[Union[TimeRange, TimeWindow, MessageQuery]] = None,
        publish_starting_at: Optional[int] = None,
        publish_starting_after: Optional[int] = None,
        publish_ending_at: Optional[int] = None,
        publish_ending_before: Optional[int] = None,
        order: Optional[Union[MessageOrder, str]] = None,
    ) -> Iterator[Tuple[Optional[Schema], Channel, Message]]:
        """iterates through the messages in an MCAP.

        :param topics: if not None, only messages from these topics will be returned.
        :param start_time: deprecated alias for ``starting_at``; use ``starting_at`` or
            ``starting_after`` instead.
        :param end_time: deprecated alias for ``ending_before``; use ``ending_before`` or
            ``ending_at`` instead.
        :param log_time_order: if True, messages will be yielded in ascending log time order. If
            False, messages will be yielded in the order they appear in the MCAP file.
        :param reverse: if True, the selected order is reversed.
        :param starting_at: an integer nanosecond timestamp. if provided, messages logged before
            this timestamp are not included (inclusive lower bound).
        :param starting_after: an integer nanosecond timestamp. if provided, messages logged at or
            before this timestamp are not included (exclusive lower bound).
        :param ending_at: an integer nanosecond timestamp. if provided, messages logged after this
            timestamp are not included (inclusive upper bound).
        :param ending_before: an integer nanosecond timestamp. if provided, messages logged at or
            after this timestamp are not included (exclusive upper bound).
        :param query: a composable :class:`~mcap.time_window.TimeWindow` or
            :class:`~mcap.time_window.MessageQuery` over ``log_time`` and
            ``publish_time``. A bare :class:`~mcap.time_window.TimeRange` constrains
            ``log_time``. Any explicit bounds intersect with it.
        :param publish_starting_at: inclusive lower bound on ``publish_time``.
        :param publish_starting_after: exclusive lower bound on ``publish_time``.
        :param publish_ending_at: inclusive upper bound on ``publish_time``.
        :param publish_ending_before: exclusive upper bound on ``publish_time``.
        :param order: the output ordering criterion: ``"log_time"`` (default) or
            ``"file_order"``. Takes precedence over ``log_time_order``. The publish
            clock has no ordering criterion; it is a query condition only.

        Every bound is an integer count of nanoseconds, kept as given. At most one
        lower bound per clock (``start_time``, ``starting_at``, ``starting_after``;
        or the ``publish_starting_*`` pair) and one upper bound per clock may be
        provided. The log-time bounds resolve to a half-open range ``[start, end)``;
        ``start > end`` raises ``ValueError`` and ``start == end`` is a valid empty
        range.

        Messages that tie on ``log_time`` are yielded in file order: by chunk start
        offset, then by position within the chunk. The order is total, so the same
        query always yields the same sequence, an indexed read and a sequential read
        agree, and ``reverse=True`` yields exactly the reverse.

        A chunk index only proves the ``log_time`` span of its messages. A query
        that constrains ``publish_time`` but leaves ``log_time`` unbounded therefore
        keeps every chunk a candidate, because the publish clock may run backwards.
        The chunk is then read and its messages are filtered on their actual
        ``publish_time``; the log-time index never prunes a publish-time match.
        """
        raise NotImplementedError()

    def iter_decoded_messages(
        self,
        topics: Union[Iterable[str], str, None] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        log_time_order: bool = True,
        reverse: bool = False,
        *,
        starting_at: Optional[int] = None,
        starting_after: Optional[int] = None,
        ending_at: Optional[int] = None,
        ending_before: Optional[int] = None,
        query: Optional[Union[TimeRange, TimeWindow, MessageQuery]] = None,
        publish_starting_at: Optional[int] = None,
        publish_starting_after: Optional[int] = None,
        publish_ending_at: Optional[int] = None,
        publish_ending_before: Optional[int] = None,
        order: Optional[Union[MessageOrder, str]] = None,
    ) -> Iterator[DecodedMessageTuple]:
        """iterates through messages in an MCAP, decoding their contents.

        Accepts the same arguments as :py:meth:`iter_messages`.
        """
        # Resolve the bounds here rather than in iter_messages so that a deprecation
        # warning is attributed to the caller of this generator, not to the loop below.
        starting_at, ending_before = _resolve_time_range(
            start_time, end_time, starting_at, starting_after, ending_at, ending_before
        )
        publish_starting_at, publish_ending_before = _resolve_publish_bounds(
            publish_starting_at,
            publish_starting_after,
            publish_ending_at,
            publish_ending_before,
        )
        message_iterator = self.iter_messages(
            topics,
            log_time_order=log_time_order,
            reverse=reverse,
            starting_at=starting_at,
            ending_before=ending_before,
            query=query,
            publish_starting_at=publish_starting_at,
            publish_ending_before=publish_ending_before,
            order=order,
        )

        def decoded_message(
            schema: Optional[Schema], channel: Channel, message: Message
        ) -> Any:
            decoder = self._decoders.get(message.channel_id)
            if decoder is not None:
                return decoder(message.data)
            for factory in self._decoder_factories:
                decoder = factory.decoder_for(channel.message_encoding, schema)
                if decoder is not None:
                    self._decoders[message.channel_id] = decoder
                    return decoder(message.data)

            raise DecoderNotFoundError(
                f"no decoder factory supplied for message encoding {channel.message_encoding}, "
                f"schema {schema}"
            )

        for schema, channel, message in message_iterator:
            yield DecodedMessageTuple(
                schema, channel, message, decoded_message(schema, channel, message)
            )

    @abstractmethod
    def get_header(self) -> Header:
        """Reads the Header records from the beginning of the MCAP file."""
        raise NotImplementedError()

    @abstractmethod
    def get_summary(self) -> Optional[Summary]:
        """Reads the (optional) summary section from the MCAP file."""
        raise NotImplementedError()

    @abstractmethod
    def iter_attachments(self) -> Iterator[Attachment]:
        """Iterates through attachment records in the MCAP."""
        raise NotImplementedError()

    @abstractmethod
    def iter_metadata(self) -> Iterator[Metadata]:
        """Iterates through metadata records in the MCAP."""
        raise NotImplementedError()


def make_reader(
    stream: IO[bytes],
    validate_crcs: bool = False,
    decoder_factories: Iterable[DecoderFactory] = (),
) -> McapReader:
    """constructs the appropriate McapReader implementation for this data source."""
    if stream.seekable():
        return SeekingReader(
            stream, validate_crcs=validate_crcs, decoder_factories=decoder_factories
        )
    return NonSeekingReader(
        stream, validate_crcs=validate_crcs, decoder_factories=decoder_factories
    )


class SeekingReader(McapReader):
    """an McapReader for reading out of seekable data sources.

    :param stream: a file-like object for reading the source data from.
    :param validate_crcs: if ``True``, will validate Chunk CRCs for any chunks read. This class
        does not validate the data section CRC in the DataEnd record because it is designed not to
        read the entire data section when reading messages. To read messages while validating the
        data section CRC, use :py:class:`NonSeekingReader`.
    :param decoder_factories: An iterable of :py:class:`~mcap.decoder.DecoderFactory`
        instances which can provide decoding functionality to
        :py:meth:`~mcap.reader.McapReader.iter_decoded_messages`.
    :param record_size_limit: An upper bound to the size of MCAP records that this reader will
        attempt to load in bytes, defaulting to 4 GiB. If this reader encounters a record with a
        greater length, it will throw an :py:class:`~mcap.exceptions.RecordLengthLimitExceeded`
        error.  Setting to ``None`` removes the limit, but can allow corrupted MCAP files to trigger
        a `MemoryError` exception.
    """

    def __init__(
        self,
        stream: IO[bytes],
        validate_crcs: bool = False,
        decoder_factories: Iterable[DecoderFactory] = (),
        record_size_limit: Optional[int] = 4 * 2**30,
    ):
        super().__init__(decoder_factories=decoder_factories)
        read_magic(ReadDataStream(stream, calculate_crc=False))
        self._stream = stream
        self._validate_crcs = validate_crcs
        self._summary: Optional[Summary] = None
        self._record_size_limit = record_size_limit

    def iter_messages(
        self,
        topics: Union[Iterable[str], str, None] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        log_time_order: bool = True,
        reverse: bool = False,
        *,
        starting_at: Optional[int] = None,
        starting_after: Optional[int] = None,
        ending_at: Optional[int] = None,
        ending_before: Optional[int] = None,
        query: Optional[Union[TimeRange, TimeWindow, MessageQuery]] = None,
        publish_starting_at: Optional[int] = None,
        publish_starting_after: Optional[int] = None,
        publish_ending_at: Optional[int] = None,
        publish_ending_before: Optional[int] = None,
        order: Optional[Union[MessageOrder, str]] = None,
    ) -> Iterator[Tuple[Optional[Schema], Channel, Message]]:
        """iterates through the messages in an MCAP.

        See :py:meth:`McapReader.iter_messages` for a description of the arguments.
        """
        starting_at, ending_before = _resolve_time_range(
            start_time, end_time, starting_at, starting_after, ending_at, ending_before
        )
        publish_starting_at, publish_ending_before = _resolve_publish_bounds(
            publish_starting_at,
            publish_starting_after,
            publish_ending_at,
            publish_ending_before,
        )
        if isinstance(topics, str):
            topics = [topics]
        resolved_query = _resolve_query(
            query,
            starting_at,
            ending_before,
            publish_starting_at,
            publish_ending_before,
        )
        resolved_order, reverse = _resolve_order(log_time_order, reverse, order)
        summary = self.get_summary()
        if summary is None or len(summary.chunk_indexes) == 0:
            # No chunk indices available, so there is no index to search for messages.
            # use a non-seeking reader to read linearly through the stream.
            self._stream.seek(0, io.SEEK_SET)
            yield from NonSeekingReader(self._stream).iter_messages(
                topics,
                query=resolved_query,
                order=resolved_order,
                reverse=reverse,
            )
            return

        if resolved_query.is_empty:
            # No message can satisfy the query; skip reading altogether.
            return

        chunk_indexes = _chunks_matching_query(summary, topics, resolved_query)
        if resolved_order is MessageOrder.FILE_ORDER:
            # `held` keeps the previous chunk alive, see _read_chunk_messages.
            for chunk_index in chunk_indexes:
                items, held = self._read_chunk_messages(
                    chunk_index,
                    summary,
                    topics,
                    resolved_query,
                    False,
                )
                for keyed in items:
                    yield keyed[3]
            return

        # Each chunk's messages go into the queue as one run sorted by (log time, chunk
        # offset, record index), and the queue merges the runs of chunks that overlap
        # in time. The messages at the head of the queue that sort before everything
        # else in it come back out as one list, which is a whole run at a time for a
        # file whose chunks do not overlap.
        queue = LogTimeOrderQueue(reverse=reverse)
        for chunk_index in chunk_indexes:
            queue.push(chunk_index)
        # `held` keeps the previous chunk alive, see _read_chunk_messages.
        while queue:
            run = queue.take_leading_run()
            if run is not None:
                for keyed in run:
                    yield keyed[3]
                continue
            item = queue.pop()
            if isinstance(item, ChunkIndex):
                items, held = self._read_chunk_messages(
                    item,
                    summary,
                    topics,
                    resolved_query,
                    reverse,
                )
                queue.push_chunk_messages(items)
            else:
                yield item

    def _read_chunk_messages(
        self,
        chunk_index: ChunkIndex,
        summary: Summary,
        topics: Optional[Iterable[str]],
        query: MessageQuery,
        reverse: bool,
    ) -> Tuple[List[KeyedMessage], bytes]:
        """Reads one chunk and returns its matching messages in file order, each under a
        sort key of (log time, chunk offset, record index), negated when reversed, so
        that a plain tuple sort orders them the way ``iter_messages`` promises.

        The chunk offset and record index make the key total, so messages that share a
        timestamp keep file order and appear stably.

        The chunk's buffer is returned too so the caller can keep it alive until the
        next chunk is read. Freeing it before the next buffer is allocated let glibc's
        allocator return the memory to the system, and every chunk read then faulted
        its buffer back in page by page, which doubled the time of a chunked read."""
        self._stream.seek(chunk_index.chunk_start_offset + 1 + 8, io.SEEK_SET)
        chunk = Chunk.read(ReadDataStream(self._stream))
        data = _get_chunk_data(chunk, validate_crc=self._validate_crcs)
        records = _parse_chunk_records(data)
        channels = summary.channels
        schemas = summary.schemas
        # Per channel: the (schema, channel) pair, or False when the topic is filtered out.
        resolved: Dict[int, Any] = {}
        chunk_offset = chunk_index.chunk_start_offset
        if reverse:
            chunk_offset = -chunk_offset
        out: List[KeyedMessage] = []
        append = out.append
        for index, record in enumerate(records):
            if not isinstance(record, Message):
                continue
            if not query.includes(record):
                continue
            channel_id = record.channel_id
            pair = resolved.get(channel_id)
            if pair is None:
                channel = channels[channel_id]
                if topics is not None and channel.topic not in topics:
                    resolved[channel_id] = False
                    continue
                if channel.schema_id == 0:
                    schema = None
                else:
                    schema = schemas[channel.schema_id]
                pair = resolved[channel_id] = (schema, channel)
            elif pair is False:
                continue
            log_time = record.log_time
            if reverse:
                append((-log_time, chunk_offset, -index, (pair[0], pair[1], record)))
            else:
                append((log_time, chunk_offset, index, (pair[0], pair[1], record)))
        return out, data

    def get_header(self) -> Header:
        """Reads the Header record from the beginning of the MCAP file."""
        self._stream.seek(0)
        header = next(
            StreamReader(
                self._stream,
                skip_magic=False,
                record_size_limit=self._record_size_limit,
            ).records
        )
        if not isinstance(header, Header):
            raise McapError(
                f"expected header at beginning of MCAP file, found {type(header)}"
            )
        return header

    def get_summary(self) -> Optional[Summary]:
        """Reads the (optional) summary section from the MCAP file."""
        if self._summary is not None:
            return self._summary
        self._stream.seek(-(FOOTER_SIZE + MAGIC_SIZE), io.SEEK_END)
        footer = next(
            StreamReader(
                self._stream,
                skip_magic=True,
                record_size_limit=self._record_size_limit,
            ).records
        )
        if not isinstance(footer, Footer):
            raise McapError(
                f"expected footer at end of MCAP file, found {type(footer)}"
            )
        if footer.summary_start == 0:
            return None
        self._stream.seek(footer.summary_start, io.SEEK_SET)
        self._summary = _read_summary_from_stream_reader(
            StreamReader(
                self._stream, skip_magic=True, record_size_limit=self._record_size_limit
            )
        )
        return self._summary

    def iter_attachments(self) -> Iterator[Attachment]:
        """Iterates through attachment records in the MCAP."""
        summary = self.get_summary()
        if summary is None:
            # no index available, use a non-seeking reader to read linearly through the stream.
            self._stream.seek(0, io.SEEK_SET)
            yield from NonSeekingReader(self._stream).iter_attachments()
            return
        for attachment_index in summary.attachment_indexes:
            self._stream.seek(attachment_index.offset)
            record = next(
                StreamReader(
                    self._stream,
                    skip_magic=True,
                    record_size_limit=self._record_size_limit,
                ).records
            )
            if isinstance(record, Attachment):
                yield record
            else:
                raise McapError(f"expected attachment record, got {type(record)}")

    def iter_metadata(self) -> Iterator[Metadata]:
        """Iterates through metadata records in the MCAP."""
        summary = self.get_summary()
        if summary is None:
            # fall back to a non-seeking reader
            self._stream.seek(0, io.SEEK_SET)
            yield from NonSeekingReader(self._stream).iter_metadata()
            return
        for metadata_index in summary.metadata_indexes:
            self._stream.seek(metadata_index.offset)
            record = next(
                StreamReader(
                    self._stream,
                    skip_magic=True,
                    record_size_limit=self._record_size_limit,
                ).records
            )
            if isinstance(record, Metadata):
                yield record
            else:
                raise McapError(f"expected attachment record, got {type(record)}")


class NonSeekingReader(McapReader):
    """an McapReader for reading out of non-seekable data sources, such as a pipe or socket.

    :param stream: a file-like object for reading the source data from.
    :param validate_crcs: if ``True``, will validate chunk and data section CRC values.
    :param decoder_factories: An iterable of :py:class:`~mcap.decoder.DecoderFactory`
        instances which can provide decoding functionality to
        :py:meth:`~mcap.reader.McapReader.iter_decoded_messages`.
    :param record_size_limit: An upper bound to the size of MCAP records that this reader will
        attempt to load in bytes, defaulting to 4 GiB. If this reader encounters a record with a
        greater length, it will throw an :py:class:`~mcap.exceptions.RecordLengthLimitExceeded`
        error.  Setting to ``None`` removes the limit, but can allow corrupted MCAP files to trigger
        a `MemoryError` exception.
    """

    def __init__(
        self,
        stream: IO[bytes],
        validate_crcs: bool = False,
        decoder_factories: Iterable[DecoderFactory] = (),
        record_size_limit: Optional[int] = 4 * 2**30,
    ):
        super().__init__(decoder_factories=decoder_factories)
        self._stream_reader = StreamReader(
            stream,
            validate_crcs=validate_crcs,
            record_size_limit=record_size_limit,
        )
        self._schemas: Dict[int, Schema] = {}
        self._channels: Dict[int, Channel] = {}
        self._spent: bool = False

    def _check_spent(self):
        if self._spent:
            raise RuntimeError(
                "cannot use more than one query against a non-seeking data source"
            )
        self._spent = True

    def get_header(self) -> Header:
        """Reads the Header record from the beginning of the MCAP file."""
        self._check_spent()
        header = next(self._stream_reader.records)
        if not isinstance(header, Header):
            raise McapError(
                f"expected header at beginning of MCAP file, found {type(header)}"
            )
        return header

    def iter_messages(
        self,
        topics: Union[Iterable[str], str, None] = None,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
        log_time_order: bool = True,
        reverse: bool = False,
        *,
        starting_at: Optional[int] = None,
        starting_after: Optional[int] = None,
        ending_at: Optional[int] = None,
        ending_before: Optional[int] = None,
        query: Optional[Union[TimeRange, TimeWindow, MessageQuery]] = None,
        publish_starting_at: Optional[int] = None,
        publish_starting_after: Optional[int] = None,
        publish_ending_at: Optional[int] = None,
        publish_ending_before: Optional[int] = None,
        order: Optional[Union[MessageOrder, str]] = None,
    ) -> Iterator[Tuple[Optional[Schema], Channel, Message]]:
        """Iterates through the messages in an MCAP.

        See :py:meth:`McapReader.iter_messages` for a description of the arguments.

        .. warning::
            ordering by log time on a non-seekable stream will cause the entire
            content of the MCAP to be loaded into memory.
        """
        starting_at, ending_before = _resolve_time_range(
            start_time, end_time, starting_at, starting_after, ending_at, ending_before
        )
        publish_starting_at, publish_ending_before = _resolve_publish_bounds(
            publish_starting_at,
            publish_starting_after,
            publish_ending_at,
            publish_ending_before,
        )
        if isinstance(topics, str):
            topics = [topics]
        resolved_query = _resolve_query(
            query,
            starting_at,
            ending_before,
            publish_starting_at,
            publish_ending_before,
        )
        resolved_order, reverse = _resolve_order(log_time_order, reverse, order)
        if resolved_order is MessageOrder.FILE_ORDER:
            yield from self._iter_messages_internal(topics, resolved_query)
            return

        messages = list(self._iter_messages_internal(topics, resolved_query))
        # Python's sort is stable, so messages that tie on log time keep the file
        # order this reader scanned them in. Reversing the forward result, rather
        # than sorting with reverse=True, makes reverse exactly symmetric.
        messages.sort(key=lambda tup: tup[2].log_time)
        if reverse:
            messages.reverse()
        yield from messages

    def _iter_messages_internal(
        self,
        topics: Optional[Iterable[str]] = None,
        query: Optional[MessageQuery] = None,
    ) -> Iterator[Tuple[Optional[Schema], Channel, Message]]:
        self._check_spent()
        channels = self._channels
        schemas = self._schemas
        effective_query = coerce_query(query)
        for record in self._stream_reader.records:
            if isinstance(record, Message):
                channel = channels.get(record.channel_id)
                if channel is None:
                    raise McapError(
                        f"no channel record found with id {record.channel_id}"
                    )
                if topics is not None and channel.topic not in topics:
                    continue
                if not effective_query.includes(record):
                    continue
                if channel.schema_id == 0:
                    schema = None
                else:
                    schema = schemas[channel.schema_id]
                yield (schema, channel, record)
            elif isinstance(record, Schema):
                schemas[record.id] = record
            elif isinstance(record, Channel):
                if record.schema_id != 0 and record.schema_id not in schemas:
                    raise McapError(
                        f"no schema record found with id {record.schema_id}"
                    )
                channels[record.id] = record

    def get_summary(self) -> Optional[Summary]:
        """Returns a Summary object containing records from the (optional) summary section."""
        self._check_spent()
        return _read_summary_from_stream_reader(self._stream_reader)

    def iter_attachments(self) -> Iterator[Attachment]:
        """Iterates through attachment records in the MCAP."""
        self._check_spent()
        for record in self._stream_reader.records:
            if isinstance(record, Attachment):
                yield record

    def iter_metadata(self) -> Iterator[Metadata]:
        """Iterates through metadata records in the MCAP."""
        self._check_spent()
        for record in self._stream_reader.records:
            if isinstance(record, Metadata):
                yield record
