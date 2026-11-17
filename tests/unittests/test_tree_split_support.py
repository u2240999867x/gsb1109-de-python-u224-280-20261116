#! /usr/bin/env python
# -*- coding: utf-8 -*-

##############################################################################
##  DendroPy Phylogenetic Computing Library.
##
##  Copyright 2010-2026 Jeet Sukumaran, Mark T. Holder, and Matthew Andres Moreno.
##  All rights reserved.
##
##  See "LICENSE.rst" for terms and conditions of usage.
##
##  If you use this work or any portion thereof in published work,
##  please cite it as:
##
##     Moreno, M. A., Holder, M. T., & Sukumaran, J. (2024). DendroPy 5: a
##     mature Python library for phylogenetic computing. Journal of Open
##     Source Software, 9(101), 6943, https://doi.org/10.21105/joss.06943
##
##############################################################################

"""
Tests for resolution-aware split support.
"""

import unittest
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))
from support import pathmap

import dendropy
from dendropy.calculate import treecompare


def _trees_from_newick(newick, taxon_namespace=None):
    tns = taxon_namespace or dendropy.TaxonNamespace()
    return dendropy.TreeList.get_from_string(
            newick,
            schema="newick",
            taxon_namespace=tns)


class SplitSupportClassificationTest(unittest.TestCase):

    def setUp(self):
        self.trees = _trees_from_newick("""\
                [&U] ((A,B),(C,D));
                [&U] ((A,C),(B,D));
                [&U] (A,B,C,D);
                [&U] ((A,B),C,D);
                """)
        self.candidates = [["A", "B"]]

    def _split(self, analysis):
        group = analysis.primary_group
        self.assertEqual(len(group), 1)
        return next(iter(group))

    def test_supported_unresolved_and_conflicting_are_distinct(self):
        analysis = treecompare.summarize_split_support(
                self.trees, candidates=self.candidates)
        split = self._split(analysis)
        self.assertEqual(split.status_by_tree, {
            0: treecompare.SPLIT_SUPPORT_SUPPORTED,
            1: treecompare.SPLIT_SUPPORT_CONFLICTING,
            2: treecompare.SPLIT_SUPPORT_UNRESOLVED,
            3: treecompare.SPLIT_SUPPORT_SUPPORTED,
            })
        self.assertEqual(split.supported_count, 2)
        self.assertEqual(split.unresolved_count, 1)
        self.assertEqual(split.conflicting_count, 1)
        self.assertEqual(split.informative_count, 3)
        self.assertEqual(split.comparable_count, 4)

    def test_unresolved_tree_not_in_support_denominator(self):
        analysis = treecompare.summarize_split_support(
                self.trees, candidates=self.candidates)
        split = self._split(analysis)
        # 2 supported / (2 supported + 1 conflicting); polytomy excluded.
        self.assertAlmostEqual(split.support_frequency, 2.0 / 3.0)
        self.assertAlmostEqual(split.observed_frequency, 2.0 / 4.0)
        self.assertAlmostEqual(split.resolution_frequency, 3.0 / 4.0)

    def test_contested_split_status_follows_majority(self):
        trees = _trees_from_newick("""\
                [&U] ((A,B),(C,D));
                [&U] ((A,B),(C,D));
                [&U] ((A,C),(B,D));
                """)
        analysis = treecompare.summarize_split_support(
                trees, candidates=[["A", "B"]])
        split = next(iter(analysis.primary_group))
        self.assertTrue(split.is_contested)
        self.assertAlmostEqual(split.support_frequency, 2.0 / 3.0)
        self.assertEqual(split.support_status,
                treecompare.SPLIT_SUPPORT_SUPPORTED)
        self.assertEqual(split.conflicting_count, 1)

    def test_soft_conflict_is_reported_as_conflicting_when_outnumbered(self):
        trees = _trees_from_newick("""\
                [&U] ((A,B),(C,D));
                [&U] ((A,C),(B,D));
                [&U] ((A,C),(B,D));
                """)
        analysis = treecompare.summarize_split_support(
                trees, candidates=[["A", "B"]])
        split = next(iter(analysis.primary_group))
        self.assertAlmostEqual(split.support_frequency, 1.0 / 3.0)
        self.assertEqual(split.support_status,
                treecompare.SPLIT_SUPPORT_CONFLICTING)

    def test_observed_frequency_is_not_support(self):
        analysis = treecompare.summarize_split_support(
                self.trees, candidates=self.candidates)
        split = self._split(analysis)
        self.assertNotAlmostEqual(
                split.observed_frequency,
                split.support_frequency)

    def test_all_unresolved_has_no_support_frequency(self):
        trees = _trees_from_newick("(A,B,C,D);(A,B,C,D);")
        analysis = treecompare.summarize_split_support(
                trees, candidates=[["A", "B"]])
        split = self._split(analysis)
        self.assertEqual(split.unresolved_count, 2)
        self.assertEqual(split.support_status, treecompare.SPLIT_SUPPORT_UNRESOLVED)
        self.assertIsNone(split.support_frequency)
        self.assertEqual(split.resolution_frequency, 0.0)
        self.assertEqual(split.observed_frequency, 0.0)

    def test_classify_split_on_tree(self):
        bipartition = dendropy.Bipartition(
                leafset_bitmask=self.trees.taxon_namespace.taxa_bitmask(
                    labels=["A", "B"]),
                tree_leafset_bitmask=self.trees.taxon_namespace.all_taxa_bitmask(),
                is_rooted=False)
        self.assertEqual(
                treecompare.classify_split_on_tree(self.trees[0], bipartition),
                treecompare.SPLIT_SUPPORT_SUPPORTED)
        self.assertEqual(
                treecompare.classify_split_on_tree(self.trees[1], bipartition),
                treecompare.SPLIT_SUPPORT_CONFLICTING)
        self.assertEqual(
                treecompare.classify_split_on_tree(self.trees[2], bipartition),
                treecompare.SPLIT_SUPPORT_UNRESOLVED)

    def test_classify_split_rejects_out_of_tree_taxa(self):
        other_namespace = dendropy.TaxonNamespace()
        other_tree = dendropy.Tree.get(
                data="[&U] ((A,B),(C,D,E));",
                schema="newick",
                taxon_namespace=other_namespace)
        candidate = dendropy.Bipartition(
                leafset_bitmask=other_namespace.taxa_bitmask(labels=["E", "A"]),
                tree_leafset_bitmask=other_namespace.all_taxa_bitmask(),
                is_rooted=False)
        small_tree = dendropy.Tree.get(
                data="[&U] ((A,B),(C,D));",
                schema="newick",
                taxon_namespace=other_namespace)
        self.assertRaises(
                ValueError,
                treecompare.classify_split_on_tree,
                small_tree,
                candidate)

    def test_candidate_off_tree_is_conflicting_not_unresolved(self):
        # On ((A,B),C,D), a candidate grouping of (A,C) is contradicted by
        # the resolved (A,B) split, so it must not be reported as unresolved.
        analysis = treecompare.summarize_split_support(
                self.trees, candidates=[["A", "C"]])
        split = self._split(analysis)
        self.assertEqual(split.status_by_tree[2],
                treecompare.SPLIT_SUPPORT_UNRESOLVED)
        self.assertEqual(split.status_by_tree[0],
                treecompare.SPLIT_SUPPORT_CONFLICTING)
        self.assertEqual(split.status_by_tree[3],
                treecompare.SPLIT_SUPPORT_CONFLICTING)

    def test_trivial_candidates_excluded_by_default(self):
        analysis = treecompare.summarize_split_support(
                self.trees, candidates=[["A"]])
        self.assertEqual(len(analysis.primary_group.splits), 0)
        analysis = treecompare.summarize_split_support(
                self.trees, candidates=[["A"]], include_trivial=True)
        split = next(iter(analysis.primary_group))
        self.assertEqual(split.leafset_labels, ("A",))
        self.assertEqual(split.supported_count, 4)

    def test_rooted_and_unrooted_are_rejected_when_mixed(self):
        trees = _trees_from_newick("""\
                [&U] ((A,B),(C,D));
                [&R] ((A,B),(C,D));
                """)
        with self.assertRaises(dendropy.utility.error.MixedRootingError):
            treecompare.summarize_split_support(trees)


