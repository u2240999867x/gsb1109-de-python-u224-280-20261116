"""Explicit read-only trust scope.

The scope is intentionally strict:

* a borrowed connection that is already selected writable is refused instead of
  being switched to another folder;
* every command sent while the scope is active is checked against a whitelist;
* message bodies and attachments are fetched lazily with ``BODY.PEEK`` only;
* flags observed at read time and at scope exit are compared, but never
  restored - differences are reported as external observations;
* a report is published only when the absence of side effects was actually
  verified.
"""
import datetime
import re
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, FrozenSet, Iterable, List, Optional, Tuple

from .errors import (
    MailboxFetchError,
    MailboxFolderSelectError,
    MailboxReadOnlyConflictError,
    MailboxReadOnlyError,
    MailboxReadOnlyUnverifiedError,
    MailboxReadOnlyViolationError,
)
from .utils import check_command_status, encode_folder

if TYPE_CHECKING:  # pragma: no cover
    from .mailbox import BaseMailBox

# Commands that can not change message flags or mailbox state.
_READ_COMMANDS = frozenset({
    'NOOP',
    'CHECK',
    'STATUS',
    'LIST',
    'LSUB',
    'SEARCH',
    'SORT',
    'THREAD',
    'FETCH',
    'CAPABILITY',
    'ID',
    'NAMESPACE',
    'GETQUOTAROOT',
    'GETQUOTA',
    'GETACL',
    'IDLE',
})

# Commands that always mutate the selected mailbox or the mailbox list.
_WRITE_COMMANDS = frozenset({
    'STORE',
    'COPY',
    'MOVE',
    'APPEND',
    'EXPUNGE',
    'CREATE',
    'DELETE',
    'RENAME',
    'SUBSCRIBE',
    'UNSUBSCRIBE',
    'SELECT',
    'EXAMINE',
    'UNSELECT',
    'CLOSE',
    'SETACL',
    'DELETEACL',
    'SETQUOTA',
    'SETMETADATA',
    'LOGIN',
    'LOGOUT',
    'AUTHENTICATE',
})

_UID_READ_COMMANDS = frozenset({'SEARCH', 'SORT', 'THREAD', 'FETCH'})
_UID_WRITE_COMMANDS = frozenset({'STORE', 'COPY', 'MOVE', 'EXPUNGE'})


def _to_text(value) -> str:
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return str(value)


def _folder_key(folder) -> bytes:
    return encode_folder(folder)


def _iter_raw_items(data) -> Iterable[bytes]:
    # Only metadata is parsed: tuple[1] is a literal body which may contain
    # arbitrary text that looks like UID/FLAGS.
    for item in data or ():
        if isinstance(item, tuple):
            if item and isinstance(item[0], bytes):
                yield item[0]
        elif isinstance(item, bytes):
            yield item


def _parse_uid_flags(data) -> Dict[str, Optional[Tuple[str, ...]]]:
    """Parse ``UID FETCH`` responses into ``{uid: flags or None}``.

    ``None`` means that the server returned the message but did not include a
    FLAGS item, so the flags can not be trusted for side-effect verification.
    """
    result: Dict[str, Optional[Tuple[str, ...]]] = {}
    for raw in _iter_raw_items(data):
        text = raw.decode('utf-8', 'replace')
        uid_match = re.search(r'(?:^|[\s(])UID\s+(\d+)', text, re.IGNORECASE)
        if not uid_match:
            continue
        flags_match = re.search(r'(?:^|[\s(])FLAGS\s+\(([^)]*)\)', text, re.IGNORECASE)
        flags = tuple(flags_match.group(1).split()) if flags_match else None
        uid = uid_match.group(1)
        if uid in result and flags is None:
            continue
        result[uid] = flags
    return result


