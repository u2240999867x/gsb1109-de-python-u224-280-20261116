"""Convert supplier parameter tables with multi-level headers to Pyomo long tables.

A supplier parameter table is a raw grid whose top rows and left columns hold
multi-level headers.  Header cells may be left blank to inherit the previous
header (merged-cell style).  Body cells may be blank either because an
observation is missing or because the supplier explicitly published an empty
value; the caller supplies the marker conventions that tell the two apart.

The converter produces a long (tidy) table in which every emitted value keeps
full provenance: the source body cell and the exact header cells each index
label and the parameter name were taken from.

Design rules implemented here (mirroring the supply contract):

* Repeated header labels are distinguished only through occurrence identity
  ``(label, occurrence)`` -- never by concatenating strings into a new key.
  If a level contains duplicate labels and the spec does not grant occurrence
  identity for that level, the governed body region is ambiguous and is
  excluded from the model.
* When several tables reference the same named index set, members and
  dimension are verified jointly through :class:`IndexSetRegistry`.  Each
  table resolves its own header layout; a previous table's column order is
  never reused just because the shapes match.
* :func:`audit_back_mapping` walks the published rows back to the raw grid
  and checks value and header responsibility cell by cell.  Regions that
  cannot be interpreted uniquely never enter the model output.

Spec format (plain dict)::

    {
        "name": "supply",                 # table name (default param name)
        "header_rows": 2,                 # number of top header rows
        "header_cols": 1,                 # number of left header columns
        "inherit": {"rows": True, "cols": True},   # blank-header inheritance
        "index_roles": {
            "rows": [{"level": 0, "set": "PLANT"}],
            "cols": [{"level": 0, "set": "QUARTER"}],
            "param": {"level": 1},        # optional: header row naming params
        },
        "occurrence_identity": {"rows": [], "cols": [0]},
        "null_conventions": {
            "missing": [None, ""],        # markers for missing observations
            "explicit_empty": ["NA"],     # markers for explicit empty values
        },
    }
"""

from dataclasses import dataclass, field
from typing import Any, Hashable, Optional

MISSING = "missing"
EXPLICIT_EMPTY = "explicit_empty"
VALUE = "value"

REASON_MISSING = "missing_observation"
REASON_AMBIGUOUS = "ambiguous_header"
REASON_UNRESOLVED = "unresolved_header"

DEFAULT_MISSING = (None, "")
DEFAULT_EXPLICIT_EMPTY = ()


class TableSpecError(ValueError):
    """The conversion spec is inconsistent or incomplete."""


class IndexSetMismatchError(ValueError):
    """Two tables disagree on the members or dimension of a shared index set."""


class DuplicateIndexError(ValueError):
    """Published rows collide on the same key; refusing to overwrite silently."""


@dataclass(frozen=True)
class HeaderRef:
    """One expanded header entry for one body row/column."""

    label: Optional[Hashable]
    cell: tuple  # (row, col) of the origin header cell in the raw grid
    occurrence: int  # 1-based ordinal among same-label header cells; 0 if unresolved


@dataclass(frozen=True)
class LongRow:
    """One published value with full provenance."""

    param: Hashable
    index: tuple  # plain labels, one per index role
    index_identity: tuple  # (label, occurrence) pairs, aligned with ``index``
    value: Any  # None when status == EXPLICIT_EMPTY
    status: str  # VALUE or EXPLICIT_EMPTY
    cell: tuple  # (row, col) of the source body cell in the raw grid
    header_cells: tuple  # origin header cells, aligned with ``index_identity``
    param_cell: Optional[tuple]  # origin header cell of the param name, if any
    param_identity: tuple  # (label, occurrence) of the param header


@dataclass(frozen=True)
class ExcludedCell:
    """A body cell that does not enter the model, with the reason."""

    cell: tuple
    reason: str  # REASON_MISSING / REASON_AMBIGUOUS / REASON_UNRESOLVED
    detail: str = ""


