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
Enumeration of root placements for a tree of unknown rooting.

Given a tree whose rooting is not (yet) determined, a collection of
*outgroup constraints* (sets of taxa, each of which is required to be
monophyletic -- i.e., to form a clade -- in the rooted tree) and a
collection of *ancestor constraints* (requirements that a particular node
of the original tree be ancestral to a particular set of taxa in the
rooted tree), this module finds **all** root placements that can satisfy
the constraints -- not just, e.g., the placement implied by the first
outgroup given.

A root placement is either:

-   a **node position**: the root is placed at an existing (internal)
    node of the tree, or
-   an **edge position**: the root is placed on an existing edge, which
    is then split into two daughter edges by the new root node.

If the tree has multifurcations (polytomies), some placements may only
become valid once the polytomies are resolved (refined).  The range of
polytomies that may be refined is specified by the caller through the
``refinable_nodes`` argument.  Placements are classified accordingly:

-   *certain* (:attr:`RootPosition.is_certain` is ``True``): the
    placement satisfies all constraints in the tree as given.  Because
    refining a polytomy never destroys an existing clade and never
    changes the leaf set descended from any pre-existing node, such a
    placement remains valid under *every* allowed refinement -- it is
    necessarily feasible.
-   *refinement-dependent* (:attr:`RootPosition.is_certain` is
    ``False``): the placement does not satisfy the constraints in the
    tree as given, but does satisfy them under *some* allowed
    refinement.  The nodes that must be resolved are reported in
    :attr:`RootPosition.required_refinements`.

If no placement is valid (certain or refinement-dependent), a
:class:`RootingConflictError` is raised as a conflict declaration,
carrying a structured report of how each constraint excluded the
candidate placements.

