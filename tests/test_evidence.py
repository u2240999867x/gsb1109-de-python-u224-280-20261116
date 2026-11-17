import json
import re

import pytest

from pyquery.evidence import (
    PLACEHOLDER_ATTR,
    PLACEHOLDER_TAG,
    EvidenceFragment,
    build_evidence_fragment,
    is_placeholder,
    query_evidence,
    real_nodes,
)
from pyquery.pyquery import PyQuery as pq


PAGE = '''<html><body><div id="results">
  <article class="row" data-owner="alice@example.com">
    <h2 class="title">Public one</h2>
  </article>
  <article class="row" data-owner="bob@example.com">
    <h2 class="title">Secret two</h2>
  </article>
  <article class="row" data-owner="carol@example.com">
    <h2 class="title">Public three</h2>
  </article>
  <p class="footnote">footnote one</p>
  <p class="footnote">footnote two</p>
</div></body></html>'''

ALLOWED = [
    'results', 'article', 'h2', 'p', 'row', 'title', 'footnote',
    'Public one', 'Public three', 'footnote one', 'footnote two',
]

QUERIES = [
    'article:nth-child(2)',
    'article:last',
    'article.row:first',
    'article:not(.row)',
    'p.footnote:first',
    'p.footnote:last',
]


def signatures(document, selector):
    return [(element.tag, element.get('class'))
            for element in document(selector)]


def assert_same_hits(page, selector, other):
    assert signatures(page, selector) == signatures(other, selector)


class TestEvidenceOrdering:
    def test_hit_order_matches_original(self):
        original = pq(PAGE)
        result = build_evidence_fragment(PAGE, QUERIES,
                                         allowed_values=ALLOWED,
                                         budget=4096)
        assert result.conflicts == []
        fragment_pq = pq(result.html)
        for selector in QUERIES:
            assert_same_hits(original, selector, fragment_pq)

    def test_placeholders_are_not_returned(self):
        result = build_evidence_fragment(
            '<ul><li>one</li><li>two</li><li>three</li></ul>',
            ['li:nth-child(2)'],
            allowed_values=['one', 'two', 'three'],
            budget=4096,
        )
        assert PLACEHOLDER_TAG in result.html
        matches = result.evaluate('li')
        assert len(matches) == 1
        assert matches[0].get('id') is None
        assert not any(is_placeholder(element, result.marker_attr,
                                      result.placeholder_tag)
                       for element in matches)
        assert len(result.evaluate('li:nth-child(2)')) == 1

    def test_positional_query_keeps_placeholder(self):
        result = build_evidence_fragment(
            '<div><span>a</span><span>b</span><span>c</span></div>',
            ['span:nth-child(2)'],
            allowed_values=['a', 'b', 'c'],
            budget=4096,
        )
        assert result.conflicts == []
        assert result.placeholders
        assert len(result.evaluate('span:nth-child(2)')) == 1

    def test_placeholder_has_no_business_content(self):
        result = build_evidence_fragment(
            '<ul><li data-account="9876543210">secret</li>'
            '<li>keep</li><li>other</li></ul>',
            ['li:nth-child(2)'],
            allowed_values=['keep', 'other'],
            budget=4096,
        )
        assert result.placeholders
        for placeholder in result.placeholders:
            assert placeholder.tag == PLACEHOLDER_TAG
            assert placeholder.original_tag == 'li'
        assert '9876543210' not in result.html
        assert 'secret' not in result.html

    def test_structure_placeholder_is_recognizable_in_html(self):
        result = build_evidence_fragment(
            '<div><b>x</b><i>y</i><b>z</b></div>',
            ['b:nth-child(3)'],
            allowed_values=['x', 'y', 'z'],
            budget=4096,
        )
        assert PLACEHOLDER_ATTR in result.html
        placeholder_nodes = [element for element in pq(result.html)('*')
                             if is_placeholder(element,
                                               result.marker_attr,
                                               result.placeholder_tag)]
        assert placeholder_nodes

    def test_reproduced_query_never_returns_placeholder_raw(self):
        """Even a plain ``pq(html)(query)`` must not see a placeholder."""
        page = '<div><p>a</p><b>x</b><p>c</p></div>'
        result = build_evidence_fragment(
            page, ['p:nth-of-type(2)', 'p:last'],
            allowed_values=['c'], budget=4096)
        assert result.conflicts == []
        raw = pq(result.html)('p:nth-of-type(2)')
        assert len(raw) == 1
        assert not is_placeholder(raw[0], result.marker_attr,
                                  result.placeholder_tag)

    def test_conflict_when_placeholder_would_be_a_raw_hit(self):
        page = '<div><p>a</p><b>x</b><p>c</p></div>'
        result = build_evidence_fragment(
            page, ['p:nth-of-type(2)', 'div *'],
            allowed_values=['c'], budget=4096)
        # Protecting "div *" prevents placeholders; "p:nth-of-type(2)" still
        # has to answer with the real second p.
        assert result.conflicts == []
        assert len(pq(result.html)('p:nth-of-type(2)')) == 1

    def test_marker_collision_with_real_nodes_is_avoided(self):
        page = ('<div><evidence-placeholder '
                'data-evidence-placeholder="real">x</evidence-placeholder>'
                '<p>keep</p><p>drop</p></div>')
        result = build_evidence_fragment(page, ['p:last'],
                                         allowed_values=['keep', 'drop'],
                                         budget=4096)
        assert result.marker_attr != 'data-evidence-placeholder'
        assert result.placeholder_tag != 'evidence-placeholder'
        assert not is_placeholder(pq(result.html)('p:last')[0],
                                  result.marker_attr,
                                  result.placeholder_tag)


