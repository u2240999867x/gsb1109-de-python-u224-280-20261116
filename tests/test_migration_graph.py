import base64
import os
import unittest

import LnkParse3
from LnkParse3.migration_graph import EdgeStatus
from LnkParse3.migration_graph import GuidRole
from LnkParse3.migration_graph import MigrationGraph
from LnkParse3.migration_graph import TrackingObservation


TARGET_DIR = os.path.join(os.path.dirname(__file__), "samples")

BV = "00000000-0000-0000-0000-0000000000B1"
BO = "00000000-0000-0000-0000-0000000000B2"
CV1 = "00000000-0000-0000-0000-0000000000C1"
CO1 = "00000000-0000-0000-0000-0000000000C2"
CV2 = "00000000-0000-0000-0000-0000000000D1"
CO2 = "00000000-0000-0000-0000-0000000000D2"


def observation(source, machine="pc-1", birth=(BV, BO), current=(CV1, CO1)):
    return TrackingObservation(
        source=source,
        machine_id=machine,
        birth_volume_guid=birth[0],
        birth_object_guid=birth[1],
        current_volume_guid=current[0],
        current_object_guid=current[1],
    )


class TestGuidRoles(unittest.TestCase):
    def test_four_roles_are_indexed_in_separate_sets(self):
        graph = MigrationGraph()
        shared = "11111111-2222-3333-4444-555555555555"
        graph.add(observation("a.lnk", birth=(shared, BO), current=(CV1, CO1)))
        graph.add(observation("b.lnk", birth=(BV, BO), current=(CV1, shared)))

        self.assertEqual(graph.sources_for_guid(GuidRole.BIRTH_VOLUME, shared), {"a.lnk"})
        self.assertEqual(graph.sources_for_guid(GuidRole.CURRENT_OBJECT, shared), {"b.lnk"})
        self.assertEqual(graph.sources_for_guid(GuidRole.BIRTH_OBJECT, shared), set())
        self.assertEqual(graph.sources_for_guid(GuidRole.CURRENT_VOLUME, shared), set())

    def test_same_guid_value_does_not_merge_across_roles(self):
        graph = MigrationGraph()
        shared = "11111111-2222-3333-4444-555555555555"
        # The same value as a volume GUID and as an object GUID must not
        # correlate two observations into one migration chain.
        graph.add(observation("a.lnk", birth=(shared, BO), current=(CV1, CO1)))
        graph.add(observation("b.lnk", birth=(BV, shared), current=(CV2, CO2)))

        view = graph.query("a.lnk")
        self.assertEqual(len(view.unconfirmed_migrations), 1)
        self.assertEqual(view.unconfirmed_migrations[0].sources, ["a.lnk"])


class TestTrackingGuarantee(unittest.TestCase):
    def test_birth_and_current_of_one_block_form_an_edge(self):
        graph = MigrationGraph()
        graph.add(observation("a.lnk"))

        edges = graph.edges_from(observation("x").birth)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0].current, observation("x").current)
        self.assertEqual(edges[0].status, EdgeStatus.CANDIDATE)

    def test_chain_forms_across_shortcuts_via_shared_identity(self):
        graph = MigrationGraph()
        graph.add(observation("a.lnk", birth=(BV, BO), current=(CV1, CO1)))
        # The second shortcut was born exactly where the first one ended.
        graph.add(observation("b.lnk", birth=(CV1, CO1), current=(CV2, CO2)))

        view = graph.query("a.lnk")
        claimed = [edge.current for edge in view.unconfirmed_migrations]
        self.assertIn(observation("x", current=(CV1, CO1)).current, claimed)
        self.assertIn(observation("x", current=(CV2, CO2)).current, claimed)

    def test_machine_name_does_not_fill_missing_object_identifier(self):
        graph = MigrationGraph()
        graph.add(observation("a.lnk", machine="same-pc", birth=(BV, None)))
        graph.add(observation("b.lnk", machine="same-pc", birth=(BV, None)))

        view_a = graph.query("a.lnk")
        view_b = graph.query("b.lnk")
        # Identities stay partial: the object GUID is not reconstructed
        # from the shared machine name.
        self.assertIsNone(view_a.original_identity.object_guid)
        self.assertFalse(view_a.original_identity.is_complete)
        # Partial identities never merge, so no chain links the two.
        self.assertEqual(len(view_a.unconfirmed_migrations), 1)
        self.assertEqual(view_a.unconfirmed_migrations[0].sources, ["a.lnk"])
        self.assertEqual(view_b.unconfirmed_migrations[0].sources, ["b.lnk"])


