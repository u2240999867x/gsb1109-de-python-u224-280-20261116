"""Tests for the metadata-driven cleaning-entry regression corpus.

The corpus itself lives in tests/cleaningcorpus_snapshot.json and is rebuilt
deterministically by tests/cleaningcorpus.py; see that module for the branch
definitions and the regeneration procedure.
"""

import os
import re
import unittest
from collections import Counter

import phonenumbers
from phonenumbers import PhoneMetadata, PhoneNumberFormat, PhoneNumberType
from phonenumbers import phonenumberutil
from phonenumbers.util import u

from . import cleaningcorpus
from .cleaningcorpus import CorpusGenerator

_SNAPSHOT_PATH = os.path.join(os.path.dirname(__file__),
                              'cleaningcorpus_snapshot.json')

_TEST_VERSION = u('test-version-1')
_SMALL_REGIONS = (u('US'), u('DE'), u('IT'), u('GB'), u('BR'), u('CA'))

_BRANCH_RE = re.compile(
    u('^(overlap:fixed_line\\+mobile|'
      'length:general:(below_min|min|max|above_max)|'
      'leading_zero:significant|'
      'type:(premium_rate|toll_free|shared_cost|voip|personal_number|pager|'
      'uan|voicemail|fixed_line|mobile))$'))


def _load_snapshot():
    with open(_SNAPSHOT_PATH) as snapshot_file:
        return cleaningcorpus.Corpus.from_json(snapshot_file.read())


class CorpusGenerationTest(unittest.TestCase):
    """The generator is deterministic, budget-driven and labels branches."""

    def testSameSeedAndVersionRebuildSameSamples(self):
        first = CorpusGenerator(seed=7, budget=60, version=_TEST_VERSION,
                                regions=_SMALL_REGIONS).generate()
        second = CorpusGenerator(seed=7, budget=60, version=_TEST_VERSION,
                                 regions=_SMALL_REGIONS).generate()
        self.assertEqual(first, second)
        self.assertEqual(first.to_json(), second.to_json())

    def testDifferentSeedBuildsDifferentSamples(self):
        base = CorpusGenerator(seed=7, budget=60, version=_TEST_VERSION,
                               regions=_SMALL_REGIONS).generate()
        other = CorpusGenerator(seed=8, budget=60, version=_TEST_VERSION,
                                regions=_SMALL_REGIONS).generate()
        self.assertNotEqual(base.to_json(), other.to_json())

    def testDifferentVersionBuildsDifferentSamples(self):
        base = CorpusGenerator(seed=7, budget=60, version=_TEST_VERSION,
                               regions=_SMALL_REGIONS).generate()
        other = CorpusGenerator(seed=7, budget=60, version=u('other-version'),
                                regions=_SMALL_REGIONS).generate()
        self.assertNotEqual(base.to_json(), other.to_json())

    def testBudgetLimitsSampleCount(self):
        corpus = CorpusGenerator(seed=1, budget=5, version=_TEST_VERSION,
                                 regions=_SMALL_REGIONS).generate()
        self.assertEqual(5, len(corpus.samples))
        corpus = CorpusGenerator(seed=1, budget=0, version=_TEST_VERSION,
                                 regions=_SMALL_REGIONS).generate()
        self.assertEqual(0, len(corpus.samples))

    def testUncoveredBranchesAreListedWithReasons(self):
        corpus = CorpusGenerator(seed=1, budget=5, version=_TEST_VERSION,
                                 regions=_SMALL_REGIONS).generate()
        # A tiny budget leaves most branches uncovered, and they must be listed.
        self.assertTrue(corpus.uncovered)
        self.assertIn(cleaningcorpus.REASON_BUDGET,
                      set(corpus.uncovered.values()))
        self.assertTrue(set(corpus.uncovered.values()) <=
                        set([cleaningcorpus.REASON_BUDGET,
                             cleaningcorpus.REASON_INFEASIBLE,
                             cleaningcorpus.REASON_UNSUPPORTED]))
        # Covered and uncovered branches are disjoint and account for every
        # sample exactly once.
        self.assertFalse(set(corpus.covered) & set(corpus.uncovered))
        counts = Counter(u('%s:%s') % (s.region, s.branch)
                         for s in corpus.samples)
        self.assertEqual(dict(counts), corpus.covered)

    def testEverySampleIsLabelledWithAKnownBranch(self):
        corpus = CorpusGenerator(seed=2, budget=80, version=_TEST_VERSION,
                                 regions=_SMALL_REGIONS).generate()
        self.assertTrue(corpus.samples)
        for sample in corpus.samples:
            self.assertIsNotNone(_BRANCH_RE.match(sample.branch), sample)

    def testLengthBoundarySamplesHitExactLengths(self):
        corpus = CorpusGenerator(seed=2, budget=200, version=_TEST_VERSION,
                                 regions=_SMALL_REGIONS).generate()
        seen = set()
        for sample in corpus.samples:
            if not sample.branch.startswith(u('length:general:')):
                continue
            seen.add(sample.branch)
            metadata = PhoneMetadata.metadata_for_region(sample.region)
            lengths = metadata.general_desc.possible_length
            expected = {u('length:general:below_min'): lengths[0] - 1,
                        u('length:general:min'): lengths[0],
                        u('length:general:max'): lengths[-1],
                        u('length:general:above_max'): lengths[-1] + 1}
            self.assertEqual(expected[sample.branch],
                             len(sample.national_number), sample)
        self.assertIn(u('length:general:min'), seen)
        self.assertIn(u('length:general:max'), seen)

    def testSamplesAreNotCopiedExampleNumbers(self):
        corpus = CorpusGenerator(seed=2, budget=200, version=_TEST_VERSION,
                                 regions=_SMALL_REGIONS).generate()
        self.assertTrue(corpus.samples)
        for sample in corpus.samples:
            metadata = PhoneMetadata.metadata_for_region(sample.region)
            for tname in cleaningcorpus._TYPE_DESCS + ('general_desc',):
                desc = getattr(metadata, tname)
                self.assertFalse(
                    desc is not None and
                    desc.example_number == sample.national_number,
                    sample)


