"""Reconcile client-side state after an MPD connection is rebuilt.

An MPD connection and an MPD service generation are different things.  A
client may reconnect to the same running server, reconnect after the server
restarted, or open a second connection to a different partition while the
first one is still winding down.  Treating all of those as a single
``reconnected`` flag leads to a queue from one server incarnation being
mistaken for another server's queue, and to cache entries being thrown away
even when the underlying database was unchanged.

This module models those states separately:

* :class:`ServiceGeneration` is an observable identity of the running MPD
  service.  It is not the same as a connection id.
* :class:`DatabaseState`, :class:`QueueState` and :class:`DirectoryIdentity`
  describe evidence that can be compared independently.
* :func:`classify_caches` assigns every cache class one of
  :attr:`CacheDisposition.REUSE`, :attr:`CacheDisposition.VERIFY` or
  :attr:`CacheDisposition.INVALID`.
* :func:`build_recovery_plan` turns those decisions into concrete actions.
  Missing evidence produces a probe and leaves the item pending; it never
  silently turns into "reuse" or "invalid".
* :func:`execute_recovery_plan` records which actual state (cache, server or
  pending) was adopted for each domain.

The module deliberately contains no socket or MPD command code.  Callers
supply observations and, for probes, a callback that fetches the actual
state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, FrozenSet, Hashable, Mapping, Optional, Tuple

__all__ = [
    "AuthScope",
    "CacheDecision",
    "CacheDisposition",
    "CacheStatus",
    "CacheVerdict",
    "CachedState",
    "Classification",
    "ConnectionStamp",
    "DatabaseState",
    "DirectoryIdentity",
    "ObservedState",
    "PendingRequest",
    "QueueIdentity",
    "QueueState",
    "RecoveryAction",
    "RecoveryPlan",
    "RecoveryResult",
    "RecoveryStep",
    "ReplyContext",
    "ServiceGeneration",
    "SongRef",
    "StateAdoption",
    "StateDomain",
    "StateReconciler",
    "StateSource",
    "build_recovery_plan",
    "classify",
    "classify_caches",
    "execute",
    "execute_recovery_plan",
    "plan_recovery",
    "recover",
    "recovery_plan",
    "reply_belongs_to",
]


class CacheDisposition(Enum):
    """What may be done with a cache entry after the evidence was compared."""

    REUSE = "reuse"
    VERIFY = "verify"
    INVALID = "invalid"

    #: Spellings used by callers that describe the middle state differently.
    REVALIDATE = "verify"
    NEEDS_VERIFICATION = "verify"
    STALE = "invalid"


#: Alias kept for callers that prefer the word "status" or "verdict".
CacheStatus = CacheDisposition
CacheVerdict = CacheDisposition


class StateDomain(Enum):
    """Independent state classes; they never share one global verdict."""

    LIBRARY = "library"
    DIRECTORY = "directory"
    QUEUE = "queue"
    SETTINGS = "settings"
    AUTH = "auth"

    #: Common alternative names.
    DATABASE = "library"
    PLAYLIST = "queue"
    TAGTYPES = "settings"
    AUTHENTICATION = "auth"


class RecoveryAction(Enum):
    """Action selected for one classified state class."""

    REUSE_CACHE = "reuse_cache"
    REFETCH = "refetch"
    ADOPT_SERVER = "adopt_server"
    PROBE = "probe"
    CLOSE_AUTH_SCOPE = "close_auth_scope"


class StateSource(Enum):
    """Provenance recorded after a recovery plan has been executed."""

    CACHE = "cache"
    SERVER = "server"
    NONE = "none"

    #: A missing observation leaves the item pending, not silently reused.
    PENDING = "none"
    UNKNOWN = "none"


def _as_generation(value: Any) -> Optional["ServiceGeneration"]:
    if value is None or isinstance(value, ServiceGeneration):
        return value
    return ServiceGeneration(value)


def _directory_map(value: Any) -> Dict[str, "DirectoryIdentity"]:
    if value is None:
        return {}
    entries: Dict[str, Any]
    if isinstance(value, Mapping):
        entries = dict(value)
    else:
        entries = {entry.uri: entry for entry in value}

    result = {}
    for key, entry in entries.items():
        if isinstance(entry, DirectoryIdentity):
            result[key] = (
                entry
                if entry.uri
                else DirectoryIdentity(
                    key,
                    entry.last_modified,
                    entry.size,
                    entry.fingerprint,
                )
            )
        elif isinstance(entry, Mapping):
            result[key] = DirectoryIdentity(uri=key, **entry)
        else:
            result[key] = DirectoryIdentity(uri=key, fingerprint=str(entry))
    return result


def _as_auth_scope(value: Any) -> Optional["AuthScope"]:
    if value is None or isinstance(value, AuthScope):
        return value
    return AuthScope(value)


def _auth_scope_set(value: Any) -> FrozenSet["AuthScope"]:
    if not value:
        return frozenset()
    return frozenset(_as_auth_scope(scope) for scope in value)


def _as_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_database(value: Any) -> Optional["DatabaseState"]:
    if value is None or isinstance(value, DatabaseState):
        return value
    if isinstance(value, Mapping):
        return DatabaseState.from_stats(value)
    return DatabaseState(db_update=_as_int(value))


def _as_queue(value: Any) -> Optional["QueueState"]:
    if value is None or isinstance(value, QueueState):
        return value
    if isinstance(value, Mapping):
        return QueueState.from_status(value)
    return QueueState(version=_as_int(value))


def _as_tagtypes(value: Any) -> Optional[Tuple[str, ...]]:
    if value is None:
        return None
    if isinstance(value, str):
        return (value,)
    return tuple(value)


@dataclass(frozen=True)
class ServiceGeneration:
    """Identity of the running MPD service, as observed by the caller.

    The token is usually supplied by a supervisor, a boot id, a systemd
    invocation id, or any other value that changes when the service process
    is replaced.  ``observed_at`` is informational and does not participate in
    equality.
    """

    token: Hashable
    observed_at: Optional[float] = field(default=None, compare=False)

    def __post_init__(self) -> None:
        hash(self.token)

    def same_as(self, other: Optional["ServiceGeneration"]) -> Optional[bool]:
        if other is None:
            return None
        return self.token == other.token


@dataclass(frozen=True)
class DatabaseState:
    """Observable database state from MPD's ``stats``/``status`` replies."""

    db_update: Optional[int] = None
    updating_db: Optional[int] = None
    songs: Optional[int] = None

    @classmethod
    def from_stats(
        cls, stats: Mapping[str, Any], updating_db: Any = None
    ) -> "DatabaseState":
        return cls(
            db_update=_as_int(stats.get("db_update")),
            updating_db=_as_int(
                updating_db if updating_db is not None else stats.get("updating_db")
            ),
            songs=_as_int(stats.get("songs")),
        )

    def is_known(self) -> bool:
        return self.db_update is not None

    def is_settled(self) -> bool:
        return self.updating_db is None

    def conflicts_with(self, other: "DatabaseState") -> Optional[bool]:
        """Compare two database states.

        ``True`` means the library changed (or the evidence contradicts
        itself), ``False`` means the material is confirmed unchanged, and
        ``None`` means there is not enough evidence to decide.
        """

        if not self.is_known() or not other.is_known():
            return None
        if not self.is_settled() or not other.is_settled():
            return None
        if self.db_update != other.db_update:
            return True
        if self.songs is not None and other.songs is not None:
            if self.songs != other.songs:
                return True
        return False


