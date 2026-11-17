#! /usr/bin/env python
# -*- coding: utf-8 -*-

"""
Tests for Tree.prune_to_leaf_set().
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import dendropy


class PruneToLeafSetTopologyAndDistances(unittest.TestCase):

    tree_str = "((A:1,B:2)i1:3,(C:4,(D:5,E:6)i3:7)i2:8)root;"

    def setUp(self):
        self.tree = dendropy.Tree.get(
            data=self.tree_str,
            schema="newick",
        )

    def patristic_distances(self, tree, labels):
        pdm = dendropy.PhylogeneticDistanceMatrix.from_tree(tree)
        distances = {}
        for i, label1 in enumerate(labels):
            for label2 in labels[i + 1 :]:
                t1 = tree.taxon_namespace.get_taxon(label1)
                t2 = tree.taxon_namespace.get_taxon(label2)
                distances[(label1, label2)] = pdm.patristic_distance(t1, t2)
        return distances

    def test_pruned_tree_structure(self):
        pruned = self.tree.prune_to_leaf_set(["A", "C", "E"])
        self.assertEqual(
            sorted(nd.taxon.label for nd in pruned.leaf_node_iter()),
            ["A", "C", "E"],
        )
        # unifurcations must be compressed
        for nd in pruned.postorder_node_iter():
            self.assertNotEqual(len(nd.child_nodes()), 1)
        # suppressed node labels are dropped, not carried over
        pruned_labels = [nd.label for nd in pruned.postorder_node_iter()]
        self.assertNotIn("i1", pruned_labels)
        self.assertNotIn("i3", pruned_labels)
        # kept internal node labels are retained
        self.assertIn("i2", pruned_labels)

    def test_distances_between_retained_leaves_preserved(self):
        retained = ["A", "C", "E"]
        original_distances = self.patristic_distances(self.tree, retained)
        pruned = self.tree.prune_to_leaf_set(retained)
        pruned_distances = self.patristic_distances(pruned, retained)
        for pair in original_distances:
            self.assertAlmostEqual(
                original_distances[pair],
                pruned_distances[pair],
                msg="distance changed for {}".format(pair),
            )

    def test_edge_lengths_combined_along_path(self):
        pruned = self.tree.prune_to_leaf_set(["A", "C", "E"])
        lengths = {}
        for nd in pruned.leaf_node_iter():
            lengths[nd.taxon.label] = nd.edge.length
        # A: 1 (A) + 3 (i1); C: 4; E: 6 + 7 (i3)
        self.assertAlmostEqual(lengths["A"], 4.0)
        self.assertAlmostEqual(lengths["C"], 4.0)
        self.assertAlmostEqual(lengths["E"], 13.0)
        i2 = pruned.find_node_with_label("i2")
        self.assertAlmostEqual(i2.edge.length, 8.0)

    def test_distance_from_root_preserved(self):
        pruned = self.tree.prune_to_leaf_set(["A", "C", "E"])
        for label in ("A", "C", "E"):
            nd0 = self.tree.find_node_with_taxon_label(label)
            nd1 = pruned.find_node_with_taxon_label(label)
            self.assertAlmostEqual(
                nd0.distance_from_root(),
                nd1.distance_from_root(),
            )

    def test_original_tree_not_modified(self):
        before = self.tree.as_string(schema="newick")
        self.tree.prune_to_leaf_set(["A", "C", "E"])
        self.assertEqual(len(self.tree.leaf_nodes()), 5)
        self.assertEqual(self.tree.as_string(schema="newick"), before)


class PruneToLeafSetSupportAndAnnotations(unittest.TestCase):

    def setUp(self):
        self.tree = dendropy.Tree.get(
            data="((A:1,B:2)i1:3,(C:4,(D:5,E:6)i3:7)i2:8)root;",
            schema="newick",
        )
        for nd in self.tree.postorder_node_iter():
            nd.edge.label = "support@{}".format(nd.label or nd.taxon.label)
            nd.edge.annotations.add_new("note", "annotation@{}".format(nd.edge.label))
            nd.edge.comments.append("comment@{}".format(nd.edge.label))

    def test_merged_edges_do_not_combine_support_or_annotations(self):
        pruned = self.tree.prune_to_leaf_set(["A", "C", "E"])
        nd_a = pruned.find_node_with_taxon_label("A")
        # merged from two edges: support/annotations must not be combined
        self.assertIsNone(nd_a.edge.label)
        self.assertEqual(len(nd_a.edge.annotations), 0)
        self.assertEqual(len(nd_a.edge.comments), 0)
        nd_e = pruned.find_node_with_taxon_label("E")
        self.assertIsNone(nd_e.edge.label)
        self.assertEqual(len(nd_e.edge.annotations), 0)
        self.assertEqual(len(nd_e.comments), 0)

    def test_unmerged_edges_keep_support(self):
        pruned = self.tree.prune_to_leaf_set(["A", "C", "E"])
        nd_c = pruned.find_node_with_taxon_label("C")
        self.assertEqual(nd_c.edge.label, "support@C")
        nd_i2 = pruned.find_node_with_label("i2")
        self.assertEqual(nd_i2.edge.label, "support@i2")

    def test_original_support_recoverable_through_provenance(self):
        pruned = self.tree.prune_to_leaf_set(["A", "C", "E"])
        nd_a = pruned.find_node_with_taxon_label("A")
        self.assertEqual(
            [e.label for e in nd_a.edge.source_edges],
            ["support@A", "support@i1"],
        )
        self.assertEqual(
            [e.annotations["note"].value for e in nd_a.edge.source_edges],
            ["annotation@support@A", "annotation@support@i1"],
        )


class PruneToLeafSetProvenance(unittest.TestCase):

    def setUp(self):
        self.tree = dendropy.Tree.get(
            data="((A:1,B:2)i1:3,(C:4,(D:5,E:6)i3:7)i2:8)root;",
            schema="newick",
        )
        self.pruned = self.tree.prune_to_leaf_set(["A", "C", "E"])

    def test_every_new_edge_references_original_path(self):
        for edge in self.pruned.postorder_edge_iter():
            self.assertTrue(hasattr(edge, "source_edges"))
            self.assertTrue(len(edge.source_edges) >= 1)
            for source_edge in edge.source_edges:
                self.assertIsNotNone(source_edge.head_node)
                self.assertIn(source_edge, list(self.tree.postorder_edge_iter()))

    def test_provenance_path_order_and_length(self):
        nd_a = self.pruned.find_node_with_taxon_label("A")
        source_edges = nd_a.edge.source_edges
        self.assertEqual(len(source_edges), 2)
        # ordered from the retained descendant upward
        self.assertIs(source_edges[0], self.tree.find_node_with_taxon_label("A").edge)
        self.assertIs(source_edges[1], self.tree.find_node_with_label("i1").edge)
        # length of the new edge is the sum along the original path
        self.assertAlmostEqual(
            nd_a.edge.length,
            sum(e.length for e in source_edges),
        )
        nd_c = self.pruned.find_node_with_taxon_label("C")
        self.assertEqual(len(nd_c.edge.source_edges), 1)
        self.assertIs(
            nd_c.edge.source_edges[0],
            self.tree.find_node_with_taxon_label("C").edge,
        )

    def test_provenance_can_be_disabled(self):
        pruned = self.tree.prune_to_leaf_set(["A", "C", "E"], provenance_attr_name=None)
        for edge in pruned.postorder_edge_iter():
            self.assertFalse(hasattr(edge, "source_edges"))


class PruneToLeafSetDegenerateCases(unittest.TestCase):

    def setUp(self):
        self.tree = dendropy.Tree.get(
            data="((A:1,B:2)i1:3,(C:4,(D:5,E:6)i3:7)i2:8)root;",
            schema="newick",
        )

    def test_empty_leaf_set_has_explicit_representation(self):
        pruned = self.tree.prune_to_leaf_set([])
        self.assertIsNone(pruned.seed_node)

    def test_single_leaf_has_explicit_representation(self):
        pruned = self.tree.prune_to_leaf_set(["D"])
        self.assertIsNotNone(pruned.seed_node)
        self.assertTrue(pruned.seed_node.is_leaf())
        self.assertEqual(pruned.seed_node.taxon.label, "D")
        self.assertEqual(len(pruned.leaf_nodes()), 1)
        # seed edge records the full original path to the root
        self.assertAlmostEqual(pruned.seed_node.edge.length, 5.0 + 7.0 + 8.0)
        self.assertEqual(len(pruned.seed_node.edge.source_edges), 4)
        self.assertIs(
            pruned.seed_node.edge.source_edges[-1],
            self.tree.seed_node.edge,
        )

    def test_unmatched_leaves_raise_error(self):
        with self.assertRaises(ValueError):
            self.tree.prune_to_leaf_set(["A", "Z"])
        with self.assertRaises(TypeError):
            self.tree.prune_to_leaf_set([42])


class PruneToLeafSetLeafSpecification(unittest.TestCase):

    def setUp(self):
        self.tree = dendropy.Tree.get(
            data="((A:1,B:2)i1:3,(C:4,(D:5,E:6)i3:7)i2:8)root;",
            schema="newick",
        )

    def retained_labels(self, pruned):
        return sorted(nd.taxon.label for nd in pruned.leaf_node_iter())

    def test_specify_by_taxon(self):
        taxa = [t for t in self.tree.taxon_namespace if t.label in ("B", "D")]
        pruned = self.tree.prune_to_leaf_set(taxa)
        self.assertEqual(self.retained_labels(pruned), ["B", "D"])

    def test_specify_by_node(self):
        nodes = [
            self.tree.find_node_with_taxon_label("B"),
            self.tree.find_node_with_taxon_label("D"),
        ]
        pruned = self.tree.prune_to_leaf_set(nodes)
        self.assertEqual(self.retained_labels(pruned), ["B", "D"])

    def test_specify_by_label_mixed(self):
        taxon_b = self.tree.taxon_namespace.get_taxon("B")
        pruned = self.tree.prune_to_leaf_set([taxon_b, "D"])
        self.assertEqual(self.retained_labels(pruned), ["B", "D"])

    def test_full_leaf_set_round_trip(self):
        labels = [t.label for t in self.tree.taxon_namespace]
        pruned = self.tree.prune_to_leaf_set(labels)
        self.assertEqual(self.retained_labels(pruned), sorted(labels))
        pdm0 = dendropy.PhylogeneticDistanceMatrix.from_tree(self.tree)
        pdm1 = dendropy.PhylogeneticDistanceMatrix.from_tree(pruned)
        taxa = list(self.tree.taxon_namespace)
        for i, t1 in enumerate(taxa):
            for t2 in taxa[i + 1 :]:
                self.assertAlmostEqual(
                    pdm0.patristic_distance(t1, t2),
                    pdm1.patristic_distance(t1, t2),
                )


if __name__ == "__main__":
    unittest.main()
