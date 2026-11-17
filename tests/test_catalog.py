from __future__ import unicode_literals

import itertools
import random
import unittest

from fs.catalog import (
    Backend,
    CacheEntry,
    CatalogError,
    FIELD_NAMESPACES,
    Manifest,
    PlanConflict,
    FSInfoTransport,
    Transport,
    TransportResponse,
    collect,
    execute,
    plan,
    replan,
    verify_manifest,
)


def _fields_for(namespaces, values):
    return {
        field: value
        for field, value in values.items()
        if field in FIELD_NAMESPACES
        and FIELD_NAMESPACES[field] in namespaces
    }


class ScriptedTransport(Transport):
    """Transport backed by a dict, recording every call."""

    def __init__(self, records, versions=None):
        self.records = records
        self.versions = versions or {}
        self.calls = []

    def request(self, backend, namespaces, paths):
        self.calls.append((backend.name, tuple(namespaces), tuple(paths)))
        values = {}
        versions = {}
        for path in paths:
            record = self.records[path]
            values[path] = _fields_for(namespaces, record)
            versions[path] = record.get("version", self.versions.get(path, 1))
        return TransportResponse(values, versions)


class TestPlanning(unittest.TestCase):
    def test_single_backend_batches(self):
        backend = Backend("one", ["details", "access"], max_batch=2, batch_cost=2,
                          per_path_cost=1)
        result = plan(
            ["/a", "/b", "/c"], fields=["size", "permissions"], backends=[backend]
        )
        self.assertEqual(result.cost, 7)
        self.assertEqual([len(r.paths) for r in result.requests], [2, 1])
        self.assertEqual(sum(r.cost for r in result.requests), result.cost)

    def test_split_across_backends_when_cheaper(self):
        cheap_batches = Backend("batched", ["details"], max_batch=5, batch_cost=36,
                                per_path_cost=2)
        cheap_paths = Backend("bulk", ["details"], max_batch=50, batch_cost=7,
                              per_path_cost=15)
        result = plan(
            ["/p{}".format(i) for i in range(61)],
            fields=["size"],
            backends=[cheap_batches, cheap_paths],
        )
        self.assertEqual(result.cost, 574)
        used = {request.backend.name for request in result.requests}
        self.assertEqual(used, {"batched", "bulk"})

    def test_namespaces_can_split_inside_one_path(self):
        details = Backend("details", ["details"], batch_cost=1)
        access = Backend("access", ["access"], batch_cost=1)
        result = plan(["/a"], fields=["size", "permissions"],
                      backends=[details, access])
        self.assertEqual(result.cost, 2)
        self.assertEqual(
            sorted(request.backend.name for request in result.requests),
            ["access", "details"],
        )

    def test_combined_backend_beats_split(self):
        both = Backend("both", ["details", "access"], batch_cost=2)
        details = Backend("details", ["details"], batch_cost=1)
        access = Backend("access", ["access"], batch_cost=1)
        result = plan(["/a", "/b"], fields=["size", "permissions"],
                      backends=[both, details, access])
        # both: 2 for two paths; split: 2+2 = 4.
        self.assertEqual(result.cost, 2)
        self.assertEqual(
            list(result.requests[0].namespaces), ["access", "details"]
        )

    def test_plan_is_marked_exact(self):
        backend = Backend("one", ["details"], batch_cost=1)
        result = plan(["/a"], fields=["size"], backends=[backend])
        self.assertTrue(result.exact)

    def test_large_mixed_catalog_stays_responsive(self):
        details_batched = Backend(
            "a", ["details"], max_batch=50, batch_cost=2, per_path_cost=1
        )
        combined = Backend(
            "b", ["details", "access"], max_batch=100, batch_cost=3,
            per_path_cost=1,
        )
        access_only = Backend(
            "c", ["access"], max_batch=10, batch_cost=1, per_path_cost=2
        )
        paths = ["/p{}".format(i) for i in range(1000)]
        cache = [
            CacheEntry(path, "size", 1, version=1, compatibility="1")
            for path in paths[::2]
        ]
        result = plan(
            paths,
            fields=["size", "permissions"],
            backends=[details_batched, combined, access_only],
            cache=cache,
        )
        self.assertEqual(result.cost, sum(r.cost for r in result.requests))
        self.assertIn(result.exact, (True, False))

    def test_mixed_signatures_use_blended_backends(self):
        details = Backend("det", ["details"], batch_cost=1)
        access = Backend("acc", ["access"], batch_cost=1)
        both = Backend("both", ["details", "access"], batch_cost=5)
        cache = [CacheEntry("/a", "size", 1, version=1, compatibility="1")]
        result = plan(
            ["/a", "/b"],
            fields=["size", "permissions"],
            backends=[details, access, both],
            cache=cache,
        )
        # /a needs only access (its size is cached); /b needs both.  The
        # access request covers /a and /b together, so splitting across
        # per-namespace backends costs 2 while ``both`` alone costs 5.
        self.assertEqual(result.cost, 2)
        requests = {
            request.backend.name: set(request.paths)
            for request in result.requests
        }
        self.assertEqual(requests["acc"], {"/a", "/b"})
        self.assertEqual(requests["det"], {"/b"})
        self.assertNotIn("both", requests)

    def test_fallback_plan_covers_every_path(self):
        details = Backend(
            "a", ["details"], max_batch=50, batch_cost=2, per_path_cost=1
        )
        combined = Backend(
            "b", ["details", "access"], max_batch=100, batch_cost=3,
            per_path_cost=1,
        )
        access = Backend(
            "c", ["access"], max_batch=10, batch_cost=1, per_path_cost=2
        )
        paths = ["/p{}".format(i) for i in range(2000)]
        cache = [
            CacheEntry(path, "size", 1, version=1, compatibility="1")
            for path in paths[::2]
        ]
        result = plan(
            paths,
            fields=["size", "permissions"],
            backends=[details, combined, access],
            cache=cache,
        )
        covered = {path: set() for path in paths}
        for request in result.requests:
            for path in request.paths:
                covered[path].update(request.namespaces)
        for path in paths:
            expected = {"access"}
            if path not in set(entry.path for entry in cache):
                expected.add("details")
            self.assertTrue(expected <= covered[path], path)
        self.assertFalse(result.exact)

    def test_missing_namespace_raises(self):
        backend = Backend("details", ["details"], batch_cost=1)
        with self.assertRaises(PlanConflict):
            plan(["/a"], fields=["permissions"], backends=[backend])

    def test_unknown_field_raises(self):
        with self.assertRaises(CatalogError):
            plan(["/a"], fields=["colour"], backends=[Backend("b", ["details"])])

    def test_unbounded_batch_is_one_request(self):
        backend = Backend("all", ["details"], max_batch=0, batch_cost=5,
                          per_path_cost=2)
        result = plan(["/a", "/b", "/c"], fields=["size"], backends=[backend])
        self.assertEqual(result.cost, 5 + 2 * 3)
        self.assertEqual(len(result.requests), 1)

    def test_matches_brute_force(self):
        def brute(paths, fields, backends):
            keys = [
                (path, namespace)
                for path in paths
                for namespace in sorted(
                    {FIELD_NAMESPACES[field] for field in fields}
                )
            ]
            choices = [
                [backend for backend in backends if backend.supports(namespace)]
                for _path, namespace in keys
            ]
            best = None
            for combination in itertools.product(*choices):
                counts = {backend.name: set() for backend in backends}
                for (path, _namespace), backend in zip(keys, combination):
                    counts[backend.name].add(path)
                cost = sum(
                    backend.request_cost(len(counts[backend.name]))
                    for backend in backends
                )
                if best is None or cost < best:
                    best = cost
            return best

        generator = random.Random(41)
        for _trial in range(120):
            backend_count = generator.randint(1, 3)
            backends = []
            for index in range(backend_count):
                namespaces = generator.choice(
                    [["details"], ["access"], ["details", "access"]]
                )
                backends.append(
                    Backend(
                        "b{}".format(index),
                        namespaces,
                        max_batch=generator.choice([0, 1, 2, 4]),
                        batch_cost=generator.randint(0, 6),
                        per_path_cost=generator.randint(0, 4),
                    )
                )
            paths = ["/p{}".format(i) for i in range(generator.randint(1, 5))]
            fields = generator.choice(
                [["size"], ["permissions"], ["size", "permissions"]]
            )
            try:
                result = plan(paths, fields=fields, backends=backends)
            except PlanConflict:
                self.assertIsNone(brute(paths, fields, backends))
                continue
            self.assertEqual(result.cost, brute(paths, fields, backends))


class TestCache(unittest.TestCase):
    def test_incompatible_cache_is_ignored(self):
        backend = Backend("b", ["details"], batch_cost=1, compatibility="2")
        cache = [
            CacheEntry("/a", "size", 5, version=1, compatibility="1"),
        ]
        result = plan(["/a"], fields=["size"], backends=[backend], cache=cache)
        self.assertEqual(result.reused, [])
        self.assertEqual(result.cost, 1)
        self.assertEqual(len(result.requests), 1)

    def test_compatible_cache_offsets_cost(self):
        backend = Backend("b", ["details"], batch_cost=1, compatibility="2")
        cache = [
            CacheEntry("/a", "size", 5, version=1, compatibility="2"),
        ]
        result = plan(["/a"], fields=["size"], backends=[backend], cache=cache)
        self.assertEqual(result.cost, 0)
        self.assertEqual(result.requests, [])
        self.assertEqual([entry.field for entry in result.reused], ["size"])

    def test_cache_version_must_match_current(self):
        backend = Backend("b", ["details"], batch_cost=1, compatibility="2")
        cache = [
            CacheEntry("/a", "size", 5, version=1, compatibility="2"),
        ]
        result = plan(
            ["/a"],
            fields=["size"],
            backends=[backend],
            cache=cache,
            current_versions={"/a": 2},
        )
        self.assertEqual(result.reused, [])
        self.assertEqual(result.cost, 1)

    def test_cache_can_cover_a_namespace_with_no_backend(self):
        access = Backend("access", ["access"], batch_cost=1, compatibility="1")
        cache = [CacheEntry("/a", "size", 5, version=1, compatibility="1")]
        result = plan(
            ["/a"],
            fields=["size", "permissions"],
            backends=[access],
            cache=cache,
        )
        self.assertEqual(result.cost, 1)
        self.assertEqual(
            {path for request in result.requests for path in request.paths},
            {"/a"},
        )
        self.assertEqual(
            {request.namespaces for request in result.requests}, {("access",)}
        )

    def test_partial_cache_still_requests_remaining_namespace(self):
        details = Backend("details", ["details"], batch_cost=1, compatibility="1")
        access = Backend("access", ["access"], batch_cost=1, compatibility="1")
        cache = [CacheEntry("/a", "size", 5, version=1, compatibility="1")]
        result = plan(
            ["/a", "/b"],
            fields=["size", "permissions"],
            backends=[details, access],
            cache=cache,
        )
        self.assertEqual([entry.path for entry in result.reused], ["/a"])
        requested = {
            (request.backend.name, path)
            for request in result.requests
            for path in request.paths
        }
        self.assertIn(("access", "/a"), requested)
        self.assertNotIn(("details", "/a"), requested)


