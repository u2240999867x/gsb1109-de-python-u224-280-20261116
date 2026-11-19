# ____________________________________________________________________________________
#
# Pyomo: Python Optimization Modeling Objects
# Copyright (c) 2008-2026 National Technology and Engineering Solutions of Sandia, LLC
# Under the terms of Contract DE-NA0003525 with National Technology and Engineering
# Solutions of Sandia, LLC, the U.S. Government retains certain rights in this
# software.  This software is distributed under the 3-clause BSD License.
# ____________________________________________________________________________________
"""Building and publishing independent scenario instances.

A :class:`ScenarioRuntime` sits between a :class:`ScenarioGraph` and a Pyomo
model.  It caches one *template* model instance per scenario and hands out
independent clones, so two published instances never share mutable state.

Rebuilds are incremental and content-addressed:

* a base-draft revision only invalidates its descendants;
* a scenario whose effective (resolved) input fingerprint is unchanged is
  not rebuilt, even when a layer's version number changed;
* changing a parameter default or an index set changes that scenario's
  fingerprint and therefore triggers a rebuild of just that scenario;
* a failure loading one scenario is recorded without discarding other
  scenarios that loaded successfully.
"""

from __future__ import annotations

import copy

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Mapping

from pyomo.scenario.data import content_fingerprint
from pyomo.scenario.errors import ScenarioLoadError, ScenarioError
from pyomo.scenario.graph import ResolvedScenario, ScenarioGraph

__all__ = [
    'PublishedScenario',
    'ScenarioRuntime',
    'ScenarioRegistry',
    'ScenarioManager',
]


def _default_factory(scenario):
    """Return the already-resolved data as a concrete data dictionary."""
    return scenario.model_input()


@dataclass
class PublishedScenario:
    """Registry record for one scenario."""

    name: str
    resolved: object = None
    instance: object = None
    fingerprint: str = None
    error: Exception = None
    builds: int = 0
    layer_versions: Mapping = field(default_factory=dict)

    @property
    def ok(self):
        return self.error is None


