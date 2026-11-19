"""Unit tests for numberrange.py"""

import unittest

from phonenumbers.numberrange import (NumberRange, NumberRangeError,
                                      intersect_ranges, merge_ranges,
                                      parse_range, range_from_number,
                                      ranges_from_numbers, subtract_ranges,
                                      total_count)
from phonenumbers.util import u
from phonenumbers import parse


class NumberRangeTest(unittest.TestCase):

    def test_equivalent_notations_normalize_to_one_range(self):
        plus = parse_range(u("+493012345678"), u("+493012345699"), u("DE"))
        idd = parse_range(u("00493012345678"), u("00493012345699"), u("DE"))
        national = parse_range(u("03012345678"), u("03012345699"), u("DE"))
        self.assertEqual(plus, idd)
        self.assertEqual(plus, national)
        self.assertEqual(plus.country_code, 49)
        self.assertEqual(plus.nsn_length, 10)
        self.assertEqual(plus.nsn_start(), u("3012345678"))
        self.assertEqual(plus.e164_start(), u("+493012345678"))
        self.assertEqual(plus.number_count, 22)

    def test_leading_zero_is_preserved(self):
        rng = parse_range(u("+390123456780"), u("+390123456789"), u("IT"))
        self.assertEqual(rng.nsn_length, 10)
        self.assertTrue(rng.nsn_start().startswith(u("0")))
        self.assertEqual(rng.nsn_start(), u("0123456780"))
        # The integer value drops the zero, but rendering restores it.
        self.assertEqual(rng.start, 123456780)
        self.assertEqual(rng.number_count, 10)

    def test_invalid_ranges(self):
        with self.assertRaises(NumberRangeError):
            parse_range(u("+493012345699"), u("+493012345678"), u("DE"))
        with self.assertRaises(NumberRangeError):
            parse_range(u("+493012345678"), u("+443012345699"), u("DE"))
        with self.assertRaises(NumberRangeError):
            # Explicit +49 calling code does not belong to the GB region.
            parse_range(u("+493012345678"), u("+493012345699"), u("GB"))
        with self.assertRaises(NumberRangeError):
            parse_range(u("not a number"), u("+493012345699"), u("DE"))

    def test_merge_overlapping_and_touching(self):
        first = NumberRange(49, 10, 100, 199)
        second = NumberRange(49, 10, 150, 250)
        touching = NumberRange(49, 10, 251, 300)
        merged = merge_ranges([second, first, touching])
        self.assertEqual(merged, [NumberRange(49, 10, 100, 300)])
        self.assertEqual(total_count(merged), 201)

    def test_different_widths_are_never_merged(self):
        ten_digits = NumberRange(49, 10, 100, 200)
        eleven_digits = NumberRange(49, 11, 100, 200)
        self.assertEqual(merge_ranges([ten_digits, eleven_digits]),
                         [ten_digits, eleven_digits])

    def test_subtract_and_intersect(self):
        base = [NumberRange(49, 10, 100, 299)]
        hole = [NumberRange(49, 10, 150, 199)]
        self.assertEqual(subtract_ranges(base, hole),
                         [NumberRange(49, 10, 100, 149),
                          NumberRange(49, 10, 200, 299)])
        self.assertEqual(intersect_ranges(base, [NumberRange(49, 10, 250, 400)]),
                         [NumberRange(49, 10, 250, 299)])
        # A removal in a different (code, width) key has no effect.
        other_width = [NumberRange(49, 11, 150, 199)]
        self.assertEqual(subtract_ranges(base, other_width), base)

    def test_dict_round_trip_keeps_width(self):
        rng = parse_range(u("+390123456780"), u("+390123456789"), u("IT"))
        data = rng.to_dict()
        self.assertEqual(data["nsn_start"], u("0123456780"))
        self.assertEqual(NumberRange.from_dict(data), rng)
        with self.assertRaises(NumberRangeError):
            bad = dict(data)
            bad["nsn_length"] = 9
            NumberRange.from_dict(bad)

    def test_ranges_from_numbers_compress(self):
        numbers = [parse(u("+493012345678"), None),
                   parse(u("+493012345679"), None),
                   parse(u("+493012345680"), None)]
        ranges = ranges_from_numbers(numbers)
        self.assertEqual(ranges, [range_from_number(numbers[0]).with_bounds(
            3012345678, 3012345680)])
