#! /usr/bin/env python
# -*- coding: utf-8 -*-

"""
Tests for explicit rooted/unrooted tree split comparison
(``dendropy.calculate.treecompare.compare_tree_bipartitions``).
"""

import unittest

import dendropy
from dendropy.calculate import treecompare
from dendropy.utility import error


class CompareTreeBipartitionsTest(unittest.TestCase):

    def setUp(self):
        self.tns = dendropy.TaxonNamespace()

    def _get_tree(self, newick):
        return dendropy.Tree.get_from_string(
                newick, "newick", taxon_namespace=self.tns)

    def test_child_order_only_difference_is_identical(self):
        tree1 = self._get_tree("((A,B),(C,D));")
        tree2 = self._get_tree("((D,C),(B,A));")
        for is_rooted in (False, True):
            comparison = treecompare.compare_tree_bipartitions(
                    tree1, tree2, is_rooted=is_rooted)
            self.assertTrue(comparison.is_identical)
            self.assertEqual(comparison.symmetric_difference, 0)
            self.assertEqual(comparison.only_in_tree1, [])
            self.assertEqual(comparison.only_in_tree2, [])

    def test_rerooted_tree_difference_depends_on_convention(self):
        # same unrooted topology, different rooting
        tree1 = self._get_tree("((A,B),(C,D));")
        tree2 = self._get_tree("(A,B,(C,D));")
        unrooted = treecompare.compare_tree_bipartitions(
                tree1, tree2, is_rooted=False)
        self.assertTrue(unrooted.is_identical)
        rooted = treecompare.compare_tree_bipartitions(
                tree1, tree2, is_rooted=True)
        self.assertFalse(rooted.is_identical)
        # tree1 has clades {A,B} and {C,D}; tree2 has only {C,D}
        self.assertEqual(len(rooted.only_in_tree1), 1)
        self.assertEqual(len(rooted.only_in_tree2), 0)
        self.assertEqual(
                {t.label for t in rooted.only_in_tree1[0].taxa}, {"A", "B"})
        self.assertEqual(rooted.symmetric_difference, 1)

    def test_unrooted_equivalence_is_not_rooted_equivalence(self):
        # unrooted-identical trees with different roots must not be reported
        # as identical under the rooted convention
        tree1 = self._get_tree("((A,B),(C,D));")
        tree2 = self._get_tree("(C,D,(A,B));")
        self.assertTrue(treecompare.compare_tree_bipartitions(
                tree1, tree2, is_rooted=False).is_identical)
        self.assertFalse(treecompare.compare_tree_bipartitions(
                tree1, tree2, is_rooted=True).is_identical)

    def test_changed_splits_reported_with_source_edges(self):
        tree1 = self._get_tree("((A:1,B:1):2,(C:1,D:1):2);")
        tree2 = self._get_tree("((A:1,C:1):3,(B:1,D:1):4);")
        comparison = treecompare.compare_tree_bipartitions(
                tree1, tree2, is_rooted=False)
        self.assertFalse(comparison.is_identical)
        self.assertEqual(comparison.symmetric_difference, 2)
        self.assertEqual(len(comparison.only_in_tree1), 1)
        self.assertEqual(len(comparison.only_in_tree2), 1)
        record1 = comparison.only_in_tree1[0]
        record2 = comparison.only_in_tree2[0]
        self.assertEqual({t.label for t in record1.taxa}, {"A", "B"})
        self.assertEqual({t.label for t in record2.taxa}, {"A", "C"})
        # source edges come from their own trees
        self.assertTrue(all(edge in tree1.edges() for edge in record1.edges))
        self.assertTrue(all(edge in tree2.edges() for edge in record2.edges))
        # lengths migrate with the edges: under the unrooted convention the
        # two edges flanking each basal bifurcation merge into one split
        self.assertEqual(record1.edge_length, 2.0 + 2.0)
        self.assertEqual(record2.edge_length, 3.0 + 4.0)

    def test_branch_lengths_migrate_with_merged_edges(self):
        # under the unrooted convention, the two edges flanking the basal
        # bifurcation of tree1 map to the same split and their lengths are
        # summed; tree2 induces the same split with a single edge
        tree1 = self._get_tree("((A:1,B:1):2,(C:1,D:1):2);")
        tree2 = self._get_tree("(A:1,B:1,(C:1,D:1):2);")
        comparison = treecompare.compare_tree_bipartitions(
                tree1, tree2, is_rooted=False)
        self.assertTrue(comparison.is_identical)
        self.assertEqual(len(comparison.common), 1)
        record1, record2 = comparison.common[0]
        self.assertEqual(len(record1.edges), 2)
        self.assertEqual(record1.edge_length, 4.0)
        self.assertEqual(len(record2.edges), 1)
        self.assertEqual(record2.edge_length, 2.0)

    def test_root_edge_stored_separately(self):
        tree1 = self._get_tree("((A,B),(C,D)):5;")
        tree2 = self._get_tree("((A,B),(C,D));")
        comparison = treecompare.compare_tree_bipartitions(
                tree1, tree2, is_rooted=False)
        self.assertIs(comparison.tree1_root_edge, tree1.seed_node.edge)
        self.assertIs(comparison.tree2_root_edge, tree2.seed_node.edge)
        self.assertEqual(comparison.tree1_root_edge_length, 5.0)
        self.assertIsNone(comparison.tree2_root_edge_length)
        # the root edge is not reported as a split
        all_edges = []
        for record in comparison.only_in_tree1 + comparison.only_in_tree2:
            all_edges.extend(record.edges)
        for record1, record2 in comparison.common:
            all_edges.extend(record1.edges)
            all_edges.extend(record2.edges)
        self.assertNotIn(tree1.seed_node.edge, all_edges)
        self.assertNotIn(tree2.seed_node.edge, all_edges)

    def test_trees_are_not_modified(self):
        tree1 = self._get_tree("((A:1,B:1):2,(C:1,D:1):2):5;")
        tree2 = self._get_tree("(A:1,B:1,(C:1,D:1):2);")
        before1 = tree1.as_string("newick")
        before2 = tree2.as_string("newick")
        treecompare.compare_tree_bipartitions(tree1, tree2, is_rooted=False)
        treecompare.compare_tree_bipartitions(tree1, tree2, is_rooted=True)
        self.assertEqual(tree1.as_string("newick"), before1)
        self.assertEqual(tree2.as_string("newick"), before2)

    def test_taxon_namespace_identity_checked(self):
        tree1 = self._get_tree("((A,B),(C,D));")
        tree2 = dendropy.Tree.get_from_string("((A,B),(C,D));", "newick")
        self.assertRaises(
                error.TaxonNamespaceIdentityError,
                treecompare.compare_tree_bipartitions,
                tree1, tree2)

    def test_common_splits_pair_source_edges_from_both_trees(self):
        tree1 = self._get_tree("((A:1,B:1):2,(C:1,D:1):2);")
        tree2 = self._get_tree("((B:1,A:1):5,(D:1,C:1):6);")
        comparison = treecompare.compare_tree_bipartitions(
                tree1, tree2, is_rooted=True)
        self.assertTrue(comparison.is_identical)
        self.assertEqual(len(comparison.common), 2)
        lengths1 = sorted(r1.edge_length for r1, r2 in comparison.common)
        lengths2 = sorted(r2.edge_length for r1, r2 in comparison.common)
        self.assertEqual(lengths1, [2.0, 2.0])
        self.assertEqual(lengths2, [5.0, 6.0])
        for record1, record2 in comparison.common:
            self.assertTrue(all(edge in tree1.edges() for edge in record1.edges))
            self.assertTrue(all(edge in tree2.edges() for edge in record2.edges))


if __name__ == "__main__":
    unittest.main()
