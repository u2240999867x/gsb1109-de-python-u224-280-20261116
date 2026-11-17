# Copyright (c) 2018 The Pooch Developers.
# Distributed under the terms of the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause
#
# This code is part of the Fatiando a Terra project (https://www.fatiando.org)
#
"""
Test cache space reclamation (eviction planning and execution).
"""

import os
from pathlib import Path

import pytest

from ..eviction import (
    CacheMaterial,
    EvictionError,
    execute_eviction,
    plan_eviction,
    revalidate_eviction,
)


def make_file(root, name, size, content=None):
    "Create a file of the given size and return its path."
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content if content is not None else b"x" * size
    path.write_bytes(data)
    return path



def material(name, size, cost=1.0, **kwargs):
    "Build a material mapping with the given fields."
    data = {"name": name, "size": size, "refetch_cost": cost, "paths": (name,)}
    data.update(kwargs)
    return data


def test_plan_cheapest_option(tmp_path):
    "The planner should pick the cheapest way to reach the target."
    make_file(tmp_path, "small", 10)
    make_file(tmp_path, "large", 1000)
    materials = [
        material("small", 10, cost=1.0),
        material("large", 1000, cost=5.0),
    ]
    plan = plan_eviction(materials, 500, cache_root=tmp_path)
    assert plan.names == ["large"]
    assert plan.released == 1000
    assert plan.cost == 5.0
    assert plan.target_met


def test_plan_prefers_least_cost_across_materials(tmp_path):
    "Several cheap materials should beat one expensive one."
    for name, size, cost in [("a", 100, 1.0), ("b", 200, 1.5), ("c", 400, 9.0)]:
        make_file(tmp_path, name, size)
        material(name, size, cost=cost)
    materials = [
        material("a", 100, cost=1.0),
        material("b", 200, cost=1.5),
        material("c", 400, cost=9.0),
    ]
    plan = plan_eviction(materials, 300, cache_root=tmp_path)
    assert plan.names == ["a", "b"]
    assert plan.released == 300
    assert plan.cost == pytest.approx(2.5)


def test_plan_tie_prefers_smaller_overshoot(tmp_path):
    "On equal cost, the plan that releases fewer extra bytes wins."
    for name, size in [("a", 100), ("b", 101)]:
        make_file(tmp_path, name, size)
    materials = [
        material("a", 100, cost=2.0),
        material("b", 101, cost=2.0),
    ]
    plan = plan_eviction(materials, 50, cache_root=tmp_path)
    assert plan.names == ["a"]
    assert plan.released == 100


def test_plan_logical_size_is_not_released_size(tmp_path):
    "Hard links count once and only when every link is removed."
    make_file(tmp_path, "original", 1000)
    os.link(tmp_path / "original", tmp_path / "alias")
    materials = [
        material("original", 1000, cost=1.0),
        material("alias", 1000, cost=1.0, paths=("alias",)),
    ]
    plan = plan_eviction(materials, 1000, cache_root=tmp_path)
    assert set(plan.names) == {"original", "alias"}
    assert plan.released_logical == 2000
    assert plan.released == 1000
    assert len(plan.storage) == 1


def test_plan_partial_hardlink_releases_nothing(tmp_path):
    "A file with another link outside the selection cannot be reclaimed."
    make_file(tmp_path, "a", 100)
    make_file(tmp_path, "b", 100)
    os.link(tmp_path / "a", tmp_path / "outside")
    materials = [
        material("a", 100, cost=1.0),
        material("b", 100, cost=5.0),
    ]
    plan = plan_eviction(materials, 1, cache_root=tmp_path)
    assert plan.names == ["b"]
    assert plan.released == 100


def test_plan_unowned_file_never_released(tmp_path):
    "Files not owned by the cache must not be proposed or counted."
    make_file(tmp_path, "mine", 100)
    make_file(tmp_path, "theirs", 500)
    materials = [
        material("mine", 100, cost=1.0),
        material("theirs", 500, cost=0.1, owner=False),
    ]
    plan = plan_eviction(materials, 100, cache_root=tmp_path)
    assert plan.names == ["mine"]
    assert plan.released == 100
    assert plan.max_releasable == 100


