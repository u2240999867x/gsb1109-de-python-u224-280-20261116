import importlib
import unittest
from decimal import Decimal
from io import StringIO

pr = importlib.import_module("pyx12.examples.835_payer_responsibility")

SOURCE = """ISA*00*          *00*          *ZZ*383319999      *ZZ*382999999      *090220*1816*U*00501*000003450*1*P*:~
GS*HP*383319999*382999999*20090220*1816*3447*X*005010X221A1~
ST*835*40740~
BPR*I*1100*C*CHK************20090220~
TRN*1*0004930*1382999999~
N1*PR*Primary Payer Inc~
N3*1 Elm~
N4*Town*MI*49000~
PER*BL*Billing*TE*5551234567~
N1*PE*Provider 1*FI*382999999~
N3*2 Oak~
N4*Town*MI*49000~
LX*1~
CLP*COB1*1*1000*700*150*HM*PCN1~
CAS*CO*42*100~
CAS*PR*1*150~
NM1*QC*1*Doe*Jane****34*373900000~
NM1*TT*2*Secondary Payer Co*****FI*999888777~
AMT*ZK*150~
SVC*HC:99213*1000*700**1~
CAS*CO*42*100~
CAS*PR*1*150~
AMT*ZK*150~
CLP*COB2*2*800**200*HM*PCN2~
CAS*PR*2*200~
NM1*QC*1*Roe*John****34*373900001~
NM1*TT*2*Secondary Payer Co*****FI*999888777~
AMT*ZK*200~
SVC*HC:99214*800***1~
CAS*PR*2*200~
CLP*COB3*1*500*400*0*HM*PCN3~
NM1*QC*1*Poe*Pat****34*373900002~
NM1*TT*2*First Secondary*****FI*111111111~
NM1*TT*2*Second Secondary*****FI*222222222~
SE*33*40740~
GE*1*3447~
IEA*1*000003450~
"""


CORRECTED_SOURCE = """ISA*00*          *00*          *ZZ*383319999      *ZZ*382999999      *090220*1816*U*00501*000003460*1*P*:~
GS*HP*383319999*382999999*20090220*1816*3457*X*005010X221A1~
ST*835*40750~
BPR*I*700*C*CHK************20090220~
TRN*1*0004940*1382999999~
N1*PR*Sender Payer*FI*111111111~
N3*1 Elm~
N4*Town*MI*49000~
PER*BL*Billing*TE*5551234567~
N1*PE*Provider 1*FI*382999999~
N3*2 Oak~
N4*Town*MI*49000~
LX*1~
CLP*COB4*1*1000*700*0*HM*PCN4~
CAS*CO*42*100~
NM1*QC*1*Doe*Jane****34*373900000~
NM1*PR*2*Corrected Same*****FI*111111111~
CLP*COB5*1*1000*700*0*HM*PCN5~
CAS*CO*42*100~
NM1*QC*1*Roe*John****34*373900001~
NM1*PR*2*Corrected Other*****FI*222222222~
SE*20*40750~
GE*1*3457~
IEA*1*000003460~
"""


class _FakeSegment:
    seg_term = "~"

    def __init__(self, raw):
        self._raw = raw

    def format(self):
        return self._raw


class _FakeCasNode:
    cur_line_number = 7

    def __init__(self, values, raw):
        self._values = values
        self.seg_data = _FakeSegment(raw)

    def get_value(self, path):
        return self._values.get(path)


