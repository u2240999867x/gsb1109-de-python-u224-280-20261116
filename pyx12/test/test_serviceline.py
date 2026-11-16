import unittest
from datetime import date
from decimal import Decimal

import pyx12.serviceline as sl
from pyx12.serviceline import (
    ERR_CROSS_DIMENSION,
    ERR_DATE_ORDER,
    ERR_INCOMPLETE_DATE,
    ERR_INVALID_QUANTITY,
    ERR_MISSING_UNIT,
    ERR_UNKNOWN_QUALIFIER,
    ERR_UNKNOWN_TARGET_QUALIFIER,
    RULE_IDENTITY,
    RULE_PRORATE,
)


class QualifierTable(unittest.TestCase):
    def test_837_service_line_qualifiers_present(self):
        # SV103 (professional) admits MJ/UN; SV204 (institutional) admits DA/UN.
        self.assertEqual(sl.get_unit_spec("UN").dimension, "count")
        self.assertEqual(sl.get_unit_spec("MJ").dimension, "time")
        self.assertEqual(sl.get_unit_spec("DA").dimension, "day")

    def test_unknown_qualifier_lookup(self):
        self.assertIsNone(sl.get_unit_spec("ZZ"))

    def test_count_and_day_have_no_convertible_basis(self):
        self.assertIsNone(sl.UNIT_SPECS["UN"].seconds)
        self.assertIsNone(sl.UNIT_SPECS["DA"].seconds)
        self.assertEqual(sl.UNIT_SPECS["MJ"].seconds, 60)
        self.assertEqual(sl.UNIT_SPECS["HR"].seconds, 3600)


class NormalizeSingleDay(unittest.TestCase):
    def test_unit_count_is_identity(self):
        result = sl.normalize_service_line(
            "L1", qualifier="un", quantity="3", service_date="20260115"
        )
        self.assertEqual(result.errors, ())
        self.assertEqual(result.qualifier, "UN")
        self.assertEqual(result.dimension, "count")
        self.assertEqual(result.date_kind, "single")
        self.assertEqual(result.calendar_days, 1)
        self.assertEqual(result.normalized_quantity, Decimal("3"))
        self.assertEqual(result.normalized_unit, "UN")
        self.assertEqual(result.conversion.rule, RULE_IDENTITY)

    def test_minute_line_keeps_time_dimension(self):
        result = sl.normalize_service_line(
            "L2", qualifier="MJ", quantity=45, service_date=date(2026, 1, 15)
        )
        self.assertEqual(result.dimension, "time")
        self.assertEqual(result.source_quantity, Decimal("45"))
        self.assertEqual(result.normalized_quantity, Decimal("45"))

    def test_day_line_keeps_day_dimension(self):
        result = sl.normalize_service_line(
            "L3", qualifier="DA", quantity="2", service_date="20260115"
        )
        self.assertEqual(result.dimension, "day")
        self.assertEqual(result.daily_lines[0].rule, RULE_IDENTITY)

    def test_single_day_daily_line_matches_whole_quantity(self):
        result = sl.normalize_service_line(
            "L4", qualifier="UN", quantity="7", service_date="20260115"
        )
        self.assertEqual(len(result.daily_lines), 1)
        line = result.daily_lines[0]
        self.assertEqual(line.service_date, date(2026, 1, 15))
        self.assertEqual(line.day_index, 1)
        self.assertEqual(line.quantity, Decimal("7"))
        self.assertEqual(line.qualifier, "UN")


