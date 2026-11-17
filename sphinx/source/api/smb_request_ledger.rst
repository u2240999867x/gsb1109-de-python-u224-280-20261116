SMB2 Request Ledger
===================

Large file transfers split into many SMB2 READ requests. Each request occupies
a contiguous MessageId range of ``CreditCharge`` MessageIds, and the server may
answer asynchronously (a ``STATUS_PENDING`` response followed later by the final
response carrying an AsyncId).

:mod:`smb.request_ledger` keeps a replayable identity record for every such
request so that MessageId ranges, credits, session IDs and AsyncIds can be
correlated after the fact.

MessageId allocation
--------------------

MessageIds are allocated by the request's actual credit charge, not by the
number of logical operations::

    from smb.request_ledger import SMB2RequestLedger, credit_charge_for_size

    ledger = SMB2RequestLedger(initial_credits = 64)
    a = ledger.register(command = 0x0008, size = 64 * 1024)        # frames 1..1
    b = ledger.register(command = 0x0008, size = 3 * 64 * 1024)    # frames 2..4
    c = ledger.register(command = 0x0008, size = 64 * 1024 + 1)    # frames 5..6

    assert b.mid_range == (2, 4)
    assert c.mid_range == (5, 6)

The allocator skips MessageId ranges that are still in flight, so two requests
can never overlap even when a large request was sent first.

Lifecycle and queries
---------------------

A record moves through ``pending`` / ``async_pending`` and then reaches exactly
one terminal state. Success, failure and still-incomplete requests can be
queried separately::

    ledger.successful_requests()     # status == success
    ledger.failed_requests()         # failed / cancelled / timed_out
    ledger.incomplete_requests()     # still pending or aborted by a reconnect
    ledger.by_status('success')
    ledger.find(mid = 2)             # the request owning MessageId 2
    ledger.replay()                  # replayable transition log

Credits are reserved at registration and released exactly once on the first
terminal transition. Cancelling or timing out one request only affects that
request; a late completion for it is logged but does not release credits again::

    ledger.cancel(a)
    ledger.timeout(b)
    ledger.complete(a, status = 0)   # ignored (late); no second release

Reconnecting
------------

``begin_generation()`` starts a new MessageId namespace. Outstanding requests
from the previous connection are aborted and release their credits; a response
delivered after the reconnect can only match a request from the new generation::

    ledger.begin_generation()
    fresh = ledger.register(...)     # may reuse MessageId 1
    ledger.handle_message(mid = 1, status = 0, generation = ledger.generation)

SMB client integration
----------------------

``smb.base.SMB`` owns an ``SMB2RequestLedger`` in ``self.request_ledger``. The
same data is available as ``self.smb2_requests``. For an in-memory check of a
multi-segment read, inspect:

* ``smb.request_ledger.records`` – one record per request, with
  ``mid_range``, ``credit_charge``, ``session_id``, ``async_id``,
  ``status`` and ``delivered_bytes``
* ``smb.request_ledger.available_credits`` – remaining credits after the
  in-flight reservations

No network connection or shared folder is required to replay messages against
the ledger.

.. autoclass:: smb.request_ledger.SMB2RequestLedger
    :members:

.. autoclass:: smb.request_ledger.MessageIdAllocator
    :members:

.. autoclass:: smb.request_ledger.RequestRecord
    :members:
