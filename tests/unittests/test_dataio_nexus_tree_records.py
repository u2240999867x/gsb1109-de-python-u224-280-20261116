#! /usr/bin/env python
# -*- coding: utf-8 -*-

##############################################################################
##  DendroPy Phylogenetic Computing Library.
##
##  Copyright 2010-2026 Jeet Sukumaran, Mark T. Holder, and Matthew Andres Moreno.
##  All rights reserved.
##
##  See "LICENSE.rst" for terms and conditions of usage.
##
##  If you use this work or any portion thereof in published work,
##  please cite it as:
##
##     Moreno, M. A., Holder, M. T., & Sukumaran, J. (2024). DendroPy 5: a
##     mature Python library for phylogenetic computing. Journal of Open
##     Source Software, 9(101), 6943, https://doi.org/10.21105/joss.06943
##
##############################################################################

"""
Tests for independently deliverable and resumable NEXUS tree records.
"""

from io import StringIO
import os
import sys
import tempfile
import unittest

import dendropy
from dendropy.dataio import nexusreader
from dendropy.dataio import nexusyielder
from dendropy.dataio import treerecords

sys.path.insert(0, os.path.dirname(__file__))
from support import dendropytest


MULTI_BLOCK_NEXUS = """#NEXUS
Begin taxa;
    Dimensions ntax=4;
    Taxlabels A B C D;
End;
Begin trees;
    Translate 1 A, 2 B, 3 C, 4 D;
    Tree t1 = [&R] ((1,2),(3,4));
End;
Begin trees;
    Translate 1 D, 2 C, 3 B, 4 A;
    Tree t2 = [&R] ((1,2),(3,4));
End;
"""


def yield_nexus(data,
        taxon_namespace=None,
        records=False,
        resume_from=None):
    if taxon_namespace is None:
        taxon_namespace = dendropy.TaxonNamespace()
    return nexusyielder.NexusTreeDataYielder(
            files=[StringIO(data)],
            taxon_namespace=taxon_namespace,
            tree_type=dendropy.Tree,
            records=records,
            resume_from=resume_from)


class TranslateScopeTestCase(dendropytest.ExtendedTestCase):

    def test_each_tree_carries_its_own_translation(self):
        yielder = yield_nexus(MULTI_BLOCK_NEXUS, records=True)
        records = list(yielder)
        self.assertEqual(len(records), 2)
        self.assertEqual(
                dict(records[0].translate),
                {"1": "A", "2": "B", "3": "C", "4": "D"})
        self.assertEqual(
                dict(records[1].translate),
                {"1": "D", "2": "C", "3": "B", "4": "A"})
        self.assertEqual(records[0].newick, "[&R] ((A,B),(C,D));")
        self.assertEqual(records[1].newick, "[&R] ((D,C),(B,A));")
        self.assertEqual([r.block_index for r in records], [0, 1])

    def test_same_tokens_do_not_borrow_previous_table(self):
        data = """#NEXUS
Begin trees;
    Translate 1 A, 2 B;
    Tree t1 = ((1,2));
End;
Begin trees;
    Translate 1 D, 2 C;
    Tree t2 = ((1,2));
End;
"""
        records = list(yield_nexus(data, records=True))
        self.assertEqual(dict(records[0].translate), {"1": "A", "2": "B"})
        self.assertEqual(dict(records[1].translate), {"1": "D", "2": "C"})
        self.assertEqual(records[1].newick, "((D,C));")

    def test_translation_redefined_within_block(self):
        data = """#NEXUS
Begin trees;
    Translate 1 A, 2 B;
    Tree t1 = ((1,2));
    Translate 1 D, 2 C;
    Tree t2 = ((1,2));
End;
"""
        records = list(yield_nexus(data, records=True))
        self.assertEqual(records[0].newick, "((A,B));")
        self.assertEqual(records[1].newick, "((D,C));")
        self.assertEqual(dict(records[1].translate), {"1": "D", "2": "C"})

    def test_record_translation_snapshot_is_independent(self):
        records = list(yield_nexus(MULTI_BLOCK_NEXUS, records=True))
        snapshot = dict(records[0].translate)
        records[0].translate["1"] = "MUTATED"
        self.assertEqual(snapshot, {"1": "A", "2": "B", "3": "C", "4": "D"})
        self.assertEqual(
                dict(records[1].translate),
                {"1": "D", "2": "C", "3": "B", "4": "A"})


