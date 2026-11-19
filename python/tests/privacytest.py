"""Unit tests for privacy.py"""

# Copyright (C) 2026 The Libphonenumber Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import itertools
import unittest

from phonenumbers.privacy import (
    NumberCategory, DirectoryRecord, RegionLevel,
    PublishedGroup, UnpublishableRange, ReleasePlan,
    build_release_plan, check_reconciliation)


class PrivacyReleasePlanTest(unittest.TestCase):
    def _two_region_hierarchy(self):
        return [RegionLevel("Berlin", 0), RegionLevel("Munich", 0),
                RegionLevel("Germany", 1, "Berlin"),
                RegionLevel("Germany", 1, "Munich")]

    def test_partition_covers_all_records_and_reconciles(self):
        records = [
            DirectoryRecord("Berlin", NumberCategory.FIXED_LINE, 30),
            DirectoryRecord("Berlin", NumberCategory.MOBILE, 4),
            DirectoryRecord("Munich", NumberCategory.MOBILE, 6),
            DirectoryRecord("Berlin", NumberCategory.UNCERTAIN, 3),
            DirectoryRecord("Munich", NumberCategory.UNCERTAIN, 2),
        ]
        plan = build_release_plan(records, self._two_region_hierarchy(), 5)
        self.assertTrue(plan.publishable)
        self.assertEqual(plan.unpublishable, ())
        self.assertEqual(sum(g.count for g in plan.groups), 45)
        self.assertEqual(plan.total_count, 45)
        self.assertTrue(plan.reconciled)
        self.assertTrue(check_reconciliation(plan))
        # Every published group reaches the threshold.
        self.assertTrue(all(g.count >= 5 for g in plan.groups))

    def test_groups_are_mutually_exclusive(self):
        records = [
            DirectoryRecord("Berlin", NumberCategory.MOBILE, 4),
            DirectoryRecord("Munich", NumberCategory.MOBILE, 6),
            DirectoryRecord("Berlin", NumberCategory.UNCERTAIN, 3),
            DirectoryRecord("Munich", NumberCategory.UNCERTAIN, 7),
        ]
        plan = build_release_plan(records, self._two_region_hierarchy(), 5)
        seen = set()
        for group in plan.groups:
            key = (group.region, group.number_category)
            self.assertNotIn(key, seen)
            seen.add(key)
        # The same region cell cannot be both a merged group and a subgroup.
        for first, second in itertools.combinations(plan.groups, 2):
            self.assertFalse(
                first.region == second.region
                and first.number_category == second.number_category)

    def test_merging_only_happens_when_threshold_requires_it(self):
        records = [DirectoryRecord("Berlin", NumberCategory.MOBILE, 9),
                   DirectoryRecord("Munich", NumberCategory.MOBILE, 8)]
        plan = build_release_plan(records, self._two_region_hierarchy(), 5)
        # Both regions pass the threshold alone, so no merging is needed and
        # the information loss is zero.
        self.assertEqual(plan.total_information_loss, 0)
        self.assertEqual(sorted(g.region for g in plan.groups),
                         ["Berlin", "Munich"])
        self.assertTrue(all(not g.is_merged for g in plan.groups))

    def test_merge_gathers_small_groups_at_one_level(self):
        records = [DirectoryRecord("Berlin", NumberCategory.MOBILE, 4),
                   DirectoryRecord("Munich", NumberCategory.MOBILE, 6)]
        plan = build_release_plan(records, self._two_region_hierarchy(), 5)
        self.assertEqual(len(plan.groups), 1)
        group = plan.groups[0]
        self.assertEqual((group.region, group.count, group.is_merged),
                         ("Germany", 10, True))
        self.assertEqual(group.information_loss, 10)

    def test_uncertain_records_are_never_reclassified_as_mobile(self):
        records = [
            DirectoryRecord("Berlin", NumberCategory.MOBILE, 4),
            DirectoryRecord("Berlin", NumberCategory.UNCERTAIN, 4),
            DirectoryRecord("Munich", NumberCategory.MOBILE, 4),
            DirectoryRecord("Munich", NumberCategory.UNCERTAIN, 4),
        ]
        plan = build_release_plan(records, self._two_region_hierarchy(), 5)
        categories = set(g.number_category for g in plan.groups)
        self.assertEqual(categories,
                         {NumberCategory.MOBILE, NumberCategory.UNCERTAIN})
        mobile_total = sum(g.count for g in plan.groups
                           if g.number_category == NumberCategory.MOBILE)
        uncertain_total = sum(g.count for g in plan.groups
                              if g.number_category
                              == NumberCategory.UNCERTAIN)
        self.assertEqual(mobile_total, 8)
        self.assertEqual(uncertain_total, 8)

    def test_unpublishable_range_withholds_everything(self):
        records = [DirectoryRecord("Berlin", NumberCategory.MOBILE, 2)]
        plan = build_release_plan(records, [RegionLevel("Berlin", 0)], 5)
        self.assertFalse(plan.publishable)
        # No group counts may be released, and no subtotals are produced.
        self.assertEqual(plan.groups, ())
        self.assertEqual(plan.total_information_loss, 0)
        self.assertEqual(len(plan.unpublishable), 1)
        withheld = plan.unpublishable[0]
        self.assertIsInstance(withheld, UnpublishableRange)
        self.assertEqual((withheld.region, withheld.number_category,
                          withheld.count),
                         ("Berlin", NumberCategory.MOBILE, 2))
        self.assertTrue(plan.reconciled)
        self.assertTrue(check_reconciliation(plan))

    def test_partial_failure_withholds_whole_directory(self):
        # Even the most general allowed merge stays below the threshold, so
        # no cut of the hierarchy is publishable and the whole directory is
        # withheld; the blocking raw ranges are reported.
        records = [
            DirectoryRecord("Berlin", NumberCategory.MOBILE, 2),
            DirectoryRecord("Munich", NumberCategory.MOBILE, 2),
        ]
        plan = build_release_plan(records, self._two_region_hierarchy(), 5)
        self.assertFalse(plan.publishable)
        self.assertEqual(plan.groups, ())
        self.assertEqual(
            sorted(item.region for item in plan.unpublishable),
            ["Berlin", "Munich"])
        self.assertTrue(plan.reconciled)

    def test_one_blocking_range_withholds_otherwise_valid_directory(self):
        records = [
            DirectoryRecord("Berlin", NumberCategory.MOBILE, 2),
            DirectoryRecord("Munich", NumberCategory.MOBILE, 20),
        ]
        # Berlin has no allowed merge, so its small group can never reach the
        # threshold; no partition covering every record can be released.
        hierarchy = [RegionLevel("Berlin", 0), RegionLevel("Munich", 0)]
        plan = build_release_plan(records, hierarchy, 5)
        self.assertFalse(plan.publishable)
        self.assertEqual(plan.groups, ())
        self.assertEqual(
            [item.region for item in plan.unpublishable], ["Berlin"])

    def test_blocking_category_withholds_even_safe_categories(self):
        # The fixed-line groups would be safe to publish, but the mobile
        # group can never reach the threshold.  Releasing only the safe
        # categories would leave subtotals that reconstruct the blocked
        # group, so the whole directory is withheld and only the blocking
        # range is reported.
        records = [
            DirectoryRecord("Berlin", NumberCategory.FIXED_LINE, 20),
            DirectoryRecord("Berlin", NumberCategory.MOBILE, 2),
        ]
        plan = build_release_plan(records, [RegionLevel("Berlin", 0)], 5)
        self.assertFalse(plan.publishable)
        self.assertEqual(plan.groups, ())
        self.assertEqual(
            [(item.region, item.number_category)
             for item in plan.unpublishable],
            [("Berlin", NumberCategory.MOBILE)])
        self.assertTrue(plan.reconciled)
        self.assertTrue(check_reconciliation(plan))

    def test_release_cost_breaks_ties_only(self):
        records = [DirectoryRecord("Berlin", NumberCategory.MOBILE, 3),
                   DirectoryRecord("Munich", NumberCategory.MOBILE, 3)]
        cheap = build_release_plan(records, self._two_region_hierarchy(), 5,
                                   {("Germany", 1): 1})
        pricey = build_release_plan(records, self._two_region_hierarchy(), 5,
                                    {("Germany", 1): 9})
        # Information loss is the primary objective and is identical.
        self.assertEqual(cheap.total_information_loss,
                         pricey.total_information_loss)
        self.assertEqual(cheap.total_information_loss, 6)
        self.assertEqual(cheap.total_release_cost, 1)
        self.assertEqual(pricey.total_release_cost, 9)

    def test_uneven_hierarchy_depths(self):
        records = [
            DirectoryRecord("CityX", NumberCategory.MOBILE, 4),
            DirectoryRecord("P1Town", NumberCategory.MOBILE, 4),
            DirectoryRecord("P1Town2", NumberCategory.MOBILE, 2),
        ]
        hierarchy = [
            RegionLevel("CityX", 0), RegionLevel("P1Town", 0),
            RegionLevel("P1Town2", 0),
            RegionLevel("Prov1", 1, "P1Town"),
            RegionLevel("Prov1", 1, "P1Town2"),
            RegionLevel("Country", 2, "Prov1"),
            RegionLevel("Country", 2, "CityX"),
        ]
        plan = build_release_plan(records, hierarchy, 5)
        self.assertTrue(plan.publishable)
        # CityX cannot stop alone (4 < 5) and has no level-1 cell, so the
        # country cut absorbs every region.
        self.assertEqual([(g.region, g.count) for g in plan.groups],
                         [("Country", 10)])

    def test_three_level_cut_prefers_lowest_feasible_merge(self):
        records = [DirectoryRecord("A", NumberCategory.MOBILE, 3),
                   DirectoryRecord("B", NumberCategory.MOBILE, 3),
                   DirectoryRecord("C", NumberCategory.MOBILE, 3)]
        hierarchy = [
            RegionLevel("A", 0), RegionLevel("B", 0),
            RegionLevel("C", 0),
            RegionLevel("AB", 1, "A"), RegionLevel("AB", 1, "B"),
            RegionLevel("ALL", 2, "AB"), RegionLevel("ALL", 2, "C"),
        ]
        plan = build_release_plan(records, hierarchy, 5)
        # AB alone (6) would pass, but C cannot stop alone and has no level-1
        # cell of its own, so the only threshold-safe cut is ALL over 9.
        self.assertEqual([(g.region, g.count) for g in plan.groups],
                         [("ALL", 9)])
        self.assertEqual(plan.total_information_loss, 18)

    def test_empty_category_cell_does_not_force_over_merging(self):
        records = [
            DirectoryRecord("Berlin", NumberCategory.UNCERTAIN, 3),
            DirectoryRecord("Munich", NumberCategory.UNCERTAIN, 3),
            DirectoryRecord("Berlin", NumberCategory.MOBILE, 9),
        ]
        hierarchy = self._two_region_hierarchy()
        plan = build_release_plan(records, hierarchy, 5)
        mobile = [g for g in plan.groups
                  if g.number_category == NumberCategory.MOBILE]
        uncertain = [g for g in plan.groups
                     if g.number_category == NumberCategory.UNCERTAIN]
        # Munich has no mobile records; that empty cell must not drag Berlin's
        # mobile group up into a Germany merge.
        self.assertEqual(
            [(g.region, g.count, g.region_level) for g in mobile],
            [("Berlin", 9, 0)])
        self.assertEqual(
            [(g.region, g.count) for g in uncertain], [("Germany", 6)])
        self.assertEqual(sum(g.count for g in plan.groups), 15)

    def test_region_without_records_is_ignored(self):
        plan = build_release_plan(
            [DirectoryRecord("Berlin", NumberCategory.MOBILE, 9)],
            [RegionLevel("Berlin", 0), RegionLevel("Munich", 0),
             RegionLevel("Germany", 1, "Berlin"),
             RegionLevel("Germany", 1, "Munich")], 5)
        self.assertEqual([(g.region, g.count) for g in plan.groups],
                         [("Berlin", 9)])

    def test_empty_directory(self):
        plan = build_release_plan([], [RegionLevel("Berlin", 0)], 5)
        self.assertTrue(plan.publishable)
        self.assertEqual(plan.groups, ())
        self.assertEqual(plan.total_count, 0)
        self.assertTrue(plan.reconciled)

    def test_no_individual_numbers_are_held_or_returned(self):
        records = [DirectoryRecord("Berlin", NumberCategory.MOBILE, 6)]
        plan = build_release_plan(records, [RegionLevel("Berlin", 0)], 5)
        for group in plan.groups:
            self.assertIsInstance(group, PublishedGroup)
            self.assertFalse(hasattr(group, "numbers"))
            self.assertFalse(hasattr(group, "records"))

    def test_invalid_threshold(self):
        records = [DirectoryRecord("Berlin", NumberCategory.MOBILE, 6)]
        self.assertRaises(ValueError, build_release_plan,
                          records, [RegionLevel("Berlin", 0)], 0)

    def test_unknown_category_rejected(self):
        self.assertRaises(ValueError, build_release_plan,
                          [DirectoryRecord("Berlin", "satellite", 6)],
                          [RegionLevel("Berlin", 0)], 5)

    def test_duplicate_record_rejected(self):
        records = [
            DirectoryRecord("Berlin", NumberCategory.MOBILE, 6),
            DirectoryRecord("Berlin", NumberCategory.MOBILE, 6),
        ]
        self.assertRaises(ValueError, build_release_plan,
                          records, [RegionLevel("Berlin", 0)], 5)

    def test_region_without_level_zero_rejected(self):
        records = [DirectoryRecord("Berlin", NumberCategory.MOBILE, 6)]
        self.assertRaises(ValueError, build_release_plan,
                          records, [RegionLevel("Germany", 1, "Berlin")], 5)

    def test_region_rolling_into_two_cells_rejected(self):
        hierarchy = [
            RegionLevel("Berlin", 0),
            RegionLevel("Germany", 1, "Berlin"),
            RegionLevel("Bavaria", 1, "Berlin"),
        ]
        records = [DirectoryRecord("Berlin", NumberCategory.MOBILE, 6)]
        self.assertRaises(ValueError, build_release_plan,
                          records, hierarchy, 5)

    def test_undeclared_parent_rejected(self):
        hierarchy = [RegionLevel("Berlin", 0),
                     RegionLevel("Germany", 1, "Ghost")]
        records = [DirectoryRecord("Berlin", NumberCategory.MOBILE, 6)]
        self.assertRaises(ValueError, build_release_plan,
                          records, hierarchy, 5)

    def test_negative_release_cost_rejected(self):
        records = [DirectoryRecord("Berlin", NumberCategory.MOBILE, 6)]
        self.assertRaises(ValueError, build_release_plan,
                          records, [RegionLevel("Berlin", 0)], 5,
                          {("Berlin", 0): -1})

    def test_optimality_against_exhaustive_cuts(self):
        # Enumerate every legal cut of a small random hierarchy and confirm
        # the dynamic program picks the minimum (loss, cost) feasible cut.
        import random
        random.seed(17)
        for _ in range(60):
            n = random.randint(1, 4)
            leaves = [("L%d" % i, 0) for i in range(n)]
            root = ("R", 1)
            tree = {root: leaves}
            for leaf in leaves:
                tree[leaf] = []
            leaf_counts = dict((leaf, random.randint(1, 12))
                               for leaf in leaves)
            counts = dict(leaf_counts)
            counts[root] = sum(leaf_counts.values())
            levels = dict((cell, cell[1]) for cell in tree)
            costs = dict((leaf, 0) for leaf in leaves)
            costs[root] = random.randint(0, 5)
            threshold = random.randint(1, 10)

            def cuts(cell):
                count = counts[cell]
                options = []
                if count >= threshold:
                    options.append((count * levels[cell], costs[cell]))
                children = tree[cell]
                if children:
                    per_child = [cuts(child) for child in children]
                    if all(per_child):
                        for combo in itertools.product(*per_child):
                            options.append(
                                (sum(value[0] for value in combo),
                                 sum(value[1] for value in combo)))
                return options

            reference = cuts(root)
            records = [DirectoryRecord(leaf[0], NumberCategory.MOBILE, count)
                       for leaf, count in leaf_counts.items()]
            hierarchy = [RegionLevel(leaf[0], 0) for leaf in leaves]
            hierarchy += [RegionLevel("R", 1, leaf[0]) for leaf in leaves]
            plan = build_release_plan(records, hierarchy, threshold, costs)
            if not reference:
                self.assertFalse(plan.publishable)
                continue
            best = min(reference)
            self.assertTrue(plan.publishable)
            self.assertEqual((plan.total_information_loss,
                              plan.total_release_cost), best)
            self.assertEqual(sum(g.count for g in plan.groups),
                             sum(leaf_counts.values()))


class ReleasePlanTypesTest(unittest.TestCase):
    def test_returned_types(self):
        plan = build_release_plan(
            [DirectoryRecord("Berlin", NumberCategory.MOBILE, 6)],
            [RegionLevel("Berlin", 0)], 5)
        self.assertIsInstance(plan, ReleasePlan)
        self.assertIsInstance(plan.groups, tuple)
        self.assertIsInstance(plan.unpublishable, tuple)
        self.assertIsInstance(plan.groups[0], PublishedGroup)
