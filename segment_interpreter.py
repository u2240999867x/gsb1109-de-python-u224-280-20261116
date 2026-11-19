"""Segment-level interpretation of digit runs as phone numbers.

Given a text segment (e.g. an announcement) containing several runs of
digits, decide for every run whether it is

* part of a phone number (possibly a multi-run number such as
  "030 123456"),
* the extension of a preceding number ("030 123456 Durchwahl 89"),
* excluded content such as a date or an identifier (context rules), or
* uninterpreted.

The interpretation is chosen by minimising the total cost over the whole
segment instead of greedily in text order, so boundary conflicts are
resolved by the configured costs only.  When several interpretations are
equally cheap, only the occurrences shared by all optimal interpretations
are confirmed; the rest stay as candidates.  Every occurrence points back
to the original character offsets; normalisation never changes spans.
"""

import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional, Tuple

import phonenumbers

__all__ = [
    "ContextRule",
    "CostModel",
    "ShareableConventions",
    "Extension",
    "Number",
    "Excluded",
    "Uninterpreted",
    "SegmentInterpretation",
    "interpret_segment",
]

_TOL = dict(rel_tol=1e-9, abs_tol=1e-9)


@dataclass(frozen=True)
class ContextRule:
    """A rule marking text spans (dates, identifiers, ...) as excluded."""

    name: str
    pattern: str
    flags: int = 0


@dataclass(frozen=True)
class CostModel:
    """Costs of the candidate interpretations."""

    number: float = 1.0
    extension: float = 0.5  # added to the number cost for number+extension
    excluded: float = 0.5
    uninterpreted: float = 2.0


@dataclass(frozen=True)
class ShareableConventions:
    """Which characters may be shared inside / between interpretations.

    ``number_separators`` may appear between the digit runs of a single
    number, ``extension_separators`` between a number and its extension.
    Both are regexes full-matched against the gap between two runs.
    """

    number_separators: Tuple[str, ...] = (r"[\s.\-]+",)
    extension_separators: Tuple[str, ...] = (
        r"\s*(?:ext\.?|x|#|dw|durchwahl)\s*",
    )
    max_extension_digits: int = 7
    max_number_parts: int = 4


@dataclass(frozen=True)
class Extension:
    start: int
    end: int
    raw: str
    digits: str
    kind: str = field(default="extension", init=False)


@dataclass(frozen=True)
class Number:
    start: int
    end: int
    raw: str
    normalized: str
    regions: frozenset
    extension: Optional[Extension] = None
    kind: str = field(default="number", init=False)

    @property
    def region_ambiguous(self):
        return len(self.regions) > 1


@dataclass(frozen=True)
class Excluded:
    start: int
    end: int
    raw: str
    rules: Tuple[str, ...]
    kind: str = field(default="excluded", init=False)


@dataclass(frozen=True)
class Uninterpreted:
    start: int
    end: int
    raw: str
    digits: str
    kind: str = field(default="uninterpreted", init=False)


@dataclass(frozen=True)
class SegmentInterpretation:
    total_cost: float
    confirmed: tuple  # occurrences present in every optimal interpretation
    candidates: tuple  # occurrences present in some but not all optima

    @property
    def unique(self):
        return not self.candidates


@dataclass(frozen=True)
class _Run:
    start: int
    end: int
    raw: str
    digits: str


@dataclass(frozen=True)
class _Piece:
    first: int  # index of first covered run
    last: int  # index of last covered run (inclusive)
    cost: float
    occurrence: object


_PLUS_RE = re.compile(r"\+\s*\Z")


def _scan_runs(text):
    runs = []
    i = 0
    while i < len(text):
        if text[i].isdecimal():
            j = i
            while j < len(text) and text[j].isdecimal():
                j += 1
            raw = text[i:j]
            digits = "".join(str(unicodedata.decimal(ch)) for ch in raw)
            runs.append(_Run(i, j, raw, digits))
            i = j
        else:
            i += 1
    return runs


