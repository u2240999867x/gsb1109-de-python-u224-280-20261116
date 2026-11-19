# ____________________________________________________________________________________
#
# Pyomo: Python Optimization Modeling Objects
# Copyright (c) 2008-2026 National Technology and Engineering Solutions of Sandia, LLC
# Under the terms of Contract DE-NA0003525 with National Technology and Engineering
# Solutions of Sandia, LLC, the U.S. Government retains certain rights in this
# software.  This software is distributed under the 3-clause BSD License.
# ____________________________________________________________________________________
"""Reasoning about the observation coverage of sparse parameter data.

A supplier may publish a sparse parameter together with a compact
statement of the indices it claims to cover.  That statement can be an
inclusive index range, a handful of explicit index combinations, or a
mixture of the two with exclusions.  This module evaluates those claims
against the **actual** Pyomo index sets so callers can identify the
common observation domain usable for a particular calculation.

The implementation enforces three invariants:

* Coverage is always intersected with the real index set.  A member
  named by a declaration that no longer exists is reported as unknown;
  it is never silently accepted.
* The members observed along individual dimensions are never crossed to
  form a Cartesian product.  The common domain is the intersection of
  real member sets.
* A missing observation is never represented by, or replaced with,
  zero.  Every index is classified as an original value, a model
  default, or a missing observation.

"""

from __future__ import annotations

import enum
import logging

from pyomo.common.collections import OrderedSet
from pyomo.core.base.param import Param

logger = logging.getLogger('pyomo.dataportal')


class ObservationKind(str, enum.Enum):
    """The provenance of the value available at a parameter index."""

    ORIGINAL = 'original'
    """An explicit value was supplied (by data or an initialization rule)."""

    DEFAULT = 'default'
    """No explicit value was supplied; the declared model default applies."""

    MISSING = 'missing'
    """No value is available.  This is not the same as a value of zero."""


class Observation:
    """The value available at a single parameter index.

    Attributes
    ----------
    kind : ObservationKind
    value : object or None
        The numeric value for ``ORIGINAL`` and ``DEFAULT``; ``None`` for
        ``MISSING``.
    """

    __slots__ = ('kind', 'value')

    def __init__(self, kind, value):
        self.kind = kind
        self.value = value

    def __iter__(self):
        return iter((self.kind, self.value))

    def __repr__(self):
        return "Observation(kind=%s, value=%r)" % (self.kind.value, self.value)

    def __eq__(self, other):
        if isinstance(other, Observation):
            return self.kind is other.kind and self.value == other.value
        return NotImplemented

    def __hash__(self):
        return hash((self.kind, self.value))


def _normalize_index(index):
    """Normalize an index to the canonical form used by Pyomo sets."""
    if index.__class__ is list:
        index = tuple(index)
    if index.__class__ is tuple and len(index) == 1:
        return index[0]
    return index


class CoverageResult:
    """The result of evaluating a declaration against a real index set."""

    __slots__ = ('covered', 'excluded', 'unknown')

    def __init__(self, covered, excluded, unknown):
        self.covered = tuple(covered)
        self.excluded = tuple(excluded)
        self.unknown = tuple(unknown)

    def __repr__(self):
        return "CoverageResult(covered=%r, excluded=%r, unknown=%r)" % (
            self.covered,
            self.excluded,
            self.unknown,
        )


