"""tests for metadata range queries and business priority arbitration."""

from io import BytesIO
from pathlib import Path
from typing import Optional, Type, Union

import pytest

from mcap.metadata_query import (
    ConflictKind,
    MetadataClaim,
    MetadataQuery,
    MetadataQueryResult,
    PriorityRule,
    TimeDomain,
    TimeRange,
    execute_metadata_query,
)
from mcap.reader import McapReader, NonSeekingReader, SeekingReader
from mcap.records import Metadata
from mcap.writer import CompressionType, IndexType, Writer

READER_SUBCLASSES = [SeekingReader, NonSeekingReader]
AnyReaderSubclass = Union[Type[SeekingReader], Type[NonSeekingReader]]


def write_mcap(
    output,
    *,
    metadata_records,
    messages_per_gap=0,
    chunk_size=128,
    index_types=IndexType.ALL,
):
    """Writes metadata records separated by chunk boundaries."""
    writer = Writer(
        output,
        compression=CompressionType.NONE,
        chunk_size=chunk_size,
        index_types=index_types,
    )
    writer.start()
    channel_id = writer.register_channel("/t1", "json", 0)
    other_channel_id = writer.register_channel("/t2", "json", 0)
    time = 0
    for record in metadata_records:
        for _ in range(messages_per_gap):
            time += 1
            writer.add_message(channel_id, time, b"{}", time)
            writer.add_message(other_channel_id, time, b"{}", time)
        writer.add_metadata(record[0], record[1])
    writer.finish()


def make_parser(claim_specs):
    """claim_specs maps a Metadata record name/key payload to a MetadataClaim."""

    def parser(order: int, record: Metadata) -> Optional[MetadataClaim]:
        for spec in claim_specs:
            if spec.name == record.name and spec.metadata == record.metadata:
                return spec
        return None

    return parser


def values_for(channel, key, result: MetadataQueryResult):
    return [
        (segment.start_time, segment.end_time, segment.value)
        for segment in result.segments
        if segment.channel == channel and segment.key == key
    ]


def test_business_priority_overrides_physical_order():
    # c1 is written first but the business rule says c2 (written later, lower
    # priority) loses; physical order must never decide the effective value.
    c1 = MetadataClaim(
        name="cal",
        metadata={"offset": "10"},
        valid_from=0,
        valid_to=100,
        claim_id="c1",
    )
    c2 = MetadataClaim(
        name="cal",
        metadata={"offset": "20"},
        valid_from=20,
        valid_to=80,
        claim_id="c2",
    )
    result = MetadataQuery(
        [c1, c2],
        priority_rules=[PriorityRule(higher="c1", lower="c2")],
        channel_universe=["/t1"],
    ).run()
    assert values_for("/t1", "offset", result) == [
        (0, 20, "10"),
        (20, 80, "10"),
        (80, 100, "10"),
    ]
    assert result.conflicts == ()


def test_common_value_and_overlap_conflict_interval():
    c1 = MetadataClaim(
        name="cal", metadata={"k": "a"}, valid_from=0, valid_to=10, claim_id="c1"
    )
    c2 = MetadataClaim(
        name="cal", metadata={"k": "a"}, valid_from=5, valid_to=15, claim_id="c2"
    )
    c3 = MetadataClaim(
        name="cal", metadata={"k": "b"}, valid_from=12, valid_to=20, claim_id="c3"
    )
    result = MetadataQuery(
        [c1, c2, c3], channel_universe=["/t1"]
    ).run()
    # Independent sources agreeing on a value share a joint, conflict-free value.
    assert values_for("/t1", "k", result) == [
        (0, 5, "a"),
        (5, 10, "a"),
        (10, 12, "a"),
        (12, 15, None),
        (15, 20, "b"),
    ]
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.kind == ConflictKind.OVERLAP
    assert (conflict.start_time, conflict.end_time) == (12, 15)
    assert conflict.values == {"a": ("c2",), "b": ("c3",)}
    assert set(conflict.claim_ids) == {"c2", "c3"}


