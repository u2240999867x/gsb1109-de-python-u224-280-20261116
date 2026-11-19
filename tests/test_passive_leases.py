# Copyright (C) 2007 Giampaolo Rodola' <g.rodola@gmail.com>.
# Use of this source code is governed by MIT license that can be
# found in the LICENSE file.

"""Tests for the passive (PASV/EPSV) port leasing machinery.

These tests use in-memory socket stand-ins, so that bind addresses,
lease ownership and lease reclamation can be verified without binding
any real (public) port.
"""

import errno
import socket

from pyftpdlib.handlers.ftp.dispatchers import PassiveDTP
from pyftpdlib.handlers.ftp.leases import PassivePortInUse
from pyftpdlib.handlers.ftp.leases import PassivePortLeaseManager
from pyftpdlib.handlers.ftp.leases import PassivePortsExhausted
from pyftpdlib.ioloop import IOLoop

from . import PyftpdlibTestCase

AF_INET = socket.AF_INET
AF_INET6 = socket.AF_INET6


class TestPassivePortLeaseManager(PyftpdlibTestCase):
    """Unit tests for PassivePortLeaseManager conflict detection and
    lease reclamation semantics.
    """

    def make_manager(self, dual_stack=True):
        return PassivePortLeaseManager(dual_stack=dual_stack)

    def test_same_endpoint_conflicts(self):
        m = self.make_manager()
        m.acquire(AF_INET, "127.0.0.1", 40000)
        with self.assertRaises(PassivePortInUse):
            m.acquire(AF_INET, "127.0.0.1", 40000)

    def test_same_port_different_addresses_is_fine(self):
        # Same port number alone does not imply a conflict.
        m = self.make_manager()
        m.acquire(AF_INET, "127.0.0.1", 40000)
        m.acquire(AF_INET, "127.0.0.2", 40000)
        assert len(m) == 2

    def test_wildcard_covers_specific_address(self):
        m = self.make_manager()
        m.acquire(AF_INET, "0.0.0.0", 40000)
        with self.assertRaises(PassivePortInUse):
            m.acquire(AF_INET, "127.0.0.1", 40000)
        # ...and the other way around
        m = self.make_manager()
        m.acquire(AF_INET, "127.0.0.1", 40000)
        with self.assertRaises(PassivePortInUse):
            m.acquire(AF_INET, "0.0.0.0", 40000)

    def test_empty_address_means_wildcard(self):
        m = self.make_manager()
        m.acquire(AF_INET, "", 40000)
        with self.assertRaises(PassivePortInUse):
            m.acquire(AF_INET, "127.0.0.1", 40000)

    def test_different_ports_do_not_conflict(self):
        m = self.make_manager()
        m.acquire(AF_INET, "0.0.0.0", 40000)
        m.acquire(AF_INET, "0.0.0.0", 40001)
        assert len(m) == 2

    def test_dual_stack_v6_wildcard_covers_v4(self):
        # A dual-stack "::" listener also claims the IPv4 port space.
        m = self.make_manager(dual_stack=True)
        m.acquire(AF_INET6, "::", 40000)
        with self.assertRaises(PassivePortInUse):
            m.acquire(AF_INET, "0.0.0.0", 40000)
        with self.assertRaises(PassivePortInUse):
            m.acquire(AF_INET, "127.0.0.1", 40000)
        # ...and the other way around
        m = self.make_manager(dual_stack=True)
        m.acquire(AF_INET, "127.0.0.1", 40000)
        with self.assertRaises(PassivePortInUse):
            m.acquire(AF_INET6, "::", 40000)

    def test_v6only_wildcard_does_not_cover_v4(self):
        # With IPV6_V6ONLY the two stacks are independent.
        m = self.make_manager(dual_stack=False)
        m.acquire(AF_INET6, "::", 40000)
        m.acquire(AF_INET, "0.0.0.0", 40000)
        assert len(m) == 2

    def test_specific_v6_and_v4_are_independent(self):
        # Same port number alone does not imply independence either:
        # a specific IPv6 address never overlaps an IPv4 endpoint.
        m = self.make_manager(dual_stack=True)
        m.acquire(AF_INET6, "::1", 40000)
        # an IPv4 wildcard does not cover a specific IPv6 address
        m.acquire(AF_INET, "0.0.0.0", 40000)
        assert len(m) == 2
        # ...but it still covers specific IPv4 addresses
        with self.assertRaises(PassivePortInUse):
            m.acquire(AF_INET, "127.0.0.1", 40000)

    def test_ipv4_mapped_v6_address_is_treated_as_v4(self):
        m = self.make_manager(dual_stack=True)
        m.acquire(AF_INET6, "::ffff:127.0.0.1", 40000)
        with self.assertRaises(PassivePortInUse):
            m.acquire(AF_INET, "127.0.0.1", 40000)
        # ...but not as the whole IPv4 space
        m.acquire(AF_INET, "127.0.0.2", 40000)
        assert len(m) == 2

    def test_release_frees_endpoint(self):
        m = self.make_manager()
        lease = m.acquire(AF_INET, "127.0.0.1", 40000)
        lease.release()
        assert lease.released
        assert len(m) == 0
        m.acquire(AF_INET, "127.0.0.1", 40000)  # can be leased again

    def test_release_is_idempotent(self):
        m = self.make_manager()
        lease = m.acquire(AF_INET, "127.0.0.1", 40000)
        lease.release()
        lease.release()
        m.release(lease)
        assert len(m) == 0

    def test_stale_lease_cannot_evict_new_generation(self):
        # Releasing an old lease object must not terminate a newer
        # lease covering the same endpoint.
        m = self.make_manager()
        gen1 = m.acquire(AF_INET, "127.0.0.1", 40000, owner="gen1")
        gen1.release()
        gen2 = m.acquire(AF_INET, "127.0.0.1", 40000, owner="gen2")
        gen1.release()
        m.release(gen1)
        assert len(m) == 1
        assert m.leases()[0] is gen2
        assert not gen2.released
        assert m.is_leased(AF_INET, "127.0.0.1", 40000)

    def test_ownership_is_tracked(self):
        m = self.make_manager()
        owner = object()
        lease = m.acquire(AF_INET, "127.0.0.1", 40000, owner=owner)
        assert lease.owner is owner
        assert m.leases()[0].owner is owner

    def test_is_leased(self):
        m = self.make_manager()
        assert not m.is_leased(AF_INET, "127.0.0.1", 40000)
        lease = m.acquire(AF_INET, "127.0.0.1", 40000)
        assert m.is_leased(AF_INET, "127.0.0.1", 40000)
        assert m.is_leased(AF_INET, "0.0.0.0", 40000)
        assert not m.is_leased(AF_INET, "127.0.0.1", 40001)
        lease.release()
        assert not m.is_leased(AF_INET, "127.0.0.1", 40000)


# ===================================================================
# --- PassiveDTP with in-memory socket stand-ins
# ===================================================================


class FakeSocket:
    """In-memory socket stand-in recording bind()/listen()/close()
    calls.  Ports listed in busy_ports fail with EADDRINUSE.
    """

    def __init__(self, family=AF_INET, busy_ports=()):
        self.family = family
        self.busy_ports = set(busy_ports)
        self.bound = None
        self.listen_backlog = None
        self.reuse_addr = False
        self.closed = False

    def bind(self, addr):
        host, port = addr[:2]
        if port in self.busy_ports:
            raise OSError(errno.EADDRINUSE, "Address already in use")
        self.bound = (host, port)

    def listen(self, backlog):
        self.listen_backlog = backlog

    def getsockname(self):
        return self.bound

    def setsockopt(self, level, optname, value):
        if optname == socket.SO_REUSEADDR:
            self.reuse_addr = True

    def getsockopt(self, level, optname):
        return 0

    def setblocking(self, flag):
        pass

    def close(self):
        self.closed = True


class FakeDTPHandler:
    """Stands in for the data channel handler."""

    connected = True

    def __init__(self, sock):
        self.sock = sock


class FakeCmdChannel:
    """Stands in for FTPHandler (the control channel)."""

    permit_foreign_addresses = False
    masquerade_address = None
    masquerade_address_map = {}
    connected = True

    def __init__(self, server, remote_ip, passive_ports, local_ip):
        self.server = server
        self.remote_ip = remote_ip
        self.passive_ports = passive_ports
        self.ioloop = IOLoop.instance()
        self.socket = FakeSocket()
        self.socket.bound = (local_ip, 21)
        self.responses = []
        self.data_channel = None
        self.dtp_handler_calls = 0
        self.on_dtp_connection_calls = 0

    def log(self, msg, logfun=None):
        pass

    def log_exception(self, inst):
        pass

    def respond(self, resp, logfun=None):
        self.responses.append(resp)

    def respond_w_warning(self, resp):
        self.responses.append(resp)

    def dtp_handler(self, sock, cmd_channel):
        assert cmd_channel is self
        self.dtp_handler_calls += 1
        return FakeDTPHandler(sock)

    def _on_dtp_connection(self):
        self.on_dtp_connection_calls += 1


