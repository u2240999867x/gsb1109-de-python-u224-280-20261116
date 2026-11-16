import json
import os
import sys
import tempfile
import unittest
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))

import cif_uniform_precision as cup


SYNTHETIC_CIF = """data_test

loop_
_atom_site_label
_atom_site_U_Fourier_param_cos
A 2.0e-05(9)
B 0.0028(7)
C -1.9e-05(19)
D ?
E .
F 0.0
G -7.0e-06(5)
"""

SPEC = {
    "rounding": "half-even",
    "columns": {
        "_atom_site_U_Fourier_param_cos": {
            "min_decimals": 0,
            "max_decimals": 12,
            "value_tolerance": "0.5ulp_original",
            "su_tolerance": "0.5ulp_original",
        }
    },
}


class ParserTestCase(unittest.TestCase):

    def test_mantissa_esd_order(self):
        parsed = cup.parse_numeric_token("0.0028(7)")
        self.assertEqual(parsed.value, Decimal("0.0028"))
        self.assertEqual(parsed.su, Decimal("0.0007"))
        self.assertEqual(parsed.ulp, Decimal("0.0001"))

    def test_exponent_before_bracket_scales_esd(self):
        parsed = cup.parse_numeric_token("2.0e-05(9)")
        self.assertEqual(parsed.value, Decimal("2.0e-5"))
        self.assertEqual(parsed.su, Decimal("9e-6"))
        self.assertEqual(parsed.ulp, Decimal("1e-6"))

    def test_esd_scale_tracks_mantissa_and_exponent(self):
        fine = cup.parse_numeric_token("-7.0e-06(5)")
        self.assertEqual(fine.su, Decimal("5e-7"))
        coarse = cup.parse_numeric_token("1210.8(3)")
        self.assertEqual(coarse.su, Decimal("0.3"))

    def test_bracket_after_exponent_uses_same_scale(self):
        parsed = cup.parse_numeric_token("1.5(2)e-3")
        self.assertEqual(parsed.value, Decimal("0.0015"))
        self.assertEqual(parsed.su, Decimal("0.0002"))
        self.assertEqual(parsed.ulp, Decimal("0.0001"))

    def test_trailing_decimal_point(self):
        parsed = cup.parse_numeric_token("100.")
        self.assertEqual(parsed.value, Decimal("100"))
        self.assertEqual(parsed.ulp, Decimal("1"))
        self.assertIsNone(parsed.su)

    def test_special_and_plain_values(self):
        self.assertIsNone(cup.parse_numeric_token("?"))
        self.assertIsNone(cup.parse_numeric_token("."))
        plain = cup.parse_numeric_token("0.0")
        self.assertEqual(plain.value, Decimal("0.0"))
        self.assertIsNone(plain.su)


class RoundingTestCase(unittest.TestCase):

    def test_half_even_vs_half_up_tie(self):
        parsed = cup.parse_numeric_token("1.2345(2)")
        even_value, _ = cup.round_token(parsed, 3, "half-even")
        up_value, _ = cup.round_token(parsed, 3, "half-up")
        self.assertEqual(format(even_value, "f"), "1.234")
        self.assertEqual(format(up_value, "f"), "1.235")

    def test_fixed_point_keeps_uniform_decimals(self):
        parsed = cup.parse_numeric_token("2.0e-05(9)")
        value_q, su_q = cup.round_token(parsed, 6, "half-even")
        self.assertEqual(cup.format_fixed(value_q, su_q, 6), "0.000020(9)")


class EndToEndTestCase(unittest.TestCase):

    def _run(self, spec, expect_feasible):
        with tempfile.TemporaryDirectory() as tmp:
            cif_path = os.path.join(tmp, "in.cif")
            out_path = os.path.join(tmp, "out.cif")
            map_path = os.path.join(tmp, "map.tsv")
            report_path = os.path.join(tmp, "report.json")
            spec_path = os.path.join(tmp, "spec.json")
            with open(cif_path, "w") as handle:
                handle.write(SYNTHETIC_CIF)
            with open(spec_path, "w") as handle:
                json.dump(spec, handle)
            exit_code = cup.main([
                cif_path, "--spec", spec_path,
                "--output-cif", out_path,
                "--mapping", map_path,
                "--report", report_path,
            ])
            with open(report_path) as handle:
                report = json.load(handle)
            with open(map_path) as handle:
                mapping = handle.read().splitlines()
            if os.path.exists(out_path):
                with open(out_path) as handle:
                    output = handle.read()
            else:
                output = ""
            self.assertEqual(exit_code == 0, expect_feasible)
            self.assertEqual(report["feasible"], expect_feasible)
            return report, mapping, output

    def test_feasible_run_is_lossless_and_uniform(self):
        report, mapping, output = self._run(SPEC, True)
        column = report["columns"]["_atom_site_U_Fourier_param_cos"]
        self.assertEqual(column["chosen_decimals"], 7)
        self.assertEqual(column["blockers"], [])
        self.assertIn("0.0000200(90)", output)
        self.assertIn("-0.0000070(5)", output)
        self.assertIn("D ?", output)
        self.assertIn("E .", output)
        self.assertNotIn("e-0", output)
        statuses = dict((line.split("\t")[2], line.split("\t")[6])
                        for line in mapping[1:])
        self.assertEqual(statuses["?"], "preserved")
        self.assertEqual(statuses["."], "preserved")
        self.assertEqual(statuses["0.0"], "ok")
        for line in mapping[1:]:
            fields = line.split("\t")
            if fields[6] == "ok":
                self.assertEqual(Decimal(fields[4]), Decimal("0"))
                self.assertIn(fields[5], ("", "0E-7", "0.0000000", "0E-6"))

    def test_infeasible_run_lists_blocking_rows(self):
        strict = json.loads(json.dumps(SPEC))
        strict["columns"]["_atom_site_U_Fourier_param_cos"]["max_decimals"] = 3
        report, mapping, output = self._run(strict, False)
        column = report["columns"]["_atom_site_U_Fourier_param_cos"]
        self.assertIsNone(column["chosen_decimals"])
        blocked_lines = set(item["line"] for item in column["blockers"])
        self.assertIn(6, blocked_lines)
        self.assertIn(8, blocked_lines)
        self.assertNotIn(9, blocked_lines)
        self.assertEqual(output, "")
        statuses = [line.split("\t")[6] for line in mapping[1:]]
        self.assertIn("blocked", statuses)
        self.assertIn("preserved", statuses)

    def test_reparsed_tokens_match_originals(self):
        _, _, output = self._run(SPEC, True)
        expected = {
            "0.0000200(90)": (Decimal("2.0e-5"), Decimal("9e-6")),
            "-0.0000070(5)": (Decimal("-7.0e-6"), Decimal("5e-7")),
        }
        for token, (value, su) in expected.items():
            self.assertIn(token, output)
            reparsed = cup.parse_numeric_token(token)
            self.assertEqual(reparsed.value, value)
            self.assertEqual(reparsed.su, su)


if __name__ == "__main__":
    unittest.main()
