import base64
import importlib
import json
import os
import unittest

from LnkParse3.reconcile import Catalog
from LnkParse3.reconcile import reconcile


reconcile_module = importlib.import_module("LnkParse3.reconcile")


SAMPLE_DIR = os.path.join(os.path.dirname(__file__), "samples")


def catalog(machines, shortcuts, **constraints):
    data = {"machines": machines, "shortcuts": shortcuts}
    data.update(constraints)
    return Catalog.from_dict(data)


def machine(machine_id, *volume_ids):
    return {
        "id": machine_id,
        "volumes": [{"id": volume_id, "drive": "C:"} for volume_id in volume_ids],
    }


def shortcut(shortcut_id, machine_id, volume_id, path):
    return {
        "id": shortcut_id,
        "machine": machine_id,
        "volume": volume_id,
        "path": path,
    }


class TestCatalog(unittest.TestCase):
    def test_rejects_duplicate_volume(self):
        with self.assertRaises(ValueError):
            catalog(
                [
                    {"id": "A", "volumes": [{"id": "v"}]},
                    {"id": "A", "volumes": [{"id": "w"}]},
                ],
                [],
            )

    def test_rejects_unknown_shortcut_volume(self):
        with self.assertRaises(ValueError):
            catalog([machine("A", "v")], [shortcut("s", "A", "missing", "C:\\x")])

    def test_rejects_ambiguous_volume_reference(self):
        with self.assertRaises(ValueError):
            Catalog.from_dict(
                {
                    "machines": [machine("A", "v"), machine("B", "v")],
                    "shortcuts": [
                        {
                            "id": "s",
                            "volume": "v",
                            "path": "C:\\x",
                        }
                    ],
                }
            )

    def test_round_trip(self):
        original = catalog(
            [machine("A", "v"), machine("B", "w")],
            [shortcut("a", "A", "v", "C:\\x"), shortcut("b", "B", "w", "C:\\x")],
            candidate_volume_mappings=[{"id": "c", "a": "A:v", "b": "B:w"}],
            verified_objects=[{"id": "o", "members": ["a", "b"]}],
        )
        restored = Catalog.from_dict(json.loads(json.dumps(original.to_dict())))
        self.assertEqual(set(restored.volumes), {("A", "v"), ("B", "w")})
        self.assertEqual(set(restored.shortcuts), {"a", "b"})
        self.assertEqual(len(restored.candidate_mappings), 1)
        self.assertEqual(len(restored.verified_objects), 1)


