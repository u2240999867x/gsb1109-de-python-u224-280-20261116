#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import annotations

import itertools
import os
import tempfile
import unittest
from unittest import mock

import mpd
from mpd.archive import (
    DIRECTORY_SIDE,
    EVIDENCE_RESOLVED,
    EVIDENCE_UNRESOLVED,
    MPDArchiveFetcher,
    QueueArchive,
    UnresolvedEvidenceError,
)

SHARED_URI = "music/dup.flac"
OLD_ALBUM = "Album-OLD"
NEW_ALBUM = "Album-NEW"
STABLE_ALBUM = "Stable Album"


def queued_song(songid: int, uri: str, position: int, album: str) -> dict:
    return {
        "file": uri,
        "id": str(songid),
        "pos": str(position),
        "album": album,
        "duration": "100",
    }


def directory_song(uri: str, album: str) -> dict:
    return {"file": uri, "album": album, "last_modified": "2026-11-16T00:00:00Z"}


def initial_queue() -> list:
    # 1-based positions 2 and 7 carry the same URI with two distinct songids.
    songs = []
    for position in range(9):
        songid = 10 + position
        if position in (1, 6):
            uri = SHARED_URI
            album = OLD_ALBUM
        else:
            uri = "music/song-%d.ogg" % position
            album = STABLE_ALBUM
        songs.append(queued_song(songid, uri, position, album))
    return songs


def directory_v1() -> list:
    items = [{"directory": "music", "last_modified": "2026-11-16T00:00:00Z"}]
    for song in initial_queue():
        items.append(directory_song(song["file"], song["album"]))
    return items


def directory_v2() -> list:
    # A full rescan: only the shared URI's album tag changed.
    items = []
    seen = set()
    for song in initial_queue():
        if song["file"] in seen:
            continue
        seen.add(song["file"])
        album = NEW_ALBUM if song["file"] == SHARED_URI else song["album"]
        items.append(directory_song(song["file"], album))
    return items


def surviving_queue() -> list:
    # The last two entries (songids 17 and 18) were deleted; nothing else
    # moved, so plchanges since the old playlist version reports no changes.
    return [dict(song) for song in initial_queue()[:7]]


class FakeMPDClient:
    """Stand-in exposing only the read entries used by the fetcher."""

    def __init__(self, responses: dict) -> None:
        self.calls = []
        self._responses = responses

    def status(self):
        self.calls.append("status")
        return dict(self._responses["status"])

    def playlistinfo(self):
        self.calls.append("playlistinfo")
        return [dict(song) for song in self._responses["playlistinfo"]]

    def plchanges(self, version):
        self.calls.append(("plchanges", version))
        return [dict(song) for song in self._responses["plchanges"]]

    def listallinfo(self):
        self.calls.append("listallinfo")
        return [dict(item) for item in self._responses["listallinfo"]]


class TestMPDArchiveFetcher(unittest.TestCase):
    def test_uses_existing_queue_and_directory_entries(self) -> None:
        client = FakeMPDClient(
            {
                "status": {"playlist": "2"},
                "plchanges": [],
                "playlistinfo": surviving_queue(),
                "listallinfo": directory_v2(),
            }
        )
        fetcher = MPDArchiveFetcher(client)

        queue_version, songs = fetcher.queue_full()
        self.assertEqual(2, queue_version)
        self.assertEqual(surviving_queue(), songs)

        delta_version, changed, current = fetcher.queue_delta(1)
        self.assertEqual(2, delta_version)
        self.assertEqual([], changed)
        self.assertEqual(surviving_queue(), current)
        self.assertEqual(("plchanges", 1), client.calls[3])

        self.assertEqual(directory_v2(), fetcher.directory_full())
        self.assertEqual(
            [
                "status",
                "playlistinfo",
                "status",
                ("plchanges", 1),
                "playlistinfo",
                "listallinfo",
            ],
            client.calls,
        )

    def test_protocol_level_full_rescan_semantics(self) -> None:
        # Drive the real python-mpd2 client (including its protocol parser)
        # through the fetcher so the archived material keeps the exact
        # semantics of a full re-collection.
        socket_patch = mock.patch("mpd.base.socket")
        socket_mock = socket_patch.start()
        try:
            socket_mock.getaddrinfo.return_value = [range(5)]
            socket_mock.socket.side_effect = (
                lambda *a, **kw: mock.MagicMock(name="socket.socket")
            )
            client = mpd.MPDClient()
            client.connect("example.com", 10000)
            client._sock.reset_mock()

            lines = [
                "playlist: 1\n",
                "OK\n",
                "file: music/song-0.ogg\n",
                "Id: 10\n",
                "Pos: 0\n",
                "album: Stable Album\n",
                "file: %s\n" % SHARED_URI,
                "Id: 11\n",
                "Pos: 1\n",
                "album: Album-OLD\n",
                "OK\n",
                "directory: music\n",
                "file: %s\n" % SHARED_URI,
                "album: Album-OLD\n",
                "OK\n",
            ]
            client._rbfile.readline.side_effect = (
                line.encode("utf-8")
                for line in itertools.chain(lines, itertools.repeat(""))
            )

            fetcher = MPDArchiveFetcher(client)
            queue_version, queue_songs = fetcher.queue_full()
            directory_items = fetcher.directory_full()

            self.assertEqual(1, queue_version)
            self.assertEqual(2, len(queue_songs))
            self.assertEqual("11", queue_songs[1]["id"])
            self.assertEqual(OLD_ALBUM, queue_songs[1]["album"])
            directory_files = [
                item for item in directory_items if "file" in item
            ]
            self.assertEqual(OLD_ALBUM, directory_files[0]["album"])
        finally:
            socket_patch.stop()


