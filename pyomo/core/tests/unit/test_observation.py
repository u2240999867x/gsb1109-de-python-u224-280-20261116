# ____________________________________________________________________________________
#
# Pyomo: Python Optimization Modeling Objects
# Copyright (c) 2008-2026 National Technology and Engineering Solutions of Sandia, LLC
# Under the terms of Contract DE-NA0003525 with National Technology and Engineering
# Solutions of Sandia, LLC, the U.S. Government retains certain rights in this
# software.  This software is distributed under the 3-clause BSD License.
# ____________________________________________________________________________________

#
# Unit Tests for sparse Param observation provenance
#
# These tests verify that the explicit observations, default-rule values,
# and indices still lacking collection evidence can be queried separately,
# and that the answers are computed from the live rules rather than from the
# materialized parameter data.
#

import pickle

import pyomo.common.unittest as unittest

from pyomo.environ import (
    AbstractModel,
    Any,
    ConcreteModel,
    NonNegativeReals,
    Param,
    Reference,
    Set,
    ValueOrigin,
    value,
)
from pyomo.core.base.observation import ParamProvenance


class TestValueOrigin(unittest.TestCase):
    def test_enum_values(self):
        self.assertEqual(ValueOrigin.EXPLICIT.value, 'explicit')
        self.assertEqual(ValueOrigin.DEFAULT.value, 'default')
        self.assertEqual(ValueOrigin.MISSING.value, 'missing')