def _find_exclusions(text, rules):
    matches = []
    for rule in rules:
        for m in re.finditer(rule.pattern, text, rule.flags):
            if m.end() > m.start():
                matches.append((m.start(), m.end(), (rule.name,)))
    matches.sort()
    merged = []
    for start, end, names in matches:
        if merged and start < merged[-1][1]:
            prev_start, prev_end, prev_names = merged[-1]
            merged[-1] = (
                prev_start,
                max(prev_end, end),
                tuple(sorted(set(prev_names) | set(names))),
            )
        else:
            merged.append((start, end, names))
    return merged


def _validate(digits, international, regions):
    """Return the set of regions in which the digit string is valid."""
    found = set()
    if international:
        try:
            num = phonenumbers.parse("+" + digits, None)
        except phonenumbers.NumberParseException:
            return frozenset()
        if phonenumbers.is_valid_number(num):
            found.add(phonenumbers.region_code_for_number(num) or "")
    else:
        for region in regions:
            try:
                num = phonenumbers.parse(digits, region)
            except phonenumbers.NumberParseException:
                continue
            if phonenumbers.is_valid_number(num):
                found.add(phonenumbers.region_code_for_number(num) or region)
    return frozenset(found)


def _make_pieces(text, runs, exclusions, regions, costs, conv):
    n_runs = len(runs)
    excluded_run = [False] * n_runs
    for xs, xe, _names in exclusions:
        for i, run in enumerate(runs):
            if run.start < xe and xs < run.end:
                excluded_run[i] = True

    def span_clear(start, end):
        return all(end <= xs or xe <= start for xs, xe, _n in exclusions)

    number_seps = tuple(re.compile(p) for p in conv.number_separators)
    ext_seps = tuple(
        re.compile(p, re.IGNORECASE) for p in conv.extension_separators
    )

    def gap_matches(a, b, seps):
        gap = text[runs[a].end:runs[b].start]
        return any(sep.fullmatch(gap) for sep in seps)

    pieces = []
    number_pieces = []

    for a in range(n_runs):
        if excluded_run[a]:
            continue
        # A leading '+' is evidence for the international format; when it
        # is present and yields a valid number, the national reading of the
        # same runs is not offered as a competing interpretation.
        plus_start = None
        m = _PLUS_RE.search(text, max(0, runs[a].start - 9), runs[a].start)
        if m and m.end() == runs[a].start and span_clear(m.start(), runs[a].start):
            plus_start = m.start()
        digits = ""
        for b in range(a, min(a + conv.max_number_parts, n_runs)):
            if excluded_run[b]:
                break
            if b > a and not gap_matches(b - 1, b, number_seps):
                break
            digits += runs[b].digits
            end = runs[b].end
            if plus_start is not None and span_clear(plus_start, end):
                intl_regions = _validate(digits, True, regions)
                if intl_regions:
                    occ = Number(
                        start=plus_start,
                        end=end,
                        raw=text[plus_start:end],
                        normalized="+" + digits,
                        regions=intl_regions,
                    )
                    piece = _Piece(a, b, costs.number, occ)
                    pieces.append(piece)
                    number_pieces.append(piece)
                    continue
            if span_clear(runs[a].start, end):
                nat_regions = _validate(digits, False, regions)
                if nat_regions:
                    occ = Number(
                        start=runs[a].start,
                        end=end,
                        raw=text[runs[a].start:end],
                        normalized=digits,
                        regions=nat_regions,
                    )
                    piece = _Piece(a, b, costs.number, occ)
                    pieces.append(piece)
                    number_pieces.append(piece)

    for piece in number_pieces:
        nxt = piece.last + 1
        if nxt >= n_runs or excluded_run[nxt]:
            continue
        if not 1 <= len(runs[nxt].digits) <= conv.max_extension_digits:
            continue
        if not gap_matches(piece.last, nxt, ext_seps):
            continue
        base = piece.occurrence
        end = runs[nxt].end
        if not span_clear(base.start, end):
            continue
        ext = Extension(
            start=runs[nxt].start,
            end=end,
            raw=text[runs[nxt].start:end],
            digits=runs[nxt].digits,
        )
        occ = Number(
            start=base.start,
            end=end,
            raw=text[base.start:end],
            normalized=base.normalized,
            regions=base.regions,
            extension=ext,
        )
        pieces.append(
            _Piece(piece.first, nxt, piece.cost + costs.extension, occ)
        )

    for xs, xe, names in exclusions:
        covered = [
            i for i, run in enumerate(runs) if run.start < xe and xs < run.end
        ]
        if not covered:
            continue
        occ = Excluded(start=xs, end=xe, raw=text[xs:xe], rules=tuple(names))
        pieces.append(_Piece(covered[0], covered[-1], costs.excluded, occ))

    for i, run in enumerate(runs):
        if excluded_run[i]:
            continue
        occ = Uninterpreted(
            start=run.start, end=run.end, raw=run.raw, digits=run.digits
        )
        pieces.append(_Piece(i, i, costs.uninterpreted, occ))

    return pieces


