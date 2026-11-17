from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from itertools import permutations

import pytest

from dateparser.batch import BatchParser, Convention, VerifiedRow, parse_batch


def conventions(result) -> list[str]:
    return [str(convention) for convention in result.conventions]


def by_text(result) -> dict[str, datetime | None]:
    return {row.text: row.date for row in result.rows}


class TestInference:
    def test_verified_rows_narrow_the_batch_to_one_convention(self) -> None:
        result = parse_batch(
            ["05/06/2021"],
            allowed_languages=["en", "de", "fr"],
            verified=[
                ("5 März 2021", date(2021, 3, 5)),
                ("05/03/2021", date(2021, 3, 5)),
            ],
        )

        assert result.ok
        assert result.inferred
        assert conventions(result) == ["de:DMY"]
        assert result.rows[0].date == datetime(2021, 6, 5)

    def test_language_and_date_order_are_reported_together(self) -> None:
        result = parse_batch(
            ["05/06/2021"],
            allowed_languages=["en", "de", "fr"],
            verified=[("5 März 2021", date(2021, 3, 5))],
        )

        assert result.conventions == (Convention("de", "DMY"),)

    def test_ambiguous_row_keeps_every_supported_candidate(self) -> None:
        result = parse_batch(
            ["05/03/2021", "13/05/2021"],
            allowed_languages=["en", "de"],
            date_orders=["DMY", "MDY"],
        )

        ambiguous, unambiguous = result.rows

        assert ambiguous.date is None
        assert ambiguous.ambiguous
        assert [(candidate.date, sorted(str(c) for c in candidate.conventions)) for candidate in ambiguous.candidates] == [
            (datetime(2021, 3, 5), ["de:DMY", "en:DMY"]),
            (datetime(2021, 5, 3), ["de:MDY", "en:MDY"]),
        ]
        assert unambiguous.date == datetime(2021, 5, 13)

    def test_a_row_is_definite_only_when_all_explanations_agree(self) -> None:
        result = parse_batch(
            ["05/03/2021", "2021-03-05"],
            allowed_languages=["en", "de"],
            date_orders=["DMY", "MDY"],
        )

        ambiguous, definite = result.rows

        assert ambiguous.date is None
        assert definite.date == datetime(2021, 3, 5)

    def test_international_numeric_format_does_not_decide_from_file_order(
        self,
    ) -> None:
        # "05/03/2021" alone must not make DMY the convention just because it
        # is the first row: both DMY and MDY explain it.
        result = parse_batch(
            ["05/03/2021"],
            allowed_languages=["en", "de"],
            date_orders=["DMY", "MDY"],
        )

        assert result.ok
        assert conventions(result) == ["de:DMY", "de:MDY", "en:DMY", "en:MDY"]
        assert result.rows[0].date is None

    def test_verified_date_order_wins_over_row_order(self) -> None:
        result = parse_batch(
            ["06/04/2021"],
            allowed_languages=["en", "de"],
            date_orders=["DMY", "MDY"],
            verified=[("05/03/2021", date(2021, 5, 3))],
        )

        assert result.rows[0].date == datetime(2021, 6, 4)

    def test_verified_rows_can_be_given_as_a_mapping(self) -> None:
        result = parse_batch(
            ["05/06/2021"],
            allowed_languages=["de", "en"],
            verified={"5 März 2021": date(2021, 3, 5)},
        )

        assert result.rows[0].date == datetime(2021, 6, 5)

    def test_verified_rows_can_be_given_as_objects(self) -> None:
        result = parse_batch(
            ["05/06/2021"],
            allowed_languages=["de", "en"],
            verified=[VerifiedRow("5 März 2021", date(2021, 3, 5))],
        )

        assert result.rows[0].date == datetime(2021, 6, 5)

    def test_iso_datetime_rows_keep_their_time(self) -> None:
        result = parse_batch(
            ["2021-03-05T12:30:00"],
            allowed_languages=["de"],
            date_orders=["DMY"],
        )

        assert result.rows[0].date == datetime(2021, 3, 5, 12, 30)

    def test_date_only_expected_dates_ignore_the_time(self) -> None:
        result = parse_batch(
            [],
            allowed_languages=["de"],
            date_orders=["DMY"],
            verified=[("5 März 2021 12:30", date(2021, 3, 5))],
        )

        assert result.ok
        assert result.verified_rows[0].matches_expected

    def test_no_evidence_leaves_every_language_open(self) -> None:
        result = parse_batch(
            ["5 März 2021"],
            allowed_languages=["de", "fr"],
        )

        assert result.ok
        assert conventions(result) == ["de:DMY", "fr:DMY"]
        assert result.rows[0].date == datetime(2021, 3, 5)

    def test_default_date_orders_come_from_the_language_data(self) -> None:
        result = parse_batch(
            ["5 März 2021"],
            allowed_languages=["de"],
        )

        assert conventions(result) == ["de:DMY"]

    def test_an_explicit_date_order_set_is_honoured(self) -> None:
        result = parse_batch(
            ["5 März 2021"],
            allowed_languages=["de"],
            date_orders=["YMD"],
        )

        assert conventions(result) == ["de:YMD"]


