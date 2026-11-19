"""
Recovery of interrupted MCAP writes.

When a write is interrupted (crash, power loss, killed process), the file on
disk usually ends with an incomplete tail: a half-written chunk, a torn record,
or a data section whose DataEnd, summary and Footer records were never (or only
partially) written. This module completes such a file into a valid MCAP archive
using the confirmed write receipt as the ledger of what may be published.

The approach:

* The damaged file is parsed strictly structurally, record by record, from the
  leading magic. The longest prefix of complete, valid records is retained.
  Record headers are never searched for inside record payloads to resynchronize
  parsing: the first record that does not parse ends the retained prefix.
* Unfinished chunks, DataEnd, the summary section and the Footer are each
  checked on their own; the evidence for every discarded byte is kept in the
  returned report.
* Messages in the retained prefix must match the confirmed receipt exactly, in
  order. Messages may carry out-of-order (backwards) log times; indexes and
  statistics are rebuilt from the actual file order and are never sorted to
  make reconstruction easier.
* Definitions (schemas and channels) and messages that were verified in the
  retained prefix are not written again. The old summary is never reused as
  the index of the new file: every index is rebuilt from validated content.
* The completed file is only published (moved to the output path) after both a
  full sequential read and random topic/time queries against it match the
  receipt. The damaged original is always left untouched.
"""

import os
import random
import struct
import tempfile
import zlib
from collections import defaultdict
from dataclasses import dataclass, field
from io import BytesIO
from typing import BinaryIO, Dict, List, Optional, Sequence, Tuple, Union

from .data_stream import ReadDataStream
from .exceptions import McapError, UnsupportedCompressionError
from .opcode import Opcode
from .reader import make_reader
from .records import (
    MESSAGE_FIELDS,
    MESSAGE_FIELDS_SIZE,
    Attachment,
    AttachmentIndex,
    Channel,
    Chunk,
    ChunkIndex,
    DataEnd,
    Footer,
    Header,
    Message,
    MessageIndex,
    Metadata,
    MetadataIndex,
    Schema,
    Statistics,
)
from .stream_reader import (
    MAGIC_SIZE,
    StreamReader,
    _get_chunk_data,
    _parse_chunk_records,
)
from .writer import MCAP0_MAGIC, CompressionType, IndexType, Writer

_RECORD_PREFIX = struct.Struct("<BQ")
_RECORD_PREFIX_SIZE = _RECORD_PREFIX.size
_RECORD_SIZE_LIMIT = 4 * 2**30
_EVIDENCE_SAMPLE_SIZE = 32
_COPY_BUFFER_SIZE = 1024 * 1024
_SUMMARY_OPCODES = (
    Opcode.SCHEMA,
    Opcode.CHANNEL,
    Opcode.STATISTICS,
    Opcode.CHUNK_INDEX,
    Opcode.ATTACHMENT_INDEX,
    Opcode.METADATA_INDEX,
    Opcode.SUMMARY_OFFSET,
)


class RecoveryError(McapError):
    """Raised when an interrupted MCAP file cannot be safely recovered."""


@dataclass
class ConfirmedMessage:
    """One entry of a write receipt: a message the interrupted writer confirmed.

    The completed file contains exactly the receipt's messages, in receipt
    order. Log times may go backwards between consecutive entries; entries are
    never reordered.
    """

    channel_id: int
    log_time: int
    publish_time: int
    sequence: int
    data: bytes

    def key(self) -> Tuple[int, int, int, int, bytes]:
        return _message_key(
            self.channel_id, self.log_time, self.publish_time, self.sequence, self.data
        )


@dataclass
class DiscardedRegion:
    """Evidence for a region of the damaged file that was not retained.

    :param offset: Byte offset of the region in the damaged file.
    :param length: Length of the region in bytes.
    :param reason: Why the region was not retained, including the validation
        verdict for DataEnd, summary and Footer regions.
    :param sample: The first bytes of the region, for inspection.
    """

    offset: int
    length: int
    reason: str
    sample: bytes


@dataclass
class RecoveryReport:
    """Describes how an interrupted file was completed.

    :param source: Path of the damaged original (left untouched).
    :param output: Path the completed file was published to.
    :param kept_offset: Number of bytes of the original file retained as the
        safe physical prefix.
    :param discarded: Evidence for every region that was not retained.
    :param messages_kept: Confirmed messages found in the retained prefix.
    :param messages_appended: Receipt messages appended during recovery.
    :param message_count: Messages in the completed file; equals the receipt
        length.
    :param random_queries: Random topic/time queries verified before publish.
    """

    source: str
    output: str
    kept_offset: int
    discarded: List[DiscardedRegion]
    messages_kept: int
    messages_appended: int
    message_count: int
    random_queries: int


def _message_key(
    channel_id: int, log_time: int, publish_time: int, sequence: int, data: bytes
) -> Tuple[int, int, int, int, bytes]:
    return (channel_id, log_time, publish_time, sequence, bytes(data))


