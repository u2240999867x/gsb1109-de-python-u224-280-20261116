# Copyright (C) 2008 - Olivier Lauzanne <olauzanne@gmail.com>
#
# Distributed under the BSD license, see LICENSE.txt
"""Reasoning over partially collected DOM trees.

An RPA collector sometimes submits only a fragment of the real page together
with metadata describing *which* axes are already complete.  A plain
``PyQuery(selector)`` call on such a fragment cannot distinguish "the selector
matches nothing" from "the nodes that would decide the answer were not
collected".  This module keeps the two apart.

Each collection rule (a CSS selector) is evaluated to one of three statuses:

``confirmed``
    The result set is known exactly.  Every reported match is genuine and no
    legal completion of the page can add or remove a match.

``possible``
    At least one match is observed in the fragment but the result is not yet
    exact.  ``evidence`` lists every missing structural axis (unknown
    preceding siblings, an incomplete attribute list, an unsearched ancestor
    axis, ...).  Missing structure is never interpreted as absence.

``unsupported``
    The selector uses a feature this module cannot reason about (pseudo
    elements, namespaces, unknown pseudo classes, ...).  It is reported
    explicitly instead of being answered by an ordinary query on the
    residual tree.

Internally every sub-test returns a tri-state:

* ``TRUE`` / ``FALSE`` are only produced when the axes the test depends on are
  covered by the collector metadata;
* ``UNKNOWN`` carries evidence describing the missing structure.

Positional pseudo-classes (``:nth-child()`` and friends) are evaluated against
a single :class:`SharedCompletion` view of every incomplete sibling list,
shared by every rule in a run.  One missing preceding sibling shifts the index
of every following element, so the conditions on one element are intersected
over the *same* reachable completed index rather than each picking their own
favourable completion.  When no single index satisfies all conditions (for
example ``:nth-child(2):nth-child(3)``) the element is ruled out instead of
being reported as a possible match.
"""
from cssselect import parse as parse_css
from cssselect.parser import (
    Attrib,
    Class,
    CombinedSelector,
    Element,
    Function,
    Hash,
    Matching,
    Negation,
    Pseudo,
    Relation,
    SpecificityAdjustment,
)
from cssselect import SelectorError
from cssselect.parser import parse_series
from cssselect.xpath import ExpressionError

from lxml import etree

from .cssselectpatch import JQueryTranslator

#: Rule statuses.
UNSUPPORTED = 'unsupported'
CONFIRMED = 'confirmed'
POSSIBLE = 'possible'

# Tri-states for individual sub-tests.
TRUE = 'true'
FALSE = 'false'
UNKNOWN = 'unknown'

# Evidence reason codes.
MISSING_PRECEDING_SIBLING = 'missing_preceding_sibling'
MISSING_FOLLOWING_SIBLING = 'missing_following_sibling'
MISSING_PARENT = 'missing_parent'
MISSING_ANCESTOR = 'missing_ancestor'
MISSING_CHILD = 'missing_child'
MISSING_DESCENDANT = 'missing_descendant'
INCOMPLETE_ATTRIBUTES = 'incomplete_attributes'
INCOMPLETE_CHILDREN = 'incomplete_children'
INCOMPLETE_SIBLINGS = 'incomplete_siblings'
INCOMPLETE_SELECTION = 'incomplete_selection_set'
UNSUPPORTED_PSEUDO = 'unsupported_pseudo'

#: Pseudo-classes whose value depends on user interaction, not on the tree.
_DYNAMIC_PSEUDOS = frozenset((
    'hover', 'focus', 'active', 'visited', 'link', 'target',
    'focus-visible', 'focus-within',
))

#: Index based structural pseudo-classes over all element children.
_CHILD_FLAG_PSEUDOS = frozenset((
    'first-child', 'last-child', 'only-child',
))
#: Index based structural pseudo-classes over same-tag children.
_TYPE_FLAG_PSEUDOS = frozenset((
    'first-of-type', 'last-of-type', 'only-of-type',
))
_NTH_CHILD_FUNCTIONS = frozenset(('nth-child', 'nth-last-child'))
_NTH_TYPE_FUNCTIONS = frozenset(('nth-of-type', 'nth-last-of-type'))

#: jQuery pseudo-classes that are purely local (tag / attribute / text based)
#: and therefore exact once the element itself is complete.
_LOCAL_JQUERY_PSEUDOS = frozenset((
    'checked', 'selected', 'disabled', 'enabled', 'file', 'input', 'button',
    'radio', 'text', 'checkbox', 'password', 'submit', 'hidden', 'image',
    'reset', 'header', 'parent', 'empty',
))
#: jQuery filters operating on the whole selection set rather than on tree
#: structure.  They can only be confirmed on a fully covered document.
_SELECTION_JQUERY_PSEUDOS = frozenset(('first', 'last', 'even', 'odd'))
_SELECTION_JQUERY_FUNCTIONS = frozenset(('eq', 'gt', 'lt'))


class _UnsupportedSelector(Exception):
    def __init__(self, reason, detail=None):
        super().__init__(f'{reason}: {detail}')
        self.reason = reason
        self.detail = detail


