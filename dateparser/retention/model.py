from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from .calendar import BusinessCalendar, parse_datetime
from .errors import RetentionInputError
from .policy import RetentionRule, parse_policy

if TYPE_CHECKING:
    from datetime import datetime

MATERIAL_ORIGINAL = "original"
MATERIAL_DERIVED = "derived"


@dataclass(frozen=True)
class Material:
    id: str
    kind: str
    project: str
    collected_at: datetime
    collection_note: str | None
    collection_period: str | None
    settlement_at: datetime | None
    settlement_note: str | None
    depends_on: tuple[str, ...]
    rule_ids: tuple[str, ...] | None
    extra_rules: tuple[RetentionRule, ...]
    label: str | None
    shared_with: tuple[str, ...]
    explicit_shared_with: tuple[str, ...] = ()

    @property
    def is_original(self) -> bool:
        return self.kind == MATERIAL_ORIGINAL


@dataclass(frozen=True)
class Project:
    id: str
    name: str | None
    rule_ids: tuple[str, ...] | None
    extra_rules: tuple[RetentionRule, ...]


@dataclass(frozen=True)
class RetentionInput:
    calendar: BusinessCalendar
    rules: tuple[RetentionRule, ...]
    projects: dict[str, Project]
    materials: dict[str, Material]
    rule_order: tuple[str, ...]
    dependent_projects: dict[str, set[str]]


def parse_retention_input(data: Any) -> RetentionInput:
    if not isinstance(data, dict):
        raise RetentionInputError("input must be a JSON object")

    calendar = BusinessCalendar.from_dict(data.get("calendar"))
    policy_data = data.get("policy", data.get("policies"))
    if policy_data is None:
        raise RetentionInputError("input must define policy or policies")
    rules = parse_policy(policy_data)
    rule_order = tuple(rule.id for rule in rules)

    projects = _parse_projects(data.get("projects", []))
    materials = _parse_materials(
        data.get("materials", []),
        calendar=calendar,
        projects=projects,
    )
    _validate_graph(materials)

    originals_by_project: dict[str, set[str]] = {}
    for material in materials.values():
        if material.is_original:
            originals_by_project.setdefault(material.project, set()).add(material.id)
    derived_projects: dict[str, set[str]] = {}
    for material in materials.values():
        for parent in material.depends_on:
            if materials[parent].is_original:
                derived_projects.setdefault(parent, set()).add(material.project)
    del originals_by_project

    augmented: dict[str, Material] = {}
    for material in materials.values():
        shared = set(material.shared_with)
        if material.is_original:
            shared |= derived_projects.get(material.id, set())
            shared.discard(material.project)
        augmented[material.id] = replace(
            material,
            shared_with=tuple(sorted(shared)),
            explicit_shared_with=material.shared_with,
        )
    return RetentionInput(
        calendar=calendar,
        rules=tuple(rules),
        projects=projects,
        materials=augmented,
        rule_order=rule_order,
        dependent_projects=derived_projects,
    )


def _parse_projects(value: Any) -> dict[str, Project]:
    if value is None:
        return {}
    if not isinstance(value, list):
        raise RetentionInputError("projects must be a list")
    projects: dict[str, Project] = {}
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise RetentionInputError(f"project {index} must be an object")
        project_id = item.get("id")
        if not isinstance(project_id, str) or not project_id:
            raise RetentionInputError(f"project {index} must define a string id")
        if project_id in projects:
            raise RetentionInputError(f"duplicate project id {project_id!r}")
        raw_rule_ids = item.get("rule_ids")
        rule_ids = None
        if raw_rule_ids is not None:
            if not isinstance(raw_rule_ids, (list, tuple)) or not raw_rule_ids:
                raise RetentionInputError(
                    f"project {project_id!r} rule_ids must be a non-empty list"
                )
            for rule_id in raw_rule_ids:
                if not isinstance(rule_id, str):
                    raise RetentionInputError(
                        f"project {project_id!r} rule_ids must be strings"
                    )
            rule_ids = tuple(raw_rule_ids)
        extra_rules = _parse_inline_rules(
            item.get("rules", []), f"project {project_id!r}"
        )
        name = item.get("name")
        if name is not None and not isinstance(name, str):
            raise RetentionInputError(f"project {project_id!r} name must be a string")
        projects[project_id] = Project(
            id=project_id,
            name=name,
            rule_ids=rule_ids,
            extra_rules=extra_rules,
        )
    return projects


