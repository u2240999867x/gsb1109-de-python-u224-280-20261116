"""Incremental mailbox import with resumable, UIDVALIDITY-aware state.

The importer in this module is deliberately conservative:

* the state belongs to one folder and one UIDVALIDITY epoch;
* a changed UIDVALIDITY starts a new epoch, so old UIDs never hide new messages;
* a message is confirmed only after the download and the optional archive/callback
  succeeded, and the cursor never passes an unconfirmed UID;
* a successful empty search is a complete scan, while a connection/protocol
  failure leaves coverage unknown.

Old epochs are kept in the state, and :class:`FileSystemArchive` namespaces files
by folder and UIDVALIDITY, so an archive written before a UIDVALIDITY change keeps
its original source metadata.
"""
import datetime
import json
import os
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union
from urllib.parse import quote

from .errors import ImapToolsError
from .message import MailMessage
from .query import A, U

STATE_VERSION = 1


class ImportStateError(ImapToolsError):
    """Incremental import state is invalid or can not be read/written."""


class CoverageStatus(str, Enum):
    """How much of the mailbox a scan has actually covered.

    COMPLETE - search succeeded and every UID in the searched interval is either
        confirmed, absent, or already confirmed in a previous run.
    INCOMPLETE - search succeeded, but a message could not be downloaded or
        stored, so the cursor stopped before it.
    UNKNOWN - the folder status or the search itself failed.  No coverage claim
        can be made for this attempt.
    """

    COMPLETE = 'complete'
    INCOMPLETE = 'incomplete'
    UNKNOWN = 'unknown'

    # Friendly aliases for code that describes the state by its effect.
    PARTIAL = 'incomplete'
    ERROR = 'unknown'


