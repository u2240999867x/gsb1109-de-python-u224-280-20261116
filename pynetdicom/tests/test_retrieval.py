"""Tests for the C-GET/C-MOVE retrieval evidence ledger."""

import threading

import pytest

from pynetdicom.dimse_primitives import C_CANCEL, C_GET, C_MOVE, C_STORE
from pynetdicom.retrieval import (
    RetrievalLedger,
    RetrievalStore,
    RetrievalSubOperation,
    RetrievalTask,
)

SOP_CLASS = "1.2.840.10008.5.1.4.1.1.2"
INSTANCE_UID = "1.2.3.4.5"
OTHER_UID = "1.2.3.4.6"


def _c_get(message_id=1, sop_class=SOP_CLASS):
    req = C_GET()
    req.MessageID = message_id
    req.AffectedSOPClassUID = sop_class
    return req


def _c_move(message_id=1, sop_class=SOP_CLASS):
    req = C_MOVE()
    req.MessageID = message_id
    req.AffectedSOPClassUID = sop_class
    return req


def _c_store(message_id, instance_uid, *, command_id=None, originator=None):
    req = C_STORE()
    req.MessageID = message_id
    req.AffectedSOPClassUID = SOP_CLASS
    req.AffectedSOPInstanceUID = instance_uid
    req.MoveOriginatorMessageID = command_id
    req.MoveOriginatorApplicationEntityTitle = originator
    return req


class TestTaskLifecycle:
    """The request, expected total, sub-operations and terminal state."""

    def test_begin_records_request(self):
        ledger = RetrievalLedger()
        task = ledger.begin("C-GET", 1, expected_total=2)

        assert isinstance(task, RetrievalTask)
        assert task.service == "C-GET"
        assert task.message_id == 1
        assert task.expected_total == 2
        assert task.terminal_state == "pending"
        assert not task.is_terminal
        assert ledger.tasks == (task,)

    def test_begin_from_c_get_request(self):
        ledger = RetrievalLedger()
        task = ledger.begin_from_request(_c_get(7), expected_total=1)
        assert task.service == "C-GET"
        assert task.message_id == 7
        assert task.sop_class_uid == SOP_CLASS

    def test_begin_from_c_move_request(self):
        ledger = RetrievalLedger()
        task = ledger.begin_from_request(_c_move(9))
        assert task.service == "C-MOVE"
        assert task.message_id == 9
        assert task.expected_total is None

    def test_begin_rejects_duplicate_active_message_id(self):
        ledger = RetrievalLedger()
        ledger.begin("C-GET", 1)
        with pytest.raises(ValueError):
            ledger.begin("C-GET", 1)

    def test_message_id_reusable_after_terminal(self):
        ledger = RetrievalLedger()
        first = ledger.begin("C-GET", 1, expected_total=0)
        ledger.close(first.task_id)
        second = ledger.begin("C-GET", 1)
        assert second.task_id != first.task_id
        assert len(ledger.tasks) == 2

    def test_invalid_service_and_total(self):
        ledger = RetrievalLedger()
        with pytest.raises(ValueError):
            ledger.begin("C-FIND", 1)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            ledger.begin("C-GET", 1, expected_total=-1)

    def test_set_expected_total(self):
        ledger = RetrievalLedger()
        task = ledger.begin("C-GET", 1)
        ledger.set_expected_total(task.task_id, 3)
        assert ledger.get_task(task.task_id).expected_total == 3

    def test_set_expected_total_below_recorded_subops_rejected(self):
        ledger = RetrievalLedger()
        task = ledger.begin("C-GET", 1, expected_total=2)
        ledger.record_sub_operation(task.task_id, 2, SOP_CLASS, INSTANCE_UID)
        with pytest.raises(ValueError):
            ledger.set_expected_total(task.task_id, 0)

    def test_unknown_task_raises(self):
        ledger = RetrievalLedger()
        with pytest.raises(KeyError):
            ledger.get_task(99)
        with pytest.raises(KeyError):
            ledger.set_expected_total(99, 1)