class CoverageDeclaration:
    """A supplier's statement of which indices are covered.

    Parameters
    ----------
    index_set : Set
        The Pyomo index set that the declaration refers to.
    ranges : mapping or iterable, optional
        Inclusive index ranges.  A mapping is of the form
        ``{dimension: (start, end)}`` and its entries are
        **conjunctive**: a member is selected only if it satisfies every
        range.  An iterable may contain ``(dimension, start, end)``
        triples; those are **independent** positive statements (their
        union is covered), which allows overlapping declarations.  For
        a one-dimensional index set, a bare ``(start, end)`` pair is
        also accepted.
    include : iterable, optional
        Explicit index combinations to include.  These widen the
        covered set; they are not crossed with anything.  Every
        combination is checked against ``index_set`` and any that does
        not exist is reported as unknown.
    exclude : iterable, optional
        Explicit index combinations, or a mapping of
        ``{dimension: (start, end)}`` ranges, to remove from the covered
        set.  A list of ``(dimension, start, end)`` triples is also
        accepted as a collection of ranges.  Every exclusion is checked
        against ``index_set``; an exclusion naming a member that does
        not exist is reported as unknown.
    dimension : int, optional
        When given with a bare ``(start, end)`` range, identifies the
        dimension that the range applies to.  Ignored otherwise.

    Notes
    -----
    Overlapping statements are harmless.  A member is covered when it is
    selected by the ranges or by an explicit inclusion, and it is not
    removed by an exclusion.  Ranges are only ever used to filter the
    members that actually exist in ``index_set``.
    """

    def __init__(
        self,
        index_set,
        ranges=None,
        include=None,
        exclude=None,
        dimension=None,
    ):
        if isinstance(index_set, Param):
            # Accept a Param directly; coverage is always evaluated
            # against its real index set.
            index_set = index_set.index_set()
        if not hasattr(index_set, 'dimen'):
            raise TypeError(
                "CoverageDeclaration requires a Pyomo Set or Param; "
                "received %s" % (type(index_set).__name__,)
            )
        self._index_set = index_set
        self._dimension = dimension
        self._ranges = []
        self._include = OrderedSet()
        self._exclude = OrderedSet()
        self._exclude_ranges = []
        self._unknown = OrderedSet()
        self._evaluated = None

        if ranges is not None:
            self._add_ranges(ranges)
        if include is not None:
            self._add_explicit(include, self._include)
        if exclude is not None:
            self._add_exclude(exclude)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @property
    def index_set(self):
        return self._index_set

    @property
    def ranges(self):
        return tuple(self._ranges)

    @property
    def include(self):
        return tuple(self._include)

    @property
    def exclude(self):
        return tuple(self._exclude)

    @property
    def unknown(self):
        return tuple(self._unknown)

    def _dimen(self):
        return self._index_set.dimen

    def _check_dimension(self, dim):
        if isinstance(dim, bool) or not isinstance(dim, int):
            raise TypeError(
                "Coverage range dimension must be an integer; received %s"
                % (type(dim).__name__,)
            )
        dimen = self._dimen()
        if dimen is not None and dimen >= 0 and not (0 <= dim < dimen):
            raise ValueError(
                "Coverage range dimension %s is outside the %s-dimensional "
                "index set '%s'" % (dim, dimen, self._index_set.name)
            )
        return dim

    def _add_ranges(self, ranges, target=None):
        if target is None:
            target = self._ranges
        if isinstance(ranges, dict):
            entries = [
                {'dimension': dim, 'start': bounds[0], 'end': bounds[1]}
                for dim, bounds in ranges.items()
            ]
            grouped = [entries]
        elif (
            isinstance(ranges, (list, tuple))
            and len(ranges) == 2
            and all(not isinstance(v, (tuple, list, dict)) for v in ranges)
        ):
            dim = self._dimension
            if dim is None:
                if self._dimen() == 1:
                    dim = 0
                else:
                    raise ValueError(
                        "A bare range pair requires an explicit dimension "
                        "for the %s-dimensional index set '%s'"
                        % (self._dimen(), self._index_set.name)
                    )
            grouped = [[(dim, ranges[0], ranges[1])]]
        else:
            grouped = [[entry] for entry in ranges]
        for group in grouped:
            normalized = []
            for entry in group:
                if isinstance(entry, dict):
                    if set(entry) != {'dimension', 'start', 'end'}:
                        raise ValueError(
                            "Coverage range mappings must contain 'dimension', "
                            "'start', and 'end'"
                        )
                    dim, start, end = (
                        entry['dimension'],
                        entry['start'],
                        entry['end'],
                    )
                elif len(entry) == 2:
                    dim, start, end = self._dimension, entry[0], entry[1]
                    if dim is None:
                        if self._dimen() == 1:
                            dim = 0
                        else:
                            raise ValueError(
                                "A bare range pair requires an explicit "
                                "dimension for index set '%s'"
                                % (self._index_set.name,)
                            )
                elif len(entry) == 3:
                    dim, start, end = entry[0], entry[1], entry[2]
                else:
                    raise ValueError(
                        "A coverage range must be (dimension, start, end) or "
                        "a (start, end) pair"
                    )
                dim = self._check_dimension(dim)
                if start > end:
                    raise ValueError(
                        "Coverage range start %r is greater than end %r"
                        % (start, end)
                    )
                normalized.append((dim, start, end))
            target.append(normalized)

    def _add_explicit(self, values, target):
        if self._is_single_combination(values):
            values = (values,)
        for value in values:
            index = self._normalize_member(value)
            target.add(index)

    def _is_single_combination(self, values):
        if values.__class__ is not tuple:
            return False
        dimen = self._dimen()
        return dimen is not None and dimen > 1 and len(values) == dimen

    def _add_exclude(self, exclude):
        if isinstance(exclude, dict):
            self._add_ranges(exclude, target=self._exclude_ranges)
            return
        if _is_range_triple(exclude):
            dim = self._check_dimension(exclude[0])
            self._exclude_ranges.append([(dim, exclude[1], exclude[2])])
            return
        if self._is_single_combination(exclude):
            self._exclude.add(self._normalize_member(exclude))
            return
        if isinstance(exclude, (list, tuple)):
            for value in exclude:
                if _is_range_triple(value):
                    dim = self._check_dimension(value[0])
                    self._exclude_ranges.append([(dim, value[1], value[2])])
                else:
                    self._exclude.add(self._normalize_member(value))
            return
        self._exclude.add(self._normalize_member(exclude))

    def _normalize_member(self, value):
        dim = self._dimen()
        if value.__class__ is list:
            value = tuple(value)
        if dim == 1:
            return _normalize_index(value)
        if dim is not None and dim > 1:
            if value.__class__ is not tuple:
                return (value,)
            return value
        return _normalize_index(value)

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------
    def _range_value(self, member, dim):
        if member.__class__ is not tuple:
            if dim != 0:
                raise ValueError(
                    "Cannot apply a range to dimension %s of the scalar "
                    "member %r" % (dim, member)
                )
            return member
        if dim >= len(member):
            raise ValueError(
                "Cannot apply a range to dimension %s of member %r"
                % (dim, member)
            )
        return member[dim]

    def _member_satisfies_ranges(self, member):
        for group in self._ranges:
            for dim, start, end in group:
                value = self._range_value(member, dim)
                if not (start <= value <= end):
                    break
            else:
                return True
        return False

    def _member_is_excluded_by_range(self, member):
        for group in self._exclude_ranges:
            for dim, start, end in group:
                value = self._range_value(member, dim)
                if not (start <= value <= end):
                    break
            else:
                return True
        return False

    def _candidate_members(self):
        """Iterate real members selected by the declared ranges."""
        if not self._ranges:
            return
        index_set = self._index_set
        if not index_set.isfinite():
            raise ValueError(
                "Coverage ranges require a finite index set; '%s' is not "
                "finite" % (index_set.name,)
            )
        for member in index_set:
            if self._member_satisfies_ranges(member):
                yield member

    def evaluate(self):
        """Evaluate this declaration against the real index set.

        Returns
        -------
        CoverageResult
            ``covered`` contains only members that exist in the index
            set.  ``unknown`` contains explicitly declared members that
            do not exist.  ``excluded`` contains existing members that
            were named by an exclusion.
        """
        return self._evaluation()

    def _evaluate(self):
        index_set = self._index_set
        covered = OrderedSet()
        unknown = OrderedSet()
        excluded = OrderedSet()

        for member in self._candidate_members():
            covered.add(member)
        for member in self._include:
            if member in index_set:
                covered.add(member)
            else:
                unknown.add(member)
        for member in self._exclude:
            if member in index_set:
                excluded.add(member)
            else:
                unknown.add(member)

        for member in tuple(covered):
            if self._member_is_excluded_by_range(member):
                covered.discard(member)
                excluded.add(member)
        for member in self._exclude:
            covered.discard(member)

        self._unknown = unknown
        if unknown:
            logger.warning(
                "Coverage declaration for index set '%s' names %s member(s) "
                "that do not exist: %s",
                index_set.name,
                len(unknown),
                list(unknown)[:10],
            )
        return CoverageResult(
            covered=tuple(covered), excluded=tuple(excluded), unknown=tuple(unknown)
        )

    def _evaluation(self):
        if self._evaluated is None:
            self._evaluated = self._evaluate()
        return self._evaluated

    def covered_members(self):
        """Return the covered members as an ordered set."""
        return OrderedSet(self._evaluation().covered)

    def __contains__(self, member):
        return self._normalize_member(member) in self._evaluation().covered

    def __iter__(self):
        return iter(self._evaluation().covered)

    def __len__(self):
        return len(self._evaluation().covered)

    # ------------------------------------------------------------------
    # Migration
    # ------------------------------------------------------------------
    def migrate(self, new_index_set, identity=None):
        """Migrate this declaration to a revised index set.

        Only members whose stable identity is present in the new index
        set are migrated.  The migrated declaration contains explicit
        members only, so ranges that happened to match new members are
        not carried over by accident.  Members that appear in the new
        set but were not covered by this declaration are reported as
        ``added`` and are not covered by the returned declaration.

        Parameters
        ----------
        new_index_set : Set
        identity : callable or mapping, optional
            ``identity(member)`` (or ``mapping[member]``) returns a
            stable identity key.  Return ``None`` to mark a member as
            unstable.  By default a member's value is its identity.
        """
        if not hasattr(new_index_set, 'dimen'):
            raise TypeError(
                "CoverageDeclaration.migrate requires a Pyomo Set; received %s"
                % (type(new_index_set).__name__,)
            )
        if identity is None:
            key = lambda member: member
        elif isinstance(identity, dict):
            key = lambda member: identity.get(member, member)
        elif callable(identity):
            key = identity
        else:
            raise TypeError(
                "identity must be a callable or a mapping; received %s"
                % (type(identity).__name__,)
            )

        old = self.evaluate()
        new_members = list(new_index_set) if new_index_set.isfinite() else None
        if new_members is None:
            raise ValueError(
                "Coverage migration requires a finite index set; '%s' is not "
                "finite" % (new_index_set.name,)
            )
        new_by_key = {}
        for member in new_members:
            member_key = key(member)
            if member_key is not None:
                new_by_key.setdefault(member_key, member)

        migrated = OrderedSet()
        dropped = OrderedSet()
        for member in old.covered:
            member_key = key(member)
            if member_key is not None and member_key in new_by_key:
                migrated.add(new_by_key[member_key])
            else:
                dropped.add(member)

        declaration = CoverageDeclaration(new_index_set, include=list(migrated))
        migrated_keys = {key(member) for member in migrated}
        added = OrderedSet(
            member for member in new_members if key(member) not in migrated_keys
        )
        return MigrationResult(
            declaration=declaration,
            migrated=tuple(migrated),
            dropped=tuple(dropped),
            added=tuple(added),
            unknown=old.unknown,
        )


