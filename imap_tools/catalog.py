"""
Durable mailbox folder and message occurrence catalog.

IMAP folder names are not stable identities:

* a folder may be renamed, and its messages remain the same messages;
* a folder may be deleted and later recreated under the same name, which is a
  new mailbox generation and must not inherit the old folder's history;
* a message moved to another folder keeps its old occurrence in the archive
  even though the source copy no longer exists on the server.

This module keeps that history explicitly.  Reconciliation never relies on
folder-name similarity and never matches moved messages by subject, sender,
Message-ID or body equality.  The only accepted move correspondence is an
explicit source UID -> destination UID mapping, normally parsed from the
``COPYUID`` response code of ``UID COPY``/``UID MOVE`` (RFC 4315).

The catalog is server scoped: the same folder name and UIDVALIDITY on two
accounts, or on the same account reached through two host names, are different
identities.  Pass one canonical :class:`ServerIdentity` per mailbox account.

Folder reconciliation rules
---------------------------

A folder generation is identified by ``(server identity, UIDVALIDITY)``.
UIDVALIDITY values are only unique inside one mailbox, so a snapshot may
legitimately contain the same value in several folders.  Additional evidence is
therefore used before a rename is accepted:

* equal UIDNEXT is the strongest practical continuity signal;
* overlap of observed UID sets also supports continuity;
* a lower UIDNEXT than the recorded one contradicts continuity, so the
  observation is not applied to that generation and is reported;
* when UIDVALIDITY is shared by several folders and neither UIDNEXT nor UID
  evidence distinguishes a single candidate, the observation is reported as an
  ambiguity and kept as a separate generation instead of being guessed to be a
  rename.

Deletion and recreation are detected by the same name appearing with a
different UIDVALIDITY.  The old record is marked ``superseded`` but is never
removed, and its message occurrences remain in the catalog.  A folder that is
absent from a snapshot is marked ``absent``; it is not deleted either.

Message occurrences
-------------------

An occurrence is ``(folder generation, UID)``.  A recorded move marks the old
occurrence ``moved`` and links it to the new occurrence; both remain queryable.
A later snapshot that no longer contains the old folder cannot remove either
side of that link.  Content-based matching is deliberately absent from the API.
"""

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

CATALOG_FORMAT = 'imap_tools.catalog'
CATALOG_VERSION = 1

FOLDER_ACTIVE = 'active'
FOLDER_ABSENT = 'absent'
FOLDER_SUPERSEDED = 'superseded'

OCCURRENCE_PRESENT = 'present'
OCCURRENCE_UNOBSERVED = 'unobserved'
OCCURRENCE_MOVED = 'moved'
OCCURRENCE_DELETED = 'deleted'

_FOLDER_STATUSES = (FOLDER_ACTIVE, FOLDER_ABSENT, FOLDER_SUPERSEDED)
_OCCURRENCE_STATUSES = (
    OCCURRENCE_PRESENT,
    OCCURRENCE_UNOBSERVED,
    OCCURRENCE_MOVED,
    OCCURRENCE_DELETED,
)

_UID_RE = re.compile(r'^[0-9]+$')
_UID_SET_RE = re.compile(r'^[0-9*:,]+$')
_COPYUID_RE = re.compile(
    r'\[COPYUID[ \t]+(?P<uidvalidity>[0-9]+)[ \t]+'
    r'(?P<source>[0-9*:,]+)[ \t]+(?P<destination>[0-9*:,]+)\]',
    re.IGNORECASE,
)


class CatalogError(Exception):
    """Base catalog error"""


class UnknownFolderError(CatalogError):
    """Folder generation is not present in the catalog"""


class MoveEvidenceError(CatalogError):
    """Move evidence is malformed or contradicts the catalog"""


def _coerce_time(value: Optional[Union[datetime, str]] = None) -> datetime:
    """Return a timezone-aware UTC datetime"""
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    else:
        parsed = value
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _time_to_json(value: Optional[datetime]) -> Optional[str]:
    return None if value is None else _coerce_time(value).isoformat()


def _time_from_json(value: Optional[str]) -> Optional[datetime]:
    return None if value is None else _coerce_time(value)


def _uid_tuple(uids: Optional[Iterable[str]]) -> Optional[Tuple[str, ...]]:
    if uids is None:
        return None
    result = []
    for uid in uids:
        if not isinstance(uid, str) or not _UID_RE.match(uid):
            raise ValueError(f'UID must be a decimal string, got {uid!r}')
        result.append(uid)
    return tuple(result)


@dataclass(frozen=True)
class ServerIdentity:
    """
    Canonical identity of one mailbox account.

    ``host`` and ``username`` are required.  ``port`` is optional and only
    needed when the same host serves different accounts on non-standard ports.
    Two identities with different values are never merged in the catalog.
    """

    host: str
    username: str
    port: Optional[int] = None

    def __post_init__(self):
        if not isinstance(self.host, str) or not self.host:
            raise ValueError('ServerIdentity.host must be a non-empty string')
        if not isinstance(self.username, str) or not self.username:
            raise ValueError('ServerIdentity.username must be a non-empty string')
        if self.port is not None and (not isinstance(self.port, int) or self.port < 1):
            raise ValueError('ServerIdentity.port must be a positive integer')

    @property
    def key(self) -> str:
        """Stable key used in folder identifiers"""
        port = '' if self.port is None else f':{self.port}'
        return f'{self.username}@{self.host}{port}'

    def to_dict(self) -> Dict[str, Any]:
        return {'host': self.host, 'username': self.username, 'port': self.port}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ServerIdentity':
        return cls(
            host=data['host'],
            username=data['username'],
            port=data.get('port'),
        )


def _status_value(status: Dict[str, Any], key: str) -> Optional[int]:
    for candidate, value in status.items():
        if str(candidate).upper() == key:
            return None if value is None else int(value)
    return None


