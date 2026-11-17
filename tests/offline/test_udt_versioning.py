import copy

import pytest

from pycomm3.udt_versioning import (
    Acquisition,
    CatalogStore,
    ChangeKind,
    DeviceMismatchError,
    MissingCatalogError,
    MissingTemplateError,
    UDTCatalog,
    build_type_layout,
    catalog_from_tags,
    compare_catalogs,
    compare_type_definitions,
    dependency_chain,
    snapshot_types,
)
from pycomm3.cip import Struct, DINT, REAL, SINT, BOOL, UDINT, DataTypes


def member(offset, type_name="DINT", array=0, bit=None, nested=None, tag_type="atomic"):
    info = {
        "offset": offset,
        "tag_type": tag_type,
        "data_type": nested if nested is not None else type_name,
        "data_type_name": type_name,
        "array": array,
    }
    if bit is not None:
        info["bit"] = bit
    return info


def udt(name, size, members):
    internal = {}
    for member_name, info in members:
        internal[member_name] = info
    return {
        "name": name,
        "internal_tags": internal,
        "attributes": [name for name, _ in members],
        "template": {
            "object_definition_size": 0,
            "structure_size": size,
            "member_count": len(members),
            "structure_handle": 0,
        },
    }


def tag(name, type_name, definition, dim=0, dimensions=None):
    return {
        "tag_name": name,
        "tag_type": "struct",
        "data_type_name": type_name,
        "data_type": definition,
        "dim": dim,
        "dimensions": dimensions or [0, 0, 0],
        "instance_id": 1,
        "external_access": "Read/Write",
    }


def simple(name="Simple", size=8, members=None):
    members = members or [
        ("a", member(0, "DINT")),
        ("b", member(4, "REAL")),
    ]
    return udt(name, size, members)


def test_identical_layouts_are_unchanged():
    definition = simple()
    result = compare_type_definitions(definition, copy.deepcopy(definition))
    assert result.verdict is ChangeKind.UNCHANGED
    assert result.changes == ()


def test_member_rename_is_rename_only_when_slot_is_identical():
    old = simple()
    new = simple(members=[("renamed", member(0, "DINT")), ("b", member(4, "REAL"))])
    result = compare_type_definitions(old, new)
    assert result.verdict is ChangeKind.RENAME_ONLY
    assert any(c.kind is ChangeKind.RENAME_ONLY for c in result.changes)


def test_type_rename_with_identical_layout_is_rename_only():
    old = simple("OldName")
    new = simple("NewName")
    result = compare_type_definitions(old, new)
    assert result.verdict is ChangeKind.RENAME_ONLY
    assert result.changes[0].old == "OldName"
    assert result.changes[0].new == "NewName"


def test_same_slot_dint_to_real_is_layout_compatible_not_rename():
    old = simple()
    new = simple(members=[("a", member(0, "REAL")), ("b", member(4, "REAL"))])
    result = compare_type_definitions(old, new)
    assert result.verdict is ChangeKind.LAYOUT_COMPATIBLE
    assert any(
        c.kind is ChangeKind.LAYOUT_COMPATIBLE and c.old == "DINT" and c.new == "REAL"
        for c in result.changes
    )


def test_offset_change_requires_reread():
    old = simple()
    new = simple(members=[("a", member(4, "DINT")), ("b", member(0, "REAL"))])
    result = compare_type_definitions(old, new)
    assert result.verdict is ChangeKind.REREAD_REQUIRED


def test_bit_position_change_requires_reread_even_with_same_host_offsets():
    old = simple(
        size=4,
        members=[
            ("host", member(0, "DINT")),
            ("flag", member(0, "BOOL", bit=0)),
        ],
    )
    new = simple(
        size=4,
        members=[
            ("host", member(0, "DINT")),
            ("flag", member(0, "BOOL", bit=1)),
        ],
    )
    result = compare_type_definitions(old, new)
    assert result.verdict is ChangeKind.REREAD_REQUIRED


def test_array_extent_change_requires_reread():
    old = simple(size=8, members=[("vals", member(0, "INT", array=4))])
    new = simple(size=8, members=[("vals", member(0, "INT", array=2))])
    result = compare_type_definitions(old, new)
    assert result.verdict is ChangeKind.REREAD_REQUIRED


def test_outer_size_change_requires_reread():
    old = simple(size=8)
    new = simple(size=12)
    result = compare_type_definitions(old, new)
    assert result.verdict is ChangeKind.REREAD_REQUIRED
    assert any("outer structure size" in c.detail for c in result.changes)


