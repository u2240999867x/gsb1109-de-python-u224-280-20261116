from __future__ import annotations

import struct
from binascii import unhexlify
from io import BytesIO

import pytest

import cbor2
from cbor2 import (
    CBORTag,
    MISSING,
    SelectiveResult,
    TagKey,
    dumps,
    loads,
    loads_selective,
)


def test_selects_values_without_materializing_the_rest():
    payload = dumps({"skip": ["x" * 10000], "keep": {"nested": [1, 2, 3]}})
    result = loads_selective(payload, [("keep", "nested", 1), ("keep", "nested")])

    assert isinstance(result, SelectiveResult)
    assert result.complete
    assert result[("keep", "nested", 1)] == 2
    assert result[("keep", "nested")] == [1, 2, 3]


def test_missing_paths_are_reported_separately():
    result = loads_selective(dumps({"a": 1}), [("a",), ("b",), ("a", "too-deep")])
    assert result[("a",)] == 1
    assert result.missing == (("b",), ("a", "too-deep"))
    assert result.unfinished == ()
    assert result.complete


def test_bool_and_integer_keys_do_not_claim_each_other():
    # Hand-built map with the distinct CBOR keys true and 1.
    payload = unhexlify("a2f56179016178")
    result = loads_selective(payload, [(True,), (1,), (False,), (0,)])

    assert result[(True,)] == "y"
    assert result[(1,)] == "x"
    assert result.missing == ((False,), (0,))
    assert result.unfinished == ()


def test_text_and_bytes_keys_do_not_claim_each_other():
    payload = dumps({"1": "text", b"1": "bytes"})
    result = loads_selective(payload, [("1",), (b"1",)])
    assert result[("1",)] == "text"
    assert result[(b"1",)] == "bytes"


def test_float_and_integer_keys_do_not_claim_each_other():
    # A map with the integer key 1 followed by the float key 1.0.
    payload = unhexlify("a20163696e74fb3ff000000000000065666c6f6174")
    result = loads_selective(payload, [(1,), (1.0,)])

    assert result[(1,)] == "int"
    assert result[(1.0,)] == "float"


def test_unselected_payload_is_skipped_for_seekable_inputs():
    payload = dumps(["x" * 50000, 42])
    # Structural indexing skips the 50 kB string payload.  Only the selected
    # integer asks for payload bytes, so a small budget is sufficient.
    result = loads_selective(payload, [(1,)], read_budget=20)
    assert result[(1,)] == 42
    assert result.complete


def test_read_budget_returns_unfinished_with_confirmed_sources():
    payload = dumps(["ok", "x" * 50000])
    result = loads_selective(payload, [(0,), (1,)], read_budget=20)

    assert result[(0,)] == "ok"
    assert (1,) in result.unfinished
    assert result.complete is False
    assert "read" in result.exhausted
    assert (1,) not in result.values


def test_expansion_budget_reports_unfinished_paths():
    payload = dumps([[1, 2, 3], 4])
    result = loads_selective(payload, [(1,), (0,)], expansion_budget=2)

    assert result[(1,)] == 4
    assert (0,) in result.unfinished
    assert result.complete is False
    assert "expansion" in result.exhausted
    assert (0,) not in result.values


def test_string_reference_fetches_the_definition_after_skipping_it():
    payload = dumps(["first", "second", "first"], string_referencing=True)
    result = loads_selective(payload, [(2,)])

    assert result[(2,)] == "first"
    assert result.complete
    assert [source.kind for source in result.sources] == ["stringref"]
    assert result.sources[0].index == 0


def test_string_reference_dependency_within_a_selected_structure():
    payload = dumps(
        {"a": ["repeated-value", "repeated-value"], "b": 1}, string_referencing=True
    )
    result = loads_selective(payload, [("a", 1)])

    assert result[("a", 1)] == "repeated-value"
    assert result.sources


