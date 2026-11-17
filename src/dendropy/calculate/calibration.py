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
##     Moreno, M. A., & Sukumaran, J. (2024). DendroPy 5: a mature
##     Python library for phylogenetic computing. Journal of Open
##     Source Software, 9(101), 6943, https://doi.org/10.21105/joss.06943
##
##############################################################################

"""
Joint time-calibration feasibility checking and provable node date ranges.

Given a rooted tree, sampling-date information for some (not necessarily all)
nodes, and duration ranges for its edges, this module decides whether a single
global time assignment exists that simultaneously satisfies *every* constraint.
When the system is feasible it also reports the tightest provable date interval
of every node (and duration interval of every edge).

The problem is a system of linear difference constraints.  For every pair of
variables (node times) a constraint of the form

.. math::

    T(u) - T(v) \\le w

is represented by a directed edge ``v -> u`` of weight ``w``.  A feasible
assignment exists if and only if the resulting constraint graph has no
negative-weight cycle, which is tested with the Bellman-Ford algorithm.  A
negative cycle is reported as the original chain of user-supplied constraints
that contradicts itself, which is particularly important for trees: every
individual root-to-leaf path can be internally consistent while the paths
jointly contradict each other (e.g. two zero-length terminal edges subtending
leaves sampled at different times).

Time is treated as forward time: a parent is never younger than its child
(``T(parent) >= T(child)``).  A zero-length edge is distinct from an
unknown-length edge: the former forces equality of the two endpoint times,
while the latter only imposes the parent-before-child ordering and contributes
no bound on the elapsed duration.
"""

import math

__all__ = [
    "CalibrationError",
    "Constraint",
    "EdgeDurationConstraint",
    "NodeDateConstraint",
    "CalibrationResult",
    "TreeCalibration",
    "check_time_calibration",
    "node_date_ranges",
]

_DEFAULT_TOLERANCE = 1e-12


class CalibrationError(ValueError):
    """
    Raised when no joint time assignment can satisfy all the calibration
    constraints.

    The offending closed chain of original constraints is available both from
    :attr:`contradictory_constraint_chain` (the sequence of
    :class:`Constraint` objects) and from the exception message.
    """

    def __init__(self, message, constraint_chain=None):
        super().__init__(message)
        if constraint_chain is None:
            constraint_chain = []
        self._constraint_chain = list(constraint_chain)

    @property
    def contradictory_constraint_chain(self):
        """
        Sequence of :class:`Constraint` objects forming the contradictory
        closed constraint chain (a negative cycle in the constraint graph).
        """
        return list(self._constraint_chain)

    def __str__(self):
        message = super().__str__()
        if not self._constraint_chain:
            return message
        lines = [message, "Contradictory constraint chain:"]
        for idx, constraint in enumerate(self._constraint_chain, 1):
            lines.append("  ({}) {}".format(idx, constraint.describe()))
        return "\n".join(lines)


class Constraint:
    """
    Base class for a single calibration constraint.

    Each constraint maps to one or two inequalities on node time variables.
    The ``bound_tag`` recorded on a graph edge identifies which side of the
    originating constraint is involved (``"lower"``, ``"upper"`` or
    ``"exact"``).
    """

    def describe(self):
        raise NotImplementedError

    def __repr__(self):
        return "{}({})".format(type(self).__name__, self.describe())


class EdgeDurationConstraint(Constraint):
    """
    Constrains the duration of the edge leading from ``parent_node`` to
    ``child_node`` to lie within ``[minimum, maximum]``.

    A constraint with ``is_unknown=True`` carries no duration bound at all:
    only the topological ordering ``T(parent) >= T(child)`` is imposed.  This
    is deliberately different from a zero minimum, which positively asserts
    that the edge may be instantaneous.
    """

    def __init__(
        self,
        edge,
        parent_node,
        child_node,
        minimum=None,
        maximum=None,
        is_unknown=False,
    ):
        self.edge = edge
        self.parent_node = parent_node
        self.child_node = child_node
        self.minimum = minimum
        self.maximum = maximum
        self.is_unknown = is_unknown

    def describe(self):
        endpoints = "{} -> {}".format(
            _node_name(self.parent_node),
            _node_name(self.child_node),
        )
        if self.is_unknown:
            return "edge duration [{}] unknown: T({}) >= T({}) only".format(
                endpoints,
                _node_name(self.parent_node),
                _node_name(self.child_node),
            )
        if self.minimum == self.maximum:
            return "edge [{}] duration exactly {}".format(
                endpoints, self.minimum
            )
        return "edge [{}] duration in [{}, {}]".format(
            endpoints, self.minimum, self.maximum
        )


