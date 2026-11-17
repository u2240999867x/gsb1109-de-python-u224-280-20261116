#! /usr/bin/env python
# -*- coding: utf-8 -*-

##############################################################################
##  DendroPy Phylogenetic Computing Library.
##
##  Copyright 2010-2026 Jeet Sukumaran, Mark T. Holder, and Matthew Andres Moreno.
##  All rights reserved.
##  See "LICENSE.rst" for terms and conditions of usage.
##
##############################################################################

"""
Tests for explicit, identity-based association of tree leaves with character
matrix rows.
"""

import os
import sys
import unittest

import dendropy
from dendropy.calculate import treescore
from dendropy.datamodel import charstatemodel
from dendropy.datamodel import treecharassociation as tca
from dendropy.datamodel.treecharassociation import TreeCharacterMatrixAssociation
from dendropy.utility import error

sys.path.insert(0, os.path.dirname(__file__))
from support import dendropytest


DNA_NEXUS_TEMPLATE = """#NEXUS
BEGIN DATA;
DIMENSIONS NTAX={ntax} NCHAR=4;
FORMAT DATATYPE=DNA MISSING=? GAP=-;
MATRIX
{rows}
;
END;
"""


def get_dna_matrix(taxon_namespace, rows):
    nexus = DNA_NEXUS_TEMPLATE.format(
            ntax=len(rows),
            rows="\n".join("{} {}".format(label, seq) for label, seq in rows))
    return dendropy.DnaCharacterMatrix.get(
            data=nexus, schema="nexus", taxon_namespace=taxon_namespace)


def get_tree(newick, taxon_namespace):
    return dendropy.Tree.get(
            data=newick, schema="newick", taxon_namespace=taxon_namespace)


def build_manual_tree(taxon_namespace, leaf_taxa):
    "Builds a tree whose leaves bind the exact Taxon instances given."
    tree = dendropy.Tree(taxon_namespace=taxon_namespace)
    root = dendropy.Node()
    pivot = dendropy.Node()
    root.add_child(pivot)
    for taxon in leaf_taxa:
        pivot.add_child(dendropy.Node(taxon=taxon))
    tree.seed_node = root
    return tree


class TreeCharacterMatrixAssociationBasicTest(dendropytest.ExtendedTestCase):

    def test_complete_association(self):
        tns = dendropy.TaxonNamespace()
        matrix = get_dna_matrix(tns, [("A", "ACGT"), ("B", "ACGT"), ("C", "TTTT")])
        tree = get_tree("((A,B),C);", tns)
        assoc = TreeCharacterMatrixAssociation(tree, matrix)
        self.assertTrue(assoc.is_complete)
        self.assertEqual(
                [t.label for t in assoc.tree_leaf_taxa], ["A", "B", "C"])
        self.assertEqual(
                [t.label for t in assoc.matrix_row_taxa], ["A", "B", "C"])
        self.assertEqual(
                [t.label for t in assoc.matched_taxa], ["A", "B", "C"])
        self.assertEqual(assoc.tree_leaves_without_matrix_rows, [])
        self.assertEqual(assoc.matrix_rows_without_tree_leaves, [])
        self.assertIs(assoc.require_complete(), assoc)

    def test_tree_leaf_without_matrix_row_is_listed(self):
        # Leaf 'D' occurs in the tree but the matrix has no row for it: a
        # "missing matrix row", distinct from an extra matrix row.
        tns = dendropy.TaxonNamespace()
        matrix = get_dna_matrix(tns, [("A", "ACGT"), ("B", "ACGT"), ("C", "TTTT")])
        tree = get_tree("((A,B),(C,D));", tns)
        assoc = TreeCharacterMatrixAssociation(tree, matrix)
        self.assertFalse(assoc.is_complete)
        self.assertEqual(
                [t.label for t in assoc.tree_leaves_without_matrix_rows], ["D"])
        self.assertEqual(assoc.matrix_rows_without_tree_leaves, [])
        self.assertEqual(
                [t.label for t in assoc.matched_taxa], ["A", "B", "C"])

    def test_matrix_row_without_tree_leaf_is_listed_separately(self):
        # Row 'E' exists in the matrix but no tree leaf references its taxon:
        # an "extra/unmatched matrix row", reported separately from missing
        # matrix rows.
        tns = dendropy.TaxonNamespace()
        matrix = get_dna_matrix(
                tns, [("A", "ACGT"), ("B", "ACGT"), ("C", "TTTT"), ("E", "GGGG")])
        tree = get_tree("((A,B),C);", tns)
        assoc = TreeCharacterMatrixAssociation(tree, matrix)
        self.assertFalse(assoc.is_complete)
        self.assertEqual(assoc.tree_leaves_without_matrix_rows, [])
        self.assertEqual(
                [t.label for t in assoc.matrix_rows_without_tree_leaves], ["E"])

    def test_both_mismatch_kinds_are_reported_separately(self):
        tns = dendropy.TaxonNamespace()
        matrix = get_dna_matrix(
                tns, [("A", "ACGT"), ("B", "ACGT"), ("C", "TTTT"), ("E", "GGGG")])
        tree = get_tree("((A,B),(C,D));", tns)
        assoc = TreeCharacterMatrixAssociation(tree, matrix)
        self.assertEqual(
                [t.label for t in assoc.tree_leaves_without_matrix_rows], ["D"])
        self.assertEqual(
                [t.label for t in assoc.matrix_rows_without_tree_leaves], ["E"])
        with self.assertRaises(error.CharacterMatrixTreeAssociationError) as ctx:
            assoc.require_complete()
        self.assertEqual(
                [t.label for t in ctx.exception.tree_leaves_without_matrix_rows],
                ["D"])
        self.assertEqual(
                [t.label for t in ctx.exception.matrix_rows_without_tree_leaves],
                ["E"])
        self.assertIn("D", str(ctx.exception))
        self.assertIn("E", str(ctx.exception))

    def test_different_taxon_namespaces_raise_identity_error(self):
        tns1 = dendropy.TaxonNamespace()
        tns2 = dendropy.TaxonNamespace()
        matrix = get_dna_matrix(tns1, [("A", "ACGT"), ("B", "ACGT")])
        tree = get_tree("(A,B);", tns2)
        with self.assertRaises(error.TaxonNamespaceIdentityError):
            TreeCharacterMatrixAssociation(tree, matrix)


