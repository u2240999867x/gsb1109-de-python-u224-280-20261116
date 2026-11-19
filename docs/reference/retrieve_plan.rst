.. _api_retrieve_plan:

.. py:module:: pynetdicom.retrieve_plan

Retrieval Route Planning (:mod:`pynetdicom.retrieve_plan`)
==========================================================

.. currentmodule:: pynetdicom.retrieve_plan

This module provides pre-execution planning of minimum-cost DICOM retrieval
routes using C-GET and/or C-MOVE, with separate query and reception
conditions, blocking-combination reporting and incremental replanning when
station capabilities change.

Planner
-------

.. autosummary::
   :toctree: generated/

   RetrievalPlanner
   RetrievalPlan
   LegPlan
   BlockedClass
   BlockedRoute

Route and Capabilities
----------------------

.. autosummary::
   :toctree: generated/

   RouteOption
   StationCapabilities
   capabilities_from_contexts
   QueryRequirement
   ReceptionRequirement

Enumerations and Materials
--------------------------

.. autosummary::
   :toctree: generated/

   RetrieveMethod
   Material
   MaterialState
   QueryOutcome
