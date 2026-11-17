#!/usr/bin/env python3
"""
Migrate a Pooch data registry from the legacy digest to a new algorithm.

The migration never derives the new credential from the legacy one. For every
entry of the legacy registry the tool:

1. Locates the registered bytes in one of the local cache roots. The cache may
   hold downloaded originals alongside decompressed or converted products;
   only paths matching the registered relative name are considered.
2. Re-computes the legacy digest directly from those bytes and compares it
   with the legacy credential. This is the only use made of the old hash.
3. If the credential is complete and every cached copy matches, computes the
   new digest from the same bytes and publishes the entry.
4. Holds back entries whose credential is incomplete, whose bytes are missing
   from every cache root or whose bytes have drifted (a cached copy no longer
   hashes to the legacy credential).

Byte-identical copies of the same registered artifact (for example the same
file cached under several roots) share a single digest calculation and
verification.
"""

from __future__ import annotations

import argparse
import json
import logging
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Allow running the script directly (./tools/migrate_registry.py) without an
# editable install by making the repository root importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pooch.hashes import file_hash, hash_algorithm

logger = logging.getLogger(__name__)

LEGACY_ALGORITHM = "sha256"
NEW_ALGORITHM = "sha512"
HEX_DIGITS = set("0123456789abcdef")

STATUS_PUBLISHED = "published"
STATUS_PENDING = "pending"


@dataclass
class CacheRoot:
    """A cache root: path as given by the caller and its resolved location."""

    display: str
    path: Path


@dataclass
class CacheCopy:
    """A cached copy of a registered artifact."""

    root: str
    relative_path: str
    absolute_path: str
    size: int
    legacy_digest: str
    matches_legacy: bool

    @property
    def display_path(self) -> str:
        """Portable location, relative to the cache root argument."""
        return f"{self.root}/{self.relative_path}"


@dataclass
class EntryResult:
    """Outcome of migrating a single registry entry."""

    name: str
    line_number: int
    legacy_credential: str | None
    url: str | None
    status: str
    reason: str | None = None
    legacy_algorithm: str | None = None
    legacy_digest: str | None = None
    new_credential: str | None = None
    new_digest: str | None = None
    size: int | None = None
    copies: list[CacheCopy] = field(default_factory=list)

    def as_dict(self) -> dict:
        data = {
            "name": self.name,
            "line_number": self.line_number,
            "legacy_credential": self.legacy_credential,
            "url": self.url,
            "status": self.status,
            "reason": self.reason,
            "legacy_algorithm": self.legacy_algorithm,
            "legacy_digest": self.legacy_digest,
            "new_credential": self.new_credential,
            "new_algorithm": NEW_ALGORITHM if self.new_credential else None,
            "new_digest": self.new_digest,
            "size": self.size,
            "cache_copies": [vars(copy) for copy in self.copies],
        }
        return data


def parse_legacy_registry(path: Path) -> list[dict]:
    """
    Parse a legacy registry file using the same rules as Pooch.load_registry.

    Returns one dictionary per non-comment line. Malformed lines are kept and
    flagged so that incomplete credentials end up in the pending list instead
    of being silently dropped.
    """
    entries = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            elements = shlex.split(line)
        except ValueError as exc:
            entries.append(
                {
                    "line_number": line_number,
                    "name": None,
                    "credential": None,
                    "url": None,
                    "parse_error": f"unparseable line: {exc}",
                }
            )
            continue
        if len(elements) not in (2, 3):
            entries.append(
                {
                    "line_number": line_number,
                    "name": elements[0] if elements else None,
                    "credential": elements[1] if len(elements) > 1 else None,
                    "url": elements[2] if len(elements) > 2 else None,
                    "parse_error": (f"expected 2 or 3 elements, got {len(elements)}"),
                }
            )
            continue
        entries.append(
            {
                "line_number": line_number,
                "name": elements[0],
                "credential": elements[1],
                "url": elements[2] if len(elements) == 3 else None,
                "parse_error": None,
            }
        )
    return entries


def classify_credential(
    credential: str | None, legacy_algorithm: str
) -> tuple[str | None, str | None]:
    """
    Return (legacy digest, incompleteness reason) for a legacy credential.

    A complete credential is an explicit or implicit legacy-algorithm digest
    with the expected hex length. Anything else cannot be verified against the
    cached bytes and must be held back.
    """
    if not credential:
        return None, "missing or empty legacy credential"
    digest = credential.split(":", maxsplit=1)[-1].lower()
    if legacy_algorithm != LEGACY_ALGORITHM:
        return None, (
            f"legacy credential uses '{legacy_algorithm}', expected "
            f"'{LEGACY_ALGORITHM}'"
        )
    if len(digest) != 64 or any(char not in HEX_DIGITS for char in digest):
        return None, "legacy digest is not a 64-character hex SHA-256 value"
    return digest, None


