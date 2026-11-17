import base64
import json
import unittest
from pathlib import Path

import LnkParse3
from LnkParse3.resolution import MachineProfile
from LnkParse3.resolution import resolve


SAMPLES = Path(__file__).parent / "samples"


def load_sample(name):
    with open(SAMPLES / name, "rb") as handle:
        return LnkParse3.lnk_file(indata=base64.b64decode(handle.read()))


def lnk(
    flags=None,
    target_items=None,
    link_info=None,
    data=None,
    extra=None,
    file_flags=None,
):
    return {
        "header": {"link_flags": flags or [], "file_flags": file_flags or []},
        "target": {"items": target_items or []},
        "link_info": link_info or {},
        "data": data or {},
        "extra": extra or {},
    }


def my_computer_items(names, volume_name="C:\\"):
    items = [
        {
            "class": "Root Folder",
            "guid": "20D04FE0-3AEA-1069-A2D8-08002B30309D",
            "sort_index": "My Computer",
        },
        {"class": "Volume Item", "volume_name": volume_name},
    ]
    items.extend(
        {"class": "File entry", "primary_name": name, "flags": "Is directory"}
        for name in names[:-1]
    )
    items.append({"class": "File entry", "primary_name": names[-1], "flags": "Is file"})
    return items


def local_link_info(path, **location_info):
    return {
        "link_info_flags": 1,
        "location": "Local",
        "local_base_path": path,
        "common_path_suffix": "",
        "location_info": location_info,
    }


