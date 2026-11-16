from dbus_next.message_bus import BaseMessageBus, _PendingCall
from dbus_next.aio.message_bus import MessageBus as AioMessageBus
from dbus_next import Message, MessageType

import asyncio
import pytest


class FakeBaseBus(BaseMessageBus):
    """A BaseMessageBus with no real socket so reply routing can be tested
    without a running DBus daemon."""

    def __init__(self):
        self.sent = []
        super().__init__(bus_address='unix:path=/tmp/fake')

    def _setup_socket(self):
        pass

    def send(self, msg):
        if not msg.serial:
            msg.serial = self.next_serial()
        self.sent.append(msg)


class FakeAioBus(AioMessageBus):
    """An asyncio MessageBus without the socket/auth/writer machinery."""

    def __init__(self, loop):
        BaseMessageBus.__init__(self, bus_address='unix:path=/tmp/fake')
        self._loop = loop
        self.sent = []

    def _setup_socket(self):
        pass

    def send(self, msg):
        if not msg.serial:
            msg.serial = self.next_serial()
        self.sent.append(msg)
        future = self._loop.create_future()
        future.set_result(None)
        return future


def make_call(bus, destination=':1.1', serial=None):
    return Message(destination=destination,
                   path='/org/test/path',
                   interface='org.test.iface',
                   member='Member',
                   serial=serial if serial is not None else bus.next_serial())


def make_return(call, body='ok'):
    return Message(message_type=MessageType.METHOD_RETURN,
                   reply_serial=call.serial,
                   signature='s',
                   body=[body])


def test_reply_is_delivered_exactly_once():
    bus = FakeBaseBus()
    notified = []
    call = make_call(bus)
    bus._call(call, lambda reply, err: notified.append((reply, err)))

    reply = make_return(call)
    bus._process_message(reply)

    assert len(notified) == 1
    assert notified[0] == (reply, None)
    assert call.serial not in bus._method_return_calls

    # a duplicated, late reply must not notify the consumer a second time
    bus._process_message(make_return(call))
    assert len(notified) == 1


def test_cancelled_subscription_keeps_call_but_swallows_late_reply():
    bus = FakeBaseBus()
    notified = []
    call = make_call(bus)
    bus._call(call, lambda reply, err: notified.append((reply, err)))
    serial = call.serial

    # the consumer stops waiting locally; the call fact stays pending
    bus._cancel_method_return(serial)
    pending = bus._method_return_calls[serial]
    assert isinstance(pending, _PendingCall)
    assert pending.cancelled
    assert pending.callback is None

    # the remote eventually answers: the original call completes once, but the
    # already-unsubscribed consumer is not notified
    bus._process_message(make_return(call))
    assert notified == []
    assert serial not in bus._method_return_calls

    # any further reply is now an unknown serial and stays harmless
    bus._process_message(make_return(call))
    assert notified == []


def test_reply_handled_by_user_handler_still_consumes_the_call():
    bus = FakeBaseBus()
    notified = []
    call = make_call(bus)
    bus._call(call, lambda reply, err: notified.append((reply, err)))

    def user_handler(msg):
        return True

    bus.add_message_handler(user_handler)
    bus._process_message(make_return(call))

    # the consumer is not notified, but the call is consumed exactly once so a
    # later duplicate cannot reach it
    assert notified == []
    assert call.serial not in bus._method_return_calls
    bus._process_message(make_return(call))
    assert notified == []


def test_cancelling_does_not_resend_the_call():
    bus = FakeBaseBus()
    call = make_call(bus)
    bus._call(call, lambda reply, err: None)

    assert len(bus.sent) == 1
    bus._cancel_method_return(call.serial)
    assert len(bus.sent) == 1


def test_stale_generation_reply_is_not_matched():
    bus = FakeBaseBus()
    notified = []
    call = make_call(bus)
    bus._call(call, lambda reply, err: notified.append((reply, err)))

    record = bus._method_return_calls[call.serial]
    assert record.generation == bus._connection_generation

    # the connection is replaced; the surviving record is now stale
    bus._connection_generation += 1
    bus._process_message(make_return(call))

    assert notified == []
    # the stale record is left untouched rather than matched against a reply
    assert call.serial in bus._method_return_calls


def test_finalize_replaces_generation_clears_calls_and_errors_consumers():
    bus = FakeBaseBus()
    notified = []
    cancelled_call = make_call(bus)
    bus._call(cancelled_call, lambda reply, err: notified.append((reply, err)))
    bus._cancel_method_return(cancelled_call.serial)

    call = make_call(bus)
    bus._call(call, lambda reply, err: notified.append((reply, err)))

    error = EOFError('connection lost')
    bus._finalize(error)

    assert bus._connection_generation == 1
    assert bus._method_return_calls == {}
    # only the still-subscribed consumer gets a terminal error, and just once;
    # the already-cancelled consumer is not notified
    assert len(notified) == 1
    assert notified[0][0] is None
    assert isinstance(notified[0][1], EOFError)

    # a reply that arrives on the dead connection has nothing to match
    bus._process_message(make_return(call))
    assert len(notified) == 1


def test_new_connection_starts_with_a_fresh_call_table():
    bus = FakeBaseBus()
    old_call = make_call(bus)
    bus._call(old_call, lambda reply, err: None)
    assert old_call.serial in bus._method_return_calls

    # the connection dies: its pending table is retired wholesale and the
    # generation moves on, so nothing from the old connection can be matched
    bus._finalize(EOFError('connection lost'))
    assert bus._method_return_calls == {}
    assert bus._connection_generation == 1

    # the replacement connection tracks its own calls independently
    new_call = make_call(bus)
    bus._call(new_call, lambda reply, err: None)
    record = bus._method_return_calls[new_call.serial]
    assert record.generation == 1

    # a reply for the retired connection cannot reach the new consumer
    bus._process_message(make_return(old_call))
    assert new_call.serial in bus._method_return_calls


@pytest.mark.asyncio
async def test_aio_call_reply_resolves_consumer():
    loop = asyncio.get_running_loop()
    bus = FakeAioBus(loop)
    call = make_call(bus)

    task = asyncio.create_task(bus.call(call))
    await asyncio.sleep(0)
    assert call.serial in bus._method_return_calls

    bus._process_message(make_return(call))
    reply = await task

    assert reply.message_type == MessageType.METHOD_RETURN
    assert reply.body == ['ok']
    assert call.serial not in bus._method_return_calls


@pytest.mark.asyncio
async def test_aio_call_cancelled_then_late_reply_does_not_notify():
    loop = asyncio.get_running_loop()
    bus = FakeAioBus(loop)
    call = make_call(bus)
    serial = call.serial

    task = asyncio.create_task(bus.call(call))
    await asyncio.sleep(0)
    assert serial in bus._method_return_calls

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # cancelling the local wait detaches the consumer but keeps the call
    pending = bus._method_return_calls[serial]
    assert pending.cancelled
    # the method call was sent exactly once and is never resent
    assert len(bus.sent) == 1

    # the late reply finishes the original call without resurrecting the
    # cancelled consumer
    bus._process_message(make_return(call, body='late'))
    assert serial not in bus._method_return_calls

    bus._process_message(make_return(call, body='late again'))
    assert task.cancelled()
