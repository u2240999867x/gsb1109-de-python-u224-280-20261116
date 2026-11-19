"""Coordinate-frame transforms and point projection for MCAP readers.

A capture package records frame relationships as JSON messages on one agreed topic.
Each message describes a rigid transform from a child frame into its parent frame at a
sample time::

    {
      "timestamp": {"sec": 12, "nsec": 34},
      "parent_frame_id": "map",
      "child_frame_id": "lidar",
      "translation": {"x": 1.0, "y": 0.0, "z": 0.0},
      "rotation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}
    }

Points on other topics name their own frame.  :py:class:`TransformTimeline` resolves
the transform from a point's frame to a target frame at the point's sample time and
projects the point.  The timeline is built from all transform records at once, sorted
by a canonical semantic key, so the result does not depend on the order in which the
file was scanned.

Interpolation is a caller choice (``"linear"``, ``"step"``/``"zero_order_hold"``, or
``"nearest"``), bounded by a caller-supplied maximum interval.  Interpolation never
crosses a re-parenting event (a child frame changing parents) or a clock restart (its
sample timestamps jumping backwards in log order).  When several frame paths resolve
to different positions the conflict is retained as a list of alternatives rather than
silently choosing one.
"""

import json
import math
from bisect import bisect_left
from collections.abc import Mapping as MappingABC
from collections.abc import Sequence as SequenceABC
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set
from typing import Tuple

from .exceptions import McapTransformError
from .records import Channel, Message, Schema

__all__ = [
    "FrameTransform",
    "PathResolution",
    "PointMessage",
    "PointProjection",
    "ProjectedMessage",
    "ProjectionCandidate",
    "Quaternion",
    "RigidTransform",
    "TransformRecordError",
    "TransformResolution",
    "TransformTimeline",
    "Vector3",
    "parse_point",
    "parse_point_message",
    "parse_transform_message",
]

_EPS = 1e-12
_INTERPOLATION_ALIASES = {
    "linear": "linear",
    "lerp": "linear",
    "step": "step",
    "zero_order_hold": "step",
    "zoh": "step",
    "nearest": "nearest",
}


@dataclass(frozen=True)
class Vector3:
    """A point or translation in 3D space."""

    x: float
    y: float
    z: float

    def __add__(self, other: "Vector3") -> "Vector3":
        return Vector3(self.x + other.x, self.y + other.y, self.z + other.z)

    def __sub__(self, other: "Vector3") -> "Vector3":
        return Vector3(self.x - other.x, self.y - other.y, self.z - other.z)

    def __mul__(self, scale: float) -> "Vector3":
        return Vector3(self.x * scale, self.y * scale, self.z * scale)

    def __neg__(self) -> "Vector3":
        return Vector3(-self.x, -self.y, -self.z)

    def dot(self, other: "Vector3") -> float:
        return self.x * other.x + self.y * other.y + self.z * other.z

    def cross(self, other: "Vector3") -> "Vector3":
        return Vector3(
            self.y * other.z - self.z * other.y,
            self.z * other.x - self.x * other.z,
            self.x * other.y - self.y * other.x,
        )

    def norm(self) -> float:
        return math.sqrt(self.dot(self))

    def as_tuple(self) -> Tuple[float, float, float]:
        return (self.x, self.y, self.z)

    def distance(self, other: "Vector3") -> float:
        return (self - other).norm()


@dataclass(frozen=True)
class Quaternion:
    """A quaternion in ``(x, y, z, w)`` order.

    Frame transforms carry unit quaternions.  Parsing normalizes small numerical
    drift away; a quaternion whose norm is effectively zero is rejected.
    """

    x: float
    y: float
    z: float
    w: float

    @staticmethod
    def identity() -> "Quaternion":
        return Quaternion(0.0, 0.0, 0.0, 1.0)

    def norm(self) -> float:
        return math.sqrt(self.x * self.x + self.y * self.y + self.z * self.z + self.w * self.w)

    def normalized(self) -> "Quaternion":
        norm = self.norm()
        if norm < _EPS:
            raise McapTransformError("rotation quaternion has zero norm")
        return Quaternion(self.x / norm, self.y / norm, self.z / norm, self.w / norm)

    def conjugate(self) -> "Quaternion":
        return Quaternion(-self.x, -self.y, -self.z, self.w)

    def __mul__(self, other: "Quaternion") -> "Quaternion":
        return Quaternion(
            self.w * other.x + self.x * other.w + self.y * other.z - self.z * other.y,
            self.w * other.y - self.x * other.z + self.y * other.w + self.z * other.x,
            self.w * other.z + self.x * other.y - self.y * other.x + self.z * other.w,
            self.w * other.w - self.x * other.x - self.y * other.y - self.z * other.z,
        )

    def rotate(self, vector: Vector3) -> Vector3:
        q = self
        v = Vector3(q.x, q.y, q.z)
        t = v.cross(vector) * 2.0
        return vector + t * q.w + v.cross(t)

    def slerp(self, other: "Quaternion", fraction: float) -> "Quaternion":
        """Spherical linear interpolation, taking the shorter path."""
        dot = self.x * other.x + self.y * other.y + self.z * other.z + self.w * other.w
        target = other
        if dot < 0.0:
            dot = -dot
            target = Quaternion(-other.x, -other.y, -other.z, -other.w)
        if dot > 1.0 - 1e-9:
            # Nearly parallel: a normalized linear blend avoids a division by zero.
            return Quaternion(
                self.x + (target.x - self.x) * fraction,
                self.y + (target.y - self.y) * fraction,
                self.z + (target.z - self.z) * fraction,
                self.w + (target.w - self.w) * fraction,
            ).normalized()
        dot = min(1.0, max(-1.0, dot))
        theta = math.acos(dot)
        sin_theta = math.sin(theta)
        a = math.sin((1.0 - fraction) * theta) / sin_theta
        b = math.sin(fraction * theta) / sin_theta
        return Quaternion(
            self.x * a + target.x * b,
            self.y * a + target.y * b,
            self.z * a + target.z * b,
            self.w * a + target.w * b,
        ).normalized()

    def as_tuple(self) -> Tuple[float, float, float, float]:
        return (self.x, self.y, self.z, self.w)


