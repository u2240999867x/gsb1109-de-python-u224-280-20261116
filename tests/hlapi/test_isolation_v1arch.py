"""Error-isolation contract tests for the v1arch async wrapper."""

import asyncio

import pytest

from pysnmp.hlapi.isolation import BindingState, RetryBudget
from pysnmp.hlapi.v1arch.asyncio import (
    CommunityData,
    SnmpDispatcher,
    get_cmd_isolated,
)
from pysnmp.proto import rfc1905
from pysnmp.proto.rfc1902 import OctetString
from pysnmp.smi import builder, view
from pysnmp.smi.rfc1902 import ObjectIdentity, ObjectType


@pytest.fixture
def resolved_bindings():
    mib_view = view.MibViewController(builder.MibBuilder())

    def make(oid):
        return ObjectType(ObjectIdentity(oid)).resolve_with_mib(mib_view)

    return [
        make("1.3.6.1.2.1.1.1.0"),
        make("1.3.6.1.2.1.1.3.0"),
        make("1.3.6.1.2.1.1.5.0"),
    ]


class _FakeTarget:
    """Placeholder target; the receipt boundary is patched."""


def test_v2c_wrapper_recovers_readable_bindings(monkeypatch, resolved_bindings):
    import pysnmp.hlapi.v1arch.asyncio.cmdgen as cmdgen

    calls = []
    bad_oid = resolved_bindings[1][0]

    async def fake_receipt(dispatcher, auth, target, var_binds):
        wire = [binding[0] for binding in var_binds]
        calls.append(wire)
        if bad_oid in wire:
            return {
                "error_indication": None,
                "error_status": 6,
                "error_index": wire.index(bad_oid) + 1,
                "values": (),
                "request_id": 1,
                "response_request_id": 1,
                "matched": True,
            }
        return {
            "error_indication": None,
            "error_status": 0,
            "error_index": 0,
            "values": (OctetString("descr"), OctetString("name")),
            "request_id": 1,
            "response_request_id": 1,
            "matched": True,
        }

    monkeypatch.setattr(cmdgen, "_get_cmd_receipt", fake_receipt)

    result = asyncio.run(
        get_cmd_isolated(
            SnmpDispatcher(),
            CommunityData("public"),
            _FakeTarget(),
            *resolved_bindings,
            lookupMib=False,
            budget=RetryBudget(max_requests=4, max_attempts_per_binding=2),
        )
    )

    assert calls[0] == [binding[0] for binding in resolved_bindings]
    assert result.outcomes[1].is_unconfirmed
    assert result.outcomes[0].is_confirmed
    assert result.outcomes[2].is_confirmed
    assert result.outcomes[0].value.prettyPrint() == "descr"


def test_v1_wrapper_uses_no_such_name(monkeypatch, resolved_bindings):
    import pysnmp.hlapi.v1arch.asyncio.cmdgen as cmdgen

    async def fake_receipt(dispatcher, auth, target, var_binds):
        return {
            "error_indication": None,
            "error_status": 2,
            "error_index": 2,
            "values": (None, None, None),
            "request_id": 1,
            "response_request_id": 1,
            "matched": True,
        }

    monkeypatch.setattr(cmdgen, "_get_cmd_receipt", fake_receipt)

    result = asyncio.run(
        get_cmd_isolated(
            SnmpDispatcher(),
            CommunityData("public", mpModel=0),
            _FakeTarget(),
            *resolved_bindings,
            lookupMib=False,
            budget=RetryBudget(max_requests=1),
        )
    )

    assert result.version == 0
    assert result.outcomes[1].state is BindingState.ABSENT
    assert result.outcomes[0].is_unconfirmed
    assert result.outcomes[2].is_unconfirmed
    assert result.exhausted


def test_v1arch_absent_value_does_not_become_empty(monkeypatch, resolved_bindings):
    import pysnmp.hlapi.v1arch.asyncio.cmdgen as cmdgen

    async def fake_receipt(dispatcher, auth, target, var_binds):
        return {
            "error_indication": None,
            "error_status": 0,
            "error_index": 0,
            "values": (OctetString("descr"), rfc1905.noSuchInstance, OctetString("n")),
            "request_id": 1,
            "response_request_id": 1,
            "matched": True,
        }

    monkeypatch.setattr(cmdgen, "_get_cmd_receipt", fake_receipt)

    result = asyncio.run(
        get_cmd_isolated(
            SnmpDispatcher(),
            CommunityData("public"),
            _FakeTarget(),
            *resolved_bindings,
            lookupMib=False,
        )
    )

    assert result.outcomes[1].state is BindingState.ABSENT
    assert len(result.absent_varbinds) == 1
    assert len(result.confirmed) == 2
