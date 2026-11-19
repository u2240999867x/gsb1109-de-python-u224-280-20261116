# Copyright (C) 2026.
# Tests for the schedule_audit toolkit.  Kept outside tests/ so that the
# FTP integration conftest (which needs pyasynchat/pyasyncore) is not
# pulled in.
import json

import pytest

from schedule_audit import (
    EVENT_IO,
    EVENT_WAIT,
    EVENT_WORK,
    Event,
    Material,
    REASON_DEPENDENCY,
    REASON_NETWORK,
    REASON_OTHER,
    REASON_RESOURCE,
    REASON_THROTTLE,
    Resource,
    Trace,
    Transfer,
    audit_trace,
    report_as_dict,
    report_as_text,
)
from schedule_audit.constraints import InconsistentTraceError
from schedule_audit.engine import (
    counterfactual_duration,
    critical_path_guarantee,
)


def make_event(
    name,
    kind=EVENT_WORK,
    start=0.0,
    end=1.0,
    duration_min=None,
    duration_max=None,
    **kwargs,
):
    if duration_min is None:
        duration_min = end - start
    if duration_max is None:
        duration_max = end - start
    return Event(
        name,
        kind,
        start,
        end,
        duration_min,
        duration_max,
        **kwargs,
    )


def simple_trace(
    events,
    materials=None,
    resources=None,
    skew=0.0,
    removed_waits=frozenset(),
    deadline=None,
):
    if materials is None:
        materials = [Material("M", events, deadline=deadline)]
    return Trace(
        materials,
        resources or {},
        clock_skew=skew,
        removed_waits=frozenset(removed_waits),
    )


class TestModelValidation:
    def test_duration_range_must_be_ordered(self):
        with pytest.raises(ValueError):
            make_event("e", start=0, end=1, duration_min=2, duration_max=1)

    def test_negative_values_rejected(self):
        with pytest.raises(ValueError):
            make_event("e", start=-1, end=1)

    def test_wait_requires_reason(self):
        with pytest.raises(ValueError):
            make_event("w", EVENT_WAIT, 0, 2)

    def test_removable_requires_throttle_reason(self):
        with pytest.raises(ValueError):
            make_event(
                "w",
                EVENT_WAIT,
                0,
                2,
                reasons=(REASON_NETWORK,),
                removable_throttle=True,
            )

    def test_non_wait_cannot_carry_reasons(self):
        with pytest.raises(ValueError):
            make_event(
                "w", EVENT_WORK, 0, 2, reasons=(REASON_THROTTLE,)
            )

    def test_io_event_gets_default_transfer(self):
        event = make_event("io", EVENT_IO, 0, 1)
        assert event.transfer == Transfer()

    def test_transfer_accounts_must_be_consistent(self):
        with pytest.raises(ValueError):
            Transfer(wire_bytes=10, payload_bytes=8, retransmit_bytes=4)

    def test_unknown_resource_rejected(self):
        event = make_event(
            "e", start=0, end=1, resources=("worker",)
        )
        with pytest.raises(ValueError):
            simple_trace([event])

    def test_removed_wait_must_be_removable(self):
        wait = make_event(
            "w",
            EVENT_WAIT,
            0,
            2,
            reasons=(REASON_THROTTLE,),
        )
        with pytest.raises(ValueError):
            simple_trace([wait], removed_waits={"w"})

    def test_duplicate_event_names_rejected(self):
        with pytest.raises(ValueError):
            Trace(
                [
                    Material(
                        "A",
                        [make_event("dup", start=0, end=1)],
                    ),
                    Material(
                        "B",
                        [make_event("dup", start=2, end=3)],
                    ),
                ]
            )

    def test_capacity_must_be_positive(self):
        with pytest.raises(ValueError):
            Resource("r", capacity=0)


class TestConstraints:
    def test_duration_incompatible_with_skew_is_inconsistent(self):
        # Clock readings span 1 time unit +/- 0.1, so the true duration
        # is at most 1.2, but the event claims at least 3.
        event = make_event(
            "e", start=0, end=1, duration_min=3, duration_max=3
        )
        trace = simple_trace([event], skew=0.1)
        with pytest.raises(InconsistentTraceError):
            audit_trace(trace)

    def test_proven_order_contradiction_is_inconsistent(self):
        first = make_event("first", start=10, end=12)
        second = make_event("second", start=0, end=2)
        trace = simple_trace([first, second], skew=0.0)
        with pytest.raises(InconsistentTraceError):
            audit_trace(trace)


