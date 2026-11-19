# ____________________________________________________________________________________
#
# Pyomo: Python Optimization Modeling Objects
# Copyright (c) 2008-2026 National Technology and Engineering Solutions of Sandia, LLC
# Under the terms of Contract DE-NA0003525 with National Technology and Engineering
# Solutions of Sandia, LLC, the U.S. Government retains certain rights in this
# software.  This software is distributed under the 3-clause BSD License.
# ____________________________________________________________________________________
#
# Unit Tests for observation coverage declarations
#

import pyomo.common.unittest as unittest

import pyomo.environ as pyo

from pyomo.dataportal.coverage import (
    CoverageDeclaration,
    Observation,
    ObservationDomain,
    ObservationKind,
    observation,
)


class TestCoverageDeclaration(unittest.TestCase):
    def test_range_selects_only_real_members(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[1, 2, 3, 5])
        decl = CoverageDeclaration(m.I, ranges={0: (2, 4)})
        result = decl.evaluate()
        self.assertEqual(result.covered, (2, 3))
        self.assertEqual(result.unknown, ())

    def test_range_does_not_invent_members(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[1, 5])
        decl = CoverageDeclaration(m.I, ranges=(2, 4))
        self.assertEqual(decl.evaluate().covered, ())

    def test_explicit_combinations_are_not_crossed(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[1, 2])
        m.J = pyo.Set(initialize=['a', 'b'])
        m.IJ = m.I * m.J
        # Two explicit combinations, not the full 2x2 cross product.
        decl = CoverageDeclaration(m.IJ, include=[(1, 'a'), (2, 'b')])
        self.assertEqual(decl.evaluate().covered, ((1, 'a'), (2, 'b')))

    def test_per_dimension_members_are_not_crossed(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[1, 2])
        m.J = pyo.Set(initialize=['a', 'b'])
        m.IJ = pyo.Set(initialize=[(1, 'a'), (2, 'b')], dimen=2)
        decl = CoverageDeclaration(m.IJ, include=[(1, 'a')])
        self.assertEqual(decl.evaluate().covered, ((1, 'a'),))
        # (2, 'b') is in the real set, but was not declared covered.
        self.assertNotIn((2, 'b'), decl)

    def test_exclusion(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[1, 2, 3])
        decl = CoverageDeclaration(m.I, ranges={0: (1, 3)}, exclude=[2])
        result = decl.evaluate()
        self.assertEqual(result.covered, (1, 3))
        self.assertEqual(result.excluded, (2,))

    def test_range_exclusion(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[1, 2, 3, 4, 5])
        decl = CoverageDeclaration(m.I, ranges={0: (1, 5)}, exclude=[(0, 2, 4)])
        self.assertEqual(decl.evaluate().covered, (1, 5))

    def test_unknown_member_is_reported(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[1, 2, 3])
        decl = CoverageDeclaration(m.I, include=[2, 99])
        with self.assertLogs('pyomo.dataportal', level='WARNING'):
            result = decl.evaluate()
        self.assertEqual(result.covered, (2,))
        self.assertEqual(result.unknown, (99,))

    def test_overlapping_declarations(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[1, 2, 3, 4])
        decl = CoverageDeclaration(
            m.I, ranges=[(0, 1, 3), (0, 3, 4)], include=[1]
        )
        covered = decl.evaluate().covered
        self.assertEqual(covered, (1, 2, 3, 4))

    def test_two_dimensional_ranges_are_conjunctive(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[1, 2])
        m.J = pyo.Set(initialize=['a', 'b'])
        m.IJ = m.I * m.J
        decl = CoverageDeclaration(m.IJ, ranges={0: (1, 1), 1: ('b', 'b')})
        self.assertEqual(decl.evaluate().covered, ((1, 'b'),))

    def test_ranges_on_explicit_multidim_set(self):
        m = pyo.ConcreteModel()
        m.IJ = pyo.Set(initialize=[(1, 'a'), (2, 'b'), (3, 'c')], dimen=2)
        decl = CoverageDeclaration(m.IJ, ranges={0: (1, 2), 1: ('a', 'b')})
        self.assertEqual(decl.evaluate().covered, ((1, 'a'), (2, 'b')))
        decl = CoverageDeclaration(m.IJ, ranges=[(0, 2, 3), (1, 'c', 'c')])
        self.assertEqual(decl.evaluate().covered, ((2, 'b'), (3, 'c')))

    def test_multidim_exclusion_by_combination(self):
        m = pyo.ConcreteModel()
        m.IJ = pyo.Set(initialize=[(1, 'a'), (1, 'b'), (2, 'a')], dimen=2)
        decl = CoverageDeclaration(
            m.IJ,
            ranges={0: (1, 2)},
            exclude=[(1, 'b')],
        )
        result = decl.evaluate()
        self.assertEqual(result.covered, ((1, 'a'), (2, 'a')))
        self.assertEqual(result.excluded, ((1, 'b'),))

    def test_multidim_dimension_exclusion(self):
        m = pyo.ConcreteModel()
        m.IJ = pyo.Set(initialize=[(1, 'a'), (1, 'b'), (2, 'a')], dimen=2)
        decl = CoverageDeclaration(
            m.IJ,
            ranges={0: (1, 2)},
            exclude=[(0, 1, 1)],
        )
        result = decl.evaluate()
        self.assertEqual(result.covered, ((2, 'a'),))
        self.assertEqual(result.excluded, ((1, 'a'), (1, 'b')))

    def test_bare_range_requires_dimension_on_multidim_set(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[1, 2])
        m.J = pyo.Set(initialize=['a', 'b'])
        m.IJ = m.I * m.J
        with self.assertRaisesRegex(ValueError, 'requires an explicit dimension'):
            CoverageDeclaration(m.IJ, ranges=(1, 2))

    def test_noncontiguous_numeric_component(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[1, 3, 5, 7])
        decl = CoverageDeclaration(m.I, ranges=(2, 6))
        self.assertEqual(decl.evaluate().covered, (3, 5))

    def test_single_combination_tuple(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[1, 2])
        m.J = pyo.Set(initialize=['a', 'b'])
        m.IJ = m.I * m.J
        decl = CoverageDeclaration(m.IJ, include=(1, 'a'))
        self.assertEqual(decl.evaluate().covered, ((1, 'a'),))
        decl = CoverageDeclaration(m.IJ, ranges={0: (1, 2)}, exclude=(1, 'a'))
        self.assertEqual(decl.evaluate().excluded, ((1, 'a'),))

    def test_index_set_can_be_a_param(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[1, 2])
        m.p = pyo.Param(m.I, initialize={1: 10})
        decl = CoverageDeclaration(m.p, include=[1])
        self.assertIs(decl.index_set, m.I)
        self.assertEqual(decl.evaluate().covered, (1,))

    def test_non_set_index_is_rejected(self):
        with self.assertRaisesRegex(TypeError, 'Pyomo Set or Param'):
            CoverageDeclaration([1, 2, 3])