class NodeDateConstraint(Constraint):
    """
    Constrains the time of ``node``.

    If ``reference_node`` is |None| the bounds are absolute (relative to the
    reporting time origin, shifted by ``present_time`` when results are
    reported).  Otherwise the bounds apply to the difference
    ``T(node) - T(reference_node)``.
    """

    def __init__(self, node, minimum=None, maximum=None, reference_node=None):
        self.node = node
        self.reference_node = reference_node
        self.minimum = minimum
        self.maximum = maximum

    def describe(self):
        target = _node_name(self.node)
        if self.reference_node is None:
            subject = "T({})".format(target)
        else:
            subject = "T({}) - T({})".format(
                target, _node_name(self.reference_node)
            )
        if self.minimum == self.maximum:
            return "{} exactly {}".format(subject, self.minimum)
        return "{} in [{}, {}]".format(subject, self.minimum, self.maximum)


class _GaugeConstraint(Constraint):
    """
    Internal anchor used only when no absolute node date is supplied: fixes
    the root at the reporting origin so that finite relative date ranges can
    be reported.  It never causes a contradiction on its own.
    """

    def __init__(self, node):
        self.node = node

    def describe(self):
        return "reporting gauge: T({}) fixed at time origin".format(
            _node_name(self.node)
        )


class CalibrationResult:
    """
    Outcome of a feasible calibration.

    Attributes
    ----------
    node_date_ranges : dict
        Mapping from each |Node| to ``(earliest_date, latest_date)``.  Open
        ranges use :data:`math.inf` (or ``-math.inf``).
    edge_duration_ranges : dict
        Mapping from each |Edge| (excluding a root edge, which has no parent
        variable) to ``(minimum_duration, maximum_duration)``.
    present_time : float
        Value added to the time-origin-relative solution when reporting node
        dates.
    """

    def __init__(
        self,
        tree,
        node_date_ranges,
        edge_duration_ranges,
        present_time,
        constraints,
    ):
        self.tree = tree
        self.node_date_ranges = dict(node_date_ranges)
        self.edge_duration_ranges = dict(edge_duration_ranges)
        self.present_time = present_time
        self.constraints = list(constraints)

    @property
    def is_feasible(self):
        return True

    def node_date_range(self, node):
        """Return ``(earliest_date, latest_date)`` for ``node``."""
        return self.node_date_ranges[node]

    def edge_duration_range(self, edge):
        """Return ``(minimum_duration, maximum_duration)`` for ``edge``."""
        return self.edge_duration_ranges[edge]

    def format_report(self, precision=6):
        """Return a human-readable report of all provable ranges."""
        lines = ["Joint time calibration is feasible."]
        lines.append("Node date ranges (present_time = {}):".format(self.present_time))
        for node in self.tree.preorder_node_iter():
            lower, upper = self.node_date_ranges[node]
            lines.append(
                "  {}: [{}, {}]".format(
                    _node_name(node),
                    _format_value(lower, precision),
                    _format_value(upper, precision),
                )
            )
        lines.append("Edge duration ranges:")
        for node in self.tree.preorder_node_iter():
            if node.parent_node is None:
                continue
            lower, upper = self.edge_duration_ranges[node.edge]
            lines.append(
                "  {} -> {}: [{}, {}]".format(
                    _node_name(node.parent_node),
                    _node_name(node),
                    _format_value(lower, precision),
                    _format_value(upper, precision),
                )
            )
        return "\n".join(lines)


