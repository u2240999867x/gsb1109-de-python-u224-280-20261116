"""Forensic analysis of MFT records that may contain torn sectors.

A collected ``$MFT`` can contain records with sector tears: the update sequence array (USA) check fails
for one or more sectors, meaning the on-disk sector is a mix of old and new data. This module analyzes
such records without hiding the damage:

- Both the original bytes and the USA-restored view are preserved. The restored view applies fixups
  only to sectors that passed USA verification; a readable restored view does **not** prove the
  original record was intact.
- Every sector that fails verification taints its full byte range. Any field, attribute header,
  attribute value or record reference whose bytes intersect a tainted range is reported without a
  value -- fields crossing a bad sector never get a "best guess" value, and no defaults are filled in
  to produce a complete-looking directory record.
- Attributes whose boundaries are independently provable (a clean ``RecordLength`` in a verified
  sector) remain readable even when a neighbouring attribute is damaged. When the boundary of an
  attribute cannot be proven, interpretation of everything after it stops explicitly.
- A tainted record reference must not be used to locate extension records, and a consistent extension
  record found through it can never retroactively prove the tainted field. References carry an
  explicit ``locatable`` flag to encode this.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

from dissect.ntfs.c_ntfs import ATTRIBUTE_TYPE_CODE, DEFAULT_RECORD_SIZE, SECTOR_SIZE

if TYPE_CHECKING:
    from collections.abc import Iterator

DISCLAIMER = (
    "The restored view applies update-sequence fixups only to sectors that passed USA verification; "
    "readability of the restored view does not prove integrity of the original record. "
    "Only fields whose bytes lie entirely within verified sectors are reported with a value."
)

# (name, offset, size) layouts, relative to the start of the record or attribute header.
RECORD_HEADER_LAYOUT = (
    ("Signature", 0, 4),
    ("UpdateSequenceArrayOffset", 4, 2),
    ("UpdateSequenceArraySize", 6, 2),
    ("Lsn", 8, 8),
    ("SequenceNumber", 16, 2),
    ("ReferenceCount", 18, 2),
    ("FirstAttributeOffset", 20, 2),
    ("Flags", 22, 2),
    ("BytesInUse", 24, 4),
    ("BytesAllocated", 28, 4),
    ("BaseFileRecordSegment", 32, 8),
    ("NextAttributeInstance", 40, 2),
)
ATTRIBUTE_HEADER_LAYOUT = (
    ("TypeCode", 0, 4),
    ("RecordLength", 4, 4),
    ("FormCode", 8, 1),
    ("NameLength", 9, 1),
    ("NameOffset", 10, 2),
    ("Flags", 12, 2),
    ("Instance", 14, 2),
)
RESIDENT_LAYOUT = (
    ("ValueLength", 16, 4),
    ("ValueOffset", 20, 2),
    ("ResidentFlags", 22, 1),
)
NONRESIDENT_LAYOUT = (
    ("LowestVcn", 16, 8),
    ("HighestVcn", 24, 8),
    ("MappingPairsOffset", 32, 2),
    ("CompressionUnit", 34, 1),
    ("AllocatedLength", 40, 8),
    ("FileSize", 48, 8),
    ("ValidDataLength", 56, 8),
    ("TotalAllocated", 64, 8),
)

RESIDENT_HEADER_SIZE = 24
NONRESIDENT_HEADER_SIZE = 72

END_OF_ATTRIBUTES = 0xFFFFFFFF


@dataclass
class FieldReport:
    """A single field of a record or attribute, with its in-record offset and trust verdict.

    ``value`` is ``None`` whenever the field's bytes intersect a tainted sector; no substitute
    or default value is ever fabricated.
    """

    name: str
    offset: int
    size: int
    value: int | str | None
    trustworthy: bool
    tainted_by: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "offset": self.offset,
            "size": self.size,
            "value": self.value,
            "trustworthy": self.trustworthy,
            "tainted_by": self.tainted_by,
        }


@dataclass
class ReferenceReport:
    """A file reference (MFT segment reference) found inside a record.

    A reference that is not ``trustworthy`` has ``segment``/``sequence`` set to ``None`` and
    ``locatable`` set to ``False``: it must not be used to locate extension records, and any
    record reached through it cannot retroactively prove the reference itself.
    """

    kind: str
    offset: int
    segment: int | None
    sequence: int | None
    trustworthy: bool
    tainted_by: list[int] = field(default_factory=list)

    @property
    def locatable(self) -> bool:
        """Return whether this reference may be used to locate another record."""
        return self.trustworthy and self.segment is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "offset": self.offset,
            "segment": self.segment,
            "sequence": self.sequence,
            "trustworthy": self.trustworthy,
            "tainted_by": self.tainted_by,
            "locatable": self.locatable,
        }


@dataclass
class SectorVerdict:
    """USA verification evidence for one sector of the record."""

    index: int
    offset: int
    size: int
    status: str  # "ok" | "torn" | "unverifiable"
    expected_usn: str | None
    actual_tail: str | None
    replacement: str | None
    fixup_applied: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "offset": self.offset,
            "size": self.size,
            "status": self.status,
            "expected_usn": self.expected_usn,
            "actual_tail": self.actual_tail,
            "replacement": self.replacement,
            "fixup_applied": self.fixup_applied,
        }


@dataclass
class AttributeReport:
    """One attribute (or unparsable attribute stub) of the record.

    Status values:
        - ``ok``: header and value bytes are entirely within verified sectors.
        - ``tainted_header``: part of the header is tainted, but ``RecordLength`` was provable.
        - ``tainted_value``: header is provable, (part of) the value is tainted.
        - ``boundary_unknown``: the attribute boundary cannot be proven; interpretation of this
          record stops here and nothing after this offset is interpreted.
    """

    index: int
    offset: int
    status: str
    type_code: int | None = None
    type_name: str | None = None
    name: str | None = None
    resident: bool | None = None
    header_range: tuple[int, int] | None = None
    value_range: tuple[int, int] | None = None
    fields: list[FieldReport] = field(default_factory=list)
    references: list[ReferenceReport] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "offset": self.offset,
            "status": self.status,
            "type_code": self.type_code,
            "type_name": self.type_name,
            "name": self.name,
            "resident": self.resident,
            "header_range": list(self.header_range) if self.header_range else None,
            "value_range": list(self.value_range) if self.value_range else None,
            "fields": [f.to_dict() for f in self.fields],
            "references": [r.to_dict() for r in self.references],
        }


@dataclass
class TornRecordReport:
    """Full forensic analysis result of a single MFT record.

    Attributes:
        segment: The MFT segment number of this record, if known.
        offset: The byte offset of this record within the source ``$MFT``, if known.
        raw: The original record bytes, exactly as collected.
        restored: The USA-restored view; fixups are applied only to verified sectors.
    """

    segment: int | None
    offset: int | None
    record_size: int
    sector_size: int
    raw: bytes
    restored: bytes
    usa: dict[str, Any]
    sectors: list[SectorVerdict]
    header_fields: list[FieldReport]
    references: list[ReferenceReport]
    attributes: list[AttributeReport]
    stop_reason: str
    disclaimer: str = DISCLAIMER

    @property
    def tainted_sectors(self) -> list[int]:
        """Return the indices of all sectors that failed USA verification."""
        return [sector.index for sector in self.sectors if sector.status != "ok"]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary of the analysis, excluding the bulk bytes."""
        return {
            "segment": self.segment,
            "offset": self.offset,
            "record_size": self.record_size,
            "sector_size": self.sector_size,
            "usa": self.usa,
            "sectors": [sector.to_dict() for sector in self.sectors],
            "tainted_sectors": self.tainted_sectors,
            "header_fields": [f.to_dict() for f in self.header_fields],
            "references": [r.to_dict() for r in self.references],
            "attributes": [a.to_dict() for a in self.attributes],
            "stop_reason": self.stop_reason,
            "disclaimer": self.disclaimer,
        }

    def write(self, directory: str | Path) -> None:
        """Write the raw bytes, the restored view and the JSON report to ``directory``.

        Args:
            directory: Output directory, created if it does not exist.
        """
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)

        if self.segment is not None:
            stem = f"record_{self.segment:06d}"
        elif self.offset is not None:
            stem = f"record_0x{self.offset:012x}"
        else:
            stem = "record"

        (directory / f"{stem}.raw.bin").write_bytes(self.raw)
        (directory / f"{stem}.restored.bin").write_bytes(self.restored)
        (directory / f"{stem}.json").write_text(json.dumps(self.to_dict(), indent=2) + "\n")


