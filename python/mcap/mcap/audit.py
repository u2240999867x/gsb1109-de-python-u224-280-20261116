"""Acceptance auditing of MCAP summary and chunk-index claims.

The audit rebuilds an independent ledger from the bytes actually present in the
file, then compares that ledger against the declared
:class:`~mcap.records.Statistics` and :class:`~mcap.records.ChunkIndex` records.
Declarations are treated only as constraints: a missing or unreadable chunk
never contributes fabricated message rows, it only widens the reported bounds.

Each finding points back at the byte range (or record) where the conflict lives,
so an acceptance reviewer can locate every contradiction without re-reading the
file. The scanner is sequential and keeps at most one chunk's decompressed
contents in memory at a time.
"""

import struct
import zlib
from dataclasses import dataclass, field
from io import BytesIO
from typing import IO, Dict, List, Optional, Tuple, Union, cast

try:
    import lz4.frame as lz4
except ImportError:
    lz4 = None

try:
    import zstandard
except ImportError:
    zstandard = None

from .data_stream import ReadDataStream
from .exceptions import (
    EndOfFile,
)
from .opcode import Opcode
from .records import (
    MESSAGE_FIELDS,
    MESSAGE_FIELDS_SIZE,
    Channel,
    Chunk,
    ChunkIndex,
    Footer,
    Header,
    MessageIndex,
    Schema,
    Statistics,
)

MAGIC = struct.pack("<8B", 137, 77, 67, 65, 80, 48, 13, 10)
_RECORD_PREFIX = struct.Struct("<BQ")
_RECORD_PREFIX_SIZE = _RECORD_PREFIX.size
_U16 = struct.Struct("<H")
_U64 = struct.Struct("<Q")

_OPCODE_HEADER = int(Opcode.HEADER)
_OPCODE_FOOTER = int(Opcode.FOOTER)
_OPCODE_SCHEMA = int(Opcode.SCHEMA)
_OPCODE_CHANNEL = int(Opcode.CHANNEL)
_OPCODE_MESSAGE = int(Opcode.MESSAGE)
_OPCODE_CHUNK = int(Opcode.CHUNK)
_OPCODE_MESSAGE_INDEX = int(Opcode.MESSAGE_INDEX)
_OPCODE_CHUNK_INDEX = int(Opcode.CHUNK_INDEX)
_OPCODE_STATISTICS = int(Opcode.STATISTICS)

# Verdicts shared by every claim comparison.
VERDICT_CONFIRMED = "confirmed"
VERDICT_CONTRADICTED = "contradicted"
VERDICT_INDETERMINATE = "indeterminate"
VERDICT_UNVERIFIABLE = "unverifiable"
VERDICT_UNCHECKED = "unchecked"


class AuditError(Exception):
    """Raised when a file cannot be audited at all, for example because it does
    not begin with the MCAP magic. Unreadable individual chunks do not raise;
    they are recorded as findings instead."""


@dataclass
class ByteRange:
    """A half-open ``[start, end)`` byte range inside the audited file."""

    start: int
    end: int

    def __str__(self) -> str:
        return f"bytes [{self.start}, {self.end})"


@dataclass
class Finding:
    """A single audit observation.

    :ivar code: stable machine-readable identifier.
    :ivar severity: ``error`` for contradictions or structural faults,
        ``warning`` for conditions that block verification (such as an
        unreadable chunk), ``info`` for ledger notes.
    :ivar message: human-readable explanation.
    :ivar byte_range: where in the file the observation applies, when known.
    :ivar chunk_index: ordinal of the chunk the finding relates to, if any.
    :ivar channel_id: channel id the finding relates to, if any.
    """

    code: str
    severity: str
    message: str
    byte_range: Optional[ByteRange] = None
    chunk_index: Optional[int] = None
    channel_id: Optional[int] = None

    def __str__(self) -> str:
        location: List[str] = []
        if self.chunk_index is not None:
            location.append(f"chunk #{self.chunk_index}")
        if self.channel_id is not None:
            location.append(f"channel {self.channel_id}")
        if self.byte_range is not None:
            location.append(str(self.byte_range))
        prefix = f"[{self.severity}/{self.code}]"
        if location:
            return f"{prefix} {' '.join(location)}: {self.message}"
        return f"{prefix} {self.message}"


@dataclass
class ChunkAudit:
    """The independent ledger for one data-section chunk, paired with the
    verdict on its chunk-index declaration."""

    ordinal: int
    byte_range: ByteRange
    compression: str
    readable: bool
    error: Optional[str]
    observed_message_count: int
    observed_channel_counts: Dict[int, int]
    observed_channel_log_times: Dict[int, List[int]]
    observed_min_log_time: Optional[int]
    observed_max_log_time: Optional[int]
    index: Optional[ChunkIndex]
    index_byte_range: Optional[ByteRange]
    index_verdict: str
    # Decompressed byte count actually obtained; None when unreadable.
    observed_uncompressed_size: Optional[int] = None
    findings: List[Finding] = field(default_factory=list)


@dataclass
class ClaimVerdict:
    """Verdict for a single scalar or mapping claim."""

    name: str
    verdict: str
    observed: Optional[object]
    declared: Optional[object]
    detail: str
    byte_range: Optional[ByteRange] = None
    channel_id: Optional[int] = None


@dataclass
class MessageIndexAudit:
    """Verdict on the MessageIndex record declared for one channel of a chunk."""

    channel_id: int
    verdict: str
    detail: str
    declared_entry_count: Optional[int]
    observed_message_count: Optional[int]
    message_index_byte_range: Optional[ByteRange]


