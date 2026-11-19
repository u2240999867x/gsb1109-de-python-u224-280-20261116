"""Tests for supplier_param_table: multi-level header expansion, occurrence
identity, null conventions, cross-table index-set verification, back-mapping
audit, and Pyomo loading."""

import pytest

pyo = pytest.importorskip("pyomo.environ")

from supplier_param_table import (
    EXPLICIT_EMPTY,
    REASON_AMBIGUOUS,
    REASON_MISSING,
    REASON_UNRESOLVED,
    VALUE,
    DuplicateIndexError,
    IndexSetMismatchError,
    IndexSetRegistry,
    audit_back_mapping,
    convert_table,
    load_into_pyomo,
)


def make_spec(**overrides):
    spec = {
        "name": "supply",
        "header_rows": 2,
        "header_cols": 1,
        "index_roles": {
            "rows": [{"level": 0, "set": "PLANT"}],
            "cols": [{"level": 0, "set": "QUARTER"}],
            "param": {"level": 1},
        },
        "null_conventions": {"missing": [None, ""], "explicit_empty": ["NA"]},
    }
    spec.update(overrides)
    return spec


# ---------------------------------------------------------------------------
# Multi-level headers, inheritance, provenance
# ---------------------------------------------------------------------------

def grid_basic():
    # Top header row: "2026" merged over two quarter columns.
    # Second header row names the parameter per column.
    return [
        [None, "2026", None, "2027"],
        ["plant", "capacity", "demand", "capacity"],
        ["A", 10, 3, 11],
        ["B", 20, "NA", ""],
    ]


def test_multilevel_header_expansion_and_inheritance():
    table = convert_table(grid_basic(), make_spec())
    by_cell = {row.cell: row for row in table.rows}

    row = by_cell[(2, 1)]  # plant A, first 2026 column, capacity
    assert row.param == "capacity"
    assert row.index == ("A", "2026")
    assert row.value == 10
    # Provenance: value cell plus the exact header cells behind each label.
    assert row.cell == (2, 1)
    assert row.header_cells == ((2, 0), (0, 1))
    assert row.param_cell == (1, 1)

    # The merged "2026" header is inherited, not repeated: both body columns
    # trace back to the same origin header cell and share one occurrence.
    inherited = by_cell[(2, 2)]
    assert inherited.index == ("A", "2026")
    assert inherited.param == "demand"
    assert inherited.header_cells[1] == (0, 1)
    assert inherited.index_identity[1] == ("2026", 1)

    other = by_cell[(2, 3)]
    assert other.index == ("A", "2027")
    assert other.header_cells[1] == (0, 3)


def test_row_header_inheritance_downwards():
    grid = [
        ["region", "capacity"],
        ["north", 5],
        [None, 6],  # inherits "north"
        ["south", 7],
    ]
    spec = make_spec(header_rows=1, index_roles={"rows": [{"level": 0, "set": "PLANT"}]})
    spec["name"] = "capacity"
    table = convert_table(grid, spec)
    assert [row.index for row in table.rows] == [("north",), ("north",), ("south",)]
    assert table.rows[1].header_cells[0] == (1, 0)  # origin of merged label


# ---------------------------------------------------------------------------
# Null conventions: missing observation vs explicit empty
# ---------------------------------------------------------------------------

def test_missing_vs_explicit_empty():
    table = convert_table(grid_basic(), make_spec())
    by_cell = {row.cell: row for row in table.rows}

    # "NA" is an explicit empty value: published with value None.
    explicit = by_cell[(3, 2)]
    assert explicit.status == EXPLICIT_EMPTY
    assert explicit.value is None

    # "" is a missing observation: no row is emitted, cell is recorded.
    assert (3, 3) not in by_cell
    missing = {ex.cell: ex for ex in table.excluded}
    assert missing[(3, 3)].reason == REASON_MISSING

    # Explicit empties stay out of the numeric model data but keep provenance.
    data = table.to_param_data()
    assert ("B", "2026") not in data["demand"]
    data_with_empty = table.to_param_data(include_empty=True)
    assert data_with_empty["demand"][("B", "2026")] is None


# ---------------------------------------------------------------------------
# Repeated headers: occurrence identity, never string concatenation
# ---------------------------------------------------------------------------

def grid_duplicate_headers():
    # Two distinct "Q" columns at level 0, both with param "capacity".
    return [
        ["plant", "Q", "Q"],
        ["", "capacity", "capacity"],
        ["A", 1, 2],
    ]


def test_duplicate_headers_without_occurrence_identity_are_excluded():
    table = convert_table(grid_duplicate_headers(), make_spec())
    assert table.rows == []
    reasons = {ex.reason for ex in table.excluded}
    assert reasons == {REASON_AMBIGUOUS}


def test_duplicate_headers_with_occurrence_identity():
    spec = make_spec(occurrence_identity={"cols": [0]})
    table = convert_table(grid_duplicate_headers(), spec)
    assert len(table.rows) == 2
    identities = sorted(row.index_identity for row in table.rows)
    assert identities == [(("A", 1), ("Q", 1)), (("A", 1), ("Q", 2))]
    values = {row.index_identity: row.value for row in table.rows}
    assert values[(("A", 1), ("Q", 1))] == 1
    assert values[(("A", 1), ("Q", 2))] == 2

    # Plain labels collide; the converter refuses to overwrite silently.
    with pytest.raises(DuplicateIndexError):
        table.to_param_data()
    # Structured occurrence keys keep both values without string splicing.
    data = table.to_param_data(use_identity=True)
    assert data["capacity"][(("A", 1), ("Q", 1))] == 1
    assert data["capacity"][(("A", 1), ("Q", 2))] == 2


def test_merged_span_is_single_occurrence_not_duplicate():
    # "Q" merged across two columns is one occurrence; no identity needed.
    grid = [
        ["plant", "Q", None],
        ["", "capacity", "demand"],
        ["A", 1, 2],
    ]
    table = convert_table(grid, make_spec())
    assert len(table.rows) == 2
    assert all(row.index_identity[1] == ("Q", 1) for row in table.rows)


# ---------------------------------------------------------------------------
# Cross-table index sets: joint member/dimension check, no order reuse
# ---------------------------------------------------------------------------

def test_shared_index_set_verified_jointly():
    registry = IndexSetRegistry()
    grid1 = [["plant", "capacity"], ["A", 1], ["B", 2]]
    grid2 = [["plant", "capacity"], ["B", 20], ["A", 10]]  # same members, new order
    spec = make_spec(header_rows=1, index_roles={"rows": [{"level": 0, "set": "PLANT"}]})

    t1 = convert_table(grid1, spec, registry)
    t2 = convert_table(grid2, spec, registry)  # different order is fine
    assert t1.index_sets["PLANT"] == ("A", "B")
    assert t2.index_sets["PLANT"] == ("B", "A")
    # Each table resolves its own layout: values follow each table's rows.
    assert t1.to_param_data()["supply"] == {("A",): 1, ("B",): 2}
    assert t2.to_param_data()["supply"] == {("B",): 20, ("A",): 10}


def test_same_shape_does_not_reuse_previous_column_order():
    registry = IndexSetRegistry()
    spec = make_spec(
        header_rows=1,
        index_roles={
            "rows": [{"level": 0, "set": "PLANT"}],
            "cols": [{"level": 0, "set": "QUARTER"}],
        },
    )
    grid1 = [["plant", "Q1", "Q2"], ["A", 1, 2]]
    grid2 = [["plant", "Q2", "Q1"], ["A", 2, 1]]  # same shape, swapped columns
    t1 = convert_table(grid1, spec, registry)
    t2 = convert_table(grid2, spec, registry)
    assert t1.to_param_data()["supply"][("A", "Q1")] == 1
    # Column order is re-read from grid2's own headers, not reused from grid1.
    assert t2.to_param_data()["supply"][("A", "Q1")] == 1
    assert t2.to_param_data()["supply"][("A", "Q2")] == 2


