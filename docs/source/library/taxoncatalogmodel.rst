**********************************************************************************
:mod:`dendropy.datamodel.taxoncatalogmodel`: Versioned Taxon Catalogs and Leaf-Identity Corrections
**********************************************************************************

.. module:: dendropy.datamodel.taxoncatalogmodel

.. toctree::
    :maxdepth: 2

A shared taxon directory is modeled as a versioned :class:`TaxonCatalog`.
Taxonomic corrections (renames, alias merges, and splits that undo an
erroneous merge) never mutate an existing version: each correction creates a
new, immutable :class:`TaxonCatalogVersion` with its own
:class:`~dendropy.datamodel.taxonmodel.TaxonNamespace`, while earlier versions
remain available so historical trees and comparison results keep their
original interpretation.

Trees are migrated explicitly with :meth:`TaxonCatalog.migrate_tree` /
:meth:`TaxonCatalog.migrate_trees`. Migration remaps leaf
:class:`~dendropy.datamodel.taxonmodel.Taxon` objects, leaves internal node
labels untouched, invalidates all derived caches (split encodings and edge
maps), and records provenance on every leaf so that each new leaf identity can
be traced back to the label declared in the base (version 0) catalog. A split
whose leaf destinations cannot all be determined raises an
:class:`IndeterminateLeafIdentityError` without modifying the tree.

The :class:`TaxonCatalog` Class
===============================
.. autoclass:: dendropy.datamodel.taxoncatalogmodel.TaxonCatalog
    :members:

The :class:`TaxonCatalogVersion` Class
======================================
.. autoclass:: dendropy.datamodel.taxoncatalogmodel.TaxonCatalogVersion
    :members:

The :class:`TaxonCatalogRevision` Class
=======================================
.. autoclass:: dendropy.datamodel.taxoncatalogmodel.TaxonCatalogRevision
    :members:

Exceptions
==========
.. autoclass:: dendropy.datamodel.taxoncatalogmodel.TaxonCatalogCorrectionError
    :members:

.. autoclass:: dendropy.datamodel.taxoncatalogmodel.IndeterminateLeafIdentityError
    :members:
