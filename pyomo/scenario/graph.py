# ____________________________________________________________________________________
#
# Pyomo: Python Optimization Modeling Objects
# Copyright (c) 2008-2026 National Technology and Engineering Solutions of Sandia, LLC
# Under the terms of Contract DE-NA0003525 with National Technology and Engineering
# Solutions of Sandia, LLC, the U.S. Government retains certain rights in this
# software.  This software is distributed under the 3-clause BSD License.
# ____________________________________________________________________________________
"""Scenario inheritance graphs and per-index data resolution.

Resolution is deliberately *not* "last file wins".  For every symbol and
index, all declarations from the reachable layers are collected, grouped by
semantic precedence, and either the unique winning group is selected or an
explicit conflict/cycle error is raised.  Each winning value carries its
provenance (layer name, layer revision, and whether it was declared,
defaulted, or retracted).
"""

from __future__ import annotations

import copy
import numbers

from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Mapping

from pyomo.scenario.data import DataLayer, RETRACT, content_fingerprint
from pyomo.scenario.errors import (
    ScenarioConflictError,
    ScenarioCycleError,
    ScenarioError,
)

__all__ = [
    'NEVER_DECLARED',
    'RETRACTED',
    'DECLARED',
    'DEFAULTED',
    'CONFLICTED',
    'CYCLE',
    'CONFLICT',
    'Provenance',
    'ResolvedSymbol',
    'ResolvedScenario',
    'ScenarioGraph',
]


NEVER_DECLARED = 'never_declared'
RETRACTED = 'retracted'
DECLARED = 'declared'
DEFAULTED = 'defaulted'
CONFLICTED = 'conflict'
CYCLE = 'cycle'
CONFLICT = 'conflict'


@dataclass(frozen=True)
class Provenance:
    """Where one resolved value came from.

    Attributes
    ----------
    layer:
        The layer that provided the winning declaration.
    layer_version:
        The layer revision at resolution time.
    status:
        One of ``"declared"``, ``"defaulted"``, or ``"retracted"``.
    distance:
        Inheritance distance from the scenario.  ``0`` is the scenario's
        own layer, ``1`` is a direct parent, and so on.
    priority:
        The layer's explicit priority (or ``None``).
    """

    layer: str
    layer_version: int
    status: str
    distance: int
    priority: object = None

    def as_dict(self):
        return {
            'layer': self.layer,
            'layer_version': self.layer_version,
            'status': self.status,
            'distance': self.distance,
            'priority': self.priority,
        }

    def __str__(self):
        return (
            f'{self.layer}@{self.layer_version} '
            f'({self.status}, distance={self.distance})'
        )


@dataclass(frozen=True)
class ResolvedSymbol:
    """Resolution result for one symbol."""

    name: str
    kind: str
    value: object = None
    provenance: object = None
    status: str = NEVER_DECLARED
    sources: Mapping = field(default_factory=dict)
    candidates: tuple = ()

    @property
    def is_retracted(self):
        return self.status == RETRACTED

    @property
    def is_conflict(self):
        return self.status == CONFLICTED

    @property
    def is_declared(self):
        return self.status == DECLARED

    @property
    def is_defaulted(self):
        return self.status == DEFAULTED

    def source_for(self, index):
        """Return the :class:`Provenance` for *index* (or ``None``)."""
        return self.sources.get(index)

    def status_for(self, index):
        source = self.sources.get(index)
        if source is None:
            return NEVER_DECLARED
        return source.status

    @property
    def retracted_indices(self):
        return tuple(
            index
            for index, source in self.sources.items()
            if source.status == RETRACTED
        )


@dataclass(frozen=True)
class ResolvedScenario:
    """The complete model input for one scenario plus its provenance."""

    name: str
    data: Mapping
    provenance: Mapping
    order: tuple
    fingerprint: str
    layers: tuple = ()

    def __getitem__(self, symbol):
        return self.data[symbol]

    def symbol(self, name):
        """Return the :class:`ResolvedSymbol` for *name*."""
        return self.provenance.get(name, ResolvedSymbol(name=name, kind='param'))

    def source(self, symbol, index=None):
        """Return provenance for a whole symbol or one index."""
        resolved = self.symbol(symbol)
        if index is not None:
            return resolved.source_for(index)
        if resolved.provenance is not None:
            return resolved.provenance
        if len(resolved.sources) == 1:
            return next(iter(resolved.sources.values()))
        return dict(resolved.sources)

    @property
    def defaults(self):
        return {
            name: resolved.value
            for name, resolved in self.provenance.items()
            if resolved.is_defaulted
        }

    @property
    def retracted(self):
        """Symbols whose whole declaration is retracted."""
        return tuple(
            name
            for name, resolved in self.provenance.items()
            if resolved.status == RETRACTED
        )

    def retracted_indices(self, symbol):
        """Indices of *symbol* explicitly retracted in this scenario."""
        resolved = self.symbol(symbol)
        return tuple(
            index
            for index in resolved.sources
            if resolved.sources[index].status == RETRACTED
        )

    def as_dict(self):
        return {
            'name': self.name,
            'data': copy.deepcopy(dict(self.data)),
            'provenance': {
                name: {
                    'kind': resolved.kind,
                    'status': resolved.status,
                    'sources': {
                        index: source.as_dict()
                        for index, source in resolved.sources.items()
                    },
                }
                for name, resolved in self.provenance.items()
            },
        }

    def model_input(self):
        """Return the model input as a plain nested dictionary.

        The result is wrapped in the default namespace, matching the
        ``{None: {symbol: data}}`` structure accepted by
        :meth:`AbstractModel.create_instance`.
        """
        return {None: copy.deepcopy(dict(self.data))}