class TestExecution(unittest.TestCase):
    def setUp(self):
        self.records = {
            "/a": {
                "size": 10,
                "modified": 100,
                "permissions": "rw-r--r--",
                "version": 7,
            },
            "/b": {
                "size": 20,
                "modified": 200,
                "permissions": "rw-------",
                "version": 7,
            },
        }

    def test_atomic_request_is_complete(self):
        backend = Backend(
            "both",
            ["details", "access"],
            max_batch=0,
            batch_cost=1,
            supports_consistency_proof=False,
        )
        transport = ScriptedTransport(self.records)
        manifest = collect(
            ["/a", "/b"],
            fields=["size", "permissions"],
            backends=[backend],
            transport=transport,
        )
        self.assertEqual(len(transport.calls), 1)
        for path in ("/a", "/b"):
            self.assertTrue(manifest.is_complete(path))
            self.assertEqual(manifest.independent_fields(path), [])
            self.assertEqual(manifest.state(path), "complete")

    def test_unproven_split_is_independent(self):
        details = Backend("details", ["details"], batch_cost=1)
        access = Backend("access", ["access"], batch_cost=1)
        manifest = collect(
            ["/a"],
            fields=["size", "permissions"],
            backends=[details, access],
            transport=ScriptedTransport(self.records),
        )
        self.assertFalse(manifest.is_complete("/a"))
        self.assertEqual(
            manifest.independent_fields("/a"), ["permissions", "size"]
        )
        self.assertIsNone(manifest.publishable_record("/a"))
        self.assertEqual(manifest.state("/a"), "independent")

    def test_proven_split_is_complete(self):
        details = Backend(
            "details", ["details"], batch_cost=1, supports_consistency_proof=True
        )
        access = Backend(
            "access", ["access"], batch_cost=1, supports_consistency_proof=True
        )
        manifest = collect(
            ["/a"],
            fields=["size", "permissions"],
            backends=[details, access],
            transport=ScriptedTransport(self.records),
        )
        self.assertTrue(manifest.is_complete("/a"))
        self.assertEqual(
            manifest.publishable_record("/a"),
            {"permissions": "rw-r--r--", "size": 10},
        )

    def test_transport_may_assert_coherence(self):
        backend = Backend("details", ["details"], batch_cost=1)
        records = {"/a": {"size": 10, "version": 5}}
        transport = ScriptedTransport(records)
        original = transport.request

        def asserting(backend_, namespaces, paths):
            response = original(backend_, namespaces, paths)
            response.consistent = True
            return response

        transport.request = asserting
        manifest = collect(
            ["/a"], fields=["size"], backends=[backend], transport=transport
        )
        self.assertTrue(manifest.requests[0].supports_consistency_proof)
        self.assertEqual(verify_manifest(manifest), [])

    def test_persistent_drift_marks_path_independent(self):
        records = {
            "/a": {"size": 10, "permissions": "r", "version": 1},
            "/b": {"size": 20, "permissions": "w", "version": 1},
        }
        details = Backend("details", ["details"], batch_cost=1)
        access = Backend("access", ["access"], batch_cost=1)
        transport = ScriptedTransport(records)

        original_request = transport.request

        def drifting(backend, namespaces, paths):
            if "details" in namespaces:
                records["/a"]["version"] += 1
            return original_request(backend, namespaces, paths)

        transport.request = drifting
        manifest = collect(
            ["/a", "/b"],
            fields=["size", "permissions"],
            backends=[details, access],
            transport=transport,
            max_replans=2,
        )
        self.assertNotEqual(
            manifest.field("/a", "size").version,
            manifest.field("/a", "permissions").version,
        )
        self.assertEqual(manifest.independent_fields("/a"), ["permissions", "size"])

    def test_one_time_change_is_replanned(self):
        records = {
            "/a": {"size": 10, "permissions": "r", "version": 1},
            "/b": {"size": 20, "permissions": "w", "version": 1},
            "/c": {"size": 30, "permissions": "v", "version": 1},
        }
        details = Backend(
            "details", ["details"], batch_cost=1, supports_consistency_proof=True
        )
        access = Backend(
            "access", ["access"], batch_cost=1, supports_consistency_proof=True
        )
        transport = ScriptedTransport(records)
        original_request = transport.request
        state = {"changed": False}

        def drifting(backend, namespaces, paths):
            if "details" in namespaces and not state["changed"]:
                state["changed"] = True
                records["/a"]["version"] = 2
                records["/a"]["size"] = 110
            return original_request(backend, namespaces, paths)

        transport.request = drifting
        manifest = collect(
            ["/a", "/b", "/c"],
            fields=["size", "permissions"],
            backends=[details, access],
            transport=transport,
        )
        first_pass = len(transport.calls)
        self.assertTrue(first_pass >= 2)
        # The affected path was requested again; unrelated paths were not.
        for _backend, _namespaces, call_paths in transport.calls[first_pass:]:
            self.assertNotIn("/b", call_paths)
            self.assertNotIn("/c", call_paths)
        self.assertEqual(manifest.state("/a"), "complete")
        self.assertEqual(manifest.value("/a", "size"), 110)
        for path in ("/b", "/c"):
            self.assertEqual(manifest.state(path), "complete")
            self.assertEqual(
                manifest.field(path, "size").source, "cache"
            )

    def test_transport_error_marks_path_incomplete(self):
        class Failing(Transport):
            def request(self, backend, namespaces, paths):
                raise IOError("backend down")

        backend = Backend("b", ["details"], batch_cost=1)
        manifest = collect(
            ["/a"], fields=["size"], backends=[backend], transport=Failing()
        )
        self.assertEqual(manifest.missing("/a"), ["size"])
        self.assertEqual(manifest.state("/a"), "incomplete")
        self.assertEqual(manifest.requests[0].error, "backend down")


