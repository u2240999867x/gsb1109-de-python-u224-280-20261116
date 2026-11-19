"""Tests for RFC 7162 CONDSTORE conditional flag updates.

The network is replaced by a scripted in-memory IMAP object so that the
compare-and-swap semantics, per-UID categorisation, UIDVALIDITY domain checks
and safe-retry behaviour can be exercised deterministically.
"""

import pytest

from imaplib2 import IMAP4
from imaplib2.condstore import (
    ConditionalFlagStore,
    ConditionalStoreUnsupported,
    MailboxDomainError,
    SUCCESS,
    CONFLICT,
    DENIED,
    UNKNOWN,
    conditional_label,
)
from imaplib2.imaplib2 import UNCHANGEDSINCE


class FakeError(Exception):
    pass


class FakeAbort(FakeError):
    pass


class FakeReadonly(FakeAbort):
    pass


class FakeIMAP:
    """Minimal IMAP4 stand-in modelling a CONDSTORE-aware mailbox."""

    error = FakeError
    abort = FakeAbort
    readonly = FakeReadonly

    def __init__(self, supported=True, uidvalidity=42, condstore_enabled=True):
        self.capabilities = ("IMAP4REV1", "CONDSTORE") if supported else ("IMAP4REV1",)
        self.uidvalidity = uidvalidity
        self.highestmodseq = 100
        self.condstore_enabled = condstore_enabled
        self.mailbox = "INBOX"
        # uid -> {"flags": set[bytes], "modseq": int}
        self.messages = {}
        self.calls = []
        self._modified = []
        self.store_mode = "normal"   # normal | deny-no | abort | readonly | drift
        self.select_calls = []

    # -- helpers used by tests to seed mailbox state -----------------------

    def seed(self, uid, flags=(), modseq=1):
        self.messages[int(uid)] = {
            "flags": {f.encode() for f in flags},
            "modseq": int(modseq),
        }

    def condstore_supported(self):
        caps = {str(c).upper() for c in self.capabilities}
        return "CONDSTORE" in caps or "QRESYNC" in caps

    def select(self, mailbox="INBOX", condstore=False, **kw):
        self.select_calls.append((mailbox, condstore))
        self.condstore_enabled = True
        return "OK", [None]

    # -- IMAP command surface ----------------------------------------------

    def uid(self, command, *args):
        self.calls.append(("UID", command) + args)
        command = command.upper()
        if command == "STORE":
            return self._handle_store(args)
        if command == "FETCH":
            return self._handle_fetch(args)
        raise AssertionError("unexpected UID command %r" % command)

    def _handle_store(self, args):
        uid_set, marker, op, flag_list = args
        assert marker.startswith("(%s" % UNCHANGEDSINCE), marker
        assert op == "+FLAGS", "retry must merge flags, never overwrite them"
        since = int(marker.split()[1].rstrip(")"))
        uids = ConditionalFlagStore._parse_uid_set(uid_set)
        self._modified = []
        data = []
        for uid in uids:
            msg = self.messages.get(uid)
            if msg is None:
                self._modified.append(uid)
                continue
            if self.store_mode == "deny-no":
                return "NO", [b"[PERMISSIONDENIED] not allowed to alter flags"]
            if self.store_mode == "abort":
                raise self.abort("socket timeout waiting for tagged response")
            if self.store_mode == "readonly":
                raise self.readonly("mailbox is read-only")
            if self.store_mode == "drift":
                # Another agent keeps mutating the message between our
                # re-query and our conditional STORE, so every marker is stale.
                self.highestmodseq += 1
                msg["modseq"] = self.highestmodseq
            if msg["modseq"] > since:
                self._modified.append(uid)
                data.append(self._fetch_line(uid))
                continue
            for token in flag_list[1:-1].split():
                msg["flags"].add(token.encode())
            self.highestmodseq += 1
            msg["modseq"] = self.highestmodseq
            data.append(self._fetch_line(uid))
        return "OK", data

    def _handle_fetch(self, args):
        uid_set, _parts = args
        uids = ConditionalFlagStore._parse_uid_set(uid_set)
        data = [self._fetch_line(uid) for uid in uids if uid in self.messages]
        return "OK", data

    def _fetch_line(self, uid):
        msg = self.messages[uid]
        flags = b" ".join(sorted(msg["flags"]))
        return (
            b"%d FETCH (UID %d FLAGS (%b) MODSEQ (%d))"
            % (uid, uid, flags, msg["modseq"])
        )

    def response(self, code):
        if code == "MODIFIED" and self._modified:
            payload = ",".join(str(u) for u in self._modified).encode()
            self._modified = []
            return "OK", [b" " + payload]
        return "OK", [None]


