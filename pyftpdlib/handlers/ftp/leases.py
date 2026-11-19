# Copyright (C) 2007 Giampaolo Rodola' <g.rodola@gmail.com>.
# Use of this source code is governed by MIT license that can be
# found in the LICENSE file.

"""Bookkeeping for passive (PASV/EPSV) data channel ports.

When ``FTPHandler.passive_ports`` is configured, all the FTP sessions
served by the same FTPServer instance share that same limited pool of
ports.  The classes in here keep track of which (address family,
local address, port) endpoints are currently leased out to a session,
so that:

* two sessions are never handed the same endpoint at the same time;
* leases are keyed by the actual bind address and address family, not
  just by the port number, taking dual-stack IPv4/IPv6 overlaps into
  account (two endpoints sharing the same port number are neither
  necessarily conflicting nor necessarily independent);
* a lease is terminated exactly once, by its owner, no matter whether
  the passive listener is torn down because of a PASV/EPSV
  renegotiation, a timeout, or the control connection being closed;
* a failed bind() never leaves a "ghost" lease behind.
"""

import ipaddress
import socket
import threading

from pyftpdlib.utils import has_dualstack_ipv6

__all__ = [
    "PassivePortInUse",
    "PassivePortsExhausted",
    "PassivePortLease",
    "PassivePortLeaseManager",
]


class PassivePortInUse(Exception):
    """Raised when a (family, address, port) endpoint conflicts with
    an already active lease."""


class PassivePortsExhausted(Exception):
    """Raised when no port in the configured passive_ports range could
    be leased and bound."""


def _normalize_address(af, address):
    """Return a (family, address, is_wildcard) tuple describing the
    endpoint a bind() on (af, address) would occupy.

    IPv4-mapped IPv6 addresses (e.g. "::ffff:1.2.3.4") are folded into
    their IPv4 counterpart, since a dual-stack bind on such an address
    effectively occupies the IPv4 endpoint.
    """
    if af == socket.AF_INET6:
        default_family = socket.AF_INET6
    else:
        default_family = socket.AF_INET
    if not address:
        # "" / None mean "all interfaces" for the given family
        wildcard = "::" if default_family == socket.AF_INET6 else "0.0.0.0"
        return default_family, wildcard, True
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        # non-numeric address (host name): compare it literally
        return default_family, address, False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
            return socket.AF_INET, str(ip), ip.is_unspecified
        return socket.AF_INET6, str(ip), ip.is_unspecified
    return socket.AF_INET, str(ip), ip.is_unspecified


def _endpoints_conflict(endpoint_a, endpoint_b, dual_stack):
    """Return True if two (family, address, is_wildcard) endpoints
    cannot be bound at the same time (on the same port).
    """
    fam_a, addr_a, wild_a = endpoint_a
    fam_b, addr_b, wild_b = endpoint_b
    if fam_a == fam_b:
        return wild_a or wild_b or addr_a == addr_b
    # Mixed IPv4 / IPv6 endpoints on the same port only overlap when
    # the IPv6 side is the wildcard "::" of a dual-stack socket, which
    # also claims the whole IPv4 port space.  A specific IPv6 address
    # never overlaps an IPv4 endpoint.
    v6_wild = wild_a if fam_a == socket.AF_INET6 else wild_b
    return bool(dual_stack and v6_wild)


class PassivePortLease:
    """An owned reservation over a (family, address, port) endpoint.

     - (PassivePortLeaseManager) manager: the originating manager.
     - (int) af: the address family (socket.AF_INET / socket.AF_INET6).
     - (str) address: the local address the listener is bound to.
     - (int) port: the local port the listener is bound to.
     - (instance) owner: whoever acquired the lease (typically a
       PassiveDTP instance); informational only.
    """

    __slots__ = (
        "manager",
        "af",
        "address",
        "port",
        "owner",
        "_endpoint",
        "_released",
    )

    def __init__(self, manager, af, address, port, owner):
        self.manager = manager
        self.af = af
        self.address = address
        self.port = port
        self.owner = owner
        self._endpoint = _normalize_address(af, address)
        self._released = False

    def __repr__(self):
        return (
            f"<{self.__class__.__name__} af={self.af} "
            f"address={self.address!r} port={self.port} "
            f"released={self._released}>"
        )

    @property
    def released(self):
        """True if the lease has been terminated."""
        return self._released

    def release(self):
        """Terminate the lease.  Idempotent: releasing twice or
        releasing a stale lease object never affects newer leases
        covering the same endpoint.
        """
        self.manager.release(self)


class PassivePortLeaseManager:
    """Tracks the passive port endpoints currently leased out to the
    FTP sessions of a server.

     - (bool) dual_stack: whether an IPv6 wildcard ("::") listener
       also claims the IPv4 port space.  Defaults to the platform
       capability as reported by has_dualstack_ipv6().
    """

    def __init__(self, dual_stack=None):
        self._dual_stack = dual_stack
        self._buckets = {}  # port -> list of active leases
        self._lock = threading.Lock()

    def __repr__(self):
        return f"<{self.__class__.__name__} active_leases={len(self)}>"

    def __len__(self):
        with self._lock:
            return sum(len(bucket) for bucket in self._buckets.values())

    @property
    def dual_stack(self):
        if self._dual_stack is None:
            self._dual_stack = has_dualstack_ipv6()
        return self._dual_stack

    def acquire(self, af, address, port, owner=None):
        """Lease the (af, address, port) endpoint for *owner*.
        Raise PassivePortInUse if it conflicts with an active lease.
        """
        lease = PassivePortLease(self, af, address, port, owner)
        with self._lock:
            for other in self._buckets.get(port, ()):
                if _endpoints_conflict(
                    lease._endpoint, other._endpoint, self.dual_stack
                ):
                    raise PassivePortInUse(
                        f"passive endpoint {address!r}:{port} (family "
                        f"{af}) conflicts with active lease {other!r}"
                    )
            self._buckets.setdefault(port, []).append(lease)
        return lease

    def release(self, lease):
        """Terminate *lease*.  Only the exact lease object handed out
        by acquire() is removed, hence a stale lease can never evict a
        newer lease covering the same endpoint.
        """
        with self._lock:
            if lease._released:
                return
            lease._released = True
            bucket = self._buckets.get(lease.port)
            if bucket:
                for index, other in enumerate(bucket):
                    if other is lease:
                        del bucket[index]
                        break
                if not bucket:
                    del self._buckets[lease.port]

    def is_leased(self, af, address, port):
        """Return True if the (af, address, port) endpoint conflicts
        with an active lease.
        """
        endpoint = _normalize_address(af, address)
        with self._lock:
            return any(
                _endpoints_conflict(endpoint, other._endpoint, self.dual_stack)
                for other in self._buckets.get(port, ())
            )

    def leases(self):
        """Return a snapshot list of the active leases."""
        with self._lock:
            return [
                lease for bucket in self._buckets.values() for lease in bucket
            ]
