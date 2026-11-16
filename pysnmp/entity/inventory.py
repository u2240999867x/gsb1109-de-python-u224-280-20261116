"""Build a structured device inventory from ENTITY-MIB and IF-MIB walk data.

ENTITY-MIB describes the physical hierarchy of a device (chassis, cards,
ports) through ``entPhysicalTable`` and links physical components to logical
interfaces through ``entAliasMappingTable``.  IF-MIB describes the interfaces
themselves through ``ifTable``.

The two index spaces are deliberately kept separate:

* ``entPhysicalIndex`` identifies a row of ``entPhysicalTable``;
* ``ifIndex`` identifies a row of ``ifTable``.

Equal numeric values in the two spaces carry no meaning on their own.  A
physical component is associated with an interface only when an
``entAliasMappingIdentifier`` variable binding points at that interface row.
Containment comes exclusively from ``entPhysicalContainedIn``.

The builder never raises on a single bad branch: malformed containment,
orphaned components, containment cycles and aliases pointing at missing
interfaces are all reported as structured issues while every component that
can still be confirmed remains in the inventory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Sequence


# IF-MIB::ifEntry = 1.3.6.1.2.1.2.2.1; an alias identifier is normally
# ifEntry.ifIndex.<ifIndex>, i.e. ifEntry.1.<ifIndex>.
IF_ENTRY_OID = (1, 3, 6, 1, 2, 1, 2, 2, 1)


class ComponentClass(str, Enum):
    """Physical component classes from ENTITY-MIB ``entPhysicalClass``."""

    CHASSIS = "chassis"
    MODULE = "module"
    PORT = "port"
    OTHER = "other"


class SerialEvidence(str, Enum):
    """How much evidence supports the serial number recorded for a component."""

    CONFIRMED = "confirmed"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    NOT_APPLICABLE = "not_applicable"


class IssueCode(str, Enum):
    """Inventory validation issue codes."""

    DUPLICATE_PHYSICAL_INDEX = "duplicate_physical_index"
    DUPLICATE_INTERFACE_INDEX = "duplicate_interface_index"
    ORPHAN_COMPONENT = "orphan_component"
    UNKNOWN_PARENT = "unknown_parent"
    CONTAINMENT_CYCLE = "containment_cycle"
    DANGLING_ALIAS_PHYSICAL = "dangling_alias_physical"
    ALIAS_TO_NON_IF_ENTRY = "alias_to_non_if_entry"
    MISSING_INTERFACE = "missing_interface"
    SERIAL_INSUFFICIENT_EVIDENCE = "serial_insufficient_evidence"
    REUSED_INDEX_NEW_IDENTITY = "reused_index_new_identity"
    HISTORICAL_SERIAL_NOT_CARRIED = "historical_serial_not_carried"


_CLASS_ALIASES = {
    "chassis": ComponentClass.CHASSIS,
    "module": ComponentClass.MODULE,
    "port": ComponentClass.PORT,
    "container": ComponentClass.OTHER,
    "stack": ComponentClass.OTHER,
    "powersupply": ComponentClass.OTHER,
    "fan": ComponentClass.OTHER,
    "sensor": ComponentClass.OTHER,
    "backplane": ComponentClass.OTHER,
    "cpu": ComponentClass.OTHER,
    "other": ComponentClass.OTHER,
    "unknown": ComponentClass.OTHER,
}


@dataclass(frozen=True)
class InventoryIssue:
    """A single inventory anomaly.

    A issue always carries a machine-readable ``code``.  ``physical_index``
    and ``interface_index`` are set when the issue is scoped to a component
    or interface; ``related_indices`` captures extra context such as the
    members of a containment cycle.
    """

    code: IssueCode
    message: str
    physical_index: int | None = None
    interface_index: int | None = None
    related_indices: tuple[int, ...] = ()

    def to_dict(self) -> dict:
        """Return a plain, JSON-serializable representation."""
        return {
            "code": self.code.value,
            "message": self.message,
            "physical_index": self.physical_index,
            "interface_index": self.interface_index,
            "related_indices": list(self.related_indices),
        }


@dataclass(frozen=True)
class Interface:
    """An IF-MIB interface row (``ifEntry``)."""

    if_index: int
    name: str | None = None
    description: str | None = None


@dataclass
class Component:
    """A physical ENTITY-MIB component (``entPhysicalEntry``)."""

    physical_index: int
    component_class: ComponentClass
    name: str | None
    description: str | None
    parent_index: int | None
    is_replaceable: bool
    serial_number: str | None
    serial_evidence: SerialEvidence
    children: list[int] = field(default_factory=list)
    interface_indices: list[int] = field(default_factory=list)
    unresolved_interface_indices: list[int] = field(default_factory=list)
    chassis_index: int | None = None
    is_orphan: bool = False
    parent_missing: bool = False
    in_cycle: bool = False
    issue_codes: list[IssueCode] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Return a plain, JSON-serializable representation."""
        return {
            "physical_index": self.physical_index,
            "component_class": self.component_class.value,
            "name": self.name,
            "description": self.description,
            "parent_index": self.parent_index,
            "chassis_index": self.chassis_index,
            "is_replaceable": self.is_replaceable,
            "serial_number": self.serial_number,
            "serial_evidence": self.serial_evidence.value,
            "children": list(self.children),
            "interface_indices": list(self.interface_indices),
            "unresolved_interface_indices": list(
                self.unresolved_interface_indices
            ),
            "is_orphan": self.is_orphan,
            "parent_missing": self.parent_missing,
            "in_cycle": self.in_cycle,
            "issues": [code.value for code in self.issue_codes],
        }


