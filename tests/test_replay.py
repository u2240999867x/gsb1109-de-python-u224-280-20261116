"""Tests for versioned tag interpretation rules and record replay."""

from __future__ import annotations

from decimal import Decimal

import pytest

from cbor2 import (
    CBORReplayError,
    CBORRuleError,
    CBORTag,
    Replay,
    RuleSet,
    TaggedValue,
    TagRule,
    builtin_rules,
    pending_tickets,
    replay_records,
)


def raw_text(value: bytes) -> str:
    return value.decode()


def datetime_v1(ctx):
    return f"v1:{ctx.raw.decode()}"


def datetime_v2(ctx):
    return f"v2:{ctx.raw.decode()}"


def epoch_seconds(ctx):
    return f"epoch:{ctx.raw.decode()}"


def bignum(ctx):
    return int.from_bytes(ctx.raw, "big")


def identity_outer(ctx):
    return ctx.interpret(ctx.payload)


def test_tag_rule_normalises_dependencies():
    rule = TagRule(4, "decimal", "interpreted", [3, 2])
    assert rule.dependencies == frozenset({2, 3})
    assert rule.depends_on(2)
    assert not rule.depends_on(5)
    assert TagRule(4, "decimal", dependencies="all").depends_on(9999)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"tag": -1, "semantic": "x"},
        {"tag": 2**64, "semantic": "x"},
        {"tag": "0", "semantic": "x"},
        {"tag": 0, "semantic": ""},
        {"tag": 0, "semantic": "x", "input": "bytes"},
        {"tag": 0, "semantic": "x", "dependencies": "some"},
    ],
)
def test_tag_rule_validation(kwargs):
    with pytest.raises(CBORRuleError):
        TagRule(**kwargs)


def test_rule_set_validates_declared_tag():
    with pytest.raises(CBORRuleError):
        RuleSet("v1", {1: TagRule(2, "other")})


def test_rule_set_from_dict_layouts():
    as_list = RuleSet.from_dict(
        {
            "version": "v2",
            "rules": [
                {"tag": 0, "handler": "datetime", "receives": "raw"},
                {"tag": 1, "semantic": "epoch", "depends_on": [0]},
            ],
        }
    )
    as_mapping = RuleSet.from_dict(
        {
            "version": "v2",
            "rules": {
                "0": {"semantic": "datetime", "input": "raw"},
                "1": {"semantic": "epoch", "dependencies": [0]},
            },
        }
    )
    flat = RuleSet.from_dict(
        {
            "version": "v2",
            "0": {"semantic": "datetime", "input": "raw"},
            "1": {"semantic": "epoch", "dependencies": [0]},
        }
    )
    assert as_list == as_mapping == flat
    assert as_list[1].dependencies == frozenset({0})


def test_diff_distinguishes_metadata_from_behaviour():
    old = RuleSet("v1", {0: TagRule(0, "datetime", "raw", description="old note")})
    new = RuleSet("v2", {0: TagRule(0, "datetime", "raw", description="new note")})
    diff = old.diff(new)
    assert diff.changed_tags == {0}
    assert diff.behavioural_tags == set()
    assert not diff.affects_tag(0)
    assert diff.changes[0].attributes == ("description",)


def test_diff_reports_behavioural_attributes():
    old = RuleSet("v1", {0: TagRule(0, "datetime", "raw")})
    new = RuleSet("v2", {0: TagRule(0, "datetime", "interpreted")})
    change = old.diff(new).changes[0]
    assert change.kind == "modified"
    assert change.behavioural_attributes == ("input",)
    assert change.affects_interpretation


def test_diff_added_and_removed_rules():
    old = RuleSet("v1", {0: TagRule(0, "datetime", "raw")})
    new = RuleSet("v2", {1: TagRule(1, "epoch", "raw")})
    diff = old.diff(new)
    assert diff.changes[0].kind == "removed"
    assert diff.changes[1].kind == "added"
    assert diff.behavioural_tags == {0, 1}


def test_dependency_closure_follows_old_and_new_rules():
    old = RuleSet("v1", {4: TagRule(4, "decimal", "interpreted", {2})})
    new = RuleSet("v2", {4: TagRule(4, "decimal", "interpreted", {3})})
    replay = Replay(old, new)
    assert replay.dependency_closure(4) == {4, 2, 3}
    assert replay.is_affected(4)
    assert replay.is_affected(4, nested_tags=[3])


