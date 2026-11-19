"""tests for sequence continuity analysis over MCAP message observations."""

import random
from io import BytesIO
from typing import Iterable, List, Optional, Tuple

import pytest

from mcap.continuity import (
    ConfirmedGap,
    CoverageInterval,
    MessageObservation,
    ObservationGap,
    RestartEvidence,
    analyze_continuity,
    check_continuity,
    iter_message_observations,
)
from mcap.reader import make_reader
from mcap.writer import CompressionType, IndexType, Writer


def observe(
    producer: str,
    topic: str,
    sequence: int,
    log_time: int,
    payload: Optional[bytes] = None,
    channel_id: Optional[int] = None,
) -> MessageObservation:
    if payload is None:
        payload = "{}:{}".format(sequence, log_time).encode()
    return MessageObservation.from_payload(
        producer, topic, sequence, log_time, payload, channel_id=channel_id
    )


def full_coverage(topic: str, start: int = 0, end: int = 10**9) -> List[CoverageInterval]:
    return [CoverageInterval(topic, start, end)]


def test_confirmed_gap_between_complete_coverage() -> None:
    observations = [
        observe("cam-a", "/image", 1, 10),
        observe("cam-a", "/image", 2, 20),
        observe("cam-a", "/image", 5, 30),
        observe("cam-a", "/image", 6, 40),
    ]
    report = analyze_continuity(
        observations, coverage=full_coverage("/image")
    )
    stream = report.for_stream("cam-a", "/image")
    assert len(stream.confirmed_gaps) == 1
    gap = stream.confirmed_gaps[0]
    assert isinstance(gap, ConfirmedGap)
    assert (gap.missing.first, gap.missing.last, gap.missing.count) == (3, 4, 2)
    assert (gap.after_sequence, gap.before_sequence) == (2, 5)
    assert stream.confirmed_missing_sequences == 2
    assert stream.observation_gaps == ()
    assert stream.undecidable_segments == ()
    assert report.confirmed_missing_sequences == 2


def test_wraparound_is_legal_and_does_not_hide_a_gap() -> None:
    modulus = 8
    observations = [
        observe("p", "/t", 6, 10),
        observe("p", "/t", 7, 20),
        observe("p", "/t", 0, 30),
        observe("p", "/t", 2, 40),
    ]
    report = analyze_continuity(
        observations,
        coverage=full_coverage("/t"),
        sequence_modulus=modulus,
    )
    stream = report.for_stream("p", "/t")
    assert len(stream.wraparounds) == 1
    wrap = stream.wraparounds[0]
    assert (wrap.from_sequence, wrap.to_sequence) == (7, 0)
    assert stream.undecidable_segments == ()
    # The 0 -> 2 step still crosses sequence 1.
    assert len(stream.confirmed_gaps) == 1
    assert (stream.confirmed_gaps[0].missing.first, stream.confirmed_gaps[0].missing.last) == (
        1,
        1,
    )


def test_wraparound_gap_reports_wrapping_span() -> None:
    modulus = 8
    observations = [
        observe("p", "/t", 6, 10),
        observe("p", "/t", 1, 20),
    ]
    stream = analyze_continuity(
        observations,
        coverage=full_coverage("/t"),
        sequence_modulus=modulus,
    ).for_stream("p", "/t")
    assert stream.wraparounds[0].from_sequence == 6
    gap = stream.confirmed_gaps[0]
    assert (gap.missing.first, gap.missing.last, gap.missing.count) == (7, 0, 2)
    assert gap.missing.wraps


def test_restart_makes_sequence_reset_legal() -> None:
    observations = [
        observe("p", "/t", 100, 10),
        observe("p", "/t", 101, 20),
        observe("p", "/t", 0, 30),
        observe("p", "/t", 1, 40),
    ]
    without_restart = analyze_continuity(
        observations, coverage=full_coverage("/t")
    ).for_stream("p", "/t")
    assert len(without_restart.undecidable_segments) == 1
    assert without_restart.undecidable_segments[0].reason == "unexplained_sequence_reset"

    with_restart = analyze_continuity(
        observations,
        coverage=full_coverage("/t"),
        restarts=[RestartEvidence("p", 25)],
    ).for_stream("p", "/t")
    assert with_restart.undecidable_segments == ()
    assert with_restart.confirmed_gaps == ()
    assert with_restart.observation_gaps == ()
    assert len(with_restart.restart_boundaries) == 1
    boundary = with_restart.restart_boundaries[0]
    assert (boundary.before_sequence, boundary.after_sequence) == (101, 0)


