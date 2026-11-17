"""Build small, reviewable evidence fragments from an extracted page.

When an extraction rule matches a page, a small copy of that page often has to
be kept for review.  The original page may also contain values that are not
allowed to be retained.  :func:`build_evidence_fragment` builds a fragment in
which every requested CSS query returns the same elements, in the same order,
as it did on the original page, while disallowed values are redacted.

Two details are essential for a trustworthy fragment:

* Removing a sibling can change positional selectors (``:nth-child()``,
  ``:first``, ``+`` ...) and negation selectors.  Nodes that have to stay in
  the tree only to preserve such a position are turned into *structural
  placeholders*.  Placeholders are recognizable (``data-evidence-placeholder``
  and, where possible, the ``<evidence-placeholder>`` tag), carry no business
  content, and are filtered out by :func:`query_evidence` and
  :meth:`EvidenceFragment.evaluate`.
* Some queries need a value that is not allowed to be retained (for example
  ``input[value="secret"]`` or ``li:contains("secret")``).  Such a query cannot
  be reproduced faithfully.  It is listed in
  :attr:`EvidenceFragment.conflicts` instead of being answered with a
  different result.

The module can also be used from the command line::

    python -m pyquery.evidence -i request.json -o result.json

where ``request.json`` is ``{"html": "...", "queries": [...],
"allowed_values": [...], "budget": 4096}``.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from lxml import etree

from .pyquery import PyQuery

__all__ = [
    'EvidenceConflict',
    'EvidenceFragment',
    'EvidenceHit',
    'EvidencePlaceholder',
    'PLACEHOLDER_ATTR',
    'PLACEHOLDER_TAG',
    'PLACEHOLDER_VALUE',
    'REDACTED_TEXT',
    'REDACTED_VALUE',
    'build_evidence_fragment',
    'create_evidence_fragment',
    'generate_evidence_fragment',
    'is_placeholder',
    'make_evidence_fragment',
    'query_evidence',
    'real_nodes',
    'strip_placeholders',
]

PLACEHOLDER_TAG = 'evidence-placeholder'
PLACEHOLDER_ATTR = 'data-evidence-placeholder'
PLACEHOLDER_VALUE = '1'
REDACTED_TEXT = '[redacted]'
REDACTED_VALUE = 'evidence-redacted'

#: Boolean attributes whose value carries no business data.
PRESENCE_ATTRIBUTES = frozenset({
    'checked', 'disabled', 'hidden', 'multiple', 'open', 'readonly',
    'required', 'selected',
})

_VOID_TAGS = frozenset({
    'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
    'meta', 'param', 'source', 'track', 'wbr',
})


# ---------------------------------------------------------------------------
# Result objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EvidenceConflict:
    """A query that could not be reproduced under the confidentiality rules."""

    query: str
    reason: str
    detail: str = ''

    def __str__(self) -> str:
        return self.query

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.query == other
        if isinstance(other, EvidenceConflict):
            return ((self.query, self.reason, self.detail)
                    == (other.query, other.reason, other.detail))
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.query, self.reason, self.detail))

    def to_dict(self) -> dict[str, str]:
        return {'query': self.query, 'reason': self.reason,
                'detail': self.detail}


@dataclass(frozen=True)
class EvidencePlaceholder:
    """A structural placeholder that is not a business node."""

    tag: str
    path: tuple[int, ...]
    uid: int
    original_tag: str = ''
    marker_attr: str = PLACEHOLDER_ATTR

    def to_dict(self) -> dict[str, Any]:
        return {
            'tag': self.tag,
            'original_tag': self.original_tag,
            'path': list(self.path),
            'uid': self.uid,
            'marker_attr': self.marker_attr,
        }


@dataclass(frozen=True)
class EvidenceHit:
    """A real (non-placeholder) match in the generated fragment."""

    tag: str
    path: tuple[int, ...]
    uid: int

    def to_dict(self) -> dict[str, Any]:
        return {'tag': self.tag, 'path': list(self.path), 'uid': self.uid}


@dataclass
class EvidenceFragment:
    """The generated fragment, its verified hits and its conflicts."""

    html: str
    queries: tuple[str, ...] = ()
    query_hits: dict[str, tuple[EvidenceHit, ...]] = field(
        default_factory=dict)
    conflict_details: tuple[EvidenceConflict, ...] = ()
    placeholders: tuple[EvidencePlaceholder, ...] = ()
    budget: int | None = None
    size: int = 0
    budget_exceeded: bool = False
    marker_attr: str = PLACEHOLDER_ATTR
    placeholder_tag: str = PLACEHOLDER_TAG
    parser: str | None = None

    @property
    def fragment(self) -> str:
        return self.html

    @property
    def snippet(self) -> str:
        return self.html

    @property
    def conflicts(self) -> list[str]:
        """Queries that cannot be reproduced, ready to show to a reviewer."""
        return [conflict.query for conflict in self.conflict_details]

    @property
    def conflicting_queries(self) -> list[str]:
        return self.conflicts

    @property
    def conflict_queries(self) -> list[str]:
        return self.conflicts

    @property
    def reproduced_queries(self) -> list[str]:
        conflicting = set(self.conflicts)
        return [query for query in self.queries if query not in conflicting]

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflict_details)

    @property
    def within_budget(self) -> bool:
        return not self.budget_exceeded

    @property
    def ok(self) -> bool:
        return not self.conflict_details and not self.budget_exceeded

    def __str__(self) -> str:
        return self.html

    def __html__(self) -> str:
        return self.html

    def pyquery(self) -> PyQuery:
        if self.parser:
            return PyQuery(self.html, parser=self.parser)
        return PyQuery(self.html)

    def evaluate(self, selector: str) -> PyQuery:
        """Run *selector* on the fragment, excluding placeholders."""
        return query_evidence(self.html, selector,
                              marker_attr=self.marker_attr,
                              placeholder_tag=self.placeholder_tag,
                              parser=self.parser)

    def evaluate_all(self) -> dict[str, PyQuery]:
        return {query: self.evaluate(query) for query in self.queries}

    def to_dict(self) -> dict[str, Any]:
        return {
            'html': self.html,
            'size': self.size,
            'budget': self.budget,
            'budget_exceeded': self.budget_exceeded,
            'ok': self.ok,
            'queries': list(self.queries),
            'reproduced_queries': self.reproduced_queries,
            'conflicts': self.conflicts,
            'conflict_details': [conflict.to_dict()
                                 for conflict in self.conflict_details],
            'query_hits': {
                query: [hit.to_dict() for hit in hits]
                for query, hits in self.query_hits.items()
            },
            'placeholders': [placeholder.to_dict()
                             for placeholder in self.placeholders],
            'marker_attr': self.marker_attr,
            'placeholder_tag': self.placeholder_tag,
        }


# ---------------------------------------------------------------------------
# Convenience helpers
# ---------------------------------------------------------------------------

def is_placeholder(element: Any, marker_attr: str = PLACEHOLDER_ATTR,
                   placeholder_tag: str = PLACEHOLDER_TAG) -> bool:
    """Return True when *element* is a structural evidence placeholder."""
    attrib = getattr(element, 'attrib', None)
    if attrib is not None and marker_attr in attrib:
        return True
    tag = getattr(element, 'tag', None)
    return isinstance(tag, str) and tag == placeholder_tag


def strip_placeholders(elements: Iterable[Any],
                       marker_attr: str = PLACEHOLDER_ATTR,
                       placeholder_tag: str = PLACEHOLDER_TAG) -> list[Any]:
    """Return only real business nodes from *elements*."""
    return [element for element in elements
            if not is_placeholder(element, marker_attr, placeholder_tag)]


def real_nodes(elements: Iterable[Any],
               marker_attr: str = PLACEHOLDER_ATTR,
               placeholder_tag: str = PLACEHOLDER_TAG) -> list[Any]:
    """Alias for :func:`strip_placeholders`."""
    return strip_placeholders(elements, marker_attr, placeholder_tag)


def query_evidence(source: Any, selector: str,
                   marker_attr: str = PLACEHOLDER_ATTR,
                   placeholder_tag: str = PLACEHOLDER_TAG,
                   parser: str | None = None) -> PyQuery:
    """Query an evidence fragment without returning structural placeholders.

    ``source`` may be an :class:`EvidenceFragment`, an HTML string, a
    :class:`PyQuery` object or a list of elements.
    """
    if isinstance(source, EvidenceFragment):
        marker_attr = source.marker_attr
        placeholder_tag = source.placeholder_tag
        parser = source.parser
        source = source.html
    if isinstance(source, PyQuery):
        document = source
    elif isinstance(source, (str, bytes)):
        if parser:
            document = PyQuery(source, parser=parser)
        else:
            document = PyQuery(source)
    else:
        document = PyQuery(source)
    matches = [element for element in document(selector)
               if not is_placeholder(element, marker_attr, placeholder_tag)]
    return PyQuery(matches)


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def build_evidence_fragment(
    source: Any,
    queries: str | Iterable[str] | Mapping[str, Any],
    allowed_values: Iterable[Any] | Mapping[str, Any] | None = None,
    budget: int | None = None,
    *,
    max_bytes: int | None = None,
    forbidden_values: Iterable[Any] | Mapping[str, Any] | None = None,
    forbidden_attributes: Iterable[str] | None = None,
    marker_attr: str = PLACEHOLDER_ATTR,
    placeholder_tag: str = PLACEHOLDER_TAG,
    redacted_text: str = REDACTED_TEXT,
    redacted_value: str = REDACTED_VALUE,
    minimize: bool = True,
) -> EvidenceFragment:
    """Build a small evidence fragment.

    Parameters
    ----------
    source:
        HTML (``str``/``bytes``), an lxml element/tree, a :class:`PyQuery`
        object or a list of elements.
    queries:
        CSS selectors whose matches and match order must be preserved.
    allowed_values:
        Values that may be retained: text nodes, class tokens and attribute
        values.  Items may be exact strings, compiled regular expressions
        (used with ``.search``) or callables returning a truthy value.  A
        mapping gives rules per attribute name, e.g. ``{'text': [...],
        'value': [...], '*': [...]}``; ``'class'`` rules also apply to
        individual class tokens.  ``None`` keeps every value.
        Boolean attributes (:data:`PRESENCE_ATTRIBUTES`) always stay.
    budget:
        Maximum size of the serialized fragment in UTF-8 bytes.
    max_bytes:
        Alias for ``budget``.
    forbidden_values:
        Values that must never appear, even if they are also allowed.
    forbidden_attributes:
        Attribute names that are always removed.
    marker_attr:
        Attribute used to mark structural placeholders.
    placeholder_tag:
        Tag used for placeholders when changing the tag does not change a
        query result.
    redacted_text:
        Text used in place of disallowed text.
    redacted_value:
        Attribute value used in place of a disallowed attribute value.
    minimize:
        Remove or reduce content that is not needed by the queries
        (default true).

    Returns
    -------
    EvidenceFragment
        The fragment, verified hits per query, placeholders and conflicts.
    """
    if budget is None:
        budget = max_bytes
    elif max_bytes is not None and max_bytes != budget:
        raise ValueError('budget and max_bytes disagree')
    if budget is not None and budget < 0:
        raise ValueError('budget must be non-negative')

    query_list = _normalize_queries(queries)
    marker_attr = _safe_marker(marker_attr or PLACEHOLDER_ATTR, query_list)
    placeholder_tag = _safe_tag(placeholder_tag or PLACEHOLDER_TAG,
                                query_list)
    redacted_text = _safe_text(redacted_text or REDACTED_TEXT, query_list)
    redacted_value = _safe_value(redacted_value or REDACTED_VALUE, query_list)

    roots = _source_roots(source)
    if not roots:
        return EvidenceFragment(html='', queries=tuple(query_list),
                                budget=budget, size=0,
                                marker_attr=marker_attr,
                                placeholder_tag=placeholder_tag,
                                parser=None)

    # Never reuse a marker that already exists in the page: a real node with
    # the same tag or attribute would be indistinguishable from a placeholder.
    marker_attr = _collision_free_marker(marker_attr, roots)
    placeholder_tag = _collision_free_tag(placeholder_tag, roots)

    original_elements = _elements(roots)
    original_uid = {element: uid
                    for uid, element in enumerate(original_elements)}

    expected: dict[str, list[int]] = {}
    conflicts: list[EvidenceConflict] = []
    conflicted: set[str] = set()

    def add_conflict(query: str, reason: str, detail: str = '') -> None:
        if query not in conflicted:
            conflicted.add(query)
            conflicts.append(EvidenceConflict(query, reason, detail))

    for query in query_list:
        try:
            matches = _select(roots, query)
        except Exception as exc:
            add_conflict(query, 'invalid_selector',
                         f'{type(exc).__name__}: {exc}')
            expected[query] = []
            continue
        uids = []
        for element in matches:
            uid = original_uid.get(element)
            if uid is None:
                add_conflict(
                    query, 'invalid_selector',
                    'the selector returned a node outside the source tree')
                break
            uids.append(uid)
        else:
            expected[query] = uids
            continue
        expected[query] = []

    clone_roots = [copy.deepcopy(root) for root in roots]
    clone_uids = _clone_uid_map(roots, clone_roots)

    _Redactor(
        allowed_values=allowed_values,
        forbidden_values=forbidden_values,
        forbidden_attributes=forbidden_attributes,
        redacted_text=redacted_text,
        redacted_value=redacted_value,
        marker_attr=marker_attr,
    ).apply(clone_roots)

    state = _State(query_list, expected, conflicted, clone_roots, clone_uids,
                   marker_attr, placeholder_tag)
    protected = _protected_uids(expected, conflicted, clone_roots, clone_uids)

    # Redaction alone may already have changed an answer.  Such a query can
    # never be repaired by trimming, so record the real reason right away.
    for query in state.queries:
        if state.result_uids(query) != expected.get(query, []):
            add_conflict(
                query, 'confidential_value',
                'the selector matches on a value that cannot be retained')
    state = _State(query_list, expected, conflicted, clone_roots, clone_uids,
                   marker_attr, placeholder_tag)

    placeholder_original: dict[int, str] = {}
    if minimize:
        _minimize(state, protected, placeholder_original)

    html = _serialize(clone_roots)
    size = len(html.encode('utf-8'))

    if minimize and budget is not None and size > budget:
        # Only content that no query needs is dropped, and every removal is
        # verified, so this cannot change a query answer.
        hit_uids = {uid
                    for query, uids in expected.items()
                    if query not in conflicted
                    for uid in uids}
        _squeeze(state, hit_uids)
        html = _serialize(clone_roots)
        size = len(html.encode('utf-8'))

    parser = _fragment_parser(clone_roots)

    # Verify the fragment exactly as a reviewer will consume it: parse the
    # serialized HTML again and compare the real hit sequence with the one in
    # the tree that was verified during minimisation.
    parsed = _parse_fragment(html, parser)
    parsed_roots = list(parsed)
    clone_paths = {_path_key(element, clone_roots): uid
                   for element, uid in clone_uids.items()}
    clone_tags = {_path_key(element, clone_roots): _tag_name(element)
                  for element in clone_uids}

    for query in query_list:
        if query in conflicted:
            continue
        try:
            clone_hits = _hit_signatures(state, query)
            parsed_hits = _hit_signatures_from_document(parsed, query)
        except Exception as exc:
            add_conflict(query, 'invalid_selector',
                         f'{type(exc).__name__}: {exc}')
            continue
        if clone_hits != parsed_hits:
            add_conflict(
                query, 'serialization_roundtrip',
                'the serialized fragment no longer reproduces the original '
                'match order')

    # A budget squeeze must never silently change an answer.  If the fragment
    # is over budget, say so explicitly.
    budget_exceeded = bool(budget is not None and size > budget)

    final_hits: dict[str, tuple[EvidenceHit, ...]] = {}
    for query in query_list:
        if query in conflicted:
            final_hits[query] = ()
            continue
        matches = query_evidence(parsed, query, marker_attr=marker_attr,
                                 placeholder_tag=placeholder_tag)
        final_hits[query] = tuple(
            EvidenceHit(tag=_tag_name(element),
                        path=_path_key(element, parsed_roots),
                        uid=clone_paths.get(
                            _path_key(element, parsed_roots), -1))
            for element in matches
        )

    placeholders = []
    for element in _elements(parsed_roots):
        if is_placeholder(element, marker_attr, placeholder_tag):
            path = _path_key(element, parsed_roots)
            uid = clone_paths.get(path, -1)
            placeholders.append(EvidencePlaceholder(
                tag=_tag_name(element),
                path=path,
                uid=uid,
                original_tag=placeholder_original.get(
                    uid, clone_tags.get(path, '')),
                marker_attr=marker_attr,
            ))

    return EvidenceFragment(
        html=html,
        queries=tuple(query_list),
        query_hits=final_hits,
        conflict_details=tuple(conflicts),
        placeholders=tuple(placeholders),
        budget=budget,
        size=size,
        budget_exceeded=budget_exceeded,
        marker_attr=marker_attr,
        placeholder_tag=placeholder_tag,
        parser=parser,
    )


generate_evidence_fragment = build_evidence_fragment
make_evidence_fragment = build_evidence_fragment
create_evidence_fragment = build_evidence_fragment


# ---------------------------------------------------------------------------
# Verification state
# ---------------------------------------------------------------------------

class _State:
    def __init__(self, queries, expected, conflicted, roots, uid_map,
                 marker_attr, placeholder_tag):
        self.queries = [query for query in queries
                        if query not in conflicted]
        self.expected = expected
        self.roots = roots
        self.uid_map = uid_map
        self.marker_attr = marker_attr
        self.placeholder_tag = placeholder_tag

    def result_uids(self, selector: str) -> list[int]:
        """UIDs of the raw matches, placeholders included.

        Verification must use raw matches: a query is only reproduced when
        even ``pq(fragment)(selector)`` returns exactly the original hits.
        Otherwise a placeholder could leak into a reviewer's plain query.
        """
        uids = []
        for element in _select(self.roots, selector):
            uid = self.uid_map.get(element)
            if uid is not None:
                uids.append(uid)
        return uids

    def verify(self) -> bool:
        for query in self.queries:
            try:
                if self.result_uids(query) != self.expected.get(query, []):
                    return False
            except Exception:
                return False
        return True


def _hit_signatures(state: _State, query: str) -> list[tuple]:
    """Raw hit signatures in the working tree, placeholders included."""
    roots = list(state.roots)
    return [(_tag_name(element), _path_key(element, roots))
            for element in _select(state.roots, query)]


def _hit_signatures_from_document(document: PyQuery,
                                  query: str) -> list[tuple]:
    """Raw hit signatures in the re-parsed fragment, placeholders included."""
    roots = list(document)
    return [(_tag_name(element), _path_key(element, roots))
            for element in document(query)]


def _protected_uids(expected: Mapping[str, Sequence[int]],
                    conflicted: Iterable[str],
                    roots: Sequence[Any],
                    uid_map: Mapping[Any, int]) -> set[int]:
    """UIDs of hits, their ancestors and their descendants.

    A hit may be redacted, but it must never be removed or turned into a
    placeholder: the reviewer has to see a real matched node.  Its subtree is
    kept as review context (permitted values stay visible).  The verifier
    still rejects any trimming that would change a query answer.
    """
    conflicted = set(conflicted)
    protected: set[int] = set()
    by_uid = {uid: element for element, uid in uid_map.items()}
    for query, uids in expected.items():
        if query in conflicted:
            continue
        for uid in uids:
            element = by_uid.get(uid)
            for descendant in element.iter():
                descendant_uid = uid_map.get(descendant)
                if descendant_uid is not None:
                    protected.add(descendant_uid)
            while element is not None:
                element_uid = uid_map.get(element)
                if element_uid is not None:
                    protected.add(element_uid)
                element = element.getparent()
    for root in roots:
        uid = uid_map.get(root)
        if uid is not None:
            protected.add(uid)
    return protected


# ---------------------------------------------------------------------------
# Minimisation
# ---------------------------------------------------------------------------

def _minimize(state: _State, protected: set[int],
              placeholder_original: dict[int, str]) -> None:
    for element, parent in _postorder(state.roots):
        if parent is None:
            continue
        uid = state.uid_map.get(element)
        if uid in protected:
            continue
        if is_placeholder(element, state.marker_attr, state.placeholder_tag):
            continue
        if _try_remove(element, state):
            continue
        original_tag = _tag_name(element)
        if _try_placeholder(element, state):
            placeholder_original[uid] = original_tag


def _squeeze(state: _State, hit_uids: set[int]) -> None:
    """Drop content that no query needs.

    Used only when the fragment is over budget.  Hits and their ancestors are
    kept as real nodes; everything else may be trimmed as long as the
    verifier still sees the exact original hit sequence.
    """
    for element in _elements(state.roots):
        for attribute in ('text', 'tail'):
            old = getattr(element, attribute)
            if old is None:
                continue
            setattr(element, attribute, None)
            if not state.verify():
                setattr(element, attribute, old)
    for element, _parent in reversed(_postorder(state.roots)):
        for name in list(element.attrib):
            if name == state.marker_attr:
                continue
            old = element.attrib[name]
            del element.attrib[name]
            if not state.verify():
                element.set(name, old)
    for element, parent in _postorder(state.roots):
        if parent is None:
            continue
        if state.uid_map.get(element) in hit_uids:
            continue
        if is_placeholder(element, state.marker_attr, state.placeholder_tag):
            continue
        _try_remove(element, state)


def _postorder(roots: Sequence[Any]) -> list[tuple[Any, Any]]:
    """Snapshot the tree in child-first order.

    Minimisation mutates the tree while iterating, so the traversal must not
    depend on a live lxml iterator: removing a child would otherwise make the
    parent's iterator skip the next sibling.
    """
    result: list[tuple[Any, Any]] = []

    def walk(element):
        for child in list(element):
            walk(child)
        result.append((element, element.getparent()))

    for root in roots:
        walk(root)
    return result


def _try_remove(element, state: _State) -> bool:
    parent = element.getparent()
    if parent is None:
        return False
    index = parent.index(element)
    tail = element.tail
    previous = element.getprevious()
    previous_tail = previous.tail if previous is not None else None
    parent_text = parent.text

    def restore():
        element.tail = tail
        parent.insert(index, element)
        if previous is not None:
            previous.tail = previous_tail
        else:
            parent.text = parent_text

    # Prefer dropping the tail too: it is usually review noise.
    element.tail = None
    parent.remove(element)
    if state.verify():
        return True
    restore()

    # Otherwise keep the tail, which may contribute to an ancestor's text.
    if tail:
        if previous is not None:
            previous.tail = (previous_tail or '') + tail
        else:
            parent.text = (parent_text or '') + tail
    element.tail = None
    parent.remove(element)
    if state.verify():
        return True
    restore()
    return False


def _try_placeholder(element, state: _State) -> bool:
    if not isinstance(element.tag, str):
        return False
    original_tag = element.tag
    children = list(element)
    saved_text = element.text
    saved_tail = element.tail
    saved_attrib = dict(element.attrib)

    def make_placeholder(tag: str) -> None:
        element.tag = tag
        for child in list(element):
            element.remove(child)
        element.text = None
        element.attrib.clear()
        element.set(state.marker_attr, PLACEHOLDER_VALUE)

    # A distinct tag is safest: ordinary tag selectors cannot return it.
    make_placeholder(state.placeholder_tag)
    if state.verify():
        return True

    # :nth-of-type() and tag-based negation need the original tag.  Keep the
    # marker attribute so consumers can still tell it apart.
    make_placeholder(original_tag)
    if state.verify():
        return True

    for name in _attribute_priority(saved_attrib):
        element.set(name, saved_attrib[name])
        if state.verify():
            return True

    # A selector may need the subtree itself (:has(), :empty ...).  Restore
    # the full (already redacted) node rather than pretending it is safe.
    element.tag = original_tag
    element.attrib.clear()
    element.attrib.update(saved_attrib)
    element.text = saved_text
    element.tail = saved_tail
    element.extend(children)
    return False


def _attribute_priority(attributes: Mapping[str, str]) -> list[str]:
    preferred = ('id', 'class', 'name', 'type', 'value', 'href', 'src',
                 'for', 'rel', 'role', 'title', 'alt')
    ordered = [name for name in preferred if name in attributes]
    ordered.extend(sorted(name for name in attributes if name not in ordered))
    return ordered


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

class _Rules:
    def __init__(self, rules: Any):
        self.per_name: dict[str, list[Any]] = {}
        self.global_rules: list[Any] = []
        if rules is None:
            return
        if isinstance(rules, Mapping):
            for name, value in rules.items():
                key = str(name)
                values = _clean_rules(_as_values(value))
                if key == '*':
                    self.global_rules.extend(values)
                else:
                    self.per_name.setdefault(key, []).extend(values)
        else:
            self.global_rules.extend(_clean_rules(_as_values(rules)))

    def for_name(self, name: str) -> list[Any]:
        return list(self.per_name.get(name, ())) + self.global_rules

    def __bool__(self) -> bool:
        return bool(self.per_name or self.global_rules)


def _as_values(values: Any) -> list[Any]:
    if isinstance(values, (str, bytes)):
        return [values]
    return list(values)


def _clean_rules(values: Iterable[Any]) -> list[Any]:
    return [value.decode() if isinstance(value, bytes) else value
            for value in values]


class _Redactor:
    def __init__(self, *, allowed_values, forbidden_values,
                 forbidden_attributes, redacted_text, redacted_value,
                 marker_attr):
        self.allowed = _Rules(allowed_values)
        self.forbidden = _Rules(forbidden_values)
        # ``None`` means "keep everything"; an explicit empty allow-list means
        # "keep nothing that is data".
        self.enabled = (allowed_values is not None
                        or forbidden_values is not None)
        self.forbidden_attributes = set(forbidden_attributes or ())
        self.redacted_text = redacted_text
        self.redacted_value = redacted_value
        self.marker_attr = marker_attr

    def apply(self, roots: Sequence[Any]) -> None:
        if not self.enabled:
            return
        for root in roots:
            root.tail = self.redact_text(root.tail)
        for element in _elements(roots):
            if not isinstance(element.tag, str):
                # Comments and processing instructions keep their payload in
                # ``.text``; it must be redacted like any other text.
                element.text = self.redact_text(element.text)
                element.tail = self.redact_text(element.tail)
                continue
            element.text = self.redact_text(element.text)
            for child in element:
                child.tail = self.redact_text(child.tail)
            for name, value in list(element.attrib.items()):
                if name == self.marker_attr:
                    continue
                if name in self.forbidden_attributes:
                    del element.attrib[name]
                    continue
                if name in PRESENCE_ATTRIBUTES:
                    # Presence is meaningful; the value is not data and may
                    # be arbitrary in malformed markup.  Normalise it so no
                    # text can leak through the attribute value.
                    element.set(name, name)
                    continue
                element.set(name, self.redact_attribute(name, value))

    def redact_text(self, value: str | None) -> str | None:
        if not value or not self.enabled or not value.strip():
            return value
        rules = self.allowed.for_name('text')
        forbidden = self.forbidden.for_name('text')
        if _allowed(value, rules, forbidden):
            return value
        # Text is kept only as a whole; never leak a value by keeping a
        # matching substring of a larger text node.
        if _allowed(value.strip(), rules, forbidden):
            return value
        return self.redacted_text

    def redact_attribute(self, name: str, value: str) -> str:
        if not value or not self.enabled:
            return value
        rules = self.allowed.for_name(name)
        forbidden = self.forbidden.for_name(name)

        if name == 'class':
            # Class tokens are independently selectable; check them one by
            # one and keep only the tokens that are allowed.
            kept = [token for token in value.split()
                    if _allowed(token, rules, forbidden)]
            if kept:
                return ' '.join(kept)
            if _allowed(value, rules, forbidden):
                return value
            return self.redacted_value

        if name == 'style':
            kept = []
            for declaration in value.split(';'):
                declaration = declaration.strip()
                if declaration and _allowed(declaration, rules, forbidden):
                    kept.append(declaration)
            return '; '.join(kept) if kept else self.redacted_value

        if _allowed(value, rules, forbidden):
            return value
        return self.redacted_value


def _allowed(value: str, rules: Sequence[Any],
             forbidden: Sequence[Any]) -> bool:
    if forbidden and _matches(value, forbidden):
        return False
    return bool(rules) and _matches(value, rules)


def _matches(value: str, rules: Iterable[Any]) -> bool:
    for rule in rules:
        if isinstance(rule, str):
            if rule == '*' or rule == value:
                return True
        elif callable(rule):
            try:
                if rule(value):
                    return True
            except Exception:
                continue
        elif hasattr(rule, 'search'):
            try:
                if rule.search(value):
                    return True
            except Exception:
                continue
    return False


# ---------------------------------------------------------------------------
# Source parsing and query helpers
# ---------------------------------------------------------------------------

def _normalize_queries(queries: Any) -> list[str]:
    if isinstance(queries, str):
        return [queries]
    if isinstance(queries, Mapping):
        return [str(query) for query in queries]
    result = []
    for query in queries:
        if isinstance(query, Mapping):
            query = query.get('query', query.get('selector'))
        elif hasattr(query, 'selector') and not isinstance(query, str):
            query = query.selector
        if not isinstance(query, str):
            raise TypeError(f'invalid query: {query!r}')
        result.append(query)
    return list(dict.fromkeys(result))


def _source_roots(source: Any) -> list[Any]:
    if isinstance(source, EvidenceFragment):
        return _source_roots(source.html)
    if isinstance(source, PyQuery):
        return [element for element in source
                if isinstance(element, etree._Element)]
    if isinstance(source, etree._ElementTree):
        root = source.getroot()
        return [] if root is None else [root]
    if isinstance(source, etree._Element):
        return [source]
    if isinstance(source, (list, tuple)):
        roots = [item.getroot() if isinstance(item, etree._ElementTree)
                 else item for item in source]
        if not all(isinstance(item, etree._Element) for item in roots):
            raise TypeError('source list must contain elements')
        return roots
    if isinstance(source, (str, bytes)):
        return _parse_source(source)
    raise TypeError(f'unsupported source: {type(source).__name__}')


def _parse_source(source: str | bytes) -> list[Any]:
    """Parse the source the same way ``PyQuery(source)`` does.

    This matters: the reviewer compares the fragment against the page as
    pyquery normally sees it, so the fragment must start from exactly that
    tree.
    """
    return [element for element in PyQuery(source)
            if isinstance(element, etree._Element)]


def _parse_fragment(html: str, parser: str | None) -> PyQuery:
    if parser:
        return PyQuery(html, parser=parser)
    return PyQuery(html)


def _fragment_parser(roots: Sequence[Any]) -> str | None:
    # A single root (including a full document) re-parses exactly like the
    # source.  Several roots only happen for explicit element lists; keep
    # their sibling structure with the fragments parser in that case.
    if len(roots) > 1:
        return 'html_fragments'
    return None


def _elements(roots: Iterable[Any]) -> list[Any]:
    result = []
    for root in roots:
        result.extend(root.iter())
    return result


def _select(roots: Sequence[Any], selector: str) -> list[Any]:
    matches: list[Any] = []
    for root in roots:
        matches.extend(PyQuery(root)(selector))
    return matches


def _clone_uid_map(original_roots: Sequence[Any],
                   clone_roots: Sequence[Any]) -> dict[Any, int]:
    uid_map: dict[Any, int] = {}
    uid = 0
    for original, clone in zip(original_roots, clone_roots):
        for _original_element, clone_element in zip(original.iter(),
                                                    clone.iter()):
            uid_map[clone_element] = uid
            uid += 1
    return uid_map


def _path_key(element: Any, roots: Sequence[Any]) -> tuple[int, ...]:
    for root_index, root in enumerate(roots):
        chain = []
        current = element
        # lxml may hand out different proxy objects for the same node, and a
        # fragment parsed by lxml sits under a synthetic <body>.  Compare with
        # ``==`` and stop as soon as the root is reached.
        while current is not None and current != root:
            parent = current.getparent()
            if parent is None:
                break
            chain.append(parent.index(current))
            current = parent
        if current is not None and current == root:
            chain.append(0)
            chain.reverse()
            return (root_index, *chain)
    return ()


def _tag_name(element: Any) -> str:
    tag = getattr(element, 'tag', '')
    return tag if isinstance(tag, str) else ''


def _safe_marker(marker: str, queries: Sequence[str]) -> str:
    while any(marker in query for query in queries):
        marker = f'{marker}-x'
    return marker


def _safe_tag(tag: str, queries: Sequence[str]) -> str:
    while any(tag in query for query in queries):
        tag = f'{tag}-x'
    return tag


def _collision_free_marker(marker: str, roots: Sequence[Any]) -> str:
    while any(marker in element.attrib for element in _elements(roots)):
        marker = f'{marker}-x'
    return marker


def _collision_free_tag(tag: str, roots: Sequence[Any]) -> str:
    while any(_tag_name(element) == tag for element in _elements(roots)):
        tag = f'{tag}-x'
    return tag


def _safe_text(text: str, queries: Sequence[str]) -> str:
    while any(text in query for query in queries):
        text = f'{text}​'
    return text


def _safe_value(value: str, queries: Sequence[str]) -> str:
    while any(value in query for query in queries):
        value = f'{value}-x'
    return value


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _serialize(roots: Sequence[Any]) -> str:
    """Serialize with explicit end tags for non-void elements.

    ``lxml.html.tostring`` omits ``</li>`` for an empty ``li`` (likewise for
    a few other elements).  Re-parsing such output merges the next sibling
    into the previous element and silently changes positional selectors.  The
    evidence fragment must survive a round trip, so end tags are explicit.
    """
    return ''.join(_serialize_element(root) for root in roots)


_RAW_TEXT_TAGS = frozenset({'script', 'style', 'xmp'})


def _serialize_element(element: Any) -> str:
    if not isinstance(element.tag, str):
        return etree.tostring(element, encoding='unicode', method='html')

    tag = element.tag
    parts = ['<', tag]
    for name, value in element.attrib.items():
        parts.append(f' {name}="{_escape_attr(value)}"')
    parts.append('>')
    if tag in _VOID_TAGS:
        return ''.join(parts)

    raw = tag in _RAW_TEXT_TAGS
    if element.text:
        parts.append(element.text if raw else _escape_text(element.text))
    for child in element:
        parts.append(_serialize_element(child))
        if child.tail:
            parts.append(child.tail if raw else _escape_text(child.tail))
    parts.append(f'</{tag}>')
    return ''.join(parts)


def _escape_text(value: str) -> str:
    return (value.replace('&', '&amp;')
                 .replace('<', '&lt;')
                 .replace('>', '&gt;'))


def _escape_attr(value: str) -> str:
    return (value.replace('&', '&amp;')
                 .replace('"', '&quot;')
                 .replace('<', '&lt;')
                 .replace('>', '&gt;'))


# ---------------------------------------------------------------------------
# Command line interface
# ---------------------------------------------------------------------------

def main(argv: Sequence[str] | None = None) -> int:
    """Generate an evidence fragment from a JSON description.

    Input (stdin or ``--input``)::

        {"html": "...", "queries": ["li:nth-child(2)"],
         "allowed_values": ["keep"], "budget": 4096}

    Output: the :meth:`EvidenceFragment.to_dict` JSON document.
    """
    parser = argparse.ArgumentParser(
        prog='python -m pyquery.evidence',
        description='Build a redacted evidence fragment for CSS queries.')
    parser.add_argument('-i', '--input',
                        help='JSON input file (default stdin)')
    parser.add_argument('-o', '--output', help='write output to this file')
    parser.add_argument('--html', help='HTML source (overrides input html)')
    parser.add_argument('--query', action='append', dest='query',
                        help='CSS query to reproduce (repeatable)')
    parser.add_argument('--budget', type=int,
                        help='size budget in UTF-8 bytes')
    parser.add_argument('--allow', action='append', dest='allow',
                        help='allowed value (repeatable)')
    parser.add_argument('--compact', action='store_true',
                        help='only print the fragment HTML')
    args = parser.parse_args(argv)

    if args.input:
        with open(args.input, encoding='utf-8') as handle:
            spec = json.load(handle)
    elif args.html or args.query:
        spec = {}
    else:
        spec = json.load(sys.stdin)

    html = args.html if args.html is not None else spec.get('html')
    if html is None:
        parser.error('an html value is required')
    queries = args.query or spec.get('queries') or spec.get('query') or []
    allowed = (args.allow if args.allow is not None
               else spec.get('allowed_values'))
    budget = args.budget if args.budget is not None else spec.get('budget')

    result = build_evidence_fragment(
        html,
        queries,
        allowed_values=allowed,
        budget=budget,
        forbidden_values=spec.get('forbidden_values'),
        forbidden_attributes=spec.get('forbidden_attributes'),
    )
    output = result.html if args.compact else json.dumps(
        result.to_dict(), ensure_ascii=False, indent=2)

    if args.output:
        with open(args.output, 'w', encoding='utf-8') as handle:
            handle.write(output)
            if not output.endswith('\n'):
                handle.write('\n')
    else:
        sys.stdout.write(output)
        if not output.endswith('\n'):
            sys.stdout.write('\n')

    return 0 if result.ok else 1


if __name__ == '__main__':  # pragma: no cover
    raise SystemExit(main())
