"""Tests for compliance driven collection coverage analysis."""

import json

import pytest

from pysnmp.tools.coverage.engine import (
    CoverageInputError,
    MibCatalog,
    ObjectRef,
    build_coverage,
    parse_device,
    parse_metrics,
    parse_policy,
)
from pysnmp.tools.coverage.report import (
    build_object_report,
    build_polling_plan,
)

MODULES = [
    "SNMP-FRAMEWORK-MIB",
    "SNMP-TARGET-MIB",
    "SNMP-USER-BASED-SM-MIB",
    "SNMPv2-MIB",
]


def _catalog():
    return MibCatalog(MODULES)


def _group(ref, requirement="mandatory", condition=None, min_access=None):
    entry = {"group": ref, "requirement": requirement}
    if condition is not None:
        entry["condition"] = condition
    if min_access is not None:
        entry["object_min_access"] = min_access
    return entry


def _device_group(ref, supported, condition_active=None, reason=""):
    return {
        "group": ref,
        "supported": supported,
        "condition_active": condition_active,
        "reason": reason,
    }


def _build(policy_data, device_data, metrics_data=None):
    catalog = _catalog()
    policy = parse_policy(policy_data, catalog)
    device = parse_device(device_data, catalog)
    metrics = parse_metrics(metrics_data or {"required_metrics": []}, catalog)
    coverage = build_coverage(catalog, policy, device, metrics)
    return catalog, coverage, build_polling_plan(coverage), build_object_report(
        coverage
    )


def test_mandatory_group_scalar_and_column_polling_shapes():
    policy_data = {
        "groups": [
            _group("SNMP-FRAMEWORK-MIB::snmpEngineGroup"),
            _group("SNMPv2-MIB::systemGroup"),
        ]
    }
    device_data = {
        "groups": [
            _device_group("SNMP-FRAMEWORK-MIB::snmpEngineGroup", True),
            _device_group("SNMPv2-MIB::systemGroup", True),
        ]
    }
    catalog, coverage, plan, report = _build(policy_data, device_data)

    engine_time = coverage[
        ObjectRef("SNMP-FRAMEWORK-MIB", "snmpEngineTime")
    ]
    assert engine_time.verdict == "satisfied"
    assert engine_time.polling["operation"] == "GET"
    assert engine_time.polling["target"].endswith(".0")

    sys_orid = ObjectRef("SNMPv2-MIB", "sysORID")
    column = coverage[sys_orid]
    assert column.verdict == "satisfied"
    assert column.polling["operation"] == "WALK"
    assert column.polling["index_columns"] == ["SNMPv2-MIB::sysORIndex"]
    assert "GETNEXT/GETBulk" in column.polling["note"]

    walk_entries = [entry for entry in plan if entry["operation"] == "WALK"]
    assert all(entry["target"] != entry["oid"] for entry in walk_entries)


def test_conditional_inactive_group_is_waived_and_not_polled():
    condition = "device implements the SNMPv3 User-based Security Model (USM)"
    policy_data = {
        "groups": [
            _group(
                "SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup",
                requirement="conditional",
                condition=condition,
            )
        ]
    }
    device_data = {
        "groups": [
            _device_group(
                "SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup",
                supported=False,
                condition_active=False,
                reason="SNMPv2c-only product",
            )
        ]
    }
    _, coverage, plan, _report = _build(policy_data, device_data)
    ref = ObjectRef("SNMP-USER-BASED-SM-MIB", "usmStatsUnknownUserNames")
    assert coverage[ref].verdict == "waived"
    assert coverage[ref].polling is None
    assert all(entry["object"] != str(ref) for entry in plan)
    evidence = coverage[ref].evidences[0]
    assert evidence.verdict == "condition-inactive"


def test_conditional_active_but_unsupported_group_is_partial():
    condition = "device includes an SNMP command responder application"
    policy_data = {
        "groups": [
            _group(
                "SNMP-TARGET-MIB::snmpTargetCommandResponderGroup",
                requirement="conditional",
                condition=condition,
            )
        ]
    }
    device_data = {
        "groups": [
            _device_group(
                "SNMP-TARGET-MIB::snmpTargetCommandResponderGroup",
                supported=False,
                condition_active=True,
                reason="counters not instrumented",
            )
        ]
    }
    _, coverage, _, _ = _build(policy_data, device_data)

    ref = ObjectRef("SNMP-TARGET-MIB", "snmpUnknownContexts")
    assert coverage[ref].verdict == "partial"
    assert coverage[ref].evidences[0].verdict == "group-unsupported"
    assert "counters not instrumented" in coverage[ref].reasons[0]


def test_min_access_deficit_is_partial_with_traceable_evidence():
    policy_data = {
        "groups": [_group("SNMPv2-MIB::snmpGroup")],
        "objects": [
            {
                "object": "SNMPv2-MIB::snmpEnableAuthenTraps",
                "requirement": "mandatory",
                "min_access": "read-write",
                "reference": "RFC3418 OBJECT clause",
            }
        ],
    }
    device_data = {
        "groups": [_device_group("SNMPv2-MIB::snmpGroup", True)],
        "object_access": [
            {
                "object": "SNMPv2-MIB::snmpEnableAuthenTraps",
                "access": "read-only",
            }
        ],
    }
    _, coverage, _, report = _build(policy_data, device_data)

    ref = ObjectRef("SNMPv2-MIB", "snmpEnableAuthenTraps")
    item = coverage[ref]
    assert item.verdict == "partial"
    assert item.effective_min_access == "read-write"
    object_evidence = next(
        evidence
        for evidence in item.evidences
        if evidence.source == "compliance-object"
    )
    assert object_evidence.verdict == "access-deficit"
    assert object_evidence.ref == "RFC3418 OBJECT clause"
    entry = next(row for row in report if row["object"] == str(ref))
    assert any("read-write" in reason for reason in entry["reasons"])


