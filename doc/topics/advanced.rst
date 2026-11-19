Future Compatible
-----------------

New commands or special handling of commands can be easily implemented.  Use
``add_command()`` or ``remove_command()`` to modify the commands of the
*MPDClient* class and all its instances.::

    def fetch_cover(client):
        """"Take a MPDClient instance as its arguments and return mimetype and image"""
        # this command may come in the future.
        pass

    client.add_command("get_cover", fetch_cover)
    # you can then use:
    client.get_cover()

    # remove the command, because it doesn't exist already.
    client.remove_command("get_cover")


Thread-Safety
-------------

Currently ``MPDClient`` is **NOT** thread-safe. As it use a socket internally,
only one thread can send or receive at the time.

But ``MPDClient`` can be easily extended to be thread-safe using `locks
<http://docs.python.org/library/threading.html#lock-objects>`__.  Take a look at
``examples/locking.py`` for further informations.


Generation-aware background monitoring
--------------------------------------

The asyncio client already keeps ``idle``/``noidle`` and command responses
correctly framed, but it does not remember *which* refresh observed *which*
notification.  :class:`mpd.monitor.IdleMonitor` adds that layer on top of an
asyncio client.

Every ``idle`` notification opens one numbered invalidation *generation*.
When a refresh runs, the set of generations it is allowed to close is
snapshotted at the moment the command is sent; notifications arriving while
the response is being read open newer generations that the in-flight refresh
must not touch.  A refresh therefore only closes the generations it actually
covers, and a disconnect turns the still-open generations into an explicit,
auditable *gap*.::

    import asyncio
    import mpd.asyncio
    from mpd.monitor import IdleMonitor

    async def main():
        client = mpd.asyncio.MPDClient()
        await client.connect("localhost", 6600)

        monitor = IdleMonitor(client, default_command="status")
        await monitor.start()

        def on_change(events):
            for event in events:
                # event.served_by identifies the fetch that covered it
                print(event.seq, event.subsystems, event.generation)

        monitor.subscribe(on_change)

        # Coalesced background refreshes happen automatically; an explicit
        # refresh is always served exactly once.
        status = await monitor.refresh()

        # Every notification is attributable to a fetch, a gap, or pending.
        for event in monitor.events():
            print(event.seq, monitor.coverage(event))

        await monitor.aclose()
        client.disconnect()

    asyncio.run(main())

Key guarantees:

* refreshes move through auditable ``PENDING``/``RUNNING``/``SETTLED``
  (and ``FAILED``/``GAPPED``) phases with a fixed coverage snapshot,
* the coalescing deadline is computed once and never extended, so a steady
  stream of notifications cannot starve an explicit :meth:`refresh`,
* disconnects record a :class:`~mpd.monitor.GapRecord` instead of silently
  losing generations, and the first refresh after :meth:`reconnect` closes it,
* removing one subscription never discards changes other subscriptions need,
* :meth:`IdleMonitor.replay` returns the ordered trail of notifications,
  fetches and gaps for verification.


Unicode Handling
----------------

To quote the `mpd protocol documentation
<https://www.musicpd.org/doc/protocol/request_syntax.html>`_:

> All data between the client and the server is encoded in UTF-8.

With Python 3:
~~~~~~~~~~~~~~

In Python 3, Unicode string is the default string type. So just pass these
strings as arguments for MPD commands and *python-mpd2* will also return such
Unicode string.
