"""Offline replay harness for imaplib2 tests.

Replays scripted IMAP4 sessions through a real ``IMAP4`` client over an
in-memory socket pair - no network involved. The scripted server accepts
commands in parallel and answers with tagged replies interleaved with
unsolicited (unowned) events, so tests can assert that:

- every call issued during a replay reaches a terminal state,
- events owned by no pending command are stored separately,
- a callback completes only its own request,
- one failing command does not swallow other in-flight replies,
- stale tags (eg: replayed after a reconnect) are never claimed.
"""

import socket
import threading

from imaplib2.imaplib2 import IMAP4


class ReplayServer(threading.Thread):
    """Scripted in-memory IMAP4 server.

    Rules are matched against the command text following the tag:

        server.rule(b'NOOP',
                    untagged=[b'* 3 EXISTS'],        # sent on receipt
                    completion=b'OK NOOP completed', # tagged completion
                    defer_until=2)                   # hold completion until
                                                     # 2 commands were seen

    Unsolicited events are injected after N commands were received:

        server.inject(b'* 4 EXISTS', after=2)
    """

    def __init__(self, welcome=b'* OK replay server ready'):
        threading.Thread.__init__(self)
        self.daemon = True
        self.welcome = welcome
        self.left, self.right = socket.socketpair()
        self.rules = []
        self.events = []
        self.received = []      # Command lines seen since last reset()
        self._deferred = []     # Completions held back by defer_until
        self._send_lock = threading.Lock()

    def rule(self, prefix, untagged=(), completion=b'OK completed', defer_until=1):
        self.rules.append((prefix, list(untagged), completion, defer_until))

    def inject(self, line, after=1):
        self.events.append((after, line))

    def reset(self):
        # Restart command counting, eg: after client setup traffic.
        self.received = []

    def run(self):
        self._send(self.welcome)
        buf = b''
        while True:
            try:
                data = self.right.recv(4096)
            except OSError:
                break
            if not data:
                break
            buf += data
            while b'\r\n' in buf:
                line, buf = buf.split(b'\r\n', 1)
                self._dispatch(line)

    def _dispatch(self, line):
        self.received.append(line)
        count = len(self.received)
        tag, _, rest = line.partition(b' ')
        for prefix, untagged, completion, defer_until in self.rules:
            if rest.startswith(prefix):
                for uline in untagged:
                    self._send(uline)
                if count >= defer_until:
                    self._send(tag + b' ' + completion)
                else:
                    self._deferred.append((defer_until, tag, completion))
                break
        for after, eline in self.events:
            if after == count:
                self._send(eline)
        if self._deferred:
            pending = []
            for need, dtag, dcompletion in self._deferred:
                if count >= need:
                    self._send(dtag + b' ' + dcompletion)
                else:
                    pending.append((need, dtag, dcompletion))
            self._deferred = pending

    def _send(self, line):
        with self._send_lock:
            try:
                self.right.sendall(line + b'\r\n')
            except OSError:
                pass


class ReplayClient(IMAP4):
    """IMAP4 client wired to a ReplayServer instead of the network."""

    def __init__(self, server, **kw):
        self._replay_server = server
        kw.setdefault('host', 'replay')
        IMAP4.__init__(self, **kw)

    def open(self, host=None, port=None):
        self.host = self._choose_nonull_or_dflt('replay', host)
        self.port = self._choose_nonull_or_dflt(0, port)
        self.sock = self._replay_server.left
        self.read_fd = self.sock.fileno()


class CallTracker:
    """Records the terminal state of every call issued during a replay.

    Each recorded outcome is either
        ('completed', typ, data, cb_arg)
    or, when the call failed/was aborted,
        ('error', exception_class, reason, cb_arg).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._events = {}
        self.records = {}
        self.order = []

    def callback(self, name):
        event = threading.Event()
        with self._lock:
            self._events[name] = event

        def _callback(arg):
            result, cb_arg, error = arg
            if error is not None:
                outcome = ('error', error[0], error[1], cb_arg)
            else:
                typ, dat = result
                outcome = ('completed', typ, dat, cb_arg)
            with self._lock:
                self.records[name] = outcome
                self.order.append(name)
            event.set()
        return _callback

    def done(self, name):
        event = self._events.get(name)
        return event is not None and event.is_set()

    def wait(self, name, timeout=10):
        event = self._events.get(name)
        return event is not None and event.wait(timeout)

    def outcome(self, name):
        return self.records[name]
