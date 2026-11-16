"""Split one CIF loop into two loops and merge the results back.

A loop is an ordered mapping of dataname to column values, the same shape used
by :class:`CifFile.StarFile.StarBlock`.

A split records which source row produced each target row.  That provenance is
what makes a merge exact.  Equal column lengths are never treated as evidence
that rows correspond.  When provenance is unavailable, merging falls back to
strictly unique key matching and reports duplicate keys as candidate groups
instead of constructing a Cartesian product.
"""

from __future__ import print_function
from __future__ import unicode_literals
from __future__ import division
from __future__ import absolute_import

from collections import OrderedDict


class LoopSplitError(Exception):
    """Raised for malformed split or merge requests."""

    def __init__(self, message, ambiguities=None):
        Exception.__init__(self, message)
        self.message = message
        self.ambiguities = tuple(ambiguities or ())

    def __str__(self):
        return self.message


def _as_key(key):
    if isinstance(key, (list, tuple)):
        return tuple(key)
    return (key,)


class RowCandidate(object):
    """One row that could take part in an ambiguous key correspondence."""

    def __init__(self, side, row, key, values=None):
        self.side = side
        self.row = row
        self.key = _as_key(key)
        self.values = dict(values or {})

    def as_dict(self):
        return {
            'side': self.side,
            'row': self.row,
            'key': list(self.key),
            'values': dict(self.values),
        }

    def __eq__(self, other):
        if isinstance(other, RowCandidate):
            return (self.side == other.side and self.row == other.row and
                    self.key == other.key and self.values == other.values)
        return NotImplemented

    def __repr__(self):
        return 'RowCandidate({!r}, {!r}, {!r})'.format(
            self.side, self.row, self.key)


class AmbiguityGroup(object):
    """Rows sharing one key value that cannot be paired uniquely.

    The group lists the candidate rows on each side.  It does not expose every
    possible cross-side pairing: resolving the ambiguity requires explicit row
    correspondence or a narrower key.
    """

    def __init__(self, key, first_rows, second_rows, reason='duplicate-key'):
        self.key = _as_key(key)
        self.first_rows = tuple(first_rows)
        self.second_rows = tuple(second_rows)
        self.reason = reason

    @property
    def candidates(self):
        return OrderedDict([
            ('first', self.first_rows),
            ('second', self.second_rows),
        ])

    def candidate_rows(self):
        return tuple(self.first_rows) + tuple(self.second_rows)

    def as_dict(self):
        return {
            'key': list(self.key),
            'reason': self.reason,
            'first_candidates': [a.as_dict() for a in self.first_rows],
            'second_candidates': [a.as_dict() for a in self.second_rows],
        }

    def __len__(self):
        return len(self.first_rows) + len(self.second_rows)

    def __eq__(self, other):
        if isinstance(other, AmbiguityGroup):
            return (self.key == other.key and
                    self.first_rows == other.first_rows and
                    self.second_rows == other.second_rows and
                    self.reason == other.reason)
        return NotImplemented

    def __repr__(self):
        return 'AmbiguityGroup({!r}, first={}, second={})'.format(
            self.key, len(self.first_rows), len(self.second_rows))


class RowOrigin(object):
    """The source row that produced one target row."""

    def __init__(self, side, target_row, source_row, key):
        self.side = side
        self.target_row = target_row
        self.source_row = source_row
        self.key = _as_key(key)

    def as_dict(self):
        return {
            'side': self.side,
            'target_row': self.target_row,
            'source_row': self.source_row,
            'key': list(self.key),
        }

    def __eq__(self, other):
        if isinstance(other, RowOrigin):
            return (self.side == other.side and
                    self.target_row == other.target_row and
                    self.source_row == other.source_row and
                    self.key == other.key)
        return NotImplemented

    def __repr__(self):
        return 'RowOrigin({!r}, target={}, source={})'.format(
            self.side, self.target_row, self.source_row)


