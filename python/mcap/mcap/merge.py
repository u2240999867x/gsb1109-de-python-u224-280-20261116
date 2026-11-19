"""Merge several MCAP files, or the files of a capture directory, into one MCAP.

The merge keeps the origin of every declaration instead of flattening files into one
anonymous namespace:

* schema and channel ids are renumbered from 1, independently;
* a schema is shared only when its declaration is byte-for-byte equivalent (same name,
  encoding and data);
* every source channel gets its own output channel, even when another file declares an
  equivalent channel.  That one-to-one mapping is what lets every merged message be
  traced back to its original channel, and through it to the original file profile;
* files with different header profiles are never written into one output file.

The mapping between output ids and ``(source file, original id)`` pairs is returned by
the merge functions and is also written to the output as a metadata record named
:data:`MERGE_METADATA_NAME`.  Message payloads are copied without decoding, so channels
with an unknown profile or encodings keep their original bytes.

Example::

    from mcap.merge import merge_directory

    result = merge_directory("capture/", "capture/merged.mcap")
    print(result.origin_for_channel(1))

To merge a directory that may contain more than one profile, write one merged file per
profile into a destination directory::

    from mcap.merge import merge_to_directory

    for result in merge_to_directory(["capture-a/", "capture-b/"], "merged/"):
        print(result.profile, result.output_path)
"""

import base64
import hashlib
import json
import os
import struct
import tempfile
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    IO,
    Any,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from .data_stream import ReadDataStream
from .exceptions import McapError
from .opcode import Opcode
from .reader import make_reader
from .records import (
    Attachment,
    Channel,
    Header,
    Message,
    MessageIndex,
    Metadata,
    Schema,
)
from .stream_reader import StreamReader
from .summary import Summary
from .writer import LIBRARY_IDENTIFIER, CompressionType, IndexType, Writer

# Opcode and record length prefix, used to check message-index records in place.
_RECORD_PREFIX = struct.Struct("<BQ")
_RECORD_PREFIX_SIZE = _RECORD_PREFIX.size

PathLike = Union[str, os.PathLike]

#: Metadata record written by the merge functions.  Its ``manifest`` entry is a JSON
#: document describing every source file and every output schema/channel.
MERGE_METADATA_NAME = "mcap.merge.manifest"

#: Version of the manifest document.
MERGE_MANIFEST_VERSION = 1

#: Rules used to decide whether two declarations are equivalent.
EQUIVALENCE_RULES = {
    "schema": "same name, same encoding and identical schema data bytes",
    "channel": (
        "same topic, same message_encoding, same metadata map and an equivalent schema"
    ),
    "channel_identity": (
        "each source file channel keeps a distinct output channel so that every message "
        "can be traced back to its original channel and profile"
    ),
    "profile": "files with different header profiles are never merged into one file",
}


class MergeError(McapError, ValueError):
    """Base class for merge failures."""


class ProfileMismatchError(MergeError):
    """Raised when inputs with different header profiles would be put in one file."""

    def __init__(self, profiles_by_path: Mapping[PathLike, str]):
        self.profiles_by_path = dict(profiles_by_path)
        details = ", ".join(
            f"{path!s}={profile!r}" for path, profile in self.profiles_by_path.items()
        )
        profiles = sorted(set(self.profiles_by_path.values()))
        super().__init__(
            "cannot merge MCAP files with different profiles into one output "
            f"({details}); write one output per profile with merge_to_directory() "
            f"or merge_capture(). Profiles: {profiles}"
        )


class DeclarationConflictError(MergeError):
    """Raised when one file reuses a schema or channel id for another declaration."""

    def __init__(self, path: PathLike, kind: str, record_id: int):
        super().__init__(
            f"{path!s} declares {kind} id {record_id} more than once with different "
            "contents; MCAP ids must identify one declaration within a file"
        )


@dataclass(frozen=True)
class SourceInfo:
    """A file that contributed to a merge."""

    index: int
    path: str
    profile: str
    library: str
    schema_count: int
    channel_count: int
    message_count: int = 0


@dataclass(frozen=True)
class SchemaOrigin:
    """One source schema declaration and the output schema it was mapped to."""

    source_index: int
    source_path: str
    profile: str
    schema_id: int
    output_schema_id: int
    name: str
    encoding: str
    data: bytes
    equivalence_key: str


@dataclass(frozen=True)
class ChannelOrigin:
    """One source channel declaration and the output channel it was mapped to."""

    source_index: int
    source_path: str
    profile: str
    channel_id: int
    output_channel_id: int
    topic: str
    message_encoding: str
    metadata: Mapping[str, str]
    schema_id: int
    output_schema_id: int
    equivalence_key: str


