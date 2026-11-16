"""Tests for provenance-preserving loop splitting and merging."""

import unittest

import CifFile
from CifFile import LoopSplit
from CifFile.LoopSplit import (
    AmbiguityGroup, LoopSplitError, merge_split, match_rows, split_loop,
)
from CifFile.StarFile import StarBlock


class LoopSplitCoreTestCase(unittest.TestCase):
    def setUp(self):
        self.source = {
            'id': ['a', 'b', 'c', 'd'],
            'x': ['1', '2', '3', '4'],
            'y': ['p', 'q', 'r', 's'],
        }

    def testIdentitySplitRoundTrips(self):
        """An identity split restores every original value."""
        result = split_loop(self.source, ['x'], ['y'], ['id'])
        self.assertEqual(result.mode, 'project')
        self.assertTrue(result.reversible)
        self.assertEqual(result.first_loop['id'], ['a', 'b', 'c', 'd'])
        self.assertEqual(result.second_loop['y'], ['p', 'q', 'r', 's'])
        merged = result.merge_back()
        self.assertTrue(merged.reversible)
        self.assertEqual(dict(merged.columns), self.source)

    def testRowProvenanceIsExplicit(self):
        """Each target row records the exact source row it came from."""
        result = split_loop(self.source, ['x'], ['y'], ['id'])
        self.assertEqual(
            [(a.target_row, a.source_row) for a in result.first_provenance],
            [(0, 0), (1, 1), (2, 2), (3, 3)])
        self.assertEqual([a.key for a in result.second_provenance],
                         [('a',), ('b',), ('c',), ('d',)])

    def testPartitionKeepsProvenanceButIsNotReversible(self):
        """Rows present on only one side keep their source row but cannot
        reconstruct missing values, so they must not claim reversibility."""
        result = split_loop(
            self.source, ['x'], ['y'], ['id'],
            row_map=[(0, None), (None, 1), (2, 2), (None, 3)])
        self.assertFalse(result.reversible)
        merged = result.merge_back()
        self.assertFalse(merged.reversible)
        self.assertEqual(merged.provenance,
                         [(0, 'first', 0), (2, 'first', 1),
                          (1, 'second', 0), (2, 'second', 1),
                          (3, 'second', 2)])
        self.assertEqual(merged.complete_rows, (2,))
        self.assertEqual(merged.columns['x'], ['1', '?', '3', '?'])
        self.assertEqual(merged.columns['y'], ['?', 'q', 'r', 's'])

    def testDuplicateKeysAreCandidatesNotProducts(self):
        """A repeated key on both sides is not expanded."""
        source = {'id': ['a', 'a', 'b'], 'x': ['1', '2', '3'],
                  'y': ['p', 'q', 'r']}
        result = split_loop(source, ['x'], ['y'], ['id'])
        self.assertEqual(len(result.duplicate_key_groups()), 1)
        group = result.duplicate_key_groups()[0]
        self.assertEqual(group.key, ('a',))
        self.assertEqual([a.row for a in group.first_rows], [0, 1])
        self.assertEqual([a.row for a in group.second_rows], [0, 1])
        merged = merge_split(result, use_provenance=False)
        self.assertFalse(merged.reversible)
        self.assertEqual(len(merged.ambiguities), 1)
        self.assertEqual(
            [a.row for a in merged.ambiguities[0].first_rows], [0, 1])
        self.assertEqual(merged.basis, 'unique-key')

    def testProvenanceSurvivesDuplicateKeys(self):
        """Complete provenance restores duplicate-key rows correctly."""
        source = {'id': ['a', 'a'], 'x': ['1', '2'], 'y': ['p', 'q']}
        result = split_loop(source, ['x'], ['y'], ['id'])
        self.assertTrue(result.reversible)
        self.assertEqual(dict(result.merge_back().columns), source)

    def testRenamedOutputColumns(self):
        """Column renaming needed for a single CIF block is reversible."""
        first_names = dict((name, '_one.' + name) for name in self.source)
        second_names = dict((name, '_two.' + name) for name in self.source)
        result = split_loop(self.source, ['x'], ['y'], ['id'],
                            first_names=first_names,
                            second_names=second_names)
        self.assertEqual(sorted(result.first_loop),
                         ['_one.id', '_one.x'])
        self.assertTrue(result.reversible)
        self.assertEqual(dict(result.merge_back().columns), self.source)

    def testStaleProvenanceIsRejected(self):
        """Changing a key after the split invalidates the row association."""
        result = split_loop(self.source, ['x'], ['y'], ['id'])
        first = dict((name, list(values))
                     for name, values in result.first_loop.items())
        first['id'][0] = 'other'
        merged = result.merge_back(first_columns=first)
        self.assertFalse(merged.reversible)
        self.assertEqual(merged.ambiguities[0].reason, 'stale-provenance')

    def testEqualLengthIsNotEvidence(self):
        """Reordered equal-length columns are not reversible."""
        first = {'id': ['a', 'b', 'c', 'd'], 'x': ['2', '1', '3', '4']}
        second = {'id': ['a', 'b', 'c', 'd'], 'y': ['p', 'q', 'r', 's']}
        result = split_loop(self.source, ['x'], ['y'], ['id'])
        merged = merge_split(result, first, second)
        self.assertEqual(len(first['x']), len(second['y']))
        self.assertFalse(merged.reversible)

    def testMissingProvenanceFallsBackToUniqueKeys(self):
        """Without provenance, unique keys remain usable."""
        result = split_loop(self.source, ['x'], ['y'], ['id'])
        merged = merge_split(result, result.first_loop, result.second_loop,
                             use_provenance=False)
        self.assertTrue(merged.reversible)
        self.assertEqual(merged.basis, 'unique-key')

    def testAmbiguousKeyFallbackLeavesColumnsUnmerged(self):
        """The fallback must not invent pairings for repeated keys."""
        source = {'id': ['a', 'a'], 'x': ['1', '2'], 'y': ['p', 'q']}
        result = split_loop(source, ['x'], ['y'], ['id'])
        merged = merge_split(result, use_provenance=False)
        self.assertFalse(merged.reversible)
        self.assertEqual(len(merged.ambiguities), 1)

    def testMatchRowsProjectionAndAmbiguity(self):
        pairs, ambiguities, left, right = match_rows(
            ['v', 'w', 'w'], ['v', 'w', 'w'])
        self.assertEqual(pairs, [(0, 0, 'v')])
        self.assertEqual(len(ambiguities), 1)
        self.assertEqual((left, right), ([], []))

    def testMalformedRequestsRaise(self):
        self.assertRaises(LoopSplitError, split_loop, self.source, ['nope'],
                          ['y'], ['id'])
        self.assertRaises(LoopSplitError, split_loop, self.source, ['x'],
                          ['y'], ['id'], row_map=[(0, None)] * 4)
        bad = {'id': ['a'], 'x': ['1'], 'y': ['p', 'q']}
        self.assertRaises(LoopSplitError, split_loop, bad, ['x'], ['y'],
                          ['id'])


