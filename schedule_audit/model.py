"""Input model for the schedule audit toolkit.

The model describes a recorded trace of one or more *materials* (the
items whose completion windows must be checked).  Every material is a
sequence of :class:`Event` objects; observed timestamps carry an
explicit clock uncertainty instead of being treated as exact.

Event kinds
-----------

``EVENT_WORK``
    A control / scheduling step.  It occupies a worker (shared
    resource) and has a processing duration given as a range.

``EVENT_IO``
    A data read or write.  It occupies a resource, transfers bytes
    (wire / payload / business-retransmit accounting) and has a
    network duration given as a range.  Network durations never shrink
    when rate limiting is removed.

``EVENT_WAIT``
    A waiting interval whose duration is recorded as a range.  A wait
    may carry more than one reason; the reasons are never deducted
    twice.  Waits flagged ``removable_throttle=True`` are the only
    intervals the counterfactual run is allowed to shorten, and even
    then the other reasons (if any) still consume their share.
"""

from dataclasses import dataclass
from dataclasses import field

EVENT_WORK = "work"
EVENT_IO = "io"
EVENT_WAIT = "wait"

# Wait reason tags.
REASON_THROTTLE = "throttle"
REASON_NETWORK = "network"
REASON_DEPENDENCY = "dependency"
REASON_RESOURCE = "resource"
REASON_OTHER = "other"

VALID_KINDS = (EVENT_WORK, EVENT_IO, EVENT_WAIT)
VALID_REASONS = (
    REASON_THROTTLE,
    REASON_NETWORK,
    REASON_DEPENDENCY,
    REASON_RESOURCE,
    REASON_OTHER,
)


def _check_pair(name, low, high):
    if low < 0 or high < 0:
        raise ValueError(f"{name} must be non-negative, got ({low}, {high})")
    if low > high:
        raise ValueError(
            f"{name} lower bound {low} exceeds upper bound {high}"
        )


@dataclass
class Resource:
    """A shared resource with a capacity (how many events may run on it
    at the same time).  Capacity 1 serializes every event using it.
    """

    name: str
    capacity: int = 1

    def __post_init__(self):
        if not self.name:
            raise ValueError("resource name must be a non-empty string")
        if self.capacity < 1:
            raise ValueError(
                f"resource {self.name!r} capacity must be >= 1"
            )


@dataclass
class Transfer:
    """Byte accounting for an IO event.

    - ``wire_bytes``:   bytes observed on the line (framing included).
    - ``payload_bytes``: effective payload bytes.
    - ``retransmit_bytes``: business-level retransmissions, tracked
      separately; they are part of the wire total but never folded
      into the payload figure.
    """

    wire_bytes: int = 0
    payload_bytes: int = 0
    retransmit_bytes: int = 0

    def __post_init__(self):
        for name, value in (
            ("wire_bytes", self.wire_bytes),
            ("payload_bytes", self.payload_bytes),
            ("retransmit_bytes", self.retransmit_bytes),
        ):
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.payload_bytes + self.retransmit_bytes > self.wire_bytes:
            raise ValueError(
                "payload + retransmit bytes cannot exceed wire bytes"
            )


@dataclass
class Event:
    """A single recorded event.

    Observed timing
    ~~~~~~~~~~~~~~~
    ``observed_start`` / ``observed_end`` are clock readings and may
    overlap slightly; ``clock_skew`` is the maximum absolute error of
    each reading, so the true start lies in
    ``[observed_start - skew, observed_start + skew]`` and likewise for
    the end.

    Durations
    ~~~~~~~~~
    ``duration_min`` / ``duration_max`` bound the *intrinsic* duration
    of the event (processing or network time).  For wait events they
    bound the full observed waiting interval.

    Reasons and removability
    ~~~~~~~~~~~~~~~~~~~~~~~~
    ``reasons`` applies to wait events and lists every cause that
    contributed.  ``removable_throttle`` marks waits the analysis is
    allowed to shorten when rate limiting is removed.  A removable
    wait with non-throttle reasons keeps the time those reasons need.
    """

    name: str
    kind: str
    observed_start: float
    observed_end: float
    duration_min: float
    duration_max: float
    resources: tuple = ()
    reasons: tuple = ()
    removable_throttle: bool = False
    transfer: "Transfer | None" = None
    material: str = ""

    def __post_init__(self):
        if not self.name:
            raise ValueError("event name must be a non-empty string")
        if self.kind not in VALID_KINDS:
            raise ValueError(
                f"event {self.name!r}: unknown kind {self.kind!r}"
            )
        _check_pair(
            f"event {self.name!r} timestamps",
            self.observed_start,
            self.observed_end,
        )
        _check_pair(
            f"event {self.name!r} duration",
            self.duration_min,
            self.duration_max,
        )
        self.resources = tuple(self.resources)
        self.reasons = tuple(self.reasons)
        for reason in self.reasons:
            if reason not in VALID_REASONS:
                raise ValueError(
                    f"event {self.name!r}: unknown reason {reason!r}"
                )
        if self.kind == EVENT_WAIT:
            if not self.reasons:
                raise ValueError(
                    f"wait event {self.name!r} must declare at least "
                    "one reason"
                )
        elif self.reasons:
            raise ValueError(
                f"non-wait event {self.name!r} cannot declare reasons"
            )
        if self.removable_throttle and REASON_THROTTLE not in self.reasons:
            raise ValueError(
                f"event {self.name!r}: removable_throttle requires the "
                f"{REASON_THROTTLE!r} reason"
            )
        if self.kind == EVENT_IO:
            if self.transfer is None:
                self.transfer = Transfer()
        elif self.transfer is not None:
            raise ValueError(
                f"non-IO event {self.name!r} cannot carry byte accounting"
            )

    @property
    def is_throttled(self):
        return REASON_THROTTLE in self.reasons

    @property
    def has_other_reason(self):
        return any(r != REASON_THROTTLE for r in self.reasons)


@dataclass
class Material:
    """A unit of work whose completion window must be audited."""

    name: str
    events: list = field(default_factory=list)
    # Optional deadline (clock reading, same time base as events).
    deadline: "float | None" = None

    def __post_init__(self):
        if not self.name:
            raise ValueError("material name must be a non-empty string")
        if not self.events:
            raise ValueError(f"material {self.name!r} has no events")
        names = set()
        for event in self.events:
            if not isinstance(event, Event):
                raise ValueError(
                    f"material {self.name!r}: expected Event, got "
                    f"{type(event)!r}"
                )
            if event.name in names:
                raise ValueError(
                    f"material {self.name!r}: duplicate event name "
                    f"{event.name!r}"
                )
            names.add(event.name)
            event.material = self.name


@dataclass
class Trace:
    """The full recorded trace: materials, resources and clock skew."""

    materials: list
    resources: dict = field(default_factory=dict)
    clock_skew: float = 0.0
    # Names of waits the counterfactual run may remove.  Must be a
    # subset of the events already flagged removable_throttle; the
    # flag on the event is the per-event permission, this set records
    # which of those permissions are actually exercised.
    removed_waits: frozenset = frozenset()

    def __post_init__(self):
        if self.clock_skew < 0:
            raise ValueError("clock_skew must be non-negative")
        if not self.materials:
            raise ValueError("trace must contain at least one material")
        resource_names = set()
        for resource in self.resources.values():
            if not isinstance(resource, Resource):
                raise ValueError(
                    f"resources must map to Resource instances, got "
                    f"{type(resource)!r}"
                )
            if resource.name in resource_names:
                raise ValueError(
                    f"duplicate resource name {resource.name!r}"
                )
            resource_names.add(resource.name)
        seen_events = set()
        for material in self.materials:
            if not isinstance(material, Material):
                raise ValueError(
                    "materials must be Material instances, got "
                    f"{type(material)!r}"
                )
            for event in material.events:
                if event.name in seen_events:
                    raise ValueError(
                        f"event name {event.name!r} appears in more "
                        "than one material"
                    )
                seen_events.add(event.name)
                for resource_name in event.resources:
                    if resource_name not in resource_names:
                        raise ValueError(
                            f"event {event.name!r} references unknown "
                            f"resource {resource_name!r}"
                        )
        self.removed_waits = frozenset(self.removed_waits)
        for wait_name in self.removed_waits:
            event = self.event_by_name(wait_name)
            if not event.removable_throttle:
                raise ValueError(
                    f"removed wait {wait_name!r} is not flagged "
                    "removable_throttle"
                )

    @property
    def all_events(self):
        for material in self.materials:
            for event in material.events:
                yield event

    def event_by_name(self, name):
        for event in self.all_events:
            if event.name == name:
                return event
        raise KeyError(name)

    def material_of(self, event_name):
        for material in self.materials:
            for event in material.events:
                if event.name == event_name:
                    return material.name
        raise KeyError(event_name)