@dataclass
class AuditReport:
    """Result of :func:`audit_mcap`.

    The ``observed_*`` fields are proven exclusively from message records that
    were actually read. When bytes are missing, ``proven_*_bound`` fields state
    what can still be proven about the whole file; declarations never feed the
    ledger.
    """

    findings: List[Finding] = field(default_factory=list)
    chunks: List[ChunkAudit] = field(default_factory=list)
    statistics_verdicts: List[ClaimVerdict] = field(default_factory=list)
    message_index_audits: Dict[int, List[MessageIndexAudit]] = field(
        default_factory=dict
    )
    channel_verdicts: List[ClaimVerdict] = field(default_factory=list)
    readable: bool = True

    # Independent ledger, proven from message records alone.
    observed_message_count: int = 0
    observed_channel_counts: Dict[int, int] = field(default_factory=dict)
    observed_min_log_time: Optional[int] = None
    observed_max_log_time: Optional[int] = None
    # Whole-file bounds that stay provable when part of the file is unreadable.
    # The count is a true lower bound: hidden messages can only raise it. The
    # observed minimum is an upper bound on the true minimum, and the observed
    # maximum is a lower bound on the true maximum; the latter goes open
    # (None) whenever bytes are missing rather than quoting a declaration.
    proven_message_count_lower_bound: int = 0
    proven_min_log_time_bound: Optional[int] = None
    proven_max_log_time_bound: Optional[int] = None
    channels: Dict[int, Channel] = field(default_factory=dict)
    schemas: Dict[int, Schema] = field(default_factory=dict)
    # First chunk ordinal in which each channel's messages were seen, so a
    # message referencing an undefined channel can point back at a location.
    channel_first_chunk: Dict[int, int] = field(default_factory=dict)
    # File range of the first unchunked message on each channel.
    channel_first_message_range: Dict[int, ByteRange] = field(default_factory=dict)
    statistics: Optional[Statistics] = None
    statistics_byte_range: Optional[ByteRange] = None
    has_statistics: bool = False

    def errors(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    def warnings(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    def is_clean(self) -> bool:
        """True when every byte was read and no finding contradicts a declaration
        or reports a structural fault. Files that simply omit optional indexes are
        still clean; their unverifiable claims show up as warnings instead."""
        return self.readable and not self.errors()

    def add_finding(self, finding: Finding) -> None:
        self.findings.append(finding)


def audit_mcap(
    source: Union[str, IO[bytes]],
    *,
    validate_crcs: bool = True,
) -> AuditReport:
    """Audit an MCAP file against its summary and chunk-index declarations.

    :param source: a file path or a binary, seekable file-like object. The audit
        needs random access to resolve chunk-index offsets, so non-seekable
        streams are not accepted.
    :param validate_crcs: when True (default) a chunk whose stored CRC32 does not
        match its decompressed contents is treated as unreadable. Set to False to
        audit CRC-disabled or CRC-corrupt files on the strength of their bytes.
    :returns: an :class:`AuditReport` whose ledger is built only from records
        that were actually read.
    :raises AuditError: if the source is not a seekable MCAP file.
    """
    owned_stream = False
    stream: IO[bytes]
    if isinstance(source, str):
        stream = open(source, "rb")
        owned_stream = True
    else:
        stream = source
    seekable = stream.seekable() if hasattr(stream, "seekable") else hasattr(stream, "seek")
    if not seekable:
        raise AuditError("audit_mcap requires a seekable binary stream or a path")
    try:
        return _AuditRunner(stream, validate_crcs).run()
    finally:
        if owned_stream:
            stream.close()


class _ChunkUnreadable(Exception):
    """Internal control-flow signal for a chunk whose bytes cannot be trusted."""


class _AuditRunner:
    def __init__(self, stream: IO[bytes], validate_crcs: bool):
        self._stream = stream
        self._validate_crcs = validate_crcs
        self._report = AuditReport()
        self._chunk_indexes: List[Tuple[ChunkIndex, ByteRange]] = []
        self._message_indexes: List[Tuple[MessageIndex, ByteRange]] = []
        self._seen_statistics: List[Tuple[Statistics, ByteRange]] = []

    def run(self) -> AuditReport:
        self._read_magic()
        self._scan_records()
        self._reconcile()
        return self._report

    # ------------------------------------------------------------------ scan

    def _read_magic(self) -> None:
        self._stream.seek(0)
        magic = self._stream.read(len(MAGIC))
        if magic != MAGIC:
            raise AuditError("not a valid MCAP file: bad leading magic at bytes [0, 8)")

    def _scan_records(self) -> None:
        stream = self._stream
        stream.seek(len(MAGIC))
        file_size = self._file_size()
        report = self._report
        while True:
            offset = stream.tell()
            prefix = stream.read(_RECORD_PREFIX_SIZE)
            if len(prefix) == 0:
                report.add_finding(
                    Finding(
                        code="truncated_data_section",
                        severity="warning",
                        message=(
                            "file ends without a footer; trailing bytes are missing"
                        ),
                        byte_range=ByteRange(offset, file_size),
                    )
                )
                report.readable = False
                break
            if len(prefix) < _RECORD_PREFIX_SIZE:
                report.add_finding(
                    Finding(
                        code="truncated_record_prefix",
                        severity="warning",
                        message=(
                            "record header is truncated; remaining bytes cannot be "
                            "attributed to any record"
                        ),
                        byte_range=ByteRange(offset, file_size),
                    )
                )
                report.readable = False
                break
            opcode, length = _RECORD_PREFIX.unpack(prefix)
            record_end = offset + _RECORD_PREFIX_SIZE + length
            if record_end > file_size:
                if opcode == _OPCODE_CHUNK:
                    # Keep the partial chunk in the ledger as an unreadable one so
                    # intact chunks stay independently countable.
                    self._audit_truncated_chunk(ByteRange(offset, file_size))
                report.add_finding(
                    Finding(
                        code="truncated_record",
                        severity="warning",
                        message=(
                            f"record op=0x{opcode:02x} declares {length} body bytes "
                            f"but only {file_size - stream.tell()} remain"
                        ),
                        byte_range=ByteRange(offset, file_size),
                    )
                )
                report.readable = False
                break
            self._handle_record(opcode, length, ByteRange(offset, record_end))
            stream.seek(record_end)
            if opcode == _OPCODE_FOOTER:
                trailing = stream.read(len(MAGIC))
                if trailing != MAGIC:
                    report.add_finding(
                        Finding(
                            code="bad_trailing_magic",
                            severity="error",
                            message="trailing magic after footer is missing or invalid",
                            byte_range=ByteRange(
                                stream.tell() - len(trailing), file_size
                            ),
                        )
                    )
                break

    def _handle_record(self, opcode: int, length: int, byte_range: ByteRange) -> None:
        if opcode == _OPCODE_HEADER:
            self._read_body_then(Header.read, byte_range)
        elif opcode == _OPCODE_SCHEMA:
            body = self._read_body(byte_range)
            if body is not None:
                self._register_schema(body, byte_range)
        elif opcode == _OPCODE_CHANNEL:
            body = self._read_body(byte_range)
            if body is not None:
                self._register_channel(body, byte_range)
        elif opcode == _OPCODE_MESSAGE:
            body = self._read_body(byte_range)
            if body is not None:
                self._ledger_message_body(body, byte_range)
        elif opcode == _OPCODE_CHUNK:
            self._audit_chunk(byte_range)
        elif opcode == _OPCODE_MESSAGE_INDEX:
            body = self._read_body(byte_range)
            if body is not None:
                try:
                    index = MessageIndex.read(ReadDataStream(BytesIO(body)))
                except EndOfFile:
                    self._truncated("message index", byte_range)
                else:
                    self._message_indexes.append((index, byte_range))
        elif opcode == _OPCODE_CHUNK_INDEX:
            body = self._read_body(byte_range)
            if body is not None:
                try:
                    index = ChunkIndex.read(ReadDataStream(BytesIO(body)))
                except EndOfFile:
                    self._truncated("chunk index", byte_range)
                else:
                    self._chunk_indexes.append((index, byte_range))
        elif opcode == _OPCODE_STATISTICS:
            body = self._read_body(byte_range)
            if body is not None:
                try:
                    statistics = Statistics.read(ReadDataStream(BytesIO(body)))
                except EndOfFile:
                    self._truncated("statistics", byte_range)
                else:
                    self._seen_statistics.append((statistics, byte_range))
        elif opcode == _OPCODE_FOOTER:
            self._read_body_then(Footer.read, byte_range)
        # DataEnd, padding and unknown records need no ledger work.

    def _register_schema(self, body: bytes, byte_range: ByteRange) -> None:
        try:
            schema = Schema.read(ReadDataStream(BytesIO(body)))
        except EndOfFile:
            self._truncated("schema", byte_range)
            return
        existing = self._report.schemas.get(schema.id)
        if existing is not None and (
            existing.name != schema.name
            or existing.encoding != schema.encoding
            or existing.data != schema.data
        ):
            self._report.add_finding(
                Finding(
                    code="conflicting_schema_id",
                    severity="error",
                    message=(
                        f"schema id {schema.id} redefined with different contents "
                        f"({existing.name!r} then {schema.name!r})"
                    ),
                    byte_range=byte_range,
                )
            )
        self._report.schemas[schema.id] = schema

    def _register_channel(self, body: bytes, byte_range: ByteRange) -> None:
        try:
            channel = Channel.read(ReadDataStream(BytesIO(body)))
        except EndOfFile:
            self._truncated("channel", byte_range)
            return
        existing = self._report.channels.get(channel.id)
        if existing is not None and (
            existing.topic != channel.topic
            or existing.message_encoding != channel.message_encoding
            or existing.schema_id != channel.schema_id
        ):
            self._report.add_finding(
                Finding(
                    code="conflicting_channel_id",
                    severity="error",
                    message=(
                        f"channel id {channel.id} redefined with different contents "
                        f"({existing.topic!r} then {channel.topic!r})"
                    ),
                    byte_range=byte_range,
                    channel_id=channel.id,
                )
            )
        self._report.channels[channel.id] = channel

    def _audit_chunk(self, byte_range: ByteRange) -> None:
        report = self._report
        ordinal = len(report.chunks)
        self._stream.seek(byte_range.start + _RECORD_PREFIX_SIZE)
        try:
            chunk = Chunk.read(ReadDataStream(self._stream))
        except EndOfFile:
            report.add_finding(
                Finding(
                    code="truncated_record",
                    severity="warning",
                    message="chunk record header is truncated",
                    byte_range=byte_range,
                    chunk_index=ordinal,
                )
            )
            report.readable = False
            report.chunks.append(
                self._unreadable_chunk(
                    ordinal, byte_range, "", "truncated chunk header"
                )
            )
            return

        audit = ChunkAudit(
            ordinal=ordinal,
            byte_range=byte_range,
            compression=chunk.compression,
            readable=False,
            error=None,
            observed_message_count=0,
            observed_channel_counts={},
            observed_channel_log_times={},
            observed_min_log_time=None,
            observed_max_log_time=None,
            index=None,
            index_byte_range=None,
            index_verdict=VERDICT_UNCHECKED,
        )
        try:
            data = self._decompress(chunk)
            audit.observed_uncompressed_size = len(data)
            if self._validate_crcs and chunk.uncompressed_crc != 0:
                actual_crc = zlib.crc32(data)
                if actual_crc != chunk.uncompressed_crc:
                    raise _ChunkUnreadable(
                        f"crc mismatch: declared {chunk.uncompressed_crc}, "
                        f"calculated {actual_crc}"
                    )
            self._ledger_chunk_messages(audit, data)
            audit.readable = True
        except _ChunkUnreadable as error:
            audit.error = str(error)
            report.readable = False
            report.add_finding(
                Finding(
                    code="unreadable_chunk",
                    severity="warning",
                    message=(
                        f"chunk could not be read ({error}); its messages are not "
                        "part of the proven ledger and only bound the whole-file totals"
                    ),
                    byte_range=byte_range,
                    chunk_index=ordinal,
                )
            )
        report.chunks.append(audit)

    def _unreadable_chunk(
        self, ordinal: int, byte_range: ByteRange, compression: str, error: str
    ) -> ChunkAudit:
        return ChunkAudit(
            ordinal=ordinal,
            byte_range=byte_range,
            compression=compression,
            readable=False,
            error=error,
            observed_message_count=0,
            observed_channel_counts={},
            observed_channel_log_times={},
            observed_min_log_time=None,
            observed_max_log_time=None,
            index=None,
            index_byte_range=None,
            index_verdict=VERDICT_UNVERIFIABLE,
        )

    def _audit_truncated_chunk(self, byte_range: ByteRange) -> None:
        """Record a chunk whose record body runs past end-of-file as unreadable."""
        ordinal = len(self._report.chunks)
        self._report.chunks.append(
            self._unreadable_chunk(
                ordinal, byte_range, "", "chunk record is truncated"
            )
        )
        self._report.add_finding(
            Finding(
                code="unreadable_chunk",
                severity="warning",
                message=(
                    "chunk record is truncated by end of file; its messages are not "
                    "part of the proven ledger"
                ),
                byte_range=byte_range,
                chunk_index=ordinal,
            )
        )

    def _decompress(self, chunk: Chunk) -> bytes:
        if chunk.compression == "zstd":
            if zstandard is None:
                raise _ChunkUnreadable("zstandard support is not installed")
            try:
                return zstandard.decompress(chunk.data, chunk.uncompressed_size)
            except Exception as error:
                raise _ChunkUnreadable(f"zstd decompression failed: {error}")
        if chunk.compression == "lz4":
            if lz4 is None:
                raise _ChunkUnreadable("lz4 support is not installed")
            try:
                return lz4.decompress(chunk.data)
            except Exception as error:
                raise _ChunkUnreadable(f"lz4 decompression failed: {error}")
        if chunk.compression == "":
            return chunk.data
        raise _ChunkUnreadable(f"unsupported compression {chunk.compression!r}")

    def _ledger_chunk_messages(self, audit: ChunkAudit, data: bytes) -> None:
        """Parse a decompressed chunk and extend the independent ledger from
        every well-formed message record found in it."""
        pos = 0
        end = len(data)
        chunk_min: Optional[int] = None
        chunk_max: Optional[int] = None
        while pos < end:
            if end - pos < _RECORD_PREFIX_SIZE:
                raise _ChunkUnreadable(
                    f"trailing {end - pos} bytes do not form a complete record"
                )
            opcode, length = _RECORD_PREFIX.unpack_from(data, pos)
            body_start = pos + _RECORD_PREFIX_SIZE
            record_end = body_start + length
            if record_end > end:
                raise _ChunkUnreadable(
                    "chunk record declares a length past the end of the chunk data"
                )
            if opcode == _OPCODE_MESSAGE:
                if length < MESSAGE_FIELDS_SIZE:
                    raise _ChunkUnreadable(
                        "message record is shorter than its fixed header"
                    )
                channel_id, _sequence, log_time, _publish_time = (
                    MESSAGE_FIELDS.unpack_from(data, body_start)
                )
                self._record_message(audit, channel_id, log_time)
                chunk_min = log_time if chunk_min is None else min(chunk_min, log_time)
                chunk_max = log_time if chunk_max is None else max(chunk_max, log_time)
            elif opcode == _OPCODE_CHANNEL:
                try:
                    channel = Channel.read(
                        ReadDataStream(BytesIO(data[body_start:record_end]))
                    )
                except EndOfFile:
                    raise _ChunkUnreadable("channel record inside chunk is truncated")
                existing = self._report.channels.get(channel.id)
                if existing is None:
                    self._report.channels[channel.id] = channel
            elif opcode == _OPCODE_SCHEMA:
                try:
                    schema = Schema.read(
                        ReadDataStream(BytesIO(data[body_start:record_end]))
                    )
                except EndOfFile:
                    raise _ChunkUnreadable("schema record inside chunk is truncated")
                self._report.schemas.setdefault(schema.id, schema)
            # Unknown in-chunk records are skipped, matching the reader.
            pos = record_end
        audit.observed_min_log_time = chunk_min
        audit.observed_max_log_time = chunk_max

    def _ledger_message_body(self, body: bytes, byte_range: ByteRange) -> None:
        """Ledger a message record sitting directly in the data section."""
        if len(body) < MESSAGE_FIELDS_SIZE:
            self._report.add_finding(
                Finding(
                    code="truncated_record",
                    severity="warning",
                    message="message record is shorter than its fixed header",
                    byte_range=byte_range,
                )
            )
            self._report.readable = False
            return
        channel_id, _sequence, log_time, _publish_time = MESSAGE_FIELDS.unpack_from(
            body, 0
        )
        if channel_id not in self._report.channel_first_message_range:
            self._report.channel_first_message_range[channel_id] = byte_range
        self._record_message(None, channel_id, log_time)

    def _record_message(
        self,
        audit: Optional[ChunkAudit],
        channel_id: int,
        log_time: int,
    ) -> None:
        """Extend the global ledger exactly once per message record, and, when the
        message lives in a chunk, that chunk's ledger as well."""
        report = self._report
        report.observed_message_count += 1
        report.observed_channel_counts[channel_id] = (
            report.observed_channel_counts.get(channel_id, 0) + 1
        )
        if audit is not None:
            audit.observed_message_count += 1
            audit.observed_channel_counts[channel_id] = (
                audit.observed_channel_counts.get(channel_id, 0) + 1
            )
            audit.observed_channel_log_times.setdefault(channel_id, []).append(log_time)
            if channel_id not in report.channel_first_chunk:
                report.channel_first_chunk[channel_id] = audit.ordinal
        if (
            report.observed_min_log_time is None
            or log_time < report.observed_min_log_time
        ):
            report.observed_min_log_time = log_time
        if (
            report.observed_max_log_time is None
            or log_time > report.observed_max_log_time
        ):
            report.observed_max_log_time = log_time

    # ------------------------------------------------------------- utilities

    def _read_body(self, byte_range: ByteRange) -> Optional[bytes]:
        self._stream.seek(byte_range.start + _RECORD_PREFIX_SIZE)
        body_length = byte_range.end - byte_range.start - _RECORD_PREFIX_SIZE
        body = self._stream.read(body_length)
        if len(body) != body_length:
            self._truncated("record body", byte_range)
            return None
        return body

    def _read_body_then(self, read_fn, byte_range: ByteRange) -> None:
        body = self._read_body(byte_range)
        if body is None:
            return
        try:
            read_fn(ReadDataStream(BytesIO(body)))
        except EndOfFile:
            self._truncated("record", byte_range)

    def _truncated(self, what: str, byte_range: ByteRange) -> None:
        self._report.add_finding(
            Finding(
                code="truncated_record",
                severity="warning",
                message=f"{what} is truncated",
                byte_range=byte_range,
            )
        )
        self._report.readable = False

    def _file_size(self) -> int:
        current = self._stream.tell()
        self._stream.seek(0, 2)
        size = self._stream.tell()
        self._stream.seek(current)
        return size

    # ------------------------------------------------------------- reconcile

    def _reconcile(self) -> None:
        self._select_statistics()
        self._reconcile_chunk_indexes()
        self._reconcile_message_indexes()
        self._reconcile_statistics()
        self._reconcile_channel_claims()
        self._compute_bounds()

    def _select_statistics(self) -> None:
        report = self._report
        if not self._seen_statistics:
            report.has_statistics = False
            return
        report.has_statistics = True
        if len(self._seen_statistics) > 1:
            for _, byte_range in self._seen_statistics[1:]:
                report.add_finding(
                    Finding(
                        code="duplicate_statistics",
                        severity="error",
                        message=(
                            f"{len(self._seen_statistics)} statistics records found; "
                            "only the first is treated as the declaration"
                        ),
                        byte_range=byte_range,
                    )
                )
        statistics, byte_range = self._seen_statistics[0]
        report.statistics = statistics
        report.statistics_byte_range = byte_range

    def _reconcile_chunk_indexes(self) -> None:
        report = self._report
        declared: Dict[int, Tuple[ChunkIndex, ByteRange]] = {}
        for index, byte_range in self._chunk_indexes:
            if index.chunk_start_offset in declared:
                report.add_finding(
                    Finding(
                        code="duplicate_chunk_index",
                        severity="error",
                        message=(
                            "multiple chunk index records point at chunk offset "
                            f"{index.chunk_start_offset}"
                        ),
                        byte_range=byte_range,
                    )
                )
            declared[index.chunk_start_offset] = (index, byte_range)

        for audit in report.chunks:
            match = declared.pop(audit.byte_range.start, None)
            if match is None:
                audit.index_verdict = VERDICT_UNVERIFIABLE
                report.add_finding(
                    Finding(
                        code="missing_chunk_index",
                        severity="warning",
                        message=(
                            "chunk has no chunk index; its declaration cannot be checked"
                        ),
                        byte_range=audit.byte_range,
                        chunk_index=audit.ordinal,
                    )
                )
                continue
            index, index_byte_range = match
            audit.index = index
            audit.index_byte_range = index_byte_range
            audit.index_verdict = self._verdict_for_chunk(audit, index, index_byte_range)

        for offset, (index, byte_range) in declared.items():
            report.add_finding(
                Finding(
                    code="dangling_chunk_index",
                    severity="error",
                    message=(
                        f"chunk index declares a chunk at offset {offset} but no "
                        "chunk record exists there"
                    ),
                    byte_range=byte_range,
                )
            )

    def _verdict_for_chunk(
        self,
        audit: ChunkAudit,
        index: ChunkIndex,
        index_byte_range: ByteRange,
    ) -> str:
        report = self._report
        contradicted = False

        def add(code: str, message: str, channel_id: Optional[int] = None) -> None:
            nonlocal contradicted
            contradicted = True
            report.add_finding(
                Finding(
                    code=code,
                    severity="error",
                    message=message,
                    byte_range=index_byte_range,
                    chunk_index=audit.ordinal,
                    channel_id=channel_id,
                )
            )

        if index.chunk_length != audit.byte_range.end - audit.byte_range.start:
            add(
                "bad_chunk_length",
                (
                    f"declared chunk_length {index.chunk_length} does not match the "
                    f"{audit.byte_range.end - audit.byte_range.start} bytes the chunk "
                    "record occupies"
                ),
            )
        if index.compression != audit.compression:
            add(
                "bad_compression",
                (
                    f"declared compression {index.compression!r} does not match chunk "
                    f"compression {audit.compression!r}"
                ),
            )
        actual_compressed = self._chunk_data_length(audit.byte_range)
        if (
            actual_compressed is not None
            and index.compressed_size != actual_compressed
        ):
            add(
                "bad_compressed_size",
                (
                    f"declared compressed_size {index.compressed_size} does not match "
                    f"the {actual_compressed} compressed bytes in the chunk record"
                ),
            )
        if (
            audit.observed_uncompressed_size is not None
            and index.uncompressed_size != audit.observed_uncompressed_size
        ):
            add(
                "bad_uncompressed_size",
                (
                    f"declared uncompressed_size {index.uncompressed_size} does not "
                    "match the "
                    f"{audit.observed_uncompressed_size} decompressed bytes observed"
                ),
            )
        self._check_index_map_duplicates(index, index_byte_range, audit.ordinal)

        if not audit.readable:
            report.add_finding(
                Finding(
                    code="chunk_index_unverifiable",
                    severity="info",
                    message=(
                        "chunk is unreadable, so its time and message-index claims are "
                        "only constraints"
                    ),
                    byte_range=index_byte_range,
                    chunk_index=audit.ordinal,
                )
            )
            return VERDICT_INDETERMINATE

        if audit.observed_message_count == 0:
            report.add_finding(
                Finding(
                    code="empty_chunk",
                    severity="warning",
                    message="chunk contains no message records",
                    byte_range=audit.byte_range,
                    chunk_index=audit.ordinal,
                )
            )
            if index.message_start_time != 0 or index.message_end_time != 0:
                add(
                    "bad_chunk_time_range",
                    (
                        "empty chunk must declare zero time bounds but declares "
                        f"[{index.message_start_time}, {index.message_end_time}]"
                    ),
                )
        else:
            if audit.observed_min_log_time != index.message_start_time:
                add(
                    "bad_chunk_start_time",
                    (
                        f"declared message_start_time {index.message_start_time} does "
                        "not match observed earliest log time "
                        f"{audit.observed_min_log_time}"
                    ),
                )
            if audit.observed_max_log_time != index.message_end_time:
                add(
                    "bad_chunk_end_time",
                    (
                        f"declared message_end_time {index.message_end_time} does not "
                        f"match observed latest log time {audit.observed_max_log_time}"
                    ),
                )

        for channel_id, offset in index.message_index_offsets.items():
            if channel_id not in audit.observed_channel_counts:
                add(
                    "index_unknown_channel",
                    (
                        f"a message index offset is declared for channel {channel_id} "
                        "but the chunk holds no messages on that channel"
                    ),
                    channel_id=channel_id,
                )
            zone_end = audit.byte_range.end + index.message_index_length
            in_zone = audit.byte_range.end <= offset < zone_end
            if not in_zone or not self._offset_points_at_message_index(offset):
                add(
                    "bad_message_index_offset",
                    (
                        f"message index offset {offset} for channel {channel_id} does "
                        f"not point at a message index record inside the declared zone "
                        f"[{audit.byte_range.end}, {zone_end})"
                    ),
                    channel_id=channel_id,
                )
        if index.message_index_offsets:
            for channel_id in audit.observed_channel_counts:
                if channel_id not in index.message_index_offsets:
                    add(
                        "missing_message_index_offset",
                        (
                            f"channel {channel_id} has "
                            f"{audit.observed_channel_counts[channel_id]} messages in "
                            "the chunk but no message index offset"
                        ),
                        channel_id=channel_id,
                    )
        return VERDICT_CONTRADICTED if contradicted else VERDICT_CONFIRMED

    def _check_index_map_duplicates(
        self, index: ChunkIndex, index_byte_range: ByteRange, ordinal: int
    ) -> None:
        """ChunkIndex.read collapses repeated channel ids in the wire map into a
        dict, so duplicates are re-detected here from the raw record bytes."""
        body = self._read_body(index_byte_range)
        if body is None:
            return
        try:
            ds = ReadDataStream(BytesIO(body))
            ds.read8()  # message_start_time
            ds.read8()  # message_end_time
            ds.read8()  # chunk_start_offset
            ds.read8()  # chunk_length
            map_length = ds.read4()
            map_end = ds.count + map_length
            seen: set = set()
            while ds.count < map_end:
                entry_start = index_byte_range.start + _RECORD_PREFIX_SIZE + ds.count
                channel_id = ds.read2()
                ds.read8()  # channel offset
                if channel_id in seen:
                    self._report.add_finding(
                        Finding(
                            code="duplicate_message_index_mapping",
                            severity="error",
                            message=(
                                f"channel id {channel_id} appears more than once in "
                                "the chunk index message-index map"
                            ),
                            byte_range=ByteRange(
                                entry_start, entry_start + _U16.size + _U64.size
                            ),
                            chunk_index=ordinal,
                            channel_id=channel_id,
                        )
                    )
                seen.add(channel_id)
        except EndOfFile:
            self._truncated("chunk index map", index_byte_range)

    def _chunk_data_length(self, chunk_byte_range: ByteRange) -> Optional[int]:
        """Length of the compressed records field inside a Chunk record."""
        self._stream.seek(chunk_byte_range.start + _RECORD_PREFIX_SIZE)
        ds = ReadDataStream(self._stream)
        try:
            ds.read8()  # message_start_time
            ds.read8()  # message_end_time
            ds.read8()  # uncompressed_size
            ds.read4()  # uncompressed_crc
            compression_length = ds.read4()
            ds.read(compression_length)
            return ds.read8()
        except EndOfFile:
            return None

    def _offset_points_at_message_index(self, offset: int) -> bool:
        self._stream.seek(offset)
        prefix = self._stream.read(_RECORD_PREFIX_SIZE)
        if len(prefix) < _RECORD_PREFIX_SIZE:
            return False
        opcode, _length = _RECORD_PREFIX.unpack(prefix)
        return opcode == _OPCODE_MESSAGE_INDEX

    def _reconcile_message_indexes(self) -> None:
        report = self._report
        by_offset: Dict[int, Tuple[MessageIndex, ByteRange]] = {}
        for index, byte_range in self._message_indexes:
            if byte_range.start in by_offset:
                report.add_finding(
                    Finding(
                        code="duplicate_message_index_record",
                        severity="error",
                        message=(
                            "a second message index record starts at offset "
                            f"{byte_range.start}, already used by channel "
                            f"{by_offset[byte_range.start][0].channel_id}"
                        ),
                        byte_range=byte_range,
                        channel_id=index.channel_id,
                    )
                )
            else:
                by_offset[byte_range.start] = (index, byte_range)
        referenced_offsets: set = set()
        referenced_offset_owners: Dict[int, Tuple[int, int]] = {}
        for audit in report.chunks:
            if audit.index is None:
                continue
            # Log times were captured during the single chunk ledger pass, so
            # reconciliation never has to decompress a chunk a second time.
            channel_times = (
                audit.observed_channel_log_times if audit.readable else None
            )
            audits: List[MessageIndexAudit] = []
            for channel_id, offset in audit.index.message_index_offsets.items():
                owner = referenced_offset_owners.get(offset)
                if owner is not None and owner != (audit.ordinal, channel_id):
                    report.add_finding(
                        Finding(
                            code="duplicate_message_index_offset",
                            severity="error",
                            message=(
                                f"message index offset {offset} is referenced by both "
                                f"chunk #{owner[0]} channel {owner[1]} and chunk "
                                f"#{audit.ordinal} channel {channel_id}"
                            ),
                            byte_range=audit.index_byte_range,
                            chunk_index=audit.ordinal,
                            channel_id=channel_id,
                        )
                    )
                referenced_offset_owners[offset] = (audit.ordinal, channel_id)
                referenced_offsets.add(offset)
                located = by_offset.get(offset)
                observed_count = audit.observed_channel_counts.get(channel_id)
                if located is None:
                    audits.append(
                        MessageIndexAudit(
                            channel_id=channel_id,
                            verdict=VERDICT_CONTRADICTED,
                            detail=f"no message index record at declared offset {offset}",
                            declared_entry_count=None,
                            observed_message_count=observed_count,
                            message_index_byte_range=None,
                        )
                    )
                    continue
                message_index, message_index_range = located
                declared_count = len(message_index.records)
                if message_index.channel_id != channel_id:
                    audits.append(
                        MessageIndexAudit(
                            channel_id=channel_id,
                            verdict=VERDICT_CONTRADICTED,
                            detail=(
                                "message index record at the declared offset carries "
                                f"channel id {message_index.channel_id}, expected "
                                f"{channel_id}"
                            ),
                            declared_entry_count=declared_count,
                            observed_message_count=observed_count,
                            message_index_byte_range=message_index_range,
                        )
                    )
                    continue
                if not audit.readable:
                    audits.append(
                        MessageIndexAudit(
                            channel_id=channel_id,
                            verdict=VERDICT_INDETERMINATE,
                            detail=(
                                f"index declares {declared_count} entries but the "
                                "chunk is unreadable"
                            ),
                            declared_entry_count=declared_count,
                            observed_message_count=None,
                            message_index_byte_range=message_index_range,
                        )
                    )
                    continue
                assert channel_times is not None
                observed_times = channel_times.get(channel_id, [])
                indexed_times = [ts for ts, _off in message_index.records]
                offsets = [off for _ts, off in message_index.records]
                if declared_count != len(observed_times):
                    verdict = VERDICT_CONTRADICTED
                    detail = (
                        f"index lists {declared_count} entries but the chunk holds "
                        f"{len(observed_times)} messages on this channel"
                    )
                elif indexed_times != sorted(observed_times):
                    verdict = VERDICT_CONTRADICTED
                    detail = (
                        "index timestamps do not match the log times of the messages "
                        "actually recorded in the chunk"
                    )
                elif len(set(offsets)) != len(offsets) or any(
                    off < 0 for off in offsets
                ):
                    verdict = VERDICT_CONTRADICTED
                    detail = "index contains repeated or invalid message offsets"
                else:
                    verdict = VERDICT_CONFIRMED
                    detail = (
                        f"{declared_count} index entries match the recorded messages"
                    )
                audits.append(
                    MessageIndexAudit(
                        channel_id=channel_id,
                        verdict=verdict,
                        detail=detail,
                        declared_entry_count=declared_count,
                        observed_message_count=len(observed_times),
                        message_index_byte_range=message_index_range,
                    )
                )
            report.message_index_audits[audit.ordinal] = audits

        # Message index records that no chunk index points at are only reportable
        # when the file uses chunk indexes at all. A writer can emit message
        # indexes without chunk indexes (separate index-type flags), and those
        # records are simply not addressable rather than contradictory.
        chunk_indexes_present = len(self._chunk_indexes) > 0
        if chunk_indexes_present:
            for offset, (message_index, byte_range) in by_offset.items():
                if offset not in referenced_offsets:
                    report.add_finding(
                        Finding(
                            code="orphan_message_index",
                            severity="error",
                            message=(
                                f"message index record for channel "
                                f"{message_index.channel_id} with "
                                f"{len(message_index.records)} entries is not referenced "
                                "by any chunk index"
                            ),
                            byte_range=byte_range,
                            channel_id=message_index.channel_id,
                        )
                    )

    def _reconcile_statistics(self) -> None:
        report = self._report
        statistics = report.statistics
        if statistics is None:
            return
        assert report.statistics_byte_range is not None
        byte_range = report.statistics_byte_range
        any_unreadable = any(not chunk.readable for chunk in report.chunks) or not (
            report.readable
        )

        report.statistics_verdicts.append(
            self._scalar_claim(
                "message_count",
                observed=report.observed_message_count,
                declared=statistics.message_count,
                byte_range=byte_range,
                bound_mode="count" if any_unreadable else "exact",
            )
        )
        report.statistics_verdicts.append(
            self._scalar_claim(
                "message_start_time",
                observed=report.observed_min_log_time,
                declared=statistics.message_start_time,
                byte_range=byte_range,
                bound_mode="min_time" if any_unreadable else "exact",
            )
        )
        report.statistics_verdicts.append(
            self._scalar_claim(
                "message_end_time",
                observed=report.observed_max_log_time,
                declared=statistics.message_end_time,
                byte_range=byte_range,
                bound_mode="max_time" if any_unreadable else "exact",
            )
        )
        report.statistics_verdicts.append(
            self._scalar_claim(
                "chunk_count",
                observed=len(report.chunks),
                declared=statistics.chunk_count,
                byte_range=byte_range,
                bound_mode="exact",
            )
        )
        report.statistics_verdicts.append(
            self._scalar_claim(
                "channel_count",
                observed=len(report.channels),
                declared=statistics.channel_count,
                byte_range=byte_range,
                bound_mode="exact",
            )
        )
        report.statistics_verdicts.append(
            self._scalar_claim(
                "schema_count",
                observed=len(report.schemas),
                declared=statistics.schema_count,
                byte_range=byte_range,
                bound_mode="exact",
            )
        )

        declared_sum = sum(statistics.channel_message_counts.values())
        if declared_sum != statistics.message_count:
            report.add_finding(
                Finding(
                    code="channel_counts_sum_mismatch",
                    severity="error",
                    message=(
                        "sum of channel_message_counts "
                        f"({declared_sum}) does not equal message_count "
                        f"({statistics.message_count})"
                    ),
                    byte_range=byte_range,
                )
            )

        for verdict in report.statistics_verdicts:
            if verdict.verdict == VERDICT_CONTRADICTED:
                report.add_finding(
                    Finding(
                        code=f"bad_{verdict.name}",
                        severity="error",
                        message=verdict.detail,
                        byte_range=verdict.byte_range,
                        channel_id=verdict.channel_id,
                    )
                )

    def _scalar_claim(
        self,
        name: str,
        observed: Optional[object],
        declared: object,
        byte_range: ByteRange,
        bound_mode: str,
    ) -> ClaimVerdict:
        """Compare one declaration against the ledger.

        ``exact`` demands equality. ``count``, ``min_time`` and ``max_time`` apply
        when bytes are missing: the hidden records can only raise the count, move
        the minimum earlier, or move the maximum later, so a declaration inside
        the proven side is indeterminate rather than confirmed.
        """
        if observed is None:
            return ClaimVerdict(
                name=name,
                verdict=VERDICT_UNVERIFIABLE,
                observed=None,
                declared=declared,
                detail=(
                    f"declared {name}={declared} cannot be checked: no evidence in "
                    "the readable bytes"
                ),
                byte_range=byte_range,
            )
        if bound_mode == "exact":
            if observed == declared:
                return ClaimVerdict(
                    name=name,
                    verdict=VERDICT_CONFIRMED,
                    observed=observed,
                    declared=declared,
                    detail=f"{name}={declared} matches the independent ledger",
                    byte_range=byte_range,
                )
            return ClaimVerdict(
                name=name,
                verdict=VERDICT_CONTRADICTED,
                observed=observed,
                declared=declared,
                detail=(
                    f"declared {name}={declared} contradicts the observed {observed}"
                ),
                byte_range=byte_range,
            )

        consistent: bool
        constraint_text: str
        # Bound-mode claims are always integer counts or nanosecond timestamps.
        declared_int = cast(int, declared)
        observed_int = cast(int, observed)
        if bound_mode == "count":
            consistent = declared_int >= observed_int
            constraint_text = f"proven lower bound is {observed}"
        elif bound_mode == "min_time":
            # The true minimum can only be earlier than the observed minimum.
            consistent = declared_int <= observed_int
            constraint_text = f"observed minimum {observed} is an upper bound on it"
        else:  # max_time: the true maximum can only be later.
            consistent = declared_int >= observed_int
            constraint_text = f"observed maximum {observed} is a lower bound on it"
        if observed == declared:
            return ClaimVerdict(
                name=name,
                verdict=VERDICT_INDETERMINATE,
                observed=observed,
                declared=declared,
                detail=(
                    f"declared {name}={declared} equals the ledger value but hidden "
                    "bytes could still change it; " + constraint_text
                ),
                byte_range=byte_range,
            )
        if not consistent:
            return ClaimVerdict(
                name=name,
                verdict=VERDICT_CONTRADICTED,
                observed=observed,
                declared=declared,
                detail=(
                    f"declared {name}={declared} is impossible given the ledger; "
                    + constraint_text
                ),
                byte_range=byte_range,
            )
        return ClaimVerdict(
            name=name,
            verdict=VERDICT_INDETERMINATE,
            observed=observed,
            declared=declared,
            detail=(
                f"declared {name}={declared} is unprovable but consistent with the "
                + constraint_text
            ),
            byte_range=byte_range,
        )

    def _reconcile_channel_claims(self) -> None:
        report = self._report
        # A channel that messages actually use but no Channel record defines is a
        # structural fault independent of any statistics. The finding points back
        # at the first chunk carrying one of its messages.
        for channel_id in sorted(report.observed_channel_counts):
            if channel_id not in report.channels:
                first_chunk = report.channel_first_chunk.get(channel_id)
                message_range: Optional[ByteRange]
                if first_chunk is not None:
                    message_range = report.chunks[first_chunk].byte_range
                else:
                    message_range = report.channel_first_message_range.get(channel_id)
                report.add_finding(
                    Finding(
                        code="unknown_channel",
                        severity="error",
                        message=(
                            f"channel id {channel_id} is referenced by messages but no "
                            "channel record defines it"
                        ),
                        byte_range=message_range,
                        chunk_index=first_chunk,
                        channel_id=channel_id,
                    )
                )
        statistics = report.statistics
        if statistics is None:
            return
        byte_range: Optional[ByteRange] = report.statistics_byte_range
        any_unreadable = any(not chunk.readable for chunk in report.chunks) or not (
            report.readable
        )
        declared_counts: Dict[int, int] = dict(statistics.channel_message_counts)
        all_channel_ids = set(declared_counts) | set(report.observed_channel_counts)
        entry_ranges = self._statistics_channel_entry_ranges()
        for channel_id in sorted(all_channel_ids):
            observed = report.observed_channel_counts.get(channel_id)
            declared = declared_counts.get(channel_id)
            entry_range = entry_ranges.get(channel_id) or byte_range
            if declared is None:
                verdict = ClaimVerdict(
                    name="channel_message_count",
                    verdict=VERDICT_CONTRADICTED,
                    observed=observed,
                    declared=None,
                    detail=(
                        f"{observed} messages were recorded on channel {channel_id} "
                        "but statistics carry no count for it"
                    ),
                    byte_range=byte_range,
                    channel_id=channel_id,
                )
            elif observed is None:
                verdict = ClaimVerdict(
                    name="channel_message_count",
                    verdict=(
                        VERDICT_INDETERMINATE
                        if any_unreadable
                        else VERDICT_CONTRADICTED
                    ),
                    observed=None,
                    declared=declared,
                    detail=(
                        f"statistics declare {declared} messages on channel "
                        f"{channel_id} but no message on that channel appears in the "
                        "readable bytes"
                        + (
                            "; the channel may live inside an unreadable chunk"
                            if any_unreadable
                            else ""
                        )
                    ),
                    byte_range=entry_range,
                    channel_id=channel_id,
                )
            elif observed == declared:
                verdict = ClaimVerdict(
                    name="channel_message_count",
                    verdict=VERDICT_CONFIRMED,
                    observed=observed,
                    declared=declared,
                    detail=f"channel {channel_id}: {declared} messages match the ledger",
                    byte_range=entry_range,
                    channel_id=channel_id,
                )
            else:
                detail = (
                    f"channel {channel_id}: statistics declare {declared} messages "
                    f"but the ledger proves {observed}"
                )
                if any_unreadable and declared > observed:
                    verdict = ClaimVerdict(
                        name="channel_message_count",
                        verdict=VERDICT_INDETERMINATE,
                        observed=observed,
                        declared=declared,
                        detail=detail + "; hidden bytes could account for the difference",
                        byte_range=entry_range,
                        channel_id=channel_id,
                    )
                else:
                    verdict = ClaimVerdict(
                        name="channel_message_count",
                        verdict=VERDICT_CONTRADICTED,
                        observed=observed,
                        declared=declared,
                        detail=detail,
                        byte_range=entry_range,
                        channel_id=channel_id,
                    )
            report.channel_verdicts.append(verdict)
            if verdict.verdict == VERDICT_CONTRADICTED:
                report.add_finding(
                    Finding(
                        code="bad_channel_message_count",
                        severity="error",
                        message=verdict.detail,
                        byte_range=entry_range,
                        channel_id=channel_id,
                    )
                )
            if channel_id not in report.channels:
                report.add_finding(
                    Finding(
                        code="unknown_channel",
                        severity="error",
                        message=(
                            f"channel id {channel_id} appears in statistics but no "
                            "channel record defines it"
                        ),
                        byte_range=entry_range,
                        channel_id=channel_id,
                    )
                )

    def _statistics_channel_entry_ranges(self) -> Dict[int, ByteRange]:
        """Map each channel id to the byte range of its 10-byte (id, count)
        entry inside the statistics record, so channel findings point at the
        exact mapping bytes."""
        byte_range = self._report.statistics_byte_range
        if byte_range is None:
            return {}
        body = self._read_body(byte_range)
        if body is None:
            return {}
        ranges: Dict[int, ByteRange] = {}
        try:
            ds = ReadDataStream(BytesIO(body))
            ds.read8()  # message_count
            ds.read2()  # schema_count
            ds.read4()  # channel_count
            ds.read4()  # attachment_count
            ds.read4()  # metadata_count
            ds.read4()  # chunk_count
            ds.read8()  # message_start_time
            ds.read8()  # message_end_time
            mapping_length = ds.read4()
            mapping_end = ds.count + mapping_length
            while ds.count < mapping_end:
                entry_body_offset = ds.count
                entry_channel_id = ds.read2()
                ds.read8()  # channel message count
                entry_start = (
                    byte_range.start + _RECORD_PREFIX_SIZE + entry_body_offset
                )
                ranges[entry_channel_id] = ByteRange(entry_start, entry_start + 10)
        except EndOfFile:
            return ranges
        return ranges

    def _compute_bounds(self) -> None:
        report = self._report
        report.proven_message_count_lower_bound = report.observed_message_count
        report.proven_min_log_time_bound = report.observed_min_log_time
        if report.readable and all(chunk.readable for chunk in report.chunks):
            report.proven_max_log_time_bound = report.observed_max_log_time
        else:
            # With missing bytes the true maximum is unknowable; leave the bound
            # open rather than quoting a declaration as fact.
            report.proven_max_log_time_bound = None


__all__ = [
    "AuditError",
    "AuditReport",
    "ByteRange",
    "ChunkAudit",
    "ClaimVerdict",
    "Finding",
    "MessageIndexAudit",
    "audit_mcap",
    "VERDICT_CONFIRMED",
    "VERDICT_CONTRADICTED",
    "VERDICT_INDETERMINATE",
    "VERDICT_UNVERIFIABLE",
    "VERDICT_UNCHECKED",
]
