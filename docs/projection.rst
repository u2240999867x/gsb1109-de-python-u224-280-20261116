Slot projection
===============

RPA captures often ship a static DOM together with a table of component
roots and a table describing which nodes are assigned to which
``<slot>``.  ``PyQuery.project()`` composes those tables into a display
tree without fetching anything or starting a browser.

The original DOM is never modified.  Two queryable views are returned:

``projection.original(selector=None)``
    The tree exactly as supplied; component roots are not merged.
    ``source()`` is an alias.

``projection.projected(selector=None)``
    The display tree; each component host is replaced by its root, and
    every slot contains its assigned nodes or, when nothing is assigned,
    its fallback content.  ``display()`` and ``rendered()`` are aliases.

::

    >>> from pyquery import PyQuery as pq
    >>> doc = pq('<div data-component="card">'
    ...          '<span slot="title" id="t">Hi</span></div>')
    >>> projection = doc.project(
    ...     components={'card': '<h1><slot name="title">Untitled</slot></h1>'},
    ...     assignments={'card': {'title': ['#t']}})
    >>> projection.complete
    True
    >>> projection.original('#t').text()
    'Hi'
    >>> projection.projected('h1').text()
    'Hi'

Accepted tables
---------------

``components`` (alias ``component_roots``) maps a component id to its
root markup.  The value may be markup, an element, a file-like object,
or a mapping with ``root`` / ``markup`` / ``html`` / ``content``::

    {'card': '<h1><slot name="title"></slot></h1>'}
    {'card': {'root': '<h1><slot name="title"></slot></h1>',
              'host': '.card'}}

A host is found by an explicit host selector, by a component attribute
(``data-component``, ``component``, ...), by a custom element tag
matching the component id, or by a matching ``id``.

``assignments`` (alias ``slot_assignments``) accepts several equivalent
shapes::

    {'card': {'title': ['#a', '#b']}}
    [{'component': 'card', 'slot': 'title', 'nodes': ['#a', '#b']}]
    [{'host': '.card', 'slot': 'title', 'nodes': ['#a']}]
    [('card', 'title', ['#a', '#b'])]

Node references may be CSS selectors, ``xpath:...`` expressions,
elements, or ``{'id'|'path'|'xpath'|'selector'|'node': ...}`` mappings.
A node is placed only once: if it is assigned to several slots, the
first slot receives it and later occurrences are reported.  A slot with
no resolved assignment shows its fallback content; a slot whose
assignment references an unknown node is *not* known to be empty, so it
stays empty and the reference is reported instead of showing fallback.

Correspondence
--------------

Every position can be mapped in both directions::

    >>> original = projection.original('#t')[0]
    >>> projected = projection.projected_of(original)
    >>> projection.original_of(projected) is original
    True
    >>> projection.position(original).projected_path
    'h1/slot/span'

``projection.correspondence`` maps every original node to its projected
node (``None`` when it is not displayed).  Nodes introduced by a
component root have no original counterpart; ``position()`` reports
them with ``origin='component'``.

Partial input
-------------

Composition never turns unconfirmed input into a complete page.  The
following are recorded and ``projection.complete`` becomes ``False``:

- a component root that cannot be read (``readable: False``, a missing
  value, a non-markup object, a failing stream);
- a reference to a node, component or slot that does not exist;
- the same node assigned more than once, directly or by containment;
- a component that instantiates itself, directly or indirectly.

The usable parts are still returned.  Details are available through
``projection.issues``, ``projection.conflicts``,
``projection.unresolved`` and ``projection.unreadable``.  An explicitly
empty root is different from an unreadable one: ``''`` is confirmed
empty, while a missing or unreadable root keeps the host unexpanded.

``project`` can also remember the projection on the document::

    >>> doc.project(components={'card': '<h1><slot></slot></h1>'},
    ...             attach=True)
    <Projection complete=True issues=0 conflicts=0 nodes=4>
    >>> doc.projected('h1').length
    1
