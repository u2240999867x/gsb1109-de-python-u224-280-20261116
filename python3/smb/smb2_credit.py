"""SMB2 credit accounting and offline capture replay.

The module models the credit rules from [MS-SMB2] 3.2.4.1.5:

* a request consumes ``CreditCharge`` credits before it may be sent;
* ``CreditCharge`` is ``ceil(transfer_size / 65536)`` for READ/WRITE and at
  least one for every other command;
* an interim ``STATUS_PENDING`` response keeps the request in flight; only the
  final response completes it;
* the credits granted by a response (``CreditResponse``) are added once;
* duplicate/late responses must not complete a request or grant credits twice;
* a cancelled request can either keep its charge until the server's final
  ``STATUS_CANCELLED`` response, or be released immediately by the caller;
* after a disconnect all outstanding requests are failed and every later
  response is ignored.

The module can be used directly::

    ledger = CreditLedger()
    ledger.grant(8)
    ledger.submit_read(1, 131072)       # charges two credits
    ledger.pending(1, async_id=0x1234)
    ledger.response(async_id=0x1234)

It can also replay a JSON capture::

    result = replay_capture(json.load(open('capture.json')))

or a raw SMB2/NetBIOS/pcap capture::

    result = replay_capture(open('capture.pcap', 'rb').read())
"""

import json
import struct
import sys

from .smb2_constants import (
    SMB2_COM_CANCEL,
    SMB2_COM_READ,
    SMB2_COM_WRITE,
)
from .smb2_structs import SMB2Message


CREDIT_UNIT = 65536
STATUS_SUCCESS = 0x00000000
STATUS_PENDING = 0x00000103
STATUS_CANCELLED = 0xC0000120
STATUS_CONNECTION_DISCONNECTED = 0xC000020C

SMB2_FLAGS_SERVER_TO_REDIR = 0x01
SMB2_FLAGS_ASYNC_COMMAND = 0x02


def smb2_message_payload_size(message):
    """Return the byte size used for SMB2 credit charging."""
    if message.command == SMB2_COM_READ:
        return getattr(message.payload, 'read_len', 0) or 0
    if message.command == SMB2_COM_WRITE:
        return len(getattr(message.payload, 'data', b'') or b'')
    return 0


_smb2_message_payload_size = smb2_message_payload_size


def calculate_credit_charge(request_or_size, command=None):
    """Return the number of credits charged by an SMB2 request.

    ``request_or_size`` may be an integer transfer size or an ``SMB2Message``.
    For a message the size is taken from the READ/WRITE payload.  A size of
    zero still costs one credit, as required by [MS-SMB2] 3.2.4.1.5.
    """
    if isinstance(request_or_size, SMB2Message):
        message = request_or_size
        command = message.command
        payload = message.payload
        size = 0
        if command == SMB2_COM_READ:
            size = getattr(payload, 'read_len', 0) or 0
        elif command == SMB2_COM_WRITE:
            size = len(getattr(payload, 'data', b'') or b'')
    else:
        size = int(request_or_size or 0)

    if command not in (None, SMB2_COM_READ, SMB2_COM_WRITE):
        return 1
    if size <= 0:
        return 1
    return (size + CREDIT_UNIT - 1) // CREDIT_UNIT


# Common spelling variants; keeping these public makes the helper convenient
# for callers that already use one of these names.
credit_charge = calculate_credit_charge
compute_credit_charge = calculate_credit_charge
credits_for_read = calculate_credit_charge


def credit_charge_for_read(length):
    """Number of credits needed to read *length* bytes."""
    return calculate_credit_charge(length, SMB2_COM_READ)


def credit_charge_for_write(length):
    """Number of credits needed to write *length* bytes."""
    return calculate_credit_charge(length, SMB2_COM_WRITE)


class CreditRequest(object):
    """A request tracked by :class:`CreditLedger`."""

    QUEUED = 'queued'
    IN_FLIGHT = 'in_flight'
    PENDING = 'pending'
    COMPLETED = 'completed'
    CANCELLED = 'cancelled'
    FAILED = 'failed'

    def __init__(self, mid, charge, size=0, command=None, enqueued_at=0.0):
        self.mid = mid
        self.charge = int(charge)
        self.size = int(size or 0)
        self.command = command
        self.async_id = None
        self.enqueued_at = enqueued_at
        self.dispatched_at = None
        self.pending_at = None
        self.finished_at = None
        self.cancel_requested_at = None
        self.state = self.QUEUED
        self.finish_status = None
        self.credits_granted = 0
        self.pending_credits_granted = 0
        self.duplicate_responses = 0
        self.response_seen = False
        self.available_at_dispatch = None
        self.in_flight_at_dispatch = None
        self.in_flight_after_dispatch = None
        self.available_at_finish = None
        self.in_flight_at_finish = None

    @property
    def active(self):
        return self.state in (self.QUEUED, self.IN_FLIGHT, self.PENDING)

    @property
    def in_flight(self):
        return self.state in (self.IN_FLIGHT, self.PENDING)

    @property
    def duplicate_response_count(self):
        return self.duplicate_responses

    def to_dict(self):
        return {
            'mid': self.mid,
            'command': self.command,
            'length': self.size,
            'size': self.size,
            'charge': self.charge,
            'credit_charge': self.charge,
            'async_id': self.async_id,
            'state': self.state,
            'enqueued_at': self.enqueued_at,
            'dispatched_at': self.dispatched_at,
            'dispatchable_at': self.dispatched_at,
            'pending_at': self.pending_at,
            'finished_at': self.finished_at,
            'cancel_requested_at': self.cancel_requested_at,
            'credit_response': self.credits_granted,
            'credits_granted': self.credits_granted,
            'pending_credits_granted': self.pending_credits_granted,
            'duplicate_responses': self.duplicate_responses,
            'duplicate_response_count': self.duplicate_responses,
            'available_at_dispatch': self.available_at_dispatch,
            'in_flight_at_dispatch': self.in_flight_at_dispatch,
            'in_flight_after_dispatch': self.in_flight_after_dispatch,
            'available_at_finish': self.available_at_finish,
            'in_flight_at_finish': self.in_flight_at_finish,
            'status': self.finish_status,
        }


