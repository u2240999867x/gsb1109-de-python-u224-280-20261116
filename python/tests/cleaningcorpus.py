"""Metadata-driven regression corpus for the number cleaning entry point.

The "cleaning entry" is phonenumbers.parse(): the point where raw user input
is turned into a PhoneNumber and classified.  This module builds a
deterministic corpus of raw inputs straight from the metadata in use
(patterns, possible lengths, national prefixes), never by copying the
metadata's example numbers.  Each sample is labelled with the rule branch it
exercises:

  overlap:fixed_line+mobile  matches both the fixed-line and the mobile
                             description (classified FIXED_LINE_OR_MOBILE)
  length:general:below_min   one digit shorter than the shortest possible length
  length:general:min         shortest possible length
  length:general:max         longest possible length
  length:general:above_max   one digit longer than the longest possible length
  leading_zero:significant   national significant numbers starting with 0
  type:<desc>                numbers classified as one specific number type

Expectations (parseable / possible / valid / number type / leading zero) come
from a metadata oracle that mirrors the library's own classification rules, so
the corpus fails when the library and its metadata disagree.  Generation is
driven by a sample budget and is fully deterministic: the same seed and the
same library version rebuild exactly the same samples.  Branches that the
budget did not reach, or for which no sample could be generated, are listed in
Corpus.uncovered together with the reason.

shrink_sample() minimises a failing sample while preserving the exact
classification difference that made it fail, so a shrunk failure still
triggers the original discrepancy.

Regenerate the checked-in snapshot after a metadata upgrade with:

    python -m tests.cleaningcorpus > tests/cleaningcorpus_snapshot.json
"""

import json
import random
import re
from collections import namedtuple

import phonenumbers
from phonenumbers import PhoneMetadata, PhoneNumberFormat, PhoneNumberType
from phonenumbers import phonenumberutil
from phonenumbers.util import u, unicod

try:  # Python 3.11+
    from re import _parser as _re_parser
    from re import _constants as _re_constants
except ImportError:  # pragma: no cover
    import sre_parse as _re_parser
    import sre_constants as _re_constants

_MAX_NSN_LENGTH = 17  # mirrors phonenumberutil._MAX_LENGTH_FOR_NSN
_REPEAT_WINDOW = 16
_RANGE_CAP = 64
_DIGITS = u('0123456789')

BRANCH_OVERLAP = u('overlap:fixed_line+mobile')
BRANCH_LEADING_ZERO = u('leading_zero:significant')
BRANCH_LENGTH_PREFIX = u('length:general:')
BRANCH_TYPE_PREFIX = u('type:')

REASON_BUDGET = u('budget-exhausted')
REASON_INFEASIBLE = u('generation-infeasible')
REASON_UNSUPPORTED = u('unsupported-pattern')

# Number type descriptions, in the order the library checks them.
_TYPE_DESCS = ('premium_rate', 'toll_free', 'shared_cost', 'voip',
               'personal_number', 'pager', 'uan', 'voicemail',
               'fixed_line', 'mobile')

Classification = namedtuple('Classification',
                            ('parseable', 'possible', 'valid', 'number_type',
                             'italian_leading_zero'))

Sample = namedtuple('Sample', ('region', 'branch', 'raw_input',
                               'national_number', 'expected'))

SNAPSHOT_SEED = 20261116
SNAPSHOT_BUDGET = 600
DEFAULT_FOCUS_REGIONS = (u('US'), u('DE'), u('IT'))


class _UnsupportedPatternError(Exception):
    pass


class _InfeasibleTargetError(Exception):
    pass


def _const(name):
    return getattr(_re_constants, name, None)

_REPEAT_OPS = tuple(c for c in (_const('MAX_REPEAT'), _const('MIN_REPEAT'),
                                _const('REPEAT'), _const('POSSESSIVE_REPEAT'))
                    if c is not None)
_ANY_ALL = _const('ANY_ALL')