@dataclass(frozen=True)
class QueueIdentity:
    """Identity of one queue incarnation.

    ``version`` is MPD's playlist version.  It is only meaningful together
    with the queue incarnation: version 7 of one queue is not version 7 of a
    queue rebuilt by a fresh server process.
    """

    generation: Hashable
    version: int


@dataclass(frozen=True)
class QueueState:
    """Observable queue state from MPD's ``status`` reply."""

    generation: Optional[Hashable] = None
    version: Optional[int] = None
    length: Optional[int] = None

    @classmethod
    def from_status(
        cls, status: Mapping[str, Any], generation: Optional[Hashable] = None
    ) -> "QueueState":
        return cls(
            generation=generation,
            version=_as_int(status.get("playlist")),
            length=_as_int(status.get("playlistlength")),
        )

    def is_known(self) -> bool:
        return self.version is not None

    def with_generation(self, generation: Optional[Hashable]) -> "QueueState":
        return QueueState(generation, self.version, self.length)

    def identity(self) -> Optional[QueueIdentity]:
        if not self.is_known() or self.generation is None:
            return None
        assert self.version is not None
        return QueueIdentity(self.generation, self.version)

    def song(self, songid: int) -> Optional["SongRef"]:
        identity = self.identity()
        if identity is None:
            return None
        return SongRef(identity, songid)

    def same_as(self, other: "QueueState") -> Optional[bool]:
        """Return whether both observations describe the same live queue."""

        if not self.is_known() or not other.is_known():
            return None
        if self.generation != other.generation:
            return False
        if self.version != other.version:
            return False
        if self.length is not None and other.length is not None:
            if self.length != other.length:
                return False
        return True