@dataclass(frozen=True)
class FolderObservation:
    """
    One folder as seen on the server at one point in time.

    :param name: current folder name
    :param uidvalidity: IMAP UIDVALIDITY value (generation evidence)
    :param uidnext: IMAP UIDNEXT value, when available
    :param uids: complete observed UID set, when available
    :param message_count: IMAP MESSAGES value, when available

    ``uids`` should only be supplied when the snapshot contains the complete
    UID set.  Partial UID samples would make continuity checks unreliable.
    """

    name: str
    uidvalidity: int
    uidnext: Optional[int] = None
    uids: Optional[Tuple[str, ...]] = None
    message_count: Optional[int] = None

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name:
            raise ValueError('FolderObservation.name must be a non-empty string')
        if not isinstance(self.uidvalidity, int) or self.uidvalidity < 0:
            raise ValueError('FolderObservation.uidvalidity must be a non-negative integer')
        if self.uidnext is not None and (not isinstance(self.uidnext, int) or self.uidnext < 0):
            raise ValueError('FolderObservation.uidnext must be a non-negative integer')
        if self.message_count is not None and (not isinstance(self.message_count, int) or self.message_count < 0):
            raise ValueError('FolderObservation.message_count must be a non-negative integer')
        object.__setattr__(self, 'uids', _uid_tuple(self.uids))

    @classmethod
    def from_status(
        cls, name: str, status: Dict[str, Any], uids: Optional[Iterable[str]] = None
    ) -> 'FolderObservation':
        """
        Build an observation from ``MailBoxFolderManager.status`` output.

        Example: ``FolderObservation.from_status('INBOX', mailbox.folder.status('INBOX'))``
        """
        uidvalidity = _status_value(status, 'UIDVALIDITY')
        if uidvalidity is None:
            raise ValueError('status has no UIDVALIDITY value; generation is unknown')
        return cls(
            name=name,
            uidvalidity=uidvalidity,
            uidnext=_status_value(status, 'UIDNEXT'),
            uids=None if uids is None else tuple(uids),
            message_count=_status_value(status, 'MESSAGES'),
        )