def test_conflict_isolated_with_other_metadata_still_queryable():
    clashing_a = MetadataClaim(
        name="cal",
        metadata={"k": "a", "other": "x"},
        valid_from=0,
        valid_to=10,
        claim_id="a",
    )
    clashing_b = MetadataClaim(
        name="cal",
        metadata={"k": "b"},
        valid_from=4,
        valid_to=8,
        claim_id="b",
    )
    calm = MetadataClaim(
        name="cal",
        metadata={"other": "x"},
        valid_from=0,
        valid_to=10,
        claim_id="calm",
    )
    result = MetadataQuery(
        [clashing_a, clashing_b, calm], channel_universe=["/t1"]
    ).run()
    key_conflicts = [c for c in result.conflicts if c.key == "k"]
    assert len(key_conflicts) == 1
    assert (key_conflicts[0].start_time, key_conflicts[0].end_time) == (4, 8)
    # The other key never participates in the conflict and stays queryable as one
    # continuous segment even across the conflicting range.
    assert values_for("/t1", "other", result) == [(0, 10, "x")]
    assert all(c.key != "other" for c in result.conflicts)
    # Outside the clash the key itself resolves normally.
    assert values_for("/t1", "k", result) == [
        (0, 4, "a"),
        (4, 8, None),
        (8, 10, "a"),
    ]


def test_priority_cycle_isolated_to_overlap_range():
    c1 = MetadataClaim(
        name="cal", metadata={"k": "a"}, valid_from=0, valid_to=10, claim_id="c1"
    )
    c2 = MetadataClaim(
        name="cal", metadata={"k": "b"}, valid_from=5, valid_to=15, claim_id="c2"
    )
    c3 = MetadataClaim(
        name="cal", metadata={"k": "c"}, valid_from=6, valid_to=9, claim_id="c3"
    )
    rules = [
        PriorityRule("c2", "c1"),
        PriorityRule("c3", "c2"),
        PriorityRule("c1", "c3"),
    ]
    result = MetadataQuery(
        [c1, c2, c3], priority_rules=rules, channel_universe=["/t1"]
    ).run()
    assert [
        (c.start_time, c.end_time, c.kind) for c in result.conflicts
    ] == [(6, 9, ConflictKind.CYCLE)]
    assert set(result.conflicts[0].claim_ids) == {"c1", "c2", "c3"}
    # Ranges outside the cycle are still arbitrated by the same rules.
    resolved = [
        (segment.start_time, segment.end_time, segment.value)
        for segment in result.segments
        if segment.conflict is None
    ]
    assert (0, 5, "a") in resolved
    assert (5, 6, "b") in resolved
    assert (9, 10, "b") in resolved
    assert (10, 15, "b") in resolved


def test_channel_scoping_does_not_share_calibrations():
    only_t2 = MetadataClaim(
        name="cal",
        metadata={"k": "x"},
        channels=["/t2"],
        valid_from=0,
        valid_to=10,
        claim_id="only-t2",
    )
    wildcard = MetadataClaim(
        name="cal",
        metadata={"k": "w"},
        channels=None,
        valid_from=0,
        valid_to=10,
        claim_id="wild",
    )
    query = MetadataQuery(
        [only_t2, wildcard], channel_universe=["/t1", "/t2"]
    )
    t1 = query.run(topics="/t1")
    assert values_for("/t1", "k", t1) == [(0, 10, "w")]
    t2 = query.run(topics="/t2")
    # Both declarations apply on /t2 and disagree without a rule: conflict there,
    # but /t1 keeps its own value.
    assert t2.conflicts[0].channel == "/t2"
    assert all(segment.channel == "/t2" for segment in t2.segments)
    assert values_for("/t2", "k", t2) == [(0, 10, None)]


def test_log_and_publish_time_ranges_use_their_own_axis():
    log_claim = MetadataClaim(
        name="cal",
        metadata={"k": "log"},
        valid_from=10,
        valid_to=20,
        time_domain=TimeDomain.LOG_TIME,
        claim_id="lc",
    )
    publish_claim = MetadataClaim(
        name="cal",
        metadata={"k": "pub"},
        valid_from=100,
        valid_to=200,
        time_domain=TimeDomain.PUBLISH_TIME,
        claim_id="pc",
    )
    query = MetadataQuery(
        [log_claim, publish_claim], channel_universe=["/t1"]
    )
    result = query.run(
        log_time=TimeRange(starting_at=0, ending_before=1000),
        publish_time=TimeRange(starting_at=150),
    )
    log_segments = [s for s in result.segments if s.time_domain == TimeDomain.LOG_TIME]
    pub_segments = [
        s for s in result.segments if s.time_domain == TimeDomain.PUBLISH_TIME
    ]
    assert [(s.start_time, s.end_time, s.value) for s in log_segments] == [
        (10, 20, "log")
    ]
    # Publish range [150, +inf) clips the publish claim; it is never filtered by
    # the log-time bounds.
    assert [(s.start_time, s.end_time, s.value) for s in pub_segments] == [
        (150, 200, "pub")
    ]