class TestCounterfactualWaitRules:
    def test_pure_removable_throttle_disappears(self):
        event = make_event(
            "w",
            EVENT_WAIT,
            0,
            3,
            reasons=(REASON_THROTTLE,),
            removable_throttle=True,
        )
        assert counterfactual_duration(event, {"w"}) == (0.0, 0.0)

    def test_mixed_cause_keeps_other_reasons_share(self):
        event = make_event(
            "w",
            EVENT_WAIT,
            0,
            3,
            reasons=(REASON_THROTTLE, REASON_NETWORK),
            removable_throttle=True,
        )
        assert counterfactual_duration(event, {"w"}) == (0.0, 3.0)

    def test_throttle_wait_not_removed_is_untouched(self):
        event = make_event(
            "w",
            EVENT_WAIT,
            0,
            3,
            reasons=(REASON_THROTTLE,),
            removable_throttle=True,
        )
        assert counterfactual_duration(event, frozenset()) == (3.0, 3.0)

    def test_non_removable_throttle_is_untouched(self):
        event = make_event(
            "w", EVENT_WAIT, 0, 3, reasons=(REASON_THROTTLE,)
        )
        assert counterfactual_duration(event, {"w"}) == (3.0, 3.0)

    def test_non_throttle_wait_is_untouched(self):
        event = make_event(
            "w", EVENT_WAIT, 0, 3, reasons=(REASON_DEPENDENCY,)
        )
        assert counterfactual_duration(event, {"w"}) == (3.0, 3.0)


class TestBaselineWindows:
    def test_deterministic_chain_windows_are_exact(self):
        events = [
            make_event("e1", start=0, end=2),
            make_event("e2", start=2, end=5),
            make_event("e3", start=5, end=9),
        ]
        result = audit_trace(simple_trace(events))
        window = result.materials[0].baseline
        assert window["earliest_start"] == pytest.approx(0.0)
        assert window["latest_start"] == pytest.approx(0.0)
        assert window["earliest_end"] == pytest.approx(9.0)
        assert window["latest_end"] == pytest.approx(9.0)

    def test_skew_widens_windows_by_skew(self):
        events = [
            make_event("e1", start=0, end=2),
            make_event("e2", start=2, end=5),
        ]
        result = audit_trace(simple_trace(events, skew=0.25))
        window = result.materials[0].baseline
        assert window["earliest_start"] == pytest.approx(-0.25)
        assert window["latest_start"] == pytest.approx(0.25)
        assert window["earliest_end"] == pytest.approx(4.75)
        assert window["latest_end"] == pytest.approx(5.25)

    def test_duration_range_propagates_to_completion_window(self):
        # With skew the clock no longer pins the end, so the declared
        # duration range sets the completion envelope.
        events = [
            make_event(
                "e1",
                start=0,
                end=2,
                duration_min=1.0,
                duration_max=3.0,
            ),
        ]
        result = audit_trace(simple_trace(events, skew=1.0))
        window = result.materials[0].baseline
        assert window["earliest_end"] == pytest.approx(1.0)
        assert window["latest_end"] == pytest.approx(3.0)


class TestResourceConstraints:
    def _ambiguous_trace(self, capacity, skew=2.0):
        worker = Resource("worker", capacity=capacity)
        material_x = Material(
            "X",
            [
                make_event(
                    "x", start=0, end=2, resources=("worker",)
                ),
            ],
        )
        material_y = Material(
            "Y",
            [
                make_event(
                    "y", start=1, end=3, resources=("worker",)
                ),
            ],
        )
        return Trace(
            [material_x, material_y],
            {"worker": worker},
            clock_skew=skew,
        )

    def test_capacity_one_enumerates_both_orders(self):
        # The clock uncertainty makes both serial orders compatible
        # with the readings, so both must be considered.
        result = audit_trace(self._ambiguous_trace(capacity=1))
        assert result.feasible
        assert result.baseline["orderings_enumerated"] == 2
        assert result.baseline["orderings_considered"] == 2

    def test_capacity_two_keeps_parallel_and_serial_orders(self):
        # Capacity 2 permits parallel work but does not require it, so
        # the parallel assignment and both serial orders all survive.
        result = audit_trace(self._ambiguous_trace(capacity=2))
        assert result.feasible
        assert result.baseline["orderings_enumerated"] == 3
        assert result.baseline["orderings_considered"] == 3

    def test_zero_skew_proves_overlap_and_is_infeasible(self):
        result = audit_trace(
            self._ambiguous_trace(capacity=1, skew=0.0)
        )
        assert not result.feasible

    def test_skew_keeps_overlapping_usage_feasible(self):
        result = audit_trace(
            self._ambiguous_trace(capacity=1, skew=2.0)
        )
        assert result.feasible


