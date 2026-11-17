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
##############################################################################

"""
Versioned taxon catalogs and leaf-identity corrections.

A :class:`TaxonCatalog` is a shared directory of leaf identities (taxon
concepts) used by one or more trees. Taxonomic work does not invalidate old
results: every correction (a rename, an alias merge, or the undoing of an
erroneous merge by splitting) produces a *new, immutable*
:class:`TaxonCatalogVersion` instead of mutating the version against which
existing trees and comparisons were interpreted.

Each version owns its own :class:`~dendropy.datamodel.taxonmodel.TaxonNamespace`
and a full revision history, so every leaf identity in any version can be
traced back to the label declared in the base (version 0) catalog.

Trees are migrated explicitly with :meth:`TaxonCatalog.migrate_tree` /
:meth:`TaxonCatalog.migrate_trees`. Migration remaps leaf :class:`Taxon`
objects and invalidates every derived cache on the migrated trees (bipartition
encodings, split bitmask edge maps), so splits, distances and leaf-set queries
are recomputed against the target version rather than silently reusing stale
bitmasks. Internal node labels never participate in leaf-identity corrections;
if a split cannot determine the destination of every affected leaf, no
migration occurs and an :class:`IndeterminateLeafIdentityError` listing the
unresolved leaves is raised.
"""

import copy

from dendropy.datamodel import basemodel
from dendropy.datamodel.taxonmodel import Taxon
from dendropy.datamodel.taxonmodel import TaxonNamespace


##############################################################################
## Exceptions

class TaxonCatalogCorrectionError(ValueError):
    """An invalid taxon catalog correction was requested."""

class IndeterminateLeafIdentityError(TaxonCatalogCorrectionError):
    """
    A split correction cannot be applied to a tree because the destination of
    one or more affected leaves was not specified. No state on the tree is
    modified when this exception is raised.
    """

    def __init__(self, message, indeterminate_leaves=None):
        TaxonCatalogCorrectionError.__init__(self, message)
        if indeterminate_leaves is None:
            indeterminate_leaves = []
        self.indeterminate_leaves = list(indeterminate_leaves)


##############################################################################
## Constants

RENAME_OPERATION = "rename"
MERGE_OPERATION = "merge"
SPLIT_OPERATION = "split"

VERSION_ANNOTATION_NAME = "taxonCatalogVersion"
ORIGIN_VERSION_ANNOTATION_NAME = "taxonCatalogOriginVersion"
ORIGIN_LABEL_ANNOTATION_NAME = "taxonCatalogOriginLabel"
IDENTITY_CHAIN_ANNOTATION_NAME = "taxonCatalogIdentityChain"
_TREE_VERSION_ANNOTATION_NAME = "taxonCatalogTreeVersion"


##############################################################################
## Revision record

class TaxonCatalogRevision(basemodel.DataObject):
    """
    An immutable record of a single catalog correction.

    Attributes
    ----------
    revision_index : int
        Zero-based ordinal of this correction within the catalog. The version
        produced by the correction is ``revision_index + 1``.
    operation : str
        One of :data:`RENAME_OPERATION`, :data:`MERGE_OPERATION`, or
        :data:`SPLIT_OPERATION`.
    affected_labels : tuple[str]
        Label(s) in the source version consumed by this correction.
    new_labels : tuple[str]
        Label(s) introduced in the resulting version.
    rationale : str or None
        Free-text justification for the correction.
    """

    def __init__(self, revision_index, operation, affected_labels, new_labels,
            rationale=None):
        basemodel.DataObject.__init__(self, label=None)
        self.revision_index = revision_index
        self.operation = operation
        self.affected_labels = tuple(affected_labels)
        self.new_labels = tuple(new_labels)
        self.rationale = rationale

    def __repr__(self):
        return ("<{} {}: {} {} -> {}>".format(
            self.__class__.__name__,
            self.revision_index,
            self.operation,
            list(self.affected_labels),
            list(self.new_labels),
        ))

    def as_dict(self):
        """Returns a plain-dictionary serialization of this revision."""
        return {
            "revision_index": self.revision_index,
            "operation": self.operation,
            "affected_labels": list(self.affected_labels),
            "new_labels": list(self.new_labels),
            "rationale": self.rationale,
        }


