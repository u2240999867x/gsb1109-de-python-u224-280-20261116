"""Shared match rule registration for a message bus connection.

Several consumers (proxy interfaces, low level subscribers, internal caches)
may want the same D-Bus match rule.  The daemon treats AddMatch and
RemoveMatch as reference counted, but a caller must not blindly mirror every
consumer demand as an AddMatch/RemoveMatch pair: confirmations arrive
asynchronously, may fail, and may interleave with a reconnect.

This module keeps one :class:`_MatchRegistration` per *semantic* match rule
and drives a small state machine:

``unregistered``
    No registration is known to exist with the daemon.

``adding``
    AddMatch was sent and not confirmed yet.  Further consumer demand is
    coalesced into this in-flight request.  If all demand disappears while
    adding, removal is deferred until the add is confirmed so that a
    RemoveMatch can never race ahead of its AddMatch.

``registered``
    AddMatch was confirmed.  This is the only state that promises signal
    delivery without further bus traffic.

``removing``
    RemoveMatch was sent.  The old registration is still assumed valid until
    the confirmation arrives.  New demand arriving in this state is queued
    and an AddMatch is sent only after the removal is confirmed, which avoids
    creating an extra daemon reference to the same rule.

``remove_failed``
    RemoveMatch failed for a reason other than MatchRuleNotFound.  The rule is
    conservatively reported as still registered and can be reused by a new
    consumer.

``failed``
    AddMatch failed.  Consumers are told about the error and no registration
    is reported.
"""

from .match_rule import MatchRule
from ..constants import MessageType, ErrorType
from ..errors import DBusError
from ..message import Message

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple


STATE_UNREGISTERED = 'unregistered'
STATE_ADDING = 'adding'
STATE_REGISTERED = 'registered'
STATE_REMOVING = 'removing'
STATE_REMOVE_FAILED = 'remove_failed'
STATE_FAILED = 'failed'

CONSUMER_PENDING = 'pending'
CONSUMER_ACTIVE = 'active'
CONSUMER_ERROR = 'error'


class MatchSubscription:
    """The result of subscribing a handler to a match rule.

    :ivar rule: The semantic match rule that was subscribed.
    :ivar consumer: The opaque consumer identity used for reference counting.
    """
    def __init__(self, registry, rule: MatchRule, consumer, handler=None, original_handler=None):
        self._registry = registry
        self.rule = rule
        self.consumer = consumer
        self._handler = handler
        self._original_handler = original_handler
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def status(self) -> Optional[dict]:
        """The registration status for this subscription's rule."""
        return self._registry.status(self.rule)

    @property
    def registered(self) -> bool:
        status = self.status
        return bool(status and status['registered'])

    def cancel(self) -> None:
        """Withdraw this subscription's demand for the rule."""
        if self._cancelled:
            return
        self._cancelled = True
        self._registry.unsubscribe(self.rule, self.consumer, self._handler)
        if self._handler is not None:
            self._registry.bus.remove_message_handler(self._handler)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, tb):
        self.cancel()
        return False

    def __repr__(self):
        return f'MatchSubscription({self.rule.to_string()!r}, consumer={self.consumer!r})'


class _ConsumerRecord:
    """One consumer's demand for a particular registration."""
    def __init__(self, consumer, handler=None):
        self.consumer = consumer
        # Creating a record consumes exactly one demand, with or without a
        # message handler.
        self.count = 1
        self.handlers: List[Callable] = []
        self.status = CONSUMER_PENDING
        self.error: Optional[Exception] = None
        if handler is not None:
            self.handlers.append(handler)

    def add(self, handler=None):
        self.count += 1
        if handler is not None and handler not in self.handlers:
            self.handlers.append(handler)

    def remove(self, handler=None) -> int:
        self.count -= 1
        if handler is not None and handler in self.handlers:
            self.handlers.remove(handler)
        return self.count

    def set_status(self, status, error=None):
        self.status = status
        self.error = error

    def status_dict(self) -> dict:
        return {
            'consumer': self.consumer,
            'status': self.status,
            'demand': self.count,
            'error': self.error,
        }