class SplitSupportLeafSetGroupingTest(unittest.TestCase):

    def setUp(self):
        self.trees = _trees_from_newick("""\
                [&U] ((A,B),(C,D));
                [&U] (A,B,(C,D));
                [&U] (A,B,C,D);
                [&U] ((A,C),(B,D));
                [&U] ((A,B),(C,(D,E)));
                [&U] ((A,B),C,(D,E));
                [&U] ((A,B),(C,D),(E,F));
                """)

    def test_groups_by_exact_leaf_set(self):
        analysis = treecompare.summarize_split_support(
                self.trees, candidates=[["A", "B"]])
        self.assertEqual(analysis.num_groups, 3)
        self.assertEqual(
                [group.num_trees for group in analysis],
                [4, 2, 1])
        self.assertEqual(
                [group.leafset_labels for group in analysis],
                [("A", "B", "C", "D"),
                    ("A", "B", "C", "D", "E"),
                    ("A", "B", "C", "D", "E", "F")])

    def test_missing_taxa_are_recorded_not_counted(self):
        analysis = treecompare.summarize_split_support(
                self.trees, candidates=[["A", "B"]])
        group = analysis.primary_group
        self.assertEqual(sorted(group.leafset_labels), ["A", "B", "C", "D"])
        self.assertEqual(group.num_trees, 4)
        self.assertEqual(group.incomparable_count, 3)
        scope_by_index = {
                scope.tree_index: scope
                for scope in group.incomparable}
        self.assertEqual(scope_by_index[4].extra_taxon_labels, ("E",))
        self.assertEqual(scope_by_index[5].extra_taxon_labels, ("E",))
        self.assertEqual(scope_by_index[6].extra_taxon_labels, ("E", "F"))
        for scope in group.incomparable:
            self.assertEqual(scope.missing_taxon_labels, ())

        split = next(iter(group))
        self.assertEqual(split.comparable_count, 4)
        self.assertEqual(split.incomparable_count, 3)
        self.assertEqual(split.total_count, 7)
        self.assertNotIn(4, split.status_by_tree)
        self.assertNotIn(5, split.status_by_tree)
        self.assertNotIn(6, split.status_by_tree)

    def test_incomparable_counts_do_not_affect_support(self):
        analysis = treecompare.summarize_split_support(
                self.trees, candidates=[["A", "B"]])
        group = analysis.primary_group
        split = next(iter(group))
        self.assertEqual(split.supported_count, 2)
        self.assertEqual(split.unresolved_count, 1)
        self.assertEqual(split.conflicting_count, 1)
        self.assertAlmostEqual(split.support_frequency, 2.0 / 3.0)
        self.assertAlmostEqual(split.resolution_frequency, 3.0 / 4.0)
        self.assertAlmostEqual(split.observed_frequency, 2.0 / 4.0)

    def test_analysis_exposes_primary_incomparable_scope(self):
        analysis = treecompare.summarize_split_support(
                self.trees, candidates=[["A", "B"]])
        self.assertEqual(analysis.incomparable_tree_indices, (4, 5, 6))
        incomparable = {
                record.tree_index: record
                for record in analysis.incomparable}
        self.assertEqual(
                incomparable[6].extra_taxon_labels,
                ("E", "F"))
        self.assertEqual(
                incomparable[4].missing_taxon_labels,
                ())

    def test_candidate_not_contained_in_group_is_skipped(self):
        analysis = treecompare.summarize_split_support(
                self.trees, candidates=[["E", "F"]])
        primary = analysis.primary_group
        self.assertEqual(len(primary.splits), 0)
        self.assertEqual(len(primary.incomparable_candidates), 1)
        self.assertEqual(
                primary.incomparable_candidates[0]["reason"],
                "candidate includes taxa outside group leaf set")
        three_leaf_group = analysis[
                analysis.leafsets[1]]
        self.assertEqual(len(three_leaf_group.splits), 0)
        six_leaf_group = analysis[
                analysis.leafsets[2]]
        self.assertEqual(len(six_leaf_group.splits), 1)

    def test_incomparable_records_missing_leaves_in_both_directions(self):
        trees = _trees_from_newick("""\
                [&U] ((A,B),(C,D));
                [&U] ((A,B),(C,D),E);
                """)
        analysis = treecompare.summarize_split_support(
                trees, candidates=[["A", "B"]])
        four_leaf_group = analysis.groups[analysis.leafsets[0]]
        five_leaf_group = analysis.groups[analysis.leafsets[1]]
        self.assertEqual(
                [(scope.tree_index,
                    scope.missing_taxon_labels,
                    scope.extra_taxon_labels)
                    for scope in four_leaf_group.incomparable],
                [(1, (), ("E",))])
        self.assertEqual(
                [(scope.tree_index,
                    scope.missing_taxon_labels,
                    scope.extra_taxon_labels)
                    for scope in five_leaf_group.incomparable],
                [(0, ("E",), ())])

    def test_consensus_tree_covers_only_group_leaf_set(self):
        analysis = treecompare.summarize_split_support(
                self.trees, candidates=[["A", "B"]])
        group = analysis.primary_group
        tree = group.consensus_tree()
        labels = sorted(leaf.taxon.label for leaf in tree.leaf_node_iter())
        self.assertEqual(labels, ["A", "B", "C", "D"])
        tree.encode_bipartitions()
        self.assertEqual(
                len([b for b in tree.bipartition_encoding if not b.is_trivial()]),
                1)


