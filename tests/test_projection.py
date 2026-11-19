from __future__ import annotations

import pytest

import cbor2
from cbor2 import (
    CBORTag,
    Column,
    EmptyPolicy,
    MissingPolicy,
    Projection,
    ProjectionBudgetError,
    ProjectionCardinalityError,
    ProjectionCycleError,
    ProjectionPathError,
    ProjectionTagError,
    TagPolicy,
    parse_path,
)


def roundtrip(value):
    return cbor2.loads(cbor2.dumps(value, value_sharing=True))


ORDERS = {
    "orders": [
        {"id": 1, "items": [{"sku": "a"}, {"sku": "b"}]},
        {"id": 2, "items": [{"sku": "c"}]},
    ]
}


def test_plan_reports_exact_rows_and_column_sources():
    projection = Projection({"order": "orders[].id", "sku": "orders[].items[].sku"})
    plan = projection.plan(roundtrip(ORDERS))

    assert plan.row_count == 3
    assert plan.rows == 3
    assert plan.sources == {"order": "orders[].id", "sku": "orders[].items[].sku"}
    assert [(column.name, column.occurrences) for column in plan.columns] == [
        ("order", 2),
        ("sku", 3),
    ]
    assert plan.columns[0].missing == plan.columns[0].empty == plan.columns[0].tags == 0
    assert plan.finite is True


def test_plan_does_not_emit_records():
    projection = Projection({"order": "orders[].id"})
    plan = projection.plan(roundtrip(ORDERS))
    assert not hasattr(plan, "records")
    assert plan.describe().startswith("2 rows [paired]")


def test_shared_prefix_is_correlated_not_crossed():
    projection = Projection({"order": "orders[].id", "sku": "orders[].items[].sku"}, mode="cross")
    assert projection.rows(roundtrip(ORDERS)) == [
        {"order": 1, "sku": "a"},
        {"order": 1, "sku": "b"},
        {"order": 2, "sku": "c"},
    ]


def test_paired_expansion_pads_and_skips():
    data = {"left": [1, 2], "right": [3]}

    assert Projection({"x": "left[]", "y": "right[]"}, missing_value=0).rows(data) == [
        {"x": 1, "y": 3},
        {"x": 2, "y": 0},
    ]
    assert Projection({"x": "left[]", "y": "right[]"}, on_missing="skip").rows(data) == [
        {"x": 1, "y": 3}
    ]


def test_paired_length_mismatch_can_be_an_error():
    projection = Projection({"x": "left[]", "y": "right[]"}, on_missing="error")
    with pytest.raises(ProjectionCardinalityError, match="occurrence counts"):
        projection.plan({"left": [1, 2], "right": [3]})


def test_cross_expansion_is_stable():
    projection = Projection({"x": "left[]", "y": "right[]"}, mode="cross")
    assert projection.rows({"left": [1, 2], "right": [3, 4]}) == [
        {"x": 1, "y": 3},
        {"x": 1, "y": 4},
        {"x": 2, "y": 3},
        {"x": 2, "y": 4},
    ]
    assert projection.plan({"left": [1, 2], "right": [3, 4]}).row_count == 4


def test_scalar_column_broadcasts_in_paired_mode():
    projection = Projection({"kind": "kind", "value": "values[]"})
    assert projection.rows({"kind": "K", "values": [1, 2, 3]}) == [
        {"kind": "K", "value": 1},
        {"kind": "K", "value": 2},
        {"kind": "K", "value": 3},
    ]


def test_shared_object_is_counted_per_occurrence():
    shared = {"n": 7}
    data = {"first": [shared, shared], "second": [shared]}
    projection = Projection({"a": "first[].n", "b": "second[].n"}, mode="cross")

    assert projection.plan(data).row_count == 2
    assert projection.rows(data) == [
        {"a": 7, "b": 7},
        {"a": 7, "b": 7},
    ]


def test_shared_object_decoded_with_value_sharing_is_counted_twice():
    shared = ["node"]
    payload = cbor2.dumps({"a": shared, "b": shared}, value_sharing=True)
    decoded = cbor2.loads(payload)
    assert decoded["a"] is decoded["b"]

    projection = Projection({"a": "a[]", "b": "b[]"}, mode="cross")
    assert projection.rows(decoded) == [{"a": "node", "b": "node"}]


def test_missing_null_skip_and_error():
    assert Projection({"v": "a.b"}).rows({}) == [{"v": None}]
    assert Projection({"v": "a.b"}, on_missing="skip").rows({}) == []
    with pytest.raises(ProjectionCardinalityError, match="projection path 'a'"):
        Projection({"v": "a.b"}, on_missing="error").plan({})
    assert Projection({"v": "a.b"}, missing_value="?").rows({}) == [{"v": "?"}]


