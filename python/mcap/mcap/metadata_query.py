"""Range queries over MCAP metadata declarations.

An MCAP file can carry many Metadata records sharing a name. The file format says
nothing about how those declarations relate to one another: which channels they
apply to, on which time domain they are valid, how competing declarations are
ranked, or when a declaration is withdrawn. This module implements that layer on
top of the raw records.

The physical order of Metadata records in the file is never used to decide a
value. Business-supplied :class:`PriorityRule` objects are the only arbitration
mechanism, and declarations that cannot be arbitrated are reported as conflicts
on just the affected ranges instead of poisoning the rest of the query.
"""

from dataclasses import dataclass
from enum import Enum
from typing import (
    Callable,
    Collection,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from .records import Metadata

# A half-open ``[start, end)`` interval where a ``None`` end means +inf.
_Interval = Tuple[Optional[int], Optional[int]]

# A single topic or a collection of topic names.
Topics = Optional[Union[str, Collection[str]]]

#: Builds a claim from one Metadata record, or skips the record by returning None.
ClaimParser = Callable[[int, Metadata], Optional["MetadataClaim"]]


class TimeDomain(Enum):
    """The timestamp axis a declaration is interpreted on."""

    LOG_TIME = "log_time"
    PUBLISH_TIME = "publish_time"


class ConflictKind(Enum):
    """Why a range cannot be assigned a single effective value."""

    #: Priority rules relating the competing declarations contain a cycle.
    CYCLE = "cycle"
    #: Competing declarations overlap with no priority rule deciding between them.
    OVERLAP = "overlap"


@dataclass(frozen=True)
class TimeRange:
    """Bounds on one time domain, resolved to a half-open ``[start, end)`` range.

    At most one lower bound and one upper bound may be given; all timestamps are
    integer nanoseconds.
    """

    starting_at: Optional[int] = None
    starting_after: Optional[int] = None
    ending_at: Optional[int] = None
    ending_before: Optional[int] = None

    def resolve(self) -> _Interval:
        if self.starting_at is not None and self.starting_after is not None:
            raise ValueError(
                "provide at most one of starting_at and starting_after per time domain"
            )
        if self.ending_at is not None and self.ending_before is not None:
            raise ValueError(
                "provide at most one of ending_at and ending_before per time domain"
            )
        start = self.starting_at
        if self.starting_after is not None:
            start = self.starting_after + 1
        end = self.ending_before
        if self.ending_at is not None:
            end = self.ending_at + 1
        if start is not None and end is not None and start > end:
            raise ValueError("end time cannot come before start time")
        return start, end


@dataclass(frozen=True)
class MetadataClaim:
    """A business declaration derived from one Metadata record.

    :param name: the Metadata record name this declaration belongs to.
    :param metadata: the key/value payload of the original Metadata record.
    :param channels: channels the declaration applies to. ``None`` or an empty
        collection means every channel in the query's channel universe.
    :param valid_from: inclusive start of validity on ``time_domain``.
    :param valid_to: exclusive end of validity on ``time_domain``, or ``None``
        for an open-ended declaration.
    :param time_domain: the timestamp axis the validity interval is measured on.
    :param claim_id: stable business identifier used by :class:`PriorityRule`
        objects and surfaced in conflict reports.
    """

    name: str
    metadata: Dict[str, str]
    channels: Optional[Collection[str]] = None
    valid_from: int = 0
    valid_to: Optional[int] = None
    time_domain: TimeDomain = TimeDomain.LOG_TIME
    claim_id: Optional[str] = None


@dataclass(frozen=True)
class PriorityRule:
    """Declares that ``higher`` outranks ``lower`` where both are in effect.

    The identifiers reference :attr:`MetadataClaim.claim_id`. Rules are business
    policy: the record's physical position in the file is never consulted.
    """

    higher: str
    lower: str


@dataclass(frozen=True)
class MetadataConflict:
    """A range where no single effective value can be chosen."""

    kind: ConflictKind
    key: str
    channel: str
    time_domain: TimeDomain
    start_time: int
    end_time: Optional[int]
    #: competing value mapped to the ids of declarations supporting it.
    values: Dict[str, Tuple[str, ...]]
    #: every declaration implicated in the conflict.
    claim_ids: Tuple[str, ...]


@dataclass(frozen=True)
class MetadataValueSegment:
    """One constant-value piece of a key's timeline on one channel and domain."""

    key: str
    channel: str
    time_domain: TimeDomain
    start_time: int
    end_time: Optional[int]
    #: the jointly effective value, or ``None`` when ``conflict`` is set.
    value: Optional[str]
    #: original declarations active on this segment that carry ``key``.
    declarations: Tuple[MetadataClaim, ...]
    conflict: Optional[MetadataConflict] = None


@dataclass(frozen=True)
class MetadataQueryResult:
    """The resolved timeline plus the original evidence and any conflicts."""

    segments: Tuple[MetadataValueSegment, ...]
    conflicts: Tuple[MetadataConflict, ...]
    #: declarations supporting at least one segment inside the query range.
    declarations: Tuple[MetadataClaim, ...]


@dataclass
class _IndexedClaim:
    claim: MetadataClaim
    sequence: int
    channels: Optional[Tuple[str, ...]]
    interval: Tuple[int, Optional[int]]
    id: str
    active: bool = True


def _normalize_channels(
    channels: Optional[Collection[str]],
) -> Optional[Tuple[str, ...]]:
    if channels is None or len(channels) == 0:
        return None
    return tuple(sorted(set(channels)))


def _intersect(
    interval: Tuple[int, Optional[int]], bounds: _Interval
) -> Optional[Tuple[int, Optional[int]]]:
    start, end = interval
    bound_start, bound_end = bounds
    if bound_start is not None and start < bound_start:
        start = bound_start
    if bound_end is not None and (end is None or end > bound_end):
        end = bound_end
    if end is not None and start >= end:
        return None
    return start, end


def _claims_equal_for_retraction(
    indexed: _IndexedClaim, retraction: MetadataClaim
) -> bool:
    if indexed.claim.name != retraction.name:
        return False
    if indexed.claim.time_domain != retraction.time_domain:
        return False
    if indexed.interval != (retraction.valid_from, retraction.valid_to):
        return False
    if indexed.channels != _normalize_channels(retraction.channels):
        return False
    return indexed.claim.metadata == retraction.metadata


class _PriorityGraph:
    """The business priority relation, restricted to the claims competing on a
    segment whenever it is evaluated."""

    def __init__(self, rules: Iterable[PriorityRule], known_ids: Collection[str]):
        self._higher_to_lowers: Dict[str, List[str]] = {}
        known = set(known_ids)
        for rule in rules:
            if rule.higher not in known or rule.lower not in known:
                raise ValueError(
                    f"priority rule references unknown claim id "
                    f"{rule.higher!r} or {rule.lower!r}"
                )
            self._higher_to_lowers.setdefault(rule.higher, []).append(rule.lower)

    def resolve(
        self, key: str, participants: Sequence[_IndexedClaim]
    ) -> Tuple[Optional[str], Optional[ConflictKind]]:
        """Returns ``(winning value, conflict kind)`` for participants already
        known to carry competing values for ``key``.

        Priority is the transitive closure of the rules restricted to the
        participants: a declaration wins when no other participant can reach it
        through higher-to-lower edges. Mutual reachability means the rules form a
        cycle on this segment."""
        id_set = {item.id for item in participants}
        reachable: Dict[str, set[str]] = {}
        for node in id_set:
            seen: set[str] = set()
            stack = [node]
            while stack:
                current = stack.pop()
                for lower in self._higher_to_lowers.get(current, ()):
                    if lower in id_set and lower not in seen:
                        seen.add(lower)
                        stack.append(lower)
            reachable[node] = seen
        for node, reaches in reachable.items():
            for other in reaches:
                if node in reachable[other]:
                    return None, ConflictKind.CYCLE
        winners = [
            node
            for node in id_set
            if not any(
                node != other and node in reaches
                for other, reaches in reachable.items()
            )
        ]
        winner_values = {
            item.claim.metadata[key]
            for item in participants
            if item.id in winners
        }
        if len(winner_values) == 1:
            return next(iter(winner_values)), None
        return None, ConflictKind.OVERLAP


class MetadataQuery:
    """Evaluates metadata declarations over time ranges.

    :param claims: all business declarations, typically one per Metadata record.
    :param retractions: declarations to withdraw. A retraction carrying a
        ``claim_id`` removes that exact declaration; otherwise it removes the most
        recently recorded still-active declaration whose name, channels, time
        domain, interval and payload match. A same-valued declaration from an
        independent source stays in effect because only the matched projection is
        removed.
    :param priority_rules: business ordering between claim ids. It never falls
        back to physical record order.
    :param channel_universe: concrete channels that channel-agnostic declarations
        (``channels=None``) apply to. A reader supplies its summary channels here.
    """

    def __init__(
        self,
        claims: Iterable[MetadataClaim],
        retractions: Iterable[MetadataClaim] = (),
        priority_rules: Iterable[PriorityRule] = (),
        channel_universe: Optional[Collection[str]] = None,
    ):
        indexed: List[_IndexedClaim] = []
        for sequence, claim in enumerate(claims):
            if claim.claim_id is None:
                raise ValueError("every claim needs a claim_id for arbitration")
            indexed.append(
                _IndexedClaim(
                    claim=claim,
                    sequence=sequence,
                    channels=_normalize_channels(claim.channels),
                    interval=(claim.valid_from, claim.valid_to),
                    id=claim.claim_id,
                )
            )
        for retraction in retractions:
            match = self._find_retraction_target(indexed, retraction)
            if match is not None:
                match.active = False
        self._claims = indexed
        known_ids = [item.id for item in indexed]
        self._graph = _PriorityGraph(priority_rules, known_ids)
        universe = set(channel_universe) if channel_universe is not None else set()
        for item in indexed:
            if item.active and item.channels is not None:
                universe.update(item.channels)
        self._channel_universe = tuple(sorted(universe))

    @staticmethod
    def _find_retraction_target(
        indexed: Sequence[_IndexedClaim], retraction: MetadataClaim
    ) -> Optional[_IndexedClaim]:
        if retraction.claim_id is not None:
            for item in indexed:
                if item.active and item.id == retraction.claim_id:
                    return item
            return None
        for item in reversed(indexed):
            if item.active and _claims_equal_for_retraction(item, retraction):
                return item
        return None

    def run(
        self,
        *,
        topics: Topics = None,
        time_domain: Optional[TimeDomain] = None,
        log_time: Optional[TimeRange] = None,
        publish_time: Optional[TimeRange] = None,
    ) -> MetadataQueryResult:
        """Run the query.

        :param topics: restrict output to these channels. ``None`` uses the
            channel universe.
        :param time_domain: restrict output to one timestamp axis. ``None``
            evaluates both axes independently.
        :param log_time: log-time query bounds. Log-time and publish-time bounds
            are always applied on their own axis, never mixed.
        :param publish_time: publish-time query bounds, applied to publish-time
            declarations only.
        """
        channels = self._select_channels(topics)
        domains = (
            (time_domain,)
            if time_domain is not None
            else tuple(domain for domain in TimeDomain)
        )
        log_bounds = log_time.resolve() if log_time is not None else (None, None)
        publish_bounds = (
            publish_time.resolve() if publish_time is not None else (None, None)
        )
        raw_segments: List[MetadataValueSegment] = []
        used_sequences: set[int] = set()
        for domain in domains:
            bounds = log_bounds if domain == TimeDomain.LOG_TIME else publish_bounds
            active_claims = [
                item
                for item in self._claims
                if item.active and item.claim.time_domain == domain
            ]
            keys = self._collect_keys(active_claims)
            for channel in channels:
                for key in keys:
                    self._build_segments(
                        domain=domain,
                        channel=channel,
                        key=key,
                        claims=active_claims,
                        bounds=bounds,
                        segments=raw_segments,
                        used_sequences=used_sequences,
                    )
        segments, conflicts = self._coalesce_conflicts(raw_segments)
        declarations = tuple(
            item.claim
            for item in self._claims
            if item.sequence in used_sequences and item.active
        )
        return MetadataQueryResult(
            segments=segments,
            conflicts=conflicts,
            declarations=declarations,
        )

    def _select_channels(self, topics: Topics) -> Tuple[str, ...]:
        if topics is None:
            return self._channel_universe
        if isinstance(topics, str):
            return (topics,)
        return tuple(sorted(set(topics)))

    @staticmethod
    def _collect_keys(claims: Sequence[_IndexedClaim]) -> Tuple[str, ...]:
        keys = set()
        for item in claims:
            keys.update(item.claim.metadata.keys())
        return tuple(sorted(keys))

    def _build_segments(
        self,
        *,
        domain: TimeDomain,
        channel: str,
        key: str,
        claims: Sequence[_IndexedClaim],
        bounds: _Interval,
        segments: List[MetadataValueSegment],
        used_sequences: set[int],
    ) -> None:
        clipped: List[Tuple[_IndexedClaim, Tuple[int, Optional[int]]]] = []
        for item in claims:
            if key not in item.claim.metadata:
                continue
            if item.channels is not None and channel not in item.channels:
                continue
            overlap = _intersect(item.interval, bounds)
            if overlap is not None:
                clipped.append((item, overlap))
        if not clipped:
            return
        boundaries = {bound for _, interval in clipped for bound in interval}
        boundaries.discard(None)
        bound_start, _ = bounds
        if bound_start is not None:
            boundaries.add(bound_start)
        ordered = sorted(boundaries)
        for index, start in enumerate(ordered):
            end = ordered[index + 1] if index + 1 < len(ordered) else None
            participants = [
                item
                for item, interval in clipped
                if interval[0] <= start
                and (interval[1] is None or interval[1] > start)
            ]
            if not participants:
                continue
            for item in participants:
                used_sequences.add(item.sequence)
            value, conflict_kind = self._resolve_participants(key, participants)
            segments.append(
                MetadataValueSegment(
                    key=key,
                    channel=channel,
                    time_domain=domain,
                    start_time=start,
                    end_time=end,
                    value=value,
                    declarations=tuple(item.claim for item in participants),
                    conflict=self._make_conflict(
                        domain, channel, key, start, end, conflict_kind, participants
                    ),
                )
            )

    def _resolve_participants(
        self, key: str, participants: Sequence[_IndexedClaim]
    ) -> Tuple[Optional[str], Optional[ConflictKind]]:
        values = {item.claim.metadata[key] for item in participants}
        if len(values) == 1:
            return next(iter(values)), None
        return self._graph.resolve(key, participants)

    @staticmethod
    def _make_conflict(
        domain: TimeDomain,
        channel: str,
        key: str,
        start: int,
        end: Optional[int],
        kind: Optional[ConflictKind],
        participants: Sequence[_IndexedClaim],
    ) -> Optional[MetadataConflict]:
        if kind is None:
            return None
        values: Dict[str, List[str]] = {}
        for item in participants:
            value = item.claim.metadata[key]
            values.setdefault(value, []).append(item.id)
        return MetadataConflict(
            kind=kind,
            key=key,
            channel=channel,
            time_domain=domain,
            start_time=start,
            end_time=end,
            values={value: tuple(claim_ids) for value, claim_ids in values.items()},
            claim_ids=tuple(item.id for item in participants),
        )

    @staticmethod
    def _coalesce_conflicts(
        raw_segments: Sequence[MetadataValueSegment],
    ) -> Tuple[Tuple[MetadataValueSegment, ...], Tuple[MetadataConflict, ...]]:
        """Merge adjacent conflict segments on the same key/channel/domain into one
        conflict interval, and point every affected segment at the merged record."""
        ordered = sorted(
            raw_segments,
            key=lambda segment: (
                segment.time_domain.value,
                segment.channel,
                segment.key,
                segment.start_time,
            ),
        )
        conflicts: List[MetadataConflict] = []
        resolved: List[MetadataValueSegment] = []
        current: Optional[MetadataConflict] = None
        for segment in ordered:
            conflict = segment.conflict
            if conflict is None:
                current = None
                resolved.append(segment)
                continue
            if (
                current is not None
                and current.end_time == conflict.start_time
                and current.kind == conflict.kind
                and current.key == conflict.key
                and current.channel == conflict.channel
                and current.time_domain == conflict.time_domain
            ):
                current = MetadataConflict(
                    kind=current.kind,
                    key=current.key,
                    channel=current.channel,
                    time_domain=current.time_domain,
                    start_time=current.start_time,
                    end_time=conflict.end_time,
                    values={
                        value: tuple(
                            sorted(
                                set(current.values.get(value, ()))
                                | set(conflict.values.get(value, ()))
                            )
                        )
                        for value in set(current.values) | set(conflict.values)
                    },
                    claim_ids=tuple(
                        sorted(set(current.claim_ids) | set(conflict.claim_ids))
                    ),
                )
                conflicts[-1] = current
            else:
                current = conflict
                conflicts.append(conflict)
            resolved.append(
                MetadataValueSegment(
                    key=segment.key,
                    channel=segment.channel,
                    time_domain=segment.time_domain,
                    start_time=segment.start_time,
                    end_time=segment.end_time,
                    value=None,
                    declarations=segment.declarations,
                    conflict=current,
                )
            )
        return tuple(resolved), tuple(conflicts)


def execute_metadata_query(
    records: Iterable[Metadata],
    *,
    parser: ClaimParser,
    retraction_parser: Optional[ClaimParser] = None,
    priority_rules: Iterable[PriorityRule] = (),
    channel_universe: Optional[Collection[str]] = None,
    topics: Topics = None,
    time_domain: Optional[TimeDomain] = None,
    log_time: Optional[TimeRange] = None,
    publish_time: Optional[TimeRange] = None,
) -> MetadataQueryResult:
    """Turn ordered Metadata records into claims and run a :class:`MetadataQuery`.

    :param records: Metadata records in physical file order. The order is only
        used to pick a target for content-based retractions; it never decides
        effective values.
    :param parser: maps each record to a :class:`MetadataClaim`, or skips it by
        returning ``None``.
    :param retraction_parser: maps a record to a retraction claim, or skips it.
        Retractions are classified before the regular parser runs.
    """
    claims: List[MetadataClaim] = []
    retractions: List[MetadataClaim] = []
    for order, record in enumerate(records):
        if retraction_parser is not None:
            retraction = retraction_parser(order, record)
            if retraction is not None:
                retractions.append(retraction)
                continue
        claim = parser(order, record)
        if claim is not None:
            claims.append(claim)
    query = MetadataQuery(
        claims,
        retractions=retractions,
        priority_rules=priority_rules,
        channel_universe=channel_universe,
    )
    return query.run(
        topics=topics,
        time_domain=time_domain,
        log_time=log_time,
        publish_time=publish_time,
    )


__all__ = [
    "ClaimParser",
    "ConflictKind",
    "MetadataClaim",
    "MetadataConflict",
    "MetadataQuery",
    "MetadataQueryResult",
    "MetadataValueSegment",
    "PriorityRule",
    "TimeDomain",
    "TimeRange",
    "execute_metadata_query",
]