def _opcode_name(opcode: int) -> str:
    try:
        return Opcode(opcode).name
    except ValueError:
        return f"unknown (opcode {opcode})"


def _sample(stream: BinaryIO, offset: int) -> bytes:
    stream.seek(offset)
    return stream.read(_EVIDENCE_SAMPLE_SIZE)


class _Truncated(Exception):
    """The record at the current offset does not fit in the remaining bytes."""


class _Invalid(Exception):
    """The record at the current offset is complete but not valid."""


@dataclass
class _Scan:
    header: Header
    kept_offset: int
    kept_crc: int
    data_crcs_enabled: bool
    schemas: Dict[int, Schema]
    channels: Dict[int, Channel]
    captured_schemas: Dict[int, Schema]
    captured_channels: Dict[int, Channel]
    chunk_indices: List[ChunkIndex]
    attachment_indexes: List[AttachmentIndex]
    metadata_indexes: List[MetadataIndex]
    statistics: Statistics
    messages_kept: int
    unchunked_messages: bool
    discarded: List[DiscardedRegion] = field(default_factory=list)


def _message_entries(data: bytes) -> Dict[int, List[Tuple[int, int]]]:
    """(log time, offset) of every message record in an uncompressed chunk."""
    entries: Dict[int, List[Tuple[int, int]]] = {}
    pos = 0
    end = len(data)
    while pos < end:
        opcode, length = _RECORD_PREFIX.unpack_from(data, pos)
        if opcode == Opcode.MESSAGE:
            channel_id, _, log_time, _ = MESSAGE_FIELDS.unpack_from(
                data, pos + _RECORD_PREFIX_SIZE
            )
            entries.setdefault(channel_id, []).append((log_time, pos))
        pos += _RECORD_PREFIX_SIZE + length
    return entries


def _check_footer(
    stream: BinaryIO,
    summary_start: int,
    footer_start: int,
    body: bytes,
    discarded: List[DiscardedRegion],
) -> bool:
    """Checks a footer record, recording the verdict as evidence. Returns True
    when the footer and the summary it covers are valid."""
    record_length = _RECORD_PREFIX_SIZE + len(body)
    try:
        footer = Footer.read(ReadDataStream(BytesIO(body)))
    except Exception as exc:
        discarded.append(
            DiscardedRegion(
                footer_start,
                record_length,
                f"invalid footer record: {exc}",
                _sample(stream, footer_start),
            )
        )
        return False
    notes = []
    summary_length = footer_start - summary_start
    expected_start = 0 if summary_length == 0 else summary_start
    if footer.summary_start != expected_start:
        notes.append(
            f"summary start {footer.summary_start} does not follow the data "
            f"section at {expected_start}"
        )
    if footer.summary_crc != 0:
        stream.seek(summary_start)
        crc = zlib.crc32(stream.read(summary_length))
        crc = zlib.crc32(
            struct.pack(
                "<BQQQ",  # cspell:disable-line
                Opcode.FOOTER,
                8 + 8 + 4,
                expected_start,
                footer.summary_offset_start,
            ),
            crc,
        )
        if crc != footer.summary_crc:
            notes.append("summary CRC mismatch")
    verdict = "valid" if not notes else "; ".join(notes)
    discarded.append(
        DiscardedRegion(
            footer_start,
            record_length,
            f"footer record ({verdict}); rewritten with the rebuilt summary",
            _sample(stream, footer_start),
        )
    )
    return not notes


