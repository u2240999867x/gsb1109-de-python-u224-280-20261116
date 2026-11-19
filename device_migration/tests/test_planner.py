"""device_migration 规划器的行为测试。"""

from __future__ import annotations

import copy

import pytest

from device_migration import (
    Allocation,
    CatalogPlanner,
    Conflict,
    DerivedSet,
    MigrationConflict,
    MigrationRule,
    Observation,
    ParameterKind,
    ParameterSpec,
    ReferenceRule,
    Scenario,
)
from device_migration.model import as_key, as_value_key


STATION = "S1"
TIMES = ("T1", "T2")


def _obs(parameter, device, time, value):
    return Observation(parameter, device, time, STATION, value)


def _base_observations():
    return [
        _obs("capacity_mw", "D1", "T1", 100.0),
        _obs("capacity_mw", "D1", "T2", 110.0),
        _obs("efficiency", "D1", "T1", 0.90),
        _obs("efficiency", "D1", "T2", 0.90),
        _obs("asset_code", "D1", "T1", "A1"),
        _obs("asset_code", "D1", "T2", "A1"),
        _obs("capacity_mw", "D2", "T1", 50.0),
        _obs("capacity_mw", "D2", "T2", 55.0),
        _obs("efficiency", "D2", "T1", 0.80),
        _obs("efficiency", "D2", "T2", 0.80),
        _obs("asset_code", "D2", "T1", "A2"),
        _obs("asset_code", "D2", "T2", "A2"),
        _obs("capacity_mw", "D3", "T1", 25.0),
        _obs("capacity_mw", "D3", "T2", 20.0),
        _obs("efficiency", "D3", "T1", 0.75),
        _obs("efficiency", "D3", "T2", 0.75),
        _obs("asset_code", "D3", "T1", "A3"),
        _obs("asset_code", "D3", "T2", "A3"),
        _obs("capacity_mw", "D4", "T1", 10.0),
        _obs("capacity_mw", "D4", "T2", 10.0),
        _obs("efficiency", "D4", "T1", 0.70),
        _obs("efficiency", "D4", "T2", 0.70),
        _obs("asset_code", "D4", "T1", "A4"),
        _obs("asset_code", "D4", "T2", "A4"),
        _obs("capacity_mw", "D5", "T1", 5.0),
        _obs("capacity_mw", "D5", "T2", 5.0),
        _obs("efficiency", "D5", "T1", 0.60),
        _obs("efficiency", "D5", "T2", 0.60),
        _obs("asset_code", "D5", "T1", "A5"),
        _obs("asset_code", "D5", "T2", "A5"),
    ]


def _old_scenario(observations=None):
    observations = _base_observations() if observations is None else observations
    return Scenario(
        name="old",
        devices=("D1", "D2", "D3", "D4", "D5"),
        stations=(STATION,),
        times=TIMES,
        device_station={d: STATION for d in ("D1", "D2", "D3", "D4", "D5")},
        observations=tuple(observations),
    )


def _specs():
    return {
        "capacity_mw": ParameterSpec(
            name="capacity_mw",
            kind=ParameterKind.ADDITIVE,
            bounds=(0.0, None),
            required=True,
        ),
        "efficiency": ParameterSpec(
            name="efficiency",
            kind=ParameterKind.NON_ADDITIVE,
            bounds=(0.0, 1.0),
        ),
        "asset_code": ParameterSpec(
            name="asset_code",
            kind=ParameterKind.NON_ADDITIVE,
            unique_within=("station",),
        ),
        "group_code": ParameterSpec(
            name="group_code",
            kind=ParameterKind.NON_ADDITIVE,
        ),
        "carbon_intensity": ParameterSpec(
            name="carbon_intensity",
            kind=ParameterKind.DERIVED,
            bounds=(0.0, None),
        ),
    }


def _formulas():
    def weighted_efficiency(ctx):
        return sum(ctx.sources.values()) / len(ctx.sources)

    def carbon_intensity(ctx):
        capacity = ctx.members["capacity_mw"]
        return round(1000.0 / capacity, 6) if capacity else 0.0

    return {
        "avg_efficiency": weighted_efficiency,
        "carbon_from_capacity": carbon_intensity,
    }


def _new_catalog():
    return {device: STATION for device in ("D1a", "D1b", "D6", "D7")}


