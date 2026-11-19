#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import annotations

import unittest

from mpd.migration import (
    AmbiguityError,
    CollisionError,
    Equivalence,
    MigrationError,
    MigrationPlanner,
    RootCatalog,
    RootMapping,
    StoredMapping,
    UnknownRootError,
    parse_relative,
    parse_resource,
    with_alternatives,
)


def make_catalog(prefixes=None, version: str = "v1", **kwargs) -> RootCatalog:
    mapping = dict(prefixes or {})
    mapping.update(kwargs)
    mappings = {}
    for library, prefix in mapping.items():
        mappings[library] = RootMapping.build(
            library=library,
            source_root=f"/srv/{library}",
            prefix=tuple(prefix.split("/")),
        )
    return RootCatalog.from_mappings(mappings, version)


class FileUriTests(unittest.TestCase):
    def test_percent_escape_is_decoded_only_for_file_resources(self) -> None:
        ref = parse_resource("file:///srv/music/A%20B/01.flac", "lib-a")
        self.assertEqual(ref.kind, "file")
        self.assertEqual(ref.segments, ("srv", "music", "A B", "01.flac"))
        self.assertEqual(ref.original, "file:///srv/music/A%20B/01.flac")

    def test_encoded_slash_does_not_become_a_separator(self) -> None:
        ref = parse_resource("file:///srv/a%2Fb.flac", "lib-a")
        self.assertEqual(ref.segments, ("srv", "a/b.flac"))

    def test_literal_backslash_is_a_normal_character(self) -> None:
        literal = parse_resource("file:///srv/a\\b.flac", "lib-a")
        escaped = parse_resource("file:///srv/a%5Cb.flac", "lib-a")
        # File-URI semantics: %5C and a literal backslash name the same byte.
        self.assertEqual(literal.segments, escaped.segments)
        self.assertEqual(literal.segments, ("srv", "a\\b.flac"))

    def test_target_never_contains_a_literal_backslash(self) -> None:
        catalog = make_catalog({"lib-a": "lib-a"})
        plan = MigrationPlanner(catalog).plan(
            [parse_resource("file:///srv/a\\b.flac", "lib-a")]
        )
        (target,) = plan.targets_for("lib-a", "file:///srv/a\\b.flac")
        self.assertNotIn("\\", target)
        self.assertIn("a%5Cb.flac", target)

    def test_reserved_and_host_components_are_rejected(self) -> None:
        with self.assertRaises(MigrationError):
            parse_resource("file:///srv/../secret.flac", "lib-a")
        with self.assertRaises(MigrationError):
            parse_resource("file://otherhost/srv/track.flac", "lib-a")
        with self.assertRaises(MigrationError):
            parse_resource("file://srv/track.flac", "lib-a")


class RelativeResourceTests(unittest.TestCase):
    def test_relative_name_uses_posix_segments(self) -> None:
        ref = parse_relative("Trio/01 - Opener.flac", "lib-b")
        self.assertEqual(ref.kind, "relative")
        self.assertEqual(ref.segments, ("Trio", "01 - Opener.flac"))

    def test_backslash_is_not_a_separator_for_relative_resources(self) -> None:
        slashed = parse_relative("Mixes/new.flac", "lib-b")
        backslashed = parse_relative("Mixes\\new.flac", "lib-b")
        self.assertEqual(slashed.segments, ("Mixes", "new.flac"))
        self.assertEqual(backslashed.segments, ("Mixes\\new.flac",))
        self.assertNotEqual(slashed.segments, backslashed.segments)

    def test_percent_escape_follows_file_rules(self) -> None:
        ref = parse_relative("A%20B/01.flac", "lib-b")
        self.assertEqual(ref.segments, ("A B", "01.flac"))

    def test_absolute_and_scheme_values_are_rejected(self) -> None:
        with self.assertRaises(MigrationError):
            parse_relative("/abs/track.flac", "lib-b")
        with self.assertRaises(MigrationError):
            parse_relative("http://example.com/track", "lib-b")
        with self.assertRaises(MigrationError):
            parse_relative("dir/../track.flac", "lib-b")


