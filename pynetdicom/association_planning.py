"""Planning of association proposals from required SOP class combinations.

This module is used to plan one or more association requests when the caller
has a number of required SOP class/transfer syntax combinations that must
each be executable, along with a limit on the number of presentation
contexts that may be included in a single association.

The output is a *proposal* only: whether a presentation context is accepted
and which transfer syntax is selected is determined by the association
*Acceptor* during negotiation (see
:func:`~pynetdicom.presentation.negotiate_as_acceptor`). The planner never
treats a proposed context or transfer syntax as having been accepted.
"""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypeAlias, cast

from pydicom.uid import UID

from pynetdicom.presentation import SCP_SCU_ROLES, PresentationContext, build_role

if TYPE_CHECKING:
    from pynetdicom.pdu_primitives import SCP_SCU_RoleSelectionNegotiation

# The DICOM presentation context ID is an odd integer between 1 and 255, so
#   at most 128 presentation contexts may be included in an association
MAXIMUM_CONTEXTS = 128

RoleType: TypeAlias = tuple[None | bool, None | bool]
CostType: TypeAlias = dict[str | bytes | UID, int | float]
UIDInputType: TypeAlias = str | bytes | UID


class AssociationPlanError(ValueError):
    """Exception raised when no valid association proposal can be produced.

    This includes role combinations that are rejected under the SCP/SCU Role
    Selection negotiation rules for every possible acceptor response, and
    requirements with no transfer syntax that can be proposed.
    """


def _as_uid(value: UIDInputType) -> UID:
    """Return `value` as a :class:`~pydicom.uid.UID`."""
    if isinstance(value, bytes):
        return UID(value.decode("ascii"))

    return UID(value)


def _role_proposal_is_feasible(
    roles: RoleType, require_scu: bool, require_scp: bool
) -> bool:
    """Return ``True`` if proposing `roles` can satisfy the required roles.

    The SCP/SCU Role Selection negotiation table from
    :mod:`~pynetdicom.presentation` is used, so the check follows the same
    semantics as actual association negotiation. Every possible acceptor
    response is considered: the proposal is feasible only when at least one
    negotiation outcome grants the requestor the required roles. When neither
    role is required then any outcome that leaves the requestor with a role
    is sufficient.
    """
    if not require_scu and not require_scp:
        return any(
            outcome[0] or outcome[1]
            for outcome in SCP_SCU_ROLES[cast(tuple[bool, bool], roles)].values()
        )

    return any(
        (not require_scu or outcome[0]) and (not require_scp or outcome[1])
        for outcome in SCP_SCU_ROLES[cast(tuple[bool, bool], roles)].values()
    )


