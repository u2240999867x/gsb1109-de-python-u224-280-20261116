from rpa_validation import build_validation_report


RESPONSE_HTML = '''
<form id="order">
  <div class="field">
    <input id="ctl_101" name="email" aria-describedby="h101 e101">
    <span id="h101" class="hint">We never share your email.</span>
    <span id="e101" class="error-message">Email is invalid.</span>
  </div>
  <div class="field">
    <input id="ctl_102" name="tag" aria-describedby="e102">
    <span id="e102" class="error-message" role="alert">Tag is required.</span>
  </div>
  <div class="field">
    <input id="ctl_103" name="tag" aria-describedby="e103">
    <span id="e103" data-error>Tag must be unique.</span>
  </div>
  <label id="e104" class="error-label" data-error for="ctl_101">
      Email looks odd.</label>
  <span id="e900" class="error-message">Something went wrong.</span>
  <input id="legacy_1" name="legacy" aria-describedby="e901">
  <span id="e901" class="error-message">Legacy field error.</span>
</form>
'''

REQUEST_FIELDS = [
    {'key': 'f-email', 'name': 'email', 'id': 'old_email'},
    {'key': 'f-tag-1', 'name': 'tag', 'id': 'old_tag_a'},
    {'key': 'f-tag-2', 'name': 'tag', 'id': 'old_tag_b'},
]

SERVER_MAPPING = {
    'f-email': 'ctl_101',
    'f-tag-1': 'ctl_102',
    'f-tag-2': 'ctl_103',
}


def make_report(**kwargs):
    kwargs.setdefault('error_marker', None)
    return build_validation_report(
        REQUEST_FIELDS, SERVER_MAPPING, RESPONSE_HTML, **kwargs)


def test_fields_relocated_through_server_mapping():
    report = make_report()
    assert [f['control_id'] for f in report['fields']] == [
        'ctl_101', 'ctl_102', 'ctl_103']
    assert all(f['resolved'] for f in report['fields'])


def test_duplicate_names_are_kept_separate():
    report = make_report()
    assert [f['name'] for f in report['fields']] == ['email', 'tag', 'tag']
    assert [f['key'] for f in report['fields']] == [
        'f-email', 'f-tag-1', 'f-tag-2']


def test_errors_attached_to_their_own_field():
    report = make_report()
    errors = [f['errors'] for f in report['fields']]
    assert errors[0] == ['Email is invalid.', 'Email looks odd.']
    assert errors[1] == ['Tag is required.']
    assert errors[2] == ['Tag must be unique.']


def test_help_text_is_not_reported_as_error():
    report = make_report()
    all_errors = [e for f in report['fields'] for e in f['errors']]
    assert 'We never share your email.' not in all_errors


def test_unattributable_error_is_pending_at_form_level():
    report = make_report()
    assert [(p['element_id'], p['reason']) for p in report['pending']] == [
        ('e900', 'unresolved')]
    assert report['pending'][0]['message'] == 'Something went wrong.'


def test_previous_request_errors_are_not_attached():
    report = make_report()
    all_errors = [e for f in report['fields'] for e in f['errors']]
    pending = [p['message'] for p in report['pending']]
    assert 'Legacy field error.' not in all_errors
    assert 'Legacy field error.' not in pending


def test_custom_error_marker_selector():
    report = make_report(error_marker='.error-message')
    errors = [f['errors'] for f in report['fields']]
    assert errors[0] == ['Email is invalid.']
    assert errors[1] == ['Tag is required.']
    assert errors[2] == []  # e103 has no .error-message class
    assert [p['element_id'] for p in report['pending']] == ['e900']


def test_custom_error_marker_callable():
    report = make_report(
        error_marker=lambda el: el.get('data-error') is not None)
    errors = [f['errors'] for f in report['fields']]
    assert errors[0] == ['Email looks odd.']
    assert errors[2] == ['Tag must be unique.']
    assert report['pending'] == []


def test_saved_id_fallback_when_mapping_is_missing():
    html = '<input id="still_there" name="code" aria-describedby="e1">' \
           '<span id="e1" class="error">Bad code.</span>'
    report = build_validation_report(
        [{'key': 'f1', 'name': 'code', 'id': 'still_there'}], {}, html)
    assert report['fields'][0]['resolved'] is True
    assert report['fields'][0]['errors'] == ['Bad code.']


def test_unique_name_fallback_when_ids_changed():
    html = '<input id="brand_new" name="city" aria-describedby="e1">' \
           '<span id="e1" class="error">Unknown city.</span>'
    report = build_validation_report(
        [{'key': 'f1', 'name': 'city', 'id': 'gone'}], {}, html)
    assert report['fields'][0]['control_id'] == 'brand_new'
    assert report['fields'][0]['errors'] == ['Unknown city.']


def test_duplicate_name_without_mapping_stays_unresolved():
    html = '<input id="a" name="tag"><input id="b" name="tag">'
    report = build_validation_report(
        [{'key': 'f1', 'name': 'tag', 'id': 'gone'},
         {'key': 'f2', 'name': 'tag', 'id': 'gone'}], {}, html)
    assert [f['resolved'] for f in report['fields']] == [False, False]


def test_shared_error_reference_is_ambiguous():
    html = '<input id="a" name="t1" aria-describedby="e1">' \
           '<input id="b" name="t2" aria-describedby="e1">' \
           '<span id="e1" class="error">Shared failure.</span>'
    report = build_validation_report(
        [{'key': 'f1', 'name': 't1', 'id': 'a'},
         {'key': 'f2', 'name': 't2', 'id': 'b'}], {}, html)
    assert report['fields'][0]['errors'] == []
    assert report['fields'][1]['errors'] == []
    assert report['pending'] == [
        {'element_id': 'e1', 'message': 'Shared failure.',
         'reason': 'ambiguous'}]


def test_no_state_leaks_between_calls():
    first = make_report()
    second = build_validation_report(
        REQUEST_FIELDS, SERVER_MAPPING,
        '<form><input id="ctl_101" name="email"></form>')
    assert first['fields'][0]['errors']  # first call had errors
    assert all(not f['errors'] for f in second['fields'])
    assert second['pending'] == []
