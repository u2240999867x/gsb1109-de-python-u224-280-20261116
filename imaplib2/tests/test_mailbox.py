import json
import unicodedata

import pytest

from imaplib2 import mailbox as mb


def ns_response(personal=b'(("" "."))', other=b'NIL', shared=b'NIL'):
    return [b'* NAMESPACE ' + personal + b' ' + other + b' ' + shared]


@pytest.fixture
def catalog():
    namespaces = mb.parse_namespace_response(
        ns_response(personal=b'(("" ".") ("INBOX." "."))',
                    other=b'(("Other/" "/"))',
                    shared=b'(("Shared/" "/"))'))
    return mb.MailboxCatalog(namespaces)


# ---------------------------------------------------------------- encoding


@pytest.mark.parametrize("text,raw", [
    ("INBOX", b"INBOX"),
    ("", b""),
    ("a&b", b"a&-b"),
    ("Entwürfe", b"Entw&APw-rfe"),
    ("台北", b"&U,BTFw-"),
    ("日本語", b"&ZeVnLIqe-"),
    ("\U0001f600", b"&2D3eAA-"),
])
def test_encode_modified_utf7_vectors(text, raw):
    assert mb.encode_modified_utf7(text) == raw
    assert mb.decode_modified_utf7(raw) == text


def test_modified_utf7_encodes_ascii_as_is_and_ampersand_specially():
    assert mb.encode_modified_utf7("~$()[]{}\\_") == b"~$()[]{}\\_"
    assert mb.encode_modified_utf7("&&") == b"&-&-"


def test_modified_utf7_rejects_invalid_shift():
    with pytest.raises(mb.MailboxNameError):
        mb.decode_modified_utf7(b"&AOQ")          # missing terminator
    with pytest.raises(mb.MailboxNameError):
        mb.decode_modified_utf7(b"&A!Q-")         # bad base64 alphabet


def test_modified_utf7_rejects_lone_surrogate():
    with pytest.raises(mb.MailboxNameError):
        mb.encode_modified_utf7("\ud800")


def test_decode_marks_noncanonical_but_round_trippable_names():
    padded = mb.decode_mailbox_name(b"INBOX.&AOQ=-")
    assert padded.text == "INBOX.ä"
    assert padded.noncanonical
    assert not padded.round_trip

    canonical = mb.decode_mailbox_name(b"INBOX.&AOQ-")
    assert canonical.text == "INBOX.ä"
    assert canonical.round_trip
    assert not canonical.noncanonical


def test_encoded_ascii_is_not_round_trippable():
    decoded = mb.decode_mailbox_name(b"&AGE-")
    assert decoded.text == "a"
    assert decoded.noncanonical
    assert not decoded.round_trip


def test_literal_slash_decodes_as_text_not_hierarchy():
    decoded = mb.decode_mailbox_name(b"INBOX.a/b")
    assert decoded.text == "INBOX.a/b"
    assert decoded.round_trip


# ------------------------------------------------------------- conventions


def test_utf8_convention_decodes_and_round_trips():
    decoded = mb.decode_mailbox_name("Entwürfe".encode("utf-8"), mb.UTF8)
    assert decoded.text == "Entwürfe"
    assert decoded.decode_state is mb.MailboxDecodeState.UTF8
    assert decoded.round_trip


def test_utf8_convention_rejects_invalid_utf8_bytes():
    decoded = mb.decode_mailbox_name(b"Entw\xfc", mb.UTF8)
    assert decoded.text is None
    assert decoded.decode_state is mb.MailboxDecodeState.INVALID
    assert decoded.raw == b"Entw\xfc"


def test_modified_utf7_convention_does_not_silently_decode_utf8():
    decoded = mb.decode_mailbox_name("Entwürfe".encode("utf-8"))
    assert decoded.text is None
    assert decoded.decode_state is mb.MailboxDecodeState.INVALID


def test_fallback_marks_state_and_is_not_addressable(catalog):
    raw = b"Entw\xc3\xbcrfe"
    decoded = mb.decode_mailbox_name(raw, fallback_utf8=True)
    assert decoded.text == "Entwürfe"
    assert decoded.decode_state is mb.MailboxDecodeState.UTF8_FALLBACK
    assert not decoded.round_trip

    entry = mb.MailboxCatalog(encoding=mb.MODIFIED_UTF7,
                              fallback_utf8=True).add(raw)
    assert entry.decode_state is mb.MailboxDecodeState.UTF8_FALLBACK
    assert not entry.addressable_by_name


def test_invalid_name_still_archives_with_stable_identity(catalog):
    entry = catalog.add(b"bad\xffname")
    assert entry.text is None
    assert entry.decode_state is mb.MailboxDecodeState.INVALID
    assert entry.raw == b"bad\xffname"
    assert entry.directory_id.startswith("mbx_")
    assert "invalid" in entry.display_name

    again = catalog.add(b"bad\xffname")
    assert again is entry


# --------------------------------------------------------------- namespaces


def test_parse_namespace_lists_and_delimiters():
    parsed = mb.parse_namespace_response(ns_response(
        personal=b'(("" ".") ("INBOX." "."))',
        other=b'(("Other/" "/"))',
        shared=b'(("Shared/" "/"))'))
    assert [ns.prefix for ns in parsed.personal] == [b"", b"INBOX."]
    assert [ns.delimiter for ns in parsed.personal] == [".", "."]
    assert parsed.other[0].kind == "other"
    assert parsed.shared[0].delimiter == "/"


def test_parse_namespace_nil_lists_and_missing_data():
    parsed = mb.parse_namespace_response(ns_response())
    assert parsed.other == () and parsed.shared == ()
    assert mb.parse_namespace_response([]) == mb.NamespaceSet()
    assert mb.parse_namespace_response([None]) == mb.NamespaceSet()


def test_parse_namespace_accepts_bare_argument_bytes():
    parsed = mb.parse_namespace_response(
        b'(("" ".")) NIL (("Shared/" "/"))')
    assert parsed.personal[0].delimiter == "."
    assert parsed.shared[0].prefix_text == "Shared/"


def test_namespace_prefix_matching_requires_the_declared_prefix(catalog):
    assert [ns.kind for ns in catalog.namespaces.match(b"INBOX.x")] \
        == ["personal"]
    # "INBOX" without the delimiter is outside "INBOX.", so only "" matches
    assert [ns.prefix for ns in catalog.namespaces.match(b"INBOX")] == [b""]
    assert catalog.namespaces.match(b"Shared/x")[0].prefix == b"Shared/"
    # A root prefix "" is a declared namespace and matches everything.
    assert catalog.namespaces.match(b"Nowhere/x")[0].prefix == b""


def test_ambiguous_namespace_prefix_is_not_resolved_silently():
    namespaces = mb.parse_namespace_response(
        ns_response(personal=b'(("" ".") ("A/" "/"))',
                    other=b'(("A/" "."))'))
    catalog = mb.MailboxCatalog(namespaces)
    entry = catalog.add(b"A/x", delimiter="/")
    assert entry.namespace is None
    assert entry.namespace_ambiguous
    assert entry.delimiter == "/"       # LIST delimiter is still honoured
    assert any("namespace" in warning for warning in entry.warnings)


# ---------------------------------------------------------------- hierarchy


def test_delimiter_comes_from_server_not_from_slash_in_name(catalog):
    entry = catalog.add(b"INBOX.a/b")
    assert entry.delimiter == "."
    assert entry.segments == ("a/b",)
    assert entry.relative_name == "a/b"

    nested = catalog.add(b"INBOX.parent.child")
    assert nested.segments == ("parent", "child")


def test_flat_namespace_has_no_hierarchy_segments():
    namespaces = mb.parse_namespace_response(ns_response(personal=b'(("" NIL))'))
    entry = mb.MailboxCatalog(namespaces).add(b"a/b/c")
    assert entry.delimiter is None
    assert entry.segments == ("a/b/c",)


def test_list_delimiter_overrides_namespace_delimiter(catalog):
    entry = catalog.add(b"INBOX.x", delimiter="/")
    assert entry.delimiter == "/"
    assert entry.segments == ("x",)
    assert any("differs" in warning for warning in entry.warnings)


# ---------------------------------------------------------------- conflicts


def test_same_text_from_different_names_gets_distinct_ids_and_conflict(catalog):
    canonical = catalog.add(b"INBOX.&AOQ-")
    padded = catalog.add(b"INBOX.&AOQ=-")
    assert canonical.text == padded.text == "INBOX.ä"
    assert canonical.directory_id != padded.directory_id
    assert canonical.conflict_group == padded.conflict_group
    assert canonical.is_conflict and padded.is_conflict
    assert catalog.conflicts()[canonical.conflict_group] == (canonical, padded)