@dataclass
class MergeResult:
    """The outcome of one merged output file.

    :ivar output_path: path of the written MCAP.
    :ivar profile: the header profile of the output.
    :ivar sources: one entry per input file, in merge order.
    :ivar schemas: maps ``(source_index, source_schema_id)`` to :class:`SchemaOrigin`.
    :ivar channels: maps ``(source_index, source_channel_id)`` to
        :class:`ChannelOrigin`.
    :ivar message_count: number of merged messages.
    :ivar manifest: the JSON-serializable manifest written to the output.
    """

    output_path: Path
    profile: str
    sources: List[SourceInfo]
    schemas: Dict[Tuple[int, int], SchemaOrigin]
    channels: Dict[Tuple[int, int], ChannelOrigin]
    message_count: int
    manifest: Dict[str, Any] = field(repr=False)

    def origin_for_channel(self, output_channel_id: int) -> ChannelOrigin:
        """Return the source channel behind an output channel id.

        Output channels are one-to-one with source channels, so this is unambiguous.
        """
        for origin in self.channels.values():
            if origin.output_channel_id == output_channel_id:
                return origin
        raise KeyError(f"output channel id {output_channel_id} was not produced")

    def origins_for_schema(self, output_schema_id: int) -> List[SchemaOrigin]:
        """Return every source schema declaration that shares an output schema id."""
        return [
            origin
            for origin in self.schemas.values()
            if origin.output_schema_id == output_schema_id
        ]

    def source_for_channel(self, output_channel_id: int) -> SourceInfo:
        """Return the source file behind an output channel id."""
        origin = self.origin_for_channel(output_channel_id)
        for source in self.sources:
            if source.index == origin.source_index:
                return source
        raise KeyError(f"no source recorded for index {origin.source_index}")

    def profile_for_channel(self, output_channel_id: int) -> str:
        """Return the original profile behind an output channel id."""
        return self.origin_for_channel(output_channel_id).profile

    def channel_ids_for_source(self, source_index: int) -> List[int]:
        """Return output channel ids belonging to one source file, in source id order."""
        origins = [
            origin
            for origin in self.channels.values()
            if origin.source_index == source_index
        ]
        origins.sort(key=lambda origin: origin.channel_id)
        return [origin.output_channel_id for origin in origins]


@dataclass
class _SourceDeclarations:
    index: int
    path: Path
    header: Header
    # Insertion order follows the first declaration in the file.
    schemas: "OrderedDict[int, Schema]"
    channels: "OrderedDict[int, Channel]"

    @property
    def profile(self) -> str:
        return self.header.profile


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _schema_key(schema: Schema) -> str:
    """The explicit schema equivalence rule, as a stable digest.

    The id is deliberately excluded: ids only have meaning within one file.
    """
    return _digest(
        {
            "name": schema.name,
            "encoding": schema.encoding,
            "data": base64.b64encode(bytes(schema.data)).decode("ascii"),
        }
    )


def _channel_key(channel: Channel, schema_key: Optional[str], profile: str) -> str:
    """The explicit channel equivalence rule, as a stable digest.

    The channel id and the concrete schema id are excluded; equivalent schemas (and
    therefore channels) may carry different per-file ids.
    """
    return _digest(
        {
            "profile": profile,
            "topic": channel.topic,
            "message_encoding": channel.message_encoding,
            "metadata": dict(sorted(channel.metadata.items())),
            "schema": schema_key,
        }
    )


def _same_schema(left: Schema, right: Schema) -> bool:
    return (
        left.name == right.name
        and left.encoding == right.encoding
        and bytes(left.data) == bytes(right.data)
    )


def _same_channel(left: Channel, right: Channel) -> bool:
    return (
        left.topic == right.topic
        and left.message_encoding == right.message_encoding
        and dict(left.metadata) == dict(right.metadata)
        and left.schema_id == right.schema_id
    )