class NormalizeDateRange(unittest.TestCase):
    def test_inclusive_calendar_days(self):
        result = sl.normalize_service_line(
            "L1", qualifier="DA", quantity="1", service_date="20260101", end_date="20260110"
        )
        self.assertEqual(result.date_kind, "range")
        self.assertEqual(result.calendar_days, 10)
        self.assertEqual(result.end_date, date(2026, 1, 10))

    def test_proration_even_split(self):
        result = sl.normalize_service_line(
            "L2", qualifier="MJ", quantity="9", service_date="20260101", end_date="20260103"
        )
        quantities = [line.quantity for line in result.daily_lines]
        self.assertEqual(quantities, [Decimal("3"), Decimal("3"), Decimal("3")])
        self.assertEqual(sum(quantities, Decimal(0)), Decimal("9"))
        self.assertTrue(all(line.rule == RULE_PRORATE for line in result.daily_lines))

    def test_proration_remainder_goes_to_first_day(self):
        result = sl.normalize_service_line(
            "L3", qualifier="MJ", quantity="10", service_date="20260101", end_date="20260103"
        )
        quantities = [line.quantity for line in result.daily_lines]
        self.assertEqual(quantities, [Decimal("4"), Decimal("3"), Decimal("3")])
        self.assertEqual(sum(quantities, Decimal(0)), Decimal("10"))

    def test_proration_dates_are_consecutive(self):
        result = sl.normalize_service_line(
            "L4", qualifier="UN", quantity="6", service_date="20260129", end_date="20260201"
        )
        self.assertEqual(
            [line.service_date for line in result.daily_lines],
            [date(2026, 1, 29), date(2026, 1, 30), date(2026, 1, 31), date(2026, 2, 1)],
        )
        self.assertEqual([line.day_index for line in result.daily_lines], [1, 2, 3, 4])

    def test_prorate_disabled_keeps_whole_quantity(self):
        result = sl.normalize_service_line(
            "L5",
            qualifier="DA",
            quantity="4",
            service_date="20260101",
            end_date="20260104",
            prorate=False,
        )
        self.assertEqual(result.date_kind, "range")
        self.assertEqual(result.calendar_days, 4)
        self.assertEqual(result.daily_lines, ())
        self.assertEqual(result.normalized_quantity, Decimal("4"))

    def test_no_dates_means_no_daily_lines(self):
        result = sl.normalize_service_line("L6", qualifier="UN", quantity="2")
        self.assertIsNone(result.date_kind)
        self.assertIsNone(result.service_date)
        self.assertIsNone(result.calendar_days)
        self.assertEqual(result.daily_lines, ())
        self.assertEqual(result.errors, ())


class ConversionBasisTests(unittest.TestCase):
    def test_minutes_to_hours(self):
        normalized, basis = sl.convert_quantity("MJ", "HR", Decimal("90"))
        self.assertEqual(normalized, Decimal("1.5"))
        self.assertEqual((basis.numerator, basis.denominator), (60, 3600))
        self.assertEqual(basis.factor, Decimal(1) / Decimal(60))
        self.assertEqual(basis.from_code, "MJ")
        self.assertEqual(basis.to_code, "HR")

    def test_hours_to_minutes(self):
        normalized, basis = sl.convert_quantity("HR", "MJ", Decimal("2"))
        self.assertEqual(normalized, Decimal("120"))
        self.assertEqual((basis.numerator, basis.denominator), (3600, 60))

    def test_same_qualifier_is_identity(self):
        normalized, basis = sl.convert_quantity("UN", "UN", Decimal("7"))
        self.assertEqual(normalized, Decimal("7"))
        self.assertEqual((basis.numerator, basis.denominator), (1, 1))

    def test_unknown_qualifier_raises(self):
        with self.assertRaises(ValueError):
            sl.convert_quantity("ZZ", "MJ", Decimal("1"))
        with self.assertRaises(ValueError):
            sl.convert_quantity("MJ", "ZZ", Decimal("1"))

    def test_cross_dimension_raises(self):
        for source, target in [("UN", "MJ"), ("MJ", "DA"), ("DA", "HR"), ("HR", "UN")]:
            with self.subTest(source=source, target=target):
                with self.assertRaises(ValueError):
                    sl.convert_quantity(source, target, Decimal("1"))

    def test_normalize_with_same_dimension_target(self):
        result = sl.normalize_service_line(
            "L1",
            qualifier="MJ",
            quantity="90",
            service_date="20260101",
            target_qualifier="HR",
        )
        self.assertEqual(result.errors, ())
        self.assertEqual(result.normalized_quantity, Decimal("1.5"))
        self.assertEqual(result.normalized_unit, "HR")
        self.assertEqual(result.daily_lines[0].qualifier, "HR")
        self.assertEqual(result.daily_lines[0].unit, "Hour")

    def test_normalize_rejects_cross_dimension_target(self):
        result = sl.normalize_service_line(
            "L2",
            qualifier="UN",
            quantity="1",
            service_date="20260101",
            target_qualifier="MJ",
        )
        self.assertEqual(result.errors, (ERR_CROSS_DIMENSION,))
        self.assertIsNone(result.normalized_quantity)
        self.assertEqual(result.daily_lines, ())

    def test_normalize_rejects_unknown_target(self):
        result = sl.normalize_service_line(
            "L3",
            qualifier="UN",
            quantity="1",
            service_date="20260101",
            target_qualifier="ZZ",
        )
        self.assertEqual(result.errors, (ERR_UNKNOWN_TARGET_QUALIFIER,))


