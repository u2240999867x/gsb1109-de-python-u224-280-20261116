# Tests for row-based CIF loop storage (CifFile.LoopRows)
#
# To maximize python3/python2 compatibility
from __future__ import print_function
from __future__ import division
from __future__ import absolute_import

import unittest
try:
    from StringIO import StringIO
except ImportError:
    from io import StringIO

import CifFile
from CifFile import LoopRows, CellKind, scan_loops
from CifFile.LoopRows import (LoopRowsError, IncompleteLoopError,
                              RowKeyError, RowAlignmentError)


BASIC_LOOP = """
data_test
loop_
_a
_b
_c
1 2 3
4 5 6
"""


class RowIdentityTestCase(unittest.TestCase):
    """Row identity follows the token sequence and column count, not
    physical lines."""

    def test_basic_rows(self):
        lr = LoopRows.from_text(BASIC_LOOP)
        self.assertEqual(lr.columns, ('_a', '_b', '_c'))
        self.assertEqual(lr.row_count, 2)
        self.assertTrue(lr.complete)
        self.assertEqual(lr.tail, ())
        self.assertEqual(lr.row(0)['_a'], '1')
        self.assertEqual(lr.row(1)['_c'], '6')

    def test_line_breaks_are_not_row_boundaries(self):
        # one row spread over three physical lines
        spread = "data_t\nloop_\n_a\n_b\n_c\n1\n2\n3\n4\n5\n6\n"
        lr = LoopRows.from_text(spread)
        self.assertEqual(lr.row_count, 2)
        self.assertEqual(lr.row(0).as_dict(), {'_a': '1', '_b': '2', '_c': '3'})
        self.assertEqual(lr.row(1).as_dict(), {'_a': '4', '_b': '5', '_c': '6'})

    def test_multiple_rows_on_one_line(self):
        packed = "data_t\nloop_\n_a\n_b\n_c\n1 2 3 4 5 6\n"
        lr = LoopRows.from_text(packed)
        self.assertEqual(lr.row_count, 2)
        self.assertEqual(lr.row(1)['_a'], '4')

    def test_row_identity_anchored_in_token_sequence(self):
        lr = LoopRows.from_text(BASIC_LOOP)
        for i, row in enumerate(lr):
            self.assertEqual(row.first_token_index, i * lr.column_count)
            self.assertEqual([c.token_index for c in row.cells],
                             [i * 3, i * 3 + 1, i * 3 + 2])
        self.assertEqual(lr.row(0).line, 7)
        self.assertEqual(lr.row(1).line, 8)

    def test_semicolon_text_field_is_one_value(self):
        text = ("data_t\n"
                "loop_\n_a\n_b\n"
                "1\n"
                ";multi line\n"
                "text field\n"
                ";\n"
                "2 x\n")
        lr = LoopRows.from_text(text)
        self.assertEqual(lr.row_count, 2)
        self.assertEqual(lr.row(0)['_b'], 'multi line\ntext field')
        self.assertEqual(lr.row(1)['_a'], '2')
        self.assertEqual(lr.row(1)['_b'], 'x')

    def test_select_loop_by_column_name(self):
        text = ("data_t\n"
                "loop_\n_x\n1\n2\n"
                "loop_\n_y\n_z\n3 4\n")
        lr = LoopRows.from_text(text, loop='_z')
        self.assertEqual(lr.columns, ('_y', '_z'))
        self.assertEqual(lr.row_count, 1)
        self.assertRaises(LoopRowsError, LoopRows.from_text, text,
                          loop='_missing')

    def test_scan_loops_finds_all(self):
        text = ("data_t\n"
                "loop_\n_x\n1\n2\n"
                "loop_\n_y\n_z\n3 4\n")
        loops = scan_loops(text)
        self.assertEqual(len(loops), 2)
        self.assertEqual(loops[0].columns, ('_x',))
        self.assertEqual(loops[1].columns, ('_y', '_z'))


