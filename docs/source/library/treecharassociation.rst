***************************************************************************
:mod:`dendropy.datamodel.treecharassociation`: Tree / Character Matrix Association
***************************************************************************

.. module:: dendropy.datamodel.treecharassociation

A |TreeCharacterMatrixAssociation| associates the leaf taxa of a tree with the
rows of a |CharacterMatrix| using explicit |Taxon| object identity. Because
association is by identity rather than positional index or label text,
reordering the leaves of a tree never changes which matrix row a leaf is
associated with, and taxa that share a label are never arbitrarily matched to a
row: each distinct |Taxon| is matched only to the row keyed by that same
instance.

Two distinct mismatch conditions are always reported separately:

* *Tree leaves without matrix rows*: a leaf references a |Taxon| for which the
  matrix has no sequence (missing matrix rows).
* *Matrix rows without tree leaves*: the matrix carries a sequence for a
  |Taxon| that does not occur as a leaf of the tree (extra/unmatched matrix
  rows).

The original matrix rows (the actual |CharacterDataSequence| objects) and the
per-column |StateIdentity| values are referenced directly and never reordered
or copied, so missing-data, gap, ambiguous, and polymorphic states remain
distinguishable.

Association
===========

.. autoclass:: dendropy.datamodel.treecharassociation.TreeCharacterMatrixAssociation
    :members:

State Categories
================

.. autodata:: dendropy.datamodel.treecharassociation.FUNDAMENTAL_STATE_CATEGORY
.. autodata:: dendropy.datamodel.treecharassociation.GAP_STATE_CATEGORY
.. autodata:: dendropy.datamodel.treecharassociation.MISSING_DATA_STATE_CATEGORY
.. autodata:: dendropy.datamodel.treecharassociation.AMBIGUOUS_STATE_CATEGORY
.. autodata:: dendropy.datamodel.treecharassociation.POLYMORPHIC_STATE_CATEGORY
.. autodata:: dendropy.datamodel.treecharassociation.CONTINUOUS_STATE_CATEGORY

.. autofunction:: dendropy.datamodel.treecharassociation.classify_character_state