def _split_rules():
    return [
        MigrationRule(
            parameter="capacity_mw",
            operation="split",
            sources=("D1",),
            targets=("D1a", "D1b"),
            allocations=(
                Allocation(target="D1a", fraction=0.6),
                Allocation(target="D1b", fraction=0.4),
            ),
        ),
        MigrationRule(
            parameter="efficiency",
            operation="split",
            sources=("D1",),
            targets=("D1a", "D1b"),
            allocations=(
                Allocation(target="D1a", share=0.91),
                Allocation(target="D1b", share=0.89),
            ),
        ),
        MigrationRule(
            parameter="asset_code",
            operation="split",
            sources=("D1",),
            targets=("D1a", "D1b"),
            allocations=(
                Allocation(target="D1a", share="A1a"),
                Allocation(target="D1b", share="A1b"),
            ),
        ),
    ]


def _merge_rules(aggregate="formula"):
    efficiency_rule = MigrationRule(
        parameter="efficiency",
        operation="merge",
        sources=("D2", "D3"),
        targets=("D7",),
        aggregate=aggregate,
        formula="avg_efficiency" if aggregate == "formula" else None,
    )
    return [
        MigrationRule(
            parameter="capacity_mw",
            operation="merge",
            sources=("D2", "D3"),
            targets=("D7",),
            aggregate="sum",
        ),
        efficiency_rule,
        MigrationRule(
            parameter="asset_code",
            operation="merge",
            sources=("D2", "D3"),
            targets=("D7",),
            aggregate="formula",
            formula="pick_first_code",
        ),
    ]


def _rename_retire_rules():
    return [
        MigrationRule(
            parameter="capacity_mw",
            operation="rename",
            sources=("D4",),
            targets=("D6",),
        ),
        MigrationRule(
            parameter="efficiency",
            operation="rename",
            sources=("D4",),
            targets=("D6",),
        ),
        MigrationRule(
            parameter="asset_code",
            operation="rename",
            sources=("D4",),
            targets=("D6",),
        ),
        MigrationRule(
            parameter="capacity_mw",
            operation="retire",
            sources=("D5",),
        ),
        MigrationRule(
            parameter="efficiency",
            operation="retire",
            sources=("D5",),
        ),
        MigrationRule(
            parameter="asset_code",
            operation="retire",
            sources=("D5",),
        ),
    ]


def _planner(references=()):
    formulas = _formulas()
    formulas["pick_first_code"] = lambda ctx: next(iter(ctx.sources.values()))
    return CatalogPlanner(_specs(), formulas=formulas, references=references)


def _derived_sets():
    return (
        DerivedSet(
            name="intensity_set",
            parameter="carbon_intensity",
            members=("capacity_mw",),
            formula="carbon_from_capacity",
        ),
    )


def _all_rules():
    return _split_rules() + _merge_rules() + _rename_retire_rules()


def test_full_plan_publishes_and_keeps_old_scenario_untouched():
    old = _old_scenario()
    old_snapshot = copy.deepcopy(old)
    planner = _planner()

    plan = planner.plan(
        old,
        _all_rules(),
        new_name="new",
        new_device_station=_new_catalog(),
        derived_sets=_derived_sets(),
    )

    assert plan.is_valid
    new = plan.publish()
    assert new.name == "new"
    assert set(new.devices) == {"D1a", "D1b", "D6", "D7"}
    # 旧实例完全不被修改
    assert old.observations == old_snapshot.observations
    assert old.devices == old_snapshot.devices
    assert old.values == old_snapshot.values

    # 拆分：显式比例，不自动平分
    assert new.values[as_value_key("capacity_mw", "D1a", "T1", STATION)] == 60.0
    assert new.values[as_value_key("capacity_mw", "D1b", "T1", STATION)] == 40.0
    # 非可加量拆分使用显式 share
    assert new.values[as_value_key("efficiency", "D1a", "T1", STATION)] == 0.91
    # 合并：可加量求和，非可加量走公式而非求和
    assert new.values[as_value_key("capacity_mw", "D7", "T1", STATION)] == 75.0
    assert new.values[as_value_key("efficiency", "D7", "T1", STATION)] == pytest.approx(0.775)
    assert new.values[as_value_key("asset_code", "D7", "T1", STATION)] == "A2"
    # 更名与撤销
    assert new.values[as_value_key("capacity_mw", "D6", "T1", STATION)] == 10.0
    assert all(key[1] != "D5" for key in new.values)
    # 派生参数按最终成员重算
    assert new.values[
        as_value_key("carbon_intensity", "D1a", "T1", STATION)
    ] == pytest.approx(1000.0 / 60.0)


