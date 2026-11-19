"""Offline classification of mixed phone-number collections.

A collection directory often mixes several kinds of diallable strings:

 - ordinary full phone numbers (national or international format),
 - regional short codes (emergency, short services, ...),
 - enterprise extensions (the part dialled after a call is connected), and
 - bare local digits whose region is not known.

This module adds a classification exit point that only emits conclusions
that are backed by evidence in the bundled metadata.  In particular:

 - the length of a short number never counts as evidence that it is locally
   valid (a length/pattern match is treated as a possibility, not a category),
 - matching an emergency pattern only proves emergency reachability, never
   that the string is a diallable ordinary service,
 - when no region is available, every region-specific candidate conclusion is
   retained and no area code is ever invented to turn a short string into a
   full number,
 - identical digits in different regions/domains are classified independently,
   and an extension is kept distinct from a short number of the same digits,
 - everything is decided offline from the locally available metadata; no
   network lookup is performed or required.
"""
# Copyright (C) 2026 The Libphonenumber Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from .util import UnicodeMixin, u, force_unicode
from .phonenumber import PhoneNumber
from .phonenumberutil import (UNKNOWN_REGION, NumberParseException,
                              ValidationResult, PhoneNumberType,
                              _maybe_strip_extension,
                              can_be_internationally_dialled,
                              is_possible_number_with_reason,
                              is_valid_number_for_region,
                              normalize_digits_only,
                              number_type,
                              parse,
                              region_codes_for_country_code)
from .shortnumberinfo import (ShortNumberCost,
                              _region_dialing_from_matches_number,
                              connects_to_emergency_number,
                              expected_cost_for_region,
                              is_carrier_specific_for_region,
                              is_emergency_number,
                              is_possible_short_number_for_region,
                              is_sms_service_for_region,
                              is_valid_short_number_for_region)


# Number categories.  These describe what the digits are, not whether they can
# be dialled from the current terminal; see NumberReach for the latter.
class NumberCategory(object):
    """Evidence-backed categories a classified number may belong to."""
    # A full-length ordinary phone number (fixed line, mobile, toll free, ...).
    ORDINARY_NUMBER = u("ORDINARY_NUMBER")
    # A regional short code, such as an emergency or service number.
    REGIONAL_SHORT_CODE = u("REGIONAL_SHORT_CODE")
    # An enterprise extension, only meaningful inside its organization domain.
    ENTERPRISE_EXTENSION = u("ENTERPRISE_EXTENSION")
    # No metadata-backed category could be established for the digits.
    UNKNOWN = u("UNKNOWN")


# Where a classified number can actually be dialled from.  Every value is
# derived from a metadata check, never from the length of the digits.
class NumberReach(object):
    """Evidence-backed dialling ranges for a classified number."""
    # Diallable from outside the region of the number as well as inside it.
    INTERNATIONAL = u("INTERNATIONAL")
    # Only diallable inside the region of the number.
    REGIONAL = u("REGIONAL")
    # Only diallable inside the enterprise that owns the extension.
    ENTERPRISE_ONLY = u("ENTERPRISE_ONLY")
    # Emergency services only; this does not make the string an ordinary
    # diallable service number.
    EMERGENCY_ONLY = u("EMERGENCY_ONLY")
    # No metadata-backed dialling range could be established.
    UNKNOWN = u("UNKNOWN")


# Mapping of short-number cost categories to human-readable labels.  Kept as
# data rather than exposing the integer ShortNumberCost values directly.
_COST_LABELS = {
    ShortNumberCost.TOLL_FREE: u("TOLL_FREE"),
    ShortNumberCost.STANDARD_RATE: u("STANDARD_RATE"),
    ShortNumberCost.PREMIUM_RATE: u("PREMIUM_RATE"),
    ShortNumberCost.UNKNOWN_COST: u("UNKNOWN_COST"),
}


