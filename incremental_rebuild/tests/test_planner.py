from incremental_rebuild import (
    Action,
    ComponentDecl,
    ImpactKind,
    LoadResult,
    plan_rebuild,
)

# Component graph used across the planner tests:
#   I      index domain taken from the keys of table 'plants'
#   cap    Param over I, values read from table 'capacity'
#   cost   Param over I, values read from table 'cost', default for gaps
#   I_hi   Set built by a rule filtering table 'plants' by value
#   con    Constraint built over I, referencing cap
DECLS = [
    ComponentDecl("I", index_from="plants"),
    ComponentDecl("cap", value_reads={"capacity"}, upstream={"I"}),
    ComponentDecl("cost", value_reads={"cost"}, upstream={"I"}, has_default=True),
    ComponentDecl("I_hi", index_reads={"plants"}, upstream={"I"}),
    ComponentDecl("con", upstream={"I", "cap", "I_hi"}),
]

BASE = LoadResult.from_rows(
    "plants", [("p1", 80.0, "obs-p1"), ("p2", 30.0, "obs-p2")]
).updated(
    LoadResult.from_rows(
        "capacity", [("p1", 10.0, "obs-c1"), ("p2", 20.0, "obs-c2")]
    )
).updated(
    LoadResult.from_rows("cost", [("p1", 1.5, "obs-k1")])
)


def test_no_changes_is_noop():
    plan = plan_rebuild(BASE, BASE, DECLS)
    assert plan.is_noop
    assert set(plan.unaffected) == {"I", "cap", "cost", "I_hi", "con"}


def test_value_only_change_revalues_only_the_reader():
    new = BASE.updated(
        LoadResult.from_rows(
            "capacity", [("p1", 11.0, "obs-c1-fix"), ("p2", 20.0, "obs-c2")]
        )
    )
    plan = plan_rebuild(BASE, new, DECLS)
    assert plan.actions == {"cap": Action.REVALUE}
    assert plan.revalue == {"cap"}
    # A revalued upstream does not propagate: 'con' holds a symbolic
    # reference to 'cap' and stays untouched.
    assert set(plan.unaffected) == {"I", "cost", "I_hi", "con"}
    (impact,) = plan.impacts["cap"]
    assert impact.kind is ImpactKind.VALUE_ONLY
    assert impact.sources == ("obs-c1", "obs-c1-fix")


def test_membership_without_default_reconstructs_and_propagates():
    new = BASE.updated(
        LoadResult.from_rows("capacity", [("p1", 10.0, "obs-c1")])
    )
    plan = plan_rebuild(BASE, new, DECLS)
    assert plan.actions["cap"] is Action.RECONSTRUCT
    assert plan.impacts["cap"][0].kind is ImpactKind.MEMBERSHIP
    # Downstream of a reconstructed component must be rebuilt too.
    assert plan.actions["con"] is Action.RECONSTRUCT
    assert plan.revalue == frozenset()
    assert set(plan.unaffected) == {"I", "cost", "I_hi"}


def test_membership_with_default_is_default_triggering_revalue():
    new = BASE.updated(LoadResult.from_rows("cost", []))
    plan = plan_rebuild(BASE, new, DECLS)
    assert plan.actions == {"cost": Action.REVALUE}
    (impact,) = plan.impacts["cost"]
    assert impact.kind is ImpactKind.DEFAULT_TRIGGERING
    assert impact.sources == ("obs-k1",)


def test_index_domain_membership_is_construction_scope():
    new = BASE.updated(
        LoadResult.from_rows(
            "plants",
            [("p1", 80.0, "obs-p1"), ("p2", 30.0, "obs-p2"), ("p3", 55.0, "obs-p3")],
        )
    )
    plan = plan_rebuild(BASE, new, DECLS)
    # The domain of I changes: reconstruct, never revalue in place.
    assert plan.actions["I"] is Action.RECONSTRUCT
    assert plan.impacts["I"][0].kind is ImpactKind.CONSTRUCTION_SCOPE
    # 'plants' also feeds the filtered set's index selection.
    assert plan.actions["I_hi"] is Action.RECONSTRUCT
    assert plan.impacts["I_hi"][0].kind is ImpactKind.CONSTRUCTION_SCOPE
    # Everything downstream of I is reconstructed.
    assert plan.reconstruct == {"I", "cap", "cost", "I_hi", "con"}
    assert plan.unaffected == {}


