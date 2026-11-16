"""Tests for reading snapshot files and for the command line entry point."""

import json

import pytest

from pysnmp.linkreport import SnapshotFormatError, load_snapshots, render_json
from pysnmp.linkreport.cli import main
from pysnmp.linkreport.reader import parse_timestamp
from pysnmp.linkreport.report import build_report

MIB_SNAPSHOT = {
    "device": "edge1",
    "timestamp": "2026-11-16T00:00:00Z",
    "sysUpTime": 8640000,
    "interfaces": [
        {
            "ifIndex": 1,
            "ifDescr": "Gi0/1",
            "ifHighSpeed": 1000,
            "ifInOctets": 1000,
            "ifOutOctets": 2000,
            "ifHCInOctets": 1000,
            "ifHCOutOctets": 2000,
            "ifCounterDiscontinuityTime": 0,
        }
    ],
}


def test_mib_object_names_are_accepted(tmp_path):
    path = tmp_path / "snap.json"
    path.write_text(json.dumps({"snapshots": [MIB_SNAPSHOT]}), encoding="utf-8")

    (snapshot,) = load_snapshots(path)
    assert snapshot.device == "edge1"
    assert snapshot.sys_uptime_ticks == 8640000
    sample = snapshot.interfaces[1]
    assert sample.description == "Gi0/1"
    assert sample.speed_bps == 1000 * 1_000_000
    assert sample.hc_in_octets == 1000
    assert sample.discontinuity_ticks == 0


def test_csv_groups_rows_into_snapshots(tmp_path):
    path = tmp_path / "snap.csv"
    path.write_text(
        "device,timestamp,sysUpTime,ifIndex,ifDescr,ifSpeed,ifInOctets,ifOutOctets\n"
        "edge1,2026-11-16T00:00:00Z,1000,1,Gi0/1,1000000,10,20\n"
        "edge1,2026-11-16T00:00:00Z,1000,2,Gi0/2,1000000,30,40\n"
        "edge1,2026-11-16T00:05:00Z,1300,1,Gi0/1,1000000,20,40\n"
        "edge1,2026-11-16T00:05:00Z,1300,2,Gi0/2,1000000,50,60\n",
        encoding="utf-8",
    )
    snapshots = load_snapshots(path)

    assert len(snapshots) == 2
    assert set(snapshots[0].interfaces) == {1, 2}
    assert snapshots[0].sys_uptime_ticks == 1000

    report = build_report(snapshots)
    assert report.devices[0].summaries[1].inbound.minimum_bytes == 10
    assert report.devices[0].summaries[2].inbound.minimum_bytes == 20


