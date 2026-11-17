"""Queryable migration graph built from LNK TrackerDataBlock identifiers.

Every tracker block carries four identifiers with distinct responsibilities:

* birth volume GUID   -- volume where the target was first seen
* birth object GUID   -- object identity assigned at birth
* current volume GUID -- volume where the target resided when the shortcut
  was written
* current object GUID -- object identity at that moment

The graph keeps those four responsibilities in separate per-role indexes
instead of merging them into a single deduplicated set, so a GUID value
seen as a birth volume never collides with the same value seen as a
current object.

Correlation follows the tracking guarantee supplied by the input itself:
within one tracker block the birth pair and the current pair refer to the
same target, so each observation contributes one directed edge
``birth -> current``.  Edges that share a complete identity chain into
migration paths across shortcuts.  The machine name is stored as evidence
only; it is never used to synthesize a missing object identifier.

Migration chains may fork or contradict an already verified target.  All
such connections are kept: edges that can hold simultaneously stay
candidates, later material may narrow them (confirming one branch rules
the siblings out), and contradictions with verified targets are recorded
as conflict evidence.  Narrowing never rewrites or deletes the original
divergence, and no "latest truth" is derived from arrival order.
"""

from dataclasses import dataclass
from dataclasses import field
from enum import Enum

from LnkParse3.extra.distributed_tracker import DistributedTracker


class GuidRole(Enum):
    """The four distinct responsibilities carried by a tracker block."""

    BIRTH_VOLUME = "birth_volume"
    BIRTH_OBJECT = "birth_object"
    CURRENT_VOLUME = "current_volume"
    CURRENT_OBJECT = "current_object"


class EdgeStatus(Enum):
    """Lifecycle of a migration claim.

    Edges move CANDIDATE -> CONFIRMED/RULED_OUT when later material
    narrows a fork.  A ruled-out edge is kept as evidence of the original
    divergence and is never rewritten back or deleted.
    """

    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    RULED_OUT = "ruled_out"


@dataclass(frozen=True)
class TargetIdentity:
    """A (volume, object) GUID pair locating a target.

    The identity is complete only when both GUIDs are present.  A missing
    object GUID is left missing -- it is never reconstructed from the
    machine name or from any other observation.
    """

    volume_guid: str | None
    object_guid: str | None

    @property
    def is_complete(self):
        return self.volume_guid is not None and self.object_guid is not None


@dataclass(frozen=True)
class TrackingObservation:
    """The four tracker GUIDs of one shortcut, kept role by role."""

    source: str
    machine_id: str | None = None
    birth_volume_guid: str | None = None
    birth_object_guid: str | None = None
    current_volume_guid: str | None = None
    current_object_guid: str | None = None

    @property
    def birth(self):
        return TargetIdentity(self.birth_volume_guid, self.birth_object_guid)

    @property
    def current(self):
        return TargetIdentity(self.current_volume_guid, self.current_object_guid)

    def guids_by_role(self):
        return {
            GuidRole.BIRTH_VOLUME: self.birth_volume_guid,
            GuidRole.BIRTH_OBJECT: self.birth_object_guid,
            GuidRole.CURRENT_VOLUME: self.current_volume_guid,
            GuidRole.CURRENT_OBJECT: self.current_object_guid,
        }

    @classmethod
    def from_tracker(cls, tracker, source):
        """Build an observation from a DistributedTracker block or its dict."""
        if isinstance(tracker, DistributedTracker):
            return cls(
                source=source,
                machine_id=tracker.machine_id(),
                birth_volume_guid=tracker.droid_birth_volume_id(),
                birth_object_guid=tracker.droid_birth_file_id(),
                current_volume_guid=tracker.droid_volume_id(),
                current_object_guid=tracker.droid_file_id(),
            )
        return cls(
            source=source,
            machine_id=tracker.get("machine_identifier"),
            birth_volume_guid=tracker.get("birth_droid_volume_identifier"),
            birth_object_guid=tracker.get("birth_droid_file_identifier"),
            current_volume_guid=tracker.get("droid_volume_identifier"),
            current_object_guid=tracker.get("droid_file_identifier"),
        )


@dataclass(frozen=True)
class Conflict:
    """Evidence that a claimed migration contradicts a verified target."""

    birth: TargetIdentity
    claimed_current: TargetIdentity
    verified_current: TargetIdentity
    sources: tuple[str, ...]
    detail: str


class MigrationEdge:
    """A directed ``birth -> current`` claim shared by one or more shortcuts."""

    def __init__(self, birth, current):
        self.birth = birth
        self.current = current
        self.sources = []
        self.status = EdgeStatus.CANDIDATE
        self.conflicts = []

    def __repr__(self):
        return "MigrationEdge(birth=%r, current=%r, status=%r)" % (
            self.birth,
            self.current,
            self.status.value,
        )


@dataclass
class ShortcutView:
    """Query result for one shortcut.

    Keeps the original identity, the location claimed by the shortcut
    itself, and the migrations that are still unconfirmed clearly apart.
    """

    source: str
    machine_id: str | None
    original_identity: TargetIdentity
    current_location: TargetIdentity
    confirmed_migrations: list[MigrationEdge] = field(default_factory=list)
    unconfirmed_migrations: list[MigrationEdge] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)


