"""Tests for semantic match rule sharing and registration transitions.

These tests don't need a running D-Bus daemon: they use a fake bus object that
records the AddMatch/RemoveMatch traffic and lets the test decide when and how
each request is confirmed.
"""

from dbus_next._private.match_rule import MatchRule
from dbus_next._private.match_registry import MatchRuleRegistry
from dbus_next.constants import MessageType
from dbus_next.errors import InvalidMatchRuleError
from dbus_next.message import Message

import pytest


class FakeBus:
    def __init__(self):
        self._connection_ready = True
        self._connection_generation = 0
        self._name_owners = {}
        self.unique_name = ':1.1'
        self._serial = 0
        self.requests = []
        self._method_return_handlers = {}
        self._user_message_handlers = []
        self._match_rules = {}
        self._match_registry = MatchRuleRegistry(self)

    def next_serial(self):
        self._serial += 1
        return self._serial

    def _call(self, msg, callback):
        self.requests.append([msg, callback])

    def add_message_handler(self, handler):
        self._user_message_handlers.append(handler)

    def remove_message_handler(self, handler):
        for i, h in enumerate(self._user_message_handlers):
            if h == handler:
                del self._user_message_handlers[i]
                break

    def _sync_match_rule_view(self):
        self._match_rules.clear()
        for rule_string, status in self._match_registry.registrations().items():
            demand = sum(c['demand'] for c in status['consumers'])
            if demand:
                self._match_rules[rule_string] = demand

    def last(self, member, rule_string=None):
        matches = [
            request for request in self.requests
            if all([
                request[0].member == member,
                rule_string is None or request[0].body[0] == rule_string,
            ])
        ]
        assert matches, f'no {member} request for {rule_string!r}'
        return matches[-1]

    def count(self, member, rule_string=None):
        return len([
            request for request in self.requests
            if all([
                request[0].member == member,
                rule_string is None or request[0].body[0] == rule_string,
            ])
        ])

    def confirm_newest(self, member, rule_string=None, error=False):
        msg, callback = self.last(member, rule_string)
        reply = None
        err = None
        if error:
            reply = Message(message_type=MessageType.ERROR,
                            reply_serial=msg.serial,
                            error_name='org.freedesktop.DBus.Error.MatchRuleInvalid',
                            signature='s',
                            body=['bad rule'])
        else:
            reply = Message(message_type=MessageType.METHOD_RETURN, reply_serial=msg.serial)
        callback(reply, err)
        return msg


def signal(path='/a/b', interface='test.iface', member='Changed', body=None, serial=1,
           sender=':1.2', signature=''):
    msg = Message.new_signal(path, interface, member, signature, body or [])
    msg.sender = sender
    msg.serial = serial
    return msg


@pytest.fixture
def bus():
    return FakeBus()


def test_semantic_rules_share_one_registration(bus):
    registry = bus._match_registry

    first = registry.subscribe("type='signal',sender=foo.bar,path=/a/b", 'one')
    second = registry.subscribe("path='/a/b',sender='foo.bar',type='signal'", 'two')

    assert first.rule == second.rule
    assert bus.count('AddMatch') == 1
    bus.confirm_newest('AddMatch')

    assert bus.count('AddMatch') == 1
    status = registry.status('type=signal,sender=foo.bar,path=/a/b')
    assert status['registered'] is True
    assert len(status['consumers']) == 2
    assert {c['status'] for c in status['consumers']} == {'active'}


def test_consumer_withdrawal_keeps_registration_until_last(bus):
    registry = bus._match_registry

    # Separate subscriptions on the same rule object, each one demand.
    first = registry.subscribe("type='signal',path=/a/b", 'one')
    second = registry.subscribe("type='signal',path=/a/b", 'two')
    bus.confirm_newest('AddMatch')

    first.cancel()
    assert bus.count('RemoveMatch') == 0
    assert registry.status("type='signal',path=/a/b")['registered'] is True

    second.cancel()
    assert bus.count('RemoveMatch') == 1
    # Still reported registered while the removal confirmation is awaited.
    status = registry.status("type='signal',path=/a/b")
    assert status['registered'] is True
    assert status['state'] == 'removing'

    bus.confirm_newest('RemoveMatch')
    assert registry.status("type='signal',path=/a/b") is None


