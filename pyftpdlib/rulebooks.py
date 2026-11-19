# Copyright (C) 2007 Giampaolo Rodola' <g.rodola@gmail.com>.
# Use of this source code is governed by MIT license that can be
# found in the LICENSE file.

"""Versioned authorization rulebooks.

This module implements hot-swappable, *versioned* directory
authorization rules intended for deployments where an external
service (e.g. a collection/ingestion service) adjusts FTP directory
grants *after* users have already logged in.

The main class is :class:`VersionedRuleAuthorizer`, a
:class:`pyftpdlib.authorizers.DummyAuthorizer` subclass which decides
filesystem permissions by consulting the currently active
:class:`Rulebook` snapshot:

- every command issued after a rule update is checked against the
  new rulebook version; commands issued before keep being checked
  against the old one (checks happen at command time);
- read, write and rename eligibilities are judged as separate
  capabilities (:data:`READ`, :data:`WRITE`, :data:`RENAME`);
- paths are canonicalized (symlinks resolved) before matching, so
  that a virtual path aliasing a file cannot bypass a permission
  which was revoked on the real file;
- rule updates are atomic: the new rulebook is fully validated
  *before* being swapped in, hence a failed update raises
  :class:`pyftpdlib.exceptions.RulebookError` and leaves the old
  version active;
- data transfers already in progress when an update occurs are
  handled according to an explicit transfer policy ("continue" or
  "terminate"); termination is selective: only the sessions whose
  transfer lost its authorization are stopped, everybody else's
  transfers are left alone;
- every authorization decision is recorded into an audit log
  reporting *which rulebook version* the decision was based on.
  Audit records never include passwords or any other credential.

Usage example:

>>> from pyftpdlib.rulebooks import DirectoryRule, Rulebook
>>> from pyftpdlib.rulebooks import VersionedRuleAuthorizer
>>> authorizer = VersionedRuleAuthorizer()
>>> authorizer.add_user("user", "12345", "/home/user", perm="elr")
>>> rule = DirectoryRule("/home/user/incoming", capabilities=["write"])
>>> authorizer.update_rules("v1", [rule])
'v1'
"""

import os
import threading
import time
import weakref
from typing import NamedTuple

from .authorizers import DummyAuthorizer
from .exceptions import RulebookError
from .log import logger

__all__ = [
    "CAPABILITIES",
    "PERM_TO_CAPABILITY",
    "READ",
    "RENAME",
    "WRITE",
    "AuditRecord",
    "DirectoryRule",
    "Rulebook",
    "TransferPolicy",
    "VersionedRuleAuthorizer",
]


# --- capabilities

#: capability to read (download, list, stat) a path.
READ = "read"
#: capability to write (create, append, delete, chmod) a path.
WRITE = "write"
#: capability to rename (move) a path.
RENAME = "rename"

CAPABILITIES = (READ, WRITE, RENAME)

#: map authorizer permission letters ("elradfmwMT") to capabilities.
PERM_TO_CAPABILITY = {
    "e": READ,  # change directory
    "l": READ,  # list files
    "r": READ,  # retrieve file
    "a": WRITE,  # append to file
    "d": WRITE,  # delete file/directory
    "m": WRITE,  # create directory
    "w": WRITE,  # store file
    "M": WRITE,  # change file mode
    "T": WRITE,  # update file modification time
    "f": RENAME,  # rename file/directory
}

#: map data transfer commands to the permission letter they require.
_CMD_TO_PERM = {
    "RETR": "r",
    "LIST": "l",
    "NLST": "l",
    "MLSD": "l",
    "STOR": "w",
    "STOU": "w",
    "APPE": "a",
}


class TransferPolicy:
    """Policies deciding what happens to data transfers which are
    already in progress when a new rulebook version is activated.
    """

    #: let in-progress transfers run to completion, even if the new
    #: rules revoked the permission they were started with.
    CONTINUE = "continue"
    #: selectively stop the in-progress transfers whose permission
    #: was revoked by the new rules; transfers which are still
    #: authorized (including everybody else's) keep running.
    TERMINATE = "terminate"

    ALL = (CONTINUE, TERMINATE)


