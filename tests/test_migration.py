# -*- coding: utf-8 -*-
# Copyright: (c) 2021, Jordan Borean (@jborean93) <jborean93@gmail.com>
# MIT License (see LICENSE or https://opensource.org/licenses/MIT)

import pytest

import psrpcore
from psrpcore._crypto import PSRemotingCrypto
from psrpcore.types import (
    PSCustomObject,
    PSSecureString,
    PSString,
    add_note_property,
    deserialize,
    serialize,
)

OLD_KEY = b"\x01" * 32
NEW_KEY = b"\x02" * 32
SECRET = "super secret value"


def _cipher(key):
    cipher = PSRemotingCrypto()
    cipher.register_key(key)
    return cipher


def _secure_string(value, key=OLD_KEY):
    cipher = _cipher(key)
    return PSSecureString(cipher.encrypt(value), cipher)


def _package(secret=SECRET):
    obj = PSCustomObject()
    add_note_property(obj, "Username", PSString("admin"))
    add_note_property(obj, "Password", _secure_string(secret))
    return obj


def test_migrate_secure_string_to_new_session():
    obj = _package()

    report = psrpcore.migrate_session_object(obj, old_key=OLD_KEY, new_key=NEW_KEY)

    assert report.complete
    assert len(report.migrated) == 1
    assert report.migrated[0].path == "$.Password"
    assert report.migrated[0].status == psrpcore.MigrationStatus.MIGRATED
    assert not report.pending

    # The field is still ciphertext but now readable by the new session key.
    assert obj.Password != SECRET
    assert _cipher(NEW_KEY).decrypt(obj.Password._value) == SECRET
    with pytest.raises(Exception):
        _cipher(OLD_KEY).decrypt(obj.Password._value)


def test_migrate_leaves_plain_strings_untouched():
    obj = _package()
    username_before = obj.Username

    psrpcore.migrate_session_object(obj, old_key=OLD_KEY, new_key=NEW_KEY)

    assert obj.Username == "admin"
    assert obj.Username is username_before


def test_migrated_package_serializes_for_new_session():
    obj = _package()
    psrpcore.migrate_session_object(obj, old_key=OLD_KEY, new_key=NEW_KEY)

    new_cipher = _cipher(NEW_KEY)
    element = serialize(obj, new_cipher)
    actual = deserialize(element, new_cipher)

    assert actual.Password.decrypt() == SECRET
    assert actual.Username == "admin"


def test_missing_old_key_stays_pending():
    obj = _package()
    value_before = obj.Password._value

    report = psrpcore.migrate_session_object(obj, new_key=NEW_KEY)

    assert not report.complete
    assert not report.migrated
    assert len(report.pending) == 1
    assert report.pending[0].path == "$.Password"
    assert report.pending[0].status == psrpcore.MigrationStatus.PENDING
    assert report.pending[0].detail is not None

    # The field was not relabelled, it is still readable by the old session.
    assert obj.Password._value == value_before
    assert _cipher(OLD_KEY).decrypt(obj.Password._value) == SECRET


def test_wrong_old_key_stays_pending():
    obj = _package()
    value_before = obj.Password._value

    report = psrpcore.migrate_session_object(obj, old_key=b"\xff" * 32, new_key=NEW_KEY)

    assert not report.complete
    assert not report.migrated
    assert len(report.pending) == 1
    assert obj.Password._value == value_before
    assert _cipher(OLD_KEY).decrypt(obj.Password._value) == SECRET


def test_shared_reference_is_migrated_once():
    shared = _secure_string("shared secret")
    obj = PSCustomObject()
    add_note_property(obj, "First", shared)
    add_note_property(obj, "Second", shared)

    report = psrpcore.migrate_session_object(obj, old_key=OLD_KEY, new_key=NEW_KEY)

    assert report.complete
    assert len(report.migrated) == 1
    assert _cipher(NEW_KEY).decrypt(obj.First._value) == "shared secret"
    assert obj.Second._value == obj.First._value


def test_failed_branch_does_not_pollute_other_fields():
    obj = _package()
    broken = _secure_string("broken secret")
    broken._value = "not valid base64!!"
    add_note_property(obj, "Broken", broken)

    report = psrpcore.migrate_session_object(obj, old_key=OLD_KEY, new_key=NEW_KEY)

    assert not report.complete
    assert [f.path for f in report.migrated] == ["$.Password"]
    assert [f.path for f in report.pending] == ["$.Broken"]

    # The healthy field migrated and the broken one kept its original value.
    assert _cipher(NEW_KEY).decrypt(obj.Password._value) == SECRET
    assert obj.Broken._value == "not valid base64!!"


def test_strict_raises_with_field_path_only():
    obj = _package()

    with pytest.raises(psrpcore.SecureStringMigrationError) as exc_ctx:
        psrpcore.migrate_session_object(obj, new_key=NEW_KEY, strict=True)

    assert exc_ctx.value.path == "$.Password"
    assert "$.Password" in str(exc_ctx.value)
    assert SECRET not in str(exc_ctx.value)


def test_report_and_exception_do_not_leak_plaintext():
    obj = _package()

    report = psrpcore.migrate_session_object(obj, new_key=NEW_KEY)
    for field in report.fields:
        assert SECRET not in field.path
        assert SECRET not in (field.detail or "")
    assert SECRET not in str(report)
    assert SECRET not in repr(report)

    with pytest.raises(psrpcore.SecureStringMigrationError) as exc_ctx:
        psrpcore.migrate_session_object(_package(), new_key=NEW_KEY, strict=True)
    assert SECRET not in str(exc_ctx.value)
    assert SECRET not in repr(exc_ctx.value)


def test_migrate_nested_containers():
    obj = {
        "credentials": [_secure_string("first"), {"nested": _secure_string("second")}],
        "plain": "untouched",
    }

    report = psrpcore.migrate_session_object(obj, old_key=OLD_KEY, new_key=NEW_KEY)

    assert report.complete
    assert {f.path for f in report.migrated} == {
        "$['credentials'][0]",
        "$['credentials'][1]['nested']",
    }
    assert _cipher(NEW_KEY).decrypt(obj["credentials"][0]._value) == "first"
    assert _cipher(NEW_KEY).decrypt(obj["credentials"][1]["nested"]._value) == "second"
    assert obj["plain"] == "untouched"


def test_migrate_requires_new_key():
    with pytest.raises(ValueError, match="new_key"):
        psrpcore.migrate_session_object(_package(), old_key=OLD_KEY, new_key=None)

    with pytest.raises(ValueError, match="new_key"):
        psrpcore.migrate_session_object(_package(), old_key=OLD_KEY, new_key=b"")


def test_unencrypted_secure_string_is_not_session_bound():
    obj = PSCustomObject()
    add_note_property(obj, "Password", PSSecureString(SECRET))

    report = psrpcore.migrate_session_object(obj, old_key=OLD_KEY, new_key=NEW_KEY)

    # A plaintext secure string was never encrypted with the old session key
    # so there is nothing to re-key and nothing to report.
    assert report.complete
    assert not report.fields
    assert obj.Password.decrypt() == SECRET