class TestTimeoutClassification:
    def _trace_with_deadline(self, end, deadline, skew):
        event = make_event("e", start=0, end=end)
        return audit_trace(
            simple_trace([event], skew=skew, deadline=deadline)
        )

    def test_timeout_proven_when_latest_deadline_exceeded(self):
        result = self._trace_with_deadline(end=10, deadline=8, skew=0.5)
        assert result.materials[0].timeout_state == "timeout_proven"

    def test_within_deadline_proven(self):
        result = self._trace_with_deadline(end=5, deadline=8, skew=0.5)
        assert (
            result.materials[0].timeout_state
            == "within_deadline_proven"
        )

    def test_timeout_possible_under_skew(self):
        result = self._trace_with_deadline(end=8, deadline=8, skew=0.5)
        assert result.materials[0].timeout_state == "timeout_possible"


class TestImprovementAttribution:
    def test_pure_throttle_wait_gives_provable_improvement(self):
        events = [
            make_event("work", start=0, end=2),
            make_event(
                "thr",
                EVENT_WAIT,
                start=2,
                end=5,
                reasons=(REASON_THROTTLE,),
                removable_throttle=True,
            ),
            make_event("io", EVENT_IO, start=5, end=9),
        ]
        result = audit_trace(
            simple_trace(events, removed_waits={"thr"})
        )
        material = result.materials[0]
        assert material.improvement_state == "improvement_proven"
        assert material.guaranteed_reduction == pytest.approx(3.0)
        # The relaxed envelope lets the suffix start as early as the
        # material release: 9 - 6 = 3, never more than the wait itself.
        assert material.max_possible_reduction == pytest.approx(3.0)

    def test_mixed_cause_wait_only_possible_improvement(self):
        events = [
            make_event("work", start=0, end=2),
            make_event(
                "mix",
                EVENT_WAIT,
                start=2,
                end=5,
                reasons=(REASON_THROTTLE, REASON_NETWORK),
                removable_throttle=True,
            ),
            make_event("io", EVENT_IO, start=5, end=9),
        ]
        result = audit_trace(
            simple_trace(events, removed_waits={"mix"})
        )
        material = result.materials[0]
        assert material.improvement_state == "improvement_possible"
        assert material.guaranteed_reduction == pytest.approx(0.0)
        assert material.max_possible_reduction > 0

    def test_non_removable_throttle_cannot_improve(self):
        events = [
            make_event("work", start=0, end=2),
            make_event(
                "thr",
                EVENT_WAIT,
                start=2,
                end=5,
                reasons=(REASON_THROTTLE,),
            ),
        ]
        result = audit_trace(simple_trace(events))
        material = result.materials[0]
        assert material.improvement_state == "no_improvement_possible"
        assert material.max_possible_reduction == pytest.approx(0.0)

    def test_off_critical_path_wait_adds_no_guarantee(self):
        worker = Resource("worker", capacity=2)
        main = Material(
            "main",
            [
                make_event(
                    "m1", start=0, end=10, resources=("worker",)
                ),
            ],
        )
        side = Material(
            "side",
            [
                make_event(
                    "s1", start=0, end=2, resources=("worker",)
                ),
                make_event(
                    "sw",
                    EVENT_WAIT,
                    start=2,
                    end=5,
                    reasons=(REASON_THROTTLE,),
                    removable_throttle=True,
                ),
            ],
        )
        trace = Trace(
            [main, side],
            {"worker": worker},
            removed_waits={"sw"},
        )
        guarantees = critical_path_guarantee(trace)
        assert guarantees["main"] == pytest.approx(0.0)
        assert guarantees["side"] == pytest.approx(3.0)

    def test_guarantee_survives_resource_ordering(self):
        worker = Resource("worker", capacity=1)
        material_a = Material(
            "A",
            [
                make_event(
                    "a-work",
                    start=0,
                    end=2,
                    resources=("worker",),
                ),
                make_event(
                    "a-thr",
                    EVENT_WAIT,
                    start=2,
                    end=5,
                    reasons=(REASON_THROTTLE,),
                    removable_throttle=True,
                ),
            ],
        )
        material_b = Material(
            "B",
            [
                make_event(
                    "b-work",
                    start=6,
                    end=8,
                    resources=("worker",),
                ),
            ],
        )
        trace = Trace(
            [material_a, material_b],
            {"worker": worker},
            removed_waits={"a-thr"},
        )
        guarantees = critical_path_guarantee(trace)
        # The pure throttle wait always sits on A's own chain, so the
        # guarantee is 3 regardless of the resource ordering.
        assert guarantees["A"] == pytest.approx(3.0)


