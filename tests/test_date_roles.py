from collections.abc import Sequence
from datetime import datetime

from parameterized import param, parameterized

from dateparser.search import DateRole, extract_role_candidates, find_date_roles
from tests import BaseTestCase


class TestDateRoles(BaseTestCase):
    @parameterized.expand(
        [
            param(
                ["en"],
                (
                    "Published on 10 January 2025. Effective 01 February 2025. "
                    "As amended on 01 December 2024."
                ),
                {
                    DateRole.PUBLICATION: datetime(2025, 1, 10),
                    DateRole.EFFECTIVE: datetime(2025, 2, 1),
                    DateRole.HISTORICAL: datetime(2024, 12, 1),
                },
                frozenset(),
                True,
            ),
            param(
                ["en"],
                "Published 10 January 2025. Published 20 January 2025. "
                "Effective 01 March 2025.",
                {DateRole.EFFECTIVE: datetime(2025, 3, 1)},
                frozenset({DateRole.PUBLICATION}),
                True,
            ),
            param(
                ["en"],
                "Published and effective 10 January 2025.",
                {},
                frozenset({DateRole.PUBLICATION, DateRole.EFFECTIVE}),
                True,
            ),
            param(
                ["en"],
                "Published 01 March 2025. Effective 01 January 2025.",
                {},
                frozenset({DateRole.PUBLICATION, DateRole.EFFECTIVE}),
                False,
            ),
            param(
                ["en"],
                "Published 10 January 2025. Amended 01 February 2025.",
                {},
                frozenset({DateRole.PUBLICATION, DateRole.HISTORICAL}),
                False,
            ),
            param(
                ["en"],
                "Published 10 January 2025. Effective 10 January 2025.",
                {
                    DateRole.PUBLICATION: datetime(2025, 1, 10),
                    DateRole.EFFECTIVE: datetime(2025, 1, 10),
                },
                frozenset(),
                True,
            ),
            param(
                ["en"],
                "Effective 01 January 2025. Effective 01 February 2025.",
                {},
                frozenset({DateRole.EFFECTIVE}),
                True,
            ),
            param(
                ["de"],
                "Bekanntmachung vom 04.11.2020, in Kraft ab 01.01.2021.",
                {
                    DateRole.PUBLICATION: datetime(2020, 4, 11),
                    DateRole.EFFECTIVE: datetime(2021, 1, 1),
                },
                frozenset(),
                True,
            ),
            param(
                ["en"],
                "Published 10 January 2025, effective 01 March 2025, "
                "replacing the 01 December 2024 version.",
                {
                    DateRole.PUBLICATION: datetime(2025, 1, 10),
                    DateRole.EFFECTIVE: datetime(2025, 3, 1),
                    DateRole.HISTORICAL: datetime(2024, 12, 1),
                },
                frozenset(),
                True,
            ),
        ]
    )
    def test_find_date_roles_assignments(
        self,
        languages: Sequence[str],
        text: str,
        expected: dict[DateRole, datetime],
        ambiguous: frozenset[DateRole],
        consistent: bool,
    ) -> None:
        result = find_date_roles(text, languages=languages)
        self.assertEqual(
            {role: candidate.date for role, candidate in result.chosen().items()},
            expected,
        )
        self.assertEqual(result.ambiguous_roles, ambiguous)
        self.assertIs(result.consistent, consistent)

    def test_extract_role_candidates_keeps_position_and_evidence(self) -> None:
        text = "Published on 10 January 2025. Effective 01 February 2025."
        candidates = extract_role_candidates(text, languages=["en"])

        self.assertEqual(len(candidates), 2)
        publication, effective = candidates

        self.assertEqual(
            text[publication.start : publication.end], publication.substring
        )
        self.assertEqual(publication.date, datetime(2025, 1, 10))
        self.assertEqual(publication.roles, frozenset({DateRole.PUBLICATION}))
        self.assertEqual(len(publication.evidence), 1)
        evidence = publication.evidence[0]
        self.assertEqual(evidence.role, DateRole.PUBLICATION)
        self.assertEqual(
            text[evidence.cue_start : evidence.cue_end].lower(), evidence.cue.lower()
        )
        self.assertTrue(evidence.before)

        self.assertEqual(text[effective.start : effective.end], effective.substring)
        self.assertEqual(effective.date, datetime(2025, 2, 1))
        self.assertEqual(effective.roles, frozenset({DateRole.EFFECTIVE}))

    def test_repeated_substring_is_located_in_order(self) -> None:
        text = "Published 10 January 2025. Effective 10 January 2025."
        candidates = extract_role_candidates(text, languages=["en"])

        self.assertEqual(len(candidates), 2)
        starts = [candidate.start for candidate in candidates]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(len(set(starts)), 2)
        for candidate in candidates:
            self.assertEqual(text[candidate.start : candidate.end], candidate.substring)

    def test_text_without_dates(self) -> None:
        result = find_date_roles("No dates here, just words.", languages=["en"])

        self.assertEqual(result.candidates, ())
        self.assertEqual(result.chosen(), {})
        self.assertEqual(result.ambiguous_roles, frozenset())
        self.assertTrue(result.consistent)
