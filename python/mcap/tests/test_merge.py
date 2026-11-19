"""tests for merging MCAP files and capture directories."""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pytest

from mcap.merge import (
    EQUIVALENCE_RULES,
    MERGE_METADATA_NAME,
    ChannelOrigin,
    DeclarationConflictError,
    MergeResult,
    ProfileMismatchError,
    _channel_key,
    _schema_key,
    merge_capture,
    merge_directory,
    merge_mcaps,
    merge_to_directory,
    read_merge_manifest,
)
from mcap.reader import make_reader
from mcap.records import Channel, Header, Message, Schema
from mcap.writer import CompressionType, Writer


def write_mcap(
    path: Path,
    *,
    profile: str = "",
    library: str = "test-suite",
    schemas: Sequence[Tuple[int, str, str, bytes]] = (),
    channels: Sequence[Tuple[int, str, str, int, Dict[str, str]]] = (),
    messages: Sequence[Tuple[int, int, bytes, int, int]] = (),
    metadata: Sequence[Tuple[str, Dict[str, str]]] = (),
) -> None:
    """Write an MCAP with explicit schema/channel ids so tests control the numbering.

    ``channels`` entries are ``(id, topic, message_encoding, schema_id, metadata)``;
    ``messages`` entries are ``(channel_id, log_time, data, publish_time, sequence)``.
    """
    from mcap.records import Metadata

    records: List[Any] = [Header(profile=profile, library=library)]
    for schema_id, name, encoding, data in schemas:
        records.append(Schema(id=schema_id, name=name, encoding=encoding, data=data))
    for channel_id, topic, encoding, schema_id, channel_metadata in channels:
        records.append(
            Channel(
                id=channel_id,
                topic=topic,
                message_encoding=encoding,
                metadata=dict(channel_metadata),
                schema_id=schema_id,
            )
        )
    for channel_id, log_time, data, publish_time, sequence in messages:
        records.append(
            Message(
                channel_id=channel_id,
                log_time=log_time,
                data=data,
                publish_time=publish_time,
                sequence=sequence,
            )
        )
    for name, entries in metadata:
        records.append(Metadata(name=name, metadata=dict(entries)))
    write_raw_records(path, records)


def read_messages(path: Path) -> List[Tuple[Optional[Schema], Channel, Message]]:
    with open(path, "rb") as stream:
        return list(make_reader(stream).iter_messages(log_time_order=True))


def summary(path: Path):
    with open(path, "rb") as stream:
        return make_reader(stream).get_summary()


def test_schema_and_channel_ids_restart_at_one_and_do_not_collide(tmp_path: Path):
    """two files that both use ids 1 and 2 are renumbered independently from 1."""
    first = tmp_path / "first.mcap"
    second = tmp_path / "second.mcap"
    write_mcap(
        first,
        profile="ros2",
        schemas=[(1, "pkg/A", "ros2msg", b"a"), (2, "pkg/B", "ros2msg", b"b")],
        channels=[
            (1, "/a", "cdr", 1, {}),
            (2, "/b", "cdr", 2, {}),
        ],
        messages=[(1, 10, b"a10", 10, 0), (2, 20, b"b20", 20, 0)],
    )
    write_mcap(
        second,
        profile="ros2",
        schemas=[(1, "pkg/C", "ros2msg", b"c")],
        channels=[(1, "/c", "cdr", 1, {})],
        messages=[(1, 30, b"c30", 30, 0)],
    )
    output = tmp_path / "merged.mcap"
    result = merge_mcaps([first, second], output)

    assert result.profile == "ros2"
    assert sorted({origin.output_schema_id for origin in result.schemas.values()}) == [
        1,
        2,
        3,
    ]
    assert sorted(
        {origin.output_channel_id for origin in result.channels.values()}
    ) == [
        1,
        2,
        3,
    ]

    out_summary = summary(output)
    assert out_summary is not None
    assert sorted(out_summary.schemas) == [1, 2, 3]
    assert sorted(out_summary.channels) == [1, 2, 3]

    # Every message still points at the channel of the file it came from.
    origins = {origin.output_channel_id: origin for origin in result.channels.values()}
    for _, channel, message in read_messages(output):
        origin = origins[channel.id]
        assert channel.topic == origin.topic
        assert channel.metadata == origin.metadata
        assert (
            message.data
            == {
                1: b"a10",
                2: b"b20",
                3: b"c30",
            }[origin.output_channel_id]
        )


