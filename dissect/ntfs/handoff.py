from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from dissect.ntfs.c_ntfs import (
    FILE_NAME_DOS,
    FILE_NUMBER_ROOT,
    FILE_RECORD_SEGMENT_IN_USE,
    c_ntfs,
)
from dissect.ntfs.exceptions import Error
from dissect.ntfs.util import segment_reference

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, MutableSet, Sequence

    from dissect.ntfs.mft import Mft, MftRecord
    from dissect.ntfs.ntfs import NTFS
    from dissect.ntfs.usnjrnl import UsnJrnl, UsnRecord

FILE_ATTRIBUTE_DIRECTORY = 0x10

USN_REASON_FILE_CREATE = 0x00000100
USN_REASON_FILE_DELETE = 0x00000200
USN_REASON_RENAME_OLD_NAME = 0x00001000
USN_REASON_RENAME_NEW_NAME = 0x00002000


@dataclass(frozen=True)
class EventIdentity:
    """The identity of a change journal event.

    The USN is the identity assigned by the filesystem. The file and parent references, reason and
    name are included so that the two records of a rename (which may share a USN) stay distinct,
    while exact copies from overlapping journal segments collapse into one event.
    """

    usn: int
    file_reference: int | bytes
    parent_reference: int | bytes
    reason: int
    name: str | None


@dataclass(frozen=True)
class SnapshotEntry:
    """The state of one file name in a volume snapshot.

    A snapshot entry is state, not an event. It records the name and parent reference that were
    current when the snapshot was taken, together with the last USN applied to the MFT record.
    """

    segment: int
    sequence: int
    name: str
    parent_segment: int
    parent_sequence: int
    usn: int
    is_directory: bool = False
    is_dos: bool = False

    @property
    def file_reference(self) -> int:
        return self.segment

    @property
    def reference(self) -> tuple[int, int]:
        return (self.segment, self.sequence)

    @property
    def parent_reference(self) -> tuple[int, int]:
        return (self.parent_segment, self.parent_sequence)


@dataclass(frozen=True)
class Gap:
    """An explicit range of USNs for which no journal evidence is available.

    The range is half-open: ``[start, end)``. ``end`` is ``None`` when the missing range extends
    beyond the last known journal record, so the true end is unknown.
    """

    start: int
    end: int | None = None
    kind: str = "journal"

    @property
    def length(self) -> int | None:
        return None if self.end is None else self.end - self.start

    def __contains__(self, usn: object) -> bool:
        if not isinstance(usn, int):
            return False

        return usn >= self.start and (self.end is None or usn < self.end)

    def __repr__(self) -> str:
        end = "?" if self.end is None else str(self.end)
        return f"<Gap [{self.start}, {end})>"


@dataclass(frozen=True)
class HandoffBoundary:
    """The boundary between snapshot state and subsequent journal events.

    Args:
        usn: The effective boundary. Events at or below this USN are snapshot state.
        snapshot_usn: The highest USN reflected in the snapshot.
        declared_usn: The boundary declared by the caller, if any.
        source: ``"snapshot"``, ``"explicit"`` or ``"snapshot-overrides-explicit"``.
    """

    usn: int
    snapshot_usn: int = 0
    declared_usn: int | None = None
    source: str = "snapshot"

    @property
    def consistent(self) -> bool:
        """Return whether the declared boundary agrees with the snapshot.

        A declared boundary below the snapshot's highest USN means the snapshot already contains
        changes the caller wanted to treat as subsequent events. The effective boundary is raised
        to the snapshot USN so those changes are not replayed, but the handoff reports the
        inconsistency instead of hiding it.
        """
        return self.declared_usn is None or self.declared_usn >= self.snapshot_usn