class TreeCalibration:
    """
    Builds and solves the joint time-calibration constraint system for a
    single rooted |Tree|.

    Parameters
    ----------
    tree : |Tree|
        The rooted tree to calibrate.  Edge lengths already set on the tree
        are imported automatically: a numeric length is an exact duration,
        while ``None`` means an unknown duration (ordering constraint only).
    present_time : float, optional
        Absolute time assigned to the synthetic time origin when reporting
        dates.  Durations are unaffected.

    Examples
    --------
    Create a calibration from the tree's edge lengths, pin one leaf to an
    exact date and another to a date range, then read off the root range::

        calibration = TreeCalibration(tree)
        calibration.constrain_node_date(leaf_a, exact=10.0)
        calibration.constrain_node_date(leaf_b, minimum=4.0, maximum=6.0)
        result = calibration.solve()
        result.node_date_range(tree.seed_node)
    """

    def __init__(self, tree, present_time=0.0):
        if tree.seed_node is None:
            raise ValueError("Cannot calibrate an empty tree")
        self.tree = tree
        self.present_time = float(present_time)
        self._constraints = []
        self._tolerance = _DEFAULT_TOLERANCE
        self._import_tree_edge_lengths()

    def _import_tree_edge_lengths(self):
        for node in self.tree.postorder_node_iter():
            parent = node.parent_node
            if parent is None:
                # A root edge has no parent time variable to relate to.
                continue
            length = node.edge.length
            if length is None:
                self.constrain_edge_duration(
                    parent_node=parent, child_node=node
                )
            else:
                self.constrain_edge_duration(
                    parent_node=parent,
                    child_node=node,
                    exact=float(length),
                )

    @property
    def constraints(self):
        """All :class:`Constraint` objects registered so far."""
        return list(self._constraints)

    def constrain_node_date(
        self,
        node,
        minimum=None,
        maximum=None,
        exact=None,
        reference_node=None,
    ):
        """
        Add a constraint on the date of ``node``.

        Parameters
        ----------
        node : |Node| or str
            The constrained node, or its taxon/label string.
        minimum, maximum : float, optional
            Inclusive date bounds.
        exact : float, optional
            Convenience for an exact date; cannot be combined with
            ``minimum``/``maximum``.
        reference_node : |Node| or str, optional
            If given, the constraint applies to
            ``T(node) - T(reference_node)`` instead of an absolute date.
        """
        node = self._coerce_node(node)
        if reference_node is not None:
            reference_node = self._coerce_node(reference_node)
        minimum, maximum = _resolve_date_bounds(minimum, maximum, exact)
        constraint = NodeDateConstraint(
            node=node,
            minimum=minimum,
            maximum=maximum,
            reference_node=reference_node,
        )
        self._constraints.append(constraint)
        return constraint

    def constrain_edge_duration(
        self,
        edge=None,
        parent_node=None,
        child_node=None,
        minimum=None,
        maximum=None,
        exact=None,
    ):
        """
        Add a constraint on the duration of an edge.

        The edge is identified either by its |Edge| object or by its
        ``parent_node``/``child_node`` pair.  Calling with no bounds at all
        registers an unknown-duration edge, which contributes only the
        parent-before-child ordering constraint.
        """
        parent_node, child_node, resolved_edge = self._coerce_edge(
            edge=edge,
            parent_node=parent_node,
            child_node=child_node,
        )
        if minimum is None and maximum is None and exact is None:
            constraint = EdgeDurationConstraint(
                edge=resolved_edge,
                parent_node=parent_node,
                child_node=child_node,
                is_unknown=True,
            )
        else:
            minimum, maximum = _resolve_duration_bounds(minimum, maximum, exact)
            constraint = EdgeDurationConstraint(
                edge=resolved_edge,
                parent_node=parent_node,
                child_node=child_node,
                minimum=minimum,
                maximum=maximum,
            )
        self._constraints.append(constraint)
        return constraint

    def is_feasible(self):
        """Return |True| if a joint satisfying time assignment exists."""
        try:
            self.solve()
        except CalibrationError:
            return False
        return True

    def solve(self):
        """
        Solve the constraint system.

        Returns
        -------
        CalibrationResult
            Provable date and duration ranges.

        Raises
        ------
        CalibrationError
            If the constraints are jointly contradictory; the exception
            carries the original constraint chain responsible.
        """
        nodes = list(self.tree.postorder_node_iter())
        index = {node: idx for idx, node in enumerate(nodes)}
        reference_index = len(nodes)
        num_variables = reference_index + 1

        graph = []
        has_absolute_date = False
        has_relative_constraint = False
        # Tightest bounds asserted directly on each edge, keyed by the child
        # node index.  These are intersected with the bounds implied by the
        # joint system via the reference point below.
        direct_lower = {}
        direct_upper = {}
        for constraint in self._constraints:
            if isinstance(constraint, EdgeDurationConstraint):
                self._add_edge_constraint_edges(constraint, index, graph)
                if not constraint.is_unknown:
                    child_idx = index[constraint.child_node]
                    if child_idx not in direct_lower or (
                        constraint.minimum > direct_lower[child_idx]
                    ):
                        direct_lower[child_idx] = constraint.minimum
                    if child_idx not in direct_upper or (
                        constraint.maximum < direct_upper[child_idx]
                    ):
                        direct_upper[child_idx] = constraint.maximum
            elif isinstance(constraint, NodeDateConstraint):
                if constraint.reference_node is None:
                    has_absolute_date = True
                    ref_idx = reference_index
                    # Absolute dates share the reporting calendar: translate
                    # them into internal coordinates anchored at the origin
                    # (whose reported date is present_time).
                    shifted = NodeDateConstraint(
                        node=constraint.node,
                        minimum=constraint.minimum - self.present_time,
                        maximum=constraint.maximum - self.present_time,
                        reference_node=None,
                    )
                    self._add_node_constraint_edges(
                        shifted, index[constraint.node], ref_idx, graph
                    )
                else:
                    has_relative_constraint = True
                    ref_idx = index[constraint.reference_node]
                    self._add_node_constraint_edges(
                        constraint, index[constraint.node], ref_idx, graph
                    )

        if not has_absolute_date:
            gauge = _GaugeConstraint(self.tree.seed_node)
            root_idx = index[self.tree.seed_node]
            # T(root) == T(reference): both directions, weight zero.
            graph.append(
                (reference_index, root_idx, 0.0, gauge, "exact")
            )
            graph.append(
                (root_idx, reference_index, 0.0, gauge, "exact")
            )

        # Feasibility gate.  Initializing every distance to zero is
        # equivalent to adding a virtual super-source with a zero-weight edge
        # to every variable, which guarantees that negative cycles in
        # subgraphs unreachable from the reporting origin (e.g. a clade
        # linked solely by unknown-duration edges) are still detected.
        self._bellman_ford(
            source_index=reference_index,
            graph=graph,
            num_variables=num_variables,
            use_super_source=True,
        )
        if has_relative_constraint:
            # Relative date constraints add cross-edges ("chords") to the
            # tree graph, so exact endpoint bounds need all-pairs shortest
            # paths.
            shortest = self._floyd_warshall(
                graph=graph,
                num_variables=num_variables,
            )
            forward_dist = [
                shortest[reference_index][i] for i in range(num_variables)
            ]
            reverse_dist = [
                shortest[i][reference_index] for i in range(num_variables)
            ]
        else:
            forward_dist, _ = self._bellman_ford(
                source_index=reference_index,
                graph=graph,
                num_variables=num_variables,
            )
            reverse_graph = [
                (v, u, w, constraint, tag)
                for (u, v, w, constraint, tag) in graph
            ]
            reverse_dist, _ = self._bellman_ford(
                source_index=reference_index,
                graph=reverse_graph,
                num_variables=num_variables,
            )

        node_date_ranges = {}
        for node in nodes:
            idx = index[node]
            earliest = self.present_time - reverse_dist[idx]
            latest = self.present_time + forward_dist[idx]
            node_date_ranges[node] = (earliest, latest)

        edge_duration_ranges = {}
        for node in nodes:
            parent = node.parent_node
            if parent is None:
                continue
            parent_idx = index[parent]
            child_idx = index[node]
            if has_relative_constraint:
                # Relative date constraints add cross-edges ("chords") to the
                # tree graph, so the exact bound is the all-pairs shortest
                # path between the edge's endpoints.
                minimum_duration = max(
                    0.0, -shortest[child_idx][parent_idx]
                )
                maximum_duration = shortest[parent_idx][child_idx]
            else:
                # Bounds implied by paths routed through the reference point.
                minimum_via_reference = (
                    -reverse_dist[child_idx] - forward_dist[parent_idx]
                )
                maximum_via_reference = (
                    forward_dist[child_idx] + reverse_dist[parent_idx]
                )
                # On a tree every shortest path between an edge's endpoints is
                # either the direct constraint edge or a path through the
                # reference point, so the tightest bound is their intersection.
                # Zero is always included via the parent-before-child ordering.
                minimum_duration = max(
                    0.0,
                    minimum_via_reference,
                    direct_lower.get(child_idx, -math.inf),
                )
                maximum_duration = min(
                    maximum_via_reference, direct_upper.get(child_idx, math.inf)
                )
            edge_duration_ranges[node.edge] = (
                minimum_duration,
                maximum_duration,
            )

        return CalibrationResult(
            tree=self.tree,
            node_date_ranges=node_date_ranges,
            edge_duration_ranges=edge_duration_ranges,
            present_time=self.present_time,
            constraints=self._constraints,
        )

    def _add_edge_constraint_edges(self, constraint, index, graph):
        parent_idx = index[constraint.parent_node]
        child_idx = index[constraint.child_node]
        # Forward time: the parent is never later than the child, i.e.
        # T(parent) <= T(child), or T(parent) - T(child) <= 0,
        # represented by an edge child -> parent of weight 0.
        graph.append(
            (child_idx, parent_idx, 0.0, constraint, "ordering")
        )
        if constraint.is_unknown:
            return
        # Let d = T(child) - T(parent) >= 0.
        # d <= maximum => T(child) - T(parent) <= maximum
        if constraint.maximum != math.inf:
            graph.append(
                (parent_idx, child_idx, constraint.maximum, constraint, "upper")
            )
        # d >= minimum => T(parent) - T(child) <= -minimum
        if constraint.minimum != -math.inf:
            graph.append(
                (child_idx, parent_idx, -constraint.minimum, constraint, "lower")
            )

    def _add_node_constraint_edges(self, constraint, node_idx, ref_idx, graph):
        # x = T(node) - T(reference)
        # x <= maximum => T(node) - T(reference) <= maximum
        if constraint.maximum != math.inf:
            graph.append(
                (ref_idx, node_idx, constraint.maximum, constraint, "upper")
            )
        # x >= minimum => T(reference) - T(node) <= -minimum
        if constraint.minimum != -math.inf:
            graph.append(
                (node_idx, ref_idx, -constraint.minimum, constraint, "lower")
            )

    def _bellman_ford(self, source_index, graph, num_variables,
                      use_super_source=False):
        dist = [math.inf] * num_variables
        predecessor = [None] * num_variables
        if use_super_source:
            # Virtual super-source with a zero-weight edge to every variable.
            for i in range(num_variables):
                dist[i] = 0.0
        else:
            dist[source_index] = 0.0
        tolerance = self._tolerance
        for _ in range(num_variables - 1):
            updated = False
            for (u, v, weight, constraint, tag) in graph:
                if dist[u] == math.inf:
                    continue
                candidate = dist[u] + weight
                if candidate < dist[v] - tolerance:
                    dist[v] = candidate
                    predecessor[v] = (u, constraint, tag)
                    updated = True
            if not updated:
                break
        for (u, v, weight, constraint, tag) in graph:
            if dist[u] == math.inf:
                continue
            if dist[u] + weight < dist[v] - tolerance:
                chain = self._recover_negative_cycle(
                    entering_node=v,
                    predecessor=predecessor,
                    num_variables=num_variables,
                )
                raise CalibrationError(
                    "Joint time calibration is contradictory: "
                    "the constraints form an impossible closed chain",
                    constraint_chain=chain,
                )
        return dist, predecessor

    def _floyd_warshall(self, graph, num_variables):
        # All-pairs shortest paths; only called after Bellman-Ford has
        # established feasibility, so no negative cycle can occur here.
        dist = [
            [math.inf] * num_variables for _ in range(num_variables)
        ]
        for i in range(num_variables):
            dist[i][i] = 0.0
        for (u, v, weight, constraint, tag) in graph:
            if weight < dist[u][v]:
                dist[u][v] = weight
        for k in range(num_variables):
            row_k = dist[k]
            for i in range(num_variables):
                via_k = dist[i][k]
                if via_k == math.inf:
                    continue
                row_i = dist[i]
                for j in range(num_variables):
                    candidate = via_k + row_k[j]
                    if candidate < row_i[j]:
                        row_i[j] = candidate
        return dist

    @staticmethod
    def _recover_negative_cycle(entering_node, predecessor, num_variables):
        # A node relaxed in the extra round is reachable from a negative
        # cycle; walking back num_variables steps guarantees landing on it.
        cycle_node = entering_node
        for _ in range(num_variables):
            previous = predecessor[cycle_node]
            if previous is None:
                break
            cycle_node = previous[0]
        chain = []
        current = cycle_node
        seen = set()
        while current not in seen:
            seen.add(current)
            previous = predecessor[current]
            if previous is None:
                break
            parent_idx, constraint, tag = previous
            chain.append(constraint)
            current = parent_idx
        chain.reverse()
        # De-duplicate while preserving order (exact constraints appear on
        # both sides of the same cycle).
        unique_chain = []
        seen_constraints = set()
        for constraint in chain:
            if id(constraint) not in seen_constraints:
                seen_constraints.add(id(constraint))
                unique_chain.append(constraint)
        return unique_chain

    def _coerce_node(self, node):
        if hasattr(node, "parent_node"):
            return node
        if not isinstance(node, str):
            raise TypeError(
                "Node reference must be a Node or a label string, "
                "not {}".format(repr(node))
            )
        found = self.tree.find_node(
            lambda nd: (nd.taxon is not None and nd.taxon.label == node)
            or nd.label == node
        )
        if found is None:
            raise KeyError("No node found with label or taxon '{}'".format(node))
        return found

    def _coerce_edge(self, edge, parent_node, child_node):
        if edge is not None:
            child_node = edge.head_node
            parent_node = child_node.parent_node
            if parent_node is None:
                raise ValueError(
                    "The root edge has no parent time variable and cannot "
                    "be duration-constrained"
                )
            return parent_node, child_node, edge
        if child_node is None:
            raise ValueError(
                "An edge must be specified by 'edge' or by 'child_node'"
            )
        child_node = self._coerce_node(child_node)
        parent_node = self._coerce_node(parent_node) if parent_node is not None else None
        if parent_node is None:
            parent_node = child_node.parent_node
        if parent_node is None or child_node.parent_node is not parent_node:
            raise ValueError(
                "Node '{}' is not a child of '{}'".format(
                    _node_name(child_node), _node_name(parent_node)
                )
            )
        return parent_node, child_node, child_node.edge