def test_retraction_removes_only_its_own_projection():
    c1 = MetadataClaim(
        name="cal", metadata={"k": "a"}, valid_from=0, valid_to=10, claim_id="c1"
    )
    c2 = MetadataClaim(
        name="cal", metadata={"k": "a"}, valid_from=5, valid_to=15, claim_id="c2"
    )
    c3 = MetadataClaim(
        name="cal", metadata={"k": "b"}, valid_from=10, valid_to=20, claim_id="c3"
    )
    result = MetadataQuery(
        [c1, c2, c3],
        retractions=[
            MetadataClaim(
                name="cal",
                metadata={"k": "b"},
                valid_from=10,
                valid_to=20,
                claim_id="c3",
            )
        ],
        channel_universe=["/t1"],
    ).run()
    assert result.conflicts == ()
    assert values_for("/t1", "k", result) == [
        (0, 5, "a"),
        (5, 10, "a"),
        (10, 15, "a"),
    ]
    # The independent same-valued source c2 still backs the value after c1 ends.
    assert {claim.claim_id for claim in result.declarations} == {"c1", "c2"}


def test_content_based_retraction_keeps_independent_same_value_evidence():
    c1 = MetadataClaim(
        name="cal", metadata={"k": "a"}, valid_from=0, valid_to=10, claim_id="c1"
    )
    c2 = MetadataClaim(
        name="cal", metadata={"k": "a"}, valid_from=0, valid_to=10, claim_id="c2"
    )
    # No claim id on the retraction: it matches name/payload/channels/interval and
    # removes one physical projection, leaving the other source in effect.
    result = MetadataQuery(
        [c1, c2],
        retractions=[
            MetadataClaim(
                name="cal", metadata={"k": "a"}, valid_from=0, valid_to=10
            )
        ],
        channel_universe=["/t1"],
    ).run()
    assert values_for("/t1", "k", result) == [(0, 10, "a")]
    assert len(result.declarations) == 1


def test_query_continuity_across_chunk_boundaries(tmp_path: Path):
    records = [
        ("cal", {"k": "a"}),
        ("cal", {"k": "b"}),
        ("cal", {"k": "a"}),
        ("cal", {"k": "a"}),
    ]
    path = tmp_path / "chunked.mcap"
    with open(path, "wb") as stream:
        write_mcap(stream, metadata_records=records, messages_per_gap=4)
    c1 = MetadataClaim(
        name="cal",
        metadata={"k": "a"},
        valid_from=0,
        valid_to=100,
        claim_id="c1",
    )
    c2 = MetadataClaim(
        name="cal",
        metadata={"k": "b"},
        valid_from=30,
        valid_to=60,
        claim_id="c2",
    )
    with open(path, "rb") as stream:
        reader = SeekingReader(stream)
        summary = reader.get_summary()
        assert summary is not None and len(summary.chunk_indexes) > 1
        result = reader.query_metadata(
            make_parser([c1, c2]),
            priority_rules=[PriorityRule("c1", "c2")],
            topics="/t1",
        )
    assert values_for("/t1", "k", result) == [
        (0, 30, "a"),
        (30, 60, "a"),
        (60, 100, "a"),
    ]
    # Every original Metadata record is exposed as evidence, even though the three
    # "a" records share one business claim id.
    assert len(result.declarations) == 4
    assert {d.claim_id for d in result.declarations} == {"c1", "c2"}


@pytest.mark.parametrize("reader_cls", READER_SUBCLASSES)
def test_reader_query_metadata_end_to_end(
    tmp_path: Path, reader_cls: AnyReaderSubclass
):
    records = [("cal", {"k": "a"}), ("cal", {"k": "b"})]
    path = tmp_path / "meta.mcap"
    with open(path, "wb") as stream:
        write_mcap(stream, metadata_records=records, messages_per_gap=2)
    c1 = MetadataClaim(
        name="cal",
        metadata={"k": "a"},
        valid_from=0,
        valid_to=10,
        claim_id="c1",
    )
    c2 = MetadataClaim(
        name="cal",
        metadata={"k": "b"},
        valid_from=5,
        valid_to=15,
        claim_id="c2",
    )
    with open(path, "rb") as stream:
        reader: McapReader = reader_cls(stream)
        result = reader.query_metadata(
            make_parser([c1, c2]),
            priority_rules=[PriorityRule("c2", "c1")],
            log_time=TimeRange(starting_at=2, ending_before=12),
        )
    assert values_for("/t1", "k", result) == [
        (2, 5, "a"),
        (5, 10, "b"),
        (10, 12, "b"),
    ]
    assert values_for("/t2", "k", result) == [
        (2, 5, "a"),
        (5, 10, "b"),
        (10, 12, "b"),
    ]
    assert result.conflicts == ()


