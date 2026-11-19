"""Tests for the Storage Commitment handover archive."""

import json

import pytest
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence

from pynetdicom.handover import (
    CommitmentReport,
    CommitmentReportMismatch,
    CommitmentRequest,
    CommitmentRequestMismatch,
    CommitmentStatus,
    FailedSOPInstanceReference,
    HandoverArchive,
    InvalidCommitmentDataset,
    SOPInstanceReference,
    TransactionUIDConflict,
    _as_utc,
    failure_reason_text,
    parse_commitment_report,
    parse_commitment_request,
)
from pynetdicom.sop_class import StorageCommitmentPushModel

CT = "1.2.840.10008.5.1.4.1.1.2"
MR = "1.2.840.10008.5.1.4.1.1.4"


def _dt(value):
    """Return a datetime from a test timestamp."""
    return _as_utc(value)


def ref(instance_uid, class_uid=CT):
    """Return an instance reference."""
    return SOPInstanceReference(class_uid, instance_uid)


def failed(instance_uid, reason=1, class_uid=CT):
    """Return a failed instance reference."""
    return FailedSOPInstanceReference(class_uid, instance_uid, failure_reason=reason)


def report_dataset(transaction_uid, committed=(), failed_instances=()):
    """Return a Storage Commitment N-EVENT-REPORT dataset."""
    ds = Dataset()
    ds.TransactionUID = transaction_uid
    if committed:
        ds.ReferencedSOPSequence = Sequence(
            [
                Dataset()
                for _ in committed
            ]
        )
        for item, (class_uid, instance_uid) in zip(ds.ReferencedSOPSequence, committed):
            item.ReferencedSOPClassUID = class_uid
            item.ReferencedSOPInstanceUID = instance_uid

    if failed_instances:
        ds.FailedSOPSequence = Sequence([Dataset() for _ in failed_instances])
        for item, (class_uid, instance_uid, reason) in zip(
            ds.FailedSOPSequence, failed_instances
        ):
            item.ReferencedSOPClassUID = class_uid
            item.ReferencedSOPInstanceUID = instance_uid
            item.FailureReason = reason

    return ds


class TestTransferRecord:
    def test_transfer_completion(self):
        archive = HandoverArchive()
        record = archive.record_transfer(
            "1.2.3",
            [ref("1.1"), ref("1.2")],
            transferred_at="2026-01-01T10:00:00Z",
        )

        assert record.transaction_uid == "1.2.3"
        assert record.transferred_at.isoformat() == "2026-01-01T10:00:00+00:00"
        assert record.instance_manifest == (ref("1.1"), ref("1.2"))
        assert record.status is CommitmentStatus.TRANSFERRED
        assert archive["1.2.3"] is record

    def test_duplicate_transfer_is_idempotent(self):
        archive = HandoverArchive()
        first = archive.record_transfer(
            "1.2.3", [ref("1.1")], transferred_at="2026-01-01T10:00:00Z"
        )
        second = archive.record_transfer(
            "1.2.3", [ref("1.1")], transferred_at="2026-02-02T10:00:00Z"
        )

        assert first is second
        assert second.transferred_at.isoformat() == "2026-01-01T10:00:00+00:00"

    def test_conflicting_manifest(self):
        archive = HandoverArchive()
        archive.record_transfer("1.2.3", [ref("1.1")])

        with pytest.raises(TransactionUIDConflict):
            archive.record_transfer("1.2.3", [ref("1.2")])

    def test_explicit_manifest_correction_keeps_transfer_time(self):
        archive = HandoverArchive()
        first = archive.record_transfer(
            "1.2.3", [ref("1.1")], transferred_at="2026-01-01T10:00:00Z"
        )
        corrected = archive.record_transfer(
            "1.2.3",
            [ref("1.1"), ref("1.2")],
            transferred_at="2026-02-02T10:00:00Z",
            replace_existing=True,
        )

        assert corrected.transferred_at == first.transferred_at
        assert corrected.instances == (ref("1.1"), ref("1.2"))

    def test_empty_manifest(self):
        archive = HandoverArchive()
        with pytest.raises(ValueError):
            archive.record_transfer("1.2.3", [])