def test_value_change_in_index_selection_table_forces_reconstruct():
    # p2 crosses the 50 threshold used by the I_hi construction rule:
    # a value-only diff that still changes the constructed domain.
    new = BASE.updated(
        LoadResult.from_rows(
            "plants", [("p1", 80.0, "obs-p1"), ("p2", 60.0, "obs-p2-fix")]
        )
    )
    plan = plan_rebuild(BASE, new, DECLS)
    assert plan.revalue == frozenset()
    assert plan.actions["I_hi"] is Action.RECONSTRUCT
    assert plan.impacts["I_hi"][0].kind is ImpactKind.CONSTRUCTION_SCOPE
    assert plan.actions["con"] is Action.RECONSTRUCT
    # The domain of I is unchanged and its values are not read: untouched.
    assert "I" in plan.unaffected
    assert set(plan.unaffected) == {"I", "cap", "cost"}
    assert plan.affected_sources() == ("obs-p2", "obs-p2-fix")


def test_incomplete_declaration_keeps_full_verification_scope():
    decls = DECLS + [ComponentDecl("mystery", complete=False)]
    new = BASE.updated(
        LoadResult.from_rows(
            "capacity", [("p1", 10.0, "obs-c1"), ("p2", 21.0, "obs-c2-fix")]
        )
    )
    plan = plan_rebuild(BASE, new, decls)
    assert plan.actions["mystery"] is Action.VERIFY_FULL
    assert "full verification scope" in plan.explain("mystery")[-1]
    # Unrelated but complete components are still proven unaffected.
    assert set(plan.unaffected) == {"I", "cost", "I_hi", "con"}


def test_incomplete_declaration_without_any_change_is_noop():
    decls = DECLS + [ComponentDecl("mystery", complete=False)]
    plan = plan_rebuild(BASE, BASE, decls)
    assert plan.is_noop


def test_downstream_of_verify_full_is_reconstructed():
    decls = DECLS + [
        ComponentDecl("mystery", complete=False),
        ComponentDecl("user_of_mystery", upstream={"mystery"}),
    ]
    new = BASE.updated(
        LoadResult.from_rows(
            "capacity", [("p1", 10.0, "obs-c1"), ("p2", 21.0, "obs-c2-fix")]
        )
    )
    plan = plan_rebuild(BASE, new, decls)
    assert plan.actions["mystery"] is Action.VERIFY_FULL
    assert plan.actions["user_of_mystery"] is Action.RECONSTRUCT


def test_revalued_index_upstream_forces_reconstruct():
    decls = DECLS + [
        ComponentDecl("big_caps", index_upstream={"cap"}, upstream={"I"}),
    ]
    new = BASE.updated(
        LoadResult.from_rows(
            "capacity", [("p1", 10.0, "obs-c1"), ("p2", 21.0, "obs-c2-fix")]
        )
    )
    plan = plan_rebuild(BASE, new, decls)
    assert plan.actions["cap"] is Action.REVALUE
    # 'cap' participates in the index selection of 'big_caps': a new
    # value can change the constructed domain, so re-assigning values on
    # the old instance is not enough.
    assert plan.actions["big_caps"] is Action.RECONSTRUCT


def test_minimality_and_provenance_of_the_plan():
    new = BASE.updated(
        LoadResult.from_rows(
            "capacity", [("p1", 10.0, "obs-c1"), ("p2", 21.0, "obs-c2-fix")]
        )
    )
    plan = plan_rebuild(BASE, new, DECLS)
    assert plan.actions == {"cap": Action.REVALUE}
    assert plan.affected_sources() == ("obs-c2", "obs-c2-fix")
    for name in ("I", "cost", "I_hi", "con"):
        assert "identical" in plan.unaffected[name]
