"""Cross-referencing of DICOM transfers and Storage Commitment results.

A *handover archive* records when a batch of SOP Instances finished
transferring and then links the later Storage Commitment outcome to that
batch.  The archive is independent of any particular association, so it can
be updated by an SCU after a C-STORE batch or by either peer when a Storage
Commitment N-EVENT-REPORT is received.

The model has three important properties:

* A transfer is identified by its (0008,1195) *Transaction UID* and its
  (0008,1199) *Referenced SOP Sequence* recorded at transfer completion.
* A commitment result is matched to a transfer using **only** the
  transaction UID.  SOP Instance UIDs are not unique across transactions, so
  a report for an unknown transaction is kept separately and is never
  attached to another batch that happens to contain the same instances.
* :attr:`TransferRecord.transferred_at` is immutable.  A commitment result
  that arrives late may change the handover status but never the recorded
  transfer time.

The archive can be used in memory or persisted as JSON.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydicom.dataset import Dataset

__all__ = [
    "CommitmentReport",
    "CommitmentReportMismatch",
    "CommitmentRequest",
    "CommitmentRequestMismatch",
    "CommitmentResult",
    "CommitmentStatus",
    "FailedSOPInstanceReference",
    "HandoverArchive",
    "HandoverEntry",
    "HandoverError",
    "HandoverLedger",
    "HandoverRecord",
    "InstanceReference",
    "InvalidCommitmentDataset",
    "SOPInstanceReference",
    "TransactionUIDConflict",
    "TransferRecord",
    "UnmatchedCommitmentReport",
    "UnmatchedCommitmentRequest",
    "failure_reason_text",
    "commitment_report_from_event",
    "commitment_request_from_event",
    "parse_commitment_report",
    "parse_commitment_request",
    "record_from_event",
]


class HandoverError(Exception):
    """Base exception for handover archive errors."""


class TransactionUIDConflict(HandoverError):
    """Raised when one transaction UID is reused with a different manifest."""


class CommitmentReportMismatch(HandoverError):
    """Raised when a report contains instances outside the transfer manifest."""


class CommitmentRequestMismatch(HandoverError):
    """Raised when a request contains instances outside the transfer manifest."""


class InvalidCommitmentDataset(HandoverError):
    """Raised when a Storage Commitment dataset cannot be interpreted."""


# Failure Reason values from DICOM PS3.4, Annex J.3.1
_FAILURE_REASONS = {
    0x0001: "Processing failure",
    0x0002: "No such object instance",
    0x0003: "Resource limitation",
    0x0004: "Referencing nonexistent SOP Instance",
    0x0005: "Referencing nonexistent SOP Class",
}


def failure_reason_text(value: int | None) -> str | None:
    """Return the DICOM description for a Storage Commitment failure reason.

    Parameters
    ----------
    value : int or None
        The (0008,1197) *Failure Reason* value.

    Returns
    -------
    str or None
        The standard description if known, otherwise a hexadecimal value.
        ``None`` is returned when `value` is ``None``.
    """
    if value is None:
        return None

    try:
        value = int(value)
    except (TypeError, ValueError):
        return str(value)

    return _FAILURE_REASONS.get(value, f"Unknown failure reason (0x{value:04X})")


def _from_iso_or_dicom(text: str) -> datetime:
    """Return a datetime from an ISO 8601 or DICOM DT string."""
    # Only use fromisoformat() when the text actually contains the ISO date
    # separators, otherwise Python may accept a DICOM date as a naive value.
    if "-" in text or "T" in text:
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            pass

    # DICOM DT: YYYYMMDDHHMMSS.FFFFFF&ZZXX
    for fmt in (
        "%Y%m%d%H%M%S.%f%z",
        "%Y%m%d%H%M%S%z",
        "%Y%m%d%H%M%S.%f",
        "%Y%m%d%H%M%S",
        "%Y%m%d",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue

    raise ValueError(f"Invalid timestamp: {text!r}")


def _as_utc(value: datetime | str | None) -> datetime:
    """Return a timezone-aware UTC datetime.

    ISO 8601 strings and DICOM DT values (``YYYYMMDDHHMMSS`` with an optional
    fractional part and UTC offset) are accepted.
    """
    if value is None:
        return datetime.now(tz=timezone.utc)

    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"

        value = _from_iso_or_dicom(text)

    if not isinstance(value, datetime):
        raise TypeError("A datetime, ISO 8601 string or None is required")

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    """Return an ISO 8601 string for `value`."""
    if value is None:
        return None

    return _as_utc(value).isoformat()


def _text(value: Any, name: str) -> str:
    """Return a validated non-empty text value."""
    if value is None:
        raise ValueError(f"'{name}' must not be empty")

    result = str(value)
    if not result:
        raise ValueError(f"'{name}' must not be empty")

    return result


def _sequence(value: Any) -> list[Any]:
    """Return `value` as a list of sequence items."""
    if value is None:
        return []

    if isinstance(value, (str, bytes, bytearray)):
        raise InvalidCommitmentDataset("Expected a sequence of dataset items")

    if isinstance(value, Sequence):
        return list(value)

    return [value]


def _dataset_value(dataset: Any, *keywords: str) -> Any:
    """Return the first available keyword value from `dataset`."""
    for keyword in keywords:
        if isinstance(dataset, Mapping):
            value = dataset.get(keyword)
            if value is not None:
                return value
            continue

        try:
            return getattr(dataset, keyword)
        except (AttributeError, KeyError):
            continue

    return None


def _dedupe(values: Iterable[Any]) -> list[Any]:
    """Return `values` with duplicate instance references removed."""
    result: list[Any] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        item = (
            value
            if isinstance(value, (SOPInstanceReference, FailedSOPInstanceReference))
            else _coerce_instance(value)
        )
        if item.key in seen:
            continue

        seen.add(item.key)
        result.append(item)

    return result


@dataclass(frozen=True, order=True)
class SOPInstanceReference:
    """A reference to a SOP Instance in a transfer or commitment result."""

    sop_class_uid: str
    sop_instance_uid: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "sop_class_uid", _text(self.sop_class_uid, "sop_class_uid")
        )
        object.__setattr__(
            self, "sop_instance_uid", _text(self.sop_instance_uid, "sop_instance_uid")
        )

    @property
    def key(self) -> tuple[str, str]:
        """Return the (SOP Class UID, SOP Instance UID) pair."""
        return self.sop_class_uid, self.sop_instance_uid

    @classmethod
    def from_dataset(cls, item: Dataset) -> "SOPInstanceReference":
        """Create a reference from a *Referenced SOP Sequence* item."""
        class_uid = _dataset_value(item, "ReferencedSOPClassUID", "SOPClassUID")
        instance_uid = _dataset_value(
            item, "ReferencedSOPInstanceUID", "SOPInstanceUID"
        )
        if class_uid is None or instance_uid is None:
            raise InvalidCommitmentDataset(
                "A referenced SOP Instance requires both a SOP Class UID and "
                "a SOP Instance UID"
            )

        return cls(str(class_uid), str(instance_uid))

    def as_dict(self) -> dict[str, str]:
        """Return the reference as a JSON-compatible dictionary."""
        return {
            "sop_class_uid": self.sop_class_uid,
            "sop_instance_uid": self.sop_instance_uid,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SOPInstanceReference":
        """Create a reference from a dictionary."""
        return cls(
            _text(
                value.get("sop_class_uid", value.get("sop_class")),
                "sop_class_uid",
            ),
            _text(
                value.get("sop_instance_uid", value.get("sop_instance")),
                "sop_instance_uid",
            ),
        )


@dataclass(frozen=True)
class FailedSOPInstanceReference(SOPInstanceReference):
    """A SOP Instance that failed Storage Commitment.

    Failed instances are kept in a separate collection from committed
    instances so a partial failure remains visible in the archive.
    """

    failure_reason: int | None = None
    failure_reason_text: str | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        reason = self.failure_reason
        if reason is not None:
            try:
                reason = int(reason)
            except (TypeError, ValueError):
                reason = None
            object.__setattr__(self, "failure_reason", reason)

        if self.failure_reason_text is None and reason is not None:
            object.__setattr__(
                self, "failure_reason_text", failure_reason_text(reason)
            )

    @classmethod
    def from_dataset(cls, item: Dataset) -> "FailedSOPInstanceReference":
        """Create a failed reference from a *Failed SOP Sequence* item."""
        reference = SOPInstanceReference.from_dataset(item)
        reason = _dataset_value(item, "FailureReason")
        try:
            reason = int(reason) if reason is not None else None
        except (TypeError, ValueError):
            reason = None

        return cls(
            reference.sop_class_uid,
            reference.sop_instance_uid,
            failure_reason=reason,
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the failed reference as a JSON-compatible dictionary."""
        value = super().as_dict()
        value["failure_reason"] = self.failure_reason
        value["failure_reason_text"] = self.failure_reason_text
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FailedSOPInstanceReference":
        """Create a failed reference from a dictionary."""
        reason = value.get("failure_reason")
        try:
            reason = int(reason) if reason is not None else None
        except (TypeError, ValueError):
            reason = None

        return cls(
            _text(
                value.get("sop_class_uid", value.get("sop_class")),
                "sop_class_uid",
            ),
            _text(
                value.get("sop_instance_uid", value.get("sop_instance")),
                "sop_instance_uid",
            ),
            failure_reason=reason,
            failure_reason_text=value.get("failure_reason_text"),
        )


