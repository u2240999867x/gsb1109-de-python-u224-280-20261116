import pytest

from lxml import etree

from pyquery import PyQuery as pq
from pyquery.projection import (
    Projection,
    build_projection,
    normalize_assignments,
    normalize_components,
    project,
)


CARD = """
<div class="card" data-component="card">
  <span slot="title" id="title">Title</span>
  <span slot="body" id="body">Body</span>
</div>
"""

CARD_ROOT = """
<article class="card-view">
  <h2><slot name="title">Untitled</slot></h2>
  <div class="content"><slot name="body">Empty body</slot></div>
</article>
"""


def make_card(html=CARD):
    return pq(html)


def project_card(html=CARD, components=None, assignments=None, doc=None,
                 **kwargs):
    if doc is None:
        doc = pq(html)
    return doc.project(
        components=components or {'card': CARD_ROOT},
        assignments=assignments or {
            'card': {'title': ['#title'], 'body': ['#body']},
        },
        **kwargs)


class TestOriginalAndProjectedViews:
    def test_original_tree_is_not_mutated(self):
        doc = make_card()
        before = str(doc)
        projection = project_card(doc=doc)
        assert str(doc) == before
        assert str(projection.original()) == before
        assert '<article' not in str(projection.original())

    def test_projected_tree_contains_component_root(self):
        projection = project_card()
        projected = projection.projected()
        assert len(projected('article.card-view')) == 1
        assert projected('slot[name="title"] span#title').text() == 'Title'
        assert projection.projected('span#body').text() == 'Body'
        assert projection.original('article') == []

    def test_both_views_query_independently(self):
        projection = project_card()
        assert len(projection.original('#title')) == 1
        assert len(projection.projected('#title')) == 1
        projection.original('#title').text('Changed')
        assert projection.original('#title').text() == 'Changed'
        assert projection.projected('#title').text() == 'Title'

    def test_projection_is_truthy_and_reprs(self):
        projection = project_card()
        assert projection
        assert projection.original_root is not None
        assert projection.projected_root is not None
        assert 'complete=True' in repr(projection)

    def test_flat_function_api(self):
        doc = pq(CARD)
        projection = build_projection(
            doc, component_roots={'card': CARD_ROOT},
            slot_assignments={'card': {'title': ['#title']}})
        assert isinstance(projection, Projection)
        assert isinstance(
            project(CARD, {'card': CARD_ROOT},
                    {'card': {'title': ['#title']}}), Projection)


class TestCorrespondence:
    def test_original_nodes_map_to_projected(self):
        projection = project_card()
        original_title = projection.original('#title')[0]
        projected_title = projection.projected_of(original_title)
        assert projected_title is not None
        assert projected_title.get('id') == 'title'
        assert projection.original_of(projected_title) is original_title

    def test_component_only_nodes_have_no_original(self):
        projection = project_card()
        article = projection.projected('article')[0]
        assert projection.original_of(article) is None
        position = projection.position(article)
        assert position.original is None
        assert position.origin == 'component'

    def test_paths_are_relative_to_each_root(self):
        projection = project_card()
        title = projection.original('#title')[0]
        position = projection.position(title)
        assert position.original_path == 'span[1]'
        assert position.projected_path.endswith('h2/slot/span')

    def test_correspondence_maps_include_original_nodes(self):
        projection = project_card()
        original_title = projection.original('#title')[0]
        original_body = projection.original('#body')[0]
        mapping = projection.correspondence
        assert set(mapping) >= {original_title, original_body}
        reverse = projection.node_map['projected_to_original']
        assert reverse[mapping[original_title]] is original_title

    def test_position_rejects_unrelated_nodes(self):
        projection = project_card()
        with pytest.raises(KeyError):
            projection.position(pq('<div/>')[0])
        assert projection.original_of(pq('<div/>')[0]) is None

    def test_assigned_nodes_are_recorded(self):
        projection = project_card()
        assert len(projection.assigned) == 2
        assert projection.original_of(projection.assigned[0]) is not None