class TestObservation(unittest.TestCase):
    def _model(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[1, 2, 3])
        m.explicit = pyo.Param(m.I, initialize={1: 10, 3: 30})
        m.with_default = pyo.Param(m.I, initialize={1: 10}, default=7)
        m.mutable_default = pyo.Param(
            m.I, initialize={1: 10}, default=7, mutable=True
        )
        return m

    def test_original_and_missing(self):
        m = self._model()
        self.assertEqual(
            observation(m.explicit, 1), Observation(ObservationKind.ORIGINAL, 10)
        )
        self.assertEqual(
            observation(m.explicit, 2), Observation(ObservationKind.MISSING, None)
        )

    def test_default_is_distinct_from_missing(self):
        m = self._model()
        self.assertEqual(
            observation(m.with_default, 2), Observation(ObservationKind.DEFAULT, 7)
        )
        self.assertEqual(
            observation(m.explicit, 2), Observation(ObservationKind.MISSING, None)
        )

    def test_mutable_default_is_not_materialized(self):
        m = self._model()
        before = dict(m.mutable_default._data)
        self.assertEqual(
            observation(m.mutable_default, 2),
            Observation(ObservationKind.DEFAULT, 7),
        )
        self.assertEqual(dict(m.mutable_default._data), before)

    def test_missing_is_not_zero(self):
        m = self._model()
        result = observation(m.explicit, 2)
        self.assertIsNone(result.value)
        self.assertIsNot(result.value, 0)
        self.assertIs(result.kind, ObservationKind.MISSING)

    def test_rule_initialized_values_are_original(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[1, 2, 3])
        m.p = pyo.Param(m.I, initialize=lambda m, i: i * 10)
        for i in (1, 2, 3):
            self.assertIs(observation(m.p, i).kind, ObservationKind.ORIGINAL)
            self.assertEqual(observation(m.p, i).value, i * 10)

    def test_scalar_param(self):
        m = pyo.ConcreteModel()
        m.p = pyo.Param(initialize=5)
        self.assertEqual(
            observation(m.p, None), Observation(ObservationKind.ORIGINAL, 5)
        )
        with self.assertRaises(KeyError):
            observation(m.p, 1)

    def test_rejects_index_outside_set(self):
        m = self._model()
        with self.assertRaises(KeyError):
            observation(m.explicit, 4)


