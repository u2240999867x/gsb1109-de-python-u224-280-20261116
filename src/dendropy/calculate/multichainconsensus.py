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
##     Moreno, M. A., & Sukumaran, J. (2024). DendroPy 4: a
##     mature Python library for phylogenetic computing. Journal of Open
##     Source Software, 9(101), 6943, https://doi.org/10.21105/joss.06943
##
##############################################################################

"""
Multi-chain tree-sample consensus.

Builds a consensus tree from tree samples produced by multiple independent
chains (e.g. independent MCMC runs), with explicit handling of:

    - per-chain burn-in (the same number of leading trees is discarded from
      each chain, and discarded trees never vote),
    - sample identity (a retransmitted source does not vote a second time,
      while equally indexed samples from distinct chains remain independent
      votes),
    - per-chain and per-tree weights,
    - a minimum support requirement for a split to be eligible, and
    - selection of the maximum total-support-weight set of *mutually
      compatible* eligible splits.

When the minimum support threshold is below a majority, mutually incompatible
splits can each clear it, so not every split that passes the threshold can be
placed on one tree. Eligible splits are therefore treated as a weighted
conflict graph, and a maximum-weight independent set (an exact branch-and-bound
search, not a greedy pass) is solved on the conflicting core. Ties for the
optimum are enumerated so that the ambiguity is reported rather than resolved
silently, and every eligible split left out of the chosen tree retains a
record of the selected split(s) it conflicts with.
"""

import os

import dendropy


class MultiChainConsensusError(Exception):
    pass


class ChainTreeSource:
    """
    A single chain of tree samples contributing votes to the consensus.

    Parameters
    ----------
    path : str
        Path to the file of sampled trees. Either ``path`` or ``trees`` must be
        given, but not both.
    trees : |TreeList| or iterable of |Tree|
        In-memory sampled trees. Either ``path`` or ``trees`` must be given,
        but not both.
    label : str
        Human-readable identity of the chain. Defaults to the file name of
        ``path``, or a generated label for in-memory chains. Used in coverage
        reports; it does not affect voting.
    burnin : int
        Number of leading trees of this chain to discard as burn-in. Burned-in
        trees are not counted and do not vote.
    weight : float
        Chain-level weight. Every vote cast by a retained tree of this chain
        is multiplied by this weight (and, when tree weights are enabled, by
        the tree's own weight).
    schema : str
        Format of ``path`` (e.g. ``"newick"`` or ``"nexus"``). Defaults to
        ``"newick"``.
    tree_reader_kwargs : dict
        Extra keyword arguments passed through to the tree reader.
    """

    def __init__(self,
            path=None,
            trees=None,
            label=None,
            burnin=0,
            weight=1.0,
            schema="newick",
            tree_reader_kwargs=None):
        if path is None and trees is None:
            raise MultiChainConsensusError(
                    "A ChainTreeSource requires either a 'path' or 'trees'")
        if path is not None and trees is not None:
            raise MultiChainConsensusError(
                    "A ChainTreeSource cannot be given both 'path' and 'trees'")
        try:
            burnin = int(burnin)
        except (TypeError, ValueError):
            raise MultiChainConsensusError(
                    "'burnin' must be a non-negative integer, got: {}".format(burnin))
        if burnin < 0:
            raise MultiChainConsensusError(
                    "'burnin' must be a non-negative integer, got: {}".format(burnin))
        weight = float(weight)
        if weight <= 0:
            raise MultiChainConsensusError(
                    "'weight' must be positive, got: {}".format(weight))
        self.path = os.path.realpath(path) if path is not None else None
        self.trees = list(trees) if trees is not None else None
        if label is not None:
            self.label = str(label)
        elif self.path is not None:
            self.label = os.path.basename(self.path)
        else:
            self.label = None
        self.burnin = burnin
        self.weight = weight
        self.schema = schema
        self.tree_reader_kwargs = dict(tree_reader_kwargs) if tree_reader_kwargs else {}

    def identity_key(self):
        """
        Stable identity used to detect retransmission.

        File-backed chains are identified by resolved path; in-memory chains by
        object identity. Two registrations sharing a key represent the same
        transmitted sample, and the second registration does not vote.
        """
        if self.path is not None:
            return ("path", self.path)
        return ("memory", id(self.trees))

    def is_missing(self):
        return self.path is not None and not os.path.exists(self.path)


class MultiChainConsensusResult:
    """
    Outcome of a multi-chain consensus build.

    Attributes
    ----------
    consensus_tree : |Tree|
        The consensus tree assembled from the chosen compatible splits.
    selected_splits : frozenset of int
        Bitmasks of the splits placed on ``consensus_tree``.
    eligible_splits : frozenset of int
        Bitmasks of every split that cleared the minimum support requirement,
        including those left out because they were incompatible with a chosen
        split.
    split_weights : dict
        Raw total support weight accumulated by each observed split.
    split_frequencies : dict
        Support weight of each observed split as a fraction of
        ``total_voting_weight``.
    split_conflicts : dict
        Mapping from every eligible split bitmask to the set of eligible split
        bitmasks it is incompatible with. For an eligible split absent from
        ``selected_splits``, the members of this set that are in
        ``selected_splits`` are the conflicting split(s) that excluded it.
    is_tie : bool
        |True| if more than one compatible split set attains the optimum total
        support weight.
    alternative_optimal_sets : list of frozensets
        Optimal compatible split sets other than the one used to build the
        tree. Empty when there is a unique optimum; may be truncated per the
        builder's enumeration limit.
    is_tie_enumeration_truncated : bool
        |True| if the number of tied optima exceeded the enumeration limit.
    chain_coverage : list of dict
        Per-chain coverage statements: label, source, burn-in, number of trees
        available/discarded/voting, missing-file flag, weights and the total
        voting weight the chain contributed.
    total_voting_weight : float
        Sum of the weights of all retained voting trees across all chains.
    min_support : float
        The minimum support requirement applied.
    min_support_is_fraction : bool
        Whether ``min_support`` was interpreted as a fraction of the total
        voting weight (|True|) or as an absolute support weight (|False|).
    """

    def __init__(self, **kwargs):
        self.consensus_tree = kwargs["consensus_tree"]
        self.selected_splits = frozenset(kwargs["selected_splits"])
        self.eligible_splits = frozenset(kwargs["eligible_splits"])
        self.split_weights = dict(kwargs["split_weights"])
        self.split_frequencies = dict(kwargs["split_frequencies"])
        self.split_conflicts = {
                split: set(conflicts)
                for split, conflicts in kwargs["split_conflicts"].items()}
        self.is_tie = kwargs["is_tie"]
        self.alternative_optimal_sets = [
                frozenset(option) for option in kwargs["alternative_optimal_sets"]]
        self.is_tie_enumeration_truncated = kwargs["is_tie_enumeration_truncated"]
        self.chain_coverage = list(kwargs["chain_coverage"])
        self.total_voting_weight = kwargs["total_voting_weight"]
        self.min_support = kwargs["min_support"]
        self.min_support_is_fraction = kwargs["min_support_is_fraction"]

    def conflicts_for(self, split):
        """
        Returns the set of eligible split bitmasks incompatible with ``split``.
        """
        return set(self.split_conflicts.get(split, set()))

    def rejected_eligible_splits(self):
        """
        Returns the eligible splits not placed on the consensus tree.
        """
        return [split for split in self.eligible_splits if split not in self.selected_splits]

    def describe_coverage(self):
        """
        Returns a human-readable multi-line coverage statement.
        """
        lines = []
        for entry in self.chain_coverage:
            if entry["is_missing"]:
                lines.append(
                        "Chain '{}': source '{}' missing; no votes cast "
                        "(coverage retained, 0 voting trees)".format(
                            entry["label"], entry["path"]))
                continue
            if entry["was_retransmission_dropped"]:
                lines.append(
                        "Chain '{}': retransmission of '{}' dropped; "
                        "no additional votes cast".format(
                            entry["label"], entry["path"]))
                continue
            lines.append(
                    "Chain '{label}': {trees_voting} voting tree(s) "
                    "({trees_discarded} of {trees_available} discarded as burn-in); "
                    "chain weight {chain_weight}; contributed support weight "
                    "{contributed_weight}".format(**entry))
        lines.append("Total voting weight: {}".format(self.total_voting_weight))
        return "\n".join(lines)


