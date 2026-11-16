import json
import os
import unittest
from io import StringIO
from typing import Any

import pyx12.map_if
import pyx12.params
import pyx12.segment
import pyx12.x12n_document
from pyx12.companion import (
    BASE_INVALID,
    PAYER_REJECTED,
    CompanionEngine,
    load_companion,
)
from pyx12.errors import EngineError
from pyx12.test.x12testdata import datafiles

MAP_837 = "837.4010.X098.A1.xml"
NM1_PATH = "/ISA_LOOP/GS_LOOP/ST_LOOP/DETAIL/2000A/2010AA/NM1"
REF_PATH = "/ISA_LOOP/GS_LOOP/ST_LOOP/HEADER/REF"
FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "companion", "acme_837.xml")


def _guide(body: str) -> StringIO:
    return StringIO(f"<companion_guide><name>t</name><payer>P</payer>{body}</companion_guide>")


def _engine(map_root: Any, body: str, base_file: str = MAP_837) -> CompanionEngine:
    rules, guide_name, payer, source = load_companion(_guide(body))
    engine = CompanionEngine(rules, guide_name, payer, source)
    engine.bind_map(map_root, base_file)
    return engine


class LoadCompanion(unittest.TestCase):
    def test_parses_allow_disallow_require(self):
        guide = _guide(
            f'<rule id="a" applies_to="837"><location>{NM1_PATH}</location>'
            "<element>NM108</element><expect><allow><code>XX</code></allow></expect></rule>"
        )
        rules, name, payer, source = load_companion(guide)
        self.assertEqual(name, "t")
        self.assertEqual(payer, "P")
        self.assertEqual(source, "<stream>")
        self.assertEqual(rules[0].expect_type, "allow")
        self.assertEqual(set(rules[0].expect_values), {"XX"})

    def test_wrong_root_rejected(self):
        with self.assertRaises(EngineError):
            load_companion(StringIO("<other/>"))

    def test_missing_rule_attributes_rejected(self):
        with self.assertRaises(EngineError):
            load_companion(
                _guide(f"<rule><location>{NM1_PATH}</location><element>NM108</element></rule>")
            )

    def test_two_expect_kinds_rejected(self):
        with self.assertRaises(EngineError):
            load_companion(
                _guide(
                    f'<rule id="x" applies_to="837"><location>{NM1_PATH}</location>'
                    "<element>NM108</element><expect><allow><code>XX</code></allow>"
                    "<require/></expect></rule>"
                )
            )

    def test_unknown_element_rejected_at_bind(self):
        param = pyx12.params.params()
        map_root = pyx12.map_if.load_map_file(MAP_837, param)
        with self.assertRaises(EngineError):
            _engine(
                map_root,
                f'<rule id="x" applies_to="837"><location>{NM1_PATH}</location>'
                "<element>NM999</element><expect><require/></expect></rule>",
            )


