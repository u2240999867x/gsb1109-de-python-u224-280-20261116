######################################################################
# Copyright (c)
# All rights reserved.
#
# This software is licensed as described in the file LICENSE.txt, which
# you should have received as part of this distribution.
#
######################################################################

"""
Payer companion-guide rules evaluated alongside the base implementation guide.

The base map (HIPAA implementation guide) and a payer companion guide are two
independent rule sets that must BOTH pass. This module never mutates the base
map or the source data: it re-runs the read-only element validators for the
base verdict and applies the companion rule separately, then classifies the
pair of verdicts so neither rule set can overwrite the other.

Classifications (per data occurrence, mutually exclusive):
  * base_invalid    - the base guide rejects the position. A payer rule can
                      never legalize a base-illegal value, so this dominates.
  * payer_rejected  - the base guide accepts, but the companion rule rejects.

Rule-set self-contradictions are reported separately, once per rule, under
``rule_conflicts`` (not per data occurrence): the companion guide asserts the
opposite of the base guide at the same position, independent of the data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import IO, Any

import defusedxml.ElementTree as et

from .errors import EngineError
from .map_if._composite import composite_if
from .map_if._element import element_if
from .map_if._segment import segment_if
from .map_walker import pop_to_parent_loop
from .segment import Composite

SOURCE_BASE = "base_guideline"
SOURCE_PAYER = "payer_companion"

BASE_INVALID = "base_invalid"
PAYER_REJECTED = "payer_rejected"

_EXPECT_REQUIRE = "require"
_EXPECT_ALLOW = "allow"
_EXPECT_DISALLOW = "disallow"


@dataclass(frozen=True, slots=True)
class CompanionRule:
    rule_id: str
    applies_to: str
    location: str
    element: str
    expect_type: str
    expect_values: frozenset[str]
    subelement: str | None = None


@dataclass(frozen=True, slots=True)
class _BoundRule:
    rule: CompanionRule
    seg_node: segment_if
    leaf: element_if
    in_composite: bool
    contradicts_base: bool


@dataclass(frozen=True, slots=True)
class ConstraintFinding:
    classification: str
    rule_id: str
    payer_source: str
    base_source: str
    seg_id: str
    element: str
    loop_path: str
    loop_occurrence: int
    seg_count: int
    src_line: int
    value: str | None
    base_accepted: bool
    payer_accepted: bool
    base_error_codes: tuple[str, ...]
    expect_type: str
    expect_values: tuple[str, ...]
    subelement: str | None


@dataclass(frozen=True, slots=True)
class RuleConflict:
    rule_id: str
    location: str
    element: str
    payer_source: str
    base_source: str
    reason: str


@dataclass(slots=True)
class CompanionReport:
    guide_name: str
    payer: str
    source_file: str
    findings: list[ConstraintFinding] = field(default_factory=list)
    rule_conflicts: list[RuleConflict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "guide_name": self.guide_name,
            "payer": self.payer,
            "source_file": self.source_file,
            "summary": {
                BASE_INVALID: sum(1 for f in self.findings if f.classification == BASE_INVALID),
                PAYER_REJECTED: sum(1 for f in self.findings if f.classification == PAYER_REJECTED),
                "rule_conflicts": len(self.rule_conflicts),
            },
            "rule_conflicts": [
                {
                    "rule_id": c.rule_id,
                    "location": c.location,
                    "element": c.element,
                    "payer_source": c.payer_source,
                    "base_source": c.base_source,
                    "reason": c.reason,
                }
                for c in self.rule_conflicts
            ],
            "findings": [
                {
                    "classification": f.classification,
                    "rule_id": f.rule_id,
                    "sources": {
                        SOURCE_PAYER: {
                            "file": f.payer_source,
                            "rule_id": f.rule_id,
                            "expect": f.expect_type,
                            "expect_values": list(f.expect_values),
                        },
                        SOURCE_BASE: {
                            "file": f.base_source,
                            "loop_path": f.loop_path,
                            "element": f.element,
                            "error_codes": list(f.base_error_codes),
                        },
                    },
                    "location": {
                        "seg_id": f.seg_id,
                        "element": f.element,
                        "subelement": f.subelement,
                        "loop_path": f.loop_path,
                        "loop_occurrence": f.loop_occurrence,
                        "seg_count": f.seg_count,
                        "src_line": f.src_line,
                    },
                    "value": f.value,
                    "base_accepted": f.base_accepted,
                    "payer_accepted": f.payer_accepted,
                }
                for f in self.findings
            ],
        }


def load_companion(source: str | IO[Any]) -> tuple[list[CompanionRule], str, str, str]:
    """Parse a payer companion guide XML file.

    Returns (rules, guide_name, payer, source_name). Raises EngineError on any
    malformed rule so a bad companion guide fails loudly rather than silently
    dropping a constraint.
    """
    if hasattr(source, "read"):
        source_name = getattr(source, "name", "<stream>")
        tree = et.parse(source)
    else:
        source_name = str(source)
        tree = et.parse(source_name)
    root = tree.getroot()
    if root.tag != "companion_guide":
        raise EngineError(
            f"Companion guide {source_name!r} root must be <companion_guide>, got <{root.tag}>"
        )
    guide_name = root.findtext("name") or ""
    payer = root.findtext("payer") or ""
    rules: list[CompanionRule] = []
    for rel in root.findall("rule"):
        rule_id = rel.get("id")
        applies_to = rel.get("applies_to")
        if not rule_id or not applies_to:
            raise EngineError("Companion <rule> requires id and applies_to attributes")
        location = rel.findtext("location")
        element = rel.findtext("element")
        if not location or not element:
            raise EngineError(f"Companion rule {rule_id!r} requires <location> and <element>")
        subelement = rel.findtext("subelement")
        expect = rel.find("expect")
        if expect is None:
            raise EngineError(f"Companion rule {rule_id!r} requires an <expect> block")
        expect_type, expect_values = _parse_expect(rule_id, expect)
        rules.append(
            CompanionRule(
                rule_id=rule_id,
                applies_to=applies_to,
                location=location,
                element=element,
                expect_type=expect_type,
                expect_values=frozenset(expect_values),
                subelement=subelement,
            )
        )
    return rules, guide_name, payer, source_name


def _parse_expect(rule_id: str, expect: Any) -> tuple[str, frozenset[str]]:
    allow_el = expect.find("allow")
    disallow_el = expect.find("disallow")
    require_el = expect.find("require")
    kinds = (
        bool(allow_el is not None) + bool(disallow_el is not None) + bool(require_el is not None)
    )
    if kinds != 1:
        raise EngineError(
            f"Companion rule {rule_id!r} <expect> must contain exactly one of "
            "<allow>, <disallow>, or <require/>"
        )
    if allow_el is not None:
        values = [c.text for c in allow_el.findall("code") if c.text is not None]
        if not values:
            raise EngineError(f"Companion rule {rule_id!r} <allow> needs at least one <code>")
        return _EXPECT_ALLOW, frozenset(values)
    if disallow_el is not None:
        values = [c.text for c in disallow_el.findall("code") if c.text is not None]
        if not values:
            raise EngineError(f"Companion rule {rule_id!r} <disallow> needs at least one <code>")
        return _EXPECT_DISALLOW, frozenset(values)
    return _EXPECT_REQUIRE, frozenset()


class CompanionEngine:
    """Binds companion rules to a base map and evaluates each segment read-only."""

    def __init__(self, rules: list[CompanionRule], guide_name: str, payer: str, source_file: str):
        self._rules = rules
        self._guide_name = guide_name
        self._payer = payer
        self._source_file = source_file
        self._bound: dict[int, list[_BoundRule]] = {}
        self._base_source: dict[int, str] = {}
        self._loop_occurrence: dict[tuple[int, str], int] = {}
        self.report = CompanionReport(guide_name=guide_name, payer=payer, source_file=source_file)

    def bind_map(self, map_root: Any, base_map_file: str) -> None:
        """Resolve this map's applicable rules against its nodes (once per map)."""
        key = id(map_root)
        if key in self._bound:
            return
        self._base_source[key] = base_map_file
        bound: list[_BoundRule] = []
        conflicts: list[RuleConflict] = []
        for rule in self._rules:
            if rule.applies_to != map_root.id:
                continue
            seg_node, leaf, in_composite = self._resolve(map_root, rule)
            contradicts, reason = self._detect_contradiction(rule, leaf)
            if contradicts:
                conflicts.append(
                    RuleConflict(
                        rule_id=rule.rule_id,
                        location=rule.location,
                        element=rule.element,
                        payer_source=self._source_file,
                        base_source=base_map_file,
                        reason=reason,
                    )
                )
            bound.append(
                _BoundRule(
                    rule=rule,
                    seg_node=seg_node,
                    leaf=leaf,
                    in_composite=in_composite,
                    contradicts_base=contradicts,
                )
            )
        self._bound[key] = bound
        self.report.rule_conflicts.extend(conflicts)

    def evaluate(
        self,
        map_root: Any,
        seg_node: segment_if,
        seg_data: Any,
        seg_count: int,
        src_line: int,
    ) -> None:
        bound = self._bound.get(id(map_root))
        if not bound:
            return
        loop_node = pop_to_parent_loop(seg_node)
        loop_path = loop_node.get_path()
        # Scope the occurrence to the map: distinct transactions may share
        # identical loop paths and must not share a running counter.
        occ_key = (id(map_root), loop_path)
        first_seg = loop_node.get_first_seg()
        if first_seg is not None and first_seg is seg_node:
            self._loop_occurrence[occ_key] = self._loop_occurrence.get(occ_key, 0) + 1
        occurrence = self._loop_occurrence.get(occ_key, 1)
        for br in bound:
            if br.contradicts_base or br.seg_node is not seg_node:
                # A self-contradictory rule is reported once at the rule level
                # (rule_conflicts); it does not also generate per-data findings.
                continue
            value, data = self._read_value(seg_data, br)
            base_ok, base_codes = self._base_verdict(br, data)
            payer_ok = self._payer_verdict(br.rule, value)
            if base_ok and payer_ok:
                continue
            classification = BASE_INVALID if not base_ok else PAYER_REJECTED
            self.report.findings.append(
                ConstraintFinding(
                    classification=classification,
                    rule_id=br.rule.rule_id,
                    payer_source=self._source_file,
                    base_source=self._base_source[id(map_root)],
                    seg_id=seg_data.get_seg_id(),
                    element=br.rule.subelement or br.rule.element,
                    loop_path=loop_path,
                    loop_occurrence=occurrence,
                    seg_count=seg_count,
                    src_line=src_line,
                    value=value,
                    base_accepted=base_ok,
                    payer_accepted=payer_ok,
                    base_error_codes=tuple(base_codes),
                    expect_type=br.rule.expect_type,
                    expect_values=tuple(sorted(br.rule.expect_values)),
                    subelement=br.rule.subelement,
                )
            )

    @staticmethod
    def _resolve(map_root: Any, rule: CompanionRule) -> tuple[segment_if, element_if, bool]:
        seg_node = map_root.getnodebypath(rule.location)
        if not isinstance(seg_node, segment_if):
            raise EngineError(
                f"Companion rule {rule.rule_id!r} location {rule.location!r} is not a segment"
            )
        if rule.subelement:
            for child in seg_node.children:
                if isinstance(child, composite_if):
                    for sub in child.children:
                        if sub.id == rule.subelement:
                            return seg_node, sub, True
            raise EngineError(
                f"Companion rule {rule.rule_id!r} subelement {rule.subelement!r} "
                f"not found under {rule.location!r}"
            )
        for child in seg_node.children:
            if isinstance(child, element_if) and child.id == rule.element:
                return seg_node, child, False
        raise EngineError(
            f"Companion rule {rule.rule_id!r} element {rule.element!r} "
            f"not found under {rule.location!r}"
        )

    @staticmethod
    def _detect_contradiction(rule: CompanionRule, leaf: element_if) -> tuple[bool, str]:
        if rule.expect_type == _EXPECT_REQUIRE:
            if leaf.usage == "N":
                return True, (
                    "base guide marks the element Not Used (usage=N) but the companion requires it"
                )
            return False, ""
        base_codes = {code for code in leaf.valid_codes if code is not None}
        if not base_codes:
            # Base asserts no enumerated value set, so a payer list tightens
            # rather than contradicts.
            return False, ""
        if rule.expect_type == _EXPECT_ALLOW:
            forbidden = rule.expect_values - base_codes
            if forbidden:
                return True, (
                    f"companion allows {sorted(forbidden)} but the base guide code list "
                    f"does not include them"
                )
        else:  # disallow
            removed = rule.expect_values & base_codes
            if removed:
                return True, (
                    f"companion disallows {sorted(removed)} but the base guide code list "
                    f"includes them"
                )
        return False, ""

    @staticmethod
    def _base_verdict(br: _BoundRule, data: Any) -> tuple[bool, list[str]]:
        ok, errors = br.leaf.is_valid_errors(data)
        return ok, [e.err_cde for e in errors]

    @staticmethod
    def _payer_verdict(rule: CompanionRule, value: str | None) -> bool:
        present = value is not None and value != ""
        if rule.expect_type == _EXPECT_REQUIRE:
            return present
        if not present:
            # Absence is governed by the base usage rule, not by an allow/disallow list.
            return True
        if rule.expect_type == _EXPECT_ALLOW:
            return value in rule.expect_values
        return value not in rule.expect_values

    @staticmethod
    def _read_value(seg_data: Any, br: _BoundRule) -> tuple[str | None, Any]:
        if br.in_composite:
            parent = br.leaf.parent
            if not isinstance(parent, composite_if):
                return None, None
            comp = seg_data.get(f"{parent.seq:02d}")
            if comp is None or not isinstance(comp, Composite):
                return None, None
            sub = comp[br.leaf.seq - 1]
            return sub.get_value(), sub
        el = seg_data.get(f"{br.leaf.seq:02d}")
        if el is None:
            return None, None
        return el.get_value(), el
