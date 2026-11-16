import unittest
from io import StringIO

import pyx12.eligibility271 as el271


def _seg(*elems):
    return "*".join(elems)


def _271(detail):
    segs = [
        "ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       "
        "*200101*1253*U*00401*000000001*0*T*:",
        "GS*HB*SENDER*RECEIVER*20200101*1253*1*X*004010X092A1",
        "ST*271*0001",
        "BHT*0022*11*10001234*20200101*1253",
        "HL*1**20*1",
        "NM1*PR*2*PAYER CO*****PI*PAYERID",
        "HL*2*1*21*1",
        "NM1*1P*2*PROVIDER*****XX*1234567890",
    ]
    segs += list(detail)
    segs.append("SE*%i*0001" % (len(segs) - 1))
    segs.append("GE*1*1")
    segs.append("IEA*1*000000001")
    return "~\n".join(segs) + "~\n"


DETAIL = [
    "HL*3*2*22*0",
    "TRN*1*93175-012547*9877281234",
    "NM1*IL*1*DOE*JOHN****MI*W123456789",
    "DMG*D8*19700101*M",
    "DTP*291*RD8*20200101-20201231",
    _seg("EB", "1", "IND", "30"),
    _seg("EB", "A", "IND", "47", "MA", "", "", "27.50"),
    _seg("EB", "B", "IND", "47", "MA", "", "", "", "20", "", "", "", "Y"),
    _seg("EB", "I", "IND", "98"),
    _seg("EB", "1", "IND", "50"),
    _seg("EB", "I", "IND", "50"),
    _seg("EB", "1", "IND", "52"),
    _seg("EB", "I", "IND", "52"),
    "MSG*Verified non-covered per plan documents",
    _seg("EB", "1", "IND", "53"),
    "MSG*Coverage confirmed by payer representative",
    _seg("EB", "I", "IND", "53"),
    _seg("EB", "V", "IND", "60"),
    _seg("EB", "A", "IND", "51", "MA", "", "", "5"),
    _seg("EB", "I", "IND", "51", "", "", "", "", "", "", "", "", "N"),
    "HL*4*3*23*0",
    "TRN*2*93175-012548*9877281234",
    "NM1*03*1*DOE*JANE****MI*W123456789",
    "DTP*291*RD8*20200101-20201231",
    _seg("EB", "1", "IND", "30"),
    "DTP*307*D8*20200301",
    _seg("EB", "I", "IND", "47"),
]


class Eligibility271TestCase(unittest.TestCase):
    def setUp(self):
        self.response = el271.parse_eligibility_response(StringIO(_271(DETAIL)))

    def _query(self, service_types, **kwargs):
        if isinstance(service_types, str):
            service_types = (service_types,)
        query = el271.BenefitQuery(service_types=tuple(service_types), **kwargs)
        return el271.query_benefits(self.response, query)

    def _subscriber_query(self, service_types, **kwargs):
        return self._query(
            service_types, role=el271.ROLE_SUBSCRIBER, member_id="W123456789", **kwargs
        )

    def _dependent_query(self, service_types, **kwargs):
        return self._query(
            service_types, role=el271.ROLE_DEPENDENT, member_id="W123456789", **kwargs
        )

    def _conclusion(self, result, service_type):
        for concl in result.conclusions:
            if concl.service_type == service_type:
                return concl
        self.fail("no conclusion for service type %s" % service_type)