def test_reader_query_metadata_without_summary(tmp_path: Path):
    records = [("cal", {"k": "a"}), ("cal", {"k": "b"})]
    path = tmp_path / "no_summary.mcap"
    with open(path, "wb") as stream:
        write_mcap(
            stream,
            metadata_records=records,
            messages_per_gap=1,
            index_types=IndexType.NONE,
        )
    c1 = MetadataClaim(
        name="cal", metadata={"k": "a"}, valid_from=0, valid_to=10, claim_id="c1"
    )
    c2 = MetadataClaim(
        name="cal", metadata={"k": "b"}, valid_from=5, valid_to=15, claim_id="c2"
    )
    with open(path, "rb") as stream:
        result = SeekingReader(stream).query_metadata(
            make_parser([c1, c2]), topics="/t1"
        )
    assert values_for("/t1", "k", result) == [
        (0, 5, "a"),
        (5, 10, None),
        (10, 15, "b"),
    ]
    assert len(result.conflicts) == 1


def test_execute_metadata_query_with_retraction_parser():
    records = [
        Metadata(name="cal", metadata={"k": "a"}),
        Metadata(name="cal", metadata={"k": "b"}),
        Metadata(name="retract", metadata={"claim_id": "c2"}),
    ]
    specs = [
        MetadataClaim(
            name="cal",
            metadata={"k": "a"},
            valid_from=0,
            valid_to=10,
            claim_id="c1",
        ),
        MetadataClaim(
            name="cal",
            metadata={"k": "b"},
            valid_from=5,
            valid_to=15,
            claim_id="c2",
        ),
    ]

    def parser(order: int, record: Metadata):
        return make_parser(specs)(order, record)

    def retraction_parser(order: int, record: Metadata):
        if record.name == "retract":
            return MetadataClaim(
                name="cal",
                metadata={"k": "b"},
                valid_from=5,
                valid_to=15,
                claim_id=record.metadata["claim_id"],
            )
        return None

    result = execute_metadata_query(
        records,
        parser=parser,
        retraction_parser=retraction_parser,
        channel_universe=["/t1"],
    )
    # With c2 withdrawn, c1 alone covers the range in one continuous segment.
    assert values_for("/t1", "k", result) == [(0, 10, "a")]
    assert {d.claim_id for d in result.declarations} == {"c1"}


def test_time_range_bound_spellings_and_validation():
    claim = MetadataClaim(
        name="cal", metadata={"k": "a"}, valid_from=0, valid_to=100, claim_id="c1"
    )
    query = MetadataQuery([claim], channel_universe=["/t1"])
    assert values_for(
        "/t1",
        "k",
        query.run(log_time=TimeRange(starting_at=10, ending_before=20)),
    ) == [(10, 20, "a")]
    assert values_for(
        "/t1",
        "k",
        query.run(log_time=TimeRange(starting_after=10, ending_at=20)),
    ) == [(11, 21, "a")]
    with pytest.raises(ValueError, match="at most one"):
        query.run(
            log_time=TimeRange(starting_at=1, starting_after=2),
        )
    with pytest.raises(ValueError, match="end time"):
        query.run(log_time=TimeRange(starting_at=10, ending_before=9))


def test_unknown_priority_rule_id_rejected():
    claim = MetadataClaim(
        name="cal", metadata={"k": "a"}, valid_from=0, claim_id="c1"
    )
    with pytest.raises(ValueError, match="unknown claim id"):
        MetadataQuery(
            [claim],
            priority_rules=[PriorityRule("c1", "ghost")],
            channel_universe=["/t1"],
        )


def test_query_requires_claim_ids():
    with pytest.raises(ValueError, match="claim_id"):
        MetadataQuery(
            [MetadataClaim(name="cal", metadata={"k": "a"})],
            channel_universe=["/t1"],
        )


def test_in_memory_stream_round_trip():
    buffer = BytesIO()
    write_mcap(
        buffer,
        metadata_records=[("cal", {"k": "a"}), ("cal", {"k": "b"})],
        messages_per_gap=2,
    )
    c1 = MetadataClaim(
        name="cal", metadata={"k": "a"}, valid_from=0, valid_to=10, claim_id="c1"
    )
    c2 = MetadataClaim(
        name="cal", metadata={"k": "b"}, valid_from=5, valid_to=15, claim_id="c2"
    )
    result = NonSeekingReader(BytesIO(buffer.getvalue())).query_metadata(
        make_parser([c1, c2]), topics="/t1"
    )
    assert len(result.segments) > 0
    assert {segment.channel for segment in result.segments} == {"/t1"}