def test_plan_infeasible_reports_deficit(tmp_path):
    "An impossible request reports the missing bytes instead of raising."
    make_file(tmp_path, "a", 100)
    plan = plan_eviction([material("a", 100)], 250, cache_root=tmp_path)
    assert not plan.feasible
    assert not plan.target_met
    assert plan.released == 0
    assert plan.deficit == 150
    assert plan.max_releasable == 100
    assert "100 cache-owned bytes" in plan.reason


def test_plan_deficit_with_leases_and_unowned(tmp_path):
    "Deficit accounts for leased and unowned storage."
    make_file(tmp_path, "a", 100)
    make_file(tmp_path, "b", 100)
    make_file(tmp_path, "c", 100)
    materials = [
        material("a", 100),
        material("b", 100, leased=True),
        material("c", 100, owner=False),
    ]
    plan = plan_eviction(materials, 500, cache_root=tmp_path)
    assert plan.deficit == 400
    assert plan.max_releasable == 100
    assert plan.retained["b"] == "leased"


def test_dependency_original_forces_derived(tmp_path):
    "Evicting an original must evict everything derived from it."
    make_file(tmp_path, "archive.zip", 1000)
    make_file(tmp_path, "archive.zip.unzip/data", 4000)
    materials = [
        material("archive.zip", 1000, cost=1.0, derived=("data",)),
        material(
            "data",
            4000,
            cost=0.0,
            originals=("archive.zip",),
            paths=("archive.zip.unzip/data",),
        ),
    ]
    plan = plan_eviction(materials, 5000, cache_root=tmp_path)
    assert set(plan.names) == {"archive.zip", "data"}
    assert plan.released == 5000


def test_dependency_derived_can_go_alone(tmp_path):
    "A derived artifact may be evicted while keeping its original."
    make_file(tmp_path, "archive.zip", 1000)
    make_file(tmp_path, "data", 4000)
    materials = [
        material("archive.zip", 1000, cost=1.0, derived=("data",)),
        material("data", 4000, cost=0.0, originals=("archive.zip",)),
    ]
    plan = plan_eviction(materials, 4000, cache_root=tmp_path)
    assert plan.names == ["data"]
    assert plan.released == 4000
    assert plan.cost == 0.0


def test_dependency_both_directions_are_merged(tmp_path):
    "Declaring the link on either side produces the same graph."
    make_file(tmp_path, "o", 100)
    make_file(tmp_path, "d", 100)
    from_original = [
        material("o", 100, derived=("d",)),
        material("d", 100),
    ]
    from_derived = [
        material("o", 100),
        material("d", 100, originals=("o",)),
    ]
    first = plan_eviction(from_original, 200, cache_root=tmp_path)
    second = plan_eviction(from_derived, 200, cache_root=tmp_path)
    assert first.names == second.names == ["d", "o"]


def test_dependency_cycle_is_rejected(tmp_path):
    "Cycles make the problem invalid."
    materials = [
        material("a", 1, originals=("b",)),
        material("b", 1, originals=("a",)),
    ]
    with pytest.raises(EvictionError, match="Cyclic dependency"):
        plan_eviction(materials, 1, cache_root=tmp_path)


def test_unknown_dependency_is_rejected(tmp_path):
    "Dependencies must reference known materials."
    with pytest.raises(EvictionError, match="unknown material"):
        plan_eviction([material("a", 1, originals=("missing",))], 1)


def test_retained_version_is_protected(tmp_path):
    "Retained versions are never selected."
    make_file(tmp_path, "a", 100)
    make_file(tmp_path, "b", 100)
    materials = [
        material("a", 100, cost=1.0, version="v1"),
        material("b", 100, cost=9.0, version="v2"),
    ]
    plan = plan_eviction(
        materials, 100, cache_root=tmp_path, retained_versions=["v1"]
    )
    assert plan.names == ["b"]
    assert plan.retained["a"] == "version v1 is retained"


