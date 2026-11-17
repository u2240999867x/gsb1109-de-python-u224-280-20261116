from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from dateparser.retention import (
    RULE_CALENDAR_DAYS,
    RULE_MONTH_END,
    RULE_SETTLEMENT_BUSINESS_DAYS,
    RetentionInputError,
    correction_from_dict,
    plan_from_dict,
)
from dateparser.retention.__main__ import main
from dateparser.retention.model import parse_retention_input

BASE_INPUT = {
    "calendar": {
        "id": "test-calendar",
        "timezone": "Asia/Shanghai",
        "working_weekdays": ["monday", "tuesday", "wednesday", "thursday", "friday"],
        "holidays": {
            "2026-10-01": "National Day",
            "2026-10-02": "National Day",
            "2026-10-05": "National Day holiday",
            "2026-10-06": "National Day holiday",
            "2026-10-07": "National Day holiday",
        },
        "working_days": {"2026-10-10": "adjusted working Saturday"},
    },
    "policy": {
        "rules": [
            {"id": "R1", "raw_text": "次月月底", "kind": RULE_MONTH_END},
            {
                "id": "R2",
                "raw_text": "采集后30天",
                "kind": RULE_CALENDAR_DAYS,
                "days": 30,
            },
            {
                "id": "R3",
                "raw_text": "结清后第三个工作日",
                "kind": RULE_SETTLEMENT_BUSINESS_DAYS,
                "days": 3,
            },
        ]
    },
    "projects": [
        {"id": "alpha", "rule_ids": ["R1", "R2", "R3"]},
        {"id": "beta", "rule_ids": ["R1", "R3"]},
        {"id": "gamma", "rule_ids": ["R2"]},
    ],
    "materials": [],
}


def make_input(**overrides):
    data = json.loads(json.dumps(BASE_INPUT))
    data.update(overrides)
    return data


def plan(data, as_of="2026-11-18T00:00:00+08:00", generated_at=None):
    generated = generated_at or as_of
    return plan_from_dict(data, as_of=as_of, generated_at=generated)


def material(material_id, **overrides):
    base = {
        "id": material_id,
        "kind": "original",
        "project": "alpha",
        "collected_at": "2026-09-29T14:20:00+08:00",
    }
    base.update(overrides)
    return base


class TestMonthEndRule:
    def test_next_month_end_is_a_calendar_date_not_a_duration(self):
        data = make_input(
            materials=[
                material(
                    "m1", collected_at="2026-01-31T23:30:00+08:00", rule_ids=["R1"]
                )
            ]
        )
        result = plan(data, as_of="2026-03-01T00:00:00+08:00")
        item = result.materials["m1"]
        assert item.status == "reclaimable"
        assert item.reclaim_from == datetime.fromisoformat("2026-03-01T00:00:00+08:00")
        basis = item.requirements[0].window.basis
        assert "2026-02-28" in basis

    def test_collection_period_uses_the_named_month(self):
        data = make_input(
            materials=[
                material(
                    "m1",
                    collected_at="2026-01-03T09:00:00+08:00",
                    collection_period="2026-02",
                    rule_ids=["R1"],
                )
            ]
        )
        result = plan(data, as_of="2026-03-01T00:00:00+08:00")
        window = result.materials["m1"].requirements[0].window
        assert window.end == datetime.fromisoformat("2026-04-01T00:00:00+08:00")

    def test_december_collection_rolls_into_next_year(self):
        data = make_input(
            materials=[
                material(
                    "m1", collected_at="2026-12-10T08:00:00+08:00", rule_ids=["R1"]
                )
            ]
        )
        result = plan(data, as_of="2027-01-01T00:00:00+08:00")
        window = result.materials["m1"].requirements[0].window
        assert window.end == datetime.fromisoformat("2027-02-01T00:00:00+08:00")


