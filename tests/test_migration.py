import json
import unittest

from LnkParse3.migration import build_plan
from LnkParse3.migration import declarations_from_lnk_json
from LnkParse3.migration import render_plan_text


GUID_DOWNLOADS = "{374DE290-123F-4565-9164-39C4925E467B}"


def base_document():
    return {
        "source": {
            "name": "PC-A",
            "env": {
                "SystemRoot": "C:\\Windows",
                "USERNAME": "alice",
                "PROFILE": "C:\\Users\\%USERNAME%",
                "APPDATA": "%PROFILE%\\AppData\\Roaming",
            },
            "known_folders": {
                GUID_DOWNLOADS: "C:\\Users\\alice\\Downloads",
            },
            "drives": ["C:", "D:"],
        },
        "target": {
            "name": "PC-B",
            "env": {
                "SystemRoot": "E:\\Windows",
                "USERNAME": "bob",
                "PROFILE": "E:\\Profiles\\%USERNAME%",
                "APPDATA": "%PROFILE%\\AppData\\Roaming",
            },
            "known_folders": {
                GUID_DOWNLOADS: "E:\\Profiles\\bob\\Downloads",
            },
            "drives": ["E:"],
        },
        "migrations": [
            {"id": "docs", "source": "D:\\Work\\Docs", "target": "E:\\Data\\Docs", "type": "dir"},
            {
                "id": "roaming",
                "source": "C:\\Users\\alice\\AppData\\Roaming",
                "target": "E:\\Data\\Roaming",
                "type": "dir",
            },
        ],
        "allowed_fields": [
            "relative_path",
            "working_directory",
            "icon_location",
            "link_info.local_base_path",
            "link_info.common_path_suffix",
            "extra.ENVIRONMENTAL_VARIABLES_LOCATION_BLOCK.target_unicode",
        ],
        "shortcuts": [],
    }


def plan_for(shortcuts, document=None):
    document = document or base_document()
    document["shortcuts"] = shortcuts
    return build_plan(document)


def updates_for(plan, shortcut=None):
    return [
        update
        for update in plan["plan"]["updates"]
        if shortcut is None or update["shortcut"] == shortcut
    ]


class TestVariableExpansion(unittest.TestCase):
    def test_indirect_variable_chain_expands_transitively(self):
        document = base_document()
        document["shortcuts"] = [
            {
                "id": "chain",
                "dir": "D:\\Work\\Docs",
                "declarations": [
                    {"field": "working_directory", "value": "%APPDATA%\\App"},
                ],
            }
        ]
        plan = build_plan(document)
        self.assertEqual(plan["variables"]["missing"], [])
        self.assertEqual(plan["variables"]["cycles"], [])
        update = updates_for(plan)[0]
        self.assertEqual(update["after"], "E:\\Data\\Roaming\\App")

    def test_missing_variable_is_reported_and_declaration_kept(self):
        document = base_document()
        document["source"]["env"]["ALIAS"] = "%NOT_DEFINED%\\sub"
        document["shortcuts"] = [
            {
                "id": "missing",
                "dir": "D:\\Work\\Docs",
                "declarations": [
                    {"field": "working_directory", "value": "%ALIAS%"},
                ],
            }
        ]
        plan = build_plan(document)
        missing = [item for item in plan["variables"]["missing"] if item["machine"] == "PC-A"]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["chain"], ["ALIAS", "NOT_DEFINED"])
        self.assertEqual(plan["plan"]["updates"], [])
        retained = plan["plan"]["retained"]
        self.assertEqual(retained[0]["value"], "%ALIAS%")
        self.assertEqual(retained[0]["reason"], "unresolved_source")
        self.assertEqual(plan["status"], "needs_confirmation")

    def test_cycle_is_reported_separately_from_missing(self):
        document = base_document()
        document["source"]["env"]["A"] = "%B%"
        document["source"]["env"]["B"] = "%A%"
        document["shortcuts"] = [
            {
                "id": "cycle",
                "dir": "D:\\Work\\Docs",
                "declarations": [
                    {"field": "icon_location", "value": "%A%\\x.ico"},
                ],
            }
        ]
        plan = build_plan(document)
        self.assertEqual(
            [item for item in plan["variables"]["missing"] if item["machine"] == "PC-A"], []
        )
        cycles = plan["variables"]["cycles"]
        self.assertTrue(any(item["chain"] == ["A", "B", "A"] for item in cycles))
        self.assertEqual(plan["plan"]["updates"], [])
        self.assertEqual(plan["plan"]["retained"][0]["reason"], "unresolved_source")

    def test_target_machine_missing_variable_is_reported(self):
        document = base_document()
        document["shortcuts"] = [
            {
                "id": "target-missing",
                "dir": "D:\\Work\\Docs",
                "declarations": [
                    {"field": "working_directory", "value": "%ONLY_ON_TARGET%\\x"},
                ],
            }
        ]
        document["target"]["env"]["ONLY_ON_TARGET"] = "%NOT_THERE%\\x"
        plan = build_plan(document)
        target_missing = [
            item for item in plan["variables"]["missing"] if item["machine"] == "PC-B"
        ]
        self.assertTrue(target_missing)

    def test_variable_names_are_case_insensitive(self):
        document = base_document()
        document["shortcuts"] = [
            {
                "id": "case",
                "dir": "D:\\Work\\Docs",
                "declarations": [
                    {"field": "working_directory", "value": "%systemroot%\\Temp"},
                ],
            }
        ]
        plan = build_plan(document)
        self.assertEqual(plan["variables"]["missing"], [])
        self.assertEqual(updates_for(plan), [])


