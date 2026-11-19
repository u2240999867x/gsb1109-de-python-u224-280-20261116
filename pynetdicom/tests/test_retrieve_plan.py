"""Tests for the retrieve_plan module."""

import pytest

from pynetdicom.presentation import PresentationContext
from pynetdicom.retrieve_plan import (
    MaterialState,
    QueryOutcome,
    RetrievalPlanner,
    RetrieveMethod,
    RouteOption,
    StationCapabilities,
    capabilities_from_contexts,
)


CT = "1.2.840.10008.5.1.4.1.1.2"
MR = "1.2.840.10008.5.1.4.1.1.4"
STUDY_FIND = "1.2.840.10008.5.1.4.1.2.2.1"
STUDY_MOVE = "1.2.840.10008.5.1.4.1.2.2.2"
STUDY_GET = "1.2.840.10008.5.1.4.1.2.2.3"


def _context(abstract_syntax, as_scu=False, as_scp=False):
    cx = PresentationContext()
    cx.abstract_syntax = abstract_syntax
    cx._as_scu = as_scu
    cx._as_scp = as_scp
    return cx


def _get_route(source="PACS-A", cost=1.0):
    return RouteOption(source=source, method=RetrieveMethod.C_GET, cost=cost)


def _move_route(source="PACS-A", destination="STORE-1", cost=1.0):
    return RouteOption(
        source=source,
        method=RetrieveMethod.C_MOVE,
        cost=cost,
        destination=destination,
    )


class TestCapabilitiesFromContexts:
    def test_roles_split(self):
        contexts = [
            _context(STUDY_FIND, as_scp=True),
            _context(STUDY_GET, as_scp=True),
            _context(CT, as_scu=True),  # peer pushes to us (C-GET reception)
            _context(MR, as_scp=True),  # peer receives from us (not usable)
        ]
        caps = capabilities_from_contexts("PACS-A", contexts)

        assert caps.ae_title == "PACS-A"
        assert caps.query_models == frozenset({STUDY_FIND, STUDY_GET})
        assert caps.storage_classes == frozenset({CT})


class TestRouteOption:
    def test_move_requires_destination(self):
        with pytest.raises(ValueError, match="destination"):
            RouteOption(source="PACS-A", method=RetrieveMethod.C_MOVE, cost=1.0)

    def test_get_forbids_destination(self):
        with pytest.raises(ValueError, match="destination"):
            RouteOption(
                source="PACS-A",
                method=RetrieveMethod.C_GET,
                cost=1.0,
                destination="STORE-1",
            )

    def test_negative_cost_rejected(self):
        with pytest.raises(ValueError, match="cost"):
            RouteOption(source="PACS-A", method=RetrieveMethod.C_GET, cost=-1.0)