class TestEvidenceConfidentiality:
    def test_text_and_attribute_values_are_redacted(self):
        result = build_evidence_fragment(PAGE, QUERIES,
                                         allowed_values=ALLOWED,
                                         budget=4096)
        assert 'bob@example.com' not in result.html
        assert 'carol@example.com' not in result.html
        assert 'alice@example.com' not in result.html
        assert 'Secret two' not in result.html
        assert 'Public one' in result.html

    def test_regex_rules(self):
        page = '<div><p id="a">id-123</p><p id="b">id-456</p></div>'
        result = build_evidence_fragment(
            page, ['p:last'],
            allowed_values=[re.compile(r'^id-\d+$')],
            budget=4096,
        )
        assert result.conflicts == []
        assert 'id-456' in result.html

    def test_callable_rules(self):
        page = '<div><p>alpha</p><p>beta</p></div>'
        result = build_evidence_fragment(
            page, ['p:last'],
            allowed_values=[lambda value: value.endswith('ish')],
            budget=4096,
        )
        assert result.conflicts == []
        assert result.html == '<div><p>[redacted]</p><p>[redacted]</p></div>' \
            or 'alpha' not in result.html

    def test_forbidden_values_win_over_allowed(self):
        page = '<div><p>alpha</p><p>beta</p></div>'
        result = build_evidence_fragment(
            page, ['p:last'],
            allowed_values=['alpha', 'beta'],
            forbidden_values=['beta'],
            budget=4096,
        )
        assert result.conflicts == []
        assert 'beta' not in result.html
        assert result.evaluate('p:last').attr('id') is None

    def test_forbidden_attribute_is_removed(self):
        page = ('<form><input name="q" value="secret-token">'
                '<input name="page" value="2"></form>')
        result = build_evidence_fragment(
            page, ['input[name="page"]'],
            allowed_values=['q', 'page', '2'],
            forbidden_attributes=['value'],
            budget=4096,
        )
        assert 'secret-token' not in result.html
        assert 'value=' not in result.html

    def test_presence_attributes_survive_redaction(self):
        page = ('<form><input type="checkbox" checked>'
                '<input type="checkbox"></form>')
        result = build_evidence_fragment(
            page, ['input:checked'],
            allowed_values=[],
            budget=4096,
        )
        assert result.conflicts == []
        assert 'checked' in result.html

    def test_no_redaction_when_allow_list_is_none(self):
        result = build_evidence_fragment(PAGE, ['article:last'],
                                         allowed_values=None, budget=8192)
        assert 'carol@example.com' in result.html
        # The last article itself is the only retained article, but its text
        # must be intact.
        assert 'Public three' in result.html
        assert 'evidence-redacted' not in result.html

    def test_minimize_keeps_hit_subtrees(self):
        result = build_evidence_fragment(PAGE, ['article:first'],
                                         allowed_values=ALLOWED, budget=8192)
        assert 'Public one' in result.html
        assert result.evaluate('article:first h2').text() == 'Public one'


