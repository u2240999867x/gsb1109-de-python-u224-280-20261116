"""Reconcile 997/999 functional acknowledgments into a per-transaction delivery ledger."""

from __future__ import annotations

import io
from dataclasses import dataclass

import pyx12.x12file

ACCEPTED = "accepted"
REJECTED = "rejected"
UNKNOWN = "unknown"
CONFLICT = "conflict"

UNMATCHED = "unmatched"
AMBIGUOUS = "ambiguous"

# AK5-01 / IK5-01 acknowledgment codes mapped to a delivery verdict. Codes
# outside the map (P, M, W, X, ...) prove neither acceptance nor rejection.
_TRANSACTION_ACK_VERDICTS = {"A": ACCEPTED, "E": ACCEPTED, "R": REJECTED}

_GROUP_ACCEPT_CODES = frozenset({"A", "E"})
_GROUP_REJECT_CODES = frozenset({"R"})


@dataclass(frozen=True)
class GroupKey:
    """Reference to an outbound functional group, from the original sender's perspective.

    Control numbers are only unique within a sender domain, so the sender and
    receiver ids are part of the key.
    """

    sender_id: str
    receiver_id: str
    functional_id: str
    control_number: str


@dataclass(frozen=True)
class TransactionKey:
    group: GroupKey
    transaction_set_id: str
    control_number: str


@dataclass(frozen=True)
class GroupTotals:
    included: int | None = None
    received: int | None = None
    accepted: int | None = None


@dataclass(frozen=True)
class GroupConclusion:
    group: GroupKey
    code: str
    totals: GroupTotals
    report_id: str


@dataclass(frozen=True)
class TransactionVerdict:
    transaction: TransactionKey
    verdict: str
    report_id: str


@dataclass(frozen=True)
class AckReport:
    report_id: str
    conclusions: tuple[GroupConclusion, ...]
    verdicts: tuple[TransactionVerdict, ...]
    raw: str


@dataclass(frozen=True)
class OutboundGroup:
    group_id: str
    key: GroupKey
    transactions: frozenset[TransactionKey]


@dataclass(frozen=True)
class CandidateConclusion:
    conclusion: GroupConclusion
    reason: str


@dataclass(frozen=True)
class TotalsCheck:
    report_id: str
    consistent: bool
    detail: str


@dataclass(frozen=True)
class GroupReconciliation:
    group: OutboundGroup
    confirmed: bool
    codes: frozenset[str]
    group_conflict: bool
    statuses: dict[TransactionKey, str]
    totals_checks: tuple[TotalsCheck, ...]
    out_of_scope: tuple[TransactionVerdict, ...]


def _status_from_verdicts(verdicts: set[str]) -> str:
    if ACCEPTED in verdicts and REJECTED in verdicts:
        return CONFLICT
    if ACCEPTED in verdicts:
        return ACCEPTED
    if REJECTED in verdicts:
        return REJECTED
    return UNKNOWN


def _as_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _check_totals(
    conclusion: GroupConclusion, group: OutboundGroup, statuses: dict[TransactionKey, str]
) -> list[TotalsCheck]:
    checks = []
    totals = conclusion.totals
    if totals.included is not None:
        scope_count = len(group.transactions)
        checks.append(
            TotalsCheck(
                report_id=conclusion.report_id,
                consistent=totals.included == scope_count,
                detail=(
                    f"reported {totals.included} transaction sets included, "
                    f"ledger scope has {scope_count}"
                ),
            )
        )
    if totals.accepted is not None:
        proven = sum(1 for status in statuses.values() if status == ACCEPTED)
        undecided = sum(1 for status in statuses.values() if status in (UNKNOWN, CONFLICT))
        consistent = proven <= totals.accepted <= proven + undecided
        checks.append(
            TotalsCheck(
                report_id=conclusion.report_id,
                consistent=consistent,
                detail=(
                    f"reported {totals.accepted} accepted, {proven} proven accepted and "
                    f"{undecided} undecided in ledger scope"
                ),
            )
        )
    return checks