class TestPlanning:
    def test_cget_leg_requirements(self):
        planner = RetrievalPlanner(
            "ACQ",
            routes=[_get_route(cost=3.0)],
            capabilities=[
                StationCapabilities("PACS-A", frozenset({STUDY_GET}), frozenset({CT}))
            ],
        )
        planner.add_material("1.2.3.1", CT)
        plan = planner.plan()

        assert plan.is_complete
        assert plan.total_cost == 3.0
        (leg,) = plan.legs
        assert leg.route.method is RetrieveMethod.C_GET
        assert leg.instances == ("1.2.3.1",)

        # Query conditions: existing association, we are SCU
        assert leg.query.source == "PACS-A"
        assert leg.query.model_uid == STUDY_GET
        assert leg.query.association == "existing"
        assert leg.query.requester_role == "SCU"
        assert leg.query.responder_role == "SCP"

        # Reception conditions: same association, we are the Storage SCP
        assert leg.reception.association == "existing"
        assert leg.reception.receiver == "ACQ"
        assert leg.reception.receiver_role == "SCP"
        assert leg.reception.sender == "PACS-A"
        assert leg.reception.sender_role == "SCU"
        assert leg.reception.storage_classes == frozenset({CT})

    def test_cmove_leg_requirements(self):
        planner = RetrievalPlanner(
            "ACQ",
            routes=[_move_route(cost=2.0)],
            capabilities=[
                StationCapabilities("PACS-A", frozenset({STUDY_MOVE}), frozenset()),
                StationCapabilities("STORE-1", frozenset(), frozenset({CT})),
            ],
        )
        planner.add_material("1.2.3.1", CT)
        plan = planner.plan()

        assert plan.is_complete
        (leg,) = plan.legs
        assert leg.query.model_uid == STUDY_MOVE
        assert leg.query.association == "existing"

        # Reception conditions: new association, destination is the SCP
        assert leg.reception.association == "new"
        assert leg.reception.receiver == "STORE-1"
        assert leg.reception.receiver_role == "SCP"
        assert leg.reception.sender == "PACS-A"
        assert leg.reception.sender_role == "SCU"

    def test_lowest_cost_route_selected(self):
        planner = RetrievalPlanner(
            "ACQ",
            routes=[
                _get_route(cost=5.0),
                _move_route(cost=2.0),
            ],
            capabilities=[
                StationCapabilities(
                    "PACS-A", frozenset({STUDY_GET, STUDY_MOVE}), frozenset({CT})
                ),
                StationCapabilities("STORE-1", frozenset(), frozenset({CT})),
            ],
        )
        planner.add_material("1.2.3.1", CT)
        plan = planner.plan()

        (leg,) = plan.legs
        assert leg.route.method is RetrieveMethod.C_MOVE
        assert plan.total_cost == 2.0

    def test_queryable_does_not_imply_receivable(self):
        # The class can be queried via C-GET but no Storage context for it
        # exists on the association, so the payload cannot be received
        planner = RetrievalPlanner(
            "ACQ",
            routes=[_get_route()],
            capabilities=[
                StationCapabilities("PACS-A", frozenset({STUDY_GET}), frozenset())
            ],
        )
        planner.add_material("1.2.3.1", CT)
        plan = planner.plan()

        assert not plan.is_complete
        (blocked,) = plan.blocked
        assert blocked.sop_class_uid == CT
        (blocked_route,) = blocked.routes
        assert any("Storage presentation context" in r for r in blocked_route.reasons)

    def test_roles_not_interchangeable(self):
        # Destination storage support must not satisfy C-GET reception...
        planner = RetrievalPlanner(
            "ACQ",
            routes=[_get_route()],
            capabilities=[
                StationCapabilities("PACS-A", frozenset({STUDY_GET}), frozenset()),
                StationCapabilities("STORE-1", frozenset(), frozenset({CT})),
            ],
        )
        planner.add_material("1.2.3.1", CT)
        assert not planner.plan().is_complete

        # ...and association storage contexts must not satisfy a C-MOVE
        # destination
        planner = RetrievalPlanner(
            "ACQ",
            routes=[_move_route()],
            capabilities=[
                StationCapabilities("PACS-A", frozenset({STUDY_MOVE}), frozenset({CT})),
                StationCapabilities("STORE-1", frozenset(), frozenset()),
            ],
        )
        planner.add_material("1.2.3.1", CT)
        plan = planner.plan()

        assert not plan.is_complete
        (blocked,) = plan.blocked
        assert any("STORE-1" in r for r in blocked.routes[0].reasons)

    def test_blocked_combinations_listed(self):
        planner = RetrievalPlanner(
            "ACQ",
            routes=[_get_route(), _move_route()],
            capabilities=[
                StationCapabilities("PACS-A", frozenset(), frozenset()),
                StationCapabilities("STORE-1", frozenset(), frozenset()),
            ],
        )
        planner.add_material("1.2.3.1", CT)
        plan = planner.plan()

        assert not plan.is_complete
        (blocked,) = plan.blocked
        assert blocked.instances == ("1.2.3.1",)
        # Every candidate route is listed with its own failure reasons
        assert len(blocked.routes) == 2
        for blocked_route in blocked.routes:
            assert blocked_route.reasons