class TreeCharacterMatrixAssociationPreservationTest(dendropytest.ExtendedTestCase):

    def test_original_row_references_are_preserved(self):
        tns = dendropy.TaxonNamespace()
        matrix = get_dna_matrix(tns, [("A", "ACGT"), ("B", "ACGT")])
        tree = get_tree("(A,B);", tns)
        assoc = TreeCharacterMatrixAssociation(tree, matrix)
        for taxon in assoc.matched_taxa:
            self.assertIs(assoc.sequence_for(taxon), matrix[taxon])
            self.assertIs(assoc.taxon_sequence_map[taxon], matrix[taxon])

    def test_column_order_and_states_are_preserved(self):
        tns = dendropy.TaxonNamespace()
        matrix = get_dna_matrix(tns, [("A", "ACGT"), ("B", "TGCA")])
        tree = get_tree("(A,B);", tns)
        assoc = TreeCharacterMatrixAssociation(tree, matrix)
        taxon_a = tns.get_taxon("A")
        symbols = [
                assoc.sequence_for(taxon_a)[i].symbol for i in range(4)]
        self.assertEqual(symbols, ["A", "C", "G", "T"])
        category_matrix = assoc.state_category_matrix()
        self.assertEqual(
                [c for c in category_matrix[taxon_a]],
                [tca.FUNDAMENTAL_STATE_CATEGORY] * 4)