@dataclass(slots=True, init=False)
class PlanningRequirement:
    """A required SOP class combination that must be executable.

    Each requirement becomes exactly one presentation context in one of the
    resulting association proposals. The transfer syntaxes are alternatives:
    only one can be accepted per context, so the expected conversion cost is
    based on the cheapest proposable alternative rather than the sum of all
    alternatives.

    Attributes
    ----------
    abstract_syntax : pydicom.uid.UID
        The abstract syntax (SOP Class UID) that must be proposed.
    transfer_syntaxes : tuple of pydicom.uid.UID
        The alternative transfer syntax UIDs, in order of preference. At
        least one is required.
    scu_role : bool or None
        The SCU role proposed for the association *Requestor*. If both
        `scu_role` and `scp_role` are ``None`` (default) then no SCP/SCU Role
        Selection item is included.
    scp_role : bool or None
        The SCP role proposed for the association *Requestor*.
    require_scu : bool
        ``True`` (default) if the requestor must end up with the SCU role
        after negotiation for the combination to be executable.
    require_scp : bool
        ``True`` (default) if the requestor must end up with the SCP role
        after negotiation for the combination to be executable.
    name : str or None
        Optional label used when reporting the requirement, for example in
        error messages and :attr:`AssociationProposal.requirement_ids`.
    """

    abstract_syntax: UID
    transfer_syntaxes: tuple[UID, ...]
    scu_role: None | bool
    scp_role: None | bool
    require_scu: bool
    require_scp: bool
    name: None | str

    def __init__(
        self,
        abstract_syntax: UIDInputType,
        transfer_syntaxes: UIDInputType | list[UIDInputType] | tuple[UIDInputType, ...],
        scu_role: None | bool = None,
        scp_role: None | bool = None,
        require_scu: bool = True,
        require_scp: bool = False,
        name: None | str = None,
    ) -> None:
        if isinstance(transfer_syntaxes, (str, bytes, UID)):
            transfer_syntaxes = [transfer_syntaxes]

        alternatives: list[UID] = []
        for syntax in transfer_syntaxes:
            uid = _as_uid(syntax)
            if uid not in alternatives:
                alternatives.append(uid)

        if not alternatives:
            raise ValueError(
                "At least one transfer syntax is required for "
                f"'{_as_uid(abstract_syntax)}'"
            )

        roles = (scu_role, scp_role)
        if None in roles and roles != (None, None):
            raise ValueError(
                "'scu_role' and 'scp_role' must both be None or both be bool"
            )

        for role in roles:
            if not isinstance(role, (bool, type(None))):
                raise TypeError("'scu_role' and 'scp_role' must be bool or None")

        if not isinstance(require_scu, bool) or not isinstance(require_scp, bool):
            raise TypeError("'require_scu' and 'require_scp' must be bool")

        if not require_scu and not require_scp:
            raise ValueError(
                "At least one of 'require_scu' and 'require_scp' must be True"
            )

        self.abstract_syntax = _as_uid(abstract_syntax)
        self.transfer_syntaxes = tuple(alternatives)
        self.scu_role = scu_role
        self.scp_role = scp_role
        self.require_scu = require_scu
        self.require_scp = require_scp
        self.name = name

    @property
    def roles(self) -> RoleType:
        """The proposed requestor roles as an ``(scu_role, scp_role)`` tuple."""
        return (self.scu_role, self.scp_role)

    @property
    def proposes_roles(self) -> bool:
        """Return ``True`` if an SCP/SCU Role Selection item will be proposed."""
        return (self.scu_role, self.scp_role) != (None, None)

    @classmethod
    def from_context(
        cls,
        context: PresentationContext,
        scu_role: None | bool = None,
        scp_role: None | bool = None,
        require_scu: bool = True,
        require_scp: bool = False,
        name: None | str = None,
    ) -> "PlanningRequirement":
        """Return a requirement built from an existing presentation context.

        Parameters
        ----------
        context : presentation.PresentationContext
            The context providing the abstract and transfer syntaxes.
        scu_role : bool or None
            The SCU role proposed for the association *Requestor*.
        scp_role : bool or None
            The SCP role proposed for the association *Requestor*.
        require_scu : bool
            ``True`` if the requestor must retain the SCU role after
            negotiation.
        require_scp : bool
            ``True`` if the requestor must retain the SCP role after
            negotiation.
        name : str or None
            Optional label used when reporting the requirement.
        """
        return cls(
            cast(UID, context.abstract_syntax),
            tuple(context.transfer_syntax),
            scu_role,
            scp_role,
            require_scu,
            require_scp,
            name,
        )