def test_shared_definition_before_the_selected_reference_is_fetched():
    # [[{"a": 1}], shared-reference(1)] where the second element refers to a
    # definition that was already skipped by structural indexing.
    payload = unhexlify("d81c82d81c81a1616101d81d01")
    result = loads_selective(payload, [(1, 0)])

    assert result[(1, 0)] == {"a": 1}
    assert result.complete
    assert len(result.sources) == len(set(result.sources))
    assert any(source.kind == "shared" for source in result.sources)


def test_shared_definition_after_the_selected_reference_cannot_be_claimed():
    payload = unhexlify("82d81d00d81c81a1616101")
    result = loads_selective(payload, [(0,)])
    assert (0,) in result.unfinished
    assert (0,) not in result.values


def test_shared_value_identity_matches_full_decode():
    # The second array element is a reference back to the enclosing array.
    payload = unhexlify("d81c82d81c81a1616101d81d00d81d01")
    result = loads_selective(payload, [()])

    # Both backends produce an array whose second element refers back to it.
    assert result[()][0] == [{"a": 1}]
    assert result[()][1] is result[()]


def test_selected_paths_preserve_shared_occurrences():
    payload = unhexlify("d81c82d81c81a1616101d81d01d81d01")
    result = loads_selective(payload, [(0,), (1,), (0, 0)])

    assert result[(0,)] is result[(1,)]
    assert result[(0, 0)] == {"a": 1}
    assert result[(0,)][0] is result[(0, 0)]


def test_cyclic_array_with_a_finite_path():
    payload = unhexlify("d81c81d81d00")
    result = loads_selective(payload, [(), (0,), (0, 0), (0, 0, 0)])

    assert result.complete
    root = result[()]
    assert root[0] is root
    assert result[(0,)] is root
    assert result[(0, 0)] is root
    assert result.missing == ()


def test_cyclic_map_with_a_finite_path():
    payload = unhexlify("d81ca100d81d00")
    result = loads_selective(payload, [(), (0,), (0, 0)])

    root = result[()]
    assert root[0] is root
    assert result[(0,)] is root


def test_scalar_cycle_is_unfinished_not_a_guessed_value():
    payload = unhexlify("d81cd81d00")
    result = loads_selective(payload, [()])
    assert result.unfinished == ((),)
    assert () not in result.values


def test_unknown_tag_is_not_implicitly_a_map():
    payload = dumps(CBORTag(4242, {"a": 1}))

    plain = loads_selective(payload, [("a",)])
    assert plain.missing == (("a",),)

    explicit = loads_selective(payload, [(TagKey(4242), "a")])
    assert explicit[(TagKey(4242), "a")] == 1


def test_known_transparent_tags_are_unwrapped_for_paths():
    payload = dumps(CBORTag(55799, {"a": 1}))
    result = loads_selective(payload, [("a",)])
    assert result[("a",)] == 1


def test_truncated_selected_value_is_unfinished():
    # Array declares 3 items and provides only two.  A proven prefix element
    # is still returned; the unproven rest is unfinished.
    payload = unhexlify("83016261")
    result = loads_selective(payload, [(0,), (1,), (2,)])

    assert result[(0,)] == 1
    assert (1,) in result.unfinished
    assert (2,) in result.unfinished
    assert result.complete is False
    assert result.scan_error


def test_incomplete_map_cannot_confirm_a_key():
    # The map declares three pairs but ends after one.  Because a later,
    # unseen pair could repeat the key, even the first pair stays unfinished.
    payload = unhexlify("a3616101616202")
    result = loads_selective(payload, [("a",), ("c",)])

    assert result.values == {}
    assert ("a",) in result.unfinished
    assert ("c",) in result.unfinished
    assert result.scan_error


def test_corrupt_unselected_region_invalidates_later_boundaries():
    # Array of 3: the second item has a reserved simple value.  The third
    # item's boundary cannot be proven, so it must not be returned.
    payload = unhexlify("83fc01")
    result = loads_selective(payload, [(0,), (2,)])

    assert result.values == {}
    assert set(result.unfinished) == {(0,), (2,)}
    assert (2,) not in result.values