def _read_declarations(path: Path, index: int) -> _SourceDeclarations:
    """Read one file's header, schemas and channels without decoding messages.

    Files with a summary are read through its index (header, then the repeated schema
    and channel records); files without one are scanned linearly, which also validates
    that no id is reused for a conflicting declaration.
    """
    header: Optional[Header] = None
    schemas: "OrderedDict[int, Schema]" = OrderedDict()
    channels: "OrderedDict[int, Channel]" = OrderedDict()

    def add_schema(schema: Schema) -> None:
        if schema.id == 0:
            raise DeclarationConflictError(path, "schema", 0)
        previous = schemas.get(schema.id)
        if previous is not None and not _same_schema(previous, schema):
            raise DeclarationConflictError(path, "schema", schema.id)
        schemas[schema.id] = schema

    def add_channel(channel: Channel) -> None:
        previous = channels.get(channel.id)
        if previous is not None and not _same_channel(previous, channel):
            raise DeclarationConflictError(path, "channel", channel.id)
        channels[channel.id] = channel

    with open(path, "rb") as stream:
        reader = make_reader(stream)
        header = reader.get_header()
        summary = reader.get_summary()
        stats = None if summary is None else summary.statistics
        # A summary that repeats fewer declarations than the statistics count (for
        # example repeat_schemas=False) does not describe the whole file; scanning is
        # then the only lossless option.
        complete_summary = (
            summary is not None
            and stats is not None
            and len(summary.schemas) == stats.schema_count
            and len(summary.channels) == stats.channel_count
        )
        if complete_summary and summary is not None:
            for schema_id in sorted(summary.schemas):
                add_schema(summary.schemas[schema_id])
            for channel_id in sorted(summary.channels):
                add_channel(summary.channels[channel_id])
        else:
            stream.seek(0)
            for record in StreamReader(stream).records:
                if isinstance(record, Schema):
                    add_schema(record)
                elif isinstance(record, Channel):
                    add_channel(record)
    for channel in channels.values():
        if channel.schema_id != 0 and channel.schema_id not in schemas:
            raise MergeError(
                f"{path!s} channel id {channel.id} references missing schema "
                f"id {channel.schema_id}"
            )
    return _SourceDeclarations(
        index=index,
        path=path,
        header=header,
        schemas=schemas,
        channels=channels,
    )


def _indexed_message_count(stream: IO[bytes], summary: Summary) -> Optional[int]:
    """Count the messages reachable through the chunk indexes, or ``None`` if an index
    record cannot be parsed."""
    offsets: set = set()
    total = 0
    for chunk_index in summary.chunk_indexes:
        for offset in chunk_index.message_index_offsets.values():
            if offset in offsets:
                continue
            offsets.add(offset)
            try:
                stream.seek(offset)
                opcode, _ = _RECORD_PREFIX.unpack(stream.read(_RECORD_PREFIX_SIZE))
                if opcode != int(Opcode.MESSAGE_INDEX):
                    return None
                index = MessageIndex.read(ReadDataStream(stream))
            except (ValueError, struct.error, McapError):
                return None
            total += len(index.records)
    return total


def _indexes_all_messages(stream: IO[bytes], summary: Optional[Summary]) -> bool:
    """Whether a seeking read proves it reaches every message in the file.

    A summary with chunk indexes but no statistics cannot prove it, and statistics that
    disagree with the indexed count mean some messages live outside chunks.  Both cases
    need a linear scan: a seeking reader would silently skip those messages.
    """
    if summary is None:
        return False
    if not summary.chunk_indexes or summary.statistics is None:
        return False
    indexed = _indexed_message_count(stream, summary)
    return indexed is not None and indexed == summary.statistics.message_count


def _is_mcap_name(path: Path, pattern: str) -> bool:
    if pattern == "*.mcap":
        return path.suffix.lower() == ".mcap"
    return path.match(pattern)


def _expand_inputs(
    inputs: Union[PathLike, Iterable[PathLike]],
    *,
    pattern: str = "*.mcap",
    recursive: bool = False,
    exclude: Optional[Iterable[PathLike]] = None,
) -> List[Path]:
    if isinstance(inputs, (str, os.PathLike)):
        items: Iterable[PathLike] = [inputs]
    else:
        items = inputs

    excluded = {Path(item).resolve() for item in (exclude or ())}

    def is_excluded(path: Path) -> bool:
        resolved = path.resolve()
        return any(
            resolved == candidate or candidate in resolved.parents
            for candidate in excluded
        )

    files: List[Path] = []
    seen: set = set()
    for item in items:
        path = Path(item)
        if path.is_dir():
            iterator = path.rglob(pattern) if recursive else path.glob(pattern)
            candidates = [
                candidate
                for candidate in iterator
                if candidate.is_file()
                and _is_mcap_name(candidate, pattern)
                and not is_excluded(candidate)
            ]
            candidates.sort(key=lambda candidate: str(candidate))
        else:
            # An explicitly named input is never silently dropped: _prepare reports it
            # if it collides with the output path.
            candidates = [path]
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            files.append(candidate)
    return files


def _output_file(output: Path) -> Path:
    if output.exists() and output.is_dir():
        return output / "merged.mcap"
    if str(output).endswith(os.sep):
        return output / "merged.mcap"
    if output.suffix == "":
        return output / "merged.mcap"
    return output


def _safe_profile_name(profile: str) -> str:
    if not profile:
        return "unknown"
    safe = "".join(
        character if character.isalnum() or character in "-_." else "_"
        for character in profile
    )
    return safe or "unknown"


