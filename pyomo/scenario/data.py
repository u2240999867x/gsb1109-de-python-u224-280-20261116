# ____________________________________________________________________________________
#
# Pyomo: Python Optimization Modeling Objects
# Copyright (c) 2008-2026 National Technology and Engineering Solutions of Sandia, LLC
# Under the terms of Contract DE-NA0003525 with National Technology and Engineering
# Solutions of Sandia, LLC, the U.S. Government retains certain rights in this
# software.  This software is distributed under the 3-clause BSD License.
# ____________________________________________________________________________________
"""Versioned data layers used to build scenario model inputs.

A :class:`DataLayer` is a revisioned source of declarations.  Every public
mutation increments :attr:`DataLayer.version`; the runtime-computed
:attr:`DataLayer.fingerprint` additionally detects direct edits to the
exposed mappings.  Scenarios may share one base draft and only pay for a
new resolved input when the effective data actually changes.
"""

from __future__ import annotations

import copy
import hashlib

from collections import OrderedDict
from dataclasses import dataclass
from typing import Mapping

from pyomo.scenario.errors import ScenarioError

__all__ = [
    'RETRACT',
    'is_retract',
    'DataLayer',
    'BaseDraft',
    'SharedDraft',
    'LayerSnapshot',
    'content_fingerprint',
]


class _RetractType:
    __slots__ = ()

    def __repr__(self):
        return 'RETRACT'

    def __reduce__(self):
        return 'RETRACT'


RETRACT = _RetractType()


def is_retract(value):
    """Return True if *value* is the retraction sentinel."""
    return value is RETRACT


def _freeze(value):
    """Return a hashable, order-independent representation of *value*."""
    if value is RETRACT:
        return ('__retract__',)
    if isinstance(value, Mapping):
        return (
            '__mapping__',
            tuple(
                sorted(
                    ((repr(_freeze(k)), _freeze(v)) for k, v in value.items()),
                    key=lambda item: item[0],
                )
            ),
        )
    if isinstance(value, (list, tuple)):
        return ('__sequence__', tuple(_freeze(v) for v in value))
    if isinstance(value, (set, frozenset)):
        return ('__set__', tuple(sorted((repr(_freeze(v)) for v in value))))
    if isinstance(value, (str, bytes, int, float, bool, type(None))):
        return (type(value).__name__, value)
    return ('__object__', repr(value))


def content_fingerprint(*objects):
    """Compute a stable fingerprint for nested mappings/sequences."""
    sha = hashlib.sha256()
    sha.update(repr(_freeze(objects)).encode('utf-8'))
    return sha.hexdigest()


def _as_member_dict(members):
    if members is None:
        return OrderedDict()
    if isinstance(members, Mapping):
        return OrderedDict((key, True) for key in members)
    return OrderedDict((key, True) for key in members)


def _is_scalar_symbol(value):
    return not isinstance(value, Mapping)


@dataclass(frozen=True)
class LayerSnapshot:
    """A consistent view of a :class:`DataLayer` at one revision."""

    name: str
    version: int
    priority: object
    values: Mapping
    sets: Mapping
    set_modes: Mapping
    indices: Mapping
    index_modes: Mapping
    index_sets: Mapping
    retractions: Mapping
    defaults: Mapping
    default_retracted: frozenset
    symbol_retracted: frozenset
    kinds: Mapping
    fingerprint: str

    def symbols(self):
        return set(self.kinds)


