# -*- coding: utf-8 -*-
# Copyright: (c) 2021, Jordan Borean (@jborean93) <jborean93@gmail.com>
# MIT License (see LICENSE or https://opensource.org/licenses/MIT)

import uuid

import pytest

import psrpcore
from psrpcore import (
    ProgressActivity,
    ProgressCoverage,
    ProgressKey,
    ProgressStatus,
    ProgressTree,
    ProgressView,
    ProgressRecordEvent,
)
from psrpcore.types import ProgressRecord, ProgressRecordMsg, ProgressRecordType


def _event(
    record: ProgressRecord,
    runspace_pool_id: uuid.UUID = uuid.UUID(int=1),
    pipeline_id: uuid.UUID = uuid.UUID(int=2),
) -> ProgressRecordEvent:
    msg = ProgressRecordMsg(
        Activity=record.Activity,
        ActivityId=record.ActivityId,
        StatusDescription=record.StatusDescription,
        CurrentOperation=record.CurrentOperation,
        ParentActivityId=record.ParentActivityId,
        PercentComplete=record.PercentComplete,
        Type=record.RecordType,
        SecondsRemaining=record.SecondsRemaining,
    )
    return psrpcore.ProgressRecordEvent(
        psrpcore.types.PSRPMessageType.ProgressRecord,
        msg,
        runspace_pool_id,
        pipeline_id,
    )


def test_progress_view_exports_public_types():
    assert psrpcore.ProgressView is ProgressView
    assert psrpcore.ProgressTree is ProgressTree
    assert psrpcore.ProgressActivity is ProgressActivity
    assert psrpcore.ProgressKey is ProgressKey
    assert psrpcore.ProgressStatus is ProgressStatus
    assert psrpcore.ProgressCoverage is ProgressCoverage


def test_progress_view_keeps_sources_and_pipelines_separate():
    view = ProgressView()

    source_a = uuid.UUID(int=1)
    source_b = uuid.UUID(int=2)
    pipeline_a = uuid.UUID(int=10)
    pipeline_b = uuid.UUID(int=11)

    view.add(
        source=source_a,
        pipeline_id=pipeline_a,
        activity_id=1,
        activity="copy a",
        percent_complete=20,
    )
    view.add(
        source=source_a,
        pipeline_id=pipeline_b,
        activity_id=1,
        activity="copy b",
        percent_complete=40,
    )
    view.add(
        source=source_b,
        pipeline_id=pipeline_a,
        activity_id=1,
        activity="copy c",
        percent_complete=60,
    )

    assert view.get(source_a, pipeline_a, 1).activity == "copy a"
    assert view.get(source_a, pipeline_b, 1).activity == "copy b"
    assert view.get(source_b, pipeline_a, 1).activity == "copy c"

    trees = view.trees()
    assert len(trees) == 3
    assert all(len(tree.activities) == 1 for tree in trees)


def test_progress_view_distinguishes_no_update_unknown_percent_and_completed():
    view = ProgressView()
    source = uuid.UUID(int=1)
    pipeline = uuid.UUID(int=2)

    view.expect(source, pipeline, 1, activity="waiting")
    view.add(source=source, pipeline_id=pipeline, activity_id=2, activity="unknown", percent_complete=-1)
    view.add(source=source, pipeline_id=pipeline, activity_id=3, activity="done", completed=True)

    waiting = view.get(source, pipeline, 1)
    unknown = view.get(source, pipeline, 2)
    done = view.get(source, pipeline, 3)

    assert waiting.status == ProgressStatus.NO_UPDATE
    assert waiting.received is False
    assert waiting.percent_complete is None

    assert unknown.status == ProgressStatus.UNKNOWN_PERCENT
    assert unknown.received is True
    assert unknown.percent_complete is None

    assert done.status == ProgressStatus.COMPLETED
    assert done.completed is True
    assert done.percent_complete is None
    assert done.effective_percent == 100.0

    assert len({waiting.status, unknown.status, done.status}) == 3


