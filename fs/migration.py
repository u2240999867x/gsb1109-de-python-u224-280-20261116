"""Resumable, journaled migration of resources between filesystems.

This module moves a batch of *materials* (files) from one backend
filesystem to another without losing track of progress when the process
is interrupted.

Every resource is driven through an explicit state machine, and each
transition is persisted to a JSON journal on disk:

``pending`` -> ``copied`` -> ``verified`` -> ``deletable`` -> ``completed``

A resource whose destination already exists with *different* contents is
moved to the ``conflict`` state instead of being overwritten, and the
conflict is reported before anything is written to the destination.

When several migration tasks share the same source, a :class:`ShareRegistry`
records every task's claim on a source resource. A source only becomes
``deletable`` once *all* claiming tasks have verified their own copies, so
no single task can delete a source that another task still depends on.

Re-running (replaying) a migration is safe: resources that already reached
``completed`` are skipped, destinations are always re-written by
truncation (never by appending), and an existing destination whose
checksum already matches the source is left untouched.
"""

from __future__ import print_function, unicode_literals

import typing

import json
import os
import tempfile
import threading
import uuid
from collections import namedtuple
from datetime import datetime

from ._fscompat import fsdecode
from .copy import copy_file_internal
from .errors import MigrationConflict, MigrationIncomplete, ResourceNotFound
from .osfs import OSFS
from .path import abspath, basename, combine, dirname, frombase, normpath
from .tools import remove_empty
from .walk import Walker

if typing.TYPE_CHECKING:
    from typing import Any, Callable, Dict, List, Optional, Text, Tuple, Union

    from .base import FS

    _OnEvent = Callable[[Text, "JournalEntry"], None]


# ---------------------------------------------------------------------------
# States
# ---------------------------------------------------------------------------

STATE_PENDING = "pending"
STATE_COPIED = "copied"
STATE_VERIFIED = "verified"
STATE_DELETABLE = "deletable"
STATE_COMPLETED = "completed"
STATE_CONFLICT = "conflict"

STATES = (
    STATE_PENDING,
    STATE_COPIED,
    STATE_VERIFIED,
    STATE_DELETABLE,
    STATE_COMPLETED,
    STATE_CONFLICT,
)

# Allowed state transitions. A transition to the same state is always
# permitted (journal writes are idempotent).
_TRANSITIONS = {
    STATE_PENDING: (STATE_COPIED, STATE_CONFLICT),
    STATE_COPIED: (STATE_VERIFIED, STATE_CONFLICT, STATE_PENDING),
    STATE_VERIFIED: (STATE_DELETABLE, STATE_COMPLETED, STATE_CONFLICT),
    STATE_DELETABLE: (STATE_COMPLETED, STATE_PENDING),
    STATE_COMPLETED: (),
    STATE_CONFLICT: (STATE_PENDING,),
}


JournalEntry = namedtuple(
    "JournalEntry",
    [
        "src_path",
        "dst_path",
        "state",
        "src_hash",
        "dst_hash",
        "size",
        "updated_at",
        "attempts",
        "error",
    ],
)
"""A single journaled resource and its migration state."""


MigrationResult = namedtuple(
    "MigrationResult",
    [
        "total",
        "pending",
        "copied",
        "verified",
        "deletable",
        "completed",
        "conflicts",
    ],
)
"""Summary of the states of all resources after a migration run."""


# ---------------------------------------------------------------------------
# Locking helpers
# ---------------------------------------------------------------------------

try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - non POSIX platforms
    fcntl = None  # type: ignore


