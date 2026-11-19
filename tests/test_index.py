from __future__ import annotations

import os
from io import BytesIO
from typing import Any

import pytest

from cbor2._decoder import CBORDecoder, major_decoders, semantic_decoders
from cbor2._encoder import CBOREncoder
from cbor2._index import (
    IndexFileChangedError,
    StringRefArchiveIndex,
    StringRefContextError,
    StringRefRangeError,
)
from cbor2._types import CBORDecodeValueError, CBORTag


def encode_archive(objects: list[Any]) -> bytes:
    """Encode objects sharing one running string table, like an acquisition
    process appending to an archive with a single long-lived encoder."""
    fp = BytesIO()
    encoder = CBOREncoder(fp, string_referencing=True)
    with encoder.disable_string_namespacing():
        for obj in objects:
            encoder.encode(obj)

    return fp.getvalue()


def encode_namespaced_objects(objects: list[Any]) -> bytes:
    """Encode objects so that each one carries its own tag 256 namespace."""
    fp = BytesIO()
    for obj in objects:
        CBOREncoder(fp, string_referencing=True).encode(obj)

    return fp.getvalue()


def sequential_decode(data: bytes, outer_namespace: bool = True) -> list[Any]:
    """Decode every object in one pass, the way a consumer reading the whole
    archive from the start would."""
    fp = BytesIO(data)
    decoder = CBORDecoder(fp)
    if outer_namespace:
        decoder._stringref_namespace = []

    values = []
    while fp.tell() < len(data):
        values.append(decoder.decode())

    return values


class SequentialTracer(CBORDecoder):
    """Records the (index, value) of every string reference, for comparison
    against index-based replay."""

    def __init__(self, fp: BytesIO):
        super().__init__(fp)
        self.references: list[tuple[int, Any]] = []

    def _decode(self, immutable: bool = False, unshared: bool = False) -> Any:
        # The decoder dispatches through module level function tables bound to
        # the base class, so semantic tags have to be intercepted here for the
        # decode_stringref override below to take effect
        if immutable:
            old_immutable = self._immutable
            self._immutable = True
        if unshared:
            old_index = self._share_index
            self._share_index = None
        try:
            initial_byte = self.read(1)[0]
            major_type = initial_byte >> 5
            subtype = initial_byte & 31
            if major_type == 6:
                return self.decode_semantic(subtype)

            return major_decoders[major_type](self, subtype)
        finally:
            if immutable:
                self._immutable = old_immutable
            if unshared:
                self._share_index = old_index

    def decode_semantic(self, subtype: int) -> Any:
        tagnum = self._decode_length(subtype)
        if tagnum == 25:
            return self.decode_stringref()

        if semantic_decoder := semantic_decoders.get(tagnum):
            return semantic_decoder(self)

        tag = CBORTag(tagnum, None)
        self.set_shareable(tag)
        tag.value = self._decode(unshared=True)
        if self._tag_hook:
            tag = self._tag_hook(self, tag)

        return self.set_shareable(tag)

    def decode_stringref(self) -> Any:
        if self._stringref_namespace is None:
            raise CBORDecodeValueError("string reference outside of namespace")

        index = self._decode()
        try:
            value = self._stringref_namespace[index]
        except IndexError:
            raise CBORDecodeValueError(f"string reference {index} not found")

        self.references.append((index, value))
        return value


def sequential_decode_with_trace(data: bytes) -> tuple[list[Any], list[list[tuple[int, Any]]]]:
    fp = BytesIO(data)
    decoder = SequentialTracer(fp)
    decoder._stringref_namespace = []
    values = []
    traces = []
    while fp.tell() < len(data):
        decoder.references = []
        values.append(decoder.decode())
        traces.append(decoder.references)

    return values, traces


def test_replay_matches_sequential_read():
    objects = [
        {"name": "alpha-project", "tags": ["alpha-project", "beta"]},
        ["beta", "beta", "gamma-gamma"],
        {"name": "gamma-gamma", "tags": ["alpha-project"]},
        b"binary-payload",
        [b"binary-payload", "alpha-project"],
    ]
    data = encode_archive(objects)
    index = StringRefArchiveIndex.build(BytesIO(data))

    assert len(index) == len(objects)
    assert [info.ordinal for info in index.objects] == list(range(len(objects)))
    assert index.objects[0].offset == 0
    for current, following in zip(index.objects, index.objects[1:]):
        assert current.offset + current.length == following.offset
    assert index.objects[-1].offset + index.objects[-1].length == len(data)

    expected = sequential_decode(data)
    for i, value in enumerate(expected):
        assert index.read_object(BytesIO(data), i) == value