@dataclass(frozen=True)
class RigidTransform:
    """A rotation followed by a translation: ``rotation * point + translation``."""

    translation: Vector3
    rotation: Quaternion

    @staticmethod
    def identity() -> "RigidTransform":
        return RigidTransform(Vector3(0.0, 0.0, 0.0), Quaternion.identity())

    def apply(self, point: Vector3) -> Vector3:
        return self.rotation.rotate(point) + self.translation

    def compose(self, other: "RigidTransform") -> "RigidTransform":
        """Return the transform that applies ``other`` and then ``self``."""
        return RigidTransform(
            self.rotation.rotate(other.translation) + self.translation,
            (self.rotation * other.rotation).normalized(),
        )

    def inverse(self) -> "RigidTransform":
        inverse_rotation = self.rotation.conjugate()
        return RigidTransform(
            -inverse_rotation.rotate(self.translation), inverse_rotation
        )

    def almost_equal(self, other: "RigidTransform", tolerance: float = 1e-9) -> bool:
        if self.translation.distance(other.translation) > tolerance:
            return False
        # q and -q are the same rotation, so compare the absolute dot product.
        dot = abs(
            self.rotation.x * other.rotation.x
            + self.rotation.y * other.rotation.y
            + self.rotation.z * other.rotation.z
            + self.rotation.w * other.rotation.w
        )
        return dot >= 1.0 - 1e-9


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise McapTransformError(f"{field} must be a number, got {type(value).__name__}")
    result = float(value)
    if not math.isfinite(result):
        raise McapTransformError(f"{field} must be finite")
    return result


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, MappingABC):
        raise McapTransformError(f"{field} must be an object")
    return value


def _first(mapping: Mapping[str, Any], names: Sequence[str]) -> Any:
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return None


def _parse_vector(value: Any, field: str) -> Vector3:
    if isinstance(value, (list, tuple)):
        if len(value) != 3:
            raise McapTransformError(f"{field} must have three components")
        return Vector3(
            _number(value[0], f"{field}.x"),
            _number(value[1], f"{field}.y"),
            _number(value[2], f"{field}.z"),
        )
    obj = _mapping(value, field)
    return Vector3(
        _number(obj.get("x", 0.0), f"{field}.x"),
        _number(obj.get("y", 0.0), f"{field}.y"),
        _number(obj.get("z", 0.0), f"{field}.z"),
    )


def _parse_quaternion(value: Any, field: str) -> Quaternion:
    if isinstance(value, (list, tuple)):
        if len(value) != 4:
            raise McapTransformError(f"{field} must have four components")
        quaternion = Quaternion(
            _number(value[0], f"{field}.x"),
            _number(value[1], f"{field}.y"),
            _number(value[2], f"{field}.z"),
            _number(value[3], f"{field}.w"),
        )
    else:
        obj = _mapping(value, field)
        quaternion = Quaternion(
            _number(obj.get("x", 0.0), f"{field}.x"),
            _number(obj.get("y", 0.0), f"{field}.y"),
            _number(obj.get("z", 0.0), f"{field}.z"),
            _number(obj.get("w", 1.0), f"{field}.w"),
        )
    return quaternion.normalized()


def parse_timestamp(value: Any, field: str = "timestamp") -> Optional[int]:
    """Parse ``{sec, nsec}``, integer nanoseconds, or numeric seconds."""
    if value is None:
        return None
    if isinstance(value, MappingABC):
        sec = value.get("sec", value.get("seconds", 0))
        nsec = value.get("nsec", value.get("nanoseconds", value.get("nanosec", 0)))
        return int(_number(sec, f"{field}.sec")) * 1_000_000_000 + int(
            _number(nsec, f"{field}.nsec")
        )
    if isinstance(value, (list, tuple)):
        if len(value) != 2:
            raise McapTransformError(f"{field} must be a [sec, nsec] pair")
        return int(_number(value[0], f"{field}[0]")) * 1_000_000_000 + int(
            _number(value[1], f"{field}[1]")
        )
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise McapTransformError(f"{field} must be a timestamp object or number")
    number = float(value)
    if not math.isfinite(number):
        raise McapTransformError(f"{field} must be finite")
    if isinstance(value, int):
        # MCAP timestamps are integer nanoseconds.
        return value
    # Float seconds (e.g. 1490149580.117) are ambiguous with small nanosecond
    # values; magnitudes at or above 1e13 are treated as nanoseconds.
    if abs(number) >= 1e13:
        return int(round(number))
    return int(round(number * 1_000_000_000))


