"""Resolve the domestic dialling convention shared by all rows of a batch.

A legacy directory may not record a region per row.  The supplier guarantees
that the rows in one column all follow the same domestic dialling convention,
i.e. a single region code.  Given a finite list of candidate regions, this
module works out which explanations (region choices) still hold for the whole
batch and which full numbers can therefore be confirmed.

Rules mirrored from the task statement:

* every row is read under every candidate region, and a region stays an
  explanation only if *every* row is compatible with it, so reordering the
  rows cannot change the outcome;
* a row that already carries an explicit international prefix ("+", or the
  international direct dialling prefix of a candidate region) is parsed as an
  international number and is never re-read as a domestic number;
* when several regions survive, a row is confirmed only when every surviving
  explanation produces the same E.164 number, otherwise it keeps its
  per-region candidates;
* when no region survives, the rows that reject each region are reported as
  contradictory evidence.
"""

import re

from phonenumbers import NumberParseException, PhoneMetadata
from phonenumbers import PhoneNumberFormat, format_number
from phonenumbers import is_possible_number, is_valid_number, parse

_IDD_PATTERN_CACHE = {}


class RowRead(object):
    """How one raw row is read under one candidate region.

    ``error`` is ``None`` for an acceptable read; otherwise it explains why
    the region cannot account for the row.
    """

    def __init__(self, region, e164=None, possible=False, valid=False,
                 error=None):
        self.region = region
        self.e164 = e164
        self.possible = possible
        self.valid = valid
        self.error = error

    def __repr__(self):
        if self.error is not None:
            return "RowRead(region=%r, error=%r)" % (self.region, self.error)
        return "RowRead(region=%r, e164=%r)" % (self.region, self.e164)


class RowReport(object):
    """Outcome for a single row once all surviving regions are known."""

    def __init__(self, row_id, raw, international, reads, rejected,
                 confirmed):
        self.row_id = row_id
        self.raw = raw
        self.international = international
        # region -> E164 number, for every surviving explanation
        self.reads = dict(reads)
        # region -> reason, for the explanations this row rejects
        self.rejected = dict(rejected)
        # E164 number when all surviving reads agree, otherwise None
        self.confirmed = confirmed

    @property
    def is_confirmed(self):
        return self.confirmed is not None

    @property
    def candidates(self):
        """region -> E164 number while the row stays unconfirmed."""
        if self.is_confirmed:
            return {}
        return dict(self.reads)


class BatchReport(object):
    """Result of resolving a whole batch against its candidate regions."""

    def __init__(self, candidate_regions, rows, surviving, eliminated,
                 reports):
        self.candidate_regions = tuple(candidate_regions)
        self.rows = tuple(rows)
        self.surviving_regions = tuple(surviving)
        # region -> sorted tuple of (row_id, reason) evidence against it;
        # recording every rejecting row keeps the report independent of the
        # order in which the rows were supplied
        self.eliminated = dict(eliminated)
        self.row_reports = list(reports)

    @property
    def consistent(self):
        return len(self.surviving_regions) > 0

    @property
    def contradiction(self):
        return not self.consistent

    def report_for(self, row_id):
        for report in self.row_reports:
            if report.row_id == row_id:
                return report
        raise KeyError(row_id)

    def confirmed_numbers(self):
        """row_id -> E164 number for every row with a unique reading."""
        return dict(
            (report.row_id, report.confirmed)
            for report in self.row_reports if report.is_confirmed)

    def candidate_numbers(self):
        """row_id -> {region: E164 number} for every still-open row."""
        return dict(
            (report.row_id, report.candidates)
            for report in self.row_reports if not report.is_confirmed)


def _metadata(region):
    metadata = PhoneMetadata.metadata_for_region(region)
    if metadata is None:
        raise ValueError("unknown candidate region %r" % region)
    return metadata


def _idd_pattern(region):
    if region not in _IDD_PATTERN_CACHE:
        prefix = _metadata(region).international_prefix
        _IDD_PATTERN_CACHE[region] = (
            re.compile("^(?:%s)" % prefix) if prefix else None)
    return _IDD_PATTERN_CACHE[region]


def _has_international_prefix(raw, region):
    text = raw.strip()
    if text.startswith("+"):
        return True
    pattern = _idd_pattern(region)
    if pattern is None:
        return False
    return pattern.match(re.sub(r"\D", "", text)) is not None


def _validate(region, raw, parse_region):
    try:
        numobj = parse(raw, parse_region)
    except NumberParseException as exc:
        return RowRead(
            region,
            error="not parseable under region %s: %s" % (region, exc))
    e164 = format_number(numobj, PhoneNumberFormat.E164)
    if not is_possible_number(numobj):
        return RowRead(region, e164=e164,
                       error="not a possible number under region %s" % region)
    valid = is_valid_number(numobj)
    return RowRead(region, e164=e164, possible=True, valid=valid)


