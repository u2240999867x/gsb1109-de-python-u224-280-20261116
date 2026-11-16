The Message Bus
===============

.. toctree::
   :maxdepth: 2

   base-message-bus.rst
   aio-message-bus.rst
   glib-message-bus.rst

The message bus manages a connection to the DBus daemon. It's capable of sending and receiving messages and wiring up the classes of the high level interfaces.

There are currently two implementations of the message bus depending on what main loop implementation you want to use. Use :class:`aio.MessageBus <dbus_next.aio.MessageBus>` if you are using an asyncio main loop. Use :class:`glib.MessageBus <dbus_next.glib.MessageBus>` if you are using a GLib main loop.

For standalone applications, the asyncio message bus is preferable because it has a nice async/await api in place of the callback/synchronous interface of the GLib message bus. If your application is using other libraries that use the GLib main loop, such as a GTK application, the GLib implementation will be needed. However neither library is a requirement.

For more information on how to use the message bus, see the documentation for the specific interfaces you plan to use.

Shared match rules
------------------

Multiple consumers on one connection can subscribe to the same match rule.
Rules are compared semantically, not as raw strings, so differences in key
order, quoting, escaping, a trailing ``path_namespace`` slash, or how an
object path or argument namespace is spelled do not create a second
registration.  The daemon is given the rule once while at least one consumer
wants it, and each consumer still receives every signal matched by its own
rule.

Use :func:`MessageBus.subscribe() <dbus_next.message_bus.BaseMessageBus.subscribe>`
to get a cancellable subscription handle, or the lower level
:func:`MessageBus.add_match_rule()
<dbus_next.message_bus.BaseMessageBus.add_match_rule>` and
:func:`MessageBus.remove_match_rule()
<dbus_next.message_bus.BaseMessageBus.remove_match_rule>` pair.

.. code-block:: python3

    def handler(msg):
        print(f'a signal from {msg.sender}: {msg.member}')

    subscription = bus.subscribe("type='signal',path_namespace='/com/example'",
                                 handler)

    # ...

    subscription.cancel()

AddMatch and RemoveMatch confirmations are asynchronous and may fail or
interleave.  A consumer withdrawal never sends RemoveMatch before a pending
AddMatch has been confirmed, and a new consumer arriving while a removal is
pending keeps using the old registration until it is safe to register again.
Removing one consumer never affects the others.

:func:`MessageBus.get_match_rules()
<dbus_next.message_bus.BaseMessageBus.get_match_rules>` reports the actual
daemon registrations and the status of every consumer.  When a connection is
lost, subscriptions are kept: after reconnecting, registration is re-verified
against the new connection and confirmations belonging to the old connection
are discarded.

To inspect or construct a rule in code, use
:class:`MatchRule <dbus_next.MatchRule>`; it parses strings with the same
semantics as the bus and compares rules by meaning.
