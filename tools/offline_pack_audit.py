"""Offline data-package audit tool.

Reconciles several offline data packages against registries (path, hash,
partial sizes), processor definitions (dependencies and parameter versions),
archive total-size constraints, and a local cache, then computes:

* lower/upper bounds on the total bytes that must be downloaded so that all
  declared claims hold simultaneously,
* a verifiable material allocation (which original serves which packages,
  which processed products still need to be generated),
* conflicts when the declared claims are mutually contradictory.

Design rules (mirroring the audit requirements):

* Originals shared between packages are accounted once, keyed by content
  hash.  A same-name cache entry that was not hash-verified is never
  deducted.
* Processed products are keyed by (original hash, processor, parameter
  version).  Sharing an original does NOT share products across versions.
* A cached usable product whose original was reclaimed is reported as
  usable; the download need of the original is reported separately.
* Unknown sizes may carry ranges; archive declarations add total-size
  constraints over groups of members.  Bounds are the exact min/max of the
  download sum subject to all claims (archive groups must be disjoint).
* Contradictory claims are listed as conflicts; no negative remaining
  budget is ever emitted.

Usage:
    python tools/offline_pack_audit.py manifest.json [--json]
"""

from __future__ import annotations

import json
import sys

INF = float("inf")


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------

def parse_size(spec, where, conflicts):
    """Normalise a size declaration to a [lo, hi] pair (hi may be INF)."""
    if spec is None:
        return 0, INF
    if isinstance(spec, (int, float)):
        if spec < 0:
            conflicts.append(f"{where}: 声明了负的大小 {spec}")
            return 0, INF
        return spec, spec
    if isinstance(spec, (list, tuple)) and len(spec) == 2:
        lo = 0 if spec[0] is None else spec[0]
        hi = INF if spec[1] is None else spec[1]
        if lo < 0 or hi < 0 or lo > hi:
            conflicts.append(f"{where}: 非法的大小范围 {spec}")
            return 0, INF
        return lo, hi
    conflicts.append(f"{where}: 无法解析的大小声明 {spec!r}")
    return 0, INF


def fmt_size(value):
    return "unbounded" if value == INF else value


# ---------------------------------------------------------------------------
# registry merge
# ---------------------------------------------------------------------------

class FileRec:
    """One physical original, keyed by content hash."""

    def __init__(self, digest):
        self.hash = digest
        self.paths = set()
        self.packages = set()
        self.lo = 0
        self.hi = INF
        self.size_claims = []      # human readable, for conflict reports
        self.retain = False        # any package needs the raw file
        self.needed = False        # must be present locally (retain or regenerate)
        self.cache_hit = False     # verified cache original covers it
        self.archive = None        # archive name, if any

    @property
    def download(self):
        return self.needed and not self.cache_hit


def merge_registries(manifest, conflicts, warnings):
    files = {}          # hash -> FileRec
    path_hashes = {}    # path -> set of hashes
    for package in manifest.get("packages", []):
        pname = package.get("name", "<unnamed>")
        for entry in package.get("registry", []):
            path = entry.get("path")
            digest = entry.get("hash")
            if not path or not digest:
                conflicts.append(f"包 {pname}: 注册表条目缺少 path 或 hash: {entry!r}")
                continue
            rec = files.setdefault(digest, FileRec(digest))
            rec.paths.add(path)
            rec.packages.add(pname)
            path_hashes.setdefault(path, set()).add(digest)
            lo, hi = parse_size(entry.get("size"), f"{pname}:{path}", conflicts)
            rec.size_claims.append((pname, path, lo, hi))
            rec.lo = max(rec.lo, lo)
            rec.hi = min(rec.hi, hi)
            if entry.get("retain", True):
                rec.retain = True
    for digest, rec in files.items():
        if rec.lo > rec.hi:
            claims = ", ".join(
                f"{p}:{path} 声明 [{fmt_size(lo)}, {fmt_size(hi)}]"
                for p, path, lo, hi in rec.size_claims
            )
            conflicts.append(
                f"原件 {digest} 的大小声明互相矛盾（交集为空）: {claims}"
            )
    for path, digests in sorted(path_hashes.items()):
        if len(digests) > 1:
            warnings.append(
                f"路径 {path} 在不同包中对应不同摘要 {sorted(digests)}，"
                "按不同原件分别核账"
            )
    return files, path_hashes


# ---------------------------------------------------------------------------
# processors / products
# ---------------------------------------------------------------------------

def resolve_products(manifest, files, conflicts, warnings):
    """Expand required products with dependency closure and classify them.

    Returns (to_generate, usable, product_cache) where each of the first two
    is a list of dicts, and product_cache maps (hash, name, version) -> bool.
    """
    proc_defs = {}
    for proc in manifest.get("processors", []):
        proc_defs[(proc.get("name"), proc.get("version"))] = [
            (d.get("name"), d.get("version"))
            for d in proc.get("depends_on", [])
        ]

    product_cache = {}
    for prod in manifest.get("cache", {}).get("products", []):
        key = (prod.get("original_hash"), prod.get("processor"), prod.get("version"))
        product_cache[key] = bool(prod.get("usable", True))

    hash_to_rec = files
    to_generate = []
    usable = []
    for package in manifest.get("packages", []):
        pname = package.get("name", "<unnamed>")
        for entry in package.get("registry", []):
            digest = entry.get("hash")
            path = entry.get("path")
            requested = [
                (p.get("processor"), p.get("version"))
                for p in entry.get("products", [])
            ]
            if not requested:
                continue
            # dependency closure over (processor, version)
            closure = []
            seen = set()
            stack = list(requested)
            while stack:
                key = stack.pop()
                if key in seen:
                    continue
                seen.add(key)
                closure.append(key)
                if key in proc_defs:
                    stack.extend(proc_defs[key])
                else:
                    warnings.append(
                        f"包 {pname}: 处理器 {key[0]}@{key[1]} 未在 processors 中定义，"
                        "按无依赖处理"
                    )
            for name, version in closure:
                cached = product_cache.get((digest, name, version), False)
                item = {
                    "package": pname,
                    "path": path,
                    "original_hash": digest,
                    "processor": name,
                    "version": version,
                }
                if cached:
                    usable.append(item)
                else:
                    deps = proc_defs.get((name, version), [])
                    item["depends_on"] = [
                        {"processor": dname, "version": dver,
                         "satisfied_from_cache": product_cache.get(
                             (digest, dname, dver), False)}
                        for dname, dver in deps
                    ]
                    to_generate.append(item)
                    rec = hash_to_rec.get(digest)
                    if rec is not None and not deps:
                        # A product without dependencies consumes the original
                        # directly, so the original must be (re)downloaded.
                        # Products whose inputs come from cache-satisfied
                        # dependencies never touch the original.
                        rec.needed = True
    return to_generate, usable, product_cache


# ---------------------------------------------------------------------------
# cache reconciliation
# ---------------------------------------------------------------------------

def apply_cache(manifest, files, warnings):
    not_deducted = []
    verified_by_hash = {}
    for entry in manifest.get("cache", {}).get("originals", []):
        if entry.get("verified"):
            verified_by_hash[entry.get("hash")] = entry
    needed_paths = set()
    for rec in files.values():
        rec.needed = rec.needed or rec.retain
        if rec.needed:
            needed_paths.update(rec.paths)
    for entry in manifest.get("cache", {}).get("originals", []):
        path = entry.get("path")
        digest = entry.get("hash")
        if entry.get("verified"):
            rec = files.get(digest)
            if rec is not None and rec.needed:
                rec.cache_hit = True
            elif digest not in files:
                warnings.append(f"已验证缓存 {path} ({digest}) 不在任何注册表中，忽略")
        else:
            if path in needed_paths:
                not_deducted.append({
                    "path": path,
                    "reason": "同名缓存未经过摘要验证，不能抵扣下载量",
                })
    return not_deducted


# ---------------------------------------------------------------------------
# archive groups and bounds
# ---------------------------------------------------------------------------

def build_groups(manifest, files, path_hashes, conflicts, warnings):
    """Return (groups, grouped_hashes).

    groups: list of dicts with members (FileRec list) and constraint
    (kind, value) where kind is 'eq' | 'le' | 'ge'.
    """
    groups = []
    grouped = {}
    for archive in manifest.get("archives", []):
        name = archive.get("name", "<archive>")
        members = []
        for path in archive.get("members", []):
            digests = path_hashes.get(path)
            if not digests:
                warnings.append(
                    f"压缩包 {name} 的成员 {path} 不在任何注册表中，"
                    "按 [0, unbounded] 的未知原件处理"
                )
                rec = FileRec(f"{name}:{path}")
                rec.paths.add(path)
                members.append(rec)
                continue
            if len(digests) > 1:
                conflicts.append(
                    f"压缩包 {name} 的成员 {path} 对应多个摘要 "
                    f"{sorted(digests)}，无法确定总量约束作用对象"
                )
                continue
            members.append(files[next(iter(digests))])
        kinds = []
        if archive.get("total_size") is not None:
            kinds.append(("eq", archive["total_size"]))
        if archive.get("max_total_size") is not None:
            kinds.append(("le", archive["max_total_size"]))
        if archive.get("min_total_size") is not None:
            kinds.append(("ge", archive["min_total_size"]))
        if not kinds:
            warnings.append(f"压缩包 {name} 没有总量约束，忽略")
            continue
        for rec in members:
            if rec.hash in grouped and grouped[rec.hash] != name:
                conflicts.append(
                    f"原件 {rec.hash} 同时出现在压缩包 {grouped[rec.hash]} "
                    f"和 {name} 中，成员分组必须互不相交"
                )
            grouped[rec.hash] = name
            rec.archive = name
        for kind, value in kinds:
            groups.append({"name": name, "kind": kind, "value": value,
                           "members": members})
    return groups, grouped