@dataclass(slots=True)
class AssociationProposal:
    """A proposed association containing a subset of the requirements.

    Attributes
    ----------
    index : int
        The zero-based index of the proposal within the returned plan.
    requirements : list of PlanningRequirement
        The requirements assigned to this association, in input order.
    contexts : list of presentation.PresentationContext
        The presentation contexts to propose, with unique odd context IDs
        starting at 1. Each context's :attr:`~PresentationContext.result` is
        ``None`` because nothing has been negotiated yet.
    selected_transfer_syntax : dict of UID
        The cheapest proposable transfer syntax selected for each abstract
        syntax, used for cost accounting. The peer remains free to accept a
        different alternative (or reject the context) during negotiation.
    expected_cost : float
        The sum of the conversion costs for `selected_transfer_syntax`.
    """

    index: int
    requirements: list[PlanningRequirement]
    contexts: list[PresentationContext] = field(init=False)
    selected_transfer_syntax: dict[UID, UID] = field(init=False)
    expected_cost: float = field(init=False, default=0.0)

    def __post_init__(self) -> None:
        self.contexts = []
        self.selected_transfer_syntax = {}
        self.expected_cost = 0.0

    @property
    def abstract_syntaxes(self) -> list[UID]:
        """The abstract syntaxes included in the proposal, in context order."""
        return [requirement.abstract_syntax for requirement in self.requirements]

    @property
    def requirement_ids(self) -> list[str]:
        """Labels for the included requirements.

        Returns the requirement :attr:`~PlanningRequirement.name` when set,
        otherwise the abstract syntax UID as a :class:`str`.
        """
        return [
            requirement.name or str(requirement.abstract_syntax)
            for requirement in self.requirements
        ]

    @property
    def role_selection(self) -> "list[SCP_SCU_RoleSelectionNegotiation]":
        """Role selection items suitable for use as ``ext_neg`` items."""
        return [
            build_role(
                requirement.abstract_syntax,
                cast(bool, requirement.scu_role),
                cast(bool, requirement.scp_role),
            )
            for requirement in self.requirements
            if requirement.proposes_roles
        ]


def _normalise_costs(transfer_costs: CostType | None) -> dict[UID, float]:
    """Return `transfer_costs` keyed by :class:`~pydicom.uid.UID`."""
    costs: dict[UID, float] = {}
    for syntax, cost in (transfer_costs or {}).items():
        if not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost < 0:
            raise ValueError(
                f"Conversion cost for '{_as_uid(syntax)}' must be a non-negative number"
            )

        costs[_as_uid(syntax)] = float(cost)

    return costs


def _pack_minimum(
    requirements: list[PlanningRequirement], max_contexts: int
) -> list[list[int]]:
    """Return requirement indices packed into the minimum number of bins.

    Each requirement consumes exactly one presentation context and any two
    requirements may share an association, so the minimum number of
    associations is ``ceil(len(requirements) / max_contexts)``. Contiguous
    chunks in input order achieve the bound deterministically.
    """
    return [
        list(range(start, min(start + max_contexts, len(requirements))))
        for start in range(0, len(requirements), max_contexts)
    ]


