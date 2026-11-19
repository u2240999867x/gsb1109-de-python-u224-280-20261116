# ____________________________________________________________________________________
#
# Pyomo: Python Optimization Modeling Objects
# Copyright (c) 2008-2026 National Technology and Engineering Solutions of Sandia, LLC
# Under the terms of Contract DE-NA0003525 with National Technology and Engineering
# Solutions of Sandia, LLC, the U.S. Government retains certain rights in this
# software.  This software is distributed under the 3-clause BSD License.
# ____________________________________________________________________________________

"""Provenance tracking for sparse parameter observations.

A :class:`~pyomo.core.base.param.Param` combines three things:

  * explicit observations supplied through ``initialize`` / data files /
    item assignment,
  * a default rule (a constant or an index-dependent callable) that fills
    in indices that were never observed, and
  * indices for which neither an observation nor a usable default exists.

The materialized component data (``_data``) cannot distinguish these
sources.  In particular, mutable Params inject default values into
``_data`` when an index is first accessed, and explicit observations can
legitimately be zero.  This module maintains a separate record of where
every value actually came from, independent of the materialized values.

The record is write-only with respect to the default rule: changing or
replacing the default rule never rewrites historical observations.
"""

from enum import Enum
from weakref import ref as weakref_ref

from pyomo.common.autoslots import AutoSlots
from pyomo.common.numeric_types import value as _value
from pyomo.core.expr.numvalue import NumericValue


def _evaluate(rule_output):
    """Reduce a rule output to a comparable Python value.

    Rules may return plain Python data (including tuples and strings for
    Params declared with domain ``Any``) or Pyomo expressions that
    reference other parameters.  Only :class:`NumericValue` objects are
    evaluated; other objects pass through unchanged.  Evaluation errors
    propagate to the caller, which records the index as unverifiable.
    """
    if isinstance(rule_output, NumericValue):
        return _value(rule_output)
    return rule_output


class ValueOrigin(str, Enum):
    """Where the value associated with a Param index came from.

    ``EXPLICIT``
        The index has an explicit observation (a value supplied through
        initialization, external data, or item assignment).  An explicit
        observation of zero is still ``EXPLICIT``.

    ``DEFAULT``
        There is no explicit observation for the index; the value is
        produced by the Param's default rule (a constant or an
        index-dependent callable).

    ``MISSING``
        There is neither an explicit observation nor a usable default
        rule, so the index still lacks collection evidence.
    """

    EXPLICIT = 'explicit'
    DEFAULT = 'default'
    MISSING = 'missing'


