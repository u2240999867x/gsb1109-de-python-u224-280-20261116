import socket
import threading
import time

import pytest

from imaplib2 import IMAP4


class PipeIMAP4(IMAP4):
    """IMAP4 client connected to an in-process fake server via socketpair."""

    def __init__(self, sock, **kw):
        self._client_sock = sock
        IMAP4.__init__(self, **kw)

    def open_socket(self):
        return self._client_sock


class FakeIMAPServer:
    """Minimal scripted IMAP4 server exercising SELECT state transitions."""

    def __init__(self, sock):
        self.sock = sock
        self.untagged_sent = threading.Event()
        self.release_ok = threading.Event()
        self.error = None
        self.thread = threading.Thread(target=self._serve)
        self.thread.daemon = True
        self.thread.start()

    def _serve(self):
        try:
            self._send('* OK fake IMAP4rev1 server ready')
            buf = b''
            while True:
                data = self.sock.recv(4096)
                if not data:
                    return
                buf += data
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1)
                    line = line.strip()
                    if line and not self._handle(line.decode('ascii')):
                        return
        except Exception as exc:
            self.error = exc

    def _send(self, data):
        self.sock.sendall(data.encode('ascii') + b'\r\n')

    def _handle(self, line):
        parts = line.split(' ', 2)
        tag = parts[0]
        command = parts[1].upper() if len(parts) > 1 else ''
        args = parts[2] if len(parts) > 2 else ''

        if command == 'CAPABILITY':
            self._send('* CAPABILITY IMAP4REV1')
            self._send('%s OK CAPABILITY completed' % tag)
        elif command == 'LOGIN':
            self._send('%s OK LOGIN completed' % tag)
        elif command in ('SELECT', 'EXAMINE'):
            mailbox = args.strip('"')
            if mailbox == 'failbox':
                # Untagged responses arrive, then the selection fails.
                self._send(r'* FLAGS (\Answered \Seen)')
                self._send('%s NO no such mailbox' % tag)
            elif mailbox == 'slowbox':
                # Untagged responses arrive well before the tagged OK.
                self._send(r'* FLAGS (\Answered \Seen)')
                self._send('* 3 EXISTS')
                self._send('* OK [UIDVALIDITY 999] UIDs valid')
                self.untagged_sent.set()
                self.release_ok.wait(10)
                self._send('%s OK %s completed' % (tag, command))
            else:
                self._send(r'* FLAGS (\Answered \Seen)')
                self._send('* 2 EXISTS')
                self._send('* 1 RECENT')
                self._send('* OK [UIDVALIDITY 1] UIDs valid')
                self._send('%s OK %s completed' % (tag, command))
        elif command == 'LOGOUT':
            self._send('* BYE logging out')
            self._send('%s OK LOGOUT completed' % tag)
            return False
        else:
            self._send('%s OK %s completed' % (tag, command))
        return True


@pytest.fixture
def client():
    server_sock, client_sock = socket.socketpair()
    server = FakeIMAPServer(server_sock)
    conn = PipeIMAP4(client_sock, host='fake', timeout=10)
    conn.login('user', 'pass')
    conn.fake_server = server
    yield conn
    try:
        conn.logout()
    except Exception:
        pass
    server.thread.join(5)
    assert server.error is None


def wait_for(predicate, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_select_confirms_mailbox(client):
    typ, dat = client.select('first')
    assert typ == 'OK'
    assert dat == [b'2']
    assert client.mailbox == 'first'
    assert client.state == 'SELECTED'
    assert not client.is_readonly


def test_pending_select_keeps_previous_mailbox(client):
    server = client.fake_server
    client.select('first')

    result = {}

    def switch():
        result['value'] = client.select('slowbox')

    worker = threading.Thread(target=switch)
    worker.daemon = True
    worker.start()

    # New mailbox attributes have arrived at the client, but the
    # tagged OK has not: the confirmed selection must not change yet.
    assert server.untagged_sent.wait(5)
    assert wait_for(lambda: client._get_untagged_response('EXISTS', leave=True))
    assert client.mailbox == 'first'
    assert client.state == 'SELECTED'

    server.release_ok.set()
    worker.join(10)
    assert not worker.is_alive()
    assert result['value'][0] == 'OK'
    assert client.mailbox == 'slowbox'
    assert client.state == 'SELECTED'


def test_failed_select_deselects(client):
    client.select('first')
    assert client.mailbox == 'first'

    typ, dat = client.select('failbox')
    assert typ == 'NO'
    # A failed SELECT leaves no mailbox selected: the previous mailbox
    # must not be assumed to still be selected, and the failed target
    # must not be published either.
    assert client.mailbox is None
    assert client.state == 'AUTH'

    # Mailbox commands are illegal until a re-selection succeeds.
    with pytest.raises(IMAP4.error):
        client.fetch('1', '(FLAGS)')

    # Re-selection recovers.
    typ, dat = client.select('first')
    assert typ == 'OK'
    assert client.mailbox == 'first'
    assert client.state == 'SELECTED'


def test_close_clears_mailbox(client):
    client.select('first')
    typ, dat = client.close()
    assert typ == 'OK'
    assert client.mailbox is None
    assert client.state == 'AUTH'


def test_examine_confirms_readonly(client):
    typ, dat = client.examine('first')
    assert typ == 'OK'
    assert client.mailbox == 'first'
    assert client.is_readonly