def test_equal_schema_declarations_share_one_output_schema(tmp_path: Path):
    """byte-identical schema declarations are associated with one output schema."""
    first = tmp_path / "first.mcap"
    second = tmp_path / "second.mcap"
    write_mcap(
        first,
        profile="ros1",
        schemas=[(1, "pkg/A", "ros1msg", b"canonical")],
        channels=[(1, "/a", "ros1", 1, {})],
        messages=[(1, 1, b"x", 1, 0)],
    )
    write_mcap(
        second,
        profile="ros1",
        schemas=[(7, "pkg/A", "ros1msg", b"canonical")],
        channels=[(7, "/a", "ros1", 7, {})],
        messages=[(7, 2, b"y", 2, 0)],
    )
    result = merge_mcaps([first, second], tmp_path / "out.mcap")

    schema_ids = {origin.output_schema_id for origin in result.schemas.values()}
    assert schema_ids == {1}
    assert len(result.origins_for_schema(1)) == 2

    # Channels are still kept apart so the message origin is exact.
    assert result.channels[(0, 1)].output_channel_id == 1
    assert result.channels[(1, 7)].output_channel_id == 2
    assert (
        result.channels[(0, 1)].equivalence_key
        == result.channels[(1, 7)].equivalence_key
    )


def test_different_schema_data_is_not_coalesced(tmp_path: Path):
    """the same name with different bytes is two distinct schemas, never merged."""
    first = tmp_path / "first.mcap"
    second = tmp_path / "second.mcap"
    write_mcap(
        first,
        profile="ros2",
        schemas=[(1, "pkg/A", "ros2msg", b"v1")],
        channels=[(1, "/a", "cdr", 1, {})],
        messages=[(1, 1, b"x", 1, 0)],
    )
    write_mcap(
        second,
        profile="ros2",
        schemas=[(1, "pkg/A", "ros2msg", b"v2")],
        channels=[(1, "/a", "cdr", 1, {})],
        messages=[(1, 2, b"y", 2, 0)],
    )
    result = merge_mcaps([first, second], tmp_path / "out.mcap")
    assert {origin.output_schema_id for origin in result.schemas.values()} == {1, 2}
    assert _schema_key(result.schemas[(0, 1)]) != _schema_key(result.schemas[(1, 1)])


def test_same_ids_with_different_profiles_are_never_mixed(tmp_path: Path):
    """id 1 in a ros1 file and id 1 in a ros2 file stay in separate outputs."""
    first = tmp_path / "first.mcap"
    second = tmp_path / "second.mcap"
    write_mcap(
        first,
        profile="ros1",
        schemas=[(1, "pkg/A", "ros1msg", b"a")],
        channels=[(1, "/same", "ros1", 1, {})],
        messages=[(1, 1, b"one", 1, 0)],
    )
    write_mcap(
        second,
        profile="ros2",
        schemas=[(1, "pkg/A", "ros2msg", b"a")],
        channels=[(1, "/same", "cdr", 1, {})],
        messages=[(1, 2, b"two", 2, 0)],
    )

    with pytest.raises(ProfileMismatchError) as error:
        merge_mcaps([first, second], tmp_path / "mixed.mcap")
    assert "different profiles" in str(error.value)
    assert not (tmp_path / "mixed.mcap").exists()

    results = merge_to_directory([first, second], tmp_path / "merged")
    assert [result.profile for result in results] == ["ros1", "ros2"]
    assert {result.output_path.name for result in results} == {
        "merged-ros1.mcap",
        "merged-ros2.mcap",
    }
    for result in results:
        assert summary(result.output_path).channels  # type: ignore[union-attr]
        with open(result.output_path, "rb") as stream:
            header = make_reader(stream).get_header()
        assert header.profile == result.profile
        payload = [message.data for _, _, message in read_messages(result.output_path)]
        assert payload == [b"one"] if result.profile == "ros1" else [b"two"]