class TestSlots:
    def test_assigned_node_appears_once(self):
        projection = project_card()
        projected = projection.projected('#title')
        assert len(projected) == 1
        assert projected.text() == 'Title'

    def test_assignment_to_two_slots_places_first_only(self):
        doc = make_card()
        projection = doc.project(
            components={
                'card': '<div><slot name="a"></slot>'
                        '<slot name="b"></slot></div>',
            },
            assignments={
                'card': {'a': ['#title'], 'b': ['#title']},
            })
        assert projection.projected('#title').length == 1
        assert projection.projected('slot[name="a"] #title').length == 1
        assert projection.projected('slot[name="b"] #title').length == 0
        assert projection.projected('slot[name="b"]').html() in (
            '', None, '\n')
        assert projection.complete is False
        assert [c.reason for c in projection.conflicts] == [
            'duplicate-assignment']

    def test_empty_slot_uses_fallback(self):
        doc = make_card('<div data-component="card"></div>')
        projection = doc.project(
            components={'card': CARD_ROOT}, assignments={'card': {}})
        assert projection.projected('slot[name="title"]').text() == 'Untitled'
        assert projection.projected('slot[name="body"]').text() == \
            'Empty body'
        assert projection.complete is True

    def test_no_assignment_at_all_uses_fallback(self):
        doc = make_card('<div data-component="card"></div>')
        projection = doc.project(components={'card': CARD_ROOT})
        assert projection.projected('slot').length == 2
        assert projection.projected('slot[name="body"]').text() == \
            'Empty body'

    def test_slot_with_assigned_node_drops_fallback(self):
        projection = project_card()
        assert 'Untitled' not in projection.projected().text()
        assert 'Empty body' not in projection.projected().text()

    def test_explicit_empty_assignment_is_an_empty_slot(self):
        doc = make_card()
        projection = doc.project(
            components={'card': CARD_ROOT},
            assignments={'card': {'title': []}})
        # No node is assigned, so the slot is empty and shows fallback.
        assert projection.projected('slot[name="title"]').text() == \
            'Untitled'
        assert projection.projected('slot[name="body"]').text() == \
            'Empty body'

    def test_unnamed_slot_is_the_default_slot(self):
        doc = pq('<div id="app"><em>Default</em></div>')
        projection = doc.project(
            components={'app': '<div><slot>fallback</slot></div>'},
            assignments={'app': {'': ['#app em']}})
        assert projection.complete is True
        assert projection.projected('slot em').text() == 'Default'

    def test_repeated_host_does_not_duplicate_assigned_node(self):
        doc = pq(
            '<div>'
            '<section data-component="card"><i id="n">N</i></section>'
            '<section data-component="card"></section>'
            '</div>')
        projection = doc.project(
            components={'card': '<div><slot name="t">fb</slot></div>'},
            assignments={'card': {'t': ['#n']}})
        assert projection.projected('#n').length == 1
        assert projection.projected('slot[name="t"]').length == 2
        assert projection.complete is True


class TestUnreadableRoots:
    @pytest.mark.parametrize('root', [
        None,
        {'readable': False},
        object(),
        {'readable': True},
    ])
    def test_unreadable_root_is_not_empty(self, root):
        doc = pq('<div data-component="card"><b>kept</b></div>')
        projection = doc.project(components={'card': root})
        assert projection.complete is False
        assert 'card' in projection.unreadable
        # The host is preserved, not replaced by an empty component.
        assert projection.projected('b').text() == 'kept'
        assert projection.projected('slot').length == 0
        assert projection.unresolved

    def test_unreadable_root_does_not_use_fallback(self):
        doc = pq('<div data-component="card"></div>')
        projection = doc.project(
            components={'card': {'readable': False, 'root': CARD_ROOT}})
        assert projection.projected('article').length == 0
        assert 'Untitled' not in projection.projected().text()
        assert projection.complete is False

    def test_empty_root_is_distinct_from_unreadable(self):
        doc = pq('<div data-component="card"></div>')
        projection = doc.project(components={'card': ''})
        assert projection.complete is True
        assert projection.unreadable == {}
        assert projection.projected('pq-empty-root').length == 1

    def test_component_root_can_be_an_element(self):
        doc = pq('<div data-component="card"></div>')
        root = pq('<section><slot></slot></section>')[0]
        projection = doc.project(components={'card': root})
        assert projection.complete is True
        assert projection.projected('section').length == 1


