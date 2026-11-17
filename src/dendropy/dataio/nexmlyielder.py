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
Implementation of NEXML-schema tree iterator.
"""

from dendropy.dataio import ioservice
from dendropy.dataio import nexmlreader
from dendropy.dataio import treerecords
from dendropy.dataio import xmlprocessing

class NexmlTreeDataYielder(
        ioservice.TreeDataYielder,
        nexmlreader.NexmlReader):

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
            a string, then it is assumed to be a path to a file. Otherwise, the
            source is assumed to be a file-like object.
        taxon_namespace : |TaxonNamespace| instance
            The operational taxonomic unit concept namespace to use to manage
            taxon definitions.
        records : bool
            If |True|, iteration yields
            :class:`~dendropy.dataio.treerecords.TreeRecord` objects, each
            carrying its trees-block/tree identity.
        \*\*kwargs : keyword arguments
            These will be passed directly to the base `nexmlreader.NexusReader`
            class. See `nexmlreader.NexusReader` for details.
        """
        ioservice.TreeDataYielder.__init__(self,
                files=files,
                taxon_namespace=taxon_namespace,
                tree_type=tree_type,
                records=records,
                resume_from=resume_from)
        nexmlreader.NexmlReader.__init__(self,
                **kwargs)
        self.attached_taxon_namespace = self.taxon_namespace
        self._tree_record_index = 0

    ###########################################################################
    ## Implementation of DataYielder interface

    def _yield_items_from_stream(self, stream):
        xml_doc = xmlprocessing.XmlDocument(file_obj=stream,
                subelement_factory=self._subelement_factory)
        self._namespace_registry = xml_doc.namespace_registry
        xml_root = xml_doc.root
        self._parse_taxon_namespaces(xml_root)
        tree_parser = nexmlreader._NexmlTreeParser(
                id_taxon_map=self._id_taxon_map,
                annotations_processor_fn=self._parse_annotations,
                )
        for trees_idx, trees_element in enumerate(xml_root.iter_trees()):
            trees_id = trees_element.get('id', "Trees" + str(trees_idx))
            trees_label = trees_element.get('label', None)
            otus_id = trees_element.get('otus', None)
            if otus_id is None:
                raise Exception("Taxa block not specified for trees block '{}'".format(otus_id))
            taxon_namespace = self._id_taxon_namespace_map.get(otus_id, None)
            if not taxon_namespace:
                raise Exception("Tree block '{}': Taxa block '{}' not found".format(trees_id, otus_id))
            for tree_index_in_block, tree_element in enumerate(trees_element.findall_tree()):
                tree_obj = self.tree_factory()
                tree_parser.build_tree(tree_obj, tree_element, otus_id)
                record = treerecords.TreeRecord(
                        tree=tree_obj,
                        record_index=self._tree_record_index,
                        block_index=trees_idx,
                        tree_index_in_block=tree_index_in_block,
                        source_file=self.current_file_name,
                        label=tree_obj.label,
                        block_title=trees_label,
                        link_title=otus_id)
                tree_obj.tree_record = record
                self._tree_record_index += 1
                yield tree_obj
