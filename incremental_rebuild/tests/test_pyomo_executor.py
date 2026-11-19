from pyomo.environ import ConcreteModel, Constraint, NonNegativeReals, Param, Set, Var, value

from incremental_rebuild import Action, ComponentDecl, LoadResult, plan_rebuild
from incremental_rebuild.executor import ParamFromTable, apply_plan

DECLS = [
    ComponentDecl("I", index_from="plants"),
    ComponentDecl("cap", value_reads={"capacity"}, upstream={"I"}),
    ComponentDecl("cost", value_reads={"cost"}, upstream={"I"}, has_default=True),
    ComponentDecl("I_hi", index_reads={"plants"}, upstream={"I"}),
    ComponentDecl("x", upstream={"I"}),
    ComponentDecl("cap_con", upstream={"I", "cap"}),
]

COST_DEFAULT = 9.0
HI_THRESHOLD = 50.0


def _plant_keys(data):
    return sorted(k[0] for k in data.table("plants"))


def _hi_keys(data):
    return sorted(
        k[0] for k, cell in data.table("plants").items() if cell.value >= HI_THRESHOLD
    )


class SetFromPlants:
    def construct(self, model, name, data):
        if hasattr(model, name):
            model.del_component(name)
        setattr(model, name, Set(initialize=_plant_keys(data)))

    def revalue(self, model, name, data):
        raise AssertionError("a Set has no values to reassign in place")


class FilteredHiSet:
    """Set whose construction rule selects indices by table values."""

    def construct(self, model, name, data):
        if hasattr(model, name):
            model.del_component(name)
        setattr(model, name, Set(initialize=_hi_keys(data)))

    def revalue(self, model, name, data):
        raise AssertionError("index selection must be reconstructed, not revalued")


class VarOverI:
    def construct(self, model, name, data):
        if hasattr(model, name):
            model.del_component(name)
        setattr(model, name, Var(model.I, within=NonNegativeReals))

    def revalue(self, model, name, data):
        raise AssertionError("variables are not data-revalued here")


class CapConstraint:
    def construct(self, model, name, data):
        if hasattr(model, name):
            model.del_component(name)

        def rule(m, i):
            return m.x[i] <= m.cap[i]

        setattr(model, name, Constraint(model.I, rule=rule))

    def revalue(self, model, name, data):
        raise AssertionError("constraints are reconstructed, never revalued")


BUILDERS = {
    "I": SetFromPlants(),
    "cap": ParamFromTable("capacity", "I"),
    "cost": ParamFromTable("cost", "I", default=COST_DEFAULT),
    "I_hi": FilteredHiSet(),
    "x": VarOverI(),
    "cap_con": CapConstraint(),
}


def build_fresh(data):
    model = ConcreteModel()
    for name in ("I", "cap", "cost", "I_hi", "x", "cap_con"):
        BUILDERS[name].construct(model, name, data)
    return model


def snapshot(model):
    snap = {}
    for comp in model.component_objects(Set):
        snap[comp.name] = ("set", sorted(str(k) for k in comp))
    for comp in model.component_objects(Param):
        snap[comp.name] = (
            "param",
            {str(k): value(comp[k]) for k in comp.index_set()},
        )
    for comp in model.component_objects(Var):
        snap[comp.name] = ("var", sorted(str(k) for k in comp.index_set()))
    for comp in model.component_objects(Constraint):
        snap[comp.name] = (
            "con",
            {str(k): str(comp[k].expr) for k in comp.index_set()},
        )
    return snap


BASE = LoadResult.from_rows(
    "plants", [("p1", 80.0, "obs-p1"), ("p2", 30.0, "obs-p2")]
).updated(
    LoadResult.from_rows(
        "capacity", [("p1", 10.0, "obs-c1"), ("p2", 20.0, "obs-c2")]
    )
).updated(
    LoadResult.from_rows("cost", [("p1", 1.5, "obs-k1")])
)