@dataclass(frozen=True)
class HistoricalComponent:
    """A component record from a previous inventory snapshot.

    Only replaceable units carry identity across snapshots, and only through
    a confirmed serial number.  ``serial_number=None`` means the historical
    snapshot itself lacked sufficient evidence and therefore makes no claim.
    """

    physical_index: int
    component_class: ComponentClass
    serial_number: str | None = None


@dataclass
class DeviceInventory:
    """The structured result of joining ENTITY-MIB and IF-MIB walks."""

    components: Mapping[int, Component]
    interfaces: Mapping[int, Interface]
    issues: Sequence[InventoryIssue]
    roots: tuple[int, ...]

    def interfaces_for(self, physical_index: int) -> tuple[Interface, ...]:
        """Return the confirmed interfaces mapped to a component."""
        component = self.components[physical_index]
        return tuple(
            self.interfaces[if_index]
            for if_index in component.interface_indices
        )

    def issues_for(self, physical_index: int) -> tuple[InventoryIssue, ...]:
        """Return the issues scoped to a component."""
        return tuple(
            issue
            for issue in self.issues
            if issue.physical_index == physical_index
        )

    def to_listing(self) -> list[dict]:
        """Return the inventory as a list of plain, serializable rows.

        Rows are ordered by ``entPhysicalIndex`` and each row carries its
        containment path, serial evidence state, mapped interfaces and
        component-scoped issues.
        """
        rows = []
        for physical_index in sorted(self.components):
            component = self.components[physical_index]
            row = component.to_dict()
            row["containment_path"] = _containment_path(self.components, component)
            row["component_issues"] = [
                issue.to_dict()
                for issue in self.issues_for(physical_index)
            ]
            rows.append(row)
        return rows


def _coerce_class(value: object) -> ComponentClass:
    if isinstance(value, ComponentClass):
        return value
    if value is None:
        return ComponentClass.OTHER
    text = str(value).strip().lower().replace("-", "").replace("_", "")
    return _CLASS_ALIASES.get(text, ComponentClass.OTHER)