class _RecordContext:
    """Helper holding the record views and taint information while analyzing."""

    def __init__(self, restored: bytes, tainted: list[int], sector_size: int):
        self.restored = restored
        self.tainted = tainted
        self.sector_size = sector_size

    def tainted_by(self, offset: int, size: int) -> list[int]:
        """Return the indices of the tainted sectors intersecting ``[offset, offset + size)``."""
        end = offset + size
        return [
            index
            for index in self.tainted
            if offset < (index + 1) * self.sector_size and index * self.sector_size < end
        ]

    def read_field(self, name: str, offset: int, size: int) -> FieldReport:
        """Read an unsigned integer field from the restored view, if its bytes are provable."""
        tainted_by = self.tainted_by(offset, size)
        value = None
        if not tainted_by:
            value = int.from_bytes(self.restored[offset : offset + size], "little")
        return FieldReport(
            name=name, offset=offset, size=size, value=value, trustworthy=not tainted_by, tainted_by=tainted_by
        )


def _verify_sectors(raw: bytes, sector_size: int) -> tuple[bytes, dict[str, Any], list[SectorVerdict]]:
    """Verify each sector of ``raw`` against the USA and build the restored view.

    Fixups are applied only to sectors that pass verification; torn sectors keep their original
    bytes in the restored view. If the USA itself is invalid, every sector is unverifiable.
    """
    record_size = len(raw)
    sector_count = record_size // sector_size
    restored = bytearray(raw)

    usa_offset, usa_declared_size = struct.unpack_from("<HH", raw, 4)
    usa_valid = (
        usa_offset % 2 == 0
        and usa_offset + (sector_count + 1) * 2 <= sector_size
        and usa_offset + (usa_declared_size) * 2 <= record_size
        and usa_declared_size >= sector_count + 1
    )

    usa: dict[str, Any] = {
        "offset": usa_offset,
        "declared_size": usa_declared_size,
        "expected_size": sector_count + 1,
        "valid": usa_valid,
        "trustworthy": False,
    }

    verdicts: list[SectorVerdict] = []
    if not usa_valid:
        verdicts.extend(
            SectorVerdict(
                index=index,
                offset=index * sector_size,
                size=sector_size,
                status="unverifiable",
                expected_usn=None,
                actual_tail=None,
                replacement=None,
                fixup_applied=False,
            )
            for index in range(sector_count)
        )
        return raw, usa, verdicts

    usn = raw[usa_offset : usa_offset + 2]
    for index in range(sector_count):
        tail_offset = (index + 1) * sector_size - 2
        actual = raw[tail_offset : tail_offset + 2]
        replacement = raw[usa_offset + 2 + index * 2 : usa_offset + 4 + index * 2]
        ok = actual == usn
        if ok:
            restored[tail_offset : tail_offset + 2] = replacement
        verdicts.append(
            SectorVerdict(
                index=index,
                offset=index * sector_size,
                size=sector_size,
                status="ok" if ok else "torn",
                expected_usn=usn.hex(),
                actual_tail=actual.hex(),
                replacement=replacement.hex(),
                fixup_applied=ok,
            )
        )

    # The USA itself lives in the first sector; if that sector is torn, the USA entries used to
    # verify the other sectors are themselves suspect, so every "ok" verdict is conditional.
    usa["trustworthy"] = verdicts[0].status == "ok" if verdicts else False

    return bytes(restored), usa, verdicts


