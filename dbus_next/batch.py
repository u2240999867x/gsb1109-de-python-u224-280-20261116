'''Batch collection of DBus method calls with strict request/reply correlation.

A batch issues many method calls over a :class:`MessageBus
<dbus_next.message_bus.BaseMessageBus>` and tallies the outcome of each call.
Every call ends in exactly one of four failure categories or a success:

- ``bus_rejected``: the bus daemon refused the call. The explicit DBus error
  name (e.g. ``org.freedesktop.DBus.Error.AccessDenied``) is preserved.
- ``application_error``: the call reached the service and the service replied
  with an error. The explicit DBus error name (``Message.error_name``) is
  preserved.
- ``disconnected``: the connection went away before a reply was observed. The
  result is therefore unknown and is deliberately *not* counted as an
  application failure.
- ``no_result``: the batch ended without a reply or disconnection for the call
  (for example a timeout while the connection stayed up).

Request/reply correlation

Calls are matched to replies on both the connection generation and the serial.
The serial alone is not enough because it is reused: every fresh connection
starts its serials again at one. A reply left over from an old connection whose
serial has been reused by a call on the new connection must never complete that
new call. Each connection therefore gets a monotonic ``generation`` and a call
is only ever completed by the reply handler of its own ``(generation,
serial)`` record.
'''

from .constants import MessageType

from enum import Enum
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


# Error names in the namespace reserved by D-Bus for errors raised by the bus
# daemon itself, as opposed to errors raised by the application a call was
# addressed to. Matching the prefix keeps the rule correct for bus errors that
# are not enumerated explicitly.
_BUS_ERROR_PREFIX = 'org.freedesktop.DBus.Error.'


class CallOutcome(Enum):
    '''The terminal classification of a single method call.'''
    SUCCESS = 'success'
    BUS_REJECTED = 'bus_rejected'
    APPLICATION_ERROR = 'application_error'
    DISCONNECTED = 'disconnected'
    NO_RESULT = 'no_result'


class CallRecord:
    '''The outcome of one method call within a batch.

    :ivar key: User supplied key identifying the call (or the message serial if
        none was given).
    :ivar serial: The serial of the outgoing method call message.
    :ivar generation: The connection generation the call was issued on.
    :ivar outcome: How the call terminated.
    :vartype outcome: :class:`CallOutcome`
    :ivar error_name: The explicit DBus error name for a bus rejection or
        application error, otherwise ``None``.
    :ivar error_text: Human readable text accompanying an error, if any.
    :ivar reply: The raw reply :class:`Message <dbus_next.Message>` for a
        success or application error, otherwise ``None``.
    :ivar disconnect_error: The connection exception that terminated an
        unresolved call, otherwise ``None``.
    '''

    __slots__ = (
        'key', 'serial', 'generation', 'outcome', 'error_name', 'error_text',
        'reply', 'disconnect_error')

    def __init__(self, key: Any, serial: int, generation: int):
        self.key = key
        self.serial = serial
        self.generation = generation
        self.outcome: Optional[CallOutcome] = None
        self.error_name: Optional[str] = None
        self.error_text: Optional[str] = None
        self.reply = None
        self.disconnect_error: Optional[BaseException] = None

    @property
    def resolved(self) -> bool:
        '''True once the call has a terminal outcome.'''
        return self.outcome is not None

    def __repr__(self):
        return (f'CallRecord(key={self.key!r}, serial={self.serial}, '
                f'generation={self.generation}, outcome={self.outcome!r}, '
                f'error_name={self.error_name!r})')


class BatchResult:
    '''The tallied results of a completed batch.

    Counts are available both as attributes and through item access using the
    :class:`CallOutcome` value or its name. The four failure categories are
    kept separate from the number of successful calls.
    '''

    def __init__(self):
        self.success = 0
        self.bus_rejected = 0
        self.application_error = 0
        self.disconnected = 0
        self.no_result = 0
        # explicit error name -> number of occurrences
        self.bus_error_names: Dict[str, int] = {}
        self.application_error_names: Dict[str, int] = {}
        self.records: List[CallRecord] = []

    def _count(self, record: CallRecord):
        outcome = record.outcome
        if outcome is CallOutcome.SUCCESS:
            self.success += 1
        elif outcome is CallOutcome.BUS_REJECTED:
            self.bus_rejected += 1
            self._tally_name(self.bus_error_names, record.error_name)
        elif outcome is CallOutcome.APPLICATION_ERROR:
            self.application_error += 1
            self._tally_name(self.application_error_names, record.error_name)
        elif outcome is CallOutcome.DISCONNECTED:
            self.disconnected += 1
        elif outcome is CallOutcome.NO_RESULT:
            self.no_result += 1
        self.records.append(record)

    @staticmethod
    def _tally_name(counter: Dict[str, int], name: Optional[str]):
        if name is None:
            name = ''
        counter[name] = counter.get(name, 0) + 1

    @property
    def total(self) -> int:
        return len(self.records)

    @property
    def failures(self) -> int:
        '''All calls that did not succeed, across every failure category.'''
        return sum((self.bus_rejected, self.application_error,
                    self.disconnected, self.no_result))

    def __getitem__(self, outcome):
        if isinstance(outcome, str):
            outcome = CallOutcome(outcome)
        return getattr(self, outcome.value)

    def as_dict(self) -> Dict[str, int]:
        '''Plain mapping of outcome name to count.'''
        return {outcome.value: self[outcome] for outcome in CallOutcome}

    def __repr__(self):
        return (f'BatchResult(total={self.total}, success={self.success}, '
                f'bus_rejected={self.bus_rejected}, '
                f'application_error={self.application_error}, '
                f'disconnected={self.disconnected}, no_result={self.no_result})')


