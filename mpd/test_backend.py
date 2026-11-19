#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from mpd.backend import MusicDataBackend, collect_from_client


def song(
    file: str,
    *,
    id: int | str | None = None,
    pos: int | str | None = None,
    **tags: Any,
) -> dict[str, Any]:
    entry: dict[str, Any] = {"file": file, **tags}
    if id is not None:
        entry["id"] = str(id)
    if pos is not None:
        entry["pos"] = str(pos)
    return entry


class LibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = MusicDataBackend()

    def tearDown(self) -> None:
        self.backend.close()

    def test_upsert_keyed_by_uri(self) -> None:
        self.assertTrue(
            self.backend.upsert_track(
                song("a.flac", title="A", artist="X"), now=1
            )
        )
        self.assertFalse(
            self.backend.upsert_track(
                song("a.flac", title="A2", artist="Y"), now=2
            )
        )
        self.assertEqual(self.backend.library_uris(), ["a.flac"])
        track = self.backend.get_track("a.flac")
        assert track is not None
        self.assertEqual(track["title"], "A2")
        self.assertEqual(track["last_seen"], 2)
        self.assertEqual(track["tags"]["artist"], "Y")

    def test_sync_skips_non_file_entries_and_counts(self) -> None:
        inserted, updated = self.backend.sync_library(
            [
                {"directory": "foo"},
                {"playlist": "stored"},
                song("a.flac", title="A"),
                song("b.flac", title="B"),
            ],
            now=1,
        )
        self.assertEqual((inserted, updated), (2, 0))
        inserted, updated = self.backend.sync_library(
            [song("a.flac", title="A v2"), song("c.flac")], now=2
        )
        self.assertEqual((inserted, updated), (1, 1))
        self.assertEqual(
            self.backend.library_uris(), ["a.flac", "b.flac", "c.flac"]
        )

    def test_track_without_uri_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.backend.upsert_track({"title": "no uri"})

    def test_same_uri_in_queue_stays_single_track(self) -> None:
        self.backend.sync_library([song("a.flac")], now=1)
        gen = self.backend.rebuild_queue(
            [
                song("a.flac", id=1, pos=0),
                song("a.flac", id=2, pos=1),
            ],
            now=1,
        )
        self.assertEqual(self.backend.library_uris(), ["a.flac"])
        self.assertEqual(len(self.backend.get_queue(gen)), 2)
        self.assertEqual(self.backend.count_requests("a.flac"), 2)


class QueueRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = MusicDataBackend()

    def tearDown(self) -> None:
        self.backend.close()

    def test_repeated_poll_is_idempotent(self) -> None:
        gen1 = self.backend.refresh_queue(
            [song("a.flac", id=1, pos=0), song("b.flac", id=2, pos=1)],
            now=10,
        )
        gen2 = self.backend.refresh_queue(
            [song("a.flac", id=1, pos=0), song("b.flac", id=2, pos=1)],
            now=20,
        )
        self.assertEqual(gen1, gen2)
        self.assertEqual(len(self.backend.get_queue()), 2)
        self.assertEqual(self.backend.count_requests("a.flac"), 1)
        self.assertEqual(self.backend.count_requests("b.flac"), 1)

    def test_position_refresh_does_not_add_request(self) -> None:
        gen = self.backend.refresh_queue(
            [song("a.flac", id=1, pos=0), song("b.flac", id=2, pos=1)],
            now=10,
        )
        self.backend.refresh_queue(
            [song("b.flac", id=2, pos=0), song("a.flac", id=1, pos=1)],
            now=20,
        )
        queue = self.backend.get_queue(gen)
        self.assertEqual([entry["songid"] for entry in queue], [2, 1])
        self.assertEqual(self.backend.count_requests("a.flac"), 1)

    def test_left_entries_remain_in_history(self) -> None:
        gen = self.backend.refresh_queue(
            [
                song("a.flac", id=1, pos=0),
                song("b.flac", id=2, pos=1),
                song("c.flac", id=3, pos=2),
            ],
            now=10,
        )
        self.backend.refresh_queue([song("a.flac", id=1, pos=0)], now=20)
        queue = self.backend.get_queue(gen)
        self.assertEqual([entry["songid"] for entry in queue], [1, 2, 3])
        self.assertEqual(self.backend.count_requests("c.flac"), 1)

        self.backend.refresh_queue([], now=30)
        self.assertEqual(
            [entry["songid"] for entry in self.backend.get_queue(gen)],
            [1, 2, 3],
        )
        self.assertEqual(self.backend.latest_generation_id(), gen)

    def test_duplicate_songid_in_snapshot_stored_once(self) -> None:
        gen = self.backend.rebuild_queue(
            [song("a.flac", id=1, pos=0), song("a.flac", id=1, pos=5)],
            now=10,
        )
        entries = self.backend.get_queue(gen)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["songid"], 1)
        self.assertEqual(self.backend.count_requests("a.flac"), 1)

    def test_entry_without_songid_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.backend.refresh_queue([song("a.flac", pos=0)], now=1)

    def test_record_entry_refresh_returns_new_flag(self) -> None:
        gen = self.backend.start_generation(now=1)
        self.assertTrue(
            self.backend.record_queue_entry(gen, song("a.flac", id=7, pos=0))
        )
        self.assertFalse(
            self.backend.record_queue_entry(gen, song("a.flac", id=7, pos=3))
        )
        self.assertEqual(self.backend.count_requests("a.flac"), 1)


class QueueRebuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = MusicDataBackend()

    def tearDown(self) -> None:
        self.backend.close()

    def test_rebuild_opens_isolated_generation(self) -> None:
        first = self.backend.rebuild_queue(
            [song("a.flac", id=1, pos=0)], now=10
        )
        second = self.backend.rebuild_queue(
            [song("a.flac", id=1, pos=0), song("b.flac", id=2, pos=1)],
            now=20,
        )
        self.assertNotEqual(first, second)
        self.assertEqual(self.backend.latest_generation_id(), second)
        self.assertEqual(
            [entry["songid"] for entry in self.backend.get_queue(first)], [1]
        )
        self.assertEqual(
            [entry["songid"] for entry in self.backend.get_queue(second)],
            [1, 2],
        )

    def test_reused_songid_after_rebuild_is_new_occurrence(self) -> None:
        self.backend.rebuild_queue([song("a.flac", id=1, pos=0)], now=10)
        self.backend.rebuild_queue([song("a.flac", id=1, pos=0)], now=20)
        self.assertEqual(len(self.backend.get_queue(1)), 1)
        self.assertEqual(len(self.backend.get_queue(2)), 1)
        self.assertEqual(self.backend.count_requests("a.flac"), 2)

    def test_default_get_queue_uses_latest_generation(self) -> None:
        self.backend.rebuild_queue([song("old.flac", id=1)], now=1)
        self.backend.rebuild_queue([song("new.flac", id=9)], now=2)
        queue = self.backend.get_queue()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["songid"], 9)


class PersistenceAndClientTests(unittest.TestCase):
    def test_database_reopens_with_split_tables_intact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "music.db"
            backend = MusicDataBackend(path)
            backend.sync_library([song("a.flac", title="A")], now=1)
            backend.rebuild_queue([song("a.flac", id=1, pos=0)], now=1)
            backend.close()

            reopened = MusicDataBackend(path)
            try:
                self.assertEqual(reopened.library_uris(), ["a.flac"])
                self.assertEqual(len(reopened.get_queue()), 1)
                self.assertEqual(reopened.count_requests("a.flac"), 1)
            finally:
                reopened.close()

    def test_context_manager_closes_connection(self) -> None:
        with MusicDataBackend() as backend:
            backend.sync_library([song("a.flac")], now=1)
        with self.assertRaises(Exception):
            backend.library_uris()

    def test_collect_from_client_refresh_and_rebuild(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.library = [
                    {"directory": "dir"},
                    song("a.flac", title="A"),
                    song("b.flac", title="B"),
                ]
                self.queue = [song("a.flac", id=1, pos=0)]

            def listallinfo(self) -> list[dict[str, Any]]:
                return list(self.library)

            def playlistinfo(self) -> list[dict[str, Any]]:
                return list(self.queue)

        backend = MusicDataBackend()
        client = FakeClient()
        summary = collect_from_client(client, backend, now=1)
        self.assertEqual(summary["library_inserted"], 2)
        self.assertEqual(summary["library_updated"], 0)
        first_gen = summary["queue_generation"]

        client.queue = [
            song("a.flac", id=1, pos=0),
            song("b.flac", id=2, pos=1),
        ]
        summary = collect_from_client(client, backend, now=2)
        self.assertEqual(summary["queue_generation"], first_gen)
        self.assertEqual(summary["library_updated"], 2)
        self.assertEqual(len(backend.get_queue()), 2)

        summary = collect_from_client(client, backend, rebuild=True, now=3)
        self.assertNotEqual(summary["queue_generation"], first_gen)
        self.assertEqual(backend.count_requests("a.flac"), 2)
        backend.close()


if __name__ == "__main__":
    unittest.main()
