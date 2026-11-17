"""Offline tests for the lock-catalog access planner (pycomm3.offline)."""

import json
from pathlib import Path

import pytest

from pycomm3 import OfflineAccessPlanner, OfflinePlan, PathSegment, load_tag_catalog
from pycomm3.cip import DataTypes
from pycomm3.packets.util import tag_request_path

FIXTURE_DIR = Path(__file__).parent
ALL_TAGS = FIXTURE_DIR / "all_tags.json"


@pytest.fixture(scope="module")
def catalog():
    return load_tag_catalog(str(ALL_TAGS))


@pytest.fixture(scope="module")
def planner(catalog):
    return OfflineAccessPlanner(catalog, use_instance_ids=True)


def test_load_tag_catalog_returns_dict():
    tags = load_tag_catalog(str(ALL_TAGS))
    assert isinstance(tags, dict)
    assert "write_int2" in tags


def test_scalar_controller_tag_resolves_with_identity_separate_from_display_name(planner):
    plan = planner.plan("write_int2")

    assert isinstance(plan, OfflinePlan)
    assert plan.resolved is True
    assert bool(plan) is True
    assert plan.display_name == "write_int2"
    assert plan.symbol_identity["scope"] == "controller"
    assert plan.symbol_identity["catalog_key"] == "write_int2"
    assert plan.symbol_identity["instance_id"] == planner._tags["write_int2"]["instance_id"]
    assert plan.elements == 1
    assert plan.element_size == DataTypes["INT"].size
    assert plan.request_length == 2
    assert plan.indices == ()
    assert plan.bit is None
    assert plan.dimensions == (0, 0, 0)


def test_effective_path_matches_driver_encoder_for_every_fixture_tag(catalog, planner):
    symbolic_planner = OfflineAccessPlanner(catalog, use_instance_ids=False)

    for key, definition in catalog.items():
        plan = planner.plan(key)
        assert plan.resolved, (key, plan.reasons)

        driver_path = tag_request_path(plan.plc_tag, definition, True)
        assert plan.request_path == driver_path
        assert plan.symbolic_request_path == tag_request_path(
            plan.plc_tag, definition, False
        )

        symbolic_plan = symbolic_planner.plan(key)
        assert symbolic_plan.request_path == symbolic_plan.symbolic_request_path


def test_program_scoped_tag_is_symbolic_even_with_instance_policy(catalog, planner):
    request = "Program:pycomm3.read_prog_dint_2d_ary1[0,0]{3}"
    plan = planner.plan(request)

    assert plan.resolved
    assert plan.symbol_identity["scope"] == "program:pycomm3"
    assert plan.symbol_identity["program"] == "pycomm3"
    assert plan.instance_request_path is None
    assert plan.request_path == plan.symbolic_request_path
    assert plan.request_path == tag_request_path(
        plan.plc_tag, catalog[plan.symbol_identity["catalog_key"]], True
    )
    assert plan.request_length == 12
    assert plan.elements == 3


def test_unknown_program_tag_is_unresolved_without_path_bytes(planner):
    plan = planner.plan("Program:other.missing_tag")

    assert not plan.resolved
    assert plan.status == "unresolved"
    assert plan.request_path is None
    assert plan.symbolic_request_path is None
    assert plan.instance_request_path is None
    assert any("locked tag catalog" in reason for reason in plan.reasons)


def test_malformed_program_scope_is_unresolved(planner):
    for tag_request in ("Program:.leaf", "Program:pycomm3."):
        plan = planner.plan(tag_request)
        assert not plan.resolved
        assert plan.request_path is None


def test_same_named_type_cannot_be_backfilled_from_another_program():
    catalog = {
        "Program:A.foo": {
            "tag_name": "Program:A.foo",
            "dim": 0,
            "dimensions": [0, 0, 0],
            "instance_id": 1,
            "tag_type": "struct",
            "data_type_name": "SharedT",
            "data_type": {
                "name": "SharedT",
                "internal_tags": {
                    "x": {"offset": 0, "tag_type": "atomic",
                          "data_type": "SINT", "data_type_name": "SINT", "array": 0}
                },
                "attributes": ["x"],
                "template": {"structure_size": 1},
            },
        },
        "Program:B.foo": {
            "tag_name": "Program:B.foo",
            "dim": 0,
            "dimensions": [0, 0, 0],
            "instance_id": 2,
            "tag_type": "struct",
            "data_type_name": "SharedT",
            "data_type": {
                "name": "SharedT",
                "internal_tags": {
                    "y": {"offset": 0, "tag_type": "atomic",
                          "data_type": "INT", "data_type_name": "INT", "array": 0}
                },
                "attributes": ["y"],
                "template": {"structure_size": 2},
            },
        },
    }
    planner = OfflineAccessPlanner(catalog)

    assert not planner.plan("Program:A.foo.y").resolved
    assert not planner.plan("Program:B.foo.x").resolved
    assert planner.plan("Program:A.foo.x").resolved
    assert planner.plan("Program:B.foo.y").resolved


def test_three_dimensional_subscripts_validate_dimensions_and_bounds(planner):
    ok = planner.plan("write_dint_3d_ary1[1,2,0]")
    assert ok.resolved
    assert ok.indices == (1, 2, 0)
    assert ok.boundary["start_index"] == 15  # 1*9 + 2*3 + 0
    assert ok.boundary["capacity"] == 27

    wrong_rank = planner.plan("write_dint_3d_ary1[1,2]")
    assert not wrong_rank.resolved
    assert any("3 dimension(s)" in reason for reason in wrong_rank.reasons)

    out_of_bounds = planner.plan("write_dint_3d_ary1[1,2,3]")
    assert not out_of_bounds.resolved
    assert any("outside [0, 2]" in reason for reason in out_of_bounds.reasons)
    assert out_of_bounds.request_path is None


def test_subscript_on_scalar_tag_is_rejected(planner):
    plan = planner.plan("write_int2[0]")
    assert not plan.resolved
    assert any("not an array" in reason for reason in plan.reasons)


def test_negative_subscript_is_outside_bounds(planner):
    plan = planner.plan("write_dint_3d_ary1[0,0,-1]")
    assert not plan.resolved
    assert any("outside [0, 2]" in reason for reason in plan.reasons)


def test_bool_member_resolves_to_one_byte_and_explains_bit(planner):
    plan = planner.plan("Local:4:O.Range0En")
    assert plan.resolved
    assert plan.element_size == 1
    member = next(
        segment for segment in plan.segments
        if segment.kind == "member" and segment.name == "Range0En"
    )
    assert "bit 4" in member.detail


def test_nested_structure_members_walk_their_own_type(catalog, planner):
    plan = planner.plan("write_nested_udt1.udt_ary1[2].real")

    assert plan.resolved
    assert plan.plc_tag == "write_nested_udt1.udt_ary1[2].real"
    assert plan.element_size == DataTypes["REAL"].size
    member_segment = next(s for s in plan.segments if s.kind == "member" and s.name == "udt_ary1")
    assert member_segment.offset == 20
    assert plan.request_path == tag_request_path(
        plan.plc_tag, catalog["write_nested_udt1"], True
    )


def test_unknown_member_is_unresolved_and_never_guessed(planner):
    plan = planner.plan("write_nested_udt1.does_not_exist")
    assert not plan.resolved
    assert plan.request_path is None
    assert any("does not exist" in reason for reason in plan.reasons)


def test_member_array_request_staying_inside_boundary_resolves(planner):
    plan = planner.plan("write_udt2.sints[5]{3}")

    assert plan.resolved
    assert plan.boundary["local_span"] == (13, 16)
    assert plan.boundary["next_member_offset"] == 16
    assert plan.request_length == 3


