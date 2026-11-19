"""Plan minimum-cost DICOM retrieval routes using C-GET and/or C-MOVE.

An acquirer that has negotiated a Query/Retrieve association with a source
station may pull instances with C-GET (the source pushes storage
sub-operations back over the *existing* association) or ask the source to
C-MOVE them to a destination station (the source opens a *new* association
to the destination and acts as a Storage SCU there).

This module decides, *before anything is executed*, whether a complete
route exists for every pending SOP Class and picks the lowest-cost one:

* **Query conditions** and **reception conditions** are reported separately
  for every planned leg, because they are independent: a SOP Class that can
  be queried cannot necessarily be received.
* The roles are **not** interchangeable: C-GET reception requires accepted
  Storage presentation contexts on the *existing* association (receiver is
  the SCP), while C-MOVE reception requires the *destination* to accept the
  Storage SOP Classes on a *new* association.
* If no complete route exists for a SOP Class, every candidate route is
  listed together with the conditions it failed (the blocking
  combinations).
* Instances that have not been executed yet stay
  :attr:`QueryOutcome.NOT_QUERIED`; an unexecuted instance must never be
  recorded as "query returned no results".
* When a station's capabilities are updated only the routes that depend on
  that station are recomputed; instances already verified at the receiver
  are never fetched again.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from pynetdicom.presentation import PresentationContext
from pynetdicom.service_class import QueryRetrieveServiceClass, StorageServiceClass
from pynetdicom.sop_class import _QR_CLASSES, uid_to_service_class


_QR_GET_MODELS = frozenset(
    uid for name, uid in _QR_CLASSES.items() if name.endswith("Get")
)
_QR_MOVE_MODELS = frozenset(
    uid for name, uid in _QR_CLASSES.items() if name.endswith("Move")
)


class RetrieveMethod(Enum):
    """The DIMSE retrieval service used by a route."""

    C_GET = "C-GET"
    C_MOVE = "C-MOVE"


class MaterialState(Enum):
    """The retrieval state of a single SOP Instance."""

    PENDING = "pending"
    """Known to be needed; retrieval not yet executed."""
    VERIFIED = "verified"
    """Received and verified; must not be fetched again."""


class QueryOutcome(Enum):
    """The outcome of an *executed* query for a SOP Instance."""

    NOT_QUERIED = "not_queried"
    """No query has been executed for the instance yet."""
    MATCHED = "matched"
    """An executed query matched the instance at the source."""
    NO_MATCH = "no_match"
    """An executed query returned no match for the instance."""


@dataclass
class Material:
    """A single SOP Instance tracked by the planner."""

    sop_instance_uid: str
    sop_class_uid: str
    state: MaterialState = MaterialState.PENDING
    query_outcome: QueryOutcome = QueryOutcome.NOT_QUERIED


@dataclass(frozen=True)
class StationCapabilities:
    """The capabilities of a station relevant to retrieval planning.

    Parameters
    ----------
    ae_title : str
        The AE title of the station.
    query_models : frozenset of str
        Query/Retrieve SOP Class UIDs (FIND/GET/MOVE information models)
        that may be used to query *this* station on the negotiated
        association, with the acquirer as SCU.
    storage_classes : frozenset of str
        Storage SOP Class UIDs whose instances may be *received* on the
        relevant association, with this station as the Storage SCP. For the
        association peer these are the Storage presentation contexts
        accepted on the existing association (peer as SCU); for a C-MOVE
        destination they are the Storage SOP Classes it accepts on a new
        association.
    """

    ae_title: str
    query_models: frozenset = frozenset()
    storage_classes: frozenset = frozenset()


def capabilities_from_contexts(
    ae_title: str, contexts: list[PresentationContext]
) -> StationCapabilities:
    """Build capabilities for the peer of a negotiated association.

    Parameters
    ----------
    ae_title : str
        The AE title of the association peer (the acceptor).
    contexts : list of presentation.PresentationContext
        The accepted presentation contexts from the association. The
        negotiated roles are taken from the acceptor's perspective, as
        reported by :attr:`~pynetdicom.presentation.PresentationContext.as_scu`
        and :attr:`~pynetdicom.presentation.PresentationContext.as_scp`.

    Returns
    -------
    StationCapabilities
        ``query_models`` holds the Query/Retrieve contexts the peer
        accepted as SCP (so the acquirer can query as SCU);
        ``storage_classes`` holds the Storage contexts the peer accepted as
        SCU (so it can push instances back over the same association during
        C-GET).
    """
    query_models = set()
    storage_classes = set()
    for cx in contexts:
        uid = cx.abstract_syntax
        if uid is None:
            continue
        service = uid_to_service_class(uid)
        if issubclass(service, QueryRetrieveServiceClass) and cx.as_scp:
            query_models.add(str(uid))
        elif issubclass(service, StorageServiceClass) and cx.as_scu:
            storage_classes.add(str(uid))

    return StationCapabilities(
        ae_title, frozenset(query_models), frozenset(storage_classes)
    )


@dataclass(frozen=True)
class RouteOption:
    """An allowed retrieval route and its cost.

    Parameters
    ----------
    source : str
        AE title of the station that is queried (the association peer).
    method : RetrieveMethod
        ``C_GET`` to receive on the existing association, ``C_MOVE`` to
        have the source send to ``destination`` on a new association.
    cost : float
        The cost of using this route (charged once per planned leg).
    destination : str or None
        The receiving AE title for ``C_MOVE``; must be ``None`` for
        ``C_GET``.
    """

    source: str
    method: RetrieveMethod
    cost: float
    destination: str | None = None

    def __post_init__(self) -> None:
        if self.method is RetrieveMethod.C_MOVE and not self.destination:
            raise ValueError("C-MOVE routes require a 'destination' AE title")
        if self.method is RetrieveMethod.C_GET and self.destination is not None:
            raise ValueError(
                "C-GET routes receive on the existing association and must "
                "not set a 'destination'"
            )
        if self.cost < 0:
            raise ValueError("'cost' must not be negative")


@dataclass(frozen=True)
class QueryRequirement:
    """The conditions needed to *query* the source for a SOP Class."""

    source: str
    """AE title of the station being queried."""
    model_uid: str
    """The Query/Retrieve GET or MOVE SOP Class UID to use."""
    association: str
    """``'existing'``: the query runs on the current association."""
    requester_role: str
    """The acquirer's role for the query (always ``'SCU'``)."""
    responder_role: str
    """The source's role for the query (always ``'SCP'``)."""