class MigrationResult:
    """The result of migrating a declaration to a revised index set."""

    __slots__ = ('declaration', 'migrated', 'dropped', 'added', 'unknown')

    def __init__(self, declaration, migrated, dropped, added, unknown):
        self.declaration = declaration
        self.migrated = tuple(migrated)
        self.dropped = tuple(dropped)
        self.added = tuple(added)
        self.unknown = tuple(unknown)

    def __repr__(self):
        return (
            "MigrationResult(migrated=%r, dropped=%r, added=%r, unknown=%r)"
            % (self.migrated, self.dropped, self.added, self.unknown)
        )


# ----------------------------------------------------------------------
# Parameter value provenance
# ----------------------------------------------------------------------
def _default_at(param, index):
    """Return the model default for ``index`` without materializing data."""
    if param._default is None:
        return Param.NoValue
    if param.is_indexed():
        return param._default(param.parent_block(), index)
    return param._default(param.parent_block(), None)


def _is_range_triple(entry):
    """True when ``entry`` looks like ``(dimension, start, end)``."""
    return (
        isinstance(entry, tuple)
        and len(entry) == 3
        and isinstance(entry[0], int)
        and not isinstance(entry[0], bool)
    )


def _stored_value(data):
    value = getattr(data, '_value', data)
    if value is Param.NoValue:
        return Param.NoValue
    return value


