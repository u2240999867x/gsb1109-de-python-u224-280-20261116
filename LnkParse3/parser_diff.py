#!/usr/bin/env python3
"""Compare two parser-version reports over the same batch of LNK files.

When a fleet of LNK files is re-parsed with a newer parser, the asset report
must not describe "the parser now recognizes more" as "the file was modified".
This module diffs two *parse reports* (old parser vs. new parser) produced over
the same batch and classifies every difference into exactly one of four
categories:

- ``newly_recognized`` (新增识别)
      Byte ranges the old report never covered, or ranges the old report
      covered only with one coarse field that the new report refines into
      several structured fields (one old coarse field may map to N new
      fields). This is a parser capability change, never a file change.
- ``reinterpreted`` (解释更正)
      The same byte range received a different interpretation in the two
      reports. Both claims are kept side by side; the old claim is never
      dropped just because the new report carries a default value.
- ``original_changed`` (原件改变)
      The original file itself differs between the two runs (hash mismatch,
      or size mismatch when hashes are unavailable). Field-level comparison
      is skipped for such files because differences are attributable to the
      file, not to the parser.
- ``insufficient_evidence`` (证据不足)
      Differences that cannot be located or attributed conclusively: fields
      without byte ranges, ranges with only partial overlap, coverage the
      new report lost (a missing field in one version does not prove the
      field never existed in the file), files missing from one report, ...

Field renames and output-order changes are explicitly *not* business changes
and are excluded from the four categories (renames are listed separately for
transparency). The two reports do not need to share a field-tree shape:
matching is done on byte ranges and values, not on tree paths.

Input report format (JSON)::

    {
      "parser": {"name": "LnkParse3", "version": "1.6.0"},
      "files": [
        {
          "path": "inventory/host1/run.lnk",   // batch-relative identity
          "sha256": "<hex>",                    // optional, recommended
          "size": 1234,                         // optional, bytes
          "fields": [
            {"name": "header.link_flags", "value": ["HasLinkInfo"],
             "offset": 20, "size": 4},
            ...
          ]
        }
      ]
    }

``offset``/``size`` locate the field in the *original* file bytes. Fields
without a range are still compared by name and value, but any difference
involving them can only be reported as ``insufficient_evidence`` because it
cannot be located in the original file.

CLI::

    python -m LnkParse3.parser_diff OLD_REPORT.json NEW_REPORT.json [-o OUT.json]
"""

import argparse
import json
import sys


CATEGORY_NEWLY_RECOGNIZED = "newly_recognized"
CATEGORY_REINTERPRETED = "reinterpreted"
CATEGORY_ORIGINAL_CHANGED = "original_changed"
CATEGORY_INSUFFICIENT = "insufficient_evidence"

IDENTITY_SAME = "same"
IDENTITY_ASSUMED_SAME = "assumed_same"
IDENTITY_CHANGED = "changed"
IDENTITY_UNVERIFIABLE = "unverifiable"


def _canonical(value):
    """Return a canonical string form of a value for order-insensitive compare.

    Dict key order never affects the result, so reordered output of object
    fields is not a business change.
    """
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(value)


class Field:
    """One parsed field with its location in the original file."""

    def __init__(self, name, value, offset=None, size=None):
        self.name = name
        self.value = value
        self.offset = offset
        self.size = size

    @classmethod
    def from_dict(cls, raw):
        for key in ("name", "value"):
            if key not in raw:
                raise ValueError(f"field is missing required key {key!r}: {raw!r}")
        return cls(
            name=raw["name"],
            value=raw["value"],
            offset=raw.get("offset"),
            size=raw.get("size"),
        )

    @property
    def has_range(self):
        return self.offset is not None and self.size is not None

    @property
    def end(self):
        return self.offset + self.size

    @property
    def canonical_value(self):
        return _canonical(self.value)

    def strictly_contains(self, other):
        """True if this field's range strictly covers ``other``'s range."""
        if not self.has_range or not other.has_range:
            return False
        if self.offset == other.offset and self.size == other.size:
            return False
        return self.offset <= other.offset and other.end <= self.end

    def overlaps(self, other):
        if not self.has_range or not other.has_range:
            return False
        return self.offset < other.end and other.offset < self.end

    def claim(self):
        """Serializable claim: the field plus its location in the original."""
        claim = {"name": self.name, "value": self.value}
        if self.has_range:
            claim["range"] = {"offset": self.offset, "size": self.size}
        return claim


