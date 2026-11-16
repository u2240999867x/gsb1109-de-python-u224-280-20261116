import asyncio

import pytest

from pysnmp.hlapi.isolation import (
    BindingState,
    IsolationPlanner,
    Receipt,
    RetryBudget,
    SamplingWindow,
    SNMP_V1,
    SNMP_V2C,
    SNMP_V3,
    classify_receipt,
    error_status_name,
    execute_isolation_plan,
    isolation_plan,
    merge_isolation_results,
    sampling_windows_compatible,
)
from pysnmp.proto.rfc1902 import Integer32, OctetString
from pysnmp.proto.rfc1905 import endOfMibView, noSuchInstance, noSuchObject
from pysnmp.smi import builder, view
from pysnmp.smi.rfc1902 import ObjectIdentity, ObjectType


@pytest.fixture(scope="module")
def mib_view():
    return view.MibViewController(builder.MibBuilder())


@pytest.fixture
def bindings(mib_view):
    def make(oid):
        return ObjectType(ObjectIdentity(oid)).resolve_with_mib(mib_view)

    return [
        make("1.3.6.1.2.1.1.1.0"),
        make("1.3.6.1.2.1.1.3.0"),
        make("1.3.6.1.2.1.1.5.0"),
    ]


def test_receipt_error_name_respects_v1():
    v1 = Receipt([], error_status=6, version=SNMP_V1)
    v2c = Receipt([], error_status=6, version=SNMP_V2C)
    assert v1.error_name == "6"
    assert v2c.error_name == "noAccess"


def test_error_status_names_are_version_specific():
    assert error_status_name(0, SNMP_V1) == "noError"
    assert error_status_name(2, SNMP_V1) == "noSuchName"
    assert error_status_name(6, SNMP_V2C) == "noAccess"
    assert error_status_name(18, SNMP_V3) == "inconsistentName"
    # 6 does not exist in SNMPv1 and must not be read as noAccess
    assert error_status_name(6, SNMP_V1) == "6"


def test_v2c_no_such_object_is_absent_not_empty(bindings):
    receipt = Receipt(
        bindings,
        error_status=0,
        values=[OctetString("descr"), noSuchObject, OctetString("name")],
        version=SNMP_V2C,
    )
    result = isolation_plan(bindings, [receipt], version=SNMP_V2C)

    assert result.settled
    assert [x.state for x in result.outcomes] == [
        BindingState.CONFIRMED,
        BindingState.ABSENT,
        BindingState.CONFIRMED,
    ]
    assert result.absent[0].reason == "noSuchObject"
    assert result.plan is None
    assert len(result.confirmed_varbinds) == 2


def test_v2c_no_such_instance_and_end_of_mib_are_absent(bindings):
    receipt = Receipt(
        bindings,
        values=[noSuchInstance, endOfMibView, OctetString("name")],
        version=SNMP_V2C,
    )
    result = isolation_plan(bindings, [receipt])

    assert [x.state for x in result.outcomes] == [
        BindingState.ABSENT,
        BindingState.ABSENT,
        BindingState.CONFIRMED,
    ]


def test_v2c_generic_error_splits_batch(bindings):
    receipt = Receipt(
        bindings, error_status=5, error_index=0, values=[], version=SNMP_V2C
    )
    result = isolation_plan(bindings, [receipt])

    assert result.plan is not None
    assert result.plan.positions != (0, 1, 2)
    assert result.plan.size < len(bindings)
    assert all(x.is_unconfirmed for x in result.outcomes)


def test_named_error_binding_is_deferred_not_repeated(bindings):
    receipt = Receipt(
        bindings,
        error_status="genErr",
        error_index=2,
        values=[None, None, None],
        version=SNMP_V2C,
    )
    result = isolation_plan(bindings, [receipt])

    assert result.plan is not None
    # The offending binding must not be sent in the same batch again.
    assert result.plan.positions == (0, 2)
    assert "error-index 2" in result.plan.reason
    assert "deferred" in result.plan.reason