class Parse(Eligibility271TestCase):
    def test_beneficiaries(self):
        self.assertEqual(len(self.response.beneficiaries), 2)
        sub, dep = self.response.beneficiaries
        self.assertEqual(sub.role, el271.ROLE_SUBSCRIBER)
        self.assertEqual(dep.role, el271.ROLE_DEPENDENT)
        self.assertEqual(sub.member_id, "W123456789")
        self.assertEqual(dep.member_id, "W123456789")
        self.assertEqual(sub.first_name, "JOHN")
        self.assertEqual(dep.first_name, "JANE")
        self.assertEqual(sub.dob, "19700101")
        self.assertEqual(dep.dob, None)

    def test_statements_kept_separate_per_beneficiary(self):
        sub, dep = self.response.beneficiaries
        self.assertEqual(len(sub.statements), 13)
        self.assertEqual(len(dep.statements), 2)
        for stmt in sub.statements:
            self.assertIn("2110C", stmt.ref.loop_path)
        for stmt in dep.statements:
            self.assertIn("2110D", stmt.ref.loop_path)

    def test_statement_qualifiers(self):
        sub = self.response.beneficiaries[0]
        copay = sub.statements[1]
        self.assertEqual(copay.info_code, "A")
        self.assertEqual(copay.coverage_level, "IND")
        self.assertEqual(copay.service_types, ("47",))
        self.assertEqual(copay.insurance_type, "MA")
        self.assertEqual(copay.amount, "27.50")
        self.assertEqual(copay.in_network, None)
        coins = sub.statements[2]
        self.assertEqual(coins.percent, "20")
        self.assertEqual(coins.in_network, "Y")

    def test_original_positions_preserved(self):
        sub, dep = self.response.beneficiaries
        refs = [s.ref for s in sub.statements] + [s.ref for s in dep.statements]
        counts = [r.seg_count for r in refs]
        self.assertEqual(counts, sorted(counts))
        self.assertEqual(len(set(counts)), len(counts))
        for r in refs:
            self.assertGreater(r.line_number, 0)
        self.assertLess(sub.statements[-1].ref.seg_count, dep.statements[0].ref.seg_count)

    def test_loop_and_statement_dates(self):
        sub, dep = self.response.beneficiaries
        self.assertEqual(len(sub.loop_dates), 1)
        self.assertEqual(sub.loop_dates[0].start, "20200101")
        self.assertEqual(sub.loop_dates[0].end, "20201231")
        dep_stmt = dep.statements[0]
        self.assertEqual(len(dep_stmt.dates), 1)
        self.assertEqual(dep_stmt.dates[0].qualifier, "307")
        self.assertEqual(dep_stmt.dates[0].start, "20200301")
        self.assertEqual(dep_stmt.dates[0].end, "20200301")


