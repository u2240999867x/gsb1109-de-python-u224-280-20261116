"""Tests for rf_completion_bounds.

Covers: exact min/max with feasible witnesses, the no-fake-endpoints
guarantee, infeasible-vs-budget-exhausted separation, proven-bound
consistency, and witness validation.
"""

import itertools

import pytest

from rf_completion_bounds import (
    Infeasible,
    enumerate_completions,
    is_binary,
    leaf_set,
    parse_newick,
    proven_bounds,
    solve,
    split_distance,
    split_set,
    validate_completion,
)


def demo_spec(**overrides):
    spec = {
        "target_leaves": ["a", "b", "c", "d", "e"],
        "tree1": {"newick": "((a,b),(c,d));",
                  "allowed_attachments": [["c", "d"], "ROOT"]},
        "tree2": {"newick": "(a,(b,c,d));",
                  "allowed_attachments": [["b", "c", "d"]]},
    }
    spec.update(overrides)
    return spec


def brute_force_check(spec, result):
    """Independently recompute min/max over all completion pairs and compare
    with the solver's exact answer; also confirm witnesses attain them."""
    comps = {}
    for key in ("tree1", "tree2"):
        tree = parse_newick(spec[key]["newick"])
        c, exhausted, _ = enumerate_completions(
            tree, frozenset(spec["target_leaves"]),
            spec[key].get("allowed_attachments", []), 10**9)
        assert not exhausted
        comps[key] = list(c)
    distances = [
        split_distance(s1, s2)
        for s1, s2 in itertools.product(comps["tree1"], comps["tree2"])
    ]
    assert result["min_distance"] == min(distances)
    assert result["max_distance"] == max(distances)
    for prefix in ("min", "max"):
        w = result[prefix + "_witness"]
        assert w["distance"] == result[prefix + "_distance"]
        assert w["tree1_valid_completion"] and w["tree2_valid_completion"]
        d = split_distance(split_set(parse_newick(w["tree1_newick"])),
                           split_set(parse_newick(w["tree2_newick"])))
        assert d == result[prefix + "_distance"]


def test_demo_exact_min_max():
    result = solve(demo_spec())
    assert result["status"] == "exact"
    assert result["min_distance"] == 4
    assert result["max_distance"] == 6
    brute_force_check(demo_spec(), result)


def test_identical_unresolved_trees_zero_min():
    spec = {
        "target_leaves": ["a", "b", "c", "d"],
        "tree1": {"newick": "((a,b,c),d);", "allowed_attachments": []},
        "tree2": {"newick": "((a,b,c),d);", "allowed_attachments": []},
    }
    result = solve(spec)
    assert result["status"] == "exact"
    assert result["min_distance"] == 0
    # polytomy (a,b,c) has 3 binary resolutions; max pairs disagreeing splits
    assert result["max_distance"] == 2
    brute_force_check(spec, result)


def test_unknown_splits_not_scored_as_zero_support():
    # A raw comparison of the unresolved trees would give distance 0 here
    # (no non-trivial splits differ).  Over completions the distance is
    # strictly positive for every pair that resolves the two polytomies
    # differently, and 0 only when they resolve identically.
    spec = {
        "target_leaves": ["a", "b", "c", "d", "e", "f"],
        "tree1": {"newick": "((a,b,c),(d,e,f));", "allowed_attachments": []},
        "tree2": {"newick": "((a,b,c),(d,e,f));", "allowed_attachments": []},
    }
    result = solve(spec)
    assert result["status"] == "exact"
    assert result["min_distance"] == 0
    assert result["max_distance"] == 4  # both 2-split disagreements


def test_infeasible_extra_leaf():
    spec = demo_spec()
    spec["tree1"] = {"newick": "((a,b),(c,z));", "allowed_attachments": []}
    result = solve(spec)
    assert result["status"] == "infeasible"
    assert any("z" in r for r in result["reasons"])


def test_infeasible_missing_leaf_without_position():
    spec = demo_spec()
    spec["tree1"] = {"newick": "((a,b),(c,d));", "allowed_attachments": []}
    result = solve(spec)
    assert result["status"] == "infeasible"
    assert any("e" in r for r in result["reasons"])