class NumberClassification(UnicodeMixin, object):
    """The evidence-backed classification of one input string.

    Attributes:
    raw_input -- the string that was classified.
    organization_domain -- the enterprise domain the string was attributed
                  to, or None when no domain was supplied.
    region -- the region the conclusions were evaluated for (an ISO 3166-1
                  alpha-2 code), or None when the region was not known.
    roaming -- whether the terminal was said to be roaming.
    conclusions -- a tuple of NumberConclusion objects, one per region/domain
                  for which evidence was found.  When the region is known this
                  holds at most one conclusion; when it is unknown it holds one
                  candidate conclusion per region that supplied evidence.
    """

    def __init__(self, raw_input, organization_domain, region, roaming, conclusions):
        self.raw_input = force_unicode(raw_input)
        self.organization_domain = (force_unicode(organization_domain)
                                    if organization_domain is not None else None)
        self.region = force_unicode(region) if region is not None else None
        self.roaming = bool(roaming)
        self.conclusions = tuple(conclusions)

    @property
    def categories(self):
        """The set of distinct evidence-backed categories across conclusions."""
        return tuple(dict.fromkeys(conclusion.category for conclusion in self.conclusions))

    @property
    def reaches(self):
        """The set of distinct evidence-backed dialling ranges across conclusions."""
        return tuple(dict.fromkeys(conclusion.reach for conclusion in self.conclusions))

    def __unicode__(self):
        return (u("NumberClassification(raw_input=%r, region=%r, roaming=%r, conclusions=%r)") %
                (self.raw_input, self.region, self.roaming,
                 [conclusion.__unicode__() for conclusion in self.conclusions]))

    def __eq__(self, other):
        if not isinstance(other, NumberClassification):
            return False
        return (self.raw_input == other.raw_input and
                self.organization_domain == other.organization_domain and
                self.region == other.region and
                self.roaming == other.roaming and
                self.conclusions == other.conclusions)

    def __ne__(self, other):
        return not self.__eq__(other)

    def to_dict(self):
        """Plain-data representation, suitable for offline storage/export.

        The round trip is completed by NumberClassification.from_dict.
        """
        return {
            u("raw_input"): self.raw_input,
            u("organization_domain"): self.organization_domain,
            u("region"): self.region,
            u("roaming"): self.roaming,
            u("conclusions"): [conclusion.to_dict()
                               for conclusion in self.conclusions],
        }

    @classmethod
    def from_dict(cls, data):
        """Rebuild a NumberClassification previously produced by to_dict."""
        return cls(data[u("raw_input")], data[u("organization_domain")],
                   data[u("region")], data[u("roaming")],
                   [NumberConclusion.from_dict(conclusion)
                    for conclusion in data[u("conclusions")]])


class NumberConclusion(UnicodeMixin, object):
    """One evidence-backed conclusion, scoped to a region and optional domain.

    Attributes:
    number -- the parsed PhoneNumber carrying the classified national number,
              or None when the digits could not be parsed.
    category -- a NumberCategory value.
    reach -- a NumberReach value.
    region -- the region the evidence came from (an ISO 3166-1 alpha-2 code);
              None only for an UNKNOWN conclusion without a region.
    organization_domain -- the enterprise domain for an extension conclusion,
              otherwise None.
    extension -- the extension digits for an ENTERPRISE_EXTENSION conclusion,
              otherwise None.
    evidence -- a tuple of short labels naming the metadata facts that back
              the conclusion, e.g. ("valid_short_number", "emergency").
    cost -- the cost label of a regional short code, or None.
    """

    def __init__(self, number, category, reach, region, organization_domain,
                 extension, evidence, cost=None):
        self.number = number
        self.category = category
        self.reach = reach
        self.region = force_unicode(region) if region is not None else None
        self.organization_domain = (force_unicode(organization_domain)
                                    if organization_domain is not None else None)
        self.extension = force_unicode(extension) if extension is not None else None
        self.evidence = tuple(evidence)
        self.cost = force_unicode(cost) if cost is not None else None

    def __unicode__(self):
        return (u("NumberConclusion(region=%r, category=%s, reach=%s, evidence=%r)") %
                (self.region, self.category, self.reach, list(self.evidence)))

    def __eq__(self, other):
        if not isinstance(other, NumberConclusion):
            return False
        return (self.number == other.number and
                self.category == other.category and
                self.reach == other.reach and
                self.region == other.region and
                self.organization_domain == other.organization_domain and
                self.extension == other.extension and
                self.evidence == other.evidence and
                self.cost == other.cost)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash((self.category, self.reach, self.region,
                     self.organization_domain, self.extension, self.evidence, self.cost))

    def to_dict(self):
        """Plain-data representation, suitable for offline storage/export."""
        if self.number is None:
            number_data = None
        else:
            number_data = {
                u("country_code"): self.number.country_code,
                u("national_number"): self.number.national_number,
                u("extension"): self.number.extension,
                u("italian_leading_zero"): self.number.italian_leading_zero,
                u("number_of_leading_zeros"): self.number.number_of_leading_zeros,
                u("preferred_domestic_carrier_code"): self.number.preferred_domestic_carrier_code,
            }
        return {
            u("number"): number_data,
            u("category"): self.category,
            u("reach"): self.reach,
            u("region"): self.region,
            u("organization_domain"): self.organization_domain,
            u("extension"): self.extension,
            u("evidence"): list(self.evidence),
            u("cost"): self.cost,
        }

    @classmethod
    def from_dict(cls, data):
        """Rebuild a NumberConclusion previously produced by to_dict."""
        number_data = data[u("number")]
        if number_data is None:
            number = None
        else:
            number = PhoneNumber(
                country_code=number_data[u("country_code")],
                national_number=number_data[u("national_number")],
                extension=number_data[u("extension")],
                italian_leading_zero=number_data[u("italian_leading_zero")],
                number_of_leading_zeros=number_data[u("number_of_leading_zeros")],
                preferred_domestic_carrier_code=number_data[u("preferred_domestic_carrier_code")])
        return cls(number, data[u("category")], data[u("reach")], data[u("region")],
                   data[u("organization_domain")], data[u("extension")],
                   tuple(data[u("evidence")]), data[u("cost")])