def plan_associations(
    requirements: list[PlanningRequirement],
    max_contexts: int = MAXIMUM_CONTEXTS,
    transfer_costs: CostType | None = None,
    supported_transfer_syntaxes: "set[UIDInputType] | frozenset[UIDInputType] | list[UIDInputType] | None" = None,
) -> list[AssociationProposal]:
    """Plan the minimum number of association proposals for the requirements.

    The requirements are first validated so that every required combination
    is proposable (feasible transfer syntax and negotiable roles), then
    packed into the fewest associations that respect `max_contexts`. Within
    each requirement the transfer syntax with the lowest expected conversion
    cost is selected for cost accounting; all alternatives are still included
    in the proposed context because the *Acceptor* decides which one is
    accepted during negotiation.

    Parameters
    ----------
    requirements : list of PlanningRequirement
        The required SOP class combinations. Abstract syntaxes must be
        unique.
    max_contexts : int, optional
        The maximum number of presentation contexts per association
        (default 128, the DICOM limit).
    transfer_costs : dict, optional
        The expected conversion cost for each transfer syntax UID, as
        ``{transfer_syntax: cost}``. Missing entries default to ``0``.
    supported_transfer_syntaxes : list, set or frozenset, optional
        The transfer syntax UIDs known to be supported by the peer. When
        given, alternatives outside this set are excluded when selecting the
        expected syntax. When ``None`` (default) every alternative is
        considered proposable. This is a planning input only: acceptance is
        still determined by the actual negotiation.

    Returns
    -------
    list of AssociationProposal
        The proposals, ordered so the first proposal contains the first
        requirements. Every requirement appears in exactly one proposal.

    Raises
    ------
    AssociationPlanError
        If a requirement has no proposable transfer syntax or a role
        combination that is rejected under every negotiation outcome.
    ValueError
        For invalid inputs such as duplicate abstract syntaxes or an
        out-of-range `max_contexts`.
    """
    if not isinstance(max_contexts, int) or isinstance(max_contexts, bool):
        raise TypeError("'max_contexts' must be an int")

    if not 1 <= max_contexts <= MAXIMUM_CONTEXTS:
        raise ValueError(
            f"'max_contexts' must be between 1 and {MAXIMUM_CONTEXTS}, inclusive"
        )

    if not requirements:
        return []

    seen_abstract_syntaxes: set[UID] = set()
    for requirement in requirements:
        if not isinstance(requirement, PlanningRequirement):
            raise TypeError("'requirements' must contain PlanningRequirement instances")

        if requirement.abstract_syntax in seen_abstract_syntaxes:
            raise ValueError(
                "Duplicate requirement for abstract syntax "
                f"'{requirement.abstract_syntax}'"
            )

        seen_abstract_syntaxes.add(requirement.abstract_syntax)

        if not _role_proposal_is_feasible(
            requirement.roles, requirement.require_scu, requirement.require_scp
        ):
            raise AssociationPlanError(
                f"Role proposal (scu_role={requirement.scu_role}, "
                f"scp_role={requirement.scp_role}) for "
                f"'{requirement.abstract_syntax}' cannot satisfy the required "
                f"roles (require_scu={requirement.require_scu}, "
                f"require_scp={requirement.require_scp}) for any possible "
                "acceptor response"
            )

    costs = _normalise_costs(transfer_costs)
    supported: frozenset[UID] | None = None
    if supported_transfer_syntaxes is not None:
        supported = frozenset(_as_uid(syntax) for syntax in supported_transfer_syntaxes)

    unsatisfiable: list[PlanningRequirement] = []
    selected: dict[UID, UID] = {}
    for requirement in requirements:
        alternatives = requirement.transfer_syntaxes
        if supported is not None:
            alternatives = tuple(
                syntax for syntax in alternatives if syntax in supported
            )

        if not alternatives:
            unsatisfiable.append(requirement)
            continue

        selected[requirement.abstract_syntax] = min(
            alternatives, key=lambda syntax: costs.get(syntax, 0.0)
        )

    if unsatisfiable:
        labels = ", ".join(
            requirement.name or str(requirement.abstract_syntax)
            for requirement in unsatisfiable
        )
        raise AssociationPlanError(
            f"No proposable transfer syntax for the following requirements: {labels}"
        )

    packed = _pack_minimum(requirements, max_contexts)

    proposals: list[AssociationProposal] = []
    for proposal_index, bin_indices in enumerate(packed):
        proposal = AssociationProposal(
            index=proposal_index,
            requirements=[requirements[idx] for idx in bin_indices],
        )

        for context_id, requirement in zip(
            range(1, 2 * len(bin_indices) + 1, 2),
            proposal.requirements,
            strict=True,
        ):
            context = PresentationContext()
            context.context_id = context_id
            context.abstract_syntax = requirement.abstract_syntax
            context.transfer_syntax = list(requirement.transfer_syntaxes)
            proposal.contexts.append(context)

            syntax = selected[requirement.abstract_syntax]
            proposal.selected_transfer_syntax[requirement.abstract_syntax] = syntax
            proposal.expected_cost += costs.get(syntax, 0.0)

        proposals.append(proposal)

    return proposals