@dataclass
class Change:
    """A normalized, de-duplicated change journal event.

    ``path`` is only set when the snapshot and the events that came before it support a path. It is
    never filled in from the current MFT. If the available evidence is insufficient, ``path``
    stays ``None`` and the event is listed in :attr:`Handoff.path_gaps`.
    """

    usn: int
    timestamp_ns: int
    file_reference: int | bytes
    parent_reference: int | bytes
    sequence: int
    parent_sequence: int
    name: str | None
    reason: int
    is_directory: bool = False
    path: str | None = None
    migration: Migration | None = None
    major_version: int = 2
    source: Any = field(default=None, repr=False, compare=False)
    index: int = field(default=0, repr=False, compare=False)

    @property
    def identity(self) -> EventIdentity:
        """Return the event identity used for de-duplication."""
        return EventIdentity(
            usn=self.usn,
            file_reference=self.file_reference,
            parent_reference=self.parent_reference,
            reason=self.reason,
            name=self.name,
        )

    @property
    def filename(self) -> str | None:
        """Return the file name carried by the event."""
        return self.name

    @property
    def is_rename_old_name(self) -> bool:
        return bool(self.reason & USN_REASON_RENAME_OLD_NAME)

    @property
    def is_rename_new_name(self) -> bool:
        return bool(self.reason & USN_REASON_RENAME_NEW_NAME)

    @property
    def is_rename(self) -> bool:
        return self.is_rename_old_name or self.is_rename_new_name

    @property
    def is_create(self) -> bool:
        return bool(self.reason & USN_REASON_FILE_CREATE)

    @property
    def is_delete(self) -> bool:
        return bool(self.reason & USN_REASON_FILE_DELETE)

    @property
    def path_known(self) -> bool:
        return self.path is not None


@dataclass(frozen=True)
class Migration:
    """A path change reconstructed from rename records.

    ``from_path`` is only populated from actual evidence: a paired ``RENAME_OLD_NAME`` record, the
    snapshot, or a preceding event for the same file. It is never derived by walking the current
    directory tree. If that evidence is missing, ``from_path`` stays ``None`` and ``complete`` is
    ``False``.
    """

    identity: EventIdentity
    usn: int
    file_reference: int | bytes
    sequence: int
    from_path: str | None
    to_path: str | None
    old_name: str | None
    new_name: str | None
    complete: bool = False
    timestamp_ns: int = 0

    @property
    def path_known(self) -> bool:
        return self.from_path is not None and self.to_path is not None


@dataclass(frozen=True)
class HandoffReport:
    """A verifiable summary of a snapshot/journal handoff."""

    boundary_usn: int
    snapshot_usn: int
    boundary_consistent: bool
    snapshot_entries: int
    overlap_events: int
    subsequent_events: int
    duplicate_events: int
    gaps: tuple[Gap, ...]
    unresolved_renames: int
    path_gaps: int
    migrations: int
    migrations_incomplete: int

    @property
    def ok(self) -> bool:
        """Return whether the handoff boundary and replay inputs are internally consistent.

        Gaps and unresolved renames are allowed; they must be explicitly present in the report
        rather than silently filled in.
        """
        return self.boundary_consistent

    @property
    def verified(self) -> bool:
        """Alias for :attr:`ok`."""
        return self.ok