def classify_number(number, region=None, roaming=False, organization_domain=None):
    """Classify a single diallable string using only offline metadata.

    Arguments:
    number -- the raw digits/string as collected (may contain formatting and an
              extension marker such as "ext" or "x").
    region -- the region the terminal is in (an ISO 3166-1 alpha-2 code such as
              "US"), or None when the region is not known.
    roaming -- True if the terminal is roaming outside its home region.  This is
              recorded on the result for context; the bundled metadata contains
              no roaming-specific dialling rules, so roaming never upgrades a
              region/enterprise-scoped reach into a wider one.
    organization_domain -- optional identifier of the enterprise (PBX) domain
              the collection belongs to.  Extensions are only meaningful inside
              such a domain.

    Returns a NumberClassification whose conclusions are all backed by
    metadata.  When region is None the region-specific candidates are retained
    instead of guessed, and a bare short string is never padded with an area
    code.  All checks run offline against the locally bundled metadata.
    """
    raw_input = force_unicode(number)
    if raw_input is None:
        raise NumberParseException(NumberParseException.NOT_A_NUMBER,
                                   "The phone number supplied was None.")
    if region is not None and region == UNKNOWN_REGION:
        # UNKNOWN_REGION ("ZZ") is an internal sentinel, not a usable region.
        region = None
    else:
        region = region.upper() if region is not None else None
    conclusions = _classify(raw_input, region, organization_domain)
    return NumberClassification(raw_input, organization_domain, region,
                                roaming, conclusions)


def classify_numbers(numbers, region=None, roaming=False, organization_domain=None):
    """Classify an iterable of mixed diallable strings.

    See classify_number for the meaning of region, roaming and
    organization_domain.  Identical digits collected for different regions or
    organization domains are classified independently by calling this method
    separately with the matching context.  Returns a tuple of
    NumberClassification objects, in input order.
    """
    return tuple(classify_number(number, region=region, roaming=roaming,
                                 organization_domain=organization_domain)
                 for number in numbers)


def _classify(raw_input, region, organization_domain):
    extension_digits, base_input = _split_extension(raw_input)

    extension_conclusions = ()
    if extension_digits is not None:
        if organization_domain is not None:
            # Inside an enterprise domain the trailing digits are an extension,
            # distinct from a regional short code with the same digits.
            extension_conclusions = (NumberConclusion(
                None, NumberCategory.ENTERPRISE_EXTENSION,
                NumberReach.ENTERPRISE_ONLY, region, organization_domain,
                extension_digits, (u("extension_marker"),)),)
        else:
            # No domain means the extension cannot be attributed; retain it as
            # an unclassified candidate rather than silently dropping it or
            # turning it into a short code.
            extension_conclusions = (NumberConclusion(
                None, NumberCategory.UNKNOWN, NumberReach.UNKNOWN,
                region, None, extension_digits,
                (u("extension_without_domain"),)),)

    base_conclusions = _classify_base(base_input, region)
    return extension_conclusions + base_conclusions


