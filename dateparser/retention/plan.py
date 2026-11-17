from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .errors import RetentionInputError
from .policy import Window, evaluate_rule

if TYPE_CHECKING:
    from datetime import datetime

    from .model import Material, RetentionInput
    from .policy import RetentionRule

STATUS_PENDING = "pending"
STATUS_HELD = "held"
STATUS_RECLAIMABLE = "reclaimable"

SOURCE_MATERIAL = "material"
SOURCE_PROJECT = "project"
SOURCE_DEPENDENCY = "dependency"


@dataclass(frozen=True)
class Requirement:
    """One effective retention requirement on a material.

    ``source`` tells where the requirement came from: the material's own rule
    set, another project that shares the material, or a derived artifact that
    still depends on it.
    """

    window: Window | None
    source_kind: str
    source: str
    project: str
    detail: str

    @property
    def is_pending(self) -> bool:
        return self.window is not None and self.window.is_pending

    @property
    def end(self) -> datetime | None:
        return self.window.end if self.window is not None else None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "source_kind": self.source_kind,
            "source": self.source,
            "project": self.project,
            "detail": self.detail,
        }
        if self.window is not None:
            data.update(self.window.to_dict())
        else:
            data.update(
                {
                    "rule_id": None,
                    "raw_text": None,
                    "kind": None,
                    "anchor": None,
                    "anchor_value": None,
                    "reclaim_from": None,
                    "window": None,
                    "basis": self.detail,
                    "notes": [],
                    "status": "resolved",
                    "pending": None,
                }
            )
        return data


@dataclass
class MaterialPlan:
    material: Material
    requirements: list[Requirement] = field(default_factory=list)
    dependency_requirements: list[Requirement] = field(default_factory=list)
    dependents: list[str] = field(default_factory=list)
    status: str = STATUS_RECLAIMABLE
    reclaim_from: datetime | None = None
    own_reclaim_from: datetime | None = None
    final_rule_id: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def is_pending(self) -> bool:
        return self.status == STATUS_PENDING

    @property
    def is_held(self) -> bool:
        return self.status == STATUS_HELD

    @property
    def is_reclaimable(self) -> bool:
        return self.status == STATUS_RECLAIMABLE

    @property
    def is_shared_original(self) -> bool:
        return self.material.is_original and bool(self.material.shared_with)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.material.id,
            "label": self.material.label,
            "kind": self.material.kind,
            "project": self.material.project,
            "shared_with": list(self.material.shared_with),
            "shared_original": self.is_shared_original,
            "collected_at": self.material.collected_at.isoformat(),
            "collection_period": self.material.collection_period,
            "collection_input_note": self.material.collection_note,
            "settlement_at": (
                self.material.settlement_at.isoformat()
                if self.material.settlement_at is not None
                else None
            ),
            "settlement_input_note": self.material.settlement_note,
            "depends_on": list(self.material.depends_on),
            "dependents": list(self.dependents),
            "requirements": [item.to_dict() for item in self.requirements],
            "dependency_requirements": [
                item.to_dict() for item in self.dependency_requirements
            ],
            "own_reclaim_from": (
                self.own_reclaim_from.isoformat()
                if self.own_reclaim_from is not None
                else None
            ),
            "reclaim_from": (
                self.reclaim_from.isoformat() if self.reclaim_from is not None else None
            ),
            "status": self.status,
            "final_rule_id": self.final_rule_id,
            "notes": list(self.notes),
        }


@dataclass
class ProjectRetention:
    id: str
    name: str | None
    materials: list[str]
    requirements: list[dict[str, Any]]
    reclaimable: list[str]
    held: list[str]
    pending: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "materials": list(self.materials),
            "requirements": [dict(item) for item in self.requirements],
            "reclaimable": list(self.reclaimable),
            "held": list(self.held),
            "pending": list(self.pending),
        }


@dataclass
class RetentionPlan:
    calendar_id: str
    calendar_fingerprint: str
    calendar_snapshot: dict[str, Any]
    as_of: datetime
    generated_at: datetime
    materials: dict[str, MaterialPlan]
    projects: dict[str, ProjectRetention]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "retention_plan",
            "calendar": {
                "id": self.calendar_id,
                "fingerprint": self.calendar_fingerprint,
                "snapshot": self.calendar_snapshot,
            },
            "as_of": self.as_of.isoformat(),
            "generated_at": self.generated_at.isoformat(),
            "summary": {
                "total": len(self.materials),
                "reclaimable": len(self.reclaimable()),
                "held": len(self.held()),
                "pending": len(self.pending()),
            },
            "reclaimable": self.reclaimable(),
            "held": self.held(),
            "pending": self.pending(),
            "materials": [plan.to_dict() for plan in self.materials.values()],
            "projects": [project.to_dict() for project in self.projects.values()],
        }

    def reclaimable(self) -> list[str]:
        return [
            material_id
            for material_id, plan in self.materials.items()
            if plan.is_reclaimable
        ]

    def pending(self) -> list[str]:
        return [
            material_id
            for material_id, plan in self.materials.items()
            if plan.is_pending
        ]

    def held(self) -> list[str]:
        return [
            material_id for material_id, plan in self.materials.items() if plan.is_held
        ]