class _PatternSampler(object):
    """Deterministically generates strings matching a metadata regex.

    All randomness flows through the random.Random instance handed in, so a
    fixed seed reproduces the exact same sequence of generated strings.
    generate(target_length=N) produces a match of exactly N characters by
    tracking the minimum/maximum length of every subexpression.
    """

    def __init__(self, pattern, rng):
        self._pattern = pattern
        self._parsed = _re_parser.parse(pattern)
        self._rng = rng
        self._range_cache = {}

    def generate(self, target_length=None):
        if target_length is not None:
            lo, hi = self._seq_range(self._parsed.data)
            if target_length < lo or target_length > hi:
                raise _InfeasibleTargetError(
                    u('%d not in [%d, %d]') % (target_length, lo, hi))
        return self._gen_seq(self._parsed.data, target_length)

    def leading_chars(self):
        """Characters that a match of the pattern may start with."""
        return sorted(self._leading_chars(self._parsed.data))

    def _seq_range(self, ops):
        # Only call with lists owned by the parsed pattern tree: the cache is
        # keyed by object identity, so the list must outlive the sampler.
        key = id(ops)
        cached = self._range_cache.get(key)
        if cached is None:
            cached = self._seq_range_from(ops, 0)
            self._range_cache[key] = cached
        return cached

    def _seq_range_from(self, ops, start):
        lo = hi = 0
        for j in range(start, len(ops)):
            nlo, nhi = self._node_range(ops[j][0], ops[j][1])
            lo += nlo
            hi = min(hi + nhi, _RANGE_CAP)
        return (lo, hi)

    def _node_range(self, op, arg):
        C = _re_constants
        if op in (C.LITERAL, C.NOT_LITERAL, C.IN, C.ANY, _ANY_ALL, C.CATEGORY):
            return (1, 1)
        if op is C.AT:
            return (0, 0)
        if op is C.SUBPATTERN:
            return self._seq_range(arg[-1])
        if op is C.BRANCH:
            los = []
            his = []
            for alt in arg[-1]:
                alo, ahi = self._seq_range(alt)
                los.append(alo)
                his.append(ahi)
            return (min(los), min(max(his), _RANGE_CAP))
        if op in _REPEAT_OPS:
            mn, mx, sub = arg[0], arg[1], arg[-1]
            slo, shi = self._seq_range(sub)
            return (mn * slo, min(min(mx, _RANGE_CAP) * shi, _RANGE_CAP))
        raise _UnsupportedPatternError(unicod(op))

    def _gen_seq(self, ops, target):
        out = []
        produced = 0
        for i in range(len(ops)):
            op, arg = ops[i]
            if target is None:
                piece = self._gen_node(op, arg, None)
            else:
                rest_lo, rest_hi = self._seq_range_from(ops, i + 1)
                node_lo, node_hi = self._node_range(op, arg)
                lo_k = max(node_lo, target - produced - rest_hi)
                hi_k = min(node_hi, target - produced - rest_lo)
                ks = list(range(lo_k, hi_k + 1))
                self._rng.shuffle(ks)
                piece = None
                for k in ks:
                    try:
                        piece = self._gen_node(op, arg, k)
                        break
                    except _InfeasibleTargetError:
                        continue
                if piece is None:
                    raise _InfeasibleTargetError(unicod(self._pattern))
            out.append(piece)
            produced += len(piece)
        return u('').join(out)

    def _gen_node(self, op, arg, k):
        C = _re_constants
        if op in (C.LITERAL, C.NOT_LITERAL, C.IN, C.CATEGORY, C.ANY, _ANY_ALL):
            if k is not None and k != 1:
                raise _InfeasibleTargetError(unicod(self._pattern))
            return self._gen_atom(op, arg)
        if op is C.AT:
            if k not in (None, 0):
                raise _InfeasibleTargetError(unicod(self._pattern))
            return u('')
        if op is C.SUBPATTERN:
            return self._gen_seq(arg[-1], k)
        if op is C.BRANCH:
            alts = list(arg[-1])
            if k is None:
                return self._gen_seq(self._rng.choice(alts), None)
            feasible = []
            for alt in alts:
                alo, ahi = self._seq_range(alt)
                if alo <= k <= ahi:
                    feasible.append(alt)
            self._rng.shuffle(feasible)
            for alt in feasible:
                try:
                    return self._gen_seq(alt, k)
                except _InfeasibleTargetError:
                    continue
            raise _InfeasibleTargetError(unicod(self._pattern))
        if op in _REPEAT_OPS:
            mn, mx, sub = arg[0], arg[1], arg[-1]
            if k is None:
                count = self._rng.randint(mn, min(mx, mn + _REPEAT_WINDOW))
                return u('').join(self._gen_seq(sub, None)
                                  for _ in range(count))
            slo, shi = self._seq_range(sub)
            counts = [c for c in range(mn, min(mx, _RANGE_CAP) + 1)
                      if c * slo <= k <= c * shi]
            self._rng.shuffle(counts)
            for c in counts:
                try:
                    return self._gen_repeat(sub, c, k, slo, shi)
                except _InfeasibleTargetError:
                    continue
            raise _InfeasibleTargetError(unicod(self._pattern))
        raise _UnsupportedPatternError(unicod(op))

    def _gen_repeat(self, sub, count, k, slo, shi):
        if count == 0:
            if k != 0:
                raise _InfeasibleTargetError(unicod(self._pattern))
            return u('')
        parts = []
        remaining = k
        for j in range(count):
            left = count - 1 - j
            lo = max(slo, remaining - left * shi)
            hi = min(shi, remaining - left * slo)
            options = list(range(lo, hi + 1))
            self._rng.shuffle(options)
            piece = None
            for pj in options:
                try:
                    piece = self._gen_seq(sub, pj)
                    remaining -= pj
                    break
                except _InfeasibleTargetError:
                    continue
            if piece is None:
                raise _InfeasibleTargetError(unicod(self._pattern))
            parts.append(piece)
        return u('').join(parts)

    def _gen_atom(self, op, arg):
        C = _re_constants
        if op is C.LITERAL:
            return chr(arg)
        if op is C.NOT_LITERAL:
            return self._rng.choice([d for d in _DIGITS if ord(d) != arg])
        if op is C.IN:
            return self._gen_in(arg)
        if op is C.CATEGORY:
            return self._rng.choice(self._category_chars(arg))
        if op is C.ANY or op is _ANY_ALL:
            return self._rng.choice(_DIGITS)
        raise _UnsupportedPatternError(unicod(op))

    def _gen_in(self, items):
        C = _re_constants
        negate = False
        pool = []
        for iop, iarg in items:
            if iop is C.NEGATE:
                negate = True
            elif iop is C.LITERAL:
                pool.append(chr(iarg))
            elif iop is C.RANGE:
                lo, hi = iarg
                if hi - lo <= 32:
                    pool.extend(chr(c) for c in range(lo, hi + 1))
                else:
                    pool.append(chr(self._rng.randint(lo, hi)))
            elif iop is C.CATEGORY:
                pool.extend(self._category_chars(iarg))
        if negate:
            excluded = set(pool)
            pool = [d for d in _DIGITS if d not in excluded]
            if not pool:
                pool = [c for c in u('abcdef') if c not in excluded]
        if not pool:
            raise _UnsupportedPatternError(u('empty character class'))
        return self._rng.choice(pool)

    def _category_chars(self, cat):
        C = _re_constants
        if cat in (C.CATEGORY_DIGIT, _const('CATEGORY_UNI_DIGIT')):
            return list(_DIGITS)
        if cat in (C.CATEGORY_NOT_DIGIT, _const('CATEGORY_UNI_NOT_DIGIT')):
            return list(u('abcxyz'))
        if cat in (C.CATEGORY_SPACE, _const('CATEGORY_UNI_SPACE')):
            return [u(' ')]
        if cat in (C.CATEGORY_NOT_SPACE, _const('CATEGORY_UNI_NOT_SPACE')):
            return list(_DIGITS)
        if cat in (C.CATEGORY_WORD, _const('CATEGORY_UNI_WORD'),
                   _const('CATEGORY_LOC_WORD')):
            return list(u('abc012'))
        if cat in (C.CATEGORY_NOT_WORD, _const('CATEGORY_UNI_NOT_WORD'),
                   _const('CATEGORY_LOC_NOT_WORD')):
            return [u('-'), u(' ')]
        return list(_DIGITS)

    def _leading_chars(self, ops):
        result = set()
        for op, arg in ops:
            lo, _hi = self._node_range(op, arg)
            result |= self._node_first_chars(op, arg)
            if lo > 0:
                break
        return result

    def _node_first_chars(self, op, arg):
        C = _re_constants
        if op is C.LITERAL:
            return set([chr(arg)])
        if op is C.NOT_LITERAL:
            return set(d for d in _DIGITS if ord(d) != arg)
        if op is C.IN:
            chars = set()
            negate = False
            for iop, iarg in arg:
                if iop is C.NEGATE:
                    negate = True
                elif iop is C.LITERAL:
                    chars.add(chr(iarg))
                elif iop is C.RANGE:
                    lo, hi = iarg
                    for c in range(lo, min(hi, lo + 128) + 1):
                        chars.add(chr(c))
                elif iop is C.CATEGORY:
                    chars |= set(self._category_chars(iarg))
            if negate:
                chars = set(d for d in _DIGITS if d not in chars)
            return chars
        if op is C.CATEGORY:
            return set(self._category_chars(arg))
        if op is C.ANY or op is _ANY_ALL:
            return set(_DIGITS)
        if op is C.SUBPATTERN:
            return self._leading_chars(arg[-1])
        if op is C.BRANCH:
            chars = set()
            for alt in arg[-1]:
                chars |= self._leading_chars(alt)
            return chars
        if op in _REPEAT_OPS:
            return self._leading_chars(arg[-1])
        return set()