class TestSubOperations:
    def test_record_and_result(self):
        ledger = RetrievalLedger()
        task = ledger.begin("C-GET", 1, expected_total=1)
        subop = ledger.record_sub_operation(
            task.task_id, 2, SOP_CLASS, INSTANCE_UID
        )
        assert isinstance(subop, RetrievalSubOperation)
        assert subop.command_id == 1
        assert subop.status_code is None

        ledger.record_sub_operation_result(task.task_id, 2, 0x0000)
        assert subop.is_success
        assert task.n_completed == 1
        assert task.n_remaining == 0

    def test_duplicate_sub_operation_rejected(self):
        ledger = RetrievalLedger()
        task = ledger.begin("C-GET", 1, expected_total=2)
        ledger.record_sub_operation(task.task_id, 2, SOP_CLASS, INSTANCE_UID)
        with pytest.raises(ValueError):
            ledger.record_sub_operation(task.task_id, 2, SOP_CLASS, OTHER_UID)

    def test_unknown_sub_operation_result_rejected(self):
        ledger = RetrievalLedger()
        task = ledger.begin("C-GET", 1)
        with pytest.raises(KeyError):
            ledger.record_sub_operation_result(task.task_id, 5, 0x0000)

    def test_failure_and_warning_counters(self):
        ledger = RetrievalLedger()
        task = ledger.begin("C-GET", 1, expected_total=3)
        ledger.record_sub_operation(task.task_id, 2, SOP_CLASS, "uid-a")
        ledger.record_sub_operation(task.task_id, 3, SOP_CLASS, "uid-b")
        ledger.record_sub_operation(task.task_id, 4, SOP_CLASS, "uid-c")
        ledger.record_sub_operation_result(task.task_id, 2, 0x0000)
        ledger.record_sub_operation_result(task.task_id, 3, 0xB000)
        ledger.record_sub_operation_result(task.task_id, 4, 0xA700)

        assert task.n_completed == 1
        assert task.n_warning == 1
        assert task.n_failed == 1
        assert task.n_remaining == 0
        assert task.failed_instance_uids == ["uid-c"]

    def test_transport_failure_is_counted(self):
        ledger = RetrievalLedger()
        task = ledger.begin("C-GET", 1, expected_total=1)
        ledger.record_sub_operation(task.task_id, 2, SOP_CLASS, INSTANCE_UID)
        ledger.record_sub_operation_result(task.task_id, 2, None)
        assert task.n_completed == 0
        assert task.n_remaining == 1