@dataclass(frozen=True)
class ReceptionRequirement:
    """The conditions needed to *receive* the instances of a SOP Class."""

    method: RetrieveMethod
    association: str
    """``'existing'`` for C-GET, ``'new'`` for C-MOVE."""
    receiver: str
    """AE title of the receiving station."""
    receiver_role: str
    """The receiver's Storage role (always ``'SCP'``)."""
    sender: str
    """AE title of the sending (source) station."""
    sender_role: str
    """The sender's Storage role (always ``'SCU'``)."""
    storage_classes: frozenset
    """The Storage SOP Class UIDs that must be receivable."""


@dataclass
class LegPlan:
    """The planned route for a single SOP Class."""

    sop_class_uid: str
    route: RouteOption
    query: QueryRequirement
    reception: ReceptionRequirement
    instances: tuple
    """The pending SOP Instance UIDs covered by this leg."""
    depends_on: frozenset
    """AE titles of the stations on the chosen route."""
    revision: int
    """The planner revision at which this leg was (re)computed."""


@dataclass(frozen=True)
class BlockedRoute:
    """A candidate route that cannot be used, with the reasons why."""

    route: RouteOption
    reasons: tuple


@dataclass
class BlockedClass:
    """A SOP Class for which no complete route exists."""

    sop_class_uid: str
    instances: tuple
    routes: tuple
    """One :class:`BlockedRoute` per candidate route."""
    depends_on: frozenset
    revision: int


@dataclass
class RetrievalPlan:
    """The result of planning: feasible legs plus blocking combinations."""

    legs: tuple
    blocked: tuple

    @property
    def total_cost(self) -> float:
        """The summed cost of every planned leg."""
        return sum(leg.route.cost for leg in self.legs)

    @property
    def is_complete(self) -> bool:
        """``True`` if every pending SOP Class has a feasible route."""
        return not self.blocked