def _unique_path(
    directory: Path,
    stem: str,
    suffix: str,
    used: set,
    avoid: Optional[Iterable[Path]] = None,
) -> Path:
    """Return a deterministic output path, disambiguating only names used in this run.

    Existing outputs are replaced: writing the same merge twice must land on the same
    path rather than accumulating ``-2`` suffixes that a later run would merge as input.
    Input paths are never chosen, even when they happen to share an output's name.
    """
    forbidden = {path.resolve() for path in (avoid or ())}
    candidate = directory / f"{stem}{suffix}"
    counter = 2
    while candidate.name in used or candidate.resolve() in forbidden:
        candidate = directory / f"{stem}-{counter}{suffix}"
        counter += 1
    used.add(candidate.name)
    return candidate


def _prepare(
    inputs: List[Path],
    *,
    output_path: Optional[Path] = None,
    expected_profile: Optional[str] = None,
    allow_multiple_profiles: bool = False,
) -> List[_SourceDeclarations]:
    if not inputs:
        raise ValueError("no MCAP input files were found")
    output_resolved = output_path.resolve() if output_path is not None else None
    resolved_inputs: List[Path] = []
    seen: set = set()
    for path in inputs:
        if not path.is_file():
            raise FileNotFoundError(f"MCAP input not found: {path!s}")
        resolved = path.resolve()
        if output_resolved is not None and resolved == output_resolved:
            raise ValueError(f"output path {path!s} is also an input")
        if resolved in seen:
            continue
        seen.add(resolved)
        resolved_inputs.append(path)
    if not resolved_inputs:
        raise ValueError("no MCAP input files were found")

    declarations = [
        _read_declarations(path, index) for index, path in enumerate(resolved_inputs)
    ]
    if expected_profile is not None:
        mismatched = {
            source.path: source.profile
            for source in declarations
            if source.profile != expected_profile
        }
        if mismatched:
            raise ProfileMismatchError(mismatched)
    profiles = {source.profile for source in declarations}
    if len(profiles) > 1 and not allow_multiple_profiles:
        raise ProfileMismatchError(
            {source.path: source.profile for source in declarations}
        )
    return declarations


def _register_declarations(
    writer: Writer,
    declarations: Sequence[_SourceDeclarations],
    *,
    coalesce_schemas: bool,
) -> Tuple[
    Dict[Tuple[int, int], SchemaOrigin],
    Dict[Tuple[int, int], ChannelOrigin],
]:
    schema_key_to_output: Dict[str, int] = {}
    schema_origins: Dict[Tuple[int, int], SchemaOrigin] = {}
    channel_origins: Dict[Tuple[int, int], ChannelOrigin] = {}

    for source in declarations:
        for schema_id in sorted(source.schemas):
            schema = source.schemas[schema_id]
            key = _schema_key(schema)
            if coalesce_schemas and key in schema_key_to_output:
                output_id = schema_key_to_output[key]
            else:
                output_id = writer.register_schema(
                    name=schema.name,
                    encoding=schema.encoding,
                    data=bytes(schema.data),
                )
                schema_key_to_output[key] = output_id
            schema_origins[(source.index, schema_id)] = SchemaOrigin(
                source_index=source.index,
                source_path=str(source.path),
                profile=source.profile,
                schema_id=schema_id,
                output_schema_id=output_id,
                name=schema.name,
                encoding=schema.encoding,
                data=bytes(schema.data),
                equivalence_key=key,
            )

    for source in declarations:
        for channel_id in sorted(source.channels):
            channel = source.channels[channel_id]
            if channel.schema_id == 0:
                schema_key: Optional[str] = None
                output_schema_id = 0
            else:
                schema_key = _schema_key(source.schemas[channel.schema_id])
                output_schema_id = schema_origins[
                    (source.index, channel.schema_id)
                ].output_schema_id
            key = _channel_key(channel, schema_key, source.profile)
            # Every source channel deliberately keeps its own output channel: that
            # one-to-one mapping is what makes each merged message traceable back
            # to its original channel and profile.
            output_channel_id = writer.register_channel(
                topic=channel.topic,
                message_encoding=channel.message_encoding,
                schema_id=output_schema_id,
                metadata=dict(channel.metadata),
            )
            channel_origins[(source.index, channel_id)] = ChannelOrigin(
                source_index=source.index,
                source_path=str(source.path),
                profile=source.profile,
                channel_id=channel_id,
                output_channel_id=output_channel_id,
                topic=channel.topic,
                message_encoding=channel.message_encoding,
                metadata=dict(channel.metadata),
                schema_id=channel.schema_id,
                output_schema_id=output_schema_id,
                equivalence_key=key,
            )

    return schema_origins, channel_origins


