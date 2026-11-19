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


Reconnecting and State Reconciliation
-------------------------------------

A successful reconnect says nothing about which client-side state is still
valid.  ``mpd.recovery`` keeps these facts apart:

* the connection incarnation (see ``MPDClient.connection_epoch``)
* the service generation, supplied by the application or a supervisor
* the database identity (``db_update`` / ``songs``, and ``updating_db``)
* the queue identity (playlist version plus its service generation)

Directory entries are tracked separately as ``DirectoryIdentity`` objects
built from ``Last-Modified``, size or an application fingerprint.

Classify cached state against what the server reported::

    from mpd.recovery import (
        CachedState, CacheDisposition, ObservedState, StateDomain,
        classify_caches,
    )

    classification = classify_caches(cached, observed)
    classification.disposition(StateDomain.LIBRARY)   # REUSE
    classification.disposition(StateDomain.QUEUE)     # INVALID
    classification.disposition(StateDomain.DIRECTORY, "Music/Album")

Every domain receives its own verdict:

* ``REUSE``: the evidence confirms the cached material is unchanged.
* ``VERIFY``: evidence is missing or inconclusive; the item stays pending.
* ``INVALID``: the evidence proves the cached material cannot be used.

Missing evidence never silently becomes either ``REUSE`` or ``INVALID``.

``build_recovery_plan()`` selects an action per domain, and
``execute_recovery_plan()`` reports the state actually adopted::

    plan = build_recovery_plan(cached, observed, classification)
    result = execute_recovery_plan(plan, cached, observed, probe=fetch_actual)

    result.source_for(StateDomain.LIBRARY)   # StateSource.CACHE
    result.source_for(StateDomain.QUEUE)     # StateSource.SERVER
    result.source_for(StateDomain.DIRECTORY, "Music/Album")  # StateSource.NONE

A queue song reference is bound to its queue incarnation::

    from mpd.recovery import QueueState

    old = QueueState("boot-1", 12, 3).song(42)
    new = QueueState("boot-2", 12, 3).song(42)
    old.continues(new)        # False: songid 42 was reused by a new queue
    old.songid_reused_by(new) # True

When evidence conflicts for one credential, only that ``AuthScope`` is
closed.  Other scopes remain usable, and uncorroborated scopes are reported
as pending rather than being disconnected.

The asyncio client additionally binds every queued result to the connection
epoch that issued it.  A reply from an older connection raises
``StaleReplyError`` instead of being applied to the replacing connection.

Thread-Safety
-------------

Currently ``MPDClient`` is **NOT** thread-safe. As it use a socket internally,
only one thread can send or receive at the time.

But ``MPDClient`` can be easily extended to be thread-safe using `locks
<http://docs.python.org/library/threading.html#lock-objects>`__.  Take a look at
``examples/locking.py`` for further informations.


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