class AuditRecord(NamedTuple):
    """A single authorization decision. It deliberately carries no
    password or credential material, only *which rulebook version*
    the decision was based on.
    """

    timestamp: float
    username: str
    capability: str
    path: str
    allowed: bool
    version: str
    basis: str  # "rule" | "fallback" | "transfer-policy"


def _canonicalize(path):
    """Return the canonical form of a path, resolving any symbolic
    link, so that different virtual paths mapping to the same file
    are subjected to the very same rules.
    """
    return os.path.normcase(os.path.realpath(path))


def _issubpath(a, b):
    """Return True if a is a sub-path of b or if the paths are equal."""
    p1 = a.rstrip(os.sep).split(os.sep)
    p2 = b.rstrip(os.sep).split(os.sep)
    return p1[: len(p2)] == p2


class DirectoryRule:
    """A rule granting a set of capabilities on a directory.

    - (str) path: an absolute filesystem directory path. It is
      canonicalized (symlinks resolved) at construction time.

    - (iterable) capabilities: a non empty iterable containing any
      of "read", "write", "rename".

    - (bool) recursive: when True (default) the rule applies to the
      whole directory subtree; when False it applies to the directory
      itself and its immediate files only.

    - (iterable) users: an optional iterable of usernames the rule
      applies to; None (default) means "all users".

    Raises RulebookError on invalid input.
    """

    def __init__(self, path, capabilities, recursive=True, users=None):
        if not isinstance(path, str) or not os.path.isabs(path):
            raise RulebookError(
                f"rule path must be an absolute path, got {path!r}"
            )
        if isinstance(capabilities, str):
            capabilities = (capabilities,)
        try:
            capabilities = frozenset(capabilities)
        except TypeError as err:
            raise RulebookError(
                f"invalid capabilities {capabilities!r}"
            ) from err
        if not capabilities:
            raise RulebookError("rule must grant at least one capability")
        invalid = capabilities - set(CAPABILITIES)
        if invalid:
            raise RulebookError(
                f"no such capabilities: {sorted(invalid)!r}; "
                f"expected a subset of {sorted(CAPABILITIES)!r}"
            )
        if users is not None:
            if isinstance(users, str):
                users = (users,)
            users = frozenset(users)
            if not users:
                raise RulebookError("users must not be empty")
        self.path = _canonicalize(path)
        self.capabilities = capabilities
        self.recursive = bool(recursive)
        self.users = users

    def __repr__(self):
        return (
            f"{self.__class__.__name__}(path={self.path!r}, "
            f"capabilities={sorted(self.capabilities)!r}, "
            f"recursive={self.recursive!r}, users={self.users!r})"
        )

    def covers(self, path):
        """Whether the rule applies to the given canonicalized path."""
        if self.recursive:
            return _issubpath(path, self.path)
        return path == self.path or (
            os.path.dirname(path) == self.path and not os.path.isdir(path)
        )

    def matches(self, username, path):
        """Whether the rule applies to the given user and
        canonicalized path.
        """
        if self.users is not None and username not in self.users:
            return False
        return self.covers(path)


