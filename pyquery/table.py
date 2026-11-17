"""Structured table specifications with multi-level header resolution.

A specification table commonly has several header levels.  In a price table
the label ``Price`` may span two currencies, each of which spans several
plans, and the same label can appear below different parents.  Reading the
nearest row of text is therefore not enough to name a column.

This module builds the real table grid (honouring ``rowspan``, ``colspan``,
``thead``/``tbody``/``tfoot`` and ``colgroup``/``col``), and then computes,
for every data cell, the complete set of header cells that decide its
meaning.  Header relationships come from:

* ``headers`` references, on data and header cells alike;
* ``scope`` declarations (``col``, ``colgroup``, ``row``, ``rowgroup``);
* column-group and row-group declarations.

Declarations are authoritative and are marked ``"declared"``.  Inference is
opt-in through ``infer=``, runs only on an axis for which the cell has no
declared relationship, and every result is marked ``"inferred"``.  The
caller may pass ``True``, an axis name (``"column"``/``"row"``), a
collection of axis names, or a callable ``(element, axis, candidate)``.

Invalid declarations are never silently repaired.  An explicit reference
without a target, a reference cycle, and two mutually exclusive headers
attached to the same cell are all preserved in :attr:`TableSpec.conflicts`.
Lookups are scoped to the table being parsed: a header that exists only in
a nested table with the same ``id`` cannot satisfy an outer reference.

Every result keeps the original lxml elements and XPaths.  A record for a
data cell therefore points back to the data cell *and* to every header cell
and header path that decided its meaning.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field

from .text import extract_text

DECLARED = 'declared'
INFERRED = 'inferred'

COLUMN = 'column'
ROW = 'row'

_SCOPE_COLUMN = ('col', 'colgroup')
_SCOPE_ROW = ('row', 'rowgroup')
_SCOPE_VALUES = _SCOPE_COLUMN + _SCOPE_ROW
_ROW_SECTIONS = ('thead', 'tbody', 'tfoot')
_UNIT_ATTRIBUTES = (
    'data-unit',
    'data-currency',
    'data-field-unit',
    'unit',
    'currency',
)
_RESPONSIBILITY_ATTRIBUTES = (
    'data-field-role',
    'data-responsibility',
    'data-dimension',
    'data-role',
)
_UNIT_SPLIT_RE = re.compile(r'[,;/|]+')


def _local_name(node):
    tag = getattr(node, 'tag', '')
    if not isinstance(tag, str):
        return ''
    if tag.startswith('{'):
        tag = tag.split('}', 1)[1]
    return tag.lower()


def _element_text(node):
    if node is None:
        return ''
    text = extract_text(node, squash_space=False).strip()
    if not text:
        text = ''.join(node.itertext())
    return ' '.join(text.split())


def _parse_span(value, default=1):
    if value is None:
        return default
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _normalize_id(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _node_path(node):
    if node is None:
        return None
    try:
        return node.getroottree().getpath(node)
    except (AttributeError, ValueError):
        return None


def _node_html(node):
    if node is None:
        return None
    try:
        from lxml import etree
        return etree.tostring(node, encoding='unicode')
    except (ImportError, TypeError, ValueError):
        return None


def _iter_rows(table):
    """Yield ``(row, section)`` for rows belonging to *table* itself.

    Nested tables are skipped so their cells can never leak into the outer
    table's IDs, spans or header relationships.
    """
    rows = []

    def walk(node, section):
        for child in node:
            if not isinstance(getattr(child, 'tag', None), str):
                continue
            name = _local_name(child)
            if name == 'table':
                continue
            if name == 'tr':
                rows.append((child, section))
            elif name in _ROW_SECTIONS:
                walk(child, name)
            elif name in ('td', 'th', 'colgroup', 'col', 'caption'):
                continue
            else:
                walk(child, section)

    walk(table, None)
    return rows


def _row_cells(row):
    for child in row:
        if not isinstance(getattr(child, 'tag', None), str):
            continue
        if _local_name(child) in ('td', 'th'):
            yield child


def _declared_ids(element):
    raw = element.get('headers')
    if not raw:
        return []
    result = []
    for token in raw.split():
        if token not in result:
            result.append(token)
    return result


@dataclass
class _ColumnGroup:
    element: object
    start: int
    end: int
    columns: list = field(default_factory=list)

    def contains(self, column):
        return self.start <= column < self.end


@dataclass
class _Row:
    element: object
    section: str | None
    index: int
    cells: list = field(default_factory=list)


@dataclass
class _Cell:
    element: object
    row: int
    column: int
    rowspan: int
    colspan: int
    is_header: bool
    headers: list
    scope: str | None
    text: str
    section: str | None

    @property
    def row_end(self):
        return self.row + self.rowspan

    @property
    def column_end(self):
        return self.column + self.colspan

    @property
    def span(self):
        return (self.row, self.column, self.row_end, self.column_end)

    @property
    def ident(self):
        return _normalize_id(self.element.get('id'))

    def covers(self, row, column):
        return (self.row <= row < self.row_end and
                self.column <= column < self.column_end)


@dataclass
class _Link:
    cell: _Cell
    header: _Cell
    basis: str
    axis: str
    via: str


def _read_column_groups(table):
    """Read top-level ``colgroup``/``col`` declarations.

    A ``colgroup`` is one column group spanning all of its ``col``
    children; a bare top-level ``col`` is a group of its own.  ``span`` is
    honoured on both elements.
    """
    groups = []
    column = 0
    for child in table:
        if not isinstance(getattr(child, 'tag', None), str):
            continue
        name = _local_name(child)
        if name == 'colgroup':
            start = column
            columns = []
            for col in child:
                if _local_name(col) != 'col':
                    continue
                span = max(1, _parse_span(col.get('span'), 1))
                columns.append((column, column + span, col))
                column += span
            group_span = _parse_span(child.get('span'), 0)
            if group_span > 0:
                column = max(column, start + group_span)
            if not columns and group_span <= 0:
                column += 1
            groups.append(_ColumnGroup(child, start, column, columns))
        elif name == 'col':
            span = max(1, _parse_span(child.get('span'), 1))
            groups.append(_ColumnGroup(
                None, column, column + span, [(column, column + span, child)]))
            column += span
    return groups


class _Grid:
    def __init__(self, table):
        self.table = table
        self.rows = []
        self.cells = []
        self.by_id = {}
        self.column_groups = _read_column_groups(table)
        self._build()
        self.width = max(
            (cell.column_end for cell in self.cells), default=0)
        self.height = len(self.rows)
        for cell in self.cells:
            if cell.ident is not None:
                self.by_id.setdefault(cell.ident, []).append(cell)

    def _build(self):
        row_defs = _iter_rows(self.table)
        occupied = set()
        for index, (row_element, section) in enumerate(row_defs):
            row = _Row(row_element, section, index)
            self.rows.append(row)
            column = 0
            for element in _row_cells(row_element):
                while (index, column) in occupied:
                    column += 1
                rowspan = _parse_span(element.get('rowspan'), 1)
                colspan = _parse_span(element.get('colspan'), 1)
                if rowspan == 0:
                    if section is None:
                        rowspan = len(row_defs) - index
                    else:
                        rowspan = sum(
                            1 for _, other in row_defs[index:]
                            if other == section)
                if colspan == 0:
                    colspan = 1
                rowspan = max(1, rowspan)
                colspan = max(1, colspan)
                cell = _Cell(
                    element=element,
                    row=index,
                    column=column,
                    rowspan=rowspan,
                    colspan=colspan,
                    is_header=_local_name(element) == 'th',
                    headers=_declared_ids(element),
                    scope=(element.get('scope') or '').strip().lower() or None,
                    text=_element_text(element),
                    section=section,
                )
                row.cells.append(cell)
                self.cells.append(cell)
                for row_index in range(cell.row, cell.row_end):
                    for column_index in range(cell.column, cell.column_end):
                        occupied.add((row_index, column_index))
                column += cell.colspan

    def column_group_for(self, column):
        """Innermost declared column group containing *column*.

        A ``colgroup`` containing several ``col`` elements is one group;
        a request for one of its columns still returns the enclosing
        group.  When groups overlap, the narrowest one wins.
        """
        candidates = [
            group for group in self.column_groups
            if group.start <= column < group.end]
        if not candidates:
            return None
        return min(candidates, key=lambda group: group.end - group.start)


def _axis_between(header, cell):
    """Axis on which *header* qualifies *cell*.

    An explicit ``scope`` wins.  Otherwise a header above the cell is a
    column header and one to its left is a row header.  A header that is
    both above and left (a corner header) follows the axis on which it
    overlaps the cell; if it overlaps neither, it is treated as a row
    header because it occupies an earlier column.
    """
    if header.scope in _SCOPE_COLUMN:
        return COLUMN
    if header.scope in _SCOPE_ROW:
        return ROW

    above = header.row_end <= cell.row
    left = header.column_end <= cell.column
    column_overlap = _ranges_overlap(
        header.column, header.column_end, cell.column, cell.column_end)

    if above and left:
        return COLUMN if column_overlap else ROW
    if above:
        return COLUMN
    if left:
        return ROW
    return 'unknown'


def _scope_targets(grid, header):
    """Cells covered by one ``scope`` declaration.

    Header cells are included: a parent header must be able to declare the
    subheaders that sit below it even when inference is disabled.
    """
    scope = header.scope
    targets = []

    if scope == 'col':
        for row in grid.rows:
            if row.index < header.row_end:
                continue
            for cell in row.cells:
                if cell is not header and _ranges_overlap(
                        header.column, header.column_end,
                        cell.column, cell.column_end):
                    if cell.is_header and cell.scope in _SCOPE_ROW:
                        continue
                    targets.append(cell)

    elif scope == 'colgroup':
        # The declaring header's own colspan defines the column group it
        # labels.  Reading the enclosing <colgroup> would make a header
        # cover sibling groups that merely share the same colgroup element.
        start, end = header.column, header.column_end
        for row in grid.rows:
            if row.index < header.row_end:
                continue
            for cell in row.cells:
                if cell is not header and _ranges_overlap(
                        start, end, cell.column, cell.column_end):
                    if cell.is_header and cell.scope in _SCOPE_ROW:
                        continue
                    targets.append(cell)

    elif scope == 'row':
        for row in grid.rows:
            if not _ranges_overlap(
                    header.row, header.row_end, row.index, row.index + 1):
                continue
            for cell in row.cells:
                if cell is not header and cell.column >= header.column_end:
                    if cell.is_header and cell.scope in _SCOPE_COLUMN:
                        continue
                    targets.append(cell)

    elif scope == 'rowgroup':
        rows = _group_rows(grid, header)
        for row in rows:
            for cell in row.cells:
                if cell is not header and cell.column >= header.column:
                    if cell.is_header and cell.scope in _SCOPE_COLUMN:
                        continue
                    if cell.row == header.row:
                        continue
                    targets.append(cell)

    return targets


def _group_rows(grid, header):
    """Rows belonging to the row group declared by *header*.

    A ``rowgroup`` spans the enclosing section when one exists.  For a
    table without explicit sections the group is the set of rows from the
    declaring row onward, bounded by any other ``rowgroup`` declaration.
    """
    if header.section is not None:
        return [row for row in grid.rows if row.section == header.section]
    return [row for row in grid.rows if row.index >= header.row]


def _ranges_overlap(start, end, other_start, other_end):
    return start < other_end and other_start < end


def _declared_links(grid):
    links = {id(cell): [] for cell in grid.cells}

    for cell in grid.cells:
        for ident in cell.headers:
            for header in grid.by_id.get(ident, ()):
                links[id(cell)].append(_Link(
                    cell, header, DECLARED, _axis_between(header, cell),
                    'headers'))

    for header in grid.cells:
        if not header.is_header or header.scope not in _SCOPE_VALUES:
            continue
        axis = COLUMN if header.scope in _SCOPE_COLUMN else ROW
        for target in _scope_targets(grid, header):
            links[id(target)].append(_Link(
                target, header, DECLARED, axis, f'scope-{header.scope}'))

    return links


def _nearest_above(grid, cell):
    """Nearest header above the first column covered by *cell*."""
    candidates = [
        other for other in grid.cells
        if other is not cell and other.is_header
        and other.row_end <= cell.row
        and other.column <= cell.column < other.column_end
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item.row_end, item.column))
    return candidates[-1]


def _nearest_above_all(grid, cell):
    """Nearest header above every column covered by *cell*.

    A data cell with ``colspan=3`` covers three header columns and must
    report all three deciding headers, not just the one above its first
    column.
    """
    result = []
    seen = set()
    for column in range(cell.column, cell.column_end):
        candidates = [
            other for other in grid.cells
            if other is not cell and other.is_header
            and other.row_end <= cell.row
            and other.column <= column < other.column_end
        ]
        if not candidates:
            continue
        candidates.sort(key=lambda item: (item.row_end, item.column))
        header = candidates[-1]
        if id(header) not in seen:
            seen.add(id(header))
            result.append(header)
    return result


def _nearest_left_all(grid, cell):
    result = []
    seen = set()
    for row in range(cell.row, cell.row_end):
        candidates = [
            other for other in grid.cells
            if other is not cell and other.is_header
            and other.row <= row < other.row_end
            and other.column_end <= cell.column
        ]
        if not candidates:
            continue
        candidates.sort(key=lambda item: (item.column_end, item.row))
        header = candidates[-1]
        if id(header) not in seen:
            seen.add(id(header))
            result.append(header)
    return result


def _inference_allowed(infer, axis):
    if infer is True:
        return True
    if infer in (False, None):
        return False
    if isinstance(infer, str):
        return infer in ('all', axis)
    if isinstance(infer, (list, tuple, set, frozenset)):
        return 'all' in infer or axis in infer
    if callable(infer):
        return None  # decided per candidate below
    raise ValueError(
        'infer must be True, False, an axis name, a collection of axis '
        'names, or a callable')


def _candidate_allowed(infer, cell, axis, candidate):
    if callable(infer):
        return bool(infer(cell.element, axis, candidate.element))
    return _inference_allowed(infer, axis)


def _ancestors(grid, links, infer):
    """Declared or caller-allowed ancestors for every header cell.

    Header nesting is a column-axis concept: an upper header qualifies the
    headers below it.  Headers sitting side by side in one heading row are
    siblings, never parent and child, so no row-axis ancestor is inferred
    for header cells.
    """
    result = {id(cell): [] for cell in grid.cells}
    for cell in grid.cells:
        if not cell.is_header:
            continue
        own = list(links.get(id(cell), ()))
        axes = {link.axis for link in own}
        if COLUMN not in axes:
            above = _nearest_above(grid, cell)
            if above is not None and _candidate_allowed(
                    infer, cell, COLUMN, above):
                own.append(_Link(
                    cell, above, INFERRED, COLUMN, 'structure'))
        result[id(cell)] = own
    return result


def _header_chain(header, ancestors, axis):
    """Root-to-header list of links, following nearest qualifying parents.

    ``scope`` covers every descendant, so the declared links are
    deliberately not used here: a header's *nesting* parent is the closest
    header above it on the column axis.  An explicit ``headers``
    declaration still wins when it names such a parent.
    """
    chain = []
    seen = {id(header)}
    current = header

    while True:
        link = _parent_link(current, ancestors, axis)
        if link is None:
            break
        parent = link.header
        if id(parent) in seen:
            break
        seen.add(id(parent))
        chain.append(link)
        current = parent

    chain.reverse()
    return chain


def _parent_link(header, ancestors, axis):
    links = [
        link for link in ancestors.get(id(header), ())
        if link.axis == axis]
    if not links:
        return None

    def distance(link):
        candidate = link.header
        if axis == COLUMN:
            return (candidate.row_end, candidate.column)
        return (candidate.column_end, candidate.row)

    return max(links, key=distance)


def _unit_values(header):
    values = set()
    for name in _UNIT_ATTRIBUTES:
        raw = header.element.get(name)
        if not raw:
            continue
        for token in _UNIT_SPLIT_RE.split(raw):
            token = token.strip().casefold()
            if token:
                values.add(token)
    return values


def _declared_responsibility(header):
    """Responsibility explicitly declared on a header cell, if any."""
    for name in _RESPONSIBILITY_ATTRIBUTES:
        raw = header.element.get(name)
        if raw and raw.strip():
            return raw.strip()
    return None


def _declared_responsibility_from_element(element):
    for name in _RESPONSIBILITY_ATTRIBUTES:
        raw = element.get(name)
        if raw and raw.strip():
            return raw.strip()
    return None


def _responsibility(header, axis, basis):
    declared = _declared_responsibility(header)
    if declared is not None:
        return declared, DECLARED
    scope = header.scope
    if scope in _SCOPE_COLUMN:
        return 'column', DECLARED
    if scope in _SCOPE_ROW:
        return 'row', DECLARED
    if axis == COLUMN:
        return 'column', basis
    if axis == ROW:
        return 'row', basis
    return None, basis


def _structural_ancestors(grid):
    """Structural nesting of header cells, independent of ``infer``.

    This is used only to decide whether two *declared* headers are
    compatible; it never adds a relationship to a cell's reported header
    path.  Header nesting follows the column axis only: the nearest header
    above a header is its parent, while headers to its left are siblings.
    """
    result = {id(cell): [] for cell in grid.cells}
    for cell in grid.cells:
        if not cell.is_header:
            continue
        above = _nearest_above(grid, cell)
        if above is not None:
            result[id(cell)].append(
                _Link(cell, above, DECLARED, COLUMN, 'structure'))
    return result


def _sibling_headers_conflict(cell, left, right, structural):
    """Detect two headers that qualify the cell incompatibly.

    Two headers are compatible when one is the structural parent of the
    other (ordinary multi-level nesting).  They conflict when they share a
    parent but are different siblings, or when they sit in unrelated
    branches and both are explicitly attached to the same cell.
    """
    if left.axis != right.axis:
        return False
    if left.header is right.header:
        return False
    if left.header.text == right.header.text:
        return False

    left_link = _parent_link(left.header, structural, left.axis)
    right_link = _parent_link(right.header, structural, right.axis)
    left_parent = left_link.header if left_link is not None else None
    right_parent = right_link.header if right_link is not None else None

    if left.header is right_parent or right.header is left_parent:
        return False
    if left_parent is right_parent and left_parent is not None:
        return True
    if left_parent is None and right_parent is None:
        return left.via == 'headers' and right.via == 'headers'
    if left.via == 'headers' and right.via == 'headers':
        return True
    return False


def _conflicts(grid, declared, ancestors):
    structural = _structural_ancestors(grid)
    conflicts = []

    for cell in grid.cells:
        for ident in cell.headers:
            targets = grid.by_id.get(ident, ())
            if not targets:
                conflicts.append(Conflict(
                    'missing-header',
                    cell.element,
                    'headers',
                    f'headers reference {ident!r} has no target in this '
                    f'table',
                    [ident],
                ))
            elif len(targets) > 1:
                conflicts.append(Conflict(
                    'duplicate-header-id',
                    cell.element,
                    'headers',
                    f'headers reference {ident!r} resolves to '
                    f'{len(targets)} cells in this table',
                    [ident],
                ))

    for cycle in _cycles(grid):
        ids = [cell.ident for cell in cycle]
        for cell in cycle:
            conflicts.append(Conflict(
                'header-cycle',
                cell.element,
                'headers',
                'headers references form a cycle',
                ids,
            ))

    for cell in grid.cells:
        links = [
            link for link in declared.get(id(cell), ())
            if link.basis == DECLARED]
        seen = set()
        for index, left in enumerate(links):
            for right in links[index + 1:]:
                if left.header is right.header or left.axis != right.axis:
                    continue
                pair = tuple(sorted((id(left.header), id(right.header))))
                if pair in seen:
                    continue
                seen.add(pair)

                left_units = _unit_values(left.header)
                right_units = _unit_values(right.header)
                unit_conflict = bool(
                    left_units and right_units and
                    left_units.isdisjoint(right_units))
                sibling_conflict = _sibling_headers_conflict(
                    cell, left, right, structural)

                if not unit_conflict and not sibling_conflict:
                    continue

                kind = 'unit-conflict' if unit_conflict else 'header-conflict'
                detail = (
                    f'cell {_node_path(cell.element)!r} receives mutually '
                    f'exclusive headers {left.header.text!r} and '
                    f'{right.header.text!r} on the {left.axis} axis'
                )
                conflicts.append(Conflict(
                    kind,
                    cell.element,
                    'headers',
                    detail,
                    [
                        item.ident or _node_path(item.element)
                        for item in (left.header, right.header)
                    ],
                ))

    return conflicts


def _cycles(grid):
    """Tarjan strongly-connected components of the ``headers`` graph."""
    graph = {id(cell): [] for cell in grid.cells}
    for cell in grid.cells:
        for ident in cell.headers:
            graph[id(cell)].extend(grid.by_id.get(ident, ()))

    index = [0]
    stack = []
    on_stack = set()
    indexes = {}
    lowlinks = {}
    components = []

    def strongconnect(cell):
        indexes[id(cell)] = index[0]
        lowlinks[id(cell)] = index[0]
        index[0] += 1
        stack.append(cell)
        on_stack.add(id(cell))

        for target in graph[id(cell)]:
            if id(target) not in indexes:
                strongconnect(target)
                lowlinks[id(cell)] = min(
                    lowlinks[id(cell)], lowlinks[id(target)])
            elif id(target) in on_stack:
                lowlinks[id(cell)] = min(
                    lowlinks[id(cell)], indexes[id(target)])

        if lowlinks[id(cell)] == indexes[id(cell)]:
            component = []
            while True:
                member = stack.pop()
                on_stack.discard(id(member))
                component.append(member)
                if member is cell:
                    break
            if len(component) > 1:
                components.append(component)
            elif any(target is cell for target in graph[id(cell)]):
                components.append(component)

    for cell in grid.cells:
        if id(cell) not in indexes:
            strongconnect(cell)
    return components


@dataclass
class HeaderRef:
    """One header cell in a data cell's real header path.

    ``basis`` records whether the *relationship* to the data cell was
    declared by markup or inferred.  ``responsibility`` and
    ``responsibility_basis`` separately record the field duty of the
    header (for example ``price`` or ``currency``); the duty also comes
    from a declaration when one exists, and is otherwise inferred.
    """

    label: str
    node: object
    id: str | None = None
    basis: str = DECLARED
    axis: str | None = None
    scope: str | None = None
    xpath: str | None = None
    span: tuple | None = None
    role: str | None = None
    via: str | None = None
    depth: int = 0
    path: list = field(default_factory=list)
    responsibility: str | None = None
    responsibility_basis: str = DECLARED
    units: tuple = ()
    units_basis: str = DECLARED

    @property
    def text(self):
        return self.label

    @property
    def value(self):
        return self.label

    @property
    def element(self):
        return self.node

    @property
    def declared(self):
        return self.basis == DECLARED

    @property
    def inferred(self):
        return self.basis == INFERRED

    @property
    def field_role(self):
        return self.responsibility

    @property
    def header_path(self):
        return list(self.path)

    def as_dict(self):
        return OrderedDict([
            ('label', self.label),
            ('id', self.id),
            ('basis', self.basis),
            ('axis', self.axis),
            ('scope', self.scope),
            ('xpath', self.xpath),
            ('span', self.span),
            ('role', self.role),
            ('responsibility', self.responsibility),
            ('responsibility_basis', self.responsibility_basis),
            ('units', list(self.units)),
            ('units_basis', self.units_basis),
            ('via', self.via),
            ('depth', self.depth),
            ('header_path', list(self.path)),
        ])


@dataclass
class Conflict:
    """A declaration preserved because it cannot be resolved safely."""

    kind: str
    cell: object
    declaration: str
    detail: str
    headers: list = field(default_factory=list)

    @property
    def node(self):
        return self.cell

    @property
    def cell_path(self):
        return _node_path(self.cell)

    @property
    def message(self):
        return self.detail

    def as_dict(self):
        return OrderedDict([
            ('kind', self.kind),
            ('cell', self.cell_path),
            ('declaration', self.declaration),
            ('detail', self.detail),
            ('headers', list(self.headers)),
        ])


@dataclass
class CellSpec:
    """One cell of a table specification, with full provenance."""

    value: str
    node: object
    row: int
    column: int
    rowspan: int
    colspan: int
    headers: list = field(default_factory=list)
    role: str = 'data'
    section: str | None = None
    conflicts: list = field(default_factory=list)

    @property
    def text(self):
        return self.value

    @property
    def element(self):
        return self.node

    @property
    def tag(self):
        return _local_name(self.node)

    @property
    def id(self):
        return _normalize_id(self.node.get('id'))

    @property
    def xpath(self):
        return _node_path(self.node)

    @property
    def html(self):
        return _node_html(self.node)

    @property
    def span(self):
        return (self.row, self.column, self.row + self.rowspan,
                self.column + self.colspan)

    @property
    def coordinates(self):
        return (self.row, self.column)

    @property
    def header_path(self):
        """Labels of every header that decides this cell, in path order."""
        return [item.label for item in self.headers]

    @property
    def header_paths(self):
        """Every distinct root-to-leaf header path for this cell."""
        paths = []
        for item in self.headers:
            path = list(item.path)
            if path and path not in paths:
                paths.append(path)
        return paths

    @property
    def column_headers(self):
        return [item for item in self.headers if item.axis == COLUMN]

    @property
    def row_headers(self):
        return [item for item in self.headers if item.axis == ROW]

    @property
    def column_path(self):
        return self._axis_path(COLUMN)

    @property
    def row_path(self):
        return self._axis_path(ROW)

    def _axis_path(self, axis):
        return [
            item.label for item in self.headers if item.axis == axis]

    @property
    def declared_headers(self):
        return [item for item in self.headers if item.declared]

    @property
    def inferred_headers(self):
        return [item for item in self.headers if item.inferred]

    @property
    def field_name(self):
        if self.column_headers:
            return self.column_headers[-1].label
        if self.row_headers:
            return self.row_headers[-1].label
        return None

    @property
    def field_path(self):
        if self.column_headers:
            return self.column_path
        return self.row_path

    @property
    def field_role(self):
        """The data field's responsibility, declared or inferred.

        A data cell carries a value, declared by its element type.  An
        explicit ``data-field-role``/``data-responsibility`` attribute
        overrides that.
        """
        declared = _declared_responsibility_from_element(self.node)
        if declared is not None:
            return declared
        if self.role == 'data':
            return 'value'
        return self.role

    @property
    def field_role_basis(self):
        if _declared_responsibility_from_element(self.node) is not None:
            return DECLARED
        if self.role == 'data':
            return DECLARED
        return DECLARED

    @property
    def responsibilities(self):
        """Ordered list of every declared/inferred duty in the path."""
        result = []
        for item in self.headers:
            result.append(OrderedDict([
                ('header', item.label),
                ('responsibility', item.responsibility),
                ('basis', item.responsibility_basis),
                ('units', list(item.units)),
                ('units_basis', item.units_basis),
                ('path', list(item.path)),
            ]))
        if self.role == 'data':
            result.append(OrderedDict([
                ('header', None),
                ('responsibility', self.field_role),
                ('basis', self.field_role_basis),
                ('units', self._own_units),
                ('units_basis', DECLARED if self._own_units else None),
                ('path', self.field_path),
            ]))
        return result

    @property
    def header_roles(self):
        """Mapping of each deciding header label to its responsibility."""
        roles = OrderedDict()
        for item in self.headers:
            if item.label not in roles:
                roles[item.label] = OrderedDict([
                    ('responsibility', item.responsibility),
                    ('basis', item.responsibility_basis),
                ])
        return roles

    @property
    def declared_field(self):
        """``True`` when the field role came from an explicit declaration."""
        return self.field_role_basis == DECLARED

    @property
    def field(self):
        return OrderedDict([
            ('name', self.field_name),
            ('role', self.field_role),
            ('role_basis', self.field_role_basis),
            ('path', self.field_path),
        ])

    @property
    def units(self):
        units = list(self._own_units)
        for item in self.headers:
            for unit in item.units:
                if unit not in units:
                    units.append(unit)
        return units

    @property
    def _own_units(self):
        values = set()
        for name in _UNIT_ATTRIBUTES:
            raw = self.node.get(name)
            if not raw:
                continue
            for token in _UNIT_SPLIT_RE.split(raw):
                token = token.strip().casefold()
                if token:
                    values.add(token)
        return sorted(values)

    @property
    def units_basis(self):
        if self._own_units:
            return DECLARED
        if not self.units:
            return None
        bases = {item.units_basis for item in self.headers if item.units}
        if DECLARED in bases:
            return DECLARED
        return INFERRED

    @property
    def source(self):
        return OrderedDict([
            ('tag', self.tag),
            ('id', self.id),
            ('xpath', self.xpath),
            ('row', self.row),
            ('column', self.column),
            ('rowspan', self.rowspan),
            ('colspan', self.colspan),
            ('span', self.span),
            ('html', self.html),
        ])

    @property
    def record(self):
        return OrderedDict([
            ('value', self.value),
            ('field', self.field),
            ('units', self.units),
            ('units_basis', self.units_basis),
            ('header_path', self.header_path),
            ('header_paths', self.header_paths),
            ('headers', [item.as_dict() for item in self.headers]),
            ('source', self.source),
            ('conflicts', [dict(item) for item in self.conflicts]),
        ])

    @property
    def is_data(self):
        return self.role == 'data'

    @property
    def is_header(self):
        return self.role in ('header', 'column-header', 'row-header',
                             'corner')

    def as_dict(self):
        return OrderedDict([
            ('value', self.value),
            ('tag', self.tag),
            ('id', self.id),
            ('xpath', self.xpath),
            ('html', self.html),
            ('row', self.row),
            ('column', self.column),
            ('rowspan', self.rowspan),
            ('colspan', self.colspan),
            ('span', self.span),
            ('role', self.role),
            ('field_role', self.field_role),
            ('field_role_basis', self.field_role_basis),
            ('units', self.units),
            ('units_basis', self.units_basis),
            ('section', self.section),
            ('header_path', self.header_path),
            ('header_paths', self.header_paths),
            ('column_path', self.column_path),
            ('row_path', self.row_path),
            ('headers', [item.as_dict() for item in self.headers]),
            ('conflicts', [dict(item) for item in self.conflicts]),
        ])

    def __getitem__(self, name):
        if name == 'headers':
            return self.headers
        if name == 'header_path':
            return self.header_path
        if name == 'node':
            return self.node
        return getattr(self, name)


class TableSpec:
    """Structured, provenance-preserving view of one HTML table."""

    def __init__(self, table, cells, header_cells, conflicts, width, height,
                 infer=False):
        self.table = table
        self.cells = cells
        self.header_cells = header_cells
        self.conflicts = conflicts
        self.width = width
        self.height = height
        self.infer = infer

    @property
    def node(self):
        return self.table

    @property
    def element(self):
        return self.table

    @property
    def xpath(self):
        return _node_path(self.table)

    @property
    def id(self):
        return _normalize_id(self.table.get('id'))

    @property
    def rows(self):
        return self.height

    @property
    def columns(self):
        return self.width

    @property
    def all_cells(self):
        return list(self.cells) + list(self.header_cells)

    @property
    def records(self):
        return [cell.record for cell in self.cells]

    def __iter__(self):
        return iter(self.cells)

    def __len__(self):
        return len(self.cells)

    def __getitem__(self, index):
        if isinstance(index, tuple) and len(index) == 2:
            return self.cell(*index)
        return self.cells[index]

    def cell(self, row, column):
        for cell in self.all_cells:
            if (cell.row <= row < cell.row + cell.rowspan and
                    cell.column <= column < cell.column + cell.colspan):
                return cell
        return None

    cell_at = cell

    def data_cell(self, row, column):
        for cell in self.cells:
            if (cell.row <= row < cell.row + cell.rowspan and
                    cell.column <= column < cell.column + cell.colspan):
                return cell
        return None

    def row(self, index):
        return [cell for cell in self.cells if cell.row == index]

    def column(self, index):
        return [cell for cell in self.cells
                if cell.column <= index < cell.column + cell.colspan]

    def header_path(self, row, column):
        cell = self.cell(row, column)
        return [] if cell is None else cell.header_path

    def as_dict(self):
        return OrderedDict([
            ('id', self.id),
            ('xpath', self.xpath),
            ('width', self.width),
            ('height', self.height),
            ('infer', self.infer),
            ('header_cells', [
                cell.as_dict() for cell in self.header_cells]),
            ('cells', [cell.as_dict() for cell in self.cells]),
            ('records', self.records),
            ('conflicts', [item.as_dict() for item in self.conflicts]),
        ])

    def to_dict(self):
        return self.as_dict()

    def __repr__(self):
        ident = f'#{self.id}' if self.id else ''
        return (
            f'<TableSpec{ident} {self.height}x{self.width} '
            f'{len(self.cells)} data cells>'
        )


def _refs_for(cell, direct_links, ancestors):
    refs = []
    seen = set()
    for link in direct_links:
        axis = link.axis
        chain = _header_chain(link.header, ancestors, axis)
        steps = chain + [link]
        path = [step.header.text for step in chain]
        path.append(link.header.text)
        for depth, step in enumerate(steps):
            key = (id(step.header), step.basis, axis)
            if key in seen:
                continue
            seen.add(key)
            responsibility, responsibility_basis = _responsibility(
                step.header, axis, step.basis)
            units = _unit_values(step.header)
            declared_units = any(
                step.header.element.get(name) for name in _UNIT_ATTRIBUTES)
            refs.append(HeaderRef(
                label=step.header.text,
                node=step.header.element,
                id=step.header.ident,
                basis=step.basis,
                axis=axis,
                scope=step.header.scope,
                xpath=_node_path(step.header.element),
                span=step.header.span,
                role=f'{axis}-header',
                via=step.via,
                depth=depth,
                path=path,
                responsibility=responsibility,
                responsibility_basis=responsibility_basis,
                units=tuple(sorted(units)),
                units_basis=DECLARED if declared_units else INFERRED,
            ))
    refs.sort(key=lambda item: (
        0 if item.axis == COLUMN else 1,
        item.span[0] if item.span else 0,
        item.span[1] if item.span else 0,
    ))
    return refs


def _infer_direct(grid, cell, infer, links):
    """Add inferred links only on axes with no resolved declaration.

    A resolved ``headers``/``scope`` relationship on one axis blocks
    inference on that axis but not on the other.  A broken declaration
    (missing target, cycle) does not supply a value, so inference may still
    fill that axis when the caller allows it; the conflict remains in the
    specification either way.
    """
    if cell.is_header:
        return links
    axes = {link.axis for link in links if link.basis == DECLARED}
    result = list(links)
    if COLUMN not in axes:
        for above in _nearest_above_all(grid, cell):
            if _candidate_allowed(infer, cell, COLUMN, above):
                result.append(_Link(
                    cell, above, INFERRED, COLUMN, 'structure'))
    if ROW not in axes:
        for left in _nearest_left_all(grid, cell):
            if _candidate_allowed(infer, cell, ROW, left):
                result.append(_Link(
                    cell, left, INFERRED, ROW, 'structure'))
    return result


def _cell_role(cell):
    if cell.is_header:
        if cell.scope in _SCOPE_COLUMN:
            return 'column-header'
        if cell.scope in _SCOPE_ROW:
            return 'row-header'
        return 'header'
    return 'data'


def _role_from_refs(cell, refs):
    """Refine a header's role from how descendants actually use it."""
    if not cell.is_header:
        return 'data'
    axes = {ref.axis for ref in refs}
    if axes == {COLUMN}:
        return 'column-header'
    if axes == {ROW}:
        return 'row-header'
    if axes == {COLUMN, ROW}:
        return 'corner'
    return _cell_role(cell)