def _project_rule_ids(data: RetentionInput, project_id: str) -> tuple[str, ...] | None:
    project = data.projects.get(project_id)
    return project.rule_ids if project is not None else None


def _evaluate_requirements(
    material: Material, data: RetentionInput
) -> list[Requirement]:
    interested: list[str] = []
    for project_id in (material.project, *material.shared_with):
        if project_id not in interested:
            interested.append(project_id)

    requirements: list[Requirement] = []

    for project_id in interested:
        rules = _rules_for_project(material, data, project_id)
        if rules is None:
            continue
        source_kind, source, detail = _requirement_source(material, project_id)
        for rule in rules:
            window = evaluate_rule(
                rule,
                collected_at=material.collected_at,
                calendar=data.calendar,
                settlement_at=material.settlement_at,
                collection_period=material.collection_period,
            )
            requirements.append(
                Requirement(
                    window=window,
                    source_kind=source_kind,
                    source=source,
                    project=project_id,
                    detail=detail,
                )
            )
    return requirements


def _requirement_source(material: Material, project_id: str) -> tuple[str, str, str]:
    if project_id == material.project:
        return (
            SOURCE_MATERIAL,
            f"material:{material.id}",
            "the material's own retention rule",
        )
    return (
        SOURCE_PROJECT,
        f"project:{project_id}",
        f"retention requirement of sharing project {project_id}",
    )


def _rules_for_project(
    material: Material, data: RetentionInput, project_id: str
) -> list[RetentionRule] | None:
    if (
        project_id != material.project
        and project_id not in material.explicit_shared_with
    ):
        return None

    project = data.projects.get(project_id)
    selected = _project_rule_ids(data, project_id)
    if project_id == material.project and material.rule_ids is not None:
        selected = material.rule_ids

    rules = _select_rules(data, selected, project_id)
    if project is not None:
        rules = [*rules, *project.extra_rules]
    if project_id == material.project:
        known = {rule.id for rule in rules}
        rules = [
            *rules,
            *(rule for rule in material.extra_rules if rule.id not in known),
        ]
    return rules


def _select_rules(
    data: RetentionInput, selected: tuple[str, ...] | None, project_id: str
) -> list[RetentionRule]:
    if selected is None:
        return list(data.rules)
    known = {rule.id for rule in data.rules}
    unknown = [rule_id for rule_id in selected if rule_id not in known]
    if unknown:
        raise RetentionInputError(
            f"project {project_id!r} references unknown rules: {unknown}"
        )
    allowed = set(selected)
    return [rule for rule in data.rules if rule.id in allowed]


