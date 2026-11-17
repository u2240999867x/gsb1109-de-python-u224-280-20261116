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
Tests for joint time-calibration feasibility checking and provable node date
ranges (``dendropy.calculate.calibration``).
"""

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from support import dendropytest

import dendropy
from dendropy.calculate import calibration


class CalibrationTestSupport(dendropytest.ExtendedTestCase):

    def assert_interval(self, result, node, expected_min, expected_max, tol=1e-9):
        observed_min, observed_max = result.node_date_range(node)
        self.assertAlmostEqual(observed_min, expected_min, delta=tol)
        self.assertAlmostEqual(observed_max, expected_max, delta=tol)

    def assert_edge_interval(self, result, node, expected_min, expected_max, tol=1e-9):
        observed_min, observed_max = result.edge_duration_range(node.edge)
        self.assertAlmostEqual(observed_min, expected_min, delta=tol)
        self.assertAlmostEqual(observed_max, expected_max, delta=tol)

    def label_to_node(self, tree):
        mapping = {}
        for node in tree.postorder_node_iter():
            if node.taxon is not None and node.taxon.label:
                mapping[node.taxon.label] = node
            elif node.label:
                mapping[node.label] = node
        return mapping


class ExactCalibrationTests(CalibrationTestSupport):

    def setUp(self):
        # ((A:3, B:2):1, C:4); root R; internal I
        self.tree = dendropy.Tree.get(
            data="((A:3,B:2)I:1,C:4)R;",
            schema="newick",
        )
        self.nodes = self.label_to_node(self.tree)

    def test_exact_leaf_date_pins_all_nodes(self):
        cal = calibration.TreeCalibration(self.tree)
        cal.constrain_node_date(self.nodes["A"], exact=10.0)
        result = cal.solve()
        # A = 10, edge I->A = 3 => I = 7, edge R->I = 1 => R = 6,
        # B = I + 2 = 9, C = R + 4 = 10.
        self.assert_interval(result, self.nodes["A"], 10.0, 10.0)
        self.assert_interval(result, self.nodes["I"], 7.0, 7.0)
        self.assert_interval(result, self.nodes["R"], 6.0, 6.0)
        self.assert_interval(result, self.nodes["B"], 9.0, 9.0)
        self.assert_interval(result, self.nodes["C"], 10.0, 10.0)
        for node in self.nodes.values():
            if node.parent_node is not None:
                self.assert_edge_interval(
                    result, node, node.edge.length, node.edge.length
                )

    def test_present_time_shifts_dates_but_not_durations(self):
        cal = calibration.TreeCalibration(self.tree, present_time=100.0)
        cal.constrain_node_date(self.nodes["A"], exact=110.0)
        result = cal.solve()
        self.assert_interval(result, self.nodes["R"], 106.0, 106.0)
        self.assert_edge_interval(result, self.nodes["A"], 3.0, 3.0)

    def test_label_string_node_reference(self):
        cal = calibration.TreeCalibration(self.tree)
        cal.constrain_node_date("A", exact=10.0)
        result = cal.solve()
        self.assert_interval(result, self.nodes["A"], 10.0, 10.0)

    def test_convenience_wrapper_exact_and_ranged(self):
        result = calibration.check_time_calibration(
            self.tree,
            node_date_constraints={"A": 10.0, "B": (8.0, 12.0)},
        )
        self.assertTrue(result.is_feasible)
        self.assert_interval(result, self.nodes["A"], 10.0, 10.0)
        self.assert_interval(result, self.nodes["B"], 9.0, 9.0)

    def test_node_date_ranges_helper(self):
        ranges = calibration.node_date_ranges(
            self.tree, node_date_constraints={"C": 4.0}
        )
        lower, upper = ranges[self.nodes["R"]]
        self.assertAlmostEqual(lower, 0.0)
        self.assertAlmostEqual(upper, 0.0)


class RangedEdgeDurationTests(CalibrationTestSupport):

    def setUp(self):
        # Root with two leaves, all edge lengths unknown initially.
        self.tree = dendropy.Tree.get(
            data="(A,B)R;",
            schema="newick",
        )
        for node in self.tree.postorder_node_iter():
            node.edge.length = None
        self.nodes = self.label_to_node(self.tree)

    def test_unknown_edges_only_impose_ordering(self):
        cal = calibration.TreeCalibration(self.tree)
        result = cal.solve()
        root = self.nodes["R"]
        leaf_a = self.nodes["A"]
        # Gauge fixes root at the origin; leaves can only be later.
        self.assert_interval(result, root, 0.0, 0.0)
        lower, upper = result.node_date_range(leaf_a)
        self.assertAlmostEqual(lower, 0.0)
        self.assertEqual(upper, math.inf)
        self.assert_edge_interval(result, leaf_a, 0.0, math.inf)

    def test_range_edge_duration_propagates_to_node_range(self):
        cal = calibration.TreeCalibration(self.tree)
        cal.constrain_node_date(self.nodes["A"], exact=10.0)
        cal.constrain_edge_duration(
            parent_node=self.nodes["R"],
            child_node=self.nodes["A"],
            minimum=2.0,
            maximum=6.0,
        )
        result = cal.solve()
        # R = A - d, d in [2, 6] => R in [4, 8].
        self.assert_interval(result, self.nodes["R"], 4.0, 8.0)
        self.assert_edge_interval(result, self.nodes["A"], 2.0, 6.0)

    def test_one_sided_bounds(self):
        cal = calibration.TreeCalibration(self.tree)
        cal.constrain_node_date(self.nodes["A"], exact=10.0)
        cal.constrain_edge_duration(
            parent_node=self.nodes["R"],
            child_node=self.nodes["A"],
            minimum=3.0,
        )
        result = cal.solve()
        lower, upper = result.node_date_range(self.nodes["R"])
        self.assertAlmostEqual(lower, -math.inf)
        self.assertAlmostEqual(upper, 7.0)
        dmin, dmax = result.edge_duration_range(self.nodes["A"].edge)
        self.assertAlmostEqual(dmin, 3.0)
        self.assertEqual(dmax, math.inf)

    def test_zero_length_edge_is_distinct_from_unknown(self):
        cal = calibration.TreeCalibration(self.tree)
        cal.constrain_edge_duration(
            parent_node=self.nodes["R"],
            child_node=self.nodes["A"],
            exact=0.0,
        )
        cal.constrain_node_date(self.nodes["A"], exact=5.0)
        result = cal.solve()
        # Zero-length edge forces R == A exactly.
        self.assert_interval(result, self.nodes["R"], 5.0, 5.0)
        self.assert_edge_interval(result, self.nodes["A"], 0.0, 0.0)


class JointContradictionTests(CalibrationTestSupport):

    def _two_leaf_tree(self, edge_length):
        tree = dendropy.Tree.get(
            data="(A,B)R;",
            schema="newick",
        )
        for node in tree.postorder_node_iter():
            if node.parent_node is not None:
                node.edge.length = edge_length
        return tree

    def test_each_root_leaf_path_legal_but_jointly_contradictory(self):
        # Both terminal edges are zero length: each path alone only forces
        # equality, but leaves sampled at different times cannot share a root.
        tree = self._two_leaf_tree(edge_length=0.0)
        nodes = self.label_to_node(tree)
        cal = calibration.TreeCalibration(tree)
        cal.constrain_node_date(nodes["A"], exact=10.0)
        cal.constrain_node_date(nodes["B"], exact=5.0)
        with self.assertRaises(calibration.CalibrationError) as context:
            cal.solve()
        error = context.exception
        chain = error.contradictory_constraint_chain
        self.assertTrue(len(chain) >= 3)
        descriptions = " | ".join(item.describe() for item in chain)
        # The reported chain must name the original conflicting ingredients.
        self.assertIn("A", descriptions)
        self.assertIn("B", descriptions)
        self.assertIn("0", descriptions)
        message = str(error)
        self.assertIn("Contradictory constraint chain", message)

    def test_is_feasible_flag(self):
        tree = self._two_leaf_tree(edge_length=0.0)
        nodes = self.label_to_node(tree)
        cal = calibration.TreeCalibration(tree)
        cal.constrain_node_date(nodes["A"], exact=10.0)
        cal.constrain_node_date(nodes["B"], exact=10.0)
        self.assertTrue(cal.is_feasible())

        cal2_tree = dendropy.Tree.get(data="(A,B)R;", schema="newick")
        for node in cal2_tree.postorder_node_iter():
            if node.parent_node is not None:
                node.edge.length = 0.0
        cal2 = calibration.TreeCalibration(cal2_tree)
        nodes2 = self.label_to_node(cal2_tree)
        cal2.constrain_node_date(nodes2["A"], exact=10.0)
        cal2.constrain_node_date(nodes2["B"], exact=5.0)
        self.assertFalse(cal2.is_feasible())

    def test_single_edge_duration_contradicts_dates(self):
        tree = dendropy.Tree.get(data="(A,B)R;", schema="newick")
        nodes = self.label_to_node(tree)
        nodes["A"].edge.length = None
        nodes["B"].edge.length = None
        cal = calibration.TreeCalibration(tree)
        cal.constrain_node_date(nodes["R"], exact=0.0)
        cal.constrain_node_date(nodes["A"], exact=2.0)
        cal.constrain_edge_duration(
            parent_node=nodes["R"],
            child_node=nodes["A"],
            minimum=5.0,
            maximum=8.0,
        )
        with self.assertRaises(calibration.CalibrationError) as context:
            cal.solve()
        chain_text = " | ".join(
            c.describe() for c in context.exception.contradictory_constraint_chain
        )
        self.assertIn("[5.0, 8.0]", chain_text)

    def test_parent_cannot_be_later_than_child_with_unknown_edges(self):
        # R pinned later than its leaf despite unknown (but non-negative)
        # edge durations: ordering alone forbids it.
        tree = dendropy.Tree.get(data="(A,B)R;", schema="newick")
        for node in tree.postorder_node_iter():
            node.edge.length = None
        nodes = self.label_to_node(tree)
        cal = calibration.TreeCalibration(tree)
        cal.constrain_node_date(nodes["R"], exact=10.0)
        cal.constrain_node_date(nodes["A"], maximum=4.0)
        with self.assertRaises(calibration.CalibrationError):
            cal.solve()


class MultifurcationAndReferenceTests(CalibrationTestSupport):

    def test_multifurcating_tree_feasible_ranges(self):
        tree = dendropy.Tree.get(data="(A,B,C)R;", schema="newick")
        for node in tree.postorder_node_iter():
            if node.parent_node is not None:
                node.edge.length = 1.0
        nodes = self.label_to_node(tree)
        result = calibration.check_time_calibration(
            tree, node_date_constraints={"A": 3.0}
        )
        self.assert_interval(result, nodes["R"], 2.0, 2.0)
        for label in ("A", "B", "C"):
            self.assert_interval(result, nodes[label], 3.0, 3.0)

    def test_relative_node_date_constraint(self):
        tree = dendropy.Tree.get(data="(A,B)R;", schema="newick")
        nodes = self.label_to_node(tree)
        nodes["A"].edge.length = None
        nodes["B"].edge.length = None
        cal = calibration.TreeCalibration(tree)
        cal.constrain_node_date(nodes["A"], exact=10.0)
        # B must be between 1 and 4 time units later than A.
        cal.constrain_node_date(
            nodes["B"],
            minimum=1.0,
            maximum=4.0,
            reference_node=nodes["A"],
        )
        result = cal.solve()
        lower, upper = result.node_date_range(nodes["B"])
        self.assertAlmostEqual(lower, 11.0)
        self.assertAlmostEqual(upper, 14.0)

    def test_duplicate_edge_constraints_intersect(self):
        tree = dendropy.Tree.get(data="(A,B)R;", schema="newick")
        nodes = self.label_to_node(tree)
        nodes["A"].edge.length = None
        nodes["B"].edge.length = None
        cal = calibration.TreeCalibration(tree)
        cal.constrain_node_date(nodes["A"], exact=10.0)
        cal.constrain_edge_duration(
            parent_node=nodes["R"], child_node=nodes["A"], minimum=2.0, maximum=8.0
        )
        cal.constrain_edge_duration(
            parent_node=nodes["R"], child_node=nodes["A"], minimum=5.0, maximum=6.0
        )
        result = cal.solve()
        self.assert_interval(result, nodes["R"], 4.0, 5.0)

    def test_report_contains_all_nodes(self):
        tree = dendropy.Tree.get(data="(A:1,B:2)R;", schema="newick")
        result = calibration.check_time_calibration(
            tree, node_date_constraints={"A": 5.0}
        )
        report = result.format_report()
        for label in ("R", "A", "B"):
            self.assertIn(label, report)
        self.assertIn("Node date ranges", report)
        self.assertIn("Edge duration ranges", report)


class InputValidationTests(CalibrationTestSupport):

    def test_unknown_label_raises(self):
        tree = dendropy.Tree.get(data="(A,B)R;", schema="newick")
        cal = calibration.TreeCalibration(tree)
        with self.assertRaises(KeyError):
            cal.constrain_node_date("Z", exact=1.0)

    def test_inverted_bounds_rejected(self):
        tree = dendropy.Tree.get(data="(A,B)R;", schema="newick")
        cal = calibration.TreeCalibration(tree)
        with self.assertRaises(ValueError):
            cal.constrain_node_date(
                tree.seed_node, minimum=10.0, maximum=2.0
            )

    def test_negative_duration_rejected(self):
        tree = dendropy.Tree.get(data="(A,B)R;", schema="newick")
        nodes = self.label_to_node(tree)
        cal = calibration.TreeCalibration(tree)
        with self.assertRaises(ValueError):
            cal.constrain_edge_duration(
                parent_node=nodes["R"],
                child_node=nodes["A"],
                minimum=-1.0,
            )

    def test_exact_conflicts_with_bounds(self):
        tree = dendropy.Tree.get(data="(A,B)R;", schema="newick")
        cal = calibration.TreeCalibration(tree)
        with self.assertRaises(ValueError):
            cal.constrain_node_date(tree.seed_node, exact=1.0, minimum=0.0)

    def test_empty_tree_rejected(self):
        tree = dendropy.Tree()
        tree.seed_node = None
        with self.assertRaises(ValueError):
            calibration.TreeCalibration(tree)


if __name__ == "__main__":
    unittest.main()