@dataclass(frozen=True)
class FrameTransform:
    """One frame relationship at one sample time, with enough context to trace it."""

    parent_frame_id: str
    child_frame_id: str
    sample_time: int
    translation: Vector3
    rotation: Quaternion
    log_time: int = 0
    topic: str = ""
    channel_id: int = 0
    sequence: int = 0

    @property
    def transform(self) -> RigidTransform:
        return RigidTransform(self.translation, self.rotation)

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any],
        *,
        log_time: int = 0,
        topic: str = "",
        channel_id: int = 0,
        sequence: int = 0,
        default_sample_time: Optional[int] = None,
    ) -> "FrameTransform":
        """Parse a transform message.

        The canonical Foxglove field names are accepted, as are the ROS-style
        ``header.frame_id`` / ``child_frame_id`` and a nested ``transform`` object.
        """
        parent = _first(
            payload, ("parent_frame_id", "parent_frame", "parent", "frame_id")
        )
        child = _first(payload, ("child_frame_id", "child_frame", "child"))
        if parent is None:
            header = payload.get("header")
            if isinstance(header, MappingABC):
                parent = header.get("frame_id")
        if parent is None or not isinstance(parent, str) or not parent:
            raise McapTransformError("transform is missing parent_frame_id")
        if child is None or not isinstance(child, str) or not child:
            raise McapTransformError("transform is missing child_frame_id")

        timestamp = _first(payload, ("timestamp", "sample_time", "time", "stamp"))
        sample_time = parse_timestamp(timestamp)
        if sample_time is None:
            header = payload.get("header")
            if isinstance(header, MappingABC):
                sample_time = parse_timestamp(
                    _first(header, ("stamp", "timestamp", "time"))
                )
        if sample_time is None:
            sample_time = default_sample_time if default_sample_time is not None else 0

        nested = payload.get("transform")
        body = _mapping(nested, "transform") if nested is not None else payload
        translation = _first(body, ("translation", "position"))
        rotation = _first(body, ("rotation", "orientation"))
        if translation is None or rotation is None:
            raise McapTransformError(
                "transform is missing translation or rotation"
            )
        return cls(
            parent_frame_id=parent,
            child_frame_id=child,
            sample_time=int(sample_time),
            translation=_parse_vector(translation, "translation"),
            rotation=_parse_quaternion(rotation, "rotation"),
            log_time=int(log_time),
            topic=topic,
            channel_id=int(channel_id),
            sequence=int(sequence),
        )

    @classmethod
    def from_json(
        cls,
        data: bytes,
        *,
        log_time: int = 0,
        topic: str = "",
        channel_id: int = 0,
        sequence: int = 0,
        default_sample_time: Optional[int] = None,
    ) -> "FrameTransform":
        try:
            payload = json.loads(data)
        except (TypeError, ValueError) as error:
            raise McapTransformError(f"transform message is not valid JSON: {error}")
        if not isinstance(payload, MappingABC):
            raise McapTransformError("transform message must be a JSON object")
        return cls.from_mapping(
            payload,
            log_time=log_time,
            topic=topic,
            channel_id=channel_id,
            sequence=sequence,
            default_sample_time=default_sample_time,
        )

    def canonical_key(self) -> Tuple[Any, ...]:
        """A key independent of scan order, including the transform value."""
        return (
            self.log_time,
            self.sequence,
            self.channel_id,
            self.topic,
            self.sample_time,
            self.parent_frame_id,
            self.child_frame_id,
            self.translation.x,
            self.translation.y,
            self.translation.z,
            self.rotation.x,
            self.rotation.y,
            self.rotation.z,
            self.rotation.w,
        )


@dataclass(frozen=True)
class _TimestampGroup:
    time: int
    samples: Tuple[FrameTransform, ...]


@dataclass(frozen=True)
class _Segment:
    parent: str
    child: str
    epoch: int
    groups: Tuple[_TimestampGroup, ...]
    start_time: int
    end_time: Optional[int]
    segment_index: int

    def canonical_key(self) -> Tuple[Any, ...]:
        return (self.child, self.epoch, self.segment_index, self.parent)


@dataclass(frozen=True)
class _Epoch:
    index: int
    order_start: Optional[int]
    order_end: Optional[int]
    segments: Tuple[_Segment, ...]


@dataclass(frozen=True)
class _EdgeValue:
    parent: str
    child: str
    transform: RigidTransform
    samples: Tuple[FrameTransform, ...]
    epoch: int
    segment_index: int
    status: str = "ok"
    reason: Optional[str] = None

    def canonical_key(self) -> Tuple[Any, ...]:
        return (
            self.child,
            self.parent,
            self.epoch,
            self.segment_index,
            self.status,
            self.reason or "",
            tuple(sample.canonical_key() for sample in self.samples),
        )


def _dedupe_edge_values(
    values: Iterable[_EdgeValue], tolerance: float
) -> List[_EdgeValue]:
    """Keep one entry per distinct (edge, value), preserving conflicting values."""
    ordered = sorted(values, key=lambda item: item.canonical_key())
    result: List[_EdgeValue] = []
    for value in ordered:
        duplicate = False
        for existing in result:
            if (
                existing.child == value.child
                and existing.parent == value.parent
                and existing.status == value.status
                and existing.reason == value.reason
                and existing.transform.almost_equal(value.transform, tolerance)
            ):
                duplicate = True
                break
        if not duplicate:
            result.append(value)
    return result


def _group_by_time(samples: Sequence[FrameTransform]) -> Tuple[_TimestampGroup, ...]:
    groups: List[_TimestampGroup] = []
    for sample in sorted(samples, key=lambda item: item.canonical_key()):
        if groups and groups[-1].time == sample.sample_time:
            previous = groups[-1]
            groups[-1] = _TimestampGroup(
                previous.time, previous.samples + (sample,)
            )
        else:
            groups.append(_TimestampGroup(sample.sample_time, (sample,)))
    return tuple(groups)


def _split_epochs(samples: Sequence[FrameTransform]) -> List[List[FrameTransform]]:
    """Split one child's samples into clock epochs.

    Samples are ordered by log time (the file's record order), then by canonical
    fields.  A sample timestamp that decreases is a clock restart: transforms on
    either side never interpolate through each other.
    """
    ordered = sorted(samples, key=lambda item: item.canonical_key())
    epochs: List[List[FrameTransform]] = []
    current: List[FrameTransform] = []
    previous_time: Optional[int] = None
    for sample in ordered:
        if current and previous_time is not None and sample.sample_time < previous_time:
            epochs.append(current)
            current = []
        current.append(sample)
        previous_time = sample.sample_time
    if current:
        epochs.append(current)
    return epochs


