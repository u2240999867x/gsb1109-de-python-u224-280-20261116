Grouped `list` results
======================

``MPDClient.list`` supports grouping with the ``group`` keyword::

    list album group albumartist group date

MPD answers with a flat stream of ``Tag: value`` lines.  A group value is
typically printed once and then reused by following lines, so the response
does not repeat every upper-level field on every line.  Reconstructing the
rows therefore requires the group order of the request, not just the response
text.

``list_catalog`` returns a :class:`mpd.CatalogResult` instead of the plain
list of dictionaries that ``list`` returns::

    >>> result = client.list_catalog("album", "group", "albumartist", "group", "date")
    >>> for row in result.rows:
    ...     print(row.as_dict())

It is available on the synchronous client, on ``mpd.asyncio.MPDClient`` (as
a coroutine) and on ``mpd.twisted.MPDProtocol`` (as a deferred firing with the
same result object).

Row model
---------

Every row is backed by exactly one leaf line of the response.  Rows are never
built as the Cartesian product of the distinct values per level, so a value
that MPD did not actually emit for that line does not appear in the
reconstruction.

``row.groups`` maps every group tag to a tuple of
:class:`mpd.FieldOccurrence` objects (one per declaration seen for that tag).
Each occurrence carries:

``value``
    The value exactly as received.  Empty values stay ``""``.
``source``
    ``FieldSource.DECLARED`` if the value was declared in this row's block,
    ``FieldSource.INHERITED`` if it was inherited from an earlier declaration.
``line``
    The zero-based response line that declared the value.  For inherited
    values this points at the original declaration, which makes the chain
    traceable.

Use ``row.provenance(tag)`` for the first occurrence, or
``row.occurrences(tag)`` for all of them.  Repeated tags in a block
(multi-value tags) produce several occurrences and are returned as a list by
``row.as_dict()``.

Switching groups
----------------

Request groups are outermost first.  MPD prints them in reverse request order,
so the last requested group is the response's outermost column and the first
requested group is its innermost column.  A declaration of an outer group
resets every inner level at that boundary: after switching to a new outer
group, an inner value from the previous group is not carried over.

A level that was reset and never declared again is reported in ``row.missing``
and omitted from ``row.as_dict()``.  This differs from an explicitly empty
value, which remains present as ``""``.  ``row.is_missing(tag)`` and
``row.is_empty(tag)`` distinguish the two cases.

Because the mapping is derived from the request, two requests with different
group orders must be parsed separately.  ``list_catalog`` creates a fresh
parser for each call and never reuses implicit field positions.

Truncated responses
-------------------

If the connection ends before MPD's terminating ``OK``, the synchronous and
asynchronous clients raise :class:`mpd.CatalogTruncatedError`.  Rows that the
received lines already support are attached to the exception as
``error.result`` and are marked ``truncated``.  A trailing group context that
never received a leaf line is exposed as ``result.pending`` instead of being
turned into a row.

Lower-level API
---------------

The parser is transport independent:

* :func:`mpd.parse_grouped_list` parses an iterable of response lines.
* :class:`mpd.GroupedListParser` feeds lines incrementally with ``feed()`` and
  is finalised with ``finish(complete=...)``.