def test_row_reorder_leaves_model_untouched():
    model = build_fresh(BASE)
    before = {name: getattr(model, name) for name in BUILDERS}
    reordered = LoadResult.from_rows(
        "plants", [("p2", 30.0, "obs-p2"), ("p1", 80.0, "obs-p1")]
    ).updated(
        LoadResult.from_rows(
            "capacity", [("p2", 20.0, "obs-c2"), ("p1", 10.0, "obs-c1")]
        )
    ).updated(
        LoadResult.from_rows("cost", [("p1", 1.5, "obs-k1")])
    )
    plan = plan_rebuild(BASE, reordered, DECLS)
    assert plan.is_noop
    apply_plan(model, plan, DECLS, BUILDERS, reordered)
    for name, comp in before.items():
        assert getattr(model, name) is comp
    assert snapshot(model) == snapshot(build_fresh(reordered))


def test_value_only_change_revalues_in_place():
    model = build_fresh(BASE)
    new = BASE.updated(
        LoadResult.from_rows(
            "capacity", [("p1", 11.0, "obs-c1-fix"), ("p2", 20.0, "obs-c2")]
        )
    )
    plan = plan_rebuild(BASE, new, DECLS)
    assert plan.actions == {"cap": Action.REVALUE}
    keep = {name: getattr(model, name) for name in ("I", "cost", "I_hi", "x", "cap_con")}
    apply_plan(model, plan, DECLS, BUILDERS, new)
    assert value(model.cap["p1"]) == 11.0
    for name, comp in keep.items():
        assert getattr(model, name) is comp
    assert snapshot(model) == snapshot(build_fresh(new))


def test_default_triggering_change_revalues_with_default_fallback():
    model = build_fresh(BASE)
    assert value(model.cost["p1"]) == 1.5
    new = BASE.updated(LoadResult.from_rows("cost", []))
    plan = plan_rebuild(BASE, new, DECLS)
    assert plan.actions == {"cost": Action.REVALUE}
    apply_plan(model, plan, DECLS, BUILDERS, new)
    assert value(model.cost["p1"]) == COST_DEFAULT
    assert snapshot(model) == snapshot(build_fresh(new))


def test_membership_change_reconstructs_domain_and_dependents():
    model = build_fresh(BASE)
    new = BASE.updated(
        LoadResult.from_rows(
            "plants",
            [("p1", 80.0, "obs-p1"), ("p2", 30.0, "obs-p2"), ("p3", 55.0, "obs-p3")],
        )
    ).updated(
        LoadResult.from_rows(
            "capacity",
            [
                ("p1", 10.0, "obs-c1"),
                ("p2", 20.0, "obs-c2"),
                ("p3", 30.0, "obs-c3"),
            ],
        )
    )
    plan = plan_rebuild(BASE, new, DECLS)
    assert plan.reconstruct == {"I", "cap", "cost", "I_hi", "x", "cap_con"}
    apply_plan(model, plan, DECLS, BUILDERS, new)
    assert snapshot(model) == snapshot(build_fresh(new))
    assert sorted(str(k) for k in model.I) == ["p1", "p2", "p3"]
    assert value(model.cost["p3"]) == COST_DEFAULT


def test_index_selection_change_reconstructs_filtered_set_only():
    model = build_fresh(BASE)
    assert sorted(str(k) for k in model.I_hi) == ["p1"]
    new = BASE.updated(
        LoadResult.from_rows(
            "plants", [("p1", 80.0, "obs-p1"), ("p2", 60.0, "obs-p2-fix")]
        )
    )
    plan = plan_rebuild(BASE, new, DECLS)
    # 'plants' participates in index selection: the old instance must not
    # be revalued in place, the filtered set is reconstructed.
    assert plan.actions == {"I_hi": Action.RECONSTRUCT}
    keep = {name: getattr(model, name) for name in ("I", "cap", "cost", "x", "cap_con")}
    apply_plan(model, plan, DECLS, BUILDERS, new)
    assert sorted(str(k) for k in model.I_hi) == ["p1", "p2"]
    for name, comp in keep.items():
        assert getattr(model, name) is comp
    assert snapshot(model) == snapshot(build_fresh(new))


def test_plan_provenance_points_back_to_observations():
    new = BASE.updated(
        LoadResult.from_rows(
            "plants", [("p1", 80.0, "obs-p1"), ("p2", 60.0, "obs-p2-fix")]
        )
    )
    plan = plan_rebuild(BASE, new, DECLS)
    (impact,) = plan.impacts["I_hi"]
    assert impact.sources == ("obs-p2", "obs-p2-fix")
    assert plan.affected_sources() == ("obs-p2", "obs-p2-fix")