class ParamProvenance(AutoSlots.Mixin):
    """Record of the observation/default provenance for a single Param.

    The owning :class:`~pyomo.core.base.param.Param` updates this record
    whenever an explicit observation is written or removed.  The record
    never materializes default values itself; queries re-evaluate the
    current default rule on demand.  As a result, replacing the default
    rule changes the answers returned for defaulted indices without
    rewriting any historical observation.
    """

    __slots__ = ('_owner', '_observed')
    __autoslot_mappers__ = {'_owner': AutoSlots.weakref_mapper}

    def __init__(self, param):
        self._owner = weakref_ref(param)
        # Set of indices carrying an explicit observation.  This is the
        # only state persisted for the component; default values are
        # always derived from the live default rule.
        self._observed = set()

    @property
    def _param(self):
        return self._owner()

    def _mark_observed(self, index):
        self._observed.add(index)

    def _mark_missing_observation(self, index):
        self._observed.discard(index)

    def explicit_indices(self):
        """Return the set of indices with an explicit observation.

        Explicitly observed zeros are included; membership is based on
        the presence of collection evidence, not on the value.
        """
        return set(self._observed)

    def is_explicit(self, index):
        """Return True if ``index`` carries an explicit observation."""
        return index in self._observed

    def _finite_index_set(self):
        index_set = self._param.index_set()
        if not index_set.isfinite():
            raise ValueError(
                "Cannot enumerate provenance for Param '%s': its index set "
                "is not finite.  Query specific indices with "
                "value_origin() instead." % (self._param.name,)
            )
        return index_set

    def defaulted_indices(self):
        """Return the set of indices filled in by the default rule.

        An index is included when it has no explicit observation *and*
        the Param currently has a default rule that can supply a value.
        The result is computed against the live default rule and the
        live index set, not the materialized component data, so it
        remains correct after inputs or the default rule change.
        """
        index_set = self._finite_index_set()
        if self._param._default is None:
            return set()
        return set(index_set) - self._observed

    def missing_indices(self):
        """Return the set of indices still lacking collection evidence.

        These are indices with neither an explicit observation nor a
        default rule that can supply a value.  For a mutable Param with
        no default, this also covers indices whose materialized value is
        still the uninitialized ``NoValue`` sentinel.
        """
        index_set = self._finite_index_set()
        if self._param._default is not None:
            return set()
        return set(index_set) - self._observed

    def value_origin(self, index):
        """Return the :class:`ValueOrigin` of a single index.

        Explicit observations always win, even when the observed value
        equals the value that the default rule would produce (including
        an explicit zero).
        """
        if index in self._observed:
            return ValueOrigin.EXPLICIT
        if self._param._default is not None:
            return ValueOrigin.DEFAULT
        return ValueOrigin.MISSING

    def default_value(self, index):
        """Evaluate the current default rule for ``index``.

        Returns the value that the default rule produces without
        recording it as an observation.  Raises ``KeyError`` if the
        Param has no default rule, and propagates any exception raised
        by an index-dependent rule.
        """
        default = self._param._default
        if default is None:
            raise KeyError(
                "Param '%s' has no default rule for index %r"
                % (self._param.name, index)
            )
        return _evaluate(default(self._param.parent_block(), index))

    def explicit_observations(self):
        """Return a dict of explicitly observed ``{index: value}`` pairs.

        Values are read through the component so mutable Params report
        their current values; only indices that carry collection
        evidence are included (materialized defaults are not).  The full
        evidence record is returned, including indices that have since
        been removed from the index set; use :meth:`provenance` for a
        report strictly partitioned over the current index set.
        """
        param = self._param
        if param._mutable:
            return {index: param._data[index]() for index in self._observed}
        return {index: param._data[index] for index in self._observed}

    def default_values(self):
        """Return a dict mapping defaulted indices to their rule values.

        Only indices covered by the default rule and lacking an explicit
        observation are included; the rule is evaluated against the
        current model state.  The returned dict is a snapshot: later
        changes to the rule, the sets, or other parameters are not
        reflected in it.
        """
        param = self._param
        if param._default is None:
            return {}
        block = param.parent_block()
        rule = param._default
        return {
            index: _evaluate(rule(block, index))
            for index in set(self._finite_index_set()) - self._observed
        }

    def provenance(self):
        """Return the full three-way classification as a dict.

        The keys are the :class:`ValueOrigin` members.  ``EXPLICIT`` and
        ``DEFAULT`` map to ``{index: value}`` dictionaries, while
        ``MISSING`` maps to the set of indices without collection
        evidence.  The two value dictionaries partition the finite index
        set together with the missing set.
        """
        index_set = set(self._finite_index_set())
        observed = self._observed & index_set
        defaulted = index_set - observed
        all_observations = self.explicit_observations()
        observations = {idx: all_observations[idx] for idx in observed}
        if self._param._default is None:
            return {
                ValueOrigin.EXPLICIT: observations,
                ValueOrigin.DEFAULT: {},
                ValueOrigin.MISSING: defaulted,
            }
        block = self._param.parent_block()
        rule = self._param._default
        return {
            ValueOrigin.EXPLICIT: observations,
            ValueOrigin.DEFAULT: {
                index: _evaluate(rule(block, index)) for index in defaulted
            },
            ValueOrigin.MISSING: set(),
        }

    def affected_by_default_change(self, new_default):
        """Return the indices actually affected by replacing the default.

        ``new_default`` is anything accepted by
        :meth:`Param.set_default() <pyomo.core.base.param.Param.set_default>`
        (a constant, a callable, a mapping, or ``Param.NoValue``).

        Only indices without an explicit observation can be affected:
        historical observations are never rewritten.  Both the
        incumbent and the candidate rules are evaluated against the
        *current* model state (including sets and other parameters), so
        the result reflects real value changes rather than a comparison
        of materialized parameter dictionaries.  An index is included in
        ``unverifiable`` when one of the two rules cannot be evaluated
        for it; the caller cannot prove the dependency is complete for
        those indices and must audit them separately.
        """
        from pyomo.core.base.initializer import Initializer
        from pyomo.core.base.param import Param

        index_set = set(self._finite_index_set())
        candidate_indices = index_set - self._observed

        if new_default is Param.NoValue:
            candidate = None
        else:
            candidate = Initializer(new_default)
        incumbent = self._param._default

        block = self._param.parent_block()
        affected = set()
        unverifiable = set()
        for index in candidate_indices:
            try:
                if incumbent is None:
                    old_value = Param.NoValue
                else:
                    old_value = _evaluate(incumbent(block, index))
            except Exception:
                unverifiable.add(index)
                continue
            try:
                if candidate is None:
                    new_value = Param.NoValue
                else:
                    new_value = _evaluate(candidate(block, index))
            except Exception:
                unverifiable.add(index)
                continue
            if new_value != old_value:
                affected.add(index)
        return affected, unverifiable