def test_second_round_reuses_confirmed_bindings(bindings):
    first = Receipt(
        bindings, error_status=2, error_index=2, values=[None] * 3, version=SNMP_V2C
    )
    result = isolation_plan(bindings, [first])
    plan = result.plan
    assert plan.positions == (0, 2)

    second = Receipt(
        (bindings[0], bindings[2]),
        values=[OctetString("descr"), OctetString("name")],
        positions=plan.positions,
        row=plan.row,
        version=SNMP_V2C,
    )
    final = isolation_plan(bindings, [first, second], version=SNMP_V2C)

    assert final.settled
    assert final.plan is None
    assert final.outcomes[0].value.prettyPrint() == "descr"
    assert final.outcomes[1].is_absent
    assert final.outcomes[2].value.prettyPrint() == "name"
    # Every outcome points back at the receipts that produced it.
    assert final.outcomes[0].receipts == (0, 1)
    assert final.outcomes[1].receipts == (0,)


def test_v1_no_such_name_uses_error_index(bindings):
    receipt = Receipt(
        bindings,
        error_status=2,
        error_index=2,
        values=[None, None, None],
        version=SNMP_V1,
    )
    result = isolation_plan(bindings, [receipt], version=SNMP_V1)

    assert result.outcomes[1].state is BindingState.ABSENT
    assert result.outcomes[0].is_unconfirmed
    assert result.outcomes[2].is_unconfirmed
    assert result.plan.positions == (0, 2)


def test_v1_does_not_accept_v2c_exception_values(bindings):
    # A v1 agent cannot return noSuchObject; a proxy that does so would
    # produce a receipt that must not be trusted per binding.
    receipt = Receipt(
        bindings,
        values=[noSuchObject, OctetString("t"), OctetString("n")],
        version=SNMP_V1,
    )
    v2c_receipt = Receipt(
        bindings,
        values=[noSuchObject, OctetString("t"), OctetString("n")],
        version=SNMP_V2C,
    )
    classification = classify_receipt(bindings, v2c_receipt, version=SNMP_V2C)
    assert classification[0][1] is BindingState.ABSENT

    v1 = isolation_plan(bindings, [receipt], version=SNMP_V1)
    assert v1.outcomes[0].is_conflict
    assert "SNMPv1" in v1.outcomes[0].reason
    assert v1.outcomes[1].is_confirmed
    assert v1.outcomes[2].is_confirmed


def test_out_of_range_error_index_is_conflict(bindings):
    receipt = Receipt(
        bindings, error_status=5, error_index=9, values=[], version=SNMP_V2C
    )
    result = isolation_plan(bindings, [receipt])

    assert all(x.is_conflict for x in result.outcomes)
    assert "error-index 9" in result.outcomes[0].reason
    assert result.plan is None
    assert not result.settled


def test_request_id_mismatch_is_conflict(bindings):
    receipt = Receipt(
        bindings,
        values=[OctetString("a"), OctetString("b"), OctetString("c")],
        version=SNMP_V2C,
        request_id=11,
        response_request_id=12,
    )
    result = isolation_plan(bindings, [receipt])

    assert all(x.is_conflict for x in result.outcomes)
    assert "identity mismatch" in result.outcomes[0].reason


def test_matched_false_is_conflict(bindings):
    receipt = Receipt(
        bindings,
        values=[OctetString("a")] * 3,
        version=SNMP_V2C,
        matched=False,
    )
    result = isolation_plan(bindings, [receipt])
    assert all(x.is_conflict for x in result.outcomes)


def test_too_big_shrinks_request_and_keeps_bindings(bindings):
    receipt = Receipt(
        bindings, error_status=1, error_index=0, values=[], version=SNMP_V2C
    )
    result = isolation_plan(bindings, [receipt])

    assert result.plan is not None
    assert 0 < result.plan.size < len(bindings)
    assert result.plan.reason == "tooBig: reduce request size"
    assert all(x.is_unconfirmed for x in result.outcomes)


