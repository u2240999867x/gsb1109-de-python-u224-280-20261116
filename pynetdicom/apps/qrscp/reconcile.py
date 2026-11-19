"""Reconciliation of an imaging catalogue against its source instances.

The reconciliation workflow is query first, then per-instance retrieval:

1. Query the imaging catalogue and record each match
2. Request the payload for each matched instance and record each request
3. Verify the retrieved payloads and record only verified material

Query results that are still pending are admitted as candidates. If the
terminal state of the reconciliation is incomplete then the outstanding
instances are retained as coverage gaps. When the same *SOP Instance UID* is
available from more than one source each source is tracked separately: an
existing row in the catalogue is never taken as proof that the payload itself
is present.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pydicom import dcmread


# Reconciliation record types, one stream per reconciliation event
QUERY_MATCH = "query_match"
RETRIEVE_REQUEST = "retrieve_request"
VERIFIED_MATERIAL = "verified_material"

# Reconciliation entry states
CANDIDATE = "CANDIDATE"
MATCHED = "MATCHED"
REQUESTED = "REQUESTED"
VERIFIED = "VERIFIED"
FAILED = "FAILED"
GAP = "GAP"

# A complete payload shall contain the information model's unique keys plus
# the SOP Class UID, which determines the presentation context
_REQUIRED_KEYWORDS = (
    "PatientID",
    "StudyInstanceUID",
    "SeriesInstanceUID",
    "SOPClassUID",
    "SOPInstanceUID",
)


def _timestamp():
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ReconciliationRecord:
    """An append-only record of a single reconciliation event.

    Attributes
    ----------
    record_type : str
        One of ``QUERY_MATCH``, ``RETRIEVE_REQUEST`` or ``VERIFIED_MATERIAL``.
    sop_instance_uid : str
        The (0008,0018) *SOP Instance UID* the record relates to.
    source : str
        The catalogue or retrieval source (for example an AE title).
    status : str
        The entry's state at the time the record was made.
    timestamp : str
        An ISO 8601 UTC timestamp for the event.
    detail : str or None
        Optional extra information about the event.
    payload_path : str or None
        For a verified material record, the path to the stored payload.
    """

    record_type: str
    sop_instance_uid: str
    source: str
    status: str
    timestamp: str
    detail: str | None = None
    payload_path: str | None = None


@dataclass
class ReconciliationEntry:
    """The reconciliation state of a ``(SOP Instance UID, source)`` pair.

    Attributes
    ----------
    sop_instance_uid : str
        The (0008,0018) *SOP Instance UID*.
    source : str
        The catalogue or retrieval source (for example an AE title).
    status : str
        The current reconciliation state.
    detail : str or None
        Optional extra information, such as a failure reason.
    payload_path : str or None
        The path to the verified payload, once one has been verified.
    """

    sop_instance_uid: str
    source: str
    status: str = CANDIDATE
    detail: str | None = None
    payload_path: str | None = None


def verify_instance(payload_path, expected_uid=None):
    """Verify that a retrieved payload is a complete SOP Instance.

    The payload must exist, be readable as DICOM, contain non-empty values
    for all the information model's unique keys and, when `expected_uid` is
    given, carry a matching (0008,0018) *SOP Instance UID*.

    Parameters
    ----------
    payload_path : str or pathlib.Path
        The path to the retrieved payload.
    expected_uid : str or None
        The *SOP Instance UID* the payload is expected to carry. If ``None``
        then the payload's own UID is only checked for presence.

    Returns
    -------
    bool
        ``True`` when the payload is complete and verified, ``False``
        otherwise.
    """
    path = Path(payload_path)
    if not path.is_file() or path.stat().st_size == 0:
        return False

    try:
        ds = dcmread(path)
    except Exception:
        return False

    uid = getattr(ds, "SOPInstanceUID", None)
    if not uid:
        return False

    if expected_uid is not None and uid != expected_uid:
        return False

    for keyword in _REQUIRED_KEYWORDS:
        if keyword not in ds or ds[keyword].value in (None, ""):
            return False

    return True


class ReconciliationLedger:
    """Records reconciliation events and tracks per-source state.

    The three event streams are kept separate so that catalogue matches,
    retrieval requests and verified materials can be audited independently.
    State is tracked per ``(SOP Instance UID, source)`` pair so that a UID
    available from multiple sources is never marked complete on the strength
    of a single catalogue row.
    """

    def __init__(self):
        self.query_matches: list[ReconciliationRecord] = []
        self.retrieve_requests: list[ReconciliationRecord] = []
        self.verified_materials: list[ReconciliationRecord] = []
        self._entries: dict[tuple[str, str], ReconciliationEntry] = {}

    def _get_entry(self, sop_instance_uid, source):
        """Return the entry for a UID/source pair, creating it if needed."""
        key = (sop_instance_uid, source)
        if key not in self._entries:
            self._entries[key] = ReconciliationEntry(sop_instance_uid, source)

        return self._entries[key]

    def _make_record(self, record_type, entry, payload_path=None):
        """Build an append-only record from the current state of `entry`."""
        return ReconciliationRecord(
            record_type=record_type,
            sop_instance_uid=entry.sop_instance_uid,
            source=entry.source,
            status=entry.status,
            timestamp=_timestamp(),
            detail=entry.detail,
            payload_path=payload_path or entry.payload_path,
        )

    def record_query_match(
        self, sop_instance_uid, source, *, pending=False, detail=None
    ):
        """Record a catalogue query match.

        A pending query result is admitted as a candidate. A terminal result
        upgrades a candidate to a match but never downgrades an entry that
        has already been requested or verified.

        Parameters
        ----------
        sop_instance_uid : str
            The matched (0008,0018) *SOP Instance UID*.
        source : str
            The catalogue source the match came from.
        pending : bool, optional
            ``True`` when the catalogue query is still pending (default
            ``False``, meaning a terminal catalogue row).
        detail : str or None
            Optional extra information about the match.

        Returns
        -------
        str
            The entry's state after recording the match.
        """
        entry = self._get_entry(sop_instance_uid, source)
        if detail is not None:
            entry.detail = detail

        if pending:
            # Pending results join the candidates without advancing further
            if entry.status not in (MATCHED, REQUESTED, VERIFIED, FAILED, GAP):
                entry.status = CANDIDATE
        elif entry.status == CANDIDATE:
            # A terminal catalogue row advances a candidate, but is still no
            # proof that the payload itself is available
            entry.status = MATCHED

        self.query_matches.append(self._make_record(QUERY_MATCH, entry))
        return entry.status

    def record_retrieve_request(self, sop_instance_uid, source, detail=None):
        """Record that a payload retrieval request has been issued.

        Parameters
        ----------
        sop_instance_uid : str
            The (0008,0018) *SOP Instance UID* being retrieved.
        source : str
            The source the payload is being retrieved from.
        detail : str or None
            Optional extra information about the request.

        Returns
        -------
        str
            The entry's state after recording the request.
        """
        entry = self._get_entry(sop_instance_uid, source)
        if detail is not None:
            entry.detail = detail

        if entry.status in (CANDIDATE, MATCHED):
            entry.status = REQUESTED

        self.retrieve_requests.append(self._make_record(RETRIEVE_REQUEST, entry))
        return entry.status

    def record_verified_material(
        self, sop_instance_uid, source, payload_path, detail=None
    ):
        """Verify a retrieved payload and record it when complete.

        The payload is verified against `sop_instance_uid` before anything is
        added to the verified material stream. Verification failure leaves
        the entry in the :data:`FAILED` state instead.

        Parameters
        ----------
        sop_instance_uid : str
            The (0008,0018) *SOP Instance UID* being reconciled.
        source : str
            The source the payload was retrieved from.
        payload_path : str or pathlib.Path
            The path to the retrieved payload.
        detail : str or None
            Optional extra information about the verification.

        Returns
        -------
        bool
            ``True`` when the payload was verified, ``False`` otherwise.
        """
        entry = self._get_entry(sop_instance_uid, source)

        if not verify_instance(payload_path, sop_instance_uid):
            return self.record_failure(
                sop_instance_uid,
                source,
                detail or f"payload verification failed: {payload_path}",
            )

        entry.status = VERIFIED
        entry.payload_path = str(payload_path)
        if detail is not None:
            entry.detail = detail

        self.verified_materials.append(
            self._make_record(VERIFIED_MATERIAL, entry, payload_path=str(payload_path))
        )
        return True

    def record_failure(self, sop_instance_uid, source, detail=None):
        """Record that retrieval or verification failed for an entry.

        A verified entry is never downgraded.

        Parameters
        ----------
        sop_instance_uid : str
            The (0008,0018) *SOP Instance UID* that failed.
        source : str
            The source involved in the failure.
        detail : str or None
            Optional reason for the failure.

        Returns
        -------
        bool
            Always ``False``, so the method can be used directly as a
            verification result.
        """
        entry = self._get_entry(sop_instance_uid, source)
        if entry.status != VERIFIED:
            entry.status = FAILED

        if detail is not None:
            entry.detail = detail

        return False

    def finalize(self):
        """Retain every incomplete entry as a coverage gap.

        Entries still in the normal lifecycle (candidate, match or requested)
        move to :data:`GAP`; failed entries stay :data:`FAILED`. Verified
        entries are untouched.

        Returns
        -------
        list of ReconciliationEntry
            The entries that remain incomplete after finalisation.
        """
        for entry in self._entries.values():
            if entry.status in (CANDIDATE, MATCHED, REQUESTED):
                entry.status = GAP

        return self.coverage_gaps

    @property
    def entries(self):
        """list of ReconciliationEntry: all tracked UID/source pairs."""
        return list(self._entries.values())

    @property
    def candidates(self):
        """list of ReconciliationEntry: entries admitted from pending query
        results that have not yet advanced."""
        return [entry for entry in self._entries.values() if entry.status == CANDIDATE]

    @property
    def coverage_gaps(self):
        """list of ReconciliationEntry: entries without a verified payload."""
        return [entry for entry in self._entries.values() if entry.status != VERIFIED]

    def state(self, sop_instance_uid, source):
        """Return the state of a UID/source pair.

        Parameters
        ----------
        sop_instance_uid : str
            The (0008,0018) *SOP Instance UID*.
        source : str
            The catalogue or retrieval source.

        Returns
        -------
        str
            The entry's current state.

        Raises
        ------
        KeyError
            If the UID/source pair has never been recorded.
        """
        return self._entries[(sop_instance_uid, source)].status

    def is_verified(self, sop_instance_uid, source):
        """Return ``True`` if a payload is verified for the UID/source pair."""
        entry = self._entries.get((sop_instance_uid, source))
        return bool(entry and entry.status == VERIFIED)

    def verified_uids(self, source=None):
        """Return the verified *SOP Instance UID*s, optionally for one source.

        Parameters
        ----------
        source : str or None
            When given, only UIDs verified from this source are returned.

        Returns
        -------
        list of str
            The verified UIDs in recording order.
        """
        return [
            record.sop_instance_uid
            for record in self.verified_materials
            if source is None or record.source == source
        ]


def reconcile_instances(
    ledger: ReconciliationLedger,
    query_results: Iterable[Mapping],
    fetch_instance: Callable[[str, str], object],
) -> ReconciliationLedger:
    """Query the catalogue first, then retrieve and verify each instance.

    Every catalogue match is recorded before any retrieval request is issued.
    Pending query results are admitted as candidates, then each matched
    instance is fetched individually from its own source. A catalogue row is
    never treated as a payload: only a verified retrieve moves an entry to
    the verified material stream.

    Parameters
    ----------
    ledger : ReconciliationLedger
        The ledger to record into.
    query_results : Iterable[Mapping]
        Catalogue query rows. Each mapping requires ``sop_instance_uid`` and
        ``source`` keys and may provide a boolean ``pending`` flag and a
        ``detail`` string.
    fetch_instance : Callable[[str, str], object]
        Called as ``fetch_instance(sop_instance_uid, source)`` for each
        matched instance. Returns the path to the retrieved payload, or
        ``None`` if no payload could be retrieved. Exceptions are recorded as
        failures rather than stopping the reconciliation.

    Returns
    -------
    ReconciliationLedger
        The same ledger, finalised with any coverage gaps retained.
    """
    # Phase 1: catalogue query - pending results are admitted as candidates
    for row in query_results:
        ledger.record_query_match(
            row["sop_instance_uid"],
            row["source"],
            pending=bool(row.get("pending", False)),
            detail=row.get("detail"),
        )

    # Phase 2: per-instance retrieval, one request per UID/source pair
    for entry in list(ledger.entries):
        if entry.status not in (CANDIDATE, MATCHED):
            continue

        ledger.record_retrieve_request(entry.sop_instance_uid, entry.source)

        try:
            payload_path = fetch_instance(entry.sop_instance_uid, entry.source)
        except Exception as exc:
            ledger.record_failure(entry.sop_instance_uid, entry.source, str(exc))
            continue

        if payload_path is None:
            ledger.record_failure(
                entry.sop_instance_uid,
                entry.source,
                "retrieval returned no payload",
            )
            continue

        ledger.record_verified_material(
            entry.sop_instance_uid, entry.source, payload_path
        )

    # Terminal state: anything still incomplete is retained as a gap
    ledger.finalize()
    return ledger