@dataclass(frozen=True)
class _Declaration:
    symbol: str
    index: object
    value: object
    layer: str
    version: int
    priority: object
    distance: int
    precedence: tuple
    order: int
    is_retract: bool = False

    @property
    def provenance(self):
        return Provenance(
            layer=self.layer,
            layer_version=self.version,
            status=RETRACTED if self.is_retract else DECLARED,
            distance=self.distance,
            priority=self.priority,
        )


class _Node:
    __slots__ = ('name', 'layer', 'parents')

    def __init__(self, name, layer, parents):
        self.name = name
        self.layer = layer
        self.parents = parents

    @property
    def priority(self):
        return self.layer.priority

    def __repr__(self):
        return f'_Node({self.name!r})'


@dataclass(frozen=True)
class _LayerView:
    name: str
    snapshot: object
    distance: int
    precedence: tuple
    order: int


class ScenarioGraph:
    """A directed acyclic graph of scenario layers.

    Each node owns a :class:`DataLayer`.  Parents are more general; a node
    inherits every parent's effective data, then applies its own layer.
    Multiple inheritance is supported.  Edges may be added in any order;
    cycles are reported when the graph is resolved and never silently
    broken by insertion order.

    Parameters
    ----------
    layers:
        An optional mapping ``name -> DataLayer`` or iterable of layers.
    """

    def __init__(self, layers=None):
        self._layers = OrderedDict()
        self._parents = OrderedDict()
        self._children = OrderedDict()
        self._cache = {}
        if layers is not None:
            if isinstance(layers, Mapping):
                items = layers.items()
            else:
                items = ((layer.name, layer) for layer in layers)
            for name, layer in items:
                self.add_layer(name, layer)

    # ------------------------------------------------------------------
    # Graph construction
    # ------------------------------------------------------------------
    def add_layer(self, name, layer=None, data=None, parents=(), **kwargs):
        """Add a layer under *name*.

        If *layer* is omitted, a new :class:`DataLayer` is created with the
        remaining keyword arguments.  Adding an existing name replaces the
        layer but preserves its parent edges.
        """
        if layer is None:
            layer = DataLayer(name=name, data=data, **kwargs)
        if not isinstance(layer, DataLayer):
            raise TypeError('Scenario layers must be DataLayer instances')
        self._layers[name] = layer
        self._parents.setdefault(name, [])
        self._children.setdefault(name, [])
        if parents:
            self.set_parents(name, parents)
        return layer

    def __setitem__(self, name, layer):
        self.add_layer(name, layer)

    def __getitem__(self, name):
        return self._layers[name]

    def __contains__(self, name):
        return name in self._layers

    def __iter__(self):
        return iter(self._layers)

    def __len__(self):
        return len(self._layers)

    def __repr__(self):
        return f'ScenarioGraph({list(self._layers)!r})'

    @property
    def nodes(self):
        return tuple(self._layers)

    def layer(self, name):
        return self._layers[name]

    def parents(self, name):
        self._require(name)
        return tuple(self._parents[name])

    def children(self, name):
        self._require(name)
        return tuple(self._children[name])

    def set_parents(self, name, parents):
        """Replace the parent set for *name*."""
        self._require(name)
        if isinstance(parents, str):
            parents = [parents]
        new_parents = []
        for parent in parents:
            if parent == name:
                raise ScenarioCycleError(
                    f'Scenario {name!r} cannot inherit from itself'
                )
            self._require(parent)
            if parent not in new_parents:
                new_parents.append(parent)
        for parent in self._parents[name]:
            self._children[parent].remove(name)
        self._parents[name] = new_parents
        for parent in new_parents:
            self._children[parent].append(name)

    def add_edge(self, child, parent):
        """Declare that *child* inherits from *parent*."""
        self._require(child)
        self._require(parent)
        if parent not in self._parents[child]:
            self._parents[child].append(parent)
            self._children[parent].append(child)

    def remove_edge(self, child, parent):
        self._require(child)
        self._require(parent)
        if parent in self._parents[child]:
            self._parents[child].remove(parent)
            self._children[parent].remove(child)

    def _require(self, name):
        if name not in self._layers:
            raise ScenarioError(f'Unknown scenario layer {name!r}')

    def _order(self, name):
        for i, key in enumerate(self._layers):
            if key == name:
                return i
        raise ScenarioError(f'Unknown scenario layer {name!r}')

    # ------------------------------------------------------------------
    # Traversal
    # ------------------------------------------------------------------
    def ancestors(self, name):
        """Return all ancestors of *name* in deterministic order.

        Ordering is breadth-first by inheritance distance, then insertion
        order.  This ordering is used only for presentation; semantic
        precedence never uses insertion order to break ties.
        """
        self._require(name)
        seen = {}
        queue = deque((parent, 1) for parent in self._parents[name])
        while queue:
            parent, distance = queue.popleft()
            if parent == name:
                continue
            if parent in seen and seen[parent] <= distance:
                continue
            seen[parent] = distance
            for grandparent in self._parents[parent]:
                queue.append((grandparent, distance + 1))
        return [
            ancestor
            for ancestor, _ in sorted(
                seen.items(), key=lambda item: (item[1], self._order(item[0]))
            )
        ]

    def _distance(self, child, ancestor):
        if child == ancestor:
            return 0
        queue = deque((parent, 1) for parent in self._parents[child])
        seen = set()
        while queue:
            node, distance = queue.popleft()
            if node == ancestor:
                return distance
            if node in seen:
                continue
            seen.add(node)
            for parent in self._parents[node]:
                queue.append((parent, distance + 1))
        return None

    def lineage(self, name):
        """Return this node and its ancestors, nearest first."""
        self._require(name)
        return [name] + self.ancestors(name)

    def topological_order(self):
        """Return nodes in dependency order (parents before children)."""
        indegree = {name: len(parents) for name, parents in self._parents.items()}
        queue = deque(name for name in self._layers if indegree[name] == 0)
        result = []
        while queue:
            name = queue.popleft()
            result.append(name)
            for child in self._children[name]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        if len(result) != len(self._layers):
            cyclic = [name for name in self._layers if name not in result]
            raise ScenarioCycleError(
                'Scenario inheritance graph contains a cycle involving: '
                + ', '.join(cyclic)
            )
        return result

    def find_cycles(self):
        """Return detected cycles (each as a tuple of scenario names)."""
        color = {name: 0 for name in self._layers}
        stack = []
        cycles = []
        seen_cycles = set()

        def visit(node):
            color[node] = 1
            stack.append(node)
            for parent in self._parents[node]:
                if color[parent] == 0:
                    visit(parent)
                elif color[parent] == 1:
                    start = stack.index(parent)
                    cycle = tuple(stack[start:])
                    key = frozenset(cycle)
                    if key not in seen_cycles:
                        seen_cycles.add(key)
                        cycles.append(cycle)
            stack.pop()
            color[node] = 2

        for name in self._layers:
            if color[name] == 0:
                visit(name)
        return cycles

    def _reachable_cycles(self, scenario):
        """Return cycles reachable from *scenario* (never from siblings)."""
        reachable = set(self.lineage(scenario))
        color = {name: 0 for name in reachable}
        stack = []
        cycles = []
        seen_cycles = set()

        def visit(node):
            color[node] = 1
            stack.append(node)
            for parent in self._parents[node]:
                if parent not in reachable:
                    continue
                if color[parent] == 0:
                    visit(parent)
                elif color[parent] == 1:
                    start = stack.index(parent)
                    cycle = tuple(stack[start:])
                    key = frozenset(cycle)
                    if key not in seen_cycles:
                        seen_cycles.add(key)
                        cycles.append(cycle)
            stack.pop()
            color[node] = 2

        visit(scenario)
        return cycles

    # ------------------------------------------------------------------
    # Precedence
    # ------------------------------------------------------------------
    @staticmethod
    def _priority_key(priority):
        if isinstance(priority, numbers.Number):
            return (0, -priority)
        return (1, repr(priority))

    def _precedence(self, name, distance):
        """Semantic precedence (smaller wins); deliberately excludes order."""
        layer = self._layers[name]
        if layer.priority is None:
            return (1, 0, distance)
        return (0,) + self._priority_key(layer.priority)

    def _layer_views(self, scenario):
        lineage = self.lineage(scenario)
        views = []
        for position, name in enumerate(lineage):
            views.append(
                _LayerView(
                    name=name,
                    snapshot=self._layers[name].snapshot(),
                    distance=self._distance(scenario, name),
                    precedence=self._precedence(name, self._distance(scenario, name)),
                    order=position,
                )
            )
        # Lowest precedence first, so later (higher precedence) layers
        # override.  `order` only makes the walk deterministic.
        return sorted(
            views,
            key=lambda view: (view.precedence, view.order),
            reverse=True,
        )

    @staticmethod
    def _best(candidates):
        best_precedence = min(candidate.precedence for candidate in candidates)
        best = [
            candidate
            for candidate in candidates
            if candidate.precedence == best_precedence
        ]
        # Deterministic only for equivalent choices; never used to hide a
        # semantic conflict (conflicts are checked before this ordering).
        return sorted(best, key=lambda candidate: (candidate.layer, candidate.order))

    @staticmethod
    def _assert_unique(best, message):
        layers = {candidate.layer for candidate in best}
        if len(layers) <= 1:
            return
        if all(candidate.is_retract for candidate in best):
            # Multiple layers retiring the same thing is redundant.
            return
        if len({repr(candidate.value) for candidate in best}) == 1:
            # Independent layers agreeing on the exact same value is not an
            # ambiguity.
            return
        raise ScenarioConflictError(message + ': ' + ', '.join(sorted(layers)))

    def effective_indices(self, scenario, symbol):
        """Return the effective index membership for *symbol* in *scenario*.

        The result is computed from the scenario's own reachable layers; it
        never borrows members from an unrelated scenario.
        """
        self._require(scenario)
        views = self._layer_views(scenario)
        resolved = self.resolve(scenario, include_defaults=False).data
        return tuple(self._effective_indices(symbol, views, resolved))

    def check_indices(self, scenario, strict=False):
        """Validate that declared data uses the parameter's effective indices.

        Parameters
        ----------
        strict:
            If False (default), parameters without an explicit index set are
            not checked (their index set is implicit).  If True, a parameter
            whose explicit index set exists must not contain data outside it.
        """
        resolved = self.resolve(scenario)
        views = self._layer_views(scenario)
        problems = []
        for symbol, payload in resolved.data.items():
            meta = resolved.symbol(symbol)
            if meta.kind != 'param' or not isinstance(payload, Mapping):
                continue
            indices = self._effective_indices(symbol, views, resolved.data)
            if not indices:
                continue
            explicit = any(
                symbol in view.snapshot.indices
                or symbol in getattr(view.snapshot, 'index_sets', {})
                for view in views
            )
            if not explicit and not strict:
                continue
            for index in payload:
                if index is None:
                    continue
                if index not in indices:
                    problems.append((symbol, index))
        return problems

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------
    def resolve(self, scenario, include_defaults=True):
        """Resolve *scenario* into a :class:`ResolvedScenario`.

        The complete model input uses the conventional
        ``symbol -> {index: value}`` structure accepted by
        :meth:`AbstractModel.create_instance`.  Scalar parameters use the
        ``{None: value}`` convention; sets use ``{None: [members]}``.
        Retracted symbols are omitted from ``data`` but retained in
        ``provenance`` with status ``"retracted"``.
        """
        self._require(scenario)
        # Only cycles reachable from this scenario make *this* resolution
        # invalid; cycles in unrelated branches must not poison it.
        cycles = self._reachable_cycles(scenario)
        if cycles:
            cycle = cycles[0]
            raise ScenarioCycleError(
                'Scenario inheritance graph contains a cycle: '
                + ' -> '.join(tuple(cycle) + (cycle[0],))
            )
        views = self._layer_views(scenario)
        kinds = self._collect_kinds(views, scenario)
        resolved = OrderedDict()
        provenance = OrderedDict()

        for symbol, kind in kinds.items():
            if kind == 'set':
                self._resolve_set(symbol, views, scenario, resolved, provenance)
            else:
                self._resolve_param(symbol, views, scenario, resolved, provenance)

        if include_defaults:
            self._apply_defaults(views, scenario, resolved, provenance, kinds)

        fingerprint = self._fingerprint(resolved, provenance)
        return ResolvedScenario(
            name=scenario,
            data=resolved,
            provenance=provenance,
            order=tuple(provenance),
            fingerprint=fingerprint,
            layers=tuple(view.name for view in reversed(views)),
        )

    @staticmethod
    def _collect_kinds(views, scenario):
        kinds = OrderedDict()
        unknown = []
        # Lowest precedence first, so a higher-precedence declaration wins.
        for view in reversed(views):
            snapshot = view.snapshot
            for symbol, kind in snapshot.kinds.items():
                previous = kinds.get(symbol)
                if previous is not None and previous != kind:
                    raise ScenarioError(
                        f'Symbol {symbol!r} is declared as both {previous!r} '
                        f'and {kind!r} in the inheritance of scenario '
                        f'{scenario!r}'
                    )
                kinds[symbol] = kind
            for symbol in set(snapshot.symbol_retracted) | set(snapshot.retractions):
                if symbol not in kinds and symbol not in unknown:
                    unknown.append(symbol)
        # A symbol only ever retracted has no declared structure.  It is
        # treated as an (indexed or scalar) parameter, which is how Pyomo's
        # data commands express "retract this declaration".
        for symbol in unknown:
            kinds.setdefault(symbol, 'param')
        return kinds
        return kinds

    # ------------------------------------------------------------------
    # Parameters
    # ------------------------------------------------------------------
    def _param_candidates(self, symbol, views):
        """Collect all parameter declarations for *symbol* by index."""
        indices = set()
        values = {}
        retractions = {}
        whole_retractions = []
        order = 0
        for view in views:
            snapshot = view.snapshot
            for index in snapshot.values.get(symbol, {}):
                indices.add(index)
            for index in snapshot.indices.get(symbol, ()):
                indices.add(index)
            if symbol in snapshot.retractions:
                for index in snapshot.retractions[symbol]:
                    indices.add(index)
            if symbol in snapshot.symbol_retracted:
                whole_retractions.append(
                    _Declaration(
                        symbol=symbol,
                        index=RETRACT,
                        value=RETRACT,
                        layer=view.name,
                        version=snapshot.version,
                        priority=snapshot.priority,
                        distance=view.distance,
                        precedence=view.precedence,
                        order=order,
                        is_retract=True,
                    )
                )
                order += 1
        # A whole-symbol retraction competes with every index's declaration.
        for index in indices:
            if whole_retractions:
                values[index] = []
        for view in views:
            snapshot = view.snapshot
            for index, value in snapshot.values.get(symbol, {}).items():
                values.setdefault(index, []).append(
                    _Declaration(
                        symbol=symbol,
                        index=index,
                        value=copy.deepcopy(value),
                        layer=view.name,
                        version=snapshot.version,
                        priority=snapshot.priority,
                        distance=view.distance,
                        precedence=view.precedence,
                        order=order,
                    )
                )
                order += 1
            for index in snapshot.retractions.get(symbol, ()):
                retractions.setdefault(index, []).append(
                    _Declaration(
                        symbol=symbol,
                        index=index,
                        value=RETRACT,
                        layer=view.name,
                        version=snapshot.version,
                        priority=snapshot.priority,
                        distance=view.distance,
                        precedence=view.precedence,
                        order=order,
                        is_retract=True,
                    )
                )
                order += 1
        for index, entries in retractions.items():
            values.setdefault(index, []).extend(entries)
        for index, entries in values.items():
            entries.extend(copy.copy(item) for item in whole_retractions)
        return values, whole_retractions

    def _resolve_param(self, symbol, views, scenario, resolved, provenance):
        declarations, whole_retractions = self._param_candidates(symbol, views)
        values = OrderedDict()
        sources = {}
        candidates = []
        any_retracted = False
        for index, entries in declarations.items():
            candidates.extend(entries)
            best = self._best(entries)
            self._assert_unique(
                best,
                f'Conflicting same-priority declarations for parameter '
                f'{symbol!r}[{index!r}] in scenario {scenario!r}',
            )
            winner = best[0]
            if winner.is_retract:
                sources[index] = winner.provenance
                any_retracted = True
            else:
                values[index] = winner.value
                sources[index] = winner.provenance
        if not declarations and whole_retractions:
            best = self._best(whole_retractions)
            self._assert_unique(
                best,
                f'Conflicting same-priority retractions for symbol '
                f'{symbol!r} in scenario {scenario!r}',
            )
            provenance[symbol] = ResolvedSymbol(
                name=symbol,
                kind='param',
                status=RETRACTED,
                provenance=best[0].provenance,
                candidates=tuple(best),
            )
            return
        if not values:
            status = RETRACTED if any_retracted else NEVER_DECLARED
            provenance[symbol] = ResolvedSymbol(
                name=symbol,
                kind='param',
                status=status,
                sources=sources,
                candidates=tuple(candidates),
            )
            return
        payload = {None: values[None]} if None in values else values
        resolved[symbol] = payload
        provenance[symbol] = ResolvedSymbol(
            name=symbol,
            kind='param',
            value=payload,
            status=DECLARED,
            sources=sources,
            candidates=tuple(candidates),
        )

    # ------------------------------------------------------------------
    # Sets
    # ------------------------------------------------------------------
    def _set_candidates(self, symbol, views):
        layers = []
        members = set()
        retractions = {}
        whole_retractions = []
        declarations = []
        order = 0
        for view in views:
            snapshot = view.snapshot
            if symbol in snapshot.sets:
                mode = snapshot.set_modes.get(symbol, 'replace')
                declarations.append((view, mode, tuple(snapshot.sets[symbol])))
                members.update(snapshot.sets[symbol])
                order += 1
            for member in snapshot.retractions.get(symbol, ()):
                members.add(member)
                retractions.setdefault(member, []).append(
                    _Declaration(
                        symbol=symbol,
                        index=member,
                        value=RETRACT,
                        layer=view.name,
                        version=snapshot.version,
                        priority=snapshot.priority,
                        distance=view.distance,
                        precedence=view.precedence,
                        order=order,
                        is_retract=True,
                    )
                )
                order += 1
            if symbol in snapshot.symbol_retracted:
                whole_retractions.append(
                    _Declaration(
                        symbol=symbol,
                        index=RETRACT,
                        value=RETRACT,
                        layer=view.name,
                        version=snapshot.version,
                        priority=snapshot.priority,
                        distance=view.distance,
                        precedence=view.precedence,
                        order=order,
                        is_retract=True,
                    )
                )
                order += 1
        return declarations, members, retractions, whole_retractions

    def _resolve_set(self, symbol, views, scenario, resolved, provenance):
        declarations, members, retractions, whole_retractions = self._set_candidates(
            symbol, views
        )
        if not declarations and not whole_retractions:
            # only member retractions of a symbol nobody declared
            if retractions:
                sources = {}
                for member, entries in retractions.items():
                    best = self._best(entries)
                    self._assert_unique(
                        best,
                        f'Conflicting same-priority retractions for set '
                        f'member {symbol!r}[{member!r}] in scenario '
                        f'{scenario!r}',
                    )
                    sources[member] = best[0].provenance
                provenance[symbol] = ResolvedSymbol(
                    name=symbol,
                    kind='set',
                    status=RETRACTED,
                    sources=sources,
                    candidates=tuple(
                        entry for entries in retractions.values() for entry in entries
                    ),
                )
            return

        # Detect same-precedence ambiguity among declarations before any
        # ordering tiebreak.  Multiple concurrent 'extend' layers can be
        # combined; concurrent 'replace' layers must agree.
        by_precedence = {}
        for view, mode, layer_members in declarations:
            by_precedence.setdefault(view.precedence, []).append(
                (view, mode, frozenset(layer_members))
            )
        for precedence, group in by_precedence.items():
            if len(group) == 1:
                continue
            modes = {mode for _, mode, _ in group}
            if modes == {'extend'}:
                continue
            member_sets = {frozenset(items) for _, _, items in group}
            if modes == {'replace'} and len(member_sets) == 1:
                continue
            raise ScenarioConflictError(
                f'Conflicting same-priority set declarations for {symbol!r} '
                f'in scenario {scenario!r}: '
                + ', '.join(sorted({view.name for view, _, _ in group}))
            )

        # Whole-symbol retractions compete with declarations at equal or
        # higher precedence.
        membership = OrderedDict()
        sources = {}
        member_declaration_precedence = {}
        for view, mode, layer_members in declarations:
            # Conflict if a same-precedence whole-symbol retraction exists.
            for retract in whole_retractions:
                if retract.precedence == view.precedence:
                    raise ScenarioConflictError(
                        f'Same-priority declaration and retraction for set '
                        f'{symbol!r} in scenario {scenario!r}: '
                        + ', '.join(sorted({view.name, retract.layer}))
                    )
            if mode == 'replace':
                membership = OrderedDict((member, view) for member in layer_members)
            else:
                for member in layer_members:
                    membership.setdefault(member, view)
            for member in layer_members:
                member_declaration_precedence[member] = view.precedence

        # Apply whole-symbol retractions per member: the strongest
        # declaration or retraction for each member wins.
        for retract in whole_retractions:
            for member in list(membership):
                if member_declaration_precedence[member] >= retract.precedence:
                    # The retraction outranks (or ties) this member's
                    # declaration.  Higher-precedence members survive.
                    sources[member] = retract.provenance
                    del membership[member]
                    del member_declaration_precedence[member]

        # Apply member-level retractions.
        for member, entries in retractions.items():
            best = self._best(entries)
            self._assert_unique(
                best,
                f'Conflicting same-priority retractions for set member '
                f'{symbol!r}[{member!r}] in scenario {scenario!r}',
            )
            winner = best[0]
            if member not in membership:
                # The layer's own membership snapshot may already omit the
                # member; the retraction is still a visible tombstone.
                sources.setdefault(member, winner.provenance)
                continue
            declaration_precedence = member_declaration_precedence[member]
            if winner.precedence <= declaration_precedence:
                del membership[member]
                del member_declaration_precedence[member]
                sources[member] = winner.provenance

        candidates = tuple(
            entry
            for view, _, _ in declarations
            for entry in [
                _Declaration(
                    symbol=symbol,
                    index=member,
                    value=True,
                    layer=view.name,
                    version=view.snapshot.version,
                    priority=view.snapshot.priority,
                    distance=view.distance,
                    precedence=view.precedence,
                    order=view.order,
                )
                for member in view.snapshot.sets.get(symbol, ())
            ]
        )
        if not membership:
            # An explicitly declared, empty set is still a declaration.
            # A retraction that wiped the whole set is reported as retracted.
            winning_retract = None
            for retract in whole_retractions:
                if not any(
                    view.precedence < retract.precedence for view, _, _ in declarations
                ):
                    winning_retract = retract
                    break
            is_declared = bool(declarations) and winning_retract is None
            provenance[symbol] = ResolvedSymbol(
                name=symbol,
                kind='set',
                status=DECLARED if is_declared else RETRACTED,
                value={None: []} if is_declared else None,
                provenance=(
                    None if winning_retract is None else winning_retract.provenance
                ),
                sources=sources,
                candidates=candidates,
            )
            if is_declared:
                resolved[symbol] = {None: []}
            return
        for member, view in membership.items():
            sources[member] = Provenance(
                layer=view.name,
                layer_version=view.snapshot.version,
                status=DECLARED,
                distance=view.distance,
                priority=view.snapshot.priority,
            )
        payload = {None: list(membership)}
        resolved[symbol] = payload
        provenance[symbol] = ResolvedSymbol(
            name=symbol,
            kind='set',
            value=payload,
            status=DECLARED,
            sources=sources,
            candidates=candidates,
        )

    # ------------------------------------------------------------------
    # Index sets and defaults
    # ------------------------------------------------------------------
    def _effective_indices(self, symbol, views, resolved_sets=None):
        """Resolve the parameter's effective index set from its own layers."""
        membership = OrderedDict()
        declaration_precedence = {}
        declarations = []
        retractions = {}
        whole_retractions = []
        linked_sets = []
        for view in views:
            snapshot = view.snapshot
            if symbol in snapshot.indices:
                mode = snapshot.index_modes.get(symbol, 'replace')
                declarations.append((view, mode, tuple(snapshot.indices[symbol])))
            if symbol in getattr(snapshot, 'index_sets', {}):
                linked_sets.append((view, snapshot.index_sets[symbol]))
            if symbol in snapshot.retractions:
                for index in snapshot.retractions[symbol]:
                    retractions.setdefault(index, []).append((view, True))
            if symbol in snapshot.symbol_retracted:
                whole_retractions.append(view)

        by_precedence = {}
        for view, mode, layer_members in declarations:
            by_precedence.setdefault(view.precedence, []).append(
                (view, mode, frozenset(layer_members))
            )
        for precedence, group in by_precedence.items():
            if len(group) == 1:
                continue
            modes = {mode for _, mode, _ in group}
            if modes == {'extend'}:
                continue
            member_sets = {frozenset(items) for _, _, items in group}
            if modes == {'replace'} and len(member_sets) == 1:
                continue
            raise ScenarioConflictError(
                f'Conflicting same-priority index declarations for {symbol!r}: '
                + ', '.join(sorted({view.name for view, _, _ in group}))
            )

        for view, mode, layer_members in declarations:
            for retract in whole_retractions:
                if retract.precedence == view.precedence:
                    raise ScenarioConflictError(
                        f'Same-priority index declaration and retraction for '
                        f'{symbol!r}: ' + ', '.join(sorted({view.name, retract.name}))
                    )
            if mode == 'replace':
                membership = OrderedDict((index, view) for index in layer_members)
            else:
                for index in layer_members:
                    membership.setdefault(index, view)
            for index in layer_members:
                declaration_precedence[index] = view.precedence

        for retract in sorted(
            whole_retractions, key=lambda view: view.precedence, reverse=True
        ):
            for index in list(membership):
                if declaration_precedence[index] > retract.precedence:
                    del membership[index]
                    del declaration_precedence[index]

        for index, entries in retractions.items():
            if index not in membership:
                continue
            best = min(view.precedence for view, _ in entries)
            if best <= declaration_precedence[index]:
                del membership[index]
                del declaration_precedence[index]
        if not declarations and linked_sets:
            # ``Param(J)``-style declaration: the effective index set is the
            # referenced set's membership.  Resolve it as a set in this same
            # scenario, never from a sibling.  The highest-precedence link
            # wins; equal-precedence links to different sets are ambiguous.
            best_link_precedence = min(view.precedence for view, _ in linked_sets)
            best_links = [
                (view, name)
                for view, name in linked_sets
                if view.precedence == best_link_precedence
            ]
            if len({name for _, name in best_links}) > 1:
                raise ScenarioConflictError(
                    f'Conflicting same-priority index-set links for '
                    f'parameter {symbol!r}: '
                    + ', '.join(sorted({view.name for view, _ in best_links}))
                )
            set_name = best_links[0][1]
            set_payload = (resolved_sets or {}).get(set_name)
            if set_payload is not None and None in set_payload:
                membership = OrderedDict((member, None) for member in set_payload[None])
        return membership

    def _apply_defaults(self, views, scenario, resolved, provenance, kinds):
        """Apply each parameter's effective default to undeclared indices."""
        for symbol, kind in kinds.items():
            if kind != 'param':
                continue
            defaults = []
            default_retractions = []
            for view in views:
                snapshot = view.snapshot
                if symbol in snapshot.defaults:
                    defaults.append((view, snapshot.defaults[symbol]))
                if symbol in snapshot.default_retracted:
                    default_retractions.append(view)
            if not defaults:
                continue
            best_precedence = min(view.precedence for view, _ in defaults)
            best = [
                (view, value)
                for view, value in defaults
                if view.precedence == best_precedence
            ]
            tied_retractions = [
                view
                for view in default_retractions
                if view.precedence == best_precedence
            ]
            if tied_retractions and len({view.name for view, _ in best}) > 0:
                raise ScenarioConflictError(
                    f'Same-priority default and default retraction for '
                    f'parameter {symbol!r} in scenario {scenario!r}: '
                    + ', '.join(
                        sorted(
                            {view.name for view, _ in best}
                            | {view.name for view in tied_retractions}
                        )
                    )
                )
            if len({view.name for view, _ in best}) > 1:
                values = {repr(value) for _, value in best}
                if len(values) > 1:
                    raise ScenarioConflictError(
                        f'Conflicting same-priority defaults for parameter '
                        f'{symbol!r} in scenario {scenario!r}: '
                        + ', '.join(sorted(view.name for view, _ in best))
                    )
            if any(view.precedence < best_precedence for view in default_retractions):
                continue
            default_view, default_value = best[0]
            resolved_symbol = provenance.get(symbol)
            if resolved_symbol is not None and resolved_symbol.is_retracted:
                continue

            effective = resolved.get(symbol)
            if effective is None:
                effective = OrderedDict()
                sources = {}
            else:
                if not isinstance(effective, Mapping):
                    continue
                sources = dict(resolved_symbol.sources) if resolved_symbol else {}

            declared = set(effective)
            indices = list(self._effective_indices(symbol, views, resolved))
            if not indices:
                # Never borrow indices from another scenario: when the
                # parameter has no index declaration, only indices already
                # present in this scenario's input are eligible.  If the
                # symbol is a scalar that was never declared, the default
                # applies at index None.
                indices = list(declared) or [None]
            applied = False
            for index in indices:
                if index in declared:
                    continue
                if (
                    resolved_symbol is not None
                    and resolved_symbol.status_for(index) == RETRACTED
                ):
                    continue
                effective[index] = copy.deepcopy(default_value)
                applied = True
                sources[index] = Provenance(
                    layer=default_view.name,
                    layer_version=default_view.snapshot.version,
                    status=DEFAULTED,
                    distance=default_view.distance,
                    priority=default_view.snapshot.priority,
                )
            if not applied:
                continue
            resolved[symbol] = effective
            if symbol not in provenance:
                provenance[symbol] = ResolvedSymbol(
                    name=symbol,
                    kind='param',
                    value=effective,
                    status=DEFAULTED,
                    sources=sources,
                )
            else:
                previous = provenance[symbol]
                provenance[symbol] = ResolvedSymbol(
                    name=symbol,
                    kind='param',
                    value=effective,
                    status=previous.status,
                    sources=sources,
                    provenance=previous.provenance,
                    candidates=previous.candidates,
                )

    # ------------------------------------------------------------------
    # Fingerprinting and multi-scenario helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _fingerprint(resolved, provenance):
        # The fingerprint intentionally excludes layer *revision numbers*:
        # a no-op version bump must not force a rebuild.  It includes the
        # effective data and the identity/status of the winning sources.
        return content_fingerprint(
            dict(resolved),
            {
                symbol: {
                    'status': item.status,
                    'sources': {
                        index: (
                            source.layer,
                            source.status,
                            source.distance,
                            source.priority,
                        )
                        for index, source in item.sources.items()
                    },
                    'provenance': (
                        None
                        if item.provenance is None
                        else (
                            item.provenance.layer,
                            item.provenance.status,
                            item.provenance.distance,
                            item.provenance.priority,
                        )
                    ),
                }
                for symbol, item in provenance.items()
            },
        )

    def resolve_all(self, include_defaults=True):
        """Resolve every node in insertion order."""
        return OrderedDict(
            (name, self.resolve(name, include_defaults)) for name in self._layers
        )
