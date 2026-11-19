Scenario Inheritance
====================

The :mod:`pyomo.scenario` package builds complete model input for every
scenario in an inheritance graph, records per-index provenance, and publishes
independent model instances.

Core concepts
-------------

* **Layers** are :class:`~pyomo.scenario.DataLayer` objects.  Each layer has a
  monotonic ``version`` and a content ``fingerprint``.
* **Graphs** are :class:`~pyomo.scenario.ScenarioGraph` objects.  Parents are
  more general; children inherit all reachable data.
* **Overrides** are resolved per symbol and per index.  The nearest declaration
  wins; an explicit layer ``priority`` outranks inheritance distance.
* **Retractions** are explicit declarations.  ``RETRACT`` (or
  ``retract={...}``) records a tombstone that is distinct from
  "never declared" and blocks inherited defaults.
* **Conflicts and cycles** raise
  :class:`~pyomo.scenario.ScenarioConflictError` and
  :class:`~pyomo.scenario.ScenarioCycleError`.  Insertion or file order is
  never used to break a semantic tie.
* **Defaults and index sets** are resolved from each scenario's own effective
  layers.  A scenario never borrows missing members from a sibling.

Example
-------

.. testcode::

    import pyomo.scenario as scenario

    base = scenario.DataLayer(
        'base',
        sets={'J': [1, 2, 3]},
        params={'c': {1: 1.0, 2: 2.0, 3: 3.0}},
    )
    high = scenario.DataLayer(
        'high',
        params={'c': {2: 9.0}},
        retract={'c': [3]},
    )

    graph = scenario.ScenarioGraph({'base': base})
    graph.add_layer('high', high, parents=['base'])

    resolved = graph.resolve('high')
    resolved.data['c']                 # {1: 1.0, 2: 9.0}
    resolved.source('c', 2).layer      # 'high'
    resolved.source('c', 3).status     # 'retracted'

Publishing instances
--------------------

.. testcode::

    import pyomo.environ as pyo

    model = pyo.AbstractModel()
    model.J = pyo.Set()
    model.c = pyo.Param(model.J)

    runtime = scenario.ScenarioRuntime(graph, model=model)
    runtime.load_all()
    first = runtime.publish('high')    # independent clone
    second = runtime.publish('high')

A failure while loading one scenario is stored on that scenario's record;
other scenarios remain published.  :meth:`ScenarioRuntime.refresh` rebuilds
only scenarios whose resolved content fingerprint changed.

Declarative specifications
--------------------------

:func:`pyomo.scenario.build_inputs` accepts a plain mapping:

.. testcode::

    inputs = scenario.build_inputs(
        {
            'base': {
                'sets': {'J': [1, 2]},
                'params': {'c': {1: 1.0, 2: 2.0}},
            },
            'high': {
                'parents': ['base'],
                'params': {'c': {2: 9.0}},
                'retract': {'c': [1]},
            },
        }
    )
    dict(inputs['high'].data['c'])     # {2: 9.0}

Layer attributes
----------------

.. autoclass:: pyomo.scenario.DataLayer
   :members:

.. autoclass:: pyomo.scenario.ScenarioGraph
   :members:

.. autoclass:: pyomo.scenario.ScenarioRuntime
   :members:

.. autoclass:: pyomo.scenario.ResolvedScenario
   :members:

.. autoclass:: pyomo.scenario.ResolvedSymbol
   :members:

.. autoclass:: pyomo.scenario.Provenance
   :members:
