"""Resolve a shortcut to a version in a target-volume history catalog.

The resolver combines three kinds of evidence:

* identity — volume identifiers and per-object identifiers, mapped by explicit
  rules from LNK fields to catalog fields;
* declared size — the size saved in the shortcut, compared with the size the
  catalog declares for a version;
* declared time — the shortcut write/access/creation time and the shell file
  entry time, compared at the coarsest precision either side can prove.

Path is corroboration only.  A path that received a newer file does not make
that newer file inherit the old target relation, and a timestamp stored in the
shortcut never proves that a version survived until the catalog collection
date.

Every comparison keeps the raw value on both sides so that adopted and
excluded candidates can be traced back to the original fields.
"""

import argparse
import json
import os
import re
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Dict
from typing import List
from typing import Mapping
from typing import Optional
from typing import Sequence
from typing import Tuple
from typing import Union

from LnkParse3.lnk_file import LnkFile


__all__ = [
    "EvidenceError",
    "Resolution",
    "resolve_target_version",
]


class EvidenceError(ValueError):
    """Raised when the supplied catalog or identity rules cannot be used."""


# Precision is the smallest interval the source can prove.  Comparing two
# values uses the coarser precision, so an exact FILETIME never overstates what
# a day-precision catalog row can support.
_PRECISION_RANK = {
    "100ns": 0,
    "1us": 1,
    "10us": 2,
    "1ms": 3,
    "10ms": 4,
    "1s": 5,
    "2s": 6,
    "1min": 7,
    "1h": 8,
    "day": 9,
    "month": 10,
    "year": 11,
}

_PRECISION_SECONDS = {
    "100ns": 0.0000001,
    "1us": 0.000001,
    "10us": 0.00001,
    "1ms": 0.001,
    "10ms": 0.01,
    "1s": 1.0,
    "2s": 2.0,
    "1min": 60.0,
    "1h": 3600.0,
}

_MATCH = "match"
_MISMATCH = "mismatch"
_UNKNOWN = "unknown"
_CONFLICT = "conflict"

_DEFAULT_RULES: Dict[str, Any] = {
    "schema": "lnk-identity-rules/1",
    "volume": [
        {
            "lnk": "link_info.location_info.drive_serial_number",
            "catalog": "volume.identifiers.serial",
            "normalize": "hex32",
            "label": "volume serial number",
        },
        {
            "lnk": "extra.DISTRIBUTED_LINK_TRACKER_BLOCK.droid_volume_identifier",
            "catalog": "volume.identifiers.droid_volume_id",
            "normalize": "uuid",
            "label": "droid volume identifier",
        },
    ],
    "object": [
        {
            "lnk": "extra.DISTRIBUTED_LINK_TRACKER_BLOCK.droid_file_identifier",
            "catalog": "entry.identifiers.droid_file_id",
            "normalize": "uuid",
            "label": "droid file identifier",
        },
    ],
    "size": [
        {
            "lnk": "header.file_size",
            "catalog": "entry.size",
            "label": "shortcut header file size",
        },
        {
            "lnk": "target.file_entry.file_size",
            "catalog": "entry.size",
            "label": "shell file-entry size",
        },
    ],
    "time": [
        {
            "lnk": "header.modified_time",
            "catalog": "entry.times.modified",
            "precision": "auto",
            "label": "shortcut header write time",
        },
        {
            "lnk": "target.file_entry.modification_time",
            "catalog": "entry.times.modified",
            "precision": "auto",
            "label": "shell file-entry modification time",
        },
    ],
    "path": [
        {
            "lnk": "path.full",
            "catalog": "entry.path",
            "normalize": "path",
            "label": "target path",
        },
    ],
    "zero_size_is_unknown": True,
}


@dataclass(frozen=True)
class TimePoint:
    """A timestamp and the precision its source can actually prove."""

    value: datetime
    precision: str
    source: str = ""

    def common_precision(self, other: "TimePoint") -> Optional[str]:
        if self.precision not in _PRECISION_RANK or other.precision not in _PRECISION_RANK:
            return None
        if _PRECISION_RANK[self.precision] >= _PRECISION_RANK[other.precision]:
            return self.precision
        return other.precision


@dataclass
class Check:
    """One LNK-field versus catalog-field comparison."""

    kind: str
    result: str
    lnk_field: Optional[str] = None
    lnk_raw: Any = None
    lnk_normalized: Any = None
    lnk_precision: Optional[str] = None
    catalog_field: Optional[str] = None
    catalog_raw: Any = None
    catalog_normalized: Any = None
    catalog_precision: Optional[str] = None
    catalog_location: Optional[str] = None
    detail: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "result": self.result,
            "lnk_field": self.lnk_field,
            "lnk_raw": _jsonable(self.lnk_raw),
            "lnk_normalized": _jsonable(self.lnk_normalized),
            "lnk_precision": self.lnk_precision,
            "catalog_field": self.catalog_field,
            "catalog_raw": _jsonable(self.catalog_raw),
            "catalog_normalized": _jsonable(self.catalog_normalized),
            "catalog_precision": self.catalog_precision,
            "catalog_location": self.catalog_location,
            "detail": self.detail,
        }


@dataclass
class Candidate:
    version: str
    volume_id: str
    path: Optional[str]
    raw_entry: Mapping[str, Any]
    raw_volume: Mapping[str, Any]
    location: str
    checks: List[Check] = field(default_factory=list)
    status: str = "excluded"
    strength: Optional[str] = None
    reasons: List[str] = field(default_factory=list)
    presence_at_link: str = _UNKNOWN
    existed_at_collection: str = _UNKNOWN
    path_reused_after_link: bool = False
    path_later_versions: List[str] = field(default_factory=list)

    @property
    def key(self) -> Tuple[str, str]:
        return self.volume_id, self.version

    def add_check(self, check: Check) -> None:
        self.checks.append(check)

    def aggregate(self, kind: str) -> str:
        results = [check.result for check in self.checks if check.kind == kind]
        if not results:
            return _UNKNOWN
        if _CONFLICT in results:
            return _CONFLICT
        if _MISMATCH in results:
            return _MISMATCH
        if _MATCH in results:
            return _MATCH
        return _UNKNOWN

    def matching(self, kind: str) -> List[Check]:
        return [
            check for check in self.checks if check.kind == kind and check.result == _MATCH
        ]

    def failing(self, kind: str) -> List[Check]:
        return [
            check for check in self.checks if check.kind == kind and check.result == _MISMATCH
        ]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "volume_id": self.volume_id,
            "path": self.path,
            "catalog_location": self.location,
            "status": self.status,
            "strength": self.strength,
            "identity_result": {
                "volume": self.aggregate("volume"),
                "object": self.aggregate("object"),
            },
            "size_result": self.aggregate("size"),
            "time_result": self.aggregate("time"),
            "path_result": self.aggregate("path"),
            "reasons": self.reasons,
            "presence": {
                "at_shortcut_time": self.presence_at_link,
                "at_collection": self.existed_at_collection,
                "path_reused_after_shortcut": self.path_reused_after_link,
                "later_versions_at_path": self.path_later_versions,
            },
            "checks": [check.as_dict() for check in self.checks],
            "adopted_fields": [
                check.as_dict()
                for check in self.checks
                if check.result == _MATCH and check.kind in {"volume", "object", "size", "time"}
            ],
            "excluded_fields": [
                check.as_dict()
                for check in self.checks
                if check.result in {_MISMATCH, _CONFLICT}
            ],
        }