# --------------------------------------------------------------------------
# Guarantee / capability handling
# --------------------------------------------------------------------------

def test_unsupported_server_raises_and_never_writes():
    imap = FakeIMAP(supported=False)
    imap.seed(1, modseq=5)
    with pytest.raises(ConditionalStoreUnsupported):
        ConditionalFlagStore(imap).apply({1: 5}, "Archived", read_uidvalidity=42)
    assert imap.calls == []


def test_condstore_capability_suffices_without_reselecting():
    # RFC 7162: a CONDSTORE-capable server honours UNCHANGEDSINCE regardless of
    # whether SELECT carried a CONDSTORE parameter, so the selected mailbox must
    # not be disturbed by a hidden re-select.
    imap = FakeIMAP(supported=True, condstore_enabled=False)
    imap.seed(1, modseq=5)
    result = ConditionalFlagStore(imap).apply(
        {1: 5}, "Archived", read_uidvalidity=42
    )
    assert imap.select_calls == []
    assert result.succeeded == [1]


# --------------------------------------------------------------------------
# UIDVALIDITY domain
# --------------------------------------------------------------------------

def test_stale_uidvalidity_is_refused_before_any_write():
    imap = FakeIMAP(uidvalidity=99)
    imap.seed(1, modseq=5)
    with pytest.raises(MailboxDomainError):
        ConditionalFlagStore(imap).apply({1: 5}, "Archived", read_uidvalidity=42)
    assert imap.calls == []


def test_matching_uidvalidity_proceeds():
    imap = FakeIMAP(uidvalidity=42)
    imap.seed(1, modseq=5)
    result = ConditionalFlagStore(imap).apply(
        {1: 5}, "Archived", read_uidvalidity=42
    )
    assert result.uidvalidity == 42
    assert result.succeeded == [1]


# --------------------------------------------------------------------------
# Per-UID outcome categories
# --------------------------------------------------------------------------

def test_successful_conditional_store_adds_label():
    imap = FakeIMAP()
    imap.seed(1, flags=("\\Seen",), modseq=5)
    result = ConditionalFlagStore(imap).apply(
        {1: 5}, "Archived", read_uidvalidity=42
    )
    assert result.as_dict()[SUCCESS] == [1]
    assert result.conflicts == result.denied == result.unknown == []
    assert b"Archived" in imap.messages[1]["flags"]
    # Wire format: marker between set and +FLAGS, flags parenthesised.
    assert imap.calls == [
        ("UID", "STORE", "1", "(UNCHANGEDSINCE 5)", "+FLAGS", "(Archived)")
    ]


def test_version_conflict_then_retry_with_fresh_modseq():
    imap = FakeIMAP()
    imap.seed(1, flags=(), modseq=7)   # advanced past the read-time modseq
    result = ConditionalFlagStore(imap).apply(
        {1: 5}, "Archived", read_uidvalidity=42
    )
    assert result.succeeded == [1]
    assert result.conflicts == []
    # First attempt uses the stale MODSEQ, the retry the freshly fetched one.
    markers = [call[3] for call in imap.calls if call[1] == "STORE"]
    assert markers == ["(UNCHANGEDSINCE 5)", "(UNCHANGEDSINCE 7)"]
    # Every write merges flags; an unconditional full FLAGS overwrite is forbidden.
    assert all(call[4] == "+FLAGS" for call in imap.calls if call[1] == "STORE")
    assert any(call[1] == "FETCH" for call in imap.calls)