def test_budget_exhaustion_returns_unconfirmed_set(bindings):
    receipt = Receipt(
        bindings, error_status=5, error_index=0, values=[], version=SNMP_V2C
    )
    result = isolation_plan(
        bindings,
        [receipt],
        budget=RetryBudget(max_requests=1, max_attempts_per_binding=1),
    )

    assert result.exhausted
    assert result.plan is None
    assert len(result.unconfirmed) == len(bindings)
    assert result.unconfirmed_varbinds == tuple(bindings)


def test_request_size_caps_plan(bindings):
    budget = RetryBudget(
        max_requests=8, max_bindings_per_request=1, max_attempts_per_binding=4
    )
    result = isolation_plan(bindings, [], budget=budget)

    assert result.plan is not None
    assert result.plan.size == 1
    assert result.plan.positions == (0,)


def test_error_indication_keeps_bindings_unconfirmed(bindings):
    receipt = Receipt(
        bindings, error_indication="timeout", values=[], version=SNMP_V2C
    )
    result = isolation_plan(
        bindings, [receipt], budget=RetryBudget(max_requests=1)
    )

    assert all(x.is_unconfirmed for x in result.outcomes)
    assert "timeout" in result.outcomes[0].reason


def test_conflicting_values_become_conflict(bindings):
    first = Receipt(bindings, values=[OctetString("a")] * 3, version=SNMP_V2C)
    second = Receipt(
        (bindings[0],), values=[OctetString("different")], positions=(0,),
        row=1, version=SNMP_V2C
    )
    result = isolation_plan(bindings, [first, second])

    assert result.outcomes[0].is_conflict
    assert result.outcomes[1].is_confirmed


def test_unconfirmed_bindings_are_never_filled_with_nulls(bindings):
    result = isolation_plan(
        bindings,
        [Receipt(bindings, error_status=5, error_index=0, values=[])],
        budget=RetryBudget(max_requests=1),
    )
    assert len(result.confirmed) == 0
    assert len(result.absent) == 0
    assert len(result.unconfirmed) == 3
    assert result.unconfirmed_varbinds == tuple(bindings)


def test_primary_conflict_survives_merge(bindings):
    conflicted = isolation_plan(
        bindings,
        [
            Receipt(
                bindings,
                error_status=5,
                error_index=9,
                values=[],
                version=SNMP_V2C,
            )
        ],
    )
    fresh = isolation_plan(
        bindings,
        [Receipt(bindings, values=[OctetString("v")] * 3)],
    )
    merged = merge_isolation_results(conflicted, fresh)

    assert all(outcome.is_conflict for outcome in merged.outcomes)


def test_sampling_windows_overlap_and_merge(bindings):
    assert sampling_windows_compatible(SamplingWindow(0, 10), SamplingWindow(5, 15))
    assert not sampling_windows_compatible(
        SamplingWindow(0, 10), SamplingWindow(20, 30)
    )
    assert sampling_windows_compatible(None, SamplingWindow(0, 10))

    base = isolation_plan(bindings, [], sampling_window=SamplingWindow(0, 10))
    fresh = isolation_plan(
        bindings,
        [Receipt(bindings, values=[OctetString("v")] * 3)],
        sampling_window=SamplingWindow(5, 15),
    )
    merged = merge_isolation_results(base, fresh)
    assert all(x.is_confirmed for x in merged.outcomes)


def test_incompatible_sampling_window_is_not_merged(bindings):
    base = isolation_plan(bindings, [], sampling_window=SamplingWindow(0, 10))
    other = isolation_plan(
        bindings,
        [Receipt(bindings, values=[OctetString("v")] * 3)],
        sampling_window=SamplingWindow(20, 30),
    )
    merged = merge_isolation_results(base, other)

    assert len(merged.confirmed) == 0
    assert len(merged.unconfirmed) == 3
    assert merged.ignored_receipts


