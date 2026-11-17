import unittest

from LnkParse3.parser_diff import diff_reports


def field(name, value, offset=None, size=None):
    return {"name": name, "value": value, "offset": offset, "size": size}


def file_entry(path="a.lnk", sha256="h" * 64, size=100, fields=None):
    return {"path": path, "sha256": sha256, "size": size, "fields": fields or []}


def report(files, version="1.0.0"):
    return {"parser": {"name": "LnkParse3", "version": version}, "files": files}


def diff(old_files, new_files):
    return diff_reports(report(old_files, "1.0.0"), report(new_files, "2.0.0"))


def only_file(result):
    assert len(result["files"]) == 1
    return result["files"][0]


class TestParserDiff(unittest.TestCase):
    def test_identical_reports_have_no_changes(self):
        files = [file_entry(fields=[field("header.size", 76, 0, 4)])]
        entry = only_file(diff(files, [file_entry(fields=[field("header.size", 76, 0, 4)])]))
        self.assertEqual(entry["newly_recognized"], [])
        self.assertEqual(entry["reinterpreted"], [])
        self.assertEqual(entry["insufficient_evidence"], [])
        self.assertIsNone(entry["original_changed"])
        self.assertEqual(entry["non_business"]["unchanged_fields"], 1)

    def test_field_order_is_not_a_change(self):
        old = [file_entry(fields=[field("a", 1, 0, 4), field("b", 2, 4, 4)])]
        new = [file_entry(fields=[field("b", 2, 4, 4), field("a", 1, 0, 4)])]
        entry = only_file(diff(old, new))
        self.assertEqual(entry["newly_recognized"], [])
        self.assertEqual(entry["reinterpreted"], [])
        self.assertEqual(entry["insufficient_evidence"], [])
        self.assertEqual(entry["non_business"]["unchanged_fields"], 2)

    def test_dict_key_order_in_value_is_not_a_change(self):
        old = [file_entry(fields=[field("flags", {"a": 1, "b": 2}, 0, 4)])]
        new = [file_entry(fields=[field("flags", {"b": 2, "a": 1}, 0, 4)])]
        entry = only_file(diff(old, new))
        self.assertEqual(entry["reinterpreted"], [])
        self.assertEqual(entry["non_business"]["unchanged_fields"], 1)

    def test_rename_is_not_a_business_change(self):
        old = [file_entry(fields=[field("header.hotkey", "CTRL+A", 60, 2)])]
        new = [file_entry(fields=[field("header.hot_key", "CTRL+A", 60, 2)])]
        entry = only_file(diff(old, new))
        self.assertEqual(entry["newly_recognized"], [])
        self.assertEqual(entry["reinterpreted"], [])
        self.assertEqual(entry["insufficient_evidence"], [])
        self.assertEqual(len(entry["non_business"]["renames"]), 1)
        rename = entry["non_business"]["renames"][0]
        self.assertEqual(rename["old_name"], "header.hotkey")
        self.assertEqual(rename["new_name"], "header.hot_key")
        self.assertEqual(rename["range"], {"offset": 60, "size": 2})

    def test_new_coverage_is_newly_recognized(self):
        old = [file_entry(fields=[field("header.size", 76, 0, 4)])]
        new = [file_entry(fields=[
            field("header.size", 76, 0, 4),
            field("extra.console.font", "Lucida", 200, 32),
        ])]
        entry = only_file(diff(old, new))
        self.assertEqual(len(entry["newly_recognized"]), 1)
        change = entry["newly_recognized"][0]
        self.assertEqual(change["kind"], "new_coverage")
        self.assertEqual(change["new"]["range"], {"offset": 200, "size": 32})
        self.assertEqual(entry["reinterpreted"], [])
        self.assertIsNone(entry["original_changed"])

    def test_one_coarse_old_field_may_refine_into_many_new_fields(self):
        old = [file_entry(fields=[field("extra.unknown_block", "0xAABBCCDD", 100, 100)])]
        new = [file_entry(fields=[
            field("extra.console.font_size", 12, 110, 4),
            field("extra.console.codepage", 65001, 150, 4),
        ])]
        entry = only_file(diff(old, new))
        self.assertEqual(len(entry["newly_recognized"]), 1)
        change = entry["newly_recognized"][0]
        self.assertEqual(change["kind"], "refinement")
        self.assertEqual(change["old"]["name"], "extra.unknown_block")
        self.assertEqual(change["old"]["range"], {"offset": 100, "size": 100})
        self.assertEqual(len(change["new_fields"]), 2)
        # The refined old field is fully explained: it must not show up as
        # uncovered_by_new.
        self.assertEqual(entry["insufficient_evidence"], [])

    def test_same_range_different_value_keeps_both_claims(self):
        old = [file_entry(fields=[field("header.hotkey", "CTRL+ALT+A", 60, 2)])]
        new = [file_entry(fields=[field("header.hotkey", "CTRL+ALT+B", 60, 2)])]
        entry = only_file(diff(old, new))
        self.assertEqual(len(entry["reinterpreted"]), 1)
        change = entry["reinterpreted"][0]
        self.assertEqual(change["range"], {"offset": 60, "size": 2})
        self.assertEqual(change["old"]["value"], "CTRL+ALT+A")
        self.assertEqual(change["new"]["value"], "CTRL+ALT+B")

    def test_new_default_value_does_not_erase_old_claim(self):
        old = [file_entry(fields=[field("header.hotkey", "CTRL+ALT+A", 60, 2)])]
        new = [file_entry(fields=[field("header.hotkey", "UNSET", 60, 2)])]
        entry = only_file(diff(old, new))
        self.assertEqual(len(entry["reinterpreted"]), 1)
        change = entry["reinterpreted"][0]
        self.assertEqual(change["old"]["value"], "CTRL+ALT+A")
        self.assertEqual(change["new"]["value"], "UNSET")

    def test_same_range_different_name_and_value_keeps_both_claims(self):
        old = [file_entry(fields=[field("block.raw", "0x00FF", 300, 16)])]
        new = [file_entry(fields=[field("block.signature", "INVALID", 300, 16)])]
        entry = only_file(diff(old, new))
        self.assertEqual(len(entry["reinterpreted"]), 1)
        change = entry["reinterpreted"][0]
        self.assertEqual(change["old"]["name"], "block.raw")
        self.assertEqual(change["new"]["name"], "block.signature")

    def test_range_correction_keeps_both_ranges(self):
        old = [file_entry(fields=[field("header.file_size", 42, 48, 4)])]
        new = [file_entry(fields=[field("header.file_size", 42, 52, 4)])]
        entry = only_file(diff(old, new))
        self.assertEqual(len(entry["reinterpreted"]), 1)
        change = entry["reinterpreted"][0]
        self.assertEqual(change["kind"], "range")
        self.assertEqual(change["old"]["range"], {"offset": 48, "size": 4})
        self.assertEqual(change["new"]["range"], {"offset": 52, "size": 4})

    def test_hash_mismatch_is_original_changed_not_parser_change(self):
        old = [file_entry(sha256="a" * 64, fields=[field("header.size", 76, 0, 4)])]
        new = [file_entry(sha256="b" * 64, fields=[field("header.size", 80, 0, 4)])]
        entry = only_file(diff(old, new))
        self.assertIsNotNone(entry["original_changed"])
        self.assertEqual(entry["original_changed"]["old_sha256"], "a" * 64)
        self.assertEqual(entry["original_changed"]["new_sha256"], "b" * 64)
        # Field-level differences are attributable to the file, not the parser.
        self.assertEqual(entry["newly_recognized"], [])
        self.assertEqual(entry["reinterpreted"], [])
        self.assertEqual(entry["insufficient_evidence"], [])

    def test_size_mismatch_without_hash_is_original_changed(self):
        old = [file_entry(sha256=None, size=100)]
        new = [file_entry(sha256=None, size=120)]
        entry = only_file(diff(old, new))
        self.assertIsNotNone(entry["original_changed"])
        self.assertEqual(entry["identity"]["status"], "changed")

    def test_old_only_coverage_is_insufficient_evidence_not_removal(self):
        old = [file_entry(fields=[
            field("header.size", 76, 0, 4),
            field("extra.console.font", "Lucida", 200, 32),
        ])]
        new = [file_entry(fields=[field("header.size", 76, 0, 4)])]
        entry = only_file(diff(old, new))
        self.assertEqual(entry["newly_recognized"], [])
        self.assertEqual(len(entry["insufficient_evidence"]), 1)
        change = entry["insufficient_evidence"][0]
        self.assertEqual(change["kind"], "uncovered_by_new")
        self.assertEqual(change["old"]["range"], {"offset": 200, "size": 32})

    def test_new_coarse_field_swallowing_old_fields_keeps_old_claims(self):
        old = [file_entry(fields=[
            field("extra.console.font_size", 12, 110, 4),
            field("extra.console.codepage", 65001, 150, 4),
        ])]
        new = [file_entry(fields=[field("extra.unknown_block", "0xAABBCCDD", 100, 100)])]
        entry = only_file(diff(old, new))
        self.assertEqual(len(entry["insufficient_evidence"]), 1)
        change = entry["insufficient_evidence"][0]
        self.assertEqual(change["kind"], "coverage_coarsened")
        self.assertEqual(len(change["old_fields"]), 2)
        self.assertEqual(change["new"]["range"], {"offset": 100, "size": 100})

    def test_partial_overlap_is_insufficient_evidence(self):
        old = [file_entry(fields=[field("block.a", "x", 100, 32)])]
        new = [file_entry(fields=[field("block.b", "y", 120, 32)])]
        entry = only_file(diff(old, new))
        self.assertEqual(len(entry["insufficient_evidence"]), 1)
        change = entry["insufficient_evidence"][0]
        self.assertEqual(change["kind"], "partial_overlap")
        self.assertEqual(change["new"]["range"], {"offset": 120, "size": 32})
        self.assertEqual(change["old_fields"][0]["range"], {"offset": 100, "size": 32})

    def test_field_without_range_cannot_be_located(self):
        old = [file_entry(fields=[field("header.title", "old title")])]
        new = [file_entry(fields=[field("header.title", "new title")])]
        entry = only_file(diff(old, new))
        self.assertEqual(entry["reinterpreted"], [])
        self.assertEqual(len(entry["insufficient_evidence"]), 1)
        change = entry["insufficient_evidence"][0]
        self.assertEqual(change["kind"], "missing_range")
        self.assertEqual(change["old"]["value"], "old title")
        self.assertEqual(change["new"]["value"], "new title")

    def test_unranged_field_without_counterpart_is_insufficient_evidence(self):
        old = [file_entry(fields=[field("header.title", "title")])]
        new = [file_entry()]
        entry = only_file(diff(old, new))
        self.assertEqual(len(entry["insufficient_evidence"]), 1)
        self.assertEqual(entry["insufficient_evidence"][0]["kind"], "missing_range")

    def test_file_missing_in_one_report_is_insufficient_evidence(self):
        result = diff([file_entry(path="only-old.lnk")], [])
        entry = only_file(result)
        self.assertEqual(entry["insufficient_evidence"][0]["kind"], "file_missing_in_new")
        self.assertEqual(result["summary"]["files_compared"], 0)

    def test_unverifiable_identity_adds_warning_but_still_compares(self):
        old = [file_entry(sha256=None, size=None, fields=[field("a", 1, 0, 4)])]
        new = [file_entry(sha256=None, size=None, fields=[field("a", 1, 0, 4),
                                                          field("b", 2, 4, 4)])]
        entry = only_file(diff(old, new))
        self.assertEqual(entry["identity"]["status"], "unverifiable")
        self.assertIn("warning", entry["identity"])
        self.assertEqual(len(entry["newly_recognized"]), 1)

    def test_summary_counts(self):
        old = [
            file_entry(path="same.lnk", fields=[field("a", 1, 0, 4)]),
            file_entry(path="changed.lnk", sha256="a" * 64),
            file_entry(path="grows.lnk", fields=[field("a", 1, 0, 4)]),
        ]
        new = [
            file_entry(path="same.lnk", fields=[field("a", 1, 0, 4)]),
            file_entry(path="changed.lnk", sha256="b" * 64),
            file_entry(path="grows.lnk", fields=[field("a", 1, 0, 4),
                                                 field("b", 2, 4, 4)]),
        ]
        result = diff(old, new)
        summary = result["summary"]
        self.assertEqual(summary["files_compared"], 3)
        self.assertEqual(summary["original_changed"], 1)
        self.assertEqual(summary["newly_recognized"], 1)
        self.assertEqual(summary["reinterpreted"], 0)
        self.assertEqual(summary["insufficient_evidence"], 0)


if __name__ == '__main__':
    unittest.main()