def test_nested_change_is_found_even_when_outer_size_is_unchanged():
    old_inner = udt(
        "Inner", 8, [("x", member(0, "DINT")), ("y", member(4, "REAL"))]
    )
    new_inner = udt(
        "Inner", 8, [("x", member(0, "REAL")), ("y", member(4, "REAL"))]
    )
    old = udt(
        "Outer",
        16,
        [
            (
                "nested",
                member(0, "Inner", tag_type="struct", nested=old_inner),
            ),
            ("tail", member(8, "DINT")),
        ],
    )
    new = udt(
        "Outer",
        16,
        [
            (
                "nested",
                member(0, "Inner", tag_type="struct", nested=new_inner),
            ),
            ("tail", member(8, "DINT")),
        ],
    )

    result = compare_type_definitions(old, new)
    assert result.verdict is ChangeKind.LAYOUT_COMPATIBLE
    assert any(c.path.endswith("nested.x") for c in result.changes)


def test_nested_bit_change_hidden_by_equal_sizes_requires_reread():
    old_inner = udt(
        "Inner",
        4,
        [("host", member(0, "DINT")), ("flag", member(0, "BOOL", bit=2))],
    )
    new_inner = udt(
        "Inner",
        4,
        [("host", member(0, "DINT")), ("flag", member(0, "BOOL", bit=3))],
    )
    old = udt("Outer", 8, [("n", member(0, "Inner", tag_type="struct", nested=old_inner))])
    new = udt("Outer", 8, [("n", member(0, "Inner", tag_type="struct", nested=new_inner))])
    result = compare_type_definitions(old, new)
    assert result.verdict is ChangeKind.REREAD_REQUIRED


def test_array_of_udt_nested_change_requires_reread():
    old_elem = udt("Elem", 8, [("a", member(0, "DINT")), ("b", member(4, "DINT"))])
    new_elem = udt("Elem", 8, [("a", member(0, "DINT")), ("b", member(4, "REAL"))])
    old = udt(
        "ArrayHolder",
        24,
        [("items", member(0, "Elem", array=3, tag_type="struct", nested=old_elem))],
    )
    new = udt(
        "ArrayHolder",
        24,
        [("items", member(0, "Elem", array=3, tag_type="struct", nested=new_elem))],
    )
    result = compare_type_definitions(old, new)
    assert result.verdict is ChangeKind.LAYOUT_COMPATIBLE
    assert any(c.path == "ArrayHolder.items[3].b" for c in result.changes)


def test_unknown_atomic_type_makes_comparison_undetermined():
    old = simple()
    new = simple(members=[("a", member(0, "NOT_A_TYPE")), ("b", member(4, "REAL"))])
    result = compare_type_definitions(old, new)
    assert result.verdict is ChangeKind.UNDETERMINED


def test_missing_new_template_is_undetermined_not_copied_from_old():
    old_def = simple()
    old = UDTCatalog("plc1", "v1", [old_def])
    new = UDTCatalog("plc1", "v2", [])
    tags = [tag("t", "Simple", old_def)]

    report = compare_catalogs(old, new, tags)
    impact = report.tags[0]
    assert impact.verdict is ChangeKind.UNDETERMINED
    assert impact.chain[0].missing is True
    assert "missing" in impact.reason


def test_missing_nested_template_keeps_tag_undetermined():
    inner = udt("Inner", 4, [("x", member(0, "DINT"))])
    old_outer = udt(
        "Outer",
        8,
        [("n", member(0, "Inner", tag_type="struct", nested=inner))],
    )
    new_outer = udt(
        "Outer",
        8,
        [("n", member(0, "Inner", tag_type="struct"))],
    )
    old = UDTCatalog("plc1", "v1", [old_outer, inner])
    new = UDTCatalog("plc1", "v2", [new_outer])

    report = compare_catalogs(old, new, [tag("t", "Outer", old_outer)])
    impact = report.tags[0]
    assert impact.verdict is ChangeKind.UNDETERMINED
    assert any(link.missing for link in impact.chain if link.type_name == "Inner")


def test_catalogs_from_different_devices_cannot_be_compared():
    old = UDTCatalog("plc-a", "v1", [simple()])
    new = UDTCatalog("plc-b", "v2", [simple()])
    with pytest.raises(DeviceMismatchError):
        compare_catalogs(old, new, [tag("t", "Simple", simple())])


def test_same_named_type_on_another_device_is_not_used():
    definition = simple()
    store = CatalogStore()
    store.add_catalog("plc-a", "v1", [definition])
    with pytest.raises(MissingTemplateError):
        store.get_layout("plc-b", "v1", "Simple")
    with pytest.raises(MissingCatalogError):
        store.get("plc-b", "v1")