class PartialCoverage:
    """Completeness metadata attached to a collected fragment.

    Annotations are supplied as ``{'element': <lxml element>, 'complete':
    <axis or tuple of axes>}``.  Supported axes:

    ``element``
        The element itself (tag, attributes and own text) is fully collected.

    ``attributes``
        The attribute set is complete even though the subtree may not be.

    ``children``
        The element children are exactly those present in the fragment, in
        their real order: no child is missing.

    ``subtree``
        The whole subtree rooted at the element is collected.  Implies
        ``element``, ``attributes`` and ``children`` for it and every
        descendant.

    ``siblings``
        The element and all of its element siblings were collected, so the
        shared sibling list (and its positional indices) is complete.

    ``document``
        The element is the document root and the whole document was collected:
        no ancestor, sibling or descendant exists outside the fragment.
    """

    def __init__(self, metadata=None):
        self._elements = set()
        self._attributes = set()
        self._children = set()
        self._subtree = set()
        self._siblings = set()
        self._document_roots = set()
        for entry in metadata or ():
            self._add(entry)

    def _add(self, entry):
        element = entry['element']
        complete = entry.get('complete', ())
        if isinstance(complete, str):
            complete = (complete,)
        for axis in complete:
            if axis == 'element':
                self._elements.add(element)
            elif axis == 'attributes':
                self._attributes.add(element)
            elif axis == 'children':
                self._children.add(element)
            elif axis == 'subtree':
                self._subtree.add(element)
            elif axis == 'siblings':
                self._siblings.add(element)
            elif axis == 'document':
                self._document_roots.add(element)
                self._subtree.add(element)
            else:
                raise ValueError(f'unknown coverage axis: {axis!r}')

    @classmethod
    def subtree_only(cls, root):
        """Coverage for a single fully collected subtree.

        Ancestors and outer siblings stay unknown, the usual shape of an RPA
        fragment submission.
        """
        coverage = cls()
        coverage._subtree.add(root)
        return coverage

    @classmethod
    def whole_document(cls, root):
        """Coverage for a fully collected document tree."""
        coverage = cls()
        coverage._document_roots.add(root)
        coverage._subtree.add(root)
        coverage._siblings.update(root.iter())
        return coverage

    def is_document_root(self, element):
        """True when ``element`` is certified as the whole document root."""
        return element in self._document_roots

    def element_complete(self, element):
        if element in self._elements or element in self._subtree:
            return True
        return self._inside_complete_subtree(element)

    def attributes_complete(self, element):
        if element in self._attributes or self.element_complete(element):
            return True
        return self._inside_complete_subtree(element)

    def children_complete(self, element):
        if element in self._children or element in self._subtree:
            return True
        return self._inside_complete_subtree(element)

    def subtree_complete(self, element):
        """True when the whole subtree rooted at ``element`` was collected."""
        return element in self._subtree or \
            self._inside_complete_subtree(element)

    def siblings_complete(self, element):
        if element in self._siblings:
            return True
        parent = element.getparent()
        return parent is not None and self.children_complete(parent)

    def _inside_complete_subtree(self, element):
        parent = element.getparent()
        while parent is not None:
            if parent in self._subtree:
                return True
            parent = parent.getparent()
        return False

    def whole_document_complete(self, root):
        """True when the fragment is certified to be the whole document."""
        if root not in self._document_roots:
            return False
        for element in root.iter():
            if not isinstance(element.tag, str):
                continue
            if element.getparent() is not None \
                    and not self.siblings_complete(element):
                return False
        return True


# ---------------------------------------------------------------------------
# Parsed selector representation
# ---------------------------------------------------------------------------

class _PositionSpec:
    """One positional requirement extracted from a compound selector."""

    def __init__(self, kind, name, series_a=None, series_b=None):
        self.kind = kind              # 'nth' | 'flag'
        self.name = name
        self.series_a = series_a
        self.series_b = series_b

    @property
    def is_type_based(self):
        return self.name in _TYPE_FLAG_PSEUDOS or \
            self.name in _NTH_TYPE_FUNCTIONS

    @property
    def is_reverse(self):
        return self.name in ('nth-last-child', 'nth-last-of-type') or \
            self.name.startswith('last')


class _Compound:
    """Requirements gathered from one compound selector."""

    def __init__(self):
        self.tag = None
        self.namespace = None
        self.classes = []
        self.id = None
        self.attributes = []
        # Purely local predicates, exact once the element axis is complete.
        self.local_predicates = []
        # Local jQuery pseudos whose truth depends on the child axis, so an
        # incomplete child list keeps them undecidable.
        self.child_axis_pseudos = []
        # :contains() needles; text may live in an uncollected descendant.
        self.contains_needles = []
        # Positional requirements (_PositionSpec).
        self.positions = []
        # Selection-set filters (jQuery :first/:eq/...): names only.
        self.selection_filters = []
        # Embedded full selectors introduced by :has().  One entry per
        # :has() pseudo-class; each entry lists the OR-branches of that
        # single :has() (its comma separated arguments).
        self.has_groups = []
        # Embedded full selectors introduced by :not().
        self.negated_selectors = []
        # Embedded full selector branches introduced by :is()/:where().
        self.is_branches = []
        # :root / :scope style requirement.
        self.requires_root = False


def _parse_nth(arguments):
    try:
        return parse_series(arguments)
    except Exception as exc:  # pragma: no cover - cssselect guards syntax
        raise _UnsupportedSelector('nth_syntax', str(exc))


def _build_compound(node):
    """Walk the simple-selector chain of one compound selector."""
    compound = _Compound()
    current = node
    while current is not None:
        if isinstance(current, Element):
            compound.namespace = current.namespace
            compound.tag = current.element
            if current.namespace is not None:
                raise _UnsupportedSelector('namespace', current.namespace)
        elif isinstance(current, Hash):
            compound.id = current.id
        elif isinstance(current, Class):
            compound.classes.append(current.class_name)
        elif isinstance(current, Attrib):
            if current.namespace is not None:
                raise _UnsupportedSelector('namespace', current.namespace)
            compound.attributes.append(current)
        elif isinstance(current, Pseudo):
            _walk_pseudo(current, compound)
        elif isinstance(current, Function):
            _walk_function(current, compound)
        elif isinstance(current, Negation):
            # The negated selector lives in ``subselector``.
            compound.negated_selectors.append(
                _parse_selector_tree(current.subselector))
        elif isinstance(current, (Matching, SpecificityAdjustment)):
            compound.is_branches.extend(
                _parse_selector_tree(sub) for sub in current.selector_list)
        elif isinstance(current, Relation):
            # In cssselect 1.x Relation nodes are produced exclusively by the
            # :has() relational pseudo-class.  One node is one :has() call,
            # and its arguments are OR-branches.  The leading token carries
            # the combinator connecting the scope element to each branch.
            branches = []
            for delim, sub_selector in current.arguments:
                compounds, combinators = _parse_selector_tree(
                    sub_selector.parsed_tree)
                branches.append((delim.value, compounds, combinators))
            compound.has_groups.append(branches)
        else:
            raise _UnsupportedSelector('node', type(current).__name__)
        current = getattr(current, 'selector', None)
    return compound