class TestCommitment:
    def test_request_marks_pending(self):
        archive = HandoverArchive()
        archive.record_transfer("1.2.3", [ref("1.1")])

        record = archive.record_commitment_request(
            CommitmentRequest("1.2.3", (ref("1.1"),))
        )
        assert record.status is CommitmentStatus.COMMITMENT_REQUESTED

    def test_full_commitment(self):
        archive = HandoverArchive()
        archive.record_transfer("1.2.3", [ref("1.1"), ref("1.2")])

        record = archive.record_commitment_report(
            CommitmentReport("1.2.3", committed_instances=(ref("1.1"), ref("1.2")))
        )
        assert record.status is CommitmentStatus.COMMITTED
        assert record.is_complete

    def test_partial_failure_is_separate(self):
        archive = HandoverArchive()
        archive.record_transfer("1.2.3", [ref("1.1"), ref("1.2")])

        record = archive.record_commitment_report(
            CommitmentReport(
                "1.2.3",
                committed_instances=(ref("1.1"),),
                failed_instances=(failed("1.2", reason=2),),
            )
        )

        assert record.status is CommitmentStatus.PARTIALLY_COMMITTED
        assert record.committed_instances == (ref("1.1"),)
        assert record.failed_instances == (failed("1.2", reason=2),)
        assert record.failed_instances[0].failure_reason_text == (
            "No such object instance"
        )
        assert record.is_complete

    def test_all_failed(self):
        archive = HandoverArchive()
        archive.record_transfer("1.2.3", [ref("1.1")])
        record = archive.record_commitment_report(
            CommitmentReport("1.2.3", failed_instances=(failed("1.1", reason=1),))
        )
        assert record.status is CommitmentStatus.COMMITMENT_FAILED

    def test_incomplete_commitment(self):
        archive = HandoverArchive()
        archive.record_transfer("1.2.3", [ref("1.1"), ref("1.2")])
        record = archive.record_commitment_report(
            CommitmentReport("1.2.3", committed_instances=(ref("1.1"),))
        )
        assert record.status is CommitmentStatus.COMMITMENT_INCOMPLETE
        assert not record.is_complete

    def test_same_instance_cannot_be_committed_and_failed(self):
        with pytest.raises(ValueError):
            CommitmentReport(
                "1.2.3",
                committed_instances=(ref("1.1"),),
                failed_instances=(failed("1.1"),),
            )


