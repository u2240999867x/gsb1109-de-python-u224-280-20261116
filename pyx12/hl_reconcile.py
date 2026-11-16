"""
Offline reconciliation of 837 HL parent references against implementation-guide loop placement.

The HL hierarchy in an 837 carries two independent statements of where a claim-side
node belongs: the HL02 parent id number (an intra-transaction reference) and the
implementation-guide loop the HL segment sits in (2000A/2000B/2000C, level codes
20/22/23). A submitted file can make those two statements disagree. This module
parses the stream without moving or rewriting any segment, lists every conflicting
attribution, and solves for the minimum-cost set of caller-authorized HL02
corrections that makes the hierarchy legal (level code, child code and loop
placement all satisfied at once).

HL id numbers are only meaningful inside the transaction that declares them: a
parent id is never resolved across an ST boundary, so a cross-transaction HL02
reference is reported as a contradiction rather than silently re-parented.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TextIO

import pyx12.map_if
import pyx12.map_index
import pyx12.params
import pyx12.x12file
from pyx12.map_if._element import element_if
from pyx12.map_if._loop import loop_if
from pyx12.map_if._segment import segment_if
from pyx12.segment import Segment

# HL03 hierarchical level codes used by the 837 claim transactions.
LEVEL_BILLING_PROVIDER = "20"
LEVEL_SUBSCRIBER = "22"
LEVEL_PATIENT = "23"
ROOT_LEVEL_CODES = frozenset({LEVEL_BILLING_PROVIDER})


@dataclass(slots=True, frozen=True)
class RawSegment:
    """One input segment, captured verbatim so the original stream is preserved."""

    line_number: int
    seg_id: str
    text: str


@dataclass(slots=True, frozen=True)
class HlDeclaration:
    """A single HL segment and the two attributions it carries."""

    raw: RawSegment
    hl_id: str
    parent_id: str | None
    level_code: str
    child_code: str | None
    guideline_loop_id: str
    guideline_parent_levels: frozenset[str]
    # Level code of the node HL02 currently points at, resolved within this ST only.
    hl_parent_level: str | None


@dataclass(slots=True, frozen=True)
class Conflict:
    """An HL whose HL02 attribution and guideline placement disagree."""

    st_control_number: str | None
    hl_id: str
    level_code: str
    line_number: int
    hl_parent_id: str | None
    hl_parent_level: str | None
    guideline_parent_levels: frozenset[str]
    reasons: tuple[str, ...]
    raw_text: str


@dataclass(slots=True, frozen=True)
class AllowedCorrection:
    """A caller-authorized HL02 replacement and its cost."""

    st_control_number: str
    hl_id: str
    new_parent: str | None
    cost: int


@dataclass(slots=True, frozen=True)
class Recommendation:
    """One HL02 change proposed by a minimum-cost solution."""

    solution_index: int
    st_control_number: str | None
    hl_id: str
    current_parent: str | None
    proposed_parent: str | None
    cost: int


@dataclass(slots=True, frozen=True)
class ParentAssignment:
    """The resolved parent of an HL in a concrete solution."""

    st_control_number: str | None
    hl_id: str
    parent_id: str | None


@dataclass(slots=True, frozen=True)
class PendingAssignment:
    """An HL whose parent differs across tied minimum-cost solutions."""

    st_control_number: str | None
    hl_id: str
    candidate_parents: tuple[str | None, ...]


@dataclass(slots=True, frozen=True)
class Contradiction:
    """A declaration that cannot be made legal with the authorized corrections."""

    st_control_number: str | None
    hl_id: str
    line_number: int
    reasons: tuple[str, ...]
    raw_text: str


@dataclass(slots=True, frozen=True)
class TransactionReport:
    st_control_number: str | None
    map_file: str
    declarations: tuple[HlDeclaration, ...]
    conflicts: tuple[Conflict, ...]
    recommendations: tuple[Recommendation, ...]
    contradictions: tuple[Contradiction, ...]
    segments: tuple[RawSegment, ...]


@dataclass(slots=True, frozen=True)
class ReconcileReport:
    transactions: tuple[TransactionReport, ...]
    conflicts: tuple[Conflict, ...]
    recommendations: tuple[Recommendation, ...]
    auto_confirmed: tuple[ParentAssignment, ...]
    pending: tuple[PendingAssignment, ...]
    contradictions: tuple[Contradiction, ...]
    solution_count: int
    total_cost: int

    @property
    def solvable(self) -> bool:
        return not self.contradictions


@dataclass(slots=True, frozen=True)
class _LoopSpec:
    level_code: str
    loop_id: str
    parent_levels: frozenset[str]


@dataclass(slots=True)
class _StBuilder:
    control_number: str | None
    map_file: str
    segments: list[RawSegment] = field(default_factory=list)
    hl_raw: list[RawSegment] = field(default_factory=list)
    hl_data: list[Segment] = field(default_factory=list)


def _hl03_code(seg_node: segment_if | None) -> str | None:
    if seg_node is None or seg_node.id != "HL" or len(seg_node.children) < 3:
        return None
    hl03 = seg_node.children[2]
    if not isinstance(hl03, element_if) or not hl03.valid_codes:
        return None
    return hl03.valid_codes[0]


def _loop_hl_level(loop_node: loop_if) -> str | None:
    seg_node = loop_node.get_first_seg()
    if isinstance(seg_node, segment_if) and seg_node.id == "HL":
        return _hl03_code(seg_node)
    return None


def _nearest_hl_ancestor(loop_node: loop_if) -> loop_if | None:
    node = loop_node.parent
    while isinstance(node, loop_if):
        if _loop_hl_level(node) is not None:
            return node
        node = node.parent
    return None


def _build_loop_specs(
    map_root: pyx12.map_if.map_if,
) -> tuple[dict[str, _LoopSpec], dict[str, str]]:
    """Derive level -> allowed parent levels from the guide's nested 2000 loops."""
    specs: dict[str, _LoopSpec] = {}
    loop_by_id: dict[str, str] = {}
    for node in map_root.loop_segment_iterator():
        if not isinstance(node, segment_if) or node.id != "HL":
            continue
        level = _hl03_code(node)
        parent_loop = node.parent
        if level is None or not isinstance(parent_loop, loop_if):
            continue
        loop_by_id[parent_loop.id or ""] = level
        ancestor = _nearest_hl_ancestor(parent_loop)
        parent_levels: set[str] = set()
        if ancestor is not None and isinstance(ancestor.parent, loop_if):
            for sibling in ancestor.parent.childIterator():
                if not isinstance(sibling, loop_if):
                    continue
                sibling_level = _loop_hl_level(sibling)
                if sibling_level is not None:
                    parent_levels.add(sibling_level)
        specs[level] = _LoopSpec(level, parent_loop.id or "", frozenset(parent_levels))
    return specs, loop_by_id


