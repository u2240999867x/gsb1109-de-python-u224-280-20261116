# -*- coding: utf-8 -*-
"""SQLite backend collecting the MPD music library and play queue separately.

The music library and the play queue are two different domains and are stored
in separate tables:

* ``tracks`` is keyed by the track URI (the MPD ``file`` field). A track is a
  single row no matter how often it shows up in the queue.
* ``queue_generations`` / ``queue_entries`` model the queue. Every queue
  collection starts a new generation and entries are keyed by the MPD
  ``songid`` *within that generation*. The queue position (``pos``) is only a
  ordering hint, never part of the identity.

Rebuilding the queue therefore never joins old history: a ``songid`` reused by
MPD after a rebuild lands in a new generation as a new row (use
:meth:`MusicDataBackend.rebuild_queue`). Re-polling the same live queue uses
:meth:`MusicDataBackend.refresh_queue` and stays idempotent within the current
generation, so polling the same occurrence does not create additional request
counts; only its position is refreshed.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Protocol, Tuple

__all__ = ["MusicDataBackend", "collect_from_client"]


class _MPDClientLike(Protocol):
    def listallinfo(self, *args: Any) -> list[dict[str, Any]]: ...

    def playlistinfo(self, *args: Any) -> list[dict[str, Any]]: ...


class MusicDataBackend:
    """Persist MPD library tracks and play queue occurrences in SQLite."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def __enter__(self) -> "MusicDataBackend":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def _create_schema(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tracks (
                    uri       TEXT PRIMARY KEY,
                    title     TEXT,
                    artist    TEXT,
                    album     TEXT,
                    duration  REAL,
                    last_seen INTEGER NOT NULL,
                    raw_tags  TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS queue_generations (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    collected_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS queue_entries (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    generation_id INTEGER NOT NULL
                                  REFERENCES queue_generations(id),
                    songid        INTEGER NOT NULL,
                    pos           INTEGER,
                    uri           TEXT,
                    collected_at  INTEGER NOT NULL,
                    UNIQUE (generation_id, songid)
                );

                CREATE INDEX IF NOT EXISTS idx_queue_entries_generation
                    ON queue_entries (generation_id);
                CREATE INDEX IF NOT EXISTS idx_queue_entries_uri
                    ON queue_entries (uri);
                """
            )

    @staticmethod
    def _now(now: Optional[int]) -> int:
        return int(time.time()) if now is None else now

    @staticmethod
    def _optional_int(song: Mapping[str, Any], key: str) -> Optional[int]:
        value = song.get(key)
        if value is None:
            return None
        return int(value)

    # -- music library -----------------------------------------------------

    @staticmethod
    def _track_tags(song: Mapping[str, Any]) -> dict[str, Any]:
        tags = dict(song)
        tags.pop("file", None)
        return tags

    def _upsert_track(
        self, uri: str, song: Mapping[str, Any], timestamp: int
    ) -> bool:
        """Insert or update a track. Returns ``True`` for a new row."""
        exists = self._conn.execute(
            "SELECT 1 FROM tracks WHERE uri = ?", (uri,)
        ).fetchone()
        duration: Optional[float]
        raw_duration = song.get("duration") or song.get("time")
        try:
            duration = None if raw_duration is None else float(raw_duration)
        except (TypeError, ValueError):
            duration = None
        self._conn.execute(
            """
            INSERT INTO tracks (uri, title, artist, album, duration,
                                last_seen, raw_tags)
            VALUES (:uri, :title, :artist, :album, :duration,
                    :last_seen, :raw_tags)
            ON CONFLICT (uri) DO UPDATE SET
                title     = excluded.title,
                artist    = excluded.artist,
                album     = excluded.album,
                duration  = excluded.duration,
                last_seen = excluded.last_seen,
                raw_tags  = excluded.raw_tags
            """,
            {
                "uri": uri,
                "title": song.get("title"),
                "artist": song.get("artist"),
                "album": song.get("album"),
                "duration": duration,
                "last_seen": timestamp,
                "raw_tags": json.dumps(self._track_tags(song), ensure_ascii=False),
            },
        )
        return exists is None

    def upsert_track(
        self, song: Mapping[str, Any], *, now: Optional[int] = None
    ) -> bool:
        """Store one library track, keyed by its ``file`` URI.

        Returns ``True`` when the track was newly inserted, ``False`` when an
        existing row was refreshed.
        """
        uri = song.get("file")
        if not uri:
            raise ValueError("library track requires a 'file' URI")
        timestamp = self._now(now)
        with self._conn:
            return self._upsert_track(uri, song, timestamp)

    def sync_library(
        self,
        songs: Iterable[Mapping[str, Any]],
        *,
        now: Optional[int] = None,
    ) -> Tuple[int, int]:
        """Upsert a batch of library tracks.

        Entries without a ``file`` field (MPD directories or stored playlists
        returned by ``listallinfo``) are skipped. Returns the
        ``(inserted, updated)`` counts.
        """
        timestamp = self._now(now)
        inserted = 0
        updated = 0
        with self._conn:
            for song in songs:
                uri = song.get("file")
                if not uri:
                    continue
                if self._upsert_track(uri, song, timestamp):
                    inserted += 1
                else:
                    updated += 1
        return inserted, updated

    def get_track(self, uri: str) -> Optional[dict[str, Any]]:
        row = self._conn.execute(
            "SELECT * FROM tracks WHERE uri = ?", (uri,)
        ).fetchone()
        if row is None:
            return None
        track = dict(row)
        track["tags"] = json.loads(track.pop("raw_tags"))
        return track

    def library_uris(self) -> list[str]:
        return [
            row[0]
            for row in self._conn.execute(
                "SELECT uri FROM tracks ORDER BY uri"
            ).fetchall()
        ]

    # -- play queue --------------------------------------------------------

    @staticmethod
    def _require_songid(song: Mapping[str, Any]) -> int:
        if "id" not in song or song["id"] is None:
            raise ValueError("queue entry requires an 'id' (MPD songid)")
        return int(song["id"])

    def _start_generation(self, timestamp: int) -> int:
        cursor = self._conn.execute(
            "INSERT INTO queue_generations (collected_at) VALUES (?)",
            (timestamp,),
        )
        return int(cursor.lastrowid)

    def start_generation(self, *, now: Optional[int] = None) -> int:
        """Open a new queue generation and return its id.

        Call this when the queue has been rebuilt (e.g. after an MPD
        ``clear``): subsequent entries never attach to the old generation,
        even when MPD reuses the same songid numbers.
        """
        timestamp = self._now(now)
        with self._conn:
            return self._start_generation(timestamp)

    def _insert_entry(
        self,
        generation_id: int,
        song: Mapping[str, Any],
        timestamp: int,
    ) -> bool:
        """Insert one queue entry. Idempotent per (generation, songid)."""
        songid = self._require_songid(song)
        cursor = self._conn.execute(
            """
            INSERT OR IGNORE INTO queue_entries
                (generation_id, songid, pos, uri, collected_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                generation_id,
                songid,
                self._optional_int(song, "pos"),
                song.get("file"),
                timestamp,
            ),
        )
        return cursor.rowcount == 1

    def _upsert_entry(
        self,
        generation_id: int,
        song: Mapping[str, Any],
        timestamp: int,
    ) -> bool:
        """Insert an entry or refresh its position in an open generation.

        Position is only the current ordering, so re-collecting the same
        occurrence updates ``pos`` without inserting another request row.
        Returns ``True`` only when a new occurrence was stored.
        """
        songid = self._require_songid(song)
        pos = self._optional_int(song, "pos")
        uri = song.get("file")
        existed = (
            self._conn.execute(
                "SELECT 1 FROM queue_entries "
                "WHERE generation_id = ? AND songid = ?",
                (generation_id, songid),
            ).fetchone()
            is not None
        )
        self._conn.execute(
            """
            INSERT INTO queue_entries
                (generation_id, songid, pos, uri, collected_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (generation_id, songid) DO UPDATE SET
                pos = excluded.pos,
                uri = excluded.uri
            """,
            (generation_id, songid, pos, uri, timestamp),
        )
        return not existed

    def record_queue_entry(
        self,
        generation_id: int,
        song: Mapping[str, Any],
        *,
        now: Optional[int] = None,
    ) -> bool:
        """Record one entry inside an existing queue generation.

        Re-recording the same ``songid`` only refreshes its position and does
        not add another request row. Returns ``True`` for a new occurrence.
        """
        timestamp = self._now(now)
        with self._conn:
            return self._upsert_entry(generation_id, song, timestamp)

    def refresh_queue(
        self,
        songs: Iterable[Mapping[str, Any]],
        *,
        now: Optional[int] = None,
    ) -> int:
        """Poll the queue into the current generation, creating it if needed.

        Repeated polls of the same live queue stay in one generation: entries
        already seen are not counted again, while changed positions are
        refreshed. Entries that have left the queue stay recorded in history
        instead of being deleted; call :meth:`rebuild_queue` when the queue is
        rebuilt and a clean generation is required.
        """
        timestamp = self._now(now)
        generation_id = self.latest_generation_id()
        with self._conn:
            if generation_id is None:
                generation_id = self._start_generation(timestamp)
            for song in songs:
                self._upsert_entry(generation_id, song, timestamp)
        return generation_id

    def rebuild_queue(
        self,
        songs: Iterable[Mapping[str, Any]],
        *,
        now: Optional[int] = None,
    ) -> int:
        """Store a rebuilt queue snapshot as a brand-new generation.

        Use this after the queue has been rebuilt. The new generation never
        joins old history, so a reused songid becomes a fresh occurrence and
        can be counted as a new request again.
        """
        timestamp = self._now(now)
        with self._conn:
            generation_id = self._start_generation(timestamp)
            for song in songs:
                self._insert_entry(generation_id, song, timestamp)
        return generation_id

    def latest_generation_id(self) -> Optional[int]:
        row = self._conn.execute(
            "SELECT MAX(id) FROM queue_generations"
        ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def get_queue(
        self, generation_id: Optional[int] = None
    ) -> list[dict[str, Any]]:
        """Return entries of one generation, newest first by ``pos``.

        Defaults to the latest generation; returns an empty list when no
        queue has ever been collected.
        """
        if generation_id is None:
            generation_id = self.latest_generation_id()
            if generation_id is None:
                return []
        rows = self._conn.execute(
            """
            SELECT id, generation_id, songid, pos, uri, collected_at
            FROM queue_entries
            WHERE generation_id = ?
            ORDER BY pos IS NULL, pos, songid
            """,
            (generation_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def count_requests(self, uri: str) -> int:
        """Count queue occurrences of a URI across all generations."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM queue_entries WHERE uri = ?", (uri,)
        ).fetchone()
        return int(row[0])


def collect_from_client(
    client: _MPDClientLike,
    backend: MusicDataBackend,
    *,
    rebuild: bool = False,
    now: Optional[int] = None,
) -> dict[str, int]:
    """Collect library and queue from a connected ``MPDClient`` in one pass.

    The library snapshot (``listallinfo``) upserts tracks by URI while the
    queue snapshot (``playlistinfo``) is either polled into the current
    generation (``rebuild=False``, idempotent repeats) or stored as a new
    generation after a queue rebuild (``rebuild=True``). Returns a summary
    with the library insert/update counts and the affected generation id.
    """
    inserted, updated = backend.sync_library(client.listallinfo(), now=now)
    queue = list(client.playlistinfo())
    if rebuild:
        generation_id = backend.rebuild_queue(queue, now=now)
    else:
        generation_id = backend.refresh_queue(queue, now=now)
    return {
        "library_inserted": inserted,
        "library_updated": updated,
        "queue_generation": generation_id,
    }