class TestConflicts:
    def test_conflicting_rows_are_reported_instead_of_switching_language(self) -> None:
        # Both rows are explainable on their own, but no convention reads both
        # as their expected dates.
        result = parse_batch(
            ["06/04/2021"],
            allowed_languages=["en", "de"],
            date_orders=["DMY", "MDY"],
            verified=[
                ("05/03/2021", date(2021, 3, 5)),
                ("05/03/2021", date(2021, 5, 3)),
            ],
        )

        assert not result.ok
        assert conventions(result) == []
        assert [conflict.text for conflict in result.conflicts] == [
            "05/03/2021",
            "05/03/2021",
        ]
        assert result.rows[0].date is None

    def test_conflicting_rows_keep_their_own_candidates(self) -> None:
        result = parse_batch(
            [],
            allowed_languages=["en", "de"],
            date_orders=["DMY", "MDY"],
            verified=[
                ("05/03/2021", date(2021, 3, 5)),
                ("05/03/2021", date(2021, 5, 3)),
            ],
        )

        first, second = result.verified_rows

        assert first.matches_expected
        assert second.matches_expected
        assert first.date == datetime(2021, 3, 5)
        assert second.date == datetime(2021, 5, 3)
        assert len(first.candidates) == 2

    def test_conflicting_row_is_the_one_not_supported_by_any_convention(
        self,
    ) -> None:
        result = parse_batch(
            [],
            allowed_languages=["de"],
            date_orders=["DMY"],
            verified=[
                ("05/03/2021", date(2021, 3, 5)),
                ("05/03/2021", date(2021, 5, 3)),
            ],
        )

        assert [conflict.index for conflict in result.conflicts] == [1]
        assert result.verified_rows[1].matches_expected is False

    def test_conflict_is_found_regardless_of_row_order(self) -> None:
        rows = [
            VerifiedRow("05/03/2021", date(2021, 3, 5)),
            VerifiedRow("05/03/2021", date(2021, 5, 3)),
            VerifiedRow("7 März 2021", date(2021, 3, 7)),
        ]
        reported = {
            tuple(
                (conflict.text, str(conflict.expected))
                for conflict in parse_batch(
                    [],
                    allowed_languages=["en", "de"],
                    date_orders=["DMY", "MDY"],
                    verified=list(order),
                ).conflicts
            )
            for order in permutations(rows)
        }

        # Both rows of the minimal conflict are reported, and neither the set
        # nor its order depends on the position of the rows in the input.
        assert reported == {
            (
                ("05/03/2021", "2021-03-05 00:00:00"),
                ("05/03/2021", "2021-05-03 00:00:00"),
            )
        }

    def test_rows_are_not_parsed_with_one_language_each_to_make_them_fit(
        self,
    ) -> None:
        # "5 März 2021" needs German, "5 mars 2021" needs French, so no single
        # language/date-order convention explains both.  The result must report
        # the conflict instead of reading one row in German and the other in
        # French.
        result = parse_batch(
            [],
            allowed_languages=["de", "fr"],
            verified=[
                ("5 März 2021", date(2021, 3, 5)),
                ("5 mars 2021", date(2021, 3, 5)),
            ],
        )

        assert not result.ok
        assert result.conflicts
        assert result.verified_rows[0].languages == ("de",)
        assert result.verified_rows[1].languages == ("fr",)

    def test_unparseable_row_is_not_used_as_an_explanation(self) -> None:
        result = parse_batch(
            ["05/06/2021"],
            allowed_languages=["de", "fr"],
            verified=[("not a date", date(2021, 3, 5))],
        )

        assert not result.ok
        assert [conflict.index for conflict in result.conflicts] == [0]
        assert result.conflicts[0].supported == ()