def parse_table(table, infer=False):
    """Parse one ``<table>`` element into a :class:`TableSpec`.

    ``infer`` is disabled by default.  Enable it only for relationships
    the caller considers safe; inferred entries are marked as such.
    """
    if _local_name(table) != 'table':
        raise ValueError('parse_table expects a <table> element')

    grid = _Grid(table)
    declared = _declared_links(grid)
    ancestors = _ancestors(grid, declared, infer)

    refs_for = {}
    for cell in grid.cells:
        direct = _infer_direct(grid, cell, infer, declared.get(id(cell), ()))
        refs_for[id(cell)] = _refs_for(cell, direct, ancestors)

    conflicts = _conflicts(grid, declared, ancestors)
    cell_by_element = {id(cell.element): cell for cell in grid.cells}
    per_cell = {}
    for conflict in conflicts:
        owner = cell_by_element.get(id(conflict.cell))
        if owner is not None:
            per_cell.setdefault(id(owner), []).append(
                conflict.as_dict())
    data_cells = []
    header_cells = []
    for cell in grid.cells:
        refs = refs_for[id(cell)]
        cell_conflicts = per_cell.get(id(cell), [])
        if cell.is_header:
            role = _role_from_refs(cell, refs)
        else:
            role = 'data'
        spec = CellSpec(
            value=cell.text,
            node=cell.element,
            row=cell.row,
            column=cell.column,
            rowspan=cell.rowspan,
            colspan=cell.colspan,
            headers=refs,
            role=role,
            section=cell.section,
            conflicts=cell_conflicts,
        )
        if cell.is_header:
            header_cells.append(spec)
        else:
            data_cells.append(spec)

    return TableSpec(
        table,
        data_cells,
        header_cells,
        conflicts,
        grid.width,
        grid.height,
        infer=infer,
    )


def parse_tables(node, infer=False):
    """Yield a :class:`TableSpec` for every table below ``node``."""
    for table in node.iter():
        if _local_name(table) == 'table':
            yield parse_table(table, infer=infer)


def table_cells(table, infer=False):
    """Yield the data cells of one table."""
    yield from parse_table(table, infer=infer).cells


def table_header_path(table, row, column, infer=False):
    """Return the effective header labels for one grid slot."""
    return parse_table(table, infer=infer).header_path(row, column)
