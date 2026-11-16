"""Tests for ENTITY-MIB / IF-MIB device inventory construction.

The inventory joins physical containment (entPhysicalContainedIn) with
logical interface aliases (entAliasMappingIdentifier).  These tests pin down
the rules that are easy to get wrong:

* entPhysicalIndex and ifIndex are different index spaces; equal numbers do
  not imply any relationship;
* one physical component may map to several interfaces;
* replaceable units without a serial number stay "evidence insufficient"
  rather than inventing an identity;
* orphans, containment cycles and aliases to missing interfaces are reported
  without dropping the components that can still be confirmed;
* when a device generation reuses an entPhysicalIndex, the old serial number
  must never be carried over to the new unit.
"""

import pytest

from pysnmp.entity.inventory import (
    IF_ENTRY_OID,
    ComponentClass,
    HistoricalComponent,
    IssueCode,
    SerialEvidence,
    build_inventory,
    interface_index_from_identifier,
)


def _if_entry_oid(if_index):
    return IF_ENTRY_OID + (1, if_index)


def _chassis_tree_rows():
    return [
        {
            "physical_index": 1,
            "component_class": "chassis",
            "contained_in": 0,
            "name": "chassis-1",
            "serial_number": "CHASSIS-SN",
        },
        {
            "physical_index": 2,
            "component_class": "module",
            "contained_in": 1,
            "name": "line-card-2",
            "serial_number": "CARD-SN-2",
        },
        {
            "physical_index": 3,
            "component_class": "port",
            "contained_in": 2,
            "name": "port-3",
        },
        {
            "physical_index": 4,
            "component_class": "port",
            "contained_in": 2,
            "name": "port-4",
        },
    ]


def test_containment_tree_and_listing():
    inventory = build_inventory(_chassis_tree_rows(), [])

    assert inventory.roots == (1,)
    chassis = inventory.components[1]
    card = inventory.components[2]
    port3 = inventory.components[3]
    port4 = inventory.components[4]

    assert chassis.component_class is ComponentClass.CHASSIS
    assert card.component_class is ComponentClass.MODULE
    assert port3.component_class is ComponentClass.PORT

    assert chassis.children == [2]
    assert card.children == [3, 4]
    assert port3.children == []

    assert port3.parent_index == 2
    assert port3.chassis_index == 1
    assert card.chassis_index == 1
    assert chassis.chassis_index == 1

    listing = inventory.to_listing()
    assert [row["physical_index"] for row in listing] == [1, 2, 3, 4]
    assert listing[2]["containment_path"] == [1, 2, 3]
    assert inventory.issues == []


def test_equal_index_numbers_do_not_link_component_and_interface():
    # Port entPhysicalIndex 7 and interface ifIndex 7 share a number but
    # there is no alias mapping, so no relationship may be inferred.
    rows = [
        {"physical_index": 1, "component_class": "chassis", "contained_in": 0},
        {"physical_index": 7, "component_class": "port", "contained_in": 1},
    ]
    interfaces = [{"if_index": 7, "name": "eth7"}]
    inventory = build_inventory(rows, interfaces, [])

    assert inventory.components[7].interface_indices == []
    assert inventory.issues == []


def test_alias_uses_identifier_oid_not_numeric_coincidence():
    rows = [
        {"physical_index": 1, "component_class": "chassis", "contained_in": 0},
        {"physical_index": 3, "component_class": "port", "contained_in": 1},
    ]
    interfaces = [
        {"if_index": 3, "name": "unrelated-eth3"},
        {"if_index": 100, "name": "eth100"},
    ]
    aliases = [
        {"physical_index": 3, "identifier_oid": _if_entry_oid(100)},
    ]
    inventory = build_inventory(rows, interfaces, aliases)

    assert inventory.components[3].interface_indices == [100]
    assert inventory.interfaces_for(3)[0].name == "eth100"