class TestQueueArchiveScenario(unittest.TestCase):
    def setUp(self) -> None:
        self.archive = QueueArchive()
        self.records = self.archive.collect_full(
            queue_version=1,
            queue_songs=initial_queue(),
            directory_items=directory_v1(),
        )

    # ------------------------------------------------------------------
    # checkpoint / full collection
    # ------------------------------------------------------------------

    def test_full_collection_checkpoints_queue_and_directory(self) -> None:
        self.assertEqual(9, len(self.records))
        self.assertEqual(9, len(self.archive))
        self.assertEqual(
            {"queue_version": 1, "directory_version": 1, "batch_seq": 1},
            self.archive.checkpoint,
        )
        self.assertEqual(1, self.archive.directory_version_count())
        for record in self.records:
            self.assertEqual(EVIDENCE_RESOLVED, record["evidence"])
            self.assertEqual([], record["missing_evidence"])

    def test_same_uri_two_songids_keep_separate_identities(self) -> None:
        occurrences = self.archive.occurrences(SHARED_URI)
        self.assertEqual(2, len(occurrences))
        self.assertEqual({"11", "16"}, {row["songid"] for row in occurrences})
        self.assertEqual({1, 6}, {row["position"] for row in occurrences})
        for row in occurrences:
            self.assertEqual(OLD_ALBUM, row["album"])
            self.assertEqual(1, row["directory_version"])

    # ------------------------------------------------------------------
    # incremental collection with out-of-order directory evidence
    # ------------------------------------------------------------------

    def _collect_incremental(self):
        return self.archive.collect_incremental(
            queue_version=2,
            changed_songs=[],
            current_songs=surviving_queue(),
        )

    def test_incremental_marks_missing_directory_evidence(self) -> None:
        records = self._collect_incremental()
        self.assertEqual(7, len(records))
        self.assertEqual(
            {"queue_version": 2, "directory_version": 1, "batch_seq": 2},
            self.archive.checkpoint,
        )
        for record in records:
            self.assertEqual(EVIDENCE_UNRESOLVED, record["evidence"])
            self.assertEqual([DIRECTORY_SIDE], record["missing_evidence"])
            self.assertIsNone(record["album"])
            self.assertIsNone(record["directory_version"])

    def test_only_actually_deleted_entries_disappear(self) -> None:
        self._collect_incremental()
        deletions = self.archive.deletions()
        self.assertEqual(
            [("17", 7), ("18", 8)],
            [(row["songid"], row["last_position"]) for row in deletions],
        )
        latest = self.archive.latest_queue()
        self.assertEqual(7, len(latest))
        self.assertEqual(
            ["10", "11", "12", "13", "14", "15", "16"],
            [row["songid"] for row in latest],
        )
        # Both surviving same-URI entries keep their own songid identity.
        shared_rows = [row for row in latest if row["uri"] == SHARED_URI]
        self.assertEqual(["11", "16"], [row["songid"] for row in shared_rows])

    def test_unordered_confirmation_marks_changed_tag_records(self) -> None:
        batch_two = self._collect_incremental()
        revised = self.archive.confirm_directory_update(
            batch=2, directory_items=directory_v2(), ordered=False
        )

        # Only the five URIs whose tag did not change can be resolved.
        self.assertEqual(5, len(revised))
        revised_ids = {row["songid"] for row in revised}
        self.assertEqual({"10", "12", "13", "14", "15"}, revised_ids)
        for record in revised:
            self.assertEqual(EVIDENCE_RESOLVED, record["evidence"])
            self.assertEqual(1, record["directory_version"])
            self.assertEqual(STABLE_ALBUM, record["album"])

        unresolved = self.archive.unresolved_records(batch=2)
        self.assertEqual(2, len(unresolved))
        self.assertEqual(
            {"11", "16"}, {row["songid"] for row in unresolved}
        )
        for record in unresolved:
            self.assertEqual([DIRECTORY_SIDE], record["missing_evidence"])
            self.assertEqual([2], record["missing_evidence_versions"])
            self.assertEqual(2, record["candidate_directory_version"])
            self.assertIsNone(record["album"])

        # A second directory version now exists, but it is not yet an ordered
        # checkpoint, so the newest label must not leak into historical rows.
        self.assertEqual(2, self.archive.directory_version_count())
        self.assertEqual(NEW_ALBUM, self.archive.latest_directory_album(SHARED_URI))
        for record in batch_two:
            if record["songid"] in ("11", "16"):
                with self.assertRaises(UnresolvedEvidenceError):
                    self.archive.album_at(2, record["songid"])

    def test_archived_queue_keeps_original_album_label(self) -> None:
        self._collect_incremental()
        self.archive.confirm_directory_update(
            batch=2, directory_items=directory_v2(), ordered=False
        )
        archived = self.archive.queue_at(1)
        shared = [row for row in archived if row["uri"] == SHARED_URI]
        self.assertEqual(2, len(shared))
        for record in shared:
            self.assertEqual(OLD_ALBUM, self.archive.album_at(1, record["songid"]))
            self.assertEqual(1, record["directory_version"])

    def test_ordered_confirmation_revises_only_affected_records(self) -> None:
        self._collect_incremental()
        self.archive.confirm_directory_update(
            batch=2, directory_items=directory_v2(), ordered=False
        )
        ordered_revised = self.archive.confirm_directory_update(
            batch=2, directory_items=directory_v2(), ordered=True
        )

        self.assertEqual(2, len(ordered_revised))
        self.assertEqual(
            {"11", "16"}, {row["songid"] for row in ordered_revised}
        )
        for record in ordered_revised:
            self.assertEqual(EVIDENCE_RESOLVED, record["evidence"])
            self.assertEqual(2, record["directory_version"])
            self.assertEqual(NEW_ALBUM, record["album"])
            self.assertEqual([], record["missing_evidence"])

        # Stable records were not touched a second time.
        revisions = self.archive.revisions()
        self.assertEqual(7, len(revisions))
        reasons = sorted(row["reason"] for row in revisions)
        self.assertEqual(["ordered"] * 2 + ["unchanged"] * 5, reasons)
        for songid in ("11", "16"):
            self.assertEqual(NEW_ALBUM, self.archive.album_at(2, songid))

        # Re-confirming once everything is resolved replays the same
        # revision without adding entries to the log.
        again = self.archive.confirm_directory_update(
            batch=2, directory_items=directory_v2(), ordered=True
        )
        self.assertEqual(
            ["11", "16"], [row["songid"] for row in again]
        )
        self.assertEqual(7, len(self.archive.revisions()))
        self.assertEqual(
            2, len(self.archive._batch(2)["directory_confirmations"])
        )

        # The ordered snapshot becomes the directory checkpoint.
        self.assertEqual(2, self.archive.checkpoint["directory_version"])

    def test_replay_same_material_creates_no_new_records(self) -> None:
        self._collect_incremental()
        self.archive.confirm_directory_update(
            batch=2, directory_items=directory_v2(), ordered=False
        )
        self.archive.confirm_directory_update(
            batch=2, directory_items=directory_v2(), ordered=True
        )

        entries_before = len(self.archive)
        batches_before = len(self.archive.queue_at(1)) + len(
            self.archive.queue_at(2)
        )
        self.assertEqual(0, self.archive.replay_stored())
        self.assertEqual(0, self.archive.replay_stored())
        self.assertEqual(entries_before, len(self.archive))
        self.assertEqual(
            batches_before,
            len(self.archive.queue_at(1)) + len(self.archive.queue_at(2)),
        )
        # Replayed rows retain their resolved evidence and labels.
        for songid in ("11", "16"):
            self.assertEqual(NEW_ALBUM, self.archive.album_at(2, songid))
            self.assertEqual(OLD_ALBUM, self.archive.album_at(1, songid))

    def test_occurrences_span_every_collection(self) -> None:
        self._collect_incremental()
        occurrences = self.archive.occurrences(SHARED_URI)
        self.assertEqual(4, len(occurrences))
        self.assertEqual(
            {(1, "11"), (1, "16"), (2, "11"), (2, "16")},
            {(row["batch"], row["songid"]) for row in occurrences},
        )

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def test_save_and_load_roundtrip(self) -> None:
        self._collect_incremental()
        self.archive.confirm_directory_update(
            batch=2, directory_items=directory_v2(), ordered=False
        )
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "archive.json")
            self.archive.save(path)
            restored = QueueArchive.load(path)

        self.assertEqual(self.archive.checkpoint, restored.checkpoint)
        self.assertEqual(len(self.archive), len(restored))
        self.assertEqual(
            self.archive.unresolved_records(2),
            restored.unresolved_records(2),
        )
        self.assertEqual(
            self.archive.deletions(), restored.deletions()
        )
        with self.assertRaises(UnresolvedEvidenceError):
            restored.album_at(2, "11")

    def test_incremental_requires_checkpoint(self) -> None:
        fresh = QueueArchive()
        with self.assertRaises(Exception):
            fresh.collect_incremental(2, [], surviving_queue())

    def test_non_empty_plchanges_delta_keeps_songid_identity(self) -> None:
        # A moved entry appears in plchanges while everything else survives;
        # the moved song keeps its songid and is flagged as changed.
        moved = initial_queue()
        moved[1], moved[2] = moved[2], moved[1]
        moved[1]["pos"] = "1"
        moved[2]["pos"] = "2"
        changed = [moved[1]]
        records = self.archive.collect_incremental(
            queue_version=2, changed_songs=changed, current_songs=moved
        )
        self.assertEqual(9, len(records))
        flagged = {row["songid"]: row["changed_in_batch"] for row in records}
        self.assertTrue(flagged["12"])
        self.assertEqual(
            {"12"},
            {row["songid"] for row in records if row["changed_in_batch"]},
        )
        self.assertEqual(2, self.archive._batch(2)["queue_version"])
        self.assertEqual([], self.archive.deletions())

    def test_fetcher_driven_incremental_roundtrip(self) -> None:
        client = FakeMPDClient(
            {
                "status": {"playlist": "2"},
                "plchanges": [],
                "playlistinfo": surviving_queue(),
                "listallinfo": directory_v2(),
            }
        )
        fetcher = MPDArchiveFetcher(client)
        records = self.archive.collect_incremental_from(fetcher)
        self.assertEqual(7, len(records))
        self.assertEqual(2, self.archive.checkpoint["queue_version"])
        revised = self.archive.confirm_directory_update(
            batch=2, directory_items=client.listallinfo(), ordered=True
        )
        self.assertEqual(7, len(revised))
        for songid in ("11", "16"):
            self.assertEqual(NEW_ALBUM, self.archive.album_at(2, songid))

    def test_confirmation_replay_is_idempotent(self) -> None:
        self._collect_incremental()
        first = self.archive.confirm_directory_update(
            batch=2, directory_items=directory_v2(), ordered=False
        )
        second = self.archive.confirm_directory_update(
            batch=2, directory_items=directory_v2(), ordered=False
        )
        self.assertEqual(
            [row["songid"] for row in first],
            [row["songid"] for row in second],
        )
        batch = self.archive._batch(2)
        self.assertEqual(1, len(batch["directory_confirmations"]))
        revisions_after_first = len(self.archive.revisions())
        self.assertEqual(revisions_after_first, 5)

        ordered_first = self.archive.confirm_directory_update(
            batch=2, directory_items=directory_v2(), ordered=True
        )
        ordered_second = self.archive.confirm_directory_update(
            batch=2, directory_items=directory_v2(), ordered=True
        )
        self.assertEqual(
            [row["songid"] for row in ordered_first],
            [row["songid"] for row in ordered_second],
        )
        self.assertEqual(2, len(batch["directory_confirmations"]))
        self.assertEqual(7, len(self.archive.revisions()))


if __name__ == "__main__":
    unittest.main()