def test_incompatible_receipts_are_ignored_in_one_plan(bindings):
    receipt = Receipt(
        bindings,
        values=[OctetString("v")] * 3,
        sampling_window=SamplingWindow(20, 30),
    )
    result = isolation_plan(
        bindings, [receipt], sampling_window=SamplingWindow(0, 10)
    )
    assert len(result.confirmed) == 0
    assert result.ignored_receipts == (receipt,)


def test_reused_outcomes_are_not_queried(bindings):
    previous = isolation_plan(
        bindings,
        [Receipt(bindings, values=[OctetString("a"), noSuchObject, OctetString("c")])],
    )
    result = isolation_plan(bindings, [], reused=previous.outcomes)

    assert result.outcomes[0].is_confirmed
    assert result.outcomes[1].is_absent
    assert result.outcomes[2].is_confirmed
    assert result.plan is None


def test_planner_drives_rounds_from_real_receipts(bindings):
    planner = IsolationPlanner(bindings, version=SNMP_V2C)

    plan = planner.next_plan()
    assert plan.positions == (0, 1, 2)

    result = planner.record(
        Receipt(
            plan.bindings,
            error_status=2,
            error_index=1,
            values=[None] * 3,
            version=SNMP_V2C,
        ),
        positions=plan.positions,
    )
    assert result.plan.positions == (1, 2)

    result = planner.record(
        Receipt(
            result.plan.bindings,
            values=[OctetString("uptime"), OctetString("name")],
            version=SNMP_V2C,
        ),
        positions=result.plan.positions,
    )
    assert result.settled
    assert result.outcomes[0].is_absent


def test_one_bad_binding_does_not_hide_readable_ones(bindings):
    # Round 1: error-index 2 names the poisoned binding.
    first = Receipt(
        bindings, error_status=2, error_index=2, values=[None] * 3
    )
    # Round 2: good bindings read fine.
    second = Receipt(
        (bindings[0], bindings[2]),
        values=[OctetString("descr"), OctetString("name")],
        positions=(0, 2),
        row=1,
    )
    # Round 3: the offender alone is explicitly missing.
    third = Receipt(
        (bindings[1],), values=[noSuchInstance], positions=(1,), row=2
    )
    result = isolation_plan(bindings, [first, second, third])

    assert result.settled
    assert len(result.confirmed) == 2
    assert len(result.absent) == 1
    assert result.plan is None


def test_execute_isolation_plan_uses_receipts(bindings):
    plans = []

    async def send(plan):
        plans.append(plan.positions)
        if len(plans) == 1:
            return Receipt(
                plan.bindings,
                error_status="noSuchName",
                error_index=2,
                values=[None] * 3,
            )
        return Receipt(
            plan.bindings,
            values=[OctetString("a"), OctetString("b")],
        )

    result = asyncio.run(
        execute_isolation_plan(
            send, bindings, version=SNMP_V2C, budget=RetryBudget(max_requests=4)
        )
    )

    assert plans == [(0, 1, 2), (0, 2)]
    assert result.settled
    assert result.outcomes[0].value.prettyPrint() == "a"
    assert result.outcomes[2].value.prettyPrint() == "b"


def test_empty_request_is_a_noop():
    result = isolation_plan([])
    assert result.outcomes == ()
    assert result.plan is None
    assert not result.exhausted


def test_budget_validation():
    with pytest.raises(ValueError):
        RetryBudget(max_requests=0)
    with pytest.raises(ValueError):
        RetryBudget(max_bindings_per_request=0)
    with pytest.raises(ValueError):
        RetryBudget(max_attempts_per_binding=0)
    with pytest.raises(ValueError):
        isolation_plan([], request_size=0)


def test_integer_and_objecttype_values_are_accepted(bindings):
    receipt = Receipt(
        bindings,
        values=[Integer32(7), noSuchObject, OctetString("x")],
    )
    result = isolation_plan(bindings, [receipt])
    assert result.outcomes[0].value == 7
    assert result.outcomes[1].is_absent