class TestMaterialTracking:
    def test_unexecuted_not_recorded_as_no_match(self):
        planner = RetrievalPlanner("ACQ")
        planner.add_material("1.2.3.1", CT)

        with pytest.raises(ValueError, match="Unexecuted"):
            planner.record_query_result("1.2.3.1", matched=False, executed=False)

        material = planner.materials["1.2.3.1"]
        assert material.query_outcome is QueryOutcome.NOT_QUERIED
        assert planner.unexecuted() == [material]

    def test_executed_query_outcomes(self):
        planner = RetrievalPlanner("ACQ")
        planner.add_material("1.2.3.1", CT)
        planner.add_material("1.2.3.2", CT)

        planner.record_query_result("1.2.3.1", matched=False)
        planner.record_query_result("1.2.3.2", matched=True)

        assert planner.materials["1.2.3.1"].query_outcome is QueryOutcome.NO_MATCH
        assert planner.materials["1.2.3.2"].query_outcome is QueryOutcome.MATCHED
        assert planner.unexecuted() == []

    def test_verified_material_not_refetched(self):
        planner = RetrievalPlanner(
            "ACQ",
            routes=[_get_route()],
            capabilities=[
                StationCapabilities("PACS-A", frozenset({STUDY_GET}), frozenset({CT}))
            ],
        )
        planner.add_material("1.2.3.1", CT)
        planner.add_material("1.2.3.2", CT)
        planner.mark_verified(["1.2.3.1"])

        (leg,) = planner.plan().legs
        assert leg.instances == ("1.2.3.2",)

        # Re-adding a verified instance does not schedule it again
        material = planner.add_material("1.2.3.1", CT)
        assert material.state is MaterialState.VERIFIED
        (leg,) = planner.plan().legs
        assert leg.instances == ("1.2.3.2",)

        # Nor does a capability update bring it back
        planner.update_capabilities(
            StationCapabilities("PACS-A", frozenset({STUDY_GET}), frozenset({CT}))
        )
        (leg,) = planner.plan().legs
        assert leg.instances == ("1.2.3.2",)


class TestIncrementalReplan:
    def test_only_dependent_routes_recomputed(self):
        planner = RetrievalPlanner(
            "ACQ",
            routes=[
                _get_route(source="PACS-A", cost=1.0),
                _move_route(source="PACS-B", destination="STORE-1", cost=1.0),
            ],
            capabilities=[
                StationCapabilities("PACS-A", frozenset({STUDY_GET}), frozenset({CT})),
                StationCapabilities("PACS-B", frozenset({STUDY_MOVE}), frozenset()),
                StationCapabilities("STORE-1", frozenset(), frozenset({MR})),
            ],
        )
        planner.add_material("1.2.3.1", CT)
        planner.add_material("1.2.3.2", MR)
        plan = planner.plan()
        legs = {leg.sop_class_uid: leg for leg in plan.legs}
        ct_revision = legs[CT].revision
        mr_revision = legs[MR].revision

        # Updating PACS-A only affects the CT leg that depends on it
        planner.update_capabilities(
            StationCapabilities("PACS-A", frozenset({STUDY_GET}), frozenset({CT}))
        )
        plan = planner.plan()
        legs = {leg.sop_class_uid: leg for leg in plan.legs}
        assert legs[CT].revision > ct_revision
        assert legs[MR].revision == mr_revision

    def test_capability_update_unblocks_route(self):
        planner = RetrievalPlanner(
            "ACQ",
            routes=[_move_route()],
            capabilities=[
                StationCapabilities("PACS-A", frozenset({STUDY_MOVE}), frozenset()),
                StationCapabilities("STORE-1", frozenset(), frozenset()),
            ],
        )
        planner.add_material("1.2.3.1", CT)
        assert not planner.plan().is_complete

        plan = planner.update_capabilities(
            StationCapabilities("STORE-1", frozenset(), frozenset({CT}))
        )
        assert plan.is_complete
        assert plan.legs[0].route.destination == "STORE-1"

    def test_new_route_unblocks_class(self):
        planner = RetrievalPlanner(
            "ACQ",
            routes=[_get_route()],
            capabilities=[
                StationCapabilities(
                    "PACS-A", frozenset({STUDY_GET, STUDY_MOVE}), frozenset()
                ),
                StationCapabilities("STORE-1", frozenset(), frozenset({CT})),
            ],
        )
        planner.add_material("1.2.3.1", CT)
        assert not planner.plan().is_complete

        planner.add_route(_move_route(cost=4.0))
        plan = planner.plan()
        assert plan.is_complete
        assert plan.total_cost == 4.0
