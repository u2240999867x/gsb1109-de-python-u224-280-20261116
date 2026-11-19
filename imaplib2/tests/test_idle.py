"""Tests for resumable IDLE collection and UID-domain reconciliation."""

from __future__ import annotations

import threading
import time

import pytest

from imaplib2.idle import (
    IdleCollector,
    Snapshot,
    parse_fetch,
    parse_notification,
)


def fetch_line(seq: int, uid: int, flags: str = "") -> bytes:
    return b'%d (UID %d FLAGS (%s))' % (seq, uid, flags.encode())


class FakeIMAP:
    """Minimal IMAP client double.

    Scripted notifications are delivered by ``idle()`` itself, as they would
    arrive during a listening window.  ``idle()`` then waits for ``end_idle()``
    before returning, mimicking the tagged DONE handshake.
    """

    def __init__(self, uidvalidity=7, messages=None):
        self.uidvalidity = uidvalidity
        self.messages = dict(messages or {})       # uid -> flags string
        self.sequence_order = sorted(self.messages)
        self.next_uid = max(self.messages, default=0) + 1
        self.idle_entered = threading.Event()
        self.idle_release = threading.Event()
        self.done_count = 0
        self.done_ok = True
        self.script = []
        self.select_calls = []
        self.fetch_calls = 0
        self.on_fetch = None
        self.fail_next_fetch = None
        self.timeout = None
        self.observer = None

    # -- IMAP-like surface ------------------------------------------------

    def select(self, mailbox="INBOX"):
        self.select_calls.append(mailbox)
        return "OK", [str(len(self.messages)).encode()]

    def response(self, code):
        if code.upper() == "UIDVALIDITY":
            return "UIDVALIDITY", [str(self.uidvalidity).encode()]
        return code, [None]

    def idle(self, timeout=None):
        self.idle_entered.set()
        self.timeout = timeout
        script, self.script = self.script, []
        for typ, data in script:
            self.change(typ, data)
        if script:
            self.idle_release.wait(0.5)
            self.idle_release.clear()
        return "OK", [b"Idle completed"]

    def end_idle(self, timeout=None):
        self.done_count += 1
        self.idle_release.set()
        return self.done_ok

    def uid(self, command, *args, **kwargs):
        assert command.upper() == "FETCH"
        assert args[0] == "1:*"
        return self.fetch_directory()

    def fetch_directory(self):
        self.fetch_calls += 1
        if self.fail_next_fetch is not None:
            error, self.fail_next_fetch = self.fail_next_fetch, None
            raise error
        result = [
            fetch_line(seq, uid, self.messages[uid])
            for seq, uid in enumerate(self.sequence_order, 1)
            if uid in self.messages
        ]
        if self.on_fetch is not None:
            self.on_fetch(self.fetch_calls)
        return "OK", result

    # -- test controls ----------------------------------------------------

    def change(self, typ, data, phase="idle"):
        if self.observer is not None:
            self.observer({
                "type": typ,
                "data": data,
                "mailbox": "INBOX",
                "uidvalidity": self.uidvalidity,
                "idle_phase": phase,
                "pending_names": (),
            })

    def add(self, flags="", seq=None, notify=True):
        uid = self.next_uid
        self.next_uid += 1
        self.messages[uid] = flags
        if seq is None:
            self.sequence_order.append(uid)
        else:
            self.sequence_order.insert(seq - 1, uid)
        if notify:
            self.script.append(("EXISTS", str(len(self.sequence_order)).encode()))
        return uid


class SyncFakeIMAP(FakeIMAP):
    """Fake where IDLE returns as soon as its scripted change is delivered."""

    def idle(self, timeout=None):
        script, self.script = self.script, []
        for typ, data in script:
            self.change(typ, data)
        return "OK", [b"Idle completed"]

    def end_idle(self, timeout=None):
        self.done_count += 1
        return self.done_ok


def make_collector(imap, **kwargs):
    collector = IdleCollector(imap, "INBOX", auto_select=True, **kwargs)
    imap.observer = collector._observe
    collector._ensure_selected()
    return collector