class TestExplicitConfiguration:
    def test_explicit_languages_are_used_as_configured(self) -> None:
        result = parse_batch(["05/03/2021"], languages=["en"])

        assert not result.inferred
        assert conventions(result) == ["en:MDY"]
        assert result.rows[0].date == datetime(2021, 5, 3)

    def test_explicit_configuration_is_not_widened_to_fit(self) -> None:
        result = parse_batch(
            ["5 März 2021"],
            languages=["en"],
        )

        assert result.rows[0].date is None
        assert conventions(result) == ["en:MDY"]

    def test_explicit_verified_mismatch_is_reported(self) -> None:
        result = parse_batch(
            [],
            languages=["en"],
            verified=[("05/03/2021", date(2021, 3, 5))],
        )

        assert not result.ok
        assert [conflict.index for conflict in result.conflicts] == [0]

    def test_explicit_locating_date_order_is_honoured(self) -> None:
        result = parse_batch(
            ["05/03/2021"],
            languages=["de"],
            date_orders=["MDY"],
        )

        assert conventions(result) == ["de:MDY"]
        assert result.rows[0].date == datetime(2021, 5, 3)

    def test_languages_and_allowed_languages_are_mutually_exclusive(self) -> None:
        with pytest.raises(ValueError, match="not both"):
            BatchParser(allowed_languages=["de"], languages=["de"])


