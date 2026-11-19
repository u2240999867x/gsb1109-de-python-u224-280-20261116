"""Unit tests for numberrules.py"""

import random
import unittest

from phonenumbers import PhoneNumber, PhoneNumberType
from phonenumbers.numberrange import NumberRange, parse_range
from phonenumbers.numberrules import (RANGE_INVALID, RANGE_PENDING, RANGE_VALID,
                                      RangeRuleClassifier)
from phonenumbers.phonemetadata import PhoneMetadata, REGION_CODE_FOR_NON_GEO_ENTITY
from phonenumbers.phonenumberutil import (_is_number_matching_desc,
                                          _number_type_helper)
from phonenumbers.util import u


class RangeRuleClassifierTest(unittest.TestCase):

    def _reference_status(self, metadata, country_code, length, value):
        nsn = str(value).zfill(length)
        if not _is_number_matching_desc(nsn, metadata.general_desc):
            return RANGE_INVALID, None
        number_type = _number_type_helper(nsn, metadata)
        if number_type == PhoneNumberType.UNKNOWN:
            return RANGE_PENDING, None
        return RANGE_VALID, number_type

    def _assert_brute_force(self, region, country_code):
        metadata = PhoneMetadata.metadata_for_region(region)
        classifier = RangeRuleClassifier(region)
        random.seed(1234)
        lengths = sorted(set(metadata.general_desc.possible_length))
        for length in random.sample(lengths, min(5, len(lengths))):
            for _ in range(8):
                lower = random.randint(0, 10 ** length - 1)
                upper = min(10 ** length - 1, lower + random.randint(0, 150))
                pieces = classifier.classify(
                    NumberRange(country_code, length, lower, upper))
                self.assertEqual(sum(p.number_count for p in pieces),
                                 upper - lower + 1)
                index = 0
                for value in range(lower, upper + 1):
                    while not (pieces[index].number_range.start <= value <=
                               pieces[index].number_range.end):
                        index += 1
                    status, number_type = self._reference_status(
                        metadata, country_code, length, value)
                    self.assertEqual(pieces[index].status, status,
                                     (region, length, value))
                    if status == RANGE_VALID:
                        self.assertEqual(pieces[index].number_type, number_type)
                    else:
                        self.assertIsNone(pieces[index].number_type)

    def test_matches_pointwise_library_rules(self):
        for region in (u("DE"), u("US"), u("GB"), u("IT"), u("FR"),
                       u("JP"), u("BR"), u("AU")):
            country_code = PhoneMetadata.metadata_for_region(region).country_code
            self._assert_brute_force(region, country_code)

    def test_endpoints_valid_does_not_certify_whole_range(self):
        classifier = RangeRuleClassifier(u("DE"))
        # First and last numbers are valid mobile numbers, but the interior
        # 15300000000..15309999999 only matches the general description.
        rng = NumberRange(49, 11, 15299999990, 15310000005)
        pieces = classifier.classify(rng)
        statuses = [piece.status for piece in pieces]
        self.assertIn(RANGE_PENDING, statuses)
        self.assertIn(RANGE_VALID, statuses)
        pending = [piece for piece in pieces if piece.status == RANGE_PENDING]
        self.assertTrue(any(piece.number_range.start <= 15300000000 <=
                            piece.number_range.end for piece in pending))
        self.assertEqual(sum(piece.number_count for piece in pieces),
                         rng.number_count)

    def test_zero_leading_nsn_is_invalid_not_renumbered(self):
        classifier = RangeRuleClassifier(u("DE"))
        rng = NumberRange(49, 11, 1234567890, 1234567899)
        self.assertTrue(rng.nsn_start().startswith(u("0")))
        pieces = classifier.classify(rng)
        self.assertEqual({piece.status for piece in pieces}, {RANGE_INVALID})
        metadata = PhoneMetadata.metadata_for_region(u("DE"))
        self.assertFalse(_is_number_matching_desc(rng.nsn_start(),
                                                  metadata.general_desc))

    def test_leading_zero_preserved_through_split(self):
        classifier = RangeRuleClassifier(u("IT"))
        rng = parse_range(u("+390123456780"), u("+390123456789"), u("IT"))
        pieces = classifier.classify(rng)
        self.assertEqual(sum(piece.number_count for piece in pieces), 10)
        for piece in pieces:
            self.assertTrue(piece.number_range.nsn_start().startswith(u("0")))
            self.assertEqual(piece.number_range.nsn_length, 10)

    def test_non_geographical_calling_code(self):
        classifier = RangeRuleClassifier(REGION_CODE_FOR_NON_GEO_ENTITY)
        metadata = PhoneMetadata.metadata_for_nongeo_region(800)
        length = sorted(metadata.general_desc.possible_length)[0]
        lower = 10 ** (length - 1)
        pieces = classifier.classify(NumberRange(800, length, lower, lower + 99))
        self.assertEqual(sum(piece.number_count for piece in pieces), 100)
        for piece in pieces:
            status, _ = self._reference_status(
                metadata, 800, length, piece.number_range.start)
            self.assertEqual(piece.status, status)

    def test_wrong_calling_code_and_region_are_rejected(self):
        classifier = RangeRuleClassifier(u("DE"))
        with self.assertRaises(Exception):
            classifier.classify(NumberRange(44, 10, 3000000000, 3000000099))
        with self.assertRaises(Exception):
            RangeRuleClassifier(u("ZZ"))


if __name__ == "__main__":
    unittest.main()