def _scan_summary(
    stream: BinaryIO,
    summary_start: int,
    size: int,
    discarded: List[DiscardedRegion],
) -> Tuple[bool, Dict[int, Schema], Dict[int, Channel]]:
    """Checks the summary section and footer of an interrupted file, recording
    evidence for each part. Returns whether the summary and footer validated,
    plus the definitions found in the summary; those are only trustworthy when
    the summary validated."""
    schemas: Dict[int, Schema] = {}
    channels: Dict[int, Channel] = {}
    pos = summary_start
    while pos < size:
        stream.seek(pos)
        prefix = stream.read(_RECORD_PREFIX_SIZE)
        if len(prefix) < _RECORD_PREFIX_SIZE:
            discarded.append(
                DiscardedRegion(
                    pos,
                    size - pos,
                    "summary section truncated: incomplete record header",
                    _sample(stream, pos),
                )
            )
            return False, schemas, channels
        opcode, length = _RECORD_PREFIX.unpack(prefix)
        if length > size - pos - _RECORD_PREFIX_SIZE:
            discarded.append(
                DiscardedRegion(
                    pos,
                    size - pos,
                    f"summary section truncated: {_opcode_name(opcode)} record "
                    f"declares {length} bytes, "
                    f"{size - pos - _RECORD_PREFIX_SIZE} remain",
                    _sample(stream, pos),
                )
            )
            return False, schemas, channels
        body = stream.read(length)
        record_length = _RECORD_PREFIX_SIZE + length
        if opcode == Opcode.FOOTER:
            if pos > summary_start:
                discarded.append(
                    DiscardedRegion(
                        summary_start,
                        pos - summary_start,
                        "summary section; indexes are rebuilt from the "
                        "validated data section, the old summary is not reused",
                        _sample(stream, summary_start),
                    )
                )
            valid = _check_footer(stream, summary_start, pos, body, discarded)
            pos += record_length
            stream.seek(pos)
            if stream.read(MAGIC_SIZE) != MCAP0_MAGIC:
                discarded.append(
                    DiscardedRegion(
                        pos,
                        size - pos,
                        "trailing magic missing or invalid after the footer",
                        _sample(stream, pos),
                    )
                )
            elif pos + MAGIC_SIZE < size:
                discarded.append(
                    DiscardedRegion(
                        pos + MAGIC_SIZE,
                        size - pos - MAGIC_SIZE,
                        "trailing bytes after the final magic",
                        _sample(stream, pos + MAGIC_SIZE),
                    )
                )
            return valid, schemas, channels
        if opcode not in _SUMMARY_OPCODES:
            discarded.append(
                DiscardedRegion(
                    pos,
                    size - pos,
                    f"unexpected {_opcode_name(opcode)} record in the summary "
                    "section",
                    _sample(stream, pos),
                )
            )
            return False, schemas, channels
        if opcode == Opcode.SCHEMA:
            try:
                schema = Schema.read(ReadDataStream(BytesIO(body)))
                schemas.setdefault(schema.id, schema)
            except Exception as exc:
                discarded.append(
                    DiscardedRegion(
                        pos,
                        size - pos,
                        f"invalid schema record in the summary section: {exc}",
                        _sample(stream, pos),
                    )
                )
                return False, schemas, channels
        elif opcode == Opcode.CHANNEL:
            try:
                channel = Channel.read(ReadDataStream(BytesIO(body)))
                channels.setdefault(channel.id, channel)
            except Exception as exc:
                discarded.append(
                    DiscardedRegion(
                        pos,
                        size - pos,
                        f"invalid channel record in the summary section: {exc}",
                        _sample(stream, pos),
                    )
                )
                return False, schemas, channels
        pos += record_length
    discarded.append(DiscardedRegion(size, 0, "file ends without a footer", b""))
    return False, schemas, channels


_FOOTER_RECORD_SIZE = _RECORD_PREFIX_SIZE + 8 + 8 + 4


def _harvest_summary_definitions(
    stream: BinaryIO, size: int, discarded: List[DiscardedRegion]
) -> Tuple[bool, Dict[int, Schema], Dict[int, Channel]]:
    """When the data section scan never reached a DataEnd record, the summary
    of a previously completed write may still sit at the end of the file.
    Definitions from a structurally valid, CRC-checked summary can stand in for
    definitions lost in the discarded tail; its indexes are never reused."""
    if size < MAGIC_SIZE + _FOOTER_RECORD_SIZE:
        return False, {}, {}
    stream.seek(size - MAGIC_SIZE)
    if stream.read(MAGIC_SIZE) != MCAP0_MAGIC:
        return False, {}, {}
    footer_offset = size - MAGIC_SIZE - _FOOTER_RECORD_SIZE
    stream.seek(footer_offset)
    prefix = stream.read(_RECORD_PREFIX_SIZE)
    if len(prefix) < _RECORD_PREFIX_SIZE:
        return False, {}, {}
    opcode, length = _RECORD_PREFIX.unpack(prefix)
    if opcode != Opcode.FOOTER or length != 8 + 8 + 4:
        return False, {}, {}
    try:
        footer = Footer.read(ReadDataStream(BytesIO(stream.read(length))))
    except Exception:
        return False, {}, {}
    if footer.summary_start == 0 or footer.summary_start >= footer_offset:
        return False, {}, {}
    return _scan_summary(
        stream, footer.summary_start, footer_offset + _FOOTER_RECORD_SIZE, discarded
    )