def _explicit_indices(param):
    """Return the set of indices for which a value was explicitly supplied.

    ``None`` means that the initializer is universal (a callback rule,
    a constant, or no initializer at all), so explicit data cannot be
    distinguished from a missing observation on a mutable Param that
    has already materialized defaults.  In that case the presence of a
    value in ``_data`` is treated as an original observation.
    """
    rule = param._rule
    if rule is not None and rule.contains_indices():
        return set(rule.indices())
    return None


class _Resolver:
    """Classify observations for one parameter without side effects."""

    __slots__ = ('param', '_explicit')

    def __init__(self, param):
        self.param = param
        self._explicit = _explicit_indices(param)

    def at(self, index):
        param = self.param
        if not param.is_indexed():
            index = None
        data = param._data
        if index in data:
            value = _stored_value(data[index])
            if value is Param.NoValue:
                return Observation(ObservationKind.MISSING, None)
            if (
                param.mutable
                and param._default is not None
                and self._explicit is not None
                and index not in self._explicit
            ):
                # A mutable Param materializes defaults into ``_data``
                # when they are accessed.  A value equal to the default
                # at an index that was not explicitly initialized is a
                # default observation, not an original one.
                default = _default_at(param, index)
                if default is not Param.NoValue and value == default:
                    return Observation(ObservationKind.DEFAULT, value)
            return Observation(ObservationKind.ORIGINAL, value)
        default = _default_at(param, index)
        if default is Param.NoValue:
            return Observation(ObservationKind.MISSING, None)
        return Observation(ObservationKind.DEFAULT, default)