class LoopSplitResult(object):
    """The two loops produced by :func:`split_loop` plus row provenance."""

    def __init__(self, source_columns, first_loop, second_loop, key_columns,
                 first_names, second_names, provenance, row_map,
                 first_sources, second_sources):
        self.source_columns = source_columns
        self.first_loop = first_loop
        self.second_loop = second_loop
        self.key_columns = tuple(key_columns)
        self.first_names = dict(first_names)
        self.second_names = dict(second_names)
        self.provenance = provenance
        self.row_map = OrderedDict(row_map)
        self.first_sources = dict(first_sources)
        self.second_sources = dict(second_sources)
        self.ambiguities = ()
        self.duplicate_keys = ()
        self._reversible = False
        self._basis = 'unverified'
        self._roundtrip_columns = None

    @property
    def first(self):
        """Alias for :attr:`first_loop`."""
        return self.first_loop

    @property
    def second(self):
        """Alias for :attr:`second_loop`."""
        return self.second_loop

    @property
    def first_provenance(self):
        return self.provenance['first']

    @property
    def second_provenance(self):
        return self.provenance['second']

    @property
    def source_row_count(self):
        return _column_length(self.source_columns)

    @property
    def mode(self):
        both = one = 0
        for targets in self.row_map.values():
            if targets[0] is not None and targets[1] is not None:
                both += 1
            elif targets[0] is not None or targets[1] is not None:
                one += 1
        if both and not one:
            return 'project'
        if one and not both:
            return 'partition'
        if not both and not one:
            return 'empty'
        return 'mixed'

    @property
    def reversible(self):
        """True only when the provenance reconstructs the source exactly."""
        return self._reversible

    @property
    def basis(self):
        return self._basis

    def duplicate_key_groups(self):
        """Return key groups containing more than one row on either side."""
        return self.duplicate_keys

    def merge_back(self, **kwargs):
        """Merge using this result's provenance.  See :func:`merge_split`."""
        return merge_split(self, **kwargs)

    def as_dict(self):
        return {
            'first_loop': _copy_columns(self.first_loop),
            'second_loop': _copy_columns(self.second_loop),
            'key_columns': list(self.key_columns),
            'provenance': OrderedDict(
                (side, [a.as_dict() for a in rows])
                for side, rows in self.provenance.items()),
            'mode': self.mode,
            'reversible': self.reversible,
            'basis': self.basis,
            'duplicate_keys': [a.as_dict() for a in self.duplicate_keys],
        }


class MergeResult(object):
    """The outcome of merging two split loops."""

    def __init__(self, columns, provenance, ambiguities, unresolved,
                 reversible, basis, source_row_count, restored_rows,
                 unresolved_rows, source_columns=None):
        self.columns = columns
        self.provenance = provenance
        self.ambiguities = tuple(ambiguities)
        self.unresolved = tuple(unresolved)
        self.reversible = bool(reversible)
        self.basis = basis
        self.source_row_count = source_row_count
        self.restored_rows = tuple(restored_rows)
        self.unresolved_rows = tuple(unresolved_rows)
        self.complete_rows = tuple(restored_rows)
        self.source_columns = source_columns

    @property
    def complete(self):
        return (not self.ambiguities and not self.unresolved and
                len(self.complete_rows) == self.source_row_count)

    def as_dict(self):
        return {
            'columns': _copy_columns(self.columns),
            'ambiguities': [a.as_dict() for a in self.ambiguities],
            'unresolved': list(self.unresolved),
            'restored_rows': list(self.restored_rows),
            'complete_rows': list(self.complete_rows),
            'unresolved_rows': list(self.unresolved_rows),
            'reversible': self.reversible,
            'basis': self.basis,
        }


def _copy_columns(columns):
    return OrderedDict((name, list(values)) for name, values in columns.items())


def _column_length(columns):
    if not columns:
        return 0
    return len(next(iter(columns.values())))


def _normalise_columns(columns, label):
    if columns is None:
        return OrderedDict()
    result = OrderedDict()
    lengths = set()
    for name, values in columns.items():
        if isinstance(values, (str, bytes)):
            raise LoopSplitError(
                '{} column {} must be a sequence of values'.format(label, name))
        try:
            values = list(values)
        except TypeError:
            raise LoopSplitError(
                '{} column {} must be a sequence of values'.format(label, name))
        result[name] = values
        lengths.add(len(values))
    if len(lengths) > 1:
        raise LoopSplitError(
            '{} columns have different lengths: {}'.format(
                label, repr(sorted(lengths))))
    return result


def _normalise_names(names, available, label):
    if names is None:
        names = {}
    unknown = [name for name in names if name not in available]
    if unknown:
        raise LoopSplitError(
            '{} names refer to unknown source columns: {}'.format(
                label, repr(unknown)))
    result = OrderedDict()
    for name in available:
        result[name] = names.get(name, name)
    outputs = list(result.values())
    if len(set(outputs)) != len(outputs):
        raise LoopSplitError(
            '{} output column names are not unique: {}'.format(
                label, repr(outputs)))
    return result