class TestMalformedTables:
    def test_missing_referenced_node_is_a_conflict(self):
        doc = pq('<div data-component="card"></div>')
        projection = doc.project(
            components={'card': CARD_ROOT},
            assignments={'card': {'title': ['#does-not-exist']}})
        assert projection.complete is False
        assert projection.projected('slot[name="title"]').text() == ''
        assert 'Untitled' not in projection.projected().text()
        assert [c.reason for c in projection.conflicts] == ['missing-node']
        assert projection.unresolved

    def test_missing_node_does_not_drop_the_rest(self):
        doc = make_card()
        projection = doc.project(
            components={'card': CARD_ROOT},
            assignments={'card': {
                'title': ['#title', '#missing'],
                'body': ['#body'],
            }})
        assert projection.projected('#title').text() == 'Title'
        assert projection.projected('#body').text() == 'Body'
        assert [c.reason for c in projection.conflicts] == ['missing-node']
        assert projection.complete is False

    def test_assignment_to_unknown_component_is_reported(self):
        doc = make_card()
        projection = doc.project(
            components={'card': CARD_ROOT},
            assignments={'card': {'title': ['#title']},
                         'ghost': {'title': ['#body']}})
        assert projection.projected('#title').text() == 'Title'
        assert any(issue.kind == 'missing-component'
                   for issue in projection.issues)
        assert projection.complete is False

    def test_assignment_to_missing_slot_is_reported(self):
        doc = make_card()
        projection = doc.project(
            components={'card': CARD_ROOT},
            assignments={'card': {'nope': ['#title']}})
        assert any(issue.kind == 'missing-slot'
                   for issue in projection.issues)
        assert [c.reason for c in projection.conflicts] == ['missing-slot']
        assert projection.complete is False

    def test_invalid_assignment_entry_is_reported(self):
        doc = make_card()
        projection = doc.project(
            components={'card': CARD_ROOT},
            assignments=[42])
        assert any(issue.kind == 'invalid-assignment'
                   for issue in projection.issues)
        assert projection.complete is False

    def test_repeated_node_in_one_slot_is_reported_once(self):
        doc = pq('<div data-component="card"><b id="n">N</b></div>')
        projection = doc.project(
            components={'card': '<div><slot>F</slot></div>'},
            assignments={'card': {'': ['#n', '#n']}})
        assert projection.projected('#n').length == 1
        assert [c.reason for c in projection.conflicts] == [
            'duplicate-assignment']
        assert projection.complete is False

    def test_partial_results_are_never_complete(self):
        doc = make_card()
        projection = doc.project(
            components={'card': CARD_ROOT},
            assignments={'card': {'title': ['#missing']}})
        assert projection.partial is True
        assert projection.is_complete is False


class TestCycles:
    def test_self_referencing_component_is_reported(self):
        doc = pq('<div data-component="loop"></div>')
        projection = doc.project(
            components={'loop': '<div><div data-component="loop"/></div>'})
        assert projection.complete is False
        assert [c.reason for c in projection.conflicts] == ['cycle']
        assert any(issue.kind == 'cycle' for issue in projection.issues)
        # The usable outer structure is still projected.
        assert projection.projected('div').length >= 2

    def test_mutual_cycle_is_reported_once_per_host(self):
        doc = pq('<div data-component="a"></div>')
        projection = doc.project(components={
            'a': '<div><div data-component="b"></div></div>',
            'b': '<div><div data-component="a"></div></div>',
        })
        assert projection.complete is False
        assert any(issue.kind == 'cycle' for issue in projection.issues)
        assert projection.projected('div').length >= 3

    def test_self_host_assignment_is_a_cycle(self):
        doc = pq('<div id="app" data-component="app"><b>B</b></div>')
        projection = doc.project(
            components={'app': '<section><slot>F</slot></section>'},
            assignments={'app': {'': ['#app']}})
        assert projection.complete is False
        assert [c.reason for c in projection.conflicts] == ['cycle']
        # The node is not placed inside itself.
        assert projection.projected('section slot').html() in ('', None)

    def test_cycle_keeps_usable_structure(self):
        doc = pq('<div data-component="a"></div>')
        projection = doc.project(
            components={'a': '<div><slot></slot>'
                             '<div data-component="a"></div></div>'})
        # Content before the recursive host is still projected.
        assert projection.projected('slot').length == 1
        assert projection.projected(
            'div[data-component="a"]').length >= 1
        assert projection.complete is False