class Snapshot:
    """The file name state of a volume at a point in time.

    Args:
        entries: The snapshot entries to use.
    """

    def __init__(self, entries: Iterable[SnapshotEntry]):
        self.entries = tuple(entries)
        self._entries_by_ref: dict[tuple[int, int], list[SnapshotEntry]] = defaultdict(list)
        self._path_cache: dict[tuple[int, int], str | None] = {}

        for entry in self.entries:
            self._entries_by_ref[entry.reference].append(entry)

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self) -> Iterator[SnapshotEntry]:
        return iter(self.entries)

    def __repr__(self) -> str:
        return f"<Snapshot entries={len(self.entries)} boundary_usn={self.boundary_usn}>"

    @classmethod
    def from_mft(cls, mft: Mft, include_dos: bool = False) -> Snapshot:
        """Build a snapshot from all allocated MFT records.

        Args:
            mft: The MFT to read the snapshot state from.
            include_dos: Include DOS names even when a long name exists for the same record.
        """
        entries = []

        for record in mft.segments():
            entries.extend(snapshot_entries(record, include_dos=include_dos))

        return cls(entries)

    @classmethod
    def from_ntfs(cls, ntfs: NTFS, include_dos: bool = False) -> Snapshot:
        """Build a snapshot from the MFT of an :class:`~dissect.ntfs.ntfs.NTFS` instance."""
        if not ntfs.mft:
            raise Error("No MFT available")

        return cls.from_mft(ntfs.mft, include_dos=include_dos)

    @property
    def boundary_usn(self) -> int:
        """The highest USN reflected in the snapshot, or ``0`` if no entry has a USN."""
        return max((entry.usn for entry in self.entries), default=0)

    @property
    def paths(self) -> dict[tuple[int, int], str | None]:
        """Return the preferred path for every file reference in the snapshot."""
        return {key: self._path_for_key(key, set()) for key in self._entries_by_ref}

    def entries_for(self, segment: int, sequence: int | None = None) -> tuple[SnapshotEntry, ...]:
        """Return all snapshot entries for a file reference."""
        if sequence is None:
            return tuple(
                entry for key, entries in self._entries_by_ref.items() if key[0] == segment for entry in entries
            )

        return tuple(self._entries_by_ref.get((segment, sequence), ()))

    def path(self, segment: int, sequence: int | None = None) -> str | None:
        """Return the preferred snapshot path for a file reference.

        Args:
            segment: The segment number of the file reference.
            sequence: The sequence number of the file reference. If omitted, a unique segment
                match is used.
        """
        if sequence is None:
            candidates = [key for key in self._entries_by_ref if key[0] == segment]
            if len(candidates) != 1:
                return None

            key = candidates[0]
        else:
            key = (segment, sequence)

        return self._path_for_key(key, set())

    def _preferred(self, key: tuple[int, int]) -> SnapshotEntry | None:
        entries = self._entries_by_ref.get(key)
        if not entries:
            return None

        for entry in entries:
            if not entry.is_dos:
                return entry

        return entries[0]

    def _path_for_key(self, key: tuple[int, int], seen: set[tuple[int, int]]) -> str | None:
        if key in self._path_cache:
            return self._path_cache[key]

        if key in seen:
            self._path_cache[key] = None
            return None

        entry = self._preferred(key)
        if entry is None:
            self._path_cache[key] = None
            return None

        seen.add(key)

        if entry.segment == FILE_NUMBER_ROOT or entry.parent_reference == key:
            path = ""
        else:
            parent_path = self._path_for_key(entry.parent_reference, seen)
            path = join_path(parent_path, entry.name)

        self._path_cache[key] = path
        return path


