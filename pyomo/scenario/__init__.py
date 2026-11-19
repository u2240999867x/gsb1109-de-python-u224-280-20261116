# ____________________________________________________________________________________
#
# Pyomo: Python Optimization Modeling Objects
# Copyright (c) 2008-2026 National Technology and Engineering Solutions of Sandia, LLC
# Under the terms of Contract DE-NA0003525 with National Technology and Engineering
# Solutions of Sandia, LLC, the U.S. Government retains certain rights in this
# software.  This software is distributed under the 3-clause BSD License.
# ____________________________________________________________________________________
"""Scenario inheritance, override/retraction resolution, and publishing.

This package builds *complete* model input for every scenario in an
inheritance graph, with per-index provenance, and publishes independent
model instances.

Quick start::

    from pyomo.scenario import DataLayer, ScenarioGraph, ScenarioRuntime

    base = DataLayer('base', sets={'J': [1, 2]}, params={'c': {1: 1.0, 2: 2.0}})
    child = DataLayer('child', params={'c': {2: 5.0}}, retract={'c': [1]})

    graph = ScenarioGraph({'base': base})
    graph.add_layer('child', child, parents=['base'])
    inputs = graph.resolve('child')

    inputs['c']              # {2: 5.0}
    inputs.source('c', 2).layer        # 'child'
    inputs.source('c', 1).status       # 'retracted'

    runtime = ScenarioRuntime(graph, model=my_abstract_model)
    runtime.load_all()
    with runtime:
        instance = runtime.publish('child')   # independent clone

Semantics
---------
* **Override** is the nearest declaration in the inheritance graph.
  Explicit layer ``priority`` values take precedence over distance.
* **Retraction** is an explicit declaration, distinct from never declaring
  a symbol or index.  It blocks lower layers and inherited defaults.
* **Cycles** raise :class:`ScenarioCycleError`; a cycle is never silently
  broken by file or insertion order.
* **Same-priority conflicts** raise :class:`ScenarioConflictError`; an
  ambiguous tie is never resolved by "last file wins".
* **Defaults and index sets** are resolved per scenario from that
  scenario's own effective versions; members are never borrowed from
  another scenario.
"""

from pyomo.scenario.data import (
    RETRACT,
    BaseDraft,
    DataLayer,
    LayerSnapshot,
    SharedDraft,
    content_fingerprint,
    is_retract,
)
from pyomo.scenario.errors import (
    ScenarioConflictError,
    ScenarioCycleError,
    ScenarioError,
    ScenarioLoadError,
)
from pyomo.scenario.graph import (
    CYCLE,
    CONFLICT,
    CONFLICTED,
    DECLARED,
    DEFAULTED,
    NEVER_DECLARED,
    RETRACTED,
    Provenance,
    ResolvedScenario,
    ResolvedSymbol,
    ScenarioGraph,
)
from pyomo.scenario.runtime import (
    PublishedScenario,
    ScenarioManager,
    ScenarioRegistry,
    ScenarioRuntime,
)
from pyomo.scenario.spec import (
    build_graph,
    build_inputs,
    build_runtime,
    load_scenarios,
)

__all__ = [
    'RETRACT',
    'CYCLE',
    'CONFLICT',
    'CONFLICTED',
    'DECLARED',
    'DEFAULTED',
    'NEVER_DECLARED',
    'RETRACTED',
    'BaseDraft',
    'DataLayer',
    'LayerSnapshot',
    'Provenance',
    'PublishedScenario',
    'ResolvedScenario',
    'ResolvedSymbol',
    'ScenarioConflictError',
    'ScenarioCycleError',
    'ScenarioError',
    'ScenarioGraph',
    'ScenarioLoadError',
    'ScenarioManager',
    'ScenarioRegistry',
    'ScenarioRuntime',
    'SharedDraft',
    'build_graph',
    'build_inputs',
    'build_runtime',
    'content_fingerprint',
    'is_retract',
    'load_scenarios',
]