class _MatchRegistration:
    """The daemon registration state for one semantic match rule."""
    def __init__(self, rule: MatchRule):
        self.rule = rule
        self.consumers: Dict[Any, _ConsumerRecord] = {}
        self.state = STATE_UNREGISTERED
        self.generation: Optional[int] = None
        self.add_serial: Optional[int] = None
        self.remove_serial: Optional[int] = None
        self.remove_after_add = False
        self.last_error: Optional[Exception] = None

    @property
    def registered(self) -> bool:
        """Whether the daemon is believed to have this rule registered."""
        return self.state in (STATE_REGISTERED, STATE_REMOVING, STATE_REMOVE_FAILED)

    @property
    def in_transition(self) -> bool:
        return self.state in (STATE_ADDING, STATE_REMOVING)

    def add_consumer(self, consumer, handler=None) -> _ConsumerRecord:
        record = self.consumers.get(consumer)
        if record is None:
            record = _ConsumerRecord(consumer, handler)
            self.consumers[consumer] = record
        else:
            record.add(handler)
        return record

    def remove_consumer(self, consumer, handler=None) -> bool:
        """Remove one demand.  Returns True if the consumer is now gone."""
        record = self.consumers.get(consumer)
        if record is None:
            return False
        if record.remove(handler) > 0:
            return False
        del self.consumers[consumer]
        return True

    def has_consumers(self) -> bool:
        return bool(self.consumers)

    def mark_consumers(self, status, error=None):
        for record in self.consumers.values():
            record.set_status(status, error)

    def status_dict(self) -> dict:
        return {
            'rule': self.rule,
            'rule_string': self.rule.to_string(),
            'state': self.state,
            'registered': self.registered,
            'in_transition': self.in_transition,
            'generation': self.generation,
            'add_serial': self.add_serial,
            'remove_serial': self.remove_serial,
            'error': self.last_error,
            'consumers': [record.status_dict() for record in self.consumers.values()],
        }


