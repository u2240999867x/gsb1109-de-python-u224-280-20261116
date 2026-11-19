# ____________________________________________________________________________________
#
# Pyomo: Python Optimization Modeling Objects
# Copyright (c) 2008-2026 National Technology and Engineering Solutions of Sandia, LLC
# Under the terms of Contract DE-NA0003525 with National Technology and Engineering
# Solutions of Sandia, LLC, the U.S. Government retains certain rights in this
# software.  This software is distributed under the 3-clause BSD License.
# ____________________________________________________________________________________

"""Whole-batch validation of data loaded through a :class:`DataPortal`.

The data commands in a ``.dat`` file (and the rows in an external table)
are processed incrementally, but they describe a single coherent batch:
set members, composite index keys, and parameter bindings must all be
consistent before the batch is published (i.e., before an abstract model
instance is constructed).

This module validates a *complete* batch against the model declarations,
independent of the order in which the individual data commands were
loaded.  It intentionally operates on the declarations (and on the
membership information available in the batch) rather than constructing
the model: construction is what would leave behind a partially
initialized instance.

Validation failures raise :class:`DataValidationError` with the original
source location (row or line) attached.
"""

from pyomo.common.collections import OrderedDict
from pyomo.core.base.indexed_component import normalize_index
from pyomo.dataportal._batch import Provenance, struct_key


class DataValidationError(ValueError):
    """A batch of data is inconsistent with the model declarations."""

    def __init__(self, message, origin=None, symbol=None):
        self.origin = origin
        self.symbol = symbol
        self.detail = message
        if symbol is not None:
            message = "%s (while loading '%s')" % (message, symbol)
        if origin is not None:
            message = "%s [%s]" % (message, origin)
        super().__init__(message)


class _Unknown:
    pass


_UNKNOWN = _Unknown()


class _BatchView:
    """Resolve symbols exactly as ``Model._initialize_component`` does."""

    def __init__(self, model, data, namespaces, provenance=None):
        self.model = model
        self.data = data
        self.namespaces = namespaces
        self.provenance = provenance or Provenance()

    def raw(self, name):
        for namespace in self.namespaces:
            namespace_data = self.data.get(namespace, {})
            if name in namespace_data:
                return namespace_data[name]
        return _UNKNOWN

    def namespace(self, name):
        for namespace in self.namespaces:
            if name in self.data.get(namespace, {}):
                return namespace
        return self.namespaces[0] if self.namespaces else None

    def origin(self, name, key):
        return self.provenance.get(self.namespace(name), name, _normalize(key))


def _normalize(value):
    if normalize_index.flatten:
        return normalize_index(value)
    return value


def _known_dimen(dimen):
    """Return ``dimen`` when it is a usable integer, else ``None``."""
    from pyomo.core.base.set import UnknownSetDimen

    if dimen is None or dimen is UnknownSetDimen:
        return None
    if isinstance(dimen, type):  # e.g. UnknownSetDimen used as a sentinel
        return None
    return dimen


def _arity(value):
    value = _normalize(value)
    if value.__class__ is tuple:
        return len(value)
    return 1


def _resolve(model, name):
    if model is None:
        return None
    find_component = getattr(model, 'find_component', None)
    if find_component is not None:
        try:
            component = find_component(name)
        except Exception:
            component = None
        if component is not None:
            return component
    try:
        return getattr(model, name)
    except Exception:
        return None


def _is_set(component):
    from pyomo.core.base.set import Set

    return getattr(component, 'ctype', None) is Set


def _is_param(component):
    from pyomo.core.base.param import Param

    return getattr(component, 'ctype', None) is Param


def _is_set_operator(component):
    from pyomo.core.base.set import SetOperator

    return isinstance(component, SetOperator)