def test_raw_handler_receives_original_bytes():
    old = RuleSet("v1", {0: TagRule(0, "datetime", "raw")})
    new = RuleSet("v2", {0: TagRule(0, "datetime-v2", "raw")})
    replay = Replay(old, new, {"datetime-v2": datetime_v2})
    record = {"when": TaggedValue(0, "decoded", raw=b"2024-01-02T03:04:05Z")}
    result = replay.replay(record)
    assert result.new == {"when": "v2:2024-01-02T03:04:05Z"}


def test_raw_handler_without_bytes_is_pending_not_a_string():
    old = RuleSet("v1", {0: TagRule(0, "datetime-v1", "raw")})
    new = RuleSet("v2", {0: TagRule(0, "datetime", "raw")})
    replay = Replay(old, new, {"datetime": datetime_v2})
    record = {"when": CBORTag(0, "not bytes")}
    result = replay.replay(record)
    field = result.field("when")
    assert field.status == "pending"
    assert field.new_value is not None
    assert result.new["when"] == record["when"]
    assert isinstance(result.new["when"], CBORTag)


def test_unchanged_rules_do_not_replay():
    rules = RuleSet("v1", {0: TagRule(0, "datetime", "raw")})
    calls = []
    replay = Replay(
        rules,
        rules,
        {"datetime": lambda ctx: calls.append(ctx.tag) or "changed"},
    )
    result = replay.replay({"when": TaggedValue(0, "x", raw=b"x")})
    assert calls == []
    assert result.field("when").status == "unchanged"


def test_description_only_change_does_not_replay():
    old = RuleSet("v1", {0: TagRule(0, "datetime", "raw", description="old")})
    new = RuleSet("v2", {0: TagRule(0, "datetime", "raw", description="new")})
    calls = []
    replay = Replay(old, new, {"datetime": lambda ctx: calls.append(ctx.tag) or "changed"})
    replay.replay({"when": TaggedValue(0, "x", raw=b"x")})
    assert calls == []


def test_plain_number_does_not_inherit_time_semantics():
    old = RuleSet("v1", {1: TagRule(1, "epoch", "raw")})
    new = RuleSet("v2", {1: TagRule(1, "epoch-v2", "raw")})
    replay = Replay(old, new, {"epoch-v2": lambda ctx: "time"})
    result = replay.replay({"count": 1700000000, "items": [1, 2, 3]})
    assert result.new == {"count": 1700000000, "items": [1, 2, 3]}
    assert result.fields == []
    assert not result.replayed


def test_unknown_outer_tag_does_not_run_inner_vendor_handler():
    calls = []

    def vendor(ctx):
        calls.append(ctx.tag)
        return "vendor"

    old = RuleSet("v1", {4000: TagRule(4000, "vendor", "raw")})
    new = RuleSet("v2", {4000: TagRule(4000, "vendor", "raw")})
    replay = Replay(old, new, {"vendor": vendor})
    inner = TaggedValue(4000, b"\x01", raw=b"\x01")
    outer = TaggedValue(5000, inner, raw=b"\x01")
    result = replay.replay({"payload": outer})
    assert calls == []
    assert result.new["payload"] is outer


def test_out_of_scope_inner_tag_stays_opaque():
    calls = []

    def vendor(ctx):
        calls.append(ctx.tag)
        return "vendor"

    old = RuleSet(
        "v1", {1000: TagRule(1000, "outer", "raw"), 4000: TagRule(4000, "vendor", "raw")}
    )
    new = RuleSet(
        "v2",
        {
            1000: TagRule(1000, "outer", "raw"),
            4000: TagRule(4000, "vendor", "raw", description="touched"),
        },
    )
    inner = TaggedValue(4000, b"\x01", raw=b"\x01")
    outer = TaggedValue(1000, inner, raw=b"\x01")
    replay = Replay(old, new, {"outer": lambda ctx: ctx.raw, "vendor": vendor})
    result = replay.replay({"payload": outer})
    assert calls == []
    assert result.field("payload").status == "unchanged"


def test_declared_dependency_lets_outer_propagate_inner_change():
    def outer(ctx):
        return ("outer", ctx.payload)

    old = RuleSet(
        "v1",
        {
            1000: TagRule(1000, "outer", "interpreted", "all"),
            4000: TagRule(4000, "vendor-v1", "raw"),
        },
    )
    new = RuleSet(
        "v2",
        {
            1000: TagRule(1000, "outer", "interpreted", "all"),
            4000: TagRule(4000, "vendor-v2", "raw"),
        },
    )
    replay = Replay(
        old,
        new,
        {
            "outer": outer,
            "vendor-v1": lambda ctx: "v1",
            "vendor-v2": lambda ctx: "v2",
        },
    )
    inner = TaggedValue(4000, b"\x01", raw=b"\x01")
    outer = TaggedValue(1000, inner, raw=b"\x01")
    result = replay.replay({"payload": outer})
    field = result.field("payload")
    assert field.status == "reinterpreted"
    assert result.new["payload"] == ("outer", "v2")
    assert [dependency.tag for dependency in result.dependencies] == [4000]