def _copy_metadata_and_attachments(
    writer: Writer, declarations: Sequence[_SourceDeclarations]
) -> None:
    for source in declarations:
        with open(source.path, "rb") as stream:
            reader = make_reader(stream)
            summary = reader.get_summary()
            stats = None if summary is None else summary.statistics
            # A summary only reaches the records it indexes.  When its statistics show
            # that some metadata or attachments were left out of those indexes, a
            # seeking read would silently drop them.
            complete_summary = (
                summary is not None
                and stats is not None
                and len(summary.metadata_indexes) == stats.metadata_count
                and len(summary.attachment_indexes) == stats.attachment_count
            )
            if complete_summary and summary is not None:
                for record in reader.iter_metadata():
                    writer.add_metadata(record.name, dict(record.metadata))
                for attachment in reader.iter_attachments():
                    writer.add_attachment(
                        create_time=attachment.create_time,
                        log_time=attachment.log_time,
                        name=attachment.name,
                        media_type=attachment.media_type,
                        data=bytes(attachment.data),
                    )
                continue
            # Without a complete index, scan the file.  `emit_chunks` skips chunk
            # payloads, which cannot hold either record type, so this stays bounded.
            stream.seek(0)
            for record in StreamReader(stream, emit_chunks=True).records:
                if isinstance(record, Metadata):
                    writer.add_metadata(record.name, dict(record.metadata))
                elif isinstance(record, Attachment):
                    writer.add_attachment(
                        create_time=record.create_time,
                        log_time=record.log_time,
                        name=record.name,
                        media_type=record.media_type,
                        data=bytes(record.data),
                    )


def _linear_message_iterator(stream: IO[bytes]) -> List[Tuple[Any, Any, Message]]:
    """Return ``(schema, channel, message)`` from a sequential scan of one MCAP.

    Used when the summary cannot prove every message is reachable through the chunk
    indexes.  Records inside chunks are parsed (`emit_chunks=False`, the default), so
    chunk-embedded schema and channel declarations resolve exactly as in a normal read.
    The messages are materialized and sorted because a log-time ordered merge needs the
    whole input's order first, mirroring how a non-seeking reader sorts.
    """
    schemas: Dict[int, Schema] = {}
    channels: Dict[int, Channel] = {}
    messages: List[Tuple[Any, Any, Message]] = []
    for record in StreamReader(stream).records:
        if isinstance(record, Schema):
            schemas[record.id] = record
        elif isinstance(record, Channel):
            if record.schema_id != 0 and record.schema_id not in schemas:
                raise MergeError(
                    f"channel id {record.id} references missing schema id "
                    f"{record.schema_id}"
                )
            channels[record.id] = record
        elif isinstance(record, Message):
            channel = channels.get(record.channel_id)
            if channel is None:
                raise MergeError(
                    f"message references unknown channel id {record.channel_id}"
                )
            schema = None if channel.schema_id == 0 else schemas[channel.schema_id]
            messages.append((schema, channel, record))
    messages.sort(key=lambda entry: entry[2].log_time)
    return messages


def _merge_messages(
    writer: Writer,
    declarations: Sequence[_SourceDeclarations],
    channel_origins: Mapping[Tuple[int, int], ChannelOrigin],
) -> Dict[int, int]:
    """K-way merge messages by log time, returning per-source message counts.

    Inputs whose summary does not prove it indexes every message are read linearly in
    stored order (and sorted if needed) rather than through the chunk indexes, which
    would silently drop messages stored outside chunks.
    """
    import heapq

    streams: List[IO[bytes]] = []
    iterators: Dict[int, Any] = {}
    counts: Dict[int, int] = {source.index: 0 for source in declarations}
    try:
        for source in declarations:
            stream = open(source.path, "rb")
            streams.append(stream)
            reader = make_reader(stream)
            if not _indexes_all_messages(stream, reader.get_summary()):
                stream.seek(0)
                readers = iter(_linear_message_iterator(stream))
            else:
                readers = reader.iter_messages(log_time_order=True)
            iterators[source.index] = readers

        heap: List[Tuple[int, int, int, Message]] = []
        ordinal = 0
        for source in declarations:
            try:
                _, _, message = next(iterators[source.index])
            except StopIteration:
                continue
            heap.append((message.log_time, source.index, ordinal, message))
            ordinal += 1
        heapq.heapify(heap)

        while heap:
            _, source_index, _, message = heapq.heappop(heap)
            origin = channel_origins.get((source_index, message.channel_id))
            if origin is None:
                raise MergeError(
                    f"message references unknown channel id {message.channel_id}"
                )
            writer.add_message(
                channel_id=origin.output_channel_id,
                log_time=message.log_time,
                data=message.data,
                publish_time=message.publish_time,
                sequence=message.sequence,
            )
            counts[source_index] += 1
            try:
                _, _, following = next(iterators[source_index])
            except StopIteration:
                continue
            heapq.heappush(heap, (following.log_time, source_index, ordinal, following))
            ordinal += 1
    finally:
        for stream in streams:
            stream.close()
    return counts


