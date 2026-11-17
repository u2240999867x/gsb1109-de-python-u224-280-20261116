# Copyright (C) 2008 - Olivier Lauzanne <olauzanne@gmail.com>
#
# Distributed under the BSD license, see LICENSE.txt
"""Tests for partial-DOM collection rule reasoning (``pyquery.partial``).

The central safety property exercised here is: a rule is only ever reported
``confirmed`` when the residual tree, together with its coverage metadata,
already determines the answer exactly.  Anything that an uncollected node
could still change must come back ``possible`` with the structural evidence
explaining why, and selectors the engine cannot reason about are reported
``unsupported`` rather than answered by an ordinary query on the residual
tree.
"""
from copy import deepcopy
from unittest import TestCase

from lxml import etree

from pyquery import PyQuery as pq
from pyquery.partial import (
    CONFIRMED,
    POSSIBLE,
    UNSUPPORTED,
    PartialCoverage,
    evaluate_rule,
    evaluate_rules,
    verify_completion,
)


def parse(html):
    return etree.fromstring(f'<root>{html}</root>')


class CompleteDocumentTests(TestCase):
    """On a fully collected document every rule must be confirmed exactly."""

    HTML = '''
    <section>
      <ul id="list">
        <li class="a x" data-k="v1"><a href="#">one</a></li>
        <li class="b">two</li>
        <li class="c">three</li>
      </ul>
      <div class="card"><p>hello</p><p>world</p></div>
      <input type="text" checked="checked"></input>
    </section>
    '''

    SELECTORS = [
        # type, class, id, attribute combinations
        'li', '.a', '#list', 'li.a', 'li.x.a',
        '[data-k]', '[data-k="v1"]', 'li[data-k="v1"]',
        '[class~="a"]', '[data-k^="v"]', '[data-k$="1"]', '[data-k*="v"]',
        '[class|="a"]',
        # parent / child / sibling relations
        'ul > li', 'section li', 'li + li', 'li ~ li', 'ul li a',
        # :not and structural position
        'li:not(.a)', 'a:not([href])', 'li:first-child', 'li:last-child',
        'li:only-child', 'li:nth-child(2)', 'li:nth-child(2n+1)',
        'li:nth-child(odd)', 'li:nth-child(even)', 'li:nth-last-child(1)',
        'li:nth-of-type(1)', 'p:first-of-type', 'p:last-of-type',
        # grouped / relational / jQuery extensions
        ':is(li, span)', 'div:has(p)', 'div:has(> p)', 'li:eq(1)',
        'li:first', 'li:even', 'li:gt(0)', 'p:contains("hello")',
        ':header', 'div:parent', 'input:checked', 'input:text',
        'li:not(.x):not(.b)',
    ]

    def setUp(self):
        self.root = parse(self.HTML)
        self.coverage = PartialCoverage.whole_document(self.root)
        self.real = pq(etree.tostring(self.root, encoding=str))

    def test_every_selector_confirmed_and_matches_pyquery(self):
        for selector in self.SELECTORS:
            result = evaluate_rule(self.root, selector, self.coverage)
            self.assertEqual(
                result.status, CONFIRMED,
                f'{selector} should be confirmed on a full document')
            self.assertEqual(
                len(result.confirmed_matches), len(self.real(selector)),
                f'{selector} match count differs from PyQuery')
            self.assertEqual(result.evidence, [])

    def test_empty_and_only_children_on_full_document(self):
        root = parse('<div></div><div><p/></div><div> </div>')
        coverage = PartialCoverage.whole_document(root)
        real = pq(etree.tostring(root, encoding=str))
        for selector in ('div:empty', 'div:parent'):
            result = evaluate_rule(root, selector, coverage)
            self.assertEqual(result.status, CONFIRMED)
            self.assertEqual(
                len(result.confirmed_matches), len(real(selector)))


class PartialCoverageTests(TestCase):

    def test_subtree_only_marks_descendants_complete(self):
        root = parse('<div><p><span/></p></div>')
        coverage = PartialCoverage.subtree_only(root)
        span = root[0][0][0]
        self.assertTrue(coverage.element_complete(span))
        self.assertTrue(coverage.attributes_complete(span))
        self.assertTrue(coverage.children_complete(root[0]))
        self.assertTrue(coverage.siblings_complete(span))

    def test_unknown_coverage_axis_is_conservative(self):
        root = parse('<div class="box">x</div>')
        div = root[0]
        # Element present but its attribute list is not certified complete.
        coverage = PartialCoverage([
            {'element': root, 'complete': ('element', 'attributes',
                                           'children')},
            {'element': div, 'complete': 'children'},
        ])
        self.assertFalse(coverage.attributes_complete(div))
        for selector in ('div.box', 'div[data-id]', 'div:not([data-id])'):
            result = evaluate_rule(root, selector, coverage)
            self.assertEqual(result.status, POSSIBLE)
            self.assertIn('incomplete_attributes', result.evidence)

    def test_complete_attributes_settle_absence(self):
        root = parse('<div class="box">x</div>')
        div = root[0]
        coverage = PartialCoverage([
            {'element': root, 'complete': ('element', 'attributes',
                                           'children')},
            {'element': div, 'complete': ('attributes', 'children')},
        ])
        missing = evaluate_rule(root, 'div[data-id]', coverage)
        self.assertEqual(missing.status, CONFIRMED)
        self.assertEqual(missing.confirmed_matches, [])
        negated = evaluate_rule(root, 'div:not([data-id])', coverage)
        self.assertEqual(negated.status, CONFIRMED)
        self.assertEqual(len(negated.confirmed_matches), 1)

    def test_unknown_axis_rejected(self):
        root = parse('<div/>')
        with self.assertRaises(ValueError):
            PartialCoverage([{'element': root[0], 'complete': 'nonsense'}])


class MissingSiblingTests(TestCase):
    """The first ``li`` of the real page was not collected."""

    def setUp(self):
        # Real page would be <li class="a"/><li class="b"/><li class="c"/>.
        self.root = parse('<ul><li class="b"/><li class="c"/></ul>')
        self.ul = self.root[0]
        first, second = list(self.ul)
        self.coverage = PartialCoverage([
            {'element': self.root,
             'complete': ('element', 'attributes', 'children')},
            # The ul exists but its child list is explicitly incomplete.
            {'element': self.ul,
             'complete': ('element', 'attributes')},
            {'element': first, 'complete': 'subtree'},
            {'element': second, 'complete': 'subtree'},
        ])

    def test_type_and_class_still_node_confirmed(self):
        result = evaluate_rule(self.root, 'li.b', self.coverage)
        # The observed node genuinely matches; only the result *set* is open.
        self.assertEqual(result.status, POSSIBLE)
        self.assertEqual(len(result.confirmed_matches), 1)
        self.assertEqual(len(result.possible_matches), 0)

    def test_missing_preceding_sibling_downgrades_position_rules(self):
        for selector in ('li:first-child', 'li:nth-child(1)',
                         'li:nth-child(2)', 'li:nth-child(2n+1)'):
            result = evaluate_rule(self.root, selector, self.coverage)
            self.assertEqual(result.status, POSSIBLE, selector)
            self.assertIn('missing_preceding_sibling', result.evidence)
            self.assertIn('incomplete_siblings', result.evidence)

    def test_hard_position_contradiction_is_false_not_unknown(self):
        # The second collected child already has an immutable predecessor, so
        # it can never become the first child; insertions never remove nodes.
        result = evaluate_rule(self.root, 'li:first-child', self.coverage)
        self.assertNotIn(self.ul[1], result.possible_matches)
        # The first collected child may legitimately stay first (a completion
        # may insert zero siblings), so it remains merely possible.
        self.assertEqual(result.possible_matches, [self.ul[0]])
        # An index that no element can ever reach is false, not unknown.
        impossible = evaluate_rule(
            self.root, 'li:nth-child(0)', self.coverage)
        self.assertEqual(impossible.confirmed_matches, [])
        self.assertEqual(impossible.possible_matches, [])

    def test_mutually_exclusive_positions_share_one_completion(self):
        # No single completed index can satisfy both nth-child(2) and (3).
        result = evaluate_rule(
            self.root, 'li:nth-child(2):nth-child(3)', self.coverage)
        self.assertEqual(result.possible_matches, [])
        self.assertEqual(result.confirmed_matches, [])
        # nth-child(2) and even are jointly satisfiable.
        satisfiable = evaluate_rule(
            self.root, 'li:nth-child(2):nth-child(even)', self.coverage)
        self.assertEqual(len(satisfiable.possible_matches), 2)

    def test_one_completion_is_reused_across_rules(self):
        results = evaluate_rules(
            self.root,
            ['li:nth-child(2)', 'li:nth-child(3)', 'li:nth-child(4)'],
            self.coverage)
        # All stay possible on the same completion; evidence never claims an
        # observed node is confirmed merely because each rule in isolation
        # could be made true by a different page.
        self.assertTrue(all(r.status == POSSIBLE for r in results))
        for result in results:
            self.assertIn('missing_preceding_sibling', result.evidence)

    def test_adjacent_sibling_relation_is_open(self):
        result = evaluate_rule(self.root, 'li + li', self.coverage)
        self.assertEqual(result.status, POSSIBLE)
        # The second collected li has a known predecessor and is node-sound.
        self.assertEqual(len(result.confirmed_matches), 1)
        self.assertEqual(len(result.possible_matches), 1)

    def test_last_child_needs_following_sibling_evidence(self):
        result = evaluate_rule(self.root, 'li:last-child', self.coverage)
        self.assertEqual(result.status, POSSIBLE)
        self.assertIn('missing_following_sibling', result.evidence)


