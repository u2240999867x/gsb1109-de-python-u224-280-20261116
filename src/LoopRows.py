# Row-based storage for CIF loops.
#
# This module implements a "row warehouse" for CIF loop data.  In contrast
# to the column-based storage used by `StarBlock` (each dataname mapped to
# a list of values), a `LoopRows` object stores a loop as a sequence of
# rows, while preserving the ability to map every value back to its
# original dataname (column).
#
# Row identity is established from the actual token sequence of the source
# text together with the number of loop columns: row `i` owns the value
# tokens `[i*n, (i+1)*n)` where `n` is the number of columns.  Physical
# line breaks in the source text are never treated as row boundaries; a
# semicolon-delimited text field spanning many lines is a single value.
#
# An unquoted `?` (unknown) and an unquoted `.` (inapplicable) keep their
# distinct meanings (`CellKind.UNKNOWN` and `CellKind.INAPPLICABLE`).  The
# same characters inside quotes or text fields are ordinary values.
#
# If the number of value tokens is not a multiple of the column count,
# the complete rows form the provable prefix and the leftover tokens are
# exposed as an unassigned `tail`.  A short tail can indicate a missing
# unmarked value anywhere in the loop; without additional evidence this
# module does not guess which column or row such a value belongs to, and
# never inserts placeholder values.
#
# Callers may correct, filter or reorder rows by confirmed row identity;
# because storage is row-major, all columns of the loop stay aligned under
# these operations.  Reusing row identity across file versions requires
# explicit row keys (see `set_key` and `align_with`); identity is never
# stitched together from original row ordinals across files.
#
# To maximize python3/python2 compatibility
from __future__ import print_function
from __future__ import unicode_literals
from __future__ import division
from __future__ import absolute_import

from .yapps3_compiled_rt import NoMoreTokens

if isinstance(u"abc", str):   #Python 3
    unicode = str

# Token names that can begin a loop value, and the restrict sets used when
# scanning loop headers and loop values.  These mirror the peek sets of
# the generated parsers (YappsStarParser_1_1 / YappsStarParser_2_0), so
# the token sequence seen here is exactly the one the real parser sees.
_VALUE_STARTS_11 = ('data_value_1', 'start_sc_line')
_LOOPVALUES_PEEK_11 = _VALUE_STARTS_11 + (
    'LBLOCK', 'data_name', 'save_heading', 'save_end', 'END', 'data_heading')

_VALUE_STARTS_20 = ('data_value_1', 'triple_quote_data_value',
                    'single_quote_data_value', 'start_sc_line',
                    'o_s_b', 'o_c_b')
_LOOPVALUES_PEEK_20 = _VALUE_STARTS_20 + (
    'LBLOCK', 'data_name', 'save_end', 'save_heading', 'END', 'data_heading')

_OPEN_BRACKETS = ('o_s_b', 'o_c_b')
_CLOSE_BRACKETS = ('c_s_b', 'c_c_b')


class LoopRowsError(Exception):
    """Base class for loop row warehouse errors."""
    pass


class IncompleteLoopError(LoopRowsError):
    """Raised when an operation requires a complete loop but the value
    token count is not a multiple of the column count.  Carries the
    provable complete prefix (via `loop_rows`) and the unassigned tail."""

    def __init__(self, loop_rows):
        self.loop_rows = loop_rows
        self.complete_rows = loop_rows.row_count
        self.tail = loop_rows.tail
        first = self.tail[0]
        if first.line is not None:
            location = 'line %d' % first.line
        else:
            location = 'value token %d' % first.token_index
        msg = ('Loop over %s has %d value token(s) beyond the last complete '
               'row (first at %s). The %d complete row(s) are the provable '
               'prefix; the tail is left unassigned as its column and row '
               'membership cannot be determined without additional evidence.'
               % (repr(list(loop_rows.columns)), len(self.tail), location,
                  self.complete_rows))
        super(IncompleteLoopError, self).__init__(msg)


