"""
Replayable SMB2 request identity and credit accounting.

The module is deliberately transport-free.  An SMB2 client can feed it outgoing
requests and incoming messages (real or in-memory) and the ledger will keep
track of

  * the contiguous MessageId range occupied by each request (CreditCharge can
    be larger than one),
  * the session and asynchronous id associated with the request,
  * credit reservations/releases, and
  * a replayable sequence of state transitions.

The allocator never advances MessageId by the number of logical operations.  It
advances by the credit charge of the request so that a request occupying, for
example, MessageIds 7..9 cannot overlap the next request which starts at 10.

References:
  - [MS-SMB2] 2.2.1.2, 3.2.4.1.1 and 3.2.5.1.1
"""

import time

from .smb2_constants import (SMB2_COM_READ,
                             SMB2_COM_WRITE,
                             SMB2_COM_IOCTL,
                             SMB2_COM_QUERY_DIRECTORY,
                             SMB2_COM_QUERY_INFO,
                             SMB2_COM_SET_INFO)


CREDIT_UNIT = 64 * 1024
STATUS_PENDING = 0x00000103
STATUS_SUCCESS = 0x00000000

MAX_MESSAGE_ID = (1 << 64) - 1


class RequestLedgerError(Exception):
    """Base class for request ledger/allocator errors."""


class MessageIdRangeConflict(RequestLedgerError):
    """Raised when an explicitly requested MessageId range is already in use."""


class MessageIdExhausted(RequestLedgerError):
    """Raised when no free MessageId range can be found."""


class InsufficientCredits(RequestLedgerError):
    """Raised when a request's credit charge exceeds the available budget."""


class RequestStatus:
    """Values used by :class:`RequestRecord.status`."""

    PENDING = 'pending'
    ASYNC_PENDING = 'async_pending'
    SUCCESS = 'success'
    FAILED = 'failed'
    CANCELLED = 'cancelled'
    TIMED_OUT = 'timed_out'
    ABORTED = 'aborted'

    # Common aliases people use when querying a ledger.
    SUCCEEDED = SUCCESS
    FAILURE = FAILED
    TIMEOUT = TIMED_OUT
    CANCELED = CANCELLED
    INCOMPLETE = PENDING

    ALL = (PENDING, ASYNC_PENDING, SUCCESS, FAILED, CANCELLED, TIMED_OUT, ABORTED)
    TERMINAL = (SUCCESS, FAILED, CANCELLED, TIMED_OUT, ABORTED)
    ACTIVE = (PENDING, ASYNC_PENDING)
    INCOMPLETE_STATUSES = (PENDING, ASYNC_PENDING, ABORTED)
    FAILURE_STATUSES = (FAILED, CANCELLED, TIMED_OUT)

    ALIASES = {
        'pending': PENDING,
        'incomplete': PENDING,
        'outstanding': PENDING,
        'waiting': PENDING,
        'async': ASYNC_PENDING,
        'async_pending': ASYNC_PENDING,
        'asyncpending': ASYNC_PENDING,
        'in_progress': ASYNC_PENDING,
        'success': SUCCESS,
        'succeeded': SUCCESS,
        'ok': SUCCESS,
        'complete': SUCCESS,
        'completed': SUCCESS,
        'failed': FAILED,
        'failure': FAILED,
        'error': FAILED,
        'cancelled': CANCELLED,
        'canceled': CANCELLED,
        'timeout': TIMED_OUT,
        'timed_out': TIMED_OUT,
        'timedout': TIMED_OUT,
        'aborted': ABORTED,
        'abort': ABORTED,
    }

    @classmethod
    def normalize(cls, status):
        if status is None:
            return None
        if isinstance(status, bool):
            return cls.SUCCESS if status else cls.FAILED
        if isinstance(status, int):
            if status == STATUS_SUCCESS:
                return cls.SUCCESS
            if status == STATUS_PENDING:
                return cls.PENDING
            return cls.FAILED
        if isinstance(status, str):
            return cls.ALIASES.get(status.strip().lower(), status)
        return status

    @classmethod
    def matches(cls, status, wanted):
        """True if *status* matches *wanted*, which may be a collection."""
        if wanted is None:
            return True
        wanted = cls.normalize(wanted)
        if isinstance(wanted, (list, tuple, set, frozenset)):
            normalized = {cls.normalize(item) for item in wanted}
            return cls.normalize(status) in normalized
        if isinstance(wanted, str):
            if cls.normalize(wanted) == cls.SUCCESS and cls.normalize(status) == cls.SUCCESS:
                return True
        return cls.normalize(status) == wanted


STATE_PENDING = RequestStatus.PENDING
STATE_ASYNC_PENDING = RequestStatus.ASYNC_PENDING
STATE_SUCCESS = RequestStatus.SUCCESS
STATE_SUCCEEDED = RequestStatus.SUCCESS
STATE_FAILED = RequestStatus.FAILED
STATE_CANCELLED = RequestStatus.CANCELLED
STATE_TIMED_OUT = RequestStatus.TIMED_OUT
STATE_ABORTED = RequestStatus.ABORTED


