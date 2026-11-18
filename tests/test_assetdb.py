from __future__ import annotations

import io
import struct
from typing import BinaryIO

import pytest
from dissect.util.sid import write_sid

from dissect.ntfs.assetdb import (
    AccessCheckPolicy,
    AccessDecision,
    AssetDatabase,
    DaclState,
    Token,
)
from dissect.ntfs.c_ntfs import ACE_TYPE, c_ntfs
from dissect.ntfs.ntfs import NTFS
from dissect.ntfs.secure import Secure

ACE_FLAGS = c_ntfs.ACE_FLAGS
SE_SELF_RELATIVE = int(c_ntfs.SECURITY_DESCRIPTOR_CONTROL.SE_SELF_RELATIVE)
SE_DACL_PRESENT = int(c_ntfs.SECURITY_DESCRIPTOR_CONTROL.SE_DACL_PRESENT)
SE_DACL_PROTECTED = int(c_ntfs.SECURITY_DESCRIPTOR_CONTROL.SE_DACL_PROTECTED)

READ_CONTROL = 0x00020000
WRITE_DAC = 0x00040000
FILE_READ_DATA = 0x00000001
GENERIC_ALL = 0x10000000

SYSTEM = "S-1-5-18"
ADMINISTRATORS = "S-1-5-32-544"
USERS = "S-1-5-32-545"
EVERYONE = "S-1-1-0"
ALICE = "S-1-5-21-1-2-3-1001"


def make_ace(ace_type: int, flags: int, mask: int, sid: str) -> bytes:
    body = struct.pack("<I", mask) + write_sid(sid)
    return struct.pack("<BBH", ace_type, flags, 4 + len(body)) + body


def make_acl(aces: list[bytes]) -> bytes:
    body = b"".join(aces)
    return struct.pack("<BBHHH", 2, 0, 8 + len(body), len(aces), 0) + body


def make_sd(
    owner: str = SYSTEM,
    group: str = ADMINISTRATORS,
    dacl: bytes | None = None,
    control: int = SE_SELF_RELATIVE | SE_DACL_PRESENT,
) -> bytes:
    owner_bytes = write_sid(owner)
    group_bytes = write_sid(group)
    owner_offset = 20
    group_offset = owner_offset + len(owner_bytes)
    dacl_offset = group_offset + len(group_bytes) if dacl is not None else 0
    header = struct.pack(
        "<BBHIIII",
        1,
        0,
        control,
        owner_offset,
        group_offset,
        0,
        dacl_offset,
    )
    return header + owner_bytes + group_bytes + (dacl or b"")


def make_sds(entries: list[tuple[int, bytes]]) -> BinaryIO:
    out = bytearray()
    for security_id, sd in entries:
        out += struct.pack("<IIQI", 0, security_id, len(out), 20 + len(sd)) + sd
        out += b"\x00" * (-len(out) & 0xF)
    return io.BytesIO(bytes(out))


def make_db(entries: list[tuple[int, bytes]]) -> AssetDatabase:
    db = AssetDatabase()
    db.ingest_secure(Secure(sds=make_sds(entries)))
    return db


@pytest.fixture
def volume_db(ntfs_bin: BinaryIO) -> AssetDatabase:
    db = AssetDatabase()
    db.ingest(NTFS(ntfs_bin))
    return db


def test_ingest_volume_preserves_shared_references_and_order(volume_db: AssetDatabase) -> None:
    files = volume_db.files()
    assert files
    assert all(f.security_id is not None for f in files)

    # Multiple files share the same descriptor row (stored exactly once)
    shared = [f for f in files if f.security_id == 256]
    assert len(shared) > 1
    assert len({f.security_id for f in files}) < len(files)

    desc = volume_db.descriptor(256)
    assert desc.owner == SYSTEM
    assert desc.group == ADMINISTRATORS
    assert desc.dacl_state is DaclState.NORMAL

    # ACE order is preserved by position
    aces = volume_db.aces(256)
    assert [ace.position for ace in aces] == [0, 1]
    assert aces[0].type == "ACCESS_ALLOWED"
    assert aces[0].mask == 0x120089
    assert aces[0].sid == SYSTEM
    assert aces[1].sid == ADMINISTRATORS


def test_ingest_volume_records_paths_and_parents(volume_db: AssetDatabase) -> None:
    with_path = [f for f in volume_db.files() if f.path]
    assert with_path
    assert all(f.parent_known for f in with_path)

    # Querying by a stored path resolves to the same file as by segment
    target = with_path[0]
    token = Token(SYSTEM)
    by_segment = volume_db.check_access(target.segment, token, 0x120089)
    by_path = volume_db.check_access(target.path, token, 0x120089)
    assert by_segment.decision is by_path.decision
    assert by_segment.security_id == by_path.security_id == target.security_id


def test_access_check_against_real_descriptor(volume_db: AssetDatabase) -> None:
    segment = volume_db.files(256)[0].segment
    # SYSTEM is granted 0x120089 by the first ACE
    result = volume_db.check_access(segment, Token(SYSTEM), 0x120089)
    assert result.decision is AccessDecision.ALLOW
    assert result.decisive_ace is not None
    assert result.decisive_ace.position == 0
    assert result.decisive_ace.sid == SYSTEM

    # Membership in Administrators grants the same mask through the second ACE
    result = volume_db.check_access(segment, Token(ALICE, (ADMINISTRATORS,)), 0x120089)
    assert result.decision is AccessDecision.ALLOW
    assert result.decisive_ace.position == 1

    # Requesting more than granted is an implicit deny with the remaining mask reported
    result = volume_db.check_access(segment, Token(SYSTEM), GENERIC_ALL)
    assert result.decision is AccessDecision.DENY
    assert result.decisive_ace is None
    assert result.remaining_mask

    # A token with no matching ACE is denied
    result = volume_db.check_access(segment, Token(ALICE, (USERS,)), 0x120089)
    assert result.decision is AccessDecision.DENY


def test_empty_dacl_denies_everyone() -> None:
    db = make_db([(300, make_sd(dacl=make_acl([])))])
    desc = db.descriptor(300)
    assert desc.dacl_state is DaclState.EMPTY
    assert "empty DACL" in db.explain_dacl_state(300)
    assert "no one" in db.explain_dacl_state(300)

    result = db.check_access(300, Token(EVERYONE), FILE_READ_DATA)
    # Segment 300 has no file row, so there is nothing to evaluate
    assert result.decision is AccessDecision.INSUFFICIENT

    db.add_file(42, "/x", None, False, 300)
    result = db.check_access(42, Token(EVERYONE), FILE_READ_DATA)
    assert result.decision is AccessDecision.DENY
    assert result.decisive_ace is None
    assert "empty DACL" in result.reason


def test_null_dacl_allows_everyone() -> None:
    db = make_db([(301, make_sd(control=SE_SELF_RELATIVE))])
    desc = db.descriptor(301)
    assert desc.dacl_state is DaclState.NULL
    assert "no DACL" in db.explain_dacl_state(301)
    assert "full control" in db.explain_dacl_state(301)

    db.add_file(43, "/y", 5, True, 301)
    result = db.check_access(43, Token(ALICE), GENERIC_ALL)
    assert result.decision is AccessDecision.ALLOW
    assert result.decisive_ace is None
    assert "NULL DACL" in result.reason


def test_unreadable_descriptor_is_isolated() -> None:
    good = make_sd(dacl=make_acl([make_ace(ACE_TYPE.ACCESS_ALLOWED, 0, FILE_READ_DATA, EVERYONE)]))
    # Corrupt: DACL offset points far beyond the end of the descriptor
    broken = bytearray(make_sd(dacl=make_acl([])))
    struct.pack_into("<I", broken, 16, 0xFFFF)
    db = make_db([(302, good), (303, bytes(broken)), (304, good)])

    assert db.descriptor(302).dacl_state is DaclState.NORMAL
    assert db.descriptor(303).dacl_state is DaclState.UNREADABLE
    assert db.descriptor(304).dacl_state is DaclState.NORMAL
    assert "unreadable" in db.explain_dacl_state(303)

    db.add_file(44, "/a", 5, True, 303)
    db.add_file(45, "/b", 5, True, 304)

    # The bad descriptor yields insufficient evidence, not a guess
    result = db.check_access(44, Token(ALICE, (EVERYONE,)), FILE_READ_DATA)
    assert result.decision is AccessDecision.INSUFFICIENT
    assert result.decisive_ace is None

    # The unrelated file sharing the good descriptor is unaffected
    result = db.check_access(45, Token(ALICE, (EVERYONE,)), FILE_READ_DATA)
    assert result.decision is AccessDecision.ALLOW
    assert result.decisive_ace.position == 0


def test_deny_ace_is_decisive() -> None:
    # Canonical order: deny ACEs come first
    dacl = make_acl(
        [
            make_ace(ACE_TYPE.ACCESS_DENIED, 0, FILE_READ_DATA, ALICE),
            make_ace(ACE_TYPE.ACCESS_ALLOWED, 0, FILE_READ_DATA | 0x00000002, EVERYONE),
        ]
    )
    db = make_db([(305, make_sd(dacl=dacl))])
    db.add_file(46, "/c", 5, True, 305)

    result = db.check_access(46, Token(ALICE, (EVERYONE,)), FILE_READ_DATA)
    assert result.decision is AccessDecision.DENY
    assert result.decisive_ace.position == 0
    assert result.decisive_ace.type == "ACCESS_DENIED"

    # Write access is still allowed for Alice by the second ACE
    result = db.check_access(46, Token(ALICE, (EVERYONE,)), 0x00000002)
    assert result.decision is AccessDecision.ALLOW
    assert result.decisive_ace.position == 1


def test_inheritance_source_is_preserved() -> None:
    inherited = int(ACE_FLAGS.INHERITED_ACE)
    dacl = make_acl(
        [
            make_ace(ACE_TYPE.ACCESS_ALLOWED, 0, FILE_READ_DATA, EVERYONE),
            make_ace(ACE_TYPE.ACCESS_ALLOWED, inherited, READ_CONTROL, USERS),
        ]
    )
    db = make_db([(306, make_sd(dacl=dacl, control=SE_SELF_RELATIVE | SE_DACL_PRESENT | SE_DACL_PROTECTED))])

    desc = db.descriptor(306)
    assert desc.dacl_protected
    aces = db.aces(306)
    assert [ace.inherited for ace in aces] == [False, True]
    assert "inheritance is blocked" in db.explain_dacl_state(306)

    # A protected DACL is evaluated exactly as stored; nothing is backfilled from parents
    db.add_file(47, None, None, False, 306)
    result = db.check_access(47, Token(ALICE, (EVERYONE,)), FILE_READ_DATA)
    assert result.decision is AccessDecision.ALLOW
    assert "SE_DACL_PROTECTED" in result.reason
    assert "no inherited information backfilled" in result.reason


def test_privileges_and_owner_rights_are_policy_driven() -> None:
    db = make_db([(307, make_sd(owner=ALICE, dacl=make_acl([])))])
    db.add_file(48, "/d", 5, True, 307)

    # SeBackupPrivilege grants read access even with an empty DACL
    result = db.check_access(48, Token(SYSTEM, (), ("SeBackupPrivilege",)), FILE_READ_DATA)
    assert result.decision is AccessDecision.ALLOW
    assert "privileges" in result.reason

    # The owner implicitly gets READ_CONTROL | WRITE_DAC under the default policy
    result = db.check_access(48, Token(ALICE), READ_CONTROL | WRITE_DAC)
    assert result.decision is AccessDecision.ALLOW
    assert "owner" in result.reason

    # An explicit policy without owner rights denies the same request
    policy = AccessCheckPolicy(owner_rights=0, privilege_rights={})
    result = db.check_access(48, Token(ALICE), READ_CONTROL, policy)
    assert result.decision is AccessDecision.DENY

    # A privilege not present in the token grants nothing
    result = db.check_access(48, Token(SYSTEM), FILE_READ_DATA)
    assert result.decision is AccessDecision.DENY


def test_callback_ace_yields_insufficient() -> None:
    dacl = make_acl([make_ace(ACE_TYPE.ACCESS_ALLOWED_CALLBACK, 0, FILE_READ_DATA, ALICE)])
    db = make_db([(308, make_sd(dacl=dacl))])
    db.add_file(49, "/e", 5, True, 308)

    result = db.check_access(49, Token(ALICE), FILE_READ_DATA)
    assert result.decision is AccessDecision.INSUFFICIENT
    assert result.decisive_ace.position == 0

    # Unless the policy says callback ACEs can be evaluated
    policy = AccessCheckPolicy(evaluate_callback_aces=True)
    result = db.check_access(49, Token(ALICE), FILE_READ_DATA, policy)
    assert result.decision is AccessDecision.ALLOW


def test_unknown_target_is_insufficient_not_guessed() -> None:
    db = make_db([(309, make_sd(dacl=make_acl([])))])
    assert db.check_access(999, Token(SYSTEM), FILE_READ_DATA).decision is AccessDecision.INSUFFICIENT
    assert db.check_access("/missing", Token(SYSTEM), FILE_READ_DATA).decision is AccessDecision.INSUFFICIENT

    # A file referencing a descriptor that is not in the database
    db.add_file(50, "/f", 5, True, 12345)
    result = db.check_access(50, Token(SYSTEM), FILE_READ_DATA)
    assert result.decision is AccessDecision.INSUFFICIENT
    assert "not present" in result.reason
