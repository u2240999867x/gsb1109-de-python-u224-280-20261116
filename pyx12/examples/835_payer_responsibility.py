"""Build a per-payer responsibility table from an 835 Claim Payment/Advice.

One 835 is sent by a single payer; when a claim crosses over to another payer
(COB), that secondary payer is named by the claim-level ``NM1*TT`` (Crossover
Carrier Name) loop inside ``2100``. Payer order is therefore an explicit
responsibility order, never the order segments happen to appear in the file:

* primary payer   - the 835 sender (header ``N1*PR``), unless the claim carries
                    a Corrected Priority Payer (``NM1*PR``) that supersedes it
* secondary payer - the Crossover Carrier (``NM1*TT``)

Amount conventions follow X221: the primary payer's adjudication is the CLP/SVC
payment and the ``CO``/``OA``/``PI`` adjustment groups; the secondary payer's
share is the patient-responsibility group (``PR``) plus any prior-payer
amounts carried in ``AMT*ZK``/``ZL``/``ZM``/``ZN``/``ZO``. A missing primary
payment result is reported as missing, never treated as zero and re-adjudicated
against the secondary, and claims whose payer-order signals disagree are listed
separately instead of being silently ordered by file position.
"""

from __future__ import annotations

import collections
from collections.abc import Iterable
from decimal import Decimal
from typing import Any

import pyx12.error_handler
import pyx12.params
import pyx12.x12context

PRIMARY = "P"
SECONDARY = "S"
UNRESOLVED = "U"

_ORDER_RANK = {PRIMARY: 1, SECONDARY: 2, UNRESOLVED: 9}

# Claim- and service-level AMT qualifiers that state amounts adjudicated by a
# payer other than the 835 sender (COB prior/remaining-payer amounts, X221 5010).
_PRIOR_PAYER_AMT_QUALIFIERS = ("ZK", "ZL", "ZM", "ZN", "ZO")
_ADJUSTMENT_GROUPS = ("CO", "OA", "PI", "PR")
_PATIENT_RESPONSIBILITY_GROUP = "PR"


def _money(value: str | None) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(value)


def _raw(seg_node: Any) -> str:
    return seg_node.seg_data.format().rstrip(seg_node.seg_data.seg_term)


def _seg_ref(item: dict[str, Any]) -> dict[str, Any]:
    seg_data = item["segment"]
    return {
        "segment_id": item["id"],
        "raw": seg_data.format().rstrip(seg_data.seg_term),
        "line_number": item["cur_line_number"],
    }


def _select_segments(node: Any, seg_id: str) -> list[Any]:
    # Filter on the data segment id: when the input violates loop ordering the
    # walker can attach a segment to the wrong map node, so a map-based select
    # alone is not trustworthy for responsibility ordering.
    return [
        n
        for n in node.select(seg_id)
        if n.seg_data is not None and n.seg_data.get_seg_id() == seg_id
    ]


