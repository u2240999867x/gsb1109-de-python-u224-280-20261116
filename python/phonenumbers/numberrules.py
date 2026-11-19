"""Classify whole number ranges against a region's metadata rules.

Rather than testing the first and last number of a supplier range (which
proves nothing about the numbers in between), each range is split by the
same regular expressions that back :func:`phonenumbers.is_valid_number`.
Pieces where every number matches a concrete number-type rule are marked
``VALID``; pieces that only match the general description are ``PENDING``
manual verification; pieces outside even the general description are
``INVALID``.
"""
from .phonemetadata import PhoneMetadata, REGION_CODE_FOR_NON_GEO_ENTITY
from .phonenumberutil import PhoneNumberType
from .range_automaton import DigitRangeDFA
from .util import u
from .numberrange import NumberRange, NumberRangeError


RANGE_VALID = "valid"
RANGE_PENDING = "pending"
RANGE_INVALID = "invalid"
RANGE_STATUSES = (RANGE_VALID, RANGE_PENDING, RANGE_INVALID)


class ClassifiedRange(object):
    """A :class:`NumberRange` tagged with the outcome of rule classification."""

    __slots__ = ("number_range", "status", "number_type")

    def __init__(self, number_range, status, number_type=None):
        if status not in RANGE_STATUSES:
            raise NumberRangeError(u("unknown range status %r") % status)
        self.number_range = number_range
        self.status = status
        self.number_type = number_type

    @property
    def number_count(self):
        return self.number_range.number_count

    def to_dict(self):
        data = self.number_range.to_dict()
        data["status"] = self.status
        data["number_type"] = self.number_type
        return data

    @classmethod
    def from_dict(cls, data):
        number_type = data.get("number_type")
        if number_type is not None:
            number_type = int(number_type)
        return cls(NumberRange.from_dict(data), data["status"], number_type)

    def __eq__(self, other):
        if not isinstance(other, ClassifiedRange):
            return False
        return (self.number_range == other.number_range and
                self.status == other.status and
                self.number_type == other.number_type)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __repr__(self):
        return u("ClassifiedRange(%r, %r, %r)") % (
            self.number_range, self.status, self.number_type)


# Number-type descriptors in the precedence order used by the library's
# _number_type_helper; fixed-line and mobile need combined handling and are
# therefore excluded from this list.
_TYPED_DESCRIPTORS = (
    (PhoneNumberType.PREMIUM_RATE, "premium_rate"),
    (PhoneNumberType.TOLL_FREE, "toll_free"),
    (PhoneNumberType.SHARED_COST, "shared_cost"),
    (PhoneNumberType.VOIP, "voip"),
    (PhoneNumberType.PERSONAL_NUMBER, "personal_number"),
    (PhoneNumberType.PAGER, "pager"),
    (PhoneNumberType.UAN, "uan"),
    (PhoneNumberType.VOICEMAIL, "voicemail"),
)