@dataclass
class LongTable:
    """Pyomo-loadable long table plus provenance and exclusions."""

    name: str
    rows: list = field(default_factory=list)
    excluded: list = field(default_factory=list)
    index_sets: dict = field(default_factory=dict)  # set name -> ordered labels
    index_set_identities: dict = field(default_factory=dict)  # name -> (label, occ)
    params: tuple = ()
    # True when the param header level is granted occurrence identity, in
    # which case repeated param labels are distinct parameters.
    param_occurrence_identity: bool = False

    def to_param_data(self, use_identity=False, include_empty=False):
        """Return ``{param_key: {index_key: value}}`` for Pyomo initialisation.

        Explicit-empty entries stay in :attr:`rows` for provenance but are not
        numeric model data, so they are omitted unless ``include_empty`` is
        set.  Colliding keys raise :class:`DuplicateIndexError` instead of
        silently overwriting -- repeated headers must be told apart through
        occurrence identity (``use_identity=True``), never by string
        concatenation.
        """
        data = {}
        for row in self.rows:
            if row.status == EXPLICIT_EMPTY and not include_empty:
                continue
            if use_identity and self.param_occurrence_identity:
                param_key = row.param_identity
            else:
                param_key = row.param
            index_key = row.index_identity if use_identity else row.index
            pdata = data.setdefault(param_key, {})
            if index_key in pdata:
                raise DuplicateIndexError(
                    f"param {param_key!r}: duplicate index key {index_key!r} "
                    f"from cells {pdata[index_key].cell} and {row.cell}; "
                    "distinguish repeated headers by occurrence identity"
                    if not use_identity
                    else f"param {param_key!r}: duplicate index key {index_key!r}"
                )
            pdata[index_key] = row
        return {param: {k: r.value for k, r in pdata.items()} for param, pdata in data.items()}


class IndexSetRegistry:
    """Joint verification of index sets shared across supplier tables.

    The registry only checks members and dimension; it never hands one
    table's column order to another table.
    """

    def __init__(self):
        self._sets = {}

    @staticmethod
    def _dim(members):
        if not members:
            return 0
        first = members[0]
        return len(first) if isinstance(first, tuple) else 1

    def sync(self, name, members):
        """Register ``members`` under ``name`` or verify against the known set."""
        members = tuple(members)
        dim = self._dim(members)
        if name not in self._sets:
            self._sets[name] = members
            return members
        existing = self._sets[name]
        if self._dim(existing) != dim:
            raise IndexSetMismatchError(
                f"index set {name!r}: dimension {dim} does not match "
                f"registered dimension {self._dim(existing)}"
            )
        if set(existing) != set(members):
            missing = set(existing) - set(members)
            extra = set(members) - set(existing)
            raise IndexSetMismatchError(
                f"index set {name!r}: member mismatch "
                f"(missing={sorted(map(repr, missing))}, extra={sorted(map(repr, extra))})"
            )
        return existing

    def members(self, name):
        return self._sets[name]


def _coerce(raw):
    """Best-effort numeric coercion for string cells."""
    if isinstance(raw, str):
        text = raw.strip()
        try:
            return int(text)
        except ValueError:
            pass
        try:
            return float(text)
        except ValueError:
            pass
        return raw
    return raw


def _interpret(raw, missing_markers, empty_markers):
    """Classify a raw body cell under the null conventions."""
    for marker in missing_markers:
        if raw is marker or raw == marker:
            return MISSING, None
    for marker in empty_markers:
        if raw is marker or raw == marker:
            return EXPLICIT_EMPTY, None
    return VALUE, _coerce(raw)


def _expand_level(raw_labels, origin_cells, inherit):
    """Expand one header level with previous-cell inheritance.

    ``raw_labels`` are the labels along the level, ``origin_cells`` the grid
    cells they sit in.  Returns a list of :class:`HeaderRef`.  A blank cell
    inherits label and origin cell of the previous non-blank cell when
    ``inherit`` is true; otherwise it stays unresolved.
    """
    expanded = []
    last = None
    for pos, raw in enumerate(raw_labels):
        if raw is None or raw == "":
            if inherit and last is not None:
                expanded.append(last)
            else:
                expanded.append((None, origin_cells[pos]))
        else:
            last = (raw, origin_cells[pos])
            expanded.append(last)
    # Occurrence identity: rank of each origin cell among the distinct origin
    # cells carrying the same label, in order of first appearance.  A merged
    # span shares one origin cell and therefore one occurrence.
    origins_by_label = {}
    for label, cell in expanded:
        if label is None:
            continue
        origins_by_label.setdefault(label, [])
        if cell not in origins_by_label[label]:
            origins_by_label[label].append(cell)
    refs = []
    for label, cell in expanded:
        if label is None:
            refs.append(HeaderRef(None, cell, 0))
        else:
            refs.append(HeaderRef(label, cell, origins_by_label[label].index(cell) + 1))
    return refs, origins_by_label


