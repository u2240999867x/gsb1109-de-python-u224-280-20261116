"""Plan and execute restores from SMB shadow copies.

A shadow copy is meaningful only inside the share that enumerates it.  Two
shares may list the same timestamp without those copies belonging to the same
backup, so this module never infers a common restore set from timestamps
alone.  Callers supply a trusted backup-group correspondence that names the
snapshot for every share in scope.  The planner accepts only groups whose
correspondence and evidence are complete for all shares, whose copies fall in
the allowed time window, and whose required files can be found in the selected
copy.

The second half of the module pins directory and file reads to a selected set.
:class:`PinnedRestore` always addresses ``@GMT-...`` paths and has no API that
falls back to the live share.
"""

import datetime
from dataclasses import dataclass, field
from typing import Any, Callable, List, Mapping, Optional, Sequence, Tuple

from .smb_structs import OperationFailure


#: Windows previous-version directory name.  The timestamp is always UTC.
GMT_FORMAT = '@GMT-%Y.%m.%d-%H.%M.%S'

#: Diagnostic categories used by :class:`RestoreReport`.
MISSING_FILE = 'missing_file'
MISSING_CORRESPONDENCE = 'missing_correspondence'
CONTRADICTION = 'contradiction'
OUTSIDE_WINDOW = 'outside_window'
UNTRUSTED_GROUP = 'untrusted_group'
SNAPSHOT_UNAVAILABLE = 'snapshot_unavailable'
OUT_OF_SCOPE = 'out_of_scope'

#: Statuses that mean "the path does not exist" rather than an access or
#: transport failure.  See [MS-ERREF].
_NOT_FOUND_STATUSES = frozenset((
    0x80000006,  # STATUS_NO_MORE_FILES
    0xC000000F,  # STATUS_NO_SUCH_FILE
    0xC0000034,  # STATUS_OBJECT_NAME_NOT_FOUND
    0xC000003A,  # STATUS_OBJECT_PATH_NOT_FOUND
    0xC0000033,  # STATUS_OBJECT_NAME_INVALID
    0xC00000BA,  # STATUS_FILE_IS_A_DIRECTORY
    0xC0000103,  # STATUS_NOT_A_DIRECTORY
))


class RestoreError(Exception):
    """Base class for restore planning and pinned-read errors."""


class NoCommonRestoreSetError(RestoreError):
    """Raised when no evidence-complete common restore set exists."""


class AmbiguousRestoreSetError(RestoreError):
    """Raised when several complete sets exist and no group was selected."""


class RestoreSetRevokedError(RestoreError):
    """Raised when a set was revoked because one of its copies is unavailable."""


class SnapshotUnavailableError(RestoreSetRevokedError):
    """Raised when a read targets a copy that is no longer available."""


class ShareNotInRestoreSetError(RestoreError):
    """Raised when a read names a share outside the selected restore set."""


class SnapshotNotFoundError(RestoreError):
    """Raised when an invalidation names a copy that was never listed."""


def _coerce_datetime(value: Any, name: str = 'timestamp') -> datetime.datetime:
    """Return an aware UTC datetime.

    ``SMBConnection.listSnapshots`` returns naive datetimes in GMT/UTC, so
    naive values are interpreted as UTC.  ISO-8601 strings are accepted for
    callers loading a correspondence manifest from JSON.
    """
    if isinstance(value, str):
        text = value.strip()
        if text.endswith(('Z', 'z')):
            text = text[:-1] + '+00:00'
        try:
            value = datetime.datetime.fromisoformat(text)
        except ValueError:
            raise ValueError('%s must be a datetime or ISO-8601 string: %r' % (name, value))
    if isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
        value = datetime.datetime.combine(value, datetime.time())
    if not isinstance(value, datetime.datetime):
        raise TypeError('%s must be a datetime, date, or ISO-8601 string: %r' % (name, value))
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def format_snapshot_time(value: Any) -> str:
    """Format a timestamp as the UTC ``@GMT-...`` component."""
    return _coerce_datetime(value).strftime(GMT_FORMAT)


def normalize_path(path: Any) -> str:
    """Normalize a share-relative path and reject parent traversal."""
    if path is None:
        return '/'
    if not isinstance(path, str):
        raise TypeError('path must be a string: %r' % (path,))
    parts = []
    for part in path.replace('\\', '/').split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            raise ValueError('path must not contain "..": %r' % (path,))
        if part.upper().startswith('@GMT-'):
            raise ValueError(
                'logical path must not contain a shadow-copy component: %r' % (path,))
        parts.append(part)
    return '/' + '/'.join(parts)


def _path_key(path: Any) -> str:
    return normalize_path(path).casefold()