def test_unknown_profile_keeps_payload_and_profile(tmp_path: Path):
    """an unrecognized profile and message encoding survive untouched."""
    source = tmp_path / "capture.mcap"
    payload = bytes(range(256))
    write_mcap(
        source,
        profile="acme.custom/2",
        schemas=[(1, "acme.Message", "acme-schema", b"opaque-definition")],
        channels=[(1, "/raw", "acme-wire", 1, {"unit": "raw"})],
        messages=[(1, 5, payload, 4, 9)],
    )
    result = merge_mcaps([source], tmp_path / "merged.mcap")

    assert result.profile == "acme.custom/2"
    with open(result.output_path, "rb") as stream:
        header = make_reader(stream).get_header()
    assert header.profile == "acme.custom/2"
    [(schema, channel, message)] = read_messages(result.output_path)
    assert schema is not None
    assert schema.name == "acme.Message"
    assert schema.encoding == "acme-schema"
    assert schema.data == b"opaque-definition"
    assert channel.topic == "/raw"
    assert channel.message_encoding == "acme-wire"
    assert channel.metadata == {"unit": "raw"}
    assert message.data == payload
    assert (message.log_time, message.publish_time, message.sequence) == (5, 4, 9)


def test_messages_are_ordered_by_log_time_across_files(tmp_path: Path):
    first = tmp_path / "first.mcap"
    second = tmp_path / "second.mcap"
    write_mcap(
        first,
        profile="ros2",
        channels=[(1, "/a", "cdr", 0, {})],
        messages=[(1, 30, b"a30", 30, 0), (1, 10, b"a10", 10, 0)],
    )
    write_mcap(
        second,
        profile="ros2",
        channels=[(1, "/b", "cdr", 0, {})],
        messages=[(1, 20, b"b20", 20, 0), (1, 5, b"b05", 5, 0)],
    )
    result = merge_mcaps([first, second], tmp_path / "merged.mcap")
    assert [
        message.log_time for _, _, message in read_messages(result.output_path)
    ] == [
        5,
        10,
        20,
        30,
    ]
    assert result.message_count == 4
    assert [source.message_count for source in result.sources] == [2, 2]


def test_result_maps_each_output_channel_back_to_its_source(tmp_path: Path):
    first = tmp_path / "a.mcap"
    second = tmp_path / "b.mcap"
    write_mcap(
        first,
        profile="ros1",
        channels=[(1, "/topic", "ros1", 0, {})],
        messages=[(1, 1, b"a", 1, 0)],
    )
    write_mcap(
        second,
        profile="ros1",
        channels=[(1, "/topic", "ros1", 0, {})],
        messages=[(1, 2, b"b", 2, 0)],
    )
    result = merge_mcaps([first, second], tmp_path / "merged.mcap")

    first_origin: ChannelOrigin = result.channels[(0, 1)]
    second_origin = result.channels[(1, 1)]
    assert first_origin.output_channel_id != second_origin.output_channel_id
    assert result.source_for_channel(first_origin.output_channel_id).path == str(first)
    assert result.source_for_channel(second_origin.output_channel_id).path == str(
        second
    )
    assert result.profile_for_channel(first_origin.output_channel_id) == "ros1"
    assert result.channel_ids_for_source(1) == [second_origin.output_channel_id]