def convert_table(grid, spec, registry=None):
    """Convert a raw supplier grid to a :class:`LongTable` per ``spec``.

    ``registry`` is an optional :class:`IndexSetRegistry` used to jointly
    verify named index sets shared with other tables.
    """
    grid = [list(row) for row in grid]
    if not grid or not grid[0]:
        raise TableSpecError("grid is empty")
    width = len(grid[0])
    if any(len(row) != width for row in grid):
        raise TableSpecError("grid rows have inconsistent lengths")
    height = len(grid)

    try:
        header_rows = spec["header_rows"]
        header_cols = spec["header_cols"]
        roles = spec["index_roles"]
    except KeyError as exc:
        raise TableSpecError(f"spec is missing required key {exc}") from None
    if not (0 < header_rows < height) or not (0 < header_cols < width):
        raise TableSpecError("header_rows/header_cols leave no body region")

    inherit = spec.get("inherit", {})
    inherit_rows = inherit.get("rows", True)  # column headers inherit leftwards
    inherit_cols = inherit.get("cols", True)  # row headers inherit downwards

    row_roles = list(roles.get("rows", ()))
    col_roles = list(roles.get("cols", ()))
    param_role = roles.get("param")
    for role in row_roles + col_roles:
        if "level" not in role:
            raise TableSpecError(f"index role {role!r} lacks a 'level'")
    if any(not 0 <= r["level"] < header_cols for r in row_roles):
        raise TableSpecError("row index role level outside header_cols")
    if any(not 0 <= r["level"] < header_rows for r in col_roles):
        raise TableSpecError("col index role level outside header_rows")
    if param_role is not None and not 0 <= param_role["level"] < header_rows:
        raise TableSpecError("param level outside header_rows")

    occ_identity = spec.get("occurrence_identity", {})
    occ_rows = set(occ_identity.get("rows", ()))
    occ_cols = set(occ_identity.get("cols", ()))

    conventions = spec.get("null_conventions", {})
    missing_markers = tuple(conventions.get("missing", DEFAULT_MISSING))
    empty_markers = tuple(conventions.get("explicit_empty", DEFAULT_EXPLICIT_EMPTY))

    body_rows = height - header_rows
    body_cols = width - header_cols

    # Expand column-header levels (one per header row, scanning left to right).
    col_levels = []
    col_ambiguous = []  # per level: set of ambiguous body-column positions
    for lvl in range(header_rows):
        raw = [grid[lvl][header_cols + j] for j in range(body_cols)]
        cells = [(lvl, header_cols + j) for j in range(body_cols)]
        refs, origins_by_label = _expand_level(raw, cells, inherit_rows)
        col_levels.append(refs)
        ambiguous = set()
        for label, origins in origins_by_label.items():
            if len(origins) > 1 and lvl not in occ_cols:
                for j, ref in enumerate(refs):
                    if ref.label == label:
                        ambiguous.add(j)
        col_ambiguous.append(ambiguous)

    # Expand row-header levels (one per header column, scanning top to bottom).
    row_levels = []
    row_ambiguous = []
    for lvl in range(header_cols):
        raw = [grid[header_rows + i][lvl] for i in range(body_rows)]
        cells = [(header_rows + i, lvl) for i in range(body_rows)]
        refs, origins_by_label = _expand_level(raw, cells, inherit_cols)
        row_levels.append(refs)
        ambiguous = set()
        for label, origins in origins_by_label.items():
            if len(origins) > 1 and lvl not in occ_rows:
                for i, ref in enumerate(refs):
                    if ref.label == label:
                        ambiguous.add(i)
        row_ambiguous.append(ambiguous)

    default_param = spec.get("param", spec.get("name", "param"))
    table = LongTable(name=spec.get("name", "table"))
    set_members = {}  # set name -> ordered unique labels / identities

    for i in range(body_rows):
        for j in range(body_cols):
            cell = (header_rows + i, header_cols + j)
            raw = grid[cell[0]][cell[1]]

            # Resolve header responsibilities for this body cell.
            refs = []
            problem = None
            for role in row_roles:
                lvl = role["level"]
                ref = row_levels[lvl][i]
                if ref.label is None:
                    problem = (REASON_UNRESOLVED, f"row header level {lvl} is blank")
                    break
                if i in row_ambiguous[lvl]:
                    problem = (
                        REASON_AMBIGUOUS,
                        f"row header level {lvl} repeats {ref.label!r} "
                        "without occurrence identity",
                    )
                    break
                refs.append(ref)
            if problem is None:
                for role in col_roles:
                    lvl = role["level"]
                    ref = col_levels[lvl][j]
                    if ref.label is None:
                        problem = (REASON_UNRESOLVED, f"column header level {lvl} is blank")
                        break
                    if j in col_ambiguous[lvl]:
                        problem = (
                            REASON_AMBIGUOUS,
                            f"column header level {lvl} repeats {ref.label!r} "
                            "without occurrence identity",
                        )
                        break
                    refs.append(ref)
            param_ref = None
            if problem is None and param_role is not None:
                lvl = param_role["level"]
                param_ref = col_levels[lvl][j]
                if param_ref.label is None:
                    problem = (REASON_UNRESOLVED, f"param header level {lvl} is blank")
                # A repeated param name is not ambiguous by itself: the same
                # parameter may legitimately span several index columns.
                # Genuine (param, index) collisions are rejected as duplicate
                # keys when the long table is loaded, never overwritten.

            if problem is not None:
                table.excluded.append(ExcludedCell(cell, problem[0], problem[1]))
                continue

            status, value = _interpret(raw, missing_markers, empty_markers)
            if status == MISSING:
                table.excluded.append(
                    ExcludedCell(cell, REASON_MISSING, "empty observation per null convention")
                )
                continue

            param = param_ref.label if param_ref is not None else default_param
            param_identity = (
                (param_ref.label, param_ref.occurrence) if param_ref is not None else (param, 1)
            )
            param_cell = param_ref.cell if param_ref is not None else None
            row = LongRow(
                param=param,
                index=tuple(ref.label for ref in refs),
                index_identity=tuple((ref.label, ref.occurrence) for ref in refs),
                value=value,
                status=status,
                cell=cell,
                header_cells=tuple(ref.cell for ref in refs),
                param_cell=param_cell,
                param_identity=param_identity,
            )
            table.rows.append(row)

            # Collect named index-set members in first-appearance order.
            for role, ref in zip(row_roles + col_roles, refs):
                set_name = role.get("set")
                if not set_name:
                    continue
                bucket = set_members.setdefault(set_name, ([], []))
                if ref.label not in bucket[0]:
                    bucket[0].append(ref.label)
                    bucket[1].append((ref.label, ref.occurrence))

    # Jointly verify shared index sets; never reuse another table's ordering.
    for set_name, (labels, identities) in set_members.items():
        if registry is not None:
            registry.sync(set_name, labels)
        table.index_sets[set_name] = tuple(labels)
        table.index_set_identities[set_name] = tuple(identities)

    seen_params = []
    for row in table.rows:
        if row.param_identity not in seen_params:
            seen_params.append(row.param_identity)
    table.params = tuple(label for label, _ in seen_params)
    table.param_occurrence_identity = param_role is not None and param_role["level"] in occ_cols
    return table