class TestCalendarDayRule:
    def test_calendar_days_preserve_local_wall_clock(self):
        data = make_input(
            materials=[
                material(
                    "m1", collected_at="2026-10-01T09:00:00+08:00", rule_ids=["R2"]
                )
            ]
        )
        result = plan(data)
        window = result.materials["m1"].requirements[0].window
        assert window.end == datetime.fromisoformat("2026-10-31T09:00:00+08:00")

    def test_calendar_days_do_not_convert_to_hours_across_a_dst_change(self):
        data = json.loads(json.dumps(BASE_INPUT))
        data["calendar"]["timezone"] = "America/New_York"
        data["calendar"].pop("holidays")
        data["calendar"].pop("working_days")
        data["projects"] = [{"id": "alpha", "rule_ids": ["R2"]}]
        data["materials"] = [
            material(
                "m1",
                collected_at="2026-02-15T09:00:00-05:00",
                rule_ids=["R2"],
            )
        ]
        result = plan(data, as_of="2026-03-20T00:00:00-04:00")
        window = result.materials["m1"].requirements[0].window
        assert window.end == datetime.fromisoformat("2026-03-17T09:00:00-04:00")
        naive_hours = window.start.replace(tzinfo=None) + (window.end - window.start)
        assert naive_hours.hour == 9

    def test_calendar_day_rule_ignores_business_calendar(self):
        data = make_input(
            materials=[
                material(
                    "m1", collected_at="2026-09-30T12:00:00+08:00", rule_ids=["R2"]
                )
            ]
        )
        result = plan(data)
        window = result.materials["m1"].requirements[0].window
        assert window.end == datetime.fromisoformat("2026-10-30T12:00:00+08:00")


class TestSettlementRule:
    def test_business_days_skip_weekends_and_holidays(self):
        data = make_input(
            materials=[
                material(
                    "m1",
                    collected_at="2026-09-20T10:00:00+08:00",
                    settlement_at="2026-09-30T16:00:00+08:00",
                    rule_ids=["R3"],
                )
            ]
        )
        result = plan(data, as_of="2026-10-01T00:00:00+08:00")
        window = result.materials["m1"].requirements[0].window
        notes = " ".join(window.notes)
        assert "2026-10-01 (National Day)" in notes
        assert "non-working weekday Saturday" in notes
        assert window.end == datetime.fromisoformat("2026-10-11T00:00:00+08:00")

    def test_adjusted_working_weekend_is_counted(self):
        data = make_input(
            materials=[
                material(
                    "m1",
                    collected_at="2026-09-20T10:00:00+08:00",
                    settlement_at="2026-10-08T16:00:00+08:00",
                    rule_ids=["R3"],
                )
            ]
        )
        result = plan(data, as_of="2026-10-01T00:00:00+08:00")
        window = result.materials["m1"].requirements[0].window
        assert window.end == datetime.fromisoformat("2026-10-13T00:00:00+08:00")
        assert any(
            "2026-10-10 (adjusted working Saturday)" in note for note in window.notes
        )

    def test_missing_settlement_anchor_stays_pending(self):
        data = make_input(materials=[material("m1", rule_ids=["R3"])])
        result = plan(data, as_of="2027-01-01T00:00:00+08:00")
        item = result.materials["m1"]
        assert item.status == "pending"
        assert item.reclaim_from is None
        requirement = item.requirements[0]
        assert requirement.window.is_pending
        assert requirement.window.end is None
        assert "settlement" in (requirement.window.pending or "")
        assert item.material.collected_at == datetime.fromisoformat(
            "2026-09-29T14:20:00+08:00"
        )

    def test_pending_rule_never_falls_back_to_collection(self):
        data = make_input(materials=[material("m1", rule_ids=["R3"])])
        result = plan(data, as_of="2030-01-01T00:00:00+08:00")
        item = result.materials["m1"]
        assert item.reclaim_from is None
        assert item.status == "pending"
        assert all(requirement.window.end is None for requirement in item.requirements)


