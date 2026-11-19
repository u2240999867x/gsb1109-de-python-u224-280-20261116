Data backend: library and play queue
=====================================

``mpd.backend`` is an optional SQLite backend that collects the MPD music
library and the play queue into separate tables. It is only built on the
Python standard library (``sqlite3``).

Storage model
-------------

The two data sources are intentionally not merged:

* ``tracks`` is keyed by the track URI, i.e. the MPD ``file`` field. A track
  is a single row however often it is played or queued.
* ``queue_generations`` groups one logical queue lifetime, while
  ``queue_entries`` holds its occurrences keyed by
  ``(generation_id, songid)``. The queue position ``pos`` is stored but is
  only the current ordering; it is never part of the entry identity.

A queue rebuild opens a new generation. Song IDs reused by MPD after a rebuild
therefore never attach to old history. Re-polling the same live queue stays in
the current generation and is idempotent: collecting the same occurrence again
only refreshes its position and does not add another request row.

Usage
-----

::

    from mpd import MPDClient
    from mpd.backend import MusicDataBackend, collect_from_client

    client = MPDClient()
    client.connect("localhost", 6600)

    backend = MusicDataBackend("music-data.db")
    # periodic poll: library upsert by URI + idempotent queue refresh
    collect_from_client(client, backend)

    # after the queue was rebuilt (e.g. MPD clear/load):
    collect_from_client(client, backend, rebuild=True)

The same primitives are available directly:

* ``MusicDataBackend.sync_library`` upserts tracks by URI and returns the
  ``(inserted, updated)`` counts.
* ``MusicDataBackend.refresh_queue`` polls the queue into the current
  generation, creating it on first use.
* ``MusicDataBackend.rebuild_queue`` starts a fresh generation for a rebuilt
  queue.
* ``MusicDataBackend.count_requests(uri)`` counts queue occurrences of a
  track across all generations, which doubles as the accumulated request
  count without double counting polls.