def test_local_table_and_sources():
    objects = [
        ["foofoo", "foofoo"],
        ["barbar", "foofoo"],
        ["foofoo"],
    ]
    data = encode_archive(objects)
    index = StringRefArchiveIndex.build(BytesIO(data))

    assert index.local_table(0) == []
    assert index.local_table(1) == ["foofoo"]
    assert index.local_table(2) == ["foofoo", "barbar"]

    assert [source.table_index for source in index.sources] == [0, 1]
    assert [source.object_index for source in index.sources] == [0, 1]
    for source in index.sources:
        # each source offset points at an encoded text string item
        assert data[source.offset] >> 5 == 3

    assert index.resolve(2, 0) == "foofoo"
    assert index.resolve(2, 1) == "barbar"


def test_table_admission_follows_table_size():
    # Fill the table to 24 entries, after which only strings of length >= 4
    # may enter; length 3 must be left out even though it appeared before.
    first = [f"s{i:02d}" for i in range(24)]
    objects = [first, ["abc", "abcd", "abcde"], ["abc"]]
    data = encode_archive(objects)
    index = StringRefArchiveIndex.build(BytesIO(data))

    table = index.local_table(2)
    assert table is not None
    assert len(table) == 26
    assert "abc" not in table
    assert table[24:] == ["abcd", "abcde"]
    assert index.read_object(BytesIO(data), 1) == ["abc", "abcd", "abcde"]
    assert index.read_object(BytesIO(data), 2) == ["abc"]


def test_short_and_empty_strings_not_indexed():
    objects = [["ab", "ab", "", "", b""], ["ab", ""]]
    data = encode_archive(objects)
    index = StringRefArchiveIndex.build(BytesIO(data))

    assert index.sources == ()
    assert index.local_table(1) == []
    # empty strings still decode as complete values
    assert index.read_object(BytesIO(data), 0) == ["ab", "ab", "", "", b""]
    assert index.read_object(BytesIO(data), 1) == ["ab", ""]


def test_nested_namespace_does_not_pollute_outer_table():
    # object 0: tag 256 wrapping ["xxx", ref(0)]; the inner table is discarded
    obj0 = b"\xd9\x01\x00" + b"\x82" + b"\x63xxx" + b"\xd8\x19\x00"
    # object 1: ["yyyy", ref(0)]; the outer table gains "yyyy"
    obj1 = b"\x82" + b"\x64yyyy" + b"\xd8\x19\x00"
    # object 2: ["xxx", ref(0)]; "xxx" enters the outer table at index 1
    obj2 = b"\x82" + b"\x63xxx" + b"\xd8\x19\x00"
    data = obj0 + obj1 + obj2

    index = StringRefArchiveIndex.build(BytesIO(data))
    assert [info.table_size for info in index.objects] == [0, 0, 1]
    assert index.local_table(2) == ["yyyy"]
    assert index.sources[0].object_index == 1

    values = [index.read_object(BytesIO(data), i) for i in range(3)]
    assert values == [["xxx", "xxx"], ["yyyy", "yyyy"], ["xxx", "yyyy"]]
    assert values == sequential_decode(data)


def test_indefinite_strings_do_not_enter_table():
    obj0 = b"\x7f\x63abc\x63def\xff"  # "abcdef" assembled from chunks
    obj1 = b"\x5f\x42ab\x42cd\xff"  # b"abcd" assembled from chunks
    obj2 = b"\x7f\xff"  # a complete empty string
    obj3 = b"\x63abc"  # "abc": the first (and only) table entry
    obj4 = b"\xd8\x19\x00"  # ref 0 -> "abc"
    data = obj0 + obj1 + obj2 + obj3 + obj4

    index = StringRefArchiveIndex.build(BytesIO(data))
    assert [info.table_size for info in index.objects] == [0, 0, 0, 0, 1]
    assert index.local_table(4) == ["abc"]

    values = [index.read_object(BytesIO(data), i) for i in range(5)]
    assert values == ["abcdef", b"abcd", "", "abc", "abc"]
    assert values == sequential_decode(data)


def test_missing_context():
    data = b"\xd8\x19\x00"  # stringref(0) with no namespace anywhere
    with pytest.raises(CBORDecodeValueError, match="string reference outside of namespace"):
        StringRefArchiveIndex.build(BytesIO(data), outer_namespace=False)