def test_every_new_value_traces_back_to_observations():
    planner = _planner()
    plan = planner.plan(
        _old_scenario(),
        _all_rules(),
        new_name="new",
        new_device_station=_new_catalog(),
        derived_sets=_derived_sets(),
    )
    obs_index = {o.key: o for o in _old_scenario().observations}

    # 可加量合并值回溯到两个源观测
    traced = plan.trace("capacity_mw", "D7", "T2", STATION)
    assert set(traced) == {
        as_key("D2", "T2", STATION),
        as_key("D3", "T2", STATION),
    }
    assert all(key in obs_index for key in traced)

    # 派生值沿成员继续回溯到原始观测
    derived_trace = plan.trace("carbon_intensity", "D6", "T1", STATION)
    assert derived_trace == (as_key("D4", "T1", STATION),)


def test_split_must_conserve_additive_quantity():
    bad_rules = [
        MigrationRule(
            parameter="capacity_mw",
            operation="split",
            sources=("D1",),
            targets=("D1a", "D1b"),
            allocations=(
                Allocation(target="D1a", fraction=0.6),
                Allocation(target="D1b", fraction=0.3),
            ),
        )
    ] + [rule for rule in _all_rules() if rule is not _all_rules()[0]]
    planner = _planner()
    plan = planner.plan(
        _old_scenario(),
        bad_rules,
        new_name="new",
        new_device_station=_new_catalog(),
    )
    assert not plan.is_valid
    assert any(c.code == "SPLIT_CONSERVATION" for c in plan.conflicts)
    with pytest.raises(MigrationConflict) as exc:
        plan.publish()
    assert all(isinstance(c, Conflict) for c in exc.value.conflicts)


def test_non_additive_split_without_explicit_share_conflicts():
    bad_rule = MigrationRule(
        parameter="efficiency",
        operation="split",
        sources=("D1",),
        targets=("D1a", "D1b"),
        allocations=(
            Allocation(target="D1a", fraction=0.5),
            Allocation(target="D1b", fraction=0.5),
        ),
    )
    rules = [bad_rule] + [
        rule for rule in _all_rules() if rule.parameter != "efficiency"
    ]
    planner = _planner()
    plan = planner.plan(
        _old_scenario(),
        rules,
        new_name="new",
        new_device_station=_new_catalog(),
    )
    codes = {c.code for c in plan.conflicts}
    assert "NON_ADDITIVE_SPLIT" in codes


def test_non_additive_merge_rejects_sum():
    planner = _planner()
    rules = _split_rules() + _rename_retire_rules() + [
        MigrationRule(
            parameter="capacity_mw",
            operation="merge",
            sources=("D2", "D3"),
            targets=("D7",),
            aggregate="sum",
        ),
        MigrationRule(
            parameter="efficiency",
            operation="merge",
            sources=("D2", "D3"),
            targets=("D7",),
            aggregate="sum",
        ),
        MigrationRule(
            parameter="asset_code",
            operation="merge",
            sources=("D2", "D3"),
            targets=("D7",),
            aggregate="formula",
            formula="pick_first_code",
        ),
    ]
    plan = planner.plan(
        _old_scenario(),
        rules,
        new_name="new",
        new_device_station=_new_catalog(),
    )
    assert any(c.code == "NON_ADDITIVE_SUM" for c in plan.conflicts)
    with pytest.raises(MigrationConflict):
        plan.publish()


def test_identity_merge_conflicts_on_distinct_values():
    planner = _planner()
    rules = _split_rules() + _rename_retire_rules() + [
        MigrationRule(
            parameter="capacity_mw",
            operation="merge",
            sources=("D2", "D3"),
            targets=("D7",),
            aggregate="sum",
        ),
        MigrationRule(
            parameter="efficiency",
            operation="merge",
            sources=("D2", "D3"),
            targets=("D7",),
            aggregate="identity",
        ),
        MigrationRule(
            parameter="asset_code",
            operation="merge",
            sources=("D2", "D3"),
            targets=("D7",),
            aggregate="formula",
            formula="pick_first_code",
        ),
    ]
    plan = planner.plan(
        _old_scenario(),
        rules,
        new_name="new",
        new_device_station=_new_catalog(),
    )
    assert any(c.code == "MERGE_IDENTITY_CONFLICT" for c in plan.conflicts)


def test_missing_rule_for_removed_device_is_conflict():
    rules = [
        rule for rule in _all_rules()
        if not (rule.operation == "retire" and rule.parameter == "efficiency")
    ]
    planner = _planner()
    plan = planner.plan(
        _old_scenario(),
        rules,
        new_name="new",
        new_device_station=_new_catalog(),
    )
    missing = [c for c in plan.conflicts if c.code == "MISSING_RULE"]
    assert missing and all(c.device == "D5" for c in missing)


