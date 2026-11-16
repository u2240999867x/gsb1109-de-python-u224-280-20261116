######################################################################
#   John Holland <john@zoner.org>
# All rights reserved.
#
# This software is licensed as described in the file LICENSE.txt, which
# you should have received as part of this distribution.
#
######################################################################

"""
Split oversized 837 transaction sets into smaller valid ones.

A receiver may cap the number of segments accepted per 837 transaction
set.  This module splits a large set into several smaller sets without
breaking a single claim (CLM) apart: claims are assigned to parts in
their original order, and the subscriber / patient HL context each part
needs is copied into it.  HL references and envelope counts (SE01, GE01,
IEA01) are rebuilt so every part stands alone under the implementation
guide.  A claim that on its own exceeds the limit is emitted as its own
transaction set, whole, rather than having service lines moved out.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TextIO

from pyx12.rawx12file import RawX12File


@dataclass
class _HLNode:
    ele: list[str]
    own: list[str] = field(default_factory=list)
    parent: str = ""


@dataclass
class _Claim:
    node_id: str
    segs: list[str] = field(default_factory=list)


@dataclass
class _TransactionSet:
    header: list[str] = field(default_factory=list)
    nodes: dict[str, _HLNode] = field(default_factory=dict)
    items: list[tuple[str, str | int]] = field(default_factory=list)
    claims: list[_Claim] = field(default_factory=list)
    trailer: list[str] | None = None


def _parse_transaction_set(segs: list[str], ele_term: str) -> _TransactionSet:
    ts = _TransactionSet()
    cur_node: _HLNode | None = None
    cur_claim: _Claim | None = None
    seen_hl = False
    for seg in segs:
        ele = seg.split(ele_term)
        seg_id = ele[0]
        if seg_id == "SE":
            ts.trailer = ele
        elif seg_id == "HL":
            seen_hl = True
            node = _HLNode(ele=ele, parent=ele[2] if len(ele) > 2 else "")
            ts.nodes[ele[1]] = node
            ts.items.append(("node", ele[1]))
            cur_node = node
            cur_claim = None
        elif not seen_hl:
            ts.header.append(seg)
        elif seg_id == "CLM":
            ts.claims.append(_Claim(node_id=cur_node.ele[1] if cur_node else "", segs=[seg]))
            ts.items.append(("claim", len(ts.claims) - 1))
            cur_claim = ts.claims[-1]
        elif cur_claim is not None:
            cur_claim.segs.append(seg)
        elif cur_node is not None:
            cur_node.own.append(seg)
    return ts


def _chain(nodes: dict[str, _HLNode], node_id: str) -> list[str]:
    chain = []
    cur = node_id
    while cur and cur in nodes:
        chain.append(cur)
        cur = nodes[cur].parent
    chain.reverse()
    return chain


def _free_numeric(used: set[str]) -> str:
    num = 1
    while str(num) in used:
        num += 1
    return str(num)


def _fresh_control_number(orig: str, offset: int, used: set[str]) -> str:
    if orig.isdigit():
        while True:
            cand = str(int(orig) + offset).zfill(len(orig))
            if len(cand) > 9:
                break
            if cand not in used:
                return cand
            offset += 1
    elif len(orig) < 9:
        while True:
            cand = orig + str(offset)
            if len(cand) > 9:
                break
            if cand not in used:
                return cand
            offset += 1
    return _free_numeric(used)


def _build_part(ts: _TransactionSet, claim_idxs: list[int], st02: str, ele_term: str) -> list[str]:
    claim_set = set(claim_idxs)
    needed: set[str] = set()
    for idx in claim_idxs:
        needed.update(_chain(ts.nodes, ts.claims[idx].node_id))
    has_child = {ts.nodes[nid].parent for nid in needed if ts.nodes[nid].parent in needed}
    new_id: dict[str, str] = {}
    out = []
    st = ts.header[0].split(ele_term)
    st[2] = st02
    out.append(ele_term.join(st))
    out.extend(ts.header[1:])
    for kind, ref in ts.items:
        if kind == "node":
            if ref not in needed:
                continue
            node = ts.nodes[ref]
            new_id[ref] = str(len(new_id) + 1)
            ele = list(node.ele)
            ele[1] = new_id[ref]
            if len(ele) > 2:
                ele[2] = new_id.get(node.parent, "")
            if len(ele) > 4:
                ele[4] = "1" if ref in has_child else "0"
            out.append(ele_term.join(ele))
            out.extend(node.own)
        elif ref in claim_set:
            out.extend(ts.claims[ref].segs)
    se = list(ts.trailer) if ts.trailer else ["SE"]
    while len(se) < 3:
        se.append("")
    se[1] = str(len(out) + 1)
    se[2] = st02
    out.append(ele_term.join(se))
    return out


def split_transaction_set(
    segs: list[str],
    max_segments: int,
    ele_term: str = "*",
    used_control_numbers: set[str] | None = None,
) -> list[list[str]]:
    """
    Split one 837 transaction set (ST through SE, without terminators) into
    parts of at most max_segments segments each, counting ST through SE.
    Claims stay whole and in original order; HL context is copied per part.
    A single claim that exceeds the limit is emitted alone, unsplit.
    """
    if not segs or segs[0].split(ele_term)[0] != "ST":
        raise ValueError("segs must start with an ST segment")
    ts = _parse_transaction_set(segs, ele_term)
    if ts.trailer is None:
        raise ValueError("segs must end with an SE segment")
    if len(segs) <= max_segments or not ts.claims:
        return [list(segs)]
    base = len(ts.header) + 1
    parts: list[list[int]] = []
    cur: list[int] = []
    cur_nodes: set[str] = set()
    cur_size = base
    for idx, claim in enumerate(ts.claims):
        chain = _chain(ts.nodes, claim.node_id)
        add = len(claim.segs) + sum(
            1 + len(ts.nodes[nid].own) for nid in chain if nid not in cur_nodes
        )
        if cur and cur_size + add > max_segments:
            parts.append(cur)
            cur, cur_nodes, cur_size = [], set(), base
            add = len(claim.segs) + sum(1 + len(ts.nodes[nid].own) for nid in chain)
        cur.append(idx)
        cur_nodes.update(chain)
        cur_size += add
    if cur:
        parts.append(cur)
    if len(parts) <= 1:
        return [list(segs)]
    orig_st02 = segs[0].split(ele_term)[2] if len(segs[0].split(ele_term)) > 2 else ""
    used = used_control_numbers if used_control_numbers is not None else set()
    result = []
    for i, idxs in enumerate(parts):
        if i == 0 and orig_st02 not in used:
            st02 = orig_st02
        else:
            st02 = _fresh_control_number(orig_st02, i, used)
        used.add(st02)
        result.append(_build_part(ts, idxs, st02, ele_term))
    return result


def split_document(fin: TextIO, fout: TextIO, max_segments: int) -> list[str]:
    """
    Split oversized 837 transaction sets read from fin and write the
    document to fout, rebuilding GE01 / IEA01 counts.  Returns warnings
    for 837 parts that still exceed max_segments because one claim alone
    is too large to fit.
    """
    reader = RawX12File(fin)
    seg_term, ele_term, _subele, _eol, _rep = reader.get_term()
    warnings: list[str] = []
    out: list[str] = []
    st_buf: list[str] | None = None
    st02_used: set[str] = set()
    st_count = 0
    gs_count = 0

    def flush_st() -> None:
        nonlocal st_buf, st_count
        assert st_buf is not None
        segs, st_buf = st_buf, None
        ele = segs[0].split(ele_term)
        st01 = ele[1] if len(ele) > 1 else ""
        if st01 == "837" and len(segs) > max_segments:
            parts = split_transaction_set(segs, max_segments, ele_term, st02_used)
            if len(parts) == 1:
                segs = parts[0]
            else:
                for part in parts:
                    emit(part, st01)
                return
        st02 = ele[2] if len(ele) > 2 else ""
        if st02 in st02_used:
            st02 = _fresh_control_number(st02, 1, st02_used)
            st = segs[0].split(ele_term)
            st[2] = st02
            segs[0] = ele_term.join(st)
            se = segs[-1].split(ele_term)
            if se[0] == "SE" and len(se) > 2:
                se[2] = st02
                segs[-1] = ele_term.join(se)
        st02_used.add(st02)
        emit(segs, st01)

    def emit(part: list[str], st01: str) -> None:
        nonlocal st_count
        st_count += 1
        if st01 == "837" and len(part) > max_segments:
            part_st02 = part[0].split(ele_term)[2]
            warnings.append(
                f"Transaction set {part_st02} has {len(part)} segments, over the "
                f"{max_segments} limit; a single claim cannot be split"
            )
        out.extend(part)

    for seg in reader:
        ele = seg.split(ele_term)
        seg_id = ele[0]
        if st_buf is not None:
            st_buf.append(seg)
            if seg_id == "SE":
                flush_st()
        elif seg_id == "ST":
            st_buf = [seg]
        elif seg_id == "GS":
            gs_count += 1
            st_count = 0
            st02_used.clear()
            out.append(seg)
        elif seg_id == "GE":
            ele[1] = str(st_count)
            out.append(ele_term.join(ele))
        elif seg_id == "IEA":
            ele[1] = str(gs_count)
            out.append(ele_term.join(ele))
        else:
            out.append(seg)
    if st_buf is not None:
        out.extend(st_buf)
    fout.write(seg_term.join(out) + seg_term)
    return warnings
