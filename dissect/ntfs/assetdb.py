"""Security descriptor asset database and offline access checker.

This module ingests NTFS security descriptors (from ``$Secure``) and the files that reference
them into a set of relational tables, and answers access-check queries for a fully explicit
test token against a specific file.

Design principles:

- Files reference shared descriptors by ``security_id``; each unique descriptor is stored once.
- ACE order (``position``) and inheritance source (``INHERITED_ACE`` flag, ``SE_DACL_PROTECTED``
  control flag) are preserved exactly as stored on disk.
- The three DACL states are kept strictly apart: an *empty* DACL (present, zero ACEs, denies
  everyone), a *NULL* DACL (``SE_DACL_PRESENT`` not set, grants everyone) and an *unreadable*
  DACL (descriptor could not be read or parsed, so there is insufficient evidence).
- Access checks are computed solely from the explicitly provided token (user SID, group SIDs,
  privileges) and the given :class:`AccessCheckPolicy`. When parent directory information is
  incomplete or inheritance is blocked, nothing is backfilled from ambient (current user)
  context; the stored descriptor is evaluated exactly as found.
- A corrupt descriptor only taints its own row; unrelated files and descriptors are unaffected.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from dissect.ntfs.c_ntfs import (
    ACE_TYPE,
    ATTRIBUTE_TYPE_CODE,
    FILE_RECORD_SEGMENT_IN_USE,
    c_ntfs,
)
from dissect.ntfs.secure import SecurityDescriptor

ACE_FLAGS = c_ntfs.ACE_FLAGS
SECURITY_DESCRIPTOR_CONTROL = c_ntfs.SECURITY_DESCRIPTOR_CONTROL

if TYPE_CHECKING:
    from collections.abc import Mapping

    from dissect.ntfs.mft import MftRecord
    from dissect.ntfs.ntfs import NTFS
    from dissect.ntfs.secure import Secure

__all__ = [
    "AccessCheckPolicy",
    "AccessDecision",
    "AccessResult",
    "AceRow",
    "AssetDatabase",
    "DaclState",
    "DescriptorRow",
    "FileRow",
    "IngestStats",
    "Token",
]

#: ACE types that grant access.
_ALLOW_TYPES = frozenset(
    {
        ACE_TYPE.ACCESS_ALLOWED,
        ACE_TYPE.ACCESS_ALLOWED_OBJECT,
        ACE_TYPE.ACCESS_ALLOWED_COMPOUND,
        ACE_TYPE.ACCESS_ALLOWED_CALLBACK,
        ACE_TYPE.ACCESS_ALLOWED_CALLBACK_OBJECT,
    }
)

#: ACE types that deny access.
_DENY_TYPES = frozenset(
    {
        ACE_TYPE.ACCESS_DENIED,
        ACE_TYPE.ACCESS_DENIED_OBJECT,
        ACE_TYPE.ACCESS_DENIED_CALLBACK,
        ACE_TYPE.ACCESS_DENIED_CALLBACK_OBJECT,
    }
)

#: Conditional (callback) ACE types; their application data cannot be evaluated statically.
_CALLBACK_TYPES = frozenset(
    {
        ACE_TYPE.ACCESS_ALLOWED_CALLBACK,
        ACE_TYPE.ACCESS_ALLOWED_CALLBACK_OBJECT,
        ACE_TYPE.ACCESS_DENIED_CALLBACK,
        ACE_TYPE.ACCESS_DENIED_CALLBACK_OBJECT,
    }
)

#: Access mask bits used by the default policy.
_READ_CONTROL = 0x00020000
_WRITE_DAC = 0x00040000
_WRITE_OWNER = 0x00080000
_ACCESS_SYSTEM_SECURITY = 0x01000000
_FILE_READ_DATA = 0x00000001
_FILE_WRITE_DATA = 0x00000002
_FILE_APPEND_DATA = 0x00000004
_FILE_READ_EA = 0x00000008
_FILE_WRITE_EA = 0x00000010
_FILE_READ_ATTRIBUTES = 0x00000080
_FILE_WRITE_ATTRIBUTES = 0x00000100
_DELETE = 0x00010000

#: Default mapping of privilege name to the access mask it implicitly grants.
DEFAULT_PRIVILEGE_RIGHTS: dict[str, int] = {
    "SeBackupPrivilege": _FILE_READ_DATA | _FILE_READ_EA | _FILE_READ_ATTRIBUTES | _READ_CONTROL,
    "SeRestorePrivilege": (
        _FILE_WRITE_DATA
        | _FILE_APPEND_DATA
        | _FILE_WRITE_EA
        | _FILE_WRITE_ATTRIBUTES
        | _WRITE_DAC
        | _WRITE_OWNER
        | _DELETE
    ),
    "SeTakeOwnershipPrivilege": _WRITE_OWNER,
    "SeSecurityPrivilege": _ACCESS_SYSTEM_SECURITY,
}


class DaclState(Enum):
    """The state of a descriptor's DACL."""

    #: DACL present with at least one ACE.
    NORMAL = "normal"
    #: DACL present but containing zero ACEs: implicitly denies everyone.
    EMPTY = "empty"
    #: No DACL (``SE_DACL_PRESENT`` not set): grants everyone full control.
    NULL = "null"
    #: The descriptor or DACL could not be read or parsed.
    UNREADABLE = "unreadable"


