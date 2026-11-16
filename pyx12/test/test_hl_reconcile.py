import unittest
from io import StringIO

import pyx12.hl_reconcile as hlr
import pyx12.map_if
import pyx12.params


def _envelope(hl_lines, st_control="11280001", vriic="005010X222A1", icvn="00501", fic="HC"):
    isa11 = "^" if icvn == "00501" else "U"
    lines = [
        "ISA*00*          *00*          *ZZ*ZZ000          *ZZ*ZZ001          "
        f"*030828*1128*{isa11}*{icvn}*000010121*0*T*:~",
        f"GS*{fic}*ZZ000*ZZ001*20030828*1128*17*X*{vriic}~",
        f"ST*837*{st_control}~",
    ]
    lines.extend(hl_lines)
    lines.extend(
        [
            f"SE*{len(hl_lines) + 2}*{st_control}~",
            "GE*1*17~",
            "IEA*1*000010121~",
        ]
    )
    return StringIO("\n".join(lines) + "\n")


class GuidelineLoopSpecs(unittest.TestCase):
    def setUp(self):
        self.param = pyx12.params.params()

    def test_837p_5010_parent_levels(self):
        map_root = pyx12.map_if.load_map_file("837.5010.X222.A1.xml", self.param)
        specs, loop_by_id = hlr._build_loop_specs(map_root)
        self.assertEqual(specs["20"].parent_levels, frozenset())
        self.assertEqual(specs["20"].loop_id, "2000A")
        self.assertEqual(specs["22"].parent_levels, frozenset({"20"}))
        self.assertEqual(specs["22"].loop_id, "2000B")
        self.assertEqual(specs["23"].parent_levels, frozenset({"22"}))
        self.assertEqual(specs["23"].loop_id, "2000C")
        self.assertEqual(loop_by_id["2000A"], "20")

    def test_837p_4010_parent_levels(self):
        map_root = pyx12.map_if.load_map_file("837.4010.X098.A1.xml", self.param)
        specs, _ = hlr._build_loop_specs(map_root)
        self.assertEqual(specs["22"].parent_levels, frozenset({"20"}))
        self.assertEqual(specs["23"].parent_levels, frozenset({"22"}))


class CleanHierarchy(unittest.TestCase):
    def setUp(self):
        self.param = pyx12.params.params()

    def test_no_conflicts(self):
        fd = _envelope(
            [
                "HL*1**20*1~",
                "HL*2*1*22*1~",
                "HL*3*2*23*0~",
                "HL*4*1*22*0~",
            ]
        )
        report = hlr.reconcile(fd, self.param)
        self.assertTrue(report.solvable)
        self.assertEqual(report.conflicts, ())
        self.assertEqual(report.contradictions, ())
        self.assertEqual(report.recommendations, ())
        self.assertEqual(report.total_cost, 0)
        self.assertEqual(report.solution_count, 1)
        self.assertEqual(len(report.transactions), 1)
        declared = {a.hl_id: a.parent_id for a in report.auto_confirmed}
        self.assertEqual(declared, {"1": None, "2": "1", "3": "2", "4": "1"})

    def test_raw_segments_preserved(self):
        hl_lines = [
            "HL*1**20*1~",
            "HL*2*1*22*0~",
        ]
        fd = _envelope(hl_lines)
        report = hlr.reconcile(fd, self.param)
        raw_texts = [seg.text for seg in report.transactions[0].segments]
        self.assertEqual(raw_texts, hl_lines)
        self.assertEqual(report.transactions[0].declarations[0].raw.text, "HL*1**20*1~")


