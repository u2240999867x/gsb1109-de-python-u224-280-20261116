"""Tests for tools/offline_pack_audit.py."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from offline_pack_audit import build_report  # noqa: E402

TOOLS = Path(__file__).resolve().parent


def load(name):
    with open(TOOLS / name, encoding="utf-8") as fobj:
        return json.load(fobj)


def file_entry(report, digest):
    return next(f for f in report["files"] if f["hash"] == digest)


def test_example_manifest_bounds():
    report = build_report(load("example_manifest.json"))
    assert report["status"] == "ok"
    # geom [400,600] needed inside bundle-1 (total 1000, other member
    # unbounded and not needed) + extra.bin [100,300]; tiny-data cached.
    assert report["download_bytes"] == {"min": 500, "max": 900}


def test_shared_original_downloaded_once():
    report = build_report(load("example_manifest.json"))
    shared = {s["hash"] for s in report["shared_originals"]}
    assert "sha256:BBBB" in shared  # geom.bin used by alpha and beta
    geom = file_entry(report, "sha256:BBBB")
    assert geom["packages"] == ["alpha", "beta"]
    assert geom["download"] is True
    # counted once in the totals (400..600, not 800..1200)
    assert report["download_bytes"]["max"] == 900


def test_unverified_cache_not_deducted():
    report = build_report(load("example_manifest.json"))
    paths = {c["path"] for c in report["cache_not_deducted"]}
    assert "data/geom.bin" in paths
    assert file_entry(report, "sha256:BBBB")["cache"] is None


def test_verified_cache_deducted():
    report = build_report(load("example_manifest.json"))
    tiny = file_entry(report, "sha256:AAAA")
    assert tiny["cache"] == "verified-hit"
    assert tiny["download"] is False


def test_products_not_shared_across_versions():
    report = build_report(load("example_manifest.json"))
    gen = {(p["processor"], p["version"], p["package"])
           for p in report["products"]["to_generate"]}
    assert ("unzip", "1", "alpha") in gen
    assert ("unzip", "2", "beta") in gen


def test_dependency_closure_and_reclaimed_original():
    report = build_report(load("example_manifest.json"))
    gen = {(p["processor"], p["version"]) for p in report["products"]["to_generate"]}
    # grid@2 pulled in its dependency closure; reproject@3 is cache-satisfied
    assert ("grid", "2") in gen
    usable = report["products"]["usable_from_cache"]
    reproj = next(p for p in usable if p["processor"] == "reproject")
    # product usable, original reclaimed -> no download for map.tif
    assert reproj["original_downloaded"] is False
    assert file_entry(report, "sha256:EEEE")["download"] is False


def test_budget_never_negative():
    report = build_report(load("example_manifest.json"))
    budget = report["budget"]
    assert budget["remaining_best"] == 300          # 800 - 500
    assert budget["remaining_worst"] == 0           # clamped, not -100
    assert budget["shortfall_worst"] == 100
    assert report["status"] == "ok"                 # budget >= min download


def test_conflicting_claims_listed_without_budget():
    report = build_report(load("example_conflict.json"))
    assert report["status"] == "conflict"
    text = "\n".join(report["conflicts"])
    assert "pack-x.zip" in text          # archive total contradicts member mins
    assert "sha256:3333" in text         # same hash, sizes 50 vs 60
    # no bounds and no negative remaining budget are emitted
    assert report["download_bytes"] is None
    assert report["budget"] is None


def test_archive_equality_pins_total():
    manifest = {
        "packages": [{"name": "p", "registry": [
            {"path": "a", "hash": "h1", "size": [10, 20]},
            {"path": "b", "hash": "h2", "size": [30, 40]},
        ]}],
        "archives": [{"name": "z", "members": ["a", "b"], "total_size": 50}],
    }
    report = build_report(manifest)
    assert report["status"] == "ok"
    assert report["download_bytes"] == {"min": 50, "max": 50}
    allocs = {f["hash"]: f["alloc_min"] for f in report["files"]}
    assert allocs["h1"] + allocs["h2"] == 50


def test_archive_max_total_constraint():
    manifest = {
        "packages": [{"name": "p", "registry": [
            {"path": "a", "hash": "h1", "size": [10, 100]},
            {"path": "b", "hash": "h2", "size": [10, 100]},
        ]}],
        "archives": [{"name": "z", "members": ["a", "b"],
                      "max_total_size": 60}],
    }
    report = build_report(manifest)
    assert report["download_bytes"] == {"min": 20, "max": 60}


def test_unbounded_unknown_size():
    manifest = {
        "packages": [{"name": "p", "registry": [
            {"path": "a", "hash": "h1"},
        ]}],
    }
    report = build_report(manifest)
    assert report["download_bytes"] == {"min": 0, "max": "unbounded"}


def test_budget_shortfall_is_conflict_not_negative():
    manifest = {
        "budget_bytes": 10,
        "packages": [{"name": "p", "registry": [
            {"path": "a", "hash": "h1", "size": 50},
        ]}],
    }
    report = build_report(manifest)
    assert report["status"] == "conflict"
    assert any("预算不足" in c for c in report["conflicts"])
    assert report["budget"]["remaining_best"] == 0
    assert report["budget"]["remaining_worst"] == 0


def test_allocation_verifiable_against_claims():
    """The emitted allocation must satisfy every declared claim."""
    report = build_report(load("example_manifest.json"))
    assert report["status"] == "ok"
    for entry in report["files"]:
        lo, hi = entry["size_range"]
        hi = float("inf") if hi == "unbounded" else hi
        assert lo <= entry["alloc_min"] <= hi
        if entry["alloc_max"] is not None:
            assert lo <= entry["alloc_max"] <= hi
            assert entry["alloc_min"] <= entry["alloc_max"]
    bundle = [f for f in report["files"] if f["archive"] == "bundle-1.zip"]
    # needed members of bundle-1 plus its (unregistered) third member must
    # leave room for the declared total of 1000 bytes
    assert sum(f["alloc_min"] for f in bundle) <= 1000


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
