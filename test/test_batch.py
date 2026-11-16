from dbus_next import Message, MessageType
from dbus_next.batch import BatchCollector, CallOutcome

import pytest


def make_call():
    return Message(destination='org.test',
                   path='/test/path',
                   interface='org.test.iface',
                   member='DoWork')


def make_return(serial):
    return Message(message_type=MessageType.METHOD_RETURN, reply_serial=serial)


def make_error(serial, error_name, text='failed'):
    return Message(message_type=MessageType.ERROR,
                   error_name=error_name,
                   reply_serial=serial,
                   signature='s',
                   body=[text])


class FakeBus:
    '''Minimal stand-in for BaseMessageBus covering the _call/serial surface.

    Each instance restarts its serial at one, just like a freshly opened
    connection, which lets the tests reproduce serial reuse across reconnects
    without a real bus daemon.
    '''

    def __init__(self):
        self._serial = 0
        self.handlers = {}

    def next_serial(self):
        self._serial += 1
        return self._serial

    def _call(self, msg, callback):
        if not msg.serial:
            msg.serial = self.next_serial()
        self.handlers[msg.serial] = callback

    def deliver_return(self, serial):
        self.handlers.pop(serial)(make_return(serial), None)

    def deliver_error(self, serial, error_name, text='failed'):
        self.handlers.pop(serial)(make_error(serial, error_name, text), None)

    def drop_connection(self, error):
        # Mirror BaseMessageBus._finalize: every pending call gets the
        # connection error and the handler table is cleared.
        for callback in list(self.handlers.values()):
            callback(None, error)
        self.handlers.clear()


def test_success_bus_rejection_and_application_error_are_separate():
    bus = FakeBus()
    collector = BatchCollector(bus)
    collector.note_connection()

    ok = collector.submit(make_call(), key='ok')
    rejected = collector.submit(make_call(), key='rejected')
    app_failed = collector.submit(make_call(), key='app')

    bus.deliver_return(ok.serial)
    bus.deliver_error(rejected.serial, 'org.freedesktop.DBus.Error.AccessDenied',
                      'not allowed')
    bus.deliver_error(app_failed.serial, 'com.example.Service.Exploded', 'boom')

    result = collector.finish()

    assert result.total == 3
    assert result.success == 1
    assert result.bus_rejected == 1
    assert result.application_error == 1
    assert result.disconnected == 0
    assert result.no_result == 0
    assert result.failures == 2
    assert result.as_dict() == {
        'success': 1,
        'bus_rejected': 1,
        'application_error': 1,
        'disconnected': 0,
        'no_result': 0,
    }
    assert result.bus_error_names == {
        'org.freedesktop.DBus.Error.AccessDenied': 1
    }
    assert result.application_error_names == {'com.example.Service.Exploded': 1}
    assert rejected.outcome is CallOutcome.BUS_REJECTED
    assert rejected.error_name == 'org.freedesktop.DBus.Error.AccessDenied'
    assert rejected.error_text == 'not allowed'
    assert app_failed.outcome is CallOutcome.APPLICATION_ERROR
    assert app_failed.error_name == 'com.example.Service.Exploded'


def test_disconnect_is_unknown_not_application_failure():
    bus = FakeBus()
    collector = BatchCollector(bus)
    collector.note_connection()

    answered = collector.submit(make_call(), key='answered')
    unanswered = collector.submit(make_call(), key='unanswered')
    drop = EOFError('connection closed')

    bus.deliver_return(answered.serial)
    bus.drop_connection(drop)
    collector.note_disconnect(drop)

    result = collector.finish()

    assert result.success == 1
    assert result.disconnected == 1
    assert result.application_error == 0
    assert result.bus_rejected == 0
    assert unanswered.outcome is CallOutcome.DISCONNECTED
    assert unanswered.disconnect_error is drop
    assert unanswered.error_name is None


def test_no_result_when_connection_stays_open_but_no_reply():
    bus = FakeBus()
    collector = BatchCollector(bus)
    collector.note_connection()

    record = collector.submit(make_call())
    result = collector.finish()

    assert record.outcome is CallOutcome.NO_RESULT
    assert result.no_result == 1
    assert result.disconnected == 0


