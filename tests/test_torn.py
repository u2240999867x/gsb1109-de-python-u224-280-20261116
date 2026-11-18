from __future__ import annotations

import json
import struct
from typing import TYPE_CHECKING

from dissect.ntfs.torn import analyze_record, main
from dissect.ntfs.util import apply_fixup

if TYPE_CHECKING:
    from pathlib import Path

    from dissect.ntfs.torn import FieldReport

# Single MFT record of the $MFT file itself, identical to the one in test_mft.py.
MFT_RECORD_HEX = (
    "46494c453000030051511000000000000100010038000100a001000000040000"
    "0000000000000000070000000000000002000000000000001000000060000000"
    "00001800000000004800000018000000d2145d665666d801d2145d665666d801"
    "d2145d665666d801d2145d665666d80106000000000000000000000000000000"
    "0000000000010000000000000000000000000000000000003000000068000000"
    "00001800000003004a000000180001000500000000000500d2145d665666d801"
    "d2145d665666d801d2145d665666d801d2145d665666d8010040000000000000"
    "00400000000000000600000000000000040324004d0046005400000000000000"
    "8000000048000000010040000000060000000000000000003f00000000000000"
    "4000000000000000000004000000000000000400000000000000040000000000"
    "2140550200000000b00000005000000001004000000005000000000000000000"
    "0100000000000000400000000000000000200000000000000810000000000000"
    "0810000000000000210154022101d1fd0000000000000000ffffffff00000000"
    "00000400000000002140550200000000b0000000500000000100400000000500"
    "0000000000000000010000000000000040000000000000000020000000000000"
    "08100000000000000810000000000000210154022101d1fd0000000000000200"
    "ffffffff00000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000200"
)

USN = b"\x11\x22"
REPLACEMENT_SECTOR_0 = b"\x34\x12"
REPLACEMENT_SECTOR_1 = b"\x78\x56"


def make_record(attr2_offset: int = 0x1E8) -> bytes:
    """Build a synthetic 1024-byte MFT record with three attributes.

    Attribute 2 is a resident ``$FILE_NAME`` whose value starts exactly at the sector boundary,
    so tearing the second sector taints its value but not its header.
    """
    buf = bytearray(1024)
    buf[0:4] = b"FILE"
    struct.pack_into("<HH", buf, 4, 0x30, 3)  # USA offset and size
    struct.pack_into("<H", buf, 16, 1)  # SequenceNumber
    struct.pack_into("<H", buf, 20, 0x38)  # FirstAttributeOffset
    struct.pack_into("<H", buf, 22, 1)  # Flags: in use
    struct.pack_into("<I", buf, 24, 0x400)  # BytesInUse
    struct.pack_into("<I", buf, 28, 0x400)  # BytesAllocated

    # Attribute 1: $STANDARD_INFORMATION, resident, entirely in sector 0
    struct.pack_into("<I", buf, 0x38, 0x10)
    struct.pack_into("<I", buf, 0x3C, attr2_offset - 0x38)  # RecordLength
    struct.pack_into("<I", buf, 0x38 + 16, 0x48)  # ValueLength
    struct.pack_into("<H", buf, 0x38 + 20, 0x18)  # ValueOffset

    # Attribute 2: $FILE_NAME, resident, value spans into sector 1
    struct.pack_into("<I", buf, attr2_offset, 0x30)
    struct.pack_into("<I", buf, attr2_offset + 4, 0x118)  # RecordLength
    struct.pack_into("<I", buf, attr2_offset + 16, 0x100)  # ValueLength
    struct.pack_into("<H", buf, attr2_offset + 20, 0x18)  # ValueOffset
    value = attr2_offset + 0x18
    struct.pack_into("<IHH", buf, value, 0x1122, 0, 7)  # ParentDirectory: segment 0x1122, sequence 7
    buf[value + 64] = 2  # FileNameLength
    buf[value + 66 : value + 70] = "Ab".encode("utf-16-le")

    # Attribute 3: $DATA, non-resident, header in sector 1
    attr3_offset = attr2_offset + 0x118
    struct.pack_into("<I", buf, attr3_offset, 0x80)
    struct.pack_into("<I", buf, attr3_offset + 4, 0x48)  # RecordLength
    buf[attr3_offset + 8] = 1  # FormCode: non-resident
    struct.pack_into("<H", buf, attr3_offset + 32, 0x40)  # MappingPairsOffset

    struct.pack_into("<I", buf, attr3_offset + 0x48, 0xFFFFFFFF)  # END marker

    buf[0x30:0x32] = USN
    buf[0x32:0x34] = REPLACEMENT_SECTOR_0
    buf[0x34:0x36] = REPLACEMENT_SECTOR_1
    buf[510:512] = USN
    buf[1022:1024] = USN
    return bytes(buf)


