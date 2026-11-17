# -*- coding: utf-8 -*-
# Copyright: (c) 2021, Jordan Borean (@jborean93) <jborean93@gmail.com>
# MIT License (see LICENSE or https://opensource.org/licenses/MIT)

"""Migrates Secure String fields between PSRP sessions.

An object package captured in one runspace pool session can only be read by
that session: every :class:`psrpcore.types.PSSecureString` field is encrypted
with the session key exchanged for that runspace pool. This module re-keys
those fields with explicitly provided session keys so the package stays
usable - and still encrypted - in the new session. Plain strings and all
other values are left untouched.

Fields that cannot be re-keyed (for example because the old session key was
not provided or does not match) are left as they are and reported as
pending; they are never relabelled as migrated. The migration report and
the :class:`psrpcore.SecureStringMigrationError` exception locate the
affected field by path but never contain plaintext values.
"""

from __future__ import annotations

import dataclasses
import enum
import queue
import typing

from psrpcore._crypto import PSRemotingCrypto
from psrpcore._exceptions import SecureStringMigrationError
from psrpcore.types import PSObject, PSSecureString


class MigrationStatus(str, enum.Enum):
    """The migration outcome of a single secure field."""

    MIGRATED = "migrated"  #: The field was re-keyed for the new session.
    PENDING = "pending"  #: The field could not be re-keyed and is unchanged.


@dataclasses.dataclass(frozen=True)
class FieldMigration:
    """The migration outcome of a single secure field.

    This only locates the field inside the migrated object graph, it never
    contains the plaintext or ciphertext value of the field.

    Attributes:
        path: The location of the field, e.g. ``$.Credentials.Password``.
        status: The :class:`MigrationStatus` of the field.
        detail: An optional plaintext free reason, set for pending fields.
    """

    path: str
    status: MigrationStatus
    detail: typing.Optional[str] = None


class SessionMigrationReport:
    """The per field outcomes of :meth:`migrate_session_object`.

    Attributes:
        fields (Tuple[FieldMigration, ...]): The outcome of every secure
            field found in the object graph, in the order they were visited.
    """

    def __init__(self) -> None:
        self._fields: typing.List[FieldMigration] = []

    @property
    def fields(self) -> typing.Tuple[FieldMigration, ...]:
        return tuple(self._fields)

    @property
    def migrated(self) -> typing.Tuple[FieldMigration, ...]:
        """The fields that were re-keyed for the new session."""
        return tuple(f for f in self._fields if f.status == MigrationStatus.MIGRATED)

    @property
    def pending(self) -> typing.Tuple[FieldMigration, ...]:
        """The fields that could not be re-keyed and are left unchanged."""
        return tuple(f for f in self._fields if f.status == MigrationStatus.PENDING)

    @property
    def complete(self) -> bool:
        """Whether every secure field found was migrated."""
        return not self.pending

    def add(
        self,
        path: str,
        status: MigrationStatus,
        detail: typing.Optional[str] = None,
    ) -> None:
        self._fields.append(FieldMigration(path, status, detail))

    def __repr__(self) -> str:
        return f"{type(self).__name__}(migrated={len(self.migrated)}, pending={len(self.pending)})"

    def __str__(self) -> str:
        return f"{len(self.migrated)} field(s) migrated, {len(self.pending)} field(s) pending"