def _build_epochs(samples: Sequence[FrameTransform]) -> Tuple[_Epoch, ...]:
    epochs: List[_Epoch] = []
    for epoch_index, epoch_samples in enumerate(_split_epochs(samples)):
        segments: List[_Segment] = []
        current_samples: List[FrameTransform] = []
        current_parent: Optional[str] = None
        for sample in sorted(epoch_samples, key=lambda item: item.canonical_key()):
            if current_samples and sample.parent_frame_id != current_parent:
                segments.append(
                    _make_segment(current_samples, epoch_index, len(segments))
                )
                current_samples = []
            current_samples.append(sample)
            current_parent = sample.parent_frame_id
        if current_samples:
            segments.append(_make_segment(current_samples, epoch_index, len(segments)))

        # A segment ends where the next re-parenting segment begins.  At that exact
        # time the new parent applies; the old parent never holds past it.
        rebuilt = [
            _Segment(
                parent=segment.parent,
                child=segment.child,
                epoch=epoch_index,
                groups=segment.groups,
                start_time=segment.start_time,
                end_time=(
                    segments[index + 1].start_time
                    if index + 1 < len(segments)
                    else None
                ),
                segment_index=index,
            )
            for index, segment in enumerate(segments)
        ]
        epochs.append(_Epoch(epoch_index, None, None, tuple(rebuilt)))

    # Epoch order bounds use log time.  The first epoch reaches back indefinitely;
    # each later epoch starts at its first transform's log time, so a point logged
    # before a restart cannot use post-restart data.
    epoch_logs: List[List[int]] = [
        [
            sample.log_time
            for segment in epoch.segments
            for group in segment.groups
            for sample in group.samples
        ]
        for epoch in epochs
    ]
    for index, epoch in enumerate(epochs):
        order_start = None if index == 0 else min(epoch_logs[index])
        order_end = (
            min(epoch_logs[index + 1]) if index + 1 < len(epoch_logs) else None
        )
        epochs[index] = _Epoch(epoch.index, order_start, order_end, epoch.segments)

    return tuple(epochs)


def _make_segment(
    samples: Sequence[FrameTransform],
    epoch: int,
    segment_index: int,
) -> _Segment:
    groups = _group_by_time(samples)
    return _Segment(
        parent=samples[0].parent_frame_id,
        child=samples[0].child_frame_id,
        epoch=epoch,
        groups=groups,
        start_time=groups[0].time,
        end_time=None,
        segment_index=segment_index,
    )


@dataclass(frozen=True)
class PathResolution:
    """One way of reaching the target frame and its resulting transform."""

    path: Tuple[str, ...]
    transform: Optional[RigidTransform]
    samples: Tuple[FrameTransform, ...]
    status: str
    reason: Optional[str] = None
    epoch: int = 0


@dataclass(frozen=True)
class TransformResolution:
    """The outcome of resolving a frame-to-frame transform."""

    source_frame: str
    target_frame: str
    sample_time: int
    status: str
    transform: Optional[RigidTransform]
    paths: Tuple[PathResolution, ...]
    conflict: bool = False
    missing_edges: Tuple[Tuple[str, str], ...] = ()

    @property
    def path(self) -> Optional[Tuple[str, ...]]:
        return self.paths[0].path if self.paths else None

    @property
    def samples(self) -> Tuple[FrameTransform, ...]:
        seen: Set[Tuple[Any, ...]] = set()
        result: List[FrameTransform] = []
        for path in self.paths:
            for sample in path.samples:
                key = sample.canonical_key()
                if key not in seen:
                    seen.add(key)
                    result.append(sample)
        return tuple(result)


@dataclass(frozen=True)
class ProjectionCandidate:
    """A transformed point and the transform records that produced it."""

    path: Tuple[str, ...]
    position: Optional[Vector3]
    transform: Optional[RigidTransform]
    samples: Tuple[FrameTransform, ...]
    status: str
    reason: Optional[str] = None
    epoch: int = 0


@dataclass(frozen=True)
class PointProjection:
    """A point projected into a target frame, retaining unresolved alternatives.

    ``origin`` is the point as it appeared in the message; ``point`` is the same
    point in the message's frame after applying the message pose.  ``sample_time``
    and ``candidates`` (each with its transform records) make the calculation
    auditable, including which alternative won or why none did.
    """

    point: Vector3
    source_frame: str
    target_frame: str
    sample_time: int
    status: str
    position: Optional[Vector3]
    positions: Tuple[Vector3, ...]
    candidates: Tuple[ProjectionCandidate, ...]
    missing_edges: Tuple[Tuple[str, str], ...] = ()
    origin: Optional[Vector3] = None

    @property
    def conflict(self) -> bool:
        return self.status == "conflict"

    @property
    def transforms(self) -> Tuple[FrameTransform, ...]:
        seen: Set[Tuple[Any, ...]] = set()
        result: List[FrameTransform] = []
        for candidate in self.candidates:
            for sample in candidate.samples:
                key = sample.canonical_key()
                if key not in seen:
                    seen.add(key)
                    result.append(sample)
        return tuple(result)


@dataclass(frozen=True)
class TransformRecordError:
    """A transform record that could not be parsed, kept for auditing."""

    topic: str
    channel_id: int
    log_time: int
    sequence: int
    reason: str


@dataclass(frozen=True)
class ProjectedMessage:
    """One point-bearing message with every point projected into a target frame."""

    schema: Optional[Schema]
    channel: Channel
    message: Message
    frame_id: str
    target_frame: str
    sample_time: int
    time_source: str
    projections: Tuple[PointProjection, ...]
    error: Optional[str] = None

    @property
    def status(self) -> str:
        """``"ok"`` when every point resolved, else ``"conflict"``/``"unresolved"``."""
        if self.error is not None:
            return "unresolved"
        statuses = {projection.status for projection in self.projections}
        if statuses == {"ok"} or not statuses:
            return "ok"
        if "unresolved" in statuses:
            return "unresolved"
        return "conflict"

    @property
    def frame_conflict(self) -> bool:
        """Whether the transform stage, rather than a point's value, conflicted."""
        return self.status == "conflict"


@dataclass(frozen=True)
class PointMessage:
    """A decoded point-bearing message: points, their frame, and their pose."""

    frame_id: str
    sample_time: int
    time_source: str
    points: Tuple[Vector3, ...]
    pose: Optional[RigidTransform] = None

    def local_points(self) -> Tuple[Vector3, ...]:
        """Points expressed relative to the message's own frame (pose applied)."""
        if self.pose is None:
            return self.points
        return tuple(self.pose.apply(point) for point in self.points)


