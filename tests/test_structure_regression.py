"""Structural regression corpus for the LNK parsing entry point.

Every sample below is built in memory from an explicitly documented byte
layout. No sample is read from disk and none of the shortcut targets they
describe is ever accessed. The goal is to drive the real branches behind:

* LinkInfo (``LnkParse3/lnk_info.py`` plus ``info/local.py``, ``info/network.py``)
* StringData (``LnkParse3/string_data.py``)
* ExtraData (``LnkParse3/extra_data.py`` and the ``extra/`` blocks)

The cases fall into three expectations:

* accepted  - the complete declared structure materializes through ``get_json``
* rejected  - full materialization is impossible (raised ``struct.error``)
* partial   - construction survives but only part of the declaration can be
              handed over, signalled by a warning, a size/stream mismatch or a
              value that visibly crosses another field's boundary

Each test first re-derives offsets and extents from the produced bytes with
``struct`` (an independent, checkable layout declaration) and only then parses
them with the library.
"""

import json
import struct
import unittest
import warnings
from contextlib import redirect_stdout
from io import StringIO

import LnkParse3


# ---------------------------------------------------------------------------
# Fixed ShellLinkHeader facts ([MS-SHLLINK] 2.1)
# ---------------------------------------------------------------------------

HEADER_SIZE = 0x4C
LINK_CLSID = bytes.fromhex("0114020000000000C000000000000046")

FLAG_HAS_LINK_INFO = 0x00000002
FLAG_HAS_NAME = 0x00000004
FLAG_HAS_RELATIVE_PATH = 0x00000008
FLAG_HAS_ARGUMENTS = 0x00000020
FLAG_IS_UNICODE = 0x00000080

TERMINAL_BLOCK = b"\x00\x00\x00\x00"

# Every corpus sample stays well below this bound; parsing is in-memory only.
MAX_CORPUS_SIZE = 512


def shell_header(link_flags):
    """Build a 76-byte ShellLinkHeader with the given LinkFlags.

    Layout: HeaderSize(4) + LinkCLSID(16) + LinkFlags(4) + FileAttributes(4)
    + three FILETIMEs(24) + FileSize(4) + IconIndex(4) + ShowCommand(4)
    + HotKey(2) + Reserved1(2) + Reserved2(4) + Reserved3(4).
    """
    header = struct.pack("<I", HEADER_SIZE) + LINK_CLSID
    header += struct.pack("<II", link_flags, 0)
    header += b"\x00" * (HEADER_SIZE - len(header))
    assert len(header) == HEADER_SIZE
    return header


# ---------------------------------------------------------------------------
# LinkInfo builders
# ---------------------------------------------------------------------------


def build_local_link_info():
    """ANSI Local LinkInfo, header size 0x1C (no unicode offset fields).

    LinkInfo header (28 bytes) at offsets 0..27:
        LinkInfoSize(4) LinkInfoHeaderSize(4)=0x1C LinkInfoFlags(4)=1
        VolumeIDOffset(4)=0x1C LocalBasePathOffset(4)
        CommonNetworkRelativeLinkOffset(4)=0 CommonPathSuffixOffset(4)
    VolumeID at 0x1C, 20 bytes:
        VolumeIDSize(4)=20 DriveType(4)=3 DriveSerial(4)=0x11223344
        VolumeLabelOffset(4)=0x10 followed by "LBL\\0"
    LocalBasePath at 0x30: "C:\\D\\f.txt\\0" (11 bytes)
    CommonPathSuffix at 0x3B: "sub\\0" (4 bytes), block ends at 0x3F (63).
    """
    volume_id = struct.pack("<IIII", 20, 3, 0x11223344, 0x10) + b"LBL\x00"
    local_base_path = b"C:\\D\\f.txt\x00"
    suffix = b"sub\x00"

    volume_id_offset = 0x1C
    local_base_path_offset = volume_id_offset + len(volume_id)
    suffix_offset = local_base_path_offset + len(local_base_path)
    link_info_size = suffix_offset + len(suffix)

    header = struct.pack(
        "<IIIIIII",
        link_info_size,
        0x1C,
        0x1,
        volume_id_offset,
        local_base_path_offset,
        0,
        suffix_offset,
    )
    return header + volume_id + local_base_path + suffix