def _has_cycle(hl_id: str, assignments: dict[str, str | None]) -> bool:
    seen: set[str] = set()
    current = assignments.get(hl_id)
    while current is not None:
        if current in seen:
            return True
        seen.add(current)
        current = assignments.get(current)
    return False


def _legality_reasons(
    declaration: HlDeclaration,
    proposed_parent: str | None,
    assignments: dict[str, str | None],
    declarations_by_id: dict[str, HlDeclaration],
    specs: dict[str, _LoopSpec],
) -> tuple[str, ...]:
    reasons: list[str] = []
    spec = specs.get(declaration.level_code)
    if spec is None:
        reasons.append("level_not_in_guideline")
        return tuple(reasons)

    if declaration.level_code in ROOT_LEVEL_CODES:
        if proposed_parent is not None:
            reasons.append("root_with_parent")
    elif proposed_parent is None:
        reasons.append("nonroot_without_parent")
    elif proposed_parent == declaration.hl_id:
        reasons.append("parent_is_self")
    elif proposed_parent not in declarations_by_id:
        # HL numbers resolve only inside the owning ST; a missing id means the
        # reference dangles or points into another transaction.
        reasons.append("parent_not_in_transaction")
    else:
        parent_level = declarations_by_id[proposed_parent].level_code
        if parent_level not in spec.parent_levels:
            reasons.append("parent_level_not_allowed")

    if _has_cycle(declaration.hl_id, assignments):
        reasons.append("cycle")

    has_children = any(
        other.hl_id != declaration.hl_id and assignments.get(other.hl_id) == declaration.hl_id
        for other in declarations_by_id.values()
    )
    if declaration.child_code not in ("0", "1"):
        reasons.append("child_code_invalid")
    elif (declaration.child_code == "1") != has_children:
        reasons.append("child_code_contradiction")
    return tuple(reasons)


