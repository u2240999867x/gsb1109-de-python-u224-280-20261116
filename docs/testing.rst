==========================
In-Memory CIP Peer (Testing)
==========================

``pycomm3.testing.cip_peer`` provides a reusable, fully in-memory CIP target
for offline regression tests.  It does not open a network socket and does not
require a real device.  Responses are synthesized from the identity and
contents of each request rather than replayed from a fixed byte script.

Scenario
========

A :class:`~pycomm3.testing.cip_peer.Scenario` is a sequence of expectations and
replies.  The helpers describe the frames a driver sends::

    from pycomm3 import LogixDriver
    from pycomm3.testing.cip_peer import (
        CIPPeer, Scenario, RegisterSession, ListIdentity, ForwardOpen,
        ForwardClose, Generic, Read, Reply, dint, string,
    )

    peer = CIPPeer(Scenario([
        RegisterSession(),
        ListIdentity(),
        ForwardOpen(reply=Reply.forward_open(connection_id=0x22)),
        Generic(0x01, class_id=0x64, instance=1,
                reply=Reply.success(string("PROGRAM").encode())),
        Read(tag="MyTag", elements=1, reply=Reply.read(dint(42))),
        ForwardClose(),
    ]))

    plc = LogixDriver("192.168.1.10", init_tags=False, init_program_tags=False)
    plc._sock = peer.socket()
    plc.open()
    plc._tags = {"MyTag": {...}}   # tag definition uploaded separately
    assert plc.read("MyTag").value == 42
    plc.close()
    peer.assert_complete()

``peer.socket()`` returns a drop-in replacement for
:class:`pycomm3.socket_.Socket`; ``connect()``, ``send()`` and ``receive()`` are
all served in memory.

Request matching
================

Every request is decoded into a
:class:`~pycomm3.testing.cip_peer.Request`.  The peer matches on:

* encapsulation command (``RegisterSession``, ``SendRRData``,
  ``SendUnitData``, ...);
* CIP service, request path (class/instance/attribute or symbolic tag);
* requested element count and fragmented-read byte offset;
* session handle and CIP connection ID.

Reads are never matched by arrival position.  Declare ``independent=True`` on a
read to allow several reads to be grouped into a ``MultipleServiceRequest`` or
reordered.  Each embedded reply is generated from the step matched to that
embedded request, so every returned ``Tag`` keeps its own type and value.

Fragmented reads use ``Reply.fragmented(value, count=..., more=...)``.  The
slice is taken at the byte offset carried by the matched request, and
``more=True`` returns service status ``0x06`` (insufficient packets).

Session and connection rules
----------------------------

* Each ``RegisterSession`` receives a new session handle.
* Only the active session may satisfy a CIP request.  A packet from an older
  session is answered with encapsulation status ``0x64`` (invalid session
  handle) and recorded in ``peer.rejections``; it consumes no scenario step.
* A ``SendUnitData`` frame using a stale connection ID is answered with a CIP
  error and likewise recorded as a rejection.
* Therefore a late packet from an old connection cannot complete a request in
  a new connection, even if both use the same sequence number.

Mismatch diagnostics
====================

An unexpected request raises
:class:`~pycomm3.testing.cip_peer.ScenarioMismatch` and the peer stops
processing; it will not answer later requests with the remaining declared
replies.  The exception provides:

* ``first_divergence`` — description of the first unexpected request;
* ``consumed`` and ``consumed_indexes`` — steps already consumed and their
  original scenario indexes;
* ``attempted`` — the decoded offending request;
* ``remaining`` and ``remaining_indexes`` — steps still outstanding.

Drivers wrap transport failures in ``CommError``.  After catching it, call
``peer.assert_no_mismatch()`` to re-raise the detailed ``ScenarioMismatch``.

Use ``peer.assert_complete()`` to verify that every declared step was
consumed, and ``peer.exchanges`` to correlate each request with the step it
satisfied.