class PayerResponsibilityTableTestCase(unittest.TestCase):
    def setUp(self):
        self.table = pr.build_responsibility_table(StringIO(SOURCE))
        self.claims = {c["claim_control_number"]: c for c in self.table["claims"]}

    def test_sender_is_primary_payer(self):
        self.assertEqual(self.table["sender_payer"]["name"], "Primary Payer Inc")
        claim = self.claims["COB1"]
        orders = [(p["responsibility_order"], p["name"]) for p in claim["payers"]]
        self.assertEqual(
            orders,
            [(pr.PRIMARY, "Primary Payer Inc"), (pr.SECONDARY, "Secondary Payer Co")],
        )
        self.assertEqual(claim["payers"][0]["order_basis"], "835 sender N1*PR in 1000A header loop")
        self.assertEqual(claim["payers"][1]["order_basis"], "NM1*TT Crossover Carrier in 2100 loop")

    def test_primary_and_secondary_amounts(self):
        claim = self.claims["COB1"]
        self.assertEqual(claim["primary_paid"], Decimal("700"))
        self.assertTrue(claim["primary_paid_present"])
        self.assertEqual(claim["adjustment_totals_by_group"]["CO"], Decimal("100"))
        self.assertEqual(claim["adjustment_totals_by_group"]["PR"], Decimal("150"))

        rows = {r["responsibility_order"]: r for r in claim["payer_responsibility"]}
        self.assertEqual(rows[pr.PRIMARY]["paid_amount"], Decimal("700"))
        self.assertTrue(rows[pr.PRIMARY]["paid_present"])
        self.assertEqual(rows[pr.PRIMARY]["adjustment_groups"]["CO"], Decimal("100"))
        self.assertEqual(rows[pr.SECONDARY]["patient_responsibility_assumed"], Decimal("150"))
        self.assertEqual(rows[pr.SECONDARY]["prior_payer_amounts"][0]["amount"], Decimal("150"))
        self.assertEqual(rows[pr.SECONDARY]["prior_payer_amounts"][0]["qualifier"], "ZK")

    def test_service_line_association_and_raw_segments(self):
        claim = self.claims["COB1"]
        self.assertEqual(len(claim["service_lines"]), 1)
        line = claim["service_lines"][0]
        self.assertEqual(line["service_id"], "99213")
        self.assertEqual(line["line_item_charge"], Decimal("1000"))
        self.assertEqual(line["primary_paid"], Decimal("700"))
        self.assertEqual(line["patient_responsibility"], Decimal("150"))
        self.assertEqual(line["adjustment_totals_by_group"]["CO"], Decimal("100"))
        self.assertEqual(line["source_segment"], "SVC*HC:99213*1000*700**1")
        self.assertGreater(line["line_number"], 0)
        raw_ids = [s["segment_id"] for s in line["source_segments"]]
        self.assertIn("SVC", raw_ids)
        self.assertIn("CAS", raw_ids)
        self.assertIn("AMT", raw_ids)
        clp_raw = next(s for s in claim["source_segments"] if s["segment_id"] == "CLP")
        self.assertEqual(clp_raw["raw"], "CLP*COB1*1*1000*700*150*HM*PCN1")

    def test_missing_primary_result_is_not_zeroed(self):
        claim = self.claims["COB2"]
        self.assertIsNone(claim["primary_paid"])
        self.assertFalse(claim["primary_paid_present"])
        line = claim["service_lines"][0]
        self.assertIsNone(line["primary_paid"])

        rows = {r["responsibility_order"]: r for r in claim["payer_responsibility"]}
        self.assertIsNone(rows[pr.PRIMARY]["paid_amount"])
        self.assertFalse(rows[pr.PRIMARY]["paid_present"])
        # Secondary amounts come only from explicit PR/AMT data, never from a
        # zero substituted for the missing primary payment.
        self.assertEqual(rows[pr.SECONDARY]["patient_responsibility_assumed"], Decimal("200"))
        self.assertEqual(rows[pr.SECONDARY]["prior_payer_amounts"][0]["amount"], Decimal("200"))
        self.assertNotIn(Decimal("0"), [e["amount"] for e in claim["adjustments"]])

    def test_contradictory_order_is_listed_separately(self):
        claim = self.claims["COB3"]
        self.assertTrue(claim["order_contradictions"])
        self.assertIn("COB3", self.table["unresolved_claims"])

        orders = {p["name"]: p["responsibility_order"] for p in claim["payers"]}
        # The uncontested primary keeps its explicit order; only the two
        # carriers fighting over the secondary position are unresolved.
        self.assertEqual(orders["Primary Payer Inc"], pr.PRIMARY)
        self.assertEqual(orders["First Secondary"], pr.UNRESOLVED)
        self.assertEqual(orders["Second Secondary"], pr.UNRESOLVED)

        resp = {r["payer_name"]: r for r in claim["payer_responsibility"]}
        self.assertEqual(resp["Primary Payer Inc"]["responsibility_order"], pr.PRIMARY)
        self.assertTrue(resp["First Secondary"]["order_unresolved"])
        self.assertTrue(resp["Second Secondary"]["order_unresolved"])

        # The sender primary plus both crossover carriers are retained; file
        # position never picks a winner among the contradictory secondaries.
        self.assertEqual(len(claim["payers"]), 3)
        names = {p["name"] for p in claim["payers"]}
        self.assertEqual(names, {"Primary Payer Inc", "First Secondary", "Second Secondary"})

    def test_cas_multi_pair_segment(self):
        node = _FakeCasNode(
            {
                "CAS01": "PR",
                "CAS02": "1",
                "CAS03": "10",
                "CAS05": "2",
                "CAS06": "20",
                "CAS08": "3",
                "CAS09": "30",
            },
            "CAS*PR*1*10*2*20*3*30~",
        )
        entries = pr._cas_entries([node])
        self.assertEqual(
            [(e["group_code"], e["reason_code"], e["amount"]) for e in entries],
            [
                ("PR", "1", Decimal("10")),
                ("PR", "2", Decimal("20")),
                ("PR", "3", Decimal("30")),
            ],
        )
        self.assertTrue(all(e["line_number"] == 7 for e in entries))
        self.assertEqual(entries[0]["source_segment"], "CAS*PR*1*10*2*20*3*30")


class CorrectedPriorityPayerTestCase(unittest.TestCase):
    def setUp(self):
        self.table = pr.build_responsibility_table(StringIO(CORRECTED_SOURCE))
        self.claims = {c["claim_control_number"]: c for c in self.table["claims"]}

    def test_matching_corrected_payer_supersedes_sender_quietly(self):
        claim = self.claims["COB4"]
        self.assertEqual(claim["order_contradictions"], [])
        self.assertEqual(len(claim["payers"]), 1)
        payer = claim["payers"][0]
        self.assertEqual(payer["responsibility_order"], pr.PRIMARY)
        self.assertEqual(payer["name"], "Corrected Same")
        self.assertEqual(payer["identifier"], "111111111")
        self.assertEqual(payer["order_basis"], "NM1*PR Corrected Priority Payer in 2100 loop")
        self.assertNotIn("COB4", self.table["unresolved_claims"])

    def test_conflicting_corrected_payer_is_unresolved_not_file_ordered(self):
        claim = self.claims["COB5"]
        self.assertEqual(
            claim["order_contradictions"],
            ["conflicting primary payer identity between header N1*PR and claim NM1*PR"],
        )
        self.assertIn("COB5", self.table["unresolved_claims"])
        self.assertEqual(len(claim["payers"]), 2)
        self.assertTrue(all(p["responsibility_order"] == pr.UNRESOLVED for p in claim["payers"]))
        names = {p["name"] for p in claim["payers"]}
        self.assertEqual(names, {"Sender Payer", "Corrected Other"})


if __name__ == "__main__":
    unittest.main()