class Rulebook:
    """An immutable, versioned snapshot of directory authorization
    rules.

    - (str or int) version: an identifier for this set of rules.
      It is reported in the audit log so that every authorization
      decision can be traced back to the rulebook version which
      produced it.

    - (iterable) rules: an iterable of DirectoryRule instances.

    The whole snapshot is validated at construction time; on invalid
    input RulebookError is raised and no partial state is left
    behind.
    """

    def __init__(self, version, rules=()):
        if (
            isinstance(version, bool)
            or not isinstance(version, (str, int))
            or (isinstance(version, str) and not version.strip())
        ):
            raise RulebookError(
                f"invalid rulebook version {version!r}; "
                "expected a non empty string or an int"
            )
        rules = tuple(rules)
        for rule in rules:
            if not isinstance(rule, DirectoryRule):
                raise RulebookError(
                    f"invalid rule {rule!r}; expected a DirectoryRule"
                )
        self.version = version
        self.rules = rules

    def __repr__(self):
        return (
            f"{self.__class__.__name__}(version={self.version!r}, "
            f"rules={len(self.rules)})"
        )

    def decide(self, username, capability, path):
        """Return True/False if a rule allows/denies the capability
        on path for the given user, None if no rule matches (in which
        case the caller is supposed to fall back to the base
        authorizer permissions).

        The path is canonicalized first (symlinks resolved), so that
        aliased virtual paths pointing to the same file cannot bypass
        a revoked permission.
        """
        if capability not in CAPABILITIES:
            raise RulebookError(f"no such capability {capability!r}")
        path = _canonicalize(path)
        best = None
        for rule in self.rules:
            if not rule.matches(username, path):
                continue
            # most specific rule wins: longest path first, then
            # user-specific rules over global ones.
            key = (len(rule.path), rule.users is not None)
            if best is None or key > best[0]:
                best = (key, rule)
        if best is None:
            return None
        return capability in best[1].capabilities


