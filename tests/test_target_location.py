import base64
import struct
import unittest
import warnings

from LnkParse3.lnk_file import LnkFile
from LnkParse3.lnk_targets import LnkTargets


TARGET_DIR = "tests/samples"


def sample(name):
    with open(f"{TARGET_DIR}/{name}", "rb") as fp:
        return base64.b64decode(fp.read())


def item(item_type, payload, size=None):
    data = bytes([item_type]) + payload
    total = 2 + len(data) if size is None else size
    return struct.pack("<H", total) + data


def file_entry(name, extension=b""):
    encoded = name.encode("ascii") + b"\x00"
    payload = bytes([0x01]) + b"\x00\x00\x00\x00" + b"\x00" * 4 + b"\x00\x00" + encoded
    if len(encoded) % 2:
        payload += b"\x00"
    return item(0x31, payload + extension)


def extension_block(body=b"", kind=0xBEEF0004, size=None):
    if size is None:
        size = 8 + len(body)
    return struct.pack("<HHI", size, 4, kind) + body


def root_folder(guid_bytes, sort_index=0x50):
    return item(0x1F, bytes([sort_index]) + guid_bytes)


def volume_item(name):
    raw = name.encode("ascii").ljust(23, b"\x00")
    return item(0x2F, raw)


def id_list(items):
    data = b"".join(items) + b"\x00\x00"
    return struct.pack("<H", len(data)) + data


MY_COMPUTER = bytes.fromhex("e04fd020ea3a6910a2d808002b30309d")
USERS = bytes.fromhex("471a0359723fa74489c55595fe6b30ee")
CONTROL_PANEL = bytes.fromhex("2020ec21ea3a6910a2dd08002b30309d")


class TestPidlBoundaries(unittest.TestCase):
    def setUp(self):
        warnings.simplefilter("ignore")

    def parse(self, items):
        # Offset 76 matches the samples: the header ends there and the
        # IDListSize field occupies the next two bytes.
        return LnkTargets(indata=id_list(items), offset=76)

    def test_terminal_and_offsets(self):
        targets = self.parse([root_folder(MY_COMPUTER), volume_item("C:\\"), file_entry("Windows")])
        integrity = targets.integrity()
        self.assertTrue(integrity["terminal_present"])
        records = targets.records()
        expected_offsets = []
        position = 0
        for record in records:
            expected_offsets.append(position)
            position += record["size"]
        self.assertEqual([record["offset"] for record in records], expected_offsets)
        self.assertEqual(integrity["terminal_offset"], position)
        self.assertEqual(targets.terminal()["raw"], "0000")

    def test_bad_extension_size_does_not_swallow_next_item(self):
        bad_extension = struct.pack("<HHI", 0x7FFF, 4, 0xBEEF0004)
        targets = self.parse(
            [
                root_folder(MY_COMPUTER),
                volume_item("C:\\"),
                file_entry("bad", bad_extension),
                file_entry("next"),
            ]
        )
        self.assertEqual(len(targets.records()), 4)
        self.assertEqual(targets.records()[2]["size"], len(file_entry("bad")) + len(bad_extension))
        details = targets.details()
        self.assertEqual(details[2]["extensions"], [])
        self.assertIsNotNone(details[2]["unparsed_tail"])
        self.assertEqual(details[3]["class"], "File entry")
        self.assertFalse(details[3]["truncated"])

    def test_unknown_item_keeps_original_range(self):
        unknown = item(0x99, b"\x11\x22\x33\x44")
        targets = self.parse([root_folder(MY_COMPUTER), unknown, file_entry("Windows")])
        details = targets.details()
        self.assertEqual(details[1]["class"], "Unknown")
        self.assertEqual(details[1]["item_type"], "0x99")
        self.assertEqual(details[1]["size"], len(unknown))
        self.assertEqual(details[1]["raw"], unknown.hex())
        self.assertIsNone(details[1]["extensions_complete"])
        self.assertEqual(details[2]["index"], 2)

    def test_missing_terminal_is_reported(self):
        data = b"".join([root_folder(MY_COMPUTER), file_entry("Windows")])
        targets = LnkTargets(indata=struct.pack("<H", len(data)) + data)
        integrity = targets.integrity()
        self.assertFalse(integrity["terminal_present"])
        self.assertIn("missing_terminal", [error["code"] for error in integrity["errors"]])
        self.assertFalse(integrity["complete"])

    def test_item_overrun_is_reported_and_truncated(self):
        overrun = struct.pack("<H", 500) + bytes([0x31]) + b"x" * 4
        data = root_folder(MY_COMPUTER) + overrun + b"\x00\x00"
        targets = LnkTargets(indata=struct.pack("<H", len(data)) + data)
        integrity = targets.integrity()
        self.assertIn("item_overruns_id_list", [error["code"] for error in integrity["errors"]])
        self.assertTrue(targets.records()[-1]["truncated"])
        self.assertFalse(integrity["items_complete"])

    def test_extension_offsets_are_reported_in_absolute_terms(self):
        ext = extension_block(body=b"\x01\x02\x03\x04")
        targets = self.parse(
            [root_folder(MY_COMPUTER), volume_item("C:\\"), file_entry("Windows", ext)]
        )
        details = targets.details()
        found = details[2]["extensions"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["size"], len(ext))
        self.assertEqual(found[0]["signature"], "0xBEEF0004")
        self.assertEqual(found[0]["raw"], ext.hex())
        self.assertEqual(
            found[0]["file_offset"],
            details[2]["file_offset"] + 2 + found[0]["offset"],
        )

    def test_unexplained_tail_is_not_an_extension(self):
        ext = extension_block(body=b"\x00\x00") + b"\xaa"
        targets = self.parse(
            [root_folder(MY_COMPUTER), volume_item("C:\\"), file_entry("Windows", ext)]
        )
        integrity = targets.integrity()
        self.assertEqual(integrity["extensions_checked"], 1)
        self.assertEqual(len(integrity["unexplained_extensions"]), 1)
        self.assertFalse(integrity["extensions_complete"])
        self.assertFalse(integrity["complete"])