_FULLMATCH_CACHE = {}


def _fullmatch(pattern, text):
    compiled = _FULLMATCH_CACHE.get(pattern)
    if compiled is None:
        compiled = re.compile(pattern)
        _FULLMATCH_CACHE[pattern] = compiled
    match = compiled.match(text)
    return match is not None and match.end() == len(text)


def _desc_matches(desc, nsn):
    """Mirror of phonenumberutil._is_number_matching_desc."""
    if desc is None or not desc.national_number_pattern:
        return False
    possible_lengths = desc.possible_length
    if possible_lengths and len(nsn) not in possible_lengths:
        return False
    return _fullmatch(desc.national_number_pattern, nsn)


def oracle_number_type(region, nsn):
    """Number type derived from metadata alone.

    Mirrors phonenumberutil._number_type_helper; returns the type name as a
    string ('MOBILE', 'FIXED_LINE_OR_MOBILE', 'UNKNOWN', ...).
    """
    metadata = PhoneMetadata.metadata_for_region(region)
    if metadata is None or not _desc_matches(metadata.general_desc, nsn):
        return u('UNKNOWN')
    for tname in _TYPE_DESCS[:-2]:
        if _desc_matches(getattr(metadata, tname), nsn):
            return unicod(tname.upper())
    if _desc_matches(metadata.fixed_line, nsn):
        if (metadata.same_mobile_and_fixed_line_pattern or
                _desc_matches(metadata.mobile, nsn)):
            return u('FIXED_LINE_OR_MOBILE')
        return u('FIXED_LINE')
    if (not metadata.same_mobile_and_fixed_line_pattern and
            _desc_matches(metadata.mobile, nsn)):
        return u('MOBILE')
    return u('UNKNOWN')