def test_member_array_request_crossing_next_field_is_rejected(planner):
    plan = planner.plan("write_udt2.sints[5]{4}")

    assert not plan.resolved
    assert plan.request_path is None
    assert any("cross-member reads are not permitted" in reason for reason in plan.reasons)
    assert any("exceeds" in reason for reason in plan.reasons)


def test_member_array_request_crossing_structure_end_is_rejected(planner):
    plan = planner.plan("write_udt2.reals[9]{2}")

    assert not plan.resolved
    assert any("enclosing structure size" in reason for reason in plan.reasons)


def test_base_array_element_count_checked_against_remaining_capacity(planner):
    ok = planner.plan("write_dint_2d_ary1[4,3]{2}")
    assert ok.resolved
    assert ok.request_length == 8

    blocked = planner.plan("write_dint_2d_ary1[4,4]{5}")
    assert not blocked.resolved
    assert any("remaining" in reason for reason in blocked.reasons)


def test_dword_bool_array_sized_per_dword_element(planner):
    plan = planner.plan("read_bool_ary1{2}")
    assert plan.resolved
    assert plan.element_size == DataTypes["DWORD"].size
    assert plan.request_length == 8


def test_bit_selector_keeps_bit_separate_and_strips_from_plc_tag(planner):
    plan = planner.plan("write_int2.3")
    assert plan.resolved
    assert plan.bit == 3
    assert plan.plc_tag == "write_int2"
    assert plan.request_path == planner.plan("write_int2").request_path

    invalid = planner.plan("write_int2.99")
    assert not invalid.resolved
    assert any("outside" in reason for reason in invalid.reasons)


@pytest.mark.parametrize(
    "tag_request",
    [
        "",
        "   ",
        "garbage..tag",
        ".write_int2",
        "write_int2.",
        "write_int2[",
        "write_int2[1,]",
        "write_int2[]",
        "write_int2[abc]",
    ],
)
def test_malformed_paths_are_unresolved_without_bytes(planner, tag_request):
    plan = planner.plan(tag_request)
    assert not plan.resolved
    assert plan.request_path is None
    assert plan.symbolic_request_path is None


def test_explicit_element_count_must_be_positive(planner):
    plan = planner.plan("write_int2{0}")
    assert not plan.resolved
    assert any("positive integer" in reason for reason in plan.reasons)


def test_duplicate_element_count_specification_is_rejected(planner):
    plan = planner.plan("write_int2{2}", elements=3)
    assert not plan.resolved
    assert any("both" in reason for reason in plan.reasons)


def test_plan_all_preserves_order_and_independent_results(planner):
    requests = ["write_int2", "missing_tag", "write_dint_3d_ary1[0,0,0]"]
    plans = planner.plan_all(requests)

    assert [plan.request for plan in plans] == requests
    assert plans[0].resolved and not plans[1].resolved and plans[2].resolved


def test_segments_explain_every_wire_step(planner):
    plan = planner.plan("Program:pycomm3.read_prog_dint_2d_ary1[0,0]")
    assert plan.resolved

    kinds = [segment.kind for segment in plan.segments]
    assert kinds[0] == "scope"
    assert "symbol" in kinds and "array" in kinds
    assert all(isinstance(segment, PathSegment) for segment in plan.segments)
    assert all(segment.detail for segment in plan.segments)


def test_hex_path_properties_render_only_when_resolved(planner):
    resolved = planner.plan("write_int2")
    assert resolved.request_path_hex == resolved.request_path.hex(" ")
    assert resolved.symbolic_request_path_hex
    assert resolved.instance_request_path_hex

    unresolved = planner.plan("no_such_tag")
    assert unresolved.request_path_hex is None
    assert unresolved.symbolic_request_path_hex is None
    assert unresolved.instance_request_path_hex is None


def test_controller_tags_catalog_contains_no_program_entries():
    controller_only = load_tag_catalog(str(FIXTURE_DIR / "controller_tags.json"))
    planner = OfflineAccessPlanner(controller_only)

    assert not planner.plan("Program:pycomm3.read_prog_sint1").resolved
    assert planner.plan("write_sint_min").resolved