class SourceRangeTestCase(dendropytest.ExtendedTestCase):

    def test_record_range_indexes_original_tree_statement(self):
        records = list(yield_nexus(MULTI_BLOCK_NEXUS, records=True))
        for record in records:
            observed = MULTI_BLOCK_NEXUS[
                    record.source_range.start_offset:record.source_range.end_offset]
            self.assertTrue(observed.startswith("Tree "))
            self.assertTrue(observed.rstrip().endswith(";"))
            self.assertEqual(observed.count(";"), 1)

    def test_block_and_translate_ranges_are_preserved(self):
        record = list(yield_nexus(MULTI_BLOCK_NEXUS, records=True))[0]
        block_text = MULTI_BLOCK_NEXUS[
                record.block_range.start_offset:record.block_range.end_offset]
        self.assertTrue(block_text.startswith("Begin trees;"))
        self.assertTrue(block_text.rstrip().endswith("End;"))
        translate_text = MULTI_BLOCK_NEXUS[
                record.translate_range.start_offset:record.translate_range.end_offset]
        self.assertTrue(translate_text.startswith("Translate "))
        self.assertTrue(translate_text.rstrip().endswith(";"))

    def test_used_translate_only_lists_tokens_in_the_tree(self):
        data = """#NEXUS
Begin trees;
    Translate 1 A, 2 B, 3 C;
    Tree t1 = ((1));
End;
"""
        record = list(yield_nexus(data, records=True))[0]
        self.assertEqual(dict(record.translate), {"1": "A", "2": "B", "3": "C"})
        self.assertEqual(dict(record.used_translate), {"1": "A"})

    def test_default_iteration_still_yields_trees_with_attached_record(self):
        yielder = yield_nexus(MULTI_BLOCK_NEXUS)
        trees = list(yielder)
        self.assertTrue(all(isinstance(t, dendropy.Tree) for t in trees))
        self.assertEqual(trees[1].translate["1"], "D")
        self.assertEqual(trees[1].source_range.start_offset,
                trees[1].tree_record.source_range.start_offset)


class TailHandlingTestCase(dendropytest.ExtendedTestCase):

    def _collect(self, data, records=False):
        yielder = yield_nexus(data, records=records)
        return yielder, list(yielder)

    def test_unterminated_tail_is_not_committed(self):
        data = """#NEXUS
Begin trees;
    Translate 1 A, 2 B;
    Tree t1 = ((1,2));
    Tree t2 = ((1,2)
"""
        yielder, trees = self._collect(data)
        self.assertEqual([t.label for t in trees], ["t1"])
        self.assertIsNotNone(yielder.tail)
        self.assertFalse(yielder.tail.is_complete)
        self.assertIsNone(yielder.tail.tree)

    def test_tail_covers_the_partial_statement(self):
        data = """#NEXUS
Begin trees;
    Tree t1 = ((A,B));
    Tree t2 = ((A,B)
"""
        yielder, trees = self._collect(data)
        tail_text = data[
                yielder.tail.source_range.start_offset:yielder.tail.source_range.end_offset]
        self.assertEqual(tail_text.rstrip(), "Tree t2 = ((A,B)")

    def test_missing_semicolon_before_end_is_a_tail_not_a_tree(self):
        data = """#NEXUS
Begin trees;
    Tree t1 = ((A,B),(C,D))
End;
"""
        yielder, trees = self._collect(data)
        self.assertEqual(trees, [])
        self.assertIsNotNone(yielder.tail)

    def test_malformed_statement_in_middle_still_raises(self):
        data = """#NEXUS
Begin trees;
    THIS IS NOT A TREE
    Tree t2 = ((A,B));
End;
"""
        yielder = yield_nexus(data)
        with self.assertRaises(nexusreader.NexusReader.NexusReaderError):
            list(yielder)

    def test_balanced_but_unterminated_at_eof_is_tail(self):
        data = """#NEXUS
Begin trees;
    Tree t1 = ((A,B),(C,D))
"""
        yielder, trees = self._collect(data)
        self.assertEqual(trees, [])
        self.assertIsNotNone(yielder.tail)