def observation(param, index):
    """Classify and return the value available for ``param[index]``.

    Unlike ``param[index]``, this function does not materialize a
    mutable Param's default into ``param._data``.  It distinguishes an
    explicitly supplied value from a model default, and reports a
    missing observation without substituting zero.

    Parameters
    ----------
    param : Param
    index : object
        The parameter index.  Scalar parameters accept only ``None``
        (or an empty tuple).

    Returns
    -------
    Observation
    """
    if not param.is_indexed():
        if index not in (None, ()):
            raise KeyError(
                "Scalar parameter '%s' does not accept index %r"
                % (param.name, index)
            )
        return _Resolver(param).at(None)
    normalized = _normalize_index(index)
    if normalized not in param.index_set():
        raise KeyError(
            "Index %r is not in the index set of parameter '%s'"
            % (index, param.name)
        )
    return _Resolver(param).at(normalized)


def original_value(param, index):
    """Return the explicitly supplied value at ``index``, or ``None``."""
    result = observation(param, index)
    if result.kind is ObservationKind.ORIGINAL:
        return result.value
    return None


def default_value(param, index=None):
    """Return the model default for ``param`` (at ``index`` if indexed).

    Returns ``None`` when the parameter declares no default.
    """
    if param.is_indexed():
        if index is None:
            value = param.default()
        else:
            value = _default_at(param, _normalize_index(index))
    else:
        value = _default_at(param, None)
    if value is Param.NoValue:
        return None
    return value