class SnapshotCorpusTest(unittest.TestCase):
    """Runs the checked-in corpus through the cleaning entry (parse)."""

    @classmethod
    def setUpClass(cls):
        cls.corpus = _load_snapshot()

    def testSnapshotMatchesGeneratorInputs(self):
        corpus = self.corpus
        self.assertEqual(cleaningcorpus.SNAPSHOT_SEED, corpus.seed)
        self.assertEqual(cleaningcorpus.SNAPSHOT_BUDGET, corpus.budget)
        self.assertEqual(phonenumbers.__version__, corpus.version)
        self.assertEqual(corpus.budget, len(corpus.samples))

    def testFocusBranchesAreCovered(self):
        covered = self.corpus.covered
        # Fixed-line/mobile overlap.
        self.assertIn(u('US:') + cleaningcorpus.BRANCH_OVERLAP, covered)
        # Length boundaries on a region with a wide length range.
        for boundary in (u('below_min'), u('min'), u('max'), u('above_max')):
            self.assertIn(u('DE:length:general:') + boundary, covered)
        # Significant leading zero.
        self.assertIn(u('IT:') + cleaningcorpus.BRANCH_LEADING_ZERO, covered)

    def testUncoveredBranchesAreListed(self):
        # The snapshot budget cannot cover every branch of every region; the
        # remainder must be listed, with reasons, and disjoint from covered.
        self.assertTrue(self.corpus.uncovered)
        self.assertFalse(set(self.corpus.covered) &
                         set(self.corpus.uncovered))

    def testClassificationLevelsMatchExpectations(self):
        for sample in self.corpus.samples:
            try:
                numobj = phonenumbers.parse(sample.raw_input, sample.region)
                parseable = True
            except phonenumbers.NumberParseException:
                parseable = False
            expected = sample.expected
            self.assertEqual(expected.parseable, parseable, sample)
            if not parseable:
                continue
            self.assertEqual(expected.possible,
                             phonenumbers.is_possible_number(numobj), sample)
            self.assertEqual(
                expected.valid,
                phonenumbers.is_valid_number_for_region(numobj, sample.region),
                sample)
            self.assertEqual(expected.italian_leading_zero,
                             bool(numobj.italian_leading_zero), sample)
            if expected.valid:
                self.assertTrue(phonenumbers.is_valid_number(numobj), sample)
                self.assertEqual(
                    expected.number_type,
                    PhoneNumberType.to_string(
                        phonenumbers.number_type(numobj)),
                    sample)

    def testCorpusDistinguishesParseablePossibleValid(self):
        # The corpus must actually exercise the three distinct levels:
        # parseable-but-not-possible, possible-but-not-valid, and valid.
        levels = set()
        for sample in self.corpus.samples:
            expected = sample.expected
            self.assertTrue(expected.parseable, sample)  # by construction
            if expected.valid:
                self.assertTrue(expected.possible or
                                self._shared_country_code(sample), sample)
                levels.add(u('valid'))
            elif expected.possible:
                levels.add(u('possible-not-valid'))
            else:
                levels.add(u('parseable-not-possible'))
        self.assertEqual(set([u('valid'), u('possible-not-valid'),
                              u('parseable-not-possible')]), levels)

    def _shared_country_code(self, sample):
        metadata = PhoneMetadata.metadata_for_region(sample.region)
        return (phonenumberutil.region_code_for_country_code(
            metadata.country_code) != sample.region)

    def testNationalInternationalRoundTripPreservesIdentityAndType(self):
        for sample in self.corpus.samples:
            numobj = phonenumbers.parse(sample.raw_input, sample.region)
            for number_format in (PhoneNumberFormat.NATIONAL,
                                  PhoneNumberFormat.INTERNATIONAL,
                                  PhoneNumberFormat.E164):
                formatted = phonenumbers.format_number(numobj, number_format)
                reparsed = phonenumbers.parse(formatted, sample.region)
                self.assertTrue(
                    cleaningcorpus.same_number_identity(numobj, reparsed),
                    (sample, number_format, formatted))
                if sample.expected.valid:
                    self.assertEqual(phonenumbers.number_type(numobj),
                                     phonenumbers.number_type(reparsed),
                                     sample)

    def testOverlapSamplesAreFixedLineOrMobile(self):
        overlap_samples = [s for s in self.corpus.samples
                           if s.branch == cleaningcorpus.BRANCH_OVERLAP]
        self.assertTrue(overlap_samples)
        for sample in overlap_samples:
            self.assertEqual(u('FIXED_LINE_OR_MOBILE'),
                             sample.expected.number_type, sample)
            numobj = phonenumbers.parse(sample.raw_input, sample.region)
            self.assertEqual(PhoneNumberType.FIXED_LINE_OR_MOBILE,
                             phonenumbers.number_type(numobj), sample)
            metadata = PhoneMetadata.metadata_for_region(sample.region)
            self.assertTrue(
                metadata.same_mobile_and_fixed_line_pattern or
                (cleaningcorpus._desc_matches(metadata.fixed_line,
                                              sample.national_number) and
                 cleaningcorpus._desc_matches(metadata.mobile,
                                              sample.national_number)),
                sample)

    def testLeadingZeroSamplesKeepSignificantZero(self):
        zero_samples = [s for s in self.corpus.samples
                        if s.branch == cleaningcorpus.BRANCH_LEADING_ZERO]
        self.assertTrue(zero_samples)
        for sample in zero_samples:
            self.assertTrue(sample.national_number.startswith(u('0')), sample)
            self.assertTrue(sample.expected.italian_leading_zero, sample)
            numobj = phonenumbers.parse(sample.raw_input, sample.region)
            self.assertTrue(numobj.italian_leading_zero, sample)
            # The zero is significant: it survives the E.164 round-trip.
            e164 = phonenumbers.format_number(numobj, PhoneNumberFormat.E164)
            reparsed = phonenumbers.parse(e164, sample.region)
            self.assertTrue(reparsed.italian_leading_zero, sample)
            self.assertEqual(
                sample.national_number,
                phonenumberutil.national_significant_number(reparsed), sample)