class TestTimeoutRelief:
    def test_timeout_remains_when_earliest_still_late(self):
        events = [
            make_event("work", start=0, end=2),
            make_event(
                "thr",
                EVENT_WAIT,
                start=2,
                end=3,
                reasons=(REASON_THROTTLE,),
                removable_throttle=True,
            ),
            make_event(
                "long",
                EVENT_IO,
                start=3,
                end=20,
                transfer=Transfer(1, 1, 0),
            ),
        ]
        result = audit_trace(
            simple_trace(
                events, skew=0.0, removed_waits={"thr"}, deadline=10
            )
        )
        material = result.materials[0]
        assert material.timeout_state == "timeout_proven"
        assert (
            material.timeout_relief_state
            == "timeout_remains_in_every_schedule"
        )

    def test_relief_possible_when_earliest_meets_deadline(self):
        events = [
            make_event("work", start=0, end=2),
            make_event(
                "thr",
                EVENT_WAIT,
                start=2,
                end=8,
                reasons=(REASON_THROTTLE,),
                removable_throttle=True,
            ),
            make_event(
                "io",
                EVENT_IO,
                start=8,
                end=10,
                transfer=Transfer(1, 1, 0),
            ),
        ]
        result = audit_trace(
            simple_trace(
                events, skew=0.0, removed_waits={"thr"}, deadline=7
            )
        )
        material = result.materials[0]
        assert material.timeout_state == "timeout_proven"
        assert (
            material.timeout_relief_state
            == "relief_possible_not_proven"
        )


class TestWaitLedger:
    def test_multi_reason_wait_counted_once(self):
        events = [
            make_event(
                "mix",
                EVENT_WAIT,
                start=0,
                end=4,
                reasons=(
                    REASON_THROTTLE,
                    REASON_NETWORK,
                    REASON_RESOURCE,
                    REASON_OTHER,
                ),
                removable_throttle=True,
            ),
        ]
        result = audit_trace(
            simple_trace(events, removed_waits={"mix"})
        )
        assert len(result.waits) == 1
        wait = result.waits[0]
        assert wait.classification == "mixed_cause_partial"
        assert wait.provable_min == pytest.approx(0.0)
        assert wait.provable_max == pytest.approx(4.0)

    def test_pure_wait_attribution_is_full_interval(self):
        events = [
            make_event(
                "thr",
                EVENT_WAIT,
                start=0,
                end=4,
                reasons=(REASON_THROTTLE,),
                removable_throttle=True,
            ),
        ]
        result = audit_trace(
            simple_trace(events, removed_waits={"thr"})
        )
        wait = result.waits[0]
        assert wait.provable_min == pytest.approx(4.0)
        assert wait.provable_max == pytest.approx(4.0)
        assert (
            wait.classification == "throttle_fully_attributable"
        )


class TestByteLedger:
    def test_three_accounts_kept_separate(self):
        events = [
            make_event(
                "io1",
                EVENT_IO,
                start=0,
                end=2,
                transfer=Transfer(100, 90, 5),
            ),
            make_event(
                "io2",
                EVENT_IO,
                start=2,
                end=4,
                transfer=Transfer(60, 55, 0),
            ),
        ]
        result = audit_trace(simple_trace(events))
        ledger = result.bytes
        assert ledger.wire_bytes == 160
        assert ledger.payload_bytes == 145
        assert ledger.retransmit_bytes == 5
        assert len(ledger.per_event) == 2

    def test_retransmit_not_folded_into_payload(self):
        event = make_event(
            "io",
            EVENT_IO,
            start=0,
            end=1,
            transfer=Transfer(10, 7, 3),
        )
        result = audit_trace(simple_trace([event]))
        ledger = result.bytes
        assert ledger.payload_bytes == 7
        assert ledger.retransmit_bytes == 3
        assert ledger.wire_bytes == 10