class Handoff:
    """Combine a volume snapshot with a change journal.

    The snapshot is state at the boundary. Journal events at or below the boundary are overlap and
    are not replayed as subsequent events. Events above the boundary are de-duplicated by event
    identity, ordered by USN, and assigned paths only from snapshot state and preceding event
    evidence. Ranges without journal coverage remain explicit :class:`Gap` values.

    Args:
        snapshot: A :class:`Snapshot`, an MFT, an NTFS instance, or an iterable of
            :class:`SnapshotEntry`.
        journal: A :class:`~dissect.ntfs.usnjrnl.UsnJrnl`, a file-like object, a sequence of USN
            records, or a sequence of journal chunks. Overlapping chunks are allowed.
        boundary: The USN boundary of the snapshot. If omitted, the highest USN in the snapshot is
            used.
        snapshot_usn: Alias for ``boundary``.
        coverage: Optional explicit journal coverage ranges as ``(start, end)`` tuples. Normally
            inferred from the journal records.
    """

    def __init__(
        self,
        snapshot: Snapshot | Mft | NTFS | Iterable[SnapshotEntry] | None,
        journal: UsnJrnl | Iterable[UsnRecord] | Sequence[Any] | Any | None = None,
        *,
        boundary: int | None = None,
        snapshot_usn: int | None = None,
        coverage: Iterable[tuple[int, int]] | None = None,
    ):
        self.snapshot = _as_snapshot(snapshot)
        self.snapshot_boundary = self.snapshot.boundary_usn

        declared = boundary if boundary is not None else snapshot_usn
        if declared is None:
            boundary_usn = self.snapshot_boundary
            source = "snapshot"
        elif int(declared) < self.snapshot_boundary:
            boundary_usn = self.snapshot_boundary
            source = "snapshot-overrides-explicit"
        else:
            boundary_usn = int(declared)
            source = "explicit"

        self.boundary = HandoffBoundary(
            boundary_usn,
            self.snapshot_boundary,
            None if declared is None else int(declared),
            source,
        )

        records, inferred_coverage = _collect_journal(journal)
        if coverage is not None:
            inferred_coverage = [(int(start), int(end)) for start, end in coverage]

        changes, duplicates = normalize_records(records)
        self.duplicates = tuple(duplicates)
        self.overlap = tuple(change for change in changes if change.usn <= self.boundary.usn)

        subsequent = [change for change in changes if change.usn > self.boundary.usn]

        self.coverage = tuple(merge_ranges(inferred_coverage))
        self.gaps = tuple(coverage_gaps(self.boundary.usn, self.coverage))
        self.changes = tuple(resolve_paths(self.snapshot, subsequent, self.gaps))
        self.events = self.changes

        self.unresolved = tuple(change for change in self.changes if change.is_rename and change.migration is None)
        self.unresolved_renames = tuple(
            change
            for change in self.changes
            if change.is_rename and (change.migration is None or not change.migration.complete)
        )
        self.path_gaps = tuple(change for change in self.changes if change.path is None)
        self.migrations = tuple(change.migration for change in self.changes if change.migration is not None)

        self._emitted_events: set[EventIdentity] = set()
        self._emitted_migrations: set[EventIdentity] = set()

    def __repr__(self) -> str:
        return (
            f"<Handoff boundary={self.boundary.usn} snapshot={len(self.snapshot.entries)} "
            f"changes={len(self.changes)} gaps={len(self.gaps)}>"
        )

    @classmethod
    def from_ntfs(
        cls,
        ntfs: NTFS,
        *,
        boundary: int | None = None,
        snapshot_usn: int | None = None,
    ) -> Handoff:
        """Build a handoff from an :class:`~dissect.ntfs.ntfs.NTFS` instance."""
        return cls(ntfs, ntfs.usnjrnl, boundary=boundary, snapshot_usn=snapshot_usn)

    @property
    def boundary_usn(self) -> int:
        """Return the USN of the snapshot/journal boundary."""
        return self.boundary.usn

    @property
    def pending(self) -> tuple[Change, ...]:
        """Return the changes that have not been emitted by :meth:`replay` yet."""
        return tuple(change for change in self.changes if change.identity not in self._emitted_events)

    @property
    def pending_migrations(self) -> tuple[Migration, ...]:
        """Return the migrations that have not been emitted by :meth:`replay_migrations` yet."""
        return tuple(
            change.migration
            for change in self.changes
            if change.migration is not None and change.migration.identity not in self._emitted_migrations
        )

    def paths_at(self, usn: int) -> dict[tuple[int | bytes, int], str | None]:
        """Return the preferred path state after applying everything up to and including ``usn``.

        The result is derived from the snapshot and event evidence only, never from the current
        MFT. A value of ``None`` means the path is unknown and must remain an explicit gap.
        """
        names, _ = _names_from_snapshot(self.snapshot)

        for change in self.changes:
            if change.usn > usn:
                break

            _apply_change_names(names, change)

        return {key: _resolve_names_path(names, key, set()) for key in names}

    def verify(self) -> HandoffReport:
        """Return a verifiable summary of the handoff."""
        return HandoffReport(
            boundary_usn=self.boundary.usn,
            snapshot_usn=self.snapshot_boundary,
            boundary_consistent=self.boundary.consistent,
            snapshot_entries=len(self.snapshot.entries),
            overlap_events=len(self.overlap),
            subsequent_events=len(self.changes),
            duplicate_events=len(self.duplicates),
            gaps=self.gaps,
            unresolved_renames=len(self.unresolved_renames),
            path_gaps=len(self.path_gaps),
            migrations=len(self.migrations),
            migrations_incomplete=sum(1 for migration in self.migrations if not migration.complete),
        )

    def replay(self, emitted: MutableSet[EventIdentity] | None = None) -> Iterator[Change]:
        """Yield subsequent changes exactly once.

        Args:
            emitted: An optional persistent set of already emitted event identities. If omitted,
                this handoff's internal set is used, so replaying the same handoff twice does not
                emit the same event again.
        """
        emitted = self._emitted_events if emitted is None else emitted

        for change in self.changes:
            identity = change.identity
            if identity in emitted:
                continue

            emitted.add(identity)
            yield change

    def replay_migrations(self, emitted: MutableSet[EventIdentity] | None = None) -> Iterator[Migration]:
        """Yield migrations exactly once.

        Args:
            emitted: An optional persistent set of already emitted migration identities. If
                omitted, this handoff's internal set is used, so replaying the same handoff or
                journal twice does not re-emit a migration.
        """
        emitted = self._emitted_migrations if emitted is None else emitted

        for change in self.changes:
            migration = change.migration
            if migration is None or migration.identity in emitted:
                continue

            emitted.add(migration.identity)
            yield migration

    def replay_all(
        self,
        emitted_events: MutableSet[EventIdentity] | None = None,
        emitted_migrations: MutableSet[EventIdentity] | None = None,
    ) -> Iterator[Change | Migration]:
        """Yield events and their migrations exactly once, interleaved in USN order."""
        emitted_events = self._emitted_events if emitted_events is None else emitted_events
        emitted_migrations = self._emitted_migrations if emitted_migrations is None else emitted_migrations

        for change in self.changes:
            identity = change.identity
            if identity not in emitted_events:
                emitted_events.add(identity)
                yield change

            migration = change.migration
            if migration is not None and migration.identity not in emitted_migrations:
                emitted_migrations.add(migration.identity)
                yield migration

    def reset_replay(self) -> None:
        """Forget which events and migrations were emitted by this handoff."""
        self._emitted_events.clear()
        self._emitted_migrations.clear()


def merge_snapshot_and_journal(
    snapshot: Snapshot | Mft | NTFS | Iterable[SnapshotEntry] | None,
    journal: UsnJrnl | Iterable[UsnRecord] | Sequence[Any] | Any | None = None,
    *,
    boundary: int | None = None,
    snapshot_usn: int | None = None,
    coverage: Iterable[tuple[int, int]] | None = None,
) -> Handoff:
    """Convenience wrapper for :class:`Handoff`."""
    return Handoff(
        snapshot,
        journal,
        boundary=boundary,
        snapshot_usn=snapshot_usn,
        coverage=coverage,
    )


apply_journal = merge_snapshot_and_journal
build_handoff = merge_snapshot_and_journal


def snapshot_entries(record: MftRecord, include_dos: bool = False) -> Iterator[SnapshotEntry]:
    """Yield snapshot entries for a single MFT record.

    Args:
        record: The MFT record to read.
        include_dos: Include DOS names even when a long name exists for the same record.
    """
    try:
        if not record.header.Flags & FILE_RECORD_SEGMENT_IN_USE:
            return

        if segment_reference(record.header.BaseFileRecordSegment) != 0:
            return

        usn = record.usn
        if usn is None:
            return

        entries = [
            entry
            for attr in record.attributes[c_ntfs.ATTRIBUTE_TYPE_CODE.FILE_NAME]
            if (entry := _entry_from_attribute(record, attr, usn)) is not None
        ]

        if not include_dos and any(not entry.is_dos for entry in entries):
            entries = [entry for entry in entries if not entry.is_dos]

        yield from entries
    except (AttributeError, Error, EOFError, ValueError):
        return


def _entry_from_attribute(record: MftRecord, attr: Any, usn: int) -> SnapshotEntry | None:
    try:
        parent = attr.attr.ParentDirectory
        return SnapshotEntry(
            segment=record.segment,
            sequence=record.header.SequenceNumber,
            name=attr.file_name,
            parent_segment=segment_reference(parent),
            parent_sequence=parent.SequenceNumber,
            usn=usn,
            is_directory=record.is_dir(),
            is_dos=bool(attr.flags & FILE_NAME_DOS),
        )
    except (AttributeError, Error, ValueError):
        return None


def normalize_records(records: Sequence[UsnRecord]) -> tuple[list[Change], list[Change]]:
    """Normalize USN records and de-duplicate them by event identity."""
    changes = []
    duplicates = []
    seen: dict[EventIdentity, Change] = {}

    for index, record in enumerate(records):
        change = _change_from_record(record, index)
        if change is None:
            continue

        existing = seen.get(change.identity)
        if existing is not None:
            duplicates.append(change)
            continue

        seen[change.identity] = change
        changes.append(change)

    changes.sort(key=lambda change: (change.usn, change.index))
    return changes, duplicates


