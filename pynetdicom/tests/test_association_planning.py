"""Tests for the association planning module."""

import pytest

from pydicom.uid import UID

from pynetdicom.association_planning import (
    MAXIMUM_CONTEXTS,
    AssociationPlanError,
    AssociationProposal,
    PlanningRequirement,
    plan_associations,
)
from pynetdicom.presentation import (
    PresentationContext,
    build_context,
    negotiate_as_acceptor,
)

IMPLICIT_VR_LE = "1.2.840.10008.1.2"
EXPLICIT_VR_LE = "1.2.840.10008.1.2.1"
JPEG_BASELINE = "1.2.840.10008.1.2.4.50"
JPEG_LOSSLESS = "1.2.840.10008.1.2.4.70"

VERIFICATION = "1.2.840.10008.1.1"
CT_STORAGE = "1.2.840.10008.5.1.4.1.1.2"
MR_STORAGE = "1.2.840.10008.5.1.4.1.1.4"
US_STORAGE = "1.2.840.10008.5.1.4.1.1.6.1"


class TestPlanningRequirement:
    def test_single_transfer_syntax_accepted(self):
        requirement = PlanningRequirement(VERIFICATION, IMPLICIT_VR_LE)
        assert requirement.abstract_syntax == UID(VERIFICATION)
        assert requirement.transfer_syntaxes == (UID(IMPLICIT_VR_LE),)
        assert requirement.roles == (None, None)
        assert requirement.proposes_roles is False

    def test_duplicate_transfer_syntaxes_removed(self):
        requirement = PlanningRequirement(
            CT_STORAGE, [IMPLICIT_VR_LE, IMPLICIT_VR_LE, EXPLICIT_VR_LE]
        )
        assert requirement.transfer_syntaxes == (
            UID(IMPLICIT_VR_LE),
            UID(EXPLICIT_VR_LE),
        )

    def test_bytes_and_uid_inputs(self):
        requirement = PlanningRequirement(
            UID(VERIFICATION), [IMPLICIT_VR_LE.encode("ascii"), UID(EXPLICIT_VR_LE)]
        )
        assert requirement.transfer_syntaxes == (
            UID(IMPLICIT_VR_LE),
            UID(EXPLICIT_VR_LE),
        )

    def test_empty_transfer_syntaxes_raises(self):
        with pytest.raises(ValueError, match="At least one transfer syntax"):
            PlanningRequirement(VERIFICATION, [])

    def test_partial_roles_raises(self):
        with pytest.raises(ValueError, match="both be None or both be bool"):
            PlanningRequirement(VERIFICATION, IMPLICIT_VR_LE, scu_role=True)

    def test_invalid_role_type_raises(self):
        with pytest.raises(TypeError, match="bool or None"):
            PlanningRequirement(VERIFICATION, IMPLICIT_VR_LE, "yes", False)

    def test_from_context(self):
        context = build_context(CT_STORAGE, [IMPLICIT_VR_LE, JPEG_BASELINE])
        requirement = PlanningRequirement.from_context(
            context, scu_role=True, scp_role=True
        )
        assert requirement.abstract_syntax == UID(CT_STORAGE)
        assert requirement.transfer_syntaxes == (
            UID(IMPLICIT_VR_LE),
            UID(JPEG_BASELINE),
        )
        assert requirement.roles == (True, True)
        assert requirement.proposes_roles is True