class ConflictResolution(unittest.TestCase):
    def setUp(self):
        self.param = pyx12.params.params()
        self.st = "11280001"

    def test_patient_under_provider_conflicts_and_is_corrected(self):
        fd = _envelope(
            [
                "HL*1**20*1~",
                "HL*2*1*22*1~",
                "HL*3*1*23*0~",
            ],
            st_control=self.st,
        )
        allowed = [hlr.AllowedCorrection(self.st, "3", "2", 5)]
        report = hlr.reconcile(fd, self.param, allowed)
        self.assertTrue(report.solvable)
        # HL2 declares children (HL04=1) while HL3 is not its child, so the
        # initial stream carries both a placement and a child-code conflict;
        # one HL02 correction clears both at once.
        self.assertEqual(len(report.conflicts), 2)
        conflict = next(c for c in report.conflicts if c.hl_id == "3")
        self.assertIn("parent_level_not_allowed", conflict.reasons)
        self.assertEqual(conflict.hl_parent_id, "1")
        self.assertEqual(conflict.hl_parent_level, "20")
        self.assertEqual(conflict.guideline_parent_levels, frozenset({"22"}))
        self.assertEqual(report.total_cost, 5)
        self.assertEqual(len(report.recommendations), 1)
        rec = report.recommendations[0]
        self.assertEqual((rec.hl_id, rec.current_parent, rec.proposed_parent), ("3", "1", "2"))
        confirmed = {a.hl_id: a.parent_id for a in report.auto_confirmed}
        self.assertEqual(confirmed["3"], "2")

    def test_cheapest_authorized_correction_wins(self):
        fd = _envelope(
            [
                "HL*1**20*1~",
                "HL*2*1*22*1~",
                "HL*3*2*23*0~",
                "HL*4*1*22*1~",
                "HL*5*4*23*0~",
                "HL*6*1*23*0~",
            ]
        )
        allowed = [
            hlr.AllowedCorrection(self.st, "6", "2", 10),
            hlr.AllowedCorrection(self.st, "6", "4", 2),
        ]
        report = hlr.reconcile(fd, self.param, allowed)
        self.assertTrue(report.solvable)
        self.assertEqual(report.total_cost, 2)
        self.assertEqual(len(report.recommendations), 1)
        self.assertEqual(report.recommendations[0].proposed_parent, "4")

    def test_two_conflicts_min_combination(self):
        fd = _envelope(
            [
                "HL*1**20*1~",
                "HL*2*1*22*1~",
                "HL*3*1*23*0~",
                "HL*4*1*22*1~",
                "HL*5*1*23*0~",
            ]
        )
        allowed = [
            hlr.AllowedCorrection(self.st, "3", "2", 7),
            hlr.AllowedCorrection(self.st, "5", "4", 3),
        ]
        report = hlr.reconcile(fd, self.param, allowed)
        self.assertTrue(report.solvable)
        self.assertEqual(report.total_cost, 10)
        self.assertEqual(
            {(r.hl_id, r.proposed_parent) for r in report.recommendations},
            {("3", "2"), ("5", "4")},
        )

    def test_tied_solutions_share_only_common_assignments(self):
        fd = _envelope(
            [
                "HL*1**20*1~",
                "HL*2**20*1~",
                "HL*3*1*22*0~",
                "HL*4*2*22*0~",
                "HL*5**22*0~",
            ]
        )
        allowed = [
            hlr.AllowedCorrection(self.st, "5", "1", 4),
            hlr.AllowedCorrection(self.st, "5", "2", 4),
        ]
        report = hlr.reconcile(fd, self.param, allowed)
        self.assertTrue(report.solvable)
        self.assertEqual(report.solution_count, 2)
        self.assertEqual(report.total_cost, 4)
        pending = {(p.hl_id, p.candidate_parents) for p in report.pending}
        self.assertEqual(pending, {("5", ("1", "2"))})
        auto = {a.hl_id for a in report.auto_confirmed}
        self.assertEqual(auto, {"1", "2", "3", "4"})
        self.assertEqual(
            {(r.solution_index, r.proposed_parent) for r in report.recommendations},
            {(0, "1"), (1, "2")},
        )

    def test_root_with_parent_corrected_to_none(self):
        fd = _envelope(["HL*1*9*20*0~"])
        allowed = [hlr.AllowedCorrection(self.st, "1", None, 2)]
        report = hlr.reconcile(fd, self.param, allowed)
        self.assertTrue(report.solvable)
        self.assertIn("root_with_parent", report.conflicts[0].reasons)
        self.assertEqual(report.recommendations[0].proposed_parent, None)
        self.assertEqual(report.auto_confirmed[0].parent_id, None)