def _equivalence_groups(
    items: Iterable[Tuple[Tuple[int, int], str]], kind: str
) -> List[Dict[str, Any]]:
    groups: "OrderedDict[str, List[Dict[str, int]]]" = OrderedDict()
    for (source_index, record_id), key in items:
        groups.setdefault(key, []).append(
            {"source_index": source_index, f"source_{kind}_id": record_id}
        )
    return [
        {"equivalence_key": key, "declarations": declarations}
        for key, declarations in groups.items()
        if len(declarations) > 1
    ]


def _build_manifest(
    *,
    profile: str,
    sources: Sequence[SourceInfo],
    schema_origins: Mapping[Tuple[int, int], SchemaOrigin],
    channel_origins: Mapping[Tuple[int, int], ChannelOrigin],
) -> Dict[str, Any]:
    schemas = []
    for origin in schema_origins.values():
        schemas.append(
            {
                "source_index": origin.source_index,
                "source_path": origin.source_path,
                "profile": origin.profile,
                "source_schema_id": origin.schema_id,
                "output_schema_id": origin.output_schema_id,
                "name": origin.name,
                "encoding": origin.encoding,
                "data_base64": base64.b64encode(origin.data).decode("ascii"),
                "equivalence_key": origin.equivalence_key,
            }
        )
    channels = []
    for origin in channel_origins.values():
        channels.append(
            {
                "source_index": origin.source_index,
                "source_path": origin.source_path,
                "profile": origin.profile,
                "source_channel_id": origin.channel_id,
                "output_channel_id": origin.output_channel_id,
                "topic": origin.topic,
                "message_encoding": origin.message_encoding,
                "metadata": dict(origin.metadata),
                "source_schema_id": origin.schema_id,
                "output_schema_id": origin.output_schema_id,
                "equivalence_key": origin.equivalence_key,
            }
        )
    return {
        "version": MERGE_MANIFEST_VERSION,
        "library": LIBRARY_IDENTIFIER,
        "profile": profile,
        "equivalence_rules": EQUIVALENCE_RULES,
        "sources": [
            {
                "index": source.index,
                "path": source.path,
                "profile": source.profile,
                "library": source.library,
                "schema_count": source.schema_count,
                "channel_count": source.channel_count,
                "message_count": source.message_count,
            }
            for source in sources
        ],
        "schemas": schemas,
        "channels": channels,
        # Explicit association of declarations that are equivalent but were kept
        # distinct so that their file domain and per-message origin stay intact.
        "schema_equivalence": _equivalence_groups(
            ((key, origin.equivalence_key) for key, origin in schema_origins.items()),
            "schema",
        ),
        "channel_equivalence": _equivalence_groups(
            ((key, origin.equivalence_key) for key, origin in channel_origins.items()),
            "channel",
        ),
    }


def _write_merge(
    declarations: Sequence[_SourceDeclarations],
    output: Path,
    *,
    chunk_size: int,
    compression: CompressionType,
    index_types: Optional[IndexType] = None,
    use_chunking: bool = True,
    coalesce_schemas: bool = True,
    enable_crcs: bool = True,
) -> MergeResult:
    output = Path(output)
    if output.parent and not output.parent.exists():
        output.parent.mkdir(parents=True, exist_ok=True)

    profile = declarations[0].profile
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{output.stem}.", suffix=".mcap.tmp", dir=str(output.parent)
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        writer_kwargs: Dict[str, Any] = {
            "chunk_size": chunk_size,
            "compression": compression,
            "use_chunking": use_chunking,
            "enable_crcs": enable_crcs,
        }
        if index_types is not None:
            writer_kwargs["index_types"] = index_types
        with open(temporary, "wb") as stream:
            writer = Writer(stream, **writer_kwargs)
            writer.start(profile=profile, library=LIBRARY_IDENTIFIER)
            schema_origins, channel_origins = _register_declarations(
                writer,
                declarations,
                coalesce_schemas=coalesce_schemas,
            )
            _copy_metadata_and_attachments(writer, declarations)
            counts = _merge_messages(writer, declarations, channel_origins)

            sources = [
                SourceInfo(
                    index=source.index,
                    path=str(source.path),
                    profile=source.profile,
                    library=source.header.library,
                    schema_count=len(source.schemas),
                    channel_count=len(source.channels),
                    message_count=counts[source.index],
                )
                for source in declarations
            ]
            manifest = _build_manifest(
                profile=profile,
                sources=sources,
                schema_origins=schema_origins,
                channel_origins=channel_origins,
            )
            writer.add_metadata(
                MERGE_METADATA_NAME,
                {
                    "version": str(MERGE_MANIFEST_VERSION),
                    "library": LIBRARY_IDENTIFIER,
                    "manifest": _canonical_json(manifest),
                },
            )
            writer.finish()
        os.replace(temporary, output)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise

    return MergeResult(
        output_path=output,
        profile=profile,
        sources=sources,
        schemas=schema_origins,
        channels=channel_origins,
        message_count=sum(counts.values()),
        manifest=manifest,
    )


