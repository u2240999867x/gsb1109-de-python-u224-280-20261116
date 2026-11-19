"""Handover archive integration for the qrscp application.

The archive is stored separately from the QR instance database so it can be
inspected or handed to another system without exposing the instance files.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from pydicom.dataset import Dataset

from pynetdicom.handover import (
    CommitmentReport,
    CommitmentRequest,
    HandoverArchive,
    SOPInstanceReference,
    TransactionUIDConflict,
    _as_utc,
    commitment_report_from_event,
    commitment_request_from_event,
)


class HandoverManager:
    """Track C-STORE batches and update a :class:`HandoverArchive`.

    Parameters
    ----------
    path : str or pathlib.Path
        The JSON file used to persist the archive.
    """

    def __init__(self, path: Any) -> None:
        self.archive = HandoverArchive(path)
        self._sessions: dict[int, dict[str, Any]] = {}
        self._batches: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    def note_store(
        self,
        assoc: Any,
        dataset: Dataset,
        timestamp: datetime | str | None = None,
    ) -> None:
        """Record a successfully stored instance for `assoc`'s batch."""
        try:
            reference = SOPInstanceReference(
                dataset.SOPClassUID, dataset.SOPInstanceUID
            )
        except Exception:
            return

        moment = _as_utc(timestamp)
        key = id(assoc)
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                session = {
                    "instances": {},
                    "first": moment,
                    "last": moment,
                    "source_ae": getattr(
                        getattr(assoc, "requestor", None), "ae_title", None
                    ),
                    "destination_ae": getattr(
                        getattr(assoc, "ae", None), "ae_title", None
                    ),
                }
                self._sessions[key] = session

            session["instances"][reference.key] = moment
            session["last"] = moment

    def transfer_time(self, assoc: Any) -> datetime | None:
        """Return the last C-STORE time for `assoc`'s batch, if known."""
        with self._lock:
            session = self._sessions.get(id(assoc))
            return session["last"] if session else None

    def transfer_time_for(self, instances: Any) -> datetime | None:
        """Return the last C-STORE time for a matching batch, if known.

        A commitment request may arrive on a different association from the
        C-STORE batch, so the batch is located by its instance manifest.  A
        batch is only used when it contains every requested instance; a batch
        that merely overlaps an unrelated transfer is not enough.
        """
        wanted = frozenset(item.key for item in instances)
        with self._lock:
            sessions = list(self._sessions.values()) + self._batches

        moments = [
            moment
            for session in sessions
            if wanted <= set(session["instances"])
            for key, moment in session["instances"].items()
            if key in wanted
        ]

        return max(moments) if moments else None

    def release(self, assoc: Any) -> None:
        """Finish the C-STORE batch associated with `assoc`.

        The batch is retained for later commitment correlation but no longer
        belongs to the association.
        """
        key = id(assoc)
        with self._lock:
            session = self._sessions.pop(key, None)
            if session and session["instances"]:
                self._batches.append(session)
                if len(self._batches) > 100:
                    self._batches = self._batches[-100:]

    def record_request(self, event: Any):
        """Record a Storage Commitment N-ACTION request.

        The request's transaction UID and instance manifest define the
        handover batch.  The transfer time comes from the C-STORE batch that
        delivered the same instances, even when the request arrived on a
        different association.  If no C-STORE batch is known, the request time
        is used.
        """
        request: CommitmentRequest = commitment_request_from_event(event)
        source_ae = getattr(getattr(event.assoc, "requestor", None), "ae_title", None)
        destination_ae = getattr(getattr(event.assoc, "ae", None), "ae_title", None)

        transferred_at = (
            self.transfer_time(event.assoc)
            or self.transfer_time_for(request.instances)
            or event.timestamp
        )

        try:
            self.archive.record_transfer(
                request.transaction_uid,
                request.instances,
                transferred_at=transferred_at,
                source_ae=source_ae,
                destination_ae=destination_ae,
            )
        except TransactionUIDConflict:
            # The transaction is already associated with a different manifest;
            # keep the original record and let the request validation below
            # reject any references outside that manifest.
            pass

        return self.archive.record_commitment_request(request)

    def request_transaction_uid(self, event: Any) -> str:
        """Return the transaction UID from an N-ACTION event."""
        return commitment_request_from_event(event).transaction_uid

    def record_report(self, event: Any):
        """Record a Storage Commitment N-EVENT-REPORT result."""
        report: CommitmentReport = commitment_report_from_event(event)
        return self.archive.record_commitment_report(report)

    def status(self, transaction_uid: str):
        """Return the handover status for `transaction_uid`."""
        return self.archive.status(transaction_uid)

    def records(self):
        """Return all handover records."""
        return self.archive.records
