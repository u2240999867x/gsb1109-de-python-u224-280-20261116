#!/usr/bin/env python3
"""Minimum-cost OCR correction of scanned archive dates.

Model
-----
Input is a JSON spec with allowed languages, an inclusive date range, a list
of substitution rules (single character -> single character, each with a
cost, digits and punctuation alike), and the scanned rows. Each row carries
the print batch it belongs to.

Constraints and objective:
- One shared rule set per print batch: for every source character at
  most one rule may be active within a batch, and the active set is common
  to all rows of that batch.
- Each occurrence of an active rule's source character may be substituted or
  left untouched; every substitution pays the rule's cost.
- A correction is legal only if dateparser reads the corrected text in one
  of the allowed languages and the date falls inside the range.
- Verified rows are never modified. A rule whose source character appears in
  a verified row of the same batch cannot be activated for that batch,
  because the verified text is ground truth and must stay stable under the
  shared rule set.
- Batches are optimized lexicographically: first minimize the number of
  failed rows (a row fails only when no legal date is reachable), then the
  total substitution cost.
- When several optima tie, only corrections confirmed by every optimal
  solution are applied automatically; differing rows keep their candidate
  list. Every change, punctuation included, points back to the original
  character and position.
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from dataclasses import dataclass, field
from datetime import date

import dateparser


@dataclass(frozen=True)
class Rule:
    rid: str
    src: str
    dst: str
    cost: int


@dataclass
class Row:
    rid: str
    batch: str
    text: str
    verified: bool = False
    verified_date: str | None = None


@dataclass(frozen=True)
class Change:
    pos: int
    original: str
    replacement: str
    rule: str
    cost: int


@dataclass
class Candidate:
    text: str
    cost: int
    changes: tuple
    dates: tuple  # ((language, iso-date), ...)


class DateValidator:
    def __init__(self, languages, start: date, end: date):
        self.languages = languages
        self.start = start
        self.end = end
        self._cache = {}

    def check(self, text: str):
        if text in self._cache:
            return self._cache[text]
        hits = []
        for lang in self.languages:
            try:
                parsed = dateparser.parse(text, languages=[lang])
            except Exception:
                parsed = None
            if parsed is not None and self.start <= parsed.date() <= self.end:
                hits.append((lang, parsed.date().isoformat()))
        result = tuple(hits)
        self._cache[text] = result
        return result


def row_corrections(text: str, active_rules, validator: DateValidator):
    """All minimum-cost legal corrections of one row under the active rules."""
    rule_by_pos = []
    for pos, ch in enumerate(text):
        for rule in active_rules:
            if rule.src == ch:
                rule_by_pos.append((pos, rule))
                break
    best_cost = None
    best = []
    for mask in range(1 << len(rule_by_pos)):
        chars = list(text)
        changes = []
        cost = 0
        for bit, (pos, rule) in enumerate(rule_by_pos):
            if mask >> bit & 1:
                chars[pos] = rule.dst
                changes.append(Change(pos, rule.src, rule.dst, rule.rid, rule.cost))
                cost += rule.cost
        if best_cost is not None and cost > best_cost:
            continue
        candidate = "".join(chars)
        dates = validator.check(candidate)
        if not dates:
            continue
        if best_cost is None or cost < best_cost:
            best_cost = cost
            best = []
        best.append(Candidate(candidate, cost, tuple(changes), dates))
    unique = {}
    for cand in best:
        unique.setdefault(cand.text, cand)
    return list(unique.values())


def solve_batch(rows, rules, validator):
    batch_text = "".join(row.text for row in rows)
    relevant = [rule for rule in rules if rule.src in batch_text]
    by_src = {}
    for rule in relevant:
        by_src.setdefault(rule.src, []).append(rule)
    verified_text = "".join(row.text for row in rows if row.verified)
    options = []
    choice_lists = [[None] + by_src[src] for src in sorted(by_src)]
    for combo in itertools.product(*choice_lists):
        active = [rule for rule in combo if rule is not None]
        if any(rule.src in verified_text for rule in active):
            continue
        failed = 0
        total_cost = 0
        per_row = {}
        for row in rows:
            if row.verified:
                per_row[row.rid] = [
                    Candidate(row.text, 0, (), (("verified", row.verified_date),))
                ]
                continue
            sols = row_corrections(row.text, active, validator)
            per_row[row.rid] = sols
            if sols:
                total_cost += sols[0].cost
            else:
                failed += 1
        options.append({"key": (failed, total_cost), "rules": active, "rows": per_row})
    best_key = min(opt["key"] for opt in options)
    best = [opt for opt in options if opt["key"] == best_key]
    distinct = {}
    for opt in best:
        signature = tuple(
            (rid, tuple(sorted(cand.text for cand in opt["rows"][rid])))
            for rid in sorted(opt["rows"])
        )
        distinct.setdefault(signature, opt)
    return best_key, list(distinct.values())


def aggregate_row(best_options, rid):
    """Merge a row's outcome across all tied optimal solutions."""
    texts = {}
    for opt in best_options:
        for cand in opt["rows"][rid]:
            texts.setdefault(cand.text, cand)
    return list(texts.values())