def _walk_pseudo(node, compound):
    ident = node.ident
    if ident in _CHILD_FLAG_PSEUDOS or ident in _TYPE_FLAG_PSEUDOS:
        compound.positions.append(_PositionSpec('flag', ident))
    elif ident in _DYNAMIC_PSEUDOS:
        raise _UnsupportedSelector(UNSUPPORTED_PSEUDO, f':{ident}')
    elif ident in ('root', 'scope'):
        compound.requires_root = True
    elif ident in _LOCAL_JQUERY_PSEUDOS:
        compound.local_predicates.append(_LOCAL_PREDICATES[ident])
        if ident in ('empty', 'parent'):
            compound.child_axis_pseudos.append(ident)
    elif ident in _SELECTION_JQUERY_PSEUDOS:
        compound.selection_filters.append((ident, None))
    else:
        raise _UnsupportedSelector(UNSUPPORTED_PSEUDO, f':{ident}')


def _walk_function(node, compound):
    name = node.name
    if name in _NTH_CHILD_FUNCTIONS or name in _NTH_TYPE_FUNCTIONS:
        series_a, series_b = _parse_nth(node.arguments)
        compound.positions.append(
            _PositionSpec('nth', name, series_a, series_b))
    elif name == 'contains':
        # Text may live in a descendant that was not collected, so this is
        # evaluated subtree-aware rather than as a plain local predicate.
        compound.contains_needles.append(node.arguments[0].value)
    elif name in _SELECTION_JQUERY_FUNCTIONS:
        value = int(node.arguments[0].value)
        compound.selection_filters.append((name, value))
    else:
        raise _UnsupportedSelector(UNSUPPORTED_PSEUDO, f':{name}()')


def _parse_selector_tree(tree):
    """Return ``(compounds, combinators)`` ordered target-first."""
    compounds = []
    combinators = []
    node = tree
    while isinstance(node, CombinedSelector):
        combinators.append(node.combinator)
        compounds.append(_build_compound(node.subselector))
        node = node.selector
    compounds.append(_build_compound(node))
    return compounds, combinators


# ---------------------------------------------------------------------------
# Local predicates for jQuery pseudo-classes
# ---------------------------------------------------------------------------

def _is_input(element):
    return element.tag in ('input', 'select', 'textarea', 'button')


def _is_button(element):
    return element.tag == 'button' or (
        element.tag == 'input' and element.get('type') == 'button')


def _is_header(element):
    return element.tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6')


def _is_parent(element):
    return any(isinstance(child.tag, str) for child in element)


def _is_empty(element):
    # CSS :empty: no element children and no text nodes at all (whitespace
    # text nodes still count as content).
    if list(element) or element.text:
        return False
    return all(not child.tail for child in element)


def _typed_input(type_name):
    def predicate(element):
        return element.tag == 'input' and element.get('type') == type_name
    return predicate


_LOCAL_PREDICATES = {
    'input': _is_input,
    'button': _is_button,
    'checked': lambda e: e.tag == 'input' and 'checked' in e.attrib,
    'selected': lambda e: e.tag == 'option' and 'selected' in e.attrib,
    'disabled': lambda e: 'disabled' in e.attrib,
    'enabled': lambda e: 'disabled' not in e.attrib,
    'header': _is_header,
    'parent': _is_parent,
    'empty': _is_empty,
    'file': _typed_input('file'),
    'radio': _typed_input('radio'),
    'text': _typed_input('text'),
    'checkbox': _typed_input('checkbox'),
    'password': _typed_input('password'),
    'submit': _typed_input('submit'),
    'hidden': _typed_input('hidden'),
    'image': _typed_input('image'),
    'reset': _typed_input('reset'),
}


def _attribute_matches(element, attrib_node):
    name = attrib_node.attrib
    if name not in element.attrib:
        return False
    if attrib_node.operator == 'exists':
        return True
    actual = element.get(name)
    expected = attrib_node.value.value
    if attrib_node.flag == 'i':
        actual = actual.lower()
        expected = expected.lower()
    operator = attrib_node.operator
    if operator == '=':
        return actual == expected
    if operator == '~=':
        return expected in actual.split()
    if operator == '|=':
        return actual == expected or actual.startswith(expected + '-')
    if operator == '^=':
        return actual.startswith(expected)
    if operator == '$=':
        return actual.endswith(expected)
    if operator == '*=':
        return expected in actual
    raise _UnsupportedSelector('attribute_operator', operator)


def _local_compound_matches(element, compound):
    """Observed truth of the local part of a compound."""
    if not isinstance(element.tag, str):
        return False
    if compound.tag is not None and compound.tag != '*' \
            and element.tag != compound.tag:
        return False
    if compound.id is not None and element.get('id') != compound.id:
        return False
    for class_name in compound.classes:
        class_attr = element.get('class')
        if class_attr is None or class_name not in class_attr.split():
            return False
    for attrib_node in compound.attributes:
        if not _attribute_matches(element, attrib_node):
            return False
    for predicate in compound.local_predicates:
        if not predicate(element):
            return False
    return True


# ---------------------------------------------------------------------------
# Shared completion of incomplete sibling lists
# ---------------------------------------------------------------------------

