# ____________________________________________________________________________________
#
# Pyomo: Python Optimization Modeling Objects
# Copyright (c) 2008-2026 National Technology and Engineering Solutions of Sandia, LLC
# Under the terms of Contract DE-NA0003525 with National Technology and Engineering
# Solutions of Sandia, LLC, the U.S. Government retains certain rights in this
# software.  This software is distributed under the 3-clause BSD License.
# ____________________________________________________________________________________

import os

import pyomo.common.unittest as unittest

import pyomo.environ as pyo
from pyomo.common.fileutils import this_file_dir
from pyomo.scenario import (
    DECLARED,
    DEFAULTED,
    NEVER_DECLARED,
    RETRACT,
    RETRACTED,
    DataLayer,
    ScenarioConflictError,
    ScenarioCycleError,
    ScenarioGraph,
    ScenarioRuntime,
    build_graph,
    build_inputs,
    build_runtime,
)


class TestDataLayer(unittest.TestCase):
    def test_retraction_is_a_declaration(self):
        layer = DataLayer('base', params={'p': {1: 1, 2: 2}})
        layer.retract('p', 1)
        snapshot = layer.snapshot()
        self.assertEqual(dict(snapshot.values['p']), {2: 2})
        self.assertEqual(dict(snapshot.retractions['p']), {1: True})
        self.assertNotIn(1, snapshot.values['p'])
        self.assertNotIn('q', snapshot.values)

    def test_retract_whole_symbol_then_redeclare(self):
        layer = DataLayer('base', params={'p': {1: 1}})
        layer.retract('p')
        self.assertIn('p', layer.snapshot().symbol_retracted)
        layer.set_value('p', 1, 9)
        self.assertNotIn('p', layer.snapshot().symbol_retracted)
        self.assertEqual(dict(layer.snapshot().values['p']), {1: 9})

    def test_version_and_fingerprint(self):
        layer = DataLayer('base', params={'p': {1: 1}})
        before = layer.version
        fingerprint = layer.fingerprint()
        layer.set_value('p', 2, 2)
        self.assertGreater(layer.version, before)
        self.assertNotEqual(layer.fingerprint(), fingerprint)
        # Direct edits are caught by the content fingerprint.
        layer.data['p'][3] = 3
        self.assertNotEqual(layer.fingerprint(), fingerprint)

    def test_set_replace_and_extend(self):
        layer = DataLayer('base', sets={'J': [1, 2]})
        layer.declare_set('J', [2, 3])
        self.assertEqual(list(layer.snapshot().sets['J']), [2, 3])
        layer.extend_set('J', [1])
        self.assertEqual(set(layer.snapshot().sets['J']), {1, 2, 3})

    def test_retract_default(self):
        layer = DataLayer('base', defaults={'p': 0})
        layer.retract_default('p')
        snapshot = layer.snapshot()
        self.assertNotIn('p', snapshot.defaults)
        self.assertIn('p', snapshot.default_retracted)

    def test_fork_is_independent(self):
        base = DataLayer('base', params={'p': {1: 1}})
        fork = base.fork('child')
        fork.set_value('p', 1, 7)
        self.assertEqual(dict(base.snapshot().values['p']), {1: 1})
        self.assertEqual(dict(fork.snapshot().values['p']), {1: 7})


