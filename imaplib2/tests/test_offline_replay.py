"""Offline replay tests: interleaved tagged replies and unsolicited events.

Each test replays a scripted session through a real IMAP4 client (see
replay.py) and checks that every call reaches a terminal state, unowned
events are stored separately, callbacks complete only their own request,
one failing command does not swallow other in-flight replies, and stale
tags from before a reconnect are never claimed.
"""

import time

import pytest

import imaplib2.imaplib2 as imaplib2_module
from replay import CallTracker, ReplayClient, ReplayServer


def wait_for(cond, timeout=10, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


def make_session():
    server = ReplayServer()
    server.rule(b'CAPABILITY',
                untagged=[b'* CAPABILITY IMAP4REV1'],
                completion=b'OK CAPABILITY completed')
    server.rule(b'LOGOUT',
                untagged=[b'* BYE logging out'],
                completion=b'OK LOGOUT completed')
    server.start()
    client = ReplayClient(server)
    server.reset()
    return server, client


@pytest.fixture
def session():
    server, client = make_session()
    yield server, client
    try:
        client.logout()
    except Exception:
        pass
    server.right.close()
    server.join(timeout=5)


def login_and_select(server, client):
    server.rule(b'LOGIN', completion=b'OK LOGIN completed')
    typ, _dat = client.login('user', 'pass')
    assert typ == 'OK'
    server.rule(b'SELECT',
                untagged=[b'* FLAGS (\\Seen \\Flagged)',
                          b'* 3 EXISTS',
                          b'* 1 RECENT',
                          b'* OK [UIDVALIDITY 12345]'],
                completion=b'OK SELECT completed')
    typ, dat = client.select('INBOX')
    assert typ == 'OK'
    assert dat == [b'3']
    server.reset()


def test_parallel_replies_interleaved_with_events_reach_terminal_state(session):
    server, client = session
    login_and_select(server, client)

    # Backend accepts all three commands before answering any of them;
    # tagged replies come back interleaved with an unsolicited event.
    server.rule(b'SEARCH',
                untagged=[b'* SEARCH 1 2'],
                completion=b'OK SEARCH completed',
                defer_until=3)
    server.rule(b'FETCH',
                untagged=[b'* 1 FETCH (FLAGS (\\Seen))'],
                completion=b'OK FETCH completed',
                defer_until=2)
    server.rule(b'NOOP', completion=b'OK NOOP completed')
    server.inject(b'* 4 EXISTS', after=2)

    tracker = CallTracker()
    client.search(None, 'ALL',
                  callback=tracker.callback('search'), cb_arg='search-arg')
    client.fetch('1', '(FLAGS)',
                 callback=tracker.callback('fetch'), cb_arg='fetch-arg')
    client.noop(callback=tracker.callback('noop'), cb_arg='noop-arg')

    # Every call reaches a terminal state.
    for name in ('search', 'fetch', 'noop'):
        assert tracker.wait(name), 'no terminal state for %s' % name

    assert tracker.outcome('search') == ('completed', 'OK', [b'1 2'], 'search-arg')
    assert tracker.outcome('fetch') == ('completed', 'OK', [b'1 (FLAGS (\\Seen))'], 'fetch-arg')
    assert tracker.outcome('noop') == ('completed', 'OK', [b'NOOP completed'], 'noop-arg')

    # Each callback fired exactly once, for its own request only.
    for name in ('search', 'fetch', 'noop'):
        assert tracker.order.count(name) == 1

    # The unsolicited event was attributed to no command; it is stored.
    typ, dat = client.response('EXISTS')
    assert dat == [b'4']


def test_unowned_events_stored_separately(session):
    server, client = session
    server.rule(b'NOOP', completion=b'OK NOOP completed', defer_until=2)

    tracker = CallTracker()
    client.noop(callback=tracker.callback('noop'))

    # Replay events owned by no pending command.
    client.inq.put(b'ZZZ9 OK stale session reply\r\n')          # foreign tag
    client.inq.put(client.tagpre + b'999 OK never issued\r\n')  # unknown own tag
    client.inq.put(b'+ stray continuation\r\n')                 # unasked continuation

    assert wait_for(lambda: len(client.unexpected_responses) == 3)
    unexpected = list(client.pop_unexpected_responses())
    assert [typ for typ, _dat in unexpected] == ['UNEXPECTED', 'TAGGED', 'CONTINUATION']
    assert unexpected[0][1] == b'ZZZ9 OK stale session reply'
    assert unexpected[1][1] == client.tagpre + b'999 OK never issued'
    assert unexpected[2][1] == b'+ stray continuation'

    # None of them completed the in-flight request.
    assert not tracker.done('noop')

    # A second command lets the deferred reply through; both complete.
    client.noop(callback=tracker.callback('noop2'))
    assert tracker.wait('noop')
    assert tracker.wait('noop2')
    assert tracker.outcome('noop')[1] == 'OK'

    # A duplicate reply for an already completed tag is also unowned.
    completed_tag = server.received[0].split(b' ')[0]
    client.inq.put(completed_tag + b' OK duplicate reply\r\n')
    assert wait_for(lambda: len(client.unexpected_responses) == 1)
    typ, dat = list(client.pop_unexpected_responses())[0]
    assert typ == 'TAGGED'
    assert dat == completed_tag + b' OK duplicate reply'


def test_callback_completes_only_its_own_request(session):
    server, client = session
    server.rule(b'NOOP', completion=b'OK NOOP completed')

    tracker = CallTracker()

    def boom(arg, _record=tracker.callback('boom')):
        _record(arg)
        raise RuntimeError('callback failure')

    client.noop(callback=boom, cb_arg='boom-arg')
    client.noop(callback=tracker.callback('healthy'), cb_arg='healthy-arg')

    assert tracker.wait('boom')
    assert tracker.wait('healthy')

    # The failing callback terminated only its own request.
    assert tracker.outcome('boom')[3] == 'boom-arg'
    assert tracker.outcome('healthy') == ('completed', 'OK', [b'NOOP completed'], 'healthy-arg')
    assert tracker.order.count('healthy') == 1

    # The handler survived: synchronous commands still work.
    typ, _dat = client.noop()
    assert typ == 'OK'


def test_failing_command_does_not_swallow_inflight_replies(session):
    server, client = session

    # All three completions are released together; one is NO, one is BAD.
    server.rule(b'NOOP', completion=b'NO noop rejected', defer_until=3)
    server.rule(b'CAPABILITY',
                untagged=[b'* CAPABILITY IMAP4REV1'],
                completion=b'OK CAPABILITY completed',
                defer_until=3)
    server.rule(b'ID',
                untagged=[b'* ID NIL'],
                completion=b'BAD id broken',
                defer_until=3)

    tracker = CallTracker()
    client.noop(callback=tracker.callback('noop'))
    client.capability(callback=tracker.callback('capability'))
    client.id(callback=tracker.callback('id'))

    for name in ('noop', 'capability', 'id'):
        assert tracker.wait(name), 'in-flight reply for %s was swallowed' % name

    # NO is a plain terminal state for that call alone.
    assert tracker.outcome('noop')[0:2] == ('completed', 'NO')
    # The sibling command still got its own OK reply.
    assert tracker.outcome('capability') == ('completed', 'OK', [b'IMAP4REV1'], None)
    # BAD aborts only its own request.
    outcome = tracker.outcome('id')
    assert outcome[0] == 'error'
    assert outcome[1] is client.error


def test_reconnected_session_does_not_claim_stale_tags(session):
    server1, client1 = session

    randint = imaplib2_module.random.randint
    try:
        # A reconnect gets a new tag prefix; force distinct, deterministic ones.
        imaplib2_module.random.randint = lambda a, b: 40000
        server2, client2 = make_session()
    finally:
        imaplib2_module.random.randint = randint

    try:
        assert client1.tagpre != client2.tagpre

        server1.rule(b'NOOP', completion=b'OK NOOP completed')
        typ, _dat = client1.noop()
        assert typ == 'OK'
        stale_tag = server1.received[-1].split(b' ')[0]

        server2.rule(b'NOOP', completion=b'OK NOOP completed', defer_until=2)
        tracker = CallTracker()
        client2.noop(callback=tracker.callback('noop'))

        # An event replayed from the old session must not complete the
        # pending request, even though it is a well-formed tagged reply.
        client2.inq.put(stale_tag + b' OK stale replay\r\n')
        # Same for a tag of this session that was completed long ago.
        client2.inq.put(client2.tagpre + b'0 OK old capability reply\r\n')

        assert wait_for(lambda: len(client2.unexpected_responses) == 2)
        assert not tracker.done('noop')

        client2.noop(callback=tracker.callback('noop2'))
        assert tracker.wait('noop')
        assert tracker.wait('noop2')
        assert tracker.outcome('noop') == ('completed', 'OK', [b'NOOP completed'], None)

        unexpected = list(client2.pop_unexpected_responses())
        assert [typ for typ, _dat in unexpected] == ['UNEXPECTED', 'TAGGED']
        assert unexpected[0][1] == stale_tag + b' OK stale replay'
        assert unexpected[1][1] == client2.tagpre + b'0 OK old capability reply'
    finally:
        try:
            client2.logout()
        except Exception:
            pass
        server2.right.close()
        server2.join(timeout=5)


def test_pending_calls_aborted_on_connection_loss(session):
    server, client = session
    server.rule(b'NOOP', completion=b'OK NOOP completed', defer_until=99)

    tracker = CallTracker()
    client.noop(callback=tracker.callback('pending'))

    # Backend drops the connection without answering.
    server.right.close()

    # The in-flight call still reaches a terminal state.
    assert tracker.wait('pending', timeout=15)
    outcome = tracker.outcome('pending')
    assert outcome[0] == 'error'
    assert outcome[1] is client.abort

    client.logout()