class RetrievalPlanner:
    """Plan and incrementally replan minimum-cost retrieval routes.

    Parameters
    ----------
    local_ae : str
        The AE title of the acquirer (the C-GET receiver).
    routes : iterable of RouteOption, optional
        The allowed routes; a route that is not listed is not allowed.
    capabilities : iterable of StationCapabilities, optional
        The known station capabilities.
    """

    def __init__(
        self,
        local_ae: str,
        routes: Iterable[RouteOption] = (),
        capabilities: Iterable[StationCapabilities] = (),
    ) -> None:
        self.local_ae = local_ae
        self._routes = list(routes)
        self._stations = {caps.ae_title: caps for caps in capabilities}
        self._materials: dict[str, Material] = {}
        self._legs: dict[str, LegPlan] = {}
        self._blocked: dict[str, BlockedClass] = {}
        self._revision = 0

    @property
    def routes(self) -> list[RouteOption]:
        """Return the allowed routes."""
        return list(self._routes)

    @property
    def stations(self) -> dict[str, StationCapabilities]:
        """Return the known station capabilities, keyed by AE title."""
        return dict(self._stations)

    @property
    def materials(self) -> dict[str, Material]:
        """Return the tracked instances, keyed by SOP Instance UID."""
        return dict(self._materials)

    def add_route(self, route: RouteOption) -> None:
        """Add an allowed route and invalidate the legs that may use it."""
        self._routes.append(route)
        # A new route may unblock any blocked class, even one whose
        # previous candidate routes involved other stations entirely
        affected = set(self._blocked)
        # A new route may also offer a cheaper alternative for planned legs
        affected |= {
            sop_class
            for sop_class, leg in self._legs.items()
            if route.source in leg.depends_on
            or (route.destination or "") in leg.depends_on
        }
        self._invalidate(affected)

    def add_material(self, sop_instance_uid: str, sop_class_uid: str) -> Material:
        """Track a SOP Instance for retrieval.

        Instances that have already been verified are kept as
        :attr:`MaterialState.VERIFIED` and are not scheduled again.
        """
        existing = self._materials.get(sop_instance_uid)
        if existing is not None:
            return existing

        material = Material(sop_instance_uid, sop_class_uid)
        self._materials[sop_instance_uid] = material
        self._invalidate({sop_class_uid})

        return material

    def mark_verified(self, sop_instance_uids: Iterable[str]) -> None:
        """Mark instances as received and verified; they are never
        planned for retrieval again.
        """
        affected = set()
        for uid in sop_instance_uids:
            material = self._materials[uid]
            material.state = MaterialState.VERIFIED
            affected.add(material.sop_class_uid)
        self._invalidate(affected)

    def record_query_result(
        self, sop_instance_uid: str, matched: bool, *, executed: bool = True
    ) -> None:
        """Record the outcome of an *executed* query for an instance.

        Raises
        ------
        ValueError
            If ``executed`` is ``False``: an unexecuted instance must stay
            :attr:`QueryOutcome.NOT_QUERIED` and must never be recorded as
            "query returned no results".
        """
        if not executed:
            raise ValueError(
                "Unexecuted queries must not be recorded: leave the instance "
                "as NOT_QUERIED rather than reporting no results"
            )

        material = self._materials[sop_instance_uid]
        material.query_outcome = (
            QueryOutcome.MATCHED if matched else QueryOutcome.NO_MATCH
        )

    def unexecuted(self) -> list[Material]:
        """Return the pending instances that no query has been executed for."""
        return [
            material
            for material in self._materials.values()
            if material.state is MaterialState.PENDING
            and material.query_outcome is QueryOutcome.NOT_QUERIED
        ]

    def update_capabilities(self, capabilities: StationCapabilities) -> RetrievalPlan:
        """Update a station's capabilities and recompute only the routes
        that depend on that station, then return the new plan.
        """
        self._stations[capabilities.ae_title] = capabilities
        ae_title = capabilities.ae_title
        affected = {
            sop_class
            for sop_class, leg in self._legs.items()
            if ae_title in leg.depends_on
        }
        affected |= {
            sop_class
            for sop_class, blocked in self._blocked.items()
            if ae_title in blocked.depends_on
        }
        self._invalidate(affected)

        return self.plan()

    def plan(self) -> RetrievalPlan:
        """Return the minimum-cost plan for all pending instances.

        Legs that are still valid are reused; only legs that were
        invalidated (new material, capability updates, new routes) are
        recomputed.
        """
        pending: dict[str, list[str]] = {}
        for material in self._materials.values():
            if material.state is MaterialState.PENDING:
                pending.setdefault(material.sop_class_uid, []).append(
                    material.sop_instance_uid
                )

        for sop_class in list(self._legs):
            if sop_class not in pending:
                del self._legs[sop_class]
        for sop_class in list(self._blocked):
            if sop_class not in pending:
                del self._blocked[sop_class]

        for sop_class, instance_uids in pending.items():
            if sop_class in self._legs or sop_class in self._blocked:
                continue
            self._revision += 1
            self._compute_leg(sop_class, sorted(instance_uids))

        return RetrievalPlan(
            tuple(self._legs[sop_class] for sop_class in sorted(self._legs)),
            tuple(self._blocked[sop_class] for sop_class in sorted(self._blocked)),
        )

    def _invalidate(self, sop_classes: set[str]) -> None:
        for sop_class in sop_classes:
            self._legs.pop(sop_class, None)
            self._blocked.pop(sop_class, None)

    def _compute_leg(self, sop_class_uid: str, instance_uids: list[str]) -> None:
        candidates = []
        blocked_routes = []
        blocked_depends: set[str] = set()
        for route in self._routes:
            reasons, depends = self._evaluate(route, sop_class_uid)
            blocked_depends |= depends
            if reasons:
                blocked_routes.append(BlockedRoute(route, tuple(reasons)))
            else:
                candidates.append((route, depends))

        if not candidates:
            # Any of the candidate routes could unblock the class, so the
            # decision depends on every station they involve
            self._blocked[sop_class_uid] = BlockedClass(
                sop_class_uid,
                tuple(instance_uids),
                tuple(blocked_routes),
                frozenset(blocked_depends),
                self._revision,
            )
            return

        route, depends = min(
            candidates,
            key=lambda item: (
                item[0].cost,
                item[0].method.value,
                item[0].source,
                item[0].destination or "",
            ),
        )
        query, reception = self._requirements(route, sop_class_uid)
        self._legs[sop_class_uid] = LegPlan(
            sop_class_uid,
            route,
            query,
            reception,
            tuple(instance_uids),
            frozenset(depends),
            self._revision,
        )

    def _evaluate(
        self, route: RouteOption, sop_class_uid: str
    ) -> tuple[list[str], set[str]]:
        """Return the unmet conditions for `route` and the AE titles the
        decision depends on. An empty reason list means the route is
        feasible for `sop_class_uid`.
        """
        reasons = []
        depends = {route.source}
        source = self._stations.get(route.source)
        models = (
            _QR_GET_MODELS if route.method is RetrieveMethod.C_GET else _QR_MOVE_MODELS
        )

        if source is None:
            reasons.append(f"no capability information for source '{route.source}'")
        elif not set(source.query_models) & models:
            reasons.append(
                f"association with '{route.source}' has no accepted "
                f"{route.method.value} query/retrieve presentation context "
                "(acquirer SCU / source SCP)"
            )

        if route.method is RetrieveMethod.C_GET:
            # Reception on the *existing* association: the source must be
            # able to act as a Storage SCU for the class.
            if source is not None and sop_class_uid not in source.storage_classes:
                reasons.append(
                    f"C-GET for {sop_class_uid} requires an accepted Storage "
                    f"presentation context on the existing association with "
                    f"'{route.source}' (receiver '{self.local_ae}' SCP / "
                    "sender SCU)"
                )
        else:
            # Reception on a *new* association: the destination must be
            # able to act as a Storage SCP for the class.
            depends.add(route.destination)
            destination = self._stations.get(route.destination)
            if destination is None:
                reasons.append(
                    f"no capability information for C-MOVE destination "
                    f"'{route.destination}'"
                )
            elif sop_class_uid not in destination.storage_classes:
                reasons.append(
                    f"C-MOVE destination '{route.destination}' cannot receive "
                    f"{sop_class_uid}: no Storage SCP support on a new "
                    "association"
                )

        return reasons, depends

    def _requirements(
        self, route: RouteOption, sop_class_uid: str
    ) -> tuple[QueryRequirement, ReceptionRequirement]:
        source = self._stations[route.source]
        models = (
            _QR_GET_MODELS if route.method is RetrieveMethod.C_GET else _QR_MOVE_MODELS
        )
        model_uid = sorted(set(source.query_models) & models)[0]
        query = QueryRequirement(
            source=route.source,
            model_uid=model_uid,
            association="existing",
            requester_role="SCU",
            responder_role="SCP",
        )

        if route.method is RetrieveMethod.C_GET:
            reception = ReceptionRequirement(
                method=route.method,
                association="existing",
                receiver=self.local_ae,
                receiver_role="SCP",
                sender=route.source,
                sender_role="SCU",
                storage_classes=frozenset({sop_class_uid}),
            )
        else:
            reception = ReceptionRequirement(
                method=route.method,
                association="new",
                receiver=route.destination,
                receiver_role="SCP",
                sender=route.source,
                sender_role="SCU",
                storage_classes=frozenset({sop_class_uid}),
            )

        return query, reception