def test_normalization_collision_gets_distinct_ids_and_conflict():
    catalog = mb.MailboxCatalog(encoding=mb.UTF8)
    composed = catalog.add("café".encode("utf-8"))
    decomposed = catalog.add("café".encode("utf-8"))
    assert composed.raw != decomposed.raw
    assert composed.text != decomposed.text
    assert composed.normalized == decomposed.normalized
    assert composed.directory_id != decomposed.directory_id
    assert composed.conflict_group == decomposed.conflict_group


def test_normalization_none_only_compares_exact_text():
    catalog = mb.MailboxCatalog(encoding=mb.UTF8, normalization=None)
    composed = catalog.add("café".encode("utf-8"))
    decomposed = catalog.add("café".encode("utf-8"))
    assert composed.conflict_group is None
    assert decomposed.conflict_group is None


def test_conflicting_names_never_overwrite_or_subsume(catalog):
    first = catalog.add(b"INBOX.&AOQ-")
    second = catalog.add(b"INBOX.&AOQ=-")
    snapshot = {entry.directory_id: entry.raw for entry in catalog}
    assert snapshot[first.directory_id] == b"INBOX.&AOQ-"
    assert snapshot[second.directory_id] == b"INBOX.&AOQ=-"
    assert len(catalog) == 2


def test_text_lookup_of_conflict_refuses_instead_of_picking(catalog):
    first = catalog.add(b"INBOX.&AOQ-")
    second = catalog.add(b"INBOX.&AOQ=-")
    with pytest.raises(mb.MailboxConflictError) as info:
        catalog.protocol_name_for_text("INBOX.ä")
    assert set(info.value.directory_ids) == {first.directory_id,
                                             second.directory_id}
    with pytest.raises(mb.MailboxConflictError):
        catalog.resolve("INBOX.ä")


def test_conflict_members_are_not_addressable_by_name(catalog):
    first = catalog.add(b"INBOX.&AOQ-")
    catalog.add(b"INBOX.&AOQ=-")
    assert not first.addressable_by_name
    assert first.round_trip                # bytes are fine; text is ambiguous


# ------------------------------------------------------------- round-tripping


def test_unroundtrippable_name_archives_and_resolves_by_identity(catalog):
    entry = catalog.add(b"&AGE-")             # encoded printable 'a'
    assert entry.text == "a"
    assert not entry.round_trip
    assert not entry.addressable_by_name

    assert catalog.protocol_name(entry.directory_id) == b"&AGE-"
    assert catalog.resolve(entry.directory_id) is entry

    with pytest.raises(mb.MailboxNotAddressableError):
        catalog.protocol_name_for_text("a")
    with pytest.raises(mb.MailboxNotAddressableError):
        catalog.resolve("a")


def test_reencoding_must_match_the_archived_name(catalog):
    entry = catalog.add(b"INBOX.&AOQ-")
    assert catalog.protocol_name_for_text("INBOX.ä") == entry.raw
    # Canonically equivalent text that encodes to different bytes is refused.
    decomposed = unicodedata.normalize("NFD", "INBOX.ä")
    with pytest.raises(mb.MailboxNotAddressableError):
        catalog.protocol_name_for_text(decomposed)


def test_invalid_name_cannot_be_addressed_by_its_placeholder(catalog):
    entry = catalog.add(b"bad\xffname")
    with pytest.raises(mb.MailboxNotFound):
        catalog.protocol_name_for_text(entry.display_name)
    with pytest.raises(mb.MailboxNotFound):
        catalog.protocol_name_for_text("bad-name")


def test_identity_addressing_survives_export_round_trip(catalog):
    entry = catalog.add(b"INBOX.&AOQ-")
    exported = json.loads(json.dumps(catalog.export_dicts()))
    record = next(item for item in exported
                  if item["directory_id"] == entry.directory_id)
    assert record["name"] == "INBOX.ä"
    assert record["round_trip"] is True
    assert record["delimiter"] == "."
    assert record["segments"] == ["ä"]
    assert record["decode_state"] == "modified-utf7"
    assert catalog.protocol_name(record["directory_id"]) == b"INBOX.&AOQ-"