class RowKeyError(LoopRowsError):
    """Raised for problems with explicit row keys."""
    pass


class RowAlignmentError(LoopRowsError):
    """Raised when a row-wise operation would break column correspondence."""
    pass


class CellKind(object):
    """Classification of a single loop value."""
    VALUE = 'value'                    # an ordinary value
    UNKNOWN = 'unknown'                # unquoted `?`
    INAPPLICABLE = 'inapplicable'      # unquoted `.`


def _classify(value, quoted):
    """Assign a CellKind.  Only unquoted `?` and `.` carry their special
    meanings; quoted lookalikes are ordinary values."""
    if not quoted:
        if value == '?':
            return CellKind.UNKNOWN
        if value == '.':
            return CellKind.INAPPLICABLE
    return CellKind.VALUE


class Cell(object):
    """A single loop value together with its provenance.

    * `value` - the interpreted value (quotes and text-field delimiters removed)
    * `kind` - one of `CellKind.VALUE`, `CellKind.UNKNOWN`, `CellKind.INAPPLICABLE`
    * `quoted` - True if the source token was explicitly delimited (quotes,
      triple quotes or a semicolon text field)
    * `raw` - the exact source text of the token, or None if not text-derived
    * `line` - 1-based source line of the token, or None if not text-derived
    * `column_name` - the dataname this value belongs to, or None if the
      cell is part of an unassigned incomplete tail
    * `token_index` - position within the loop's value token sequence
    """
    __slots__ = ('value', 'kind', 'quoted', 'raw', 'line', 'column_name',
                 'token_index')

    def __init__(self, value, kind=None, quoted=False, raw=None, line=None,
                 column_name=None, token_index=None):
        self.value = value
        self.kind = kind if kind is not None else _classify(value, quoted)
        self.quoted = quoted
        self.raw = raw
        self.line = line
        self.column_name = column_name
        self.token_index = token_index

    @property
    def is_unknown(self):
        return self.kind == CellKind.UNKNOWN

    @property
    def is_inapplicable(self):
        return self.kind == CellKind.INAPPLICABLE

    def __repr__(self):
        return 'Cell(%r, kind=%s, column=%s)' % (self.value, self.kind,
                                                 self.column_name)


def _scanner_module(grammar):
    g = str(grammar).lower()
    if g in ('1.1', 'cif1', 'cif1.1'):
        from . import YappsStarParser_1_1
        return YappsStarParser_1_1
    if g in ('2.0', 'cif2', 'cif2.0', 'star2'):
        from . import YappsStarParser_2_0
        return YappsStarParser_2_0
    raise LoopRowsError('Unsupported grammar %r: use "1.1" or "2.0"' % grammar)


def _line_of(text, pos):
    return text.count('\n', 0, pos) + 1


def _decode_simple(module, is_cif2, name, raw):
    """Return (value, quoted) for a single non-compound value token."""
    if is_cif2:
        if name == 'single_quote_data_value':
            return module.stripstring(raw), True
        if name == 'triple_quote_data_value':
            return module.striptriple(raw), True
        return raw, False
    # CIF 1.1: quoted strings also arrive as data_value_1
    if raw[:1] in ("'", '"'):
        return module.stripstring(raw), True
    return raw, False


class _Cursor(object):
    """Single-pass cursor over the token stream of a yapps scanner."""

    def __init__(self, scanner):
        self.scanner = scanner
        self.pos = 0

    def peek(self, restrict=None):
        return self.scanner.token(self.pos, restrict)

    def advance(self):
        self.pos += 1


