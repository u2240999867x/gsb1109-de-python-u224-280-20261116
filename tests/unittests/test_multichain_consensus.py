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
Tests for multi-chain tree-sample consensus building.
"""

import os
import shutil
import tempfile
import unittest

import dendropy
from dendropy.calculate import multichainconsensus as mcc
from dendropy.calculate import treesum


class MultiChainConsensusTest(unittest.TestCase):

    # Taxon bit assignments: A=1, B=2, C=4, D=8.
    AB = 0b0011
    CD = 0b1100
    AC = 0b0101
    BD = 0b1010

    T1 = "((A:1,B:1):1,(C:1,D:1):1);"   # splits AB, CD
    T2 = "((A:1,C:1):1,(B:1,D:1):1);"   # splits AC, BD

    def setUp(self):
        self.taxon_namespace = dendropy.TaxonNamespace(["A", "B", "C", "D"])
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def make_tree(self, newick):
        tree = dendropy.Tree.get(
                data=newick,
                schema="newick",
                taxon_namespace=self.taxon_namespace,
                rooting="force-rooted")
        tree.encode_bipartitions()
        return tree

    def internal_splits_of(self, tree):
        tree.encode_bipartitions()
        fill = self.taxon_namespace.all_taxa_bitmask()
        return frozenset(
                edge.bipartition.split_bitmask
                for edge in tree.postorder_edge_iter()
                if not dendropy.Bipartition.is_trivial_bitmask(
                        edge.bipartition.split_bitmask, fill))

    def build(self, chains, **kwargs):
        builder = mcc.MultiChainConsensusBuilder(
                taxon_namespace=self.taxon_namespace,
                **kwargs)
        for chain in chains:
            builder.add_chain(chain)
        return builder.build()

    def test_burnin_and_voting_scope_per_chain(self):
        # Two chains of three trees with one-tree burn-in: each contributes
        # two independent votes, so four trees vote even though equally
        # indexed samples occur in both chains.
        chain1 = mcc.ChainTreeSource(
                trees=[self.make_tree(self.T1)] * 3,
                label="chain1",
                burnin=1)
        chain2 = mcc.ChainTreeSource(
                trees=[self.make_tree(self.T2)] * 3,
                label="chain2",
                burnin=1)
        result = self.build([chain1, chain2], min_support=0.0)
        self.assertEqual(result.total_voting_weight, 4.0)
        coverage = {entry["label"]: entry for entry in result.chain_coverage}
        for label in ("chain1", "chain2"):
            self.assertEqual(coverage[label]["trees_available"], 3)
            self.assertEqual(coverage[label]["trees_discarded"], 1)
            self.assertEqual(coverage[label]["trees_voting"], 2)
        # Each split of T1 gets two votes, each split of T2 two votes.
        for split in (self.AB, self.CD, self.AC, self.BD):
            self.assertEqual(result.split_weights[split], 2.0)
            self.assertAlmostEqual(result.split_frequencies[split], 0.5)

    def test_burnin_consuming_a_chain_is_recorded_but_not_voted(self):
        chain = mcc.ChainTreeSource(
                trees=[self.make_tree(self.T1)],
                label="burned",
                burnin=5)
        # A single fully burned-in chain leaves no voters at all.
        with self.assertRaises(mcc.MultiChainConsensusError):
            self.build([chain], min_support=0.0)

    def test_chain_weight_multiplies_votes(self):
        chain1 = mcc.ChainTreeSource(
                trees=[self.make_tree(self.T1)], label="c1", weight=1.0)
        chain2 = mcc.ChainTreeSource(
                trees=[self.make_tree(self.T1)], label="c2", weight=3.0)
        result = self.build([chain1, chain2], min_support=0.0)
        self.assertEqual(result.total_voting_weight, 4.0)
        self.assertEqual(result.split_weights[self.AB], 4.0)
        self.assertEqual(result.split_weights[self.CD], 4.0)

    def test_tree_weights_are_used_when_enabled(self):
        heavy = self.make_tree(self.T2)
        heavy.weight = 5.0
        chain = mcc.ChainTreeSource(
                trees=[self.make_tree(self.T1), heavy],
                label="weighted")
        result = self.build([chain], min_support=0.0, use_tree_weights=True)
        self.assertEqual(result.total_voting_weight, 6.0)
        self.assertEqual(result.split_weights[self.AC], 5.0)
        self.assertEqual(result.split_weights[self.BD], 5.0)
        self.assertEqual(result.split_weights[self.AB], 1.0)
        # Without tree weights every retained tree counts once.
        unweighted = self.build([chain], min_support=0.0, use_tree_weights=False)
        self.assertEqual(unweighted.total_voting_weight, 2.0)
        self.assertEqual(unweighted.split_weights[self.AC], 1.0)

    def test_majority_threshold_excludes_minority_splits(self):
        trees = [self.make_tree(self.T1)] * 3 + [self.make_tree(self.T2)]
        result = self.build(
                [mcc.ChainTreeSource(trees=trees, label="c")],
                min_support=0.5)
        # T1 splits occur in 3/4 trees; T2 splits in 1/4 and fail majority.
        self.assertEqual(set(result.eligible_splits), {self.AB, self.CD})
        self.assertEqual(set(result.selected_splits), {self.AB, self.CD})
        self.assertNotIn(self.AC, result.split_conflicts)
        tree_splits = self.internal_splits_of(result.consensus_tree)
        self.assertEqual(tree_splits, {self.AB, self.CD})

    def test_absolute_support_threshold(self):
        trees = [self.make_tree(self.T1)] * 4
        result = self.build(
                [mcc.ChainTreeSource(trees=trees, label="c")],
                min_support=3.0,
                min_support_is_fraction=False)
        self.assertEqual(set(result.eligible_splits), {self.AB, self.CD})
        below = self.build(
                [mcc.ChainTreeSource(trees=trees, label="c")],
                min_support=5.0,
                min_support_is_fraction=False)
        self.assertEqual(set(below.eligible_splits), set())

    def test_submajority_threshold_picks_max_weight_compatible_set(self):
        # Five-taxon polytomy trees each carry one internal split:
        #   AB weight 11; AC and BD weight 6 each.
        # Greedy-by-support would take AB (11) first, blocking AC and BD for a
        # total of 11. But AC and BD are mutually compatible, so the exact
        # optimum is {AC, BD} with total weight 12.
        ab_only = "((A,B),C,D,E);"
        ac_only = "((A,C),B,D,E);"
        bd_only = "(A,(B,D),C,E);"
        trees = (
                [self.make_tree(ab_only)] * 11
                + [self.make_tree(ac_only)] * 6
                + [self.make_tree(bd_only)] * 6)
        result = self.build(
                [mcc.ChainTreeSource(trees=trees, label="c")],
                min_support=0.0)
        self.assertEqual(result.split_weights[self.AB], 11.0)
        self.assertEqual(result.split_weights[self.AC], 6.0)
        self.assertEqual(result.split_weights[self.BD], 6.0)
        self.assertEqual(set(result.selected_splits), {self.AC, self.BD})
        # Every split passed the zero threshold, but the conflicting AB split
        # was not stuffed onto the tree.
        self.assertEqual(
                set(result.eligible_splits),
                {self.AB, self.AC, self.BD})
        self.assertEqual(result.rejected_eligible_splits(), [self.AB])

    def test_rejected_split_reports_selected_conflict(self):
        ab_only = "((A,B),C,D,E);"
        ac_only = "((A,C),B,D,E);"
        bd_only = "(A,(B,D),C,E);"
        trees = (
                [self.make_tree(ab_only)] * 11
                + [self.make_tree(ac_only)] * 6
                + [self.make_tree(bd_only)] * 6)
        result = self.build(
                [mcc.ChainTreeSource(trees=trees, label="c")],
                min_support=0.0)
        rejected = result.rejected_eligible_splits()
        self.assertEqual(rejected, [self.AB])
        # AB is incompatible with both selected splits AC and BD.
        self.assertEqual(
                result.conflicts_for(self.AB) & result.selected_splits,
                {self.AC, self.BD})

    def test_tied_optima_are_enumerated_and_flagged(self):
        trees = [self.make_tree(self.T1), self.make_tree(self.T2)]
        result = self.build(
                [mcc.ChainTreeSource(trees=trees, label="tie")],
                min_support=0.0)
        # T1 pair and T2 pair each total weight 2: two distinct optima.
        self.assertTrue(result.is_tie)
        self.assertEqual(len(result.alternative_optimal_sets), 1)
        all_optima = [result.selected_splits] + result.alternative_optimal_sets
        self.assertIn(frozenset([self.AB, self.CD]), all_optima)
        self.assertIn(frozenset([self.AC, self.BD]), all_optima)

    def test_unique_optimum_is_not_flagged_as_tie(self):
        trees = [self.make_tree(self.T1)] * 2 + [self.make_tree(self.T2)]
        result = self.build(
                [mcc.ChainTreeSource(trees=trees, label="c")],
                min_support=0.0)
        self.assertFalse(result.is_tie)
        self.assertEqual(result.alternative_optimal_sets, [])
        self.assertEqual(set(result.selected_splits), {self.AB, self.CD})

    def test_tie_enumeration_cap_keeps_optimum_but_flags_truncation(self):
        trees = [self.make_tree(self.T1), self.make_tree(self.T2)]
        builder = mcc.MultiChainConsensusBuilder(
                taxon_namespace=self.taxon_namespace,
                min_support=0.0,
                max_optimal_set_enumerations=1)
        builder.add_chain(mcc.ChainTreeSource(trees=trees, label="tie"))
        result = builder.build()
        # With a cap of one only the chosen optimum is retained, yet the tie is
        # still announced and the retained set must be a valid optimum.
        self.assertTrue(result.is_tie)
        self.assertTrue(result.is_tie_enumeration_truncated)
        self.assertEqual(result.alternative_optimal_sets, [])
        self.assertIn(
                result.selected_splits,
                [frozenset([self.AB, self.CD]), frozenset([self.AC, self.BD])])

    def test_retransmitted_source_votes_once(self):
        path = os.path.join(self.tmpdir, "chain.tre")
        with open(path, "w") as handle:
            handle.write(self.T1 + "\n" + self.T1 + "\n")
        builder = mcc.MultiChainConsensusBuilder(
                taxon_namespace=self.taxon_namespace,
                min_support=0.0)
        builder.add_chain_file(
                path,
                label="first",
                tree_reader_kwargs={"rooting": "force-rooted"})
        builder.add_chain_file(
                path,
                label="retransmission",
                tree_reader_kwargs={"rooting": "force-rooted"})
        result = builder.build()
        self.assertEqual(result.total_voting_weight, 2.0)
        labels = {entry["label"]: entry for entry in result.chain_coverage}
        self.assertEqual(labels["first"]["trees_voting"], 2)
        self.assertTrue(labels["retransmission"]["was_retransmission_dropped"])
        self.assertEqual(labels["retransmission"]["trees_voting"], 0)
        self.assertEqual(result.split_weights[self.AB], 2.0)

    def test_missing_file_keeps_coverage_but_casts_no_votes(self):
        good_chain = mcc.ChainTreeSource(
                trees=[self.make_tree(self.T1)], label="good")
        missing_chain = mcc.ChainTreeSource(
                path=os.path.join(self.tmpdir, "does-not-exist.tre"),
                label="missing")
        result = self.build([good_chain, missing_chain], min_support=0.0)
        labels = {entry["label"]: entry for entry in result.chain_coverage}
        self.assertTrue(labels["missing"]["is_missing"])
        self.assertEqual(labels["missing"]["trees_voting"], 0)
        self.assertEqual(result.total_voting_weight, 1.0)
        self.assertEqual(set(result.selected_splits), {self.AB, self.CD})
        self.assertIn("missing", result.describe_coverage())

    def test_all_sources_missing_raises(self):
        missing = mcc.ChainTreeSource(
                path=os.path.join(self.tmpdir, "nope.tre"),
                label="missing")
        with self.assertRaises(mcc.MultiChainConsensusError):
            self.build([missing], min_support=0.0)

    def test_trivial_splits_are_not_tallied(self):
        result = self.build(
                [mcc.ChainTreeSource(
                        trees=[self.make_tree(self.T1)], label="c")],
                min_support=0.0)
        fill = self.taxon_namespace.all_taxa_bitmask()
        for split in result.split_weights:
            self.assertFalse(
                    dendropy.Bipartition.is_trivial_bitmask(split, fill))

    def test_consensus_tree_carries_support_annotations(self):
        trees = [self.make_tree(self.T1)] * 3 + [self.make_tree(self.T2)]
        result = self.build(
                [mcc.ChainTreeSource(trees=trees, label="c")],
                min_support=0.0)
        by_split = {}
        for node in result.consensus_tree.postorder_internal_node_iter():
            if hasattr(node, "support"):
                by_split[node.edge.bipartition.split_bitmask] = node.support
        self.assertAlmostEqual(by_split[self.AB], 0.75)
        self.assertAlmostEqual(by_split[self.CD], 0.75)

    def test_convenience_wrapper(self):
        chains = [
                mcc.ChainTreeSource(
                        trees=[self.make_tree(self.T1)] * 2, label="c1"),
                mcc.ChainTreeSource(
                        trees=[self.make_tree(self.T2)], label="c2"),
        ]
        result = mcc.multi_chain_consensus_tree(
                chains,
                min_support=0.0,
                taxon_namespace=self.taxon_namespace)
        self.assertIsInstance(result, mcc.MultiChainConsensusResult)
        self.assertEqual(set(result.selected_splits), {self.AB, self.CD})

    def test_invalid_arguments_rejected(self):
        with self.assertRaises(mcc.MultiChainConsensusError):
            mcc.ChainTreeSource(burnin=-1)
        with self.assertRaises(mcc.MultiChainConsensusError):
            mcc.ChainTreeSource(trees=[], weight=0)
        with self.assertRaises(mcc.MultiChainConsensusError):
            mcc.MultiChainConsensusBuilder(min_support=1.5)
        with self.assertRaises(mcc.MultiChainConsensusError):
            mcc.MultiChainConsensusBuilder(
                    min_support=-0.1, min_support_is_fraction=False)

    def test_weighted_treesum_path_no_longer_references_missing_attr(self):
        # Regression: TreeSummarizer.weighted_splits=True used to hit a
        # nonexistent SplitDistribution.weighted_split_frequencies attribute.
        tree_list = dendropy.TreeList.get_from_string(
                self.T1 + "\n" + self.T1 + "\n" + self.T2 + "\n",
                "newick",
                taxon_namespace=self.taxon_namespace,
                rooting="force-rooted")
        split_distribution = dendropy.SplitDistribution(
                taxon_namespace=self.taxon_namespace,
                use_tree_weights=True)
        for tree in tree_list:
            tree.encode_bipartitions()
            split_distribution.count_splits_on_tree(
                    tree, is_bipartitions_updated=True)
        target = self.make_tree(self.T1)
        summarizer = treesum.TreeSummarizer()
        summarizer.weighted_splits = True
        with self.assertWarns(dendropy.utility.deprecate.CriticalDeprecationWarning):
            summarizer.map_split_support_to_tree(target, split_distribution)
        supports = {
                node.edge.bipartition.split_bitmask: node.support
                for node in target.postorder_internal_node_iter()}
        self.assertAlmostEqual(supports[self.AB], 2.0 / 3.0)


if __name__ == "__main__":
    unittest.main()