class TestTargetLocation(unittest.TestCase):
    def setUp(self):
        warnings.simplefilter("ignore")

    def parse(self, items):
        # Offset 76 matches the samples: the header ends there and the
        # IDListSize field occupies the next two bytes.
        return LnkTargets(indata=id_list(items), offset=76)

    def test_drive_chain_builds_path_and_display_separately(self):
        targets = self.parse(
            [
                root_folder(MY_COMPUTER),
                volume_item("C:\\"),
                file_entry("Windows"),
                file_entry("System32"),
            ]
        )
        location = targets.location()
        self.assertEqual(location["status"], "resolved")
        self.assertEqual(location["path"], "C:\\Windows\\System32")
        self.assertEqual(location["display_name"], "My Computer\\C:\\Windows\\System32")
        self.assertEqual(location["confirmed_components"], ["C:\\", "Windows", "System32"])
        self.assertEqual(location["uncertain_connections"], [])

    def test_adjacent_names_do_not_create_path_in_virtual_namespace(self):
        targets = self.parse(
            [root_folder(CONTROL_PANEL), file_entry("Windows"), file_entry("System32")]
        )
        location = targets.location()
        self.assertIsNone(location["path"])
        self.assertEqual(location["status"], "partial")
        self.assertEqual(location["namespace"], "virtual")
        self.assertIn("Windows", location["display_name"])
        self.assertTrue(location["uncertain_connections"])
        self.assertNotIn("C:\\Windows", location["display_name"])

    def test_users_root_without_volume_keeps_drive_gap(self):
        targets = self.parse([root_folder(USERS), file_entry("Roaming")])
        location = targets.location()
        self.assertIsNone(location["path"])
        self.assertEqual(location["status"], "partial")
        self.assertEqual(location["confirmed_components"], ["Users", "Roaming"])
        self.assertEqual(location["hierarchy"], ["<missing:drive letter>", "Users", "Roaming"])
        missing = [gap["missing"] for gap in location["uncertain_connections"]]
        self.assertIn("drive letter", missing)

    def test_users_root_with_volume_anchor_resolves(self):
        targets = self.parse([volume_item("C:\\"), root_folder(USERS), file_entry("Roaming")])
        location = targets.location()
        self.assertEqual(location["path"], "C:\\Users\\Roaming")
        self.assertIsNone(location["partial_path"])

    def test_unknown_middle_item_breaks_path(self):
        targets = self.parse(
            [
                root_folder(MY_COMPUTER),
                volume_item("C:\\"),
                item(0x77, b"\x01\x02"),
                file_entry("Windows"),
            ]
        )
        location = targets.location()
        self.assertIsNone(location["path"])
        self.assertEqual(location["status"], "partial")
        self.assertEqual(location["confirmed_components"], ["C:\\"])
        self.assertEqual(
            location["hierarchy"],
            ["C:\\", "<missing:parent namespace>", "Windows"],
        )
        self.assertTrue(any(gap["after_index"] == 2 for gap in location["uncertain_connections"]))

    def test_missing_parent_is_not_filled_from_next_name(self):
        targets = self.parse([item(0x99, b"\x01\x02"), file_entry("Windows"), file_entry("Temp")])
        location = targets.location()
        self.assertIsNone(location["path"])
        self.assertEqual(location["confirmed_components"], [])
        self.assertEqual(location["hierarchy"][0], "<missing:parent namespace>")
        self.assertEqual(location["hierarchy"][1:], ["Windows", "Temp"])


