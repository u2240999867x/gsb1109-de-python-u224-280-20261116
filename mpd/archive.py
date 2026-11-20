# python-mpd2: Python MPD client library
#
# A checkpointed archive layer on top of the regular python-mpd2 read
# commands.  It incrementally collects the MPD play queue and the song
# directory into a local repository so that every appearance of a queue
# entry can later be queried against the directory version that was
# provably in effect at that time.
#
# The collection is two-sided:
#
# * the queue side provides song identities (``songid``/``file``/``pos``)
#   together with the queue (playlist) version they were read at;
# * the directory side provides tag evidence (e.g. the album tag) through
#   full directory snapshots, each assigned a local directory version.
#
# A queue record may only be tagged from a directory snapshot whose ordering
# relative to the queue response is proven.  When the available evidence only
# shows that a tag changed during a collection window but cannot show whether
# the change happened before or after the queue delta, the record is stored
# with an explicit unresolved-evidence marker instead of being silently
# backfilled with the newest label.  Once ordering-capable evidence arrives,
# only the affected records are revised and replaying the same material never
# creates additional records.

from __future__ import annotations

import copy
import hashlib
import json
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

EVIDENCE_RESOLVED = "resolved"
EVIDENCE_UNRESOLVED = "unresolved"

DIRECTORY_SIDE = "directory"

SCHEMA_VERSION = 1

# MPD song dictionaries identify a queued file with these keys.
URI_KEY = "file"
SONGID_KEY = "id"
POSITION_KEY = "pos"
ALBUM_KEY = "album"


class ArchiveError(Exception):
    """Base class for archive failures."""


class UnresolvedEvidenceError(ArchiveError):
    """Raised when a tag lookup lacks ordered directory evidence."""


def _song_uri(song: Dict[str, Any]) -> str:
    try:
        return str(song[URI_KEY])
    except KeyError as exc:
        raise ArchiveError("queue/directory song lacks a %r key" % URI_KEY) from exc


def _song_id(song: Dict[str, Any]) -> str:
    try:
        return str(song[SONGID_KEY])
    except KeyError as exc:
        raise ArchiveError("queue song lacks a %r key" % SONGID_KEY) from exc


def _song_position(song: Dict[str, Any]) -> Optional[int]:
    if POSITION_KEY not in song:
        return None
    return int(song[POSITION_KEY])


def _song_album(song: Optional[Dict[str, Any]]) -> Any:
    if song is None:
        return None
    return song.get(ALBUM_KEY)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