@dataclass(frozen=True)
class SongRef:
    """A reference to a queue item, bound to its queue incarnation.

    A songid is only unique inside a queue incarnation.  Two references with
    the same numeric songid are the same request only when their queue
    identities also match.
    """

    queue: QueueIdentity
    songid: int

    def continues(self, other: "SongRef") -> bool:
        return self.queue == other.queue and self.songid == other.songid

    def songid_reused_by(self, other: "SongRef") -> bool:
        return self.songid == other.songid and self.queue != other.queue

    def matches(self, other: "SongRef") -> bool:
        return self.continues(other)

    def is_same_request(self, other: "SongRef") -> bool:
        return self.continues(other)

    def is_same_songid(self, other: "SongRef") -> bool:
        return self.songid == other.songid and not self.continues(other)


@dataclass(frozen=True)
class DirectoryIdentity:
    """Confirmed identity of directory material.

    MPD reports ``Last-Modified`` and ``size`` for directory entries.  A
    caller that has a stronger fingerprint (for example a content hash or a
    storage-plugin generation) can provide that instead.  If neither side
    carries enough evidence, the comparison returns ``None`` and the cache is
    kept pending rather than being downloaded again.
    """

    uri: str
    last_modified: Optional[str] = None
    size: Optional[int] = None
    fingerprint: Optional[str] = None

    def is_confirmed(self) -> bool:
        return bool(self.uri) and (
            self.fingerprint is not None
            or self.last_modified is not None
            or self.size is not None
        )

    def compare(self, other: "DirectoryIdentity") -> Optional[bool]:
        if self.uri != other.uri:
            return False
        if not self.is_confirmed() or not other.is_confirmed():
            return None

        if self.fingerprint is not None or other.fingerprint is not None:
            if self.fingerprint is None or other.fingerprint is None:
                return None
            return self.fingerprint == other.fingerprint

        if self.last_modified is not None or other.last_modified is not None:
            if self.last_modified is None or other.last_modified is None:
                return None
            if self.last_modified != other.last_modified:
                return False
            if self.size is not None and other.size is not None:
                return self.size == other.size
            return True

        if self.size is not None and other.size is not None:
            return self.size == other.size
        return None


@dataclass(frozen=True)
class AuthScope:
    """An authentication scope.

    Closing one scope must not close unrelated scopes.  A scope can be an
    opaque token, or an endpoint tuple built with :meth:`from_endpoint`.
    """

    token: Hashable = "default"

    def __post_init__(self) -> None:
        hash(self.token)

    @classmethod
    def from_endpoint(
        cls,
        host: str,
        port: Optional[int] = None,
        *,
        partition: str = "default",
        user: Optional[str] = None,
    ) -> "AuthScope":
        return cls((host, port, partition, user))

    @property
    def name(self) -> str:
        return str(self.token)


@dataclass(frozen=True)
class CachedState:
    """State that was cached before the reconnect/restart."""

    generation: Optional[ServiceGeneration] = None
    database: Optional[DatabaseState] = None
    directories: Mapping[str, DirectoryIdentity] = field(default_factory=dict)
    queue: Optional[QueueState] = None
    tagtypes: Optional[Tuple[str, ...]] = None
    auth_scope: Optional[AuthScope] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "generation", _as_generation(self.generation))
        object.__setattr__(self, "database", _as_database(self.database))
        object.__setattr__(self, "queue", _as_queue(self.queue))
        object.__setattr__(self, "directories", _directory_map(self.directories))
        object.__setattr__(self, "auth_scope", _as_auth_scope(self.auth_scope))
        object.__setattr__(self, "tagtypes", _as_tagtypes(self.tagtypes))

    @classmethod
    def from_status(
        cls,
        status: Mapping[str, Any],
        *,
        generation: Any = None,
        stats: Optional[Mapping[str, Any]] = None,
        directories: Any = None,
        tagtypes: Any = None,
        auth_scope: Any = None,
    ) -> "CachedState":
        """Build state from an MPD ``status`` reply (plus optional extras)."""

        resolved_generation = _as_generation(generation)
        return cls(
            generation=resolved_generation,
            database=DatabaseState.from_stats(
                stats or {}, status.get("updating_db")
            ),
            directories=directories or {},
            queue=QueueState.from_status(
                status,
                resolved_generation.token if resolved_generation is not None else None,
            ),
            tagtypes=tagtypes,
            auth_scope=auth_scope,
        )