def test_restart_is_scoped_by_producer_and_topic() -> None:
    observations = [
        observe("p", "/t", 100, 10),
        observe("p", "/t", 0, 30),
        observe("p", "/other", 50, 10),
        observe("p", "/other", 0, 30),
    ]
    report = analyze_continuity(
        observations,
        coverage=[CoverageInterval("/t", 0, 100), CoverageInterval("/other", 0, 100)],
        restarts=[RestartEvidence("p", 20, topic="/t")],
    )
    assert report.for_stream("p", "/t").undecidable_segments == ()
    other = report.for_stream("p", "/other")
    assert len(other.undecidable_segments) == 1


def test_duplicate_delivery_same_sequence_and_payload() -> None:
    observations = [
        observe("p", "/t", 1, 10, b"same"),
        observe("p", "/t", 1, 11, b"same"),
        observe("p", "/t", 1, 12, b"same"),
        observe("p", "/t", 2, 20, b"next"),
    ]
    stream = analyze_continuity(
        observations, coverage=full_coverage("/t")
    ).for_stream("p", "/t")
    assert len(stream.duplicate_deliveries) == 1
    duplicate = stream.duplicate_deliveries[0]
    assert duplicate.sequence == 1
    assert duplicate.count == 3
    assert duplicate.duplicate_count == 2
    assert (duplicate.first_log_time, duplicate.last_log_time) == (10, 12)
    assert stream.payload_conflicts == ()
    assert stream.confirmed_gaps == ()
    assert stream.unique_sequence_count == 2
    assert stream.observation_count == 4


def test_duplicate_across_input_fragments_is_aggregated() -> None:
    first_fragment = [
        observe("p", "/t", 1, 10, b"same"),
        observe("p", "/t", 2, 20, b"two"),
    ]
    second_fragment = [
        observe("p", "/t", 1, 11, b"same"),
        observe("p", "/t", 3, 30, b"three"),
    ]
    merged = analyze_continuity(
        first_fragment + second_fragment, coverage=full_coverage("/t")
    ).for_stream("p", "/t")
    swapped = analyze_continuity(
        second_fragment + first_fragment, coverage=full_coverage("/t")
    ).for_stream("p", "/t")
    assert merged == swapped
    assert merged.duplicate_delivery_count == 1
    assert merged.observation_count == 4
    assert merged.unique_sequence_count == 3


def test_same_sequence_different_payload_is_a_conflict() -> None:
    observations = [
        observe("p", "/t", 1, 10, b"first", channel_id=1),
        observe("p", "/t", 1, 10, b"second", channel_id=2),
        observe("p", "/t", 2, 20, b"next", channel_id=1),
    ]
    stream = analyze_continuity(
        observations, coverage=full_coverage("/t")
    ).for_stream("p", "/t")
    assert len(stream.payload_conflicts) == 1
    conflict = stream.payload_conflicts[0]
    assert conflict.sequence == 1
    assert len(conflict.observations) == 2
    assert {observation.payload_digest for observation in conflict.observations} == {
        MessageObservation.from_payload("p", "/t", 1, 10, b"first").payload_digest,
        MessageObservation.from_payload("p", "/t", 1, 10, b"second").payload_digest,
    }
    assert all(observation.count == 1 for observation in conflict.observations)
    # A conflict is not a duplicate and does not break continuity.
    assert stream.duplicate_deliveries == ()
    assert stream.confirmed_gaps == ()
    assert stream.unique_sequence_count == 2


def test_duplicate_and_conflict_together_keep_both_payloads() -> None:
    observations = [
        observe("p", "/t", 1, 10, b"a"),
        observe("p", "/t", 1, 11, b"a"),
        observe("p", "/t", 1, 12, b"b"),
        observe("p", "/t", 2, 20, b"c"),
    ]
    stream = analyze_continuity(
        observations, coverage=full_coverage("/t")
    ).for_stream("p", "/t")
    assert stream.duplicate_delivery_count == 1
    assert len(stream.payload_conflicts) == 1
    counts = {
        observation.payload_digest: observation.count
        for observation in stream.payload_conflicts[0].observations
    }
    assert sorted(counts.values()) == [1, 2]