class TestDependencyPropagation:
    def test_derived_material_extends_the_original_window(self):
        data = make_input(
            materials=[
                material("orig", rule_ids=["R2"]),
                material(
                    "derived",
                    kind="derived",
                    collected_at="2026-09-30T09:00:00+08:00",
                    depends_on=["orig"],
                    rule_ids=["R1"],
                ),
            ]
        )
        result = plan(data, as_of="2026-11-18T00:00:00+08:00")
        original = result.materials["orig"]
        assert original.own_reclaim_from == datetime.fromisoformat(
            "2026-10-29T14:20:00+08:00"
        )
        assert original.reclaim_from == datetime.fromisoformat(
            "2026-11-01T00:00:00+08:00"
        )
        assert original.reclaim_from > original.own_reclaim_from
        assert original.final_rule_id == "R1"

    def test_dependency_chain_propagates_to_the_root(self):
        data = make_input(
            materials=[
                material("root", rule_ids=["R2"]),
                material(
                    "middle",
                    kind="derived",
                    depends_on=["root"],
                    rule_ids=["R2"],
                ),
                material(
                    "leaf",
                    kind="derived",
                    depends_on=["middle"],
                    rule_ids=["R1"],
                ),
            ]
        )
        result = plan(data, as_of="2026-11-18T00:00:00+08:00")
        assert result.materials["root"].reclaim_from == datetime.fromisoformat(
            "2026-11-01T00:00:00+08:00"
        )
        assert result.materials["middle"].final_rule_id == "R1"

    def test_diamond_dependency_shares_one_evaluation(self):
        data = make_input(
            materials=[
                material("root", rule_ids=["R2"]),
                material("left", kind="derived", depends_on=["root"], rule_ids=["R1"]),
                material("right", kind="derived", depends_on=["root"], rule_ids=["R2"]),
                material(
                    "bottom",
                    kind="derived",
                    depends_on=["left", "right"],
                    rule_ids=["R2"],
                ),
            ]
        )
        result = plan(data, as_of="2026-11-18T00:00:00+08:00")
        root = result.materials["root"]
        assert root.reclaim_from == datetime.fromisoformat("2026-11-01T00:00:00+08:00")
        assert root.final_rule_id == "R1"

    def test_pending_dependent_keeps_the_original_pending(self):
        data = make_input(
            materials=[
                material("orig", rule_ids=["R2"]),
                material(
                    "derived",
                    kind="derived",
                    depends_on=["orig"],
                    rule_ids=["R3"],
                ),
            ]
        )
        result = plan(data, as_of="2030-01-01T00:00:00+08:00")
        assert result.materials["orig"].status == "pending"
        assert result.materials["derived"].status == "pending"
        assert result.materials["orig"].dependency_requirements[0].window.is_pending


class TestSharedOriginals:
    def test_shared_original_waits_for_every_project_requirement(self):
        data = make_input(
            materials=[material("shared", shared_with=["beta"], rule_ids=["R2"])],
            projects=[
                {"id": "alpha", "rule_ids": ["R2"]},
                {"id": "beta", "rule_ids": ["R1"]},
            ],
        )
        result = plan(data, as_of="2026-11-18T00:00:00+08:00")
        item = result.materials["shared"]
        assert item.is_shared_original
        assert item.final_rule_id == "R1"
        assert item.reclaim_from == datetime.fromisoformat("2026-11-01T00:00:00+08:00")
        assert item.status == "reclaimable"
        assert {requirement.project for requirement in item.requirements} == {
            "alpha",
            "beta",
        }

    def test_shared_original_is_reclaimable_only_after_all_releases(self):
        data = make_input(
            materials=[material("shared", shared_with=["beta"], rule_ids=["R2"])],
            projects=[
                {"id": "alpha", "rule_ids": ["R2"]},
                {"id": "beta", "rule_ids": ["R1"]},
            ],
        )
        early = plan(data, as_of="2026-10-20T00:00:00+08:00")
        late = plan(data, as_of="2026-11-02T00:00:00+08:00")
        assert "shared" not in early.reclaimable()
        assert "shared" in late.reclaimable()

    def test_one_pending_project_requirement_blocks_reclaim(self):
        data = make_input(
            materials=[material("shared", shared_with=["beta"], rule_ids=["R1"])],
            projects=[
                {"id": "alpha", "rule_ids": ["R1"]},
                {"id": "beta", "rule_ids": ["R3"]},
            ],
        )
        result = plan(data, as_of="2030-01-01T00:00:00+08:00")
        assert result.materials["shared"].status == "pending"
        assert "shared" not in result.reclaimable()

    def test_shared_original_inferred_from_dependent_projects(self):
        data = make_input(
            materials=[
                material("orig", project="alpha", rule_ids=["R2"]),
                material(
                    "audit-copy",
                    kind="derived",
                    project="gamma",
                    depends_on=["orig"],
                    rule_ids=["R2"],
                ),
            ],
        )
        result = plan(data, as_of="2026-11-18T00:00:00+08:00")
        assert result.materials["orig"].material.shared_with == ("gamma",)

    def test_explicit_share_applies_the_sharing_project_rules(self):
        data = make_input(
            materials=[material("shared", shared_with=["beta"], rule_ids=["R2"])],
            projects=[
                {"id": "alpha", "rule_ids": ["R2"]},
                {"id": "beta", "rule_ids": ["R1"]},
            ],
        )
        result = plan(data, as_of="2026-11-18T00:00:00+08:00")
        beta_requirements = [
            requirement
            for requirement in result.materials["shared"].requirements
            if requirement.project == "beta"
        ]
        assert [requirement.window.rule_id for requirement in beta_requirements] == [
            "R1"
        ]

    def test_dependency_does_not_import_the_dependent_project_rules(self):
        data = make_input(
            materials=[
                material("orig", project="alpha", rule_ids=["R2"]),
                material(
                    "audit-copy",
                    kind="derived",
                    project="gamma",
                    depends_on=["orig"],
                    rule_ids=["R2"],
                ),
            ],
            projects=[
                {"id": "alpha", "rule_ids": ["R2"]},
                {"id": "gamma", "rule_ids": ["R1", "R2"]},
            ],
        )
        result = plan(data, as_of="2026-11-18T00:00:00+08:00")
        original = result.materials["orig"]
        assert {requirement.project for requirement in original.requirements} == {
            "alpha"
        }
        assert original.final_rule_id == "R2"