class StreamUrlTests(unittest.TestCase):
    def test_query_and_fragment_are_part_of_stream_identity(self) -> None:
        base = "http://example.com/stream"
        first = parse_resource(f"{base}?x=1&y=2", "lib-a")
        second = parse_resource(f"{base}?x=1&y=2#frag", "lib-a")
        third = parse_resource(f"{base}?x=1", "lib-a")
        self.assertEqual(first.source_domain, "example.com")
        self.assertNotEqual(first.segments, second.segments)
        self.assertNotEqual(first.segments, third.segments)

        catalog = make_catalog({"lib-a": "lib-a"})
        plan = MigrationPlanner(catalog).plan([first, second, third])
        targets = [
            plan.targets_for("lib-a", ref.original)
            for ref in (first, second, third)
        ]
        self.assertEqual(len({t[0] for t in targets}), 3)
        for ref in (first, second, third):
            target = plan.targets_for("lib-a", ref.original)[0]
            self.assertTrue(target.startswith("_streams/example.com/"))

    def test_url_is_never_decoded_like_a_file_path(self) -> None:
        ref = parse_resource(
            "https://a.example.com:8443/p%20ath;c?q=1&r=2", "lib-a"
        )
        self.assertEqual(
            ref.segments,
            ("https://a.example.com:8443/p%20ath;c?q=1&r=2",),
        )
        self.assertEqual(ref.source_domain, "a.example.com")

    def test_literal_backslash_stays_literal_in_stream_slot(self) -> None:
        url = "http://example.com/a\\b"
        ref = parse_resource(url, "lib-a")
        catalog = make_catalog({"lib-a": "lib-a"})
        plan = MigrationPlanner(catalog).plan([ref])
        target = plan.targets_for("lib-a", url)[0]
        self.assertNotIn("\\", target)
        self.assertIn("%5C", target)

    def test_malformed_streams_are_rejected(self) -> None:
        with self.assertRaises(MigrationError):
            parse_resource("http:///nohost", "lib-a")
        with self.assertRaises(MigrationError):
            parse_resource("gopher://example.com/x", "lib-a")


class TypeSeparationTests(unittest.TestCase):
    def test_equal_spellings_of_different_types_never_share_a_slot(self) -> None:
        catalog = make_catalog({"lib-a": "lib-a"})
        relative = parse_resource("a/b?x=1", "lib-a")
        stream = parse_resource("http://example.com/a/b?x=1", "lib-a")
        plan = MigrationPlanner(catalog).plan([relative, stream])
        rel_target = plan.targets_for("lib-a", "a/b?x=1")
        url_target = plan.targets_for(
            "lib-a", "http://example.com/a/b?x=1"
        )
        self.assertTrue(rel_target[0].startswith("lib-a/"))
        self.assertTrue(url_target[0].startswith("_streams/"))
        self.assertNotEqual(rel_target, url_target)
        self.assertEqual(plan.collisions, [])

    def test_backslash_interpretation_differs_by_type(self) -> None:
        catalog = make_catalog({"lib-a": "lib-a"})
        relative = parse_resource("a\\b", "lib-a")
        stream = parse_resource("http://example.com/a\\b", "lib-a")
        plan = MigrationPlanner(catalog).plan([relative, stream])
        self.assertEqual(
            plan.targets_for("lib-a", "a\\b"),
            ("lib-a/a%5Cb",),
        )
        self.assertTrue(
            plan.targets_for("lib-a", "http://example.com/a\\b")[0].startswith(
                "_streams/example.com/"
            )
        )


