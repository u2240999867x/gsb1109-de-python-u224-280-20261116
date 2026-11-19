"""Unit tests for dialbatch/resolver.py"""

import unittest

from dialbatch import (BatchReport, candidate_numbers, confirmed_numbers,
                       explain, resolve_batch)


class ResolveBatchTest(unittest.TestCase):
    """Batch resolution against finite candidate region lists."""

    def test_single_surviving_region_confirms_every_row(self):
        rows = ["030 901820",          # valid only under DE
                "089 12345678",        # valid only under DE
                "+43 1 58058",         # explicit international, same everywhere
                "00 41 44 123 45 67"]  # IDD form shared by DE/AT/CH
        report = resolve_batch(rows, ["DE", "AT", "CH"])
        self.assertTrue(report.consistent)
        self.assertEqual(report.surviving_regions, ("DE",))
        self.assertEqual(report.confirmed_numbers(), {
            "r1": "+4930901820",
            "r2": "+498912345678",
            "r3": "+43158058",
            "r4": "+41441234567",
        })
        self.assertEqual(report.eliminated["AT"][0][0], "r1")
        self.assertEqual(report.eliminated["CH"][0][0], "r1")
        self.assertTrue(report.report_for("r3").international)
        self.assertTrue(report.report_for("r4").international)
        self.assertFalse(report.report_for("r1").international)

    def test_multiple_survivors_leave_diverging_rows_open(self):
        rows = ["022 123 45 67",   # valid under DE, AT and CH, different E164
                "044 123 45 67",   # valid under DE and CH, not AT
                "+49 30 901820"]   # explicit international, converges
        report = resolve_batch(rows, ["DE", "AT", "CH"])
        self.assertEqual(report.surviving_regions, ("CH", "DE"))
        self.assertIn("AT", report.eliminated)
        self.assertEqual(report.confirmed_numbers(),
                         {"r3": "+4930901820"})
        self.assertEqual(report.candidate_numbers(), {
            "r1": {"CH": "+41221234567", "DE": "+49221234567"},
            "r2": {"CH": "+41441234567", "DE": "+49441234567"},
        })
        open_row = report.report_for("r1")
        self.assertFalse(open_row.is_confirmed)
        self.assertEqual(set(open_row.candidates), set(["CH", "DE"]))

    def test_nanp_regions_converge_on_same_full_numbers(self):
        rows = ["(212) 555-0177", "(416) 555-0142", "(650) 253-2222"]
        report = resolve_batch(rows, ["US", "CA"])
        self.assertEqual(report.surviving_regions, ("CA", "US"))
        self.assertEqual(report.confirmed_numbers(), {
            "r1": "+12125550177",
            "r2": "+14165550142",
            "r3": "+16502532222",
        })
        self.assertEqual(report.candidate_numbers(), {})

    def test_contradiction_reports_evidence_per_region(self):
        rows = [("german", "07911 123456"),       # possible/valid under DE only
                ("french", "01 23 45 67 89"),     # valid under FR only
                ("french2", "01 99 00 11 22")]    # second FR-only rejection of DE
        report = resolve_batch(rows, ["DE", "FR"])
        self.assertIsInstance(report, BatchReport)
        self.assertTrue(report.contradiction)
        self.assertEqual(report.surviving_regions, ())
        de_rows = [row_id for row_id, _reason in report.eliminated["DE"]]
        fr_rows = [row_id for row_id, _reason in report.eliminated["FR"]]
        self.assertEqual(de_rows, ["french", "french2"])
        self.assertEqual(fr_rows, ["german"])
        self.assertEqual(report.confirmed_numbers(), {})
        summary = explain(report)
        self.assertIn("no common explanation", summary)
        self.assertIn("DE", summary)
        self.assertIn("FR", summary)
        self.assertIn("french2", summary)

    def test_explicit_international_not_reread_domestically(self):
        # DE recognises the 00 IDD prefix; US (IDD 011) does not and falls
        # back to a domestic read, which cannot validate the same digits.
        report = resolve_batch(["00 43 1 58058"], ["DE", "US"])
        self.assertEqual(report.surviving_regions, ("DE",))
        row = report.report_for("r1")
        self.assertTrue(row.international)
        self.assertEqual(row.confirmed, "+43158058")
        self.assertIn("US", row.rejected)

    def test_possible_only_mode_keeps_more_explanations(self):
        strict = resolve_batch(["0151 1234567"], ["DE", "AT"])
        self.assertEqual(strict.surviving_regions, ("AT",))
        relaxed = resolve_batch(["0151 1234567"], ["DE", "AT"],
                                require_valid=False)
        self.assertEqual(relaxed.surviving_regions, ("AT", "DE"))
        self.assertEqual(relaxed.candidate_numbers(), {
            "r1": {"AT": "+431511234567", "DE": "+491511234567"},
        })

    def test_result_is_independent_of_input_order(self):
        rows = [("a", "022 123 45 67"),
                ("b", "044 123 45 67"),
                ("c", "+49 30 901820")]
        first = resolve_batch(rows, ["DE", "AT", "CH"])
        second = resolve_batch(list(reversed(rows)), ["CH", "AT", "DE"])
        self.assertEqual(first.surviving_regions, second.surviving_regions)
        self.assertEqual(first.confirmed_numbers(), second.confirmed_numbers())
        self.assertEqual(first.candidate_numbers(),
                         second.candidate_numbers())

        contradiction_rows = [("german", "07911 123456"),
                              ("french", "01 23 45 67 89"),
                              ("french2", "01 99 00 11 22")]
        third = resolve_batch(list(reversed(contradiction_rows)),
                              ["FR", "DE"])
        self.assertTrue(third.contradiction)
        self.assertEqual(set(third.eliminated), set(["DE", "FR"]))
        # Every rejecting row is recorded; evidence must not depend on order.
        forward = resolve_batch(contradiction_rows, ["DE", "FR"])
        self.assertEqual(third.eliminated, forward.eliminated)
        self.assertEqual(len(forward.eliminated["DE"]), 2)
        self.assertEqual(len(forward.eliminated["FR"]), 1)

    def test_explicit_row_ids_and_lookup(self):
        report = resolve_batch([("alpha", "030 901820"),
                                ("beta", "089 12345678")], ["DE", "AT"])
        self.assertEqual(report.report_for("alpha").confirmed,
                         "+4930901820")
        self.assertEqual(report.report_for("beta").confirmed,
                         "+498912345678")
        with self.assertRaises(KeyError):
            report.report_for("missing")

    def test_helper_wrappers_match_full_report(self):
        rows = ["022 123 45 67", "+49 30 901820"]
        report = resolve_batch(rows, ["DE", "CH"])
        self.assertEqual(confirmed_numbers(rows, ["DE", "CH"]),
                         report.confirmed_numbers())
        self.assertEqual(candidate_numbers(rows, ["DE", "CH"]),
                         report.candidate_numbers())

    def test_bad_candidate_regions(self):
        with self.assertRaises(ValueError):
            resolve_batch(["030 901820"], ["DE", "ZZ"])
        with self.assertRaises(ValueError):
            resolve_batch(["030 901820"], [])


if __name__ == '__main__':
    unittest.main()