def test_retained_version_string_is_accepted(tmp_path):
    "A single version does not have to be wrapped in a list."
    make_file(tmp_path, "a", 100)
    make_file(tmp_path, "b", 100)
    materials = [
        material("a", 100, cost=1.0, version="v1"),
        material("b", 100, cost=9.0, version="v2"),
    ]
    plan = plan_eviction(materials, 100, cache_root=tmp_path, retained_versions="v1")
    assert plan.names == ["b"]


def test_retained_derived_keeps_original(tmp_path):
    "If a derived artifact is retained, its original cannot be evicted."
    make_file(tmp_path, "original", 100)
    make_file(tmp_path, "derived", 100)
    materials = [
        material("original", 100, cost=1.0, derived=("derived",)),
        material("derived", 100, cost=1.0, originals=("original",), pinned=True),
    ]
    plan = plan_eviction(materials, 100, cache_root=tmp_path)
    assert not plan.feasible
    assert plan.max_releasable == 0
    assert "needed by retained derived" in plan.retained["original"]


def test_retained_path_protects_material(tmp_path):
    "A retained path cannot be deleted."
    make_file(tmp_path, "a", 100)
    make_file(tmp_path, "b", 100)
    materials = [material("a", 100), material("b", 100)]
    plan = plan_eviction(
        materials, 100, cache_root=tmp_path, retained_paths=[tmp_path / "a"]
    )
    assert plan.names == ["b"]
    assert plan.retained["a"] == "path is retained"


def test_keep_unknown_material_is_rejected(tmp_path):
    "Keeping something that is not in the problem is an error."
    with pytest.raises(EvictionError, match="unknown material"):
        plan_eviction([material("a", 1)], 1, keep_materials=["nope"])


def test_lease_is_never_evicted(tmp_path):
    "Leased materials are excluded even if they are the cheapest."
    make_file(tmp_path, "a", 100)
    make_file(tmp_path, "b", 100)
    materials = [
        material("a", 100, cost=0.1, leased=True),
        material("b", 100, cost=5.0),
    ]
    plan = plan_eviction(materials, 100, cache_root=tmp_path)
    assert plan.names == ["b"]


def test_lease_holder_marks_material_leased(tmp_path):
    "A non-empty lease identifier protects the material."
    make_file(tmp_path, "a", 100)
    plan = plan_eviction(
        [material("a", 100, lease="job-42")], 100, cache_root=tmp_path
    )
    assert not plan.feasible
    assert plan.retained["a"] == "leased by job-42"


def test_execute_deletes_selected_files(tmp_path):
    "Execution removes the selected files and reports the released bytes."
    make_file(tmp_path, "keep", 100)
    make_file(tmp_path, "drop", 500)
    materials = [material("keep", 100), material("drop", 500, cost=0.1)]
    plan = plan_eviction(materials, 500, cache_root=tmp_path)
    result = execute_eviction(plan, materials, cache_root=tmp_path)
    assert result.evicted == ["drop"]
    assert result.released == 500
    assert result.cost == 0.1
    assert not (tmp_path / "drop").exists()
    assert (tmp_path / "keep").exists()


def test_execute_dry_run_deletes_nothing(tmp_path):
    "A dry run reports the plan without touching the disk."
    make_file(tmp_path, "a", 100)
    materials = [material("a", 100)]
    plan = plan_eviction(materials, 100, cache_root=tmp_path)
    result = execute_eviction(plan, materials, cache_root=tmp_path, dry_run=True)
    assert result.evicted == ["a"]
    assert result.released == 100
    assert result.dry_run
    assert (tmp_path / "a").exists()


def test_execute_skips_file_leased_after_planning(tmp_path):
    "A lease taken after planning must stop the deletion."
    make_file(tmp_path, "a", 100)
    make_file(tmp_path, "b", 100)
    materials = [material("a", 100, cost=1.0), material("b", 100, cost=2.0)]
    plan = plan_eviction(materials, 100, cache_root=tmp_path)
    assert plan.names == ["a"]
    leased = [
        material("a", 100, cost=1.0, leased=True),
        material("b", 100, cost=2.0),
    ]
    result = execute_eviction(plan, leased, cache_root=tmp_path)
    assert result.evicted == ["b"]
    assert result.released == 100
    assert "a" in result.skipped
    assert (tmp_path / "a").exists()
    assert not (tmp_path / "b").exists()


