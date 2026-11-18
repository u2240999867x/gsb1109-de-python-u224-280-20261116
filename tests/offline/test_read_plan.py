"""Tests for the read_plan module (shared read plans over aliased tags)."""

import pytest

from pycomm3.read_plan import (
    AliasEvidence,
    CollectionRule,
    DirectoryEntry,
    StopReason,
    TagDirectory,
    build_read_plan,
    parse_member_path,
)


class Recorder:
    """Fake reader returning preset raw values per base identity."""

    def __init__(self, values):
        self.values = values
        self.calls = []

    def __call__(self, read):
        self.calls.append(read.base_identity)
        return self.values[read.base_identity]


def test_parse_member_path():
    assert parse_member_path("") == ()
    assert parse_member_path("meta.count") == ("meta", "count")
    assert parse_member_path("axes[2].pos") == ("axes", 2, "pos")
    assert parse_member_path("[0]") == (0,)


def test_rules_sharing_base_tag_share_one_read():
    directory = TagDirectory(
        3,
        [
            DirectoryEntry("Base", "id-base", "DINT"),
            DirectoryEntry("AliasA", "id-alias-a", "DINT", alias=True),
            DirectoryEntry("AliasB", "id-alias-b", "DINT", alias=True),
        ],
    )
    evidence = [
        AliasEvidence("id-alias-a", "id-base", target_name="Base", version=3),
        AliasEvidence("id-alias-b", "id-base", target_name="Base", version=3),
    ]
    rules = [
        CollectionRule("r1", "AliasA", "DINT"),
        CollectionRule("r2", "AliasB", "DINT"),
        CollectionRule("r3", "Base", "DINT"),
    ]

    plan = build_read_plan(rules, directory, evidence)

    assert len(plan.reads) == 1
    read = plan.reads[0]
    assert read.base_identity == "id-base"
    assert set(read.rule_ids) == {"r1", "r2", "r3"}
    assert all(branch.resolved for branch in plan.branches.values())

    reader = Recorder({"id-base": 42})
    results = plan.execute(reader)

    assert reader.calls == ["id-base"]  # one shared physical read
    for rule_id in ("r1", "r2", "r3"):
        assert results[rule_id].value == 42
        assert results[rule_id].error is None
        assert results[rule_id].source.base_identity == "id-base"
        assert results[rule_id].source.base_name == "Base"
        assert results[rule_id].source.version == 3


def test_alias_to_struct_member():
    directory = TagDirectory(
        1,
        [
            DirectoryEntry("Cfg", "id-cfg", "Config"),
            DirectoryEntry("AliasCount", "id-al-count", "DINT", alias=True),
            DirectoryEntry("AliasLimit", "id-al-limit", "DINT", alias=True),
        ],
    )
    evidence = [
        AliasEvidence("id-al-count", "id-cfg", target_member="count", target_name="Cfg", version=1),
        AliasEvidence("id-al-limit", "id-cfg", target_member="limit", target_name="Cfg", version=1),
    ]
    rules = [
        CollectionRule("count", "AliasCount", "DINT"),
        CollectionRule("limit", "AliasLimit", "DINT"),
    ]

    plan = build_read_plan(rules, directory, evidence)
    assert len(plan.reads) == 1  # both aliases land on the same struct

    reader = Recorder({"id-cfg": {"count": 5, "limit": 9}})
    results = plan.execute(reader)

    assert results["count"].value == 5
    assert results["count"].source.member_path == ("count",)
    assert results["limit"].value == 9
    assert results["limit"].source.member_path == ("limit",)


