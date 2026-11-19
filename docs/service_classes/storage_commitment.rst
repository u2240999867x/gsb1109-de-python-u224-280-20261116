.. _service_storecommit:

Storage Commitment Service Class
================================
The :dcm:`Storage Commitment Service Class<part04/chapter_J.html>`
defines a service that uses the DIMSE N-ACTION and N-EVENT-REPORT services to
provide a mechanism for the SCU to request the SCP make a commitment for the
safekeeping of SOP Instances.

.. _commitment_sops:

Supported SOP Classes
---------------------

+----------------------------+------------------------------------------------+
| UID                        | SOP Class                                      |
+============================+================================================+
| 1.2.840.10008.1.20.1       | StorageCommitmentPushModel                     |
+----------------------------+------------------------------------------------+


DIMSE Services
--------------

+-----------------+-----------------------------------------+
| DIMSE Service   | Usage SCU/SCP                           |
+=================+=========================================+
| N-EVENT-REPORT  | Mandatory/Mandatory                     |
+-----------------+-----------------------------------------+
| N-ACTION        | Mandatory/Mandatory                     |
+-----------------+-----------------------------------------+


.. _commitment_statuses:

Statuses
--------

N-ACTION Statuses
~~~~~~~~~~~~~~~~~

+------------------+----------+-----------------------------------------------+
| Code (hex)       | Category | Description                                   |
+==================+==========+===============================================+
| 0x0000           | Success  | Success                                       |
+------------------+----------+-----------------------------------------------+
| 0x0112           | Failure  | No such SOP Instance                          |
+------------------+----------+-----------------------------------------------+
| 0x0114           | Failure  | No such argument                              |
+------------------+----------+-----------------------------------------------+
| 0x0115           | Failure  | Invalid argument value                        |
+------------------+----------+-----------------------------------------------+
| 0x0117           | Failure  | Invalid object instance                       |
+------------------+----------+-----------------------------------------------+
| 0x0118           | Failure  | No such SOP Class                             |
+------------------+----------+-----------------------------------------------+
| 0x0119           | Failure  | Class-Instance conflict                       |
+------------------+----------+-----------------------------------------------+
| 0x0123           | Failure  | No such action                                |
+------------------+----------+-----------------------------------------------+
| 0x0124           | Failure  | Refused: not authorised                       |
+------------------+----------+-----------------------------------------------+
| 0x0210           | Failure  | Duplicate invocation                          |
+------------------+----------+-----------------------------------------------+
| 0x0211           | Failure  | Unrecognised operation                        |
+------------------+----------+-----------------------------------------------+
| 0x0212           | Failure  | Mistyped argument                             |
+------------------+----------+-----------------------------------------------+
| 0x0213           | Failure  | Resource limitation                           |
+------------------+----------+-----------------------------------------------+

N-EVENT-REPORT Statuses
~~~~~~~~~~~~~~~~~~~~~~~

+------------------+----------+----------------------------------+
| Code (hex)       | Category | Description                      |
+==================+==========+==================================+
| 0x0000           | Success  | Success                          |
+------------------+----------+----------------------------------+
| 0x0110           | Failure  | Processing failure               |
+------------------+----------+----------------------------------+
| 0x0112           | Failure  | No such SOP Instance             |
+------------------+----------+----------------------------------+
| 0x0113           | Failure  | No such event type               |
+------------------+----------+----------------------------------+
| 0x0114           | Failure  | No such argument                 |
+------------------+----------+----------------------------------+
| 0x0115           | Failure  | Invalid argument value           |
+------------------+----------+----------------------------------+
| 0x0117           | Failure  | Invalid object Instance          |
+------------------+----------+----------------------------------+
| 0x0118           | Failure  | No such SOP Class                |
+------------------+----------+----------------------------------+
| 0x0119           | Failure  | Class-Instance conflict          |
+------------------+----------+----------------------------------+
| 0x0210           | Failure  | Duplicate invocation             |
+------------------+----------+----------------------------------+
| 0x0211           | Failure  | Unrecognised operation           |
+------------------+----------+----------------------------------+
| 0x0212           | Failure  | Mistyped argument                |
+------------------+----------+----------------------------------+
| 0x0213           | Failure  | Resource limitation              |
+------------------+----------+----------------------------------+


.. _commitment_handover:

Handover Archive
----------------

The :class:`~pynetdicom.handover.HandoverArchive` provides a persistent
record that links a completed transfer to its later Storage Commitment
result.  It is intended for the common workflow where C-STORE requests finish
first and the commitment result (N-EVENT-REPORT) may arrive much later, or on
a different association::

    from pynetdicom.handover import HandoverArchive

    archive = HandoverArchive("handover.json")

    # Record the completed C-STORE batch
    archive.record_transfer(
        "1.2.826.0.1.3680043.9.3811.1",
        [
            ("1.2.840.10008.5.1.4.1.1.2", "1.2.3.1"),
            ("1.2.840.10008.5.1.4.1.1.2", "1.2.3.2"),
        ],
        transferred_at="2026-01-01T10:00:00Z",
        source_ae="STORESCU",
        destination_ae="QRSCP",
    )

    # Record the later commitment result
    archive.record_commitment_report_dataset(event.event_information)

Each record is keyed by the (0008,1195) *Transaction UID* and keeps the
original transfer time separate from the commitment time.  A report that
arrives after the transfer changes the record's handover status but never
rewrites ``transferred_at``.

Committed and failed instances are kept in separate collections, so a
partially successful commitment remains visible::

    record = archive["1.2.826.0.1.3680043.9.3811.1"]
    record.committed_instances  # tuple of SOPInstanceReference
    record.failed_instances     # tuple of FailedSOPInstanceReference
    record.status               # CommitmentStatus

Matching rules
~~~~~~~~~~~~~~

* A commitment result is matched **only** by its *Transaction UID*.
* A result whose transaction UID is unknown is retained separately in
  :attr:`~pynetdicom.handover.HandoverArchive.unmatched_reports`.  It is never
  attached to another transfer whose instance manifest happens to contain the
  same SOP Instance UIDs.
* A known transaction whose result references instances outside the
  recorded manifest raises
  :class:`~pynetdicom.handover.CommitmentReportMismatch`.
* When a result arrives before its transfer was registered, recording the
  transfer reconciles the pending result by transaction UID.

Status values are ``transferred``, ``commitment_requested``, ``committed``,
``partially_committed``, ``commitment_failed`` and ``commitment_incomplete``.