class TestScenarioGraph(unittest.TestCase):
    def _graph(self):
        base = DataLayer(
            'base',
            sets={'J': [1, 2, 3]},
            params={'c': {1: 1.0, 2: 2.0, 3: 3.0}},
        )
        child = DataLayer('child', params={'c': {2: 20.0}})
        leaf = DataLayer('leaf', params={'c': {3: 30.0}}, retract={'c': [2]})
        graph = ScenarioGraph({'base': base})
        graph.add_layer('child', child, parents=['base'])
        graph.add_layer('leaf', leaf, parents=['child'])
        return graph

    def test_complete_model_input(self):
        graph = self._graph()
        resolved = graph.resolve('leaf')
        self.assertEqual(dict(resolved.data['J']), {None: [1, 2, 3]})
        self.assertEqual(dict(resolved.data['c']), {1: 1.0, 3: 30.0})

    def test_per_index_provenance(self):
        graph = self._graph()
        resolved = graph.resolve('leaf')
        self.assertEqual(resolved.source('c', 1).layer, 'base')
        self.assertEqual(resolved.source('c', 1).status, DECLARED)
        self.assertEqual(resolved.source('c', 2).layer, 'leaf')
        self.assertEqual(resolved.source('c', 2).status, RETRACTED)
        self.assertEqual(resolved.source('c', 3).layer, 'leaf')
        self.assertEqual(resolved.source('J', 1).layer, 'base')

    def test_retraction_differs_from_never_declared(self):
        graph = self._graph()
        resolved = graph.resolve('leaf')
        self.assertEqual(resolved.symbol('c').status_for(2), RETRACTED)
        self.assertEqual(resolved.symbol('c').status_for(99), NEVER_DECLARED)

    def test_cycle_is_reported(self):
        graph = ScenarioGraph()
        graph.add_layer('a', DataLayer('a'))
        graph.add_layer('b', DataLayer('b'))
        graph.add_edge('a', 'b')
        graph.add_edge('b', 'a')
        with self.assertRaises(ScenarioCycleError):
            graph.resolve('a')
        with self.assertRaises(ScenarioCycleError):
            graph.topological_order()

    def test_same_priority_conflict(self):
        graph = ScenarioGraph()
        graph.add_layer('a', DataLayer('a', params={'x': {None: 1}}))
        graph.add_layer('b', DataLayer('b', params={'x': {None: 2}}))
        graph.add_layer('c', DataLayer('c'), parents=['a', 'b'])
        with self.assertRaises(ScenarioConflictError):
            graph.resolve('c')

    def test_explicit_priority_breaks_tie(self):
        graph = ScenarioGraph()
        graph.add_layer('a', DataLayer('a', params={'x': {None: 1}}))
        graph.add_layer('b', DataLayer('b', params={'x': {None: 2}}, priority=5))
        graph.add_layer('c', DataLayer('c'), parents=['a', 'b'])
        resolved = graph.resolve('c')
        self.assertEqual(resolved.data['x'], {None: 2})
        self.assertEqual(resolved.source('x').layer, 'b')

    def test_identical_same_priority_values_are_not_a_conflict(self):
        graph = ScenarioGraph()
        graph.add_layer('a', DataLayer('a', params={'x': {None: 1}}))
        graph.add_layer('b', DataLayer('b', params={'x': {None: 1}}))
        graph.add_layer('c', DataLayer('c'), parents=['a', 'b'])
        resolved = graph.resolve('c')
        self.assertEqual(resolved.data['x'], {None: 1})

    def test_defaults_only_fill_effective_indices(self):
        base = DataLayer('base', indices={'p': [1, 2]}, defaults={'p': 0})
        child = DataLayer('child', params={'p': {1: 10}})
        graph = ScenarioGraph()
        graph.add_layer('base', base)
        graph.add_layer('child', child, parents=['base'])
        resolved = graph.resolve('child')
        self.assertEqual(dict(resolved.data['p']), {1: 10, 2: 0})
        self.assertEqual(resolved.source('p', 1).status, DECLARED)
        self.assertEqual(resolved.source('p', 2).status, DEFAULTED)

    def test_default_retraction_blocks_inherited_default(self):
        base = DataLayer('base', indices={'p': [1, 2]}, defaults={'p': 0})
        child = DataLayer('child', indices={'p': [1, 2]}, retract={'p_default': True})
        child.retract_default('p')
        graph = ScenarioGraph()
        graph.add_layer('base', base)
        graph.add_layer('child', child, parents=['base'])
        resolved = graph.resolve('child')
        self.assertNotIn('p', resolved.data)

    def test_does_not_borrow_members_from_sibling(self):
        base = DataLayer('base', sets={'J': [1, 2]})
        a = DataLayer('a', sets={'J': [1, 2, 3]})
        b = DataLayer('b')
        graph = ScenarioGraph()
        graph.add_layer('base', base)
        graph.add_layer('a', a, parents=['base'])
        graph.add_layer('b', b, parents=['base'])
        self.assertEqual(dict(graph.resolve('a').data['J']), {None: [1, 2, 3]})
        self.assertEqual(dict(graph.resolve('b').data['J']), {None: [1, 2]})

    def test_set_extend(self):
        base = DataLayer('base', sets={'J': [1, 2]})
        child = DataLayer('child')
        child.extend_set('J', [3])
        graph = ScenarioGraph()
        graph.add_layer('base', base)
        graph.add_layer('child', child, parents=['base'])
        self.assertEqual(dict(graph.resolve('child').data['J']), {None: [1, 2, 3]})

    def test_ambiguous_set_tie_conflicts(self):
        graph = ScenarioGraph()
        graph.add_layer('a', DataLayer('a', sets={'J': [1]}))
        graph.add_layer('b', DataLayer('b', sets={'J': [2]}))
        graph.add_layer('c', DataLayer('c'), parents=['a', 'b'])
        with self.assertRaises(ScenarioConflictError):
            graph.resolve('c')