def distribute(amount, recs, priority_needed):
    """Greedily raise member sizes from their lo, honouring hi.

    Returns dict hash -> allocated size.  Files with priority get capacity
    first; a file with hi = INF absorbs any remaining amount.
    """
    order = sorted(
        recs,
        key=lambda r: (r.needed != priority_needed, sorted(r.paths)[0]),
    )
    alloc = {id(r): r.lo for r in recs}
    rest = amount
    for rec in order:
        if rest <= 0:
            break
        room = INF if rec.hi == INF else rec.hi - rec.lo
        take = min(room, rest)
        alloc[id(rec)] += take
        rest -= take
    return alloc


def group_bounds(group, conflicts):
    """Exact min/max of the to-download sum inside one archive group."""
    name, kind, total = group["name"], group["kind"], group["value"]
    members = group["members"]
    sum_lo = sum(r.lo for r in members)
    sum_hi = sum(r.hi for r in members)
    feasible = True
    if kind in ("eq", "le") and sum_lo > total:
        feasible = False
    if kind in ("eq", "ge") and sum_hi < total:
        feasible = False
    if not feasible:
        member_desc = ", ".join(
            f"{sorted(r.paths)[0]}[{fmt_size(r.lo)}, {fmt_size(r.hi)}]"
            for r in members
        )
        op = {"eq": "=", "le": "<=", "ge": ">="}[kind]
        conflicts.append(
            f"压缩包 {name} 的总量约束 sum {op} {total} 与成员大小范围矛盾"
            f"（成员下限和 {fmt_size(sum_lo)}，上限和 {fmt_size(sum_hi)}）: "
            f"{member_desc}"
        )
        return None

    if kind in ("eq", "ge"):
        raise_min = max(0, total - sum_lo)
    else:  # le
        raise_min = 0
    alloc_min = distribute(raise_min, members, priority_needed=False)

    if kind == "ge":
        alloc_max = {id(r): r.hi for r in members}
    else:
        alloc_max = distribute(total - sum_lo, members, priority_needed=True)

    need_min = sum(a for r, a in ((r, alloc_min[id(r)]) for r in members) if r.needed)
    need_max = sum(a for r, a in ((r, alloc_max[id(r)]) for r in members) if r.needed)
    return {"alloc_min": alloc_min, "alloc_max": alloc_max,
            "need_min": need_min, "need_max": need_max}


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

