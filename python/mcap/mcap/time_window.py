"""Composable time-window queries for MCAP messages.

An MCAP message carries two independent timestamps: ``log_time``, the time at
which the recorder wrote it, and ``publish_time``, the time at which its
publisher produced it.  Both are kept as the original unsigned 64-bit
nanosecond integers from the file; this module never converts them to seconds
or to floating point.

A :class:`TimeRange` is a half-open range of nanosecond integers, ``[start,
end)``.  A :class:`TimeWindow` is a conjunction of a log-time range and a
publish-time range: a message matches the window when its ``log_time`` is in
the first range *and* its ``publish_time`` is in the second.  A
:class:`MessageQuery` is a disjunction of windows.  Windows and queries
compose with ``&`` (intersection) and ``|`` (union):

>>> query = (
...     TimeWindow(log_time=TimeRange(100, 200))
...     | TimeWindow(publish_time=TimeRange(500, 600))
... ) & TimeWindow(publish_time=TimeRange.ending_at(550))

The publish clock is independent of the log clock and may run backwards.  The
difference ``publish_time - log_time`` is therefore *not* a link latency and
must not be reported as one without separate clock-correlation evidence.  This
module deliberately provides no such subtraction.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Optional, Tuple, Union

__all__ = [
    "MAX_TIMESTAMP",
    "MessageOrder",
    "MessageQuery",
    "ReadOrder",
    "TimeInterval",
    "TimeRange",
    "TimeWindow",
    "coerce_query",
]

#: The largest timestamp an MCAP ``Message`` can carry: an unsigned 64-bit
#: nanosecond count.  Range *bounds* may exceed it by one, as in
#: ``TimeRange.ending_at(MAX_TIMESTAMP)``, but no message timestamp can.
MAX_TIMESTAMP = 2**64 - 1


def _check_timestamp(name: str, value: Optional[int]) -> Optional[int]:
    """Validate a nanosecond bound and return it as an exact ``int``.

    Integral types such as ``numpy.int64`` are accepted through ``__index__``
    and normalized to ``int``; floats and bools are rejected so a timestamp is
    never rounded or silently treated as an index.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not hasattr(value, "__index__"):
        raise TypeError(f"{name} must be an integer number of nanoseconds or None")
    normalized = int(value.__index__())
    if normalized < 0:
        raise ValueError(f"{name} must not be negative")
    return normalized


