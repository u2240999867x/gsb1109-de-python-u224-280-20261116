"""Ownership and shutdown support for filesystem wrapper graphs.

A filesystem may be referenced by more than one wrapper (a mount in two
`~fs.mountfs.MountFS` instances, a backend shared by a `~fs.multifs.MultiFS`
and a sub-directory, and so on).  Closing one wrapper must not close a
backend that another wrapper still needs.

This module defines the small vocabulary used to describe those
relationships.  The implementation lives on `~fs.base.FS`, which keeps a
queryable ledger of *holds* on each filesystem instance.

"""

from __future__ import absolute_import, print_function, unicode_literals

import io
import time
import typing
import weakref

from enum import Enum

if typing.TYPE_CHECKING:
    from typing import Any, Dict, List, Optional, Text


class Ownership(str, Enum):
    """The responsibility a wrapper edge has for its child."""

    #: The holder owns the child and, once no other holder needs it, is
    #: responsible for closing it.
    OWNED = "owned"

    #: The holder merely borrows the child. It never closes the child.
    BORROWED = "borrowed"

    #: Alias retained for callers which use the noun form.
    OWNER = "owned"
    OWN = "owned"
    BORROW = "borrowed"


class ClosePolicy(str, Enum):
    """How a shutdown deals with admitted operations and open handles."""

    #: Stop new operations and wait for already admitted reads and handles.
    DRAIN = "drain"

    #: Close outstanding handles immediately. Reads which cannot be
    #: interrupted cause the shutdown to be deferred with an explicit result.
    ABORT = "abort"

    #: Refuse to wait or force; return a deferred result when anything is
    #: still active.
    DEFER = "defer"

    #: Aliases.
    FORCE = "abort"
    FAIL = "defer"
    WAIT = "drain"


class CloseStatus(str, Enum):
    """The outcome of a close or shutdown request."""

    CLOSED = "closed"
    DEFERRED = "deferred"
    FAILED = "failed"


class LifecycleState(str, Enum):
    """The observable state of a filesystem's lifecycle."""

    OPEN = "open"
    DRAINING = "draining"
    CLOSED = "closed"


class CloseCascade(str, Enum):
    """How much confidence we have in a wrapper's cascade behaviour."""

    #: Closing the object only closes itself. Children are borrowed.
    SELF = "self"

    #: Closing the object closes owned children, but not borrowed ones.
    OWNED_CHILDREN = "owned"

    #: The object does not participate in this protocol, or overrides
    #: ``close`` without declaring what it does.
    UNKNOWN = "unknown"

    @property
    def safe_to_share(self):
        # type: () -> bool
        return self is not CloseCascade.UNKNOWN


_TRUE_VALUES = {"own", "owned", "owner", "owns", "true", "1", "yes", "exclusive"}
_FALSE_VALUES = {
    "borrow",
    "borrowed",
    "borrower",
    "share",
    "shared",
    "reference",
    "false",
    "0",
    "no",
}


def parse_ownership(value, default=Ownership.BORROWED):
    # type: (Any, Ownership) -> Ownership
    """Coerce an ownership declaration to an `Ownership` value."""
    if value is None:
        return default
    if isinstance(value, Ownership):
        return value
    if isinstance(value, bool):
        return Ownership.OWNED if value else Ownership.BORROWED
    if isinstance(value, bytes):
        value = value.decode("ascii", "replace")
    text = str(value).strip().lower()
    if text in _TRUE_VALUES:
        return Ownership.OWNED
    if text in _FALSE_VALUES:
        return Ownership.BORROWED
    raise ValueError(
        "ownership must be 'owned' or 'borrowed', not {!r}".format(value)
    )


def parse_policy(value, default=ClosePolicy.ABORT):
    # type: (Any, ClosePolicy) -> ClosePolicy
    """Coerce a close-policy declaration to a `ClosePolicy` value."""
    if value is None:
        return default
    if isinstance(value, ClosePolicy):
        return value
    if isinstance(value, bool):
        return ClosePolicy.DRAIN if value else ClosePolicy.ABORT
    if isinstance(value, bytes):
        value = value.decode("ascii", "replace")
    text = str(value).strip().lower()
    try:
        return ClosePolicy(text)
    except ValueError:
        aliases = {
            "immediate": ClosePolicy.ABORT,
            "close": ClosePolicy.ABORT,
            "legacy": ClosePolicy.ABORT,
            "reject": ClosePolicy.DEFER,
            "busy": ClosePolicy.DEFER,
            "none": ClosePolicy.DEFER,
        }
        try:
            return aliases[text]
        except KeyError:
            raise ValueError(
                "close policy must be 'drain', 'abort' or 'defer', not {!r}".format(
                    value
                )
            )


def parse_cascade(value, default=CloseCascade.UNKNOWN):
    # type: (Any, CloseCascade) -> CloseCascade
    if value is None:
        return default
    if isinstance(value, CloseCascade):
        return value
    if isinstance(value, bytes):
        value = value.decode("ascii", "replace")
    text = str(value).strip().lower()
    aliases = {
        "self": CloseCascade.SELF,
        "none": CloseCascade.SELF,
        "borrowed": CloseCascade.SELF,
        "owned": CloseCascade.OWNED_CHILDREN,
        "children": CloseCascade.OWNED_CHILDREN,
        "cascade": CloseCascade.OWNED_CHILDREN,
        "unknown": CloseCascade.UNKNOWN,
        "opaque": CloseCascade.UNKNOWN,
    }
    try:
        return aliases[text]
    except KeyError:
        raise ValueError("unknown close cascade {!r}".format(value))


def cascade_of(resource):
    # type: (Any) -> CloseCascade
    """Get the declared cascade behaviour for a filesystem instance.

    A wrapper which declares `~fs.base.FS.close_cascade` participates in
    the protocol.  A subclass which overrides ``close`` without declaring
    a cascade is opaque: we cannot know whether closing it would also
    close a backend shared with another wrapper, so safe sharing must be
    refused rather than guessed.
    """
    from .base import FS

    for cls in type(resource).__mro__:
        declared = cls.__dict__.get("close_cascade")
        if declared is not None:
            cascade = parse_cascade(declared)
            if cascade is not CloseCascade.UNKNOWN:
                return cascade
        if "close" in cls.__dict__:
            # Reaching the cooperative base implementation means the
            # object closes only itself and the holds it declared.
            if cls is FS:
                return CloseCascade.SELF
            return CloseCascade.UNKNOWN
    return CloseCascade.SELF


class Hold(object):
    """A record of one wrapper edge holding a child filesystem."""

    def __init__(
        self,
        resource,  # type: Any
        holder,  # type: Any
        ownership,  # type: Ownership
        label=None,  # type: Optional[Text]
        token=None,  # type: Any
    ):
        # type: (...) -> None
        self.resource = resource
        self._holder_ref = weakref.ref(holder)
        self.ownership = ownership
        self.label = label
        self.token = token if token is not None else object()
        self.created = time.time()
        self.active = True
        self.released = False
        self.released_at = None  # type: Optional[float]
        self.release_error = None  # type: Optional[BaseException]
        self.close_report = None  # type: Any

    @property
    def holder(self):
        # type: () -> Any
        return self._holder_ref()

    @property
    def holder_alive(self):
        # type: () -> bool
        return self._holder_ref() is not None

    @property
    def is_owned(self):
        # type: () -> bool
        return self.ownership is Ownership.OWNED

    @property
    def is_borrowed(self):
        # type: () -> bool
        return self.ownership is Ownership.BORROWED

    @property
    def holder_name(self):
        # type: () -> Text
        holder = self.holder
        if holder is None:
            return "<collected>"
        name = getattr(holder, "name", None)
        return name if isinstance(name, str) and name else repr(holder)

    def release(self, error=None):
        # type: (Optional[BaseException]) -> None
        """Mark this hold released. Calling this twice is harmless."""
        if self.released:
            return
        self.released = True
        self.active = False
        self.released_at = time.time()
        self.release_error = error

    def describe(self):
        # type: () -> Text
        role = "owns" if self.is_owned else "borrows"
        state = "active" if self.active else "released"
        label = "" if not self.label else " at {!r}".format(self.label)
        return "{} {} {}{} ({})".format(
            self.holder_name, role, repr(self.resource), label, state
        )

    def to_dict(self):
        # type: () -> Dict[Text, Any]
        holder = self.holder
        return {
            "holder": repr(holder),
            "holder_type": type(holder).__name__,
            "resource": repr(self.resource),
            "ownership": self.ownership.value,
            "label": self.label,
            "active": self.active,
            "released": self.released,
            "release_error": (
                None if self.release_error is None else str(self.release_error)
            ),
            "created": self.created,
            "released_at": self.released_at,
        }

    def __repr__(self):
        # type: () -> Text
        return "<Hold {}>".format(self.describe())


class CloseReport(object):
    """A queryable description of a close/shutdown attempt."""

    def __init__(self, fs, policy, status=CloseStatus.DEFERRED):
        # type: (Any, ClosePolicy, CloseStatus) -> None
        self.fs = fs
        self.policy = policy
        self.status = status
        self.created = time.time()
        self.finished = None  # type: Optional[float]
        self.reason = ""
        self.open_handles = 0
        self.active_operations = 0
        self.released_holds = []  # type: List[Hold]
        self.retained_holds = []  # type: List[Hold]
        self.errors = []  # type: List[Dict[Text, Any]]
        self.details = {}  # type: Dict[Text, Any]
        self.already_closed = False

    @property
    def state(self):
        # type: () -> LifecycleState
        return getattr(self.fs, "lifecycle_state", LifecycleState.CLOSED)

    @property
    def complete(self):
        # type: () -> bool
        return self.status is CloseStatus.CLOSED

    @property
    def closed(self):
        # type: () -> bool
        return self.complete

    @property
    def deferred(self):
        # type: () -> bool
        return self.status is CloseStatus.DEFERRED

    @property
    def failed(self):
        # type: () -> bool
        return self.status is CloseStatus.FAILED

    @property
    def ok(self):
        # type: () -> bool
        return not self.errors

    @property
    def pending(self):
        # type: () -> bool
        return self.status is CloseStatus.DEFERRED

    def add_error(self, error, action=None, holder=None):
        # type: (BaseException, Optional[Text], Any) -> None
        item = {
            "action": action or "close",
            "error": str(error),
            "error_type": type(error).__name__,
        }
        if holder is not None:
            item["holder"] = repr(holder)
        self.errors.append(item)

    def as_dict(self):
        # type: () -> Dict[Text, Any]
        return {
            "fs": repr(self.fs),
            "state": self.state.value,
            "status": self.status.value,
            "policy": self.policy.value,
            "complete": self.complete,
            "already_closed": self.already_closed,
            "reason": self.reason,
            "open_handles": self.open_handles,
            "active_operations": self.active_operations,
            "released_holds": [hold.to_dict() for hold in self.released_holds],
            "retained_holds": [hold.to_dict() for hold in self.retained_holds],
            "errors": list(self.errors),
            "details": dict(self.details),
        }

    to_dict = as_dict

    def __bool__(self):
        # type: () -> bool
        return self.complete

    __nonzero__ = __bool__

    def __repr__(self):
        # type: () -> Text
        return "<CloseReport {} {} policy={}>".format(
            repr(self.fs), self.status.value, self.policy.value
        )


class ManagedFile(io.IOBase):
    """Proxy an open file and release its lifecycle handle on close."""

    def __init__(self, file_object, fs, token, path):
        # type: (Any, Any, Any, Text) -> None
        super(ManagedFile, self).__init__()
        self._file = file_object
        self._fs = fs
        self._token = token
        self._path = path
        self._force_closed = False
        self._released = False
        self._shutdown_error = None

    # -- io.IOBase properties -------------------------------------------

    @property
    def closed(self):
        # type: () -> bool
        return self._force_closed or self._file.closed

    @property
    def mode(self):
        return getattr(self._file, "mode", None)

    @property
    def name(self):
        return getattr(self._file, "name", self._path)

    @property
    def encoding(self):
        return getattr(self._file, "encoding", None)

    @property
    def errors(self):
        return getattr(self._file, "errors", None)

    @property
    def newlines(self):
        return getattr(self._file, "newlines", None)

    def __getattr__(self, name):
        # Delegate implementation-specific attributes without masking
        # attributes of the proxy itself.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._file, name)

    def __repr__(self):
        # type: () -> Text
        return "<ManagedFile {!r} {!r}>".format(self._path, self._file)

    def _check_open(self):
        # type: () -> None
        if self._force_closed:
            from .errors import FilesystemClosed

            raise FilesystemClosed(
                "file handle for {!r} was closed during shutdown".format(self._path)
            )

    def _release(self, error=None):
        # type: (Optional[BaseException]) -> None
        if self._released:
            return
        self._released = True
        release = getattr(self._fs, "_release_handle", None)
        if release is not None:
            release(self._token, error=error)

    def close(self):
        # type: () -> None
        if self._force_closed:
            return
        error = None
        try:
            if not self._file.closed:
                self._file.close()
        except Exception as exc:  # pragma: no cover - backend specific
            error = exc
            raise
        finally:
            self._force_closed = True
            self._release(error)

    def force_close(self):
        # type: () -> Optional[BaseException]
        """Close the underlying handle, returning any error.

        This is used by the ``abort`` shutdown policy. It deliberately
        does not raise so that one bad handle cannot prevent a filesystem
        from being shut down.
        """
        error = None
        try:
            if not self._file.closed:
                try:
                    self._file.flush()
                except Exception as exc:  # pragma: no cover - backend specific
                    error = exc
                self._file.close()
        except Exception as exc:
            error = exc
        finally:
            self._force_closed = True
            self._release(error)
        return error

    # -- file operations ------------------------------------------------

    def flush(self):
        self._check_open()
        return self._file.flush()

    def fileno(self):
        self._check_open()
        return self._file.fileno()

    def isatty(self):
        self._check_open()
        return self._file.isatty()

    def readable(self):
        self._check_open()
        return self._file.readable()

    def writable(self):
        self._check_open()
        return self._file.writable()

    def seekable(self):
        self._check_open()
        return self._file.seekable()

    def read(self, size=-1):
        self._check_open()
        return self._file.read(size)

    def read1(self, size=-1):
        self._check_open()
        read1 = getattr(self._file, "read1", self._file.read)
        return read1(size)

    def readall(self):
        self._check_open()
        readall = getattr(self._file, "readall", self._file.read)
        return readall()

    def readinto(self, buffer):
        self._check_open()
        return self._file.readinto(buffer)

    def readinto1(self, buffer):
        self._check_open()
        try:
            readinto1 = self._file.readinto1
        except AttributeError:
            return self._file.readinto(buffer)
        return readinto1(buffer)

    def readline(self, size=-1):
        self._check_open()
        return self._file.readline(size)

    def readlines(self, hint=-1):
        self._check_open()
        return self._file.readlines(hint)

    def write(self, data):
        self._check_open()
        return self._file.write(data)

    def writelines(self, lines):
        self._check_open()
        return self._file.writelines(lines)

    def seek(self, offset, whence=0):
        self._check_open()
        return self._file.seek(offset, whence)

    def tell(self):
        self._check_open()
        return self._file.tell()

    def truncate(self, size=None):
        self._check_open()
        return self._file.truncate(size)

    def __iter__(self):
        self._check_open()
        return iter(self._file)

    def __next__(self):
        self._check_open()
        return next(self._file)

    next = __next__
