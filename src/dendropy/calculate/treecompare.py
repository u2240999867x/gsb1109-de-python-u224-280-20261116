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
Statistics, metrics, measurements, and values calculated *between* trees or
across collections of trees.
"""

import math
import collections
import itertools
from dendropy.utility import error
from dendropy.utility import bitprocessing

###############################################################################
## Public Functions

def symmetric_difference(tree1, tree2, is_bipartitions_updated=False):
    """
    Returns *unweighted* Robinson-Foulds distance between two trees.

    Trees need to share the same |TaxonNamespace| reference. The
    bipartition bitmasks of the trees must be correct for the current tree
    structures (by calling :meth:`Tree.encode_bipartitions()` method) or the
    ``is_bipartitions_updated`` argument must be |False| to force recalculation
    of bipartitions.

    Parameters
    ----------
    tree1 : |Tree| object
        The first tree of the two trees being compared. This must share the
        same |TaxonNamespace| reference as ``tree2`` and must have
        bipartitions encoded.
    tree2 : |Tree| object
        The second tree of the two trees being compared. This must share the
        same |TaxonNamespace| reference as ``tree1`` and must have
        bipartitions encoded.
    is_bipartitions_updated : bool
        If |False|, then the bipartitions on *both* trees will be updated
        before comparison. If |True| then the bipartitions will only be
        calculated for a |Tree| object if they have not been calculated
        before, either explicitly or implicitly.

    Returns
    -------
    d : int
        The symmetric difference (a.k.a. the unweighted Robinson-Foulds
        distance) between ``tree1`` and ``tree2``.

    Notes
    -----
    Encoding bipartitions may collapse an unrooted basal bifurcation or
    suppress unifurcations, modifying the tree. Alternatively, call
    :meth:`Tree.encode_bipartitions()` with desired options then pass
    ``is_bipartitions_updated=True``.

    Examples
    --------

    ::

        from dendropy import TaxonNamespace, Tree
        from dendropy.calculate import treecompare
        tns = TaxonNamespace()
        tree1 = Tree.get_from_path(
                "t1.nex",
                "nexus",
                taxon_namespace=tns)
        tree2 = Tree.get_from_path(
                "t2.nex",
                "nexus",
                taxon_namespace=tns)
        tree1.encode_bipartitions()
        tree2.encode_bipartitions()
        print(treecompare.symmetric_difference(tree1, tree2))

    """
    t = false_positives_and_negatives(
            tree1,
            tree2,
            is_bipartitions_updated=is_bipartitions_updated)
    return t[0] + t[1]

def unweighted_robinson_foulds_distance(tree1, tree2, is_bipartitions_updated=False):
    """
    Alias for ``symmetric_difference()``.

    Notes
    -----
    Encoding bipartitions may collapse an unrooted basal bifurcation or
    suppress unifurcations, modifying the tree. Alternatively, call
    :meth:`Tree.encode_bipartitions()` with desired options then pass
    ``is_bipartitions_updated=True``.
    """
    return symmetric_difference(tree1, tree2, is_bipartitions_updated)

def weighted_robinson_foulds_distance(
        tree1,
        tree2,
        edge_weight_attr="length",
        is_bipartitions_updated=False):
    """
    Returns *weighted* Robinson-Foulds distance between two trees based on
    ``edge_weight_attr``.

    Trees need to share the same |TaxonNamespace| reference. The
    bipartition bitmasks of the trees must be correct for the current tree
    structures (by calling :meth:`Tree.encode_bipartitions()` method) or the
    ``is_bipartitions_updated`` argument must be |False| to force recalculation of
    bipartitions.

    Parameters
    ----------
    tree1 : |Tree| object
        The first tree of the two trees being compared. This must share the
        same |TaxonNamespace| reference as ``tree2`` and must have
        bipartitions encoded.
    tree2 : |Tree| object
        The second tree of the two trees being compared. This must share the
        same |TaxonNamespace| reference as ``tree1`` and must have
        bipartitions encoded.
    edge_weight_attr : string
        Name of attribute on edges of trees to be used as the weight.
    is_bipartitions_updated : bool
        If |False| (default), then the bipartitions on *both* trees will be updated
        before comparison. If |True| then the bipartitions will only be
        calculated for a |Tree| object if they have not been calculated
        before, either explicitly or implicitly.

    Returns
    -------
    d : float
        The edge-weighted Robinson-Foulds distance between ``tree1`` and ``tree2``.

    Notes
    -----
    Encoding bipartitions may collapse an unrooted basal bifurcation or
    suppress unifurcations, modifying the tree. Alternatively, call
    :meth:`Tree.encode_bipartitions()` with desired options then pass
    ``is_bipartitions_updated=True``.

    Examples
    --------

    ::

        import dendropy
        from dendropy.calculate import treecompare
        tns = dendropy.TaxonNamespace()
        tree1 = tree.get_from_path(
                "t1.nex",
                "nexus",
                taxon_namespace=tns)
        tree2 = tree.get_from_path(
                "t2.nex",
                "nexus",
                taxon_namespace=tns)
        tree1.encode_bipartitions()
        tree2.encode_bipartitions()
        print(treecompare.weighted_robinson_foulds_distance(tree1, tree2))

    """
    df = lambda length_diffs: sum([abs(i[0] - i[1]) for i in length_diffs])
    return _bipartition_difference(tree1,
                           tree2,
                           dist_fn=df,
                           edge_weight_attr=edge_weight_attr,
                           value_type=float,
                           is_bipartitions_updated=is_bipartitions_updated)

def false_positives_and_negatives(
        reference_tree,
        comparison_tree,
        is_bipartitions_updated=False):
    """
    Counts and returns number of false positive bipar (bipartitions found in
    ``comparison_tree`` but not in ``reference_tree``) and false negative
    bipartitions (bipartitions found in ``reference_tree`` but not in
    ``comparison_tree``).

    Trees need to share the same |TaxonNamespace| reference. The
    bipartition bitmasks of the trees must be correct for the current tree
    structures (by calling :meth:`Tree.encode_bipartitions()` method) or the
    ``is_bipartitions_updated`` argument must be |False| to force recalculation of
    bipartitions.

    Parameters
    ----------
    reference_tree : |Tree| object
        The first tree of the two trees being compared. This must share the
        same |TaxonNamespace| reference as ``tree2`` and must have
        bipartitions encoded.
    comparison_tree : |Tree| object
        The second tree of the two trees being compared. This must share the
        same |TaxonNamespace| reference as ``tree1`` and must have
        bipartitions encoded.
    is_bipartitions_updated : bool
        If |False| (default), then the bipartitions on *both* trees will be updated
        before comparison. If |True| then the bipartitions
        will only be calculated for a |Tree| object if they have not been
        calculated before, either explicitly or implicitly.

    Returns
    -------
    t : tuple(int)
        A pair of integers, with first integer being the number of false
        positives and the second being the number of false negatives.

    Notes
    -----
    Encoding bipartitions may collapse an unrooted basal bifurcation or
    suppress unifurcations, modifying the tree. Alternatively, call
    :meth:`Tree.encode_bipartitions()` with desired options then pass
    ``is_bipartitions_updated=True``.

    Examples
    --------

    ::

        import dendropy
        from dendropy.calculate import treecompare
        tns = dendropy.TaxonNamespace()
        tree1 = tree.get_from_path(
                "t1.nex",
                "nexus",
                taxon_namespace=tns)
        tree2 = tree.get_from_path(
                "t2.nex",
                "nexus",
                taxon_namespace=tns)
        tree1.encode_bipartitions()
        tree2.encode_bipartitions()
        print(treecompare.false_positives_and_negatives(tree1, tree2))

    """
    if reference_tree.taxon_namespace is not comparison_tree.taxon_namespace:
        raise error.TaxonNamespaceIdentityError(reference_tree, comparison_tree)
    if not is_bipartitions_updated:
        reference_tree.encode_bipartitions()
        comparison_tree.encode_bipartitions()
    else:
        if reference_tree.bipartition_encoding is None:
            reference_tree.encode_bipartitions()
        if comparison_tree.bipartition_encoding is None:
            comparison_tree.encode_bipartitions()
    ref_bipartitions = set(reference_tree.bipartition_encoding)
    comparison_bipartitions = set(comparison_tree.bipartition_encoding)
    false_positives = comparison_bipartitions.difference(ref_bipartitions)
    false_negatives = ref_bipartitions.difference(comparison_bipartitions)
    return len(false_positives), len(false_negatives)

def euclidean_distance(
        tree1,
        tree2,
        edge_weight_attr="length",
        value_type=float,
        is_bipartitions_updated=False):
    """
    Returns the Euclidean distance (a.k.a. Felsenstein's 2004 "branch length
    distance") between two trees based on ``edge_weight_attr``.

    Trees need to share the same |TaxonNamespace| reference. The
    bipartition bitmasks of the trees must be correct for the current tree
    structures (by calling :meth:`Tree.encode_bipartitions()` method) or the
    ``is_bipartitions_updated`` argument must be |False| to force recalculation of
    bipartitions.

    Parameters
    ----------
    tree1 : |Tree| object
        The first tree of the two trees being compared. This must share the
        same |TaxonNamespace| reference as ``tree2`` and must have
        bipartitions encoded.
    tree2 : |Tree| object
        The second tree of the two trees being compared. This must share the
        same |TaxonNamespace| reference as ``tree1`` and must have
        bipartitions encoded.
    edge_weight_attr : string
        Name of attribute on edges of trees to be used as the weight.
    is_bipartitions_updated : bool
        If |False| (default), then the bipartitions on *both* trees will be updated
        before comparison. If |True| then the bipartitions
        will only be calculated for a |Tree| object if they have not been
        calculated before, either explicitly or implicitly.

    Returns
    -------
    d : int
        The Euclidean distance between ``tree1`` and ``tree2``.

    Notes
    -----
    Encoding bipartitions may collapse an unrooted basal bifurcation or
    suppress unifurcations, modifying the tree. Alternatively, call
    :meth:`Tree.encode_bipartitions()` with desired options then pass
    ``is_bipartitions_updated=True``.

    Examples
    --------

    ::

        import dendropy
        from dendropy.calculate import treecompare
        tns = dendropy.TaxonNamespace()
        tree1 = tree.get_from_path(
                "t1.nex",
                "nexus",
                taxon_namespace=tns)
        tree2 = tree.get_from_path(
                "t2.nex",
                "nexus",
                taxon_namespace=tns)
        tree1.encode_bipartitions()
        tree2.encode_bipartitions()
        print(treecompare.euclidean_distance(tree1, tree2))

    """
    df = lambda length_diffs: math.sqrt(sum([pow(i[0] - i[1], 2) for i in length_diffs]))
    return _bipartition_difference(tree1,
                           tree2,
                           dist_fn=df,
                           edge_weight_attr=edge_weight_attr,
                           value_type=value_type,
                           is_bipartitions_updated=is_bipartitions_updated)

def find_missing_bipartitions(reference_tree, comparison_tree, is_bipartitions_updated=False):
    """
    Returns a list of bipartitions that are in ``reference_tree``, but
    not in ``comparison_tree``.

    Trees need to share the same |TaxonNamespace| reference. The
    bipartition bitmasks of the trees must be correct for the current tree
    structures (by calling :meth:`Tree.encode_bipartitions()` method) or the
    ``is_bipartitions_updated`` argument must be |False| to force recalculation of
    bipartitions.

    Parameters
    ----------
    reference_tree : |Tree| object
        The first tree of the two trees being compared. This must share the
        same |TaxonNamespace| reference as ``tree2`` and must have
        bipartitions encoded.
    comparison_tree : |Tree| object
        The second tree of the two trees being compared. This must share the
        same |TaxonNamespace| reference as ``tree1`` and must have
        bipartitions encoded.
    is_bipartitions_updated : bool
        If |False| (default), then the bipartitions on *both* trees will be updated
        before comparison. If |True| then the bipartitions
        will only be calculated for a |Tree| object if they have not been
        calculated before, either explicitly or implicitly.

    Returns
    -------
    s : list[|Bipartition|]
        A list of bipartitions that are in the first tree but not in the second.

    Notes
    -----
    Encoding bipartitions may collapse an unrooted basal bifurcation or
    suppress unifurcations, modifying the tree. Alternatively, call
    :meth:`Tree.encode_bipartitions()` with desired options then pass
    ``is_bipartitions_updated=True``.
    """
    missing = []
    if reference_tree.taxon_namespace is not comparison_tree.taxon_namespace:
        raise error.TaxonNamespaceIdentityError(reference_tree, comparison_tree)
    if not is_bipartitions_updated:
        reference_tree.encode_bipartitions()
        comparison_tree.encode_bipartitions()
    else:
        if reference_tree.bipartition_encoding is None:
            reference_tree.encode_bipartitions()
        elif reference_tree._has_unrooted_basal_bifurcation():
            reference_tree._warn_using_uncollapsed_bipartition_encoding()
        if comparison_tree.bipartition_encoding is None:
            comparison_tree.encode_bipartitions()
    for bipartition in reference_tree.bipartition_encoding:
        if bipartition in comparison_tree.bipartition_encoding:
            pass
        else:
            missing.append(bipartition)
    return missing

###############################################################################
### Resolution-Aware Split Support

SPLIT_SUPPORT_SUPPORTED = "supported"
SPLIT_SUPPORT_UNRESOLVED = "unresolved"
SPLIT_SUPPORT_CONFLICTING = "conflicting"


class IncomparableTreeScope(object):
    """
    Records why a tree cannot contribute evidence about splits defined on a
    particular leaf set.
    """

    def __init__(self, tree, tree_index, leafset, missing_taxa, extra_taxa):
        self.tree = tree
        self.tree_index = tree_index
        self.leafset = frozenset(leafset)
        self.missing_taxa = tuple(sorted(missing_taxa, key=lambda t: (t.label or "")))
        self.extra_taxa = tuple(sorted(extra_taxa, key=lambda t: (t.label or "")))

    @property
    def missing_taxon_labels(self):
        return tuple(t.label for t in self.missing_taxa)

    @property
    def extra_taxon_labels(self):
        return tuple(t.label for t in self.extra_taxa)

    def __bool__(self):
        return bool(self.missing_taxa or self.extra_taxa)

    def __repr__(self):
        return "<IncomparableTreeScope tree_index={} missing={} extra={}>".format(
                self.tree_index,
                self.missing_taxon_labels,
                self.extra_taxon_labels)

    def as_dict(self):
        return {
                "tree_index": self.tree_index,
                "missing_taxon_labels": self.missing_taxon_labels,
                "extra_taxon_labels": self.extra_taxon_labels,
                }


class SplitSupport(object):
    """
    Support evidence for a single split within a single leaf-set group.

    Each comparable tree is classified as:

    - ``"supported"``: the tree contains the split.
    - ``"unresolved"``: the tree does not contain the split, but every split
      on the tree is compatible with it. This is the signature of a
      polytomy/multifurcation: the tree simply does not resolve the grouping.
    - ``"conflicting"``: the tree contains at least one split incompatible
      with the split. This is positive evidence against the grouping.

    For a single tree, the status is :attr:`support_status`. Across a
    collection, the per-tree statuses are exposed by :attr:`status_by_tree`,
    and :attr:`support_status` follows the majority of informative trees.
    ``support_frequency`` is calculated over *informative* trees only
    (``supported + conflicting``). Unresolved trees are deliberately excluded
    from the denominator; they are not evidence of zero support.
    """

    def __init__(self,
            group,
            bipartition,
            status_by_tree,
            incomparable_scopes=None):
        self.group = group
        self.bipartition = bipartition
        self.split_bitmask = bipartition.split_bitmask
        self.leafset_bitmask = bipartition.leafset_bitmask
        self.tree_leafset_bitmask = bipartition.tree_leafset_bitmask
        self._status_by_tree = collections.OrderedDict(status_by_tree)
        self.incomparable_scopes = tuple(incomparable_scopes or ())
        self.incomparable_tree_indices = tuple(
                scope.tree_index for scope in self.incomparable_scopes)
        taxa = group.taxon_namespace.bitmask_taxa_list(self.leafset_bitmask)
        taxa = sorted(taxa, key=lambda t: (t.label or ""))
        self.leafset_taxa = tuple(taxa)
        self.leafset_labels = tuple(t.label for t in self.leafset_taxa)

    def __repr__(self):
        return "<SplitSupport {} supported={} unresolved={} conflicting={}>".format(
                self.leafset_labels,
                self.supported_count,
                self.unresolved_count,
                self.conflicting_count)

    @property
    def status_by_tree(self):
        return self._status_by_tree

    def _tree_indices_with_status(self, status):
        return tuple(idx for idx, value in self._status_by_tree.items() if value == status)

    @property
    def supported_tree_indices(self):
        return self._tree_indices_with_status(SPLIT_SUPPORT_SUPPORTED)

    @property
    def unresolved_tree_indices(self):
        return self._tree_indices_with_status(SPLIT_SUPPORT_UNRESOLVED)

    @property
    def conflicting_tree_indices(self):
        return self._tree_indices_with_status(SPLIT_SUPPORT_CONFLICTING)

    @property
    def supported_count(self):
        return len(self.supported_tree_indices)

    @property
    def unresolved_count(self):
        return len(self.unresolved_tree_indices)

    @property
    def conflicting_count(self):
        return len(self.conflicting_tree_indices)

    @property
    def comparable_count(self):
        return len(self._status_by_tree)

    @property
    def informative_count(self):
        return self.supported_count + self.conflicting_count

    @property
    def incomparable_count(self):
        return len(self.incomparable_scopes)

    @property
    def total_count(self):
        return self.comparable_count + self.incomparable_count

    @property
    def support_frequency(self):
        """
        Proportion of *informative* comparable trees that contain the split.

        Returns |None| if no comparable tree resolves the split either way.
        """
        if self.informative_count == 0:
            return None
        return float(self.supported_count) / self.informative_count

    @property
    def resolution_frequency(self):
        """
        Proportion of comparable trees that resolve the split (i.e., either
        support or conflict with it).
        """
        if self.comparable_count == 0:
            return None
        return float(self.informative_count) / self.comparable_count

    @property
    def observed_frequency(self):
        """
        Proportion of all comparable trees that contain the split.

        This is provided for descriptive purposes only: unlike
        :attr:`support_frequency`, it counts unresolved trees in the
        denominator, and so should not be interpreted as support.
        """
        if self.comparable_count == 0:
            return None
        return float(self.supported_count) / self.comparable_count

    @property
    def support_status(self):
        """
        Summary status of the split across comparable trees.

        - ``"unresolved"``: no comparable tree resolves the split either way.
        - ``"supported"``: more comparable trees support the split than
          conflict with it.
        - ``"conflicting"``: at least as many comparable trees conflict with
          the split as support it.

        Individual conflicts remain visible through
        :attr:`conflicting_count` and :attr:`conflicting_tree_indices`; a
        split can be ``"supported"`` and still be contradicted by some trees.
        """
        if self.informative_count == 0:
            return SPLIT_SUPPORT_UNRESOLVED
        if self.supported_count > self.conflicting_count:
            return SPLIT_SUPPORT_SUPPORTED
        return SPLIT_SUPPORT_CONFLICTING

    @property
    def is_contested(self):
        """
        |True| if at least one tree supports and at least one tree conflicts
        with the split.
        """
        return self.supported_count > 0 and self.conflicting_count > 0

    @property
    def is_supported(self):
        return self.support_status == SPLIT_SUPPORT_SUPPORTED

    @property
    def is_unresolved(self):
        return self.support_status == SPLIT_SUPPORT_UNRESOLVED

    @property
    def is_conflicting(self):
        return self.support_status == SPLIT_SUPPORT_CONFLICTING

    def is_supported_by_majority(self, min_freq=0.5):
        """
        Returns |True| if the split is resolved by at least one comparable
        tree and its :attr:`support_frequency` is at least ``min_freq``.
        """
        if min_freq is None:
            return self.informative_count > 0
        frequency = self.support_frequency
        return frequency is not None and frequency >= min_freq

    def as_dict(self):
        return {
                "split_bitmask": self.split_bitmask,
                "leafset_bitmask": self.leafset_bitmask,
                "leafset_labels": self.leafset_labels,
                "supported": self.supported_count,
                "unresolved": self.unresolved_count,
                "conflicting": self.conflicting_count,
                "incomparable": self.incomparable_count,
                "comparable": self.comparable_count,
                "informative": self.informative_count,
                "support_frequency": self.support_frequency,
                "resolution_frequency": self.resolution_frequency,
                "observed_frequency": self.observed_frequency,
                "support_status": self.support_status,
                "is_contested": self.is_contested,
                "status_by_tree": dict(self._status_by_tree),
                "incomparable_tree_indices": self.incomparable_tree_indices,
                }


class SplitSupportGroup(object):
    """
    Resolution-aware split support for all trees in a collection that share
    exactly the same leaf set (and rooting state).
    """

    def __init__(self,
            taxon_namespace,
            trees,
            tree_indices,
            leafset,
            is_rooted):
        self.taxon_namespace = taxon_namespace
        self.trees = tuple(trees)
        self.tree_indices = tuple(tree_indices)
        self.leafset = frozenset(leafset)
        self.is_rooted = is_rooted
        self.splits = collections.OrderedDict()
        self.incomparable = []
        self.incomparable_candidates = []
        self.include_trivial = False
        self.all_tree_count = len(self.trees)

    def __len__(self):
        return len(self.splits)

    def __iter__(self):
        return iter(self.splits.values())

    def __getitem__(self, split_bitmask):
        return self.splits[split_bitmask]

    def get(self, split_bitmask, default=None):
        return self.splits.get(split_bitmask, default)

    @property
    def leafset_key(self):
        return tuple(sorted(
            self.taxon_namespace.accession_index(taxon)
            for taxon in self.leafset))

    @property
    def leafset_labels(self):
        return tuple(sorted(taxon.label for taxon in self.leafset))

    @property
    def leafset_bitmask(self):
        return self.taxon_namespace.taxa_bitmask(taxa=self.leafset)

    @property
    def num_trees(self):
        return len(self.trees)

    @property
    def incomparable_tree_indices(self):
        return tuple(scope.tree_index for scope in self.incomparable)

    @property
    def incomparable_count(self):
        return len(self.incomparable)

    @property
    def supported_splits(self):
        return tuple(split for split in self
                if split.support_status == SPLIT_SUPPORT_SUPPORTED)

    @property
    def unresolved_splits(self):
        return tuple(split for split in self
                if split.support_status == SPLIT_SUPPORT_UNRESOLVED)

    @property
    def conflicting_splits(self):
        return tuple(split for split in self
                if split.support_status == SPLIT_SUPPORT_CONFLICTING)

    @property
    def contested_splits(self):
        return tuple(split for split in self if split.is_contested)

    def consensus_tree(self, min_freq=0.5):
        """
        Returns a consensus tree for this leaf set.

        Splits are added in descending order of :attr:`support_frequency`,
        with unresolved trees excluded from the denominator. Splits that are
        not resolved by any comparable tree are never added (and therefore
        remain collapsed).
        """
        from dendropy.datamodel.treemodel import Tree
        included = [split for split in self
                if split.is_supported_by_majority(min_freq=min_freq)
                and split.support_status == SPLIT_SUPPORT_SUPPORTED]
        included.sort(key=lambda split: (
            -(split.support_frequency if split.support_frequency is not None else -1.0),
            -split.supported_count,
            split.leafset_bitmask))
        tree = Tree.from_split_bitmasks(
                split_bitmasks=[split.split_bitmask for split in included],
                taxon_namespace=self.taxon_namespace,
                is_rooted=self.is_rooted)
        tree.retain_taxa(self.leafset)
        return tree

    def as_dict(self):
        return {
                "leafset_labels": self.leafset_labels,
                "num_trees": self.num_trees,
                "tree_indices": self.tree_indices,
                "incomparable_tree_indices": self.incomparable_tree_indices,
                "splits": [split.as_dict() for split in self],
                }


class SplitSupportAnalysis(object):
    """
    Resolution-aware split support across a collection of trees.

    Trees are partitioned by exact leaf set (and rooting state), so that every
    conclusion is made only among comparable trees. Trees with missing or
    extra leaves are recorded in :attr:`incomparable` rather than being
    silently treated as absent support.
    """

    def __init__(self, taxon_namespace, trees, is_rooted, groups):
        self.taxon_namespace = taxon_namespace
        self.trees = tuple(trees)
        self.is_rooted = is_rooted
        self.groups = groups

    def __len__(self):
        return len(self.groups)

    def __iter__(self):
        return iter(self.groups.values())

    def __getitem__(self, leafset_key):
        return self.groups[leafset_key]

    @property
    def num_groups(self):
        return len(self.groups)

    @property
    def leafsets(self):
        return tuple(self.groups.keys())

    def get_group(self, labels=None, taxa=None, leafset_bitmask=None):
        """
        Returns the leaf-set group matching the given criteria, or |None|.
        """
        if leafset_bitmask is not None:
            key = tuple(sorted(
                self.taxon_namespace.accession_index(taxon)
                for taxon in self.taxon_namespace.bitmask_taxa_list(leafset_bitmask)))
            return self.groups.get(key)
        if taxa is None:
            taxa = self.taxon_namespace.get_taxa(labels=labels)
        key = tuple(sorted(
            self.taxon_namespace.accession_index(taxon)
            for taxon in taxa))
        return self.groups.get(key)

    @property
    def primary_group(self):
        """
        Group with the largest number of trees (ties broken by leaf-set key).
        """
        if not self.groups:
            return None
        return max(self.groups.values(),
                key=lambda group: (group.num_trees, tuple(-i for i in group.leafset_key)))

    @property
    def incomparable(self):
        """
        Incomparable-tree records relative to :attr:`primary_group`.
        """
        group = self.primary_group
        if group is None:
            return tuple()
        return tuple(group.incomparable)

    @property
    def incomparable_tree_indices(self):
        return tuple(scope.tree_index for scope in self.incomparable)

    @property
    def incomparable_by_leafset(self):
        return collections.OrderedDict(
                (group.leafset_key, tuple(group.incomparable))
                for group in self.groups.values())

    def consensus_trees(self, min_freq=0.5):
        """
        Returns an ordered mapping of leaf-set key to consensus tree.
        """
        return collections.OrderedDict(
                (group.leafset_key, group.consensus_tree(min_freq=min_freq))
                for group in self.groups.values())

    def all_splits(self):
        """
        Returns a flat tuple of all :class:`SplitSupport` records, grouped by
        leaf set and then in each group's support order.
        """
        records = []
        for group in self.groups.values():
            records.extend(group)
        return tuple(records)

    def as_dict(self):
        return {
                "is_rooted": self.is_rooted,
                "num_trees": len(self.trees),
                "groups": [group.as_dict() for group in self.groups.values()],
                "incomparable": [scope.as_dict() for scope in self.incomparable],
                }


def _is_candidate_taxon(obj):
    from dendropy.datamodel.taxonmodel import Taxon
    return isinstance(obj, Taxon)


def _normalize_candidate_specifications(candidates=None, candidate_labels=None):
    specs = []
    if candidates is not None:
        from dendropy.datamodel.treemodel import Bipartition
        if isinstance(candidates, Bipartition) or isinstance(candidates, int) or _is_candidate_taxon(candidates):
            specs.append(candidates)
        else:
            candidates = list(candidates)
            if not candidates:
                pass
            elif all(isinstance(x, str) for x in candidates):
                specs.append(candidates)
            elif all(_is_candidate_taxon(x) for x in candidates):
                specs.append(candidates)
            else:
                specs.extend(candidates)
    if candidate_labels is not None:
        if isinstance(candidate_labels, str):
            specs.append([candidate_labels])
        else:
            candidate_labels = list(candidate_labels)
            if candidate_labels and all(isinstance(x, str) for x in candidate_labels):
                specs.append(candidate_labels)
            else:
                specs.extend(candidate_labels)
    return specs


def _candidate_leafset_bitmask(spec, taxon_namespace, group_leafset):
    from dendropy.datamodel.treemodel import Bipartition
    from dendropy.datamodel.taxonmodel import Taxon
    if isinstance(spec, Bipartition):
        if spec.tree_leafset_bitmask is not None:
            group_mask = taxon_namespace.taxa_bitmask(taxa=group_leafset)
            if (spec.tree_leafset_bitmask & group_mask) != spec.tree_leafset_bitmask:
                return None, "candidate leaf set is not contained in group leaf set"
        return spec.leafset_bitmask, None
    if isinstance(spec, int):
        return spec, None
    if isinstance(spec, Taxon):
        return taxon_namespace.taxon_bitmask(spec), None
    taxa = []
    for item in spec:
        if isinstance(item, Taxon):
            taxa.append(item)
        else:
            taxon = taxon_namespace.get_taxon(label=item)
            if taxon is None:
                return None, "taxon label not found in namespace: {}".format(item)
            taxa.append(taxon)
    return taxon_namespace.taxa_bitmask(taxa=taxa), None


def summarize_split_support(trees,
        candidates=None,
        candidate_labels=None,
        min_freq=0.5,
        is_rooted=None,
        is_bipartitions_updated=False,
        include_trivial=False):
    """
    Summarizes split support across a collection of trees while treating
    multifurcations as unresolved rather than as zero support.

    For each exact leaf set (and rooting state), every candidate split is
    evaluated against every tree in that group and classified as:

    - ``"supported"``: the split is present on the tree.
    - ``"unresolved"``: the split is absent, but the tree contains no split
      incompatible with it (typically because of a polytomy).
    - ``"conflicting"``: the tree contains at least one split incompatible
      with the candidate.

    :attr:`SplitSupport.support_frequency` uses only the informative
    (supported plus conflicting) trees as its denominator. Unresolved trees
    therefore do not depress support values. ``observed_frequency`` is also
    available for callers that explicitly want the proportion over all
    comparable trees.

    Trees whose leaf sets differ from a group's leaf set are not compared
    against that group's splits. They are recorded as
    :class:`IncomparableTreeScope` objects, with the missing and extra taxa,
    under ``group.incomparable`` and ``analysis.incomparable``.

    Parameters
    ----------
    trees : iterable of |Tree| objects
        Trees to summarize. They must share the same |TaxonNamespace| object
        and rooting state. A |TreeList| is accepted directly.
    candidates : iterable, optional
        Candidate splits. Each candidate may be a |Bipartition|, a split
        bitmask, an iterable of |Taxon| objects, or an iterable of taxon
        labels. If |None|, all non-trivial splits observed on at least one
        tree in each leaf-set group are used. Candidates that are not
        contained within a group's leaf set are skipped for that group and
        recorded in ``group.incomparable_candidates``.
    candidate_labels : iterable of iterables of str, optional
        Convenience form for candidates specified as label collections.
    min_freq : float
        Threshold used by :meth:`SplitSupport.is_supported_by_majority` and
        by consensus-tree construction. This does not change the recorded
        counts or ``support_frequency``.
    is_rooted : bool, optional
        Rooting state to assume. If |None|, the state of the first tree is
        used and all trees must agree.
    is_bipartitions_updated : bool
        If |False| (default), bipartitions are recalculated on every tree.
        If |True|, existing encodings are reused when present.
    include_trivial : bool
        If |False| (default), trivial splits (single leaf versus the rest)
        are excluded.

    Returns
    -------
    SplitSupportAnalysis
        Grouped split-support results, including incomparable-tree scopes and
        consensus-tree construction.
    """
    from dendropy.datamodel.treemodel import Bipartition

    trees = list(trees)
    if not trees:
        raise ValueError("At least one tree is required")
    taxon_namespace = trees[0].taxon_namespace
    for tree in trees[1:]:
        if tree.taxon_namespace is not taxon_namespace:
            raise error.TaxonNamespaceIdentityError(trees[0], tree)
    if is_rooted is None:
        is_rooted = trees[0].is_rooted
    for tree in trees:
        if tree.is_rooted is not is_rooted:
            raise error.MixedRootingError(
                    "Cannot summarize split support across mixed rooting states")

    tree_leafsets = []
    for tree_index, tree in enumerate(trees):
        if not is_bipartitions_updated:
            tree.encode_bipartitions()
        elif tree.bipartition_encoding is None:
            tree.encode_bipartitions()
        elif tree._has_unrooted_basal_bifurcation():
            tree._warn_using_uncollapsed_bipartition_encoding()
        leafset = frozenset(
                node.taxon for node in tree.leaf_node_iter()
                if node.taxon is not None)
        if len(leafset) != len(tree.leaf_nodes()):
            raise ValueError(
                    "Tree at index {} has leaf nodes without associated taxa; "
                    "split support requires every leaf to have a taxon".format(tree_index))
        tree_leafsets.append(leafset)

    groups_by_leafset = collections.OrderedDict()
    for tree_index, leafset in enumerate(tree_leafsets):
        groups_by_leafset.setdefault(leafset, []).append(tree_index)
    sorted_leafsets = sorted(
            groups_by_leafset,
            key=lambda leafset: (
                -len(groups_by_leafset[leafset]),
                tuple(sorted(
                    taxon_namespace.accession_index(taxon)
                    for taxon in leafset))))

    analysis_groups = collections.OrderedDict()
    candidate_specs = _normalize_candidate_specifications(
            candidates=candidates,
            candidate_labels=candidate_labels)

    for leafset in sorted_leafsets:
        tree_indices = groups_by_leafset[leafset]
        group_trees = [trees[idx] for idx in tree_indices]
        group = SplitSupportGroup(
                taxon_namespace=taxon_namespace,
                trees=group_trees,
                tree_indices=tree_indices,
                leafset=leafset,
                is_rooted=is_rooted)
        group.all_tree_count = len(trees)
        group.include_trivial = include_trivial
        group_mask = group.leafset_bitmask
        group_tree_splits = {}
        for tree_index, tree in zip(tree_indices, group_trees):
            group_tree_splits[tree_index] = [
                    b.split_bitmask for b in tree.bipartition_encoding
                    if include_trivial or not b.is_trivial()]

        candidate_bipartitions = collections.OrderedDict()
        if candidate_specs:
            for spec in candidate_specs:
                leafset_bitmask, reason = _candidate_leafset_bitmask(
                        spec=spec,
                        taxon_namespace=taxon_namespace,
                        group_leafset=leafset)
                if leafset_bitmask is None:
                    group.incomparable_candidates.append({
                        "candidate": spec,
                        "reason": reason,
                        })
                    continue
                if (leafset_bitmask & group_mask) != leafset_bitmask:
                    group.incomparable_candidates.append({
                        "candidate": spec,
                        "reason": "candidate includes taxa outside group leaf set",
                        })
                    continue
                leafset_bitmask &= group_mask
                if leafset_bitmask == 0 or leafset_bitmask == group_mask:
                    continue
                bipartition = Bipartition(
                        leafset_bitmask=leafset_bitmask,
                        tree_leafset_bitmask=group_mask,
                        is_rooted=is_rooted,
                        compile_bipartition=True)
                if bipartition.is_trivial() and not include_trivial:
                    continue
                candidate_bipartitions.setdefault(
                        bipartition.split_bitmask, bipartition)
        else:
            for tree in group_trees:
                for bipartition in tree.bipartition_encoding:
                    if bipartition.split_bitmask in (0, group_mask):
                        continue
                    if bipartition.is_trivial() and not include_trivial:
                        continue
                    observed = Bipartition(
                            bitmask=bipartition.split_bitmask,
                            tree_leafset_bitmask=group_mask,
                            is_rooted=is_rooted,
                            compile_bipartition=True)
                    candidate_bipartitions.setdefault(
                            observed.split_bitmask, observed)

        for bipartition in candidate_bipartitions.values():
            candidate_split = bipartition.split_bitmask
            status_by_tree = collections.OrderedDict()
            for tree_index, tree in zip(tree_indices, group_trees):
                tree_splits = group_tree_splits[tree_index]
                if candidate_split in tree_splits:
                    status = SPLIT_SUPPORT_SUPPORTED
                elif any(not Bipartition.is_compatible_bitmasks(
                        candidate_split, other_split, group_mask)
                        for other_split in tree_splits):
                    status = SPLIT_SUPPORT_CONFLICTING
                else:
                    status = SPLIT_SUPPORT_UNRESOLVED
                status_by_tree[tree_index] = status
            group.splits[candidate_split] = SplitSupport(
                    group=group,
                    bipartition=bipartition,
                    status_by_tree=status_by_tree)

        for tree_index, other_leafset in enumerate(tree_leafsets):
            if other_leafset == leafset:
                continue
            group.incomparable.append(IncomparableTreeScope(
                    tree=trees[tree_index],
                    tree_index=tree_index,
                    leafset=leafset,
                    missing_taxa=leafset.difference(other_leafset),
                    extra_taxa=other_leafset.difference(leafset)))

        for split in group.splits.values():
            split.incomparable_scopes = tuple(group.incomparable)
            split.incomparable_tree_indices = tuple(
                    scope.tree_index for scope in group.incomparable)

        sorted_splits = sorted(
                group.splits.values(),
                key=lambda split: (
                    -(split.support_frequency if split.support_frequency is not None else -1.0),
                    -split.supported_count,
                    split.conflicting_count,
                    split.leafset_bitmask))
        group.splits = collections.OrderedDict(
                (split.split_bitmask, split) for split in sorted_splits)
        analysis_groups[group.leafset_key] = group

    return SplitSupportAnalysis(
            taxon_namespace=taxon_namespace,
            trees=trees,
            is_rooted=is_rooted,
            groups=analysis_groups)


def split_support_summary(trees, *args, **kwargs):
    """
    Alias for :func:`summarize_split_support`.
    """
    return summarize_split_support(trees, *args, **kwargs)


class SplitComparison(object):
    """
    Pairwise split comparison restricted to the leaf set shared by two trees.

    Attributes
    ----------
    reference_tree, comparison_tree : |Tree|
        The original trees.
    shared_leafset : frozenset[|Taxon|]
        Taxa present on both trees.
    reference_only_taxa, comparison_only_taxa : tuple[|Taxon|]
        Taxa absent from the other tree, recorded as incomparable scope.
    group : SplitSupportGroup or None
        Resolution-aware support for every non-trivial split observed on
        either tree after restricting both to ``shared_leafset``. |None| if
        the shared leaf set has fewer than four taxa (no non-trivial splits
        are possible).
    """

    def __init__(self,
            reference_tree,
            comparison_tree,
            shared_leafset,
            reference_only_taxa,
            comparison_only_taxa,
            group):
        self.reference_tree = reference_tree
        self.comparison_tree = comparison_tree
        self.shared_leafset = frozenset(shared_leafset)
        self.reference_only_taxa = tuple(reference_only_taxa)
        self.comparison_only_taxa = tuple(comparison_only_taxa)
        self.group = group

    def __repr__(self):
        return "<SplitComparison shared={} reference_only={} comparison_only={}>".format(
                len(self.shared_leafset),
                len(self.reference_only_taxa),
                len(self.comparison_only_taxa))

    @property
    def is_comparable(self):
        return self.group is not None

    @property
    def reference_only_taxon_labels(self):
        return tuple(t.label for t in self.reference_only_taxa)

    @property
    def comparison_only_taxon_labels(self):
        return tuple(t.label for t in self.comparison_only_taxa)

    def _splits(self):
        return tuple(self.group) if self.group is not None else tuple()

    @staticmethod
    def _pairwise_status(split):
        statuses = set(split.status_by_tree.values())
        if SPLIT_SUPPORT_CONFLICTING in statuses:
            return SPLIT_SUPPORT_CONFLICTING
        if statuses == {SPLIT_SUPPORT_SUPPORTED}:
            return SPLIT_SUPPORT_SUPPORTED
        return SPLIT_SUPPORT_UNRESOLVED

    @property
    def splits(self):
        """
        Every non-trivial split observed on either restricted tree.
        """
        return self._splits()

    @property
    def status_by_split(self):
        return collections.OrderedDict(
                (split.split_bitmask, self._pairwise_status(split))
                for split in self._splits())

    @property
    def supported(self):
        """
        Splits resolved identically by both trees.
        """
        return tuple(split for split in self._splits()
                if self._pairwise_status(split) == SPLIT_SUPPORT_SUPPORTED)

    @property
    def unresolved(self):
        """
        Splits resolved by one tree but left unresolved by the other, with
        no contradiction from either.
        """
        return tuple(split for split in self._splits()
                if self._pairwise_status(split) == SPLIT_SUPPORT_UNRESOLVED)

    @property
    def conflicting(self):
        """
        Splits contradicted by at least one tree.
        """
        return tuple(split for split in self._splits()
                if self._pairwise_status(split) == SPLIT_SUPPORT_CONFLICTING)

    @property
    def contested(self):
        return tuple(split for split in self._splits()
                if split.is_contested)

    def as_dict(self):
        return {
                "shared_leafset_labels": tuple(sorted(
                    taxon.label for taxon in self.shared_leafset)),
                "reference_only_taxon_labels": self.reference_only_taxon_labels,
                "comparison_only_taxon_labels": self.comparison_only_taxon_labels,
                "comparable": self.is_comparable,
                "splits": [
                    dict(split.as_dict(),
                        pairwise_status=self._pairwise_status(split))
                    for split in self._splits()],
                }


def compare_splits(reference_tree,
        comparison_tree,
        is_bipartitions_updated=False):
    """
    Compares the splits of two trees on their shared leaf set.

    Both trees are restricted to the taxa common to both before encoding
    splits. This avoids reporting a split as conflicting merely because taxa
    are missing from one tree. Taxa unique to either input are reported in
    :attr:`SplitComparison.reference_only_taxa` and
    :attr:`SplitComparison.comparison_only_taxa`.

    For each non-trivial split observed on either (restricted) tree, the
    result classifies it against the other tree as:

    - ``"supported"``: both trees contain the split.
    - ``"unresolved"``: one tree contains the split and the other leaves it
      unresolved, without contradicting it (e.g., a polytomy there).
    - ``"conflicting"``: at least one tree contains a split incompatible
      with it.

    Parameters
    ----------
    reference_tree, comparison_tree : |Tree|
        Trees to compare. They must share the same |TaxonNamespace| reference
        and rooting state.
    is_bipartitions_updated : bool
        Retained for API consistency. The restricted trees are always
        freshly encoded.

    Returns
    -------
    SplitComparison
    """
    if reference_tree.taxon_namespace is not comparison_tree.taxon_namespace:
        raise error.TaxonNamespaceIdentityError(reference_tree, comparison_tree)
    if reference_tree.is_rooted is not comparison_tree.is_rooted:
        raise error.MixedRootingError(
                "Cannot compare splits across mixed rooting states")

    def leafset(tree):
        return frozenset(
                node.taxon for node in tree.leaf_node_iter()
                if node.taxon is not None)

    reference_leafset = leafset(reference_tree)
    comparison_leafset = leafset(comparison_tree)
    shared_leafset = reference_leafset.intersection(comparison_leafset)
    reference_only = sorted(
            reference_leafset.difference(comparison_leafset),
            key=lambda t: (t.label or ""))
    comparison_only = sorted(
            comparison_leafset.difference(reference_leafset),
            key=lambda t: (t.label or ""))

    if len(shared_leafset) < 4:
        return SplitComparison(
                reference_tree=reference_tree,
                comparison_tree=comparison_tree,
                shared_leafset=shared_leafset,
                reference_only_taxa=reference_only,
                comparison_only_taxa=comparison_only,
                group=None)
    return SplitComparison(
            reference_tree=reference_tree,
            comparison_tree=comparison_tree,
            shared_leafset=shared_leafset,
            reference_only_taxa=reference_only,
            comparison_only_taxa=comparison_only,
            group=_compare_restricted_splits(
                reference_tree=reference_tree,
                comparison_tree=comparison_tree,
                shared_leafset=shared_leafset))


def _restrict_tree_to_leafset(tree, leafset):
    """
    Returns a copy of ``tree`` containing only ``leafset``, without modifying
    the original; returns |None| if the restriction has fewer than four
    leaves.
    """
    if len(leafset) < 4:
        return None
    return tree.extract_tree_with_taxa(taxa=leafset)


def _compare_restricted_splits(reference_tree, comparison_tree, shared_leafset):
    if len(shared_leafset) < 4:
        return None
    reference_restricted = _restrict_tree_to_leafset(
            reference_tree, shared_leafset)
    comparison_restricted = _restrict_tree_to_leafset(
            comparison_tree, shared_leafset)
    if reference_restricted is None or comparison_restricted is None:
        return None
    reference_restricted.encode_bipartitions()
    comparison_restricted.encode_bipartitions()
    candidate_splits = []
    seen_split_bitmasks = set()
    for tree in (reference_restricted, comparison_restricted):
        for bipartition in tree.bipartition_encoding:
            if not bipartition.split_bitmask or bipartition.is_trivial():
                continue
            if bipartition.split_bitmask in seen_split_bitmasks:
                continue
            seen_split_bitmasks.add(bipartition.split_bitmask)
            candidate_splits.append(bipartition)
    analysis = summarize_split_support(
            trees=[reference_restricted, comparison_restricted],
            candidates=candidate_splits,
            is_bipartitions_updated=True)
    return analysis.primary_group


def classify_split_on_tree(tree,
        bipartition,
        is_bipartitions_updated=False):
    """
    Classifies a single candidate split against a single tree.

    Parameters
    ----------
    tree : |Tree|
        Tree against which the split is evaluated.
    bipartition : |Bipartition|
        Candidate split. Its ``tree_leafset_bitmask`` must cover the tree's
        leaf set for the result to be meaningful.
    is_bipartitions_updated : bool
        If |False| (default), bipartitions are recalculated on ``tree``.

    Returns
    -------
    str
        ``"supported"`` if the split is present on the tree,
        ``"unresolved"`` if the split is absent but no tree split conflicts
        with it, or ``"conflicting"`` if at least one tree split is
        incompatible with it.
    """
    from dendropy.datamodel.treemodel import Bipartition
    if not is_bipartitions_updated:
        tree.encode_bipartitions()
    elif tree.bipartition_encoding is None:
        tree.encode_bipartitions()
    tree_leafset_mask = tree.seed_node.edge.bipartition.leafset_bitmask
    if isinstance(bipartition, Bipartition):
        candidate_leafset = bipartition.leafset_bitmask
    else:
        candidate_leafset = int(bipartition)
    if (candidate_leafset & tree_leafset_mask) != candidate_leafset:
        raise ValueError(
                "Candidate split includes taxa outside the tree leaf set")
    if candidate_leafset == 0 or candidate_leafset == tree_leafset_mask:
        raise ValueError("Candidate split must be a non-trivial leafset")
    bipartition = Bipartition(
            leafset_bitmask=candidate_leafset,
            tree_leafset_bitmask=tree_leafset_mask,
            is_rooted=tree.is_rooted,
            compile_bipartition=True)
    candidate_split = bipartition.split_bitmask
    tree_splits = [
            b.split_bitmask for b in tree.bipartition_encoding
            if not b.is_trivial()]
    if candidate_split in tree_splits:
        return SPLIT_SUPPORT_SUPPORTED
    elif any(not Bipartition.is_compatible_bitmasks(
            candidate_split, other_split, tree_leafset_mask)
            for other_split in tree_splits):
        return SPLIT_SUPPORT_CONFLICTING
    return SPLIT_SUPPORT_UNRESOLVED

##############################################################################
### TreeshapeKernel

class TreeShapeKernel(object):

    _TreeShapeKernelNodeCache = collections.namedtuple("_TreeShapeKernelNodeCache",
            ["production", "index", "edge_lengths", "sum_of_square_edge_lengths"])

    def __init__(self, **kwargs):
        """
        Calculator for tree shape kernel tricking.

        References
        ----------

        [1] Poon, A. F., Pond, S. L. K., Bennett, P., Richman, D. D., Brown, A.
        J. L., & Frost, S. D. (2007). Adaptation to human populations is
        revealed by within-host polymorphisms in HIV-1 and hepatitis C virus.
        PLoS Pathog, 3(3), e45.

        [2] Poon, A. F., Walker, L. W., Murray, H., McCloskey, R. M., Harrigan,
        P. R., & Liang, R. H. (2013). Mapping the shapes of phylogenetic trees
        from human and zoonotic RNA viruses. PLoS one, 8(11), e78122.

        [3] Poon, A. F., Walker, L. W., Murray, H., McCloskey, R. M., Harrigan,
        P. R., & Liang, R. H. (2013). Mapping the shapes of phylogenetic trees
        from human and zoonotic RNA viruses. PLoS one, 8(11), e78122.

        [4] Poon, A. F. (2015). Phylodynamic inference with kernel ABC and its
        application to HIV epidemiology. Molecular biology and evolution,
        msv123.

        """

        # kernel function
        # sigma=1,
        # gauss_factor=1,
        # decay_factor=0.1,
        self.sigma = kwargs.pop("sigma", 1)
        self.gauss_factor = kwargs.pop("gauss_factor", 1)
        self.decay_factor = kwargs.pop("decay_factor", 0.1)

        # cache management
        self._tree_cache = {}

    def remove_from_cache(self, tree):
        del self._tree_cache[tree]

    def update_cache(self, tree):
        """
        Pre-computes values needed for the kernel trick with this tree and
        caches them.
        """
        current_tree_cache = {}
        for nd in tree.leaf_node_iter():
            current_tree_cache[nd] = TreeShapeKernel._TreeShapeKernelNodeCache(
                    production=0,
                    index=0,
                    edge_lengths=None,
                    sum_of_square_edge_lengths=0)
            nd.production = 0
        for nd_idx, nd in enumerate(tree.postorder_internal_node_iter()):
            nterms = 0
            edge_lengths = []
            for ch in nd.child_node_iter():
                if current_tree_cache[ch].production == 0:
                    nterms += 1
                edge_lengths.append(ch.edge.length)
            production = nterms + 1
            index = nd_idx
            sum_of_square_edge_lengths = sum([elen**2 for elen in edge_lengths])
            current_tree_cache[nd] = TreeShapeKernel._TreeShapeKernelNodeCache(
                    production=production,
                    index=index,
                    edge_lengths=edge_lengths,
                    sum_of_square_edge_lengths=sum_of_square_edge_lengths)
        self._tree_cache[tree] = current_tree_cache
        return current_tree_cache

    def __call__(self,
            tree1,
            tree2,
            is_tree1_cache_updated=False,
            is_tree2_cache_updated=False,
            ):
        """
        Recursive function for computing tree convolution
        kernel.

        Parameters
        ----------
        tree1 : |Tree| instance
            First tree to be compared. If it has already been seen by self, its
            values will have been cached. If the tree has changed since it has been
            seen by self, it will need to be recached, either explicitly before
            the calculation by calling 'update_cache' or by specifying
            'is_tree1_cache_updated=False'
        tree2 : |Tree| instance
            Second tree to be compared. If it has already been seen by self, its
            values will have been cached. If the tree has changed since it has been
            seen by self, it will need to be recached, either explicitly before
            the calculation by calling 'update_cache' or by specifying
            'is_tree2_cache_updated=True'
        is_tree1_cache_updated : bool
            If ``tree1`` has not been seen before, then this is ignored as the
            cache will be updated regardless. If ``tree1`` has been seen, then
            the cached values representing it will be used unless
            ``is_tree1_cache_updated`` is ``False``.
        is_tree2_cache_updated : bool
            If ``tree1`` has not been seen before, then this is ignored as the
            cache will be updated regardless. If ``tree1`` has been seen, then
            the cached values representing it will be used unless
            ``is_tree1_cache_updated`` is ``False``.

        Acknowledgements
        ----------------

        Based in part on:

            KAMPHIR
            https://github.com/ArtPoon/kamphir.git
            KAMPHIR is written and maintained by: Art F.Y. Poon.
            With major contributions from: Rosemary McCloskey

        Copyright (c) 2015, Art Poon. All rights reserved.
        See https://github.com/ArtPoon/kamphir/blob/master/LICENSE.md for
        more license information.

        Original work adapted from Moschitti (2006) Making tree kernels
        practical for natural language learning. Proceedings of the
        11th Conference of the European Chapter of the Association
        for Computational Linguistics.
        """
        internal_nodes2 = list(tree2.postorder_internal_node_iter())
        k = 0
        if not is_tree1_cache_updated:
            tree1_cache = self.update_cache(tree1)
        else:
            try:
                tree1_cache = self._tree_cache[tree1]
            except KeyError:
                tree1_cache = self.update_cache(tree1)
        if not is_tree2_cache_updated:
            tree2_cache = self.update_cache(tree2)
        else:
            try:
                tree2_cache = self._tree_cache[tree2]
            except KeyError:
                tree2_cache = self.update_cache(tree2)
        dp_matrix = {}
        for ni, tree1_node in enumerate(tree1.postorder_internal_node_iter()):
            tree1_cache_node = tree1_cache[tree1_node]
            for tree2_node in internal_nodes2:
                tree2_cache_node = tree2_cache[tree2_node]
                if tree1_cache_node.production != tree2_cache_node.production:
                    continue
                res = self.decay_factor * math.exp( -1. / self.gauss_factor
                    * (tree1_cache_node.sum_of_square_edge_lengths + tree2_cache_node.sum_of_square_edge_lengths - 2*sum([(tree1_cache_node.edge_lengths[i]*tree2_cache_node.edge_lengths[i]) for i in range(len(tree1_cache_node.edge_lengths))])))
                ## TODO:
                ##  - (check and) handles cases where unequal number of children
                ##  - how to handle rotation mismatch problems? or do we assume
                ##    trees have equal rotations
                # for node_idx in range(2):
                #     c1 = tree1_node.clades[node_idx]
                #     c2 = tree2_node.clades[node_idx]
                for c1, c2 in zip(tree1_node.child_node_iter(), tree2_node.child_node_iter()):
                    if tree1_cache[c1].production != tree2_cache[c2].production:
                        continue
                    if tree1_cache[c1].production == 0:
                        # branches are terminal
                        res *= self.sigma + self.decay_factor
                    else:
                        try:
                            res *= self.sigma + dp_matrix[(tree1_cache[c1].index, tree2_cache[c2].index)]
                        except KeyError:
                            res *= self.sigma
                dp_matrix[(tree1_cache[tree1_node].index, tree2_cache[tree2_node].index)] = res
                k += res
        return k

##############################################################################
### AssemblageInducedTree

class AssemblageInducedTreeManager(object):

    def __init__(self, *args, **kwargs):
        self.is_exchangeable_assemblage_classifications = kwargs.pop("is_exchangeable_assemblage_classifications", True)
        self._num_assemblage_classifications = kwargs.pop("num_assemblages", None)
        self.induced_tree_factory = kwargs.pop("induced_tree_factory", None)
        self.induced_tree_node_factory = kwargs.pop("induced_tree_node_factory", None)
        self.skip_null_assemblages = kwargs.pop("skip_null_assemblages", False)
        self._tree_assemblage_induced_trees_map = {}

    def remove_from_cache(self, tree):
        del self._tree_assemblage_induced_trees_map[tree]

    def generate_induced_trees(self, tree, assemblage_leaf_sets):
        if assemblage_leaf_sets is None:
            if self._num_assemblage_classifications is not None:
                raise ValueError("Expecting {} assemblage leaf set classifications, but none provided".format(self._num_assemblage_classifications))
        else:
            if self._num_assemblage_classifications is None:
                self._num_assemblage_classifications = len(assemblage_leaf_sets)
            elif len(assemblage_leaf_sets) != self._num_assemblage_classifications:
                    raise ValueError("Expecting {} assemblage leaf set classifications, but only {} specified".format(
                        self._num_assemblage_classifications,
                        len(assemblage_leaf_sets)))
        induced_trees = []
        for idx, assemblage_leaf_set in enumerate(assemblage_leaf_sets):
            if len(assemblage_leaf_set) == 0:
                if self.skip_null_assemblages:
                    continue
                raise error.NullLeafSetException()
            node_filter_fn = lambda nd: nd in assemblage_leaf_set
            induced_tree = tree.extract_tree(
                               node_filter_fn=node_filter_fn,
                               is_apply_filter_to_leaf_nodes=True,
                               is_apply_filter_to_internal_nodes=False,
                               tree_factory=self.induced_tree_factory,
                               node_factory=self.induced_tree_node_factory)
            induced_trees.append(induced_tree)
        self._tree_assemblage_induced_trees_map[tree] = induced_trees
        return induced_trees

##############################################################################
### AssemblageInducedTreeShapeKernel

class AssemblageInducedTreeShapeKernel(TreeShapeKernel, AssemblageInducedTreeManager):

    @staticmethod
    def _euclidean_distance(v1, v2, is_weight_values_by_comparison_size=True):
        v1_size = len(v1)
        v2_size = len(v2)
        v1_idx = 0
        v2_idx = 0
        if v1_size > v2_size:
            v1_idx = v1_size - v2_size
            weight = float(v2_size)
        elif v2_size > v1_size:
            v2_idx = v2_size - v1_size
            weight = float(v1_size)
        else:
            weight = float(v1_size)
        if not is_weight_values_by_comparison_size:
            weight = 1.0
        ss = 0.0
        while v1_idx < v1_size and v2_idx < v2_size:
            ss += pow(v1[v1_idx]/weight - v2[v2_idx]/weight, 2)
            v1_idx += 1
            v2_idx += 1
        return math.sqrt(ss)

    def __init__(self, *args, **kwargs):
        self.exchangeable_assemblage_comparison_strategy = kwargs.pop("exchangeable_assemblage_comparison_strategy", "joint minimum")
        TreeShapeKernel.__init__(self, *args, **kwargs)
        AssemblageInducedTreeManager.__init__(self, *args, **kwargs)

    def remove_from_cache(self, tree):
        for induced_tree in self._tree_assemblage_induced_trees_map[tree]:
            TreeShapeKernel.remove_from_cache(self, induced_tree)
        TreeShapeKernel.remove_from_cache(self, tree)
        AssemblageInducedTreeManager.remove_from_cache(self, tree)

    def update_assemblage_induced_tree_cache(self,
            tree,
            assemblage_leaf_sets):
        self.update_cache(tree=tree)
        induced_trees = self.generate_induced_trees(tree=tree,
                assemblage_leaf_sets=assemblage_leaf_sets)
        for induced_tree in induced_trees:
            self.update_cache(tree=induced_tree)

    def __call__(self,
            tree1,
            tree2,
            tree1_assemblage_leaf_sets,
            tree2_assemblage_leaf_sets,
            is_tree1_cache_updated=False,
            is_tree2_cache_updated=False,
            ):
        main_trees_score = TreeShapeKernel.__call__(self,
                tree1=tree1,
                tree2=tree2,
                is_tree1_cache_updated=is_tree1_cache_updated,
                is_tree2_cache_updated=is_tree2_cache_updated,
                )
        if not is_tree1_cache_updated or tree1 not in self._tree_assemblage_induced_trees_map:
            if tree1_assemblage_leaf_sets is None:
                raise ValueError("Uncached tree requires specification of 'tree1_assemblage_leaf_sets'")
            self.update_assemblage_induced_tree_cache(
                    tree=tree1,
                    assemblage_leaf_sets=tree1_assemblage_leaf_sets)
        if not is_tree2_cache_updated or tree2 not in self._tree_assemblage_induced_trees_map:
            if tree2_assemblage_leaf_sets is None:
                raise ValueError("Uncached tree requires specification of 'tree2_assemblage_leaf_sets'")
            self.update_assemblage_induced_tree_cache(
                    tree=tree2,
                    assemblage_leaf_sets=tree2_assemblage_leaf_sets)
        ## ++ main tree score
        score_table = collections.OrderedDict()
        score_table["primary.tree.kernel.trick.distance"] = main_trees_score
        induced_trees1 = self._tree_assemblage_induced_trees_map[tree1]
        induced_trees2 = self._tree_assemblage_induced_trees_map[tree2]
        # assert len(induced_trees1) == len(induced_trees2) == self._num_assemblage_classifications
        if not self.is_exchangeable_assemblage_classifications:
            if len(induced_trees1) != len(induced_trees2):
                raise TypeError("Different numbers of induced trees not supported for non-exchangeable classifications: {} vs. {}".format(len(induced_trees1), len(induced_trees2)))
            for idx, (induced_tree1, induced_tree2) in enumerate(zip(induced_trees1, induced_trees2)):
                s = TreeShapeKernel.__call__(self,
                                tree1=induced_tree1,
                                tree2=induced_tree2,
                                is_tree1_cache_updated=True,
                                is_tree2_cache_updated=True,
                                )
                ## ++ raw scores direct comparisons of each of the induced trees
                score_table["induced.tree.{}.kernel.trick.distance".format(idx+1)] = s
        else:
            if self.exchangeable_assemblage_comparison_strategy == "joint minimum":
                # if lengths are different, we want to fix the smaller set
                if len(induced_trees1) > len(induced_trees2):
                    induced_trees2, induced_trees1 = induced_trees1, induced_trees2
                comparison_vector = [0.0] * len(induced_trees1)
                current_minimum_distance = None
                current_joint_minimum_vector = None
                for induced_trees_permutation in itertools.permutations(induced_trees2, len(induced_trees1)):
                    distances = []
                    for t2, t1 in zip(induced_trees_permutation, induced_trees1):
                        distances.append(TreeShapeKernel.__call__(self,
                                tree1=t1,
                                tree2=t2,
                                is_tree1_cache_updated=True,
                                is_tree2_cache_updated=True,))
                    euclidean_distance = self._euclidean_distance(distances, comparison_vector)
                    if current_minimum_distance is None or euclidean_distance < current_minimum_distance:
                        current_minimum_distance = euclidean_distance
                        current_joint_minimum_vector = distances
                for didx, d in enumerate(distances):
                    score_table["induced.tree.{}.kernel.trick.distance".format(didx+1)] = d
                for didx in range(didx+1, self._num_assemblage_classifications):
                    score_table["induced.tree.{}.kernel.trick.distance".format(didx+1)] = "NA"
            else:
                raise NotImplementedError()
        return score_table

###############################################################################
## Legacy

def robinson_foulds_distance(tree1, tree2, edge_weight_attr="length"):
    """
    DEPRECATED: Use :func:``symmetric_difference`` for the common
    unweighged Robinson-Fould's distance metric (i.e., the symmetric difference between two trees)
    :func:``weighted_robinson_foulds_distance`` or for the RF distance as defined by Felsenstein, 2004.
    """
    return weighted_robinson_foulds_distance(tree1, tree2, edge_weight_attr)

def mason_gamer_kellogg_score(tree1, tree2, is_bipartitions_updated=False):
    """
    Mason-Gamer and Kellogg. Testing for phylogenetic conflict among molecular
    data sets in the tribe Triticeae (Gramineae). Systematic Biology (1996)
    vol. 45 (4) pp. 524
    """
    if tree1.taxon_namespace is not tree2.taxon_namespace:
        raise error.TaxonNamespaceIdentityError(tree1, tree2)
    if not is_bipartitions_updated:
        tree1.encode_bipartitions()
        tree2.encode_bipartitions()
    else:
        if tree1.bipartition_encoding is None:
            tree1.encode_bipartitions()
        if tree2.bipartition_encoding is None:
            tree2.encode_bipartitions()
    se1 = tree1.bipartition_encoding
    se2 = tree2.bipartition_encoding
    bipartitions = sorted(list(set(se1.keys() + se2.keys())))

###############################################################################
## Supporting

def _get_length_diffs(
        tree1,
        tree2,
        edge_weight_attr="length",
        value_type=float,
        is_bipartitions_updated=False,
        bipartition_length_diff_map=False):
    """
    Returns a list of tuples, with the first element of each tuple representing
    the length of the branch subtending a particular bipartition on ``tree1``, and
    the second element the length of the same branch on ``tree2``. If a
    particular bipartition is found on one tree but not in the other, a value of zero
    is used for the missing bipartition.
    """
    length_diffs = []
    bipartition_length_diffs = {}
    if tree1.taxon_namespace is not tree2.taxon_namespace:
        raise error.TaxonNamespaceIdentityError(tree1, tree2)
    if not is_bipartitions_updated:
        tree1.encode_bipartitions()
        tree2.encode_bipartitions()
    else:
        for tree in (tree1, tree2):
            if tree.bipartition_encoding is None:
                tree.encode_bipartitions()
            elif tree._has_unrooted_basal_bifurcation():
                tree._warn_using_uncollapsed_bipartition_encoding()

    tree1_bipartition_edge_map = dict(tree2.bipartition_edge_map) # O(n*(2*bind + dict_item_cost))
    tree2_bipartition_edge_map = tree1.bipartition_edge_map
    for bipartition in tree2_bipartition_edge_map: # O n : 2*bind
        edge = tree2_bipartition_edge_map[bipartition]
        elen1 = getattr(edge, edge_weight_attr) # attr + bind
        if elen1 is None:
            elen1 = 0 # worst-case: bind
        value1 = value_type(elen1) #  ctor + bind
        try:
            e2 = tree1_bipartition_edge_map.pop(bipartition) # attr + dict_lookup + bind
            elen2 = getattr(e2, edge_weight_attr) # attr + bind
            if elen2 is None:
                # allow root edge to have bipartition with no value: raise error if not root edge
                if e2.tail_node is None:
                    elen2 = 0.0
                else:
                    raise ValueError("Edge length attribute is 'None': Tree: %s ('%s'), Split: %s" % (id(tree2), tree2.label, bipartition.leafset_as_newick_string(tree2.taxon_namespace)))
        except KeyError: # excep
            elen2 = 0.0
        value2 = value_type(elen2) #  ctor + bind # best case
        # if abs(value2-value1) > 1e-5:
        #     print("{}: {}, {}".format(bipartition.leafset_as_newick_string(tree1.taxon_namespace), value2, value1))
        length_diffs.append((value1,value2)) # ctor + listappend
        bipartition_length_diffs[bipartition] = length_diffs[-1]

    for bipartition in tree1_bipartition_edge_map: # best-case not executed, worst case O(n) : 2*bind
        edge = tree1_bipartition_edge_map[bipartition]
        elen2 = getattr(edge, edge_weight_attr) # attr +  bind
        if elen2 is None:
            elen2 = 0
        value2 = value_type(elen2) #  ctor + bind
        e1 = tree2_bipartition_edge_map.get(bipartition) # attr + dict_lookup + bind
        if e1 is None:
            elen1 = 0.0
        else:
            elen1 = getattr(e1, edge_weight_attr) # attr  + bind
            if elen1 is None:
                # allow root edge to have bipartition with no value: raise error if not root edge
                if e1.tail_node is None:
                    elen1 = 0.0
                else:
                    raise ValueError("Edge length attribute is 'None': Tree: %s ('%s'), Split: %s" % (id(tree1), tree1.label, bipartition))
                #elen1 = 0
        value1 = value_type(elen1)
        length_diffs.append((value1,value2)) # ctor + listappend
        bipartition_length_diffs[bipartition] = length_diffs[-1]

    # the numbers below do not reflect additions to the code to protect against
    #   edges with length None
    # loops
    #  best-case:
    #   O(n * (dict_lookup + 3*attr + 3*ctor + 7*bind + listappend))
    #  worst-case:
    #     separated: O(n * (2*dict_lookup + 4*attr + 3*ctor + 8*bind + listappend + excep) + n*(2*dict_lookup + 4*attr + 3*ctor + 8*bind + listappend))
    #   or:
    #     O(2n*(2*dict_lookup + 4*attr + 3*ctor + 8*bind + listappend + 0.5*excep))

    # total
    #  best-case:
    #       O(n * (dict_lookup + 3*attr + 3*ctor + 8*bind + listappend + dict_item_cost))
    #  worst-case:
    #     O(2n*(2*dict_lookup + 4*attr + 3*ctor + 9*bind + listappend + 0.5*(dict_item_cost + excep))
    if bipartition_length_diff_map:
        return length_diffs, bipartition_length_diffs
    else:
        return length_diffs

def _bipartition_difference(
        tree1,
        tree2,
        dist_fn,
        edge_weight_attr="length",
        value_type=float,
        is_bipartitions_updated=False):
    """
    Returns distance between two trees, each represented by a dictionary of
    bipartitions (as bipartition_mask strings) to edges, using ``dist_fn`` to calculate the
    distance based on ``edge_weight_attr`` of the edges. ``dist_fn`` is a function
    that takes a list of pairs of values, where the values correspond to the edge
    lengths of a given bipartition on tree1 and tree2 respectively.
    """
    length_diffs = _get_length_diffs(
            tree1,
            tree2,
            edge_weight_attr=edge_weight_attr,
            value_type=value_type,
            is_bipartitions_updated=is_bipartitions_updated)
    return dist_fn(length_diffs)