def test_same_topic_with_two_producers_is_counted_separately() -> None:
    observations = [
        observe("cam-a", "/image", 1, 10),
        observe("cam-a", "/image", 2, 20),
        observe("cam-a", "/image", 4, 30),
        observe("cam-b", "/image", 1, 15),
        observe("cam-b", "/image", 3, 25),
    ]
    report = analyze_continuity(observations, coverage=full_coverage("/image"))
    assert len(report.streams) == 2
    a = report.for_stream("cam-a", "/image")
    b = report.for_stream("cam-b", "/image")
    assert a.confirmed_missing_sequences == 1  # sequence 3
    assert b.confirmed_missing_sequences == 1  # sequence 2
    assert [(gap.producer_id, gap.missing.first) for gap in report.confirmed_gaps] == [
        ("cam-a", 3),
        ("cam-b", 2),
    ]


def test_same_producer_across_channels_keeps_one_sequence() -> None:
    observations = [
        observe("cam-a", "/image", 1, 10, channel_id=10),
        observe("cam-a", "/image", 3, 20, channel_id=11),
    ]
    stream = analyze_continuity(
        observations, coverage=full_coverage("/image")
    ).for_stream("cam-a", "/image")
    assert stream.confirmed_missing_sequences == 1


def test_channel_ids_alone_do_not_define_a_sequence() -> None:
    # Two channel ids of the same producer share a sequence; two producers sharing
    # one topic do not. Neither grouping may be derived from the channel count.
    observations = [
        observe("cam-a", "/image", 1, 10, channel_id=10),
        observe("cam-a", "/image", 2, 20, channel_id=10),
        observe("cam-a", "/image", 4, 30, channel_id=11),
        observe("cam-b", "/image", 1, 15, channel_id=10),
        observe("cam-b", "/image", 2, 25, channel_id=10),
    ]
    report = analyze_continuity(observations, coverage=full_coverage("/image"))
    assert report.for_stream("cam-a", "/image").confirmed_missing_sequences == 1
    assert report.for_stream("cam-b", "/image").confirmed_missing_sequences == 0


def test_observation_gap_outside_coverage_is_not_a_confirmed_drop() -> None:
    observations = [
        observe("p", "/t", 1, 10),
        observe("p", "/t", 5, 50),
    ]
    report = analyze_continuity(
        observations,
        coverage=[CoverageInterval("/t", 0, 20), CoverageInterval("/t", 40, 100)],
    )
    stream = report.for_stream("p", "/t")
    assert stream.confirmed_gaps == ()
    assert len(stream.observation_gaps) == 1
    gap = stream.observation_gaps[0]
    assert isinstance(gap, ObservationGap)
    assert gap.reason == "outside_coverage"
    assert gap.missing_count == 3


def test_incomplete_coverage_is_an_observation_gap() -> None:
    observations = [
        observe("p", "/t", 1, 10),
        observe("p", "/t", 5, 50),
    ]
    stream = analyze_continuity(
        observations,
        coverage=[
            CoverageInterval("/t", 0, 100, complete=False),
        ],
    ).for_stream("p", "/t")
    assert stream.confirmed_gaps == ()
    assert stream.observation_gaps[0].reason == "incomplete_coverage"


def test_complete_coverage_must_reach_both_bounding_messages() -> None:
    observations = [
        observe("p", "/t", 1, 10),
        observe("p", "/t", 5, 50),
    ]
    # Complete up to log time 49 only: the window's end is not covered.
    stream = analyze_continuity(
        observations,
        coverage=[
            CoverageInterval("/t", 0, 49),
            CoverageInterval("/t", 0, 100, complete=False),
        ],
    ).for_stream("p", "/t")
    assert stream.confirmed_gaps == ()
    assert stream.observation_gaps[0].reason == "incomplete_coverage"


def test_coverage_can_be_scoped_to_one_producer() -> None:
    observations = [
        observe("cam-a", "/image", 1, 10),
        observe("cam-a", "/image", 3, 30),
        observe("cam-b", "/image", 1, 10),
        observe("cam-b", "/image", 3, 30),
    ]
    report = analyze_continuity(
        observations,
        coverage=[
            CoverageInterval("/image", 0, 100, producer_id="cam-a"),
        ],
    )
    assert report.for_stream("cam-a", "/image").confirmed_missing_sequences == 1
    assert report.for_stream("cam-b", "/image").observation_gaps[0].reason == "outside_coverage"


def test_without_coverage_everything_is_an_observation_gap() -> None:
    observations = [observe("p", "/t", 1, 10), observe("p", "/t", 9, 90)]
    stream = analyze_continuity(observations).for_stream("p", "/t")
    assert stream.confirmed_gaps == ()
    assert len(stream.observation_gaps) == 1
    assert stream.observation_gaps[0].reason == "outside_coverage"