def test_dependency_chain_reports_root_and_nested_array_types():
    inner = udt("Inner", 4, [("x", member(0, "DINT"))])
    outer = udt(
        "Outer",
        16,
        [
            ("single", member(0, "Inner", tag_type="struct", nested=inner)),
            ("many", member(4, "Inner", array=3, tag_type="struct", nested=inner)),
        ],
    )
    catalog = UDTCatalog("plc1", "v1", [outer, inner])
    chain = dependency_chain(tag("t", "Outer", outer), catalog)
    assert chain[0] == "Outer"
    assert "Inner" in chain
    assert "Inner[3]" in chain


def test_report_only_calls_out_affected_tags_and_summarizes():
    old_def = simple()
    new_def = simple(members=[("a", member(0, "REAL")), ("b", member(4, "REAL"))])
    old = UDTCatalog("plc1", "v1", [old_def])
    new = UDTCatalog("plc1", "v2", [new_def])
    tags = [tag("changed", "Simple", old_def)]
    report = compare_catalogs(old, new, tags)
    assert report.summary()["layout_compatible"] == 1
    assert report.affected_tags[0].tag_name == "changed"
    assert report.affected_tags[0].describe_chain() == "Simple"


def test_acquisition_keeps_raw_bytes_and_values_under_original_version():
    acquisition = Acquisition.capture(
        "t", "plc1", "v1", "Simple", b"\x01\x00\x00\x00", {"a": 1, "b": 0.0}
    )
    reinterpreted = acquisition.reinterpret("v2", {"a": 1.0, "b": 0.0})

    assert acquisition.catalog_version == "v1"
    assert acquisition.values == {"a": 1, "b": 0.0}
    assert reinterpreted.catalog_version == "v2"
    assert reinterpreted.raw == acquisition.raw

    copy_of_values = acquisition.values
    copy_of_values["a"] = 2
    assert acquisition.values == {"a": 1, "b": 0.0}


def test_snapshot_types_does_not_share_mutable_definitions():
    definitions = {"Simple": simple()}
    snapshot = snapshot_types(definitions)
    snapshot["Simple"]["template"]["structure_size"] = 999
    assert definitions["Simple"]["template"]["structure_size"] == 8


def test_tag_dimension_change_requires_reread():
    old_def = simple()
    old = UDTCatalog("plc1", "v1", [old_def])
    new = UDTCatalog("plc1", "v2", [old_def])
    old_tags = [tag("arr", "Simple", old_def, dim=1, dimensions=[4, 0, 0])]
    new_tags = [tag("arr", "Simple", old_def, dim=1, dimensions=[8, 0, 0])]
    report = compare_catalogs(old, new, old_tags, new_tags)
    assert report.tags[0].verdict is ChangeKind.REREAD_REQUIRED


def test_atomic_width_change_requires_reread():
    old = UDTCatalog("plc1", "v1", [])
    new = UDTCatalog("plc1", "v2", [])
    old_tags = [{"tag_name": "a", "tag_type": "atomic", "data_type_name": "DINT"}]
    new_tags = [{"tag_name": "a", "tag_type": "atomic", "data_type_name": "LINT"}]
    report = compare_catalogs(old, new, old_tags, new_tags)
    assert report.tags[0].verdict is ChangeKind.REREAD_REQUIRED


def test_atomic_same_width_reinterpretation_is_layout_compatible():
    old = UDTCatalog("plc1", "v1", [])
    new = UDTCatalog("plc1", "v2", [])
    old_tags = [{"tag_name": "a", "tag_type": "atomic", "data_type_name": "DINT"}]
    new_tags = [{"tag_name": "a", "tag_type": "atomic", "data_type_name": "UDINT"}]
    report = compare_catalogs(old, new, old_tags, new_tags)
    assert report.tags[0].verdict is ChangeKind.LAYOUT_COMPATIBLE


def test_archive_assess_marks_reread_without_mutating_records():
    from pycomm3.udt_versioning import AcquisitionArchive

    old_def = simple(size=8)
    new_def = simple(size=12, members=[("a", member(0, "DINT")), ("b", member(4, "REAL"))])
    archive = AcquisitionArchive("plc1")
    archive.add_catalog("v1", data_types={"Simple": old_def}, tags=[tag("t", "Simple", old_def)])
    old_record = archive.record("t", "v1", b"\x01\x00\x00\x00\x00\x00\x00\x00", {"a": 1, "b": 0.0})
    archive.add_catalog("v2", data_types={"Simple": new_def})

    assessment = archive.assess("v1", "v2")
    assert assessment.report.tags[0].verdict is ChangeKind.REREAD_REQUIRED
    assert assessment.reread_required == (old_record,)
    assert archive.get("t", "v1").values == {"a": 1, "b": 0.0}
    assert archive.get("t", "v1").raw == old_record.raw