class TransformTimeline:
    """Frame transform history resolved at caller-specified sample times.

    :param samples: transform records, in any order.
    :param interpolation: ``"linear"`` (default), ``"step"``/``"zero_order_hold"``,
        or ``"nearest"``.  A callable with signature ``(left, right, fraction)`` may
        also be supplied for linear-style two-sample interpolation.
    :param max_interval_ns: largest allowed gap between bracketing samples (linear)
        or distance from the held/nearest sample (step/nearest).  ``None`` means no
        limit; interpolation still never crosses a re-parent or clock restart.
    :param tolerance: position tolerance used to decide whether several paths agree.
    :param errors: malformed transform records, retained for auditing.
    """

    def __init__(
        self,
        samples: Iterable[FrameTransform],
        *,
        interpolation: str = "linear",
        max_interval_ns: Optional[int] = None,
        tolerance: float = 1e-6,
        errors: Iterable[TransformRecordError] = (),
    ):
        if isinstance(interpolation, str):
            key = interpolation.strip().lower()
            if key not in _INTERPOLATION_ALIASES:
                raise ValueError(
                    "interpolation must be one of linear, step, zero_order_hold, nearest"
                )
            self._interpolation = _INTERPOLATION_ALIASES[key]
            self._interpolator: Optional[Callable[..., RigidTransform]] = None
        elif callable(interpolation):
            self._interpolation = "linear"
            self._interpolator = interpolation
        else:
            raise TypeError("interpolation must be a string or callable")
        if max_interval_ns is not None and max_interval_ns < 0:
            raise ValueError("max_interval_ns must be non-negative")
        if tolerance < 0:
            raise ValueError("tolerance must be non-negative")
        self._max_interval_ns = max_interval_ns
        self._tolerance = float(tolerance)
        self._errors = tuple(errors)

        canonical: List[FrameTransform] = []
        for sample in samples:
            if sample.parent_frame_id == sample.child_frame_id:
                # A frame is already in itself; an explicit self edge is noise.
                continue
            canonical.append(sample)
        canonical.sort(key=lambda item: item.canonical_key())
        self._samples = tuple(canonical)

        by_child: Dict[str, List[FrameTransform]] = {}
        for sample in canonical:
            by_child.setdefault(sample.child_frame_id, []).append(sample)
        self._epochs_by_child: Dict[str, Tuple[_Epoch, ...]] = {
            child: _build_epochs(child_samples)
            for child, child_samples in sorted(by_child.items())
        }
        self._frames: Tuple[str, ...] = tuple(
            sorted(
                {
                    sample.parent_frame_id
                    for sample in canonical
                }
                | {sample.child_frame_id for sample in canonical}
            )
        )

    @property
    def samples(self) -> Tuple[FrameTransform, ...]:
        """All transform records in canonical order."""
        return self._samples

    @property
    def frames(self) -> Tuple[str, ...]:
        """All frame names mentioned by the transform records."""
        return self._frames

    @property
    def interpolation(self) -> str:
        return self._interpolation

    @property
    def max_interval_ns(self) -> Optional[int]:
        return self._max_interval_ns

    @property
    def errors(self) -> Tuple[TransformRecordError, ...]:
        """Transform records skipped because they could not be parsed."""
        return self._errors

    def _select_epochs(
        self, child: str, order_time: Optional[int]
    ) -> Tuple[_Epoch, ...]:
        epochs = self._epochs_by_child.get(child, ())
        if order_time is None:
            return epochs
        selected: List[_Epoch] = []
        for epoch in epochs:
            if epoch.order_start is not None and order_time < epoch.order_start:
                continue
            if epoch.order_end is not None and order_time >= epoch.order_end:
                continue
            selected.append(epoch)
        return tuple(selected)

    def _active_segments(
        self, sample_time: int, order_time: Optional[int]
    ) -> List[_Segment]:
        segments: List[_Segment] = []
        for child in sorted(self._epochs_by_child):
            for epoch in self._select_epochs(child, order_time):
                if not epoch.segments:
                    continue
                # The newest segment that has already begun at ``sample_time`` is
                # followed until another segment starts at or before the sample
                # time.  A later re-parent is invisible to this point, so the
                # previous parent keeps applying up to the new segment's first
                # sample and never past it.
                chosen = None
                for candidate in epoch.segments:
                    if candidate.start_time <= sample_time and (
                        chosen is None
                        or candidate.start_time >= chosen.start_time
                    ):
                        chosen = candidate
                if chosen is None:
                    continue
                active = [
                    candidate
                    for candidate in epoch.segments
                    if candidate.start_time == chosen.start_time
                    and (
                        candidate.end_time is None
                        or sample_time < candidate.end_time
                        # Two parents declared at the same sample time are concurrent
                        # declarations, not a re-parent: keep both so a disagreement
                        # surfaces as a conflict instead of silently picking one.
                        or candidate.end_time == candidate.start_time
                    )
                ]
                segments.extend(active)
        return segments

    def _evaluate_segment(self, segment: _Segment, sample_time: int) -> List[_EdgeValue]:
        groups = segment.groups
        times = [group.time for group in groups]
        index = bisect_left(times, sample_time)
        mode = self._interpolation

        if index < len(times) and times[index] == sample_time:
            return [
                _EdgeValue(
                    parent=segment.parent,
                    child=segment.child,
                    transform=sample.transform,
                    samples=(sample,),
                    epoch=segment.epoch,
                    segment_index=segment.segment_index,
                )
                for sample in groups[index].samples
            ]

        if mode == "linear":
            if index == 0 or index == len(times):
                return [
                    _EdgeValue(
                        parent=segment.parent,
                        child=segment.child,
                        transform=RigidTransform.identity(),
                        samples=(),
                        epoch=segment.epoch,
                        segment_index=segment.segment_index,
                        status="unresolved",
                        reason="out_of_range",
                    )
                ]
            left_time, right_time = times[index - 1], times[index]
            gap = right_time - left_time
            # Interpolating between two samples of this segment never crosses the
            # boundary, even when the right sample is the last one before it.  Only
            # holding and nearest-neighbour selection can reach past the last sample.
            if self._max_interval_ns is not None and gap > self._max_interval_ns:
                return [
                    _EdgeValue(
                        parent=segment.parent,
                        child=segment.child,
                        transform=RigidTransform.identity(),
                        samples=(),
                        epoch=segment.epoch,
                        segment_index=segment.segment_index,
                        status="unresolved",
                        reason="gap_too_large",
                    )
                ]
            fraction = (sample_time - left_time) / float(gap)
            values: List[_EdgeValue] = []
            for left in groups[index - 1].samples:
                for right in groups[index].samples:
                    if self._interpolator is not None:
                        transform = self._interpolator(
                            left, right, fraction
                        )
                        if not isinstance(transform, RigidTransform):
                            raise TypeError(
                                "custom interpolation callable must return RigidTransform"
                            )
                    else:
                        transform = RigidTransform(
                            left.translation * (1.0 - fraction)
                            + right.translation * fraction,
                            left.rotation.slerp(right.rotation, fraction),
                        )
                    values.append(
                        _EdgeValue(
                            parent=segment.parent,
                            child=segment.child,
                            transform=transform,
                            samples=(left, right),
                            epoch=segment.epoch,
                            segment_index=segment.segment_index,
                        )
                    )
            return _dedupe_edge_values(values, self._tolerance)

        # step and nearest use a single anchor sample.
        if mode == "step":
            if index == 0:
                anchor_index = 0
                distance = times[0] - sample_time
                # Backward hold is extrapolation; only exact-first or forward hold.
                if sample_time < times[0]:
                    return [
                        _EdgeValue(
                            parent=segment.parent,
                            child=segment.child,
                            transform=RigidTransform.identity(),
                            samples=(),
                            epoch=segment.epoch,
                            segment_index=segment.segment_index,
                            status="unresolved",
                            reason="out_of_range",
                        )
                    ]
            else:
                anchor_index = index - 1
                distance = sample_time - times[anchor_index]
        else:  # nearest
            candidates = []
            if index > 0:
                candidates.append(index - 1)
            if index < len(times):
                candidates.append(index)
            if not candidates:
                return []
            anchor_index = min(
                candidates, key=lambda item: (abs(times[item] - sample_time), times[item])
            )
            distance = abs(times[anchor_index] - sample_time)

        if self._max_interval_ns is not None and distance > self._max_interval_ns:
            return [
                _EdgeValue(
                    parent=segment.parent,
                    child=segment.child,
                    transform=RigidTransform.identity(),
                    samples=(),
                    epoch=segment.epoch,
                    segment_index=segment.segment_index,
                    status="unresolved",
                    reason="gap_too_large",
                )
            ]
        if segment.end_time is not None and sample_time >= segment.end_time:
            return [
                _EdgeValue(
                    parent=segment.parent,
                    child=segment.child,
                    transform=RigidTransform.identity(),
                    samples=(),
                    epoch=segment.epoch,
                    segment_index=segment.segment_index,
                    status="unresolved",
                    reason="reparented",
                )
            ]
        return [
            _EdgeValue(
                parent=segment.parent,
                child=segment.child,
                transform=sample.transform,
                samples=(sample,),
                epoch=segment.epoch,
                segment_index=segment.segment_index,
            )
            for sample in groups[anchor_index].samples
        ]

    def _active_edges(
        self, sample_time: int, order_time: Optional[int]
    ) -> Tuple[List[_EdgeValue], List[_EdgeValue]]:
        values: List[_EdgeValue] = []
        unresolved: List[_EdgeValue] = []
        for segment in self._active_segments(sample_time, order_time):
            for value in self._evaluate_segment(segment, sample_time):
                if value.status == "ok":
                    values.append(value)
                else:
                    unresolved.append(value)
        values = _dedupe_edge_values(values, self._tolerance)
        unresolved = _dedupe_edge_values(unresolved, self._tolerance)
        return values, unresolved

    def _frame_component(
        self,
        source_frame: str,
        sample_time: int,
        order_time: Optional[int],
    ) -> Set[str]:
        """Frames connected to ``source_frame`` by any active (or unavailable) edge."""
        edges, unresolved = self._active_edges(sample_time, order_time)
        adjacency: Dict[str, Set[str]] = {}
        for edge in list(edges) + list(unresolved):
            adjacency.setdefault(edge.child, set()).add(edge.parent)
            adjacency.setdefault(edge.parent, set()).add(edge.child)
        reachable: Set[str] = {source_frame}
        queue = [source_frame]
        while queue:
            frame = queue.pop()
            for neighbor in adjacency.get(frame, ()):
                if neighbor not in reachable:
                    reachable.add(neighbor)
                    queue.append(neighbor)
        return reachable

    def _paths(
        self,
        source_frame: str,
        target_frame: str,
        sample_time: int,
        order_time: Optional[int],
    ) -> Tuple[List[PathResolution], List[_EdgeValue]]:
        edges, unresolved = self._active_edges(sample_time, order_time)
        adjacency: Dict[str, List[Tuple[str, _EdgeValue, bool]]] = {}
        for edge in edges:
            adjacency.setdefault(edge.child, []).append((edge.parent, edge, False))
            adjacency.setdefault(edge.parent, []).append((edge.child, edge, True))
        for node in adjacency:
            adjacency[node].sort(
                key=lambda item: (
                    item[0],
                    item[1].canonical_key(),
                    item[2],
                )
            )

        if source_frame == target_frame:
            return [
                PathResolution(
                    path=(source_frame,),
                    transform=RigidTransform.identity(),
                    samples=(),
                    status="ok",
                )
            ], []
        if source_frame not in adjacency:
            return [], unresolved

        paths: List[PathResolution] = []
        seen_paths: Set[Tuple[Any, ...]] = set()

        def visit(
            frame: str,
            visited: Set[str],
            current_path: Tuple[str, ...],
            transform: RigidTransform,
            samples: Tuple[FrameTransform, ...],
            edge_keys: Tuple[Tuple[Any, ...], ...],
            epoch: int,
        ) -> None:
            if len(paths) >= 1024:
                return
            for next_frame, edge, inverse in adjacency.get(frame, ()):
                if next_frame in visited:
                    continue
                used_transform = edge.transform.inverse() if inverse else edge.transform
                next_transform = transform.compose(used_transform)
                next_samples = samples + edge.samples
                next_path = current_path + (next_frame,)
                next_edge_keys = edge_keys + ((edge.canonical_key(), inverse),)
                if next_frame == target_frame:
                    path_key = (next_path, next_edge_keys)
                    if path_key not in seen_paths:
                        seen_paths.add(path_key)
                        paths.append(
                            PathResolution(
                                path=next_path,
                                transform=next_transform,
                                samples=next_samples,
                                status="ok",
                                epoch=epoch if epoch >= 0 else edge.epoch,
                            )
                        )
                    continue
                visit(
                    next_frame,
                    visited | {next_frame},
                    next_path,
                    next_transform,
                    next_samples,
                    next_edge_keys,
                    edge.epoch,
                )

        visit(
            source_frame,
            {source_frame},
            (source_frame,),
            RigidTransform.identity(),
            (),
            (),
            -1,
        )
        paths.sort(
            key=lambda item: (
                len(item.path),
                item.path,
                tuple(sample.canonical_key() for sample in item.samples),
            )
        )
        return paths, unresolved

    def resolve_transform(
        self,
        source_frame: str,
        target_frame: str,
        sample_time: int,
        *,
        order_time: Optional[int] = None,
    ) -> TransformResolution:
        """Resolve every path from ``source_frame`` to ``target_frame``.

        ``order_time`` (normally the point message's log time) disambiguates clock
        epochs whose sample-time ranges overlap.  Without it, all epochs that can
        cover ``sample_time`` contribute candidates.
        """
        if not source_frame:
            raise ValueError("source_frame must not be empty")
        if not target_frame:
            raise ValueError("target_frame must not be empty")
        paths, unresolved = self._paths(
            source_frame, target_frame, int(sample_time), order_time
        )
        reachable = self._frame_component(source_frame, int(sample_time), order_time)
        missing_edges = tuple(
            sorted(
                {
                    (value.parent, value.child)
                    for value in unresolved
                    if value.child in reachable
                }
            )
        )
        if not paths:
            return TransformResolution(
                source_frame=source_frame,
                target_frame=target_frame,
                sample_time=int(sample_time),
                status="unresolved",
                transform=None,
                paths=(
                    PathResolution(
                        path=(source_frame, target_frame),
                        transform=None,
                        samples=(),
                        status="unresolved",
                        reason=unresolved[0].reason if unresolved else "missing_edge",
                    ),
                ),
                conflict=False,
                missing_edges=missing_edges,
            )

        distinct: List[RigidTransform] = []
        for path in paths:
            assert path.transform is not None
            if not any(
                path.transform.almost_equal(existing, self._tolerance)
                for existing in distinct
            ):
                distinct.append(path.transform)
        conflict = len(distinct) > 1
        status = "conflict" if conflict else "ok"
        return TransformResolution(
            source_frame=source_frame,
            target_frame=target_frame,
            sample_time=int(sample_time),
            status=status,
            transform=None if conflict else paths[0].transform,
            paths=tuple(paths),
            conflict=conflict,
            missing_edges=missing_edges,
        )

    def lookup_transform(
        self,
        source_frame: str,
        target_frame: str,
        sample_time: int,
        *,
        order_time: Optional[int] = None,
    ) -> Optional[RigidTransform]:
        """Return the unambiguous transform, or ``None`` when unresolved/conflicting."""
        return self.resolve_transform(
            source_frame, target_frame, sample_time, order_time=order_time
        ).transform

    def project_point(
        self,
        point: Vector3,
        source_frame: str,
        target_frame: str,
        sample_time: int,
        *,
        order_time: Optional[int] = None,
        origin: Optional[Vector3] = None,
    ) -> PointProjection:
        """Project one point, retaining every conflicting path result."""
        query = self.prepare_projection(
            source_frame, target_frame, sample_time, order_time=order_time
        )
        return query.project(point, origin=origin)

    def prepare_projection(
        self,
        source_frame: str,
        target_frame: str,
        sample_time: int,
        *,
        order_time: Optional[int] = None,
    ) -> "_PointQuery":
        """Resolve the frame graph once so many points can share the result.

        Use this for dense point clouds; :py:meth:`project_point` is a one-shot
        wrapper around it.
        """
        resolution = self.resolve_transform(
            source_frame, target_frame, sample_time, order_time=order_time
        )
        return _PointQuery(
            self,
            resolution,
            resolution.missing_edges,
            source_frame,
            target_frame,
            sample_time,
        )