##############################################################################
## Catalog version

class TaxonCatalogVersion(basemodel.DataObject):
    """
    An immutable snapshot of a :class:`TaxonCatalog` at a point in time.

    Attributes
    ----------
    version : int
        Version number, with the initial catalog declaration at version 0.
    taxon_namespace : |TaxonNamespace|
        The taxon namespace (leaf identity directory) for this version.
    revisions : tuple[TaxonCatalogRevision]
        The complete, ordered chain of corrections separating the base
        declaration (version 0) from this version. Empty for version 0.
    label_origin_map : dict[str, tuple[int, str]]
        Maps every label of this version to the ``(version, label)`` pair from
        which it was directly derived. Base-version labels map to themselves.
    label_lineage_map : dict[str, tuple[tuple[int, str], ...]]
        Maps every label to its full identity chain, from the base declaration
        at version 0 to the label in this version.
    historical_lineage_map : dict[str, tuple[tuple[int, str], ...]]
        Archives the identity chains of labels no longer declared in this
        version (consumed by renames or merges). This allows a later split that
        undoes an erroneous merge to restore each product to its original base
        declaration rather than to the merged identity.
    """

    def __init__(self, version, taxon_namespace, revisions, label_origin_map,
            label_lineage_map, historical_lineage_map=None):
        basemodel.DataObject.__init__(self, label=None)
        self.version = version
        self.taxon_namespace = taxon_namespace
        self.revisions = tuple(revisions)
        self.label_origin_map = dict(label_origin_map)
        self.label_lineage_map = {
            label: tuple(chain)
            for label, chain in label_lineage_map.items()
        }
        if historical_lineage_map is None:
            historical_lineage_map = {}
        self.historical_lineage_map = {
            label: tuple(chain)
            for label, chain in historical_lineage_map.items()
        }

    def __repr__(self):
        return "<{} v{}: {}>".format(
            self.__class__.__name__,
            self.version,
            self.taxon_namespace.labels(),
        )

    @property
    def labels(self):
        """Tuple of leaf labels declared in this version."""
        return tuple(self.taxon_namespace.labels())

    def label_taxon_map(self):
        """Maps each label of this version to its |Taxon| object."""
        return self.taxon_namespace.label_taxon_map()

    def lineage(self, label):
        """
        Returns the identity chain of ``label`` as a list of
        ``(version, label)`` pairs, starting at the base declaration (version
        0) and ending at this version.

        Raises
        ------
        KeyError
            If ``label`` is not declared in this version.
        """
        if label not in self.label_lineage_map:
            raise KeyError(label)
        return list(self.label_lineage_map[label])

    def historical_lineage(self, label):
        """
        Returns the archived identity chain of a label that is no longer
        declared in this version (e.g. a label consumed by a rename or merge).

        Raises
        ------
        KeyError
            If no historical identity chain is archived for ``label``.
        """
        if label not in self.historical_lineage_map:
            raise KeyError(label)
        return list(self.historical_lineage_map[label])

    @property
    def historical_labels(self):
        """Labels whose identity chains are archived but not declared here."""
        return tuple(self.historical_lineage_map.keys())


##############################################################################
## Catalog

