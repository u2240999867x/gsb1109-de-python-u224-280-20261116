"""Static slot projection for RPA supplied DOM snapshots.

RPA payloads contain a static DOM plus a table of component roots (the
markup a component contributes) and a slot assignment table (which
light-DOM nodes belong to which ``<slot>``).  A browser composes these
trees while rendering; this module performs the same composition purely
from the supplied data, without starting a browser.

Two views stay distinct:

``original``
    The DOM exactly as supplied.  Component roots are *not* merged into it.

``projected``
    The display tree: every host is replaced by its component root, every
    ``<slot>`` is filled with its assigned nodes, and an unfilled slot
    shows its fallback content.

Each projected node maps back to the original node it came from (component
root nodes have no original counterpart).  Composition is deliberately
partial: a missing reference, a duplicate assignment, a cycle or an
unreadable component root is recorded as a conflict/issue while the rest
of the tree is still produced.  :attr:`Projection.complete` is ``False``
whenever anything could not be confirmed, so a partial structure is never
presented as a complete page.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import copy, deepcopy

from lxml import etree, html

__all__ = [
    'Projection',
    'ProjectionError',
    'ProjectionIssue',
    'SlotConflict',
    'NodePosition',
    'ComponentRootError',
    'normalize_components',
    'normalize_assignments',
    'build_projection',
    'project',
]


class ProjectionError(ValueError):
    """Base error for invalid projection input."""


class ComponentRootError(ProjectionError):
    """A component root was supplied but cannot be read as markup."""


class ProjectionIssue:
    """One problem found while composing the projection.

    ``kind`` is one of ``'missing-component'``, ``'missing-node'``,
    ``'missing-slot'``, ``'duplicate-assignment'``, ``'cycle'`` or
    ``'unreadable-component'``.
    """

    __slots__ = ('kind', 'message', 'component', 'slot', 'nodes')

    def __init__(self, kind, message, component=None, slot=None, nodes=()):
        self.kind = kind
        self.message = message
        self.component = component
        self.slot = slot
        self.nodes = tuple(nodes)

    def as_dict(self):
        return {
            'kind': self.kind,
            'message': self.message,
            'component': self.component,
            'slot': self.slot,
            'nodes': list(self.nodes),
        }

    def __repr__(self):
        return (f'ProjectionIssue({self.kind!r}, {self.message!r}, '
                f'component={self.component!r}, slot={self.slot!r})')

    def __eq__(self, other):
        if not isinstance(other, ProjectionIssue):
            return NotImplemented
        return self.as_dict() == other.as_dict()


class SlotConflict:
    """An assignment that could not be honoured as supplied."""

    __slots__ = ('reason', 'component', 'slot', 'nodes', 'detail')

    def __init__(self, reason, component=None, slot=None, nodes=(),
                 detail=None):
        self.reason = reason
        self.component = component
        self.slot = slot
        self.nodes = tuple(nodes)
        self.detail = detail

    def as_dict(self):
        return {
            'reason': self.reason,
            'component': self.component,
            'slot': self.slot,
            'nodes': list(self.nodes),
            'detail': self.detail,
        }

    def __repr__(self):
        return (f'SlotConflict({self.reason!r}, component={self.component!r}, '
                f'slot={self.slot!r}, nodes={self.nodes!r})')

    def __eq__(self, other):
        if not isinstance(other, SlotConflict):
            return NotImplemented
        return self.as_dict() == other.as_dict()


class NodePosition:
    """A node's position in the original tree and/or the projection.

    ``original`` / ``projected`` are the node objects, ``None`` when the
    node exists in only one view.  The ``*_path`` values are structural
    paths relative to each view's root.
    """

    __slots__ = ('original', 'projected', 'original_path', 'projected_path',
                 'origin', 'slot')

    def __init__(self, original=None, projected=None, original_path=None,
                 projected_path=None, origin='original', slot=None):
        self.original = original
        self.projected = projected
        self.original_path = original_path
        self.projected_path = projected_path
        self.origin = origin
        self.slot = slot

    def as_dict(self):
        return {
            'original': self.original,
            'projected': self.projected,
            'original_path': self.original_path,
            'projected_path': self.projected_path,
            'origin': self.origin,
            'slot': self.slot,
        }

    def __repr__(self):
        return (f'<NodePosition {self.original_path!r} -> '
                f'{self.projected_path!r} origin={self.origin!r}'
                + (f' slot={self.slot!r}' if self.slot is not None else '')
                + '>')


class Projection:
    """Original tree, display projection, and their correspondence."""

    def __init__(self, original_view, projected_view, positions, issues,
                 conflicts, unresolved, unreadable, assigned, slots,
                 component_roots):
        self._original_view = original_view
        self._projected_view = projected_view
        self._positions = list(positions)
        self._issues = list(issues)
        self._conflicts = list(conflicts)
        self._unresolved = list(unresolved)
        self._unreadable = OrderedDict(unreadable)
        self._assigned = list(assigned)
        self._slots = {key: list(value) for key, value in slots.items()}
        self._component_roots = OrderedDict(component_roots)
        self._by_original = {}
        self._by_projected = {}
        for position in self._positions:
            if position.original is not None:
                self._by_original[id(position.original)] = position
            if position.projected is not None:
                self._by_projected[id(position.projected)] = position
        self._complete = not (
            self._issues or self._conflicts or
            self._unresolved or self._unreadable
        )

    # -- views ---------------------------------------------------------
    def original(self, selector=None):
        """Query the original (unprojected) tree."""
        if selector is None:
            return self._original_view
        return self._original_view(selector)

    def projected(self, selector=None):
        """Query the display (projected) tree."""
        if selector is None:
            return self._projected_view
        return self._projected_view(selector)

    query_original = original
    query_projected = projected

    def source(self, selector=None):
        """Alias for :meth:`original`."""
        return self.original(selector)

    def display(self, selector=None):
        """Alias for :meth:`projected`."""
        return self.projected(selector)

    def rendered(self, selector=None):
        """Alias for :meth:`projected`."""
        return self.projected(selector)

    def __call__(self, selector=None):
        """Query the projected view (the display tree)."""
        return self.projected(selector)

    def __iter__(self):
        return iter(self._projected_view)

    def __len__(self):
        return len(self._projected_view)

    def __getitem__(self, index):
        return self._projected_view[index]

    def __contains__(self, node):
        try:
            self.position(node)
        except (KeyError, TypeError):
            return False
        return True

    @property
    def original_view(self):
        return self._original_view

    @property
    def projected_view(self):
        return self._projected_view

    source_view = original_view
    display_view = projected_view

    @property
    def original_root(self):
        try:
            return self._original_view[0]
        except IndexError:
            return None

    @property
    def projected_root(self):
        try:
            return self._projected_view[0]
        except IndexError:
            return None

    # -- diagnostics ---------------------------------------------------
    @property
    def complete(self):
        """``True`` only when every supplied structure was confirmed."""
        return self._complete

    @property
    def is_complete(self):
        """Alias for :attr:`complete`."""
        return self._complete

    @property
    def partial(self):
        """``True`` when anything could not be confirmed."""
        return not self._complete

    @property
    def issues(self):
        return list(self._issues)

    @property
    def conflicts(self):
        return list(self._conflicts)

    @property
    def unresolved(self):
        return list(self._unresolved)

    @property
    def unreadable(self):
        """Component ids whose root could not be read, with the reason."""
        return dict(self._unreadable)

    @property
    def assigned(self):
        """Nodes placed in a slot, in projected order."""
        return list(self._assigned)

    @property
    def slots(self):
        """``(component, slot name)`` -> projected ``<slot>`` nodes."""
        return {key: list(value) for key, value in self._slots.items()}

    @property
    def component_roots(self):
        return OrderedDict(self._component_roots)

    @property
    def positions(self):
        return list(self._positions)

    @property
    def correspondence(self):
        """Original node -> projected node.

        Every original node is listed; nodes that were not placed in the
        projection map to ``None``.  Component-only nodes are not included
        because they have no original counterpart.
        """
        return OrderedDict(
            (position.original, position.projected)
            for position in self._positions
            if position.original is not None
        )

    @property
    def node_map(self):
        """Correspondence in both directions, without synthetic nodes."""
        original_to_projected = self.correspondence
        return {
            'original_to_projected': original_to_projected,
            'projected_to_original': OrderedDict(
                (projected, original)
                for original, projected in original_to_projected.items()
                if projected is not None
            ),
        }

    def position(self, node):
        """Locate ``node`` (from either view) and return its mapping."""
        if node is None or isinstance(node, str):
            raise KeyError(node)
        try:
            return self._by_original[id(node)]
        except (KeyError, TypeError):
            pass
        try:
            return self._by_projected[id(node)]
        except (KeyError, TypeError):
            raise KeyError(node) from None

    def original_of(self, node):
        """Projected node -> original node (``None`` for synthetic nodes)."""
        try:
            return self.position(node).original
        except KeyError:
            return None

    def projected_of(self, node):
        """Original node -> projected node (``None`` if not placed)."""
        try:
            return self.position(node).projected
        except KeyError:
            return None

    def original_path_of(self, node):
        """Structural path of ``node`` in the original view."""
        try:
            return self.position(node).original_path
        except KeyError:
            return None

    def projected_path_of(self, node):
        """Structural path of ``node`` in the projected view."""
        try:
            return self.position(node).projected_path
        except KeyError:
            return None

    def corresponding(self, node):
        """Return the same node in the other view, or ``None``."""
        position = self.position(node)
        known = self.node_map
        if node in known['original_to_projected']:
            return position.projected
        return position.original

    @property
    def path_map(self):
        """Original structural path -> projected structural path."""
        return OrderedDict(
            (position.original_path, position.projected_path)
            for position in self._positions
            if position.original_path is not None
        )

    def __repr__(self):
        return (
            f'<Projection complete={self._complete} '
            f'issues={len(self._issues)} conflicts={len(self._conflicts)} '
            f'nodes={len(self._positions)}>'
        )

    def __bool__(self):
        return bool(self._projected_view)


# ---------------------------------------------------------------------------
# Input normalisation
# ---------------------------------------------------------------------------

def _local_name(tag):
    if not isinstance(tag, str) or callable(tag):
        return None
    if tag.startswith('{'):
        return tag.split('}', 1)[1]
    return tag


def _is_element(value):
    return isinstance(value, etree._Element)


def _is_slot(node):
    return _local_name(node.tag) == 'slot'


def _to_text(fragment):
    if isinstance(fragment, bytes):
        try:
            return fragment.decode('utf-8')
        except UnicodeDecodeError as exc:
            raise ComponentRootError(
                f'root is not valid UTF-8: {exc}') from exc
    return fragment


def _element_from_fragment(fragment, component):
    """Parse a component root into a detached element.

    Blank strings are explicitly empty roots.  A missing (``None``) value
    or anything that cannot be parsed raises :class:`ComponentRootError`
    so an unreadable root is never mistaken for an empty one.
    """
    if fragment is None:
        raise ComponentRootError('no root supplied')
    if _is_element(fragment):
        return deepcopy(fragment)
    if hasattr(fragment, 'read'):
        try:
            fragment.seek(0)
        except (AttributeError, OSError):
            pass
        try:
            fragment = fragment.read()
        except Exception as exc:  # NOQA
            raise ComponentRootError(
                f'cannot read root stream: {exc}') from exc
    fragment = _to_text(fragment)
    if not isinstance(fragment, str):
        raise ComponentRootError(
            f'unreadable root of type {type(fragment).__name__}')
    if not fragment.strip():
        return None
    try:
        wrapper = html.fragment_fromstring(
            fragment, create_parent='pq-root')
    except (etree.ParserError, etree.XMLSyntaxError, ValueError) as exc:
        raise ComponentRootError(f'cannot parse root markup: {exc}') from exc
    children = wrapper.getchildren()
    if len(children) == 1 and wrapper.text is None:
        return children[0]
    root = wrapper.makeelement('pq-component-root', {}, nsmap=wrapper.nsmap)
    root.text = wrapper.text
    for child in children:
        root.append(child)
    return root


def normalize_components(components):
    """Normalize the component table to ``(roots, unreadable)``.

    Accepted shapes::

        {'card': '<div>...</div>'}
        {'card': {'root|markup|html|content': '<div>...</div>'}}
        [{'id': 'card', 'root': '<div>...</div>'}]

    ``roots`` maps component ids to their parsed root element.  Entries
    whose root is explicitly unreadable (``readable: False``, an error
    string, an exception object) are returned in ``unreadable`` with the
    reason instead of being guessed as empty.
    """
    roots = OrderedDict()
    unreadable = OrderedDict()
    if components is None:
        return roots, unreadable

    if isinstance(components, dict):
        entries = list(components.items())
    elif isinstance(components, (list, tuple)):
        entries = []
        for item in components:
            if isinstance(item, dict):
                cid = item.get('id', item.get('component', item.get('name')))
                entries.append((cid, item))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                entries.append((item[0], item[1]))
            else:
                raise ProjectionError(f'invalid component entry: {item!r}')
    else:
        raise ProjectionError(f'invalid component table: {components!r}')

    for cid, value in entries:
        if not isinstance(cid, str):
            raise ProjectionError(
                f'component id must be a string: {cid!r}')
        root = value
        readable = None
        if isinstance(value, dict):
            for key in ('root', 'markup', 'html', 'content'):
                if key in value:
                    root = value[key]
                    break
            else:
                root = None
            readable = value.get('readable', value.get('read'))
            if readable is None and value.get('error') is not None:
                readable = False
            if readable is None and value.get('unreadable') is not None:
                readable = False
        if isinstance(root, BaseException):
            unreadable[cid] = str(root) or type(root).__name__
            continue
        if readable is False:
            unreadable[cid] = str(root) if root else 'marked unreadable'
            continue
        if readable is True and root is None:
            unreadable[cid] = 'marked readable but no root supplied'
            continue
        try:
            element = _element_from_fragment(root, cid)
        except ComponentRootError as exc:
            unreadable[cid] = str(exc)
            continue
        if element is None:
            element = html.Element('pq-empty-root', component=cid)
        roots[cid] = element
    return roots, unreadable


def normalize_assignments(assignments):
    """Normalize the slot assignment table to a flat list of records.

    Accepted shapes::

        [{'component': 'card', 'slot': 'title', 'nodes': ['#a']}]
        {'card': {'title': ['#a', '#b']}}
        {'card': [{'slot': 'title', 'nodes': ['#a']}]}
        {'card': {'slot': 'title', 'nodes': ['#a']}}

    Each record has ``component``, ``slot`` and ``nodes``.  Unreadable
    entries are kept (with ``invalid: True``) so they can be reported
    instead of silently ignored.
    """
    records = []

    def add(component, slot, nodes, invalid=False, host=None):
        if nodes is None:
            nodes = []
        elif not isinstance(nodes, (list, tuple)):
            nodes = [nodes]
        record = {
            'component': component,
            'host': host,
            'slot': slot,
            'nodes': list(nodes),
        }
        if invalid:
            record['invalid'] = True
        records.append(record)

    if assignments is None:
        return records
    if isinstance(assignments, dict):
        for component, slots in assignments.items():
            if isinstance(slots, dict):
                if 'nodes' in slots or 'assigned' in slots:
                    add(component, slots.get('slot', slots.get('name')),
                        slots.get('nodes', slots.get('assigned')),
                        host=slots.get('host'))
                else:
                    for slot, nodes in slots.items():
                        add(component, slot, nodes)
            elif isinstance(slots, (list, tuple)):
                for entry in slots:
                    if isinstance(entry, dict):
                        add(entry.get('component', component),
                            entry.get('slot', entry.get('name')),
                            entry.get('nodes', entry.get('assigned', [])),
                            host=entry.get('host'))
                    else:
                        add(component, None, [entry])
            else:
                add(component, None, [slots])
        return records
    if isinstance(assignments, (list, tuple)):
        for entry in assignments:
            if isinstance(entry, dict):
                add(entry.get('component', entry.get('host')),
                    entry.get('slot', entry.get('name')),
                    entry.get('nodes', entry.get('assigned', [])),
                    host=entry.get('host'))
            elif isinstance(entry, (list, tuple)) and len(entry) == 3:
                add(entry[0], entry[1], entry[2])
            else:
                add(None, None, [entry], invalid=True)
        return records
    raise ProjectionError(f'invalid slot assignment table: {assignments!r}')


def _document_nodes(document):
    if document is None:
        return []
    if isinstance(document, (str, bytes)):
        from .pyquery import PyQuery
        return list(PyQuery(document))
    if _is_element(document):
        return [document]
    if isinstance(document, (list, tuple)):
        return [node for node in document if _is_element(node)]
    if hasattr(document, 'getroot'):
        root = document.getroot()
        return [] if root is None else [root]
    if hasattr(document, '__iter__'):
        return [node for node in document if _is_element(node)]
    raise ProjectionError(f'invalid document: {document!r}')


def _view(nodes):
    from .pyquery import PyQuery
    return PyQuery(list(nodes))


def _iter_elements(nodes):
    for node in nodes:
        if not _is_element(node):
            continue
        yield node
        yield from _iter_elements(node.getchildren())


def _relative_path(root, node):
    if node is None:
        return None
    try:
        full = node.getroottree().getpath(node)
    except (AttributeError, ValueError):
        return None
    if root is node:
        return '.'
    if root is None:
        return full
    try:
        root_path = root.getroottree().getpath(root)
    except (AttributeError, ValueError):
        return full
    prefix = root_path + '/'
    if full.startswith(prefix):
        return full[len(prefix):]
    return full


def _clone_shallow(node):
    """Copy the element itself (attributes/text) without its children."""
    if callable(node.tag):
        return deepcopy(node)
    clone = copy(node)
    clone[:] = []
    clone.text = None
    return clone


def _element_hints(components_table, assignments):
    """Map component ids to explicit host selectors/id hints."""
    hints = {}
    if isinstance(components_table, dict):
        entries = components_table.items()
    elif isinstance(components_table, (list, tuple)):
        entries = []
        for item in components_table:
            if isinstance(item, dict):
                cid = item.get('id', item.get('component',
                                             item.get('name')))
                entries.append((cid, item))
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                entries.append((item[0], item[1]))
    else:
        entries = []
    for cid, value in entries:
        if not isinstance(cid, str) or not isinstance(value, dict):
            continue
        for key in ('host', 'hosts', 'host_selector', 'selector', 'for_',
                    'host_id', 'host_ref'):
            if not value.get(key):
                continue
            selectors = value[key]
            if isinstance(selectors, str):
                selectors = [selectors]
            hints.setdefault(cid, []).extend(selectors)
    for record in assignments:
        component = record.get('component')
        if not isinstance(component, str):
            continue
        for key in ('host', 'host_selector', 'selector', 'host_id',
                    'host_ref'):
            value = record.get(key)
            if isinstance(value, str):
                hints.setdefault(component, []).append(value)
    return hints


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------

class _Resolver:
    """Resolve node references against the original document."""

    def __init__(self, document_nodes):
        self.document_nodes = document_nodes
        self.view = _view(document_nodes)
        self.elements = list(_iter_elements(document_nodes))

    def resolve(self, ref):
        """Return the original elements named by ``ref``.

        References are CSS selectors, ``xpath:`` expressions, element
        objects, ``{'id'|'path'|'selector'|'node': ...}`` mappings, or plain
        ids/names as used by some RPA payloads.
        """
        if ref is None:
            return []
        if _is_element(ref):
            return [ref]
        if isinstance(ref, (list, tuple)):
            found = []
            for item in ref:
                for element in self.resolve(item):
                    if all(element is not known for known in found):
                        found.append(element)
            return found
        if isinstance(ref, dict):
            for key in ('node', 'element'):
                if key in ref:
                    return self.resolve(ref[key])
            if 'selector' in ref:
                return self.resolve(ref['selector'])
            if 'path' in ref:
                return self.resolve('xpath:' + str(ref['path']))
            if 'xpath' in ref:
                return self.resolve('xpath:' + str(ref['xpath']))
            if 'id' in ref:
                value = str(ref['id'])
                if value.startswith(('#', '.', '[')):
                    return self.resolve(value)
                return self.resolve('#' + value)
            if 'index' in ref:
                try:
                    index = int(ref['index'])
                    return [self.elements[index]]
                except (ValueError, IndexError, TypeError):
                    return []
            return []
        if not isinstance(ref, str):
            return []
        value = ref.strip()
        if not value:
            return []
        if value.startswith('xpath:'):
            path = value[len('xpath:'):]
            found = []
            for root in self.document_nodes:
                try:
                    found.extend(root.xpath(path))
                except Exception:  # NOQA
                    continue
            return [node for node in found if _is_element(node)]
        try:
            found = list(self.view(value))
        except Exception:  # NOQA
            found = []
        if found:
            unique = []
            for element in found:
                if all(element is not known for known in unique):
                    unique.append(element)
            return unique
        if value.startswith('#'):
            found = [
                node for node in self.elements
                if node.get('id') == value[1:]
            ]
            if found:
                return found
        found = [
            node for node in self.elements
            if node.get('id') == value or node.get('name') == value
        ]
        return found


COMPONENT_ATTRS = (
    'data-component', 'data-component-id', 'component', 'component-id',
    'is', 'data-is',
)


class _HostTable:
    """Find the original elements that instantiate a component."""

    def __init__(self, document_nodes, roots, table, assignments,
                 unreadable=()):
        self.resolver = _Resolver(document_nodes)
        self.elements = self.resolver.elements
        self.roots = roots
        self.component_ids = set(roots) | set(unreadable)
        self._lower_ids = {
            cid.lower(): cid for cid in self.component_ids
        }
        self.hints = _element_hints(table, assignments)
        self.hosts = OrderedDict()
        self.appears = set()
        self.aliases = {}
        self._build()
        self._scan_nested_roots(roots)

    def identify(self, node):
        """Return the declared component id this node instantiates.

        Attributes that name an undeclared component are ignored: they are
        ordinary data attributes, not evidence of a broken projection.
        """
        for attr in COMPONENT_ATTRS:
            value = node.get(attr)
            if value in self.component_ids:
                return value
            if isinstance(value, str) and value.lower() in self._lower_ids:
                return self._lower_ids[value.lower()]
        tag = _local_name(node.tag)
        if tag in self.component_ids:
            return tag
        if isinstance(tag, str) and tag.lower() in self._lower_ids:
            return self._lower_ids[tag.lower()]
        return None

    def _scan_nested_roots(self, roots):
        """Record components instantiated inside other component roots.

        Such instances are not nodes of the original document, so they do
        not get entries in ``hosts``, but they still prove the component is
        present and can receive slot assignments.
        """
        for root in roots.values():
            for node in _iter_elements([root]):
                cid = self.identify(node)
                if cid is not None:
                    self.appears.add(cid)

    def _build(self):
        for cid in self.component_ids:
            found = []
            for selector in self.hints.get(cid, []):
                found.extend(self.resolver.resolve(selector))
            if not found:
                found = [
                    node for node in self.elements
                    if self.identify(node) == cid
                ]
            if not found:
                found = [
                    node for node in self.elements if node.get('id') == cid
                ][:1]
            unique = []
            seen = set()
            for node in found:
                if id(node) in seen:
                    continue
                seen.add(id(node))
                unique.append(node)
            self.hosts[cid] = unique
            if unique:
                self.appears.add(cid)
            for node in unique:
                self.aliases[id(node)] = cid
                if node.get('id'):
                    self.aliases['#' + node.get('id')] = cid
                try:
                    self.aliases[node.getroottree().getpath(node)] = cid
                except (AttributeError, ValueError):
                    pass
            for selector in self.hints.get(cid, []):
                self.aliases.setdefault(selector, cid)


def _slot_names(hosts, root):
    """Names of the slots the component root itself owns.

    Slots inside a nested component host belong to that nested component,
    so those subtrees are skipped.
    """
    names = set()

    def walk(node):
        for child in node.getchildren():
            if callable(child.tag):
                continue
            if _is_slot(child):
                name = child.get('name')
                names.add('' if name is None else str(name))
                continue
            if hosts.identify(child) is not None:
                continue
            walk(child)

    walk(root)
    return names


class _Plan:
    """Resolved assignments: what each slot shows, and what conflicts."""

    def __init__(self, document_nodes, hosts, assignments, roots, unreadable):
        self.issues = []
        self.conflicts = []
        self.unresolved = []
        self.assignments = OrderedDict()
        self.placements = {}
        self._resolver = _Resolver(document_nodes)
        self._hosts = hosts
        self._roots = roots
        self._unreadable = unreadable
        self.slot_names = {
            cid: _slot_names(hosts, root)
            for cid, root in roots.items()
        }
        self._build(assignments)

    def _issue(self, kind, message, component=None, slot=None, nodes=()):
        self.issues.append(
            ProjectionIssue(kind, message, component, slot, nodes))

    def _conflict(self, reason, component=None, slot=None, nodes=(),
                  detail=None):
        self.conflicts.append(
            SlotConflict(reason, component, slot, nodes, detail))

    def _unresolved(self, kind, message, component=None, slot=None, ref=None):
        self.unresolved.append({
            'kind': kind,
            'message': message,
            'component': component,
            'slot': slot,
            'ref': ref,
        })

    def _build(self, assignments):
        grouped = OrderedDict()
        for record in assignments:
            if record.get('invalid'):
                self._issue(
                    'invalid-assignment',
                    f'invalid assignment entry: {record["nodes"]!r}')
                self._unresolved(
                    'invalid-assignment', 'invalid assignment entry',
                    ref=record['nodes'])
                continue
            cid = _hosts_component_id(
                self._hosts, record.get('component'), record.get('host'))
            slot = record.get('slot')
            slot = '' if slot is None else str(slot)
            key = (cid, slot)
            entry = grouped.get(key)
            if entry is None:
                entry = grouped[key] = {
                    'component': cid,
                    'slot': slot,
                    'elements': [],
                    'missing': [],
                    'duplicates': [],
                    'declared': 0,
                }
            refs = record.get('nodes') or []
            entry['declared'] += len(refs)
            for ref in refs:
                elements = self._resolver.resolve(ref)
                if not elements:
                    entry['missing'].append(ref)
                    continue
                for element in elements:
                    if any(element is known
                           for known in entry['elements']):
                        if all(element is not known
                               for known in entry['duplicates']):
                            entry['duplicates'].append(element)
                    else:
                        entry['elements'].append(element)

        for (cid, slot), entry in grouped.items():
            known = cid in self._roots or cid in self._unreadable
            if not known:
                self._issue(
                    'missing-component',
                    f'assignment references unknown component {cid!r}',
                    component=cid, slot=slot)
                self._unresolved(
                    'missing-component',
                    f'unknown component {cid!r}', component=cid, slot=slot)
                continue
            if cid in self._unreadable:
                reason = self._unreadable[cid]
                self._issue(
                    'unreadable-component',
                    f'component {cid!r} root is unreadable: {reason}',
                    component=cid, slot=slot)
                self._unresolved(
                    'unreadable-component',
                    f'component {cid!r} root is unreadable',
                    component=cid, slot=slot, ref=reason)
                continue
            if cid not in self._hosts.appears:
                self._issue(
                    'missing-host',
                    f'component {cid!r} has no host in the original tree',
                    component=cid, slot=slot)
                self._unresolved(
                    'missing-host', f'component {cid!r} has no host',
                    component=cid, slot=slot)
            if slot not in self.slot_names.get(cid, set()):
                self._issue(
                    'missing-slot',
                    f'component {cid!r} has no slot named {slot!r}',
                    component=cid, slot=slot)
                self._conflict(
                    'missing-slot', component=cid, slot=slot,
                    detail=slot)
                self._unresolved(
                    'missing-slot',
                    f'component {cid!r} has no slot named {slot!r}',
                    component=cid, slot=slot)
                entry['missing_slot'] = True
            for ref in entry['missing']:
                self._issue(
                    'missing-node',
                    f'slot {slot!r} of component {cid!r} references a '
                    f'missing node: {ref!r}', component=cid, slot=slot,
                    nodes=(ref,))
                self._conflict(
                    'missing-node', component=cid, slot=slot, nodes=(ref,),
                    detail=ref)
                self._unresolved(
                    'missing-node',
                    f'slot {slot!r} references a missing node',
                    component=cid, slot=slot, ref=ref)

        # An assigned node contained by another assigned node would be
        # displayed twice.  Keep the outer assignment and report the inner
        # one as a duplicate, preserving every other slot.
        assigned_elements = []
        for entry in grouped.values():
            for element in entry['elements']:
                if all(element is not known for known in assigned_elements):
                    assigned_elements.append(element)
        assigned_ids = {id(element) for element in assigned_elements}
        contained = set()
        for element in assigned_elements:
            parent = element.getparent()
            while parent is not None:
                if id(parent) in assigned_ids:
                    contained.add(id(element))
                    self._issue(
                        'duplicate-assignment',
                        'node is assigned both directly and inside another '
                        'assigned node; the outer assignment is kept',
                        nodes=(element,))
                    self._conflict(
                        'duplicate-assignment', nodes=(element,),
                        detail='contained in another assigned node')
                    self._unresolved(
                        'duplicate-assignment',
                        'node is contained in another assigned node',
                        ref=element)
                    break
                parent = parent.getparent()
        if contained:
            for entry in grouped.values():
                entry['elements'] = [
                    element for element in entry['elements']
                    if id(element) not in contained
                ]
                entry['declared'] = len(entry['elements']) + len(
                    entry['missing'])

        # A node assigned more than once is placed at its first occurrence;
        # later occurrences are reported and skipped.
        for key, entry in grouped.items():
            placed = []
            # An assignment that cannot reach a slot or a host is not a
            # placement; do not let it consume nodes that a valid slot
            # could still display.
            if entry.get('missing_slot') or \
                    key[0] not in self._hosts.appears:
                entry['placed'] = placed
                entry['use_fallback'] = False
                self.assignments[key] = entry
                continue
            for element in entry['elements']:
                if self._is_placement_cycle(element, key[0]):
                    self._issue(
                        'cycle',
                        f'assigning this node to {key[0]!r}/{key[1]!r} '
                        'would place a component inside itself',
                        component=key[0], slot=key[1], nodes=(element,))
                    self._conflict(
                        'cycle', component=key[0], slot=key[1],
                        nodes=(element,), detail='assignment contains host')
                    self._unresolved(
                        'cycle',
                        'assignment would place a component inside itself',
                        component=key[0], slot=key[1], ref=element)
                    continue
                if id(element) in self.placements:
                    first = self.placements[id(element)]
                    self._issue(
                        'duplicate-assignment',
                        f'node assigned more than once; kept at its first '
                        f'placement {first[0]!r}/{first[1]!r}',
                        component=key[0], slot=key[1], nodes=(element,))
                    self._conflict(
                        'duplicate-assignment', component=key[0],
                        slot=key[1], nodes=(element,), detail=first)
                    self._unresolved(
                        'duplicate-assignment',
                        'node assigned more than once', component=key[0],
                        slot=key[1], ref=element)
                    continue
                self.placements[id(element)] = key
                placed.append(element)
            for element in entry['duplicates']:
                self._issue(
                    'duplicate-assignment',
                    f'node listed more than once for slot {key[1]!r} of '
                    f'component {key[0]!r}; it is displayed once',
                    component=key[0], slot=key[1], nodes=(element,))
                self._conflict(
                    'duplicate-assignment', component=key[0], slot=key[1],
                    nodes=(element,), detail='repeated in the same slot')
                self._unresolved(
                    'duplicate-assignment',
                    'node listed more than once for the same slot',
                    component=key[0], slot=key[1], ref=element)
            entry['placed'] = placed
            # A slot shows fallback content only when it is confirmed
            # empty: no resolved node, no unresolved reference, and no
            # assignment that was dropped as a duplicate.
            entry['use_fallback'] = (
                not placed and not entry['missing']
                and entry['declared'] == 0
            )
            if entry.get('missing_slot'):
                entry['use_fallback'] = False
            self.assignments[key] = entry


    def _is_placement_cycle(self, element, component):
        """True if ``element`` contains the host it is assigned to."""
        for host in self._hosts.hosts.get(component, []):
            node = host
            while node is not None:
                if node is element:
                    return True
                node = node.getparent()
        return False


def _hosts_component_id(hosts, component, host=None):
    if _is_element(component):
        return hosts.aliases.get(id(component))
    if isinstance(component, str):
        if component in hosts.component_ids:
            return component
        known = hosts.aliases.get(component)
        if known is not None:
            return known
    if isinstance(host, str):
        known = hosts.aliases.get(host)
        if known is not None:
            return known
    return component


class _Renderer:
    """Build the projected tree from the original tree and the plan."""

    def __init__(self, document_nodes, roots, hosts, plan, unreadable):
        self.document_nodes = document_nodes
        self.roots = roots
        self.hosts = hosts
        self.plan = plan
        self.unreadable = unreadable
        self.projected_roots = []
        self.by_original = {}
        self.by_projected = {}
        self.origin = {}
        self.slot_nodes = OrderedDict()
        self.assigned = []
        self.issues = list(plan.issues)
        self.conflicts = list(plan.conflicts)
        self.unresolved = list(plan.unresolved)
        self._slot_seen = set()
        self._stack = []

    # -- diagnostics ---------------------------------------------------
    def _issue(self, kind, message, component=None, slot=None, nodes=()):
        self.issues.append(
            ProjectionIssue(kind, message, component, slot, nodes))

    def _conflict(self, reason, component=None, slot=None, nodes=(),
                  detail=None):
        self.conflicts.append(
            SlotConflict(reason, component, slot, nodes, detail))

    def _unresolved(self, kind, message, component=None, slot=None, ref=None):
        self.unresolved.append({
            'kind': kind,
            'message': message,
            'component': component,
            'slot': slot,
            'ref': ref,
        })

    # -- helpers -------------------------------------------------------
    def _record(self, original, projected, origin='original', slot=None):
        if original is not None:
            self.by_original[id(original)] = projected
        if projected is not None:
            self.by_projected[id(projected)] = original
            self.origin[id(projected)] = (origin, slot)
        return projected

    def _copy_children(self, source, target, owner=None, origin='original'):
        target.text = source.text
        for child in source.getchildren():
            copied = self._copy(child, owner=owner, origin=origin)
            if copied is None:
                if getattr(child, 'tail', None):
                    _attach_tail(target, child.tail)
                continue
            target.append(copied)
            if child.tail:
                copied.tail = child.tail
        return target

    def _copy(self, node, owner=None, origin='original', forced=False):
        if not _is_element(node):
            return None
        if callable(node.tag):
            clone = deepcopy(node)
            self._record(
                node if origin == 'original' else None, clone, origin)
            return clone
        key = id(node)
        if not forced and key in self.plan.placements:
            # Assigned nodes are rendered once, inside their slot.
            return None
        if _is_slot(node) and owner is not None:
            return self._slot(node, owner)
        cid = self._component_of(node)
        if cid is not None:
            return self._host(node, cid)
        clone = _clone_shallow(node)
        slot = node.get('slot') if origin == 'original' else None
        self._record(node if origin == 'original' else None, clone, origin,
                     slot)
        self._copy_children(node, clone, owner, origin=origin)
        return clone

    def _component_of(self, node):
        cid = self.hosts.identify(node)
        if cid is None:
            cid = self.hosts.aliases.get(id(node))
        if cid is None and node.get('id') in self.hosts.component_ids:
            cid = node.get('id')
        return cid

    def _host(self, node, cid):
        if cid in self._stack:
            cycle = list(self._stack) + [cid]
            self._issue(
                'cycle',
                f'component cycle detected: {" -> ".join(cycle)}',
                component=cid)
            self._conflict('cycle', component=cid, detail=cycle)
            self._unresolved(
                'cycle', f'component {cid!r} forms a cycle', component=cid)
            return self._keep_host(node)
        if cid in self.unreadable:
            reason = self.unreadable[cid]
            self._issue(
                'unreadable-component',
                f'component {cid!r} root is unreadable: {reason}',
                component=cid)
            self._unresolved(
                'unreadable-component',
                f'component {cid!r} root is unreadable', component=cid,
                ref=reason)
            return self._keep_host(node)
        if cid not in self.roots:
            self._issue(
                'missing-component',
                f'host references unknown component {cid!r}', component=cid)
            self._unresolved(
                'missing-component',
                f'host references unknown component {cid!r}', component=cid)
            return self._keep_host(node)
        clone = _clone_shallow(node)
        self._record(node, clone, 'original')
        clone.append(self._clone_root(cid))
        return clone

    def _keep_host(self, node):
        """Keep an unexpanded host, preserving whatever it contained."""
        clone = _clone_shallow(node)
        self._record(node, clone, 'original')
        self._copy_children(node, clone, owner=None, origin='original')
        return clone

    def _clone_root(self, cid):
        source = self.roots[cid]
        clone = _clone_shallow(source)
        # Trailing text in a single-element root is content after the root;
        # whitespace-only tails (common in triple-quoted markup) are dropped.
        if source.tail and source.tail.strip():
            clone.tail = source.tail
        else:
            clone.tail = None
        self._record(None, clone, 'component')
        self._stack.append(cid)
        try:
            self._copy_children(source, clone, owner=cid, origin='component')
        finally:
            self._stack.pop()
        return clone

    def _slot(self, slot, owner):
        name = slot.get('name')
        name = '' if name is None else str(name)
        key = (owner, name)
        clone = _clone_shallow(slot)
        self._record(None, clone, 'component', name)
        self.slot_nodes.setdefault(key, []).append(clone)
        entry = self.plan.assignments.get(key)

        if key in self._slot_seen:
            # Assigned nodes appear only in the first matching slot; a
            # repeated slot has nothing assigned to it, so it shows its
            # fallback content like any other empty slot.
            self._copy_children(slot, clone, owner, origin='component')
            return clone
        self._slot_seen.add(key)

        if entry is None:
            self._copy_children(slot, clone, owner, origin='component')
            return clone
        if entry['use_fallback']:
            self._copy_children(slot, clone, owner, origin='component')
            return clone
        if not entry['placed']:
            # An unresolved assignment is not an empty slot, so the
            # fallback must not be shown.
            return clone

        for element in entry['placed']:
            copied = self._copy(element, owner=None, origin='original',
                                forced=True)
            if copied is None:
                continue
            copied.tail = None
            clone.append(copied)
            self.assigned.append(copied)
        return clone

    # -- entry point ---------------------------------------------------
    def render(self):
        for node in self.document_nodes:
            copied = self._copy(node, owner=None)
            if copied is not None:
                self.projected_roots.append(copied)
        return self


def _attach_tail(target, tail):
    if not tail:
        return None
    if len(target):
        last = target[-1]
        last.tail = (last.tail or '') + tail
    else:
        target.tail = (target.tail or '') + tail


def build_projection(document, components=None, assignments=None,
                     component_roots=None, slot_assignments=None,
                     original_selector=None):
    """Compose ``document`` using the supplied component and slot tables.

    ``components`` / ``component_roots`` are aliases, as are ``assignments``
    / ``slot_assignments``.  See :func:`normalize_components` and
    :func:`normalize_assignments` for the accepted table shapes.

    :returns: a :class:`Projection`.
    """
    if components is None:
        components = component_roots
    if assignments is None:
        assignments = slot_assignments
    document_nodes = _document_nodes(document)
    if original_selector:
        document_nodes = list(_view(document_nodes)(original_selector))

    roots, unreadable = normalize_components(components)
    records = normalize_assignments(assignments)
    hosts = _HostTable(document_nodes, roots, components, records,
                       unreadable=unreadable)
    plan = _Plan(document_nodes, hosts, records, roots, unreadable)
    renderer = _Renderer(document_nodes, roots, hosts, plan, unreadable)
    renderer.render()

    projected_nodes = renderer.projected_roots
    positions = []
    root = document_nodes[0] if document_nodes else None
    projected_root = projected_nodes[0] if projected_nodes else None
    for node in _iter_elements(document_nodes):
        projected = renderer.by_original.get(id(node))
        info = renderer.origin.get(id(projected))
        positions.append(NodePosition(
            original=node,
            projected=projected,
            original_path=_relative_path(root, node),
            projected_path=_relative_path(projected_root, projected),
            origin=info[0] if info else 'original',
            slot=info[1] if info else None,
        ))
    for node in _iter_elements(projected_nodes):
        original = renderer.by_projected.get(id(node))
        if original is not None:
            continue
        info = renderer.origin.get(id(node))
        positions.append(NodePosition(
            original=None,
            projected=node,
            original_path=None,
            projected_path=_relative_path(projected_root, node),
            origin=info[0] if info else 'component',
            slot=info[1] if info else None,
        ))

    projection = Projection(
        original_view=_view(document_nodes),
        projected_view=_view(projected_nodes),
        positions=positions,
        issues=renderer.issues,
        conflicts=renderer.conflicts,
        unresolved=renderer.unresolved,
        unreadable=unreadable,
        assigned=renderer.assigned,
        slots=renderer.slot_nodes,
        component_roots=roots,
    )
    return projection


def project(document, components=None, assignments=None, *,
            component_roots=None, slot_assignments=None,
            original_selector=None):
    """Convenience wrapper around :func:`build_projection`."""
    if components is None:
        components = component_roots
    if assignments is None:
        assignments = slot_assignments
    return build_projection(
        document, components=components, assignments=assignments,
        original_selector=original_selector)