def _parse_file_name_value(ctx: _RecordContext, attribute: AttributeReport, value_start: int, value_end: int) -> None:
    """Extract the parent reference and file name from a provable ``$FILE_NAME`` value."""
    parent = ctx.read_field("ParentDirectory", value_start, 8)
    attribute.references.append(
        ReferenceReport(
            kind="file_name_parent",
            offset=parent.offset,
            segment=(parent.value & 0xFFFFFFFFFFFF) if parent.trustworthy else None,
            sequence=(parent.value >> 48) if parent.trustworthy else None,
            trustworthy=parent.trustworthy,
            tainted_by=parent.tainted_by,
        )
    )

    name_length = ctx.read_field("FileNameLength", value_start + 64, 1)
    attribute.fields.append(name_length)
    if name_length.trustworthy and value_start + 66 + name_length.value * 2 <= value_end:
        name_field = ctx.read_field("FileName", value_start + 66, name_length.value * 2)
        if name_field.trustworthy:
            try:
                name_field.value = ctx.restored[name_field.offset : name_field.offset + name_field.size].decode(
                    "utf-16-le"
                )
            except UnicodeDecodeError:
                name_field.value = None
                name_field.trustworthy = False
        attribute.fields.append(name_field)


def _parse_attribute_list_value(
    ctx: _RecordContext, attribute: AttributeReport, value_start: int, value_end: int
) -> None:
    """Extract segment references from a provable ``$ATTRIBUTE_LIST`` value.

    Entry walking stops at the first entry whose ``RecordLength`` cannot be proven; entries after
    it are not interpreted.
    """
    entry_offset = value_start
    entry_index = 0
    while entry_offset + 26 <= value_end:
        length_field = ctx.read_field(f"AttributeListEntry[{entry_index}].RecordLength", entry_offset + 4, 2)
        attribute.fields.append(length_field)
        if not length_field.trustworthy or length_field.value == 0:
            break

        reference_field = ctx.read_field(f"AttributeListEntry[{entry_index}].SegmentReference", entry_offset + 16, 8)
        attribute.references.append(
            ReferenceReport(
                kind="attribute_list_segment",
                offset=reference_field.offset,
                segment=(reference_field.value & 0xFFFFFFFFFFFF) if reference_field.trustworthy else None,
                sequence=(reference_field.value >> 48) if reference_field.trustworthy else None,
                trustworthy=reference_field.trustworthy,
                tainted_by=reference_field.tainted_by,
            )
        )

        entry_offset += length_field.value
        entry_index += 1