class TestScenarioRuntime(unittest.TestCase):
    def _abstract(self):
        m = pyo.AbstractModel()
        m.J = pyo.Set()
        m.c = pyo.Param(m.J)
        m.x = pyo.Var(m.J)
        return m

    def _graph(self):
        base = DataLayer(
            'base',
            sets={'J': [1, 2, 3]},
            params={'c': {1: 1, 2: 2, 3: 3}},
        )
        a = DataLayer('a', params={'c': {2: 20}})
        b = DataLayer('b', params={'c': {1: 10}})
        graph = ScenarioGraph({'base': base})
        graph.add_layer('a', a, parents=['base'])
        graph.add_layer('b', b, parents=['base'])
        return graph

    def test_published_instances_are_independent(self):
        runtime = ScenarioRuntime(self._graph(), model=self._abstract())
        runtime.load_all()
        first = runtime.publish('a')
        second = runtime.publish('a')
        first.x[2].set_value(42)
        self.assertIsNone(second.x[2].value)
        self.assertIsNot(first, second)
        self.assertIsNot(first.c, second.c)

    def test_one_failure_does_not_remove_others(self):
        graph = self._graph()
        calls = []

        def factory(resolved):
            calls.append(resolved.name)
            if resolved.name == 'b':
                raise RuntimeError('boom')
            return resolved.model_input()

        runtime = ScenarioRuntime(graph, model=factory)
        runtime.load_all()
        self.assertEqual(runtime.record('a').error, None)
        self.assertIsNotNone(runtime.record('b').error)
        instances, errors = runtime.publish_all()
        self.assertIn('a', instances)
        self.assertIn('b', errors)

    def test_rebuild_is_descendant_scoped(self):
        graph = self._graph()
        runtime = ScenarioRuntime(graph, model=self._abstract())
        runtime.load_all()
        a_builds = runtime.record('a').builds
        b_builds = runtime.record('b').builds
        base = graph.layer('base')
        base.set_value('c', 3, 100)
        runtime.refresh()
        self.assertGreater(runtime.record('a').builds, a_builds)
        self.assertGreater(runtime.record('b').builds, b_builds)

        # A change under 'a' must not rebuild 'b' (or 'base').
        a_builds = runtime.record('a').builds
        b_builds = runtime.record('b').builds
        base_builds = runtime.record('base').builds
        graph.layer('a').set_value('c', 2, 200)
        runtime.refresh()
        self.assertGreater(runtime.record('a').builds, a_builds)
        self.assertEqual(runtime.record('b').builds, b_builds)
        self.assertEqual(runtime.record('base').builds, base_builds)

    def test_version_bump_without_effective_change_is_not_rebuilt(self):
        graph = self._graph()
        runtime = ScenarioRuntime(graph, model=self._abstract())
        runtime.load_all()
        builds = runtime.record('a').builds
        # Declare a value identical to the inherited default.
        graph.layer('a').bump_version()
        runtime.refresh()
        self.assertEqual(runtime.record('a').builds, builds)

    def test_failed_rebuild_keeps_other_instances(self):
        graph = self._graph()
        fail = {'active': False}

        def factory(resolved):
            if fail['active'] and resolved.name == 'a':
                raise RuntimeError('reload failed')
            return resolved.model_input()

        runtime = ScenarioRuntime(graph, model=factory)
        runtime.load_all()
        fail['active'] = True
        graph.layer('a').set_value('c', 2, 99)
        runtime.refresh()
        self.assertIsNotNone(runtime.record('a').error)
        self.assertIsNone(runtime.record('b').error)
        self.assertIsNotNone(runtime.publish('b'))

    def test_direct_edit_is_detected_by_fingerprint(self):
        graph = self._graph()
        runtime = ScenarioRuntime(graph, model=self._abstract())
        runtime.load_all()
        builds = runtime.record('a').builds
        graph.layer('a').values['c'][2] = 77
        runtime.refresh()
        self.assertGreater(runtime.record('a').builds, builds)
        self.assertEqual(runtime.record('a').resolved['c'][2], 77)

    def test_retraction_tombstone_is_visible(self):
        base = DataLayer('base', sets={'J': [1, 2]}, params={'c': {1: 1, 2: 2}})
        child = DataLayer('child', retract={'c': [1]})
        graph = ScenarioGraph()
        graph.add_layer('base', base)
        graph.add_layer('child', child, parents=['base'])
        runtime = ScenarioRuntime(graph, model=self._abstract())
        runtime.load_all()
        instance = runtime.publish('child')
        self.assertEqual(list(instance.J), [1, 2])
        self.assertEqual(list(instance.c.keys()), [2])
        resolved = runtime.record('child').resolved
        self.assertEqual(resolved.source('c', 1).status, RETRACTED)