def check_time_calibration(
    tree,
    node_date_constraints=None,
    present_time=0.0,
):
    """
    Convenience wrapper: import the edge lengths already stored on ``tree``
    and add the supplied node dates, then solve the joint system.

    Parameters
    ----------
    tree : |Tree|
        Tree whose edge lengths provide the edge duration constraints
        (``None`` length = unknown duration).
    node_date_constraints : dict, optional
        Mapping of node (or label string) to either an exact numeric date or
        an ``(minimum, maximum)`` pair.
    present_time : float, optional
        Time-origin shift for reported dates.

    Returns
    -------
    CalibrationResult
        Provable date and duration ranges.

    Raises
    ------
    CalibrationError
        If no joint time assignment exists; the contradictory original
        constraint chain is attached to the exception.
    """
    calibration = TreeCalibration(tree, present_time=present_time)
    if node_date_constraints:
        for node, spec in node_date_constraints.items():
            if isinstance(spec, (tuple, list)):
                minimum, maximum = spec
                calibration.constrain_node_date(
                    node, minimum=minimum, maximum=maximum
                )
            else:
                calibration.constrain_node_date(node, exact=spec)
    return calibration.solve()


def node_date_ranges(tree, node_date_constraints=None, present_time=0.0):
    """
    Return a mapping of every node to its provable ``(earliest, latest)``
    date range, or raise :class:`CalibrationError` if the constraints are
    jointly contradictory.
    """
    result = check_time_calibration(
        tree=tree,
        node_date_constraints=node_date_constraints,
        present_time=present_time,
    )
    return result.node_date_ranges