def test_one_component_maps_to_multiple_interfaces():
    rows = [
        {"physical_index": 1, "component_class": "chassis", "contained_in": 0},
        {"physical_index": 5, "component_class": "port", "contained_in": 1},
    ]
    interfaces = [
        {"if_index": 11, "name": "eth11"},
        {"if_index": 12, "name": "eth12"},
    ]
    aliases = [
        {"physical_index": 5, "identifier_oid": _if_entry_oid(11)},
        {"physical_index": 5, "identifier_oid": _if_entry_oid(12)},
    ]
    inventory = build_inventory(rows, interfaces, aliases)

    assert inventory.components[5].interface_indices == [11, 12]
    assert {interface.if_index for interface in inventory.interfaces_for(5)} == {
        11,
        12,
    }


def test_duplicate_alias_pair_is_idempotent():
    rows = [
        {"physical_index": 1, "component_class": "chassis", "contained_in": 0},
        {"physical_index": 5, "component_class": "port", "contained_in": 1},
    ]
    interfaces = [{"if_index": 11}]
    aliases = [
        {"physical_index": 5, "identifier_oid": _if_entry_oid(11)},
        {"physical_index": 5, "if_index": 11},
    ]
    inventory = build_inventory(rows, interfaces, aliases)

    assert inventory.components[5].interface_indices == [11]
    assert inventory.issues == []


def test_replaceable_component_without_serial_is_insufficient_evidence():
    rows = [
        {"physical_index": 1, "component_class": "chassis", "contained_in": 0},
        {
            "physical_index": 2,
            "component_class": "module",
            "contained_in": 1,
            "serial_number": "",
        },
        {"physical_index": 3, "component_class": "port", "contained_in": 2},
    ]
    inventory = build_inventory(rows, [])

    card = inventory.components[2]
    assert card.is_replaceable is True
    assert card.serial_number is None
    assert card.serial_evidence is SerialEvidence.INSUFFICIENT_EVIDENCE
    assert IssueCode.SERIAL_INSUFFICIENT_EVIDENCE in card.issue_codes

    port = inventory.components[3]
    assert port.is_replaceable is False
    assert port.serial_evidence is SerialEvidence.NOT_APPLICABLE
    assert IssueCode.SERIAL_INSUFFICIENT_EVIDENCE not in port.issue_codes

    assert [issue.code for issue in inventory.issues] == [
        IssueCode.SERIAL_INSUFFICIENT_EVIDENCE
    ]


def test_non_replaceable_module_does_not_require_serial():
    rows = [
        {"physical_index": 1, "component_class": "chassis", "contained_in": 0},
        {
            "physical_index": 2,
            "component_class": "module",
            "contained_in": 1,
            "is_replaceable": False,
        },
    ]
    inventory = build_inventory(rows, [])

    assert inventory.components[2].serial_evidence is SerialEvidence.NOT_APPLICABLE
    assert inventory.issues == []


def test_orphan_root_component_is_reported_but_kept():
    rows = [
        {"physical_index": 1, "component_class": "chassis", "contained_in": 0},
        {"physical_index": 9, "component_class": "port", "contained_in": 0},
    ]
    inventory = build_inventory(rows, [])

    orphan = inventory.components[9]
    assert orphan.is_orphan is True
    assert orphan.chassis_index is None
    assert 9 in inventory.roots
    assert [issue.code for issue in inventory.issues] == [
        IssueCode.ORPHAN_COMPONENT
    ]
    # The healthy chassis is still confirmed.
    assert inventory.components[1].is_orphan is False


