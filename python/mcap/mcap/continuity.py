"""Sequence continuity checks for messages read out of MCAP files.

MCAP stores a ``sequence`` counter on every message, but the counter belongs to
a publisher, not to a channel. A single topic may carry messages from several
producers, and one producer may publish the same topic through more than one
channel. This module therefore groups observations by ``(producer_id, topic)``.
A channel id is reported for provenance but is never used as a sequence identity.

The analysis is deliberately conservative:

* A missing sequence range is *confirmed* only when it lies between two observed
  messages of the same producer/topic/epoch and the whole time window between
  those messages is covered by a coverage interval marked ``complete``.
* Nothing is claimed before a stream's first observation or after its last one:
  only the observed window can establish a producer drop. Likewise a gap that
  spans a restart boundary is not attributed across the restart.
* A range whose time window is not fully covered, or is covered only by
  incomplete intervals, is an :class:`ObservationGap`: collection may have been
  off, so the producer is not accused of having dropped a message.
* A sequence step close to the modulus is a legal wraparound. A large step
  backwards without restart evidence is an :class:`UndecidableSegment`, not a
  missing range.
* Repeated ``(sequence, payload)`` pairs are duplicate deliveries. The same
  sequence with different payloads is a :class:`PayloadConflict`; every distinct
  payload is kept in the report.
* A sequence value occupying several payloads still counts as one observed
  sequence slot; :attr:`StreamReport.observation_count` retains the delivery
  count, so totals do not change with input or fragment order.

Input order does not matter: observations, coverage intervals, and restart
evidence are normalized and sorted before analysis, so the same set of records
produces the same report no matter how fragments are concatenated.

Payloads are reduced to SHA-256 digests as observations are consumed, and
duplicate deliveries are aggregated rather than retained one by one. Memory use
therefore scales with the number of distinct sequences and payloads, not with
the number of duplicated message bodies read.
"""

from __future__ import annotations