class StarBlockSplitTestCase(unittest.TestCase):
    def setUp(self):
        self.block = StarBlock()
        self.block['_item.id'] = ['1', '2', '3']
        self.block['_item.x'] = ['a', 'b', 'c']
        self.block['_item.y'] = ['A', 'B', 'C']
        self.block.CreateLoop(['_item.id', '_item.x', '_item.y'])

    def split(self):
        return self.block.SplitLoop(
            '_item.id', ['_item.x'], ['_item.y'],
            first_output_names={'_item.id': '_item.first_id'},
            second_output_names={'_item.id': '_item.second_id'})

    def testSplitCreatesTwoLoops(self):
        result = self.split()
        self.assertEqual(len(self.block.loops), 2)
        self.assertEqual(self.block.FindLoop('_item.x'),
                         result.loop_numbers[0])
        self.assertEqual(self.block.FindLoop('_item.y'),
                         result.loop_numbers[1])
        self.assertEqual(self.block['_item.first_id'], ['1', '2', '3'])
        self.assertEqual(self.block['_item.second_id'], ['1', '2', '3'])

    def testMergeRestoresSingleOriginalLoop(self):
        result = self.split()
        merged = self.block.MergeLoops(result)
        self.assertTrue(merged.reversible)
        self.assertEqual(len(self.block.loops), 1)
        self.assertEqual(self.block['_item.id'], ['1', '2', '3'])
        self.assertEqual(self.block['_item.x'], ['a', 'b', 'c'])
        self.assertEqual(self.block['_item.y'], ['A', 'B', 'C'])

    def testKeyNameCollisionIsRejectedBeforeMutation(self):
        self.assertRaises(LoopSplit.LoopSplitError, self.block.SplitLoop,
                          '_item.id', ['_item.x'], ['_item.y'])
        self.assertEqual(len(self.block.loops), 1)

    def testMergeRefusesUnprovenRows(self):
        """Rows absent on one side are not restored by guessing."""
        result = self.block.SplitLoop(
            '_item.id', ['_item.x'], ['_item.y'],
            first_output_names={'_item.id': '_item.first_id'},
            second_output_names={'_item.id': '_item.second_id'},
            row_map=[(0, None), (None, 1), (2, 2)])
        unproven = []

        def fail_install(*args):
            unproven.append(True)
        self.block._install_merged_loop = fail_install
        merged = self.block.MergeLoops(result)
        self.assertFalse(merged.reversible)
        self.assertEqual(unproven, [])

    def testDuplicatePacketKeysReportCandidates(self):
        block = StarBlock()
        block['_item.id'] = ['1', '1']
        block['_item.x'] = ['a', 'b']
        block['_item.y'] = ['A', 'B']
        block.CreateLoop(['_item.id', '_item.x', '_item.y'])
        result = block.SplitLoop(
            '_item.id', ['_item.x'], ['_item.y'],
            first_output_names={'_item.id': '_item.first_id'},
            second_output_names={'_item.id': '_item.second_id'})
        self.assertEqual(len(result.duplicate_key_groups()), 1)
        merged = block.MergeLoops(result, use_provenance=False)
        self.assertEqual(len(merged.ambiguities), 1)
        self.assertEqual(len(block.loops), 2)

    def testGeneratedTextHasBothLoops(self):
        self.split()
        text = str(self.block)
        self.assertEqual(text.count('loop_'), 2)
        self.assertIn('_item.x', text)
        self.assertIn('_item.y', text)


