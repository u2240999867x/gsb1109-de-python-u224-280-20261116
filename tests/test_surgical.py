import json

import pytest

from cbor2 import dumps, loads
from cbor2.surgical import (
    BlockingError,
    OriginalChangedError,
    SurgicalError,
    apply_edit,
    main,
    plan_edit,
)


def shareable(payload: bytes) -> bytes:
    return b"\xd8\x1c" + payload


def sharedref(index: int) -> bytes:
    assert index < 24
    return b"\xd8\x1d" + bytes([index])


# {"a": [1, 2, 3], "b": shareable("hello"), "c": sharedref(0), "d": 10}
DOC = (
    b"\xa4"
    b"\x61a\x83\x01\x02\x03"
    b"\x61b" + shareable(b"\x65hello") + b"\x61c" + sharedref(0) + b"\x61d\x0a"
)

# Offsets in DOC
OFF_A = 3  # [1, 2, 3]
OFF_B = 9  # tag 28 item
OFF_B_CONTENT = 11  # "hello"
OFF_C = 19  # sharedref(0)
OFF_D = 24  # 10

# [shareable("x"), shareable("y"), sharedref(1)]
DOC_TWO_SHARED = b"\x83" + shareable(b"\x61x") + shareable(b"\x61y") + sharedref(1)

# {"list": shareable([10, 20, 30]), "again": sharedref(0)}
DOC_SHARED_LIST = (
    b"\xa2\x64list" + shareable(b"\x83\x0a\x14\x18\x1e") + b"\x65again" + sharedref(0)
)


def test_plan_lists_rewrite_scope():
    plan = plan_edit(DOC, "/d", "occurrence", dumps(11))
    assert plan["target"]["start"] == OFF_D
    assert plan["target"]["end"] == OFF_D + 1
    assert plan["target"]["hex"] == "0a"
    assert plan["delta"] == 0
    assert plan["shareables"]["defined_in_span"] == []
    assert plan["references"]["removed_in_span"] == []
    assert plan["changed_byte_estimate"] == 1
    # the plan must be serializable so it can be reviewed before applying
    json.dumps(plan)


def test_occurrence_scalar_edit_changes_one_byte():
    plan = plan_edit(DOC, "/d", "occurrence", dumps(11))
    new, report = apply_edit(DOC, plan)
    assert report["ok"]
    assert report["changed_bytes"] == 1
    assert len(new) == len(DOC)
    assert new[:OFF_D] == DOC[:OFF_D]
    assert new[OFF_D + 1 :] == DOC[OFF_D + 1 :]
    assert loads(new)["d"] == 11
    assert loads(new)["b"] == "hello"


def test_occurrence_length_change_preserves_surrounding_bytes():
    replacement = dumps([1, 2, 3, 4, 5])
    plan = plan_edit(DOC, "/a", "occurrence", replacement)
    assert plan["target"]["start"] == OFF_A
    assert plan["delta"] == len(replacement) - 4
    new, report = apply_edit(DOC, plan)
    assert report["ok"]
    assert new[:OFF_A] == DOC[:OFF_A]
    assert new[OFF_A + len(replacement) :] == DOC[OFF_A + 4 :]
    decoded = loads(new)
    assert decoded["a"] == [1, 2, 3, 4, 5]
    assert decoded["d"] == 10
    assert decoded["c"] == "hello"


def test_occurrence_replaces_single_sharedref_only():
    plan = plan_edit(DOC, "/c", "occurrence", dumps("world"))
    assert plan["references"]["removed_in_span"] == [OFF_C]
    new, report = apply_edit(DOC, plan)
    assert report["ok"]
    decoded = loads(new)
    assert decoded["c"] == "world"
    assert decoded["b"] == "hello"


def test_occurrence_inside_shared_value_is_rejected():
    with pytest.raises(SurgicalError, match="shared value #0"):
        plan_edit(DOC, "/b/0", "occurrence", dumps("hi"))


def test_shared_edit_via_reference_updates_all_occurrences():
    plan = plan_edit(DOC, "/c", "shared", dumps("hi"))
    assert plan["target"]["start"] == OFF_B_CONTENT
    assert plan["target"]["shared_index"] == 0
    assert plan["references"]["affected_by_shared_edit"] == [OFF_C]
    new, report = apply_edit(DOC, plan)
    assert report["ok"]
    # the tag 28 head is outside the authorized span and stays put
    assert new[OFF_B : OFF_B + 2] == b"\xd8\x1c"
    decoded = loads(new)
    assert decoded["b"] == "hi"
    assert decoded["c"] == "hi"
    assert decoded["b"] is decoded["c"]


def test_shared_edit_via_definition_matches_reference_edit():
    via_ref = plan_edit(DOC, "/c", "shared", dumps("hi"))
    via_def = plan_edit(DOC, "/b", "shared", dumps("hi"))
    assert via_ref["target"]["start"] == via_def["target"]["start"]
    assert via_ref["target"]["end"] == via_def["target"]["end"]


def test_shared_partial_edit_of_shared_list():
    plan = plan_edit(DOC_SHARED_LIST, "/list/0/1", "shared", dumps(99))
    assert plan["target"]["shared_index"] == 0
    new, report = apply_edit(DOC_SHARED_LIST, plan)
    assert report["ok"]
    decoded = loads(new)
    assert decoded["list"] == [10, 99, 30]
    assert decoded["again"] == [10, 99, 30]
    assert decoded["list"] is decoded["again"]