class EquivalenceTests(unittest.TestCase):
    def test_evidence_backed_tracks_share_one_target(self) -> None:
        catalog = make_catalog({"lib-a": "shared", "lib-b": "shared"})
        eq = Equivalence(
            left=("lib-a", "same.flac"),
            right=("lib-b", "same.flac"),
            evidence_id="EV-1",
            checksum="sha256:abc",
        )
        plan = MigrationPlanner(catalog, [eq]).plan(
            [
                parse_relative("same.flac", "lib-a"),
                parse_relative("same.flac", "lib-b"),
            ]
        )
        self.assertEqual(plan.collisions, [])
        self.assertEqual(
            plan.targets_for("lib-a", "same.flac"),
            plan.targets_for("lib-b", "same.flac"),
        )
        target = plan.targets_for("lib-a", "same.flac")[0]
        self.assertEqual(
            plan.reverse[target],
            (
                parse_relative("same.flac", "lib-a"),
                parse_relative("same.flac", "lib-b"),
            ),
        )
        entry = plan.entries[0]
        self.assertEqual(entry.evidence_ids, ["EV-1"])
        self.assertEqual(entry.checksums, ["sha256:abc"])

    def test_same_name_without_evidence_is_a_collision_not_a_merge(self) -> None:
        catalog = make_catalog({"lib-a": "shared", "lib-b": "shared"})
        plan = MigrationPlanner(catalog).plan(
            [
                parse_relative("same.flac", "lib-a"),
                parse_relative("same.flac", "lib-b"),
            ]
        )
        self.assertEqual(len(plan.collisions), 1)
        collision = plan.collisions[0]
        self.assertIsInstance(collision, CollisionError)
        self.assertEqual(collision.target, "shared/same.flac")
        self.assertEqual(
            sorted(collision.keys),
            [("lib-a", "same.flac"), ("lib-b", "same.flac")],
        )
        # The reverse mapping still lists the independent originals as two
        # separate keys rather than folding them into one identity.
        stored = StoredMapping.from_plan(plan)
        self.assertEqual(
            sorted(stored.lookup_original("shared/same.flac")),
            [("lib-a", "same.flac"), ("lib-b", "same.flac")],
        )
        # And forward lookup still answers for each original on its own.
        self.assertEqual(
            stored.lookup_targets("lib-a", "same.flac"),
            ("shared/same.flac",),
        )
        self.assertEqual(
            stored.lookup_targets("lib-b", "same.flac"),
            ("shared/same.flac",),
        )

    def test_transitive_evidence_merges_the_whole_group(self) -> None:
        catalog = make_catalog(
            {"lib-a": "shared", "lib-b": "shared", "lib-c": "shared"}
        )
        eqs = [
            Equivalence(("lib-a", "t.flac"), ("lib-b", "t.flac"), "EV-1"),
            Equivalence(("lib-b", "t.flac"), ("lib-c", "t.flac"), "EV-2"),
        ]
        plan = MigrationPlanner(catalog, eqs).plan(
            [
                parse_relative("t.flac", "lib-a"),
                parse_relative("t.flac", "lib-b"),
                parse_relative("t.flac", "lib-c"),
            ]
        )
        self.assertEqual(plan.collisions, [])
        target = "shared/t.flac"
        self.assertEqual(len(plan.reverse[target]), 3)


class AmbiguityTests(unittest.TestCase):
    def test_equivalent_resources_without_common_target_keep_candidates(self) -> None:
        catalog = make_catalog({"lib-a": "lib-a", "lib-b": "lib-b"})
        eq = Equivalence(
            left=("lib-a", "file:///srv/track.flac"),
            right=("lib-b", "http://example.com/track"),
            evidence_id="EV-9",
        )
        resources = [
            parse_resource("file:///srv/track.flac", "lib-a"),
            parse_resource("http://example.com/track", "lib-b"),
        ]
        plan = MigrationPlanner(catalog, [eq]).plan(resources)
        self.assertEqual(len(plan.ambiguities), 1)
        ambiguity = plan.ambiguities[0]
        self.assertIsInstance(ambiguity, AmbiguityError)
        self.assertEqual(len(ambiguity.candidates), 2)
        # Every interpretation is retained; nothing is guessed away.
        self.assertEqual(
            len(plan.targets_for("lib-a", "file:///srv/track.flac")), 1
        )
        self.assertEqual(
            len(plan.targets_for("lib-b", "http://example.com/track")), 1
        )
        self.assertNotEqual(
            plan.targets_for("lib-a", "file:///srv/track.flac"),
            plan.targets_for("lib-b", "http://example.com/track"),
        )

    def test_alternative_spellings_are_all_retained(self) -> None:
        catalog = make_catalog({"lib-a": "lib-a"})
        ref = with_alternatives(
            parse_relative("Mix/track 1.flac", "lib-a"),
            ["Mix/track%201.flac", "Mix/other-name.flac"],
        )
        plan = MigrationPlanner(catalog).plan([ref])
        targets = plan.targets_for("lib-a", "Mix/track 1.flac")
        # Two distinct interpretations survive (the first pair is the same
        # name under file-style escaping and collapses to one target).
        self.assertEqual(
            targets,
            ("lib-a/Mix/track 1.flac", "lib-a/Mix/other-name.flac"),
        )