class TestEvidenceConflicts:
    def test_value_dependent_selector_is_conflict(self):
        page = '<div><input name="q" value="secret-token"></div>'
        result = build_evidence_fragment(
            page, ['input[value="secret-token"]'],
            allowed_values=['q'],
            budget=4096,
        )
        assert result.conflicts == ['input[value="secret-token"]']
        assert result.conflict_details[0].reason == 'confidential_value'
        assert result.query_hits['input[value="secret-token"]'] == ()

    def test_class_dependent_selector_is_conflict(self):
        page = '<ul><li class="keep">a</li><li class="secret">b</li></ul>'
        result = build_evidence_fragment(
            page, ['li.secret'],
            allowed_values=['keep', 'a', 'b'],
            budget=4096,
        )
        assert result.conflicts == ['li.secret']
        assert result.reproduced_queries == []

    def test_text_dependent_selector_is_conflict(self):
        page = '<div><p>alpha</p><p>secret beta</p></div>'
        result = build_evidence_fragment(
            page, ['p:contains("secret")'],
            allowed_values=['alpha', 'beta'],
            budget=4096,
        )
        assert result.conflicts == ['p:contains("secret")']

    def test_id_dependent_selector_is_conflict(self):
        page = '<div><span id="user-8842">Alice</span></div>'
        result = build_evidence_fragment(
            page, ['#user-8842'],
            allowed_values=['Alice'],
            budget=4096,
        )
        assert result.conflicts == ['#user-8842']

    def test_invalid_selector_is_conflict_not_crash(self):
        result = build_evidence_fragment(
            '<div><p>a</p></div>', ['p::nonsense'],
            allowed_values=['a'], budget=4096,
        )
        assert result.conflicts == ['p::nonsense']
        assert result.conflict_details[0].reason == 'invalid_selector'

    def test_only_conflicting_queries_are_listed(self):
        page = '<div><p class="a">one</p><p class="b">two</p></div>'
        result = build_evidence_fragment(
            page, ['p.a', 'p.b', 'p:last'],
            allowed_values=['one', 'two', 'a'],
            budget=4096,
        )
        assert result.conflicts == ['p.b']
        assert result.reproduced_queries == ['p.a', 'p:last']
        assert result.query_hits['p.b'] == ()
        assert len(result.evaluate('p:last')) == 1

    def test_conflict_object_compares_with_string(self):
        result = build_evidence_fragment(
            '<div><p>alpha</p><p>beta</p></div>',
            ['p:contains("beta")'],
            allowed_values=['alpha'],
            budget=4096,
        )
        assert result.conflict_details[0] == 'p:contains("beta")'
        assert str(result.conflict_details[0]) == 'p:contains("beta")'


