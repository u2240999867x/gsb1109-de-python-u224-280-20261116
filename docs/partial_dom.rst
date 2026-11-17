Reasoning over partially collected DOM trees
--------------------------------------------

An RPA collector sometimes submits only a fragment of the real page together
with metadata describing which structural axes are already complete.  Running
an ordinary ``PyQuery(selector)`` on such a fragment cannot tell "the selector
matches nothing" apart from "the nodes that would decide the answer were not
collected".  The :mod:`pyquery.partial` module keeps the two apart.

A coverage description declares, per element, which axes are trustworthy:

``element``
    the element itself (tag, attributes, own text) is fully collected;
``attributes``
    its attribute set is complete even if the subtree is not;
``children``
    its element children are exactly those present, in their real order;
``subtree``
    the whole subtree rooted at the element is collected;
``siblings``
    the element and every one of its siblings were collected;
``document``
    the element is the document root and nothing exists outside the fragment.

Each collection rule returns one of three statuses.

``confirmed``
    the answer is already exact, on every legal completion of the page;
``possible``
    observed matches exist but missing structure may still change the result;
``unsupported``
    the selector uses a feature the engine cannot reason about, so no answer
    is fabricated from the residual tree.

Setup::

    >>> from lxml import etree
    >>> from pyquery.partial import (
    ...     PartialCoverage, evaluate_rule, evaluate_rules)

On a fully collected document every supported rule is confirmed and matches
plain PyQuery exactly::

    >>> doc = etree.fromstring(
    ...     '<root><ul><li class="a"/><li class="b"/></ul></root>')
    >>> full = PartialCoverage.whole_document(doc)
    >>> result = evaluate_rule(doc, 'li:nth-child(2)', full)
    >>> result.status
    'confirmed'
    >>> len(result.confirmed_matches)
    1

When the first list item was not collected, the ul's child list is explicitly
left incomplete.  Positional rules can no longer be confirmed, and the missing
preceding sibling is reported as structural evidence instead of being treated
as absent::

    >>> fragment = etree.fromstring(
    ...     '<root><ul><li class="b"/><li class="c"/></ul></root>')
    >>> ul = fragment[0]
    >>> coverage = PartialCoverage([
    ...     {'element': fragment,
    ...      'complete': ('element', 'attributes', 'children')},
    ...     {'element': ul, 'complete': ('element', 'attributes')},
    ...     {'element': ul[0], 'complete': 'subtree'},
    ...     {'element': ul[1], 'complete': 'subtree'},
    ... ])
    >>> result = evaluate_rule(fragment, 'li:nth-child(2)', coverage)
    >>> result.status
    'possible'
    >>> 'missing_preceding_sibling' in result.evidence
    True
    >>> 'incomplete_siblings' in result.evidence
    True

One missing preceding sibling shifts the index of every following element.
A single completion of the sibling lists is therefore shared by every rule in
a batch; conditions never each pick their own favourable completion.  Two
conditions that cannot hold in the same completed page produce no possible
match::

    >>> rules = evaluate_rules(
    ...     fragment,
    ...     ['li:nth-child(2):nth-child(3)', 'li:nth-child(2):nth-child(even)'],
    ...     coverage)
    >>> rules[0].possible_matches
    []
    >>> len(rules[1].possible_matches)
    2

Type, class and attribute rules keep their observed node-level matches even
when the overall result set is still open::

    >>> result = evaluate_rule(fragment, 'li.b', coverage)
    >>> len(result.confirmed_matches)
    1
    >>> result.status
    'possible'

An incomplete attribute list makes attribute based rules undecidable rather
than confirming an observed absence::

    >>> box = etree.fromstring('<root><div class="box">x</div></root>')
    >>> div = box[0]
    >>> attr_coverage = PartialCoverage([
    ...     {'element': box,
    ...      'complete': ('element', 'attributes', 'children')},
    ...     {'element': div, 'complete': 'children'}])
    >>> evaluate_rule(box, 'div[data-id]', attr_coverage).status
    'possible'

Supported selectors cover types, classes, id and attribute combinations, the
child/descendant/adjacent/sibling combinators, ``:not()`` (including nested
and complex arguments), ``:is()``/``:where()``, ``:has()``, the structural
position pseudo-classes (``:nth-child()``, ``:nth-of-type()`` and friends) and
the jQuery extensions PyQuery already understands (``:first``, ``:eq()``,
``:checked``, ...).  Anything outside this scope is reported explicitly::

    >>> evaluate_rule(doc, 'a:hover', full).status
    'unsupported'
    >>> evaluate_rule(doc, 'p::before', full).status
    'unsupported'

Once the missing DOM is supplied, :func:`verify_completion` runs the ordinary
PyQuery query on the completed page and checks that every partial match
survives, so the completed answer is identical to a full-page query.