class TestReplanning(unittest.TestCase):
    def test_unrelated_verified_paths_are_reused(self):
        records = {
            "/a": {"size": 10, "permissions": "p1", "version": 1},
            "/b": {"size": 20, "permissions": "p2", "version": 1},
            "/c": {"size": 30, "permissions": "p3", "version": 1},
        }
        details = Backend(
            "details", ["details"], batch_cost=1, supports_consistency_proof=True
        )
        access = Backend(
            "access", ["access"], batch_cost=1, supports_consistency_proof=True
        )
        state = {"changed": False}
        transport = ScriptedTransport(records)
        original_request = transport.request

        def drifting(backend, namespaces, paths):
            if "details" in namespaces and not state["changed"]:
                state["changed"] = True
                records["/a"]["version"] = 2
                records["/a"]["size"] = 110
            return original_request(backend, namespaces, paths)

        transport.request = drifting
        first_pass = plan(
            ["/a", "/b", "/c"],
            fields=["size", "permissions"],
            backends=[details, access],
        )
        first_calls = len(first_pass.requests)
        manifest = collect(
            ["/a", "/b", "/c"],
            fields=["size", "permissions"],
            backends=[details, access],
            transport=transport,
        )
        second_pass = transport.calls[first_calls:]
        self.assertEqual(
            {path for _b, _n, call_paths in second_pass for path in call_paths},
            {"/a"},
        )
        self.assertEqual(manifest.state("/b"), "complete")
        self.assertEqual(manifest.state("/c"), "complete")
        self.assertEqual(
            manifest.field("/b", "size").compatibility, details.compatibility
        )
        # Cached values still resolve to the original request and version.
        self.assertEqual(verify_manifest(manifest), [])
        trace = manifest.trace("/b", "size")
        self.assertEqual(trace["request"]["request_id"], "r2")
        self.assertEqual(trace["basis"]["path_version"], 1)

    def test_replan_keeps_unchanged_values(self):
        backend = Backend(
            "d", ["details"], batch_cost=1, supports_consistency_proof=True
        )
        first = plan(["/a", "/b"], fields=["size"], backends=[backend])
        records = {
            "/a": {"size": 1, "version": 1},
            "/b": {"size": 2, "version": 1},
        }
        manifest = execute(first, ScriptedTransport(records))
        second = replan(first, ["/a"], manifest=manifest)
        self.assertEqual(
            {path for request in second.requests for path in request.paths},
            {"/a"},
        )
        self.assertEqual(
            {entry.path for entry in second.reused}, {"/b"}
        )