def _constant_initializer(component, name):
    initializer = getattr(component, '_init_values', None)
    if initializer is None:
        return None
    if getattr(initializer, 'contains_indices', lambda: False)():
        return None
    if not initializer.constant():
        return None
    try:
        values = initializer(component.parent_block(), None)
    except Exception:
        return None
    if values is None:
        return []
    try:
        return list(values)
    except TypeError:
        return None


def _data_set_members(entry, indexed=False):
    """Return the members of a Set (or of every slice of an IndexedSet)."""
    if entry is _UNKNOWN:
        return None
    if isinstance(entry, (list, tuple, set, frozenset)):
        return list(entry)
    if isinstance(entry, dict):
        if None in entry:
            value = entry[None]
            if isinstance(value, (list, tuple, set, frozenset)):
                return list(value)
            return [value]
        if indexed:
            members = []
            for values in entry.values():
                if isinstance(values, (list, tuple, set, frozenset)):
                    members.extend(values)
                else:
                    members.append(values)
            return members
        # A scalar Set stored as a mapping of index -> value
        return [entry]
    return [entry]


def _tuplize_members(members, dimen):
    """Mirror Set's flat-list handling: group scalars into ``dimen``-tuples.

    Set initializers accept a flat sequence (``[1, 4, 2, 5]``) for a
    2-dimensional Set and regroup it into tuples.  The validator must see
    the grouped members, otherwise it would report an arity error on the
    first scalar.  If the flat list cannot be grouped, ``None`` is
    returned so that Set construction raises its canonical "Cannot
    tuplize" error.
    """
    if not members or dimen is None or dimen < 2:
        return members
    if isinstance(members[0], tuple):
        return members
    if len(members) % dimen:
        # Cannot regroup: defer to Set construction for its canonical
        # "Cannot tuplize" error.
        return None
    return [tuple(members[i : i + dimen]) for i in range(0, len(members), dimen)]


class _SetInfo:
    __slots__ = ('component', 'name', 'members', 'dimen', 'domain')

    def __init__(self, component, name, members, dimen, domain):
        self.component = component
        self.name = name
        self.members = members
        self.dimen = dimen
        self.domain = domain


