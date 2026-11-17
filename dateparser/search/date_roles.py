"""Assign semantic roles to the dates found in an announcement.

An announcement often states several dates in one sentence, e.g.
"Published 10 January 2025, effective 01 March 2025, amended on
01 December 2024". :func:`dateparser.search.search_dates` finds each date but
not what it is for. This module pairs every found date with the role its
surrounding words imply:

* :class:`DateRole.PUBLICATION` - when the announcement was published,
* :class:`DateRole.EFFECTIVE` - when the announced measure takes effect,
* :class:`DateRole.HISTORICAL` - an earlier document or version it references.

The work is split in two, mirroring the task. :func:`extract_role_candidates`
locates each date in the text, records its position and the words that argue
for a role, and :func:`assign_roles` then picks one date per role only when the
picked dates satisfy the time relations of a single document. A role with more
than one plausible date, or a single fragment asked to play two roles, is left
ambiguous rather than guessed.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import regex as re

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import datetime

# How close a role word has to be to a date to count as evidence for it. The
# words in between are prepositions and articles ("on", "from", "vom"), so a
# fixed window is enough in the short clauses announcements use. A cue that
# only reaches a date across a sentence still attaches, but as evidence rather
# than a decision: if it muddies a role, the role is reported ambiguous instead
# of assigned with false confidence.
_CUE_WINDOW = 48


class DateRole(enum.Enum):
    """The part a date plays in an announcement."""

    PUBLICATION = "publication"
    EFFECTIVE = "effective"
    HISTORICAL = "historical"


# Role words, matched case-insensitively on word boundaries. Phrases are listed
# alongside single words; every match is collected, so a date may gather evidence
# for more than one role. English comes first, with the German phrases the
# article-date dataset uses most often added below.
_CUE_PATTERNS: dict[DateRole, tuple[str, ...]] = {
    DateRole.PUBLICATION: (
        r"published",
        r"publication",
        r"announc(?:e|ed|ement|ing)",
        r"releas(?:e|ed|ing)",
        r"issued?",
        r"promulgated?",
        r"dated",
        r"veröffentlicht",
        r"bekanntgegeben?",
        r"bekanntmachung",
    ),
    DateRole.EFFECTIVE: (
        r"effective",
        r"in(?:to)? force",
        r"takes? effect",
        r"comes? into force",
        r"enters? into force",
        r"in kraft",
        r"inkrafttreten",
        r"wirksam",
    ),
    DateRole.HISTORICAL: (
        r"amended?",
        r"amendment",
        r"revis(?:e|ed|ion|ing)",
        r"superseded?",
        r"repealed?",
        r"replac(?:e|ed|ing)",
        r"pursuant to",
        r"according to",
        r"referenc(?:e|ed|ing)",
        r"cit(?:e|ed|ing)",
        r"geändert",
        r"neugefasst",
    ),
}

_CUE_REGEXES: dict[DateRole, re.Pattern[str]] = {
    role: re.compile(r"\b(?:" + "|".join(patterns) + r")\b", re.IGNORECASE)
    for role, patterns in _CUE_PATTERNS.items()
}


@dataclass(frozen=True)
class RoleEvidence:
    """A single role word found next to a date."""

    role: DateRole
    cue: str
    cue_start: int
    cue_end: int
    before: bool


@dataclass(frozen=True)
class RoleCandidate:
    """A date found in the text with every role argued for it."""

    substring: str
    date: datetime
    start: int
    end: int
    evidence: tuple[RoleEvidence, ...]

    @property
    def roles(self) -> frozenset[DateRole]:
        return frozenset(item.role for item in self.evidence)


@dataclass(frozen=True)
class RoleAssignment:
    """The chosen role/date pairs and why a role could not be chosen."""

    text: str
    candidates: tuple[RoleCandidate, ...]
    publication: RoleCandidate | None
    effective: RoleCandidate | None
    historical: RoleCandidate | None
    ambiguous_roles: frozenset[DateRole]
    consistent: bool

    def chosen(self) -> dict[DateRole, RoleCandidate]:
        """Return the roles resolved to a single date."""
        return {
            role: candidate
            for role, candidate in (
                (DateRole.PUBLICATION, self.publication),
                (DateRole.EFFECTIVE, self.effective),
                (DateRole.HISTORICAL, self.historical),
            )
            if candidate is not None
        }


def _locate_occurrences(text: str, substrings: Sequence[str]) -> list[tuple[int, int]]:
    """Return the ``[start, end)`` span of each substring, in order.

    ``search_dates`` returns substrings but not positions, and the same
    substring can occur more than once, so each one is looked up after the end
    of the previous one rather than with a single ``str.find``.
    """
    spans: list[tuple[int, int]] = []
    cursor = 0
    for substring in substrings:
        if not substring:
            continue
        position = text.find(substring, cursor)
        if position == -1:
            position = text.find(substring)
        if position == -1:
            continue
        spans.append((position, position + len(substring)))
        cursor = position + len(substring)
    return spans


def _attach_evidence(
    text: str, spans: Sequence[tuple[int, int]]
) -> dict[int, list[RoleEvidence]]:
    """Attach every role word to the nearest date within the cue window."""
    evidence_by_candidate: dict[int, list[RoleEvidence]] = {
        index: [] for index in range(len(spans))
    }
    for role, pattern in _CUE_REGEXES.items():
        for match in pattern.finditer(text):
            cue_start, cue_end = match.span()
            options: list[tuple[int, int, bool]] = []
            for index, (start, end) in enumerate(spans):
                if cue_end <= start:
                    gap = start - cue_end
                    before = True
                elif cue_start >= end:
                    gap = cue_start - end
                    before = False
                else:
                    continue
                if gap <= _CUE_WINDOW:
                    options.append((gap, index, before))
            if not options:
                continue
            nearest = min(gap for gap, _, _ in options)
            for gap, index, before in options:
                if gap != nearest:
                    continue
                evidence_by_candidate[index].append(
                    RoleEvidence(
                        role=role,
                        cue=match.group(),
                        cue_start=cue_start,
                        cue_end=cue_end,
                        before=before,
                    )
                )
    return evidence_by_candidate


def extract_role_candidates(
    text: str,
    languages: Iterable[str] | None = None,
    settings: dict[str, object] | None = None,
) -> tuple[RoleCandidate, ...]:
    """Find the dates in *text* and the role evidence next to each.

    The search settings and languages are passed straight to
    :func:`dateparser.search.search_dates`. Each candidate keeps its original
    position and one :class:`RoleEvidence` per nearby role word, so the reason
    for a role is inspectable before any choice is made.
    """
    # Imported lazily: dateparser.search imports this module, so a top-level
    # import of the package would be circular.
    from dateparser.search import search_dates  # noqa: PLC0415

    found = search_dates(text, languages=languages, settings=settings)
    if not found:
        return ()
    # search_dates widens its return type to cover the optional period and
    # language tuples; the first two elements are always substring and date.
    matches = cast("list[tuple[str, datetime]]", found)

    spans = _locate_occurrences(text, [item[0] for item in matches])
    if len(spans) != len(matches):
        # A returned substring that cannot be located has no defensible
        # position, so assigning it a role would be guessing.
        return ()

    evidence_by_candidate = _attach_evidence(text, spans)
    candidates: list[RoleCandidate] = []
    for index, (item, (start, end)) in enumerate(zip(matches, spans, strict=True)):
        evidence = tuple(evidence_by_candidate[index])
        candidates.append(
            RoleCandidate(
                substring=item[0],
                date=item[1],
                start=start,
                end=end,
                evidence=evidence,
            )
        )
    return tuple(candidates)


def _candidates_for(
    candidates: Sequence[RoleCandidate], role: DateRole
) -> list[RoleCandidate]:
    return [candidate for candidate in candidates if role in candidate.roles]


def _naive(date: datetime) -> datetime:
    return date.replace(tzinfo=None) if date.tzinfo is not None else date


def _temporal_order_holds(
    publication: RoleCandidate | None,
    effective: RoleCandidate | None,
    historical: RoleCandidate | None,
) -> bool:
    """Check the time relations a single announcement's dates must satisfy.

    A referenced document predates publication, and a measure cannot take
    effect before it is published. Taking effect on the publication day is
    allowed.
    """
    if (
        publication is not None
        and effective is not None
        and _naive(effective.date) < _naive(publication.date)
    ):
        return False
    if (
        historical is not None
        and publication is not None
        and (_naive(historical.date) >= _naive(publication.date))
    ):
        return False
    return not (
        historical is not None
        and effective is not None
        and _naive(historical.date) >= _naive(effective.date)
    )


def assign_roles(candidates: Sequence[RoleCandidate], text: str = "") -> RoleAssignment:
    """Choose one date per role among *candidates*, keeping ambiguity visible.

    A role is resolved only when a single candidate argues for it and that
    candidate argues for no other role. Candidates sharing a role, or one
    fragment carrying several roles, leave the role ambiguous. The remaining
    choices are then checked against the document's time relations; a
    combination that violates them is reported as inconsistent and the roles
    involved are made ambiguous instead of being forced.
    """
    chosen: dict[DateRole, RoleCandidate | None] = {}
    ambiguous: set[DateRole] = set()
    for role in DateRole:
        role_candidates = _candidates_for(candidates, role)
        if len(role_candidates) != 1:
            chosen[role] = None
            if len(role_candidates) > 1:
                ambiguous.add(role)
            continue
        sole_candidate = role_candidates[0]
        if len(sole_candidate.roles) > 1:
            # One fragment cannot stand for two independent declarations.
            chosen[role] = None
            ambiguous.add(role)
            continue
        chosen[role] = sole_candidate

    publication = chosen[DateRole.PUBLICATION]
    effective = chosen[DateRole.EFFECTIVE]
    historical = chosen[DateRole.HISTORICAL]
    consistent = _temporal_order_holds(publication, effective, historical)
    if not consistent:
        for role, candidate in tuple(chosen.items()):
            if candidate is not None:
                chosen[role] = None
                ambiguous.add(role)

    return RoleAssignment(
        text=text,
        candidates=tuple(candidates),
        publication=chosen[DateRole.PUBLICATION],
        effective=chosen[DateRole.EFFECTIVE],
        historical=chosen[DateRole.HISTORICAL],
        ambiguous_roles=frozenset(ambiguous),
        consistent=consistent,
    )


def find_date_roles(
    text: str,
    languages: Iterable[str] | None = None,
    settings: dict[str, object] | None = None,
) -> RoleAssignment:
    """Extract the dates in *text* and assign each an announcement role."""
    candidates = extract_role_candidates(text, languages=languages, settings=settings)
    return assign_roles(candidates, text=text)
