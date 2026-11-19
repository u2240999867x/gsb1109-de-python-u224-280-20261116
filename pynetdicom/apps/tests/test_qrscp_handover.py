"""Tests for the qrscp handover integration."""

import pytest
from pydicom.dataset import Dataset

try:
    import sqlalchemy  # noqa: F401

    HAVE_SQLALCHEMY = True
except ImportError:
    HAVE_SQLALCHEMY = False

from pynetdicom.handover import CommitmentStatus
from pynetdicom.apps.qrscp.handover import HandoverManager
from pynetdicom.sop_class import StorageCommitmentPushModel

CT = "1.2.840.10008.5.1.4.1.1.2"


class FakeAE:
    ae_title = "QRSCP"


class FakeRequestor:
    ae_title = "STORESCU"
    address = "127.0.0.1"
    port = 11112


class FakeAssoc:
    def __init__(self):
        self.ae = FakeAE()
        self.requestor = FakeRequestor()


class FakeEvent:
    def __init__(self, assoc, timestamp):
        self.assoc = assoc
        self.timestamp = timestamp
        self.event_type = 1
        self.action_type = 1


def ref(instance_uid):
    ds = Dataset()
    ds.SOPClassUID = CT
    ds.SOPInstanceUID = instance_uid
    return ds


def request_dataset(transaction_uid, instance_uids):
    ds = Dataset()
    ds.TransactionUID = transaction_uid
    ds.ReferencedSOPSequence = []
    for instance_uid in instance_uids:
        item = Dataset()
        item.ReferencedSOPClassUID = CT
        item.ReferencedSOPInstanceUID = instance_uid
        ds.ReferencedSOPSequence.append(item)

    return ds


def report_dataset(transaction_uid, committed=(), failed=()):
    ds = Dataset()
    ds.TransactionUID = transaction_uid
    if committed:
        ds.ReferencedSOPSequence = []
        for instance_uid in committed:
            item = Dataset()
            item.ReferencedSOPClassUID = CT
            item.ReferencedSOPInstanceUID = instance_uid
            ds.ReferencedSOPSequence.append(item)

    if failed:
        ds.FailedSOPSequence = []
        for instance_uid, reason in failed:
            item = Dataset()
            item.ReferencedSOPClassUID = CT
            item.ReferencedSOPInstanceUID = instance_uid
            item.FailureReason = reason
            ds.FailedSOPSequence.append(item)

    return ds


def n_action_event(assoc, dataset, timestamp):
    event = FakeEvent(assoc, timestamp)
    request = Dataset()
    request.RequestedSOPClassUID = StorageCommitmentPushModel
    event.request = request
    event.action_information = dataset
    return event


def n_event_report_event(assoc, dataset, timestamp):
    event = FakeEvent(assoc, timestamp)
    request = Dataset()
    request.AffectedSOPClassUID = StorageCommitmentPushModel
    event.request = request
    event.event_information = dataset
    return event