class _PointQuery:
    """A resolved frame graph shared by many points at one sample time."""

    __slots__ = (
        "_timeline",
        "_resolution",
        "_missing",
        "_source",
        "_target",
        "_sample_time",
    )

    def __init__(
        self,
        timeline: TransformTimeline,
        resolution: TransformResolution,
        missing: Tuple[Tuple[str, str], ...],
        source_frame: str,
        target_frame: str,
        sample_time: int,
    ):
        self._timeline = timeline
        self._resolution = resolution
        self._missing = missing
        self._source = source_frame
        self._target = target_frame
        self._sample_time = int(sample_time)

    @property
    def resolution(self) -> TransformResolution:
        return self._resolution

    def project(self, point: Vector3, *, origin: Optional[Vector3] = None) -> PointProjection:
        resolution = self._resolution
        candidates: List[ProjectionCandidate] = []
        for path in resolution.paths:
            if path.status != "ok" or path.transform is None:
                candidates.append(
                    ProjectionCandidate(
                        path=path.path,
                        position=None,
                        transform=None,
                        samples=path.samples,
                        status=path.status,
                        reason=path.reason,
                        epoch=path.epoch,
                    )
                )
                continue
            candidates.append(
                ProjectionCandidate(
                    path=path.path,
                    position=path.transform.apply(point),
                    transform=path.transform,
                    samples=path.samples,
                    status="ok",
                    epoch=path.epoch,
                )
            )
        candidates.sort(
            key=lambda item: (
                item.path,
                item.position.as_tuple() if item.position is not None else (),
                tuple(sample.canonical_key() for sample in item.samples),
            )
        )
        tolerance = self._timeline._tolerance
        positions: List[Vector3] = []
        for candidate in candidates:
            if candidate.position is None:
                continue
            if not any(
                candidate.position.distance(existing) <= tolerance
                for existing in positions
            ):
                positions.append(candidate.position)
        if not positions:
            return PointProjection(
                point=point,
                source_frame=self._source,
                target_frame=self._target,
                sample_time=self._sample_time,
                status="unresolved",
                position=None,
                positions=(),
                candidates=tuple(candidates),
                missing_edges=self._missing,
                origin=origin,
            )
        if len(positions) > 1:
            return PointProjection(
                point=point,
                source_frame=self._source,
                target_frame=self._target,
                sample_time=self._sample_time,
                status="conflict",
                position=None,
                positions=tuple(sorted(positions, key=lambda item: item.as_tuple())),
                candidates=tuple(candidates),
                missing_edges=self._missing,
                origin=origin,
            )
        return PointProjection(
            point=point,
            source_frame=self._source,
            target_frame=self._target,
            sample_time=self._sample_time,
            status="ok",
            position=positions[0],
            positions=tuple(positions),
            candidates=tuple(candidates),
            missing_edges=self._missing,
            origin=origin,
        )