class TestPlanOutput:
    def test_plan_is_json_serializable_and_has_stable_calendar_fingerprint(self):
        data = make_input(materials=[material("m1")])
        first = plan(data)
        second = plan(data)
        assert first.calendar_fingerprint == second.calendar_fingerprint
        payload = first.to_dict()
        assert payload["calendar"]["fingerprint"] == first.calendar_fingerprint
        assert json.loads(json.dumps(payload))["summary"]["total"] == 1

    def test_calendar_fingerprint_changes_with_a_holiday(self):
        data = make_input(materials=[material("m1")])
        changed = json.loads(json.dumps(data))
        changed["calendar"]["holidays"]["2026-11-17"] = "new holiday"
        assert plan(data).calendar_fingerprint != plan(changed).calendar_fingerprint

    def test_as_of_controls_held_and_reclaimable(self):
        data = make_input(materials=[material("m1", rule_ids=["R2"])])
        early = plan(data, as_of="2026-10-01T00:00:00+08:00")
        late = plan(data, as_of="2026-10-30T00:00:00+08:00")
        assert early.materials["m1"].status == "held"
        assert late.materials["m1"].status == "reclaimable"

    def test_original_collection_time_is_preserved_verbatim(self):
        data = make_input(materials=[material("m1")])
        result = plan(data)
        payload = json.loads(json.dumps(result.to_dict()))
        assert payload["materials"][0]["collected_at"] == "2026-09-29T14:20:00+08:00"

    def test_input_is_not_mutated(self):
        data = make_input(materials=[material("m1")])
        snapshot = json.loads(json.dumps(data))
        plan(data)
        assert data == snapshot


class TestValidation:
    def test_unknown_dependency_is_rejected(self):
        data = make_input(
            materials=[
                material(
                    "derived",
                    kind="derived",
                    depends_on=["missing"],
                )
            ]
        )
        with pytest.raises(RetentionInputError, match="unknown material"):
            parse_retention_input(data)

    def test_dependency_cycle_is_rejected(self):
        data = make_input(
            materials=[
                material("a", kind="derived", depends_on=["b"]),
                material("b", kind="derived", depends_on=["a"]),
            ]
        )
        with pytest.raises(RetentionInputError, match="cycle"):
            parse_retention_input(data)

    def test_missing_collection_time_is_rejected(self):
        data = make_input(materials=[{"id": "m1", "project": "alpha"}])
        with pytest.raises(RetentionInputError, match="collected_at"):
            parse_retention_input(data)

    def test_holiday_and_working_day_overlap_is_rejected(self):
        data = make_input(materials=[material("m1")])
        data["calendar"]["working_days"]["2026-10-01"] = "clash"
        with pytest.raises(RetentionInputError, match="both holidays and working days"):
            parse_retention_input(data)

    def test_unknown_calendar_timezone_is_rejected(self):
        data = make_input(materials=[material("m1")])
        data["calendar"]["timezone"] = "Mars/Olympus"
        with pytest.raises(RetentionInputError, match="timezone"):
            parse_retention_input(data)

    def test_empty_business_week_is_rejected(self):
        data = make_input(materials=[material("m1")])
        data["calendar"]["working_weekdays"] = []
        with pytest.raises(RetentionInputError, match="working_weekdays"):
            parse_retention_input(data)

    def test_unknown_rule_kind_is_rejected(self):
        data = make_input(materials=[material("m1")])
        data["policy"]["rules"].append({"id": "bad", "kind": "whenever"})
        with pytest.raises(RetentionInputError, match="unknown retention rule kind"):
            parse_policy_error(data)