@dataclass(frozen=True)
class ObservedState:
    """State observed from the server after the reconnect/restart.

    ``conflicting_auth_scopes`` contains only scopes for which the evidence
    contradicts itself.  Other scopes are left alone.
    """

    generation: Optional[ServiceGeneration] = None
    database: Optional[DatabaseState] = None
    directories: Mapping[str, DirectoryIdentity] = field(default_factory=dict)
    queue: Optional[QueueState] = None
    tagtypes: Optional[Tuple[str, ...]] = None
    auth_scope: Optional[AuthScope] = None
    conflicting_auth_scopes: FrozenSet[AuthScope] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "generation", _as_generation(self.generation))
        object.__setattr__(self, "database", _as_database(self.database))
        object.__setattr__(self, "queue", _as_queue(self.queue))
        object.__setattr__(self, "directories", _directory_map(self.directories))
        object.__setattr__(self, "auth_scope", _as_auth_scope(self.auth_scope))
        object.__setattr__(self, "tagtypes", _as_tagtypes(self.tagtypes))
        object.__setattr__(
            self,
            "conflicting_auth_scopes",
            _auth_scope_set(self.conflicting_auth_scopes),
        )

    @classmethod
    def from_status(
        cls,
        status: Mapping[str, Any],
        *,
        generation: Any = None,
        stats: Optional[Mapping[str, Any]] = None,
        directories: Any = None,
        tagtypes: Any = None,
        auth_scope: Any = None,
        conflicting_auth_scopes: Any = (),
    ) -> "ObservedState":
        """Build state from an MPD ``status`` reply (plus optional extras)."""

        resolved_generation = _as_generation(generation)
        return cls(
            generation=resolved_generation,
            database=DatabaseState.from_stats(
                stats or {}, status.get("updating_db")
            ),
            directories=directories or {},
            queue=QueueState.from_status(
                status,
                resolved_generation.token if resolved_generation is not None else None,
            ),
            tagtypes=tagtypes,
            auth_scope=auth_scope,
            conflicting_auth_scopes=conflicting_auth_scopes,
        )


@dataclass(frozen=True)
class CacheDecision:
    """One independently-classified cache entry."""

    domain: StateDomain
    disposition: CacheDisposition
    reason: str
    key: Optional[str] = None
    auth_scope: Optional[AuthScope] = None


@dataclass(frozen=True)
class Classification:
    """The result of comparing cached and observed state."""

    decisions: Tuple[CacheDecision, ...]
    generation_changed: Optional[bool]

    def for_domain(self, domain: StateDomain) -> Tuple[CacheDecision, ...]:
        return tuple(d for d in self.decisions if d.domain is domain)

    def decision(
        self, domain: StateDomain, key: Optional[str] = None
    ) -> Optional[CacheDecision]:
        for decision in self.decisions:
            if decision.domain is domain and decision.key == key:
                return decision
        return None

    def disposition(
        self, domain: StateDomain, key: Optional[str] = None
    ) -> CacheDisposition:
        decision = self.decision(domain, key)
        if decision is None:
            raise KeyError((domain, key))
        return decision.disposition

    def __getitem__(self, domain: StateDomain) -> Tuple[CacheDecision, ...]:
        return self.for_domain(domain)

    def summary(self) -> Dict[StateDomain, CacheDisposition]:
        """One conservative verdict per domain.

        Directory entries are aggregated by severity: any ``INVALID`` entry
        makes the directory summary ``INVALID``; otherwise any pending entry
        makes it ``VERIFY``.
        """

        rank = {
            CacheDisposition.REUSE: 0,
            CacheDisposition.VERIFY: 1,
            CacheDisposition.INVALID: 2,
        }
        result: Dict[StateDomain, CacheDisposition] = {}
        for decision in self.decisions:
            previous = result.get(decision.domain)
            if previous is None or rank[decision.disposition] > rank[previous]:
                result[decision.domain] = decision.disposition
        return result


def _generation_changed(
    cached: Optional[ServiceGeneration], observed: Optional[ServiceGeneration]
) -> Optional[bool]:
    if cached is None or observed is None:
        return None
    return cached.token != observed.token