def _scan(stream: BinaryIO, size: int, receipt: Sequence[ConfirmedMessage]) -> _Scan:
    """Parses the damaged file structurally and returns everything needed to
    resume writing after the safe physical prefix. Parsing never resynchronizes
    by scanning record payloads for record headers: the first record that does
    not parse ends the retained prefix."""
    discarded: List[DiscardedRegion] = []
    stream.seek(0)
    magic = stream.read(MAGIC_SIZE)
    if magic != MCAP0_MAGIC:
        raise RecoveryError(f"not an MCAP file: invalid leading magic {magic!r}")
    crc = zlib.crc32(magic)
    offset = MAGIC_SIZE

    def read_record() -> Optional[Tuple[int, int, int, bytes]]:
        nonlocal crc, offset
        start = offset
        crc_before = crc
        prefix = stream.read(_RECORD_PREFIX_SIZE)
        if len(prefix) == 0:
            return None
        if len(prefix) < _RECORD_PREFIX_SIZE:
            raise _Truncated(
                f"truncated record header: {len(prefix)} of "
                f"{_RECORD_PREFIX_SIZE} bytes remain"
            )
        opcode, length = _RECORD_PREFIX.unpack(prefix)
        if length > _RECORD_SIZE_LIMIT:
            raise _Truncated(
                f"{_opcode_name(opcode)} record declares {length} bytes, "
                "exceeding the record size limit"
            )
        remaining = size - start - _RECORD_PREFIX_SIZE
        if length > remaining:
            raise _Truncated(
                f"truncated {_opcode_name(opcode)} record: declares {length} "
                f"bytes, {remaining} remain"
            )
        body = stream.read(length)
        crc = zlib.crc32(prefix, crc)
        crc = zlib.crc32(body, crc)
        offset = start + _RECORD_PREFIX_SIZE + length
        return start, crc_before, opcode, body

    try:
        first = read_record()
    except _Truncated as exc:
        raise RecoveryError(f"no complete header record: {exc}") from exc
    if first is None:
        raise RecoveryError("file contains no records")
    _, _, opcode, body = first
    if opcode != Opcode.HEADER:
        raise RecoveryError(f"expected a header record, found {_opcode_name(opcode)}")
    try:
        header = Header.read(ReadDataStream(BytesIO(body)))
    except Exception as exc:
        raise RecoveryError(f"invalid header record: {exc}") from exc

    kept_offset = offset
    kept_crc = crc
    schemas: Dict[int, Schema] = {}
    channels: Dict[int, Channel] = {}
    captured_schemas: Dict[int, Schema] = {}
    captured_channels: Dict[int, Channel] = {}
    chunk_indices: List[ChunkIndex] = []
    attachment_indexes: List[AttachmentIndex] = []
    metadata_indexes: List[MetadataIndex] = []
    channel_message_counts: Dict[int, int] = defaultdict(int)
    message_count = 0
    message_start_time = 0
    message_end_time = 0
    receipt_pos = 0
    data_crcs_enabled = False
    summary_scanned = False
    saw_chunked_messages = False
    saw_unchunked_messages = False
    pending_chunk: Optional[ChunkIndex] = None
    pending_entries: Optional[Dict[int, List[Tuple[int, int]]]] = None

    def close_pending_chunk() -> None:
        nonlocal pending_chunk, pending_entries
        if (
            pending_chunk is not None
            and pending_entries is not None
            and 0 < len(pending_chunk.message_index_offsets) < len(pending_entries)
        ):
            # Partial message index coverage cannot be trusted for topic
            # filtering. The records stay in the file but are not indexed.
            pending_chunk.message_index_offsets = {}
            pending_chunk.message_index_length = 0
        pending_chunk = None
        pending_entries = None

    def match_receipt(messages: List[Message]) -> int:
        matched = 0
        for message in messages:
            index = receipt_pos + matched
            if index >= len(receipt):
                break
            if (
                _message_key(
                    message.channel_id,
                    message.log_time,
                    message.publish_time,
                    message.sequence,
                    message.data,
                )
                != receipt[index].key()
            ):
                break
            matched += 1
        return matched

    def divergence_reason(matched: int) -> str:
        if receipt_pos + matched >= len(receipt):
            return "file contains messages beyond the confirmed receipt"
        return (
            "message content does not match confirmed receipt entry "
            f"{receipt_pos + matched}"
        )

    def absorb(messages: List[Message]) -> None:
        nonlocal message_count, message_start_time, message_end_time, receipt_pos
        for message in messages:
            if message_count == 0:
                message_start_time = message.log_time
            elif message.log_time < message_start_time:
                message_start_time = message.log_time
            if message.log_time > message_end_time:
                message_end_time = message.log_time
            channel_message_counts[message.channel_id] += 1
            message_count += 1
        receipt_pos += len(messages)

    def check_schema(schema: Schema) -> None:
        known = captured_schemas.get(schema.id)
        if known is not None and known != schema:
            raise _Invalid(f"conflicting definitions for schema id {schema.id}")

    def check_channel(channel: Channel) -> None:
        known = captured_channels.get(channel.id)
        if known is not None and known != channel:
            raise _Invalid(f"conflicting definitions for channel id {channel.id}")

    def add_schema(schema: Schema) -> None:
        check_schema(schema)
        if schema.id not in captured_schemas:
            captured_schemas[schema.id] = schema
            schemas[schema.id] = schema

    def add_channel(channel: Channel) -> None:
        check_channel(channel)
        if channel.id not in captured_channels:
            captured_channels[channel.id] = channel
            channels[channel.id] = channel

    while True:
        try:
            record = read_record()
        except _Truncated as exc:
            discarded.append(
                DiscardedRegion(
                    offset, size - offset, str(exc), _sample(stream, offset)
                )
            )
            break
        if record is None:
            discarded.append(
                DiscardedRegion(
                    size, 0, "file ends without a data end record", b""
                )
            )
            break
        start, crc_before, opcode, body = record
        record_length = _RECORD_PREFIX_SIZE + len(body)

        if opcode != Opcode.MESSAGE_INDEX:
            close_pending_chunk()

        try:
            if opcode == Opcode.CHUNK:
                try:
                    chunk = Chunk.read(ReadDataStream(BytesIO(body)))
                    data = _get_chunk_data(chunk, validate_crc=True)
                except UnsupportedCompressionError as exc:
                    raise RecoveryError(
                        f"cannot validate chunk at offset {start}: {exc}"
                    ) from exc
                except Exception as exc:
                    raise _Invalid(f"invalid chunk record: {exc}")
                if len(data) != chunk.uncompressed_size:
                    raise _Invalid(
                        f"chunk uncompressed size {chunk.uncompressed_size} does "
                        f"not match its decompressed size {len(data)}"
                    )
                try:
                    records = _parse_chunk_records(data)
                except Exception as exc:
                    raise _Invalid(f"chunk contents are not parseable: {exc}")
                messages = [r for r in records if isinstance(r, Message)]
                chunk_schemas = [r for r in records if isinstance(r, Schema)]
                chunk_channels = [r for r in records if isinstance(r, Channel)]
                # Validate read-only first: nothing below may mutate the scan
                # state until every check for this chunk has passed.
                matched = match_receipt(messages)
                if matched < len(messages):
                    raise _Invalid(divergence_reason(matched))
                for schema in chunk_schemas:
                    check_schema(schema)
                for channel in chunk_channels:
                    check_channel(channel)
                if messages:
                    chunk_start_time = min(m.log_time for m in messages)
                    chunk_end_time = max(m.log_time for m in messages)
                    if (
                        chunk.message_start_time != chunk_start_time
                        or chunk.message_end_time != chunk_end_time
                    ):
                        raise _Invalid(
                            "chunk message time range does not match its contents"
                        )
                else:
                    chunk_start_time = chunk.message_start_time
                    chunk_end_time = chunk.message_end_time
                absorb(messages)
                if messages:
                    saw_chunked_messages = True
                for schema in chunk_schemas:
                    add_schema(schema)
                for channel in chunk_channels:
                    add_channel(channel)
                chunk_index = ChunkIndex(
                    message_start_time=chunk_start_time,
                    message_end_time=chunk_end_time,
                    chunk_start_offset=start,
                    chunk_length=record_length,
                    message_index_offsets={},
                    message_index_length=0,
                    compression=chunk.compression,
                    compressed_size=len(chunk.data),
                    uncompressed_size=chunk.uncompressed_size,
                )
                chunk_indices.append(chunk_index)
                pending_chunk = chunk_index
                pending_entries = _message_entries(data)
            elif opcode == Opcode.MESSAGE:
                if len(body) < MESSAGE_FIELDS_SIZE:
                    raise _Invalid("truncated message record")
                channel_id, sequence, log_time, publish_time = (
                    MESSAGE_FIELDS.unpack_from(body)
                )
                message = Message(
                    channel_id,
                    log_time,
                    body[MESSAGE_FIELDS_SIZE:],
                    publish_time,
                    sequence,
                )
                if match_receipt([message]) == 0:
                    raise _Invalid(divergence_reason(0))
                absorb([message])
                saw_unchunked_messages = True
            elif opcode == Opcode.SCHEMA:
                try:
                    add_schema(Schema.read(ReadDataStream(BytesIO(body))))
                except _Invalid:
                    raise
                except Exception as exc:
                    raise _Invalid(f"invalid schema record: {exc}")
            elif opcode == Opcode.CHANNEL:
                try:
                    add_channel(Channel.read(ReadDataStream(BytesIO(body))))
                except _Invalid:
                    raise
                except Exception as exc:
                    raise _Invalid(f"invalid channel record: {exc}")
            elif opcode == Opcode.MESSAGE_INDEX:
                try:
                    index = MessageIndex.read(ReadDataStream(BytesIO(body)))
                except Exception as exc:
                    raise _Invalid(f"invalid message index record: {exc}")
                if pending_chunk is not None and pending_entries is not None:
                    expected = pending_entries.get(index.channel_id, [])
                    if list(index.records) != expected:
                        raise _Invalid(
                            f"message index for channel {index.channel_id} does "
                            "not match the contents of the preceding chunk"
                        )
                    if index.channel_id in pending_chunk.message_index_offsets:
                        raise _Invalid(
                            f"duplicate message index for channel "
                            f"{index.channel_id}"
                        )
                    pending_chunk.message_index_offsets[index.channel_id] = start
                    pending_chunk.message_index_length += record_length
                # A message index with no preceding chunk is kept unreferenced.
            elif opcode == Opcode.ATTACHMENT:
                try:
                    attachment = Attachment.read(ReadDataStream(BytesIO(body)))
                except Exception as exc:
                    raise _Invalid(f"invalid attachment record: {exc}")
                stored_crc = struct.unpack_from("<I", body, len(body) - 4)[0]
                if stored_crc != 0 and stored_crc != zlib.crc32(body[:-4]):
                    raise _Invalid("attachment CRC mismatch")
                attachment_indexes.append(
                    AttachmentIndex(
                        offset=start,
                        length=record_length,
                        create_time=attachment.create_time,
                        log_time=attachment.log_time,
                        data_size=len(attachment.data),
                        name=attachment.name,
                        media_type=attachment.media_type,
                    )
                )
            elif opcode == Opcode.METADATA:
                try:
                    metadata = Metadata.read(ReadDataStream(BytesIO(body)))
                except Exception as exc:
                    raise _Invalid(f"invalid metadata record: {exc}")
                metadata_indexes.append(
                    MetadataIndex(
                        offset=start, length=record_length, name=metadata.name
                    )
                )
            elif opcode == Opcode.DATA_END:
                try:
                    data_end = DataEnd.read(ReadDataStream(BytesIO(body)))
                except Exception as exc:
                    raise _Invalid(f"invalid data end record: {exc}")
                if data_end.data_section_crc != 0:
                    if data_end.data_section_crc != crc_before:
                        raise RecoveryError(
                            f"data section CRC mismatch: the data end record "
                            f"expects {data_end.data_section_crc}, the data "
                            f"section calculates to {crc_before}"
                        )
                    data_crcs_enabled = True
                discarded.append(
                    DiscardedRegion(
                        start,
                        record_length,
                        "data end record (data section CRC "
                        + ("valid" if data_crcs_enabled else "not enabled")
                        + "); rewritten with the rebuilt summary",
                        _sample(stream, start),
                    )
                )
                summary_valid, summary_schemas, summary_channels = _scan_summary(
                    stream, offset, size, discarded
                )
                if summary_valid:
                    for schema_id, schema in summary_schemas.items():
                        captured_schemas.setdefault(schema_id, schema)
                    for channel_id, channel in summary_channels.items():
                        captured_channels.setdefault(channel_id, channel)
                summary_scanned = True
                # DataEnd, summary and footer are rebuilt, not retained.
                break
            elif opcode in (
                Opcode.HEADER,
                Opcode.FOOTER,
                Opcode.STATISTICS,
                Opcode.CHUNK_INDEX,
                Opcode.ATTACHMENT_INDEX,
                Opcode.METADATA_INDEX,
                Opcode.SUMMARY_OFFSET,
            ):
                raise _Invalid(
                    f"unexpected {_opcode_name(opcode)} record in the data section"
                )
            # Unknown opcodes are skipped, as readers do.
        except _Invalid as exc:
            discarded.append(
                DiscardedRegion(start, size - start, str(exc), _sample(stream, start))
            )
            break

        kept_offset = offset
        kept_crc = crc

    if not summary_scanned:
        summary_valid, summary_schemas, summary_channels = (
            _harvest_summary_definitions(stream, size, discarded)
        )
        if summary_valid:
            for schema_id, schema in summary_schemas.items():
                captured_schemas.setdefault(schema_id, schema)
            for channel_id, channel in summary_channels.items():
                captured_channels.setdefault(channel_id, channel)

    if saw_chunked_messages and saw_unchunked_messages:
        raise RecoveryError(
            "file mixes chunked and unchunked messages; resuming it could not "
            "produce a file whose random access matches its sequential read"
        )

    return _Scan(
        header=header,
        kept_offset=kept_offset,
        kept_crc=kept_crc,
        data_crcs_enabled=data_crcs_enabled,
        schemas=schemas,
        channels=channels,
        captured_schemas=captured_schemas,
        captured_channels=captured_channels,
        chunk_indices=chunk_indices,
        attachment_indexes=attachment_indexes,
        metadata_indexes=metadata_indexes,
        statistics=Statistics(
            attachment_count=len(attachment_indexes),
            channel_count=len(channels),
            channel_message_counts=channel_message_counts,
            chunk_count=len(chunk_indices),
            message_count=message_count,
            metadata_count=len(metadata_indexes),
            message_start_time=message_start_time,
            message_end_time=message_end_time,
            schema_count=len(schemas),
        ),
        messages_kept=receipt_pos,
        unchunked_messages=saw_unchunked_messages,
        discarded=discarded,
    )