def test_same_name_different_identity_is_not_same_material():
    # directory v2: "Cell" was retired and its name reused by a placeholder
    directory = TagDirectory(
        2,
        [
            DirectoryEntry("Cell", "id-new", "DINT"),  # placeholder reusing the name
            DirectoryEntry("AliasA", "id-alias-a", "DINT", alias=True),
        ],
    )
    # evidence confirmed the alias against the *old* identity
    evidence = [
        AliasEvidence("id-alias-a", "id-old", target_name="Cell", version=2),
    ]
    rules = [
        CollectionRule("via-alias", "AliasA", "DINT"),
        CollectionRule("direct", "Cell", "DINT"),
    ]

    plan = build_read_plan(rules, directory, evidence)

    # the alias branch stops: old evidence must not attach to the placeholder
    assert plan.branches["via-alias"].status == StopReason.TARGET_RETIRED
    # the direct reference resolves to the placeholder's own identity
    assert plan.branches["direct"].resolved
    assert plan.branches["direct"].base_identity == "id-new"
    # only the placeholder is read, and only for the direct rule
    assert len(plan.reads) == 1
    assert plan.reads[0].rule_ids == ("direct",)

    reader = Recorder({"id-new": 7})
    results = plan.execute(reader)
    assert results["via-alias"].error == StopReason.TARGET_RETIRED
    assert results["via-alias"].value is None
    assert results["direct"].value == 7


def test_renamed_base_tag_follows_confirmed_identity():
    # directory v2: base tag renamed "Old" -> "New", identity unchanged
    directory = TagDirectory(
        2,
        [
            DirectoryEntry("New", "id-1", "DINT"),
            DirectoryEntry("AliasA", "id-alias-a", "DINT", alias=True),
        ],
    )
    evidence = [
        AliasEvidence("id-alias-a", "id-1", target_name="Old", version=2),
    ]
    rules = [CollectionRule("r1", "AliasA", "DINT")]

    plan = build_read_plan(rules, directory, evidence)

    assert plan.branches["r1"].resolved
    reader = Recorder({"id-1": 11})
    results = plan.execute(reader)
    assert results["r1"].value == 11
    # source reports the current name reached through the confirmed identity
    assert results["r1"].source.base_name == "New"
    assert results["r1"].source.base_identity == "id-1"


def test_alias_cycle_stops_only_that_branch():
    directory = TagDirectory(
        1,
        [
            DirectoryEntry("Base", "id-base", "DINT"),
            DirectoryEntry("AliasA", "id-alias-a", "DINT", alias=True),
            DirectoryEntry("AliasB", "id-alias-b", "DINT", alias=True),
        ],
    )
    evidence = [
        AliasEvidence("id-alias-a", "id-alias-b", target_name="AliasB", version=1),
        AliasEvidence("id-alias-b", "id-alias-a", target_name="AliasA", version=1),
    ]
    rules = [
        CollectionRule("loopy", "AliasA", "DINT"),
        CollectionRule("fine", "Base", "DINT"),
    ]

    plan = build_read_plan(rules, directory, evidence)

    assert plan.branches["loopy"].status == StopReason.ALIAS_CYCLE
    assert plan.branches["fine"].resolved
    assert len(plan.reads) == 1
    assert plan.reads[0].rule_ids == ("fine",)

    reader = Recorder({"id-base": 3})
    results = plan.execute(reader)
    assert results["loopy"].error == StopReason.ALIAS_CYCLE
    assert results["fine"].value == 3


def test_version_conflict_stops_branch():
    directory = TagDirectory(
        2,
        [
            DirectoryEntry("Base", "id-base", "DINT"),
            DirectoryEntry("AliasA", "id-alias-a", "DINT", alias=True),
        ],
    )
    # evidence confirmed against directory v1, but the directory is now v2
    evidence = [AliasEvidence("id-alias-a", "id-base", target_name="Base", version=1)]
    rules = [CollectionRule("r1", "AliasA", "DINT")]

    plan = build_read_plan(rules, directory, evidence)

    assert plan.branches["r1"].status == StopReason.VERSION_CONFLICT
    assert plan.reads == ()
    results = plan.execute(Recorder({}))
    assert results["r1"].error == StopReason.VERSION_CONFLICT