class RangeRuleClassifier(object):
    """Splits ranges into valid, pending and invalid pieces for one region.

    The region is explicit: either an ISO 3166-1 alpha-2 code such as
    ``"DE"``, or ``REGION_CODE_FOR_NON_GEO_ENTITY`` (``"001"``) together with
    a non-geographical country calling code.  All classification uses the
    metadata version bundled with this package.
    """

    def __init__(self, region_code):
        self.region_code = region_code.upper()
        if self.region_code == REGION_CODE_FOR_NON_GEO_ENTITY:
            self._metadata = None
            self._non_geo = True
        else:
            self._metadata = PhoneMetadata.metadata_for_region(self.region_code, None)
            self._non_geo = False
            if self._metadata is None:
                raise NumberRangeError(u("unknown region code %r") % region_code)
        self._dfa_cache = {}

    def metadata_for_code(self, country_code):
        if self._non_geo:
            metadata = PhoneMetadata.metadata_for_nongeo_region(country_code, None)
            if metadata is None:
                raise NumberRangeError(
                    u("no non-geographical metadata for calling code %d") % country_code)
            return metadata
        return self._metadata

    def _dfa_for(self, descriptor):
        if descriptor is None or descriptor.national_number_pattern is None:
            return None
        cached = self._dfa_cache.get(descriptor.national_number_pattern)
        if cached is None:
            cached = DigitRangeDFA(descriptor.national_number_pattern)
            self._dfa_cache[descriptor.national_number_pattern] = cached
        return cached

    def classify(self, number_range):
        """Return maximal, disjoint ClassifiedRanges covering ``number_range``."""
        metadata = self.metadata_for_code(number_range.country_code)
        if metadata.country_code != number_range.country_code:
            raise NumberRangeError(
                u("calling code %d does not belong to region %r") %
                (number_range.country_code, self.region_code))
        length = number_range.nsn_length
        invalid = []
        # Step 1: the general description decides which numbers are even possible.
        general_pieces, outside = self._match_descriptor(
            number_range, metadata.general_desc)
        for start, end in outside:
            invalid.append(ClassifiedRange(number_range.with_bounds(start, end),
                                           RANGE_INVALID))
        results = []
        candidates = list(general_pieces)
        # Step 2: concrete number types, in library precedence order.
        for number_type, attr_name in _TYPED_DESCRIPTORS:
            descriptor = getattr(metadata, attr_name)
            candidates = self._consume_descriptor(candidates, number_range, descriptor,
                                                  length, number_type, results)
        # Step 3: fixed-line versus mobile, including their shared overlap.
        candidates = self._consume_fixed_mobile(candidates, number_range, metadata,
                                                 length, results)
        # Step 4: possible but not provably valid against any specific rule.
        for start, end in candidates:
            results.append(ClassifiedRange(number_range.with_bounds(start, end),
                                           RANGE_PENDING))
        results.extend(invalid)
        return _merge_classified(sorted(results, key=lambda item: item.number_range.start))

    def _consume_descriptor(self, candidates, template, descriptor, length,
                            number_type, results):
        matched_intervals, _ = self._match_descriptor(
            template, descriptor, sub_intervals=candidates, length=length)
        remaining = _subtract_intervals(candidates, matched_intervals)
        for start, end in matched_intervals:
            results.append(ClassifiedRange(template.with_bounds(start, end),
                                           RANGE_VALID, number_type))
        return remaining

    def _consume_fixed_mobile(self, candidates, template, metadata, length, results):
        fixed_line = metadata.fixed_line
        mobile = metadata.mobile
        fixed_intervals, _ = self._match_descriptor(
            template, fixed_line, sub_intervals=candidates, length=length)
        mobile_intervals, _ = self._match_descriptor(
            template, mobile, sub_intervals=candidates, length=length)
        if metadata.same_mobile_and_fixed_line_pattern:
            shared_intervals = fixed_intervals
            fixed_only = []
            mobile_only = []
        else:
            shared_intervals = _intersect_intervals(fixed_intervals, mobile_intervals)
            fixed_only = _subtract_intervals(fixed_intervals, mobile_intervals)
            mobile_only = _subtract_intervals(mobile_intervals, fixed_intervals)
        for start, end in shared_intervals:
            results.append(ClassifiedRange(template.with_bounds(start, end),
                                           RANGE_VALID,
                                           PhoneNumberType.FIXED_LINE_OR_MOBILE))
        for start, end in fixed_only:
            results.append(ClassifiedRange(template.with_bounds(start, end),
                                           RANGE_VALID, PhoneNumberType.FIXED_LINE))
        for start, end in mobile_only:
            results.append(ClassifiedRange(template.with_bounds(start, end),
                                           RANGE_VALID, PhoneNumberType.MOBILE))
        consumed = fixed_intervals + mobile_intervals
        return _subtract_intervals(candidates, consumed)

    def _match_descriptor(self, template, descriptor, sub_intervals=None, length=None):
        """Split template intervals into (matching, non-matching) integer pieces."""
        if length is None:
            length = template.nsn_length
        if sub_intervals is None:
            sub_intervals = [(template.start, template.end)]
        dfa = self._dfa_for(descriptor)
        if dfa is None:
            return [], list(sub_intervals)
        allowed_lengths = getattr(descriptor, "possible_length", ())
        if allowed_lengths and length not in allowed_lengths:
            return [], list(sub_intervals)
        matched = []
        unmatched = []
        for start, end in sub_intervals:
            piece_matched, piece_unmatched = dfa.classify_interval(length, start, end)
            matched.extend(piece_matched)
            unmatched.extend(piece_unmatched)
        return _merge_intervals(matched), _merge_intervals(unmatched)


def _merge_intervals(intervals):
    if not intervals:
        return []
    intervals = sorted(intervals)
    merged = [list(intervals[0])]
    for start, end in intervals[1:]:
        if start <= merged[-1][1] + 1:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _subtract_intervals(base, removals):
    result = []
    for start, end in _merge_intervals(base):
        cursor = start
        for cut_start, cut_end in _merge_intervals(removals):
            cut_start = max(cut_start, cursor)
            if cut_start > end:
                break
            if cut_start > cursor:
                result.append((cursor, cut_start - 1))
            cursor = max(cursor, cut_end + 1)
            if cursor > end:
                break
        if cursor <= end:
            result.append((cursor, end))
    return _merge_intervals(result)


def _intersect_intervals(left, right):
    result = []
    for left_start, left_end in _merge_intervals(left):
        for right_start, right_end in _merge_intervals(right):
            start = max(left_start, right_start)
            end = min(left_end, right_end)
            if start <= end:
                result.append((start, end))
    return _merge_intervals(result)


def _merge_classified(classified):
    merged = []
    for item in classified:
        if (merged and
                merged[-1].status == item.status and
                merged[-1].number_type == item.number_type and
                merged[-1].number_range.key() == item.number_range.key() and
                item.number_range.start == merged[-1].number_range.end + 1):
            previous = merged[-1].number_range
            merged[-1] = ClassifiedRange(
                previous.with_bounds(previous.start, item.number_range.end),
                item.status, item.number_type)
        else:
            merged.append(item)
    return merged