def merge_mcaps(
    inputs: Union[PathLike, Iterable[PathLike]],
    output: PathLike,
    *,
    pattern: str = "*.mcap",
    recursive: bool = False,
    chunk_size: int = 1024 * 1024,
    compression: CompressionType = CompressionType.ZSTD,
    index_types: Optional[IndexType] = None,
    use_chunking: bool = True,
    coalesce_schemas: bool = True,
    enable_crcs: bool = True,
    profile: Optional[str] = None,
) -> MergeResult:
    """Merge MCAP files into one output, ordered by log time.

    :param inputs: an MCAP file, a directory containing MCAP files, or an iterable of
        files/directories.  Directories are expanded using ``pattern``.
    :param output: the output ``.mcap`` path, or a directory.  A directory receives
        ``merged.mcap``.
    :param pattern: glob used when expanding a directory.
    :param recursive: recurse into subdirectories when expanding a directory.
    :param chunk_size: output chunk size in bytes.
    :param compression: output chunk compression.
    :param index_types: optional :class:`~mcap.writer.IndexType` mask.
    :param use_chunking: write messages in chunks.
    :param coalesce_schemas: share an output schema id when declarations are exactly
        equivalent.  Source schema ids are still recorded in the manifest.
    :param enable_crcs: write CRCs in the output.
    :param profile: optional required profile.  All inputs must match it; inputs with
        different profiles are rejected instead of being mixed.

    Channels are always renumbered one-to-one with the source channels (see
    :class:`MergeResult`), so the equivalence relation between channels is recorded in
    the manifest rather than collapsing them into one id.

    :raises ProfileMismatchError: if the inputs have different header profiles.
    :raises DeclarationConflictError: if one input file reuses an id for a different
        declaration.
    """
    output_path = _output_file(Path(output))
    expanded = _expand_inputs(
        inputs, pattern=pattern, recursive=recursive, exclude=[output_path]
    )
    declarations = _prepare(expanded, output_path=output_path, expected_profile=profile)
    return _write_merge(
        declarations,
        output_path,
        chunk_size=chunk_size,
        compression=compression,
        index_types=index_types,
        use_chunking=use_chunking,
        coalesce_schemas=coalesce_schemas,
        enable_crcs=enable_crcs,
    )


def merge_to_directory(
    inputs: Union[PathLike, Iterable[PathLike]],
    output_directory: PathLike,
    *,
    pattern: str = "*.mcap",
    recursive: bool = False,
    output_name: str = "merged",
    chunk_size: int = 1024 * 1024,
    compression: CompressionType = CompressionType.ZSTD,
    index_types: Optional[IndexType] = None,
    use_chunking: bool = True,
    coalesce_schemas: bool = True,
    enable_crcs: bool = True,
) -> List[MergeResult]:
    """Merge inputs into a capture directory, one file per header profile.

    Profiles are never combined in one output.  When all inputs share a profile the
    result is ``<output_directory>/<output_name>.mcap``.  With several profiles the
    outputs are named ``<output_name>-<profile>.mcap`` (an empty profile becomes
    ``unknown``), and every result retains that profile in its header.

    Returns one :class:`MergeResult` per profile, in first-seen input order.
    """
    directory = Path(output_directory)
    directory.mkdir(parents=True, exist_ok=True)
    expanded = _expand_inputs(inputs, pattern=pattern, recursive=recursive)
    if not expanded:
        raise ValueError("no MCAP input files were found")

    # Read declarations once, then group by profile without ever mixing them.
    declarations = _prepare(expanded, allow_multiple_profiles=True)

    # Outputs from an earlier run may sit beside the inputs.  Every name this function
    # can produce starts with `output_name`, so those files are reserved and never
    # treated as sources; this also covers disambiguated names from a previous run.
    prefix = f"{output_name}"
    declarations = [
        source
        for source in declarations
        if not (
            source.path.parent.resolve() == directory.resolve()
            and source.path.name.startswith(prefix)
            and source.path.suffix.lower() == ".mcap"
        )
    ]
    if not declarations:
        raise ValueError("no MCAP input files were found")

    by_profile: "OrderedDict[str, List[_SourceDeclarations]]" = OrderedDict()
    for source in declarations:
        by_profile.setdefault(source.profile, []).append(source)
    for sources in by_profile.values():
        sources.sort(key=lambda source: source.index)

    used_names: set = set()
    input_paths = [source.path for source in declarations]
    results: List[MergeResult] = []
    for profile_name, sources in by_profile.items():
        if len(by_profile) > 1:
            stem = f"{output_name}-{_safe_profile_name(profile_name)}"
        else:
            stem = output_name
        output_path = _unique_path(
            directory, stem, ".mcap", used_names, avoid=input_paths
        )
        results.append(
            _write_merge(
                sources,
                output_path,
                chunk_size=chunk_size,
                compression=compression,
                index_types=index_types,
                use_chunking=use_chunking,
                coalesce_schemas=coalesce_schemas,
                enable_crcs=enable_crcs,
            )
        )
    return results