class StateCategoryDistinctionTest(dendropytest.ExtendedTestCase):

    def setUp(self):
        # Use an isolated, locally-created alphabet rather than the shared
        # DNA_STATE_ALPHABET singleton so that defining ambiguous/polymorphic
        # states here does not mutate global state for other tests.
        self.tns = dendropy.TaxonNamespace()
        alphabet = charstatemodel.StateAlphabet(
                fundamental_states=["a", "c", "g", "t"],
                gap_symbol="-",
                no_data_symbol="?")
        ambiguous = alphabet.new_ambiguous_state(
                symbol="K", member_state_symbols=["g", "t"])
        polymorphic = alphabet.new_polymorphic_state(
                symbol="P", member_state_symbols=["a", "c"])
        state_map = alphabet.full_symbol_state_map
        self.matrix = dendropy.StandardCharacterMatrix(
                taxon_namespace=self.tns,
                default_state_alphabet=alphabet)
        # Row A: fundamental, fundamental, gap, fundamental.
        self.matrix[self.tns.require_taxon("A")] = dendropy.CharacterDataSequence([
                state_map["a"], state_map["c"], alphabet.gap_state, state_map["t"]])
        # Row B: fundamental, ambiguous {g,t}, polymorphic (a,c), missing '?'.
        self.matrix[self.tns.require_taxon("B")] = dendropy.CharacterDataSequence([
                state_map["a"], ambiguous, polymorphic, alphabet.no_data_state])
        # Row C: all fundamental.
        self.matrix[self.tns.require_taxon("C")] = dendropy.CharacterDataSequence([
                state_map["a"], state_map["c"], state_map["g"], state_map["t"]])
        self.tree = get_tree("((A,B),C);", self.tns)
        self.assoc = TreeCharacterMatrixAssociation(self.tree, self.matrix)

    def test_missing_gap_ambiguous_polymorphic_are_distinct(self):
        taxon_a = self.tns.get_taxon("A")
        taxon_b = self.tns.get_taxon("B")
        self.assertEqual(
                self.assoc.state_category_for_taxon(taxon_a, 2),
                tca.GAP_STATE_CATEGORY)
        self.assertEqual(
                self.assoc.state_category_for_taxon(taxon_b, 1),
                tca.AMBIGUOUS_STATE_CATEGORY)
        self.assertEqual(
                self.assoc.state_category_for_taxon(taxon_b, 2),
                tca.POLYMORPHIC_STATE_CATEGORY)
        self.assertEqual(
                self.assoc.state_category_for_taxon(taxon_b, 3),
                tca.MISSING_DATA_STATE_CATEGORY)
        self.assertEqual(
                self.assoc.state_category_for_taxon(taxon_b, 0),
                tca.FUNDAMENTAL_STATE_CATEGORY)

    def test_column_state_categories(self):
        col2 = self.assoc.column_state_categories(2)
        self.assertEqual(
                col2[self.tns.get_taxon("A")], tca.GAP_STATE_CATEGORY)
        self.assertEqual(
                col2[self.tns.get_taxon("B")], tca.POLYMORPHIC_STATE_CATEGORY)
        self.assertEqual(
                col2[self.tns.get_taxon("C")], tca.FUNDAMENTAL_STATE_CATEGORY)

    def test_summary_counts_each_category_and_keeps_zeros(self):
        summary = self.assoc.state_category_summary()
        # 12 cells: 8 fundamental, 1 gap, 1 missing, 1 ambiguous, 1 polymorphic
        self.assertEqual(summary[tca.FUNDAMENTAL_STATE_CATEGORY], 8)
        self.assertEqual(summary[tca.GAP_STATE_CATEGORY], 1)
        self.assertEqual(summary[tca.MISSING_DATA_STATE_CATEGORY], 1)
        self.assertEqual(summary[tca.AMBIGUOUS_STATE_CATEGORY], 1)
        self.assertEqual(summary[tca.POLYMORPHIC_STATE_CATEGORY], 1)
        self.assertEqual(summary[tca.CONTINUOUS_STATE_CATEGORY], 0)
        self.assertEqual(sum(summary.values()), 12)

    def test_continuous_values_classified_as_continuous(self):
        tns = dendropy.TaxonNamespace()
        matrix = dendropy.ContinuousCharacterMatrix(taxon_namespace=tns)
        taxon = tns.require_taxon("A")
        matrix[taxon] = dendropy.CharacterDataSequence([1.5, 2.5])
        tree = build_manual_tree(tns, [taxon])
        assoc = TreeCharacterMatrixAssociation(tree, matrix)
        self.assertEqual(
                assoc.state_category_for_taxon(taxon, 0),
                tca.CONTINUOUS_STATE_CATEGORY)
        self.assertEqual(
                assoc.state_category_summary()[tca.CONTINUOUS_STATE_CATEGORY], 2)