class SharedCompletion:
    """A single coherent view of how unknown sibling gaps may be completed.

    A legal completion only *inserts* unknown element children; collected
    nodes are never removed or reordered.  This class therefore exposes, for
    every element, its *observed* index on both axes plus whether the gap
    before/after it is still open.

    Positional verdicts are derived by requiring one reachable completed index
    to satisfy *all* positional constraints of a compound at once.  Because an
    incomplete axis can only ever yield FALSE or UNKNOWN (never a confirmed
    match), no concrete gap counts have to be committed; the per-element
    feasibility intersection is enough and it guarantees that conditions never
    each pick their own favourable completion.
    """

    def __init__(self, root, coverage):
        self._coverage = coverage
        # parent -> whether each gap is certified closed, length = nchildren+1
        self._closed = {}
        for parent in root.iter():
            if not isinstance(parent.tag, str):
                continue
            children = [c for c in parent if isinstance(c.tag, str)]
            self._closed[parent] = [
                coverage.children_complete(parent)] * (len(children) + 1)

    def _children(self, element, same_tag_only):
        parent = element.getparent()
        children = [c for c in parent if isinstance(c.tag, str)]
        if same_tag_only:
            children = [c for c in children if c.tag == element.tag]
        return parent, children

    def observed_index(self, element, same_tag_only=False):
        """1-based index counting only the nodes already collected."""
        if element.getparent() is None:
            return 1
        _, children = self._children(element, same_tag_only)
        return children.index(element) + 1

    def observed_reverse_index(self, element, same_tag_only=False):
        if element.getparent() is None:
            return 1
        _, children = self._children(element, same_tag_only)
        return len(children) - children.index(element)

    def preceding_gap_open(self, element, same_tag_only=False):
        parent = element.getparent()
        if parent is None:
            return False
        _, children = self._children(element, same_tag_only)
        return not self._closed[parent][children.index(element)]

    def trailing_gap_open(self, element, same_tag_only=False):
        parent = element.getparent()
        if parent is None:
            return False
        _, children = self._children(element, same_tag_only)
        return not self._closed[parent][children.index(element) + 1]

    def known_peer_count(self, element, same_tag_only=False):
        parent = element.getparent()
        if parent is None:
            return 0
        _, children = self._children(element, same_tag_only)
        return len(children)


def _nth_series_matches(series_a, series_b, index):
    if series_a == 0:
        return index == series_b
    quotient = (index - series_b) / series_a
    return quotient >= 0 and quotient == int(quotient)


def _lcm(values):
    result = 1
    for value in values:
        result = result * value // _gcd(result, value)
    return result


def _gcd(left, right):
    while right:
        left, right = right, left % right
    return left


class _IndexConstraint:
    """A positional condition expressed over a completed 1-based index."""

    def __init__(self, holds, cap, period, source):
        self._holds = holds
        # Fixed largest index that can satisfy the constraint, or None when
        # solutions repeat periodically forever.
        self.cap = cap
        # Period of repeating solutions (positive slope), else None.
        self.period = period
        self.source = source

    @classmethod
    def exact(cls, spec, value):
        return cls(
            lambda index, value=value: index == value,
            cap=value, period=None, source=spec.name)

    @classmethod
    def from_spec(cls, spec):
        if spec.kind == 'flag':
            # first-* / last-* pin the relevant index to 1.
            return cls.exact(spec, 1)
        series_a, series_b = spec.series_a, spec.series_b
        holds = lambda index: _nth_series_matches(series_a, series_b, index)
        if series_a > 0:
            return cls(holds, cap=None, period=series_a, source=spec.name)
        # a == 0: one solution at b; a < 0: finite prefix bounded by b.
        return cls(holds, cap=series_b, period=None, source=spec.name)

    def holds(self, index):
        return self._holds(index)

    def impossible_above(self, observed):
        return self.cap is not None and self.cap < observed


def _search_bound(observed, constraints):
    """Upper index that must contain a joint solution if one exists."""
    caps = [constraint.cap for constraint in constraints
            if constraint.cap is not None]
    periods = [constraint.period for constraint in constraints
               if constraint.period is not None]
    bound = min(caps) if caps else None
    if periods:
        periodic_bound = observed + _lcm(periods)
        bound = periodic_bound if bound is None else min(bound, periodic_bound)
    return observed - 1 if bound is None or bound < observed else bound


# ---------------------------------------------------------------------------
# Tri-state evaluation
# ---------------------------------------------------------------------------