def _library_decision(
    cached: Optional[DatabaseState], observed: Optional[DatabaseState]
) -> CacheDecision:
    if cached is None or observed is None:
        return CacheDecision(
            StateDomain.LIBRARY,
            CacheDisposition.VERIFY,
            "database state is missing on one side",
        )
    if not cached.is_known() or not observed.is_known():
        return CacheDecision(
            StateDomain.LIBRARY,
            CacheDisposition.VERIFY,
            "db_update is missing; library identity is unconfirmed",
        )
    if not cached.is_settled() or not observed.is_settled():
        return CacheDecision(
            StateDomain.LIBRARY,
            CacheDisposition.VERIFY,
            "a database update is in progress",
        )
    conflict = observed.conflicts_with(cached)
    if conflict is None:
        return CacheDecision(
            StateDomain.LIBRARY,
            CacheDisposition.VERIFY,
            "database evidence is insufficient",
        )
    if conflict:
        return CacheDecision(
            StateDomain.LIBRARY,
            CacheDisposition.INVALID,
            "db_update/song count changed",
        )
    return CacheDecision(
        StateDomain.LIBRARY,
        CacheDisposition.REUSE,
        "database identity matches; material is unchanged",
    )


def _directory_decision(
    key: str,
    cached: Optional[DirectoryIdentity],
    observed: Optional[DirectoryIdentity],
) -> CacheDecision:
    if cached is None:
        return CacheDecision(
            StateDomain.DIRECTORY,
            CacheDisposition.VERIFY,
            "no cached directory identity",
            key=key,
        )
    if observed is None:
        return CacheDecision(
            StateDomain.DIRECTORY,
            CacheDisposition.VERIFY,
            "directory identity was not observed",
            key=key,
        )
    if cached.uri != observed.uri:
        return CacheDecision(
            StateDomain.DIRECTORY,
            CacheDisposition.INVALID,
            "directory URI no longer matches the cached entry",
            key=key,
        )
    comparison = cached.compare(observed)
    if comparison is None:
        return CacheDecision(
            StateDomain.DIRECTORY,
            CacheDisposition.VERIFY,
            "directory identity is incomplete",
            key=key,
        )
    if comparison:
        return CacheDecision(
            StateDomain.DIRECTORY,
            CacheDisposition.REUSE,
            "confirmed directory identity matches",
            key=key,
        )
    return CacheDecision(
        StateDomain.DIRECTORY,
        CacheDisposition.INVALID,
        "confirmed directory identity changed",
        key=key,
    )


def _queue_decision(
    cached: Optional[QueueState],
    observed: Optional[QueueState],
    generation_changed: Optional[bool],
) -> CacheDecision:
    if cached is None or observed is None:
        return CacheDecision(
            StateDomain.QUEUE,
            CacheDisposition.VERIFY,
            "queue state is missing on one side",
        )
    if not cached.is_known() or not observed.is_known():
        return CacheDecision(
            StateDomain.QUEUE,
            CacheDisposition.VERIFY,
            "queue version is unconfirmed",
        )
    if generation_changed is True:
        return CacheDecision(
            StateDomain.QUEUE,
            CacheDisposition.INVALID,
            "service generation changed; the queue was rebuilt",
        )

    both_have_generation = (
        cached.generation is not None and observed.generation is not None
    )
    if both_have_generation and cached.generation != observed.generation:
        return CacheDecision(
            StateDomain.QUEUE,
            CacheDisposition.INVALID,
            "queue incarnation changed",
        )
    if generation_changed is None and not both_have_generation:
        return CacheDecision(
            StateDomain.QUEUE,
            CacheDisposition.VERIFY,
            "service generation is missing",
        )

    comparison = cached.same_as(observed)
    if comparison is None:
        return CacheDecision(
            StateDomain.QUEUE,
            CacheDisposition.VERIFY,
            "queue identity is incomplete",
        )
    if comparison:
        return CacheDecision(
            StateDomain.QUEUE,
            CacheDisposition.REUSE,
            "queue identity matches",
        )
    return CacheDecision(
        StateDomain.QUEUE,
        CacheDisposition.INVALID,
        "queue version/length changed",
    )


def _settings_decision(
    cached: Optional[Tuple[str, ...]],
    observed: Optional[Tuple[str, ...]],
) -> CacheDecision:
    if cached is None or observed is None:
        return CacheDecision(
            StateDomain.SETTINGS,
            CacheDisposition.VERIFY,
            "tagtypes evidence is missing on one side",
        )
    if tuple(cached) != tuple(observed):
        return CacheDecision(
            StateDomain.SETTINGS,
            CacheDisposition.INVALID,
            "tagtypes changed",
        )
    return CacheDecision(
        StateDomain.SETTINGS,
        CacheDisposition.REUSE,
        "tagtypes match the observed settings",
    )