def _read_domestic(raw, region, require_valid):
    read = _validate(region, raw, region)
    if read.error is None and require_valid and not read.valid:
        read.error = (
            "not a valid assigned number under region %s" % region)
    return read


def _read_international(raw, region):
    parse_region = None if raw.strip().startswith("+") else region
    return _validate(region, raw, parse_region)


def _normalise_regions(candidate_regions):
    if not candidate_regions:
        raise ValueError("at least one candidate region is required")
    regions = []
    for region in candidate_regions:
        _metadata(region)
        if region not in regions:
            regions.append(region)
    return regions


def _normalise_rows(rows):
    normalised = []
    for index, row in enumerate(rows):
        if isinstance(row, tuple):
            row_id, raw = row
        else:
            row_id, raw = "r%d" % (index + 1), row
        normalised.append((str(row_id), raw))
    return normalised


def resolve_batch(rows, candidate_regions, require_valid=True):
    """Find the explanations that hold for every row of the batch.

    ``rows`` is an iterable of either raw strings (assigned ids ``r1`` ...)
    or ``(row_id, raw)`` tuples.  ``candidate_regions`` is the finite list of
    ISO 3166-1 region codes allowed by the directory.  When ``require_valid``
    is true a read must be a valid assigned number; otherwise being a possible
    number is enough.

    Returns a :class:`BatchReport`; the computation builds the full
    region-by-row compatibility table before selecting survivors, so it is
    independent of the order of ``rows``.
    """
    regions = _normalise_regions(candidate_regions)
    normalised_rows = _normalise_rows(rows)

    table = []
    international_flags = []
    for row_id, raw in normalised_rows:
        reads = {}
        treated_as_international = False
        for region in regions:
            # Each region applies its own convention: a row is international
            # for that region only when it carries "+" or the region's own IDD
            # prefix, so an international record is never force-read as a
            # domestic number under a convention that recognises the prefix.
            if _has_international_prefix(raw, region):
                treated_as_international = True
                reads[region] = _read_international(raw, region)
            else:
                reads[region] = _read_domestic(raw, region, require_valid)
        international_flags.append(treated_as_international)
        table.append(reads)

    surviving = [
        region for region in regions
        if all(
            table[index][region].error is None
            for index in range(len(normalised_rows)))]

    eliminated = {}
    for region in regions:
        if region in surviving:
            continue
        evidence = [
            (row_id, table[index][region].error)
            for index, (row_id, _raw) in enumerate(normalised_rows)
            if table[index][region].error is not None]
        eliminated[region] = tuple(sorted(evidence, key=lambda item: item[0]))

    reports = []
    for index, (row_id, raw) in enumerate(normalised_rows):
        reads = dict(
            (region, table[index][region].e164) for region in surviving)
        rejected = dict(
            (region, table[index][region].error)
            for region in regions
            if table[index][region].error is not None)
        values = set(reads.values())
        confirmed = next(iter(values)) if len(values) == 1 else None
        reports.append(
            RowReport(row_id, raw, international_flags[index],
                      reads, rejected, confirmed))

    return BatchReport(
        regions, normalised_rows, sorted(surviving), eliminated, reports)


def confirmed_numbers(rows, candidate_regions, require_valid=True):
    """Convenience wrapper returning only the uniquely confirmed E164 numbers."""
    return resolve_batch(rows, candidate_regions,
                         require_valid=require_valid).confirmed_numbers()


def candidate_numbers(rows, candidate_regions, require_valid=True):
    """Convenience wrapper returning row_id -> {region: E164} for open rows."""
    return resolve_batch(rows, candidate_regions,
                         require_valid=require_valid).candidate_numbers()


def explain(report):
    """Human-readable summary, including contradiction evidence."""
    lines = ["candidate regions: %s" % ", ".join(report.candidate_regions)]
    if report.contradiction:
        lines.append("no common explanation; contradictory evidence:")
        for region in sorted(report.eliminated):
            for row_id, reason in report.eliminated[region]:
                lines.append("  %s rejected by row %s (%s)"
                             % (region, row_id, reason))
        return "\n".join(lines)
    lines.append("surviving regions: %s"
                 % ", ".join(report.surviving_regions))
    for row_report in report.row_reports:
        if row_report.is_confirmed:
            lines.append("  %s: confirmed %s"
                         % (row_report.row_id, row_report.confirmed))
        else:
            options = ", ".join("%s->%s" % (region, e164)
                                for region, e164 in sorted(
                                    row_report.candidates.items()))
            lines.append("  %s: open candidates (%s)"
                         % (row_report.row_id, options))
    return "\n".join(lines)