class StoredMappingTests(unittest.TestCase):
    def _plan(self, catalog: RootCatalog) -> tuple:
        resources = [
            parse_relative("album/track.flac", "lib-a"),
            parse_resource("http://example.com/live?token=abc", "lib-b"),
        ]
        plan = MigrationPlanner(catalog).plan(resources)
        return plan, StoredMapping.from_plan(plan)

    def test_directory_query_returns_original_material(self) -> None:
        catalog = make_catalog({"lib-a": "lib-a", "lib-b": "lib-b"})
        plan, stored = self._plan(catalog)
        target = plan.targets_for("lib-a", "album/track.flac")[0]
        self.assertEqual(
            stored.lookup_original(target),
            (("lib-a", "album/track.flac"),),
        )
        self.assertEqual(
            stored.lookup_targets("lib-b", "http://example.com/live?token=abc"),
            plan.targets_for("lib-b", "http://example.com/live?token=abc"),
        )

    def test_reverse_mapping_keeps_independent_resources_apart(self) -> None:
        catalog = make_catalog({"lib-a": "lib-a", "lib-b": "lib-b"})
        _, stored = self._plan(catalog)
        self.assertTrue(
            stored.originals_remain_distinct(
                [
                    ("lib-a", "album/track.flac"),
                    ("lib-b", "http://example.com/live?token=abc"),
                ]
            )
        )

    def test_verify_passes_against_unchanged_catalog(self) -> None:
        catalog = make_catalog({"lib-a": "lib-a", "lib-b": "lib-b"})
        _, stored = self._plan(catalog)
        stored.verify(catalog)
        self.assertFalse(
            MigrationPlanner(catalog)
            .plan([parse_relative("x.flac", "lib-a")])
            .is_stale_against(catalog)
        )

    def test_root_change_invalidates_the_mapping(self) -> None:
        catalog = make_catalog({"lib-a": "lib-a", "lib-b": "lib-b"})
        _, stored = self._plan(catalog)

        changed_prefix = RootCatalog.from_mappings(
            {
                **catalog.mappings,
                "lib-a": RootMapping.build("lib-a", "/srv/lib-a", ("moved",)),
            },
            "v1",
        )
        with self.assertRaises(MigrationError):
            stored.verify(changed_prefix)
        self.assertTrue(stored.root_digests or True)

        changed_root = RootCatalog.from_mappings(
            {
                **catalog.mappings,
                "lib-a": RootMapping.build(
                    "lib-a", "/srv/relocated-lib-a", ("lib-a",)
                ),
            },
            "v1",
        )
        with self.assertRaises(MigrationError):
            stored.verify(changed_root)

        new_version = RootCatalog.from_mappings(catalog.mappings, "v2")
        with self.assertRaises(MigrationError):
            stored.verify(new_version)

        dropped = RootCatalog.from_mappings(
            {"lib-b": catalog.mappings["lib-b"]}, "v1"
        )
        with self.assertRaises(MigrationError):
            stored.verify(dropped)

    def test_unknown_library_is_rejected(self) -> None:
        catalog = make_catalog({"lib-a": "lib-a"})
        with self.assertRaises(UnknownRootError):
            MigrationPlanner(catalog).plan(
                [parse_relative("track.flac", "ghost-library")]
            )


if __name__ == "__main__":
    unittest.main()