def _split_extension(raw_input):
    """Return (extension digits or None, input without the extension)."""
    extension, base_input = _maybe_strip_extension(raw_input)
    if extension == u(""):
        return None, raw_input
    return normalize_digits_only(extension), base_input


def _classify_base(base_input, region):
    parsed = _try_parse(base_input, region)
    if parsed is None:
        if region is None:
            # Parsing needs a region for national-format input.  With no region
            # the bare digits are kept as an UNKNOWN candidate instead of
            # inventing a country/area code.  International ("+...") input
            # parses without a region, so it does not reach this branch.
            evidence = (u("no_region_no_international_prefix"),)
        else:
            # A region was known but the string is not a viable phone number
            # even for that region; nothing stronger can be claimed.
            evidence = (u("not_a_viable_number"),)
        return (NumberConclusion(None, NumberCategory.UNKNOWN, NumberReach.UNKNOWN,
                                 region, None, None, evidence),)

    if region is None:
        # The country calling code identifies the candidate region(s); each is
        # evaluated independently and every evidence-backed result is kept.
        # Regions for which no metadata evidence exists are not emitted, so a
        # shared calling code does not flood the result with unknowns.
        candidate_regions = region_codes_for_country_code(parsed.country_code)
        conclusions = []
        for candidate_region in candidate_regions:
            conclusion = _classify_for_region(parsed, candidate_region, base_input)
            if conclusion is not None and conclusion.category != NumberCategory.UNKNOWN:
                conclusions.append(conclusion)
        if len(conclusions) == 0:
            return (NumberConclusion(parsed, NumberCategory.UNKNOWN, NumberReach.UNKNOWN,
                                     None, None, None,
                                     (u("no_evidence_in_any_candidate_region"),)),)
        return tuple(conclusions)

    conclusion = _classify_for_region(parsed, region, base_input)
    if conclusion is None:
        return (NumberConclusion(parsed, NumberCategory.UNKNOWN, NumberReach.UNKNOWN,
                                 region, None, None, (u("no_metadata_match"),)),)
    return (conclusion,)


def _try_parse(base_input, region):
    try:
        if region is None:
            return parse(base_input, UNKNOWN_REGION)
        return parse(base_input, region)
    except NumberParseException:
        return None


def _classify_for_region(numobj, region, dial_string):
    """Build the single evidence-backed conclusion for numobj in region.

    dial_string is the number exactly as dialled (extension already removed);
    it matters for emergency detection because a leading "+" is not believed
    to reach emergency services.

    Returns None when no metadata-backed category applies at all, or a
    conclusion (including an UNKNOWN one that carries weak evidence).
    """
    if not _region_dialing_from_matches_number(numobj, region):
        return None

    # Short codes first: a short code can also be a possible-length ordinary
    # number, but the explicit short metadata is the stronger evidence and
    # keeps emergency numbers out of the ordinary-service category.
    short_conclusion = _classify_short_code(numobj, region, dial_string)
    if short_conclusion is not None and short_conclusion.category != NumberCategory.UNKNOWN:
        return short_conclusion

    ordinary_conclusion = _classify_ordinary(numobj, region)
    if short_conclusion is None:
        return ordinary_conclusion
    # Only weak short-code evidence existed (length match without a valid
    # pattern); keep it visible on the conclusion instead of silently
    # discarding the candidate.
    return _with_extra_evidence(ordinary_conclusion, short_conclusion.evidence)


def _with_extra_evidence(conclusion, extra_evidence):
    merged = tuple(dict.fromkeys(tuple(conclusion.evidence) + tuple(extra_evidence)))
    return NumberConclusion(conclusion.number, conclusion.category, conclusion.reach,
                            conclusion.region, conclusion.organization_domain,
                            conclusion.extension, merged, conclusion.cost)


def _classify_short_code(numobj, region, dial_string):
    # Emergency checks use the string exactly as dialled on purpose: the
    # library does not believe a country-code-prefixed form such as "+1911"
    # reaches emergency services, while the bare "911" does.
    emergency = is_emergency_number(dial_string, region)
    connects_to_emergency = connects_to_emergency_number(dial_string, region)
    possible_short = is_possible_short_number_for_region(numobj, region)
    valid_short = is_valid_short_number_for_region(numobj, region)
    if not (emergency or connects_to_emergency or possible_short):
        return None

    evidence = []
    if possible_short:
        # A length match is only weak evidence and never, on its own, proves
        # the short code is locally valid.
        evidence.append(u("possible_short_number_length"))
    if connects_to_emergency and not emergency:
        # Extra digits may still route to emergency services in this region;
        # this does not make the string an ordinary diallable service.
        evidence.append(u("connects_to_emergency_prefix"))
    if emergency:
        evidence.append(u("emergency"))
    if valid_short:
        evidence.append(u("valid_short_number"))
    if is_carrier_specific_for_region(numobj, region):
        evidence.append(u("carrier_specific"))
    if is_sms_service_for_region(numobj, region):
        evidence.append(u("sms_service"))

    cost = _COST_LABELS[expected_cost_for_region(numobj, region)]

    if emergency:
        # Emergency matching only proves emergency reachability; it never makes
        # the string a diallable ordinary service, even when it is also a
        # valid short code.
        return NumberConclusion(numobj, NumberCategory.REGIONAL_SHORT_CODE,
                                NumberReach.EMERGENCY_ONLY, region, None,
                                None, evidence, cost)
    if not valid_short:
        # Length or emergency-prefix matching alone does not prove the short
        # code is locally valid as a service, so no category or dialling range
        # is claimed; the evidence is retained on an UNKNOWN conclusion.
        return NumberConclusion(numobj, NumberCategory.UNKNOWN, NumberReach.UNKNOWN,
                                region, None, None, evidence, cost)
    # Valid, non-emergency short codes are regional services and are not
    # internationally diallable.
    return NumberConclusion(numobj, NumberCategory.REGIONAL_SHORT_CODE,
                            NumberReach.REGIONAL, region, None,
                            None, evidence, cost)


def _classify_ordinary(numobj, region):
    if not is_valid_number_for_region(numobj, region):
        # Record the lenient length outcome as evidence, but do not claim a
        # category or reach from length alone.
        reason = is_possible_number_with_reason(numobj)
        if reason == ValidationResult.IS_POSSIBLE_LOCAL_ONLY:
            evidence = (u("possible_local_only_length"),)
        else:
            evidence = (u("length_not_valid_for_region"),)
        return NumberConclusion(numobj, NumberCategory.UNKNOWN, NumberReach.UNKNOWN,
                                region, None, None, evidence)

    evidence = [u("valid_number_pattern")]
    number_type_label = _number_type_label(numobj)
    if number_type_label is not None:
        evidence.append(number_type_label)
    if can_be_internationally_dialled(numobj):
        evidence.append(u("internationally_diallable"))
        reach = NumberReach.INTERNATIONAL
    else:
        evidence.append(u("no_international_dialling"))
        reach = NumberReach.REGIONAL
    return NumberConclusion(numobj, NumberCategory.ORDINARY_NUMBER, reach,
                            region, None, None, evidence)


def _number_type_label(numobj):
    numtype = number_type(numobj)
    mapping = {
        PhoneNumberType.FIXED_LINE: u("fixed_line"),
        PhoneNumberType.MOBILE: u("mobile"),
        PhoneNumberType.FIXED_LINE_OR_MOBILE: u("fixed_line_or_mobile"),
        PhoneNumberType.TOLL_FREE: u("toll_free"),
        PhoneNumberType.PREMIUM_RATE: u("premium_rate"),
        PhoneNumberType.SHARED_COST: u("shared_cost"),
        PhoneNumberType.VOIP: u("voip"),
        PhoneNumberType.PERSONAL_NUMBER: u("personal_number"),
        PhoneNumberType.PAGER: u("pager"),
        PhoneNumberType.UAN: u("uan"),
        PhoneNumberType.VOICEMAIL: u("voicemail"),
    }
    return mapping.get(numtype)