class TestVolumeReconciliation(unittest.TestCase):
    def test_single_candidate_gives_one_plan(self):
        result = reconcile(
            catalog(
                [machine("A", "v"), machine("B", "w")],
                [shortcut("a", "A", "v", "C:\\x")],
                candidate_volume_mappings=[{"id": "c", "a": "A:v", "b": "B:w"}],
            )
        )
        self.assertTrue(result.complete)
        self.assertEqual(result.plan_count, 1)
        self.assertEqual(len(result.confirmed_volume_relations), 1)
        members = result.confirmed_volume_relations[0].members
        self.assertEqual(
            {(member["machine"], member["volume"]) for member in members},
            {("A", "v"), ("B", "w")},
        )

    def test_ambiguous_candidate_yields_two_plans_and_no_relation(self):
        result = reconcile(
            catalog(
                [machine("A", "v"), {"id": "B", "volumes": [{"id": "w1"}, {"id": "w2"}]}],
                [],
                candidate_volume_mappings=[
                    {"id": "c1", "a": "A:v", "b": "B:w1"},
                    {"id": "c2", "a": "A:v", "b": "B:w2"},
                ],
            )
        )
        self.assertTrue(result.complete)
        self.assertEqual(result.plan_count, 2)
        self.assertEqual(result.confirmed_volume_relations, ())

    def test_unrelated_components_multiply_plans(self):
        result = reconcile(
            catalog(
                [
                    machine("A", "v"),
                    machine("B", "w1", "w2"),
                    machine("C", "x"),
                    machine("D", "y1", "y2"),
                ],
                [],
                candidate_volume_mappings=[
                    {"id": "c1", "a": "A:v", "b": "B:w1"},
                    {"id": "c2", "a": "A:v", "b": "B:w2"},
                    {"id": "c3", "a": "C:x", "b": "D:y1"},
                    {"id": "c4", "a": "C:x", "b": "D:y2"},
                ],
            )
        )
        self.assertEqual(result.plan_count, 4)

    def test_candidate_inside_one_machine_is_never_used(self):
        result = reconcile(
            catalog(
                [machine("A", "v1", "v2")],
                [],
                candidate_volume_mappings=[{"id": "c", "a": "A:v1", "b": "A:v2"}],
            )
        )
        self.assertTrue(result.complete)
        self.assertEqual(
            [warning["kind"] for warning in result.warnings],
            ["candidate_same_machine"],
        )
        self.assertEqual(result.confirmed_volume_relations, ())

    def test_candidate_contradicted_by_distinct_volumes_is_never_used(self):
        result = reconcile(
            catalog(
                [machine("A", "v"), machine("B", "w")],
                [],
                candidate_volume_mappings=[{"id": "c", "a": "A:v", "b": "B:w"}],
                distinct_volumes=[{"id": "d", "a": "A:v", "b": "B:w"}],
            )
        )
        self.assertTrue(result.complete)
        self.assertEqual(
            [warning["kind"] for warning in result.warnings],
            ["candidate_contradicts_distinct"],
        )
        self.assertEqual(result.confirmed_volume_relations, ())

    def test_distinct_volume_blocks_merge(self):
        result = reconcile(
            catalog(
                [machine("A", "v"), machine("B", "w")],
                [],
                candidate_volume_mappings=[
                    {"id": "c", "a": "A:v", "b": "B:w"},
                    {"id": "c2", "a": "A:v", "b": "B:w"},
                ],
                distinct_volumes=[{"id": "d", "a": "A:v", "b": "B:w"}],
            )
        )
        self.assertTrue(result.complete)
        self.assertEqual(result.plan_count, 1)

    def test_mutual_exclusion_across_verified_objects(self):
        result = reconcile(
            catalog(
                [
                    machine("A", "v1", "v2"),
                    machine("B", "w"),
                ],
                [
                    shortcut("a1", "A", "v1", "C:\\one"),
                    shortcut("a2", "A", "v2", "C:\\two"),
                    shortcut("b", "B", "w", "C:\\three"),
                ],
                verified_objects=[
                    {"id": "o1", "members": ["a1", "b"]},
                    {"id": "o2", "members": ["a2", "b"]},
                ],
            )
        )
        self.assertFalse(result.complete)
        self.assertEqual(result.plan_count, 0)
        kinds = {conflict.kind for conflict in result.groups[0].conflicts}
        self.assertIn("mutual_exclusion", kinds)
        conflict = next(
            item for item in result.groups[0].conflicts if item.kind == "mutual_exclusion"
        )
        self.assertTrue(conflict.records)
        self.assertTrue(conflict.constraints)