def test_parse_fetch_uses_uid_not_sequence():
    parsed = parse_fetch(b'12 (UID 99 FLAGS (\\Seen) RFC822.SIZE 42)')
    assert parsed["sequence_hint"] == 12
    assert parsed["uid"] == 99
    assert parsed["flags"] == frozenset({"\\Seen"})
    assert parsed["size"] == 42


def test_parse_notification_never_aliases_expunge_sequence_to_uid():
    parsed = parse_notification("EXPUNGE", b"3")
    assert parsed["sequence_hint"] == 3
    assert parsed["uid"] is None


def test_parse_notification_records_vanished_uids():
    parsed = parse_notification("VANISHED", b"4,7:9")
    assert parsed["uids"] == (4, 7, 8, 9)


def test_synchronize_publishes_only_complete_uid_directory():
    imap = FakeIMAP(messages={10: "", 11: "\\Seen"})
    collector = make_collector(imap)

    snapshot = collector.synchronize()

    assert snapshot is not None
    assert snapshot.complete
    assert snapshot.uids == (10, 11)
    assert snapshot.uidvalidity == 7
    assert snapshot[11].flags == frozenset({"\\Seen"})
    assert collector.complete


def test_sequence_number_movement_does_not_create_messages():
    imap = FakeIMAP(messages={10: "", 11: ""})
    collector = make_collector(imap)
    collector.synchronize()

    # First message expunged; the surviving message is now sequence 1 but its
    # UID is unchanged.
    imap.messages.pop(10)
    imap.sequence_order.remove(10)
    imap.change("EXPUNGE", b"1")
    imap.change("EXISTS", b"1")

    collector.listen()
    snapshot = collector._catch_up("W1", phase="after_idle")

    assert snapshot.uids == (11,)
    assert len(imap.messages) == 1


def test_change_during_catchup_prevents_publishing_stale_result():
    imap = FakeIMAP(messages={10: ""})
    collector = make_collector(imap)
    collector.synchronize()

    raced = []

    def race_on_first_fetch(call):
        if not raced:
            raced.append(call)
            imap.messages[11] = ""
            imap.sequence_order.append(11)
            imap.change("EXISTS", b"2")

    imap.on_fetch = race_on_first_fetch

    snapshot = collector._catch_up(None, phase="sync")

    assert snapshot is not None
    assert snapshot.uids == (10, 11)
    first, second = collector.queries[-2:]
    assert first.superseded
    assert first.published_generation is None
    assert not second.superseded
    assert second.published_generation == snapshot.generation
    # Only the converging result was published.
    snapshots = [event for event in collector.replay()
                 if event["kind"] == "snapshot"]
    assert len(snapshots) == 2  # initial + converged
    assert snapshots[-1]["uids"] == (10, 11)


def test_catchup_that_never_quiets_opens_gap_and_publishes_nothing():
    imap = FakeIMAP(messages={10: ""})
    collector = make_collector(imap)
    initial = collector.synchronize()

    def always_race(call):
        imap.messages[100 + call] = ""
        imap.sequence_order.append(100 + call)
        imap.change("EXISTS", str(len(imap.sequence_order)).encode())

    imap.on_fetch = always_race

    result = collector._catch_up(None, phase="sync")

    assert result is None
    # The last directory stays readable, but no stale query is presented as
    # the newest complete catalogue.
    assert collector.snapshot.uids == initial.uids
    assert collector.snapshot.complete is False
    assert collector.snapshot.generation == initial.generation
    assert any("did not converge" in gap.reason for gap in collector.gaps)
    assert all(
        query.published_generation is None
        for query in collector.queries
        if query.superseded
    )


def test_unknown_sequence_numbers_do_not_become_message_identity():
    imap = FakeIMAP(messages={10: ""})
    collector = make_collector(imap)
    collector.synchronize()

    collector.feed("EXPUNGE", b"1")
    notification = collector.notifications[-1]

    assert notification.sequence_hint == 1
    assert notification.uid is None
    assert collector.snapshot.uids == (10,)