def _parse_attribute(
    ctx: _RecordContext, index: int, offset: int, record_size: int
) -> tuple[AttributeReport, str | None]:
    """Parse one attribute at ``offset``.

    Returns the attribute report and, if the attribute walk cannot continue, the reason why.
    """
    type_field = ctx.read_field("TypeCode", offset, 4)
    if not type_field.trustworthy:
        stub = AttributeReport(index=index, offset=offset, status="boundary_unknown", fields=[type_field])
        return stub, "attribute_type_unknown"

    type_code = type_field.value
    length_field = ctx.read_field("RecordLength", offset + 4, 4)
    if not length_field.trustworthy:
        return (
            AttributeReport(
                index=index,
                offset=offset,
                status="boundary_unknown",
                type_code=type_code,
                type_name=ATTRIBUTE_TYPE_CODE(type_code).name,
                fields=[type_field, length_field],
            ),
            "record_length_unknown",
        )

    record_length = length_field.value
    if record_length == 0 or record_length % 8 or offset + record_length > record_size:
        return (
            AttributeReport(
                index=index,
                offset=offset,
                status="boundary_unknown",
                type_code=type_code,
                type_name=ATTRIBUTE_TYPE_CODE(type_code).name,
                fields=[type_field, length_field],
            ),
            "invalid_record_length",
        )

    attribute = AttributeReport(
        index=index,
        offset=offset,
        status="ok",
        type_code=type_code,
        type_name=ATTRIBUTE_TYPE_CODE(type_code).name,
        fields=[type_field, length_field],
    )

    fields = [ctx.read_field(name, offset + rel, size) for name, rel, size in ATTRIBUTE_HEADER_LAYOUT[2:]]
    attribute.fields.extend(fields)

    form_code = fields[0]  # FormCode
    if not form_code.trustworthy:
        # Without a provable FormCode we cannot know the header layout, but RecordLength still
        # proves the boundary, so the walk can continue.
        attribute.status = "tainted_header"
        return attribute, None

    attribute.resident = form_code.value == 0
    layout = RESIDENT_LAYOUT if attribute.resident else NONRESIDENT_LAYOUT
    header_size = RESIDENT_HEADER_SIZE if attribute.resident else NONRESIDENT_HEADER_SIZE
    attribute.header_range = (offset, min(offset + header_size, offset + record_length))

    for name, rel, size in layout:
        if rel + size <= record_length and offset + rel + size <= record_size:
            attribute.fields.append(ctx.read_field(name, offset + rel, size))

    field_values = {f.name: f for f in attribute.fields}

    name_length = field_values["NameLength"]
    name_offset = field_values["NameOffset"]
    if name_length.trustworthy and name_offset.trustworthy:
        if name_length.value:
            name_field = ctx.read_field("Name", offset + name_offset.value, name_length.value * 2)
            if name_field.trustworthy:
                try:
                    attribute.name = ctx.restored[name_field.offset : name_field.offset + name_field.size].decode(
                        "utf-16-le"
                    )
                except UnicodeDecodeError:
                    attribute.name = None
            attribute.fields.append(name_field)
        else:
            attribute.name = ""

    value_start = value_end = None
    if attribute.resident:
        value_length = field_values["ValueLength"]
        value_offset = field_values["ValueOffset"]
        if value_length.trustworthy and value_offset.trustworthy:
            value_start = offset + value_offset.value
            value_end = value_start + value_length.value
            if value_end > offset + record_length or value_end > record_size:
                value_start = value_end = None
    else:
        mapping_pairs_offset = field_values["MappingPairsOffset"]
        if mapping_pairs_offset.trustworthy:
            value_start = offset + mapping_pairs_offset.value
            value_end = offset + record_length
            if value_start > value_end:
                value_start = value_end = None

    if value_start is not None:
        attribute.value_range = (value_start, value_end)

    if any(not f.trustworthy for f in attribute.fields):
        attribute.status = "tainted_header"
    elif attribute.value_range:
        value_start, value_end = attribute.value_range
        if ctx.tainted_by(value_start, value_end - value_start):
            attribute.status = "tainted_value"

    # Extract references and provable values from well-known resident attribute types.
    if attribute.resident and value_start is not None:
        if type_code == ATTRIBUTE_TYPE_CODE.FILE_NAME:
            _parse_file_name_value(ctx, attribute, value_start, value_end)
        elif type_code == ATTRIBUTE_TYPE_CODE.ATTRIBUTE_LIST:
            _parse_attribute_list_value(ctx, attribute, value_start, value_end)

    return attribute, None