class TestFSInfoTransport(unittest.TestCase):
    def test_collects_from_a_real_filesystem(self):
        from fs.tempfs import TempFS

        with TempFS() as temp_fs:
            temp_fs.writetext("a.txt", "hello")
            temp_fs.makedir("d")
            backend = Backend(
                "temp",
                ["details", "access"],
                batch_cost=1,
                supports_consistency_proof=True,
            )
            manifest = collect(
                ["/a.txt", "/d"],
                fields=["size", "permissions"],
                backends=[backend],
                transport=FSInfoTransport({"temp": temp_fs}),
            )
        self.assertEqual(manifest.value("/a.txt", "size"), 5)
        self.assertEqual(manifest.value("/d", "permissions"), "rwxr-xr-x")
        self.assertTrue(manifest.is_complete("/a.txt"))
        self.assertEqual(verify_manifest(manifest), [])

    def test_missing_namespace_is_reported_as_missing_field(self):
        from fs.memoryfs import MemoryFS

        memory_fs = MemoryFS()
        memory_fs.writetext("a.txt", "hello")
        # MemoryFS has no access namespace; permissions stay missing.
        backend = Backend(
            "mem", ["details", "access"], batch_cost=1,
            supports_consistency_proof=True,
        )
        manifest = collect(
            ["/a.txt"],
            fields=["size", "permissions"],
            backends=[backend],
            transport=FSInfoTransport({"mem": memory_fs}),
        )
        self.assertEqual(manifest.value("/a.txt", "size"), 5)
        self.assertEqual(manifest.missing("/a.txt"), ["permissions"])
        self.assertEqual(manifest.state("/a.txt"), "incomplete")