def build_unicode_local_link_info():
    """Local LinkInfo, header size 0x24, with both unicode offset fields.

    Header (36 bytes) layout adds LocalBasePathOffsetUnicode(4) at 28 and
    CommonPathSuffixOffsetUnicode(4) at 32. Body order and extents:
        VolumeID            0x24 .. 0x38 (20 bytes)
        LocalBasePath       0x38 .. 0x43 (11 bytes, ANSI)
        CommonPathSuffix    0x43 .. 0x47 (4 bytes, ANSI)
        LocalBasePathUnicode   0x47 .. 0x5D (22 bytes, 10 UTF-16 chars + NUL)
        CommonPathSuffixUnicode 0x5D .. 0x65 (8 bytes, 3 UTF-16 chars + NUL)
    """
    volume_id = struct.pack("<IIII", 20, 3, 0x11223344, 0x10) + b"LBL\x00"
    local_base_path = b"C:\\D\\f.txt\x00"
    suffix = b"sub\x00"
    local_base_path_u = "C:\\D\\f.txt".encode("utf-16le") + b"\x00\x00"
    suffix_u = "sub".encode("utf-16le") + b"\x00\x00"

    volume_id_offset = 0x24
    local_base_path_offset = volume_id_offset + len(volume_id)
    suffix_offset = local_base_path_offset + len(local_base_path)
    local_base_path_u_offset = suffix_offset + len(suffix)
    suffix_u_offset = local_base_path_u_offset + len(local_base_path_u)
    link_info_size = suffix_u_offset + len(suffix_u)

    header = struct.pack(
        "<IIIIIIIII",
        link_info_size,
        0x24,
        0x1,
        volume_id_offset,
        local_base_path_offset,
        0,
        suffix_offset,
        local_base_path_u_offset,
        suffix_u_offset,
    )
    body = volume_id + local_base_path + suffix + local_base_path_u + suffix_u
    return header + body


def build_network_link_info(net_name_offset=20, device_name_offset=None):
    """Network LinkInfo, header size 0x1C, LinkInfoFlags=2.

    CommonNetworkRelativeLink at LinkInfo offset 0x1C (28):
        CNRSize(4) CNRFlags(4)=0 NetNameOffset(4) DeviceNameOffset(4)
        NetworkProviderType(4)=0, then NetName "//srv/share\\0" (12 bytes)
        and DeviceName "Z:\\0" (3 bytes); CNR size is 35.
    CommonPathSuffix "dir\\0" starts at 0x3F and ends the 67-byte block.

    The two internal offsets may be overridden to craft boundary violations.
    """
    net_name = b"\\\\srv\\share\x00"
    device_name = b"Z:\x00"
    cnr_body = net_name + device_name

    if device_name_offset is None:
        device_name_offset = 20 + len(net_name)
    cnr_size = 20 + len(cnr_body)
    cnr = (
        struct.pack(
            "<IIIII",
            cnr_size,
            0,
            net_name_offset,
            device_name_offset,
            0,
        )
        + cnr_body
    )

    cnr_offset = 0x1C
    suffix_offset = cnr_offset + len(cnr)
    link_info_size = suffix_offset + len(b"dir\x00")

    header = struct.pack(
        "<IIIIIII",
        link_info_size,
        0x1C,
        0x2,
        0,
        0,
        cnr_offset,
        suffix_offset,
    )
    return header + cnr + b"dir\x00"


# ---------------------------------------------------------------------------
# ExtraData block builders
# ---------------------------------------------------------------------------


def codepage_block(code_page=1252, block_size=12):
    """CodePage block: BlockSize(4) + Signature 0xA0000004(4) + CodePage(4)."""
    return struct.pack("<II", block_size, 0xA0000004) + struct.pack("<I", code_page)


def special_folder_block():
    """SpecialFolder block (16 bytes): signature 0xA0000005 + id + offset."""
    return struct.pack("<II", 16, 0xA0000005) + struct.pack("<II", 5, 0)