def test_unknown_parent_is_reported_and_subtree_survives():
    rows = [
        {"physical_index": 1, "component_class": "chassis", "contained_in": 0},
        {
            "physical_index": 20,
            "component_class": "module",
            "contained_in": 77,
            "is_replaceable": False,
        },
        {"physical_index": 21, "component_class": "port", "contained_in": 20},
    ]
    inventory = build_inventory(rows, [])

    module = inventory.components[20]
    assert module.parent_missing is True
    assert module.is_orphan is True
    assert inventory.components[21].parent_index == 20
    assert inventory.components[21].children == []
    assert module.children == [21]
    assert [issue.code for issue in inventory.issues] == [
        IssueCode.UNKNOWN_PARENT
    ]
    assert inventory.issues[0].related_indices == (77,)


def test_containment_cycle_does_not_swallow_healthy_branch():
    rows = [
        {"physical_index": 1, "component_class": "chassis", "contained_in": 0},
        {"physical_index": 2, "component_class": "module", "contained_in": 1},
        {
            "physical_index": 10,
            "component_class": "module",
            "contained_in": 12,
            "is_replaceable": False,
        },
        {
            "physical_index": 11,
            "component_class": "module",
            "contained_in": 10,
            "is_replaceable": False,
        },
        {
            "physical_index": 12,
            "component_class": "module",
            "contained_in": 11,
            "is_replaceable": False,
        },
    ]
    inventory = build_inventory(rows, [])

    cycle_issue = next(
        issue
        for issue in inventory.issues
        if issue.code is IssueCode.CONTAINMENT_CYCLE
    )
    assert set(cycle_issue.related_indices) == {10, 11, 12}
    for index in (10, 11, 12):
        assert inventory.components[index].in_cycle is True

    # The healthy branch remains fully confirmed.
    assert inventory.components[2].in_cycle is False
    assert inventory.components[2].chassis_index == 1
    assert inventory.components[1].children == [2]
    # A closed cycle is unreachable from any chassis by construction.
    assert inventory.components[10].chassis_index is None
    assert inventory.components[10].parent_index == 12


def test_self_containment_is_a_cycle():
    rows = [
        {"physical_index": 1, "component_class": "chassis", "contained_in": 0},
        {
            "physical_index": 8,
            "component_class": "module",
            "contained_in": 8,
            "is_replaceable": False,
        },
    ]
    inventory = build_inventory(rows, [])

    assert inventory.components[8].in_cycle is True
    assert [issue.code for issue in inventory.issues] == [
        IssueCode.CONTAINMENT_CYCLE
    ]


def test_alias_to_missing_interface_is_reported():
    rows = [
        {"physical_index": 1, "component_class": "chassis", "contained_in": 0},
        {"physical_index": 3, "component_class": "port", "contained_in": 1},
    ]
    interfaces = [{"if_index": 4, "name": "eth4"}]
    aliases = [
        {"physical_index": 3, "identifier_oid": _if_entry_oid(4)},
        {"physical_index": 3, "identifier_oid": _if_entry_oid(99)},
    ]
    inventory = build_inventory(rows, interfaces, aliases)

    component = inventory.components[3]
    assert component.interface_indices == [4]
    assert component.unresolved_interface_indices == [99]
    assert [issue.code for issue in inventory.issues] == [
        IssueCode.MISSING_INTERFACE
    ]
    assert inventory.issues[0].interface_index == 99


def test_alias_referencing_unknown_component_is_reported():
    aliases = [{"physical_index": 55, "identifier_oid": _if_entry_oid(1)}]
    inventory = build_inventory([], [{"if_index": 1}], aliases)

    assert [issue.code for issue in inventory.issues] == [
        IssueCode.DANGLING_ALIAS_PHYSICAL
    ]
    assert inventory.issues[0].physical_index == 55


def test_alias_oid_outside_if_entry_is_not_guessed():
    rows = [
        {"physical_index": 1, "component_class": "chassis", "contained_in": 0},
        {"physical_index": 3, "component_class": "port", "contained_in": 1},
    ]
    # An interface with ifIndex 5 exists, and the foreign OID ends in 5.
    # The trailing arc must not be treated as an ifIndex.
    interfaces = [{"if_index": 5, "name": "eth5"}]
    aliases = [
        {
            "physical_index": 3,
            "identifier_oid": (1, 3, 6, 1, 2, 1, 4, 20, 1, 5),
        },
    ]
    inventory = build_inventory(rows, interfaces, aliases)

    assert inventory.components[3].interface_indices == []
    assert [issue.code for issue in inventory.issues] == [
        IssueCode.ALIAS_TO_NON_IF_ENTRY
    ]