def _parse_materials(
    value: Any,
    *,
    calendar: BusinessCalendar,
    projects: dict[str, Project],
) -> dict[str, Material]:
    if value is None:
        return {}
    if not isinstance(value, list):
        raise RetentionInputError("materials must be a list")
    materials: dict[str, Material] = {}
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise RetentionInputError(f"material {index} must be an object")
        material = _parse_material(item, index, calendar=calendar, projects=projects)
        if material.id in materials:
            raise RetentionInputError(f"duplicate material id {material.id!r}")
        materials[material.id] = material
    return materials


def _parse_material(
    item: dict[str, Any],
    index: int,
    *,
    calendar: BusinessCalendar,
    projects: dict[str, Project],
) -> Material:
    material_id = item.get("id")
    if not isinstance(material_id, str) or not material_id:
        raise RetentionInputError(f"material {index} must define a string id")

    kind = item.get("kind", item.get("type"))
    if kind is None:
        kind = MATERIAL_DERIVED if item.get("depends_on") else MATERIAL_ORIGINAL
    if kind in {"source", "collected", "采集原件"}:
        kind = MATERIAL_ORIGINAL
    if kind in {"derivative", "derived_material", "派生物"}:
        kind = MATERIAL_DERIVED
    if kind not in {MATERIAL_ORIGINAL, MATERIAL_DERIVED}:
        raise RetentionInputError(
            f"material {material_id!r} kind must be {MATERIAL_ORIGINAL!r} or "
            f"{MATERIAL_DERIVED!r}, got {kind!r}"
        )

    project = item.get("project")
    if not isinstance(project, str) or not project:
        raise RetentionInputError(f"material {material_id!r} must define a project")
    if projects and project not in projects:
        raise RetentionInputError(
            f"material {material_id!r} references unknown project {project!r}"
        )

    collected_at, collection_note = _parse_collected_at(item, material_id, calendar)
    collection_period = _parse_collection_period(item, material_id)
    settlement_at, settlement_note = _parse_settlement_at(item, material_id, calendar)
    depends_on = _parse_depends_on(item, material_id)
    rule_ids = _parse_material_rule_ids(item, material_id)
    shared_with = _parse_shared_with(item, material_id, projects)
    extra_rules = _parse_inline_rules(
        item.get("rules", []), f"material {material_id!r}"
    )

    label = item.get("label")
    if label is not None and not isinstance(label, str):
        raise RetentionInputError(f"material {material_id!r} label must be a string")

    return Material(
        id=material_id,
        kind=kind,
        project=project,
        collected_at=collected_at,
        collection_note=collection_note,
        collection_period=collection_period,
        settlement_at=settlement_at,
        settlement_note=settlement_note,
        depends_on=depends_on,
        rule_ids=rule_ids,
        extra_rules=extra_rules,
        label=label,
        shared_with=shared_with,
    )


def _parse_collected_at(
    item: dict[str, Any], material_id: str, calendar: BusinessCalendar
) -> tuple[datetime, str | None]:
    raw_collected = item.get("collected_at", item.get("collection_time"))
    if raw_collected is None:
        raise RetentionInputError(
            f"material {material_id!r} must define collected_at; the collection "
            "time is never inferred or rewritten"
        )
    return parse_datetime(
        raw_collected, calendar.timezone, f"material {material_id!r} collected_at"
    )