class TaxonCatalog(basemodel.Annotable):
    """
    A shared, versioned directory of leaf identities.

    The initial set of labels constitutes version 0. Each correction returns a
    new :class:`TaxonCatalogVersion` (also available via
    :attr:`current_version`) while all earlier versions remain available and
    interpretable.

    Parameters
    ----------
    taxon_labels : iterable[str], optional
        Labels of the base (version 0) catalog declaration.
    label : str, optional
        Name for this catalog.
    is_case_sensitive : bool, optional
        Whether label matching is case-sensitive (default |False|, matching
        :class:`TaxonNamespace` conventions).
    """

    def __init__(self, taxon_labels=None, label=None, is_case_sensitive=False):
        basemodel.Annotable.__init__(self)
        self.label = label
        self.is_case_sensitive = is_case_sensitive
        base_taxon_namespace = TaxonNamespace(
            label=label, is_case_sensitive=is_case_sensitive)
        if taxon_labels is not None:
            base_taxon_namespace.new_taxa(taxon_labels)
        base_origin_map = {
            taxon.label: (0, taxon.label)
            for taxon in base_taxon_namespace
        }
        base_lineage_map = {
            taxon.label: ((0, taxon.label),)
            for taxon in base_taxon_namespace
        }
        base_version = TaxonCatalogVersion(
                version=0,
                taxon_namespace=base_taxon_namespace,
                revisions=(),
                label_origin_map=base_origin_map,
                label_lineage_map=base_lineage_map,
            )
        self._versions = [base_version]
        self._annotate_version_taxa(base_version)

    ## Version access

    @property
    def base_version(self):
        """The original catalog declaration (version 0)."""
        return self._versions[0]

    @property
    def base_taxon_namespace(self):
        """|TaxonNamespace| of the original catalog declaration."""
        return self._versions[0].taxon_namespace

    @property
    def current_version(self):
        """Most recently created :class:`TaxonCatalogVersion`."""
        return self._versions[-1]

    @property
    def current_version_number(self):
        """Version number of :attr:`current_version`."""
        return self._versions[-1].version

    def __len__(self):
        return len(self._versions)

    def __iter__(self):
        return iter(self._versions)

    def get_version(self, version):
        """
        Returns the :class:`TaxonCatalogVersion` with the given version number.

        Negative indices count from the current version, following Python
        sequence conventions.
        """
        return self._versions[version]

    def versions(self):
        """Returns a tuple of all catalog versions, oldest first."""
        return tuple(self._versions)

    ## Corrections

    def rename_taxon(self, old_label, new_label, rationale=None):
        """
        Declares that the leaf identity known as ``old_label`` is now known as
        ``new_label``. Returns the new :class:`TaxonCatalogVersion`.
        """
        revision = self._build_revision(
            operation=RENAME_OPERATION,
            affected_labels=[old_label],
            new_labels=[new_label],
            rationale=rationale,
        )
        return self._commit_revision(revision)

    def merge_taxa(self, source_labels, new_label=None, rationale=None):
        """
        Declares that several leaf identities (aliases) are a single identity.

        Parameters
        ----------
        source_labels : iterable[str]
            Labels to merge; every label must be declared in the current
            version.
        new_label : str, optional
            Label for the merged identity. Defaults to the first source label.
        rationale : str, optional
            Free-text justification.

        Returns
        -------
        TaxonCatalogVersion
        """
        source_labels = list(source_labels)
        if len(source_labels) < 2:
            raise TaxonCatalogCorrectionError(
                "Merging requires at least two source labels (got {})".format(
                    source_labels))
        if len(set(source_labels)) != len(source_labels):
            raise TaxonCatalogCorrectionError(
                "Merge source labels must be distinct (got {})".format(
                    source_labels))
        if new_label is None:
            new_label = source_labels[0]
        revision = self._build_revision(
            operation=MERGE_OPERATION,
            affected_labels=source_labels,
            new_labels=[new_label],
            rationale=rationale,
        )
        return self._commit_revision(revision)

    def split_taxon(self, source_label, new_labels, rationale=None):
        """
        Declares that one identity (typically an earlier erroneous merge) is
        actually several distinct identities.

        The destinations of individual leaves on any migrated tree are given
        later, at migration time, via ``leaf_destinations``. A tree containing
        leaves of ``source_label`` without an explicit destination cannot be
        migrated and raises :class:`IndeterminateLeafIdentityError`.

        Parameters
        ----------
        source_label : str
            Label of the identity to split; must be declared in the current
            version.
        new_labels : iterable[str]
            Two or more labels for the resulting identities; none may collide
            with an existing label other than ``source_label``.
        rationale : str, optional
            Free-text justification.

        Returns
        -------
        TaxonCatalogVersion
        """
        new_labels = list(new_labels)
        if len(new_labels) < 2:
            raise TaxonCatalogCorrectionError(
                "Splitting requires at least two new labels (got {})".format(
                    new_labels))
        if len(set(new_labels)) != len(new_labels):
            raise TaxonCatalogCorrectionError(
                "Split products must be distinct (got {})".format(new_labels))
        revision = self._build_revision(
            operation=SPLIT_OPERATION,
            affected_labels=[source_label],
            new_labels=new_labels,
            rationale=rationale,
        )
        return self._commit_revision(revision)

    def apply_corrections(self, corrections, rationale=None):
        """
        Applies an ordered series of corrections, returning the final new
        :class:`TaxonCatalogVersion`. One new version is created per
        correction.

        Each correction is a dictionary with an ``"operation"`` key:

        ``{"operation": "rename", "source": old, "target": new}``
        ``{"operation": "merge", "sources": [s1, s2, ...], "target": new}``
            (``"target"`` optional)
        ``{"operation": "split", "source": s, "targets": [t1, t2, ...]}``
        """
        version = None
        for correction in corrections:
            spec = dict(correction)
            operation = spec.pop("operation")
            spec_rationale = spec.pop("rationale", rationale)
            if operation == RENAME_OPERATION:
                version = self.rename_taxon(
                    old_label=spec["source"],
                    new_label=spec["target"],
                    rationale=spec_rationale,
                )
            elif operation == MERGE_OPERATION:
                version = self.merge_taxa(
                    source_labels=spec["sources"],
                    new_label=spec.get("target"),
                    rationale=spec_rationale,
                )
            elif operation == SPLIT_OPERATION:
                version = self.split_taxon(
                    source_label=spec["source"],
                    new_labels=spec["targets"],
                    rationale=spec_rationale,
                )
            else:
                raise TaxonCatalogCorrectionError(
                    "Unrecognized catalog correction operation: '{}'".format(
                        operation))
        if version is None:
            raise TaxonCatalogCorrectionError(
                "No corrections were specified")
        return version

    def _build_revision(self, operation, affected_labels, new_labels,
            rationale):
        source_version = self.current_version
        source_tns = source_version.taxon_namespace
        for label in affected_labels:
            if not source_tns.has_taxon_label(label):
                raise TaxonCatalogCorrectionError(
                    "Source label '{}' is not declared in catalog version {}"
                    " (labels: {})".format(
                        label, source_version.version,
                        source_tns.labels()))
        if operation == RENAME_OPERATION:
            new_label = new_labels[0]
            if source_tns.has_taxon_label(new_label):
                raise TaxonCatalogCorrectionError(
                    "Cannot rename '{}' to '{}': label already exists in"
                    " catalog version {}".format(
                        affected_labels[0], new_label,
                        source_version.version))
        elif operation == SPLIT_OPERATION:
            source_label = affected_labels[0]
            for new_label in new_labels:
                if (new_label != source_label
                        and source_tns.has_taxon_label(new_label)):
                    raise TaxonCatalogCorrectionError(
                        "Cannot split '{}' into '{}': label already exists in"
                        " catalog version {}".format(
                            source_label, new_label,
                            source_version.version))
        elif operation == MERGE_OPERATION:
            target_label = new_labels[0]
            if (target_label not in affected_labels
                    and source_tns.has_taxon_label(target_label)):
                raise TaxonCatalogCorrectionError(
                    "Merge target '{}' already exists and is not among the"
                    " merged source labels".format(target_label))
        return TaxonCatalogRevision(
            revision_index=len(source_version.revisions),
            operation=operation,
            affected_labels=affected_labels,
            new_labels=new_labels,
            rationale=rationale,
        )

    def _commit_revision(self, revision):
        source_version = self.current_version
        new_tns = copy.deepcopy(source_version.taxon_namespace)
        new_tns.label = self.label
        new_origin_map = dict(source_version.label_origin_map)
        new_lineage_map = {
            label: tuple(chain)
            for label, chain in source_version.label_lineage_map.items()
        }
        # carry forward all previously archived identity chains
        new_historical_map = {
            label: tuple(chain)
            for label, chain in source_version.historical_lineage_map.items()
        }
        new_revisions = source_version.revisions + (revision,)
        target_version_number = source_version.version + 1

        if revision.operation == RENAME_OPERATION:
            old_label = revision.affected_labels[0]
            new_label = revision.new_labels[0]
            taxon = new_tns.get_taxon(old_label)
            taxon.label = new_label
            prior_chain = new_lineage_map.pop(old_label)
            new_historical_map[old_label] = prior_chain
            new_origin_map.pop(old_label, None)
            new_origin_map[new_label] = (
                source_version.version, old_label)
            new_lineage_map[new_label] = prior_chain + (
                (target_version_number, new_label),)

        elif revision.operation == MERGE_OPERATION:
            target_label = revision.new_labels[0]
            retained_chain = None
            for source_label in revision.affected_labels:
                source_taxon = new_tns.get_taxon(source_label)
                source_chain = new_lineage_map.pop(source_label)
                new_historical_map[source_label] = source_chain
                new_origin_map.pop(source_label, None)
                if source_label == target_label:
                    retained_chain = source_chain
                if source_taxon is not None and new_tns.get_taxon(target_label) is not source_taxon:
                    new_tns.remove_taxon(source_taxon)
            if retained_chain is None:
                # merged identity carries a brand-new label: representative
                # chain follows the first merged source; every source chain
                # remains recoverable from the historical archive
                retained_chain = source_version.label_lineage_map[
                    revision.affected_labels[0]]
                target_taxon = Taxon(label=target_label)
                new_tns.add_taxon(target_taxon)
            new_origin_map[target_label] = (
                source_version.version,
                revision.affected_labels[0],
            )
            new_lineage_map[target_label] = retained_chain + (
                (target_version_number, target_label),)

        elif revision.operation == SPLIT_OPERATION:
            source_label = revision.affected_labels[0]
            source_taxon = new_tns.get_taxon(source_label)
            source_chain = new_lineage_map.pop(source_label)
            new_historical_map[source_label] = source_chain
            new_origin_map.pop(source_label, None)
            new_tns.remove_taxon(source_taxon)
            for new_label in revision.new_labels:
                if new_label in new_historical_map:
                    # restoring a previously consumed identity (e.g. undoing
                    # an erroneous merge): trace it to its original declaration
                    # while retaining the consumed merged identity as an
                    # intermediate step
                    product_chain = (
                        new_historical_map[new_label]
                        + ((source_version.version, source_label),)
                    )
                else:
                    # genuinely new identity: inherit the split source's chain
                    product_chain = source_chain
                new_tns.add_taxon(Taxon(label=new_label))
                new_origin_map[new_label] = (
                    source_version.version, source_label)
                new_lineage_map[new_label] = product_chain + (
                    (target_version_number, new_label),)

        else:
            raise TaxonCatalogCorrectionError(
                "Unrecognized catalog correction operation: '{}'".format(
                    revision.operation))

        new_version = TaxonCatalogVersion(
            version=target_version_number,
            taxon_namespace=new_tns,
            revisions=new_revisions,
            label_origin_map=new_origin_map,
            label_lineage_map=new_lineage_map,
            historical_lineage_map=new_historical_map,
        )
        self._annotate_version_taxa(new_version)
        self._versions.append(new_version)
        return new_version

    def _annotate_version_taxa(self, version):
        provenance_names = (
            VERSION_ANNOTATION_NAME,
            ORIGIN_VERSION_ANNOTATION_NAME,
            ORIGIN_LABEL_ANNOTATION_NAME,
            IDENTITY_CHAIN_ANNOTATION_NAME,
        )
        for taxon in version.taxon_namespace:
            # deep-copied taxa may carry provenance from an earlier version:
            # replace it rather than accumulate stale annotations
            if taxon.has_annotations:
                stale = [
                    annotation for annotation in taxon.annotations
                    if annotation.name in provenance_names
                ]
                for annotation in stale:
                    taxon.annotations.discard(annotation)
            chain = version.label_lineage_map[taxon.label]
            origin_version, origin_label = version.label_origin_map[taxon.label]
            chain_string = "|".join(
                "{}:{}".format(step_version, step_label)
                for step_version, step_label in chain
            )
            annotations = taxon.annotations
            annotations.add_new(
                VERSION_ANNOTATION_NAME, version.version,
                datatype_hint="xsd:integer")
            annotations.add_new(
                ORIGIN_VERSION_ANNOTATION_NAME, origin_version,
                datatype_hint="xsd:integer")
            annotations.add_new(
                ORIGIN_LABEL_ANNOTATION_NAME, origin_label,
                datatype_hint="xsd:string")
            annotations.add_new(
                IDENTITY_CHAIN_ANNOTATION_NAME, chain_string,
                datatype_hint="xsd:string")

    ## Provenance queries

    def lineage(self, label, version=None):
        """
        Returns the identity chain of ``label`` at ``version`` (default:
        current version) as ``(version, label)`` pairs from the base
        declaration to the requested version.
        """
        return self._resolve_version(version).lineage(label)

    def historical_lineage(self, label, version=None):
        """
        Returns the archived identity chain of a label that has been consumed
        by a correction at or before ``version`` (default: current version).
        """
        return self._resolve_version(version).historical_lineage(label)

    def provenance(self, taxon):
        """
        Reads the catalog provenance recorded on a migrated |Taxon| object.

        Returns a dictionary with keys ``"version"``, ``"origin_version"``,
        ``"origin_label"`` and ``"identity_chain"``. Returns |None| for taxa
        that carry no catalog provenance (e.g. internal node labels).
        """
        if not hasattr(taxon, "annotations") or not taxon.has_annotations:
            return None
        values = {}
        for annotation in taxon.annotations:
            if annotation.name == VERSION_ANNOTATION_NAME:
                values["version"] = annotation.value
            elif annotation.name == ORIGIN_VERSION_ANNOTATION_NAME:
                values["origin_version"] = annotation.value
            elif annotation.name == ORIGIN_LABEL_ANNOTATION_NAME:
                values["origin_label"] = annotation.value
            elif annotation.name == IDENTITY_CHAIN_ANNOTATION_NAME:
                values["identity_chain"] = annotation.value
        if not values:
            return None
        return values

    ## Tree migration

    def migrate_tree(self, tree, target_version=None, leaf_destinations=None,
            rationale=None):
        """
        Migrates ``tree`` from its current catalog version to ``target_version``
        (default: current version).

        Leaf :class:`Taxon` objects are remapped to the target version's
        taxon namespace; internal node labels are left untouched. All derived
        caches on the tree are invalidated so subsequent split, distance and
        leaf-set calculations use the target version. Every remapped leaf
        retains provenance annotations tracing it to the base declaration.

        Parameters
        ----------
        tree : |Tree|
            Tree to migrate in place.
        target_version : int or TaxonCatalogVersion, optional
            Version to migrate to. Defaults to :attr:`current_version`.
        leaf_destinations : dict, optional
            Required for split corrections. Maps each affected leaf (keyed by
            |Taxon| object or current label) to the destination label in the
            target version.
        rationale : str, optional
            Recorded on the tree-level migration annotation.

        Returns
        -------
        tree : |Tree|
            The same tree object, migrated in place.

        Raises
        ------
        IndeterminateLeafIdentityError
            If a split affects a leaf whose destination is not given; the tree
            is not modified.
        """
        target = self._resolve_version(target_version)
        source_version_number = self._tree_version_number(tree)
        if source_version_number > target.version:
            raise TaxonCatalogCorrectionError(
                "Cannot migrate backwards from catalog version {} to {}"
                .format(source_version_number, target.version))
        normalized_destinations = self._normalize_destinations(
            leaf_destinations)

        # Dry run: resolve every leaf across every revision before touching
        # the tree, so an indeterminate split never leaves a partially
        # remapped tree behind.
        leaf_labels = {
            node: node.taxon.label
            for node in tree.leaf_node_iter()
            if node.taxon is not None
        }
        source_version = self._versions[source_version_number]
        undeclared = sorted({
            label for label in leaf_labels.values()
            if label not in source_version.label_lineage_map
        })
        if undeclared:
            raise TaxonCatalogCorrectionError(
                "Cannot migrate tree: leaf label(s) {} are not declared in"
                " catalog version {} (labels: {})".format(
                    undeclared, source_version.version,
                    list(source_version.labels)))
        simulated_labels = dict(leaf_labels)
        steps = []
        indeterminate = []
        for next_version_index in range(
                source_version_number, target.version):
            next_version = self._versions[next_version_index + 1]
            revision = next_version.revisions[-1]
            step_destinations = {}
            for node, label in simulated_labels.items():
                destination = self._apply_revision_to_label(
                    revision=revision,
                    label=label,
                    start_label=leaf_labels[node],
                    normalized_destinations=normalized_destinations,
                    node=node,
                    indeterminate=indeterminate,
                )
                step_destinations[node] = destination
            steps.append((next_version, step_destinations))
            simulated_labels = step_destinations
        if indeterminate:
            unresolved_nodes = []
            seen = set()
            for node in indeterminate:
                if id(node) not in seen:
                    seen.add(id(node))
                    unresolved_nodes.append(node)
            labels = sorted({
                node.taxon.label for node in unresolved_nodes
            })
            raise IndeterminateLeafIdentityError(
                "Cannot migrate tree to catalog version {}: split"
                " destination not specified for leaf label(s) {}; pass"
                " 'leaf_destinations' for every affected leaf".format(
                    target.version, labels),
                indeterminate_leaves=unresolved_nodes,
            )

        # All destinations resolved: remap leaf taxa only, one version at a
        # time. Internal node taxa are never part of leaf-identity corrections
        # and are deliberately left untouched.
        for next_version, step_destinations in steps:
            for node, destination_label in step_destinations.items():
                target_taxon = next_version.taxon_namespace.get_taxon(
                    destination_label)
                if target_taxon is None:
                    raise TaxonCatalogCorrectionError(
                        "Migration destination label '{}' is not declared in"
                        " catalog version {}".format(
                            destination_label, next_version.version))
                node.taxon = target_taxon
        tree._taxon_namespace = target.taxon_namespace
        self._invalidate_tree_caches(tree)
        self._stamp_tree_version(
            tree=tree,
            target=target,
            source_version_number=source_version_number,
            rationale=rationale,
        )
        return tree

    def migrate_trees(self, trees, target_version=None,
            leaf_destinations=None, rationale=None):
        """
        Migrates one or more trees to ``target_version``.

        A plain iterable of |Tree| objects returns a list of migrated trees.
        A |TreeList| returns a new |TreeList| bound to the target version's
        taxon namespace; the source collection and its trees are left
        untouched, preserving their original interpretation. Trees that are not
        explicitly migrated keep their original taxon namespace, caches and
        interpretation.

        See :meth:`migrate_tree` for ``leaf_destinations`` semantics.
        """
        from dendropy.datamodel.treecollectionmodel import TreeList
        target = self._resolve_version(target_version)
        if isinstance(trees, TreeList):
            migrated_collection = TreeList(
                taxon_namespace=target.taxon_namespace,
                label=trees.label,
            )
            for tree in trees:
                migrated_tree = tree.taxon_namespace_scoped_copy()
                copied_destinations = self._remap_node_destinations(
                    source_tree=tree,
                    copied_tree=migrated_tree,
                    leaf_destinations=leaf_destinations,
                )
                self.migrate_tree(
                    tree=migrated_tree,
                    target_version=target,
                    leaf_destinations=copied_destinations,
                    rationale=rationale,
                )
                migrated_collection.append(
                    migrated_tree, taxon_import_strategy="add")
            return migrated_collection
        migrated = []
        for tree in trees:
            migrated.append(
                self.migrate_tree(
                    tree=tree,
                    target_version=target,
                    leaf_destinations=leaf_destinations,
                    rationale=rationale,
                ))
        return migrated

    ## Internal helpers

    def _resolve_version(self, version):
        if version is None:
            return self.current_version
        if isinstance(version, TaxonCatalogVersion):
            if version not in self._versions:
                raise TaxonCatalogCorrectionError(
                    "TaxonCatalogVersion {} does not belong to this catalog".format(
                        version))
            return version
        return self._versions[version]

    def _tree_version_number(self, tree):
        if hasattr(tree, "annotations") and tree.has_annotations:
            for annotation in tree.annotations:
                if annotation.name == _TREE_VERSION_ANNOTATION_NAME:
                    return annotation.value
        return 0

    def _stamp_tree_version(self, tree, target, source_version_number,
            rationale):
        if hasattr(tree, "annotations"):
            tree.annotations.add_new(
                _TREE_VERSION_ANNOTATION_NAME, target.version,
                datatype_hint="xsd:integer")
            tree.annotations.add_new(
                "taxonCatalogSourceVersion", source_version_number,
                datatype_hint="xsd:integer")
            if rationale is not None:
                tree.annotations.add_new(
                    "taxonCatalogMigrationRationale", rationale,
                    datatype_hint="xsd:string")

    def _remap_node_destinations(self, source_tree, copied_tree,
            leaf_destinations):
        """
        Translates |Node|-keyed ``leaf_destinations`` from ``source_tree`` onto
        the corresponding nodes of a taxon-namespace-scoped copy. Taxon- and
        label-keyed entries pass through unchanged, as scoped copies share the
        original |Taxon| objects.
        """
        if not leaf_destinations:
            return leaf_destinations
        source_nodes = list(source_tree.leaf_node_iter())
        copied_nodes = list(copied_tree.leaf_node_iter())
        node_key_to_copy = {
            id(source_node): copied_node
            for source_node, copied_node in zip(source_nodes, copied_nodes)
        }
        remapped = {}
        for leaf_key, destination in leaf_destinations.items():
            if hasattr(leaf_key, "child_nodes"):
                copied_node = node_key_to_copy.get(id(leaf_key))
                if copied_node is None:
                    # node belongs to another tree in the collection: it is
                    # remapped when that tree is processed, so skip it here
                    continue
                remapped[copied_node] = destination
            else:
                remapped[leaf_key] = destination
        return remapped

    def _normalize_destinations(self, leaf_destinations):
        if not leaf_destinations:
            return {}, {}, {}
        label_destinations = {}
        identity_destinations = {}
        node_destinations = {}
        for leaf_key, destination in leaf_destinations.items():
            if hasattr(leaf_key, "child_nodes"):
                # a Node object: distinguishes leaves even when they share a
                # single Taxon object (e.g. after a merge)
                node_destinations[id(leaf_key)] = destination
                if leaf_key.taxon is not None:
                    identity_destinations[id(leaf_key.taxon)] = destination
                    label_destinations[leaf_key.taxon.label] = destination
            elif isinstance(leaf_key, Taxon):
                identity_destinations[id(leaf_key)] = destination
                label_destinations[leaf_key.label] = destination
            else:
                label_destinations[leaf_key] = destination
        return label_destinations, identity_destinations, node_destinations

    def _apply_revision_to_label(self, revision, label, start_label,
            normalized_destinations, node, indeterminate):
        """
        Returns the label a leaf carrying ``label`` maps to under
        ``revision``. ``start_label`` is the label the leaf carried at the
        start of the migration, which is how label-keyed ``leaf_destinations``
        identify a leaf even after an intermediate merge. A split with no
        declared destination appends ``node`` to ``indeterminate`` and returns
        the label unchanged.
        """
        (label_destinations,
         identity_destinations,
         node_destinations) = normalized_destinations
        if revision.operation == RENAME_OPERATION:
            old_label = revision.affected_labels[0]
            if label == old_label:
                return revision.new_labels[0]
            return label
        if revision.operation == MERGE_OPERATION:
            if label in revision.affected_labels:
                return revision.new_labels[0]
            return label
        if revision.operation == SPLIT_OPERATION:
            source_label = revision.affected_labels[0]
            if label != source_label:
                return label
            candidates = tuple(revision.new_labels)
            destination = node_destinations.get(id(node))
            if destination is None and node.taxon is not None:
                destination = identity_destinations.get(id(node.taxon))
            if destination is None:
                destination = label_destinations.get(start_label)
            if destination is None:
                indeterminate.append(node)
                return label
            if destination not in candidates:
                raise TaxonCatalogCorrectionError(
                    "Split destination '{}' for leaf label '{}' is not one of"
                    " the declared split products {}".format(
                        destination, start_label, list(candidates)))
            return destination
        raise TaxonCatalogCorrectionError(
            "Unrecognized catalog correction operation: '{}'".format(
                revision.operation))

    def _invalidate_tree_caches(self, tree):
        if hasattr(tree, "invalidate_derived_taxon_caches"):
            tree.invalidate_derived_taxon_caches()
        else:
            tree.bipartition_encoding = None
            tree._split_bitmask_edge_map = None
            tree._bipartition_edge_map = None
