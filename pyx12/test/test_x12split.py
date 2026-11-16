import io
import unittest

from pyx12.rawx12file import RawX12File
from pyx12.x12split import split_document, split_transaction_set

ISA = "ISA*00*          *00*          *ZZ*SUBMITTER      *ZZ*RECEIVER       *260101*0100*^*00501*000000001*0*T*:"
GS = "GS*HC*SUB*REC*20260101*0100*1*X*005010X222A1"
GE = "GE*1*1"
IEA = "IEA*1*000000001"

HEADER = [
    "ST*837*0001*005010X222A1",
    "BHT*0019*00*1*20260101*0100*CH",
    "NM1*41*2*SUBMITTER*****46*SUB1",
    "PER*IC*CONTACT*TE*5551234",
    "NM1*40*2*RECEIVER*****46*REC1",
]
BILLING = ["HL*1**20*1", "NM1*85*2*PROVIDER*****XX*1234567890"]
SUBSCRIBER = [
    "HL*2*1*22*1",
    "SBR*P*18*******CI",
    "NM1*IL*1*DOE*JOHN****MI*MEMBER1",
    "NM1*PR*2*PAYER*****PI*PAYER1",
]
SE = "SE*18*0001"


def make_claim(clm_id, lines=1):
    segs = [f"CLM*{clm_id}*100***11:B:1*Y*A*Y*I"]
    for i in range(lines):
        segs.append(f"LX*{i + 1}")
        segs.append("SV1*HC:99213*50*UN*1***1")
    return segs


def make_doc(st_segs_list, ge=GE, iea=IEA):
    segs = [ISA, GS]
    for st_segs in st_segs_list:
        segs.extend(st_segs)
    segs.extend([ge, iea])
    return "~".join(segs) + "~\n"


def read_doc(text):
    return list(RawX12File(io.StringIO(text)))


def split_doc(text, max_segments):
    out = io.StringIO()
    warnings = split_document(io.StringIO(text), out, max_segments)
    return read_doc(out.getvalue()), warnings


def st_sets(segs):
    sets, cur = [], None
    for seg in segs:
        if seg.startswith("ST*"):
            cur = [seg]
        elif cur is not None:
            cur.append(seg)
            if seg.startswith("SE*"):
                sets.append(cur)
                cur = None
    return sets


def claims_of(st_segs):
    return [s.split("*")[1] for s in st_segs if s.startswith("CLM*")]


class SplitTransactionSetTest(unittest.TestCase):
    def test_under_limit_unchanged(self):
        ts = HEADER + BILLING + SUBSCRIBER + make_claim("C1") + [SE]
        self.assertEqual(split_transaction_set(ts, 100), [ts])

    def test_missing_st_rejected(self):
        with self.assertRaises(ValueError):
            split_transaction_set(["BHT*0019"], 10)

    def test_missing_se_rejected(self):
        ts = HEADER + BILLING + SUBSCRIBER + make_claim("C1")
        with self.assertRaises(ValueError):
            split_transaction_set(ts, 5)

    def test_split_preserves_order_and_context(self):
        ts = HEADER + BILLING + SUBSCRIBER + make_claim("C1") + make_claim("C2") + [SE]
        parts = split_transaction_set(ts, 15)
        self.assertEqual(len(parts), 2)
        for part in parts:
            self.assertLessEqual(len(part), 15)
            self.assertEqual(part[0].split("*")[0], "ST")
            self.assertEqual(part[-1].split("*")[0], "SE")
            se = part[-1].split("*")
            self.assertEqual(int(se[1]), len(part))
            self.assertEqual(se[2], part[0].split("*")[2])
            self.assertEqual(len(claims_of(part)), 1)
            self.assertIn("NM1*IL*1*DOE*JOHN****MI*MEMBER1", part)
            self.assertIn("HL*1**20*1", part)
            self.assertIn("HL*2*1*22*0", part)
        self.assertEqual(claims_of(parts[0]), ["C1"])
        self.assertEqual(claims_of(parts[1]), ["C2"])
        self.assertNotEqual(parts[0][0].split("*")[2], parts[1][0].split("*")[2])

    def test_oversize_claim_emitted_alone_wholesale(self):
        big = make_claim("BIG", lines=20)
        ts = HEADER + BILLING + SUBSCRIBER + big + make_claim("C2") + [SE]
        parts = split_transaction_set(ts, 30)
        self.assertEqual(len(parts), 2)
        self.assertEqual(claims_of(parts[0]), ["BIG"])
        self.assertGreater(len(parts[0]), 30)
        self.assertEqual(parts[0].count("SV1*HC:99213*50*UN*1***1"), 20)
        self.assertEqual(claims_of(parts[1]), ["C2"])
        self.assertLessEqual(len(parts[1]), 30)

    def test_unknown_segments_keep_affiliation(self):
        sub = SUBSCRIBER[:3] + ["ZZZ*2*Y"] + SUBSCRIBER[3:]
        c1 = make_claim("C1") + ["ZZZ*1*X"]
        ts = HEADER + BILLING + sub + c1 + make_claim("C2") + [SE]
        parts = split_transaction_set(ts, 17)
        self.assertEqual(len(parts), 2)
        self.assertIn("ZZZ*2*Y", parts[0])
        self.assertIn("ZZZ*2*Y", parts[1])
        self.assertIn("ZZZ*1*X", parts[0])
        self.assertNotIn("ZZZ*1*X", parts[1])

    def test_hl04_child_code_recomputed(self):
        patient = ["HL*3*2*23*0", "NM1*QC*1*DOE*JANE"]
        sub = [SUBSCRIBER[0].replace("22*1", "22*1")] + SUBSCRIBER[1:]
        ts = HEADER + BILLING + sub + patient + make_claim("C1") + make_claim("C2") + [SE]
        parts = split_transaction_set(ts, 17)
        self.assertEqual(len(parts), 2)
        for part in parts:
            self.assertIn("HL*1**20*1", part)
            self.assertIn("HL*2*1*22*1", part)
            self.assertIn("HL*3*2*23*0", part)
            self.assertIn("NM1*QC*1*DOE*JANE", part)
            self.assertEqual(len(claims_of(part)), 1)