def test_invalid_input_unknown_position():
    spec = demo_spec()
    spec["tree1"]["allowed_attachments"] = [["x", "y"]]
    with pytest.raises(Exception) as excinfo:
        solve(spec)
    assert "matches no node" in str(excinfo.value)


def test_budget_exhausted_keeps_bounds_and_witnesses():
    spec = {
        "target_leaves": list("abcdefgh"),
        "tree1": {"newick": "(a,b,c,d,e,f,g,h);", "allowed_attachments": []},
        "tree2": {"newick": "(a,b,c,d,e,f,g,h);", "allowed_attachments": []},
        "budget": {"max_completions_per_tree": 5,
                   "max_pair_evaluations": 1000},
    }
    result = solve(spec)
    assert result["status"] == "budget_exhausted"
    mn, mx = result["min_distance"], result["max_distance"]
    # proven bounds bracket the witnessed values
    assert mn["proven_lower_bound"] <= mn["best_witnessed"]
    assert mx["best_witnessed"] <= mx["proven_upper_bound"]
    # witnesses are real, feasible completions attaining the witnessed values
    for entry in (mn["witness"], mx["witness"]):
        assert entry["tree1_valid_completion"]
        assert entry["tree2_valid_completion"]
        d = split_distance(split_set(parse_newick(entry["tree1_newick"])),
                           split_set(parse_newick(entry["tree2_newick"])))
        assert d == entry["distance"]
    # for identical star trees the true min is 0 and must be found
    assert mn["best_witnessed"] == 0
    # upper bound for 8 leaves with no shared forced splits is 2*(8-2)
    assert mx["proven_upper_bound"] == 12


def test_pair_budget_exhaustion_only():
    spec = demo_spec(budget={"max_completions_per_tree": 100,
                             "max_pair_evaluations": 2})
    result = solve(spec)
    assert result["status"] == "budget_exhausted"
    assert result["pairs_evaluated"] == 2
    mn, mx = result["min_distance"], result["max_distance"]
    assert mn["proven_lower_bound"] <= mn["best_witnessed"]
    assert mx["best_witnessed"] <= mx["proven_upper_bound"]


def test_proven_bounds_bracket_true_range():
    spec = demo_spec()
    t1 = parse_newick(spec["tree1"]["newick"])
    t2 = parse_newick(spec["tree2"]["newick"])
    from rf_completion_bounds import _resolve_positions
    p1 = _resolve_positions(t1, spec["tree1"]["allowed_attachments"])
    p2 = _resolve_positions(t2, spec["tree2"]["allowed_attachments"])
    lb, ub = proven_bounds(t1, p1, t2, p2, 5)
    result = solve(spec)
    assert lb <= result["min_distance"]
    assert result["max_distance"] <= ub
    # here the upper bound is even tight
    assert ub == 6


def test_witness_validation_rejects_illegal_completion():
    spec = demo_spec()
    orig = parse_newick(spec["tree1"]["newick"])
    target = frozenset(spec["target_leaves"])
    positions = spec["tree1"]["allowed_attachments"]
    # legal: e attached at the (c,d) edge
    good = parse_newick("((a,b),((c,d),e));")
    assert validate_completion(good, orig, target, positions)
    # illegal: e attached inside the (a,b) clade (not an allowed position)
    bad = parse_newick("(((a,b),e),(c,d));")
    assert not validate_completion(bad, orig, target, positions)
    # illegal: does not refine the data tree
    bad2 = parse_newick("((a,c),(b,(d,e)));")
    assert not validate_completion(bad2, orig, target, positions)
    # illegal: not binary
    bad3 = parse_newick("((a,b),(c,d,e));")
    assert not validate_completion(bad3, orig, target, positions)


def test_completions_are_binary_and_on_target():
    spec = demo_spec()
    tree = parse_newick(spec["tree1"]["newick"])
    comps, exhausted, _ = enumerate_completions(
        tree, frozenset(spec["target_leaves"]),
        spec["tree1"]["allowed_attachments"], 1000)
    assert not exhausted
    assert len(comps) == 2  # e at ROOT, or e at the (c,d) edge
    for sp, nk in comps.items():
        t = parse_newick(nk)
        assert is_binary(t)
        assert leaf_set(t) == frozenset(spec["target_leaves"])
