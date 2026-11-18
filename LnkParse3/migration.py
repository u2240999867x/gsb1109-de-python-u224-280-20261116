"""Plan minimal LNK field corrections for a material package migration.

The planner is read-only: it never launches or rewrites a shortcut. It consumes
a JSON description of the source machine, the target machine, the material
migration mapping and the fields that may change, then reports the smallest set
of field corrections that keeps every provable declaration pointed at the same
material under the target configuration.

Declarations are symbolic. Environment variables are expanded transitively;
missing definitions and cycles are reported separately. A known-folder value is
a base plus an appended relative segment, never a second absolute root. When
several declarations of one material disagree after migration, the group is
left for confirmation and every declaration keeps its original symbolic form.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import ntpath
import re
import sys
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any


VAR_RE = re.compile(r"%([^%]+)%")
GUID_RE = re.compile(r"^\s*\{([^}]+)\}(.*)$", re.DOTALL)
DRIVE_RE = re.compile(r"^[A-Za-z]:$|^[A-Za-z]:\\")
KIND_ALIASES = {
    "environment": "env",
    "environment_variable": "env",
    "variable": "env",
    "env": "env",
    "knownfolder": "known_folder",
    "known-folder": "known_folder",
    "known_folder": "known_folder",
    "relative": "relative",
    "rel": "relative",
    "literal": "literal",
    "absolute": "literal",
    "path": "literal",
}
STRATEGY_ORDER = {"env": 0, "known_folder": 1, "literal": 2, "relative": 3}
ROLE_BY_FIELD = {
    "relative_path": "target",
    "link_info.local_base_path": "target",
    "link_info.local_base_path_unicode": "target",
    "extra.ENVIRONMENTAL_VARIABLES_LOCATION_BLOCK.target_ansi": "target",
    "extra.ENVIRONMENTAL_VARIABLES_LOCATION_BLOCK.target_unicode": "target",
    "working_directory": "directory",
    "icon_location": "icon",
    "extra.ICON_LOCATION_BLOCK.target_ansi": "icon",
    "extra.ICON_LOCATION_BLOCK.target_unicode": "icon",
}
SUFFIX_FIELD_BY_FIELD = {
    "link_info.local_base_path": "link_info.common_path_suffix",
    "link_info.local_base_path_unicode": "link_info.common_path_suffix_unicode",
}


class PlanError(ValueError):
    """Raised when the plan document itself cannot be interpreted."""


@dataclass(frozen=True)
class Fault:
    code: str
    message: str
    chain: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        result = {"code": self.code, "message": self.message}
        if self.chain:
            result["chain"] = list(self.chain)
        return result


@dataclass
class Resolution:
    kind: str
    value: str
    path: str | None
    faults: list[Fault] = dataclass_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.path is not None and not self.faults


@dataclass(frozen=True)
class Migration:
    identifier: str
    source: str
    target: str
    entry_type: str


@dataclass(frozen=True)
class Material:
    key: str
    source_path: str
    target_path: str
    migration: str | None
    reason: str | None = None


@dataclass
class Claim:
    shortcut: str
    declaration_id: str
    field: str
    declaration: dict[str, Any]
    role: str
    value: str
    kind: str
    source: Resolution
    natural_target: Resolution
    material: Material | None
    source_base: str | None
    target_base: str | None
    conflict: str | None = None


def _dedupe_faults(faults: list[Fault]) -> list[Fault]:
    seen = set()
    result = []
    for fault in faults:
        key = (fault.code, fault.message, fault.chain)
        if key in seen:
            continue
        seen.add(key)
        result.append(fault)
    return result


def _canonical_guid(value: str) -> str:
    text = value.strip()
    text = text.removeprefix("{")
    text = text.removesuffix("}")
    return "{%s}" % text.upper()


def _to_backslashes(value: str) -> str:
    return value.replace("/", "\\")


def _norm_path(value: str) -> str:
    text = _to_backslashes(value)
    text = text.removeprefix("\\\\?\\")
    return ntpath.normpath(text)


def _path_key(value: str) -> str:
    return ntpath.normcase(_norm_path(value)).rstrip("\\")


def _is_absolute(value: str) -> bool:
    text = _to_backslashes(value)
    return bool(DRIVE_RE.match(text)) or text.startswith("\\\\")


def _is_rooted_without_drive(value: str) -> bool:
    text = _to_backslashes(value)
    return text.startswith("\\") and not text.startswith("\\\\")


def _is_new_absolute_root(value: str) -> bool:
    """Whether an appended segment starts a different absolute root.

    One leading separator is only a join separator. A drive-qualified segment
    or a UNC path would replace the base root and must not be joined.
    """

    text = _to_backslashes(value)
    if text.startswith("\\\\"):
        return True
    stripped = text.lstrip("\\")
    return bool(DRIVE_RE.match(stripped))


def _join(base: str, suffix: str) -> str:
    tail = _to_backslashes(suffix).lstrip("\\")
    if not tail:
        return _norm_path(base)
    return _norm_path(base.rstrip("\\") + "\\" + tail)


def _is_under(path: str, root: str) -> bool:
    path_key = _path_key(path)
    root_key = _path_key(root)
    return path_key == root_key or path_key.startswith(root_key + "\\")


def _suffix_of(path: str, root: str) -> str:
    normalized_path = _norm_path(path)
    normalized_root = _norm_path(root).rstrip("\\")
    if _path_key(normalized_path) == _path_key(normalized_root):
        return ""
    return normalized_path[len(normalized_root) + 1 :]


def _root_depth(base: str) -> int:
    text = _to_backslashes(base)
    if text.startswith("\\\\"):
        parts = [part for part in text.split("\\") if part]
        return max(0, len(parts) - 2)
    parts = [part for part in text.split("\\") if part]
    if parts and parts[0].endswith(":"):
        parts = parts[1:]
    return len(parts)


def _escapes_root(base: str, relative: str) -> bool:
    depth = _root_depth(base)
    for part in _to_backslashes(relative).split("\\"):
        if part in ("", "."):
            continue
        if part == "..":
            depth -= 1
            if depth < 0:
                return True
        else:
            depth += 1
    return False


def _kind_of(value: Any, explicit: str | None = None) -> str:
    if explicit:
        alias = KIND_ALIASES.get(str(explicit).strip().lower())
        if not alias:
            raise PlanError(f"Unknown declaration kind: {explicit!r}")
        return alias
    text = str(value).lstrip()
    if GUID_RE.match(text):
        return "known_folder"
    if text.startswith("."):
        return "relative"
    if VAR_RE.search(text):
        return "env"
    if _is_absolute(text):
        return "literal"
    return "relative"


def _identity_suffix(value: str) -> str:
    text = _to_backslashes(value).strip("\\")
    return ntpath.normcase(ntpath.normpath(text)) if text else ""


def _identity(
    raw: str,
    kind: str,
    resolution: Resolution,
    config: MachineConfig,
    base_identity: tuple | None = None,
) -> tuple | None:
    """Stable material identity for one declaration under one machine.

    Symbolic declarations are keyed by symbol plus appended suffix so a
    variable or known folder keeps the same material across machines even when
    its value differs. Literal and relative paths fall back to path identity.
    """

    if not resolution.ok:
        return None

    if kind == "known_folder":
        match = GUID_RE.match(raw)
        if not match:
            return ("path", _path_key(resolution.path))
        guid = _canonical_guid(match.group(1))
        expanded_suffix, faults = config.expand(match.group(2))
        if faults:
            return None
        return ("kf", guid, _identity_suffix(expanded_suffix))

    if kind == "env":
        match = re.match(r"^\s*(%[^%]+%)", raw)
        if not match:
            return ("path", _path_key(resolution.path))
        variable = match.group(1)[1:-1].upper()
        base_value = config.env.get(variable)
        if base_value is None:
            return ("path", _path_key(resolution.path))
        expanded_base, faults = config.expand(base_value)
        if faults or not _is_absolute(expanded_base):
            return ("path", _path_key(resolution.path))
        if not _is_under(resolution.path, expanded_base):
            return ("path", _path_key(resolution.path))
        suffix = _suffix_of(resolution.path, expanded_base)
        return ("env", variable, _identity_suffix(suffix))

    if kind == "relative":
        expanded, faults = config.expand(raw)
        if faults:
            return None
        return ("rel", base_identity, _identity_suffix(expanded))

    return ("path", _path_key(resolution.path))


def _identity_key(identity: tuple | None) -> str:
    if identity is None:
        return ""
    return json.dumps(identity, ensure_ascii=False, sort_keys=True)


class VariableReport:
    """Collect missing-variable and cycle reports without duplicates."""

    def __init__(self):
        self._entries: dict[tuple[str, str, tuple[str, ...]], dict[str, Any]] = {}

    def add(
        self,
        machine: str,
        code: str,
        chain: tuple[str, ...],
        message: str,
        user: str,
        affects_plan: bool,
    ) -> None:
        key = (machine, code, chain)
        entry = self._entries.get(key)
        if entry is None:
            entry = {
                "machine": machine,
                "code": code,
                "variable": chain[0] if chain else None,
                "chain": list(chain),
                "message": message,
                "used_by": [],
                "affects_plan": affects_plan,
            }
            self._entries[key] = entry
        if user not in entry["used_by"]:
            entry["used_by"].append(user)
        entry["affects_plan"] = entry["affects_plan"] or affects_plan

    def as_dict(self) -> dict[str, Any]:
        entries = sorted(
            self._entries.values(),
            key=lambda item: (item["machine"], item["code"], item["chain"], item["used_by"]),
        )
        missing = [entry for entry in entries if entry["code"] == "missing_variable"]
        cycles = [entry for entry in entries if entry["code"] == "variable_cycle"]
        return {"missing": missing, "cycles": cycles}

    @property
    def has_plan_affecting_faults(self) -> bool:
        return any(entry["affects_plan"] for entry in self._entries.values())


class MachineConfig:
    """One machine's variable, known-folder and drive namespace."""

    def __init__(self, name: str, document: dict[str, Any] | None):
        document = document or {}
        self.name = str(document.get("name") or name)
        raw_env = document.get("env") or {}
        self.env = {str(key).upper(): str(value) for key, value in raw_env.items()}
        self.env_name = {str(key).upper(): str(key) for key in raw_env}

        raw_folders = document.get("known_folders") or {}
        self.known_folders: dict[str, str] = {}
        for key, value in raw_folders.items():
            if isinstance(value, dict):
                value = value.get("path")
            if value is None:
                continue
            self.known_folders[_canonical_guid(str(key))] = str(value)

        raw_drives = document.get("drives")
        if raw_drives is None:
            self.drives = None
        elif isinstance(raw_drives, dict):
            self.drives = {
                str(key).upper().rstrip("\\") for key, enabled in raw_drives.items() if enabled
            }
        else:
            self.drives = {str(item).upper().rstrip("\\") for item in raw_drives}

    def expand(self, value: str, stack: tuple[str, ...] = ()) -> tuple[str, list[Fault]]:
        faults: list[Fault] = []

        def replace(match: re.Match) -> str:
            name = match.group(1)
            key = name.upper()
            if key in stack:
                start = stack.index(key)
                cycle = (*stack[start:], key)
                faults.append(
                    Fault(
                        "variable_cycle",
                        "Variable cycle: " + " -> ".join("%" + item + "%" for item in cycle),
                        cycle,
                    )
                )
                return match.group(0)
            if key not in self.env:
                chain = (*stack, key)
                faults.append(
                    Fault(
                        "missing_variable",
                        "Missing variable: " + " -> ".join("%" + item + "%" for item in chain),
                        chain,
                    )
                )
                return match.group(0)
            expanded, nested = self.expand(self.env[key], (*stack, key))
            faults.extend(nested)
            return expanded

        result = VAR_RE.sub(replace, value)
        return result, _dedupe_faults(faults)

    def validate(self, reporter: VariableReport) -> None:
        for key in sorted(self.env):
            _expanded, faults = self.expand(self.env[key], (key,))
            for fault in faults:
                reporter.add(
                    self.name,
                    fault.code,
                    fault.chain,
                    fault.message,
                    "env:" + self.env_name.get(key, key),
                    False,
                )

    def drive_available(self, path: str) -> bool:
        if self.drives is None:
            return True
        text = _to_backslashes(path)
        if text.startswith("\\\\"):
            return True
        match = re.match(r"^[A-Za-z]:", text)
        if not match:
            return False
        return match.group(0).upper() in self.drives

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "env": dict(sorted(self.env.items())),
            "known_folders": dict(sorted(self.known_folders.items())),
            "drives": None if self.drives is None else sorted(self.drives),
        }