@dataclass(frozen=True)
class TimeRange:
    """A half-open range of integer nanosecond timestamps, ``[start, end)``.

    ``None`` leaves that side unbounded.  ``start`` greater than ``end`` is
    rejected as a caller error; ``start == end`` is a valid empty range.  A
    range whose ``start`` is past :data:`MAX_TIMESTAMP`, or whose ``end`` is
    zero, is also empty because no message timestamp can fall in it.

    Bounds are kept exactly as given.  In particular, a nanosecond value larger
    than 2**53 survives round-tripping through this type untouched.  Integral
    types such as ``numpy.int64`` are accepted and normalized to ``int``;
    floats and bools are rejected so a timestamp is never rounded.
    """

    start: Optional[int] = None
    end: Optional[int] = None

    def __post_init__(self):
        object.__setattr__(self, "start", _check_timestamp("start", self.start))
        object.__setattr__(self, "end", _check_timestamp("end", self.end))
        if self.start is not None and self.end is not None:
            if self.start > self.end:
                raise ValueError("end time cannot come before start time")

    @property
    def is_empty(self) -> bool:
        """Whether this range can match no message timestamp at all."""
        if self.end == 0:
            return True
        if self.start is not None and self.start > MAX_TIMESTAMP:
            return True
        return (
            self.start is not None
            and self.end is not None
            and self.start >= self.end
        )

    @property
    def is_unbounded(self) -> bool:
        """Whether both sides are open."""
        return self.start is None and self.end is None

    def includes(self, timestamp: int) -> bool:
        """Whether ``timestamp`` falls in ``[start, end)``."""
        if self.start is not None and timestamp < self.start:
            return False
        if self.end is not None and timestamp >= self.end:
            return False
        return True

    def intersects(self, first: int, last: int) -> bool:
        """Whether any timestamp in the closed range ``[first, last]`` is included.

        MCAP chunk indexes describe their messages as an inclusive
        ``[message_start_time, message_end_time]`` range.  An unbounded range
        intersects every span, including a chunk that has no messages.
        """
        if self.is_empty:
            return False
        if self.start is None and self.end is None:
            return True
        if self.end is not None and first >= self.end:
            return False
        if self.start is not None and last < self.start:
            return False
        return True

    def intersect(self, other: "TimeRange") -> "TimeRange":
        """Return the intersection of two ranges, empty if they do not overlap."""
        starts = [value for value in (self.start, other.start) if value is not None]
        ends = [value for value in (self.end, other.end) if value is not None]
        start = max(starts) if starts else None
        end = min(ends) if ends else None
        if start is not None and end is not None and start > end:
            # Canonical empty range: it includes no timestamp.
            return TimeRange(0, 0)
        return TimeRange(start, end)

    def union(self, other: "TimeRange") -> "TimeRange":
        """Return the smallest range covering both, if they touch or overlap.

        A union of disjoint ranges is not one range; use :class:`MessageQuery`
        (or ``TimeWindow | TimeWindow``) for that.
        """
        if self.is_empty:
            return other
        if other.is_empty:
            return self
        # ``self`` entirely before ``other``, with a gap between them.
        if (
            self.end is not None
            and other.start is not None
            and self.end < other.start
        ):
            raise ValueError(
                "disjoint time ranges cannot be merged; compose windows with"
                " TimeWindow.__or__ or MessageQuery instead"
            )
        # ``other`` entirely before ``self``, with a gap between them.
        if (
            other.end is not None
            and self.start is not None
            and other.end < self.start
        ):
            raise ValueError(
                "disjoint time ranges cannot be merged; compose windows with"
                " TimeWindow.__or__ or MessageQuery instead"
            )
        starts = [value for value in (self.start, other.start) if value is not None]
        ends = [value for value in (self.end, other.end) if value is not None]
        return TimeRange(
            min(starts) if starts else None,
            max(ends) if ends else None,
        )

    def __and__(self, other: "TimeRange") -> "TimeRange":
        if not isinstance(other, TimeRange):
            return NotImplemented
        return self.intersect(other)

    def __or__(self, other: "TimeRange") -> "TimeRange":
        if not isinstance(other, TimeRange):
            return NotImplemented
        return self.union(other)

    @classmethod
    def starting_at(cls, timestamp: int) -> "TimeRange":
        """``[timestamp, unbounded)``: an inclusive lower bound."""
        return cls(timestamp, None)

    @classmethod
    def starting_after(cls, timestamp: int) -> "TimeRange":
        """``(timestamp, unbounded)``: an exclusive lower bound, in nanoseconds."""
        return cls(int(_check_timestamp("timestamp", timestamp)) + 1, None)

    @classmethod
    def ending_at(cls, timestamp: int) -> "TimeRange":
        """``(unbounded, timestamp]``: an inclusive upper bound, in nanoseconds."""
        return cls(None, int(_check_timestamp("timestamp", timestamp)) + 1)

    @classmethod
    def ending_before(cls, timestamp: int) -> "TimeRange":
        """``(unbounded, timestamp)``: an exclusive upper bound."""
        return cls(None, timestamp)

    @classmethod
    def between(cls, start: int, end: int) -> "TimeRange":
        """``[start, end)``: a half-open bounded range."""
        return cls(start, end)


#: Alias for callers that call a range an interval.
TimeInterval = TimeRange


def _coerce_range(value: Any, name: str) -> TimeRange:
    if value is None:
        return TimeRange()
    if isinstance(value, TimeRange):
        return value
    if isinstance(value, tuple) and len(value) == 2:
        return TimeRange(value[0], value[1])
    raise TypeError(f"{name} must be a TimeRange, a (start, end) tuple, or None")


