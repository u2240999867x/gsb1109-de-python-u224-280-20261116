"""Canonical, compressed representation of supplier-provided number ranges.

A supplier hands over *ranges* of numbers rather than individual contacts.
A range is stored as a closed integer interval over national significant
numbers (NSN) together with the country calling code and the fixed NSN
length, which is enough to preserve leading zero(s) without ever materialising
the (potentially millions of) individual numbers.

All operations work on interval lists; a whole catalogue therefore stays a
small, compressed description of the covered E.164 space.
"""
from .phonenumberutil import (NumberParseException, country_code_for_valid_region,
                              national_significant_number, parse)
from .util import u


class NumberRangeError(ValueError):
    """A number range could not be parsed or combined."""


class NumberRange(object):
    """A closed interval of fixed-length national significant numbers.

    The integer endpoints deliberately drop any leading zero, but the zero is
    not lost: ``nsn_length`` records the full NSN width and :meth:`nsn_start`
    / :meth:`nsn_end` render the zero-padded strings.
    """

    __slots__ = ("country_code", "nsn_length", "start", "end")

    def __init__(self, country_code, nsn_length, start, end):
        if not (0 <= start <= end < 10 ** nsn_length):
            raise NumberRangeError(
                u("invalid NSN interval [%d, %d] for length %d") % (start, end, nsn_length))
        self.country_code = int(country_code)
        self.nsn_length = int(nsn_length)
        self.start = int(start)
        self.end = int(end)

    @property
    def number_count(self):
        return self.end - self.start + 1

    def key(self):
        return (self.country_code, self.nsn_length)

    def _pad(self, value):
        return str(value).zfill(self.nsn_length)

    def nsn_start(self):
        return self._pad(self.start)

    def nsn_end(self):
        return self._pad(self.end)

    def e164_start(self):
        return u("+%d%s") % (self.country_code, self.nsn_start())

    def e164_end(self):
        return u("+%d%s") % (self.country_code, self.nsn_end())

    def with_bounds(self, start, end):
        return NumberRange(self.country_code, self.nsn_length, start, end)

    def to_dict(self):
        return {"country_code": self.country_code,
                "nsn_length": self.nsn_length,
                "nsn_start": self.nsn_start(),
                "nsn_end": self.nsn_end(),
                "e164_start": self.e164_start(),
                "e164_end": self.e164_end(),
                "number_count": self.number_count}

    @classmethod
    def from_dict(cls, data):
        nsn_length = int(data["nsn_length"])
        start = int(data["nsn_start"])
        end = int(data["nsn_end"])
        if len(data["nsn_start"]) != nsn_length or len(data["nsn_end"]) != nsn_length:
            raise NumberRangeError(u("stored NSN width disagrees with nsn_length: %r") % data)
        return cls(int(data["country_code"]), nsn_length, start, end)

    def __eq__(self, other):
        if not isinstance(other, NumberRange):
            return False
        return (self.country_code == other.country_code and
                self.nsn_length == other.nsn_length and
                self.start == other.start and
                self.end == other.end)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self):
        return u("NumberRange(%r, %d, %d, %d)") % (
            self.e164_start(), self.nsn_length, self.start, self.end)


def parse_endpoint(text, region):
    """Parse one range endpoint into a (country code, NSN length, NSN int) triple."""
    try:
        numobj = parse(text, region)
    except NumberParseException as exc:
        raise NumberRangeError(u("could not parse endpoint %r for region %r: %s") %
                               (text, region, exc))
    nsn = national_significant_number(numobj)
    return numobj.country_code, len(nsn), int(nsn)


def parse_range(start_text, end_text, region):
    """Parse two endpoint notations into a canonical :class:`NumberRange`.

    Different notations (``+49...``, ``00 49 ...``, national ``0...``) are
    accepted; they are compared in E.164 space so that equivalent spellings
    always describe the same international interval.
    """
    start_triple = parse_endpoint(start_text, region)
    end_triple = parse_endpoint(end_text, region)
    if start_triple[0] != end_triple[0]:
        raise NumberRangeError(
            u("range endpoints %r and %r have different country calling codes") %
            (start_text, end_text))
    if start_triple[1] != end_triple[1]:
        raise NumberRangeError(
            u("range endpoints %r and %r have different NSN lengths") % (start_text, end_text))
    country_code, nsn_length, start = start_triple
    _, _, end = end_triple
    if region is not None:
        expected_code = country_code_for_valid_region(region)
        if expected_code != country_code:
            raise NumberRangeError(
                u("range %r..%r uses calling code %d but region %r uses %d") %
                (start_text, end_text, country_code, region, expected_code))
    if start > end:
        raise NumberRangeError(
            u("range start %r is after end %r") % (start_text, end_text))
    return NumberRange(country_code, nsn_length, start, end)


def merge_ranges(ranges):
    """Merge overlapping or touching ranges; ranges must share each (code, width).

    Returns a new sorted list of maximal, disjoint :class:`NumberRange` objects.
    Ranges with different country codes or NSN lengths are never merged.
    """
    ordered = sorted(ranges, key=lambda rng: (rng.country_code, rng.nsn_length,
                                               rng.start, rng.end))
    merged = []
    for rng in ordered:
        if merged and merged[-1].key() == rng.key() and rng.start <= merged[-1].end + 1:
            if rng.end > merged[-1].end:
                previous = merged[-1]
                merged[-1] = previous.with_bounds(previous.start,
                                                  max(previous.end, rng.end))
        else:
            merged.append(rng)
    return merged


def subtract_ranges(ranges, removals):
    """Subtract every interval in ``removals`` from ``ranges``.

    Both arguments are lists of :class:`NumberRange`; only intervals sharing a
    country code and NSN length interact.  The result is a sorted, disjoint,
    maximal interval list.
    """
    result = []
    removal_groups = _group_by_key(removals)
    for rng in merge_ranges(ranges):
        cuts = removal_groups.get(rng.key(), [])
        cursor = rng.start
        for removed in cuts:
            cut_start = max(removed.start, cursor)
            if cut_start > rng.end:
                break
            if cut_start > cursor:
                result.append(rng.with_bounds(cursor, cut_start - 1))
            cursor = max(cursor, removed.end + 1)
            if cursor > rng.end:
                break
        if cursor <= rng.end:
            result.append(rng.with_bounds(cursor, rng.end))
    return merge_ranges(result)


def intersect_ranges(left, right):
    """Return the intersection of two interval lists."""
    right_groups = _group_by_key(right)
    result = []
    for rng in merge_ranges(left):
        for other in right_groups.get(rng.key(), []):
            start = max(rng.start, other.start)
            end = min(rng.end, other.end)
            if start <= end:
                result.append(rng.with_bounds(start, end))
    return merge_ranges(result)


def _group_by_key(ranges):
    groups = {}
    for rng in merge_ranges(ranges):
        groups.setdefault(rng.key(), []).append(rng)
    return groups


def total_count(ranges):
    """Return the total number of individual numbers covered by the ranges."""
    return sum(rng.number_count for rng in ranges)


def range_from_number(numobj):
    """Build a single-number range from a parsed :class:`PhoneNumber`."""
    nsn = national_significant_number(numobj)
    return NumberRange(numobj.country_code, len(nsn), int(nsn), int(nsn))


def ranges_from_numbers(numobjs):
    """Compress a sequence of parsed numbers into minimal covering ranges."""
    ranges = [range_from_number(numobj) for numobj in numobjs]
    return merge_ranges(ranges)