def test_uidvalidity_change_clears_directory_and_records_gap():
    imap = FakeIMAP(uidvalidity=7, messages={10: "", 11: ""})
    collector = make_collector(imap)
    collector.synchronize()

    imap.uidvalidity = 8
    imap.messages = {50: ""}
    imap.sequence_order = [50]
    collector.feed("UIDVALIDITY", b"8")
    collector.listen()
    snapshot = collector._catch_up("W1", phase="after_idle")

    assert any("UIDVALIDITY" in gap.reason for gap in collector.gaps)
    assert snapshot.uidvalidity == 8
    assert snapshot.uids == (50,)


def test_mailbox_switch_records_gap_between_domains():
    imap = FakeIMAP(uidvalidity=7, messages={10: ""})
    collector = make_collector(imap)
    collector.synchronize()

    imap.uidvalidity = 12
    imap.messages = {70: ""}
    imap.sequence_order = [70]
    snapshot = collector.switch_mailbox("Archive")

    assert [gap for gap in collector.gaps
            if "mailbox switch" in gap.reason]
    assert snapshot.mailbox == "Archive"
    assert snapshot.uidvalidity == 12
    assert snapshot.uids == (70,)
    assert collector.snapshot.uids == (70,)


def test_done_timeout_records_gap_and_does_not_publish():
    imap = FakeIMAP(messages={10: ""})
    collector = make_collector(imap)
    initial = collector.synchronize()

    imap.done_ok = False
    imap.messages[11] = ""
    imap.sequence_order.append(11)
    imap.script.append(("EXISTS", b"2"))

    window = collector.listen()
    assert window.reason == "done_timeout"
    assert any("DONE handshake" in gap.reason for gap in collector.gaps)
    assert collector.snapshot.uids == initial.uids
    assert collector.snapshot.complete is False


def test_replay_places_each_notification_in_its_listening_window():
    imap = FakeIMAP(messages={10: ""})
    collector = make_collector(imap)
    collector.synchronize()

    imap.add(notify=False)
    imap.script.append(("EXISTS", b"2"))
    collector.listen()
    collector._catch_up("W1", phase="after_idle")

    imap.add(notify=False)
    imap.script.append(("FETCH", b'2 (UID 12 FLAGS (\\Seen))'))
    collector.listen()
    collector._catch_up("W2", phase="after_idle")

    windows = {window.id: window for window in collector.windows}
    assert windows["W1"].start_seq == 1
    assert windows["W1"].end_seq == 1
    assert windows["W1"].notifications == 1
    assert windows["W2"].start_seq == windows["W1"].end_seq + 1
    assert windows["W2"].end_seq == 2
    assert windows["W2"].notifications == 1
    assert [n.window for n in collector.notifications] == ["W1", "W2"]
    assert [n.seq for n in collector.notifications] == [1, 2]

    replay = collector.format_replay()
    assert "WINDOW W1 start" in replay
    assert "NOTIFY seq=1 type=EXISTS window=W1" in replay
    assert "NOTIFY seq=2 type=FETCH window=W2" in replay
    assert "QUERY Q" in replay


def test_every_query_has_a_recorded_uid_range():
    imap = FakeIMAP(messages={10: "", 11: ""})
    collector = make_collector(imap)
    collector.synchronize()

    query = collector.queries[-1]
    assert query.requested_range == "1:*"
    assert query.covered_range == "10:11"
    assert query.uids == (10, 11)
    assert query.ok
    assert not query.superseded
    assert collector.was_uid_queried(11)
    assert not collector.was_uid_queried(99)


def test_repeated_notifications_do_not_archive_same_uid_twice():
    imap = FakeIMAP(messages={10: ""})
    collector = make_collector(imap)
    collector.synchronize()

    assert collector.archive(10, b"raw-10")
    assert not collector.archive(10, b"raw-10")
    assert collector.archive_duplicates == 1
    assert collector.archived_uids == (10,)

    # Redeliver the same notification and re-run a full query: still one copy.
    imap.script.append(("EXISTS", b"1"))
    collector.listen()
    collector._catch_up("W1", phase="after_idle")

    assert collector.archive_duplicates >= 1
    archive_events = [event for event in collector.replay()
                      if event["kind"] == "archive"]
    firsts = [event for event in archive_events if not event["duplicate"]]
    duplicates = [event for event in archive_events if event["duplicate"]]
    assert len(firsts) == 1
    assert len(duplicates) >= 1