class TestUnknownTransactions:
    def test_unknown_report_not_matched_by_instance_uid(self):
        archive = HandoverArchive()
        archive.record_transfer("1.2.3", [ref("1.1")])

        result = archive.record_commitment_report(
            CommitmentReport("9.9.9", committed_instances=(ref("1.1"),)),
            reported_at="2026-01-02T10:00:00Z",
        )

        assert result is None
        assert archive["1.2.3"].status is CommitmentStatus.TRANSFERRED
        assert len(archive.unmatched_reports) == 1
        assert archive.unmatched_reports[0].transaction_uid == "9.9.9"
        assert archive.unmatched_reports[0].committed_instances == (ref("1.1"),)

    def test_unknown_request_not_matched_by_instance_uid(self):
        archive = HandoverArchive()
        archive.record_transfer("1.2.3", [ref("1.1")])

        result = archive.record_commitment_request(
            CommitmentRequest("9.9.9", (ref("1.1"),))
        )

        assert result is None
        assert archive["1.2.3"].status is CommitmentStatus.TRANSFERRED
        assert len(archive.unmatched_requests) == 1

    def test_records_for_instance_is_inspection_only(self):
        archive = HandoverArchive()
        archive.record_transfer("1.2.3", [ref("1.1")])
        archive.record_transfer("4.5.6", [ref("1.1")])

        assert len(archive.records_for_instance("1.1")) == 2

    def test_unknown_report_reconciled_by_transaction_uid(self):
        archive = HandoverArchive()
        archive.record_commitment_report(
            CommitmentReport(
                "1.2.3",
                committed_instances=(ref("1.1"),),
                reported_at="2026-01-02T10:00:00Z",
            )
        )
        assert len(archive.unmatched_reports) == 1

        record = archive.record_transfer(
            "1.2.3", [ref("1.1")], transferred_at="2026-01-01T10:00:00Z"
        )

        assert record.status is CommitmentStatus.COMMITTED
        assert record.transferred_at.isoformat() == "2026-01-01T10:00:00+00:00"
        assert len(archive.unmatched_reports) == 0

    def test_reconcile_does_not_borrow_other_transaction(self):
        archive = HandoverArchive()
        archive.record_commitment_report(
            CommitmentReport("9.9.9", committed_instances=(ref("1.1"),))
        )
        archive.record_transfer("1.2.3", [ref("1.1")])

        assert archive["1.2.3"].status is CommitmentStatus.TRANSFERRED
        assert len(archive.unmatched_reports) == 1

    def test_report_for_known_transaction_rejects_foreign_instance(self):
        archive = HandoverArchive()
        archive.record_transfer("1.2.3", [ref("1.1")])
        archive.record_transfer("9.9.9", [ref("2.2")])

        with pytest.raises(CommitmentReportMismatch):
            archive.record_commitment_report(
                CommitmentReport(
                    "1.2.3",
                    committed_instances=(ref("2.2"),),
                )
            )

    def test_request_for_known_transaction_rejects_foreign_instance(self):
        archive = HandoverArchive()
        archive.record_transfer("1.2.3", [ref("1.1")])

        with pytest.raises(CommitmentRequestMismatch):
            archive.record_commitment_request(
                CommitmentRequest("1.2.3", (ref("2.2"),))
            )


class TestLateCommitment:
    def test_late_report_updates_status_only(self):
        archive = HandoverArchive()
        record = archive.record_transfer(
            "1.2.3", [ref("1.1")], transferred_at="2026-01-01T10:00:00Z"
        )
        assert record.status is CommitmentStatus.TRANSFERRED

        updated = archive.record_commitment_report(
            CommitmentReport(
                "1.2.3",
                committed_instances=(ref("1.1"),),
                reported_at="2026-01-05T10:00:00Z",
            )
        )

        assert updated.status is CommitmentStatus.COMMITTED
        assert updated.transferred_at == record.transferred_at
        assert updated.transferred_at.isoformat() == "2026-01-01T10:00:00+00:00"

    def test_later_report_replaces_result(self):
        archive = HandoverArchive()
        archive.record_transfer("1.2.3", [ref("1.1")])
        archive.record_commitment_report(
            CommitmentReport(
                "1.2.3",
                failed_instances=(failed("1.1"),),
                reported_at="2026-01-02T10:00:00Z",
            )
        )
        record = archive.record_commitment_report(
            CommitmentReport(
                "1.2.3",
                committed_instances=(ref("1.1"),),
                reported_at="2026-01-03T10:00:00Z",
            )
        )

        assert record.status is CommitmentStatus.COMMITTED
        assert record.failed_instances == ()
        assert record.commitment.reported_at.isoformat() == (
            "2026-01-03T10:00:00+00:00"
        )
        assert len(record.commitment_history) == 2


class TestParsing:
    def test_parse_request(self):
        ds = Dataset()
        ds.TransactionUID = "1.2.3"
        item = Dataset()
        item.ReferencedSOPClassUID = CT
        item.ReferencedSOPInstanceUID = "1.1"
        ds.ReferencedSOPSequence = Sequence([item])

        request = parse_commitment_request(ds, action_type_id=1)
        assert request.transaction_uid == "1.2.3"
        assert request.instances == (ref("1.1"),)
        assert request.action_type_id == 1

    def test_parse_request_missing_uid(self):
        ds = Dataset()
        ds.ReferencedSOPSequence = Sequence([Dataset()])
        with pytest.raises(InvalidCommitmentDataset):
            parse_commitment_request(ds)

    def test_parse_report(self):
        ds = report_dataset(
            "1.2.3",
            committed=[(CT, "1.1")],
            failed_instances=[(CT, "1.2", 2)],
        )
        parsed = parse_commitment_report(ds, event_type_id=1)

        assert parsed.committed_instances == (ref("1.1"),)
        assert parsed.failed_instances == (failed("1.2", reason=2),)
        assert parsed.event_type_id == 1

    def test_failure_reason_text(self):
        assert failure_reason_text(1) == "Processing failure"
        assert failure_reason_text(2) == "No such object instance"
        assert failure_reason_text(0x00FF).startswith("Unknown failure reason")
        assert failure_reason_text(None) is None