class MPDArchiveFetcher:
    """Thin adapter around the existing python-mpd2 read commands.

    Only regular read entries of the synchronous :class:`mpd.MPDClient` are
    used; parsing stays inside python-mpd2 and this adapter merely groups the
    calls a collection round needs.  The asyncio client exposes the same
    command names for callers that need an asynchronous variant.
    """

    def __init__(self, client: Any) -> None:
        self.client = client

    def queue_full(self) -> Tuple[int, List[Dict[str, Any]]]:
        """Read the whole queue, preserving full ``playlistinfo`` semantics."""
        status = self.client.status()
        songs = list(self.client.playlistinfo())
        return int(status["playlist"]), songs

    def queue_delta(
        self, version: int
    ) -> Tuple[int, List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Read a queue increment plus the resulting full queue.

        ``plchanges`` carries the changed songs since ``version`` while the
        accompanying ``playlistinfo`` re-read establishes the surviving
        songid set, which is the only way to observe deletions (MPD does not
        report removed songids through ``plchanges``).
        """
        status = self.client.status()
        changed = list(self.client.plchanges(version))
        current = list(self.client.playlistinfo())
        return int(status["playlist"]), changed, current

    def directory_full(self) -> List[Dict[str, Any]]:
        """Read a complete directory snapshot via ``listallinfo``."""
        return list(self.client.listallinfo())


class QueueArchive:
    """Checkpointed repository of queue appearances and directory versions.

    The repository is a plain JSON document, so it can be inspected or
    archived alongside the music collection.  Passing ``path=None`` keeps the
    whole archive in memory, which is mainly useful for tests.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path
        self._state: Dict[str, Any] = self._empty_state()
        if path is not None and os.path.exists(path):
            self._state = self._load(path)

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _empty_state() -> Dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "checkpoint": {
                "queue_version": None,
                "directory_version": None,
                "batch_seq": 0,
            },
            # hash of a directory snapshot -> directory version
            "directory_hashes": {},
            # directory version (str) -> snapshot payload
            "directory_versions": {},
            # processed collection batches, including the raw fetch results
            "batches": [],
            # one record per (batch, songid) appearance
            "queue_entries": [],
            # deletions observed through full-queue re-reads
            "deletions": [],
            # append-only revision log for evidence repairs
            "revisions": [],
            "revision_seq": 0,
            # songid -> batch of first appearance
            "first_seen": {},
        }

    @staticmethod
    def _load(path: str) -> Dict[str, Any]:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        if state.get("schema") != SCHEMA_VERSION:
            raise ArchiveError(
                "unsupported archive schema: %r" % state.get("schema")
            )
        return state

    def save(self, path: Optional[str] = None) -> None:
        target = path or self.path
        if target is None:
            raise ArchiveError("no repository path configured")
        temporary = target + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(
                self._state,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
        os.replace(temporary, target)

    @classmethod
    def load(cls, path: str) -> "QueueArchive":
        return cls(path)

    # ------------------------------------------------------------------
    # directory snapshots
    # ------------------------------------------------------------------

    @staticmethod
    def _snapshot_from_items(
        items: Sequence[Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        snapshot: Dict[str, Dict[str, Any]] = {}
        for item in items:
            # listallinfo also yields directory/playlist entries; only the
            # "file" objects carry song tags.
            if URI_KEY in item:
                snapshot[str(item[URI_KEY])] = copy.deepcopy(dict(item))
        return snapshot

    def _store_directory(self, items: Sequence[Dict[str, Any]]) -> int:
        snapshot = self._snapshot_from_items(items)
        digest = hashlib.sha256(_canonical(snapshot).encode("utf-8")).hexdigest()
        existing = self._state["directory_hashes"].get(digest)
        if existing is not None:
            return int(existing)
        version = len(self._state["directory_versions"]) + 1
        self._state["directory_hashes"][digest] = version
        self._state["directory_versions"][str(version)] = {
            "collected_batch": self._state["checkpoint"]["batch_seq"],
            "song_count": len(snapshot),
            "songs": snapshot,
        }
        return version

    def _directory_song(
        self, version: Optional[int], uri: str
    ) -> Optional[Dict[str, Any]]:
        if version is None:
            return None
        payload = self._state["directory_versions"].get(str(version))
        if payload is None:
            return None
        return payload["songs"].get(uri)

    def _ordered_directory_version(self) -> Optional[int]:
        return self._state["checkpoint"]["directory_version"]

    # ------------------------------------------------------------------
    # collection entry points
    # ------------------------------------------------------------------

    def collect_full_from(self, fetcher: MPDArchiveFetcher) -> List[Dict[str, Any]]:
        """Run a complete, consistent queue + directory re-collection."""
        queue_version, queue_songs = fetcher.queue_full()
        directory_items = fetcher.directory_full()
        return self.collect_full(queue_version, queue_songs, directory_items)

    def collect_full(
        self,
        queue_version: int,
        queue_songs: Sequence[Dict[str, Any]],
        directory_items: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Ingest a full queue and a full directory snapshot together.

        Both sides are read against the same checkpoint, so every queue
        record is directly resolved against the new directory version.
        """
        directory_version = self._store_directory(directory_items)
        batch = self._next_batch()
        records = self._ingest_queue(
            batch=batch,
            kind="full",
            queue_version=int(queue_version),
            changed_songs=list(queue_songs),
            current_songs=list(queue_songs),
            ordered_directory_version=directory_version,
            raw_queue=list(queue_songs),
            raw_directory=list(directory_items),
        )
        self._state["checkpoint"]["queue_version"] = int(queue_version)
        self._state["checkpoint"]["directory_version"] = directory_version
        return records

    def collect_incremental_from(
        self, fetcher: MPDArchiveFetcher
    ) -> List[Dict[str, Any]]:
        """Run an incremental queue collection against the stored checkpoint."""
        checkpoint_version = self._state["checkpoint"]["queue_version"]
        if checkpoint_version is None:
            raise ArchiveError("no queue checkpoint to diff against")
        queue_version, changed, current = fetcher.queue_delta(checkpoint_version)
        return self.collect_incremental(queue_version, changed, current)

    def collect_incremental(
        self,
        queue_version: int,
        changed_songs: Sequence[Dict[str, Any]],
        current_songs: Sequence[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Ingest a ``plchanges`` delta plus the post-change full queue.

        One appearance record is archived per surviving songid (deletions are
        derived from the songids missing from ``current_songs``).  No
        directory evidence is read in this round, so the new records are
        marked unresolved until a directory snapshot can be ordered against
        them.
        """
        previous_version = self._state["checkpoint"]["queue_version"]
        if previous_version is None:
            raise ArchiveError("no queue checkpoint to diff against")
        if int(queue_version) < int(previous_version):
            raise ArchiveError(
                "queue version moved backwards: %s < %s"
                % (queue_version, previous_version)
            )
        batch = self._next_batch()
        records = self._ingest_queue(
            batch=batch,
            kind="incremental",
            queue_version=int(queue_version),
            changed_songs=list(changed_songs),
            current_songs=list(current_songs),
            ordered_directory_version=self._ordered_directory_version(),
            raw_queue=list(current_songs),
            raw_directory=None,
        )
        self._record_deletions(batch, current_songs)
        self._state["checkpoint"]["queue_version"] = int(queue_version)
        return records

    def confirm_directory_update(
        self,
        batch: int,
        directory_items: Sequence[Dict[str, Any]],
        ordered: bool,
    ) -> List[Dict[str, Any]]:
        """Attach directory evidence to a previously collected batch.

        ``ordered=False`` models a confirmation that only proves a tag changed
        while the batch was being collected: records whose tag is provably
        unchanged are resolved against the baseline version, while changed
        records stay unresolved and keep the candidate directory version as
        evidence that still lacks ordering.

        ``ordered=True`` means the snapshot's position relative to the queue
        delta is proven, so every still-unresolved record of the batch is
        revised against it.  Only the affected records change.
        """
        target_batch = self._batch(batch)
        directory_version = self._store_directory(directory_items)
        for confirmation in target_batch["directory_confirmations"]:
            # Replaying identical evidence is a no-op: no duplicate
            # confirmation entries and no duplicate revisions.
            if (
                confirmation["directory_version"] == directory_version
                and confirmation["ordered"] == bool(ordered)
            ):
                return [
                    self._find_record(batch, songid)
                    for songid in confirmation["revised_songids"]
                ]
        snapshot = self._state["directory_versions"][str(directory_version)][
            "songs"
        ]
        baseline_version = target_batch.get("directory_version")
        revised: List[Dict[str, Any]] = []
        for record in self._batch_records(batch):
            if record["evidence"] == EVIDENCE_RESOLVED:
                continue
            uri = record["uri"]
            new_song = snapshot.get(uri)
            new_album = _song_album(new_song)
            if ordered:
                self._revise_record(
                    record,
                    directory_version=directory_version,
                    album=new_album,
                    reason="ordered",
                )
                revised.append(record)
            elif self._album_matches(baseline_version, uri, new_album):
                # The label did not change, so its ordering relative to the
                # queue delta is irrelevant: the baseline version remains
                # valid evidence for this appearance.
                self._revise_record(
                    record,
                    directory_version=baseline_version,
                    album=_song_album(
                        self._directory_song(baseline_version, uri)
                    ),
                    reason="unchanged",
                )
                revised.append(record)
            else:
                record["candidate_directory_version"] = directory_version
                if DIRECTORY_SIDE not in record["missing_evidence"]:
                    record["missing_evidence"].append(DIRECTORY_SIDE)
                if directory_version not in record["missing_evidence_versions"]:
                    record["missing_evidence_versions"].append(directory_version)
        target_batch["directory_confirmations"].append(
            {
                "directory_version": directory_version,
                "ordered": bool(ordered),
                "revised_songids": [record["songid"] for record in revised],
            }
        )
        if ordered:
            self._state["checkpoint"]["directory_version"] = directory_version
        return revised

    # ------------------------------------------------------------------
    # core ingestion
    # ------------------------------------------------------------------

    def _next_batch(self) -> int:
        return int(self._state["checkpoint"]["batch_seq"]) + 1

    def _batch(self, batch: int) -> Dict[str, Any]:
        for stored in self._state["batches"]:
            if stored["batch"] == batch:
                return stored
        raise ArchiveError("unknown batch: %s" % batch)

    def _batch_records(self, batch: int) -> List[Dict[str, Any]]:
        return [
            record
            for record in self._state["queue_entries"]
            if record["batch"] == batch
        ]

    def _album_matches(
        self, baseline_version: Optional[int], uri: str, album: Any
    ) -> bool:
        if baseline_version is None:
            return False
        baseline_song = self._directory_song(baseline_version, uri)
        if baseline_song is None:
            return False
        return _canonical(_song_album(baseline_song)) == _canonical(album)

    def _ingest_queue(
        self,
        batch: int,
        kind: str,
        queue_version: int,
        changed_songs: List[Dict[str, Any]],
        current_songs: List[Dict[str, Any]],
        ordered_directory_version: Optional[int],
        raw_queue: List[Dict[str, Any]],
        raw_directory: Optional[List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        # Idempotent replay: reprocessing archived material for a batch must
        # neither duplicate appearance records nor batch metadata.
        if any(stored["batch"] == batch for stored in self._state["batches"]):
            return self._batch_records(batch)

        changed_ids = {_song_id(song) for song in changed_songs}
        records: List[Dict[str, Any]] = []
        for song in current_songs:
            songid = _song_id(song)
            uri = _song_uri(song)
            first_seen = self._state["first_seen"].get(songid, batch)
            self._state["first_seen"][songid] = first_seen
            resolved = kind == "full" and ordered_directory_version is not None
            directory_song = (
                self._directory_song(ordered_directory_version, uri)
                if resolved
                else None
            )
            record = {
                "batch": batch,
                "songid": songid,
                "uri": uri,
                "position": _song_position(song),
                "changed_in_batch": songid in changed_ids,
                "first_seen_batch": first_seen,
                "directory_version": (
                    ordered_directory_version if resolved else None
                ),
                "evidence": (
                    EVIDENCE_RESOLVED if resolved else EVIDENCE_UNRESOLVED
                ),
                "missing_evidence": [] if resolved else [DIRECTORY_SIDE],
                "missing_evidence_versions": [],
                "candidate_directory_version": None,
                "album": _song_album(directory_song) if resolved else None,
                "song": copy.deepcopy(dict(song)),
            }
            self._state["queue_entries"].append(record)
            records.append(record)

        self._state["batches"].append(
            {
                "batch": batch,
                "kind": kind,
                "queue_version": queue_version,
                "directory_version": ordered_directory_version,
                "directory_collected": raw_directory is not None,
                "directory_confirmations": [],
                "current_songids": [_song_id(song) for song in current_songs],
                "changed_songids": sorted(changed_ids),
                "raw_queue": copy.deepcopy(raw_queue),
                "raw_directory": (
                    copy.deepcopy(raw_directory)
                    if raw_directory is not None
                    else None
                ),
            }
        )
        self._state["checkpoint"]["batch_seq"] = batch
        return records

    def _find_record(self, batch: int, songid: str) -> Dict[str, Any]:
        for record in self._state["queue_entries"]:
            if record["batch"] == batch and record["songid"] == songid:
                return record
        raise ArchiveError(
            "unknown record for batch %s songid %s" % (batch, songid)
        )

    def _record_deletions(
        self, batch: int, current_songs: List[Dict[str, Any]]
    ) -> None:
        if len(self._state["batches"]) < 2:
            return
        previous = self._state["batches"][-2]
        surviving = {_song_id(song) for song in current_songs}
        known_deleted = {entry["songid"] for entry in self._state["deletions"]}
        for song in previous["raw_queue"]:
            songid = _song_id(song)
            if songid not in surviving and songid not in known_deleted:
                self._state["deletions"].append(
                    {
                        "batch": batch,
                        "songid": songid,
                        "uri": _song_uri(song),
                        "last_position": _song_position(song),
                    }
                )
                known_deleted.add(songid)

    def _revise_record(
        self,
        record: Dict[str, Any],
        directory_version: Optional[int],
        album: Any,
        reason: str,
    ) -> None:
        before = {
            "evidence": record["evidence"],
            "directory_version": record["directory_version"],
            "album": record["album"],
            "missing_evidence": list(record["missing_evidence"]),
        }
        record["evidence"] = EVIDENCE_RESOLVED
        record["directory_version"] = directory_version
        record["album"] = album
        record["missing_evidence"] = []
        record["missing_evidence_versions"] = []
        record["candidate_directory_version"] = None
        self._state["revision_seq"] += 1
        self._state["revisions"].append(
            {
                "revision": self._state["revision_seq"],
                "batch": record["batch"],
                "songid": record["songid"],
                "uri": record["uri"],
                "reason": reason,
                "before": before,
                "after": {
                    "evidence": record["evidence"],
                    "directory_version": directory_version,
                    "album": album,
                },
            }
        )

    # ------------------------------------------------------------------
    # replay
    # ------------------------------------------------------------------

    def replay_stored(self) -> int:
        """Re-process every stored batch from its archived raw material.

        Returns the number of newly created records, which is always zero:
        ingestion is keyed by (batch, songid), so replaying the same material
        never duplicates appearances.
        """
        before = len(self._state["queue_entries"])
        for stored in list(self._state["batches"]):
            changed = [
                song
                for song in stored["raw_queue"]
                if _song_id(song) in set(stored["changed_songids"])
            ]
            self._ingest_queue(
                batch=stored["batch"],
                kind=stored["kind"],
                queue_version=stored["queue_version"],
                changed_songs=changed,
                current_songs=stored["raw_queue"],
                ordered_directory_version=stored["directory_version"],
                raw_queue=stored["raw_queue"],
                raw_directory=stored["raw_directory"],
            )
        return len(self._state["queue_entries"]) - before

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    @property
    def checkpoint(self) -> Dict[str, Any]:
        return copy.deepcopy(self._state["checkpoint"])

    def directory_version_count(self) -> int:
        return len(self._state["directory_versions"])

    def latest_directory_album(self, uri: str) -> Any:
        """Return the album tag from the newest stored snapshot, if any.

        This is an explicit current-label lookup; it deliberately reads the
        newest stored directory version even when that version is not yet
        order-proven against a queue batch.  Historical queue records are
        never backfilled from it -- :meth:`album_at` enforces that.
        """
        if not self._state["directory_versions"]:
            return None
        newest = max(int(version) for version in self._state["directory_versions"])
        return _song_album(self._directory_song(newest, uri))

    def queue_at(self, batch: int) -> List[Dict[str, Any]]:
        """Return every appearance archived for ``batch``.

        Unresolved records expose ``evidence == "unresolved"`` and ``album is
        None``; the caller must not substitute the latest directory label.
        """
        return [copy.deepcopy(record) for record in self._batch_records(batch)]

    def latest_queue(self) -> List[Dict[str, Any]]:
        """Return the current queue, one identity row per surviving songid."""
        if not self._state["batches"]:
            return []
        latest = self._state["batches"][-1]
        rows: List[Dict[str, Any]] = []
        for song in latest["raw_queue"]:
            songid = _song_id(song)
            record = self._latest_record_for(songid)
            rows.append(
                {
                    "songid": songid,
                    "uri": _song_uri(song),
                    "position": _song_position(song),
                    "evidence": (
                        record["evidence"] if record is not None
                        else EVIDENCE_RESOLVED
                    ),
                    "directory_version": (
                        record["directory_version"]
                        if record is not None
                        else None
                    ),
                    "album": record["album"] if record is not None else None,
                    "record_batch": record["batch"] if record is not None else None,
                }
            )
        return rows

    def _latest_record_for(self, songid: str) -> Optional[Dict[str, Any]]:
        for record in reversed(self._state["queue_entries"]):
            if record["songid"] == songid:
                return record
        return None

    def occurrences(self, uri: str) -> List[Dict[str, Any]]:
        """Return every archived appearance of ``uri`` across all batches."""
        return [
            copy.deepcopy(record)
            for record in self._state["queue_entries"]
            if record["uri"] == uri
        ]

    def album_at(self, batch: int, songid: str) -> Any:
        """Return the album tag provably in effect for an appearance.

        Raises :class:`UnresolvedEvidenceError` when the directory-side
        version evidence is missing instead of guessing from the newest
        snapshot.
        """
        record = self._find_record(batch, songid)
        if record["evidence"] != EVIDENCE_RESOLVED:
            raise UnresolvedEvidenceError(
                "batch %s songid %s lacks ordered directory evidence "
                "(missing: %s)"
                % (batch, songid, ",".join(record["missing_evidence"]))
            )
        return record["album"]

    def deletions(self) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._state["deletions"])

    def revisions(self) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._state["revisions"])

    def unresolved_records(
        self, batch: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        if batch is not None:
            records = self._batch_records(batch)
        else:
            records = self._state["queue_entries"]
        return [
            copy.deepcopy(record)
            for record in records
            if record["evidence"] == EVIDENCE_UNRESOLVED
        ]

    def __len__(self) -> int:
        return len(self._state["queue_entries"])
