Version-aware filters
=====================

``find`` and ``search`` changed language at MPD 0.21.  Older releases accept
only repeated ``TAG VALUE`` pairs, combined with AND: ``find`` compares
exactly and case sensitively, ``search`` matches case-insensitive substrings.
Neither can express OR or NOT.  MPD 0.21 added filter expressions such as
``(artist == "AC/DC")``, ``(title contains "live")``, ``(!(genre == "Pop"))``
and ``AND``/``OR`` grouping.

The planning rules below name the syntax each server generation accepts.
Anything outside that syntax becomes a candidate query plus local
verification rather than an unsupported command.

``mpd.filters`` keeps one condition and translates it for the connected
server.  ``MPDClient.find_filtered()`` (and its asyncio counterpart) returns a
:class:`~mpd.filters.FilterResult` instead of raw server rows.

Conditions
----------

A condition may be a filter expression string, a ``Filter`` tree, a
``{tag: value}`` mapping, or a ``(tag, value)`` pair::

    from mpd.filters import And, Not, Or, TagFilter

    client.find_filtered('(artist == "AC/DC")')
    client.find_filtered({"artist": "AC/DC", "album": "Powerage"})
    client.find_filtered(TagFilter("genre", "Rock", "contains"))
    client.find_filtered(
        And(
            Or(TagFilter("genre", "Rock"), TagFilter("genre", "Pop")),
            Not(TagFilter("album", "Mixed")),
        )
    )

``TagFilter`` supports ``==``, ``!=``, ``contains`` and ``starts_with``.
``==`` is case sensitive, mirroring ``find``; ``contains`` and
``starts_with`` are case insensitive, mirroring ``search``.  Pass
``ignore_case=False`` for a case-sensitive substring comparison.  On servers
with filter expressions but without ``starts_with``, the planner uses
``contains`` as a candidate and keeps the original comparison for local
verification.

A tag may occur multiple times on a song.  A positive comparison matches when
any value matches.  ``!=`` is the negation of the positive comparison, so it
means "no value equals this", not "some value differs".

Result states
-------------

``FilterResult`` separates three outcomes:

* ``confirmed``: the original condition is true.
* ``excluded``: it is false.
* ``undetermined``: a tag required by the condition was not present in the
  record returned by the server, so the condition cannot be decided.  These
  records are not silently dropped; check ``result.complete`` or
  ``result.undetermined_files``.

``complete`` is true when no returned record is undetermined.  ``authoritative``
additionally requires that the server query could not have hidden any record:
a narrowed candidate query is not authoritative even if every row it returned
could be classified.  Resolve undetermined records by querying the missing
tag or by using a full scan; do not treat them as exclusions.

``FilterResult.unsupported`` lists guarantees the server could not provide,
and ``guarantee`` is either ``"exact"`` or ``"candidate superset with local
verification"``.

How conditions are planned
~~~~~~~~~~~~~~~~~~~~~~~~~~

For MPD 0.21 and later, a condition that uses one case mode is sent as a
single native expression to ``find`` (case sensitive) or ``search`` (case
insensitive).  Conditions that mix both modes are split into a candidate query
and a local residual.

For older servers, exact comparisons use ``find`` and case-insensitive
substring comparisons use ``search``; multiple pairs are ANDed, as the
protocol defines.  A loose ``search`` is never used as a substitute for an
exact ``find``.  Mixed families use one family as a candidate and verify the
rest locally; OR becomes several candidate queries; NOT falls back to
``listallinfo``.  Every candidate query is a superset of the matches, and the
original condition is then evaluated locally.

If the server version is unknown, no version support is assumed: the client
uses a legacy candidate query plus local verification and reports that in
``unsupported``.  To require a complete server-side answer, pass
``require_server_only=True``; conditions that would need local verification
then raise :class:`~mpd.filters.FilterNotSupported` with the fallback plan
attached instead of returning unverified rows.

Escaping
--------

Values are escaped before being embedded in an expression: backslashes first,
then double quotes.  An escaped quote or backslash therefore stays inside the
value and cannot change the expression structure.

Replaying a directory
---------------------

``mpd.replay`` runs one condition against fixture records for several server
versions and compares the confirmed sets.  A fixture directory can contain
``*.json`` lists of song dictionaries, ``*.jsonl`` files, and ``*.mpd`` /
``*.txt`` protocol blocks::

    from mpd.filters import Not, TagFilter
    from mpd.replay import replay

    report = replay(
        Not(TagFilter("genre", "Rock")),
        "fixtures/",
        versions=("0.19.0", "0.21.0"),
    )
    assert report.equivalent
    print(report.as_dict())

``report.equivalent`` is true only when every version confirms the same files.
``report.unsupported`` names the versions whose server language could not
express the condition, and ``report.undetermined`` lists records that could
not be decided because a tag was missing.  Use
``require_server_only=True`` when a version without native support must fail
explicitly instead of falling back to local verification.