def parse_point(value: Any) -> Vector3:
    """Parse ``{x, y, z}`` or ``[x, y, z]`` point representations."""
    if isinstance(value, Vector3):
        return value
    if isinstance(value, (list, tuple)):
        if len(value) < 3:
            raise ValueError("point sequence must have at least three components")
        return Vector3(
            _number(value[0], "point.x"),
            _number(value[1], "point.y"),
            _number(value[2], "point.z"),
        )
    if isinstance(value, MappingABC):
        position = value.get("position")
        if isinstance(position, MappingABC) and "x" in position:
            value = position
        return Vector3(
            _number(value.get("x", 0.0), "point.x"),
            _number(value.get("y", 0.0), "point.y"),
            _number(value.get("z", 0.0), "point.z"),
        )
    raise ValueError("point must be an object or a three-element sequence")


def _timestamp_from_mapping(
    payload: Mapping[str, Any], log_time: int
) -> Tuple[int, str]:
    """Choose a point message's sample time and say where it came from."""
    for key in ("timestamp", "sample_time", "stamp", "time"):
        if key in payload:
            parsed = parse_timestamp(payload[key], key)
            if parsed is not None:
                return parsed, key
    header = payload.get("header")
    if isinstance(header, MappingABC):
        parsed = parse_timestamp(_first(header, ("stamp", "timestamp", "time")))
        if parsed is not None:
            return parsed, "header.stamp"
    return int(log_time), "log_time"