def test_conflict_reported_when_business_condition_no_longer_met():
    imap = FakeIMAP()
    # Someone else already applied the label before our retry.
    imap.seed(1, flags=("Archived",), modseq=7)
    result = ConditionalFlagStore(imap).apply(
        {1: 5}, "Archived", read_uidvalidity=42
    )
    assert result.conflicts == [1]
    assert result.succeeded == []
    stores = [call for call in imap.calls if call[1] == "STORE"]
    assert len(stores) == 1            # no retry write once condition is false


def test_custom_predicate_blocks_retry():
    imap = FakeIMAP()
    imap.seed(1, flags=("\\Flagged",), modseq=7)

    def only_if_unflagged(uid, info):
        return "\\Flagged" not in info["flags"]

    result = ConditionalFlagStore(imap).apply(
        {1: 5}, "Archived", condition=only_if_unflagged, read_uidvalidity=42
    )
    assert result.conflicts == [1]
    assert "no longer met" in result.reasons[1]


def test_expunged_message_ends_as_conflict():
    imap = FakeIMAP()
    imap.seed(1, modseq=7)
    original_fetch = imap._handle_fetch

    def fetch_once(args):
        # Message disappears between the conflict and the re-query.
        imap.messages.pop(1, None)
        return original_fetch(args)

    imap._handle_fetch = fetch_once
    result = ConditionalFlagStore(imap).apply(
        {1: 5}, "Archived", read_uidvalidity=42
    )
    assert result.conflicts == [1]
    assert "no longer present" in result.reasons[1]


def test_denied_no_response():
    imap = FakeIMAP()
    imap.seed(1, modseq=5)
    imap.store_mode = "deny-no"
    result = ConditionalFlagStore(imap).apply(
        {1: 5}, "Archived", read_uidvalidity=42
    )
    assert result.denied == [1]
    assert "PERMISSIONDENIED" in result.reasons[1]


def test_readonly_mailbox_is_denied():
    imap = FakeIMAP()
    imap.seed(1, modseq=5)
    imap.store_mode = "readonly"
    result = ConditionalFlagStore(imap).apply(
        {1: 5}, "Archived", read_uidvalidity=42
    )
    assert result.denied == [1]


def test_connection_failure_is_unknown_not_assumed_success():
    imap = FakeIMAP()
    imap.seed(1, modseq=5)
    imap.store_mode = "abort"
    result = ConditionalFlagStore(imap).apply(
        {1: 5}, "Archived", read_uidvalidity=42
    )
    assert result.unknown == [1]
    assert result.succeeded == result.conflicts == result.denied == []
    assert "timeout" in result.reasons[1]


def test_mixed_batch_buckets_every_uid_once():
    imap = FakeIMAP()
    imap.seed(1, modseq=5)                       # succeeds
    imap.seed(2, modseq=9)                       # conflict then retry success
    imap.seed(3, modseq=5)                       # denied
    imap.seed(4, modseq=5)                       # unknown

    def mode_for(uid):
        return {3: "deny-no", 4: "abort"}.get(uid, "normal")

    original_handle_store = imap._handle_store

    def routed_store(args):
        uid = int(args[0])
        imap.store_mode = mode_for(uid)
        return original_handle_store(args)

    imap._handle_store = routed_store
    result = ConditionalFlagStore(imap).apply(
        {1: 5, 2: 5, 3: 5, 4: 5}, "Archived", read_uidvalidity=42
    )
    assert result.succeeded == [1, 2]
    assert result.denied == [3]
    assert result.unknown == [4]
    assert result.conflicts == []
    assert sorted(result.all_uids()) == [1, 2, 3, 4]


def test_persistent_conflict_exhausts_retries():
    imap = FakeIMAP()
    imap.seed(1, modseq=100)                    # always ahead of any marker
    imap.store_mode = "drift"
    result = ConditionalFlagStore(imap, max_retries=1).apply(
        {1: 5}, "Archived", read_uidvalidity=42
    )
    assert result.conflicts == [1]
    stores = [call for call in imap.calls if call[1] == "STORE"]
    assert len(stores) == 2                     # initial + one retry


def test_conditional_label_wrapper_matches_class_api():
    imap = FakeIMAP()
    imap.seed(1, modseq=5)
    result = conditional_label(
        imap, {1: 5}, "Archived", read_uidvalidity=42
    )
    assert result.succeeded == [1]