class TestProvenanceClassification(unittest.TestCase):
    def _model(self, **kwds):
        m = ConcreteModel()
        m.I = Set(initialize=[1, 2, 3, 4])
        m.p = Param(m.I, initialize={1: 0, 2: 5}, default=9, **kwds)
        return m

    def test_three_way_partition(self):
        m = self._model()
        self.assertEqual(
            [m.p.value_origin(i) for i in m.I],
            [
                ValueOrigin.EXPLICIT,
                ValueOrigin.EXPLICIT,
                ValueOrigin.DEFAULT,
                ValueOrigin.DEFAULT,
            ],
        )
        self.assertEqual(m.p.explicit_observations(), {1: 0, 2: 5})
        self.assertEqual(m.p.default_values(), {3: 9, 4: 9})
        self.assertEqual(m.p.missing_indices(), set())
        report = m.p.provenance_report()
        self.assertEqual(set(report), set(ValueOrigin))
        self.assertEqual(report[ValueOrigin.EXPLICIT], {1: 0, 2: 5})
        self.assertEqual(report[ValueOrigin.DEFAULT], {3: 9, 4: 9})
        self.assertEqual(report[ValueOrigin.MISSING], set())

    def test_explicit_zero_is_not_default(self):
        m = self._model()
        # The explicit observation of zero must remain explicit even
        # though zero is falsy and the default is a nonzero constant.
        self.assertEqual(m.p.value_origin(1), ValueOrigin.EXPLICIT)
        m2 = ConcreteModel()
        m2.I = Set(initialize=[1, 2])
        m2.p = Param(m2.I, initialize={1: 0}, default=0)
        # Even when the observed value equals the default value...
        self.assertEqual(m2.p[1], m2.p[2])
        self.assertEqual(m2.p.value_origin(1), ValueOrigin.EXPLICIT)
        self.assertEqual(m2.p.value_origin(2), ValueOrigin.DEFAULT)
        self.assertEqual(m2.p.explicit_observations(), {1: 0})

    def test_missing_without_default(self):
        m = ConcreteModel()
        m.I = Set(initialize=['a', 'b'])
        m.p = Param(m.I, initialize={'a': 1})
        self.assertEqual(m.p.missing_indices(), {'b'})
        self.assertEqual(m.p.value_origin('b'), ValueOrigin.MISSING)
        self.assertEqual(m.p.default_values(), {})
        report = m.p.provenance_report()
        self.assertEqual(report[ValueOrigin.MISSING], {'b'})
        self.assertEqual(report[ValueOrigin.DEFAULT], {})

    def test_materialized_mutable_default_stays_default(self):
        m = ConcreteModel()
        m.I = Set(initialize=['a', 'b', 'c'])
        m.p = Param(
            m.I,
            initialize={'a': 0},
            default=lambda m, i: 10 * len(i),
            mutable=True,
        )
        # Accessing an index injects the default into _data
        self.assertEqual(value(m.p['b']), 10)
        self.assertIn('b', m.p._data)
        # ... but the provenance must still classify it as a default.
        self.assertEqual(m.p.value_origin('b'), ValueOrigin.DEFAULT)
        self.assertEqual(m.p.explicit_observations(), {'a': 0})
        self.assertEqual(m.p.default_values(), {'b': 10, 'c': 10})

    def test_dense_initialization_does_not_create_observations(self):
        m = ConcreteModel()
        m.I = Set(initialize=[1, 2, 3])
        m.p = Param(m.I, default=9, mutable=True, initialize_as_dense=True)
        self.assertEqual(len(m.p._data), 3)
        self.assertEqual(m.p.explicit_observations(), {})
        self.assertEqual(
            {m.p.value_origin(i) for i in m.I}, {ValueOrigin.DEFAULT}
        )
        m.p.to_dense_data()
        self.assertEqual(m.p.explicit_observations(), {})

    def test_index_dependent_default_evaluated_live(self):
        m = ConcreteModel()
        m.I = Set(initialize=[1, 2, 3])
        m.factor = Param(initialize=2, mutable=True)
        m.p = Param(m.I, initialize={1: 100}, default=lambda m, i: m.factor * i)
        self.assertEqual(m.p.default_values(), {2: 4, 3: 6})
        # Correcting an upstream input changes the produced default values;
        # the observation at index 1 is untouched.
        m.factor.set_value(3)
        self.assertEqual(m.p.default_values(), {2: 6, 3: 9})
        self.assertEqual(m.p.explicit_observations(), {1: 100})

    def test_index_set_growth_and_shrink(self):
        m = ConcreteModel()
        m.I = Set(initialize=[1, 2])
        m.p = Param(m.I, initialize={1: 0}, default=9, mutable=True)
        m.I.add(3)
        self.assertEqual(m.p.value_origin(3), ValueOrigin.DEFAULT)
        self.assertEqual(m.p.default_values(), {2: 9, 3: 9})

        m2 = ConcreteModel()
        m2.I = Set(initialize=[1])
        m2.q = Param(m2.I, initialize={1: 1}, mutable=True)
        m2.I.add(2)
        self.assertEqual(m2.q.missing_indices(), {2})

        m3 = ConcreteModel()
        m3.I = Set(initialize=[1, 2])
        m3.r = Param(m3.I, initialize={1: 1, 2: 2})
        m3.I.remove(2)
        # The report is partitioned over the current index set...
        report = m3.r.provenance_report()
        self.assertEqual(report[ValueOrigin.EXPLICIT], {1: 1})
        self.assertEqual(report[ValueOrigin.MISSING], set())
        # ...while the full evidence history remains queryable.
        self.assertEqual(m3.r.explicit_observations(), {1: 1, 2: 2})

    def test_query_order_does_not_change_results(self):
        m = ConcreteModel()
        m.I = Set(initialize=[1, 2, 3])
        m.p = Param(m.I, initialize={1: 0}, default=9, mutable=True)
        # Query defaults before touching explicit observations, and force
        # default materialization in between.
        first_defaults = m.p.default_values()
        _ = m.p[2]
        first_report = m.p.provenance_report()
        self.assertEqual(first_defaults, {2: 9, 3: 9})
        self.assertEqual(m.p.explicit_observations(), {1: 0})
        self.assertEqual(
            first_report[ValueOrigin.DEFAULT], m.p.default_values()
        )
        self.assertEqual(m.p.value_origin(2), ValueOrigin.DEFAULT)
        self.assertEqual(m.p.value_origin(1), ValueOrigin.EXPLICIT)