class ReorderingAndDuplicateLabelTest(dendropytest.ExtendedTestCase):

    def test_leaf_reordering_does_not_change_association(self):
        tns = dendropy.TaxonNamespace()
        matrix = get_dna_matrix(
                tns, [("A", "AAAA"), ("B", "CCCC"), ("C", "GGGG"), ("D", "TTTT")])
        taxa = {label: tns.get_taxon(label) for label in ["A", "B", "C", "D"]}
        tree1 = get_tree("((A,B),(C,D));", tns)
        tree2 = get_tree("(D,((C,A),B));", tns)
        assoc1 = TreeCharacterMatrixAssociation(tree1, matrix)
        assoc2 = TreeCharacterMatrixAssociation(tree2, matrix)
        for label, taxon in taxa.items():
            self.assertIs(assoc1.sequence_for(taxon), matrix[taxon])
            self.assertIs(assoc2.sequence_for(taxon), matrix[taxon])
        # Both are complete regardless of leaf ordering.
        self.assertTrue(assoc1.is_complete)
        self.assertTrue(assoc2.is_complete)

    def test_duplicate_labels_are_not_arbitrarily_matched_to_rows(self):
        tns = dendropy.TaxonNamespace()
        x_first = tns.new_taxon("X")
        x_second = tns.new_taxon("X")
        taxon_a = tns.new_taxon("A")
        alphabet = dendropy.new_standard_state_alphabet(["0", "1"])
        state0, state1 = alphabet[0], alphabet[1]
        matrix = dendropy.StandardCharacterMatrix(
                taxon_namespace=tns, default_state_alphabet=alphabet)
        matrix[x_first] = dendropy.CharacterDataSequence([state0])
        matrix[x_second] = dendropy.CharacterDataSequence([state1])
        matrix[taxon_a] = dendropy.CharacterDataSequence([state0])

        tree = build_manual_tree(tns, [taxon_a, x_first, x_second])
        assoc = TreeCharacterMatrixAssociation(tree, matrix)
        self.assertTrue(assoc.is_complete)
        # Each distinct Taxon identity keeps its own row; a label lookup that
        # returned the first 'X' for both leaves would fail these assertions.
        self.assertIs(assoc.sequence_for(x_first), matrix[x_first])
        self.assertIs(assoc.sequence_for(x_second), matrix[x_second])
        self.assertIs(list(assoc.sequence_for(x_first))[0], state0)
        self.assertIs(list(assoc.sequence_for(x_second))[0], state1)

    def test_association_follows_identity_when_leaf_binding_swapped(self):
        tns = dendropy.TaxonNamespace()
        x_first = tns.new_taxon("X")
        x_second = tns.new_taxon("X")
        alphabet = dendropy.new_standard_state_alphabet(["0", "1"])
        state0, state1 = alphabet[0], alphabet[1]
        matrix = dendropy.StandardCharacterMatrix(
                taxon_namespace=tns, default_state_alphabet=alphabet)
        matrix[x_first] = dendropy.CharacterDataSequence([state0])
        matrix[x_second] = dendropy.CharacterDataSequence([state1])
        tree = build_manual_tree(tns, [x_first, x_second])
        leaf_nodes = list(tree.leaf_node_iter())
        # Swap the Taxon identity bound to each (same-label) leaf.
        leaf_nodes[0].taxon, leaf_nodes[1].taxon = x_second, x_first
        assoc = TreeCharacterMatrixAssociation(tree, matrix)
        self.assertIs(assoc.sequence_for(leaf_nodes[0].taxon), matrix[x_second])
        self.assertIs(assoc.sequence_for(leaf_nodes[1].taxon), matrix[x_first])


class ParsimonyAssociationErrorTest(dendropytest.ExtendedTestCase):

    def test_parsimony_score_raises_association_error_for_missing_row(self):
        tns = dendropy.TaxonNamespace()
        matrix = get_dna_matrix(tns, [("A", "ACGT"), ("B", "ACGT"), ("C", "TTTT")])
        tree = get_tree("((A,B),(C,D));", tns)
        with self.assertRaises(error.CharacterMatrixTreeAssociationError) as ctx:
            treescore.parsimony_score(tree, matrix)
        self.assertEqual(
                [t.label for t in ctx.exception.tree_leaves_without_matrix_rows],
                ["D"])

    def test_parsimony_score_still_works_when_fully_associated(self):
        tns = dendropy.TaxonNamespace()
        matrix = get_dna_matrix(
                tns, [("A", "ACGT"), ("B", "ACGT"), ("C", "TTTT"), ("D", "TTTT")])
        tree = get_tree("((A,B),(C,D));", tns)
        # Should not raise; exact score is validated elsewhere.
        score = treescore.parsimony_score(tree, matrix)
        self.assertIsInstance(score, int)


if __name__ == "__main__":
    unittest.main()