class Query(Eligibility271TestCase):
    def test_covered(self):
        result = self._subscriber_query("30")
        self.assertTrue(result.matched)
        concl = self._conclusion(result, "30")
        self.assertEqual(concl.status, el271.STATUS_COVERED)
        self.assertEqual(len(concl.supporting), 1)
        self.assertIn("2110C", concl.supporting[0].loop_path)

    def test_repeated_service_type_not_collapsed(self):
        result = self._subscriber_query("47")
        concl = self._conclusion(result, "47")
        self.assertEqual(concl.status, el271.STATUS_COVERED)
        self.assertEqual(len(concl.statements), 2)
        amounts = sorted(s.amount or s.percent for s in concl.statements)
        self.assertEqual(amounts, ["20", "27.50"])

    def test_not_covered(self):
        result = self._subscriber_query("98")
        concl = self._conclusion(result, "98")
        self.assertEqual(concl.status, el271.STATUS_NOT_COVERED)
        self.assertEqual(len(concl.supporting), 1)

    def test_conflict_without_adjudication_basis(self):
        result = self._subscriber_query("50")
        concl = self._conclusion(result, "50")
        self.assertEqual(concl.status, el271.STATUS_CONFLICT)
        self.assertEqual(len(concl.supporting), 2)

    def test_adjudication_basis_negative(self):
        result = self._subscriber_query("52")
        concl = self._conclusion(result, "52")
        self.assertEqual(concl.status, el271.STATUS_NOT_COVERED)

    def test_adjudication_basis_positive(self):
        result = self._subscriber_query("53")
        concl = self._conclusion(result, "53")
        self.assertEqual(concl.status, el271.STATUS_COVERED)

    def test_neutral_statement_is_unknown_not_uncovered(self):
        result = self._subscriber_query("60")
        concl = self._conclusion(result, "60")
        self.assertEqual(concl.status, el271.STATUS_UNKNOWN)
        self.assertEqual(len(concl.supporting), 1)

    def test_unanswered_service_is_unknown(self):
        result = self._subscriber_query("71")
        concl = self._conclusion(result, "71")
        self.assertEqual(concl.status, el271.STATUS_UNKNOWN)
        self.assertEqual(concl.supporting, ())

    def test_dependent_not_filled_from_subscriber(self):
        result = self._dependent_query("98")
        concl = self._conclusion(result, "98")
        self.assertEqual(concl.status, el271.STATUS_UNKNOWN)
        result = self._dependent_query("47")
        concl = self._conclusion(result, "47")
        self.assertEqual(concl.status, el271.STATUS_NOT_COVERED)
        self.assertIn("2110D", concl.supporting[0].loop_path)

    def test_same_member_id_is_ambiguous_without_role(self):
        result = self._query("30", member_id="W123456789")
        self.assertFalse(result.matched)
        self.assertIsNone(result.beneficiary)
        concl = self._conclusion(result, "30")
        self.assertEqual(concl.status, el271.STATUS_UNKNOWN)

    def test_role_disambiguates_same_member_id(self):
        result = self._dependent_query("30", date="20200301")
        self.assertTrue(result.matched)
        self.assertEqual(result.beneficiary.first_name, "JANE")
        concl = self._conclusion(result, "30")
        self.assertEqual(concl.status, el271.STATUS_COVERED)

    def test_unknown_member_id(self):
        result = self._query("30", member_id="NOPE")
        self.assertFalse(result.matched)
        concl = self._conclusion(result, "30")
        self.assertEqual(concl.status, el271.STATUS_UNKNOWN)

    def test_network_scoping(self):
        result = self._subscriber_query("51", network="in")
        self.assertEqual(self._conclusion(result, "51").status, el271.STATUS_COVERED)
        result = self._subscriber_query("51", network="out")
        self.assertEqual(self._conclusion(result, "51").status, el271.STATUS_CONFLICT)
        result = self._subscriber_query("51")
        self.assertEqual(self._conclusion(result, "51").status, el271.STATUS_CONFLICT)

    def test_unspecified_network_not_applied_when_disabled(self):
        result = self._subscriber_query("51", network="out", unspecified_network_applies=False)
        concl = self._conclusion(result, "51")
        self.assertEqual(concl.status, el271.STATUS_NOT_COVERED)
        result = self._subscriber_query("47", network="in", unspecified_network_applies=False)
        concl = self._conclusion(result, "47")
        self.assertEqual(concl.status, el271.STATUS_COVERED)
        self.assertEqual(len(concl.statements), 1)
        self.assertEqual(concl.statements[0].in_network, "Y")

    def test_date_inherited_from_loop(self):
        result = self._subscriber_query("30", date="20200601")
        self.assertEqual(self._conclusion(result, "30").status, el271.STATUS_COVERED)
        result = self._subscriber_query("30", date="20210601")
        self.assertEqual(self._conclusion(result, "30").status, el271.STATUS_UNKNOWN)

    def test_date_inheritance_disabled(self):
        result = self._subscriber_query("30", date="20210601", inherit_loop_dates=False)
        self.assertEqual(self._conclusion(result, "30").status, el271.STATUS_COVERED)

    def test_statement_date_takes_priority_over_loop_date(self):
        result = self._dependent_query("30", date="20200501")
        self.assertEqual(self._conclusion(result, "30").status, el271.STATUS_UNKNOWN)
        result = self._dependent_query("30", date="20200301")
        self.assertEqual(self._conclusion(result, "30").status, el271.STATUS_COVERED)


if __name__ == "__main__":
    unittest.main()
