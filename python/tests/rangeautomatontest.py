"""Unit tests for range_automaton.py"""

import glob
import os
import random
import re
import unittest

from phonenumbers.range_automaton import DigitRangeDFA, RegexSyntaxError
from phonenumbers.util import u

_PATTERN_RE = re.compile(r"national_number_pattern='((?:[^'\\]|\\.)*)'")


def _reference_match(pattern, digits):
    return re.match(u("^(?:%s)$") % pattern, digits) is not None


def _reference_split(pattern, length, lower, upper):
    """Brute-force reference implementation over every number in an interval."""
    accepted, rejected = [], []
    for value in range(lower, upper + 1):
        digits = str(value).zfill(length)
        (accepted if _reference_match(pattern, digits) else rejected).append(value)
    return _merge_numbers(accepted), _merge_numbers(rejected)


def _merge_numbers(numbers):
    merged = []
    for value in numbers:
        if merged and value == merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], value)
        else:
            merged.append((value, value))
    return merged


class DigitRangeDFATest(unittest.TestCase):

    def test_accepts_matches_python_regex(self):
        patterns = [r"15\d{8}", r"[2579]\d{5,7}", r"3[0-5]\d",
                    r"1(?:6[023]|7\d)\d{7,8}", r"[0-4][0-9]\d?",
                    r"800\d{7,9}", r"(?:12|3)\d{2,4}", r"1?[35]\d{2}",
                    r"\d{2,4}", r"9?9?\d{2}"]
        for pattern in patterns:
            dfa = DigitRangeDFA(pattern)
            for digits in (u(""), u("0"), u("3"), u("30"), u("300"),
                           u("130"), u("8001234567"), u("1512345678")):
                self.assertEqual(dfa.accepts(digits),
                                 _reference_match(pattern, digits),
                                 (pattern, digits))

    def test_classify_interval_brute_force(self):
        random.seed(42)
        patterns = [r"15\d{8}", r"[2579]\d{5,7}",
                    r"1(?:6[023]|7\d)\d{7,8}", r"1?[35]\d{2}",
                    r"\d{2,4}", r"9?9?\d{2}", r"[0-4][0-9]\d?"]
        for pattern in patterns:
            dfa = DigitRangeDFA(pattern)
            for _ in range(25):
                length = random.randint(1, 7)
                lower = random.randint(0, 10 ** length - 1)
                upper = random.randint(lower, 10 ** length - 1)
                accepted, rejected = dfa.classify_interval(length, lower, upper)
                expected_accepted, expected_rejected = _reference_split(
                    pattern, length, lower, upper)
                self.assertEqual(accepted, expected_accepted, pattern)
                self.assertEqual(rejected, expected_rejected, pattern)
                # The two partitions are complete and disjoint.
                pieces = sorted(accepted + rejected)
                self.assertEqual(pieces[0][0], lower)
                self.assertEqual(pieces[-1][1], upper)
                for (left_start, left_end), (right_start, _) in zip(pieces, pieces[1:]):
                    self.assertEqual(right_start, left_end + 1)

    def test_real_metadata_patterns(self):
        # Every region pattern must compile and agree with the regex engine on
        # random fixed-length probes; this guards the restricted-dialect parser.
        data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                "phonenumbers", "data")
        pattern_count = 0
        random.seed(7)
        for path in sorted(glob.glob(os.path.join(data_dir, "region_*.py"))):
            text = open(path, encoding="utf-8").read()
            for pattern in _PATTERN_RE.findall(text):
                pattern_count += 1
                dfa = DigitRangeDFA(pattern)
                for _ in range(5):
                    length = random.randint(1, 11)
                    lower = random.randint(0, 10 ** length - 1)
                    upper = min(10 ** length - 1, lower + random.randint(0, 200))
                    accepted, rejected = dfa.classify_interval(length, lower, upper)
                    accepted_set = set()
                    for start, end in accepted:
                        accepted_set.update(range(start, end + 1))
                    for value in random.sample(range(lower, upper + 1),
                                               min(20, upper - lower + 1)):
                        digits = str(value).zfill(length)
                        self.assertEqual(value in accepted_set,
                                         _reference_match(pattern, digits),
                                         (pattern, digits))
        self.assertGreater(pattern_count, 1000)

    def test_unsupported_syntax_is_rejected(self):
        for pattern in (u("(captured)"), u("[^0-9]"), u("a|(?=b)"),
                        u("a\\Db"), u("^abc$"), u("(a)\\1")):
            with self.assertRaises(RegexSyntaxError):
                DigitRangeDFA(pattern)

    def test_literal_hyphen_in_character_class(self):
        dfa = DigitRangeDFA(r"[3-5-]")
        for digits, expected in ((u("3"), True), (u("5"), True),
                                 (u("-"), True), (u("4"), True),
                                 (u("6"), False)):
            self.assertEqual(dfa.accepts(digits), expected)

    def test_interval_bounds_are_validated(self):
        dfa = DigitRangeDFA(r"\d{2}")
        with self.assertRaises(ValueError):
            dfa.classify_interval(2, 50, 12)
        with self.assertRaises(ValueError):
            dfa.classify_interval(2, 0, 100)


if __name__ == "__main__":
    unittest.main()
