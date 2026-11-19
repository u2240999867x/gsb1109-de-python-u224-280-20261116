.. _api_association_planning:

.. py:module:: pynetdicom.association_planning

Association Proposal Planning (:mod:`pynetdicom.association_planning`)
======================================================================

.. currentmodule:: pynetdicom.association_planning

The Association Proposal Planning module splits a set of required SOP class
combinations into the minimum number of association proposals that respect
the presentation context limit, while selecting the transfer syntax with the
lowest expected conversion cost for each combination.

The output is a proposal only: whether a presentation context is accepted and
which transfer syntax is selected is determined by the association
*Acceptor* during negotiation.

Requirements
------------

.. autosummary::
   :toctree: generated/

   PlanningRequirement

Proposals
---------

.. autosummary::
   :toctree: generated/

   AssociationProposal
   plan_associations

Exceptions
----------

.. autosummary::
   :toctree: generated/

   AssociationPlanError