class TestAttribution:
    """Only command or association evidence may uniquely attribute a store."""

    def test_unique_attribution_via_sub_operation(self):
        ledger = RetrievalLedger()
        task = ledger.begin("C-GET", 1, expected_total=1)
        ledger.record_sub_operation(task.task_id, 2, SOP_CLASS, INSTANCE_UID)

        store = ledger.record_store(2, SOP_CLASS, INSTANCE_UID, status_code=0x0000)
        assert store.attribution == "unique"
        assert store.task_ids == (task.task_id,)
        assert task.n_completed == 1

    def test_same_uid_alone_is_not_unique(self):
        ledger = RetrievalLedger()
        first = ledger.begin("C-GET", 1, expected_total=1)
        second = ledger.begin("C-GET", 3, expected_total=1)
        ledger.record_sub_operation(first.task_id, 2, SOP_CLASS, INSTANCE_UID)
        ledger.record_sub_operation(second.task_id, 4, SOP_CLASS, INSTANCE_UID)

        # A store with an unrecognised message ID but matching UID
        store = ledger.record_store(99, SOP_CLASS, INSTANCE_UID, status_code=0x0000)
        assert store.attribution == "shared"
        assert set(store.task_ids) == {first.task_id, second.task_id}

    def test_strong_evidence_beats_same_uid_weak_evidence(self):
        ledger = RetrievalLedger()
        first = ledger.begin("C-GET", 1, expected_total=1)
        second = ledger.begin("C-GET", 3, expected_total=1)
        ledger.record_sub_operation(first.task_id, 2, SOP_CLASS, INSTANCE_UID)
        ledger.record_sub_operation(second.task_id, 4, SOP_CLASS, INSTANCE_UID)

        # The message ID uniquely identifies second's sub-operation; the fact
        #   that first also requested the same UID must not dilute this
        store = ledger.record_store(4, SOP_CLASS, INSTANCE_UID, status_code=0x0000)
        assert store.attribution == "unique"
        assert store.task_ids == (second.task_id,)
        assert first.n_completed == 0
        assert second.n_completed == 1

    def test_unmatched_store_is_unattributed(self):
        ledger = RetrievalLedger()
        ledger.begin("C-GET", 1, expected_total=1)
        store = ledger.record_store(50, SOP_CLASS, "unknown.uid", status_code=0x0000)
        assert store.attribution == "unattributed"
        assert store.task_ids == ()
        assert not store.is_attributed
        assert ledger.unattributed_stores() == (store,)

    def test_uid_mismatch_on_message_id_not_attributed(self):
        ledger = RetrievalLedger()
        task = ledger.begin("C-GET", 1, expected_total=1)
        ledger.record_sub_operation(task.task_id, 2, SOP_CLASS, INSTANCE_UID)
        # Same message ID but a different instance UID: evidence does not match
        store = ledger.record_store(2, SOP_CLASS, OTHER_UID, status_code=0x0000)
        assert store.attribution == "unattributed"
        assert task.n_completed == 0

    def test_late_sub_operation_resolves_shared_store(self):
        ledger = RetrievalLedger()
        task = ledger.begin("C-GET", 1, expected_total=1)
        # Store arrives before the task has recorded its sub-operation
        store = ledger.record_store(5, SOP_CLASS, INSTANCE_UID, status_code=0x0000)
        assert store.attribution == "unattributed"

        ledger.record_sub_operation(task.task_id, 5, SOP_CLASS, INSTANCE_UID)
        assert store.attribution == "unique"
        assert store.task_ids == (task.task_id,)
        assert task.n_completed == 1

    def test_shared_material_never_double_completed(self):
        ledger = RetrievalLedger()
        first = ledger.begin("C-GET", 1, expected_total=1)
        second = ledger.begin("C-GET", 3, expected_total=1)
        ledger.record_sub_operation(first.task_id, 2, SOP_CLASS, INSTANCE_UID)
        ledger.record_sub_operation(second.task_id, 4, SOP_CLASS, INSTANCE_UID)

        shared = ledger.record_store(
            99, SOP_CLASS, INSTANCE_UID, status_code=0x0000
        )
        assert shared.attribution == "shared"
        # Neither side gets a free completion from the unlinked material
        assert first.n_completed == 0
        assert second.n_completed == 0
        assert ledger.ambiguous_stores() == (shared,)

    def test_move_originator_command_evidence(self):
        ledger = RetrievalLedger()
        task = ledger.begin(
            "C-MOVE", 7, expected_total=1, originator_ae_title="QR_SCP"
        )
        store = ledger.record_store(
            8,
            SOP_CLASS,
            INSTANCE_UID,
            status_code=0x0000,
            command_id=7,
            originator_ae_title="QR_SCP",
        )
        assert store.attribution == "unique"
        assert store.task_ids == (task.task_id,)

    def test_move_originator_wrong_ae_not_attributed(self):
        ledger = RetrievalLedger()
        ledger.begin("C-MOVE", 7, expected_total=1, originator_ae_title="QR_SCP")
        store = ledger.record_store(
            8,
            SOP_CLASS,
            INSTANCE_UID,
            status_code=0x0000,
            command_id=7,
            originator_ae_title="SOMEONE_ELSE",
        )
        assert store.attribution == "unattributed"

    def test_record_store_from_c_store_request(self):
        ledger = RetrievalLedger()
        task = ledger.begin(
            "C-MOVE", 7, expected_total=1, originator_ae_title="QR_SCP"
        )
        request = _c_store(8, INSTANCE_UID, command_id=7, originator="QR_SCP")
        store = ledger.record_store_from_request(request, status_code=0x0000)
        assert isinstance(store, RetrievalStore)
        assert store.attribution == "unique"
        assert store.task_ids == (task.task_id,)
        assert store.command_id == 7

    def test_record_store_from_request_missing_uid_raises(self):
        ledger = RetrievalLedger()
        request = C_STORE()
        request.MessageID = 8
        request.AffectedSOPClassUID = SOP_CLASS
        # AffectedSOPInstanceUID left unset
        with pytest.raises(ValueError):
            ledger.record_store_from_request(request, status_code=0x0000)
        # The rejected store leaves no evidence behind
        assert ledger.stores == ()