def _consume_text_field(cur, first_tok, module, text):
    """Consume a semicolon-delimited text field.  `first_tok` is the
    start_sc_line token at the cursor.  Returns a Cell.  The field may
    span many physical lines but is a single value token."""
    parts = [first_tok[3]]
    cur.advance()
    while True:
        try:
            tok = cur.peek(('end_sc_line', 'sc_line_of_text'))
        except NoMoreTokens:
            raise LoopRowsError('Unterminated text field at char %d'
                                % first_tok[0])
        parts.append(tok[3])
        cur.advance()
        if tok[2] == 'end_sc_line':
            break
    raw_all = ''.join(parts)
    return Cell(value=module.stripextras(raw_all), quoted=True, raw=raw_all,
                line=_line_of(text, first_tok[0]))


def _consume_bracket_expression(cur, first_tok, module, text, top_restrict):
    """Consume a CIF2 bracket expression (list or table) as a single
    value.  `first_tok` is the opening bracket token at the cursor."""
    begin = first_tok[0]
    depth = 1  # first_tok is the opening bracket
    cur.advance()
    end_pos = None
    while True:
        try:
            tok = cur.peek(top_restrict)
        except NoMoreTokens:
            raise LoopRowsError('Unterminated bracket expression at char %d'
                                % begin)
        name = tok[2]
        if name == 'start_sc_line':
            _consume_text_field(cur, tok, module, text)
            continue
        if name in _OPEN_BRACKETS:
            depth += 1
        elif name in _CLOSE_BRACKETS:
            depth -= 1
            if depth == 0:
                end_pos = tok[1]
                cur.advance()
                break
        cur.advance()
    raw_all = text[begin:end_pos]
    # A compound value keeps its source text as its value; it is never an
    # unquoted `?` or `.`.
    return Cell(value=raw_all, quoted=True, raw=raw_all,
                line=_line_of(text, begin))


def _consume_value(cur, first_tok, module, is_cif2, text, top_restrict):
    """Consume exactly one loop value starting with `first_tok`."""
    name = first_tok[2]
    if name == 'start_sc_line':
        return _consume_text_field(cur, first_tok, module, text)
    if name in _OPEN_BRACKETS:
        return _consume_bracket_expression(cur, first_tok, module, text,
                                           top_restrict)
    value, quoted = _decode_simple(module, is_cif2, name, first_tok[3])
    cur.advance()
    return Cell(value=value, quoted=quoted, raw=first_tok[3],
                line=_line_of(text, first_tok[0]))


def _scan_loop_tokens(text, grammar):
    """Scan `text` with the genuine PyCifRW scanner and yield
    (columns, cells) for every loop_ found.  `cells` is the flat value
    token sequence of the loop."""
    module = _scanner_module(grammar)
    is_cif2 = str(grammar).lower() in ('2.0', 'cif2', 'cif2.0', 'star2')
    if is_cif2:
        value_starts = _VALUE_STARTS_20
        loopvalues_peek = _LOOPVALUES_PEEK_20
    else:
        value_starts = _VALUE_STARTS_11
        loopvalues_peek = _LOOPVALUES_PEEK_11
    scanner = module.StarParserScanner(text)
    # At top level, text-field body tokens cannot legitimately appear;
    # excluding them keeps the scanner context-correct between loops.
    top_restrict = tuple(p for p, _ in scanner.patterns
                         if p not in ('sc_line_of_text', 'end_sc_line'))
    cur = _Cursor(scanner)
    pending = None  # terminator token already scanned for the outer level
    while True:
        if pending is not None:
            tok = pending
            pending = None
        else:
            try:
                tok = cur.peek(top_restrict)
            except NoMoreTokens:
                return
        if tok[2] == 'END':
            return
        if tok[2] == 'start_sc_line':
            _consume_text_field(cur, tok, module, text)
            continue
        if tok[2] in _OPEN_BRACKETS:
            _consume_bracket_expression(cur, tok, module, text, top_restrict)
            continue
        if tok[2] != 'LBLOCK':
            cur.advance()
            continue
        cur.advance()
        # Header and values are scanned under a single restrict set, so
        # every position is scanned exactly once.
        columns = []
        cells = []
        token_index = 0
        while True:
            try:
                tok = cur.peek(loopvalues_peek)
            except NoMoreTokens:
                break
            if tok[2] == 'data_name' and token_index == 0:
                columns.append(tok[3])
                cur.advance()
                continue
            if tok[2] in value_starts:
                cell = _consume_value(cur, tok, module, is_cif2, text,
                                      top_restrict)
                cell.token_index = token_index
                cells.append(cell)
                token_index += 1
                continue
            # A loop terminator: hand it back to the outer level without
            # rescanning this position.
            pending = tok
            break
        yield columns, cells