def test_bad_unselected_string_does_not_invent_following_values():
    # The first map value declares 255 bytes but the document ends early.
    payload = unhexlify("a2616178ff6162")
    result = loads_selective(payload, [("b",)])
    assert ("b",) not in result.values
    assert result.complete is False


@pytest.mark.parametrize(
    "value",
    [
        0,
        -1,
        2**70,
        "text",
        b"bytes",
        [1, 2, 3],
        {"a": 1, "b": [True, False]},
        True,
        False,
        None,
        CBORTag(6000, {"x": 1}),
        float("inf"),
    ],
)
def test_root_selection_matches_full_decode(value):
    payload = dumps(value)
    result = loads_selective(payload, [()])
    assert result.complete
    assert result[()] == loads(payload)


def test_selected_array_elements_match_full_decode():
    value = {"items": [0, "one", {"two": 2}, [3, 4]]}
    payload = dumps(value)
    result = loads_selective(
        payload, [("items", 0), ("items", 1), ("items", 2), ("items", 3)]
    )
    expected = loads(payload)["items"]
    for index in range(4):
        assert result[("items", index)] == expected[index]


def test_map_keys_use_immutable_types_and_the_object_hook_context():
    # {{1: 1}: "x"} — the nested map is a key and must be a FrozenDict.
    payload = unhexlify("a1a101016178")

    result = loads_selective(payload, [()])
    assert result[()] == loads(payload)
    key = next(iter(result.value))
    assert isinstance(key, cbor2.FrozenDict)

    seen: list[bool] = []

    def object_hook(decoder, value):
        seen.append(decoder.immutable)
        # An immutable hook must return a hashable replacement.
        return cbor2.FrozenDict(value) if decoder.immutable else value

    loads_selective(payload, [()], object_hook=object_hook)
    # Inner key map expands immutably; it is then frozen and can still be a
    # key.  The outer map expands mutably.
    assert seen == [True, False]


def test_tag_hook_is_used_for_selected_values():
    class Dummy:
        def __init__(self, value):
            self.value = value

    payload = dumps({"outer": CBORTag(6001, {"a": 1})})

    def tag_hook(decoder, tag):
        return Dummy(tag.value)

    result = loads_selective(payload, [("outer",)], tag_hook=tag_hook)
    assert isinstance(result[("outer",)], Dummy)
    assert result[("outer",)].value == {"a": 1}


def test_budget_validation():
    payload = dumps([1])
    with pytest.raises(ValueError):
        loads_selective(payload, [()], read_budget=-1)
    with pytest.raises(ValueError):
        loads_selective(payload, [()], expansion_budget=-1)
    with pytest.raises(ValueError):
        loads_selective(payload, [()], read_budget=True)


def test_path_validation():
    payload = dumps([1])
    with pytest.raises(TypeError):
        loads_selective(payload, ["not-a-path"])
    with pytest.raises(TypeError):
        loads_selective(payload, [1])


def test_load_selective_from_stream(tmp_path):
    path = tmp_path / "data.cbor"
    path.write_bytes(dumps({"a": [1, 2, 3], "b": "x"}))
    with path.open("rb") as fp:
        result = cbor2.load_selective(fp, [("a", 2), ("b",)])

    assert result[("a", 2)] == 3
    assert result[("b",)] == "x"


def test_non_seekable_stream_is_supported():
    class NonSeekable:
        def __init__(self, data):
            self._buffer = BytesIO(data)

        def read(self, size=-1):
            return self._buffer.read(size)

    payload = dumps({"a": 1, "b": "value"})
    result = cbor2.load_selective(NonSeekable(payload), [("b",)])
    assert result[("b",)] == "value"


def test_result_helpers():
    result = loads_selective(dumps({"a": 1}), [("a",)])
    assert result.get(("a",)) == 1
    assert result.get(("missing",), "default") == "default"
    assert result.value == 1
    assert result.incomplete == result.unfinished
    assert MISSING is not None