def test_serial_reuse_cannot_let_old_reply_complete_new_call():
    first_bus = FakeBus()
    collector = BatchCollector(first_bus)
    collector.note_connection()

    old_call = collector.submit(make_call(), key='old')
    assert old_call.generation == 1
    assert old_call.serial == 1
    old_callback = first_bus.handlers[old_call.serial]

    drop = ConnectionError('gone')
    first_bus.drop_connection(drop)
    collector.note_disconnect(drop)
    assert old_call.outcome is CallOutcome.DISCONNECTED

    # A new connection restarts serials at one, but the generation is bumped.
    second_bus = FakeBus()
    generation = collector.note_connection(second_bus)
    assert generation == 2

    new_call = collector.submit(make_call(), key='new')
    assert new_call.serial == old_call.serial == 1
    assert new_call.generation == 2
    assert (old_call.generation, old_call.serial) != (
        new_call.generation, new_call.serial)

    # A stale reply from the dead connection carries the reused serial but must
    # not reopen the old call nor complete the new one.
    old_callback(make_return(old_call.serial), None)
    assert old_call.outcome is CallOutcome.DISCONNECTED
    assert not new_call.resolved

    # The genuine reply on the current generation completes only the new call.
    second_bus.deliver_return(new_call.serial)
    assert new_call.outcome is CallOutcome.SUCCESS

    result = collector.finish()
    assert result.disconnected == 1
    assert result.success == 1
    assert result.application_error == 0


def test_reply_with_wrong_reply_serial_is_ignored():
    bus = FakeBus()
    collector = BatchCollector(bus)
    collector.note_connection()

    record = collector.submit(make_call())
    callback = bus.handlers[record.serial]

    callback(make_return(record.serial + 99), None)
    assert not record.resolved

    result = collector.finish()
    assert record.outcome is CallOutcome.NO_RESULT
    assert result.no_result == 1


def test_late_reply_after_resolution_does_not_change_outcome():
    bus = FakeBus()
    collector = BatchCollector(bus)
    collector.note_connection()

    record = collector.submit(make_call())
    callback = bus.handlers[record.serial]

    callback(make_error(record.serial, 'com.example.Service.Bad'), None)
    assert record.outcome is CallOutcome.APPLICATION_ERROR

    callback(make_return(record.serial), None)
    assert record.outcome is CallOutcome.APPLICATION_ERROR
    assert record.error_name == 'com.example.Service.Bad'


def test_custom_bus_error_names():
    bus = FakeBus()
    collector = BatchCollector(bus, bus_error_names={'com.example.BusReject'})
    collector.note_connection()

    custom = collector.submit(make_call())
    standard = collector.submit(make_call())

    bus.deliver_error(custom.serial, 'com.example.BusReject')
    bus.deliver_error(standard.serial, 'org.freedesktop.DBus.Error.NoMemory')

    result = collector.finish()
    assert result.bus_rejected == 1
    assert result.application_error == 1
    assert custom.outcome is CallOutcome.BUS_REJECTED
    assert standard.outcome is CallOutcome.APPLICATION_ERROR


def test_submit_before_connection_is_rejected():
    bus = FakeBus()
    collector = BatchCollector(bus)
    with pytest.raises(RuntimeError):
        collector.submit(make_call())


def test_pending_tracks_composite_generation_serial_key():
    first_bus = FakeBus()
    collector = BatchCollector(first_bus)
    collector.note_connection()
    first = collector.submit(make_call())
    assert list(collector._pending) == [(1, first.serial)]

    second_bus = FakeBus()
    collector.note_disconnect(ConnectionError())
    collector.note_connection(second_bus)
    second = collector.submit(make_call())

    # The disconnected call dropped out of the pending table and the new call,
    # despite reusing the same serial, is keyed by the bumped generation.
    assert list(collector._pending) == [(2, second.serial)]
    assert first.serial == second.serial
    assert first.generation != second.generation
    assert first.outcome is CallOutcome.DISCONNECTED