def collect_readonly_evidence(client, data=()) -> Tuple[str, ...]:
    """Return server-side evidence that the selection is READ-ONLY.

    imaplib stores the ``[READ-ONLY]`` response code as a key in
    ``untagged_responses``; for a code without data the value is ``None``, so
    key presence itself is the proof.
    """
    evidence: List[str] = []
    untagged = getattr(client, 'untagged_responses', {}) or {}
    if 'READ-ONLY' in untagged:
        evidence.append('READ-ONLY')
        for raw in untagged.get('READ-ONLY') or ():
            if isinstance(raw, bytes) and raw:
                evidence.append(raw.decode('utf-8', 'replace'))
    for raw in _iter_raw_items(data):
        if b'READ-ONLY' in raw.upper():
            evidence.append(raw.decode('utf-8', 'replace'))
    return tuple(dict.fromkeys(evidence))


def _is_peek_only_fetch(message_parts) -> bool:
    r"""Return True when a FETCH item set can not set the \Seen flag."""
    text = _to_text(message_parts).upper()
    # BODY (the macro) and BODY[...] without .PEEK implicitly set \Seen.
    for match in re.finditer(r'BODY', text):
        start = match.start()
        if text.startswith('BODYSTRUCTURE', start):
            continue
        if text.startswith('BODY.PEEK[', start):
            continue
        # any other BODY form: BODY[], BODY[HEADER], bare BODY macro
        return False
    # RFC822 and RFC822.TEXT set \Seen, RFC822.HEADER and RFC822.SIZE do not.
    for match in re.finditer(r'RFC822', text):
        start = match.start()
        if text.startswith('RFC822.SIZE', start) or text.startswith('RFC822.HEADER', start):
            continue
        return False
    return True


@dataclass(frozen=True)
class FlagObservation:
    """Flags as returned by the server during a read-only operation."""

    uid: str
    flags: Tuple[str, ...]
    observed_at: datetime.datetime
    source: str

    def as_dict(self) -> dict:
        return {
            'uid': self.uid,
            'flags': self.flags,
            'observed_at': self.observed_at.isoformat(),
            'source': self.source,
        }


@dataclass(frozen=True)
class ExternalFlagChange:
    """A flag difference that is attributed to a user or another client.

    The library never restores the old value: it only records the observation.
    """

    uid: str
    before: Tuple[str, ...]
    after: Optional[Tuple[str, ...]]
    observed_at: datetime.datetime
    source: str

    def as_dict(self) -> dict:
        return {
            'uid': self.uid,
            'before': self.before,
            'after': self.after,
            'observed_at': self.observed_at.isoformat(),
            'source': self.source,
        }


@dataclass(frozen=True)
class ReadOnlyReport:
    """Immutable, queryable read-only guarantee report."""

    folder: Optional[str]
    started_at: datetime.datetime
    finished_at: Optional[datetime.datetime]
    finalized: bool
    readonly_verified: bool
    readonly_evidence: Tuple[str, ...]
    side_effects_verified: bool
    complete: bool
    reason: Optional[str]
    observed_uids: Tuple[str, ...]
    observations: Tuple[FlagObservation, ...]
    external_flag_changes: Tuple[ExternalFlagChange, ...]
    write_attempts: Tuple[str, ...]
    lazy_loads: int
    guard_intact: bool
    selection_unchanged: bool
    reconnect_detected: bool
    partial_read: bool = False

    @property
    def readonly_complete(self) -> bool:
        """Alias: True only when the no-side-effect guarantee was verified."""
        return self.complete

    @property
    def verified(self) -> bool:
        return self.complete

    @property
    def no_side_effects(self) -> bool:
        """True only when the absence of side effects was actually verified."""
        return self.complete

    @property
    def verified_at(self) -> Optional[datetime.datetime]:
        return self.finished_at

    @property
    def external_changes(self) -> Tuple['ExternalFlagChange', ...]:
        """Alias for external_flag_changes."""
        return self.external_flag_changes

    @property
    def status(self) -> dict:
        """Alias for the serializable report dict."""
        return self.as_dict()

    @property
    def uid_guarantees(self) -> Dict[str, dict]:
        """Per-UID view: observed flags and whether the scope guarantee holds."""
        result = {uid: {'flags': None, 'verified': self.complete} for uid in self.observed_uids}
        for observation in self.observations:
            if observation.uid in result:
                result[observation.uid]['flags'] = observation.flags
        for change in self.external_flag_changes:
            if change.uid in result:
                result[change.uid]['external_change'] = {
                    'before': change.before,
                    'after': change.after,
                    'source': change.source,
                }
        return result

    def as_dict(self) -> dict:
        return {
            'folder': self.folder,
            'started_at': self.started_at.isoformat(),
            'finished_at': self.finished_at.isoformat() if self.finished_at else None,
            'finalized': self.finalized,
            'readonly_verified': self.readonly_verified,
            'readonly_evidence': self.readonly_evidence,
            'side_effects_verified': self.side_effects_verified,
            'complete': self.complete,
            'reason': self.reason,
            'observed_uids': self.observed_uids,
            'observations': [i.as_dict() for i in self.observations],
            'external_flag_changes': [i.as_dict() for i in self.external_flag_changes],
            'write_attempts': self.write_attempts,
            'lazy_loads': self.lazy_loads,
            'guard_intact': self.guard_intact,
            'selection_unchanged': self.selection_unchanged,
            'reconnect_detected': self.reconnect_detected,
            'partial_read': self.partial_read,
            'uid_guarantees': self.uid_guarantees,
        }

    def __str__(self):
        state = 'COMPLETE' if self.complete else 'UNVERIFIED'
        return f'ReadOnlyReport({state}, folder={self.folder!r}, reason={self.reason!r})'