class TestIndependence:
    TEXTS = ["05/03/2021", "06.03.2021", "13/05/2021", "2021-03-05"]
    VERIFIED = [("5 März 2021", date(2021, 3, 5))]

    def _parse(self, texts, verified):
        return parse_batch(
            list(texts),
            allowed_languages=["en", "de", "fr"],
            verified=list(verified),
        )

    def test_input_order_does_not_change_results(self) -> None:
        forward = self._parse(self.TEXTS, self.VERIFIED)
        backward = self._parse(list(reversed(self.TEXTS)), list(reversed(self.VERIFIED)))

        assert by_text(forward) == by_text(backward)
        assert conventions(forward) == conventions(backward)

    def test_parallel_configurations_do_not_interfere(self) -> None:
        german = BatchParser(allowed_languages=["de"], date_orders=["DMY"])
        french = BatchParser(allowed_languages=["fr"], date_orders=["DMY"])

        german_result = german.parse(["05/06/2021"], self.VERIFIED)
        french_result = french.parse(["05/06/2021"], self.VERIFIED)

        assert german_result.rows[0].date == datetime(2021, 6, 5)
        assert french_result.rows[0].date is None

    def test_configurations_run_in_parallel_without_sharing_state(self) -> None:
        german = BatchParser(
            allowed_languages=["de"],
            date_orders=["DMY"],
            settings={"RELATIVE_BASE": datetime(2021, 3, 5, 12, 0)},
        )
        english = BatchParser(
            allowed_languages=["en"],
            date_orders=["MDY"],
            settings={"RELATIVE_BASE": datetime(1999, 1, 1, 0, 0)},
        )

        def run(parser: BatchParser) -> tuple[dict[str, datetime | None], list[str]]:
            result = parser.parse(
                ["05/06/2021", "tomorrow"],
                [("05/03/2021", date(2021, 3, 5))],
            )
            return by_text(result), conventions(result)

        with ThreadPoolExecutor(max_workers=8) as executor:
            outcomes = [
                executor.submit(run, parser)
                for parser in [german, english, german, english] * 8
            ]
            results = [future.result() for future in outcomes]

        expected = {
            "german": (
                {
                    "05/06/2021": datetime(2021, 6, 5),
                    "tomorrow": None,
                },
                ["de:DMY"],
            ),
            "english": (
                {"05/06/2021": None, "tomorrow": None},
                [],
            ),
        }
        for index, result in enumerate(results):
            assert result == expected["german" if index % 2 == 0 else "english"]

    def test_reusing_a_parser_gives_the_same_results(self) -> None:
        parser = BatchParser(allowed_languages=["de"])

        first = parser.parse(self.TEXTS, self.VERIFIED)
        second = parser.parse(self.TEXTS, self.VERIFIED)

        assert by_text(first) == by_text(second)
        assert conventions(first) == conventions(second)

    def test_settings_are_not_shared_between_batches(self) -> None:
        first = parse_batch(
            ["05/03/2021"],
            languages=["en"],
            settings={"DATE_ORDER": "DMY"},
        )
        second = parse_batch(["05/03/2021"], languages=["en"])

        assert first.rows[0].date == datetime(2021, 3, 5)
        assert second.rows[0].date == datetime(2021, 5, 3)

    def test_result_is_deterministic_across_repeated_parsers(self) -> None:
        results = []
        for _ in range(3):
            parser = BatchParser(
                allowed_languages=["de", "fr", "en"], date_orders=["DMY", "MDY"]
            )
            results.append(
                parser.parse(
                    ["05/03/2021"],
                    [("5 März 2021", date(2021, 3, 5)), ("05/03/2021", date(2021, 3, 5))],
                )
            )
        assert {
            tuple(conventions(result)) for result in results
        } == {("de:DMY",)}


class TestValidation:
    def test_unknown_language_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unknown language"):
            parse_batch(["05/03/2021"], allowed_languages=["xx"])

    def test_unknown_date_order_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unknown date order"):
            parse_batch(["05/03/2021"], allowed_languages=["en"], date_orders=["XYZ"])

    def test_a_batch_needs_languages_or_allowed_languages(self) -> None:
        with pytest.raises(ValueError, match="must be given"):
            BatchParser()

    def test_texts_must_be_strings(self) -> None:
        with pytest.raises(TypeError, match="texts"):
            parse_batch([20210305], allowed_languages=["en"])  # type: ignore[list-item]

    def test_verified_rows_must_be_dates(self) -> None:
        with pytest.raises(TypeError, match="Expected dates"):
            parse_batch(
                [],
                allowed_languages=["en"],
                verified=[("05/03/2021", 20210305)],  # type: ignore[list-item]
            )


class TestEdgeCases:
    def test_an_empty_batch_still_reports_the_convention(self) -> None:
        result = parse_batch([], allowed_languages=["de"])

        assert result.ok
        assert conventions(result) == ["de:DMY"]
        assert result.rows == ()

    def test_alias_arguments_are_accepted(self) -> None:
        result = parse_batch(
            None,
            allowed_languages=["de"],
            unverified_texts=["05.06.2021"],
            verified_rows=[{"text": "5 März 2021", "expected": "2021-03-05"}],
        )

        assert result.rows[0].date == datetime(2021, 6, 5)

    def test_giving_a_text_twice_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="not both"):
            parse_batch(["05/03/2021"], allowed_languages=["en"], unverified_texts=[])

    def test_a_batch_with_no_rows_has_no_conflicts(self) -> None:
        result = parse_batch([], allowed_languages=["de"], verified=[])

        assert result.conflicts == ()
        assert result.verified_rows == ()
