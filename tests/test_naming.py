import json

import numpy as np
import pytest

from cdflib import CDF, cdfwrite
from cdflib.naming import (
    NamePlan,
    VariableIdentity,
    VariableNameConflict,
    apply_name_plan,
    build_name_plan,
    restore_name_plan,
)


def _write_variables(path, variables):
    with cdfwrite.CDF(path, cdf_spec={"Majority": "row_major"}) as cdf:
        for spec in variables:
            cdf.write_var(
                {
                    "Variable": spec["name"],
                    "Var_Type": spec.get("domain", "zvariable"),
                    "Data_Type": 21,
                    "Num_Elements": 1,
                    "Rec_Vary": True,
                    "Dim_Sizes": spec.get("dims", []),
                    "Dim_Vary": spec.get("dim_vary", [True]),
                },
                var_attrs=spec.get("attrs", {}),
                var_data=np.asarray(spec["data"], dtype=np.float32),
            )


def test_plan_selects_from_caller_supplied_legal_name_pool():
    identities = [VariableIdentity("a b", "zvariable"), VariableIdentity("a-b", "zvariable")]
    plan = build_name_plan(
        identities,
        reserved=["ab1_reserved"],
        legal_names={"a b": ["ab1"], ("a-b", "zvariable"): ["ab1", "ab2"]},
    )

    assert plan.assignments[("a b", "zvariable")] == "ab1"
    assert plan.assignments[("a-b", "zvariable")] == "ab2"
    assert len(set(plan.assignments.values())) == 2


def test_plan_does_not_share_a_target_for_identical_identities():
    with pytest.raises(VariableNameConflict):
        build_name_plan([VariableIdentity("Same", "zvariable"), VariableIdentity("Same", "zvariable")])


def test_plan_never_assigns_same_target_to_folded_names():
    identities = [
        VariableIdentity("Bx", "zvariable"),
        VariableIdentity("bx ", "zvariable"),
        VariableIdentity(" BX", "zvariable"),
    ]
    plan = build_name_plan(identities)
    targets = list(plan.assignments.values())

    assert len(targets) == len(set(targets))
    assert set(plan.mapping) == set(targets)
    assert plan.mapping[plan.target_for("bx ", "zvariable")] == "bx "


def test_plan_uses_caller_legality_and_reserved_rules():
    identities = [
        VariableIdentity("data", "zvariable"),
        VariableIdentity("1bad", "zvariable"),
        VariableIdentity("has space", "zvariable"),
    ]
    legal_names = set()

    def is_legal(name):
        legal_names.add(name)
        return name.isidentifier() and not name.startswith("_")

    plan = build_name_plan(identities, reserved=["DATA"], is_legal=is_legal)
    targets = list(plan.assignments.values())

    assert len(targets) == len(set(targets))
    assert all(name.isidentifier() for name in targets)
    assert all(name.casefold() != "data" for name in targets)
    assert legal_names


def test_ambiguous_depend_reference_stays_conflicted_and_is_not_position_selected():
    identities = [
        VariableIdentity("Foo", "zvariable"),
        VariableIdentity("foo", "zvariable"),
        VariableIdentity("Bar", "zvariable"),
    ]
    plan = build_name_plan(identities, depends={"Bar": {0: "FOO"}})

    assert plan.depend_targets[(("Bar", "zvariable"), 0)] is None
    assert set(plan.conflicts[(("Bar", "zvariable"), 0)]) == {"Foo", "foo"}

    reversed_plan = build_name_plan(list(reversed(identities)), depends={"Bar": {0: "FOO"}})
    assert reversed_plan.depend_targets[(("Bar", "zvariable"), 0)] is None
    assert plan.assignments == reversed_plan.assignments


def test_unique_depend_reference_migrates_with_mapping():
    identities = [
        VariableIdentity("Epoch", "zvariable"),
        VariableIdentity("Bx", "zvariable"),
    ]
    plan = build_name_plan(identities, depends={"Bx": {0: " epoch "}})

    migrated = plan.depend_targets[(("Bx", "zvariable"), 0)]
    assert migrated == plan.target_for("Epoch", "zvariable")
    assert plan.depend_reference("Bx", 0) == migrated


def test_plan_is_stable_when_physical_variable_order_changes():
    identities = [VariableIdentity("a", "zvariable"), VariableIdentity("A ", "zvariable")]
    first = build_name_plan(identities)
    second = build_name_plan(list(reversed(identities)))

    assert first.assignments == second.assignments
    assert first.reverse == second.reverse


def test_plan_keeps_r_and_z_domains_distinct():
    identities = [VariableIdentity("E", "zvariable"), VariableIdentity("E", "rvariable")]
    plan = build_name_plan(identities)

    z_target = plan.target_for("E", "zvariable")
    r_target = plan.target_for("E", "rvariable")
    assert z_target != r_target
    assert plan.domains[z_target] == "zvariable"
    assert plan.domains[r_target] == "rvariable"
    assert plan.mapping[z_target] == "E"
    assert plan.mapping[r_target] == "E"