def test_inner_change_without_dependency_does_not_rerun_outer():
    calls = []

    def outer(ctx):
        calls.append(ctx.tag)
        return "outer"

    old = RuleSet(
        "v1",
        {1000: TagRule(1000, "outer", "interpreted"), 4000: TagRule(4000, "vendor", "raw")},
    )
    new = RuleSet(
        "v2",
        {
            1000: TagRule(1000, "outer", "interpreted"),
            4000: TagRule(4000, "vendor", "raw", description="changed"),
        },
    )
    replay = Replay(old, new, {"outer": outer, "vendor": lambda ctx: "vendor"})
    inner = TaggedValue(4000, b"\x01", raw=b"\x01")
    outer = TaggedValue(1000, inner, raw=b"\x01")
    replay.replay({"payload": outer})
    assert calls == []


def test_nested_pending_propagates_to_outer_interpreter():
    old = RuleSet(
        "v1",
        {
            1000: TagRule(1000, "outer", "interpreted", "all"),
            4000: TagRule(4000, "vendor", "raw"),
        },
    )
    new = RuleSet(
        "v2",
        {
            1000: TagRule(1000, "outer", "interpreted", "all"),
            4000: TagRule(4000, "vendor-v2", "raw"),
        },
    )
    replay = Replay(old, new, {"outer": identity_outer})
    inner = TaggedValue(4000, b"\x01", raw=None)
    outer = TaggedValue(1000, inner, raw=b"\x01")
    result = replay.replay({"payload": outer})
    assert result.field("payload").status == "pending"
    assert result.new["payload"] is outer


def test_retagged_result_keeps_original_tag_and_bytes():
    old = RuleSet("v1", {24: TagRule(24, "embedded-v1", "interpreted", "all")})
    new = RuleSet("v2", {24: TagRule(24, "embedded", "interpreted", "all")})
    replay = Replay(old, new, {"embedded": lambda ctx: CBORTag(0, ctx.payload)})
    record = {"payload": TaggedValue(24, "x", raw=b"x")}
    result = replay.replay(record)
    field = result.field("payload")
    assert field.old_tag == 24
    assert field.new_tag == 0
    assert field.status == "reinterpreted"
    assert field.nodes[0].raw == b"x"
    assert field.nodes[0].tag_raw == b"\xd8\x18"


def test_removed_rule_is_pending_and_keeps_original_type():
    old = RuleSet("v1", {0: TagRule(0, "datetime", "raw")})
    new = RuleSet("v2", {})
    replay = Replay(old, new, {"datetime": datetime_v1})
    record = {"when": TaggedValue(0, "x", raw=b"x")}
    result = replay.replay(record)
    field = result.field("when")
    assert field.status == "pending"
    assert result.new["when"] is record["when"]


def test_missing_handler_is_pending():
    old = RuleSet("v1", {0: TagRule(0, "datetime", "raw")})
    new = RuleSet("v2", {0: TagRule(0, "datetime-v2", "raw")})
    replay = Replay(old, new)
    result = replay.replay({"when": TaggedValue(0, "x", raw=b"x")})
    field = result.field("when")
    assert field.status == "pending"
    assert "datetime-v2" in field.reason


def test_handler_may_request_pending_explicitly():
    def handler(ctx):
        return ctx.pending("clock not available")

    old = RuleSet("v1", {0: TagRule(0, "datetime", "raw")})
    new = RuleSet("v2", {0: TagRule(0, "datetime-v2", "raw")})
    replay = Replay(old, new, {"datetime-v2": handler})
    result = replay.replay({"when": TaggedValue(0, "x", raw=b"x")})
    field = result.field("when")
    assert field.status == "pending"
    assert field.reason == "clock not available"
    assert pending_tickets(result)[0].path == "when"


def test_replay_reports_full_provenance():
    old = RuleSet("v1", {0: TagRule(0, "datetime", "raw")})
    new = RuleSet("v2", {0: TagRule(0, "datetime-v2", "raw")})
    replay = Replay(old, new, {"datetime-v2": datetime_v2})
    record = {
        "when": TaggedValue(0, "x", raw=b"2024-01-01T00:00:00Z"),
        "other": "plain",
    }
    result = replay.replay(record)
    field = result.field("when")
    assert field.old_rule is old[0]
    assert field.new_rule is new[0]
    assert field.rule_version == "v2"
    assert field.old_value == "x"
    assert field.new_value == "v2:2024-01-01T00:00:00Z"
    assert field.nodes[0].raw == b"2024-01-01T00:00:00Z"