# ------------------------------------------------------------ LIST parsing


def test_parse_list_quoted_atom_and_literal_names():
    data = [
        b'(\\HasNoChildren) "." "INBOX.Sent"',
        b'(\\HasChildren) "/" Other/x',
        (b'(\\HasNoChildren) "." {9}', b"INBOX.\xc3\xa4"),
        b'* LIST (\\Noselect) "." ""',
    ]
    entries = mb.parse_list_response(data)
    assert [entry.raw_name for entry in entries] == [
        b"INBOX.Sent", b"Other/x", b"INBOX.\xc3\xa4", b""]
    assert [entry.delimiter for entry in entries] == [".", "/", ".", "."]


def test_parse_list_escaped_quotes_and_backslash():
    entry = mb.parse_list_response([b'(\\HasNoChildren) "." "a\\"b\\\\c"'])[0]
    assert entry.raw_name == b'a"b\\c'


def test_parse_list_uses_literal_name_not_braces_marker():
    entry = mb.parse_list_response(
        [(b'(\\HasNoChildren) "." {3}', b"a.b")])[0]
    assert entry.raw_name == b"a.b"


def test_parse_list_nil_delimiter():
    entry = mb.parse_list_response([b'(\\HasNoChildren) NIL INBOX'])[0]
    assert entry.delimiter is None
    assert entry.raw_name == b"INBOX"


def test_parse_list_subscribed_flag():
    entry = mb.parse_list_response(
        [b'(\\Subscribed \\HasNoChildren) "." "INBOX"'])[0]
    assert entry.subscribed


# ---------------------------------------------------------------- connection


class FakeIMAP:
    """Minimal IMAP4 stand-in for catalogue construction."""

    def __init__(self, namespaces, listing, utf8_enabled=False, lsub=None,
                 namespace_ok=True):
        self.utf8_enabled = utf8_enabled
        self._namespaces = namespaces
        self._listing = listing
        self._lsub = lsub or []
        self._namespace_ok = namespace_ok
        self.selected = []

    def namespace(self):
        if not self._namespace_ok:
            return "NO", [b"unknown command"]
        return "OK", [self._namespaces]

    def list(self, directory='""', pattern="*"):
        return "OK", list(self._listing)

    def lsub(self, directory='""', pattern="*"):
        return "OK", list(self._lsub)

    def select(self, mailbox="INBOX", readonly=False):
        self.selected.append((mailbox, readonly))
        return "OK", [b"1"]


def test_catalog_from_imap_uses_connection_encoding_and_namespaces():
    fake = FakeIMAP(
        b'* NAMESPACE (("" ".")) NIL NIL',
        [b'(\\HasNoChildren) "." "INBOX.Sent"',
         (b'(\\HasNoChildren) "." {13}', b"INBOX.&AOQ-")])
    catalog = mb.MailboxCatalog.from_imap(fake)
    assert catalog.encoding == mb.MODIFIED_UTF7
    texts = [entry.text for entry in catalog]
    assert "INBOX.Sent" in texts
    assert "INBOX.ä" in texts


def test_catalog_from_imap_uses_utf8_when_enabled():
    fake = FakeIMAP(
        b'* NAMESPACE (("" ".")) NIL NIL',
        [(b'(\\HasNoChildren) "." {13}', "INBOX.Entwürfe".encode("utf-8"))],
        utf8_enabled=True)
    catalog = mb.MailboxCatalog.from_imap(fake)
    assert catalog.encoding == mb.UTF8
    entry = catalog.entries()[0]
    assert entry.text == "INBOX.Entwürfe"
    assert entry.decode_state is mb.MailboxDecodeState.UTF8


def test_catalog_from_imap_tolerates_missing_namespace_support():
    fake = FakeIMAP(b"", [b'(\\HasNoChildren) "." "INBOX"'],
                    namespace_ok=False)
    catalog = mb.MailboxCatalog.from_imap(fake)
    entry = catalog.entries()[0]
    assert entry.namespace is not None
    assert entry.fallback_namespace
    assert entry.segments == ("INBOX",)


