import io
import unittest

import pyx12.remit_recon
from pyx12.test.x12testdata import datafiles

RECON_835 = """ISA*00*          *00*          *ZZ*383319999      *ZZ*382999999      *090220*1816*U*00401*000003447*1*P*:~
GS*HP*383319999*382999999*20090220*1816*3444*X*004010X091A1~
ST*835*0001~
BPR*I*410*C*CHK************20090220~
TRN*1*0004926*1382999999~
N1*PR*Payer 1~
N1*PE*Provider 1*FI*382999999~
LX*1~
CLP*C1*1*500*350*50*HM*PCN1~
CAS*CO*45*60~
SVC*HC:A*300*200**1~
CAS*PR*1*40~
SVC*HC:B*200*150**1~
CLP*C2*1*100*80*0*HM*PCN2~
PLB*382999999*20090930*WO*-20~
SE*14*0001~
GE*1*3444~
IEA*1*000003447~
"""


def _parse(source):
    return pyx12.remit_recon.parse_remit(io.StringIO(source))


class Parse835TestCase(unittest.TestCase):
    def test_claims_and_lines(self):
        remit = _parse(datafiles["835id"]["source"])
        self.assertEqual(len(remit.payments), 1)
        claims = remit.payments[0].claims
        self.assertEqual(len(claims), 3)
        self.assertEqual(claims[0].claim_id, "123839-24635")
        self.assertEqual(str(claims[0].charge), "-310")
        self.assertEqual(str(claims[0].paid), "-210")
        self.assertEqual(len(claims[0].lines), 1)
        self.assertEqual(str(claims[0].lines[0].charge), "-310")

    def test_negative_amounts_keep_sign(self):
        remit = _parse(datafiles["835id"]["source"])
        claim = remit.payments[0].claims[0]
        self.assertEqual(str(claim.lines[0].adjustments[0].amount), "-100")
        provider_amounts = [str(a.amount) for a in remit.payments[0].adjustments]
        self.assertEqual(provider_amounts, ["-1008.1", "24.21", "5.95"])

    def test_adjustment_levels(self):
        remit = _parse(datafiles["835id"]["source"])
        claims = remit.payments[0].claims
        self.assertEqual([a.level for c in claims for a in c.adjustments], [])
        line_levels = [a.level for c in claims for line in c.lines for a in line.adjustments]
        self.assertEqual(line_levels, ["line", "line"])
        self.assertEqual(len(remit.payments[0].adjustments), 3)

    def test_claim_and_line_levels_not_double_counted(self):
        remit = _parse(RECON_835)
        claim = remit.payments[0].claims[0]
        recon = pyx12.remit_recon.reconcile_claim(claim)
        self.assertEqual(str(recon["total_gap"]), "100")
        self.assertEqual(str(recon["claim_adjustments"]), "60")
        self.assertEqual(str(recon["line_adjustments"]), "40")
        self.assertEqual(str(recon["line_unexplained"]), "110")
        self.assertEqual(str(recon["level_mismatch"]), "-110")
        self.assertEqual(
            recon["difference_sources"],
            "claim_adjustment;line_adjustment;line_unexplained;level_mismatch",
        )
        bucket_sum = (
            recon["claim_adjustments"]
            + recon["line_adjustments"]
            + recon["line_unexplained"]
            + recon["level_mismatch"]
        )
        self.assertEqual(bucket_sum, recon["total_gap"])

    def test_claim_without_service_lines_is_not_zero(self):
        remit = _parse(RECON_835)
        claim = remit.payments[0].claims[1]
        self.assertEqual(claim.claim_id, "C2")
        self.assertEqual(claim.lines, [])
        recon = pyx12.remit_recon.reconcile_claim(claim)
        self.assertIsNone(recon["line_adjustments"])
        self.assertIsNone(recon["line_unexplained"])
        self.assertIsNone(recon["level_mismatch"])
        self.assertEqual(str(recon["unexplained_residual"]), "20")
        self.assertEqual(recon["difference_sources"], "no_service_line_detail")

    def test_balanced_claim(self):
        remit = _parse(datafiles["835id"]["source"])
        recon = pyx12.remit_recon.reconcile_claim(remit.payments[0].claims[2])
        self.assertEqual(str(recon["total_gap"]), "0.00")
        self.assertEqual(recon["difference_sources"], "balanced")

    def test_line_level_source_attribution(self):
        remit = _parse(datafiles["835id"]["source"])
        recon = pyx12.remit_recon.reconcile_claim(remit.payments[0].claims[0])
        self.assertEqual(str(recon["total_gap"]), "-100")
        self.assertEqual(str(recon["line_adjustments"]), "-100")
        self.assertEqual(recon["difference_sources"], "line_adjustment")

    def test_payment_reconciliation(self):
        remit = _parse(RECON_835)
        pay_recon = pyx12.remit_recon.reconcile_payment(remit.payments[0])
        self.assertEqual(str(pay_recon["provider_adjustments"]), "-20")
        self.assertEqual(str(pay_recon["unexplained_residual"]), "0")
        self.assertEqual(pay_recon["difference_sources"], "provider_adjustment")

    def test_payment_unexplained(self):
        remit = _parse(datafiles["835id"]["source"])
        pay_recon = pyx12.remit_recon.reconcile_payment(remit.payments[0])
        self.assertEqual(str(pay_recon["claims_paid_total"]), "-510.25")
        self.assertEqual(str(pay_recon["unexplained_residual"]), "7438.40")
        self.assertEqual(pay_recon["difference_sources"], "provider_adjustment;payment_unexplained")


class WriteTablesTestCase(unittest.TestCase):
    def test_csv_tables(self):
        import csv
        import tempfile
        from pathlib import Path

        remit = _parse(datafiles["835id"]["source"])
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = pyx12.remit_recon.write_tables(remit, Path(tmpdir))
            self.assertEqual(len(paths), 4)
            with open(Path(tmpdir) / "claims.csv", encoding="utf-8") as fd:
                rows = list(csv.DictReader(fd))
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0]["charge"], "-310")
            self.assertEqual(rows[0]["line_charge_total"], "-310")
            with open(Path(tmpdir) / "adjustments.csv", encoding="utf-8") as fd:
                adj_rows = list(csv.DictReader(fd))
            levels = [row["level"] for row in adj_rows]
            self.assertEqual(levels.count("line"), 2)
            self.assertEqual(levels.count("provider"), 3)
            with open(Path(tmpdir) / "reconciliation.csv", encoding="utf-8") as fd:
                recon_rows = list(csv.DictReader(fd))
            self.assertEqual([row["row_type"] for row in recon_rows], ["claim"] * 3 + ["payment"])

    def test_no_service_line_columns_blank(self):
        import csv
        import tempfile
        from pathlib import Path

        remit = _parse(RECON_835)
        with tempfile.TemporaryDirectory() as tmpdir:
            pyx12.remit_recon.write_tables(remit, Path(tmpdir))
            with open(Path(tmpdir) / "claims.csv", encoding="utf-8") as fd:
                rows = list(csv.DictReader(fd))
            no_line_row = [row for row in rows if row["claim_id"] == "C2"][0]
            self.assertEqual(no_line_row["line_count"], "0")
            self.assertEqual(no_line_row["line_charge_total"], "")
            self.assertEqual(no_line_row["line_adj_total"], "")


if __name__ == "__main__":
    unittest.main()
