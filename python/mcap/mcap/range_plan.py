"""Cost-based read planning for MCAP files on remote, range-request-capable storage.

Given the parsed :class:`~mcap.summary.Summary` (chunk index plus channel and schema
records), a set of topic/time windows, a hard upper bound on the size of a single
ranged request, and the fixed overhead of issuing one request,
:func:`build_read_plan` produces a :class:`ReadPlan` that minimises the estimated
total fetch cost::

    total_cost = request_count * request_overhead + total_bytes_transferred

The planner only decides *which byte ranges to fetch*. It never treats a chunk's time
bounding box as proof of a hit: a chunk index's ``message_start_time`` /
``message_end_time`` is a candidate filter only, and every plan records that the
decompressed messages must still be filtered by topic and log-time window. A region
without a message index is scheduled for a full chunk scan rather than being assumed to
contain no messages. Compressed chunks are shared across all windows that touch them:
the merged byte range is downloaded once and the chunk is decompressed once.
"""

from dataclasses import dataclass, field
from io import BytesIO
from typing import (
    Callable,
    Dict,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from .data_stream import ReadDataStream
from .reader import FOOTER_SIZE
from .records import Channel, Chunk, ChunkIndex, Message, Schema
from .stream_reader import MAGIC_SIZE, _get_chunk_data, _parse_chunk_records
from .summary import Summary

# Type alias for a callback that fetches ``[start, end)`` from the remote file.
RangeFetcher = Callable[[int, int], bytes]


class TopicTimeWindow(NamedTuple):
    """A half-open ``[start_time, end_time)`` log-time window on one topic.

    Either bound may be ``None`` to leave that side unbounded. Times are integer
    nanoseconds, matching the MCAP record timestamps.
    """

    topic: str
    start_time: Optional[int] = None
    end_time: Optional[int] = None


@dataclass(frozen=True)
class RangeRequest:
    """One HTTP-style ranged request that the executor should issue.

    :ivar start: inclusive byte offset.
    :ivar end: exclusive byte offset; ``end - start`` never exceeds
        ``max_range_size`` unless the request covers a single record that is itself
        larger than the cap (the plan carries a warning in that case).
    :ivar purpose: human-readable tags for the records covered (chunk positions,
        message indexes, summary, footer), useful for logging the plan.
    """

    start: int
    end: int
    purpose: Tuple[str, ...]

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class ChunkDownload:
    """A compressed chunk that must be fetched and decompressed exactly once.

    Several topic/time windows can share one download: :attr:`serves_windows` lists all
    of them. The chunk's time bounding box only makes it a candidate; the messages
    inside still have to pass :func:`filter_messages`.
    """

    chunk_index_position: int
    start: int
    end: int
    compressed_size: int
    uncompressed_size: int
    compression: str
    request_indexes: Tuple[int, ...]
    unindexed: bool
    serves_windows: Tuple[int, ...]
    requires_message_filtering: bool = True


@dataclass(frozen=True)
class IndexDependency:
    """A definition or index record that must be available before chunk contents can
    be interpreted.

    :ivar kind: one of ``"schema"``, ``"channel"``, ``"message_index"``,
        ``"summary_section"`` or ``"footer"``.
    :ivar identifier: record id (schema/channel id), chunk position, or a descriptive
        string for section-level dependencies.
    :ivar start: byte offset of the dependency, or ``None`` when only the containing
        section is known.
    :ivar end: exclusive byte offset, or ``None``.
    :ivar available: ``False`` marks a missing message index: the chunk must be scanned
        in full instead of being pruned.
    :ivar reason: why the dependency is required first.
    """

    kind: str
    identifier: object
    start: Optional[int]
    end: Optional[int]
    available: bool
    reason: str


@dataclass
class ReadPlan:
    """The result of :func:`build_read_plan`.

    :ivar requests: deduplicated, cost-merged ranged requests in ascending offset
        order; these are the only byte ranges an executor needs to fetch.
    :ivar chunk_downloads: one entry per candidate compressed chunk, each mapped to the
        request that contains it and marked for a single decompression.
    :ivar dependencies: schema, channel, message-index, summary and footer records that
        must be fetched before the chunk payloads are interpreted.
    :ivar windows: the normalised query windows.
    :ivar unindexed_regions: chunk byte ranges whose chunk index carries no message
        index; they are scheduled unconditionally and must never be read as "no
        messages here".
    :ivar warnings: planning caveats, e.g. a record larger than ``max_range_size``.
    :ivar request_overhead: per-request cost used when computing
        :attr:`estimated_cost`.
    """

    requests: List[RangeRequest] = field(default_factory=list)
    chunk_downloads: List[ChunkDownload] = field(default_factory=list)
    dependencies: List[IndexDependency] = field(default_factory=list)
    windows: List[TopicTimeWindow] = field(default_factory=list)
    unindexed_regions: List[Tuple[int, int]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    request_overhead: int = 0

    @property
    def total_requests(self) -> int:
        return len(self.requests)

    @property
    def total_bytes(self) -> int:
        return sum(request.length for request in self.requests)

    @property
    def estimated_cost(self) -> int:
        """Request overhead plus transferred bytes, using the planner's cost model."""
        return self.request_overhead * len(self.requests) + self.total_bytes

    # Fetch order for definition/index dependencies: locate the summary, read the
    # schema/channel definitions, then consult message indexes before chunk payloads.
    _DEPENDENCY_ORDER = (
        "footer",
        "summary_section",
        "schema",
        "channel",
        "message_index",
    )

    def dependencies_in_order(self) -> List[IndexDependency]:
        """Returns :attr:`dependencies` in the order a remote reader must fetch them.

        The footer locates the summary section, which carries schema and channel
        definitions; message indexes come last, because they only make sense once the
        channel ids they reference can be resolved. Missing indexes (``available is
        False``) stay in this list so the reader scans rather than assumes emptiness.
        """
        priority = {kind: index for index, kind in enumerate(self._DEPENDENCY_ORDER)}
        return sorted(
            self.dependencies, key=lambda dependency: priority[dependency.kind]
        )


def _bbox_overlaps_window(
    chunk_index: ChunkIndex, start_time: Optional[int], end_time: Optional[int]
) -> bool:
    """Whether chunk bbox ``[message_start_time, message_end_time]`` can contain any
    log time in half-open ``[start_time, end_time)``. A bbox overlap is a necessary,
    not sufficient, condition for a hit."""
    if start_time is not None and chunk_index.message_end_time < start_time:
        return False
    if end_time is not None and chunk_index.message_start_time >= end_time:
        return False
    return True


def _normalise_windows(
    windows: Sequence[TopicTimeWindow],
) -> List[TopicTimeWindow]:
    if not windows:
        raise ValueError("at least one topic/time window is required")
    normalised: List[TopicTimeWindow] = []
    for window in windows:
        if not window.topic:
            raise ValueError("topic/time windows require a non-empty topic")
        if (
            window.start_time is not None
            and window.end_time is not None
            and window.start_time > window.end_time
        ):
            raise ValueError(
                f"window for {window.topic!r} starts after it ends: {window}"
            )
        normalised.append(window)
    return normalised


def _message_index_block_range(
    chunk_index: ChunkIndex,
) -> Optional[Tuple[int, int]]:
    """Byte range covering every MessageIndex record written after this chunk, or
    ``None`` when the chunk has no message indexes."""
    if chunk_index.message_index_length <= 0 or not chunk_index.message_index_offsets:
        return None
    block_start = min(chunk_index.message_index_offsets.values())
    return block_start, block_start + chunk_index.message_index_length


def _merge_groups(
    intervals: List[Tuple[int, int]],
    max_range_size: int,
    request_overhead: int,
) -> List[Tuple[int, int]]:
    """Partition sorted, disjoint intervals into request groups.

    A group is fetched as one ranged request covering ``[first start, last end)``, so
    gaps between adjacent intervals are paid for as extra download. Dynamic programming
    picks the partition minimising ``overhead per request + bytes transferred``:
    merging two groups saves one ``request_overhead`` but costs the gap bytes between
    them, so a merge is chosen only when the overhead exceeds that gap. Groups spanning
    more than ``max_range_size`` are forbidden unless a single interval is itself
    oversized; such an interval is split afterwards.
    """
    if not intervals:
        return []

    count = len(intervals)
    # dp[k] = minimum cost to fetch the first k intervals; choice[k] is the first
    # interval index of the group that ends at k-1.
    dp: List[float] = [float("inf")] * (count + 1)
    choice: List[int] = [0] * (count + 1)
    dp[0] = 0
    for k in range(1, count + 1):
        end = intervals[k - 1][1]
        for j in range(k - 1, -1, -1):
            start = intervals[j][0]
            span = end - start
            single_oversized = (
                j == k - 1 and intervals[j][1] - intervals[j][0] > max_range_size
            )
            if span > max_range_size and not single_oversized:
                break
            candidate = dp[j] + request_overhead + span
            # On equal cost, prefer the larger merged group (fewer requests).
            if candidate <= dp[k]:
                dp[k] = candidate
                choice[k] = j

    groups: List[Tuple[int, int]] = []
    k = count
    while k > 0:
        j = choice[k]
        groups.append((intervals[j][0], intervals[k - 1][1]))
        k = j
    groups.reverse()
    return groups


def _split_oversized(
    groups: List[Tuple[int, int]],
    max_range_size: int,
    warnings: List[str],
    purposes: Dict[Tuple[int, int], List[str]],
) -> List[Tuple[int, int]]:
    """Split any single interval larger than the cap into cap-sized pieces."""
    split: List[Tuple[int, int]] = []
    for start, end in groups:
        if end - start <= max_range_size:
            split.append((start, end))
            continue
        warnings.append(
            f"record at byte {start} is {end - start} bytes, larger than the "
            f"per-request cap of {max_range_size}; splitting into multiple requests"
        )
        cursor = start
        while cursor < end:
            piece_end = min(cursor + max_range_size, end)
            split.append((cursor, piece_end))
            purposes.setdefault((cursor, piece_end), []).extend(
                purposes.get((start, end), [])
            )
            cursor = piece_end
    return split


def filter_messages(
    records: Sequence[object],
    channels: Dict[int, Channel],
    windows: Sequence[TopicTimeWindow],
) -> List[Tuple[Channel, Message]]:
    """Apply the definitive query filter to a decompressed chunk's records.

    Only :class:`~mcap.records.Message` records whose channel topic is one of the
    requested topics and whose log time lies inside that topic's half-open window are
    returned. The chunk time bounding box is not consulted here: callers must not hand
    unfiltered chunk payloads to consumers as query hits.
    """
    by_topic: Dict[str, List[Tuple[Optional[int], Optional[int]]]] = {}
    for window in windows:
        by_topic.setdefault(window.topic, []).append(
            (window.start_time, window.end_time)
        )

    hits: List[Tuple[Channel, Message]] = []
    for record in records:
        if not isinstance(record, Message):
            continue
        channel = channels.get(record.channel_id)
        if channel is None or channel.topic not in by_topic:
            continue
        for start_time, end_time in by_topic[channel.topic]:
            if start_time is not None and record.log_time < start_time:
                continue
            if end_time is not None and record.log_time >= end_time:
                continue
            hits.append((channel, record))
            break
    return hits


def build_read_plan(
    summary: Summary,
    windows: Sequence[TopicTimeWindow],
    *,
    max_range_size: int,
    request_overhead: int,
    file_size: Optional[int] = None,
    summary_section: Optional[Tuple[int, int]] = None,
) -> ReadPlan:
    """Build a minimum-cost ranged-read plan for the given topic/time windows.

    :param summary: parsed MCAP summary (chunk indexes, channels, schemas).
    :param windows: topic/time windows to read; windows on the same topic are all
        honoured and may share chunk downloads.
    :param max_range_size: hard upper bound on ``end - start`` for one request.
    :param request_overhead: fixed cost (in the same unit as transferred bytes) added
        for every request issued; merging adjacent ranges is only chosen when the saved
        overhead exceeds the extra gap bytes that would be downloaded.
    :param file_size: total file size in bytes; when supplied, a footer fetch is added
        as a bootstrap dependency so a remote reader can locate the summary.
    :param summary_section: ``[start, end)`` of the summary section; when supplied,
        schema/channel/index dependencies can carry concrete byte ranges.
    :returns: a :class:`ReadPlan`. Chunks whose message index is missing are included
        unconditionally as full scans and reported in
        :attr:`ReadPlan.unindexed_regions`; they are never pruned as empty.
    """
    if max_range_size <= 0:
        raise ValueError("max_range_size must be positive")
    if request_overhead < 0:
        raise ValueError("request_overhead must be non-negative")

    normalised = _normalise_windows(windows)
    requested_topics = {window.topic for window in normalised}

    topic_channels: Dict[str, List[int]] = {}
    for channel_id, channel in summary.channels.items():
        if channel.topic in requested_topics:
            topic_channels.setdefault(channel.topic, []).append(channel_id)
    unknown_topics = requested_topics.difference(topic_channels)
    if unknown_topics:
        raise ValueError(
            "no channel records in the summary for topics: "
            + ", ".join(sorted(unknown_topics))
        )

    plan = ReadPlan(windows=normalised, request_overhead=request_overhead)

    # Deduplicated intervals that must be fetched, tagged with purpose strings. Chunk
    # intervals and index/section intervals are merged together by the same cost model.
    interval_purposes: Dict[Tuple[int, int], List[str]] = {}
    candidate_positions: List[int] = []
    dependency_keys: Set[Tuple[str, object]] = set()

    def add_dependency(dependency: IndexDependency) -> None:
        key = (dependency.kind, dependency.identifier)
        if key not in dependency_keys:
            dependency_keys.add(key)
            plan.dependencies.append(dependency)

    def add_interval(start: int, end: int, purpose: str) -> Tuple[int, int]:
        key = (start, end)
        purposes = interval_purposes.setdefault(key, [])
        if purpose not in purposes:
            purposes.append(purpose)
        return key

    relevant_channel_ids = sorted(
        {
            channel_id
            for channel_ids in topic_channels.values()
            for channel_id in channel_ids
        }
    )

    for position, chunk_index in enumerate(summary.chunk_indexes):
        indexed_channels = set(chunk_index.message_index_offsets)
        unindexed = not indexed_channels
        serving: List[int] = []
        for window_index, window in enumerate(normalised):
            if not _bbox_overlaps_window(
                chunk_index, window.start_time, window.end_time
            ):
                continue
            if unindexed:
                # Topic membership is unknown without a message index: the chunk is a
                # candidate and must be scanned, never assumed empty.
                serving.append(window_index)
            else:
                channel_ids = topic_channels[window.topic]
                if any(channel_id in indexed_channels for channel_id in channel_ids):
                    serving.append(window_index)
        if not serving:
            continue

        candidate_positions.append(position)
        chunk_key = (
            chunk_index.chunk_start_offset,
            chunk_index.chunk_start_offset + chunk_index.chunk_length,
        )
        add_interval(*chunk_key, f"chunk[{position}]")

        if unindexed:
            plan.unindexed_regions.append(chunk_key)
            add_dependency(
                IndexDependency(
                    kind="message_index",
                    identifier=(position, None),
                    start=None,
                    end=None,
                    available=False,
                    reason=(
                        f"chunk {position} carries no message index; its full byte "
                        "range must be scanned and the missing index must not be "
                        "interpreted as an absence of messages"
                    ),
                )
            )
            continue

        index_block = _message_index_block_range(chunk_index)
        if index_block is not None:
            add_interval(*index_block, f"message-index[{position}]")
        for channel_id in relevant_channel_ids:
            offset = chunk_index.message_index_offsets.get(channel_id)
            add_dependency(
                IndexDependency(
                    kind="message_index",
                    identifier=(position, channel_id),
                    start=offset,
                    end=None,
                    available=offset is not None,
                    reason=(
                        f"locate channel {channel_id} messages inside chunk {position}"
                        if offset is not None
                        else f"channel {channel_id} has no message index in chunk "
                        f"{position}; fall back to scanning the whole chunk"
                    ),
                )
            )

    # Schema and channel definitions must be resolved before any message is interpreted.
    for channel_id in relevant_channel_ids:
        channel = summary.channels[channel_id]
        add_dependency(
            IndexDependency(
                kind="channel",
                identifier=channel_id,
                start=summary_section[0] if summary_section else None,
                end=summary_section[1] if summary_section else None,
                available=True,
                reason=f"resolve topic {channel.topic!r} and its message encoding",
            )
        )
        if channel.schema_id != 0 and channel.schema_id in summary.schemas:
            schema: Schema = summary.schemas[channel.schema_id]
            add_dependency(
                IndexDependency(
                    kind="schema",
                    identifier=schema.id,
                    start=summary_section[0] if summary_section else None,
                    end=summary_section[1] if summary_section else None,
                    available=True,
                    reason=(
                        f"decode messages on channel {channel_id} ({channel.topic!r})"
                    ),
                )
            )

    if summary_section is not None:
        add_interval(summary_section[0], summary_section[1], "summary-section")
        add_dependency(
            IndexDependency(
                kind="summary_section",
                identifier="summary",
                start=summary_section[0],
                end=summary_section[1],
                available=True,
                reason="contains the schema and channel definition records",
            )
        )
    if file_size is not None:
        footer_start = file_size - (FOOTER_SIZE + MAGIC_SIZE)
        add_interval(footer_start, file_size, "footer")
        add_dependency(
            IndexDependency(
                kind="footer",
                identifier="footer",
                start=footer_start,
                end=file_size,
                available=True,
                reason="read summary offsets before fetching any index",
            )
        )

    sorted_intervals = sorted(interval_purposes)
    groups = _merge_groups(sorted_intervals, max_range_size, request_overhead)
    groups = _split_oversized(
        groups, max_range_size, plan.warnings, interval_purposes
    )

    requests: List[RangeRequest] = []
    for group_start, group_end in groups:
        purposes: List[str] = []
        for interval, tags in interval_purposes.items():
            if interval[0] >= group_start and interval[1] <= group_end:
                purposes.extend(tags)
        requests.append(
            RangeRequest(
                start=group_start,
                end=group_end,
                purpose=tuple(dict.fromkeys(purposes)),
            )
        )
    plan.requests = requests

    def requests_covering(start: int, end: int) -> Tuple[int, ...]:
        return tuple(
            index
            for index, request in enumerate(requests)
            if request.start < end and request.end > start
        )

    for position in candidate_positions:
        chunk_index = summary.chunk_indexes[position]
        chunk_key = (
            chunk_index.chunk_start_offset,
            chunk_index.chunk_start_offset + chunk_index.chunk_length,
        )
        serves = tuple(
            window_index
            for window_index, window in enumerate(normalised)
            if _bbox_overlaps_window(
                chunk_index, window.start_time, window.end_time
            )
            and (
                not chunk_index.message_index_offsets
                or any(
                    channel_id in chunk_index.message_index_offsets
                    for channel_id in topic_channels[window.topic]
                )
            )
        )
        plan.chunk_downloads.append(
            ChunkDownload(
                chunk_index_position=position,
                start=chunk_key[0],
                end=chunk_key[1],
                compressed_size=chunk_index.compressed_size,
                uncompressed_size=chunk_index.uncompressed_size,
                compression=chunk_index.compression,
                request_indexes=requests_covering(*chunk_key),
                unindexed=not chunk_index.message_index_offsets,
                serves_windows=serves,
            )
        )

    return plan


class RangeReadExecutor:
    """Executes a :class:`ReadPlan` against a remote file via a ranged-read callback.

    Each ranged request is issued at most once while a plan runs (its bytes are cached
    until the last chunk that needs it has been processed) and each compressed chunk is
    downloaded and decompressed at most once, even when it serves several windows.
    Decompressed messages pass through :func:`filter_messages`, so a consumer never
    receives an unfiltered chunk payload as a query hit. Request buffers are released as
    soon as their reference count drops to zero, so memory scales with the largest
    request group and one decompressed chunk, not with the file length.
    """

    def __init__(
        self,
        plan: ReadPlan,
        summary: Summary,
        read_range: RangeFetcher,
        validate_crcs: bool = False,
    ):
        self._plan = plan
        self._summary = summary
        self._read_range = read_range
        self._validate_crcs = validate_crcs
        self._request_cache: Dict[int, bytes] = {}
        # Number of not-yet-processed chunk downloads still needing each request.
        self._request_refcounts: Dict[int, int] = {}
        for download in plan.chunk_downloads:
            for request_index in download.request_indexes:
                self._request_refcounts[request_index] = (
                    self._request_refcounts.get(request_index, 0) + 1
                )

    def _fetch_request(self, request_index: int) -> bytes:
        if request_index not in self._request_cache:
            request = self._plan.requests[request_index]
            self._request_cache[request_index] = self._read_range(
                request.start, request.end
            )
        return self._request_cache[request_index]

    def _release_requests(self, request_indexes: Tuple[int, ...]) -> None:
        for request_index in request_indexes:
            remaining = self._request_refcounts[request_index] - 1
            if remaining == 0:
                self._request_refcounts.pop(request_index, None)
                self._request_cache.pop(request_index, None)
            else:
                self._request_refcounts[request_index] = remaining

    def _read_chunk_bytes(self, download: ChunkDownload) -> bytes:
        """Assembles the chunk record's bytes from its covering requests, issuing each
        ranged request at most once. Adjacent requests share a boundary, so the
        concatenation reconstructs the exact original bytes."""
        try:
            pieces: List[bytes] = []
            for request_index in download.request_indexes:
                request = self._plan.requests[request_index]
                buffer = self._fetch_request(request_index)
                piece_start = max(download.start, request.start)
                piece_end = min(download.end, request.end)
                pieces.append(
                    buffer[
                        piece_start - request.start : piece_end - request.start
                    ]
                )
            return b"".join(pieces)
        finally:
            self._release_requests(download.request_indexes)

    def decompress_chunk(self, download: ChunkDownload) -> bytes:
        """Downloads (once) and decompresses (once) one planned chunk.

        The chunk is fetched through its covering requests, which are released
        immediately afterwards, so only the decompressed contents remain in memory."""
        chunk = self._read_chunk(download)
        return _get_chunk_data(chunk, validate_crc=self._validate_crcs)

    def _read_chunk(self, download: ChunkDownload) -> Chunk:
        raw = self._read_chunk_bytes(download)
        stream = BytesIO(raw)
        stream.seek(1 + 8)  # skip opcode and record-length prefix
        return Chunk.read(ReadDataStream(stream))

    def _resolve_schema(self, channel: Channel) -> Optional[Schema]:
        if channel.schema_id == 0:
            return None
        return self._summary.schemas.get(channel.schema_id)

    def iter_filtered_messages(
        self,
    ) -> Iterator[Tuple[Optional[Schema], Channel, Message]]:
        """Streams the filtered hits one chunk at a time, in file order.

        Each chunk is downloaded, decompressed, filtered and released before the next
        one is touched; nothing beyond the current chunk is buffered here."""
        for download in self._plan.chunk_downloads:
            data = self.decompress_chunk(download)
            records = _parse_chunk_records(data)
            for channel, message in filter_messages(
                records, self._summary.channels, self._plan.windows
            ):
                yield self._resolve_schema(channel), channel, message

    def iter_messages(
        self,
    ) -> List[Tuple[Optional[Schema], Channel, Message]]:
        """Runs the plan and returns the filtered hits across all windows, sorted by
        log time. Use :meth:`iter_filtered_messages` to avoid buffering the hits."""
        hits = list(self.iter_filtered_messages())
        hits.sort(key=lambda item: item[2].log_time)
        return hits