def test_interface_index_from_identifier_validation():
    assert interface_index_from_identifier(_if_entry_oid(7)) == 7
    # Wrong prefix.
    assert interface_index_from_identifier((1, 3, 6, 1, 2, 1, 4, 1, 7)) is None
    # Right prefix, wrong column/shape (scalar-style, missing ifIndex).
    assert interface_index_from_identifier(IF_ENTRY_OID + (1,)) is None


def test_reused_index_with_different_serial_flags_new_identity():
    rows = [
        {"physical_index": 1, "component_class": "chassis", "contained_in": 0},
        {
            "physical_index": 2,
            "component_class": "module",
            "contained_in": 1,
            "serial_number": "NEW-SN",
        },
    ]
    history = [
        HistoricalComponent(
            physical_index=2,
            component_class=ComponentClass.MODULE,
            serial_number="OLD-SN",
        ),
    ]
    inventory = build_inventory(rows, [], history=history)

    component = inventory.components[2]
    assert component.serial_number == "NEW-SN"
    assert IssueCode.REUSED_INDEX_NEW_IDENTITY in component.issue_codes
    assert [issue.code for issue in inventory.issues] == [
        IssueCode.REUSED_INDEX_NEW_IDENTITY
    ]


def test_reused_index_without_serial_does_not_inherit_old_serial():
    rows = [
        {"physical_index": 1, "component_class": "chassis", "contained_in": 0},
        {
            "physical_index": 2,
            "component_class": "module",
            "contained_in": 1,
        },
    ]
    history = {
        2: HistoricalComponent(
            physical_index=2,
            component_class=ComponentClass.MODULE,
            serial_number="OLD-SN",
        ),
    }
    inventory = build_inventory(rows, [], history=history)

    component = inventory.components[2]
    assert component.serial_number is None
    assert component.serial_evidence is SerialEvidence.INSUFFICIENT_EVIDENCE
    issue_codes = [issue.code for issue in inventory.issues]
    assert IssueCode.HISTORICAL_SERIAL_NOT_CARRIED in issue_codes
    assert IssueCode.SERIAL_INSUFFICIENT_EVIDENCE in issue_codes


def test_history_without_serial_makes_no_identity_claim():
    rows = [
        {"physical_index": 1, "component_class": "chassis", "contained_in": 0},
        {
            "physical_index": 2,
            "component_class": "module",
            "contained_in": 1,
            "serial_number": "NEW-SN",
        },
    ]
    history = [
        HistoricalComponent(
            physical_index=2,
            component_class=ComponentClass.MODULE,
            serial_number=None,
        ),
    ]
    inventory = build_inventory(rows, [], history=history)

    assert inventory.components[2].serial_number == "NEW-SN"
    assert inventory.issues == []


def test_duplicate_physical_and_interface_indices_keep_first_row():
    rows = [
        {
            "physical_index": 1,
            "component_class": "chassis",
            "contained_in": 0,
            "name": "first",
        },
        {
            "physical_index": 1,
            "component_class": "chassis",
            "contained_in": 0,
            "name": "second",
        },
    ]
    interfaces = [
        {"if_index": 1, "name": "first-if"},
        {"if_index": 1, "name": "second-if"},
    ]
    inventory = build_inventory(rows, interfaces)

    assert inventory.components[1].name == "first"
    assert inventory.interfaces[1].name == "first-if"
    assert {issue.code for issue in inventory.issues} == {
        IssueCode.DUPLICATE_PHYSICAL_INDEX,
        IssueCode.DUPLICATE_INTERFACE_INDEX,
    }