def migrate_session_object(
    value: typing.Any,
    *,
    new_key: bytes,
    old_key: typing.Optional[bytes] = None,
    strict: bool = False,
) -> SessionMigrationReport:
    """Re-key Secure String fields of an object package for a new session.

    Walks the object graph and re-encrypts every encrypted
    :class:`psrpcore.types.PSSecureString` field with the new session key so
    the package can be used in the new session. The field values remain
    ciphertext, they are just readable by the new session instead of the old
    one. Plain strings and all other values are not transformed.

    Each distinct object is migrated once, objects referenced multiple times
    are not re-processed. A field that fails to migrate keeps its original
    value and does not affect the migration of any other field.

    Args:
        value: The object package to migrate, modified in place.
        new_key: The session key of the new (target) session, the migrated
            fields are encrypted with this key.
        old_key: The session key of the old (source) session the fields are
            currently encrypted with. If omitted, every encrypted field is
            reported as pending and left unchanged.
        strict: Raise :class:`psrpcore.SecureStringMigrationError` on the
            first field that cannot be migrated instead of just recording it
            as pending in the report.

    Returns:
        SessionMigrationReport: The per field outcomes. Fields that could
        not be migrated are reported as pending, never as migrated.
    """
    if not isinstance(new_key, bytes) or not new_key:
        raise ValueError("new_key must be provided as the new session key bytes")

    new_cipher = PSRemotingCrypto()
    new_cipher.register_key(new_key)

    old_cipher = None
    if old_key is not None:
        old_cipher = PSRemotingCrypto()
        old_cipher.register_key(old_key)

    state = _MigrationState(old_cipher, new_cipher, strict)
    _walk(value, "$", state)

    return state.report


class _MigrationState:
    """Bookkeeping shared across the migration walk."""

    def __init__(
        self,
        old_cipher: typing.Optional[PSRemotingCrypto],
        new_cipher: PSRemotingCrypto,
        strict: bool,
    ) -> None:
        self.old_cipher = old_cipher
        self.new_cipher = new_cipher
        self.strict = strict
        self.report = SessionMigrationReport()
        self.seen: typing.Set[int] = set()

    def fail(
        self,
        path: str,
        reason: str,
    ) -> None:
        self.report.add(path, MigrationStatus.PENDING, reason)
        if self.strict:
            raise SecureStringMigrationError(path, reason)


def _walk(
    value: typing.Any,
    path: str,
    state: _MigrationState,
) -> None:
    if isinstance(value, PSSecureString):
        _migrate_field(value, path, state)
        return

    # Plain strings, numbers, and other simple values are never transformed.
    if value is None or isinstance(value, (str, bytes, bool, int, float, complex)):
        return

    value_id = id(value)
    if value_id in state.seen:
        return
    state.seen.add(value_id)

    if isinstance(value, dict):
        for dict_key, dict_value in value.items():
            _walk(dict_value, f"{path}[{dict_key!r}]", state)

    elif isinstance(value, (list, tuple, set, frozenset)):
        for idx, entry in enumerate(value):
            _walk(entry, f"{path}[{idx}]", state)

    elif isinstance(value, queue.Queue):
        for idx, entry in enumerate(value.queue):
            _walk(entry, f"{path}[{idx}]", state)

    if isinstance(value, PSObject):
        seen_props: typing.Set[str] = set()
        for properties in [value.PSObject.adapted_properties, value.PSObject.extended_properties]:
            for prop in properties:
                if prop.name in seen_props:
                    continue
                seen_props.add(prop.name)

                try:
                    prop_value = prop.get_value(value)
                except Exception:
                    continue

                _walk(prop_value, f"{path}.{prop.name}", state)


def _migrate_field(
    value: PSSecureString,
    path: str,
    state: _MigrationState,
) -> None:
    value_id = id(value)
    if value_id in state.seen:
        # A shared reference, already migrated once.
        return
    state.seen.add(value_id)

    if not value._encrypted:
        # The value is not ciphertext bound to the old session so there is
        # nothing to re-key.
        return

    if state.old_cipher is None:
        state.fail(path, "old session key was not provided")
        return

    try:
        plaintext = state.old_cipher.decrypt(value._value)
    except Exception:
        state.fail(path, "cannot be decrypted with the provided old session key")
        return

    try:
        new_value = state.new_cipher.encrypt(plaintext)
    except Exception:
        state.fail(path, "cannot be encrypted for the new session")
        return
    finally:
        plaintext = None

    # Only swap the value once the new ciphertext exists so a failure leaves
    # the field untouched.
    value._value = new_value
    value._cipher = state.new_cipher
    state.report.add(path, MigrationStatus.MIGRATED)


__all__ = [
    "FieldMigration",
    "MigrationStatus",
    "SessionMigrationReport",
    "migrate_session_object",
]