def _parse_collection_period(item: dict[str, Any], material_id: str) -> str | None:
    collection_period = item.get("collection_period")
    if collection_period is not None and not isinstance(collection_period, str):
        raise RetentionInputError(
            f"material {material_id!r} collection_period must be YYYY-MM"
        )
    return collection_period


def _parse_settlement_at(
    item: dict[str, Any], material_id: str, calendar: BusinessCalendar
) -> tuple[datetime | None, str | None]:
    if item.get("settlement_at") is None:
        return None, None
    return parse_datetime(
        item["settlement_at"],
        calendar.timezone,
        f"material {material_id!r} settlement_at",
    )


def _parse_depends_on(item: dict[str, Any], material_id: str) -> tuple[str, ...]:
    raw_depends = item.get("depends_on") or []
    if not isinstance(raw_depends, (list, tuple)):
        raise RetentionInputError(f"material {material_id!r} depends_on must be a list")
    depends_on: list[str] = []
    for parent in raw_depends:
        if not isinstance(parent, str) or not parent:
            raise RetentionInputError(
                f"material {material_id!r} depends_on must contain material ids"
            )
        if parent == material_id:
            raise RetentionInputError(
                f"material {material_id!r} cannot depend on itself"
            )
        depends_on.append(parent)
    return tuple(depends_on)


def _parse_material_rule_ids(
    item: dict[str, Any], material_id: str
) -> tuple[str, ...] | None:
    raw_rule_ids = item.get("rule_ids")
    if raw_rule_ids is None:
        return None
    if not isinstance(raw_rule_ids, (list, tuple)) or not raw_rule_ids:
        raise RetentionInputError(
            f"material {material_id!r} rule_ids must be a non-empty list"
        )
    for rule_id in raw_rule_ids:
        if not isinstance(rule_id, str):
            raise RetentionInputError(
                f"material {material_id!r} rule_ids must be strings"
            )
    return tuple(raw_rule_ids)


def _parse_shared_with(
    item: dict[str, Any], material_id: str, projects: dict[str, Project]
) -> tuple[str, ...]:
    shared_with = item.get("shared_with") or []
    if not isinstance(shared_with, (list, tuple)):
        raise RetentionInputError(
            f"material {material_id!r} shared_with must be a list of project ids"
        )
    for shared_project in shared_with:
        if not isinstance(shared_project, str):
            raise RetentionInputError(
                f"material {material_id!r} shared_with must contain project ids"
            )
        if projects and shared_project not in projects:
            raise RetentionInputError(
                f"material {material_id!r} is shared with unknown project "
                f"{shared_project!r}"
            )
    return tuple(shared_with)


def _parse_inline_rules(value: Any, owner: str) -> tuple[RetentionRule, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise RetentionInputError(f"{owner} rules must be a list")
    rules: list[RetentionRule] = []
    for index, item in enumerate(value):
        rule = RetentionRule.from_dict(item, index=index)
        if any(existing.id == rule.id for existing in rules):
            raise RetentionInputError(f"{owner} defines duplicate rule id {rule.id!r}")
        rules.append(rule)
    return tuple(rules)


def _validate_graph(materials: dict[str, Material]) -> None:
    for material in materials.values():
        for parent in material.depends_on:
            if parent not in materials:
                raise RetentionInputError(
                    f"material {material.id!r} depends on unknown material {parent!r}"
                )
            if material.is_original:
                raise RetentionInputError(
                    f"original material {material.id!r} cannot depend on other materials"
                )
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(material_id: str, trail: list[str]) -> None:
        if material_id in visited:
            return
        if material_id in visiting:
            cycle = " -> ".join([*trail, material_id])
            raise RetentionInputError(f"dependency cycle detected: {cycle}")
        visiting.add(material_id)
        for parent in materials[material_id].depends_on:
            visit(parent, [*trail, material_id])
        visiting.remove(material_id)
        visited.add(material_id)

    for material_id in materials:
        visit(material_id, [])