@dataclass(frozen=True, init=False)
class TimeWindow:
    """A conjunctive condition on a message's two timestamps.

    A message matches when its ``log_time`` is inside :attr:`log_time` *and*
    its ``publish_time`` is inside :attr:`publish_time`.  An omitted range is
    unbounded, so ``TimeWindow()`` matches every message.

    The constructor accepts :class:`TimeRange` objects, ``(start, end)``
    tuples, or ``None``.  ``log_time_range`` / ``publish_time_range`` are
    accepted as keyword aliases for the two fields.

    Windows compose with ``&`` (intersection) and ``|`` (union).  Intersecting
    windows narrows each clock independently; unioning them produces a
    :class:`MessageQuery` whose windows are alternatives.
    """

    log_time: TimeRange = field(default_factory=TimeRange)
    publish_time: TimeRange = field(default_factory=TimeRange)

    def __init__(
        self,
        log_time: Any = None,
        publish_time: Any = None,
        *,
        log_time_range: Any = None,
        publish_time_range: Any = None,
    ):
        if log_time is not None and log_time_range is not None:
            raise TypeError("provide log_time or log_time_range, not both")
        if publish_time is not None and publish_time_range is not None:
            raise TypeError("provide publish_time or publish_time_range, not both")
        object.__setattr__(
            self,
            "log_time",
            _coerce_range(
                log_time_range if log_time_range is not None else log_time,
                "log_time",
            ),
        )
        object.__setattr__(
            self,
            "publish_time",
            _coerce_range(
                publish_time_range
                if publish_time_range is not None
                else publish_time,
                "publish_time",
            ),
        )

    @property
    def is_empty(self) -> bool:
        """Whether no message can satisfy this window."""
        return self.log_time.is_empty or self.publish_time.is_empty

    def includes(self, message: Any) -> bool:
        """Whether ``message`` (with ``log_time`` and ``publish_time``) matches."""
        return self.includes_times(message.log_time, message.publish_time)

    def includes_times(self, log_time: int, publish_time: int) -> bool:
        """Whether a message with these two timestamps matches."""
        return self.log_time.includes(log_time) and self.publish_time.includes(
            publish_time
        )

    def intersect(self, other: "TimeWindow") -> "TimeWindow":
        """Narrow this window by another, intersecting both clocks."""
        return TimeWindow(
            log_time=self.log_time.intersect(other.log_time),
            publish_time=self.publish_time.intersect(other.publish_time),
        )

    def __and__(
        self, other: Union["TimeWindow", "MessageQuery"]
    ) -> Union["TimeWindow", "MessageQuery"]:
        if isinstance(other, MessageQuery):
            return other & self
        if not isinstance(other, TimeWindow):
            return NotImplemented
        return self.intersect(other)

    def __or__(self, other: Union["TimeWindow", "MessageQuery"]) -> "MessageQuery":
        if isinstance(other, MessageQuery):
            return MessageQuery((self,)) | other
        if not isinstance(other, TimeWindow):
            return NotImplemented
        return MessageQuery((self, other))

    @classmethod
    def between(
        cls,
        log_start: Optional[int],
        log_end: Optional[int],
        publish_start: Optional[int] = None,
        publish_end: Optional[int] = None,
    ) -> "TimeWindow":
        """A window with half-open ``log_time`` and ``publish_time`` ranges.

        Passing ``None`` for either side of a range leaves it unbounded.
        """
        return cls(
            log_time=TimeRange(log_start, log_end),
            publish_time=TimeRange(publish_start, publish_end),
        )

    @classmethod
    def from_bounds(
        cls,
        *,
        log_start: Optional[int] = None,
        log_end: Optional[int] = None,
        publish_start: Optional[int] = None,
        publish_end: Optional[int] = None,
    ) -> "TimeWindow":
        """Build a window from half-open ``[start, end)`` nanosecond bounds."""
        return cls.between(log_start, log_end, publish_start, publish_end)