def parse_policy_error(data):
    return parse_retention_input(data)


class TestCorrection:
    def test_policy_correction_lists_affected_materials(self):
        data = make_input(
            materials=[
                material("m1", rule_ids=["R2"]),
                material("m2", collected_at="2026-09-01T08:00:00+08:00"),
            ]
        )
        changes = {
            "rules": [
                {"id": "R1", "raw_text": "次月月底", "kind": RULE_MONTH_END},
                {
                    "id": "R2",
                    "raw_text": "采集后45天",
                    "kind": RULE_CALENDAR_DAYS,
                    "days": 45,
                },
                {
                    "id": "R3",
                    "raw_text": "结清后第三个工作日",
                    "kind": RULE_SETTLEMENT_BUSINESS_DAYS,
                    "days": 3,
                },
            ]
        }
        report = correction_from_dict(
            data,
            changes,
            as_of="2026-11-18T00:00:00+08:00",
            generated_at="2026-11-18T00:00:00+08:00",
        )
        assert "m1" in report.affected_materials()
        assert "m2" not in report.affected_materials()
        change = next(item for item in report.changes if item.material_id == "m1")
        assert change.before_reclaim_from == datetime.fromisoformat(
            "2026-10-29T14:20:00+08:00"
        )
        assert change.after_reclaim_from == datetime.fromisoformat(
            "2026-11-13T14:20:00+08:00"
        )
        assert change.to_dict()["change"] == "reclaim_later"

    def test_correction_does_not_rewrite_collection_times(self):
        data = make_input(materials=[material("m1")])
        changes = {
            "materials": [
                {
                    "id": "m1",
                    "collected_at": "2020-01-01T00:00:00+08:00",
                    "rule_ids": ["R2"],
                }
            ]
        }
        report = correction_from_dict(
            data,
            changes,
            as_of="2026-11-18T00:00:00+08:00",
            generated_at="2026-11-18T00:00:00+08:00",
        )
        assert report.collection_times_unchanged
        assert report.rejected_changes
        assert report.after.materials[
            "m1"
        ].material.collected_at == datetime.fromisoformat("2026-09-29T14:20:00+08:00")

    def test_correction_reports_rule_selection_changes(self):
        data = make_input(materials=[material("m1", rule_ids=["R2"])])
        changes = {"materials": [{"id": "m1", "rule_ids": ["R1"]}]}
        report = correction_from_dict(
            data,
            changes,
            as_of="2026-11-18T00:00:00+08:00",
            generated_at="2026-11-18T00:00:00+08:00",
        )
        change = report.changes[0]
        assert change.before_rules == ["R2"]
        assert change.after_rules == ["R1"]

    def test_unsupported_change_field_is_rejected(self):
        data = make_input(materials=[material("m1")])
        with pytest.raises(RetentionInputError, match="unsupported fields"):
            correction_from_dict(
                data,
                {"everything": True},
                as_of="2026-11-18T00:00:00+08:00",
                generated_at="2026-11-18T00:00:00+08:00",
            )


class TestCli:
    def test_plan_command_writes_json(self, tmp_path: Path):
        source = tmp_path / "input.json"
        source.write_text(json.dumps(make_input(materials=[material("m1")])))
        output = tmp_path / "plan.json"
        code = main(
            [
                "plan",
                "--input",
                str(source),
                "--as-of",
                "2026-11-18T00:00:00+08:00",
                "--output",
                str(output),
            ]
        )
        assert code == 0
        payload = json.loads(output.read_text())
        assert payload["kind"] == "retention_plan"
        assert payload["materials"][0]["id"] == "m1"

    def test_plan_command_reports_errors(self, tmp_path: Path, capsys):
        source = tmp_path / "input.json"
        source.write_text(json.dumps({"materials": []}))
        code = main(["plan", "--input", str(source)])
        assert code == 2
        assert "error:" in capsys.readouterr().err
