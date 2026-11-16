#
# This file is part of pysnmp software.
#
# Copyright (c) 2005-2020, Ilya Etingof <etingof@gmail.com>
# License: https://www.pysnmp.com/pysnmp/license.html
#
"""Load poll snapshots from JSON, JSONL or CSV.

A *snapshot* is one poll of one device.  Every snapshot carries the four
pieces of evidence a link report needs:

* the 32-bit octet counters (``ifInOctets``/``ifOutOctets``),
* the 64-bit octet counters (``ifHCInOctets``/``ifHCOutOctets``),
* the device uptime (``sysUpTime``),
* the per-interface discontinuity time (``ifCounterDiscontinuityTime``).

Both the compact snake_case names and the MIB object names are accepted, so
exports straight from an SNMP walk can be fed in with little editing.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from pysnmp.linkreport.model import InterfaceSample, Snapshot

__all__ = ["SnapshotFormatError", "load_snapshots", "parse_timestamp"]


class SnapshotFormatError(ValueError):
    """Raised when an input file cannot be understood."""


#: Accepted spellings for each snapshot field.
_FIELD_ALIASES = {
    "device": ("device", "host", "hostname", "sysName", "sys_name"),
    "timestamp": (
        "timestamp",
        "time",
        "sampled_at",
        "collected_at",
        "datetime",
        "date",
    ),
    "sys_uptime_ticks": (
        "sys_uptime_ticks",
        "sysuptime",
        "sysuptime_ticks",
        "sysup_time",
        "uptime_ticks",
        "uptime",
    ),
    "interfaces": ("interfaces", "ifaces", "ports", "interface_table"),
}

_INTERFACE_ALIASES = {
    "if_index": ("if_index", "ifindex", "index", "ifIndex"),
    "description": ("description", "ifdescr", "if_descr", "name", "ifname"),
    "speed_bps": (
        "speed_bps",
        "ifspeed",
        "if_speed",
        "ifhighspeed_bps",
        "speed",
        "bandwidth_bps",
    ),
    "speed_mbps": ("ifhighspeed", "if_high_speed", "speed_mbps"),
    "in_octets": ("in_octets", "ifinoctets", "if_in_octets", "in", "rx_octets"),
    "out_octets": (
        "out_octets",
        "ifoutoctets",
        "if_out_octets",
        "out",
        "tx_octets",
    ),
    "hc_in_octets": (
        "hc_in_octets",
        "ifhcinOctets",
        "ifhcinoctets",
        "hc_in",
        "rx_hc_octets",
    ),
    "hc_out_octets": (
        "hc_out_octets",
        "ifhcoutOctets",
        "ifhcoutoctets",
        "hc_out",
        "tx_hc_octets",
    ),
    "discontinuity_ticks": (
        "discontinuity_ticks",
        "ifcounterdiscontinuitytime",
        "if_counter_discontinuity_time",
        "discontinuity_time",
        "discontinuity",
    ),
}


def _normalise_key(key: str) -> str:
    return "".join(ch for ch in key.strip().lower() if ch.isalnum() or ch == "_")


def _build_lookup(aliases: dict[str, tuple[str, ...]]) -> dict[str, str]:
    lookup = {}
    for canonical, names in aliases.items():
        for name in names:
            lookup[_normalise_key(name)] = canonical
    return lookup


_SNAPSHOT_LOOKUP = _build_lookup(_FIELD_ALIASES)
_INTERFACE_LOOKUP = _build_lookup(_INTERFACE_ALIASES)


def parse_timestamp(value) -> datetime:
    """Parse an ISO-8601 timestamp into an aware UTC datetime.

    Values without a timezone are interpreted as UTC rather than as local
    time, so a report does not shift depending on where it is generated.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
    else:
        text = str(value).strip()
        if not text:
            raise SnapshotFormatError("empty timestamp")
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise SnapshotFormatError(f"bad timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_int(value, field: str, *, required: bool = False) -> int | None:
    """Convert a JSON/CSV scalar to int, keeping ``None`` for "not present"."""
    if value is None:
        if required:
            raise SnapshotFormatError(f"missing required field {field!r}")
        return None
    if isinstance(value, bool):
        # bool is an int subclass; a counter being true/false is a data bug.
        raise SnapshotFormatError(f"field {field!r} is a boolean, expected integer")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise SnapshotFormatError(f"field {field!r} is fractional: {value!r}")
    text = str(value).strip()
    if not text or text in {"-", "n/a", "N/A", "null", "None"}:
        if required:
            raise SnapshotFormatError(f"missing required field {field!r}")
        return None
    try:
        return int(text, 0)
    except ValueError:
        try:
            numeric = float(text)
        except ValueError as exc:
            raise SnapshotFormatError(
                f"field {field!r} is not an integer: {value!r}"
            ) from exc
        if not numeric.is_integer():
            raise SnapshotFormatError(f"field {field!r} is fractional: {value!r}")
        return int(numeric)


def _canonical_dict(raw: dict, lookup: dict[str, str]) -> dict:
    canonical = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        target = lookup.get(_normalise_key(key))
        if target is not None:
            canonical[target] = value
    return canonical


def _interface_from_dict(raw: dict, where: str) -> InterfaceSample:
    if not isinstance(raw, dict):
        raise SnapshotFormatError(f"{where}: interface entry is not an object")
    fields = _canonical_dict(raw, _INTERFACE_LOOKUP)

    if_index = _as_int(fields.get("if_index"), "if_index", required=True)
    if if_index < 0:
        raise SnapshotFormatError(f"{where}: negative ifIndex {if_index}")

    speed_bps = _as_int(fields.get("speed_bps"), "speed_bps")
    if speed_bps is None:
        speed_mbps = _as_int(fields.get("speed_mbps"), "speed_mbps")
        if speed_mbps is not None:
            speed_bps = speed_mbps * 1_000_000
    if speed_bps is not None and speed_bps < 0:
        raise SnapshotFormatError(f"{where}: negative ifSpeed {speed_bps}")

    description = fields.get("description")
    if description is not None:
        description = str(description)

    sample = InterfaceSample(
        if_index=if_index,
        description=description,
        speed_bps=speed_bps,
        in_octets=_as_int(fields.get("in_octets"), "in_octets"),
        out_octets=_as_int(fields.get("out_octets"), "out_octets"),
        hc_in_octets=_as_int(fields.get("hc_in_octets"), "hc_in_octets"),
        hc_out_octets=_as_int(fields.get("hc_out_octets"), "hc_out_octets"),
        discontinuity_ticks=_as_int(
            fields.get("discontinuity_ticks"), "discontinuity_ticks"
        ),
    )
    for name in ("in_octets", "out_octets", "hc_in_octets", "hc_out_octets"):
        value = getattr(sample, name)
        if value is not None and value < 0:
            raise SnapshotFormatError(f"{where}: negative counter {name}={value}")
    return sample


def _snapshot_from_dict(raw: dict, where: str) -> Snapshot:
    if not isinstance(raw, dict):
        raise SnapshotFormatError(f"{where}: snapshot is not an object")
    fields = _canonical_dict(raw, _SNAPSHOT_LOOKUP)

    device = fields.get("device")
    if device is None or not str(device).strip():
        raise SnapshotFormatError(f"{where}: missing device name")
    if "timestamp" not in fields:
        raise SnapshotFormatError(f"{where}: missing timestamp")

    interfaces_raw = fields.get("interfaces")
    if interfaces_raw is None:
        raise SnapshotFormatError(f"{where}: missing interfaces")
    if isinstance(interfaces_raw, dict):
        interfaces_raw = list(interfaces_raw.values())
    if not isinstance(interfaces_raw, list):
        raise SnapshotFormatError(f"{where}: interfaces is not a list")

    interfaces: dict[int, InterfaceSample] = {}
    for position, entry in enumerate(interfaces_raw):
        sample = _interface_from_dict(entry, f"{where}[{position}]")
        if sample.if_index in interfaces:
            raise SnapshotFormatError(
                f"{where}: duplicate ifIndex {sample.if_index}"
            )
        interfaces[sample.if_index] = sample

    return Snapshot(
        device=str(device).strip(),
        sampled_at=parse_timestamp(fields["timestamp"]),
        interfaces=interfaces,
        sys_uptime_ticks=_as_int(
            fields.get("sys_uptime_ticks"), "sys_uptime_ticks"
        ),
    )


def _iter_json_snapshots(document, where: str):
    if isinstance(document, list):
        for position, entry in enumerate(document):
            yield _snapshot_from_dict(entry, f"{where}[{position}]")
        return
    if not isinstance(document, dict):
        raise SnapshotFormatError(f"{where}: expected an object or a list")

    lowered = {_normalise_key(k) for k in document if isinstance(k, str)}
    if "snapshots" in lowered:
        entries = document["snapshots"]
        if not isinstance(entries, list):
            raise SnapshotFormatError(f"{where}: 'snapshots' is not a list")
        for position, entry in enumerate(entries):
            yield _snapshot_from_dict(entry, f"{where}.snapshots[{position}]")
        return

    if "interfaces" in lowered or "timestamp" in lowered:
        # A single bare snapshot object.
        yield _snapshot_from_dict(document, where)
        return

    # A mapping of device name -> list of snapshots.
    for device, entries in document.items():
        if not isinstance(entries, list):
            raise SnapshotFormatError(
                f"{where}: device {device!r} does not map to a list"
            )
        for position, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise SnapshotFormatError(
                    f"{where}: {device}[{position}] is not an object"
                )
            hydrated = dict(entry)
            hydrated.setdefault("device", device)
            yield _snapshot_from_dict(hydrated, f"{where}.{device}[{position}]")


def _load_json(text: str, where: str) -> list[Snapshot]:
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SnapshotFormatError(f"{where}: invalid JSON: {exc}") from exc
    return list(_iter_json_snapshots(document, where))


def _load_jsonl(text: str, where: str) -> list[Snapshot]:
    snapshots = []
    for number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            document = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SnapshotFormatError(
                f"{where}:{number}: invalid JSON: {exc}"
            ) from exc
        if isinstance(document, dict) and "snapshots" in {
            _normalise_key(k) for k in document
        }:
            snapshots.extend(_iter_json_snapshots(document, f"{where}:{number}"))
        else:
            snapshots.append(_snapshot_from_dict(document, f"{where}:{number}"))
    return snapshots


def _load_csv(text: str, where: str) -> list[Snapshot]:
    reader = csv.DictReader(text.splitlines())
    if reader.fieldnames is None:
        raise SnapshotFormatError(f"{where}: CSV has no header row")

    # Each row describes one interface; rows sharing device+timestamp form a
    # snapshot.  Header names go through the same alias table as JSON.
    grouped: dict[tuple[str, datetime], list[dict]] = {}
    order: list[tuple[str, datetime]] = []
    uptimes: dict[tuple[str, datetime], int | None] = {}

    for number, row in enumerate(reader, start=2):
        fields = _canonical_dict(row, _SNAPSHOT_LOOKUP)
        if "device" not in fields or "timestamp" not in fields:
            raise SnapshotFormatError(
                f"{where}:{number}: CSV needs device and timestamp columns"
            )
        device = str(fields["device"]).strip()
        if not device:
            raise SnapshotFormatError(f"{where}:{number}: empty device")
        try:
            timestamp = parse_timestamp(fields["timestamp"])
        except SnapshotFormatError as exc:
            raise SnapshotFormatError(f"{where}:{number}: {exc}") from exc

        key = (device, timestamp)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
            uptimes[key] = _as_int(
                fields.get("sys_uptime_ticks"), "sys_uptime_ticks"
            )
        else:
            candidate = _as_int(fields.get("sys_uptime_ticks"), "sys_uptime_ticks")
            if candidate is not None:
                if uptimes[key] is not None and uptimes[key] != candidate:
                    raise SnapshotFormatError(
                        f"{where}:{number}: conflicting sysUpTime for "
                        f"{device} at {timestamp.isoformat()}"
                    )
                uptimes[key] = candidate

        grouped[key].append(_interface_from_dict(row, f"{where}:{number}"))

    snapshots = []
    for key in order:
        device, timestamp = key
        interfaces: dict[int, InterfaceSample] = {}
        for sample in grouped[key]:
            if sample.if_index in interfaces:
                raise SnapshotFormatError(
                    f"{where}: duplicate ifIndex {sample.if_index} for "
                    f"{device} at {timestamp.isoformat()}"
                )
            interfaces[sample.if_index] = sample
        snapshots.append(
            Snapshot(
                device=device,
                sampled_at=timestamp,
                interfaces=interfaces,
                sys_uptime_ticks=uptimes[key],
            )
        )
    return snapshots


def load_snapshots(path: str | Path) -> list[Snapshot]:
    """Read snapshots from *path*.

    The format is chosen by extension: ``.csv`` is CSV, ``.jsonl``/``.ndjson``
    is one JSON snapshot per line, anything else is a JSON document.  A JSON
    document may be a list of snapshots, a ``{"snapshots": [...]}`` object, a
    single snapshot, or a mapping of device name to snapshots.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise SnapshotFormatError(f"cannot read {path}: {exc}") from exc

    suffix = path.suffix.lower()
    if suffix == ".csv":
        snapshots = _load_csv(text, str(path))
    elif suffix in (".jsonl", ".ndjson"):
        snapshots = _load_jsonl(text, str(path))
    else:
        snapshots = _load_json(text, str(path))

    if not snapshots:
        raise SnapshotFormatError(f"{path}: contains no snapshots")
    return snapshots
