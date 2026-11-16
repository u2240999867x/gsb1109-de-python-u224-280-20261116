"""End-to-end error-isolation tests against an in-process fake agent.

The fake agent sits at the raw receipt boundary so the async wrapper is
exercised exactly as it is in production: one real PDU per round, with
error-status, error-index and per-binding values driving the next plan.
"""

import asyncio

import pytest

from pysnmp.hlapi.isolation import BindingState, RetryBudget
from pysnmp.hlapi.v3arch.asyncio import (
    CommunityData,
    ContextData,
    ObjectIdentity,
    ObjectType,
    SnmpEngine,
    get_cmd_isolated,
)
from pysnmp.proto import rfc1905
from pysnmp.proto.rfc1902 import OctetString
from pysnmp.smi import builder, view


@pytest.fixture
def resolved_bindings():
    mib_view = view.MibViewController(builder.MibBuilder())

    def make(*args):
        return ObjectType(ObjectIdentity(*args)).resolve_with_mib(mib_view)

    return {
        "descr": make("SNMPv2-MIB", "sysDescr", 0),
        "uptime": make("SNMPv2-MIB", "sysUpTime", 0),
        "name": make("SNMPv2-MIB", "sysName", 0),
        "location": make("SNMPv2-MIB", "sysLocation", 0),
    }


def test_fake_agent_isolates_one_bad_oid(monkeypatch, resolved_bindings):
    import pysnmp.hlapi.v3arch.asyncio.cmdgen as cmdgen

    calls = []
    bad_oid = resolved_bindings["name"][0]
    requested = list(resolved_bindings.values())

    async def fake_receipt(engine, auth, target, context, var_binds):
        wire = [binding[0] for binding in var_binds]
        calls.append(wire)
        if bad_oid in wire:
            index = wire.index(bad_oid) + 1
            return {
                "error_indication": None,
                "error_status": 6,  # noAccess
                "error_index": index,
                "values": (),
                "request_id": len(calls),
                "response_request_id": len(calls),
                "matched": True,
            }
        return {
            "error_indication": None,
            "error_status": 0,
            "error_index": 0,
            "values": tuple(
                OctetString(f"value-{position}")
                for position, _ in enumerate(wire)
            ),
            "request_id": len(calls),
            "response_request_id": len(calls),
            "matched": True,
        }

    monkeypatch.setattr(cmdgen, "_get_cmd_receipt", fake_receipt)

    plans = []

    async def run():
        return await get_cmd_isolated(
            SnmpEngine(),
            CommunityData("public"),
            _FakeTarget(),
            ContextData(),
            *requested,
            lookupMib=False,
            requestSize=4,
            budget=RetryBudget(max_requests=6, max_attempts_per_binding=2),
            onPlan=lambda plan: plans.append(plan),
        )

    result = asyncio.run(run())

    # First round sends everything.  The bad binding is then deferred, and
    # only the healthy bindings are re-queried.
    assert len(calls) >= 2
    assert bad_oid in calls[0]
    assert bad_oid not in calls[1]
    bad_position = requested.index(resolved_bindings["name"])
    assert result.outcomes[bad_position].state is BindingState.UNCONFIRMED
    assert all(
        outcome.is_confirmed
        for position, outcome in enumerate(result.outcomes)
        if position != bad_position
    )
    assert len(plans) == len(calls)
    assert result.exhausted


class _FakeTarget:
    """Placeholder target; the fake receipt boundary ignores it."""


def test_fake_agent_reports_absent_without_inventing_values(
    monkeypatch, resolved_bindings
):
    import pysnmp.hlapi.v3arch.asyncio.cmdgen as cmdgen

    requested = list(resolved_bindings.values())

    async def fake_receipt(engine, auth, target, context, var_binds):
        return {
            "error_indication": None,
            "error_status": 0,
            "error_index": 0,
            "values": (
                OctetString("descr"),
                rfc1905.noSuchObject,
                OctetString("name"),
                OctetString("location"),
            ),
            "request_id": 1,
            "response_request_id": 1,
            "matched": True,
        }

    monkeypatch.setattr(cmdgen, "_get_cmd_receipt", fake_receipt)

    result = asyncio.run(
        get_cmd_isolated(
            SnmpEngine(),
            CommunityData("public"),
            _FakeTarget(),
            ContextData(),
            *requested,
            lookupMib=False,
        )
    )

    assert [outcome.state for outcome in result.outcomes] == [
        BindingState.CONFIRMED,
        BindingState.ABSENT,
        BindingState.CONFIRMED,
        BindingState.CONFIRMED,
    ]
    assert result.absent[0].reason == "noSuchObject"
    assert result.absent[0].is_absent
    assert len(result.confirmed_varbinds) == 3


def test_fake_agent_conflict_on_out_of_range_index(monkeypatch, resolved_bindings):
    import pysnmp.hlapi.v3arch.asyncio.cmdgen as cmdgen

    requested = list(resolved_bindings.values())

    async def fake_receipt(engine, auth, target, context, var_binds):
        return {
            "error_indication": None,
            "error_status": 5,
            "error_index": 9,
            "values": (),
            "request_id": 1,
            "response_request_id": 1,
            "matched": True,
        }

    monkeypatch.setattr(cmdgen, "_get_cmd_receipt", fake_receipt)

    result = asyncio.run(
        get_cmd_isolated(
            SnmpEngine(),
            CommunityData("public"),
            _FakeTarget(),
            ContextData(),
            *requested,
            lookupMib=False,
        )
    )

    assert all(outcome.is_conflict for outcome in result.outcomes)
    assert result.plan is None
    assert result.exhausted is False