class TestDefaultRuleChanges(unittest.TestCase):
    def test_change_default_does_not_rewrite_observations(self):
        m = ConcreteModel()
        m.I = Set(initialize=[1, 2])
        m.p = Param(m.I, initialize={1: 7}, default=8)
        m.p.set_default(80)
        self.assertEqual(m.p.explicit_observations(), {1: 7})
        self.assertEqual(m.p.default_values(), {2: 80})
        self.assertEqual(m.p[1], 7)
        self.assertEqual(m.p[2], 80)

    def test_affected_indices_constant_rules(self):
        m = ConcreteModel()
        m.I = Set(initialize=[1, 2, 3])
        m.p = Param(m.I, initialize={1: 100}, default=4)
        affected, unverifiable = m.p.indices_affected_by_default_change(40)
        self.assertEqual(affected, {2, 3})
        self.assertEqual(unverifiable, set())
        # An identical rule affects nothing.
        affected, _ = m.p.indices_affected_by_default_change(4)
        self.assertEqual(affected, set())

    def test_affected_uses_produced_values_not_materialized_dict(self):
        m = ConcreteModel()
        m.I = Set(initialize=[1, 2, 3])
        m.factor = Param(initialize=2, mutable=True)
        m.p = Param(m.I, initialize={1: 100}, default=lambda m, i: m.factor * i)
        # Correct the upstream parameter after construction: the impact
        # analysis must evaluate both rules against the corrected state.
        m.factor.set_value(3)
        affected, unverifiable = m.p.indices_affected_by_default_change(
            lambda m, i: 5 * i
        )
        self.assertEqual(affected, {2, 3})
        self.assertEqual(unverifiable, set())

    def test_affected_skips_explicit_observations(self):
        m = ConcreteModel()
        m.I = Set(initialize=[1, 2])
        m.p = Param(m.I, initialize={1: 0, 2: 0}, default=9)
        affected, _ = m.p.indices_affected_by_default_change(90)
        self.assertEqual(affected, set())

    def test_unverifiable_scope_when_rule_fails(self):
        m = ConcreteModel()
        m.I = Set(initialize=[0, 1, 2])
        m.p = Param(m.I, default=1)

        def bad_rule(model, i):
            if i == 0:
                raise ZeroDivisionError
            return i * 10

        affected, unverifiable = m.p.indices_affected_by_default_change(bad_rule)
        self.assertEqual(unverifiable, {0})
        self.assertEqual(affected, {1, 2})

    def test_mapping_candidate_default(self):
        m = ConcreteModel()
        m.I = Set(initialize=[1, 2, 3])
        m.p = Param(m.I, initialize={1: 10}, default=0)
        affected, unverifiable = m.p.indices_affected_by_default_change(
            {2: 20, 3: 30}
        )
        self.assertEqual(affected, {2, 3})
        self.assertEqual(unverifiable, set())
        m.p.set_default({2: 20, 3: 30})
        self.assertEqual(m.p.default_values(), {2: 20, 3: 30})
        self.assertEqual(m.p.explicit_observations(), {1: 10})

    def test_mapping_candidate_missing_keys_is_unverifiable(self):
        m = ConcreteModel()
        m.I = Set(initialize=[1, 2, 3])
        m.p = Param(m.I, default=1)
        affected, unverifiable = m.p.indices_affected_by_default_change({2: 20})
        self.assertEqual(affected, {2})
        self.assertEqual(unverifiable, {1, 3})

    def test_removing_default_marks_missing(self):
        m = ConcreteModel()
        m.I = Set(initialize=[1, 2, 3])
        m.p = Param(m.I, initialize={1: 0}, default=4)
        affected, unverifiable = m.p.indices_affected_by_default_change(
            Param.NoValue
        )
        self.assertEqual(affected, {2, 3})
        self.assertEqual(unverifiable, set())
        m.p.set_default(Param.NoValue)
        self.assertIs(m.p.default(), Param.NoValue)
        self.assertEqual(m.p.missing_indices(), {2, 3})
        self.assertEqual(m.p.explicit_observations(), {1: 0})


