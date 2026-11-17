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
Explicit association of trees (or tree leaves) with character matrix rows.

Association is always by |Taxon| object identity, never by positional index or
label text. This means that reordering the leaves of a tree does not change
which matrix row a leaf is associated with, and that taxa sharing a label are
never arbitrarily matched to a row: each distinct |Taxon| instance is matched
only to the row keyed by that same instance.

The two distinct ways in which a tree and a matrix can fail to line up are kept
separate:

    * *Tree leaves without matrix rows*: a leaf references a |Taxon| for which
      the matrix has no sequence ("missing matrix rows").
    * *Matrix rows without tree leaves*: the matrix carries a sequence for a
      |Taxon| that does not occur as a leaf of the tree ("extra/unmatched
      matrix rows").

The original matrix rows (the actual |CharacterDataSequence| objects) and the
per-column |StateIdentity| values are referenced, never reordered or copied, so
missing-data, gap, ambiguous, and polymorphic states remain distinguishable.
"""

from dendropy.utility import error
from dendropy.datamodel import charstatemodel


# State category labels. These are kept distinct on purpose: a missing-data
# state, a gap state, and a polymorphic state are not interchangeable.
FUNDAMENTAL_STATE_CATEGORY = "fundamental"
GAP_STATE_CATEGORY = "gap"
MISSING_DATA_STATE_CATEGORY = "missing"
AMBIGUOUS_STATE_CATEGORY = "ambiguous"
POLYMORPHIC_STATE_CATEGORY = "polymorphic"
CONTINUOUS_STATE_CATEGORY = "continuous"


def classify_character_state(state, state_alphabet=None):
    """
    Classifies a single character cell value into one of the state categories
    used by DendroPy, keeping missing data, gaps, ambiguity, and polymorphism
    distinct.

    Parameters
    ----------
    state : any
        A cell value. For discrete character data this is a |StateIdentity|
        instance. Continuous (numeric) values are classified as
        :py:const:`CONTINUOUS_STATE_CATEGORY`.
    state_alphabet : |StateAlphabet| or |None|
        The state alphabet the value belongs to. Used, when available, to
        recognize the alphabet's "no data" (missing) state. If |None|, a state
        is still recognized as missing when it is the ambiguous/no-data state
        of its own alphabet when that can be determined.

    Returns
    -------
    category : str
        One of :py:const:`FUNDAMENTAL_STATE_CATEGORY`,
        :py:const:`GAP_STATE_CATEGORY`,
        :py:const:`MISSING_DATA_STATE_CATEGORY`,
        :py:const:`AMBIGUOUS_STATE_CATEGORY`,
        :py:const:`POLYMORPHIC_STATE_CATEGORY`, or
        :py:const:`CONTINUOUS_STATE_CATEGORY`.
    """
    if not isinstance(state, charstatemodel.StateIdentity):
        # Continuous character matrices store plain numeric values.
        return CONTINUOUS_STATE_CATEGORY
    if state.is_gap_state:
        return GAP_STATE_CATEGORY
    if state_alphabet is not None and state_alphabet.no_data_state is not None \
            and state is state_alphabet.no_data_state:
        return MISSING_DATA_STATE_CATEGORY
    denomination = state.state_denomination
    if denomination == charstatemodel.StateAlphabet.POLYMORPHIC_STATE:
        return POLYMORPHIC_STATE_CATEGORY
    if denomination == charstatemodel.StateAlphabet.AMBIGUOUS_STATE:
        # An ambiguous state spanning every fundamental state of its alphabet
        # is the "no data"/missing condition even if the alphabet did not wire
        # up an explicit no-data state.
        if state_alphabet is not None \
                and set(state.fundamental_states) == set(state_alphabet.fundamental_state_iter()):
            return MISSING_DATA_STATE_CATEGORY
        return AMBIGUOUS_STATE_CATEGORY
    return FUNDAMENTAL_STATE_CATEGORY


class TreeCharacterMatrixAssociation(object):
    """
    Records the explicit, identity-based correspondence between the leaf taxa
    of a tree and the rows of a character matrix.

    Parameters
    ----------
    tree : |Tree| or |None|
        The tree whose leaf taxa are to be associated with matrix rows.
    char_matrix : |CharacterMatrix| or |None|
        The character matrix whose rows are to be associated with tree leaves.

    Raises
    ------
    TaxonNamespaceIdentityError
        If ``tree`` and ``char_matrix`` do not reference the exact same
        |TaxonNamespace| object. Association across separate namespaces is not
        performed here: taxa must first be brought into a common namespace
        (e.g. via :meth:`Tree.migrate_taxon_namespace`).
    """

    def __init__(self, tree=None, char_matrix=None):
        self.tree = tree
        self.char_matrix = char_matrix
        self._tree_leaf_taxa = []
        self._matched_taxon_sequence_map = {}
        self._tree_leaves_without_matrix_rows = []
        self._matrix_rows_without_tree_leaves = []
        if tree is not None and char_matrix is not None:
            self._build_association()

    @classmethod
    def associate(cls, tree, char_matrix):
        """
        Constructs and returns a |TreeCharacterMatrixAssociation| for
        ``tree`` and ``char_matrix``.
        """
        return cls(tree=tree, char_matrix=char_matrix)

    def _build_association(self):
        tree = self.tree
        char_matrix = self.char_matrix
        if tree.taxon_namespace is not char_matrix.taxon_namespace:
            raise error.TaxonNamespaceIdentityError(tree, char_matrix)

        # Collect distinct leaf taxa in tree traversal order. Identity, not
        # label, determines association, so duplicate labels and leaf
        # reorderings never cause a row to be matched to the wrong leaf.
        leaf_taxa = []
        seen = set()
        for node in tree.leaf_node_iter():
            taxon = node.taxon
            if taxon is None or taxon in seen:
                continue
            seen.add(taxon)
            leaf_taxa.append(taxon)
        self._tree_leaf_taxa = leaf_taxa

        # Rows are keyed by Taxon identity; membership testing therefore never
        # falls back to label text or positional index.
        for taxon in leaf_taxa:
            if taxon in char_matrix:
                self._matched_taxon_sequence_map[taxon] = char_matrix[taxon]
            else:
                self._tree_leaves_without_matrix_rows.append(taxon)

        # Matrix iteration order is the original row order; it is preserved.
        for taxon in char_matrix:
            if taxon not in self._matched_taxon_sequence_map:
                self._matrix_rows_without_tree_leaves.append(taxon)

    ###
    # Taxon/row accessors
    ###

    @property
    def tree_leaf_taxa(self):
        "List of distinct leaf |Taxon| objects, in tree traversal order."
        return list(self._tree_leaf_taxa)

    @property
    def matrix_row_taxa(self):
        "List of |Taxon| objects that have matrix rows, in original row order."
        if self.char_matrix is None:
            return []
        return list(self.char_matrix)

    @property
    def matched_taxa(self):
        "List of |Taxon| objects that are both tree leaves and matrix rows."
        return list(self._matched_taxon_sequence_map.keys())

    @property
    def tree_leaves_without_matrix_rows(self):
        """
        Leaf |Taxon| objects of the tree for which the matrix has no row
        (missing matrix rows).
        """
        return list(self._tree_leaves_without_matrix_rows)

    @property
    def matrix_rows_without_tree_leaves(self):
        """
        |Taxon| objects with a matrix row but no corresponding tree leaf
        (extra/unmatched matrix rows).
        """
        return list(self._matrix_rows_without_tree_leaves)

    @property
    def is_complete(self):
        "|True| if every tree leaf has a matrix row and vice versa."
        return (not self._tree_leaves_without_matrix_rows
                and not self._matrix_rows_without_tree_leaves)

    @property
    def taxon_sequence_map(self):
        """
        Mapping of matched |Taxon| objects to their original
        |CharacterDataSequence| objects. The sequences are referenced directly,
        not copied, so original rows and per-column states are preserved.
        """
        return dict(self._matched_taxon_sequence_map)

    def sequence_for(self, taxon):
        """
        Returns the original |CharacterDataSequence| associated with ``taxon``.

        Raises
        ------
        KeyError
            If ``taxon`` is a tree leaf with no corresponding matrix row.
        """
        return self._matched_taxon_sequence_map[taxon]

    def require_complete(self):
        """
        Returns self if every tree leaf is matched to a matrix row (and every
        matrix row to a tree leaf), otherwise raises a descriptive
        |CharacterMatrixTreeAssociationError| that lists both kinds of
        mismatches separately.
        """
        if not self.is_complete:
            raise error.CharacterMatrixTreeAssociationError(
                    tree=self.tree,
                    char_matrix=self.char_matrix,
                    tree_leaves_without_matrix_rows=self._tree_leaves_without_matrix_rows,
                    matrix_rows_without_tree_leaves=self._matrix_rows_without_tree_leaves)
        return self

    ###
    # Per-column state categories
    ###

    def _state_alphabet(self):
        alphabet = getattr(self.char_matrix, "default_state_alphabet", None)
        return alphabet

    def state_category_for_taxon(self, taxon, char_index):
        """
        Returns the state category of the cell at column ``char_index`` of the
        row for ``taxon``. Missing, gap, ambiguous, and polymorphic states are
        reported as distinct categories.
        """
        sequence = self._matched_taxon_sequence_map[taxon]
        return classify_character_state(
                sequence[char_index],
                state_alphabet=self._state_alphabet())

    def column_state_categories(self, char_index):
        """
        Returns a dict mapping each matched |Taxon| to the state category of
        its cell at column ``char_index``.
        """
        alphabet = self._state_alphabet()
        result = {}
        for taxon, sequence in self._matched_taxon_sequence_map.items():
            result[taxon] = classify_character_state(
                    sequence[char_index], state_alphabet=alphabet)
        return result

    def state_category_matrix(self):
        """
        Returns a dict mapping each matched |Taxon| to a list of state
        category labels, one entry per column, preserving column order.
        """
        alphabet = self._state_alphabet()
        result = {}
        for taxon, sequence in self._matched_taxon_sequence_map.items():
            result[taxon] = [
                    classify_character_state(cell, state_alphabet=alphabet)
                    for cell in sequence]
        return result

    def state_category_summary(self):
        """
        Returns a dict counting every state category across all matched rows
        and columns. Categories with zero occurrences are included with a count
        of zero so that missing/gap/polymorphic handling is always explicit.
        """
        summary = {
                FUNDAMENTAL_STATE_CATEGORY: 0,
                GAP_STATE_CATEGORY: 0,
                MISSING_DATA_STATE_CATEGORY: 0,
                AMBIGUOUS_STATE_CATEGORY: 0,
                POLYMORPHIC_STATE_CATEGORY: 0,
                CONTINUOUS_STATE_CATEGORY: 0,
        }
        alphabet = self._state_alphabet()
        for sequence in self._matched_taxon_sequence_map.values():
            for cell in sequence:
                summary[classify_character_state(cell, state_alphabet=alphabet)] += 1
        return summary