def test_execute_reports_shortfall_when_new_lease_makes_target_unreachable(tmp_path):
    "If the only safe material is gone, the actual release is reported."
    make_file(tmp_path, "a", 100)
    materials = [material("a", 100)]
    plan = plan_eviction(materials, 100, cache_root=tmp_path)
    result = execute_eviction(
        plan, [material("a", 100, leased=True)], cache_root=tmp_path
    )
    assert result.evicted == []
    assert result.released == 0
    assert result.plan.target_met is False
    assert result.plan.deficit == 100
    assert (tmp_path / "a").exists()


def test_execute_skips_replaced_file(tmp_path):
    "A file replaced since planning must not be deleted."
    path = make_file(tmp_path, "a", 100)
    materials = [material("a", 100)]
    plan = plan_eviction(materials, 100, cache_root=tmp_path)
    path.write_bytes(b"new content")
    result = execute_eviction(plan, materials, cache_root=tmp_path)
    assert result.evicted == []
    assert result.released == 0
    assert path.exists()


def test_execute_never_deletes_unowned_file(tmp_path):
    "Unowned files stay on disk even if they appear in a plan."
    path = make_file(tmp_path, "theirs", 100)
    materials = [material("theirs", 100, owner=False)]
    plan = plan_eviction(materials, 1, cache_root=tmp_path)
    assert not plan.feasible
    result = execute_eviction(plan, materials, cache_root=tmp_path)
    assert result.released == 0
    assert path.exists()


def test_execute_derived_blocked_by_late_lease_keeps_original(tmp_path):
    "A late lease on a derived artifact protects its original."
    make_file(tmp_path, "original", 100)
    make_file(tmp_path, "derived", 100)
    materials = [
        material("original", 100, derived=("derived",)),
        material("derived", 100, originals=("original",)),
    ]
    plan = plan_eviction(materials, 200, cache_root=tmp_path)
    assert set(plan.names) == {"original", "derived"}
    leased = [
        material("original", 100, derived=("derived",)),
        material("derived", 100, originals=("original",), leased=True),
    ]
    result = execute_eviction(plan, leased, cache_root=tmp_path)
    assert result.evicted == []
    assert (tmp_path / "original").exists()
    assert (tmp_path / "derived").exists()


def test_execute_never_follows_symlinks(tmp_path):
    "A symlinked cache entry must not delete its target."
    target = make_file(tmp_path, "target", 100)
    link = tmp_path / "link"
    link.symlink_to(target)
    materials = [material("link", 100, paths=("link",))]
    plan = plan_eviction(materials, 1, cache_root=tmp_path)
    assert not plan.feasible
    result = execute_eviction(plan, materials, cache_root=tmp_path)
    assert result.released == 0
    assert target.exists()
    assert link.exists()


def test_execute_measures_actual_release_with_hardlinks(tmp_path):
    "Only the last link releases bytes, measured after deletion."
    make_file(tmp_path, "a", 100)
    os.link(tmp_path / "a", tmp_path / "alias")
    materials = [
        material("a", 100, paths=("a",)),
        material("alias", 100, paths=("alias",)),
    ]
    plan = plan_eviction(materials, 100, cache_root=tmp_path)
    result = execute_eviction(plan, materials, cache_root=tmp_path)
    assert set(result.evicted) == {"a", "alias"}
    assert result.released == 100
    assert not (tmp_path / "a").exists()
    assert not (tmp_path / "alias").exists()


def test_execute_partial_hardlink_reports_no_release(tmp_path):
    "Deleting one link of a shared file releases nothing."
    make_file(tmp_path, "a", 100)
    os.link(tmp_path / "a", tmp_path / "keep")
    materials = [
        material("a", 100, paths=("a",)),
        material("keep", 100, paths=("keep",), pinned=True),
    ]
    plan = plan_eviction(materials, 1, cache_root=tmp_path)
    assert not plan.feasible
    result = execute_eviction(plan, materials, cache_root=tmp_path)
    assert result.released == 0
    assert (tmp_path / "keep").exists()