@dataclass
class FolderRecord:
    """Durable record of one folder generation"""

    folder_id: str
    server: ServerIdentity
    uidvalidity: int
    generation: int
    name: str
    status: str
    first_seen: datetime
    last_seen: datetime
    uidnext: Optional[int] = None
    uids: Optional[Tuple[str, ...]] = None
    message_count: Optional[int] = None
    previous_names: List[str] = field(default_factory=list)
    absent_since: Optional[datetime] = None
    superseded_by: Optional[str] = None
    superseded_at: Optional[datetime] = None

    def __post_init__(self):
        if self.status not in _FOLDER_STATUSES:
            raise ValueError(f'Unknown folder status: {self.status!r}')
        if not isinstance(self.uidvalidity, int) or self.uidvalidity < 0:
            raise ValueError('FolderRecord.uidvalidity must be a non-negative integer')

    @property
    def current_name(self) -> str:
        """Name in the newest observation"""
        return self.name

    @property
    def all_names(self) -> Tuple[str, ...]:
        """Every name this generation has had, oldest first"""
        return tuple(self.previous_names + [self.name])

    def to_dict(self) -> Dict[str, Any]:
        return {
            'folder_id': self.folder_id,
            'server': self.server.to_dict(),
            'uidvalidity': self.uidvalidity,
            'generation': self.generation,
            'name': self.name,
            'status': self.status,
            'first_seen': _time_to_json(self.first_seen),
            'last_seen': _time_to_json(self.last_seen),
            'uidnext': self.uidnext,
            'uids': None if self.uids is None else list(self.uids),
            'message_count': self.message_count,
            'previous_names': list(self.previous_names),
            'absent_since': _time_to_json(self.absent_since),
            'superseded_by': self.superseded_by,
            'superseded_at': _time_to_json(self.superseded_at),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'FolderRecord':
        uids = data.get('uids')
        return cls(
            folder_id=data['folder_id'],
            server=ServerIdentity.from_dict(data['server']),
            uidvalidity=data['uidvalidity'],
            generation=data['generation'],
            name=data['name'],
            status=data['status'],
            first_seen=_time_from_json(data['first_seen']),
            last_seen=_time_from_json(data['last_seen']),
            uidnext=data.get('uidnext'),
            uids=None if uids is None else tuple(uids),
            message_count=data.get('message_count'),
            previous_names=list(data.get('previous_names', [])),
            absent_since=_time_from_json(data.get('absent_since')),
            superseded_by=data.get('superseded_by'),
            superseded_at=_time_from_json(data.get('superseded_at')),
        )


@dataclass
class MessageOccurrence:
    """
    One message occurrence in one folder generation.

    The record is never deleted automatically.  When the message is moved, the
    old occurrence becomes ``moved`` and points at the new occurrence through
    ``moved_to``; the new occurrence points back through ``moved_from``.
    """

    folder_id: str
    uid: str
    status: str
    first_seen: datetime
    last_seen: datetime
    moved_to: Optional[str] = None
    moved_from: Optional[str] = None
    unobserved_since: Optional[datetime] = None

    def __post_init__(self):
        if not _UID_RE.match(self.uid):
            raise ValueError(f'UID must be a decimal string, got {self.uid!r}')
        if self.status not in _OCCURRENCE_STATUSES:
            raise ValueError(f'Unknown occurrence status: {self.status!r}')

    @property
    def occurrence_id(self) -> str:
        return f'{self.folder_id}/{self.uid}'

    def to_dict(self) -> Dict[str, Any]:
        return {
            'folder_id': self.folder_id,
            'uid': self.uid,
            'status': self.status,
            'first_seen': _time_to_json(self.first_seen),
            'last_seen': _time_to_json(self.last_seen),
            'moved_to': self.moved_to,
            'moved_from': self.moved_from,
            'unobserved_since': _time_to_json(self.unobserved_since),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'MessageOccurrence':
        return cls(
            folder_id=data['folder_id'],
            uid=data['uid'],
            status=data['status'],
            first_seen=_time_from_json(data['first_seen']),
            last_seen=_time_from_json(data['last_seen']),
            moved_to=data.get('moved_to'),
            moved_from=data.get('moved_from'),
            unobserved_since=_time_from_json(data.get('unobserved_since')),
        )


@dataclass(frozen=True)
class RenameEvent:
    """Accepted folder rename, including the continuity evidence"""

    server: ServerIdentity
    folder_id: str
    old_name: str
    new_name: str
    observed_at: datetime
    evidence: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            'server': self.server.to_dict(),
            'folder_id': self.folder_id,
            'old_name': self.old_name,
            'new_name': self.new_name,
            'observed_at': _time_to_json(self.observed_at),
            'evidence': self.evidence,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'RenameEvent':
        return cls(
            server=ServerIdentity.from_dict(data['server']),
            folder_id=data['folder_id'],
            old_name=data['old_name'],
            new_name=data['new_name'],
            observed_at=_time_from_json(data['observed_at']),
            evidence=data['evidence'],
        )


@dataclass(frozen=True)
class CatalogConflict:
    """A snapshot detail that could not be resolved without guessing"""

    kind: str
    message: str
    folder_ids: Tuple[str, ...] = ()
    names: Tuple[str, ...] = ()

    def __str__(self):
        details = []
        if self.folder_ids:
            details.append(f'folders={", ".join(self.folder_ids)}')
        if self.names:
            details.append(f'names={", ".join(self.names)}')
        suffix = f' ({"; ".join(details)})' if details else ''
        return f'{self.kind}: {self.message}{suffix}'


@dataclass(frozen=True)
class ReconcileReport:
    """Result of one folder snapshot reconciliation"""

    server: ServerIdentity
    observed_at: datetime
    observed_folder_ids: Tuple[str, ...] = ()
    created: Tuple[str, ...] = ()
    renamed: Tuple[RenameEvent, ...] = ()
    recreated: Tuple[Tuple[str, str], ...] = ()
    absent: Tuple[str, ...] = ()
    conflicts: Tuple[CatalogConflict, ...] = ()
    unresolved: Tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True when the whole snapshot was applied without conflicts"""
        return not self.conflicts and not self.unresolved

    @property
    def recorded_as_new(self) -> Tuple[str, ...]:
        """Observed names kept as new generations after a rename conflict"""
        renamed_names = {event.new_name for event in self.renamed}
        conflict_names = {name for conflict in self.conflicts for name in conflict.names}
        return tuple(
            name for name in sorted(conflict_names) if name not in renamed_names and name not in self.unresolved
        )


@dataclass(frozen=True)
class CopyUid:
    """One explicit source UID -> destination UID correspondence"""

    source_uid: str
    destination_uid: str

    def __post_init__(self):
        if not _UID_RE.match(self.source_uid):
            raise ValueError(f'source_uid must be a decimal string, got {self.source_uid!r}')
        if not _UID_RE.match(self.destination_uid):
            raise ValueError(f'destination_uid must be a decimal string, got {self.destination_uid!r}')

    def to_dict(self) -> Dict[str, str]:
        return {'source_uid': self.source_uid, 'destination_uid': self.destination_uid}


@dataclass(frozen=True)
class CopyUidMapping:
    """Parsed COPYUID response code"""

    uidvalidity: int
    pairs: Tuple[CopyUid, ...]
    raw: str = ''

    def __post_init__(self):
        if not isinstance(self.uidvalidity, int) or self.uidvalidity < 0:
            raise ValueError('COPYUID uidvalidity must be a non-negative integer')
        if not self.pairs:
            raise ValueError('COPYUID mapping must contain at least one UID pair')
        sources = [pair.source_uid for pair in self.pairs]
        destinations = [pair.destination_uid for pair in self.pairs]
        if len(set(sources)) != len(sources):
            raise ValueError('COPYUID source UIDs must be unique')
        if len(set(destinations)) != len(destinations):
            raise ValueError('COPYUID destination UIDs must be unique')

    def to_dict(self) -> Dict[str, Any]:
        return {
            'uidvalidity': self.uidvalidity,
            'pairs': [pair.to_dict() for pair in self.pairs],
            'raw': self.raw,
        }


@dataclass(frozen=True)
class MoveEvidence:
    """
    Explicit evidence that messages moved between two folder generations.

    Build it from :meth:`from_copyuid` or from another authoritative source
    that provides source/destination UID pairs.  There is intentionally no
    constructor that matches messages by subject, Message-ID or body content.
    """

    server: ServerIdentity
    source_folder_id: str
    destination_folder_id: str
    destination_uidvalidity: int
    pairs: Tuple[CopyUid, ...]
    origin: str = 'COPYUID'
    source_uidvalidity: Optional[int] = None

    def __post_init__(self):
        if self.source_folder_id == self.destination_folder_id:
            raise ValueError('Move source and destination folder must differ')
        if not isinstance(self.destination_uidvalidity, int) or self.destination_uidvalidity < 0:
            raise ValueError('destination_uidvalidity must be a non-negative integer')
        if not self.pairs:
            raise ValueError('Move evidence must contain at least one UID pair')
        sources = [pair.source_uid for pair in self.pairs]
        destinations = [pair.destination_uid for pair in self.pairs]
        if len(set(sources)) != len(sources):
            raise ValueError('Move evidence source UIDs must be unique')
        if len(set(destinations)) != len(destinations):
            raise ValueError('Move evidence destination UIDs must be unique')
        if self.source_uidvalidity is not None and (
            not isinstance(self.source_uidvalidity, int) or self.source_uidvalidity < 0
        ):
            raise ValueError('source_uidvalidity must be a non-negative integer')

    @classmethod
    def from_copyuid(
        cls,
        server: ServerIdentity,
        source_folder_id: str,
        destination_folder_id: str,
        mapping: CopyUidMapping,
        source_uidvalidity: Optional[int] = None,
    ) -> 'MoveEvidence':
        """Build move evidence from a parsed COPYUID response"""
        return cls(
            server=server,
            source_folder_id=source_folder_id,
            destination_folder_id=destination_folder_id,
            destination_uidvalidity=mapping.uidvalidity,
            pairs=mapping.pairs,
            origin='COPYUID',
            source_uidvalidity=source_uidvalidity,
        )


@dataclass(frozen=True)
class MoveReport:
    """Result of recording one explicit move evidence"""

    evidence: MoveEvidence
    moved: Tuple[Tuple[MessageOccurrence, MessageOccurrence], ...]
    already_applied: Tuple[Tuple[str, str], ...] = ()


def _resolve_uid_token(value: str, max_uid: Optional[int]) -> int:
    if value == '*':
        if max_uid is None:
            raise ValueError('UID set contains "*" but max_uid was not provided')
        return max_uid
    return int(value)


def expand_uid_set(value: str, max_uid: Optional[int] = None) -> List[str]:
    """
    Expand an IMAP UID set like ``1:3,7,*`` into decimal UID strings.

    :param value: IMAP sequence-set text
    :param max_uid: value used to expand ``*``
    """
    if not isinstance(value, str) or not _UID_SET_RE.match(value):
        raise ValueError(f'Invalid IMAP UID set: {value!r}')
    result = []
    for part in value.split(','):
        if ':' in part:
            start_text, end_text = part.split(':', 1)
            start = _resolve_uid_token(start_text, max_uid)
            end = _resolve_uid_token(end_text, max_uid)
            if start > end:
                raise ValueError(f'Reversed UID range: {part!r}')
            result.extend(str(uid) for uid in range(start, end + 1))
        else:
            result.append(str(_resolve_uid_token(part, max_uid)))
    return result


def parse_copyuid(
    response: Union[bytes, str, Iterable[Union[bytes, str]]],
    source_max_uid: Optional[int] = None,
    destination_max_uid: Optional[int] = None,
) -> CopyUidMapping:
    """
    Parse a COPYUID response code.

    :param response: raw response bytes/str, or an imaplib result data list.
        All items are scanned for ``[COPYUID ...]`` codes, so the result lists
        of a chunked operation can be passed together.
    :param source_max_uid: value used to expand ``*`` in the source UID set
    :param destination_max_uid: value used to expand ``*`` in the destination
        UID set
    :return: :class:`CopyUidMapping`
    :raises ValueError: when no COPYUID response is found, several distinct
        destination UIDVALIDITY values are present, or the UID sets are invalid

    The COPYUID UIDVALIDITY is the UIDVALIDITY of the destination mailbox
    (RFC 4315, section 3).  Source and destination UID sets are paired in the
    order the server lists them.
    """
    items = [response] if isinstance(response, (bytes, str)) else list(response)
    uidvalidity = None
    pairs: List[CopyUid] = []
    raw_texts = []
    for item in items:
        if isinstance(item, bytes):
            text = item.decode('ascii', 'ignore')
        elif isinstance(item, str):
            text = item
        else:
            continue
        for match in _COPYUID_RE.finditer(text):
            raw_texts.append(match.group(0))
            found_uidvalidity = int(match.group('uidvalidity'))
            if uidvalidity is None:
                uidvalidity = found_uidvalidity
            elif uidvalidity != found_uidvalidity:
                raise ValueError(
                    'COPYUID responses reference different destination '
                    f'UIDVALIDITY values: {uidvalidity} != {found_uidvalidity}'
                )
            source_set = expand_uid_set(match.group('source'), source_max_uid)
            destination_set = expand_uid_set(match.group('destination'), destination_max_uid)
            if len(source_set) != len(destination_set):
                raise ValueError(
                    'COPYUID source and destination UID sets have different sizes: '
                    f'{len(source_set)} != {len(destination_set)}'
                )
            pairs.extend(
                CopyUid(source_uid=source, destination_uid=destination)
                for source, destination in zip(source_set, destination_set)
            )
    if uidvalidity is None:
        raise ValueError('COPYUID response code was not found')
    sources = [pair.source_uid for pair in pairs]
    destinations = [pair.destination_uid for pair in pairs]
    if len(set(sources)) != len(sources):
        raise ValueError('COPYUID responses contain duplicate source UIDs')
    if len(set(destinations)) != len(destinations):
        raise ValueError('COPYUID responses contain duplicate destination UIDs')
    return CopyUidMapping(
        uidvalidity=uidvalidity,
        pairs=tuple(pairs),
        raw=' '.join(raw_texts),
    )


def _rename_score(record: FolderRecord, observation: FolderObservation) -> Tuple[Optional[int], str]:
    """
    Score continuity between a recorded generation and a renamed observation.

    ``None`` means the observation contradicts the recorded generation.
    A lower observed UIDNEXT is a contradiction: IMAP UIDNEXT never decreases
    inside one mailbox generation.
    """
    if record.uidnext is not None and observation.uidnext is not None:
        if observation.uidnext < record.uidnext:
            return None, f'UIDNEXT decreased from {record.uidnext} to {observation.uidnext}'
    if record.uidnext is not None and observation.uidnext is not None and record.uidnext == observation.uidnext:
        return 3, f'UIDVALIDITY and UIDNEXT ({record.uidnext}) match'
    if record.uids is not None and observation.uids is not None:
        overlap = set(record.uids).intersection(observation.uids)
        if overlap:
            return 3, f'UIDVALIDITY and {len(overlap)} observed UIDs match'
    if record.uidnext is not None and observation.uidnext is not None:
        return 2, (f'UIDVALIDITY matches and UIDNEXT is monotonic ({record.uidnext} -> {observation.uidnext})')
    if record.uids is not None and observation.uids is not None:
        return 1, 'UIDVALIDITY matches, observed UID sets are disjoint'
    return 1, 'UIDVALIDITY matches'


def capture_observations(mailbox, folder_filter: str = '', with_uids: bool = False) -> List[FolderObservation]:
    r"""
    Build folder observations from a live ``imap_tools`` mailbox.

    :param mailbox: BaseMailBox instance
    :param folder_filter: LIST root argument, empty for the whole tree
    :param with_uids: also read every folder's UID set.  This selects each
        folder and may be slow; UIDNEXT/UIDVALIDITY alone are enough for
        ordinary rename tracking, while UIDs improve continuity evidence when
        several folders share a UIDVALIDITY value.
    :return: list of :class:`FolderObservation`

    Folders that cannot be queried (for example \Noselect containers) are
    skipped.  The caller decides how to treat a partial snapshot before passing
    it to :meth:`Catalog.observe_folders`.
    """
    result = []
    previous_folder = mailbox.folder.get()
    try:
        for folder in mailbox.folder.list(folder_filter):
            try:
                status = mailbox.folder.status(folder.name)
            except Exception:  # noqa: BLE001, S112 - server-specific status failures
                continue
            uids = None
            if with_uids:
                mailbox.folder.set(folder.name)
                uids = tuple(mailbox.uids())
            try:
                result.append(FolderObservation.from_status(folder.name, status, uids=uids))
            except ValueError:
                continue
    finally:
        if previous_folder is not None:
            mailbox.folder.set(previous_folder)
    return result


class Catalog:
    """
    Persistent folder generation and message occurrence catalog.

    All methods are synchronous and do not talk to a server.  Feed them folder
    snapshots and explicit move evidence collected by the caller.  The catalog
    is serializable with :meth:`to_dict` / :meth:`from_dict` (or JSON helpers).
    """

    def __init__(self) -> None:
        self._folders: Dict[str, FolderRecord] = {}
        self._occurrences: Dict[str, MessageOccurrence] = {}
        self._renames: List[RenameEvent] = []

    # ------------------------------------------------------------------
    # Folder reconciliation
    # ------------------------------------------------------------------
    def observe_folders(
        self,
        server: ServerIdentity,
        observations: Iterable[FolderObservation],
        at: Optional[Union[datetime, str]] = None,
        complete: bool = True,
    ) -> ReconcileReport:
        """
        Reconcile one folder snapshot for one server identity.

        :param server: canonical mailbox identity
        :param observations: folders and their generation evidence
        :param at: observation time, now by default
        :param complete: the snapshot lists every folder on the server.  Only
            then are unobserved records marked ``absent``.  Pass ``False`` for
            a subtree or filtered listing, so the call cannot make an
            unrelated folder look absent.

        Records are never deleted.  Conflicts are returned in the report.
        Ambiguous evidence is never turned into a rename: the observation is
        recorded as a separate generation.  Only observations that contradict
        the recorded history (a superseded identity reappearing, or a UIDNEXT
        decrease) are left out of the catalog and listed in ``unresolved``.
        """
        if not isinstance(server, ServerIdentity):
            raise TypeError('server must be a ServerIdentity')
        now = _coerce_time(at)
        obs_list = list(observations)
        for observation in obs_list:
            if not isinstance(observation, FolderObservation):
                raise TypeError('observations must contain FolderObservation objects')
        self._validate_snapshot(obs_list)

        records = [record for record in self._folders.values() if record.server == server]
        records_by_name: Dict[str, List[FolderRecord]] = {}
        for record in records:
            if record.status != FOLDER_SUPERSEDED:
                records_by_name.setdefault(record.name, []).append(record)

        matched: Dict[int, str] = {}
        used_folder_ids = set()
        conflicts: List[CatalogConflict] = []
        blocked_names = set()
        created: List[str] = []
        renamed: List[RenameEvent] = []
        recreated: List[Tuple[str, str]] = []

        # Phase 1: exact identity matches (same name and same UIDVALIDITY).
        for index, observation in enumerate(obs_list):
            candidates = [
                record
                for record in records_by_name.get(observation.name, ())
                if record.uidvalidity == observation.uidvalidity and record.folder_id not in used_folder_ids
            ]
            if not candidates:
                # A superseded generation becoming visible again contradicts
                # the recorded history; leave it unresolved instead of either
                # reviving it or silently creating a third generation.
                superseded = [
                    record
                    for record in records
                    if record.status == FOLDER_SUPERSEDED
                    and record.name == observation.name
                    and record.uidvalidity == observation.uidvalidity
                ]
                if superseded:
                    conflicts.append(
                        CatalogConflict(
                            kind='superseded_identity_reappeared',
                            message=(
                                'A UIDVALIDITY recorded as superseded appeared again; '
                                'generation evidence contradicts the catalog'
                            ),
                            folder_ids=tuple(record.folder_id for record in superseded),
                            names=(observation.name,),
                        )
                    )
                    blocked_names.add(observation.name)
                continue
            if len(candidates) > 1:
                conflicts.append(
                    CatalogConflict(
                        kind='duplicate_folder_identity',
                        message='Several catalog records have the same name and UIDVALIDITY',
                        folder_ids=tuple(record.folder_id for record in candidates),
                        names=(observation.name,),
                    )
                )
                blocked_names.add(observation.name)
                continue
            record = candidates[0]
            if record.uidnext is not None and observation.uidnext is not None and observation.uidnext < record.uidnext:
                conflicts.append(
                    CatalogConflict(
                        kind='contradictory_uidnext',
                        message=(
                            f'UIDNEXT decreased from {record.uidnext} to {observation.uidnext} '
                            'inside one UIDVALIDITY; the snapshot contradicts the catalog'
                        ),
                        folder_ids=(record.folder_id,),
                        names=(observation.name,),
                    )
                )
                blocked_names.add(observation.name)
                continue
            self._apply_observation(record, observation, now)
            matched[index] = record.folder_id
            used_folder_ids.add(record.folder_id)

        # Phase 2: renames.  Pair remaining records with remaining
        # observations using UIDVALIDITY plus continuity evidence, without
        # looking at name similarity.  Mutual-best matching handles simple
        # renames and name swaps; ties are reported, never guessed.
        observed_names = {observation.name for observation in obs_list}
        observed_uidvalidity_by_name: Dict[str, int] = {
            observation.name: observation.uidvalidity for observation in obs_list
        }
        remaining_indexes = [index for index in range(len(obs_list)) if index not in matched]
        remaining_records = [
            record
            for record in records
            if record.folder_id not in used_folder_ids and record.status != FOLDER_SUPERSEDED
        ]
        pair_scores: Dict[Tuple[int, str], Tuple[int, str]] = {}
        contradictions: Dict[int, List[Tuple[FolderRecord, str]]] = {}
        for index in remaining_indexes:
            observation = obs_list[index]
            for record in remaining_records:
                if record.uidvalidity != observation.uidvalidity:
                    continue
                score, reason = _rename_score(record, observation)
                if score is None:
                    contradictions.setdefault(index, []).append((record, reason))
                    continue
                # When the record's old name still exists with the same
                # UIDVALIDITY the pair can only be a name swap.  Weak
                # UIDVALIDITY-only evidence is not enough for that, because
                # UIDVALIDITY is unique per mailbox and may legitimately repeat
                # across folders.  A same-named folder with a *different*
                # UIDVALIDITY is a recreation and does not block a rename.
                if (
                    record.name in observed_names
                    and observed_uidvalidity_by_name.get(record.name) == record.uidvalidity
                    and score < 2
                ):
                    continue
                pair_scores[(index, record.folder_id)] = (score, reason)

        best_by_observation: Dict[int, List[Tuple[int, str]]] = {}
        best_by_record: Dict[str, List[Tuple[int, int]]] = {}
        for (index, folder_id), (score, reason) in pair_scores.items():
            best_by_observation.setdefault(index, []).append((score, folder_id))
            best_by_record.setdefault(folder_id, []).append((score, index))

        uidvalidity_record_count: Dict[int, int] = {}
        for record in records:
            if record.status != FOLDER_SUPERSEDED:
                uidvalidity_record_count[record.uidvalidity] = uidvalidity_record_count.get(record.uidvalidity, 0) + 1
        uidvalidity_observation_count: Dict[int, int] = {}
        for observation in obs_list:
            uidvalidity_observation_count[observation.uidvalidity] = (
                uidvalidity_observation_count.get(observation.uidvalidity, 0) + 1
            )

        accepted: List[Tuple[int, str, str]] = []
        for index in remaining_indexes:
            candidates = best_by_observation.get(index, [])
            if not candidates:
                contradicted = contradictions.get(index, [])
                if contradicted:
                    # A decreasing UIDNEXT is impossible inside one
                    # generation, so this snapshot cannot be reconciled as a
                    # rename.  Fall through to a new generation below; the
                    # conflict stays visible for manual review.
                    conflicts.append(
                        CatalogConflict(
                            kind='contradictory_rename_evidence',
                            message=(
                                '; '.join(f'{record.folder_id}: {reason}' for record, reason in contradicted)
                                + '; recorded as a new folder generation'
                            ),
                            folder_ids=tuple(record.folder_id for record, _ in contradicted),
                            names=(obs_list[index].name,),
                        )
                    )
                continue
            max_score = max(score for score, _ in candidates)
            if max_score == 1:
                uidvalidity = obs_list[index].uidvalidity
                if (
                    uidvalidity_record_count.get(uidvalidity, 0) > 1
                    or uidvalidity_observation_count.get(uidvalidity, 0) > 1
                ):
                    conflicts.append(
                        CatalogConflict(
                            kind='ambiguous_rename',
                            message=(
                                'UIDVALIDITY is shared by several folders and no UIDNEXT '
                                'or UID evidence selects one; recorded as a new folder generation'
                            ),
                            folder_ids=tuple(sorted(folder_id for _, folder_id in candidates)),
                            names=(obs_list[index].name,),
                        )
                    )
                    continue
            top = [folder_id for score, folder_id in candidates if score == max_score]
            if len(top) > 1:
                conflicts.append(
                    CatalogConflict(
                        kind='ambiguous_rename',
                        message=(
                            'UIDVALIDITY and continuity evidence do not identify a single '
                            'recorded folder; recorded as a new folder generation'
                        ),
                        folder_ids=tuple(sorted(top)),
                        names=(obs_list[index].name,),
                    )
                )
                continue
            folder_id = top[0]
            reverse = best_by_record.get(folder_id, [])
            reverse_max = max(score for score, _ in reverse)
            reverse_top = [other for score, other in reverse if score == reverse_max]
            if len(reverse_top) != 1 or reverse_top[0] != index:
                conflicts.append(
                    CatalogConflict(
                        kind='ambiguous_rename',
                        message=('Rename evidence is not mutual; recorded as a new folder generation'),
                        folder_ids=(folder_id,),
                        names=(obs_list[index].name,),
                    )
                )
                continue
            accepted.append((index, folder_id, pair_scores[(index, folder_id)][1]))

        for index, folder_id, reason in accepted:
            record = self._folders[folder_id]
            if record.folder_id in used_folder_ids:
                continue
            event = self._apply_rename(record, obs_list[index], now, reason)
            renamed.append(event)
            matched[index] = record.folder_id
            used_folder_ids.add(record.folder_id)

        # Phase 3: remaining observations are new generations.  A record with
        # the same name but a different UIDVALIDITY is deletion plus recreation:
        # it is marked superseded, retained, and linked to the new generation.
        for index, observation in enumerate(obs_list):
            if index in matched:
                continue
            if observation.name in blocked_names:
                # The same name and UIDVALIDITY had already been superseded:
                # creating a third generation would hide the contradiction.
                continue
            record = self._create_folder(server, observation, now)
            created.append(record.folder_id)
            matched[index] = record.folder_id
            used_folder_ids.add(record.folder_id)
            for old in records_by_name.get(observation.name, ()):
                if old.folder_id not in used_folder_ids:
                    old.status = FOLDER_SUPERSEDED
                    old.superseded_by = record.folder_id
                    old.superseded_at = now
                    recreated.append((old.folder_id, record.folder_id))

        # Phase 4: on a complete snapshot, a record that was not observed is
        # absent, not deleted.  Partial listings leave unrelated records alone.
        absent = []
        if complete:
            for record in records:
                if record.folder_id in used_folder_ids:
                    continue
                if record.status == FOLDER_ACTIVE:
                    record.status = FOLDER_ABSENT
                    record.absent_since = now
                    absent.append(record.folder_id)

        observed_ids = tuple(matched[index] for index in range(len(obs_list)) if index in matched)
        return ReconcileReport(
            server=server,
            observed_at=now,
            observed_folder_ids=observed_ids,
            created=tuple(created),
            renamed=tuple(renamed),
            recreated=tuple(recreated),
            absent=tuple(absent),
            conflicts=tuple(conflicts),
            unresolved=tuple(sorted(blocked_names)),
        )

    def _validate_snapshot(self, observations: Sequence[FolderObservation]) -> None:
        names = [observation.name for observation in observations]
        if len(set(names)) != len(names):
            raise ValueError('Folder snapshot contains duplicate folder names')

    def _apply_observation(self, record: FolderRecord, observation: FolderObservation, now: datetime) -> None:
        record.status = FOLDER_ACTIVE
        record.absent_since = None
        record.last_seen = now
        if observation.uidnext is not None:
            record.uidnext = observation.uidnext
        if observation.uids is not None:
            record.uids = observation.uids
        if observation.message_count is not None:
            record.message_count = observation.message_count

    def _apply_rename(
        self, record: FolderRecord, observation: FolderObservation, now: datetime, reason: str
    ) -> RenameEvent:
        old_name = record.name
        if old_name != observation.name and old_name not in record.previous_names:
            record.previous_names.append(old_name)
        record.name = observation.name
        self._apply_observation(record, observation, now)
        event = RenameEvent(
            server=record.server,
            folder_id=record.folder_id,
            old_name=old_name,
            new_name=observation.name,
            observed_at=now,
            evidence=reason,
        )
        self._renames.append(event)
        return event

    def _create_folder(self, server: ServerIdentity, observation: FolderObservation, now: datetime) -> FolderRecord:
        generation = self._next_generation(server)
        folder_id = f'{server.key}#{generation}'
        record = FolderRecord(
            folder_id=folder_id,
            server=server,
            uidvalidity=observation.uidvalidity,
            generation=generation,
            name=observation.name,
            status=FOLDER_ACTIVE,
            first_seen=now,
            last_seen=now,
            uidnext=observation.uidnext,
            uids=observation.uids,
            message_count=observation.message_count,
        )
        self._folders[folder_id] = record
        return record

    def _next_generation(self, server: ServerIdentity) -> int:
        generations = [record.generation for record in self._folders.values() if record.server == server]
        return max(generations, default=0) + 1

    # ------------------------------------------------------------------
    # Folder queries
    # ------------------------------------------------------------------
    def folder(self, folder_id: str) -> FolderRecord:
        """Get a folder record by identifier"""
        try:
            return self._folders[folder_id]
        except KeyError:
            raise UnknownFolderError(f'Unknown folder id: {folder_id}') from None

    def find_folder(self, server: ServerIdentity, name: str, status: Optional[str] = None) -> Optional[FolderRecord]:
        """Find the newest folder record for a server and current name"""
        candidates = [
            record
            for record in self._folders.values()
            if record.server == server and record.name == name and (status is None or record.status == status)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda record: (record.generation, record.last_seen))

    def folders(
        self, server: Optional[ServerIdentity] = None, status: Optional[str] = None
    ) -> Tuple[FolderRecord, ...]:
        """Return folder records, optionally filtered"""
        result = [
            record
            for record in self._folders.values()
            if (server is None or record.server == server) and (status is None or record.status == status)
        ]
        result.sort(key=lambda record: (record.server.key, record.generation))
        return tuple(result)

    def renames(self, server: Optional[ServerIdentity] = None) -> Tuple[RenameEvent, ...]:
        """Return accepted rename events in observation order"""
        if server is None:
            return tuple(self._renames)
        return tuple(event for event in self._renames if event.server == server)

    # ------------------------------------------------------------------
    # Message occurrences
    # ------------------------------------------------------------------
    def occurrence(self, folder_id: str, uid: str) -> Optional[MessageOccurrence]:
        """Get one occurrence, or None"""
        return self._occurrences.get(f'{folder_id}/{uid}')

    def occurrences(
        self, folder_id: Optional[str] = None, status: Optional[str] = None
    ) -> Tuple[MessageOccurrence, ...]:
        """Return occurrences, optionally filtered"""
        result = [
            occurrence
            for occurrence in self._occurrences.values()
            if (folder_id is None or occurrence.folder_id == folder_id)
            and (status is None or occurrence.status == status)
        ]
        result.sort(key=lambda occurrence: (occurrence.folder_id, int(occurrence.uid)))
        return tuple(result)

    def observe_messages(
        self, folder_id: str, uids: Iterable[str], complete: bool = False, at: Optional[Union[datetime, str]] = None
    ) -> Tuple[MessageOccurrence, ...]:
        """
        Record message UIDs seen in a folder generation.

        :param folder_id: folder generation identifier
        :param uids: observed UID set
        :param complete: when True, previously present occurrences that are not
            in ``uids`` become ``unobserved``.  They are retained, never deleted.
        :param at: observation time
        :return: occurrences touched by the call
        """
        folder = self.folder(folder_id)
        if folder.status == FOLDER_SUPERSEDED:
            raise CatalogError(
                f'Folder generation {folder_id} is superseded; observe messages in the current generation instead'
            )
        now = _coerce_time(at)
        uid_set = set(_uid_tuple(uids) or ())
        touched = []
        for uid in sorted(uid_set, key=int):
            occurrence = self.occurrence(folder_id, uid)
            if occurrence is None:
                occurrence = MessageOccurrence(
                    folder_id=folder_id,
                    uid=uid,
                    status=OCCURRENCE_PRESENT,
                    first_seen=now,
                    last_seen=now,
                )
                self._occurrences[occurrence.occurrence_id] = occurrence
            elif occurrence.status == OCCURRENCE_UNOBSERVED:
                occurrence.status = OCCURRENCE_PRESENT
                occurrence.unobserved_since = None
                occurrence.last_seen = now
            elif occurrence.status == OCCURRENCE_PRESENT:
                occurrence.last_seen = now
            touched.append(occurrence)
        if complete:
            for occurrence in self._occurrences.values():
                if (
                    occurrence.folder_id == folder_id
                    and occurrence.status == OCCURRENCE_PRESENT
                    and occurrence.uid not in uid_set
                ):
                    occurrence.status = OCCURRENCE_UNOBSERVED
                    occurrence.unobserved_since = now
        return tuple(touched)

    def record_move(self, evidence: MoveEvidence, at: Optional[Union[datetime, str]] = None) -> MoveReport:
        """
        Record an explicit message move.

        The old occurrence is retained with status ``moved`` and linked to the
        destination occurrence.  The whole evidence is validated before any
        change is applied, so a conflicting mapping leaves the catalog intact.
        Re-applying the same evidence is idempotent.
        """
        if not isinstance(evidence, MoveEvidence):
            raise TypeError('evidence must be a MoveEvidence')
        now = _coerce_time(at)
        source = self.folder(evidence.source_folder_id)
        destination = self.folder(evidence.destination_folder_id)
        if source.server != evidence.server or destination.server != evidence.server:
            raise MoveEvidenceError('Move evidence server does not match both folder records')
        if destination.status == FOLDER_SUPERSEDED:
            raise MoveEvidenceError(f'Destination folder generation {destination.folder_id} is superseded')
        if destination.uidvalidity != evidence.destination_uidvalidity:
            raise MoveEvidenceError(
                'COPYUID destination UIDVALIDITY does not match the destination folder: '
                f'{evidence.destination_uidvalidity} != {destination.uidvalidity}'
            )
        if evidence.source_uidvalidity is not None and (source.uidvalidity != evidence.source_uidvalidity):
            raise MoveEvidenceError(
                'Move evidence source UIDVALIDITY does not match the source folder: '
                f'{evidence.source_uidvalidity} != {source.uidvalidity}'
            )

        already_applied = []
        pending = []
        for pair in evidence.pairs:
            source_occurrence = self.occurrence(source.folder_id, pair.source_uid)
            destination_occurrence = self.occurrence(destination.folder_id, pair.destination_uid)
            source_id = f'{source.folder_id}/{pair.source_uid}'
            destination_id = f'{destination.folder_id}/{pair.destination_uid}'
            if source_occurrence is not None and source_occurrence.status == OCCURRENCE_MOVED:
                if source_occurrence.moved_to != destination_id:
                    raise MoveEvidenceError(
                        f'Source UID {pair.source_uid} was already moved to {source_occurrence.moved_to}'
                    )
                already_applied.append((source_id, destination_id))
                continue
            if source_occurrence is not None and source_occurrence.status == OCCURRENCE_DELETED:
                raise MoveEvidenceError(f'Source UID {pair.source_uid} is recorded as deleted')
            if destination_occurrence is not None:
                if destination_occurrence.moved_from == source_id:
                    already_applied.append((source_id, destination_id))
                    continue
                raise MoveEvidenceError(
                    f'Destination UID {pair.destination_uid} already exists in {destination.folder_id}'
                )
            pending.append((pair, source_occurrence, source_id, destination_id))

        moved = []
        for pair, source_occurrence, source_id, destination_id in pending:
            if source_occurrence is None:
                source_occurrence = MessageOccurrence(
                    folder_id=source.folder_id,
                    uid=pair.source_uid,
                    status=OCCURRENCE_PRESENT,
                    first_seen=now,
                    last_seen=now,
                )
                self._occurrences[source_occurrence.occurrence_id] = source_occurrence
            source_occurrence.status = OCCURRENCE_MOVED
            source_occurrence.moved_to = destination_id
            source_occurrence.last_seen = now
            source_occurrence.unobserved_since = None
            destination_occurrence = MessageOccurrence(
                folder_id=destination.folder_id,
                uid=pair.destination_uid,
                status=OCCURRENCE_PRESENT,
                first_seen=now,
                last_seen=now,
                moved_from=source_id,
            )
            self._occurrences[destination_occurrence.occurrence_id] = destination_occurrence
            moved.append((source_occurrence, destination_occurrence))
        return MoveReport(
            evidence=evidence,
            moved=tuple(moved),
            already_applied=tuple(already_applied),
        )

    def mark_deleted(
        self, folder_id: str, uids: Iterable[str], at: Optional[Union[datetime, str]] = None
    ) -> Tuple[MessageOccurrence, ...]:
        """
        Explicitly mark occurrences deleted.

        This is the only operation that changes a present occurrence to
        ``deleted``.  Missing folders and snapshots never do it implicitly.
        """
        self.folder(folder_id)
        now = _coerce_time(at)
        result = []
        for uid in sorted(set(_uid_tuple(uids) or ()), key=int):
            occurrence = self.occurrence(folder_id, uid)
            if occurrence is None:
                occurrence = MessageOccurrence(
                    folder_id=folder_id,
                    uid=uid,
                    status=OCCURRENCE_DELETED,
                    first_seen=now,
                    last_seen=now,
                )
                self._occurrences[occurrence.occurrence_id] = occurrence
            elif occurrence.status == OCCURRENCE_MOVED:
                raise CatalogError(
                    f'UID {uid} in {folder_id} has already moved; deleting the old occurrence would lose its move link'
                )
            else:
                occurrence.status = OCCURRENCE_DELETED
                occurrence.last_seen = now
                occurrence.unobserved_since = None
            result.append(occurrence)
        return tuple(result)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable representation"""
        return {
            'format': CATALOG_FORMAT,
            'version': CATALOG_VERSION,
            'folders': [
                record.to_dict()
                for record in sorted(self._folders.values(), key=lambda item: (item.server.key, item.generation))
            ],
            'occurrences': [
                occurrence.to_dict()
                for occurrence in sorted(self._occurrences.values(), key=lambda item: (item.folder_id, int(item.uid)))
            ],
            'renames': [event.to_dict() for event in self._renames],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Catalog':
        """Restore a catalog from :meth:`to_dict` output"""
        if data.get('format') != CATALOG_FORMAT:
            raise ValueError(f'Unexpected catalog format: {data.get("format")!r}')
        if data.get('version') != CATALOG_VERSION:
            raise ValueError(f'Unsupported catalog version: {data.get("version")!r}')
        catalog = cls()
        for folder_data in data.get('folders', []):
            record = FolderRecord.from_dict(folder_data)
            if record.folder_id in catalog._folders:
                raise ValueError(f'Duplicate folder id in catalog: {record.folder_id}')
            catalog._folders[record.folder_id] = record
        for occurrence_data in data.get('occurrences', []):
            occurrence = MessageOccurrence.from_dict(occurrence_data)
            if occurrence.occurrence_id in catalog._occurrences:
                raise ValueError(f'Duplicate occurrence in catalog: {occurrence.occurrence_id}')
            catalog._occurrences[occurrence.occurrence_id] = occurrence
        catalog._renames = [RenameEvent.from_dict(event_data) for event_data in data.get('renames', [])]
        return catalog

    def dumps(self, indent: Optional[int] = 2) -> str:
        """Serialize to JSON text"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, sort_keys=True)

    @classmethod
    def loads(cls, text: str) -> 'Catalog':
        """Restore a catalog from JSON text"""
        return cls.from_dict(json.loads(text))

    def save(self, path: str, indent: Optional[int] = 2) -> None:
        """Write the catalog to a UTF-8 JSON file"""
        directory = os.path.dirname(os.path.abspath(path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as catalog_file:
            catalog_file.write(self.dumps(indent=indent))

    @classmethod
    def load(cls, path: str) -> 'Catalog':
        """Read the catalog from a UTF-8 JSON file"""
        with open(path, 'r', encoding='utf-8') as catalog_file:
            return cls.loads(catalog_file.read())
