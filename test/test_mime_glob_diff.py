import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mime_glob_diff as mg


def rules(*specs, aliases=None):
    """specs: (weight, mime, pattern, flags, kind)."""
    aliases = aliases or {}
    out = []
    for weight, mime, pattern, flags, kind in specs:
        out.append(
            mg.Rule(
                weight,
                mg.canonical_mime(mg.normalise_mime(mime), aliases),
                pattern,
                "cs" in flags,
                kind,
                "test",
            )
        )
    return out


class WitnessTest(unittest.TestCase):
    def run_case(self, old, new, charset="abAB.-_09", min_length=1, max_length=None):
        return mg.analyse(old, new, mg.parse_charset(charset), min_length, max_length, 200000)

    def test_tie_is_preserved(self):
        old = rules((50, "text/x-one", "*", "", "glob"))
        new = rules(
            (50, "text/x-one", "*", "", "glob"),
            (50, "text/x-two", "*", "", "glob"),
        )
        result = self.run_case(old, new, charset="ab")
        self.assertEqual(result.witnesses, {(frozenset(["text/x-one"]), frozenset(["text/x-one", "text/x-two"])): (1, "a")})

    def test_equal_weight_old_tie_to_one(self):
        old = rules(
            (50, "text/x-one", "*.q", "", "extension"),
            (50, "text/x-two", "*.q", "", "extension"),
        )
        new = rules((50, "text/x-one", "*.q", "", "extension"))
        result = self.run_case(old, new, charset="q.")
        self.assertEqual(
            result.witnesses,
            {(frozenset(["text/x-one", "text/x-two"]), frozenset(["text/x-one"])): (2, ".q")},
        )

    def test_higher_weight_wins_over_literal_and_glob(self):
        old = rules(
            (50, "text/x-file", "README", "", "literal"),
            (10, "text/x-readme", "readme*", "", "glob"),
        )
        new = rules((10, "text/x-readme", "readme*", "", "glob"))
        result = self.run_case(old, new, charset="REAHDM.-")
        self.assertEqual(result.witnesses, {(frozenset(["text/x-file"]), frozenset(["text/x-readme"])): (6, "README")})

    def test_rule_text_change_without_answer_is_not_reported(self):
        old = rules((50, "text/x-python", "*.py", "", "extension"))
        new = rules((60, "text/x-python", "*.py", "", "extension"))
        result = self.run_case(old, new)
        self.assertEqual(result.witnesses, {})
        self.assertTrue(result.covered)

    def test_alias_canonicalises_target(self):
        old = rules((50, "text/x-alias", "*.q", "", "extension"))
        new = rules((50, "text/x-canonical", "*.q", "", "extension"))
        aliases = {"text/x-alias": "text/x-canonical"}
        result = mg.analyse(
            rules((50, "text/x-alias", "*.q", "", "extension"), aliases=aliases),
            new,
            mg.parse_charset("ab.-"),
            1,
            None,
            200000,
        )
        self.assertEqual(result.witnesses, {})

    def test_extension_is_case_insensitive_unless_cs(self):
        old = rules((50, "text/x-python", "*.py", "", "extension"))
        new = rules((50, "text/x-python", "*.PY", "", "extension"))
        result = self.run_case(old, new)
        self.assertEqual(result.witnesses, {})
        cased_new = rules((50, "text/x-python", "*.PY", "cs", "extension"))
        result = self.run_case(old, cased_new, charset="pPyY.-")
        self.assertEqual(
            result.witnesses,
            {(frozenset(["text/x-python"]), frozenset()): (3, ".py")},
        )

    def test_character_class_and_question_mark(self):
        old = rules((50, "text/x-one", "*.a[0-9]", "", "glob"))
        new = rules((50, "text/x-two", "*.a?", "", "glob"))
        result = self.run_case(old, new, charset="a09A.-")
        self.assertIn(
            (frozenset(["text/x-one"]), frozenset(["text/x-two"])),
            result.witnesses,
        )
        self.assertEqual(result.witnesses[(frozenset(["text/x-one"]), frozenset(["text/x-two"]))], (3, ".a0"))

    def test_unknown_printed_range_is_uncovered(self):
        old = rules((50, "text/x-one", "*.[[:alpha:]]", "", "glob"))
        new = rules((250, "text/x-two", "*.[[:alpha:]]", "", "glob"))
        result = self.run_case(old, new, max_length=2)
        self.assertFalse(result.covered)

    def test_max_length_limit_is_marked(self):
        old = rules((50, "text/x-one", "abcdef", "", "literal"))
        new = rules((50, "text/x-two", "abcdef", "", "literal"))
        result = self.run_case(old, new, charset="abcdef", max_length=3)
        self.assertFalse(result.covered)
        self.assertEqual(result.witnesses, {})

    def test_bound_does_not_truncate_after_saturation(self):
        old = rules((50, "text/x-one", "*.q", "", "extension"))
        new = rules((50, "text/x-two", "*.q", "", "extension"))
        result = self.run_case(old, new, charset="q.", max_length=10)
        self.assertTrue(result.covered)
        self.assertFalse(result.truncated)

    def test_reserved_names_are_skipped(self):
        old = rules((50, "text/x-one", "*", "", "glob"))
        new = rules((50, "text/x-two", "*", "", "glob"))
        result = mg.analyse(old, new, [".", "a"], 1, None, 200000, [".", ".."])
        self.assertEqual(
            result.witnesses,
            {(frozenset(["text/x-one"]), frozenset(["text/x-two"])): (1, "a")},
        )


if __name__ == "__main__":
    unittest.main()