class ScenarioRuntime:
    """Build, cache, and publish scenario model instances.

    Parameters
    ----------
    graph:
        The :class:`ScenarioGraph` to resolve.
    model:
        An optional model or callable.  A callable receives the
        :class:`ResolvedScenario`; an abstract model is instantiated with
        ``create_instance(data=resolved.model_input())``; a concrete model
        is cloned and then has its data validated (concrete models cannot
        be re-instantiated safely).
    validate:
        If True (default), check that parameter data references only the
        scenario's effective index set.  This never borrows missing members
        from another scenario.
    """

    def __init__(self, graph, model=None, validate=True):
        if not isinstance(graph, ScenarioGraph):
            raise TypeError('ScenarioRuntime requires a ScenarioGraph')
        self.graph = graph
        self.model = model
        self.validate = validate
        self._records = OrderedDict()
        self._layer_fingerprints = OrderedDict()
        self._topology = OrderedDict()
        self._base_versions = {}
        self._rebuild_count = 0

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def scenarios(self):
        return tuple(self.graph)

    def record(self, scenario):
        return self._records.get(scenario)

    def instance(self, scenario):
        """Return the cached template instance (do not mutate)."""
        self._require_record(scenario)
        return self._records[scenario].instance

    def errors(self):
        return OrderedDict(
            (name, record.error)
            for name, record in self._records.items()
            if record.error is not None
        )

    @property
    def rebuild_count(self):
        return self._rebuild_count

    # ------------------------------------------------------------------
    # Resolution / build
    # ------------------------------------------------------------------
    def _require_record(self, scenario):
        if scenario not in self._records:
            raise ScenarioError(
                f'Scenario {scenario!r} has not been loaded; call load_all() '
                'or load() first'
            )

    def resolve(self, scenario):
        """Resolve (but do not build) one scenario, with validation."""
        resolved = self.graph.resolve(scenario)
        if self.validate:
            self._validate(resolved)
        return resolved

    def _validate(self, resolved):
        # Each scenario is checked against its own effective index sets.  We
        # do not consult any other scenario's resolved input.
        problems = self.graph.check_indices(resolved.name, strict=self.validate)
        if problems:
            symbol, index = problems[0]
            raise ScenarioLoadError(
                resolved.name,
                f'Parameter {symbol!r} contains index {index!r} that is not '
                "part of this scenario's effective index set",
            )

    def load(self, scenario):
        """Resolve and build one scenario, returning its registry record.

        Errors are stored on the record; other scenarios are untouched.
        """
        record = self._records.get(scenario)
        if record is None:
            record = PublishedScenario(name=scenario)
            self._records[scenario] = record
        try:
            resolved = self.resolve(scenario)
            record.resolved = resolved
            record.fingerprint = resolved.fingerprint
            record.error = None
            record.instance = self._build(resolved)
            record.builds += 1
            self._rebuild_count += 1
            record.layer_versions = {
                name: self.graph.layer(name).version for name in resolved.layers
            }
        except Exception as err:  # noqa: BLE001 - isolate per-scenario failure
            record.error = (
                err
                if isinstance(err, ScenarioError)
                else ScenarioLoadError(scenario, err)
            )
            record.instance = None
        if scenario in self.graph:
            self._layer_fingerprints = self._snapshot_layer_fingerprints()
            self._topology = self._snapshot_topology()
        return record

    def _build(self, resolved):
        factory = _default_factory if self.model is None else self.model
        if callable(factory):
            return factory(resolved)
        # Pyomo model objects.
        data = resolved.model_input()
        if hasattr(factory, 'is_constructed') and factory.is_constructed():
            instance = factory.clone()
            self._apply_concrete_data(instance, data)
            return instance
        if hasattr(factory, 'create_instance'):
            return factory.create_instance(data=data)
        raise ScenarioError(
            'ScenarioRuntime model must be callable, an abstract model with '
            'create_instance(), or a concrete model instance'
        )

    @staticmethod
    def _apply_concrete_data(instance, data):
        """Load data into a concrete model without sharing mutable state."""
        if isinstance(data, Mapping) and set(data) == {None}:
            data = data[None]
        for symbol, payload in data.items():
            component = instance.find_component(symbol)
            if component is None:
                continue
            if isinstance(payload, Mapping):
                if None in payload:
                    payload = payload[None]
                if hasattr(component, 'set_value'):
                    for index, value in (
                        payload.items() if isinstance(payload, Mapping) else ()
                    ):
                        try:
                            component[index].set_value(value)
                        except (KeyError, IndexError, TypeError):
                            pass

    def load_all(self):
        """Load every scenario, isolating failures per scenario."""
        for scenario in self.graph:
            self.load(scenario)
        self._layer_fingerprints = self._snapshot_layer_fingerprints()
        self._topology = self._snapshot_topology()
        return OrderedDict(self._records)

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------
    def publish(self, scenario, copy_instance=True):
        """Publish an independent instance for *scenario*.

        By default the template is cloned, so mutating the returned model
        cannot affect any other published instance.  Pass
        ``copy_instance=False`` only when the caller will not mutate the
        template.
        """
        self._require_record(scenario)
        record = self._records[scenario]
        if record.error is not None:
            raise record.error
        if record.instance is None:
            raise ScenarioLoadError(scenario, 'scenario has no built instance')
        if copy_instance:
            if hasattr(record.instance, 'clone'):
                return record.instance.clone()
            return copy.deepcopy(record.instance)
        return record.instance

    def publish_all(self, copy_instance=True, raise_on_error=False):
        """Publish all successful scenarios, keeping failures isolated."""
        result = OrderedDict()
        errors = OrderedDict()
        for scenario in self._records:
            record = self._records[scenario]
            if record.error is not None:
                errors[scenario] = record.error
                if raise_on_error:
                    raise record.error
                continue
            result[scenario] = self.publish(scenario, copy_instance=copy_instance)
        return result, errors

    # ------------------------------------------------------------------
    # Incremental refresh
    # ------------------------------------------------------------------
    def _snapshot_layer_fingerprints(self):
        return OrderedDict(
            (name, self.graph.layer(name).fingerprint()) for name in self.graph
        )

    def _affected(self, changed):
        """Return changed layers plus all descendants."""
        affected = set(changed)
        queue = list(changed)
        while queue:
            name = queue.pop()
            for child in self.graph.children(name):
                if child not in affected:
                    affected.add(child)
                    queue.append(child)
        return affected

    def refresh(self):
        """Rebuild only scenarios whose effective input actually changed.

        Direct edits to a layer's exposed mappings are detected through
        content fingerprints even if the layer's ``version`` was not
        bumped.  If an affected scenario resolves to the same fingerprint
        as before, its existing template is retained.
        """
        previous = self._layer_fingerprints
        current = self._snapshot_layer_fingerprints()
        topology = self._snapshot_topology()
        previous_topology = getattr(self, '_topology', None)
        if not self._records:
            self._layer_fingerprints = current
            self._topology = topology
            return OrderedDict()
        changed = {
            name
            for name, fingerprint in current.items()
            if previous.get(name) != fingerprint
        }
        changed.update(name for name in previous if name not in current)
        if previous_topology is not None and previous_topology != topology:
            # Parent edges changed: every scenario whose reachable layer set
            # or distances changed is affected.  We conservatively refresh
            # all nodes that are new, or whose ancestors differ.
            old_parents = dict(previous_topology)
            for name, parents in topology.items():
                if old_parents.get(name) != parents:
                    changed.add(name)
            for name in old_parents:
                if name not in topology:
                    changed.add(name)
        affected = self._affected(changed)
        rebuilt = OrderedDict()
        for scenario in list(self._records):
            if scenario not in affected:
                continue
            record = self._records[scenario]
            try:
                resolved = self.resolve(scenario)
                if resolved.fingerprint == record.fingerprint:
                    # The revision did not change the effective input.
                    continue
                record.resolved = resolved
                record.fingerprint = resolved.fingerprint
                record.instance = self._build(resolved)
                record.builds += 1
                record.error = None
                self._rebuild_count += 1
                record.layer_versions = {
                    name: self.graph.layer(name).version for name in resolved.layers
                }
                rebuilt[scenario] = record
            except Exception as err:  # noqa: BLE001
                record.error = (
                    err
                    if isinstance(err, ScenarioError)
                    else ScenarioLoadError(scenario, err)
                )
                record.instance = None
                rebuilt[scenario] = record
        self._layer_fingerprints = current
        self._topology = topology
        return rebuilt

    def _snapshot_topology(self):
        return OrderedDict(
            (name, tuple(self.graph.parents(name))) for name in self.graph
        )

    def __enter__(self):
        self.load_all()
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


ScenarioRegistry = ScenarioRuntime
ScenarioManager = ScenarioRuntime