class AccessDecision(Enum):
    """The outcome of an access check."""

    ALLOW = "allow"
    DENY = "deny"
    #: Not enough trustworthy evidence to decide (e.g. unreadable DACL, unevaluable callback ACE).
    INSUFFICIENT = "insufficient"


@dataclass(frozen=True)
class Token:
    """A fully explicit test token.

    Nothing is resolved from the environment: the user SID, group SIDs and privileges
    are exactly what the access check will use.

    Args:
        user_sid: The user SID string, e.g. ``S-1-5-18``.
        group_sids: Group SID strings the token is a member of.
        privileges: Enabled privilege names, e.g. ``SeBackupPrivilege``.
    """

    user_sid: str
    group_sids: frozenset[str] = frozenset()
    privileges: frozenset[str] = frozenset()

    def __init__(
        self,
        user_sid: str,
        group_sids: tuple[str, ...] | frozenset[str] = (),
        privileges: tuple[str, ...] | frozenset[str] = (),
    ):
        object.__setattr__(self, "user_sid", user_sid)
        object.__setattr__(self, "group_sids", frozenset(group_sids))
        object.__setattr__(self, "privileges", frozenset(privileges))

    @property
    def sids(self) -> frozenset[str]:
        """Return all SIDs of the token (user and groups)."""
        return self.group_sids | {self.user_sid}


@dataclass(frozen=True)
class AccessCheckPolicy:
    """Explicit policy for access checks.

    Args:
        owner_rights: Mask implicitly granted to the owner of a descriptor.
        privilege_rights: Mapping of privilege name to implicitly granted access mask.
        evaluate_callback_aces: Whether callback (conditional) ACEs are treated as ordinary
            ACEs. If ``False`` (default), a decisive callback ACE yields
            :attr:`AccessDecision.INSUFFICIENT` because its condition cannot be evaluated offline.
    """

    owner_rights: int = _READ_CONTROL | _WRITE_DAC
    privilege_rights: Mapping[str, int] = field(default_factory=lambda: dict(DEFAULT_PRIVILEGE_RIGHTS))
    evaluate_callback_aces: bool = False

    def privilege_mask(self, privileges: frozenset[str]) -> int:
        """Return the combined access mask granted by the given privileges."""
        mask = 0
        for privilege in privileges:
            mask |= self.privilege_rights.get(privilege, 0)
        return mask


@dataclass(frozen=True)
class AceRow:
    """A single ACE as stored in the database."""

    security_id: int
    acl: str
    position: int
    type: str
    flags: int
    mask: int | None
    sid: str | None
    inherited: bool
    object_type: str | None
    inherited_object_type: str | None

    def __repr__(self) -> str:
        return (
            f"<AceRow {self.acl}[{self.position}] {self.type} mask={self.mask!r} sid={self.sid} "
            f"inherited={self.inherited}>"
        )


@dataclass(frozen=True)
class DescriptorRow:
    """A shared security descriptor as stored in the database."""

    security_id: int
    owner: str | None
    group: str | None
    control: int
    dacl_state: DaclState
    dacl_protected: bool
    sacl_present: bool
    error: str | None