class _Evaluation:
    """One element's evaluation; evidence is local to the element."""

    def __init__(self, root, coverage, completion, selection_rank=None,
                 selection_size=None, ignore_selection_filters=False):
        self.root = root
        self.coverage = coverage
        self.completion = completion
        # Document-order rank of the element inside its selector group's
        # selection set, and that set's size (jQuery :first/:eq/... support).
        self.selection_rank = selection_rank
        self.selection_size = selection_size
        self.ignore_selection_filters = ignore_selection_filters
        self.evidence = set()

    def note(self, reason):
        self.evidence.add(reason)

    # -- local part --------------------------------------------------------

    def local_state(self, element, compound):
        # The tag is known as soon as the element was collected, even when
        # its attribute set is incomplete.
        if compound.tag is not None and compound.tag != '*' \
                and element.tag != compound.tag:
            return FALSE
        if not self.coverage.attributes_complete(element):
            # Missing attributes may add matches ([a], :not([a])) but can
            # never invalidate an observed hard mismatch; stay conservative.
            self.note(INCOMPLETE_ATTRIBUTES)
            return UNKNOWN
        if compound.child_axis_pseudos and \
                not self.coverage.children_complete(element):
            # :empty / :parent inspect the child axis; a partially collected
            # child list cannot settle them once the rest of the compound
            # matches.
            if not self._local_matches_without_child_pseudos(
                    element, compound):
                return FALSE
            self.note(INCOMPLETE_CHILDREN)
            self.note(MISSING_CHILD)
            return UNKNOWN
        return TRUE if _local_compound_matches(element, compound) else FALSE

    def _local_matches_without_child_pseudos(self, element, compound):
        if compound.id is not None and element.get('id') != compound.id:
            return False
        for class_name in compound.classes:
            class_attr = element.get('class')
            if class_attr is None or class_name not in class_attr.split():
                return False
        for attrib_node in compound.attributes:
            if not _attribute_matches(element, attrib_node):
                return False
        child_predicates = {_LOCAL_PREDICATES[name]
                            for name in compound.child_axis_pseudos}
        for predicate in compound.local_predicates:
            if predicate in child_predicates:
                continue
            if not predicate(element):
                return False
        return True

    # -- positional part ---------------------------------------------------

    def position_state(self, element, compound):
        if not compound.positions:
            return TRUE

        # only-child / only-of-type impose a hard peer-count ceiling that no
        # amount of insertion can relax.
        for spec in compound.positions:
            if spec.kind == 'flag' and spec.name.startswith('only'):
                if self.completion.known_peer_count(
                        element, spec.is_type_based) > 1:
                    return FALSE

        # Group the remaining constraints by (typed axis, direction).  Every
        # constraint in one group must hold at the *same* completed index.
        groups = {}
        for spec in compound.positions:
            if spec.kind == 'flag' and spec.name.startswith('only'):
                # only-X == first-X and last-X simultaneously.
                groups.setdefault((spec.is_type_based, False), []).append(
                    _IndexConstraint.exact(spec, 1))
                groups.setdefault((spec.is_type_based, True), []).append(
                    _IndexConstraint.exact(spec, 1))
            else:
                key = (spec.is_type_based, spec.is_reverse)
                groups.setdefault(key, []).append(
                    _IndexConstraint.from_spec(spec))

        states = []
        for (same_type, reverse), constraints in groups.items():
            states.append(self._axis_group_state(
                element, same_type, reverse, constraints))
        return _combine(states)

    def _axis_group_state(self, element, same_type, reverse, constraints):
        observed = (
            self.completion.observed_reverse_index(element, same_type)
            if reverse else
            self.completion.observed_index(element, same_type))
        gap_open = (
            self.completion.trailing_gap_open(element, same_type)
            if reverse else
            self.completion.preceding_gap_open(element, same_type))

        if not gap_open:
            # The completed index on this axis is fixed at the observed one.
            return TRUE if all(constraint.holds(observed)
                               for constraint in constraints) else FALSE

        if any(constraint.impossible_above(observed)
               for constraint in constraints):
            return FALSE
        upper = _search_bound(observed, constraints)
        for index in range(observed, upper + 1):
            if all(constraint.holds(index) for constraint in constraints):
                self.note(INCOMPLETE_SIBLINGS)
                self.note(MISSING_FOLLOWING_SIBLING if reverse
                          else MISSING_PRECEDING_SIBLING)
                return UNKNOWN
        return FALSE

    # -- root requirement --------------------------------------------------

    def root_state(self, element, compound):
        if not compound.requires_root:
            return TRUE
        if element.getparent() is None:
            if self.coverage.whole_document_complete(self.root):
                return TRUE
            # The fragment root may still have uncollected ancestors above.
            self.note(MISSING_PARENT)
            return UNKNOWN
        if not self.coverage.element_complete(element.getparent()):
            self.note(MISSING_PARENT)
            return UNKNOWN
        return FALSE

    # -- embedded :has / :not / :is ---------------------------------------

    def embedded_state(self, element, compound):
        states = []
        # Distinct :has() calls must all hold (AND); the comma separated
        # arguments of one :has() are alternatives (OR).
        for has_group in compound.has_groups:
            branch_states = [self._has_state(element, spec)
                             for spec in has_group]
            states.append(_combine_disjunction(branch_states))
        for embedded in compound.negated_selectors:
            states.append(_negate(
                self._full_selector_state(element, embedded)))
        if compound.is_branches:
            states.append(_combine_disjunction([
                self._full_selector_state(element, branch)
                for branch in compound.is_branches]))
        return _combine(states)

    def _has_state(self, scope, spec):
        # :has() matches are confined to the scope element's subtree (or, for
        # a leading sibling combinator, to its following siblings).  The chain
        # is therefore evaluated source-order/forward so it can never climb
        # out to the scope element's own ancestors.
        leading, compounds, combinators = spec
        unknown_axis = False
        if leading in (' ', '>'):
            # The descendant/child axis is open whenever *any* element in the
            # scope subtree has an incomplete child list, not just the scope
            # itself: a missing match may sit several levels down.
            if leading == '>':
                if not self.coverage.children_complete(scope):
                    unknown_axis = True
                    self.note(MISSING_CHILD)
                    self.note(INCOMPLETE_CHILDREN)
            elif self._subtree_has_open_children(scope):
                unknown_axis = True
                self.note(MISSING_DESCENDANT)
                self.note(INCOMPLETE_CHILDREN)
            if leading == '>':
                starts = [child for child in scope
                          if isinstance(child.tag, str)]
            else:
                starts = [node for node in scope.iter()
                          if node is not scope and isinstance(node.tag, str)]
        else:
            if not self.coverage.siblings_complete(scope):
                unknown_axis = True
                self.note(MISSING_FOLLOWING_SIBLING)
                self.note(INCOMPLETE_SIBLINGS)
            sibling = scope.getnext()
            if leading == '+':
                starts = ([sibling] if sibling is not None
                          and isinstance(sibling.tag, str) else [])
            else:
                starts = []
                while sibling is not None:
                    if isinstance(sibling.tag, str):
                        starts.append(sibling)
                    sibling = sibling.getnext()

        source_compounds = list(reversed(compounds))
        source_combinators = list(reversed(combinators))
        current = [candidate for candidate in dict.fromkeys(starts)
                   if self._compound_state(
                       candidate, source_compounds[0]) in (TRUE, UNKNOWN)]
        combined = self._match_candidates(starts, source_compounds[0])
        for index, combinator in enumerate(source_combinators):
            current, combined = self._forward_step(
                current, combinator, source_compounds[index + 1])
            if combined == FALSE:
                break
        if combined == FALSE and unknown_axis:
            return UNKNOWN
        return combined

    def _match_candidates(self, candidates, compound):
        if not candidates:
            return FALSE
        return _combine_disjunction([
            self._compound_state(candidate, compound)
            for candidate in dict.fromkeys(candidates)])

    def _subtree_has_open_children(self, scope):
        if not self.coverage.children_complete(scope):
            return True
        for descendant in scope.iter():
            if descendant is scope:
                continue
            if isinstance(descendant.tag, str) and \
                    not self.coverage.children_complete(descendant):
                return True
        return False

    def _forward_step(self, current, combinator, compound):
        candidates = []
        unknown_axis = False
        for node in current:
            if combinator == '>':
                if not self.coverage.children_complete(node):
                    unknown_axis = True
                    self.note(MISSING_CHILD)
                    self.note(INCOMPLETE_CHILDREN)
                candidates.extend(
                    child for child in node if isinstance(child.tag, str))
            elif combinator == ' ':
                if not self.coverage.children_complete(node):
                    unknown_axis = True
                    self.note(MISSING_DESCENDANT)
                    self.note(INCOMPLETE_CHILDREN)
                candidates.extend(
                    descendant for descendant in node.iter()
                    if descendant is not node
                    and isinstance(descendant.tag, str))
            else:
                if not self.coverage.siblings_complete(node):
                    unknown_axis = True
                    self.note(MISSING_FOLLOWING_SIBLING)
                    self.note(INCOMPLETE_SIBLINGS)
                sibling = node.getnext()
                if combinator == '+':
                    if sibling is not None and isinstance(sibling.tag, str):
                        candidates.append(sibling)
                else:
                    while sibling is not None:
                        if isinstance(sibling.tag, str):
                            candidates.append(sibling)
                        sibling = sibling.getnext()
        combined = self._match_candidates(candidates, compound)
        matched = [candidate for candidate in dict.fromkeys(candidates)
                   if self._compound_state(candidate, compound)
                   in (TRUE, UNKNOWN)]
        if combined == FALSE and unknown_axis:
            return matched, UNKNOWN
        return matched, combined

    # -- full selector chain ----------------------------------------------

    def _full_selector_state(self, element, parsed_selector):
        compounds, combinators = parsed_selector
        state = self._compound_state(element, compounds[0])
        if state == FALSE:
            return FALSE
        current_nodes = [element]
        for step, combinator in enumerate(combinators, start=1):
            current_nodes, step_state = self._combinator_state(
                current_nodes, combinator, compounds[step])
            state = _combine([state, step_state])
            if state == FALSE:
                return FALSE
        return state

    def _compound_state(self, element, compound):
        local = self.local_state(element, compound)
        if local == FALSE:
            # A hard local mismatch settles the compound; the remaining axes
            # must not contribute evidence for an element that cannot match.
            return FALSE
        if compound.selection_filters and \
                not self.ignore_selection_filters:
            if not self.coverage.whole_document_complete(self.root):
                # The element may match the compound, but its rank in the
                # full selection set is unknowable until the whole page is
                # collected.
                self.note(INCOMPLETE_SELECTION)
                return UNKNOWN
            if self.selection_rank is None:
                return FALSE
            for name, value in compound.selection_filters:
                if not self._selection_filter_matches(name, value):
                    return FALSE
        return _combine([
            local,
            self.contains_state(element, compound),
            self.position_state(element, compound),
            self.root_state(element, compound),
            self.embedded_state(element, compound),
        ])

    def contains_state(self, element, compound):
        if not compound.contains_needles:
            return TRUE
        observed_text = ''.join(element.itertext())
        if all(needle in observed_text
               for needle in compound.contains_needles):
            return TRUE
        # The needle is absent from the collected text.  It could still appear
        # inside a descendant that was not collected, so an open subtree keeps
        # the test undecidable rather than false.
        if self._subtree_has_open_children(element):
            self.note(MISSING_DESCENDANT)
            self.note(INCOMPLETE_CHILDREN)
            return UNKNOWN
        return FALSE

    def _selection_filter_matches(self, name, value):
        rank = self.selection_rank
        size = self.selection_size
        if name == 'first':
            return rank == 0
        if name == 'last':
            return rank == size - 1
        if name == 'even':
            return rank % 2 == 0
        if name == 'odd':
            return rank % 2 == 1
        if name == 'eq':
            target = size + value if value < 0 else value
            return rank == target
        if name == 'gt':
            return rank > value
        if name == 'lt':
            return rank < value
        raise _UnsupportedSelector(UNSUPPORTED_PSEUDO, f':{name}')

    def _combinator_state(self, current_nodes, combinator, next_compound):
        candidates = []
        axis_unknown = False
        for node in current_nodes:
            related, unknown = self._related_nodes(node, combinator)
            axis_unknown = axis_unknown or unknown
            candidates.extend(related)
        candidate_states = []
        for candidate in dict.fromkeys(candidates):
            candidate_states.append(
                self._compound_state(candidate, next_compound))
        combined = _combine_disjunction(candidate_states)
        if combined == FALSE and axis_unknown:
            if combinator == '>':
                self.note(MISSING_PARENT)
            elif combinator == ' ':
                self.note(MISSING_ANCESTOR)
            else:
                self.note(MISSING_PRECEDING_SIBLING)
                self.note(INCOMPLETE_SIBLINGS)
            return list(dict.fromkeys(candidates)), UNKNOWN
        return list(dict.fromkeys(candidates)), combined

    def _related_nodes(self, node, combinator):
        unknown = False
        if combinator == '>':
            parent = node.getparent()
            if parent is None:
                # The fragment root genuinely has no parent only when the
                # whole document was certified collected; otherwise uncollected
                # ancestors may sit above it.
                return [], not self.coverage.whole_document_complete(self.root)
            if not self.coverage.element_complete(parent):
                unknown = True
            return [parent], unknown
        if combinator == ' ':
            parents = []
            parent = node.getparent()
            while parent is not None:
                if not self.coverage.element_complete(parent):
                    unknown = True
                parents.append(parent)
                parent = parent.getparent()
            if node.getparent() is None and \
                    not self.coverage.whole_document_complete(self.root):
                unknown = True
            return parents, unknown
        if combinator == '+':
            previous = node.getprevious()
            # Only mark the axis unknown when the sibling list is actually
            # incomplete; a certified first child simply has no predecessor.
            unknown = not self.coverage.siblings_complete(node)
            if previous is not None and isinstance(previous.tag, str):
                return [previous], unknown
            return [], unknown
        if combinator == '~':
            if not self.coverage.siblings_complete(node):
                unknown = True
            siblings = []
            sibling = node.getprevious()
            while sibling is not None:
                if isinstance(sibling.tag, str):
                    siblings.append(sibling)
                sibling = sibling.getprevious()
            return siblings, unknown
        raise _UnsupportedSelector('combinator', combinator)