def test_manifest_is_written_and_readable_from_the_output(tmp_path: Path):
    first = tmp_path / "first.mcap"
    second = tmp_path / "second.mcap"
    write_mcap(
        first,
        profile="ros2",
        schemas=[(1, "pkg/A", "ros2msg", b"a")],
        channels=[(1, "/a", "cdr", 1, {"k": "v"})],
        messages=[(1, 1, b"x", 1, 0)],
        metadata=[("source", {"capture": "front"})],
    )
    write_mcap(
        second,
        profile="ros2",
        schemas=[(9, "pkg/A", "ros2msg", b"a")],
        channels=[(4, "/a", "cdr", 9, {"k": "v"})],
        messages=[(4, 2, b"y", 2, 0)],
    )
    result = merge_mcaps([first, second], tmp_path / "merged.mcap")

    manifest = read_merge_manifest(result.output_path)
    assert manifest is not None
    assert manifest == json.loads(json.dumps(result.manifest))
    assert manifest["version"] == 1
    assert manifest["profile"] == "ros2"
    assert manifest["equivalence_rules"] == EQUIVALENCE_RULES
    assert [source["path"] for source in manifest["sources"]] == [
        str(first),
        str(second),
    ]
    assert [source["message_count"] for source in manifest["sources"]] == [1, 1]
    assert manifest["schemas"][0]["output_schema_id"] == 1
    assert manifest["schemas"][1]["source_schema_id"] == 9
    assert manifest["channels"][1]["source_channel_id"] == 4
    assert len(manifest["channel_equivalence"]) == 1
    assert manifest["channel_equivalence"][0]["declarations"] == [
        {"source_index": 0, "source_channel_id": 1},
        {"source_index": 1, "source_channel_id": 4},
    ]

    names = [metadata.name for metadata in _metadata(result.output_path)]
    assert MERGE_METADATA_NAME in names
    assert "source" in names


def _metadata(path: Path):
    with open(path, "rb") as stream:
        return list(make_reader(stream).iter_metadata())


def test_metadata_and_attachments_are_preserved(tmp_path: Path):
    source = tmp_path / "capture.mcap"
    writer = Writer(str(source), compression=CompressionType.NONE)
    writer.start(profile="ros2", library="recorder")
    channel_id = writer.register_channel("/t", "cdr", 0)
    writer.add_message(channel_id, 1, b"x", 1)
    writer.add_metadata("calibration", {"fx": "100"})
    writer.add_attachment(
        create_time=4,
        log_time=5,
        name="frame.jpg",
        media_type="image/jpeg",
        data=b"jpeg",
    )
    writer.finish()

    merge_mcaps([source], tmp_path / "merged.mcap")

    metadata = {
        record.name: record.metadata for record in _metadata(tmp_path / "merged.mcap")
    }
    assert metadata["calibration"] == {"fx": "100"}
    with open(tmp_path / "merged.mcap", "rb") as stream:
        attachments = list(make_reader(stream).iter_attachments())
    assert [
        (a.name, a.media_type, a.data, a.create_time, a.log_time) for a in attachments
    ] == [("frame.jpg", "image/jpeg", b"jpeg", 4, 5)]


def test_merge_directory_discovers_files_and_skips_its_own_output(tmp_path: Path):
    for index in range(2):
        write_mcap(
            tmp_path / f"part-{index}.mcap",
            profile="ros2",
            channels=[(1, "/t", "cdr", 0, {})],
            messages=[(1, index, f"m{index}".encode(), index, 0)],
        )
    result = merge_directory(tmp_path, tmp_path / "merged.mcap")
    assert isinstance(result, MergeResult)
    assert result.message_count == 2

    # Merging again must not ingest the output written by the first pass.
    second = merge_directory(tmp_path, tmp_path / "merged.mcap")
    assert isinstance(second, MergeResult)
    assert second.message_count == 2
    assert len(second.sources) == 2