def unknown_block(payload=b"abcdefgh", signature=0xDEADBEEF):
    """Well-formed block carrying an unknown signature."""
    return struct.pack("<II", 8 + len(payload), signature) + payload


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def parse_lnk(data, **kwargs):
    """Parse in-memory bytes, returning ``(lnk, warning_messages)``."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        lnk = LnkParse3.lnk_file(indata=data, **kwargs)
    return lnk, [str(item.message) for item in caught]


def parse_lnk_expecting_struct_error(data, **kwargs):
    """Assert that constructing the LNK raises ``struct.error``."""
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        try:
            LnkParse3.lnk_file(indata=data, **kwargs)
        except struct.error:
            return
        raise AssertionError("expected struct.error to be raised")


def collect_extra_warnings(lnk):
    """Materialize the extra blocks and capture their lazy warnings.

    Block payload errors are raised from ``ExtraData.as_dict`` rather than
    from the constructor, so the warning has to be collected at that point.
    """
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        lnk.extras.as_dict()
    return [str(item.message) for item in caught]


def materialize_json(lnk):
    """Run the same full materialization ``print_json`` relies on.

    Returns ``(ok, payload_or_error)``. Several lenient parses construct fine
    but fail here, which is how "rejected" cases are distinguished.
    """
    buffer = StringIO()
    try:
        with redirect_stdout(buffer):
            lnk.print_json(print_all=True)
        return True, json.loads(buffer.getvalue())
    except struct.error as exc:
        return False, exc


def independent_link_info_violations(raw):
    """Re-derive LinkInfo layout independently of the library.

    Checks the two invariants the parser itself does not enforce:
    every declared offset must stay inside the declared LinkInfoSize, and an
    offset must not point into a sibling field's extent.
    """
    violations = []
    link_info_size, _header_size, flags = struct.unpack("<III", raw[0:12])

    if link_info_size > len(raw):
        violations.append("link_info_size_beyond_available_bytes")
    elif link_info_size < len(raw):
        violations.append("link_info_size_smaller_than_available_bytes")

    offsets = {
        "volume_id": struct.unpack("<I", raw[12:16])[0],
        "local_base_path": struct.unpack("<I", raw[16:20])[0],
        "common_network_relative_link": struct.unpack("<I", raw[20:24])[0],
        "common_path_suffix": struct.unpack("<I", raw[24:28])[0],
    }
    for name, offset in offsets.items():
        if offset and offset >= link_info_size:
            violations.append(f"{name}_offset_outside_block")

    if flags & 0x1 and offsets["local_base_path"] and offsets["common_path_suffix"]:
        local_base_path_offset = offsets["local_base_path"]
        suffix_offset = offsets["common_path_suffix"]
        terminator = raw.find(b"\x00", local_base_path_offset)
        local_extent_end = terminator + 1 if terminator != -1 else link_info_size
        if local_base_path_offset < suffix_offset < local_extent_end:
            violations.append("common_path_suffix_aliases_local_base_path")
    return violations


class LinkInfoStructureTests(unittest.TestCase):
    def test_valid_local_link_info_reaches_volume_and_string_branches(self):
        # Accepted: VolumeID, LocalBasePath and CommonPathSuffix all decode.
        link_info = build_local_link_info()
        self.assertEqual(struct.unpack("<I", link_info[0:4])[0], len(link_info))
        self.assertEqual(independent_link_info_violations(link_info), [])

        data = shell_header(FLAG_HAS_LINK_INFO) + link_info + TERMINAL_BLOCK
        self.assertLess(len(data), MAX_CORPUS_SIZE)

        lnk, warning_messages = parse_lnk(data)
        self.assertEqual(warning_messages, [])
        self.assertEqual(lnk.size, len(data))
        self.assertEqual(lnk.info.location(), "Local")
        self.assertEqual(lnk.info.drive_type(), "DRIVE_FIXED")
        self.assertEqual(lnk.info.volume_label(), "LBL")
        self.assertEqual(lnk.info.local_base_path(), "C:\\D\\f.txt")
        self.assertEqual(lnk.info.common_path_suffix(), "sub")

        ok, payload = materialize_json(lnk)
        self.assertTrue(ok, payload)

    def test_valid_network_link_info_reaches_common_network_branches(self):
        # Accepted: Network class, NetName and DeviceName ANSI strings.
        link_info = build_network_link_info()
        self.assertEqual(struct.unpack("<I", link_info[0:4])[0], len(link_info))
        self.assertEqual(independent_link_info_violations(link_info), [])

        data = shell_header(FLAG_HAS_LINK_INFO) + link_info + TERMINAL_BLOCK
        lnk, warning_messages = parse_lnk(data)
        self.assertEqual(warning_messages, [])
        self.assertEqual(lnk.size, len(data))
        self.assertEqual(lnk.info.location(), "Network")
        self.assertEqual(lnk.info.net_name(), "\\\\srv\\share")
        self.assertEqual(lnk.info.device_name(), "Z:")
        self.assertEqual(lnk.info.common_path_suffix(), "dir")

        ok, payload = materialize_json(lnk)
        self.assertTrue(ok, payload)

    def test_valid_unicode_offsets_reach_unicode_string_branches(self):
        # Accepted: header 0x24 activates the unicode offset accessors.
        link_info = build_unicode_local_link_info()
        self.assertEqual(struct.unpack("<I", link_info[4:8])[0], 0x24)
        self.assertEqual(independent_link_info_violations(link_info), [])

        data = shell_header(FLAG_HAS_LINK_INFO) + link_info + TERMINAL_BLOCK
        lnk, warning_messages = parse_lnk(data)
        self.assertEqual(warning_messages, [])
        self.assertEqual(lnk.info.local_base_path_unicode(), "C:\\D\\f.txt")
        self.assertIsNotNone(lnk.info.common_path_suffix_offset_unicode())

        ok, payload = materialize_json(lnk)
        self.assertTrue(ok, payload)
        self.assertIn("local_base_path_unicode", payload["link_info"])

    def test_volume_id_offset_outside_block_is_rejected_on_materialization(self):
        # Rejected (full output): VolumeIDOffset 9999 exceeds LinkInfoSize.
        link_info = build_local_link_info()
        link_info = link_info[:12] + struct.pack("<I", 9999) + link_info[16:]
        self.assertIn(
            "volume_id_offset_outside_block",
            independent_link_info_violations(link_info),
        )

        data = shell_header(FLAG_HAS_LINK_INFO) + link_info + TERMINAL_BLOCK
        lnk, warning_messages = parse_lnk(data)
        # Construction is lenient and keeps partial header data...
        self.assertIsNotNone(lnk.info)
        self.assertEqual(lnk.info.common_path_suffix(), "sub")
        # ...but reaching the VolumeID branch cannot materialize.
        ok, error = materialize_json(lnk)
        self.assertFalse(ok)
        self.assertIsInstance(error, struct.error)
        self.assertEqual(warning_messages, [])

    def test_truncated_link_info_header_is_rejected_on_materialization(self):
        # Rejected (full output): only 20 of 28 header bytes remain, so the
        # CommonPathSuffixOffset field itself is unreadable. The failure stays
        # inside LinkInfo; the ShellLinkHeader is untouched.
        link_info = build_local_link_info()[:20]
        data = shell_header(FLAG_HAS_LINK_INFO) + link_info + TERMINAL_BLOCK

        lnk, warning_messages = parse_lnk(data)
        self.assertIsNotNone(lnk.info)
        ok, error = materialize_json(lnk)
        self.assertFalse(ok)
        self.assertIsInstance(error, struct.error)
        self.assertEqual(warning_messages, [])

    def test_declared_size_smaller_than_block_leaves_terminal_residue(self):
        # Partial: LinkInfoSize is shortened by one while all bytes stay.
        # The parser consumes the declared size only, so the orphaned byte is
        # reinterpreted as the start of the ExtraData section.
        link_info = build_local_link_info()
        declared_size = struct.unpack("<I", link_info[0:4])[0]
        link_info = struct.pack("<I", declared_size - 1) + link_info[4:]
        self.assertIn(
            "link_info_size_smaller_than_available_bytes",
            independent_link_info_violations(link_info),
        )

        data = shell_header(FLAG_HAS_LINK_INFO) + link_info + TERMINAL_BLOCK
        lnk, _ = parse_lnk(data)
        self.assertEqual(lnk.size, len(data))
        names = [extra.name() for extra in lnk.extras.data]
        self.assertIn("TERMINAL_BLOCK", names)
        self.assertEqual(lnk.info.size(), declared_size - 1)

    def test_truncated_block_still_decodes_unterminated_suffix_with_warning(self):
        # Partial: the final NUL of CommonPathSuffix is cut off together with
        # one block byte. The string accessor tolerates the missing
        # terminator; the following section reports the boundary damage.
        link_info = build_local_link_info()[:-1]
        data = shell_header(FLAG_HAS_LINK_INFO) + link_info + TERMINAL_BLOCK

        lnk, warning_messages = parse_lnk(data)
        self.assertEqual(lnk.size, len(data))
        self.assertEqual(lnk.info.common_path_suffix(), "sub")
        self.assertTrue(any("Error while parsing extra data" in msg for msg in warning_messages))

    def test_suffix_offset_into_sibling_field_cross_parses_foreign_bytes(self):
        # Library accepts, independent verifier rejects: the suffix offset is
        # moved two bytes into LocalBasePath. The decoded suffix is then made
        # of another field's bytes and cannot match the declared suffix field.
        link_info = build_local_link_info()
        local_base_path_offset = struct.unpack("<I", link_info[16:20])[0]
        hijacked_offset = local_base_path_offset + 2
        link_info = link_info[:24] + struct.pack("<I", hijacked_offset) + link_info[28:]

        violations = independent_link_info_violations(link_info)
        self.assertTrue(any("common_path_suffix" in item for item in violations), violations)

        data = shell_header(FLAG_HAS_LINK_INFO) + link_info + TERMINAL_BLOCK
        canonical, _ = parse_lnk(
            shell_header(FLAG_HAS_LINK_INFO) + build_local_link_info() + TERMINAL_BLOCK
        )
        lnk, warning_messages = parse_lnk(data)
        self.assertEqual(warning_messages, [])
        self.assertEqual(lnk.info.common_path_suffix(), "\\D\\f.txt")
        # The same library cannot turn the canonical layout into this value.
        self.assertNotEqual(
            canonical.info.common_path_suffix(),
            lnk.info.common_path_suffix(),
        )

    def test_suffix_offset_beyond_block_end_reads_next_section_silently(self):
        # Library accepts, independent verifier rejects: offset 9999 walks
        # past LinkInfo into the terminal block, yielding an empty string.
        link_info = build_local_link_info()
        link_info = link_info[:24] + struct.pack("<I", 9999) + link_info[28:]
        self.assertIn(
            "common_path_suffix_offset_outside_block",
            independent_link_info_violations(link_info),
        )

        data = shell_header(FLAG_HAS_LINK_INFO) + link_info + TERMINAL_BLOCK
        lnk, warning_messages = parse_lnk(data)
        self.assertEqual(warning_messages, [])
        self.assertEqual(lnk.info.common_path_suffix(), "")
        self.assertEqual(lnk.size, len(data))

    def test_network_net_name_offset_into_device_field_drops_ansi_strings(self):
        # Partial: NetNameOffset is retargeted inside the DeviceName extent.
        # Both ANSI accessors then disable themselves (offset > 20) and the
        # unicode accessors read ANSI region bytes instead.
        link_info = build_network_link_info(net_name_offset=34, device_name_offset=32)
        data = shell_header(FLAG_HAS_LINK_INFO) + link_info + TERMINAL_BLOCK

        lnk, _ = parse_lnk(data)
        self.assertEqual(lnk.info.net_name_offset(), 34)
        self.assertIsNone(lnk.info.net_name())
        self.assertIsNone(lnk.info.device_name())
        ok, payload = materialize_json(lnk)
        self.assertTrue(ok, payload)
        self.assertNotIn("net_name", payload["link_info"]["location_info"])


class StringDataStructureTests(unittest.TestCase):
    def test_ansi_and_unicode_strings_decode_with_full_size_accounting(self):
        # Accepted: NAME_STRING in both encodings, count in characters.
        ansi = struct.pack("<H", 3) + b"abc\x00"
        unicode_string = struct.pack("<H", 3) + "abc".encode("utf-16le") + b"\x00\x00"

        for flags, section, expected_text in (
            (FLAG_HAS_NAME, ansi, "abc"),
            (FLAG_HAS_NAME | FLAG_IS_UNICODE, unicode_string, "abc"),
        ):
            with self.subTest(flags=flags):
                data = shell_header(flags) + section + TERMINAL_BLOCK
                lnk, warning_messages = parse_lnk(data)
                self.assertEqual(warning_messages, [])
                self.assertEqual(lnk.size, len(data))
                self.assertEqual(lnk.string_data.description(), expected_text)

    def test_unicode_count_given_in_bytes_overruns_into_terminal_block(self):
        # Partial: UTF-16 payload is 8 bytes but CountCharacter holds 8 (the
        # byte count) instead of 3 (the character count); the parser doubles
        # it again and reads across the StringData/ExtraData boundary.
        payload = "abc".encode("utf-16le") + b"\x00\x00"
        section = struct.pack("<H", len(payload)) + payload
        data = shell_header(FLAG_HAS_NAME | FLAG_IS_UNICODE) + section

        lnk, _ = parse_lnk(data)
        self.assertEqual(lnk.string_data.description(), "abc")
        self.assertNotEqual(lnk.size, len(data))
        self.assertGreater(lnk.size, len(data))

    def test_truncated_unicode_payload_recovers_only_declared_prefix(self):
        # Partial: CountCharacter declares 10 chars but only 4 UTF-16 bytes
        # follow and neither NUL nor a terminal block exists.
        section = struct.pack("<H", 10) + "ab".encode("utf-16le")
        data = shell_header(FLAG_HAS_NAME | FLAG_IS_UNICODE) + section

        lnk, _ = parse_lnk(data)
        self.assertEqual(lnk.string_data.description(), "ab")
        self.assertGreater(lnk.size, len(data))

    def test_truncated_ansi_payload_recovers_available_bytes(self):
        # Partial: CountCharacter declares 10 chars but only 3 bytes exist.
        section = struct.pack("<H", 10) + b"abc"
        data = shell_header(FLAG_HAS_NAME) + section

        lnk, _ = parse_lnk(data)
        self.assertEqual(lnk.string_data.description(), "abc")
        self.assertGreater(lnk.size, len(data))

    def test_missing_count_field_warns_and_skips_string_data(self):
        # Partial: HasName is set but zero StringData bytes are present.
        data = shell_header(FLAG_HAS_NAME)
        lnk, warning_messages = parse_lnk(data)
        self.assertEqual(lnk.string_data.as_dict(), {})
        self.assertEqual(lnk.size, HEADER_SIZE)
        self.assertTrue(any("Error while parsing String data" in msg for msg in warning_messages))

    def test_single_count_byte_warns_in_string_and_extra_sections(self):
        # Partial: only one byte of the two-byte CountCharacter is present;
        # the remaining byte then cannot start an ExtraData block either.
        data = shell_header(FLAG_HAS_NAME) + b"\x05"
        lnk, warning_messages = parse_lnk(data)
        self.assertEqual(lnk.size, len(data))
        self.assertTrue(any("Error while parsing String data" in msg for msg in warning_messages))
        self.assertTrue(any("Error while parsing extra data" in msg for msg in warning_messages))

    def test_second_string_overrun_desyncs_all_following_sections(self):
        # Partial: NAME_STRING is intact; RELATIVE_PATH claims 4 characters
        # but holds 2 plus NUL, so its declared window eats the terminal
        # block and the computed LNK size loses all relation to the stream.
        name = struct.pack("<H", 2) + "ab".encode("utf-16le") + b"\x00\x00"
        relative_path = struct.pack("<H", 4) + "cd".encode("utf-16le") + b"\x00\x00"
        data = (
            shell_header(FLAG_HAS_NAME | FLAG_HAS_RELATIVE_PATH | FLAG_IS_UNICODE)
            + name
            + relative_path
            + TERMINAL_BLOCK
        )

        lnk, _ = parse_lnk(data)
        self.assertEqual(lnk.string_data.description(), "ab")
        self.assertNotEqual(lnk.string_data.relative_path(), "cd")
        self.assertGreater(abs(lnk.size - len(data)), 1_000_000)

    def test_count_above_260_is_capped_for_limited_strings(self):
        # Partial (documented cap): relative paths clamp CountCharacter to
        # 260 while 300 bytes are present, so section bookkeeping drifts.
        section = struct.pack("<H", 300) + b"x" * 300
        data = shell_header(FLAG_HAS_RELATIVE_PATH) + section + TERMINAL_BLOCK

        lnk, _ = parse_lnk(data)
        self.assertEqual(len(lnk.string_data.relative_path()), 260)
        self.assertNotEqual(lnk.size, len(data))


class ExtraDataStructureTests(unittest.TestCase):
    def test_known_blocks_and_terminal_parse_completely(self):
        # Accepted: CodePage and SpecialFolder blocks followed by terminal.
        for block, expected_name in (
            (codepage_block(), "CONSOLE_CODEPAGE_BLOCK"),
            (special_folder_block(), "SPECIAL_FOLDER_LOCATION_BLOCK"),
        ):
            with self.subTest(block=expected_name):
                data = shell_header(0) + block + TERMINAL_BLOCK
                lnk, warning_messages = parse_lnk(data)
                self.assertEqual(warning_messages, [])
                self.assertEqual(lnk.size, len(data))
                self.assertEqual([extra.name() for extra in lnk.extras.data], [expected_name])

    def test_unknown_signature_with_valid_size_is_retained(self):
        # Accepted: unknown signatures are kept as UNKNOWN_BLOCK, not dropped.
        data = shell_header(0) + unknown_block() + TERMINAL_BLOCK
        lnk, warning_messages = parse_lnk(data)
        self.assertEqual(warning_messages, [])
        self.assertEqual(lnk.size, len(data))
        ok, payload = materialize_json(lnk)
        self.assertTrue(ok, payload)
        self.assertIn("UNKNOWN_BLOCK", payload["extra"])

    def test_truncated_known_block_warns_and_drops_its_payload(self):
        # Partial: BlockSize says 12 but only the 8-byte header is present.
        block = codepage_block()[:8]
        data = shell_header(0) + block
        self.assertLess(len(data), MAX_CORPUS_SIZE)

        lnk, _ = parse_lnk(data)
        self.assertEqual(lnk.extras.as_dict(), {})
        block_warnings = collect_extra_warnings(lnk)
        self.assertTrue(any("CONSOLE_CODEPAGE_BLOCK" in msg for msg in block_warnings))

    def test_block_size_beyond_eof_over_reports_section_size(self):
        # Partial: BlockSize 60 while 12 bytes exist; the declared size is
        # trusted blindly and the section claims bytes it never received.
        block = codepage_block(block_size=60)
        data = shell_header(0) + block
        lnk, warning_messages = parse_lnk(data)
        self.assertEqual(warning_messages, [])
        self.assertGreater(lnk.size, len(data))
        self.assertEqual(lnk.extras.size(), 60)

    def test_terminal_block_with_appended_data_is_tracked_and_toggleable(self):
        # Accepted with tracking: data after a <4 terminal is retained as
        # TERMINAL_BLOCK; disabling terminal support hides it again.
        appended = b"PAYLOAD12"
        data = shell_header(0) + TERMINAL_BLOCK + appended
        lnk, _ = parse_lnk(data, allow_terminal_blocks=True)
        self.assertEqual(lnk.size, len(data))
        (terminal,) = lnk.extras.data
        self.assertEqual(terminal.name(), "TERMINAL_BLOCK")
        self.assertEqual(terminal.appended_data(), appended)

        strict_lnk, _ = parse_lnk(data, allow_terminal_blocks=False)
        self.assertEqual(list(strict_lnk.extras), [])

    def test_terminal_size_below_four_raises_struct_error(self):
        # Rejected: a 3-byte terminal value cannot even hold the size field
        # once it is treated as a block, so construction fails outright.
        data = shell_header(0) + struct.pack("<I", 3) + b"abcdefgh"
        parse_lnk_expecting_struct_error(data)

    def test_shrunk_block_size_desyncs_neighbor_into_unknown_block(self):
        # Partial: CodePage BlockSize is rewritten to 8, so its CodePage
        # value (1252) becomes the next block header and is mislabelled
        # unknown with a size that runs far past the end of the stream.
        broken_codepage = codepage_block(block_size=8)
        neighbor = special_folder_block()
        data = shell_header(0) + broken_codepage + neighbor + TERMINAL_BLOCK

        lnk, _ = parse_lnk(data)
        names = [extra.name() for extra in lnk.extras.data]
        self.assertEqual(names, ["CONSOLE_CODEPAGE_BLOCK", "UNKNOWN_BLOCK"])
        self.assertEqual(lnk.extras.data[0].size(), 8)
        self.assertEqual(lnk.extras.data[1].size(), 1252)
        self.assertGreater(lnk.size, len(data))
        block_warnings = collect_extra_warnings(lnk)
        self.assertTrue(any("CONSOLE_CODEPAGE_BLOCK" in msg for msg in block_warnings))

    def test_trailing_bytes_smaller_than_block_header_warn(self):
        # Partial: three orphan bytes cannot hold an 8-byte block header.
        data = shell_header(0) + codepage_block() + b"abc"
        lnk, warning_messages = parse_lnk(data)
        self.assertEqual(lnk.size, len(data))
        self.assertTrue(any("Error while parsing extra data" in msg for msg in warning_messages))


class CrossSectionInvariantTests(unittest.TestCase):
    def test_every_accepted_sample_is_deterministic_across_parses(self):
        # A second parse of the same in-memory bytes must yield the same
        # materialized output; corrupted cases are excluded by construction.
        accepted = [
            shell_header(FLAG_HAS_LINK_INFO) + build_local_link_info() + TERMINAL_BLOCK,
            shell_header(FLAG_HAS_LINK_INFO) + build_network_link_info() + TERMINAL_BLOCK,
            shell_header(FLAG_HAS_LINK_INFO) + build_unicode_local_link_info() + TERMINAL_BLOCK,
            shell_header(0) + codepage_block() + TERMINAL_BLOCK,
            shell_header(0) + special_folder_block() + TERMINAL_BLOCK,
            shell_header(0) + unknown_block() + TERMINAL_BLOCK,
        ]
        for data in accepted:
            with self.subTest(length=len(data)):
                self.assertLess(len(data), MAX_CORPUS_SIZE)
                first, first_warnings = parse_lnk(data)
                second, second_warnings = parse_lnk(data)
                self.assertEqual(first_warnings, [])
                self.assertEqual(second_warnings, [])
                _, first_json = materialize_json(first)
                _, second_json = materialize_json(second)
                self.assertEqual(first_json, second_json)

    def test_independent_verifier_accepts_canonical_and_flags_all_mutants(self):
        # The verifier mirrors the structural responsibility without using
        # parser internals: clean layouts pass, every offset mutant fails.
        canonical_local = build_local_link_info()
        canonical_network = build_network_link_info()
        self.assertEqual(independent_link_info_violations(canonical_local), [])
        self.assertEqual(independent_link_info_violations(canonical_network), [])

        mutants = [
            canonical_local[:12] + struct.pack("<I", 9999) + canonical_local[16:],
            canonical_local[:24] + struct.pack("<I", 9999) + canonical_local[28:],
            canonical_local[:24]
            + struct.pack("<I", struct.unpack("<I", canonical_local[16:20])[0] + 2)
            + canonical_local[28:],
        ]
        for mutant in mutants:
            with self.subTest(mutant=mutant[:8].hex()):
                self.assertTrue(independent_link_info_violations(mutant))


if __name__ == "__main__":
    unittest.main()