@dataclass(frozen=True)
class AuditIssue:
    kind: str  # "value_mismatch" / "status_mismatch" / "header_mismatch"
    cell: tuple
    detail: str


@dataclass
class AuditReport:
    ok: bool
    checked: int
    issues: list = field(default_factory=list)


def audit_back_mapping(long_table, grid, spec):
    """Map every published row back to the raw grid and verify it cell by cell.

    Checks that the source body cell still interprets to the published value
    under the same null conventions, and that every index label and the
    parameter name match the labels of their recorded header cells.
    """
    conventions = spec.get("null_conventions", {})
    missing_markers = tuple(conventions.get("missing", DEFAULT_MISSING))
    empty_markers = tuple(conventions.get("explicit_empty", DEFAULT_EXPLICIT_EMPTY))

    issues = []
    for row in long_table.rows:
        raw = grid[row.cell[0]][row.cell[1]]
        status, value = _interpret(raw, missing_markers, empty_markers)
        if status != row.status:
            issues.append(
                AuditIssue(
                    "status_mismatch",
                    row.cell,
                    f"published status {row.status!r} but grid cell re-reads as {status!r}",
                )
            )
        elif status == VALUE and value != row.value:
            issues.append(
                AuditIssue(
                    "value_mismatch",
                    row.cell,
                    f"published value {row.value!r} != grid value {value!r}",
                )
            )
        for (label, _occ), header_cell in zip(row.index_identity, row.header_cells):
            if grid[header_cell[0]][header_cell[1]] != label:
                issues.append(
                    AuditIssue(
                        "header_mismatch",
                        header_cell,
                        f"index label {label!r} not found at its header cell",
                    )
                )
        if row.param_cell is not None and grid[row.param_cell[0]][row.param_cell[1]] != row.param:
            issues.append(
                AuditIssue(
                    "header_mismatch",
                    row.param_cell,
                    f"param name {row.param!r} not found at its header cell",
                )
            )
    return AuditReport(ok=not issues, checked=len(long_table.rows), issues=issues)