def test_jsonl_reads_one_snapshot_per_line(tmp_path):
    path = tmp_path / "snap.jsonl"
    lines = [
        json.dumps(
            {
                "device": "edge1",
                "timestamp": "2026-11-16T00:00:00Z",
                "sys_uptime_ticks": 1000,
                "interfaces": {"1": {"if_index": 1, "in_octets": 0}},
            }
        ),
        json.dumps(
            {
                "device": "edge1",
                "timestamp": "2026-11-16T00:05:00Z",
                "sys_uptime_ticks": 1300,
                "interfaces": {"1": {"if_index": 1, "in_octets": 50}},
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    snapshots = load_snapshots(path)
    assert len(snapshots) == 2
    assert snapshots[1].interfaces[1].in_octets == 50


def test_device_keyed_mapping_is_accepted(tmp_path):
    path = tmp_path / "snap.json"
    path.write_text(
        json.dumps(
            {
                "edge1": [
                    {
                        "timestamp": "2026-11-16T00:00:00Z",
                        "sys_uptime_ticks": 100,
                        "interfaces": [{"if_index": 1, "in_octets": 0}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (snapshot,) = load_snapshots(path)
    assert snapshot.device == "edge1"


def test_naive_timestamp_is_treated_as_utc():
    assert parse_timestamp("2026-11-16 00:00:00").isoformat() == (
        "2026-11-16T00:00:00+00:00"
    )


def test_epoch_seconds_are_accepted():
    assert parse_timestamp(1763251200).isoformat() == "2025-11-16T00:00:00+00:00"


@pytest.mark.parametrize(
    "payload,message",
    [
        ({"timestamp": "2026-11-16T00:00:00Z", "interfaces": []}, "device"),
        ({"device": "x", "interfaces": []}, "timestamp"),
        ({"device": "x", "timestamp": "2026-11-16T00:00:00Z"}, "interfaces"),
    ],
)
def test_missing_required_fields_are_reported(tmp_path, payload, message):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SnapshotFormatError, match=message):
        load_snapshots(path)


def test_duplicate_ifindex_is_rejected(tmp_path):
    path = tmp_path / "dup.json"
    payload = dict(MIB_SNAPSHOT)
    payload["interfaces"] = [
        {"if_index": 1, "in_octets": 0},
        {"if_index": 1, "in_octets": 1},
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SnapshotFormatError, match="duplicate"):
        load_snapshots(path)


def test_negative_counter_is_rejected(tmp_path):
    path = tmp_path / "neg.json"
    payload = dict(MIB_SNAPSHOT)
    payload["interfaces"] = [{"if_index": 1, "in_octets": -5}]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SnapshotFormatError, match="negative counter"):
        load_snapshots(path)


def test_boolean_counter_is_rejected(tmp_path):
    path = tmp_path / "bool.json"
    payload = dict(MIB_SNAPSHOT)
    payload["interfaces"] = [{"if_index": 1, "in_octets": True}]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SnapshotFormatError, match="boolean"):
        load_snapshots(path)


def test_missing_file_is_reported(tmp_path):
    with pytest.raises(SnapshotFormatError, match="cannot read"):
        load_snapshots(tmp_path / "absent.json")


def test_invalid_json_is_reported(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SnapshotFormatError, match="invalid JSON"):
        load_snapshots(path)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _write_day(tmp_path):
    path = tmp_path / "day.json"
    path.write_text(
        json.dumps(
            {
                "snapshots": [
                    {
                        "device": "edge1",
                        "timestamp": "2026-11-16T00:00:00Z",
                        "sysUpTime": 1000,
                        "interfaces": [
                            {
                                "ifIndex": 1,
                                "ifDescr": "Gi0/1",
                                "ifSpeed": 1000000,
                                "ifInOctets": 2**32 - 100,
                            }
                        ],
                    },
                    {
                        "device": "edge1",
                        "timestamp": "2026-11-16T00:05:00Z",
                        "sysUpTime": 31000,
                        "interfaces": [
                            {
                                "ifIndex": 1,
                                "ifDescr": "Gi0/1",
                                "ifSpeed": 1000000,
                                "ifInOctets": 100,
                            }
                        ],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_cli_prints_text_report(tmp_path, capsys):
    path = _write_day(tmp_path)
    assert main([str(path)]) == 0
    output = capsys.readouterr().out
    assert "Link report" in output
    assert "Gi0/1" in output
    assert "wrap" in output


def test_cli_json_output(tmp_path, capsys):
    path = _write_day(tmp_path)
    assert main([str(path), "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["devices"][0]["device"] == "edge1"
    inbound = document["devices"][0]["generations"][0]["interfaces"][0]["inbound"]
    assert inbound["minimum_bytes"] == 200


def test_cli_writes_to_file(tmp_path):
    path = _write_day(tmp_path)
    out = tmp_path / "report.txt"
    assert main([str(path), "-o", str(out)]) == 0
    assert "Link report" in out.read_text(encoding="utf-8")


def test_cli_bad_input_returns_error(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text("{}", encoding="utf-8")
    assert main([str(path)]) == 2
    assert "error:" in capsys.readouterr().err


def test_cli_accepts_human_rate_suffix(tmp_path, capsys):
    path = _write_day(tmp_path)
    assert main([str(path), "--max-rate", "10G", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["config"]["max_rate_bps"] == 10_000_000_000


def test_render_json_is_serialisable_for_full_report(tmp_path):
    path = _write_day(tmp_path)
    report = build_report(load_snapshots(path))
    document = json.loads(render_json(report))
    assert document["period_start"].startswith("2026-11-16")
    assert document["devices"][0]["totals"][0]["if_index"] == 1
