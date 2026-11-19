"""Tests for frame transform collection and point projection."""

import json
from io import BytesIO
from typing import Any, Dict, List, Optional

import pytest

from mcap.reader import NonSeekingReader, SeekingReader
from mcap.records import Channel, Message, Schema
from mcap.transforms import (
    FrameTransform,
    Quaternion,
    RigidTransform,
    TransformTimeline,
    Vector3,
)
from mcap.writer import Writer

NS = 1_000_000_000


def transform(
    parent: str,
    child: str,
    time: int,
    x: float = 0.0,
    y: float = 0.0,
    z: float = 0.0,
    log_time: Optional[int] = None,
    sequence: int = 0,
    channel_id: int = 0,
    topic: str = "/tf",
) -> FrameTransform:
    return FrameTransform(
        parent_frame_id=parent,
        child_frame_id=child,
        sample_time=time,
        translation=Vector3(x, y, z),
        rotation=Quaternion.identity(),
        log_time=time if log_time is None else log_time,
        sequence=sequence,
        channel_id=channel_id,
        topic=topic,
    )


def approx(vector: Optional[Vector3], expected: List[float]) -> bool:
    assert vector is not None
    return all(
        abs(actual - wanted) < 1e-9
        for actual, wanted in zip(vector.as_tuple(), expected)
    )


def write_mcap(records: List[Dict[str, Any]], **writer_kwargs: Any) -> BytesIO:
    """Write ``records`` (each with topic/log_time/data) into an in-memory MCAP."""
    output = BytesIO()
    writer = Writer(output, **writer_kwargs)
    writer.start()
    channels: Dict[str, int] = {}
    for record in records:
        topic = record["topic"]
        if topic not in channels:
            schema_id = 0
            if record.get("schema_name"):
                schema_id = writer.register_schema(
                    record["schema_name"], "jsonschema", b"{}"
                )
            channels[topic] = writer.register_channel(topic, "json", schema_id)
        writer.add_message(
            channels[topic],
            log_time=record["log_time"],
            data=json.dumps(record["data"]).encode("utf8"),
            publish_time=record.get("log_time", 0),
            sequence=record.get("sequence", 0),
        )
    writer.finish()
    output.seek(0)
    return output


def transform_record(
    parent: str,
    child: str,
    sample_time: int,
    translation: List[float],
    log_time: Optional[int] = None,
    **payload: Any,
) -> Dict[str, Any]:
    return {
        "topic": "/tf",
        "log_time": sample_time if log_time is None else log_time,
        "data": {
            "timestamp": {
                "sec": sample_time // NS,
                "nsec": sample_time % NS,
            },
            "parent_frame_id": parent,
            "child_frame_id": child,
            "translation": {"x": translation[0], "y": translation[1], "z": translation[2]},
            "rotation": {"x": 0, "y": 0, "z": 0, "w": 1},
            **payload,
        },
    }


