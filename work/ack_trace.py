"""Trace 999 acknowledgements back to the originating X12 transaction.

Two modes:
  analyze   - run pyx12 validation on a source transaction and map every
              IK3/IK4 finding back to its source segment / element / component,
              annotated with the owning 837 claim (CLM01) and service line (LX01).
  reconcile - parse an externally supplied 999 and verify each IK3/IK4 pointer
              against the original transaction. A pointer whose position does
              not exist in the original is reported as an acknowledgement
              conflict; same-named segments are never substituted.

Disposition distinction:
  local errors  - the transaction structure is complete and only segment /
                  element-level findings exist (spec IK501 = E, accepted with
                  errors; note pyx12 itself emits R*5).
  rejected      - transaction / group / interchange level structural faults
                  (missing ST/SE/GE/IEA, unreadable envelope), IK501 = R with
                  no locatable IK3/IK4 body.
"""

from __future__ import annotations

import argparse
import io
import json
from dataclasses import dataclass, field
from typing import Any

import pyx12.error_codes
import pyx12.params
import pyx12.segment
import pyx12.x12file
import pyx12.x12n_document

ENVELOPE_SEGS = {"ISA", "IEA", "GS", "GE", "ST", "SE"}
# Segments that end the current 2300 claim / 2400 service line context.
CLAIM_RESET_SEGS = {"HL"}
LINE_OPEN_SEG = "LX"
CLAIM_OPEN_SEG = "CLM"


@dataclass
class SourceSegment:
    seg_id: str
    position: int  # IK302 numbering: ST is position 1
    line_no: int
    seg: pyx12.segment.Segment
    claim_id: str | None
    line_id: str | None
    st_control_num: str | None


@dataclass
class TransactionIndex:
    trn_set_id: str | None
    st_control_num: str | None
    vriic: str | None
    segments: list[SourceSegment] = field(default_factory=list)

    def segment_at(self, position: int) -> SourceSegment | None:
        for seg in self.segments:
            if seg.position == position:
                return seg
        return None

    def max_position(self) -> int:
        return max((s.position for s in self.segments), default=0)


def index_source(src_file: Any) -> list[TransactionIndex]:
    """Index every body segment with IK302 position, source line and 837 context."""
    reader = pyx12.x12file.X12Reader(src_file)
    transactions: list[TransactionIndex] = []
    current: TransactionIndex | None = None
    counter = 0
    cur_claim: str | None = None
    cur_line: str | None = None
    for seg in reader:
        seg_id = seg.get_seg_id()
        if seg_id == "ST":
            counter = 1  # ST itself is position 1 in IK302 numbering
            cur_claim = None
            cur_line = None
            current = TransactionIndex(
                trn_set_id=seg.get_value("ST01"),
                st_control_num=seg.get_value("ST02"),
                vriic=seg.get_value("ST03"),
            )
            transactions.append(current)
            continue
        if seg_id in ENVELOPE_SEGS:
            if seg_id == "SE":
                current = None
            continue
        counter += 1
        if seg_id in CLAIM_RESET_SEGS:
            cur_claim = None
            cur_line = None
        elif seg_id == CLAIM_OPEN_SEG:
            cur_claim = seg.get_value("CLM01")
            cur_line = None
        elif seg_id == LINE_OPEN_SEG:
            cur_line = seg.get_value("LX01")
        assert current is not None
        current.segments.append(
            SourceSegment(
                seg_id=seg_id,
                position=counter,
                line_no=reader.get_cur_line(),
                seg=seg,
                claim_id=cur_claim,
                line_id=cur_line,
                st_control_num=current.st_control_num,
            )
        )
    return transactions


def _element_value(seg: pyx12.segment.Segment, ele_pos: int, subele_pos: int | None) -> str | None:
    if ele_pos < 1 or ele_pos > len(seg):
        return None
    composite = seg.elements[ele_pos - 1]
    if subele_pos is None:
        return composite.format()
    if subele_pos < 1 or subele_pos > len(composite):
        return None
    return composite[subele_pos - 1].format()


def _code_description(err_cde: str) -> str:
    spec = pyx12.error_codes.ERROR_CODES.get(err_cde)
    if spec is None:
        return err_cde
    return f"{spec.x12_description} ({spec.ik_code or spec.ak_code or '-'})"


