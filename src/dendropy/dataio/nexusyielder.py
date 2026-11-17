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
Implementation of NEXUS-schema tree iterator.
"""

import collections
import os

from dendropy.dataio import ioservice
from dendropy.dataio import nexusreader
from dendropy.dataio import newickreader
from dendropy.dataio import treerecords
from dendropy.dataio import tokenizer

class NexusTreeDataYielder(
        ioservice.TreeDataYielder,
        nexusreader.NexusReader):

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
            a string (``isinstance(i,str) == True``), then it is assumed to be
            a path to a file. Otherwise, the source is assumed to be a file-like
            object.
        taxon_namespace : |TaxonNamespace| instance
            The operational taxonomic unit concept namespace to use to manage
            taxon definitions.
        records : bool
            If |True|, iteration yields :class:`~dendropy.dataio.treerecords.TreeRecord`
            objects instead of bare |Tree| objects.  Records are independently
            interpretable: each carries the source range of its tree and the
            translation table that was actually in effect for that tree.
        resume_from : |CollectionCheckpoint| or dict
            Checkpoint produced by a previous iteration.  Iteration continues
            at the next uncommitted tree.  A tree at the source tail that was
            not terminated is never committed; the checkpoint continues to
            point at its start so that the tail can be re-read once completed.
        \*\*kwargs : keyword arguments
            These will be passed directly to the base `nexusreader.NexusReader`
            class. See `nexusreader.NexusReader` for details.
        """
        ioservice.TreeDataYielder.__init__(self,
                files=files,
                taxon_namespace=taxon_namespace,
                tree_type=tree_type,
                records=records,
                resume_from=resume_from)
        self.assume_newick_if_not_nexus = kwargs.pop("assume_newick_if_not_nexus", False)
        kwargs["attached_taxon_namespace"] = self.attached_taxon_namespace
        kwargs.setdefault("statement_terminator_tokens",
                ("END", "ENDBLOCK", "TREE"))
        nexusreader.NexusReader.__init__(self, **kwargs)
        self.exclude_chars = True
        self.exclude_trees = False
        self._incomplete_tail = None
        self._checkpoint = None
        self._block_index_counter = -1
        self._tree_index_counter = 0
        self._resume_consumed = False

    ###########################################################################
    ## Implementation of DataYielder interface

    def _yield_items_from_stream(self, stream):
        if self._nexus_tokenizer is None:
            self.create_tokenizer(stream,
                preserve_unquoted_underscores=self.preserve_underscores)
        else:
            self._nexus_tokenizer.set_stream(stream)
        is_resume = self._apply_resume_if_pending(stream)
        if is_resume:
            resume_state = self._resume_checkpoint
            self._resume_checkpoint = None
            if resume_state.in_trees_block:
                # Continue inside the TREES block the cursor points at.
                for tree in self._yield_from_trees_block(resume_state=resume_state):
                    yield tree
                if self._nexus_tokenizer.is_eof():
                    return
            # Otherwise the cursor is between TREES blocks: fall through to
            # the normal scan, which starts at the next BEGIN.
            token = None
        else:
            token = self._nexus_tokenizer.next_token()
            if token.upper() != "#NEXUS":
                if self.assume_newick_if_not_nexus:
                    taxon_symbol_mapper = self._get_taxon_symbol_mapper(
                            taxon_namespace=self.attached_taxon_namespace,
                            enable_lookup_by_taxon_number=False,
                            )
                    while True:
                        tree = self._build_tree_from_newick_tree_string(
                                tree_factory=self.tree_factory,
                                taxon_symbol_mapper=taxon_symbol_mapper)
                        if tree is None:
                            break
                        yield tree
                    return
                else:
                    raise self._nexus_error("Expecting '#NEXUS', but found '{}'".format(token),
                            nexusreader.NexusReader.NotNexusFileError)
        while not self._nexus_tokenizer.is_eof():
            token = self._nexus_tokenizer.next_token_ucase()
            while token != None and token != 'BEGIN' and not self._nexus_tokenizer.is_eof():
                token = self._nexus_tokenizer.next_token_ucase()
            self._nexus_tokenizer.process_and_clear_comments_for_item(
                    self._global_annotations_target,
                    self.extract_comment_metadata)
            begin_position = self._nexus_tokenizer.token_position
            token = self._nexus_tokenizer.next_token_ucase()
            if token == 'TAXA':
                self._parse_taxa_block()
            elif token == 'TREES':
                for tree in self._yield_from_trees_block(
                        block_start_position=begin_position):
                    yield tree
            elif token == 'BEGIN':
                raise self._nexus_error("'BEGIN' found without completion of previous block",
                        nexusreader.NexusReader.IncompleteBlockError)
            else:
                # unknown block
                token = self._consume_to_end_of_block(token)

    ###########################################################################
    ## Supporting Functions

    def _yield_from_trees_block(self, resume_state=None,
            block_start_position=None):
        """
        Expectations:
            - current token: "TREES" [part of "BEGIN TREES"] unless resuming
              from ``resume_state``, in which case the tokenizer is positioned
              immediately after the last committed tree statement.
        """
        if resume_state is None:
            token = self._nexus_tokenizer.cast_current_token_to_ucase()
            if token != "TREES":
                raise self._nexus_error("Expecting 'TREES' token, but instead found '{}'".format(token))
            if self.exclude_trees:
                self._consume_to_end_of_block(self._nexus_tokenizer.current_token)
                return
            if block_start_position is None:
                block_start_position = self._nexus_tokenizer.token_position
                if block_start_position is not None:
                    # Range starts at the 'BEGIN' that opens this TREES block.
                    block_start_position = self._block_begin_position(block_start_position)
            self._nexus_tokenizer.skip_to_semicolon() # move past "BEGIN TREES" command
            self._block_index_counter += 1
            block_index = self._block_index_counter
            link_title = None
            taxon_namespace = None
            taxon_symbol_mapper = None
            block_title = None
            block_range = treerecords.SourceRange(
                    start_offset=block_start_position[0],
                    end_offset=None,
                    start_line=block_start_position[1],
                    start_column=block_start_position[2])
            translate_labels = None
            translate_taxon_map = None
            translate_range = None
            tree_index_in_block = 0
        else:
            block_index = resume_state.block_index
            if block_index is None:
                block_index = self._block_index_counter + 1
                self._block_index_counter = block_index
            else:
                self._block_index_counter = max(self._block_index_counter, block_index)
            link_title = resume_state.link_title
            taxon_namespace = None
            block_title = resume_state.block_title
            block_range = resume_state.block_range
            translate_range = resume_state.translate_range
            translate_labels = collections.OrderedDict(resume_state.translate or ())
            translate_taxon_map = None
            tree_index_in_block = resume_state.tree_index_in_block
            taxon_symbol_mapper = None
            if translate_labels:
                if taxon_namespace is None:
                    taxon_namespace = self._get_taxon_namespace(link_title)
                taxon_symbol_mapper = self._restore_taxon_symbol_mapper(
                        taxon_namespace=taxon_namespace,
                        translation=translate_labels)
                translate_taxon_map = dict(taxon_symbol_mapper.token_taxon_map)
            token = ""
        pending_token = None
        while ((not self._nexus_tokenizer.is_eof())
                and token is not None
                and token != 'END'
                and token != 'ENDBLOCK'):
            if pending_token is not None:
                token = pending_token
                pending_token = None
            else:
                token = self._nexus_tokenizer.next_token_ucase()
            if token is None or token == 'END' or token == 'ENDBLOCK':
                break
            if token == 'LINK':
                link_title = self._parse_link_statement().get("taxa")
            elif token == 'TITLE':
                block_title = self._parse_title_statement()
            elif token == 'TRANSLATE':
                translate_start_position = self._nexus_tokenizer.token_position
                if taxon_namespace is None:
                    taxon_namespace = self._get_taxon_namespace(link_title)
                taxon_symbol_mapper = self._parse_translate_statement(taxon_namespace)
                translate_range = self._range_from_positions(
                        translate_start_position,
                        self._nexus_tokenizer.last_semicolon_position)
                translate_labels = self._snapshot_translation(taxon_symbol_mapper)
                translate_taxon_map = dict(taxon_symbol_mapper.token_taxon_map)
            elif token == 'TREE':
                if taxon_namespace is None:
                    taxon_namespace = self._get_taxon_namespace(link_title)
                if taxon_symbol_mapper is None:
                    taxon_symbol_mapper = self._get_taxon_symbol_mapper(taxon_namespace=taxon_namespace)
                    translate_labels = collections.OrderedDict()
                    translate_taxon_map = dict(taxon_symbol_mapper.token_taxon_map)
                pre_tree_comments = self._nexus_tokenizer.pull_captured_comments()
                tree_factory = self.tree_factory
                while True:
                    ## After the following, the current token
                    ## will be the token immediately following
                    ## the terminating semi-colon of a tree
                    ## statement. Typically, this will be
                    ## 'TREE' if there is another tree, or
                    ## 'END'/'ENDBLOCK'.
                    tree_start_position = self._nexus_tokenizer.token_position
                    try:
                        tree = self._parse_tree_statement(
                                tree_factory=tree_factory,
                                taxon_symbol_mapper=taxon_symbol_mapper)
                    except Exception as exc:
                        if self._is_truncated_tree_error(exc):
                            self._set_incomplete_tail(
                                    start_position=tree_start_position,
                                    block_index=block_index,
                                    tree_index_in_block=tree_index_in_block,
                                    block_title=block_title,
                                    link_title=link_title,
                                    translation=translate_labels,
                                    translate_taxon_map=translate_taxon_map,
                                    translate_range=translate_range,
                                    block_range=block_range,
                                    error=exc)
                            self._close_block_range(block_range)
                            self._stop_iteration = True
                            return
                        raise
                    if tree is None:
                        self._set_incomplete_tail(
                                start_position=tree_start_position,
                                block_index=block_index,
                                tree_index_in_block=tree_index_in_block,
                                block_title=block_title,
                                link_title=link_title,
                                translation=translate_labels,
                                translate_taxon_map=translate_taxon_map,
                                translate_range=translate_range,
                                block_range=block_range,
                                error=None)
                        self._close_block_range(block_range)
                        self._stop_iteration = True
                        return
                    end_position = self._tree_end_position()
                    record = self._make_tree_record(
                            tree=tree,
                            start_position=tree_start_position,
                            end_position=end_position,
                            block_index=block_index,
                            tree_index_in_block=tree_index_in_block,
                            block_title=block_title,
                            link_title=link_title,
                            translation=translate_labels,
                            translate_taxon_map=translate_taxon_map,
                            translate_range=translate_range,
                            block_range=block_range,
                            used_translation=self._used_translation(
                                taxon_symbol_mapper, translate_labels))
                    tree_index_in_block += 1
                    self._tree_index_counter = record.record_index + 1
                    yield tree
                    if self._nexus_tokenizer.is_eof() or not self._nexus_tokenizer.current_token:
                        break
                    if self._nexus_tokenizer.cast_current_token_to_ucase() != "TREE":
                        pending_token = self._nexus_tokenizer.cast_current_token_to_ucase()
                        token = pending_token
                        break
            elif token == 'BEGIN':
                raise self._nexus_error("'BEGIN' found without completion of previous block",
                        nexusreader.NexusReader.IncompleteBlockError)
        self._nexus_tokenizer.skip_to_semicolon() # move past END command
        self._close_block_range(block_range)
        self._set_block_checkpoint(
                block_index=block_index,
                block_title=block_title,
                tree_index_in_block=tree_index_in_block)

    ###########################################################################
    ## Record/checkpoint support

    def _source_digest(self, end_offset):
        tokenizer_ = self._nexus_tokenizer
        if tokenizer_ is None:
            return None
        if end_offset == tokenizer_.last_semicolon_offset:
            if tokenizer_.last_semicolon_digest is not None:
                return tokenizer_.last_semicolon_digest
        return tokenizer_.digest_at_offset(end_offset)

    def _block_begin_position(self, block_keyword_position):
        """
        Returns the position of the ``BEGIN`` keyword that precedes the block
        keyword at ``block_keyword_position``, when that keyword is available.
        """
        if block_keyword_position is None:
            return None
        begin_token = getattr(self._nexus_tokenizer, "previous_token", None)
        begin_position = getattr(self._nexus_tokenizer, "previous_token_position", None)
        if begin_token == "BEGIN" and begin_position is not None:
            return begin_position
        return block_keyword_position

    def _snapshot_translation(self, taxon_symbol_mapper):
        translation = collections.OrderedDict()
        for token, taxon in taxon_symbol_mapper.token_taxon_map.items():
            translation[token] = taxon.label
        return translation

    def _restore_taxon_symbol_mapper(self, taxon_namespace, translation):
        taxon_symbol_mapper = self._get_taxon_symbol_mapper(
                taxon_namespace=taxon_namespace,
                enable_lookup_by_taxon_number=False)
        for token, label in translation.items():
            taxon = taxon_symbol_mapper.require_taxon_for_symbol(label)
            taxon_symbol_mapper.add_translate_token(token, taxon)
        return taxon_symbol_mapper

    def _used_translation(self, taxon_symbol_mapper, translation):
        if not translation:
            return collections.OrderedDict()
        used_taxa = getattr(self.newick_reader, "last_tree_seen_taxa", None)
        if used_taxa is None:
            used_taxa = set()
        used = collections.OrderedDict()
        for token, label in translation.items():
            taxon = taxon_symbol_mapper.token_taxon_map.get(token)
            if taxon is not None and taxon in used_taxa:
                used[token] = label
        return used

    def _source_text(self, start_offset, end_offset):
        """
        Returns the original source text for a range, when the stream is
        seekable and still open.  This never affects parsing.
        """
        stream = self._nexus_tokenizer.src if self._nexus_tokenizer else None
        if stream is None or end_offset is None:
            return None
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

    def _range_from_positions(self, start_position, end_position):
        if start_position is None:
            start_position = (0, 1, 1, None)
        if end_position is None:
            end_position = (self._nexus_tokenizer.char_offset,
                    self._nexus_tokenizer.current_line_num,
                    self._nexus_tokenizer.current_column_num + 1,
                    None)
        return treerecords.SourceRange(
                start_offset=start_position[0],
                end_offset=end_position[0],
                start_line=start_position[1],
                start_column=start_position[2],
                end_line=end_position[1],
                end_column=end_position[2])

    def _tree_end_position(self):
        end_position = getattr(self.newick_reader, "tree_statement_end_position", None)
        if end_position is None:
            end_position = self._nexus_tokenizer.last_semicolon_position
        if end_position is None:
            end_position = self._nexus_tokenizer.next_position
        return end_position

    def _make_tree_record(self,
            tree,
            start_position,
            end_position,
            block_index,
            tree_index_in_block,
            block_title,
            link_title,
            translation,
            translate_taxon_map,
            translate_range,
            block_range,
            used_translation):
        record_index = self._tree_index_counter
        self._tree_index_counter += 1
        record = treerecords.TreeRecord(
                tree=tree,
                record_index=record_index,
                block_index=block_index,
                tree_index_in_block=tree_index_in_block,
                source_file=self.current_file_name,
                source_range=self._range_from_positions(start_position, end_position),
                label=tree.label,
                translate=translation,
                translate_taxon_map=translate_taxon_map,
                used_translate=used_translation,
                translate_range=translate_range,
                block_range=block_range,
                block_title=block_title,
                link_title=link_title,
                is_complete=True,
                raw_text=self._source_text(
                    start_position[0], end_position[0]))
        self._attach_record(tree, record)
        self._set_tree_checkpoint(
                end_position=end_position,
                record=record)
        return record

    def _attach_record(self, tree, record):
        tree.tree_record = record
        tree.record_index = record.record_index
        tree.tree_index = record.tree_index
        tree.block_index = record.block_index
        tree.tree_index_in_block = record.tree_index_in_block
        tree.source_file = record.source_file
        tree.source_range = record.source_range
        tree.source_span = record.source_range
        tree.translate = collections.OrderedDict(record.translate)
        tree.used_translate = collections.OrderedDict(record.used_translate)
        tree.translate_range = record.translate_range
        tree.block_range = record.block_range
        tree.block_title = record.block_title
        tree.link_title = record.link_title

    def _set_tree_checkpoint(self, end_position, record):
        # Checkpoint is placed after the tree's terminating semi-colon, so a
        # resumed parse starts at the next statement.

        self._checkpoint = treerecords.CollectionCheckpoint(
                next_record_index=self._tree_index_counter,
                next_tree_index=self._tree_index_counter,
                next_char_offset=end_position[0],
                next_line_num=end_position[1],
                next_column_num=end_position[2],
                next_stream_position=end_position[3],
                source_file=self.current_file_name,
                file_index=self.current_file_index or 0,
                in_trees_block=True,
                block_index=record.block_index,
                tree_index_in_block=record.tree_index_in_block + 1,
                block_title=record.block_title,
                link_title=record.link_title,
                translate=record.translate,
                translate_range=record.translate_range,
                block_range=record.block_range,
                last_record_index=record.record_index,
                source_digest=self._source_digest(end_position[0]),
                complete=False)

    def _set_block_checkpoint(self,
            block_index,
            block_title,
            tree_index_in_block):
        end_position = self._nexus_tokenizer.last_semicolon_position
        if end_position is None:
            end_position = self._nexus_tokenizer.next_position
        self._checkpoint = treerecords.CollectionCheckpoint(
                next_record_index=self._tree_index_counter,
                next_tree_index=self._tree_index_counter,
                next_char_offset=end_position[0],
                next_line_num=end_position[1],
                next_column_num=end_position[2],
                next_stream_position=end_position[3],
                source_file=self.current_file_name,
                file_index=self.current_file_index or 0,
                in_trees_block=False,
                block_index=block_index,
                tree_index_in_block=tree_index_in_block,
                block_title=block_title,
                last_record_index=self._tree_index_counter - 1,
                source_digest=self._source_digest(end_position[0]),
                complete=False)

    def _set_incomplete_tail(self,
            start_position,
            block_index,
            tree_index_in_block,
            block_title,
            link_title,
            translation,
            translate_taxon_map,
            translate_range,
            block_range,
            error):
        end_position = (
                self._nexus_tokenizer.char_offset,
                self._nexus_tokenizer.current_line_num,
                self._nexus_tokenizer.current_column_num + 1,
                None)
        source_range = self._range_from_positions(start_position, end_position)
        self._incomplete_tail = treerecords.TreeRecord(
                tree=None,
                record_index=self._tree_index_counter,
                block_index=block_index,
                tree_index_in_block=tree_index_in_block,
                source_file=self.current_file_name,
                source_range=source_range,
                label=None,
                translate=translation,
                translate_taxon_map=translate_taxon_map,
                used_translate=collections.OrderedDict(),
                translate_range=translate_range,
                block_range=block_range,
                block_title=block_title,
                link_title=link_title,
                is_complete=False,
                raw_text=self._source_text(
                    source_range.start_offset, source_range.end_offset),
                error=None if error is None else "{}: {}".format(
                    type(error).__name__, error))
        # The cursor stays at the beginning of the unterminated tree, so a
        # resumed run re-reads it once the source has been completed.
        self._checkpoint = treerecords.CollectionCheckpoint(
                next_record_index=self._tree_index_counter,
                next_tree_index=self._tree_index_counter,
                next_char_offset=start_position[0],
                next_line_num=start_position[1],
                next_column_num=start_position[2],
                next_stream_position=start_position[3],
                source_file=self.current_file_name,
                file_index=self.current_file_index or 0,
                in_trees_block=True,
                block_index=block_index,
                tree_index_in_block=tree_index_in_block,
                block_title=block_title,
                link_title=link_title,
                translate=translation,
                translate_range=translate_range,
                block_range=block_range,
                last_record_index=self._tree_index_counter - 1,
                source_digest=self._source_digest(start_position[0]),
                complete=False)

    def _close_block_range(self, block_range):
        if block_range is None or block_range.end_offset is not None:
            return
        end_position = self._nexus_tokenizer.last_semicolon_position
        if end_position is None:
            end_position = self._nexus_tokenizer.next_position
        block_range.close(
                end_offset=end_position[0],
                end_line=end_position[1],
                end_column=end_position[2])

    def _is_truncated_tree_error(self, exc):
        """
        True only if ``exc`` was caused by the source ending in the middle of
        a tree statement. Malformed (but complete) statements at the end of
        the source are errors, not resumable tails.
        """
        if not self._nexus_tokenizer.is_eof():
            # A tree interrupted by a block keyword was never terminated, so
            # it is an uncommittable tail even though the stream continues.
            if getattr(self.newick_reader, "last_tree_terminated_by_keyword", None) is not None:
                return True
            return False
        if isinstance(exc, (tokenizer.Tokenizer.UnexpectedEndOfStreamError,
                tokenizer.Tokenizer.UnterminatedQuoteError)):
            return True
        if isinstance(exc, newickreader.NewickReader.NewickReaderIncompleteTreeStatementError):
            return True
        if isinstance(exc, newickreader.NewickReader.NewickReaderMalformedStatementError):
            return "Unbalanced parentheses" in str(exc)
        if isinstance(exc, nexusreader.NexusReader.NexusReaderError):
            message = str(exc)
            return ("Expecting tree definition for Tree" in message
                    or "Expecting '=' in definition of Tree" in message)
        return False

    @property
    def tail(self):
        """
        The uncommitted, unterminated tree at the end of the source, or |None|.

        A tail is reported but never yielded: committing a partially-built
        tree would misrepresent the data.  If more source text becomes
        available, resuming from :attr:`checkpoint` re-reads this tree.
        """
        return self._incomplete_tail

    @property
    def incomplete_tail(self):
        return self._incomplete_tail

    @property
    def tail_record(self):
        return self._incomplete_tail

    @property
    def tail_range(self):
        if self._incomplete_tail is None:
            return None
        return self._incomplete_tail.source_range

    ###########################################################################
    ## Resume support

    def _apply_resume_if_pending(self, stream):
        resume = self._resume_checkpoint
        if resume is None or self._resume_consumed:
            return False
        if not self._resume_matches_current_file(resume):
            return False
        self._verify_resume_prefix(stream, resume)
        position = getattr(resume, "next_stream_position", None)
        self._nexus_tokenizer.set_position(
                stream,
                char_offset=resume.next_char_offset,
                line_num=resume.next_line_num,
                column_num=resume.next_column_num,
                stream_position=position)
        self._tree_index_counter = max(
                self._tree_index_counter,
                resume.next_record_index)
        if resume.block_index is not None:
            self._block_index_counter = max(
                    self._block_index_counter,
                    resume.block_index)
        self._resume_consumed = True
        return True

    def _verify_resume_prefix(self, stream, resume):
        expected = getattr(resume, "source_digest", None)
        if expected is None:
            return
        observed = treerecords.prefix_digest(stream, resume.next_char_offset)
        if observed != expected:
            raise treerecords.ResumeCheckpointError(
                    "Checkpoint does not match the source prefix at offset {}: "
                    "the source was modified or replaced".format(
                        resume.next_char_offset))

    def _resume_matches_current_file(self, resume):
        if resume.source_file is None or self.current_file_name is None:
            return (resume.file_index or 0) == (self.current_file_index or 0)
        return os.path.abspath(str(resume.source_file)) == os.path.abspath(
                str(self.current_file_name))

    def _finalize_checkpoint(self):
        if self._checkpoint is not None and self._incomplete_tail is None:
            self._checkpoint.complete = True

    @property
    def checkpoint(self):
        """
        Cursor describing the next uncommitted tree record.

        The checkpoint is updated only after a complete tree has been yielded.
        If the source ends with an unterminated tree, the checkpoint continues
        to point at the beginning of that tree.
        """
        return self._checkpoint

    @property
    def last_checkpoint(self):
        return self._checkpoint

    @property
    def resume_checkpoint(self):
        return self._checkpoint

class NexusNewickTreeDataYielder(NexusTreeDataYielder):

    def __init__(self,
            files=None,
            taxon_namespace=None,
            tree_type=None,
            records=False,
            resume_from=None,
            **kwargs):
        kwargs["assume_newick_if_not_nexus"] = kwargs.get("assume_newick_if_not_nexus", True)
        NexusTreeDataYielder.__init__(self,
                files=files,
                taxon_namespace=taxon_namespace,
                tree_type=tree_type,
                records=records,
                resume_from=resume_from,
                **kwargs)