class MissingAncestorTests(TestCase):

    def setUp(self):
        # A collected subtree whose parent and outer siblings are unknown:
        # the fragment root itself has no in-tree parent.
        self.root = etree.fromstring('<div class="card"><p>text</p></div>')
        self.div = self.root
        self.coverage = PartialCoverage.subtree_only(self.root)

    def test_child_combinator_needs_parent(self):
        result = evaluate_rule(self.root, 'section > div', self.coverage)
        self.assertEqual(result.status, POSSIBLE)
        self.assertIn('missing_parent', result.evidence)

    def test_descendant_combinator_is_open_above(self):
        result = evaluate_rule(self.root, 'main div', self.coverage)
        self.assertEqual(result.status, POSSIBLE)
        self.assertIn('missing_ancestor', result.evidence)

    def test_local_rule_without_axis_is_node_confirmed(self):
        result = evaluate_rule(self.root, 'div.card', self.coverage)
        self.assertEqual(len(result.confirmed_matches), 1)
        self.assertEqual(result.possible_matches, [])


class NegationAndRelationalTests(TestCase):

    HTML = ('<section><div class="card"><p>x</p></div>'
            '<div class="card"><p>y</p></div></section>')

    def _coverage_with_open_section(self, root):
        section = root[0]
        return PartialCoverage([
            {'element': root,
             'complete': ('element', 'attributes', 'children')},
            {'element': section,
             'complete': ('element', 'attributes')},
            *[{'element': child, 'complete': 'subtree'}
              for child in section],
        ])

    def test_not_with_complex_argument(self):
        root = parse(self.HTML)
        coverage = PartialCoverage.whole_document(root)
        real = pq(etree.tostring(root, encoding=str))
        for selector in ('div:not(section > div)', 'p:not(:first-child)',
                         'div:not(.card)'):
            result = evaluate_rule(root, selector, coverage)
            self.assertEqual(result.status, CONFIRMED, selector)
            self.assertEqual(
                len(result.confirmed_matches), len(real(selector)), selector)

    def test_double_negation_is_intersection(self):
        root = parse('<ul><li class="x"/><li class="y"/>'
                     '<li class="z"/><li/></ul>')
        coverage = PartialCoverage.whole_document(root)
        result = evaluate_rule(
            root, 'li:not(.x):not(.y)', coverage)
        self.assertEqual(result.status, CONFIRMED)
        self.assertEqual(len(result.confirmed_matches), 2)

    def test_has_is_scoped_to_its_subtree(self):
        root = parse(self.HTML)
        coverage = PartialCoverage.whole_document(root)
        # section is an ancestor of p, but :has() must not climb out of div.
        result = evaluate_rule(root, 'div:has(section p)', coverage)
        self.assertEqual(result.status, CONFIRMED)
        self.assertEqual(result.confirmed_matches, [])
        scoped = evaluate_rule(root, 'div:has(p)', coverage)
        self.assertEqual(len(scoped.confirmed_matches), 2)

    def test_has_comma_arguments_are_disjunctive(self):
        root = parse('<div><em/></div><div><b/></div><div><p/></div>')
        coverage = PartialCoverage.whole_document(root)
        result = evaluate_rule(root, 'div:has(em, b)', coverage)
        self.assertEqual(result.status, CONFIRMED)
        self.assertEqual(len(result.confirmed_matches), 2)

    def test_has_with_incomplete_subtree_is_possible(self):
        root = parse('<div><span/></div>')
        div = root[0]
        coverage = PartialCoverage([
            {'element': root,
             'complete': ('element', 'attributes', 'children')},
            {'element': div,
             'complete': ('element', 'attributes')},
            {'element': div[0], 'complete': ('element', 'attributes')},
        ])
        result = evaluate_rule(root, 'div:has(p)', coverage)
        self.assertEqual(result.status, POSSIBLE)
        self.assertIn('missing_descendant', result.evidence)
        self.assertEqual(result.possible_matches, [div])

    def test_contains_with_pruned_descendant_is_possible(self):
        # The collected text does not contain the needle, but a pruned
        # descendant could carry it; absence must not be treated as settled.
        root = parse('<select>hi<p/></select>')
        select = root[0]
        paragraph = select[0]
        coverage = PartialCoverage([
            {'element': root,
             'complete': ('element', 'attributes', 'children')},
            {'element': select,
             'complete': ('element', 'attributes')},
            {'element': paragraph, 'complete': ('element', 'attributes')},
        ])
        result = evaluate_rule(root, 'select:contains("missing")', coverage)
        self.assertEqual(result.status, POSSIBLE)
        self.assertEqual(result.possible_matches, [select])
        self.assertIn('missing_descendant', result.evidence)

    def test_contains_observed_text_is_confirmed_on_full_document(self):
        root = parse('<select>hi<p/></select>')
        coverage = PartialCoverage.whole_document(root)
        present = evaluate_rule(root, 'select:contains("hi")', coverage)
        self.assertEqual(present.status, CONFIRMED)
        self.assertEqual(len(present.confirmed_matches), 1)
        absent = evaluate_rule(root, 'select:contains("nope")', coverage)
        self.assertEqual(absent.confirmed_matches, [])