def _forward(pieces, n_runs, forbid=None):
    """f[i] = minimum cost to cover runs 0..i-1."""
    inf = float("inf")
    by_start = [[] for _ in range(n_runs)]
    for piece in pieces:
        if piece is not forbid:
            by_start[piece.first].append(piece)
    f = [inf] * (n_runs + 1)
    f[0] = 0.0
    for i in range(n_runs):
        if f[i] == inf:
            continue
        for piece in by_start[i]:
            cand = f[i] + piece.cost
            if cand < f[piece.last + 1]:
                f[piece.last + 1] = cand
    return f


def _backward(pieces, n_runs, forbid=None):
    """g[i] = minimum cost to cover runs i..n_runs-1."""
    inf = float("inf")
    by_start = [[] for _ in range(n_runs)]
    for piece in pieces:
        if piece is not forbid:
            by_start[piece.first].append(piece)
    g = [inf] * (n_runs + 1)
    g[n_runs] = 0.0
    for i in range(n_runs - 1, -1, -1):
        best = inf
        for piece in by_start[i]:
            if g[piece.last + 1] != inf:
                best = min(best, piece.cost + g[piece.last + 1])
        g[i] = best
    return g


def interpret_segment(text, *, regions, context_rules=(), costs=None,
                      conventions=None):
    """Choose the minimum-cost compatible interpretation of a text segment.

    ``regions`` are the allowed region codes, ``context_rules`` mark
    excluded content (dates, identifiers), ``costs`` prices the candidate
    interpretations and ``conventions`` defines the shareable separator
    characters.  Returns a SegmentInterpretation whose ``confirmed``
    occurrences appear in every optimal interpretation while
    ``candidates`` appear in only some of them.
    """
    costs = costs or CostModel()
    conv = conventions or ShareableConventions()
    runs = _scan_runs(text)
    exclusions = _find_exclusions(text, context_rules)
    pieces = _make_pieces(text, runs, exclusions, tuple(regions), costs, conv)

    n_runs = len(runs)
    f = _forward(pieces, n_runs)
    g = _backward(pieces, n_runs)
    optimum = f[n_runs]

    confirmed = []
    candidates = []
    for piece in pieces:
        if f[piece.first] == float("inf") or g[piece.last + 1] == float("inf"):
            continue
        on_optimum = math.isclose(
            f[piece.first] + piece.cost + g[piece.last + 1], optimum, **_TOL
        )
        if not on_optimum:
            continue
        without = _forward(pieces, n_runs, forbid=piece)[n_runs]
        if math.isclose(without, optimum, **_TOL):
            candidates.append(piece.occurrence)
        else:
            confirmed.append(piece.occurrence)

    key = lambda occ: (occ.start, occ.end, occ.kind)
    return SegmentInterpretation(
        total_cost=optimum,
        confirmed=tuple(sorted(confirmed, key=key)),
        candidates=tuple(sorted(candidates, key=key)),
    )