class CompanionVerdicts(unittest.TestCase):
    def setUp(self):
        param = pyx12.params.params()
        self.map = pyx12.map_if.load_map_file(MAP_837, param)
        self.nm1 = self.map.getnodebypath(NM1_PATH)

    def test_payer_rejected_when_base_allows_but_companion_tightens(self):
        engine = _engine(
            self.map,
            f'<rule id="r" applies_to="837"><location>{NM1_PATH}</location>'
            "<element>NM108</element><expect><allow><code>XX</code></allow></expect></rule>",
        )
        engine.evaluate(
            self.map, self.nm1, pyx12.segment.Segment("NM1*85*1*P**A***24*1~", "~", "*", ":"), 1, 9
        )
        self.assertEqual(len(engine.report.findings), 1)
        finding = engine.report.findings[0]
        self.assertEqual(finding.classification, PAYER_REJECTED)
        self.assertTrue(finding.base_accepted)
        self.assertFalse(finding.payer_accepted)
        self.assertEqual(finding.base_error_codes, ())
        self.assertEqual(finding.value, "24")

    def test_base_invalid_dominates_and_keeps_base_codes(self):
        # NM108='MIM' fails the base guide (too long + invalid code). The
        # companion allow-list also rejects it, but the base verdict dominates.
        engine = _engine(
            self.map,
            f'<rule id="r" applies_to="837"><location>{NM1_PATH}</location>'
            "<element>NM108</element><expect><allow><code>XX</code></allow></expect></rule>",
        )
        engine.evaluate(
            self.map, self.nm1, pyx12.segment.Segment("NM1*85*1*P**A***MIM*1~", "~", "*", ":"), 1, 9
        )
        finding = engine.report.findings[0]
        self.assertEqual(finding.classification, BASE_INVALID)
        self.assertFalse(finding.base_accepted)
        self.assertIn("ELE_7_invalid_code", finding.base_error_codes)
        self.assertEqual(finding.payer_source, "<stream>")
        self.assertTrue(finding.base_source.endswith(MAP_837))

    def test_disallow_on_codeless_element_is_tightening_not_conflict(self):
        # NM103 (AN) has no base code list, so a payer disallow cannot
        # contradict the base guide; a disallowed present value is payer_rejected.
        engine = _engine(
            self.map,
            f'<rule id="r" applies_to="837"><location>{NM1_PATH}</location>'
            "<element>NM103</element>"
            "<expect><disallow><code>Sender 1</code></disallow></expect></rule>",
        )
        self.assertEqual(engine.report.rule_conflicts, [])
        seg = pyx12.segment.Segment("NM1*85*1*Sender 1**A***24*1~", "~", "*", ":")
        engine.evaluate(self.map, self.nm1, seg, 1, 9)
        self.assertEqual(engine.report.findings[0].classification, PAYER_REJECTED)

    def test_require_missing_situational_element_is_payer_rejected(self):
        # NM104 is situational (usage='S') in the base guide, so its absence
        # passes the base. A payer require turns that same absence into a
        # payer-only rejection — the clean "payer not accept" case.
        engine = _engine(
            self.map,
            f'<rule id="r" applies_to="837"><location>{NM1_PATH}</location>'
            "<element>NM104</element><expect><require/></expect></rule>",
        )
        engine.evaluate(
            self.map, self.nm1, pyx12.segment.Segment("NM1*85*1*P**A***24~", "~", "*", ":"), 1, 9
        )
        finding = engine.report.findings[0]
        self.assertEqual(finding.classification, PAYER_REJECTED)
        self.assertTrue(finding.base_accepted)
        self.assertIn(finding.value, (None, ""))
        self.assertFalse(finding.payer_accepted)

    def test_loop_occurrence_increments_per_loop_entry(self):
        engine = _engine(
            self.map,
            f'<rule id="r" applies_to="837"><location>{NM1_PATH}</location>'
            "<element>NM108</element><expect><allow><code>XX</code></allow></expect></rule>",
        )
        seg = pyx12.segment.Segment("NM1*85*1*P**A***24*1~", "~", "*", ":")
        engine.evaluate(self.map, self.nm1, seg, 1, 9)
        engine.evaluate(self.map, self.nm1, seg, 2, 15)
        self.assertEqual([f.loop_occurrence for f in engine.report.findings], [1, 2])
        self.assertEqual(
            {f.loop_path for f in engine.report.findings},
            {"/ISA_LOOP/GS_LOOP/ST_LOOP/DETAIL/2000A/2010AA"},
        )

    def test_disallowing_a_base_code_is_a_static_rule_conflict(self):
        engine = _engine(
            self.map,
            f'<rule id="r" applies_to="837"><location>{NM1_PATH}</location>'
            "<element>NM108</element><expect><disallow><code>24</code></disallow></expect></rule>",
        )
        self.assertEqual(len(engine.report.rule_conflicts), 1)
        conflict = engine.report.rule_conflicts[0]
        self.assertEqual(conflict.rule_id, "r")
        self.assertIn("base guide code list", conflict.reason)
        # The contradictory rule does not also emit per-data findings.
        engine.evaluate(
            self.map, self.nm1, pyx12.segment.Segment("NM1*85*1*P**A***24*1~", "~", "*", ":"), 1, 9
        )
        self.assertEqual(engine.report.findings, [])

    def test_require_vs_not_used_is_a_static_rule_conflict(self):
        # NM110 is usage='N' in the base guide; requiring it contradicts that.
        engine = _engine(
            self.map,
            f'<rule id="r" applies_to="837"><location>{NM1_PATH}</location>'
            "<element>NM110</element><expect><require/></expect></rule>",
        )
        self.assertEqual(len(engine.report.rule_conflicts), 1)
        self.assertIn("Not Used", engine.report.rule_conflicts[0].reason)

    def test_applies_to_mismatch_binds_no_rules(self):
        engine = _engine(
            self.map,
            f'<rule id="r" applies_to="835"><location>{NM1_PATH}</location>'
            "<element>NM108</element><expect><allow><code>XX</code></allow></expect></rule>",
        )
        self.assertEqual(engine._bound[id(self.map)], [])
        engine.evaluate(
            self.map, self.nm1, pyx12.segment.Segment("NM1*85*1*P**A***24*1~", "~", "*", ":"), 1, 9
        )
        self.assertEqual(engine.report.findings, [])

    def test_finding_carries_both_sources_and_location(self):
        engine = _engine(
            self.map,
            f'<rule id="r" applies_to="837"><location>{NM1_PATH}</location>'
            "<element>NM108</element><expect><allow><code>XX</code></allow></expect></rule>",
            base_file="837.4010.X098.A1.xml",
        )
        engine.evaluate(
            self.map, self.nm1, pyx12.segment.Segment("NM1*85*1*P**A***24*1~", "~", "*", ":"), 7, 42
        )
        doc = engine.report.to_dict()
        finding = doc["findings"][0]
        self.assertEqual(finding["sources"]["payer_companion"]["rule_id"], "r")
        self.assertEqual(finding["sources"]["base_guideline"]["file"], "837.4010.X098.A1.xml")
        self.assertEqual(finding["location"]["seg_id"], "NM1")
        self.assertEqual(finding["location"]["element"], "NM108")
        self.assertEqual(finding["location"]["seg_count"], 7)
        self.assertEqual(finding["location"]["src_line"], 42)