def _combine(states):
    if FALSE in states:
        return FALSE
    if UNKNOWN in states:
        return UNKNOWN
    return TRUE


def _combine_disjunction(states):
    if TRUE in states:
        return TRUE
    if UNKNOWN in states:
        return UNKNOWN
    return FALSE


def _negate(state):
    if state == TRUE:
        return FALSE
    if state == FALSE:
        return TRUE
    return UNKNOWN


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class PartialRuleResult:
    """Verdict for one collection rule."""

    def __init__(self, selector, status, confirmed_matches=None,
                 possible_matches=None, evidence=None, unsupported=None):
        self.selector = selector
        self.status = status
        self.confirmed_matches = list(confirmed_matches or ())
        self.possible_matches = list(possible_matches or ())
        self.evidence = sorted(evidence or (), key=str)
        self.unsupported = unsupported

    @property
    def matches(self):
        return self.confirmed_matches + self.possible_matches

    def __repr__(self):
        return (f'<PartialRuleResult {self.selector!r} {self.status} '
                f'confirmed={len(self.confirmed_matches)} '
                f'possible={len(self.possible_matches)} '
                f'evidence={self.evidence!r}>')

    def as_dict(self):
        return {
            'selector': self.selector,
            'status': self.status,
            'confirmed_matches': self.confirmed_matches,
            'possible_matches': self.possible_matches,
            'evidence': self.evidence,
            'unsupported': self.unsupported,
        }