def build_report(manifest):
    conflicts = []
    warnings = []

    files, path_hashes = merge_registries(manifest, conflicts, warnings)
    to_generate, usable, _ = resolve_products(manifest, files, conflicts, warnings)
    not_deducted = apply_cache(manifest, files, warnings)
    groups, grouped = build_groups(manifest, files, path_hashes, conflicts, warnings)

    report = {
        "status": "ok",
        "conflicts": conflicts,
        "warnings": warnings,
        "products": {
            "to_generate": to_generate,
            "usable_from_cache": [
                {**item,
                 "original_downloaded": bool(
                     files.get(item["original_hash"])
                     and files[item["original_hash"]].download)}
                for item in usable
            ],
        },
        "cache_not_deducted": not_deducted,
    }

    # Evaluate every archive group even if some claims already contradict
    # each other, so that all conflicts are reported in one pass.
    group_results = {}
    for group in groups:
        res = group_bounds(group, conflicts)
        if res is not None:
            group_results[group["name"], group["kind"], group["value"]] = res

    if conflicts:
        # Contradictory claims: list conflicts, never emit bounds or a
        # (possibly negative) remaining budget.
        report["status"] = "conflict"
        report["download_bytes"] = None
        report["budget"] = None
        return report

    alloc_min = {}
    alloc_max = {}
    total_min = 0
    total_max = 0
    for group in groups:
        key = (group["name"], group["kind"], group["value"])
        res = group_results[key]
        total_min += res["need_min"]
        total_max += res["need_max"]
        alloc_min.update(res["alloc_min"])
        alloc_max.update(res["alloc_max"])
    for rec in files.values():
        if rec.hash in grouped:
            continue
        alloc_min[id(rec)] = rec.lo
        alloc_max[id(rec)] = rec.hi
        if rec.download:
            total_min += rec.lo
            total_max += rec.hi

    file_entries = []
    shared = []
    for digest in sorted(files):
        rec = files[digest]
        entry = {
            "hash": digest,
            "paths": sorted(rec.paths),
            "packages": sorted(rec.packages),
            "size_range": [rec.lo, fmt_size(rec.hi)],
            "download": rec.download,
            "cache": "verified-hit" if rec.cache_hit else None,
            "archive": rec.archive,
            "alloc_min": alloc_min.get(id(rec)),
            "alloc_max": (None if alloc_max.get(id(rec)) == INF
                          else alloc_max.get(id(rec))),
        }
        file_entries.append(entry)
        if rec.download and len(rec.packages) > 1:
            shared.append({"hash": digest, "paths": sorted(rec.paths),
                           "packages": sorted(rec.packages)})

    report["download_bytes"] = {"min": total_min, "max": fmt_size(total_max)}
    report["files"] = file_entries
    report["shared_originals"] = shared

    budget = manifest.get("budget_bytes")
    if budget is not None:
        remaining_best = budget - total_min
        remaining_worst = None if total_max == INF else budget - total_max
        budget_info = {
            "budget_bytes": budget,
            "remaining_best": max(0, remaining_best),
            "remaining_worst": (None if remaining_worst is None
                                else max(0, remaining_worst)),
        }
        if remaining_best < 0:
            conflicts.append(
                f"预算不足：最少需下载 {total_min} 字节，超过预算 {budget}，"
                f"缺口 {total_min - budget}"
            )
            report["status"] = "conflict"
        elif remaining_worst is not None and remaining_worst < 0:
            budget_info["shortfall_worst"] = -remaining_worst
            warnings.append(
                f"预算 {budget} 仅够最好情形；最坏情形缺口 {-remaining_worst} 字节"
            )
        elif total_max == INF:
            budget_info["shortfall_worst"] = "unbounded"
            warnings.append("下载量上界无界，预算无法覆盖最坏情形")
        report["budget"] = budget_info
    else:
        report["budget"] = None

    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def print_summary(report):
    status = "通过" if report["status"] == "ok" else "存在冲突"
    print(f"核账状态: {status}")
    if report["conflicts"]:
        print("冲突:")
        for c in report["conflicts"]:
            print(f"  - {c}")
    if report["download_bytes"]:
        bounds = report["download_bytes"]
        print(f"下载总量: 下限 {bounds['min']} 字节, 上限 {bounds['max']} 字节")
    if report.get("budget"):
        b = report["budget"]
        print(f"预算 {b['budget_bytes']} 字节, "
              f"剩余: 最好 {b['remaining_best']}, 最坏 {b['remaining_worst']}")
        if b.get("shortfall_worst"):
            print(f"  最坏情形缺口: {b['shortfall_worst']} 字节")
    if report.get("shared_originals"):
        print("共享原件（只下载一次）:")
        for s in report["shared_originals"]:
            print(f"  - {s['hash']} <- {', '.join(s['paths'])} "
                  f"(包: {', '.join(s['packages'])})")
    if report["products"]["to_generate"]:
        print("待生成处理产物:")
        for p in report["products"]["to_generate"]:
            print(f"  - [{p['package']}] {p['path']} -> "
                  f"{p['processor']}@{p['version']}")
    if report["products"]["usable_from_cache"]:
        print("可直接使用的缓存产物（下载需求另计）:")
        for p in report["products"]["usable_from_cache"]:
            note = "原件需另行下载" if p["original_downloaded"] else "原件已回收，无需下载"
            print(f"  - [{p['package']}] {p['path']} -> "
                  f"{p['processor']}@{p['version']} ({note})")
    if report["cache_not_deducted"]:
        print("未抵扣的缓存:")
        for c in report["cache_not_deducted"]:
            print(f"  - {c['path']}: {c['reason']}")
    if report["warnings"]:
        print("警告:")
        for w in report["warnings"]:
            print(f"  - {w}")


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    with open(argv[0], encoding="utf-8") as fobj:
        manifest = json.load(fobj)
    report = build_report(manifest)
    if "--json" in argv[1:]:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        print_summary(report)
    return 1 if report["status"] == "conflict" else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