class QuestionDotTestCase(unittest.TestCase):
    """Explicit ? and . keep their meanings; quoted lookalikes are plain
    values."""

    def test_unquoted_markers(self):
        text = "data_t\nloop_\n_a\n_b\n? .\n"
        lr = LoopRows.from_text(text)
        self.assertEqual(lr.row(0).kind('_a'), CellKind.UNKNOWN)
        self.assertEqual(lr.row(0).kind('_b'), CellKind.INAPPLICABLE)
        self.assertTrue(lr.row(0).cell('_a').is_unknown)
        self.assertTrue(lr.row(0).cell('_b').is_inapplicable)

    def test_quoted_lookalikes_are_values(self):
        text = "data_t\nloop_\n_a\n_b\n_c\n_d\n'?' \".\" '?' ?\n"
        lr = LoopRows.from_text(text)
        row = lr.row(0)
        self.assertEqual(row.kind('_a'), CellKind.VALUE)
        self.assertEqual(row.kind('_b'), CellKind.VALUE)
        self.assertEqual(row.kind('_c'), CellKind.VALUE)
        self.assertEqual(row.kind('_d'), CellKind.UNKNOWN)
        self.assertEqual(row['_a'], '?')
        self.assertEqual(row['_b'], '.')
        self.assertTrue(row.cell('_a').quoted)
        self.assertFalse(row.cell('_d').quoted)

    def test_text_field_lookalike_is_value(self):
        text = ("data_t\nloop_\n_a\n;\n?\n;\n")
        lr = LoopRows.from_text(text)
        self.assertEqual(lr.row(0).kind('_a'), CellKind.VALUE)


class IncompleteTailTestCase(unittest.TestCase):
    """A short tail yields the provable complete prefix plus the
    unassigned tail; nothing is guessed."""

    def test_prefix_and_tail(self):
        text = "data_t\nloop_\n_a\n_b\n_c\n1 2 3\n4 5 6\n7\n"
        lr = LoopRows.from_text(text)
        self.assertFalse(lr.complete)
        self.assertEqual(lr.row_count, 2)
        self.assertEqual(len(lr.tail), 1)
        tail_cell = lr.tail[0]
        self.assertEqual(tail_cell.value, '7')
        self.assertEqual(tail_cell.token_index, 6)
        # the tail is not assigned to any column or row
        self.assertEqual(tail_cell.column_name, None)

    def test_require_complete_raises_with_prefix_and_tail(self):
        text = "data_t\nloop_\n_a\n_b\n_c\n1 2 3\n4 5 6\n7 8\n"
        lr = LoopRows.from_text(text)
        try:
            lr.require_complete()
            self.fail('require_complete should raise')
        except IncompleteLoopError as e:
            self.assertEqual(e.complete_rows, 2)
            self.assertEqual(len(e.tail), 2)
            self.assertEqual([c.value for c in e.tail], ['7', '8'])
            self.assertEqual(e.loop_rows.row_count, 2)

    def test_tail_not_distributed_over_columns(self):
        text = "data_t\nloop_\n_a\n_b\n_c\n1 2 3\n4 5 6\n7\n"
        lr = LoopRows.from_text(text)
        cols = lr.to_columns()
        self.assertEqual(cols['_a'], ['1', '4'])
        self.assertEqual(cols['_b'], ['2', '5'])
        self.assertEqual(cols['_c'], ['3', '6'])

    def test_apply_to_block_refuses_incomplete(self):
        text = "data_t\nloop_\n_a\n_b\n_c\n1 2 3\n4 5 6\n7\n"
        lr = LoopRows.from_text(text)
        block = CifFile.CifBlock()
        self.assertRaises(IncompleteLoopError, lr.apply_to_block, block)

    def test_middle_gap_not_guessed(self):
        # 8 tokens for 3 columns: consistent with a missing unmarked value
        # anywhere.  The warehouse reports prefix + tail and offers no
        # placement for the deficit.
        text = "data_t\nloop_\n_a\n_b\n_c\n1 2 3\n4 5 6\n7 8\n"
        lr = LoopRows.from_text(text)
        self.assertEqual(lr.row_count, 2)
        self.assertEqual(len(lr.tail), 2)
        for cell in lr.tail:
            self.assertEqual(cell.column_name, None)
        # no row object exposes the tail tokens
        all_values = [c.value for row in lr for c in row.cells]
        self.assertEqual(all_values, ['1', '2', '3', '4', '5', '6'])


