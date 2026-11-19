"""Resumable IMAP IDLE mailbox collection.

IDLE notifications are not a directory.  An ``EXISTS`` count only says how
many messages the server believes are present; an ``EXPUNGE`` renumbers every
following message sequence number; flag notifications may be interleaved with
both.  The safe unit of identity is the UID, valid only inside the mailbox's
current ``UIDVALIDITY`` domain.

:class:`IdleCollector` therefore treats IDLE as a change signal, not as a
source of message identity:

* listen in explicit windows;
* leave IDLE and wait for the tagged completion of ``DONE``;
* reconcile the whole current directory with ``UID FETCH 1:* (UID FLAGS ...)``;
* publish only if no new change arrived while the reconciliation was running;
* otherwise repeat the reconciliation without publishing the stale result;
* record explicit gaps for disconnects, UIDVALIDITY changes and mailbox
  switches.

Every notification, query, window, gap and archive operation is recorded in an
ordered replay log.  Archiving is keyed by ``(UIDVALIDITY, UID)``, so repeated
notifications and repeated catch-up queries cannot archive the same message
twice.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping, Optional


__all__ = (
    "IdleCollector",
    "IDLECollector",
    "IdleMailboxCollector",
    "IdleSession",
    "IdleSync",
    "CollectorError",
    "collect_mailbox",
    "collect",
    "Gap",
    "Window",
    "Notification",
    "Query",
    "Message",
    "Snapshot",
    "ReplayEvent",
    "NOTIFICATION_TYPES",
)


NOTIFICATION_TYPES = frozenset(
    ("EXISTS", "EXPUNGE", "FETCH", "RECENT", "VANISHED", "FLAGS")
)

# These change the set of messages, or the meaning of sequence numbers.
STRUCTURAL_TYPES = frozenset(("EXISTS", "EXPUNGE", "RECENT", "VANISHED"))

DEFAULT_FETCH_PARTS = "(UID FLAGS INTERNALDATE RFC822.SIZE)"


def _as_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii", "replace")
    if value is None:
        return ""
    return str(value)


def _as_int(value: Any) -> Optional[int]:
    try:
        text = _as_text(value).strip()
        match = re.search(r"-?\d+", text)
        if match:
            return int(match.group(0))
    except (TypeError, ValueError):
        pass
    return None


def _parse_uid_set(value: Any) -> tuple[int, ...]:
    """Parse an IMAP UID set (``1:5,8``) into sorted unique integers."""

    text = _as_text(value).replace("(EARLIER)", " ").strip()
    result: set[int] = set()
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if ":" in part:
            low_s, high_s = part.split(":", 1)
            if high_s == "*" or low_s == "*":
                continue
            try:
                low, high = int(low_s), int(high_s)
            except ValueError:
                continue
            if low > high:
                low, high = high, low
            # Bound absurd ranges; a UID set is data, not an allocation hint.
            if high - low <= 1000000:
                result.update(range(low, high + 1))
        else:
            try:
                result.add(int(part))
            except ValueError:
                continue
    return tuple(sorted(result))


_FETCH_SEQ_RE = re.compile(r"^\s*(\d+)\s+", re.ASCII)
_FETCH_UID_RE = re.compile(r"(?:^|[\s(])UID\s+(\d+)", re.ASCII | re.IGNORECASE)
_FETCH_FLAGS_RE = re.compile(
    r"(?:^|[\s(])FLAGS\s+\(([^)]*)\)", re.ASCII | re.IGNORECASE
)
_FETCH_SIZE_RE = re.compile(
    r"(?:^|[\s(])RFC822\.SIZE\s+(\d+)", re.ASCII | re.IGNORECASE
)
_FETCH_DATE_RE = re.compile(
    r'(?:^|[\s(])INTERNALDATE\s+"([^"]*)"', re.ASCII | re.IGNORECASE
)
_FLAGS_ATOM_RE = re.compile(r"\\?[^\s()]+")


def _fetch_header(data: Any) -> str:
    if isinstance(data, tuple) and data:
        return _as_text(data[0])
    return _as_text(data)


def parse_fetch(data: Any) -> dict[str, Any]:
    """Parse the metadata in one ``FETCH`` response.

    A leading sequence number is reported only as a hint.  It is never used as
    message identity; identity comes from the ``UID`` item, and is valid only
    within the current UIDVALIDITY domain.
    """

    header = _fetch_header(data)
    parsed: dict[str, Any] = {"sequence_hint": None, "uid": None, "flags": None}
    match = _FETCH_SEQ_RE.match(header)
    if match:
        parsed["sequence_hint"] = int(match.group(1))
    match = _FETCH_UID_RE.search(header)
    if match:
        parsed["uid"] = int(match.group(1))
    match = _FETCH_FLAGS_RE.search(header)
    if match:
        parsed["flags"] = frozenset(
            atom for atom in _FLAGS_ATOM_RE.findall(match.group(1)) if atom
        )
    match = _FETCH_SIZE_RE.search(header)
    if match:
        parsed["size"] = int(match.group(1))
    match = _FETCH_DATE_RE.search(header)
    if match:
        parsed["internaldate"] = match.group(1)
    return parsed


def parse_notification(typ: str, data: Any) -> dict[str, Any]:
    """Parse notification data without inventing identities.

    ``EXISTS`` and ``EXPUNGE`` carry sequence numbers.  They are recorded as
    hints only.  A UID is reported only when the server actually supplied one.
    """

    parsed: dict[str, Any] = {
        "sequence_hint": None,
        "uid": None,
        "uids": (),
        "flags": None,
    }
    typ = typ.upper()
    if typ in ("EXISTS", "EXPUNGE", "RECENT"):
        match = re.match(r"^\s*(\d+)", _as_text(data), re.ASCII)
        if match:
            parsed["sequence_hint"] = int(match.group(1))
    elif typ == "FETCH":
        parsed.update(parse_fetch(data))
    elif typ == "VANISHED":
        parsed["uids"] = _parse_uid_set(data)
    return parsed


class _Record:
    """Mapping-style access for the small public record classes."""

    def __getitem__(self, key):
        try:
            return getattr(self, key)
        except AttributeError as exc:
            raise KeyError(key) from exc

    def get(self, key, default=None):
        return getattr(self, key, default)

    def keys(self):
        return self.__dataclass_fields__.keys()

    def items(self):
        return tuple((key, getattr(self, key)) for key in self.keys())

    def __iter__(self):
        return iter(self.keys())

    def to_dict(self) -> dict[str, Any]:
        return dict(self.items(), kind=self.kind)


@dataclass
class Gap(_Record):
    """An explicitly unknown stretch of notification history."""

    id: int
    reason: str
    mailbox: Optional[str]
    uidvalidity: Optional[int]
    start_seq: int
    end_seq: Optional[int] = None
    window: Optional[str] = None
    open: bool = True
    resolved: bool = False
    created_at: float = field(default_factory=time.time)
    kind: str = field(default="gap", init=False)

    @property
    def start(self) -> int:
        return self.start_seq

    @property
    def end(self) -> Optional[int]:
        return self.end_seq

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "gap",
            "id": self.id,
            "reason": self.reason,
            "mailbox": self.mailbox,
            "uidvalidity": self.uidvalidity,
            "start_seq": self.start_seq,
            "end_seq": self.end_seq,
            "window": self.window,
            "open": self.open,
            "resolved": self.resolved,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class Notification(_Record):
    seq: int
    type: str
    data: Any
    mailbox: Optional[str]
    uidvalidity: Optional[int]
    window: Optional[str]
    phase: str
    sequence_hint: Optional[int]
    uid: Optional[int]
    uids: tuple[int, ...]
    flags: Optional[frozenset[str]]
    at: float
    kind: str = field(default="notification", init=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "notification",
            "seq": self.seq,
            "type": self.type,
            "data": self.data,
            "mailbox": self.mailbox,
            "uidvalidity": self.uidvalidity,
            "window": self.window,
            "phase": self.phase,
            "sequence_hint": self.sequence_hint,
            "uid": self.uid,
            "uids": self.uids,
            "flags": self.flags,
            "at": self.at,
        }


@dataclass
class Window(_Record):
    id: str
    mailbox: Optional[str]
    uidvalidity: Optional[int]
    start_seq: int
    started_at: float
    end_seq: Optional[int] = None
    ended_at: Optional[float] = None
    reason: Optional[str] = None
    notifications: int = 0
    kind: str = field(default="window", init=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "window",
            "id": self.id,
            "mailbox": self.mailbox,
            "uidvalidity": self.uidvalidity,
            "start_seq": self.start_seq,
            "end_seq": self.end_seq,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "reason": self.reason,
            "notifications": self.notifications,
        }


@dataclass
class Query(_Record):
    id: str
    window: Optional[str]
    phase: str
    mailbox: Optional[str]
    uidvalidity: Optional[int]
    command: str
    requested_range: str
    parts: str
    start_seq: int
    started_at: float
    end_seq: Optional[int] = None
    finished_at: Optional[float] = None
    uids: tuple[int, ...] = ()
    covered_range: Optional[str] = None
    ok: bool = False
    error: Optional[str] = None
    superseded: bool = False
    reason: Optional[str] = None
    published_generation: Optional[int] = None
    replay_written: bool = field(default=False, init=False, repr=False)
    kind: str = field(default="query", init=False)

    @property
    def range(self) -> str:
        return self.requested_range

    @property
    def queried_range(self) -> str:
        return self.requested_range

    @property
    def uid_range(self) -> Optional[str]:
        return self.covered_range

    @property
    def uids_queried(self) -> tuple[int, ...]:
        return self.uids

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "query",
            "id": self.id,
            "window": self.window,
            "phase": self.phase,
            "mailbox": self.mailbox,
            "uidvalidity": self.uidvalidity,
            "command": self.command,
            "requested_range": self.requested_range,
            "queried_range": self.requested_range,
            "parts": self.parts,
            "start_seq": self.start_seq,
            "end_seq": self.end_seq,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "uids": self.uids,
            "covered_range": self.covered_range,
            "ok": self.ok,
            "error": self.error,
            "superseded": self.superseded,
            "reason": self.reason,
            "published_generation": self.published_generation,
        }


@dataclass(frozen=True)
class Message(_Record):
    uid: int
    flags: frozenset[str] = frozenset()
    internaldate: Optional[str] = None
    size: Optional[int] = None
    sequence_hint: Optional[int] = None
    material: Any = None
    kind: str = field(default="message", init=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "message",
            "uid": self.uid,
            "flags": self.flags,
            "internaldate": self.internaldate,
            "size": self.size,
            "sequence_hint": self.sequence_hint,
            "material": self.material,
        }


@dataclass(frozen=True)
class Snapshot:
    generation: int
    mailbox: Optional[str]
    uidvalidity: Optional[int]
    messages: Mapping[int, Message]
    complete: bool
    taken_at: float
    query: Optional[str] = None
    window: Optional[str] = None
    gaps: tuple[Gap, ...] = ()
    kind: str = field(default="snapshot", init=False)

    @property
    def uids(self) -> tuple[int, ...]:
        return tuple(self.messages)

    @property
    def count(self) -> int:
        return len(self.messages)

    def __len__(self) -> int:
        return len(self.messages)

    def __iter__(self):
        return iter(self.messages.values())

    def __contains__(self, uid: object) -> bool:
        return uid in self.messages

    def __getitem__(self, uid: int) -> Message:
        return self.messages[uid]

    def get(self, uid: int, default=None):
        return self.messages.get(uid, default)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "snapshot",
            "generation": self.generation,
            "mailbox": self.mailbox,
            "uidvalidity": self.uidvalidity,
            "messages": {uid: msg.as_dict() for uid, msg in self.messages.items()},
            "uids": self.uids,
            "complete": self.complete,
            "taken_at": self.taken_at,
            "query": self.query,
            "window": self.window,
            "gaps": [gap.as_dict() for gap in self.gaps],
        }


class ReplayEvent(dict):
    """Replay record supporting both mapping and attribute access."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


class CollectorError(RuntimeError):
    """Raised when the collector's mailbox state is invalid."""


class IdleCollector:
    """Collect a mailbox through IMAP IDLE with resumable UID reconciliation.

    ``imap`` is expected to be an :class:`imaplib2.IMAP4` instance (or a test
    double exposing ``select``, ``response('UIDVALIDITY')``, ``idle``,
    ``end_idle`` and ``uid('FETCH', ...)``).  The collector owns the selected
    mailbox while running; do not issue unrelated commands concurrently.

    The collector can run in a background thread with :meth:`start`, or in the
    current thread with :meth:`run`.  For deterministic replay, call
    :meth:`synchronize`, :meth:`listen`, :meth:`catch_up` explicitly.
    """

    def __init__(
        self,
        imap,
        mailbox: str = "INBOX",
        *,
        idle_timeout: Optional[float] = None,
        done_timeout: Optional[float] = None,
        fetcher: Optional[Callable[[int, Message], Any]] = None,
        materialize: Optional[Callable[[int, Message], Any]] = None,
        fetch_directory: Optional[Callable[[], Iterable[Any]]] = None,
        on_snapshot: Optional[Callable[[Snapshot], None]] = None,
        on_gap: Optional[Callable[[Gap], None]] = None,
        on_archive: Optional[Callable[[int, Any, bool], None]] = None,
        reconnect: Optional[Callable[[], Any]] = None,
        fetch_parts: str = DEFAULT_FETCH_PARTS,
        max_catchup_rounds: int = 8,
        auto_select: bool = True,
        leave_on_first_notification: bool = True,
    ) -> None:
        self._imap = imap
        self._mailbox = mailbox
        self._idle_timeout = idle_timeout
        self._done_timeout = done_timeout
        self._fetcher = fetcher or materialize
        self._fetch_directory = fetch_directory
        self._on_snapshot = on_snapshot
        self._on_gap = on_gap
        self._on_archive = on_archive
        self._reconnect = reconnect
        self._fetch_parts = fetch_parts
        self._max_catchup_rounds = max(1, int(max_catchup_rounds))
        self._auto_select = auto_select
        self._leave_on_first_notification = leave_on_first_notification

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._previous_observer = None

        self._selected = False
        self._selecting = False
        self._collecting = False
        self._uidvalidity: Optional[int] = None
        self._domain_hint: Optional[int] = None

        self._seq = 0                     # last notification sequence number
        self._window_num = 0
        self._query_num = 0
        self._gap_num = 0
        self._generation = 0

        self._current_window: Optional[str] = None
        self._last_window: Optional[str] = None
        self._catchup_window: Optional[str] = None
        self._window_records: dict[str, Window] = {}
        self._notifications: list[Notification] = []
        self._query_fetch_checks: list[
            tuple[int, Optional[int], Optional[frozenset], str]
        ] = []
        self._queries: list[Query] = []
        self._gaps: list[Gap] = []
        self._replay: list[ReplayEvent] = []

        self._messages: dict[int, Message] = {}
        self._query_records: dict[int, Message] = {}
        self._materials: dict[tuple[Optional[int], int], Any] = {}
        self._archived: dict[tuple[Optional[int], int], Any] = {}
        self._archive_duplicates = 0

        self._snapshot: Optional[Snapshot] = None
        self._complete = False
        self._domain_changed = False
        self._last_error: Optional[str] = None
        self._leave_requested = threading.Event()
        self._defer_snapshot_refresh = False
        self._snapshot_dirty = False

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def imap(self):
        return self._imap

    @property
    def mailbox(self) -> str:
        return self._mailbox

    @property
    def uidvalidity(self) -> Optional[int]:
        return self._uidvalidity

    @property
    def snapshot(self) -> Optional[Snapshot]:
        """The latest *complete* snapshot, never an unpublished query result."""
        return self._snapshot

    @property
    def latest(self) -> Optional[Snapshot]:
        return self._snapshot

    @property
    def directory(self) -> Mapping[int, Message]:
        snapshot = self._snapshot
        if snapshot is None:
            return MappingProxyType({})
        return snapshot.messages

    @property
    def messages(self) -> Mapping[int, Message]:
        return self.directory

    @property
    def complete(self) -> bool:
        return self._complete

    @property
    def gaps(self) -> tuple[Gap, ...]:
        return tuple(self._gaps)

    @property
    def open_gaps(self) -> tuple[Gap, ...]:
        return tuple(gap for gap in self._gaps if gap.open)

    @property
    def notifications(self) -> tuple[Notification, ...]:
        return tuple(self._notifications)

    @property
    def windows(self) -> tuple[Window, ...]:
        return tuple(self._window_records.values())

    @property
    def queries(self) -> tuple[Query, ...]:
        return tuple(self._queries)

    @property
    def archived(self) -> Mapping[tuple[Optional[int], int], Any]:
        return MappingProxyType(self._archived)

    @property
    def archived_uids(self) -> tuple[int, ...]:
        return tuple(uid for (_uv, uid) in self._archived)

    @property
    def archive_duplicates(self) -> int:
        return self._archive_duplicates

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    @property
    def state(self) -> dict[str, Any]:
        return self.status()

    @property
    def phase(self) -> str:
        """One of ``detached``, ``listening``, ``done``, or ``catchup``."""

        with self._lock:
            if self._catchup_window is not None:
                return "catchup"
            if self._current_window is not None:
                return "done" if self._leave_requested.is_set() else "listening"
            return "detached"

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "mailbox": self._mailbox,
                "uidvalidity": self._uidvalidity,
                "phase": self.phase,
                "complete": self._complete,
                "generation": self._generation,
                "count": len(self._messages),
                "windows": len(self._window_records),
                "notifications": len(self._notifications),
                "queries": len(self._queries),
                "gaps": len(self._gaps),
                "open_gaps": len(self.open_gaps),
                "archived": len(self._archived),
                "archive_duplicates": self._archive_duplicates,
                "last_error": self._last_error,
            }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> "IdleCollector":
        """Start the background listener (selecting and syncing first)."""

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stop.clear()
            self._install_observer()
            self._thread = threading.Thread(
                target=self._run, name="idle-collector", daemon=True
            )
            self._thread.start()
        return self

    def run(self) -> None:
        """Run the collector in the current thread until stopped."""

        self._install_observer()
        self._run()

    def stop(self, timeout: Optional[float] = None) -> None:
        self._stop.set()
        try:
            if hasattr(self._imap, "end_idle"):
                # Never block shutdown forever on an unresponsive server.
                wait = self._done_timeout if self._done_timeout is not None else 5
                self._imap.end_idle(timeout=wait)
        except Exception:
            pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def join(self, timeout: Optional[float] = None) -> None:
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _run(self) -> None:
        if self._auto_select:
            try:
                self._ensure_selected(self._mailbox)
            except Exception as exc:
                self._record_gap("select failed: %s" % (exc,), None)
                self._last_error = str(exc)
                return
        try:
            self.synchronize()
        except Exception as exc:
            self._record_gap("initial synchronise failed: %s" % (exc,), None)
            self._last_error = str(exc)

        while not self._stop.is_set():
            try:
                self.collect_once()
            except Exception as exc:
                self._last_error = str(exc)
                # _listen_once has already closed its window on the error
                # path; attribute the gap to that window if possible.
                window = self._current_window or self._last_window
                if not self.open_gaps:
                    self._record_gap("connection lost: %s" % (exc,), window)
                self._end_listen(window, reason="error")
                if self._reconnect is None:
                    break
                try:
                    replacement = self._reconnect()
                    if replacement is not None:
                        self._imap = replacement
                    self._selected = False
                    self._install_observer()
                    if self._auto_select:
                        self._ensure_selected(self._mailbox)
                    self.synchronize()
                except Exception as reconnect_exc:
                    self._last_error = str(reconnect_exc)
                    self._record_gap(
                        "reconnect failed: %s" % (reconnect_exc,), None
                    )
                    break

    # ------------------------------------------------------------------
    # Selection and initial synchronisation
    # ------------------------------------------------------------------

    def _ensure_selected(self, mailbox: Optional[str] = None,
                         force: bool = False) -> None:
        target = mailbox or self._mailbox
        if self._selected and target == self._mailbox and not force:
            return

        previous_mailbox = self._mailbox
        previous_uidvalidity = self._uidvalidity
        same_domain = True
        if self._selected and target != previous_mailbox:
            same_domain = False
            self._record_gap(
                "mailbox switch: %s -> %s" % (previous_mailbox, target),
                self._current_window,
            )

        self._selecting = True
        try:
            result = self._imap.select(target)
            typ = result[0] if isinstance(result, tuple) else "OK"
            if typ not in (None, "OK"):
                raise RuntimeError("SELECT %s failed: %r" % (target, result))
            uidvalidity = self._read_uidvalidity()
        finally:
            self._selecting = False

        if (
            previous_uidvalidity is not None
            and uidvalidity is not None
            and uidvalidity != previous_uidvalidity
        ):
            same_domain = False
            self._record_gap(
                "UIDVALIDITY changed: %s -> %s"
                % (previous_uidvalidity, uidvalidity),
                self._current_window,
            )

        if not same_domain:
            self._messages = {}
            self._complete = False
            self._domain_changed = True

        self._mailbox = target
        self._uidvalidity = uidvalidity
        self._selected = True
        if hasattr(self._imap, "untagged_observer"):
            self._install_observer()

    def switch_mailbox(self, mailbox: str, *, synchronize: bool = True):
        """Leave IDLE, record a gap, select another mailbox and resync.

        The gap stays unresolved: scanning the new mailbox cannot recover the
        old mailbox's notification history.
        """

        try:
            if hasattr(self._imap, "end_idle"):
                self._imap.end_idle(timeout=self._done_timeout)
        except Exception:
            pass
        with self._lock:
            previous = self._mailbox
            self._record_gap_locked(
                "mailbox switch: %s -> %s" % (previous, mailbox),
                self._current_window,
            )
            self._messages = {}
            self._complete = False
            self._selected = False
            self._uidvalidity = None
        self._ensure_selected(mailbox, force=True)
        if synchronize:
            return self.synchronize()
        return None

    def _require_selected(self) -> None:
        if self._selected:
            return
        if not self._auto_select:
            raise CollectorError(
                "collector is not attached to a selected mailbox; "
                "call attach() or construct with auto_select=True"
            )
        self._ensure_selected(self._mailbox)

    def attach(self, mailbox: Optional[str] = None) -> None:
        """Mark the collector as attached after an external SELECT.

        Use this with ``auto_select=False`` when the caller owns selection.
        """

        with self._lock:
            if mailbox is not None:
                self._mailbox = mailbox
            uidvalidity = self._read_uidvalidity()
            if (
                self._uidvalidity is not None
                and uidvalidity is not None
                and uidvalidity != self._uidvalidity
            ):
                self._record_gap_locked(
                    "UIDVALIDITY changed on attach: %s -> %s"
                    % (self._uidvalidity, uidvalidity),
                    self._current_window,
                )
                self._messages = {}
                self._complete = False
            self._uidvalidity = uidvalidity
            self._selected = True
        self._install_observer()

    def _read_uidvalidity(self) -> Optional[int]:
        value = None
        response = getattr(self._imap, "response", None)
        if callable(response):
            try:
                typ, dat = response("UIDVALIDITY")
                for item in dat or ():
                    parsed = _as_int(item)
                    if parsed is not None:
                        value = parsed
            except Exception:
                pass
        if value is None:
            attr = getattr(self._imap, "uidvalidity", None)
            value = _as_int(attr) if attr is not None else None
        return value

    # ------------------------------------------------------------------
    # Observer / notification capture
    # ------------------------------------------------------------------

    def _install_observer(self) -> None:
        if not hasattr(self._imap, "untagged_observer"):
            return
        existing = getattr(self._imap, "untagged_observer", None)
        if existing is self._observe:
            return
        self._previous_observer = existing
        try:
            self._imap.untagged_observer = self._observe
        except Exception:
            self._previous_observer = None

    def _observe(self, event: Mapping[str, Any]) -> None:
        """Installed on the IMAP client; called in the reader/handler thread."""

        previous = self._previous_observer
        if previous is not None:
            try:
                previous(event)
            except Exception:
                pass

        typ = _as_text(event.get("type")).upper()
        data = event.get("data")

        if typ == "IDLE":
            return

        if typ == "UIDVALIDITY":
            parsed = _as_int(data)
            if parsed is not None and not self._selecting:
                with self._lock:
                    if self._uidvalidity is None:
                        self._uidvalidity = parsed
                    elif (
                        parsed != self._uidvalidity
                        and not self._domain_changed
                    ):
                        self._domain_hint = parsed
                        self._domain_changed = True
                        self._complete = False
                        self._record_gap(
                            "UIDVALIDITY notification: %s -> %s"
                            % (self._uidvalidity, parsed),
                            self._current_window,
                        )
            return

        if typ not in NOTIFICATION_TYPES:
            return

        with self._lock:
            if self._selecting:
                return

            phase = self._notification_phase(event)

            # FETCH responses belonging to our own UID FETCH are query output,
            # not unsolicited notifications.  They still have to be checked
            # against the result: a flag change racing with the scan makes the
            # result stale.  Listening-window FETCHes remain notifications.
            if (
                typ == "FETCH"
                and self._current_window is None
                and phase in ("catchup", "query", "other")
            ):
                if self._collecting:
                    parsed = parse_fetch(data)
                    if parsed.get("uid") is not None:
                        self._query_fetch_checks.append((
                            self._seq + 1,
                            parsed["uid"],
                            parsed.get("flags"),
                            _as_text(event.get("pending_names")),
                        ))
                return

            parsed = parse_notification(typ, data)
            self._seq += 1
            window = self._current_window

            uidvalidity = event.get("uidvalidity")
            if uidvalidity is None:
                uidvalidity = self._uidvalidity

            notification = Notification(
                seq=self._seq,
                type=typ,
                data=data,
                mailbox=event.get("mailbox", self._mailbox),
                uidvalidity=uidvalidity,
                window=window,
                phase=phase,
                sequence_hint=parsed.get("sequence_hint"),
                uid=parsed.get("uid"),
                uids=tuple(parsed.get("uids") or ()),
                flags=parsed.get("flags"),
                at=time.time(),
            )
            self._notifications.append(notification)
            window_record = self._window_records.get(window) if window else None
            if window_record is not None:
                window_record.notifications += 1
            self._replay.append(ReplayEvent(notification.as_dict()))

            # Leave IDLE promptly: waiting for the IDLE timeout would hold the
            # directory stale.  The DONE handshake still drains comments that
            # were already in flight, and those keep the window record.
            if (
                self._leave_on_first_notification
                and self._current_window is not None
                and phase == "idle"
            ):
                self._leave_requested.set()

            # A FETCH carrying a UID may safely update flags for an already
            # confirmed UID in the same domain.  It never creates identity by
            # sequence number, and never adds an unconfirmed UID.
            if typ == "FETCH" and parsed.get("uid") is not None:
                uid = parsed["uid"]
                if (
                    uid in self._messages
                    and uidvalidity == self._uidvalidity
                    and parsed.get("flags") is not None
                ):
                    old = self._messages[uid]
                    self._messages[uid] = Message(
                        uid=uid,
                        flags=parsed["flags"],
                        internaldate=old.internaldate,
                        size=old.size,
                        sequence_hint=parsed.get("sequence_hint"),
                        material=old.material,
                    )

    def _notification_phase(self, event: Mapping[str, Any]) -> str:
        pending = event.get("pending_names") or ()
        if "FETCH" in pending or "UID" in pending:
            return "query"
        phase = event.get("idle_phase")
        if phase == "idle":
            return "idle"
        if phase == "done_sent":
            return "done_sent"
        if self._catchup_window is not None and self._current_window is None:
            return "catchup"
        if self._current_window is not None:
            return "idle"
        return "other"

    def feed(self, typ: str, data: Any = b"", **kw) -> Optional[Notification]:
        """Inject a notification (useful for replay tests without a socket)."""

        event = {
            "type": typ,
            "data": data,
            "mailbox": kw.get("mailbox", self._mailbox),
            "uidvalidity": kw.get("uidvalidity", self._uidvalidity),
            "idle_phase": kw.get("idle_phase", "idle"),
            "pending_names": kw.get("pending_names", ()),
            "force": True,
        }
        before = len(self._notifications)
        self._observe(event)
        if len(self._notifications) == before:
            return None
        return self._notifications[-1]

    # ------------------------------------------------------------------
    # Listening windows
    # ------------------------------------------------------------------

    def _begin_window(self) -> Window:
        with self._lock:
            self._window_num += 1
            window_id = "W%d" % self._window_num
            window = Window(
                id=window_id,
                mailbox=self._mailbox,
                uidvalidity=self._uidvalidity,
                start_seq=self._seq + 1,
                started_at=time.time(),
            )
            self._window_records[window_id] = window
            self._current_window = window_id
            self._last_window = window_id
            self._catchup_window = None
            self._replay.append(ReplayEvent({
                "kind": "window_start",
                "id": window_id,
                "mailbox": self._mailbox,
                "uidvalidity": self._uidvalidity,
                "start_seq": window.start_seq,
                "at": window.started_at,
            }))
            return window

    def _end_listen(self, window_id: Optional[str], reason: str) -> None:
        with self._lock:
            if self._current_window == window_id:
                self._current_window = None
            window = self._window_records.get(window_id) if window_id else None
            if window is not None and window.ended_at is None:
                window.end_seq = self._seq
                window.ended_at = time.time()
                window.reason = reason
                self._replay.append(ReplayEvent({
                    "kind": "window_end",
                    "id": window.id,
                    "end_seq": window.end_seq,
                    "reason": reason,
                    "notifications": window.notifications,
                    "at": window.ended_at,
                }))

    def _finish_done(self, timeout: Optional[float] = None) -> bool:
        """Leave IDLE and require the tagged completion of DONE."""

        end_idle = getattr(self._imap, "end_idle", None)
        if end_idle is None:
            return True
        wait = self._done_timeout if timeout is None else timeout
        try:
            result = end_idle(timeout=wait)
        except TypeError:
            result = end_idle(wait)
        return result is not False

    def _watch_leave(self, window_id: str) -> None:
        """Ask IDLE to send DONE without waiting (`idle()` does the waiting)."""

        self._leave_requested.wait()
        with self._lock:
            if self._current_window != window_id:
                return
        send_done = getattr(self._imap, "_end_idle", None)
        if send_done is None:
            send_done = getattr(self._imap, "end_idle", None)
        try:
            if send_done is not None:
                send_done()
        except Exception:
            pass

    def _listen_once(self) -> Optional[Window]:
        window = self._begin_window()
        self._leave_requested.clear()
        watcher = None
        if self._leave_on_first_notification:
            watcher = threading.Thread(
                target=self._watch_leave,
                args=(window.id,),
                name="idle-leave",
                daemon=True,
            )
            watcher.start()
        reason = "notification"
        failed = False
        try:
            idle = getattr(self._imap, "idle")
            idle(timeout=self._idle_timeout)
            if window.notifications == 0:
                reason = "idle_timeout"
            if self._stop.is_set():
                reason = "stopped"
            return window
        except Exception:
            reason = "error"
            failed = True
            raise
        finally:
            self._leave_requested.set()
            if watcher is not None:
                watcher.join(1)
            done_ok = True
            try:
                if failed:
                    # After an IDLE error the connection may be gone; never
                    # block forever waiting for a DONE that cannot arrive.
                    done_ok = self._finish_done(timeout=1)
                else:
                    done_ok = self._finish_done()
            except Exception:
                # The connection died while draining IDLE.  The outer error
                # path records the disconnect; the window still gets closed.
                done_ok = False
            if not done_ok:
                reason = "error" if failed else "done_timeout"
                if not failed:
                    self._record_gap(
                        "DONE handshake did not complete", window.id
                    )
                self._selected = False
            self._end_listen(window.id, reason)

    def listen(self) -> Optional[Window]:
        """Enter one IDLE window and return after its DONE handshake."""

        with self._lock:
            self._collecting = True
        try:
            return self._listen_once()
        finally:
            with self._lock:
                self._collecting = False

    def collect_once(self) -> Optional[Snapshot]:
        """Listen for one change, complete DONE, reconcile, and publish.

        Returns the newly published snapshot, or ``None`` if the attempt was
        interrupted or had to leave the directory incomplete.
        """

        self._require_selected()
        if self._snapshot is None:
            self.synchronize()

        window = self.listen()
        if window is None:
            return None
        if self._stop.is_set():
            return None
        if window.reason == "done_timeout":
            return None
        return self._catch_up(window.id, phase="after_idle")

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------

    def synchronize(self) -> Optional[Snapshot]:
        """Reconcile the directory without an IDLE window (initial/full sync)."""

        self._require_selected()
        self._install_observer()
        return self._catch_up(None, phase="sync")

    def catch_up(self, window: Optional[str] = None,
                 phase: str = "after_idle") -> Optional[Snapshot]:
        """Run a full UID-domain reconciliation.

        With no arguments this is equivalent to :meth:`synchronize`.  Passing
        a listening-window id associates the query record with that window.
        """

        if window is None:
            return self.synchronize()
        self._require_selected()
        snapshot = self._catch_up(window, phase=phase)
        return snapshot

    def sync(self) -> Optional[Snapshot]:
        return self.synchronize()

    catchup = catch_up
    poll_once = collect_once
    run_once = collect_once
    listen_once = listen

    def is_archived(self, uid: int,
                    uidvalidity: Optional[int] = None) -> bool:
        domain = self._uidvalidity if uidvalidity is None else uidvalidity
        with self._lock:
            return (domain, int(uid)) in self._archived

    def _fetch_directory_response(self) -> tuple[str, Iterable[Any]]:
        if self._fetch_directory is not None:
            return "OK", self._fetch_directory()
        uid = getattr(self._imap, "uid", None)
        if callable(uid):
            return uid("FETCH", "1:*", self._fetch_parts)
        fetch = getattr(self._imap, "fetch", None)
        if callable(fetch):
            return fetch("1:*", self._fetch_parts)
        raise AttributeError("IMAP object has neither uid() nor fetch()")

    def _catch_up(self, window: Optional[str], phase: str) -> Optional[Snapshot]:
        self._install_observer()
        with self._lock:
            self._collecting = True
            self._catchup_window = window
            # A UIDVALIDITY notification invalidates the previous domain.
            if self._domain_changed and self._domain_hint is not None:
                self._uidvalidity = self._domain_hint
                self._messages = {}
                self._domain_hint = None
                self._complete = False
            self._domain_changed = False

        try:
            reason = None
            for _attempt in range(self._max_catchup_rounds):
                with self._lock:
                    if self._domain_changed and self._domain_hint is not None:
                        # The server announced a new UIDVALIDITY while a scan
                        # was running.  UIDs from the old domain are not
                        # comparable, so discard them and scan the new domain.
                        self._uidvalidity = self._domain_hint
                        self._messages = {}
                        self._domain_hint = None
                        self._domain_changed = False
                        self._complete = False
                    marker = self._seq
                    self._query_fetch_checks = []
                query = self._begin_query(window, phase)
                try:
                    typ, dat = self._fetch_directory_response()
                    records, uids = self._parse_directory(dat)
                    query.ok = typ == "OK"
                    if not query.ok:
                        query.error = "UID FETCH returned %r" % (typ,)
                except Exception as exc:
                    records, uids = {}, ()
                    query.ok = False
                    query.error = str(exc)
                    self._finish_query(query)
                    self._record_gap("catch-up query failed: %s" % (exc,), window)
                    self._complete = False
                    self._last_error = str(exc)
                    return None

                query.uids = uids
                query.covered_range = self._covered_range(uids)

                with self._lock:
                    dirty, reason = self._superseded_locked(marker, records)
                    if not query.ok and not dirty:
                        dirty, reason = True, query.error or "failed query"
                    query.superseded = dirty
                    query.reason = reason
                    query.end_seq = self._seq
                    query.finished_at = time.time()
                    if not dirty:
                        self._query_records = records
                self._finish_query(query)

                if not dirty:
                    with self._lock:
                        snapshot = self._publish_locked(query)
                    self._after_publish(snapshot)
                    return snapshot

                # Never publish the stale result.  Re-query in the same
                # UID domain until a quiet reconciliation is observed.

            self._record_gap(
                "catch-up did not converge after %d rounds: %s"
                % (self._max_catchup_rounds, reason or "changes kept arriving"),
                window,
            )
            self._complete = False
            return None
        finally:
            with self._lock:
                self._catchup_window = None
                self._collecting = False

    def _begin_query(self, window: Optional[str], phase: str) -> Query:
        with self._lock:
            self._query_num += 1
            query = Query(
                id="Q%d" % self._query_num,
                window=window,
                phase=phase,
                mailbox=self._mailbox,
                uidvalidity=self._uidvalidity,
                command="UID FETCH",
                requested_range="1:*",
                parts=self._fetch_parts,
                start_seq=self._seq + 1,
                started_at=time.time(),
            )
            self._queries.append(query)
            self._replay.append(ReplayEvent({
                "kind": "query_start",
                "id": query.id,
                "window": window,
                "phase": phase,
                "mailbox": self._mailbox,
                "uidvalidity": self._uidvalidity,
                "command": query.command,
                "requested_range": query.requested_range,
                "queried_range": query.requested_range,
                "parts": query.parts,
                "start_seq": query.start_seq,
                "at": query.started_at,
            }))
            return query

    def _finish_query(self, query: Query) -> None:
        with self._lock:
            if query.end_seq is None:
                query.end_seq = self._seq
            if query.finished_at is None:
                query.finished_at = time.time()
            if query.replay_written:
                return
            query.replay_written = True
            self._replay.append(ReplayEvent({
                "kind": "query_end",
                "id": query.id,
                "window": query.window,
                "phase": query.phase,
                "uidvalidity": query.uidvalidity,
                "end_seq": query.end_seq,
                "uids": query.uids,
                "queried_range": query.requested_range,
                "covered_range": query.covered_range,
                "ok": query.ok,
                "error": query.error,
                "superseded": query.superseded,
                "reason": query.reason,
                "published_generation": query.published_generation,
                "at": query.finished_at,
            }))

    def _parse_directory(
        self, dat: Iterable[Any]
    ) -> tuple[dict[int, Message], tuple[int, ...]]:
        previous = self._messages
        records: dict[int, Message] = {}
        saw_fetch = False
        missing_uid = 0

        for raw in dat or ():
            if raw is None:
                continue
            header = _fetch_header(raw)
            if not header.strip():
                continue
            if not re.match(r"^\s*\d+\s+\(", header):
                continue
            saw_fetch = True
            parsed = parse_fetch(raw)
            uid = parsed.get("uid")
            if uid is None:
                missing_uid += 1
                continue
            old = previous.get(uid)
            material = self._materials.get((self._uidvalidity, uid))
            records[uid] = Message(
                uid=uid,
                flags=(parsed["flags"] if parsed.get("flags") is not None
                       else (old.flags if old else frozenset())),
                internaldate=(parsed.get("internaldate")
                              or (old.internaldate if old else None)),
                size=(parsed["size"] if parsed.get("size") is not None
                      else (old.size if old else None)),
                sequence_hint=parsed.get("sequence_hint"),
                material=(material if material is not None
                          else (old.material if old else None)),
            )

        if saw_fetch and missing_uid and not records:
            # The server returned sequence-only data.  That cannot identify
            # messages across EXPUNGE renumbering, so the query is unusable.
            raise RuntimeError("UID FETCH response contained no UIDs")

        return records, tuple(sorted(records))

    @staticmethod
    def _covered_range(uids: tuple[int, ...]) -> Optional[str]:
        if not uids:
            return None
        if len(uids) == 1:
            return str(uids[0])
        return "%d:%d" % (uids[0], uids[-1])

    def _superseded_locked(
        self, marker: int, records: Mapping[int, Message]
    ) -> tuple[bool, Optional[str]]:
        """Did a real change arrive after we started this reconciliation?

        The caller holds ``self._lock`` so that a notification cannot slip
        between this check and publication.
        """

        if self._domain_changed:
            return True, "UIDVALIDITY changed during catch-up"

        notifications = [
            notification
            for notification in self._notifications
            if notification.seq > marker
        ]

        # A FETCH for a UID outside our own query result is an unsolicited
        # change that raced with the scan.  A matching FETCH whose flags
        # contradict the result is likewise a stale-scan signal.
        for seq, uid, flags, pending in self._query_fetch_checks:
            if seq <= marker:
                continue
            if "UID" not in pending and "FETCH" not in pending:
                # Not part of our own UID FETCH command.
                return True, "unsolicited FETCH during catch-up"
            record = records.get(uid)
            if record is None:
                return True, "FETCH for UID %s outside query result" % uid
            if (
                flags is not None
                and record.flags != flags
            ):
                return True, "UID %s flags moved during catch-up" % uid

        for notification in notifications:
            if notification.mailbox not in (None, self._mailbox):
                return True, "mailbox changed during catch-up"
            if (
                notification.uidvalidity is not None
                and self._uidvalidity is not None
                and notification.uidvalidity != self._uidvalidity
            ):
                return True, "UIDVALIDITY changed during catch-up"

            if notification.type in STRUCTURAL_TYPES:
                return True, "%s arrived during catch-up" % notification.type

            if notification.type == "FETCH":
                uid = notification.uid
                if uid is None:
                    # Sequence-only data is not identity and cannot be used to
                    # invalidate or confirm the UID directory.
                    continue
                record = records.get(uid)
                if record is None:
                    return True, "FETCH for UID %s outside query result" % uid
                if (
                    notification.flags is not None
                    and record.flags != notification.flags
                ):
                    return True, "UID %s flags changed during catch-up" % uid

        return False, None

    def _publish_locked(self, query: Query) -> Snapshot:
        records = dict(self._query_records)
        self._generation += 1
        query.published_generation = self._generation
        self._messages = dict(records)
        self._complete = True
        self._close_open_gap_locked(resolved=True)
        snapshot = Snapshot(
            generation=self._generation,
            mailbox=self._mailbox,
            uidvalidity=self._uidvalidity,
            messages=MappingProxyType(dict(records)),
            complete=True,
            taken_at=time.time(),
            query=query.id,
            window=query.window,
            gaps=tuple(self._gaps),
        )
        self._snapshot = snapshot
        self._replay.append(ReplayEvent({
            "kind": "snapshot",
            "generation": snapshot.generation,
            "mailbox": snapshot.mailbox,
            "uidvalidity": snapshot.uidvalidity,
            "uids": snapshot.uids,
            "query": query.id,
            "window": query.window,
            "complete": True,
            "at": snapshot.taken_at,
        }))
        return snapshot

    def _after_publish(self, snapshot: Snapshot) -> None:
        with self._lock:
            self._defer_snapshot_refresh = True
        try:
            self._archive_new(snapshot)
        finally:
            with self._lock:
                self._defer_snapshot_refresh = False
                refresh = self._snapshot_dirty
                self._snapshot_dirty = False
            if refresh:
                self._refresh_snapshot()
        if self._on_snapshot is not None:
            try:
                self._on_snapshot(self.snapshot)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Material reuse and idempotent archive
    # ------------------------------------------------------------------

    def _archive_new(self, snapshot: Snapshot) -> None:
        for uid in snapshot.uids:
            key = (snapshot.uidvalidity, uid)
            if key in self._archived and self._archived[key] is not None:
                continue
            message = snapshot.messages[uid]
            material = self._materials.get(key, message.material)
            if material is None and self._fetcher is not None:
                try:
                    material = self._fetcher(uid, message)
                except Exception as exc:
                    self._last_error = "material fetch failed for UID %s: %s" % (
                        uid,
                        exc,
                    )
                    # The UID is confirmed but its material is not.  Leave it
                    # unarchived so a later pass can retry; because archive
                    # identity is the UID, the retry cannot create a duplicate.
                    continue
                if material is not None:
                    with self._lock:
                        self._materials[key] = material
            if material is not None:
                self.archive(uid, material, emit=False)

    def material_for(self, uid: int,
                     uidvalidity: Optional[int] = None) -> Any:
        domain = self._uidvalidity if uidvalidity is None else uidvalidity
        with self._lock:
            return self._materials.get((domain, int(uid)))

    def _attach_to_message_locked(self, uid: int, domain: Optional[int],
                                  material: Any) -> None:
        if domain != self._uidvalidity or uid not in self._messages:
            return
        old = self._messages[uid]
        if old.material is not None:
            return
        self._messages[uid] = Message(
            uid=old.uid,
            flags=old.flags,
            internaldate=old.internaldate,
            size=old.size,
            sequence_hint=old.sequence_hint,
            material=material,
        )
        self._snapshot_dirty = True
        if not self._defer_snapshot_refresh:
            self._refresh_snapshot()

    def _refresh_snapshot(self) -> None:
        """Publish an immutable refresh of the current snapshot."""

        with self._lock:
            current = self._snapshot
            if current is None or current.uidvalidity != self._uidvalidity:
                return
            self._snapshot = Snapshot(
                generation=current.generation,
                mailbox=current.mailbox,
                uidvalidity=current.uidvalidity,
                messages=MappingProxyType(dict(self._messages)),
                complete=current.complete,
                taken_at=current.taken_at,
                query=current.query,
                window=current.window,
                gaps=current.gaps,
            )

    def attach_material(self, uid: int, material: Any,
                        uidvalidity: Optional[int] = None) -> bool:
        """Attach reused material to an already archived UID.

        Returns ``False`` if this archive identity already had material; the
        stored material is never replaced, so a duplicate delivery cannot
        change the archived message.
        """

        domain = self._uidvalidity if uidvalidity is None else uidvalidity
        key = (domain, int(uid))
        with self._lock:
            if self._materials.get(key) is not None:
                return False
            self._materials[key] = material
            if key in self._archived:
                self._archived[key] = material
            self._attach_to_message_locked(uid, domain, material)
        return True

    def archive(self, uid: int, material: Any = None, *,
                uidvalidity: Optional[int] = None,
                emit: bool = True) -> bool:
        """Archive a message by ``(UIDVALIDITY, UID)``.

        Returns ``True`` only for the first archive of this identity.  Repeated
        notifications or repeated catch-up queries return ``False`` and record
        a duplicate in the replay log.  A duplicate with material can fill in
        a previously empty material slot, but never replaces existing material.
        """

        domain = self._uidvalidity if uidvalidity is None else uidvalidity
        key = (domain, int(uid))
        with self._lock:
            if key in self._archived:
                self._archive_duplicates += 1
                if material is not None and self._archived[key] is None:
                    self._archived[key] = material
                    self._materials[key] = material
                    self._attach_to_message_locked(uid, domain, material)
                self._replay.append(ReplayEvent({
                    "kind": "archive",
                    "uid": int(uid),
                    "uidvalidity": domain,
                    "duplicate": True,
                    "at": time.time(),
                }))
                return False
            if material is None:
                material = self._materials.get(key)
            self._archived[key] = material
            self._materials[key] = material
            self._attach_to_message_locked(uid, domain, material)
            self._replay.append(ReplayEvent({
                "kind": "archive",
                "uid": int(uid),
                "uidvalidity": domain,
                "duplicate": False,
                "at": time.time(),
            }))

        if emit and self._on_archive is not None:
            try:
                self._on_archive(int(uid), material, True)
            except Exception:
                pass
        return True

    def material_for(self, uid: int,
                     uidvalidity: Optional[int] = None) -> Any:
        domain = self._uidvalidity if uidvalidity is None else uidvalidity
        with self._lock:
            return self._materials.get((domain, int(uid)))

    def checkpoint(self) -> dict[str, Any]:
        """Serialisable resumable state.

        Notifications, windows and replay are history; the checkpoint carries
        the confirmed identity domain, confirmed UIDs and archive keys so a
        restarted collector can avoid re-archiving and re-fetching material.
        """

        with self._lock:
            return {
                "version": 1,
                "mailbox": self._mailbox,
                "uidvalidity": self._uidvalidity,
                "generation": self._generation,
                "complete": self._complete,
                "uids": tuple(sorted(self._messages)),
                "archived": sorted(
                    ([domain, uid] for domain, uid in self._archived),
                    key=lambda item: (item[0] is not None, item[0] or 0, item[1]),
                ),
                "gaps": [
                    {
                        "id": gap.id,
                        "reason": gap.reason,
                        "mailbox": gap.mailbox,
                        "uidvalidity": gap.uidvalidity,
                        "start_seq": gap.start_seq,
                        "end_seq": gap.end_seq,
                        "open": gap.open,
                        "resolved": gap.resolved,
                    }
                    for gap in self._gaps
                ],
                "last_seq": self._seq,
            }

    def restore(self, checkpoint: Mapping[str, Any]) -> bool:
        """Restore UID-domain confirmation from :meth:`checkpoint`.

        Returns ``False`` if the checkpoint belongs to another mailbox.  When
        its UIDVALIDITY differs from the server's current domain, the
        checkpoint's UIDs are discarded and a gap is recorded: the same UID
        numbers would mean different messages.
        """

        if checkpoint.get("mailbox") not in (None, self._mailbox):
            return False
        with self._lock:
            saved_domain = checkpoint.get("uidvalidity")
            if saved_domain is not None and (
                self._uidvalidity is None or saved_domain == self._uidvalidity
            ):
                if self._uidvalidity is None:
                    self._uidvalidity = saved_domain
                for value in checkpoint.get("archived", ()):
                    domain, uid = value
                    if domain == self._uidvalidity:
                        key = (domain, int(uid))
                        self._archived.setdefault(key, None)
                # Identity is only "known" again after a fresh scan; the
                # restored keys exist solely to keep archiving idempotent.
                self._complete = False
            elif saved_domain != self._uidvalidity:
                self._record_gap_locked(
                    "restored checkpoint UIDVALIDITY %s != current %s"
                    % (saved_domain, self._uidvalidity),
                    None,
                )
                return False
        return True

    def save(self, path: str) -> None:
        """Write :meth:`checkpoint` as JSON."""

        import json

        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.checkpoint(), handle, indent=2)

    def load(self, path: str) -> bool:
        """Read JSON written by :meth:`save` and call :meth:`restore`."""

        import json

        with open(path, "r", encoding="utf-8") as handle:
            return self.restore(json.load(handle))

    # ------------------------------------------------------------------
    # Gaps
    # ------------------------------------------------------------------

    def _record_gap(self, reason: str, window: Optional[str]) -> Gap:
        with self._lock:
            return self._record_gap_locked(reason, window)

    def _record_gap_locked(self, reason: str, window: Optional[str]) -> Gap:
        # A single incident is one gap, not one gap per caught exception.
        for gap in reversed(self._gaps):
            if gap.open and gap.reason == reason:
                return gap
        self._gap_num += 1
        gap = Gap(
            id=self._gap_num,
            reason=reason,
            mailbox=self._mailbox,
            uidvalidity=self._uidvalidity,
            start_seq=self._seq + 1,
            end_seq=None,
            window=window,
        )
        self._gaps.append(gap)
        self._complete = False
        self._invalidate_snapshot_locked()
        self._replay.append(ReplayEvent(gap.as_dict()))
        if self._on_gap is not None:
            try:
                self._on_gap(gap)
            except Exception:
                pass
        return gap

    def _invalidate_snapshot_locked(self) -> None:
        """Keep the last directory visible, but stop claiming it is complete."""

        current = self._snapshot
        if current is None or not current.complete:
            return
        self._snapshot = Snapshot(
            generation=current.generation,
            mailbox=current.mailbox,
            uidvalidity=current.uidvalidity,
            messages=current.messages,
            complete=False,
            taken_at=current.taken_at,
            query=current.query,
            window=current.window,
            gaps=tuple(self._gaps),
        )

    def _close_open_gap_locked(self, resolved: bool = False) -> None:
        for gap in self._gaps:
            if gap.open:
                gap.end_seq = self._seq if self._seq >= gap.start_seq else None
                gap.open = False
                # A full scan resolves a gap only when it is still the same
                # mailbox and UID domain.  A switch or UIDVALIDITY change
                # makes the old history unrecoverable, so it stays unresolved.
                gap.resolved = bool(
                    resolved
                    and gap.mailbox == self._mailbox
                    and gap.uidvalidity == self._uidvalidity
                )
                self._replay.append(ReplayEvent({
                    "kind": "gap_close",
                    "id": gap.id,
                    "end_seq": gap.end_seq,
                    "resolved": gap.resolved,
                    "at": time.time(),
                }))

    def close_gap(self, gap_id: Optional[int] = None,
                  resolved: bool = False) -> int:
        """Explicitly close open gaps (for example after manual recovery)."""

        closed = 0
        with self._lock:
            for gap in self._gaps:
                if gap.open and (gap_id is None or gap.id == gap_id):
                    gap.end_seq = self._seq if self._seq >= gap.start_seq else None
                    gap.open = False
                    gap.resolved = resolved
                    closed += 1
                    self._replay.append(ReplayEvent({
                        "kind": "gap_close",
                        "id": gap.id,
                        "end_seq": gap.end_seq,
                        "resolved": resolved,
                        "at": time.time(),
                    }))
        return closed

    # ------------------------------------------------------------------
    # Replay
    # ------------------------------------------------------------------

    def replay(self) -> list[ReplayEvent]:
        """Ordered records: windows, notifications, queries, gaps, archives."""

        with self._lock:
            return [ReplayEvent(event) for event in self._replay]

    replay_log = replay

    @property
    def events(self) -> tuple[ReplayEvent, ...]:
        return tuple(self.replay())

    def notifications_for_window(self, window: str) -> tuple[Notification, ...]:
        return tuple(
            notification
            for notification in self._notifications
            if notification.window == window
        )

    def queries_for_window(self, window: Optional[str]) -> tuple[Query, ...]:
        return tuple(query for query in self._queries if query.window == window)

    def was_uid_queried(self, uid: int,
                        uidvalidity: Optional[int] = None) -> bool:
        domain = self._uidvalidity if uidvalidity is None else uidvalidity
        return any(
            query.ok
            and query.uidvalidity == domain
            and uid in query.uids
            for query in self._queries
        )

    def queried_ranges(self) -> tuple[tuple[str, Optional[str], str], ...]:
        """``(query_id, uidvalidity, requested_range)`` for successful scans."""

        return tuple(
            (query.id, query.covered_range, query.requested_range)
            for query in self._queries
            if query.ok
        )

    def format_replay(self) -> str:
        lines = []
        for event in self.replay():
            kind = event.get("kind")
            if kind == "window_start":
                lines.append(
                    "WINDOW %s start mailbox=%s uidvalidity=%s notifications>=%s"
                    % (
                        event.get("id"),
                        event.get("mailbox"),
                        event.get("uidvalidity"),
                        event.get("start_seq"),
                    )
                )
            elif kind == "notification":
                lines.append(
                    "  NOTIFY seq=%s type=%s window=%s phase=%s uid=%s seqhint=%s"
                    % (
                        event.get("seq"),
                        event.get("type"),
                        event.get("window"),
                        event.get("phase"),
                        event.get("uid"),
                        event.get("sequence_hint"),
                    )
                )
            elif kind == "window_end":
                lines.append(
                    "WINDOW %s end notifications=%s reason=%s"
                    % (event.get("id"), event.get("notifications"),
                       event.get("reason"))
                )
            elif kind == "query_start":
                lines.append(
                    "QUERY %s %s %s window=%s uidvalidity=%s"
                    % (
                        event.get("id"),
                        event.get("command"),
                        event.get("requested_range"),
                        event.get("window"),
                        event.get("uidvalidity"),
                    )
                )
            elif kind == "query_end":
                lines.append(
                    "QUERY %s end ok=%s uids=%s covered=%s superseded=%s%s"
                    % (
                        event.get("id"),
                        event.get("ok"),
                        event.get("uids"),
                        event.get("covered_range"),
                        event.get("superseded"),
                        (" (%s)" % event.get("reason"))
                        if event.get("reason") else "",
                    )
                )
            elif kind == "snapshot":
                lines.append(
                    "SNAPSHOT generation=%s uidvalidity=%s uids=%s"
                    % (
                        event.get("generation"),
                        event.get("uidvalidity"),
                        event.get("uids"),
                    )
                )
            elif kind == "gap":
                lines.append(
                    "GAP %s %s seq=%s..%s"
                    % (
                        event.get("id"),
                        event.get("reason"),
                        event.get("start_seq"),
                        event.get("end_seq"),
                    )
                )
            elif kind == "archive":
                lines.append(
                    "ARCHIVE uid=%s uidvalidity=%s duplicate=%s"
                    % (
                        event.get("uid"),
                        event.get("uidvalidity"),
                        event.get("duplicate"),
                    )
                )
        return "\n".join(lines)


IDLECollector = IdleCollector
IdleMailboxCollector = IdleCollector
IdleSession = IdleCollector
IdleSync = IdleCollector


def collect_mailbox(imap, mailbox: str = "INBOX", **kwargs) -> IdleCollector:
    """Create and start an :class:`IdleCollector`."""

    return IdleCollector(imap, mailbox, **kwargs).start()


collect = collect_mailbox