def _normalise_row_map(row_map, source_row_count):
    """Return an OrderedDict mapping source row to (first_row, second_row)."""
    if row_map is None:
        return OrderedDict((row, (row, row))
                           for row in range(source_row_count))

    entries = []
    if isinstance(row_map, dict):
        for source_row, targets in row_map.items():
            entries.append((source_row, targets))
    else:
        for position, item in enumerate(row_map):
            if not isinstance(item, (list, tuple)):
                raise LoopSplitError(
                    'row_map entries must be tuples, got {!r}'.format(item))
            if len(item) == 2:
                entries.append((position, item))
            elif len(item) == 3:
                entries.append((item[0], item[1:]))
            else:
                raise LoopSplitError(
                    'row_map entries must have 2 or 3 items, got {!r}'.format(item))

    mapping = OrderedDict()
    for source_row, targets in entries:
        if not isinstance(source_row, int):
            raise LoopSplitError(
                'row_map source rows must be integers: {!r}'.format(source_row))
        if source_row < 0 or source_row >= source_row_count:
            raise LoopSplitError(
                'row_map source row {} is outside 0..{}'.format(
                    source_row, source_row_count - 1))
        if not isinstance(targets, (list, tuple)) or len(targets) != 2:
            raise LoopSplitError(
                'row_map value for source row {} must be (first, second)'.format(
                    source_row))
        first_row, second_row = targets
        for side, target in (('first', first_row), ('second', second_row)):
            if target is not None and (not isinstance(target, int) or target < 0):
                raise LoopSplitError(
                    'row_map {} target for source row {} must be a '
                    'non-negative integer or None'.format(side, source_row))
        mapping[source_row] = (first_row, second_row)

    used = {'first': {}, 'second': {}}
    for source_row, targets in mapping.items():
        for side, target in zip(('first', 'second'), targets):
            if target is None:
                continue
            if target in used[side]:
                raise LoopSplitError(
                    'row_map assigns {} row {} to source rows {} and {}'.format(
                        side, target, used[side][target], source_row))
            used[side][target] = source_row
    return mapping


def _side_rows(row_map, position):
    rows = [(targets[position], source_row)
            for source_row, targets in row_map.items()
            if targets[position] is not None]
    rows.sort(key=lambda item: item[0])
    return OrderedDict((compact_row, source_row)
                       for compact_row, (target_row, source_row)
                       in enumerate(rows))


def _build_side(source_columns, source_rows, selected, key_columns, names):
    ordered = list(key_columns)
    for name in selected:
        if name not in ordered:
            ordered.append(name)
    result = OrderedDict()
    for source_name in ordered:
        result[names[source_name]] = [
            source_columns[source_name][source_row]
            for source_row in source_rows.values()]
    return result