class TestTargets(unittest.TestCase):
    def test_equal_paths_alone_do_not_create_identity(self):
        result = reconcile(
            catalog(
                [machine("A", "v"), machine("B", "w")],
                [
                    shortcut("a", "A", "v", "C:\\Users\\x\\a.txt"),
                    shortcut("b", "B", "w", "C:\\Users\\x\\a.txt"),
                ],
                candidate_volume_mappings=[{"id": "c", "a": "A:v", "b": "B:w"}],
            )
        )
        self.assertEqual(result.confirmed_target_relations, ())
        self.assertEqual(len(result.confirmed_volume_relations), 1)

    def test_equal_labels_and_drives_alone_do_not_merge(self):
        result = reconcile(
            catalog(
                [
                    {"id": "A", "volumes": [{"id": "v", "label": "OS", "drive": "C:"}]},
                    {"id": "B", "volumes": [{"id": "w", "label": "OS", "drive": "C:"}]},
                ],
                [],
            )
        )
        self.assertTrue(result.complete)
        self.assertEqual(result.confirmed_volume_relations, ())

    def test_verified_object_confirms_target(self):
        result = reconcile(
            catalog(
                [machine("A", "v"), machine("B", "w")],
                [
                    shortcut("a", "A", "v", "C:\\Users\\x\\a.txt"),
                    shortcut("b", "B", "w", "D:\\other\\a.txt"),
                ],
                verified_objects=[{"id": "o", "members": ["a", "b"]}],
            )
        )
        self.assertEqual(len(result.confirmed_volume_relations), 1)
        self.assertEqual(len(result.confirmed_target_relations), 1)
        members = result.confirmed_target_relations[0].members
        self.assertEqual(
            {(member["machine"], member["shortcut"]) for member in members},
            {("A", "a"), ("B", "b")},
        )

    def test_verified_object_requires_one_volume_per_machine(self):
        result = reconcile(
            catalog(
                [machine("A", "v1", "v2")],
                [
                    shortcut("a1", "A", "v1", "C:\\one"),
                    shortcut("a2", "A", "v2", "C:\\two"),
                ],
                verified_objects=[{"id": "o", "members": ["a1", "a2"]}],
            )
        )
        self.assertFalse(result.complete)
        kinds = {conflict.kind for conflict in result.groups[0].conflicts}
        self.assertEqual(kinds, {"mutual_exclusion"})

    def test_distinct_target_constraint_conflicts_with_verified_object(self):
        result = reconcile(
            catalog(
                [machine("A", "v"), machine("B", "w")],
                [
                    shortcut("a", "A", "v", "C:\\x"),
                    shortcut("b", "B", "w", "C:\\x"),
                ],
                verified_objects=[{"id": "o", "members": ["a", "b"]}],
                distinct_targets=[{"id": "d", "a": "a", "b": "b"}],
            )
        )
        self.assertFalse(result.complete)
        kinds = {conflict.kind for conflict in result.groups[0].conflicts}
        self.assertIn("distinct_target_conflict", kinds)

    def test_distinct_targets_keep_matching_paths_on_separate_volumes(self):
        result = reconcile(
            catalog(
                [machine("A", "v"), machine("B", "w")],
                [
                    shortcut("a", "A", "v", "C:\\x"),
                    shortcut("b", "B", "w", "C:\\x"),
                ],
                candidate_volume_mappings=[{"id": "c", "a": "A:v", "b": "B:w"}],
                distinct_targets=[{"id": "d", "a": "a", "b": "b"}],
            )
        )
        self.assertTrue(result.complete)
        self.assertEqual(result.plan_count, 1)
        self.assertEqual(result.confirmed_volume_relations, ())

    def test_paths_preserve_each_machine_spelling(self):
        result = reconcile(
            catalog(
                [machine("A", "v"), machine("B", "w")],
                [
                    shortcut("a", "A", "v", "C:\\Users\\alice\\a.txt"),
                    shortcut("b", "B", "w", "D:\\backup\\a.txt"),
                ],
                verified_objects=[{"id": "o", "members": ["a", "b"]}],
            )
        )
        members = result.confirmed_target_relations[0].members
        self.assertEqual(
            {member["machine"]: member["path"] for member in members},
            {"A": "C:\\Users\\alice\\a.txt", "B": "D:\\backup\\a.txt"},
        )


