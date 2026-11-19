import pytest

from scenario_enumeration import (
    CalibrationChoice,
    Observation,
    build_models,
    enumerate_scenarios,
    make_pyomo_factory,
)

CAL_A_T1 = CalibrationChoice('instrA', '2026-Q1', 'calA-q1')
CAL_A_T2 = CalibrationChoice('instrA', '2026-Q2', 'calA-q2')
CAL_B_T1 = CalibrationChoice('instrB', '2026-Q1', 'calB-q1')


def table_shared_instrument():
    # x and y are both measured by instrA; each has candidates from two
    # calibration periods.
    return {
        'x': [
            Observation('x1', 'x', 1.0, CAL_A_T1),
            Observation('x2', 'x', 2.0, CAL_A_T2),
        ],
        'y': [
            Observation('y1', 'y', 10.0, CAL_A_T1),
            Observation('y2', 'y', 20.0, CAL_A_T2),
        ],
    }


def test_calibration_periods_never_mixed():
    res = enumerate_scenarios(table_shared_instrument())
    assert res.complete
    # 4 raw combos, but cross-period mixes (x1,y2) and (x2,y1) are pruned.
    assert len(res.scenarios) == 2
    for sc in res.scenarios:
        periods = {c.period for c in sc.calibrations.values()}
        assert len(periods) == 1
    combos = {sc.evidence_key for sc in res.scenarios}
    assert combos == {
        (('x', 'x1'), ('y', 'y1')),
        (('x', 'x2'), ('y', 'y2')),
    }


def test_joint_constraint_prunes():
    table = table_shared_instrument()

    def small_sum(evidence):
        vals = [o.value for o in evidence.values()]
        return sum(vals) <= 15

    res = enumerate_scenarios(table, constraints=[small_sum])
    assert [sc.values for sc in res.scenarios] == [{'x': 1.0, 'y': 10.0}]
    assert res.pruned > 0


def test_provenance_traceable_per_parameter():
    res = enumerate_scenarios(table_shared_instrument())
    sc = res.scenarios[0]
    for param, obs in sc.evidence.items():
        assert obs.parameter == param
        assert sc.values[param] == obs.value
        assert sc.calibrations[obs.calibration.instrument] is obs.calibration


def test_equivalent_scenarios_coexist_with_own_evidence():
    # Two different observations (different calibration) give the same value.
    table = {
        'x': [
            Observation('x-q1', 'x', 5.0, CAL_A_T1),
            Observation('x-q2', 'x', 5.0, CAL_A_T2),
        ],
        'z': [Observation('z1', 'z', 1.0)],  # no calibration dependency
    }
    res = enumerate_scenarios(table)
    assert len(res.scenarios) == 2
    value_keys = [sc.value_key for sc in res.scenarios]
    assert value_keys[0] == value_keys[1]  # same parameter dict
    evidence_keys = [sc.evidence_key for sc in res.scenarios]
    assert len(set(evidence_keys)) == 2  # distinct evidence, both kept
    groups = res.equivalent_groups()
    assert len(groups) == 1 and len(groups[value_keys[0]]) == 2


def test_budget_reports_incomplete_and_unexplored():
    table = {
        'x': [Observation(f'x{i}', 'x', float(i)) for i in range(3)],
        'y': [Observation(f'y{i}', 'y', float(i)) for i in range(3)],
    }
    res = enumerate_scenarios(table, max_scenarios=2)
    assert not res.complete
    assert res.truncated
    assert len(res.scenarios) == 2  # confirmed scenarios delivered
    assert len(res.unexplored) > 0  # unexplored range reported, not hidden
    for branch in res.unexplored:
        assert set(branch.assigned) | set(branch.remaining) == {'x', 'y'}
    # Full enumeration for cross-check: 9 legal scenarios exist.
    full = enumerate_scenarios(table)
    assert full.complete and len(full.scenarios) == 9


def test_misfiled_observation_rejected():
    with pytest.raises(ValueError):
        enumerate_scenarios({'x': [Observation('o1', 'y', 1.0)]})


def test_bad_scenario_does_not_pollute_other_models():
    table = {'x': [Observation(f'x{i}', 'x', float(i)) for i in range(3)]}
    scenarios = enumerate_scenarios(table).scenarios

    def factory(sc):
        if sc.values['x'] == 1.0:
            raise RuntimeError('bad scenario')
        return {'x': sc.values['x']}

    result = build_models(scenarios, factory)
    assert len(result.failures) == 1
    assert result.failures[0].scenario.values['x'] == 1.0
    assert isinstance(result.failures[0].error, RuntimeError)
    assert result.models == [{'x': 0.0}, {'x': 2.0}]


def test_pyomo_factory_builds_isolated_instances_with_provenance():
    pyomo = pytest.importorskip('pyomo.environ')
    from pyomo.environ import ConcreteModel, Param

    template = ConcreteModel()
    template.x = Param(mutable=True, initialize=0.0)
    template.z = Param(mutable=True, initialize=0.0)

    table = {
        'x': [
            Observation('x-q1', 'x', 5.0, CAL_A_T1),
            Observation('x-q2', 'x', 5.0, CAL_A_T2),
        ],
        'z': [Observation('z1', 'z', 1.0)],
    }
    scenarios = enumerate_scenarios(table).scenarios
    result = build_models(scenarios, make_pyomo_factory(template))

    assert not result.failures
    m1, m2 = result.models
    assert m1 is not m2 and m1.x is not m2.x
    assert pyomo.value(m1.x) == 5.0 and pyomo.value(m2.z) == 1.0
    # Provenance attached to each instance, distinct per evidence combo.
    assert m1._scenario_evidence['x'].obs_id == 'x-q1'
    assert m2._scenario_evidence['x'].obs_id == 'x-q2'
    assert (
        m1._scenario_calibrations['instrA'].period
        != m2._scenario_calibrations['instrA'].period
    )
    # Template itself untouched.
    assert pyomo.value(template.x) == 0.0


def test_pyomo_factory_failure_isolated_per_instance():
    from pyomo.environ import ConcreteModel, Param

    template = ConcreteModel()
    template.x = Param(mutable=True, initialize=0.0)

    table = {
        'x': [Observation('x1', 'x', 1.0)],
        'missing': [Observation('m1', 'missing', 9.0)],
    }
    scenarios = enumerate_scenarios(table).scenarios
    result = build_models(scenarios, make_pyomo_factory(template))
    assert len(result.failures) == 1
    assert result.models == []

    # A good scenario built afterwards is unaffected by the earlier failure.
    good = enumerate_scenarios({'x': [Observation('x2', 'x', 2.0)]}).scenarios
    ok = build_models(good, make_pyomo_factory(template))
    assert not ok.failures and len(ok.models) == 1