def test_plan_manifest_round_trip_and_apply_restore():
    identities = [
        VariableIdentity("X", "zvariable"),
        VariableIdentity("x ", "zvariable"),
        VariableIdentity("Epoch", "zvariable"),
    ]
    plan = build_name_plan(identities, depends={"X": {0: "Epoch"}})
    restored_plan = NamePlan.from_dict(json.loads(json.dumps(plan.as_dict())))

    assert restored_plan.assignments == plan.assignments
    assert restored_plan.depend_targets == plan.depend_targets

    renames, attributes = apply_name_plan(
        plan,
        variable_names={"X": "zvariable", "x ": "zvariable", "Epoch": "zvariable"},
        attributes={"X": {"DEPEND_0": "Epoch"}},
    )
    assert renames["X"] == plan.target_for("X", "zvariable")
    assert attributes["X"]["DEPEND_0"] == plan.target_for("Epoch", "zvariable")

    restored_names, restored_attributes = restore_name_plan(
        plan,
        variable_names={target: plan.domains[target] for target in plan.reverse},
        attributes={renames["X"]: attributes["X"]},
    )
    assert set(restored_names.values()) == {"X", "x ", "Epoch"}
    assert restored_attributes[renames["X"]]["DEPEND_0"] == "Epoch"


def test_depend_reference_uses_exact_spelling_even_when_folded_names_exist():
    identities = [
        VariableIdentity("E", "zvariable"),
        VariableIdentity("e", "zvariable"),
        VariableIdentity("E", "rvariable"),
        VariableIdentity("data", "zvariable"),
    ]
    plan = build_name_plan(identities, depends={"data": {0: "E"}})

    # "E" is an exact spelling in both domains, so the dependent variable's
    # own domain decides it.  The folded "e" is never considered.
    assert plan.depend_reference("data", 0) == plan.target_for("E", "zvariable")


def test_depend_reference_stays_conflicted_when_folded_match_is_not_unique():
    identities = [
        VariableIdentity("E", "zvariable"),
        VariableIdentity("e", "zvariable"),
        VariableIdentity("data", "zvariable"),
    ]
    plan = build_name_plan(identities, depends={"data": {0: " e "}})

    assert plan.depend_reference("data", 0) is None
    assert set(plan.conflicts[(("data", "zvariable"), 0)]) == {"E", "e"}


def test_reader_distinguishes_colliding_names_without_position_bias(tmp_path):
    path = tmp_path / "collide.cdf"
    _write_variables(
        path,
        [
            {"name": "Bx", "data": [1.0, 1.0]},
            {"name": "bx ", "data": [2.0, 2.0]},
            {"name": "BX", "data": [3.0, 3.0]},
        ],
    )

    with CDF(path) as cdf:
        assert [identity.name for identity in cdf.variable_identities()] == ["Bx", "bx ", "BX"]
        np.testing.assert_array_equal(cdf.varget("Bx"), np.array([1.0, 1.0], dtype=np.float32))
        np.testing.assert_array_equal(cdf.varget("bx "), np.array([2.0, 2.0], dtype=np.float32))
        np.testing.assert_array_equal(cdf.varget("BX"), np.array([3.0, 3.0], dtype=np.float32))

        # A folded query matches all three names, so no one is selected.
        with pytest.raises(VariableNameConflict) as error:
            cdf.varget("bx")
        assert len(error.value.candidates) == 3


def test_reader_unambiguous_folded_lookup_still_works(tmp_path):
    path = tmp_path / "plain.cdf"
    _write_variables(path, [{"name": "Flux", "data": [1.0, 2.0]}])

    with CDF(path) as cdf:
        np.testing.assert_array_equal(cdf.varget("flux"), np.array([1.0, 2.0], dtype=np.float32))


def test_writer_translates_separate_variable_attributes(tmp_path):
    path = tmp_path / "separate.cdf"
    plan = build_name_plan(
        [VariableIdentity("Epoch", "zvariable"), VariableIdentity("Bx", "zvariable"), VariableIdentity("bx ", "zvariable")]
    )
    with cdfwrite.CDF(path, name_plan=plan) as cdf:
        for name in ("Epoch", "Bx", "bx "):
            cdf.write_var(
                {"Variable": name, "Data_Type": 21, "Num_Elements": 1, "Rec_Vary": True, "Dim_Sizes": []},
                var_data=np.asarray([1.0, 2.0], dtype=np.float32),
            )
        cdf.write_variableattrs({"DEPEND_0": {"Bx": "Epoch", "bx ": "Epoch"}})

    with CDF(path) as cdf:
        assert cdf.varattsget(plan.target_for("Bx", "zvariable"), domain="zvariable")["DEPEND_0"] == plan.target_for(
            "Epoch", "zvariable"
        )
        assert cdf.varattsget(plan.target_for("bx ", "zvariable"), domain="zvariable")["DEPEND_0"] == plan.target_for(
            "Epoch", "zvariable"
        )