def test_large_backwards_jump_without_restart_is_undecidable() -> None:
    modulus = 8
    observations = [
        observe("p", "/t", 1, 10),
        observe("p", "/t", 2, 20),
        observe("p", "/t", 0, 30),
    ]
    stream = analyze_continuity(
        observations,
        coverage=full_coverage("/t"),
        sequence_modulus=modulus,
    ).for_stream("p", "/t")
    assert len(stream.undecidable_segments) == 1
    assert stream.undecidable_segments[0].reason == "unexplained_sequence_reset"
    assert stream.undecidable_segments[0].after_sequence == 2
    assert stream.undecidable_segments[0].before_sequence == 0
    # A small backwards step is not claimed as a missing range.
    assert stream.confirmed_gaps == ()


def test_forward_jump_larger_than_half_modulus_is_undecidable() -> None:
    modulus = 8
    observations = [observe("p", "/t", 0, 10), observe("p", "/t", 6, 20)]
    stream = analyze_continuity(
        observations,
        coverage=full_coverage("/t"),
        sequence_modulus=modulus,
    ).for_stream("p", "/t")
    assert stream.confirmed_gaps == ()
    assert stream.undecidable_segments[0].reason == "sequence_jump_too_large"


def test_gap_at_restart_boundary_is_not_confirmed() -> None:
    observations = [
        observe("p", "/t", 100, 10),
        observe("p", "/t", 105, 20),
        observe("p", "/t", 4, 30),
        observe("p", "/t", 6, 40),
    ]
    stream = analyze_continuity(
        observations,
        coverage=full_coverage("/t"),
        restarts=[RestartEvidence("p", 25)],
    ).for_stream("p", "/t")
    # The pre-restart sequence gap is confirmed; the post-restart 4 -> 6 gap is too.
    assert [gap.missing.first for gap in stream.confirmed_gaps] == [101, 5]
    # No phantom gap or reset is reported across the restart itself.
    assert stream.undecidable_segments == ()


def test_input_order_and_fragment_concatenation_do_not_change_counts() -> None:
    fragments = [
        [
            observe("p", "/t", 1, 10),
            observe("p", "/t", 4, 40),
            observe("q", "/u", 1, 15),
        ],
        [
            observe("p", "/t", 2, 20),
            observe("p", "/t", 3, 30),
            observe("q", "/u", 3, 35),
        ],
        [
            observe("p", "/t", 4, 40),  # duplicate across fragments
            observe("q", "/u", 2, 25),
        ],
    ]
    coverage = [CoverageInterval("/t", 0, 100), CoverageInterval("/u", 0, 100)]
    reference = analyze_continuity(
        [item for fragment in fragments for item in fragment], coverage=coverage
    )

    shuffled = [item for fragment in fragments for item in fragment]
    random.Random(7).shuffle(shuffled)
    assert analyze_continuity(shuffled, coverage=coverage) == reference

    for order in ([0, 1, 2], [2, 0, 1], [1, 2, 0], [2, 1, 0]):
        merged = [item for index in order for item in fragments[index]]
        assert analyze_continuity(merged, coverage=coverage) == reference


def test_coverage_and_restart_input_order_does_not_matter() -> None:
    observations = [
        observe("p", "/t", 10, 10),
        observe("p", "/t", 11, 20),
        observe("p", "/t", 0, 30),
        observe("p", "/t", 3, 40),
    ]
    coverage_a = [CoverageInterval("/t", 0, 25), CoverageInterval("/t", 25, 100)]
    coverage_b = list(reversed(coverage_a))
    restarts = [RestartEvidence("p", 25), RestartEvidence("p", 5)]
    reference = analyze_continuity(observations, coverage=coverage_a, restarts=restarts)
    assert (
        analyze_continuity(observations, coverage=coverage_b, restarts=reversed(restarts))
        == reference
    )


def test_same_log_time_across_wraparound_stays_forward() -> None:
    modulus = 8
    observations = [
        observe("p", "/t", 7, 10),
        observe("p", "/t", 0, 10),
    ]
    stream = analyze_continuity(
        observations,
        coverage=full_coverage("/t"),
        sequence_modulus=modulus,
    ).for_stream("p", "/t")
    assert len(stream.wraparounds) == 1
    assert stream.undecidable_segments == ()


def test_streams_are_sorted_and_lookup_is_explicit() -> None:
    observations = [
        observe("z", "/t", 1, 1),
        observe("a", "/u", 1, 1),
        observe("a", "/t", 1, 1),
    ]
    report = analyze_continuity(observations)
    assert [(stream.producer_id, stream.topic) for stream in report.streams] == [
        ("a", "/t"),
        ("a", "/u"),
        ("z", "/t"),
    ]
    with pytest.raises(KeyError):
        report.for_stream("missing", "/t")


