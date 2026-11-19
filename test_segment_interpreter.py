import unittest

from segment_interpreter import (
    ContextRule,
    CostModel,
    Excluded,
    Number,
    Uninterpreted,
    interpret_segment,
)

DATE_RULE = ContextRule("date", r"\d{2}\.\d{2}\.\d{4}")
ID_RULE = ContextRule("id", r"AZ-\d{4}")


def numbers(occurrences):
    return [o for o in occurrences if isinstance(o, Number)]


class SegmentInterpreterTest(unittest.TestCase):
    def test_single_multirun_number(self):
        text = "Rufen Sie 030 123456 an."
        result = interpret_segment(text, regions=("DE",))
        self.assertTrue(result.unique)
        self.assertEqual(1, len(result.confirmed))
        occ = result.confirmed[0]
        self.assertIsInstance(occ, Number)
        self.assertEqual("030 123456", occ.raw)
        self.assertEqual(text[occ.start:occ.end], occ.raw)
        self.assertEqual("030123456", occ.normalized)
        self.assertEqual(frozenset({"DE"}), occ.regions)
        self.assertIsNone(occ.extension)
        self.assertAlmostEqual(1.0, result.total_cost)

    def test_extension_preferred_by_default_costs(self):
        text = "Tel. 030 123456 Durchwahl 89"
        result = interpret_segment(text, regions=("DE",))
        self.assertTrue(result.unique)
        (occ,) = result.confirmed
        self.assertIsInstance(occ, Number)
        self.assertIsNotNone(occ.extension)
        self.assertEqual("89", occ.extension.raw)
        self.assertEqual("89", occ.extension.digits)
        self.assertEqual(text[occ.extension.start:occ.extension.end], "89")
        self.assertEqual("030 123456 Durchwahl 89", occ.raw)
        self.assertAlmostEqual(1.5, result.total_cost)

    def test_two_independent_numbers(self):
        text = "Büro: 6502530000, Mobil: 6502531111"
        result = interpret_segment(text, regions=("US",))
        self.assertTrue(result.unique)
        found = numbers(result.confirmed)
        self.assertEqual(2, len(found))
        self.assertEqual("6502530000", found[0].raw)
        self.assertEqual("6502531111", found[1].raw)
        self.assertTrue(all(o.extension is None for o in found))
        self.assertAlmostEqual(2.0, result.total_cost)

    def test_tie_confirms_only_common_occurrences(self):
        # "030 123456" + extension "3456789" costs the same as two
        # independent numbers when the extension surcharge equals the
        # number cost, so nothing may be confirmed.
        text = "030 123456 x 3456789"
        costs = CostModel(extension=1.0)
        result = interpret_segment(text, regions=("DE", "AT"), costs=costs)
        self.assertFalse(result.unique)
        self.assertEqual((), result.confirmed)
        kinds = sorted(
            (o.start, o.end, o.kind) for o in result.candidates
        )
        self.assertEqual(3, len(kinds))
        combined = [o for o in result.candidates if o.extension is not None]
        singles = [o for o in result.candidates if o.extension is None]
        self.assertEqual(1, len(combined))
        self.assertEqual(2, len(singles))
        self.assertAlmostEqual(2.0, result.total_cost)

    def test_lower_extension_cost_breaks_tie(self):
        text = "030 123456 x 3456789"
        result = interpret_segment(text, regions=("DE", "AT"))
        self.assertTrue(result.unique)
        (occ,) = result.confirmed
        self.assertIsNotNone(occ.extension)
        self.assertEqual("3456789", occ.extension.raw)
        self.assertAlmostEqual(1.5, result.total_cost)

    def test_region_ambiguity_retained(self):
        result = interpret_segment("3456789", regions=("DE", "AT"))
        self.assertTrue(result.unique)
        (occ,) = result.confirmed
        self.assertEqual(frozenset({"DE", "AT"}), occ.regions)
        self.assertTrue(occ.region_ambiguous)
        # Restricting the allowed regions resolves the ambiguity.
        result_de = interpret_segment("3456789", regions=("DE",))
        (occ_de,) = result_de.confirmed
        self.assertEqual(frozenset({"DE"}), occ_de.regions)
        self.assertFalse(occ_de.region_ambiguous)

    def test_international_prefix_is_region_evidence(self):
        text = "Tel +49 30 123456"
        result = interpret_segment(text, regions=("DE", "US"))
        self.assertTrue(result.unique)
        (occ,) = result.confirmed
        self.assertEqual("+49 30 123456", occ.raw)
        self.assertEqual("+4930123456", occ.normalized)
        self.assertEqual(frozenset({"DE"}), occ.regions)
        self.assertEqual(text.index("+"), occ.start)

    def test_date_exclusion(self):
        text = "Am 19.11.2026 gilt: 030 123456"
        result = interpret_segment(
            text, regions=("DE",), context_rules=(DATE_RULE,)
        )
        self.assertTrue(result.unique)
        excluded = [o for o in result.confirmed if isinstance(o, Excluded)]
        self.assertEqual(1, len(excluded))
        self.assertEqual("19.11.2026", excluded[0].raw)
        self.assertEqual(("date",), excluded[0].rules)
        found = numbers(result.confirmed)
        self.assertEqual(1, len(found))
        self.assertEqual("030 123456", found[0].raw)
        self.assertAlmostEqual(1.5, result.total_cost)

    def test_no_crossing_excluded_content(self):
        # "030" and "123456" would form a valid number if the date between
        # them could be skipped; the exclusion must not be crossed.
        text = "030 19.11.2026 123456"
        result = interpret_segment(
            text, regions=("DE",), context_rules=(DATE_RULE,)
        )
        all_occ = list(result.confirmed) + list(result.candidates)
        self.assertEqual([], numbers(all_occ))
        excluded = [o for o in result.confirmed if isinstance(o, Excluded)]
        self.assertEqual(1, len(excluded))
        uninterpreted = [
            o for o in result.confirmed if isinstance(o, Uninterpreted)
        ]
        self.assertEqual({"030", "123456"}, {o.raw for o in uninterpreted})

    def test_id_rule_excludes_identifier(self):
        text = "AZ-4321 bitte angeben"
        result = interpret_segment(
            text, regions=("DE",), context_rules=(ID_RULE,)
        )
        self.assertTrue(result.unique)
        (occ,) = result.confirmed
        self.assertIsInstance(occ, Excluded)
        self.assertEqual("AZ-4321", occ.raw)
        self.assertEqual(("id",), occ.rules)

    def test_normalization_preserves_spans(self):
        text = "０３０１２３４５６"  # full-width digits
        result = interpret_segment(text, regions=("DE",))
        self.assertTrue(result.unique)
        (occ,) = result.confirmed
        self.assertIsInstance(occ, Number)
        self.assertEqual(text, occ.raw)
        self.assertEqual(0, occ.start)
        self.assertEqual(len(text), occ.end)
        self.assertEqual("030123456", occ.normalized)
        self.assertEqual(frozenset({"DE"}), occ.regions)

    def test_uninterpreted_fallback(self):
        result = interpret_segment("12 34", regions=("DE",))
        self.assertTrue(result.unique)
        self.assertEqual(2, len(result.confirmed))
        self.assertTrue(
            all(isinstance(o, Uninterpreted) for o in result.confirmed)
        )
        self.assertAlmostEqual(4.0, result.total_cost)

    def test_tie_between_number_and_uninterpreted(self):
        # With equal costs the valid reading must not win just because it
        # exists; both readings stay candidates.
        costs = CostModel(number=1.0, uninterpreted=1.0)
        result = interpret_segment("030123456", regions=("DE",), costs=costs)
        self.assertFalse(result.unique)
        self.assertEqual((), result.confirmed)
        self.assertEqual(
            {"number", "uninterpreted"},
            {o.kind for o in result.candidates},
        )

    def test_excluded_spans_are_mandatory(self):
        # Forbidding an excluded piece must make the segment infeasible,
        # so exclusions are always confirmed.
        text = "19.11.2026"
        result = interpret_segment(
            text, regions=("DE",), context_rules=(DATE_RULE,)
        )
        self.assertTrue(result.unique)
        (occ,) = result.confirmed
        self.assertIsInstance(occ, Excluded)
        self.assertAlmostEqual(0.5, result.total_cost)


if __name__ == "__main__":
    unittest.main()
