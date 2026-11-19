"""Tests for pynetdicom.apps.retrieval_report."""

import json

from pydicom.dataset import Dataset

from pynetdicom.apps.retrieval_report import RetrievalReport


def status(code, **kwargs):
    """Return a C-GET/C-MOVE response status dataset."""
    ds = Dataset()
    ds.Status = code
    for key, value in kwargs.items():
        setattr(ds, key, value)
    return ds


def counts(remaining=None, completed=None, failed=None, warning=None):
    """Return the four sub-operation count keywords."""
    return {
        "NumberOfRemainingSuboperations": remaining,
        "NumberOfCompletedSuboperations": completed,
        "NumberOfFailedSuboperations": failed,
        "NumberOfWarningSuboperations": warning,
    }


class TestRetrievalReport:
    """Tests for RetrievalReport."""

    def test_empty(self):
        """An empty report doesn't claim any coverage."""
        report = RetrievalReport().report()

        assert report["reported"]["response_count"] == 0
        assert report["reported"]["final_received"] is False
        assert report["observed"]["distinct_instances"] == 0
        assert report["coverage"]["identified_completed"] == 0
        assert report["coverage"]["unidentified_total"] is None
        assert report["instances"] == []

    def test_counts_match_receipts(self):
        """Matching counts and receipts produce no difference."""
        report = RetrievalReport()
        report.add_response(
            status(0xFF00, **counts(1, 1, 0, 0)),
        )
        report.add_response(
            status(0x0000, **counts(0, 2, 0, 0)),
            Dataset(),
        )
        report.add_store_receipt("1.1", 0x0000)
        report.add_store_receipt("1.2", 0x0000)

        data = report.report()
        assert data["reported"]["total"] == 2
        assert data["observed"]["distinct_instances"] == 2
        assert data["differences"]["total"] == 0
        assert data["differences"]["completed"] == 0
        assert data["coverage"]["identified_completed"] == 2
        assert data["coverage"]["unidentified_total"] == 0

    def test_duplicate_receipt_counted_once(self):
        """Repeated receipts for one SOP Instance don't increment the total."""
        report = RetrievalReport()
        report.add_response(status(0x0000, **counts(0, 1, 0, 0)), Dataset())

        assert report.add_store_receipt("1.1", 0x0000) is True
        assert report.add_store_receipt("1.1", 0x0000) is False
        assert report.add_store_receipt("1.1", 0x0000) is False

        data = report.report()
        assert data["observed"]["distinct_instances"] == 1
        assert data["observed"]["total_receipts"] == 3
        assert data["observed"]["duplicate_receipts"] == 2
        assert data["differences"]["total"] == 0
        assert len(data["instances"]) == 1
        assert data["instances"][0]["duplicate_receipts"] == 2

    def test_failed_without_uid_list_not_guessed(self):
        """Failed counts without a UID list stay unidentified."""
        report = RetrievalReport()
        report.add_response(status(0x0000, **counts(0, 2, 1, 0)), Dataset())
        report.add_store_receipt("1.1", 0x0000)
        report.add_store_receipt("1.2", 0x0000)

        data = report.report()
        assert data["coverage"]["failed_uid_list_provided"] is False
        assert data["coverage"]["identified_failed"] == 0
        assert data["coverage"]["unidentified_failed"] == 1
        assert data["coverage"]["unidentified_total"] == 1
        assert [row["sop_instance_uid"] for row in data["instances"]] == ["1.1", "1.2"]
        assert all(row["result"] == "Completed" for row in data["instances"])
        assert any(
            "did not provide a Failed SOP Instance UID List" in note
            for note in data["coverage"]["notes"]
        )

    def test_empty_failed_uid_list_not_treated_as_provided(self):
        """An empty failed UID list doesn't identify any instance."""
        report = RetrievalReport()
        identifier = Dataset()
        identifier.FailedSOPInstanceUIDList = ""
        report.add_response(status(0xB000, **counts(0, 2, 1, 0)), identifier)
        report.add_store_receipt("1.1", 0x0000)
        report.add_store_receipt("1.2", 0x0000)

        data = report.report()
        assert data["coverage"]["failed_uid_list_provided"] is False
        assert data["coverage"]["identified_failed"] == 0
        assert data["coverage"]["unidentified_failed"] == 1
        assert any(
            "empty Failed SOP Instance UID List" in note
            for note in data["coverage"]["notes"]
        )

    def test_failed_uid_list_identifies_instances(self):
        """UIDs in the failed list become failed per-instance rows."""
        report = RetrievalReport()
        identifier = Dataset()
        identifier.FailedSOPInstanceUIDList = ["1.2"]
        report.add_response(status(0xB000, **counts(0, 1, 1, 0)), identifier)

        report.add_store_receipt("1.1", 0x0000)

        data = report.report()
        assert data["coverage"]["failed_uid_list_provided"] is True
        assert data["coverage"]["identified_failed"] == 1
        assert data["coverage"]["unidentified_failed"] == 0
        rows = {row["sop_instance_uid"]: row for row in data["instances"]}
        assert rows["1.1"]["result"] == "Completed"
        assert rows["1.2"]["result"] == "Failed"
        assert rows["1.2"]["evidence"] == ["failed-uid-list"]

    def test_failed_uid_list_smaller_than_failed_count(self):
        """A short UID list leaves the remainder unidentified."""
        report = RetrievalReport()
        identifier = Dataset()
        identifier.FailedSOPInstanceUIDList = ["1.2"]
        report.add_response(status(0xB000, **counts(0, 0, 3, 0)), identifier)

        data = report.report()
        assert data["coverage"]["failed_uid_list_count"] == 1
        assert data["coverage"]["identified_failed"] == 1
        assert data["coverage"]["unidentified_failed"] == 2
        assert any("identifies 1 of 3" in note for note in data["coverage"]["notes"])

    def test_local_failure_without_uid_list(self):
        """A local C-STORE failure is named even without a UID list."""
        report = RetrievalReport()
        report.add_response(status(0xB000, **counts(0, 1, 1, 0)), Dataset())
        report.add_store_receipt("1.1", 0x0000)
        report.add_store_receipt("1.2", 0xA700)

        data = report.report()
        # The failed instance is identified by its local receipt, not by a
        #   failed UID list, so the peer's failed count is fully covered even
        #   though the list itself is absent.
        assert data["coverage"]["identified_failed"] == 1
        assert data["coverage"]["unidentified_failed"] == 0
        assert data["coverage"]["failed_uid_list_provided"] is False
        rows = {row["sop_instance_uid"]: row for row in data["instances"]}
        assert rows["1.2"]["result"] == "Failed"
        assert rows["1.2"]["evidence"] == ["store-receipt", "store-failure"]

    def test_conflicting_evidence(self):
        """A local success and a peer failed UID produce a conflict."""
        report = RetrievalReport()
        identifier = Dataset()
        identifier.FailedSOPInstanceUIDList = "1.2"
        report.add_response(status(0xB000, **counts(0, 1, 1, 0)), identifier)
        report.add_store_receipt("1.1", 0x0000)
        report.add_store_receipt("1.2", 0x0000)

        data = report.report()
        rows = {row["sop_instance_uid"]: row for row in data["instances"]}
        assert rows["1.2"]["result"] == "Conflict"
        assert data["coverage"]["identified_conflicts"] == 1
        assert data["coverage"]["identified_failed"] == 1
        assert any("conflicting evidence" in note for note in data["coverage"]["notes"])

    def test_conflicting_receipts(self):
        """Multiple receipts with different results are a conflict."""
        report = RetrievalReport()
        report.add_response(status(0x0000, **counts(0, 1, 0, 0)), Dataset())
        report.add_store_receipt("1.1", 0x0000)
        report.add_store_receipt("1.1", 0xA700)

        data = report.report()
        assert data["observed"]["conflict"] == 1
        assert data["observed"]["completed"] == 0
        assert data["instances"][0]["result"] == "Conflict"
        assert data["differences"]["completed"] == 1

    def test_warning_receipts(self):
        """Warning C-STORE receipts are counted separately."""
        report = RetrievalReport()
        report.add_response(status(0xB000, **counts(0, 0, 0, 1)), Dataset())
        report.add_store_receipt("1.1", 0xB000)

        data = report.report()
        assert data["observed"]["warning"] == 1
        assert data["coverage"]["identified_warning"] == 1
        assert data["coverage"]["unidentified_total"] == 0
        assert data["instances"][0]["result"] == "Warning"

    def test_unidentified_receipt(self):
        """Receipts without a SOP Instance UID can't be de-duplicated."""
        report = RetrievalReport()
        assert report.add_store_receipt(None, 0x0000) is False
        assert report.add_store_receipt("", 0x0000) is False

        data = report.report()
        assert data["observed"]["distinct_instances"] == 0
        assert data["observed"]["unidentified_receipts"] == 2
        assert any(
            "had no SOP Instance UID" in note for note in data["coverage"]["notes"]
        )

    def test_pending_only(self):
        """A missing final response is reported as incomplete."""
        report = RetrievalReport()
        report.add_response(status(0xFF00, **counts(3, 2, 0, 0)))

        data = report.report()
        assert data["reported"]["final_received"] is False
        assert data["reported"]["total"] == 5
        assert any("No final" in note for note in data["coverage"]["notes"])

    def test_missing_final_counts_not_backfilled(self):
        """Counts missing from the final response aren't taken from Pending."""
        report = RetrievalReport()
        report.add_response(status(0xFF00, **counts(3, 2, 0, 0)))
        report.add_response(status(0x0000, NumberOfCompletedSuboperations=2), Dataset())

        data = report.report()
        assert data["reported"]["remaining"] == 0
        assert data["reported"]["completed"] == 2
        assert data["reported"]["failed"] is None
        assert data["reported"]["total"] is None

    def test_empty_response_is_not_final(self):
        """An empty status (timeout/abort) isn't a final response."""
        report = RetrievalReport()
        report.add_response(Dataset(), None)

        data = report.report()
        assert data["reported"]["final_received"] is False
        assert data["reported"]["status"] is None
        assert data["reported"]["total"] is None

    def test_more_local_failures_than_reported(self):
        """Local failures above the reported failed count are called out."""
        report = RetrievalReport()
        report.add_response(status(0x0000, **counts(0, 2, 0, 0)), Dataset())
        report.add_store_receipt("1.1", 0x0000)
        report.add_store_receipt("1.2", 0xA700)

        data = report.report()
        assert data["coverage"]["unidentified_failed"] == 0
        assert any(
            "more locally observed C-STORE failure" in note
            for note in data["coverage"]["notes"]
        )

    def test_more_receipts_than_reported(self):
        """More distinct receipts than the reported total is called out."""
        report = RetrievalReport()
        report.add_response(status(0x0000, **counts(0, 1, 0, 0)), Dataset())
        report.add_store_receipt("1.1", 0x0000)
        report.add_store_receipt("1.2", 0x0000)

        data = report.report()
        assert data["differences"]["total"] == -1
        assert any(
            "more distinct C-STORE" in note for note in data["coverage"]["notes"]
        )

    def test_report_is_not_mutated(self):
        """Calling report() twice doesn't change the result."""
        report = RetrievalReport()
        report.add_response(status(0x0000, **counts(0, 1, 0, 0)), Dataset())
        report.add_store_receipt("1.1", 0x0000)

        assert report.report() == report.report()

    def test_write(self, tmp_path):
        """write() produces the same JSON-serialisable data."""
        report = RetrievalReport()
        report.add_response(status(0x0000, **counts(0, 1, 0, 0)), Dataset())
        report.add_store_receipt("1.1", 0x0000)

        path = tmp_path / "report.json"
        report.write(str(path))
        with open(path, encoding="utf-8") as fp:
            assert json.load(fp) == report.report()

    def test_summary(self):
        """summary() includes the reported, observed and coverage totals."""
        report = RetrievalReport()
        report.add_response(status(0x0000, **counts(0, 1, 1, 0)), Dataset())
        report.add_store_receipt("1.1", 0x0000)

        summary = report.summary()
        assert "Reported" in summary
        assert "Observed" in summary
        assert "Difference" in summary
        assert "Coverage" in summary
        assert "Failed UID list: not provided" in summary
        assert "1.1: Completed" in summary
