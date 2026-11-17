#! /usr/bin/env python
# -*- coding: utf-8 -*-

"""
Tests for dendropy.calculate.treerooting: enumeration of root placements
for trees of unknown rooting under outgroup and ancestor constraints.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import dendropy
from dendropy.calculate import treerooting


def _tree(data="(A,B,(C,(D,E)));"):
    return dendropy.Tree.get(data=data, schema="newick")


def _internal_nodes(tree):
    return [nd for nd in tree.preorder_node_iter() if not nd.is_leaf()]


class RootPositionEnumerationTestCase(unittest.TestCase):
    def test_all_valid_positions_returned_not_just_first_outgroup(self):
        # unrooted topology: seed(A, B, Q), Q(C, R), R(D, E)
        tree = _tree()
        positions = treerooting.find_root_positions(tree, outgroups=[["A", "B"]])
        # valid: nodes Q, R and edges S-Q, Q-C, Q-R, R-D, R-E
        self.assertEqual(len(positions), 7)
        kinds = [p.kind for p in positions]
        self.assertEqual(kinds.count(treerooting.RootPosition.NODE), 2)
        self.assertEqual(kinds.count(treerooting.RootPosition.EDGE), 5)
        # every position is distinct
        targets = [(p.kind, id(p.target)) for p in positions]
        self.assertEqual(len(set(targets)), len(targets))

    def test_multiple_outgroups_are_intersected(self):
        tree = _tree()
        positions = treerooting.find_root_positions(
            tree, outgroups=[["A", "B"], ["D", "E"]]
        )
        # from the 7 placements valid for {A,B}, root at R and roots on
        # the R-D / R-E edges separate D from E
        self.assertEqual(len(positions), 4)
        kinds = [p.kind for p in positions]
        self.assertEqual(kinds.count(treerooting.RootPosition.NODE), 1)
        self.assertEqual(kinds.count(treerooting.RootPosition.EDGE), 3)

    def test_outgroups_accept_taxon_objects(self):
        tree = _tree()
        tns = tree.taxon_namespace
        positions = treerooting.find_root_positions(
            tree, outgroups=[[tns.get_taxon("A"), tns.get_taxon("B")]]
        )
        self.assertEqual(len(positions), 7)

    def test_unknown_outgroup_taxon_raises(self):
        tree = _tree()
        self.assertRaises(
            ValueError,
            treerooting.find_root_positions,
            tree,
            outgroups=[["A", "Z"]],
        )

    def test_empty_outgroup_raises(self):
        tree = _tree()
        self.assertRaises(
            ValueError,
            treerooting.find_root_positions,
            tree,
            outgroups=[[]],
        )

    def test_ancestor_constraint(self):
        tree = _tree()
        internal = _internal_nodes(tree)
        r_node = None
        for nd in internal:
            leaf_labels = {lf.taxon.label for lf in nd.leaf_iter()}
            if leaf_labels == {"D", "E"}:
                r_node = nd
        self.assertIsNotNone(r_node)
        positions = treerooting.find_root_positions(
            tree, ancestor_constraints=[(r_node, ["D", "E"])]
        )
        # 10 candidate placements in total; the two edges between R and
        # its children D and E put the root between R and its required
        # descendants
        self.assertEqual(len(positions), 8)
        # placements on edges R-D and R-E must be excluded
        excluded_edges = {
            nd.edge for nd in tree.leaf_node_iter() if nd.taxon.label in ("D", "E")
        }
        used_edges = {p.target for p in positions if p.kind == p.EDGE}
        self.assertTrue(excluded_edges.isdisjoint(used_edges))

    def test_ancestor_constraint_node_not_in_tree_raises(self):
        tree = _tree()
        other = _tree()
        foreign = _internal_nodes(other)[0]
        self.assertRaises(
            ValueError,
            treerooting.find_root_positions,
            tree,
            ancestor_constraints=[(foreign, ["D"])],
        )

    def test_original_tree_not_modified(self):
        tree = _tree("(A:1.0,B:2.0,(C:3.0,(D:4.0,E:5.0):6.0):7.0);")
        before = tree.as_string("newick")
        treerooting.find_root_positions(
            tree,
            outgroups=[["A", "B"]],
            ancestor_constraints=[(_internal_nodes(tree)[2], ["D"])],
            refinable_nodes="all",
        )
        self.assertEqual(tree.as_string("newick"), before)


class RefinementClassificationTestCase(unittest.TestCase):
    def test_no_refinement_allowed_gives_conflict(self):
        tree = _tree("(A,B,C,(D,E));")
        self.assertRaises(
            treerooting.RootingConflictError,
            treerooting.find_root_positions,
            tree,
            outgroups=[["A", "B"]],
        )

    def test_refinement_dependent_positions(self):
        tree = _tree("(A,B,C,(D,E));")
        positions = treerooting.find_root_positions(
            tree, outgroups=[["A", "B"]], refinable_nodes="all"
        )
        # edges S-A and S-B isolate one of the outgroup taxa: infeasible
        self.assertEqual(len(positions), 6)
        for p in positions:
            self.assertFalse(p.is_certain)
            self.assertTrue(p.requires_refinement)
            self.assertEqual(len(p.required_refinements), 1)
            self.assertIs(p.required_refinements[0], tree.seed_node)

    def test_refinement_scope_limited_by_caller(self):
        tree = _tree("(A,B,C,(D,E));")
        seed = tree.seed_node
        positions = treerooting.find_root_positions(
            tree, outgroups=[["A", "B"]], refinable_nodes=[seed]
        )
        self.assertEqual(len(positions), 6)
        # excluding the seed from the refinable scope leaves nothing,
        # since only the seed polytomy ever needs resolution here
        self.assertRaises(
            treerooting.RootingConflictError,
            treerooting.find_root_positions,
            tree,
            outgroups=[["A", "B"]],
            refinable_nodes=lambda nd: False,
        )

    def test_certain_and_refinement_dependent_are_separated(self):
        tree = _tree("(A,B,C,(D,E));")
        positions = treerooting.find_root_positions(
            tree, outgroups=[["D", "E"]], refinable_nodes="all"
        )
        certain = [p for p in positions if p.is_certain]
        conditional = [p for p in positions if not p.is_certain]
        self.assertEqual(len(certain), 5)
        self.assertEqual(len(conditional), 1)
        # only rooting at the (D,E) node's parent of D,E itself -- i.e.
        # at node Q with children D, E, S-side -- needs refinement
        self.assertEqual(conditional[0].kind, treerooting.RootPosition.NODE)
        self.assertEqual(len(conditional[0].required_refinements), 1)
        # certain placements need no refinement
        for p in certain:
            self.assertEqual(p.required_refinements, ())

    def test_impossible_outgroup_conflicts_even_with_refinement(self):
        tree = _tree("(A,B,C,(D,E));")
        try:
            treerooting.find_root_positions(
                tree, outgroups=[["A", "D"]], refinable_nodes="all"
            )
        except treerooting.RootingConflictError as e:
            self.assertEqual(e.report["evaluated_positions"], 8)
            self.assertEqual(e.report["outgroup_rejections"], [8])
        else:
            self.fail("expected RootingConflictError")

    def test_crossing_outgroup_refinements_conflict(self):
        # {A,B} and {B,C} require crossing groupings at the same polytomy
        tree = _tree("(A,B,C,(D,E));")
        self.assertRaises(
            treerooting.RootingConflictError,
            treerooting.find_root_positions,
            tree,
            outgroups=[["A", "B"], ["B", "C"]],
            refinable_nodes="all",
        )

    def test_nested_outgroups_compatible(self):
        tree = _tree("(A,B,C,(D,E));")
        positions = treerooting.find_root_positions(
            tree, outgroups=[["A", "B"], ["A", "B", "C"]], refinable_nodes="all"
        )
        self.assertEqual(len(positions), 5)


class RootedTreeCandidateTestCase(unittest.TestCase):
    def _annotated_tree(self):
        tree = _tree("(A:1.0,B:2.0,(C:3.0,(D:4.0,E:5.0):6.0):7.0);")
        for i, nd in enumerate(tree.preorder_node_iter()):
            if nd is tree.seed_node:
                continue
            nd.edge.annotations.add_new("eid", "e{}".format(i))
        return tree

    def test_generate_returns_one_candidate_per_position(self):
        tree = self._annotated_tree()
        candidates = treerooting.generate_rooted_trees(tree, outgroups=[["A", "B"]])
        positions = treerooting.find_root_positions(tree, outgroups=[["A", "B"]])
        self.assertEqual(len(candidates), len(positions))
        for c in candidates:
            self.assertTrue(c.tree.is_rooted)
            self.assertIs(c.tree.taxon_namespace, tree.taxon_namespace)

    def test_lengths_and_annotations_migrate_with_edge_identity(self):
        tree = self._annotated_tree()
        candidates = treerooting.generate_rooted_trees(tree, outgroups=[["A", "B"]])
        for c in candidates:
            split = set(c.split_edges or ())
            n_mapped = 0
            for nd in c.tree.postorder_node_iter():
                if nd is c.tree.seed_node:
                    continue
                n_mapped += 1
                new_edge = nd.edge
                self.assertIn(new_edge, c.edge_map)
                orig_edge = c.edge_map[new_edge]
                if new_edge in split:
                    continue
                self.assertEqual(new_edge.length, orig_edge.length)
                self.assertEqual(
                    new_edge.annotations.get_value("eid"),
                    orig_edge.annotations.get_value("eid"),
                )
            self.assertEqual(n_mapped, len(c.edge_map))

    def test_root_edge_split_convention(self):
        tree = self._annotated_tree()
        positions = treerooting.find_root_positions(tree, outgroups=[["A", "B"]])
        edge_positions = [p for p in positions if p.kind == p.EDGE]
        self.assertTrue(edge_positions)
        for pos in edge_positions:
            candidate = treerooting.generate_rooted_trees(tree, positions=[pos])[0]
            orig = pos.edge
            tail_edge, head_edge = candidate.split_edges
            # daughter lengths sum to the original length (equal halves
            # by default)
            self.assertAlmostEqual(
                tail_edge.length + head_edge.length, orig.length
            )
            self.assertAlmostEqual(tail_edge.length, orig.length / 2.0)
            # both daughters carry copies of the original annotations
            self.assertEqual(
                tail_edge.annotations.get_value("eid"),
                orig.annotations.get_value("eid"),
            )
            self.assertEqual(
                head_edge.annotations.get_value("eid"),
                orig.annotations.get_value("eid"),
            )
            # both daughters map back to the original edge
            self.assertIs(candidate.edge_map[tail_edge], orig)
            self.assertIs(candidate.edge_map[head_edge], orig)
            # the new root maps to no original node; all other nodes do
            self.assertIsNone(candidate.node_map[candidate.tree.seed_node])
            # tail-side daughter leads toward the original tail node,
            # head-side daughter toward the original head node
            self.assertIs(
                candidate.node_map[tail_edge.head_node], orig.tail_node
            )
            self.assertIs(
                candidate.node_map[head_edge.head_node], orig.head_node
            )

    def test_split_length_assignment(self):
        tree = self._annotated_tree()
        positions = treerooting.find_root_positions(tree, outgroups=[["A", "B"]])
        pos = [p for p in positions if p.kind == p.EDGE][0]
        orig_length = pos.edge.length
        candidate = treerooting.generate_rooted_trees(
            tree, positions=[pos], length1=1.5
        )[0]
        tail_edge, head_edge = candidate.split_edges
        self.assertAlmostEqual(tail_edge.length, 1.5)
        self.assertAlmostEqual(head_edge.length, orig_length - 1.5)
        candidate = treerooting.generate_rooted_trees(
            tree, positions=[pos], length1=1.0, length2=2.0
        )[0]
        tail_edge, head_edge = candidate.split_edges
        self.assertAlmostEqual(tail_edge.length, 1.0)
        self.assertAlmostEqual(head_edge.length, 2.0)

    def test_annotation_copies_are_independent(self):
        tree = self._annotated_tree()
        positions = treerooting.find_root_positions(tree, outgroups=[["A", "B"]])
        pos = [p for p in positions if p.kind == p.EDGE][0]
        candidate = treerooting.generate_rooted_trees(tree, positions=[pos])[0]
        tail_edge, head_edge = candidate.split_edges
        original_value = pos.edge.annotations.get_value("eid")
        for annotation in tail_edge.annotations:
            if annotation.name == "eid":
                annotation.value = "CHANGED"
        self.assertEqual(pos.edge.annotations.get_value("eid"), original_value)
        self.assertEqual(head_edge.annotations.get_value("eid"), original_value)

    def test_node_position_candidate(self):
        tree = self._annotated_tree()
        positions = treerooting.find_root_positions(tree, outgroups=[["A", "B"]])
        node_positions = [p for p in positions if p.kind == p.NODE]
        self.assertTrue(node_positions)
        for pos in node_positions:
            candidate = treerooting.generate_rooted_trees(tree, positions=[pos])[0]
            self.assertIsNone(candidate.split_edges)
            self.assertIs(candidate.node_map[candidate.tree.seed_node], pos.target)
            self.assertTrue(candidate.tree.is_rooted)

    def test_derive_tree_convenience(self):
        tree = self._annotated_tree()
        positions = treerooting.find_root_positions(tree, outgroups=[["A", "B"]])
        for pos in positions:
            new_tree = pos.derive_tree()
            self.assertIsInstance(new_tree, dendropy.Tree)
            self.assertTrue(new_tree.is_rooted)

    def test_generation_does_not_modify_original(self):
        tree = self._annotated_tree()
        before = tree.as_string("newick")
        treerooting.generate_rooted_trees(tree, outgroups=[["A", "B"]])
        self.assertEqual(tree.as_string("newick"), before)


if __name__ == "__main__":
    unittest.main()
