Resolve Effective Styles
========================

A KML feature can get its appearance from several places at once. It can carry a
``styleUrl`` pointing at a shared ``Style`` or ``StyleMap``; it can contain inline ``Style``
or ``StyleMap`` children; and a ``StyleMap`` can point at other selectors, including another
``StyleMap``. ``fastkml.style_catalog`` resolves that layering once and keeps the provenance
of every individual attribute.

Build a catalog from any KML tree:

.. code-block:: pycon

    >>> from fastkml.enums import PairKey
    >>> from fastkml.styles import IconStyle, LineStyle, Pair, Style, StyleMap, StyleUrl
    >>> from fastkml.containers import Document
    >>> from fastkml.features import Placemark

    >>> normal = Style(id="normal", styles=[IconStyle(color="ff0000ff", scale=1.0)])
    >>> highlight = Style(id="highlight", styles=[IconStyle(color="ff00ff00")])
    >>> state_map = StyleMap(
    ...     id="states",
    ...     pairs=[
    ...         Pair(key=PairKey.normal, style=StyleUrl(url="#normal")),
    ...         Pair(key=PairKey.highlight, style=StyleUrl(url="#highlight")),
    ...     ],
    ... )
    >>> document = Document(id="ops", styles=[normal, highlight, state_map])
    >>> placemark = Placemark(
    ...     id="depot",
    ...     style_url=StyleUrl(url="#states"),
    ...     styles=[Style(styles=[LineStyle(width=3.0)])],
    ... )
    >>> document.append(placemark)

    >>> catalog = document.style_catalog()
    >>> entry = catalog[placemark]
    >>> entry.normal["IconStyle.color"]
    'ff0000ff'
    >>> entry.normal["IconStyle.scale"]
    1.0
    >>> entry.normal["LineStyle.width"]
    3.0
    >>> entry.highlight["IconStyle.color"]
    'ff00ff00'

Provenance
----------

``ResolvedStyle`` maps every ``"<SubStyle>.<attribute>"`` key to a ``StyleDeclaration``.
``entry.normal.source("IconStyle", "color")`` returns the ``StyleSource`` for that value.
The source records whether the value was ``inline`` or ``shared``, the winning ``Style`` and
``StyleMap`` ids, the ``styleUrl``, the mode, and the full reference chain.

``entry.normal.attributes`` gives all effective values at once, while ``sources``,
``provenance``, and ``values`` expose the same data grouped by declaration, key, or sub-style.
Nested values keep their own keys, e.g. ``IconStyle.icon.href`` and ``IconStyle.hot_spot.x``.

Catalog entries cover leaf features (``Placemark``, ``NetworkLink``, overlays), because those
are the objects a viewer draws. Containers are still walked so their shared styles are
indexed, and they remain available in ``catalog.containers`` / ``catalog.documents``.

Precedence
----------

Within one feature, layers are applied in this order, with later values winning
per attribute:

1. ``styleUrl`` on the feature, which may resolve to a ``Style`` or ``StyleMap``.
2. Inline ``StyleMap`` children of the feature, which select the normal/highlight pair.
3. Inline ``Style`` children of the feature, which are per-attribute overrides.

Both states are resolved independently: an inline override applies to normal and highlight,
while the values it does not mention still come from the state-specific shared declarations.
Inline children never replace the parsed ``styles`` list.

If a ``StyleMap`` has no usable highlight pair, the highlight state reuses the normal pair.
``ResolvedStyle.fallback_used`` is ``True`` and the reused declarations keep
``mode=normal`` so the report does not pretend they were declared for highlight.

Reference problems
------------------

Each feature is resolved once per state. Independent of the successful values, the catalog
collects three lists:

* ``cycles`` — a ``StyleMap`` that references itself or an ancestor in the same chain.
* ``external`` — a reference to another file. ``fastkml`` does not fetch remote styles, so
  the reference is reported rather than silently ignored.
* ``missing`` — a local ``styleUrl`` whose target does not exist in the feature's document.

Each issue has ``url``, ``target``, ``mode``, ``feature_id``, ``document_id``,
``source_style_id``/``source_style_map_id``, ``chain``, and a readable ``message``.
Resolution continues after a broken reference, so other valid layers still contribute.
``catalog.dangling`` combines ``external`` and ``missing`` when only unresolved targets are
interesting.

Document boundaries
-------------------

Lookup tables are per document. Two ``Document`` elements may both declare ``<Style
id="shared">``; a feature in one document sees only its own declaration. ``Folder`` styles
are addressable by features inside that folder's document. Features outside any ``Document``
share one synthetic scope rooted at the object passed to the catalog.

Because catalogs are snapshots, editing a style or switching the highlight state does not
change an existing catalog or the parsed declarations. Rebuild the catalog after edits. To
produce a modified copy, use ``ResolvedStyle.to_style()`` or
``catalog.highlight_style(placemark)``, which return detached ``Style`` objects.

Serialisation
-------------

``catalog.to_dict()`` turns the entire catalog, including provenance and issue lists, into
plain Python data. ``ResolvedStyle.to_dict()`` and ``StyleReferenceIssue.to_dict()`` do the
same for one feature state or one problem.