class BatchCollector:
    '''Issue a batch of method calls and classify every outcome.

    The collector is transport agnostic: it only relies on the low level bus
    ``_call`` API and works with both the asyncio and GLib message bus
    implementations. A single collector can follow a logical bus across
    reconnects. Pass the new bus to :meth:`note_connection` after each
    reconnect so a fresh generation begins.

    :param bus: The message bus used for the first connection.
    :param bus_error_names: Optional explicit set of error names that count as
        bus rejections. When omitted, any error in the
        ``org.freedesktop.DBus.Error.`` namespace is treated as a bus rejection
        and every other error as an application error.
    '''

    def __init__(self, bus, bus_error_names: Optional[Iterable[str]] = None):
        self._bus = bus
        if bus_error_names is None:
            self._is_bus_error = lambda name: (
                name is not None and name.startswith(_BUS_ERROR_PREFIX))
        else:
            names = frozenset(bus_error_names)
            self._is_bus_error = lambda name: name in names

        # (generation, serial) -> pending record. The composite key is what
        # keeps a reused serial on a new connection distinct from the old one.
        self._pending: Dict[Tuple[int, int], CallRecord] = {}
        self._records: List[CallRecord] = []
        self._generation = 0
        self._generation_open = False

    @property
    def generation(self) -> int:
        '''The current connection generation (0 before the first connection).'''
        return self._generation

    @property
    def pending(self) -> int:
        '''Number of calls still awaiting a reply across all generations.'''
        return len(self._pending)

    def note_connection(self, bus=None) -> int:
        '''Begin a new connection generation.

        Call once after each successful (re)connect. Pass the freshly connected
        bus when reconnecting produced a new bus object; its serials restart at
        one but the bumped generation keeps its calls distinct from the previous
        connection. Pending calls from prior generations keep their own
        generation and can never be completed by a reply on the new connection.
        Returns the new generation number.
        '''
        if bus is not None:
            self._bus = bus
        self._generation += 1
        self._generation_open = True
        return self._generation

    def note_disconnect(self, error: Optional[BaseException] = None):
        '''Mark the current connection as disconnected.

        Every call still awaiting a reply on the current generation is recorded
        as :attr:`CallOutcome.DISCONNECTED`: its result is unknown, so it is
        never counted as an application error.
        '''
        self._generation_open = False
        for (generation, _serial), record in list(self._pending.items()):
            if generation == self._generation:
                self._finish(record, CallOutcome.DISCONNECTED,
                             disconnect_error=error)

    def submit(self, message, key: Any = None) -> CallRecord:
        '''Issue a method call and register it for correlation.

        :param message: The method call :class:`Message <dbus_next.Message>` to
            send. Its serial is assigned by the bus if unset.
        :param key: Optional caller supplied identifier for the call. Defaults
            to the message serial once assigned.
        '''
        if not self._generation_open:
            raise RuntimeError(
                'cannot submit a call before the connection is established '
                '(call note_connection first)')
        if not message.serial:
            message.serial = self._bus.next_serial()

        record = CallRecord(key if key is not None else message.serial,
                            message.serial, self._generation)
        self._pending[(record.generation, record.serial)] = record
        self._records.append(record)

        self._bus._call(message, self._make_callback(record))
        return record

    def finish(self) -> BatchResult:
        '''Resolve any outstanding calls and return the tallied result.

        Calls still pending on an open connection are recorded as
        :attr:`CallOutcome.NO_RESULT`. Pending calls on a disconnected
        generation have already been classified by :meth:`note_disconnect`.
        '''
        for record in self._records:
            if not record.resolved:
                self._finish(record, CallOutcome.NO_RESULT)

        result = BatchResult()
        for record in self._records:
            result._count(record)
        self._pending.clear()
        return result

    def _make_callback(self, record: CallRecord) -> Callable:
        # The closure captures the specific record (and hence its generation).
        # A reply delivered on another connection cannot reach this callback,
        # and a late reply after the record resolved is ignored, so a reused
        # serial can never complete the wrong call.
        def reply_callback(reply, err):
            if record.resolved:
                # A late reply after disconnection can never reopen a resolved
                # call, and a reused serial on another generation is bound to a
                # different record.
                return
            if err is not None:
                self._finish(record, CallOutcome.DISCONNECTED,
                             disconnect_error=err)
                return
            # Strict correlation: only a reply whose reply_serial addresses this
            # record's serial on its own generation may complete it.
            if reply.reply_serial != record.serial:
                return
            self._classify_reply(record, reply)

        return reply_callback

    def _classify_reply(self, record: CallRecord, reply):
        if reply.message_type == MessageType.METHOD_RETURN:
            self._finish(record, CallOutcome.SUCCESS, reply=reply)
        elif reply.message_type == MessageType.ERROR:
            error_name = reply.error_name
            error_text = reply.body[0] if reply.body else None
            outcome = (CallOutcome.BUS_REJECTED
                       if self._is_bus_error(error_name)
                       else CallOutcome.APPLICATION_ERROR)
            record.error_name = error_name
            record.error_text = error_text
            self._finish(record, outcome, reply=reply)

    def _finish(self, record: CallRecord, outcome: CallOutcome, reply=None,
                disconnect_error: Optional[BaseException] = None):
        if record.resolved:
            return
        record.outcome = outcome
        record.reply = reply
        record.disconnect_error = disconnect_error
        self._pending.pop((record.generation, record.serial), None)