def test_missing_evidence_and_unknown_tag():
    directory = TagDirectory(
        1,
        [DirectoryEntry("AliasA", "id-alias-a", "DINT", alias=True)],
    )
    rules = [
        CollectionRule("no-evidence", "AliasA", "DINT"),
        CollectionRule("ghost", "NoSuchTag", "DINT"),
    ]

    plan = build_read_plan(rules, directory, evidence=[])

    assert plan.branches["no-evidence"].status == StopReason.MISSING_EVIDENCE
    assert plan.branches["ghost"].status == StopReason.UNKNOWN_TAG
    assert plan.reads == ()


def test_overlapping_bytes_different_interpretations():
    # two rules over the same underlying tag decode it differently
    directory = TagDirectory(1, [DirectoryEntry("Word", "id-word", "UINT")])
    rules = [
        CollectionRule("low-byte", "Word", "USINT", decoder=lambda v: v & 0xFF),
        CollectionRule("high-byte", "Word", "USINT", decoder=lambda v: (v >> 8) & 0xFF),
    ]

    plan = build_read_plan(rules, directory)
    assert len(plan.reads) == 1  # bytes overlap, read is still shared

    reader = Recorder({"id-word": 0x0102})
    results = plan.execute(reader)

    # each rule keeps its own logical value and its own declared type
    assert results["low-byte"].value == 0x02
    assert results["high-byte"].value == 0x01
    assert results["low-byte"].source.data_type == "USINT"
    assert results["low-byte"].source is not results["high-byte"].source


def test_rule_member_path_appended_after_alias_member():
    directory = TagDirectory(
        1,
        [
            DirectoryEntry("Line", "id-line", "LineData"),
            DirectoryEntry("AliasAxis", "id-al-axis", "Axis", alias=True),
        ],
    )
    evidence = [
        AliasEvidence("id-al-axis", "id-line", target_member="axes", target_name="Line", version=1),
    ]
    rules = [CollectionRule("r1", "AliasAxis[1].pos", "REAL")]

    plan = build_read_plan(rules, directory, evidence)
    branch = plan.branches["r1"]
    assert branch.resolved
    assert branch.member_path == ("axes", 1, "pos")

    raw = {"axes": [{"pos": 1.5}, {"pos": 2.5}]}
    results = plan.execute(Recorder({"id-line": raw}))
    assert results["r1"].value == 2.5


def test_nested_alias_chain():
    directory = TagDirectory(
        4,
        [
            DirectoryEntry("Base", "id-base", "DINT"),
            DirectoryEntry("AliasA", "id-alias-a", "DINT", alias=True),
            DirectoryEntry("AliasB", "id-alias-b", "DINT", alias=True),
        ],
    )
    evidence = [
        AliasEvidence("id-alias-a", "id-alias-b", target_name="AliasB", version=4),
        AliasEvidence("id-alias-b", "id-base", target_name="Base", version=4),
    ]
    rules = [CollectionRule("r1", "AliasA", "DINT")]

    plan = build_read_plan(rules, directory, evidence)
    assert plan.branches["r1"].resolved
    assert plan.branches["r1"].base_identity == "id-base"
    assert len(plan.branches["r1"].evidence) == 2

    results = plan.execute(Recorder({"id-base": 8}))
    assert results["r1"].value == 8


def test_duplicate_rule_ids_rejected():
    directory = TagDirectory(1, [DirectoryEntry("Base", "id-base", "DINT")])
    rules = [CollectionRule("r1", "Base"), CollectionRule("r1", "Base")]
    with pytest.raises(ValueError):
        build_read_plan(rules, directory)


def test_reader_failure_marks_rules_but_keeps_plan():
    directory = TagDirectory(1, [DirectoryEntry("Base", "id-base", "DINT")])
    rules = [CollectionRule("r1", "Base", "DINT")]
    plan = build_read_plan(rules, directory)

    def failing_reader(read):
        raise RuntimeError("boom")

    results = plan.execute(failing_reader)
    assert results["r1"].value is None
    assert "boom" in results["r1"].error