class CompanionIntegration(unittest.TestCase):
    def _run(self, companion_file: str | None) -> tuple[bool, dict[str, Any]]:
        param = pyx12.params.params()
        if companion_file:
            param.set("companion_file", companion_file)
        fd_json = StringIO()
        ok = pyx12.x12n_document.x12n_document(
            param, StringIO(datafiles["simple_837p"]["source"]), None, None, None, fd_json=fd_json
        )
        return ok, json.loads(fd_json.getvalue())

    def test_no_companion_keeps_legacy_output_and_passes(self):
        ok, doc = self._run(None)
        self.assertTrue(ok)
        self.assertEqual(list(doc.keys()), ["interchanges"])

    def test_companion_adds_section_without_touching_base_tree(self):
        ok_base, base_doc = self._run(None)
        ok, doc = self._run(FIXTURE)
        self.assertTrue(ok_base)
        self.assertFalse(ok)
        # The base validation tree is byte-for-byte identical: the companion
        # pass neither overwrites a verdict nor mutates the source data.
        self.assertEqual(doc["interchanges"], base_doc["interchanges"])
        companion = doc["companion_validation"]
        self.assertEqual(
            companion["summary"],
            {"base_invalid": 0, "payer_rejected": 1, "rule_conflicts": 1},
        )
        self.assertEqual(companion["payer"], "ACME")

    def test_payer_rejected_finding_records_two_independent_verdicts(self):
        _, doc = self._run(FIXTURE)
        rejected = [
            f
            for f in doc["companion_validation"]["findings"]
            if f["classification"] == PAYER_REJECTED
        ]
        self.assertEqual(len(rejected), 1)
        finding = rejected[0]
        self.assertTrue(finding["base_accepted"])
        self.assertFalse(finding["payer_accepted"])
        self.assertEqual(finding["sources"]["payer_companion"]["rule_id"], "ACME-NM108-XX")
        self.assertEqual(finding["sources"]["base_guideline"]["error_codes"], [])
        self.assertEqual(finding["location"]["loop_occurrence"], 1)

    def test_rule_conflict_is_reported_once_at_rule_level(self):
        _, doc = self._run(FIXTURE)
        conflicts = doc["companion_validation"]["rule_conflicts"]
        self.assertEqual([c["rule_id"] for c in conflicts], ["ACME-NM108-NO24"])
        # No per-data finding is classified as a rule conflict.
        self.assertNotIn(
            "rule_conflict",
            {f["classification"] for f in doc["companion_validation"]["findings"]},
        )


class CompanionCLIArg(unittest.TestCase):
    def test_companion_flag_parsed(self):
        from pyx12.scripts.x12valid import build_parser

        args = build_parser().parse_args(["--companion", "payer.xml", "f.x12"])
        self.assertEqual(args.companion, "payer.xml")


if __name__ == "__main__":
    unittest.main()
