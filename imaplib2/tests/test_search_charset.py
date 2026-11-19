"""Tests for BADCHARSET-aware SEARCH charset fallback."""

import pytest

from imaplib2.imaplib2 import (
    DEFAULT_SEARCH_CHARSETS,
    IMAP4,
    _canonical_charset,
    _encode_search_criteria,
    _is_badcharset_response,
)


BADCHARSET = ('NO', [b'[BADCHARSET] Unsupported charset'])


def make_imap(handler):
    """Build an IMAP4 instance without opening a socket."""
    imap = IMAP4.__new__(IMAP4)
    imap.utf8_enabled = False
    imap._server_charsets = None
    imap._search_charset_policy = ()
    imap._log = lambda *args, **kwargs: None
    calls = []

    def fake_simple_command(name, *args, **kw):
        calls.append((name, args))
        return handler(name, args)

    imap._simple_command = fake_simple_command
    imap.calls = calls
    return imap


def list_response(charsets):
    lines = tuple(
        b'* LIST () "" "CHARSET:%s"' % name.encode('ASCII')
        for name in charsets
    )
    return 'OK', list(lines)


def search_calls(imap):
    return [args for name, args in imap.calls if name == 'SEARCH']


def charset_tokens(imap):
    tokens = []
    for args in search_calls(imap):
        tokens.append(args[1])
    return tokens


def test_search_without_badcharset_is_sent_once():
    imap = make_imap(lambda name, args: ('OK', [b'1 2 3']))

    typ, dat = imap.search('UTF-8', 'SUBJECT', 'hello')

    assert (typ, dat) == ('OK', [b'1 2 3'])
    assert len(search_calls(imap)) == 1
    assert imap.calls[0] == ('SEARCH', ('CHARSET', 'UTF-8', 'SUBJECT', 'hello'))


def test_search_without_charset_skips_charset_handling():
    imap = make_imap(lambda name, args: ('OK', [None]))

    typ, dat = imap.search(None, 'ALL')

    assert typ == 'OK'
    assert [name for name, _ in imap.calls] == ['SEARCH']
    assert 'CHARSET' not in imap.calls[0][1]


def test_utf8_mode_rejects_explicit_charset():
    imap = make_imap(lambda name, args: ('OK', []))
    imap.utf8_enabled = True

    with pytest.raises(IMAP4.error):
        imap.search('UTF-8', 'SUBJECT', 'hello')


def test_badcharset_retries_server_declared_charset_and_keeps_text():
    def handler(name, args):
        if name == 'LIST':
            return list_response(['UTF-8'])
        if args[1] == 'US-ASCII':
            return BADCHARSET
        return 'OK', [b'7']

    imap = make_imap(handler)

    typ, dat = imap.search('US-ASCII', 'SUBJECT', 'café')

    assert typ == 'OK' and dat == [b'7']
    tokens = charset_tokens(imap)
    assert tokens == ['US-ASCII', 'UTF-8']
    retry = search_calls(imap)[-1]
    assert retry[-1] == 'café'.encode('UTF-8')


def test_successful_empty_search_differs_from_unexecutable_search():
    def handler(name, args):
        if name == 'LIST':
            return list_response(['UTF-8'])
        if args[1] == 'US-ASCII':
            return BADCHARSET
        return 'OK', [None]

    imap = make_imap(handler)

    typ, dat = imap.search('US-ASCII', 'SUBJECT', 'café')

    assert typ == 'OK'
    assert dat == [None]


def test_all_candidates_rejected_returns_last_badcharset_without_loop():
    def handler(name, args):
        if name == 'LIST':
            return list_response(['UTF-8', 'ISO-8859-1'])
        return BADCHARSET

    imap = make_imap(handler)
    imap.set_search_charset_policy(['GB18030'])

    typ, dat = imap.search('US-ASCII', 'SUBJECT', 'café')

    assert typ == 'NO'
    assert dat == [b'[BADCHARSET] Unsupported charset']

    tokens = charset_tokens(imap)
    assert len(tokens) == len(set(tokens))
    assert tokens[0] == 'US-ASCII'
    candidates = set(imap._search_charset_candidates('US-ASCII'))
    sent = set(tokens[1:])
    # Only candidates declared usable locally are sent; unrepresentable
    # charsets stay undecided and never reach the server.
    assert sent <= candidates
    assert sent == {'UTF-8', 'ISO-8859-1', 'GB18030'}
    assert sent != candidates
    for args in search_calls(imap):
        for arg in args:
            if isinstance(arg, bytes):
                assert b'?' not in arg


def test_unrepresentable_word_skips_charset_without_question_marks():
    word = '你好'

    def handler(name, args):
        if name == 'LIST':
            return list_response(['ISO-8859-1'])
        if args[1] == 'US-ASCII':
            return BADCHARSET
        assert args[1] == 'UTF-8'
        return 'OK', [b'9']

    imap = make_imap(handler)

    typ, dat = imap.search('US-ASCII', 'SUBJECT', word)

    assert typ == 'OK'
    tokens = charset_tokens(imap)
    assert 'ISO-8859-1' not in tokens
    retry = search_calls(imap)[-1]
    encoded_word = retry[-1]
    assert encoded_word == word.encode('UTF-8')
    assert b'?' not in encoded_word