class MatchRuleRegistry:
    """Owns the semantic match rule registrations for a bus.

    This class is an implementation detail of
    :class:`BaseMessageBus <dbus_next.message_bus.BaseMessageBus>`.  Use the
    query methods of the message bus to inspect it.
    """
    def __init__(self, bus):
        self.bus = bus
        self._registrations: Dict[MatchRule, _MatchRegistration] = {}
        self._anonymous: Dict[MatchRule, List[object]] = {}
        # Signal occurrences are remembered briefly so a duplicate copy of a
        # signal is only dispatched once.
        self._seen_signals = set()
        self._seen_signal_order: List[Tuple[int, Any]] = []
        self._seen_signal_limit = 4096

    # ------------------------------------------------------------------
    # Consumer demand
    # ------------------------------------------------------------------
    def subscribe(self, rule, consumer, handler=None, original_handler=None) -> MatchSubscription:
        """Add one demand for ``rule`` on behalf of ``consumer``."""
        rule = MatchRule.from_string(rule)
        entry = self._get_or_create(rule)
        record = entry.add_consumer(consumer, handler)

        if entry.state == STATE_REGISTERED:
            record.set_status(CONSUMER_ACTIVE)
        elif entry.state == STATE_REMOVING:
            # The old registration is still promised to be valid until the
            # RemoveMatch confirmation arrives, so the new consumer is served
            # by it.  An AddMatch must wait until removal completes.
            entry.mark_consumers(CONSUMER_ACTIVE)
        elif entry.state == STATE_REMOVE_FAILED:
            # RemoveMatch failed: the daemon still has the rule.
            entry.state = STATE_REGISTERED
            entry.last_error = None
            entry.mark_consumers(CONSUMER_ACTIVE)
        elif entry.state in (STATE_UNREGISTERED, STATE_FAILED):
            self._send_add(entry)
        elif entry.state == STATE_ADDING:
            # Coalesce into the in-flight AddMatch.  Any deferred removal is
            # no longer wanted because there is demand again.
            entry.remove_after_add = False
            record.set_status(CONSUMER_PENDING)
        else:
            record.set_status(CONSUMER_PENDING)

        self._sync_view()
        return MatchSubscription(self, rule, consumer, handler, original_handler)

    def unsubscribe(self, rule, consumer, handler=None) -> None:
        """Remove one demand for ``rule`` from ``consumer``."""
        rule = MatchRule.from_string(rule)
        entry = self._registrations.get(rule)
        if entry is None:
            return

        if consumer not in entry.consumers:
            return

        entry.remove_consumer(consumer, handler)
        if entry.has_consumers():
            self._sync_view()
            return

        if entry.state == STATE_ADDING:
            # Do not send RemoveMatch before the pending AddMatch is
            # confirmed.  It would race and the daemon could process the
            # removal first, leaving an unexpected registration behind.
            entry.remove_after_add = True
        elif entry.state == STATE_REGISTERED:
            self._send_remove(entry)
        elif entry.state in (STATE_UNREGISTERED, STATE_FAILED):
            self._prune(entry)
        # In STATE_REMOVING a removal is already in flight.  In
        # STATE_REMOVE_FAILED the daemon may still have the rule; the entry is
        # kept (and reported as registered) until it can be reused or removed.

        self._sync_view()

    def add_anonymous(self, rule) -> MatchRule:
        """Add one anonymous demand and return the semantic rule."""
        rule = MatchRule.from_string(rule)
        token = object()
        self._anonymous.setdefault(rule, []).append(token)
        self.subscribe(rule, token)
        return rule

    def remove_anonymous(self, rule) -> None:
        """Remove one anonymous demand previously added."""
        rule = MatchRule.from_string(rule)
        tokens = self._anonymous.get(rule)
        if not tokens:
            return
        token = tokens.pop()
        if not tokens:
            del self._anonymous[rule]
        self.unsubscribe(rule, token)

    def _get_or_create(self, rule: MatchRule) -> _MatchRegistration:
        entry = self._registrations.get(rule)
        if entry is None:
            entry = _MatchRegistration(rule)
            self._registrations[rule] = entry
        return entry

    def _sync_view(self) -> None:
        """Update the legacy ``_match_rules`` ref count view on the bus."""
        sync = getattr(self.bus, '_sync_match_rule_view', None)
        if sync is not None:
            sync()

    # ------------------------------------------------------------------
    # Daemon calls
    # ------------------------------------------------------------------
    def _send_add(self, entry: _MatchRegistration) -> None:
        if not self.bus._connection_ready:
            # The registration is verified/added when the connection is
            # established.
            entry.state = STATE_UNREGISTERED
            entry.mark_consumers(CONSUMER_PENDING)
            return

        msg = Message(destination='org.freedesktop.DBus',
                      interface='org.freedesktop.DBus',
                      path='/org/freedesktop/DBus',
                      member='AddMatch',
                      signature='s',
                      body=[entry.rule.to_string()])

        generation = self.bus._connection_generation
        entry.state = STATE_ADDING
        entry.generation = generation
        entry.last_error = None
        entry.remove_after_add = False
        entry.mark_consumers(CONSUMER_PENDING)

        def reply_notify(reply, err):
            self._on_add_reply(entry, msg, generation, reply, err)

        msg.serial = self.bus.next_serial()
        entry.add_serial = msg.serial
        try:
            self.bus._call(msg, reply_notify)
        except Exception as e:
            entry.add_serial = None
            self._fail_add(entry, e)

    def _on_add_reply(self, entry, msg, generation, reply, err) -> None:
        current = self._registrations.get(entry.rule)
        stale = not all([
            current is entry,
            generation == self.bus._connection_generation,
            entry.state == STATE_ADDING,
            msg.serial == entry.add_serial,
        ])
        if stale:
            # Duplicate or stale confirmations, including confirmations from
            # an old connection, must not modify the current registration.
            return

        entry.add_serial = None
        error = self._reply_error(reply, err)

        if error is not None:
            self._fail_add(entry, error)
            return

        entry.state = STATE_REGISTERED
        entry.last_error = None
        entry.mark_consumers(CONSUMER_ACTIVE)
        self._sync_view()

        if not entry.has_consumers():
            # All demand disappeared while the add was in flight.  Now that
            # the daemon definitely has the rule, it is safe to remove it.
            self._send_remove(entry)

    def _fail_add(self, entry, error) -> None:
        entry.state = STATE_FAILED
        entry.last_error = error
        logging.error(f'add match request failed. match="{entry.rule.to_string()}", {error}')
        entry.mark_consumers(CONSUMER_ERROR, error)
        if not entry.has_consumers():
            self._prune(entry)
        self._sync_view()

    def _send_remove(self, entry: _MatchRegistration) -> None:
        if not self.bus._connection_ready:
            # Nothing is registered on a connection that is not ready.  If
            # demand appeared again it will be registered when the connection
            # is established.
            entry.state = STATE_UNREGISTERED
            entry.last_error = None
            if not entry.has_consumers():
                self._prune(entry)
            self._sync_view()
            return

        msg = Message(destination='org.freedesktop.DBus',
                      interface='org.freedesktop.DBus',
                      path='/org/freedesktop/DBus',
                      member='RemoveMatch',
                      signature='s',
                      body=[entry.rule.to_string()])

        generation = self.bus._connection_generation
        entry.state = STATE_REMOVING
        entry.generation = generation
        entry.remove_after_add = False

        def reply_notify(reply, err):
            self._on_remove_reply(entry, msg, generation, reply, err)

        msg.serial = self.bus.next_serial()
        entry.remove_serial = msg.serial
        try:
            self.bus._call(msg, reply_notify)
        except Exception as e:
            entry.remove_serial = None
            self._finish_remove(entry, e)

    def _on_remove_reply(self, entry, msg, generation, reply, err) -> None:
        current = self._registrations.get(entry.rule)
        stale = not all([
            current is entry,
            generation == self.bus._connection_generation,
            entry.state == STATE_REMOVING,
            msg.serial == entry.remove_serial,
        ])
        if stale:
            return

        entry.remove_serial = None
        error = self._reply_error(reply, err, match_rule_not_found_ok=True)
        self._finish_remove(entry, error)

    def _finish_remove(self, entry: _MatchRegistration, error) -> None:
        if error is not None:
            # The daemon may still have the rule.  Keep reporting it as
            # registered so that no consumer loses a subscription that was
            # promised to be valid.
            entry.state = STATE_REMOVE_FAILED
            entry.last_error = error
            logging.error(
                f'remove match request failed. match="{entry.rule.to_string()}", {error}')
            if entry.has_consumers():
                entry.state = STATE_REGISTERED
                entry.mark_consumers(CONSUMER_ACTIVE)
            self._sync_view()
            return

        entry.state = STATE_UNREGISTERED
        entry.last_error = None

        if entry.has_consumers():
            # Demand arrived while the removal was in flight.  The old
            # subscription served it until now; register it again.
            self._send_add(entry)
        else:
            self._prune(entry)
        self._sync_view()

    @staticmethod
    def _reply_error(reply, err, match_rule_not_found_ok=False) -> Optional[Exception]:
        if err is not None:
            return err
        if reply is None or reply.message_type != MessageType.ERROR:
            return None
        if match_rule_not_found_ok and reply.error_name == ErrorType.MATCH_RULE_NOT_FOUND.value:
            return None
        text = reply.body[0] if reply.body else reply.error_name
        return DBusError(reply.error_name, text, reply=reply)

    def _prune(self, entry: _MatchRegistration) -> None:
        current = self._registrations.get(entry.rule)
        if current is entry and not entry.has_consumers():
            del self._registrations[entry.rule]

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------
    def on_connection_established(self) -> None:
        """Verify and re-register every rule still wanted by a consumer."""
        for entry in list(self._registrations.values()):
            if not entry.has_consumers():
                self._prune(entry)
                continue

            # A disconnect clears the bus message handler list; restore the
            # handlers owned by subscriptions so consumers keep receiving
            # their signals after a reconnect.
            for record in entry.consumers.values():
                for handler in record.handlers:
                    if handler not in self.bus._user_message_handlers:
                        self.bus.add_message_handler(handler)

            if entry.state in (STATE_UNREGISTERED, STATE_FAILED):
                self._send_add(entry)
            elif entry.state in (STATE_ADDING, STATE_REMOVING):
                # A transition from an older connection is void.
                entry.state = STATE_UNREGISTERED
                self._send_add(entry)
        self._sync_view()

    def on_connection_lost(self) -> None:
        """Invalidate pending confirmations and registration knowledge."""
        for entry in list(self._registrations.values()):
            entry.add_serial = None
            entry.remove_serial = None
            entry.generation = None
            entry.remove_after_add = False
            entry.last_error = None

            if entry.has_consumers():
                entry.state = STATE_UNREGISTERED
                entry.mark_consumers(CONSUMER_PENDING)
            else:
                entry.state = STATE_UNREGISTERED
                self._prune(entry)

        self._seen_signals.clear()
        self._seen_signal_order.clear()
        self._sync_view()

    # ------------------------------------------------------------------
    # Duplicate signal suppression
    # ------------------------------------------------------------------
    def is_duplicate_signal(self, msg) -> bool:
        """Remember a signal occurrence and report duplicate copies.

        Two copies of one signal occurrence have the same sender, serial and
        routing fields.  This makes a duplicated daemon registration harmless
        without suppressing genuinely distinct signal emissions (which always
        have distinct serials on a connection).
        """
        if msg.message_type != MessageType.SIGNAL or not msg.serial:
            return False

        key = (self.bus._connection_generation, msg.sender, msg.serial, msg.path,
               msg.interface, msg.member)
        if key in self._seen_signals:
            return True

        self._seen_signals.add(key)
        self._seen_signal_order.append(key)
        while len(self._seen_signal_order) > self._seen_signal_limit:
            old = self._seen_signal_order.pop(0)
            self._seen_signals.discard(old)
        return False

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def registrations(self) -> Dict[str, dict]:
        """A copy of every known registration keyed by canonical rule string."""
        return {
            entry.rule.to_string(): entry.status_dict()
            for entry in self._registrations.values()
        }

    def registered_rules(self) -> List[str]:
        return [
            entry.rule.to_string() for entry in self._registrations.values()
            if entry.registered
        ]

    def status(self, rule) -> Optional[dict]:
        entry = self._registrations.get(MatchRule.from_string(rule))
        if entry is None:
            return None
        return entry.status_dict()

    def consumers(self, rule) -> List[dict]:
        entry = self._registrations.get(MatchRule.from_string(rule))
        if entry is None:
            return []
        return [record.status_dict() for record in entry.consumers.values()]

    def consumer_rules(self, consumer) -> List[dict]:
        result = []
        for entry in self._registrations.values():
            record = entry.consumers.get(consumer)
            if record is None:
                continue
            result.append({
                'rule': entry.rule,
                'rule_string': entry.rule.to_string(),
                'state': entry.state,
                'registered': entry.registered,
                'status': record.status,
                'demand': record.count,
                'error': record.error,
            })
        return result