def _check_translatable(selector):
    translator = JQueryTranslator()
    try:
        translator.css_to_xpath(selector)
    except (ExpressionError, SelectorError) as exc:
        raise _UnsupportedSelector(UNSUPPORTED_PSEUDO, str(exc))


def _parse_groups(selector):
    _check_translatable(selector)
    try:
        groups = parse_css(selector)
    except SelectorError as exc:
        raise _UnsupportedSelector(UNSUPPORTED_PSEUDO, str(exc))
    if any(group.pseudo_element is not None for group in groups):
        raise _UnsupportedSelector(UNSUPPORTED_PSEUDO, 'pseudo-element')
    return [_parse_selector_tree(group.parsed_tree) for group in groups]


def evaluate_rules(root, selectors, coverage):
    """Evaluate several CSS collection rules against one partial tree.

    ``root`` is an lxml element or a :class:`~pyquery.pyquery.PyQuery`
    instance holding the collected fragment, ``selectors`` an iterable of CSS
    selector strings and ``coverage`` a :class:`PartialCoverage`.

    A single :class:`SharedCompletion` is built for the whole run, so every
    positional condition across every rule is judged against the same
    completed page.  Returns one :class:`PartialRuleResult` per selector in
    input order.
    """
    if etree.iselement(root):
        fragment_root = root
    else:
        fragment_root = root[0] if len(root) else None
    if fragment_root is None:
        raise ValueError('empty fragment cannot be evaluated')

    completion = SharedCompletion(fragment_root, coverage)
    return [_evaluate_one(fragment_root, selector, coverage, completion)
            for selector in selectors]


def evaluate_rule(root, selector, coverage):
    """Evaluate a single CSS collection rule.

    See :func:`evaluate_rules`; returns one :class:`PartialRuleResult`.
    """
    return evaluate_rules(root, [selector], coverage)[0]


def _closure_evidence(root, groups, coverage):
    """Evidence that a legal completion could *add* matches to the result.

    Node level tri-states only certify observed elements.  This pass inspects
    the open regions of the fragment and asks whether a fabricated element
    placed there could satisfy a selector group, so an uncollected node is
    never silently treated as absent.  It deliberately over-approximates:
    downgrading a closed rule to ``possible`` is safe, the reverse would not.
    """
    incomplete_parents = [
        element for element in root.iter()
        if isinstance(element.tag, str)
        and not coverage.children_complete(element)]
    open_above = root.getparent() is not None and \
        not coverage.element_complete(root.getparent())
    if not incomplete_parents and not open_above:
        return set()

    evidence = set()
    for compounds, combinators in groups:
        target = compounds[0]
        if target.requires_root:
            # A document has exactly one root; no extra root can be inserted.
            continue
        uses_sibling_axis = bool(target.positions) or \
            any(combinator in ('+', '~') for combinator in combinators)
        uses_child_axis = any(combinator == '>' for combinator in combinators)
        uses_descendant_axis = ' ' in combinators

        if incomplete_parents:
            # A new element child can be inserted into each open parent.
            evidence.add(INCOMPLETE_CHILDREN)
            evidence.add(MISSING_CHILD)
            if uses_sibling_axis or not combinators:
                evidence.add(INCOMPLETE_SIBLINGS)
                evidence.add(MISSING_PRECEDING_SIBLING)
                evidence.add(MISSING_FOLLOWING_SIBLING)
            if uses_descendant_axis:
                evidence.add(MISSING_DESCENDANT)
            elif uses_child_axis:
                evidence.add(MISSING_CHILD)
        if open_above:
            # Unknown ancestors can carry their own subtrees with matches and
            # can place unknown siblings before the fragment root.
            evidence.add(MISSING_ANCESTOR)
            evidence.add(MISSING_PRECEDING_SIBLING)
            if uses_sibling_axis or not combinators:
                evidence.add(INCOMPLETE_SIBLINGS)
                evidence.add(MISSING_FOLLOWING_SIBLING)
    return evidence