class TestPlanAssociationsSingle:
    def setup_method(self):
        self.requirements = [
            PlanningRequirement(
                VERIFICATION,
                [EXPLICIT_VR_LE, IMPLICIT_VR_LE],
                name="verification",
            ),
            PlanningRequirement(
                CT_STORAGE,
                [JPEG_BASELINE, IMPLICIT_VR_LE],
                scu_role=True,
                scp_role=True,
            ),
        ]

    def test_proposal_contains_all_requirements(self):
        proposals = plan_associations(self.requirements)
        assert len(proposals) == 1
        proposal = proposals[0]

        assert isinstance(proposal, AssociationProposal)
        assert proposal.index == 0
        assert proposal.requirement_ids == ["verification", CT_STORAGE]
        assert proposal.abstract_syntaxes == [UID(VERIFICATION), UID(CT_STORAGE)]
        assert len(proposal.contexts) == 2

    def test_context_ids_are_unique_odd_values(self):
        proposal = plan_associations(self.requirements)[0]
        assert [cx.context_id for cx in proposal.contexts] == [1, 3]

    def test_all_alternative_syntaxes_proposed(self):
        proposal = plan_associations(self.requirements)[0]
        context = proposal.contexts[1]
        assert context.abstract_syntax == UID(CT_STORAGE)
        assert context.transfer_syntax == [
            UID(JPEG_BASELINE),
            UID(IMPLICIT_VR_LE),
        ]

    def test_proposals_are_not_treated_as_accepted(self):
        proposal = plan_associations(self.requirements)[0]
        assert all(cx.result is None for cx in proposal.contexts)
        assert all(cx.as_scu is None for cx in proposal.contexts)
        assert all(cx.as_scp is None for cx in proposal.contexts)

    def test_role_selection_items(self):
        proposal = plan_associations(self.requirements)[0]
        roles = proposal.role_selection
        assert len(roles) == 1
        assert roles[0].sop_class_uid == UID(CT_STORAGE)
        assert roles[0].scu_role is True
        assert roles[0].scp_role is True

    def test_cheapest_syntax_selected_for_expected_cost(self):
        costs = {
            IMPLICIT_VR_LE: 1,
            EXPLICIT_VR_LE: 2,
            JPEG_BASELINE: 5,
        }
        proposal = plan_associations(self.requirements, transfer_costs=costs)[0]

        assert proposal.selected_transfer_syntax[UID(VERIFICATION)] == UID(
            IMPLICIT_VR_LE
        )
        assert proposal.selected_transfer_syntax[UID(CT_STORAGE)] == UID(IMPLICIT_VR_LE)
        assert proposal.expected_cost == pytest.approx(2.0)

    def test_missing_cost_defaults_to_zero(self):
        proposal = plan_associations(self.requirements)[0]
        assert proposal.expected_cost == 0.0
        assert set(proposal.selected_transfer_syntax.values()) == {
            UID(EXPLICIT_VR_LE),
            UID(JPEG_BASELINE),
        }

    def test_unsupported_syntaxes_excluded_from_selection(self):
        costs = {IMPLICIT_VR_LE: 1, JPEG_BASELINE: 5}
        proposal = plan_associations(
            self.requirements,
            transfer_costs=costs,
            supported_transfer_syntaxes=[JPEG_BASELINE, EXPLICIT_VR_LE],
        )[0]

        assert proposal.selected_transfer_syntax[UID(VERIFICATION)] == UID(
            EXPLICIT_VR_LE
        )
        assert proposal.selected_transfer_syntax[UID(CT_STORAGE)] == UID(JPEG_BASELINE)

    def test_no_supported_syntax_raises(self):
        with pytest.raises(AssociationPlanError, match="No proposable transfer syntax"):
            plan_associations(
                self.requirements,
                supported_transfer_syntaxes=[JPEG_LOSSLESS],
            )

    def test_invalid_cost_raises(self):
        with pytest.raises(ValueError, match="non-negative number"):
            plan_associations(self.requirements, transfer_costs={IMPLICIT_VR_LE: -1})


class TestPlanAssociationsSplit:
    def _requirements(self, count):
        abstract_syntaxes = [
            VERIFICATION,
            CT_STORAGE,
            MR_STORAGE,
            US_STORAGE,
            "1.2.840.10008.5.1.4.1.1.1",
        ]
        return [
            PlanningRequirement(abstract_syntaxes[idx], IMPLICIT_VR_LE)
            for idx in range(count)
        ]

    def test_minimum_number_of_associations(self):
        requirements = self._requirements(5)
        proposals = plan_associations(requirements, max_contexts=2)

        assert len(proposals) == 3
        assert [len(proposal.contexts) for proposal in proposals] == [2, 2, 1]
        for proposal in proposals:
            assert len(proposal.contexts) <= 2

    def test_context_ids_restart_per_association(self):
        proposals = plan_associations(self._requirements(5), max_contexts=2)
        assert [cx.context_id for cx in proposals[0].contexts] == [1, 3]
        assert [cx.context_id for cx in proposals[1].contexts] == [1, 3]
        assert [cx.context_id for cx in proposals[2].contexts] == [1]

    def test_every_requirement_owned_exactly_once(self):
        requirements = self._requirements(5)
        proposals = plan_associations(requirements, max_contexts=2)

        owned = [uid for proposal in proposals for uid in proposal.abstract_syntaxes]
        assert owned == [requirement.abstract_syntax for requirement in requirements]

    def test_limit_equal_to_count_returns_single_association(self):
        proposals = plan_associations(self._requirements(3), max_contexts=3)
        assert len(proposals) == 1

    def test_default_limit_is_dicom_maximum(self):
        assert MAXIMUM_CONTEXTS == 128
        proposals = plan_associations(self._requirements(3))
        assert len(proposals) == 1

    def test_invalid_max_contexts(self):
        requirements = self._requirements(1)
        with pytest.raises(ValueError, match="between 1 and 128"):
            plan_associations(requirements, max_contexts=0)
        with pytest.raises(ValueError, match="between 1 and 128"):
            plan_associations(requirements, max_contexts=129)

    def test_empty_requirements(self):
        assert plan_associations([]) == []