class DataLayer:
    """A versioned bucket of declarations for one inheritance layer.

    Parameters
    ----------
    name:
        The layer name.  It is also the default scenario name.
    data:
        A mapping ``symbol -> {index: value}``.  A value of :data:`RETRACT`
        is interpreted as a retraction of that index.  A payload shaped like
        ``{None: [...]}`` is interpreted as a set declaration; if a
        list-valued parameter is intended, use ``params=`` or
        ``set_modes``/``sets=`` explicitly.
    sets:
        A mapping of set symbol name to iterable of members.
    params:
        A mapping ``symbol -> {index: value}`` of parameter declarations.
    indices:
        A mapping ``param_symbol -> iterable_of_indices`` describing the
        parameter's effective index set.
    defaults:
        A mapping ``param_symbol -> default_value``.  Retracting a default
        is expressed with :data:`RETRACT`.
    retract:
        Either a collection of symbol names (retract the whole symbol), or
        a mapping ``symbol -> index`` / ``symbol -> [index, ...]`` /
        ``symbol -> True``.  Whole-symbol retraction is also accepted via
        ``symbol -> RETRACT``.
    priority:
        Optional explicit precedence.  Layers without an explicit priority
        are ranked by graph distance (the closest declaration wins).  Two
        declarations with the same explicit priority conflict rather than
        silently using insertion order.
    set_mode, index_mode:
        Default mode for set/index declarations.  ``"replace"`` (the
        default) makes the declaration override lower-priority membership;
        ``"extend"`` adds members to inherited membership.
    """

    def __init__(
        self,
        name='base',
        data=None,
        sets=None,
        params=None,
        indices=None,
        defaults=None,
        retract=None,
        priority=None,
        set_mode='replace',
        index_mode='replace',
        set_modes=None,
        index_modes=None,
        kind=None,
    ):
        self.name = name
        self.priority = priority
        self.version = 0
        self._values = OrderedDict()
        self._sets = OrderedDict()
        self._set_modes = {}
        self._indices = OrderedDict()
        self._index_modes = {}
        self._index_sets = OrderedDict()
        self._retractions = OrderedDict()
        self._defaults = OrderedDict()
        self._default_retracted = set()
        self._symbol_retracted = set()
        self._kinds = OrderedDict()
        self._default_set_mode = self._validate_mode(set_mode)
        self._default_index_mode = self._validate_mode(index_mode)

        if data is not None:
            if not isinstance(data, Mapping):
                raise TypeError('DataLayer data must be a mapping')
            count = 0
            for symbol, payload in data.items():
                if symbol is None:
                    raise ValueError('Symbol names cannot be None')
                if _is_scalar_symbol(payload):
                    if payload is RETRACT:
                        self._retract(symbol)
                    else:
                        self._set_value(symbol, None, payload)
                    count += 1
                    continue
                if len(payload) == 1 and None in payload:
                    members = payload[None]
                    if isinstance(members, (list, tuple, set, frozenset)):
                        self._declare_set(symbol, members)
                        count += 1
                        continue
                for index, value in payload.items():
                    if value is RETRACT:
                        self._retract(symbol, index)
                    else:
                        self._set_value(symbol, index, value)
                    count += 1
            self.version = count
        if params:
            count = 0
            for symbol, payload in params.items():
                if _is_scalar_symbol(payload):
                    if payload is RETRACT:
                        self._retract(symbol)
                    else:
                        self._set_value(symbol, None, payload)
                    count += 1
                    continue
                for index, value in payload.items():
                    if value is RETRACT:
                        self._retract(symbol, index)
                    else:
                        self._set_value(symbol, index, value)
                    count += 1
            self.version += count
        if sets:
            for symbol, members in sets.items():
                self._declare_set(symbol, members)
                self.version += 1
        if indices:
            for symbol, members in indices.items():
                self._declare_index(symbol, members)
                self.version += 1
        if defaults:
            for symbol, value in defaults.items():
                self._declare_kind(symbol, 'param')
                if value is RETRACT:
                    self._retract_default(symbol)
                else:
                    self._defaults[symbol] = copy.deepcopy(value)
                self.version += 1
        if set_modes:
            for symbol, mode in set_modes.items():
                self._set_modes[symbol] = self._validate_mode(mode)
        if index_modes:
            for symbol, mode in index_modes.items():
                self._index_modes[symbol] = self._validate_mode(mode)
        if kind:
            for symbol, value in kind.items():
                self._declare_kind(symbol, value)
        if retract is not None:
            self.retract_all(retract, bump=False)

    @staticmethod
    def _validate_mode(mode):
        if mode not in ('replace', 'extend'):
            raise ValueError(
                "Declaration mode must be either 'replace' or 'extend'; "
                f'received {mode!r}'
            )
        return mode

    def _declare_kind(self, symbol, kind):
        if kind not in ('param', 'set'):
            raise ValueError(f'Unknown symbol kind {kind!r} for {symbol!r}')
        old = self._kinds.get(symbol)
        if old is not None and old != kind:
            raise ScenarioError(
                f'Symbol {symbol!r} is declared as both {old!r} and {kind!r} '
                f'in layer {self.name!r}'
            )
        self._kinds[symbol] = kind

    # ------------------------------------------------------------------
    # Internal, version-neutral mutation helpers
    # ------------------------------------------------------------------
    def _set_value(self, symbol, index, value):
        if value is RETRACT:
            self._retract(symbol, index)
            return
        self._declare_kind(symbol, 'param')
        self._symbol_retracted.discard(symbol)
        self._values.setdefault(symbol, OrderedDict())[index] = copy.deepcopy(value)

    def _declare_set(self, symbol, members, mode=None):
        mode = self._default_set_mode if mode is None else self._validate_mode(mode)
        self._declare_kind(symbol, 'set')
        self._symbol_retracted.discard(symbol)
        current = self._sets.get(symbol)
        if current is None or mode == 'replace':
            self._sets[symbol] = _as_member_dict(members)
        else:
            for member in members:
                current[member] = True
        self._set_modes[symbol] = mode

    def _declare_index(self, symbol, members, mode=None):
        mode = self._default_index_mode if mode is None else self._validate_mode(mode)
        self._declare_kind(symbol, 'param')
        current = self._indices.get(symbol)
        if current is None or mode == 'replace':
            self._indices[symbol] = _as_member_dict(members)
        else:
            for member in members:
                current[member] = True
        self._index_modes[symbol] = mode

    def _retract_default(self, symbol):
        self._defaults.pop(symbol, None)
        self._default_retracted.add(symbol)

    def _retract(self, symbol, index=None):
        if index is None:
            previous_kind = self._kinds.get(symbol)
            self._values.pop(symbol, None)
            self._sets.pop(symbol, None)
            self._indices.pop(symbol, None)
            self._defaults.pop(symbol, None)
            self._set_modes.pop(symbol, None)
            self._index_modes.pop(symbol, None)
            self._retractions.pop(symbol, None)
            self._symbol_retracted.add(symbol)
            if previous_kind is not None:
                self._kinds[symbol] = previous_kind
        else:
            old_kind = self._kinds.get(symbol)
            if old_kind is None:
                old_kind = 'set' if symbol in self._sets else 'param'
                self._kinds.setdefault(symbol, old_kind)
            self._symbol_retracted.discard(symbol)
            self._values.get(symbol, {}).pop(index, None)
            if symbol in self._sets:
                self._sets[symbol].pop(index, None)
            if symbol in self._indices:
                self._indices[symbol].pop(index, None)
            self._retractions.setdefault(symbol, OrderedDict())[index] = True

    # ------------------------------------------------------------------
    # Public mutation API (each call advances the version)
    # ------------------------------------------------------------------
    def set_value(self, symbol, index, value):
        """Declare a parameter value at *index*."""
        self._set_value(symbol, index, value)
        self.version += 1

    def update(self, data):
        """Merge a model-input mapping into this layer."""
        if not isinstance(data, Mapping):
            raise TypeError('DataLayer.update() requires a mapping')
        for symbol, payload in data.items():
            if symbol is None:
                raise ValueError('Symbol names cannot be None')
            if _is_scalar_symbol(payload):
                if payload is RETRACT:
                    self._retract(symbol)
                else:
                    self._set_value(symbol, None, payload)
                self.version += 1
                continue
            if len(payload) == 1 and None in payload:
                members = payload[None]
                if isinstance(members, (list, tuple, set, frozenset)):
                    self._declare_set(symbol, members)
                    self.version += 1
                    continue
            for index, value in payload.items():
                if value is RETRACT:
                    self._retract(symbol, index)
                else:
                    self._set_value(symbol, index, value)
                self.version += 1

    def update_params(self, params):
        """Merge an explicit ``symbol -> {index: value}`` parameter mapping."""
        for symbol, payload in params.items():
            if _is_scalar_symbol(payload):
                if payload is RETRACT:
                    self._retract(symbol)
                else:
                    self._set_value(symbol, None, payload)
                self.version += 1
                continue
            for index, value in payload.items():
                if value is RETRACT:
                    self._retract(symbol, index)
                else:
                    self._set_value(symbol, index, value)
                self.version += 1

    def declare_set(self, symbol, members, mode=None):
        """Declare set *symbol* with *members*.

        With ``mode='replace'`` (default) this layer's membership replaces
        lower-priority declarations.  With ``mode='extend'`` the members are
        added to inherited membership.  Member-level retractions still apply.
        """
        self._declare_set(symbol, members, mode)
        self.version += 1

    def extend_set(self, symbol, members):
        """Add members to set *symbol* without replacing inherited members."""
        self.declare_set(symbol, members, mode='extend')

    def declare_index(self, symbol, members, mode=None):
        """Declare the effective index set for parameter *symbol*."""
        self._declare_index(symbol, members, mode)
        self.version += 1

    def link_index_set(self, symbol, set_name):
        """Use set *set_name* as the effective index set for *symbol*.

        This is the common modeling pattern ``Param(model.J)``.  The
        parameter is linked symbolically, so later changes to the set's
        membership are reflected when the scenario is resolved.
        """
        self._declare_kind(symbol, 'param')
        self._index_sets[symbol] = set_name
        self.version += 1

    def extend_index(self, symbol, members):
        """Add indices to parameter *symbol* without replacing inherited ones."""
        self.declare_index(symbol, members, mode='extend')

    def set_default(self, symbol, value):
        """Set a default value for parameter *symbol*."""
        if value is RETRACT:
            self.retract_default(symbol)
            return
        self._declare_kind(symbol, 'param')
        self._defaults[symbol] = copy.deepcopy(value)
        self._default_retracted.discard(symbol)
        self.version += 1

    def retract_default(self, symbol):
        """Withdraw an inherited default value for *symbol*."""
        self._retract_default(symbol)
        self.version += 1

    def retract(self, symbol, index=None):
        """Retract one index, set member, or the whole symbol.

        ``index=None`` retracts the complete symbol.  A retraction is a
        first-class declaration: it is distinct from never declaring a
        symbol or index, participates in precedence, and blocks inherited
        defaults for the affected members.
        """
        self._retract(symbol, index)
        self.version += 1

    def retract_all(self, spec, bump=True):
        """Retract symbols or indices from a compact specification."""
        if spec is None:
            return
        count = 0
        if isinstance(spec, Mapping):
            for symbol, value in spec.items():
                if value is True or value is None or value is RETRACT:
                    self._retract(symbol)
                    count += 1
                elif isinstance(value, Mapping):
                    for index in value:
                        self._retract(symbol, index)
                        count += 1
                elif isinstance(value, tuple):
                    self._retract(symbol, value)
                    count += 1
                elif isinstance(value, (list, set, frozenset)):
                    for index in value:
                        self._retract(symbol, index)
                        count += 1
                else:
                    self._retract(symbol, value)
                    count += 1
        else:
            if isinstance(spec, str):
                spec = [spec]
            for symbol in spec:
                self._retract(symbol)
                count += 1
        if bump:
            self.version += count
        else:
            self.version += count

    def clear(self):
        """Clear all declarations from this layer."""
        self._values.clear()
        self._sets.clear()
        self._set_modes.clear()
        self._indices.clear()
        self._index_modes.clear()
        self._index_sets.clear()
        self._retractions.clear()
        self._defaults.clear()
        self._default_retracted.clear()
        self._symbol_retracted.clear()
        self._kinds.clear()
        self.version += 1

    def bump_version(self):
        """Explicitly mark this layer as a new revision."""
        self.version += 1

    def fork(self, name=None):
        """Create an independent copy of this layer (e.g. for a child)."""
        new = DataLayer(
            name=self.name if name is None else name,
            priority=self.priority,
            set_mode=self._default_set_mode,
            index_mode=self._default_index_mode,
        )
        new._values = copy.deepcopy(self._values)
        new._sets = copy.deepcopy(self._sets)
        new._set_modes = dict(self._set_modes)
        new._indices = copy.deepcopy(self._indices)
        new._index_modes = dict(self._index_modes)
        new._index_sets = OrderedDict(self._index_sets)
        new._retractions = copy.deepcopy(self._retractions)
        new._defaults = copy.deepcopy(self._defaults)
        new._default_retracted = set(self._default_retracted)
        new._symbol_retracted = set(self._symbol_retracted)
        new._kinds = OrderedDict(self._kinds)
        new.version = self.version
        return new

    @classmethod
    def from_dat(cls, filename, name=None, namespace=None, **kwargs):
        """Create a layer from a Pyomo DAT file.

        The file's default namespace is loaded, plus ``namespace`` when
        supplied.  Retractions are expressed in DAT syntax as a missing
        value (``param c := 1 .`` means "retract c[1]"); pass
        ``retract_values=('.',)`` to customize the sentinel.
        """
        import pyomo.environ  # ensure the DAT data manager is registered

        from pyomo.dataportal import DataPortal

        retract_values = kwargs.pop('retract_values', ('.',))
        layer = cls(name=name, **kwargs)
        portal = DataPortal(filename=filename)
        namespaces = [None]
        if namespace is not None:
            namespaces.append(namespace)
        for ns in namespaces:
            if ns not in portal._data:
                continue
            for symbol, payload in portal._data[ns].items():
                if not isinstance(payload, Mapping):
                    layer.update({symbol: payload})
                elif None in payload and isinstance(payload[None], (list, tuple)):
                    layer.declare_set(symbol, payload[None])
                else:
                    for index, value in payload.items():
                        if value in retract_values:
                            layer.retract(symbol, index)
                        else:
                            layer.set_value(symbol, index, value)
        return layer

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    @property
    def data(self):
        """Live mapping of parameter values (mutate via the public API)."""
        return self._values

    @property
    def values(self):
        return self._values

    @property
    def sets(self):
        return self._sets

    @property
    def indices(self):
        return self._indices

    @property
    def defaults(self):
        return self._defaults

    @property
    def retractions(self):
        return self._retractions

    @property
    def kinds(self):
        return self._kinds

    def symbols(self):
        return set(self._kinds)

    def __getitem__(self, symbol):
        return self._values[symbol]

    def __contains__(self, symbol):
        return symbol in self._kinds

    def snapshot(self):
        """Return a :class:`LayerSnapshot` of the current contents."""
        values = {s: dict(v) for s, v in self._values.items()}
        sets = {s: tuple(v) for s, v in self._sets.items()}
        set_modes = dict(self._set_modes)
        indices = {s: tuple(v) for s, v in self._indices.items()}
        index_modes = dict(self._index_modes)
        index_sets = dict(self._index_sets)
        retractions = {
            s: {index: True for index in v} for s, v in self._retractions.items()
        }
        defaults = {s: copy.deepcopy(v) for s, v in self._defaults.items()}
        default_retracted = frozenset(self._default_retracted)
        symbol_retracted = frozenset(self._symbol_retracted)
        kinds = dict(self._kinds)
        fingerprint = content_fingerprint(
            values,
            sets,
            set_modes,
            indices,
            index_modes,
            index_sets,
            retractions,
            defaults,
            default_retracted,
            symbol_retracted,
            kinds,
            self.priority,
        )
        return LayerSnapshot(
            name=self.name,
            version=self.version,
            priority=self.priority,
            values=values,
            sets=sets,
            set_modes=set_modes,
            indices=indices,
            index_modes=index_modes,
            index_sets=index_sets,
            retractions=retractions,
            defaults=defaults,
            default_retracted=default_retracted,
            symbol_retracted=symbol_retracted,
            kinds=kinds,
            fingerprint=fingerprint,
        )

    def fingerprint(self):
        """Return the current content fingerprint (also detects direct edits)."""
        return self.snapshot().fingerprint

    def __repr__(self):
        return (
            f'DataLayer({self.name!r}, version={self.version}, '
            f'symbols={len(self._kinds)})'
        )


class BaseDraft(DataLayer):
    """A shared base layer that multiple scenarios can inherit from.

    A base draft can be resolved once and reused until one of its revisions
    is published.  It is a semantic alias for :class:`DataLayer`.
    """

    def as_snapshot(self):
        return self.snapshot()


SharedDraft = BaseDraft
