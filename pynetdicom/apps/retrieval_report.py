"""Per-instance reporting for Query/Retrieve C-GET and C-MOVE operations.

A C-GET or C-MOVE response reports cumulative *Number of Completed, Failed,
Warning and Remaining Sub-operations* counts.  For C-GET the corresponding
C-STORE requests arrive over the same association, while for C-MOVE they
normally arrive over a separate association with the *Move Destination*.  The
counts and the C-STORE receipts therefore come from different places and do
not necessarily agree.

:class:`RetrievalReport` keeps the two sources separate, merges them by
*SOP Instance UID* and produces a JSON-serialisable report containing:

* the counts reported by the peer,
* the C-STORE receipts observed locally (with duplicate receipts counted
  once per SOP Instance UID),
* the difference between the two totals,
* the part of the reported result that can be tied to specific SOP Instance
  UIDs and the part that cannot.

A *Failed SOP Instance UID List* is the only standard way a peer identifies
which sub-operations failed.  When it is absent or incomplete the report never
invents UIDs from the failed count: the unidentified remainder is reported as
a count only.
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

from pydicom.dataset import Dataset

from pynetdicom._globals import (
    STATUS_FAILURE,
    STATUS_PENDING,
    STATUS_SUCCESS,
    STATUS_UNKNOWN,
    STATUS_WARNING,
)
from pynetdicom.status import code_to_category

__all__ = ["RetrievalReport"]

# Result categories used in the per-instance section
COMPLETED = "Completed"
WARNING = "Warning"
FAILED = "Failed"
CONFLICT = "Conflict"
UNKNOWN = "Unknown"

_COUNT_KEYWORDS = (
    ("remaining", "NumberOfRemainingSuboperations"),
    ("completed", "NumberOfCompletedSuboperations"),
    ("failed", "NumberOfFailedSuboperations"),
    ("warning", "NumberOfWarningSuboperations"),
)


def _as_int(value: Any) -> int | None:
    """Return `value` as an :class:`int` or ``None`` if it isn't one."""
    if isinstance(value, bool):
        return None

    if isinstance(value, int):
        return value

    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _status_code(status: Any) -> int | None:
    """Return the status code from a response status value."""
    if isinstance(status, int):
        return status

    if status is None:
        return None

    return _as_int(getattr(status, "Status", None))


def _receipt_category(status: Any) -> str:
    """Return the result category for a C-STORE response."""
    code = _status_code(status)
    if code is None:
        return UNKNOWN

    category = code_to_category(code)
    if category == STATUS_SUCCESS:
        return COMPLETED
    if category == STATUS_WARNING:
        return WARNING
    if category == STATUS_FAILURE:
        return FAILED

    return UNKNOWN


def _failed_uids(identifier: Any) -> list[str] | None:
    """Return the *Failed SOP Instance UID List* or ``None`` if absent."""
    if identifier is None or not isinstance(identifier, Dataset):
        return None

    if "FailedSOPInstanceUIDList" not in identifier:
        return None

    value = identifier.FailedSOPInstanceUIDList
    if value is None:
        return []

    if isinstance(value, str):
        values: Iterable[Any] = value.split("\\")
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        # pydicom MultiValue and other iterables
        try:
            values = list(value)
        except TypeError:
            values = [value]

    out = []
    for item in values:
        uid = str(item).strip()
        if uid and uid not in out:
            out.append(uid)

    return out


def _normalise_uid(value: Any) -> str | None:
    """Return `value` as a non-empty SOP Instance UID string."""
    if value is None:
        return None

    uid = str(value).strip()
    return uid or None


