"""Compute the minimal provably-safe rebuild set after a data correction.

Given two versions of the load results, explicit component dependencies
and the dependency declarations of the construction rules, the planner

1. diffs the two loads order-insensitively (row reordering never fires),
2. classifies each (component, changed table) pair into one of four
   impact kinds — value-only (仅改值), membership (改变成员),
   default-triggering (触发默认变化) and construction-scope
   (改变构造范围) — and maps them to the cheapest safe action,
3. propagates reconstructions along upstream edges, and
4. keeps the full verification scope for components whose rule
   dependency declarations cannot be proven complete.

Safety argument: a component is left untouched only if (a) its
declaration is provably complete, (b) none of its declared inputs —
value reads, index-selection reads, index-domain table, upstream
components — differs between the two loads, and (c) no upstream is
rebuilt. By (a) its from-scratch construction on the new data is
identical to the old instance, so keeping the old instance is provably
safe; everything else is rebuilt, and nothing more — the set is minimal
with respect to the declared dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, Mapping, Optional, Tuple

from .data import Cell
from .deps import ComponentDecl
from .diff import TableChangeKind, TableDiff, diff_load_results


class ImpactKind(Enum):
    VALUE_ONLY = "value_only"  # 仅改值
    MEMBERSHIP = "membership"  # 改变成员
    DEFAULT_TRIGGERING = "default_triggering"  # 触发默认变化
    CONSTRUCTION_SCOPE = "construction_scope"  # 改变构造范围


class Action(Enum):
    REVALUE = "revalue"  # assign new values onto the old instance
    RECONSTRUCT = "reconstruct"  # delete and rebuild from the new data
    VERIFY_FULL = "verify_full"  # full verification scope retained


@dataclass(frozen=True)
class KeyTrace:
    """One differing row with both versions' cells (provenance included)."""

    key: tuple
    old: Optional[Cell]
    new: Optional[Cell]


@dataclass(frozen=True)
class Impact:
    """A classified change of one table as seen by one component."""

    component: str
    table: str
    kind: ImpactKind
    traces: Tuple[KeyTrace, ...]

    @property
    def sources(self) -> tuple:
        """Source observations behind this impact, item by item."""
        out = []
        for trace in self.traces:
            for cell in (trace.old, trace.new):
                if cell is not None:
                    out.append(cell.source)
        return tuple(out)


@dataclass(frozen=True)
class RebuildPlan:
    """The minimal provably-safe rebuild set plus its justification."""

    diffs: Mapping[str, TableDiff]
    impacts: Mapping[str, Tuple[Impact, ...]]
    actions: Mapping[str, Action]
    reasons: Mapping[str, Tuple[str, ...]]
    unaffected: Mapping[str, str]

    @property
    def is_noop(self) -> bool:
        return not self.actions

    @property
    def revalue(self) -> frozenset:
        return frozenset(n for n, a in self.actions.items() if a is Action.REVALUE)

    @property
    def reconstruct(self) -> frozenset:
        return frozenset(n for n, a in self.actions.items() if a is Action.RECONSTRUCT)

    @property
    def verify_full(self) -> frozenset:
        return frozenset(n for n, a in self.actions.items() if a is Action.VERIFY_FULL)

    def explain(self, name: str) -> Tuple[str, ...]:
        """Why a component is in the plan (empty if it is unaffected)."""
        return self.reasons.get(name, ())

    def affected_sources(self) -> tuple:
        """All original observation sources driving this plan."""
        seen = []
        for impacts in self.impacts.values():
            for impact in impacts:
                for source in impact.sources:
                    if source not in seen:
                        seen.append(source)
        return tuple(seen)


_IMPACT_ACTION = {
    ImpactKind.VALUE_ONLY: Action.REVALUE,
    ImpactKind.DEFAULT_TRIGGERING: Action.REVALUE,
    ImpactKind.MEMBERSHIP: Action.RECONSTRUCT,
    ImpactKind.CONSTRUCTION_SCOPE: Action.RECONSTRUCT,
}

_ACTION_RANK = {None: 0, Action.REVALUE: 1, Action.RECONSTRUCT: 2, Action.VERIFY_FULL: 3}


