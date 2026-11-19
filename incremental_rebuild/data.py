"""Loaded observation data with item-level provenance.

A :class:`LoadResult` is one version of the loaded data (one "装载结果").
Rows are keyed by their index tuple, so row order carries no meaning:
reordering rows between two loads never produces a difference.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Hashable, Iterable, Mapping, Tuple


def normalize_key(key: Any) -> tuple:
    """Normalise an index key to a tuple."""
    if isinstance(key, tuple):
        return key
    return (key,)


@dataclass(frozen=True)
class Cell:
    """One loaded value plus the identity of its source observation.

    ``source`` is the provenance handle of the original observation
    (原观测来源); it is carried through diffs, impacts and plans so every
    rebuild action stays traceable item by item (逐项回查).
    """

    value: Any
    source: Hashable


class LoadResult:
    """One version of loaded data: table name -> {index key: Cell}."""

    def __init__(self, tables: Mapping[str, Mapping[Any, Cell]] | None = None):
        self._tables = {
            name: {normalize_key(k): cell for k, cell in rows.items()}
            for name, rows in (tables or {}).items()
        }

    @classmethod
    def from_rows(cls, table: str, rows: Iterable[Tuple[Any, Any, Hashable]]) -> "LoadResult":
        """Build a single-table result from ``(key, value, source)`` triples.

        Row order is irrelevant: rows are stored keyed by their index.
        """
        return cls({table: {k: Cell(v, s) for k, v, s in rows}})

    def updated(self, other: "LoadResult") -> "LoadResult":
        """Return a new LoadResult with *other*'s tables overlaid on this one."""
        return LoadResult({**self._tables, **other._tables})

    @property
    def table_names(self) -> frozenset:
        return frozenset(self._tables)

    @property
    def tables(self) -> Mapping[str, Mapping[tuple, Cell]]:
        return MappingProxyType(self._tables)

    def table(self, name: str) -> Mapping[tuple, Cell]:
        """Rows of one table as a read-only mapping (empty if absent)."""
        return MappingProxyType(self._tables.get(name, {}))