class DeliveryLedger:
    """Merges outbound groups and ack reports into delivery statuses.

    Every result is derived from the full set of registered groups and retained
    reports, so the outcome never depends on arrival order and recorded
    contradictions are never erased by later receipts.
    """

    def __init__(self) -> None:
        self.groups: dict[str, OutboundGroup] = {}
        self.reports: dict[str, AckReport] = {}

    def register_group(self, group: OutboundGroup) -> None:
        self.groups[group.group_id] = group

    def add_report(self, report: AckReport) -> None:
        self.reports[report.report_id] = report

    def _match(self, key: GroupKey) -> OutboundGroup | None:
        matches = [group for group in self.groups.values() if group.key == key]
        return matches[0] if len(matches) == 1 else None

    def _conclusions(self) -> list[GroupConclusion]:
        return [c for report in self.reports.values() for c in report.conclusions]

    def _linked_verdicts(self) -> list[TransactionVerdict]:
        return [
            verdict
            for report in self.reports.values()
            for verdict in report.verdicts
            if self._match(verdict.transaction.group) is not None
        ]

    def candidates(self) -> list[CandidateConclusion]:
        """Conclusions that match zero or several registered groups stay unlinked."""
        result = []
        for conclusion in self._conclusions():
            matches = [g for g in self.groups.values() if g.key == conclusion.group]
            if len(matches) == 0:
                result.append(CandidateConclusion(conclusion, UNMATCHED))
            elif len(matches) > 1:
                result.append(CandidateConclusion(conclusion, AMBIGUOUS))
        return sorted(
            result,
            key=lambda c: (
                c.conclusion.report_id,
                c.conclusion.group.functional_id,
                c.conclusion.group.control_number,
            ),
        )

    def transaction_status(self, key: TransactionKey) -> str:
        verdicts = {v.verdict for v in self._linked_verdicts() if v.transaction == key}
        return _status_from_verdicts(verdicts)

    def transaction_statuses(self) -> dict[TransactionKey, str]:
        keys = {t for group in self.groups.values() for t in group.transactions}
        keys.update(v.transaction for v in self._linked_verdicts())
        return {key: self.transaction_status(key) for key in keys}

    def reconcile_group(self, group_id: str) -> GroupReconciliation:
        group = self.groups[group_id]
        conclusions = [c for c in self._conclusions() if self._match(c.group) is group]
        codes = frozenset(c.code for c in conclusions)
        statuses = {t: self.transaction_status(t) for t in group.transactions}
        out_of_scope = tuple(
            sorted(
                (
                    v
                    for v in self._linked_verdicts()
                    if v.transaction.group == group.key and v.transaction not in group.transactions
                ),
                key=lambda v: (
                    v.report_id,
                    v.transaction.transaction_set_id,
                    v.transaction.control_number,
                ),
            )
        )
        checks = tuple(
            sorted(
                (
                    check
                    for conclusion in conclusions
                    for check in _check_totals(conclusion, group, statuses)
                ),
                key=lambda check: (check.report_id, check.detail),
            )
        )
        return GroupReconciliation(
            group=group,
            confirmed=bool(conclusions),
            codes=codes,
            group_conflict=bool(codes & _GROUP_ACCEPT_CODES and codes & _GROUP_REJECT_CODES),
            statuses=statuses,
            totals_checks=checks,
            out_of_scope=out_of_scope,
        )

    def reconcile(self) -> dict[str, GroupReconciliation]:
        return {group_id: self.reconcile_group(group_id) for group_id in self.groups}


def parse_ack_report(report_id: str, text: str) -> AckReport:
    """Build an ack report from a raw 997/999 interchange, keeping the original text."""
    reader = pyx12.x12file.X12Reader(io.StringIO(text))
    orig_sender = ""
    orig_receiver = ""
    conclusions: list[GroupConclusion] = []
    verdicts: list[TransactionVerdict] = []
    group: GroupKey | None = None
    transaction: TransactionKey | None = None
    for seg in reader:
        seg_id = seg.get_seg_id()
        if seg_id == "ISA":
            # the ack's receiver is the original sender and vice versa
            orig_receiver = (seg.get_value("ISA06") or "").strip()
            orig_sender = (seg.get_value("ISA08") or "").strip()
        elif seg_id == "AK1":
            group = GroupKey(
                sender_id=orig_sender,
                receiver_id=orig_receiver,
                functional_id=seg.get_value("AK101") or "",
                control_number=seg.get_value("AK102") or "",
            )
            transaction = None
        elif seg_id == "AK2" and group is not None:
            transaction = TransactionKey(
                group=group,
                transaction_set_id=seg.get_value("AK201") or "",
                control_number=seg.get_value("AK202") or "",
            )
        elif seg_id in ("AK5", "IK5") and transaction is not None:
            verdict = _TRANSACTION_ACK_VERDICTS.get(seg.get_value(seg_id + "01") or "")
            if verdict is not None:
                verdicts.append(TransactionVerdict(transaction, verdict, report_id))
        elif seg_id == "AK9" and group is not None:
            conclusions.append(
                GroupConclusion(
                    group=group,
                    code=seg.get_value("AK901") or "",
                    totals=GroupTotals(
                        included=_as_int(seg.get_value("AK902")),
                        received=_as_int(seg.get_value("AK903")),
                        accepted=_as_int(seg.get_value("AK904")),
                    ),
                    report_id=report_id,
                )
            )
    return AckReport(report_id, tuple(conclusions), tuple(verdicts), text)