class DictionaryConstructTestCase(unittest.TestCase):
    """The pullback/filter matcher must not pick rows by first index."""

    def setUp(self):
        self.dic = object.__new__(CifFile.CifFile_module.CifDic)

    def testPartitionProjection(self):
        positions, ambiguities = self.dic._match_pullback_rows(
            ['a', 'a', 'b'], ['a'])
        self.assertEqual(positions, [(0, 0), (1, 0)])
        self.assertEqual(ambiguities, ())

    def testRepeatedBothSidesAreCandidates(self):
        positions, ambiguities = self.dic._match_pullback_rows(
            ['a', 'a'], ['a', 'a'])
        self.assertEqual(positions, [])
        self.assertEqual(len(ambiguities), 1)
        self.assertEqual([a.row for a in ambiguities[0].first_rows], [0, 1])
        self.assertEqual([a.row for a in ambiguities[0].second_rows], [0, 1])

    def testListValuesAreComparable(self):
        positions, ambiguities = self.dic._match_pullback_rows(
            [CifFile.StarFile.StarList(['a', 'b'])],
            [CifFile.StarFile.StarList(['a', 'b'])])
        self.assertEqual(positions, [(0, 0)])
        self.assertEqual(ambiguities, ())


if __name__ == '__main__':
    unittest.main()