class TestCancellation:
    def test_cancel_preserves_verified_instances(self):
        ledger = RetrievalLedger()
        task = ledger.begin("C-GET", 1, expected_total=2)
        ledger.record_sub_operation(task.task_id, 2, SOP_CLASS, INSTANCE_UID)
        ledger.record_sub_operation_result(task.task_id, 2, 0x0000)
        ledger.confirm_cancel(task.task_id)

        assert task.is_cancelled
        assert task.terminal_state == "cancel"
        # The already verified completion is not erased
        assert task.n_completed == 1
        assert task.verified_instance_uids == (INSTANCE_UID,)
        assert task.n_remaining == 1

    def test_cancel_does_not_affect_other_task(self):
        ledger = RetrievalLedger()
        cancelled = ledger.begin("C-GET", 1, expected_total=1)
        other = ledger.begin("C-GET", 3, expected_total=1)
        ledger.confirm_cancel(cancelled.task_id)

        assert not other.is_cancelled
        assert other.terminal_state == "pending"

    def test_cancel_with_primitive(self):
        ledger = RetrievalLedger()
        task = ledger.begin("C-GET", 1, expected_total=1)
        cancel = C_CANCEL()
        cancel.MessageIDBeingRespondedTo = 1
        ledger.confirm_cancel(task.task_id, cancel)
        assert task.is_cancelled

    def test_cancel_primitive_mismatch_rejected(self):
        ledger = RetrievalLedger()
        task = ledger.begin("C-GET", 1, expected_total=1)
        cancel = C_CANCEL()
        cancel.MessageIDBeingRespondedTo = 99
        with pytest.raises(ValueError):
            ledger.confirm_cancel(task.task_id, cancel)
        assert not task.is_cancelled

    def test_late_store_after_cancel_kept_but_not_backfilled(self):
        ledger = RetrievalLedger()
        task = ledger.begin("C-GET", 1, expected_total=1)
        ledger.record_sub_operation(task.task_id, 2, SOP_CLASS, INSTANCE_UID)
        ledger.confirm_cancel(task.task_id)

        # A late C-STORE for the cancelled task's own sub-operation
        store = ledger.record_store(2, SOP_CLASS, INSTANCE_UID, status_code=0x0000)
        assert store.attribution == "unique"
        # The cancelled task is terminal so its coverage stays frozen
        assert task.n_completed == 0
        assert task.terminal_state == "cancel"


class TestIsolation:
    def test_new_task_same_message_id_is_isolated(self):
        ledger = RetrievalLedger()
        old = ledger.begin("C-GET", 1, expected_total=1)
        ledger.confirm_cancel(old.task_id)
        new = ledger.begin("C-GET", 1, expected_total=1)
        assert new.task_id != old.task_id

        # A store whose message ID matches the new task's sub-operation is
        #   attributed to the new task only, never to the old one
        ledger.record_sub_operation(new.task_id, 2, SOP_CLASS, INSTANCE_UID)
        store = ledger.record_store(2, SOP_CLASS, INSTANCE_UID, status_code=0x0000)
        assert store.attribution == "unique"
        assert store.task_ids == (new.task_id,)
        assert old.n_completed == 0
        assert new.n_completed == 1


class TestQueries:
    def test_missing_counts_when_total_undeclared(self):
        ledger = RetrievalLedger()
        task = ledger.begin("C-GET", 1)
        ledger.record_sub_operation(task.task_id, 2, SOP_CLASS, INSTANCE_UID)
        ledger.record_sub_operation_result(task.task_id, 2, 0x0000)
        # No expected total: completion coverage cannot be confirmed
        assert task.has_missing_count
        assert ledger.missing_counts() == (task,)

    def test_missing_counts_when_suboperation_outstanding(self):
        ledger = RetrievalLedger()
        task = ledger.begin("C-GET", 1, expected_total=2)
        ledger.record_sub_operation(task.task_id, 2, SOP_CLASS, INSTANCE_UID)
        ledger.record_sub_operation_result(task.task_id, 2, 0x0000)
        assert [t.task_id for t in ledger.missing_counts()] == [task.task_id]

    def test_failed_stores_query(self):
        ledger = RetrievalLedger()
        ledger.record_store(1, SOP_CLASS, "a", status_code=0x0000)
        failed = ledger.record_store(2, SOP_CLASS, "b", status_code=0xA700)
        ledger.record_store(3, SOP_CLASS, "c", status_code=None)
        # A store with no status at all is not treated as a verified failure
        assert [s.message_id for s in ledger.failed_stores()] == [failed.message_id]

    def test_unattributed_and_ambiguous_queries_are_distinct(self):
        ledger = RetrievalLedger()
        first = ledger.begin("C-GET", 1, expected_total=1)
        second = ledger.begin("C-GET", 3, expected_total=1)
        ledger.record_sub_operation(first.task_id, 2, SOP_CLASS, INSTANCE_UID)
        ledger.record_sub_operation(second.task_id, 4, SOP_CLASS, INSTANCE_UID)

        ledger.record_store(99, SOP_CLASS, INSTANCE_UID, status_code=0x0000)
        ledger.record_store(50, SOP_CLASS, "unknown.uid", status_code=0x0000)

        assert len(ledger.ambiguous_stores()) == 1
        assert len(ledger.unattributed_stores()) == 1
        assert len(ledger.unresolvable_stores()) == 2

    def test_instance_verification_avoids_refetch(self):
        ledger = RetrievalLedger()
        ledger.record_store(1, SOP_CLASS, INSTANCE_UID, status_code=0x0000)
        verified = ledger.instance_verification(INSTANCE_UID)
        assert verified is not None
        assert verified.sop_instance_uid == INSTANCE_UID

    def test_failed_instance_not_verifiable(self):
        ledger = RetrievalLedger()
        ledger.record_store(1, SOP_CLASS, INSTANCE_UID, status_code=0xA700)
        assert ledger.instance_verification(INSTANCE_UID) is None

    def test_warning_instance_is_verifiable(self):
        ledger = RetrievalLedger()
        ledger.record_store(1, SOP_CLASS, INSTANCE_UID, status_code=0xB000)
        assert ledger.instance_verification(INSTANCE_UID) is not None


