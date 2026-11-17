Evidence fragments
==================

When an extraction rule matches a page, a small copy of the page is often kept
for review.  The original page may also contain fields that must not be
retained.  ``pyquery.evidence`` builds a fragment that still answers the
queries used by the rule, in the same hit order, under those constraints.

Basic use
---------

.. code-block:: python

   from pyquery.evidence import build_evidence_fragment

   page = '''
   <div id="results">
     <article class="row" data-owner="alice@example.com">
       <h2 class="title">Public one</h2>
     </article>
     <article class="row" data-owner="bob@example.com">
       <h2 class="title">Secret two</h2>
     </article>
   </div>'''

   evidence = build_evidence_fragment(
       page,
       queries=['article:nth-child(1)', 'article.row:last', 'h2.title'],
       allowed_values=['results', 'row', 'title', 'article', 'h2',
                       'Public one'],
       budget=2048,
   )

   print(evidence.html)
   evidence.conflicts        # [] when every query can be reproduced
   evidence.evaluate('article.row:last')

``allowed_values`` lists the values that may be kept: text nodes, attribute
values, class tokens and ``style`` declarations.  Items can be exact strings,
compiled regular expressions or callables.  A mapping can set per-attribute
rules::

   allowed_values={
       'text': ['Public one'],
       'class': ['row', 'title'],
       'value': [re.compile(r'^[0-9]+$')],
   }

With ``allowed_values=None`` (the default) nothing is redacted and only the
size and match order are handled.  An empty list means "keep no data values":
everything is redacted but the structure stays queryable.

Redaction is applied before trimming.  Trimming only ever removes a node or a
value when doing so leaves every query's hit sequence unchanged; otherwise the
node is kept, redacted.

Structural placeholders
-----------------------

Removing a sibling can change positional selectors (``:nth-child()``,
``:nth-of-type()``, ``:first``, ``:last``, ``+``) and negation selectors such
as ``:not(:first-child)``.  When a node has to stay only to keep such a
position, it becomes a *structural placeholder*:

* it is marked with ``data-evidence-placeholder``;
* its tag is ``<evidence-placeholder>`` when that is safe, otherwise the
  original tag is kept (positional selectors can depend on it) and only the
  marker attribute is added;
* its content and attributes are removed, so it cannot expose a business
  value;
* it is filtered out by ``EvidenceFragment.evaluate()`` and
  ``query_evidence()``, so it is never returned as a real match.

``EvidenceFragment.placeholders`` records the tag, path and original tag of
every placeholder, so a reviewer can tell structure from data.

Conflicting queries
-------------------

Some queries can only match because of a value that may not be retained, for
example ``input[value="secret"]``, ``li.secret`` or
``p:contains("customer name")``.  Redacting that value changes the query
answer, so the fragment cannot reproduce it.

In that case the query is reported in ``EvidenceFragment.conflicts`` and its
hits are empty.  It is never counted as reproduced::

   evidence.reproduced_queries   # queries whose hit order is verified
   evidence.conflicts            # queries that must be reviewed separately

Reasons are reported in ``conflict_details``:

``confidential_value``
    The selector depends on a redacted value.
``invalid_selector``
    pyquery could not evaluate the selector.
``serialization_roundtrip``
    The serialized fragment no longer matches the verified tree.

Verification
------------

The builder verifies every query before and after trimming, then serializes
the fragment and re-parses it to compare the real hit sequence once more.  A
query is only listed as reproduced when its hits equal the original ones.
The size in ``EvidenceFragment.size`` is the UTF-8 byte length of
``EvidenceFragment.html``; ``budget_exceeded`` is set when the fragment could
not be reduced below ``budget``.

Empty elements such as ``<li></li>`` are always serialized with explicit end
tags.  ``lxml.html`` omits the end tag for empty ``li``/``td``/``option``
elements, and re-parsing such output would merge the next sibling into the
previous element, silently changing positional selectors.

Command line
------------

.. code-block:: bash

   python -m pyquery.evidence -i request.json -o result.json

``request.json`` describes one page:

.. code-block:: json

   {
     "html": "<div><p>keep</p><p>secret</p></div>",
     "queries": ["p:last"],
     "allowed_values": ["keep"],
     "budget": 4096
   }

The command prints ``EvidenceFragment.to_dict()`` and exits with status 1 when
any query conflicts.  Use ``--compact`` to print only the fragment HTML.