def find_cache_copies(
    name: str, cache_roots: list[CacheRoot], legacy_digest: str
) -> list[CacheCopy]:
    """Find every cached copy of a registered entry and verify its bytes."""
    copies = []
    for root in cache_roots:
        candidate = root.path / name
        if not candidate.is_file():
            continue
        actual_digest = file_hash(str(candidate), alg=LEGACY_ALGORITHM)
        copies.append(
            CacheCopy(
                root=root.display,
                relative_path=name,
                absolute_path=str(candidate.resolve()),
                size=candidate.stat().st_size,
                legacy_digest=actual_digest,
                matches_legacy=actual_digest == legacy_digest,
            )
        )
    return copies


def migrate_entry(entry: dict, cache_roots: list[CacheRoot]) -> EntryResult:
    """Verify one legacy entry and, when safe, issue its new credential."""
    name = entry["name"]
    credential = entry["credential"]
    if entry["parse_error"]:
        return EntryResult(
            name=name or "<unparseable entry>",
            line_number=entry["line_number"],
            legacy_credential=credential,
            url=entry["url"],
            status=STATUS_PENDING,
            reason=f"incomplete credential: {entry['parse_error']}",
        )

    legacy_algorithm = hash_algorithm(credential)
    legacy_digest, reason = classify_credential(credential, legacy_algorithm)
    if reason is not None:
        return EntryResult(
            name=name,
            line_number=entry["line_number"],
            legacy_credential=credential,
            url=entry["url"],
            status=STATUS_PENDING,
            legacy_algorithm=legacy_algorithm,
            reason=reason,
        )

    copies = find_cache_copies(name, cache_roots, legacy_digest)
    if not copies:
        return EntryResult(
            name=name,
            line_number=entry["line_number"],
            legacy_credential=credential,
            url=entry["url"],
            status=STATUS_PENDING,
            legacy_algorithm=legacy_algorithm,
            legacy_digest=legacy_digest,
            reason="no cached copy found in any cache root",
        )

    drifted = [copy for copy in copies if not copy.matches_legacy]
    if drifted:
        detail = "; ".join(
            f"{copy.display_path} has {copy.legacy_digest}" for copy in drifted
        )
        return EntryResult(
            name=name,
            line_number=entry["line_number"],
            legacy_credential=credential,
            url=entry["url"],
            status=STATUS_PENDING,
            legacy_algorithm=legacy_algorithm,
            legacy_digest=legacy_digest,
            copies=copies,
            reason=f"cached bytes drifted from the legacy credential: {detail}",
        )

    reference = copies[0]
    # The new digest is computed from the verified bytes themselves; the
    # legacy digest is never transformed or used as an input here.
    new_digest = file_hash(reference.absolute_path, alg=NEW_ALGORITHM)
    return EntryResult(
        name=name,
        line_number=entry["line_number"],
        legacy_credential=credential,
        url=entry["url"],
        status=STATUS_PUBLISHED,
        legacy_algorithm=legacy_algorithm,
        legacy_digest=legacy_digest,
        new_credential=f"{NEW_ALGORITHM}:{new_digest}",
        new_digest=new_digest,
        size=reference.size,
        copies=copies,
    )


def scan_byte_identity(
    cache_roots: list[CacheRoot], results: list[EntryResult]
) -> list[dict]:
    """
    Group every cached file by its verified legacy digest.

    This makes the provenance explicit: downloaded originals and their
    decompressed or converted products that share identical bytes are listed
    together, along with the registry entries vouching for those bytes.
    """
    digest_to_paths: dict[str, list[str]] = {}
    for root in cache_roots:
        for path in sorted(path for path in root.path.rglob("*") if path.is_file()):
            digest = file_hash(str(path), alg=LEGACY_ALGORITHM)
            digest_to_paths.setdefault(digest, []).append(
                f"{root.display}/{path.relative_to(root.path)}"
            )

    entries_by_digest = {
        result.legacy_digest: result.name
        for result in results
        if result.status == STATUS_PUBLISHED and result.legacy_digest
    }
    groups = []
    for digest, paths in sorted(digest_to_paths.items()):
        groups.append(
            {
                "sha256": digest,
                "cache_paths": paths,
                "registry_entry": entries_by_digest.get(digest),
            }
        )
    return groups