class ResumeTestCase(dendropytest.ExtendedTestCase):

    def test_resume_at_unterminated_tail_reads_it_once(self):
        partial = """#NEXUS
Begin taxa;
    Dimensions ntax=4;
    Taxlabels A B C D;
End;
Begin trees;
    Translate 1 A, 2 B, 3 C, 4 D;
    Tree t1 = ((1,2));
    Tree t2 = ((1,2),(3,4)
"""
        yielder = yield_nexus(partial, records=True)
        first = list(yielder)
        self.assertEqual([r.label for r in first], ["t1"])
        checkpoint = yielder.checkpoint
        self.assertIsNotNone(checkpoint)
        self.assertTrue(checkpoint.in_trees_block)
        self.assertEqual(checkpoint.next_record_index, 1)

        completed = partial + ");\nEnd;\n"
        resumed = yield_nexus(completed,
                records=True,
                resume_from=checkpoint.to_dict())
        records = list(resumed)
        self.assertEqual([r.label for r in records], ["t2"])
        self.assertEqual(records[0].record_index, 1)
        self.assertEqual(records[0].newick, "((A,B),(C,D));")

    def test_resume_completed_source_yields_nothing(self):
        data = MULTI_BLOCK_NEXUS
        yielder = yield_nexus(data, records=True)
        list(yielder)
        resumed = yield_nexus(data,
                records=True,
                resume_from=yielder.checkpoint)
        self.assertEqual(list(resumed), [])

    def test_resume_rejects_modified_source_prefix(self):
        partial = """#NEXUS
Begin trees;
    Translate 1 A, 2 B;
    Tree t1 = ((1,2));
    Tree t2 = ((1,2)
"""
        yielder = yield_nexus(partial, records=True)
        list(yielder)
        checkpoint = yielder.checkpoint
        modified = partial.replace("Translate 1 A", "Translate 1 Z")
        modified += ");\nEnd;\n"
        resumed = yield_nexus(modified,
                records=True,
                resume_from=checkpoint)
        with self.assertRaises(treerecords.ResumeCheckpointError):
            list(resumed)

    def test_resume_with_fresh_taxon_namespace(self):
        partial = """#NEXUS
Begin trees;
    Translate 1 A, 2 B;
    Tree t1 = ((1,2));
    Tree t2 = ((1,2)
"""
        yielder = yield_nexus(partial, records=True)
        list(yielder)
        completed = partial + ");\nEnd;\n"
        taxon_namespace = dendropy.TaxonNamespace()
        resumed = yield_nexus(completed,
                taxon_namespace=taxon_namespace,
                records=True,
                resume_from=yielder.checkpoint.to_dict())
        records = list(resumed)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].newick, "((A,B));")
        self.assertEqual(set(t.label for t in taxon_namespace), {"A", "B"})

    def test_resume_across_multiple_files(self):
        first = "#NEXUS\nBegin trees;\nTree a = ((1,2));\nEnd;\n"
        partial_second = """#NEXUS
Begin trees;
    Tree b = ((1,2));
    Tree c = ((1,2)
"""
        paths = []
        try:
            for data in (first, partial_second):
                handle, path = tempfile.mkstemp(suffix=".nex")
                os.close(handle)
                with open(path, "w") as dest:
                    dest.write(data)
                paths.append(path)
            taxon_namespace = dendropy.TaxonNamespace()
            yielder = nexusyielder.NexusTreeDataYielder(
                    files=paths,
                    taxon_namespace=taxon_namespace,
                    tree_type=dendropy.Tree,
                    records=True)
            self.assertEqual([r.label for r in yielder], ["a", "b"])
            self.assertIsNotNone(yielder.tail)
            checkpoint = yielder.checkpoint
            self.assertEqual(checkpoint.file_index, 1)

            with open(paths[1], "w") as dest:
                dest.write(partial_second + ");\nEnd;\n")
            resumed = nexusyielder.NexusTreeDataYielder(
                    files=paths,
                    taxon_namespace=taxon_namespace,
                    tree_type=dendropy.Tree,
                    records=True,
                    resume_from=checkpoint.to_dict())
            records = list(resumed)
            self.assertEqual([r.label for r in records], ["c"])
            self.assertEqual(records[0].record_index, 2)
        finally:
            for path in paths:
                os.remove(path)


class NewickResumeTestCase(dendropytest.ExtendedTestCase):

    def test_newick_tail_and_resume(self):
        source = "((A,B),(C,D));((A,C),(B,D));((A,B),(C,D)"
        taxon_namespace = dendropy.TaxonNamespace()
        yielder = dendropy.Tree.yield_from_files(
                files=[StringIO(source)],
                schema="newick",
                taxon_namespace=taxon_namespace)
        trees = list(yielder)
        self.assertEqual(len(trees), 2)
        self.assertIsNotNone(yielder.tail)
        checkpoint = yielder.checkpoint

        resumed = dendropy.Tree.yield_from_files(
                files=[StringIO(source + ");")],
                schema="newick",
                taxon_namespace=taxon_namespace,
                resume_from=checkpoint)
        self.assertEqual(
                [t.as_string(schema="newick").strip() for t in resumed],
                ["((A,B),(C,D));"])


class RecordSerializationTestCase(dendropytest.ExtendedTestCase):

    def test_as_dict_is_json_compatible(self):
        import json
        record = list(yield_nexus(MULTI_BLOCK_NEXUS, records=True))[0]
        payload = record.as_dict()
        self.assertEqual(payload["tree_label"], "t1")
        self.assertEqual(payload["translate"], {"1": "A", "2": "B", "3": "C", "4": "D"})
        self.assertEqual(payload["newick"], "[&R] ((A,B),(C,D));")
        json.dumps(payload)

    def test_raw_text_matches_source_range(self):
        record = list(yield_nexus(MULTI_BLOCK_NEXUS, records=True))[1]
        self.assertEqual(
                record.raw_text,
                MULTI_BLOCK_NEXUS[
                    record.source_range.start_offset:record.source_range.end_offset])

    def test_completed_checkpoint_is_marked_complete(self):
        yielder = yield_nexus(MULTI_BLOCK_NEXUS, records=True)
        list(yielder)
        self.assertTrue(yielder.checkpoint.complete)
        self.assertIsNone(yielder.tail)

    def test_checkpoint_round_trip(self):
        yielder = yield_nexus(MULTI_BLOCK_NEXUS, records=True)
        list(yielder)
        checkpoint = yielder.checkpoint
        restored = treerecords.CollectionCheckpoint.from_dict(
                checkpoint.to_dict())
        self.assertEqual(restored.to_dict(), checkpoint.to_dict())

if __name__ == "__main__":
    unittest.main()