def test_order_independence():
    planner = _planner()
    kwargs = dict(
        new_name="new",
        new_device_station=_new_catalog(),
        derived_sets=_derived_sets(),
    )
    first = planner.plan(_old_scenario(), _all_rules(), **kwargs)
    reversed_rules = list(reversed(_all_rules()))
    second = planner.plan(_old_scenario(), reversed_rules, **kwargs)

    assert first.is_valid and second.is_valid
    assert first.values.keys() == second.values.keys()
    for key in first.values:
        assert first.values[key].value == second.values[key].value


def test_derived_set_recomputed_on_final_members():
    planner = _planner()
    plan = planner.plan(
        _old_scenario(),
        _all_rules(),
        new_name="new",
        new_device_station=_new_catalog(),
        derived_sets=_derived_sets(),
    )
    new = plan.publish()
    # D7 T1 容量为 75 -> 派生强度 1000/75
    assert new.values[
        as_value_key("carbon_intensity", "D7", "T1", STATION)
    ] == pytest.approx(1000.0 / 75.0)
    provenance = plan.values[
        as_value_key("carbon_intensity", "D7", "T1", STATION)
    ].provenance
    assert provenance.rule == "derived:carbon_from_capacity"


def test_derived_parameter_cannot_be_migrated_directly():
    bad_rule = MigrationRule(
        parameter="carbon_intensity",
        operation="rename",
        sources=("D4",),
        targets=("D6",),
    )
    planner = _planner()
    plan = planner.plan(
        _old_scenario(),
        _all_rules() + [bad_rule],
        new_name="new",
        new_device_station=_new_catalog(),
        derived_sets=_derived_sets(),
    )
    assert any(c.code == "DERIVED_NO_MIGRATION" for c in plan.conflicts)


def test_cross_table_reference_validated_on_final_members():
    observations = _base_observations() + [
        _obs("group_code", "D1", "T1", "A1a"),
        _obs("group_code", "D2", "T1", "A2"),
    ]
    rules = _all_rules() + [
        MigrationRule(
            parameter="group_code",
            operation="rename",
            sources=("D1",),
            targets=("D1a",),
        ),
        MigrationRule(
            parameter="group_code",
            operation="rename",
            sources=("D2",),
            targets=("D7",),
        ),
    ]
    planner = _planner(
        references=(ReferenceRule("group_code", "asset_code"),)
    )
    plan = planner.plan(
        _old_scenario(observations),
        rules,
        new_name="new",
        new_device_station=_new_catalog(),
        derived_sets=_derived_sets(),
    )
    # A2/A3 都是最终 asset_code 取值，引用合法
    assert not any(c.code == "DANGLING_REFERENCE" for c in plan.conflicts)

    bad_observations = _base_observations() + [
        _obs("group_code", "D1", "T1", "GHOST"),
    ]
    bad_plan = planner.plan(
        _old_scenario(bad_observations),
        [
            rule for rule in rules
            if not (rule.parameter == "group_code" and rule.sources == ("D2",))
        ] + [
            MigrationRule(
                parameter="group_code",
                operation="rename",
                sources=("D1",),
                targets=("D1a",),
            )
        ],
        new_name="new",
        new_device_station=_new_catalog(),
        derived_sets=_derived_sets(),
    )
    assert any(c.code == "DANGLING_REFERENCE" for c in bad_plan.conflicts)


def test_bounds_violation_blocks_publication():
    observations = _base_observations() + [
        _obs("efficiency", "D2", "T2", 1.5),
    ]
    planner = _planner()
    plan = planner.plan(
        _old_scenario(observations),
        _all_rules(),
        new_name="new",
        new_device_station=_new_catalog(),
        derived_sets=_derived_sets(),
    )
    assert any(c.code == "OUT_OF_BOUNDS" for c in plan.conflicts)
    with pytest.raises(MigrationConflict):
        plan.publish()