def test_objects_without_context():
    objects = [["foofoo", "foofoo"], ["foofoo"]]
    data = encode_namespaced_objects(objects)

    index = StringRefArchiveIndex.build(BytesIO(data), outer_namespace=False)
    assert index.local_table(0) is None
    assert index.local_table(1) is None
    with pytest.raises(StringRefContextError):
        index.resolve(0, 0)

    # objects are still decodable: each opens its own namespace in the stream
    assert index.read_object(BytesIO(data), 0) == ["foofoo", "foofoo"]
    assert index.read_object(BytesIO(data), 1) == ["foofoo"]

    # with a running outer table the same file has an (empty) context, which
    # is distinct from a missing one
    index = StringRefArchiveIndex.build(BytesIO(data))
    assert index.local_table(0) == []
    with pytest.raises(StringRefRangeError):
        index.resolve(0, 0)


def test_reference_out_of_range():
    data = b"\xd8\x19\x05"  # ref 5 against an empty running table
    with pytest.raises(CBORDecodeValueError, match="string reference 5 not found"):
        StringRefArchiveIndex.build(BytesIO(data))


def test_resolve_out_of_range():
    data = encode_archive([["foofoo"], ["foofoo"]])
    index = StringRefArchiveIndex.build(BytesIO(data))
    with pytest.raises(StringRefRangeError):
        index.resolve(1, 1)
    with pytest.raises(StringRefRangeError):
        index.resolve(1, 5)


def test_replay_trace_matches_sequential_read():
    objects = [
        ["foofoo", "barbar"],
        ["foofoo", "bazbaz", "bazbaz"],
        {"barbar": ["bazbaz"]},
    ]
    data = encode_archive(objects)
    index = StringRefArchiveIndex.build(BytesIO(data))

    expected_values, expected_traces = sequential_decode_with_trace(data)
    for i in range(len(objects)):
        trace = []
        value = index.read_object(BytesIO(data), i, trace=trace)
        assert value == expected_values[i]
        assert [(ref.table_index, ref.value) for ref in trace] == expected_traces[i]

    # sources of references into the recorded table point at earlier objects,
    # while references to strings introduced by the object itself have no
    # recorded source
    trace = []
    index.read_object(BytesIO(data), 1, trace=trace)
    assert trace[0].source == index.sources[0]
    assert trace[0].source.object_index == 0
    assert trace[1].source is None


def test_file_change_detected_for_stream():
    data = encode_archive([["foofoo", "foofoo"], ["foofoo"]])
    index = StringRefArchiveIndex.build(BytesIO(data))

    with pytest.raises(IndexFileChangedError):
        index.read_object(BytesIO(data + b"\x00"), 0)

    modified = bytearray(data)
    modified[-1] ^= 0xFF
    with pytest.raises(IndexFileChangedError):
        index.read_object(BytesIO(modified), 0)

    # the unmodified file still passes
    assert index.read_object(BytesIO(data), 1) == ["foofoo"]


def test_file_change_detected_for_path(tmp_path):
    data = encode_archive([["foofoo", "foofoo"], ["foofoo"]])
    path = tmp_path / "archive.cbor"
    path.write_bytes(data)

    index = StringRefArchiveIndex.build(path)
    assert index.read_object(path, 1) == ["foofoo"]

    path.write_bytes(data + b"\x00")
    with pytest.raises(IndexFileChangedError):
        index.read_object(path, 0)

    path.write_bytes(data)
    assert index.read_object(path, 1) == ["foofoo"]


def test_deep_check_detects_same_size_same_mtime_change(tmp_path):
    data = encode_archive([["foofoo", "foofoo"], ["foofoo"]])
    path = tmp_path / "archive.cbor"
    path.write_bytes(data)
    index = StringRefArchiveIndex.build(path)

    modified = bytearray(data)
    modified[-1] ^= 0xFF
    path.write_bytes(modified)
    # restore the original mtime so only the content differs
    assert index.fingerprint.mtime_ns is not None
    os.utime(path, ns=(0, index.fingerprint.mtime_ns))

    with path.open("rb") as fp:
        with pytest.raises(IndexFileChangedError):
            index.check_file(fp, deep=True)


def test_serialization_roundtrip():
    objects = [
        ["foofoo", "barbar"],
        [b"binary-payload", "foofoo"],
        {"barbar": [b"binary-payload"]},
    ]
    data = encode_archive(objects)
    index = StringRefArchiveIndex.build(BytesIO(data))

    restored = StringRefArchiveIndex.loads(index.dumps())
    assert restored.outer_namespace == index.outer_namespace
    assert restored.strings == index.strings
    assert restored.sources == index.sources
    assert restored.objects == index.objects
    assert restored.fingerprint == index.fingerprint
    assert restored.local_table(2) == index.local_table(2)
    for i in range(len(objects)):
        assert restored.read_object(BytesIO(data), i) == index.read_object(BytesIO(data), i)


def test_empty_archive():
    index = StringRefArchiveIndex.build(BytesIO(b""))
    assert len(index) == 0
    assert index.strings == ()
    assert index.sources == ()