class TestIndependentGroups(unittest.TestCase):
    def test_conflict_does_not_hide_independent_group(self):
        result = reconcile(
            catalog(
                [
                    machine("A", "v1", "v2"),
                    machine("B", "w"),
                    machine("Z", "z"),
                ],
                [
                    shortcut("a1", "A", "v1", "C:\\one"),
                    shortcut("a2", "A", "v2", "C:\\two"),
                    shortcut("b", "B", "w", "C:\\three"),
                    shortcut("z", "Z", "z", "C:\\four"),
                ],
                verified_objects=[
                    {"id": "o1", "members": ["a1", "b"]},
                    {"id": "o2", "members": ["a2", "b"]},
                ],
            )
        )
        self.assertFalse(result.complete)
        self.assertEqual(len(result.unsatisfiable_groups), 1)
        solved = [group for group in result.groups if not group.conflicts]
        self.assertEqual(len(solved), 1)
        self.assertEqual(solved[0].status, "solved")

    def test_documented_example(self):
        result = reconcile(
            catalog(
                [
                    {"id": "laptop", "volumes": [{"id": "os", "label": "Windows", "drive": "C:"}]},
                    {
                        "id": "desktop",
                        "volumes": [
                            {"id": "sys", "label": "Windows", "drive": "C:"},
                            {"id": "data", "label": "Backup", "drive": "D:"},
                        ],
                    },
                ],
                [
                    shortcut(
                        "laptop-report",
                        "laptop",
                        "os",
                        "C:\\Users\\ana\\report.docx",
                    ),
                    shortcut(
                        "desktop-report",
                        "desktop",
                        "data",
                        "D:\\restore\\report.docx",
                    ),
                ],
                candidate_volume_mappings=[
                    {"id": "maybe-os", "a": "laptop:os", "b": "desktop:sys"},
                    {"id": "maybe-data", "a": "laptop:os", "b": "desktop:data"},
                ],
                verified_objects=[
                    {"id": "same-report", "members": ["laptop-report", "desktop-report"]}
                ],
            )
        )
        self.assertTrue(result.complete)
        self.assertEqual(result.plan_count, 1)
        self.assertEqual(
            [
                {(member["machine"], member["volume"]) for member in relation.members}
                for relation in result.confirmed_volume_relations
            ],
            [{("laptop", "os"), ("desktop", "data")}],
        )
        self.assertEqual(len(result.confirmed_target_relations), 1)

    def test_search_limit_is_reported_as_conflict(self):
        result = reconcile(
            catalog(
                [machine("A", "v"), machine("B", "w1", "w2", "w3")],
                [],
                candidate_volume_mappings=[
                    {"id": "c1", "a": "A:v", "b": "B:w1"},
                    {"id": "c2", "a": "A:v", "b": "B:w2"},
                    {"id": "c3", "a": "A:v", "b": "B:w3"},
                    {"id": "c4", "a": "A:v", "b": "B:w1"},
                ],
            ),
            max_states=2,
        )
        self.assertFalse(result.complete)
        self.assertEqual(result.groups[0].conflicts[0].kind, "search_limit")
        self.assertIsInstance(result.groups[0].conflicts[0], reconcile_module.Conflict)

    def test_plan_materialisation_limit_keeps_relations(self):
        result = reconcile(
            catalog(
                [machine("A", "v"), machine("B", "w")],
                [],
                candidate_volume_mappings=[{"id": "c", "a": "A:v", "b": "B:w"}],
            ),
            max_plans=0,
        )
        self.assertTrue(result.complete)
        self.assertTrue(result.groups[0].plans_omitted)
        self.assertEqual(len(result.confirmed_volume_relations), 1)

    def test_all_feasible_includes_separate_assignment(self):
        result = reconcile(
            catalog(
                [machine("A", "v"), machine("B", "w")],
                [],
                candidate_volume_mappings=[{"id": "c", "a": "A:v", "b": "B:w"}],
            ),
            maximal=False,
        )
        self.assertEqual(result.plan_count, 2)
        self.assertEqual(result.confirmed_volume_relations, ())

    def test_maximal_never_leaves_a_candidate_unused(self):
        result = reconcile(
            catalog(
                [machine("A", "v"), machine("B", "w")],
                [],
                candidate_volume_mappings=[{"id": "c", "a": "A:v", "b": "B:w"}],
            )
        )
        self.assertEqual(result.plan_count, 1)


class TestSampleIntegration(unittest.TestCase):
    def test_real_samples_preserve_machine_paths(self):
        records = []
        for name, machine_id in [
            ("console_properties_block", "leeholm16"),
            ("microsoft_example", "chris-xps"),
        ]:
            path = os.path.join(SAMPLE_DIR, name)
            with open(path, "rb") as handle:
                raw = base64.b64decode(handle.read())
            records.append((machine_id, name, raw))
        catalog_data = Catalog.from_lnk_records(
            [
                (machine_id, shortcut_id, _sample_record(payload))
                for machine_id, shortcut_id, payload in records
            ]
        )
        self.assertEqual(
            {volume.machine for volume in catalog_data.volumes.values()},
            {"leeholm16", "chris-xps"},
        )
        paths = {shortcut.path for shortcut in catalog_data.shortcuts.values()}
        self.assertIn("C:\\Windows\\SysWOW64\\WindowsPowerShell\\v1.0\\powershell.exe", paths)
        self.assertIn("C:\\test\\a.txt", paths)
        result = reconcile(catalog_data)
        self.assertTrue(result.complete)


def _sample_record(payload):
    from LnkParse3 import lnk_file

    return lnk_file(indata=payload).get_json()


if __name__ == "__main__":
    unittest.main()