@dataclass
class Resolution:
    status: str
    target_version: Optional[str]
    target: Optional[Dict[str, Any]]
    as_of: Optional[TimePoint]
    collection: Optional[TimePoint]
    candidates: List[Candidate]
    contradictions: List[Dict[str, Any]]
    warnings: List[str]
    rules_source: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "target_version": self.target_version,
            "target": self.target,
            "as_of": _time_dict(self.as_of),
            "collection": _time_dict(self.collection),
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "contradictions": self.contradictions,
            "warnings": self.warnings,
            "rules_source": self.rules_source,
        }


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _time_dict(point: Optional[TimePoint]) -> Optional[Dict[str, Any]]:
    if point is None:
        return None
    return {
        "value": point.value.isoformat(),
        "precision": point.precision,
        "source": point.source,
    }


def _parse_time(
    value: Any, precision: Optional[str] = None, source: str = ""
) -> Optional[TimePoint]:
    """Parse an ISO timestamp or a ``{value, precision}`` declaration.

    A missing precision is inferred narrowly: ``YYYY`` is year precision,
    ``YYYY-MM`` is month precision, ``YYYY-MM-DD`` is day precision, input with
    microseconds is microsecond precision, and any other input is second
    precision.  Catalog authors should declare precision explicitly.
    """

    if value is None:
        return None
    if isinstance(value, TimePoint):
        return value
    if isinstance(value, Mapping):
        precision = value.get("precision", precision)
        source = value.get("source", source)
        value = value.get("value", value.get("time"))

    if isinstance(value, datetime):
        dt = value
        inferred = "1us" if dt.microsecond else "1s"
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
        inferred = "day"
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            dt = datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            inferred = "day"
        elif re.fullmatch(r"\d{4}-\d{2}", text):
            dt = datetime.strptime(text, "%Y-%m").replace(tzinfo=timezone.utc)
            inferred = "month"
        elif re.fullmatch(r"\d{4}", text):
            dt = datetime.strptime(text, "%Y").replace(tzinfo=timezone.utc)
            inferred = "year"
        else:
            normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
            try:
                dt = datetime.fromisoformat(normalized)
            except ValueError as exc:
                raise EvidenceError(f"Invalid timestamp {value!r}") from exc
            if "T" not in text and " " not in text:
                inferred = "day"
            elif dt.microsecond:
                inferred = "1us"
            else:
                inferred = "1s"
    else:
        raise EvidenceError(f"Unsupported timestamp value {value!r}")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if precision is None or precision == "auto":
        precision = inferred
    if precision not in _PRECISION_RANK:
        raise EvidenceError(
            f"Unknown precision {precision!r}; expected one of {sorted(_PRECISION_RANK)}"
        )
    return TimePoint(dt.astimezone(timezone.utc), precision, source)


def _compare_points(left: TimePoint, right: TimePoint) -> Tuple[str, Optional[str]]:
    """Compare two timestamps at the coarsest precision either side can prove.

    Calendar precisions compare calendar buckets.  Sub-day precisions compare
    intervals: a value declared at granularity ``g`` is taken to identify the
    instant to within ``g / 2``, so two values agree when those intervals
    overlap.  This never lets a day-precision catalog row be satisfied by an
    arbitrary time on a neighbouring day.
    """

    precision = left.common_precision(right)
    if precision is None:
        return _UNKNOWN, None

    if precision == "year":
        left_key = left.value.year
        right_key = right.value.year
        if left_key == right_key:
            return "equal", precision
        return ("before" if left_key < right_key else "after"), precision
    if precision == "month":
        left_key = (left.value.year, left.value.month)
        right_key = (right.value.year, right.value.month)
        if left_key == right_key:
            return "equal", precision
        return ("before" if left_key < right_key else "after"), precision
    if precision == "day":
        left_key = left.value.date()
        right_key = right.value.date()
        if left_key == right_key:
            return "equal", precision
        return ("before" if left_key < right_key else "after"), precision

    granularity = (
        _PRECISION_SECONDS[left.precision] + _PRECISION_SECONDS[right.precision]
    ) / 2
    difference = abs((left.value - right.value).total_seconds())
    if difference <= granularity:
        return "equal", precision
    return ("before" if left.value < right.value else "after"), precision


def _normalize_hex32(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, int):
        return f"{value & 0xFFFFFFFF:08x}"
    text = str(value).strip()
    if not text:
        return None
    try:
        return f"{int(text, 0) & 0xFFFFFFFF:08x}"
    except ValueError:
        cleaned = re.sub(r"[^0-9a-fA-F]", "", text)
        if not cleaned:
            return None
        return cleaned.lower().rjust(8, "0")[-8:]