class Resolver:
    """Resolve one symbolic declaration under one machine configuration."""

    def __init__(self, config: MachineConfig, shortcut_dir: str | None = None):
        self.config = config
        self.shortcut_dir = shortcut_dir

    def resolve(
        self,
        value: str,
        kind: str | None = None,
        base: str | None = None,
        base_path: str | None = None,
        suffix: str | None = None,
        allow_relative: bool = True,
    ) -> Resolution:
        raw = str(value)
        resolution = self._resolve_value(raw, kind, base, base_path, allow_relative)
        if not suffix or resolution.faults:
            return resolution

        expanded_suffix, suffix_faults = self.config.expand(str(suffix))
        if suffix_faults:
            return Resolution(resolution.kind, raw, None, suffix_faults)
        if _is_new_absolute_root(expanded_suffix):
            return Resolution(
                resolution.kind,
                raw,
                None,
                [
                    Fault(
                        "absolute_suffix",
                        f"Appended segment starts a new absolute root: {suffix!r}",
                    )
                ],
            )
        if _escapes_root(resolution.path, expanded_suffix):
            return Resolution(
                resolution.kind,
                raw,
                None,
                [Fault("relative_escape", f"Appended segment escapes its base: {suffix!r}")],
            )
        path = _join(resolution.path, expanded_suffix)
        return self._checked_path(resolution.kind, raw, path)

    def _resolve_value(
        self,
        raw: str,
        kind: str | None,
        base: str | None,
        base_path: str | None,
        allow_relative: bool,
    ) -> Resolution:
        resolved_kind = _kind_of(raw, kind)
        if resolved_kind == "env":
            return self._resolve_env(raw, resolved_kind)
        if resolved_kind == "known_folder":
            return self._resolve_known_folder(raw, resolved_kind)
        if resolved_kind == "relative":
            return self._resolve_relative(raw, resolved_kind, base, base_path, allow_relative)
        return self._resolve_literal(raw, resolved_kind)

    def _resolve_env(self, raw: str, kind: str) -> Resolution:
        expanded, faults = self.config.expand(raw)
        if faults:
            return Resolution(kind, raw, None, faults)
        if not _is_absolute(expanded):
            return Resolution(
                kind,
                raw,
                None,
                [Fault("not_absolute", f"Environment value is not absolute: {raw!r}")],
            )
        return self._checked_path(kind, raw, expanded)

    def _resolve_literal(self, raw: str, kind: str) -> Resolution:
        expanded, faults = self.config.expand(raw)
        if faults:
            return Resolution(kind, raw, None, faults)
        if _is_rooted_without_drive(expanded):
            return Resolution(
                kind,
                raw,
                None,
                [
                    Fault(
                        "rooted_without_drive",
                        f"Path is rooted without a drive and cannot be mapped: {raw!r}",
                    )
                ],
            )
        if not _is_absolute(expanded):
            return Resolution(
                kind,
                raw,
                None,
                [Fault("not_absolute", f"Path is not absolute: {raw!r}")],
            )
        return self._checked_path(kind, raw, expanded)

    def _checked_path(self, kind: str, raw: str, expanded: str) -> Resolution:
        path = _norm_path(expanded)
        if not self.config.drive_available(path):
            return Resolution(
                kind,
                raw,
                None,
                [
                    Fault(
                        "drive_unavailable",
                        f"Drive is not available on {self.config.name}: {path}",
                    )
                ],
            )
        return Resolution(kind, raw, path)

    def _resolve_known_folder(self, raw: str, kind: str) -> Resolution:
        match = GUID_RE.match(raw)
        if not match:
            return Resolution(
                kind,
                raw,
                None,
                [Fault("invalid_known_folder", f"Invalid known-folder declaration: {raw!r}")],
            )
        guid = _canonical_guid(match.group(1))
        suffix = match.group(2)
        if guid not in self.config.known_folders:
            return Resolution(
                kind,
                raw,
                None,
                [Fault("missing_known_folder", f"Known folder is not mapped: {guid}")],
            )

        base_resolution = self._resolve_literal(self.config.known_folders[guid], "literal")
        if base_resolution.faults:
            return Resolution(kind, raw, None, base_resolution.faults)

        expanded_suffix, suffix_faults = self.config.expand(suffix)
        if suffix_faults:
            return Resolution(kind, raw, None, suffix_faults)
        if _is_new_absolute_root(expanded_suffix):
            return Resolution(
                kind,
                raw,
                None,
                [
                    Fault(
                        "absolute_known_folder_suffix",
                        "Known-folder suffix starts a new absolute root and was not joined: "
                        f"{guid}{suffix}",
                    )
                ],
            )
        if _escapes_root(base_resolution.path, expanded_suffix):
            return Resolution(
                kind,
                raw,
                None,
                [
                    Fault(
                        "relative_escape",
                        f"Known-folder suffix escapes the drive root: {guid}{suffix}",
                    )
                ],
            )
        path = _join(base_resolution.path, expanded_suffix)
        return self._checked_path(kind, raw, path)

    def _resolve_relative(
        self,
        raw: str,
        kind: str,
        base: str | None,
        base_path: str | None,
        allow_relative: bool,
    ) -> Resolution:
        if not allow_relative:
            return Resolution(
                kind,
                raw,
                None,
                [Fault("relative_base_not_absolute", f"Relative base cannot be relative: {raw!r}")],
            )

        expanded, faults = self.config.expand(raw)
        if faults:
            return Resolution(kind, raw, None, faults)
        if _is_absolute(expanded) or _is_rooted_without_drive(expanded):
            return Resolution(
                kind,
                raw,
                None,
                [
                    Fault(
                        "absolute_relative_value",
                        f"Relative declaration resolves to an absolute root: {raw!r}",
                    )
                ],
            )

        if base_path is not None:
            resolved_base = base_path
        elif base is None or base == "shortcut_dir":
            resolved_base = self.shortcut_dir
            if resolved_base is None:
                return Resolution(
                    kind,
                    raw,
                    None,
                    [Fault("missing_relative_base", f"No shortcut directory for: {raw!r}")],
                )
        else:
            base_resolution = self._resolve_value(str(base), None, None, None, False)
            if base_resolution.faults:
                return Resolution(kind, raw, None, base_resolution.faults)
            resolved_base = base_resolution.path

        if _escapes_root(resolved_base, expanded):
            return Resolution(
                kind,
                raw,
                None,
                [Fault("relative_escape", f"Relative path escapes the drive root: {raw!r}")],
            )
        path = _join(resolved_base, expanded)
        return self._checked_path(kind, raw, path)