class TestObservationDomain(unittest.TestCase):
    def _domain(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[1, 2, 3, 4])
        m.a = pyo.Param(m.I, initialize={1: 10, 2: 20, 3: 30})
        m.b = pyo.Param(m.I, initialize={1: 5, 2: 6, 4: 8})
        domain = ObservationDomain(m.I)
        domain.declare(m.a, ranges=(1, 3))
        domain.declare(m.b, include=[1, 2, 4])
        return domain

    def test_common_domain_requires_declaration_and_value(self):
        domain = self._domain()
        # 1 and 2 are declared and observed by both; 3 is declared but
        # b has no value; 4 is observed by b but not covered by a.
        self.assertEqual(list(domain.common_domain()), [1, 2])
        self.assertEqual(list(domain.common_domain(require_original=True)), [1, 2])

    def test_common_domain_keeps_index_set_order(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[3, 1, 2])
        m.a = pyo.Param(m.I, initialize={1: 1, 2: 2, 3: 3})
        m.b = pyo.Param(m.I, initialize={1: 1, 2: 2, 3: 3})
        domain = ObservationDomain(m.I, params=[m.a, m.b])
        domain.declare(m.a, include=[2, 3, 1])
        domain.declare(m.b, include=[2, 3, 1])
        self.assertEqual(list(domain.common_domain()), [3, 1, 2])

    def test_default_counts_as_observation(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[1, 2])
        m.a = pyo.Param(m.I, initialize={1: 1}, default=0)
        m.b = pyo.Param(m.I, initialize={1: 1, 2: 2})
        domain = ObservationDomain(m.I, params=[m.a, m.b])
        self.assertEqual(list(domain.common_domain()), [1, 2])
        self.assertEqual(list(domain.observed_domain()), [1])

    def test_missing_observation_is_explained(self):
        domain = self._domain()
        report = domain.explain()
        self.assertTrue(report)
        self.assertTrue(report.can_compute)
        self.assertEqual(report.domain, (1, 2))

    def test_explain_when_no_common_domain(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[1, 2])
        m.a = pyo.Param(m.I, initialize={1: 1})
        m.b = pyo.Param(m.I, initialize={2: 2})
        domain = ObservationDomain(m.I)
        domain.declare(m.a, include=[1])
        domain.declare(m.b, include=[2])
        report = domain.explain()
        self.assertFalse(report)
        self.assertFalse(report.can_compute)
        self.assertIn('no usable', report.reason)

    def test_missing_report_names_gap(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[1, 2])
        m.a = pyo.Param(m.I, initialize={1: 1})
        domain = ObservationDomain(m.I)
        domain.declare(m.a, include=[1, 2])
        self.assertEqual(domain.missing(m.a), (2,))
        report = domain.explain()
        self.assertTrue(report)
        self.assertEqual(report.domain, (1,))
        self.assertIn('missing', report.reason)
        self.assertEqual(report.missing, {'a': (2,)})

    def test_declaration_for_unknown_member_is_explained(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[1, 2])
        m.a = pyo.Param(m.I, initialize={1: 1, 2: 2})
        domain = ObservationDomain(m.I)
        domain.declare(m.a, include=[1, 9])
        with self.assertLogs('pyomo.dataportal', level='WARNING'):
            report = domain.explain()
        self.assertTrue(report)
        self.assertEqual(report.unknown, (9,))

    def test_require_original_excludes_defaults(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[1, 2])
        m.a = pyo.Param(m.I, initialize={1: 1}, default=0)
        domain = ObservationDomain(m.I, params=[m.a])
        self.assertEqual(list(domain.common_domain()), [1, 2])
        self.assertEqual(list(domain.common_domain(require_original=True)), [1])

    def test_no_parameters_raises(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[1, 2])
        domain = ObservationDomain(m.I)
        with self.assertRaisesRegex(ValueError, 'at least one parameter'):
            domain.common_domain()