def merge_capture(
    inputs: Union[PathLike, Iterable[PathLike]],
    output_directory: PathLike,
    **kwargs: Any,
) -> List[MergeResult]:
    """Alias for :func:`merge_to_directory`, emphasizing a capture directory."""
    return merge_to_directory(inputs, output_directory, **kwargs)


def merge_directory(
    directory: PathLike,
    output: Optional[PathLike] = None,
    *,
    pattern: str = "*.mcap",
    recursive: bool = False,
    output_name: str = "merged",
    **kwargs: Any,
) -> Union[MergeResult, List[MergeResult]]:
    """Merge the MCAP files in *directory*.

    If ``output`` is omitted, the result is written next to the inputs as
    ``<directory>/<output_name>.mcap``.  If ``output`` names a directory (or has no
    ``.mcap`` suffix), one file per profile is written into it and a list of results is
    returned, as with :func:`merge_to_directory`.  Otherwise a single merged file is
    written and one :class:`MergeResult` is returned.

    The output file is always excluded from the inputs.
    """
    directory_path = Path(directory)
    if output is None:
        output = directory_path / f"{output_name}.mcap"
    output_path = Path(output)
    directory_output = output_path.is_dir() or (
        not output_path.exists() and output_path.suffix == ""
    )
    if directory_output:
        expanded = _expand_inputs(
            directory_path,
            pattern=pattern,
            recursive=recursive,
            exclude=[output_path],
        )
        return merge_to_directory(
            expanded,
            output_path,
            pattern=pattern,
            output_name=output_name,
            **kwargs,
        )

    expanded = _expand_inputs(
        directory_path,
        pattern=pattern,
        recursive=recursive,
        exclude=[output_path],
    )
    return merge_mcaps(
        expanded,
        output_path,
        **kwargs,
    )


def read_merge_manifest(
    source: Union[PathLike, IO[bytes]],
) -> Optional[Dict[str, Any]]:
    """Read the merge manifest from a merged MCAP, if one is present.

    :param source: an MCAP path or a seekable binary stream.
    :returns: the decoded manifest document, or ``None`` when the file was not written
        by the merge functions.
    """
    if isinstance(source, (str, os.PathLike)):
        with open(source, "rb") as stream:
            return read_merge_manifest(stream)
    reader = make_reader(source)
    found: Optional[Dict[str, Any]] = None
    for metadata in reader.iter_metadata():
        if metadata.name != MERGE_METADATA_NAME:
            continue
        payload = metadata.metadata.get("manifest")
        if payload is None:
            # A source file may carry a metadata record with the reserved name; only
            # the record the merge appended last holds the manifest.
            continue
        found = json.loads(payload)
    return found


# Common spellings for the same entry point.
merge_capture_directory = merge_directory
merge_mcap_directory = merge_directory
merge_files = merge_mcaps


__all__ = [
    "EQUIVALENCE_RULES",
    "MERGE_MANIFEST_VERSION",
    "MERGE_METADATA_NAME",
    "ChannelOrigin",
    "DeclarationConflictError",
    "MergeError",
    "MergeResult",
    "ProfileMismatchError",
    "SchemaOrigin",
    "SourceInfo",
    "merge_capture",
    "merge_capture_directory",
    "merge_directory",
    "merge_files",
    "merge_mcap_directory",
    "merge_mcaps",
    "merge_to_directory",
    "read_merge_manifest",
]