def _resolve_date_bounds(minimum, maximum, exact):
    if exact is not None:
        if minimum is not None or maximum is not None:
            raise ValueError(
                "'exact' cannot be combined with 'minimum'/'maximum'"
            )
        minimum = maximum = exact
    if minimum is None:
        minimum = -math.inf
    if maximum is None:
        maximum = math.inf
    if minimum > maximum:
        raise ValueError(
            "Lower bound {} exceeds upper bound {}".format(minimum, maximum)
        )
    return _as_float(minimum), _as_float(maximum)


def _resolve_duration_bounds(minimum, maximum, exact):
    if exact is not None:
        if minimum is not None or maximum is not None:
            raise ValueError(
                "'exact' cannot be combined with 'minimum'/'maximum'"
            )
        minimum = maximum = exact
    if minimum is None:
        minimum = 0.0
    if maximum is None:
        maximum = math.inf
    if minimum < 0:
        raise ValueError(
            "Edge durations must be non-negative, got minimum {}".format(minimum)
        )
    if minimum > maximum:
        raise ValueError(
            "Minimum duration {} exceeds maximum duration {}".format(
                minimum, maximum
            )
        )
    return _as_float(minimum), _as_float(maximum)


def _as_float(value):
    if value == math.inf or value == -math.inf:
        return value
    return float(value)


def _node_name(node):
    if node is None:
        return "<none>"
    if node.taxon is not None and getattr(node.taxon, "label", None):
        return node.taxon.label
    if node.label:
        return node.label
    return "node#{}".format(id(node))


def _format_value(value, precision):
    if value == math.inf:
        return "+inf"
    if value == -math.inf:
        return "-inf"
    return "{:.{p}f}".format(value, p=precision).rstrip("0").rstrip(".")