def _normalise_serial(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def interface_index_from_identifier(identifier: Sequence[int]) -> int | None:
    """Extract an ``ifIndex`` from an ``entAliasMappingIdentifier`` OID.

    Returns ``None`` when the identifier does not point at an IF-MIB
    ``ifEntry`` row rather than guessing from a trailing arc.
    """
    oid = tuple(identifier)
    if len(oid) == len(IF_ENTRY_OID) + 2 and oid[: len(IF_ENTRY_OID)] == IF_ENTRY_OID:
        return oid[-1]
    return None


def _containment_path(
    components: Mapping[int, Component], component: Component
) -> list[int]:
    path = [component.physical_index]
    seen = {component.physical_index}
    current = component
    while current.parent_index is not None and current.parent_index in components:
        parent_index = current.parent_index
        if parent_index in seen:
            break
        path.append(parent_index)
        seen.add(parent_index)
        current = components[parent_index]
    path.reverse()
    return path


def _build_components(
    physical_rows: Sequence[Mapping[str, object]],
    issues: list[InventoryIssue],
) -> dict[int, Component]:
    components: dict[int, Component] = {}
    for row in physical_rows:
        physical_index = row["physical_index"]
        if physical_index in components:
            issues.append(
                InventoryIssue(
                    IssueCode.DUPLICATE_PHYSICAL_INDEX,
                    "duplicate entPhysicalIndex %r; first row kept"
                    % (physical_index,),
                    physical_index=physical_index,
                )
            )
            continue
        component_class = _coerce_class(row.get("component_class"))
        contained_in = row.get("contained_in")
        parent_index = None if contained_in in (None, 0) else contained_in
        is_replaceable = bool(
            row.get(
                "is_replaceable",
                component_class == ComponentClass.MODULE,
            )
        )
        serial_number = _normalise_serial(row.get("serial_number"))
        if is_replaceable:
            serial_evidence = (
                SerialEvidence.CONFIRMED
                if serial_number is not None
                else SerialEvidence.INSUFFICIENT_EVIDENCE
            )
        else:
            serial_evidence = SerialEvidence.NOT_APPLICABLE
        components[physical_index] = Component(
            physical_index=physical_index,
            component_class=component_class,
            name=row.get("name"),
            description=row.get("description"),
            parent_index=parent_index,
            is_replaceable=is_replaceable,
            serial_number=serial_number,
            serial_evidence=serial_evidence,
        )
    return components


def _build_interfaces(
    interface_rows: Sequence[Mapping[str, object]],
    issues: list[InventoryIssue],
) -> dict[int, Interface]:
    interfaces: dict[int, Interface] = {}
    for row in interface_rows:
        if_index = row["if_index"]
        if if_index in interfaces:
            issues.append(
                InventoryIssue(
                    IssueCode.DUPLICATE_INTERFACE_INDEX,
                    "duplicate ifIndex %r; first row kept" % (if_index,),
                    interface_index=if_index,
                )
            )
            continue
        interfaces[if_index] = Interface(
            if_index=if_index,
            name=row.get("name"),
            description=row.get("description"),
        )
    return interfaces


def _link_containment(
    components: dict[int, Component], issues: list[InventoryIssue]
) -> list[int]:
    roots: list[int] = []
    for component in components.values():
        parent_index = component.parent_index
        if parent_index is None:
            roots.append(component.physical_index)
            if component.component_class != ComponentClass.CHASSIS:
                component.is_orphan = True
                component.issue_codes.append(IssueCode.ORPHAN_COMPONENT)
                issues.append(
                    InventoryIssue(
                        IssueCode.ORPHAN_COMPONENT,
                        "component %r has no containing chassis"
                        % (component.physical_index,),
                        physical_index=component.physical_index,
                    )
                )
            continue
        parent = components.get(parent_index)
        if parent is None:
            component.parent_missing = True
            component.is_orphan = True
            component.issue_codes.append(IssueCode.UNKNOWN_PARENT)
            issues.append(
                InventoryIssue(
                    IssueCode.UNKNOWN_PARENT,
                    "component %r references missing parent %r"
                    % (component.physical_index, parent_index),
                    physical_index=component.physical_index,
                    related_indices=(parent_index,),
                )
            )
            roots.append(component.physical_index)
            continue
        parent.children.append(component.physical_index)

    _mark_cycles(components, issues)
    _assign_chassis(components)
    return sorted(roots)


def _mark_cycles(
    components: Mapping[int, Component], issues: list[InventoryIssue]
) -> None:
    # Iterative DFS with white/grey/black coloring so a bad branch cannot
    # recurse forever or abort processing of the remaining components.
    WHITE, GREY, BLACK = 0, 1, 2
    color = {index: WHITE for index in components}
    reported_cycles: set[frozenset[int]] = set()

    def report_cycle(cycle: list[int]) -> None:
        member_set = frozenset(cycle[:-1])
        if member_set in reported_cycles:
            return
        reported_cycles.add(member_set)
        for member in cycle[:-1]:
            cycled = components[member]
            if not cycled.in_cycle:
                cycled.in_cycle = True
                cycled.issue_codes.append(IssueCode.CONTAINMENT_CYCLE)
        issues.append(
            InventoryIssue(
                IssueCode.CONTAINMENT_CYCLE,
                "containment cycle detected: %s"
                % " -> ".join(str(index) for index in cycle),
                physical_index=cycle[0],
                related_indices=tuple(cycle[:-1]),
            )
        )

    for index, component in components.items():
        if index in component.children:
            report_cycle([index, index])

    for start in sorted(components):
        if color[start] != WHITE:
            continue
        stack: list[tuple[int, int]] = [(start, 0)]
        path: list[int] = []
        while stack:
            node, child_cursor = stack[-1]
            if child_cursor == 0:
                color[node] = GREY
                path.append(node)
            component = components[node]
            children = [
                child
                for child in component.children
                if child in components and child != node
            ]
            if child_cursor < len(children):
                stack[-1] = (node, child_cursor + 1)
                child = children[child_cursor]
                if color[child] == GREY:
                    cycle = path[path.index(child):] + [child]
                    report_cycle(cycle)
                elif color[child] == WHITE:
                    stack.append((child, 0))
            else:
                color[node] = BLACK
                path.pop()
                stack.pop()


def _assign_chassis(components: Mapping[int, Component]) -> None:
    # Walk from every root independently; a visited set keeps cycles and
    # shared subtrees harmless, so confirmed branches stay reachable.
    roots = [
        component.physical_index
        for component in components.values()
        if component.parent_index is None or component.parent_missing
    ]
    for root_index in sorted(roots):
        root = components[root_index]
        chassis_index = (
            root_index
            if root.component_class == ComponentClass.CHASSIS
            else None
        )
        visited: set[int] = set()
        queue = [root_index]
        while queue:
            current_index = queue.pop()
            if current_index in visited:
                continue
            visited.add(current_index)
            components[current_index].chassis_index = chassis_index
            queue.extend(components[current_index].children)


def _apply_aliases(
    components: Mapping[int, Component],
    interfaces: Mapping[int, Interface],
    alias_rows: Sequence[Mapping[str, object]],
    issues: list[InventoryIssue],
) -> None:
    seen_pairs: set[tuple[int, int]] = set()
    for row in alias_rows:
        physical_index = row["physical_index"]
        component = components.get(physical_index)
        if component is None:
            issues.append(
                InventoryIssue(
                    IssueCode.DANGLING_ALIAS_PHYSICAL,
                    "alias references unknown entPhysicalIndex %r"
                    % (physical_index,),
                    physical_index=physical_index,
                )
            )
            continue
        identifier = row.get("identifier_oid")
        if identifier is not None:
            if_index = interface_index_from_identifier(identifier)
            if if_index is None:
                component.issue_codes.append(IssueCode.ALIAS_TO_NON_IF_ENTRY)
                issues.append(
                    InventoryIssue(
                        IssueCode.ALIAS_TO_NON_IF_ENTRY,
                        "alias OID on component %r is outside IF-MIB "
                        "ifEntry: %r" % (physical_index, tuple(identifier)),
                        physical_index=physical_index,
                    )
                )
                continue
        else:
            if_index = row.get("if_index")
            if if_index is None:
                component.issue_codes.append(IssueCode.ALIAS_TO_NON_IF_ENTRY)
                issues.append(
                    InventoryIssue(
                        IssueCode.ALIAS_TO_NON_IF_ENTRY,
                        "alias on component %r carries neither an ifEntry "
                        "identifier nor an if_index" % (physical_index,),
                        physical_index=physical_index,
                    )
                )
                continue
        pair = (physical_index, if_index)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        if if_index not in interfaces:
            if if_index not in component.unresolved_interface_indices:
                component.unresolved_interface_indices.append(if_index)
            component.issue_codes.append(IssueCode.MISSING_INTERFACE)
            issues.append(
                InventoryIssue(
                    IssueCode.MISSING_INTERFACE,
                    "component %r maps to ifIndex %r which is absent from "
                    "ifTable" % (physical_index, if_index),
                    physical_index=physical_index,
                    interface_index=if_index,
                )
            )
            continue
        if if_index not in component.interface_indices:
            component.interface_indices.append(if_index)
    for component in components.values():
        component.interface_indices.sort()
        component.unresolved_interface_indices.sort()
        component.children.sort()


def _reconcile_history(
    components: Mapping[int, Component],
    history: Mapping[int, HistoricalComponent],
    issues: list[InventoryIssue],
) -> None:
    for physical_index, component in components.items():
        if not component.is_replaceable:
            continue
        previous = history.get(physical_index)
        if previous is None:
            continue
        if previous.serial_number is None:
            # The old snapshot had no evidence either; it cannot make any
            # claim about the identity now sitting at this index.
            continue
        if component.serial_number is None:
            # Index reuse after a swap cannot be ruled out.  Never inherit
            # the stale serial; keep the evidence-insufficient state.
            component.issue_codes.append(
                IssueCode.HISTORICAL_SERIAL_NOT_CARRIED
            )
            issues.append(
                InventoryIssue(
                    IssueCode.HISTORICAL_SERIAL_NOT_CARRIED,
                    "component %r has no current serial evidence; historical "
                    "serial %r is not carried over"
                    % (physical_index, previous.serial_number),
                    physical_index=physical_index,
                )
            )
            continue
        if component.serial_number != previous.serial_number:
            component.issue_codes.append(IssueCode.REUSED_INDEX_NEW_IDENTITY)
            issues.append(
                InventoryIssue(
                    IssueCode.REUSED_INDEX_NEW_IDENTITY,
                    "entPhysicalIndex %r reused by a different unit "
                    "(serial %r, was %r)"
                    % (
                        physical_index,
                        component.serial_number,
                        previous.serial_number,
                    ),
                    physical_index=physical_index,
                )
            )


def _record_serial_issues(
    components: Mapping[int, Component], issues: list[InventoryIssue]
) -> None:
    for component in components.values():
        if component.serial_evidence == SerialEvidence.INSUFFICIENT_EVIDENCE:
            component.issue_codes.append(
                IssueCode.SERIAL_INSUFFICIENT_EVIDENCE
            )
            issues.append(
                InventoryIssue(
                    IssueCode.SERIAL_INSUFFICIENT_EVIDENCE,
                    "replaceable component %r has no serial number evidence"
                    % (component.physical_index,),
                    physical_index=component.physical_index,
                )
            )


def _sort_issues(issues: list[InventoryIssue]) -> list[InventoryIssue]:
    return sorted(
        issues,
        key=lambda issue: (
            issue.code.value,
            issue.physical_index if issue.physical_index is not None else -1,
            issue.interface_index if issue.interface_index is not None else -1,
            issue.related_indices,
        ),
    )


def build_inventory(
    physical_rows: Sequence[Mapping[str, object]],
    interface_rows: Sequence[Mapping[str, object]],
    alias_rows: Sequence[Mapping[str, object]] = (),
    history: Mapping[int, HistoricalComponent] | Sequence[HistoricalComponent] = (),
) -> DeviceInventory:
    """Join ENTITY-MIB containment and IF-MIB aliases into an inventory.

    Args:
        physical_rows: Rows of ``entPhysicalTable`` as mappings with keys
            ``physical_index``, ``component_class`` (ENTITY-MIB class name),
            ``contained_in`` (parent ``entPhysicalIndex``, 0/None for a
            root), optional ``name``/``description``/``serial_number`` and
            an optional ``is_replaceable`` flag (defaults to ``True`` for
            ``module`` class components).
        interface_rows: Rows of ``ifTable`` as mappings with ``if_index``
            and optional ``name``/``description``.
        alias_rows: Rows of ``entAliasMappingTable`` as mappings with
            ``physical_index`` and either ``identifier_oid`` (the
            ``entAliasMappingIdentifier`` OID tuple) or an explicit
            ``if_index`` shorthand.
        history: Optional previous snapshot, either a mapping keyed by
            ``entPhysicalIndex`` or a sequence of :class:`HistoricalComponent`.

    Returns:
        DeviceInventory: every parsed component and interface, plus sorted
        issues.  Bad branches are reported, never silently dropped.
    """
    issues: list[InventoryIssue] = []
    components = _build_components(physical_rows, issues)
    interfaces = _build_interfaces(interface_rows, issues)
    roots = _link_containment(components, issues)
    _apply_aliases(components, interfaces, alias_rows, issues)
    _record_serial_issues(components, issues)

    if isinstance(history, Mapping):
        historical = dict(history)
    else:
        historical = {record.physical_index: record for record in history}
    _reconcile_history(components, historical, issues)

    return DeviceInventory(
        components=components,
        interfaces=interfaces,
        issues=_sort_issues(issues),
        roots=tuple(roots),
    )


__all__ = [
    "Component",
    "ComponentClass",
    "DeviceInventory",
    "HistoricalComponent",
    "IF_ENTRY_OID",
    "Interface",
    "InventoryIssue",
    "IssueCode",
    "SerialEvidence",
    "build_inventory",
    "interface_index_from_identifier",
]