def test_remove_is_deferred_until_pending_add_confirms(bus):
    registry = bus._match_registry

    sub = registry.subscribe("type='signal',path=/a/b", 'one')
    assert bus.count('AddMatch') == 1
    sub.cancel()

    # The remove must not race ahead of the AddMatch.
    assert bus.count('RemoveMatch') == 0

    bus.confirm_newest('AddMatch')
    assert bus.count('RemoveMatch') == 1

    bus.confirm_newest('RemoveMatch')
    assert registry.status("type='signal',path=/a/b") is None


def test_new_demand_during_removal_reuses_old_subscription(bus):
    registry = bus._match_registry

    first = registry.subscribe("type='signal',path=/a/b", 'one')
    bus.confirm_newest('AddMatch')

    first.cancel()
    assert bus.count('RemoveMatch') == 1

    # While the removal is in flight the old subscription is still valid.
    second = registry.subscribe("path=/a/b,type=signal", 'two')
    status = registry.status("type='signal',path=/a/b")
    assert status['registered'] is True
    assert second.registered is True

    bus.confirm_newest('RemoveMatch')
    # The old subscription was removed, so a fresh add is needed for the
    # consumer that arrived during the transition.
    assert bus.count('AddMatch') == 2

    bus.confirm_newest('AddMatch')
    assert registry.status("type='signal',path=/a/b")['registered'] is True
    assert bus.count('RemoveMatch') == 1


def test_failed_add_reports_error_and_no_registration(bus):
    registry = bus._match_registry

    sub = registry.subscribe("type='signal',path=/a/b", 'one')
    bus.confirm_newest('AddMatch', error=True)

    status = registry.status("type='signal',path=/a/b")
    assert status['registered'] is False
    assert status['state'] == 'failed'
    assert status['consumers'][0]['status'] == 'error'
    assert sub.registered is False


def test_failed_remove_keeps_registration_promised(bus):
    registry = bus._match_registry

    sub = registry.subscribe("type='signal',path=/a/b", 'one')
    bus.confirm_newest('AddMatch')
    sub.cancel()

    msg, callback = bus.last('RemoveMatch')
    callback(
        Message(message_type=MessageType.ERROR,
                reply_serial=msg.serial,
                error_name='org.freedesktop.DBus.Error.LimitsExceeded',
                signature='s',
                body=['nope']), None)

    status = registry.status("type='signal',path=/a/b")
    assert status['registered'] is True
    assert status['state'] == 'remove_failed'


def test_match_rule_not_found_remove_is_success(bus):
    registry = bus._match_registry

    sub = registry.subscribe("type='signal',path=/a/b", 'one')
    bus.confirm_newest('AddMatch')
    sub.cancel()

    msg, callback = bus.last('RemoveMatch')
    callback(
        Message(message_type=MessageType.ERROR,
                reply_serial=msg.serial,
                error_name='org.freedesktop.DBus.Error.MatchRuleNotFound',
                signature='s',
                body=['not found']), None)

    assert registry.status("type='signal',path=/a/b") is None


def test_stale_confirmation_from_old_generation_is_ignored(bus):
    registry = bus._match_registry

    registry.subscribe("type='signal',path=/a/b", 'one')
    old_msg, old_callback = bus.last('AddMatch')

    # Simulate a reconnect: the old confirmation is invalidated.
    bus._connection_generation += 1
    registry.on_connection_lost()
    registry.on_connection_established()

    new_msg, new_callback = bus.last('AddMatch')
    assert new_msg.serial != old_msg.serial

    # The stale confirmation must not mark the new registration active.
    old_callback(Message(message_type=MessageType.METHOD_RETURN,
                         reply_serial=old_msg.serial), None)
    status = registry.status("type='signal',path=/a/b")
    assert status['registered'] is False
    assert status['state'] == 'adding'

    new_callback(Message(message_type=MessageType.METHOD_RETURN,
                         reply_serial=new_msg.serial), None)
    assert registry.status("type='signal',path=/a/b")['registered'] is True