class MigrationGraph:
    """Append-only graph of target migrations observed in shortcuts."""

    def __init__(self):
        self._observations = {}
        # One deduplicated index per role -- the four GUID kinds are never
        # merged into a single set.
        self._guid_index = {role: {} for role in GuidRole}
        self._nodes = {}
        self._edges = {}
        self._outgoing = {}
        self._verified = {}
        self._conflicts = []

    @staticmethod
    def _identity_key(identity, source, kind):
        # Only complete identities may act as shared join nodes.  A partial
        # identity gets a key private to its observation so two shortcuts
        # that are both missing an object GUID never merge -- a matching
        # machine name does not change that.
        if identity.is_complete:
            return ("target", identity.volume_guid, identity.object_guid)
        return ("incomplete", source, kind)

    def add(self, observation):
        """Record one shortcut's tracker block as migration evidence."""
        source = observation.source
        if source in self._observations:
            raise ValueError("duplicate observation source: %r" % source)
        self._observations[source] = observation

        for role, guid in observation.guids_by_role().items():
            if guid is not None:
                self._guid_index[role].setdefault(guid, set()).add(source)

        birth_key = self._identity_key(observation.birth, source, "birth")
        current_key = self._identity_key(observation.current, source, "current")
        self._nodes.setdefault(birth_key, observation.birth)
        self._nodes.setdefault(current_key, observation.current)

        edge_key = (birth_key, current_key)
        edge = self._edges.get(edge_key)
        if edge is None:
            edge = MigrationEdge(observation.birth, observation.current)
            self._edges[edge_key] = edge
            self._outgoing.setdefault(birth_key, []).append(edge)
        edge.sources.append(source)

        verified_current = self._verified.get(birth_key)
        if verified_current is not None:
            self._reconcile_with_verified(edge, verified_current)
        return observation

    def add_tracker(self, tracker, source):
        """Record an observation built from a tracker block or its dict."""
        return self.add(TrackingObservation.from_tracker(tracker, source))

    def add_lnk(self, lnk, source):
        """Record the tracker block of a parsed LnkFile."""
        for extra in lnk.extras:
            if isinstance(extra, DistributedTracker):
                return self.add_tracker(extra, source)
        raise ValueError("no DistributedTracker block in %r" % source)

    def verify_target(
        self,
        birth_volume_guid,
        birth_object_guid,
        current_volume_guid,
        current_object_guid,
    ):
        """Register a reconciled ``birth -> current`` mapping as verified.

        Later material may narrow candidates this way: the matching edge
        is confirmed, sibling candidates are ruled out (but kept), and
        contradicting edges gain conflict evidence.  A different location
        already verified for the same birth is never overwritten.
        """
        birth = TargetIdentity(birth_volume_guid, birth_object_guid)
        current = TargetIdentity(current_volume_guid, current_object_guid)
        if not birth.is_complete or not current.is_complete:
            raise ValueError("verified targets require complete identities")
        birth_key = self._identity_key(birth, "<verified>", "birth")

        existing = self._verified.get(birth_key)
        if existing is not None and existing != current:
            raise ValueError(
                "birth %r already verified at %r; refusing to overwrite with %r"
                % (birth, existing, current)
            )
        self._verified[birth_key] = current

        for edge in self._outgoing.get(birth_key, []):
            self._reconcile_with_verified(edge, current)

    def _reconcile_with_verified(self, edge, verified_current):
        if edge.current == verified_current:
            edge.status = EdgeStatus.CONFIRMED
            return
        if edge.status is EdgeStatus.CANDIDATE:
            edge.status = EdgeStatus.RULED_OUT
        already_recorded = any(
            conflict.claimed_current == edge.current
            and conflict.verified_current == verified_current
            for conflict in edge.conflicts
        )
        if already_recorded:
            return
        conflict = Conflict(
            birth=edge.birth,
            claimed_current=edge.current,
            verified_current=verified_current,
            sources=tuple(edge.sources),
            detail="claimed migration contradicts the verified target location",
        )
        edge.conflicts.append(conflict)
        self._conflicts.append(conflict)

    def sources_for_guid(self, role, guid):
        """Shortcuts that carried ``guid`` under exactly this responsibility."""
        return set(self._guid_index[role].get(guid, set()))

    @property
    def conflicts(self):
        """All conflict evidence collected so far."""
        return list(self._conflicts)

    def edges_from(self, identity):
        """All migration claims leaving a complete identity, any status."""
        key = self._identity_key(identity, "<query>", "birth")
        return list(self._outgoing.get(key, []))

    def query(self, source):
        """Describe one shortcut without picking any arrival-order winner."""
        try:
            observation = self._observations[source]
        except KeyError:
            raise KeyError("unknown observation source: %r" % source) from None

        view = ShortcutView(
            source=source,
            machine_id=observation.machine_id,
            original_identity=observation.birth,
            current_location=observation.current,
        )

        start_key = self._identity_key(observation.birth, source, "birth")
        seen_edges = set()
        seen_nodes = set()
        stack = [start_key]
        while stack:
            node_key = stack.pop()
            if node_key in seen_nodes:
                continue
            seen_nodes.add(node_key)
            for edge in self._outgoing.get(node_key, []):
                edge_key = id(edge)
                if edge_key in seen_edges:
                    continue
                seen_edges.add(edge_key)
                if edge.status is EdgeStatus.CONFIRMED:
                    view.confirmed_migrations.append(edge)
                else:
                    view.unconfirmed_migrations.append(edge)
                for conflict in edge.conflicts:
                    if conflict not in view.conflicts:
                        view.conflicts.append(conflict)
                current_key = self._identity_key(edge.current, "<query>", "current")
                if current_key not in seen_nodes:
                    stack.append(current_key)
        return view