def _conflict(
    st_control_number: str | None,
    declaration: HlDeclaration,
    reasons: tuple[str, ...],
) -> Conflict:
    return Conflict(
        st_control_number=st_control_number,
        hl_id=declaration.hl_id,
        level_code=declaration.level_code,
        line_number=declaration.raw.line_number,
        hl_parent_id=declaration.parent_id,
        hl_parent_level=declaration.hl_parent_level,
        guideline_parent_levels=declaration.guideline_parent_levels,
        reasons=reasons,
        raw_text=declaration.raw.text,
    )


def _solve_transaction(
    st_control_number: str | None,
    declarations: tuple[HlDeclaration, ...],
    specs: dict[str, _LoopSpec],
    allowed: list[AllowedCorrection],
) -> tuple[
    list[list[ParentAssignment]],
    list[Recommendation],
    tuple[Conflict, ...],
    tuple[Contradiction, ...],
    int,
]:
    declarations_by_id = {d.hl_id: d for d in declarations}
    initial_assignments = {d.hl_id: d.parent_id for d in declarations}
    conflicts: list[Conflict] = []
    conflicted_ids: set[str] = set()
    for declaration in declarations:
        reasons = _legality_reasons(
            declaration,
            declaration.parent_id,
            initial_assignments,
            declarations_by_id,
            specs,
        )
        if reasons:
            conflicts.append(_conflict(st_control_number, declaration, reasons))
            conflicted_ids.add(declaration.hl_id)

    # Structural faults that no HL02 rewrite can remove: HL01 must identify the
    # node uniquely within the ST or parent references are ambiguous.
    id_counts = Counter(d.hl_id for d in declarations)
    structural: list[Contradiction] = []
    for declaration in declarations:
        structural_reasons: list[str] = []
        if not declaration.hl_id:
            structural_reasons.append("hl_id_missing")
        elif id_counts[declaration.hl_id] > 1:
            structural_reasons.append("hl_id_not_unique")
        if structural_reasons:
            structural.append(
                Contradiction(
                    st_control_number=st_control_number,
                    hl_id=declaration.hl_id,
                    line_number=declaration.raw.line_number,
                    reasons=tuple(structural_reasons),
                    raw_text=declaration.raw.text,
                )
            )
    if structural:
        return [], [], tuple(conflicts), tuple(structural), 0

    # Every declaration keeps its current parent at zero cost; authorized
    # corrections add branches. Nodes without corrections stay single-choice,
    # so the search only branches on the caller's actual worklist. A node that
    # is initially legal may still need a re-parent to restore another node's
    # child-code declaration, so corrections are not restricted to conflicts.
    options: dict[str, list[tuple[str | None, int]]] = {}
    for declaration in declarations:
        choices: list[tuple[str | None, int]] = [(declaration.parent_id, 0)]
        for correction in allowed:
            if (
                correction.st_control_number != st_control_number
                or correction.hl_id != declaration.hl_id
                or correction.new_parent == declaration.parent_id
            ):
                continue
            if not any(candidate[0] == correction.new_parent for candidate in choices):
                choices.append((correction.new_parent, correction.cost))
        options[declaration.hl_id] = choices

    best_cost: int | None = None
    best_solutions: list[dict[str, str | None]] = []

    def search(index: int, assignments: dict[str, str | None], cost: int) -> None:
        nonlocal best_cost, best_solutions
        if best_cost is not None and cost > best_cost:
            return
        if index == len(declarations):
            if all(
                not _legality_reasons(
                    declaration,
                    assignments[declaration.hl_id],
                    assignments,
                    declarations_by_id,
                    specs,
                )
                for declaration in declarations
            ):
                if best_cost is None or cost < best_cost:
                    best_cost = cost
                    best_solutions = [dict(assignments)]
                elif cost == best_cost:
                    best_solutions.append(dict(assignments))
            return
        declaration = declarations[index]
        for parent, correction_cost in options[declaration.hl_id]:
            assignments[declaration.hl_id] = parent
            search(index + 1, assignments, cost + correction_cost)

    search(0, dict(initial_assignments), 0)

    recommendations: list[Recommendation] = []
    solution_assignments: list[list[ParentAssignment]] = []
    contradictions: list[Contradiction] = []
    if best_solutions:
        for solution_index, solution in enumerate(best_solutions):
            solution_assignments.append(
                [
                    ParentAssignment(st_control_number, d.hl_id, solution[d.hl_id])
                    for d in declarations
                ]
            )
            for declaration in declarations:
                proposed = solution[declaration.hl_id]
                if proposed != declaration.parent_id:
                    correction_cost = next(
                        c.cost
                        for c in allowed
                        if c.st_control_number == st_control_number
                        and c.hl_id == declaration.hl_id
                        and c.new_parent == proposed
                    )
                    recommendations.append(
                        Recommendation(
                            solution_index=solution_index,
                            st_control_number=st_control_number,
                            hl_id=declaration.hl_id,
                            current_parent=declaration.parent_id,
                            proposed_parent=proposed,
                            cost=correction_cost,
                        )
                    )
    else:
        # No assignment of the authorized parents is legal. Point at the
        # declarations that were already in conflict and why every option fails,
        # rather than quietly moving segments or editing business values.
        for declaration in declarations:
            if declaration.hl_id not in conflicted_ids:
                continue
            unresolved: list[str] = []
            for parent, _cost in options[declaration.hl_id]:
                trial = dict(initial_assignments)
                trial[declaration.hl_id] = parent
                unresolved.extend(
                    _legality_reasons(declaration, parent, trial, declarations_by_id, specs)
                )
            contradictions.append(
                Contradiction(
                    st_control_number=st_control_number,
                    hl_id=declaration.hl_id,
                    line_number=declaration.raw.line_number,
                    reasons=tuple(dict.fromkeys(unresolved)),
                    raw_text=declaration.raw.text,
                )
            )
    return (
        solution_assignments,
        recommendations,
        tuple(conflicts),
        tuple(contradictions),
        best_cost or 0,
    )