class RowOperationTestCase(unittest.TestCase):
    """Correction, filtering and reordering by confirmed row identity
    keep all columns aligned."""

    def setUp(self):
        text = ("data_t\n"
                "loop_\n_id\n_x\n_y\n"
                "a 1 2\n"
                "b 3 4\n"
                "c 5 6\n")
        self.lr = LoopRows.from_text(text, key_columns='_id')

    def test_correct_by_key(self):
        old = self.lr.correct('b', '_x', '30')
        self.assertEqual(old, '3')
        self.assertEqual(self.lr.row('b').as_dict(),
                         {'_id': 'b', '_x': '30', '_y': '4'})
        cols = self.lr.to_columns()
        self.assertEqual(cols['_id'], ['a', 'b', 'c'])
        self.assertEqual(cols['_x'], ['1', '30', '5'])
        self.assertEqual(cols['_y'], ['2', '4', '6'])

    def test_correct_by_position(self):
        self.lr.correct(0, '_y', '20')
        self.assertEqual(self.lr.row('a')['_y'], '20')

    def test_correct_key_column_rekeys(self):
        self.lr.correct('b', '_id', 'bb')
        self.assertEqual(self.lr.row('bb')['_x'], '3')
        self.assertRaises(RowKeyError, self.lr.row, 'b')

    def test_correct_unknown_key(self):
        self.assertRaises(RowKeyError, self.lr.correct, 'zz', '_x', '0')

    def test_filter_keeps_columns_aligned(self):
        subset = self.lr.filter(lambda row: row['_x'] != '3')
        self.assertEqual(subset.row_count, 2)
        self.assertEqual(subset.to_columns(),
                         {'_id': ['a', 'c'], '_x': ['1', '5'],
                          '_y': ['2', '6']})
        # keys survive filtering
        self.assertEqual(subset.row('c')['_y'], '6')

    def test_reorder_by_key(self):
        reordered = self.lr.reorder(['c', 'a', 'b'])
        self.assertEqual(reordered.to_columns(),
                         {'_id': ['c', 'a', 'b'], '_x': ['5', '1', '3'],
                          '_y': ['6', '2', '4']})
        self.assertEqual(reordered.keys, ['c', 'a', 'b'])

    def test_reorder_requires_permutation(self):
        self.assertRaises(RowAlignmentError, self.lr.reorder, ['a', 'b'])
        self.assertRaises(RowAlignmentError, self.lr.reorder,
                          ['a', 'a', 'b'])
        self.assertRaises(RowKeyError, self.lr.reorder, ['a', 'b', 'zz'])

    def test_reorder_by_position_requires_no_keys(self):
        lr = LoopRows.from_text("data_t\nloop_\n_a\n1\n2\n3\n")
        reordered = lr.reorder([2, 0, 1])
        self.assertEqual(reordered.to_columns(), {'_a': ['3', '1', '2']})
        # with keys set, positional reordering is refused
        self.assertRaises(RowKeyError, self.lr.reorder, [2, 0, 1])


class RowKeyTestCase(unittest.TestCase):

    def test_keys_must_be_unique(self):
        text = "data_t\nloop_\n_id\n_x\na 1\na 2\n"
        self.assertRaises(RowKeyError, LoopRows.from_text, text,
                          key_columns='_id')

    def test_keys_may_not_be_markers(self):
        text = "data_t\nloop_\n_id\n_x\n? 1\n. 2\n"
        self.assertRaises(RowKeyError, LoopRows.from_text, text,
                          key_columns='_id')

    def test_compound_keys(self):
        text = ("data_t\nloop_\n_p\n_q\n_x\n"
                "a x 1\na y 2\nb x 3\n")
        lr = LoopRows.from_text(text, key_columns=['_p', '_q'])
        self.assertEqual(lr.row(('a', 'y'))['_x'], '2')

    def test_row_by_key_requires_keys(self):
        lr = LoopRows.from_text("data_t\nloop_\n_a\n1\n")
        self.assertRaises(RowKeyError, lr.row, 'a')


class CrossVersionTestCase(unittest.TestCase):
    """Row identity crosses file versions only via explicit row keys."""

    old_text = ("data_t\n"
                "loop_\n_id\n_x\n"
                "a 1\n"
                "b 2\n"
                "c 3\n")
    # new version: rows reordered, one removed, one added
    new_text = ("data_t\n"
                "loop_\n_id\n_x\n"
                "c 30\n"
                "a 1\n"
                "d 4\n")

    def test_align_requires_explicit_keys(self):
        old = LoopRows.from_text(self.old_text)
        new = LoopRows.from_text(self.new_text)
        self.assertRaises(RowKeyError, old.align_with, new)

    def test_align_by_key(self):
        old = LoopRows.from_text(self.old_text, key_columns='_id')
        new = LoopRows.from_text(self.new_text, key_columns='_id')
        alignment = new.align_with(old)
        self.assertEqual(alignment.common, frozenset(['a', 'c']))
        self.assertEqual(alignment.only_in_self, frozenset(['d']))
        self.assertEqual(alignment.only_in_other, frozenset(['b']))
        # positions follow keys, not original ordinals: 'c' moved
        self.assertEqual(alignment.self_positions['c'], 0)
        self.assertEqual(alignment.other_positions['c'], 2)
        self.assertEqual(alignment.self_positions['a'], 1)
        self.assertEqual(alignment.other_positions['a'], 0)

    def test_continue_identity_by_reorder(self):
        old = LoopRows.from_text(self.old_text, key_columns='_id')
        new = LoopRows.from_text(self.new_text, key_columns='_id')
        alignment = new.align_with(old)
        # bring the old rows into the new version's order for shared keys
        shared_order = [k for k in new.keys if k in alignment.common]
        old_shared = old.filter(lambda row: row.key in alignment.common)
        old_reordered = old_shared.reorder(shared_order)
        self.assertEqual(old_reordered.keys, ['c', 'a'])
        self.assertEqual(old_reordered.to_columns()['_x'], ['3', '1'])