def test_duplicate_confirmation_does_not_duplicate_registration(bus):
    registry = bus._match_registry

    registry.subscribe("type='signal',path=/a/b", 'one')
    msg, callback = bus.last('AddMatch')
    callback(Message(message_type=MessageType.METHOD_RETURN, reply_serial=msg.serial), None)
    callback(Message(message_type=MessageType.METHOD_RETURN, reply_serial=msg.serial), None)

    assert bus.count('AddMatch') == 1
    assert registry.status("type='signal',path=/a/b")['registered'] is True


def test_duplicate_signal_is_only_dispatched_once(bus):
    registry = bus._match_registry
    registry.subscribe("type='signal',path=/a/b", 'one')
    bus.confirm_newest('AddMatch')

    msg = signal()

    assert registry.is_duplicate_signal(msg) is False
    assert registry.is_duplicate_signal(msg) is True


def test_ambiguous_rules_are_not_shared(bus):
    registry = bus._match_registry

    registry.subscribe("path=/a/b", 'exact')
    registry.subscribe("path_namespace=/a/b", 'namespace')
    registry.subscribe("arg0=foo", 'arg')
    registry.subscribe("arg0path=/foo", 'argpath')

    assert bus.count('AddMatch') == 4


def test_namespace_matching_semantics():
    rule = MatchRule.from_string("type='signal',path_namespace='/a/b'")
    assert rule.matches(signal(path='/a/b')) is True
    assert rule.matches(signal(path='/a/b/c')) is True
    assert rule.matches(signal(path='/a/bc')) is False
    assert rule.matches(signal(path='/a')) is False

    rule = MatchRule.from_string("arg0namespace='org.example'", type='signal')
    assert rule.matches(signal(signature='s', body=['org.example']))
    assert rule.matches(signal(signature='s', body=['org.example.Foo']))
    assert not rule.matches(signal(signature='s', body=['org.examplefoo']))

    rule = MatchRule.from_string("arg0path='/a/b'", type='signal')
    assert rule.matches(signal(signature='o', body=['/a/b']))
    assert rule.matches(signal(signature='o', body=['/a/b/c']))
    assert not rule.matches(signal(signature='o', body=['/a/bc']))


def test_invalid_rules_raise():
    with pytest.raises(InvalidMatchRuleError):
        MatchRule.from_string("not_a_key=value")
    with pytest.raises(InvalidMatchRuleError):
        MatchRule.from_string("type='signal")


class InMemoryBus(FakeBus):
    """A bus that can deliver messages through the real dispatch path."""
    def __init__(self):
        super().__init__()
        self.deliver = []

    def _call(self, msg, callback):
        super()._call(msg, callback)
        if msg.member in ('AddMatch', 'RemoveMatch'):
            self.confirm_newest(msg.member, msg.body[0])

    def subscribe(self, rule, consumer, handler=None):
        parsed = MatchRule.from_string(rule)
        if handler is None:
            wrapped = None
        else:
            def wrapped(msg):
                if parsed.matches(msg, name_owners=self._name_owners):
                    handler(msg)
        subscription = self._match_registry.subscribe(parsed, consumer, wrapped)
        if wrapped is not None:
            self.add_message_handler(wrapped)
        return subscription

    def process(self, msg):
        from dbus_next.message_bus import BaseMessageBus
        BaseMessageBus._process_message(self, msg)


def test_consumers_receive_only_their_matching_signals():
    bus = InMemoryBus()

    first = []
    second = []

    bus.subscribe("type='signal',path='/a/b'", 'one', first.append)
    bus.subscribe("type='signal',path='/a/b',arg0='x'", 'two', second.append)

    assert bus.count('AddMatch') == 2

    bus.process(signal(path='/a/b', signature='s', body=['x'], serial=10))
    bus.process(signal(path='/a/b', signature='s', body=['y'], serial=11))

    # A handler is called once for each distinct signal occurrence.  (The
    # bus calls user handlers before the duplicate-occurrence filter, so the
    # helper above sees one call per delivered message.)
    assert len(first) == 2
    assert len(second) == 1
    assert second[0].body == ['x']