class FileReport:
    """Parse report for a single original file."""

    def __init__(self, path, sha256=None, size=None, fields=None):
        self.path = path
        self.sha256 = sha256
        self.size = size
        self.fields = fields or []

    @classmethod
    def from_dict(cls, raw):
        if "path" not in raw:
            raise ValueError(f"file entry is missing required key 'path': {raw!r}")
        fields = [Field.from_dict(f) for f in raw.get("fields", [])]
        return cls(
            path=raw["path"],
            sha256=raw.get("sha256"),
            size=raw.get("size"),
            fields=fields,
        )


def _pair_by_key(old_fields, new_fields, key_fn):
    """Greedily pair old/new fields sharing a key.

    Returns (pairs, old_rest, new_rest). Pairing is deterministic: fields are
    consumed in input order, so output order in a report never matters.
    """
    buckets = {}
    for field in old_fields:
        buckets.setdefault(key_fn(field), []).append(field)
    pairs = []
    new_rest = []
    for new_field in new_fields:
        bucket = buckets.get(key_fn(new_field))
        if bucket:
            pairs.append((bucket.pop(0), new_field))
        else:
            new_rest.append(new_field)
    old_rest = [field for bucket in buckets.values() for field in bucket]
    return pairs, old_rest, new_rest


def _identity_status(old_file, new_file):
    """Establish whether both reports describe the same original bytes."""
    if old_file.sha256 and new_file.sha256:
        if old_file.sha256 == new_file.sha256:
            return IDENTITY_SAME
        return IDENTITY_CHANGED
    if old_file.size is not None and new_file.size is not None:
        if old_file.size == new_file.size:
            return IDENTITY_ASSUMED_SAME
        return IDENTITY_CHANGED
    return IDENTITY_UNVERIFIABLE