def test_all_bad_branches_coexist_with_confirmed_inventory():
    rows = [
        {"physical_index": 1, "component_class": "chassis", "contained_in": 0},
        {
            "physical_index": 2,
            "component_class": "module",
            "contained_in": 1,
            "serial_number": "GOOD-CARD",
        },
        {"physical_index": 3, "component_class": "port", "contained_in": 2},
        {"physical_index": 9, "component_class": "port", "contained_in": 0},
        {
            "physical_index": 30,
            "component_class": "module",
            "contained_in": 31,
            "is_replaceable": False,
        },
        {
            "physical_index": 31,
            "component_class": "module",
            "contained_in": 30,
            "is_replaceable": False,
        },
    ]
    interfaces = [{"if_index": 100, "name": "eth100"}]
    aliases = [
        {"physical_index": 3, "identifier_oid": _if_entry_oid(100)},
        {"physical_index": 3, "identifier_oid": _if_entry_oid(200)},
        {"physical_index": 404, "identifier_oid": _if_entry_oid(100)},
    ]
    inventory = build_inventory(rows, interfaces, aliases)

    issue_codes = {issue.code for issue in inventory.issues}
    assert issue_codes == {
        IssueCode.ORPHAN_COMPONENT,
        IssueCode.CONTAINMENT_CYCLE,
        IssueCode.MISSING_INTERFACE,
        IssueCode.DANGLING_ALIAS_PHYSICAL,
    }

    # Confirmed parts survive untouched.
    assert inventory.components[1].chassis_index == 1
    assert inventory.components[2].serial_number == "GOOD-CARD"
    assert inventory.components[3].interface_indices == [100]
    assert inventory.components[3].unresolved_interface_indices == [200]
    assert inventory.interfaces_for(3)[0].name == "eth100"

    listing = inventory.to_listing()
    # The listing stays JSON-serializable.
    import json

    json.dumps(listing)
    json.dumps([issue.to_dict() for issue in inventory.issues])


def test_listing_rows_carry_component_scoped_issues():
    rows = [
        {"physical_index": 1, "component_class": "chassis", "contained_in": 0},
        {"physical_index": 9, "component_class": "port", "contained_in": 0},
    ]
    inventory = build_inventory(rows, [])
    listing = {row["physical_index"]: row for row in inventory.to_listing()}

    assert listing[9]["is_orphan"] is True
    assert [issue["code"] for issue in listing[9]["component_issues"]] == [
        IssueCode.ORPHAN_COMPONENT.value
    ]
    assert listing[1]["component_issues"] == []


def test_multiple_chassis_get_independent_root_assignment():
    rows = [
        {"physical_index": 1, "component_class": "chassis", "contained_in": 0},
        {"physical_index": 2, "component_class": "port", "contained_in": 1},
        {"physical_index": 10, "component_class": "chassis", "contained_in": 0},
        {"physical_index": 11, "component_class": "port", "contained_in": 10},
    ]
    inventory = build_inventory(rows, [])

    assert inventory.roots == (1, 10)
    assert inventory.components[2].chassis_index == 1
    assert inventory.components[11].chassis_index == 10
    assert inventory.issues == []


@pytest.mark.parametrize(
    "raw_class, expected",
    [
        ("chassis", ComponentClass.CHASSIS),
        ("module", ComponentClass.MODULE),
        ("port", ComponentClass.PORT),
        ("powerSupply", ComponentClass.OTHER),
        ("fan", ComponentClass.OTHER),
        ("unknown", ComponentClass.OTHER),
    ],
)
def test_class_name_normalization(raw_class, expected):
    rows = [
        {
            "physical_index": 1,
            "component_class": raw_class,
            "contained_in": 0,
        },
    ]
    inventory = build_inventory(rows, [])
    assert inventory.components[1].component_class is expected