Candidate rooted trees are generated as new |Tree| objects; the source
tree is never modified and can always be re-read or reused.  In a
generated tree, every edge corresponds to an edge of the original tree
and carries that original edge's branch length and annotations (i.e.,
branch lengths and edge annotations migrate along the original edge
identity).  When the root is placed on an edge, the original edge is
split into two daughter edges following a fixed, auditable convention
(see :func:`generate_rooted_trees` and :class:`RootedTreeCandidate`):
the daughter edge on the original tail-node side receives ``length1``
and the daughter edge on the original head-node side receives
``length2``; by default the original length is divided equally between
the two, and if exactly one of the two lengths is given the other
receives the remainder, so the two daughter lengths always sum to the
original edge length.  Both daughter edges carry (independent) copies
of the original edge's annotations, and both are mapped back to the
original edge in the candidate's ``edge_map`` so the convention can be
verified.
"""

from dendropy.datamodel.treemodel import Tree

__all__ = [
    "RootingConflictError",
    "RootPosition",
    "RootedTreeCandidate",
    "find_root_positions",
    "generate_rooted_trees",
]


## Sentinel used as the (virtual) root of an oriented tree when the root
## is placed on an edge: the virtual root's children are the two endpoints
## of the split edge.
_VIRTUAL_ROOT = object()


class RootingConflictError(Exception):
    """
    Raised when no candidate root placement can satisfy all of the given
    outgroup and ancestor constraints.

    The ``report`` attribute is a dictionary describing the conflict:

    -   ``"evaluated_positions"``: number of candidate placements checked.
    -   ``"outgroup_rejections"``: list with one entry per outgroup
        constraint, giving the number of candidate placements at which
        the outgroup cannot be monophyletic (even allowing refinements
        within the caller-specified scope).
    -   ``"ancestor_rejections"``: list with one entry per ancestor
        constraint, giving the number of candidate placements at which
        the constraint is violated.
    """

    def __init__(self, message, report=None):
        super().__init__(message)
        self.report = report if report is not None else {}


class RootPosition:
    """
    A single candidate root placement on a tree of unknown rooting.

    Attributes
    ----------
    tree : |Tree|
        The original (source) tree to which this placement refers.
    kind : str
        Either ``"node"`` (root placed at an existing node;
        :attr:`RootPosition.NODE`) or ``"edge"`` (root placed on an
        existing edge, splitting it; :attr:`RootPosition.EDGE`).
    target : |Node| or |Edge|
        The node or edge of the original tree at which the root is placed.
    is_certain : bool
        ``True`` if the placement satisfies all constraints in the tree
        as given (and hence under every allowed refinement);
        ``False`` if the placement is only valid under some allowed
        refinements of the tree's polytomies.
    required_refinements : tuple of |Node|
        Nodes of the original tree that must be resolved (refined) for
        this placement to satisfy the constraints.  Empty for certain
        placements.
    """

    NODE = "node"
    EDGE = "edge"

    def __init__(self, tree, kind, target, is_certain, required_refinements=()):
        if kind not in (RootPosition.NODE, RootPosition.EDGE):
            raise ValueError("Invalid root position kind: {!r}".format(kind))
        self.tree = tree
        self.kind = kind
        self.target = target
        self.is_certain = bool(is_certain)
        self.required_refinements = tuple(required_refinements)

    @property
    def node(self):
        "The target node, or ``None`` if this is an edge position."
        return self.target if self.kind == RootPosition.NODE else None

    @property
    def edge(self):
        "The target edge, or ``None`` if this is a node position."
        return self.target if self.kind == RootPosition.EDGE else None

    @property
    def requires_refinement(self):
        "``True`` if this placement is only valid under some allowed refinement."
        return not self.is_certain

    def describe(self):
        "Returns a human-readable description of this placement."
        if self.kind == RootPosition.NODE:
            target_desc = "node {!r}".format(self.target.label)
        else:
            tail_label = self.target.tail_node.label if self.target.tail_node else None
            head_label = self.target.head_node.label if self.target.head_node else None
            target_desc = "edge {!r}->{!r}".format(tail_label, head_label)
        feasibility = "certain" if self.is_certain else "refinement-dependent"
        return "root at {} ({})".format(target_desc, feasibility)

    def derive_tree(self, length1=None, length2=None):
        """
        Generates and returns a new rooted |Tree| corresponding to this
        placement.

        This is a convenience wrapper around :func:`generate_rooted_trees`;
        see :class:`RootedTreeCandidate` for the conventions governing
        branch length and annotation migration and the splitting of the
        root edge.  The original tree is not modified.
        """
        return _build_candidate(self, length1=length1, length2=length2).tree

    def __repr__(self):
        return "<RootPosition {}>".format(self.describe())

    def __str__(self):
        return self.describe()


class RootedTreeCandidate:
    """
    A rooted tree generated from a :class:`RootPosition`, together with
    the provenance information needed to audit how it was derived.

    Attributes
    ----------
    position : :class:`RootPosition`
        The placement from which this tree was generated.
    tree : |Tree|
        The generated rooted tree (``is_rooted`` is ``True``).  The tree
        shares the original tree's taxon namespace.
    node_map : dict
        Maps each node of the generated tree to the node of the original
        tree from which it derives.  The seed node of an edge-placement
        tree is newly created by splitting the root edge and maps to
        ``None``.
    edge_map : dict
        Maps each edge of the generated tree to the edge of the original
        tree from which it derives its identity, branch length, and
        annotations.  Both daughter edges of a split root edge map to the
        same original edge.
    split_edges : tuple of (|Edge|, |Edge|) or ``None``
        For edge placements, the two daughter edges of the split original
        edge, as ``(tail-side edge, head-side edge)``: the first leads
        toward the original edge's tail node and carries ``length1``,
        the second toward the original edge's head node and carries
        ``length2``.  ``None`` for node placements.
    """

    def __init__(self, position, tree, node_map, edge_map, split_edges=None):
        self.position = position
        self.tree = tree
        self.node_map = node_map
        self.edge_map = edge_map
        self.split_edges = split_edges


class _OutgroupConstraint:
    "A set of taxa required to be monophyletic in the rooted tree."

    def __init__(self, taxa):
        self.taxa = tuple(taxa)
        self.bits = 0

    def describe(self):
        return "outgroup {{{}}}".format(
            ", ".join(t.label for t in self.taxa)
        )


class _AncestorConstraint:
    "A requirement that ``node`` be ancestral to all of ``taxa`` in the rooted tree."

    def __init__(self, node, taxa):
        self.node = node
        self.taxa = tuple(taxa)
        self.bits = 0

    def describe(self):
        return "ancestor constraint (node {!r} ancestral to {{{}}})".format(
            self.node.label,
            ", ".join(t.label for t in self.taxa),
        )


class _RootingFrame:
    """
    The tree oriented (as an undirected topology) away from a candidate
    root placement.
    """

    def __init__(self, tree, kind, target):
        self.tree = tree
        self.kind = kind
        self.target = target
        self.nodes = list(tree.preorder_node_iter())
        adjacency = {nd: [] for nd in self.nodes}
        for nd in self.nodes:
            for ch in nd.child_node_iter():
                adjacency[nd].append(ch)
                adjacency[ch].append(nd)
        self.parent = {}
        self.children = {nd: [] for nd in self.nodes}
        self.children[_VIRTUAL_ROOT] = []
        if kind == RootPosition.NODE:
            roots = [target]
            self.parent[target] = _VIRTUAL_ROOT
        else:
            tail = target.tail_node
            head = target.head_node
            roots = [tail, head]
            self.parent[tail] = _VIRTUAL_ROOT
            self.parent[head] = _VIRTUAL_ROOT
            self.children[_VIRTUAL_ROOT] = [tail, head]
        self.postorder = []
        for root in roots:
            stack = [(root, False)]
            while stack:
                nd, processed = stack.pop()
                if processed:
                    self.postorder.append(nd)
                    continue
                stack.append((nd, True))
                for neighbor in adjacency[nd]:
                    if neighbor is self.parent.get(nd):
                        continue
                    if neighbor in self.parent:
                        continue
                    self.parent[neighbor] = nd
                    self.children[nd].append(neighbor)
                    stack.append((neighbor, False))
        # taxon bit indices
        self.taxon_index = {}
        for nd in self.nodes:
            if nd.taxon is not None and nd.taxon not in self.taxon_index:
                self.taxon_index[nd.taxon] = len(self.taxon_index)
        # bit of each node's own taxon (0 if the node has no taxon)
        self.own_bit = {}
        for nd in self.nodes:
            if nd.taxon is not None:
                self.own_bit[nd] = 1 << self.taxon_index[nd.taxon]
            else:
                self.own_bit[nd] = 0
        self.own_bit[_VIRTUAL_ROOT] = 0
        # descendant-taxon bitsets
        self.bits = {}
        for nd in self.postorder:
            b = 0
            if nd.taxon is not None:
                b = 1 << self.taxon_index[nd.taxon]
            for ch in self.children[nd]:
                b |= self.bits[ch]
            self.bits[nd] = b
        self.all_bits = 0
        for b_index in self.taxon_index.values():
            self.all_bits |= 1 << b_index
        self.bits[_VIRTUAL_ROOT] = self.all_bits

    def oriented_children(self, nd):
        return self.children[nd]

    def outdegree(self, nd):
        return len(self.children[nd])


def _resolve_taxa(tree, taxa, constraint_description):
    "Resolves an iterable of |Taxon| objects or labels to |Taxon| objects."
    tns = tree.taxon_namespace
    resolved = []
    for t in taxa:
        if isinstance(t, str):
            taxon = tns.get_taxon(t)
            if taxon is None:
                raise ValueError(
                    "{}: taxon with label {!r} not found in the tree's"
                    " taxon namespace".format(constraint_description, t)
                )
            resolved.append(taxon)
        else:
            resolved.append(t)
    return resolved


def _normalize_constraints(tree, outgroups, ancestor_constraints):
    "Validates and normalizes the constraint specifications."
    tree_nodes = set(tree.preorder_node_iter())
    outgroup_constraints = []
    for i, og in enumerate(outgroups or ()):
        taxa = _resolve_taxa(tree, og, "Outgroup constraint {}".format(i))
        if not taxa:
            raise ValueError(
                "Outgroup constraint {}: empty taxon set".format(i)
            )
        outgroup_constraints.append(_OutgroupConstraint(taxa))
    ancestor_constraint_list = []
    for i, ac in enumerate(ancestor_constraints or ()):
        try:
            node, taxa = ac
        except (TypeError, ValueError):
            raise ValueError(
                "Ancestor constraint {}: expected a (node, taxa) pair,"
                " got {!r}".format(i, ac)
            )
        if node not in tree_nodes:
            raise ValueError(
                "Ancestor constraint {}: node {!r} is not in the tree".format(
                    i, getattr(node, "label", node)
                )
            )
        taxa = _resolve_taxa(tree, taxa, "Ancestor constraint {}".format(i))
        if not taxa:
            raise ValueError(
                "Ancestor constraint {}: empty taxon set".format(i)
            )
        ancestor_constraint_list.append(_AncestorConstraint(node, taxa))
    return outgroup_constraints, ancestor_constraint_list


def _compile_refinable_predicate(refinable_nodes):
    """
    Returns a predicate deciding whether a node may be refined (its
    polytomy resolved), as limited by the caller.
    """
    if refinable_nodes is None:
        return lambda nd: False
    if refinable_nodes == "all":
        return lambda nd: True
    if callable(refinable_nodes):
        return refinable_nodes
    allowed = set(refinable_nodes)
    return lambda nd: nd in allowed


def _evaluate_frame(
    frame,
    outgroup_constraints,
    ancestor_constraints,
    may_refine,
):
    """
    Evaluates all constraints on one oriented tree.

    Returns a tuple ``(is_certain, required_refinements, failures)`` where
    ``failures`` is a list of ``(kind, index)`` tuples identifying the
    constraints that cannot be satisfied at this placement (``kind`` is
    ``"outgroup"`` or ``"ancestor"``).
    """
    # Ancestor constraints: invariant under refinement of polytomies
    # (refinement never changes the leaf set descended from a
    # pre-existing node), so they decide feasibility outright.
    failures = []
    for j, ac in enumerate(ancestor_constraints):
        if frame.bits[ac.node] & ac.bits != ac.bits:
            failures.append(("ancestor", j))
    if failures:
        return False, (), failures

    required_refinements = set()
    groupings = {}  # node -> list of frozensets of child nodes
    any_uncertain = False
    for i, og in enumerate(outgroup_constraints):
        if og.bits == frame.all_bits:
            # the full taxon set is trivially monophyletic at the root
            continue
        # Is the outgroup a clade in the tree as given?
        is_clade = False
        for nd in frame.postorder:
            if frame.bits[nd] == og.bits:
                is_clade = True
                break
        if is_clade:
            continue
        any_uncertain = True
        # Can the outgroup be made a clade by refining allowed polytomies?
        feasible = True
        for nd in list(frame.postorder) + [_VIRTUAL_ROOT]:
            inter = frame.bits[nd] & og.bits
            if inter == 0 or inter == frame.bits[nd]:
                continue
            # partial intersection: the outgroup taxa below ``nd`` must
            # be gatherable into a single lineage
            if nd is not _VIRTUAL_ROOT and frame.own_bit[nd] & og.bits:
                # ``nd``'s own taxon is in the outgroup, but part of
                # ``nd``'s subtree is not: no clade can equal the
                # outgroup exactly
                feasible = False
                break
            intersecting_children = [
                ch
                for ch in frame.oriented_children(nd)
                if frame.bits[ch] & og.bits
            ]
            if len(intersecting_children) < 2:
                # a single child carries all of the outgroup below this
                # node: nothing to resolve here (handled at the child)
                continue
            if any(
                frame.bits[ch] & og.bits != frame.bits[ch]
                for ch in intersecting_children
            ):
                # a partially-intersecting child cannot be grouped as a
                # whole, and its outgroup part cannot be lifted out of
                # its subtree: the outgroup can never be a clade here
                feasible = False
                break
            if (
                nd is _VIRTUAL_ROOT
                or len(intersecting_children) >= frame.outdegree(nd)
                or frame.outdegree(nd) < 3
                or not may_refine(nd)
            ):
                feasible = False
                break
            required_refinements.add(nd)
            groupings.setdefault(nd, []).append(frozenset(intersecting_children))
        if not feasible:
            failures.append(("outgroup", i))
    if failures:
        return False, (), failures

    # Simultaneous feasibility: the groupings required at any single node
    # by the different outgroups must form a laminar (nested or disjoint)
    # family; crossing groupings cannot be realized by one refinement.
    for nd, nd_groupings in groupings.items():
        for x in range(len(nd_groupings)):
            for y in range(x + 1, len(nd_groupings)):
                g1 = nd_groupings[x]
                g2 = nd_groupings[y]
                if g1.isdisjoint(g2) or g1 <= g2 or g2 <= g1:
                    continue
                failures.append(("outgroup", None))
    if failures:
        return False, (), failures

    is_certain = not any_uncertain
    return is_certain, tuple(sorted(required_refinements, key=id)), []


def _candidate_positions(tree):
    """
    Enumerates all candidate root placements of the tree's undirected
    topology: every internal node and every edge.
    """
    positions = []
    seed = tree.seed_node
    for nd in tree.preorder_node_iter():
        if not nd.is_leaf():
            positions.append((RootPosition.NODE, nd))
        if nd is not seed:
            positions.append((RootPosition.EDGE, nd.edge))
    return positions


def find_root_positions(
    tree,
    outgroups=None,
    ancestor_constraints=None,
    refinable_nodes=None,
):
    """
    Finds all root placements of ``tree`` that satisfy the given
    constraints.

    The tree is treated as an unrooted topology and is never modified.
    Every placement that can satisfy the constraints is returned -- not
    just the placement implied by the first outgroup.

    Parameters
    ----------
    tree : |Tree|
        The tree of unknown rooting.  Its topology is treated as
        unrooted, regardless of its ``is_rooted`` flag.
    outgroups : iterable of iterables of (|Taxon| or str), optional
        The credible outgroups.  Each outgroup is a set of taxa (given
        as |Taxon| objects or labels) that is required to be
        monophyletic (form a clade) in the rooted tree.  All outgroup
        constraints must be satisfied simultaneously.
    ancestor_constraints : iterable of (|Node|, iterable of (|Taxon| or str))
        Partial ancestor constraints.  Each entry is a ``(node, taxa)``
        pair requiring ``node`` (a node of ``tree``) to be ancestral to
        every taxon in ``taxa`` in the rooted tree.
    refinable_nodes : optional
        Limits which polytomies may be resolved when assessing
        placements that are only feasible under refinement: ``None``
        (the default) allows no refinement; ``"all"`` allows any
        polytomy to be refined; a callable takes a node and returns
        whether it may be refined; any other value is treated as a
        container of the refinable nodes.

    Returns
    -------
    list of :class:`RootPosition`
        All valid placements.  Placements with
        :attr:`RootPosition.is_certain` equal to ``True`` are valid in
        the tree as given (necessarily feasible, i.e., valid under every
        allowed refinement); the remaining placements are only valid
        under some allowed refinements, with the nodes requiring
        resolution listed in :attr:`RootPosition.required_refinements`.

    Raises
    ------
    RootingConflictError
        If no placement satisfies all the constraints.
    """
    outgroup_constraints, ancestor_constraint_list = _normalize_constraints(
        tree, outgroups, ancestor_constraints
    )
    may_refine = _compile_refinable_predicate(refinable_nodes)

    # bit-encode constraint taxa
    taxon_index = {}
    for nd in tree.preorder_node_iter():
        if nd.taxon is not None and nd.taxon not in taxon_index:
            taxon_index[nd.taxon] = len(taxon_index)
    for og in outgroup_constraints:
        for t in og.taxa:
            if t not in taxon_index:
                raise ValueError(
                    "Outgroup taxon {!r} does not occur in the tree".format(
                        t.label
                    )
                )
            og.bits |= 1 << taxon_index[t]
    for ac in ancestor_constraint_list:
        for t in ac.taxa:
            if t not in taxon_index:
                raise ValueError(
                    "Ancestor-constraint taxon {!r} does not occur in the"
                    " tree".format(t.label)
                )
            ac.bits |= 1 << taxon_index[t]

    positions = []
    evaluated = 0
    outgroup_rejections = [0] * len(outgroup_constraints)
    ancestor_rejections = [0] * len(ancestor_constraint_list)
    laminar_rejections = 0
    for kind, target in _candidate_positions(tree):
        evaluated += 1
        frame = _RootingFrame(tree, kind, target)
        is_certain, required, failures = _evaluate_frame(
            frame,
            outgroup_constraints,
            ancestor_constraint_list,
            may_refine,
        )
        if failures:
            for failure_kind, index in failures:
                if failure_kind == "ancestor":
                    ancestor_rejections[index] += 1
                elif index is None:
                    laminar_rejections += 1
                else:
                    outgroup_rejections[index] += 1
            continue
        positions.append(
            RootPosition(
                tree=tree,
                kind=kind,
                target=target,
                is_certain=is_certain,
                required_refinements=required,
            )
        )
    if not positions:
        report = {
            "evaluated_positions": evaluated,
            "outgroup_rejections": outgroup_rejections,
            "ancestor_rejections": ancestor_rejections,
            "incompatible_refinement_rejections": laminar_rejections,
        }
        message_parts = [
            "No root placement satisfies all constraints",
            "{} candidate placements evaluated".format(evaluated),
        ]
        for i, og in enumerate(outgroup_constraints):
            message_parts.append(
                "{} excludes {} placement(s)".format(
                    og.describe(), outgroup_rejections[i]
                )
            )
        for j, ac in enumerate(ancestor_constraint_list):
            message_parts.append(
                "{} excludes {} placement(s)".format(
                    ac.describe(), ancestor_rejections[j]
                )
            )
        if laminar_rejections:
            message_parts.append(
                "{} placement(s) require mutually incompatible"
                " refinements".format(laminar_rejections)
            )
        raise RootingConflictError("; ".join(message_parts), report=report)
    return positions


def _resolve_split_lengths(total_length, length1, length2):
    """
    Resolves the daughter lengths of a split edge.

    Defaults to dividing the original length equally; if exactly one
    daughter length is given, the other receives the remainder, so the
    two always sum to the original edge length.
    """
    if length1 is not None and length2 is not None:
        return length1, length2
    if total_length is None:
        return length1, length2
    if length1 is None and length2 is None:
        half = total_length / 2.0
        return half, half
    if length1 is None:
        return total_length - length2, length2
    return length1, total_length - length1


def _connecting_edge(parent_node, child_node):
    """
    Returns the edge object of the original tree representing the
    undirected connection between two adjacent nodes.
    """
    if child_node._parent_node is parent_node:
        return child_node.edge
    return parent_node.edge


def _build_candidate(position, length1=None, length2=None):
    """
    Builds a rooted tree for a root placement, migrating branch lengths
    and annotations along the original edge identities.
    """
    source = position.tree
    frame = _RootingFrame(source, position.kind, position.target)
    new_nodes = {}
    node_map = {}
    edge_map = {}
    for nd in frame.nodes:
        new_nd = source.node_factory(label=nd.label)
        new_nd.taxon = nd.taxon
        new_nd.deep_copy_annotations_from(nd)
        new_nodes[nd] = new_nd
        node_map[new_nd] = nd
    # wire up the oriented structure, copying each original edge's
    # length and annotations onto the edge derived from it
    for nd in frame.nodes:
        new_nd = new_nodes[nd]
        for ch in frame.oriented_children(nd):
            new_ch = new_nodes[ch]
            new_nd.add_child(new_ch)
            original_edge = _connecting_edge(nd, ch)
            new_edge = new_ch.edge
            new_edge.length = original_edge.length
            new_edge.deep_copy_annotations_from(original_edge)
            edge_map[new_edge] = original_edge
    split_edges = None
    if position.kind == RootPosition.NODE:
        seed = new_nodes[position.target]
    else:
        original_edge = position.target
        tail = original_edge.tail_node
        head = original_edge.head_node
        l1, l2 = _resolve_split_lengths(original_edge.length, length1, length2)
        seed = source.node_factory()
        node_map[seed] = None
        # tail-side daughter edge: leads toward the original tail node
        seed.add_child(new_nodes[tail])
        tail_side_edge = new_nodes[tail].edge
        tail_side_edge.length = l1
        tail_side_edge.deep_copy_annotations_from(original_edge)
        edge_map[tail_side_edge] = original_edge
        # head-side daughter edge: leads toward the original head node
        seed.add_child(new_nodes[head])
        head_side_edge = new_nodes[head].edge
        head_side_edge.length = l2
        head_side_edge.deep_copy_annotations_from(original_edge)
        edge_map[head_side_edge] = original_edge
        split_edges = (tail_side_edge, head_side_edge)
    new_tree = Tree(
        seed_node=seed,
        taxon_namespace=source.taxon_namespace,
        is_rooted=True,
    )
    return RootedTreeCandidate(
        position=position,
        tree=new_tree,
        node_map=node_map,
        edge_map=edge_map,
        split_edges=split_edges,
    )


def generate_rooted_trees(
    tree,
    positions=None,
    outgroups=None,
    ancestor_constraints=None,
    refinable_nodes=None,
    length1=None,
    length2=None,
):
    """
    Generates candidate rooted trees for all valid root placements.

    Parameters
    ----------
    tree : |Tree|
        The tree of unknown rooting.  It is not modified and can always
        be re-read or reused.
    positions : iterable of :class:`RootPosition`, optional
        The placements to generate trees for.  If not given, the
        placements are computed with :func:`find_root_positions` using
        the remaining arguments.
    outgroups, ancestor_constraints, refinable_nodes
        Constraint and refinement-scope specifications, as described
        for :func:`find_root_positions`.  Ignored if ``positions`` is
        given.
    length1, length2 : numeric, optional
        The daughter lengths used when a placement splits an edge:
        ``length1`` goes to the daughter edge on the original tail-node
        side, ``length2`` to the one on the original head-node side.
        By default the original edge length is divided equally; if
        exactly one is given, the other receives the remainder, so the
        two always sum to the original edge length.

    Returns
    -------
    list of :class:`RootedTreeCandidate`
        One candidate per valid placement.  In each candidate tree,
        every edge carries the branch length and (independent copies
        of) the annotations of the original edge from which it derives;
        the ``edge_map`` attribute maps each new edge back to its
        original edge so the derivation -- including the root-edge
        splitting convention -- can be audited.
    """
    if positions is None:
        positions = find_root_positions(
            tree,
            outgroups=outgroups,
            ancestor_constraints=ancestor_constraints,
            refinable_nodes=refinable_nodes,
        )
    return [
        _build_candidate(position, length1=length1, length2=length2)
        for position in positions
    ]