def load_into_pyomo(long_table, model=None, use_identity=False, set_prefix=""):
    """Load a :class:`LongTable` into a Pyomo model.

    Creates one ``pyo.Set`` per named index set and one ``pyo.Param`` per
    parameter (plus its ``<param>_index`` set).  With ``use_identity=True``
    each index element is flattened to ``(label, occurrence)`` pairs so
    repeated headers stay distinct as structured keys -- string concatenation
    is never used.  Explicit-empty entries carry no numeric value and are not
    loaded; they remain in ``long_table.rows`` for provenance.
    """
    import pyomo.environ as pyo

    if model is None:
        model = pyo.ConcreteModel()

    def flatten(identity_key):
        flat = []
        for label, occ in identity_key:
            flat.extend((label, occ))
        return tuple(flat)

    # Named index sets, in the table's own first-appearance order.
    for set_name in long_table.index_sets:
        if use_identity:
            members = [flatten((ident,)) for ident in long_table.index_set_identities[set_name]]
            members = [m[0] if len(m) == 1 else m for m in members]
        else:
            members = list(long_table.index_sets[set_name])
        setattr(model, set_prefix + str(set_name), pyo.Set(initialize=members, ordered=True))

    data = long_table.to_param_data(use_identity=use_identity)
    for param_key, values in data.items():
        keys = list(values.keys())
        if not keys:
            continue
        if use_identity:
            flat_keys = [flatten(k) for k in keys]
        else:
            flat_keys = list(keys)
        dim = len(flat_keys[0])
        if dim == 1:
            set_members = [k[0] for k in flat_keys]
            init = {k[0]: values[orig] for k, orig in zip(flat_keys, keys)}
        else:
            set_members = flat_keys
            init = {k: values[orig] for k, orig in zip(flat_keys, keys)}
        attr = set_prefix + str(param_key)
        if hasattr(model, attr):
            raise DuplicateIndexError(
                f"model already has attribute {attr!r}; repeated param headers "
                "must be loaded under distinct prefixes, not overwritten"
            )
        index_attr = attr + "_index"
        index_set = pyo.Set(dimen=dim, initialize=set_members, ordered=True)
        setattr(model, index_attr, index_set)
        param = pyo.Param(getattr(model, index_attr), initialize=init, within=pyo.Any)
        setattr(model, attr, param)
    return model
