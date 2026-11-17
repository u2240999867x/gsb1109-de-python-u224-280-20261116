# Copyright (c) 2018 The Pooch Developers.
# Distributed under the terms of the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause
#
# This code is part of the Fatiando a Terra project (https://www.fatiando.org)
#
"""
Test the shared-file reference registry.

These checks cover the requirement that removing a material from one data
package version only drops that version's reference: a file still used by
another package sharing the cache directory must not be deleted.
"""

import json
from pathlib import Path

import pytest

from .. import Pooch
from ..core import create
from ..references import SharedFileRegistry


@pytest.fixture
def cache_with_files(tmp_path):
    """Create a cache directory containing a shared and an exclusive file."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    shared = data_dir / "shared.txt"
    exclusive = data_dir / "exclusive.txt"
    shared.write_text("shared content")
    exclusive.write_text("exclusive content")
    return tmp_path, shared, exclusive


def test_add_is_idempotent_per_owner(cache_with_files):
    "Adding the same owner twice must not inflate the reference count"
    cache, shared, _ = cache_with_files
    registry = SharedFileRegistry(cache)
    registry.add(shared, "package-a:v1:shared.txt")
    registry.add(shared, "package-a:v1:shared.txt")
    assert registry.count(shared) == 1
    assert registry.owners(shared) == ["package-a:v1:shared.txt"]


def test_references_persist_across_instances(cache_with_files):
    "Counts written by one registry must be visible to a fresh instance"
    cache, shared, _ = cache_with_files
    SharedFileRegistry(cache).add(shared, "package-a:v1:shared.txt")
    SharedFileRegistry(cache).add(shared, "package-b:v2:shared.txt")
    reloaded = SharedFileRegistry(cache)
    assert reloaded.count(shared) == 2
    assert reloaded.is_shared(shared)
    assert reloaded.owners(shared) == [
        "package-a:v1:shared.txt",
        "package-b:v2:shared.txt",
    ]


def test_shared_file_survives_single_owner_removal(cache_with_files):
    "Removing one version's reference must not delete a still-shared file"
    cache, shared, exclusive = cache_with_files
    registry = SharedFileRegistry(cache)
    registry.add(shared, "package-a:v1:shared.txt")
    registry.add(shared, "package-b:v2:shared.txt")
    registry.add(exclusive, "package-a:v1:exclusive.txt")

    # A new package A version drops one material: only its reference is removed
    assert SharedFileRegistry(cache).remove(
        shared, "package-a:v1:shared.txt"
    ) is False
    assert shared.exists()
    assert SharedFileRegistry(cache).owners(shared) == [
        "package-b:v2:shared.txt"
    ]

    # The file exclusive to package A has no remaining owners and is deleted,
    # together with the directory once it becomes empty.
    assert SharedFileRegistry(cache).remove(
        exclusive, "package-a:v1:exclusive.txt"
    ) is True
    assert not exclusive.exists()


def test_last_owner_removal_deletes_file_and_prunes_dirs(tmp_path):
    "Once every owner is gone the file and emptied directories are removed"
    nested = tmp_path / "nested" / "deep"
    nested.mkdir(parents=True)
    shared = nested / "shared.txt"
    shared.write_text("shared content")
    registry = SharedFileRegistry(tmp_path)
    registry.add(shared, "package-a:v1:shared.txt")
    registry.add(shared, "package-b:v2:shared.txt")

    registry.remove(shared, "package-a:v1:shared.txt")
    assert shared.exists()
    registry.remove(shared, "package-b:v2:shared.txt")
    assert not shared.exists()
    assert not (tmp_path / "nested").exists()
    # The cache directory itself and the index are retained
    assert tmp_path.exists()
    assert Path(registry.index_file).exists()


def test_remove_without_cleanup_keeps_file_on_disk(cache_with_files):
    "Callers can release a reference while opting out of physical deletion"
    cache, shared, _ = cache_with_files
    registry = SharedFileRegistry(cache)
    registry.add(shared, "package-a:v1:shared.txt")
    deleted = registry.remove(
        shared, "package-a:v1:shared.txt", cleanup=False
    )
    assert deleted is False
    assert shared.exists()
    assert registry.count(shared) == 0


def test_removing_unknown_reference_is_a_noop(cache_with_files):
    "An owner that was never recorded must not raise or delete anything"
    cache, shared, _ = cache_with_files
    registry = SharedFileRegistry(cache)
    registry.add(shared, "package-a:v1:shared.txt")
    assert registry.remove(shared, "package-c:v9:shared.txt") is False
    assert shared.exists()
    assert registry.count(shared) == 1


def test_remove_owner_releases_all_its_files(cache_with_files):
    "Bulk removal drops every reference held by one package entry"
    cache, shared, exclusive = cache_with_files
    registry = SharedFileRegistry(cache)
    owner_a = "package-a:v1"
    registry.add(shared, f"{owner_a}:shared.txt")
    registry.add(exclusive, f"{owner_a}:exclusive.txt")
    registry.add(shared, "package-b:v2:shared.txt")

    deleted = SharedFileRegistry(cache).remove_owner(f"{owner_a}:shared.txt")
    assert deleted == []
    assert shared.exists()

    deleted_exclusive = SharedFileRegistry(cache).remove_owner(
        f"{owner_a}:exclusive.txt"
    )
    assert deleted_exclusive == [str(exclusive)]
    assert not exclusive.exists()
    assert SharedFileRegistry(cache).owners(shared) == [
        "package-b:v2:shared.txt"
    ]


def test_tracking_outside_cache_root_is_rejected(tmp_path):
    "References can only cover files inside the cache directory"
    registry = SharedFileRegistry(tmp_path)
    outside = tmp_path.parent / "outside.txt"
    with pytest.raises(ValueError, match="outside of the cache directory"):
        registry.add(outside, "package-a:v1:outside.txt")


def test_corrupted_index_is_ignored(cache_with_files):
    "An unreadable index must not prevent fresh reference bookkeeping"
    cache, shared, _ = cache_with_files
    registry = SharedFileRegistry(cache)
    registry.add(shared, "package-a:v1:shared.txt")
    Path(registry.index_file).write_text("{not valid json")
    reloaded = SharedFileRegistry(cache)
    assert reloaded.count(shared) == 0
    reloaded.add(shared, "package-b:v2:shared.txt")
    assert reloaded.count(shared) == 1


def test_index_stored_as_relative_portable_keys(cache_with_files):
    "Index keys must be relative to the cache so the folder stays portable"
    cache, shared, _ = cache_with_files
    index_file = SharedFileRegistry(cache).index_file
    SharedFileRegistry(cache).add(shared, "package-a:v1:shared.txt")
    with open(index_file, encoding="utf-8") as handle:
        contents = json.load(handle)
    assert list(contents) == ["data/shared.txt"]
    entry = contents["data/shared.txt"]
    assert entry["count"] == 1
    assert entry["owners"] == ["package-a:v1:shared.txt"]


def test_pooch_removal_keeps_files_used_by_another_package(cache_with_files):
    "Pooch.remove_registry_file only removes the current package's reference"
    cache, shared, exclusive = cache_with_files
    registry_a = {
        "data/shared.txt": "hash-shared",
        "data/exclusive.txt": "hash-exclusive",
    }
    pup_a = Pooch(
        path=cache,
        base_url="http://example.invalid/a/",
        registry=dict(registry_a),
        name="package-a",
    )
    pup_b = Pooch(
        path=cache,
        base_url="http://example.invalid/b/",
        registry={"data/shared.txt": "hash-shared"},
        name="package-b",
    )

    pup_a.track("data/shared.txt")
    pup_a.track("data/exclusive.txt")
    pup_b.track("data/shared.txt")

    # The new version of package A removes one material
    deleted_shared = pup_a.remove_registry_file("data/shared.txt")
    assert deleted_shared is False
    assert shared.exists()
    assert "data/shared.txt" not in pup_a.registry
    # Package B is untouched and still owns the shared file
    assert "data/shared.txt" in pup_b.registry
    assert pup_b.references.count(shared) == 1

    # Package A's exclusive material can be cleaned up
    deleted_exclusive = pup_a.remove_registry_file("data/exclusive.txt")
    assert deleted_exclusive is True
    assert not exclusive.exists()

    # Once package B also drops it, the shared file is finally removed
    assert pup_b.remove_registry_file("data/shared.txt") is True
    assert not shared.exists()


def test_pooch_untrack_unknown_file_raises(cache_with_files):
    "Removing a material absent from the registry must fail loudly"
    cache, _, _ = cache_with_files
    pup = Pooch(
        path=cache,
        base_url="http://example.invalid/",
        registry={"data/shared.txt": "hash"},
        name="package-a",
    )
    with pytest.raises(ValueError, match="is not in the registry"):
        pup.remove_registry_file("data/missing.txt")


def test_pooch_default_name_derived_from_cache_path(tmp_path):
    "Packages without an explicit name get a stable name from the path"
    cache = tmp_path / "my-cache"
    cache.mkdir()
    pup = Pooch(path=cache, base_url="http://example.invalid/", registry={})
    assert pup.name == "my-cache"
    assert pup.references.cache_dir == str(cache)


def test_create_forwards_package_name(tmp_path):
    "The create factory must expose the package name for shared caches"
    pup = create(
        path=tmp_path,
        base_url="http://example.invalid/",
        registry={},
        name="shared-package",
    )
    assert pup.name == "shared-package"