class _FileLock(object):
    """An inter-thread, and best-effort inter-process, exclusive lock.

    A side-car lock file is used with ``flock`` when the filesystem exposes
    a system path. On filesystems without a system path only the in-process
    threading lock is available. The lock is reentrant for the owning
    thread, so journal methods that nest under an already held lock do not
    deadlock.
    """

    _global_lock = threading.RLock()

    def __init__(self, fs, lock_path):
        # type: (FS, Text) -> None
        self._fs = fs
        self._lock_path = lock_path
        self._thread_lock = threading.RLock()
        self._local = threading.local()
        self._file_obj = None

    def __enter__(self):
        # type: () -> _FileLock
        self._thread_lock.acquire()
        _FileLock._global_lock.acquire()
        depth = getattr(self._local, "depth", 0)
        if depth == 0:
            try:
                if self._fs.hassyspath(self._lock_path):
                    sys_path = self._fs.getsyspath(self._lock_path)
                    self._file_obj = open(sys_path, "a+b")
                    if fcntl is not None:
                        fcntl.flock(self._file_obj.fileno(), fcntl.LOCK_EX)
            except Exception:
                self._release_once()
                raise
        self._local.depth = depth + 1
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._release_once()
        return False

    def _release_once(self):
        # type: () -> None
        depth = getattr(self._local, "depth", 0)
        if depth <= 1 and self._file_obj is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(self._file_obj.fileno(), fcntl.LOCK_UN)
            finally:
                self._file_obj.close()
                self._file_obj = None
        self._local.depth = max(depth - 1, 0)
        _FileLock._global_lock.release()
        self._thread_lock.release()


def _utc_now():
    # type: () -> Text
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _local_fs_and_name(path):
    # type: (Text) -> Tuple[FS, Text]
    """Split a local filesystem path into an `OSFS` and a resource name."""
    absolute = os.path.abspath(fsdecode(path))
    return OSFS(dirname(absolute), create=True), basename(absolute)


def _atomic_write(fs, path, data):
    # type: (FS, Text, bytes) -> None
    """Write bytes to ``path`` atomically where possible."""
    if fs.hassyspath(path):
        sys_path = fs.getsyspath(path)
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix="." + basename(path) + ".", dir=dirname(sys_path)
        )
        try:
            with os.fdopen(tmp_fd, "wb") as tmp_file:
                tmp_file.write(data)
            _os_replace = getattr(os, "replace", os.rename)
            _os_replace(tmp_path, sys_path)
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
    else:  # pragma: no cover - journals are expected on local storage
        with fs.openbin(path, "w") as write_file:
            write_file(data)


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------


class MigrationJournal(object):
    """Persistent, per-resource record of a migration's progress.

    The journal is a JSON document stored on a (typically local)
    filesystem. All writes go through a lock and an atomic replace, and a
    stale journal can be re-opened by a later process to resume the
    migration.

    Arguments:
        journal_path (str): Path to the JSON journal file.
        journal_fs (FS, optional): Filesystem holding the journal. If not
            given, a local `~fs.osfs.OSFS` is opened on the directory
            containing ``journal_path``.
        job_id (str, optional): Explicit identifier of the migration task.
            Generated when a new journal is created.
        hash_algorithm (str): Hash algorithm recorded and used for
            verification, e.g. ``"sha256"``.
    """

    def __init__(
        self,
        journal_path,  # type: Text
        journal_fs=None,  # type: Optional[FS]
        job_id=None,  # type: Optional[Text]
        hash_algorithm="sha256",  # type: Text
    ):
        # type: (...) -> None
        self._path = abspath(normpath(journal_path))
        if journal_fs is None:
            journal_fs, self._name = _local_fs_and_name(journal_path)
        else:
            self._name = basename(self._path)
        self._fs = journal_fs
        self._lock = _FileLock(self._fs, self._name + ".lock")
        self._hash_algorithm = hash_algorithm
        self._data = self._empty_data(job_id or uuid.uuid4().hex, hash_algorithm)
        self.load()

    @staticmethod
    def _empty_data(job_id, hash_algorithm):
        # type: (Text, Text) -> Dict[Text, Any]
        return {
            "version": 1,
            "job_id": job_id,
            "hash_algorithm": hash_algorithm,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "entries": {},
        }

    @property
    def path(self):
        # type: () -> Text
        return self._path

    @property
    def job_id(self):
        # type: () -> Text
        return self._data["job_id"]

    @property
    def hash_algorithm(self):
        # type: () -> Text
        return self._data.get("hash_algorithm", self._hash_algorithm)

    @property
    def lock(self):
        # type: () -> _FileLock
        return self._lock

    def load(self):
        # type: () -> None
        """Load the journal from disk, creating it if absent."""
        with self._lock:
            if not self._fs.exists(self._name):
                self._data["hash_algorithm"] = self._hash_algorithm
                self._save_locked()
                return
            with self._fs.openbin(self._name) as read_file:
                data = json.loads(read_file.read().decode("utf-8"))
            data.setdefault("entries", {})
            self._data = data

    def _save_locked(self):
        # type: () -> None
        self._data["updated_at"] = _utc_now()
        payload = json.dumps(self._data, ensure_ascii=False, indent=2)
        _atomic_write(self._fs, self._name, payload.encode("utf-8"))

    def save(self):
        # type: () -> None
        """Persist the current journal contents atomically."""
        with self._lock:
            self._save_locked()

    def entries(self):
        # type: () -> Dict[Text, JournalEntry]
        """Return all journaled entries keyed by source path."""
        return {
            key: JournalEntry(**record)
            for key, record in self._data["entries"].items()
        }

    def get(self, src_path):
        # type: (Text) -> Optional[JournalEntry]
        """Return the entry for ``src_path`` if it is journaled."""
        record = self._data["entries"].get(normpath(src_path))
        return None if record is None else JournalEntry(**record)

    def upsert(self, src_path, dst_path, **fields):
        # type: (Text, Text, **Any) -> JournalEntry
        """Create or update the entry for ``src_path``.

        State changes are validated against the migration state machine.
        """
        key = normpath(src_path)
        record = self._data["entries"].get(key)
        if record is None:
            record = {
                "src_path": key,
                "dst_path": normpath(dst_path),
                "state": STATE_PENDING,
                "src_hash": None,
                "dst_hash": None,
                "size": None,
                "updated_at": _utc_now(),
                "attempts": 0,
                "error": None,
            }
            self._data["entries"][key] = record
        new_state = fields.get("state")
        if new_state is not None and new_state != record["state"]:
            allowed = _TRANSITIONS[record["state"]]
            if new_state not in allowed:
                raise ValueError(
                    "illegal transition {!r} -> {!r} for {}".format(
                        record["state"], new_state, key
                    )
                )
        record.update(fields)
        record["updated_at"] = _utc_now()
        return JournalEntry(**record)

    def transition(self, src_path, state, error=None):
        # type: (Text, Text, Optional[Text]) -> JournalEntry
        """Move an entry to ``state`` and persist the journal."""
        with self._lock:
            current = self.get(src_path)
            if current is None:
                raise KeyError("no journal entry for {}".format(src_path))
            entry = self.upsert(src_path, current.dst_path, state=state)
            if error is not None:
                entry = self.upsert(
                    src_path, entry.dst_path, error=error, attempts=entry.attempts + 1
                )
            self._save_locked()
        return entry

    def resolve_conflict(self, src_path):
        # type: (Text) -> JournalEntry
        """Clear a ``conflict`` entry after manual resolution.

        The resource returns to ``pending`` and will be re-checked from
        scratch on the next run.
        """
        with self._lock:
            entry = self.get(src_path)
            if entry is None or entry.state != STATE_CONFLICT:
                raise ValueError("{} is not in the conflict state".format(src_path))
            entry = self.upsert(
                src_path, entry.dst_path, state=STATE_PENDING, error=None
            )
            self._save_locked()
        return entry

    def counts(self):
        # type: () -> Dict[Text, int]
        """Return the number of entries in each state."""
        counts = {state: 0 for state in STATES}
        for record in self._data["entries"].values():
            counts[record["state"]] += 1
        return counts

    def conflicts(self):
        # type: () -> List[JournalEntry]
        """Return all entries stuck in the ``conflict`` state."""
        return [
            JournalEntry(**record)
            for record in self._data["entries"].values()
            if record["state"] == STATE_CONFLICT
        ]

    def result(self):
        # type: () -> MigrationResult
        counts = self.counts()
        pending = (
            counts[STATE_PENDING]
            + counts[STATE_COPIED]
            + counts[STATE_VERIFIED]
            + counts[STATE_DELETABLE]
        )
        return MigrationResult(
            total=len(self._data["entries"]),
            pending=pending,
            copied=counts[STATE_COPIED],
            verified=counts[STATE_VERIFIED],
            deletable=counts[STATE_DELETABLE],
            completed=counts[STATE_COMPLETED],
            conflicts=[entry.src_path for entry in self.conflicts()],
        )