class FakeServer:
    """Stands in for FTPServer."""

    backlog = 5

    def __init__(self, manager):
        self.socket = FakeSocket()
        self.passive_port_leases = manager


class FakePassiveDTP(PassiveDTP):
    """PassiveDTP using in-memory socket stand-ins and no timeout
    scheduling.
    """

    timeout = 0
    busy_ports = ()

    def create_socket(self, af, type):
        self.socket = FakeSocket(af, busy_ports=self.busy_ports)

    def listen(self, backlog):
        # bypass the ioloop registration: there is no real fd
        self.socket.listen(backlog)


class TestPassiveDTPLeasing(PyftpdlibTestCase):
    """PassiveDTP lease acquisition / release, verified through
    in-memory socket stand-ins.
    """

    def setUp(self):
        super().setUp()
        self.manager = PassivePortLeaseManager()
        self.server = FakeServer(self.manager)
        self.addCleanup(self._reset_busy_ports)

    def _reset_busy_ports(self):
        FakePassiveDTP.busy_ports = ()

    def make_channel(self, remote_ip="1.2.3.4", passive_ports=(40000,)):
        return FakeCmdChannel(
            self.server, remote_ip, list(passive_ports), "127.0.0.1"
        )

    def make_dtp(self, channel, extmode=False):
        return FakePassiveDTP(channel, extmode=extmode)

    def test_bind_address_and_227_response(self):
        chan = self.make_channel()
        dtp = self.make_dtp(chan)
        try:
            # the listener was bound on the leased endpoint
            assert dtp.socket.bound == ("127.0.0.1", 40000)
            assert dtp.socket.reuse_addr
            assert dtp.socket.listen_backlog == self.server.backlog
            assert chan.responses == [
                "227 Entering passive mode (127,0,0,1,156,64)."
            ]
            # the lease is owned by this PassiveDTP instance
            (lease,) = self.manager.leases()
            assert lease.owner is dtp
            assert lease.port == 40000
            assert not lease.released
        finally:
            dtp.close()

    def test_epsv_response(self):
        chan = self.make_channel()
        dtp = self.make_dtp(chan, extmode=True)
        try:
            assert chan.responses == [
                "229 Entering extended passive mode (|||40000|)."
            ]
        finally:
            dtp.close()

    def test_close_terminates_lease(self):
        chan = self.make_channel()
        dtp = self.make_dtp(chan)
        assert len(self.manager) == 1
        dtp.close()
        assert len(self.manager) == 0
        assert dtp.socket.closed
        # idempotent
        dtp.close()
        assert len(self.manager) == 0

    def test_timeout_terminates_lease(self):
        chan = self.make_channel()
        dtp = self.make_dtp(chan)
        dtp.handle_timeout()
        assert "421 Passive data channel timed out." in chan.responses
        assert len(self.manager) == 0

    def test_renegotiation_releases_previous_lease(self):
        # A second PASV/EPSV on the same session closes the previous
        # acceptor first, so the same single port can be re-leased.
        chan = self.make_channel()
        dtp1 = self.make_dtp(chan)
        dtp1.close()  # what _shutdown_connecting_dtp() does
        dtp2 = self.make_dtp(chan)
        try:
            assert dtp2.socket.bound == ("127.0.0.1", 40000)
            (lease,) = self.manager.leases()
            assert lease.owner is dtp2
        finally:
            dtp2.close()

    def test_exhausted_range_raises_and_leaves_no_lease(self):
        chan1 = self.make_channel(remote_ip="1.2.3.4")
        dtp1 = self.make_dtp(chan1)
        try:
            # a second session cannot lease the only port
            chan2 = self.make_channel(remote_ip="5.6.7.8")
            with self.assertRaises(PassivePortsExhausted):
                self.make_dtp(chan2)
            # the failed attempt left no "ghost" lease behind and
            # did not disturb the legitimate one
            (lease,) = self.manager.leases()
            assert lease.owner is dtp1
        finally:
            dtp1.close()
        assert len(self.manager) == 0

    def test_bind_failure_leaves_no_ghost_lease(self):
        FakePassiveDTP.busy_ports = (40000,)
        chan = self.make_channel()
        with self.assertRaises(PassivePortsExhausted):
            self.make_dtp(chan)
        assert len(self.manager) == 0
        # no response was sent and the socket was cleaned up
        assert chan.responses == []

    def test_busy_port_is_skipped(self):
        FakePassiveDTP.busy_ports = (40000,)
        chan = self.make_channel(passive_ports=(40000, 40001))
        dtp = self.make_dtp(chan)
        try:
            assert dtp.socket.bound == ("127.0.0.1", 40001)
            (lease,) = self.manager.leases()
            assert lease.port == 40001
        finally:
            dtp.close()

    def test_accepted_connection_belongs_to_lease_owner(self):
        # An accepted data connection is handed to the session owning
        # the listener; once accepted, it is not affected by the
        # listener being closed nor by the port being re-leased to
        # another session.
        chan1 = self.make_channel(remote_ip="1.2.3.4")
        dtp1 = self.make_dtp(chan1)
        data_sock = FakeSocket()
        dtp1.handle_accepted(data_sock, ("1.2.3.4", 5000))
        # listener closed, lease terminated, connection delegated
        assert dtp1._closed
        assert len(self.manager) == 0
        assert chan1.dtp_handler_calls == 1
        assert chan1.on_dtp_connection_calls == 1
        assert isinstance(chan1.data_channel, FakeDTPHandler)
        assert chan1.data_channel.sock is data_sock
        # the port can now be leased by a new generation
        chan2 = self.make_channel(remote_ip="5.6.7.8")
        dtp2 = self.make_dtp(chan2)
        try:
            (lease,) = self.manager.leases()
            assert lease.owner is dtp2
            # ...and the already-accepted transfer of the previous
            # generation is still owned by the first session
            assert chan1.data_channel.sock is data_sock
            assert not data_sock.closed
            assert chan2.data_channel is None
        finally:
            dtp2.close()

    def test_late_connection_not_claimed_by_next_generation(self):
        # A data connection arriving late (after the lease ended and
        # the port was re-leased to another session) must not be
        # claimed by the new generation listener.
        chan1 = self.make_channel(remote_ip="1.2.3.4")
        dtp1 = self.make_dtp(chan1)
        dtp1.close()  # lease terminated (e.g. PASV renegotiation)
        chan2 = self.make_channel(remote_ip="5.6.7.8")
        dtp2 = self.make_dtp(chan2)
        try:
            # a late connection from the previous client shows up on
            # the new listener: it is a foreign address for this
            # session, hence rejected
            late_sock = FakeSocket()
            dtp2.handle_accepted(late_sock, ("1.2.3.4", 5000))
            assert late_sock.closed
            assert chan2.data_channel is None
            assert chan2.dtp_handler_calls == 0
            assert any(
                resp.startswith("425 Rejected data connection")
                for resp in chan2.responses
            )
            # the listener stays open waiting for the legit client
            assert not dtp2._closed
            assert len(self.manager) == 1
        finally:
            dtp2.close()

    def test_control_channel_close_terminates_lease(self):
        # Simulates FTPHandler.close() -> _shutdown_connecting_dtp().
        chan = self.make_channel()
        dtp = self.make_dtp(chan)
        chan.connected = False
        dtp.close()
        assert len(self.manager) == 0
        # a late connection arriving afterwards is dropped
        late_sock = FakeSocket()
        dtp.handle_accepted(late_sock, ("1.2.3.4", 5000))
        assert chan.data_channel is None