def _compare_fields(old_fields, new_fields):
    """Classify field-level differences for one identical original file."""
    newly_recognized = []
    reinterpreted = []
    insufficient = []
    renames = []
    unchanged = 0

    old_ranged = [f for f in old_fields if f.has_range]
    old_unranged = [f for f in old_fields if not f.has_range]
    new_ranged = [f for f in new_fields if f.has_range]
    new_unranged = [f for f in new_fields if not f.has_range]

    # Identical fields (same name, range and value) are not changes.
    pairs, old_rest, new_rest = _pair_by_key(
        old_ranged, new_ranged,
        lambda f: (f.name, f.offset, f.size, f.canonical_value),
    )
    unchanged += len(pairs)

    # Same range and value under a different name is a rename, not a
    # business change.
    pairs, old_rest, new_rest = _pair_by_key(
        old_rest, new_rest,
        lambda f: (f.offset, f.size, f.canonical_value),
    )
    for old_field, new_field in pairs:
        renames.append({
            "old_name": old_field.name,
            "new_name": new_field.name,
            "range": {"offset": new_field.offset, "size": new_field.size},
            "value": new_field.value,
        })

    # Same range and name but a different value: mutually exclusive
    # interpretations of the same bytes. Keep both claims.
    pairs, old_rest, new_rest = _pair_by_key(
        old_rest, new_rest,
        lambda f: (f.offset, f.size, f.name),
    )
    for old_field, new_field in pairs:
        reinterpreted.append({
            "kind": "value",
            "range": {"offset": old_field.offset, "size": old_field.size},
            "old": old_field.claim(),
            "new": new_field.claim(),
        })

    # Same range under a different name and value: still mutually exclusive
    # interpretations of the same bytes; keep both claims.
    pairs, old_rest, new_rest = _pair_by_key(
        old_rest, new_rest,
        lambda f: (f.offset, f.size),
    )
    for old_field, new_field in pairs:
        reinterpreted.append({
            "kind": "value",
            "range": {"offset": old_field.offset, "size": old_field.size},
            "old": old_field.claim(),
            "new": new_field.claim(),
        })

    # Same name and value at a different range: the claimed location in the
    # original was corrected. Keep both ranges.
    pairs, old_rest, new_rest = _pair_by_key(
        old_rest, new_rest,
        lambda f: (f.name, f.canonical_value),
    )
    for old_field, new_field in pairs:
        reinterpreted.append({
            "kind": "range",
            "old": old_field.claim(),
            "new": new_field.claim(),
        })

    # One coarse old field may be refined into several new fields whose
    # ranges sit strictly inside the old range (1:N). That is the parser
    # recognizing more of the same original, i.e. newly recognized detail.
    refinements = {}
    still_new = []
    for new_field in new_rest:
        containers = [f for f in old_rest if f.strictly_contains(new_field)]
        if containers:
            coarse = min(containers, key=lambda f: f.size)
            refinements.setdefault(id(coarse), (coarse, []))[1].append(new_field)
        else:
            still_new.append(new_field)
    refined_old_ids = set()
    for coarse, new_fields in refinements.values():
        refined_old_ids.add(id(coarse))
        newly_recognized.append({
            "kind": "refinement",
            "old": coarse.claim(),
            "new_fields": [f.claim() for f in new_fields],
        })
    old_rest = [f for f in old_rest if id(f) not in refined_old_ids]

    # Remaining new fields: either the new report got coarser (one new field
    # swallows several old ones), or ranges only partially overlap, or the
    # range was never covered before.
    still_old = list(old_rest)
    final_new = []
    for new_field in still_new:
        swallowed = [f for f in still_old if new_field.strictly_contains(f)]
        if swallowed:
            insufficient.append({
                "kind": "coverage_coarsened",
                "reason": "new report replaces several specific old fields with one "
                          "coarse field; old claims are kept because losing coverage "
                          "does not prove them wrong",
                "new": new_field.claim(),
                "old_fields": [f.claim() for f in swallowed],
            })
            swallowed_ids = {id(f) for f in swallowed}
            still_old = [f for f in still_old if id(f) not in swallowed_ids]
            continue
        overlapping = [f for f in still_old if new_field.overlaps(f)]
        if overlapping:
            insufficient.append({
                "kind": "partial_overlap",
                "reason": "ranges overlap only partially; cannot attribute the "
                          "difference to a single original range",
                "new": new_field.claim(),
                "old_fields": [f.claim() for f in overlapping],
            })
            overlapping_ids = {id(f) for f in overlapping}
            still_old = [f for f in still_old if id(f) not in overlapping_ids]
            continue
        final_new.append(new_field)
    newly_recognized.extend(
        {"kind": "new_coverage", "new": new_field.claim()} for new_field in final_new
    )

    # Ranges covered only by the old report: the new parser lacking coverage
    # does not prove the field is gone from the original, so this is not a
    # removal and not a file change.
    insufficient.extend(
        {
            "kind": "uncovered_by_new",
            "reason": "range covered only by the old report; missing coverage in "
                      "the new report does not prove the field is absent from the "
                      "original",
            "old": old_field.claim(),
        }
        for old_field in still_old
    )

    # Fields without a byte range can only be matched by name and value.
    pairs, old_unranged, new_unranged = _pair_by_key(
        old_unranged, new_unranged,
        lambda f: (f.name, f.canonical_value),
    )
    unchanged += len(pairs)

    pairs, old_unranged, new_unranged = _pair_by_key(
        old_unranged, new_unranged,
        lambda f: f.name,
    )
    for old_field, new_field in pairs:
        insufficient.append({
            "kind": "missing_range",
            "reason": "values differ but neither claim carries a byte range, so "
                      "the difference cannot be located in the original",
            "old": old_field.claim(),
            "new": new_field.claim(),
        })
    insufficient.extend(
        {
            "kind": "missing_range",
            "reason": "field has no byte range and no counterpart in the new "
                      "report; cannot decide whether it was renamed, moved or "
                      "dropped",
            "old": old_field.claim(),
        }
        for old_field in old_unranged
    )
    insufficient.extend(
        {
            "kind": "missing_range",
            "reason": "field has no byte range and no counterpart in the old "
                      "report; cannot locate it in the original",
            "new": new_field.claim(),
        }
        for new_field in new_unranged
    )

    return {
        "newly_recognized": newly_recognized,
        "reinterpreted": reinterpreted,
        "insufficient_evidence": insufficient,
        "renames": renames,
        "unchanged_fields": unchanged,
    }