class TestDeclarativeSpec(unittest.TestCase):
    def test_build_inputs(self):
        spec = {
            'base': {
                'sets': {'J': [1, 2, 3]},
                'params': {'c': {1: 1.0, 2: 2.0, 3: 3.0}},
            },
            'hi': {
                'parents': ['base'],
                'params': {'c': {2: 9.0}},
                'retract': {'c': [3]},
            },
        }
        inputs = build_inputs(spec)
        self.assertEqual(dict(inputs['hi'].data['c']), {1: 1.0, 2: 9.0})
        self.assertEqual(inputs['hi'].source('c', 3).status, RETRACTED)
        graph = build_graph(spec)
        self.assertEqual(graph.parents('hi'), ('base',))

    def test_separate_inherits_argument(self):
        spec = {
            'base': {'params': {'c': {1: 1}}},
            'hi': {'params': {'c': {2: 2}}},
        }
        inputs = build_inputs(spec, inherits={'hi': ['base']})
        self.assertEqual(dict(inputs['hi'].data['c']), {1: 1, 2: 2})

    def test_dat_layer(self):
        here = this_file_dir()
        filename = os.path.join(here, 'data1.dat')
        with open(filename, 'w') as FILE:
            FILE.write('param c := 1 1.0 2 2.0;\n')
        try:
            layer = DataLayer.from_dat(filename, name='file')
            self.assertEqual(dict(layer.values['c']), {1: 1.0, 2: 2.0})
        finally:
            os.remove(filename)

    def test_build_runtime(self):
        spec = {
            'base': {'sets': {'J': [1]}, 'params': {'c': {1: 1}}},
        }
        model = pyo.AbstractModel()
        model.J = pyo.Set()
        model.c = pyo.Param(model.J)
        runtime = build_runtime(spec, model=model)
        self.assertEqual(list(runtime.publish('base').J), [1])