@dataclass(frozen=True)
class FileRow:
    """A file referencing a shared descriptor by ``security_id``."""

    segment: int
    path: str | None
    parent_segment: int | None
    parent_known: bool
    security_id: int | None
    error: str | None


@dataclass(frozen=True)
class AccessResult:
    """The result of an access check."""

    decision: AccessDecision
    security_id: int | None
    granted_mask: int
    remaining_mask: int
    decisive_ace: AceRow | None
    reason: str


@dataclass(frozen=True)
class IngestStats:
    """Counters for an ingest run."""

    files: int = 0
    file_errors: int = 0
    descriptors: int = 0
    descriptor_errors: int = 0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS descriptor (
    security_id     INTEGER PRIMARY KEY,
    owner           TEXT,
    group_sid       TEXT,
    control         INTEGER NOT NULL DEFAULT 0,
    dacl_state      TEXT NOT NULL,
    dacl_protected  INTEGER NOT NULL DEFAULT 0,
    sacl_present    INTEGER NOT NULL DEFAULT 0,
    error           TEXT
);
CREATE TABLE IF NOT EXISTS ace (
    security_id             INTEGER NOT NULL REFERENCES descriptor (security_id),
    acl                     TEXT NOT NULL,
    position                INTEGER NOT NULL,
    type                    TEXT NOT NULL,
    flags                   INTEGER NOT NULL,
    mask                    INTEGER,
    sid                     TEXT,
    inherited               INTEGER NOT NULL DEFAULT 0,
    object_type             TEXT,
    inherited_object_type   TEXT,
    PRIMARY KEY (security_id, acl, position)
);
CREATE TABLE IF NOT EXISTS file (
    segment         INTEGER PRIMARY KEY,
    path            TEXT,
    parent_segment  INTEGER,
    parent_known    INTEGER NOT NULL DEFAULT 0,
    security_id     INTEGER REFERENCES descriptor (security_id),
    error           TEXT
);
"""


class AssetDatabase:
    """Asset database of NTFS security descriptors and the files referencing them.

    Args:
        path: Path of the SQLite database, or ``:memory:`` (default) for an in-memory database.
    """

    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path)
        self.db.executescript(_SCHEMA)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> AssetDatabase:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # ------------------------------------------------------------------ ingest

    def ingest(self, ntfs: NTFS) -> IngestStats:
        """Ingest all descriptors from ``$Secure`` and all in-use MFT records of the volume.

        Errors are isolated per descriptor and per file: one corrupt descriptor or record
        never affects unrelated rows.
        """
        stats = IngestStats()
        if ntfs.secure is not None:
            stats = self.ingest_secure(ntfs.secure)

        files = file_errors = 0
        for record in ntfs.mft.segments():
            try:
                if not record.header.Flags & FILE_RECORD_SEGMENT_IN_USE:
                    continue
                attrs = record.attributes
                std_info = attrs[ATTRIBUTE_TYPE_CODE.STANDARD_INFORMATION]
                if not std_info:
                    continue
                security_id = std_info[0].security_id

                path, parent_segment, parent_known = self._resolve_path(record)
                self.add_file(record.segment, path, parent_segment, parent_known, security_id)
                files += 1
            except Exception as e:  # noqa: BLE001, PERF203 - isolate per-record errors
                self.add_file(record.segment, None, None, False, None, error=f"{type(e).__name__}: {e}")
                file_errors += 1

        return IngestStats(files, file_errors, stats.descriptors, stats.descriptor_errors)

    def ingest_secure(self, secure: Secure) -> IngestStats:
        """Ingest all security descriptors from a :class:`~dissect.ntfs.secure.Secure` instance."""
        descriptors = descriptor_errors = 0
        for entry in secure._iter_entries():
            try:
                sd = SecurityDescriptor(secure.sds)
                self.add_descriptor(entry.SecurityId, sd)
                descriptors += 1
            except Exception as e:  # noqa: BLE001, PERF203 - isolate per-descriptor errors
                self.add_descriptor(entry.SecurityId, None, error=f"{type(e).__name__}: {e}")
                descriptor_errors += 1
        return IngestStats(descriptors=descriptors, descriptor_errors=descriptor_errors)

    @staticmethod
    def _resolve_path(record: MftRecord) -> tuple[str | None, int | None, bool]:
        """Resolve a record's path and parent.

        Returns ``(None, parent_segment, False)`` when the parent chain is incomplete;
        nothing is guessed or backfilled.
        """
        parent_segment = None
        try:
            parent_segment = record.ntfs.mft._get_parent(record).segment
        except Exception:  # noqa: BLE001 - parent information may be incomplete
            pass
        try:
            path = record.full_path()
        except Exception:  # noqa: BLE001 - parent information may be incomplete
            return None, parent_segment, False
        return path, parent_segment, path is not None or parent_segment is not None

    def add_descriptor(self, security_id: int, sd: SecurityDescriptor | None, error: str | None = None) -> None:
        """Store one security descriptor, classifying the DACL state.

        Args:
            security_id: The security ID of the descriptor.
            sd: The parsed descriptor, or ``None`` if it could not be read.
            error: Optional error message describing why the descriptor is unreadable.
        """
        if sd is None:
            state, control, protected = DaclState.UNREADABLE, 0, False
            owner = group = None
            sacl_present = False
        else:
            control = int(sd.header.Control)
            protected = bool(control & SECURITY_DESCRIPTOR_CONTROL.SE_DACL_PROTECTED)
            sacl_present = bool(control & SECURITY_DESCRIPTOR_CONTROL.SE_SACL_PRESENT)
            owner, group = sd.owner, sd.group
            if not control & SECURITY_DESCRIPTOR_CONTROL.SE_DACL_PRESENT:
                state = DaclState.NULL
            elif sd.dacl is None or not sd.dacl.ace:
                state = DaclState.EMPTY
            else:
                state = DaclState.NORMAL

        self.db.execute(
            "INSERT OR REPLACE INTO descriptor"
            " (security_id, owner, group_sid, control, dacl_state, dacl_protected, sacl_present, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (security_id, owner, group, control, state.value, protected, sacl_present, error),
        )
        self.db.execute("DELETE FROM ace WHERE security_id = ?", (security_id,))

        if sd is not None:
            for acl_name, acl in (("dacl", sd.dacl), ("sacl", sd.sacl)):
                if acl is None:
                    continue
                for position, ace in enumerate(acl.ace):
                    self.db.execute(
                        "INSERT INTO ace"
                        " (security_id, acl, position, type, flags, mask, sid, inherited, object_type,"
                        "  inherited_object_type)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            security_id,
                            acl_name,
                            position,
                            ace.header.AceType.name,
                            int(ace.header.AceFlags),
                            ace.mask,
                            ace.sid,
                            bool(ace.header.AceFlags & ACE_FLAGS.INHERITED_ACE),
                            str(ace.object_type) if ace.object_type else None,
                            str(ace.inherited_object_type) if ace.inherited_object_type else None,
                        ),
                    )
        self.db.commit()

    def add_file(
        self,
        segment: int,
        path: str | None,
        parent_segment: int | None,
        parent_known: bool,
        security_id: int | None,
        error: str | None = None,
    ) -> None:
        """Store one file row referencing a shared descriptor by ``security_id``."""
        self.db.execute(
            "INSERT OR REPLACE INTO file (segment, path, parent_segment, parent_known, security_id, error)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (segment, path, parent_segment, parent_known, security_id, error),
        )
        self.db.commit()

    # ------------------------------------------------------------------ query

    def descriptor(self, security_id: int) -> DescriptorRow | None:
        """Return the descriptor row for a security ID, or ``None``."""
        row = self.db.execute(
            "SELECT security_id, owner, group_sid, control, dacl_state, dacl_protected, sacl_present, error"
            " FROM descriptor WHERE security_id = ?",
            (security_id,),
        ).fetchone()
        if row is None:
            return None
        return DescriptorRow(row[0], row[1], row[2], row[3], DaclState(row[4]), bool(row[5]), bool(row[6]), row[7])

    def aces(self, security_id: int, acl: str = "dacl") -> list[AceRow]:
        """Return the ACEs of a descriptor in their original stored order."""
        rows = self.db.execute(
            "SELECT security_id, acl, position, type, flags, mask, sid, inherited, object_type, inherited_object_type"
            " FROM ace WHERE security_id = ? AND acl = ? ORDER BY position",
            (security_id, acl),
        ).fetchall()
        return [
            AceRow(r[0], r[1], r[2], r[3], r[4], r[5], r[6], bool(r[7]), r[8], r[9]) for r in rows
        ]

    def file(self, segment: int) -> FileRow | None:
        """Return the file row for an MFT segment, or ``None``."""
        row = self.db.execute(
            "SELECT segment, path, parent_segment, parent_known, security_id, error FROM file WHERE segment = ?",
            (segment,),
        ).fetchone()
        if row is None:
            return None
        return FileRow(row[0], row[1], row[2], bool(row[3]), row[4], row[5])

    def files(self, security_id: int | None = None) -> list[FileRow]:
        """Return all file rows, optionally filtered to those sharing a descriptor."""
        if security_id is None:
            rows = self.db.execute(
                "SELECT segment, path, parent_segment, parent_known, security_id, error FROM file ORDER BY segment"
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT segment, path, parent_segment, parent_known, security_id, error FROM file"
                " WHERE security_id = ? ORDER BY segment",
                (security_id,),
            ).fetchall()
        return [FileRow(r[0], r[1], r[2], bool(r[3]), r[4], r[5]) for r in rows]

    def explain_dacl_state(self, security_id: int) -> str:
        """Return a human-readable explanation of a descriptor's DACL state."""
        desc = self.descriptor(security_id)
        if desc is None:
            return f"security ID {security_id} is not present in the database"
        aces = self.aces(security_id)
        inherited = sum(1 for ace in aces if ace.inherited)
        if desc.dacl_state is DaclState.NULL:
            return (
                f"security ID {security_id} has no DACL (SE_DACL_PRESENT not set): "
                "a NULL DACL grants everyone full control"
            )
        if desc.dacl_state is DaclState.EMPTY:
            return (
                f"security ID {security_id} has an empty DACL (present, 0 ACEs): "
                "it grants no one any access, every check is implicitly denied"
            )
        if desc.dacl_state is DaclState.UNREADABLE:
            return (
                f"security ID {security_id} has an unreadable DACL ({desc.error}): "
                "there is insufficient evidence to decide any access check"
            )
        return (
            f"security ID {security_id} has a DACL with {len(aces)} ACE(s), {inherited} inherited"
            + ("; inheritance is blocked (SE_DACL_PROTECTED)" if desc.dacl_protected else "")
        )

    # ------------------------------------------------------------------ access check

    def check_access(
        self,
        target: int | str,
        token: Token,
        desired_mask: int,
        policy: AccessCheckPolicy | None = None,
    ) -> AccessResult:
        """Check whether ``token`` is granted ``desired_mask`` on ``target``.

        Args:
            target: An MFT segment number (``int``) or a stored path (``str``).
            token: The explicit test token.
            desired_mask: The requested access mask.
            policy: The access check policy; a default policy is used if omitted.
        """
        policy = policy or AccessCheckPolicy()

        if isinstance(target, str):
            row = self.db.execute("SELECT segment FROM file WHERE path = ?", (target,)).fetchone()
            if row is None:
                return AccessResult(
                    AccessDecision.INSUFFICIENT, None, 0, desired_mask, None, f"no file stored for path {target!r}"
                )
            target = row[0]

        file_row = self.file(target)
        if file_row is None:
            return AccessResult(
                AccessDecision.INSUFFICIENT, None, 0, desired_mask, None, f"no file stored for segment {target}"
            )
        if file_row.security_id is None:
            return AccessResult(
                AccessDecision.INSUFFICIENT,
                None,
                0,
                desired_mask,
                None,
                f"file has no readable security ID ({file_row.error})",
            )

        desc = self.descriptor(file_row.security_id)
        if desc is None:
            return AccessResult(
                AccessDecision.INSUFFICIENT,
                file_row.security_id,
                0,
                desired_mask,
                None,
                f"security ID {file_row.security_id} is referenced but not present in the database",
            )

        notes = []
        if desc.dacl_protected:
            notes.append("DACL is protected (SE_DACL_PROTECTED): inheritance blocked, evaluated exactly as stored")
        if not file_row.parent_known:
            notes.append("parent directory unknown: no inherited information backfilled")

        result = self._evaluate(desc, token, desired_mask, policy)
        if notes:
            result = AccessResult(
                result.decision,
                result.security_id,
                result.granted_mask,
                result.remaining_mask,
                result.decisive_ace,
                f"{result.reason} ({'; '.join(notes)})",
            )
        return result

    def _evaluate(
        self, desc: DescriptorRow, token: Token, desired_mask: int, policy: AccessCheckPolicy
    ) -> AccessResult:
        granted = policy.privilege_mask(token.privileges)
        grants = []
        if granted & desired_mask:
            grants.append("token privileges")
        if desc.owner is not None and token.user_sid == desc.owner:
            granted |= policy.owner_rights
            if policy.owner_rights & desired_mask:
                grants.append("owner rights")

        remaining = desired_mask & ~granted
        if remaining == 0:
            return AccessResult(
                AccessDecision.ALLOW,
                desc.security_id,
                granted & desired_mask,
                0,
                None,
                f"allowed by {' and '.join(grants)}",
            )

        if desc.dacl_state is DaclState.UNREADABLE:
            return AccessResult(
                AccessDecision.INSUFFICIENT,
                desc.security_id,
                granted & desired_mask,
                remaining,
                None,
                f"DACL could not be read ({desc.error}): insufficient evidence",
            )
        if desc.dacl_state is DaclState.NULL:
            return AccessResult(
                AccessDecision.ALLOW,
                desc.security_id,
                desired_mask,
                0,
                None,
                "allowed: NULL DACL (no DACL) grants everyone full control",
            )
        if desc.dacl_state is DaclState.EMPTY:
            return AccessResult(
                AccessDecision.DENY,
                desc.security_id,
                granted & desired_mask,
                remaining,
                None,
                "denied: empty DACL grants no one any access",
            )

        sids = token.sids
        last_allow = None
        for ace in self.aces(desc.security_id):
            if ace.flags & ACE_FLAGS.INHERIT_ONLY_ACE:
                continue
            ace_type = ACE_TYPE[ace.type]
            if ace_type not in _ALLOW_TYPES and ace_type not in _DENY_TYPES:
                continue
            if ace.object_type is not None:
                # Object-specific ACEs do not apply to plain file access
                continue
            if ace.mask is None or ace.sid not in sids:
                continue
            if ace_type in _CALLBACK_TYPES and not policy.evaluate_callback_aces:
                if ace.mask & remaining:
                    return AccessResult(
                        AccessDecision.INSUFFICIENT,
                        desc.security_id,
                        granted & desired_mask,
                        remaining,
                        ace,
                        f"decisive ACE is a conditional (callback) ACE at position {ace.position} "
                        "whose condition cannot be evaluated offline",
                    )
                continue
            if ace_type in _DENY_TYPES:
                if ace.mask & remaining:
                    return AccessResult(
                        AccessDecision.DENY,
                        desc.security_id,
                        granted & desired_mask,
                        remaining,
                        ace,
                        f"denied by ACE at position {ace.position} ({ace.type} sid={ace.sid})",
                    )
                continue
            # Allow ACE
            if ace.mask & remaining:
                granted |= ace.mask
                remaining &= ~ace.mask
                last_allow = ace
                if remaining == 0:
                    return AccessResult(
                        AccessDecision.ALLOW,
                        desc.security_id,
                        granted & desired_mask,
                        0,
                        ace,
                        f"allowed by ACE at position {ace.position} ({ace.type} sid={ace.sid})",
                    )

        if remaining == 0:
            return AccessResult(
                AccessDecision.ALLOW,
                desc.security_id,
                granted & desired_mask,
                0,
                last_allow,
                "allowed by accumulated ACEs",
            )
        return AccessResult(
            AccessDecision.DENY,
            desc.security_id,
            granted & desired_mask,
            remaining,
            None,
            f"denied: no ACE grants the remaining access mask 0x{remaining:x}",
        )