class CreditResult(object):
    """Outcome of an operation on a :class:`CreditLedger`."""

    def __init__(self, accepted, reason='', request=None, at=None, available=0,
                 in_flight=0, dispatchable_at=None, duplicate=False):
        self.accepted = bool(accepted)
        self.ok = self.accepted
        self.reason = reason
        self.request = request
        self.at = at
        self.available = available
        self.in_flight = in_flight
        self.dispatchable_at = dispatchable_at
        self.duplicate = bool(duplicate)

    def __bool__(self):
        return self.accepted

    __nonzero__ = __bool__

    def __getattr__(self, name):
        request = self.__dict__.get('request')
        if request is not None and hasattr(request, name):
            return getattr(request, name)
        raise AttributeError(name)

    def to_dict(self):
        result = {
            'accepted': self.accepted,
            'ok': self.accepted,
            'reason': self.reason,
            'at': self.at,
            'available': self.available,
            'in_flight': self.in_flight,
            'dispatchable_at': self.dispatchable_at,
            'duplicate': self.duplicate,
        }
        if self.request is not None:
            result['request'] = self.request.to_dict()
        return result


class CreditLedger(object):
    """Track SMB2 credits, in-flight charges and request completion.

    ``available`` is the number of unused credits.  A dispatch subtracts the
    request's full ``CreditCharge`` from it and increments ``in_flight`` by the
    same amount.  A final response decrements ``in_flight`` and adds the
    server's ``CreditResponse`` back to ``available``.  If no explicit
    ``credit_response`` is supplied, the request's charge is returned, which is
    the common behaviour of servers and the least surprising behaviour when a
    capture does not contain the header field.
    """

    def __init__(self, initial_credits=0, credit_unit=CREDIT_UNIT,
                 auto_pump=True, clamp_available=False, track_only=False,
                 dispatch_policy='work_conserving', history_limit=0):
        self.credit_unit = int(credit_unit)
        self.auto_pump = bool(auto_pump)
        self.clamp_available = bool(clamp_available)
        self.track_only = bool(track_only)
        self._history_limit = int(history_limit or 0)
        if dispatch_policy not in ('work_conserving', 'fifo'):
            raise ValueError('dispatch_policy must be work_conserving or fifo')
        self.dispatch_policy = dispatch_policy
        self._available = 0
        self.in_flight = 0
        self.total_granted = 0
        self.initial_credits = 0
        self.requests = {}
        self._queue = []
        self._async = {}
        self._retired_async = {}
        self._history = []
        self._events = []
        self.now = 0.0
        self.closed = False
        self.close_reason = None
        if initial_credits:
            self.grant(initial_credits, at=0.0, reason='initial')

    # ------------------------------------------------------------------
    # State properties
    # ------------------------------------------------------------------
    @property
    def available(self):
        return self._available

    @available.setter
    def available(self, value):
        value = int(value)
        self._available = value if not self.clamp_available else max(0, value)

    @property
    def credits_available(self):
        return self._available

    @property
    def available_credits(self):
        return self._available

    @property
    def in_flight_credits(self):
        return self.in_flight

    @property
    def inflight_credits(self):
        return self.in_flight

    @property
    def queued(self):
        return [req for req in self._queue if req.state == CreditRequest.QUEUED]

    @property
    def active_requests(self):
        return [req for req in self.requests.values() if req.active]

    @property
    def events(self):
        return list(self._events)

    @property
    def usable(self):
        """Credits that can still be spent on new requests."""
        return self._available

    # ------------------------------------------------------------------
    # Basic credit operations
    # ------------------------------------------------------------------
    def grant(self, credits, at=None, reason='grant'):
        """Add server-granted credits to the connection."""
        credits = int(credits or 0)
        if at is not None:
            self.now = float(at)
        if self.closed:
            self._record('grant', at=self.now, accepted=False,
                         reason='disconnected', credits=credits)
            return self._available
        if credits < 0:
            raise ValueError('credits must not be negative')
        self._available += credits
        self.total_granted += credits
        self._record('grant', at=self.now, accepted=True, reason=reason,
                     credits=credits)
        self._pump(self.now)
        return self._available

    grant_credits = grant
    add_credits = grant

    def set_available(self, credits, at=None):
        """Override the available credit count (mainly for test fixtures)."""
        if at is not None:
            self.now = float(at)
        self._available = max(0, int(credits))
        self.initial_credits = self._available
        self._record('set_available', at=self.now, accepted=True,
                     credits=self._available)
        return self._available

    # ------------------------------------------------------------------
    # Request dispatch
    # ------------------------------------------------------------------
    def submit(self, mid, size=0, command=None, charge=None, at=None,
               async_id=None, force=False):
        """Submit a request and dispatch it if enough credits are available.

        ``force`` dispatches even when the ledger does not have enough credits.
        It is useful for replaying a capture that was taken from a client which
        ignored credit limits, while still reporting the negative balance.
        """
        if at is not None:
            self.now = float(at)
        if self.closed:
            return CreditResult(False, 'disconnected', at=self.now,
                                available=self._available,
                                in_flight=self.in_flight)

        existing = self.requests.get(mid)
        if existing is not None and existing.active:
            return CreditResult(False, 'duplicate_mid', existing, self.now,
                                self._available, self.in_flight,
                                duplicate=True)
        if existing is not None:
            self._history.append(self.requests.pop(mid))
            if existing.async_id is not None:
                self._async.pop(existing.async_id, None)
            if existing in self._queue:
                self._queue.remove(existing)

        if charge is None:
            charge = calculate_credit_charge(size, command)
        else:
            # A multi-credit request must never be undercharged because a
            # capture or caller provided a too-small explicit value.
            charge = max(int(charge),
                         calculate_credit_charge(size, command))
        charge = max(1, int(charge))

        req = CreditRequest(mid, charge, size=size, command=command,
                            enqueued_at=self.now)
        if async_id is not None:
            req.async_id = async_id
            self._async[async_id] = req
        self.requests[mid] = req

        accepted = force or self.track_only or self._can_dispatch(charge)
        if accepted:
            self._dispatch(req, self.now, force=force or self.track_only)
            return CreditResult(True, 'dispatched', req, self.now,
                                self._available, self.in_flight,
                                dispatchable_at=req.dispatched_at)
        self._queue.append(req)
        self._record('queued', at=self.now, mid=mid, charge=charge,
                     available=self._available, in_flight=self.in_flight)
        return CreditResult(True, 'queued', req, self.now, self._available,
                            self.in_flight, dispatchable_at=None)

    request = submit
    submit_request = submit

    def submit_read(self, mid, length, at=None, charge=None, force=False):
        """Convenience wrapper for a READ request."""
        return self.submit(mid, size=length, command=SMB2_COM_READ,
                           charge=charge, at=at, force=force)

    def submit_write(self, mid, length, at=None, charge=None, force=False):
        """Convenience wrapper for a WRITE request."""
        return self.submit(mid, size=length, command=SMB2_COM_WRITE,
                           charge=charge, at=at, force=force)

    read = submit_read
    write = submit_write

    def dispatch(self, mid, at=None, force=False):
        """Dispatch a previously queued request by MID."""
        if at is not None:
            self.now = float(at)
        req = self.requests.get(mid)
        if req is None:
            return CreditResult(False, 'unknown_request', at=self.now,
                                available=self._available,
                                in_flight=self.in_flight)
        if req.state != CreditRequest.QUEUED:
            return CreditResult(False, 'not_queued', req, self.now,
                                self._available, self.in_flight,
                                duplicate=req.state in (CreditRequest.COMPLETED,
                                                        CreditRequest.CANCELLED,
                                                        CreditRequest.FAILED))
        if not force:
            if self.dispatch_policy == 'fifo' and self._queue and \
                    self._queue[0] is not req:
                return CreditResult(False, 'blocked_by_fifo', req, self.now,
                                    self._available, self.in_flight)
            if self._available < req.charge:
                return CreditResult(False, 'insufficient_credits', req,
                                    self.now, self._available,
                                    self.in_flight)
        self._queue.remove(req)
        self._dispatch(req, self.now, force=force)
        return CreditResult(True, 'dispatched', req, self.now,
                            self._available, self.in_flight,
                            dispatchable_at=req.dispatched_at)

    def _dispatch(self, req, at, force=False):
        if self.track_only:
            # Live connections in pysmb do not currently throttle on credits;
            # tracking only records the in-flight charge without consuming the
            # advertised budget.
            req.available_at_dispatch = self._available
            req.in_flight_at_dispatch = self.in_flight
            self.in_flight += req.charge
            req.in_flight_after_dispatch = self.in_flight
            req.state = CreditRequest.IN_FLIGHT
            req.dispatched_at = at
            self._record('dispatch', at=at, mid=req.mid, charge=req.charge,
                         available=self._available, in_flight=self.in_flight,
                         track_only=True)
            return req

        req.available_at_dispatch = self._available
        req.in_flight_at_dispatch = self.in_flight
        self._available -= req.charge
        if self._available < 0:
            if self.clamp_available:
                self._available += req.charge
                raise ValueError('insufficient credits for dispatch')
            # Replay mode may take the balance negative if the capture overran
            # the server's advertised budget; keep the in-flight figure
            # accurate and report the overrun in the result.
        self.in_flight += req.charge
        req.in_flight_after_dispatch = self.in_flight
        req.state = CreditRequest.IN_FLIGHT
        req.dispatched_at = at
        self._record('dispatch', at=at, mid=req.mid, charge=req.charge,
                     available=self._available, in_flight=self.in_flight)
        return req

    def _can_dispatch(self, charge):
        if self.dispatch_policy == 'fifo' and self._queue:
            return False
        return self._available >= charge

    def _pump(self, at):
        if not self.auto_pump or self.closed:
            return
        if self.dispatch_policy == 'fifo':
            while self._queue:
                req = self._queue[0]
                if self._available < req.charge:
                    break
                self._queue.pop(0)
                self._dispatch(req, at)
            return
        # Work-conserving dispatch: walk the queue in arrival order and send
        # every request that fits.  A large request that cannot fit must not
        # prevent a later, smaller request from using the available credits.
        index = 0
        while index < len(self._queue):
            req = self._queue[index]
            if self._available >= req.charge:
                self._queue.pop(index)
                self._dispatch(req, at)
            else:
                index += 1

    def dispatch_queued(self, at=None):
        """Dispatch as many queued requests as the current credits allow."""
        if at is not None:
            self.now = float(at)
        before = len(self._queue)
        self._pump(self.now)
        return before - len(self._queue)

    # ------------------------------------------------------------------
    # Responses and completion
    # ------------------------------------------------------------------
    def _lookup(self, mid=None, async_id=None):
        if async_id is not None and async_id in self._async:
            return self._async[async_id]
        if async_id is not None and async_id in self._retired_async:
            return self._retired_async[async_id]
        if mid is not None:
            return self.requests.get(mid)
        return None

    def _retire_async(self, req):
        if req.async_id is None:
            return
        self._async.pop(req.async_id, None)
        self._retired_async[req.async_id] = req
        if len(self._retired_async) > 128:
            oldest = next(iter(self._retired_async))
            del self._retired_async[oldest]

    def pending(self, mid=None, async_id=None, credit_response=None, at=None):
        """Handle an interim ``STATUS_PENDING`` response.

        The original charge stays in flight.  Credits explicitly granted by the
        interim response are applied once; a duplicate interim response does not
        grant them again.
        """
        if at is not None:
            self.now = float(at)
        if self.closed:
            return CreditResult(False, 'disconnected', at=self.now,
                                available=self._available,
                                in_flight=self.in_flight)

        req = self._lookup(mid, async_id)
        if req is None:
            return CreditResult(False, 'unknown_request', at=self.now,
                                available=self._available,
                                in_flight=self.in_flight,
                                duplicate=True)
        if not req.active:
            req.duplicate_responses += 1
            return CreditResult(False, 'duplicate_response', req, self.now,
                                self._available, self.in_flight,
                                duplicate=True)

        if async_id is not None:
            if req.async_id is None:
                req.async_id = async_id
                self._async[async_id] = req
            elif req.async_id != async_id:
                req.duplicate_responses += 1
                return CreditResult(False, 'async_id_mismatch', req, self.now,
                                    self._available, self.in_flight,
                                    duplicate=True)

        if req.state == CreditRequest.QUEUED:
            # A pending response proves the request exists, but the grant-based
            # model may still be waiting for credits.  Remember the AsyncId and
            # leave dispatch to the normal credit pump.
            if req.pending_at is not None:
                req.duplicate_responses += 1
                return CreditResult(False, 'duplicate_pending', req, self.now,
                                    self._available, self.in_flight,
                                    duplicate=True)
            req.pending_at = self.now
            self._record('pending_deferred', at=self.now, mid=req.mid,
                         async_id=req.async_id, credits=0,
                         available=self._available, in_flight=self.in_flight)
            return CreditResult(True, 'pending_deferred', req, self.now,
                                self._available, self.in_flight)

        if req.pending_at is not None:
            req.duplicate_responses += 1
            return CreditResult(False, 'duplicate_pending', req, self.now,
                                self._available, self.in_flight,
                                duplicate=True)

        req.pending_at = self.now
        req.state = CreditRequest.PENDING
        # The interim response still grants credits; the request charge stays
        # in flight until the final response arrives.
        granted = 0 if credit_response is None else int(credit_response)
        if self.track_only:
            # Track-only mode mirrors a live connection whose budget is set
            # from the negotiated MaxCredits; interim grants are already
            # reflected in that budget.
            granted = 0
        if granted:
            self._available += granted
            self.total_granted += granted
            req.credits_granted += granted
            req.pending_credits_granted += granted
        self._record('pending', at=self.now, mid=req.mid,
                     async_id=req.async_id, credits=granted,
                     available=self._available, in_flight=self.in_flight)
        self._pump(self.now)
        return CreditResult(True, 'pending', req, self.now, self._available,
                            self.in_flight)

    on_pending = pending

    def response(self, mid=None, async_id=None, status=STATUS_SUCCESS,
                 credit_response=None, at=None, grant_unmatched=False):
        """Handle a final (or interim) SMB2 response.

        A ``STATUS_PENDING`` status is delegated to :meth:`pending`.  Any
        duplicate/late response is accepted as a no-op: it neither releases the
        request's charge nor applies the response's credit grant again.

        With ``grant_unmatched=True`` an unmatched response still contributes
        its server-granted credits.  This is useful for handshake responses
        (NEGOTIATE/SESSION_SETUP), which are not tracked as ordinary requests.
        """
        if at is not None:
            self.now = float(at)
        if self.closed:
            return CreditResult(False, 'disconnected', at=self.now,
                                available=self._available,
                                in_flight=self.in_flight)

        status = _parse_status(status)
        if status == STATUS_PENDING:
            return self.pending(mid=mid, async_id=async_id,
                                credit_response=credit_response, at=self.now)

        req = self._lookup(mid, async_id)
        if req is None:
            if grant_unmatched and credit_response and not self.track_only:
                self._available += int(credit_response)
                self.total_granted += int(credit_response)
                self._record('grant', at=self.now, mid=mid,
                             async_id=async_id, credits=int(credit_response),
                             available=self._available,
                             in_flight=self.in_flight)
                self._pump(self.now)
                return CreditResult(True, 'granted', None, self.now,
                                    self._available, self.in_flight)
            return CreditResult(False, 'unknown_request', at=self.now,
                                available=self._available,
                                in_flight=self.in_flight,
                                duplicate=True)
        if not req.active:
            req.duplicate_responses += 1
            return CreditResult(False, 'duplicate_response', req, self.now,
                                self._available, self.in_flight,
                                duplicate=True)
        if req.state == CreditRequest.QUEUED:
            # A final response proves the request reached the server even if
            # the replay budget was tighter than the real connection's.  Charge
            # it at the observed response time so completion is coherent.
            if req in self._queue:
                self._queue.remove(req)
            self._dispatch(req, self.now, force=True)

        if async_id is not None and req.async_id is None:
            req.async_id = async_id
            self._async[async_id] = req

        self._finish(req, status, credit_response, self.now)
        return CreditResult(True, 'completed', req, self.now, self._available,
                            self.in_flight)

    on_response = response
    complete = response
    reply = response

    def _finish(self, req, status, credit_response, at):
        self.in_flight -= req.charge
        if self.in_flight < 0:
            self.in_flight = 0
        req.finished_at = at
        req.finish_status = status
        if status == STATUS_CANCELLED:
            req.state = CreditRequest.CANCELLED
        else:
            req.state = CreditRequest.COMPLETED

        if credit_response is None:
            granted = req.charge
        else:
            granted = int(credit_response)
        req.credits_granted += granted
        if self.track_only:
            # Track-only mode mirrors a live connection whose advertised budget
            # is applied by the server.  Record the grant for reporting, but do
            # not synthesize a local budget from it.
            granted = 0
        if granted:
            self._available += granted
            self.total_granted += granted
        req.available_at_finish = self._available
        req.in_flight_at_finish = self.in_flight
        self._record('complete', at=at, mid=req.mid, async_id=req.async_id,
                     status=status, credits=granted,
                     available=self._available, in_flight=self.in_flight)
        # Keep the async id reserved for duplicate detection, but it is no
        # longer part of the active lookup table.
        self._retire_async(req)
        if self._history_limit and len(self.requests) > self._history_limit:
            for mid in list(self.requests):
                if len(self.requests) <= self._history_limit:
                    break
                old = self.requests[mid]
                if old.active:
                    continue
                self._history.append(self.requests.pop(mid))
        self._pump(at)

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------
    def cancel(self, mid=None, async_id=None, at=None, release=True,
               credit_response=None, reason='cancel'):
        """Cancel a request.

        With ``release=True`` (the default for this helper) the request is
        completed immediately and its charge is returned.  A later response for
        the same MID/AsyncId is a duplicate and has no effect.

        With ``release=False`` the cancellation is only recorded; the request
        stays in flight until the server's final response (normally
        ``STATUS_CANCELLED``).  This is the protocol-faithful behaviour for a
        CANCEL message and is what :meth:`on_cancel_message` uses.
        """
        if at is not None:
            self.now = float(at)
        if self.closed:
            return CreditResult(False, 'disconnected', at=self.now,
                                available=self._available,
                                in_flight=self.in_flight)

        req = self._lookup(mid, async_id)
        if req is None:
            return CreditResult(False, 'unknown_request', at=self.now,
                                available=self._available,
                                in_flight=self.in_flight,
                                duplicate=True)
        if not req.active:
            req.duplicate_responses += 1
            return CreditResult(False, 'duplicate_response', req, self.now,
                                self._available, self.in_flight,
                                duplicate=True)

        req.cancel_requested_at = self.now
        if req.state == CreditRequest.QUEUED:
            if req in self._queue:
                self._queue.remove(req)
            req.state = CreditRequest.CANCELLED
            req.finished_at = self.now
            req.finish_status = STATUS_CANCELLED
            self._record('cancel', at=self.now, mid=req.mid, credits=0,
                         available=self._available, in_flight=self.in_flight)
            return CreditResult(True, 'cancelled_queued', req, self.now,
                                self._available, self.in_flight)

        if not release:
            self._record('cancel_pending', at=self.now, mid=req.mid,
                         async_id=req.async_id, credits=0,
                         available=self._available, in_flight=self.in_flight)
            return CreditResult(True, 'cancel_pending', req, self.now,
                                self._available, self.in_flight)

        self._finish(req, STATUS_CANCELLED, credit_response, self.now)
        return CreditResult(True, 'cancelled', req, self.now, self._available,
                            self.in_flight)

    on_cancel = cancel

    def on_cancel_message(self, mid=None, async_id=None, at=None,
                          charge_request=False):
        """Handle an SMB2 CANCEL packet observed in a capture.

        The CANCEL packet itself normally consumes one credit.  Pass
        ``charge_request=True`` to model that extra credit; by default the
        helper only marks the target request as cancel-requested and waits for
        its final response, which is the conservative SMB2 behaviour.
        """
        if at is not None:
            self.now = float(at)
        if charge_request:
            cancel_mid = ('cancel', mid, async_id)
            result = self.submit(cancel_mid, size=0, command=SMB2_COM_CANCEL,
                                 charge=1, at=self.now)
            if not result.accepted:
                return result
        return self.cancel(mid=mid, async_id=async_id, at=self.now,
                           release=False, reason='cancel_message')

    # ------------------------------------------------------------------
    # Disconnect
    # ------------------------------------------------------------------
    def disconnect(self, at=None, reason='disconnected'):
        """Mark the connection closed and fail every outstanding request.

        Credits are connection-scoped, so ``available`` becomes zero.  Requests
        are not "completed" and no credits are returned.  All later response or
        grant calls are ignored, which also covers late duplicate responses on
        the old connection.
        """
        if at is not None:
            self.now = float(at)
        if self.closed:
            return CreditResult(False, 'already_disconnected', at=self.now,
                                available=self._available,
                                in_flight=self.in_flight, duplicate=True)

        self.closed = True
        self.close_reason = reason
        failed = 0
        for req in self.requests.values():
            if req.active:
                req.state = CreditRequest.FAILED
                req.finished_at = self.now
                req.finish_status = STATUS_CONNECTION_DISCONNECTED
                failed += 1
        self._queue = []
        self.in_flight = 0
        self._available = 0
        self._record('disconnect', at=self.now, accepted=True,
                     reason=reason, failed=failed, available=0, in_flight=0)
        return CreditResult(True, 'disconnected', None, self.now, 0, 0)

    on_disconnect = disconnect
    connection_lost = disconnect

    # ------------------------------------------------------------------
    # Inspection helpers
    # ------------------------------------------------------------------
    def _record(self, action, **kwargs):
        event = {'action': action}
        event.update(kwargs)
        self._events.append(event)

    def snapshot(self, at=None):
        if at is not None:
            self.now = float(at)
        return {
            'time': self.now,
            'at': self.now,
            'available': self._available,
            'in_flight': self.in_flight,
            'granted': self.total_granted,
            'total_granted': self.total_granted,
            'queued': len(self._queue),
            'active': len(self.active_requests),
            'closed': self.closed,
        }

    def summary(self):
        return {
            'available': self._available,
            'in_flight': self.in_flight,
            'total_granted': self.total_granted,
            'requests': len(self.requests),
            'completed': sum(1 for r in self.requests.values()
                             if r.state == CreditRequest.COMPLETED),
            'cancelled': sum(1 for r in self.requests.values()
                             if r.state == CreditRequest.CANCELLED),
            'failed': sum(1 for r in self.requests.values()
                          if r.state == CreditRequest.FAILED),
            'queued': len(self._queue),
            'closed': self.closed,
        }

    def request_dicts(self):
        return [req.to_dict() for req in self.requests.values()]