def _share_key(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError('share name must be a non-empty string: %r' % (name,))
    return name.strip().casefold()


def _has_evidence(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    try:
        return bool(len(value))
    except TypeError:
        return bool(value)


def _clean_mapping_key(name: Any) -> str:
    return str(name).strip().casefold()


def _pop_first(data: dict, names: Sequence[str]) -> Any:
    for name in names:
        if name in data:
            return data.pop(name)
    return None


def _snapshot_kwargs(data: Mapping) -> dict:
    """Translate common snapshot field names into constructor arguments."""
    values = dict(data)
    timestamp = _pop_first(values, ('time', 'snapshot_time', 'copy_time', 'date'))
    snapshot_id = values.pop('id', values.pop('copy_id', values.pop('snapshot', None)))
    declared_group = values.pop('declared_backup_group',
                                 values.pop('backup_group', values.pop('group', None)))
    if 'timestamp' not in values and timestamp is not None:
        values['timestamp'] = timestamp
    if 'snapshot_id' not in values and snapshot_id is not None:
        values['snapshot_id'] = snapshot_id
    if 'declared_group' not in values and declared_group is not None:
        values['declared_group'] = declared_group
    unknown = set(values) - set(('timestamp', 'snapshot_id', 'files', 'declared_group'))
    if unknown:
        raise TypeError('unknown snapshot fields: %s' % sorted(unknown))
    return values


def _group_member_kwargs(share: str, data: Mapping) -> dict:
    values = dict(data)
    values.pop('share', None)
    snapshot_id = _pop_first(values, ('id', 'copy_id', 'snapshot'))
    timestamp = _pop_first(values, ('time', 'snapshot_time', 'copy_time', 'date'))
    evidence = _pop_first(values, ('proof', 'catalogue', 'catalog', 'reference'))
    values.pop('declared_group', None)
    if 'snapshot_id' not in values and snapshot_id is not None:
        values['snapshot_id'] = snapshot_id
    if 'timestamp' not in values and timestamp is not None:
        values['timestamp'] = timestamp
    if 'evidence' not in values and evidence is not None:
        values['evidence'] = evidence
    return values


def _entry_present(value: Any) -> bool:
    if value is False:
        return False
    if isinstance(value, Mapping):
        if 'exists' in value:
            return bool(value.get('exists'))
        if 'present' in value:
            return bool(value.get('present'))
        return True
    if hasattr(value, 'exists') and not isinstance(value, (str, bytes)):
        return bool(getattr(value, 'exists'))
    return True


def _snapshot_file_entries(files: Any) -> Optional[dict]:
    if files is None:
        return None
    entries = {}
    if isinstance(files, Mapping):
        source = files.items()
    else:
        source = []
        for item in files:
            if isinstance(item, str):
                source.append((item, True))
            elif isinstance(item, Mapping):
                path = item.get('path', item.get('filename', item.get('name')))
                if path is None:
                    raise TypeError('snapshot file entry needs a path: %r' % (item,))
                source.append((path, item))
            elif hasattr(item, 'filename'):
                source.append((item.filename, item))
            else:
                raise TypeError('cannot interpret snapshot file entry: %r' % (item,))
    for path, value in source:
        entries[_path_key(path)] = value
    return entries


@dataclass
class Snapshot:
    """One shadow copy listed by one share.

    :param timestamp: Copy timestamp.  Naive values are interpreted as UTC.
    :param snapshot_id: Optional provider identifier.  When absent, the UTC
        ``@GMT-...`` name is used as the identity.
    :param files: Optional manifest of paths known to exist in this copy.
        It may be a mapping or an iterable of path strings.  Supplying a
        manifest turns "not listed" into a definite missing-file result;
        leaving it ``None`` means file existence is unknown unless a probe is
        supplied to the planner.
    :param declared_group: Optional group declared by the snapshot metadata.
        A value that conflicts with the trusted correspondence is reported as
        a declaration contradiction.
    """

    timestamp: datetime.datetime
    snapshot_id: Optional[str] = None
    files: Any = None
    declared_group: Optional[str] = None

    def __post_init__(self):
        self.timestamp = _coerce_datetime(self.timestamp)
        if (self.files is None and self.snapshot_id is not None and
                not isinstance(self.snapshot_id, str)):
            if isinstance(self.snapshot_id, (Mapping, list, tuple, set, frozenset)):
                self.files = self.snapshot_id
                self.snapshot_id = None
        if self.snapshot_id is None or not str(self.snapshot_id).strip():
            self.snapshot_id = format_snapshot_time(self.timestamp)
        else:
            self.snapshot_id = str(self.snapshot_id).strip()
        if self.declared_group is not None:
            self.declared_group = str(self.declared_group).strip() or None
        self._file_entries = _snapshot_file_entries(self.files)

    @property
    def identity(self) -> str:
        return self.snapshot_id

    def has_file(self, path: Any, is_directory: bool = False) -> Optional[bool]:
        """Return whether *path* is in the known manifest.

        ``None`` means there is no manifest and existence cannot be decided
        locally.  A directory is considered present when it is listed itself
        or when any listed path is below it.
        """
        if self._file_entries is None:
            return None
        key = _path_key(path)
        if key in self._file_entries:
            return _entry_present(self._file_entries[key])
        if is_directory:
            prefix = key.rstrip('/') + '/'
            return any(candidate.startswith(prefix) and _entry_present(value)
                       for candidate, value in self._file_entries.items())
        return False

    def as_dict(self) -> dict:
        result = {
            'timestamp': self.timestamp.isoformat(),
            'snapshot_id': self.snapshot_id,
        }
        if self.files is not None:
            if self._file_entries is None:
                result['files'] = _jsonable(self.files)
            else:
                result['files'] = dict(
                    (path, _jsonable(value)) for path, value in self._file_entries.items()
                )
        if self.declared_group is not None:
            result['declared_group'] = self.declared_group
        return result


@dataclass
class ShareSnapshots:
    """The snapshots enumerated by one share."""

    name: str
    snapshots: Sequence[Snapshot] = ()

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError('share name must be a non-empty string')
        self.name = self.name.strip()
        snapshots = self.snapshots
        if isinstance(snapshots, Mapping) and (
                'snapshots' in snapshots or 'copies' in snapshots):
            snapshots = snapshots.get('snapshots', snapshots.get('copies'))
        if snapshots is None:
            snapshots = ()
        elif isinstance(snapshots, (Snapshot, datetime.datetime, str, Mapping)):
            snapshots = (snapshots,)
        converted = []
        for item in snapshots:
            if isinstance(item, Snapshot):
                converted.append(item)
            elif isinstance(item, datetime.datetime):
                converted.append(Snapshot(item))
            elif isinstance(item, str):
                converted.append(Snapshot(item))
            elif isinstance(item, Mapping):
                converted.append(Snapshot(**_snapshot_kwargs(item)))
            elif isinstance(item, datetime.date) and not isinstance(item, datetime.datetime):
                converted.append(Snapshot(item))
            elif hasattr(item, 'timestamp') or hasattr(item, 'time'):
                timestamp = getattr(item, 'timestamp', getattr(item, 'time', None))
                snapshot_id = getattr(item, 'snapshot_id', getattr(item, 'id', None))
                files = getattr(item, 'files', None)
                declared = getattr(item, 'declared_group',
                                   getattr(item, 'backup_group', None))
                converted.append(Snapshot(timestamp, snapshot_id, files, declared))
            else:
                raise TypeError('cannot interpret snapshot: %r' % (item,))
        self.snapshots = tuple(converted)


def snapshots_for_connection(connection: Any, share: str, path: Any = '/',
                             timeout: int = 30) -> ShareSnapshots:
    """Enumerate a share's shadow copies through an SMB connection.

    ``SMBConnection.listSnapshots`` returns naive datetimes in GMT/UTC; this
    helper passes them through :class:`Snapshot`, which applies the same UTC
    interpretation.
    """
    values = connection.listSnapshots(share, path, timeout=timeout)
    return ShareSnapshots(share, values)


@dataclass
class GroupMember:
    """A trusted correspondence entry for one share.

    ``snapshot_id`` and/or ``timestamp`` identify the listed copy.  At least
    one should be supplied.  ``evidence`` is opaque proof supplied by the
    backup catalogue; empty values count as missing evidence.
    """

    share: str
    snapshot_id: Optional[str] = None
    timestamp: Optional[datetime.datetime] = None
    evidence: Any = None

    def __post_init__(self):
        if not isinstance(self.share, str) or not self.share.strip():
            raise ValueError('member share must be a non-empty string')
        self.share = self.share.strip()
        if self.snapshot_id is not None:
            self.snapshot_id = str(self.snapshot_id).strip() or None
        if self.timestamp is not None:
            self.timestamp = _coerce_datetime(self.timestamp)


@dataclass
class BackupGroup:
    """A trusted backup group and its per-share correspondence.

    ``members`` may be a sequence of :class:`GroupMember` objects or a mapping
    keyed by share name.  Mapping values may be a :class:`GroupMember`, a
    snapshot id string, a datetime, or a mapping with ``snapshot_id``,
    ``timestamp``, and ``evidence`` keys.

    ``evidence`` may be supplied per member or once for the group.  A mapping
    supplied at group level is indexed by share; any other non-empty value
    covers the whole group.
    """

    id: str
    members: Any
    time: Optional[datetime.datetime] = None
    trusted: bool = True
    evidence: Any = None

    def __post_init__(self):
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError('backup group id must be a non-empty string')
        self.id = self.id.strip()
        if self.time is not None:
            self.time = _coerce_datetime(self.time)
        self.members = _coerce_members(self.members)


def _coerce_member(share: str, value: Any) -> GroupMember:
    if value is None:
        return GroupMember(share)
    if isinstance(value, GroupMember):
        if _share_key(value.share) != _share_key(share):
            return GroupMember(share, value.snapshot_id, value.timestamp, value.evidence)
        return value
    if isinstance(value, Snapshot):
        return GroupMember(share, snapshot_id=value.snapshot_id, timestamp=value.timestamp)
    if isinstance(value, (datetime.datetime, datetime.date)):
        return GroupMember(share, timestamp=value)
    if hasattr(value, 'snapshot_id') or hasattr(value, 'timestamp'):
        return GroupMember(
            share,
            snapshot_id=getattr(value, 'snapshot_id', None),
            timestamp=getattr(value, 'timestamp', None),
            evidence=getattr(value, 'evidence', None),
        )
    if isinstance(value, str):
        return GroupMember(share, snapshot_id=value)
    if isinstance(value, Mapping):
        data = _group_member_kwargs(share, value)
        allowed = set(('snapshot_id', 'timestamp', 'evidence', 'declared_group'))
        unknown = set(data) - allowed
        if unknown:
            raise TypeError('unknown group member fields for %s: %s' % (share, sorted(unknown)))
        data.pop('declared_group', None)
        return GroupMember(share, **data)
    if isinstance(value, (tuple, list)) and len(value) in (1, 2):
        snapshot_id = value[0]
        evidence = value[1] if len(value) == 2 else None
        if isinstance(snapshot_id, datetime.datetime):
            return GroupMember(share, timestamp=snapshot_id, evidence=evidence)
        return GroupMember(share, snapshot_id=snapshot_id, evidence=evidence)
    raise TypeError('cannot interpret group member for %s: %r' % (share, value))


def _coerce_members(members: Any) -> Tuple[GroupMember, ...]:
    if members is None:
        return ()
    if isinstance(members, GroupMember):
        return (members,)
    if isinstance(members, Mapping):
        return tuple(_coerce_member(share, value) for share, value in members.items())
    result = []
    for value in members:
        if isinstance(value, GroupMember):
            result.append(value)
        elif isinstance(value, Mapping):
            data = dict(value)
            share = data.pop('share', None)
            if share is None:
                raise TypeError('group member mapping needs a share')
            result.append(_coerce_member(share, data))
        elif isinstance(value, (tuple, list)) and len(value) in (2, 3):
            share = value[0]
            member_value = value[1]
            member = _coerce_member(share, member_value)
            if len(value) == 3:
                member.evidence = value[2]
            result.append(member)
        elif isinstance(value, datetime.date) and not isinstance(value, datetime.datetime):
            raise TypeError('group member without a share: %r' % (value,))
        else:
            raise TypeError('cannot interpret group member: %r' % (value,))
    return tuple(result)


@dataclass
class TimeWindow:
    """Inclusive allowed interval for every selected copy."""

    start: Optional[datetime.datetime] = None
    end: Optional[datetime.datetime] = None

    def __post_init__(self):
        if self.start is not None:
            self.start = _coerce_datetime(self.start, 'window start')
        if self.end is not None:
            self.end = _coerce_datetime(self.end, 'window end')

    @property
    def valid(self) -> bool:
        return self.start is None or self.end is None or self.start <= self.end

    def contains(self, value: Any) -> bool:
        moment = _coerce_datetime(value)
        if self.start is not None and moment < self.start:
            return False
        if self.end is not None and moment > self.end:
            return False
        return True

    def as_dict(self) -> dict:
        return {
            'start': self.start.isoformat() if self.start else None,
            'end': self.end.isoformat() if self.end else None,
        }


@dataclass
class RequiredFile:
    """A path that must be present in the selected copy.

    A trailing slash marks a directory.  The path is share-relative; parent
    traversal is rejected because the pinned session must not escape the
    selected ``@GMT-...`` directory.
    """

    share: str
    path: str
    is_directory: bool = False

    def __post_init__(self):
        if not isinstance(self.share, str) or not self.share.strip():
            raise ValueError('required-file share must be a non-empty string')
        self.share = self.share.strip()
        raw = self.path
        normalized = normalize_path(raw)
        directory = (bool(self.is_directory) or normalized == '/' or
                     (isinstance(raw, str) and raw.replace('\\', '/').endswith('/')))
        if directory and normalized != '/':
            normalized += '/'
        self.path = normalized
        self.is_directory = directory

    @property
    def key(self) -> Tuple[str, str, bool]:
        return (_share_key(self.share), _path_key(self.path), self.is_directory)

    def as_dict(self) -> dict:
        return {'share': self.share, 'path': self.path, 'is_directory': self.is_directory}


def _coerce_window(value: Any) -> TimeWindow:
    if value is None:
        return TimeWindow()
    if isinstance(value, TimeWindow):
        return value
    if isinstance(value, Mapping):
        data = dict(value)
        if 'start' not in data and 'from' in data:
            data['start'] = data.pop('from')
        if 'end' not in data and 'to' in data:
            data['end'] = data.pop('to')
        unknown = set(data) - set(('start', 'end'))
        if unknown:
            raise TypeError('unknown window fields: %s' % sorted(unknown))
        return TimeWindow(data.get('start'), data.get('end'))
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return TimeWindow(value[0], value[1])
    raise TypeError('window must be a TimeWindow, (start, end), or a mapping')


def _coerce_required_files(value: Any) -> Tuple[RequiredFile, ...]:
    if value is None:
        return ()
    items = []
    if isinstance(value, RequiredFile):
        value = [value]
    if isinstance(value, Mapping):
        for share, paths in value.items():
            if isinstance(paths, (str, bytes)) or isinstance(paths, RequiredFile):
                paths = [paths]
            if isinstance(paths, tuple) and len(paths) == 2 and isinstance(paths[1], bool):
                paths = [paths]
            for path in paths:
                if isinstance(path, RequiredFile):
                    items.append(path)
                elif isinstance(path, Mapping):
                    data = dict(path)
                    data.setdefault('share', share)
                    items.append(RequiredFile(**data))
                else:
                    items.append(RequiredFile(share, path))
    else:
        for item in value:
            if isinstance(item, RequiredFile):
                items.append(item)
            elif isinstance(item, Mapping):
                items.append(RequiredFile(**dict(item)))
            elif isinstance(item, (tuple, list)) and len(item) in (2, 3):
                items.append(RequiredFile(item[0], item[1], item[2] if len(item) == 3 else False))
            elif hasattr(item, 'share') and hasattr(item, 'path'):
                items.append(RequiredFile(
                    item.share, item.path, bool(getattr(item, 'is_directory', False))))
            else:
                raise TypeError('cannot interpret required file: %r' % (item,))
    result = []
    seen = set()
    for item in items:
        if item.key not in seen:
            seen.add(item.key)
            result.append(item)
    return tuple(result)


def _coerce_shares(shares: Any) -> Tuple[List[ShareSnapshots], List['RestoreDiagnostic']]:
    diagnostics = []
    result = []
    seen = {}
    if isinstance(shares, ShareSnapshots):
        shares = [shares]
    if isinstance(shares, Mapping):
        source = shares.items()
    else:
        source = []
        for item in shares:
            if isinstance(item, ShareSnapshots):
                source.append((item.name, item.snapshots))
            elif isinstance(item, Mapping):
                name = item.get('name') or item.get('share')
                if name is None:
                    raise TypeError('share mapping needs a name')
                snapshots = item.get('snapshots', item.get('copies', ()))
                source.append((name, snapshots))
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                source.append((item[0], item[1]))
            elif hasattr(item, 'name') and hasattr(item, 'snapshots'):
                source.append((item.name, item.snapshots))
            else:
                raise TypeError('cannot interpret share listing: %r' % (item,))

    for name, snapshots in source:
        if isinstance(snapshots, Snapshot):
            snapshots = [snapshots]
        listing = ShareSnapshots(name, snapshots)
        key = _share_key(listing.name)
        if key in seen:
            diagnostics.append(RestoreDiagnostic(
                CONTRADICTION, 'duplicate_share',
                'share %r is declared more than once' % listing.name,
                share=listing.name,
            ))
            continue
        seen[key] = listing
        result.append(listing)
    return result, diagnostics


@dataclass
class RestoreMember:
    """One share and its selected copy inside a restore set."""

    share: str
    snapshot: Snapshot
    evidence: Any = None

    @property
    def share_key(self) -> str:
        return _share_key(self.share)

    @property
    def snapshot_id(self) -> str:
        return self.snapshot.identity

    def pinned_path(self, path: Any = '/') -> str:
        return build_snapshot_path(self.snapshot, path)

    def as_dict(self) -> dict:
        result = {
            'share': self.share,
            'snapshot_id': self.snapshot.identity,
            'timestamp': self.snapshot.timestamp.isoformat(),
        }
        if self.evidence is not None:
            result['evidence'] = _jsonable(self.evidence)
        return result


@dataclass
class RestoreSet:
    """A complete set of copies that the trusted catalogue assigns to one backup."""

    group_id: str
    members: Tuple[RestoreMember, ...]
    group_time: Optional[datetime.datetime] = None
    evidence: Any = None
    status: str = 'complete'
    revocation_reason: Optional[str] = None

    def __post_init__(self):
        self.members = tuple(self.members)

    @property
    def complete(self) -> bool:
        return self.status == 'complete'

    @property
    def shares(self) -> Tuple[str, ...]:
        return tuple(member.share for member in self.members)

    @property
    def snapshots(self) -> dict:
        return dict((member.share, member.snapshot) for member in self.members)

    @property
    def dependencies(self) -> Tuple[Tuple[str, str], ...]:
        return tuple((member.share_key, member.snapshot_id) for member in self.members)

    @property
    def selected_snapshots(self) -> Tuple[Snapshot, ...]:
        return tuple(member.snapshot for member in self.members)

    def __contains__(self, share: Any) -> bool:
        key = _share_key(share)
        return any(member.share_key == key for member in self.members)

    def __getitem__(self, share: Any) -> RestoreMember:
        key = _share_key(share)
        for member in self.members:
            if member.share_key == key:
                return member
        raise KeyError(share)

    def pinned_path(self, share: Any, path: Any = '/') -> str:
        return self[share].pinned_path(path)

    def as_dict(self) -> dict:
        result = {
            'group_id': self.group_id,
            'status': self.status,
            'group_time': self.group_time.isoformat() if self.group_time else None,
            'members': [member.as_dict() for member in self.members],
            'revocation_reason': self.revocation_reason,
        }
        if self.evidence is not None:
            result['evidence'] = _jsonable(self.evidence)
        return result


@dataclass
class RestoreDiagnostic:
    """A structured reason why a group or path is not evidence-complete."""

    kind: str
    reason: str
    message: str
    group_id: Optional[str] = None
    share: Optional[str] = None
    snapshot_id: Optional[str] = None
    path: Optional[str] = None
    depends_on: Tuple[Tuple[str, str], ...] = ()
    details: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        result = {
            'kind': self.kind,
            'reason': self.reason,
            'message': self.message,
        }
        if self.group_id is not None:
            result['group_id'] = self.group_id
        if self.share is not None:
            result['share'] = self.share
        if self.snapshot_id is not None:
            result['snapshot_id'] = self.snapshot_id
        if self.path is not None:
            result['path'] = self.path
        if self.depends_on:
            result['depends_on'] = [list(dep) for dep in self.depends_on]
        if self.details:
            result['details'] = _jsonable(self.details)
        return result


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return dict((str(key), _jsonable(item)) for key, item in value.items())
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    return value


class RestoreReport:
    """The result of :func:`find_restore_sets`.

    ``restore_sets`` contains only complete, currently valid sets.
    ``diagnostics`` contains active diagnostics; diagnostics that depended on
    a revoked copy are withdrawn and available from
    ``withdrawn_diagnostics``.
    """

    def __init__(self, shares: Sequence[ShareSnapshots], restore_sets: Sequence[RestoreSet],
                 diagnostics: Sequence[RestoreDiagnostic], window: Optional[TimeWindow] = None,
                 required_files: Sequence[RequiredFile] = ()):
        self.shares = tuple(shares)
        self.window = window or TimeWindow()
        self.required_files = tuple(required_files)
        self._sets = list(restore_sets)
        self._diagnostics = list(diagnostics)
        self._revoked = {}

    @property
    def restore_sets(self) -> Tuple[RestoreSet, ...]:
        return tuple(item for item in self._sets if item.complete)

    @property
    def common_sets(self) -> Tuple[RestoreSet, ...]:
        return self.restore_sets

    @property
    def complete_sets(self) -> Tuple[RestoreSet, ...]:
        return self.restore_sets

    @property
    def revoked_sets(self) -> Tuple[RestoreSet, ...]:
        return tuple(item for item in self._sets if not item.complete)

    @property
    def all_sets(self) -> Tuple[RestoreSet, ...]:
        return tuple(self._sets)

    def _withdrawn(self, diagnostic: RestoreDiagnostic) -> bool:
        return any(dependency in self._revoked for dependency in diagnostic.depends_on)

    @property
    def diagnostics(self) -> Tuple[RestoreDiagnostic, ...]:
        return tuple(item for item in self._diagnostics if not self._withdrawn(item))

    @property
    def all_diagnostics(self) -> Tuple[RestoreDiagnostic, ...]:
        return tuple(self._diagnostics)

    @property
    def withdrawn_diagnostics(self) -> Tuple[RestoreDiagnostic, ...]:
        return tuple(item for item in self._diagnostics if self._withdrawn(item))

    @property
    def missing_files(self) -> Tuple[RestoreDiagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.kind == MISSING_FILE)

    @property
    def missing_correspondence(self) -> Tuple[RestoreDiagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.kind == MISSING_CORRESPONDENCE)

    @property
    def contradictions(self) -> Tuple[RestoreDiagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.kind == CONTRADICTION)

    @property
    def outside_window(self) -> Tuple[RestoreDiagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.kind == OUTSIDE_WINDOW)

    @property
    def unavailable(self) -> Tuple[RestoreDiagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.kind == SNAPSHOT_UNAVAILABLE)

    @property
    def has_complete_set(self) -> bool:
        return bool(self.restore_sets)

    @property
    def group_evidence_missing(self) -> Tuple[RestoreDiagnostic, ...]:
        """Alias for diagnostics about absent group correspondence/evidence."""
        return self.missing_correspondence

    @property
    def file_missing(self) -> Tuple[RestoreDiagnostic, ...]:
        """Alias for :attr:`missing_files`."""
        return self.missing_files

    @property
    def declarations_conflicting(self) -> Tuple[RestoreDiagnostic, ...]:
        """Alias for :attr:`contradictions`."""
        return self.contradictions

    def summary(self) -> dict:
        """Return counts suitable for logs and command-line output."""
        return {
            'shares': [item.name for item in self.shares],
            'complete_sets': [item.group_id for item in self.restore_sets],
            'revoked_sets': [item.group_id for item in self.revoked_sets],
            'missing_files': len(self.missing_files),
            'missing_correspondence': len(self.missing_correspondence),
            'contradictions': len(self.contradictions),
            'outside_window': len(self.outside_window),
            'unavailable': len(self.unavailable),
        }

    def diagnostics_for(self, group_id: str) -> Tuple[RestoreDiagnostic, ...]:
        return tuple(item for item in self.diagnostics if item.group_id == group_id)

    def set_for_group(self, group_id: str) -> RestoreSet:
        for item in self._sets:
            if item.group_id == group_id:
                return item
        raise KeyError(group_id)

    def select_set(self, group_id: Optional[str] = None) -> RestoreSet:
        """Return one complete set, requiring an explicit choice if ambiguous."""
        if group_id is not None:
            for item in self._sets:
                if item.group_id == group_id:
                    if not item.complete:
                        raise RestoreSetRevokedError(
                            'restore set %r is revoked: %s' % (group_id, item.revocation_reason)
                        )
                    return item
            raise NoCommonRestoreSetError('no restore set for group %r' % (group_id,))
        sets = self.restore_sets
        if not sets:
            raise NoCommonRestoreSetError('no evidence-complete common restore set exists')
        if len(sets) > 1:
            raise AmbiguousRestoreSetError(
                'several complete restore sets exist; select one by group id: %s'
                % ', '.join(sorted(item.group_id for item in sets))
            )
        return sets[0]

    def invalidate_snapshot(self, share: Optional[str] = None,
                            snapshot_id: Optional[str] = None,
                            timestamp: Any = None,
                            snapshot: Optional[Snapshot] = None,
                            reason: str = 'snapshot is no longer available'
                            ) -> Tuple[RestoreSet, ...]:
        """Revoke only conclusions that depend on one copy.

        Sets that use the copy are marked revoked.  Diagnostics whose proof
        referenced that copy are withdrawn; unrelated sets and diagnostics are
        left untouched.  No live-directory fallback is created.
        """
        if snapshot is not None:
            if snapshot_id is not None or timestamp is not None:
                raise TypeError('pass snapshot or snapshot_id/timestamp, not both')
            snapshot_id = snapshot.identity
            timestamp = snapshot.timestamp
        if snapshot_id is None and timestamp is None:
            raise TypeError('snapshot_id, timestamp, or snapshot is required')
        if timestamp is not None:
            timestamp = _coerce_datetime(timestamp)

        refs = self._matching_dependencies(share, snapshot_id, timestamp)
        if not refs:
            raise SnapshotNotFoundError('no listed copy matches the invalidation request')
        if share is None and len(set(ref[0] for ref in refs)) > 1:
            raise ValueError(
                'the invalidation matches copies on several shares (%s); '
                'name the share whose copy became unavailable'
                % ', '.join(sorted(set(ref[0] for ref in refs)))
            )

        for ref in refs:
            self._revoked[ref] = reason
        revoked = []
        for item in self._sets:
            if not item.complete:
                continue
            if any(dependency in self._revoked for dependency in item.dependencies):
                item.status = 'revoked'
                item.revocation_reason = reason
                revoked.append(item)
        if refs:
            share_label = share or ', '.join(sorted(set(ref[0] for ref in refs)))
            self._diagnostics.append(RestoreDiagnostic(
                SNAPSHOT_UNAVAILABLE, 'snapshot_unavailable',
                reason,
                share=share_label,
                snapshot_id=snapshot_id if snapshot_id is not None else None,
                details={'dependencies': [list(ref) for ref in refs]},
            ))
        return tuple(revoked)

    def _matching_dependencies(self, share: Optional[str],
                               snapshot_id: Optional[str],
                               timestamp: Optional[datetime.datetime]
                               ) -> Tuple[Tuple[str, str], ...]:
        matches = []
        share_key = _share_key(share) if share is not None else None
        for listing in self.shares:
            listing_key = _share_key(listing.name)
            if share_key is not None and listing_key != share_key:
                continue
            for item in listing.snapshots:
                if snapshot_id is not None and item.snapshot_id != str(snapshot_id):
                    continue
                if timestamp is not None and item.timestamp != timestamp:
                    continue
                matches.append((listing_key, item.snapshot_id))
        return tuple(dict.fromkeys(matches))

    def as_dict(self) -> dict:
        return {
            'shares': [
                {'name': item.name,
                 'snapshots': [snapshot.as_dict() for snapshot in item.snapshots]}
                for item in self.shares
            ],
            'window': self.window.as_dict(),
            'required_files': [item.as_dict() for item in self.required_files],
            'restore_sets': [item.as_dict() for item in self.restore_sets],
            'revoked_sets': [item.as_dict() for item in self.revoked_sets],
            'missing_files': [item.as_dict() for item in self.missing_files],
            'missing_correspondence': [item.as_dict() for item in self.missing_correspondence],
            'contradictions': [item.as_dict() for item in self.contradictions],
            'outside_window': [item.as_dict() for item in self.outside_window],
            'unavailable': [item.as_dict() for item in self.unavailable],
        }


def build_snapshot_path(snapshot: Snapshot, path: Any = '/') -> str:
    """Build the share-relative ``@GMT-...`` path for a copy."""
    clean = normalize_path(path)
    prefix = format_snapshot_time(snapshot.timestamp)
    if clean == '/':
        return '/' + prefix
    return '/' + prefix + clean


def _diagnostic(kind: str, reason: str, message: str, group: Optional[BackupGroup] = None,
                share: Optional[str] = None, snapshot: Optional[Snapshot] = None,
                path: Optional[str] = None,
                details: Optional[Mapping[str, Any]] = None) -> RestoreDiagnostic:
    dependencies = ()
    snapshot_id = None
    if snapshot is not None:
        snapshot_id = snapshot.identity
        if share is not None:
            dependencies = ((_share_key(share), snapshot.identity),)
    return RestoreDiagnostic(
        kind=kind,
        reason=reason,
        message=message,
        group_id=group.id if group is not None else None,
        share=share,
        snapshot_id=snapshot_id,
        path=path,
        depends_on=dependencies,
        details=dict(details or {}),
    )


def _listing_maps(listing: ShareSnapshots):
    by_id = {}
    by_time = {}
    duplicate_ids = set()
    for snapshot in listing.snapshots:
        if snapshot.identity in by_id:
            duplicate_ids.add(snapshot.identity)
        else:
            by_id[snapshot.identity] = snapshot
        by_time.setdefault(snapshot.timestamp, []).append(snapshot)
    return by_id, by_time, duplicate_ids


def _group_evidence(group: BackupGroup, member: GroupMember) -> Any:
    if _has_evidence(member.evidence):
        return member.evidence
    evidence = group.evidence
    if isinstance(evidence, Mapping):
        member_keys = set(_share_key(item.share) for item in group.members)
        per_share = any(_clean_mapping_key(key) in member_keys for key in evidence)
        if per_share:
            for key, value in evidence.items():
                if _clean_mapping_key(key) == _share_key(member.share) and _has_evidence(value):
                    return value
            for key in ('*', 'all', 'group', 'backup'):
                value = evidence.get(key)
                if _has_evidence(value):
                    return value
            return None
        # A mapping without share keys is one evidence object, not a map.
        return evidence if _has_evidence(evidence) else None
    return evidence if _has_evidence(evidence) else None


def _resolve_member(member: GroupMember,
                    listing: ShareSnapshots) -> Tuple[Optional[Snapshot], List[RestoreDiagnostic]]:
    diagnostics = []
    by_id, by_time, duplicate_ids = _listing_maps(listing)
    if member.snapshot_id is not None and member.timestamp is not None:
        candidate = by_id.get(member.snapshot_id)
        if candidate is not None and candidate.timestamp != member.timestamp:
            diagnostics.append(_diagnostic(
                CONTRADICTION, 'snapshot_time_conflict',
                'group declares snapshot %r at %s but share %r lists %s' % (
                    member.snapshot_id, member.timestamp.isoformat(), listing.name,
                    candidate.timestamp.isoformat()),
                share=listing.name, snapshot=candidate,
                details={'declared_timestamp': member.timestamp,
                         'listed_timestamp': candidate.timestamp},
            ))
            return None, diagnostics
    def same_time_conflict(snapshot):
        same_time = by_time.get(snapshot.timestamp, ())
        if len(same_time) > 1:
            diagnostics.append(_diagnostic(
                CONTRADICTION, 'ambiguous_snapshot_time',
                'share %r lists %d copies at %s; @GMT paths cannot identify the copy'
                % (listing.name, len(same_time), snapshot.timestamp.isoformat()),
                share=listing.name, snapshot=snapshot,
                details={'snapshot_ids': [item.identity for item in same_time]},
            ))
            return True
        return False

    if member.snapshot_id is not None:
        if member.snapshot_id in duplicate_ids:
            diagnostics.append(_diagnostic(
                CONTRADICTION, 'duplicate_snapshot_id',
                'share %r lists snapshot id %r more than once' % (listing.name, member.snapshot_id),
                share=listing.name,
            ))
            return None, diagnostics
        candidate = by_id.get(member.snapshot_id)
        if candidate is None:
            diagnostics.append(_diagnostic(
                MISSING_CORRESPONDENCE, 'snapshot_not_listed',
                'share %r does not list snapshot id %r' % (listing.name, member.snapshot_id),
                share=listing.name,
            ))
            return None, diagnostics
        if same_time_conflict(candidate):
            return None, diagnostics
        return candidate, diagnostics
    if member.timestamp is not None:
        candidates = by_time.get(member.timestamp, ())
        if not candidates:
            diagnostics.append(_diagnostic(
                MISSING_CORRESPONDENCE, 'snapshot_time_not_listed',
                'share %r does not list a copy at %s'
                % (listing.name, member.timestamp.isoformat()),
                share=listing.name,
                details={'timestamp': member.timestamp},
            ))
            return None, diagnostics
        if len(candidates) > 1:
            diagnostics.append(_diagnostic(
                CONTRADICTION, 'ambiguous_snapshot_time',
                'share %r lists %d copies at %s'
                % (listing.name, len(candidates), member.timestamp.isoformat()),
                share=listing.name,
                details={'snapshot_ids': [item.identity for item in candidates]},
            ))
            return None, diagnostics
        return candidates[0], diagnostics
    diagnostics.append(_diagnostic(
        MISSING_CORRESPONDENCE, 'member_reference_missing',
        'group has no snapshot id or timestamp for share %r' % (listing.name,),
        share=listing.name,
    ))
    return None, diagnostics


def _probe_result(result: Any) -> str:
    if isinstance(result, Mapping) and 'exists' in result:
        return 'present' if result.get('exists') else 'missing'
    if hasattr(result, 'exists') and not isinstance(result, (str, bytes)):
        return 'present' if bool(getattr(result, 'exists')) else 'missing'
    if result is None:
        return 'unverified'
    return 'present' if bool(result) else 'missing'


def _check_required_file(required: RequiredFile, snapshot: Snapshot,
                         probe: Optional[Callable[[str, Snapshot, str], Any]]) -> Tuple[str, Any]:
    if probe is not None:
        if isinstance(probe, Mapping):
            keys = (
                (required.share, snapshot.identity, required.path),
                (_share_key(required.share), snapshot.identity, _path_key(required.path)),
                (required.share, required.path),
                (_share_key(required.share), _path_key(required.path)),
                required.path,
                _path_key(required.path),
            )
            found = False
            result = None
            for key in keys:
                if key in probe:
                    found = True
                    result = probe[key]
                    break
            if not found:
                return 'unverified', None
            return _probe_result(result), result
        try:
            result = probe(required.share, snapshot, required.path)
        except FileNotFoundError:
            return 'missing', None
        except NotADirectoryError:
            return 'missing', None
        except OperationFailure as ex:
            status = None
            if getattr(ex, 'smb_messages', None):
                status = getattr(ex.smb_messages[-1].status, 'internal_value', None)
            if status in _NOT_FOUND_STATUSES:
                return 'missing', ex
            return 'unverified', ex
        return _probe_result(result), result
    known = snapshot.has_file(required.path, is_directory=required.is_directory)
    if known is None:
        return 'unverified', None
    return ('present' if known else 'missing'), known


def find_restore_sets(shares: Any, backup_groups: Any,
                      window: Any = None,
                      required_files: Any = None,
                      probe: Optional[Callable[[str, Snapshot, str], Any]] = None) -> RestoreReport:
    """Find every evidence-complete common restore set.

    :param shares: Mapping of share name to snapshots, or an iterable of
        :class:`ShareSnapshots` / ``(name, snapshots)`` pairs.
    :param backup_groups: A sequence of :class:`BackupGroup` objects or a
        mapping of group id to group specification.  Only these declarations
        establish that copies on different shares belong to the same backup;
        equal timestamps are deliberately not used.
    :param window: Inclusive :class:`TimeWindow`, ``(start, end)``, or mapping.
    :param required_files: Required paths as :class:`RequiredFile` objects,
        ``(share, path[, is_directory])`` tuples, or a mapping of share to
        path(s).
    :param probe: Optional callback ``probe(share, snapshot, path)`` returning
        truthy/falsy, a mapping with ``exists``, or raising
        :class:`FileNotFoundError`.  It must address the supplied snapshot;
        :class:`SMBFileProbe` implements this over a connection.
    :return: :class:`RestoreReport` with all complete sets and diagnostics.
    """
    listings, diagnostics = _coerce_shares(shares)
    window_obj = _coerce_window(window)
    required = _coerce_required_files(required_files)

    if not window_obj.valid:
        diagnostics.append(RestoreDiagnostic(
            CONTRADICTION, 'invalid_window', 'allowed time window starts after it ends',
            details=window_obj.as_dict(),
        ))

    groups, group_diagnostics = _coerce_groups(backup_groups)
    diagnostics.extend(group_diagnostics)

    listing_by_key = dict((_share_key(item.name), item) for item in listings)
    scope_keys = tuple(listing_by_key)
    global_invalid = any(
        item.reason in ('duplicate_share', 'duplicate_group_id', 'group_id_conflict')
        for item in diagnostics
    )
    required_by_share = {}
    for item in required:
        key = _share_key(item.share)
        if key not in listing_by_key:
            diagnostics.append(RestoreDiagnostic(
                CONTRADICTION, 'required_file_share_unknown',
                'required file %r refers to share %r, which is not in the restore scope'
                % (item.path, item.share),
                share=item.share, path=item.path,
            ))
            global_invalid = True
            continue
        required_by_share.setdefault(key, []).append(item)

    for listing in listings:
        _, _, duplicate_ids = _listing_maps(listing)
        for snapshot_id in sorted(duplicate_ids):
            diagnostics.append(RestoreDiagnostic(
                CONTRADICTION, 'duplicate_snapshot_id',
                'share %r lists snapshot id %r more than once' % (listing.name, snapshot_id),
                share=listing.name, snapshot_id=snapshot_id,
            ))

    if not window_obj.valid:
        return RestoreReport(listings, (), diagnostics, window_obj, required)

    # Resolve each group's correspondence first.  Cross-group declarations
    # are checked before any set is emitted.
    resolutions = {}
    group_states = {}
    for group in groups:
        state = {'invalid': False, 'incomplete': False, 'window_ok': True,
                 'resolved': {}, 'evidence': {}, 'diagnostics': []}
        group_states[group.id] = state
        if not group.trusted:
            state['diagnostics'].append(_diagnostic(
                UNTRUSTED_GROUP, 'group_not_trusted',
                'backup group %r is not marked trusted' % group.id,
                group=group,
            ))
            state['incomplete'] = True
            resolutions[group.id] = state['resolved']
            continue

        if group.time is not None and not window_obj.contains(group.time):
            state['diagnostics'].append(_diagnostic(
                OUTSIDE_WINDOW, 'group_time_outside_window',
                'backup group %r time %s is outside the allowed window'
                % (group.id, group.time.isoformat()),
                group=group,
                details={'window': window_obj.as_dict()},
            ))
            state['window_ok'] = False

        member_by_share = {}
        for member in group.members:
            key = _share_key(member.share)
            if key in member_by_share:
                state['diagnostics'].append(_diagnostic(
                    CONTRADICTION, 'duplicate_group_member',
                    'backup group %r names share %r more than once' % (group.id, member.share),
                    group=group, share=member.share,
                ))
                state['invalid'] = True
            elif key not in listing_by_key:
                # A trusted backup may cover more shares than this collection.
                # Extra members are not part of the restore scope.
                state['diagnostics'].append(_diagnostic(
                    OUT_OF_SCOPE, 'member_share_out_of_scope',
                    'backup group %r also names share %r, outside this restore scope'
                    % (group.id, member.share),
                    group=group, share=member.share,
                ))
            else:
                member_by_share[key] = member

        for key in scope_keys:
            listing = listing_by_key[key]
            member = member_by_share.get(key)
            if member is None:
                state['diagnostics'].append(_diagnostic(
                    MISSING_CORRESPONDENCE, 'member_missing',
                    'backup group %r has no correspondence for share %r' % (group.id, listing.name),
                    group=group, share=listing.name,
                ))
                state['incomplete'] = True
                continue
            evidence = _group_evidence(group, member)
            if evidence is None:
                state['diagnostics'].append(_diagnostic(
                    MISSING_CORRESPONDENCE, 'evidence_missing',
                    'backup group %r has no evidence for share %r' % (group.id, listing.name),
                    group=group, share=listing.name,
                ))
                state['incomplete'] = True
                continue
            snapshot, member_diagnostics = _resolve_member(member, listing)
            state['diagnostics'].extend(member_diagnostics)
            if snapshot is None:
                state['incomplete'] = True
                continue
            if snapshot.declared_group is not None and snapshot.declared_group != group.id:
                state['diagnostics'].append(_diagnostic(
                    CONTRADICTION, 'snapshot_group_conflict',
                    'share %r snapshot %r declares group %r, not %r'
                    % (listing.name, snapshot.identity, snapshot.declared_group, group.id),
                    group=group, share=listing.name, snapshot=snapshot,
                    details={'declared_group': snapshot.declared_group},
                ))
                state['invalid'] = True
            if not window_obj.contains(snapshot.timestamp):
                state['diagnostics'].append(_diagnostic(
                    OUTSIDE_WINDOW, 'snapshot_outside_window',
                    'share %r snapshot %r at %s is outside the allowed window'
                    % (listing.name, snapshot.identity, snapshot.timestamp.isoformat()),
                    group=group, share=listing.name, snapshot=snapshot,
                    details={'window': window_obj.as_dict()},
                ))
                state['window_ok'] = False
            state['resolved'][key] = snapshot
            state['evidence'][key] = evidence
        resolutions[group.id] = state['resolved']

    # A snapshot cannot belong to two different trusted backup groups.  Such
    # an overlap makes both declarations contradictory instead of silently
    # producing two "complete" sets from one copy.
    claims = {}
    for group in groups:
        if group.id not in group_states:
            continue
        for key, snapshot in resolutions.get(group.id, {}).items():
            claims.setdefault((key, snapshot.identity), []).append(group.id)
    for (key, snapshot_id), group_ids in claims.items():
        unique_ids = sorted(set(group_ids))
        if len(unique_ids) < 2:
            continue
        share_name = listing_by_key[key].name
        for group_id in unique_ids:
            group = _group_by_id(groups, group_id)
            snapshot = resolutions[group_id][key]
            group_states[group_id]['diagnostics'].append(_diagnostic(
                CONTRADICTION, 'snapshot_claimed_by_multiple_groups',
                'share %r snapshot %r is claimed by backup groups %s'
                % (share_name, snapshot_id, ', '.join(unique_ids)),
                group=group, share=share_name, snapshot=snapshot,
                details={'groups': unique_ids},
            ))
            group_states[group_id]['invalid'] = True

    restore_sets = []
    reported_diagnostics = set()
    def report_diagnostic(diagnostic):
        marker = (diagnostic.kind, diagnostic.reason, diagnostic.message,
                  diagnostic.group_id, diagnostic.share, diagnostic.snapshot_id,
                  diagnostic.path)
        if marker not in reported_diagnostics:
            reported_diagnostics.add(marker)
            diagnostics.append(diagnostic)

    for group in groups:
        state = group_states[group.id]
        if global_invalid:
            state['invalid'] = True
        for diagnostic in state['diagnostics']:
            report_diagnostic(diagnostic)
        if state['invalid'] or state['incomplete'] or not state['window_ok']:
            continue
        if any(key not in state['resolved'] for key in scope_keys):
            continue

        members = []
        for key in scope_keys:
            listing = listing_by_key[key]
            snapshot = state['resolved'][key]
            members.append(RestoreMember(listing.name, snapshot, state['evidence'][key]))

        for key in scope_keys:
            listing = listing_by_key[key]
            snapshot = state['resolved'][key]
            for required_file in required_by_share.get(key, ()):
                outcome, detail = _check_required_file(required_file, snapshot, probe)
                if outcome == 'present':
                    continue
                if outcome == 'missing':
                    state['diagnostics'].append(_diagnostic(
                        MISSING_FILE, 'required_file_missing',
                        'required path %r is missing from share %r snapshot %r'
                        % (required_file.path, listing.name, snapshot.identity),
                        group=group, share=listing.name, snapshot=snapshot,
                        path=required_file.path,
                        details={'is_directory': required_file.is_directory},
                    ))
                else:
                    state['diagnostics'].append(_diagnostic(
                        MISSING_CORRESPONDENCE, 'required_file_unverified',
                        'required path %r on share %r snapshot %r could not be verified'
                        % (required_file.path, listing.name, snapshot.identity),
                        group=group, share=listing.name, snapshot=snapshot,
                        path=required_file.path,
                        details={'error': str(detail) if detail is not None else None},
                    ))
                state['incomplete'] = True
                report_diagnostic(state['diagnostics'][-1])

        if state['incomplete'] or state['invalid'] or not state['window_ok']:
            continue
        restore_sets.append(RestoreSet(
            group_id=group.id,
            members=tuple(members),
            group_time=group.time,
            evidence=group.evidence,
        ))

    if not groups:
        diagnostics.append(RestoreDiagnostic(
            MISSING_CORRESPONDENCE, 'no_backup_groups',
            'no trusted backup-group correspondence was supplied',
        ))

    return RestoreReport(listings, restore_sets, diagnostics, window_obj, required)


def plan_restore(shares: Any, backup_groups: Any, window: Any = None,
                 required_files: Any = None,
                 probe: Optional[Callable[[str, Snapshot, str], Any]] = None) -> RestoreReport:
    """Alias for :func:`find_restore_sets`."""
    return find_restore_sets(shares, backup_groups, window, required_files, probe)


#: Descriptive aliases.  They all mean the same evidence-complete search.
find_common_restore_sets = find_restore_sets
find_restore_groups = find_restore_sets
common_restore_sets = find_restore_sets


class RestorePlanner:
    """Object-oriented wrapper around :func:`find_restore_sets`."""

    def __init__(self, shares: Any, backup_groups: Any, window: Any = None,
                 required_files: Any = None,
                 probe: Optional[Callable[[str, Snapshot, str], Any]] = None):
        self.shares = shares
        self.backup_groups = backup_groups
        self.window = window
        self.required_files = required_files
        self.probe = probe

    def plan(self) -> RestoreReport:
        return find_restore_sets(self.shares, self.backup_groups, self.window,
                                 self.required_files, self.probe)

    find = plan
    find_restore_sets = plan


def _coerce_groups(backup_groups: Any) -> Tuple[List[BackupGroup], List[RestoreDiagnostic]]:
    diagnostics = []
    result = []
    seen = set()
    if backup_groups is None:
        return result, diagnostics
    if isinstance(backup_groups, BackupGroup):
        backup_groups = [backup_groups]
    if isinstance(backup_groups, Mapping) and (
            'id' in backup_groups or 'group_id' in backup_groups):
        backup_groups = [backup_groups]
    if isinstance(backup_groups, Mapping):
        source = []
        for group_id, value in backup_groups.items():
            if isinstance(value, BackupGroup):
                if value.id != group_id:
                    diagnostics.append(RestoreDiagnostic(
                        CONTRADICTION, 'group_id_conflict',
                        'mapping key %r and group id %r disagree' % (group_id, value.id),
                        group_id=str(group_id),
                    ))
                source.append(value)
            elif isinstance(value, Mapping):
                data = dict(value)
                data.setdefault('id', group_id)
                if 'members' not in data:
                    data['members'] = data.pop(
                        'shares', data.pop('copies', data.pop('correspondence', ())))
                if 'time' not in data:
                    group_time = _pop_first(data, ('group_time', 'backup_time', 'date'))
                    if group_time is not None:
                        data['time'] = group_time
                source.append(BackupGroup(**data))
            else:
                source.append(BackupGroup(str(group_id), value))
    else:
        source = list(backup_groups)
        for index, value in enumerate(source):
            if not isinstance(value, BackupGroup):
                if isinstance(value, Mapping):
                    data = dict(value)
                    if 'id' not in data:
                        data['id'] = data.pop('group_id', data.pop('name', None))
                    if data.get('id') is None:
                        raise TypeError('backup group mapping needs an id')
                    if 'members' not in data:
                        data['members'] = data.pop(
                            'shares', data.pop('copies', data.pop('correspondence', ())))
                    if 'time' not in data:
                        group_time = _pop_first(data, ('group_time', 'backup_time', 'date'))
                        if group_time is not None:
                            data['time'] = group_time
                    source[index] = BackupGroup(**data)
                else:
                    raise TypeError('cannot interpret backup group: %r' % (value,))

    for group in source:
        if group.id in seen:
            diagnostics.append(RestoreDiagnostic(
                CONTRADICTION, 'duplicate_group_id',
                'backup group id %r is declared more than once' % group.id,
                group_id=group.id,
            ))
            continue
        seen.add(group.id)
        result.append(group)
    return result, diagnostics


def _group_by_id(groups: Sequence[BackupGroup], group_id: str) -> BackupGroup:
    for group in groups:
        if group.id == group_id:
            return group
    raise KeyError(group_id)


class SMBFileProbe:
    """Adapt an SMB connection to the planner's ``probe`` callback.

    The callback receives the logical path and always addresses the supplied
    snapshot through its ``@GMT-...`` directory.  It never reads the live
    share.
    """

    def __init__(self, connection: Any):
        self.connection = connection

    def __call__(self, share: str, snapshot: Snapshot, path: str) -> bool:
        pinned = build_snapshot_path(snapshot, path)
        clean = str(path).replace('\\', '/')
        is_directory = clean.rstrip('/') == '' or clean.endswith('/')
        try:
            info = self.connection.getAttributes(share, pinned)
        except (FileNotFoundError, NotADirectoryError):
            return False
        except OperationFailure as ex:
            status = None
            if getattr(ex, 'smb_messages', None):
                status = getattr(ex.smb_messages[-1].status, 'internal_value', None)
            if status in _NOT_FOUND_STATUSES:
                return False
            raise
        if info is None:
            return False
        actual_directory = bool(getattr(info, 'isDirectory', False))
        return actual_directory if is_directory else not actual_directory


class PinnedRestore:
    """Read directories and files only from a selected restore set.

    Every method prefixes the logical path with the selected copy's
    ``@GMT-...`` directory.  There is intentionally no ``latest`` or live-share
    fallback.  If a member is marked unavailable, the whole set is revoked
    because a common restore needs every share.
    """

    READ_METHODS = ('listPath', 'getAttributes', 'retrieveFile',
                    'retrieveFileFromOffset')

    def __init__(self, restore_set: RestoreSet, connection: Any,
                 report: Optional[RestoreReport] = None):
        if not isinstance(restore_set, RestoreSet):
            raise TypeError('restore_set must be a RestoreSet')
        if not restore_set.complete:
            raise RestoreSetRevokedError(
                'restore set %r is revoked: %s'
                % (restore_set.group_id, restore_set.revocation_reason)
            )
        self.restore_set = restore_set
        self.connection = connection
        self.report = report
        self._members = dict((member.share_key, member) for member in restore_set.members)
        self._unavailable = {}

    @classmethod
    def from_report(cls, report: RestoreReport, connection: Any,
                    group_id: Optional[str] = None) -> 'PinnedRestore':
        """Select one complete set; fail rather than fall back to live paths."""
        restore_set = report.select_set(group_id)
        return cls(restore_set, connection, report)

    @property
    def group_id(self) -> str:
        return self.restore_set.group_id

    @property
    def shares(self) -> Tuple[str, ...]:
        return self.restore_set.shares

    @property
    def available(self) -> bool:
        return self.restore_set.complete and not self._unavailable

    def snapshot_for(self, share: str) -> Snapshot:
        return self._member(share).snapshot

    def pinned_path(self, share: str, path: Any = '/') -> str:
        return self._member(share).pinned_path(path)

    def _member(self, share: str) -> RestoreMember:
        try:
            key = _share_key(share)
        except ValueError:
            raise ShareNotInRestoreSetError('invalid share name: %r' % (share,))
        if key not in self._members:
            raise ShareNotInRestoreSetError(
                'share %r is not in restore set %r'
                % (share, self.restore_set.group_id)
            )
        member = self._members[key]
        if key in self._unavailable:
            raise SnapshotUnavailableError(
                'share %r snapshot %r is unavailable: %s'
                % (member.share, member.snapshot_id, self._unavailable[key])
            )
        if not self.restore_set.complete:
            raise RestoreSetRevokedError(
                'restore set %r is revoked: %s'
                % (self.restore_set.group_id, self.restore_set.revocation_reason)
            )
        return member

    def mark_unavailable(self, share: Optional[str] = None,
                         snapshot_id: Optional[str] = None,
                         timestamp: Any = None,
                         reason: str = 'snapshot is no longer available') -> Tuple[RestoreSet, ...]:
        """Mark a member unavailable and revoke only dependent conclusions.

        If ``share`` is omitted, all members of this set are marked.  A report
        attached by :meth:`from_report` is updated as well, so other sets and
        unrelated diagnostics remain valid.
        """
        if share is not None:
            key = _share_key(share)
            if key not in self._members:
                raise ShareNotInRestoreSetError(
                    'share %r is not in restore set %r'
                    % (share, self.restore_set.group_id))
            member = self._members[key]
            targets = [member]
            if snapshot_id is not None and member.snapshot_id != str(snapshot_id):
                raise SnapshotNotFoundError(
                    'snapshot %r is not selected for share %r' % (snapshot_id, share))
            if (timestamp is not None and
                    member.snapshot.timestamp != _coerce_datetime(timestamp)):
                raise SnapshotNotFoundError(
                    'timestamp does not match the selected copy for share %r' % (share,))
        else:
            targets = list(self._members.values())
        for member in targets:
            self._unavailable[member.share_key] = reason
        revoked = ()
        if self.report is not None:
            for member in targets:
                revoked = revoked + self.report.invalidate_snapshot(
                    share=member.share, snapshot=member.snapshot, reason=reason
                )
        else:
            self.restore_set.status = 'revoked'
            self.restore_set.revocation_reason = reason
            revoked = (self.restore_set,)
        return revoked

    def listPath(self, share: str, path: Any = '/', **kwargs):
        member = self._member(share)
        return self.connection.listPath(member.share, member.pinned_path(path), **kwargs)

    def getAttributes(self, share: str, path: Any, **kwargs):
        member = self._member(share)
        return self.connection.getAttributes(member.share, member.pinned_path(path), **kwargs)

    def retrieveFile(self, share: str, path: Any, file_obj: Any, **kwargs):
        member = self._member(share)
        return self.connection.retrieveFile(
            member.share, member.pinned_path(path), file_obj, **kwargs
        )

    def retrieveFileFromOffset(self, share: str, path: Any, file_obj: Any,
                               *args, **kwargs):
        member = self._member(share)
        return self.connection.retrieveFileFromOffset(
            member.share, member.pinned_path(path), file_obj, *args, **kwargs
        )


#: Descriptive alias for callers that think in sessions rather than restores.
PinnedRestoreSession = PinnedRestore


def load_manifest(source: Any) -> dict:
    """Load a JSON restore manifest from a path, text stream, bytes, or mapping.

    The manifest is a dictionary with ``shares``, ``backup_groups``, and
    optional ``window`` and ``required_files`` keys.  The same shapes accepted
    by :func:`find_restore_sets` may be used.
    """
    import json
    import os

    if isinstance(source, Mapping):
        data = dict(source)
    elif isinstance(source, bytes):
        data = json.loads(source.decode('utf-8'))
    elif hasattr(source, 'read'):
        data = json.load(source)
    elif isinstance(source, os.PathLike) or isinstance(source, str):
        with open(source, 'r', encoding='utf-8') as stream:
            data = json.load(stream)
    else:
        raise TypeError('manifest must be a mapping, JSON text, stream, or path')
    allowed = {'shares', 'backup_groups', 'window', 'required_files', 'groups'}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError('unknown manifest fields: %s' % sorted(unknown))
    if 'backup_groups' not in data:
        data['backup_groups'] = data.pop('groups', data.pop('backup_groups_key', []))
    return data


def plan_from_manifest(source: Any,
                       probe: Optional[Callable[[str, Snapshot, str], Any]] = None
                       ) -> RestoreReport:
    """Load a manifest and run :func:`find_restore_sets` on it."""
    manifest = load_manifest(source)
    return find_restore_sets(
        manifest.get('shares', {}),
        manifest.get('backup_groups', []),
        manifest.get('window'),
        manifest.get('required_files'),
        probe,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Command-line entry point: print the structured report for a manifest."""
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        prog='python -m smb.restore',
        description='Find evidence-complete restore sets from a JSON manifest.',
    )
    parser.add_argument('manifest', help='path to the JSON restore manifest')
    parser.add_argument('--group', help='selected backup group id to verify')
    args = parser.parse_args(argv)

    try:
        report = plan_from_manifest(args.manifest)
    except (OSError, ValueError, TypeError) as ex:
        sys.stderr.write('cannot read manifest: %s\n' % ex)
        return 1
    result = report.as_dict()
    result['summary'] = report.summary()
    if args.group:
        try:
            report.select_set(args.group)
            result['selection'] = {'group_id': args.group, 'status': 'complete'}
        except RestoreError as ex:
            result['selection'] = {'group_id': args.group, 'status': 'unavailable',
                                   'reason': str(ex)}
    json.dump(result, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write('\n')
    return 0 if report.has_complete_set else 2


if __name__ == '__main__':
    raise SystemExit(main())