def write_new_registry(results: list[EntryResult], output: Path) -> None:
    """Write the publishable registry, one verified entry per line."""
    lines = []
    for result in results:
        if result.status != STATUS_PUBLISHED:
            continue
        elements = [result.name, result.new_credential]
        if result.url:
            elements.append(result.url)
        lines.append(" ".join(elements))
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_manifest(
    results: list[EntryResult],
    identity_groups: list[dict],
    legacy_registry: str,
    cache_roots: list[CacheRoot],
    output: Path,
) -> None:
    published = [r for r in results if r.status == STATUS_PUBLISHED]
    pending = [r for r in results if r.status == STATUS_PENDING]
    manifest = {
        "legacy_registry": legacy_registry,
        "cache_roots": [root.display for root in cache_roots],
        "algorithms": {
            "legacy": LEGACY_ALGORITHM,
            "new": NEW_ALGORITHM,
        },
        "policy": (
            "new credentials are computed from the cached bytes after the "
            "legacy credential is verified against those same bytes; they "
            "are never derived from the legacy digest"
        ),
        "summary": {
            "total": len(results),
            "published": len(published),
            "pending": len(pending),
        },
        "entries": [result.as_dict() for result in results],
        "byte_identity": identity_groups,
    }
    output.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def write_report(
    results: list[EntryResult], identity_groups: list[dict], output: Path
) -> None:
    published = [r for r in results if r.status == STATUS_PUBLISHED]
    pending = [r for r in results if r.status == STATUS_PENDING]
    lines = [
        "# Registry digest migration report",
        "",
        f"- Legacy algorithm: `{LEGACY_ALGORITHM}`",
        f"- New algorithm: `{NEW_ALGORITHM}`",
        f"- Entries in the legacy registry: {len(results)}",
        f"- Published after byte verification: {len(published)}",
        f"- Held back as pending: {len(pending)}",
        "",
        "New digests are calculated from the cached bytes themselves only",
        "after each legacy credential has been re-computed from those same",
        "bytes and found to match. The new digests are never derived from the",
        "legacy digests.",
        "",
        "## Published entries",
        "",
        "| Entry | Cached copies | Size (bytes) | Legacy SHA-256 | New SHA-512 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for result in published:
        copies = ", ".join(copy.display_path for copy in result.copies)
        lines.append(
            f"| `{result.name}` | {copies} | {result.size} | "
            f"`{result.legacy_digest}` | `{result.new_digest}` |"
        )

    lines.extend(["", "## Pending entries", ""])
    if not pending:
        lines.append(
            "None. Every legacy entry had a complete credential and verified bytes."
        )
    else:
        lines.extend(
            [
                "| Entry | Reason |",
                "| --- | --- |",
            ]
        )
        for result in pending:
            lines.append(f"| `{result.name}` | {result.reason} |")

    lines.extend(
        [
            "",
            "## Byte identity across the cache",
            "",
            "Each group lists cache paths whose bytes hash to the same legacy digest.",
            "Decompressed and converted products therefore stay traceable to "
            "the registered",
            "original bytes.",
            "",
            "| SHA-256 of the bytes | Registry entry | Cache paths |",
            "| --- | --- | --- |",
        ]
    )
    for group in identity_groups:
        paths = ", ".join(f"`{path}`" for path in group["cache_paths"])
        entry = f"`{group['registry_entry']}`" if group["registry_entry"] else "-"
        lines.append(f"| `{group['sha256']}` | {entry} | {paths} |")

    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--legacy",
        required=True,
        type=Path,
        help="Path to the legacy registry text file.",
    )
    parser.add_argument(
        "--cache-root",
        required=True,
        action="append",
        type=Path,
        help="Cache root holding the registered files (repeatable).",
    )
    parser.add_argument(
        "--out-dir",
        required=True,
        type=Path,
        help="Directory where the migrated registry and reports are written.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    legacy_display = str(args.legacy)
    legacy_registry = args.legacy.resolve()
    cache_roots = [
        CacheRoot(display=str(root), path=root.resolve()) for root in args.cache_root
    ]
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    legacy_entries = parse_legacy_registry(legacy_registry)
    results = [migrate_entry(entry, cache_roots) for entry in legacy_entries]
    identity_groups = scan_byte_identity(cache_roots, results)

    registry_output = out_dir / "registry-sha512.txt"
    manifest_output = out_dir / "migration-manifest.json"
    report_output = out_dir / "migration-report.md"
    write_new_registry(results, registry_output)
    write_manifest(
        results, identity_groups, legacy_display, cache_roots, manifest_output
    )
    write_report(results, identity_groups, report_output)

    published = sum(r.status == STATUS_PUBLISHED for r in results)
    pending = sum(r.status == STATUS_PENDING for r in results)
    logger.info(
        "Migration complete: %s published, %s pending. Outputs in %s",
        published,
        pending,
        out_dir,
    )


if __name__ == "__main__":
    main()
