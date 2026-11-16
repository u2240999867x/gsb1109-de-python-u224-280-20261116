import pytest

from ifstack_metering import build_report, diff_reports, plan_from_dict


def make_plan(**overrides):
    data = {
        "device": "sw1",
        "interfaces": [],
        "ifStack": [],
        "counters": {},
        "metering": {"layer": {}, "items": []},
    }
    for key, value in overrides.items():
        if key in ("layer", "items"):
            data["metering"][key] = value
        else:
            data[key] = value
    return plan_from_dict(data)


def iface(if_index, if_type="ethernetCsmacd", name=None, role=None):
    entry = {"ifIndex": if_index, "ifType": if_type}
    if name:
        entry["ifName"] = name
    if role:
        entry["role"] = role
    return entry


def test_physical_layer_excludes_stacked_vlan():
    plan = make_plan(
        interfaces=[iface(1, name="Gi0/1"), iface(11, "propVirtual", "Vlan100")],
        ifStack=[{"higher": 11, "lower": 1}],
        counters={"1": {"ifHCInOctets": 1000}, "11": {"ifHCInOctets": 600}},
        layer={"ifTypes": ["ethernetCsmacd"]},
    )
    report = build_report(plan)
    item = report["items"][0]
    assert [u["ifIndex"] for u in item["used"]] == [1]
    assert item["used"][0]["counters"] == {"ifHCInOctets": 1000}
    assert item["excluded"] == [
        {
            "ifIndex": 11,
            "ifName": "Vlan100",
            "via": [{"metering": 1, "relation": "higher-layer of metering interface"}],
        }
    ]
    assert report["uncovered"] == []
    assert report["indeterminate"] == []


def test_aggregate_layer_excludes_members():
    plan = make_plan(
        interfaces=[
            iface(2, "ieee8023adLag", "Po1"),
            iface(3, name="Gi0/3"),
            iface(4, name="Gi0/4"),
        ],
        ifStack=[{"higher": 2, "lower": 3}, {"higher": 2, "lower": 4}],
        counters={
            "2": {"ifHCInOctets": 300},
            "3": {"ifHCInOctets": 100},
            "4": {"ifHCInOctets": 200},
        },
        layer={"ifTypes": ["ieee8023adLag"]},
    )
    report = build_report(plan)
    item = report["items"][0]
    assert [u["ifIndex"] for u in item["used"]] == [2]
    excluded = {e["ifIndex"]: e for e in item["excluded"]}
    assert set(excluded) == {3, 4}
    assert excluded[3]["via"] == [
        {"metering": 2, "relation": "lower-layer member of metering interface"}
    ]
    assert report["uncovered"] == []


def test_physical_layer_multi_parent_aggregate():
    # Aggregate stacked on two physical ports: counting at the physical
    # layer excludes the aggregate via both parents (multi-parent).
    plan = make_plan(
        interfaces=[
            iface(2, "ieee8023adLag", "Po1"),
            iface(3, name="Gi0/3"),
            iface(4, name="Gi0/4"),
        ],
        ifStack=[{"higher": 2, "lower": 3}, {"higher": 2, "lower": 4}],
        counters={
            "2": {"ifHCInOctets": 300},
            "3": {"ifHCInOctets": 100},
            "4": {"ifHCInOctets": 200},
        },
        layer={"ifTypes": ["ethernetCsmacd"]},
    )
    report = build_report(plan)
    item = report["items"][0]
    assert [u["ifIndex"] for u in item["used"]] == [3, 4]
    assert item["excluded"][0]["ifIndex"] == 2
    assert {v["metering"] for v in item["excluded"][0]["via"]} == {3, 4}


def test_cycle_is_indeterminate_not_guessed():
    plan = make_plan(
        interfaces=[iface(5, name="Gi0/5"), iface(6, name="Gi0/6")],
        ifStack=[{"higher": 5, "lower": 6}, {"higher": 6, "lower": 5}],
        counters={"5": {"ifHCInOctets": 10}, "6": {"ifHCInOctets": 20}},
        layer={"ifTypes": ["ethernetCsmacd"]},
    )
    report = build_report(plan)
    assert report["items"][0]["used"] == []
    assert {i["ifIndex"] for i in report["indeterminate"]} == {5, 6}
    assert any("cycle" in w for w in report["warnings"])


