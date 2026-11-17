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
Data structures for self-contained, resumable records of trees read from
NEXUS sources.
"""

import collections
import hashlib

class ResumeCheckpointError(ValueError):
    """
    Raised when a checkpoint does not match the source being resumed from.

    A checkpoint is only meaningful for the same source prefix; if the source
    has been edited before the cursor, resuming would silently read the wrong
    text, so the mismatch is reported instead.
    """

def prefix_digest(stream, end_offset):
    """
    Exact digest of the source prefix of length ``end_offset``, or |None| if
    the stream cannot be seeked.
    """
    if end_offset is None or end_offset <= 0:
        return None
    try:
        stream_position = stream.tell()
        seekable = stream.seekable()
    except (AttributeError, OSError, IOError):
        return None
    if not seekable:
        return None
    try:
        stream.seek(0)
        text = stream.read(int(end_offset))
    finally:
        stream.seek(stream_position)
    digest = hashlib.sha256()
    digest.update(text.encode("utf-8", "surrogatepass"))
    return digest.hexdigest()

##############################################################################
## Source ranges

class SourceRange(object):
    """
    A half-open range of characters in a source stream.

    ``start_offset`` and ``end_offset`` are zero-based character offsets into
    the original source, with ``end_offset`` exclusive.  Line and column
    values are one-based and refer to the same positions, so that diagnostics
    can point at the exact place in the source.
    """

    def __init__(self,
            start_offset,
            end_offset,
            start_line=None,
            start_column=None,
            end_line=None,
            end_column=None):
        self.start_offset = int(start_offset)
        self.end_offset = None if end_offset is None else int(end_offset)
        self.start_line = start_line
        self.start_column = start_column
        self.end_line = end_line
        self.end_column = end_column

    def close(self, end_offset, end_line=None, end_column=None):
        """
        Sets the (exclusive) end of a range whose extent was not yet known.
        """
        self.end_offset = int(end_offset)
        self.end_line = end_line
        self.end_column = end_column
        return self

    @property
    def length(self):
        if self.end_offset is None:
            return None
        return self.end_offset - self.start_offset

    @property
    def start(self):
        return self.start_offset

    @property
    def end(self):
        return self.end_offset

    @property
    def span(self):
        return (self.start_offset, self.end_offset)

    def contains(self, offset):
        if self.end_offset is None:
            return offset >= self.start_offset
        return self.start_offset <= offset < self.end_offset

    def to_dict(self):
        return collections.OrderedDict([
                ("start_offset", self.start_offset),
                ("end_offset", self.end_offset),
                ("start_line", self.start_line),
                ("start_column", self.start_column),
                ("end_line", self.end_line),
                ("end_column", self.end_column),
                ])

    @classmethod
    def from_dict(cls, d):
        if d is None:
            return None
        return cls(
                start_offset=d["start_offset"],
                end_offset=d.get("end_offset"),
                start_line=d.get("start_line"),
                start_column=d.get("start_column"),
                end_line=d.get("end_line"),
                end_column=d.get("end_column"),
                )

    def __eq__(self, other):
        if not isinstance(other, SourceRange):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    def __repr__(self):
        return "SourceRange({}, {}, lines {}-{})".format(
                self.start_offset,
                self.end_offset,
                self.start_line,
                self.end_line)

##############################################################################
## Tree records

class TreeRecord(object):
    """
    A single, independently deliverable tree and everything needed to
    interpret it without reference to other records in the source.

    Notes
    -----
    ``translate`` maps the NEXUS translation tokens actually in effect for
    this tree to taxon labels.  It is a snapshot taken from the enclosing
    TREES block, so a translation table from another block (even one using
    the same tokens) is never reused.  ``translate_taxon_map`` maps those
    tokens to the corresponding |Taxon| objects and is a runtime-only value.
    """

    def __init__(self,
            tree,
            record_index,
            tree_index=None,
            block_index=None,
            tree_index_in_block=None,
            source_file=None,
            source_range=None,
            label=None,
            translate=None,
            translate_taxon_map=None,
            used_translate=None,
            translate_range=None,
            block_range=None,
            block_title=None,
            link_title=None,
            is_complete=True,
            raw_text=None,
            error=None):
        self.tree = tree
        self.record_index = int(record_index)
        self.tree_index = self.record_index if tree_index is None else int(tree_index)
        self.block_index = block_index
        self.tree_index_in_block = tree_index_in_block
        self.source_file = source_file
        self.source_range = source_range
        self.label = label
        self.translate = collections.OrderedDict(translate or ())
        self.translate_taxon_map = dict(translate_taxon_map or {})
        self.used_translate = collections.OrderedDict(used_translate or ())
        self.translate_range = translate_range
        self.block_range = block_range
        self.block_title = block_title
        self.link_title = link_title
        self.is_complete = bool(is_complete)
        self.raw_text = raw_text
        self.error = error

    @property
    def index(self):
        return self.record_index

    @property
    def index_in_block(self):
        return self.tree_index_in_block

    @property
    def block(self):
        return self.block_index

    @property
    def source(self):
        return self.source_file

    @property
    def range(self):
        return self.source_range

    @property
    def span(self):
        return self.source_range

    @property
    def source_span(self):
        return self.source_range

    @property
    def tree_range(self):
        return self.source_range

    def definition_range(self, source_text=None):
        """
        Range of the tree definition proper (from ``(`` through the final
        ``;``), excluding the ``TREE <name> =`` prefix.

        If ``source_text`` is given, the range is derived from it; otherwise
        the statement range is returned.
        """
        if source_text is None or self.source_range is None:
            return self.source_range
        statement = source_text[
                self.source_range.start_offset:self.source_range.end_offset]
        open_idx = statement.find("(")
        if open_idx < 0:
            return self.source_range
        return SourceRange(
                start_offset=self.source_range.start_offset + open_idx,
                end_offset=self.source_range.end_offset,
                start_line=self.source_range.start_line,
                start_column=self.source_range.start_column)

    @property
    def source_name(self):
        return self.source_file

    @property
    def filename(self):
        return self.source_file

    @property
    def tree_label(self):
        return self.label

    @property
    def name(self):
        return self.label

    @property
    def translation(self):
        return self.translate

    @property
    def translate_table(self):
        return self.translate

    @property
    def translation_map(self):
        return self.translate

    @property
    def token_map(self):
        return self.translate

    @property
    def mapping(self):
        return self.translate

    @property
    def used_mapping(self):
        return self.used_translate

    @property
    def taxon_mapping(self):
        return self.translate_taxon_map

    @property
    def taxon_map(self):
        return self.translate_taxon_map

    @property
    def translate_map(self):
        return self.translate_taxon_map

    @property
    def token_taxon_map(self):
        return self.translate_taxon_map

    @property
    def scope_range(self):
        return self.block_range

    @property
    def complete(self):
        return self.is_complete

    @property
    def newick(self):
        if self.tree is None:
            return None
        return self.tree.as_string(schema="newick").strip()

    def __getitem__(self, key):
        return self.as_dict()[key]

    def get(self, key, default=None):
        return self.as_dict().get(key, default)

    def keys(self):
        return self.as_dict().keys()

    def items(self):
        return self.as_dict().items()

    def values(self):
        return self.as_dict().values()

    def __contains__(self, key):
        return key in self.as_dict()

    def as_dict(self,
            include_tree=True,
            include_taxon_objects=False,
            include_checkpoint=False):
        """
        Returns a JSON-compatible representation of this record.
        """
        record = collections.OrderedDict()
        record["record_index"] = self.record_index
        record["tree_index"] = self.tree_index
        record["block_index"] = self.block_index
        record["tree_index_in_block"] = self.tree_index_in_block
        record["tree_label"] = self.label
        record["source_file"] = self.source_file
        record["source_range"] = (
                self.source_range.to_dict()
                if self.source_range is not None
                else None)
        record["translate"] = collections.OrderedDict(self.translate)
        record["used_translate"] = collections.OrderedDict(self.used_translate)
        record["translate_range"] = (
                self.translate_range.to_dict()
                if self.translate_range is not None
                else None)
        record["block_range"] = (
                self.block_range.to_dict()
                if self.block_range is not None
                else None)
        record["block_title"] = self.block_title
        record["link_title"] = self.link_title
        record["is_complete"] = self.is_complete
        record["raw_text"] = self.raw_text
        record["error"] = self.error
        if include_taxon_objects:
            record["translate_taxa"] = collections.OrderedDict(
                    (token, taxon.label)
                    for token, taxon in self.translate_taxon_map.items())
        if include_tree:
            record["newick"] = self.newick
        if include_checkpoint:
            record["checkpoint"] = None
        return record

    to_dict = as_dict

    def __repr__(self):
        return "TreeRecord(index={}, label={!r}, block={})".format(
                self.record_index,
                self.label,
                self.block_index)

##############################################################################
## Resume checkpoints

class CollectionCheckpoint(object):
    """
    A durable cursor describing the next tree record to deliver.

    A checkpoint is deliberately self-describing: besides the record and
    source offsets it stores the TREES-block context and the translation
    table that was in effect at the cursor.  A reader resuming from it can
    therefore continue without treating the tail of the previous run as a
    committed record, and without borrowing a translation table from a
    different block.
    """

    def __init__(self,
            next_record_index=0,
            next_tree_index=0,
            next_char_offset=0,
            next_line_num=1,
            next_column_num=0,
            next_stream_position=None,
            source_digest=None,
            source_file=None,
            file_index=0,
            in_trees_block=False,
            block_index=None,
            tree_index_in_block=0,
            block_title=None,
            link_title=None,
            translate=None,
            translate_range=None,
            block_range=None,
            last_record_index=-1,
            complete=False):
        self.next_record_index = int(next_record_index)
        self.next_tree_index = int(next_tree_index)
        self.next_char_offset = int(next_char_offset)
        self.next_line_num = next_line_num
        self.next_column_num = next_column_num
        self.next_stream_position = next_stream_position
        self.source_digest = source_digest
        self.source_file = source_file
        self.file_index = int(file_index)
        self.in_trees_block = bool(in_trees_block)
        self.block_index = block_index
        self.tree_index_in_block = int(tree_index_in_block)
        self.block_title = block_title
        self.link_title = link_title
        self.translate = collections.OrderedDict(translate or ())
        self.translate_range = translate_range
        self.block_range = block_range
        self.last_record_index = int(last_record_index)
        self.complete = bool(complete)

    @property
    def next_index(self):
        return self.next_record_index

    @property
    def offset(self):
        return self.next_char_offset

    @property
    def record_index(self):
        return self.next_record_index

    def copy(self):
        return CollectionCheckpoint.from_dict(self.to_dict())

    def to_dict(self):
        return collections.OrderedDict([
                ("next_record_index", self.next_record_index),
                ("next_tree_index", self.next_tree_index),
                ("next_char_offset", self.next_char_offset),
                ("next_line_num", self.next_line_num),
                ("next_column_num", self.next_column_num),
                ("next_stream_position", self.next_stream_position),
                ("source_digest", self.source_digest),
                ("source_file", self.source_file),
                ("file_index", self.file_index),
                ("in_trees_block", self.in_trees_block),
                ("block_index", self.block_index),
                ("tree_index_in_block", self.tree_index_in_block),
                ("block_title", self.block_title),
                ("link_title", self.link_title),
                ("translate", collections.OrderedDict(self.translate)),
                ("translate_range", (
                    self.translate_range.to_dict()
                    if self.translate_range is not None
                    else None)),
                ("block_range", (
                    self.block_range.to_dict()
                    if self.block_range is not None
                    else None)),
                ("last_record_index", self.last_record_index),
                ("complete", self.complete),
                ])

    as_dict = to_dict

    @classmethod
    def from_dict(cls, d):
        if d is None:
            return None
        if isinstance(d, cls):
            return d
        return cls(
                next_record_index=d.get("next_record_index", 0),
                next_tree_index=d.get("next_tree_index", 0),
                next_char_offset=d.get("next_char_offset", 0),
                next_line_num=d.get("next_line_num", 1),
                next_column_num=d.get("next_column_num", 0),
                next_stream_position=d.get("next_stream_position"),
                source_digest=d.get("source_digest"),
                source_file=d.get("source_file"),
                file_index=d.get("file_index", 0),
                in_trees_block=d.get("in_trees_block", False),
                block_index=d.get("block_index"),
                tree_index_in_block=d.get("tree_index_in_block", 0),
                block_title=d.get("block_title"),
                link_title=d.get("link_title"),
                translate=d.get("translate"),
                translate_range=SourceRange.from_dict(d.get("translate_range")),
                block_range=SourceRange.from_dict(d.get("block_range")),
                last_record_index=d.get("last_record_index", -1),
                complete=d.get("complete", False),
                )

    def __repr__(self):
        return ("CollectionCheckpoint(next_record_index={}, "
                "next_char_offset={}, in_trees_block={})").format(
                        self.next_record_index,
                        self.next_char_offset,
                        self.in_trees_block)

ResumeCheckpoint = CollectionCheckpoint
