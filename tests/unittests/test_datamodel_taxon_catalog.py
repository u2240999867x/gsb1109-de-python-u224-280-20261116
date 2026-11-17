#! /usr/bin/env python
# -*- coding: utf-8 -*-

##############################################################################
##  DendroPy Phylogenetic Computing Library.
##
##  Copyright 2010-2026 Jeet Sukumaran, Mark T. Holder, and Matthew Andres Moreno.
##  All rights reserved.
##
##  See "LICENSE.rst" for terms and conditions of usage.
##############################################################################

"""
Tests for versioned taxon catalogs, leaf-identity corrections and tree
migration (``dendropy.datamodel.taxoncatalogmodel``).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))

import dendropy
from dendropy.calculate import phylogeneticdistance
from dendropy.calculate import treecompare
from support import dendropytest


class CatalogTestBase(dendropytest.ExtendedTestCase):

    def build_catalog(self):
        catalog = dendropy.TaxonCatalog(
            ["A", "B", "C", "D"], label="test-catalog")
        return catalog

    def build_tree(self, catalog=None, data="((A,B),(C,D));",
            suppress_internal_node_taxa=True):
        if catalog is None:
            catalog = self.build_catalog()
        return dendropy.Tree.get(
            data=data,
            schema="newick",
            taxon_namespace=catalog.base_taxon_namespace,
            suppress_internal_node_taxa=suppress_internal_node_taxa,
        )


class TestTaxonCatalogVersions(CatalogTestBase):

    def test_base_version(self):
        catalog = self.build_catalog()
        self.assertEqual(catalog.current_version_number, 0)
        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog.base_version.labels, ("A", "B", "C", "D"))
        self.assertEqual(catalog.base_taxon_namespace.labels(),
                         ["A", "B", "C", "D"])
        self.assertEqual(len(catalog.versions()), 1)

    def test_each_correction_produces_new_immutable_version(self):
        catalog = self.build_catalog()
        v0 = catalog.current_version
        v1 = catalog.rename_taxon("B", "B2")
        self.assertIsNot(v1, v0)
        self.assertEqual(v1.version, 1)
        self.assertEqual(catalog.current_version_number, 1)
        # earlier version is retained and interpretable
        self.assertEqual(catalog.get_version(0).labels, ("A", "B", "C", "D"))
        self.assertEqual(v1.labels, ("A", "B2", "C", "D"))
        self.assertIsNot(v0.taxon_namespace, v1.taxon_namespace)
        # negative indexing follows sequence conventions
        self.assertIs(catalog.get_version(-1), v1)

    def test_revision_records(self):
        catalog = self.build_catalog()
        revision = catalog.rename_taxon(
            "B", "B2", rationale="nomen novum").revisions[-1]
        self.assertEqual(revision.operation, dendropy.RENAME_OPERATION)
        self.assertEqual(revision.affected_labels, ("B",))
        self.assertEqual(revision.new_labels, ("B2",))
        self.assertEqual(revision.rationale, "nomen novum")
        self.assertEqual(revision.as_dict()["revision_index"], 0)

    def test_version_namespaces_are_independent(self):
        catalog = self.build_catalog()
        v0 = catalog.current_version
        catalog.rename_taxon("B", "B2")
        # mutating a later-version taxon cannot alter the historical version
        new_b2 = catalog.current_version.taxon_namespace.get_taxon("B2")
        new_b2.label = "MUTATED"
        self.assertEqual(v0.labels, ("A", "B", "C", "D"))
        self.assertIsNotNone(v0.taxon_namespace.get_taxon("B"))


class TestRenameCorrection(CatalogTestBase):

    def test_rename_lineage(self):
        catalog = self.build_catalog()
        catalog.rename_taxon("B", "B2")
        self.assertEqual(
            catalog.lineage("B2"),
            [(0, "B"), (1, "B2")])
        self.assertEqual(
            catalog.historical_lineage("B"),
            [(0, "B")])

    def test_rename_rejects_unknown_and_collision(self):
        catalog = self.build_catalog()
        with self.assertRaises(dendropy.TaxonCatalogCorrectionError):
            catalog.rename_taxon("MISSING", "X")
        with self.assertRaises(dendropy.TaxonCatalogCorrectionError):
            catalog.rename_taxon("A", "B")


class TestMergeCorrection(CatalogTestBase):

    def test_merge_aliases(self):
        catalog = self.build_catalog()
        v1 = catalog.merge_taxa(["C", "D"], new_label="CD")
        self.assertEqual(v1.labels, ("A", "B", "CD"))
        # both source chains are archived for later recovery
        self.assertEqual(catalog.historical_lineage("C"), [(0, "C")])
        self.assertEqual(catalog.historical_lineage("D"), [(0, "D")])
        self.assertEqual(
            catalog.lineage("CD"),
            [(0, "C"), (1, "CD")])

    def test_merge_defaults_to_first_source_label(self):
        catalog = self.build_catalog()
        v1 = catalog.merge_taxa(["C", "D"])
        self.assertEqual(v1.labels, ("A", "B", "C"))
        self.assertEqual(catalog.lineage("C"), [(0, "C"), (1, "C")])

    def test_merge_validation(self):
        catalog = self.build_catalog()
        with self.assertRaises(dendropy.TaxonCatalogCorrectionError):
            catalog.merge_taxa(["A"])
        with self.assertRaises(dendropy.TaxonCatalogCorrectionError):
            catalog.merge_taxa(["A", "B"], new_label="C")
        with self.assertRaises(dendropy.TaxonCatalogCorrectionError):
            catalog.merge_taxa(["A", "MISSING"])
        with self.assertRaises(dendropy.TaxonCatalogCorrectionError):
            catalog.merge_taxa(["A", "A"])


class TestSplitCorrection(CatalogTestBase):

    def test_split_undoes_erroneous_merge_with_original_origins(self):
        catalog = self.build_catalog()
        catalog.merge_taxa(["C", "D"], new_label="CD")
        v2 = catalog.split_taxon("CD", ["C", "D"])
        self.assertEqual(v2.labels, ("A", "B", "C", "D"))
        # the merged identity is archived ...
        self.assertEqual(
            catalog.historical_lineage("CD"),
            [(0, "C"), (1, "CD")])
        # ... and each product traces back to its own base declaration
        self.assertEqual(
            catalog.lineage("C"),
            [(0, "C"), (1, "CD"), (2, "C")])
        self.assertEqual(
            catalog.lineage("D"),
            [(0, "D"), (1, "CD"), (2, "D")])

    def test_split_new_products_inherit_source_chain(self):
        catalog = dendropy.TaxonCatalog(["X"])
        v1 = catalog.split_taxon("X", ["X1", "X2"])
        self.assertEqual(v1.labels, ("X1", "X2"))
        self.assertEqual(
            catalog.lineage("X1"),
            [(0, "X"), (1, "X1")])
        self.assertEqual(
            catalog.lineage("X2"),
            [(0, "X"), (1, "X2")])

    def test_split_validation(self):
        catalog = self.build_catalog()
        with self.assertRaises(dendropy.TaxonCatalogCorrectionError):
            catalog.split_taxon("A", ["A"])
        with self.assertRaises(dendropy.TaxonCatalogCorrectionError):
            catalog.split_taxon("MISSING", ["X", "Y"])
        with self.assertRaises(dendropy.TaxonCatalogCorrectionError):
            catalog.split_taxon("A", ["A", "B"])  # B already exists
        with self.assertRaises(dendropy.TaxonCatalogCorrectionError):
            catalog.split_taxon("A", ["X", "X"])  # duplicate products

    def test_apply_corrections_series(self):
        catalog = self.build_catalog()
        final = catalog.apply_corrections([
            {"operation": "rename", "source": "B", "target": "B2"},
            {"operation": "merge", "sources": ["C", "D"], "target": "CD"},
            {"operation": "split", "source": "CD", "targets": ["C", "D"]},
        ])
        self.assertEqual(final.version, 3)
        self.assertEqual(final.labels, ("A", "B2", "C", "D"))
        with self.assertRaises(dendropy.TaxonCatalogCorrectionError):
            catalog.apply_corrections([])


class TestProvenance(CatalogTestBase):

    def test_taxon_annotations_trace_to_base_declaration(self):
        catalog = self.build_catalog()
        catalog.rename_taxon("B", "B2")
        catalog.merge_taxa(["C", "D"], new_label="CD")
        catalog.split_taxon("CD", ["C", "D"])
        version = catalog.current_version
        base_labels = set(catalog.base_version.labels)
        for taxon in version.taxon_namespace:
            provenance = catalog.provenance(taxon)
            self.assertIsNotNone(provenance)
            self.assertEqual(provenance["version"], version.version)
            # the annotated identity chain matches the catalog lineage ...
            annotated_chain = [
                tuple(step.split(":", 1))
                for step in provenance["identity_chain"].split("|")
            ]
            annotated_chain = [
                (int(step_version), step_label)
                for step_version, step_label in annotated_chain
            ]
            self.assertEqual(
                annotated_chain, catalog.lineage(taxon.label))
            # ... and every leaf identity resolves to a base declaration
            chain = catalog.lineage(taxon.label)
            self.assertEqual(chain[0][0], 0)
            self.assertIn(chain[0][1], base_labels)

    def test_provenance_none_for_unannotated_taxon(self):
        catalog = self.build_catalog()
        external_taxon = dendropy.Taxon(label="not-in-catalog")
        self.assertIsNone(catalog.provenance(external_taxon))

    def test_lineage_unknown_label_raises(self):
        catalog = self.build_catalog()
        with self.assertRaises(KeyError):
            catalog.lineage("MISSING")
        with self.assertRaises(KeyError):
            catalog.historical_lineage("MISSING")


class TestTreeMigration(CatalogTestBase):

    def test_rename_migration_remaps_leaves_only(self):
        catalog = self.build_catalog()
        catalog.rename_taxon("B", "B2")
        tree = self.build_tree(
            catalog,
            data="((A,B)i1,(C,D)i2)root;",
            suppress_internal_node_taxa=False)
        internal_taxa = {
            nd.taxon for nd in tree.postorder_internal_node_iter()}
        tree.encode_bipartitions()
        catalog.migrate_tree(tree)
        # leaf identities follow the new version
        self.assertIs(tree.taxon_namespace,
                      catalog.current_version.taxon_namespace)
        leaf_labels = [nd.taxon.label for nd in tree.leaf_node_iter()]
        self.assertEqual(sorted(leaf_labels), ["A", "B2", "C", "D"])
        # internal node labels do not participate in leaf-identity correction
        for nd in tree.postorder_internal_node_iter():
            self.assertIn(nd.taxon, internal_taxa)
            self.assertNotIn(nd.taxon,
                             catalog.current_version.taxon_namespace)

    def test_merge_migration_unifies_leaves(self):
        catalog = self.build_catalog()
        catalog.merge_taxa(["C", "D"], new_label="CD")
        tree = self.build_tree(catalog)
        catalog.migrate_tree(tree)
        cd_taxa = {
            nd.taxon for nd in tree.leaf_node_iter()
            if nd.taxon.label == "CD"
        }
        # two leaf nodes remain, but both reference the single merged Taxon
        self.assertEqual(len(cd_taxa), 1)
        self.assertEqual(
            [nd.taxon.label for nd in tree.leaf_node_iter()],
            ["A", "B", "CD", "CD"])

    def test_split_without_destinations_is_indeterminate_and_atomic(self):
        catalog = self.build_catalog()
        catalog.merge_taxa(["C", "D"], new_label="CD")
        catalog.split_taxon("CD", ["C", "D"])
        tree = self.build_tree(catalog)
        original_tns = tree.taxon_namespace
        tree.encode_bipartitions()
        original_encoding = tree.bipartition_encoding
        with self.assertRaises(dendropy.IndeterminateLeafIdentityError) as cm:
            catalog.migrate_tree(tree)
        indeterminate_labels = sorted(
            nd.taxon.label for nd in cm.exception.indeterminate_leaves)
        self.assertEqual(indeterminate_labels, ["C", "D"])
        # no partial migration: namespace, leaf taxa and caches are intact
        self.assertIs(tree.taxon_namespace, original_tns)
        self.assertIs(tree.bipartition_encoding, original_encoding)
        self.assertEqual(
            sorted(nd.taxon.label for nd in tree.leaf_node_iter()),
            ["A", "B", "C", "D"])

    def test_split_with_label_destinations(self):
        catalog = self.build_catalog()
        catalog.merge_taxa(["C", "D"], new_label="CD")
        catalog.split_taxon("CD", ["C", "D"])
        tree = self.build_tree(catalog)
        catalog.migrate_tree(
            tree, leaf_destinations={"C": "C", "D": "D"})
        self.assertEqual(
            sorted(nd.taxon.label for nd in tree.leaf_node_iter()),
            ["A", "B", "C", "D"])

    def test_split_with_node_destinations_after_merge(self):
        catalog = self.build_catalog()
        catalog.merge_taxa(["C", "D"], new_label="CD")
        catalog.split_taxon("CD", ["C", "D"])
        tree = self.build_tree(catalog)
        # migrate to merged version first, so both leaves share one Taxon
        catalog.migrate_tree(tree, target_version=1)
        merged_leaves = [
            nd for nd in tree.leaf_node_iter() if nd.taxon.label == "CD"]
        self.assertIs(merged_leaves[0].taxon, merged_leaves[1].taxon)
        # node-keyed destinations can still separate them
        catalog.migrate_tree(
            tree,
            leaf_destinations={
                merged_leaves[0]: "C",
                merged_leaves[1]: "D",
            })
        labels = [nd.taxon.label for nd in tree.leaf_node_iter()]
        self.assertEqual(sorted(labels), ["A", "B", "C", "D"])

    def test_invalid_split_destination_rejected(self):
        catalog = self.build_catalog()
        catalog.merge_taxa(["C", "D"], new_label="CD")
        catalog.split_taxon("CD", ["C", "D"])
        tree = self.build_tree(catalog)
        with self.assertRaises(dendropy.TaxonCatalogCorrectionError):
            catalog.migrate_tree(
                tree, leaf_destinations={"C": "BOGUS", "D": "D"})

    def test_undeclared_leaf_label_rejected_without_mutation(self):
        catalog = self.build_catalog()
        catalog.rename_taxon("B", "B2")
        tree = dendropy.Tree.get(
            data="((A,B),(C,Z));",
            schema="newick",
            taxon_namespace=catalog.base_taxon_namespace)
        original_tns = tree.taxon_namespace
        with self.assertRaises(dendropy.TaxonCatalogCorrectionError):
            catalog.migrate_tree(tree)
        self.assertIs(tree.taxon_namespace, original_tns)
        self.assertEqual(
            [nd.taxon.label for nd in tree.leaf_node_iter()],
            ["A", "B", "C", "Z"])

    def test_migration_invalidates_all_derived_caches(self):
        catalog = self.build_catalog()
        catalog.rename_taxon("B", "B2")
        tree = self.build_tree(catalog)
        tree.encode_bipartitions()
        self.assertIsNotNone(tree.bipartition_encoding)
        self.assertIsNotNone(tree.seed_node.edge._bipartition)
        catalog.migrate_tree(tree)
        self.assertIsNone(tree.bipartition_encoding)
        self.assertIsNone(tree._split_bitmask_edge_map)
        self.assertIsNone(tree._bipartition_edge_map)
        for edge in tree.postorder_edge_iter():
            self.assertIsNone(edge._bipartition)
        # recomputation is version-aware, not a reuse of old caches
        tree.encode_bipartitions()
        target_tns = catalog.current_version.taxon_namespace
        for node in tree.leaf_node_iter():
            self.assertEqual(
                node.edge.bipartition.leafset_bitmask,
                target_tns.taxon_bitmask(node.taxon))
        # edge map is rebuilt against the new namespace
        self.assertEqual(
            set(tree.split_bitmask_edge_map.keys()),
            {edge.bipartition.split_bitmask
             for edge in tree.postorder_edge_iter()})

    def test_migrated_splits_distances_and_leafset_use_new_version(self):
        catalog = self.build_catalog()
        catalog.rename_taxon("B", "B2")
        catalog.merge_taxa(["C", "D"], new_label="CD")
        catalog.split_taxon("CD", ["C", "D"])
        tree1 = self.build_tree(catalog)
        tree2 = dendropy.Tree.get(
            data="((A,C),(B,D));",
            schema="newick",
            taxon_namespace=catalog.base_taxon_namespace)
        catalog.migrate_tree(
            tree1, leaf_destinations={"C": "C", "D": "D"})
        catalog.migrate_tree(
            tree2, leaf_destinations={"C": "C", "D": "D"})
        # leaf-set query reflects the target version
        self.assertEqual(
            sorted(nd.taxon.label for nd in tree1.leaf_nodes()),
            ["A", "B2", "C", "D"])
        # split comparison runs on the shared target namespace
        self.assertEqual(
            treecompare.symmetric_difference(tree1, tree2), 2)
        # distance matrix is keyed by new-version taxa
        pdm = phylogeneticdistance.PhylogeneticDistanceMatrix()
        pdm.compile_from_tree(tree1)
        target_tns = catalog.current_version.taxon_namespace
        self.assertEqual(
            pdm.distance(
                target_tns.get_taxon("C"),
                target_tns.get_taxon("D"),
                is_weighted_edge_distances=False),
            2)

    def test_chained_migration_and_version_stamping(self):
        catalog = self.build_catalog()
        catalog.rename_taxon("B", "B2")
        catalog.merge_taxa(["C", "D"], new_label="CD")
        catalog.split_taxon("CD", ["C", "D"])
        tree = self.build_tree(catalog)
        catalog.migrate_tree(tree, target_version=1)
        self.assertIs(tree.taxon_namespace,
                      catalog.get_version(1).taxon_namespace)
        catalog.migrate_tree(
            tree, leaf_destinations={"C": "C", "D": "D"})
        self.assertIs(tree.taxon_namespace,
                      catalog.current_version.taxon_namespace)
        stamps = [
            annotation.value for annotation in tree.annotations
            if annotation.name == "taxonCatalogTreeVersion"
        ]
        self.assertEqual(stamps, [1, 3])
        with self.assertRaises(dendropy.TaxonCatalogCorrectionError):
            catalog.migrate_tree(tree, target_version=0)

    def test_migrated_leaf_identity_traceable_to_base(self):
        catalog = self.build_catalog()
        catalog.rename_taxon("B", "B2")
        catalog.merge_taxa(["C", "D"], new_label="CD")
        catalog.split_taxon("CD", ["C", "D"])
        tree = self.build_tree(catalog)
        catalog.migrate_tree(
            tree, leaf_destinations={"C": "C", "D": "D"})
        target_tns = catalog.current_version.taxon_namespace
        b2 = target_tns.get_taxon("B2")
        provenance = catalog.provenance(b2)
        self.assertEqual(provenance["origin_label"], "B")
        self.assertTrue(provenance["identity_chain"].startswith("0:B"))
        c_taxon = target_tns.get_taxon("C")
        self.assertTrue(
            catalog.provenance(c_taxon)["identity_chain"].startswith("0:C"))


class TestUnmigratedHistoryPreserved(CatalogTestBase):

    def test_historical_tree_and_comparison_keep_original_interpretation(self):
        catalog = self.build_catalog()
        catalog.rename_taxon("B", "B2")
        historical = self.build_tree(catalog)
        historical.encode_bipartitions()
        historical_splits = set(historical.bipartition_encoding)
        # a recorded comparison result computed under the old version
        other = self.build_tree(
            catalog, data="((A,C),(B,D));")
        historical_rf = treecompare.symmetric_difference(historical, other)
        self.assertEqual(historical_rf, 2)

        migrated = self.build_tree(catalog)
        catalog.migrate_tree(migrated)
        # historical tree is untouched and still interpretable under v0
        self.assertIs(historical.taxon_namespace,
                      catalog.base_taxon_namespace)
        self.assertEqual(
            set(historical.bipartition_encoding), historical_splits)
        self.assertEqual(
            sorted(nd.taxon.label for nd in historical.leaf_node_iter()),
            ["A", "B", "C", "D"])
        # old comparison remains reproducible
        self.assertEqual(
            treecompare.symmetric_difference(historical, other),
            historical_rf)
        # historical distance matrix remains keyed by old taxa
        pdm = phylogeneticdistance.PhylogeneticDistanceMatrix()
        pdm.compile_from_tree(historical)
        old_b = catalog.base_taxon_namespace.get_taxon("B")
        self.assertEqual(
            pdm.distance(
                catalog.base_taxon_namespace.get_taxon("A"),
                old_b,
                is_weighted_edge_distances=False),
            2)


class TestMigrateTreeCollections(CatalogTestBase):

    def test_migrate_list_returns_migrated_trees(self):
        catalog = self.build_catalog()
        catalog.rename_taxon("B", "B2")
        trees = [self.build_tree(catalog),
                 self.build_tree(catalog, data="((A,C),(B,D));")]
        migrated = catalog.migrate_trees(trees)
        self.assertEqual(len(migrated), 2)
        for tree in migrated:
            self.assertIs(tree.taxon_namespace,
                          catalog.current_version.taxon_namespace)

    def test_migrate_tree_list_preserves_source(self):
        catalog = self.build_catalog()
        catalog.merge_taxa(["C", "D"], new_label="CD")
        catalog.split_taxon("CD", ["C", "D"])
        tree_list = dendropy.TreeList.get(
            data="((A,B),(C,D));((A,C),(B,D));",
            schema="newick",
            taxon_namespace=catalog.base_taxon_namespace)
        destinations = {}
        for tree in tree_list:
            c_node = next(nd for nd in tree.leaf_node_iter()
                          if nd.taxon.label == "C")
            d_node = next(nd for nd in tree.leaf_node_iter()
                          if nd.taxon.label == "D")
            destinations[c_node] = "C"
            destinations[d_node] = "D"
        migrated_collection = catalog.migrate_trees(
            tree_list, leaf_destinations=destinations)
        # new collection on the target version namespace
        self.assertIs(migrated_collection.taxon_namespace,
                      catalog.current_version.taxon_namespace)
        for tree in migrated_collection:
            self.assertEqual(
                sorted(nd.taxon.label for nd in tree.leaf_node_iter()),
                ["A", "B", "C", "D"])
        # source collection retains its original interpretation
        self.assertIs(tree_list.taxon_namespace,
                      catalog.base_taxon_namespace)
        for tree in tree_list:
            self.assertEqual(
                sorted(nd.taxon.label for nd in tree.leaf_node_iter()),
                ["A", "B", "C", "D"])


class TestNamespaceMigrationCacheInvalidation(CatalogTestBase):

    def test_reconstruct_taxon_namespace_invalidates_caches(self):
        tree = self.build_tree()
        tree.encode_bipartitions()
        self.assertIsNotNone(tree.bipartition_encoding)
        fresh_tns = dendropy.TaxonNamespace()
        tree.migrate_taxon_namespace(fresh_tns)
        self.assertIsNone(tree.bipartition_encoding)
        self.assertIsNone(tree._split_bitmask_edge_map)
        self.assertIsNone(tree._bipartition_edge_map)
        for edge in tree.postorder_edge_iter():
            self.assertIsNone(edge._bipartition)
        # re-encoding works against the replacement namespace
        tree.encode_bipartitions()
        self.assertIsNotNone(tree.bipartition_encoding)


if __name__ == "__main__":
    unittest.main()