# ---------------------------------------------------------------------------
# Shared source registry
# ---------------------------------------------------------------------------


class ShareRegistry(object):
    """Registry tracking shared source resources across migration tasks.

    Every task that may read from the same source backend should be given
    the same :class:`ShareRegistry` location. A task claims each source
    resource while planning, marks the claim as verified once its own copy
    is validated, and a source may only be deleted when *all* claims are
    verified. This prevents one task from deleting a source another task
    still needs.

    Arguments:
        registry_path (str): Path to the JSON registry file.
        registry_fs (FS, optional): Filesystem holding the registry.
            Defaults to a local `~fs.osfs.OSFS`.
    """

    def __init__(self, registry_path, registry_fs=None):
        # type: (Text, Optional[FS]) -> None
        self._path = abspath(normpath(registry_path))
        if registry_fs is None:
            registry_fs, self._name = _local_fs_and_name(registry_path)
        else:
            self._name = basename(self._path)
        self._fs = registry_fs
        self._lock = _FileLock(self._fs, self._name + ".lock")
        self._data = {"version": 1, "resources": {}}
        self._load()

    def _load(self):
        # type: () -> None
        with self._lock:
            if self._fs.exists(self._name):
                with self._fs.openbin(self._name) as read_file:
                    self._data = json.loads(read_file.read().decode("utf-8"))
            self._data.setdefault("resources", {})

    def _save_locked(self):
        # type: () -> None
        payload = json.dumps(self._data, ensure_ascii=False, indent=2)
        _atomic_write(self._fs, self._name, payload.encode("utf-8"))

    @staticmethod
    def source_key(src_fs, src_path, source_name=None):
        # type: (FS, Text, Optional[Text]) -> Text
        """Build the registry key for a source resource.

        An explicit ``source_name`` namespaces paths for backends without
        system paths. When the filesystem exposes a system path it is used
        directly so that identical files are recognised across tasks.
        """
        if source_name is not None:
            return "{}!{}".format(source_name, normpath(src_path))
        if src_fs.hassyspath(src_path):
            return "sys!" + abspath(src_fs.getsyspath(src_path))
        return "rel!" + normpath(src_path)

    def claim(self, job_id, key, destination=None):
        # type: (Text, Text, Optional[Text]) -> None
        """Register that ``job_id`` needs the source resource ``key``."""
        with self._lock:
            resource = self._data["resources"].setdefault(
                key, {"deleted": False, "claims": []}
            )
            for claim in resource["claims"]:
                if claim["job_id"] == job_id:
                    return
            resource["claims"].append(
                {"job_id": job_id, "state": STATE_PENDING, "dst": destination}
            )
            self._save_locked()

    def mark_verified(self, job_id, key):
        # type: (Text, Text) -> None
        """Record that ``job_id`` has a verified copy of ``key``."""
        with self._lock:
            resource = self._data["resources"].get(key)
            if resource is None:
                return
            for claim in resource["claims"]:
                if claim["job_id"] == job_id:
                    claim["state"] = STATE_VERIFIED
            self._save_locked()

    def can_delete(self, key):
        # type: (Text) -> bool
        """Check whether every claimant of ``key`` has a verified copy."""
        with self._lock:
            resource = self._data["resources"].get(key)
            if resource is None or resource.get("deleted"):
                return True
            claims = resource["claims"]
            if not claims:
                return False
            return all(claim["state"] == STATE_VERIFIED for claim in claims)

    def is_deleted(self, key):
        # type: (Text) -> bool
        with self._lock:
            resource = self._data["resources"].get(key)
            return bool(resource and resource.get("deleted"))

    def mark_deleted(self, key):
        # type: (Text) -> None
        """Record the deletion of ``key`` and release every claim."""
        with self._lock:
            resource = self._data["resources"].get(key)
            if resource is not None:
                resource["deleted"] = True
                for claim in resource["claims"]:
                    claim["state"] = "released"
            self._save_locked()

    def release_job(self, job_id):
        # type: (Text) -> None
        """Drop all claims held by an abandoned task.

        This unblocks deletion of shared sources when a task is known to
        have been cancelled; it must only be called once the task is truly
        stopped.
        """
        with self._lock:
            for resource in self._data["resources"].values():
                resource["claims"] = [
                    claim
                    for claim in resource["claims"]
                    if claim["job_id"] != job_id
                ]
            self._save_locked()