def _normalize_uuid(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().strip("{}").strip()
    if not text:
        return None
    return text.upper()


def _normalize_path(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().strip('"')
    if not text:
        return None
    text = text.replace("/", "\\")
    text = re.sub(r"^\\\\\?\\", "", text)
    text = text.rstrip(" .\\")
    return text.casefold()


def _normalize(value: Any, normalizer: Optional[str]) -> Any:
    if normalizer in {None, "identity"}:
        if value is None:
            return None
        return str(value).strip().casefold()
    if normalizer == "hex32":
        return _normalize_hex32(value)
    if normalizer == "uuid":
        return _normalize_uuid(value)
    if normalizer == "path":
        return _normalize_path(value)
    if normalizer == "text":
        return None if value is None else str(value).strip().casefold()
    if normalizer == "number":
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    raise EvidenceError(f"Unknown normalizer {normalizer!r}")


def _selector_parts(selector: str) -> List[Tuple[str, List[int]]]:
    parts = []
    for raw in selector.split("."):
        indices = [int(index) for index in re.findall(r"\[(-?\d+)\]", raw)]
        name = re.sub(r"\[-?\d+\]", "", raw)
        if not name:
            raise EvidenceError(f"Invalid selector {selector!r}")
        parts.append((name, indices))
    return parts


def _mapping_get(mapping: Mapping[str, Any], name: str) -> Any:
    if name in mapping:
        return mapping[name]
    folded = name.casefold()
    for key, value in mapping.items():
        if str(key).casefold() == folded:
            return value
    return None


def _file_entry(data: Mapping[str, Any]) -> Any:
    target = data.get("target") or {}
    entries = [
        item
        for item in target.get("items") or []
        if isinstance(item, Mapping) and item.get("class") == "File entry"
    ]
    return entries[-1] if entries else None


def _resolve_selector(data: Any, selector: str) -> Any:
    if selector == "path.full":
        link_info = data.get("link_info") or {}
        suffix = (
            link_info.get("common_path_suffix")
            or link_info.get("common_path_suffix_unicode")
            or ""
        )
        base = link_info.get("local_base_path") or link_info.get("local_base_path_unicode")
        if not base and "Network" in (link_info.get("location") or ""):
            base = (link_info.get("location_info") or {}).get("net_name")
        if not base:
            base = (data.get("data") or {}).get("relative_path")
        if not base:
            return None
        if suffix and not suffix.startswith("\\") and not base.endswith("\\"):
            return f"{base}\\{suffix}"
        return f"{base}{suffix}"

    if selector == "target.file_entry":
        return _file_entry(data)
    if selector.startswith("target.file_entry."):
        entry = _file_entry(data)
        if entry is None:
            return None
        return _resolve_selector(entry, selector[len("target.file_entry.") :])

    current = data
    for name, indices in _selector_parts(selector):
        if current is None or not isinstance(current, Mapping):
            return None
        current = _mapping_get(current, name)
        for index in indices:
            if not isinstance(current, (list, tuple)):
                return None
            try:
                current = current[index]
            except IndexError:
                return None
    return current


def _load_lnk(lnk: Union[LnkFile, bytes, bytearray, Any]) -> LnkFile:
    if isinstance(lnk, LnkFile):
        return lnk
    if isinstance(lnk, (bytes, bytearray)):
        return LnkFile(indata=bytes(lnk))
    if hasattr(lnk, "read"):
        return LnkFile(fhandle=lnk)
    raise TypeError("lnk must be a LnkFile, bytes, or a readable binary file handle")


def _load_document(
    document: Any, what: str, allow_directory: bool = False
) -> Tuple[Dict[str, Any], str]:
    if isinstance(document, Mapping):
        return dict(document), "inline"
    if isinstance(document, (str, os.PathLike)):
        path = os.fspath(document)
        if os.path.isdir(path):
            if not allow_directory:
                raise EvidenceError(f"{what} must be a file, not directory {path!r}")
            return _load_catalog_directory(path), path
        try:
            with open(path, "rb") as handle:
                return json.load(handle), str(path)
        except OSError as exc:
            raise EvidenceError(f"Unable to read {what} {path!r}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise EvidenceError(f"Invalid JSON in {what} {path!r}: {exc}") from exc
    raise TypeError(f"{what} must be a mapping or a path to a JSON file or directory")


def _load_catalog_directory(path: str) -> Dict[str, Any]:
    """Load a volume-history directory.

    A ``catalog.json`` manifest is authoritative.  Without one, every ``.json``
    file in the directory is read and merged.  Each file may describe a volume
    with nested versions, a list of entries for one volume, or a single
    version.  Repeating the same volume identity across per-version files is
    allowed; conflicting identities or duplicate versions are rejected.
    """

    manifest = os.path.join(path, "catalog.json")
    if os.path.isfile(manifest):
        return _load_document(manifest, "catalog manifest")[0]

    names = sorted(
        name
        for name in os.listdir(path)
        if name.lower().endswith(".json") and not name.startswith(".")
    )
    if not names:
        raise EvidenceError(f"No JSON catalog files found in {path!r}")

    volumes: Dict[str, Dict[str, Any]] = {}
    flat_entries: List[Dict[str, Any]] = []
    collection: Optional[TimePoint] = None

    def register_volume(raw_volume: Mapping[str, Any], source: str) -> str:
        volume_id = _volume_id(raw_volume) or f"volume-{len(volumes)}"
        nested = raw_volume.get("entries")
        if nested is None:
            nested = raw_volume.get("versions")
        if nested is not None and not isinstance(nested, list):
            raise EvidenceError(
                f"Catalog fragment {source!r}: volume {volume_id!r} entries must be a list"
            )

        existing = volumes.get(volume_id)
        if existing is None:
            stored = {
                key: value
                for key, value in raw_volume.items()
                if key not in {"entries", "versions"}
            }
            stored["entries"] = [dict(item) for item in (nested or [])]
            volumes[volume_id] = stored
            return volume_id

        new_ids = raw_volume.get("identifiers") or {}
        old_ids = existing.get("identifiers") or {}
        for key, value in new_ids.items():
            if key in old_ids and _normalize(old_ids[key], "identity") != _normalize(
                value, "identity"
            ):
                raise EvidenceError(
                    f"Catalog fragment {source!r} disagrees on identifier "
                    f"{key!r} for volume {volume_id!r}"
                )
        merged_ids = dict(old_ids)
        merged_ids.update(new_ids)
        existing["identifiers"] = merged_ids
        for key in ("label", "volume_label"):
            if existing.get(key) is None and raw_volume.get(key) is not None:
                existing[key] = raw_volume[key]
        if nested:
            existing.setdefault("entries", []).extend(dict(item) for item in nested)
        return volume_id

    for name in names:
        fragment_path = os.path.join(path, name)
        try:
            with open(fragment_path, "rb") as handle:
                fragment = json.load(handle)
        except OSError as exc:
            raise EvidenceError(
                f"Unable to read catalog fragment {fragment_path!r}: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise EvidenceError(
                f"Invalid JSON in catalog fragment {fragment_path!r}: {exc}"
            ) from exc

        if not isinstance(fragment, Mapping):
            raise EvidenceError(f"Catalog fragment {name!r} must be an object")

        fragment_collection = _parse_time(
            fragment.get("collected_at")
            or fragment.get("collection_time")
            or fragment.get("captured_at"),
            source=f"{name}:collected_at",
        )
        if fragment_collection is not None:
            if collection is None:
                collection = fragment_collection
            elif _compare_points(collection, fragment_collection)[0] != "equal":
                raise EvidenceError(
                    f"Catalog fragments disagree on collection time: "
                    f"{collection.value.isoformat()} vs "
                    f"{fragment_collection.value.isoformat()} ({name})"
                )

        volume_definition = fragment.get("volume")
        volume_id = None
        if isinstance(volume_definition, Mapping):
            volume_id = register_volume(volume_definition, name)

        if "volumes" in fragment:
            if not isinstance(fragment["volumes"], list):
                raise EvidenceError(f"Catalog fragment {name!r}: volumes must be a list")
            for raw_volume in fragment["volumes"]:
                if not isinstance(raw_volume, Mapping):
                    raise EvidenceError(
                        f"Catalog fragment {name!r}: each volume must be an object"
                    )
                register_volume(raw_volume, name)

        fragment_entries = fragment.get("entries")
        if fragment_entries is None:
            fragment_entries = fragment.get("versions")
        if fragment_entries is None and "version" in fragment:
            fragment_entries = [fragment]

        if fragment_entries is None:
            if volume_id is not None or "volumes" in fragment:
                continue
            raise EvidenceError(
                f"Catalog fragment {name!r} has no volumes, entries or version"
            )
        if not isinstance(fragment_entries, list):
            raise EvidenceError(f"Catalog fragment {name!r}: entries must be a list")

        if volume_id is None:
            raw_id = fragment.get("volume_id")
            if raw_id is None and isinstance(volume_definition, str):
                raw_id = volume_definition
            volume_id = None if raw_id is None else str(raw_id)

        for item in fragment_entries:
            if not isinstance(item, Mapping):
                raise EvidenceError(
                    f"Catalog fragment {name!r}: each entry must be an object"
                )
            entry_doc = dict(item)
            if volume_id is not None and "volume" not in entry_doc:
                entry_doc["volume"] = volume_id
            flat_entries.append(entry_doc)

    merged: Dict[str, Any] = {
        "volumes": list(volumes.values()),
        "entries": flat_entries,
    }
    if collection is not None:
        merged["collected_at"] = {
            "value": collection.value.isoformat(),
            "precision": collection.precision,
        }

    seen: Dict[Tuple[str, str], str] = {}
    for volume in merged["volumes"]:
        volume_key = _volume_id(volume) or "unassigned"
        for item in volume.get("entries") or volume.get("versions") or []:
            version = _entry_version(item)
            if version is None:
                continue
            key = (volume_key, version)
            if key in seen:
                raise EvidenceError(
                    f"Duplicate catalog version {version!r} on volume {volume_key!r} "
                    "across catalog fragments"
                )
            seen[key] = volume_key
    for item in flat_entries:
        volume_ref = item.get("volume")
        if isinstance(volume_ref, Mapping):
            volume_key = _volume_id(volume_ref) or "unassigned"
        elif volume_ref is None:
            volume_key = "unassigned"
        else:
            volume_key = str(volume_ref)
        version = _entry_version(item)
        if version is None:
            continue
        key = (volume_key, version)
        if key in seen:
            raise EvidenceError(
                f"Duplicate catalog version {version!r} on volume {volume_key!r} "
                "across catalog fragments"
            )
        seen[key] = volume_key

    return merged


def _entry_version(entry: Mapping[str, Any]) -> Optional[str]:
    version = (
        entry.get("version")
        or entry.get("version_name")
        or entry.get("label")
    )
    return None if version is None else str(version)


def _volume_id(raw_volume: Mapping[str, Any]) -> Optional[str]:
    volume_id = (
        raw_volume.get("volume_id")
        or raw_volume.get("id")
        or raw_volume.get("device_id")
        or raw_volume.get("name")
    )
    return None if volume_id is None else str(volume_id)


def _merge_rules(rules: Optional[Union[Mapping[str, Any], str]]) -> Tuple[Dict[str, Any], str]:
    merged = json.loads(json.dumps(_DEFAULT_RULES))
    if rules is None:
        return merged, "default"

    document, source = _load_document(rules, "identity rules")
    for key, value in document.items():
        if key == "schema":
            merged[key] = value
        elif key == "zero_size_is_unknown":
            merged[key] = bool(value)
        elif key in {"volume", "object", "size", "time", "path"}:
            if not isinstance(value, list):
                raise EvidenceError(f"Rule section {key!r} must be a list")
            merged[key] = value
        else:
            raise EvidenceError(f"Unknown identity rule section {key!r}")

    for kind in ("volume", "object", "size", "time", "path"):
        if not isinstance(merged.get(kind), list):
            raise EvidenceError(f"Rule section {kind!r} must be a list")
        for rule in merged[kind]:
            if "lnk" not in rule or "catalog" not in rule:
                raise EvidenceError(f"Each {kind!r} rule needs 'lnk' and 'catalog'")
            if kind == "time" and rule.get("precision") not in (None, "auto"):
                if rule["precision"] not in _PRECISION_RANK:
                    raise EvidenceError(f"Unknown precision in rule {rule!r}")
    return merged, source


def _entry_field(entry: Mapping[str, Any], name: str) -> Any:
    aliases = {
        "size": ("size", "file_size", "byte_size", "bytes"),
        "path": ("path", "full_path", "name"),
        "times": ("times", "timestamps", "time"),
        "identifiers": ("identifiers", "ids", "object_ids"),
        "presence": ("presence", "existence", "observed"),
        "version": ("version", "version_name", "label", "id", "name"),
    }
    if name in entry:
        return entry[name]
    for alias in aliases.get(name, ()):
        if alias in entry:
            return entry[alias]
    return None


_IDENTIFIER_ALIASES = {
    "droid_file_id": ("droid_file_id", "droid_file_identifier", "file_id"),
    "droid_volume_id": ("droid_volume_id", "droid_volume_identifier"),
    "serial": ("serial", "drive_serial_number", "volume_serial", "serial_number"),
    "uuid": ("uuid", "object_id", "identifier"),
}

_VOLUME_IDENTIFIER_KEYS = {"serial", "droid_volume_id", "drive_serial_number", "volume_serial"}
_OBJECT_IDENTIFIER_KEYS = {"droid_file_id", "file_id", "uuid", "object_id"}


def _collect_identifiers(raw: Mapping[str, Any]) -> Dict[str, Any]:
    """Collect explicit identifiers plus only known top-level aliases.

    Catalog documents often repeat a volume identifier on every entry.  Those
    repeats describe one volume, not several objects, so they must not be
    treated as object-level identity collisions.
    """

    identifiers: Dict[str, Any] = {}
    explicit = raw.get("identifiers") or raw.get("ids") or raw.get("object_ids")
    if isinstance(explicit, Mapping):
        for key, value in explicit.items():
            if value is not None:
                identifiers[str(key)] = value

    for canonical, aliases in _IDENTIFIER_ALIASES.items():
        if canonical in identifiers:
            continue
        for alias in aliases:
            value = _mapping_get(raw, alias)
            if value is not None:
                identifiers[canonical] = value
                break
    return identifiers


def _normalize_catalog(catalog: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(catalog, Mapping):
        raise EvidenceError("Catalog must be a JSON object")

    collection = _parse_time(
        catalog.get("collected_at")
        or catalog.get("collection_time")
        or catalog.get("collection")
        or catalog.get("captured_at"),
        source="catalog.collected_at",
    )

    volumes: List[Dict[str, Any]] = []
    entries: List[Dict[str, Any]] = []

    raw_volumes = catalog.get("volumes")
    if raw_volumes is None and isinstance(catalog.get("volume"), Mapping):
        raw_volumes = [catalog["volume"]]
    if raw_volumes is None:
        raw_volumes = []
    if not isinstance(raw_volumes, list):
        raise EvidenceError("catalog.volumes must be a list")

    for index, raw_volume in enumerate(raw_volumes):
        if not isinstance(raw_volume, Mapping):
            raise EvidenceError(f"catalog.volumes[{index}] must be an object")
        volume_id = (
            raw_volume.get("volume_id")
            or raw_volume.get("id")
            or raw_volume.get("device_id")
            or raw_volume.get("name")
        )
        if volume_id is None:
            volume_id = f"volume-{index}"
        volume = {
            "volume_id": str(volume_id),
            "identifiers": _collect_identifiers(raw_volume),
            "label": raw_volume.get("label") or raw_volume.get("volume_label"),
            "raw": raw_volume,
        }
        volumes.append(volume)

        raw_entries = (
            raw_volume.get("entries")
            or raw_volume.get("versions")
            or raw_volume.get("files")
            or []
        )
        if not isinstance(raw_entries, list):
            raise EvidenceError(f"catalog.volumes[{index}].entries must be a list")
        for entry_index, raw_entry in enumerate(raw_entries):
            if not isinstance(raw_entry, Mapping):
                raise EvidenceError(
                    f"catalog.volumes[{index}].entries[{entry_index}] must be an object"
                )
            entries.append(
                _normalize_entry(
                    raw_entry,
                    volume,
                    f"volumes[{index}].entries[{entry_index}]",
                )
            )

    raw_entries = catalog.get("entries") or catalog.get("versions") or []
    if not isinstance(raw_entries, list):
        raise EvidenceError("catalog.entries must be a list")
    for entry_index, raw_entry in enumerate(raw_entries):
        if not isinstance(raw_entry, Mapping):
            raise EvidenceError(f"catalog.entries[{entry_index}] must be an object")
        volume_ref = raw_entry.get("volume") or raw_entry.get("volume_id") or {}
        if isinstance(volume_ref, Mapping):
            volume_id = volume_ref.get("volume_id") or volume_ref.get("id")
            volume = next((item for item in volumes if item["volume_id"] == volume_id), None)
            if volume is None:
                volume = {
                    "volume_id": str(volume_id) if volume_id is not None else "unassigned",
                    "identifiers": _collect_identifiers(volume_ref),
                    "label": volume_ref.get("label"),
                    "raw": volume_ref,
                }
                volumes.append(volume)
        else:
            volume = next((item for item in volumes if item["volume_id"] == str(volume_ref)), None)
            if volume is None:
                volume = {
                    "volume_id": str(volume_ref),
                    "identifiers": {},
                    "label": None,
                    "raw": {},
                }
                volumes.append(volume)
        entries.append(_normalize_entry(raw_entry, volume, f"entries[{entry_index}]"))

    return {
        "collection": collection,
        "volumes": volumes,
        "entries": entries,
        "complete": bool(catalog.get("complete", False)),
        "raw": catalog,
    }


def _normalize_entry(
    raw_entry: Mapping[str, Any], volume: Mapping[str, Any], location: str
) -> Dict[str, Any]:
    version = _entry_field(raw_entry, "version")
    if version is None:
        raise EvidenceError(f"Catalog entry {location} has no version name")
    identifiers = _collect_identifiers(raw_entry)
    return {
        "version": str(version),
        "path": _entry_field(raw_entry, "path"),
        "size": _entry_field(raw_entry, "size"),
        "times": _entry_field(raw_entry, "times") or {},
        "identifiers": identifiers,
        "presence": _entry_field(raw_entry, "presence") or {},
        "volume": volume,
        "raw": raw_entry,
        "location": location,
    }


def _lnk_value(data: Mapping[str, Any], rule: Mapping[str, Any]) -> Tuple[Any, Any]:
    raw = _resolve_selector(data, rule["lnk"])
    return raw, _normalize(raw, rule.get("normalize"))


def _catalog_value(
    volume: Mapping[str, Any], entry: Mapping[str, Any], rule: Mapping[str, Any]
) -> Tuple[Any, Any]:
    selector = rule["catalog"]
    if selector.startswith("volume."):
        raw = _resolve_selector(volume, selector[len("volume.") :])
    elif selector.startswith("entry."):
        raw = _resolve_selector(entry, selector[len("entry.") :])
    else:
        raw = _resolve_selector({"volume": volume, "entry": entry}, selector)
    return raw, _normalize(raw, rule.get("normalize"))


def _lnk_precision(rule: Mapping[str, Any], raw: Any) -> Optional[str]:
    if rule.get("precision") not in (None, "auto"):
        return rule["precision"]
    field_path = str(rule.get("lnk", ""))
    if "file_entry" in field_path and "modification_time" in field_path:
        # DOS timestamps store seconds in two-second increments.
        return "2s"
    if field_path.startswith("header.") and field_path.endswith("_time"):
        # The on-disk FILETIME is 100 ns, but this package exposes a datetime
        # truncated to microseconds.  Do not claim more than was parsed.
        return "1us"
    if isinstance(raw, datetime):
        return "1us" if raw.microsecond else "1s"
    return None


def _compare_identity(
    candidate: Candidate, data: Mapping[str, Any], kind: str, rules: Mapping[str, Any]
) -> None:
    for rule in rules.get(kind) or []:
        lnk_raw, lnk_value = _lnk_value(data, rule)
        catalog_raw, catalog_value = _catalog_value(
            candidate.raw_volume, candidate.raw_entry, rule
        )
        if lnk_value is None or catalog_value is None:
            result = _UNKNOWN
            detail = "one side has no value for this identity rule"
        elif lnk_value == catalog_value:
            result = _MATCH
            detail = "normalized identifiers are equal"
        else:
            result = _MISMATCH
            detail = "normalized identifiers differ"
        candidate.add_check(
            Check(
                kind=kind,
                result=result,
                lnk_field=rule.get("lnk"),
                lnk_raw=lnk_raw,
                lnk_normalized=lnk_value,
                catalog_field=rule.get("catalog"),
                catalog_raw=catalog_raw,
                catalog_normalized=catalog_value,
                catalog_location=candidate.location,
                detail=f"{rule.get('label', kind)}: {detail}",
            )
        )


def _shortcut_sizes(data: Mapping[str, Any], rules: Mapping[str, Any]) -> Dict[str, Any]:
    sizes: Dict[str, Any] = {}
    for rule in rules.get("size") or []:
        raw = _resolve_selector(data, rule["lnk"])
        if raw is not None:
            sizes[rule["lnk"]] = raw
    return sizes


def _compare_size(candidate: Candidate, data: Mapping[str, Any], rules: Mapping[str, Any]) -> None:
    zero_is_unknown = bool(rules.get("zero_size_is_unknown", True))
    shortcut_sizes = _shortcut_sizes(data, rules)
    distinct = set()
    for value in shortcut_sizes.values():
        try:
            size = int(value)
        except (TypeError, ValueError):
            continue
        if size == 0 and zero_is_unknown:
            continue
        distinct.add(size)

    if len(distinct) > 1:
        values = {str(value): [] for value in distinct}
        for rule in rules.get("size") or []:
            raw = _resolve_selector(data, rule["lnk"])
            if raw is None:
                continue
            try:
                values[str(int(raw))].append(rule["lnk"])
            except (TypeError, ValueError):
                continue
        candidate.add_check(
            Check(
                kind="size",
                result=_CONFLICT,
                lnk_field="; ".join(rule["lnk"] for rule in rules.get("size") or []),
                lnk_raw=shortcut_sizes,
                lnk_normalized={key: value for key, value in values.items()},
                catalog_field="entry.size",
                catalog_raw=candidate.raw_entry.get("size"),
                catalog_normalized=candidate.raw_entry.get("size"),
                catalog_location=candidate.location,
                detail=(
                    "shortcut size declarations disagree with each other: "
                    + ", ".join(f"{key}={value}" for key, value in sorted(values.items()))
                ),
            )
        )
        return

    for rule in rules.get("size") or []:
        lnk_raw = _resolve_selector(data, rule["lnk"])
        catalog_raw = _catalog_value(candidate.raw_volume, candidate.raw_entry, rule)[0]
        if lnk_raw in (None, 0) and zero_is_unknown:
            result = _UNKNOWN
            detail = "shortcut size is absent or zero, so it is not treated as a demand"
        elif catalog_raw is None:
            result = _UNKNOWN
            detail = "catalog entry has no declared size"
        else:
            try:
                lnk_size = int(lnk_raw)
                catalog_size = int(catalog_raw)
            except (TypeError, ValueError):
                result = _UNKNOWN
                detail = "size is not an integer"
            else:
                if lnk_size == 0 and zero_is_unknown:
                    result = _UNKNOWN
                    detail = "shortcut size is zero, so it is not treated as a demand"
                elif lnk_size == catalog_size:
                    result = _MATCH
                    detail = "declared sizes are equal"
                else:
                    result = _MISMATCH
                    detail = f"declared sizes differ ({lnk_size} != {catalog_size})"
        candidate.add_check(
            Check(
                kind="size",
                result=result,
                lnk_field=rule.get("lnk"),
                lnk_raw=lnk_raw,
                lnk_normalized=lnk_raw,
                catalog_field=rule.get("catalog"),
                catalog_raw=catalog_raw,
                catalog_normalized=catalog_raw,
                catalog_location=candidate.location,
                detail=f"{rule.get('label', 'size')}: {detail}",
            )
        )


def _compare_time(candidate: Candidate, data: Mapping[str, Any], rules: Mapping[str, Any]) -> None:
    for rule in rules.get("time") or []:
        lnk_raw = _resolve_selector(data, rule["lnk"])
        catalog_raw = _catalog_value(candidate.raw_volume, candidate.raw_entry, rule)[0]
        lnk_point = _parse_time(
            lnk_raw, precision=_lnk_precision(rule, lnk_raw), source=rule.get("lnk", "")
        )
        catalog_point = _parse_time(catalog_raw, source=rule.get("catalog", ""))
        if lnk_point is None or catalog_point is None:
            result = _UNKNOWN
            detail = "one side has no timestamp"
        else:
            relation, used_precision = _compare_points(lnk_point, catalog_point)
            if relation == _UNKNOWN:
                result = _UNKNOWN
                detail = "precision is missing or not comparable"
            elif relation == "equal":
                result = _MATCH
                detail = f"timestamps agree at {used_precision} precision"
            else:
                result = _MISMATCH
                detail = f"timestamps differ at {used_precision} precision"
        candidate.add_check(
            Check(
                kind="time",
                result=result,
                lnk_field=rule.get("lnk"),
                lnk_raw=lnk_raw,
                lnk_normalized=lnk_point.value.isoformat() if lnk_point else None,
                lnk_precision=lnk_point.precision if lnk_point else None,
                catalog_field=rule.get("catalog"),
                catalog_raw=catalog_raw,
                catalog_normalized=catalog_point.value.isoformat() if catalog_point else None,
                catalog_precision=catalog_point.precision if catalog_point else None,
                catalog_location=candidate.location,
                detail=f"{rule.get('label', 'time')}: {detail}",
            )
        )


def _compare_path(candidate: Candidate, data: Mapping[str, Any], rules: Mapping[str, Any]) -> None:
    for rule in rules.get("path") or []:
        lnk_raw, lnk_value = _lnk_value(data, rule)
        catalog_raw, catalog_value = _catalog_value(
            candidate.raw_volume, candidate.raw_entry, rule
        )
        if lnk_value is None or catalog_value is None:
            result = _UNKNOWN
            detail = "one side has no path"
        elif lnk_value == catalog_value:
            result = _MATCH
            detail = "normalized paths are equal; path is corroboration, not identity"
        else:
            result = _MISMATCH
            detail = "normalized paths differ; path is corroboration, not identity"
        candidate.add_check(
            Check(
                kind="path",
                result=result,
                lnk_field=rule.get("lnk"),
                lnk_raw=lnk_raw,
                lnk_normalized=lnk_value,
                catalog_field=rule.get("catalog"),
                catalog_raw=catalog_raw,
                catalog_normalized=catalog_value,
                catalog_location=candidate.location,
                detail=f"{rule.get('label', 'path')}: {detail}",
            )
        )


def _presence_points(
    entry: Mapping[str, Any],
) -> Tuple[Optional[TimePoint], Optional[TimePoint], Optional[TimePoint], bool]:
    presence = entry.get("presence") or {}
    first = _parse_time(
        presence.get("first_seen") or presence.get("from") or presence.get("placed_at"),
        source="entry.presence.first_seen",
    )
    last = _parse_time(
        presence.get("last_seen") or presence.get("to"),
        source="entry.presence.last_seen",
    )
    removed = _parse_time(
        presence.get("removed_at") or presence.get("deleted_at"),
        source="entry.presence.removed_at",
    )
    observed = bool(
        presence.get("observed_at_collection")
        or presence.get("present_at_collection")
        or False
    )
    return first, last, removed, observed


def _assess_presence(
    candidate: Candidate, link_time: Optional[TimePoint], collection: Optional[TimePoint]
) -> None:
    first, last, removed, observed = _presence_points(candidate.raw_entry)
    candidate.presence_at_link = _UNKNOWN

    if link_time is not None:
        if first is not None:
            relation, _ = _compare_points(link_time, first)
            if relation == "before":
                candidate.presence_at_link = "absent"
                candidate.reasons.append(
                    "catalog placement happens after the shortcut was written"
                )
        if candidate.presence_at_link != "absent" and removed is not None:
            relation, _ = _compare_points(link_time, removed)
            if relation == "after":
                candidate.presence_at_link = "absent"
                candidate.reasons.append(
                    "catalog removal happens before the shortcut was written"
                )
        if candidate.presence_at_link != "absent" and last is not None:
            relation, _ = _compare_points(link_time, last)
            if relation == "after":
                candidate.presence_at_link = "absent"
                candidate.reasons.append(
                    "last catalog observation predates the shortcut write time"
                )
        if candidate.presence_at_link == _UNKNOWN and (first is not None or last is not None):
            candidate.presence_at_link = "present"

    if observed:
        candidate.existed_at_collection = "yes"
    elif collection is None:
        candidate.existed_at_collection = _UNKNOWN
    elif removed is not None:
        relation, _ = _compare_points(removed, collection)
        candidate.existed_at_collection = "no" if relation == "before" else "yes"
    elif last is not None:
        relation, _ = _compare_points(last, collection)
        if relation == "equal":
            candidate.existed_at_collection = "yes"
        elif relation == "after":
            candidate.existed_at_collection = "yes"
        else:
            candidate.existed_at_collection = "not_observed"
    else:
        candidate.existed_at_collection = _UNKNOWN


def _mark_path_reuse(
    candidate: Candidate,
    entries: Sequence[Mapping[str, Any]],
    link_time: Optional[TimePoint],
) -> None:
    path = _normalize_path(candidate.path)
    if not path or link_time is None:
        return
    later_versions = []
    for other in entries:
        if other is candidate.raw_entry:
            continue
        if _normalize_path(other.get("path")) != path:
            continue
        first, _, _, _ = _presence_points(other)
        if first is None:
            continue
        relation, _ = _compare_points(first, link_time)
        if relation != "after":
            continue
        later_versions.append(str(other.get("version")))
    if later_versions:
        candidate.path_reused_after_link = True
        candidate.path_later_versions = sorted(set(later_versions))


def _evaluate_candidate(
    entry: Mapping[str, Any],
    data: Mapping[str, Any],
    rules: Mapping[str, Any],
    link_time: Optional[TimePoint],
    collection: Optional[TimePoint],
    all_entries: Sequence[Mapping[str, Any]],
) -> Candidate:
    volume = entry["volume"]
    candidate = Candidate(
        version=entry["version"],
        volume_id=volume["volume_id"],
        path=entry.get("path"),
        raw_entry=entry,
        raw_volume=volume,
        location=entry["location"],
    )

    _compare_identity(candidate, data, "volume", rules)
    _compare_identity(candidate, data, "object", rules)
    _compare_size(candidate, data, rules)
    _compare_time(candidate, data, rules)
    _compare_path(candidate, data, rules)
    _assess_presence(candidate, link_time, collection)
    _mark_path_reuse(candidate, all_entries, link_time)

    volume_result = candidate.aggregate("volume")
    object_result = candidate.aggregate("object")
    size_result = candidate.aggregate("size")
    time_result = candidate.aggregate("time")

    object_match = object_result == _MATCH
    volume_match = volume_result == _MATCH

    if not object_match and not volume_match:
        candidate.status = "excluded"
        if object_result == _UNKNOWN and volume_result == _UNKNOWN:
            candidate.reasons.append("no identity value could be compared")
        elif object_result == _UNKNOWN or volume_result == _UNKNOWN:
            candidate.reasons.append(
                "the only comparable identity value does not match"
            )
        else:
            candidate.reasons.append("volume and object identifiers do not match")
        return candidate

    if object_match and volume_result == _MISMATCH:
        candidate.status = "excluded"
        candidate.reasons.append(
            "object identifier and volume identifier conflict within the catalog entry"
        )
        return candidate

    if not object_match and object_result == _MISMATCH:
        # Same volume, different object.  This identifies another file, not a
        # near miss, even when size or path happens to fit.
        candidate.status = "excluded"
        candidate.reasons.append("object identifier belongs to a different catalog entry")
        if size_result in {_MISMATCH, _CONFLICT}:
            candidate.reasons.append("saved size contradicts the catalog entry")
        if time_result == _MISMATCH:
            candidate.reasons.append(
                "saved time contradicts the catalog entry at comparable precision"
            )
        return candidate

    if volume_result == _MISMATCH:
        candidate.status = "excluded"
        candidate.reasons.append("volume identifier belongs to a different catalog volume")
        return candidate

    if object_match:
        candidate.strength = "strong"
    elif size_result == _MATCH or time_result == _MATCH:
        candidate.strength = "medium"
    else:
        candidate.strength = "weak"

    blocked = (
        size_result in {_MISMATCH, _CONFLICT}
        or time_result == _MISMATCH
        or candidate.presence_at_link == "absent"
    )

    if blocked:
        candidate.status = "excluded"
        if size_result == _CONFLICT:
            candidate.reasons.append("shortcut size declarations contradict each other")
        elif size_result == _MISMATCH:
            candidate.reasons.append("saved size contradicts the catalog entry")
        if time_result == _MISMATCH:
            candidate.reasons.append(
                "saved time contradicts the catalog entry at comparable precision"
            )
        if candidate.presence_at_link == "absent":
            candidate.reasons.append(
                "catalog presence does not cover the shortcut write time"
            )
        return candidate

    if candidate.path_reused_after_link and candidate.strength == "weak":
        candidate.status = "ambiguous"
        candidate.reasons.append(
            "only the volume identifier supports this entry, and a later catalog "
            "version occupies the same path; the path relation is not inherited"
        )
        return candidate

    if candidate.strength in {"strong", "medium"}:
        candidate.status = "adopted"
    else:
        candidate.status = "ambiguous"
        if not candidate.reasons:
            candidate.reasons.append(
                "volume identifier matches but size and time give no corroboration"
            )
    return candidate


def _duplicate_identity_contradictions(
    entries: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    contradictions: List[Dict[str, Any]] = []

    volume_claims: Dict[Tuple[str, str], List[str]] = {}
    for entry in entries:
        volume = entry["volume"]
        for name, value in (volume.get("identifiers") or {}).items():
            if name not in _VOLUME_IDENTIFIER_KEYS:
                continue
            normalized = _normalize(value, "identity")
            if normalized is None:
                continue
            volume_claims.setdefault((name, normalized), []).append(volume["volume_id"])
    for (name, value), volume_ids in volume_claims.items():
        unique_ids = sorted(set(volume_ids))
        if len(unique_ids) > 1:
            contradictions.append(
                {
                    "kind": "catalog_volume_conflict",
                    "message": (
                        f"volume identifier {name}={value!r} is claimed by "
                        f"{len(unique_ids)} catalog volumes"
                    ),
                    "versions": [],
                    "volumes": unique_ids,
                    "candidate_keys": [
                        [entry["volume"]["volume_id"], entry["version"]]
                        for entry in entries
                        if entry["volume"]["volume_id"] in unique_ids
                    ],
                    "catalog_fields": [f"volume.identifiers.{name}"],
                }
            )

    object_claims: Dict[Tuple[str, str], List[Tuple[str, str]]] = {}
    for entry in entries:
        entry_key = (entry["volume"]["volume_id"], entry["version"])
        for name, value in (entry.get("identifiers") or {}).items():
            if name not in _OBJECT_IDENTIFIER_KEYS:
                continue
            normalized = _normalize(value, "identity")
            if normalized is None:
                continue
            object_claims.setdefault((name, normalized), []).append(entry_key)
    for (name, value), keys in object_claims.items():
        unique_keys = sorted(set(keys))
        if len(unique_keys) > 1:
            contradictions.append(
                {
                    "kind": "catalog_object_conflict",
                    "message": (
                        f"object identifier {name}={value!r} is claimed by "
                        f"{len(unique_keys)} catalog versions"
                    ),
                    "versions": [f"{volume_id}/{version}" for volume_id, version in unique_keys],
                    "candidate_keys": [list(key) for key in unique_keys],
                    "catalog_fields": [f"entry.identifiers.{name}"],
                }
            )
    return contradictions


def _candidate_contradictions(
    candidates: Sequence[Candidate],
) -> List[Dict[str, Any]]:
    contradictions: List[Dict[str, Any]] = []

    object_matches = [
        candidate for candidate in candidates if candidate.aggregate("object") == _MATCH
    ]
    volume_matches = [
        candidate for candidate in candidates if candidate.aggregate("volume") == _MATCH
    ]

    if object_matches:
        object_keys = {candidate.key for candidate in object_matches}
        volume_keys = {candidate.key for candidate in volume_matches}
        # Only a disjoint result is a real disagreement.  A matching overlap
        # means the two identifiers agree on one version; an extra object
        # match elsewhere is a catalog anomaly recorded separately.
        if volume_matches and not (object_keys & volume_keys):
            contradictions.append(
                {
                    "kind": "identity_disagreement",
                    "message": (
                        "the object identifier and the volume identifier point at "
                        "different catalog versions"
                    ),
                    "versions": sorted(
                        {candidate.version for candidate in object_matches + volume_matches}
                    ),
                    "candidate_keys": [
                        list(key) for key in sorted(object_keys | volume_keys)
                    ],
                    "fields": [
                        check.as_dict()
                        for candidate in object_matches + volume_matches
                        for check in candidate.checks
                        if check.kind in {"volume", "object"}
                    ],
                }
            )

    # Volume-level matches establish only that the target lives on that volume,
    # not which file it is.  A size/time mismatch there means "this is not the
    # version", not "the evidence contradicts itself", unless an object
    # identifier makes a specific version claim.
    for candidate in candidates:
        object_match = candidate.aggregate("object") == _MATCH
        if not object_match:
            continue
        if candidate.aggregate("volume") == _MISMATCH:
            contradictions.append(
                {
                    "kind": "identity_conflict",
                    "message": (
                        f"catalog entry {candidate.version} carries the shortcut's "
                        "object identifier on a volume that does not match"
                    ),
                    "versions": [candidate.version],
                    "candidate_keys": [list(candidate.key)],
                    "fields": [
                        check.as_dict()
                        for check in candidate.checks
                        if check.kind in {"volume", "object"}
                    ],
                }
            )
            continue
        blocking = []
        if candidate.aggregate("size") in {_MISMATCH, _CONFLICT}:
            blocking.append("size")
        if candidate.aggregate("time") == _MISMATCH:
            blocking.append("time")
        if candidate.presence_at_link == "absent":
            blocking.append("presence")
        if not blocking:
            continue
        contradictions.append(
            {
                "kind": "identity_contradicted",
                "message": (
                    f"object identifier points at {candidate.version}, but "
                    + ", ".join(blocking)
                    + " evidence excludes it"
                ),
                "versions": [candidate.version],
                "candidate_keys": [list(candidate.key)],
                "fields": candidate.as_dict()["excluded_fields"],
            }
        )

    internal_size = [
        candidate for candidate in candidates if candidate.aggregate("size") == _CONFLICT
    ]
    if internal_size:
        contradictions.append(
            {
                "kind": "shortcut_internal_conflict",
                "message": (
                    "the shortcut itself declares different target sizes in different fields"
                ),
                "versions": sorted({candidate.version for candidate in internal_size}),
                "fields": [
                    check.as_dict()
                    for candidate in internal_size
                    for check in candidate.checks
                    if check.kind == "size" and check.result == _CONFLICT
                ],
            }
        )

    return contradictions


def _blocking_contradictions(
    contradictions: Sequence[Mapping[str, Any]],
    adopted: Sequence[Candidate],
    ambiguous: Sequence[Candidate],
) -> List[Mapping[str, Any]]:
    if not contradictions:
        return []

    survivor_keys = {candidate.key for candidate in adopted} | {
        candidate.key for candidate in ambiguous
    }
    blocking: List[Mapping[str, Any]] = []

    for item in contradictions:
        kind = item["kind"]
        item_keys = {
            (str(key[0]), str(key[1]))
            for key in item.get("candidate_keys") or []
            if isinstance(key, (list, tuple)) and len(key) == 2
        }

        if kind in {"identity_disagreement", "identity_contradicted", "identity_conflict"}:
            if not survivor_keys or item_keys & survivor_keys or not item_keys:
                blocking.append(item)
            continue

        if kind in {"catalog_volume_conflict", "catalog_object_conflict"}:
            # A duplicate claim matters when it involves a version that would
            # otherwise be selected.  A duplicate entirely elsewhere is still
            # reported, but it is a catalog anomaly, not a reason to discard a
            # well-identified target.
            if not survivor_keys or item_keys & survivor_keys:
                blocking.append(item)
            continue

        if kind == "shortcut_internal_conflict":
            blocking.append(item)
            continue

        blocking.append(item)

    return blocking


def _select_target(
    adopted: Sequence[Candidate], ambiguous: Sequence[Candidate]
) -> Optional[str]:
    if len(adopted) == 1:
        return "unique"
    if len(adopted) > 1:
        return "multiple"
    if ambiguous:
        return "multiple"
    return "unresolved"


def resolve_target_version(
    lnk: Union[LnkFile, bytes, bytearray, Any],
    catalog: Union[Mapping[str, Any], str],
    rules: Optional[Union[Mapping[str, Any], str]] = None,
    collected_at: Optional[Union[str, datetime, Mapping[str, Any]]] = None,
) -> Resolution:
    """Resolve the catalog version a shortcut points at.

    Parameters
    ----------
    lnk:
        ``LnkFile`` instance, raw bytes, or a readable binary file handle.
    catalog:
        Catalog mapping or path to its JSON document.  It may use
        ``volumes[].entries[]`` or a flat ``entries[]`` list; see
        ``doc/version-resolution.md`` for the accepted shape.
    rules:
        Explicit identity rules or a path to them.  Missing sections fall back
        to the canonical LNK-to-catalog mapping.
    collected_at:
        Collection timestamp override.  The shortcut timestamp is never used
        as evidence that a version survived until collection.

    Returns
    -------
    Resolution
        ``status`` is one of ``unique``, ``multiple``, ``contradiction`` or
        ``unresolved``.  Each candidate carries the raw LNK and catalog values
        used to adopt or exclude it.
    """

    lnk_file = _load_lnk(lnk)
    data = lnk_file.get_json(True)
    catalog_document, _ = _load_document(catalog, "catalog", allow_directory=True)
    catalog_data = _normalize_catalog(catalog_document)
    if collected_at is not None:
        catalog_data["collection"] = _parse_time(
            collected_at, source="resolve.collected_at"
        )

    rule_document, rules_source = _merge_rules(rules)

    link_time = None
    for selector in ("header.modified_time", "header.accessed_time", "header.creation_time"):
        raw_time = _resolve_selector(data, selector)
        if raw_time is not None:
            link_time = _parse_time(raw_time, precision="1us", source=selector)
            break

    entries = catalog_data["entries"]
    candidates = [
        _evaluate_candidate(
            entry,
            data,
            rule_document,
            link_time,
            catalog_data["collection"],
            entries,
        )
        for entry in entries
    ]

    contradictions = _duplicate_identity_contradictions(entries)
    contradictions.extend(_candidate_contradictions(candidates))

    adopted = [candidate for candidate in candidates if candidate.status == "adopted"]
    ambiguous = [candidate for candidate in candidates if candidate.status == "ambiguous"]
    excluded = [candidate for candidate in candidates if candidate.status == "excluded"]

    warnings: List[str] = []
    missing_identifier = _missing_identifier_warning(data, rule_document)
    if missing_identifier:
        warnings.append(missing_identifier)

    blocking = _blocking_contradictions(contradictions, adopted, ambiguous)
    if blocking:
        status = "contradiction"
    else:
        status = _select_target(adopted, ambiguous)
    non_blocking = [
        item for item in contradictions if item not in blocking
    ]
    if status == "unique" and non_blocking:
        warnings.append(
            "The catalog contains identity anomalies elsewhere; they are reported "
            "under contradictions but do not involve the selected version."
        )

    target_candidate = adopted[0] if status == "unique" else None
    target_version = target_candidate.version if target_candidate else None
    target = None
    if target_candidate is not None:
        target = {
            "version": target_candidate.version,
            "volume_id": target_candidate.volume_id,
            "path": target_candidate.path,
            "strength": target_candidate.strength,
            "existed_at_collection": target_candidate.existed_at_collection,
            "presence_at_shortcut_time": target_candidate.presence_at_link,
        }

    if status == "multiple":
        warnings.append(
            "More than one catalog entry remains compatible; no candidate was chosen "
            "arbitrarily."
        )
    if status == "unresolved":
        warnings.append(
            "No catalog entry could be connected to the shortcut with the supplied "
            "identity rules."
        )
    if status == "contradiction":
        warnings.append(
            "Identity, size, time or presence evidence contradicts itself; the "
            "conflicting raw fields are listed under contradictions."
        )
    if target_candidate is not None and target_candidate.existed_at_collection != "yes":
        warnings.append(
            "The catalog does not prove that this version survived until the "
            "collection date; the shortcut timestamp is not used as that proof."
        )
    if ambiguous and adopted:
        warnings.append(
            "Additional weakly identified entries remain ambiguous and are not "
            "treated as alternative targets."
        )
    if excluded and not adopted and not ambiguous:
        warnings.append(
            "Every catalog entry was excluded; inspect excluded_fields for the "
            "first contradictory field."
        )

    return Resolution(
        status=status,
        target_version=target_version,
        target=target,
        as_of=link_time,
        collection=catalog_data["collection"],
        candidates=candidates,
        contradictions=contradictions,
        warnings=warnings,
        rules_source=rules_source,
    )


def _missing_identifier_warning(
    data: Mapping[str, Any], rules: Mapping[str, Any]
) -> Optional[str]:
    for kind in ("volume", "object"):
        for rule in rules.get(kind) or []:
            if _resolve_selector(data, rule["lnk"]) is not None:
                return None
    return (
        "The shortcut exposes no volume or object identifier covered by the rules; "
        "path, size and time cannot establish identity on their own."
    )


def _print_text(resolution: Resolution) -> None:
    print(f"Status: {resolution.status}")
    if resolution.target_version:
        print(f"Target version: {resolution.target_version}")
    if resolution.as_of:
        print(
            f"Shortcut written: {resolution.as_of.value.isoformat()} "
            f"({resolution.as_of.precision})"
        )
    if resolution.collection:
        print(
            f"Collection: {resolution.collection.value.isoformat()} "
            f"({resolution.collection.precision})"
        )
    for candidate in resolution.candidates:
        print(
            f"  - {candidate.version} [{candidate.status}] "
            f"({candidate.strength or 'no identity'})"
        )
        for reason in candidate.reasons:
            print(f"      {reason}")
    for contradiction in resolution.contradictions:
        print(f"Contradiction: {contradiction['kind']}: {contradiction['message']}")
    for warning in resolution.warnings:
        print(f"Warning: {warning}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resolve a Windows shortcut to a target-volume catalog version"
    )
    parser.add_argument("file", metavar="FILE", help="path to the LNK file")
    parser.add_argument(
        "--catalog", required=True, help="path to the volume-history catalog JSON"
    )
    parser.add_argument("--rules", help="path to explicit identity-rule JSON")
    parser.add_argument(
        "--collected-at",
        help="collection timestamp override (ISO 8601), never inferred from the shortcut",
    )
    parser.add_argument(
        "--text", action="store_true", help="print a short human-readable summary"
    )
    args = parser.parse_args()

    with open(args.file, "rb") as handle:
        lnk = LnkFile(fhandle=handle)
    resolution = resolve_target_version(
        lnk,
        args.catalog,
        rules=args.rules,
        collected_at=args.collected_at,
    )
    if args.text:
        _print_text(resolution)
    else:
        print(
            json.dumps(
                resolution.as_dict(),
                indent=4,
                sort_keys=True,
                default=_jsonable,
            )
        )


if __name__ == "__main__":
    main()
