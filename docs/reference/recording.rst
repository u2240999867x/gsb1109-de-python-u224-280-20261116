.. _api_recording:

.. py:module:: pynetdicom.recording

Recording Inspection (:mod:`pynetdicom.recording`)
==================================================

Offline inspection and auditing of recorded DICOM associations.

This module decodes captured A-ASSOCIATE and P-DATA PDUs and reconstructs the
presentation contexts that were actually negotiated for each association. Each
message is then checked against its association's context view.

.. currentmodule:: pynetdicom.recording

.. autosummary::
   :toctree: generated/

   inspect_recording
   read_recording
   AssociationRecord
   ContextView
   MessageRecord
   MessageStatus
   IssueCode
   Role
   Direction
   SOPClassContext
   RecordingError