class MigrationMap:
    """Forward and reverse material mapping with explicit ambiguity."""

    def __init__(self, document: dict[str, Any], source: MachineConfig, target: MachineConfig):
        self.source = source
        self.target = target
        self.migrations: list[Migration] = []
        self.diagnostics: list[dict[str, Any]] = []
        self.ambiguous_sources: set[str] = set()
        self.ambiguous_targets: set[str] = set()
        entries = document.get("migrations")
        if entries is None:
            entries = document.get("material_mapping") or []
        self._build(entries)

    def _build(self, entries: Any) -> None:
        source_resolver = Resolver(self.source)
        target_resolver = Resolver(self.target)
        raw_entries = []
        if isinstance(entries, dict):
            for key, value in entries.items():
                if isinstance(value, dict):
                    raw_entries.append(
                        {
                            "id": key,
                            "source": value.get("source"),
                            "target": value.get("target"),
                            "type": value.get("type"),
                        }
                    )
                else:
                    raw_entries.append({"id": key, "source": key, "target": value})
        elif isinstance(entries, (list, tuple)):
            raw_entries = list(entries)
        else:
            self.diagnostics.append(
                {
                    "code": "invalid_migrations",
                    "message": "migrations must be a list or an object",
                }
            )
            raw_entries = []

        parsed: list[Migration] = []
        for index, entry in enumerate(raw_entries):
            if not isinstance(entry, dict):
                self.diagnostics.append(
                    {"code": "invalid_migration", "message": f"Migration {index} is not an object"}
                )
                continue
            identifier = str(entry.get("id") or entry.get("material") or f"migration-{index}")
            raw_source = entry.get("source")
            raw_target = entry.get("target")
            if raw_source is None or raw_target is None:
                self.diagnostics.append(
                    {
                        "code": "invalid_migration",
                        "message": f"Migration {identifier!r} needs both source and target",
                    }
                )
                continue
            source_resolution = source_resolver.resolve(str(raw_source), allow_relative=False)
            target_resolution = target_resolver.resolve(str(raw_target), allow_relative=False)
            if not source_resolution.ok:
                self.diagnostics.append(
                    {
                        "code": "invalid_migration_source",
                        "message": f"Migration {identifier!r} source is unresolved",
                        "faults": [fault.as_dict() for fault in source_resolution.faults],
                    }
                )
                continue
            if not target_resolution.ok:
                self.diagnostics.append(
                    {
                        "code": "invalid_migration_target",
                        "message": f"Migration {identifier!r} target is unresolved",
                        "faults": [fault.as_dict() for fault in target_resolution.faults],
                    }
                )
                continue
            entry_type = str(entry.get("type") or "dir").lower()
            if entry_type not in ("dir", "file"):
                self.diagnostics.append(
                    {
                        "code": "invalid_migration_type",
                        "message": f"Migration {identifier!r} has unknown type {entry_type!r}",
                    }
                )
                continue
            parsed.append(
                Migration(identifier, source_resolution.path, target_resolution.path, entry_type)
            )

        by_source: dict[str, Migration] = {}
        for migration in parsed:
            key = _path_key(migration.source)
            previous = by_source.get(key)
            if previous and _path_key(previous.target) != _path_key(migration.target):
                self.ambiguous_sources.add(key)
                self.diagnostics.append(
                    {
                        "code": "migration_conflict",
                        "message": (
                            f"Source {migration.source!r} maps to both "
                            f"{previous.target!r} and {migration.target!r}"
                        ),
                        "migrations": [previous.identifier, migration.identifier],
                    }
                )
            else:
                by_source[key] = migration

        by_target: dict[str, Migration] = {}
        for migration in parsed:
            key = _path_key(migration.target)
            previous = by_target.get(key)
            if previous and _path_key(previous.source) != _path_key(migration.source):
                self.ambiguous_targets.add(key)
                self.diagnostics.append(
                    {
                        "code": "target_collision",
                        "message": (
                            f"Target {migration.target!r} comes from both "
                            f"{previous.source!r} and {migration.source!r}"
                        ),
                        "migrations": [previous.identifier, migration.identifier],
                    }
                )
            else:
                by_target[key] = migration

        self.migrations = [
            migration
            for migration in parsed
            if _path_key(migration.source) not in self.ambiguous_sources
        ]

    def forward(self, path: str) -> Material:
        normalized = _norm_path(path)
        key = _path_key(normalized)
        if key in self.ambiguous_sources:
            return Material(
                key="path:" + key,
                source_path=normalized,
                target_path=normalized,
                migration=None,
                reason="mapping_ambiguous",
            )
        migration = self._longest(normalized, "source")
        if migration is None:
            return Material("path:" + key, normalized, normalized, None)
        suffix = _suffix_of(normalized, migration.source)
        return Material(
            key="path:" + key,
            source_path=normalized,
            target_path=_join(migration.target, suffix),
            migration=migration.identifier,
        )

    def reverse(self, path: str) -> tuple[str | None, str | None]:
        normalized = _norm_path(path)
        migration = self._longest(normalized, "target")
        if migration is None:
            return "path:" + _path_key(normalized), None
        if _path_key(migration.target) in self.ambiguous_targets:
            return None, "mapping_ambiguous_target"
        suffix = _suffix_of(normalized, migration.target)
        return "path:" + _path_key(_join(migration.source, suffix)), None

    def _longest(self, path: str, attribute: str) -> Migration | None:
        best = None
        best_length = -1
        for migration in self.migrations:
            root = getattr(migration, attribute)
            if migration.entry_type == "file":
                matches = _path_key(path) == _path_key(root)
            else:
                matches = _is_under(path, root)
            if not matches:
                continue
            length = len(_path_key(root))
            if length > best_length:
                best = migration
                best_length = length
        return best

    def as_dict(self) -> dict[str, Any]:
        return {
            "migrations": [
                {
                    "id": migration.identifier,
                    "source": migration.source,
                    "target": migration.target,
                    "type": migration.entry_type,
                }
                for migration in self.migrations
            ],
            "diagnostics": self.diagnostics,
            "ambiguous_sources": sorted(self.ambiguous_sources),
            "ambiguous_targets": sorted(self.ambiguous_targets),
        }

    @property
    def has_mapping_conflicts(self) -> bool:
        return any(
            diagnosis["code"] in ("migration_conflict", "target_collision")
            for diagnosis in self.diagnostics
        )


