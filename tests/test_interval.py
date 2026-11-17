import unittest
from datetime import datetime
from typing import Any

from parameterized import param, parameterized

import dateparser
from dateparser.date import DateData
from dateparser.interval import DateRangeData, DateRangeParser, parse_date_range
from tests import BaseTestCase


class TestDateRangeParser(BaseTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.base = datetime(2026, 6, 15)
        self.settings = {"RELATIVE_BASE": self.base}

    def _ranges(
        self, date_string: str, **kwargs: Any
    ) -> list[DateRangeData]:
        return parse_date_range(
            date_string,
            settings={**self.settings, **kwargs.pop("settings", {})},
            **kwargs,
        )

    def _first(self, date_string: str, **kwargs: Any) -> DateRangeData:
        ranges = self._ranges(date_string, **kwargs)
        self.assertTrue(ranges, f"expected at least one range for {date_string!r}")
        return ranges[0]

    @parameterized.expand(
        [
            param(
                "1月31日至2月2日",
                languages=["zh-Hans"],
                expected=(datetime(2026, 1, 31), datetime(2026, 2, 2)),
            ),
            param(
                "1月31日到2月2日",
                languages=["zh-Hans"],
                expected=(datetime(2026, 1, 31), datetime(2026, 2, 2)),
            ),
            param(
                "2024年12月30日—1月3日",
                languages=["zh-Hans"],
                expected=(datetime(2024, 12, 30), datetime(2025, 1, 3)),
            ),
            param(
                "2024年12月30日-1月3日",
                languages=["zh-Hans"],
                expected=(datetime(2024, 12, 30), datetime(2025, 1, 3)),
            ),
            param(
                "January 31 to February 2",
                languages=None,
                expected=(datetime(2026, 1, 31), datetime(2026, 2, 2)),
            ),
            param(
                "31. Januar bis 2. Februar",
                languages=["de"],
                expected=(datetime(2026, 1, 31), datetime(2026, 2, 2)),
            ),
            param(
                "5 al 10 de enero",
                languages=["es"],
                expected=(datetime(2026, 1, 5), datetime(2026, 1, 10)),
            ),
            param(
                "5 au 10 janvier",
                languages=["fr"],
                expected=(datetime(2026, 1, 5), datetime(2026, 1, 10)),
            ),
        ]
    )
    def test_parses_range_with_shared_parts(
        self,
        date_string: str,
        languages: list[str] | None,
        expected: tuple[datetime, datetime],
    ) -> None:
        range_data = self._first(date_string, languages=languages)
        self.assertEqual(
            (range_data.start.date_obj, range_data.end.date_obj), expected
        )
        self.assertEqual(range_data.start.period, "day")
        self.assertEqual(range_data.end.period, "day")

    def test_fully_dated_range_keeps_both_years(self) -> None:
        range_data = self._first("2024-12-30 - 2025-01-03")
        self.assertEqual(
            (range_data.start.date_obj, range_data.end.date_obj),
            (datetime(2024, 12, 30), datetime(2025, 1, 3)),
        )

    def test_explicit_start_year_is_shared_with_end(self) -> None:
        ranges = self._ranges(
            "2024年12月30日—1月3日", languages=["zh-Hans"]
        )
        self.assertEqual(len(ranges), 1)
        self.assertEqual(
            (ranges[0].start.date_obj, ranges[0].end.date_obj),
            (datetime(2024, 12, 30), datetime(2025, 1, 3)),
        )

    def test_bare_day_shares_month_and_year(self) -> None:
        range_data = self._first("12月5日至10日", languages=["zh-Hans"])
        self.assertEqual(
            (range_data.start.date_obj, range_data.end.date_obj),
            (datetime(2026, 12, 5), datetime(2026, 12, 10)),
        )
        self.assertEqual(range_data.start.parts, ("month", "day"))
        self.assertEqual(range_data.end.parts, ("day",))

    def test_ordinal_bare_day_shares_month(self) -> None:
        range_data = self._first("June 5th to 10th")
        self.assertEqual(
            (range_data.start.date_obj, range_data.end.date_obj),
            (datetime(2026, 6, 5), datetime(2026, 6, 10)),
        )

    def test_month_phrase_keeps_month_precision(self) -> None:
        range_data = self._first("1月至2月", languages=["zh-Hans"])
        start = range_data.start.date_obj
        end = range_data.end.date_obj
        assert start is not None and end is not None
        self.assertEqual(range_data.start.period, "month")
        self.assertEqual(range_data.end.period, "month")
        self.assertEqual((start.year, start.month), (2026, 1))
        self.assertEqual((end.year, end.month), (2026, 2))

    def test_month_phrase_with_year_keeps_month_precision(self) -> None:
        range_data = self._first("2024年1月至3月", languages=["zh-Hans"])
        self.assertEqual(range_data.end.period, "month")
        self.assertEqual(
            (range_data.start.date_obj, range_data.end.date_obj),
            (datetime(2024, 1, 15), datetime(2024, 3, 15)),
        )

    def test_mixed_precision_does_not_invent_a_day(self) -> None:
        # The month-only end keeps its own day; the explicit start day is not
        # forced onto February, where it could be impossible.
        range_data = self._first("January 31 to February")
        self.assertEqual(range_data.start.period, "day")
        self.assertEqual(range_data.end.period, "month")
        assert range_data.start.date_obj is not None
        assert range_data.end.date_obj is not None
        self.assertEqual(range_data.start.date_obj.day, 31)
        self.assertEqual(
            (range_data.end.date_obj.year, range_data.end.date_obj.month),
            (2026, 2),
        )

    def test_year_phrase_keeps_year_precision(self) -> None:
        range_data = self._first("2024 to 2025")
        start = range_data.start.date_obj
        end = range_data.end.date_obj
        assert start is not None and end is not None
        self.assertEqual(range_data.start.period, "year")
        self.assertEqual(range_data.end.period, "year")
        self.assertEqual((start.year, end.year), (2024, 2025))

    def test_inverted_fully_specified_range_is_not_swapped(self) -> None:
        self.assertEqual(
            self._ranges("2025年2月2日至2024年1月31日", languages=["zh-Hans"]),
            [],
        )
        self.assertEqual(self._ranges("2024-12-30 - 2023-01-03"), [])

    def test_inverted_with_missing_year_lists_all_legal_readings(self) -> None:
        ranges = self._ranges("12月30日至1月3日", languages=["zh-Hans"])
        self.assertEqual(
            [(r.start.date_obj, r.end.date_obj) for r in ranges],
            [
                (datetime(2026, 12, 30), datetime(2027, 1, 3)),
                (datetime(2025, 12, 30), datetime(2026, 1, 3)),
            ],
        )

    def test_month_crossing_rolls_end_forward(self) -> None:
        range_data = self._first("1月31日至5日", languages=["zh-Hans"])
        self.assertEqual(
            (range_data.start.date_obj, range_data.end.date_obj),
            (datetime(2026, 1, 31), datetime(2026, 2, 5)),
        )

    def test_impossible_shared_day_has_no_reading(self) -> None:
        # February has no 31st, so sharing the explicit start day is impossible.
        self.assertEqual(self._ranges("2月5日至31日", languages=["zh-Hans"]), [])
        self.assertEqual(self._ranges("3月31日至4月31日", languages=["zh-Hans"]), [])

    def test_non_ranges_return_empty(self) -> None:
        for text in ["", "January 2024", "not a range at all", "2024-12-30"]:
            self.assertEqual(self._ranges(text), [])

    def test_locale_is_propagated(self) -> None:
        range_data = self._first("31. Januar bis 2. Februar", languages=["de"])
        self.assertEqual(range_data.locale, "de")
        self.assertEqual(range_data.start.locale, "de")
        self.assertEqual(range_data.end.locale, "de")

    def test_range_data_supports_dict_access(self) -> None:
        range_data = self._first("January 31 to February 2")
        self.assertIs(range_data["start"], range_data.start)
        self.assertIs(range_data["end"], range_data.end)
        with self.assertRaises(KeyError):
            range_data["missing"]

    def test_parser_parse_matches_first_candidate(self) -> None:
        parser = DateRangeParser(languages=["zh-Hans"], settings=self.settings)
        candidates = parser.get_date_range_data("12月30日至1月3日")
        self.assertEqual(parser.parse("12月30日至1月3日"), candidates[0])
        self.assertIsNone(parser.parse("January 2024"))

    def test_non_string_input_raises_type_error(self) -> None:
        parser = DateRangeParser(settings=self.settings)
        with self.assertRaises(TypeError):
            parser.get_date_range_data(2024)  # type: ignore[arg-type]

    def test_endpoints_are_date_data(self) -> None:
        range_data = self._first("1月31日至2月2日", languages=["zh-Hans"])
        self.assertIsInstance(range_data, DateRangeData)
        self.assertIsInstance(range_data.start, DateData)
        self.assertIsInstance(range_data.end, DateData)

    def test_module_level_helpers_are_exported(self) -> None:
        self.assertIs(dateparser.parse_date_range, parse_date_range)
        self.assertIs(dateparser.DateRangeParser, DateRangeParser)
        self.assertIs(dateparser.DateRangeData, DateRangeData)

    def test_cjk_tilde_connector(self) -> None:
        range_data = self._first("1月～2月", languages=["zh-Hans"])
        start = range_data.start.date_obj
        end = range_data.end.date_obj
        assert start is not None and end is not None
        self.assertEqual((start.month, end.month), (1, 2))


if __name__ == "__main__":
    unittest.main()