def resolve_paths(snapshot: Snapshot, changes: list[Change], gaps: Sequence[Gap] = ()) -> list[Change]:
    """Assign evidence-based paths and migrations to changes in USN order."""
    names, state_usn = _names_from_snapshot(snapshot)
    pairs, _ = pair_renames(changes, gaps)

    for change in changes:
        key = (change.file_reference, change.sequence)
        parent_key = (change.parent_reference, change.parent_sequence)
        parent_path = _resolve_names_path(names, parent_key, set())
        direct_path = join_path(parent_path, change.name)

        if change.is_rename_old_name and not change.is_rename_new_name:
            change.path = direct_path if direct_path is not None else _resolve_names_path(names, key, set())
            _remove_name(names, key, parent_key, change.name)
            continue

        if change.is_rename_new_name:
            old = pairs.get(id(change))
            if old is not None:
                from_path = old.path
            elif not gap_between(gaps, state_usn.get(key, 0), change.usn):
                from_path = _resolve_names_path(names, key, set())
            else:
                from_path = None

            change.path = direct_path
            if old is None:
                # An unpaired new name is a real rename, so any previous name for this file must
                # not survive. Keep the new name even though the old path itself is unknown.
                names[key] = []

            _add_name(names, key, parent_key, change.name, change.is_directory)
            state_usn[key] = change.usn
            change.migration = Migration(
                identity=change.identity,
                usn=change.usn,
                file_reference=change.file_reference,
                sequence=change.sequence,
                from_path=from_path,
                to_path=direct_path,
                old_name=old.name if old is not None else None,
                new_name=change.name,
                complete=from_path is not None and direct_path is not None,
                timestamp_ns=change.timestamp_ns,
            )
            continue

        if change.is_create:
            change.path = direct_path
            _add_name(names, key, parent_key, change.name, change.is_directory)
            state_usn[key] = change.usn
            continue

        if change.is_delete:
            change.path = direct_path if direct_path is not None else _resolve_names_path(names, key, set())
            _remove_name(names, key, parent_key, change.name, all_names=change.name is None)
            continue

        change.path = direct_path if direct_path is not None else _resolve_names_path(names, key, set())

    return changes


def pair_renames(changes: Sequence[Change], gaps: Sequence[Gap] = ()) -> tuple[dict[int, Change], list[Change]]:
    """Pair ``RENAME_OLD_NAME`` and ``RENAME_NEW_NAME`` records using journal evidence only.

    A new-name record is only paired with an old-name record for the same file reference when no
    delete or missing journal range sits between them. Returns a mapping from the id of the
    new-name change to its old-name change, plus the unpaired rename records.
    """
    pending: dict[tuple[int | bytes, int], list[Change]] = defaultdict(list)
    last_delete: dict[tuple[int | bytes, int], int] = {}
    pairs: dict[int, Change] = {}
    unpaired: list[Change] = []

    for change in changes:
        key = (change.file_reference, change.sequence)

        if change.is_delete:
            last_delete[key] = change.index

        if change.is_rename_old_name and not change.is_rename_new_name:
            pending[key].append(change)
            continue

        if not change.is_rename_new_name:
            continue

        old = None
        candidates = pending.get(key, [])
        while candidates:
            candidate = candidates[-1]
            if gap_between(gaps, candidate.usn, change.usn) or last_delete.get(key, -1) > candidate.index:
                break

            candidates.pop()
            old = candidate
            break

        if old is None:
            unpaired.append(change)
        else:
            pairs[id(change)] = old

    for olds in pending.values():
        unpaired.extend(olds)

    return pairs, unpaired


def join_path(parent_path: str | None, name: str | None) -> str | None:
    """Join a parent path and name, returning ``None`` if either side is unknown."""
    if parent_path is None or name is None:
        return None

    if not parent_path:
        return name

    return f"{parent_path}\\{name}"