def test_confirmed_uid_material_is_reused_after_reconnect():
    material = {}

    def fetcher(uid, message):
        material.setdefault(uid, b"raw-%d" % uid)
        return material[uid]

    imap = FakeIMAP(messages={10: ""})
    collector = make_collector(imap, fetcher=fetcher)
    collector.synchronize()
    assert collector.archived_uids == (10,)

    imap.fail_next_fetch = RuntimeError("connection reset")
    assert collector._catch_up(None, phase="sync") is None
    assert collector.archived_uids == (10,)

    imap.fail_next_fetch = None
    snapshot = collector._catch_up(None, phase="sync")
    assert snapshot.uids == (10,)
    # Material fetched once and reused; archive stayed idempotent.
    assert not collector.archive(10, b"raw-10")
    archive_events = [event for event in collector.replay()
                      if event["kind"] == "archive"]
    assert len([event for event in archive_events
                if not event["duplicate"]]) == 1


def test_connection_loss_opens_gap_and_reconnect_resyncs():
    imap = FakeIMAP(messages={10: ""})
    replacement = FakeIMAP(messages={10: "", 11: ""})
    reconnects = []

    collector = IdleCollector(
        imap, "INBOX",
        idle_timeout=0.01,
        reconnect=lambda: reconnects.append(1) or replacement,
    )
    collector._on_snapshot = lambda snapshot: (
        collector.stop() if snapshot.mailbox == "INBOX"
        and collector.imap is replacement
        else None
    )
    # The first attempt raises before any listener is established.
    imap.end_idle = lambda timeout=None: True

    def broken_idle(timeout=None):
        raise ConnectionError("socket closed")

    imap.idle = broken_idle
    collector.run()

    assert reconnects == [1]
    assert any("connection lost" in gap.reason for gap in collector.gaps)
    assert replacement.select_calls == ["INBOX"]
    assert replacement.fetch_calls >= 1


def test_collect_once_requires_done_before_catchup():
    imap = SyncFakeIMAP(messages={10: ""})
    imap.script.append(("EXISTS", b"2"))
    imap.add(notify=False)
    collector = make_collector(imap)

    snapshot = collector.collect_once()

    assert reference_uid_ok(snapshot)
    assert imap.done_count >= 1
    assert snapshot.complete


def reference_uid_ok(snapshot: Snapshot) -> bool:
    return snapshot is not None and snapshot.uids == (10, 11)


def test_background_collector_stops_cleanly():
    imap = SyncFakeIMAP(messages={10: ""})
    collector = make_collector(imap)
    collector.start()
    deadline = time.time() + 2
    while collector.snapshot is None and time.time() < deadline:
        time.sleep(0.01)

    imap.add()
    deadline = time.time() + 2
    while 11 not in collector.directory and time.time() < deadline:
        time.sleep(0.01)

    collector.stop(timeout=2)
    assert not collector._thread.is_alive()
    assert 11 in collector.directory


def test_checkpoint_restore_keeps_archive_idempotent_across_restart():
    imap = FakeIMAP(messages={10: "", 11: ""})
    collector = make_collector(imap)
    collector.synchronize()
    assert collector.archive(10, b"raw-10")

    checkpoint = collector.checkpoint()
    assert checkpoint["uidvalidity"] == 7
    assert [7, 10] in checkpoint["archived"]

    restarted = IdleCollector(FakeIMAP(messages={10: "", 11: ""}), "INBOX")
    restarted._uidvalidity = 7
    assert restarted.restore(checkpoint)
    # Restored keys are only for idempotence, not claimed completeness.
    assert not restarted.complete

    assert restarted.archive(10, b"raw-10") is False
    assert restarted.archive(11, b"raw-11") is True