def _declarations_for_builder(
    builder: _StBuilder,
    specs: dict[str, _LoopSpec],
    loop_by_id: dict[str, str],
) -> tuple[HlDeclaration, ...]:
    ids_in_st: dict[str, str] = {}
    for seg in builder.hl_data:
        hl_id = seg.get_value("HL01") or ""
        level = seg.get_value("HL03") or ""
        if hl_id:
            ids_in_st[hl_id] = level

    declarations: list[HlDeclaration] = []
    for raw, seg in zip(builder.hl_raw, builder.hl_data):
        hl_id = seg.get_value("HL01") or ""
        parent_value = seg.get_value("HL02")
        parent_id = parent_value if parent_value else None
        level = seg.get_value("HL03") or ""
        child_code = seg.get_value("HL04")
        spec = specs.get(level)
        guideline_loop = spec.loop_id if spec else loop_by_id.get(level, "")
        guideline_parents = spec.parent_levels if spec else frozenset()
        hl_parent_level = ids_in_st.get(parent_id) if parent_id is not None else None
        declarations.append(
            HlDeclaration(
                raw=raw,
                hl_id=hl_id,
                parent_id=parent_id,
                level_code=level,
                child_code=child_code,
                guideline_loop_id=guideline_loop,
                guideline_parent_levels=guideline_parents,
                hl_parent_level=hl_parent_level,
            )
        )
    return tuple(declarations)