def test_same_consumer_deduplicated_across_shared_rule():
    bus = InMemoryBus()
    calls = []

    bus.subscribe(MatchRule.from_string("path=/a/b"), 'one', calls.append)
    bus.subscribe(MatchRule.from_string("path='/a/b'"), 'one', calls.append)

    assert bus.count('AddMatch') == 1


def test_duplicate_copy_of_signal_is_not_dispatched_twice():
    bus = InMemoryBus()
    calls = []
    bus.subscribe("type='signal',path='/a/b'", 'one', calls.append)

    msg = signal(path='/a/b')
    bus.process(msg)
    bus.process(msg)

    assert len(calls) == 1


def test_reconnect_re_verifies_and_keeps_subscription():
    bus = InMemoryBus()
    calls = []
    bus.subscribe("type='signal',path='/a/b'", 'one', calls.append)
    assert bus.count('AddMatch') == 1

    bus._connection_generation += 1
    bus._user_message_handlers.clear()  # _finalize does this on disconnect
    bus._match_registry.on_connection_lost()
    bus._connection_ready = True
    bus._match_registry.on_connection_established()

    # Verified again on the new connection, and the subscription's handler was
    # restored after the disconnect cleared the handler list.
    assert bus.count('AddMatch') == 2
    assert len(bus._user_message_handlers) == 1

    bus.process(signal(path='/a/b'))
    assert len(calls) == 1


def test_consumer_query_reports_rules_and_state():
    bus = InMemoryBus()
    subscription = bus.subscribe("type='signal',path=/a/b", 'one')

    consumer_rules = bus._match_registry.consumer_rules('one')
    assert len(consumer_rules) == 1
    assert consumer_rules[0]['state'] == 'registered'
    assert consumer_rules[0]['registered'] is True
    assert subscription.status['consumers'][0]['status'] == 'active'

    subscription.cancel()
    assert bus._match_registry.consumer_rules('one') == []


class _FakeProxyInterface:
    pass


def test_proxy_interfaces_share_registration_and_withdraw_independently():
    from dbus_next import introspection as intr
    from dbus_next.proxy_object import BaseProxyInterface

    class ProxyInterface(BaseProxyInterface):
        def _add_method(self, method):
            pass

        def _add_property(self, prop):
            pass

    bus = InMemoryBus()
    bus._name_owners['svc.name'] = ':1.9'

    interface = intr.Interface('com.example.Iface')
    interface.signals.append(intr.Signal('Changed', [intr.Arg('s')]))

    first = ProxyInterface('svc.name', '/p', interface, bus)
    second = ProxyInterface('svc.name', '/p', interface, bus)
    first._add_signal(interface.signals[0], first)
    second._add_signal(interface.signals[0], second)

    first_calls = []
    second_calls = []
    first.on_changed(first_calls.append)
    second.on_changed(second_calls.append)

    # Both proxy interfaces describe the same actual match rule.
    assert bus.count('AddMatch') == 1

    bus.process(signal(path='/p', interface='com.example.Iface', member='Changed',
                       body=['one'], serial=1, sender=':1.9', signature='s'))
    assert first_calls == ['one']
    assert second_calls == ['one']

    for handler in list(first._signal_handlers['Changed']):
        first.off_changed(handler)

    # The second interface keeps its subscription; no RemoveMatch is sent.
    assert bus.count('RemoveMatch') == 0

    bus.process(signal(path='/p', interface='com.example.Iface', member='Changed',
                       body=['two'], serial=2, sender=':1.9', signature='s'))
    assert first_calls == ['one']
    assert second_calls == ['one', 'two']

    for handler in list(second._signal_handlers['Changed']):
        second.off_changed(handler)

    assert bus.count('RemoveMatch') == 1
    assert bus._match_registry.registrations() == {}
