# ____________________________________________________________________________________
#
# Pyomo: Python Optimization Modeling Objects
# Copyright (c) 2008-2026 National Technology and Engineering Solutions of Sandia, LLC
# Under the terms of Contract DE-NA0003525 with National Technology and Engineering
# Solutions of Sandia, LLC, the U.S. Government retains certain rights in this
# software.  This software is distributed under the 3-clause BSD License.
# ____________________________________________________________________________________
"""Declarative convenience front end for scenario inheritance.

A scenario specification may be supplied as plain Python data::

    spec = {
        'base': {
            'sets': {'J': [1, 2, 3]},
            'params': {'c': {1: 1.0, 2: 2.0, 3: 3.0}},
        },
        'high': {
            'parents': ['base'],
            'params': {'c': {2: 9.0}},
            'retract': {'c': [3]},
        },
    }

    inputs = build_inputs(spec)

``build_inputs`` returns ``{scenario: ResolvedScenario}``.

Supported layer keys are ``data``, ``sets``, ``params``, ``indices``,
``defaults``, ``retract``, ``priority``, ``set_mode``, ``index_mode``,
``set_modes``, ``index_modes``, ``retract_defaults``, ``filename``, and
``namespace``.  A ``filename`` layer is loaded with
:meth:`DataLayer.from_dat`.  Graph edges may also be supplied separately with
the ``inherits`` argument (``{child: [parents]}``).
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Mapping

from pyomo.scenario.data import DataLayer
from pyomo.scenario.errors import ScenarioError
from pyomo.scenario.graph import ScenarioGraph
from pyomo.scenario.runtime import ScenarioRuntime

__all__ = [
    'build_graph',
    'build_inputs',
    'build_runtime',
    'load_scenarios',
]


_LAYER_KEYS = {
    'data',
    'sets',
    'params',
    'indices',
    'index_sets',
    'defaults',
    'retract',
    'priority',
    'set_mode',
    'index_mode',
    'set_modes',
    'index_modes',
    'retract_defaults',
    'filename',
    'namespace',
}


def _layer_from_spec(name, spec):
    if isinstance(spec, DataLayer):
        return spec
    if not isinstance(spec, Mapping):
        raise TypeError(f'Scenario layer {name!r} must be a mapping or DataLayer')
    unknown = set(spec) - _LAYER_KEYS - {'parents'}
    if unknown:
        raise ScenarioError(
            f'Unknown keys for scenario layer {name!r}: ' + ', '.join(sorted(unknown))
        )
    if 'filename' in spec:
        layer = DataLayer.from_dat(
            spec['filename'],
            name=name,
            namespace=spec.get('namespace'),
        )
        for symbol, value in spec.get('defaults', {}).items():
            layer.set_default(symbol, value)
        for symbol, set_name in spec.get('index_sets', {}).items():
            layer.link_index_set(symbol, set_name)
        for symbol in spec.get('retract_defaults', ()):
            layer.retract_default(symbol)
        if 'set_modes' in spec:
            for symbol, mode in spec['set_modes'].items():
                layer._set_modes[symbol] = layer._validate_mode(mode)
        if 'index_modes' in spec:
            for symbol, mode in spec['index_modes'].items():
                layer._index_modes[symbol] = layer._validate_mode(mode)
        return layer
    kwargs = {
        key: spec[key]
        for key in _LAYER_KEYS
        if key in spec and key not in ('retract_defaults', 'index_sets')
    }
    layer = DataLayer(name=name, **kwargs)
    for symbol, set_name in spec.get('index_sets', {}).items():
        layer.link_index_set(symbol, set_name)
    for symbol in spec.get('retract_defaults', ()):
        layer.retract_default(symbol)
    return layer


def build_graph(spec, inherits=None):
    """Build a :class:`ScenarioGraph` from a declarative specification."""
    if not isinstance(spec, Mapping):
        raise TypeError('Scenario spec must be a mapping')
    graph = ScenarioGraph()
    parent_map = OrderedDict()
    for name, layer_spec in spec.items():
        layer = _layer_from_spec(name, layer_spec)
        graph.add_layer(name, layer)
        parents = ()
        if isinstance(layer_spec, Mapping):
            parents = layer_spec.get('parents', ())
        if inherits and name in inherits:
            if parents:
                raise ScenarioError(
                    f'Scenario {name!r} declares parents both inline and in '
                    'the inherits mapping'
                )
            parents = inherits[name]
        if isinstance(parents, str):
            parents = [parents]
        parent_map[name] = list(parents)
    for name, parents in parent_map.items():
        if parents:
            graph.set_parents(name, parents)
    return graph


def build_inputs(spec, inherits=None, include_defaults=True):
    """Return ``{scenario: ResolvedScenario}`` for the specification."""
    graph = build_graph(spec, inherits=inherits)
    return graph.resolve_all(include_defaults=include_defaults)


def build_runtime(spec, model=None, inherits=None, validate=True, load=True):
    """Build a :class:`ScenarioRuntime` from the specification."""
    graph = build_graph(spec, inherits=inherits)
    runtime = ScenarioRuntime(graph, model=model, validate=validate)
    if load:
        runtime.load_all()
    return runtime


def load_scenarios(spec, model=None, inherits=None, validate=True):
    """Build a runtime and load all scenarios, returning the runtime."""
    return build_runtime(
        spec, model=model, inherits=inherits, validate=validate, load=True
    )