class ReadOnlyScope:
    """Explicit read-only scope over a mailbox connection.

    Usage::

        with mailbox.readonly('INBOX') as ro:
            for msg in ro.fetch(A(seen=False)):
                print(msg.subject)       # headers only, BODY.PEEK
                print(msg.text)          # lazy BODY.PEEK[] on first access
                for att in msg.attachments:
                    save(att.payload)    # still the same verified scope

        ro.report.complete              # True only if verified
        ro.report.external_flag_changes # recorded, never restored

    If the borrowed connection is already selected writable, entering the
    scope raises :class:`MailboxReadOnlyConflictError`; the scope never issues
    ``SELECT``/``EXAMINE`` for another folder behind a running task's back.
    """

    def __init__(self, mailbox: 'BaseMailBox', folder=None, *, lazy: bool = True,
                 require_evidence: bool = True, strict: bool = False) -> None:
        self.mailbox = mailbox
        self.folder = folder
        self.lazy = lazy
        self.require_evidence = require_evidence
        self.strict = strict

        self._state = 'new'
        self._client = None
        self._orig_command = None
        self._guard = None
        self._owns_selection = False
        self._selection_token = None
        self._readonly_verified = False
        self._readonly_evidence: List[str] = []
        self._side_effects_verified = False
        self._selection_unchanged = False
        self._reconnect_detected = False
        self._guard_intact = True
        self._write_attempts: List[str] = []
        self._reasons: List[str] = []
        self._observations: List[FlagObservation] = []
        self._initial_flags: Dict[str, FrozenSet[str]] = {}
        self._external_changes: List[ExternalFlagChange] = []
        self._lazy_loads = 0
        self._lock = threading.RLock()
        self._started_at = datetime.datetime.now(datetime.timezone.utc)
        self._finished_at: Optional[datetime.datetime] = None
        self._report: Optional[ReadOnlyReport] = None
        self._exception: Optional[BaseException] = None
        self._verifying = False
        self._flags_observable = True
        self._partial = False

    # ------------------------------------------------------------------ enter
    def __enter__(self) -> 'ReadOnlyScope':
        if self.mailbox._readonly_session is not None:
            raise MailboxReadOnlyConflictError(
                'A read-only scope is already active on this mailbox connection. '
                'Nested or concurrent scopes are not allowed.')
        client = self.mailbox.client
        self._client = client
        state = getattr(client, 'state', None)
        try:
            if state == 'SELECTED':
                self._enter_existing_selection()
            elif state == 'AUTH':
                if self.folder is None:
                    raise MailboxReadOnlyConflictError(
                        'The connection is not selected and no folder was supplied. '
                        'Pass a folder to the read-only scope explicitly.')
                self._examine(self.folder)
                self._owns_selection = True
            else:
                raise MailboxReadOnlyConflictError(
                    f'Connection state {state!r} does not allow a read-only scope. '
                    'A selected read-only mailbox or an authenticated connection is required.')
            if not self._readonly_verified:
                self._unverified('EXAMINE response did not prove READ-ONLY')
            if self.require_evidence and not self._readonly_verified:
                raise MailboxReadOnlyUnverifiedError(
                    'The server did not confirm that the mailbox is READ-ONLY, so a '
                    'read-only guarantee can not be published. Use require_evidence=False '
                    'to read anyway, but the report will stay unverified.')
        except Exception:
            self._state = 'failed'
            self._finished_at = datetime.datetime.now(datetime.timezone.utc)
            self._detach()
            if self._owns_selection and self._client is not None \
                    and getattr(self._client, 'state', None) == 'SELECTED':
                # Do not leave a borrowed authenticated connection selected
                # after a failed entry.
                self._selection_unchanged = True
                self._restore_selection()
            if self.mailbox._readonly_session is self:
                self.mailbox._readonly_session = None
            self.mailbox.last_readonly_report = self.report
            raise

        self.mailbox._readonly_session = self
        self._selection_token = self._current_selection_token()
        self._state = 'active'
        self._install_guard()
        return self

    def _enter_existing_selection(self) -> None:
        current_folder = self.mailbox.folder.get()
        if current_folder is None:
            raise MailboxReadOnlyConflictError(
                'The connection is selected, but the selected folder is unknown to imap_tools. '
                'A safe read-only scope can not be established.')
        if self.folder is not None and _folder_key(self.folder) != _folder_key(current_folder):
            raise MailboxReadOnlyConflictError(
                f'Connection is already selected for {current_folder!r}; refusing to switch to '
                f'{self.folder!r} because another task may be using the selection.')
        if not self.mailbox.folder.is_readonly():
            raise MailboxReadOnlyConflictError(
                f'Connection is already selected writable for {current_folder!r}. Refusing to '
                'switch it to read-only: use a dedicated connection for the read-only scope.')
        self.folder = current_folder
        self._readonly_verified = False
        evidence = self.mailbox.folder.readonly_evidence()
        if evidence:
            self._readonly_evidence.extend(evidence)
            self._readonly_verified = True
        elif self.require_evidence:
            # Re-EXAMINE the same folder to obtain server evidence. This does
            # not switch folders and can not change message flags.
            self._examine(current_folder)
            self._owns_selection = False

    def _examine(self, folder) -> None:
        client = self.mailbox.client
        encoded = encode_folder(folder)
        prev_state = client.state
        prev_is_readonly = client.is_readonly
        prev_untagged = client.untagged_responses
        # Same bookkeeping as imaplib.IMAP4.select(readonly=True), but keep the
        # tagged response so [READ-ONLY] can actually be verified.
        client.untagged_responses = {}
        client.is_readonly = True
        typ, data = client._simple_command('EXAMINE', encoded)
        if typ != 'OK':
            # RFC 3501: on NO the server does not change the selected state.
            # imaplib unconditionally sets state to AUTH; undo that so a
            # borrowed task is not left thinking its selection disappeared.
            if prev_state == 'SELECTED':
                client.state = 'SELECTED'
                client.is_readonly = prev_is_readonly
                client.untagged_responses = prev_untagged
            else:
                client.state = 'AUTH'
                client.is_readonly = False
                self.mailbox.folder._current_folder = None
                self.mailbox.folder._current_readonly = None
                self.mailbox.folder._current_readonly_evidence = ()
            raise MailboxFolderSelectError(command_result=(typ, data), expected='OK')
        client.state = 'SELECTED'
        self._readonly_evidence = list(collect_readonly_evidence(client, data))
        self._readonly_verified = bool(self._readonly_evidence)
        self.mailbox.folder._current_folder = folder
        self.mailbox.folder._current_readonly = True
        self.mailbox.folder._current_readonly_evidence = tuple(self._readonly_evidence)

    # ------------------------------------------------------------------ guard
    def _install_guard(self) -> None:
        client = self._client
        self._orig_command = client._command
        self._orig_select = client.select
        self._orig_append = client.append
        scope = self

        def guarded_command(name, *args, **kwargs):
            scope._check_command(name, args)
            return scope._orig_command(name, *args, **kwargs)

        def guarded_select(mailbox='INBOX', readonly=False):
            # imaplib.select mutates is_readonly/untagged_responses before the
            # command is sent, so refuse before entering it at all.
            scope.refuse_write('SELECT/EXAMINE')

        def guarded_append(*args, **kwargs):
            # imaplib.append sets self.literal before sending; refuse first.
            scope.refuse_write('APPEND')

        self._guard = guarded_command
        self._guard_select = guarded_select
        self._guard_append = guarded_append
        client._command = guarded_command
        client.select = guarded_select
        client.append = guarded_append

    def _detach(self) -> None:
        if self._client is None or self._guard is None:
            return
        if getattr(self._client, '_command', None) is self._guard:
            self._client._command = self._orig_command
        else:
            self._guard_intact = False
            self._reasons.append('command guard was replaced by another client')
        if getattr(self._client, 'select', None) is self._guard_select:
            self._client.select = self._orig_select
        else:
            self._guard_intact = False
            self._reasons.append('select guard was replaced by another client')
        if getattr(self._client, 'append', None) is self._guard_append:
            self._client.append = self._orig_append
        else:
            self._guard_intact = False
            self._reasons.append('append guard was replaced by another client')

    def _check_command(self, name, args) -> None:
        if self._state == 'closing' and self._verifying:
            pass  # internal FLAGS-only verification
        elif self._state != 'active':
            raise MailboxReadOnlyError('The read-only scope is no longer active.')
        self.assert_read_context(allow_closing=self._verifying)
        command = _to_text(name).upper()
        if command == 'UID':
            sub_command = _to_text(args[0]).upper() if args else ''
            if sub_command == 'FETCH':
                self._assert_fetch_parts(args[-1] if args else '')
            elif sub_command in _UID_READ_COMMANDS:
                pass
            elif sub_command in _UID_WRITE_COMMANDS:
                self.refuse_write(f'UID {sub_command}')
            else:
                self.refuse_write(f'UID {sub_command or "<unknown>"}')
            return
        if command == 'FETCH':
            self._assert_fetch_parts(args[-1] if args else '')
            return
        if command in _READ_COMMANDS:
            return
        if command == 'LOGOUT':
            # Allowed, but the connection can not be verified afterwards.
            self._unverified('LOGOUT was issued inside the read-only scope')
            return
        # Default deny: an unknown command could mutate something.
        self.refuse_write(command or '<unknown>')

    def _assert_fetch_parts(self, message_parts) -> None:
        if not _is_peek_only_fetch(message_parts):
            self.refuse_write(f'non-PEEK FETCH {message_parts!r}')

    def refuse_write(self, operation: str) -> None:
        self._write_attempts.append(operation)
        self._reasons.append(f'write attempt: {operation}')
        raise MailboxReadOnlyViolationError(
            f'Operation {operation!r} is not allowed inside the read-only scope.')

    def _unverified(self, reason: str) -> None:
        if reason not in self._reasons:
            self._reasons.append(reason)

    # --------------------------------------------------------- selection state
    def _current_selection_token(self):
        client = self.mailbox.client
        folder = self.mailbox.folder.get()
        return (
            id(client),
            id(getattr(client, 'sock', None)),
            getattr(client, 'state', None),
            bool(getattr(client, 'is_readonly', False)),
            _folder_key(folder) if folder is not None else None,
            self.mailbox.folder.is_readonly(),
        )

    def assert_read_context(self, allow_closing: bool = False) -> None:
        """Verify selection and read-only state before every command."""
        if self._state != 'active' and not (allow_closing and self._state == 'closing'):
            raise MailboxReadOnlyError('The read-only scope is no longer active.')
        if self.mailbox.client is not self._client:
            self._reconnect_detected = True
            self._unverified('connection object changed (reconnect)')
            raise MailboxReadOnlyUnverifiedError(
                'The mailbox connection was replaced after the read-only scope started. '
                'Selection and read-only state can not be confirmed.')
        if not self.mailbox.folder.is_readonly() or not getattr(self._client, 'is_readonly', False):
            self._unverified('selection is no longer read-only')
            raise MailboxReadOnlyUnverifiedError(
                'The selected mailbox is no longer known to be READ-ONLY; refusing to read.')
        current = self._current_selection_token()
        if current != self._selection_token:
            if current[0] != self._selection_token[0] or current[1] != self._selection_token[1]:
                self._reconnect_detected = True
            self._unverified('selection changed during the read-only scope')
            raise MailboxReadOnlyUnverifiedError(
                'The borrowed connection changed its selection during the read-only scope; '
                'a side-effect free guarantee can not be confirmed.')

    def _selection_still_ours(self) -> bool:
        try:
            return self._current_selection_token() == self._selection_token
        except Exception:  # noqa: BLE001 - any failure means "can not confirm"
            return False

    # ------------------------------------------------------------ observations
    def observe(self, uid: Optional[str], flags: Iterable[str], source: str) -> None:
        """Record flags returned by the server. Never writes anything back."""
        if uid is None:
            return
        with self._lock:
            flags_tuple = tuple(flags)
            observation = FlagObservation(
                uid=uid,
                flags=flags_tuple,
                observed_at=datetime.datetime.now(datetime.timezone.utc),
                source=source,
            )
            self._observations.append(observation)
            initial = self._initial_flags.get(uid)
            if initial is None:
                self._initial_flags[uid] = frozenset(flags_tuple)
            elif initial != frozenset(flags_tuple):
                self._external_changes.append(ExternalFlagChange(
                    uid=uid,
                    before=tuple(sorted(initial)),
                    after=flags_tuple,
                    observed_at=observation.observed_at,
                    source=f'{source}:external-observation',
                ))

    def observe_message(self, message) -> None:
        """Observe a message object; kept for custom message classes."""
        uid = getattr(message, 'uid', None)
        flags = getattr(message, 'flags', None)
        if uid is not None and flags is not None:
            self.observe(uid, flags, 'fetch')

    def note_generator_closed(self) -> None:
        """Record an early end of a fetch generator.

        Early exit is not a side effect by itself; the final FLAGS comparison at
        scope exit still decides whether the guarantee holds. The note makes the
        partial read visible in the report.
        """
        self._partial = True

    # --------------------------------------------------------------- lazy load
    def body_loader(self, uid: Optional[str]):
        """Return a zero-argument loader used by MailMessage lazy properties."""
        def load():
            return self.load_body(uid)
        return load

    def load_body(self, uid: Optional[str]) -> bytes:
        """Fetch a full message body with BODY.PEEK[] inside the active scope."""
        if self._state != 'active':
            raise MailboxReadOnlyError(
                'Lazy body access after the read-only scope ended is refused: the selection '
                'may now belong to another task.')
        if not uid:
            self._unverified('lazy body load without a UID')
            raise MailboxReadOnlyUnverifiedError('Lazy body load requires a message UID.')
        self.assert_read_context()
        message_parts = '(BODY.PEEK[] UID FLAGS RFC822.SIZE)'
        try:
            fetch_result = self.mailbox.client.uid('fetch', uid, message_parts)
            check_command_status(fetch_result, MailboxFetchError)
        except Exception as exc:
            self._unverified(f'lazy body load failed for UID {uid}: {exc}')
            raise
        raw_message_data = None
        for item in fetch_result[1] or ():
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], bytes):
                raw_message_data = item[1]
                self._observe_fetch_raw(item[0], 'lazy-body')
            elif isinstance(item, bytes):
                self._observe_fetch_raw(item, 'lazy-body')
        self._lazy_loads += 1
        if raw_message_data is None:
            self._unverified(f'server returned no body for UID {uid}')
            raise MailboxReadOnlyUnverifiedError(
                f'The server returned no body for UID {uid}; the read is not complete.')
        return raw_message_data

    def _observe_fetch_raw(self, raw: bytes, source: str) -> None:
        parsed = _parse_uid_flags([raw])
        for uid, flags in parsed.items():
            if flags is not None:
                self.observe(uid, flags, source)

    def _observe_fetch_raw_items(self, data, source: str) -> None:
        """Observe all UID/FLAGS pairs in a raw FETCH response.

        A FETCH that was asked for FLAGS but did not return them means the
        absence of a Seen-flag side effect can not be confirmed.
        """
        parsed = _parse_uid_flags(data)
        metadata_items = list(_iter_raw_items(data))
        if metadata_items and not parsed:
            self._flags_observable = False
            self._unverified('FETCH response contained no UID/FLAGS metadata')
        for uid, flags in parsed.items():
            if flags is None:
                self._flags_observable = False
                self._unverified(f'FLAGS missing in FETCH response for UID {uid}')
                continue
            self.observe(uid, flags, source)

    # -------------------------------------------------------------- verification
    def verify(self) -> bool:
        """Run a safe FLAGS-only verification against observed messages.

        This is useful for long-running scopes and for callers that want a
        guarantee before leaving the ``with`` block.
        """
        if self._state not in ('active', 'closing'):
            return self._report.complete if self._report else False
        ok = self._verify_flags()
        self._side_effects_verified = ok
        return ok

    def _verify_flags(self) -> bool:
        if not self._flags_observable:
            self._unverified('FETCH flags metadata was not fully observable')
            return False
        uids = tuple(self._initial_flags)
        if not uids:
            # No message was ever observed; there is nothing that could have
            # received a Seen flag through this scope.
            return True
        if not self._selection_still_ours():
            self._unverified('selection changed before flag verification')
            return False
        parsed = {}
        while uids:
            batch, uids = uids[:200], uids[200:]
            self._verifying = True
            try:
                fetch_result = self.mailbox.client.uid('fetch', ','.join(batch), '(UID FLAGS)')
                check_command_status(fetch_result, MailboxFetchError)
            except Exception as exc:  # noqa: BLE001 - verification failure is reported, not raised
                self._unverified(f'flags verification failed: {exc}')
                return False
            finally:
                self._verifying = False
            parsed.update(_parse_uid_flags(fetch_result[1]))
        ok = True
        for uid in self._initial_flags:
            if uid not in parsed:
                # The message was removed/moved by another client; record it,
                # but do not attempt to restore anything.
                self._external_changes.append(ExternalFlagChange(
                    uid=uid,
                    before=tuple(sorted(self._initial_flags[uid])),
                    after=None,
                    observed_at=datetime.datetime.now(datetime.timezone.utc),
                    source='final-verification:message-not-present',
                ))
                continue
            flags = parsed[uid]
            if flags is None:
                self._unverified(f'FLAGS missing in verification response for UID {uid}')
                ok = False
                continue
            self.observe(uid, flags, 'verification')
        return ok

    # ------------------------------------------------------------------- report
    def _build_report(self, finalized: bool) -> ReadOnlyReport:
        complete = (
            finalized
            and self._readonly_verified
            and self._side_effects_verified
            and self._selection_unchanged
            and not self._write_attempts
            and self._guard_intact
            and self._exception is None
        )
        return ReadOnlyReport(
            folder=_to_text(self.folder) if self.folder is not None else None,
            started_at=self._started_at,
            finished_at=self._finished_at,
            finalized=finalized,
            readonly_verified=self._readonly_verified,
            readonly_evidence=tuple(self._readonly_evidence),
            side_effects_verified=self._side_effects_verified,
            complete=complete,
            reason='; '.join(self._reasons) if self._reasons else None,
            observed_uids=tuple(sorted(self._initial_flags)),
            observations=tuple(self._observations),
            external_flag_changes=tuple(self._external_changes),
            write_attempts=tuple(self._write_attempts),
            lazy_loads=self._lazy_loads,
            guard_intact=self._guard_intact,
            selection_unchanged=self._selection_unchanged,
            reconnect_detected=self._reconnect_detected,
            partial_read=self._partial,
        )

    @property
    def report(self) -> ReadOnlyReport:
        if self._report is not None:
            return self._report
        return self._build_report(finalized=False)

    def get_report(self) -> ReadOnlyReport:
        return self.report

    @property
    def readonly_complete(self) -> bool:
        return self.report.complete

    @property
    def complete(self) -> bool:
        """True only once the scope has exited and the guarantee was verified."""
        return self.report.complete

    @property
    def verified(self) -> bool:
        return self.report.complete

    @property
    def reason(self) -> Optional[str]:
        return self.report.reason

    @property
    def readonly_verified(self) -> bool:
        return self._readonly_verified

    @property
    def observed_uids(self) -> Tuple[str, ...]:
        return tuple(sorted(self._initial_flags))

    @property
    def write_attempts(self) -> Tuple[str, ...]:
        return tuple(self._write_attempts)

    @property
    def external_flag_changes(self) -> Tuple[ExternalFlagChange, ...]:
        return tuple(self._external_changes)

    @property
    def guarantees(self) -> dict:
        return self.report.as_dict()

    def is_uid_verified(self, uid: str) -> bool:
        """Whether the read-only guarantee covers this UID."""
        return bool(self.report.uid_guarantees.get(str(uid), {}).get('verified'))

    def _assert_active(self) -> None:
        if self._state != 'active':
            raise MailboxReadOnlyError(
                'The read-only scope is no longer active; refusing to touch the borrowed selection.')
        self.assert_read_context()

    # Convenience delegation: the scope always binds operations to the same
    # verified selection and refuses them after the scope ends.
    def fetch(self, *args, **kwargs):
        self._assert_active()
        return self.mailbox.fetch(*args, **kwargs)

    def uids(self, *args, **kwargs):
        self._assert_active()
        return self.mailbox.uids(*args, **kwargs)

    def numbers(self, *args, **kwargs):
        self._assert_active()
        return self.mailbox.numbers(*args, **kwargs)

    def numbers_to_uids(self, *args, **kwargs):
        self._assert_active()
        return self.mailbox.numbers_to_uids(*args, **kwargs)

    def assert_complete(self) -> None:
        """Raise unless the read-only guarantee was verified."""
        report = self.report
        if not report.complete:
            raise MailboxReadOnlyUnverifiedError(
                f'Read-only guarantee is not complete: {report.reason or "not finalized"}')

    raise_if_unverified = assert_complete

    # -------------------------------------------------------------------- exit
    def __exit__(self, exc_type, exc_value, exc_traceback) -> bool:
        self._exception = exc_value
        if exc_value is not None:
            self._unverified(f'exception inside read-only scope: {exc_value!r}')
        self._state = 'closing'
        try:
            if self._client is not None and self.mailbox.client is not self._client:
                self._reconnect_detected = True
                self._unverified('connection object changed (reconnect)')
                self._selection_unchanged = False
            elif self._client is not None:
                self._selection_unchanged = self._selection_still_ours()
                if self._selection_unchanged:
                    self._side_effects_verified = self._verify_flags()
                else:
                    self._unverified('selection changed at scope exit')
            else:
                self._side_effects_verified = False
        except Exception as exc:  # noqa: BLE001 - never mask the caller's exception
            self._unverified(f'final verification failed: {exc}')
            self._side_effects_verified = False
        finally:
            self._state = 'closed'
            self._finished_at = datetime.datetime.now(datetime.timezone.utc)
            self._detach()
            self._restore_selection()
            if self.mailbox._readonly_session is self:
                self.mailbox._readonly_session = None
            self._report = self._build_report(finalized=True)
            self.mailbox.last_readonly_report = self._report
        if self.strict and exc_type is None and not self._report.complete:
            raise MailboxReadOnlyUnverifiedError(
                f'Read-only guarantee is not complete: {self._report.reason or "unverified"}')
        return False

    def _restore_selection(self) -> None:
        if not self._owns_selection or self._client is None:
            return
        client = self._client
        if self.mailbox.client is not client or getattr(client, 'state', None) != 'SELECTED':
            return
        if not self._selection_unchanged:
            return
        try:
            typ, data = client._simple_command('UNSELECT')
            if typ == 'OK':
                self.mailbox.folder._current_folder = None
                self.mailbox.folder._current_readonly = None
                self.mailbox.folder._current_readonly_evidence = ()
            else:
                self._unverified(f'UNSELECT failed: {data!r}')
                self._side_effects_verified = False
        except Exception as exc:  # noqa: BLE001 - selection restoration is best effort
            self._unverified(f'UNSELECT failed: {exc}')
            self._side_effects_verified = False