@pytest.mark.parametrize(
    ("materials", "match"),
    [
        ([{"name": "a", "size": -1}], "negative size"),
        ([{"name": "a", "refetch_cost": -1}], "refetch cost"),
        ([{"name": "a", "refetch_cost": float("inf")}], "refetch cost"),
        ([{"name": "a"}, {"name": "a"}], "Duplicate material"),
        ([{"name": ""}], "non-empty"),
    ],
)
def test_invalid_materials(materials, match):
    "Malformed descriptions raise a clear error."
    with pytest.raises(EvictionError, match=match):
        plan_eviction(materials, 1)


@pytest.mark.parametrize("requested", [-1, float("nan"), "10", True])
def test_invalid_request(requested):
    "The requested size must be a finite non-negative number."
    with pytest.raises(EvictionError):
        plan_eviction([], requested)


def test_cache_material_object_is_accepted(tmp_path):
    "CacheMaterial instances can be used directly."
    make_file(tmp_path, "a", 100)
    materials = [CacheMaterial(name="a", size=100, refetch_cost=1.0, paths=("a",))]
    plan = plan_eviction(materials, 100, cache_root=tmp_path)
    assert plan.names == ["a"]
    assert plan.released == 100


def test_duck_typed_material_is_accepted(tmp_path):
    "Objects with matching attributes are accepted."

    class Duck:
        name = "a"
        size = 100
        refetch_cost = 2.0
        paths = ("a",)

    make_file(tmp_path, "a", 100)
    plan = plan_eviction([Duck()], 50, cache_root=tmp_path)
    assert plan.names == ["a"]
    assert plan.cost == 2.0


def test_mapping_of_names_is_accepted(tmp_path):
    "A mapping of name to fields uses the keys as names."
    make_file(tmp_path, "a", 100)
    plan = plan_eviction(
        {"a": {"size": 100, "refetch_cost": 1.0, "paths": ("a",)}},
        100,
        cache_root=tmp_path,
    )
    assert plan.names == ["a"]


def test_missing_files_are_not_counted(tmp_path):
    "A material whose files do not exist releases nothing."
    materials = [material("ghost", 100)]
    plan = plan_eviction(materials, 1, cache_root=tmp_path)
    assert not plan.feasible
    assert plan.max_releasable == 0


def test_zero_request_returns_empty_plan(tmp_path):
    "Asking for zero bytes needs no eviction."
    make_file(tmp_path, "a", 100)
    plan = plan_eviction([material("a", 100)], 0, cache_root=tmp_path)
    assert plan.names == []
    assert plan.released == 0
    assert plan.target_met


def test_relative_and_absolute_paths(tmp_path):
    "Absolute paths are used as given, relative paths against the root."
    make_file(tmp_path, "a", 100)
    materials = [material("a", 100, paths=(str(tmp_path / "a"),))]
    plan = plan_eviction(materials, 100, cache_root=tmp_path)
    assert plan.released == 100


def test_execute_provider_reports_lease_between_revalidation_and_deletion(tmp_path):
    "A lease taken while files are being removed blocks the dependent chain."
    make_file(tmp_path, "original", 100)
    make_file(tmp_path, "derived", 100)
    materials = [
        material("original", 100, derived=("derived",)),
        material("derived", 100, originals=("original",)),
    ]
    plan = plan_eviction(materials, 200, cache_root=tmp_path)
    calls = {"count": 0}

    def provider():
        calls["count"] += 1
        if calls["count"] <= 2:
            return materials
        return [
            material("original", 100, derived=("derived",)),
            material("derived", 100, originals=("original",), leased=True),
        ]

    result = execute_eviction(plan, provider, cache_root=tmp_path)
    assert result.evicted == []
    assert result.released == 0
    assert (tmp_path / "original").exists()
    assert (tmp_path / "derived").exists()