def _auth_decision(
    cached: Optional[AuthScope],
    observed: Optional[AuthScope],
    conflicts: FrozenSet[AuthScope],
) -> CacheDecision:
    if (cached is not None and cached in conflicts) or (
        observed is not None and observed in conflicts
    ):
        return CacheDecision(
            StateDomain.AUTH,
            CacheDisposition.INVALID,
            "conflicting authentication evidence for this scope",
            auth_scope=cached if cached is not None else observed,
        )
    if cached is None or observed is None:
        return CacheDecision(
            StateDomain.AUTH,
            CacheDisposition.VERIFY,
            "authentication scope is missing on one side",
            auth_scope=cached if cached is not None else observed,
        )
    if cached != observed:
        return CacheDecision(
            StateDomain.AUTH,
            CacheDisposition.INVALID,
            "authentication identity no longer matches",
            auth_scope=cached,
        )
    return CacheDecision(
        StateDomain.AUTH,
        CacheDisposition.REUSE,
        "authentication scope matches",
        auth_scope=cached,
    )


def classify_caches(cached: CachedState, observed: ObservedState) -> Classification:
    """Classify every cached state class independently."""

    generation_changed = _generation_changed(cached.generation, observed.generation)

    # A QueueState may omit its own generation when the enclosing state
    # already carries it.  Inheriting it preserves the queue incarnation while
    # still comparing cached and observed values separately.
    cached_queue = cached.queue
    if (
        cached_queue is not None
        and cached_queue.generation is None
        and cached.generation is not None
    ):
        cached_queue = cached_queue.with_generation(cached.generation.token)

    observed_queue = observed.queue
    if (
        observed_queue is not None
        and observed_queue.generation is None
        and observed.generation is not None
    ):
        observed_queue = observed_queue.with_generation(observed.generation.token)

    decisions = [
        _library_decision(cached.database, observed.database),
    ]

    for key in sorted(set(cached.directories) | set(observed.directories)):
        decisions.append(
            _directory_decision(
                key,
                cached.directories.get(key),
                observed.directories.get(key),
            )
        )

    decisions.extend(
        [
            _queue_decision(cached_queue, observed_queue, generation_changed),
            _settings_decision(cached.tagtypes, observed.tagtypes),
            _auth_decision(
                cached.auth_scope,
                observed.auth_scope,
                observed.conflicting_auth_scopes,
            ),
        ]
    )
    return Classification(tuple(decisions), generation_changed)


#: Short alias for :func:`classify_caches`.
classify = classify_caches


@dataclass(frozen=True)
class RecoveryStep:
    """One action in a recovery plan."""

    domain: StateDomain
    action: RecoveryAction
    disposition: CacheDisposition
    key: Optional[str] = None
    auth_scope: Optional[AuthScope] = None
    reason: str = ""


@dataclass(frozen=True)
class RecoveryPlan:
    """A per-domain recovery plan with its own closed-auth-scope set."""

    steps: Tuple[RecoveryStep, ...]
    closed_auth_scopes: FrozenSet[AuthScope]
    generation_changed: Optional[bool]

    def for_domain(self, domain: StateDomain) -> Tuple[RecoveryStep, ...]:
        return tuple(step for step in self.steps if step.domain is domain)

    def step(
        self, domain: StateDomain, key: Optional[str] = None
    ) -> Optional[RecoveryStep]:
        for step in self.steps:
            if step.domain is domain and step.key == key:
                return step
        return None

    def __getitem__(self, domain: StateDomain) -> Tuple[RecoveryStep, ...]:
        return self.for_domain(domain)


def _action_for(decision: CacheDecision, observed: ObservedState) -> RecoveryAction:
    if decision.disposition is CacheDisposition.REUSE:
        return RecoveryAction.REUSE_CACHE

    if decision.domain is StateDomain.QUEUE:
        if (
            decision.disposition is CacheDisposition.INVALID
            and observed.queue is not None
        ):
            return RecoveryAction.ADOPT_SERVER
        return RecoveryAction.PROBE

    if decision.domain is StateDomain.SETTINGS:
        if (
            decision.disposition is CacheDisposition.INVALID
            and observed.tagtypes is not None
        ):
            return RecoveryAction.ADOPT_SERVER
        return RecoveryAction.PROBE

    if decision.domain is StateDomain.AUTH:
        if decision.disposition is CacheDisposition.INVALID:
            return RecoveryAction.CLOSE_AUTH_SCOPE
        return RecoveryAction.PROBE

    if decision.disposition is CacheDisposition.INVALID:
        if decision.domain is StateDomain.AUTH:
            return RecoveryAction.CLOSE_AUTH_SCOPE
        return RecoveryAction.REFETCH
    return RecoveryAction.PROBE