def tear_sector(data: bytes, sector: int, sector_size: int = 512) -> bytes:
    """Return a copy of ``data`` with the tail of ``sector`` corrupted, simulating a torn write."""
    buf = bytearray(data)
    tail = (sector + 1) * sector_size - 2
    buf[tail : tail + 2] = b"\xde\xad"
    return bytes(buf)


def field_by_name(fields: list[FieldReport], name: str) -> FieldReport:
    return next(field for field in fields if field.name == name)


def test_analyze_clean_mft_record() -> None:
    data = bytes.fromhex(MFT_RECORD_HEX)
    report = analyze_record(data, segment=0, offset=0)

    assert [sector.status for sector in report.sectors] == ["ok", "ok"]
    assert report.tainted_sectors == []
    assert report.usa["trustworthy"] is True
    assert report.restored == apply_fixup(data)
    assert report.raw == data
    assert report.stop_reason == "end_marker"

    assert field_by_name(report.header_fields, "Signature").value == "FILE"
    assert field_by_name(report.header_fields, "SequenceNumber").value == 1
    assert field_by_name(report.header_fields, "FirstAttributeOffset").value == 0x38

    assert len(report.attributes) == 4
    assert [attribute.type_name for attribute in report.attributes] == [
        "STANDARD_INFORMATION",
        "FILE_NAME",
        "DATA",
        "BITMAP",
    ]
    assert all(attribute.status == "ok" for attribute in report.attributes)

    file_name = report.attributes[1]
    assert file_name.name == ""
    assert field_by_name(file_name.fields, "FileName").value == "$MFT"

    parent = file_name.references[0]
    assert parent.kind == "file_name_parent"
    assert parent.segment == 5
    assert parent.sequence == 5
    assert parent.trustworthy is True
    assert parent.locatable is True


def test_torn_second_sector_keeps_provable_attributes() -> None:
    data = tear_sector(bytes.fromhex(MFT_RECORD_HEX), 1)
    report = analyze_record(data, segment=0, offset=0)

    assert [sector.status for sector in report.sectors] == ["ok", "torn"]
    assert report.tainted_sectors == [1]
    assert report.usa["trustworthy"] is True

    # The original bytes are preserved and the torn sector is not "repaired" in the restored view
    assert report.raw == data
    assert report.restored[1022:1024] == b"\xde\xad"
    assert report.restored[:510] == apply_fixup(bytes.fromhex(MFT_RECORD_HEX))[:510]

    # All attributes of this record live in sector 0, so they remain fully provable
    assert report.stop_reason == "end_marker"
    assert len(report.attributes) == 4
    assert all(attribute.status == "ok" for attribute in report.attributes)


def test_torn_first_sector_hides_record_header() -> None:
    data = tear_sector(make_record(), 0)
    report = analyze_record(data)

    assert [sector.status for sector in report.sectors] == ["torn", "ok"]
    assert report.tainted_sectors == [0]
    # The USA lives in sector 0, so the "ok" verdict of sector 1 is conditional
    assert report.usa["trustworthy"] is False

    # The torn sector is left untouched in the restored view, the verified sector is fixed up
    assert report.restored[510:512] == b"\xde\xad"
    assert report.restored[1022:1024] == REPLACEMENT_SECTOR_1

    # No header field can be proven, so no attribute interpretation is even attempted
    assert field_by_name(report.header_fields, "FirstAttributeOffset").value is None
    assert field_by_name(report.header_fields, "FirstAttributeOffset").tainted_by == [0]
    assert report.attributes == []
    assert report.stop_reason == "first_attribute_offset_unknown"

    base_reference = report.references[0]
    assert base_reference.kind == "base_file_record_segment"
    assert base_reference.segment is None
    assert base_reference.locatable is False