def _classify_disposition(st_node: dict[str, Any]) -> tuple[str, str]:
    has_structural_fault = bool(st_node.get("errors"))
    has_findings = bool(st_node.get("segments"))
    if has_structural_fault:
        return "rejected", "transaction-level structural fault: " + "; ".join(
            f"{e['err_cde']} ({e['err_str']})" for e in st_node["errors"]
        )
    if has_findings:
        return (
            "local_errors",
            "structure complete; segment/element findings only "
            "(spec IK501=E accepted with errors; pyx12 emits R*5)",
        )
    return "accepted", "no findings"


def analyze(param: Any, src_file: Any) -> dict[str, Any]:
    fd_999 = io.StringIO()
    fd_json = io.StringIO()
    pyx12.x12n_document.x12n_document(
        param=param,
        src_file=src_file,
        fd_997=fd_999,
        fd_html=None,
        fd_xmldoc=None,
        fd_json=fd_json,
    )
    fd_json.seek(0)
    error_doc = json.loads(fd_json.read())
    fd_999.seek(0)
    ack_text = fd_999.read()

    src_file.seek(0)
    transactions = {t.st_control_num: t for t in index_source(src_file)}

    report: dict[str, Any] = {
        "ack_999": ack_text.strip().splitlines(),
        "envelope_errors": [],
        "transactions": [],
    }
    for isa in error_doc["interchanges"]:
        for err in isa["errors"]:
            report["envelope_errors"].append(
                {
                    "level": "interchange",
                    "control_num": isa["isa_trn_set_id"],
                    "code": err["err_cde"],
                    "x12_code": err.get("x12_code"),
                    "message": err["err_str"],
                }
            )
        for group in isa["groups"]:
            for err in group["errors"]:
                report["envelope_errors"].append(
                    {
                        "level": "group",
                        "control_num": group["gs_control_num"],
                        "code": err["err_cde"],
                        "x12_code": err.get("x12_code"),
                        "message": err["err_str"],
                    }
                )
            if not group["transactions"]:
                report["transactions"].append(
                    {
                        "trn_set_id": None,
                        "st_control_num": None,
                        "ack_code_emitted": group["ack_code"],
                        "disposition": "rejected",
                        "disposition_reason": (
                            "no transaction set accepted in functional group "
                            f"{group['gs_control_num']} (group/interchange fault)"
                        ),
                        "findings": [],
                    }
                )
            for st_node in group["transactions"]:
                control = st_node["trn_set_control_num"]
                index = transactions.get(control)
                disposition, reason = _classify_disposition(st_node)
                txn_report: dict[str, Any] = {
                    "trn_set_id": st_node["trn_set_id"],
                    "st_control_num": control,
                    "ack_code_emitted": st_node["ack_code"],
                    "disposition": disposition,
                    "disposition_reason": reason,
                    "findings": [],
                }
                for seg_node in st_node["segments"]:
                    txn_report["findings"].append(_trace_segment_finding(index, seg_node))
                report["transactions"].append(txn_report)
    return report


def _trace_segment_finding(
    index: TransactionIndex | None, seg_node: dict[str, Any]
) -> dict[str, Any]:
    position = seg_node["seg_count"]
    finding: dict[str, Any] = {
        "reported_seg_id": seg_node["seg_id"],
        "ik302_position": position,
        "map_name": seg_node.get("name"),
        "segment_errors": [
            {"code": e["err_cde"], "meaning": _code_description(e["err_cde"])}
            for e in seg_node["errors"]
        ],
        "element_findings": [],
        "conflict": None,
        "source": None,
    }
    if index is None:
        finding["conflict"] = "transaction not found in original"
        return finding
    target = index.segment_at(position)
    if target is None:
        finding["conflict"] = (
            f"ack references position {position} but the original transaction "
            f"ends at position {index.max_position()}"
        )
        return finding
    if target.seg_id != seg_node["seg_id"]:
        finding["conflict"] = (
            f"position {position} is {target.seg_id} in the original, "
            f"ack claims {seg_node['seg_id']}"
        )
        return finding
    finding["source"] = {
        "seg_id": target.seg_id,
        "position": target.position,
        "line_no": target.line_no,
        "claim_id": target.claim_id,
        "service_line_id": target.line_id,
        "raw_segment": target.seg.format(),
    }
    for ele_node in seg_node["elements"]:
        ele_pos = ele_node["ele_pos"]
        subele_pos = ele_node.get("subele_pos")
        actual = _element_value(target.seg, ele_pos, subele_pos)
        ref = f"{target.seg_id}{ele_pos:02d}" + (f"-{subele_pos}" if subele_pos else "")
        ele_finding: dict[str, Any] = {
            "refdes": ref,
            "element_position": ele_pos,
            "component_position": subele_pos,
            "element_ref_num": ele_node.get("ele_ref_num"),
            "element_name": ele_node.get("name"),
            "claim_id": target.claim_id,
            "service_line_id": target.line_id,
            "errors": [
                {"code": e["err_cde"], "meaning": _code_description(e["err_cde"])}
                for e in ele_node["errors"]
            ],
            "actual_value": actual,
            "conflict": None,
        }
        if actual is None:
            ele_finding["conflict"] = (
                f"{ref} does not exist in the original {target.seg_id} segment"
            )
        finding["element_findings"].append(ele_finding)
    return finding