SMB2CreditLedger = CreditLedger
SMB2CreditManager = CreditLedger
CreditManager = CreditLedger
CreditAccount = CreditLedger


# ----------------------------------------------------------------------
# Capture parsing / replay
# ----------------------------------------------------------------------
def _parse_status(value):
    if value is None:
        return STATUS_SUCCESS
    if isinstance(value, int):
        return value & 0xFFFFFFFF
    text = str(value).strip()
    upper = text.upper().replace('-', '_')
    names = {
        'SUCCESS': STATUS_SUCCESS,
        'STATUS_SUCCESS': STATUS_SUCCESS,
        'PENDING': STATUS_PENDING,
        'STATUS_PENDING': STATUS_PENDING,
        'MORE_PROCESSING_REQUIRED': 0xC0000016,
        'STATUS_MORE_PROCESSING_REQUIRED': 0xC0000016,
        'CANCELLED': STATUS_CANCELLED,
        'CANCELED': STATUS_CANCELLED,
        'STATUS_CANCELLED': STATUS_CANCELLED,
        'STATUS_CANCELED': STATUS_CANCELLED,
        'DISCONNECTED': STATUS_CONNECTION_DISCONNECTED,
        'STATUS_CONNECTION_DISCONNECTED': STATUS_CONNECTION_DISCONNECTED,
    }
    if upper in names:
        return names[upper]
    try:
        return int(text, 0) & 0xFFFFFFFF
    except ValueError:
        return STATUS_SUCCESS


