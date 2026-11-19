"""Unit tests for numbercatalog.py"""

import json
import os
import tempfile
import unittest

from phonenumbers.numbercatalog import NumberCatalog, _main
from phonenumbers.numberrules import RANGE_PENDING, RANGE_VALID
from phonenumbers.util import u


def _supply_document():
    return {"sources": [
        {"source_id": "alpha",
         "supply": [
             {"start": "+493012340000", "end": "+493012340999"},
             {"start": "00493012341000", "end": "00493012341999"},
             {"start": "03012342000", "end": "03012342999"}],
         "exclusions": [
             # Inside alpha's own supply: effective.
             {"start": "+493012340500", "end": "+493012340599"},
             # Outside alpha's supply: out of scope, must be ignored.
             {"start": "+493099990000", "end": "+493099990099"}]},
        {"source_id": "beta",
         "supply": [{"start": "03012342500", "end": "03012343999"}],
         # Overlaps alpha's supply but must only remove beta's own numbers.
         "exclusions": [{"start": "+493012342600", "end": "+493012342699"}]}]}


class NumberCatalogTest(unittest.TestCase):

    def setUp(self):
        self.catalog = NumberCatalog.from_supply_doc(u("DE"), _supply_document())

    def test_supplier_counts_and_scoped_exclusions(self):
        totals = self.catalog.totals()
        stats = self.catalog.source_stats
        # Three alpha ranges of 1000, one beta range of 1500.
        self.assertEqual(totals["supplied_count"], 4500)
        self.assertEqual(stats["alpha"]["supplied_count"], 3000)
        self.assertEqual(stats["beta"]["supplied_count"], 1500)
        # Only one 100-number exclusion per source is in scope.
        self.assertEqual(stats["alpha"]["excluded_count"], 100)
        self.assertEqual(stats["alpha"]["exclusion_out_of_scope_count"], 100)
        self.assertEqual(stats["beta"]["excluded_count"], 100)
        self.assertEqual(stats["beta"]["exclusion_out_of_scope_count"], 0)
        self.assertEqual(totals["excluded_count"], 200)
        self.assertEqual(totals["net_count"], 4300)

    def test_overlapping_supplies_merge_without_double_count(self):
        coverage = self.catalog.coverage
        # Alpha and beta overlap on 500 numbers (3012342500..3012342999); the
        # union therefore holds 4300 - 400 = 3900 unique numbers.
        self.assertEqual(self.catalog.valid_count, 3900)
        self.assertEqual(sum(rng.number_count for rng in coverage), 3900)
        # Coverage stays disjoint and maximal, never expanded per contact.
        for left, right in zip(coverage, coverage[1:]):
            self.assertFalse(left.key() == right.key() and
                             right.start <= left.end + 1)
        # Alpha-only exclusion hole remains absent from the union.
        self.assertFalse(any(rng.start <= 3012340500 <= rng.end
                             for rng in coverage))
        # Beta excluded 2600..2699 but alpha still supplies it: retained.
        self.assertTrue(any(rng.start <= 3012342600 and rng.end >= 3012342699
                            for rng in coverage))

    def test_pending_pieces_are_kept_out_of_coverage(self):
        document = {"sources": [
            {"source_id": "gamma",
             # Valid mobile at both ends, pending general-desc block inside.
             "supply": [{"start": "+4915299999990", "end": "+4915310000005"}]}]}
        catalog = NumberCatalog.from_supply_doc(u("DE"), document)
        review = catalog.review_ranges
        self.assertTrue(review)
        self.assertTrue(all(item.status == RANGE_PENDING for item in review))
        # Pending numbers never enter the valid coverage.
        covered = set()
        for rng in catalog.coverage:
            covered.update(range(rng.start, rng.end + 1))
        for item in review:
            self.assertFalse(any(item.number_range.start <= value <=
                                 item.number_range.end for value in covered))
        totals = catalog.totals()
        self.assertEqual(totals["valid_count"] + totals["pending_count"],
                         totals["net_count"])

    def test_json_round_trip(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            path = handle.name
        try:
            self.catalog.save(path)
            data = json.load(open(path))
            self.assertEqual(data["region_code"], "DE")
            self.assertEqual(data["metadata_version"], "9.0.43")
            self.assertEqual(data["totals"], self.catalog.totals())
            reloaded = NumberCatalog.load(path)
            self.assertEqual(reloaded.totals(), self.catalog.totals())
            self.assertEqual(reloaded.coverage, self.catalog.coverage)
            self.assertEqual([item.to_dict() for item in reloaded.review_ranges],
                             [item.to_dict() for item in self.catalog.review_ranges])
        finally:
            os.unlink(path)

    def test_unknown_schema_version_is_rejected(self):
        data = self.catalog.to_dict()
        data["schema_version"] = 999
        with self.assertRaises(Exception):
            NumberCatalog.from_dict(data)

    def test_command_line_build_and_show(self):
        directory = tempfile.mkdtemp()
        supply_path = os.path.join(directory, "supply.json")
        catalog_path = os.path.join(directory, "catalog.json")
        with open(supply_path, "w") as handle:
            json.dump(_supply_document(), handle)
        self.assertEqual(_main(["build", "--region", "DE",
                                "--input", supply_path,
                                "--output", catalog_path]), 0)
        self.assertTrue(os.path.exists(catalog_path))
        self.assertEqual(_main(["show", "--input", catalog_path]), 0)

    def test_rejects_calling_code_from_another_region(self):
        with self.assertRaises(Exception):
            NumberCatalog.from_supply_doc(u("DE"), {"sources": [
                {"source_id": "foreign",
                 "supply": [{"start": "+443012340000", "end": "+443012340099"}]}]})


if __name__ == "__main__":
    unittest.main()