class TestMigration(unittest.TestCase):
    def test_stable_identity_migration(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[1, 2, 3])
        decl = CoverageDeclaration(m.I, include=[1, 2])
        m.I2 = pyo.Set(initialize=[1, 2, 3, 4])
        result = decl.migrate(m.I2)
        self.assertEqual(result.migrated, (1, 2))
        self.assertEqual(result.added, (3, 4))
        self.assertEqual(result.declaration.evaluate().covered, (1, 2))

    def test_new_members_are_not_covered(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[1, 2])
        decl = CoverageDeclaration(m.I, ranges=(1, 2))
        m.I2 = pyo.Set(initialize=[1, 2, 3])
        result = decl.migrate(m.I2)
        self.assertEqual(result.declaration.evaluate().covered, (1, 2))
        self.assertNotIn(3, result.declaration)
        self.assertEqual(result.added, (3,))

    def test_dropped_member(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[1, 2, 3])
        decl = CoverageDeclaration(m.I, include=[1, 2, 3])
        m.I2 = pyo.Set(initialize=[1, 3])
        result = decl.migrate(m.I2)
        self.assertEqual(result.migrated, (1, 3))
        self.assertEqual(result.dropped, (2,))

    def test_explicit_identity(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=['a', 'b'])
        decl = CoverageDeclaration(m.I, include=['a'])
        m.I2 = pyo.Set(initialize=['A', 'B'])
        identity = {'a': 'A', 'b': 'B'}
        result = decl.migrate(m.I2, identity=identity)
        self.assertEqual(result.migrated, ('A',))
        self.assertEqual(result.declaration.evaluate().covered, ('A',))

    def test_unstable_identity_is_not_migrated(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[1, 2])
        decl = CoverageDeclaration(m.I, include=[1, 2])

        def identity(member):
            return None

        m.I2 = pyo.Set(initialize=[1, 2])
        result = decl.migrate(m.I2, identity=identity)
        self.assertEqual(result.migrated, ())
        self.assertEqual(result.dropped, (1, 2))
        self.assertEqual(result.declaration.evaluate().covered, ())


class TestDataPortalIntegration(unittest.TestCase):
    def test_portal_observation_domain(self):
        m = pyo.ConcreteModel()
        m.I = pyo.Set(initialize=[1, 2])
        m.p = pyo.Param(m.I, initialize={1: 10})
        portal = pyo.DataPortal()
        domain = portal.observation_domain(m.I, params=[m.p])
        domain.declare(m.p, include=[1, 2])
        self.assertEqual(list(domain.common_domain()), [1])
        self.assertEqual(domain.missing(m.p), (2,))


if __name__ == '__main__':
    unittest.main()