class TestSampleLocations(unittest.TestCase):
    def setUp(self):
        warnings.simplefilter("ignore")

    def load(self, name):
        return LnkFile(indata=sample(name))

    def test_target_path_and_display_are_separate_properties(self):
        lnk = self.load("sample")
        self.assertEqual(lnk.target_display_name, "Users\\AppData\\Roaming\\.minecraft")
        self.assertIsNone(lnk.target_path)

    def test_microsoft_example_resolves_pidl_path(self):
        location = self.load("microsoft_example").target_location()
        self.assertEqual(location["status"], "resolved")
        self.assertEqual(location["path"], "C:\\test\\a.txt")
        self.assertEqual(location["pidl_path"], "C:\\test\\a.txt")
        self.assertEqual(location["link_info_path"], "C:\\test\\a.txt")
        self.assertTrue(location["link_info_check"]["corroborates"])
        self.assertTrue(location["link_info_check"]["suffix_match"])
        self.assertNotEqual(location["display_name"], location["path"])

    def test_users_delegate_keeps_profile_name_unknown(self):
        location = self.load("sample").target_location()
        self.assertEqual(location["status"], "partial")
        self.assertIsNone(location["path"])
        self.assertNotIn("Jonathan", location["display_name"])
        self.assertIn("<missing:user profile directory name>", location["partial_path"])
        self.assertEqual(
            [segment["class"] for segment in location["segments"]],
            ["Root Folder", "Users files folder", "File entry", "File entry"],
        )
        self.assertEqual(location["confirmed_components"], ["Users"])

    def test_unknown_target_has_no_guessed_path(self):
        location = self.load("unknown_target").target_location()
        self.assertEqual(location["status"], "partial")
        self.assertIsNone(location["path"])
        self.assertEqual(location["namespace"], "virtual")
        self.assertIsNone(location["link_info_path"])
        self.assertTrue(location["uncertain_connections"])

    def test_short_item_does_not_raise(self):
        tiny = struct.pack("<H", 4) + b"\x31\x01"
        targets = LnkTargets(indata=id_list([tiny]))
        details = targets.details()
        self.assertEqual(details[0]["item_type"], "0x31")
        self.assertIsNone(details[0]["file_size"])
        self.assertEqual(details[0]["primary_name"], "")

    def test_extension_data_survives_in_item_trace(self):
        details = self.load("sample16").target_details()
        self.assertTrue(details["integrity"]["terminal_present"])
        self.assertEqual(details["items"][-1]["item_type"], "0xB1")
        self.assertEqual(len(details["items"][-1]["extensions"]), 2)
        self.assertEqual(details["items"][-1]["extensions"][0]["signature"], "0xBEEF0004")
        for item in details["items"]:
            self.assertIn("offset", item)
            self.assertIn("size", item)
            self.assertIn("item_type", item)

    def test_item_details_offsets_match_id_list_size(self):
        details = self.load("sample16").target_details()
        total = 2 + sum(item["size"] for item in details["items"])
        self.assertEqual(total, details["id_list_size"])
        self.assertEqual(details["terminal"]["offset"], details["id_list_size"] - 2)
        self.assertEqual(details["terminal"]["raw"], "0000")

    def test_unknown_target_extension_tail_is_reported(self):
        details = self.load("unknown_target").target_details()
        self.assertFalse(details["integrity"]["extensions_complete"])
        self.assertTrue(details["integrity"]["unexplained_extensions"])
        self.assertFalse(details["integrity"]["complete"])


if __name__ == "__main__":
    unittest.main()
