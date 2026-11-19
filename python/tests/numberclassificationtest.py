"""Unit tests for numberclassification.py

These tests use the real, bundled metadata for both ordinary numbers and
short numbers, mirroring the approach in examplenumberstest.py.
"""
# Copyright (C) 2026 The Libphonenumber Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import unittest

from phonenumbers import (NumberCategory, NumberReach, NumberParseException,
                          classify_number, classify_numbers,
                          can_be_internationally_dialled, example_number,
                          is_valid_number_for_region)
from phonenumbers.util import u


def _conclusions_for(result, region):
    matches = [conclusion for conclusion in result.conclusions
               if conclusion.region == region]
    assert len(matches) == 1, "expected one conclusion for %s, got %r" % (region, result.conclusions)
    return matches[0]


class NumberClassificationTest(unittest.TestCase):
    """Unit tests for the offline classification exit point."""

    def testOrdinaryNumberIsInternationallyDiallable(self):
        result = classify_number("650-253-0000", region="US")
        self.assertEqual(("US",), tuple(c.region for c in result.conclusions))
        conclusion = result.conclusions[0]
        self.assertEqual(NumberCategory.ORDINARY_NUMBER, conclusion.category)
        self.assertEqual(NumberReach.INTERNATIONAL, conclusion.reach)
        self.assertIn(u("valid_number_pattern"), conclusion.evidence)
        self.assertIn(u("internationally_diallable"), conclusion.evidence)
        self.assertEqual((NumberCategory.ORDINARY_NUMBER,), result.categories)
        self.assertEqual((NumberReach.INTERNATIONAL,), result.reaches)

    def testValidNumberThatIsNotInternationallyDiallable(self):
        # North Korea's example fixed-line number matches its
        # no-international-dialling description, so it is region-scoped only.
        sample = example_number("KP")
        if sample is None or not is_valid_number_for_region(sample, "KP"):
            self.skipTest("no stable KP example number in this metadata")
        if can_be_internationally_dialled(sample):
            self.skipTest("KP example is internationally diallable in this metadata")
        result = classify_number(u("21234567"), region="KP")
        conclusion = result.conclusions[0]
        self.assertEqual(NumberCategory.ORDINARY_NUMBER, conclusion.category)
        self.assertEqual(NumberReach.REGIONAL, conclusion.reach)
        self.assertIn(u("no_international_dialling"), conclusion.evidence)

    def testEmergencyNumberIsEmergencyOnly(self):
        result = classify_number("911", region="US")
        conclusion = result.conclusions[0]
        self.assertEqual(NumberCategory.REGIONAL_SHORT_CODE, conclusion.category)
        # Emergency matching must not be presented as ordinary service reach.
        self.assertEqual(NumberReach.EMERGENCY_ONLY, conclusion.reach)
        self.assertIn(u("emergency"), conclusion.evidence)
        self.assertIn(u("valid_short_number"), conclusion.evidence)

    def testEmergencyDoesNotImplyOrdinaryService(self):
        # 211 is an emergency number in Barbados but an ordinary short service
        # elsewhere; the distinction must follow the region evidence.
        barbados = classify_number("211", region="BB")
        self.assertEqual(NumberReach.EMERGENCY_ONLY, barbados.conclusions[0].reach)
        us = classify_number("211", region="US")
        self.assertNotEqual(NumberReach.EMERGENCY_ONLY, us.conclusions[0].reach)

    def testPlusPrefixedEmergencyIsNotTreatedAsEmergency(self):
        # The library does not believe "+1911" reaches emergency services,
        # even though bare "911" does.
        result = classify_number("+1911")
        for conclusion in result.conclusions:
            self.assertNotIn(u("emergency"), conclusion.evidence)
            self.assertNotEqual(NumberReach.EMERGENCY_ONLY, conclusion.reach)

    def testEmergencyPrefixMatchIsRecordedButNotOrdinaryService(self):
        # In the US extra digits may still route to emergency services.
        result = classify_number("9119", region="US")
        conclusion = result.conclusions[0]
        self.assertIn(u("connects_to_emergency_prefix"), conclusion.evidence)
        # In Brazil emergency numbers must match exactly, so no prefix evidence.
        brazil = classify_number("9119", region="BR")
        brazil_conclusion = brazil.conclusions[0]
        self.assertNotIn(u("connects_to_emergency_prefix"), brazil_conclusion.evidence)
        self.assertNotIn(u("emergency"), brazil_conclusion.evidence)

    def testShortNumberLengthAloneIsNotValidityEvidence(self):
        # 12345 is a possible short-number length in FR but matches no valid
        # pattern, so no short-code category or reach is claimed.
        result = classify_number("12345", region="FR")
        conclusion = result.conclusions[0]
        self.assertEqual(NumberCategory.UNKNOWN, conclusion.category)
        self.assertEqual(NumberReach.UNKNOWN, conclusion.reach)
        self.assertIn(u("possible_short_number_length"), conclusion.evidence)
        self.assertNotIn(u("valid_short_number"), conclusion.evidence)

    def testValidNonEmergencyShortCodeIsRegional(self):
        result = classify_number("1010", region="FR")
        conclusion = result.conclusions[0]
        self.assertEqual(NumberCategory.REGIONAL_SHORT_CODE, conclusion.category)
        self.assertEqual(NumberReach.REGIONAL, conclusion.reach)
        self.assertIn(u("valid_short_number"), conclusion.evidence)

    def testCarrierSpecificAndSmsEvidence(self):
        carrier = classify_number("33669", region="US").conclusions[0]
        self.assertIn(u("carrier_specific"), carrier.evidence)
        sms = classify_number("21234", region="US").conclusions[0]
        self.assertIn(u("sms_service"), sms.evidence)

    def testMissingRegionKeepsBareDigitsUnknownWithoutInventingAreaCode(self):
        result = classify_number("110")
        self.assertEqual(1, len(result.conclusions))
        conclusion = result.conclusions[0]
        self.assertIsNone(conclusion.region)
        self.assertEqual(NumberCategory.UNKNOWN, conclusion.category)
        self.assertIn(u("no_region_no_international_prefix"), conclusion.evidence)
        # No parsed number, hence no fabricated country/area code.
        self.assertIsNone(conclusion.number)

    def testMissingRegionRetainsInternationalCandidate(self):
        result = classify_number("+16502530000")
        # Only regions with evidence appear; the US validates the number.
        united_states = _conclusions_for(result, "US")
        self.assertEqual(NumberCategory.ORDINARY_NUMBER, united_states.category)
        self.assertEqual(NumberReach.INTERNATIONAL, united_states.reach)
        for conclusion in result.conclusions:
            self.assertNotEqual(NumberCategory.UNKNOWN, conclusion.category)

    def testSharedCallingCodeKeepsIndependentCandidates(self):
        # GB and GG share country calling code 44; 18001 is valid in both.
        result = classify_number("+4418001")
        regions = set(conclusion.region for conclusion in result.conclusions)
        self.assertEqual(set(["GB", "GG"]), regions)
        for conclusion in result.conclusions:
            self.assertEqual(NumberCategory.REGIONAL_SHORT_CODE, conclusion.category)
            self.assertEqual(NumberReach.REGIONAL, conclusion.reach)

    def testExtensionInsideDomainIsEnterpriseOnly(self):
        result = classify_number("(650) 253-0000 x1234", region="US",
                                 organization_domain="example.com")
        extension = result.conclusions[0]
        base = result.conclusions[1]
        self.assertEqual(NumberCategory.ENTERPRISE_EXTENSION, extension.category)
        self.assertEqual(NumberReach.ENTERPRISE_ONLY, extension.reach)
        self.assertEqual(u("1234"), extension.extension)
        self.assertEqual(u("example.com"), extension.organization_domain)
        self.assertEqual(NumberCategory.ORDINARY_NUMBER, base.category)

    def testExtensionWithoutDomainStaysUnclassifiedButBaseIsKept(self):
        result = classify_number("(650) 253-0000 x1234", region="US")
        extension = result.conclusions[0]
        base = result.conclusions[1]
        self.assertEqual(NumberCategory.UNKNOWN, extension.category)
        self.assertEqual(u("1234"), extension.extension)
        self.assertIn(u("extension_without_domain"), extension.evidence)
        self.assertEqual(NumberCategory.ORDINARY_NUMBER, base.category)

    def testSameDigitsStayDistinctAcrossDomainsAndShortCodes(self):
        in_domain = classify_number("6502530000 x1234", region="US",
                                    organization_domain="a.example")
        other_domain = classify_number("6502530000 x1234", region="US",
                                       organization_domain="b.example")
        bare = classify_number("1234", region="US")
        self.assertEqual(u("a.example"), in_domain.conclusions[0].organization_domain)
        self.assertEqual(u("b.example"), other_domain.conclusions[0].organization_domain)
        self.assertNotEqual(in_domain.conclusions[0], other_domain.conclusions[0])
        # The bare digits are not silently turned into an extension.
        self.assertTrue(all(conclusion.category != NumberCategory.ENTERPRISE_EXTENSION
                            for conclusion in bare.conclusions))

    def testRoamingAndContextAreRecorded(self):
        result = classify_number("911", region="US", roaming=True)
        self.assertTrue(result.roaming)
        self.assertEqual(u("US"), result.region)
        self.assertEqual(u("911"), result.raw_input)

    def testClassifyNumbersBatch(self):
        results = classify_numbers(["911", "+16502530000", "nonsense"], region="US")
        self.assertEqual(3, len(results))
        self.assertEqual([u("911"), u("+16502530000"), u("nonsense")],
                         [result.raw_input for result in results])

    def testUnknownRegionSentinelIsTreatedAsMissing(self):
        result = classify_number("110", region="ZZ")
        self.assertIsNone(result.region)
        self.assertEqual(NumberCategory.UNKNOWN, result.conclusions[0].category)

    def testNoneInputRaises(self):
        self.assertRaises(NumberParseException, classify_number, None, region="US")

    def testEqualityAndStringRendering(self):
        first = classify_number("911", region="US")
        second = classify_number("911", region="US")
        self.assertEqual(first, second)
        self.assertEqual(hash(first.conclusions[0]), hash(second.conclusions[0]))
        rendered = str(first)
        self.assertIn("NumberClassification", rendered)
        self.assertIn("NumberConclusion", rendered)

    def testDictRoundTripKeepsShortCodeAndExtensionDistinct(self):
        import json
        extension_result = classify_number("6502530000 x1234", region="US",
                                           organization_domain="example.com")
        short_result = classify_number("1234", region="US")

        reimported_extension = extension_result.from_dict(
            json.loads(json.dumps(extension_result.to_dict())))
        reimported_short = short_result.from_dict(
            json.loads(json.dumps(short_result.to_dict())))

        self.assertEqual(extension_result, reimported_extension)
        self.assertEqual(short_result, reimported_short)
        extension_conclusion = reimported_extension.conclusions[0]
        self.assertEqual(NumberCategory.ENTERPRISE_EXTENSION, extension_conclusion.category)
        self.assertEqual(NumberReach.ENTERPRISE_ONLY, extension_conclusion.reach)
        self.assertEqual(u("1234"), extension_conclusion.extension)
        self.assertTrue(all(conclusion.category != NumberCategory.ENTERPRISE_EXTENSION
                            for conclusion in reimported_short.conclusions))
        # The parsed PhoneNumber survives the round trip as well.
        ordinary_conclusion = reimported_extension.conclusions[1]
        self.assertIsNotNone(ordinary_conclusion.number)
        self.assertEqual(1, ordinary_conclusion.number.country_code)
        self.assertEqual(6502530000, ordinary_conclusion.number.national_number)


if __name__ == '__main__':
    unittest.main()