def analyze_record(
    data: bytes,
    segment: int | None = None,
    offset: int | None = None,
    sector_size: int = SECTOR_SIZE,
) -> TornRecordReport:
    """Analyze a single MFT record for torn sectors and export only provable fields.

    Args:
        data: The raw record bytes, exactly as collected.
        segment: Optional MFT segment number of this record, for traceability.
        offset: Optional byte offset of this record within the source ``$MFT``.
        sector_size: The sector size of the source volume.

    Returns:
        A :class:`TornRecordReport` with the original bytes, the USA-restored view, per-sector
        verification evidence, and every field, attribute and reference annotated with the
        sectors that taint it.
    """
    raw = bytes(data)
    record_size = len(raw)
    if record_size < 48 or record_size % sector_size:
        raise ValueError(f"Invalid record size: {record_size}")

    restored, usa, verdicts = _verify_sectors(raw, sector_size)
    tainted = [verdict.index for verdict in verdicts if verdict.status != "ok"]
    ctx = _RecordContext(restored, tainted, sector_size)

    header_fields = [ctx.read_field(name, rel, size) for name, rel, size in RECORD_HEADER_LAYOUT]
    field_values = {f.name: f for f in header_fields}

    signature = field_values["Signature"]
    if signature.trustworthy:
        try:
            signature.value = restored[0:4].decode("ascii")
        except UnicodeDecodeError:
            signature.value = restored[0:4].hex()

    references = []
    base_reference = field_values["BaseFileRecordSegment"]
    references.append(
        ReferenceReport(
            kind="base_file_record_segment",
            offset=base_reference.offset,
            segment=(base_reference.value & 0xFFFFFFFFFFFF) if base_reference.trustworthy else None,
            sequence=(base_reference.value >> 48) if base_reference.trustworthy else None,
            trustworthy=base_reference.trustworthy,
            tainted_by=base_reference.tainted_by,
        )
    )

    attributes: list[AttributeReport] = []
    stop_reason = "end_of_record"

    first_attribute = field_values["FirstAttributeOffset"]
    if not first_attribute.trustworthy:
        stop_reason = "first_attribute_offset_unknown"
    else:
        attribute_offset = first_attribute.value
        index = 0
        while attribute_offset + 8 <= record_size:
            type_code = int.from_bytes(restored[attribute_offset : attribute_offset + 4], "little")
            if not ctx.tainted_by(attribute_offset, 4) and type_code == END_OF_ATTRIBUTES:
                stop_reason = "end_marker"
                break

            attribute, reason = _parse_attribute(ctx, index, attribute_offset, record_size)
            attributes.append(attribute)
            if reason is not None:
                stop_reason = reason
                break

            record_length = attribute.fields[1].value
            attribute_offset += record_length
            index += 1

    return TornRecordReport(
        segment=segment,
        offset=offset,
        record_size=record_size,
        sector_size=sector_size,
        raw=raw,
        restored=restored,
        usa=usa,
        sectors=verdicts,
        header_fields=header_fields,
        references=references,
        attributes=attributes,
        stop_reason=stop_reason,
    )