class _ModelInspector:
    def __init__(self, view):
        self.view = view
        self.model = view.model
        self._set_cache = {}
        self._domain_cache = {}
        self._member_keys = {}
        self._evidence_cache = {}
        self._dimen_cache = {}

    # -- Set introspection -------------------------------------------------
    def set_info(self, name_or_component):
        if isinstance(name_or_component, str):
            component = _resolve(self.model, name_or_component)
            if component is None:
                return None
            name = name_or_component
        else:
            component = name_or_component
            name = component.name
        key = id(component)
        if key not in self._set_cache:
            self._set_cache[key] = self._build_set_info(component, name)
        return self._set_cache[key]

    def _build_set_info(self, component, name):
        if not _is_set(component):
            return None
        entry = self.view.raw(name)
        indexed = bool(getattr(component, 'is_indexed', lambda: False)())
        if entry is not _UNKNOWN and not _is_set_operator(component):
            members = _data_set_members(entry, indexed)
            if members is not None:
                dimen, known = self._dimen(component, name)
                if not indexed:
                    members = _tuplize_members(members, known)
                return _SetInfo(
                    component, name, members, dimen, self.domain(component, name)
                )
        if _is_set_operator(component):
            # Derived sets (products/unions/...) get their members from
            # their operands; there is no separate initializer.
            dimen, _ = self._dimen(component, name)
            return _SetInfo(component, name, None, dimen, self.domain(component, name))
        members = _constant_initializer(component, name)
        if members is not None:
            dimen, known = self._dimen(component, name)
            if not indexed:
                members = _tuplize_members(members, known)
            return _SetInfo(
                component, name, members, dimen, self.domain(component, name)
            )
        # A plain Set with no data and no constant initializer is empty.
        dimen, _ = self._dimen(component, name)
        if getattr(component, '_init_values', None) is None:
            return _SetInfo(component, name, [], dimen, self.domain(component, name))
        return _SetInfo(component, name, None, dimen, self.domain(component, name))

    def _dimen(self, component, name):
        """Return ``(dimen, known_dimen_or_None)`` for a Set declaration."""
        dimen = _component_dimen(component)
        known = _known_dimen(dimen)
        if known is None:
            domain = self.domain(component, name)
            if domain is not None and domain is not component:
                known = self.effective_dimen(domain)
                if known is not None:
                    dimen = known
        return dimen, known

    def domain(self, component, name):
        initializer = getattr(component, '_init_domain', None)
        if initializer is None:
            return None
        key = id(component)
        if key in self._domain_cache:
            return self._domain_cache[key]
        domain = None
        if initializer.constant():
            try:
                domain = initializer(component.parent_block(), None, component)
            except Exception:
                domain = None
        self._domain_cache[key] = domain
        return domain

    def effective_dimen(self, reference):
        """Best-effort dimension of a Set / SetOperator reference.

        Declarations frequently leave sets at ``UnknownSetDimen`` until
        construction.  The batch itself supplies evidence (the arity of
        its members), and a SetProduct's dimension is the sum of its
        operands.  Returning ``None`` means "not knowable".
        """
        if reference is None:
            return None
        try:
            from pyomo.core.base.set import SetOperator
        except Exception:  # pragma: no cover
            SetOperator = ()
        # Only cache stable, named components.  SetOperator instances
        # (e.g. the freshly-created A*B of a ``within`` declaration) are
        # transient and their ids can be reused after garbage collection.
        cacheable = not isinstance(reference, SetOperator)
        if cacheable:
            marker = id(reference)
            if marker in self._dimen_cache:
                return self._dimen_cache[marker]
        answer = self._effective_dimen(reference, set())
        if cacheable:
            self._dimen_cache[id(reference)] = answer
        return answer

    def _effective_dimen(self, reference, _seen):
        from pyomo.core.base.set import SetProduct

        marker = id(reference)
        if marker in _seen:
            return None
        _seen.add(marker)
        if isinstance(reference, SetProduct):
            dims = [
                self._effective_dimen(s, set(_seen))
                for s in reference.subsets(expand_all_set_operators=False)
            ]
            if None in dims:
                return None
            dimen = sum(dims)
            return dimen if dimen else None
        parent = reference.parent_component()
        if parent is not reference:
            return self._effective_dimen(parent, _seen)
        if _is_set(reference):
            if self._range_backed(reference):
                dimen = _known_dimen(getattr(reference, 'dimen', None))
                if dimen is not None:
                    return dimen
            info = self.set_info(reference)
            if info is None:
                return None
            dimen = _known_dimen(info.dimen)
            if dimen is not None:
                return dimen
            domain = self.domain(reference, info.name)
            if domain is not None and domain is not reference:
                dimen = self._effective_dimen(domain, _seen)
                if dimen is not None:
                    return dimen
            if info.members:
                arities = {_arity(m) for m in info.members}
                if len(arities) == 1:
                    return arities.pop()
        return None

    # -- Membership --------------------------------------------------------
    def _range_backed(self, reference):
        """True if ``reference`` admits values beyond a finite member list."""
        try:
            from pyomo.core.base.set import RangeSet, GlobalSetBase
        except Exception:  # pragma: no cover
            return False
        parent = reference.parent_component()
        if parent is not reference:
            reference = parent
        if isinstance(reference, (RangeSet, GlobalSetBase)):
            return True
        return bool(getattr(reference, '_ranges', ()))

    def contains(self, reference, value, _seen=None):
        """Test ``value`` against a Set / SetOperator reference.

        Returns ``True``/``False`` when the answer is known and ``None``
        when it cannot be determined from the declarations and batch.
        """
        if reference is None:
            return None
        if _seen is None:
            _seen = set()
        marker = id(reference)
        if marker in _seen:
            return None
        _seen.add(marker)
        try:
            from pyomo.core.base.set import (
                _AnySet,
                SetProduct,
                SetUnion,
                SetIntersection,
                SetDifference,
                RangeSet,
            )
        except Exception:  # pragma: no cover
            return None

        if isinstance(reference, _AnySet):
            return True
        if isinstance(reference, SetProduct):
            return self._contains_product(reference, value, _seen)
        if isinstance(reference, SetUnion):
            answers = [self.contains(s, value, set(_seen)) for s in reference._sets]
            if any(a is True for a in answers):
                return True
            if all(a is False for a in answers):
                return False
            return None
        if isinstance(reference, SetIntersection):
            answers = [self.contains(s, value, set(_seen)) for s in reference._sets]
            if any(a is False for a in answers):
                return False
            if all(a is True for a in answers):
                return True
            return None
        if isinstance(reference, SetDifference):
            left = self.contains(reference._sets[0], value, set(_seen))
            right = self.contains(reference._sets[1], value, set(_seen))
            if left is False:
                return False
            if right is True:
                return False
            if left is True and right is False:
                return True
            return None

        parent = reference.parent_component()
        if parent is not reference:
            return self.contains(parent, value, _seen)
        if _is_set(reference):
            if self._range_backed(reference) and not _is_set_operator(reference):
                try:
                    return value in reference
                except Exception:
                    return None
            info = self.set_info(reference)
            if info is None:
                return None
            if _is_set_operator(reference):
                return None
            if info.members is None:
                # No batch members: fall back on the declared domain
                # (e.g. a Set(within=NonNegativeReals) with no data).
                domain = self.domain(reference, reference.name)
                if domain is not None and domain is not reference:
                    answer = self.contains(domain, value, _seen)
                    if answer is not None:
                        return answer
                return None
            key = id(reference)
            known = self._member_keys.get(key)
            if known is None:
                known = {struct_key(_normalize(m)) for m in info.members}
                self._member_keys[key] = known
            return struct_key(_normalize(value)) in known
        try:
            return value in reference
        except Exception:
            return None

    def _contains_product(self, reference, value, _seen):
        subsets = list(reference.subsets(expand_all_set_operators=False))
        value = _normalize(value)
        items = list(value) if value.__class__ is tuple else [value]
        if len(subsets) == 1:
            return self.contains(subsets[0], value, _seen)
        dims = []
        for subset in subsets:
            dims.append(self.effective_dimen(subset) if _is_set(subset) else None)
        if None not in dims:
            total = sum(dims)
            if total != len(items):
                return False
            offset = 0
            answers = []
            for subset, dimen in zip(subsets, dims):
                piece = items[offset : offset + dimen]
                offset += dimen
                piece = piece[0] if dimen == 1 else tuple(piece)
                answers.append(self.contains(subset, piece, set(_seen)))
            if any(a is False for a in answers):
                return False
            if all(a is True for a in answers):
                return True
            return None
        # We cannot determine how to partition the key.  Fall back to
        # requiring that a single operand is itself multi-dimensional.
        answers = [self.contains(s, value, set(_seen)) for s in subsets]
        if any(a is True for a in answers):
            return True
        return None

    # -- Evidence ----------------------------------------------------------
    def has_evidence(self, reference, _seen=None):
        """Return True if the reference can supply membership evidence."""
        if reference is None:
            return None
        if _seen is None:
            _seen = set()
        marker = id(reference)
        if marker in _seen:
            return None
        _seen.add(marker)
        try:
            from pyomo.core.base.set import (
                _AnySet,
                SetProduct,
                SetUnion,
                SetIntersection,
                SetDifference,
            )
        except Exception:  # pragma: no cover
            return None
        if isinstance(reference, _AnySet):
            return True
        if isinstance(reference, (SetProduct, SetUnion, SetIntersection)):
            answers = [self.has_evidence(s, set(_seen)) for s in reference._sets]
            if any(a is False for a in answers):
                return False
            if all(a is True for a in answers):
                return True
            return None
        if isinstance(reference, SetDifference):
            return self.has_evidence(reference._sets[0], set(_seen))
        parent = reference.parent_component()
        if parent is not reference:
            return self.has_evidence(parent, _seen)
        if _is_set(reference):
            if self._range_backed(reference):
                # Range/global sets can decide membership without any
                # batch evidence.
                return True
            info = self.set_info(reference)
            if info is None:
                return None
            if _is_set_operator(reference):
                return None
            if info.members is None:
                # No batch members: a declared domain can still restrict
                # admissible values (e.g. NonNegativeReals).
                domain = self.domain(reference, reference.name)
                if domain is not None and domain is not reference:
                    answer = self.has_evidence(domain, _seen)
                    if answer is not None:
                        return answer
                return None
            key = id(reference)
            if key not in self._evidence_cache:
                self._evidence_cache[key] = bool(info.members)
            return self._evidence_cache[key]
        return None


