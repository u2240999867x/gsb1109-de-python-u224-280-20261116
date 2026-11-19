"""Unit tests for the qrscp catalogue reconciliation helpers."""

import tempfile
from pathlib import Path

import pytest

from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

from pynetdicom.apps.qrscp.reconcile import (
    CANDIDATE,
    FAILED,
    GAP,
    MATCHED,
    QUERY_MATCH,
    REQUESTED,
    RETRIEVE_REQUEST,
    VERIFIED,
    VERIFIED_MATERIAL,
    ReconciliationLedger,
    reconcile_instances,
    verify_instance,
)


CT_IMAGE_STORAGE = "1.2.840.10008.5.1.4.1.1.2"
STUDY_UID = "1.2.3.4.5"
SERIES_UID = "1.2.3.4.5.6"
INSTANCE_UID = "1.2.3.4.5.6.7"
SOURCE_A = "AE_A"
SOURCE_B = "AE_B"


def _write_instance(path, sop_instance_uid=INSTANCE_UID, *, omit=None):
    """Write a minimal but complete SOP Instance and return its path."""
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CT_IMAGE_STORAGE
    file_meta.MediaStorageSOPInstanceUID = sop_instance_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = Dataset()
    ds.file_meta = file_meta
    ds.PatientID = "PATIENT1"
    ds.StudyInstanceUID = STUDY_UID
    ds.SeriesInstanceUID = SERIES_UID
    ds.SOPClassUID = CT_IMAGE_STORAGE
    ds.SOPInstanceUID = sop_instance_uid

    for keyword in omit or []:
        delattr(ds, keyword)

    ds.save_as(path, write_like_original=False)
    return path


@pytest.fixture
def payload_path():
    with tempfile.TemporaryDirectory() as tmp:
        yield _write_instance(Path(tmp) / "instance.dcm")