def test_catalog_from_imap_merges_lsub_flags():
    fake = FakeIMAP(
        b'* NAMESPACE (("" ".")) NIL NIL',
        [b'(\\HasNoChildren) "." "INBOX"'],
        lsub=[b'(\\Subscribed) "." "INBOX"'])
    catalog = mb.MailboxCatalog.from_imap(fake, include_lsub=True)
    entry = catalog.entries()[0]
    assert "\\Subscribed" in entry.flags


def test_catalog_from_imap_rejects_failed_list():
    class Failing(FakeIMAP):
        def list(self, directory='""', pattern="*"):
            return "BAD", [b"no"]

    with pytest.raises(mb.MailboxCatalogError):
        mb.MailboxCatalog.from_imap(Failing(b"* NAMESPACE NIL NIL NIL", []))


def test_catalog_export_is_unicode_and_json_safe():
    catalog = mb.MailboxCatalog()
    catalog.add(b"INBOX.&AOQ-")
    catalog.add(b"bad\xffname")
    payload = json.dumps(catalog.export_dicts())
    assert "\\u00e4" in payload
    assert all(isinstance(item["directory_id"], str)
               for item in json.loads(payload))


# ------------------------------------------------------- connection helpers


def make_client(utf8_enabled=False, namespaces=None, listing=None):
    """Create an IMAP4 instance without opening a socket."""

    from imaplib2 import IMAP4

    client = object.__new__(IMAP4)
    client.utf8_enabled = utf8_enabled
    client.calls = []

    def select(mailbox="INBOX", readonly=False, **kw):
        client.calls.append(("select", mailbox, readonly))
        return "OK", [b"0"]

    def append(mailbox, flags, date_time, message, **kw):
        client.calls.append(("append", mailbox))
        return "OK", [b"appended"]

    def namespace(**kw):
        return "OK", [namespaces or b'* NAMESPACE (("" ".")) NIL NIL']

    def list_(directory='""', pattern="*", **kw):
        return "OK", list(listing or [])

    def simple_command(name, *args, **kw):
        if name == "LIST":
            return "OK", list(listing or [])
        raise AssertionError("unexpected command %r" % name)

    client.select = select
    client.append = append
    client.namespace = namespace
    client.list = list_
    client._simple_command = simple_command
    return client


def test_connection_reports_negotiated_encoding():
    assert make_client().mailbox_encoding() == mb.MODIFIED_UTF7
    assert make_client(utf8_enabled=True).mailbox_encoding() == mb.UTF8


def test_connection_decode_and_encode_follow_negotiation():
    client = make_client()
    assert client.decode_mailbox_name(b"Entw&APw-rfe").text == "Entwürfe"
    assert client.encode_mailbox_name("Entwürfe") == b"Entw&APw-rfe"

    utf8_client = make_client(utf8_enabled=True)
    assert utf8_client.decode_mailbox_name("Entwürfe".encode()).text == "Entwürfe"
    assert utf8_client.encode_mailbox_name("Entwürfe") == "Entwürfe".encode()


def test_select_mailbox_by_directory_id_sends_archived_bytes():
    client = make_client(
        listing=[(b'(\\HasNoChildren) "." {13}', b"INBOX.&AOQ-")])
    catalog = client.mailbox_catalog()
    entry = catalog.entries()[0]
    client.select_mailbox(entry.directory_id, catalog=catalog)
    assert client.calls[-1] == ("select", b"INBOX.&AOQ-", False)


def test_select_mailbox_refuses_conflict_and_unroundtrippable():
    client = make_client()
    catalog = client.mailbox_catalog()
    canonical = catalog.add(b"INBOX.&AOQ-")
    catalog.add(b"INBOX.&AOQ=-")
    with pytest.raises(mb.MailboxConflictError):
        client.select_mailbox("INBOX.ä", catalog=catalog)

    unsafe = mb.MailboxCatalog()
    bad = unsafe.add(b"&AGE-")
    with pytest.raises(mb.MailboxNotAddressableError):
        make_client().select_mailbox("a", catalog=unsafe)
    # Identity still works for the unsafe name.
    make_client().select_mailbox(bad.directory_id, catalog=unsafe)


def test_select_mailbox_without_catalog_requires_exact_protocol_name():
    client = make_client()
    client.select_mailbox(b"INBOX.&AOQ-")
    assert client.calls[-1] == ("select", b"INBOX.&AOQ-", False)
    with pytest.raises(mb.MailboxCatalogError):
        make_client().select_mailbox("INBOX.ä")