def _component_dimen(component):
    from pyomo.core.base.set import UnknownSetDimen

    initializer = getattr(component, '_init_dimen', None)
    if initializer is not None and initializer.constant():
        try:
            dimen = _known_dimen(initializer(component.parent_block(), None))
        except Exception:
            dimen = None
        if dimen is not None:
            return dimen
    try:
        dimen = _known_dimen(component.dimen)
    except Exception:
        dimen = None
    if dimen is not None:
        return dimen
    # A SetProduct / SetOperator exposes a usable dimen even when the
    # individual operands are not yet constructed.
    if _is_set_operator(component):
        try:
            dimen = _known_dimen(component.dimen)
        except Exception:
            dimen = None
        if dimen is not None:
            return dimen
    return UnknownSetDimen


def _iter_referenced_sets(reference, seen=None):
    """Yield the model components referenced by a Set expression."""
    from pyomo.core.base.set import SetOperator

    if reference is None:
        return
    if seen is None:
        seen = set()
    marker = id(reference)
    if marker in seen:
        return
    seen.add(marker)
    if isinstance(reference, SetOperator):
        for subset in reference._sets:
            yield from _iter_referenced_sets(subset, seen)
        return
    parent = reference.parent_component()
    if parent is not None and getattr(parent, 'name', None):
        yield parent