def test_merge_to_directory_replaces_a_previous_output(tmp_path: Path):
    """running the same capture merge twice must not ingest the first run's output."""
    write_mcap(
        tmp_path / "part.mcap",
        profile="ros2",
        channels=[(1, "/t", "cdr", 0, {})],
        messages=[(1, 1, b"x", 1, 0)],
    )
    first = merge_to_directory(tmp_path, tmp_path / "out")
    second = merge_to_directory(tmp_path, tmp_path / "out")
    assert first[0].output_path == second[0].output_path
    assert second[0].message_count == 1
    assert len(second[0].sources) == 1


def test_merge_directory_can_write_one_file_per_profile(tmp_path: Path):
    write_mcap(
        tmp_path / "ros1.mcap",
        profile="ros1",
        channels=[(1, "/a", "ros1", 0, {})],
        messages=[(1, 1, b"a", 1, 0)],
    )
    write_mcap(
        tmp_path / "custom.mcap",
        profile="custom",
        channels=[(1, "/a", "custom-wire", 0, {})],
        messages=[(1, 2, b"b", 2, 0)],
    )
    results = merge_directory(tmp_path, tmp_path / "merged")
    assert isinstance(results, list)
    assert {result.profile for result in results} == {"ros1", "custom"}
    assert all(result.output_path.parent == tmp_path / "merged" for result in results)


def test_merge_capture_is_an_alias(tmp_path: Path):
    write_mcap(
        tmp_path / "a.mcap",
        profile="ros2",
        channels=[(1, "/a", "cdr", 0, {})],
        messages=[(1, 1, b"a", 1, 0)],
    )
    results = merge_capture(tmp_path, tmp_path / "out")
    assert len(results) == 1
    assert results[0].message_count == 1


def test_empty_and_missing_inputs_are_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="no MCAP input files"):
        merge_mcaps(tmp_path, tmp_path / "out.mcap")
    with pytest.raises(FileNotFoundError):
        merge_mcaps([tmp_path / "missing.mcap"], tmp_path / "out.mcap")


def test_output_cannot_also_be_an_input(tmp_path: Path):
    source = tmp_path / "a.mcap"
    write_mcap(source, profile="ros2", channels=[(1, "/a", "cdr", 0, {})])
    with pytest.raises(ValueError, match="also an input"):
        merge_mcaps([source], source)


def test_conflicting_declaration_in_one_file_is_rejected(tmp_path: Path):
    """a file that reuses one schema/channel id for different declarations is invalid."""
    source = tmp_path / "bad.mcap"
    records = [
        Header(profile="ros2", library="test"),
        Schema(id=1, name="pkg/A", encoding="ros2msg", data=b"first"),
        Schema(id=1, name="pkg/A", encoding="ros2msg", data=b"second"),
    ]
    write_raw_records(source, records)
    with pytest.raises(DeclarationConflictError, match="schema id 1"):
        merge_mcaps([source], tmp_path / "out.mcap")

    channel_conflict = tmp_path / "bad-channel.mcap"
    write_raw_records(
        channel_conflict,
        [
            Header(profile="ros2", library="test"),
            Channel(
                id=1, topic="/one", message_encoding="cdr", metadata={}, schema_id=0
            ),
            Channel(
                id=1, topic="/two", message_encoding="cdr", metadata={}, schema_id=0
            ),
        ],
    )
    with pytest.raises(DeclarationConflictError, match="channel id 1"):
        merge_mcaps([channel_conflict], tmp_path / "out.mcap")


def write_raw_records(path: Path, records: Sequence[Any]) -> None:
    from mcap.data_stream import RecordBuilder
    from mcap.records import Footer
    from mcap.writer import MCAP0_MAGIC

    builder = RecordBuilder()
    for record in records:
        record.write(builder)
    Footer(summary_start=0, summary_offset_start=0, summary_crc=0).write(builder)
    with open(path, "wb") as stream:
        stream.write(MCAP0_MAGIC)
        stream.write(builder.end())
        stream.write(MCAP0_MAGIC)


