"""Split an 835 remittance into reconcilable claim / service-line / adjustment tables."""

import argparse
import csv
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO

import pyx12.error_handler
import pyx12.params
import pyx12.x12context

ZERO = Decimal("0")

CAS_TRIPLES = [(2, 3, 4), (5, 6, 7), (8, 9, 10), (11, 12, 13), (14, 15, 16), (17, 18, 19)]
PLB_PAIRS = [(3, 4), (5, 6), (7, 8), (9, 10), (11, 12), (13, 14)]


@dataclass
class Adjustment:
    level: str  # "claim" | "line" | "provider"
    group_code: str | None
    reason_code: str | None
    amount: Decimal | None
    quantity: str | None = None


@dataclass
class ServiceLine:
    number: int
    procedure: str | None
    charge: Decimal | None
    paid: Decimal | None
    units: str | None
    adjustments: list[Adjustment] = field(default_factory=list)


@dataclass
class Claim:
    claim_id: str | None
    status: str | None
    charge: Decimal | None
    paid: Decimal | None
    patient_resp: Decimal | None
    payer_claim_control: str | None
    adjustments: list[Adjustment] = field(default_factory=list)
    lines: list[ServiceLine] = field(default_factory=list)


@dataclass
class Payment:
    trace_number: str | None
    amount: Decimal | None
    adjustments: list[Adjustment] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)


@dataclass
class Remit:
    payments: list[Payment] = field(default_factory=list)


def _money(value: str | None) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(value)


def _sum_money(amounts: Any) -> Decimal:
    total = ZERO
    for amount in amounts:
        if amount is not None:
            total += amount
    return total


def _parse_cas(seg_data: Any, level: str) -> list[Adjustment]:
    group_code = seg_data.get_value("CAS01")
    adjustments = []
    for reason_idx, amount_idx, qty_idx in CAS_TRIPLES:
        reason = seg_data.get_value("CAS%02d" % reason_idx)
        if reason is None or reason == "":
            continue
        adjustments.append(
            Adjustment(
                level=level,
                group_code=group_code,
                reason_code=reason,
                amount=_money(seg_data.get_value("CAS%02d" % amount_idx)),
                quantity=seg_data.get_value("CAS%02d" % qty_idx),
            )
        )
    return adjustments


def _parse_plb(seg_data: Any) -> list[Adjustment]:
    adjustments = []
    for reason_idx, amount_idx in PLB_PAIRS:
        reason = seg_data.get_value("PLB%02d" % reason_idx)
        if reason is None or reason == "":
            continue
        adjustments.append(
            Adjustment(
                level="provider",
                group_code=None,
                reason_code=reason,
                amount=_money(seg_data.get_value("PLB%02d" % amount_idx)),
            )
        )
    return adjustments


def _parse_service_line(svc_node: Any, number: int) -> ServiceLine:
    line = ServiceLine(
        number=number,
        procedure=svc_node.get_value("SVC01"),
        charge=_money(svc_node.get_value("SVC02")),
        paid=_money(svc_node.get_value("SVC03")),
        units=svc_node.get_value("SVC05"),
    )
    for child in svc_node.children:
        if child.type == "seg" and child.seg_data.get_seg_id() == "CAS":
            line.adjustments.extend(_parse_cas(child.seg_data, "line"))
    return line


def _parse_claim(claim_node: Any) -> Claim:
    claim = Claim(
        claim_id=claim_node.get_value("CLP01"),
        status=claim_node.get_value("CLP02"),
        charge=_money(claim_node.get_value("CLP03")),
        paid=_money(claim_node.get_value("CLP04")),
        patient_resp=_money(claim_node.get_value("CLP05")),
        payer_claim_control=claim_node.get_value("CLP07"),
    )
    for svc_node in claim_node.select("2110"):
        claim.lines.append(_parse_service_line(svc_node, len(claim.lines) + 1))
    for child in claim_node.children:
        if child.type == "seg" and child.seg_data.get_seg_id() == "CAS":
            claim.adjustments.extend(_parse_cas(child.seg_data, "claim"))
    return claim