def oracle_classification(region, nsn):
    """Expected parseable/possible/valid/type classification from metadata.

    'possible' mirrors is_possible_number(), which is documented to consult
    only the metadata of the main region for the country calling code.
    """
    metadata = PhoneMetadata.metadata_for_region(region)
    main_region = phonenumberutil.region_code_for_country_code(
        metadata.country_code)
    main_metadata = PhoneMetadata.metadata_for_region(main_region)
    general = main_metadata.general_desc
    length = len(nsn)
    possible = (length in general.possible_length or
                length in general.possible_length_local_only)
    ntype = oracle_number_type(region, nsn)
    return Classification(parseable=True,
                          possible=possible,
                          valid=(ntype != u('UNKNOWN')),
                          number_type=ntype,
                          italian_leading_zero=nsn.startswith(u('0')))


def build_raw_input(region, nsn):
    """A raw user-style input string that parses to exactly this NSN.

    Tries, in order: national prefix + NSN (as a user would dial it), the
    bare NSN, and the E.164 form.  Returns None if none of them round-trips
    to the requested national significant number.
    """
    metadata = PhoneMetadata.metadata_for_region(region)
    candidates = []
    if metadata.national_prefix:
        candidates.append(unicod(metadata.national_prefix) + nsn)
    candidates.append(nsn)
    candidates.append(u('+') + unicod(metadata.country_code) + nsn)
    for raw in candidates:
        try:
            parsed = phonenumbers.parse(raw, region)
        except phonenumbers.NumberParseException:
            continue
        if (parsed.country_code == metadata.country_code and
                phonenumberutil.national_significant_number(parsed) == nsn):
            return raw
    return None