import hashlib
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import (
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

from .reader import McapReader
from .records import Channel, Message

DEFAULT_SEQUENCE_MODULUS = 1 << 32
"""MCAP message sequences are uint32 values, so they wrap at 2^32 by default."""


def _payload_digest(payload: bytes) -> str:
    return hashlib.sha256(bytes(payload)).hexdigest()


def _observation_digest(observation: "MessageObservation") -> str:
    if observation.payload_digest is not None:
        return observation.payload_digest
    if observation.data is not None:
        return _payload_digest(observation.data)
    raise ValueError(
        "observation for producer {producer!r} topic {topic!r} sequence {sequence} "
        "needs payload or payload_digest".format(
            producer=observation.producer_id,
            topic=observation.topic,
            sequence=observation.sequence,
        )
    )


@dataclass(frozen=True)
class MessageObservation:
    """One message delivery observed while reading MCAP data.

    Provide exactly one of ``data`` (hashed immediately and not retained) or
    ``payload_digest``. ``channel_id`` is optional provenance only; it is not
    part of the sequence identity.
    """

    producer_id: str
    topic: str
    sequence: int
    log_time: int
    payload_digest: Optional[str] = None
    data: Optional[bytes] = None
    publish_time: Optional[int] = None
    channel_id: Optional[int] = None

    def __post_init__(self) -> None:
        if self.payload_digest is None and self.data is not None:
            object.__setattr__(self, "payload_digest", _payload_digest(self.data))
        # The digest is all the analysis needs; keeping the body would defeat the
        # bounded-memory goal for large messages.
        if self.data is not None:
            object.__setattr__(self, "data", None)

    @classmethod
    def from_payload(
        cls,
        producer_id: str,
        topic: str,
        sequence: int,
        log_time: int,
        payload: bytes,
        publish_time: Optional[int] = None,
        channel_id: Optional[int] = None,
    ) -> "MessageObservation":
        """Build an observation, retaining only a digest of ``payload``."""
        return cls(
            producer_id=producer_id,
            topic=topic,
            sequence=sequence,
            log_time=log_time,
            payload_digest=_payload_digest(payload),
            publish_time=publish_time,
            channel_id=channel_id,
        )


@dataclass(frozen=True)
class RestartEvidence:
    """Evidence that a producer restarted at ``log_time``.

    Restarting splits a producer's timeline into epochs. A sequence reset across
    an epoch boundary is legal and is not reported as a gap or an unexplained
    reset. If ``topic`` is omitted the restart applies to every topic of that
    producer. A message logged exactly at the restart time belongs to the new
    epoch.
    """

    producer_id: str
    log_time: int
    topic: Optional[str] = None


@dataclass(frozen=True)
class CoverageInterval:
    """A closed time interval ``[start_time, end_time]`` where collection ran.

    ``producer_id=None`` applies the interval to every producer on the topic.
    ``complete=False`` records a window whose collection is known to have been
    interrupted (for example, a partial startup or teardown window); gaps inside
    it are reported as observation gaps, never as confirmed producer drops.

    A gap is confirmed only when complete intervals cover its whole time window.
    If no coverage intervals are supplied, every sequence gap is an observation
    gap: absence of observed messages alone is never evidence of a producer drop.
    """

    topic: str
    start_time: int
    end_time: int
    producer_id: Optional[str] = None
    complete: bool = True


@dataclass(frozen=True)
class SequenceSpan:
    """An inclusive sequence range, possibly wrapping past the modulus."""

    first: int
    last: int
    count: int

    @property
    def wraps(self) -> bool:
        return self.last < self.first


@dataclass(frozen=True)
class ConfirmedGap:
    """A sequence range that was demonstrably not delivered."""

    producer_id: str
    topic: str
    epoch: int
    after_sequence: int
    before_sequence: int
    missing: SequenceSpan
    after_log_time: int
    before_log_time: int

    @property
    def missing_count(self) -> int:
        return self.missing.count


@dataclass(frozen=True)
class ObservationGap:
    """A sequence range that cannot be attributed to the producer.

    ``reason`` is one of ``"outside_coverage"`` or ``"incomplete_coverage"``.
    Collection coverage was absent or incomplete over the time window, so the
    report only states that the range was not observed.
    """

    producer_id: str
    topic: str
    epoch: int
    after_sequence: int
    before_sequence: int
    missing: SequenceSpan
    after_log_time: int
    before_log_time: int
    reason: str

    @property
    def missing_count(self) -> int:
        return self.missing.count


@dataclass(frozen=True)
class Wraparound:
    """A legal sequence wraparound between two consecutive observations."""

    producer_id: str
    topic: str
    epoch: int
    from_sequence: int
    to_sequence: int
    from_log_time: int
    to_log_time: int


@dataclass(frozen=True)
class DuplicateDelivery:
    """The same sequence and payload observed more than once."""

    producer_id: str
    topic: str
    epoch: int
    sequence: int
    payload_digest: str
    count: int
    first_log_time: int
    last_log_time: int

    @property
    def duplicate_count(self) -> int:
        return self.count - 1


@dataclass(frozen=True)
class ConflictObservation:
    """One distinct payload seen for a conflicting sequence value."""

    payload_digest: str
    count: int
    first_log_time: int
    last_log_time: int
    channel_ids: Tuple[int, ...]


@dataclass(frozen=True)
class PayloadConflict:
    """One sequence value carried two or more distinct payloads."""

    producer_id: str
    topic: str
    epoch: int
    sequence: int
    observations: Tuple[ConflictObservation, ...]


@dataclass(frozen=True)
class UndecidableSegment:
    """A discontinuity that cannot be classified as a missing range.

    ``reason`` is ``"unexplained_sequence_reset"`` for a large backwards step
    without restart evidence, or ``"sequence_jump_too_large"`` for a forward
    jump larger than half the modulus. Neither is counted as a producer drop.
    """

    producer_id: str
    topic: str
    epoch: int
    reason: str
    after_sequence: int
    before_sequence: int
    after_log_time: int
    before_log_time: int


@dataclass(frozen=True)
class RestartBoundary:
    """A restart evidence point that falls between observations of this stream."""

    producer_id: str
    topic: str
    log_time: int
    epoch: int
    before_sequence: Optional[int]
    after_sequence: Optional[int]


@dataclass(frozen=True)
class StreamReport:
    """Continuity results for one ``(producer_id, topic)`` sequence."""

    producer_id: str
    topic: str
    observation_count: int
    unique_sequence_count: int
    first_sequence: Optional[int]
    last_sequence: Optional[int]
    confirmed_gaps: Tuple[ConfirmedGap, ...]
    observation_gaps: Tuple[ObservationGap, ...]
    wraparounds: Tuple[Wraparound, ...]
    duplicate_deliveries: Tuple[DuplicateDelivery, ...]
    payload_conflicts: Tuple[PayloadConflict, ...]
    undecidable_segments: Tuple[UndecidableSegment, ...]
    restart_boundaries: Tuple[RestartBoundary, ...]

    @property
    def confirmed_missing_sequences(self) -> int:
        return sum(gap.missing.count for gap in self.confirmed_gaps)

    @property
    def duplicate_delivery_count(self) -> int:
        return sum(dup.duplicate_count for dup in self.duplicate_deliveries)

    @property
    def payload_conflict_count(self) -> int:
        return len(self.payload_conflicts)

    @property
    def wraparound_count(self) -> int:
        return len(self.wraparounds)


@dataclass(frozen=True)
class ContinuityReport:
    """Continuity results for every ``(producer_id, topic)`` in the input."""

    streams: Tuple[StreamReport, ...]
    sequence_modulus: int = DEFAULT_SEQUENCE_MODULUS

    def for_stream(self, producer_id: str, topic: str) -> StreamReport:
        """Return the report for one producer/topic pair."""
        for stream in self.streams:
            if stream.producer_id == producer_id and stream.topic == topic:
                return stream
        raise KeyError((producer_id, topic))

    @property
    def confirmed_gaps(self) -> Tuple[ConfirmedGap, ...]:
        return tuple(gap for stream in self.streams for gap in stream.confirmed_gaps)

    @property
    def observation_gaps(self) -> Tuple[ObservationGap, ...]:
        return tuple(gap for stream in self.streams for gap in stream.observation_gaps)

    @property
    def duplicate_deliveries(self) -> Tuple[DuplicateDelivery, ...]:
        return tuple(
            dup for stream in self.streams for dup in stream.duplicate_deliveries
        )

    @property
    def payload_conflicts(self) -> Tuple[PayloadConflict, ...]:
        return tuple(
            conflict for stream in self.streams for conflict in stream.payload_conflicts
        )

    @property
    def wraparounds(self) -> Tuple[Wraparound, ...]:
        return tuple(wrap for stream in self.streams for wrap in stream.wraparounds)

    @property
    def undecidable_segments(self) -> Tuple[UndecidableSegment, ...]:
        return tuple(
            segment for stream in self.streams for segment in stream.undecidable_segments
        )

    @property
    def confirmed_missing_sequences(self) -> int:
        return sum(stream.confirmed_missing_sequences for stream in self.streams)

    @property
    def duplicate_delivery_count(self) -> int:
        return sum(stream.duplicate_delivery_count for stream in self.streams)

    @property
    def payload_conflict_count(self) -> int:
        return sum(stream.payload_conflict_count for stream in self.streams)

    @property
    def wraparound_count(self) -> int:
        return sum(stream.wraparound_count for stream in self.streams)


def _merge_intervals(intervals: Iterable[Tuple[int, int]]) -> List[Tuple[int, int]]:
    merged: List[Tuple[int, int]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
    return merged


def _covers(intervals: Sequence[Tuple[int, int]], start: int, end: int) -> bool:
    """Whether merged closed intervals cover the whole window [start, end]."""
    for left, right in intervals:
        if left > start:
            return False
        if left <= start and end <= right:
            return True
    return False


class _Coverage:
    def __init__(self, intervals: Iterable[CoverageInterval]) -> None:
        intervals = list(intervals)
        self._all = _merge_intervals((i.start_time, i.end_time) for i in intervals)
        self._complete = _merge_intervals(
            (i.start_time, i.end_time) for i in intervals if i.complete
        )

    def classify(self, start: int, end: int) -> Optional[str]:
        """Return None when the gap is confirmed, otherwise its observation reason."""
        if _covers(self._complete, start, end):
            return None
        if _covers(self._all, start, end):
            return "incomplete_coverage"
        return "outside_coverage"


def _sequence_span(first: int, last: int, modulus: int) -> SequenceSpan:
    return SequenceSpan(first=first, last=last, count=(last - first) % modulus + 1)


def _validate_modulus(modulus: int) -> None:
    if isinstance(modulus, bool) or not isinstance(modulus, int) or modulus < 2:
        raise ValueError("sequence_modulus must be an integer greater than 1")


@dataclass
class _PayloadStats:
    count: int = 0
    first_log_time: int = 0
    last_log_time: int = 0
    channel_ids: Set[int] = field(default_factory=set)

    def add(self, observation: MessageObservation) -> None:
        if self.count == 0:
            self.first_log_time = observation.log_time
            self.last_log_time = observation.log_time
        else:
            self.first_log_time = min(self.first_log_time, observation.log_time)
            self.last_log_time = max(self.last_log_time, observation.log_time)
        self.count += 1
        if observation.channel_id is not None:
            self.channel_ids.add(observation.channel_id)


@dataclass
class _SequenceStats:
    payloads: Dict[str, _PayloadStats] = field(default_factory=dict)
    first_log_time: int = 0
    last_log_time: int = 0

    def add(self, observation: MessageObservation) -> None:
        if not self.payloads:
            self.first_log_time = observation.log_time
            self.last_log_time = observation.log_time
        else:
            self.first_log_time = min(self.first_log_time, observation.log_time)
            self.last_log_time = max(self.last_log_time, observation.log_time)
        self.payloads.setdefault(
            _observation_digest(observation), _PayloadStats()
        ).add(observation)


# producer/topic -> epoch -> sequence -> stats
_Accumulator = Dict[Tuple[str, str], Dict[int, Dict[int, _SequenceStats]]]


def _validate_observation(observation: MessageObservation, modulus: int) -> None:
    if (
        isinstance(observation.sequence, bool)
        or not isinstance(observation.sequence, int)
        or not 0 <= observation.sequence < modulus
    ):
        raise ValueError(
            "sequence {sequence} for producer {producer!r} topic {topic!r} is outside "
            "[0, {modulus})".format(
                sequence=observation.sequence,
                producer=observation.producer_id,
                topic=observation.topic,
                modulus=modulus,
            )
        )
    if observation.payload_digest is None and observation.data is None:
        raise ValueError(
            "observation for producer {producer!r} topic {topic!r} sequence "
            "{sequence} needs payload or payload_digest".format(
                producer=observation.producer_id,
                topic=observation.topic,
                sequence=observation.sequence,
            )
        )


def _order_time_ties(sequences: Sequence[int], modulus: int) -> List[int]:
    """Order sequence values sharing one log time in a cyclic, deterministic way.

    Sorting the numbers themselves puts ``0`` before ``2**32 - 1`` and turns a
    tie-time wraparound into an apparent reset. Rotate each tie group so that its
    values read forward around the sequence circle instead.
    """
    ordered = sorted(set(sequences))
    if len(ordered) < 2 or ordered[0] != 0:
        return ordered
    gaps = [
        ((ordered[(index + 1) % len(ordered)] - value) % modulus, index)
        for index, value in enumerate(ordered)
    ]
    largest_gap, after_index = max(gaps, key=lambda item: (item[0], -item[1]))
    if largest_gap <= 1:
        return ordered
    start = (after_index + 1) % len(ordered)
    return ordered[start:] + ordered[:start]


def _ordered_slots(
    slots: Sequence[Tuple[int, int]], modulus: int
) -> List[Tuple[int, int]]:
    """Sort slots by log time and, inside a time tie, forward around the counter."""
    by_time: Dict[int, List[int]] = {}
    for log_time, sequence in slots:
        by_time.setdefault(log_time, []).append(sequence)
    ordered: List[Tuple[int, int]] = []
    anchor: Optional[int] = None
    for log_time in sorted(by_time):
        sequences = _order_time_ties(by_time[log_time], modulus)
        if anchor is not None and len(sequences) > 1:
            sequences = sorted(
                sequences, key=lambda sequence: (sequence - anchor) % modulus
            )
        ordered.extend((log_time, sequence) for sequence in sequences)
        anchor = sequences[-1]
    return ordered


def _build_stream_report(
    producer_id: str,
    topic: str,
    by_epoch: Dict[int, Dict[int, _SequenceStats]],
    restart_times: List[int],
    coverage: _Coverage,
    modulus: int,
) -> StreamReport:
    half = modulus // 2
    confirmed: List[ConfirmedGap] = []
    observed_gaps: List[ObservationGap] = []
    wraparounds: List[Wraparound] = []
    duplicates: List[DuplicateDelivery] = []
    conflicts: List[PayloadConflict] = []
    undecidable: List[UndecidableSegment] = []
    slots_by_epoch: Dict[int, List[Tuple[int, int]]] = {}
    observation_count = 0

    for epoch in sorted(by_epoch):
        slots: List[Tuple[int, int]] = []
        for sequence in sorted(by_epoch[epoch]):
            stats = by_epoch[epoch][sequence]
            observation_count += sum(
                payload.count for payload in stats.payloads.values()
            )
            conflict_observations: List[ConflictObservation] = []
            for digest in sorted(stats.payloads):
                payload = stats.payloads[digest]
                if payload.count > 1:
                    duplicates.append(
                        DuplicateDelivery(
                            producer_id=producer_id,
                            topic=topic,
                            epoch=epoch,
                            sequence=sequence,
                            payload_digest=digest,
                            count=payload.count,
                            first_log_time=payload.first_log_time,
                            last_log_time=payload.last_log_time,
                        )
                    )
                conflict_observations.append(
                    ConflictObservation(
                        payload_digest=digest,
                        count=payload.count,
                        first_log_time=payload.first_log_time,
                        last_log_time=payload.last_log_time,
                        channel_ids=tuple(sorted(payload.channel_ids)),
                    )
                )
            if len(conflict_observations) > 1:
                conflicts.append(
                    PayloadConflict(
                        producer_id=producer_id,
                        topic=topic,
                        epoch=epoch,
                        sequence=sequence,
                        observations=tuple(conflict_observations),
                    )
                )
            # A conflicting sequence still occupies one continuity slot. Its
            # earliest observed time represents the slot; the conflict entry
            # keeps each distinct payload separately.
            slots.append((stats.first_log_time, sequence))
        slots = _ordered_slots(slots, modulus)
        slots_by_epoch[epoch] = slots

        previous_sequence: Optional[int] = None
        previous_log_time: Optional[int] = None
        for log_time, sequence in slots:
            if previous_sequence is None or previous_log_time is None:
                previous_sequence, previous_log_time = sequence, log_time
                continue

            delta = (sequence - previous_sequence) % modulus
            if sequence < previous_sequence and delta <= half:
                wraparounds.append(
                    Wraparound(
                        producer_id=producer_id,
                        topic=topic,
                        epoch=epoch,
                        from_sequence=previous_sequence,
                        to_sequence=sequence,
                        from_log_time=previous_log_time,
                        to_log_time=log_time,
                    )
                )

            if delta == 1:
                pass
            elif 2 <= delta <= half:
                first = (previous_sequence + 1) % modulus
                last = (sequence - 1) % modulus
                span = _sequence_span(first, last, modulus)
                reason = coverage.classify(previous_log_time, log_time)
                if reason is None:
                    confirmed.append(
                        ConfirmedGap(
                            producer_id=producer_id,
                            topic=topic,
                            epoch=epoch,
                            after_sequence=previous_sequence,
                            before_sequence=sequence,
                            missing=span,
                            after_log_time=previous_log_time,
                            before_log_time=log_time,
                        )
                    )
                else:
                    observed_gaps.append(
                        ObservationGap(
                            producer_id=producer_id,
                            topic=topic,
                            epoch=epoch,
                            after_sequence=previous_sequence,
                            before_sequence=sequence,
                            missing=span,
                            after_log_time=previous_log_time,
                            before_log_time=log_time,
                            reason=reason,
                        )
                    )
            else:
                reason = (
                    "unexplained_sequence_reset"
                    if sequence < previous_sequence
                    else "sequence_jump_too_large"
                )
                undecidable.append(
                    UndecidableSegment(
                        producer_id=producer_id,
                        topic=topic,
                        epoch=epoch,
                        reason=reason,
                        after_sequence=previous_sequence,
                        before_sequence=sequence,
                        after_log_time=previous_log_time,
                        before_log_time=log_time,
                    )
                )

            previous_sequence, previous_log_time = sequence, log_time

    restart_boundaries: List[RestartBoundary] = []
    epochs_with_slots = sorted(slots_by_epoch)
    for index, restart_time in enumerate(restart_times):
        before_sequence: Optional[int] = None
        after_sequence: Optional[int] = None
        before_epochs = [epoch for epoch in epochs_with_slots if epoch <= index]
        after_epochs = [epoch for epoch in epochs_with_slots if epoch > index]
        if before_epochs and slots_by_epoch[before_epochs[-1]]:
            before_sequence = slots_by_epoch[before_epochs[-1]][-1][1]
        if after_epochs and slots_by_epoch[after_epochs[0]]:
            after_sequence = slots_by_epoch[after_epochs[0]][0][1]
        if before_sequence is not None or after_sequence is not None:
            restart_boundaries.append(
                RestartBoundary(
                    producer_id=producer_id,
                    topic=topic,
                    log_time=restart_time,
                    epoch=index + 1,
                    before_sequence=before_sequence,
                    after_sequence=after_sequence,
                )
            )

    confirmed.sort(
        key=lambda gap: (gap.after_log_time, gap.after_sequence, gap.missing.first)
    )
    observed_gaps.sort(
        key=lambda gap: (gap.after_log_time, gap.after_sequence, gap.missing.first)
    )
    wraparounds.sort(key=lambda wrap: (wrap.from_log_time, wrap.from_sequence))
    duplicates.sort(key=lambda dup: (dup.sequence, dup.payload_digest))
    conflicts.sort(key=lambda conflict: conflict.sequence)
    undecidable.sort(
        key=lambda segment: (segment.after_log_time, segment.after_sequence)
    )
    restart_boundaries.sort(key=lambda restart: restart.log_time)

    all_slots = [slot for epoch in epochs_with_slots for slot in slots_by_epoch[epoch]]
    return StreamReport(
        producer_id=producer_id,
        topic=topic,
        observation_count=observation_count,
        unique_sequence_count=len(all_slots),
        first_sequence=all_slots[0][1] if all_slots else None,
        last_sequence=all_slots[-1][1] if all_slots else None,
        confirmed_gaps=tuple(confirmed),
        observation_gaps=tuple(observed_gaps),
        wraparounds=tuple(wraparounds),
        duplicate_deliveries=tuple(duplicates),
        payload_conflicts=tuple(conflicts),
        undecidable_segments=tuple(undecidable),
        restart_boundaries=tuple(restart_boundaries),
    )


def analyze_continuity(
    observations: Iterable[MessageObservation],
    *,
    restarts: Iterable[RestartEvidence] = (),
    coverage: Iterable[CoverageInterval] = (),
    sequence_modulus: int = DEFAULT_SEQUENCE_MODULUS,
) -> ContinuityReport:
    """Analyze sequence continuity for any number of producers and topics.

    :param observations: message deliveries from one or more MCAP fragments. The
        iterable may arrive in any order; only the set of records matters.
    :param restarts: producer restart evidence. Each restart starts a new epoch for
        that producer, so a sequence reset there is legal.
    :param coverage: intervals in which collection is known to have run. A gap is
        only confirmed when complete coverage spans its whole time window.
    :param sequence_modulus: counter modulus, ``2**32`` for MCAP's uint32 sequence.
    """
    _validate_modulus(sequence_modulus)

    restarts_by_producer: Dict[str, List[RestartEvidence]] = {}
    for restart in restarts:
        restarts_by_producer.setdefault(restart.producer_id, []).append(restart)

    coverage_by_topic: Dict[str, List[CoverageInterval]] = {}
    for interval in coverage:
        if interval.start_time > interval.end_time:
            raise ValueError(
                "coverage interval end_time cannot come before start_time for topic "
                "{topic!r}".format(topic=interval.topic)
            )
        coverage_by_topic.setdefault(interval.topic, []).append(interval)

    restart_times_cache: Dict[Tuple[str, str], List[int]] = {}

    def restart_times_for(producer_id: str, topic: str) -> List[int]:
        key = (producer_id, topic)
        cached = restart_times_cache.get(key)
        if cached is None:
            cached = sorted(
                {
                    restart.log_time
                    for restart in restarts_by_producer.get(producer_id, ())
                    if restart.topic is None or restart.topic == topic
                }
            )
            restart_times_cache[key] = cached
        return cached

    accumulator: _Accumulator = {}
    for observation in observations:
        _validate_observation(observation, sequence_modulus)
        restart_times = restart_times_for(
            observation.producer_id, observation.topic
        )
        epoch = bisect_right(restart_times, observation.log_time)
        epoch_stats = accumulator.setdefault(
            (observation.producer_id, observation.topic), {}
        ).setdefault(epoch, {})
        epoch_stats.setdefault(
            observation.sequence, _SequenceStats()
        ).add(observation)

    streams: List[StreamReport] = []
    for producer_id, topic in sorted(accumulator):
        applicable_coverage = [
            interval
            for interval in coverage_by_topic.get(topic, ())
            if interval.producer_id is None or interval.producer_id == producer_id
        ]
        streams.append(
            _build_stream_report(
                producer_id=producer_id,
                topic=topic,
                by_epoch=accumulator[(producer_id, topic)],
                restart_times=restart_times_for(producer_id, topic),
                coverage=_Coverage(applicable_coverage),
                modulus=sequence_modulus,
            )
        )

    return ContinuityReport(streams=tuple(streams), sequence_modulus=sequence_modulus)


def check_continuity(
    observations: Iterable[MessageObservation],
    *,
    restarts: Iterable[RestartEvidence] = (),
    coverage: Iterable[CoverageInterval] = (),
    sequence_modulus: int = DEFAULT_SEQUENCE_MODULUS,
) -> ContinuityReport:
    """Alias for :func:`analyze_continuity`."""
    return analyze_continuity(
        observations,
        restarts=restarts,
        coverage=coverage,
        sequence_modulus=sequence_modulus,
    )


ProducerResolver = Union[
    str, Mapping[int, str], Callable[[Channel, Message], str]
]


def _resolve_producer(
    resolver: ProducerResolver, channel: Channel, message: Message
) -> str:
    if isinstance(resolver, str):
        return resolver
    if callable(resolver):
        return resolver(channel, message)
    try:
        return resolver[channel.id]
    except KeyError:
        raise ValueError(
            "no producer identity supplied for channel id {channel_id} on topic "
            "{topic!r}".format(channel_id=channel.id, topic=channel.topic)
        ) from None


def iter_message_observations(
    reader: McapReader,
    producer: ProducerResolver,
    *,
    topics: Optional[Iterable[str]] = None,
    starting_at: Optional[int] = None,
    ending_before: Optional[int] = None,
) -> Iterator[MessageObservation]:
    """Adapt an :class:`~mcap.reader.McapReader` into sequence observations.

    ``producer`` is a producer id applied to every channel, a mapping from channel
    id to producer id, or a callable receiving ``(channel, message)``. Use the
    callable when one channel id can carry messages from more than one producer
    (for example when a field inside the payload identifies the publisher).
    Grouping is still by ``(producer, topic)``, so giving different channels the
    same producer keeps them in one sequence, and giving the same topic different
    producers keeps their sequences separate.

    Messages are read in file order (``log_time_order=False``) to keep memory
    bounded; :func:`analyze_continuity` accepts observations in any order.
    """
    for _schema, channel, message in reader.iter_messages(
        topics=topics,
        log_time_order=False,
        starting_at=starting_at,
        ending_before=ending_before,
    ):
        yield MessageObservation.from_payload(
            producer_id=_resolve_producer(producer, channel, message),
            topic=channel.topic,
            sequence=message.sequence,
            log_time=message.log_time,
            payload=message.data,
            publish_time=message.publish_time,
            channel_id=channel.id,
        )


__all__ = [
    "ConfirmedGap",
    "ConflictObservation",
    "ContinuityReport",
    "CoverageInterval",
    "DEFAULT_SEQUENCE_MODULUS",
    "DuplicateDelivery",
    "MessageObservation",
    "ObservationGap",
    "PayloadConflict",
    "RestartBoundary",
    "RestartEvidence",
    "SequenceSpan",
    "StreamReport",
    "UndecidableSegment",
    "Wraparound",
    "analyze_continuity",
    "check_continuity",
    "iter_message_observations",
]
