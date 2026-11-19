Handover Archive Examples
~~~~~~~~~~~~~~~~~~~~~~~~~

The :mod:`pynetdicom.handover` module records completed transfers and the
Storage Commitment results that follow them.  It can be used independently of
the networking layer and persisted as JSON.

Record a transfer
.................

The transfer's (0008,1195) *Transaction UID* identifies the batch.  The
instance manifest should be the SOP Instances that completed C-STORE.

.. code-block:: python

    from pynetdicom.handover import HandoverArchive

    archive = HandoverArchive("handover.json")
    archive.record_transfer(
        transaction_uid="1.2.826.0.1.3680043.9.3811.1",
        instances=[
            ("1.2.840.10008.5.1.4.1.1.2", "1.2.3.1"),
            ("1.2.840.10008.5.1.4.1.1.2", "1.2.3.2"),
        ],
        transferred_at="2026-01-01T10:00:00Z",
        source_ae="STORESCU",
        destination_ae="QRSCP",
    )

Record a commitment result
..........................

Storage Commitment uses N-EVENT-REPORT.  The result is matched using only the
transaction UID; the SOP Instance UIDs are not used to find a batch.

.. code-block:: python

    from pynetdicom.handover import (
        CommitmentReport,
        FailedSOPInstanceReference,
        SOPInstanceReference,
    )

    report = CommitmentReport(
        transaction_uid="1.2.826.0.1.3680043.9.3811.1",
        committed_instances=(
            SOPInstanceReference("1.2.840.10008.5.1.4.1.1.2", "1.2.3.1"),
        ),
        failed_instances=(
            FailedSOPInstanceReference(
                "1.2.840.10008.5.1.4.1.1.2",
                "1.2.3.2",
                failure_reason=2,
            ),
        ),
    )
    record = archive.record_commitment_report(report)

    record.status  # CommitmentStatus.PARTIALLY_COMMITTED
    record.committed_instances
    record.failed_instances

Late results
............

A commitment result may arrive after an association has closed.  Recording it
updates ``status`` and the committed/failed lists, but keeps
``transferred_at`` unchanged.  If the result arrived before the transfer was
registered, recording the transfer reconciles the pending result.

Unknown transactions
....................

A result for a transaction UID that is not in the archive is retained in
``archive.unmatched_reports``.  It is never attached to another batch that
contains the same SOP Instance UIDs, because SOP Instance UIDs are not unique
across transactions.  The same rule applies to commitment requests.