class ChildCodeAndStructural(unittest.TestCase):
    def setUp(self):
        self.param = pyx12.params.params()
        self.st = "11280001"

    def test_child_code_contradiction_resolved_by_reparent(self):
        fd = _envelope(
            [
                "HL*1**20*1~",
                "HL*2*1*22*0~",
                "HL*4*1*22*1~",
                "HL*3*2*23*0~",
            ]
        )
        allowed = [hlr.AllowedCorrection(self.st, "3", "4", 1)]
        report = hlr.reconcile(fd, self.param, allowed)
        self.assertTrue(report.solvable)
        self.assertIn("child_code_contradiction", report.conflicts[0].reasons)
        self.assertEqual(report.recommendations[0].proposed_parent, "4")

    def test_no_authorized_correction_is_contradiction(self):
        fd = _envelope(
            [
                "HL*1**20*1~",
                "HL*2*1*22*1~",
                "HL*3*1*23*0~",
            ]
        )
        report = hlr.reconcile(fd, self.param)
        self.assertFalse(report.solvable)
        self.assertEqual(report.solution_count, 0)
        # HL3's parent placement and HL2's child-code declaration are both
        # unresolvable without an authorized HL02 change.
        self.assertEqual({c.hl_id for c in report.contradictions}, {"2", "3"})
        self.assertIn(
            "parent_level_not_allowed",
            next(c for c in report.contradictions if c.hl_id == "3").reasons,
        )
        self.assertEqual(report.auto_confirmed, ())

    def test_duplicate_hl_id_is_structural_contradiction(self):
        fd = _envelope(
            [
                "HL*1**20*1~",
                "HL*2*1*22*0~",
                "HL*2*1*22*0~",
            ]
        )
        report = hlr.reconcile(fd, self.param)
        self.assertFalse(report.solvable)
        self.assertTrue(any("hl_id_not_unique" in c.reasons for c in report.contradictions))

    def test_cycle_detected_and_repaired(self):
        fd = _envelope(
            [
                "HL*1**20*1~",
                "HL*2*3*22*0~",
                "HL*3*2*22*0~",
            ]
        )
        report = hlr.reconcile(fd, self.param)
        self.assertFalse(report.solvable)
        reasons = {c.hl_id: c.reasons for c in report.conflicts}
        self.assertIn("cycle", reasons["2"])
        self.assertIn("cycle", reasons["3"])

        fd = _envelope(
            [
                "HL*1**20*1~",
                "HL*2*3*22*0~",
                "HL*3*2*22*0~",
            ]
        )
        allowed = [
            hlr.AllowedCorrection(self.st, "2", "1", 1),
            hlr.AllowedCorrection(self.st, "3", "1", 1),
        ]
        report = hlr.reconcile(fd, self.param, allowed)
        self.assertTrue(report.solvable)
        self.assertEqual(report.total_cost, 2)
        confirmed = {a.hl_id: a.parent_id for a in report.auto_confirmed}
        self.assertEqual(confirmed["2"], "1")
        self.assertEqual(confirmed["3"], "1")

    def test_recommendations_do_not_mutate_input_segments(self):
        hl_lines = [
            "HL*1**20*1~",
            "HL*2*1*22*1~",
            "HL*3*1*23*0~",
        ]
        fd = _envelope(hl_lines)
        report = hlr.reconcile(fd, self.param, [hlr.AllowedCorrection(self.st, "3", "2", 5)])
        declaration = report.transactions[0].declarations[2]
        self.assertEqual(declaration.raw.text, "HL*3*1*23*0~")
        self.assertEqual(declaration.parent_id, "1")
        self.assertEqual(
            [seg.text for seg in report.transactions[0].segments],
            hl_lines,
        )


class TransactionScope(unittest.TestCase):
    def setUp(self):
        self.param = pyx12.params.params()

    def test_parent_id_not_resolved_across_st(self):
        st1 = _envelope(
            [
                "HL*1**20*1~",
                "HL*2*1*22*0~",
            ],
            st_control="000000001",
        ).getvalue()
        st2 = _envelope(
            [
                "HL*5**20*1~",
                "HL*6*5*22*1~",
                "HL*7*2*23*0~",
            ],
            st_control="000000002",
        ).getvalue()
        fd = StringIO(st1 + st2)
        report = hlr.reconcile(fd, self.param)
        st2_report = next(t for t in report.transactions if t.st_control_number == "000000002")
        conflict = next(c for c in st2_report.conflicts if c.hl_id == "7")
        self.assertIn("parent_not_in_transaction", conflict.reasons)
        self.assertFalse(report.solvable)

        fd = StringIO(st1 + st2)
        allowed = [hlr.AllowedCorrection("000000002", "7", "6", 1)]
        report = hlr.reconcile(fd, self.param, allowed)
        self.assertTrue(report.solvable)
        self.assertEqual(report.total_cost, 1)

    def test_correction_scoped_to_own_transaction(self):
        st1 = _envelope(["HL*1**20*1~", "HL*2*1*22*0~"], st_control="000000001").getvalue()
        st2 = _envelope(
            [
                "HL*1**20*1~",
                "HL*2*1*22*1~",
                "HL*3*1*23*0~",
            ],
            st_control="000000002",
        ).getvalue()
        fd = StringIO(st1 + st2)
        # A correction addressed at ST1 must not be usable inside ST2.
        allowed = [hlr.AllowedCorrection("000000001", "3", "2", 1)]
        report = hlr.reconcile(fd, self.param, allowed)
        self.assertFalse(report.solvable)
        self.assertTrue(any(c.st_control_number == "000000002" for c in report.contradictions))


class NonClaimTransactions(unittest.TestCase):
    def setUp(self):
        self.param = pyx12.params.params()

    def test_non_837_transaction_has_no_report(self):
        fd = _envelope(["NTE*1*no claim here~"], vriic="005010X221A1", fic="HP")
        report = hlr.reconcile(fd, self.param)
        self.assertEqual(report.transactions, ())
        self.assertTrue(report.solvable)
        self.assertEqual(report.solution_count, 1)


if __name__ == "__main__":
    unittest.main()