class TestEventAdapters:
    def _event(self, event_type, request, timestamp="2026-01-02T10:00:00Z"):
        class FakeEvent:
            pass

        event = FakeEvent()
        event.request = request
        event.timestamp = _dt(timestamp)
        event.event_type = event_type
        event.action_type = 1
        return event

    def test_report_from_event(self):
        from pynetdicom.handover import commitment_report_from_event

        request = Dataset()
        request.AffectedSOPClassUID = StorageCommitmentPushModel
        event = self._event(1, request)
        event.event_information = report_dataset("1.2.3", committed=[(CT, "1.1")])

        report = commitment_report_from_event(event)
        assert report.transaction_uid == "1.2.3"
        assert report.committed_instances == (ref("1.1"),)

    def test_request_from_event(self):
        from pynetdicom.handover import commitment_request_from_event

        ds = Dataset()
        ds.TransactionUID = "1.2.3"
        item = Dataset()
        item.ReferencedSOPClassUID = CT
        item.ReferencedSOPInstanceUID = "1.1"
        ds.ReferencedSOPSequence = Sequence([item])

        request = Dataset()
        request.RequestedSOPClassUID = StorageCommitmentPushModel
        event = self._event(1, request)
        event.action_information = ds

        parsed = commitment_request_from_event(event)
        assert parsed.transaction_uid == "1.2.3"
        assert parsed.instances == (ref("1.1"),)

    def test_other_service_class_rejected(self):
        from pynetdicom.handover import commitment_report_from_event

        request = Dataset()
        request.AffectedSOPClassUID = "1.2.840.10008.5.1.4.34.6.4"
        event = self._event(1, request)
        event.event_information = Dataset()

        with pytest.raises(ValueError):
            commitment_report_from_event(event)


class TestPersistence:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "handover.json"
        archive = HandoverArchive(path)
        archive.record_transfer(
            "1.2.3",
            [ref("1.1"), ref("1.2")],
            transferred_at="2026-01-01T10:00:00Z",
            source_ae="STORESCU",
            destination_ae="QRSCP",
        )
        archive.record_commitment_report(
            CommitmentReport(
                "1.2.3",
                committed_instances=(ref("1.1"),),
                failed_instances=(failed("1.2", reason=2),),
                reported_at="2026-01-02T10:00:00Z",
            )
        )
        archive.record_commitment_report(
            CommitmentReport("9.9.9", committed_instances=(ref("1.1"),))
        )

        loaded = HandoverArchive(path)
        record = loaded["1.2.3"]

        assert record.status is CommitmentStatus.PARTIALLY_COMMITTED
        assert record.transferred_at.isoformat() == "2026-01-01T10:00:00+00:00"
        assert record.failed_instances == (failed("1.2", reason=2),)
        assert len(loaded.unmatched_reports) == 1

        value = json.loads(path.read_text(encoding="utf-8"))
        assert value["version"] == 1
        assert value["records"][0]["handover_status"] == "partially_committed"

    def test_save_explicit_path(self, tmp_path):
        archive = HandoverArchive()
        archive.record_transfer("1.2.3", [ref("1.1")])
        path = archive.save(tmp_path / "out.json")

        assert path.exists()
        assert HandoverArchive(path)["1.2.3"].instances == (ref("1.1"),)

    def test_clear(self, tmp_path):
        path = tmp_path / "handover.json"
        archive = HandoverArchive(path)
        archive.record_transfer("1.2.3", [ref("1.1")])
        archive.clear()

        assert len(archive) == 0
        assert len(HandoverArchive(path)) == 0