def same_number_identity(first, second):
    """Whether two parsed numbers identify the same dialled number.

    Compares country code, national number, extension and leading-zero
    information; parse bookkeeping (raw input, country code source) is not
    part of a number's identity.
    """
    return (first.country_code == second.country_code and
            first.national_number == second.national_number and
            first.extension == second.extension and
            bool(first.italian_leading_zero) == bool(second.italian_leading_zero) and
            first.number_of_leading_zeros == second.number_of_leading_zeros)


def round_trip_stable(region, raw_input):
    """Whether raw_input survives national/international/E.164 round-trips.

    Parses raw_input, then checks that formatting the result as NATIONAL,
    INTERNATIONAL and E164 and parsing each form again yields a number with
    the same identity every time.
    """
    try:
        numobj = phonenumbers.parse(raw_input, region)
    except phonenumbers.NumberParseException:
        return False
    for number_format in (PhoneNumberFormat.NATIONAL,
                          PhoneNumberFormat.INTERNATIONAL,
                          PhoneNumberFormat.E164):
        formatted = phonenumbers.format_number(numobj, number_format)
        try:
            reparsed = phonenumbers.parse(formatted, region)
        except phonenumbers.NumberParseException:
            return False
        if not same_number_identity(numobj, reparsed):
            return False
    return True


def live_classification(region, nsn, raw_input=None):
    """Classification of an input by the library itself (test instrumentation)."""
    if raw_input is None:
        raw_input = build_raw_input(region, nsn)
        if raw_input is None:
            return Classification(False, False, False, u('UNKNOWN'), False)
    try:
        numobj = phonenumbers.parse(raw_input, region)
    except phonenumbers.NumberParseException:
        return Classification(False, False, False, u('UNKNOWN'), False)
    return Classification(
        parseable=True,
        possible=phonenumbers.is_possible_number(numobj),
        valid=phonenumbers.is_valid_number_for_region(numobj, region),
        number_type=PhoneNumberType.to_string(phonenumbers.number_type(numobj)),
        italian_leading_zero=bool(numobj.italian_leading_zero))


def _sample_to_dict(sample):
    return {u('region'): sample.region,
            u('branch'): sample.branch,
            u('raw_input'): sample.raw_input,
            u('national_number'): sample.national_number,
            u('expected'): {
                u('parseable'): sample.expected.parseable,
                u('possible'): sample.expected.possible,
                u('valid'): sample.expected.valid,
                u('number_type'): sample.expected.number_type,
                u('italian_leading_zero'): sample.expected.italian_leading_zero,
            }}