def test_missing_role_keeps_interface_unassigned():
    plan = make_plan(
        interfaces=[iface(1, name="Gi0/1"), iface(2, name="Gi0/2", role="uplink")],
        counters={"1": {"ifHCInOctets": 1}, "2": {"ifHCInOctets": 2}},
        layer={"ifTypes": ["ethernetCsmacd"]},
        items=[{"name": "uplinks", "select": {"roles": ["uplink"]}}],
    )
    report = build_report(plan)
    item = report["items"][0]
    assert [u["ifIndex"] for u in item["used"]] == [2]
    # Interface 1 is metering-layer but has no role: determinable as
    # unassigned, never guessed into an item by its name.
    assert report["uncovered"] == [
        {
            "ifIndex": 1,
            "detail": "metering-layer interface not selected by any statistic "
            "item (role/type missing or not matched)",
        }
    ]
    assert any("not selected by any statistic item" in w for w in report["warnings"])


def test_no_name_based_attribution():
    # "Gi0/1.100" looks like a sub-interface of Gi0/1 but has no ifStack
    # entry: it must stay uncovered, not be excluded via interface 1.
    plan = make_plan(
        interfaces=[iface(1, name="Gi0/1"), iface(12, "propVirtual", "Gi0/1.100")],
        counters={"1": {"ifHCInOctets": 100}, "12": {"ifHCInOctets": 60}},
        layer={"ifTypes": ["ethernetCsmacd"]},
    )
    report = build_report(plan)
    assert report["items"][0]["excluded"] == []
    assert [u["ifIndex"] for u in report["uncovered"]] == [12]
    assert "not attributed by interface name" in report["uncovered"][0]["detail"]


def test_aggregate_without_member_data():
    plan = make_plan(
        interfaces=[iface(2, "ieee8023adLag", "Po1"), iface(3, name="Gi0/3")],
        counters={"2": {"ifHCInOctets": 300}, "3": {"ifHCInOctets": 100}},
        layer={"ifTypes": ["ethernetCsmacd"]},
    )
    report = build_report(plan)
    assert any("no member data" in w and "Po1" in w for w in report["warnings"])
    # The aggregate itself is determinable only as uncovered at this layer.
    assert [u["ifIndex"] for u in report["uncovered"]] == [2]


def test_aggregate_with_members_notes_completeness():
    plan = make_plan(
        interfaces=[iface(2, "ieee8023adLag", "Po1"), iface(3, name="Gi0/3")],
        ifStack=[{"higher": 2, "lower": 3}],
        counters={"2": {"ifHCInOctets": 300}, "3": {"ifHCInOctets": 100}},
        layer={"ifTypes": ["ieee8023adLag"]},
    )
    report = build_report(plan)
    assert any("members as collected: [3]" in n for n in report["notes"])
    assert any("completeness cannot be verified" in n for n in report["notes"])


def test_metering_interfaces_stacked_on_each_other_warns():
    plan = make_plan(
        interfaces=[iface(1), iface(2)],
        ifStack=[{"higher": 2, "lower": 1}],
        counters={"1": {"ifHCInOctets": 1}, "2": {"ifHCInOctets": 2}},
        layer={"ifIndexes": [1, 2]},
    )
    report = build_report(plan)
    assert any("double-counts" in w for w in report["warnings"])


def test_layer_switch_audit_reassigns_without_rewriting_counters():
    base = {
        "interfaces": [
            iface(2, "ieee8023adLag", "Po1"),
            iface(3, name="Gi0/3"),
            iface(4, name="Gi0/4"),
        ],
        "ifStack": [{"higher": 2, "lower": 3}, {"higher": 2, "lower": 4}],
        "counters": {
            "2": {"ifHCInOctets": 300},
            "3": {"ifHCInOctets": 100},
            "4": {"ifHCInOctets": 200},
        },
    }
    old_plan = make_plan(**base, layer={"ifTypes": ["ethernetCsmacd"]})
    new_plan = make_plan(**base, layer={"ifTypes": ["ieee8023adLag"]})
    old_report = build_report(old_plan)
    new_report = build_report(new_plan)
    diff = diff_reports(old_report, new_report)

    reassigned = {r["ifIndex"]: r for r in diff["reassigned"]}
    assert set(reassigned) == {2, 3, 4}
    assert reassigned[3]["from"]["disposition"] == "metered"
    assert reassigned[3]["to"]["disposition"] == "excluded"
    assert reassigned[2]["from"]["disposition"] == "excluded"
    assert reassigned[2]["to"]["disposition"] == "metered"
    # Original counter values are echoed read-only, never rewritten.
    assert reassigned[2]["counters"] == {"ifHCInOctets": 300}
    assert dict(old_plan.counters[2]) == {"ifHCInOctets": 300}
    assert dict(new_plan.counters[3]) == {"ifHCInOctets": 100}


def test_counters_are_immutable():
    plan = make_plan(counters={"1": {"ifHCInOctets": 5}})
    with pytest.raises(TypeError):
        plan.counters[1] = {}
    with pytest.raises(TypeError):
        plan.counters[1]["ifHCInOctets"] = 99
