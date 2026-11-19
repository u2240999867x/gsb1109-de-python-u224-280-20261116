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


Scoped Tag Type Projections
---------------------------

When collecting the music library in batches that each need different tags,
``MPDClient.tagtypes_scope()`` temporarily switches the connection's tag
types and restores the previous setting on exit, even if the batch raises or
is aborted early::

    with client.tagtypes_scope(["Artist", "Date"]) as scope:
        for song in client.search("artist", "Someone"):
            states = scope.classify(song)
            # states["date"] is TagFieldState.PROVIDED or
            # TagFieldState.NOT_PROVIDED (requested, but the server did not
            # send it); fields outside the projection, such as "file",
            # report TagFieldState.NOT_REQUESTED.

Each scope carries its own projection, so two batches never share projection
state, and scopes cannot be re-entered.  Scopes may nest; they must be
closed innermost-first and restore their settings in the order in which the
projections actually took effect.

A restore is never sent while a result is still being iterated on the same
connection: ``close()`` raises ``IteratingError`` in that case and can be
retried once the pending result is fully consumed.

The saved settings are evidence of the connection they were taken from.  If
the client reconnects while a scope is active, the stale settings are not
applied to the new connection; call ``scope.revalidate()`` to query a fresh
baseline and re-apply the projection there.