class MultiChainConsensusBuilder:
    """
    Accumulates tree samples from multiple independent chains and builds a
    consensus tree under a minimum support requirement.

    Parameters
    ----------
    taxon_namespace : |TaxonNamespace|
        Shared namespace for all chains. Created on first use if |None|.
    min_support : float
        Minimum support a split must reach to be eligible. Interpreted as a
        fraction of total voting weight when ``min_support_is_fraction`` is
        |True| (so 0.5 is a majority requirement), otherwise as an absolute
        support weight.
    min_support_is_fraction : bool
        Selects fractional versus absolute interpretation of ``min_support``.
    use_tree_weights : bool
        If |True|, each tree's own ``weight`` attribute multiplies its chain
        weight; otherwise every retained tree casts a vote of its chain weight.
    is_rooted : bool
        Rooting state of the consensus tree. If |None|, inferred from the
        sampled trees when they are uniformly rooted or unrooted.
    max_optimal_set_enumerations : int
        Cap on the number of tied optimal compatible sets enumerated.
    support_epsilon : float
        Numerical tolerance for support threshold and optimum-weight
        comparisons.
    """

    def __init__(self,
            taxon_namespace=None,
            min_support=0.5,
            min_support_is_fraction=True,
            use_tree_weights=False,
            is_rooted=None,
            max_optimal_set_enumerations=128,
            support_epsilon=1e-12):
        if min_support_is_fraction and not (0.0 <= float(min_support) <= 1.0):
            raise MultiChainConsensusError(
                    "Fractional 'min_support' must be in [0, 1], got: {}".format(min_support))
        if float(min_support) < 0:
            raise MultiChainConsensusError(
                    "'min_support' must be non-negative, got: {}".format(min_support))
        if max_optimal_set_enumerations < 1:
            raise MultiChainConsensusError(
                    "'max_optimal_set_enumerations' must be >= 1")
        self.taxon_namespace = taxon_namespace
        self.min_support = float(min_support)
        self.min_support_is_fraction = bool(min_support_is_fraction)
        self.use_tree_weights = bool(use_tree_weights)
        self.is_rooted = is_rooted
        self.max_optimal_set_enumerations = int(max_optimal_set_enumerations)
        self.support_epsilon = float(support_epsilon)
        self._sources = []
        self._seen_identity_keys = set()

    def add_chain(self, source):
        """
        Registers a :class:`ChainTreeSource`.

        A source whose identity has already been registered is retained for
        coverage but marked as a retransmission: it is not read or counted a
        second time.
        """
        if not isinstance(source, ChainTreeSource):
            raise MultiChainConsensusError(
                    "add_chain() expects a ChainTreeSource instance")
        identity_key = source.identity_key()
        is_retransmission = identity_key in self._seen_identity_keys
        if not is_retransmission:
            self._seen_identity_keys.add(identity_key)
        self._sources.append((source, is_retransmission))
        return source

    def add_chain_file(self,
            path,
            burnin=0,
            weight=1.0,
            label=None,
            schema="newick",
            tree_reader_kwargs=None):
        source = ChainTreeSource(
                path=path,
                label=label,
                burnin=burnin,
                weight=weight,
                schema=schema,
                tree_reader_kwargs=tree_reader_kwargs)
        return self.add_chain(source)

    def add_chain_trees(self,
            trees,
            burnin=0,
            weight=1.0,
            label=None,
            tree_reader_kwargs=None):
        source = ChainTreeSource(
                trees=trees,
                label=label,
                burnin=burnin,
                weight=weight,
                tree_reader_kwargs=tree_reader_kwargs)
        return self.add_chain(source)

    def build(self):
        """
        Reads all registered chains, tallies weighted split votes, and returns
        a :class:`MultiChainConsensusResult`.
        """
        split_weights = {}
        rooting_types = set()
        chain_coverage = []
        total_voting_weight = 0.0
        auto_label_index = 0

        for source, was_retransmission in self._sources:
            auto_label_index += 1
            label = source.label if source.label is not None else "chain-{}".format(auto_label_index)
            coverage = {
                    "label": label,
                    "path": source.path,
                    "burnin": source.burnin,
                    "chain_weight": source.weight,
                    "use_tree_weights": self.use_tree_weights,
                    "trees_available": 0,
                    "trees_discarded": 0,
                    "trees_voting": 0,
                    "contributed_weight": 0.0,
                    "is_missing": source.is_missing(),
                    "was_retransmission_dropped": was_retransmission,
                    }

            if was_retransmission:
                chain_coverage.append(coverage)
                continue
            if coverage["is_missing"]:
                # Coverage statement is retained, but the absent chain casts no
                # votes and does not alter any support weight.
                chain_coverage.append(coverage)
                continue

            trees = self._load_trees(source)
            coverage["trees_available"] = len(trees)
            coverage["trees_discarded"] = min(source.burnin, len(trees))
            voting_trees = trees[source.burnin:]
            coverage["trees_voting"] = len(voting_trees)

            for tree in voting_trees:
                if self.taxon_namespace is None:
                    self.taxon_namespace = tree.taxon_namespace
                elif tree.taxon_namespace is not self.taxon_namespace:
                    tree.migrate_taxon_namespace(self.taxon_namespace)
                tree.encode_bipartitions()
                rooting_types.add(bool(tree.is_rooted))
                tree_weight = source.weight
                if self.use_tree_weights and tree.weight is not None:
                    tree_weight *= float(tree.weight)
                total_voting_weight += tree_weight
                coverage["contributed_weight"] += tree_weight
                for bipartition in tree.bipartition_encoding:
                    split = bipartition.split_bitmask
                    if dendropy.Bipartition.is_trivial_bitmask(
                            split, self.taxon_namespace.all_taxa_bitmask()):
                        continue
                    split_weights[split] = split_weights.get(split, 0.0) + tree_weight

            chain_coverage.append(coverage)

        if self.taxon_namespace is None or total_voting_weight <= 0.0:
            raise MultiChainConsensusError(
                    "No voting trees available: every chain was missing, "
                    "retransmitted, or fully consumed by burn-in")

        split_frequencies = {
                split: weight / total_voting_weight
                for split, weight in split_weights.items()}

        eligible_splits = self._select_eligible_splits(split_weights, split_frequencies)
        split_conflicts = self._map_conflicts(eligible_splits)
        selected_splits, alternatives, is_tie, truncated = self._choose_optimal_compatible_set(
                eligible_splits=eligible_splits,
                split_weights=split_weights,
                split_conflicts=split_conflicts)
        consensus_tree = self._build_consensus_tree(
                selected_splits=selected_splits,
                split_weights=split_weights,
                split_frequencies=split_frequencies,
                rooting_types=rooting_types)

        return MultiChainConsensusResult(
                consensus_tree=consensus_tree,
                selected_splits=selected_splits,
                eligible_splits=eligible_splits,
                split_weights=split_weights,
                split_frequencies=split_frequencies,
                split_conflicts=split_conflicts,
                is_tie=is_tie,
                alternative_optimal_sets=alternatives,
                is_tie_enumeration_truncated=truncated,
                chain_coverage=chain_coverage,
                total_voting_weight=total_voting_weight,
                min_support=self.min_support,
                min_support_is_fraction=self.min_support_is_fraction)

    def _load_trees(self, source):
        if source.trees is not None:
            return [tree for tree in source.trees]
        reader_kwargs = dict(source.tree_reader_kwargs)
        if self.taxon_namespace is not None:
            reader_kwargs["taxon_namespace"] = self.taxon_namespace
        tree_list = dendropy.TreeList.get_from_path(
                source.path,
                source.schema,
                **reader_kwargs)
        return list(tree_list)

    def _select_eligible_splits(self, split_weights, split_frequencies):
        eligible = []
        for split in split_weights:
            if self.min_support_is_fraction:
                support = split_frequencies[split]
            else:
                support = split_weights[split]
            if support + self.support_epsilon >= self.min_support:
                eligible.append(split)
        return frozenset(eligible)

    def _map_conflicts(self, eligible_splits):
        fill_bitmask = self.taxon_namespace.all_taxa_bitmask()
        conflicts = {split: set() for split in eligible_splits}
        eligible = list(eligible_splits)
        for idx in range(len(eligible)):
            split1 = eligible[idx]
            for jdx in range(idx + 1, len(eligible)):
                split2 = eligible[jdx]
                if not dendropy.Bipartition.is_compatible_bitmasks(
                        split1, split2, fill_bitmask):
                    conflicts[split1].add(split2)
                    conflicts[split2].add(split1)
        return conflicts

    def _choose_optimal_compatible_set(self,
            eligible_splits,
            split_weights,
            split_conflicts):
        """
        Solves the maximum-weight independent set problem on the conflict
        graph of eligible splits exactly via branch-and-bound, enumerating
        every optimum up to the configured cap.

        Splits with no conflicts are unconditionally included: they are
        compatible with every eligible split and carry positive weight.
        """
        universal = frozenset(
                split for split in eligible_splits if not split_conflicts[split])
        conflict_core = [
                split for split in eligible_splits if split_conflicts[split]]
        universal_weight = sum(split_weights[split] for split in universal)

        adjacency = {
                split: set(split_conflicts[split]) for split in conflict_core}
        optimal_sets = []
        best_weight = None
        truncated = False

        def record(core_set, core_weight):
            nonlocal best_weight, optimal_sets, truncated
            total_weight = universal_weight + core_weight
            if best_weight is None or total_weight > best_weight + self.support_epsilon:
                # A genuinely better optimum: any ties recorded so far were
                # only tied with a weaker weight and must be discarded.
                best_weight = total_weight
                optimal_sets = [frozenset(universal) | frozenset(core_set)]
                truncated = False
            elif abs(total_weight - best_weight) <= self.support_epsilon:
                candidate = frozenset(universal) | frozenset(core_set)
                if candidate not in optimal_sets:
                    if len(optimal_sets) < self.max_optimal_set_enumerations:
                        optimal_sets.append(candidate)
                    else:
                        # The search still runs to completion so that
                        # best_weight remains provably optimal; only the
                        # bookkeeping of extra tied optima is truncated.
                        truncated = True

        def branch_and_bound(remaining, chosen, chosen_weight):
            if not remaining:
                record(chosen, chosen_weight)
                return
            upper_bound = chosen_weight + sum(split_weights[s] for s in remaining)
            if best_weight is not None and universal_weight + upper_bound + self.support_epsilon < best_weight:
                return
            # Branch on the most constrained remaining split.
            pivot = max(
                    remaining,
                    key=lambda s: len(adjacency[s] & remaining))
            remaining_without_pivot = remaining - {pivot}
            # Exclude the pivot first, then include it (dropping its neighbors).
            branch_and_bound(remaining_without_pivot, chosen, chosen_weight)
            included = chosen | {pivot}
            included_weight = chosen_weight + split_weights[pivot]
            remaining_after_include = remaining_without_pivot - adjacency[pivot]
            branch_and_bound(remaining_after_include, included, included_weight)

        if conflict_core:
            branch_and_bound(set(conflict_core), frozenset(), 0.0)
        else:
            record(frozenset(), 0.0)

        selected = optimal_sets[0]
        alternatives = optimal_sets[1:]
        is_tie = len(optimal_sets) > 1 or truncated
        return selected, alternatives, is_tie, truncated

    def _build_consensus_tree(self,
            selected_splits,
            split_weights,
            split_frequencies,
            rooting_types):
        if self.is_rooted is not None:
            is_rooted = self.is_rooted
        elif len(rooting_types) == 1:
            is_rooted = next(iter(rooting_types))
        else:
            is_rooted = None
        ordered_splits = sorted(
                selected_splits,
                key=lambda split: split_weights[split],
                reverse=True)
        consensus_tree = dendropy.Tree.from_split_bitmasks(
                split_bitmasks=ordered_splits,
                taxon_namespace=self.taxon_namespace,
                is_rooted=is_rooted)
        consensus_tree.encode_bipartitions()
        for node in consensus_tree.postorder_internal_node_iter():
            split = node.edge.bipartition.split_bitmask
            if split in split_frequencies:
                node.support = split_frequencies[split]
                node.annotations.drop(name="support")
                node.annotations.add_bound_attribute("support")
        return consensus_tree


def multi_chain_consensus_tree(chains,
        min_support=0.5,
        min_support_is_fraction=True,
        use_tree_weights=False,
        is_rooted=None,
        taxon_namespace=None):
    """
    Convenience wrapper: builds a multi-chain consensus from an iterable of
    :class:`ChainTreeSource` objects and returns the
    :class:`MultiChainConsensusResult`.
    """
    builder = MultiChainConsensusBuilder(
            taxon_namespace=taxon_namespace,
            min_support=min_support,
            min_support_is_fraction=min_support_is_fraction,
            use_tree_weights=use_tree_weights,
            is_rooted=is_rooted)
    for chain in chains:
        builder.add_chain(chain)
    return builder.build()