def test_unknown_local_codec_is_left_undecided_and_skipped():
    def handler(name, args):
        if name == 'LIST':
            return 'OK', [None]
        if args[1] == 'US-ASCII':
            return BADCHARSET
        assert args[1] == 'UTF-8'
        return 'OK', [b'2']

    imap = make_imap(handler)
    imap.set_search_charset_policy(['X-NO-SUCH-CODEC'])

    typ, dat = imap.search('US-ASCII', 'SUBJECT', 'hello')

    assert typ == 'OK'
    assert 'X-NO-SUCH-CODEC' not in charset_tokens(imap)


def test_list_failure_keeps_charsets_undecided_but_defaults_still_run():
    def handler(name, args):
        if name == 'LIST':
            raise RuntimeError('listing unavailable')
        if args[1] == 'US-ASCII':
            return BADCHARSET
        assert args[1] == 'UTF-8'
        return 'OK', [b'11']

    imap = make_imap(handler)

    typ, dat = imap.search('US-ASCII', 'SUBJECT', 'café')

    assert typ == 'OK'
    assert charset_tokens(imap)[-1] == 'UTF-8'
    assert imap._server_charsets is None


def test_policy_only_widens_candidates():
    imap = make_imap(lambda name, args: ('OK', []))
    imap._server_charsets = ('UTF-8',)
    imap.set_search_charset_policy(['gb18030', 'GB18030', 'utf_8'])

    candidates = imap._search_charset_candidates('US-ASCII')

    assert candidates[0] == 'UTF-8'
    assert 'GB18030' in candidates
    assert len(candidates) == len(set(candidates))
    for default in DEFAULT_SEARCH_CHARSETS:
        assert default in candidates


def test_server_charset_discovery_uses_list_and_is_cached():
    imap = make_imap(lambda name, args: list_response(['UTF-8', 'GB18030']))

    charsets = imap._get_server_charsets()

    assert charsets == ('UTF-8', 'GB18030')
    list_calls = [args for name, args in imap.calls if name == 'LIST']
    assert list_calls == [('""', '"CHARSET:*"')]

    imap._get_server_charsets()
    list_calls = [args for name, args in imap.calls if name == 'LIST']
    assert len(list_calls) == 1


def test_server_charset_discovery_parses_header_field_style_listing():
    lines = [b'* LIST (\\HasNoChildren) "/" "CHARSET:SHIFT-JIS"']
    imap = make_imap(lambda name, args: ('OK', lines))

    assert imap._get_server_charsets() == ('SHIFT-JIS',)


def test_encoding_preserves_nested_operators_and_quotes():
    criteria = ('OR', 'NOT', 'SUBJECT', 'naïve',
                '(', 'FROM', '"bücher"', ')', 'SINCE', '01-Jan-2020')

    encoded = _encode_search_criteria(criteria, 'UTF-8')

    assert encoded[0] == b'OR'
    assert encoded[1] == b'NOT'
    assert encoded[2] == b'SUBJECT'
    assert encoded[3] == 'naïve'.encode('UTF-8')
    assert encoded[4] == b'('
    assert encoded[5] == b'FROM'
    assert encoded[6] == b'"' + 'bücher'.encode('UTF-8') + b'"'
    assert encoded[7] == b')'
    assert encoded[8] == b'SINCE'
    assert encoded[9] == b'01-Jan-2020'


def test_encoding_ascii_criteria_is_byte_identical():
    criteria = ('HEADER', 'X-Custom', 'plain', 'SINCE', '01-Jan-2020')

    encoded = _encode_search_criteria(criteria, 'ISO-8859-1')

    assert encoded == (b'HEADER', b'X-Custom', b'plain', b'SINCE', b'01-Jan-2020')


def test_encoding_header_field_and_text_handled_separately():
    encoded = _encode_search_criteria(('HEADER', 'Subject', 'café'), 'UTF-8')

    assert encoded[0] == b'HEADER'
    assert encoded[1] == b'Subject'
    assert encoded[2] == 'café'.encode('UTF-8')


def test_encoding_bytes_arguments_pass_through_untouched():
    encoded = _encode_search_criteria(('SUBJECT', b'raw-bytes'), 'UTF-8')

    assert encoded == (b'SUBJECT', b'raw-bytes')


def test_badcharset_detection():
    assert _is_badcharset_response('NO', [b'[BADCHARSET]'])
    assert _is_badcharset_response('NO', [b'no badcharset here'])
    assert not _is_badcharset_response('BAD', [b'[BADCHARSET]'])
    assert not _is_badcharset_response('NO', [b'[TRYCREATE]'])
    assert not _is_badcharset_response('OK', [None])


def test_canonical_charset():
    assert _canonical_charset('utf_8') == 'UTF-8'
    assert _canonical_charset(' iso-8859-1 ') == 'ISO-8859-1'
    assert _canonical_charset(None) is None