def _verify(
    path: str,
    receipt: List[ConfirmedMessage],
    channels: Dict[int, Channel],
    random_queries: int,
    random_seed: int,
) -> int:
    """Verifies the completed file against the receipt: once sequentially and
    once through random topic/time queries. Returns the number of random
    queries performed. Raises RecoveryError on any mismatch."""
    expected_keys = [message.key() for message in receipt]

    with open(path, "rb") as stream:
        sequential = [
            _message_key(
                record.channel_id,
                record.log_time,
                record.publish_time,
                record.sequence,
                record.data,
            )
            for record in StreamReader(stream, validate_crcs=True).records
            if isinstance(record, Message)
        ]
    if sequential != expected_keys:
        raise RecoveryError(
            "sequential read of the completed file does not match the "
            "confirmed receipt"
        )

    with open(path, "rb") as stream:
        reader = make_reader(stream)
        summary = reader.get_summary()
        if summary is None or summary.statistics is None:
            raise RecoveryError("completed file has no summary and statistics")
        statistics = summary.statistics
        if statistics.message_count != len(receipt):
            raise RecoveryError("statistics message count does not match the receipt")
        counts: Dict[int, int] = defaultdict(int)
        for message in receipt:
            counts[message.channel_id] += 1
        if dict(statistics.channel_message_counts) != dict(counts):
            raise RecoveryError(
                "statistics channel message counts do not match the receipt"
            )
        if receipt:
            if statistics.message_start_time != min(m.log_time for m in receipt):
                raise RecoveryError("statistics start time does not match the receipt")
            if statistics.message_end_time != max(m.log_time for m in receipt):
                raise RecoveryError("statistics end time does not match the receipt")

        queries = 0
        if receipt:
            by_topic: Dict[str, List[ConfirmedMessage]] = defaultdict(list)
            for message in receipt:
                by_topic[channels[message.channel_id].topic].append(message)
            topics = sorted(by_topic)
            rng = random.Random(random_seed)
            for _ in range(random_queries):
                topic = rng.choice(topics)
                messages = by_topic[topic]
                first = rng.choice(messages).log_time
                second = rng.choice(messages).log_time
                start, end = (
                    (first, second + 1) if first <= second else (second, first + 1)
                )
                expected = [
                    message.key()
                    for message in messages
                    if start <= message.log_time < end
                ]
                got = [
                    _message_key(
                        message.channel_id,
                        message.log_time,
                        message.publish_time,
                        message.sequence,
                        message.data,
                    )
                    for _, _, message in reader.iter_messages(
                        topics=[topic],
                        starting_at=start,
                        ending_before=end,
                        log_time_order=False,
                    )
                ]
                if got != expected:
                    raise RecoveryError(
                        f"random access query for topic {topic!r} in "
                        f"[{start}, {end}) does not match the confirmed receipt"
                    )
                queries += 1
            got_all = sorted(
                _message_key(
                    message.channel_id,
                    message.log_time,
                    message.publish_time,
                    message.sequence,
                    message.data,
                )
                for _, _, message in reader.iter_messages(log_time_order=True)
            )
            if got_all != sorted(expected_keys):
                raise RecoveryError(
                    "log-time-ordered read of the completed file does not match "
                    "the confirmed receipt"
                )
    return queries