def _classify_impact(decl: ComponentDecl, table: str, diff: TableDiff) -> Optional[ImpactKind]:
    if table in decl.index_reads:
        # The table feeds index selection inside the construction rule:
        # even a value-only change may alter the constructed domain.
        return ImpactKind.CONSTRUCTION_SCOPE
    if table == decl.index_from:
        if diff.kind is TableChangeKind.MEMBERSHIP:
            # The index domain itself is derived from these keys.
            return ImpactKind.CONSTRUCTION_SCOPE
        if table not in decl.value_reads:
            # Domain keyed by this table is unchanged; values are not read.
            return None
    if table in decl.value_reads:
        if diff.kind is TableChangeKind.VALUE_ONLY:
            return ImpactKind.VALUE_ONLY
        if decl.has_default:
            # Keys appear/disappear but the domain is fixed: only the
            # default coverage shifts (default (de)activates per key).
            return ImpactKind.DEFAULT_TRIGGERING
        return ImpactKind.MEMBERSHIP
    return None


def plan_rebuild(
    old, new, decls: Iterable[ComponentDecl]
) -> RebuildPlan:
    """Compute the minimal provably-safe rebuild set for *new* vs *old*."""
    decl_map: Dict[str, ComponentDecl] = {d.name: d for d in decls}
    diffs = diff_load_results(old, new)
    impacts: Dict[str, list] = {name: [] for name in decl_map}
    actions: Dict[str, Action] = {}
    reasons: Dict[str, list] = {name: [] for name in decl_map}

    for name, decl in decl_map.items():
        for table, diff in diffs.items():
            kind = _classify_impact(decl, table, diff)
            if kind is None:
                continue
            impacts[name].append(
                Impact(
                    component=name,
                    table=table,
                    kind=kind,
                    traces=tuple(KeyTrace(k, o, n) for k, o, n in diff.traces()),
                )
            )
        if impacts[name]:
            actions[name] = max(
                (_IMPACT_ACTION[i.kind] for i in impacts[name]),
                key=_ACTION_RANK.__getitem__,
            )
            reasons[name].extend(
                f"{i.kind.value} change in table '{i.table}'" for i in impacts[name]
            )
        if not decl.complete and diffs:
            # Rule dependencies cannot be proven complete: no input is
            # provably irrelevant, so the full verification scope is kept.
            actions[name] = Action.VERIFY_FULL
            reasons[name].append(
                "dependency declaration not provably complete; "
                "full verification scope retained"
            )

    _propagate(decl_map, actions, reasons)

    unaffected = {
        name: (
            "complete declaration, no changed declared input, no rebuilt "
            "upstream: from-scratch construction on the new data is identical"
        )
        for name in decl_map
        if name not in actions
    }
    return RebuildPlan(
        diffs=diffs,
        impacts={k: tuple(v) for k, v in impacts.items() if v},
        actions=actions,
        reasons={k: tuple(v) for k, v in reasons.items() if v},
        unaffected=unaffected,
    )


def _propagate(decl_map, actions, reasons) -> None:
    """Propagate rebuilds along upstream edges to a fixed point.

    A reconstructed upstream yields new component objects (and possibly a
    new domain), so every downstream component must be rebuilt as well. A
    revalued upstream keeps its identity and domain — Pyomo expressions
    hold symbolic references — so it only forces a rebuild of components
    that use it for index selection.
    """
    changed = True
    while changed:
        changed = False
        for name, decl in decl_map.items():
            for up in decl.upstream | decl.index_upstream:
                up_action = actions.get(up)
                if up_action is None:
                    continue
                if up_action in (Action.RECONSTRUCT, Action.VERIFY_FULL):
                    need = Action.RECONSTRUCT if decl.complete else Action.VERIFY_FULL
                    why = f"upstream component '{up}' is rebuilt"
                elif up_action is Action.REVALUE and up in decl.index_upstream:
                    need = Action.RECONSTRUCT if decl.complete else Action.VERIFY_FULL
                    why = (
                        f"upstream component '{up}' participates in index "
                        "selection and is revalued"
                    )
                else:
                    continue
                if _ACTION_RANK[need] > _ACTION_RANK[actions.get(name)]:
                    actions[name] = need
                    reasons[name].append(why)
                    changed = True