def merge_ranges(ranges: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping or adjacent half-open ranges."""
    merged = []

    for start, end in sorted(ranges):
        if end <= start:
            continue

        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    return merged


def coverage_gaps(boundary: int, coverage: Sequence[tuple[int, int]]) -> list[Gap]:
    """Return the explicit gaps between the boundary and the merged journal coverage."""
    if not coverage:
        return [Gap(boundary, None)] if boundary > 0 else []

    gaps = []
    cursor = boundary

    for start, end in coverage:
        if end <= boundary:
            continue

        start = max(start, boundary)
        if start > cursor:
            gaps.append(Gap(cursor, start))

        cursor = max(cursor, end)

    return gaps


def gap_between(gaps: Sequence[Gap], start: int, end: int) -> bool:
    """Return whether a gap overlaps the open interval ``(start, end)``."""
    for gap in gaps:
        gap_end = gap.end if gap.end is not None else end
        if gap.start < end and gap_end > start:
            return True

    return False


def _as_snapshot(snapshot: Snapshot | Mft | NTFS | Iterable[SnapshotEntry] | None) -> Snapshot:
    if isinstance(snapshot, Snapshot):
        return snapshot

    if snapshot is None:
        return Snapshot([])

    if hasattr(snapshot, "segments"):
        return Snapshot.from_mft(snapshot)

    if hasattr(snapshot, "mft"):
        return Snapshot.from_ntfs(snapshot)

    return Snapshot(
        entry if isinstance(entry, SnapshotEntry) else SnapshotEntry(**entry) if isinstance(entry, dict) else entry
        for entry in snapshot
    )


def _collect_journal(journal: Any) -> tuple[list[Any], list[tuple[int, int]]]:
    if journal is None:
        return [], []

    if isinstance(journal, Change):
        return [journal], [(journal.usn, journal.usn + 1)]

    if isinstance(journal, dict):
        return [journal], _coverage_for([journal])

    if hasattr(journal, "read") and not hasattr(journal, "records"):
        from dissect.ntfs.usnjrnl import UsnJrnl

        journal = UsnJrnl(journal)

    if hasattr(journal, "records"):
        records = list(journal.records())
        return records, _coverage_for(records)

    if isinstance(journal, (list, tuple)):
        if not journal:
            return [], []

        if all(_is_usn_record(item) for item in journal):
            records = list(journal)
            return records, _coverage_for(records)

        records = []
        coverage = []
        for part in journal:
            part_records, part_coverage = _collect_journal(part)
            records.extend(part_records)
            coverage.extend(part_coverage)

        return records, coverage

    records = list(journal)
    return records, _coverage_for(records)


def _is_usn_record(value: Any) -> bool:
    return isinstance(value, (Change, dict)) or (hasattr(value, "record") and hasattr(value, "header"))


def _coverage_for(records: Sequence[UsnRecord | dict[str, Any]]) -> list[tuple[int, int]]:
    usns = []
    for record in records:
        if isinstance(record, dict):
            usn = record.get("usn", record.get("Usn"))
            if usn is not None:
                usns.append(int(usn))
        elif getattr(record, "record", None) is not None:
            usns.append(int(record.record.Usn))

    if not usns:
        return []

    return [(min(usns), max(usns) + 1)]


def _change_from_record(record: UsnRecord | Change | dict[str, Any], index: int) -> Change | None:
    if isinstance(record, Change):
        record.index = index
        return record

    if isinstance(record, dict):
        return _change_from_mapping(record, index)

    raw = getattr(record, "record", None)
    if raw is None:
        return None

    major_version = int(getattr(record.header, "MajorVersion", getattr(raw, "MajorVersion", 2)))
    file_reference = getattr(record, "file_reference", None)
    parent_reference = getattr(record, "parent_reference", None)

    if file_reference is None:
        file_reference = _record_reference(raw.FileReferenceNumber)

    if parent_reference is None:
        parent_reference = _record_reference(raw.ParentFileReferenceNumber)

    name = getattr(record, "filename", None)
    reason = int(getattr(record, "reason", getattr(raw, "Reason", 0) or 0))
    attributes = int(getattr(raw, "FileAttributes", 0) or 0)

    return Change(
        usn=int(getattr(record, "usn", getattr(raw, "Usn", 0) or 0)),
        timestamp_ns=int(getattr(record, "timestamp_ns", 0) or 0),
        file_reference=file_reference,
        parent_reference=parent_reference,
        sequence=int(getattr(record, "sequence_number", 0) or 0),
        parent_sequence=int(getattr(record, "parent_sequence_number", 0) or 0),
        name=name,
        reason=reason,
        is_directory=bool(attributes & FILE_ATTRIBUTE_DIRECTORY),
        major_version=major_version,
        source=record,
        index=index,
    )


def _change_from_mapping(record: dict[str, Any], index: int) -> Change | None:
    usn = record.get("usn", record.get("Usn"))
    if usn is None:
        return None

    return Change(
        usn=int(usn),
        timestamp_ns=int(record.get("timestamp_ns", 0) or 0),
        file_reference=record.get("file_reference", record.get("FileReferenceNumber", 0)),
        parent_reference=record.get("parent_reference", record.get("ParentFileReferenceNumber", 0)),
        sequence=int(record.get("sequence", 0) or 0),
        parent_sequence=int(record.get("parent_sequence", 0) or 0),
        name=record.get("name", record.get("filename")),
        reason=int(record.get("reason", record.get("Reason", 0)) or 0),
        is_directory=bool(record.get("is_directory", False)),
        source=record,
        index=index,
    )


def _record_reference(reference: Any) -> int | bytes:
    if hasattr(reference, "SegmentNumberLowPart"):
        return segment_reference(reference)

    return bytes(reference.Identifier)


def _names_from_snapshot(
    snapshot: Snapshot,
) -> tuple[dict[tuple[int | bytes, int], list[tuple[Any, str, bool]]], dict[tuple[int | bytes, int], int]]:
    names: dict[tuple[int | bytes, int], list[tuple[Any, str, bool]]] = defaultdict(list)
    state_usn: dict[tuple[int | bytes, int], int] = {}

    for entry in snapshot.entries:
        names[entry.reference].append((entry.parent_reference, entry.name, entry.is_dos))
        state_usn[entry.reference] = max(state_usn.get(entry.reference, 0), entry.usn)

    return names, state_usn


def _resolve_names_path(
    names: dict[tuple[int | bytes, int], list[tuple[Any, str, bool]]],
    key: tuple[int | bytes, int],
    seen: set[tuple[int | bytes, int]],
) -> str | None:
    if key in seen:
        return None

    entries = names.get(key)
    if not entries:
        return None

    entry = next((entry for entry in entries if not entry[2]), entries[0])
    parent_key, name, _ = entry

    if key[0] == FILE_NUMBER_ROOT or parent_key == key:
        return ""

    seen.add(key)
    return join_path(_resolve_names_path(names, parent_key, seen), name)


def _state_path(
    names: dict[tuple[int | bytes, int], list[tuple[Any, str, bool]]],
    key: tuple[int | bytes, int],
    gaps: Sequence[Gap],
    usn: int,
) -> str | None:
    """Return the known state path for a key, unless a gap prevents trusting snapshot state."""
    if any(usn in gap for gap in gaps):
        return None

    return _resolve_names_path(names, key, set())


def _add_name(
    names: dict[tuple[int | bytes, int], list[tuple[Any, str, bool]]],
    key: tuple[int | bytes, int],
    parent_key: tuple[int | bytes, int],
    name: str | None,
    is_directory: bool = False,
) -> None:
    if name is None:
        return

    entries = names.setdefault(key, [])
    if not any(entry_name == name and entry_parent == parent_key for entry_parent, entry_name, _ in entries):
        entries.append((parent_key, name, False))


def _remove_name(
    names: dict[tuple[int | bytes, int], list[tuple[Any, str, bool]]],
    key: tuple[int | bytes, int],
    parent_key: tuple[int | bytes, int],
    name: str | None,
    all_names: bool = False,
) -> None:
    if key not in names:
        return

    if all_names:
        names.pop(key, None)
        return

    names[key] = [entry for entry in names[key] if not (entry[0] == parent_key and (name is None or entry[1] == name))]


def _apply_change_names(
    names: dict[tuple[int | bytes, int], list[tuple[Any, str, bool]]],
    change: Change,
) -> None:
    key = (change.file_reference, change.sequence)
    parent_key = (change.parent_reference, change.parent_sequence)

    if change.is_rename_old_name and not change.is_rename_new_name:
        _remove_name(names, key, parent_key, change.name)
    elif change.is_rename_new_name:
        names[key] = []
        _add_name(names, key, parent_key, change.name, change.is_directory)
    elif change.is_create:
        _add_name(names, key, parent_key, change.name, change.is_directory)
    elif change.is_delete:
        _remove_name(names, key, parent_key, change.name, all_names=change.name is None)
    elif change.name is not None:
        _add_name(names, key, parent_key, change.name, change.is_directory)