def test_shared_object_polled_once_with_per_group_evidence():
    policy_data = {
        "groups": [_group("SNMPv2-MIB::systemGroup")],
        "objects": [
            {
                "object": "SNMPv2-MIB::sysUpTime",
                "requirement": "mandatory",
                "min_access": "read-only",
                "reference": "extra OBJECT clause",
            }
        ],
    }
    device_data = {
        "groups": [_device_group("SNMPv2-MIB::systemGroup", True)]
    }
    metrics_data = {
        "required_metrics": [{"object": "SNMPv2-MIB::sysUpTime"}]
    }
    _, coverage, plan, _ = _build(policy_data, device_data, metrics_data)

    ref = ObjectRef("SNMPv2-MIB", "sysUpTime")
    assert len(coverage[ref].evidences) == 3
    matching = [entry for entry in plan if entry["object"] == str(ref)]
    assert len(matching) == 1
    assert len(matching[0]["dedup_sources"]) == 3


def test_not_accessible_index_column_is_unsatisfiable():
    policy_data = {
        "groups": [
            _group("SNMP-TARGET-MIB::snmpTargetResponseGroup")
        ]
    }
    device_data = {
        "groups": [
            _device_group("SNMP-TARGET-MIB::snmpTargetResponseGroup", True)
        ]
    }
    metrics_data = {
        "required_metrics": [
            {"object": "SNMP-TARGET-MIB::snmpTargetAddrName"}
        ]
    }
    _, coverage, plan, _ = _build(policy_data, device_data, metrics_data)

    ref = ObjectRef("SNMP-TARGET-MIB", "snmpTargetAddrName")
    assert coverage[ref].node.kind == "column"
    assert coverage[ref].verdict == "unsatisfied"
    assert coverage[ref].polling is None
    assert all(entry["object"] != str(ref) for entry in plan)


def test_metric_without_active_declaration_is_undeclared():
    condition = "device implements the SNMPv3 User-based Security Model (USM)"
    policy_data = {
        "groups": [
            _group(
                "SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup",
                requirement="conditional",
                condition=condition,
            )
        ]
    }
    device_data = {
        "groups": [
            _device_group(
                "SNMP-USER-BASED-SM-MIB::usmMIBBasicGroup",
                supported=False,
                condition_active=False,
            )
        ]
    }
    metrics_data = {
        "required_metrics": [
            {
                "object": "SNMP-USER-BASED-SM-MIB::usmStatsUnknownUserNames"
            }
        ]
    }
    _, coverage, _, _ = _build(policy_data, device_data, metrics_data)

    ref = ObjectRef("SNMP-USER-BASED-SM-MIB", "usmStatsUnknownUserNames")
    assert coverage[ref].verdict == "partial"
    metric_evidence = next(
        evidence
        for evidence in coverage[ref].evidences
        if evidence.source == "required-metric"
    )
    assert metric_evidence.verdict == "undeclared"
    assert "runtime" in metric_evidence.detail


def test_input_validation_errors():
    catalog = _catalog()
    with pytest.raises(CoverageInputError, match="MODULE::object"):
        parse_metrics({"required_metrics": [{"object": "sysUpTime"}]}, catalog)
    with pytest.raises(CoverageInputError, match="locked MIB"):
        parse_metrics(
            {"required_metrics": [{"object": "SNMPv2-MIB::doesNotExist"}]},
            catalog,
        )
    with pytest.raises(CoverageInputError, match="must state its condition"):
        parse_policy(
            {
                "groups": [
                    _group(
                        "SNMPv2-MIB::systemGroup",
                        requirement="conditional",
                    )
                ]
            },
            catalog,
        )
    with pytest.raises(CoverageInputError, match="not a member of group"):
        parse_policy(
            {
                "groups": [
                    _group(
                        "SNMPv2-MIB::systemGroup",
                        min_access=[
                            {
                                "object": "SNMPv2-MIB::snmpInPkts",
                                "min_access": "read-write",
                            }
                        ],
                    )
                ]
            },
            catalog,
        )


def test_device_must_answer_every_policy_group():
    policy_data = {
        "groups": [_group("SNMPv2-MIB::systemGroup")]
    }
    with pytest.raises(CoverageInputError, match="does not answer for group"):
        _build(policy_data, {"groups": []})


def test_report_is_json_serializable(tmp_path):
    policy_data = {
        "groups": [_group("SNMP-FRAMEWORK-MIB::snmpEngineGroup")]
    }
    device_data = {
        "groups": [
            _device_group("SNMP-FRAMEWORK-MIB::snmpEngineGroup", True)
        ]
    }
    _, _, plan, report = _build(policy_data, device_data)
    json.dumps(plan)
    json.dumps(report)