def _sample_from_dict(data):
    expected = data[u('expected')]
    return Sample(region=data[u('region')],
                  branch=data[u('branch')],
                  raw_input=data[u('raw_input')],
                  national_number=data[u('national_number')],
                  expected=Classification(
                      parseable=expected[u('parseable')],
                      possible=expected[u('possible')],
                      valid=expected[u('valid')],
                      number_type=expected[u('number_type')],
                      italian_leading_zero=expected[u('italian_leading_zero')]))


class Corpus(object):
    """A generated regression corpus plus its branch coverage report."""

    def __init__(self, seed, version, budget, samples, covered, uncovered):
        self.seed = seed
        self.version = version
        self.budget = budget
        self.samples = tuple(samples)
        # branch id ('<region>:<branch>') -> number of samples generated
        self.covered = dict(covered)
        # branch id -> reason no sample was generated
        self.uncovered = dict(uncovered)

    def to_dict(self):
        return {u('seed'): self.seed,
                u('version'): self.version,
                u('budget'): self.budget,
                u('samples'): [_sample_to_dict(s) for s in self.samples],
                u('covered'): self.covered,
                u('uncovered'): self.uncovered}

    def to_json(self):
        return json.dumps(self.to_dict(), sort_keys=True, indent=1)

    @classmethod
    def from_dict(cls, data):
        return cls(seed=data[u('seed')],
                   version=data[u('version')],
                   budget=data[u('budget')],
                   samples=[_sample_from_dict(s) for s in data[u('samples')]],
                   covered=data[u('covered')],
                   uncovered=data[u('uncovered')])

    @classmethod
    def from_json(cls, text):
        return cls.from_dict(json.loads(text))

    def __eq__(self, other):
        return isinstance(other, Corpus) and self.to_dict() == other.to_dict()

    def __ne__(self, other):
        return not self.__eq__(other)


