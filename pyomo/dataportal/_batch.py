# ____________________________________________________________________________________
#
# Pyomo: Python Optimization Modeling Objects
# Copyright (c) 2008-2026 National Technology and Engineering Solutions of Sandia, LLC
# Under the terms of Contract DE-NA0003525 with National Technology and Engineering
# Solutions of Sandia, LLC, the U.S. Government retains certain rights in this
# software.  This software is distributed under the 3-clause BSD License.
# ____________________________________________________________________________________

"""Utilities shared by the data portal batch loader and validator.

The objects in this module deliberately do not import any Pyomo modeling
classes: they are used while data commands are being processed (before a
model instance exists) as well as while a batch is validated against the
model declarations.
"""

import contextvars


class Origin:
    """A description of where a piece of data came from.

    ``kind`` is either ``'row'`` (a row in an external table), ``'line'``
    (a data command line), or ``None`` when the source does not expose
    positional information.
    """

    __slots__ = ('filename', 'kind', 'line', 'row')

    def __init__(self, filename=None, kind=None, line=None, row=None):
        self.filename = filename
        self.kind = kind
        self.line = line
        self.row = row

    def __str__(self):
        if self.kind == 'row':
            if self.filename:
                return "row %s of table '%s'" % (self.row, self.filename)
            return "row %s" % (self.row,)
        if self.kind == 'line':
            if self.filename:
                return "line %s of file '%s'" % (self.line, self.filename)
            return "line %s" % (self.line,)
        if self.filename:
            return "file '%s'" % (self.filename,)
        return "unknown location"

    def __repr__(self):
        return "Origin(%r, %r, line=%r, row=%r)" % (
            self.filename,
            self.kind,
            self.line,
            self.row,
        )


# ``struct_key`` is intentionally type-aware.  It must never be replaced
# by ``str(value)``: two values may display identically (for example the
# integer ``1`` and the string ``'1'``) while being structurally distinct.
def struct_key(value):
    """Return a hashable, type-aware representation of ``value``.

    Unlike ``repr()``/``str()`` this representation retains the Python
    type of every element, including nested sequences.  It is used to
    decide whether data was really duplicated or merely displayed in the
    same way.
    """
    if isinstance(value, tuple):
        return ('tuple', tuple(struct_key(v) for v in value))
    if isinstance(value, list):
        return ('list', tuple(struct_key(v) for v in value))
    if isinstance(value, set):
        return ('set', frozenset(struct_key(v) for v in value))
    if isinstance(value, frozenset):
        return ('frozenset', frozenset(struct_key(v) for v in value))
    if isinstance(value, dict):
        return (
            'dict',
            tuple(
                sorted(
                    (struct_key(k), struct_key(v)) for k, v in value.items()
                )
            ),
        )
    try:
        hash(value)
    except TypeError:
        return (type(value), repr(value))
    return (type(value), value)


class Provenance:
    """A side channel mapping data values back to their source location."""

    __slots__ = ('_entries',)

    def __init__(self):
        self._entries = {}

    def record(self, namespace, symbol, key, origin):
        if origin is None:
            return
        self._entries[(namespace, symbol, struct_key(key))] = origin

    def get(self, namespace, symbol, key):
        return self._entries.get((namespace, symbol, struct_key(key)))

    def update(self, other):
        if other is not None:
            self._entries.update(other._entries)


ACTIVE_PROVENANCE = contextvars.ContextVar('pyomo_dataportal_provenance', default=None)
ACTIVE_NAMESPACE = contextvars.ContextVar('pyomo_dataportal_namespace', default=None)
ACTIVE_ORIGIN = contextvars.ContextVar('pyomo_dataportal_origin', default=None)
ACTIVE_EXTERNAL_TABLE = contextvars.ContextVar(
    'pyomo_dataportal_external_table', default=False
)
ACTIVE_ROW_ADDRESSABLE = contextvars.ContextVar(
    'pyomo_dataportal_row_addressable', default=False
)


def enter_external_table(filename, row_addressable=True):
    """Mark the current processing context as an external table load.

    ``row_addressable`` indicates that each decoded record corresponds to
    exactly one table row (formats ``set`` and ``table``).  Matrix formats
    (``array``/``set_array``/``transposed_array``) do not have that
    property, so per-value row provenance is suppressed for them.
    """
    tokens = [
        (ACTIVE_EXTERNAL_TABLE, ACTIVE_EXTERNAL_TABLE.set(True)),
        (ACTIVE_ROW_ADDRESSABLE, ACTIVE_ROW_ADDRESSABLE.set(row_addressable)),
        (ACTIVE_ORIGIN, ACTIVE_ORIGIN.set(Origin(filename, 'row'))),
    ]
    return tokens


def row_addressable():
    return ACTIVE_EXTERNAL_TABLE.get() and ACTIVE_ROW_ADDRESSABLE.get()


def set_source(filename=None, line=None):
    """Set the active data source information.

    Returns a list of ``(token, value)`` pairs that can be passed to
    :func:`restore_source` to reinstate the previous context.
    """
    return [(ACTIVE_ORIGIN, ACTIVE_ORIGIN.set(Origin(filename, 'line', line=line)))]


def restore_source(tokens):
    for var, token in reversed(tokens):
        var.reset(token)


def record_row_origin(symbol, key, row, filename=None):
    """Record that ``key`` was defined by ``row`` of an external table."""
    prov = ACTIVE_PROVENANCE.get()
    if prov is None:
        return
    if filename is None:
        active = ACTIVE_ORIGIN.get()
        filename = active.filename if active is not None else None
    prov.record(ACTIVE_NAMESPACE.get(), symbol, key, Origin(filename, 'row', row=row))


def record_origin(symbol, key):
    """Record the active command line as the source of a data value."""
    prov = ACTIVE_PROVENANCE.get()
    if prov is None:
        return
    active = ACTIVE_ORIGIN.get()
    if active is None:
        return
    prov.record(
        ACTIVE_NAMESPACE.get(),
        symbol,
        key,
        Origin(active.filename, 'line', line=active.line),
    )