def test_required_missing_conflicts_and_default_fills():
    observations = [
        obs for obs in _base_observations()
        if not (obs.device == "D4" and obs.parameter == "capacity_mw")
    ]
    planner = _planner()
    plan = planner.plan(
        _old_scenario(observations),
        _all_rules(),
        new_name="new",
        new_device_station=_new_catalog(),
        derived_sets=_derived_sets(),
    )
    assert any(
        c.code == "MISSING_VALUE" and c.device == "D6"
        for c in plan.conflicts
    )

    specs = _specs()
    specs["capacity_mw"] = ParameterSpec(
        name="capacity_mw",
        kind=ParameterKind.ADDITIVE,
        bounds=(0.0, None),
        required=True,
        default=0.0,
        has_default=True,
    )
    formulas = _formulas()
    formulas["pick_first_code"] = lambda ctx: next(iter(ctx.sources.values()))
    planner_with_default = CatalogPlanner(specs, formulas=formulas)
    rules = [
        MigrationRule(
            parameter="capacity_mw",
            operation="rename",
            sources=("D4",),
            targets=("D6",),
            allow_default=True,
        )
    ] + [
        rule for rule in _all_rules()
        if not (rule.parameter == "capacity_mw" and rule.sources == ("D4",))
    ]
    good_plan = planner_with_default.plan(
        _old_scenario(observations),
        rules,
        new_name="new",
        new_device_station=_new_catalog(),
        derived_sets=_derived_sets(),
    )
    assert good_plan.is_valid
    new = good_plan.publish()
    assert new.values[as_value_key("capacity_mw", "D6", "T1", STATION)] == 0.0
    provenance = good_plan.values[
        as_value_key("capacity_mw", "D6", "T1", STATION)
    ].provenance
    assert provenance.rule == "default"


def test_uniqueness_constraint_on_final_members():
    observations = [
        obs for obs in _base_observations()
        if not (obs.device == "D4" and obs.parameter == "asset_code")
    ]
    observations.append(_obs("asset_code", "D4", "T1", "A1a"))
    observations.append(_obs("asset_code", "D4", "T2", "A1a"))
    planner = _planner()
    plan = planner.plan(
        _old_scenario(observations),
        _all_rules(),
        new_name="new",
        new_device_station=_new_catalog(),
        derived_sets=_derived_sets(),
    )
    assert any(c.code == "DUPLICATE_VALUE" for c in plan.conflicts)


def test_station_mismatch_is_rejected():
    catalog = dict(_new_catalog())
    catalog["D1a"] = "S2"
    planner = _planner()
    plan = planner.plan(
        _old_scenario(),
        _all_rules(),
        new_name="new",
        new_device_station=catalog,
        derived_sets=_derived_sets(),
    )
    assert any(c.code == "STATION_MISMATCH" for c in plan.conflicts)


def test_weighted_merge_and_formula_provenance():
    planner = _planner()
    rules = _split_rules() + _rename_retire_rules() + [
        MigrationRule(
            parameter="capacity_mw",
            operation="merge",
            sources=("D2", "D3"),
            targets=("D7",),
            aggregate="sum",
        ),
        MigrationRule(
            parameter="efficiency",
            operation="merge",
            sources=("D2", "D3"),
            targets=("D7",),
            aggregate="weighted",
            weights=(0.75, 0.25),
        ),
        MigrationRule(
            parameter="asset_code",
            operation="merge",
            sources=("D2", "D3"),
            targets=("D7",),
            aggregate="formula",
            formula="pick_first_code",
        ),
    ]
    plan = planner.plan(
        _old_scenario(),
        rules,
        new_name="new",
        new_device_station=_new_catalog(),
        derived_sets=_derived_sets(),
    )
    assert plan.is_valid
    new = plan.publish()
    # 0.80*0.75 + 0.75*0.25 = 0.7875
    assert new.values[
        as_value_key("efficiency", "D7", "T1", STATION)
    ] == pytest.approx(0.7875)
    provenance = plan.values[
        as_value_key("efficiency", "D7", "T1", STATION)
    ].provenance
    assert provenance.rule == "merge:weighted"
    assert set(provenance.observations) == {
        as_key("D2", "T1", STATION),
        as_key("D3", "T1", STATION),
    }


def test_ambiguous_rules_and_unknown_devices_are_conflicts():
    duplicate = MigrationRule(
        parameter="capacity_mw",
        operation="split",
        sources=("D1",),
        targets=("D1a", "D1b"),
        allocations=(
            Allocation(target="D1a", value=70.0),
            Allocation(target="D1b", value=30.0),
        ),
    )
    planner = _planner()
    plan = planner.plan(
        _old_scenario(),
        _all_rules() + [duplicate],
        new_name="new",
        new_device_station=_new_catalog(),
        derived_sets=_derived_sets(),
    )
    assert any(c.code == "AMBIGUOUS_RULE" for c in plan.conflicts)

    ghost_rule = MigrationRule(
        parameter="capacity_mw",
        operation="rename",
        sources=("GHOST",),
        targets=("D6",),
    )
    ghost_plan = planner.plan(
        _old_scenario(),
        _all_rules() + [ghost_rule],
        new_name="new",
        new_device_station=_new_catalog(),
        derived_sets=_derived_sets(),
    )
    assert any(c.code == "UNKNOWN_SOURCE_DEVICE" for c in ghost_plan.conflicts)