class CorpusGenerator(object):
    """Builds a Corpus from the metadata, honouring a sample budget.

    Determinism: every decision is driven by a single random.Random seeded
    with '<version>:<seed>', and regions/branches are visited in a fixed
    order, so the same seed and version rebuild the same samples.
    """

    def __init__(self, seed=0, budget=100, version=None, regions=None,
                 focus_regions=DEFAULT_FOCUS_REGIONS, max_attempts=64):
        self.seed = seed
        self.budget = budget
        self.version = version or phonenumbers.__version__
        if regions is None:
            regions = phonenumberutil.SUPPORTED_REGIONS
        self.regions = list(regions)
        self.focus_regions = tuple(focus_regions)
        self.max_attempts = max_attempts
        self._rng = random.Random(u('cleaningcorpus:%s:%s') %
                                  (self.version, self.seed))
        self._samplers = {}
        self._seen = set()

    def generate(self):
        samples = []
        covered = {}
        uncovered = {}
        ordered = [r for r in self.focus_regions if r in self.regions]
        ordered.extend(sorted(r for r in self.regions
                              if r not in self.focus_regions))
        for region in ordered:
            metadata = PhoneMetadata.metadata_for_region(region)
            if metadata is None:
                continue
            for branch, producer in self._branch_plan(region, metadata):
                branch_id = u('%s:%s') % (region, branch)
                if len(samples) >= self.budget:
                    uncovered[branch_id] = REASON_BUDGET
                    continue
                try:
                    sample = producer()
                except _UnsupportedPatternError:
                    uncovered[branch_id] = REASON_UNSUPPORTED
                    continue
                if sample is None:
                    uncovered[branch_id] = REASON_INFEASIBLE
                else:
                    samples.append(sample)
                    covered[branch_id] = covered.get(branch_id, 0) + 1
        return Corpus(seed=self.seed, version=self.version, budget=self.budget,
                      samples=samples, covered=covered, uncovered=uncovered)

    def _sampler(self, pattern):
        sampler = self._samplers.get(pattern)
        if sampler is None:
            sampler = _PatternSampler(pattern, self._rng)
            self._samplers[pattern] = sampler
        return sampler

    def _branch_plan(self, region, metadata):
        plan = []
        general = metadata.general_desc
        fixed = metadata.fixed_line
        mobile = metadata.mobile
        if (fixed is not None and fixed.national_number_pattern and
                mobile is not None and mobile.national_number_pattern):
            plan.append((BRANCH_OVERLAP,
                         lambda: self._gen_overlap(region, metadata,
                                                   BRANCH_OVERLAP)))
        lengths = general.possible_length if general is not None else ()
        if lengths:
            lo, hi = lengths[0], lengths[-1]
            if lo > 1:
                plan.append((BRANCH_LENGTH_PREFIX + u('below_min'),
                             lambda branch=BRANCH_LENGTH_PREFIX + u('below_min'):
                             self._gen_length(region, metadata, branch,
                                              lo - 1)))
            plan.append((BRANCH_LENGTH_PREFIX + u('min'),
                         lambda branch=BRANCH_LENGTH_PREFIX + u('min'):
                         self._gen_length(region, metadata, branch, lo)))
            plan.append((BRANCH_LENGTH_PREFIX + u('max'),
                         lambda branch=BRANCH_LENGTH_PREFIX + u('max'):
                         self._gen_length(region, metadata, branch, hi)))
            if hi < _MAX_NSN_LENGTH - 1:
                plan.append((BRANCH_LENGTH_PREFIX + u('above_max'),
                             lambda branch=BRANCH_LENGTH_PREFIX + u('above_max'):
                             self._gen_length(region, metadata, branch,
                                              hi + 1)))
        if general is not None and general.national_number_pattern:
            sampler = self._sampler(general.national_number_pattern)
            if u('0') in sampler.leading_chars():
                plan.append((BRANCH_LEADING_ZERO,
                             lambda: self._gen_leading_zero(region, metadata,
                                                            BRANCH_LEADING_ZERO)))
        for tname in _TYPE_DESCS:
            desc = getattr(metadata, tname)
            if desc is not None and desc.national_number_pattern:
                branch = BRANCH_TYPE_PREFIX + tname
                plan.append((branch,
                             lambda branch=branch, tname=tname:
                             self._gen_type(region, metadata, branch, tname)))
        return plan

    def _finalize(self, region, branch, nsn):
        if not nsn or len(nsn) > _MAX_NSN_LENGTH:
            return None
        key = (region, nsn)
        if key in self._seen:
            return None
        metadata = PhoneMetadata.metadata_for_region(region)
        if self._is_example_number(metadata, nsn):
            return None
        raw = build_raw_input(region, nsn)
        if raw is None or not round_trip_stable(region, raw):
            return None
        self._seen.add(key)
        return Sample(region=region, branch=branch, raw_input=raw,
                      national_number=nsn,
                      expected=oracle_classification(region, nsn))

    @staticmethod
    def _is_example_number(metadata, nsn):
        for tname in _TYPE_DESCS + ('general_desc',):
            desc = getattr(metadata, tname)
            if desc is not None and desc.example_number == nsn:
                return True
        return False

    def _gen_overlap(self, region, metadata, branch):
        fixed_sampler = self._sampler(metadata.fixed_line.national_number_pattern)
        mobile_sampler = self._sampler(metadata.mobile.national_number_pattern)
        for attempt in range(self.max_attempts):
            if metadata.same_mobile_and_fixed_line_pattern:
                nsn = fixed_sampler.generate()
            elif attempt % 2 == 0:
                nsn = mobile_sampler.generate()
            else:
                nsn = fixed_sampler.generate()
            if oracle_number_type(region, nsn) == u('FIXED_LINE_OR_MOBILE'):
                sample = self._finalize(region, branch, nsn)
                if sample is not None:
                    return sample
        return None

    def _gen_length(self, region, metadata, branch, target):
        sampler = self._sampler(metadata.general_desc.national_number_pattern)
        for _attempt in range(self.max_attempts):
            try:
                nsn = sampler.generate(target_length=target)
            except _InfeasibleTargetError:
                nsn = self._synthesize(metadata, target)
                if nsn is None:
                    return None
            sample = self._finalize(region, branch, nsn)
            if sample is not None:
                return sample
        return None

    def _synthesize(self, metadata, target):
        """Fallback digit string of exactly target length.

        Used when the general pattern cannot produce the boundary length
        itself (e.g. one below the minimum).  The first digit is chosen from
        the pattern's possible leading characters so the result still looks
        like a plausible national number for the region.
        """
        if target < 1:
            return None
        sampler = self._sampler(metadata.general_desc.national_number_pattern)
        first_digits = [c for c in sampler.leading_chars() if c in _DIGITS]
        if not first_digits:
            return None
        first = self._rng.choice(first_digits)
        rest = u('').join(self._rng.choice(_DIGITS)
                          for _ in range(target - 1))
        return first + rest

    def _gen_leading_zero(self, region, metadata, branch):
        sampler = self._sampler(metadata.general_desc.national_number_pattern)
        for _attempt in range(self.max_attempts):
            nsn = sampler.generate()
            if nsn.startswith(u('0')):
                sample = self._finalize(region, branch, nsn)
                if sample is not None:
                    return sample
        return None

    def _gen_type(self, region, metadata, branch, tname):
        desc = getattr(metadata, tname)
        sampler = self._sampler(desc.national_number_pattern)
        acceptable = set([unicod(tname.upper())])
        if tname in ('fixed_line', 'mobile'):
            acceptable.add(u('FIXED_LINE_OR_MOBILE'))
        for _attempt in range(self.max_attempts):
            nsn = sampler.generate()
            if oracle_number_type(region, nsn) in acceptable:
                sample = self._finalize(region, branch, nsn)
                if sample is not None:
                    return sample
        return None