class BlockRoundTripTestCase(unittest.TestCase):
    """Rows map back to their original datanames."""

    def test_apply_to_block_creates_loop(self):
        lr = LoopRows.from_text(BASIC_LOOP)
        block = CifFile.CifBlock()
        lr.apply_to_block(block)
        self.assertEqual(sorted(block.GetLoopNames('_a')),
                         ['_a', '_b', '_c'])
        self.assertEqual(list(block['_a']), ['1', '4'])
        self.assertEqual(list(block['_c']), ['3', '6'])

    def test_apply_to_existing_loop(self):
        lr = LoopRows.from_text(BASIC_LOOP)
        block = CifFile.CifBlock()
        lr.apply_to_block(block)
        lr.correct(1, '_b', '50')
        lr.apply_to_block(block)
        self.assertEqual(list(block['_b']), ['2', '50'])
        # still one loop with the same membership
        self.assertEqual(sorted(block.GetLoopNames('_b')),
                         ['_a', '_b', '_c'])

    def test_from_block(self):
        cif = CifFile.ReadCif(StringIO(BASIC_LOOP))
        block = cif[cif.keys()[0]]
        lr = LoopRows.from_block(block, '_a')
        self.assertEqual(lr.row_count, 2)
        self.assertTrue(lr.complete)
        self.assertEqual(lr.row(0).as_dict(),
                         {'_a': '1', '_b': '2', '_c': '3'})

    def test_from_block_not_looped(self):
        cif = CifFile.ReadCif(StringIO("data_t\n_a 1\n"))
        block = cif[cif.keys()[0]]
        self.assertRaises(LoopRowsError, LoopRows.from_block, block, '_a')

    def test_round_trip_preserves_markers(self):
        text = "data_t\nloop_\n_a\n_b\n? .\n1 2\n"
        lr = LoopRows.from_text(text)
        block = CifFile.CifBlock()
        lr.apply_to_block(block)
        lr2 = LoopRows.from_block(block, '_a')
        self.assertEqual(lr2.row(0).kind('_a'), CellKind.UNKNOWN)
        self.assertEqual(lr2.row(0).kind('_b'), CellKind.INAPPLICABLE)

    def test_partial_column_presence_refused(self):
        lr = LoopRows.from_text(BASIC_LOOP)
        block = CifFile.CifBlock()
        block['_a'] = ['x']
        self.assertRaises(LoopRowsError, lr.apply_to_block, block)


class Cif2GrammarTestCase(unittest.TestCase):

    def test_cif2_quoted_and_list_values(self):
        text = ("data_t\n"
                "loop_\n_a\n_b\n"
                "'''?''' [1 2 3]\n"
                "x y\n")
        lr = LoopRows.from_text(text, grammar='2.0')
        self.assertEqual(lr.row_count, 2)
        self.assertEqual(lr.row(0).kind('_a'), CellKind.VALUE)
        self.assertEqual(lr.row(0)['_a'], '?')
        # a bracket expression is a single value token
        self.assertEqual(lr.row(0)['_b'], '[1 2 3]')
        self.assertEqual(lr.row(1)['_a'], 'x')

    def test_cif2_unquoted_markers(self):
        text = "data_t\nloop_\n_a\n_b\n? .\n"
        lr = LoopRows.from_text(text, grammar='2.0')
        self.assertEqual(lr.row(0).kind('_a'), CellKind.UNKNOWN)
        self.assertEqual(lr.row(0).kind('_b'), CellKind.INAPPLICABLE)

    def test_bad_grammar(self):
        self.assertRaises(LoopRowsError, LoopRows.from_text, "data_t\n",
                          grammar='9.9')


if __name__ == '__main__':
    unittest.main()