def _cas_entries(cas_nodes: Iterable[Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for cas in cas_nodes:
        # One CAS segment carries up to six reason/amount pairs at positions
        # (02,03), (05,06), (08,09), (11,12), (14,15), (17,18).
        for reason_idx, amount_idx in ((2, 3), (5, 6), (8, 9), (11, 12), (14, 15), (17, 18)):
            reason = cas.get_value(f"CAS{reason_idx:02d}")
            amount = _money(cas.get_value(f"CAS{amount_idx:02d}"))
            if reason is None and amount is None:
                continue
            entries.append(
                {
                    "group_code": cas.get_value("CAS01"),
                    "reason_code": reason or "",
                    "amount": amount,
                    "source_segment": _raw(cas),
                    "line_number": cas.cur_line_number,
                }
            )
    return entries


def _sum_group(entries: list[dict[str, Any]], group_code: str) -> Decimal | None:
    amounts = [
        e["amount"] for e in entries if e["group_code"] == group_code and e["amount"] is not None
    ]
    if not amounts:
        return None
    return sum(amounts, Decimal("0"))


def _prior_payer_amounts(amt_nodes: Iterable[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for amt in amt_nodes:
        qualifier = amt.get_value("AMT01")
        if qualifier not in _PRIOR_PAYER_AMT_QUALIFIERS:
            continue
        rows.append(
            {
                "qualifier": qualifier,
                "amount": _money(amt.get_value("AMT02")),
                "source_segment": _raw(amt),
                "line_number": amt.cur_line_number,
            }
        )
    return rows


def _payer_name(nm1_node: Any) -> dict[str, Any]:
    return {
        "entity_code": nm1_node.get_value("NM101"),
        "name": nm1_node.get_value("NM103") or "",
        "id_qualifier": nm1_node.get_value("NM108"),
        "identifier": nm1_node.get_value("NM109"),
        "source_segment": _raw(nm1_node),
        "line_number": nm1_node.cur_line_number,
    }


def _sender_payer(n1_node: Any) -> dict[str, Any]:
    return {
        "entity_code": n1_node.get_value("N101"),
        "name": n1_node.get_value("N102") or "",
        "id_qualifier": n1_node.get_value("N103"),
        "identifier": n1_node.get_value("N104"),
        "source_segment": _raw(n1_node),
        "line_number": n1_node.cur_line_number,
    }


def _service_rows(claim_node: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for svc_loop in claim_node.select("2110"):
        svc = svc_loop.first("SVC")
        cas_entries = _cas_entries(_select_segments(svc_loop, "CAS"))
        amts = _prior_payer_amounts(_select_segments(svc_loop, "AMT"))
        rows.append(
            {
                "service_id": svc_loop.get_value("SVC01-02"),
                "line_item_charge": _money(svc_loop.get_value("SVC02")),
                "primary_paid": _money(svc_loop.get_value("SVC03")),
                "units_paid": svc_loop.get_value("SVC05"),
                "adjustments": cas_entries,
                "adjustment_totals_by_group": {
                    group: _sum_group(cas_entries, group) for group in _ADJUSTMENT_GROUPS
                },
                "patient_responsibility": _sum_group(cas_entries, _PATIENT_RESPONSIBILITY_GROUP),
                "prior_payer_amounts": amts,
                "source_segments": [_seg_ref(item) for item in svc_loop.iterate_segments()],
                "source_segment": _raw(svc),
                "line_number": svc.cur_line_number,
            }
        )
    return rows


def _claim_payers(
    claim_node: Any, sender_payer: dict[str, Any] | None
) -> tuple[list[dict[str, Any]], list[str]]:
    nm1_nodes = _select_segments(claim_node, "NM1")
    corrected_nodes = [n for n in nm1_nodes if n.get_value("NM101") == "PR"]
    crossover_nodes = [n for n in nm1_nodes if n.get_value("NM101") == "TT"]

    primary_candidates: list[tuple[dict[str, Any], str]] = []
    for node in corrected_nodes:
        primary_candidates.append(
            (_payer_name(node), "NM1*PR Corrected Priority Payer in 2100 loop")
        )
    if corrected_nodes and sender_payer is not None:
        sender_id = sender_payer.get("identifier")
        corrected_ids = {n.get_value("NM109") for n in corrected_nodes}
        if sender_id and sender_id not in corrected_ids:
            primary_candidates.append((dict(sender_payer), "835 sender N1*PR in 1000A header loop"))
    elif not corrected_nodes and sender_payer is not None:
        primary_candidates.append((dict(sender_payer), "835 sender N1*PR in 1000A header loop"))

    secondary_candidates = [
        (_payer_name(node), "NM1*TT Crossover Carrier in 2100 loop") for node in crossover_nodes
    ]

    contradictions: list[str] = []
    if len(primary_candidates) > 1:
        contradictions.append(
            "conflicting primary payer identity between header N1*PR and claim NM1*PR"
        )
    if len(secondary_candidates) > 1:
        contradictions.append(
            "multiple Crossover Carrier (NM1*TT) segments claim the secondary position"
        )

    payers: list[dict[str, Any]] = []
    primary_order = UNRESOLVED if len(primary_candidates) > 1 else PRIMARY
    for payer, basis in primary_candidates:
        payer["order_basis"] = basis
        payer["responsibility_order"] = primary_order
        payers.append(payer)
    secondary_order = UNRESOLVED if len(secondary_candidates) > 1 else SECONDARY
    for payer, basis in secondary_candidates:
        payer["order_basis"] = basis
        payer["responsibility_order"] = secondary_order
        payers.append(payer)

    payers.sort(key=lambda p: (_ORDER_RANK[p["responsibility_order"]], p["line_number"] or 0))
    return payers, contradictions


def _claim_row(claim_node: Any, sender_payer: dict[str, Any] | None) -> dict[str, Any]:
    payers, contradictions = _claim_payers(claim_node, sender_payer)
    cas_entries = _cas_entries(_select_segments(claim_node, "CAS"))
    amts = _prior_payer_amounts(_select_segments(claim_node, "AMT"))
    claim_segments = [_seg_ref(item) for item in claim_node.iterate_segments()]

    primary_paid = _money(claim_node.get_value("CLP04"))
    row: dict[str, Any] = collections.OrderedDict()
    row["claim_control_number"] = claim_node.get_value("CLP01")
    row["payer_claim_control_number"] = claim_node.get_value("CLP07")
    row["claim_status"] = claim_node.get_value("CLP02")
    row["total_charge"] = _money(claim_node.get_value("CLP03"))
    row["primary_paid"] = primary_paid
    row["primary_paid_present"] = claim_node.get_value("CLP04") not in (None, "")
    row["patient_responsibility_claim"] = _money(claim_node.get_value("CLP05"))
    row["prior_payer_amounts"] = amts
    row["adjustments"] = cas_entries
    row["adjustment_totals_by_group"] = {
        group: _sum_group(cas_entries, group) for group in _ADJUSTMENT_GROUPS
    }
    row["payer_responsibility"] = _payer_responsibility_rows(
        payers, claim_node, cas_entries, amts, primary_paid
    )
    row["service_lines"] = _service_rows(claim_node)
    row["payers"] = payers
    row["order_contradictions"] = contradictions
    row["source_segments"] = claim_segments
    return row


def _payer_responsibility_rows(
    payers: list[dict[str, Any]],
    claim_node: Any,
    cas_entries: list[dict[str, Any]],
    amts: list[dict[str, Any]],
    primary_paid: Decimal | None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    secondary_groups = ("CO", "OA", "PI")
    for payer in payers:
        order = payer["responsibility_order"]
        entry: dict[str, Any] = collections.OrderedDict()
        entry["responsibility_order"] = order
        entry["payer_name"] = payer["name"]
        entry["payer_identifier"] = payer["identifier"]
        entry["order_basis"] = payer["order_basis"]
        entry["payer_source_segment"] = payer["source_segment"]
        entry["payer_line_number"] = payer["line_number"]
        if order == PRIMARY:
            entry["paid_amount"] = primary_paid
            entry["paid_present"] = claim_node.get_value("CLP04") not in (None, "")
            entry["adjustment_groups"] = {
                group: _sum_group(cas_entries, group) for group in secondary_groups
            }
            entry["adjustment_entries"] = [
                e for e in cas_entries if e["group_code"] in secondary_groups
            ]
        elif order == SECONDARY:
            # The secondary payer's result is never derived from a missing
            # primary result; only explicit PR-group and prior-payer AMT data.
            entry["patient_responsibility_assumed"] = _sum_group(
                cas_entries, _PATIENT_RESPONSIBILITY_GROUP
            )
            entry["adjustment_entries"] = [
                e for e in cas_entries if e["group_code"] == _PATIENT_RESPONSIBILITY_GROUP
            ]
            entry["prior_payer_amounts"] = amts
        else:
            entry["order_unresolved"] = True
            entry["adjustment_entries"] = cas_entries
            entry["prior_payer_amounts"] = amts
        rows.append(entry)
    return rows


def build_responsibility_table(fd_in: Any) -> dict[str, Any]:
    """Parse an 835 stream and return the per-payer responsibility table."""
    param = pyx12.params.params()
    errh = pyx12.error_handler.errh_null()
    reader = pyx12.x12context.X12ContextReader(param, errh, fd_in)

    sender_payer: dict[str, Any] | None = None
    claims: list[dict[str, Any]] = []
    for datatree in reader.iter_segments("2100"):
        if datatree.id == "ST":
            sender_payer = None
        elif datatree.id == "N1" and datatree.get_value("N101") == "PR" and sender_payer is None:
            sender_payer = _sender_payer(datatree)
        elif datatree.id == "2100":
            claims.append(_claim_row(datatree, sender_payer))

    return {
        "sender_payer": sender_payer,
        "claims": claims,
        "unresolved_claims": [
            c["claim_control_number"] for c in claims if c["order_contradictions"]
        ],
    }


def main() -> bool:
    import argparse
    import json
    from decimal import Decimal

    parser = argparse.ArgumentParser(description="835 per-payer responsibility table")
    parser.add_argument("files", metavar="FILE", nargs="+", help="835 X12 input file(s)")
    args = parser.parse_args()

    for full_name in args.files:
        with open(full_name, encoding="ascii") as fd_in:
            table = build_responsibility_table(fd_in)
        print(
            json.dumps(table, indent=2, default=lambda o: str(o) if isinstance(o, Decimal) else o)
        )
    return True


if __name__ == "__main__":
    import sys

    sys.exit(not main())