def _first(mapping, names, default=None):
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return default


def _event_time(event, index):
    value = _first(event, ('time', 't', 'timestamp', 'ts', 'at'))
    if value is None:
        return float(index)
    return float(value)


def _event_kind(event):
    value = _first(event, ('type', 'kind', 'event', 'op', 'operation', 'action'))
    if value is None:
        if _contains_any(event, ('length', 'read_len', 'read_length',
                                 'size', 'bytes')):
            return 'read'
        if _contains_any(event, ('credits', 'grant', 'granted')):
            return 'grant'
        return ''
    return str(value).strip().lower().replace('-', '_')


def _contains_any(mapping, names):
    return any(name in mapping for name in names)


def _event_int(event, names, default=None):
    value = _first(event, names, default)
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    try:
        return int(text, 0)
    except ValueError:
        return default


_KIND_ALIASES = {
    'grant': 'grant',
    'granted': 'grant',
    'credits': 'grant',
    'credit': 'grant',
    'server_grant': 'grant',
    'grant_credits': 'grant',
    'credit_grant': 'grant',
    'negotiate': 'grant',
    'session_setup': 'grant',
    'read': 'read',
    'read_request': 'read',
    'smb2_read': 'read',
    'request': 'read',
    'write': 'write',
    'write_request': 'write',
    'smb2_write': 'write',
    'response': 'response',
    'reply': 'response',
    'complete': 'response',
    'completion': 'response',
    'read_response': 'response',
    'pending': 'pending',
    'async_pending': 'pending',
    'cancel': 'cancel',
    'cancelled': 'cancel',
    'canceled': 'cancel',
    'cancel_request': 'cancel',
    'disconnect': 'disconnect',
    'disconnected': 'disconnect',
    'connection_lost': 'disconnect',
    'close': 'disconnect',
    'disconnect_event': 'disconnect',
}


def _normalize_kind(kind):
    return _KIND_ALIASES.get(kind, kind)


def _capture_events(capture):
    """Return ``(initial_credits, [(time, kind, payload), ...])``."""
    initial = 0

    if isinstance(capture, (bytes, bytearray)):
        return _events_from_bytes(bytes(capture))

    if isinstance(capture, str):
        text = capture.strip()
        if not text:
            return 0, []
        try:
            capture = json.loads(text)
        except ValueError:
            # Try a JSONL capture: one event/object per line.
            records = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                records.append(json.loads(line))
            return _capture_events(records)

    if isinstance(capture, dict):
        initial = _event_int(capture, (
            'initial_credits', 'credits', 'granted_credits', 'server_credits',
            'grant', 'granted', 'credit_limit',
        ), 0) or 0
        records = _first(capture, ('events', 'trace', 'capture', 'records',
                                   'messages', 'packets'))
        if records is None:
            records = _events_from_compact_capture(capture)
        capture = records

    events = []
    for index, raw in enumerate(capture or []):
        if isinstance(raw, (int, float)):
            raw = {'type': 'read', 'length': int(raw)}
        elif isinstance(raw, (tuple, list)):
            raw = _tuple_event(raw, index)
        if not isinstance(raw, dict):
            continue
        at = _event_time(raw, index)
        kind = _normalize_kind(_event_kind(raw))
        events.append((at, index, kind, raw))

    events.sort(key=lambda item: (item[0], item[1]))
    return initial, [(at, kind, raw) for at, _, kind, raw in events]


def _tuple_event(values, index):
    """Convert a compact tuple/list record into an event mapping.

    Supported forms include ``(time, 'read', mid, length)``,
    ``('grant', credits, time)`` and ``('disconnect', time)``.
    """
    values = list(values)
    if not values:
        return {'type': 'read', 'length': 0}
    time = None
    if isinstance(values[0], (int, float)):
        time = values.pop(0)
    if not values:
        return {'type': 'read', 'length': 0, 'time': time}
    kind = _normalize_kind(str(values.pop(0)).strip().lower().replace('-', '_'))
    event = {'type': kind}
    if time is not None:
        event['time'] = time
    if values and isinstance(values[-1], (int, float)) and time is None:
        event['time'] = values.pop()
    if kind == 'grant':
        event['credits'] = values[0] if values else 0
    elif kind in ('read', 'write', 'request'):
        event['mid'] = values[0] if values else index + 1
        event['length'] = values[1] if len(values) > 1 else 0
    elif kind == 'pending':
        event['mid'] = values[0] if values else index + 1
        if len(values) > 1:
            event['async_id'] = values[1]
    elif kind == 'response':
        event['mid'] = values[0] if values else index + 1
        if len(values) > 1:
            event['credits'] = values[1]
        if len(values) > 2:
            event['status'] = values[2]
    elif kind == 'cancel':
        event['mid'] = values[0] if values else index + 1
    return event


def _events_from_compact_capture(capture):
    """Support a few compact capture shapes.

    Example::

        {'initial_credits': 4,
         'reads': [{'mid': 1, 'length': 65536, 'at': 0.1}],
         'responses': [{'mid': 1, 'at': 0.5}],
         'grants': [{'credits': 2, 'at': 1.0}],
         'cancels': [{'mid': 2, 'at': 1.5}],
         'disconnect': {'at': 2.0}}
    """
    records = []
    for key, kind in (('grants', 'grant'), ('credits_events', 'grant'),
                      ('reads', 'read'), ('read_requests', 'read'),
                      ('writes', 'write'), ('responses', 'response'),
                      ('replies', 'response'), ('pending', 'pending'),
                      ('cancels', 'cancel'), ('cancellations', 'cancel')):
        values = capture.get(key)
        if values is None:
            continue
        if isinstance(values, dict):
            values = [values]
        for value in values:
            if isinstance(value, (int, float)):
                value = {'length': int(value)}
            item = dict(value)
            item.setdefault('type', kind)
            records.append(item)
    disconnect = capture.get('disconnect')
    if disconnect:
        item = dict(disconnect) if isinstance(disconnect, dict) else {}
        item.setdefault('type', 'disconnect')
        records.append(item)
    return records


def replay_capture(capture, initial_credits=None, auto_pump=True):
    """Replay a capture and return per-request dispatch times and credit state.

    ``capture`` may be a dict/list already loaded from JSON, a JSON/JSONL
    string, or raw SMB2/NetBIOS/pcap bytes.
    """
    base_initial, events = _capture_events(capture)
    if initial_credits is None:
        initial_credits = base_initial
    ledger = CreditLedger(initial_credits=0, auto_pump=auto_pump)
    if initial_credits:
        ledger.grant(initial_credits, at=0.0, reason='initial')

    trace = []
    for at, kind, event in events:
        ledger.now = at
        mid = _event_int(event, ('mid', 'message_id', 'messageid', 'mId',
                                 'MessageId'))
        async_id = _event_int(event, ('async_id', 'asyncid', 'asyncId',
                                      'AsyncId'))
        if kind == 'grant':
            credits = _event_int(event, (
                'credits', 'grant', 'granted', 'credit', 'credit_response',
                'credit_re', 'credit_grant', 'n', 'count',
            ), 0) or 0
            result = CreditResult(True, 'grant', at=at,
                                  available=ledger.available,
                                  in_flight=ledger.in_flight)
            if not ledger.closed:
                ledger.grant(credits, at=at)
                result.available = ledger.available
                result.in_flight = ledger.in_flight
            else:
                result.accepted = False
                result.reason = 'disconnected'
        elif kind in ('read', 'write', 'request'):
            length = _event_int(event, (
                'length', 'len', 'read_len', 'read_length', 'read_size',
                'size', 'bytes', 'transfer_size', 'payload_size', 'count',
            ), 0) or 0
            command = SMB2_COM_WRITE if kind == 'write' else SMB2_COM_READ
            explicit_charge = _event_int(event, ('charge', 'credit_charge',
                                                 'creditcost', 'cost'))
            force = bool(_first(event, ('force', 'force_dispatch'), False))
            result = ledger.submit(mid, size=length, command=command,
                                   charge=explicit_charge, at=at,
                                   async_id=async_id, force=force)
        elif kind == 'pending':
            credits = _event_int(event, ('credits', 'credit_response',
                                         'credit_re', 'credit_grant'))
            result = ledger.pending(mid=mid, async_id=async_id,
                                    credit_response=credits, at=at)
        elif kind == 'response':
            status = _parse_status(_first(event, ('status', 'result', 'code')))
            credits = _event_int(event, ('credits', 'credit_response',
                                         'credit_re', 'credit_grant'))
            result = ledger.response(mid=mid, async_id=async_id,
                                     status=status,
                                     credit_response=credits, at=at,
                                     grant_unmatched=True)
        elif kind == 'cancel':
            release = _first(event, ('release', 'release_credits'), None)
            if release is None:
                release = True
            result = ledger.cancel(mid=mid, async_id=async_id, at=at,
                                   release=bool(release))
        elif kind == 'disconnect':
            result = ledger.disconnect(at=at, reason='capture_disconnect')
        else:
            result = CreditResult(False, 'unknown_event', at=at,
                                  available=ledger.available,
                                  in_flight=ledger.in_flight)

        trace.append({
            'time': at,
            'type': kind,
            'mid': mid,
            'async_id': async_id,
            'accepted': result.accepted,
            'reason': result.reason,
            'available': ledger.available,
            'in_flight': ledger.in_flight,
            'dispatchable_at': result.dispatchable_at,
        })

    return {
        'initial_credits': initial_credits,
        'requests': ledger.request_dicts(),
        'timeline': trace,
        'events': ledger.events,
        'summary': ledger.summary(),
        'available': ledger.available,
        'in_flight': ledger.in_flight,
        'total_granted': ledger.total_granted,
    }