def test_archive_reinterpret_creates_explicit_new_record_only():
    from pycomm3.udt_versioning import AcquisitionArchive

    definition = simple()
    archive = AcquisitionArchive("plc1")
    archive.add_catalog("v1", data_types={"Simple": definition})
    archive.add_catalog("v2", data_types={"Simple": copy.deepcopy(definition)})
    original = archive.record("t", "v1", b"\x00" * 8, {"a": 1, "b": 0.0}, type_name="Simple")

    updated = archive.reinterpret("t", "v1", "v2", {"a": 1, "b": 0.0})
    assert updated is not original
    assert archive.get("t", "v1").catalog_version == "v1"
    assert archive.get("t", "v2").catalog_version == "v2"
    assert archive.get("t", "v1").values == {"a": 1, "b": 0.0}


def test_archive_refuses_missing_new_type_instead_of_falling_back():
    from pycomm3.udt_versioning import AcquisitionArchive

    definition = simple()
    archive = AcquisitionArchive("plc1")
    archive.add_catalog("v1", data_types={"Simple": definition})
    archive.add_catalog("v2", data_types={})
    archive.record("t", "v1", b"\x00" * 8, {"a": 1, "b": 0.0}, type_name="Simple")

    assessment = archive.assess("v1", "v2")
    assert assessment.report.tags[0].verdict is ChangeKind.UNDETERMINED
    assert assessment.undetermined[0].catalog_version == "v1"


def test_catalog_from_tags_uses_explicit_definition_over_nested_copy():
    old_direct = udt("Inner", 4, [("x", member(0, "DINT"))])
    changed_direct = udt("Inner", 4, [("x", member(0, "REAL"))])
    parent_with_stale_copy = udt(
        "Outer",
        8,
        [("n", member(0, "Inner", tag_type="struct", nested=old_direct))],
    )
    tags = [
        tag("outer", "Outer", parent_with_stale_copy),
        tag("inner", "Inner", changed_direct),
    ]
    catalog = catalog_from_tags("plc1", "v2", tags)
    assert catalog.get_layout("Inner").member("x").type_name == "REAL"


def test_conflicting_explicit_definitions_are_undetermined():
    first = simple()
    second = simple(members=[("a", member(0, "REAL")), ("b", member(4, "REAL"))])
    catalog = UDTCatalog("plc1", "v2", [first, second])
    layout = catalog.get_layout("Simple")
    assert layout.complete is False
    assert "conflicting" in layout.missing_reason


def test_nested_type_rename_is_rename_only_when_layout_is_identical():
    old_inner = udt("OldInner", 4, [("x", member(0, "DINT"))])
    new_inner = udt("NewInner", 4, [("x", member(0, "DINT"))])
    old = udt(
        "Outer",
        4,
        [("n", member(0, "OldInner", tag_type="struct", nested=old_inner))],
    )
    new = udt(
        "Outer",
        4,
        [("n", member(0, "NewInner", tag_type="struct", nested=new_inner))],
    )
    result = compare_type_definitions(old, new)
    assert result.verdict is ChangeKind.RENAME_ONLY


def test_dependency_chain_marks_missing_nested_type():
    inner = udt("Inner", 4, [("x", member(0, "DINT"))])
    outer = udt(
        "Outer",
        8,
        [("n", member(0, "Inner", tag_type="struct", nested=inner))],
    )
    partial_outer = udt(
        "Outer",
        8,
        [("n", member(0, "Inner", tag_type="struct"))],
    )
    catalog = catalog_from_tags("plc1", "v2", [tag("x", "Outer", partial_outer)])
    impact = compare_catalogs(
        UDTCatalog("plc1", "v1", [outer, inner]),
        catalog,
        [tag("t", "Outer", outer)],
    ).tags[0]
    assert impact.verdict is ChangeKind.UNDETERMINED
    assert any(link.missing and link.type_name == "Inner" for link in impact.chain)


def test_atomic_to_same_size_struct_is_layout_compatible():
    inner = udt("Inner", 4, [("x", member(0, "DINT"))])
    old = udt("Outer", 4, [("slot", member(0, "DINT"))])
    new = udt(
        "Outer",
        4,
        [("slot", member(0, "Inner", tag_type="struct", nested=inner))],
    )
    result = compare_type_definitions(old, new)
    assert result.verdict is ChangeKind.LAYOUT_COMPATIBLE
    assert any("atomic/structured" in c.detail for c in result.changes)