class SplitDocumentTest(unittest.TestCase):
    def test_envelope_counts_rebuilt(self):
        ts = HEADER + BILLING + SUBSCRIBER + make_claim("C1") + make_claim("C2") + [SE]
        segs, warnings = split_doc(make_doc([ts]), 15)
        self.assertEqual(warnings, [])
        sets = st_sets(segs)
        self.assertEqual(len(sets), 2)
        self.assertIn("GE*2*1", segs)
        self.assertIn("IEA*1*000000001", segs)
        merged = [c for st in sets for c in claims_of(st)]
        self.assertEqual(merged, ["C1", "C2"])

    def test_under_limit_document_unchanged(self):
        ts = HEADER + BILLING + SUBSCRIBER + make_claim("C1") + [SE]
        doc = make_doc([ts])
        segs, warnings = split_doc(doc, 100)
        self.assertEqual(warnings, [])
        self.assertEqual(segs, read_doc(doc))

    def test_hl_ids_scoped_per_transaction(self):
        ts1 = HEADER + BILLING + SUBSCRIBER + make_claim("C1") + make_claim("C2") + [SE]
        ts2 = (
            ["ST*837*0002*005010X222A1"]
            + HEADER[1:]
            + BILLING
            + SUBSCRIBER
            + make_claim("C3")
            + ["SE*15*0002"]
        )
        segs, _ = split_doc(make_doc([ts1, ts2]), 15)
        sets = st_sets(segs)
        self.assertEqual(len(sets), 3)
        st02s = [s[0].split("*")[2] for s in sets]
        self.assertEqual(len(set(st02s)), 3)
        self.assertEqual(sets[2][0], f"ST*837*{st02s[2]}*005010X222A1")
        self.assertEqual(sets[2][-1], f"SE*15*{st02s[2]}")
        self.assertEqual(sets[2][1:-1], ts2[1:-1])
        self.assertIn("GE*3*1", segs)

    def test_non_837_passthrough(self):
        remit = ["ST*835*0007"] + ["BPR*I*100*C*ACH"] * 40 + ["SE*42*0007"]
        segs, warnings = split_doc(make_doc([remit]), 15)
        self.assertEqual(warnings, [])
        sets = st_sets(segs)
        self.assertEqual(len(sets), 1)
        self.assertEqual(sets[0], remit)

    def test_oversize_claim_warns(self):
        big = make_claim("BIG", lines=20)
        ts = HEADER + BILLING + SUBSCRIBER + big + [SE]
        segs, warnings = split_doc(make_doc([ts]), 30)
        self.assertEqual(len(warnings), 1)
        self.assertIn("cannot be split", warnings[0])
        sets = st_sets(segs)
        self.assertEqual(len(sets), 1)
        self.assertEqual(claims_of(sets[0]), ["BIG"])


if __name__ == "__main__":
    unittest.main()