class ErrorHandling(unittest.TestCase):
    def test_missing_unit_is_not_inferred(self):
        # No charge/rate is accepted, so a missing qualifier can never be
        # reverse-engineered: the line is simply rejected.
        result = sl.normalize_service_line(
            "L1", qualifier=None, quantity="5", service_date="20260101"
        )
        self.assertEqual(result.errors, (ERR_MISSING_UNIT,))
        self.assertIsNone(result.qualifier)
        self.assertIsNone(result.dimension)
        self.assertIsNone(result.normalized_quantity)
        self.assertEqual(result.daily_lines, ())

    def test_blank_qualifier_counts_as_missing(self):
        result = sl.normalize_service_line(
            "L2", qualifier="  ", quantity="5", service_date="20260101"
        )
        self.assertEqual(result.errors, (ERR_MISSING_UNIT,))

    def test_unknown_qualifier(self):
        result = sl.normalize_service_line(
            "L3", qualifier="ZZ", quantity="1", service_date="20260101"
        )
        self.assertEqual(result.errors, (ERR_UNKNOWN_QUALIFIER,))
        self.assertIsNone(result.dimension)

    def test_invalid_quantity_values(self):
        for bad in ("abc", "", "-1", "1.2.3", float("nan"), float("inf")):
            with self.subTest(bad=bad):
                result = sl.normalize_service_line(
                    "L4", qualifier="UN", quantity=bad, service_date="20260101"
                )
                self.assertIn(ERR_INVALID_QUANTITY, result.errors)
                self.assertIsNone(result.source_quantity)

    def test_boolean_quantity_rejected(self):
        result = sl.normalize_service_line(
            "L5", qualifier="UN", quantity=True, service_date="20260101"
        )
        self.assertEqual(result.errors, (ERR_INVALID_QUANTITY,))

    def test_zero_quantity_allowed(self):
        result = sl.normalize_service_line(
            "L6", qualifier="UN", quantity="0", service_date="20260101"
        )
        self.assertEqual(result.errors, ())
        self.assertEqual(result.source_quantity, Decimal("0"))

    def test_incomplete_start_date(self):
        result = sl.normalize_service_line(
            "L7", qualifier="UN", quantity="1", service_date="202601"
        )
        self.assertEqual(result.errors, (ERR_INCOMPLETE_DATE,))
        self.assertIsNone(result.service_date)
        self.assertEqual(result.daily_lines, ())

    def test_end_date_without_start_is_incomplete(self):
        result = sl.normalize_service_line(
            "L8", qualifier="UN", quantity="1", end_date="20260110"
        )
        self.assertEqual(result.errors, (ERR_INCOMPLETE_DATE,))

    def test_impossible_calendar_date(self):
        result = sl.normalize_service_line(
            "L9", qualifier="UN", quantity="1", service_date="20260230"
        )
        self.assertEqual(result.errors, (ERR_INCOMPLETE_DATE,))

    def test_end_before_start(self):
        result = sl.normalize_service_line(
            "L10", qualifier="DA", quantity="2", service_date="20260110", end_date="20260109"
        )
        self.assertEqual(result.errors, (ERR_DATE_ORDER,))
        self.assertIsNone(result.calendar_days)

    def test_errors_accumulate_without_guessing(self):
        result = sl.normalize_service_line(
            "L11", qualifier="ZZ", quantity="n/a", service_date="20260101"
        )
        self.assertEqual(result.errors, (ERR_UNKNOWN_QUALIFIER, ERR_INVALID_QUANTITY))
        self.assertIsNone(result.normalized_quantity)
        self.assertEqual(result.daily_lines, ())


class HelperFunctions(unittest.TestCase):
    def test_calendar_days_inclusive(self):
        self.assertEqual(
            sl.calendar_days(date(2026, 1, 1), date(2026, 1, 1)), 1
        )
        self.assertEqual(
            sl.calendar_days(date(2026, 1, 1), date(2026, 1, 31)), 31
        )

    def test_calendar_days_rejects_reversed_range(self):
        with self.assertRaises(ValueError):
            sl.calendar_days(date(2026, 1, 2), date(2026, 1, 1))

    def test_prorate_sum_invariant(self):
        quantity = Decimal("100")
        pieces = sl.prorate_quantity(quantity, 7)
        self.assertEqual(len(pieces), 7)
        self.assertEqual(sum(pieces, Decimal(0)), quantity)
        self.assertEqual(pieces[0], max(pieces))

    def test_prorate_rejects_non_positive_days(self):
        with self.assertRaises(ValueError):
            sl.prorate_quantity(Decimal("1"), 0)

    def test_batch_normalize(self):
        results = sl.normalize_service_lines(
            [
                {"source": "a", "qualifier": "UN", "quantity": 1},
                {
                    "source": "b",
                    "qualifier": "MJ",
                    "quantity": 60,
                    "service_date": "20260101",
                    "target_qualifier": "HR",
                },
                {"source": "c", "qualifier": None, "quantity": 2},
            ]
        )
        self.assertEqual([item.source for item in results], ["a", "b", "c"])
        self.assertEqual(results[0].dimension, "count")
        self.assertEqual(results[1].normalized_quantity, Decimal("1"))
        self.assertEqual(results[2].errors, (ERR_MISSING_UNIT,))


if __name__ == "__main__":
    unittest.main()
