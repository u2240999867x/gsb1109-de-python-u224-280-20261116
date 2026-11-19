"""Tracking and attribution of C-GET/C-MOVE retrieval tasks.

When two retrieval tasks may fetch the same SOP Instance - for example when a
duplicate C-GET request is received while an earlier one is still in progress
and one of them is cancelled - an incoming C-STORE cannot be attributed to a
task on the basis of its *SOP Instance UID* alone. The same instance may be
legitimately requested by more than one task, and a late C-STORE may arrive
after one of the tasks has been cancelled.

This module provides :class:`RetrievalLedger`, which keeps the evidence from
each step of the retrieval without guessing attribution:

* the retrieval request (C-GET/C-MOVE) and its expected sub-operations,
* any cancellation confirmation (C-CANCEL) received for the task,
* the individual sub-operations, including the command evidence
  (*Message ID* and, for C-MOVE, the *Move Originator Message ID*),
* the C-STORE material actually received, whether or not it can be
  attributed to a task.

A received store is only attributed to a task when the command evidence or a
task association recorded by the user uniquely supports it. Material that
cannot be uniquely attributed is retained as unattributed and shared between
the candidate tasks for verification purposes, but it is never counted as a
successful sub-operation more than once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import TYPE_CHECKING, Literal

from pynetdicom._globals import (
    STATUS_FAILURE,
    STATUS_SUCCESS,
    STATUS_UNKNOWN,
    STATUS_WARNING,
)
from pynetdicom.dimse_primitives import C_GET, C_MOVE
from pynetdicom.status import code_to_category

if TYPE_CHECKING:
    from pydicom.uid import UID

    from pynetdicom.dimse_primitives import C_CANCEL, C_STORE

__all__ = [
    "RetrievalLedger",
    "RetrievalStore",
    "RetrievalSubOperation",
    "RetrievalTask",
]

#: The DIMSE services that may act as retrieval tasks
RetrievalServiceType = Literal["C-GET", "C-MOVE"]
#: The possible attribution states for a received C-STORE
AttributionType = Literal["unique", "shared", "unattributed"]
#: The terminal states a retrieval task may reach
TerminalStateType = Literal[
    "pending", "success", "warning", "failure", "cancel", "aborted", "unknown"
]

_FAILURE_STATES = {STATUS_FAILURE, STATUS_UNKNOWN}


@dataclass
class RetrievalSubOperation:
    """A single C-STORE sub-operation belonging to a retrieval task.

    Attributes
    ----------
    message_id : int
        The *Message ID* of the sub-operation.
    sop_class_uid : str or None
        The *Affected SOP Class UID*, if known.
    sop_instance_uid : str or None
        The *Affected SOP Instance UID*, if known.
    command_id : int or None
        The command evidence linking the sub-operation to a task. For C-MOVE
        this is the *Move Originator Message ID*; for C-GET (which has no
        originator parameters) it is the *Message ID* of the retrieval
        request.
    originator_ae_title : str or None
        The *Move Originator Application Entity Title*, for C-MOVE.
    status_code : int or None
        The status code returned by the C-STORE response, or ``None`` if the
        sub-operation has not completed (for example because the transport
        failed before a response was received).
    store : RetrievalStore or None
        The :class:`RetrievalStore` attributed to this sub-operation, if any.
    """

    message_id: int
    sop_class_uid: str | None = None
    sop_instance_uid: str | None = None
    command_id: int | None = None
    originator_ae_title: str | None = None
    status_code: int | None = None
    store: RetrievalStore | None = None

    @property
    def is_failure(self) -> bool:
        """``True`` if the sub-operation failed or its status is unknown."""
        if self.status_code is None:
            return False

        return code_to_category(self.status_code) in _FAILURE_STATES

    @property
    def is_success(self) -> bool:
        """``True`` if the sub-operation completed with *Success*."""
        if self.status_code is None:
            return False

        return code_to_category(self.status_code) == STATUS_SUCCESS

    @property
    def is_warning(self) -> bool:
        """``True`` if the sub-operation completed with a *Warning*."""
        if self.status_code is None:
            return False

        return code_to_category(self.status_code) == STATUS_WARNING


@dataclass
class RetrievalStore:
    """C-STORE material actually received by the SCP.

    The store is retained even when it cannot be attributed to a task, so a
    late or duplicate arrival never causes a confirmed instance to be
    re-fetched.

    Attributes
    ----------
    message_id : int
        The *Message ID* of the C-STORE request.
    sop_class_uid : str
        The *Affected SOP Class UID*.
    sop_instance_uid : str
        The *Affected SOP Instance UID*.
    status_code : int or None
        The status code returned to the peer, or ``None`` if the store could
        not be completed.
    command_id : int or None
        The *Move Originator Message ID* from the C-STORE command, if present.
    originator_ae_title : str or None
        The *Move Originator Application Entity Title* from the C-STORE
        command, if present.
    attribution : str
        One of ``"unique"``, ``"shared"`` or ``"unattributed"``.
    task_ids : tuple[int, ...]
        The IDs of the candidate tasks. Contains one ID when the attribution
        is ``"unique"``, more than one when ``"shared"`` and is empty when
        ``"unattributed"``.
    """

    message_id: int
    sop_class_uid: str
    sop_instance_uid: str
    status_code: int | None = None
    command_id: int | None = None
    originator_ae_title: str | None = None
    attribution: AttributionType = "unattributed"
    task_ids: tuple[int, ...] = field(default_factory=tuple)

    @property
    def is_attributed(self) -> bool:
        """``True`` if at least one candidate task was found."""
        return bool(self.task_ids)

    @property
    def is_failure(self) -> bool:
        """``True`` if the store was rejected or failed."""
        if self.status_code is None:
            return False

        return code_to_category(self.status_code) in _FAILURE_STATES

    @property
    def is_success(self) -> bool:
        """``True`` if the store was accepted with *Success*."""
        if self.status_code is None:
            return False

        return code_to_category(self.status_code) == STATUS_SUCCESS

    @property
    def is_warning(self) -> bool:
        """``True`` if the store was accepted with a *Warning*."""
        if self.status_code is None:
            return False

        return code_to_category(self.status_code) == STATUS_WARNING


@dataclass
class RetrievalTask:
    """A C-GET or C-MOVE retrieval task.

    Attributes
    ----------
    task_id : int
        The unique, ledger-assigned identifier of the task.
    service : str
        Either ``"C-GET"`` or ``"C-MOVE"``.
    message_id : int
        The *Message ID* of the retrieval request, which acts as the task's
        command evidence within the association.
    sop_class_uid : str or None
        The *Affected SOP Class UID* of the retrieval request.
    originator_ae_title : str or None
        For C-MOVE, the AE title of the move originator.
    expected_total : int or None
        The number of sub-operations yielded by the request handler, or
        ``None`` if never declared.
    is_cancelled : bool
        ``True`` once a C-CANCEL confirmation has been recorded. Cancellation
        does not erase previously verified sub-operations and does not
        terminate any other task.
    terminal_state : str
        The terminal state of the task. ``"pending"`` means the task has not
        yet sent its final response.
    sub_operations : dict[int, RetrievalSubOperation]
        The sub-operations keyed by their *Message ID*.
    failed_instance_uids : list[str]
        The *SOP Instance UIDs* of failed sub-operations, in arrival order.
    """

    task_id: int
    service: RetrievalServiceType
    message_id: int
    sop_class_uid: str | None = None
    originator_ae_title: str | None = None
    expected_total: int | None = None
    is_cancelled: bool = False
    terminal_state: TerminalStateType = "pending"
    sub_operations: dict[int, RetrievalSubOperation] = field(default_factory=dict)
    failed_instance_uids: list[str] = field(default_factory=list)

    @property
    def is_terminal(self) -> bool:
        """``True`` once a final response state has been recorded."""
        return self.terminal_state != "pending"

    @property
    def n_completed(self) -> int:
        """The number of successfully completed sub-operations.

        A sub-operation counts once its own C-STORE response is verified as
        *Success*. That response is the task's own command evidence, so it
        does not depend on separately attributed incoming material. Shared
        or unattributed stores never create a completion on their own.
        """
        return sum(1 for subop in self.sub_operations.values() if subop.is_success)

    @property
    def n_failed(self) -> int:
        """The number of failed sub-operations."""
        return sum(1 for subop in self.sub_operations.values() if subop.is_failure)

    @property
    def n_warning(self) -> int:
        """The number of sub-operations completed with a warning."""
        return sum(1 for subop in self.sub_operations.values() if subop.is_warning)

    @property
    def n_remaining(self) -> int:
        """The number of sub-operations without a verified terminal result.

        Returns ``0`` when the expected total was never declared.
        """
        if self.expected_total is None:
            return 0

        accounted = self.n_completed + self.n_failed + self.n_warning
        return max(0, self.expected_total - accounted)

    @property
    def has_missing_count(self) -> bool:
        """``True`` if the task's completion coverage cannot be confirmed.

        This is the case when the expected total was never declared or when
        one or more expected sub-operations have no terminal result.
        """
        return self.expected_total is None or self.n_remaining > 0

    @property
    def stores(self) -> tuple[RetrievalStore, ...]:
        """The material attributed to the task, in sub-operation order."""
        return tuple(
            subop.store
            for subop in self.sub_operations.values()
            if subop.store is not None
        )

    @property
    def verified_instance_uids(self) -> tuple[str, ...]:
        """The UIDs of successfully verified instances, in arrival order."""
        return tuple(
            subop.sop_instance_uid
            for subop in self.sub_operations.values()
            if subop.is_success and subop.sop_instance_uid is not None
        )


class RetrievalLedger:
    """Evidence-preserving ledger for retrieval tasks and received stores.

    The ledger is safe to use from multiple threads. It is normally attached
    to an :class:`~pynetdicom.association.Association` so that it outlives the
    per-message service class instances.

    Examples
    --------
    Record a C-GET request, its sub-operations, a cancellation and a late
    C-STORE that cannot be uniquely attributed::

        >>> ledger = RetrievalLedger()
        >>> task = ledger.begin("C-GET", message_id=1, expected_total=1)
        >>> ledger.record_sub_operation(task.task_id, 2, "1.2.3", "1.2.3.4")
        >>> ledger.confirm_cancel(task.task_id)
        >>> store = ledger.record_store(2, "1.2.3", "1.2.3.4")
        >>> store.attribution
        'unique'
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._tasks: dict[int, RetrievalTask] = {}
        self._stores: list[RetrievalStore] = []
        self._next_task_id = 1

    def begin(
        self,
        service: RetrievalServiceType,
        message_id: int,
        *,
        sop_class_uid: str | None = None,
        originator_ae_title: str | None = None,
        expected_total: int | None = None,
    ) -> RetrievalTask:
        """Record a new retrieval request.

        Parameters
        ----------
        service : str
            Either ``"C-GET"`` or ``"C-MOVE"``.
        message_id : int
            The *Message ID* of the retrieval request.
        sop_class_uid : str, optional
            The *Affected SOP Class UID* of the retrieval request.
        originator_ae_title : str, optional
            The AE title of the move originator, for C-MOVE.
        expected_total : int, optional
            The number of sub-operations the request handler expects to
            perform. May be omitted and supplied later via
            :meth:`set_expected_total`.

        Returns
        -------
        RetrievalTask
            The newly created task.

        Raises
        ------
        ValueError
            If `service` is invalid, `expected_total` is negative or another
            active task for the same service and *Message ID* already exists.
        """
        if service not in ("C-GET", "C-MOVE"):
            raise ValueError("'service' must be 'C-GET' or 'C-MOVE'")

        if expected_total is not None and expected_total < 0:
            raise ValueError("'expected_total' must be a positive integer or None")

        with self._lock:
            for existing in self._tasks.values():
                if (
                    existing.service == service
                    and existing.message_id == message_id
                    and not existing.is_terminal
                ):
                    raise ValueError(
                        f"An active {service} task with message ID {message_id} "
                        "already exists"
                    )

            task = RetrievalTask(
                task_id=self._next_task_id,
                service=service,
                message_id=message_id,
                sop_class_uid=sop_class_uid,
                originator_ae_title=originator_ae_title,
                expected_total=expected_total,
            )
            self._next_task_id += 1
            self._tasks[task.task_id] = task
            return task

    def begin_from_request(
        self,
        request: "C_GET | C_MOVE",
        *,
        expected_total: int | None = None,
    ) -> RetrievalTask:
        """Record a retrieval request from a C-GET or C-MOVE primitive.

        Parameters
        ----------
        request : dimse_primitives.C_GET or dimse_primitives.C_MOVE
            The received retrieval request primitive.
        expected_total : int, optional
            The number of sub-operations the request handler expects to
            perform.

        Returns
        -------
        RetrievalTask
            The newly created task.
        """
        if isinstance(request, C_GET):
            service: RetrievalServiceType = "C-GET"
        elif isinstance(request, C_MOVE):
            service = "C-MOVE"
        else:
            raise ValueError(
                "The request must be a C_GET or C_MOVE primitive, "
                f"got {type(request).__name__}"
            )

        return self.begin(
            service,
            cast_message_id(request.MessageID),
            sop_class_uid=_as_str(request.AffectedSOPClassUID),
            expected_total=expected_total,
        )

    def set_expected_total(self, task_id: int, total: int) -> None:
        """Declare the number of sub-operations expected by a task.

        Parameters
        ----------
        task_id : int
            The identifier returned by :meth:`begin`.
        total : int
            The expected number of sub-operations.

        Raises
        ------
        KeyError
            If `task_id` is unknown.
        ValueError
            If `total` is negative or smaller than the number of sub-operations
            already recorded.
        """
        if total < 0:
            raise ValueError("'total' must be a positive integer")

        with self._lock:
            task = self._require_task(task_id)
            if total < len(task.sub_operations):
                raise ValueError(
                    "'total' cannot be smaller than the number of "
                    f"sub-operations already recorded ({len(task.sub_operations)})"
                )

            task.expected_total = total

    def record_sub_operation(
        self,
        task_id: int,
        message_id: int,
        sop_class_uid: str | None = None,
        sop_instance_uid: str | None = None,
        *,
        originator_ae_title: str | None = None,
    ) -> RetrievalSubOperation:
        """Record a C-STORE sub-operation performed on behalf of a task.

        Parameters
        ----------
        task_id : int
            The identifier of the owning task.
        message_id : int
            The *Message ID* of the C-STORE request.
        sop_class_uid : str, optional
            The *Affected SOP Class UID* being stored.
        sop_instance_uid : str, optional
            The *Affected SOP Instance UID* being stored.
        originator_ae_title : str, optional
            The *Move Originator Application Entity Title*, for C-MOVE.

        Returns
        -------
        RetrievalSubOperation
            The recorded sub-operation.

        Raises
        ------
        KeyError
            If `task_id` is unknown.
        ValueError
            If a sub-operation with `message_id` already exists for the task.
        """
        with self._lock:
            task = self._require_task(task_id)
            if message_id in task.sub_operations:
                raise ValueError(
                    f"A sub-operation with message ID {message_id} has already "
                    f"been recorded for task {task_id}"
                )

            subop = RetrievalSubOperation(
                message_id=message_id,
                sop_class_uid=sop_class_uid,
                sop_instance_uid=sop_instance_uid,
                command_id=task.message_id,
                originator_ae_title=originator_ae_title
                if originator_ae_title is not None
                else task.originator_ae_title,
            )
            task.sub_operations[message_id] = subop

            # New command evidence may resolve a store that was previously
            #   unattributed or shared between several tasks
            for store in self._stores:
                if store.attribution == "unique":
                    continue

                candidates = self._candidate_tasks(store)
                self._attribute(store, candidates)

            return subop

    def record_sub_operation_result(
        self,
        task_id: int,
        message_id: int,
        status_code: int | None,
    ) -> RetrievalSubOperation:
        """Record the terminal status of a sub-operation.

        Parameters
        ----------
        task_id : int
            The identifier of the owning task.
        message_id : int
            The *Message ID* of the sub-operation.
        status_code : int or None
            The C-STORE response status code, or ``None`` if no response was
            received (for example because the transport failed).

        Returns
        -------
        RetrievalSubOperation
            The updated sub-operation.

        Raises
        ------
        KeyError
            If `task_id` or the sub-operation is unknown.
        """
        with self._lock:
            task = self._require_task(task_id)
            try:
                subop = task.sub_operations[message_id]
            except KeyError:
                raise KeyError(
                    f"No sub-operation with message ID {message_id} for "
                    f"task {task_id}"
                ) from None

            subop.status_code = status_code
            if subop.is_failure and subop.sop_instance_uid is not None:
                uid = subop.sop_instance_uid
                if uid not in task.failed_instance_uids:
                    task.failed_instance_uids.append(uid)

            if subop.store is not None and subop.store.status_code is None:
                subop.store.status_code = status_code

            return subop

    def confirm_cancel(self, task_id: int, cancel: "C_CANCEL | None" = None) -> None:
        """Record a C-CANCEL confirmation for a task.

        Cancelling a task does not erase its previously verified
        sub-operations and has no effect on any other task.

        Parameters
        ----------
        task_id : int
            The identifier of the cancelled task.
        cancel : dimse_primitives.C_CANCEL, optional
            The received C-CANCEL primitive. Currently retained for evidence;
            the *Message ID Being Responded To* must match the task.

        Raises
        ------
        KeyError
            If `task_id` is unknown.
        ValueError
            If `cancel` refers to a different *Message ID*.
        """
        with self._lock:
            task = self._require_task(task_id)
            if cancel is not None and cancel.MessageIDBeingRespondedTo is not None:
                if cancel.MessageIDBeingRespondedTo != task.message_id:
                    raise ValueError(
                        "The C-CANCEL message ID being responded to "
                        f"({cancel.MessageIDBeingRespondedTo}) does not match "
                        f"the task message ID ({task.message_id})"
                    )

            task.is_cancelled = True
            if task.terminal_state == "pending":
                task.terminal_state = "cancel"

    def close(
        self,
        task_id: int,
        terminal_state: TerminalStateType | None = None,
    ) -> RetrievalTask:
        """Record the terminal state of a task when its final response is sent.

        Parameters
        ----------
        task_id : int
            The identifier of the task.
        terminal_state : str, optional
            One of ``"success"``, ``"warning"``, ``"failure"``, ``"cancel"``,
            ``"aborted"`` or ``"unknown"``. If omitted then the state is
            derived from the recorded sub-operations, with cancellation taking
            precedence.

        Returns
        -------
        RetrievalTask
            The closed task.

        Raises
        ------
        KeyError
            If `task_id` is unknown.
        ValueError
            If `terminal_state` is invalid.
        """
        valid = {
            "pending",
            "success",
            "warning",
            "failure",
            "cancel",
            "aborted",
            "unknown",
        }
        if terminal_state is not None and terminal_state not in valid:
            raise ValueError(f"Invalid terminal state: {terminal_state!r}")

        with self._lock:
            task = self._require_task(task_id)
            if terminal_state is not None:
                task.terminal_state = terminal_state
            elif task.terminal_state == "pending":
                task.terminal_state = self._derive_terminal_state(task)

            return task

    def record_store(
        self,
        message_id: int,
        sop_class_uid: str,
        sop_instance_uid: str,
        *,
        status_code: int | None = None,
        command_id: int | None = None,
        originator_ae_title: str | None = None,
    ) -> RetrievalStore:
        """Record C-STORE material actually received.

        The store is attributed only when the command evidence, or a
        previously recorded sub-operation, uniquely identifies a single task.
        A matching *SOP Instance UID* alone is never sufficient. When more
        than one task remains a candidate the store is marked ``"shared"`` and
        may be used to verify each candidate, but it contributes no
        completion count until the attribution becomes unique. Stores with no
        candidate at all are kept as ``"unattributed"``.

        Parameters
        ----------
        message_id : int
            The *Message ID* of the C-STORE request.
        sop_class_uid : str
            The *Affected SOP Class UID*.
        sop_instance_uid : str
            The *Affected SOP Instance UID*.
        status_code : int, optional
            The status returned to the peer.
        command_id : int, optional
            The *Move Originator Message ID* from the C-STORE command.
        originator_ae_title : str, optional
            The *Move Originator Application Entity Title* from the C-STORE
            command.

        Returns
        -------
        RetrievalStore
            The recorded store, including its attribution decision.
        """
        with self._lock:
            store = RetrievalStore(
                message_id=message_id,
                sop_class_uid=sop_class_uid,
                sop_instance_uid=sop_instance_uid,
                status_code=status_code,
                command_id=command_id,
                originator_ae_title=originator_ae_title,
            )
            self._stores.append(store)

            candidates = self._candidate_tasks(store)
            self._attribute(store, candidates)
            return store

    def record_store_from_request(
        self,
        request: "C_STORE",
        *,
        status_code: int | None = None,
    ) -> RetrievalStore:
        """Record a received C-STORE from a :class:`~pynetdicom.dimse_primitives.C_STORE`.

        Parameters
        ----------
        request : dimse_primitives.C_STORE
            The received C-STORE request primitive.
        status_code : int, optional
            The status returned to the peer.

        Returns
        -------
        RetrievalStore
            The recorded store.
        """
        return self.record_store(
            cast_message_id(request.MessageID),
            _require_str(request.AffectedSOPClassUID),
            _require_str(request.AffectedSOPInstanceUID),
            status_code=status_code,
            command_id=request.MoveOriginatorMessageID,
            originator_ae_title=_as_str(request.MoveOriginatorApplicationEntityTitle),
        )

    def get_task(self, task_id: int) -> RetrievalTask:
        """Return the task with `task_id`.

        Raises
        ------
        KeyError
            If the task is unknown.
        """
        with self._lock:
            return self._require_task(task_id)

    def find_task(
        self,
        service: RetrievalServiceType,
        message_id: int,
    ) -> RetrievalTask | None:
        """Return the task for `service` and `message_id`, if any."""
        with self._lock:
            for task in self._tasks.values():
                if task.service == service and task.message_id == message_id:
                    return task

            return None

    @property
    def tasks(self) -> tuple[RetrievalTask, ...]:
        """All recorded tasks, in creation order."""
        with self._lock:
            return tuple(self._tasks.values())

    @property
    def stores(self) -> tuple[RetrievalStore, ...]:
        """All recorded stores, in arrival order."""
        with self._lock:
            return tuple(self._stores)

    def failed_stores(self) -> tuple[RetrievalStore, ...]:
        """Return the stores whose actual result was a failure or unknown.

        This answers the "actual failure" query independently of any
        attribution decision.
        """
        with self._lock:
            return tuple(store for store in self._stores if store.is_failure)

    def unattributed_stores(self) -> tuple[RetrievalStore, ...]:
        """Return stores with no supporting task evidence at all."""
        with self._lock:
            return tuple(
                store
                for store in self._stores
                if store.attribution == "unattributed"
            )

    def ambiguous_stores(self) -> tuple[RetrievalStore, ...]:
        """Return stores shared between more than one candidate task.

        These cannot be uniquely attributed on the available command
        evidence. They may verify and be shared between tasks, but they
        contribute a completion count only where a recorded sub-operation
        links them, never twice for the same material.
        """
        with self._lock:
            return tuple(
                store for store in self._stores if store.attribution == "shared"
            )

    def unresolvable_stores(self) -> tuple[RetrievalStore, ...]:
        """Return every store that cannot be uniquely attributed.

        Combines :meth:`unattributed_stores` and :meth:`ambiguous_stores`.
        """
        with self._lock:
            return tuple(
                store
                for store in self._stores
                if store.attribution != "unique"
            )

    def missing_counts(self) -> tuple[RetrievalTask, ...]:
        """Return tasks whose completion coverage cannot be confirmed.

        A task is included when its expected total was never declared or when
        one or more expected sub-operations have no terminal result.
        """
        with self._lock:
            return tuple(
                task for task in self._tasks.values() if task.has_missing_count
            )

    def instance_verification(self, sop_instance_uid: str) -> RetrievalStore | None:
        """Return the latest accepted store for `sop_instance_uid`, if any.

        Allows callers to avoid re-fetching instances that have already been
        verified, regardless of which task caused them to arrive.
        """
        with self._lock:
            for store in reversed(self._stores):
                if (
                    store.sop_instance_uid == sop_instance_uid
                    and (store.is_success or store.is_warning)
                ):
                    return store

            return None

    def _candidate_tasks(self, store: RetrievalStore) -> list[RetrievalTask]:
        """Return the tasks that the command evidence for `store` supports.

        A direct match between the C-STORE *Message ID* and a recorded
        sub-operation (with matching UID) is strong command evidence. When at
        least one strong match exists the weaker evidence - C-MOVE originator
        parameters or a same-UID C-GET request list - is ignored, so it can
        never dilute a unique attribution.
        """
        strong: list[RetrievalTask] = []
        weak: list[RetrievalTask] = []
        for task in self._tasks.values():
            # Strong evidence: the C-STORE message ID matches a recorded
            #   sub-operation of the task and the UIDs agree
            subop = task.sub_operations.get(store.message_id)
            if subop is not None:
                if _uid_matches(subop.sop_instance_uid, store.sop_instance_uid):
                    strong.append(task)
                continue

            # Weak evidence (a): C-MOVE originator parameters, the command
            #   carries the message ID of the originating C-MOVE request
            if (
                store.command_id is not None
                and task.service == "C-MOVE"
                and task.message_id == store.command_id
                and _ae_matches(
                    store.originator_ae_title, task.originator_ae_title
                )
            ):
                weak.append(task)
                continue

            # Weak evidence (b): C-GET has no originator parameters; a UID in
            #   the task's request list only makes it a candidate, never a
            #   unique attribution on its own
            if store.command_id is None and task.service == "C-GET":
                for other in task.sub_operations.values():
                    if _uid_matches(
                        other.sop_instance_uid, store.sop_instance_uid
                    ):
                        weak.append(task)
                        break

        candidates = strong if strong else weak

        # De-duplicate while preserving order
        unique: list[RetrievalTask] = []
        for task in candidates:
            if task not in unique:
                unique.append(task)

        return unique

    def _attribute(
        self,
        store: RetrievalStore,
        candidates: list[RetrievalTask],
    ) -> None:
        """Apply the attribution decision for `store` and link sub-operations."""
        # Re-attribution may revoke an earlier shared link
        for task in self._tasks.values():
            for subop in task.sub_operations.values():
                if subop.store is store:
                    subop.store = None

        task_ids = tuple(task.task_id for task in candidates)
        if not task_ids:
            store.attribution = "unattributed"
            store.task_ids = ()
            return

        store.attribution = "unique" if len(task_ids) == 1 else "shared"
        store.task_ids = task_ids

        for task in candidates:
            # A task that has sent its final response keeps frozen counts:
            #   late material remains evidence on the store but must not
            #   retroactively change the task's completion coverage
            if task.is_terminal:
                continue

            linked_subop: RetrievalSubOperation | None = (
                task.sub_operations.get(store.message_id)
            )
            if linked_subop is not None and linked_subop.store is None:
                linked_subop.store = store
                if linked_subop.status_code is None:
                    linked_subop.status_code = store.status_code
                if linked_subop.sop_instance_uid is None:
                    linked_subop.sop_instance_uid = store.sop_instance_uid
                if linked_subop.sop_class_uid is None:
                    linked_subop.sop_class_uid = store.sop_class_uid

    def _derive_terminal_state(self, task: RetrievalTask) -> TerminalStateType:
        if task.is_cancelled:
            return "cancel"

        if task.n_failed:
            if task.n_completed or task.n_warning:
                return "warning"

            return "failure"

        if task.n_warning:
            return "warning"

        if task.expected_total is not None and task.n_remaining:
            return "unknown"

        return "success"

    def _require_task(self, task_id: int) -> RetrievalTask:
        try:
            return self._tasks[task_id]
        except KeyError:
            raise KeyError(f"Unknown retrieval task ID: {task_id}") from None


def cast_message_id(value: int | None) -> int:
    """Return the DIMSE *Message ID* `value`, requiring it to be set."""
    if value is None:
        raise ValueError("The request primitive has no Message ID")

    return value


def _require_str(value: str | UID | None) -> str:
    if value is None:
        raise ValueError("The C-STORE request is missing a required UID")

    return str(value)


def _as_str(value: str | UID | None) -> str | None:
    return None if value is None else str(value)


def _uid_matches(expected: str | None, actual: str) -> bool:
    """Return True if `actual` matches the expected UID.

    A missing expected UID does not match: the UID alone is not evidence.
    """
    return expected is not None and expected == actual


def _ae_matches(expected: str | None, actual: str | None) -> bool:
    """Return True if the originator AE titles are compatible."""
    if expected is None or actual is None:
        return True

    return expected == actual