def test_writer_persists_plan_in_compressed_file(tmp_path):
    path = tmp_path / "compressed.cdf"
    plan = build_name_plan([VariableIdentity("Bx", "zvariable"), VariableIdentity("bx ", "zvariable")])
    with cdfwrite.CDF(path, cdf_spec={"Compressed": 6}, name_plan=plan) as cdf:
        for name, value in (("Bx", 1.0), ("bx ", 2.0)):
            cdf.write_var(
                {"Variable": name, "Data_Type": 21, "Num_Elements": 1, "Rec_Vary": True, "Dim_Sizes": []},
                var_data=np.asarray([value, value], dtype=np.float32),
            )

    with CDF(path) as cdf:
        assert cdf.stored_name_plan() is not None
        assert cdf.varget_original("bx ")[0] == np.float32(2.0)


def test_reader_depend_migration_and_restore_for_r_and_z_domains(tmp_path):
    path = tmp_path / "domains.cdf"
    with cdfwrite.CDF(path, cdf_spec={"Majority": "row_major", "rDim_sizes": [2]}) as cdf:
        for spec in [
            {"Variable": "Epoch", "Var_Type": "zvariable"},
            {"Variable": "data", "Var_Type": "zvariable"},
            {"Variable": "Epoch", "Var_Type": "rvariable"},
            {"Variable": "rdata", "Var_Type": "rvariable"},
        ]:
            is_rvar = spec["Var_Type"] == "rvariable"
            cdf.write_var(
                {
                    "Variable": spec["Variable"],
                    "Var_Type": spec["Var_Type"],
                    "Data_Type": 21,
                    "Num_Elements": 1,
                    "Rec_Vary": True,
                    "Dim_Sizes": [] if not is_rvar else [1],
                    "Dim_Vary": [True],
                },
                var_attrs={"DEPEND_0": "Epoch"} if "data" in spec["Variable"] else {},
                var_data=np.asarray([1.0, 2.0], dtype=np.float32),
            )

    with CDF(path) as cdf:
        plan = cdf.variable_name_map()
        z_epoch = plan.target_for("Epoch", "zvariable")
        r_epoch = plan.target_for("Epoch", "rvariable")
        assert z_epoch != r_epoch
        assert plan.mapping[z_epoch] == "Epoch"
        assert plan.mapping[r_epoch] == "Epoch"
        assert plan.domains[z_epoch] == "zvariable"
        assert plan.domains[r_epoch] == "rvariable"

        # Both DEPEND_0 references are raw "Epoch"; the r/z domain of the
        # dependent variable makes each one unique.
        assert plan.depend_reference("data", 0) == plan.target_for("Epoch", "zvariable")
        assert plan.depend_reference("rdata", 0) == plan.target_for("Epoch", "rvariable")


def test_writer_applies_plan_and_reader_restores_original_identity(tmp_path):
    path = tmp_path / "roundtrip.cdf"
    source = [
        ("Bx", "zvariable", [1.0, 1.0]),
        ("bx ", "zvariable", [2.0, 2.0]),
        ("Epoch", "zvariable", [0.0, 1.0]),
    ]
    plan = build_name_plan(
        [VariableIdentity(name, domain) for name, domain, _ in source],
        depends={"Bx": {0: "Epoch"}, "bx ": {0: "Epoch"}},
    )

    with cdfwrite.CDF(path, cdf_spec={"Majority": "row_major"}, name_plan=plan) as cdf:
        for name, domain, data in source:
            cdf.write_var(
                {
                    "Variable": name,
                    "Var_Type": domain,
                    "Data_Type": 21,
                    "Num_Elements": 1,
                    "Rec_Vary": True,
                    "Dim_Sizes": [],
                },
                var_attrs={"DEPEND_0": "Epoch"} if name != "Epoch" else {},
                var_data=np.asarray(data, dtype=np.float32),
            )

    with CDF(path) as cdf:
        stored = cdf.stored_name_plan()
        assert stored is not None
        targets = cdf.cdf_info().zVariables
        assert len(targets) == len(set(targets))

        # Every target resolves to its own source value, independent of order.
        for name, domain, data in reversed(source):
            target = stored.target_for(name, domain)
            np.testing.assert_array_equal(cdf.varget(target, domain=domain), np.asarray(data, dtype=np.float32))
            np.testing.assert_array_equal(cdf.varget_original(name, domain=domain), np.asarray(data, dtype=np.float32))

        bx = stored.target_for("Bx", "zvariable")
        bx_space = stored.target_for("bx ", "zvariable")
        assert cdf.varattsget(bx, domain="zvariable")["DEPEND_0"] == stored.target_for("Epoch", "zvariable")
        assert cdf.varattsget(bx_space, domain="zvariable")["DEPEND_0"] == stored.target_for("Epoch", "zvariable")
        assert cdf.restored_variable_name(bx, "zvariable") == "Bx"
        assert cdf.restored_variable_name(bx_space, "zvariable") == "bx "