# ----------------------------------------------------------------------
# Observation domains
# ----------------------------------------------------------------------
class FeasibilityReport:
    """An explanation of whether a calculation can be performed."""

    __slots__ = ('can_compute', 'domain', 'missing', 'defaults', 'unknown', 'reason')

    def __init__(
        self, can_compute, domain=(), missing=None, defaults=None, unknown=(), reason=''
    ):
        self.can_compute = can_compute
        self.domain = tuple(domain)
        self.missing = {} if missing is None else missing
        self.defaults = {} if defaults is None else defaults
        self.unknown = tuple(unknown)
        self.reason = reason

    def __bool__(self):
        return self.can_compute

    def __repr__(self):
        return (
            "FeasibilityReport(can_compute=%r, domain=%r, missing=%r, "
            "defaults=%r, unknown=%r, reason=%r)"
            % (
                self.can_compute,
                self.domain,
                self.missing,
                self.defaults,
                self.unknown,
                self.reason,
            )
        )


class ObservationDomain:
    """The common observation domain for a specified calculation.

    Parameters
    ----------
    index_set : Set
        The real Pyomo index set against which declarations are
        evaluated.
    params : iterable of Param, optional
        Parameters whose observations must be available.
    declarations : mapping, optional
        Mapping of ``Param`` (or parameter name) to a
        :class:`CoverageDeclaration`.  A parameter without a declaration
        is treated as covering its full index set.
    """

    def __init__(self, index_set, params=None, declarations=None):
        if not hasattr(index_set, 'dimen'):
            raise TypeError(
                "ObservationDomain requires a Pyomo Set; received %s"
                % (type(index_set).__name__,)
            )
        self._index_set = index_set
        self._params = []
        self._declarations = {}
        self._resolvers = {}
        if declarations:
            for param, declaration in declarations.items():
                self._declarations[param] = declaration
                self.add_parameter(param)
        if params is not None:
            for param in params:
                self.add_parameter(param)

    @property
    def index_set(self):
        return self._index_set

    @property
    def params(self):
        return tuple(self._params)

    def add_parameter(self, param):
        if param not in self._params:
            self._params.append(param)
        self._resolvers.pop(param, None)
        return param

    def _resolver(self, param):
        resolver = self._resolvers.get(param)
        if resolver is None:
            resolver = self._resolvers[param] = _Resolver(param)
        return resolver

    def _observation(self, param, index):
        return self._resolver(param).at(index)

    def _param_key(self, param):
        if isinstance(param, str):
            for candidate in self._declarations:
                if getattr(candidate, 'name', None) == param:
                    return candidate
            raise KeyError("Unknown parameter '%s'" % (param,))
        return param

    def declare(
        self, param, ranges=None, include=None, exclude=None, dimension=None
    ):
        """Attach (or replace) a coverage declaration for ``param``.

        The declaration is evaluated against this domain's index set;
        ``param.index_set()`` must therefore be compatible with it.
        """
        declaration = CoverageDeclaration(
            self._index_set,
            ranges=ranges,
            include=include,
            exclude=exclude,
            dimension=dimension,
        )
        self._declarations[param] = declaration
        self.add_parameter(param)
        return declaration

    def declaration(self, param):
        return self._declarations.get(self._param_key(param))

    def _covered(self, param):
        declaration = self._declarations.get(param)
        if declaration is None:
            if not self._index_set.isfinite():
                raise ValueError(
                    "Parameter '%s' has no coverage declaration and the "
                    "index set is not finite" % (param.name,)
                )
            return OrderedSet(self._index_set)
        return OrderedSet(declaration.evaluate().covered)

    def _unknown(self):
        unknown = OrderedSet()
        for declaration in self._declarations.values():
            unknown.update(declaration.evaluate().unknown)
        return unknown

    def common_domain(self, require_original=False):
        """Return the indices usable for the specified calculation.

        The result is the intersection of the real members covered for
        every parameter with those for which every parameter provides a
        usable value.  With ``require_original=True``, values coming
        from a model default are not considered usable.

        No Cartesian product of per-dimension members is ever formed.
        """
        if not self._params:
            raise ValueError(self._NO_PARAMETERS)
        if not self._index_set.isfinite():
            raise ValueError(
                "ObservationDomain requires a finite index set; '%s' is not "
                "finite" % (self._index_set.name,)
            )
        candidates = OrderedSet(self._index_set)
        for param in self._params:
            covered = self._covered(param)
            usable = OrderedSet()
            for index in candidates:
                if index not in covered:
                    continue
                result = self._observation(param, index)
                if result.kind is ObservationKind.MISSING:
                    continue
                if require_original and result.kind is not ObservationKind.ORIGINAL:
                    continue
                usable.add(index)
            candidates = OrderedSet(
                index for index in candidates if index in usable
            )
            if not candidates:
                break
        return candidates

    def observed_domain(self):
        """Return the common domain restricted to original observations."""
        return self.common_domain(require_original=True)

    def observation(self, param, index):
        """Return the :class:`Observation` for ``param[index]``."""
        index = _normalize_index(index)
        if index not in self._index_set:
            return Observation(ObservationKind.MISSING, None)
        return self._observation(param, index)

    def original(self, param):
        """Return covered indices for which ``param`` has an original value."""
        covered = self._covered(param)
        return tuple(
            index
            for index in covered
            if self._observation(param, index).kind is ObservationKind.ORIGINAL
        )

    def defaulted(self, param):
        """Return covered indices whose value comes from the model default."""
        covered = self._covered(param)
        return tuple(
            index
            for index in covered
            if self._observation(param, index).kind is ObservationKind.DEFAULT
        )

    def observations(self, index):
        """Return ``{parameter name: Observation}`` for one index."""
        return {param.name: self.observation(param, index) for param in self._params}

    def missing(self, param):
        """Return covered indices for which ``param`` has no value."""
        covered = self._covered(param)
        return tuple(
            index
            for index in covered
            if self._observation(param, index).kind is ObservationKind.MISSING
        )

    def defaults(self, param):
        """Alias for :meth:`defaulted`."""
        return self.defaulted(param)

    def explain(self, require_original=False):
        """Explain whether the calculation can be performed.

        Returns
        -------
        FeasibilityReport
            ``can_compute`` is True exactly when the common domain is
            non-empty.  ``reason`` names the first blocking observation
            (or states which observations are missing).  ``missing`` and
            ``defaults`` map parameter names to the relevant indices.
        """
        domain = self.common_domain(require_original=require_original)
        missing = {}
        defaults = {}
        for param in self._params:
            covered = self._covered(param)
            param_missing = tuple(
                index
                for index in covered
                if self._observation(param, index).kind is ObservationKind.MISSING
            )
            if param_missing:
                missing[param.name] = param_missing
            if require_original:
                param_defaults = tuple(
                    index
                    for index in covered
                    if self._observation(param, index).kind is ObservationKind.DEFAULT
                )
                if param_defaults:
                    defaults[param.name] = param_defaults
        unknown = self._unknown()
        if not self._params:
            return FeasibilityReport(
                False, (), missing, defaults, unknown, self._NO_PARAMETERS
            )
        if domain:
            if missing:
                reason = "; ".join(
                    "'%s' is missing observation(s) %s" % (name, list(indices)[:5])
                    for name, indices in missing.items()
                )
            elif defaults:
                reason = "; ".join(
                    "'%s' has model default(s) at %s" % (name, list(indices)[:5])
                    for name, indices in defaults.items()
                )
            else:
                reason = "%s usable observation(s)" % (len(domain),)
            return FeasibilityReport(
                True, domain, missing, defaults, unknown, reason
            )
        if missing:
            reason = "; ".join(
                "'%s' is missing observation(s) %s" % (name, list(indices)[:5])
                for name, indices in missing.items()
            )
        elif defaults:
            reason = "; ".join(
                "'%s' only has model default(s) at %s"
                % (name, list(indices)[:5])
                for name, indices in defaults.items()
            )
        else:
            reason = "the coverage declarations share no usable members"
        return FeasibilityReport(False, (), missing, defaults, unknown, reason)

    _NO_PARAMETERS = (
        "ObservationDomain requires at least one parameter; call declare() "
        "or pass params to the constructor"
    )


__all__ = [
    'CoverageDeclaration',
    'CoverageResult',
    'FeasibilityReport',
    'MigrationResult',
    'Observation',
    'ObservationDomain',
    'ObservationKind',
    'default_value',
    'observation',
    'original_value',
]