def point_record(
    topic: str,
    sample_time: int,
    frame_id: str,
    points: List[List[float]],
    header_time: bool = True,
    **extra: Any,
) -> Dict[str, Any]:
    data: Dict[str, Any] = {"frame_id": frame_id, "points": points, **extra}
    if header_time:
        data["timestamp"] = {"sec": sample_time // NS, "nsec": sample_time % NS}
    return {"topic": topic, "log_time": sample_time, "data": data}


def test_linear_interpolation_and_provenance():
    timeline = TransformTimeline(
        [
            transform("map", "lidar", 0, x=0),
            transform("map", "lidar", 10 * NS, x=10),
        ]
    )
    projection = timeline.project_point(
        Vector3(0, 0, 0), "lidar", "map", 5 * NS
    )
    assert projection.status == "ok"
    assert approx(projection.position, [5, 0, 0])
    assert len(projection.candidates) == 1
    candidate = projection.candidates[0]
    assert candidate.path == ("lidar", "map")
    assert [sample.sample_time for sample in candidate.samples] == [0, 10 * NS]
    assert projection.sample_time == 5 * NS


def test_max_interval_is_respected():
    samples = [
        transform("map", "lidar", 0, x=0),
        transform("map", "lidar", 10 * NS, x=10),
    ]
    tight = TransformTimeline(samples, max_interval_ns=5 * NS)
    result = tight.project_point(Vector3(0, 0, 0), "lidar", "map", 5 * NS)
    assert result.status == "unresolved"
    assert result.position is None
    assert result.candidates[0].reason == "gap_too_large"

    loose = TransformTimeline(samples, max_interval_ns=10 * NS)
    assert loose.project_point(Vector3(0, 0, 0), "lidar", "map", 5 * NS).status == "ok"


def test_linear_does_not_extrapolate():
    timeline = TransformTimeline(
        [transform("map", "lidar", 10 * NS, x=10), transform("map", "lidar", 20 * NS, x=20)]
    )
    before = timeline.project_point(Vector3(0, 0, 0), "lidar", "map", 5 * NS)
    after = timeline.project_point(Vector3(0, 0, 0), "lidar", "map", 25 * NS)
    assert before.status == "unresolved"
    assert after.status == "unresolved"


def test_step_and_nearest_interpolation():
    samples = [
        transform("map", "lidar", 0, x=0),
        transform("map", "lidar", 10 * NS, x=10),
    ]
    step = TransformTimeline(samples, interpolation="step")
    assert approx(
        step.project_point(Vector3(0, 0, 0), "lidar", "map", 9 * NS).position,
        [0, 0, 0],
    )
    nearest = TransformTimeline(samples, interpolation="nearest")
    assert approx(
        nearest.project_point(Vector3(0, 0, 0), "lidar", "map", 6 * NS).position,
        [10, 0, 0],
    )
    assert approx(
        nearest.project_point(Vector3(0, 0, 0), "lidar", "map", 4 * NS).position,
        [0, 0, 0],
    )


def test_step_requires_max_interval_against_hold():
    timeline = TransformTimeline(
        [transform("map", "lidar", 0, x=0), transform("map", "lidar", 20 * NS, x=20)],
        interpolation="step",
        max_interval_ns=5 * NS,
    )
    assert timeline.project_point(Vector3(0, 0, 0), "lidar", "map", 9 * NS).status == "unresolved"
    assert timeline.project_point(Vector3(0, 0, 0), "lidar", "map", 3 * NS).status == "ok"


def test_reparenting_blocks_crossing():
    timeline = TransformTimeline(
        [
            transform("map", "lidar", 0, x=0),
            transform("map", "lidar", 10 * NS, x=10),
            transform("base", "lidar", 20 * NS, x=0, log_time=20 * NS),
        ]
    )
    # Before the re-parent: old edge interpolates normally.
    assert approx(
        timeline.project_point(Vector3(0, 0, 0), "lidar", "map", 5 * NS).position,
        [5, 0, 0],
    )
    # Past the last old sample, the old parent may not be held into the new one.
    after = timeline.project_point(Vector3(0, 0, 0), "lidar", "map", 15 * NS)
    assert after.status == "unresolved"


def test_reparenting_at_exact_time_uses_new_parent():
    timeline = TransformTimeline(
        [
            transform("map", "lidar", 0, x=0, log_time=0),
            transform("map", "lidar", 10 * NS, x=10, log_time=10 * NS),
            transform("base", "lidar", 10 * NS, x=0, log_time=11 * NS),
            transform("base", "lidar", 20 * NS, x=0, log_time=12 * NS),
            transform("map", "base", 0, x=100, log_time=0),
            transform("map", "base", 20 * NS, x=100, log_time=20 * NS),
        ]
    )
    result = timeline.project_point(
        Vector3(0, 0, 0), "lidar", "map", 20 * NS, order_time=20 * NS
    )
    assert result.status == "ok"
    assert approx(result.position, [100, 0, 0])
    # The old edge must not hold forward after the re-parent.
    old_only = TransformTimeline(
        [
            transform("map", "lidar", 0, x=0, log_time=0),
            transform("map", "lidar", 10 * NS, x=10, log_time=10 * NS),
            transform("base", "lidar", 10 * NS, x=0, log_time=11 * NS),
            transform("base", "lidar", 20 * NS, x=0, log_time=12 * NS),
        ]
    )
    assert (
        old_only.project_point(
            Vector3(0, 0, 0), "lidar", "map", 20 * NS, order_time=20 * NS
        ).status
        == "unresolved"
    )


def test_clock_restart_blocks_interpolation():
    timeline = TransformTimeline(
        [
            transform("map", "lidar", 0, x=0, log_time=0),
            transform("map", "lidar", 10 * NS, x=10, log_time=10 * NS),
            # Clock restarts: the next sample's time jumps back to 1s.
            transform("map", "lidar", 1 * NS, x=100, log_time=20 * NS),
            transform("map", "lidar", 11 * NS, x=110, log_time=30 * NS),
        ]
    )
    # A point logged before the restart cannot see post-restart samples, even though
    # its sample time falls inside the post-restart range.
    before = timeline.project_point(
        Vector3(0, 0, 0), "lidar", "map", 5 * NS, order_time=5 * NS
    )
    assert before.status == "ok"
    assert approx(before.position, [5, 0, 0])
    # A post-restart point at the same sample time sees only post-restart samples.


def test_clock_restart_never_extrapolates_across_boundary():
    timeline = TransformTimeline(
        [
            transform("map", "lidar", 0, x=0, log_time=0),
            transform("map", "lidar", 10 * NS, x=10, log_time=10 * NS),
            transform("map", "lidar", 1 * NS, x=100, log_time=20 * NS),
        ]
    )
    # In the second epoch, 6s is past the last sample: no holding or extrapolation.
    result = timeline.project_point(
        Vector3(0, 0, 0), "lidar", "map", 6 * NS, order_time=25 * NS
    )
    assert result.status == "unresolved"


def test_conflicting_paths_are_retained():
    timeline = TransformTimeline(
        [
            transform("map", "base", 0, x=0),
            transform("base", "lidar", 0, x=1),
            transform("map", "lidar", 0, x=5),
        ]
    )
    projection = timeline.project_point(Vector3(0, 0, 0), "lidar", "map", 0)
    assert projection.conflict
    assert projection.status == "conflict"
    assert projection.position is None
    assert len(projection.positions) == 2
    assert {round(p.x) for p in projection.positions} == {1, 5}
    assert {tuple(candidate.path) for candidate in projection.candidates} == {
        ("lidar", "map"),
        ("lidar", "base", "map"),
    }
    # Every conflicting alternative still reports the records it used.
    assert len(projection.transforms) == 3


def test_agreeing_multiple_paths_are_not_conflicts():
    timeline = TransformTimeline(
        [
            transform("map", "base", 0, x=0),
            transform("base", "lidar", 0, x=1),
            transform("map", "other", 0, x=0),
            transform("other", "lidar", 0, x=1),
        ]
    )
    projection = timeline.project_point(Vector3(0, 0, 0), "lidar", "map", 0)
    assert projection.status == "ok"
    assert approx(projection.position, [1, 0, 0])
    assert len(projection.candidates) == 2


def test_missing_edge_only_affects_dependent_points():
    timeline = TransformTimeline(
        [
            transform("map", "camera", 0, x=2),
            transform("map", "lidar", 0, x=5),
        ]
    )
    camera = timeline.project_point(Vector3(0, 0, 0), "camera", "map", 0)
    lidar = timeline.project_point(Vector3(0, 0, 0), "lidar", "map", 0)
    assert camera.status == "ok"
    assert approx(camera.position, [2, 0, 0])
    assert lidar.status == "ok"
    # A frame with no edge at all is unresolved, without disturbing the others.
    orphan = timeline.project_point(Vector3(0, 0, 0), "radar", "map", 0)
    assert orphan.status == "unresolved"


def test_missing_edge_does_not_contaminate_unrelated_points():
    timeline = TransformTimeline(
        [
            transform("map", "camera", 0, x=2),
            # The lidar has no transform at all.
        ]
    )
    camera = timeline.project_point(Vector3(0, 0, 0), "camera", "map", 0)
    assert camera.status == "ok"
    assert camera.missing_edges == ()


def test_deep_chain_composes_correctly():
    timeline = TransformTimeline(
        [
            transform("world", "map", 0, x=1),
            transform("map", "base", 0, x=2),
            transform("base", "lidar", 0, x=3),
        ]
    )
    own = timeline.project_point(Vector3(1, 0, 0), "lidar", "world", 0)
    assert own.status == "ok"
    assert approx(own.position, [7, 0, 0])
    assert own.candidates[0].path == ("lidar", "base", "map", "world")


def test_rotation_is_applied():
    quarter_turn = FrameTransform(
        parent_frame_id="map",
        child_frame_id="lidar",
        sample_time=0,
        translation=Vector3(0, 0, 0),
        rotation=Quaternion(0, 0, 1, 0),  # 180 degrees about z
    )
    timeline = TransformTimeline([quarter_turn])
    projected = timeline.project_point(Vector3(1, 0, 0), "lidar", "map", 0)
    assert approx(projected.position, [-1, 0, 0])


def test_order_independence():
    records = [
        transform("map", "base", 0, x=0, sequence=0),
        transform("base", "lidar", 0, x=1, sequence=1),
        transform("map", "lidar", 0, x=5, sequence=2),
        transform("map", "base", 10 * NS, x=10, sequence=3),
    ]
    forward = TransformTimeline(records)
    backward = TransformTimeline(list(reversed(records)))
    shuffled = TransformTimeline([records[2], records[0], records[3], records[1]])
    for timeline in (forward, backward, shuffled):
        result = timeline.project_point(Vector3(0, 0, 0), "base", "map", 5 * NS)
        assert result.status == "ok"
        assert approx(result.position, [5, 0, 0])


def test_reader_projection_seeking_and_nonseeking():
    records = [
        transform_record("map", "lidar", 0, [0, 0, 0]),
        transform_record("map", "lidar", 10 * NS, [10, 0, 0]),
        point_record("points", 5 * NS, "lidar", [[1, 0, 0]], timestamp=None),
    ]
    # A message without its own timestamp falls back to log_time.
    del records[-1]["data"]["timestamp"]
    buffer = write_mcap(records)

    for reader_cls in (SeekingReader, NonSeekingReader):
        buffer.seek(0)
        reader = reader_cls(buffer)
        projected = list(reader.iter_projected_messages("map"))
        assert len(projected) == 1
        message = projected[0]
        assert message.status == "ok"
        assert message.frame_id == "lidar"
        assert message.time_source == "log_time"
        assert len(message.projections) == 1
        assert approx(message.projections[0].position, [6, 0, 0])


def test_reader_uses_message_timestamp_not_log_time():
    records = [
        transform_record("map", "lidar", 0, [0, 0, 0]),
        transform_record("map", "lidar", 10 * NS, [10, 0, 0]),
        # Logged at 9s but sampled at 5s; the sample time must win.
        {
            "topic": "points",
            "log_time": 9 * NS,
            "data": {
                "frame_id": "lidar",
                "timestamp": {"sec": 5, "nsec": 0},
                "points": [[0, 0, 0]],
            },
        },
    ]
    buffer = write_mcap(records)
    buffer.seek(0)
    projected = list(SeekingReader(buffer).iter_projected_messages("map"))
    assert projected[0].sample_time == 5 * NS
    assert approx(projected[0].projections[0].position, [5, 0, 0])


def test_reader_respects_max_interval():
    records = [
        transform_record("map", "lidar", 0, [0, 0, 0]),
        transform_record("map", "lidar", 10 * NS, [10, 0, 0]),
        point_record("points", 5 * NS, "lidar", [[0, 0, 0]]),
    ]
    buffer = write_mcap(records)
    buffer.seek(0)
    projected = list(
        SeekingReader(buffer).iter_projected_messages("map", max_interval_ns=NS)
    )
    assert projected[0].projections[0].status == "unresolved"


def test_reader_retains_conflicts():
    records = [
        transform_record("map", "base", 0, [0, 0, 0]),
        transform_record("base", "lidar", 0, [1, 0, 0]),
        transform_record("map", "lidar", 0, [5, 0, 0]),
        point_record("points", 0, "lidar", [[0, 0, 0]]),
    ]
    buffer = write_mcap(records)
    buffer.seek(0)
    message = list(SeekingReader(buffer).iter_projected_messages("map"))[0]
    assert message.status == "conflict"
    assert len(message.projections[0].positions) == 2


def test_reader_transform_topic_is_configurable():
    records = [
        {
            "topic": "/frames",
            "log_time": 0,
            "data": {
                "timestamp": {"sec": 0, "nsec": 0},
                "parent_frame_id": "map",
                "child_frame_id": "lidar",
                "translation": {"x": 3, "y": 0, "z": 0},
                "rotation": {"x": 0, "y": 0, "z": 0, "w": 1},
            },
        },
        point_record("points", 0, "lidar", [[0, 0, 0]]),
    ]
    buffer = write_mcap(records)
    buffer.seek(0)
    projected = list(
        SeekingReader(buffer).iter_projected_messages(
            "map", transform_topics=["/frames"]
        )
    )
    assert approx(projected[0].projections[0].position, [3, 0, 0])


def test_reader_transform_list_message():
    records = [
        point_record("points", 0, "lidar", [[0, 0, 0]]),
        {
            "topic": "/tf",
            "log_time": 0,
            "data": {
                "transforms": [
                    {
                        "header": {"stamp": {"sec": 0, "nsec": 0}, "frame_id": "map"},
                        "child_frame_id": "lidar",
                        "transform": {
                            "translation": {"x": 2, "y": 0, "z": 0},
                            "rotation": {"x": 0, "y": 0, "z": 0, "w": 1},
                        },
                    }
                ]
            },
        },
    ]
    buffer = write_mcap(records)
    buffer.seek(0)
    projected = list(SeekingReader(buffer).iter_projected_messages("map"))
    assert approx(projected[0].projections[0].position, [2, 0, 0])


def test_malformed_transform_is_reported_not_fatal():
    records = [
        {"topic": "/tf", "log_time": 0, "data": {"parent_frame_id": "map"}},
        point_record("points", 0, "lidar", [[0, 0, 0]]),
    ]
    buffer = write_mcap(records)
    buffer.seek(0)
    timeline = SeekingReader(buffer).get_transform_timeline()
    assert timeline.samples == ()
    assert len(timeline.errors) == 1
    assert "child_frame_id" in timeline.errors[0].reason


def test_malformed_point_message_can_be_kept_or_raised():
    records = [
        {"topic": "points", "log_time": 0, "data": {"no_frame": True}},
    ]
    buffer = write_mcap(records)
    buffer.seek(0)
    kept = list(SeekingReader(buffer).iter_projected_messages("map"))
    assert len(kept) == 1
    assert kept[0].error is not None
    assert kept[0].status == "unresolved"

    buffer.seek(0)
    with pytest.raises(Exception):
        list(SeekingReader(buffer).iter_projected_messages("map", on_error="raise"))


def test_projected_message_origin_and_pose():
    records = [
        transform_record("map", "lidar", 0, [3, 0, 0]),
        {
            "topic": "points",
            "log_time": 0,
            "data": {
                "frame_id": "lidar",
                "timestamp": {"sec": 0, "nsec": 0},
                "points": [[1, 0, 0]],
                "pose": {
                    "position": {"x": 10, "y": 0, "z": 0},
                    "orientation": {"x": 0, "y": 0, "z": 0, "w": 1},
                },
            },
        },
    ]
    buffer = write_mcap(records)
    buffer.seek(0)
    message = list(SeekingReader(buffer).iter_projected_messages("map"))[0]
    projection = message.projections[0]
    assert projection.status == "ok"
    # The pose places the point within the lidar frame; the transform then moves it.
    assert approx(projection.point, [11, 0, 0])
    assert approx(projection.origin, [1, 0, 0])
    assert approx(projection.position, [14, 0, 0])


def test_invalid_interpolation_name():
    with pytest.raises(ValueError):
        TransformTimeline([], interpolation="cubic")


def test_negative_max_interval_rejected():
    with pytest.raises(ValueError):
        TransformTimeline([], max_interval_ns=-1)


def test_schema_name_identifies_transform_channel():
    records = [
        {
            "topic": "/frames",
            "schema_name": "foxglove.FrameTransform",
            "log_time": 0,
            "data": {
                "timestamp": {"sec": 0, "nsec": 0},
                "parent_frame_id": "map",
                "child_frame_id": "lidar",
                "translation": {"x": 4, "y": 0, "z": 0},
                "rotation": {"x": 0, "y": 0, "z": 0, "w": 1},
            },
        },
        point_record("points", 0, "lidar", [[0, 0, 0]]),
    ]
    buffer = write_mcap(records)
    buffer.seek(0)
    projected = list(SeekingReader(buffer).iter_projected_messages("map"))
    assert len(projected) == 1
    assert approx(projected[0].projections[0].position, [4, 0, 0])


def test_auto_detect_transform_topics_does_not_hide_points():
    records = [
        transform_record("map", "lidar", 0, [1, 0, 0]),
        point_record("points", 0, "lidar", [[0, 0, 0]]),
    ]
    buffer = write_mcap(records)
    buffer.seek(0)
    projected = list(
        SeekingReader(buffer).iter_projected_messages("map", transform_topics=None)
    )
    assert len(projected) == 1
    assert approx(projected[0].projections[0].position, [1, 0, 0])


@pytest.mark.parametrize("reader_cls", [SeekingReader, NonSeekingReader])
def test_reader_clock_restart_in_log_order(reader_cls: Any):
    """Points logged before a clock restart must not use post-restart transforms."""
    records = [
        transform_record("map", "lidar", 0, [0, 0, 0], log_time=0),
        transform_record("map", "lidar", 10 * NS, [10, 0, 0], log_time=10 * NS),
        # Restart: sample time goes back to 1s while log time keeps increasing.
        transform_record("map", "lidar", 1 * NS, [100, 0, 0], log_time=20 * NS),
        transform_record("map", "lidar", 11 * NS, [110, 0, 0], log_time=30 * NS),
        # Logged before the restart, sampled at 5s: use the pre-restart pair only.
        point_record("early", 5 * NS, "lidar", [[0, 0, 0]]),
        # Logged after the restart: use the post-restart pair only.
        {
            "topic": "late",
            "log_time": 25 * NS,
            "data": {
                "frame_id": "lidar",
                "timestamp": {"sec": 6, "nsec": 0},
                "points": [[0, 0, 0]],
            },
        },
    ]
    buffer = write_mcap(records)
    buffer.seek(0)
    projected = list(reader_cls(buffer).iter_projected_messages("map"))
    by_topic = {message.channel.topic: message for message in projected}
    assert approx(by_topic["early"].projections[0].position, [5, 0, 0])
    assert approx(by_topic["late"].projections[0].position, [105, 0, 0])
    # The early result cites only pre-restart records, the late one only post-restart.
    early_times = {sample.sample_time for sample in by_topic["early"].projections[0].transforms}
    late_times = {sample.sample_time for sample in by_topic["late"].projections[0].transforms}
    assert early_times == {0, 10 * NS}
    assert late_times == {1 * NS, 11 * NS}


@pytest.mark.parametrize("reader_cls", [SeekingReader, NonSeekingReader])
def test_reader_reparenting_prevents_hold_forward(reader_cls: Any):
    records = [
        transform_record("map", "lidar", 0, [0, 0, 0], log_time=0),
        transform_record("map", "lidar", 10 * NS, [10, 0, 0], log_time=10 * NS),
        transform_record("base", "lidar", 20 * NS, [0, 0, 0], log_time=20 * NS),
        transform_record("base", "lidar", 30 * NS, [0, 0, 0], log_time=30 * NS),
        transform_record("map", "base", 0, [100, 0, 0], log_time=0),
        transform_record("map", "base", 40 * NS, [100, 0, 0], log_time=40 * NS),
        # At 25s the lidar belongs to base, so it resolves through the new parent.
        point_record("points", 25 * NS, "lidar", [[0, 0, 0]]),
    ]
    buffer = write_mcap(records)
    buffer.seek(0)
    message = list(reader_cls(buffer).iter_projected_messages("map"))[0]
    assert message.projections[0].status == "ok"
    assert approx(message.projections[0].position, [100, 0, 0])
    assert message.projections[0].candidates[0].path == ("lidar", "base", "map")


@pytest.mark.parametrize("reader_cls", [SeekingReader, NonSeekingReader])
def test_reader_order_independence_across_chunk_layout(reader_cls: Any):
    records = [
        transform_record("map", "base", 0, [0, 0, 0]),
        transform_record("base", "lidar", 0, [1, 0, 0]),
        transform_record("map", "lidar", 0, [5, 0, 0]),
        point_record("points", 0, "lidar", [[0, 0, 0]]),
    ]
    results = []
    for chunk_size in (1, 100_000):
        buffer = write_mcap(records, chunk_size=chunk_size)
        buffer.seek(0)
        message = list(reader_cls(buffer).iter_projected_messages("map"))[0]
        results.append(
            (
                message.status,
                tuple(sorted(p.as_tuple() for p in message.projections[0].positions)),
            )
        )
    assert results[0] == results[1]
    assert results[0][0] == "conflict"
    assert results[0][1] == ((1.0, 0.0, 0.0), (5.0, 0.0, 0.0))


@pytest.mark.parametrize("reader_cls", [SeekingReader, NonSeekingReader])
def test_auto_detection_in_both_readers(reader_cls: Any):
    records = [
        {
            "topic": "frames",
            "log_time": 0,
            "data": {
                "timestamp": {"sec": 0, "nsec": 0},
                "parent_frame_id": "map",
                "child_frame_id": "lidar",
                "translation": {"x": 7, "y": 0, "z": 0},
                "rotation": {"x": 0, "y": 0, "z": 0, "w": 1},
            },
        },
        point_record("points", 0, "lidar", [[0, 0, 0]]),
    ]
    buffer = write_mcap(records)
    buffer.seek(0)
    projected = list(
        reader_cls(buffer).iter_projected_messages("map", transform_topics=None)
    )
    assert len(projected) == 1
    assert approx(projected[0].projections[0].position, [7, 0, 0])