class TestEvidenceBudget:
    def test_fragment_respects_budget(self):
        result = build_evidence_fragment(PAGE, ['article:last'],
                                         allowed_values=ALLOWED,
                                         budget=4096)
        assert result.size <= 4096
        assert result.within_budget
        assert len(result.html.encode('utf-8')) == result.size

    def test_oversized_fragment_is_flagged(self):
        result = build_evidence_fragment(PAGE, ['article:last'],
                                         allowed_values=ALLOWED,
                                         budget=10)
        assert result.budget_exceeded
        assert not result.within_budget
        assert result.ok is False

    def test_max_bytes_alias(self):
        result = build_evidence_fragment(PAGE, ['article:last'],
                                         allowed_values=ALLOWED,
                                         max_bytes=4096)
        assert result.budget == 4096
        assert result.within_budget

    def test_budget_and_max_bytes_disagree(self):
        with pytest.raises(ValueError):
            build_evidence_fragment(PAGE, ['article:last'],
                                    allowed_values=ALLOWED,
                                    budget=10, max_bytes=20)

    def test_negative_budget(self):
        with pytest.raises(ValueError):
            build_evidence_fragment(PAGE, ['article:last'],
                                    allowed_values=ALLOWED, budget=-1)


class TestEvidenceSerialization:
    @pytest.mark.parametrize('markup, query', [
        ('<ul><li>a</li><li>b</li></ul>', 'li:last'),
        ('<table><tr><td>a</td><td>b</td></tr></table>', 'td:last'),
        ('<select><option>a</option><option>b</option></select>',
         'option:last'),
        ('<dl><dt>a</dt><dd>b</dd></dl>', 'dd:last'),
    ])
    def test_empty_siblings_survive_roundtrip(self, markup, query):
        result = build_evidence_fragment(markup, [query],
                                         allowed_values=['a', 'b'],
                                         budget=4096)
        assert result.conflicts == []
        reparsed = pq(result.html)
        assert assert_same_hits(pq(markup), query, reparsed) is None

    def test_multiple_top_level_roots(self):
        page = ('<div class="a">one</div>'
                '<div class="b">two</div>')
        result = build_evidence_fragment(
            page, ['div.b'], allowed_values=['a', 'b', 'one', 'two'],
            budget=4096)
        assert result.conflicts == []
        assert result.html.count('<div') == 2

    def test_comment_and_script_nodes(self):
        page = ('<!-- reviewer note -->'
                '<div><script>if (a < b) { x(); }</script>'
                '<p>keep</p></div>')
        result = build_evidence_fragment(page, ['p'],
                                         allowed_values=['keep'],
                                         budget=4096)
        assert result.conflicts == []
        assert result.html.count('<!--') == 0
        fragment = pq(result.html)
        assert fragment('p').text() == 'keep'

    def test_empty_selection(self):
        result = build_evidence_fragment('<div><p>a</p></div>',
                                         ['span'],
                                         allowed_values=['a'], budget=4096)
        assert result.conflicts == []
        assert result.evaluate('span') == []

    def test_void_elements(self):
        page = '<div><br><img src="x"><p>a</p></div>'
        result = build_evidence_fragment(page, ['div'],
                                         allowed_values=['a', 'x'],
                                         budget=4096)
        assert result.conflicts == []
        assert '<br>' in result.html
        assert '</br>' not in result.html