def test_execute_provider_returns_fresh_materials(tmp_path):
    "Callables are re-read so late leases are respected."
    make_file(tmp_path, "a", 100)
    make_file(tmp_path, "b", 100)
    materials = [material("a", 100, cost=1.0), material("b", 100, cost=2.0)]
    plan = plan_eviction(materials, 100, cache_root=tmp_path)
    result = execute_eviction(plan, lambda: materials, cache_root=tmp_path)
    assert result.evicted == ["a"]
    assert result.released == 100


def test_plan_scales_to_many_independent_materials(tmp_path):
    "Independent materials are solved per component, not by total search."
    materials = []
    for index in range(120):
        name = f"file{index:04d}"
        make_file(tmp_path, name, 10)
        materials.append(material(name, 10, cost=1.0 + index % 7))
    plan = plan_eviction(materials, 500, cache_root=tmp_path)
    assert plan.target_met
    assert plan.released >= 500
    assert len(plan.materials) == 50


def test_plan_exact_flag_for_small_problems(tmp_path):
    "Normal problems are solved exactly."
    make_file(tmp_path, "a", 100)
    plan = plan_eviction([material("a", 100)], 50, cache_root=tmp_path)
    assert plan.exact


def test_plan_dependency_chain_requires_contiguous_eviction(tmp_path):
    "A chain can only be evicted from its newest toward its oldest link."
    materials = []
    for index in range(6):
        name = f"step{index}"
        make_file(tmp_path, name, 100)
        mat = material(name, 100, cost=1.0)
        if index:
            mat["originals"] = [f"step{index - 1}"]
            materials[-1]["derived"] = [name]
        materials.append(mat)
    plan = plan_eviction(materials, 300, cache_root=tmp_path)
    assert plan.names == ["step3", "step4", "step5"]
    assert plan.released == 300


def test_plan_shared_inode_creates_single_component(tmp_path):
    "Materials sharing a file are decided together."
    make_file(tmp_path, "shared", 100)
    os.link(tmp_path / "shared", tmp_path / "alias")
    materials = [
        material("shared", 100, cost=5.0, paths=("shared",)),
        material("alias", 100, cost=1.0, paths=("alias",)),
        material("other", 100, cost=1.0),
    ]
    make_file(tmp_path, "other", 100)
    plan = plan_eviction(materials, 100, cache_root=tmp_path)
    assert plan.names == ["other"]
    assert plan.released == 100


def test_revalidate_preserves_single_retained_version(tmp_path):
    "A version string is not split into characters when revalidated."
    make_file(tmp_path, "a", 100)
    make_file(tmp_path, "b", 100)
    materials = [
        material("a", 100, cost=1.0, version="v1"),
        material("b", 100, cost=5.0, version="v2"),
    ]
    plan = plan_eviction(materials, 100, cache_root=tmp_path, retained_versions="v1")
    assert plan.names == ["b"]
    updated = revalidate_eviction(plan, cache_root=tmp_path)
    assert updated.names == ["b"]
    assert updated.retained["a"] == "version v1 is retained"


def test_shared_path_forces_joint_eviction(tmp_path):
    "Materials listing the same path must be evicted together."
    make_file(tmp_path, "shared", 300)
    make_file(tmp_path, "only-a", 100)
    materials = [
        material("a", 400, cost=1.0, paths=("shared", "only-a")),
        material("b", 300, cost=9.0, paths=("shared",)),
    ]
    plan = plan_eviction(materials, 100, cache_root=tmp_path)
    assert plan.names == ["a", "b"]
    assert plan.released == 400
    result = execute_eviction(plan, materials, cache_root=tmp_path)
    assert result.evicted == ["a", "b"]
    assert result.released == 400
    assert not (tmp_path / "shared").exists()
    assert not (tmp_path / "only-a").exists()


def test_execute_twice_does_not_delete_unapproved_materials(tmp_path):
    "A plan can only be executed once; it never widens after the fact."
    make_file(tmp_path, "a", 100)
    make_file(tmp_path, "b", 100)
    materials = [material("a", 100, cost=1.0), material("b", 100, cost=9.0)]
    plan = plan_eviction(materials, 100, cache_root=tmp_path)
    first = execute_eviction(plan, materials, cache_root=tmp_path)
    assert first.evicted == ["a"]
    second = execute_eviction(plan, materials, cache_root=tmp_path)
    assert second.evicted == []
    assert second.released == 0
    assert "already been executed" in second.skipped["*"]
    assert (tmp_path / "b").exists()