def credit_charge_for_size(size, minimum=1):
    """
    Return the number of credits needed to carry *size* bytes.

    SMB2 charges one credit per 64 KiB (rounded up), with a minimum of one
    credit even for tiny messages.
    """

    try:
        size = int(size or 0)
    except (TypeError, ValueError):
        size = 0

    if size <= 0:
        return minimum
    return max(minimum, (size + CREDIT_UNIT - 1) // CREDIT_UNIT)


def credit_charge_for_message(message):
    """
    Calculate the actual credit charge for an SMB2 request message.

    This intentionally looks at the request's expected payload size rather than
    the size of the encoded SMB2 packet.  A READ request for 1 MiB occupies 16
    credits even though its encoded request is only a few dozen bytes.
    """

    if message is None:
        return 1

    explicit = getattr(message, 'credit_charge', 0)
    if explicit:
        return max(1, int(explicit))

    command = getattr(message, 'command', None)
    payload = getattr(message, 'payload', None)
    size = 0

    if payload is not None:
        if command == SMB2_COM_READ:
            size = getattr(payload, 'read_len', 0)
        elif command == SMB2_COM_WRITE:
            data = getattr(payload, 'data', b'')
            size = len(data) if data is not None else 0
        elif command == SMB2_COM_IOCTL:
            in_data = getattr(payload, 'in_data', b'') or b''
            size = max(len(in_data), getattr(payload, 'max_out_size', 0) or 0)
        else:
            # [MS-SMB2] 3.2.4.1.4: requests other than READ/WRITE/IOCTL use a
            # single credit regardless of their negotiated buffer sizes.
            return 1

    if size <= 0:
        # Fall back to the encoded payload length (plus a little headroom for
        # the header) for commands without a meaningful expected response size.
        data = getattr(message, 'data', b'') or b''
        size = len(data) + 64

    return credit_charge_for_size(size)


class _RecordList(list):
    """A list that is also callable so ``x.records`` and ``x.records()`` work."""

    def __call__(self):
        return list(self)


class MessageIdAllocator:
    """
    Allocate non-overlapping, contiguous SMB2 MessageId ranges.

    Allocation is monotonic by default.  If a range has been explicitly
    reserved ahead of the cursor, the allocator skips it (or finds the next
    available gap) instead of overlapping it.
    """

    def __init__(self, start_mid=1, max_mid=MAX_MESSAGE_ID):
        self.start_mid = int(start_mid)
        self.max_mid = int(max_mid)
        self.next_mid = self.start_mid
        self._ranges = {}

    @staticmethod
    def _normalize_charge(credit_charge):
        try:
            charge = int(credit_charge)
        except (TypeError, ValueError):
            charge = 1
        return max(1, charge)

    def _range_end(self, start, charge):
        return start + charge - 1

    def _conflicts(self, start, charge):
        end = self._range_end(start, charge)
        for other_start, (other_charge, generation) in self._ranges.items():
            other_end = self._range_end(other_start, other_charge)
            if not (end < other_start or start > other_end):
                return other_start, other_end
        return None

    def is_free(self, start, credit_charge=1):
        charge = self._normalize_charge(credit_charge)
        start = int(start)
        if start < self.start_mid or self._range_end(start, charge) > self.max_mid:
            return False
        return self._conflicts(start, charge) is None

    def allocate(self, credit_charge=1, generation=None, **kwargs):
        if 'credits' in kwargs:
            credit_charge = kwargs['credits']
        if 'charge' in kwargs:
            credit_charge = kwargs['charge']

        charge = self._normalize_charge(credit_charge)
        if charge > (self.max_mid - self.start_mid + 1):
            raise MessageIdExhausted('Credit charge %d exceeds the MessageId space' % charge)

        for start in self._candidate_starts(self.next_mid):
            if start + charge - 1 > self.max_mid:
                continue
            if self._conflicts(start, charge) is None:
                self._ranges[start] = (charge, generation)
                self.next_mid = start + charge
                return start

        raise MessageIdExhausted('No free SMB2 MessageId range for %d credits' % charge)

    def _candidate_starts(self, cursor):
        cursor = max(int(cursor), self.start_mid)
        for start in range(cursor, self.max_mid + 1):
            yield start
        for start in range(self.start_mid, cursor):
            yield start

    def reserve(self, start, credit_charge=1, generation=None):
        """Reserve an explicit range, raising if it overlaps an existing one."""

        charge = self._normalize_charge(credit_charge)
        start = int(start)
        if start < self.start_mid:
            raise MessageIdRangeConflict('MessageId %d is outside the allocator range' % start)
        if start + charge - 1 > self.max_mid:
            raise MessageIdRangeConflict('MessageId range %d..%d exceeds the allocator limit' %
                                         (start, start + charge - 1))

        conflict = self._conflicts(start, charge)
        if conflict is not None:
            raise MessageIdRangeConflict('MessageId range %d..%d overlaps in-flight range %d..%d' %
                                         (start, start + charge - 1, conflict[0], conflict[1]))

        self._ranges[start] = (charge, generation)
        if start >= self.next_mid:
            self.next_mid = start + charge
        return start

    def release(self, start, credit_charge=None, generation=None):
        """Release a reserved range.  Repeated releases return ``False``."""

        start = int(start)
        entry = self._ranges.get(start)
        if entry is None:
            return False
        if credit_charge is not None and self._normalize_charge(credit_charge) != entry[0]:
            return False
        del self._ranges[start]
        return True

    def reset(self, start_mid=None):
        if start_mid is not None:
            self.start_mid = int(start_mid)
        self.next_mid = self.start_mid
        self._ranges.clear()

    @property
    def reserved_ranges(self):
        return [(start, self._range_end(start, charge), charge, generation)
                for start, (charge, generation) in sorted(self._ranges.items())]

    @property
    def in_flight_ranges(self):
        return self.reserved_ranges

    def __contains__(self, start):
        return int(start) in self._ranges

    def __len__(self):
        return len(self._ranges)


# Common alternative spellings for the allocator.
MessageIDAllocator = MessageIdAllocator
CreditAllocator = MessageIdAllocator


class RequestRecord:
    """Identity of one SMB2 request and its current lifecycle state."""

    def __init__(self, request_id, mid, credit_charge, command=None,
                 session_id=0, async_id=0, generation=0, created_at=None):
        self.request_id = request_id
        self.id = request_id
        self.mid = int(mid)
        self.message_id = self.mid
        self.credit_charge = max(1, int(credit_charge))
        self.credits = self.credit_charge
        self.command = command
        self.session_id = session_id or 0
        self.session = self.session_id
        self.async_id = async_id or 0
        self.generation = generation or 0
        self.status = RequestStatus.PENDING
        self.state = self.status
        self.created_at = created_at if created_at is not None else time.time()
        self.completed_at = None
        self.response_status = None
        self.response_mid = None
        self.response_async_id = None
        self.response_session_id = None
        self.response_credit_charge = None
        self.response_credits_granted = 0
        self.delivered_bytes = 0
        self.credits_released = False
        self.duplicate_completions = 0
        self.events = []
        self.pending_request = None
        self.granted_credits = 0

    # -- Identity ---------------------------------------------------------

    @property
    def mid_start(self):
        return self.mid

    @property
    def first_mid(self):
        return self.mid

    @property
    def message_id_start(self):
        return self.mid

    @property
    def mid_end(self):
        return self.mid + self.credit_charge - 1

    @property
    def last_mid(self):
        return self.mid_end

    @property
    def message_id_end(self):
        return self.mid_end

    @property
    def mid_range(self):
        return (self.mid_start, self.mid_end)

    @property
    def message_id_range(self):
        return self.mid_range

    @property
    def range(self):
        return self.mid_range

    @property
    def message_ids(self):
        return range(self.mid_start, self.mid_end + 1)

    def message_id_list(self):
        return list(self.message_ids)

    # -- State ------------------------------------------------------------

    @property
    def is_terminal(self):
        return self.status in RequestStatus.TERMINAL

    @property
    def is_active(self):
        return self.status in RequestStatus.ACTIVE

    @property
    def is_pending(self):
        return self.status in RequestStatus.ACTIVE

    @property
    def is_incomplete(self):
        return self.status in RequestStatus.INCOMPLETE_STATUSES

    @property
    def is_success(self):
        return self.status == RequestStatus.SUCCESS

    @property
    def is_failure(self):
        return self.status in RequestStatus.FAILURE_STATUSES

    @property
    def success(self):
        return self.is_success

    @property
    def failed(self):
        return self.is_failure

    @property
    def bytes_delivered(self):
        return self.delivered_bytes

    @property
    def data_length(self):
        return self.delivered_bytes

    @property
    def bytes(self):
        return self.delivered_bytes

    @property
    def outcome(self):
        if self.status == RequestStatus.SUCCESS:
            return 'success'
        if self.is_failure:
            return 'failure'
        if self.status in RequestStatus.ACTIVE:
            return 'incomplete'
        return 'incomplete'

    # -- Serialization ----------------------------------------------------

    def to_dict(self):
        return {
            'request_id': self.request_id,
            'id': self.id,
            'mid': self.mid,
            'message_id': self.message_id,
            'mid_start': self.mid_start,
            'mid_end': self.mid_end,
            'first_mid': self.first_mid,
            'last_mid': self.last_mid,
            'mid_range': self.mid_range,
            'message_id_range': self.message_id_range,
            'credit_charge': self.credit_charge,
            'credits': self.credits,
            'command': self.command,
            'session_id': self.session_id,
            'session': self.session,
            'async_id': self.async_id,
            'generation': self.generation,
            'status': self.status,
            'state': self.status,
            'response_status': self.response_status,
            'response_mid': self.response_mid,
            'response_async_id': self.response_async_id,
            'response_session_id': self.response_session_id,
            'response_credit_charge': self.response_credit_charge,
            'response_credits_granted': self.response_credits_granted,
            'delivered_bytes': self.delivered_bytes,
            'bytes_delivered': self.delivered_bytes,
            'data_length': self.delivered_bytes,
            'credits_released': self.credits_released,
            'duplicate_completions': self.duplicate_completions,
            'created_at': self.created_at,
            'completed_at': self.completed_at,
            'events': [dict(event) for event in self.events],
        }

    def __repr__(self):
        return ('RequestRecord(id=%r, mid=%d..%d, charge=%d, session=%r, '
                'async=%r, status=%r, bytes=%d)' %
                (self.request_id, self.mid_start, self.mid_end,
                 self.credit_charge, self.session_id, self.async_id,
                 self.status, self.delivered_bytes))


class SMB2RequestLedger:
    """
    Replayable ledger of SMB2 request identities, MessageId ranges and credits.

    ``initial_credits`` is the client-side credit budget.  A request reserves
    its charge when registered and releases it exactly once when it reaches a
    terminal state (success, failure, cancellation or timeout).  Pass ``None``
    to disable budget enforcement while still tracking reservations.
    """

    def __init__(self, initial_credits=64, start_mid=1, generation=0,
                 max_mid=MAX_MESSAGE_ID, total_credits=None, credits=None,
                 enforce_budget=True):
        if total_credits is not None:
            initial_credits = total_credits
        if credits is not None:
            initial_credits = credits

        self.initial_credits = None if initial_credits is None else int(initial_credits)
        self.total_credits = self.initial_credits
        self.enforce_budget = bool(enforce_budget)
        self.max_mid = max_mid
        self.generation = int(generation)
        self.generation_started_at = time.time()
        self.allocator = MessageIdAllocator(start_mid=start_mid, max_mid=max_mid)
        self.message_id_allocator = self.allocator
        self.mid_allocator = self.allocator
        self._records = []
        self._by_id = {}
        self._by_mid = {}
        self._by_async_id = {}
        self._next_request_id = 1
        self._reserved_credits = 0
        self._events = []
        self._event_sequence = 0
        self.total_granted_credits = 0

    # -- Record collection ------------------------------------------------

    @property
    def records(self):
        return _RecordList(self._records)

    @property
    def requests(self):
        return self.records

    @property
    def all_records(self):
        return self.records

    @property
    def pending_requests(self):
        return _RecordList(record for record in self._records if record.is_pending)

    @property
    def successful_requests(self):
        return _RecordList(record for record in self._records if record.is_success)

    @property
    def failed_requests(self):
        return _RecordList(record for record in self._records if record.is_failure)

    @property
    def incomplete_requests(self):
        return _RecordList(record for record in self._records if record.is_incomplete)

    @property
    def cancelled_requests(self):
        return _RecordList(record for record in self._records
                           if record.status == RequestStatus.CANCELLED)

    @property
    def timed_out_requests(self):
        return _RecordList(record for record in self._records
                           if record.status == RequestStatus.TIMED_OUT)

    @property
    def aborted_requests(self):
        return _RecordList(record for record in self._records
                           if record.status == RequestStatus.ABORTED)

    def by_status(self, status):
        wanted = RequestStatus.normalize(status)
        if wanted == RequestStatus.PENDING:
            return _RecordList(record for record in self._records
                               if record.status in RequestStatus.ACTIVE)
        if wanted == 'incomplete':
            return self.incomplete_requests
        return _RecordList(record for record in self._records
                           if RequestStatus.matches(record.status, status))

    select = by_status
    filter = by_status
    filter_by_status = by_status
    records_with_status = by_status

    def succeeded(self):
        return self.successful_requests

    def failed(self):
        return self.failed_requests

    def pending(self):
        return self.pending_requests

    def incomplete(self):
        return self.incomplete_requests

    def cancelled(self):
        return self.cancelled_requests

    def timed_out(self):
        return self.timed_out_requests

    @property
    def successful(self):
        return self.successful_requests

    @property
    def success(self):
        return self.successful_requests

    @property
    def failures(self):
        return self.failed_requests

    @property
    def failure(self):
        return self.failed_requests

    @property
    def incomplete_records(self):
        return self.incomplete_requests

    def by_outcome(self, outcome):
        """Return records by coarse outcome: success, failure or incomplete."""
        outcome = str(outcome).strip().lower()
        if outcome in ('success', 'succeeded', 'ok', 'completed'):
            return self.successful_requests
        if outcome in ('failure', 'failed', 'error', 'errors'):
            return _RecordList(record for record in self._records if record.is_failure)
        if outcome in ('incomplete', 'pending', 'outstanding', 'unfinished'):
            return self.incomplete_requests
        return _RecordList(record for record in self._records
                           if record.outcome == outcome)

    def outcome_counts(self):
        counts = {'success': 0, 'failure': 0, 'incomplete': 0}
        for record in self._records:
            counts[record.outcome] += 1
        return counts

    def by_mid(self, mid):
        return _RecordList(record for record in self._records
                           if record.mid_start <= int(mid) <= record.mid_end)

    def by_message_id(self, mid):
        return self.by_mid(mid)

    def in_interval(self, start, end):
        return _RecordList(record for record in self._records
                           if not (record.mid_end < start or record.mid_start > end))

    @property
    def events(self):
        return [dict(event) for event in self._events]

    def __iter__(self):
        return iter(list(self._records))

    def __len__(self):
        return len(self._records)

    def __getitem__(self, key):
        if isinstance(key, int) and key in self._by_id:
            return self._by_id[key]
        return self._records[key]

    # -- Credit accounting ------------------------------------------------

    @property
    def reserved_credits(self):
        return self._reserved_credits

    @property
    def in_flight_credits(self):
        return self._reserved_credits

    @property
    def available_credits(self):
        if self.initial_credits is None:
            return None
        return self.initial_credits - self._reserved_credits

    @property
    def credits_granted(self):
        return self.total_granted_credits

    @property
    def server_credits(self):
        return self.total_granted_credits

    @property
    def remaining_credits(self):
        return self.available_credits

    @property
    def credits_remaining(self):
        return self.available_credits

    def consume_credits(self, amount):
        """
        Record credits granted by the server in a response.

        The default balance tracks the local in-flight budget (initial credits
        minus reservations). Granted credits are accumulated separately so they
        can be inspected without inflating the final remaining balance.
        """
        amount = int(amount or 0)
        if amount <= 0:
            return self.available_credits
        self.total_granted_credits += amount
        return self.available_credits

    gain_credits = consume_credits
    add_credits = consume_credits

    @property
    def credit_balance(self):
        return self.available_credits

    @property
    def total_bytes_delivered(self):
        return sum(record.delivered_bytes for record in self._records)

    @property
    def total_delivered_bytes(self):
        return self.total_bytes_delivered

    @property
    def in_flight_ranges(self):
        return self.allocator.reserved_ranges

    @property
    def reserved_ranges(self):
        return self.allocator.reserved_ranges

    # -- Allocation/registration -----------------------------------------

    def register(self, command=None, credit_charge=None, session_id=0,
                 async_id=0, mid=None, generation=None, request_id=None,
                 size=None, mid_already_reserved=False, reserve_credits=True):
        """
        Register a request and reserve its MessageId range and credits.

        Returns the :class:`RequestRecord`.  If an active record already exists
        for the same generation and MessageId (which can happen when a caller
        retries the same message object), that record is returned instead of
        double-reserving credits.
        """

        generation = self.generation if generation is None else int(generation)

        if credit_charge is None:
            credit_charge = credit_charge_for_size(size or 1)
        credit_charge = max(1, int(credit_charge))

        if mid is not None and int(mid) == 0:
            # In this codebase MID 0 means "assign the next free MessageId".
            mid = None

        if mid is not None:
            ival = int(mid)
            existing = self.find(mid=ival, generation=generation, active_only=True)
            if existing is not None:
                if existing.credit_charge == credit_charge:
                    return existing
                raise MessageIdRangeConflict(
                    'MessageId %d is already active for request %r with charge %d' %
                    (ival, existing.request_id, existing.credit_charge))
            allocated = False
        else:
            mid = self.allocator.allocate(credit_charge, generation=generation)
            allocated = True

        if not allocated and not mid_already_reserved:
            if not self.allocator.is_free(mid, credit_charge):
                raise MessageIdRangeConflict(
                    'MessageId range %d..%d is already in use' %
                    (mid, mid + credit_charge - 1))
            self.allocator.reserve(mid, credit_charge, generation=generation)

        try:
            if reserve_credits:
                self._reserve_credits(credit_charge)
        except Exception:
            if not mid_already_reserved:
                self.allocator.release(mid)
            raise

        if request_id is None:
            request_id = self._next_request_id
            while request_id in self._by_id:
                request_id += 1
            self._next_request_id = request_id + 1
        else:
            self._next_request_id = max(self._next_request_id, int(request_id) + 1)

        record = RequestRecord(request_id, mid, credit_charge, command=command,
                               session_id=session_id, async_id=async_id,
                               generation=generation)
        self._records.append(record)
        self._by_id[record.request_id] = record
        for message_id in record.message_ids:
            self._by_mid.setdefault((generation, message_id), []).append(record)
        if record.async_id:
            self._by_async_id.setdefault((generation, record.async_id), []).append(record)

        self._append_event('allocated', record,
                           message_id_range=record.mid_range,
                           credits_reserved=credit_charge)
        return record

    # Common aliases for register().
    allocate = register
    reserve = register
    register_request = register
    allocate_request = register
    reserve_request = register

    def _reserve_credits(self, charge):
        if self.initial_credits is not None and self.enforce_budget:
            available = self.initial_credits - self._reserved_credits
            if charge > available:
                raise InsufficientCredits(
                    'Request needs %d credits but only %d are available' %
                    (charge, available))
        self._reserved_credits += charge

    def _release_record(self, record):
        if record.credits_released:
            return False
        record.credits_released = True
        self.allocator.release(record.mid, record.credit_charge, generation=record.generation)
        self._reserved_credits = max(0, self._reserved_credits - record.credit_charge)
        self._append_event('credits_released', record,
                           credits_released=record.credit_charge)
        return True

    # -- Lookup -----------------------------------------------------------

    def get(self, request_id):
        return self._by_id.get(request_id)

    get_request = get

    def find(self, request_id=None, mid=None, message_id=None, async_id=None,
             generation=None, active_only=False, status=None, session_id=None,
             first_mid_only=False):
        if request_id is not None:
            record = self._by_id.get(request_id)
            if record is None:
                return None
            if generation is not None and record.generation != generation:
                return None
            if active_only and not record.is_active:
                return None
            if status is not None and not RequestStatus.matches(record.status, status):
                return None
            if session_id is not None and record.session_id != session_id:
                return None
            return record

        if mid is None:
            mid = message_id

        candidates = self._records
        if generation is not None:
            candidates = [record for record in candidates if record.generation == generation]
        if mid is not None:
            mid = int(mid)
            if first_mid_only:
                candidates = [record for record in candidates if record.mid_start == mid]
            else:
                candidates = [record for record in candidates
                              if record.mid_start <= mid <= record.mid_end]
        if async_id is not None:
            async_id = int(async_id)
            candidates = [record for record in candidates if record.async_id == async_id]
        if session_id is not None:
            candidates = [record for record in candidates if record.session_id == session_id]
        if active_only:
            candidates = [record for record in candidates if record.is_active]
        if status is not None:
            candidates = [record for record in candidates
                          if RequestStatus.matches(record.status, status)]

        if not candidates:
            return None
        # Prefer the newest identity if old generations are still in history.
        return max(candidates, key=lambda record: (record.request_id, record.generation))

    find_request = find
    find_by_mid = find
    find_by_message_id = find
    find_by_async_id = find

    # -- Lifecycle transitions -------------------------------------------

    def mark_pending(self, record=None, async_id=None, session_id=None,
                     generation=None, request_id=None, mid=None):
        record = self._resolve_record(record, request_id=request_id, mid=mid,
                                      generation=generation)
        if record is None:
            self._append_event('unmatched_pending', None, async_id=async_id, mid=mid,
                               generation=generation)
            return None

        if record.is_terminal:
            self._append_event('late_pending', record, async_id=async_id)
            return None

        if async_id is not None:
            if record.async_id and record.async_id != async_id:
                self._append_event('async_id_mismatch', record, async_id=async_id)
                return None
            record.async_id = int(async_id)
            self._by_async_id.setdefault((record.generation, record.async_id), []).append(record)
        if session_id:
            record.session_id = session_id
            record.session = session_id

        if record.status == RequestStatus.ASYNC_PENDING and record.async_id == (async_id or record.async_id):
            self._append_event('duplicate_pending', record, async_id=record.async_id)
            return record

        record.status = RequestStatus.ASYNC_PENDING
        record.state = record.status
        self._append_event('async_pending', record, async_id=record.async_id)
        return record

    mark_async_pending = mark_pending
    pending_response = mark_pending

    def complete(self, record=None, status=STATUS_SUCCESS, delivered_bytes=None,
                 async_id=None, session_id=None, generation=None, request_id=None,
                 mid=None, response_mid=None, response_async_id=None,
                 response_session_id=None, response_credit_charge=None,
                 credits_granted=None):
        """
        Finish a request with the given SMB2 response status.

        The first terminal transition releases the reserved credits.  Subsequent
        (duplicate or late) transitions are recorded but never release credits
        again.
        """

        record = self._resolve_record(record, request_id=request_id, mid=mid,
                                      generation=generation, async_id=async_id)
        if record is None:
            self._append_event('unmatched_response', None, status=status,
                               async_id=async_id, mid=mid, generation=generation)
            return None

        if record.is_terminal:
            record.duplicate_completions += 1
            self._append_event('duplicate_completion' if record.response_status is not None
                               else 'late_completion',
                               record, status=status, delivered_bytes=delivered_bytes)
            return None

        record.response_status = status
        record.response_mid = response_mid if response_mid is not None else mid
        record.response_async_id = response_async_id if response_async_id is not None else async_id
        record.response_session_id = (response_session_id if response_session_id is not None
                                      else session_id)
        if response_credit_charge is not None:
            record.response_credit_charge = response_credit_charge
        if credits_granted is not None:
            record.response_credits_granted = credits_granted
            self.total_granted_credits += credits_granted
        if delivered_bytes is not None:
            record.delivered_bytes = int(delivered_bytes)
        if async_id is not None:
            record.async_id = int(async_id)
        if session_id:
            record.session_id = session_id
            record.session = session_id

        record.status = RequestStatus.SUCCESS if status == STATUS_SUCCESS else RequestStatus.FAILED
        record.state = record.status
        record.completed_at = time.time()
        self._release_record(record)
        self._append_event('completed', record, status=status,
                           delivered_bytes=record.delivered_bytes)
        return record

    mark_complete = complete
    mark_completed = complete
    finish = complete
    on_response = complete
    handle_response = complete

    def succeed(self, record=None, delivered_bytes=None, **kwargs):
        return self.complete(record, status=STATUS_SUCCESS,
                             delivered_bytes=delivered_bytes, **kwargs)

    def fail(self, record=None, status=1, delivered_bytes=None, **kwargs):
        if status == STATUS_SUCCESS:
            status = 1
        return self.complete(record, status=status,
                             delivered_bytes=delivered_bytes, **kwargs)

    def cancel(self, record=None, request_id=None, mid=None, async_id=None,
               generation=None):
        record = self._resolve_record(record, request_id=request_id, mid=mid,
                                      async_id=async_id, generation=generation)
        if record is None:
            self._append_event('unmatched_cancel', None, mid=mid, request_id=request_id)
            return None
        if record.is_terminal:
            self._append_event('duplicate_cancel', record)
            return None
        record.status = RequestStatus.CANCELLED
        record.state = record.status
        record.completed_at = time.time()
        self._release_record(record)
        self._append_event('cancelled', record)
        return record

    def cancel_all(self, generation=None, status=RequestStatus.CANCELLED):
        """Cancel/abort every still-active request in a generation."""

        affected = []
        for record in list(self._records):
            if not record.is_active:
                continue
            if generation is not None and record.generation != generation:
                continue
            record.status = status
            record.state = record.status
            record.completed_at = time.time()
            self._release_record(record)
            self._append_event('cancelled' if status == RequestStatus.CANCELLED else 'aborted',
                               record)
            affected.append(record)
        return affected

    def timeout(self, record=None, request_id=None, mid=None, async_id=None,
                generation=None):
        record = self._resolve_record(record, request_id=request_id, mid=mid,
                                      async_id=async_id, generation=generation)
        if record is None:
            self._append_event('unmatched_timeout', None, mid=mid, request_id=request_id)
            return None
        if record.is_terminal:
            self._append_event('duplicate_timeout', record)
            return None
        record.status = RequestStatus.TIMED_OUT
        record.state = record.status
        record.completed_at = time.time()
        self._release_record(record)
        self._append_event('timed_out', record)
        return record

    timed_out = timeout
    timeout_request = timeout
    cancel_request = cancel

    # -- Message driven transitions --------------------------------------

    def handle_message(self, message=None, status=None, mid=None, async_id=None,
                       session_id=None, generation=None, delivered_bytes=None,
                       is_async=None, is_reply=None):
        """
        Apply an incoming SMB2 message (or explicit message fields).

        Returns the affected record on a real transition, or ``None`` for an
        unmatched/late/duplicate response.  Late and duplicate responses are
        still appended to the replay log.
        """

        if status is None:
            status = getattr(message, 'status', STATUS_SUCCESS)
        if mid is None:
            mid = getattr(message, 'mid', None)
        if async_id is None:
            async_id = getattr(message, 'async_id', 0)
        if session_id is None:
            session_id = getattr(message, 'session_id', 0)
        if generation is None:
            generation = self.generation
        if is_async is None:
            is_async = bool(getattr(message, 'isAsync', False))
        if is_reply is None:
            is_reply = bool(getattr(message, 'isReply', True))

        if not is_reply:
            self._append_event('ignored_request', None, mid=mid,
                               async_id=async_id, generation=generation)
            return None

        record = None
        if async_id:
            record = self.find(async_id=async_id, generation=generation, active_only=True)
            if record is None:
                record = self.find(async_id=async_id, generation=generation)
        if record is None and mid is not None and not async_id:
            # A synchronous response belongs to the request whose occupied
            # MessageId range contains the response MID.
            record = self.find(mid=mid, generation=generation, active_only=True)
            if record is None:
                record = self.find(mid=mid, generation=generation)

        if record is None:
            self._append_event('unmatched_response', None, status=status, mid=mid,
                               async_id=async_id, generation=generation)
            return None

        if record.generation != generation:
            self._append_event('generation_mismatch', record, status=status,
                               mid=mid, async_id=async_id, generation=generation)
            return None

        if (session_id and record.session_id and
                int(session_id) != int(record.session_id)):
            self._append_event('session_mismatch', record, status=status,
                               session_id=session_id)
            return None

        if status == STATUS_PENDING:
            return self.mark_pending(record, async_id=async_id,
                                     session_id=session_id)

        credits_granted = getattr(message, 'credit_re', None) if message is not None else None
        return self.complete(record, status=status,
                             delivered_bytes=delivered_bytes,
                             async_id=async_id or None,
                             session_id=session_id or None,
                             generation=generation,
                             response_mid=mid,
                             response_async_id=async_id,
                             response_session_id=session_id,
                             response_credit_charge=getattr(message, 'credit_charge', None)
                             if message is not None else None,
                             credits_granted=credits_granted)

    on_message = handle_message
    process_message = handle_message
    handle_message_response = handle_message

    # -- Generations/reconnects ------------------------------------------

    def begin_generation(self, reset_mid=True, start_mid=None, abort_active=True):
        """
        Start a new connection generation.

        Outstanding requests from the previous generation are marked aborted and
        release their credits once.  Their MessageId ranges remain in history but
        can no longer claim replies delivered in the new generation.
        """

        previous = self.generation
        self.generation += 1
        self.generation_started_at = time.time()

        if abort_active:
            for record in list(self._records):
                if record.generation == previous and record.is_active:
                    record.status = RequestStatus.ABORTED
                    record.state = record.status
                    record.completed_at = time.time()
                    self._release_record(record)
                    self._append_event('aborted', record, previous_generation=previous)

        if reset_mid:
            self.allocator.reset(start_mid if start_mid is not None else self.allocator.start_mid)

        self._append_event('generation_started', None,
                           previous_generation=previous,
                           generation=self.generation,
                           reset_mid=bool(reset_mid))
        return self.generation

    new_generation = begin_generation
    reconnect = begin_generation

    # -- Replay/serialization --------------------------------------------

    def replay(self, since=0):
        """Return replayable transition events, optionally starting at *since*."""

        return [dict(event) for event in self._events
                if event.get('sequence', 0) >= since]

    def replay_events(self, since=0):
        return self.replay(since)

    def replay_for(self, record):
        record = self._resolve_record(record)
        if record is None:
            return []
        return [dict(event) for event in record.events]

    def snapshot(self):
        return {
            'generation': self.generation,
            'initial_credits': self.initial_credits,
            'reserved_credits': self.reserved_credits,
            'available_credits': self.available_credits,
            'remaining_credits': self.available_credits,
            'total_granted_credits': self.total_granted_credits,
            'total_delivered_bytes': self.total_delivered_bytes,
            'next_mid': self.allocator.next_mid,
            'reserved_ranges': self.allocator.reserved_ranges,
            'counts': self.outcome_counts(),
            'records': [record.to_dict() for record in self._records],
            'events': self.events,
        }

    to_dict = snapshot

    # -- Internal helpers -------------------------------------------------

    def _resolve_record(self, record=None, request_id=None, mid=None,
                        async_id=None, generation=None):
        if isinstance(record, RequestRecord):
            return record
        if record is not None:
            if isinstance(record, int):
                found = self._by_id.get(record)
                if found is not None:
                    return found
                mid = record
            elif isinstance(record, dict):
                request_id = record.get('request_id', record.get('id'))

        if request_id is not None:
            found = self._by_id.get(request_id)
            if found is not None:
                return found

        return self.find(mid=mid, async_id=async_id, generation=generation,
                         active_only=True)

    def _append_event(self, event_type, record=None, **fields):
        self._event_sequence += 1
        event = {
            'sequence': self._event_sequence,
            'type': event_type,
            'event': event_type,
            'timestamp': time.time(),
            'generation': self.generation,
            'request_id': record.request_id if record is not None else None,
            'mid': record.mid if record is not None else fields.get('mid'),
            'mid_start': record.mid_start if record is not None else None,
            'mid_end': record.mid_end if record is not None else None,
            'session_id': record.session_id if record is not None else fields.get('session_id'),
            'async_id': record.async_id if record is not None else fields.get('async_id'),
            'credit_charge': record.credit_charge if record is not None else None,
            'status': record.status if record is not None else fields.get('status'),
            'available_credits': self.available_credits,
            'delivered_bytes': record.delivered_bytes if record is not None else fields.get('delivered_bytes'),
        }
        event.update(fields)
        self._events.append(event)
        if record is not None:
            record.events.append(dict(event))
        return event


# Friendly aliases.  The implementation is transport-neutral and works for
# any SMB2 client, hence the shorter names.
RequestLedger = SMB2RequestLedger
SMB2CreditLedger = SMB2RequestLedger
CreditLedger = SMB2RequestLedger
SMB2RequestTracker = SMB2RequestLedger
RequestTracker = SMB2RequestLedger