class TestResolutionEvidence(unittest.TestCase):
    def test_catalog_confirms_and_merges_redundant_candidates(self):
        sample = load_sample("microsoft_example")
        result = resolve(
            sample,
            {
                "catalog": ["C:\\test\\a.txt"],
                "volumes": [{"drive": "C:", "serial_number": "0x307a8a81"}],
            },
        )

        self.assertEqual(result.status, "confirmed")
        self.assertEqual(result.selected_target, "C:\\test\\a.txt")
        self.assertEqual(len(result.candidates), 1)
        candidate = result.candidates[0]
        self.assertEqual(set(candidate.sources), {"idlist", "link_info_local", "relative_path"})
        self.assertFalse(result.has_conflicts)

    def test_no_catalog_leaves_branch_even_for_consistent_fields(self):
        sample = load_sample("microsoft_example")
        result = resolve(sample, {})

        self.assertEqual(result.status, "unresolved")
        self.assertIsNone(result.selected_target)
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].status, "branch")
        self.assertEqual(result.candidates[0].reason, "no catalog evidence")

    def test_conflicting_sources_are_not_collapsed_by_read_order(self):
        target = lnk(
            flags=["HasTargetIDList", "HasLinkInfo"],
            target_items=my_computer_items(["a", "run.exe"]),
            link_info=local_link_info("D:\\b\\run.exe", drive_type="DRIVE_FIXED"),
        )
        result = resolve(target, {"catalog": ["C:\\a\\run.exe"]})

        self.assertEqual(result.status, "confirmed")
        self.assertEqual(result.selected_target, "C:\\a\\run.exe")
        paths = {candidate.path for candidate in result.candidates}
        self.assertEqual(paths, {"C:\\a\\run.exe", "D:\\b\\run.exe"})
        conflict = next(
            conflict for conflict in result.conflicts if conflict.type == "path_conflict"
        )
        sources = {field.source for field in conflict.fields}
        self.assertIn("idlist", sources)
        self.assertIn("link_info_local", sources)

    def test_both_catalog_objects_stay_ambiguous(self):
        target = lnk(
            flags=["HasTargetIDList", "HasLinkInfo"],
            target_items=my_computer_items(["a", "run.exe"]),
            link_info=local_link_info("D:\\b\\run.exe"),
        )
        result = resolve(
            target,
            {
                "catalog": [
                    {"path": "C:\\a\\run.exe", "object_id": "obj-1"},
                    {"path": "D:\\b\\run.exe", "object_id": "obj-2"},
                ]
            },
        )

        self.assertEqual(result.status, "ambiguous")
        self.assertIsNone(result.selected_target)
        self.assertEqual({candidate.status for candidate in result.candidates}, {"branch"})

    def test_ansi_and_unicode_fields_are_read_independently(self):
        target = lnk(
            flags=["HasLinkInfo"],
            link_info={
                "link_info_flags": 1,
                "location": "Local",
                "local_base_path": "C:\\ansi\\x.txt",
                "common_path_suffix": "",
                "local_base_path_unicode": "C:\\unicode\\x.txt",
                "common_path_suffix_unicode": "",
                "location_info": {},
            },
        )
        result = resolve(target, {"catalog": ["C:\\unicode\\x.txt"]})

        encodings = {
            candidate.path: candidate.locations[0].encoding for candidate in result.candidates
        }
        self.assertEqual(encodings["C:\\ansi\\x.txt"], "ansi")
        self.assertEqual(encodings["C:\\unicode\\x.txt"], "unicode")
        self.assertTrue(
            any(conflict.type == "link_info_encoding_conflict" for conflict in result.conflicts)
        )
        self.assertEqual(result.status, "confirmed")
        self.assertEqual(result.selected_target, "C:\\unicode\\x.txt")

    def test_environment_variable_expansion_and_missing_values(self):
        target = lnk(
            flags=["HasLinkInfo"],
            link_info=local_link_info("%USERPROFILE%\\tool.exe"),
        )

        confirmed = resolve(
            target,
            {
                "environment": {"USERPROFILE": "C:\\Users\\alice"},
                "catalog": ["C:\\Users\\alice\\tool.exe"],
            },
        )
        self.assertEqual(confirmed.selected_target, "C:\\Users\\alice\\tool.exe")

        unresolved = resolve(target, {})
        self.assertEqual(unresolved.status, "unresolved")
        self.assertEqual(unresolved.missing_environment, ["USERPROFILE"])
        self.assertTrue(
            any(conflict.type == "missing_environment" for conflict in unresolved.conflicts)
        )
        self.assertEqual(unresolved.candidates[0].status, "branch")

    def test_share_mapping_makes_drive_and_unc_the_same_object(self):
        target = lnk(
            flags=["HasLinkInfo"],
            link_info={
                "link_info_flags": 2,
                "location": "Network",
                "common_path_suffix": "dir\\f.pdf",
                "location_info": {
                    "net_name": "\\\\srv\\share",
                    "device_name": "Z:",
                    "common_network_relative_link_flags": 3,
                },
            },
        )
        result = resolve(
            target,
            {
                "shares": [{"unc": "\\\\srv\\share", "drive": "Z:"}],
                "catalog": ["Z:\\dir\\f.pdf"],
            },
        )

        self.assertEqual(result.status, "confirmed")
        self.assertEqual(result.selected_target, "Z:\\dir\\f.pdf")
        candidate = result.candidates[0]
        self.assertIn("\\\\srv\\share\\dir\\f.pdf", candidate.paths)
        self.assertTrue(candidate.share_evidence)
        self.assertFalse(result.has_conflicts)

    def test_volume_identity_remaps_only_with_positive_evidence(self):
        target = lnk(
            flags=["HasLinkInfo"],
            link_info=local_link_info(
                "C:\\data\\x.txt",
                volume_label="DATA",
                drive_serial_number="0x1234",
                drive_type="DRIVE_FIXED",
            ),
        )
        profile = {
            "volumes": [
                {
                    "drive": "D:",
                    "label": "DATA",
                    "serial_number": "0x1234",
                    "drive_type": "DRIVE_FIXED",
                }
            ],
            "catalog": ["D:\\data\\x.txt"],
        }
        result = resolve(target, profile)

        self.assertEqual(result.status, "confirmed")
        self.assertEqual(result.selected_target, "D:\\data\\x.txt")
        candidate = result.candidates[0]
        self.assertIn("C:\\data\\x.txt", candidate.paths)
        self.assertIn("D:\\data\\x.txt", candidate.paths)
        self.assertTrue(candidate.volume_evidence)

    def test_contradictory_volume_identity_is_reported(self):
        target = lnk(
            flags=["HasLinkInfo"],
            link_info=local_link_info(
                "C:\\data\\x.txt", volume_label="DATA", drive_serial_number="0x1234"
            ),
        )
        result = resolve(
            target,
            {
                "volumes": [{"drive": "C:", "label": "OTHER", "serial_number": "0x9999"}],
                "catalog": ["C:\\data\\x.txt"],
            },
        )

        self.assertEqual(result.status, "confirmed")
        self.assertTrue(
            any(conflict.type == "volume_identity_mismatch" for conflict in result.conflicts)
        )
        evidence = result.candidates[0].volume_evidence[0]
        self.assertFalse(evidence["supports"])
        self.assertIn("volume_label", evidence["contradictions"])

    def test_disabling_a_source_removes_its_candidates(self):
        sample = load_sample("microsoft_example")
        profile = {
            "catalog": ["C:\\test\\a.txt"],
            "policy": {"disabled_sources": ["relative_path", "link_info_local"]},
        }
        result = resolve(sample, profile)

        self.assertEqual(result.candidates[0].sources, ["idlist"])
        self.assertEqual(result.selected_target, "C:\\test\\a.txt")

        disabled = resolve(
            sample,
            {
                "catalog": ["C:\\test\\a.txt"],
                "policy": {"disabled_sources": ["link_info_local", "idlist"]},
            },
        )
        # Disabling sources removes their facts, not the remaining evidence:
        # the relative path still reproduces the target.
        self.assertEqual(disabled.status, "confirmed")
        self.assertEqual(disabled.candidates[0].sources, ["relative_path"])

        no_evidence = resolve(
            sample,
            {
                "catalog": ["C:\\test\\a.txt"],
                "policy": {"disabled_sources": ["link_info_local", "idlist", "relative_path"]},
            },
        )
        self.assertEqual(no_evidence.status, "unresolved")
        self.assertEqual(no_evidence.candidates, [])

    def test_preferred_source_breaks_ties_between_catalog_objects(self):
        target = lnk(
            flags=["HasTargetIDList", "HasLinkInfo"],
            target_items=my_computer_items(["a", "run.exe"]),
            link_info=local_link_info("D:\\b\\run.exe"),
        )
        profile = {
            "catalog": ["C:\\a\\run.exe", "D:\\b\\run.exe"],
            "policy": {"preferred_sources": ["link_info_local"]},
        }
        result = resolve(target, profile)

        self.assertEqual(result.status, "confirmed")
        self.assertEqual(result.selected_target, "D:\\b\\run.exe")

    def test_prefer_environment_path_flag_is_honoured(self):
        target = lnk(
            flags=["HasLinkInfo", "PreferEnvironmentPath"],
            link_info=local_link_info("C:\\stale\\tool.exe"),
            extra={
                "ENVIRONMENTAL_VARIABLES_LOCATION_BLOCK": {
                    "target_ansi": "%APPDATA%\\tool.exe",
                    "target_unicode": "%APPDATA%\\tool.exe",
                }
            },
        )
        result = resolve(
            target,
            {
                "environment": {"APPDATA": "C:\\Users\\alice\\AppData\\Roaming"},
                "catalog": ["C:\\stale\\tool.exe", "C:\\Users\\alice\\AppData\\Roaming\\tool.exe"],
            },
        )

        self.assertEqual(result.status, "confirmed")
        self.assertEqual(result.selected_target, "C:\\Users\\alice\\AppData\\Roaming\\tool.exe")

    def test_disable_link_path_tracking_ignores_environment_block(self):
        target = lnk(
            flags=["HasLinkInfo", "DisableLinkPathTracking"],
            link_info=local_link_info("C:\\stale\\tool.exe"),
            extra={
                "ENVIRONMENTAL_VARIABLES_LOCATION_BLOCK": {
                    "target_ansi": "%APPDATA%\\tool.exe",
                    "target_unicode": "%APPDATA%\\tool.exe",
                }
            },
        )
        result = resolve(
            target,
            {
                "environment": {"APPDATA": "C:\\Users\\alice\\AppData\\Roaming"},
                "catalog": ["C:\\Users\\alice\\AppData\\Roaming\\tool.exe"],
            },
        )

        self.assertEqual(result.status, "unresolved")
        self.assertTrue(
            any(
                conflict.type == "source_disabled" and conflict.sources == ["environment"]
                for conflict in result.conflicts
            )
        )

    def test_force_no_link_info_ignores_link_info_candidates(self):
        target = lnk(
            flags=["HasLinkInfo", "ForceNoLinkInfo"],
            link_info=local_link_info("C:\\a\\x.txt"),
        )
        result = resolve(target, {"catalog": ["C:\\a\\x.txt"]})
        self.assertEqual(result.status, "unresolved")
        self.assertEqual(result.candidates, [])

    def test_unknown_target_guid_keeps_unresolved_branch(self):
        target = lnk(
            flags=["HasTargetIDList"],
            target_items=[
                {"class": "Root Folder", "guid": "11111111-2222-3333-4444-555555555555"},
                {"class": "File entry", "primary_name": "x.bin", "flags": "Is file"},
            ],
        )
        result = resolve(target, {"catalog": ["C:\\x.bin"]})

        self.assertEqual(result.status, "unresolved")
        self.assertEqual(len(result.candidates), 1)
        self.assertIsNone(result.candidates[0].path)
        self.assertTrue(result.candidates[0].notes)

    def test_relative_path_without_working_directory_stays_relative(self):
        target = lnk(
            flags=["HasRelativePath"],
            data={"relative_path": ".\\x.txt"},
        )
        result = resolve(target, {"catalog": ["C:\\Users\\alice\\x.txt"]})

        self.assertEqual(result.status, "unresolved")
        self.assertEqual(result.candidates[0].path, "x.txt")
        self.assertFalse(result.candidates[0].locations[0].absolute)

    def test_working_directory_anchors_relative_path(self):
        target = lnk(
            flags=["HasRelativePath"],
            data={"relative_path": ".\\x.txt", "working_directory": "%USERPROFILE%\\docs"},
        )
        result = resolve(
            target,
            {
                "environment": {"USERPROFILE": "C:\\Users\\alice"},
                "catalog": ["C:\\Users\\alice\\docs\\x.txt"],
            },
        )

        self.assertEqual(result.status, "confirmed")
        self.assertEqual(result.selected_target, "C:\\Users\\alice\\docs\\x.txt")

    def test_profile_fingerprint_changes_and_is_not_reused(self):
        sample = load_sample("microsoft_example")
        profile_a = MachineProfile.from_dict({"catalog": ["C:\\test\\a.txt"]})
        profile_b = MachineProfile.from_dict({"catalog": ["D:\\other\\a.txt"]})

        result_a = resolve(sample, profile_a)
        result_b = resolve(sample, profile_b)

        self.assertEqual(result_a.status, "confirmed")
        self.assertEqual(result_b.status, "unresolved")
        self.assertNotEqual(result_a.profile_fingerprint, result_b.profile_fingerprint)
        self.assertNotEqual(result_a.selected_target, result_a.profile_fingerprint)
        self.assertEqual(result_a.selected_target, "C:\\test\\a.txt")

    def test_policy_override_does_not_mutate_profile(self):
        sample = load_sample("microsoft_example")
        profile = MachineProfile.from_dict({"catalog": ["C:\\test\\a.txt"]})
        before = profile.policy.as_dict()

        result = resolve(sample, profile, policy={"disabled_sources": ["idlist"]})

        self.assertEqual(profile.policy.as_dict(), before)
        self.assertNotIn("idlist", result.candidates[0].sources)
        self.assertNotEqual(result.profile_fingerprint, profile.fingerprint())

    def test_provenance_points_at_adopted_fields(self):
        sample = load_sample("network_info")
        result = resolve(
            sample,
            {
                "shares": [{"unc": "\\\\10.0.0.150\\LMmetal", "drive": "Z:"}],
                "catalog": [
                    "\\\\10.0.0.150\\LMmetal\\A - LM METAL LIFT\\01.OBCHOD - BROŽURY - "
                    "Prodejní a technické informace o produktech\\ETN\\ETN-Katalog-ENG\\"
                    "Katalog ETN 10_2017\\Lift-programme\\ETN-lift programme 2017.pdf"
                ],
            },
        )

        self.assertEqual(result.status, "confirmed")
        self.assertTrue(result.selected_target.startswith("\\\\10.0.0.150\\LMmetal\\"))
        provenance = {
            (field.source, field.field): field.value for field in result.selected.provenance
        }
        self.assertEqual(provenance[("link_info_network", "net_name")], "\\\\10.0.0.150\\LMmetal")
        self.assertIn(("link_info_network", "device_name"), provenance)
        self.assertEqual(
            provenance[("link_info_network", "common_path_suffix")],
            result.selected.locations[0].path.split("LMmetal\\", 1)[1],
        )

    def test_strict_conflicts_blocks_otherwise_supported_candidate(self):
        target = lnk(
            flags=["HasTargetIDList", "HasLinkInfo"],
            target_items=my_computer_items(["a", "run.exe"]),
            link_info=local_link_info("D:\\b\\run.exe"),
        )
        result = resolve(
            target,
            {
                "catalog": ["C:\\a\\run.exe", "D:\\b\\run.exe"],
                "policy": {"strict_conflicts": True},
            },
        )

        self.assertEqual(result.status, "unresolved")
        self.assertEqual({candidate.status for candidate in result.candidates}, {"branch"})
        self.assertTrue(
            all(
                candidate.reason == "blocked by a conflict (strict_conflicts)"
                for candidate in result.candidates
            )
        )

    def test_serialization_is_json_safe(self):
        sample = load_sample("microsoft_example")
        result = resolve(sample, {"catalog": ["C:\\test\\a.txt"]})
        payload = json.loads(result.to_json())
        self.assertEqual(payload["status"], "confirmed")
        self.assertEqual(payload["selected_target"], "C:\\test\\a.txt")
        self.assertIn("provenance", payload["target"])


if __name__ == "__main__":
    unittest.main()