def reconcile(
    src_file: str | TextIO,
    param: pyx12.params.ParamsBase,
    allowed_corrections: list[AllowedCorrection] | None = None,
    map_path: str | None = None,
) -> ReconcileReport:
    """Parse an X12 stream and reconcile 837 HL attributions without altering it."""
    allowed_corrections = allowed_corrections or []
    src = pyx12.x12file.X12Reader(src_file)
    map_index_if = pyx12.map_index.map_index(map_path)
    map_cache: dict[str, pyx12.map_if.map_if] = {}
    icvn: str | None = None
    fic: str | None = None
    vriic: str | None = None
    cur_map: pyx12.map_if.map_if | None = None
    cur_map_file: str | None = None
    builders: list[_StBuilder] = []
    current: _StBuilder | None = None

    try:
        for seg in src:
            seg_id = seg.get_seg_id()
            raw = RawSegment(src.get_cur_line(), seg_id or "", seg.format())
            if seg_id == "ISA":
                icvn = seg.get_value("ISA12")
            elif seg_id == "GS":
                fic = seg.get_value("GS01")
                vriic = seg.get_value("GS08")
                map_file_new = map_index_if.get_filename(icvn, vriic, fic)
                if map_file_new and map_file_new != cur_map_file:
                    cur_map_file = map_file_new
                    cur_map = map_cache.get(map_file_new)
                    if cur_map is None:
                        cur_map = pyx12.map_if.load_map_file(map_file_new, param, map_path)
                        map_cache[map_file_new] = cur_map
            elif seg_id == "ST":
                current = _StBuilder(seg.get_value("ST02"), cur_map_file or "")
                builders.append(current)
            elif seg_id == "SE":
                current = None
            elif current is not None and cur_map is not None and cur_map.id == "837":
                current.segments.append(raw)
                if seg_id == "HL":
                    current.hl_raw.append(raw)
                    current.hl_data.append(seg)
    finally:
        src.close()

    transaction_reports: list[TransactionReport] = []
    all_conflicts: list[Conflict] = []
    all_recommendations: list[Recommendation] = []
    all_auto_confirmed: list[ParentAssignment] = []
    all_pending: list[PendingAssignment] = []
    all_contradictions: list[Contradiction] = []
    total_cost = 0
    total_solutions = 1

    for builder in builders:
        if not builder.hl_raw or builder.map_file == "":
            continue
        map_root = map_cache.get(builder.map_file)
        if map_root is None:
            map_root = pyx12.map_if.load_map_file(builder.map_file, param, map_path)
            map_cache[builder.map_file] = map_root
        specs, loop_by_id = _build_loop_specs(map_root)
        declarations = _declarations_for_builder(builder, specs, loop_by_id)
        solutions, recommendations, conflicts, contradictions, best_cost = _solve_transaction(
            builder.control_number, declarations, specs, allowed_corrections
        )

        auto_confirmed: list[ParentAssignment] = []
        pending: list[PendingAssignment] = []
        if solutions:
            total_solutions *= len(solutions)
            total_cost += best_cost
            for index, declaration in enumerate(declarations):
                parents = tuple(solution[index].parent_id for solution in solutions)
                if len(set(parents)) == 1:
                    auto_confirmed.append(
                        ParentAssignment(builder.control_number, declaration.hl_id, parents[0])
                    )
                else:
                    pending.append(
                        PendingAssignment(
                            builder.control_number,
                            declaration.hl_id,
                            tuple(dict.fromkeys(parents)),
                        )
                    )
        else:
            total_solutions = 0

        transaction_reports.append(
            TransactionReport(
                st_control_number=builder.control_number,
                map_file=builder.map_file,
                declarations=declarations,
                conflicts=conflicts,
                recommendations=tuple(recommendations),
                contradictions=contradictions,
                segments=tuple(builder.segments),
            )
        )
        all_conflicts.extend(conflicts)
        all_recommendations.extend(recommendations)
        all_auto_confirmed.extend(auto_confirmed)
        all_pending.extend(pending)
        all_contradictions.extend(contradictions)

    return ReconcileReport(
        transactions=tuple(transaction_reports),
        conflicts=tuple(all_conflicts),
        recommendations=tuple(all_recommendations),
        auto_confirmed=tuple(all_auto_confirmed),
        pending=tuple(all_pending),
        contradictions=tuple(all_contradictions),
        solution_count=total_solutions,
        total_cost=total_cost,
    )