def test_retained_path_propagates_to_ancestors(tmp_path):
    "Retaining a derived artifact also retains the original it needs."
    make_file(tmp_path, "archive", 100)
    make_file(tmp_path, "data", 100)
    materials = [
        material("archive", 100, cost=1.0, derived=("data",)),
        material("data", 100, cost=0.0, originals=("archive",)),
    ]
    plan = plan_eviction(
        materials, 1, cache_root=tmp_path, retained_paths=[tmp_path / "data"]
    )
    assert not plan.feasible
    assert plan.max_releasable == 0
    assert plan.retained["data"] == "path is retained"
    assert "needed by retained derived" in plan.retained["archive"]


def test_deficit_accounts_for_shared_hardlink(tmp_path):
    "Bytes shared with a retained material cannot be counted toward a deficit."
    make_file(tmp_path, "a", 400)
    os.link(tmp_path / "a", tmp_path / "b")
    materials = [
        material("a", 400, cost=1.0, paths=("a",)),
        material("b", 400, cost=1.0, paths=("b",), pinned=True),
    ]
    plan = plan_eviction(materials, 400, cache_root=tmp_path)
    assert not plan.feasible
    assert plan.max_releasable == 0
    assert plan.deficit == 400


def test_max_releasable_excludes_unowned_share(tmp_path):
    "An unowned alias keeps the inode alive, so the owned name frees nothing."
    make_file(tmp_path, "owned", 250)
    os.link(tmp_path / "owned", tmp_path / "theirs")
    materials = [
        material("owned", 250, paths=("owned",)),
        material("theirs", 250, paths=("theirs",), owner=False),
    ]
    plan = plan_eviction(materials, 1, cache_root=tmp_path)
    assert plan.max_releasable == 0
    assert plan.deficit == 1


def test_plan_many_materials_is_exact(tmp_path):
    "Independent materials are solved exactly even in large numbers."
    materials = []
    for index in range(60):
        name = f"n{index:03d}"
        make_file(tmp_path, name, 7)
        materials.append(material(name, 7, cost=float((index * 37) % 11)))
    plan = plan_eviction(materials, 400, cache_root=tmp_path)
    assert plan.exact
    # Every material has the same size, so the cheapest plan is a set of the
    # cheapest materials: sort by cost and take the first 58 (58 * 7 >= 400).
    costs = sorted(mat["refetch_cost"] for mat in materials)
    assert plan.cost == pytest.approx(sum(costs[:58]))
    assert plan.released == 406


def test_plan_tree_component_is_exact(tmp_path):
    "Archive with many extracted artifacts is solved exactly."
    make_file(tmp_path, "archive", 10)
    materials = [material("archive", 10, cost=100.0, derived=[])]
    for index in range(12):
        name = f"part{index}"
        make_file(tmp_path, name, 10)
        materials.append(
            material(name, 10, cost=float(index + 1), originals=("archive",))
        )
        materials[0]["derived"].append(name)
    plan = plan_eviction(materials, 70, cache_root=tmp_path)
    assert plan.exact
    # Evicting any artifact costs more than the archive (100), so picking the
    # seven cheapest artifacts is optimal.
    assert plan.cost == pytest.approx(sum(range(1, 8)))
    assert plan.released == 70


def test_infeasible_reports_exact_deficit_with_mixed_constraints(tmp_path):
    "Deficit combines leases, unowned files and hard links correctly."
    make_file(tmp_path, "free", 300)
    make_file(tmp_path, "leased", 300)
    make_file(tmp_path, "theirs", 400)
    os.link(tmp_path / "free", tmp_path / "alias")
    materials = [
        material("free", 300, paths=("free", "alias")),
        material("leased", 300, leased=True),
        material("theirs", 400, owner=False),
    ]
    plan = plan_eviction(materials, 1000, cache_root=tmp_path)
    assert plan.max_releasable == 300
    assert plan.deficit == 700
    assert plan.released == 0