class RetrievalReport:
    """Reconcile C-GET/C-MOVE reported counts with C-STORE receipts.

    The report is built incrementally:

    * call :meth:`add_response` for every ``(status, identifier)`` yielded by
      ``Association.send_c_get()`` or ``Association.send_c_move()``,
    * call :meth:`add_store_receipt` for every C-STORE response observed by
      the local Storage SCP (``event.request.AffectedSOPInstanceUID`` and the
      status returned by the handler).

    Each SOP Instance UID contributes at most one per-instance result no
    matter how many C-STORE receipts are seen for it.
    """

    def __init__(self) -> None:
        self._responses: list[dict[str, Any]] = []
        # SOP Instance UID -> list of receipt dictionaries
        self._receipts: dict[str, list[dict[str, Any]]] = {}
        self._receipt_order: list[str] = []
        # Receipts without a usable SOP Instance UID can't be identified or
        #   de-duplicated, so they're tracked separately
        self._unidentified_receipts = 0
        # C-MOVE receipts arrive on the Storage SCP's association, which is
        #   usually a different thread from the one reading the responses
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------
    def add_response(self, status: Any, identifier: Any = None) -> None:
        """Record a C-GET or C-MOVE response.

        Parameters
        ----------
        status : pydicom.dataset.Dataset or int or None
            The response *Status* dataset yielded by
            ``Association.send_c_get()`` or ``Association.send_c_move()``.
        identifier : pydicom.dataset.Dataset or None, optional
            The response *Identifier* dataset, which for a Warning, Failure
            or Cancel response may contain a (0008,0058) *Failed SOP Instance
            UID List*.
        """
        code = _status_code(status)
        category = STATUS_UNKNOWN if code is None else code_to_category(code)

        counts = {}
        for name, keyword in _COUNT_KEYWORDS:
            counts[name] = _as_int(getattr(status, keyword, None))

        with self._lock:
            self._responses.append(
                {
                    "code": code,
                    "category": category,
                    "counts": counts,
                    "failed_uids": _failed_uids(identifier),
                }
            )

    def add_store_receipt(self, sop_instance_uid: Any, status: Any) -> bool:
        """Record a C-STORE receipt for a SOP Instance.

        Repeated receipts for the same SOP Instance UID are stored so the
        duplicate count and any conflicting outcomes remain visible, but they
        only ever produce a single per-instance result.

        Parameters
        ----------
        sop_instance_uid : str or pydicom.uid.UID or None
            The *Affected SOP Instance UID* from the C-STORE request.
        status : int or pydicom.dataset.Dataset or None
            The C-STORE response status returned by the store handler.

        Returns
        -------
        bool
            ``True`` if this is the first receipt for `sop_instance_uid`,
            ``False`` if it is a duplicate or the UID is unavailable.
        """
        code = _status_code(status)
        receipt = {"code": code, "result": _receipt_category(status)}

        uid = _normalise_uid(sop_instance_uid)
        if uid is None:
            with self._lock:
                self._unidentified_receipts += 1
            return False

        with self._lock:
            if uid not in self._receipts:
                self._receipts[uid] = []
                self._receipt_order.append(uid)

            self._receipts[uid].append(receipt)
            return len(self._receipts[uid]) == 1

    # ------------------------------------------------------------------
    # Derived values
    # ------------------------------------------------------------------
    @property
    def has_final_response(self) -> bool:
        """Return ``True`` if a non-Pending response has been recorded."""
        return any(rr["category"] != STATUS_PENDING for rr in self._responses)

    def _final_response(self) -> dict[str, Any] | None:
        """Return the final response, or the last response if none is final."""
        if not self._responses:
            return None

        for response in reversed(self._responses):
            if response["code"] is not None and response["category"] != STATUS_PENDING:
                return response

        return self._responses[-1]

    def _reported(self) -> dict[str, Any]:
        """Return the reported sub-operation counts."""
        response = self._final_response()
        if response is None:
            return {
                "remaining": None,
                "completed": None,
                "failed": None,
                "warning": None,
                "total": None,
                "final_received": False,
                "response_count": 0,
                "status": None,
                "category": None,
            }

        category = response["category"]
        counts = dict(response["counts"])

        # Final responses must include all four counts, but peers don't always
        #   comply.  A Pending response's values are not carried over because
        #   they describe an earlier point in the operation and would overstate
        #   what the final response actually proves.  The one safe completion
        #   value is a zero remaining count for Success/Warning.
        if counts["remaining"] is None and category in (STATUS_SUCCESS, STATUS_WARNING):
            counts["remaining"] = 0

        known = [counts[name] for name, _ in _COUNT_KEYWORDS]
        total = sum(known) if all(value is not None for value in known) else None

        return {
            **counts,
            "total": total,
            # An empty Dataset (timeout, abort or invalid response) has no
            #   Status, so it doesn't prove anything about the operation
            "final_received": (
                response["code"] is not None and category != STATUS_PENDING
            ),
            "response_count": len(self._responses),
            "status": None if response["code"] is None else f"0x{response['code']:04X}",
            "category": category,
        }

    def _failed_uid_list(self) -> tuple[list[str] | None, list[str]]:
        """Return the final failed UID list and any lists from other responses."""
        response = self._final_response()
        final_list = None if response is None else response["failed_uids"]

        other: list[str] = []
        for rr in self._responses:
            if rr is response or rr["failed_uids"] is None:
                continue
            for uid in rr["failed_uids"]:
                if uid not in other:
                    other.append(uid)

        return final_list, other

    def _receipt_summary(self) -> dict[str, Any]:
        """Return the de-duplicated C-STORE receipt summary."""
        results = Counter()
        status_codes = Counter()
        duplicate_receipts = 0

        for uid in self._receipt_order:
            receipts = self._receipts[uid]
            duplicate_receipts += len(receipts) - 1
            categories = {rr["result"] for rr in receipts}
            results[CONFLICT if len(categories) > 1 else next(iter(categories))] += 1
            for receipt in receipts:
                status_codes[
                    "Unknown" if receipt["code"] is None else f"0x{receipt['code']:04X}"
                ] += 1

        return {
            "distinct_instances": len(self._receipt_order),
            "total_receipts": sum(len(v) for v in self._receipts.values()),
            "duplicate_receipts": duplicate_receipts,
            "unidentified_receipts": self._unidentified_receipts,
            "completed": results[COMPLETED],
            "warning": results[WARNING],
            "failed": results[FAILED],
            "conflict": results[CONFLICT],
            "unknown": results[UNKNOWN],
            "status_codes": dict(status_codes),
        }

    def _instances(self, failed_uids: Sequence[str]) -> list[dict[str, Any]]:
        """Return the merged per-instance results."""
        failed_set = set(failed_uids)
        uids = list(self._receipt_order)
        for uid in failed_uids:
            if uid not in self._receipts:
                uids.append(uid)

        rows = []
        for uid in sorted(uids):
            receipts = self._receipts.get(uid, [])
            receipt_categories = {rr["result"] for rr in receipts}
            evidence = []
            if receipts:
                evidence.append("store-receipt")
            if COMPLETED in receipt_categories:
                evidence.append("store-success")
            if FAILED in receipt_categories:
                evidence.append("store-failure")
            if WARNING in receipt_categories:
                evidence.append("store-warning")
            if uid in failed_set:
                evidence.append("failed-uid-list")

            if len(receipt_categories) > 1:
                result = CONFLICT
            elif receipt_categories:
                result = next(iter(receipt_categories))
            elif uid in failed_set:
                result = FAILED
            else:
                result = UNKNOWN

            # A successful local receipt doesn't override the peer saying the
            #   sub-operation failed - the two sources disagree and the row is
            #   marked as a conflict rather than silently picking one.
            if uid in failed_set and result == COMPLETED:
                result = CONFLICT

            rows.append(
                {
                    "sop_instance_uid": uid,
                    "result": result,
                    "evidence": evidence,
                    "receipt_count": len(receipts),
                    "duplicate_receipts": max(len(receipts) - 1, 0),
                    "status_codes": [
                        None if rr["code"] is None else f"0x{rr['code']:04X}"
                        for rr in receipts
                    ],
                }
            )

        return rows

    def _coverage(
        self,
        reported: dict[str, Any],
        failed_uids: Sequence[str],
        instances: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return what part of the reported result is tied to UIDs."""
        count = Counter(row["result"] for row in instances)
        # Coverage follows the peer's categories: a conflicting UID is counted
        #   where the peer placed it (normally the failed UID list) and the
        #   disagreement is reported separately instead of being counted twice.
        completed_uids = [
            row["sop_instance_uid"] for row in instances if row["result"] == COMPLETED
        ]
        warning_uids = [
            row["sop_instance_uid"] for row in instances if row["result"] == WARNING
        ]
        identified_failed = [
            row["sop_instance_uid"]
            for row in instances
            if row["result"] == FAILED or "failed-uid-list" in row["evidence"]
        ]

        def unidentified(name: str, identified: int) -> int | None:
            if reported[name] is None:
                return None
            return max(reported[name] - identified, 0)

        unidentified_completed = unidentified("completed", len(completed_uids))
        unidentified_failed = unidentified("failed", len(identified_failed))
        unidentified_warning = unidentified("warning", len(warning_uids))
        unidentified_remaining = reported["remaining"]

        parts = [
            unidentified_completed,
            unidentified_failed,
            unidentified_warning,
            unidentified_remaining,
        ]
        unidentified_total = None if any(p is None for p in parts) else sum(parts)

        final_list, _ = self._failed_uid_list()
        failed_uids_provided = bool(final_list)
        failed_uid_count = len(final_list or [])
        notes = []
        if reported["response_count"] == 0:
            notes.append("No C-GET/C-MOVE response was recorded.")
        elif not reported["final_received"]:
            notes.append(
                "No final C-GET/C-MOVE response was received; the counts come "
                "from the last Pending response and may be incomplete."
            )

        if reported["failed"]:
            if final_list is None:
                notes.append(
                    f"The peer reported {reported['failed']} failed sub-operation(s) "
                    "but did not provide a Failed SOP Instance UID List, so the "
                    "specific failed SOP Instance UIDs cannot be determined from "
                    "the reported counts."
                )
            elif not final_list:
                notes.append(
                    f"The peer reported {reported['failed']} failed sub-operation(s) "
                    "with an empty Failed SOP Instance UID List, so the specific "
                    "failed SOP Instance UIDs cannot be determined from the "
                    "reported counts."
                )
            elif failed_uid_count < reported["failed"]:
                notes.append(
                    f"The Failed SOP Instance UID List identifies {failed_uid_count} "
                    f"of {reported['failed']} failed sub-operation(s); the remaining "
                    f"{reported['failed'] - failed_uid_count} cannot be assigned to "
                    "specific SOP Instance UIDs."
                )

        if not self._receipt_order:
            notes.append(
                "No C-STORE receipts were observed on this association; "
                "per-instance storage coverage is limited to any Failed SOP "
                "Instance UID List returned by the peer."
            )

        if self._unidentified_receipts:
            notes.append(
                f"{self._unidentified_receipts} C-STORE receipt(s) had no SOP "
                "Instance UID and could not be de-duplicated or tied to a "
                "reported sub-operation."
            )

        duplicates = sum(len(vv) - 1 for vv in self._receipts.values())
        if duplicates:
            notes.append(
                f"{duplicates} duplicate C-STORE receipt(s) were counted once "
                "per SOP Instance UID."
            )

        if unidentified_completed:
            notes.append(
                f"{unidentified_completed} reported completed sub-operation(s) "
                "have no matching successful C-STORE receipt on this association."
            )

        if unidentified_failed:
            notes.append(
                f"{unidentified_failed} reported failed sub-operation(s) are not "
                "identified by a SOP Instance UID."
            )
        elif reported["failed"] is not None:
            observed_failed = count[FAILED] + count[CONFLICT]
            if observed_failed > reported["failed"]:
                notes.append(
                    f"{observed_failed - reported['failed']} more locally observed "
                    "C-STORE failure(s) than the peer reported failed "
                    "sub-operation(s); the peer's counts and the local receipts "
                    "don't agree."
                )

        if unidentified_warning:
            notes.append(
                f"{unidentified_warning} reported warning sub-operation(s) are "
                "not identified by a SOP Instance UID; DICOM has no warning UID "
                "list, so only locally received warnings can be named."
            )

        if reported["remaining"]:
            notes.append(
                f"{reported['remaining']} sub-operation(s) remained at the final "
                "response."
            )

        conflicts = count[CONFLICT]
        if conflicts:
            notes.append(
                f"{conflicts} SOP Instance UID(s) have conflicting evidence "
                "(for example a successful local C-STORE receipt but inclusion "
                "in the peer's Failed SOP Instance UID List)."
            )

        observed_total = len(self._receipt_order)
        reported_total = reported["total"]
        if reported_total is not None and observed_total > reported_total:
            notes.append(
                f"{observed_total - reported_total} more distinct C-STORE "
                "receipt(s) were observed than the reported sub-operation total."
            )

        return {
            "store_receipts_observed": bool(self._receipt_order),
            "failed_uid_list_provided": failed_uids_provided,
            "failed_uid_list_count": failed_uid_count,
            "identified_completed": len(completed_uids),
            "identified_failed": len(identified_failed),
            "identified_warning": len(warning_uids),
            "identified_conflicts": conflicts,
            "unidentified_completed": unidentified_completed,
            "unidentified_failed": unidentified_failed,
            "unidentified_warning": unidentified_warning,
            "unidentified_remaining": unidentified_remaining,
            "unidentified_total": unidentified_total,
            "notes": notes,
        }

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------
    def report(self) -> dict[str, Any]:
        """Return the report as a JSON-serialisable :class:`dict`."""
        with self._lock:
            reported = self._reported()
            failed_list, other_lists = self._failed_uid_list()
            failed_uids = list(failed_list or [])
            instances = self._instances(failed_uids)
            observed = self._receipt_summary()
            coverage = self._coverage(reported, failed_uids, instances)

        differences = {}
        for name in ("completed", "failed", "warning"):
            if reported[name] is None:
                differences[name] = None
            else:
                differences[name] = reported[name] - observed[name]

        if reported["total"] is None:
            differences["total"] = None
        else:
            differences["total"] = reported["total"] - observed["distinct_instances"]
        differences["remaining"] = reported["remaining"]

        return {
            "reported": reported,
            "observed": observed,
            "differences": differences,
            "coverage": coverage,
            "instances": instances,
            "failed_uid_lists": {
                "final": failed_list,
                "other_responses": other_lists,
            },
        }

    def summary(self) -> str:
        """Return a human-readable summary of the report."""
        data = self.report()
        reported = data["reported"]
        observed = data["observed"]
        differences = data["differences"]
        coverage = data["coverage"]

        def value(item: Any) -> str:
            return "unknown" if item is None else str(item)

        lines = ["Retrieval report"]
        if reported["response_count"]:
            source = (
                "final response"
                if reported["final_received"]
                else "last Pending response"
            )
            lines.append(
                f"Reported ({source}): {value(reported['total'])} total = "
                f"{value(reported['completed'])} completed, "
                f"{value(reported['failed'])} failed, "
                f"{value(reported['warning'])} warning, "
                f"{value(reported['remaining'])} remaining"
            )
        else:
            lines.append("Reported: no response recorded")

        lines.append(
            f"Observed: {observed['distinct_instances']} distinct C-STORE "
            f"receipt(s) = {observed['completed']} completed, "
            f"{observed['failed']} failed, {observed['warning']} warning"
            + (f", {observed['conflict']} conflict" if observed["conflict"] else "")
            + (
                f"; {observed['duplicate_receipts']} duplicate receipt(s) ignored"
                if observed["duplicate_receipts"]
                else ""
            )
        )

        if differences["total"] is not None:
            lines.append(
                f"Difference (reported - observed): {differences['total']} total; "
                f"{value(differences['completed'])} completed, "
                f"{value(differences['failed'])} failed, "
                f"{value(differences['warning'])} warning"
            )

        lines.append(
            "Coverage: "
            f"{coverage['identified_completed']} completed, "
            f"{coverage['identified_failed']} failed and "
            f"{coverage['identified_warning']} warning sub-operation(s) tied to "
            f"SOP Instance UIDs; {value(coverage['unidentified_total'])} "
            "sub-operation(s) not identifiable from the available evidence"
        )
        lines.append(
            "Failed UID list: "
            + (
                f"provided ({coverage['failed_uid_list_count']} UID(s))"
                if coverage["failed_uid_list_provided"]
                else "not provided - failed counts cannot be mapped to specific UIDs"
            )
        )
        if data["instances"]:
            lines.append("Per-instance results:")
            for row in data["instances"]:
                detail = ", ".join(row["evidence"])
                duplicate = (
                    f", {row['duplicate_receipts']} duplicate(s)"
                    if row["duplicate_receipts"]
                    else ""
                )
                lines.append(
                    f"  {row['sop_instance_uid']}: {row['result']} "
                    f"[{detail}{duplicate}]"
                )

        for note in coverage["notes"]:
            lines.append(f"Note: {note}")

        return "\n".join(lines)

    def write(self, path: str) -> None:
        """Write the report as JSON to `path`."""
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(self.report(), fp, indent=2)
            fp.write("\n")