def _compound_nested_filter(compound):
    """Selection-set filters nested inside this compound's :not/:is/:has()."""
    for group in compound.negated_selectors:
        if _group_any_selection_filter(group):
            return True
    for group in compound.is_branches:
        if _group_any_selection_filter(group):
            return True
    for has_group in compound.has_groups:
        for _leading, compounds, _combinators in has_group:
            if any(compound.selection_filters for compound in compounds):
                return True
            if any(_compound_nested_filter(compound)
                   for compound in compounds):
                return True
    return False


def _group_any_selection_filter(group):
    """Any selection-set filter anywhere in a nested selector group."""
    compounds, _combinators = group
    if any(compound.selection_filters for compound in compounds):
        return True
    return any(_compound_nested_filter(compound)
               for compound in compounds)


def _has_embedded_selection_filter(group):
    # The outer target compound (index 0) may carry the selection filter;
    # filters on inner compounds or inside nested selectors are unsupported.
    compounds, _combinators = group
    if any(compound.selection_filters for compound in compounds[1:]):
        return True
    return any(_compound_nested_filter(compound)
               for compound in compounds)


def _evaluate_one(root, selector, coverage, completion):
    try:
        groups = _parse_groups(selector)
    except _UnsupportedSelector as exc:
        return PartialRuleResult(
            selector, UNSUPPORTED,
            unsupported={'reason': exc.reason, 'detail': exc.detail})

    # Selection-set filters (:first/:eq/...) are only well defined on the
    # target compound that produces the selection set.  On an inner compound
    # or inside :not()/:has()/:is() their rank semantics are ambiguous and
    # PyQuery's own translation is unreliable, so report them explicitly
    # instead of returning a confident but divergent answer.
    if any(_has_embedded_selection_filter(group) for group in groups):
        return PartialRuleResult(
            selector, UNSUPPORTED, unsupported={
                'reason': UNSUPPORTED_PSEUDO,
                'detail': 'selection-set filter in a nested selector'})

    whole_document = coverage.whole_document_complete(root)
    rank_maps = []
    if whole_document:
        for group in groups:
            ordered = []
            for element in root.iter():
                if not isinstance(element.tag, str):
                    continue
                evaluation = _Evaluation(
                    root, coverage, completion,
                    ignore_selection_filters=True)
                if evaluation._full_selector_state(element, group) == TRUE:
                    ordered.append(element)
            rank_maps.append({
                element: (rank, len(ordered))
                for rank, element in enumerate(ordered)})
    else:
        rank_maps = [None] * len(groups)

    confirmed = []
    possible = []
    evidence = set()
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        states = []
        for group_index, group in enumerate(groups):
            rank_map = rank_maps[group_index]
            rank = size = None
            if rank_map is not None:
                rank, size = rank_map.get(element, (None, None))
            evaluation = _Evaluation(
                root, coverage, completion, rank, size)
            states.append(evaluation._full_selector_state(element, group))
            if states[-1] in (TRUE, UNKNOWN):
                evidence.update(evaluation.evidence)
        state = _combine_disjunction(states)
        if state == TRUE:
            confirmed.append(element)
        elif state == UNKNOWN:
            possible.append(element)

    # Closure: even when every observed element is settled, an open region may
    # still contribute a brand new match in some legal completion.
    evidence.update(_closure_evidence(root, groups, coverage))

    if possible or evidence:
        return PartialRuleResult(selector, POSSIBLE, confirmed, possible,
                                 evidence)
    return PartialRuleResult(selector, CONFIRMED, confirmed)


def verify_completion(fragment_root, completed_root, selector):
    """Compare partial and full-page queries after the missing DOM arrives.

    ``completed_root`` must be a legal in-place completion of
    ``fragment_root``: the same collected element objects, with unknown nodes
    inserted around them and nothing removed or rewritten.  The selector is
    executed with ordinary PyQuery semantics on both trees; the call returns
    both match lists and confirms that every fragment match survives in the
    completed page.  Running PyQuery directly on ``completed_root`` is exactly
    the full-page query, so the two stay consistent by construction.
    """
    from .pyquery import PyQuery

    fragment_ids = _collected_identities(fragment_root)
    _assert_legal_completion(fragment_root, completed_root, fragment_ids)
    fragment_matches = list(PyQuery(fragment_root)(selector))
    completed_matches = list(PyQuery(completed_root)(selector))
    completed_ids = {id(element) for element in completed_matches}
    return {
        'selector': selector,
        'fragment_matches': fragment_matches,
        'completed_matches': completed_matches,
        'confirmed_subset_present': all(
            id(element) in completed_ids for element in fragment_matches),
        'full_page_query': completed_matches,
    }


def _collected_identities(root):
    return {id(element): dict(element.attrib)
            for element in root.iter() if isinstance(element.tag, str)}


def _assert_legal_completion(fragment_root, completed_root, fragment_ids):
    seen = set()
    for element in completed_root.iter():
        if not isinstance(element.tag, str):
            continue
        identity = id(element)
        if identity in fragment_ids:
            seen.add(identity)
            for name, value in fragment_ids[identity].items():
                if element.get(name) != value:
                    raise ValueError(
                        'illegal completion: collected attribute '
                        f'{name!r} was modified')
    if seen != set(fragment_ids):
        raise ValueError(
            'illegal completion: some collected elements are missing')