def test_read_versions_returns_domain_and_modseqs():
    imap = FakeIMAP()
    imap.seed(1, flags=("\\Seen",), modseq=5)
    imap.seed(2, modseq=8)
    store = ConditionalFlagStore(imap)
    uidvalidity, versions = store.read_versions([1, 2])
    assert uidvalidity == 42
    assert versions == {1: 5, 2: 8}


def test_read_then_apply_workflow_rejects_foreign_domain():
    imap = FakeIMAP(uidvalidity=42)
    imap.seed(1, modseq=5)
    store = ConditionalFlagStore(imap)
    uidvalidity, versions = store.read_versions([1])
    # Mailbox incarnation changes before the write: old MODSEQ must be refused.
    imap.uidvalidity = 99
    with pytest.raises(MailboxDomainError):
        store.apply(versions, "Archived", read_uidvalidity=uidvalidity)


def test_read_then_apply_workflow_succeeds_within_same_domain():
    imap = FakeIMAP(uidvalidity=42)
    imap.seed(1, modseq=5)
    store = ConditionalFlagStore(imap)
    uidvalidity, versions = store.read_versions([1])
    result = store.apply(versions, "Archived", read_uidvalidity=uidvalidity)
    assert result.succeeded == [1]
    assert result.uidvalidity == 42


# --------------------------------------------------------------------------
# Protocol-layer units (no sockets): capability, domain reset, STORE wire args
# --------------------------------------------------------------------------

def _bare_imap4():
    return IMAP4.__new__(IMAP4)


def test_condstore_supported_capability_detection():
    imap = _bare_imap4()
    imap.capabilities = ("IMAP4REV1",)
    assert imap.condstore_supported() is False
    imap.capabilities = ("IMAP4REV1", "condstore")
    assert imap.condstore_supported() is True
    imap.capabilities = ("IMAP4REV1", "QRESYNC")
    assert imap.condstore_supported() is True


def test_uidvalidity_change_invalidates_cached_modseq_domain():
    imap = _bare_imap4()
    imap.uidvalidity = None
    imap.highestmodseq = None
    imap.condstore_enabled = False
    responses = {
        "UIDVALIDITY": [b"42"],
        "HIGHESTMODSEQ": [b"100"],
    }
    imap._get_untagged_response = lambda name, leave=False: responses.get(name)

    imap._sync_mailbox_modseq_state()
    assert imap.uidvalidity == 42
    assert imap.highestmodseq == 100
    assert imap.condstore_enabled is True

    # Same incarnation, MODSEQ advances.
    responses["HIGHESTMODSEQ"] = [b"200"]
    imap._sync_mailbox_modseq_state()
    assert imap.uidvalidity == 42
    assert imap.highestmodseq == 200

    # New incarnation: the old MODSEQ namespace must be discarded, not reused.
    responses["UIDVALIDITY"] = [b"99"]
    responses["HIGHESTMODSEQ"] = [b"3"]
    imap._sync_mailbox_modseq_state()
    assert imap.uidvalidity == 99
    assert imap.highestmodseq == 3


def test_store_emits_unchangedsince_marker_in_rfc7162_position():
    imap = _bare_imap4()
    captured = {}

    def fake_simple_command(name, *args, **kw):
        captured["name"] = name
        captured["args"] = args
        captured["kw"] = kw
        return "OK", []

    imap._simple_command = fake_simple_command
    imap.store("1", "+FLAGS", r"(\Seen)", unchanged_since=7)
    assert captured["name"] == "STORE"
    assert captured["args"] == (
        "1", "(UNCHANGEDSINCE 7)", "+FLAGS", r"(\Seen)"
    )
    assert captured["kw"]["untagged_response"] == "FETCH"


def test_store_without_marker_preserves_legacy_wire_format():
    imap = _bare_imap4()
    captured = {}
    imap._simple_command = lambda name, *a, **k: (
        captured.update(name=name, args=a, kw=k) or ("OK", [])
    )
    imap.store("1", "FLAGS", r"\Seen")
    assert captured["args"] == ("1", "FLAGS", r"(\Seen)")