def _role_of(declaration: dict[str, Any]) -> str:
    explicit = declaration.get("role")
    if explicit:
        return str(explicit)
    field = str(declaration.get("field") or "")
    if field in ROLE_BY_FIELD:
        return ROLE_BY_FIELD[field]
    lowered = field.lower()
    if "known_folder" in lowered:
        return "known_folder"
    if "icon" in lowered:
        return "icon"
    if "working" in lowered:
        return "directory"
    return "field:" + lowered


def _allowed(document: dict[str, Any], shortcut: dict[str, Any], field: str) -> bool:
    patterns = shortcut.get("allowed_fields")
    if patterns is None:
        patterns = document.get("allowed_fields")
    if patterns is None:
        return False
    if isinstance(patterns, str):
        patterns = [patterns]
    return any(fnmatch.fnmatchcase(field, str(pattern)) for pattern in patterns)


def _declarations(shortcut: dict[str, Any]) -> list[dict[str, Any]]:
    raw = shortcut.get("declarations")
    if raw is None:
        return []
    if isinstance(raw, dict):
        return [
            {"field": str(name), "value": value} for name, value in raw.items() if value is not None
        ]
    result = []
    for item in raw:
        if isinstance(item, str):
            field, _, value = item.partition("=")
            result.append({"field": field, "value": value})
        elif isinstance(item, dict):
            result.append(dict(item))
        else:
            raise PlanError(f"Invalid declaration: {item!r}")
    return result


