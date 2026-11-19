"""设备目录迁移规划器。

对外公开的 API 见 ``__init__.py``。本包只依赖 Python 标准库，
输入数据不会被原地修改，新情景只在全部约束通过后才发布。
"""

from .model import (
    Allocation,
    Conflict,
    DerivedSet,
    MigrationRule,
    Observation,
    ParameterKind,
    ParameterSpec,
    PlannedValue,
    Provenance,
    ReferenceRule,
    Scenario,
)
from .planner import CatalogPlanner, FormulaContext, MigrationConflict

__all__ = [
    "Allocation",
    "CatalogPlanner",
    "Conflict",
    "DerivedSet",
    "FormulaContext",
    "MigrationConflict",
    "MigrationRule",
    "Observation",
    "ParameterKind",
    "ParameterSpec",
    "PlannedValue",
    "Provenance",
    "ReferenceRule",
    "Scenario",
]
