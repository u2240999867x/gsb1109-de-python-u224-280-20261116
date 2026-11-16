"""Compliance driven collection coverage analysis.

The engine combines three locked inputs:

* a compliance policy translated from the MIB ``MODULE-COMPLIANCE``
  statements (mandatory groups, conditional groups and ``MIN-ACCESS``
  object clauses - information that compiled MIB modules do not retain);
* a device declaration describing which compliance statements the device
  claims, which conditional groups are active and any ``AGENT-CAPABILITIES``
  style access refinements;
* the list of metrics that this delivery must collect.

The locked MIB definitions shipped with pysnmp remain the source of truth
for OIDs, object kinds (scalar vs. table column), ``MAX-ACCESS`` and table
indexing.  The output is a de-duplicated polling set plus a per-object
coverage verdict that keeps every conclusion traceable to a declaration.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pysnmp.smi.builder import MibBuilder

ACCESS_RANK = {
    "not-accessible": 0,
    "accessible-for-notify": 0,
    "read-only": 1,
    "read-write": 2,
    "read-create": 3,
}

DEFAULT_MIN_ACCESS = "read-only"


class CoverageInputError(ValueError):
    """Raised when one of the policy/device/metric inputs is inconsistent."""


@dataclass(frozen=True)
class ObjectRef:
    module: str
    name: str

    @classmethod
    def parse(cls, value: str) -> "ObjectRef":
        if "::" not in value:
            raise CoverageInputError(
                f"object reference {value!r} must use the MODULE::object form"
            )
        module, name = value.split("::", 1)
        return cls(module, name)

    def __str__(self) -> str:
        return f"{self.module}::{self.name}"


@dataclass
class NodeInfo:
    ref: ObjectRef
    oid: tuple[int, ...]
    kind: str
    max_access: str
    parent: ObjectRef | None = None
    table: ObjectRef | None = None
    entry: ObjectRef | None = None
    index_names: tuple[str, ...] = ()


@dataclass
class GroupPolicy:
    ref: ObjectRef
    requirement: str
    condition: str | None
    object_min_access: dict[ObjectRef, str]


@dataclass
class ObjectPolicy:
    ref: ObjectRef
    min_access: str
    requirement: str
    condition: str | None
    reference: str


@dataclass
class CompliancePolicy:
    name: str
    groups: dict[ObjectRef, GroupPolicy]
    objects: list[ObjectPolicy]
    modules: tuple[str, ...]


@dataclass
class GroupDeclaration:
    ref: ObjectRef
    supported: bool
    condition_active: bool | None
    reason: str


@dataclass
class DeviceDeclaration:
    name: str
    claims: list[ObjectRef]
    groups: dict[ObjectRef, GroupDeclaration]
    access_overrides: dict[ObjectRef, str]
    modules: tuple[str, ...]


@dataclass
class Evidence:
    source: str
    ref: str
    requirement: str
    min_access: str
    condition: str | None = None
    condition_active: bool | None = None
    group: ObjectRef | None = None
    verdict: str = ""
    detail: str = ""


@dataclass
class ObjectCoverage:
    node: NodeInfo
    evidences: list[Evidence] = field(default_factory=list)
    effective_min_access: str = DEFAULT_MIN_ACCESS
    device_access: str = DEFAULT_MIN_ACCESS
    verdict: str = ""
    reasons: list[str] = field(default_factory=list)
    polling: dict[str, Any] | None = None


class MibCatalog:
    """Read-only view over the locked, compiled MIB definitions."""

    def __init__(self, modules: list[str]) -> None:
        self.builder = MibBuilder()
        self.builder.loadTexts = True
        self.builder.load_modules(*sorted(set(modules)))
        (
            self._mib_scalar,
            self._mib_table,
            self._mib_row,
            self._mib_column,
            self._mib_tree,
            self._mib_node,
        ) = self.builder.import_symbols(
            "SNMPv2-SMI",
            "MibScalar",
            "MibTable",
            "MibTableRow",
            "MibTableColumn",
            "MibTree",
            "MibNode",
        )
        (self._object_group,) = self.builder.import_symbols(
            "SNMPv2-CONF", "ObjectGroup"
        )
        self.nodes: dict[ObjectRef, NodeInfo] = {}
        self._index_nodes()

    def _index_nodes(self) -> None:
        raw: dict[ObjectRef, tuple[int, ...]] = {}
        for module, symbols in self.builder.mibSymbols.items():
            for name, symbol in symbols.items():
                if not isinstance(symbol, self._mib_node):
                    continue
                ref = ObjectRef(module, name)
                raw[ref] = tuple(symbol.name)
        ordered = sorted(raw.items(), key=lambda item: len(item[1]))
        for ref, oid in ordered:
            (symbol,) = self.builder.import_symbols(ref.module, ref.name)
            kind = self._classify(symbol)
            parent = self._longest_prefix(oid, raw, ref)
            table = entry = None
            index_names: tuple[str, ...] = ()
            if kind == "row":
                entry = ref
                table = parent
                index_names = tuple(
                    f"{mod}::{name}"
                    for _implied, mod, name in symbol.getIndexNames()
                )
            elif kind == "column" and parent is not None:
                entry = parent
                row_info = self.nodes.get(parent)
                if row_info is not None:
                    table = row_info.parent
                    index_names = row_info.index_names
            self.nodes[ref] = NodeInfo(
                ref=ref,
                oid=oid,
                kind=kind,
                max_access=getattr(symbol, "maxAccess", "not-accessible"),
                parent=parent,
                table=table,
                entry=entry,
                index_names=index_names,
            )

    def _classify(self, symbol: Any) -> str:
        if isinstance(symbol, self._mib_column):
            return "column"
        if isinstance(symbol, self._mib_row):
            return "row"
        if isinstance(symbol, self._mib_table):
            return "table"
        if isinstance(symbol, self._mib_scalar):
            return "scalar"
        if isinstance(symbol, self._object_group):
            return "group"
        return "other"

    @staticmethod
    def _longest_prefix(
        oid: tuple[int, ...],
        raw: dict[ObjectRef, tuple[int, ...]],
        self_ref: ObjectRef,
    ) -> ObjectRef | None:
        best: ObjectRef | None = None
        for ref, candidate in raw.items():
            if ref == self_ref or len(candidate) >= len(oid):
                continue
            if oid[: len(candidate)] == candidate and (
                best is None or len(candidate) > len(raw[best])
            ):
                best = ref
        return best

    def require_node(self, ref: ObjectRef) -> NodeInfo:
        try:
            return self.nodes[ref]
        except KeyError:
            raise CoverageInputError(
                f"{ref} is not defined in the locked MIB modules"
            ) from None

    def group_members(self, ref: ObjectRef) -> list[ObjectRef]:
        self.require_node(ref)
        try:
            (group,) = self.builder.import_symbols(ref.module, ref.name)
        except Exception:
            raise CoverageInputError(f"{ref} is not an OBJECT-GROUP") from None
        if not isinstance(group, self._object_group):
            raise CoverageInputError(f"{ref} is not an OBJECT-GROUP")
        return [ObjectRef(module, name) for module, name in group.getObjects()]


def load_json(path: str | Path) -> Any:
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def parse_policy(data: dict[str, Any], catalog: MibCatalog) -> CompliancePolicy:
    groups: dict[ObjectRef, GroupPolicy] = {}
    modules: set[str] = set()
    for item in data.get("groups", ()):
        ref = ObjectRef.parse(item["group"])
        catalog.require_node(ref)
        requirement = item.get("requirement", "mandatory")
        if requirement not in ("mandatory", "conditional"):
            raise CoverageInputError(
                f"group {ref} has unknown requirement {requirement!r}"
            )
        if requirement == "conditional" and not item.get("condition"):
            raise CoverageInputError(
                f"conditional group {ref} must state its condition"
            )
        overrides = {}
        members = catalog.group_members(ref)
        for entry in item.get("object_min_access", ()):
            obj_ref = ObjectRef.parse(entry["object"])
            catalog.require_node(obj_ref)
            if obj_ref not in members:
                raise CoverageInputError(
                    f"MIN-ACCESS override {obj_ref} is not a member of "
                    f"group {ref}"
                )
            overrides[obj_ref] = _validate_access(entry["min_access"], obj_ref)
        groups[ref] = GroupPolicy(
            ref=ref,
            requirement=requirement,
            condition=item.get("condition"),
            object_min_access=overrides,
        )
        modules.add(ref.module)
        for member in members:
            catalog.require_node(member)
            modules.add(member.module)

    objects: list[ObjectPolicy] = []
    for item in data.get("objects", ()):
        ref = ObjectRef.parse(item["object"])
        catalog.require_node(ref)
        requirement = item.get("requirement", "mandatory")
        if requirement not in ("mandatory", "conditional"):
            raise CoverageInputError(
                f"object {ref} has unknown requirement {requirement!r}"
            )
        if requirement == "conditional" and not item.get("condition"):
            raise CoverageInputError(
                f"conditional object {ref} must state its condition"
            )
        objects.append(
            ObjectPolicy(
                ref=ref,
                min_access=_validate_access(
                    item.get("min_access", DEFAULT_MIN_ACCESS), ref
                ),
                requirement=requirement,
                condition=item.get("condition"),
                reference=item.get("reference", ""),
            )
        )
        modules.add(ref.module)
    return CompliancePolicy(
        name=data.get("name", "compliance-policy"),
        groups=groups,
        objects=objects,
        modules=tuple(sorted(modules)),
    )


def parse_device(data: dict[str, Any], catalog: MibCatalog) -> DeviceDeclaration:
    groups: dict[ObjectRef, GroupDeclaration] = {}
    modules: set[str] = set()
    for item in data.get("groups", ()):
        ref = ObjectRef.parse(item["group"])
        catalog.require_node(ref)
        groups[ref] = GroupDeclaration(
            ref=ref,
            supported=bool(item["supported"]),
            condition_active=item.get("condition_active"),
            reason=item.get("reason", ""),
        )
        modules.add(ref.module)
    overrides: dict[ObjectRef, str] = {}
    for item in data.get("object_access", ()):
        ref = ObjectRef.parse(item["object"])
        catalog.require_node(ref)
        overrides[ref] = _validate_access(item["access"], ref)
        modules.add(ref.module)
    claims = [ObjectRef.parse(item) for item in data.get("claims", ())]
    for claim in claims:
        catalog.require_node(claim)
        modules.add(claim.module)
    return DeviceDeclaration(
        name=data.get("name", "device"),
        claims=claims,
        groups=groups,
        access_overrides=overrides,
        modules=tuple(sorted(modules)),
    )


def parse_metrics(data: dict[str, Any], catalog: MibCatalog) -> list[ObjectRef]:
    metrics: list[ObjectRef] = []
    for item in data.get("required_metrics", ()):
        ref = ObjectRef.parse(item["object"] if isinstance(item, dict) else item)
        catalog.require_node(ref)
        metrics.append(ref)
    return metrics


def _validate_access(value: str, ref: ObjectRef) -> str:
    if value not in ACCESS_RANK:
        raise CoverageInputError(
            f"{ref} uses unknown access level {value!r}; "
            f"expected one of {sorted(ACCESS_RANK)}"
        )
    return value


def build_coverage(
    catalog: MibCatalog,
    policy: CompliancePolicy,
    device: DeviceDeclaration,
    metrics: list[ObjectRef],
) -> dict[ObjectRef, ObjectCoverage]:
    coverage: dict[ObjectRef, ObjectCoverage] = {}

    def record(ref: ObjectRef) -> ObjectCoverage:
        if ref not in coverage:
            coverage[ref] = ObjectCoverage(node=catalog.require_node(ref))
        return coverage[ref]

    for group_ref, group_policy in policy.groups.items():
        declaration = device.groups.get(group_ref)
        if declaration is None:
            raise CoverageInputError(
                f"device declaration does not answer for group {group_ref}"
            )
        for member in catalog.group_members(group_ref):
            item = record(member)
            min_access = group_policy.object_min_access.get(
                member, DEFAULT_MIN_ACCESS
            )
            evidence = Evidence(
                source="group",
                ref=f"policy-group:{group_ref}",
                requirement=group_policy.requirement,
                min_access=min_access,
                condition=group_policy.condition,
                condition_active=declaration.condition_active,
                group=group_ref,
            )
            _judge_group_evidence(evidence, declaration)
            item.evidences.append(evidence)

    for object_policy in policy.objects:
        item = record(object_policy.ref)
        item.evidences.append(
            Evidence(
                source="compliance-object",
                ref=object_policy.reference
                or f"policy-object:{object_policy.ref}",
                requirement=object_policy.requirement,
                min_access=object_policy.min_access,
                condition=object_policy.condition,
                condition_active=_condition_active(
                    policy, device, object_policy.condition
                ),
            )
        )

    for metric in metrics:
        item = record(metric)
        item.evidences.append(
            Evidence(
                source="required-metric",
                ref="delivery-metric",
                requirement="mandatory",
                min_access=DEFAULT_MIN_ACCESS,
            )
        )

    for item in coverage.values():
        _finalize(item, device)
    return coverage


def _condition_active(
    policy: CompliancePolicy,
    device: DeviceDeclaration,
    condition: str | None,
) -> bool | None:
    if condition is None:
        return None
    for group_ref, group_policy in policy.groups.items():
        if group_policy.condition != condition:
            continue
        declaration = device.groups.get(group_ref)
        if declaration is None:
            return False
        return bool(declaration.condition_active)
    return False


def _judge_group_evidence(
    evidence: Evidence, declaration: GroupDeclaration
) -> None:
    if evidence.requirement == "conditional" and (
        evidence.condition_active is False
    ):
        evidence.verdict = "condition-inactive"
        evidence.detail = evidence.condition or "conditional group inactive"
        return
    if not declaration.supported:
        evidence.verdict = "group-unsupported"
        evidence.detail = declaration.reason or "group not implemented"
        return
    evidence.verdict = "pending-access"


def _finalize(item: ObjectCoverage, device: DeviceDeclaration) -> None:
    node = item.node
    item.device_access = device.access_overrides.get(
        node.ref, node.max_access
    )
    item.effective_min_access = max(
        (
            evidence.min_access
            for evidence in item.evidences
            if evidence.verdict != "condition-inactive"
        ),
        key=lambda access: ACCESS_RANK[access],
        default=DEFAULT_MIN_ACCESS,
    )
    for evidence in item.evidences:
        if evidence.verdict not in ("", "pending-access"):
            continue
        if ACCESS_RANK[item.device_access] < ACCESS_RANK[evidence.min_access]:
            evidence.verdict = "access-deficit"
            evidence.detail = (
                f"device offers {item.device_access}, requirement "
                f"needs {evidence.min_access}"
            )
        elif item.device_access == "not-accessible":
            evidence.verdict = "inaccessible"
            evidence.detail = "object is not-accessible on this device"
        else:
            evidence.verdict = "ok"

    has_metric = any(
        evidence.source == "required-metric" for evidence in item.evidences
    )
    active_support = [
        evidence
        for evidence in item.evidences
        if evidence.source != "required-metric"
        and evidence.verdict not in ("condition-inactive", "group-unsupported")
    ]
    if has_metric and not active_support:
        for evidence in item.evidences:
            if evidence.source != "required-metric":
                continue
            evidence.verdict = "undeclared"
            evidence.detail = (
                "required metric has no active GROUP/OBJECT declaration on "
                "this device; instance existence cannot be proven from the "
                "declaration and must be verified at runtime"
            )

    active_verdicts = [
        evidence.verdict
        for evidence in item.evidences
        if evidence.verdict != "condition-inactive"
    ]
    if not active_verdicts:
        item.verdict = "waived"
        for evidence in item.evidences:
            item.reasons.append(
                f"{evidence.ref}: condition inactive ({evidence.condition})"
            )
        return
    if item.device_access == "not-accessible" or node.kind not in (
        "scalar",
        "column",
    ):
        item.verdict = "unsatisfied"
        item.reasons.append(
            f"{node.ref} is {node.kind} with access "
            f"{item.device_access}; it has no pollable instance"
            + (
                f" (index columns such as {node.ref.name} only appear "
                "encoded inside row instance OIDs)"
                if node.kind == "column"
                else ""
            )
        )
    elif all(verdict == "ok" for verdict in active_verdicts):
        item.verdict = "satisfied"
    else:
        item.verdict = "partial"
        for evidence in item.evidences:
            if evidence.verdict in (
                "group-unsupported",
                "access-deficit",
                "inaccessible",
                "undeclared",
            ):
                item.reasons.append(f"{evidence.ref}: {evidence.detail}")
            elif evidence.verdict == "condition-inactive":
                item.reasons.append(
                    f"{evidence.ref}: condition inactive ({evidence.detail})"
                )

    if item.verdict != "unsatisfied":
        item.polling = _polling_entry(item)


def _polling_entry(item: ObjectCoverage) -> dict[str, Any]:
    node = item.node
    sources = sorted({evidence.ref for evidence in item.evidences})
    if node.kind == "scalar":
        instance_oid = node.oid + (0,)
        return {
            "operation": "GET",
            "target": f"{node.ref}.0",
            "oid": ".".join(str(part) for part in instance_oid),
            "dedup_sources": sources,
            "note": "scalar instance; a single GET returns the value",
        }
    return {
        "operation": "WALK",
        "target": str(node.ref),
        "oid": ".".join(str(part) for part in node.oid),
        "index_columns": list(node.index_names),
        "dedup_sources": sources,
        "note": (
            "table column root is a schema node, not an instance; issue "
            "GETNEXT/GETBulk subtree walk and treat each returned varbind "
            "as one row instance - one GET on the column root can never "
            "prove the table is populated"
        ),
    }