def test_replay_records_helper():
    old = RuleSet("v1", {0: TagRule(0, "datetime", "raw")})
    new = RuleSet("v2", {0: TagRule(0, "datetime-v2", "raw")})
    results = replay_records(
        {
            "a": TaggedValue(0, "x", raw=b"a"),
            "b": TaggedValue(0, "y", raw=b"b"),
        },
        old,
        new,
        {"datetime-v2": datetime_v2},
    )
    assert set(results) == {"a", "b"}
    assert results["a"].new == "v2:a"
    assert results["b"].new == "v2:b"


def test_interpreted_handler_gets_replayed_nested_value():
    def decimal_fraction(ctx):
        exponent, significand = ctx.payload
        return Decimal(significand) * (Decimal(10) ** exponent)

    old = RuleSet(
        "v1",
        {4: TagRule(4, "decimal", "interpreted", {2}), 2: TagRule(2, "bignum", "raw")},
    )
    new = RuleSet(
        "v2",
        {4: TagRule(4, "decimal", "interpreted", {2}), 2: TagRule(2, "bignum-v2", "raw")},
    )
    replay = Replay(
        old,
        new,
        {"decimal": decimal_fraction, "bignum-v2": lambda ctx: int.from_bytes(ctx.raw, "big")},
    )
    record = {"price": TaggedValue(4, (2, TaggedValue(2, b"\x64", raw=b"\x64")), raw=b"")}
    result = replay.replay(record)
    assert result.new["price"] == Decimal("10000")


def test_out_of_scope_nested_tag_is_not_reinterpreted():
    def decimal_fraction(ctx):
        exponent, significand = ctx.payload
        return exponent, significand

    old = RuleSet(
        "v1",
        {4: TagRule(4, "decimal", "interpreted", {2}), 2: TagRule(2, "bignum", "raw")},
    )
    new = RuleSet(
        "v2",
        {
            4: TagRule(4, "decimal", "interpreted", {2}),
            2: TagRule(2, "bignum", "raw", description="x"),
        },
    )
    replay = Replay(old, new, {"decimal": decimal_fraction, "bignum": lambda ctx: 1})
    record = {"ok": TaggedValue(4, (2, TaggedValue(2, b"\x64", raw=b"\x64")), raw=b"")}
    result = replay.replay(record)
    assert result.field("ok").status == "unchanged"


def test_builtin_rules_cover_standard_tags():
    rules = builtin_rules()
    assert rules.version == "builtin"
    assert rules[0].input == "raw"
    assert rules[4].dependencies == frozenset({2, 3})
    assert rules[24].dependencies == "all"


def test_register_validates_handler():
    replay = Replay(builtin_rules("v1"), builtin_rules("v2"))
    with pytest.raises(CBORReplayError):
        replay.register("", lambda ctx: None)
    with pytest.raises(CBORReplayError):
        replay.register("x", "not callable")


def test_real_payload_bytes_round_trip():
    from binascii import unhexlify

    payload = unhexlify("74323031332d30332d32315432303a30343a30305a")[1:]
    old = RuleSet("v1", {0: TagRule(0, "datetime-v1", "raw")})
    new = RuleSet("v2", {0: TagRule(0, "datetime-v2", "raw")})
    replay = Replay(old, new, {"datetime-v2": lambda ctx: ctx.raw.decode()})
    result = replay.replay({"when": TaggedValue(0, "stale", raw=payload)})
    assert result.new["when"] == "2013-03-21T20:04:00Z"
    assert result["when"].nodes[0].raw == payload


def test_set_payload_is_traversed():
    old = RuleSet(
        "v1",
        {
            258: TagRule(258, "set", "interpreted", "all"),
            4000: TagRule(4000, "vendor", "raw"),
        },
    )
    new = RuleSet(
        "v2",
        {
            258: TagRule(258, "set", "interpreted", "all"),
            4000: TagRule(4000, "vendor-v2", "raw"),
        },
    )
    replay = Replay(
        old,
        new,
        {"set": lambda ctx: ctx.payload, "vendor-v2": lambda ctx: "v2"},
    )
    record = {"tags": TaggedValue(258, {TaggedValue(4000, b"\x01", raw=b"\x01")}, raw=b"")}
    result = replay.replay(record)
    assert result.new["tags"] == {"v2"}
    assert result.field("tags").status == "reinterpreted"