class CorpusShrinkTest(unittest.TestCase):
    """Shrinking a failure keeps the original classification difference."""

    def testShrinkPreservesTypeDifference(self):
        corpus = CorpusGenerator(seed=3, budget=8, version=_TEST_VERSION,
                                 regions=(u('US'),)).generate()
        sample = [s for s in corpus.samples
                  if s.branch == cleaningcorpus.BRANCH_OVERLAP][0]
        original = cleaningcorpus.live_classification(sample.region,
                                                      sample.national_number)
        # A (deliberately wrong) recorded expectation to shrink against.
        mutated = sample.expected._replace(number_type=u('MOBILE'))

        def triggers_difference(nsn):
            actual = cleaningcorpus.live_classification(sample.region, nsn)
            return actual == original and actual != mutated

        self.assertTrue(triggers_difference(sample.national_number))
        shrunk = cleaningcorpus.shrink_sample(sample, triggers_difference)
        self.assertLessEqual(len(shrunk.national_number),
                             len(sample.national_number))
        self.assertTrue(triggers_difference(shrunk.national_number))
        # The shrunk input is still classified exactly as the original
        # failing sample was.
        actual = cleaningcorpus.live_classification(sample.region,
                                                    shrunk.national_number)
        self.assertEqual(u('FIXED_LINE_OR_MOBILE'), actual.number_type)
        self.assertNotEqual(mutated.number_type, actual.number_type)

    def testShrinkPreservesParseableButNotPossible(self):
        corpus = _load_snapshot()
        sample = None
        for candidate in corpus.samples:
            if (candidate.branch == u('length:general:below_min') and
                    not candidate.expected.possible):
                sample = candidate
                break
        self.assertIsNotNone(sample)

        def triggers_difference(nsn):
            live = cleaningcorpus.live_classification(sample.region, nsn)
            return live.parseable and not live.possible

        self.assertTrue(triggers_difference(sample.national_number))
        shrunk = cleaningcorpus.shrink_sample(sample, triggers_difference)
        self.assertLess(len(shrunk.national_number),
                        len(sample.national_number))
        self.assertTrue(triggers_difference(shrunk.national_number))

    def testShrinkRequiresPredicateToHoldInitially(self):
        corpus = CorpusGenerator(seed=3, budget=2, version=_TEST_VERSION,
                                 regions=(u('US'),)).generate()
        sample = corpus.samples[0]
        self.assertRaises(ValueError, cleaningcorpus.shrink_sample,
                          sample, lambda nsn: False)


class SnapshotReproducibilityTest(unittest.TestCase):
    """The checked-in corpus rebuilds byte-for-byte from seed and version."""

    def testSnapshotRebuildsIdentically(self):
        rebuilt = cleaningcorpus.generate_snapshot_corpus()
        with open(_SNAPSHOT_PATH) as snapshot_file:
            self.assertEqual(snapshot_file.read().strip(),
                             rebuilt.to_json().strip())


if __name__ == '__main__':
    unittest.main()