class SplitComparisonPairwiseTest(unittest.TestCase):

    def setUp(self):
        self.taxon_namespace = dendropy.TaxonNamespace()

    def _tree(self, newick):
        return dendropy.Tree.get(
                data="[&U] " + newick,
                schema="newick",
                taxon_namespace=self.taxon_namespace)

    def test_shared_leaf_set_is_used(self):
        reference = self._tree("((A,B),(C,D),E);")
        comparison = self._tree("((A,B),C,(D,F));")
        result = treecompare.compare_splits(reference, comparison)
        self.assertEqual(
                sorted(t.label for t in result.shared_leafset),
                ["A", "B", "C", "D"])
        self.assertEqual(result.reference_only_taxon_labels, ("E",))
        self.assertEqual(result.comparison_only_taxon_labels, ("F",))
        self.assertTrue(result.is_comparable)

    def test_missing_leaves_do_not_create_conflict(self):
        reference = self._tree("((A,B),(C,D),E);")
        comparison = self._tree("((A,B),C,(D,F));")
        result = treecompare.compare_splits(reference, comparison)
        split = next(iter(result.group))
        self.assertEqual(split.leafset_labels, ("A", "B"))
        self.assertEqual(split.conflicting_count, 0)
        self.assertTrue(split.is_supported)

    def test_pairwise_unresolved_polytomy(self):
        reference = self._tree("((A,B),(C,D));")
        comparison = self._tree("(A,B,C,D);")
        result = treecompare.compare_splits(reference, comparison)
        split = next(iter(result.group))
        self.assertEqual(split.unresolved_count, 1)
        self.assertEqual(split.supported_count, 1)
        self.assertEqual(result.status_by_split[split.split_bitmask],
                treecompare.SPLIT_SUPPORT_UNRESOLVED)
        self.assertIn(split, result.unresolved)
        self.assertNotIn(split, result.supported)

    def test_pairwise_conflicting_topology(self):
        reference = self._tree("((A,B),(C,D));")
        comparison = self._tree("((A,C),(B,D));")
        result = treecompare.compare_splits(reference, comparison)
        split = next(s for s in result.group if s.leafset_labels == ("A", "B"))
        self.assertEqual(split.supported_count, 1)
        self.assertEqual(split.conflicting_count, 1)
        self.assertEqual(split.support_frequency, 0.5)
        self.assertEqual(result.status_by_split[split.split_bitmask],
                treecompare.SPLIT_SUPPORT_CONFLICTING)
        self.assertIn(split, result.conflicting)

    def test_pairwise_too_few_shared_taxa(self):
        reference = self._tree("((A,B),C);")
        comparison = self._tree("((A,B),D);")
        result = treecompare.compare_splits(reference, comparison)
        self.assertFalse(result.is_comparable)
        self.assertIsNone(result.group)
        self.assertEqual(
                sorted(t.label for t in result.shared_leafset),
                ["A", "B"])