def solve(spec):
    languages = spec["languages"]
    start = date.fromisoformat(spec["date_range"]["start"])
    end = date.fromisoformat(spec["date_range"]["end"])
    validator = DateValidator(languages, start, end)
    rules = [
        Rule(r["id"], r["from"], r["to"], r["cost"]) for r in spec.get("rules", [])
    ]
    for rule in rules:
        if len(rule.src) != 1 or len(rule.dst) > 1:
            raise ValueError(
                f"rule {rule.rid}: single-character source, "
                "single-character or empty (deletion) target"
            )
    rows = [
        Row(
            r["id"],
            r["batch"],
            r["text"],
            bool(r.get("verified", False)),
            r.get("verified_date"),
        )
        for r in spec["rows"]
    ]
    batches = {}
    for row in rows:
        batches.setdefault(row.batch, []).append(row)
    rule_by_id = {rule.rid: rule for rule in rules}

    report_rows = []
    batch_summaries = []
    grand_cost = 0
    grand_failed = 0
    for batch_name, batch_rows in batches.items():
        (failed, cost), best_options = solve_batch(batch_rows, rules, validator)
        grand_cost += cost
        grand_failed += failed
        applied = {}
        for opt in best_options:
            for cands in opt["rows"].values():
                for cand in cands:
                    for change in cand.changes:
                        applied[change.rule] = rule_by_id[change.rule]
        batch_summaries.append(
            {
                "batch": batch_name,
                "failed_rows": failed,
                "cost": cost,
                "optimal_solutions": len(best_options),
                "shared_rules": [
                    {"id": r.rid, "from": r.src, "to": r.dst, "cost": r.cost}
                    for r in sorted(applied.values(), key=lambda r: r.rid)
                ],
            }
        )
        for row in batch_rows:
            if row.verified:
                report_rows.append(
                    {
                        "id": row.rid,
                        "batch": batch_name,
                        "original": row.text,
                        "status": "verified",
                        "correction": row.text,
                        "date": row.verified_date,
                        "candidates": [],
                        "evidence": ["已核对行，按约束不作任何改动"],
                    }
                )
                continue
            merged = aggregate_row(best_options, row.rid)
            if not merged:
                report_rows.append(
                    {
                        "id": row.rid,
                        "batch": batch_name,
                        "original": row.text,
                        "status": "failed",
                        "correction": None,
                        "date": None,
                        "candidates": [],
                        "evidence": ["在批次共享规则下不存在落在允许范围内的合法日期"],
                    }
                )
                continue
            status = "auto" if len(merged) == 1 else "candidates"
            entry = {
                "id": row.rid,
                "batch": batch_name,
                "original": row.text,
                "status": status,
                "correction": merged[0].text if status == "auto" else None,
                "date": merged[0].dates[0][1] if status == "auto" else None,
                "candidates": [],
                "evidence": [],
            }
            for cand in sorted(merged, key=lambda c: c.text):
                changes = [
                    {
                        "pos": ch.pos,
                        "original": ch.original,
                        "replacement": ch.replacement,
                        "rule": ch.rule,
                        "cost": ch.cost,
                    }
                    for ch in cand.changes
                ]
                langs = sorted({lang for lang, _ in cand.dates})
                dates = sorted({d for _, d in cand.dates})
                entry["candidates"].append(
                    {
                        "text": cand.text,
                        "cost": cand.cost,
                        "changes": changes,
                        "languages": langs,
                        "dates": dates,
                    }
                )
                if status == "auto":
                    evidence = []
                    for ch in changes:
                        target = repr(ch["replacement"]) if ch["replacement"] else "∅（删除）"
                        evidence.append(
                            f"位置 {ch['pos']}: {ch['original']!r} → {target}"
                            f"（规则 {ch['rule']}，成本 {ch['cost']}）"
                        )
                    entry["evidence"] = evidence or [
                        "原文本身即为合法日期，无需替换（成本 0）"
                    ]
                    entry["evidence"].append(
                        f"dateparser 以语言 {langs} 解析为 {dates}，落在允许范围内"
                    )
                    entry["evidence"].append(f"该行替换总成本 {cand.cost}")
            if status == "candidates":
                entry["evidence"] = [
                    "存在多个总成本相同的最优解释，仅列出候选，不自动采用"
                ]
            report_rows.append(entry)
    return {
        "total_cost": grand_cost,
        "failed_rows": grand_failed,
        "batches": batch_summaries,
        "rows": report_rows,
    }


def render_markdown(result):
    def show(change):
        target = repr(change["replacement"]) if change["replacement"] else "∅（删除）"
        return (
            f"位置 {change['pos']}: {change['original']!r} → {target}"
            f"（规则 {change['rule']}，成本 {change['cost']}）"
        )

    lines = ["# 扫描日期 OCR 修正报告", ""]
    lines.append(f"- 总成本：{result['total_cost']}")
    lines.append(f"- 失败行数：{result['failed_rows']}")
    lines.append("")
    lines.append("## 批次汇总")
    lines.append("")
    for batch in result["batches"]:
        rules = (
            "、".join(
                f"{r['id']}({r['from']}→{r['to'] or '∅'}, 成本{r['cost']})"
                for r in batch["shared_rules"]
            )
            or "无"
        )
        lines.append(
            f"- 批次 {batch['batch']}：成本 {batch['cost']}，"
            f"失败 {batch['failed_rows']} 行，并列最优解 {batch['optimal_solutions']} 个，"
            f"共用规则：{rules}"
        )
    lines.append("")
    lines.append("## 逐行结果")
    lines.append("")
    status_label = {
        "auto": "自动修正",
        "candidates": "候选（并列最优，未自动采用）",
        "failed": "失败（无合法日期）",
        "verified": "已核对（不改动）",
    }
    for row in result["rows"]:
        lines.append(f"### 行 {row['id']}（批次 {row['batch']}）")
        lines.append("")
        lines.append(f"- 原文：`{row['original']}`")
        lines.append(f"- 状态：{status_label[row['status']]}")
        if row["status"] == "auto":
            lines.append(f"- 修正：`{row['correction']}` → {row['date']}")
        if row["status"] == "candidates":
            for cand in row["candidates"]:
                lines.append(
                    f"- 候选：`{cand['text']}` → {', '.join(cand['dates'])}"
                    f"（成本 {cand['cost']}）"
                )
        lines.append("- 依据：")
        for ev in row["evidence"]:
            lines.append(f"  - {ev}")
        for cand in row["candidates"] if row["status"] == "candidates" else []:
            for ch in cand["changes"]:
                lines.append(f"  - 候选 `{cand['text']}` {show(ch)}")
        lines.append("")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("spec", help="JSON spec: languages, date_range, rules, rows")
    parser.add_argument("--json", dest="json_out", help="write machine-readable result")
    parser.add_argument("--report", help="write Markdown report")
    args = parser.parse_args(argv)

    with open(args.spec, encoding="utf-8") as fh:
        spec = json.load(fh)
    result = solve(spec)
    markdown = render_markdown(result)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(markdown + "\n")
    if not args.json_out and not args.report:
        print(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