def test_restore_rejects_checkpoint_from_other_uidvalidity_domain():
    imap = FakeIMAP(uidvalidity=9, messages={50: ""})
    collector = make_collector(imap)

    checkpoint = {
        "version": 1,
        "mailbox": "INBOX",
        "uidvalidity": 7,
        "generation": 3,
        "complete": True,
        "uids": [10, 11],
        "archived": [[7, 10]],
        "gaps": [],
        "last_seq": 4,
    }
    assert collector.restore(checkpoint) is False
    assert any("UIDVALIDITY" in gap.reason for gap in collector.gaps)
    assert collector.archive(10, b"raw-10") is True


def test_duplicate_archive_can_fill_empty_material_without_replacing_it():
    imap = FakeIMAP(messages={10: ""})
    collector = make_collector(imap)
    collector.synchronize()

    assert collector.archive(10)
    assert collector.archived[(7, 10)] is None
    assert collector.archive(10, b"raw-10") is False
    assert collector.archived[(7, 10)] == b"raw-10"
    assert collector.archive(10, b"other") is False
    assert collector.archived[(7, 10)] == b"raw-10"


def test_material_attach_does_not_change_archive_identity():
    imap = FakeIMAP(messages={10: ""})
    collector = make_collector(imap)
    collector.synchronize()

    collector.archive(10)
    assert collector.attach_material(10, b"raw-10") is True
    assert collector.attach_material(10, b"other") is False
    assert collector.material_for(10) == b"raw-10"


def test_query_fetch_responses_are_not_recorded_as_notifications():
    imap = FakeIMAP(messages={10: ""})
    collector = make_collector(imap)

    def echo_query(call):
        # A server echo of our own UID FETCH, with a matching flag set.
        collector._observe({
            "type": "FETCH",
            "data": b"1 (UID 10 FLAGS ())",
            "mailbox": "INBOX",
            "uidvalidity": 7,
            "idle_phase": None,
            "pending_names": ("UID",),
        })

    imap.on_fetch = echo_query
    snapshot = collector.synchronize()

    assert snapshot.uids == (10,)
    assert collector.notifications == ()
    assert not collector.queries[-1].superseded


def test_contradictory_flag_echo_makes_scan_stale_until_it_converges():
    imap = FakeIMAP(messages={10: ""})
    collector = make_collector(imap)

    calls = []

    def conflicting_echo(call):
        calls.append(call)
        if len(calls) == 1:
            collector._observe({
                "type": "FETCH",
                "data": b"1 (UID 10 FLAGS (\\Seen))",
                "mailbox": "INBOX",
                "uidvalidity": 7,
                "idle_phase": None,
                "pending_names": ("UID",),
            })
            imap.messages[10] = "\\Seen"

    imap.on_fetch = conflicting_echo
    snapshot = collector.synchronize()

    first, second = collector.queries[-2:]
    assert first.superseded
    assert "flags" in first.reason
    assert not second.superseded
    assert snapshot[10].flags == frozenset({"\\Seen"})


def test_uid_fetch_failure_records_gap_and_keeps_last_snapshot():
    imap = FakeIMAP(messages={10: ""})
    collector = make_collector(imap)
    last = collector.synchronize()

    imap.fail_next_fetch = RuntimeError("UID FETCH failed")
    result = collector.synchronize()

    assert result is None
    assert collector.snapshot.uids == last.uids
    assert collector.snapshot.complete is False
    assert not collector.complete
    assert any("catch-up query failed" in gap.reason for gap in collector.gaps)
    assert collector.queries[-1].error == "UID FETCH failed"


def test_auto_select_false_requires_explicit_attach():
    from imaplib2.idle import CollectorError

    imap = FakeIMAP(messages={10: ""})
    collector = IdleCollector(imap, "INBOX", auto_select=False)
    imap.observer = collector._observe

    with pytest.raises(CollectorError):
        collector.synchronize()

    imap.select("INBOX")     # caller owns selection in this mode
    collector.attach()
    snapshot = collector.synchronize()

    assert snapshot.uids == (10,)
    assert imap.select_calls == ["INBOX"]
