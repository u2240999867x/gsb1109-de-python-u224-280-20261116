"""Minimal provably-safe rebuild of Pyomo data and model construction.

Given two versions of load results, explicit dependencies and the
dependency declarations of the construction rules, :func:`plan_rebuild`
returns the smallest set of components that must be revalued,
reconstructed or fully re-verified so that the updated model equals a
from-scratch construction on the new data — with every action traceable
back to the original observations, item by item.
"""

from .data import Cell, LoadResult, normalize_key
from .deps import ComponentDecl
from .diff import TableChangeKind, TableDiff, diff_load_results, diff_tables
from .planner import (
    Action,
    Impact,
    ImpactKind,
    KeyTrace,
    RebuildPlan,
    plan_rebuild,
)

__all__ = [
    "Action",
    "Cell",
    "ComponentDecl",
    "Impact",
    "ImpactKind",
    "KeyTrace",
    "LoadResult",
    "RebuildPlan",
    "TableChangeKind",
    "TableDiff",
    "diff_load_results",
    "diff_tables",
    "normalize_key",
    "plan_rebuild",
]