class TestKnownFolder(unittest.TestCase):
    def test_known_folder_with_suffix_joins_relative_segments(self):
        document = base_document()
        document["shortcuts"] = [
            {
                "id": "kf",
                "dir": "D:\\Work\\Docs",
                "declarations": [
                    {
                        "field": "extra.ENVIRONMENTAL_VARIABLES_LOCATION_BLOCK.target_unicode",
                        "value": GUID_DOWNLOADS + "\\reports\\a.txt",
                    }
                ],
            }
        ]
        plan = build_plan(document)
        unchanged = plan["plan"]["unchanged"][0]
        self.assertEqual(unchanged["resolves_to"], "E:\\Profiles\\bob\\Downloads\\reports\\a.txt")
        self.assertEqual(unchanged["reason"], "already_resolved")

    def test_known_folder_suffix_must_not_become_a_second_root(self):
        document = base_document()
        document["shortcuts"] = [
            {
                "id": "kf-bad",
                "dir": "D:\\Work\\Docs",
                "declarations": [
                    {
                        "field": "extra.ENVIRONMENTAL_VARIABLES_LOCATION_BLOCK.target_unicode",
                        "value": GUID_DOWNLOADS + "\\E:\\Elsewhere\\a.txt",
                    }
                ],
            }
        ]
        plan = build_plan(document)
        retained = plan["plan"]["retained"][0]
        codes = [fault["code"] for fault in retained["faults"]]
        self.assertIn("absolute_known_folder_suffix", codes)
        self.assertEqual(plan["plan"]["updates"], [])

    def test_known_folder_guid_is_case_insensitive(self):
        document = base_document()
        document["shortcuts"] = [
            {
                "id": "kf-case",
                "dir": "D:\\Work\\Docs",
                "declarations": [
                    {
                        "field": "extra.ENVIRONMENTAL_VARIABLES_LOCATION_BLOCK.target_unicode",
                        "value": GUID_DOWNLOADS.lower() + "\\a.txt",
                    }
                ],
            }
        ]
        plan = build_plan(document)
        self.assertEqual(plan["variables"]["missing"], [])
        self.assertEqual(plan["plan"]["updates"], [])


class TestMinimalCorrections(unittest.TestCase):
    def test_only_the_stale_field_changes(self):
        plan = plan_for(
            [
                {
                    "id": "stale",
                    "dir": "D:\\Work\\Docs",
                    "declarations": [
                        {"field": "relative_path", "value": ".\\a.txt", "base": "shortcut_dir"},
                        {"field": "working_directory", "value": "D:\\Work\\Docs"},
                        {"field": "icon_location", "value": "%SystemRoot%\\system32\\shell32.dll"},
                    ],
                }
            ]
        )
        updates = updates_for(plan)
        self.assertEqual([update["field"] for update in updates], ["working_directory"])
        self.assertEqual(updates[0]["before"], "D:\\Work\\Docs")
        self.assertEqual(updates[0]["after"], "E:\\Data\\Docs")
        self.assertEqual(plan["plan"]["changed_field_count"], 1)

    def test_link_info_base_moves_but_keeps_suffix_when_it_still_applies(self):
        plan = plan_for(
            [
                {
                    "id": "linkinfo",
                    "dir": "D:\\Work\\Docs",
                    "declarations": [
                        {
                            "field": "link_info.local_base_path",
                            "value": "D:\\Work\\Docs",
                            "suffix": "a.txt",
                        }
                    ],
                }
            ]
        )
        updates = updates_for(plan)
        self.assertEqual(len(updates), 1)
        update = updates[0]
        self.assertEqual(update["after"], "E:\\Data\\Docs")
        self.assertTrue(update["keeps_suffix"])
        self.assertEqual(len(update["edits"]), 1)
        self.assertEqual(update["resolves_to"], "E:\\Data\\Docs\\a.txt")

    def test_relative_path_is_rebased_on_shortcut_directory(self):
        plan = plan_for(
            [
                {
                    "id": "relative",
                    "dir": "D:\\Work\\Docs\\sub",
                    "declarations": [
                        {"field": "relative_path", "value": "..\\app.exe", "base": "shortcut_dir"},
                    ],
                }
            ]
        )
        # The same relative expression resolves correctly because the shortcut
        # directory moved with the material; no field change is needed.
        self.assertEqual(plan["plan"]["updates"], [])
        unchanged = plan["plan"]["unchanged"][0]
        self.assertEqual(unchanged["value"], "..\\app.exe")
        self.assertEqual(unchanged["resolves_to"], "E:\\Data\\Docs\\app.exe")

    def test_field_not_allowed_is_never_rewritten(self):
        document = base_document()
        document["allowed_fields"] = ["relative_path"]
        plan = plan_for(
            [
                {
                    "id": "locked",
                    "dir": "D:\\Work\\Docs",
                    "declarations": [
                        {"field": "working_directory", "value": "D:\\Work\\Docs"},
                    ],
                }
            ],
            document,
        )
        self.assertEqual(plan["plan"]["updates"], [])
        self.assertEqual(plan["plan"]["retained"][0]["reason"], "field_not_allowed")


