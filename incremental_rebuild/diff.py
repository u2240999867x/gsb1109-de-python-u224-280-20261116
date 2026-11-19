"""Order-insensitive diff between two versions of loaded data.

Rows are keyed by index tuple, so a pure row reordering yields an empty
diff (行重排不触发变化). Diffs classify each changed table as either
value-only (仅改值) or membership (改变成员); the consumer-relative
categories (default-triggering, construction-scope) are derived by the
planner, which knows how each component uses the table.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterator, Mapping, Optional, Tuple

from .data import Cell, LoadResult


class TableChangeKind(Enum):
    VALUE_ONLY = "value_only"  # same key set, some values differ
    MEMBERSHIP = "membership"  # keys added and/or removed (values may differ too)


@dataclass(frozen=True)
class TableDiff:
    """Difference between two versions of one table."""

    table: str
    added: Mapping[tuple, Cell]  # keys present only in the new load
    removed: Mapping[tuple, Cell]  # keys present only in the old load
    changed: Mapping[tuple, Tuple[Cell, Cell]]  # keys in both with differing values

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    @property
    def kind(self) -> TableChangeKind:
        if self.added or self.removed:
            return TableChangeKind.MEMBERSHIP
        return TableChangeKind.VALUE_ONLY

    def traces(self) -> Iterator[Tuple[tuple, Optional[Cell], Optional[Cell]]]:
        """Yield ``(key, old_cell, new_cell)`` per differing row, item by item."""
        for key, cell in self.added.items():
            yield key, None, cell
        for key, cell in self.removed.items():
            yield key, cell, None
        for key, (old_cell, new_cell) in self.changed.items():
            yield key, old_cell, new_cell


def diff_tables(
    old_rows: Mapping[tuple, Cell], new_rows: Mapping[tuple, Cell], table: str = ""
) -> TableDiff:
    added = {k: c for k, c in new_rows.items() if k not in old_rows}
    removed = {k: c for k, c in old_rows.items() if k not in new_rows}
    changed = {
        k: (old_rows[k], new_rows[k])
        for k in old_rows.keys() & new_rows.keys()
        if old_rows[k].value != new_rows[k].value
    }
    return TableDiff(table, added, removed, changed)


def diff_load_results(old: LoadResult, new: LoadResult) -> dict:
    """Diff two load results; tables without differences are omitted."""
    diffs = {}
    for name in old.table_names | new.table_names:
        diff = diff_tables(old.table(name), new.table(name), name)
        if not diff.is_empty:
            diffs[name] = diff
    return diffs