def declarations_from_lnk_json(lnk_json: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract path-bearing fields from ``LnkFile.get_json(True)`` output."""

    declarations: list[dict[str, Any]] = []

    def add(
        field: str,
        value: Any,
        kind: str | None = None,
        base: str | None = None,
        suffix: Any = None,
    ) -> None:
        if value in (None, ""):
            return
        item = {"field": field, "value": str(value)}
        if kind:
            item["kind"] = kind
        if base:
            item["base"] = base
        if suffix not in (None, ""):
            item["suffix"] = str(suffix)
        declarations.append(item)

    data = lnk_json.get("data") or {}
    add("relative_path", data.get("relative_path"), base="shortcut_dir")
    add("working_directory", data.get("working_directory"))
    add("icon_location", data.get("icon_location"))

    link_info = lnk_json.get("link_info") or {}
    add(
        "link_info.local_base_path",
        link_info.get("local_base_path"),
        suffix=link_info.get("common_path_suffix"),
    )
    add(
        "link_info.local_base_path_unicode",
        link_info.get("local_base_path_unicode"),
        suffix=link_info.get("common_path_suffix_unicode"),
    )

    extra = lnk_json.get("extra") or {}
    env_block = extra.get("ENVIRONMENTAL_VARIABLES_LOCATION_BLOCK") or {}
    add("extra.ENVIRONMENTAL_VARIABLES_LOCATION_BLOCK.target_ansi", env_block.get("target_ansi"))
    add(
        "extra.ENVIRONMENTAL_VARIABLES_LOCATION_BLOCK.target_unicode",
        env_block.get("target_unicode"),
    )
    icon_block = extra.get("ICON_LOCATION_BLOCK") or {}
    add("extra.ICON_LOCATION_BLOCK.target_ansi", icon_block.get("target_ansi"))
    add("extra.ICON_LOCATION_BLOCK.target_unicode", icon_block.get("target_unicode"))
    return declarations


def _shortcut_dirs(
    shortcut: dict[str, Any],
    source_config: MachineConfig,
    target_config: MachineConfig,
    mapping: MigrationMap,
) -> tuple[str | None, str | None]:
    source_dir = None
    if shortcut.get("dir") is not None:
        resolution = Resolver(source_config).resolve(str(shortcut["dir"]), allow_relative=False)
        if resolution.ok:
            source_dir = resolution.path

    target_dir = None
    if shortcut.get("target_dir") is not None:
        resolution = Resolver(target_config).resolve(
            str(shortcut["target_dir"]), allow_relative=False
        )
        if resolution.ok:
            target_dir = resolution.path
    elif source_dir:
        target_dir = mapping.forward(source_dir).target_path
    return source_dir, target_dir


def _bases(
    declaration: dict[str, Any],
    source_dir: str | None,
    target_dir: str | None,
    mapping: MigrationMap,
    target_config: MachineConfig,
) -> tuple[str | None, str | None]:
    source_base = source_dir
    target_base = target_dir
    base = declaration.get("base")
    if base and base != "shortcut_dir":
        source_resolution = Resolver(mapping.source).resolve(str(base), allow_relative=False)
        if not source_resolution.ok:
            return None, None
        source_base = source_resolution.path
        mapped = mapping.forward(source_resolution.path).target_path
        target_resolution = Resolver(target_config).resolve(mapped, allow_relative=False)
        target_base = target_resolution.path if target_resolution.ok else None
    return source_base, target_base


def _candidate_values(
    required: str,
    declaration: dict[str, Any],
    original_kind: str,
    target_config: MachineConfig,
    target_base: str | None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    original_value = str(declaration.get("value", ""))
    symbols = {name.upper() for name in VAR_RE.findall(original_value)}
    guid_match = GUID_RE.match(original_value)
    if guid_match:
        symbols.add(_canonical_guid(guid_match.group(1)))

    target_resolver = Resolver(target_config)
    for key, raw_value in target_config.env.items():
        value_resolution = target_resolver.resolve(raw_value, allow_relative=False)
        if not value_resolution.ok or not _is_under(required, value_resolution.path):
            continue
        suffix = _suffix_of(required, value_resolution.path)
        name = target_config.env_name.get(key, key)
        value = "%" + name + "%"
        if suffix:
            value += "\\" + suffix
        candidates.append(
            {
                "value": value,
                "kind": "env",
                "symbol": key,
                "anchor_length": len(suffix),
                "strategy": "environment variable",
            }
        )

    for guid, raw_value in target_config.known_folders.items():
        value_resolution = target_resolver.resolve(raw_value, allow_relative=False)
        if not value_resolution.ok or not _is_under(required, value_resolution.path):
            continue
        suffix = _suffix_of(required, value_resolution.path)
        value = guid + ("\\" + suffix if suffix else "")
        candidates.append(
            {
                "value": value,
                "kind": "known_folder",
                "symbol": guid,
                "anchor_length": len(suffix),
                "strategy": "known folder",
            }
        )

    relative_allowed = "relative_path" in (declaration.get("field") or "")
    if relative_allowed and target_base and _is_absolute(required) and _is_absolute(target_base):
        try:
            relative = ntpath.relpath(required, target_base)
        except ValueError:
            relative = None
        if relative is not None and not _is_absolute(relative):
            if relative == ".":
                value = "."
            elif relative.startswith(".."):
                value = relative
            else:
                value = ".\\" + relative
            if not _escapes_root(target_base, relative):
                candidates.append(
                    {
                        "value": value,
                        "kind": "relative",
                        "symbol": None,
                        "anchor_length": len(value),
                        "strategy": "relative path",
                    }
                )

    candidates.append(
        {
            "value": required,
            "kind": "literal",
            "symbol": None,
            "anchor_length": len(required),
            "strategy": "literal path",
        }
    )

    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate in candidates:
        unique[(candidate["kind"], candidate["value"].lower())] = candidate

    def sort_key(candidate: dict[str, Any]) -> tuple:
        return (
            0 if candidate["kind"] == original_kind else 1,
            0 if candidate.get("symbol") in symbols else 1,
            candidate["anchor_length"],
            len(candidate["value"]),
            STRATEGY_ORDER[candidate["kind"]],
            candidate["value"].lower(),
        )

    return sorted(unique.values(), key=sort_key)


def _choose_candidate(
    required: str,
    declaration: dict[str, Any],
    original_kind: str,
    target_config: MachineConfig,
    target_base: str | None,
) -> dict[str, Any] | None:
    resolver = Resolver(target_config)
    for candidate in _candidate_values(
        required, declaration, original_kind, target_config, target_base
    ):
        verification = resolver.resolve(
            candidate["value"],
            kind=candidate["kind"],
            base=declaration.get("base"),
            base_path=target_base if candidate["kind"] == "relative" else None,
        )
        if verification.ok and _path_key(verification.path) == _path_key(required):
            chosen = dict(candidate)
            chosen["resolved_to"] = verification.path
            return chosen
    return None


def _conflict(group: list[Claim]) -> dict[str, Any] | None:
    """Defer a group that asserts one source material but diverges later.

    The group must name the same material on the source side. Two target
    expressions then either agree, or the migration cannot prove a single
    destination and every declaration keeps its original symbol.
    """

    resolved = [
        claim
        for claim in group
        if claim.source.ok and claim.material is not None and claim.material.reason is None
    ]
    if len(resolved) < 2:
        return None

    source_keys = {_path_key(claim.material.source_path) for claim in resolved}
    if len(source_keys) > 1:
        return {
            "reason": "source_conflict",
            "message": "Declarations resolve to different source materials",
            "fields": [claim.field for claim in resolved],
            "source_paths": sorted({claim.material.source_path for claim in resolved}),
        }

    material_keys = {_path_key(claim.material.target_path) for claim in resolved if claim.material}
    natural_targets = [claim for claim in resolved if claim.natural_target.ok]
    target_keys = {_path_key(claim.natural_target.path) for claim in natural_targets}
    if len(material_keys) > 1 or len(target_keys) > 1:
        return {
            "reason": "post_migration_conflict",
            "message": "Declarations resolve to different targets after migration",
            "fields": [claim.field for claim in resolved],
            "target_paths": sorted({claim.natural_target.path for claim in natural_targets}),
            "material_targets": sorted(
                {claim.material.target_path for claim in resolved if claim.material}
            ),
        }
    if natural_targets and material_keys:
        material_key = next(iter(material_keys))
        for claim in natural_targets:
            if _path_key(claim.natural_target.path) != material_key:
                return {
                    "reason": "post_migration_conflict",
                    "message": ("A declaration resolves somewhere other than the mapped material"),
                    "fields": [claim.field for claim in resolved],
                    "target_paths": sorted(
                        {claim.natural_target.path for claim in natural_targets}
                    ),
                    "material_targets": sorted(
                        {claim.material.target_path for claim in resolved if claim.material}
                    ),
                }
    return None


class Planner:
    def __init__(self, document: dict[str, Any]):
        if not isinstance(document, dict):
            raise PlanError("Plan document must be a JSON object")
        self.document = document
        self.source = MachineConfig("source", document.get("source"))
        self.target = MachineConfig("target", document.get("target"))
        self.mapping = MigrationMap(document, self.source, self.target)
        self.variables = VariableReport()
        self.source.validate(self.variables)
        self.target.validate(self.variables)
        self.shortcuts = document.get("shortcuts") or []
        if not isinstance(self.shortcuts, list):
            raise PlanError("shortcuts must be a list")

        self.updates: list[dict[str, Any]] = []
        self.unchanged: list[dict[str, Any]] = []
        self.retained: list[dict[str, Any]] = []
        self.conflicts: list[dict[str, Any]] = []
        self.unproven: list[dict[str, Any]] = []

    def run(self) -> dict[str, Any]:
        for index, shortcut in enumerate(self.shortcuts):
            self._plan_shortcut(shortcut, index)
        verification = self._verify()
        return {
            "status": self._status(verification),
            "read_only": True,
            "shortcuts_are_not_modified": True,
            "source": self.source.describe(),
            "target": self.target.describe(),
            "mapping": self.mapping.as_dict(),
            "variables": self.variables.as_dict(),
            "conflicts": self.conflicts,
            "plan": {
                "updates": self.updates,
                "unchanged": self.unchanged,
                "retained": self.retained,
                "changed_field_count": sum(len(item["edits"]) for item in self.updates),
                "changed_shortcut_count": len({item["shortcut"] for item in self.updates}),
            },
            "verification": verification,
        }

    def _plan_shortcut(self, shortcut: dict[str, Any], index: int) -> None:
        shortcut_id = str(shortcut.get("id") or shortcut.get("name") or f"shortcut-{index}")
        source_dir, target_dir = _shortcut_dirs(shortcut, self.source, self.target, self.mapping)
        source_resolver = Resolver(self.source, source_dir)
        target_resolver = Resolver(self.target, target_dir)

        claims: list[Claim] = []
        for declaration_index, raw_declaration in enumerate(_declarations(shortcut)):
            declaration = dict(raw_declaration)
            field = str(declaration.get("field") or "")
            if not field:
                raise PlanError(f"Declaration in {shortcut_id!r} has no field")
            value = declaration.get("value")
            if value is None:
                continue
            declaration_id = f"{shortcut_id}:{declaration_index}"
            kind = _kind_of(str(value), declaration.get("kind"))
            source_base, target_base = _bases(
                declaration, source_dir, target_dir, self.mapping, self.target
            )
            source_resolution = source_resolver.resolve(
                str(value),
                kind=kind,
                base=declaration.get("base"),
                base_path=source_base
                if declaration.get("base") not in (None, "shortcut_dir")
                else None,
                suffix=declaration.get("suffix"),
            )
            for fault in source_resolution.faults:
                if fault.code in ("missing_variable", "variable_cycle"):
                    self.variables.add(
                        self.source.name,
                        fault.code,
                        fault.chain,
                        fault.message,
                        f"{shortcut_id}:{field}",
                        True,
                    )

            material = (
                self.mapping.forward(source_resolution.path) if source_resolution.ok else None
            )
            natural_target = target_resolver.resolve(
                str(value),
                kind=kind,
                base=declaration.get("base"),
                base_path=target_base
                if declaration.get("base") not in (None, "shortcut_dir")
                else None,
                suffix=declaration.get("suffix"),
            )
            for fault in natural_target.faults:
                if fault.code in ("missing_variable", "variable_cycle"):
                    self.variables.add(
                        self.target.name,
                        fault.code,
                        fault.chain,
                        fault.message,
                        f"{shortcut_id}:{field}",
                        True,
                    )
            claims.append(
                Claim(
                    shortcut=shortcut_id,
                    declaration_id=declaration_id,
                    field=field,
                    declaration=declaration,
                    role=_role_of(declaration),
                    value=str(value),
                    kind=kind,
                    source=source_resolution,
                    natural_target=natural_target,
                    material=material,
                    source_base=source_base,
                    target_base=target_base,
                )
            )

        self._resolve_conflicts(shortcut_id, claims)
        conflicted = {id(claim) for claim in claims if getattr(claim, "conflict", None)}
        for claim in claims:
            if id(claim) in conflicted:
                self._retain(claim, claim.conflict)
            else:
                self._plan_claim(claim, shortcut)

    def _resolve_conflicts(self, shortcut_id: str, claims: list[Claim]) -> None:
        resolved = [
            claim
            for claim in claims
            if claim.source.ok and claim.material is not None and claim.material.reason is None
        ]

        # Several declarations may describe one slot (for example the target
        # path and LinkInfo base). If those views name different source
        # materials, the migration is contradictory and must be deferred.
        by_role: dict[str, list[Claim]] = {}
        for claim in resolved:
            by_role.setdefault(claim.role, []).append(claim)
        for role in sorted(by_role):
            group = by_role[role]
            if len(group) < 2:
                continue
            source_keys = {_path_key(claim.material.source_path) for claim in group}
            if len(source_keys) < 2:
                continue
            conflict = {
                "reason": "source_conflict",
                "message": "Declarations of one slot resolve to different source materials",
                "fields": [claim.field for claim in group],
                "source_paths": sorted({claim.material.source_path for claim in group}),
            }
            self.conflicts.append({"shortcut": shortcut_id, "group": "role:" + role, **conflict})
            for claim in group:
                claim.conflict = conflict["reason"]

        # Several declarations may name one source material. If their target
        # values or mapped paths disagree, the destination cannot be proven.
        by_source: dict[str, list[Claim]] = {}
        for claim in resolved:
            by_source.setdefault(_path_key(claim.material.source_path), []).append(claim)
        for source_key in sorted(by_source):
            group = by_source[source_key]
            if len(group) < 2 or any(claim.conflict for claim in group):
                continue
            conflict = _conflict(group)
            if conflict is None:
                continue
            self.conflicts.append({"shortcut": shortcut_id, "group": source_key, **conflict})
            for claim in group:
                claim.conflict = conflict["reason"]

    def _retain(self, claim: Claim, reason: str) -> None:
        entry = {
            "shortcut": claim.shortcut,
            "field": claim.field,
            "value": claim.value,
        }
        if claim.source.faults:
            entry["faults"] = [fault.as_dict() for fault in claim.source.faults]
        if reason == "mapping_ambiguous":
            entry["reason"] = "mapping_ambiguous"
        else:
            entry["reason"] = reason
        self.retained.append(entry)
        self.unproven.append(
            {
                "shortcut": claim.shortcut,
                "field": claim.field,
                "value": claim.value,
                "reason": reason,
            }
        )

    def _plan_claim(
        self,
        claim: Claim,
        shortcut: dict[str, Any],
    ) -> None:
        declaration = claim.declaration
        material = claim.material
        suffix_field = SUFFIX_FIELD_BY_FIELD.get(claim.field)
        suffix = declaration.get("suffix")

        if not claim.source.ok or material is None:
            self._retain(claim, "unresolved_source")
            return
        if material.reason:
            self._retain(claim, material.reason)
            return
        if _path_key(material.target_path) in self.mapping.ambiguous_targets:
            self._retain(claim, "mapping_ambiguous_target")
            return

        target_ok = claim.natural_target.ok
        same_target = target_ok and _path_key(claim.natural_target.path) == _path_key(
            material.target_path
        )
        if same_target:
            self._keep(claim, material, claim.natural_target.path, "already_resolved")
            return

        if not _allowed(self.document, shortcut, claim.field):
            self._retain(claim, "field_not_allowed")
            return

        chosen = _choose_candidate(
            material.target_path,
            declaration,
            claim.kind,
            self.target,
            claim.target_base,
        )
        if chosen is None:
            chosen = self._symbol_candidate(claim)
        if chosen is None:
            self._retain(claim, "no_target_expression")
            return

        if chosen["value"] == claim.value:
            self._keep(claim, material, chosen["resolved_to"], "already_resolved")
            return

        if suffix_field and suffix:
            if self._plan_base_only(claim, material, suffix):
                return
            if not _allowed(self.document, shortcut, suffix_field):
                self._retain(claim, "suffix_field_not_allowed")
                return

        self._emit_update(claim, chosen, material, suffix_field, suffix)

    def _plan_base_only(
        self,
        claim: Claim,
        material: Material,
        suffix: Any,
    ) -> bool:
        """Rewrite only the base when the existing suffix still yields the material.

        The pair (base, suffix) is one declaration. If some base plus the
        unchanged suffix resolves to the material, changing the base alone is
        the smallest correction. The base need not itself be a material; only
        the concatenation is checked.
        """

        if not _allowed(self.document, self._shortcut_by_id(claim.shortcut), claim.field):
            return False

        prefix = None
        if claim.source_base is not None:
            mapped_base = self.mapping.forward(claim.source_base)
            if mapped_base.migration is not None and _path_key(
                _join(mapped_base.target_path, str(suffix))
            ) == _path_key(material.target_path):
                prefix = mapped_base.target_path
        if prefix is None:
            prefix = self._base_for_suffix(material.target_path, str(suffix))
        if prefix is None or _path_key(prefix) == _path_key(claim.value):
            return False

        edits = [
            {
                "field": claim.field,
                "before": claim.value,
                "after": prefix,
            }
        ]
        self.updates.append(
            {
                "shortcut": claim.shortcut,
                "declaration": claim.declaration_id,
                "field": claim.field,
                "before": claim.value,
                "after": prefix,
                "kind": claim.kind,
                "strategy": "mapped base, suffix retained",
                "resolves_to": material.target_path,
                "material": material.key,
                "source_path": material.source_path,
                "migration": material.migration,
                "target_base": None,
                "clears_suffix": None,
                "keeps_suffix": True,
                "edits": edits,
            }
        )
        return True

    def _base_for_suffix(self, target: str, suffix: str) -> str | None:
        """Find an absolute base such that ``base + suffix`` is ``target``."""

        depth = len([part for part in _to_backslashes(suffix).split("\\") if part])
        prefix = _norm_path(target)
        for _ in range(depth + 1):
            prefix = ntpath.dirname(prefix)
            if not prefix or not _is_absolute(prefix):
                return None
            if _path_key(_join(prefix, suffix)) == _path_key(target):
                return prefix
        return None

    def _shortcut_by_id(self, shortcut_id: str) -> dict[str, Any]:
        for index, shortcut in enumerate(self.shortcuts):
            name = str(shortcut.get("id") or shortcut.get("name") or f"shortcut-{index}")
            if name == shortcut_id:
                return shortcut
        return {}

    def _symbol_candidate(self, claim: Claim) -> dict[str, Any] | None:
        """Keep a symbolic declaration when its target value is also its material.

        A declaration like %SystemRoot%\\system32\\x.exe names a machine-local
        tree. If the target machine's value for that symbol is not part of the
        material migration, the symbol itself is still the declaration, and
        changing it would alter a different, machine-owned material.
        """

        if claim.kind not in ("env", "known_folder"):
            return None
        if not claim.natural_target.ok:
            return None
        source_value = claim.natural_target.path
        reverse, ambiguity = self.mapping.reverse(source_value)
        if ambiguity is not None:
            return None
        if reverse != "path:" + _path_key(source_value):
            return None
        return {
            "value": claim.value,
            "kind": claim.kind,
            "strategy": "symbol retained",
            "resolved_to": source_value,
        }

    def _keep(
        self,
        claim: Claim,
        material: Material,
        resolved_to: str,
        reason: str,
    ) -> None:
        self.unchanged.append(
            {
                "shortcut": claim.shortcut,
                "declaration": claim.declaration_id,
                "field": claim.field,
                "value": claim.value,
                "suffix": str(claim.declaration.get("suffix") or ""),
                "resolves_to": resolved_to,
                "material": material.key,
                "source_path": claim.source.path,
                "kind": claim.kind,
                "reason": reason,
                "symbolic": claim.kind in ("env", "known_folder"),
                "symbol": _identity_key(
                    _identity(
                        claim.value,
                        claim.kind,
                        claim.source,
                        self.source,
                    )
                )
                if claim.kind in ("env", "known_folder")
                else None,
            }
        )

    def _emit_update(
        self,
        claim: Claim,
        chosen: dict[str, Any],
        material: Material,
        suffix_field: str | None,
        suffix: Any,
    ) -> None:
        base_value = chosen["value"]
        edits = [
            {
                "field": claim.field,
                "before": claim.value,
                "after": base_value,
            }
        ]
        if suffix_field and suffix:
            edits.append({"field": suffix_field, "before": str(suffix), "after": ""})

        self.updates.append(
            {
                "shortcut": claim.shortcut,
                "declaration": claim.declaration_id,
                "field": claim.field,
                "before": claim.value,
                "after": base_value,
                "kind": chosen["kind"],
                "strategy": chosen["strategy"],
                "resolves_to": chosen["resolved_to"],
                "material": material.key,
                "source_path": material.source_path,
                "migration": material.migration,
                "target_base": claim.target_base if chosen["kind"] == "relative" else None,
                "clears_suffix": suffix_field if suffix_field and suffix else None,
                "keeps_suffix": False,
                "edits": edits,
            }
        )

    def _verify(self) -> dict[str, Any]:
        confirmed: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        checked = 0

        update_index = {(item["shortcut"], item["declaration"]): item for item in self.updates}
        unchanged_index = {(item["shortcut"], item["declaration"]): item for item in self.unchanged}
        retained_ids = {(item["shortcut"], item["field"]) for item in self.retained}

        for index, shortcut in enumerate(self.shortcuts):
            shortcut_id = str(shortcut.get("id") or shortcut.get("name") or f"shortcut-{index}")
            source_dir, target_dir = _shortcut_dirs(
                shortcut, self.source, self.target, self.mapping
            )
            source_resolver = Resolver(self.source, source_dir)
            target_resolver = Resolver(self.target, target_dir)

            for declaration_index, declaration in enumerate(_declarations(shortcut)):
                field = str(declaration.get("field") or "")
                key = (shortcut_id, f"{shortcut_id}:{declaration_index}")
                update = update_index.get(key)
                unchanged = unchanged_index.get(key)
                if update is None and unchanged is None:
                    if (shortcut_id, field) not in retained_ids:
                        self.unproven.append(
                            {
                                "shortcut": shortcut_id,
                                "field": field,
                                "value": str(declaration.get("value")),
                                "reason": "not_planned",
                            }
                        )
                    continue

                if update is not None:
                    value = update["after"]
                    kind = update["kind"]
                    expected_target = update["resolves_to"]
                    expected_source = update["source_path"]
                    base_path = update["target_base"]
                    suffix = declaration.get("suffix") if update.get("keeps_suffix") else None
                else:
                    value = unchanged["value"]
                    kind = unchanged.get("kind") or declaration.get("kind")
                    expected_target = unchanged["resolves_to"]
                    expected_source = unchanged.get("source_path")
                    base_path = None
                    suffix = declaration.get("suffix")

                resolution = target_resolver.resolve(
                    value,
                    kind=kind,
                    base=declaration.get("base"),
                    base_path=base_path,
                    suffix=suffix,
                )
                checked += 1
                if not resolution.ok:
                    failed.append(
                        {
                            "shortcut": shortcut_id,
                            "field": field,
                            "value": value,
                            "reason": "target_unresolved",
                            "faults": [fault.as_dict() for fault in resolution.faults],
                        }
                    )
                    continue
                if _path_key(resolution.path) != _path_key(expected_target):
                    failed.append(
                        {
                            "shortcut": shortcut_id,
                            "field": field,
                            "value": value,
                            "reason": "target_mismatch",
                            "resolved_to": resolution.path,
                            "expected": expected_target,
                        }
                    )
                    continue

                if expected_source is None:
                    original = source_resolver.resolve(
                        str(declaration.get("value")),
                        kind=declaration.get("kind"),
                        base=declaration.get("base"),
                        suffix=declaration.get("suffix"),
                    )
                    expected_source = original.path if original.ok else None
                if unchanged is not None and unchanged.get("symbolic"):
                    target_symbol = _identity_key(_identity(value, kind, resolution, self.target))
                    same_symbol = target_symbol == unchanged.get("symbol")
                    entry = {
                        "shortcut": shortcut_id,
                        "field": field,
                        "value": value,
                        "resolves_to": resolution.path,
                        "maps_back_to": "symbol:" + target_symbol,
                        "material_preserved": same_symbol,
                    }
                    if same_symbol:
                        confirmed.append(entry)
                    else:
                        failed.append(
                            {
                                **entry,
                                "reason": "symbol_mismatch",
                                "expected_symbol": unchanged.get("symbol"),
                            }
                        )
                    continue

                mapped_back, ambiguity = self.mapping.reverse(resolution.path)
                material_ok = (
                    expected_source is not None
                    and ambiguity is None
                    and mapped_back == "path:" + _path_key(expected_source)
                )
                entry = {
                    "shortcut": shortcut_id,
                    "field": field,
                    "value": value,
                    "resolves_to": resolution.path,
                    "maps_back_to": mapped_back,
                    "material_preserved": material_ok,
                }
                if material_ok:
                    confirmed.append(entry)
                else:
                    failed.append(
                        {
                            **entry,
                            "reason": ambiguity or "material_mismatch",
                            "expected_source": expected_source,
                        }
                    )

        return {
            "checked": checked,
            "confirmed": confirmed,
            "failed": failed,
            "unproven": self.unproven,
        }

    def _status(self, verification: dict[str, Any]) -> str:
        if verification["failed"]:
            return "invalid"
        if (
            self.conflicts
            or self.retained
            or self.mapping.has_mapping_conflicts
            or self.mapping.diagnostics
            or self.variables.has_plan_affecting_faults
        ):
            return "needs_confirmation"
        return "ok"


def build_plan(document: dict[str, Any]) -> dict[str, Any]:
    """Build a read-only correction plan for a migration description."""

    return Planner(document).run()


def render_plan_text(plan: dict[str, Any]) -> str:
    """Render a compact human-readable summary of a plan."""

    lines = [f"Status: {plan['status']}"]
    updates = plan["plan"]["updates"]
    lines.append(f"Field corrections: {plan['plan']['changed_field_count']}")
    lines.extend(
        "  {shortcut} {field}: {before!r} -> {after!r} ({strategy})".format(**update)
        for update in updates
    )
    if plan["plan"]["retained"]:
        lines.append(f"Retained symbolic: {len(plan['plan']['retained'])}")
        lines.extend(
            f"  {item['shortcut']} {item['field']}: {item['value']!r} ({item['reason']})"
            for item in plan["plan"]["retained"]
        )
    if plan["conflicts"]:
        lines.append(f"Conflicts left for confirmation: {len(plan['conflicts'])}")
        lines.extend(
            f"  {item['shortcut']} {item.get('group', '')}: {item['reason']}"
            for item in plan["conflicts"]
        )
    lines.append(f"Missing variables: {len(plan['variables']['missing'])}")
    lines.extend(
        f"  [{item['machine']}] {item['message']}" for item in plan["variables"]["missing"]
    )
    lines.append(f"Variable cycles: {len(plan['variables']['cycles'])}")
    lines.extend(f"  [{item['machine']}] {item['message']}" for item in plan["variables"]["cycles"])
    lines.append(
        "Verification: {checked} checked, {confirmed} confirmed, {failed} failed, "
        "{unproven} unproven".format(
            checked=plan["verification"]["checked"],
            confirmed=len(plan["verification"]["confirmed"]),
            failed=len(plan["verification"]["failed"]),
            unproven=len(plan["verification"]["unproven"]),
        )
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plan minimal LNK field corrections for a material migration"
    )
    parser.add_argument("document", help="JSON migration description")
    parser.add_argument("--text", action="store_true", help="print a readable summary")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit with status 1 when confirmation or verification is pending",
    )
    args = parser.parse_args(argv)

    with open(args.document, "rb") as handle:
        document = json.load(handle)

    plan = build_plan(document)
    if args.text:
        print(render_plan_text(plan))
    else:
        print(json.dumps(plan, indent=2, ensure_ascii=False))

    if args.strict and plan["status"] != "ok":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
