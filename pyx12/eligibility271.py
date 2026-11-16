#####################################################################
# Copyright
#   John Holland <john@zoner.org>
# All rights reserved.
#
# This software is licensed as described in the file LICENSE.txt, which
# you should have received as part of this distribution.
#
######################################################################
"""
Extract eligibility benefit statements (EB) from a HIPAA 271 response and
answer downstream coverage queries by beneficiary, service type, date and
network.

Each EB segment in loop 2110C (subscriber) or 2110D (dependent) is kept as
its own statement with its original file position; statements are never
merged into a single effective boolean. A query conclusion is one of
"covered", "not_covered", "conflict" or "unknown" per requested service
type, and always carries the segment references that support it.

Polarity of a statement follows EB01: active-coverage codes and benefit
detail codes (co-pay, deductible, ... only exist for covered services) are
positive; inactive / non-covered codes are negative; anything else is
neutral and cannot decide a conclusion. Positive and negative statements
covering the same queried scope yield "conflict" unless exactly one side
carries adjudication information (MSG text or an AAA rejection), which is
taken as the payer's basis for deciding.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TextIO

import pyx12.error_handler
import pyx12.params
import pyx12.x12context

ACTIVE_INFO_CODES = frozenset(("1", "2", "3", "4", "5"))
INACTIVE_INFO_CODES = frozenset(("6", "7", "8", "I"))
DETAIL_INFO_CODES = frozenset(
    (
        "A",
        "B",
        "C",
        "D",
        "E",
        "F",
        "G",
        "H",
        "J",
        "K",
        "L",
        "M",
        "N",
        "O",
        "P",
        "Q",
        "R",
        "S",
        "T",
        "U",
        "Y",
        "CB",
        "MC",
    )
)

STATUS_COVERED = "covered"
STATUS_NOT_COVERED = "not_covered"
STATUS_CONFLICT = "conflict"
STATUS_UNKNOWN = "unknown"

ROLE_SUBSCRIBER = "subscriber"
ROLE_DEPENDENT = "dependent"


@dataclass(frozen=True)
class SegmentRef:
    """Original position of a segment in the source 271."""

    loop_path: str
    seg_count: int
    line_number: int


@dataclass(frozen=True)
class DateRange:
    qualifier: str | None
    start: str | None
    end: str | None

    def covers(self, date: str) -> bool:
        if self.start is not None and date < self.start:
            return False
        if self.end is not None and date > self.end:
            return False
        return True


@dataclass(frozen=True)
class BenefitStatement:
    """One EB segment and the qualifying segments in its 2110 loop."""

    info_code: str
    coverage_level: str | None
    service_types: tuple[str, ...]
    insurance_type: str | None
    description: str | None
    time_period: str | None
    amount: str | None
    percent: str | None
    quantity: str | None
    auth_required: str | None
    in_network: str | None
    dates: tuple[DateRange, ...]
    messages: tuple[str, ...]
    rejections: tuple[str, ...]
    ref: SegmentRef


@dataclass(frozen=True)
class Beneficiary:
    role: str
    member_id: str | None
    last_name: str | None
    first_name: str | None
    dob: str | None
    loop_dates: tuple[DateRange, ...]
    rejections: tuple[str, ...]
    statements: tuple[BenefitStatement, ...]
    ref: SegmentRef


@dataclass(frozen=True)
class EligibilityResponse:
    beneficiaries: tuple[Beneficiary, ...]


@dataclass(frozen=True)
class BenefitQuery:
    """
    Query scope and qualifier inheritance / priority rules.

    Selectors (role, member_id, names) must all match a single beneficiary;
    subscriber and dependent records are never used to fill each other's
    gaps, so a selector matching both is ambiguous and yields no conclusion.
    """

    service_types: tuple[str, ...]
    role: str | None = None
    member_id: str | None = None
    last_name: str | None = None
    first_name: str | None = None
    date: str | None = None
    network: str | None = None
    inherit_loop_dates: bool = True
    unspecified_network_applies: bool = True


@dataclass(frozen=True)
class ServiceConclusion:
    service_type: str
    status: str
    statements: tuple[BenefitStatement, ...]
    supporting: tuple[SegmentRef, ...]


@dataclass(frozen=True)
class QueryResult:
    matched: bool
    beneficiary: Beneficiary | None
    conclusions: tuple[ServiceConclusion, ...]


def _parse_dtp(seg_data: Any) -> DateRange:
    qualifier = seg_data.get_value("DTP01")
    date_fmt = seg_data.get_value("DTP02")
    value = seg_data.get_value("DTP03") or ""
    if date_fmt == "RD8" and "-" in value:
        start, end = value.split("-", 1)
    elif value:
        start = end = value
    else:
        start = end = None
    return DateRange(qualifier, start or None, end or None)


def _segment_ref(node: Any) -> SegmentRef:
    return SegmentRef(
        loop_path=node.cur_path,
        seg_count=node.seg_count if node.seg_count is not None else -1,
        line_number=node.cur_line_number if node.cur_line_number is not None else -1,
    )


def _new_beneficiary(role: str, ref: SegmentRef) -> dict[str, Any]:
    return {
        "role": role,
        "member_id": None,
        "last_name": None,
        "first_name": None,
        "dob": None,
        "loop_dates": [],
        "rejections": [],
        "statements": [],
        "ref": ref,
    }


def _new_statement(seg_data: Any, ref: SegmentRef) -> dict[str, Any]:
    service_type = seg_data.get_value("EB03")
    return {
        "info_code": seg_data.get_value("EB01") or "",
        "coverage_level": seg_data.get_value("EB02") or None,
        "service_types": (service_type,) if service_type else (),
        "insurance_type": seg_data.get_value("EB04") or None,
        "description": seg_data.get_value("EB05") or None,
        "time_period": seg_data.get_value("EB06") or None,
        "amount": seg_data.get_value("EB07") or None,
        "percent": seg_data.get_value("EB08") or None,
        "quantity": seg_data.get_value("EB09") or None,
        "auth_required": seg_data.get_value("EB11") or None,
        "in_network": seg_data.get_value("EB12") or None,
        "dates": [],
        "messages": [],
        "rejections": [],
        "ref": ref,
    }


def _freeze_statement(stmt: dict[str, Any]) -> BenefitStatement:
    return BenefitStatement(
        info_code=stmt["info_code"],
        coverage_level=stmt["coverage_level"],
        service_types=tuple(stmt["service_types"]),
        insurance_type=stmt["insurance_type"],
        description=stmt["description"],
        time_period=stmt["time_period"],
        amount=stmt["amount"],
        percent=stmt["percent"],
        quantity=stmt["quantity"],
        auth_required=stmt["auth_required"],
        in_network=stmt["in_network"],
        dates=tuple(stmt["dates"]),
        messages=tuple(stmt["messages"]),
        rejections=tuple(stmt["rejections"]),
        ref=stmt["ref"],
    )


def _freeze_beneficiary(ben: dict[str, Any]) -> Beneficiary:
    return Beneficiary(
        role=ben["role"],
        member_id=ben["member_id"],
        last_name=ben["last_name"],
        first_name=ben["first_name"],
        dob=ben["dob"],
        loop_dates=tuple(ben["loop_dates"]),
        rejections=tuple(ben["rejections"]),
        statements=tuple(_freeze_statement(s) for s in ben["statements"]),
        ref=ben["ref"],
    )


def parse_eligibility_response(
    src_file_obj: str | TextIO, param: Any = None
) -> EligibilityResponse:
    """
    Parse a 271 response into per-beneficiary benefit statements.

    Statements stay attached to the beneficiary loop (2100C subscriber,
    2100D dependent) they were found in; identical member ids or names in
    the two loops produce distinct Beneficiary records.
    """
    if param is None:
        param = pyx12.params.params()
    errh = pyx12.error_handler.errh_null()
    beneficiaries: list[dict[str, Any]] = []
    cur_ben: dict[str, Any] | None = None
    cur_stmt: dict[str, Any] | None = None
    with pyx12.x12context.X12ContextReader(param, errh, src_file_obj) as src:
        for node in src.iter_segments():
            if node.seg_data is None:
                continue
            loops = node.x12_map_node.x12path.loop_list
            seg = node.seg_data
            seg_id = seg.get_seg_id()
            if seg_id == "HL" and loops and loops[-1] in ("2000C", "2000D"):
                role = ROLE_SUBSCRIBER if loops[-1] == "2000C" else ROLE_DEPENDENT
                cur_ben = _new_beneficiary(role, _segment_ref(node))
                beneficiaries.append(cur_ben)
                cur_stmt = None
            elif cur_ben is None:
                continue
            elif seg_id == "NM1" and loops[-1] in ("2100C", "2100D"):
                cur_ben["member_id"] = seg.get_value("NM109") or None
                cur_ben["last_name"] = seg.get_value("NM103") or None
                cur_ben["first_name"] = seg.get_value("NM104") or None
            elif seg_id == "DMG" and loops[-1] in ("2100C", "2100D"):
                cur_ben["dob"] = seg.get_value("DMG02") or None
            elif seg_id == "AAA" and loops[-1] in ("2100C", "2100D"):
                reason = seg.get_value("AAA03")
                if reason:
                    cur_ben["rejections"].append(reason)
            elif seg_id == "DTP" and loops[-1] in ("2100C", "2100D"):
                cur_ben["loop_dates"].append(_parse_dtp(seg))
            elif seg_id == "EB" and loops[-1] in ("2110C", "2110D"):
                cur_stmt = _new_statement(seg, _segment_ref(node))
                cur_ben["statements"].append(cur_stmt)
            elif cur_stmt is None:
                continue
            elif seg_id == "DTP" and loops[-1] in ("2110C", "2110D"):
                cur_stmt["dates"].append(_parse_dtp(seg))
            elif seg_id == "MSG" and loops[-1] in ("2110C", "2110D"):
                text = seg.get_value("MSG01")
                if text:
                    cur_stmt["messages"].append(text)
            elif seg_id == "AAA" and loops[-1] in ("2110C", "2110D"):
                reason = seg.get_value("AAA03")
                if reason:
                    cur_stmt["rejections"].append(reason)
    return EligibilityResponse(beneficiaries=tuple(_freeze_beneficiary(b) for b in beneficiaries))


def _polarity(stmt: BenefitStatement) -> str:
    if stmt.info_code in ACTIVE_INFO_CODES or stmt.info_code in DETAIL_INFO_CODES:
        return "positive"
    if stmt.info_code in INACTIVE_INFO_CODES:
        return "negative"
    return "neutral"


def _matches_beneficiary(ben: Beneficiary, query: BenefitQuery) -> bool:
    if query.role is not None and ben.role != query.role:
        return False
    if query.member_id is not None and ben.member_id != query.member_id:
        return False
    if query.last_name is not None and (
        ben.last_name is None or ben.last_name.upper() != query.last_name.upper()
    ):
        return False
    if query.first_name is not None and (
        ben.first_name is None or ben.first_name.upper() != query.first_name.upper()
    ):
        return False
    return True


def _statement_dates(
    stmt: BenefitStatement, ben: Beneficiary, query: BenefitQuery
) -> tuple[DateRange, ...]:
    if stmt.dates:
        return stmt.dates
    if query.inherit_loop_dates:
        return ben.loop_dates
    return ()


def _applies(
    stmt: BenefitStatement, ben: Beneficiary, service_type: str, query: BenefitQuery
) -> bool:
    if stmt.service_types and service_type not in stmt.service_types:
        return False
    if query.network == "in":
        if stmt.in_network is not None and stmt.in_network != "Y":
            return False
        if stmt.in_network is None and not query.unspecified_network_applies:
            return False
    elif query.network == "out":
        if stmt.in_network is not None and stmt.in_network != "N":
            return False
        if stmt.in_network is None and not query.unspecified_network_applies:
            return False
    if query.date is not None:
        dates = _statement_dates(stmt, ben, query)
        if dates and not any(d.covers(query.date) for d in dates):
            return False
    return True


def _has_adjudication_basis(stmt: BenefitStatement) -> bool:
    return bool(stmt.messages or stmt.rejections)


def _adjudicate(pos: list[BenefitStatement], neg: list[BenefitStatement]) -> str:
    pos_basis = any(_has_adjudication_basis(s) for s in pos)
    neg_basis = any(_has_adjudication_basis(s) for s in neg)
    if pos_basis and not neg_basis:
        return STATUS_COVERED
    if neg_basis and not pos_basis:
        return STATUS_NOT_COVERED
    return STATUS_CONFLICT


def _conclude(ben: Beneficiary, service_type: str, query: BenefitQuery) -> ServiceConclusion:
    stmts = [s for s in ben.statements if _applies(s, ben, service_type, query)]
    pos = [s for s in stmts if _polarity(s) == "positive"]
    neg = [s for s in stmts if _polarity(s) == "negative"]
    if pos and neg:
        status = _adjudicate(pos, neg)
        decisive = pos + neg
    elif pos:
        status = STATUS_COVERED
        decisive = pos
    elif neg:
        status = STATUS_NOT_COVERED
        decisive = neg
    else:
        status = STATUS_UNKNOWN
        decisive = stmts
    return ServiceConclusion(
        service_type=service_type,
        status=status,
        statements=tuple(stmts),
        supporting=tuple(s.ref for s in decisive),
    )


def query_benefits(response: EligibilityResponse, query: BenefitQuery) -> QueryResult:
    """
    Answer a benefit query against a parsed 271 response.

    Services the response says nothing about stay "unknown"; they are never
    reported as not covered. Every conclusion lists the segment references
    of the statements that decided it.
    """
    matches = [b for b in response.beneficiaries if _matches_beneficiary(b, query)]
    if len(matches) != 1:
        conclusions = tuple(
            ServiceConclusion(st, STATUS_UNKNOWN, (), ()) for st in query.service_types
        )
        return QueryResult(matched=False, beneficiary=None, conclusions=conclusions)
    ben = matches[0]
    conclusions = tuple(_conclude(ben, st, query) for st in query.service_types)
    return QueryResult(matched=True, beneficiary=ben, conclusions=conclusions)