class TestNormalization:
    def test_component_table_shapes(self):
        roots, unreadable = normalize_components({
            'a': '<div></div>',
            'b': {'root': '<span></span>'},
            'c': {'markup': '<p></p>'},
            'd': {'html': '<b></b>'},
            'e': {'readable': False},
        })
        assert set(roots) == {'a', 'b', 'c', 'd'}
        assert set(unreadable) == {'e'}

    def test_component_list_shape(self):
        roots, unreadable = normalize_components([
            {'id': 'a', 'root': '<div></div>'},
            ('b', '<span></span>'),
        ])
        assert set(roots) == {'a', 'b'}
        assert unreadable == {}

    def test_assignment_dict_shape(self):
        records = normalize_assignments({'a': {'x': ['#n']}})
        assert records == [
            {'component': 'a', 'host': None, 'slot': 'x',
             'nodes': ['#n']},
        ]

    def test_assignment_list_shape(self):
        records = normalize_assignments([
            {'component': 'a', 'slot': 'x', 'nodes': ['#n']},
        ])
        assert records[0]['component'] == 'a'
        assert records[0]['slot'] == 'x'

    def test_assignment_host_reference(self):
        records = normalize_assignments([
            {'host': '.card', 'slot': 'x', 'nodes': ['#n']},
        ])
        assert records[0]['host'] == '.card'

    def test_flat_three_tuple_shape(self):
        records = normalize_assignments([('a', 'x', ['#n'])])
        assert records[0]['component'] == 'a'
        assert records[0]['slot'] == 'x'
        assert records[0]['nodes'] == ['#n']


class TestAttachedViews:
    def test_attach_makes_both_views_queryable(self):
        doc = make_card()
        projection = doc.project(
            components={'card': CARD_ROOT},
            assignments={'card': {'title': ['#title'], 'body': ['#body']}},
            attach=True)
        assert doc.projected('#title').text() == 'Title'
        assert doc.query_projected('article').length == 1
        assert doc.original('#title').text() == 'Title'
        assert doc.query_original('article').length == 0
        assert projection.complete is True

    def test_unattached_projected_is_empty(self):
        doc = make_card()
        assert doc.projected() == []

    def test_attach_projection_method(self):
        doc = make_card()
        projection = project_card(doc=doc)
        doc.attach_projection(projection)
        assert doc.projected('#body').text() == 'Body'


class TestNestedComponents:
    def test_nested_component_is_expanded(self):
        doc = pq('<div data-component="outer"></div>')
        projection = doc.project(components={
            'outer': '<div><span data-component="inner"></span></div>',
            'inner': '<em>Inner</em>',
        })
        assert projection.complete is True
        assert projection.projected('em').text() == 'Inner'

    def test_assignment_to_nested_component_slot(self):
        doc = pq('<div data-component="outer"><i id="n">N</i></div>')
        projection = doc.project(
            components={
                'outer': '<div><span data-component="inner"></span></div>',
                'inner': '<em><slot name="s">F</slot></em>',
            },
            assignments={'inner': {'s': ['#n']}})
        assert projection.complete is True
        assert projection.projected('em #n').text() == 'N'
        assert '>F<' not in str(projection.projected())

    def test_nested_component_counts_as_hosted(self):
        doc = pq('<div data-component="outer"></div>')
        projection = doc.project(components={
            'outer': '<div><span data-component="inner"></span></div>',
            'inner': '<em><slot>F</slot></em>',
        })
        assert not [issue for issue in projection.issues
                    if issue.kind == 'missing-host']

    def test_declared_component_cycle_reports_cycle(self):
        doc = pq('<div data-component="a"><i id="keep">K</i></div>')
        projection = doc.project(components={
            'a': '<div><slot name="s"></slot>'
                 '<div data-component="a"></div></div>',
        })
        assert [c.reason for c in projection.conflicts] == ['cycle']
        assert projection.complete is False
        # The usable slot before the recursive host is still projected.
        assert projection.projected('slot').length == 1