def test_sequence_outside_modulus_is_rejected() -> None:
    with pytest.raises(ValueError, match="outside"):
        analyze_continuity([observe("p", "/t", 8, 1)], sequence_modulus=8)
    with pytest.raises(ValueError, match="outside"):
        analyze_continuity([observe("p", "/t", -1, 1)])


def test_invalid_modulus_and_coverage_are_rejected() -> None:
    with pytest.raises(ValueError, match="sequence_modulus"):
        analyze_continuity([], sequence_modulus=1)
    with pytest.raises(ValueError, match="end_time"):
        analyze_continuity(
            [observe("p", "/t", 1, 1)],
            coverage=[CoverageInterval("/t", 10, 5)],
        )


def test_check_continuity_is_an_alias() -> None:
    observations = [observe("p", "/t", 1, 10), observe("p", "/t", 3, 30)]
    assert check_continuity(
        observations, coverage=full_coverage("/t")
    ) == analyze_continuity(observations, coverage=full_coverage("/t"))


def _write_mcap(
    messages: Iterable[Tuple[int, int, int, bytes]],
    *,
    channels: int = 1,
) -> BytesIO:
    """Write (channel_index, sequence, log_time, payload) tuples to an MCAP."""
    buffer = BytesIO()
    writer = Writer(
        buffer,
        compression=CompressionType.NONE,
        chunk_size=64,
        index_types=IndexType.ALL,
    )
    writer.start()
    channel_ids = [
        writer.register_channel("/image", "raw", 0) for _ in range(channels)
    ]
    for channel_index, sequence, log_time, payload in messages:
        writer.add_message(
            channel_ids[channel_index],
            log_time=log_time,
            data=payload,
            publish_time=log_time,
            sequence=sequence,
        )
    writer.finish()
    buffer.seek(0)
    return buffer


def test_reader_adapter_groups_by_producer_and_topic() -> None:
    buffer = _write_mcap(
        [
            (0, 1, 10, b"a1"),
            (0, 2, 20, b"a2"),
            (0, 4, 30, b"a4"),
            (1, 1, 15, b"b1"),
            (1, 3, 25, b"b3"),
        ],
        channels=2,
    )
    reader = make_reader(buffer)
    observations = list(
        iter_message_observations(reader, {1: "cam-a", 2: "cam-b"})
    )
    assert len(observations) == 5
    assert {observation.producer_id for observation in observations} == {
        "cam-a",
        "cam-b",
    }
    report = analyze_continuity(observations, coverage=full_coverage("/image"))
    assert report.for_stream("cam-a", "/image").confirmed_missing_sequences == 1
    assert report.for_stream("cam-b", "/image").confirmed_missing_sequences == 1


def test_reader_adapter_accepts_callable_and_carries_channel_ids() -> None:
    buffer = _write_mcap([(0, 1, 10, b"x"), (0, 2, 20, b"y")], channels=1)
    reader = make_reader(buffer)
    observations = list(
        iter_message_observations(
            reader, lambda channel, message: "producer-{}".format(channel.id)
        )
    )
    assert all(
        observation.channel_id == observations[0].channel_id for observation in observations
    )
    assert observations[0].producer_id.startswith("producer-")


def test_reader_adapter_can_split_one_channel_by_message() -> None:
    # A single channel id carrying two producers' counters must still split by
    # producer. The resolver receives the message so it can inspect the payload.
    buffer = _write_mcap(
        [
            (0, 1, 10, b"cam-a"),
            (0, 2, 20, b"cam-a"),
            (0, 2, 30, b"cam-a"),
            (0, 1, 15, b"cam-b"),
        ]
    )
    reader = make_reader(buffer)
    observations = list(
        iter_message_observations(reader, lambda channel, message: message.data.decode())
    )
    report = analyze_continuity(observations, coverage=full_coverage("/image"))
    assert report.for_stream("cam-a", "/image").confirmed_missing_sequences == 0
    assert report.for_stream("cam-a", "/image").duplicate_delivery_count == 1
    assert report.for_stream("cam-b", "/image").confirmed_missing_sequences == 0


def test_reader_adapter_rejects_unmapped_channel() -> None:
    buffer = _write_mcap([(0, 1, 10, b"x")], channels=1)
    reader = make_reader(buffer)
    with pytest.raises(ValueError, match="no producer identity"):
        list(iter_message_observations(reader, {}))