class VersionedRuleAuthorizer(DummyAuthorizer):
    """A DummyAuthorizer whose directory permissions are driven by a
    versioned Rulebook which can be hot-swapped at runtime (e.g. by
    a collection service) while users are logged in.

    - (Rulebook) rulebook: the initial rulebook; defaults to an
      empty rulebook at version "0" (all decisions fall back to the
      base DummyAuthorizer permissions).

    - (str) transfer_policy: what to do with data transfers already
      in progress when a new rulebook version is activated; either
      TransferPolicy.CONTINUE (default) or TransferPolicy.TERMINATE.

    - (list or callable) audit_log: the sink for AuditRecord
      entries; a list to append to (default: a new list) or a
      callable accepting a single AuditRecord argument. Records
      include the rulebook version each decision was based on and
      never include credentials.
    """

    def __init__(
        self,
        rulebook=None,
        transfer_policy=TransferPolicy.CONTINUE,
        audit_log=None,
    ):
        super().__init__()
        if rulebook is None:
            rulebook = Rulebook(version="0")
        elif not isinstance(rulebook, Rulebook):
            raise RulebookError(
                f"invalid rulebook {rulebook!r}; expected a Rulebook"
            )
        if transfer_policy not in TransferPolicy.ALL:
            raise ValueError(
                f"invalid transfer policy {transfer_policy!r}; "
                f"expected one of {TransferPolicy.ALL!r}"
            )
        self._rulebook = rulebook
        self.transfer_policy = transfer_policy
        self.audit_log = audit_log if audit_log is not None else []
        self._sessions = weakref.WeakSet()
        self._lock = threading.RLock()

    @property
    def current_version(self):
        """The version of the currently active rulebook."""
        return self._rulebook.version

    def get_rulebook(self):
        """Return the currently active rulebook."""
        return self._rulebook

    def update_rules(self, version, rules):
        """Atomically activate a new set of rules under a new
        version. Returns the new version.

        The new rulebook is fully validated before being swapped in:
        if validation fails RulebookError is raised and the previous
        version remains active.

        After a successful update, data transfers already in progress
        are dealt with according to the configured transfer policy.
        """
        new_rulebook = Rulebook(version, rules)
        with self._lock:
            old_rulebook = self._rulebook
            self._rulebook = new_rulebook
        logger.info(
            "rulebook updated: version %r -> %r (%d rules)",
            old_rulebook.version,
            new_rulebook.version,
            len(new_rulebook.rules),
        )
        self._enforce_transfer_policy(old_rulebook, new_rulebook)
        return new_rulebook.version

    # --- session registry (used by FTPHandler)

    def register_session(self, session):
        """Register an authenticated FTP session so that in-progress
        transfers can be subjected to the transfer policy on rule
        updates. Called by FTPHandler after a successful login.
        """
        self._sessions.add(session)

    def unregister_session(self, session):
        """Unregister a session previously registered with
        register_session(). Called by FTPHandler on logout and on
        disconnect.
        """
        self._sessions.discard(session)

    @property
    def sessions(self):
        """The list of currently registered (logged in) sessions."""
        return list(self._sessions)

    # --- permission checks

    def has_perm(self, username, perm, path=None):
        """Whether the user has permission over path, checked against
        the currently active rulebook version. Read, write and rename
        permissions are judged as separate capabilities. Every
        decision is recorded in the audit log together with the
        rulebook version it was based on.
        """
        if path is None or perm not in PERM_TO_CAPABILITY:
            return super().has_perm(username, perm, path)
        capability = PERM_TO_CAPABILITY[perm]
        with self._lock:
            rulebook = self._rulebook
        allowed, basis = self._evaluate(username, perm, path, rulebook)
        self._audit(
            username, capability, path, allowed, rulebook.version, basis
        )
        return allowed

    def _evaluate(self, username, perm, path, rulebook):
        """Return an (allowed, basis) pair for perm on path according
        to the given rulebook, falling back to the base authorizer
        permissions when no rule matches.
        """
        capability = PERM_TO_CAPABILITY[perm]
        decision = rulebook.decide(username, capability, path)
        if decision is not None:
            return decision, "rule"
        return super().has_perm(username, perm, path), "fallback"

    def _audit(self, username, capability, path, allowed, version, basis):
        """Record an authorization decision. Never records passwords
        or any other credential material.
        """
        record = AuditRecord(
            timestamp=time.time(),
            username=username,
            capability=capability,
            path=path,
            allowed=bool(allowed),
            version=version,
            basis=basis,
        )
        sink = self.audit_log
        if callable(sink):
            sink(record)
        else:
            sink.append(record)

    # --- transfer policy

    def _enforce_transfer_policy(self, old_rulebook, new_rulebook):
        """Apply the configured transfer policy to the registered
        sessions after a rulebook update.
        """
        if self.transfer_policy != TransferPolicy.TERMINATE:
            # CONTINUE: in-progress transfers are left alone.
            return
        for session in self.sessions:
            try:
                self._enforce_session_transfer(
                    session, old_rulebook, new_rulebook
                )
            except Exception:
                # Never let a single session break the update or take
                # down unrelated transfers.
                logger.exception(
                    "error while enforcing transfer policy on %r", session
                )

    def _enforce_session_transfer(self, session, old_rulebook, new_rulebook):
        """Selectively terminate the in-progress transfer of a single
        session if (and only if) the new rulebook revoked the
        permission it was started with. Transfers which are still
        authorized keep running.
        """
        channel = getattr(session, "data_channel", None)
        if channel is None or not channel.transfer_in_progress():
            return
        perm = _CMD_TO_PERM.get(getattr(channel, "cmd", None))
        path = getattr(getattr(channel, "file_obj", None), "name", None)
        username = getattr(session, "username", "")
        if perm is None or not path or not username:
            # cannot assess this transfer: leave it alone rather than
            # closing it blindly.
            return
        was_allowed, _ = self._evaluate(username, perm, path, old_rulebook)
        if not was_allowed:
            return
        is_allowed, _ = self._evaluate(username, perm, path, new_rulebook)
        if is_allowed:
            return
        logger.info(
            "terminating transfer of %r for user %r: permission revoked "
            "by rulebook version %r",
            path,
            username,
            new_rulebook.version,
        )
        # same semantics as ABOR: close the data connection first,
        # then report the abnormal termination on the control channel
        channel.close()
        respond = getattr(session, "respond", None)
        if respond is not None:
            respond(
                "426 Transfer aborted: permission revoked by rulebook "
                f"version {new_rulebook.version}.",
                logfun=logger.info,
            )
        self._audit(
            username,
            PERM_TO_CAPABILITY[perm],
            path,
            False,
            new_rulebook.version,
            "transfer-policy",
        )