def _hashable(value):
    """Return a hashable form of a CIF value, including list-valued cells."""
    if isinstance(value, (list, tuple)):
        return tuple(_hashable(a) for a in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    return value


def _row_keys(columns, names):
    length = _column_length(columns)
    return [tuple(_hashable(columns[name][row]) for name in names)
            for row in range(length)]


def _group_rows(keys):
    groups = OrderedDict()
    for row, key in enumerate(keys):
        groups.setdefault(key, []).append(row)
    return groups


def _candidates(side, rows, keys, columns):
    return [RowCandidate(side, row, keys[row],
                         dict((name, values[row])
                              for name, values in columns.items()))
            for row in rows]


def _duplicate_groups(first_columns, first_key_names,
                      second_columns, second_key_names):
    first_keys = _row_keys(first_columns, first_key_names)
    second_keys = _row_keys(second_columns, second_key_names)
    first_groups = _group_rows(first_keys)
    second_groups = _group_rows(second_keys)
    groups = []
    for key in _ordered_key_union(first_groups, second_groups):
        first_rows = first_groups.get(key, [])
        second_rows = second_groups.get(key, [])
        if len(first_rows) > 1 or len(second_rows) > 1:
            groups.append(AmbiguityGroup(
                key,
                _candidates('first', first_rows, first_keys, first_columns),
                _candidates('second', second_rows, second_keys, second_columns)))
    return tuple(groups)


def _ordered_key_union(first, second):
    seen = []
    seen_set = set()
    for key in list(first.keys()) + list(second.keys()):
        if key not in seen_set:
            seen.append(key)
            seen_set.add(key)
    return seen


def split_loop(source_columns, first_columns, second_columns, key_columns,
               first_names=None, second_names=None, row_map=None,
               verify=True):
    """Split one loop into two loops while recording source-row provenance.

    ``source_columns`` maps dataname to a column of values.  ``key_columns``
    names the columns forming the join key.  ``first_columns`` and
    ``second_columns`` name the non-key source columns belonging to each target
    loop; key columns are included in both loops.

    ``first_names`` and ``second_names`` map source datanames to output
    datanames.  They matter when both target loops live in one CIF block,
    because a dataname may belong to only one loop in a block.

    ``row_map`` is the explicit correspondence from source rows to target rows.
    It may be a mapping ``source_row -> (first_row, second_row)`` or a sequence
    of ``(first_row, second_row)`` pairs indexed by source row, or triples
    ``(source_row, first_row, second_row)``.  ``None`` means the source row is
    absent from that side.  The default is the identity projection: every
    source row appears at the same index in both loops.

    The return value is a :class:`LoopSplitResult`.  Its ``reversible`` flag is
    set only after a value-by-value reconstruction of the source loop.
    """
    source_columns = _normalise_columns(source_columns, 'source')
    if not source_columns:
        raise LoopSplitError('Source loop has no columns')
    source_row_count = _column_length(source_columns)
    key_columns = list(key_columns or [])
    if not key_columns:
        raise LoopSplitError('At least one key column is required')
    for name in key_columns:
        if name not in source_columns:
            raise LoopSplitError(
                'Key column {!r} is not in the source loop'.format(name))
    first_columns = list(first_columns or [])
    second_columns = list(second_columns or [])
    for label, selected in (('first', first_columns), ('second', second_columns)):
        for name in selected:
            if name not in source_columns:
                raise LoopSplitError(
                    '{} column {!r} is not in the source loop'.format(
                        label, name))

    first_names = _normalise_names(first_names, source_columns, 'first')
    second_names = _normalise_names(second_names, source_columns, 'second')
    row_map = _normalise_row_map(row_map, source_row_count)
    first_rows = _side_rows(row_map, 0)
    second_rows = _side_rows(row_map, 1)

    first_loop = _build_side(source_columns, first_rows, first_columns,
                             key_columns, first_names)
    second_loop = _build_side(source_columns, second_rows, second_columns,
                              key_columns, second_names)

    first_key_names = [first_names[name] for name in key_columns]
    second_key_names = [second_names[name] for name in key_columns]
    first_sources = dict((output_name, source_name)
                         for source_name, output_name in first_names.items()
                         if output_name in first_loop)
    second_sources = dict((output_name, source_name)
                          for source_name, output_name in second_names.items()
                          if output_name in second_loop)

    provenance = OrderedDict()
    for side, rows in (('first', first_rows), ('second', second_rows)):
        provenance[side] = [
            RowOrigin(side, target_row, source_row,
                      [source_columns[name][source_row]
                       for name in key_columns])
            for target_row, source_row in rows.items()]

    result = LoopSplitResult(
        source_columns, first_loop, second_loop, key_columns,
        dict(zip(key_columns, first_key_names)),
        dict(zip(key_columns, second_key_names)),
        provenance, row_map, first_sources, second_sources)
    result.duplicate_keys = _duplicate_groups(
        first_loop, first_key_names, second_loop, second_key_names)
    if verify:
        merged = _merge_with_provenance(result, first_loop, second_loop)
        result._reversible = merged.reversible
        result._basis = merged.basis
        result._roundtrip_columns = merged.columns
    return result


def _merge_with_provenance(result, first_columns, second_columns,
                           skip_sides=()):
    source_names = list(result.source_columns.keys())
    row_count = result.source_row_count
    columns = OrderedDict((name, ['?'] * row_count) for name in source_names)
    filled = dict((name, [False] * row_count) for name in source_names)
    ambiguities = []
    unresolved = []
    assigned = {}

    for side, target_columns, sources in (
            ('first', first_columns, result.first_sources),
            ('second', second_columns, result.second_sources)):
        if side in skip_sides:
            continue
        origins = result.provenance[side]
        if len(origins) != _column_length(target_columns):
            unresolved.append(
                '{} provenance has {} rows but the loop has {}'.format(
                    side, len(origins), _column_length(target_columns)))
            continue
        key_names = result.first_names if side == 'first' else result.second_names
        for origin in origins:
            target_row = origin.target_row
            source_row = origin.source_row
            if target_row < 0 or target_row >= _column_length(target_columns):
                unresolved.append(
                    '{} provenance refers to missing row {}'.format(
                        side, target_row))
                continue
            if source_row < 0 or source_row >= row_count:
                unresolved.append(
                    '{} row {} claims source row {}'.format(
                        side, target_row, source_row))
                continue
            current_key = tuple(target_columns[key_names[name]][target_row]
                                for name in result.key_columns)
            recorded_key = tuple(result.source_columns[name][source_row]
                                 for name in result.key_columns)
            if current_key != recorded_key:
                ambiguities.append(AmbiguityGroup(
                    origin.key, (origin,), (), reason='stale-provenance'))
                continue
            marker = (side, source_row)
            if marker in assigned:
                ambiguities.append(AmbiguityGroup(
                    origin.key, (origin,), (), reason='duplicate-provenance'))
                continue
            assigned[marker] = target_row
            for output_name, values in target_columns.items():
                if output_name not in sources:
                    continue
                source_name = sources[output_name]
                value = values[target_row]
                if filled[source_name][source_row]:
                    if columns[source_name][source_row] != value:
                        ambiguities.append(AmbiguityGroup(
                            origin.key, (origin,), (),
                            reason='conflicting-provenance'))
                    continue
                columns[source_name][source_row] = value
                filled[source_name][source_row] = True

    for side in ('first', 'second'):
        if side in skip_sides:
            continue
        present = set(origin.source_row for origin in result.provenance[side])
        for source_row in result.row_map:
            if result.row_map[source_row][0 if side == 'first' else 1] is not None \
                    and source_row not in present:
                unresolved.append(
                    'source row {} has no {} row'.format(source_row, side))

    restored_rows = [row for row in range(row_count)
                     if any(filled[name][row] for name in filled)]
    complete_rows = [row for row in range(row_count)
                     if all(filled[name][row] for name in filled)]
    unresolved_rows = [row for row in range(row_count)
                       if row not in restored_rows]

    reversible = not ambiguities and not unresolved
    if reversible and len(complete_rows) != row_count:
        reversible = False
    if reversible:
        for name in source_names:
            if columns[name] != list(result.source_columns[name]):
                unresolved.append(
                    'column {} does not match the source values'.format(name))
                reversible = False
                break
    merged = MergeResult(columns, provenance_pairs(result, first_columns,
                                                  second_columns,
                                                  skip_sides),
                         ambiguities, unresolved, reversible,
                         'provenance-roundtrip', row_count, restored_rows,
                         unresolved_rows, result.source_columns)
    merged.complete_rows = tuple(complete_rows)
    return merged


def provenance_pairs(result, first_columns, second_columns, skip_sides=()):
    pairs = []
    for side, target_columns in (('first', first_columns),
                                 ('second', second_columns)):
        if side in skip_sides:
            continue
        origins = result.provenance[side]
        if len(origins) != _column_length(target_columns):
            continue
        for origin in origins:
            pairs.append((origin.source_row, side, origin.target_row))
    return pairs


def _merge_by_key(result, first_columns, second_columns, key_columns):
    first_key_names = [result.first_names[name] for name in key_columns]
    second_key_names = [result.second_names[name] for name in key_columns]
    first_keys = _row_keys(first_columns, first_key_names)
    second_keys = _row_keys(second_columns, second_key_names)
    first_groups = _group_rows(first_keys)
    second_groups = _group_rows(second_keys)

    source_names = list(result.source_columns.keys())
    rows = []
    ambiguities = []
    paired_both = 0
    for key in _ordered_key_union(first_groups, second_groups):
        first_rows = first_groups.get(key, [])
        second_rows = second_groups.get(key, [])
        if len(first_rows) > 1 or len(second_rows) > 1:
            ambiguities.append(AmbiguityGroup(
                key,
                _candidates('first', first_rows, first_keys, first_columns),
                _candidates('second', second_rows, second_keys, second_columns)))
            continue
        row = OrderedDict((name, None) for name in source_names)
        if first_rows:
            for output_name, source_name in result.first_sources.items():
                row[source_name] = first_columns[output_name][first_rows[0]]
        if second_rows:
            for output_name, source_name in result.second_sources.items():
                row[source_name] = second_columns[output_name][second_rows[0]]
        if first_rows and second_rows:
            paired_both += 1
        rows.append(row)

    columns = OrderedDict((name, []) for name in source_names)
    for row in rows:
        for name in source_names:
            columns[name].append('?' if row[name] is None else row[name])

    unresolved = []
    reversible = False
    if not ambiguities and result.source_columns:
        if paired_both != len(rows):
            unresolved.append(
                '{} rows were present on only one side'.format(
                    len(rows) - paired_both))
        for name in result.source_columns:
            if name not in columns or columns[name] != list(
                    result.source_columns[name]):
                unresolved.append(
                    'column {} does not match the source values'.format(name))
                break
        else:
            reversible = not unresolved
    return MergeResult(columns, [], ambiguities, unresolved, reversible,
                       'unique-key', _column_length(first_columns),
                       list(range(len(rows))), [], result.source_columns)


def merge_split(result, first_columns=None, second_columns=None,
                key_columns=None, use_provenance=True, allow_key_match=True):
    """Merge two split loops back into one loop.

    Provenance is used by default.  The result is marked reversible only when
    every source row is reconstructed and the reconstructed values equal the
    loop that was originally split.  If provenance is absent or incomplete,
    ``allow_key_match`` permits a fallback that pairs rows only when the key is
    unique on both sides.  Duplicate keys are returned as
    :class:`AmbiguityGroup` candidates and are never cross-multiplied.
    """
    if not isinstance(result, LoopSplitResult):
        raise LoopSplitError('merge_split requires a LoopSplitResult')
    first_columns = first_columns if first_columns is not None \
        else result.first_loop
    second_columns = second_columns if second_columns is not None \
        else result.second_loop
    first_columns = _normalise_columns(first_columns, 'first')
    second_columns = _normalise_columns(second_columns, 'second')
    key_columns = list(key_columns or result.key_columns)

    provenance_ok = (use_provenance and
                     len(result.provenance['first']) ==
                     _column_length(first_columns) and
                     len(result.provenance['second']) ==
                     _column_length(second_columns))
    if provenance_ok:
        return _merge_with_provenance(result, first_columns, second_columns)
    if not allow_key_match:
        return MergeResult(OrderedDict(), [], [], ['no usable provenance'],
                           False, 'provenance', result.source_row_count,
                           [], [], result.source_columns)
    return _merge_by_key(result, first_columns, second_columns, key_columns)


def match_rows(left_values, right_values, left_keys=None, right_keys=None):
    """Match rows by equal values without forming a Cartesian product.

    Returns ``(pairs, ambiguities, unmatched_left, unmatched_right)``.  A pair
    is ``(left_row, right_row, value)``.  Rows are paired only when the value
    occurs exactly once on each side; otherwise the candidate rows are
    returned as an :class:`AmbiguityGroup`.  ``match_rows`` never expands a
    duplicate side into speculative pairs.
    """
    left_values = list(left_values)
    right_values = list(right_values)
    left_keys = list(left_keys or [('?',)] * len(left_values))
    right_keys = list(right_keys or [('?',)] * len(right_values))
    left_groups = OrderedDict()
    right_groups = OrderedDict()
    for row, value in enumerate(left_values):
        left_groups.setdefault(_hashable(value), []).append(row)
    for row, value in enumerate(right_values):
        right_groups.setdefault(_hashable(value), []).append(row)

    pairs = []
    ambiguities = []
    unmatched_left = []
    unmatched_right = []
    for value in _ordered_key_union(left_groups, right_groups):
        left_rows = left_groups.get(value, [])
        right_rows = right_groups.get(value, [])
        if not right_rows:
            unmatched_left.extend(left_rows)
        elif not left_rows:
            unmatched_right.extend(right_rows)
        else:
            original = left_values[left_rows[0]]
            if len(left_rows) == 1:
                # One row on the left can only map to the rows on the right;
                # this is a forced projection, not a speculative product.
                for right_row in right_rows:
                    pairs.append((left_rows[0], right_row, original))
            elif len(right_rows) == 1:
                for left_row in left_rows:
                    pairs.append((left_row, right_rows[0], original))
            else:
                ambiguities.append(AmbiguityGroup(
                    (original,),
                    [RowCandidate('left', row, left_keys[row])
                     for row in left_rows],
                    [RowCandidate('right', row, right_keys[row])
                     for row in right_rows]))
    return pairs, ambiguities, unmatched_left, unmatched_right
