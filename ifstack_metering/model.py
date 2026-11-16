"""Input model for IF-MIB ifStack-based metering plans.

All data is immutable once loaded. Counter samples in particular are
wrapped in MappingProxyType so a metering-plan switch can never rewrite
the original counters; reports only reference them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Union


@dataclass(frozen=True)
class InterfaceInfo:
    if_index: int
    if_name: str | None = None
    if_type: str | None = None
    role: str | None = None  # business-provided tag; None means "missing"


@dataclass(frozen=True)
class StackLink:
    higher: int  # ifStackHigherLayer
    lower: int  # ifStackLowerLayer


@dataclass(frozen=True)
class LayerSpec:
    """Business-designated metering layer."""

    if_types: frozenset = frozenset()
    if_indexes: frozenset = frozenset()


@dataclass(frozen=True)
class ItemSpec:
    """One statistic item: a named selection over metering-layer interfaces."""

    name: str
    if_indexes: frozenset = frozenset()
    if_types: frozenset = frozenset()
    roles: frozenset = frozenset()


@dataclass(frozen=True)
class Plan:
    device: str
    interfaces: tuple = ()
    stack: tuple = ()
    counters: Mapping = field(default_factory=lambda: MappingProxyType({}))
    layer: LayerSpec = LayerSpec()
    items: tuple = ()


def _frozen_counters(raw: Mapping) -> Mapping:
    return MappingProxyType(
        {
            int(if_index): MappingProxyType({k: int(v) for k, v in counters.items()})
            for if_index, counters in raw.items()
        }
    )


def plan_from_dict(data: Mapping) -> Plan:
    interfaces = tuple(
        InterfaceInfo(
            if_index=int(entry["ifIndex"]),
            if_name=entry.get("ifName"),
            if_type=entry.get("ifType"),
            role=entry.get("role"),
        )
        for entry in data.get("interfaces", [])
    )
    stack = tuple(
        StackLink(higher=int(entry["higher"]), lower=int(entry["lower"]))
        for entry in data.get("ifStack", [])
    )
    layer_raw = data.get("metering", {}).get("layer", {})
    layer = LayerSpec(
        if_types=frozenset(layer_raw.get("ifTypes", ())),
        if_indexes=frozenset(int(i) for i in layer_raw.get("ifIndexes", ())),
    )
    items = tuple(
        ItemSpec(
            name=str(item["name"]),
            if_indexes=frozenset(int(i) for i in item.get("select", {}).get("ifIndexes", ())),
            if_types=frozenset(item.get("select", {}).get("ifTypes", ())),
            roles=frozenset(item.get("select", {}).get("roles", ())),
        )
        for item in data.get("metering", {}).get("items", ())
    )
    return Plan(
        device=str(data.get("device", "unknown")),
        interfaces=interfaces,
        stack=stack,
        counters=_frozen_counters(data.get("counters", {})),
        layer=layer,
        items=items,
    )


def load_plan(source: Union[str, Path, Mapping]) -> Plan:
    if isinstance(source, (str, Path)):
        data = json.loads(Path(source).read_text(encoding="utf-8"))
    else:
        data = source
    return plan_from_dict(data)