class TestDocumentShapes:
    def test_markup_document(self):
        projection = project(
            CARD, {'card': CARD_ROOT},
            {'card': {'title': ['#title']}})
        assert projection.projected('#title').text() == 'Title'

    def test_element_document(self):
        doc = pq(CARD)
        projection = build_projection(
            doc[0], {'card': CARD_ROOT},
            {'card': {'title': ['#title']}})
        assert projection.projected('#title').text() == 'Title'

    def test_element_list_document(self):
        doc = pq(CARD)
        projection = build_projection(
            list(doc), {'card': CARD_ROOT},
            {'card': {'title': ['#title']}})
        assert projection.projected('#title').text() == 'Title'

    def test_xpath_node_reference(self):
        doc = pq(CARD)
        projection = doc.project(
            components={'card': CARD_ROOT},
            assignments={'card': {
                'title': ['xpath://span[@id="title"]'],
            }})
        assert projection.projected('#title').text() == 'Title'

    def test_dict_node_reference(self):
        doc = pq(CARD)
        projection = doc.project(
            components={'card': CARD_ROOT},
            assignments={'card': {
                'title': [{'id': 'title'}],
            }})
        assert projection.projected('#title').text() == 'Title'

    def test_element_node_reference(self):
        doc = pq(CARD)
        title = doc('#title')[0]
        projection = doc.project(
            components={'card': CARD_ROOT},
            assignments={'card': {'title': [title]}})
        assert projection.projected('#title').text() == 'Title'

    def test_original_selector_scopes_the_document(self):
        doc = pq('<div><section id="one" data-component="card"></section>'
                 '<section id="two" data-component="card"></section></div>')
        projection = build_projection(
            doc, {'card': '<div><slot></slot></div>'}, {},
            original_selector='#one')
        assert projection.original_root.get('id') == 'one'
        assert projection.original('section').length == 1


class TestCommentsAndText:
    def test_comments_in_component_root_are_preserved(self):
        doc = pq('<div data-component="card"></div>')
        projection = doc.project(
            components={
                'card': '<section><!-- note --><slot></slot></section>'})
        assert '<!-- note -->' in str(projection.projected())
        assert projection.complete is True

    def test_text_around_slot_is_preserved(self):
        doc = pq('<div data-component="card"><b>B</b></div>')
        projection = doc.project(
            components={'card': '<section>before <slot></slot> after'
                                 '</section>'},
            assignments={'card': {
                '': ['div[data-component="card"] b'],
            }})
        text = projection.projected('section').text()
        assert text.split() == ['before', 'B', 'after']

    def test_frozen_projection_does_not_alias_original_nodes(self):
        doc = make_card()
        projection = project_card(doc=doc)
        projection.original('#title').attr('data-x', '1')
        assert projection.projected('#title').attr('data-x') is None


@pytest.mark.parametrize('tag', ['div', 'span'])
def test_tables_are_read_only(tag):
    doc = pq(f'<{tag} data-component="card"><b>N</b></{tag}>')
    original = etree.tostring(doc[0])
    doc.project(
        components={'card': '<section><slot></slot></section>'},
        assignments={'card': {'': [f'#{tag} b']}})
    assert etree.tostring(doc[0]) == original