class UnsupportedSelectorTests(TestCase):

    def setUp(self):
        self.root = parse('<div><a href="#">x</a></div>')
        self.coverage = PartialCoverage.whole_document(self.root)

    def test_unsupported_selectors_are_listed_explicitly(self):
        for selector in ('a::before', 'a:hover', ':dir(ltr)', 'ns|div',
                         ':nth-child(2 of .a)', 'a:any-link'):
            result = evaluate_rule(self.root, selector, self.coverage)
            self.assertEqual(result.status, UNSUPPORTED, selector)
            self.assertIsNotNone(result.unsupported, selector)
            # No match list is fabricated for a rule that cannot be reasoned
            # about on the residual tree.
            self.assertEqual(result.confirmed_matches, [])
            self.assertEqual(result.possible_matches, [])

    def test_residual_query_is_not_presented_as_answer(self):
        # Dynamic pseudo-classes cannot be derived from the tree.  The partial
        # engine must refuse instead of returning a (here empty or, in other
        # cssselect versions, arbitrary) residual-tree result as an answer.
        result = evaluate_rule(self.root, 'a:hover', self.coverage)
        self.assertEqual(result.status, UNSUPPORTED)
        self.assertEqual(result.matches, [])
        self.assertIn('hover', result.unsupported['detail'])

    def test_nested_selection_filter_is_unsupported(self):
        # jQuery selection-set filters are only meaningful on the target
        # compound; nested inside :not()/:has() PyQuery's own translation is
        # unreliable, so they must be refused rather than confirmed.
        root = parse('<ul><li/><li/></ul>')
        coverage = PartialCoverage.whole_document(root)
        for selector in ('li:not(:first)', 'li:not(:eq(0))',
                         'div:has(p:first)'):
            result = evaluate_rule(root, selector, coverage)
            self.assertEqual(result.status, UNSUPPORTED, selector)
        # The same filters on the target compound stay supported and exact.
        self.assertEqual(
            evaluate_rule(root, 'li:first', coverage).status, CONFIRMED)


class CompletionVerificationTests(TestCase):

    def test_completed_query_keeps_confirmed_matches(self):
        fragment = parse('<ul><li class="b"/><li class="c"/></ul>')
        ul = fragment[0]
        coverage = PartialCoverage([
            {'element': fragment,
             'complete': ('element', 'attributes', 'children')},
            {'element': ul,
             'complete': ('element', 'attributes')},
            *[{'element': child, 'complete': 'subtree'}
              for child in ul],
        ])
        partial = evaluate_rule(fragment, 'li:nth-child(2)', coverage)
        self.assertEqual(partial.status, POSSIBLE)

        # Legal in-place completion: insert the missing first li, keep objects.
        missing = etree.Element('li')
        missing.set('class', 'a')
        ul.insert(0, missing)
        report = verify_completion(fragment, fragment, 'li:nth-child(2)')
        self.assertTrue(report['confirmed_subset_present'])
        self.assertEqual(len(report['full_page_query']), 1)
        # The completed query is the ordinary full-page query.
        self.assertEqual(
            report['completed_matches'], list(pq(fragment)('li:nth-child(2)')))

    def test_illegal_completion_is_rejected(self):
        fragment = parse('<div data-id="1"/>')
        completed = deepcopy(fragment)
        completed[0].set('data-id', '2')  # rewrites a collected attribute
        with self.assertRaises(ValueError):
            verify_completion(fragment, completed, 'div')


class ResultShapeTests(TestCase):

    def test_result_dict_and_repr(self):
        root = parse('<ul><li/></ul>')
        coverage = PartialCoverage.whole_document(root)
        result = evaluate_rule(root, 'li', coverage)
        payload = result.as_dict()
        self.assertEqual(payload['status'], CONFIRMED)
        self.assertEqual(payload['selector'], 'li')
        self.assertIn('li', repr(result))

    def test_evaluate_rules_preserves_order(self):
        root = parse('<div><p/></div>')
        coverage = PartialCoverage.whole_document(root)
        results = evaluate_rules(root, ['div', 'p', 'span'], coverage)
        self.assertEqual([r.selector for r in results],
                         ['div', 'p', 'span'])