def test_shared_mode_requires_a_shared_target():
    with pytest.raises(SurgicalError, match="mode 'shared' requires"):
        plan_edit(DOC, "/d", "shared", dumps(11))


def test_blocking_dangling_reference():
    # replacing the shareable definition in "occurrence" mode would leave
    # the reference at /c pointing at a value that no longer exists
    with pytest.raises(BlockingError) as excinfo:
        plan_edit(DOC, "/b", "occurrence", dumps("x"))
    relations = excinfo.value.relations
    assert [r["kind"] for r in relations] == ["dangling-reference"]
    assert relations[0]["offset"] == OFF_C
    assert relations[0]["references_shareable"] == 0
    assert "hint" in relations[0]


def test_blocking_renumbered_reference():
    # removing shareable #0 would renumber shareable #1, forcing a rewrite
    # of the reference to it outside the authorized span
    with pytest.raises(BlockingError) as excinfo:
        plan_edit(DOC_TWO_SHARED, "/0", "occurrence", dumps("z"))
    relations = excinfo.value.relations
    assert [r["kind"] for r in relations] == ["renumbered-reference"]
    assert relations[0]["references_shareable"] == 1


def test_unreferenced_renumbering_is_allowed():
    # without any references, removing a shareable definition renumbers
    # nothing observable
    doc = b"\x82" + shareable(b"\x61x") + shareable(b"\x61y")
    plan = plan_edit(doc, "/0", "occurrence", dumps("z"))
    assert plan["shareables"]["index_shift"] == -1
    new, report = apply_edit(doc, plan)
    assert report["ok"]
    assert loads(new) == ["z", "y"]


def test_blocking_invalid_replacement_reference():
    # the replacement references shareable #5, which will not exist
    with pytest.raises(BlockingError) as excinfo:
        plan_edit(DOC, "/d", "occurrence", sharedref(5))
    relations = excinfo.value.relations
    assert [r["kind"] for r in relations] == ["invalid-replacement-reference"]


def test_replacement_may_reference_existing_shareable():
    plan = plan_edit(DOC, "/d", "occurrence", sharedref(0))
    new, report = apply_edit(DOC, plan)
    assert report["ok"]
    decoded = loads(new)
    assert decoded["d"] == "hello"
    assert decoded["d"] is decoded["b"]


def test_replacement_must_be_a_single_item():
    with pytest.raises(SurgicalError, match="single CBOR data item"):
        plan_edit(DOC, "/d", "occurrence", b"\x01\x02")


def test_original_changed_stops_apply():
    plan = plan_edit(DOC, "/d", "occurrence", dumps(11))
    tampered = DOC[:OFF_D] + b"\x18\x2a" + DOC[OFF_D + 1 :]
    with pytest.raises(OriginalChangedError):
        apply_edit(tampered, plan)


def test_path_resolution_errors():
    with pytest.raises(SurgicalError):
        plan_edit(DOC, "/missing", "occurrence", dumps(1))
    with pytest.raises(SurgicalError):
        plan_edit(DOC, "/a/9", "occurrence", dumps(1))
    with pytest.raises(SurgicalError):
        plan_edit(DOC, "/c/0", "occurrence", dumps(1))  # cannot descend a ref


def test_cli_roundtrip(tmp_path, capsys):
    input_file = tmp_path / "in.cbor"
    plan_file = tmp_path / "plan.json"
    output_file = tmp_path / "out.cbor"
    input_file.write_bytes(DOC)

    main(
        [
            "plan",
            str(input_file),
            "--path",
            "/d",
            "--value-json",
            "11",
            "-o",
            str(plan_file),
        ]
    )
    plan = json.loads(plan_file.read_text())
    assert plan["target"]["start"] == OFF_D

    main(["apply", str(plan_file), "-o", str(output_file)])
    report = json.loads(capsys.readouterr().out)
    assert report["ok"]
    assert loads(output_file.read_bytes())["d"] == 11


def test_cli_plan_blocked_exits_without_writing(tmp_path, capsys):
    input_file = tmp_path / "in.cbor"
    plan_file = tmp_path / "plan.json"
    input_file.write_bytes(DOC)
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "plan",
                str(input_file),
                "--path",
                "/b",
                "--value-json",
                '"x"',
                "-o",
                str(plan_file),
            ]
        )
    assert excinfo.value.code == 2
    assert not plan_file.exists()
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "blocked"
    assert out["blocking"][0]["kind"] == "dangling-reference"


def test_cli_apply_detects_changed_original(tmp_path):
    input_file = tmp_path / "in.cbor"
    plan_file = tmp_path / "plan.json"
    output_file = tmp_path / "out.cbor"
    input_file.write_bytes(DOC)
    main(
        [
            "plan",
            str(input_file),
            "--path",
            "/d",
            "--value-json",
            "11",
            "-o",
            str(plan_file),
        ]
    )
    input_file.write_bytes(DOC + b"\x00")  # original version changed
    with pytest.raises(SystemExit) as excinfo:
        main(["apply", str(plan_file), "-o", str(output_file)])
    assert excinfo.value.code != 0
    assert not output_file.exists()