def parse_point_message(data: bytes, *, log_time: int = 0) -> PointMessage:
    """Parse a JSON point message into points, their frame, and their pose.

    A message may contain a single point (``{x, y, z}``), a ``points`` list, or
    ``poses`` with ``position``/``orientation`` (``foxglove.PosesInFrame`` style).
    A ``pose`` composed of a position and orientation is applied to the local
    points.  A ``frame_id`` is required; messages without one are rejected.
    """
    try:
        payload = json.loads(data)
    except (TypeError, ValueError) as error:
        raise McapTransformError(f"point message is not valid JSON: {error}")
    if not isinstance(payload, MappingABC):
        raise McapTransformError("point message must be a JSON object")

    frame_id = _first(payload, ("frame_id", "frame", "child_frame_id"))
    if frame_id is None:
        header = payload.get("header")
        if isinstance(header, MappingABC):
            frame_id = header.get("frame_id")
    if not isinstance(frame_id, str) or not frame_id:
        raise McapTransformError("point message is missing frame_id")

    sample_time, time_source = _timestamp_from_mapping(payload, log_time)
    pose = _parse_pose(payload.get("pose"))

    points: List[Vector3] = []
    raw_points = payload.get("points")
    if raw_points is None:
        raw_poses = payload.get("poses")
        if isinstance(raw_poses, SequenceABC) and not isinstance(
            raw_poses, (str, bytes)
        ):
            for raw_pose in raw_poses:
                pose_value = _parse_pose(raw_pose)
                if pose_value is not None:
                    points.append(pose_value.translation)
        elif all(axis in payload for axis in ("x", "y", "z")):
            points.append(parse_point(payload))
    elif isinstance(raw_points, SequenceABC) and not isinstance(
        raw_points, (str, bytes)
    ):
        for raw_point in raw_points:
            points.append(parse_point(raw_point))
    else:
        raise McapTransformError("points must be a list")

    return PointMessage(frame_id, sample_time, time_source, tuple(points), pose)


def _parse_pose(value: Any) -> Optional[RigidTransform]:
    if value is None:
        return None
    pose = _mapping(value, "pose")
    translation = pose.get("position", pose.get("translation"))
    rotation = pose.get("orientation", pose.get("rotation"))
    if translation is None and rotation is None:
        return None
    return RigidTransform(
        _parse_vector(translation if translation is not None else {}, "pose.position"),
        _parse_quaternion(
            rotation if rotation is not None else {}, "pose.orientation"
        ),
    )


def parse_transform_message(
    data: bytes,
    *,
    log_time: int = 0,
    topic: str = "",
    channel_id: int = 0,
    sequence: int = 0,
) -> Tuple[FrameTransform, ...]:
    """Parse a JSON transform message.

    A message may hold one transform object or a ``transforms`` list (ROS ``/tf``
    style).  List entries must carry their own timestamps; a bare object without a
    timestamp uses the message's ``log_time``, since the record itself is the sample.
    """
    try:
        payload = json.loads(data)
    except (TypeError, ValueError) as error:
        raise McapTransformError(f"transform message is not valid JSON: {error}")
    if isinstance(payload, MappingABC) and isinstance(payload.get("transforms"), SequenceABC):
        entries = payload["transforms"]
        if not isinstance(entries, SequenceABC) or isinstance(entries, (str, bytes)):
            raise McapTransformError("transforms must be a list")
        return tuple(
            FrameTransform.from_mapping(
                _mapping(entry, "transforms entry"),
                log_time=log_time,
                topic=topic,
                channel_id=channel_id,
                sequence=sequence,
            )
            for entry in entries
        )
    if not isinstance(payload, MappingABC):
        raise McapTransformError("transform message must be a JSON object")
    return (
        FrameTransform.from_mapping(
            payload,
            log_time=log_time,
            topic=topic,
            channel_id=channel_id,
            sequence=sequence,
            default_sample_time=log_time,
        ),
    )