class SplitSupportRealDataTest(unittest.TestCase):

    def test_multifurcating_collection_matches_regular_frequency(self):
        trees = dendropy.TreeList.get_from_path(
                pathmap.tree_source_path("pythonidae.mb.run1.t"),
                schema="nexus")
        labels = ["Liasis olivaceus", "Apodora papuana"]
        analysis = trees.split_support(candidates=[labels])
        split = next(iter(analysis.primary_group))
        self.assertAlmostEqual(split.support_frequency, 0.346534653465)
        self.assertEqual(split.unresolved_count, 0)
        self.assertAlmostEqual(split.observed_frequency, split.support_frequency)

    def test_cetaceans_polytomies_are_unresolved_not_zero(self):
        trees = dendropy.TreeList.get_from_path(
                pathmap.tree_source_path("cetaceans.mb.no-clock.mcmc.trees"),
                schema="nexus")
        self.assertTrue(all(
            tree.is_rooted is False for tree in trees))
        self.assertTrue(all(
            any(node.num_child_nodes() > 2 for node in tree.postorder_internal_node_iter())
            for tree in trees))
        analysis = trees.split_support()
        self.assertEqual(analysis.num_groups, 1)
        resolved = [
                split for split in analysis.primary_group
                if split.informative_count > 0]
        unresolved_only = [
                split for split in analysis.primary_group
                if split.informative_count == 0]
        self.assertTrue(resolved)
        for split in resolved:
            # A resolved split's frequency must not be diluted by
            # polytomies that leave it unresolved.
            self.assertAlmostEqual(
                    split.support_frequency,
                    float(split.supported_count) / split.informative_count)
            self.assertGreaterEqual(
                    split.support_frequency,
                    split.observed_frequency)
        for split in unresolved_only:
            self.assertIsNone(split.support_frequency)
            self.assertEqual(split.support_status,
                    treecompare.SPLIT_SUPPORT_UNRESOLVED)

    def test_unresolved_is_not_zero_support_on_multifurcating_data(self):
        trees = _trees_from_newick("""\
                [&U] ((A,B),(C,D));
                [&U] (A,B,C,D);
                [&U] (A,B,C,D);
                [&U] (A,B,C,D);
                """)
        analysis = trees.split_support(candidates=[["A", "B"]])
        split = next(iter(analysis.primary_group))
        self.assertAlmostEqual(split.support_frequency, 1.0)
        self.assertAlmostEqual(split.observed_frequency, 0.25)
        self.assertEqual(split.unresolved_count, 3)


if __name__ == "__main__":
    unittest.main()