def recover(
    source: Union[str, os.PathLike, BinaryIO],
    receipt: Sequence[ConfirmedMessage],
    output: Union[str, os.PathLike],
    *,
    chunk_size: int = 1024 * 1024,
    compression: CompressionType = CompressionType.NONE,
    index_types: IndexType = IndexType.ALL,
    enable_data_crcs: Optional[bool] = None,
    random_queries: int = 16,
    random_seed: int = 0,
) -> RecoveryReport:
    """Completes an interrupted MCAP write into a valid archive.

    The damaged file is parsed structurally and its longest safe prefix is
    retained. Messages in that prefix must match the leading entries of the
    confirmed receipt; anything beyond (an unfinished chunk, a torn record,
    unconfirmed messages, the old DataEnd, summary and Footer) is discarded
    with evidence kept in the report. The remaining receipt messages are then
    appended and a fresh DataEnd, summary and Footer are written. Indexes and
    statistics are rebuilt from validated content in actual file order; the
    old summary is never reused. Definitions and messages already verified in
    the retained prefix are not written again.

    The completed file is published to ``output`` only after a full sequential
    read and random topic/time queries both match the receipt. The damaged
    original is left untouched.

    :param source: Path of (or seekable binary stream for) the damaged file.
    :param receipt: The confirmed messages, in write order. The completed file
        contains exactly these messages.
    :param output: Path to publish the completed file to. Must differ from
        ``source``.
    :param chunk_size: Maximum size of chunks written while appending.
    :param compression: Compression for newly written chunks. Compression is a
        per-chunk property, so it need not match the existing chunks.
    :param index_types: Indexes to write into the new summary.
    :param enable_data_crcs: Whether to write a data section CRC. Defaults to
        the convention found in the damaged file's DataEnd record.
    :param random_queries: Number of random topic/time queries to verify
        against the receipt before publishing.
    :param random_seed: Seed for the random query generator.
    """
    output_path = os.fspath(output)
    receipt = list(receipt)

    if isinstance(source, (str, os.PathLike)):
        source_path = os.fspath(source)
        if os.path.abspath(source_path) == os.path.abspath(output_path):
            raise ValueError(
                "output must differ from source: the damaged original is preserved"
            )
        stream: BinaryIO = open(source_path, "rb")
        should_close = True
    else:
        source_path = "<stream>"
        stream = source
        should_close = False
        if not stream.seekable():
            raise RecoveryError("recovery requires a seekable source")

    try:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        scan = _scan(stream, size, receipt)
    finally:
        if should_close:
            stream.close()

    remaining = receipt[scan.messages_kept :]

    # Definitions that receipt messages still need but whose records were in
    # the discarded tail are re-registered from the definitions captured while
    # scanning. Verified definitions in the retained prefix are not rewritten.
    missing_channels: Dict[int, Channel] = {}
    for channel_id in sorted({message.channel_id for message in remaining}):
        if channel_id in scan.channels:
            continue
        channel = scan.captured_channels.get(channel_id)
        if channel is None:
            raise RecoveryError(
                f"receipt references channel id {channel_id}, which is not "
                "defined in the recoverable prefix"
            )
        missing_channels[channel_id] = channel
    missing_schemas: Dict[int, Schema] = {}
    for channel in missing_channels.values():
        schema_id = channel.schema_id
        if (
            schema_id == 0
            or schema_id in scan.schemas
            or schema_id in missing_schemas
        ):
            continue
        schema = scan.captured_schemas.get(schema_id)
        if schema is None:
            raise RecoveryError(
                f"channel id {channel.id} references schema id {schema_id}, "
                "which is not defined in the recoverable prefix"
            )
        missing_schemas[schema_id] = schema

    out_dir = os.path.dirname(os.path.abspath(output_path))
    fd, tmp_path = tempfile.mkstemp(
        prefix=os.path.basename(output_path) + ".", suffix=".partial", dir=out_dir
    )
    try:
        with os.fdopen(fd, "wb") as out_stream:
            if should_close:
                in_stream: BinaryIO = open(source_path, "rb")
            else:
                in_stream = stream
                in_stream.seek(0)
            try:
                left = scan.kept_offset
                while left > 0:
                    block = in_stream.read(min(_COPY_BUFFER_SIZE, left))
                    if not block:
                        raise RecoveryError("source shrank during recovery")
                    out_stream.write(block)
                    left -= len(block)
            finally:
                if should_close:
                    in_stream.close()

            writer = Writer.resume(
                out_stream,
                schemas=list(scan.schemas.values()),
                channels=list(scan.channels.values()),
                statistics=scan.statistics,
                chunk_indices=scan.chunk_indices,
                attachment_indexes=scan.attachment_indexes,
                metadata_indexes=scan.metadata_indexes,
                data_section_crc=scan.kept_crc,
                chunk_size=chunk_size,
                compression=compression,
                index_types=index_types,
                # An unchunked prefix stays unchunked: mixing in new chunks
                # would hide the kept messages from index-based readers.
                use_chunking=not scan.unchunked_messages,
                enable_data_crcs=(
                    scan.data_crcs_enabled
                    if enable_data_crcs is None
                    else enable_data_crcs
                ),
            )
            for schema_id in sorted(missing_schemas):
                schema = missing_schemas[schema_id]
                new_id = writer.register_schema(
                    name=schema.name, encoding=schema.encoding, data=schema.data
                )
                if new_id != schema_id:
                    raise RecoveryError(
                        f"cannot restore schema id {schema_id} without "
                        "rewriting later definitions"
                    )
            for channel_id in sorted(missing_channels):
                channel = missing_channels[channel_id]
                new_id = writer.register_channel(
                    topic=channel.topic,
                    message_encoding=channel.message_encoding,
                    schema_id=channel.schema_id,
                    metadata=channel.metadata,
                )
                if new_id != channel_id:
                    raise RecoveryError(
                        f"cannot restore channel id {channel_id} without "
                        "rewriting later definitions"
                    )
            for message in remaining:
                writer.add_message(
                    channel_id=message.channel_id,
                    log_time=message.log_time,
                    data=message.data,
                    publish_time=message.publish_time,
                    sequence=message.sequence,
                )
            writer.finish()
            out_stream.flush()

        channels = dict(scan.channels)
        channels.update(missing_channels)
        queries = _verify(tmp_path, receipt, channels, random_queries, random_seed)
        os.replace(tmp_path, output_path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    return RecoveryReport(
        source=source_path,
        output=output_path,
        kept_offset=scan.kept_offset,
        discarded=scan.discarded,
        messages_kept=scan.messages_kept,
        messages_appended=len(remaining),
        message_count=len(receipt),
        random_queries=queries,
    )


__all__ = [
    "ConfirmedMessage",
    "DiscardedRegion",
    "RecoveryError",
    "RecoveryReport",
    "recover",
]