class TestManifest(unittest.TestCase):
    def _manifest(self):
        backend = Backend(
            "b", ["details", "access"], batch_cost=1, supports_consistency_proof=True
        )
        records = {
            "/a": {"size": 10, "permissions": "rw", "version": 3},
        }
        return collect(
            ["/a"],
            fields=["size", "permissions"],
            backends=[backend],
            transport=ScriptedTransport(records),
        )

    def test_every_field_traces_to_request_and_version(self):
        manifest = self._manifest()
        self.assertEqual(verify_manifest(manifest), [])
        trace = manifest.trace("/a", "size")
        self.assertEqual(trace["request"]["request_id"], trace["value"]["request_id"])
        self.assertEqual(trace["basis"]["path_version"], 3)
        self.assertEqual(
            trace["basis"]["versions"], {"/a": 3}
        )
        self.assertEqual(trace["value"]["backend"], "b")

    def test_plan_round_trip_preserves_exactness(self):
        backend = Backend(
            "one", ["details", "access"], batch_cost=2,
            supports_consistency_proof=True, version="v3", compatibility="c1",
        )
        original = plan(["/a"], fields=["size", "permissions"],
                        backends=[backend])
        restored = type(original).from_dict(original.to_dict())
        self.assertEqual(restored.to_dict(), original.to_dict())
        self.assertTrue(restored.exact)

    def test_all_cache_manifest_can_publish(self):
        backend = Backend(
            "det", ["details"], batch_cost=1, supports_consistency_proof=True
        )
        cache = [CacheEntry("/a", "size", 5, version=3, compatibility="1",
                            request_id="r9", backend="det")]
        manifest = collect(
            ["/a"],
            fields=["size"],
            backends=[backend],
            transport=ScriptedTransport({"/a": {}}),
            cache=cache,
        )
        self.assertEqual(manifest.state("/a"), "complete")
        self.assertEqual(manifest.publishable_record("/a"), {"size": 5})
        self.assertTrue(manifest.trace("/a", "size")["basis"]["cross_request"])

    def test_manifest_round_trip(self):
        manifest = self._manifest()
        restored = Manifest.from_dict(manifest.to_dict())
        self.assertEqual(restored.to_dict(), manifest.to_dict())
        self.assertEqual(verify_manifest(restored), [])
        self.assertEqual(restored.digest(), manifest.digest())
        self.assertEqual(
            restored.field("/a", "size").compatibility,
            manifest.field("/a", "size").compatibility,
        )

    def test_times_serialize_to_json(self):
        from fs.tempfs import TempFS

        with TempFS() as temp_fs:
            temp_fs.writetext("a.txt", "hello")
            backend = Backend(
                "temp",
                ["details"],
                batch_cost=1,
                supports_consistency_proof=True,
            )
            manifest = collect(
                ["/a.txt"],
                fields=["size", "modified"],
                backends=[backend],
                transport=FSInfoTransport({"temp": temp_fs}),
            )
        payload = manifest.dumps()
        self.assertIn("T", payload)
        restored = Manifest.from_dict(manifest.to_dict())
        self.assertEqual(restored.digest(), manifest.digest())

    def test_missing_field_is_reported(self):
        manifest = self._manifest()
        issues = verify_manifest(manifest, fields=["size", "created"])
        self.assertTrue(any("created is missing" in issue for issue in issues))

    def test_publishable_record_refuses_mixed_sources(self):
        backend = Backend("d", ["details"], batch_cost=1)
        access = Backend("a", ["access"], batch_cost=1)
        cache = [CacheEntry("/a", "size", 5, version=1, compatibility="1")]
        records = {"/a": {"permissions": "rw", "version": 1}}
        manifest = collect(
            ["/a"],
            fields=["size", "permissions"],
            backends=[backend, access],
            transport=ScriptedTransport(records),
            cache=cache,
        )
        self.assertIsNone(manifest.publishable_record("/a"))
        self.assertEqual(manifest.state("/a"), "independent")


if __name__ == "__main__":
    unittest.main()