def test_merge_accepts_files_without_summary(tmp_path: Path):
    """a header-only file has no summary; the merge still writes a valid file."""
    source = tmp_path / "header-only.mcap"
    writer = Writer(str(source), compression=CompressionType.NONE)
    writer.start(profile="ros2")
    writer.finish()
    result = merge_mcaps([source], tmp_path / "out.mcap")
    assert result.message_count == 0
    assert result.profile == "ros2"
    assert summary(result.output_path) is not None


def test_channel_and_schema_equivalence_keys_ignore_file_local_ids(tmp_path: Path):
    left = Schema(id=1, name="n", encoding="e", data=b"d")
    right = Schema(id=99, name="n", encoding="e", data=b"d")
    assert _schema_key(left) == _schema_key(right)

    channel_left = Channel(
        id=1, topic="/t", message_encoding="raw", metadata={"k": "v"}, schema_id=1
    )
    channel_right = Channel(
        id=77, topic="/t", message_encoding="raw", metadata={"k": "v"}, schema_id=99
    )
    assert _channel_key(channel_left, _schema_key(left), "p") == _channel_key(
        channel_right, _schema_key(right), "p"
    )
    # A different profile is never considered equivalent.
    assert _channel_key(channel_left, _schema_key(left), "p") != _channel_key(
        channel_right, _schema_key(right), "q"
    )


def test_loose_messages_outside_chunks_are_not_dropped(tmp_path: Path):
    """a file whose summary statistics disagree with its chunk indexes is scanned, so a
    message stored outside every chunk is merged instead of silently skipped."""
    source = tmp_path / "loose.mcap"
    writer = Writer(
        str(source),
        compression=CompressionType.NONE,
        chunk_size=1,
        enable_crcs=False,
    )
    writer.start(profile="ros2")
    channel_id = writer.register_channel("/t", "cdr", 0)
    writer.add_message(channel_id, 10, b"chunked-a", 10)
    writer.add_message(channel_id, 20, b"chunked-b", 20)
    writer.finish()

    with open(source, "rb") as stream:
        original = bytearray(stream.read())
    from mcap.data_stream import RecordBuilder

    loose = RecordBuilder()
    Message(
        channel_id=channel_id, log_time=5, data=b"loose", publish_time=5, sequence=0
    ).write(loose)
    loose_bytes = bytes(loose.end())

    # Insert the loose message before DataEnd and move the footer's summary start.
    data_end_position = original.index(bytes([0x0F, 0x04]) + bytes(4))
    original[data_end_position:data_end_position] = loose_bytes
    footer_start = len(original) - 8 - 29  # trailing magic + footer record
    summary_start_offset = footer_start + 9
    summary_start = int.from_bytes(
        original[summary_start_offset : summary_start_offset + 8], "little"
    )
    original[summary_start_offset : summary_start_offset + 8] = (
        summary_start + len(loose_bytes)
    ).to_bytes(8, "little")

    # The statistics record now under-counts by one: message_count is the first field
    # of its body, right after the opcode and length prefix.
    statistics_offset = original.index(bytes([0x0B]), summary_start + len(loose_bytes))
    message_count_offset = statistics_offset + 9
    original[message_count_offset : message_count_offset + 8] = (3).to_bytes(
        8, "little"
    )
    source.write_bytes(bytes(original))

    with open(source, "rb") as stream:
        indexed = [
            message.data for _, _, message in make_reader(stream).iter_messages()
        ]
    assert indexed == [b"chunked-a", b"chunked-b"]

    result = merge_mcaps([source], tmp_path / "out.mcap")
    assert result.message_count == 3
    assert [message.data for _, _, message in read_messages(result.output_path)] == [
        b"loose",
        b"chunked-a",
        b"chunked-b",
    ]
