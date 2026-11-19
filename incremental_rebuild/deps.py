"""Dependency declarations for model components.

Each component (Set, Param, Var, Constraint, ...) declares how it is
constructed from the loaded data:

- ``index_from``: the table whose key set defines the component's index
  domain. Only a membership change in that table alters the domain.
- ``value_reads``: tables read purely as values. A value-only change here
  can be applied by re-assigning values on the old instance.
- ``index_reads``: tables that participate in index selection inside the
  construction rule (e.g. a filtered set). Any change in such a table —
  even value-only — may alter the constructed domain, so the component
  must be reconstructed; re-assigning values on the old instance is not
  provably safe (某参数参与构造时的索引选择，就不能只把旧实例里的值赋新).
- ``upstream`` / ``index_upstream``: other components this one is built
  over, by value reference or by index selection respectively.
- ``has_default``: missing keys fall back to a default value, so a
  membership change only shifts default coverage (触发默认变化).
- ``complete``: whether this declaration is provably complete. If not,
  no input can be proven irrelevant and the component keeps its full
  verification scope (保留全量核验范围) whenever anything changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Optional


@dataclass(frozen=True)
class ComponentDecl:
    name: str
    index_from: Optional[str] = None
    value_reads: FrozenSet[str] = frozenset()
    index_reads: FrozenSet[str] = frozenset()
    upstream: FrozenSet[str] = frozenset()
    index_upstream: FrozenSet[str] = frozenset()
    has_default: bool = False
    complete: bool = True

    def __post_init__(self):
        for field_name in ("value_reads", "index_reads", "upstream", "index_upstream"):
            object.__setattr__(self, field_name, frozenset(getattr(self, field_name)))