def scan_loops(text, grammar='1.1', key_columns=None):
    """Return a list of `LoopRows`, one per loop_ found in `text`.  Row
    identity follows the actual token sequence produced by the PyCifRW
    scanner for the requested grammar."""
    return [LoopRows.from_tokens(columns, cells, key_columns=key_columns,
                                 source='text')
            for columns, cells in _scan_loop_tokens(text, grammar)]


class LoopRow(object):
    """A confirmed row of a `LoopRows` warehouse.  Access values by
    dataname with `row[dataname]`; `row.key` is the explicit row key if
    keys have been set."""

    def __init__(self, loop_rows, index):
        self._loop_rows = loop_rows
        self.index = index

    @property
    def key(self):
        keys = self._loop_rows._keys
        if keys is None:
            return None
        return keys[self.index]

    @property
    def cells(self):
        return tuple(self._loop_rows._rows[self.index])

    @property
    def line(self):
        """Source line of the row's first token, or None."""
        return self._loop_rows._rows[self.index][0].line

    @property
    def first_token_index(self):
        """Index of this row's first value token in the loop's token
        sequence; equal to `index * column_count` by construction."""
        return self._loop_rows._rows[self.index][0].token_index

    def cell(self, dataname):
        j = self._loop_rows._column_index(dataname)
        return self._loop_rows._rows[self.index][j]

    def __getitem__(self, dataname):
        return self.cell(dataname).value

    def get(self, dataname, default=None):
        try:
            return self[dataname]
        except KeyError:
            return default

    def kind(self, dataname):
        return self.cell(dataname).kind

    def as_dict(self):
        return dict((name, self._loop_rows._rows[self.index][j].value)
                    for j, name in enumerate(self._loop_rows.columns))

    def __repr__(self):
        return 'LoopRow(index=%d, key=%r)' % (self.index, self.key)


class Alignment(object):
    """Result of `LoopRows.align_with`: key-based correspondence between
    two loop versions.  `self_positions`/`other_positions` map each common
    key to its row index in the respective version; `only_in_self` and
    `only_in_other` hold keys present in one version only."""

    def __init__(self, self_positions, other_positions, only_in_self,
                 only_in_other):
        self.self_positions = self_positions
        self.other_positions = other_positions
        self.only_in_self = frozenset(only_in_self)
        self.only_in_other = frozenset(only_in_other)

    @property
    def common(self):
        return frozenset(self.self_positions)

    def __len__(self):
        return len(self.self_positions)

    def __repr__(self):
        return ('Alignment(common=%d, only_in_self=%d, only_in_other=%d)'
                % (len(self.common), len(self.only_in_self),
                   len(self.only_in_other)))