def build_recovery_plan(
    cached: CachedState,
    observed: ObservedState,
    classification: Optional[Classification] = None,
) -> RecoveryPlan:
    """Build a recovery plan from per-domain cache verdicts."""

    if classification is None:
        classification = classify_caches(cached, observed)

    steps = []
    for decision in classification.decisions:
        steps.append(
            RecoveryStep(
                domain=decision.domain,
                action=_action_for(decision, observed),
                disposition=decision.disposition,
                key=decision.key,
                auth_scope=decision.auth_scope,
                reason=decision.reason,
            )
        )

    for scope in sorted(
        observed.conflicting_auth_scopes,
        key=lambda scope: repr(scope.token),
    ):
        if any(
            step.domain is StateDomain.AUTH and step.auth_scope == scope
            for step in steps
        ):
            continue
        steps.append(
            RecoveryStep(
                domain=StateDomain.AUTH,
                action=RecoveryAction.CLOSE_AUTH_SCOPE,
                disposition=CacheDisposition.INVALID,
                auth_scope=scope,
                reason="conflicting authentication evidence for this scope",
            )
        )

    # A conflicting scope is closed even when no cached entry happened to
    # name it.  This is deliberately not a client-wide disconnect: only the
    # scope whose evidence contradicted itself is closed.
    closed = frozenset(observed.conflicting_auth_scopes)
    return RecoveryPlan(
        tuple(steps),
        closed,
        classification.generation_changed,
    )


#: Short alias for :func:`build_recovery_plan`.
plan_recovery = build_recovery_plan


@dataclass(frozen=True)
class StateAdoption:
    """Which actual state was adopted for one domain after recovery."""

    domain: StateDomain
    key: Optional[str]
    source: StateSource
    value: Any
    generation: Optional[ServiceGeneration]
    queue_version: Optional[int] = None
    auth_scope: Optional[AuthScope] = None
    note: str = ""


@dataclass(frozen=True)
class RecoveryResult:
    """Executed recovery plan and its provenance ledger."""

    plan: RecoveryPlan
    adoptions: Tuple[StateAdoption, ...]
    closed_auth_scopes: FrozenSet[AuthScope]

    def adoption_for(
        self, domain: StateDomain, key: Optional[str] = None
    ) -> Optional[StateAdoption]:
        for adoption in self.adoptions:
            if adoption.domain is domain and adoption.key == key:
                return adoption
        return None

    def source_for(self, domain: StateDomain, key: Optional[str] = None) -> StateSource:
        adoption = self.adoption_for(domain, key)
        if adoption is None:
            return StateSource.NONE
        return adoption.source

    def adopted_state(self, domain: StateDomain, key: Optional[str] = None) -> Any:
        adoption = self.adoption_for(domain, key)
        if adoption is None:
            return None
        return adoption.value


def _cached_value(cached: CachedState, step: RecoveryStep) -> Any:
    if step.domain is StateDomain.LIBRARY:
        return cached.database
    if step.domain is StateDomain.DIRECTORY:
        return cached.directories.get(step.key) if step.key is not None else None
    if step.domain is StateDomain.QUEUE:
        return cached.queue
    if step.domain is StateDomain.SETTINGS:
        return cached.tagtypes
    if step.domain is StateDomain.AUTH:
        return cached.auth_scope
    return None


def _observed_value(observed: ObservedState, step: RecoveryStep) -> Any:
    if step.domain is StateDomain.DIRECTORY:
        return observed.directories.get(step.key) if step.key is not None else None
    if step.domain is StateDomain.QUEUE:
        return observed.queue
    if step.domain is StateDomain.SETTINGS:
        return observed.tagtypes
    if step.domain is StateDomain.AUTH:
        return observed.auth_scope
    return None


def _adoption(
    step: RecoveryStep,
    source: StateSource,
    value: Any,
    cached: CachedState,
    observed: ObservedState,
    note: str = "",
) -> StateAdoption:
    generation = None
    if source is StateSource.CACHE:
        generation = cached.generation
    elif source is StateSource.SERVER:
        generation = observed.generation

    queue_version = value.version if isinstance(value, QueueState) else None
    return StateAdoption(
        domain=step.domain,
        key=step.key,
        source=source,
        value=value,
        generation=generation,
        queue_version=queue_version,
        auth_scope=step.auth_scope,
        note=note or step.reason,
    )