def test_torn_sector_taints_spanning_value_but_not_boundaries() -> None:
    data = tear_sector(make_record(), 1)
    report = analyze_record(data)

    assert report.tainted_sectors == [1]
    assert len(report.attributes) == 3

    # Attribute 1 is entirely in a verified sector and remains fully readable
    attr1, attr2, attr3 = report.attributes
    assert attr1.type_name == "STANDARD_INFORMATION"
    assert attr1.status == "ok"
    assert attr1.value_range == (0x50, 0x98)

    # Attribute 2 has a provable header and boundary, but its value crosses the torn sector
    assert attr2.type_name == "FILE_NAME"
    assert attr2.status == "tainted_value"
    assert attr2.header_range == (0x1E8, 0x200)
    assert attr2.value_range == (0x200, 0x300)
    assert field_by_name(attr2.fields, "RecordLength").trustworthy is True
    assert field_by_name(attr2.fields, "ValueLength").trustworthy is True

    # Fields inside the tainted value get no value, not even a partial or default one
    name_length = field_by_name(attr2.fields, "FileNameLength")
    assert name_length.value is None
    assert name_length.trustworthy is False
    assert all(field.name != "FileName" for field in attr2.fields)
    parent = attr2.references[0]
    assert parent.kind == "file_name_parent"
    assert parent.segment is None
    assert parent.sequence is None
    assert parent.trustworthy is False
    assert parent.locatable is False
    assert parent.tainted_by == [1]

    # Attribute 3 starts inside the torn sector: its boundary cannot be proven and
    # interpretation stops explicitly instead of guessing a type or length
    assert attr3.status == "boundary_unknown"
    assert attr3.type_code is None
    assert report.stop_reason == "attribute_type_unknown"


def test_invalid_record_length_stops_walk() -> None:
    # A readable but implausible RecordLength is corruption evidence, not a boundary to follow
    buf = bytearray(make_record())
    struct.pack_into("<I", buf, 0x1E8 + 4, 0)  # RecordLength of attribute 2
    report = analyze_record(bytes(buf))

    assert report.tainted_sectors == []
    assert len(report.attributes) == 2
    attr1, attr2 = report.attributes
    assert attr1.status == "ok"

    # The boundary cannot be proven, so nothing after it is interpreted
    assert attr2.status == "boundary_unknown"
    assert attr2.type_code == 0x30
    assert attr2.type_name == "FILE_NAME"
    assert report.stop_reason == "invalid_record_length"


def test_clean_synthetic_record() -> None:
    report = analyze_record(make_record())

    assert report.tainted_sectors == []
    assert report.stop_reason == "end_marker"
    assert len(report.attributes) == 3
    assert all(attribute.status == "ok" for attribute in report.attributes)

    attr2 = report.attributes[1]
    assert field_by_name(attr2.fields, "FileName").value == "Ab"
    parent = attr2.references[0]
    assert (parent.segment, parent.sequence) == (0x1122, 7)
    assert parent.locatable is True

    attr3 = report.attributes[2]
    assert attr3.type_name == "DATA"
    assert attr3.resident is False
    assert attr3.value_range == (0x340, 0x348)

    # Verified sectors have their fixups applied in the restored view
    assert report.restored[510:512] == REPLACEMENT_SECTOR_0
    assert report.restored[1022:1024] == REPLACEMENT_SECTOR_1


def test_unverifiable_usa_taints_everything() -> None:
    buf = bytearray(make_record())
    struct.pack_into("<H", buf, 4, 0x31)  # USA offset must be even
    report = analyze_record(bytes(buf))

    assert [sector.status for sector in report.sectors] == ["unverifiable", "unverifiable"]
    assert report.usa["valid"] is False
    assert report.restored == bytes(buf)
    assert report.attributes == []
    assert report.stop_reason == "first_attribute_offset_unknown"


def test_report_write_preserves_evidence(tmp_path: Path) -> None:
    data = tear_sector(make_record(), 1)
    report = analyze_record(data, segment=7, offset=7 * 1024)
    report.write(tmp_path)

    assert (tmp_path / "record_000007.raw.bin").read_bytes() == data
    assert (tmp_path / "record_000007.restored.bin").read_bytes() == report.restored

    exported = json.loads((tmp_path / "record_000007.json").read_text())
    assert exported["segment"] == 7
    assert exported["offset"] == 7 * 1024
    assert exported["tainted_sectors"] == [1]
    assert exported["sectors"][1]["status"] == "torn"
    assert exported["sectors"][1]["actual_tail"] == "dead"
    assert exported["sectors"][1]["fixup_applied"] is False
    assert exported["stop_reason"] == "attribute_type_unknown"
    assert exported["attributes"][1]["references"][0]["locatable"] is False
    assert "does not prove integrity" in exported["disclaimer"]


def test_main_exports_segments(tmp_path: Path, capsys) -> None:  # noqa: ANN001
    mft = tmp_path / "mft.bin"
    mft.write_bytes(make_record() + tear_sector(make_record(), 1))
    out = tmp_path / "out"

    assert main([str(mft), "--all", "--out", str(out)]) == 0
    assert (out / "record_000000.json").exists()
    assert (out / "record_000001.raw.bin").exists()

    exported = json.loads((out / "record_000001.json").read_text())
    assert exported["tainted_sectors"] == [1]

    out2 = tmp_path / "out2"
    assert main([str(mft), "--segment", "1", "--out", str(out2)]) == 0
    assert (out2 / "record_000001.json").exists()
    assert not (out2 / "record_000000.json").exists()
