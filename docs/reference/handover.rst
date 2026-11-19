.. _api_handover:

.. py:module:: pynetdicom.handover

Handover Archive (:mod:`pynetdicom.handover`)
=============================================

.. currentmodule:: pynetdicom.handover

The handover archive records completed transfers and the Storage Commitment
results that follow them.

.. autosummary::
   :toctree: generated/

   HandoverArchive
   TransferRecord
   CommitmentReport
   CommitmentRequest
   CommitmentStatus
   SOPInstanceReference
   FailedSOPInstanceReference
   UnmatchedCommitmentReport
   UnmatchedCommitmentRequest
   failure_reason_text
   parse_commitment_report
   parse_commitment_request
   commitment_report_from_event
   commitment_request_from_event
   record_from_event