class TestHandoverManager:
    def test_store_then_commit(self, tmp_path):
        manager = HandoverManager(tmp_path / "handover.json")
        assoc = FakeAssoc()

        manager.note_store(assoc, ref("1.1"), "2026-01-01T10:00:00Z")
        manager.note_store(assoc, ref("1.2"), "2026-01-01T10:01:00Z")

        ds = request_dataset("1.2.3", ["1.1", "1.2"])
        manager.record_request(n_action_event(assoc, ds, "2026-01-02T10:00:00Z"))

        record = manager.archive["1.2.3"]
        assert record.transferred_at.isoformat() == "2026-01-01T10:01:00+00:00"
        assert record.status is CommitmentStatus.COMMITMENT_REQUESTED

        manager.record_report(
            n_event_report_event(
                assoc,
                report_dataset("1.2.3", committed=["1.1"], failed=[("1.2", 2)]),
                "2026-01-03T10:00:00Z",
            )
        )
        record = manager.archive["1.2.3"]
        assert record.status is CommitmentStatus.PARTIALLY_COMMITTED
        assert record.transferred_at.isoformat() == "2026-01-01T10:01:00+00:00"

    def test_unknown_report_kept_separate(self, tmp_path):
        manager = HandoverManager(tmp_path / "handover.json")
        assoc = FakeAssoc()
        manager.note_store(assoc, ref("1.1"), "2026-01-01T10:00:00Z")
        manager.record_request(
            n_action_event(
                assoc,
                request_dataset("1.2.3", ["1.1"]),
                "2026-01-02T10:00:00Z",
            )
        )

        assert manager.record_report(
            n_event_report_event(
                assoc,
                report_dataset("9.9.9", committed=["1.1"]),
                "2026-01-03T10:00:00Z",
            )
        ) is None

        assert len(manager.archive.unmatched_reports) == 1
        assert manager.status("1.2.3") is CommitmentStatus.COMMITMENT_REQUESTED

    def test_commitment_after_reconnect_uses_store_time(self, tmp_path):
        manager = HandoverManager(tmp_path / "handover.json")
        first = FakeAssoc()
        manager.note_store(first, ref("1.1"), "2026-01-01T10:00:00Z")
        manager.release(first)

        second = FakeAssoc()
        manager.record_request(
            n_action_event(
                second,
                request_dataset("1.2.3", ["1.1"]),
                "2026-01-05T10:00:00Z",
            )
        )

        assert manager.archive["1.2.3"].transferred_at.isoformat() == (
            "2026-01-01T10:00:00+00:00"
        )


class TestHandlers:
    def _logger(self):
        import logging

        return logging.getLogger("test_handover")

    @pytest.mark.skipif(not HAVE_SQLALCHEMY, reason="Requires sqlalchemy")
    def test_n_action_records_request(self, tmp_path):
        import logging
        from pynetdicom.apps.qrscp.handlers import handle_n_action

        manager = HandoverManager(tmp_path / "handover.json")
        assoc = FakeAssoc()
        manager.note_store(assoc, ref("1.1"), "2026-01-01T10:00:00Z")

        status, reply = handle_n_action(
            n_action_event(
                assoc,
                request_dataset("1.2.3", ["1.1"]),
                "2026-01-02T10:00:00Z",
            ),
            manager,
            self._logger(),
        )

        assert status == 0x0000
        assert reply.TransactionUID == "1.2.3"
        assert manager.status("1.2.3") is CommitmentStatus.COMMITMENT_REQUESTED

    @pytest.mark.skipif(not HAVE_SQLALCHEMY, reason="Requires sqlalchemy")
    def test_n_event_report_updates_status(self, tmp_path):
        from pynetdicom.apps.qrscp.handlers import (
            handle_n_action,
            handle_n_event_report,
        )

        manager = HandoverManager(tmp_path / "handover.json")
        assoc = FakeAssoc()
        manager.note_store(assoc, ref("1.1"), "2026-01-01T10:00:00Z")
        manager.note_store(assoc, ref("1.2"), "2026-01-01T10:01:00Z")

        handle_n_action(
            n_action_event(
                assoc,
                request_dataset("1.2.3", ["1.1", "1.2"]),
                "2026-01-02T10:00:00Z",
            ),
            manager,
            self._logger(),
        )

        status, _ = handle_n_event_report(
            n_event_report_event(
                assoc,
                report_dataset("1.2.3", committed=["1.1"], failed=[("1.2", 2)]),
                "2026-01-03T10:00:00Z",
            ),
            manager,
            self._logger(),
        )

        assert status == 0x0000
        record = manager.archive["1.2.3"]
        assert record.status is CommitmentStatus.PARTIALLY_COMMITTED
        assert record.transferred_at.isoformat() == "2026-01-01T10:01:00+00:00"
        assert record.failed_instances[0].failure_reason == 2

    @pytest.mark.skipif(not HAVE_SQLALCHEMY, reason="Requires sqlalchemy")
    def test_non_storage_commitment_ignored(self, tmp_path):
        from pynetdicom.apps.qrscp.handlers import handle_n_event_report

        manager = HandoverManager(tmp_path / "handover.json")
        event = n_event_report_event(
            FakeAssoc(), Dataset(), "2026-01-03T10:00:00Z"
        )
        event.request.AffectedSOPClassUID = "1.2.840.10008.5.1.4.34.6.4"

        status, _ = handle_n_event_report(event, manager, self._logger())
        assert status == 0x0113