@dataclass
class ImportCoverage:
    """Coverage conclusion of one import attempt."""

    status: CoverageStatus = CoverageStatus.UNKNOWN
    through_uid: int = 0
    uidnext: Optional[int] = None
    searched: bool = False
    matched: int = 0
    error: Optional[str] = None

    @property
    def complete(self) -> bool:
        return self.status == CoverageStatus.COMPLETE

    @property
    def incomplete(self) -> bool:
        return self.status == CoverageStatus.INCOMPLETE

    @property
    def unknown(self) -> bool:
        return self.status == CoverageStatus.UNKNOWN

    def to_dict(self) -> dict:
        return {
            'status': self.status.value,
            'through_uid': int(self.through_uid),
            'uidnext': self.uidnext,
            'searched': bool(self.searched),
            'matched': int(self.matched),
            'error': self.error,
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> 'ImportCoverage':
        data = data or {}
        status = _coverage_status(data.get('status'))
        uidnext = data.get('uidnext')
        return cls(
            status=status,
            through_uid=_non_negative_int(data.get('through_uid'), 'through_uid'),
            uidnext=_non_negative_int(uidnext, 'uidnext') if uidnext is not None else None,
            searched=bool(data.get('searched')),
            matched=_non_negative_int(data.get('matched'), 'matched'),
            error=data.get('error'),
        )


@dataclass(frozen=True)
class MessageSource:
    """Provenance of one imported message.

    The values describe the mailbox epoch the message was downloaded from.  They
    are written to the archive at download time and are never rewritten when the
    mailbox later changes UIDVALIDITY.
    """

    folder: str
    uidvalidity: int
    uid: int

    def to_dict(self) -> dict:
        return {
            'folder': self.folder,
            'uidvalidity': int(self.uidvalidity),
            'uid': int(self.uid),
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'MessageSource':
        return cls(
            folder=str(data['folder']),
            uidvalidity=int(data['uidvalidity']),
            uid=int(data['uid']),
        )


@dataclass
class ImportResult:
    """Outcome of an incremental import attempt."""

    folder: str
    uidvalidity: int
    coverage: ImportCoverage
    fetched: int = 0
    confirmed: int = 0
    skipped: int = 0
    archived: int = 0
    failed_uid: Optional[int] = None
    errors: List[str] = field(default_factory=list)
    uidvalidity_changed: bool = False
    started_from_uid: int = 0
    cursor: int = 0

    @property
    def status(self) -> CoverageStatus:
        return self.coverage.status

    @property
    def complete(self) -> bool:
        return self.coverage.complete

    @property
    def coverage_complete(self) -> bool:
        return self.coverage.complete

    @property
    def error(self) -> Optional[str]:
        return self.coverage.error


class FileSystemArchive:
    """Store messages and their source metadata below a directory.

    Layout::

        <root>/<quoted folder>/<uidvalidity>/<uid>.eml
        <root>/<quoted folder>/<uidvalidity>/<uid>.json

    UIDVALIDITY is part of the path on purpose.  When the server changes it, new
    messages get a new directory and older files keep their original source
    values instead of being overwritten or re-labelled.
    """

    def __init__(self, root: Union[str, 'os.PathLike[str]']) -> None:
        self.root = Path(root)

    def path_for(self, source: MessageSource) -> Tuple[Path, Path]:
        folder_part = quote(source.folder, safe='')
        directory = self.root / folder_part / str(int(source.uidvalidity))
        return directory / f'{int(source.uid):010d}.eml', directory / f'{int(source.uid):010d}.json'

    def store(self, message: MailMessage, source: MessageSource) -> Path:
        eml_path, meta_path = self.path_for(source)
        eml_path.parent.mkdir(parents=True, exist_ok=True)

        raw = message.raw if message.raw else bytes(message.obj)
        meta = {
            'source': source.to_dict(),
            'imported_at': _utc_now(),
            'message': {
                'uid': message.uid,
                'message_id': _first_header(message, 'message-id'),
                'subject': message.subject,
                'date': message.date_str,
                'flags': list(message.flags),
                'size': len(raw),
            },
        }
        _atomic_write(eml_path, raw)
        _atomic_write(meta_path, json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8'))
        return eml_path


@dataclass
class FolderImportState:
    """Import state of one folder, including retired UIDVALIDITY epochs."""

    folder: str
    uidvalidity: Optional[int] = None
    cursor: int = 0
    scanned_through: int = 0
    confirmed: List[List[int]] = field(default_factory=list)
    coverage: ImportCoverage = field(default_factory=ImportCoverage)
    epochs: List[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.folder = str(self.folder)
        if self.uidvalidity is not None:
            self.uidvalidity = _non_negative_int(self.uidvalidity, 'uidvalidity')
        self.cursor = _non_negative_int(self.cursor, 'cursor')
        self.scanned_through = _non_negative_int(self.scanned_through, 'scanned_through')
        self.confirmed = _normalize_ranges(self.confirmed)
        if not isinstance(self.coverage, ImportCoverage):
            self.coverage = ImportCoverage.from_dict(self.coverage)
        self.epochs = [_normalize_epoch(epoch) for epoch in self.epochs]

    @property
    def confirmed_ranges(self) -> List[List[int]]:
        return self.confirmed

    @property
    def confirmed_uids(self) -> List[int]:
        """Flat list of confirmed UIDs, for diagnostics and small mailboxes."""
        return [uid for start, end in self.confirmed for uid in range(start, end + 1)]

    @property
    def last_uid(self) -> int:
        return self.cursor

    @property
    def next_uid(self) -> int:
        return self.cursor + 1

    def confirm(self, uid: int) -> None:
        """Record a successfully downloaded UID and merge adjacent ranges."""
        uid = int(uid)
        self.confirmed = _normalize_ranges(self.confirmed + [[uid, uid]])

    def is_confirmed(self, uid: int) -> bool:
        uid = int(uid)
        return any(start <= uid <= end for start, end in self.confirmed)

    def activate(self, uidvalidity: int) -> bool:
        """Select *uidvalidity*, retiring the previous epoch when it changes.

        :return: True when an existing epoch was retired.
        """
        uidvalidity = int(uidvalidity)
        if self.uidvalidity is None:
            self.uidvalidity = uidvalidity
            return False
        if int(self.uidvalidity) == uidvalidity:
            return False
        snapshot = self._epoch_snapshot()
        # A retry can observe the same transition twice when the previous state
        # write failed.  Merge instead of appending a duplicate epoch.
        if self.epochs and self.epochs[-1].get('uidvalidity') == snapshot['uidvalidity']:
            previous = self.epochs[-1]
            previous['cursor'] = max(previous.get('cursor', 0), snapshot['cursor'])
            previous['scanned_through'] = max(previous.get('scanned_through', 0), snapshot['scanned_through'])
            previous['confirmed'] = _normalize_ranges(
                list(previous.get('confirmed') or []) + list(snapshot['confirmed']))
            previous['coverage'] = snapshot['coverage']
            previous['retired_at'] = snapshot['retired_at']
        else:
            self.epochs.append(snapshot)
        self.uidvalidity = uidvalidity
        self.cursor = 0
        self.scanned_through = 0
        self.confirmed = []
        self.coverage = ImportCoverage(uidnext=None)
        return True

    def _epoch_snapshot(self) -> dict:
        return {
            'uidvalidity': int(self.uidvalidity) if self.uidvalidity is not None else None,
            'cursor': int(self.cursor),
            'scanned_through': int(self.scanned_through),
            'confirmed': [list(item) for item in self.confirmed],
            'coverage': self.coverage.to_dict(),
            'retired_at': _utc_now(),
        }

    def to_dict(self) -> dict:
        return {
            'folder': self.folder,
            'uidvalidity': self.uidvalidity,
            'cursor': int(self.cursor),
            'scanned_through': int(self.scanned_through),
            'confirmed': [list(item) for item in self.confirmed],
            'coverage': self.coverage.to_dict(),
            'epochs': list(self.epochs),
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'FolderImportState':
        if 'folder' not in data:
            raise ImportStateError('Import state folder entry has no folder name')
        return cls(
            folder=data['folder'],
            uidvalidity=data.get('uidvalidity'),
            cursor=data.get('cursor') or 0,
            scanned_through=data.get('scanned_through') or 0,
            confirmed=data.get('confirmed') or [],
            coverage=ImportCoverage.from_dict(data.get('coverage')),
            epochs=list(data.get('epochs') or []),
        )


class MailboxImportState:
    """Persistent incremental import state, keyed by folder.

    A state file can be shared by several folders.  Each folder has its own
    cursor, confirmed ranges and retired UIDVALIDITY epochs, so switching folders
    never reuses another folder's UIDs.
    """

    def __init__(self, path: Optional[Union[str, 'os.PathLike[str]']] = None) -> None:
        self.path = Path(path) if path is not None else None
        self.folders: Dict[str, FolderImportState] = {}
        self._active_folder: Optional[str] = None

    def __getitem__(self, folder: str) -> FolderImportState:
        return self.for_folder(folder)

    def __contains__(self, folder: str) -> bool:
        return folder in self.folders

    def __len__(self) -> int:
        return len(self.folders)

    @property
    def folder(self) -> Optional[str]:
        if self._active_folder is not None:
            return self._active_folder
        if len(self.folders) == 1:
            return next(iter(self.folders))
        return None

    @property
    def uidvalidity(self) -> Optional[int]:
        state = self._active_state()
        return state.uidvalidity if state else None

    @property
    def cursor(self) -> int:
        state = self._active_state()
        return state.cursor if state else 0

    @property
    def confirmed(self) -> List[List[int]]:
        state = self._active_state()
        return state.confirmed if state else []

    @property
    def coverage(self) -> Optional[ImportCoverage]:
        state = self._active_state()
        return state.coverage if state else None

    def _active_state(self) -> Optional[FolderImportState]:
        folder = self.folder
        return self.folders.get(folder) if folder is not None else None

    def for_folder(self, folder: str) -> FolderImportState:
        """Get (or create) mutable state for *folder*."""
        folder = str(folder)
        state = self.folders.get(folder)
        if state is None:
            state = FolderImportState(folder=folder)
            self.folders[folder] = state
        self._active_folder = folder
        return state

    def get(self, folder: str) -> Optional[FolderImportState]:
        return self.folders.get(str(folder))

    def to_dict(self) -> dict:
        return {
            'version': STATE_VERSION,
            'folders': {
                name: self.folders[name].to_dict()
                for name in sorted(self.folders)
            },
        }

    @classmethod
    def from_dict(cls, data: dict, path: Optional[Union[str, 'os.PathLike[str]']] = None) -> 'MailboxImportState':
        if not isinstance(data, dict):
            raise ImportStateError('Import state must be an object')
        version = data.get('version', STATE_VERSION)
        if version != STATE_VERSION:
            raise ImportStateError(f'Unsupported import state version: {version}')
        state = cls(path=path)
        folders = data.get('folders') or {}
        if not isinstance(folders, dict):
            raise ImportStateError('Import state "folders" must be an object')
        for name, item in folders.items():
            if not isinstance(item, dict):
                raise ImportStateError(f'Import state for folder {name!r} must be an object')
            item.setdefault('folder', name)
            folder_state = FolderImportState.from_dict(item)
            state.folders[folder_state.folder] = folder_state
        if state.folders:
            state._active_folder = next(iter(state.folders))
        return state

    def dumps(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, sort_keys=True)

    @classmethod
    def loads(cls, value: Union[str, bytes], path: Optional[Union[str, 'os.PathLike[str]']] = None) \
            -> 'MailboxImportState':
        try:
            data = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise ImportStateError(f'Invalid import state JSON: {exc}') from exc
        return cls.from_dict(data, path=path)

    @classmethod
    def load(cls, path: Union[str, 'os.PathLike[str]']) -> 'MailboxImportState':
        path = Path(path)
        try:
            value = path.read_text(encoding='utf-8')
        except OSError as exc:
            raise ImportStateError(f'Can not read import state {path}: {exc}') from exc
        return cls.loads(value, path=path)

    @classmethod
    def load_or_create(cls, path: Union[str, 'os.PathLike[str]']) -> 'MailboxImportState':
        path = Path(path)
        if path.exists():
            return cls.load(path)
        return cls(path=path)

    def save(self, path: Optional[Union[str, 'os.PathLike[str]']] = None) -> None:
        target = Path(path) if path is not None else self.path
        if target is None:
            raise ImportStateError('Import state path is not set')
        try:
            _atomic_write(target, self.dumps().encode('utf-8'))
        except OSError as exc:
            raise ImportStateError(f'Can not write import state {target}: {exc}') from exc
        self.path = target


class IncrementalImporter:
    """Download only messages not yet confirmed for a folder/UIDVALIDITY epoch.

    ``state`` may be a :class:`MailboxImportState`, a path to a state file, or
    None for an in-memory state.  ``archive`` may be a path (then
    :class:`FileSystemArchive` is used) or any object with
    ``store(message, source)``.  ``on_message(message, source)`` is called before
    the UID is confirmed; an exception from either sink leaves the cursor behind
    that message.
    """

    def __init__(
            self,
            mailbox,
            state: Optional[Union[MailboxImportState, str, 'os.PathLike[str]']] = None,
            folder: Optional[str] = None,
            archive: Optional[Union[str, 'os.PathLike[str]', FileSystemArchive]] = None,
            on_message: Optional[Callable[[MailMessage, MessageSource], Any]] = None,
            *,
            mark_seen: bool = False,
            headers_only: bool = False,
            charset: Optional[str] = None,
            save_after_each: bool = True,
    ) -> None:
        if archive is not None and headers_only:
            raise ValueError('archive requires full messages, headers_only must be False')
        self.mailbox = mailbox
        self.state = _coerce_state(state)
        self.folder = folder
        self.archive = FileSystemArchive(archive) if isinstance(archive, (str, os.PathLike)) else archive
        self.on_message = on_message
        self.mark_seen = mark_seen
        self.headers_only = headers_only
        self.charset = charset
        self.save_after_each = save_after_each

    def run(self) -> ImportResult:
        """Run one incremental import and return its coverage conclusion."""
        folder = self.folder or self.mailbox.folder.get()
        if not folder:
            raise ValueError('folder is not specified and no folder is selected in the mailbox')
        folder = str(folder)
        folder_state = self.state.for_folder(folder)
        result = ImportResult(
            folder=folder,
            uidvalidity=int(folder_state.uidvalidity or 0),
            coverage=ImportCoverage(),
            cursor=folder_state.cursor,
            started_from_uid=folder_state.cursor + 1,
        )

        # Select the folder and read a snapshot of UIDVALIDITY/UIDNEXT.
        try:
            if self.mailbox.folder.get() != folder:
                self.mailbox.folder.set(folder)
            status = self.mailbox.folder.status(folder, ['UIDNEXT', 'UIDVALIDITY'])
        except Exception as exc:  # noqa: BLE001 - any transport/protocol failure is coverage-unknown
            return self._finish_unknown(folder_state, result, exc)

        if 'UIDVALIDITY' not in status:
            return self._finish_unknown(folder_state, result, ImportStateError('server did not return UIDVALIDITY'))
        try:
            uidvalidity = int(status['UIDVALIDITY'])
            uidnext = int(status['UIDNEXT']) if status.get('UIDNEXT') is not None else None
        except (TypeError, ValueError) as exc:
            return self._finish_unknown(folder_state, result, ImportStateError(f'invalid folder status: {status}'))

        result.uidvalidity_changed = folder_state.activate(uidvalidity)
        result.uidvalidity = uidvalidity
        result.started_from_uid = folder_state.cursor + 1
        start = folder_state.cursor + 1

        # A successful search below is what makes an empty result meaningful.
        try:
            raw_uids = self.mailbox.uids(A(uid=U(str(start), '*')), charset=self.charset)
        except Exception as exc:  # noqa: BLE001
            return self._finish_unknown(folder_state, result, exc)

        uids, result.skipped = _candidate_uids(raw_uids, start, folder_state)
        result.coverage = ImportCoverage(
            status=CoverageStatus.INCOMPLETE,
            through_uid=folder_state.cursor,
            uidnext=uidnext,
            searched=True,
            matched=len(uids),
        )

        for uid in uids:
            if folder_state.is_confirmed(uid):
                result.skipped += 1
                continue

            try:
                message = self._fetch_one(uid)
                source = MessageSource(folder=folder, uidvalidity=uidvalidity, uid=uid)
                if self.on_message is not None:
                    self.on_message(message, source)
                if self.archive is not None:
                    self.archive.store(message, source)
                    result.archived += 1
            except Exception as exc:  # noqa: BLE001 - a failed message must stop the cursor here
                result.failed_uid = uid
                result.errors.append(f"UID {uid}: {_error_text(exc)}")
                break

            # Snapshot before confirmation: if the state file can not be written,
            # roll back so in-memory state never runs ahead of disk.
            snapshot = folder_state.to_dict()
            folder_state.confirm(uid)
            folder_state.cursor = max(folder_state.cursor, uid)
            result.fetched += 1
            result.confirmed += 1
            if self.save_after_each:
                try:
                    self._persist()
                except Exception as exc:  # noqa: BLE001
                    self._restore(folder, snapshot)
                    folder_state = self.state.folders[folder]
                    result.fetched -= 1
                    result.confirmed -= 1
                    result.failed_uid = uid
                    result.errors.append(f"UID {uid}: state save: {_error_text(exc)}")
                    break

        if result.failed_uid is None:
            through = max(
                folder_state.cursor,
                uidnext - 1 if uidnext is not None else 0,
                uids[-1] if uids else 0,
                start - 1,
            )
            folder_state.cursor = through
            folder_state.scanned_through = through
            folder_state.coverage = ImportCoverage(
                status=CoverageStatus.COMPLETE,
                through_uid=through,
                uidnext=uidnext,
                searched=True,
                matched=len(uids),
            )
            result.coverage = folder_state.coverage
            result.cursor = folder_state.cursor
            # Always persist the final complete snapshot: even an empty successful
            # scan may advance the cursor to UIDNEXT - 1.
            try:
                self._persist()
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"state save: {_error_text(exc)}")
                folder_state.coverage = ImportCoverage(
                    status=CoverageStatus.UNKNOWN,
                    through_uid=folder_state.cursor,
                    uidnext=uidnext,
                    searched=True,
                    matched=len(uids),
                    error=result.errors[-1],
                )
        else:
            folder_state.coverage = ImportCoverage(
                status=CoverageStatus.INCOMPLETE,
                through_uid=folder_state.cursor,
                uidnext=uidnext,
                searched=True,
                matched=len(uids),
                error=result.errors[-1] if result.errors else None,
            )
            result.coverage = folder_state.coverage
            result.cursor = folder_state.cursor
            try:
                self._persist()
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"state save: {_error_text(exc)}")
                folder_state.coverage = ImportCoverage(
                    status=CoverageStatus.UNKNOWN,
                    through_uid=folder_state.cursor,
                    uidnext=uidnext,
                    searched=True,
                    matched=len(uids),
                    error=result.errors[-1],
                )

        result.coverage = folder_state.coverage
        result.cursor = folder_state.cursor
        return result

    import_messages = run
    sync = run

    def __call__(self) -> ImportResult:
        return self.run()

    def _fetch_one(self, uid: int) -> MailMessage:
        fetched = list(self.mailbox.fetch(
            uid_list=(str(uid),),
            mark_seen=self.mark_seen,
            headers_only=self.headers_only,
        ))
        if not fetched:
            raise ImportStateError(f'no message data returned for UID {uid}')
        message = fetched[0]
        if message.uid is not None and str(message.uid) != str(uid):
            raise ImportStateError(f'fetch returned UID {message.uid} instead of {uid}')
        return message

    def _finish_unknown(self, folder_state: FolderImportState, result: ImportResult, exc: Exception) -> ImportResult:
        message = _error_text(exc)
        folder_state.coverage = ImportCoverage(
            status=CoverageStatus.UNKNOWN,
            through_uid=folder_state.cursor,
            uidnext=None,
            searched=False,
            matched=0,
            error=message,
        )
        result.coverage = folder_state.coverage
        result.errors.append(message)
        result.cursor = folder_state.cursor
        result.uidvalidity = int(folder_state.uidvalidity or result.uidvalidity or 0)
        try:
            self._persist()
        except Exception as save_exc:  # noqa: BLE001 - coverage is already unknown
            result.errors.append(f"state save: {_error_text(save_exc)}")
        return result

    def _persist(self) -> None:
        if self.state.path is None:
            return
        self.state.save()

    def _restore(self, folder: str, snapshot: dict) -> None:
        self.state.folders[folder] = FolderImportState.from_dict(snapshot)


def incremental_import(mailbox, state=None, folder: Optional[str] = None, archive=None,
                       on_message=None, **kwargs) -> ImportResult:
    """Module-level shortcut for ``mailbox.incremental_import(...)``."""
    return IncrementalImporter(
        mailbox, state=state, folder=folder, archive=archive, on_message=on_message, **kwargs).run()


def _coerce_state(state: Optional[Union[MailboxImportState, str, 'os.PathLike[str]']]) -> MailboxImportState:
    if state is None:
        return MailboxImportState()
    if isinstance(state, MailboxImportState):
        return state
    return MailboxImportState.load_or_create(state)


def _coverage_status(value: Any) -> CoverageStatus:
    try:
        return CoverageStatus(value)
    except (TypeError, ValueError):
        return CoverageStatus.UNKNOWN


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ImportStateError(f'{name} must be an integer, bool received')
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ImportStateError(f'{name} must be an integer, {value!r} received') from exc
    if result < 0:
        raise ImportStateError(f'{name} must be non-negative, {result} received')
    return result


def _normalize_epoch(epoch: dict) -> dict:
    if not isinstance(epoch, dict):
        raise ImportStateError(f'epoch must be an object, {epoch!r} received')
    uidvalidity = epoch.get('uidvalidity')
    return {
        'uidvalidity': _non_negative_int(uidvalidity, 'uidvalidity') if uidvalidity is not None else None,
        'cursor': _non_negative_int(epoch.get('cursor') or 0, 'cursor'),
        'scanned_through': _non_negative_int(epoch.get('scanned_through') or 0, 'scanned_through'),
        'confirmed': _normalize_ranges(epoch.get('confirmed') or []),
        'coverage': ImportCoverage.from_dict(epoch.get('coverage')).to_dict(),
        'retired_at': epoch.get('retired_at'),
    }


def _candidate_uids(raw_uids: Iterable[Any], start: int, folder_state: FolderImportState) \
        -> Tuple[List[int], int]:
    """Return (uids to fetch, already-confirmed count) for one search result."""
    result = []
    skipped = 0
    seen = set()
    for raw_uid in raw_uids or ():
        try:
            uid = int(raw_uid)
        except (TypeError, ValueError):
            continue
        # Some servers answer an out-of-range "n:*" with the highest UID in the
        # mailbox.  It is below our start and must not re-confirm old messages.
        if uid < start or uid in seen:
            continue
        seen.add(uid)
        if folder_state.is_confirmed(uid):
            skipped += 1
            continue
        result.append(uid)
    result.sort()
    return result, skipped


def _normalize_ranges(ranges: Iterable[Iterable[int]]) -> List[List[int]]:
    normalized = []
    for item in ranges:
        values = list(item)
        if len(values) != 2:
            raise ImportStateError(f'confirmed range must have two values: {item!r}')
        start, end = int(values[0]), int(values[1])
        if start > end:
            start, end = end, start
        if start < 0:
            raise ImportStateError(f'confirmed range must be non-negative: {item!r}')
        normalized.append((start, end))
    normalized.sort()
    merged: List[List[int]] = []
    for start, end in normalized:
        if merged and start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f'.{path.name}.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'wb') as temp_file:
            temp_file.write(data)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_name, str(path))
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _first_header(message: MailMessage, name: str) -> Optional[str]:
    values = message.headers.get(name)
    return values[0] if values else None


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _error_text(exc: BaseException) -> str:
    text = str(exc)
    return f'{type(exc).__name__}: {text}' if text else type(exc).__name__


# Straightforward aliases for callers that prefer the longer names.
ImportState = MailboxImportState
IncrementalImportState = MailboxImportState
MailboxImporter = IncrementalImporter
IncrementalImport = IncrementalImporter
IncrementalImportResult = ImportResult
FileArchive = FileSystemArchive
import_incremental = incremental_import

__all__ = [
    'CoverageStatus',
    'FileArchive',
    'FileSystemArchive',
    'FolderImportState',
    'ImportCoverage',
    'ImportResult',
    'ImportState',
    'ImportStateError',
    'IncrementalImport',
    'IncrementalImporter',
    'IncrementalImportState',
    'import_incremental',
    'IncrementalImportResult',
    'incremental_import',
    'MailboxImportState',
    'MailboxImporter',
    'MessageSource',
]