def test_append_to_uses_archived_protocol_name():
    client = make_client()
    catalog = client.mailbox_catalog()
    entry = catalog.add(b"INBOX.&AOQ-")
    client.append_to(entry.directory_id, None, None, b"msg", catalog=catalog)
    assert client.calls[-1] == ("append", b"INBOX.&AOQ-")


def test_list_decoded_keeps_delimiter_and_text():
    client = make_client(listing=[
        b'(\\HasNoChildren) "." "INBOX.Sent"',
        (b'(\\HasNoChildren) "/" {7}', b"Other/x"),
    ])
    typ, names = client.list_decoded()
    assert typ == "OK"
    assert names[0].text == "INBOX.Sent"
    assert names[0].delimiter == "."
    assert names[1].text == "Other/x"
    assert names[1].segments() == ("Other", "x")


def test_namespace_parsed_returns_namespace_set():
    typ, parsed = make_client().namespace_parsed()
    assert typ == "OK"
    assert parsed.personal[0].delimiter == "."


# --------------------------------------------------------------- restoring


def test_restore_catalog_rebuilds_identity_and_bytes():
    namespaces = mb.parse_namespace_response(
        ns_response(personal=b'(("" "."))'))
    original = mb.MailboxCatalog(namespaces)
    first = original.add(b"INBOX.&AOQ-")
    second = original.add(b"bad\xffname")
    original.add(b"INBOX.&AOQ=-")

    restored = mb.restore_catalog(original.export_dicts(),
                                  namespaces=namespaces)
    assert {entry.directory_id for entry in restored} \
        == {entry.directory_id for entry in original}
    assert restored.protocol_name(first.directory_id) == b"INBOX.&AOQ-"
    assert restored.protocol_name(second.directory_id) == b"bad\xffname"
    assert restored.conflicts()


def test_restore_catalog_rejects_tampered_directory_id():
    catalog = mb.MailboxCatalog()
    entry = catalog.add(b"INBOX")
    records = [dict(catalog.export_dicts()[0])]
    records[0]["raw_hex"] = b"Other".hex()
    with pytest.raises(mb.MailboxCatalogError):
        mb.restore_catalog(records)
    assert records[0]["directory_id"] == entry.directory_id


def test_restore_catalog_verifies_text_matches_bytes():
    record = {
        "directory_id": "anything",
        "name": "wrong",
        "namespace": "personal",
        "namespace_prefix": "",
        "delimiter": ".",
        "encoding": "modified-utf7",
        "raw_hex": b"INBOX.Sent".hex(),
    }
    with pytest.raises(mb.MailboxCatalogError):
        mb.restore_catalog([record], verify=True)
    restored = mb.restore_catalog([record], verify=False)
    assert restored.entries()[0].raw == b"INBOX.Sent"


def test_identity_includes_encoding_convention():
    utf7 = mb.MailboxCatalog(encoding=mb.MODIFIED_UTF7).add(b"INBOX")
    utf8 = mb.MailboxCatalog(encoding=mb.UTF8).add(b"INBOX")
    assert utf7.directory_id != utf8.directory_id


def test_identity_follows_wire_name_not_namespace_label():
    catalog = mb.MailboxCatalog()
    personal = catalog.add(b"X", namespace="personal")
    other = catalog.add(b"X", namespace="other")
    # The same wire name is the same mailbox; the namespace is metadata.
    assert personal is other
    assert personal.directory_id == other.directory_id


def test_distinct_wire_names_never_share_an_identity():
    catalog = mb.MailboxCatalog()
    first = catalog.add(b"INBOX.&AOQ-")
    second = catalog.add(b"INBOX.&AOQ=-")
    assert first.directory_id != second.directory_id


def test_empty_mailbox_name_is_archived_and_addressable():
    catalog = mb.MailboxCatalog()
    entry = catalog.add(b"")
    assert entry.text == ""
    assert entry.round_trip
    assert entry.segments == ()
    assert catalog.protocol_name_for_text("") == b""


def test_add_rejects_unicode_text_with_a_clear_error():
    with pytest.raises(mb.MailboxNameError):
        mb.MailboxCatalog().add("Entwürfe")
    # Callers must encode through the connection convention.
    catalog = mb.MailboxCatalog()
    assert catalog.add(mb.encode_mailbox_name("Entwürfe")).text == "Entwürfe"
