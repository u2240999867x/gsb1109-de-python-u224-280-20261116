.. _api_retrieval:

.. py:module:: pynetdicom.retrieval

Retrieval Tracking (:mod:`pynetdicom.retrieval`)
================================================

.. currentmodule:: pynetdicom.retrieval

The :class:`RetrievalLedger` keeps the evidence from C-GET and C-MOVE
retrieval tasks so that a C-STORE arrival is only attributed to a task when
the command or association evidence uniquely supports it. A matching
*SOP Instance UID* alone is never sufficient.

Each :class:`~pynetdicom.association.Association` owns a ledger, available as
:attr:`~pynetdicom.association.Association.retrieval_ledger`. The ledger
retains the retrieval request, any cancellation confirmation, the individual
C-STORE sub-operations and the material actually received. Material shared
between several candidate tasks may verify and be shared, but it is never
counted as a successful sub-operation more than once, and cancelling a task
neither erases its previously verified instances nor affects any other task.

.. autosummary::
   :toctree: generated/

   RetrievalLedger
   RetrievalTask
   RetrievalSubOperation
   RetrievalStore