class LoopRows(object):
    """Row-based storage for one CIF loop.

    Rows are stored row-major; every row keeps its cells aligned with
    `columns`, so corrections, filtering and reordering by confirmed row
    identity preserve the correspondence of all columns.  Use
    `to_columns()` or `apply_to_block()` to map rows back to the original
    datanames.
    """

    def __init__(self, columns, rows, tail=(), key_columns=None,
                 source=None):
        if len(columns) == 0:
            raise LoopRowsError('A loop must have at least one column')
        self._columns = list(columns)
        self._rows = [list(row) for row in rows]
        self._tail = list(tail)
        self.source = source
        self._key_columns = None
        self._keys = None
        self._key_index = None
        if key_columns is not None:
            self.set_key(key_columns)

    # -- construction ----------------------------------------------------

    @classmethod
    def from_tokens(cls, columns, cells, key_columns=None, source=None):
        """Build a warehouse from the loop's datanames and its flat value
        token sequence.  Row `i` owns tokens `[i*n, (i+1)*n)`; leftover
        tokens that do not fill a row become the unassigned `tail`.  No
        placeholder values are invented for short loops."""
        n = len(columns)
        if n == 0:
            raise LoopRowsError('A loop must have at least one column')
        complete = len(cells) // n
        rows = []
        for i in range(complete):
            row = list(cells[i * n:(i + 1) * n])
            for j, cell in enumerate(row):
                cell.column_name = columns[j]
                if cell.token_index is None:
                    cell.token_index = i * n + j
            rows.append(row)
        tail = list(cells[complete * n:])
        for k, cell in enumerate(tail):
            # Tail membership is undetermined: a short tail may reflect a
            # missing unmarked value anywhere in the loop, so these cells
            # are deliberately not assigned to any column or row.
            cell.column_name = None
            if cell.token_index is None:
                cell.token_index = complete * n + k
        return cls(columns, rows, tail=tail, key_columns=key_columns,
                   source=source)

    @classmethod
    def from_text(cls, text, grammar='1.1', loop=0, key_columns=None):
        """Build a warehouse from CIF text.  `loop` selects the loop: an
        integer position among the loops in the text, or the name of one
        of its columns."""
        loops = list(_scan_loop_tokens(text, grammar))
        if not loops:
            raise LoopRowsError('No loop_ found in text')
        if isinstance(loop, unicode):
            wanted = loop.lower()
            matches = [i for i, (cols, _) in enumerate(loops)
                       if wanted in [c.lower() for c in cols]]
            if not matches:
                raise LoopRowsError(
                    'No loop containing %s found in text' % repr(loop))
            loop = matches[0]
        try:
            columns, cells = loops[loop]
        except IndexError:
            raise LoopRowsError('Loop index %r out of range (%d loops found)'
                                % (loop, len(loops)))
        return cls.from_tokens(columns, cells, key_columns=key_columns,
                               source='text')

    @classmethod
    def from_block(cls, block, dataname, key_columns=None):
        """Build a warehouse from the loop containing `dataname` in a
        parsed block.  Note that parsing removes quoting information, so
        `?` and `.` values are taken at face value here; use `from_text`
        when quoted lookalikes must be distinguished."""
        try:
            names = block.GetLoopNames(dataname)
        except KeyError:
            raise LoopRowsError('%s is not in a loop' % repr(dataname))
        true_case = getattr(block, 'true_case', {})
        columns = [true_case.get(name, name) for name in names]
        colvalues = []
        for name in columns:
            vals = block[name]
            if not isinstance(vals, list):
                vals = [vals]
            colvalues.append(list(vals))
        lengths = set(len(v) for v in colvalues)
        if len(lengths) > 1:
            raise RowAlignmentError(
                'Loop columns %s have unequal lengths %s; row identity '
                'cannot be established' % (repr(columns), repr(lengths)))
        n = len(columns)
        count = lengths.pop() if lengths else 0
        cells = []
        for i in range(count):
            for j in range(n):
                cells.append(Cell(value=colvalues[j][i], quoted=False,
                                  token_index=i * n + j))
        return cls.from_tokens(columns, cells, key_columns=key_columns,
                               source='block')

    # -- basic properties --------------------------------------------------

    @property
    def columns(self):
        return tuple(self._columns)

    @property
    def column_count(self):
        return len(self._columns)

    @property
    def row_count(self):
        return len(self._rows)

    @property
    def tail(self):
        """Value tokens beyond the last complete row, unassigned to any
        column or row."""
        return tuple(self._tail)

    @property
    def complete(self):
        return len(self._tail) == 0

    def require_complete(self):
        """Raise `IncompleteLoopError` if the loop has an unassigned tail."""
        if self._tail:
            raise IncompleteLoopError(self)

    def __len__(self):
        return len(self._rows)

    def __iter__(self):
        return iter([LoopRow(self, i) for i in range(len(self._rows))])

    def _column_index(self, dataname):
        lowered = dataname.lower()
        for j, name in enumerate(self._columns):
            if name.lower() == lowered:
                return j
        raise KeyError('%s is not a column of this loop' % repr(dataname))

    # -- row identity and keys -------------------------------------------

    @property
    def has_keys(self):
        return self._keys is not None

    @property
    def key_columns(self):
        return tuple(self._key_columns) if self._key_columns else None

    @property
    def keys(self):
        if self._keys is None:
            return None
        return list(self._keys)

    def set_key(self, columns):
        """Derive explicit row keys from one or more key columns.  Keys
        must be unique and may not be `?` or `.`; a single key column
        yields scalar keys, several columns yield tuple keys.  Explicit
        keys are the only supported way to carry row identity across
        file versions."""
        if isinstance(columns, unicode):
            columns = [columns]
        idxs = [self._column_index(c) for c in columns]
        keys = []
        for r, row in enumerate(self._rows):
            values = []
            for j in idxs:
                cell = row[j]
                if cell.kind in (CellKind.UNKNOWN, CellKind.INAPPLICABLE):
                    raise RowKeyError(
                        'Row %d: key cell %s is %r and cannot serve as an '
                        'explicit row key' % (r, repr(self._columns[j]),
                                              cell.value))
                values.append(cell.value)
            key = values[0] if len(values) == 1 else tuple(values)
            try:
                hash(key)
            except TypeError:
                raise RowKeyError('Row %d: key %r is not hashable'
                                  % (r, key))
            keys.append(key)
        seen = set()
        for key in keys:
            if key in seen:
                raise RowKeyError('Duplicate row key %r' % (key,))
            seen.add(key)
        self._key_columns = list(columns)
        self._keys = keys
        self._key_index = dict((key, i) for i, key in enumerate(keys))

    def row(self, ref):
        """Return the `LoopRow` for `ref`: an integer position within
        this warehouse, or an explicit row key if keys have been set."""
        if isinstance(ref, int):
            if not 0 <= ref < len(self._rows):
                raise RowKeyError('Row index %d out of range (%d rows)'
                                  % (ref, len(self._rows)))
            return LoopRow(self, ref)
        if self._key_index is None:
            raise RowKeyError(
                'No explicit row keys set; call set_key() before '
                'referring to rows by key')
        try:
            return LoopRow(self, self._key_index[ref])
        except KeyError:
            raise RowKeyError('Unknown row key %r' % (ref,))

    # -- row-wise operations (all columns stay aligned) -------------------

    def correct(self, ref, column, value):
        """Replace the value of `column` in the row identified by `ref`
        (key or position).  Only that cell changes; all other columns of
        the row keep their values.  Returns the previous value."""
        row = self.row(ref)
        j = self._column_index(column)
        cells = self._rows[row.index]
        old = cells[j]
        cells[j] = Cell(value=value, quoted=False, raw=None, line=old.line,
                        column_name=old.column_name,
                        token_index=old.token_index)
        if self._key_columns is not None:
            key_idxs = [self._column_index(c) for c in self._key_columns]
            if j in key_idxs:
                self.set_key(self._key_columns)  # re-derive and revalidate
        return old.value

    def _spawn(self, row_indices):
        new = LoopRows(self._columns, [self._rows[i] for i in row_indices],
                       tail=self._tail, source=self.source)
        if self._key_columns is not None:
            new.set_key(self._key_columns)
        return new

    def filter(self, predicate):
        """Return a new warehouse containing only rows for which
        `predicate(LoopRow)` is true.  Column correspondence is preserved
        by construction; any unassigned tail is carried over untouched."""
        keep = [i for i in range(len(self._rows))
                if predicate(LoopRow(self, i))]
        return self._spawn(keep)

    def reorder(self, refs):
        """Return a new warehouse with rows in the order given by `refs`.
        `refs` must be a permutation of the current row identities:
        explicit keys if keys are set, otherwise integer positions.  A
        permutation requirement guarantees no row is dropped, duplicated
        or invented while reordering."""
        refs = list(refs)
        if len(refs) != len(self._rows):
            raise RowAlignmentError(
                'Reorder list has %d entries for %d rows; reordering '
                'requires a complete permutation of row identities'
                % (len(refs), len(self._rows)))
        indices = []
        for ref in refs:
            if isinstance(ref, int):
                if self._keys is not None:
                    raise RowKeyError(
                        'Explicit row keys are set; reorder by key, not by '
                        'position')
                if not 0 <= ref < len(self._rows):
                    raise RowAlignmentError('Row index %d out of range'
                                            % ref)
                indices.append(ref)
            else:
                if self._key_index is None:
                    raise RowKeyError(
                        'No explicit row keys set; reorder by position or '
                        'call set_key() first')
                if ref not in self._key_index:
                    raise RowKeyError('Unknown row key %r' % (ref,))
                indices.append(self._key_index[ref])
        if sorted(indices) != list(range(len(self._rows))):
            raise RowAlignmentError(
                'Reorder list is not a permutation of the current row '
                'identities')
        return self._spawn(indices)

    def align_with(self, other):
        """Match rows of this warehouse against another version by
        explicit row key.  Both warehouses must have keys set; row
        identity is never stitched together from original row ordinals
        across files.  Returns an `Alignment`."""
        if self._keys is None or other._keys is None:
            raise RowKeyError(
                'Cross-version row identity requires explicit row keys on '
                'both versions; refusing to align by original row order')
        common = set(self._keys) & set(other._keys)
        return Alignment(
            self_positions=dict((k, self._key_index[k]) for k in common),
            other_positions=dict((k, other._key_index[k]) for k in common),
            only_in_self=set(self._keys) - set(other._keys),
            only_in_other=set(other._keys) - set(self._keys))

    # -- mapping back to columns ------------------------------------------

    def to_columns(self):
        """Return `{dataname: [value, ...]}` for the confirmed complete
        rows (the provable prefix).  An unassigned tail, if present, is
        not distributed over columns."""
        return dict((name, [row[j].value for row in self._rows])
                    for j, name in enumerate(self._columns))

    def apply_to_block(self, block):
        """Write the confirmed rows back into `block` under the original
        datanames.  Requires a complete loop (no unassigned tail).  If
        the columns already exist in the block they must form a single
        loop together; otherwise a new loop is created."""
        self.require_complete()
        coldata = self.to_columns()
        existing = [name for name in self._columns if name in block]
        if existing and len(existing) != len(self._columns):
            missing = [name for name in self._columns if name not in block]
            raise LoopRowsError(
                'Columns %s of this loop are missing from the target block'
                % repr(missing))
        if existing:
            loop_numbers = set(block.FindLoop(name) for name in self._columns)
            if loop_numbers == set([-1]):
                block.CreateLoop(list(self._columns))
            elif len(loop_numbers) != 1 or -1 in loop_numbers:
                raise LoopRowsError(
                    'Columns of this loop are not together in a single '
                    'loop in the target block')
            for name in self._columns:
                block[name] = coldata[name]
        else:
            for name in self._columns:
                block.AddItem(name, coldata[name])
            block.CreateLoop(list(self._columns))

    def __repr__(self):
        return ('LoopRows(columns=%r, rows=%d, tail=%d, complete=%r)'
                % (self._columns, len(self._rows), len(self._tail),
                   self.complete))