class TestConflicts(unittest.TestCase):
    def test_conflicting_declarations_are_deferred_not_rewritten(self):
        plan = plan_for(
            [
                {
                    "id": "diverges",
                    "dir": "D:\\Work\\Docs",
                    "declarations": [
                        {
                            "field": "relative_path",
                            "value": "%APPDATA%\\app.exe",
                            "base": "shortcut_dir",
                        },
                        {
                            "field": "icon_location",
                            "value": "C:\\Users\\alice\\AppData\\Roaming\\app.exe",
                        },
                    ],
                }
            ]
        )
        self.assertEqual(plan["plan"]["updates"], [])
        self.assertEqual(len(plan["conflicts"]), 1)
        self.assertEqual(plan["conflicts"][0]["reason"], "post_migration_conflict")
        reasons = {item["reason"] for item in plan["plan"]["retained"]}
        self.assertEqual(reasons, {"post_migration_conflict"})
        for item in plan["plan"]["retained"]:
            self.assertIn(
                item["value"],
                {
                    "%APPDATA%\\app.exe",
                    "C:\\Users\\alice\\AppData\\Roaming\\app.exe",
                },
            )

    def test_disagreeing_source_paths_for_same_slot_are_deferred(self):
        document = base_document()
        document["shortcuts"] = [
            {
                "id": "two-targets",
                "dir": "D:\\Work\\Docs",
                "declarations": [
                    {"field": "relative_path", "value": ".\\a.txt", "base": "shortcut_dir"},
                    {
                        "field": "link_info.local_base_path",
                        "value": "D:\\Work\\Docs\\other",
                    },
                ],
            }
        ]
        plan = build_plan(document)
        self.assertEqual(plan["plan"]["updates"], [])
        self.assertEqual(plan["conflicts"][0]["reason"], "source_conflict")

    def test_clean_conflict_status_lists_no_conflicts(self):
        plan = plan_for(
            [
                {
                    "id": "fine",
                    "dir": "D:\\Work\\Docs",
                    "declarations": [
                        {"field": "relative_path", "value": ".\\a.txt", "base": "shortcut_dir"},
                    ],
                }
            ]
        )
        self.assertEqual(plan["conflicts"], [])
        self.assertEqual(plan["status"], "ok")

    def test_ambiguous_migration_is_reported(self):
        document = base_document()
        document["migrations"] = [
            {"id": "one", "source": "D:\\Work\\Docs", "target": "E:\\One", "type": "dir"},
            {"id": "two", "source": "D:\\Work\\Docs", "target": "E:\\Two", "type": "dir"},
        ]
        plan = plan_for(
            [
                {
                    "id": "amb",
                    "dir": "D:\\Work\\Docs",
                    "declarations": [{"field": "working_directory", "value": "D:\\Work\\Docs"}],
                }
            ],
            document,
        )
        self.assertEqual(plan["plan"]["updates"], [])
        self.assertEqual(plan["plan"]["retained"][0]["reason"], "mapping_ambiguous")
        self.assertEqual(plan["status"], "needs_confirmation")

    def test_target_collision_defers_reverse_mapping(self):
        document = base_document()
        document["migrations"] = [
            {"id": "one", "source": "D:\\Work\\One", "target": "E:\\Shared", "type": "dir"},
            {"id": "two", "source": "D:\\Work\\Two", "target": "E:\\Shared", "type": "dir"},
        ]
        plan = plan_for(
            [
                {
                    "id": "collision",
                    "dir": "D:\\Work\\One",
                    "declarations": [{"field": "working_directory", "value": "D:\\Work\\One"}],
                }
            ],
            document,
        )
        self.assertEqual(plan["plan"]["updates"], [])
        self.assertEqual(plan["plan"]["retained"][0]["reason"], "mapping_ambiguous_target")
        codes = [item["code"] for item in plan["mapping"]["diagnostics"]]
        self.assertIn("target_collision", codes)


class TestVerification(unittest.TestCase):
    def test_every_planned_target_maps_back_to_its_source(self):
        plan = plan_for(
            [
                {
                    "id": "verify",
                    "dir": "D:\\Work\\Docs",
                    "declarations": [
                        {"field": "relative_path", "value": ".\\a.txt", "base": "shortcut_dir"},
                        {"field": "working_directory", "value": "D:\\Work\\Docs"},
                        {"field": "icon_location", "value": "%SystemRoot%\\system32\\shell32.dll"},
                        {
                            "field": "link_info.local_base_path",
                            "value": "D:\\Work\\Docs",
                            "suffix": "a.txt",
                        },
                    ],
                }
            ]
        )
        self.assertEqual(plan["verification"]["failed"], [])
        self.assertGreater(plan["verification"]["checked"], 0)
        for entry in plan["verification"]["confirmed"]:
            self.assertTrue(entry["material_preserved"])

    def test_symbol_preserved_declarations_verify(self):
        plan = plan_for(
            [
                {
                    "id": "symbols",
                    "dir": "D:\\Work\\Docs",
                    "declarations": [
                        {"field": "icon_location", "value": "%SystemRoot%\\system32\\shell32.dll"},
                        {
                            "field": "extra.ENVIRONMENTAL_VARIABLES_LOCATION_BLOCK.target_unicode",
                            "value": GUID_DOWNLOADS + "\\a.txt",
                        },
                    ],
                }
            ]
        )
        self.assertEqual(plan["verification"]["failed"], [])
        self.assertEqual(
            {item["reason"] for item in plan["plan"]["unchanged"]},
            {"already_resolved"},
        )

    def test_verification_reports_unproven_declarations(self):
        plan = plan_for(
            [
                {
                    "id": "unproven",
                    "dir": "D:\\Work\\Docs",
                    "declarations": [
                        {"field": "working_directory", "value": "%UNDEFINED%"},
                    ],
                }
            ]
        )
        unproven = plan["verification"]["unproven"]
        self.assertEqual(len(unproven), 1)
        self.assertEqual(unproven[0]["reason"], "unresolved_source")


class TestReadOnlyContract(unittest.TestCase):
    def test_plan_states_shortcuts_are_not_modified(self):
        plan = plan_for([])
        self.assertTrue(plan["read_only"])
        self.assertTrue(plan["shortcuts_are_not_modified"])

    def test_plan_is_json_serializable(self):
        plan = plan_for(
            [
                {
                    "id": "json",
                    "dir": "D:\\Work\\Docs",
                    "declarations": [
                        {"field": "working_directory", "value": "D:\\Work\\Docs"},
                    ],
                }
            ]
        )
        json.dumps(plan)
        self.assertIn("Field corrections", render_plan_text(plan))

    def test_machine_configs_are_echoed(self):
        plan = plan_for([])
        self.assertEqual(plan["source"]["name"], "PC-A")
        self.assertEqual(plan["target"]["name"], "PC-B")
        self.assertEqual(plan["source"]["env"]["SYSTEMROOT"], "C:\\Windows")
        self.assertEqual(
            plan["target"]["known_folders"][GUID_DOWNLOADS], "E:\\Profiles\\bob\\Downloads"
        )


class TestLnkJsonAdapter(unittest.TestCase):
    def test_adapter_keeps_link_info_suffix_together(self):
        lnk_json = {
            "data": {"relative_path": ".\\a.txt", "working_directory": "%SystemRoot%\\Temp"},
            "link_info": {
                "local_base_path": "D:\\Work\\Docs",
                "common_path_suffix": "a.txt",
            },
            "extra": {
                "ENVIRONMENTAL_VARIABLES_LOCATION_BLOCK": {
                    "target_ansi": "%SystemRoot%\\system32\\cmd.exe",
                    "target_unicode": "%SystemRoot%\\system32\\cmd.exe",
                }
            },
        }
        declarations = declarations_from_lnk_json(lnk_json)
        by_field = {item["field"]: item for item in declarations}
        self.assertEqual(by_field["relative_path"]["base"], "shortcut_dir")
        self.assertEqual(by_field["link_info.local_base_path"]["suffix"], "a.txt")
        self.assertEqual(
            by_field["extra.ENVIRONMENTAL_VARIABLES_LOCATION_BLOCK.target_unicode"]["value"],
            "%SystemRoot%\\system32\\cmd.exe",
        )


if __name__ == "__main__":
    unittest.main()
