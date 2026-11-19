"""Protocol-level tests for IDLE DONE handshaking and untagged ordering.

These drive the real :class:`imaplib2.IMAP4` response parser directly, so
they cover ``_put_response``, the untagged observer and the IDLE state
machine instead of only the collector's bookkeeping.
"""

from __future__ import annotations

import io
import re
import threading
import time

import pytest

from imaplib2.imaplib2 import IMAP4


class StubRequest:
    def __init__(self, name, tag=b"A1"):
        self.name = name
        self.tag = tag
        self.delivered = None

    def deliver(self, data):
        self.delivered = data


def make_client():
    client = IMAP4.__new__(IMAP4)
    client.debug = 0
    client.tagpre = b"A"
    client.tagnum = 1
    client.tagre = re.compile(
        br"(?P<tag>A\d+) (?P<type>[A-Z]+) (?P<data>.*)", re.ASCII
    )
    client.tagged_commands = {}
    client.untagged_responses = []
    client.mailbox = "INBOX"
    client.uidvalidity = None
    client.idle_rqb = None
    client.idle_timeout = None
    client.idle_done_rqb = None
    client.idle_phase = None
    client.idle_tag = None
    client.idle_done_event = threading.Event()
    client.idle_done_error = None
    client.untagged_observer = None
    client.untagged_event_seq = 0
    client._expecting_data = False
    client._expecting_data_len = 0
    client._accumulated_data = []
    client._literal_expected = None
    client.commands_lock = threading.Lock()
    client.idle_lock = threading.Lock()
    client.debug_lock = threading.Lock()
    client.debug_file = io.StringIO()
    client.state_change_free = threading.Event()
    client.mo = None
    client._mode_ascii()
    return client


def push(client, response):
    client._put_response(response + b"\r\n")


def test_observer_records_untagged_responses_in_arrival_order():
    client = make_client()
    seen = []
    client.untagged_observer = seen.append

    push(client, b"* 3 EXISTS")
    push(client, b"* 1 EXPUNGE")
    push(client, b"* 2 FETCH (UID 11 FLAGS (\\Seen))")

    assert [event["seq"] for event in seen] == [1, 2, 3]
    assert [event["type"] for event in seen] == ["EXISTS", "EXPUNGE", "FETCH"]
    assert seen[0]["data"] == b"3"
    assert b"UID 11" in seen[2]["data"]


def test_observer_phase_tracks_idle_window():
    client = make_client()
    seen = []
    client.untagged_observer = seen.append

    client.idle_phase = "idle"
    push(client, b"* 4 EXISTS")
    client.idle_phase = "done_sent"
    push(client, b"* 5 EXISTS")
    client.idle_phase = None
    push(client, b"* 6 EXISTS")

    assert [event["idle_phase"] for event in seen] == [
        "idle",
        "done_sent",
        None,
    ]


def test_uidvalidity_is_tracked_from_untagged_response():
    client = make_client()
    seen = []
    client.untagged_observer = seen.append

    push(client, b"* OK [UIDVALIDITY 42] UIDs valid")

    assert client.uidvalidity == 42
    assert [event["type"] for event in seen] == ["OK", "UIDVALIDITY"]
    assert seen[1]["data"] == b"42"


def test_observer_exception_cannot_break_protocol_processing():
    client = make_client()

    def broken(event):
        raise RuntimeError("observer failed")

    client.untagged_observer = broken
    push(client, b"* 7 EXISTS")

    assert [typ for typ, _ in client.untagged_responses] == ["EXISTS"]


def test_idle_done_wait_returns_only_after_tagged_completion():
    client = make_client()
    request = StubRequest("IDLE", b"A9")
    client.tagged_commands[b"A9"] = request
    client.idle_phase = "done_sent"
    client.idle_done_rqb = request
    client.idle_tag = b"A9"
    client.idle_done_event.clear()

    assert client.wait_idle_done(0.01) is False
    assert client.idle_phase == "done_sent"

    push(client, b"A9 OK IDLE terminated")

    assert client.wait_idle_done(0.01) is True
    assert client.idle_phase is None
    assert client.idle_done_rqb is None
    assert request.delivered[0] == "OK"


def test_end_idle_sends_done_and_requires_acknowledgement():
    client = make_client()
    request = StubRequest("IDLE", b"A9")
    sent = []

    class OutQueue:
        def put(self, item):
            sent.append(item)

    client.idle_rqb = request
    client.idle_phase = "idle"
    client.idle_tag = b"A9"
    client.idle_timeout = time.time() + 30
    client.ouq = OutQueue()

    assert client.end_idle(timeout=0.01) is False
    assert sent == [request]
    assert request.data == b"DONE\r\n"
    assert client.idle_phase == "done_sent"

    client.tagged_commands[b"A9"] = request
    push(client, b"A9 OK IDLE terminated")
    assert client.idle_phase is None


def test_connection_termination_wakes_done_wait_with_error():
    client = make_client()
    client.idle_phase = "idle"
    client.idle_done_event.clear()
    client.idle_done_error = "connection terminated before IDLE DONE completed"
    client.idle_done_event.set()

    with pytest.raises(IMAP4.abort):
        client.wait_idle_done(0.01)


def test_query_fetch_responses_are_also_observable_with_pending_context():
    client = make_client()
    seen = []
    client.untagged_observer = seen.append
    client.tagged_commands[b"A2"] = StubRequest("UID", b"A2")

    push(client, b"* 1 FETCH (UID 10 FLAGS (\\Seen))")

    event = seen[0]
    assert event["type"] == "FETCH"
    assert b"UID 10" in event["data"]
    assert event["pending_names"] == ("UID",)