def _dependency_graph(inspector):
    model = inspector.model
    graph = OrderedDict()
    # Use component_map(): it exposes the top-level declarations without
    # iterating over (possibly unconstructed) indexed component data.
    for name, component in model.component_map().items():
        if not _is_set(component):
            continue
        dependencies = []
        index_set = getattr(component, '_index_set', None)
        for referenced in _iter_referenced_sets(index_set):
            if referenced.name != name:
                dependencies.append(referenced.name)
        domain = inspector.domain(component, name)
        for referenced in _iter_referenced_sets(domain):
            if referenced.name != name:
                dependencies.append(referenced.name)
        graph[name] = dependencies
    return graph


def _find_cycle(graph):
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {name: WHITE for name in graph}
    stack = []

    def visit(name):
        color[name] = GRAY
        stack.append(name)
        for dependency in graph.get(name, ()):
            if dependency not in color:
                continue
            if color[dependency] == GRAY:
                start = stack.index(dependency)
                return stack[start:] + [dependency]
            if color[dependency] == WHITE:
                cycle = visit(dependency)
                if cycle:
                    return cycle
        stack.pop()
        color[name] = BLACK
        return None

    for name in graph:
        if color[name] == WHITE:
            cycle = visit(name)
            if cycle:
                return cycle
    return None


def _validate_set(inspector, name, component, view):
    info = inspector.set_info(component)
    if info is None:
        return
    members = info.members
    if members is None:
        return
    dimen = inspector.effective_dimen(component)
    domain = info.domain
    for member in members:
        origin = view.origin(name, member)
        arity = _arity(member)
        if dimen is not None and arity != dimen:
            suffix = '' if arity == 1 else 's'
            raise DataValidationError(
                "member %r has %d dimension%s, but Set '%s' is declared "
                "with dimen=%s" % (member, arity, suffix, name, dimen),
                origin,
                name,
            )
        if domain is not None:
            answer = inspector.contains(domain, member)
            if answer is False:
                evidence = inspector.has_evidence(domain)
                if evidence is False:
                    raise DataValidationError(
                        "member %r has no evidence: its domain %s has no "
                        "members in this batch or in the model declaration"
                        % (member, domain),
                        origin,
                        name,
                    )
                raise DataValidationError(
                    "Cannot add value %s to Set %s.\n"
                    "\tThe value is not in the domain %s"
                    % (member, name, domain),
                    origin,
                    name,
                )
            if answer is None and inspector.has_evidence(domain) is False:
                raise DataValidationError(
                    "member %r has no evidence: its domain %s has no "
                    "members in this batch or in the model declaration"
                    % (member, domain),
                    origin,
                    name,
                )


def _index_set_of(component):
    index_set = getattr(component, '_index_set', None)
    if index_set is None:
        return None
    try:
        from pyomo.core.base.global_set import UnindexedComponent_set

        if index_set is UnindexedComponent_set:
            return None
    except Exception:  # pragma: no cover
        pass
    return index_set


def _validate_param(inspector, name, component, view):
    entry = view.raw(name)
    if entry is _UNKNOWN:
        return
    if not isinstance(entry, dict):
        return
    index_set = _index_set_of(component)
    if index_set is None:
        # Scalar parameter: value-domain validation is performed by the
        # Param constructor (with its established error semantics).
        return
    dimen = _index_dimen(inspector, index_set)
    evidence = inspector.has_evidence(index_set)
    for key, value in entry.items():
        origin = view.origin(name, key)
        arity = _arity(key)
        if dimen is not None and arity != dimen:
            raise DataValidationError(
                "index %r has %d dimension%s, but parameter '%s' is indexed "
                "by a %s-dimensional set"
                % (key, arity, '' if arity == 1 else 's', name, dimen),
                origin,
                name,
            )
        answer = inspector.contains(index_set, key)
        if answer is False:
            if evidence is False:
                raise DataValidationError(
                    "index %r has no evidence: its index set %s has no "
                    "members in this batch or in the model declaration"
                    % (key, index_set),
                    origin,
                    name,
                )
            raise DataValidationError(
                "index %r is not a member of the index set %s for parameter "
                "'%s'" % (key, index_set, name),
                origin,
                name,
            )


def _index_dimen(inspector, index_set):
    return inspector.effective_dimen(index_set)


def validate_model_data(model, data, namespaces=(None,), provenance=None):
    """Validate a complete batch of data against the model declarations.

    Parameters
    ----------
    model : BlockData
        The (cloned, unconstructed) model whose declarations define the
        sets and parameters.
    data : dict
        ``data[namespace][symbol]`` -> mapping of index to value.
    namespaces : iterable
        The namespaces selected for instance construction.
    provenance : Provenance, optional
        Side-channel mapping values back to their source row/line.
    """
    if model is None:
        return
    view = _BatchView(model, data, list(namespaces), provenance)
    inspector = _ModelInspector(view)

    cycle = _find_cycle(_dependency_graph(inspector))
    if cycle:
        raise DataValidationError(
            "dependency cycle detected among Set declarations: %s"
            % " -> ".join(cycle)
        )

    for name in _batch_symbols(view):
        component = _resolve(model, name)
        if component is None:
            continue
        if _is_set(component):
            _validate_set(inspector, name, component, view)
        elif _is_param(component):
            _validate_param(inspector, name, component, view)


def _batch_symbols(view):
    seen = OrderedDict()
    for namespace in view.namespaces:
        for name in view.data.get(namespace, {}):
            seen[name] = None
    return list(seen)