replay = replay_capture
run_replay = replay_capture
replay_trace = replay_capture


def replay_capture_file(path, initial_credits=None, auto_pump=True):
    """Read *path* (JSON, JSONL, raw SMB2/NetBIOS or pcap) and replay it."""
    with open(path, 'rb') as handle:
        data = handle.read()
    if data.lstrip()[:1] in (b'{', b'['):
        capture = json.loads(data.decode('utf-8'))
    else:
        capture = data
    return replay_capture(capture, initial_credits=initial_credits,
                          auto_pump=auto_pump)


# ----------------------------------------------------------------------
# Raw SMB2 / NetBIOS / pcap parsing
# ----------------------------------------------------------------------
def _iter_netbios_frames(data):
    """Yield ``(offset, smb_frame)`` for a NetBIOS session message stream."""
    pos = 0
    length = len(data)
    while pos + 4 <= length:
        if data[pos] != 0:
            break
        frame_len = (data[pos + 1] << 16) | (data[pos + 2] << 8) | data[pos + 3]
        if frame_len == 0 or pos + 4 + frame_len > length:
            break
        yield pos, data[pos + 4:pos + 4 + frame_len]
        pos += 4 + frame_len


def _iter_smb2_compound(data, offset=0, end=None):
    """Yield each SMB2 message in a (possibly compound) SMB2 byte range."""
    pos = offset
    if end is None:
        end = len(data)
    while pos + 64 <= end and data[pos:pos + 4] == b'\xfeSMB':
        next_offset = struct.unpack_from('<I', data, pos + 20)[0]
        if next_offset:
            frame_len = next_offset
        else:
            frame_len = _infer_frame_length(data, pos, end)
        if frame_len <= 0 or pos + frame_len > end:
            break
        yield pos, data[pos:pos + frame_len]
        if not next_offset:
            # A non-compound SMB2 message ends the NetBIOS session message.
            # For raw concatenated frames, continue only if another header
            # immediately follows.
            if pos + frame_len + 64 > end or \
                    data[pos + frame_len:pos + frame_len + 4] != b'\xfeSMB':
                break
            pos += frame_len
            continue
        pos += next_offset


def _infer_frame_length(data, pos, end):
    """Best-effort SMB2 frame length when NextCommand is zero."""
    command = struct.unpack_from('<H', data, pos + 12)[0]
    flags = struct.unpack_from('<I', data, pos + 16)[0]
    is_reply = bool(flags & SMB2_FLAGS_SERVER_TO_REDIR)

    if command == SMB2_COM_READ:
        if is_reply:
            if pos + 80 > end:
                return end - pos
            data_offset = struct.unpack_from('<B', data, pos + 66)[0]
            data_length = struct.unpack_from('<I', data, pos + 68)[0]
            if data_offset >= 64:
                return max(80, data_offset + data_length)
            return 80
        return 64 + 49
    if command == SMB2_COM_WRITE:
        if is_reply:
            return 64 + 16
        if pos + 112 <= end:
            data_offset = struct.unpack_from('<H', data, pos + 66)[0]
            data_length = struct.unpack_from('<I', data, pos + 68)[0]
            if data_offset >= 64:
                return max(112, data_offset + data_length)
        return 64 + 49
    if command == SMB2_COM_CANCEL:
        return 64
    if is_reply and pos + 68 <= end:
        structure_size = struct.unpack_from('<H', data, pos + 64)[0]
        if 1 <= structure_size <= (end - pos - 64):
            return 64 + structure_size

    # Last resort: find the next aligned SMB2 header.  This handles request
    # messages whose variable-length payload has no explicit total length when
    # NextCommand is zero (the common case for a single message captured from
    # a NetBIOS/raw stream).
    search = pos + 64
    while search + 4 <= end:
        if (search - pos) % 8 == 0 and data[search:search + 4] == b'\xfeSMB':
            return search - pos
        search += 1
    return end - pos


def _iter_capture_frames(data):
    """Yield ``(offset, frame)`` pairs from raw SMB2 or NetBIOS bytes."""
    if data[:4] == b'\xfeSMB':
        for item in _iter_smb2_compound(data):
            yield item
        return
    for offset, session_frame in _iter_netbios_frames(data):
        for sub_offset, frame in _iter_smb2_compound(session_frame):
            yield offset + 4 + sub_offset, frame


def _decode_smb2_header(frame):
    if len(frame) < 64 or frame[:4] != b'\xfeSMB':
        return None
    (_struct_size, credit_charge, status, command, credit_re,
     flags) = struct.unpack_from('<HHIHHI', frame, 4)
    next_offset = struct.unpack_from('<I', frame, 20)[0]
    mid = struct.unpack_from('<Q', frame, 24)[0]
    if flags & SMB2_FLAGS_ASYNC_COMMAND:
        async_id = struct.unpack_from('<Q', frame, 32)[0]
        pid = tid = None
    else:
        async_id = None
        pid, tid = struct.unpack_from('<II', frame, 32)
    return {
        'credit_charge': credit_charge,
        'status': status,
        'command': command,
        'credit_re': credit_re,
        'flags': flags,
        'next_offset': next_offset,
        'mid': mid,
        'async_id': async_id,
        'pid': pid,
        'tid': tid,
        'is_reply': bool(flags & SMB2_FLAGS_SERVER_TO_REDIR),
        'is_async': bool(flags & SMB2_FLAGS_ASYNC_COMMAND),
    }


def _read_request_length(frame):
    if len(frame) < 72:
        return 0
    return struct.unpack_from('<I', frame, 68)[0]


def _write_request_length(frame):
    if len(frame) < 72:
        return 0
    return struct.unpack_from('<I', frame, 68)[0]


def _events_from_frames(frames):
    """Convert decoded frames to ``(initial, sorted events)``."""
    events = []
    initial = 0
    for index, (at, frame) in enumerate(frames):
        header = _decode_smb2_header(frame)
        if header is None:
            continue
        event = {'time': at, 'mid': header['mid']}
        if header['async_id'] is not None:
            event['async_id'] = header['async_id']
        if header['is_reply']:
            # Always pass the server's credit grant, including an explicit 0.
            event['credits'] = header['credit_re']
            if header['status'] == STATUS_PENDING:
                event['type'] = 'pending'
            else:
                event['type'] = 'response'
                event['status'] = header['status']
        elif header['command'] == SMB2_COM_READ:
            length = _read_request_length(frame)
            event['type'] = 'read'
            event['length'] = length
            event['charge'] = max(header['credit_charge'],
                                  calculate_credit_charge(length,
                                                          SMB2_COM_READ))
        elif header['command'] == SMB2_COM_WRITE:
            length = _write_request_length(frame)
            event['type'] = 'write'
            event['length'] = length
            event['charge'] = max(header['credit_charge'],
                                  calculate_credit_charge(length,
                                                          SMB2_COM_WRITE))
        elif header['command'] == SMB2_COM_CANCEL:
            event['type'] = 'cancel'
        else:
            continue
        events.append((at, index, _normalize_kind(event['type']), event))
    events.sort(key=lambda item: (item[0], item[1]))
    return initial, [(at, kind, event) for at, _, kind, event in events]