def iter_records(fh: BinaryIO, record_size: int = DEFAULT_RECORD_SIZE) -> Iterator[tuple[int, int, bytes]]:
    """Yield ``(segment, offset, data)`` for every record in an opened ``$MFT`` file."""
    segment = 0
    while True:
        offset = segment * record_size
        fh.seek(offset)
        data = fh.read(record_size)
        if len(data) < record_size:
            break
        yield segment, offset, data
        segment += 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dissect.ntfs.torn",
        description="Analyze MFT records for torn sectors and export only provable fields.",
    )
    parser.add_argument("mft", type=Path, help="path to a collected $MFT file")
    parser.add_argument(
        "--record-size", type=int, default=DEFAULT_RECORD_SIZE, help="MFT record size (default: %(default)s)"
    )
    parser.add_argument("--sector-size", type=int, default=SECTOR_SIZE, help="sector size (default: %(default)s)")
    parser.add_argument("--segment", type=int, action="append", default=None, help="segment to analyze (repeatable)")
    parser.add_argument("--all", action="store_true", help="analyze all records in the file")
    parser.add_argument(
        "--out", type=Path, required=True, help="output directory for raw/restored bytes and JSON reports"
    )
    args = parser.parse_args(argv)

    if not args.all and not args.segment:
        parser.error("specify --segment and/or --all")

    with args.mft.open("rb") as fh:
        if args.all:
            segments = iter_records(fh, args.record_size)
        else:
            segments = []
            for segment in sorted(set(args.segment)):
                offset = segment * args.record_size
                fh.seek(offset)
                data = fh.read(args.record_size)
                if len(data) < args.record_size:
                    print(f"segment {segment}: short read, skipped", file=sys.stderr)
                    continue
                segments.append((segment, offset, data))

        for segment, offset, data in segments:
            report = analyze_record(data, segment=segment, offset=offset, sector_size=args.sector_size)
            report.write(args.out)
            print(
                f"segment {segment}: torn={report.tainted_sectors or '-'} "
                f"attributes={len(report.attributes)} stop={report.stop_reason}"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