class TestRoleConflicts:
    def test_required_scp_without_role_proposal_is_unfeasible(self):
        # Without an SCP/SCU Role Selection item the requestor is always SCU
        requirement = PlanningRequirement(
            CT_STORAGE,
            IMPLICIT_VR_LE,
            require_scu=False,
            require_scp=True,
        )
        with pytest.raises(AssociationPlanError, match="cannot satisfy the required"):
            plan_associations([requirement])

    def test_required_both_roles_with_scu_only_proposal_is_unfeasible(self):
        requirement = PlanningRequirement(
            CT_STORAGE,
            IMPLICIT_VR_LE,
            scu_role=True,
            scp_role=False,
            require_scu=True,
            require_scp=True,
        )
        with pytest.raises(AssociationPlanError):
            plan_associations([requirement])

    def test_conflict_not_rescued_by_another_association(self):
        # Even though the requirements are split across associations, the
        #   conflicting requirement must be reported as unsolvable rather than
        #   relying on a proposal in a different association succeeding
        requirements = [
            PlanningRequirement(VERIFICATION, IMPLICIT_VR_LE),
            PlanningRequirement(
                CT_STORAGE,
                IMPLICIT_VR_LE,
                require_scu=False,
                require_scp=True,
            ),
            PlanningRequirement(MR_STORAGE, IMPLICIT_VR_LE),
        ]
        with pytest.raises(AssociationPlanError):
            plan_associations(requirements, max_contexts=1)

    def test_feasible_role_combinations_accepted(self):
        feasible = [
            ((True, True), True, True),
            ((False, True), False, True),
            ((True, False), True, False),
            # (False, False) still has the default-role fallback outcome
            ((False, False), True, False),
        ]
        for roles, require_scu, require_scp in feasible:
            requirement = PlanningRequirement(
                CT_STORAGE,
                IMPLICIT_VR_LE,
                scu_role=roles[0],
                scp_role=roles[1],
                require_scu=require_scu,
                require_scp=require_scp,
            )
            proposals = plan_associations([requirement])
            assert len(proposals) == 1

    def test_both_required_roles_must_be_bool_or_error(self):
        with pytest.raises(ValueError, match="At least one of"):
            PlanningRequirement(
                CT_STORAGE,
                IMPLICIT_VR_LE,
                require_scu=False,
                require_scp=False,
            )


class TestNegotiationSemantics:
    def test_planned_contexts_negotiate_as_acceptor(self):
        requirements = [
            PlanningRequirement(
                CT_STORAGE,
                [JPEG_BASELINE, IMPLICIT_VR_LE],
                scu_role=True,
                scp_role=True,
            ),
            PlanningRequirement(VERIFICATION, [IMPLICIT_VR_LE]),
        ]
        proposal = plan_associations(requirements)[0]

        acceptor_contexts = [
            build_context(CT_STORAGE, [IMPLICIT_VR_LE, JPEG_BASELINE]),
            build_context(VERIFICATION, [IMPLICIT_VR_LE]),
        ]
        acceptor_contexts[0].scu_role = True
        acceptor_contexts[0].scp_role = True

        roles = {
            item.sop_class_uid: (item.scu_role, item.scp_role)
            for item in proposal.role_selection
        }
        results, reply_roles = negotiate_as_acceptor(
            proposal.contexts, acceptor_contexts, roles
        )

        assert [cx.result for cx in results] == [0x00, 0x00]
        ct_context = next(cx for cx in results if cx.abstract_syntax == UID(CT_STORAGE))
        assert ct_context.as_scu is True
        assert ct_context.as_scp is True
        assert len(reply_roles) == 1

    def test_acceptor_may_select_any_proposed_alternative(self):
        requirement = PlanningRequirement(CT_STORAGE, [JPEG_BASELINE, IMPLICIT_VR_LE])
        proposal = plan_associations(
            [requirement], transfer_costs={JPEG_BASELINE: 9, IMPLICIT_VR_LE: 1}
        )[0]

        # Planning expects the cheap syntax, but negotiation may differ
        assert proposal.selected_transfer_syntax[UID(CT_STORAGE)] == UID(IMPLICIT_VR_LE)

        acceptor_contexts = [build_context(CT_STORAGE, [JPEG_BASELINE])]
        results, _ = negotiate_as_acceptor(proposal.contexts, acceptor_contexts)

        assert results[0].result == 0x00
        assert results[0].transfer_syntax == [UID(JPEG_BASELINE)]


class TestInputValidation:
    def test_duplicate_abstract_syntax_raises(self):
        requirements = [
            PlanningRequirement(CT_STORAGE, IMPLICIT_VR_LE),
            PlanningRequirement(CT_STORAGE, EXPLICIT_VR_LE),
        ]
        with pytest.raises(ValueError, match="Duplicate requirement"):
            plan_associations(requirements)

    def test_invalid_requirement_type_raises(self):
        with pytest.raises(TypeError, match="PlanningRequirement instances"):
            plan_associations([PresentationContext()])

    def test_invalid_max_contexts_type(self):
        requirement = PlanningRequirement(CT_STORAGE, IMPLICIT_VR_LE)
        with pytest.raises(TypeError, match="'max_contexts' must be an int"):
            plan_associations([requirement], max_contexts=2.0)