def test_completed_parent_does_not_complete_children():
    view = ProgressView()
    source = uuid.UUID(int=1)

    view.add(source=source, pipeline_id=None, activity_id=1, activity="parent", completed=True)
    view.add(
        source=source,
        pipeline_id=None,
        activity_id=2,
        activity="child",
        parent_activity_id=1,
        percent_complete=10,
    )

    tree = view.tree(source=source)

    assert tree.get(1).status == ProgressStatus.COMPLETED
    assert tree.get(2).status == ProgressStatus.IN_PROGRESS
    assert tree.get(2).completed is False
    assert tree.coverage.complete is False
    assert tree.coverage.weighted_percent is None


def test_progress_view_activity_number_reuse_uses_generation_evidence():
    view = ProgressView()
    source = uuid.UUID(int=1)
    pipeline = uuid.UUID(int=2)

    view.add(
        source=source,
        pipeline_id=pipeline,
        activity_id=1,
        activity="first",
        percent_complete=100,
        generation="run-1",
    )
    view.add(
        source=source,
        pipeline_id=pipeline,
        activity_id=1,
        activity="second",
        percent_complete=5,
        generation="run-2",
    )

    assert view.get(source, pipeline, 1).activity == "second"
    assert view.generation(source, pipeline, 1) == "run-2"
    assert view.get(source, pipeline, 1, "run-1").activity == "first"
    assert len(view.all_activities) == 2
    assert len(view.activities) == 1


def test_progress_view_older_generation_cannot_replace_newer():
    view = ProgressView()
    source = uuid.UUID(int=1)

    view.add(source=source, pipeline_id=None, activity_id=1, activity="new", percent_complete=80, generation=2)
    assert view.update(
        {
            "Activity": "old",
            "ActivityId": 1,
            "PercentComplete": 20,
            "Type": ProgressRecordType.Processing,
        },
        source=source,
        pipeline_id=None,
        generation=1,
    ) is False

    assert view.get(source, None, 1).activity == "new"
    assert view.generation(source, None, 1) == 2


def test_progress_view_out_of_order_update_cannot_overwrite_confirmed_newer():
    view = ProgressView()
    source = uuid.UUID(int=1)

    view.add(source=source, pipeline_id=None, activity_id=1, activity="newer", percent_complete=90, sequence=10)
    assert view.update(
        {
            "Activity": "older",
            "ActivityId": 1,
            "PercentComplete": 10,
            "Type": ProgressRecordType.Processing,
        },
        source=source,
        pipeline_id=None,
        sequence=9,
    ) is False

    assert view.get(source, None, 1).activity == "newer"
    assert view.get(source, None, 1).percent_complete == 90

    # Equal sequence is also stale; a newer sequence is accepted.
    assert view.update(
        {
            "Activity": "newer again",
            "ActivityId": 1,
            "PercentComplete": 95,
            "Type": ProgressRecordType.Processing,
        },
        source=source,
        pipeline_id=None,
        sequence=11,
    ) is True
    assert view.get(source, None, 1).percent_complete == 95


def test_progress_view_completed_record_is_terminal_for_generation():
    view = ProgressView()
    source = uuid.UUID(int=1)

    view.add(source=source, pipeline_id=None, activity_id=1, activity="done", completed=True, generation="g1")
    assert view.add(
        source=source,
        pipeline_id=None,
        activity_id=1,
        activity="reopened",
        percent_complete=50,
        generation="g1",
    ) is False

    assert view.get(source, None, 1).status == ProgressStatus.COMPLETED
    assert view.add(
        source=source,
        pipeline_id=None,
        activity_id=1,
        activity="new run",
        percent_complete=5,
        generation="g2",
    ) is True
    assert view.get(source, None, 1).status == ProgressStatus.IN_PROGRESS


def test_progress_view_weighted_percent_uses_input_weights():
    view = ProgressView()

    view.add(source="a", pipeline_id=None, activity_id=1, activity="one", percent_complete=0, weight=1)
    view.add(source="a", pipeline_id=None, activity_id=2, activity="two", percent_complete=100, weight=3)

    coverage = view.coverage(source="a")
    assert coverage.complete is True
    assert coverage.covered_weight == 4
    assert coverage.weighted_percent == pytest.approx(75.0)
    assert view.overall_percent(source="a") == pytest.approx(75.0)


def test_progress_view_weighted_percent_requires_sufficient_coverage():
    view = ProgressView()

    view.add(source="a", pipeline_id=None, activity_id=1, activity="one", percent_complete=50, weight=1)
    view.expect("a", None, 2, activity="two", weight=3)

    coverage = view.coverage(source="a")
    assert coverage.complete is False
    assert coverage.weighted_percent is None
    assert coverage.no_update
    assert "no update" in coverage.reason

    coverage = view.coverage(source="a", total_weight=4)
    assert coverage.complete is False
    assert coverage.covered_weight == 1
    assert any("covered weight" in problem for problem in coverage.problems)


def test_progress_view_unknown_percent_blocks_overall_ratio():
    view = ProgressView()

    view.add(source="a", pipeline_id=None, activity_id=1, activity="one", percent_complete=50, weight=1)
    view.add(source="a", pipeline_id=None, activity_id=2, activity="two", percent_complete=-1, weight=1)

    coverage = view.coverage(source="a")
    assert coverage.complete is False
    assert coverage.weighted_percent is None
    assert len(coverage.unknown_percent) == 1


def test_progress_view_missing_parent_tree_still_shows_known_activities():
    view = ProgressView()

    view.add(source="a", pipeline_id=None, activity_id=5, activity="child", parent_activity_id=99, percent_complete=25)
    view.add(source="a", pipeline_id=None, activity_id=6, activity="standalone", percent_complete=50)

    tree = view.tree(source="a")
    assert tree.get(5).activity == "child"
    assert tree.get(6).activity == "standalone"
    assert [a.activity_id for a in tree.orphans] == [5]
    assert 6 in [a.activity_id for a in tree.roots]
    assert tree.coverage.complete is False
    assert tree.coverage.missing_parents

    assert tree.get(5).status == ProgressStatus.IN_PROGRESS


def test_progress_view_cycle_does_not_hide_independent_activity():
    view = ProgressView()

    view.add(source="a", pipeline_id=None, activity_id=1, activity="one", parent_activity_id=2)
    view.add(source="a", pipeline_id=None, activity_id=2, activity="two", parent_activity_id=1)
    view.add(source="a", pipeline_id=None, activity_id=3, activity="independent", percent_complete=70)

    tree = view.tree(source="a")

    assert len(tree.cycles) == 1
    assert {a.activity_id for a in tree.cycles[0]} == {1, 2}
    assert tree.get(3).activity == "independent"
    assert 3 in [a.activity_id for a in tree.roots]
    assert tree.coverage.complete is False
    assert any("cycle" in problem for problem in tree.coverage.problems)


def test_progress_view_tree_scope_and_generation():
    view = ProgressView()

    view.add(
        source="a",
        pipeline_id="p",
        activity_id=1,
        activity="current",
        percent_complete=10,
        generation=2,
        weight=1,
    )
    view.add(
        source="a",
        pipeline_id="p",
        activity_id=1,
        activity="old",
        percent_complete=90,
        generation=1,
        weight=1,
    )

    current = view.tree(source="a", pipeline_id="p")
    old = view.tree(source="a", pipeline_id="p", generation=1)

    assert current.generation == 2
    assert current.get(1).activity == "current"
    assert old.get(1).activity == "old"
    assert old.coverage.weighted_percent == pytest.approx(90.0)


def test_progress_view_update_event_and_from_events():
    record = ProgressRecord(
        Activity="from event",
        ActivityId=3,
        StatusDescription="working",
        PercentComplete=60,
    )
    event = _event(record)

    view = ProgressView.from_events([event])
    activity = view.get(event.runspace_pool_id, event.pipeline_id, 3)

    assert activity.activity == "from event"
    assert activity.status_description == "working"
    assert activity.percent_complete == 60
    assert activity.record.ActivityId == 3


def test_progress_view_add_and_expect_validation():
    view = ProgressView()

    with pytest.raises(ValueError, match="percent_complete"):
        view.add(source="a", pipeline_id=None, activity_id=1, percent_complete=101)

    with pytest.raises(ValueError, match="weight"):
        view.add(source="a", pipeline_id=None, activity_id=1, percent_complete=1, weight=-1)

    with pytest.raises(ValueError, match="source"):
        view.update({"ActivityId": 1})

    assert view.add(source="a", pipeline_id=None, activity_id=1, percent_complete=-1) is True
    assert view.get("a", None, 1).percent_complete is None


