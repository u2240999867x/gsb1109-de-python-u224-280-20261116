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
Implementation of NEWICK-schema tree iterator.
"""

from dendropy.dataio import ioservice
from dendropy.dataio import newickreader
from dendropy.dataio import nexusprocessing
from dendropy.dataio import treerecords

def _source_text(stream, start_offset, end_offset):
    try:
        position = stream.tell()
        seekable = stream.seekable()
    except (AttributeError, OSError, IOError):
        return None
    if not seekable:
        return None
    try:
        stream.seek(start_offset)
        return stream.read(end_offset - start_offset)
    finally:
        stream.seek(position)

def _digest_at(nexus_tokenizer, offset):
    """
    Exact prefix digest at ``offset``, reusing the tokenizer's incremental
    digest when the offset is a recorded boundary.
    """
    if offset == nexus_tokenizer.last_semicolon_offset:
        if nexus_tokenizer.last_semicolon_digest is not None:
            return nexus_tokenizer.last_semicolon_digest
    return nexus_tokenizer.digest_at_offset(offset)

class NewickTreeDataYielder(ioservice.TreeDataYielder):

    def __init__(self,
            files=None,
            taxon_namespace=None,
            tree_type=None,
            records=False,
            resume_from=None,
            **kwargs):
        r"""

        Parameters
        ----------
        files : iterable of sources
            Iterable of sources, which can either be strings specifying file
            paths or file-like objects open for reading. If a source element is
            a stringm then it is assumed to be a path to a file. Otherwise, the
            source is assumed to be a file-like object.
        taxon_namespace : |TaxonNamespace| instance
            The operational taxonomic unit concept namespace to use to manage
            taxon definitions.
        \*\*kwargs : keyword arguments
            These will be passed directly to the base `newickreader.NexusReader`
            class. See `newickreader.NexusReader` for details.
        """
        ioservice.TreeDataYielder.__init__(self,
                files=files,
                taxon_namespace=taxon_namespace,
                tree_type=tree_type,
                records=records,
                resume_from=resume_from)
        self.newick_reader = newickreader.NewickReader(**kwargs)
        self._checkpoint = None
        self._incomplete_tail = None
        self._resume_consumed = False

    ###########################################################################
    ## Implementation of DataYielder interface

    def _yield_items_from_stream(self, stream):
        nexus_tokenizer = nexusprocessing.NexusTokenizer(stream,
                preserve_unquoted_underscores=self.newick_reader.preserve_unquoted_underscores)
        taxon_symbol_mapper = nexusprocessing.NexusTaxonSymbolMapper(
                taxon_namespace=self.attached_taxon_namespace,
                enable_lookup_by_taxon_number=False,
                case_sensitive=self.newick_reader.case_sensitive_taxon_labels)
        record_index = 0
        resume = self._resume_checkpoint
        if resume is not None and not self._resume_consumed:
            nexus_tokenizer.set_position(
                    stream,
                    char_offset=resume.next_char_offset,
                    line_num=resume.next_line_num,
                    column_num=resume.next_column_num,
                    stream_position=resume.next_stream_position)
            record_index = resume.next_record_index
            self._resume_consumed = True
        while True:
            tree_start_position = None
            try:
                if (nexus_tokenizer.current_token is not None
                        and nexus_tokenizer.current_token != ";"):
                    # The parser leaves the first token of the next statement
                    # in hand; use its own recorded start.
                    tree_start_position = nexus_tokenizer.token_position
                else:
                    tree_start_position = nexus_tokenizer.next_position
                tree = self.newick_reader._parse_tree_statement(
                        nexus_tokenizer=nexus_tokenizer,
                        tree_factory=self.tree_factory,
                        taxon_symbol_map_fn=taxon_symbol_mapper.require_taxon_for_symbol)
            except Exception as exc:
                if not nexus_tokenizer.is_eof():
                    raise
                self._checkpoint = treerecords.CollectionCheckpoint(
                        next_record_index=record_index,
                        next_tree_index=record_index,
                        next_char_offset=tree_start_position[0],
                        next_line_num=tree_start_position[1],
                        next_column_num=tree_start_position[2],
                        next_stream_position=tree_start_position[3],
                        source_file=self.current_file_name,
                        file_index=self.current_file_index or 0,
                        last_record_index=record_index - 1,
                        source_digest=_digest_at(
                            nexus_tokenizer, tree_start_position[0]))
                self._incomplete_tail = treerecords.TreeRecord(
                        tree=None,
                        record_index=record_index,
                        source_file=self.current_file_name,
                        source_range=treerecords.SourceRange(
                            start_offset=tree_start_position[0],
                            end_offset=tree_start_position[0]),
                        is_complete=False,
                        raw_text=None,
                        error="{}: {}".format(type(exc).__name__, exc))
                self._stop_iteration = True
                return
            if tree is None:
                break
            end_position = (
                    self.newick_reader.tree_statement_end_position
                    or nexus_tokenizer.next_position)
            record = treerecords.TreeRecord(
                    tree=tree,
                    record_index=record_index,
                    source_file=self.current_file_name,
                    source_range=treerecords.SourceRange(
                        start_offset=tree_start_position[0],
                        end_offset=end_position[0],
                        start_line=tree_start_position[1],
                        start_column=tree_start_position[2],
                        end_line=end_position[1],
                        end_column=end_position[2]),
                    label=tree.label,
                    raw_text=_source_text(
                        stream, tree_start_position[0], end_position[0]))
            tree.tree_record = record
            tree.record_index = record.record_index
            tree.source_range = record.source_range
            tree.source_file = record.source_file
            self._checkpoint = treerecords.CollectionCheckpoint(
                    next_record_index=record_index + 1,
                    next_tree_index=record_index + 1,
                    next_char_offset=end_position[0],
                    next_line_num=end_position[1],
                    next_column_num=end_position[2],
                    next_stream_position=end_position[3],
                    source_file=self.current_file_name,
                    file_index=self.current_file_index or 0,
                    last_record_index=record_index,
                    source_digest=_digest_at(nexus_tokenizer, end_position[0]))
            record_index += 1
            yield tree

    def _finalize_checkpoint(self):
        if self._checkpoint is not None and self._incomplete_tail is None:
            self._checkpoint.complete = True

    @property
    def checkpoint(self):
        return getattr(self, "_checkpoint", None)

    @property
    def tail(self):
        return getattr(self, "_incomplete_tail", None)

    @property
    def incomplete_tail(self):
        return self.tail