def reconcile(src_file: Any, ack_file: Any) -> dict[str, Any]:
    src_index = {t.st_control_num: t for t in index_source(src_file)}
    reader = pyx12.x12file.X12Reader(ack_file)
    report: dict[str, Any] = {"transactions": []}
    current: dict[str, Any] | None = None
    current_ik3: dict[str, Any] | None = None
    for seg in reader:
        seg_id = seg.get_seg_id()
        if seg_id == "AK2":
            current = {
                "trn_set_id": seg.get_value("AK201"),
                "st_control_num": seg.get_value("AK202"),
                "ik3_findings": [],
                "ik5": None,
            }
            report["transactions"].append(current)
            current_ik3 = None
        elif seg_id == "IK3" and current is not None:
            current_ik3 = {
                "reported_seg_id": seg.get_value("IK301"),
                "ik302_position": int(seg.get_value("IK302")),
                "ik304_code": seg.get_value("IK304"),
                "ik4_findings": [],
                "conflict": None,
                "source": None,
            }
            current["ik3_findings"].append(current_ik3)
            _resolve_ik3(src_index, current["st_control_num"], current_ik3)
        elif seg_id == "IK4" and current_ik3 is not None:
            current_ik3["ik4_findings"].append(_resolve_ik4(current_ik3, seg))
        elif seg_id == "IK5" and current is not None:
            current["ik5"] = {
                "ack_code": seg.get_value("IK501"),
                "error_codes": [
                    seg.get_value(f"IK5{i:02d}")
                    for i in range(2, 7)
                    if seg.get_value(f"IK5{i:02d}")
                ],
            }
            current = None
            current_ik3 = None
    return report


def _resolve_ik3(
    src_index: dict[str, TransactionIndex | None],
    st_control_num: str | None,
    ik3: dict[str, Any],
) -> None:
    index = src_index.get(st_control_num)
    if index is None:
        ik3["conflict"] = f"transaction {st_control_num} not found in original"
        return
    target = index.segment_at(ik3["ik302_position"])
    if target is None:
        ik3["conflict"] = (
            f"IK3 points past the original: position {ik3['ik302_position']} "
            f"does not exist (original ends at {index.max_position()}); "
            "no same-named segment is substituted"
        )
        return
    if target.seg_id != ik3["reported_seg_id"]:
        ik3["conflict"] = (
            f"IK3 seg id conflict: position {ik3['ik302_position']} is "
            f"{target.seg_id} in the original, ack says {ik3['reported_seg_id']}"
        )
        return
    ik3["source"] = {
        "seg_id": target.seg_id,
        "position": target.position,
        "line_no": target.line_no,
        "claim_id": target.claim_id,
        "service_line_id": target.line_id,
        "raw_segment": target.seg.format(),
    }


def _resolve_ik4(ik3: dict[str, Any], seg: pyx12.segment.Segment) -> dict[str, Any]:
    pos_composite = seg.get_value("IK401")
    parts = pos_composite.split(":") if pos_composite else []
    ele_pos = int(parts[0]) if parts else None
    subele_pos = int(parts[1]) if len(parts) > 1 and parts[1] else None
    finding: dict[str, Any] = {
        "element_position": ele_pos,
        "component_position": subele_pos,
        "element_ref_num": seg.get_value("IK402"),
        "ik403_code": seg.get_value("IK403"),
        "bad_value_reported": seg.get_value("IK404"),
        "conflict": None,
        "value_mismatch": None,
    }
    source = ik3.get("source")
    if source is None:
        finding["conflict"] = "parent IK3 unresolved; cannot locate element"
        return finding
    target_seg = pyx12.segment.Segment(source["raw_segment"], "~", "*", ":")
    actual = _element_value(target_seg, ele_pos or 0, subele_pos)
    finding["actual_value_in_original"] = actual
    if actual is None:
        ref = f"{source['seg_id']}{ele_pos:02d}" + (f"-{subele_pos}" if subele_pos else "")
        finding["conflict"] = f"IK4 points past the original: {ref} does not exist"
        return finding
    reported = finding["bad_value_reported"]
    if reported is not None and reported != actual:
        finding["value_mismatch"] = (
            f"IK404 reports {reported!r} but the original holds {actual!r} "
            "(may reflect ack character sanitization)"
        )
    return finding