class TestObservationWrites(unittest.TestCase):
    def test_explicit_write_marks_observation(self):
        m = ConcreteModel()
        m.I = Set(initialize=[1, 2])
        m.p = Param(m.I, default=9, mutable=True)
        m.p[1] = 0
        self.assertEqual(m.p.value_origin(1), ValueOrigin.EXPLICIT)
        self.assertEqual(m.p.explicit_observations(), {1: 0})
        self.assertEqual(m.p.value_origin(2), ValueOrigin.DEFAULT)

    def test_failed_validation_does_not_record_observation(self):
        m = ConcreteModel()
        m.I = Set(initialize=[1, 2])
        m.p = Param(m.I, within=NonNegativeReals, mutable=True, default=1)
        with self.assertRaises(ValueError):
            m.p[1] = -3
        self.assertEqual(m.p.value_origin(1), ValueOrigin.DEFAULT)
        self.assertEqual(m.p.explicit_observations(), {})
        m.p[1] = 3
        self.assertEqual(m.p.value_origin(1), ValueOrigin.EXPLICIT)

    def test_delete_withdraws_observation(self):
        m = ConcreteModel()
        m.I = Set(initialize=[1, 2])
        m.p = Param(m.I, initialize={1: 1, 2: 2}, mutable=True)
        del m.p[2]
        self.assertEqual(m.p.value_origin(2), ValueOrigin.MISSING)
        m.p[2] = 9
        self.assertEqual(m.p.value_origin(2), ValueOrigin.EXPLICIT)
        self.assertEqual(m.p.explicit_observations(), {1: 1, 2: 9})

    def test_clear_resets_provenance(self):
        m = ConcreteModel()
        m.I = Set(initialize=[1, 2])
        m.p = Param(m.I, initialize={1: 1}, mutable=True)
        m.p.clear()
        self.assertEqual(m.p.explicit_observations(), {})
        self.assertEqual(m.p.value_origin(1), ValueOrigin.MISSING)

    def test_store_values_records_observations(self):
        m = ConcreteModel()
        m.I = Set(initialize=[1, 2, 3])
        m.p = Param(m.I, mutable=True, default=1)
        m.p.store_values({1: 11, 2: 22}, check=False)
        self.assertEqual(m.p.explicit_observations(), {1: 11, 2: 22})
        self.assertEqual(m.p.value_origin(3), ValueOrigin.DEFAULT)
        m.p.store_values(7, check=False)
        self.assertEqual(m.p.explicit_observations(), {1: 7, 2: 7, 3: 7})

    def test_abstract_model_data_loading(self):
        model = AbstractModel()
        model.I = Set()
        model.p = Param(model.I, default=7)
        instance = model.create_instance(
            {None: {'I': {None: [1, 2, 3]}, 'p': {1: 10, 3: 30}}}
        )
        self.assertEqual(instance.p.explicit_observations(), {1: 10, 3: 30})
        self.assertEqual(instance.p.default_values(), {2: 7})
        self.assertEqual(instance.p.value_origin(2), ValueOrigin.DEFAULT)


class TestScalarParam(unittest.TestCase):
    def test_explicit_scalar_zero(self):
        m = ConcreteModel()
        m.z = Param(initialize=0)
        self.assertEqual(m.z.explicit_observations(), {None: 0})
        self.assertEqual(m.z.value_origin(None), ValueOrigin.EXPLICIT)

    def test_default_scalar(self):
        m = ConcreteModel()
        m.z = Param(default=3)
        self.assertEqual(m.z.explicit_observations(), {})
        self.assertEqual(m.z.default_values(), {None: 3})
        self.assertEqual(m.z.value_origin(None), ValueOrigin.DEFAULT)

    def test_missing_mutable_scalar(self):
        m = ConcreteModel()
        m.z = Param(mutable=True)
        self.assertEqual(m.z.missing_indices(), {None})
        self.assertEqual(m.z.value_origin(None), ValueOrigin.MISSING)
        m.z.set_value(0)
        self.assertEqual(m.z.value_origin(None), ValueOrigin.EXPLICIT)