def test_empty_array_zero_null_and_error():
    assert Projection({"v": "a[].b"}).plan({"a": []}).row_count == 0
    assert Projection({"v": "a[].b"}, on_empty="null", missing_value="NA").rows({"a": []}) == [
        {"v": "NA"}
    ]
    with pytest.raises(ProjectionCardinalityError, match="empty array at projection path 'a'"):
        Projection({"v": "a[].b"}, on_empty="error").plan({"a": []})


def test_empty_nested_array_counts_only_the_empty_branch():
    projection = Projection({"v": "outer[].inner[].x"}, on_empty="null", missing_value="-")
    data = {"outer": [{"inner": [{"x": 1}]}, {"inner": []}]}
    plan = projection.plan(data)
    assert plan.row_count == 2
    assert plan.columns[0].empty == 1
    assert projection.rows(data) == [{"v": 1}, {"v": "-"}]


def test_unresolvable_tag_policies():
    tagged = {"v": CBORTag(999, {"n": 5})}

    assert Projection({"v": "v.n"}).rows(tagged) == [{"v": tagged["v"]}]
    assert Projection({"v": "v.n"}, on_tag="unwrap").rows(tagged) == [{"v": 5}]
    assert Projection({"v": "v.n"}, on_tag="zero").plan(tagged).row_count == 0
    with pytest.raises(ProjectionTagError, match="unresolvable tag 999"):
        Projection({"v": "v.n"}, on_tag="error").plan(tagged)


def test_tag_resolver_is_used_for_planning_and_projection():
    calls = []

    def resolve(tag):
        calls.append(tag.tag)
        return tag.value

    projection = Projection({"v": "v.n"}, tag_resolver=resolve)
    tagged = {"v": CBORTag(999, {"n": 5})}
    assert projection.plan(tagged).row_count == 1
    assert projection.rows(tagged) == [{"v": 5}]
    assert calls == [999, 999, 999]


def test_tag_resolving_back_to_itself_is_unresolvable():
    tagged = CBORTag(999, {"n": 5})
    projection = Projection({"v": "v.n"}, tag_resolver=lambda tag: tag, on_tag="error")
    with pytest.raises(ProjectionTagError, match="unresolvable tag 999"):
        projection.plan({"v": tagged})


def test_budget_aborts_without_partial_rows():
    projection = Projection({"x": "a[]", "y": "b[]"}, mode="cross")
    data = {"a": [1, 2, 3], "b": [4, 5, 6, 7]}

    with pytest.raises(ProjectionBudgetError) as exc_info:
        projection.project(data, max_rows=11)

    error = exc_info.value
    assert error.row_count == 12
    assert error.max_rows == 11
    assert error.plan.row_count == 12
    assert "no records were emitted" in str(error)


def test_budget_of_exact_size_and_zero_are_allowed():
    projection = Projection({"x": "a[]"})
    assert len(projection.rows({"a": [1, 2]}, max_rows=2)) == 2
    with pytest.raises(ProjectionBudgetError):
        projection.project({"a": [1, 2]}, max_rows=1)

    empty = Projection({"x": "items[]"})
    assert empty.rows({"items": []}, max_rows=0) == []


def test_budget_alias():
    projection = Projection({"x": "a[]"})
    assert projection.plan({"a": [1, 2]}, budget=2).row_count == 2
    assert projection.rows({"a": [1, 2]}, budget=2) == [{"x": 1}, {"x": 2}]
    with pytest.raises(ProjectionBudgetError):
        projection.project({"a": [1, 2, 3]}, budget=2)
    with pytest.raises(ValueError):
        projection.project({"a": [1, 2]}, max_rows=2, budget=3)


def test_empty_expansion_produces_zero_rows_even_beside_other_branches():
    projection = Projection({"x": "left[]", "y": "right[]"})
    assert projection.plan({"left": [], "right": [1, 2]}).row_count == 0
    assert projection.rows({"left": [], "right": [1, 2]}) == []


def test_plan_and_source_mapping_access():
    projection = Projection({"x": "a[]"})
    plan = projection.plan({"a": [1, 2]})
    assert plan["x"].occurrences == 2
    assert plan.column_sources is plan.columns
    assert plan["x"]["path"] == "a[]"
    with pytest.raises(KeyError):
        plan["missing"]
    result = projection.project({"a": [1, 2]})
    assert result.records is result.rows


def test_negative_or_invalid_budget_is_rejected():
    projection = Projection({"x": "a"})
    with pytest.raises(ValueError):
        projection.project({"a": 1}, max_rows=-1)
    with pytest.raises(TypeError):
        projection.project({"a": 1}, max_rows=True)


