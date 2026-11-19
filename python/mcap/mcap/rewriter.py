"""Rewrite an MCAP's container layout without altering its message stream.

A :class:`Rewriter` streams one MCAP in physical (file) order and writes a new one with
different chunking and compression settings. Message payloads, schema/channel
definitions and their relationships, and the physical order in which messages occur are
all preserved. Timestamps are allowed to go backwards: nothing is sorted. Chunk
boundaries, message indexes, chunk indexes and the summary are rebuilt at the positions
they occupy in the new file.

Memory stays bounded by the largest record or decompressed chunk in the input: the
input is read sequentially one record at a time, and messages are passed straight to
the writer instead of being buffered for reordering.
"""

import os
from typing import IO, Any, Union

from .exceptions import McapError
from .records import (
    Attachment,
    Channel,
    DataEnd,
    Header,
    Message,
    Metadata,
    Schema,
)
from .stream_reader import StreamReader
from .writer import CompressionType, Writer

__all__ = ["Rewriter"]


class Rewriter:
    """Rewrites an MCAP to new compression and chunk-size settings.

    :param input: A filename or binary stream to read the source MCAP from.
    :param output: A filename or binary stream to write the rewritten MCAP to. It must
        not be the same file as ``input``.
    :param chunk_size: Target maximum uncompressed size in bytes of each output chunk.
        A single message whose own record is larger than this is not split or held back:
        the chunk in progress is finalized and the message gets its own chunk, which may
        exceed ``chunk_size``. Rewriting always makes progress instead of waiting for
        enough messages to fill a chunk.
    :param compression: Compression applied to the output chunks.
    :param enable_crcs: Whether chunk, attachment and summary CRCs are written.
    :param validate_crcs: Whether chunk and data-section CRCs of the input are validated
        while reading. A corrupt input then fails the rewrite instead of being copied
        with freshly computed, valid-looking CRCs.

    Message records are copied byte for byte, in the order they appear in the input.
    Schema and channel records keep their source ids, so every message keeps its channel
    and every channel keeps its schema; repeated definitions (in later chunks or in the
    summary) are registered once on their first appearance. Attachments and metadata are
    carried over; the old chunk indexes, message indexes, statistics and summary offsets
    are discarded and regenerated. Unknown record types are skipped, as readers are
    allowed to do.
    """

    def __init__(
        self,
        input: Union[str, IO[bytes]],
        output: Union[str, IO[Any]],
        *,
        chunk_size: int = 1024 * 1024,
        compression: CompressionType = CompressionType.ZSTD,
        enable_crcs: bool = True,
        validate_crcs: bool = False,
    ):
        if chunk_size <= 0:
            raise ValueError("chunk_size must be a positive number of bytes")
        if isinstance(input, str) and isinstance(output, str):
            if os.path.abspath(input) == os.path.abspath(output):
                raise McapError("input and output must be different files")
        self._input = input
        self._output = output
        self._chunk_size = chunk_size
        self._compression = compression
        self._enable_crcs = enable_crcs
        self._validate_crcs = validate_crcs

    def run(self) -> None:
        """Performs the rewrite, leaving the output stream open if one was supplied."""
        close_input = False
        if isinstance(self._input, str):
            in_stream: IO[bytes] = open(self._input, "rb")
            close_input = True
        else:
            in_stream = self._input
        try:
            writer = Writer(
                self._output,
                chunk_size=self._chunk_size,
                compression=self._compression,
                enable_crcs=self._enable_crcs,
                enable_data_crcs=False,
            )
            self._copy_records(in_stream, writer)
            writer.finish()
        finally:
            if close_input:
                in_stream.close()

    def _copy_records(self, in_stream: IO[bytes], writer: Writer) -> None:
        seen_schemas: set[int] = set()
        seen_channels: set[int] = set()
        started = False
        reader = StreamReader(in_stream, validate_crcs=self._validate_crcs)
        for record in reader.records:
            if isinstance(record, Header):
                writer.start(profile=record.profile)
                started = True
            elif isinstance(record, Schema):
                if record.id in seen_schemas:
                    continue
                seen_schemas.add(record.id)
                writer.add_schema_record(record)
            elif isinstance(record, Channel):
                if record.id in seen_channels:
                    continue
                seen_channels.add(record.id)
                writer.add_channel_record(record)
            elif isinstance(record, Message):
                writer.add_message(
                    channel_id=record.channel_id,
                    log_time=record.log_time,
                    data=record.data,
                    publish_time=record.publish_time,
                    sequence=record.sequence,
                )
            elif isinstance(record, Attachment):
                writer.add_attachment(
                    create_time=record.create_time,
                    log_time=record.log_time,
                    name=record.name,
                    media_type=record.media_type,
                    data=record.data,
                )
            elif isinstance(record, Metadata):
                writer.add_metadata(name=record.name, data=record.metadata)
            elif isinstance(record, DataEnd):
                # Everything past DataEnd is the old summary, which the writer rebuilds.
                break
        if not started:
            raise McapError("input contains no header record")