class TestVerifyInstance:
    """Tests for verify_instance()."""

    def test_complete_payload(self, payload_path):
        assert verify_instance(payload_path, INSTANCE_UID)

    def test_missing_file(self):
        assert not verify_instance("/no/such/file.dcm", INSTANCE_UID)

    def test_empty_file(self):
        with tempfile.NamedTemporaryFile() as f:
            assert not verify_instance(f.name, INSTANCE_UID)

    def test_non_dicom_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".dcm") as f:
            f.write("not a dicom file")
            f.flush()
            assert not verify_instance(f.name, INSTANCE_UID)

    def test_uid_mismatch(self, payload_path):
        assert not verify_instance(payload_path, "1.2.3.4.5.6.99")

    def test_missing_required_keyword(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_instance(
                Path(tmp) / "incomplete.dcm", omit=["SeriesInstanceUID"]
            )
            assert not verify_instance(path, INSTANCE_UID)

    def test_expected_uid_optional(self, payload_path):
        assert verify_instance(payload_path)


class TestLedger:
    """Tests for ReconciliationLedger."""

    def test_three_record_streams_are_separate(self, payload_path):
        ledger = ReconciliationLedger()
        ledger.record_query_match(INSTANCE_UID, SOURCE_A)
        ledger.record_retrieve_request(INSTANCE_UID, SOURCE_A)
        assert ledger.record_verified_material(INSTANCE_UID, SOURCE_A, payload_path)

        assert len(ledger.query_matches) == 1
        assert len(ledger.retrieve_requests) == 1
        assert len(ledger.verified_materials) == 1

        assert ledger.query_matches[0].record_type == QUERY_MATCH
        assert ledger.retrieve_requests[0].record_type == RETRIEVE_REQUEST
        assert ledger.verified_materials[0].record_type == VERIFIED_MATERIAL

        assert ledger.state(INSTANCE_UID, SOURCE_A) == VERIFIED
        assert ledger.is_verified(INSTANCE_UID, SOURCE_A)
        assert ledger.verified_uids() == [INSTANCE_UID]

    def test_pending_result_enters_candidates(self):
        ledger = ReconciliationLedger()
        state = ledger.record_query_match(
            INSTANCE_UID, SOURCE_A, pending=True, detail="catalogue pending"
        )

        assert state == CANDIDATE
        assert [entry.sop_instance_uid for entry in ledger.candidates] == [INSTANCE_UID]
        assert ledger.query_matches[0].status == CANDIDATE
        assert ledger.query_matches[0].detail == "catalogue pending"

        # A terminal catalogue row advances the candidate but still doesn't
        # prove the payload is present
        assert ledger.record_query_match(INSTANCE_UID, SOURCE_A) == MATCHED
        assert ledger.verified_materials == []
        assert ledger.state(INSTANCE_UID, SOURCE_A) == MATCHED

    def test_terminal_match_does_not_downgrade_progress(self, payload_path):
        ledger = ReconciliationLedger()
        ledger.record_query_match(INSTANCE_UID, SOURCE_A)
        ledger.record_retrieve_request(INSTANCE_UID, SOURCE_A)
        assert ledger.state(INSTANCE_UID, SOURCE_A) == REQUESTED

        # A duplicate terminal catalogue row must not reset the entry
        ledger.record_query_match(INSTANCE_UID, SOURCE_A)
        assert ledger.state(INSTANCE_UID, SOURCE_A) == REQUESTED

        assert ledger.record_verified_material(INSTANCE_UID, SOURCE_A, payload_path)
        ledger.record_query_match(INSTANCE_UID, SOURCE_A)
        assert ledger.state(INSTANCE_UID, SOURCE_A) == VERIFIED

    def test_incomplete_terminal_state_retains_gap(self):
        ledger = ReconciliationLedger()

        # Pending candidate that is never retrieved
        ledger.record_query_match(INSTANCE_UID, SOURCE_A, pending=True)
        # Matched entry whose retrieval fails
        other_uid = "1.2.3.4.5.6.8"
        ledger.record_query_match(other_uid, SOURCE_A)
        ledger.record_retrieve_request(other_uid, SOURCE_A)
        ledger.record_failure(other_uid, SOURCE_A, "no payload")

        gaps = ledger.finalize()

        assert ledger.state(INSTANCE_UID, SOURCE_A) == GAP
        assert ledger.state(other_uid, SOURCE_A) == FAILED
        gap_uids = {entry.sop_instance_uid for entry in gaps}
        assert gap_uids == {INSTANCE_UID, other_uid}
        assert all(entry.status != VERIFIED for entry in ledger.coverage_gaps)

    def test_same_uid_multiple_sources_tracked_independently(self, payload_path):
        ledger = ReconciliationLedger()

        # The same UID appears in two catalogue sources
        ledger.record_query_match(INSTANCE_UID, SOURCE_A)
        ledger.record_query_match(INSTANCE_UID, SOURCE_B)

        # Only source A delivers and verifies a payload
        ledger.record_retrieve_request(INSTANCE_UID, SOURCE_A)
        assert ledger.record_verified_material(INSTANCE_UID, SOURCE_A, payload_path)

        assert ledger.is_verified(INSTANCE_UID, SOURCE_A)
        assert not ledger.is_verified(INSTANCE_UID, SOURCE_B)
        assert ledger.verified_uids(SOURCE_A) == [INSTANCE_UID]
        assert ledger.verified_uids(SOURCE_B) == []

        # The existing catalogue row for source B must not be treated as a
        # complete payload: it remains a coverage gap after finalisation
        ledger.finalize()
        assert ledger.state(INSTANCE_UID, SOURCE_B) == GAP

    def test_failed_verification_is_not_verified_material(self):
        ledger = ReconciliationLedger()
        ledger.record_query_match(INSTANCE_UID, SOURCE_A)

        assert not ledger.record_verified_material(
            INSTANCE_UID, SOURCE_A, "/missing/payload.dcm"
        )
        assert ledger.verified_materials == []
        assert ledger.state(INSTANCE_UID, SOURCE_A) == FAILED

    def test_unknown_entry_state_raises(self):
        ledger = ReconciliationLedger()
        with pytest.raises(KeyError):
            ledger.state(INSTANCE_UID, SOURCE_A)


class TestReconcileInstances:
    """Tests for the query-then-retrieve workflow."""

    def test_query_precedes_retrieval_and_records_all_streams(self, payload_path):
        ledger = ReconciliationLedger()
        query_results = [
            {"sop_instance_uid": INSTANCE_UID, "source": SOURCE_A},
            {
                "sop_instance_uid": "1.2.3.4.5.6.8",
                "source": SOURCE_B,
                "pending": True,
            },
        ]

        requested = []

        def fetch_instance(sop_instance_uid, source):
            requested.append((sop_instance_uid, source))
            return payload_path if source == SOURCE_A else None

        reconcile_instances(ledger, query_results, fetch_instance)

        # Both catalogue rows were recorded as query matches first
        assert len(ledger.query_matches) == 2
        assert {record.source for record in ledger.query_matches} == {
            SOURCE_A,
            SOURCE_B,
        }
        # One retrieval request per UID/source pair
        assert len(ledger.retrieve_requests) == 2
        assert len(requested) == 2

        # Only source A's payload was verified; source B's failed retrieval
        # is retained as an outstanding coverage gap
        assert ledger.verified_uids() == [INSTANCE_UID]
        assert ledger.is_verified(INSTANCE_UID, SOURCE_A)
        assert ledger.state("1.2.3.4.5.6.8", SOURCE_B) == FAILED
        assert {entry.sop_instance_uid for entry in ledger.coverage_gaps} == {
            "1.2.3.4.5.6.8"
        }

    def test_fetch_exception_recorded_as_failure(self):
        ledger = ReconciliationLedger()
        query_results = [
            {"sop_instance_uid": INSTANCE_UID, "source": SOURCE_A},
        ]

        def fetch_instance(sop_instance_uid, source):
            raise RuntimeError("association aborted")

        reconcile_instances(ledger, query_results, fetch_instance)

        assert ledger.verified_materials == []
        entry = ledger.entries[0]
        assert entry.status == FAILED
        assert entry.detail == "association aborted"

    def test_duplicate_uid_rows_both_require_payloads(self, payload_path):
        ledger = ReconciliationLedger()
        query_results = [
            {"sop_instance_uid": INSTANCE_UID, "source": SOURCE_A},
            {"sop_instance_uid": INSTANCE_UID, "source": SOURCE_B},
        ]

        def fetch_instance(sop_instance_uid, source):
            return payload_path if source == SOURCE_A else None

        reconcile_instances(ledger, query_results, fetch_instance)

        # Two requests despite the shared UID, and only one verified payload
        assert len(ledger.retrieve_requests) == 2
        assert len(ledger.verified_materials) == 1
        assert ledger.state(INSTANCE_UID, SOURCE_B) == FAILED
        assert len(ledger.coverage_gaps) == 1