def test_index_set_member_mismatch_rejected():
    registry = IndexSetRegistry()
    spec = make_spec(header_rows=1, index_roles={"rows": [{"level": 0, "set": "PLANT"}]})
    convert_table([["plant", "capacity"], ["A", 1], ["B", 2]], spec, registry)
    with pytest.raises(IndexSetMismatchError, match="member mismatch"):
        convert_table([["plant", "capacity"], ["A", 1], ["C", 3]], spec, registry)


def test_index_set_dimension_mismatch_rejected():
    registry = IndexSetRegistry()
    registry.sync("ARC", [("A", "B")])
    with pytest.raises(IndexSetMismatchError, match="dimension"):
        registry.sync("ARC", ["A"])


# ---------------------------------------------------------------------------
# Back-mapping audit
# ---------------------------------------------------------------------------

def test_audit_back_mapping_ok_and_tamper_detection():
    grid = grid_basic()
    spec = make_spec()
    table = convert_table(grid, spec)

    report = audit_back_mapping(table, grid, spec)
    assert report.ok
    assert report.checked == len(table.rows)

    # Tamper with the source grid: value and header responsibility must fail.
    grid[2][1] = 999
    grid[0][3] = "2099"
    report = audit_back_mapping(table, grid, spec)
    assert not report.ok
    kinds = {issue.kind for issue in report.issues}
    assert "value_mismatch" in kinds
    assert "header_mismatch" in kinds


def test_unresolvable_region_never_enters_model():
    # Leading blank header with inheritance disabled cannot be resolved.
    grid = [
        ["plant", None, "Q2"],
        ["A", 1, 2],
    ]
    spec = make_spec(
        header_rows=1,
        inherit={"rows": False},
        index_roles={
            "rows": [{"level": 0, "set": "PLANT"}],
            "cols": [{"level": 0, "set": "QUARTER"}],
        },
    )
    table = convert_table(grid, spec)
    assert [row.index for row in table.rows] == [("A", "Q2")]
    unresolved = [ex for ex in table.excluded if ex.reason == REASON_UNRESOLVED]
    assert [ex.cell for ex in unresolved] == [(1, 1)]
    # The excluded region is absent from the model data.
    assert list(table.to_param_data()["supply"]) == [("A", "Q2")]


# ---------------------------------------------------------------------------
# Pyomo loading
# ---------------------------------------------------------------------------

def test_load_into_pyomo_roundtrip():
    spec = make_spec()
    table = convert_table(grid_basic(), spec)
    model = load_into_pyomo(table)

    assert set(model.PLANT.data()) == {"A", "B"}
    assert set(model.QUARTER.data()) == {"2026", "2027"}
    assert pyo.value(model.capacity[("A", "2026")]) == 10
    assert pyo.value(model.capacity[("A", "2027")]) == 11
    assert pyo.value(model.demand[("A", "2026")]) == 3
    # Explicit-empty ("NA") carries no numeric value and is not loaded.
    assert ("B", "2026") not in model.demand
    # Missing observation ("") never entered the table at all.
    assert ("B", "2027") not in model.capacity

    # Audit still passes against the untouched grid after loading.
    assert audit_back_mapping(table, grid_basic(), spec).ok


def test_load_into_pyomo_with_occurrence_identity():
    spec = make_spec(occurrence_identity={"cols": [0]})
    table = convert_table(grid_duplicate_headers(), spec)
    model = load_into_pyomo(table, use_identity=True)
    # Index elements are flattened (label, occurrence) pairs.
    assert pyo.value(model.capacity[("A", 1, "Q", 1)]) == 1
    assert pyo.value(model.capacity[("A", 1, "Q", 2)]) == 2