def build_plan(
    data: RetentionInput,
    *,
    as_of: datetime,
    generated_at: datetime,
) -> RetentionPlan:
    if as_of.tzinfo is None or generated_at.tzinfo is None:
        raise RetentionInputError("as_of and generated_at must be timezone-aware")

    plans: dict[str, MaterialPlan] = {}
    for material_id, material in data.materials.items():
        plans[material_id] = MaterialPlan(
            material=material,
            requirements=_evaluate_requirements(material, data),
        )

    children: dict[str, list[str]] = {material_id: [] for material_id in data.materials}
    for material in data.materials.values():
        for parent in material.depends_on:
            children[parent].append(material.id)
    for material_id, plan in plans.items():
        plan.dependents = list(children.get(material_id, []))

    in_progress: set[str] = set()
    resolved: dict[str, tuple[bool, datetime | None, str | None]] = {}

    def resolve(material_id: str) -> tuple[bool, datetime | None, str | None]:
        if material_id in resolved:
            return resolved[material_id]
        if material_id in in_progress:
            raise RetentionInputError(
                f"dependency cycle detected at material {material_id!r}"
            )
        in_progress.add(material_id)
        plan = plans[material_id]

        pending = any(item.is_pending for item in plan.requirements)
        contributing: list[tuple[datetime, str | None]] = [
            (item.end, item.window.rule_id if item.window is not None else None)
            for item in plan.requirements
            if item.end is not None
        ]
        plan.own_reclaim_from = (
            max(end for end, _ in contributing) if contributing else None
        )

        for child_id in plan.dependents:
            child_pending, child_end, child_rule = resolve(child_id)
            child = plans[child_id]
            if child_pending:
                dependency_window = _pending_dependency_window(child)
                plan.dependency_requirements.append(
                    Requirement(
                        window=dependency_window,
                        source_kind=SOURCE_DEPENDENCY,
                        source=f"dependency:{child_id}",
                        project=child.material.project,
                        detail=(
                            f"still required by dependent material {child_id}, "
                            "whose own retention window is pending"
                        ),
                    )
                )
                pending = True
            else:
                assert child_end is not None
                plan.dependency_requirements.append(
                    Requirement(
                        window=None,
                        source_kind=SOURCE_DEPENDENCY,
                        source=f"dependency:{child_id}",
                        project=child.material.project,
                        detail=(
                            f"still required by dependent material {child_id} "
                            f"until {child_end.isoformat()}"
                        ),
                    )
                )
                contributing.append((child_end, child_rule))

        in_progress.remove(material_id)

        if pending:
            plan.status = STATUS_PENDING
            plan.reclaim_from = None
            plan.final_rule_id = None
            plan.notes.append(
                "at least one effective requirement is unresolved, so the "
                "material cannot be listed as reclaimable"
            )
            outcome: tuple[bool, datetime | None, str | None] = (True, None, None)
            resolved[material_id] = outcome
            return outcome

        final, final_rule = max(contributing, key=lambda item: item[0])
        plan.reclaim_from = final
        plan.final_rule_id = final_rule
        if final <= as_of:
            plan.status = STATUS_RECLAIMABLE
        else:
            plan.status = STATUS_HELD
        outcome = (False, final, final_rule)
        resolved[material_id] = outcome
        return outcome

    for material_id in data.materials:
        resolve(material_id)

    project_plans = _build_project_plans(data, plans)
    return RetentionPlan(
        calendar_id=data.calendar.id,
        calendar_fingerprint=data.calendar.fingerprint,
        calendar_snapshot=data.calendar.snapshot(),
        as_of=as_of,
        generated_at=generated_at,
        materials=plans,
        projects=project_plans,
    )


def _pending_dependency_window(child: MaterialPlan) -> Window | None:
    for requirement in child.requirements:
        window = requirement.window
        if window is not None and window.is_pending:
            return window
    for requirement in child.dependency_requirements:
        window = requirement.window
        if window is not None and window.is_pending:
            return window
    return None


def _build_project_plans(
    data: RetentionInput, plans: dict[str, MaterialPlan]
) -> dict[str, ProjectRetention]:
    project_ids: list[str] = list(data.projects)
    for material in data.materials.values():
        for project_id in (material.project, *material.shared_with):
            if project_id not in project_ids:
                project_ids.append(project_id)

    result: dict[str, ProjectRetention] = {}
    for project_id in project_ids:
        project = data.projects.get(project_id)
        own_materials = [
            material_id
            for material_id, plan in plans.items()
            if plan.material.project == project_id
        ]
        shared_materials = [
            material_id
            for material_id, plan in plans.items()
            if project_id in plan.material.shared_with
            and plan.material.project != project_id
        ]
        requirements: list[dict[str, Any]] = []
        for material_id, plan in plans.items():
            for requirement in plan.requirements:
                if requirement.project != project_id:
                    continue
                record = requirement.to_dict()
                record["material_id"] = material_id
                record["material_scope"] = (
                    "own" if plan.material.project == project_id else "shared"
                )
                requirements.append(record)
        result[project_id] = ProjectRetention(
            id=project_id,
            name=project.name if project is not None else None,
            materials=[*own_materials, *shared_materials],
            requirements=requirements,
            reclaimable=[
                material_id
                for material_id in own_materials
                if plans[material_id].is_reclaimable
            ],
            held=[
                material_id
                for material_id in own_materials
                if plans[material_id].is_held
            ],
            pending=[
                material_id
                for material_id in own_materials
                if plans[material_id].is_pending
            ],
        )
    return result


__all__ = [
    "STATUS_HELD",
    "STATUS_PENDING",
    "STATUS_RECLAIMABLE",
    "MaterialPlan",
    "ProjectRetention",
    "Requirement",
    "RetentionPlan",
    "build_plan",
]