class TestForksAndConflicts(unittest.TestCase):
    def test_fork_keeps_all_candidates_without_arrival_order_winner(self):
        graph = MigrationGraph()
        graph.add(observation("late.lnk", current=(CV2, CO2)))
        graph.add(observation("early.lnk", current=(CV1, CO1)))

        view = graph.query("early.lnk")
        self.assertEqual(view.confirmed_migrations, [])
        candidates = {edge.current for edge in view.unconfirmed_migrations}
        self.assertEqual(
            candidates,
            {
                observation("x", current=(CV1, CO1)).current,
                observation("x", current=(CV2, CO2)).current,
            },
        )

    def test_verification_narrows_candidates_but_keeps_divergence(self):
        graph = MigrationGraph()
        graph.add(observation("a.lnk", current=(CV1, CO1)))
        graph.add(observation("b.lnk", current=(CV2, CO2)))

        graph.verify_target(BV, BO, CV1, CO1)

        view = graph.query("a.lnk")
        self.assertEqual(len(view.confirmed_migrations), 1)
        self.assertEqual(
            view.confirmed_migrations[0].current, observation("x", current=(CV1, CO1)).current
        )
        # The ruled-out branch is still recorded as divergence evidence.
        ruled_out = [e for e in view.unconfirmed_migrations if e.status is EdgeStatus.RULED_OUT]
        self.assertEqual(len(ruled_out), 1)
        self.assertEqual(ruled_out[0].current, observation("x", current=(CV2, CO2)).current)
        # Contradiction with the verified target is conflict evidence.
        self.assertEqual(len(view.conflicts), 1)
        self.assertEqual(
            view.conflicts[0].verified_current, observation("x", current=(CV1, CO1)).current
        )
        self.assertEqual(graph.conflicts, view.conflicts)

    def test_contradicting_observation_after_verification_is_flagged(self):
        graph = MigrationGraph()
        graph.add(observation("a.lnk", current=(CV1, CO1)))
        graph.verify_target(BV, BO, CV1, CO1)

        graph.add(observation("c.lnk", current=(CV2, CO2)))

        view = graph.query("c.lnk")
        self.assertEqual(len(view.conflicts), 1)
        self.assertEqual(view.unconfirmed_migrations[0].status, EdgeStatus.RULED_OUT)
        # The confirmed branch is untouched by the late contradiction.
        self.assertEqual(len(view.confirmed_migrations), 1)

    def test_verified_target_is_never_overwritten(self):
        graph = MigrationGraph()
        graph.verify_target(BV, BO, CV1, CO1)
        with self.assertRaises(ValueError):
            graph.verify_target(BV, BO, CV2, CO2)

    def test_repeated_contradiction_records_one_conflict(self):
        graph = MigrationGraph()
        graph.verify_target(BV, BO, CV1, CO1)
        graph.add(observation("c1.lnk", current=(CV2, CO2)))
        graph.add(observation("c2.lnk", current=(CV2, CO2)))

        self.assertEqual(len(graph.conflicts), 1)
        view = graph.query("c2.lnk")
        self.assertEqual(len(view.conflicts), 1)
        self.assertEqual(view.unconfirmed_migrations[0].sources, ["c1.lnk", "c2.lnk"])

    def test_ruled_out_edge_is_not_rewritten_by_later_material(self):
        graph = MigrationGraph()
        graph.add(observation("a.lnk", current=(CV1, CO1)))
        graph.add(observation("b.lnk", current=(CV2, CO2)))
        graph.verify_target(BV, BO, CV1, CO1)
        # Later material confirming the other branch must not resurrect or
        # erase the recorded divergence.
        graph.add(observation("c.lnk", current=(CV2, CO2)))

        view = graph.query("a.lnk")
        statuses = {
            edge.current.object_guid: edge.status
            for edge in view.confirmed_migrations + view.unconfirmed_migrations
        }
        self.assertEqual(statuses[CO1], EdgeStatus.CONFIRMED)
        self.assertEqual(statuses[CO2], EdgeStatus.RULED_OUT)


class TestQueryView(unittest.TestCase):
    def test_view_distinguishes_origin_current_and_unconfirmed(self):
        graph = MigrationGraph()
        graph.add(observation("a.lnk", machine="desktop-1", birth=(BV, BO), current=(CV1, CO1)))
        graph.add(observation("b.lnk", birth=(CV1, CO1), current=(CV2, CO2)))

        view = graph.query("a.lnk")
        self.assertEqual(view.original_identity.volume_guid, BV)
        self.assertEqual(view.original_identity.object_guid, BO)
        self.assertEqual(view.current_location.volume_guid, CV1)
        self.assertEqual(view.current_location.object_guid, CO1)
        self.assertEqual(view.machine_id, "desktop-1")
        # The continuation past the shortcut's own claim stays unconfirmed.
        self.assertEqual(view.confirmed_migrations, [])
        self.assertEqual(len(view.unconfirmed_migrations), 2)

    def test_unknown_source_raises(self):
        graph = MigrationGraph()
        with self.assertRaises(KeyError):
            graph.query("missing.lnk")

    def test_duplicate_source_is_rejected(self):
        graph = MigrationGraph()
        graph.add(observation("a.lnk"))
        with self.assertRaises(ValueError):
            graph.add(observation("a.lnk"))


class TestLnkIntegration(unittest.TestCase):
    def test_add_lnk_from_real_sample(self):
        with open(os.path.join(TARGET_DIR, "sample"), "rb") as indata:
            lnk = LnkParse3.lnk_file(indata=base64.b64decode(indata.read()))

        graph = MigrationGraph()
        obs = graph.add_lnk(lnk, source="sample")

        self.assertEqual(obs.machine_id, "desktop-eie2fiq")
        view = graph.query("sample")
        self.assertEqual(
            view.original_identity.volume_guid,
            "F12D3570-03C2-43E3-86FC-C75D680751B6",
        )
        self.assertEqual(
            view.current_location.object_guid,
            "43633D26-86A9-11EA-9C2D-B8AEED8E1A7A",
        )
        self.assertEqual(len(view.unconfirmed_migrations), 1)

    def test_add_tracker_from_dict(self):
        block = {
            "machine_identifier": "pc-9",
            "birth_droid_volume_identifier": BV,
            "birth_droid_file_identifier": BO,
            "droid_volume_identifier": CV1,
            "droid_file_identifier": CO1,
        }
        graph = MigrationGraph()
        graph.add_tracker(block, source="dict.lnk")
        self.assertEqual(graph.sources_for_guid(GuidRole.CURRENT_VOLUME, CV1), {"dict.lnk"})


if __name__ == "__main__":
    unittest.main()