def _compare_file(old_file, new_file):
    entry = {
        "path": old_file.path,
        "identity": {
            "status": None,
            "old_sha256": old_file.sha256,
            "new_sha256": new_file.sha256,
            "old_size": old_file.size,
            "new_size": new_file.size,
        },
        "original_changed": None,
        "newly_recognized": [],
        "reinterpreted": [],
        "insufficient_evidence": [],
        "non_business": {"renames": [], "unchanged_fields": 0},
    }

    status = _identity_status(old_file, new_file)
    entry["identity"]["status"] = status

    if status == IDENTITY_CHANGED:
        # The original itself differs: attribute everything to the file,
        # never to the parser.
        entry["original_changed"] = {
            "old_sha256": old_file.sha256,
            "new_sha256": new_file.sha256,
            "old_size": old_file.size,
            "new_size": new_file.size,
        }
        return entry

    compared = _compare_fields(old_file.fields, new_file.fields)
    entry["newly_recognized"] = compared["newly_recognized"]
    entry["reinterpreted"] = compared["reinterpreted"]
    entry["insufficient_evidence"] = compared["insufficient_evidence"]
    entry["non_business"]["renames"] = compared["renames"]
    entry["non_business"]["unchanged_fields"] = compared["unchanged_fields"]

    if status != IDENTITY_SAME:
        entry["identity"]["warning"] = (
            "file identity not verified by hash; field-level results assume "
            "both reports describe the same original"
        )
    return entry


def diff_reports(old_report, new_report):
    """Diff two parse reports and return the classified change report.

    ``old_report`` and ``new_report`` are dicts in the report format described
    in the module docstring. Returns a JSON-serializable dict.
    """
    old_files = {f.path: f for f in map(FileReport.from_dict, old_report.get("files", []))}
    new_files = {f.path: f for f in map(FileReport.from_dict, new_report.get("files", []))}

    files = []
    summary = {
        "files_in_old": len(old_files),
        "files_in_new": len(new_files),
        "files_compared": 0,
        "original_changed": 0,
        "newly_recognized": 0,
        "reinterpreted": 0,
        "insufficient_evidence": 0,
        "identity_unverifiable": 0,
    }

    for path in sorted(set(old_files) | set(new_files)):
        old_file = old_files.get(path)
        new_file = new_files.get(path)
        if old_file is None or new_file is None:
            missing_in = "new" if old_file is not None else "old"
            present = old_file if old_file is not None else new_file
            entry = {
                "path": path,
                "identity": {"status": None},
                "original_changed": None,
                "newly_recognized": [],
                "reinterpreted": [],
                "insufficient_evidence": [{
                    "kind": f"file_missing_in_{missing_in}",
                    "reason": "file is present in only one report; nothing can be "
                              "concluded about the original",
                    "file": {"path": present.path, "sha256": present.sha256,
                             "size": present.size},
                }],
                "non_business": {"renames": [], "unchanged_fields": 0},
            }
            files.append(entry)
            summary["insufficient_evidence"] += 1
            continue

        entry = _compare_file(old_file, new_file)
        files.append(entry)
        summary["files_compared"] += 1
        if entry["original_changed"] is not None:
            summary["original_changed"] += 1
            continue
        summary["newly_recognized"] += len(entry["newly_recognized"])
        summary["reinterpreted"] += len(entry["reinterpreted"])
        summary["insufficient_evidence"] += len(entry["insufficient_evidence"])
        if entry["identity"]["status"] in (IDENTITY_ASSUMED_SAME, IDENTITY_UNVERIFIABLE):
            summary["identity_unverifiable"] += 1

    return {
        "parser_versions": {
            "old": old_report.get("parser"),
            "new": new_report.get("parser"),
        },
        "summary": summary,
        "files": files,
    }


def _load_report(path):
    with open(path) as fp:
        report = json.load(fp)
    if "files" not in report:
        raise ValueError(f"report {path!r} is missing required key 'files'")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="parser_diff",
        description="Compare two parser-version reports over the same batch of "
                    "LNK files and classify changes as newly_recognized, "
                    "reinterpreted, original_changed or insufficient_evidence.",
    )
    parser.add_argument("old_report", help="JSON report produced by the old parser")
    parser.add_argument("new_report", help="JSON report produced by the new parser")
    parser.add_argument("-o", "--output", help="write the diff report to this file "
                                               "instead of stdout")
    args = parser.parse_args(argv)

    result = diff_reports(_load_report(args.old_report), _load_report(args.new_report))
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if args.output:
        with open(args.output, "w") as fp:
            fp.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