class TestReport:
    def _result(self):
        events = [
            make_event("work", start=0, end=2),
            make_event(
                "thr",
                EVENT_WAIT,
                start=2,
                end=5,
                reasons=(REASON_THROTTLE,),
                removable_throttle=True,
            ),
            make_event(
                "io",
                EVENT_IO,
                start=5,
                end=9,
                transfer=Transfer(100, 90, 5),
            ),
        ]
        return audit_trace(
            simple_trace(
                events,
                skew=0.1,
                removed_waits={"thr"},
                deadline=8.5,
            )
        )

    def test_dict_is_json_serialisable(self):
        payload = json.dumps(report_as_dict(self._result()))
        assert "inf" not in payload

    def test_text_contains_all_sections(self):
        text = report_as_text(self._result())
        assert "MATERIAL COMPLETION WINDOWS" in text
        assert "WAIT ATTRIBUTION" in text
        assert "BYTE LEDGER" in text
        assert "timeout attribution" in text


class TestInfeasibleTrace:
    def test_overlapping_capacity_one_usage_is_infeasible(self):
        worker = Resource("worker", capacity=1)
        material_a = Material(
            "A",
            [make_event("a", start=0, end=4, resources=("worker",))],
        )
        material_b = Material(
            "B",
            [make_event("b", start=1, end=3, resources=("worker",))],
        )
        trace = Trace(
            [material_a, material_b], {"worker": worker}
        )
        result = audit_trace(trace)
        assert not result.feasible


class TestEdgeCases:
    def test_no_removed_waits_reports_no_improvement(self):
        events = [
            make_event("work", start=0, end=2),
            make_event(
                "thr",
                EVENT_WAIT,
                start=2,
                end=5,
                reasons=(REASON_THROTTLE,),
            ),
        ]
        result = audit_trace(simple_trace(events))
        material = result.materials[0]
        assert material.improvement_state == "no_improvement_possible"
        assert material.guaranteed_reduction == pytest.approx(0.0)
        assert material.max_possible_reduction == pytest.approx(0.0)
        assert result.counterfactual["relaxed"] is False

    def test_anchored_counterfactual_stays_feasible(self):
        # With a generous clock skew the wait's start and end windows
        # overlap, so zero duration still fits the readings and the
        # anchored no-throttle system remains feasible.
        events = [
            make_event("work", start=0, end=2),
            Event(
                "thr",
                EVENT_WAIT,
                2.0,
                3.0,
                0.0,
                1.5,
                reasons=(REASON_THROTTLE,),
                removable_throttle=True,
            ),
            make_event("io", EVENT_IO, start=3, end=5),
        ]
        result = audit_trace(
            simple_trace(events, skew=1.0, removed_waits={"thr"})
        )
        assert result.counterfactual["anchored_feasible"] is True
        assert result.counterfactual["relaxed"] is False
        material = result.materials[0]
        assert material.improvement_state == "improvement_proven"
        assert material.guaranteed_reduction > 0
        assert material.counterfactual_anchored_window is not None

    def test_duration_range_pure_wait_attribution(self):
        events = [
            make_event(
                "thr",
                EVENT_WAIT,
                start=0,
                end=4,
                duration_min=2.0,
                duration_max=4.0,
                reasons=(REASON_THROTTLE,),
                removable_throttle=True,
            ),
        ]
        result = audit_trace(
            simple_trace(events, removed_waits={"thr"})
        )
        wait = result.waits[0]
        assert wait.counterfactual_min == pytest.approx(0.0)
        assert wait.counterfactual_max == pytest.approx(0.0)
        assert wait.provable_min == pytest.approx(2.0)
        assert wait.provable_max == pytest.approx(4.0)

    def test_ordering_explosion_is_rejected(self):
        from schedule_audit.engine import MAX_ORDERINGS

        worker = Resource("worker", capacity=1)
        materials = []
        # Eight ambiguous serial events imply more than 5000 weak
        # orderings once the clock skew removes all proven ordering.
        for idx in range(8):
            materials.append(
                Material(
                    f"M{idx}",
                    [
                        make_event(
                            f"e{idx}",
                            start=0,
                            end=1,
                            resources=("worker",),
                        )
                    ],
                )
            )
        trace = Trace(
            materials,
            {"worker": worker},
            clock_skew=100.0,
        )
        assert 8 * 7 * 6 * 5 * 4 * 3 * 2 > 0  # factorial growth
        with pytest.raises(ValueError, match="orderings"):
            audit_trace(trace)
        assert MAX_ORDERINGS == 5000

    def test_witness_schedule_is_present(self):
        events = [make_event("e1", start=0, end=2)]
        result = audit_trace(simple_trace(events))
        witness = result.baseline["witness"]
        assert len(witness) == 1
        assert witness[0]["event"] == "e1"
        assert witness[0]["end"] >= witness[0]["start"]