@dataclass(frozen=True, init=False)
class MessageQuery:
    """A disjunction of :class:`TimeWindow` conditions.

    A message matches when it matches any window in :attr:`windows`.  Windows
    compose with ``|`` (add an alternative) and ``&`` (intersect every window
    with the other condition).  The constructor accepts windows as separate
    arguments or as one iterable.
    """

    windows: Tuple[TimeWindow, ...]

    def __init__(self, *windows: Any):
        if len(windows) == 1:
            only = windows[0]
            if isinstance(only, TimeWindow):
                normalized: Iterable[Any] = (only,)
            elif isinstance(only, MessageQuery):
                normalized = only.windows
            elif isinstance(only, (str, bytes)) or not isinstance(only, Iterable):
                normalized = (only,)  # rejected below with a clear message
            else:
                normalized = only
        else:
            normalized = windows
        checked = []
        for window in normalized:
            if not isinstance(window, TimeWindow):
                raise TypeError("MessageQuery windows must be TimeWindow instances")
            checked.append(window)
        object.__setattr__(self, "windows", tuple(checked))

    @property
    def is_empty(self) -> bool:
        """Whether this query can match no message."""
        return not self.windows or all(window.is_empty for window in self.windows)

    def includes(self, message: Any) -> bool:
        """Whether ``message`` matches at least one window."""
        return any(window.includes(message) for window in self.windows)

    def includes_times(self, log_time: int, publish_time: int) -> bool:
        """Whether a message with these two timestamps matches any window."""
        return any(
            window.includes_times(log_time, publish_time)
            for window in self.windows
        )

    def __bool__(self) -> bool:
        return bool(self.windows)

    def __or__(self, other: Union[TimeWindow, "MessageQuery"]) -> "MessageQuery":
        if isinstance(other, TimeWindow):
            return MessageQuery(self.windows + (other,))
        if not isinstance(other, MessageQuery):
            return NotImplemented
        return MessageQuery(self.windows + other.windows)

    def __and__(self, other: Union[TimeWindow, "MessageQuery"]) -> "MessageQuery":
        if isinstance(other, TimeWindow):
            return MessageQuery(
                tuple(window.intersect(other) for window in self.windows)
            )
        if not isinstance(other, MessageQuery):
            return NotImplemented
        return MessageQuery(
            tuple(
                left.intersect(right)
                for left in self.windows
                for right in other.windows
            )
        )


class MessageOrder(Enum):
    """The explicit ordering criterion for a query's output.

    ``LOG_TIME`` sorts by ``log_time``.  Ties are broken by file position (chunk
    start offset, then the record's position within its chunk), so the order is
    total: messages that share a timestamp always appear in the same, stable
    order, and a sequential read and an indexed read agree.

    ``FILE_ORDER`` yields records in the order they appear in the file.

    ``reverse`` reverses whichever ordering is selected; the result is exactly
    the reverse of the forward order, including the order of tied messages.

    There is deliberately no publish-time ordering.  A chunk index proves only
    the ``log_time`` span of the messages it indexes, so ordering by
    ``publish_time`` would require buffering and sorting every message.
    ``publish_time`` is a query condition, not an ordering criterion.
    """

    LOG_TIME = "log_time"
    FILE_ORDER = "file_order"

    @classmethod
    def coerce(cls, value: Union["MessageOrder", str]) -> "MessageOrder":
        """Accept an enum member or one of a few common string spellings."""
        if isinstance(value, MessageOrder):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower().replace("-", "_")
            aliases = {
                "log": cls.LOG_TIME,
                "log_time": cls.LOG_TIME,
                "logtime": cls.LOG_TIME,
                "file": cls.FILE_ORDER,
                "file_order": cls.FILE_ORDER,
                "fileorder": cls.FILE_ORDER,
            }
            if normalized in aliases:
                return aliases[normalized]
        raise ValueError(
            "order must be 'log_time' or 'file_order'; publish_time is a query"
            " condition, not an ordering criterion"
        )


#: Alias for callers that think of the ordering criterion as a read mode.
ReadOrder = MessageOrder


def coerce_query(
    value: Optional[Union[TimeRange, TimeWindow, MessageQuery]]
) -> MessageQuery:
    """Normalize a range, window, or query to a :class:`MessageQuery`.

    A bare :class:`TimeRange` is a condition on ``log_time``.
    """
    if value is None:
        return MessageQuery((TimeWindow(),))
    if isinstance(value, TimeWindow):
        return MessageQuery((value,))
    if isinstance(value, MessageQuery):
        return value
    if isinstance(value, TimeRange):
        return MessageQuery((TimeWindow(log_time=value),))
    raise TypeError("expected a TimeRange, TimeWindow, MessageQuery, or None")