def execute_recovery_plan(
    plan: RecoveryPlan,
    cached: CachedState,
    observed: ObservedState,
    probe: Optional[Callable[[RecoveryStep], Any]] = None,
) -> RecoveryResult:
    """Execute *plan* and record the actual state adopted by each domain.

    ``probe`` is called for :attr:`RecoveryAction.PROBE` and
    :attr:`RecoveryAction.REFETCH` steps with the :class:`RecoveryStep`.  If
    it is absent, or returns ``None``, the item remains pending
    (``StateSource.NONE``); absence of evidence never becomes an implicit
    cache hit.
    """

    adoptions = []
    for step in plan.steps:
        if step.action is RecoveryAction.REUSE_CACHE:
            value = _cached_value(cached, step)
            source = StateSource.CACHE if value is not None else StateSource.NONE
            adoptions.append(
                _adoption(
                    step,
                    source,
                    value,
                    cached,
                    observed,
                    "" if value is not None else "cached value is missing",
                )
            )
        elif step.action is RecoveryAction.ADOPT_SERVER:
            value = _observed_value(observed, step)
            source = StateSource.SERVER if value is not None else StateSource.NONE
            adoptions.append(
                _adoption(
                    step,
                    source,
                    value,
                    cached,
                    observed,
                    "" if value is not None else "server value is missing",
                )
            )
        elif step.action in (RecoveryAction.PROBE, RecoveryAction.REFETCH):
            value = None
            note = "evidence missing; kept pending"
            if probe is not None:
                try:
                    value = probe(step)
                except Exception as exc:  # pragma: no cover - defensive
                    note = "probe failed: {}".format(exc)
                else:
                    if value is not None:
                        note = "confirmed by probe"
            source = StateSource.SERVER if value is not None else StateSource.NONE
            adoptions.append(
                _adoption(step, source, value, cached, observed, note)
            )
        elif step.action is RecoveryAction.CLOSE_AUTH_SCOPE:
            scope = step.auth_scope
            adoptions.append(
                StateAdoption(
                    domain=step.domain,
                    key=step.key,
                    source=StateSource.NONE,
                    value=None,
                    generation=None,
                    auth_scope=scope,
                    note="closed affected authentication scope",
                )
            )

    return RecoveryResult(plan, tuple(adoptions), plan.closed_auth_scopes)


#: Short alias for :func:`execute_recovery_plan`.
execute = execute_recovery_plan


def recover(
    cached: CachedState,
    observed: ObservedState,
    probe: Optional[Callable[..., Any]] = None,
) -> RecoveryResult:
    """Classify, plan and execute recovery in one call."""

    plan = build_recovery_plan(cached, observed)
    return execute_recovery_plan(plan, cached, observed, probe)


#: Alias for callers that use the noun phrase.
recovery_plan = build_recovery_plan


class StateReconciler:
    """Convenience facade for classify -> plan -> execute."""

    def __init__(self, cached: CachedState, observed: ObservedState) -> None:
        self.cached = cached
        self.observed = observed

    def classify(self) -> Classification:
        return classify_caches(self.cached, self.observed)

    def plan(self, classification: Optional[Classification] = None) -> RecoveryPlan:
        return build_recovery_plan(self.cached, self.observed, classification)

    def execute(
        self,
        plan: Optional[RecoveryPlan] = None,
        probe: Optional[Callable[[RecoveryStep], Any]] = None,
    ) -> RecoveryResult:
        if plan is None:
            plan = self.plan()
        return execute_recovery_plan(plan, self.cached, self.observed, probe)


@dataclass(frozen=True)
class ReplyContext:
    """The connection incarnation a request/reply belongs to.

    ``connection_epoch`` changes whenever a connection is established or
    torn down.  ``generation`` is informational server identity; replies are
    never matched on generation alone.
    """

    connection_epoch: int
    generation: Optional[ServiceGeneration] = None

    def accepts(self, current: "ReplyContext") -> bool:
        return self.connection_epoch == current.connection_epoch

    def belongs_to(self, current: "ReplyContext") -> bool:
        return self.accepts(current)


#: Alias used when the context is attached to a pending request.
ConnectionStamp = ReplyContext
PendingRequest = ReplyContext


def reply_belongs_to(reply: ReplyContext, current: ReplyContext) -> bool:
    """Return whether a late reply still belongs to the current connection."""

    return reply.connection_epoch == current.connection_epoch
