from incremental_rebuild import (
    Cell,
    LoadResult,
    TableChangeKind,
    diff_load_results,
)


def test_row_reorder_is_noop():
    rows = [("a", 1.0, "obs-1"), ("b", 2.0, "obs-2"), ("c", 3.0, "obs-3")]
    old = LoadResult.from_rows("t", rows)
    new = LoadResult.from_rows("t", list(reversed(rows)))
    assert diff_load_results(old, new) == {}


def test_identical_loads_are_noop():
    old = LoadResult.from_rows("t", [("a", 1.0, "obs-1")])
    new = LoadResult.from_rows("t", [("a", 1.0, "obs-1")])
    assert diff_load_results(old, new) == {}


def test_value_only_change():
    old = LoadResult.from_rows("t", [("a", 1.0, "s1"), ("b", 2.0, "s2")])
    new = LoadResult.from_rows("t", [("a", 1.0, "s1"), ("b", 2.5, "s2-fix")])
    (diff,) = diff_load_results(old, new).values()
    assert diff.kind is TableChangeKind.VALUE_ONLY
    assert not diff.added and not diff.removed
    assert set(diff.changed) == {("b",)}
    old_cell, new_cell = diff.changed[("b",)]
    assert (old_cell.value, old_cell.source) == (2.0, "s2")
    assert (new_cell.value, new_cell.source) == (2.5, "s2-fix")


def test_membership_change():
    old = LoadResult.from_rows("t", [("a", 1.0, "s1"), ("b", 2.0, "s2")])
    new = LoadResult.from_rows("t", [("b", 2.0, "s2"), ("c", 3.0, "s3")])
    (diff,) = diff_load_results(old, new).values()
    assert diff.kind is TableChangeKind.MEMBERSHIP
    assert set(diff.added) == {("c",)}
    assert set(diff.removed) == {("a",)}
    assert not diff.changed


def test_table_added_and_removed_wholesale():
    old = LoadResult({"gone": {("x",): Cell(1, "sg")}})
    new = LoadResult({"fresh": {("y",): Cell(2, "sf")}})
    diffs = diff_load_results(old, new)
    assert set(diffs) == {"gone", "fresh"}
    assert diffs["gone"].kind is TableChangeKind.MEMBERSHIP
    assert set(diffs["gone"].removed) == {("x",)}
    assert set(diffs["fresh"].added) == {("y",)}


def test_provenance_traceable_item_by_item():
    old = LoadResult.from_rows("t", [("a", 1.0, "src-a"), ("b", 2.0, "src-b")])
    new = LoadResult.from_rows("t", [("b", 2.5, "src-b2"), ("c", 3.0, "src-c")])
    (diff,) = diff_load_results(old, new).values()
    traces = {(k, o and o.source, n and n.source) for k, o, n in diff.traces()}
    assert traces == {
        (("a",), "src-a", None),
        (("b",), "src-b", "src-b2"),
        (("c",), None, "src-c"),
    }


def test_multi_dimensional_keys():
    old = LoadResult.from_rows("t", [(("a", 1), 10.0, "s1")])
    new = LoadResult.from_rows("t", [(("a", 2), 10.0, "s2")])
    (diff,) = diff_load_results(old, new).values()
    assert diff.kind is TableChangeKind.MEMBERSHIP
    assert set(diff.removed) == {("a", 1)}
    assert set(diff.added) == {("a", 2)}