def render_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    if "ack_999" in report:
        lines.append("Generated 999:")
        lines.extend(f"  {row}" for row in report["ack_999"])
        lines.append("")
    for err in report.get("envelope_errors", []):
        lines.append(
            f"Envelope {err['level']} {err['control_num']}: "
            f"{err['code']} ({err.get('x12_code')}) {err['message']}"
        )
    if report.get("envelope_errors"):
        lines.append("")
    for txn in report["transactions"]:
        lines.append(f"Transaction {txn.get('trn_set_id')} control {txn.get('st_control_num')}")
        if "disposition" in txn:
            lines.append(f"  disposition: {txn['disposition']} - {txn['disposition_reason']}")
            for finding in txn["findings"]:
                lines.extend(_render_finding(finding))
        if txn.get("ik5"):
            lines.append(f"  IK5: {txn['ik5']}")
        for finding in txn.get("ik3_findings", []):
            lines.extend(_render_ik3(finding))
        lines.append("")
    return "\n".join(lines)


def _render_finding(finding: dict[str, Any]) -> list[str]:
    out = [f"  IK3 {finding['reported_seg_id']} at position {finding['ik302_position']}"]
    if finding["conflict"]:
        out.append(f"    CONFLICT: {finding['conflict']}")
        return out
    src = finding["source"]
    out.append(
        f"    source: line {src['line_no']}, {src['seg_id']}#{src['position']}, "
        f"claim={src['claim_id']}, service_line={src['service_line_id']}"
    )
    out.append(f"    raw: {src['raw_segment']}")
    for err in finding["segment_errors"]:
        out.append(f"    segment error: {err['meaning']}")
    for ele in finding["element_findings"]:
        loc = ele["refdes"]
        if ele["component_position"]:
            loc += f" component {ele['component_position']}"
        line = (
            f"    IK4 {loc} (data element {ele['element_ref_num']}, "
            f"{ele['element_name']}) claim={ele['claim_id']} "
            f"line={ele['service_line_id']} value={ele['actual_value']!r}"
        )
        out.append(line)
        for err in ele["errors"]:
            out.append(f"      -> {err['meaning']}")
        if ele["conflict"]:
            out.append(f"      CONFLICT: {ele['conflict']}")
    return out


def _render_ik3(finding: dict[str, Any]) -> list[str]:
    out = [
        f"  IK3 {finding['reported_seg_id']} at position {finding['ik302_position']} "
        f"code={finding['ik304_code']}"
    ]
    if finding["conflict"]:
        out.append(f"    CONFLICT: {finding['conflict']}")
    if finding["source"]:
        src = finding["source"]
        out.append(
            f"    source: line {src['line_no']}, {src['seg_id']}#{src['position']}, "
            f"claim={src['claim_id']}, service_line={src['service_line_id']}"
        )
        out.append(f"    raw: {src['raw_segment']}")
    for ik4 in finding["ik4_findings"]:
        out.append(
            f"    IK4 element={ik4['element_position']} component={ik4['component_position']} "
            f"ref={ik4['element_ref_num']} code={ik4['ik403_code']} "
            f"reported={ik4['bad_value_reported']!r} actual={ik4.get('actual_value_in_original')!r}"
        )
        if ik4["conflict"]:
            out.append(f"      CONFLICT: {ik4['conflict']}")
        if ik4.get("value_mismatch"):
            out.append(f"      VALUE MISMATCH: {ik4['value_mismatch']}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    p_analyze = sub.add_parser("analyze")
    p_analyze.add_argument("source")
    p_analyze.add_argument("--json-out")
    p_reconcile = sub.add_parser("reconcile")
    p_reconcile.add_argument("source")
    p_reconcile.add_argument("ack999")
    p_reconcile.add_argument("--json-out")
    args = parser.parse_args()

    if args.mode == "analyze":
        param = pyx12.params.params()
        with open(args.source, encoding="ascii") as src:
            report = analyze(param, src)
    else:
        with open(args.source, encoding="ascii") as src, open(args.ack999, encoding="ascii") as ack:
            report = reconcile(src, ack)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as out:
            json.dump(report, out, indent=2, ensure_ascii=False)
    print(render_text(report))


if __name__ == "__main__":
    main()
