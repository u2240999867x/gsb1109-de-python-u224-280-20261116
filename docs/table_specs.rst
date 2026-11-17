Table specifications
--------------------

:meth:`~pyquery.pyquery.PyQuery.table_spec` turns a table into a
:class:`~pyquery.table.TableSpec`.  Unlike plain text extraction it keeps
the real grid and resolves every data cell against the complete header
relationship that decides its meaning::

    >>> from pyquery import PyQuery as pq
    >>> html = '''
    ... <table id="plans">
    ...   <thead>
    ...     <tr><th rowspan="2" scope="col">Plan</th>
    ...         <th colspan="2" scope="colgroup">Price</th></tr>
    ...     <tr><th scope="col">USD</th><th scope="col">EUR</th></tr>
    ...   </thead>
    ...   <tbody>
    ...     <tr><th scope="row">Basic</th><td>10</td><td>9</td></tr>
    ...   </tbody>
    ... </table>
    ... '''
    >>> spec = pq(html)('table').table_spec()
    >>> spec.cells[0].header_path
    ['Price', 'USD', 'Basic']
    >>> spec.cells[1].header_path
    ['Price', 'EUR', 'Basic']
    >>> [(c.row, c.column, c.colspan, c.rowspan) for c in spec.cells]
    [(2, 1, 1, 1), (2, 2, 1, 1)]

Header cells are not returned as data cells; they are available from
``spec.header_cells`` with their own grid span and role.  A data cell that
spans several grid slots reports every header covering those slots.

Each :class:`~pyquery.table.CellSpec` keeps the original element in
``node`` and its XPath in ``xpath``.  ``headers`` holds a
:class:`~pyquery.table.HeaderRef` for every header cell that contributes to
the cell's meaning, including ancestors and headers reached through
``rowspan``/``colspan``.  Every reference records:

``label``
    Normalized header text.
``basis``
    ``"declared"`` when ``headers`` or ``scope`` asserted the relation,
    ``"inferred"`` when the caller allowed a structural inference.
``axis``
    ``"column"`` or ``"row"``.
``scope``
    The declaring ``scope`` value, if any.
``span``
    ``(row_start, column_start, row_end, column_end)`` on the real grid.
``xpath`` / ``node``
    The original header cell.
``responsibility`` / ``responsibility_basis``
    What the header contributes (for example ``price``, ``currency`` or
    ``plan``) and whether that duty was declared or inferred.
``units`` / ``units_basis``
    Unit values declared with ``data-unit``, ``data-currency``, ``unit`` or
    ``currency``.

``CellSpec`` also exposes ``field``, ``units``, ``header_paths`` and
``record``.  ``record`` is a JSON-serializable mapping containing the value,
field duty, complete header path, header references, original source
coordinates and any conflicts that affect the cell::

    >>> record = spec.cells[0].record
    >>> record['value']
    '10'
    >>> record['source']['xpath']
    '/table/tbody/tr/td[1]'
    >>> [item['xpath'] for item in record['headers']]
    ['/table/thead/tr[1]/th[2]', '/table/thead/tr[2]/th[1]',
     '/table/tbody/tr/th']

Declarations win.  Inference is off by default and can be enabled only for
the axes the caller trusts::

    >>> plain = '<table><tr><th>Price</th></tr><tr><td>10</td></tr></table>'
    >>> pq(plain)('table').table_spec().cells[0].header_path
    []
    >>> pq(plain)('table').table_spec(infer='column').cells[0].header_path
    ['Price']
    >>> pq(plain)('table').table_spec(infer=True).cells[0].inferred_headers[0].basis
    'inferred'

Bad declarations are preserved rather than repaired.  ``spec.conflicts``
contains :class:`~pyquery.table.Conflict` entries for a ``headers``
reference without a target, a reference cycle, a duplicate ``id`` and for
two mutually exclusive headers attached to one cell::

    >>> broken = ('<table><tr><th>Price</th></tr>'
    ...           '<tr><td headers="missing">10</td></tr></table>')
    >>> spec = pq(broken)('table').table_spec()
    >>> spec.cells[0].header_path
    []
    >>> [(item.kind, item.headers) for item in spec.conflicts]
    [('missing-header', ['missing'])]

With inference enabled the missing responsibility may be filled from the
structure, but the broken declaration is still reported::

    >>> spec = pq(broken)('table').table_spec(infer=True)
    >>> spec.cells[0].header_path
    ['Price']
    >>> [(item.kind, item.headers) for item in spec.conflicts]
    [('missing-header', ['missing'])]

A nested table is a separate specification.  Its headers never satisfy an
outer table's reference, even when both use the same ``id``.  All tables
below a selection can be read at once with
:meth:`~pyquery.pyquery.PyQuery.table_specs`::

    >>> len(pq('<div><table></table><table></table></div>')('div').table_specs())
    2

The module-level helpers :func:`~pyquery.table.parse_table` and
:func:`~pyquery.table.parse_tables` accept lxml elements directly.
