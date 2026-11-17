"""Rebuild field-level validation results after an RPA form submission.

The response page may re-layout controls or assign new ids, so submitted
fields are re-located from three sources, in order:

1. the server-returned mapping (request field key -> new control id),
2. the control id saved at request time (when it survived the re-layout),
3. the control name, but only when it matches exactly one control --
   duplicate names are never merged into a single field.

``aria-describedby`` may reference help text as well as errors; the
caller's ``error_marker`` rule decides which referenced nodes count as
errors.  Error nodes that cannot be attributed to exactly one requested
field are reported at form level under ``pending``.  The function keeps
no state between calls, so errors from a previous request never leak
into the current response.
"""

from pyquery import PyQuery

__all__ = ['build_validation_report']

#: Attributes an error node may use to point back at its control.
_BACKREF_ATTRIBUTES = ('for', 'data-for', 'data-error-for', 'data-field')


def _default_error_marker(element):
    """Default rule: role="alert", a data-error attribute, or an error class."""
    if element.get('role') == 'alert':
        return True
    if 'data-error' in element.attrib:
        return True
    classes = (element.get('class') or '').lower().split()
    return any('error' in token for token in classes)


def _text(element):
    return ' '.join(element.text_content().split())


def _field_value(field, name, default=None):
    if isinstance(field, dict):
        return field.get(name, default)
    return getattr(field, name, default)


def _normalize_mapping(server_mapping):
    if not server_mapping:
        return {}
    if hasattr(server_mapping, 'get'):
        return dict(server_mapping)
    mapping = {}
    for item in server_mapping:
        if isinstance(item, dict):
            mapping[item.get('key')] = item.get('control_id', item.get('id'))
        else:
            key, control_id = item
            mapping[key] = control_id
    return mapping


def _error_predicate(error_marker, tree):
    if error_marker is None:
        return _default_error_marker
    if isinstance(error_marker, str):
        paths = {tree.getpath(el)
                 for el in tree.getroot().cssselect(error_marker)}
        return lambda el: tree.getpath(el) in paths
    if callable(error_marker):
        return error_marker
    raise TypeError(
        'error_marker must be None, a CSS selector, or a callable')


def build_validation_report(request_fields, server_mapping, response_html,
                            error_marker=None):
    """Build validation results for one RPA form submission.

    ``request_fields`` is the list of field identities saved at request
    time; each entry is a dict (or object) with a unique ``key`` and
    optional ``name`` and ``id`` (the control id when the request was
    sent).  Duplicate names are allowed and kept as separate entries.

    ``server_mapping`` maps a field key to the control id it received in
    the response (a dict, or an iterable of pairs / dicts).

    ``response_html`` is the body of the current response.

    ``error_marker`` tells errors apart from help text referenced by
    ``aria-describedby``: ``None`` for the default rule (role="alert",
    a ``data-error`` attribute, or a class containing "error"), a CSS
    selector string, or a callable taking an lxml element.

    Returns ``{'fields': [...], 'pending': [...]}`` with one field entry
    per requested field (order and duplicates preserved) and the
    form-level errors that still need confirmation.

    >>> html = '''
    ... <input id="c1" name="email" aria-describedby="h1 e1">
    ... <span id="h1" class="hint">We never share it.</span>
    ... <span id="e1" class="error">Invalid email.</span>
    ... <span id="e9" class="error">Unknown problem.</span>'''
    >>> report = build_validation_report(
    ...     [{'key': 'f1', 'name': 'email', 'id': 'old1'}],
    ...     {'f1': 'c1'}, html)
    >>> report['fields'][0]['errors']
    ['Invalid email.']
    >>> report['pending'][0]['message']
    'Unknown problem.'
    """
    doc = PyQuery(response_html or '<html></html>')
    tree = doc.root
    mapping = _normalize_mapping(server_mapping)
    is_error = _error_predicate(error_marker, tree)

    def by_id(node_id):
        return tree.xpath('//*[@id=$node_id]', node_id=node_id)

    def unique_by_id(node_id):
        matches = by_id(node_id) if node_id else []
        return matches[0] if len(matches) == 1 else None

    fields = []
    controls = []
    control_fields = {}  # control path -> field indices (duplicates kept)
    for field in request_fields:
        key = _field_value(field, 'key')
        name = _field_value(field, 'name')
        saved_id = (_field_value(field, 'id')
                    or _field_value(field, 'control_id'))
        control = unique_by_id(mapping.get(key))
        if control is None:
            control = unique_by_id(saved_id)
        if control is None and name:
            matches = tree.xpath('//*[@name=$name]', name=name)
            if len(matches) == 1:
                control = matches[0]
        index = len(fields)
        fields.append({
            'key': key,
            'name': name,
            'control_id': control.get('id') if control is not None else None,
            'resolved': control is not None,
            'errors': [],
        })
        controls.append(control)
        if control is not None:
            control_fields.setdefault(tree.getpath(control), []).append(index)

    # Errors referenced by resolved controls through aria-describedby.
    claimed = set()
    pending = []
    described = {}  # error path -> (element, referencing field indices)
    for index, control in enumerate(controls):
        if control is None:
            continue
        for node_id in (control.get('aria-describedby') or '').split():
            for el in by_id(node_id):
                if is_error(el):
                    path = tree.getpath(el)
                    described.setdefault(path, [el, set()])[1].add(index)
    for path, (el, indices) in described.items():
        if len(indices) == 1:
            fields[next(iter(indices))]['errors'].append(_text(el))
            claimed.add(path)
        else:
            pending.append({'element_id': el.get('id'),
                            'message': _text(el),
                            'reason': 'ambiguous'})
            claimed.add(path)

    # Errors referenced by controls that are not part of this request
    # (e.g. a previous request's form still present in the page).
    foreign = set()
    for el in tree.iter():
        path = tree.getpath(el)
        if path in control_fields:
            continue
        for node_id in (el.get('aria-describedby') or '').split():
            for target in by_id(node_id):
                if is_error(target):
                    foreign.add(tree.getpath(target))

    # Remaining error nodes: attach through a back-reference when it
    # points at exactly one requested field, drop errors belonging to
    # other controls, and leave the rest at form level for confirmation.
    for el in tree.iter():
        if not is_error(el):
            continue
        path = tree.getpath(el)
        if path in claimed or path in foreign:
            continue
        targets = []
        for attr in _BACKREF_ATTRIBUTES:
            ref = el.get(attr)
            if ref:
                targets.extend(by_id(ref))
        target_fields = {index
                         for target in targets
                         for index in control_fields.get(
                                 tree.getpath(target), [])}
        if len(target_fields) == 1:
            fields[next(iter(target_fields))]['errors'].append(_text(el))
        elif len(target_fields) > 1:
            pending.append({'element_id': el.get('id'),
                            'message': _text(el),
                            'reason': 'ambiguous'})
        elif targets:
            continue  # belongs to a control outside this request
        else:
            pending.append({'element_id': el.get('id'),
                            'message': _text(el),
                            'reason': 'unresolved'})
    return {'fields': fields, 'pending': pending}