def test_progress_view_extend_and_clear():
    view = ProgressView()
    accepted = view.extend(
        [
            {"source": "a", "pipeline_id": None, "activity_id": 1, "percent_complete": 10, "weight": 1, "record": {}},
            {"source": "a", "pipeline_id": None, "activity_id": 2, "percent_complete": 20, "weight": 1, "record": {}},
        ]
    )

    assert accepted == 2
    assert len(view) == 2
    assert view.coverage(source="a").weighted_percent == pytest.approx(15.0)

    view.clear()
    assert len(view) == 0
    assert view.trees() == ()


def test_progress_view_accepts_input_weights_before_records():
    view = ProgressView({("a", None, 1): 1, ("a", None, 2): 3})

    view.add(source="a", pipeline_id=None, activity_id=1, activity="one", percent_complete=0)
    view.add(source="a", pipeline_id=None, activity_id=2, activity="two", percent_complete=100)

    assert view.coverage(source="a").weighted_percent == pytest.approx(75.0)


def test_progress_view_set_weight_after_records():
    view = ProgressView()
    view.add(source="a", pipeline_id=None, activity_id=1, activity="one", percent_complete=40)
    view.add(source="a", pipeline_id=None, activity_id=2, activity="two", percent_complete=80)

    assert view.overall_percent(source="a") is None
    view.set_weight("a", None, 1, 1)
    view.set_weight("a", None, 2, 1)

    assert view.overall_percent(source="a") == pytest.approx(60.0)


def test_progress_view_weighted_percent_across_pipelines():
    view = ProgressView()
    view.add(source="a", pipeline_id="p1", activity_id=1, percent_complete=100, weight=1)
    view.add(source="a", pipeline_id="p2", activity_id=1, percent_complete=0, weight=3)

    coverage = view.coverage(source="a")
    assert coverage.complete is True
    assert coverage.activity_count == 2
    assert coverage.weighted_percent == pytest.approx(25.0)


def test_progress_view_update_accepts_integer_activity_with_fields():
    view = ProgressView()

    assert view.update(
        5,
        source="a",
        pipeline_id=None,
        activity="integer activity",
        percent_complete=30,
        parent_activity_id=1,
        weight=2,
    )
    activity = view.get("a", None, 5)

    assert activity.activity == "integer activity"
    assert activity.percent_complete == 30
    assert activity.parent_activity_id == 1
    assert activity.weight == 2


def test_progress_view_non_comparable_generation_keeps_established_current():
    view = ProgressView()

    view.add(source="a", pipeline_id=None, activity_id=1, activity="first", percent_complete=10, generation=1)
    assert view.add(
        source="a",
        pipeline_id=None,
        activity_id=1,
        activity="second",
        percent_complete=90,
        generation="token-b",
    ) is False

    assert view.get("a", None, 1).activity == "first"
    assert view.generation("a", None, 1) == 1
    assert view.get("a", None, 1, "token-b").activity == "second"


def test_progress_view_explicit_generation_supersedes_generationless_record():
    view = ProgressView()

    view.add(source="a", pipeline_id=None, activity_id=1, activity="without evidence", percent_complete=10)
    assert view.add(
        source="a",
        pipeline_id=None,
        activity_id=1,
        activity="with evidence",
        percent_complete=20,
        generation=1,
    ) is True

    assert view.get("a", None, 1).activity == "with evidence"
    assert view.generation("a", None, 1) == 1


def test_progress_view_mapping_completed_field():
    view = ProgressView()

    view.track({"ActivityId": 1, "Activity": "explicit", "completed": True}, source="a")
    activity = view.get("a", None, 1)

    assert activity.completed is True
    assert activity.status == ProgressStatus.COMPLETED


def test_progress_view_tree_uses_input_weights():
    view = ProgressView({("a", None, 1): 2, ("a", None, 2): 1})
    view.add(source="a", pipeline_id=None, activity_id=1, percent_complete=0)
    view.add(source="a", pipeline_id=None, activity_id=2, percent_complete=100)

    tree = view.tree("a")
    assert tree.percent == pytest.approx(100 / 3)