@dataclass(frozen=True)
class CommitmentRequest:
    """A Storage Commitment request for a completed transfer."""

    transaction_uid: str
    instances: tuple[SOPInstanceReference, ...]
    requested_at: datetime | None = None
    action_type_id: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "transaction_uid", _text(self.transaction_uid, "transaction_uid")
        )
        object.__setattr__(self, "instances", tuple(_dedupe(self.instances)))
        if self.requested_at is not None:
            object.__setattr__(self, "requested_at", _as_utc(self.requested_at))

    @property
    def instance_manifest(self) -> tuple[SOPInstanceReference, ...]:
        """Return the request's instance manifest."""
        return self.instances

    def as_dict(self) -> dict[str, Any]:
        """Return the request as a JSON-compatible dictionary."""
        return {
            "transaction_uid": self.transaction_uid,
            "instance_manifest": [item.as_dict() for item in self.instances],
            "requested_at": _iso(self.requested_at),
            "action_type_id": self.action_type_id,
        }


@dataclass(frozen=True)
class CommitmentReport:
    """A Storage Commitment result received for a transaction."""

    transaction_uid: str
    committed_instances: tuple[SOPInstanceReference, ...] = ()
    failed_instances: tuple[FailedSOPInstanceReference, ...] = ()
    reported_at: datetime | None = None
    event_type_id: int | None = None
    retrieve_ae_title: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "transaction_uid", _text(self.transaction_uid, "transaction_uid")
        )
        committed: list[SOPInstanceReference] = []
        for item in self.committed_instances:
            if isinstance(item, FailedSOPInstanceReference):
                committed.append(
                    SOPInstanceReference(item.sop_class_uid, item.sop_instance_uid)
                )
            else:
                committed.append(_coerce_instance(item))

        failed: list[FailedSOPInstanceReference] = []
        for item in self.failed_instances:
            if isinstance(item, FailedSOPInstanceReference):
                failed.append(item)
            elif isinstance(item, SOPInstanceReference):
                failed.append(
                    FailedSOPInstanceReference(
                        item.sop_class_uid, item.sop_instance_uid
                    )
                )
            else:
                failed.append(_coerce_failed_instance(item))

        object.__setattr__(self, "committed_instances", tuple(_dedupe(committed)))
        object.__setattr__(self, "failed_instances", tuple(_dedupe(failed)))
        if self.reported_at is not None:
            object.__setattr__(self, "reported_at", _as_utc(self.reported_at))

        committed = {item.key for item in self.committed_instances}
        failed = {item.key for item in self.failed_instances}
        overlap = committed & failed
        if overlap:
            refs = ", ".join(f"{cls}/{instance}" for cls, instance in sorted(overlap))
            raise ValueError(
                "The same SOP Instance cannot be both committed and failed: "
                f"{refs}"
            )

    @property
    def committed_count(self) -> int:
        """Return the number of committed instances."""
        return len(self.committed_instances)

    @property
    def failed_count(self) -> int:
        """Return the number of failed instances."""
        return len(self.failed_instances)

    @property
    def has_failures(self) -> bool:
        """Return ``True`` if at least one instance failed commitment."""
        return bool(self.failed_instances)

    def as_dict(self) -> dict[str, Any]:
        """Return the report as a JSON-compatible dictionary."""
        return {
            "transaction_uid": self.transaction_uid,
            "committed_instances": [
                item.as_dict() for item in self.committed_instances
            ],
            "failed_instances": [item.as_dict() for item in self.failed_instances],
            "reported_at": _iso(self.reported_at),
            "event_type_id": self.event_type_id,
            "retrieve_ae_title": self.retrieve_ae_title,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CommitmentReport":
        """Create a report from a dictionary."""
        return cls(
            transaction_uid=_text(value.get("transaction_uid"), "transaction_uid"),
            committed_instances=tuple(
                SOPInstanceReference.from_dict(item)
                for item in value.get("committed_instances", [])
            ),
            failed_instances=tuple(
                FailedSOPInstanceReference.from_dict(item)
                for item in value.get("failed_instances", [])
            ),
            reported_at=(
                _as_utc(value["reported_at"]) if value.get("reported_at") else None
            ),
            event_type_id=value.get("event_type_id"),
            retrieve_ae_title=value.get("retrieve_ae_title"),
        )


class CommitmentStatus(str, Enum):
    """The handover status of a transfer."""

    TRANSFERRED = "transferred"
    COMMITMENT_REQUESTED = "commitment_requested"
    COMMITTED = "committed"
    PARTIALLY_COMMITTED = "partially_committed"
    COMMITMENT_FAILED = "commitment_failed"
    COMMITMENT_INCOMPLETE = "commitment_incomplete"

    PARTIAL_FAILURE = "partially_committed"
    FAILED = "commitment_failed"
    PENDING = "commitment_requested"
    AWAITING_COMMITMENT = "transferred"

    def __str__(self) -> str:
        return self.value

    @property
    def is_final(self) -> bool:
        """Return ``True`` when every manifest instance has an outcome."""
        return self in (
            CommitmentStatus.COMMITTED,
            CommitmentStatus.PARTIALLY_COMMITTED,
            CommitmentStatus.COMMITMENT_FAILED,
        )


@dataclass(frozen=True)
class TransferRecord:
    """A completed transfer and its Storage Commitment follow-up."""

    transaction_uid: str
    transferred_at: datetime
    instances: tuple[SOPInstanceReference, ...]
    source_ae: str | None = None
    destination_ae: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    commitment_requested_at: datetime | None = None
    commitment: CommitmentReport | None = None
    commitment_history: tuple[CommitmentReport, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "transaction_uid", _text(self.transaction_uid, "transaction_uid")
        )
        object.__setattr__(self, "transferred_at", _as_utc(self.transferred_at))
        object.__setattr__(self, "instances", tuple(_dedupe(self.instances)))
        if self.commitment_requested_at is not None:
            object.__setattr__(
                self,
                "commitment_requested_at",
                _as_utc(self.commitment_requested_at),
            )
        object.__setattr__(self, "metadata", dict(self.metadata or {}))
        object.__setattr__(self, "commitment_history", tuple(self.commitment_history))

    @property
    def instance_manifest(self) -> tuple[SOPInstanceReference, ...]:
        """Return the transfer's instance manifest."""
        return self.instances

    @property
    def committed_instances(self) -> tuple[SOPInstanceReference, ...]:
        """Return the instances committed by the latest result."""
        if self.commitment is None:
            return ()

        return self.commitment.committed_instances

    @property
    def failed_instances(self) -> tuple[FailedSOPInstanceReference, ...]:
        """Return the instances that failed the latest result."""
        if self.commitment is None:
            return ()

        return self.commitment.failed_instances

    @property
    def status(self) -> CommitmentStatus:
        """Return the current handover status."""
        if self.commitment is None:
            if self.commitment_requested_at is not None:
                return CommitmentStatus.COMMITMENT_REQUESTED

            return CommitmentStatus.TRANSFERRED

        manifest = {item.key for item in self.instances}
        committed = {item.key for item in self.commitment.committed_instances}
        failed = {item.key for item in self.commitment.failed_instances}
        reported = committed | failed

        if not manifest <= reported:
            return CommitmentStatus.COMMITMENT_INCOMPLETE

        if failed and committed:
            return CommitmentStatus.PARTIALLY_COMMITTED

        if failed:
            return CommitmentStatus.COMMITMENT_FAILED

        return CommitmentStatus.COMMITTED

    @property
    def handover_status(self) -> CommitmentStatus:
        """Return the current handover status (alias for :attr:`status`)."""
        return self.status

    @property
    def unresolved_instances(self) -> tuple[SOPInstanceReference, ...]:
        """Return manifest instances with no committed/failed outcome yet."""
        if self.commitment is None:
            return self.instances

        reported = {item.key for item in self.commitment.committed_instances}
        reported.update(item.key for item in self.commitment.failed_instances)
        return tuple(item for item in self.instances if item.key not in reported)

    @property
    def is_complete(self) -> bool:
        """Return ``True`` if every manifest instance has a final outcome."""
        if self.commitment is None:
            return False

        return not self.unresolved_instances

    def as_dict(self) -> dict[str, Any]:
        """Return the record as a JSON-compatible dictionary."""
        return {
            "transaction_uid": self.transaction_uid,
            "transferred_at": _iso(self.transferred_at),
            "source_ae": self.source_ae,
            "destination_ae": self.destination_ae,
            "instance_manifest": [item.as_dict() for item in self.instances],
            "instances": [item.as_dict() for item in self.instances],
            "metadata": dict(self.metadata),
            "commitment_requested_at": _iso(self.commitment_requested_at),
            "commitment_result": (
                self.commitment.as_dict() if self.commitment is not None else None
            ),
            "commitment": (
                self.commitment.as_dict() if self.commitment is not None else None
            ),
            "commitment_history": [
                item.as_dict() for item in self.commitment_history
            ],
            "committed_instances": [
                item.as_dict() for item in self.committed_instances
            ],
            "failed_instances": [item.as_dict() for item in self.failed_instances],
            "handover_status": self.status.value,
            "status": self.status.value,
            "is_complete": self.is_complete,
            "unresolved_instances": [
                item.as_dict() for item in self.unresolved_instances
            ],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TransferRecord":
        """Create a record from a dictionary."""
        manifest = value.get("instance_manifest", value.get("instances", []))
        commitment = value.get("commitment_result", value.get("commitment"))
        transferred_at = value.get("transferred_at")
        if transferred_at is None:
            raise ValueError(
                "A transfer record requires a 'transferred_at' timestamp"
            )

        return cls(
            transaction_uid=_text(value.get("transaction_uid"), "transaction_uid"),
            transferred_at=_as_utc(transferred_at),
            instances=tuple(
                SOPInstanceReference.from_dict(item) for item in manifest
            ),
            source_ae=value.get("source_ae"),
            destination_ae=value.get("destination_ae"),
            metadata=value.get("metadata") or {},
            commitment_requested_at=(
                _as_utc(value["commitment_requested_at"])
                if value.get("commitment_requested_at")
                else None
            ),
            commitment=CommitmentReport.from_dict(commitment) if commitment else None,
            commitment_history=tuple(
                CommitmentReport.from_dict(item)
                for item in value.get("commitment_history", [])
            ),
        )


@dataclass(frozen=True)
class UnmatchedCommitmentReport:
    """A commitment result for a transaction that is not in the archive."""

    report: CommitmentReport
    received_at: datetime
    reason: str = "unknown_transaction"

    @property
    def transaction_uid(self) -> str:
        """Return the unknown transaction UID."""
        return self.report.transaction_uid

    @property
    def committed_instances(self) -> tuple[SOPInstanceReference, ...]:
        """Return the result's committed instances."""
        return self.report.committed_instances

    @property
    def failed_instances(self) -> tuple[FailedSOPInstanceReference, ...]:
        """Return the result's failed instances."""
        return self.report.failed_instances

    def as_dict(self) -> dict[str, Any]:
        """Return the unmatched result as a JSON-compatible dictionary."""
        return {
            "report": self.report.as_dict(),
            "received_at": _iso(self.received_at),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "UnmatchedCommitmentReport":
        """Create an unmatched result from a dictionary."""
        return cls(
            report=CommitmentReport.from_dict(value["report"]),
            received_at=_as_utc(value.get("received_at")),
            reason=value.get("reason", "unknown_transaction"),
        )


@dataclass(frozen=True)
class UnmatchedCommitmentRequest:
    """A commitment request for a transaction that is not in the archive."""

    request: CommitmentRequest
    received_at: datetime
    reason: str = "unknown_transaction"

    @property
    def transaction_uid(self) -> str:
        """Return the unknown transaction UID."""
        return self.request.transaction_uid

    @property
    def instances(self) -> tuple[SOPInstanceReference, ...]:
        """Return the request's instance manifest."""
        return self.request.instances

    def as_dict(self) -> dict[str, Any]:
        """Return the unmatched request as a JSON-compatible dictionary."""
        return {
            "request": self.request.as_dict(),
            "received_at": _iso(self.received_at),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "UnmatchedCommitmentRequest":
        """Create an unmatched request from a dictionary."""
        request = value["request"]
        return cls(
            request=CommitmentRequest(
                transaction_uid=_text(
                    request.get("transaction_uid"), "transaction_uid"
                ),
                instances=tuple(
                    SOPInstanceReference.from_dict(item)
                    for item in request.get("instance_manifest", [])
                ),
                requested_at=(
                    _as_utc(request["requested_at"])
                    if request.get("requested_at")
                    else None
                ),
                action_type_id=request.get("action_type_id"),
            ),
            received_at=_as_utc(value.get("received_at")),
            reason=value.get("reason", "unknown_transaction"),
        )


def _coerce_instance(value: Any) -> SOPInstanceReference:
    """Return `value` as a :class:`SOPInstanceReference`."""
    if isinstance(value, FailedSOPInstanceReference):
        return SOPInstanceReference(value.sop_class_uid, value.sop_instance_uid)

    if isinstance(value, SOPInstanceReference):
        return value

    if isinstance(value, Dataset):
        return SOPInstanceReference.from_dataset(value)

    if isinstance(value, Mapping):
        return SOPInstanceReference.from_dict(value)

    if isinstance(value, (list, tuple)) and len(value) == 2:
        return SOPInstanceReference(value[0], value[1])

    raise TypeError(
        "SOP instance references must be SOPInstanceReference, Dataset, "
        "mapping or a (SOP Class UID, SOP Instance UID) pair"
    )


def _coerce_failed_instance(value: Any) -> FailedSOPInstanceReference:
    """Return `value` as a :class:`FailedSOPInstanceReference`."""
    if isinstance(value, FailedSOPInstanceReference):
        return value

    if isinstance(value, SOPInstanceReference):
        return FailedSOPInstanceReference(
            value.sop_class_uid, value.sop_instance_uid
        )

    if isinstance(value, Dataset):
        return FailedSOPInstanceReference.from_dataset(value)

    if isinstance(value, Mapping):
        if "sop_class_uid" in value or "sop_class" in value:
            return FailedSOPInstanceReference.from_dict(value)

        return FailedSOPInstanceReference.from_dataset(value)  # type: ignore[arg-type]

    if isinstance(value, (list, tuple)):
        if len(value) == 2:
            return FailedSOPInstanceReference(value[0], value[1])

        if len(value) == 3:
            return FailedSOPInstanceReference(
                value[0], value[1], failure_reason=value[2]
            )

        if len(value) == 4:
            return FailedSOPInstanceReference(
                value[0],
                value[1],
                failure_reason=value[2],
                failure_reason_text=value[3],
            )

    raise TypeError(
        "Failed SOP instance references must be FailedSOPInstanceReference, "
        "SOPInstanceReference, Dataset, mapping or a (SOP Class UID, "
        "SOP Instance UID[, Failure Reason]) tuple"
    )


def parse_commitment_request(
    dataset: Dataset,
    *,
    requested_at: datetime | str | None = None,
    action_type_id: int | None = None,
) -> CommitmentRequest:
    """Return a :class:`CommitmentRequest` from an N-ACTION dataset.

    Parameters
    ----------
    dataset : pydicom.dataset.Dataset
        The N-ACTION *Action Information* dataset.
    requested_at : datetime or str, optional
        The time the request was received.  If omitted the current UTC time
        is used.
    action_type_id : int, optional
        The N-ACTION *Action Type ID*.

    Returns
    -------
    CommitmentRequest
        The parsed request.
    """
    if not isinstance(dataset, Dataset):
        raise TypeError("'dataset' must be a pydicom Dataset")

    transaction_uid = _dataset_value(dataset, "TransactionUID")
    if transaction_uid is None:
        raise InvalidCommitmentDataset(
            "The Storage Commitment dataset has no (0008,1195) Transaction UID"
        )

    instances = [
        SOPInstanceReference.from_dataset(item)
        for item in _sequence(_dataset_value(dataset, "ReferencedSOPSequence"))
    ]
    if not instances:
        raise InvalidCommitmentDataset(
            "The Storage Commitment request has no referenced SOP instances"
        )

    return CommitmentRequest(
        transaction_uid=str(transaction_uid),
        instances=tuple(instances),
        requested_at=_as_utc(requested_at),
        action_type_id=action_type_id,
    )


def parse_commitment_report(
    dataset: Dataset,
    *,
    reported_at: datetime | str | None = None,
    event_type_id: int | None = None,
) -> CommitmentReport:
    """Return a :class:`CommitmentReport` from an N-EVENT-REPORT dataset.

    Committed instances are read from (0008,1199) *Referenced SOP Sequence*
    and failed instances are kept separate from (0008,1198) *Failed SOP
    Sequence*.  A result with no sequences is accepted as an empty result.
    """
    if not isinstance(dataset, Dataset):
        raise TypeError("'dataset' must be a pydicom Dataset")

    transaction_uid = _dataset_value(dataset, "TransactionUID")
    if transaction_uid is None:
        raise InvalidCommitmentDataset(
            "The Storage Commitment dataset has no (0008,1195) Transaction UID"
        )

    committed = [
        SOPInstanceReference.from_dataset(item)
        for item in _sequence(_dataset_value(dataset, "ReferencedSOPSequence"))
    ]
    failed = [
        FailedSOPInstanceReference.from_dataset(item)
        for item in _sequence(_dataset_value(dataset, "FailedSOPSequence"))
    ]

    return CommitmentReport(
        transaction_uid=str(transaction_uid),
        committed_instances=tuple(committed),
        failed_instances=tuple(failed),
        reported_at=_as_utc(reported_at),
        event_type_id=event_type_id,
        retrieve_ae_title=_dataset_value(dataset, "RetrieveAETitle"),
    )


class HandoverArchive:
    """A JSON-persistable archive of transfer and commitment records.

    Parameters
    ----------
    path : str or pathlib.Path, optional
        If supplied, the archive is loaded from `path` when it exists and is
        saved to `path` after each change (unless `autosave` is ``False``).
    autosave : bool, optional
        Save after every mutation when a path is configured (default
        ``True``).
    """

    VERSION = 1

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        autosave: bool = True,
    ) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, TransferRecord] = {}
        self._unmatched_reports: list[UnmatchedCommitmentReport] = []
        self._unmatched_requests: list[UnmatchedCommitmentRequest] = []
        self.path = Path(path) if path is not None else None
        self.autosave = autosave

        if self.path is not None and self.path.exists():
            self.load(self.path)

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(records={len(self._records)}, "
            f"unmatched_reports={len(self._unmatched_reports)})"
        )

    def __contains__(self, transaction_uid: object) -> bool:
        with self._lock:
            return str(transaction_uid) in self._records

    def __iter__(self):
        with self._lock:
            return iter(tuple(self._records.values()))

    def __getitem__(self, transaction_uid: str) -> TransferRecord:
        with self._lock:
            return self._records[transaction_uid]

    @property
    def records(self) -> tuple[TransferRecord, ...]:
        """Return all transfer records in insertion order."""
        with self._lock:
            return tuple(self._records.values())

    @property
    def transfers(self) -> tuple[TransferRecord, ...]:
        """Return all transfer records (alias for :attr:`records`)."""
        return self.records

    @property
    def unmatched_reports(self) -> tuple[UnmatchedCommitmentReport, ...]:
        """Return results for transactions that are not in the archive."""
        with self._lock:
            return tuple(self._unmatched_reports)

    @property
    def unmatched_commitment_reports(
        self,
    ) -> tuple[UnmatchedCommitmentReport, ...]:
        """Alias for :attr:`unmatched_reports`."""
        return self.unmatched_reports

    @property
    def unmatched_requests(self) -> tuple[UnmatchedCommitmentRequest, ...]:
        """Return requests for transactions that are not in the archive."""
        with self._lock:
            return tuple(self._unmatched_requests)

    def get(self, transaction_uid: str) -> TransferRecord | None:
        """Return the record for `transaction_uid`, or ``None``."""
        with self._lock:
            return self._records.get(str(transaction_uid))

    def find(self, transaction_uid: str) -> TransferRecord | None:
        """Alias for :meth:`get`."""
        return self.get(transaction_uid)

    def upsert(
        self,
        record: TransferRecord | Mapping[str, Any],
    ) -> TransferRecord:
        """Insert or update a transfer record.

        This is a convenience method for callers that already have a
        :class:`TransferRecord` (for example, after loading an external
        manifest).  It applies the same transaction UID and manifest rules
        as :meth:`record_transfer`.
        """
        if isinstance(record, Mapping):
            record = TransferRecord.from_dict(record)

        if not isinstance(record, TransferRecord):
            raise TypeError("'record' must be a TransferRecord or mapping")

        result = self.record_transfer(
            record.transaction_uid,
            record.instances,
            transferred_at=record.transferred_at,
            source_ae=record.source_ae,
            destination_ae=record.destination_ae,
            metadata=record.metadata,
        )
        if record.commitment is not None:
            result = self.record_commitment_report(record.commitment)

        return result

    def records_for_instance(
        self, sop_instance_uid: str
    ) -> tuple[TransferRecord, ...]:
        """Return records whose manifest contains `sop_instance_uid`.

        This method is for inspection only.  SOP Instance UIDs are not unique
        across transactions, so it must not be used to match a commitment
        result to a transfer.
        """
        with self._lock:
            return tuple(
                record
                for record in self._records.values()
                if any(
                    item.sop_instance_uid == str(sop_instance_uid)
                    for item in record.instances
                )
            )

    def status(self, transaction_uid: str) -> CommitmentStatus | None:
        """Return the handover status for `transaction_uid`, or ``None``."""
        record = self.get(transaction_uid)
        return record.status if record is not None else None

    def record_transfer(
        self,
        transaction_uid: str,
        instances: Iterable[Any],
        *,
        transferred_at: datetime | str | None = None,
        source_ae: str | None = None,
        destination_ae: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        replace_existing: bool = False,
    ) -> TransferRecord:
        """Record a completed transfer.

        Repeating the same transaction UID with the same manifest is
        idempotent: the original record, including its original
        ``transferred_at``, is returned unchanged.  Reusing a transaction UID
        with a different manifest raises :class:`TransactionUIDConflict`.

        Set `replace_existing` to ``True`` only when correcting a previously
        recorded manifest.  The original ``transferred_at`` is still kept.
        """
        transaction_uid = _text(transaction_uid, "transaction_uid")
        manifest = tuple(_dedupe(instances))
        if not manifest:
            raise ValueError("A transfer must contain at least one SOP Instance")

        new_record = TransferRecord(
            transaction_uid=transaction_uid,
            transferred_at=_as_utc(transferred_at),
            instances=manifest,
            source_ae=source_ae,
            destination_ae=destination_ae,
            metadata=metadata or {},
        )

        with self._lock:
            existing = self._records.get(transaction_uid)
            if existing is not None:
                if existing.instances != new_record.instances:
                    if not replace_existing:
                        raise TransactionUIDConflict(
                            f"Transaction UID '{transaction_uid}' is already "
                            "associated with a different instance manifest"
                        )

                    existing = replace(existing, instances=manifest)
                    self._records[transaction_uid] = existing
                    self._maybe_save()

                # A repeated completion for the same manifest is the same
                # transaction; never rewrite the original transfer time.
                return existing

            self._records[transaction_uid] = new_record
            self._maybe_save()

            # A report may have arrived before its transfer was registered.
            if any(
                item.transaction_uid == transaction_uid
                for item in self._unmatched_reports
            ):
                return self.reconcile(transaction_uid) or new_record

            return new_record

    add_transfer = record_transfer
    register_transfer = record_transfer

    def record_transfer_dataset(
        self,
        dataset: Dataset,
        *,
        transferred_at: datetime | str | None = None,
        source_ae: str | None = None,
        destination_ae: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> TransferRecord:
        """Record a transfer from a Storage Commitment N-ACTION dataset.

        The dataset supplies both the (0008,1195) *Transaction UID* and the
        (0008,1199) *Referenced SOP Sequence* instance manifest.
        """
        request = parse_commitment_request(dataset, requested_at=transferred_at)
        return self.record_transfer(
            request.transaction_uid,
            request.instances,
            transferred_at=transferred_at,
            source_ae=source_ae,
            destination_ae=destination_ae,
            metadata=metadata,
        )

    add_transfer_dataset = record_transfer_dataset

    def record_commitment_request(
        self,
        request: CommitmentRequest | str,
        instances: Iterable[Any] | None = None,
        *,
        requested_at: datetime | str | None = None,
    ) -> TransferRecord | None:
        """Record a Storage Commitment request for a known transfer.

        A request for an unknown transaction is retained in
        :attr:`unmatched_requests` and is never attached to another record.
        """
        if isinstance(request, CommitmentRequest):
            commitment_request = request
        else:
            if instances is None:
                raise ValueError("'instances' is required when 'request' is a UID")

            commitment_request = CommitmentRequest(
                transaction_uid=_text(request, "transaction_uid"),
                instances=tuple(_coerce_instance(item) for item in instances),
                requested_at=_as_utc(requested_at),
            )

        received_at = commitment_request.requested_at or _as_utc(requested_at)
        with self._lock:
            record = self._records.get(commitment_request.transaction_uid)
            if record is None:
                self._unmatched_requests.append(
                    UnmatchedCommitmentRequest(
                        request=commitment_request,
                        received_at=received_at,
                    )
                )
                self._maybe_save()
                return None

            self._validate_references(
                record,
                commitment_request.instances,
                CommitmentRequestMismatch,
                "request",
            )

            if record.commitment_requested_at is None:
                record = replace(
                    record,
                    commitment_requested_at=(
                        commitment_request.requested_at or received_at
                    ),
                )
                self._records[record.transaction_uid] = record
                self._maybe_save()

            return record

    add_commitment_request = record_commitment_request

    def record_commitment_request_dataset(
        self,
        dataset: Dataset,
        *,
        requested_at: datetime | str | None = None,
        action_type_id: int | None = None,
    ) -> TransferRecord | None:
        """Parse an N-ACTION *Action Information* and record the request.

        This is a convenience wrapper around :func:`parse_commitment_request`
        and :meth:`record_commitment_request`.
        """
        request = parse_commitment_request(
            dataset,
            requested_at=requested_at,
            action_type_id=action_type_id,
        )
        return self.record_commitment_request(request)

    def record_commitment_report_dataset(
        self,
        dataset: Dataset,
        *,
        reported_at: datetime | str | None = None,
        event_type_id: int | None = None,
    ) -> TransferRecord | None:
        """Parse an N-EVENT-REPORT *Event Information* and record the result.

        This is a convenience wrapper around :func:`parse_commitment_report`
        and :meth:`record_commitment_report`.
        """
        report = parse_commitment_report(
            dataset,
            reported_at=reported_at,
            event_type_id=event_type_id,
        )
        return self.record_commitment_report(report)

    def record_commitment_report(
        self,
        report: CommitmentReport | str,
        committed_instances: Iterable[Any] | None = None,
        failed_instances: Iterable[Any] | None = None,
        *,
        reported_at: datetime | str | None = None,
        event_type_id: int | None = None,
        retrieve_ae_title: str | None = None,
    ) -> TransferRecord | None:
        """Record a commitment result for a known transfer.

        Matching uses only the transaction UID.  A result for an unknown
        transaction is stored in :attr:`unmatched_reports` and is never
        attached to another record, even if its SOP Instance UIDs occur in
        another transfer's manifest.

        A later result replaces the previous one and may change the handover
        status.  The transfer's original ``transferred_at`` is not changed.
        """
        if isinstance(report, CommitmentReport):
            commitment_report = report
        else:
            commitment_report = CommitmentReport(
                transaction_uid=_text(report, "transaction_uid"),
                committed_instances=tuple(
                    _coerce_instance(item) for item in (committed_instances or ())
                ),
                failed_instances=tuple(
                    _coerce_failed_instance(item)
                    for item in (failed_instances or ())
                ),
                reported_at=_as_utc(reported_at),
                event_type_id=event_type_id,
                retrieve_ae_title=retrieve_ae_title,
            )

        received_at = commitment_report.reported_at or _as_utc(reported_at)
        with self._lock:
            record = self._records.get(commitment_report.transaction_uid)
            if record is None:
                self._unmatched_reports.append(
                    UnmatchedCommitmentReport(
                        report=commitment_report,
                        received_at=received_at,
                    )
                )
                self._maybe_save()
                return None

            self._validate_references(
                record,
                commitment_report.committed_instances,
                CommitmentReportMismatch,
                "report",
            )
            self._validate_references(
                record,
                commitment_report.failed_instances,
                CommitmentReportMismatch,
                "report",
            )

            record = replace(
                record,
                commitment=commitment_report,
                commitment_history=record.commitment_history + (commitment_report,),
            )
            self._records[record.transaction_uid] = record
            self._maybe_save()
            return record

    add_commitment_report = record_commitment_report
    apply_commitment_report = record_commitment_report

    def reconcile(self, transaction_uid: str) -> TransferRecord | None:
        """Attach a previously unmatched result to `transaction_uid`.

        A report may arrive before the transfer it refers to has been
        registered.  Reconciliation matches that report using its transaction
        UID only; a report for a different transaction is never borrowed, even
        if the SOP Instance UIDs overlap.

        Returns
        -------
        TransferRecord or None
            The updated record, or ``None`` if there is no record or no
            matching report.
        """
        transaction_uid = str(transaction_uid)
        with self._lock:
            record = self._records.get(transaction_uid)
            if record is None:
                return None

            matched = [
                item
                for item in self._unmatched_reports
                if item.transaction_uid == transaction_uid
            ]
            if not matched:
                return None

            # The latest report for this transaction wins, but every report
            # still has to belong to the transfer manifest.
            for item in matched:
                self._validate_references(
                    record,
                    item.committed_instances,
                    CommitmentReportMismatch,
                    "report",
                )
                self._validate_references(
                    record,
                    item.failed_instances,
                    CommitmentReportMismatch,
                    "report",
                )

            report = max(
                (item.report for item in matched),
                key=lambda item: item.reported_at or datetime.min.replace(
                    tzinfo=timezone.utc
                ),
            )
            self._unmatched_reports = [
                item
                for item in self._unmatched_reports
                if item.transaction_uid != transaction_uid
            ]
            record = replace(
                record,
                commitment=report,
                commitment_history=record.commitment_history + (report,),
            )
            self._records[transaction_uid] = record
            self._maybe_save()
            return record

    def reconcile_all(self) -> list[TransferRecord]:
        """Attach all unmatched results to their matching transfers."""
        with self._lock:
            transaction_uids = sorted(
                {
                    item.transaction_uid
                    for item in self._unmatched_reports
                    if item.transaction_uid in self._records
                }
            )

        result = []
        for transaction_uid in transaction_uids:
            record = self.reconcile(transaction_uid)
            if record is not None:
                result.append(record)

        return result

    def _validate_references(
        self,
        record: TransferRecord,
        instances: Iterable[Any],
        error_type: type[HandoverError],
        kind: str,
    ) -> None:
        """Validate that all references belong to `record`'s manifest."""
        manifest = {item.key for item in record.instances}
        unknown = sorted(item.key for item in instances if item.key not in manifest)
        if unknown:
            refs = ", ".join(f"{cls}/{instance}" for cls, instance in unknown)
            raise error_type(
                f"The commitment {kind} for transaction "
                f"'{record.transaction_uid}' references SOP Instances that "
                f"are not in the transfer manifest: {refs}"
            )

    def to_dict(self) -> dict[str, Any]:
        """Return the archive as a JSON-compatible dictionary."""
        with self._lock:
            return {
                "version": self.VERSION,
                "records": [record.as_dict() for record in self._records.values()],
                "unmatched_reports": [
                    item.as_dict() for item in self._unmatched_reports
                ],
                "unmatched_requests": [
                    item.as_dict() for item in self._unmatched_requests
                ],
            }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "HandoverArchive":
        """Create an archive from a dictionary."""
        archive = cls()
        records = value.get("records", value.get("transfers", []))
        archive._records = {
            record.transaction_uid: record
            for record in (TransferRecord.from_dict(item) for item in records)
        }
        archive._unmatched_reports = [
            UnmatchedCommitmentReport.from_dict(item)
            for item in value.get(
                "unmatched_reports", value.get("unmatched_commitment_reports", [])
            )
        ]
        archive._unmatched_requests = [
            UnmatchedCommitmentRequest.from_dict(item)
            for item in value.get("unmatched_requests", [])
        ]
        return archive

    def to_json(self, *, indent: int | None = 2) -> str:
        """Return the archive as a JSON string."""
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_json(cls, value: str | bytes) -> "HandoverArchive":
        """Create an archive from a JSON string."""
        return cls.from_dict(json.loads(value))

    def save(self, path: str | os.PathLike[str] | None = None) -> Path:
        """Save the archive to `path` (or the configured path).

        The write is atomic: the data is written to a temporary file in the
        destination directory and then renamed over the target.
        """
        destination = Path(path) if path is not None else self.path
        if destination is None:
            raise ValueError("No archive path has been configured")

        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

        fd, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=os.fspath(destination.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")

            os.replace(temp_name, destination)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

        with self._lock:
            self.path = destination

        return destination

    def load(self, path: str | os.PathLike[str] | None = None) -> None:
        """Load the archive from `path` (or the configured path)."""
        source = Path(path) if path is not None else self.path
        if source is None:
            raise ValueError("No archive path has been configured")

        with source.open("r", encoding="utf-8") as handle:
            value = json.load(handle)

        loaded = type(self).from_dict(value)
        with self._lock:
            self._records = loaded._records
            self._unmatched_reports = loaded._unmatched_reports
            self._unmatched_requests = loaded._unmatched_requests
            self.path = source

    def _maybe_save(self) -> None:
        """Save the archive if a path and autosave are configured."""
        if self.path is not None and self.autosave:
            self.save()

    def clear(self) -> None:
        """Remove all records and unmatched items."""
        with self._lock:
            self._records.clear()
            self._unmatched_reports.clear()
            self._unmatched_requests.clear()
            self._maybe_save()


# Readable aliases.  ``HandoverRecord`` and ``HandoverEntry`` are alternate
# names for a transfer record; ``CommitmentResult`` for a report; and
# ``InstanceReference`` for a SOP Instance reference.
HandoverRecord = TransferRecord
HandoverEntry = TransferRecord
CommitmentResult = CommitmentReport
InstanceReference = SOPInstanceReference
HandoverLedger = HandoverArchive




def commitment_request_from_event(event: Any) -> CommitmentRequest:
    """Return a :class:`CommitmentRequest` from an ``evt.EVT_N_ACTION`` event.

    The event must carry an N-ACTION *Action Information* dataset for the
    *Storage Commitment Push Model* SOP Class.
    """
    from pynetdicom.sop_class import StorageCommitmentPushModel

    request = getattr(event, "request", None)
    actual = getattr(request, "RequestedSOPClassUID", None)
    if actual != StorageCommitmentPushModel:
        raise ValueError(
            "The event is not a Storage Commitment N-ACTION request "
            f"(SOP Class UID: {actual})"
        )

    return parse_commitment_request(
        event.action_information,
        requested_at=event.timestamp,
        action_type_id=event.action_type,
    )


def commitment_report_from_event(event: Any) -> CommitmentReport:
    """Return a :class:`CommitmentReport` from an ``evt.EVT_N_EVENT_REPORT``.

    Both Storage Commitment and UPS use N-EVENT-REPORT, so the event's
    *Affected SOP Class UID* is checked before parsing.
    """
    from pynetdicom.sop_class import StorageCommitmentPushModel

    request = getattr(event, "request", None)
    actual = getattr(request, "AffectedSOPClassUID", None)
    if actual != StorageCommitmentPushModel:
        raise ValueError(
            "The event is not a Storage Commitment N-EVENT-REPORT "
            f"(SOP Class UID: {actual})"
        )

    return parse_commitment_report(
        event.event_information,
        reported_at=event.timestamp,
        event_type_id=event.event_type,
    )


def record_from_event(
    archive: HandoverArchive,
    event: Any,
) -> TransferRecord | None:
    """Record an N-ACTION request or N-EVENT-REPORT result from an event.

    Events for other service classes are ignored and ``None`` is returned.
    """
    from pynetdicom.sop_class import StorageCommitmentPushModel

    request = getattr(event, "request", None)
    if isinstance(request, Dataset):
        return None

    if getattr(request, "RequestedSOPClassUID", None) == (
        StorageCommitmentPushModel
    ):
        return archive.record_commitment_request_dataset(
            event.action_information,
            requested_at=event.timestamp,
            action_type_id=getattr(event, "action_type", None),
        )

    if getattr(request, "AffectedSOPClassUID", None) == (
        StorageCommitmentPushModel
    ):
        return archive.record_commitment_report_dataset(
            event.event_information,
            reported_at=event.timestamp,
            event_type_id=getattr(event, "event_type", None),
        )

    return None