# ---------------------------------------------------------------------------
# Migrator
# ---------------------------------------------------------------------------


class Migrator(object):
    """Drive a resumable migration between two filesystems.

    Arguments:
        src_fs (FS or str): Source filesystem (instance or URL).
        dst_fs (FS or str): Destination filesystem (instance or URL).
        journal (MigrationJournal): Journal used to persist progress.
        walker (~fs.walk.Walker, optional): Walker selecting the resources
            to migrate.
        hash_algorithm (str, optional): Checksum algorithm. Defaults to the
            algorithm recorded in the journal.
        preserve_time (bool): Try to preserve modification times when
            copying.
        delete_source (bool): If `True` (the default), source files are
            removed once every sharing task has a verified copy. If
            `False`, sources are retained and verification completes the
            migration.
        share_registry (ShareRegistry, optional): Registry coordinating
            deletion of sources shared with other tasks.
        source_name (str, optional): Namespace used when building share
            registry keys for backends without system paths.
        conflict_policy (str): ``"raise"`` (default) stops at the first
            conflicting resource, ``"skip"`` records every conflict and
            continues with the remaining resources.
        on_event (callable, optional): Callback invoked with
            ``(event, entry)`` after every state change.
    """

    def __init__(
        self,
        src_fs,  # type: Union[FS, Text]
        dst_fs,  # type: Union[FS, Text]
        journal,  # type: MigrationJournal
        walker=None,  # type: Optional[Walker]
        hash_algorithm=None,  # type: Optional[Text]
        preserve_time=False,  # type: bool
        delete_source=True,  # type: bool
        share_registry=None,  # type: Optional[ShareRegistry]
        source_name=None,  # type: Optional[Text]
        conflict_policy="raise",  # type: Text
        on_event=None,  # type: Optional[_OnEvent]
    ):
        # type: (...) -> None
        if conflict_policy not in ("raise", "skip"):
            raise ValueError(
                "conflict_policy must be 'raise' or 'skip', not {!r}".format(
                    conflict_policy
                )
            )
        self._src_fs = src_fs
        self._dst_fs = dst_fs
        self.journal = journal
        self.walker = walker or Walker()
        self.hash_algorithm = hash_algorithm or journal.hash_algorithm
        self.preserve_time = preserve_time
        self.delete_source = delete_source
        self.share_registry = share_registry
        self.source_name = source_name
        self.conflict_policy = conflict_policy
        self.on_event = on_event or (lambda event, entry: None)

    def _emit(self, event, entry):
        # type: (Text, JournalEntry) -> None
        self.on_event(event, entry)

    def _source_key(self, src_fs, src_path):
        # type: (FS, Text) -> Optional[Text]
        if self.share_registry is None:
            return None
        return self.share_registry.source_key(
            src_fs, src_path, source_name=self.source_name
        )

    def plan(self, src_fs, dst_fs, src_root="/", dst_root="/"):
        # type: (FS, FS, Text, Text) -> None
        """Scan the source and journal every resource as ``pending``.

        Re-planning is additive: resources already journaled keep their
        state, so an interrupted plan can be extended without losing
        progress.
        """
        _src_root = abspath(normpath(src_root))
        _dst_root = abspath(normpath(dst_root))
        with self.journal.lock:
            for src_path in self.walker.files(src_fs, _src_root):
                dst_path = combine(_dst_root, frombase(_src_root, src_path))
                entry = self.journal.get(src_path)
                if entry is None:
                    info = src_fs.getinfo(src_path, namespaces=("details",))
                    src_hash = src_fs.hash(src_path, self.hash_algorithm)
                    entry = self.journal.upsert(
                        src_path,
                        dst_path,
                        src_hash=src_hash,
                        size=info.size,
                    )
                    self._emit("planned", entry)
                key = self._source_key(src_fs, src_path)
                if key is not None:
                    self.share_registry.claim(
                        self.journal.job_id,
                        key,
                        destination=dst_fs.getsyspath(dst_path)
                        if dst_fs.hassyspath(dst_path)
                        else normpath(dst_path),
                    )
            self.journal.save()

    def _record_conflict(self, src_path, dst_path, src_hash, dst_hash):
        # type: (Text, Text, Text, Text) -> None
        self.journal.transition(
            src_path,
            STATE_CONFLICT,
            error="destination exists with different contents",
        )
        with self.journal.lock:
            self.journal.upsert(
                src_path, dst_path, src_hash=src_hash, dst_hash=dst_hash
            )
            self.journal.save()
        self._emit("conflict", self.journal.get(src_path))

    def _preflight_destination(self, src_fs, dst_fs, entry):
        # type: (FS, FS, JournalEntry) -> Text
        """Check the destination before writing anything.

        Returns the journal state the entry should be in after the
        preflight. Raises `MigrationConflict` for same-name resources with
        different contents.
        """
        if not dst_fs.exists(entry.dst_path):
            return entry.state
        dst_hash = dst_fs.hash(entry.dst_path, self.hash_algorithm)
        if dst_hash == entry.src_hash:
            # Identical copy already present: do not touch the destination,
            # replaying must never re-write (and certainly not append).
            with self.journal.lock:
                self.journal.upsert(
                    entry.src_path, entry.dst_path, dst_hash=dst_hash
                )
                if entry.state == STATE_PENDING:
                    self.journal.upsert(
                        entry.src_path, entry.dst_path, state=STATE_COPIED
                    )
                if entry.state in (STATE_PENDING, STATE_COPIED):
                    self.journal.upsert(
                        entry.src_path, entry.dst_path, state=STATE_VERIFIED
                    )
                self.journal.save()
            refreshed = self.journal.get(entry.src_path)
            self._emit("verified", refreshed)
            return refreshed.state
        self._record_conflict(
            entry.src_path, entry.dst_path, entry.src_hash, dst_hash
        )
        conflict = MigrationConflict(
            entry.dst_path,
            src_hash=entry.src_hash,
            dst_hash=dst_hash,
            msg=(
                "destination '{path}' already exists with different contents "
                "than the source"
            ),
        )
        if self.conflict_policy == "raise":
            raise conflict
        return STATE_CONFLICT

    def _temp_path(self, entry):
        # type: (JournalEntry) -> Text
        """Deterministic side-car path used while copying ``entry``.

        The name is stable across replays so an interrupted copy is
        truncated and overwritten rather than accumulated alongside new
        temporary files.
        """
        return combine(
            dirname(entry.dst_path),
            ".{}.{}.part".format(basename(entry.dst_path), self.journal.job_id[:8]),
        )

    def _copy_one(self, src_fs, dst_fs, entry):
        # type: (FS, FS, JournalEntry) -> JournalEntry
        state = self._preflight_destination(src_fs, dst_fs, entry)
        if state in (STATE_VERIFIED, STATE_CONFLICT):
            return self.journal.get(entry.src_path)

        dst_fs.makedirs(dirname(entry.dst_path), recreate=True)
        tmp_path = self._temp_path(entry)
        # copy_file_internal opens the destination in truncating mode, so a
        # partial side-car left by an interrupted run is replaced rather
        # than appended to. The real destination path is only populated by
        # the final atomic move, which means anything already at the
        # destination is either identical (handled in the preflight) or a
        # genuine conflict.
        copy_file_internal(
            src_fs,
            entry.src_path,
            dst_fs,
            tmp_path,
            preserve_time=self.preserve_time,
            lock=True,
        )
        tmp_hash = dst_fs.hash(tmp_path, self.hash_algorithm)
        if tmp_hash != entry.src_hash:
            if dst_fs.exists(tmp_path):
                dst_fs.remove(tmp_path)
            self.journal.transition(
                entry.src_path,
                STATE_PENDING,
                error="checksum mismatch of temporary copy",
            )
            raise MigrationConflict(
                entry.dst_path,
                msg="temporary copy of '{path}' failed checksum verification",
            )
        dst_fs.move(tmp_path, entry.dst_path, overwrite=True)
        entry = self.journal.transition(entry.src_path, STATE_COPIED)
        self._emit("copied", entry)
        return entry

    def _verify_one(self, src_fs, dst_fs, entry):
        # type: (FS, FS, JournalEntry) -> JournalEntry
        dst_hash = dst_fs.hash(entry.dst_path, self.hash_algorithm)
        if dst_hash != entry.src_hash:
            # Roll back so the next run truncates and re-copies the file
            # instead of getting stuck in the copied state.
            self.journal.transition(
                entry.src_path,
                STATE_PENDING,
                error="checksum mismatch after copy",
            )
            raise MigrationConflict(
                entry.dst_path,
                msg="copied resource '{path}' failed checksum verification",
            )
        with self.journal.lock:
            self.journal.upsert(entry.src_path, entry.dst_path, dst_hash=dst_hash)
            entry = self.journal.transition(entry.src_path, STATE_VERIFIED)
        self._emit("verified", entry)
        key = self._source_key(src_fs, entry.src_path)
        if key is not None:
            self.share_registry.mark_verified(self.journal.job_id, key)
        return entry

    def _mark_deletable(self, src_fs, entry):
        # type: (FS, JournalEntry) -> JournalEntry
        if not self.delete_source:
            entry = self.journal.transition(entry.src_path, STATE_COMPLETED)
            self._emit("completed", entry)
            return entry
        key = self._source_key(src_fs, entry.src_path)
        if key is not None and not self.share_registry.can_delete(key):
            # Another task still depends on this source; stay verified and
            # wait for a later run.
            return entry
        entry = self.journal.transition(entry.src_path, STATE_DELETABLE)
        self._emit("deletable", entry)
        return entry

    def _delete_one(self, src_fs, dst_fs, entry):
        # type: (FS, FS, JournalEntry) -> JournalEntry
        key = self._source_key(src_fs, entry.src_path)
        if key is not None and not self.share_registry.can_delete(key):
            return entry
        # Final safety net: never remove the source unless a verified copy
        # is still present on the destination.
        if not dst_fs.exists(entry.dst_path):
            self.journal.transition(
                entry.src_path,
                STATE_PENDING,
                error="destination disappeared before source deletion",
            )
            raise MigrationConflict(
                entry.dst_path,
                msg="destination '{path}' is missing, source was kept",
            )
        if dst_fs.hash(entry.dst_path, self.hash_algorithm) != entry.src_hash:
            self.journal.transition(
                entry.src_path,
                STATE_PENDING,
                error="destination changed before source deletion",
            )
            raise MigrationConflict(
                entry.dst_path,
                msg="destination '{path}' no longer matches the source",
            )
        if src_fs.exists(entry.src_path):
            src_fs.remove(entry.src_path)
            remove_empty(src_fs, dirname(entry.src_path))
        if key is not None:
            self.share_registry.mark_deleted(key)
        entry = self.journal.transition(entry.src_path, STATE_COMPLETED)
        self._emit("completed", entry)
        return entry

    def run(self, src_root="/", dst_root="/", rescan=True):
        # type: (Text, Text, bool) -> MigrationResult
        """Execute (or resume) the migration and return a summary.

        Arguments:
            src_root (str): Directory migrated from on the source FS.
            dst_root (str): Directory migrated into on the destination FS.
            rescan (bool): If `True` (the default), re-scan the source for
                new resources before processing journaled ones.

        Raises:
            MigrationConflict: On a same-name / different-content
                destination when ``conflict_policy`` is ``"raise"``.
            MigrationIncomplete: If resources remain unfinished at the end
                of a ``"skip"`` run.
        """
        from .opener import manage_fs

        with manage_fs(self._src_fs, writeable=self.delete_source) as src_fs:
            with manage_fs(self._dst_fs, writeable=True, create=True) as dst_fs:
                if rescan:
                    self.plan(src_fs, dst_fs, src_root, dst_root)
                entries = self.journal.entries()
                for _key, entry in sorted(entries.items()):
                    if entry.state == STATE_COMPLETED:
                        continue
                    if entry.state == STATE_CONFLICT:
                        if self.conflict_policy == "raise":
                            raise MigrationConflict(
                                entry.dst_path,
                                msg=(
                                    "destination '{path}' already exists with "
                                    "different contents than the source"
                                ),
                            )
                        continue

                    if entry.state == STATE_PENDING:
                        if src_fs.exists(entry.src_path):
                            entry = self._copy_one(src_fs, dst_fs, entry)
                        else:
                            # The source was removed outside this run. Only
                            # continue if an identical copy already exists.
                            self._preflight_destination(src_fs, dst_fs, entry)
                            entry = self.journal.get(entry.src_path)
                            if entry.state == STATE_PENDING:
                                raise ResourceNotFound(entry.src_path)
                        if entry.state == STATE_CONFLICT:
                            if self.conflict_policy == "raise":
                                raise MigrationConflict(
                                    entry.dst_path,
                                    msg=(
                                        "destination '{path}' already exists with "
                                        "different contents than the source"
                                    ),
                                )
                            continue
                    if entry.state == STATE_COPIED:
                        entry = self._verify_one(src_fs, dst_fs, entry)
                    if entry.state == STATE_VERIFIED:
                        entry = self._mark_deletable(src_fs, entry)
                    if entry.state == STATE_DELETABLE:
                        entry = self._delete_one(src_fs, dst_fs, entry)

        result = self.journal.result()
        if result.conflicts or result.pending:
            raise MigrationIncomplete(
                journal_path=self.journal.path,
                pending=result.pending,
                conflicts=result.conflicts,
            )
        return result


def migrate_fs(
    src_fs,  # type: Union[FS, Text]
    dst_fs,  # type: Union[FS, Text]
    journal_path,  # type: Text
    src_root="/",  # type: Text
    dst_root="/",  # type: Text
    journal_fs=None,  # type: Optional[FS]
    walker=None,  # type: Optional[Walker]
    hash_algorithm="sha256",  # type: Text
    preserve_time=False,  # type: bool
    delete_source=True,  # type: bool
    share_registry=None,  # type: Optional[ShareRegistry]
    source_name=None,  # type: Optional[Text]
    conflict_policy="raise",  # type: Text
    on_event=None,  # type: Optional[_OnEvent]
):
    # type: (...) -> MigrationResult
    """Migrate resources from one filesystem to another, journaled.

    Re-opening an existing ``journal_path`` resumes the migration instead
    of starting over: completed resources are skipped, partial copies are
    replaced (never appended to), and conflicting destinations are
    reported rather than overwritten.

    Arguments:
        src_fs (FS or str): Source filesystem (instance or URL).
        dst_fs (FS or str): Destination filesystem (instance or URL).
        journal_path (str): Path to the JSON journal file, on local
            storage unless ``journal_fs`` is supplied.
        src_root (str): Directory to migrate from on the source FS.
        dst_root (str): Directory to migrate into on the destination FS.
        journal_fs (FS, optional): Filesystem holding the journal.
        walker (~fs.walk.Walker, optional): Walker selecting resources.
        hash_algorithm (str): Checksum algorithm used for verification.
        preserve_time (bool): Preserve modification times while copying.
        delete_source (bool): Remove source files after verified copies
            exist everywhere they are claimed.
        share_registry (ShareRegistry, optional): Shared-source registry
            coordinating deletion across tasks.
        source_name (str, optional): Source namespace for share registry
            keys on backends without system paths.
        conflict_policy (str): ``"raise"`` or ``"skip"``.
        on_event (callable, optional): Progress callback
            ``(event, entry)``.

    Returns:
        MigrationResult: Per-state counts once every resource is completed.

    Raises:
        MigrationConflict: If a destination already holds different
            contents under the same path.
        MigrationIncomplete: If the run ends with unresolved conflicts or
            resources waiting on another task.
    """
    journal = MigrationJournal(
        journal_path, journal_fs=journal_fs, hash_algorithm=hash_algorithm
    )
    migrator = Migrator(
        src_fs,
        dst_fs,
        journal,
        walker=walker,
        hash_algorithm=hash_algorithm,
        preserve_time=preserve_time,
        delete_source=delete_source,
        share_registry=share_registry,
        source_name=source_name,
        conflict_policy=conflict_policy,
        on_event=on_event,
    )
    return migrator.run(src_root=src_root, dst_root=dst_root)