def parse_remit(fd: TextIO) -> Remit:
    param = pyx12.params.params()
    errh = pyx12.error_handler.errh_null()
    src = pyx12.x12context.X12ContextReader(param, errh, fd)
    remit = Remit()
    for datatree in src.iter_segments("ST_LOOP"):
        if datatree.id != "ST_LOOP":
            continue
        payment = Payment(
            trace_number=datatree.get_value("HEADER/TRN02"),
            amount=_money(datatree.get_value("HEADER/BPR02")),
        )
        for plb_node in datatree.select("FOOTER/PLB"):
            for seg in plb_node.iterate_segments():
                if seg["segment"].get_seg_id() == "PLB":
                    payment.adjustments.extend(_parse_plb(seg["segment"]))
        for claim_node in datatree.select("DETAIL/2000/2100"):
            payment.claims.append(_parse_claim(claim_node))
        remit.payments.append(payment)
    return remit


def reconcile_claim(claim: Claim) -> dict[str, Any]:
    gap = (claim.charge or ZERO) - (claim.paid or ZERO) - (claim.patient_resp or ZERO)
    claim_adj = _sum_money(adj.amount for adj in claim.adjustments)
    recon: dict[str, Any] = {
        "claim_id": claim.claim_id,
        "total_gap": gap,
        "claim_adjustments": claim_adj,
        "line_adjustments": None,
        "line_unexplained": None,
        "level_mismatch": None,
        "unexplained_residual": None,
    }
    sources = []
    if claim_adj != ZERO:
        sources.append("claim_adjustment")
    if claim.lines:
        line_gap = _sum_money((line.charge or ZERO) - (line.paid or ZERO) for line in claim.lines)
        line_adj = _sum_money(adj.amount for line in claim.lines for adj in line.adjustments)
        line_unexplained = line_gap - line_adj
        level_mismatch = (gap - claim_adj) - line_gap
        recon["line_adjustments"] = line_adj
        recon["line_unexplained"] = line_unexplained
        recon["level_mismatch"] = level_mismatch
        if line_adj != ZERO:
            sources.append("line_adjustment")
        if line_unexplained != ZERO:
            sources.append("line_unexplained")
        if level_mismatch != ZERO:
            sources.append("level_mismatch")
    else:
        residual = gap - claim_adj
        recon["unexplained_residual"] = residual
        if residual != ZERO:
            sources.append("no_service_line_detail")
    recon["difference_sources"] = ";".join(sources) if sources else "balanced"
    return recon


def reconcile_payment(payment: Payment) -> dict[str, Any]:
    claims_paid = _sum_money(claim.paid for claim in payment.claims)
    provider_adj = _sum_money(adj.amount for adj in payment.adjustments)
    gap = (payment.amount or ZERO) - claims_paid - provider_adj
    sources = []
    if provider_adj != ZERO:
        sources.append("provider_adjustment")
    if gap != ZERO:
        sources.append("payment_unexplained")
    return {
        "trace_number": payment.trace_number,
        "payment_amount": payment.amount,
        "claims_paid_total": claims_paid,
        "provider_adjustments": provider_adj,
        "unexplained_residual": gap,
        "difference_sources": ";".join(sources) if sources else "balanced",
    }


def _fmt(amount: Decimal | None) -> str:
    return "" if amount is None else str(amount)