def test_plan_with_budget_raises_before_rows():
    projection = Projection({"x": "a[]", "y": "b[]"}, mode="cross")
    data = {"a": list(range(1000)), "b": list(range(1000))}
    with pytest.raises(ProjectionBudgetError):
        projection.plan(data, max_rows=99)
    assert projection.plan(data).row_count == 1_000_000


def test_recursive_expansion_has_a_finite_endpoint():
    data = {"root": [1, [2, [3]], 4]}
    projection = Projection({"v": "root[**]"})
    assert projection.rows(data) == [{"v": 1}, {"v": 2}, {"v": 3}, {"v": 4}]


def test_recursive_expansion_cycle_names_the_path():
    cycle: list = []
    cycle.append(cycle)
    projection = Projection({"v": "root[**]"})

    with pytest.raises(ProjectionCycleError) as exc_info:
        projection.plan({"root": cycle})

    error = exc_info.value
    assert error.path == "root[0]"
    assert error.cycle_path == "root"
    assert "no finite endpoint" in str(error)
    assert error.value is cycle


def test_cycle_is_reported_by_project_too():
    cycle: list = []
    cycle.append(cycle)
    projection = Projection({"v": "root[].items[].name"})
    data = {"root": [cycle]}

    with pytest.raises(ProjectionCycleError) as exc_info:
        projection.project(data, max_rows=10)

    assert exc_info.value.path == "root[0][0]"


def test_finite_path_through_cyclic_object_is_allowed():
    cycle: dict = {"name": "ok"}
    cycle["self"] = cycle
    projection = Projection({"v": "root.name"})
    assert projection.rows({"root": cycle}) == [{"v": "ok"}]


def test_cycle_is_not_treated_as_zero_rows():
    cycle: list = []
    cycle.append(cycle)
    projection = Projection({"v": "root[**]"})
    with pytest.raises(ProjectionCycleError):
        projection.plan({"root": cycle})
    # A successful plan with no matching values is legitimately zero rows.
    assert projection.plan({"root": []}).row_count == 0


def test_parse_path_forms():
    assert str(parse_path("$")) == "$"
    assert str(parse_path("a.b[2].c")) == "a.b[2].c"
    assert str(parse_path("a[*]")) == "a[]"
    assert str(parse_path("a[**]")) == "a[**]"
    assert str(parse_path("a['b.c']")) == "a['b.c']"
    assert str(parse_path(["a", 2, None, "b"])) == "a[2][].b"

    with pytest.raises(ProjectionPathError):
        parse_path("a[")
    with pytest.raises(ProjectionPathError):
        parse_path("a[bogus]")
    with pytest.raises(ProjectionPathError):
        parse_path("a[-1]")


def test_byte_keys_via_sequence_path():
    projection = Projection([Column("v", [b"key", "inner"])])
    assert projection.rows({b"key": {"inner": 5}}) == [{"v": 5}]


def test_column_policy_overrides_null():
    projection = Projection(
        [
            Column("missing_ok", "a", missing=MissingPolicy.NULL),
            Column("also_missing", "b", missing=MissingPolicy.NULL),
        ],
        missing_value=0,
    )
    assert projection.rows({}) == [{"missing_ok": 0, "also_missing": 0}]


def test_column_policy_overrides_error():
    projection = Projection(
        [
            Column("missing_ok", "a", missing=MissingPolicy.NULL),
            Column("required", "b", missing=MissingPolicy.ERROR),
        ]
    )
    with pytest.raises(ProjectionCardinalityError):
        projection.plan({"a": 1})


def test_duplicate_column_names_are_rejected():
    with pytest.raises(ValueError, match="duplicate column names"):
        Projection([("x", "a"), ("x", "b")])


def test_policy_enum_coercion():
    projection = Projection({"x": "a"}, on_missing="SKIP", on_empty="Null", on_tag="Unwrap")
    assert projection.policy.missing is MissingPolicy.SKIP
    assert projection.policy.empty is EmptyPolicy.NULL
    assert projection.policy.tag is TagPolicy.UNWRAP


def test_projection_result_exposes_plan():
    projection = Projection({"x": "a[]"})
    result = projection.project({"a": [1, 2]})
    assert result.row_count == 2 == len(result)
    assert result.columns == ("x",)
    assert result.sources == {"x": "a[]"}
    assert list(result) == result.rows


def test_no_columns_yields_no_rows():
    assert Projection([]).plan({"a": 1}).row_count == 0


def test_original_value_is_not_modified():
    data = roundtrip(ORDERS)
    before = repr(data)
    Projection({"v": "orders[].items[].sku"}).project(data)
    assert repr(data) == before