class TestEvidenceAPI:
    def test_query_evidence_accepts_fragment_and_string(self):
        result = build_evidence_fragment(PAGE, ['p.footnote:first'],
                                         allowed_values=ALLOWED, budget=4096)
        assert len(query_evidence(result, 'p.footnote:first')) == 1
        assert len(query_evidence(result.html, 'p.footnote:first')) == 1

    def test_real_nodes_filters_placeholders(self):
        result = build_evidence_fragment(
            '<ul><li>a</li><li>b</li></ul>',
            ['li:last'], allowed_values=['a', 'b'], budget=4096)
        nodes = real_nodes(pq(result.html)('*'), result.marker_attr,
                           result.placeholder_tag)
        assert all(not is_placeholder(element, result.marker_attr,
                                      result.placeholder_tag)
                   for element in nodes)

    def test_accepts_pyquery_and_element_sources(self):
        from pyquery.evidence import build_evidence_fragment as build
        document = pq('<div><p>a</p><p>b</p></div>')
        for source in (document, document[0], document):
            result = build(source, ['p:last'], allowed_values=['a', 'b'],
                           budget=4096)
            assert result.conflicts == []
            assert len(result.evaluate('p:last')) == 1

    def test_to_dict_roundtrip(self):
        result = build_evidence_fragment(PAGE, QUERIES,
                                         allowed_values=ALLOWED, budget=4096)
        data = result.to_dict()
        assert json.loads(json.dumps(data))['html'] == result.html
        assert data['conflicts'] == []
        assert data['queries'] == QUERIES
        assert all(hit['tag'] for hits in data['query_hits'].values()
                   for hit in hits)

    def test_evaluate_all(self):
        result = build_evidence_fragment(PAGE, ['p:first'],
                                         allowed_values=ALLOWED, budget=4096)
        assert set(result.evaluate_all()) == {'p:first'}

    def test_string_protocols(self):
        result = build_evidence_fragment('<div><p>a</p></div>', ['p'],
                                         allowed_values=['a'], budget=4096)
        assert str(result) == result.html
        assert result.__html__() == result.html
        assert result.fragment == result.html
        assert result.snippet == result.html

    def test_duplicate_queries_are_deduplicated(self):
        result = build_evidence_fragment(PAGE, ['p:first', 'p:first'],
                                         allowed_values=ALLOWED, budget=4096)
        assert result.queries == ('p:first',)


class TestEvidenceCLI:
    def test_cli_json_output(self, tmp_path, capsys):
        from pyquery.evidence import main
        request = tmp_path / 'request.json'
        request.write_text(json.dumps({
            'html': '<div><p>keep</p><p>secret</p></div>',
            'queries': ['p:last'],
            'allowed_values': ['keep'],
            'budget': 4096,
        }), encoding='utf-8')
        assert main(['-i', str(request)]) == 0
        data = json.loads(capsys.readouterr().out)
        assert 'secret' not in data['html']
        assert data['conflicts'] == []

    def test_cli_exit_code_on_conflict(self, tmp_path, capsys):
        from pyquery.evidence import main
        request = tmp_path / 'request.json'
        request.write_text(json.dumps({
            'html': '<div><p>alpha</p><p>beta</p></div>',
            'queries': ['p:contains("beta")'],
            'allowed_values': ['alpha'],
        }), encoding='utf-8')
        assert main(['-i', str(request)]) == 1
        data = json.loads(capsys.readouterr().out)
        assert data['conflicts'] == ['p:contains("beta")']

    def test_cli_compact(self, tmp_path, capsys):
        from pyquery.evidence import main
        request = tmp_path / 'request.json'
        request.write_text(json.dumps({
            'html': '<div><p>keep</p></div>',
            'queries': ['p'],
            'allowed_values': ['keep'],
        }), encoding='utf-8')
        assert main(['-i', str(request), '--compact']) == 0
        assert capsys.readouterr().out.strip() == '<div><p>keep</p></div>'


def test_conflicts_are_not_reported_as_reproduced():
    """The core safety property: no query is both reproduced and conflicted."""
    result = build_evidence_fragment(
        '<div><p class="a">one</p><p class="b">two</p></div>',
        ['p.a', 'p.b'],
        allowed_values=['one', 'two', 'a'],
        budget=4096,
    )
    assert set(result.reproduced_queries) & set(result.conflicts) == set()
    for query in result.conflicts:
        assert result.query_hits[query] == ()


def test_fragment_rechecks_against_original_page():
    """Every reproduced query must match the original page's hit sequence."""
    original = pq(PAGE)
    result = build_evidence_fragment(PAGE, QUERIES,
                                     allowed_values=ALLOWED, budget=4096)
    for query in result.reproduced_queries:
        assert signatures(original, query) == signatures(
            pq(result.html), query)