class TestTerminalState:
    def test_derived_success(self):
        ledger = RetrievalLedger()
        task = ledger.begin("C-GET", 1, expected_total=1)
        ledger.record_sub_operation(task.task_id, 2, SOP_CLASS, INSTANCE_UID)
        ledger.record_sub_operation_result(task.task_id, 2, 0x0000)
        closed = ledger.close(task.task_id)
        assert closed.terminal_state == "success"

    def test_derived_failure_all_failed(self):
        ledger = RetrievalLedger()
        task = ledger.begin("C-GET", 1, expected_total=1)
        ledger.record_sub_operation(task.task_id, 2, SOP_CLASS, INSTANCE_UID)
        ledger.record_sub_operation_result(task.task_id, 2, 0xA700)
        assert ledger.close(task.task_id).terminal_state == "failure"

    def test_derived_warning_mixed(self):
        ledger = RetrievalLedger()
        task = ledger.begin("C-GET", 1, expected_total=2)
        ledger.record_sub_operation(task.task_id, 2, SOP_CLASS, "a")
        ledger.record_sub_operation(task.task_id, 3, SOP_CLASS, "b")
        ledger.record_sub_operation_result(task.task_id, 2, 0x0000)
        ledger.record_sub_operation_result(task.task_id, 3, 0xA700)
        assert ledger.close(task.task_id).terminal_state == "warning"

    def test_unknown_when_coverage_incomplete(self):
        ledger = RetrievalLedger()
        task = ledger.begin("C-GET", 1, expected_total=2)
        ledger.record_sub_operation(task.task_id, 2, SOP_CLASS, "a")
        ledger.record_sub_operation_result(task.task_id, 2, 0x0000)
        assert ledger.close(task.task_id).terminal_state == "unknown"

    def test_explicit_terminal_state(self):
        ledger = RetrievalLedger()
        task = ledger.begin("C-GET", 1)
        with pytest.raises(ValueError):
            ledger.close(task.task_id, "bogus")  # type: ignore[arg-type]
        ledger.close(task.task_id, "aborted")
        assert task.terminal_state == "aborted"
        assert task.is_terminal


class TestThreadSafety:
    def test_concurrent_tasks_and_stores(self):
        ledger = RetrievalLedger()
        errors = []

        def worker(task_index):
            try:
                task = ledger.begin("C-GET", 100 + task_index, expected_total=5)
                for sub in range(5):
                    msg_id = 200 + task_index * 10 + sub
                    uid = f"uid.{task_index}.{sub}"
                    ledger.record_sub_operation(
                        task.task_id, msg_id, SOP_CLASS, uid
                    )
                    ledger.record_sub_operation_result(
                        task.task_id, msg_id, 0x0000
                    )
                    ledger.record_store(msg_id, SOP_CLASS, uid, status_code=0x0000)
                ledger.close(task.task_id)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors
        assert len(ledger.tasks) == 8
        assert all(task.n_completed == 5 for task in ledger.tasks)
        assert len(ledger.stores) == 40
        assert all(store.attribution == "unique" for store in ledger.stores)