def _events_from_bytes(data):
    if data[:4] in (b'\xd4\xc3\xb2\xa1', b'\xa1\xb2\xc3\xd4',
                    b'\x4d\x3c\xb2\xa1', b'\xa1\xb2\x3c\x4d'):
        return _events_from_pcap(data)
    frames = [(float(index), frame) for index, (_, frame)
              in enumerate(_iter_capture_frames(data))]
    return _events_from_frames(frames)


def _events_from_pcap(data):
    """Minimal pcap reader for TCP port 445/139 SMB2 streams.

    Packets are reassembled per direction in capture order.  This handles the
    usual single-connection captures used for offline analysis; it does not
    attempt to reassemble out-of-order or retransmitted segments.
    """
    magic = data[:4]
    if magic == b'\xd4\xc3\xb2\xa1':
        endian, nano = '<', False
    elif magic == b'\xa1\xb2\xc3\xd4':
        endian, nano = '>', False
    elif magic == b'\x4d\x3c\xb2\xa1':
        endian, nano = '<', True
    elif magic == b'\xa1\xb2\x3c\x4d':
        endian, nano = '>', True
    else:
        return _events_from_frames([])

    if len(data) < 24:
        return 0, []
    linktype = struct.unpack_from(endian + 'I', data, 20)[0]
    pos = 24
    streams = {}
    frames = []

    while pos + 16 <= len(data):
        ts_sec, ts_frac, incl_len, _ = struct.unpack_from(
            endian + 'IIII', data, pos)
        pos += 16
        packet = data[pos:pos + incl_len]
        pos += incl_len
        at = ts_sec + (ts_frac / 1e9 if nano else ts_frac / 1e6)

        tcp = _extract_tcp(packet, linktype)
        if tcp is None:
            continue
        src_ip, dst_ip, src_port, dst_port, payload = tcp
        if not (src_port in (139, 445) or dst_port in (139, 445)):
            continue
        key = (src_ip, src_port, dst_ip, dst_port)
        buf = streams.get(key, b'') + payload
        if buf[:4] == b'\xfeSMB':
            consumed = 0
            for offset, frame in _iter_smb2_compound(buf):
                frames.append((at, frame))
                consumed = offset + len(frame)
            streams[key] = buf[consumed:]
            continue

        consumed = 0
        for offset, session_frame in _iter_netbios_frames(buf):
            sub_consumed = 0
            for sub_offset, frame in _iter_smb2_compound(session_frame):
                frames.append((at, frame))
                sub_consumed = sub_offset + len(frame)
            if sub_consumed == 0:
                break
            consumed = offset + 4 + sub_consumed
        streams[key] = buf[consumed:]

    return _events_from_frames(frames)


def _extract_tcp(packet, linktype):
    if linktype == 1:  # Ethernet
        if len(packet) < 14:
            return None
        ethertype = struct.unpack_from('>H', packet, 12)[0]
        offset = 14
        if ethertype == 0x8100 and len(packet) >= 18:
            ethertype = struct.unpack_from('>H', packet, 16)[0]
            offset = 18
        if ethertype != 0x0800:
            return None
        packet = packet[offset:]
    elif linktype == 101:  # Raw IP
        pass
    elif linktype in (0, 228):  # BSD loopback / raw IPv4
        if len(packet) >= 4 and packet[:4] == b'\x02\x00\x00\x00':
            packet = packet[4:]
    else:
        return None

    if len(packet) < 20 or (packet[0] >> 4) != 4:
        return None
    ihl = (packet[0] & 0x0F) * 4
    if packet[9] != 6 or len(packet) < ihl + 20:
        return None
    src_ip = '.'.join(str(b) for b in packet[12:16])
    dst_ip = '.'.join(str(b) for b in packet[16:20])
    tcp = packet[ihl:]
    src_port, dst_port = struct.unpack_from('>HH', tcp, 0)
    data_offset = (tcp[12] >> 4) * 4
    payload = tcp[data_offset:]
    return src_ip, dst_ip, src_port, dst_port, payload


# ----------------------------------------------------------------------
# Command line
# ----------------------------------------------------------------------
def _load_cli_input(path):
    if path in ('-', None):
        return sys.stdin.buffer.read()
    with open(path, 'rb') as handle:
        return handle.read()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if '-h' in argv or '--help' in argv:
        print('usage: smb2_credit.py [--json] [--initial N] [capture | -]')
        print()
        print('Replay an offline SMB2 capture. The input may be JSON/JSONL or a')
        print('raw SMB2/NetBIOS/pcap byte stream. Use - or omit the file for stdin.')
        return 0
    as_json = '--json' in argv
    argv = [arg for arg in argv if arg != '--json']
    initial = None
    if '--initial' in argv:
        index = argv.index('--initial')
        initial = int(argv[index + 1], 0)
        del argv[index:index + 2]
    path = argv[0] if argv else '-'
    data = _load_cli_input(path)
    if data[:1] in (b'{', b'[') or data[:1].isspace():
        capture = json.loads(data.decode('utf-8'))
    else:
        capture = data
    result = replay_capture(capture, initial_credits=initial)

    if as_json:
        print(json.dumps(result, indent=2, sort_keys=True, default=str))
        return 0

    print('initial credits: %d' % result['initial_credits'])
    print('%-6s %-8s %-8s %-10s %-10s %-10s' % (
        'MID', 'LENGTH', 'CHARGE', 'DISPATCH', 'FINISH', 'STATE'))
    for req in result['requests']:
        print('%-6s %-8d %-8d %-10s %-10s %-10s' % (
            req['mid'], req['length'], req['charge'],
            _format_time(req['dispatchable_at']),
            _format_time(req['finished_at']), req['state']))
    summary = result['summary']
    print('granted=%d available=%d in_flight=%d completed=%d cancelled=%d '
          'failed=%d closed=%s' % (
              summary['total_granted'], summary['available'],
              summary['in_flight'], summary['completed'],
              summary['cancelled'], summary['failed'], summary['closed']))
    return 0


def _format_time(value):
    if value is None:
        return '-'
    return '%.6f' % value


if __name__ == '__main__':
    sys.exit(main())