def _ddmin(nsn, pred):
    """Delta-debugging: remove chunks of digits while pred holds."""
    granularity = 2
    while len(nsn) >= 2:
        chunk = (len(nsn) + granularity - 1) // granularity
        if chunk < 1:
            break
        reduced = False
        for start in range(0, len(nsn), chunk):
            candidate = nsn[:start] + nsn[start + chunk:]
            if candidate and pred(candidate):
                nsn = candidate
                granularity = max(granularity - 1, 2)
                reduced = True
                break
        if not reduced:
            if granularity >= len(nsn):
                break
            granularity = min(granularity * 2, len(nsn))
    return nsn


def _minimize_digits(nsn, pred):
    """Lower individual digits towards 0 while pred holds."""
    changed = True
    while changed:
        changed = False
        for i in range(len(nsn)):
            for digit in (u('0'), u('1')):
                if digit >= nsn[i]:
                    continue
                candidate = nsn[:i] + digit + nsn[i + 1:]
                if pred(candidate):
                    nsn = candidate
                    changed = True
                    break
    return nsn


def shrink_sample(sample, triggers_difference):
    """Minimise a failing sample, preserving its classification difference.

    triggers_difference is a predicate over national-number strings that must
    hold for sample.national_number; callers typically build it so that it is
    true exactly when the live library classification still differs from the
    recorded expectation in the original way.  The returned Sample keeps the
    original expectation but carries the smallest national number found (by
    chunk removal, then digit minimisation) for which the predicate -- and
    therefore the original classification difference -- still holds.
    """
    nsn = sample.national_number
    if not triggers_difference(nsn):
        raise ValueError(u('predicate must hold for the original sample'))
    nsn = _ddmin(nsn, triggers_difference)
    nsn = _minimize_digits(nsn, triggers_difference)
    raw = build_raw_input(sample.region, nsn)
    if raw is None:
        raw = nsn
    return sample._replace(national_number=nsn, raw_input=raw)


def generate_snapshot_corpus():
    """Rebuilds the checked-in snapshot corpus (seed and budget are fixed)."""
    return CorpusGenerator(seed=SNAPSHOT_SEED, budget=SNAPSHOT_BUDGET).generate()


if __name__ == '__main__':
    import sys
    sys.stdout.write(generate_snapshot_corpus().to_json() + u('\n'))