class TestProvenanceObject(unittest.TestCase):
    def test_index_set_accessors(self):
        m = ConcreteModel()
        m.I = Set(initialize=[1, 2, 3])
        m.p = Param(m.I, initialize={1: 0}, default=9)
        provenance = m.p.observation_provenance()
        self.assertIsInstance(provenance, ParamProvenance)
        self.assertEqual(provenance.explicit_indices(), {1})
        self.assertTrue(provenance.is_explicit(1))
        self.assertFalse(provenance.is_explicit(2))
        self.assertEqual(provenance.defaulted_indices(), {2, 3})
        self.assertEqual(provenance.missing_indices(), set())
        self.assertEqual(provenance.default_value(2), 9)

    def test_default_value_without_rule(self):
        m = ConcreteModel()
        m.I = Set(initialize=[1])
        m.p = Param(m.I)
        with self.assertRaises(KeyError):
            m.p.observation_provenance().default_value(1)

    def test_infinite_index_set(self):
        m = ConcreteModel()
        m.p = Param(Any, default=lambda m, i: 2 * i, mutable=True)
        m.p[1] = 10
        self.assertEqual(m.p.value_origin(1), ValueOrigin.EXPLICIT)
        self.assertEqual(m.p.value_origin(99), ValueOrigin.DEFAULT)
        self.assertEqual(m.p.observation_provenance().default_value(7), 14)
        self.assertEqual(m.p.explicit_observations(), {1: 10})
        for method in (
            m.p.default_values,
            m.p.missing_indices,
            m.p.provenance_report,
        ):
            with self.assertRaises(ValueError):
                method()
        with self.assertRaises(ValueError):
            m.p.indices_affected_by_default_change(1)


class TestReferenceParam(unittest.TestCase):
    def test_provenance_delegates_to_referent(self):
        m = ConcreteModel()
        m.I = Set(initialize=[1, 2])
        m.q = Param(m.I, initialize={1: 0}, default=5, mutable=True)
        m.r = Reference(m.q)
        self.assertTrue(m.r.is_reference())
        self.assertIs(m.r.observation_provenance(), m.q.observation_provenance())
        self.assertEqual(m.r.explicit_observations(), {1: 0})
        self.assertEqual(m.r.value_origin(1), ValueOrigin.EXPLICIT)
        self.assertEqual(m.r.value_origin(2), ValueOrigin.DEFAULT)
        self.assertEqual(m.r.default_values(), {2: 5})
        # A write on the underlying mutable Param is reported through
        # the reference without replaying any history.
        m.q[2] = 8
        self.assertEqual(m.r.explicit_observations(), {1: 0, 2: 8})
        self.assertEqual(m.r.value_origin(2), ValueOrigin.EXPLICIT)

    def test_deletion_through_reference_updates_referent(self):
        m = ConcreteModel()
        m.I = Set(initialize=[1, 2])
        m.q = Param(m.I, initialize={1: 10, 2: 20}, mutable=True)
        m.r = Reference(m.q)
        del m.r[2]
        self.assertEqual(m.q.value_origin(2), ValueOrigin.MISSING)
        self.assertEqual(m.r.value_origin(2), ValueOrigin.MISSING)
        self.assertEqual(m.q.explicit_observations(), {1: 10})


class TestPersistence(unittest.TestCase):
    def _model(self):
        m = ConcreteModel()
        m.I = Set(initialize=[1, 2, 3])
        m.p = Param(m.I, initialize={1: 0, 2: 5}, default=9, mutable=True)
        _ = m.p[3]
        return m

    def test_clone_preserves_provenance(self):
        original = self._model()
        clone = original.clone()
        self.assertEqual(clone.p.explicit_observations(), {1: 0, 2: 5})
        self.assertEqual(clone.p.value_origin(3), ValueOrigin.DEFAULT)
        self.assertEqual(clone.p.default_values(), {3: 9})
        self.assertIsNot(clone.p._provenance, original.p._provenance)
        self.assertIs(clone.p.observation_provenance()._param, clone.p)

    def test_pickle_roundtrip(self):
        restored = pickle.loads(pickle.dumps(self._model()))
        self.assertEqual(restored.p.explicit_observations(), {1: 0, 2: 5})
        self.assertEqual(restored.p.value_origin(3), ValueOrigin.DEFAULT)
        self.assertIs(restored.p.observation_provenance()._param, restored.p)


if __name__ == "__main__":
    unittest.main()