class TestRuntimeEdgeCases(unittest.TestCase):
    def test_retracted_only_symbol_is_distinguishable(self):
        layer = DataLayer('x', retract={'ghost': [1, 2]})
        resolved = ScenarioGraph({'x': layer}).resolve('x')
        self.assertEqual(resolved.provenance['ghost'].status, RETRACTED)
        self.assertEqual(resolved.retracted_indices('ghost'), (1, 2))
        self.assertNotIn('ghost', resolved.data)

    def test_empty_set_is_declared_not_retracted(self):
        layer = DataLayer('e', sets={'J': []})
        resolved = ScenarioGraph({'e': layer}).resolve('e')
        self.assertEqual(resolved.data['J'], {None: []})
        self.assertEqual(resolved.symbol('J').status, DECLARED)

    def test_scoped_cycle_does_not_poison_unrelated_scenario(self):
        graph = ScenarioGraph()
        graph.add_layer('a', DataLayer('a'))
        graph.add_layer('b', DataLayer('b'))
        graph.add_layer('ok', DataLayer('ok', params={'x': {None: 1}}))
        graph.add_edge('a', 'b')
        graph.add_edge('b', 'a')
        # Resolving an unrelated scenario is unaffected by the a/b cycle.
        self.assertEqual(graph.resolve('ok').data['x'], {None: 1})
        with self.assertRaises(ScenarioCycleError):
            graph.resolve('a')

    def test_default_and_default_retraction_tie_conflicts(self):
        a = DataLayer('a', defaults={'p': 1})
        b = DataLayer('b')
        b.retract_default('p')
        graph = ScenarioGraph()
        graph.add_layer('a', a)
        graph.add_layer('b', b)
        graph.add_layer('c', DataLayer('c'), parents=['a', 'b'])
        with self.assertRaises(ScenarioConflictError):
            graph.resolve('c')

    def test_child_default_retraction_beats_parent_default(self):
        a = DataLayer('a', defaults={'p': 1})
        b = DataLayer('b')
        b.retract_default('p')
        graph = ScenarioGraph()
        graph.add_layer('a', a)
        graph.add_layer('b', b, parents=['a'])
        graph.add_layer('c', DataLayer('c', params={'p': {1: 2}}), parents=['b'])
        resolved = graph.resolve('c')
        self.assertEqual(dict(resolved.data['p']), {1: 2})

    def test_index_set_is_resolved_per_scenario(self):
        base = DataLayer('base', indices={'p': [1, 2]}, defaults={'p': 0})
        sibling = DataLayer('sibling', indices={'p': [1, 2, 3]})
        child = DataLayer('child')
        graph = ScenarioGraph()
        graph.add_layer('base', base)
        graph.add_layer('sibling', sibling, parents=['base'])
        graph.add_layer('child', child, parents=['base'])
        self.assertEqual(set(graph.resolve('child').data['p']), {1, 2})
        self.assertEqual(set(graph.resolve('sibling').data['p']), {1, 2, 3})

    def test_topology_change_rebuilds_only_affected(self):
        base = DataLayer('base', params={'x': {None: 1}})
        a = DataLayer('a', params={'y': {None: 2}})
        b = DataLayer('b', params={'z': {None: 3}})
        graph = ScenarioGraph()
        graph.add_layer('base', base)
        graph.add_layer('a', a)
        graph.add_layer('b', b)
        runtime = ScenarioRuntime(graph)
        runtime.load_all()
        builds = {name: runtime.record(name).builds for name in graph}
        # Rewire 'a' to inherit from 'base'.
        graph.add_edge('a', 'base')
        runtime.refresh()
        self.assertGreater(runtime.record('a').builds, builds['a'])
        self.assertEqual(runtime.record('b').builds, builds['b'])
        self.assertIn('x', runtime.record('a').resolved.data)

    def test_invalid_index_isolated_to_one_scenario(self):
        model = pyo.AbstractModel()
        model.J = pyo.Set()
        model.c = pyo.Param(model.J)
        base = DataLayer('base', sets={'J': [1, 2]}, params={'c': {1: 1, 2: 2}})
        good = DataLayer('good', params={'c': {1: 5}})
        bad = DataLayer('bad', params={'c': {3: 5}})
        graph = ScenarioGraph({'base': base})
        graph.add_layer('good', good, parents=['base'])
        graph.add_layer('bad', bad, parents=['base'])
        runtime = ScenarioRuntime(graph, model=model, validate=True)
        runtime.load_all()
        self.assertIsNone(runtime.record('good').error)
        self.assertIsNotNone(runtime.record('bad').error)
        instances, errors = runtime.publish_all()
        self.assertEqual(set(instances), {'base', 'good'})
        self.assertIn('bad', errors)


if __name__ == '__main__':
    unittest.main()