def write_tables(remit: Remit, outdir: Path) -> list[Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    paths = {
        name: outdir / f"{name}.csv"
        for name in ("claims", "service_lines", "adjustments", "reconciliation")
    }
    with open(paths["claims"], "w", newline="", encoding="utf-8") as fd:
        writer = csv.writer(fd)
        writer.writerow(
            [
                "claim_id",
                "status",
                "charge",
                "paid",
                "patient_resp",
                "payer_claim_control",
                "claim_adj_total",
                "line_count",
                "line_charge_total",
                "line_paid_total",
                "line_adj_total",
            ]
        )
        for payment in remit.payments:
            for claim in payment.claims:
                has_lines = bool(claim.lines)
                writer.writerow(
                    [
                        claim.claim_id,
                        claim.status,
                        _fmt(claim.charge),
                        _fmt(claim.paid),
                        _fmt(claim.patient_resp),
                        claim.payer_claim_control,
                        _fmt(_sum_money(adj.amount for adj in claim.adjustments)),
                        len(claim.lines),
                        _fmt(_sum_money(line.charge for line in claim.lines)) if has_lines else "",
                        _fmt(_sum_money(line.paid for line in claim.lines)) if has_lines else "",
                        _fmt(
                            _sum_money(
                                adj.amount for line in claim.lines for adj in line.adjustments
                            )
                        )
                        if has_lines
                        else "",
                    ]
                )
    with open(paths["service_lines"], "w", newline="", encoding="utf-8") as fd:
        writer = csv.writer(fd)
        writer.writerow(
            [
                "claim_id",
                "line_number",
                "procedure",
                "charge",
                "paid",
                "units",
                "line_adj_total",
                "line_unexplained",
            ]
        )
        for payment in remit.payments:
            for claim in payment.claims:
                for line in claim.lines:
                    line_adj = _sum_money(adj.amount for adj in line.adjustments)
                    line_unexplained = (line.charge or ZERO) - (line.paid or ZERO) - line_adj
                    writer.writerow(
                        [
                            claim.claim_id,
                            line.number,
                            line.procedure,
                            _fmt(line.charge),
                            _fmt(line.paid),
                            line.units,
                            _fmt(line_adj),
                            _fmt(line_unexplained),
                        ]
                    )
    with open(paths["adjustments"], "w", newline="", encoding="utf-8") as fd:
        writer = csv.writer(fd)
        writer.writerow(
            [
                "level",
                "claim_id",
                "line_number",
                "group_code",
                "reason_code",
                "amount",
                "quantity",
            ]
        )
        for payment in remit.payments:
            for adj in payment.adjustments:
                writer.writerow(["provider", "", "", "", adj.reason_code, _fmt(adj.amount), ""])
            for claim in payment.claims:
                for adj in claim.adjustments:
                    writer.writerow(
                        [
                            "claim",
                            claim.claim_id,
                            "",
                            adj.group_code,
                            adj.reason_code,
                            _fmt(adj.amount),
                            adj.quantity or "",
                        ]
                    )
                for line in claim.lines:
                    for adj in line.adjustments:
                        writer.writerow(
                            [
                                "line",
                                claim.claim_id,
                                line.number,
                                adj.group_code,
                                adj.reason_code,
                                _fmt(adj.amount),
                                adj.quantity or "",
                            ]
                        )
    with open(paths["reconciliation"], "w", newline="", encoding="utf-8") as fd:
        writer = csv.writer(fd)
        writer.writerow(
            [
                "row_type",
                "ref_id",
                "total_gap",
                "claim_adjustments",
                "line_adjustments",
                "line_unexplained",
                "level_mismatch",
                "provider_adjustments",
                "unexplained_residual",
                "difference_sources",
            ]
        )
        for payment in remit.payments:
            for claim in payment.claims:
                recon = reconcile_claim(claim)
                writer.writerow(
                    [
                        "claim",
                        recon["claim_id"],
                        _fmt(recon["total_gap"]),
                        _fmt(recon["claim_adjustments"]),
                        _fmt(recon["line_adjustments"]),
                        _fmt(recon["line_unexplained"]),
                        _fmt(recon["level_mismatch"]),
                        "",
                        _fmt(recon["unexplained_residual"]),
                        recon["difference_sources"],
                    ]
                )
            pay_recon = reconcile_payment(payment)
            writer.writerow(
                [
                    "payment",
                    pay_recon["trace_number"],
                    _fmt(pay_recon["unexplained_residual"]),
                    "",
                    "",
                    "",
                    "",
                    _fmt(pay_recon["provider_adjustments"]),
                    _fmt(pay_recon["unexplained_residual"]),
                    pay_recon["difference_sources"],
                ]
            )
    return list(paths.values())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Split an 835 remittance into reconcilable CSV tables"
    )
    parser.add_argument("input", help="Path to the 835 X12 file")
    parser.add_argument("-o", "--outdir", default=".", help="Output directory for CSV tables")
    args = parser.parse_args(argv)
    with open(args.input, encoding="ascii") as fd:
        remit = parse_remit(fd)
    for table_path in write_tables(remit, Path(args.outdir)):
        print(table_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
