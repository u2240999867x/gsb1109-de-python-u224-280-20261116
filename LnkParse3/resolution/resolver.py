"""Offline, evidence-driven resolution of a shell link target.

The resolver never opens the target, never lists a directory and never consults
the machine it is running on.  It combines:

* the target ID list,
* LinkInfo local and network fields (ANSI and Unicode independently),
* environment-variable paths,
* the relative path and working directory,

with a caller-supplied description of the machine (environment, volumes, share
mappings and a catalog of known files/directories).

A target is only confirmed when the resolution policy and catalog evidence
agree.  Everything else stays an explicit branch.  Contradictory fields are
reported as conflicts instead of being collapsed by read order.
"""

import copy
import json
from dataclasses import dataclass
from dataclasses import field

from LnkParse3.resolution.facts import extract_facts
from LnkParse3.resolution.facts import SourceField
from LnkParse3.resolution.paths import basename
from LnkParse3.resolution.paths import drive_root
from LnkParse3.resolution.paths import expand_environment
from LnkParse3.resolution.paths import is_absolute_path
from LnkParse3.resolution.paths import is_under
from LnkParse3.resolution.paths import join_path
from LnkParse3.resolution.paths import normalize_drive
from LnkParse3.resolution.paths import normalize_guid
from LnkParse3.resolution.paths import normalize_path
from LnkParse3.resolution.paths import path_key
from LnkParse3.resolution.paths import relative_to_root
from LnkParse3.resolution.paths import split_drive
from LnkParse3.resolution.paths import split_unc
from LnkParse3.resolution.paths import unc_key
from LnkParse3.resolution.profile import MachineProfile
from LnkParse3.resolution.profile import ResolutionPolicy


KNOWN_FOLDER_ENV = {
    # FOLDERID_Profile / user profile
    "5E6C858F-0E22-4760-9AFE-EA3317B67173": "%USERPROFILE%",
    # FOLDERID_RoamingAppData
    "3EB685DB-65F9-4CF6-A03A-E3EF65729F3D": "%APPDATA%",
    # FOLDERID_LocalAppData
    "F1B32785-6FBA-4FCF-9D55-7B8E7F157091": "%LOCALAPPDATA%",
    # FOLDERID_Desktop
    "B4BFCC3A-DB2C-424C-B029-7FE99A87C641": "%USERPROFILE%\\Desktop",
    # FOLDERID_Documents
    "FDD39AD0-238F-46AF-ADB4-6C85480369C7": "%USERPROFILE%\\Documents",
    # FOLDERID_Downloads
    "374DE290-123F-4565-9164-39C4925E467B": "%USERPROFILE%\\Downloads",
    # FOLDERID_Pictures
    "33E28130-4E1E-4676-835A-98395C3BC3BB": "%USERPROFILE%\\Pictures",
    # FOLDERID_Music
    "4BD8D571-6D19-48D3-BE97-422220080E43": "%USERPROFILE%\\Music",
    # FOLDERID_Videos
    "18989B1D-99B5-455B-841C-AB7C74E4DDFC": "%USERPROFILE%\\Videos",
    # FOLDERID_Public
    "DFDF76A2-C82A-4D63-906A-5644AC457385": "%PUBLIC%",
    # FOLDERID_ProgramData
    "62AB5D82-FDC1-4DC3-A9DD-070D1D495D97": "%PROGRAMDATA%",
}

ROOT_FOLDER_ENV = {
    "59031A47-3F72-44A7-89C5-5595FE6B30EE": "%USERPROFILE%",
    "450D8FBA-AD25-11D0-98A8-0800361B1103": "%USERPROFILE%\\Documents",
    "4336A54D-038B-4685-AB02-99BB52D3FB8B": "%PUBLIC%",
}

ROOT_FOLDER_BY_SORT_INDEX = {
    "Users": "%USERPROFILE%",
    "My Documents": "%USERPROFILE%\\Documents",
    "Public Folder": "%PUBLIC%",
}

NON_FILESYSTEM_CLASSES = {
    "Control panel",
    "Control panel category",
    "Control panel CPL file",
    "Internet",
    "Printers",
    "Common places folder",
    "Compressed folder",
}


def _source_field_dict(values):
    return [value.as_dict() if hasattr(value, "as_dict") else value for value in values]


def _normalize_serial(value):
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    text = text.removeprefix("0x")
    text = text.replace(" ", "")
    try:
        return int(text, 16)
    except ValueError:
        return text


def _normalize_drive_type(value):
    if value is None:
        return None
    text = str(value).strip().upper()
    if text.isdigit():
        return text
    return text


def _kind_from_item(item):
    flags = str(item.get("flags") or "").lower()
    if "directory" in flags or "share" in flags:
        return "directory"
    if "file" in flags:
        return "file"
    return None


def _kind_from_header(file_flags):
    flags = {str(flag).upper() for flag in file_flags or []}
    if "FILE_ATTRIBUTE_DIRECTORY" in flags:
        return "directory"
    return None


@dataclass
class Location:
    path: str
    source: str
    fields: list = field(default_factory=list)
    encoding: str = None
    note: str = None
    missing_environment: list = field(default_factory=list)
    absolute: bool = True

    def as_dict(self):
        result = {
            "path": self.path,
            "source": self.source,
            "fields": _source_field_dict(self.fields),
            "absolute": self.absolute,
        }
        if self.encoding:
            result["encoding"] = self.encoding
        if self.note:
            result["note"] = self.note
        if self.missing_environment:
            result["missing_environment"] = list(self.missing_environment)
        return result


@dataclass
class Candidate:
    generation: int
    source: str
    locations: list = field(default_factory=list)
    provenance: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    kind: str = None
    missing_environment: list = field(default_factory=list)
    catalog_matches: list = field(default_factory=list)
    volume_evidence: list = field(default_factory=list)
    share_evidence: list = field(default_factory=list)
    identity_keys: set = field(default_factory=set)
    status: str = "branch"
    reason: str = None
    candidate_id: str = None
    item_evidence: list = field(default_factory=list)
    contributing_sources: list = field(default_factory=list)

    @property
    def paths(self):
        result = []
        seen = set()
        for location in self.locations:
            if not location.path:
                continue
            key = path_key(location.path)
            if key in seen:
                continue
            seen.add(key)
            result.append(location.path)
        return result

    @property
    def path(self):
        if self.catalog_matches:
            for match in self.catalog_matches:
                if match.get("entry_path"):
                    return match["entry_path"]
        paths = self.paths
        if not paths:
            return None
        return paths[0]

    @property
    def sources(self):
        if self.contributing_sources:
            return list(self.contributing_sources)
        return sorted({location.source for location in self.locations})

    def add_location(self, location):
        if location.path is None:
            existing = [
                candidate_location
                for candidate_location in self.locations
                if candidate_location.path is None and candidate_location.source == location.source
            ]
            if existing:
                return
            self.locations.append(location)
            return

        key = (location.source, path_key(location.path))
        for existing in self.locations:
            if existing.path is None:
                continue
            if (existing.source, path_key(existing.path)) != key:
                continue
            for value in location.fields:
                if value.key() not in {item.key() for item in existing.fields}:
                    existing.fields.append(value)
                if value.key() not in {item.key() for item in self.provenance}:
                    self.provenance.append(value)
            if location.encoding and not existing.encoding:
                existing.encoding = location.encoding
            if location.note and location.note not in self.notes:
                self.notes.append(location.note)
            for name in location.missing_environment:
                if name not in self.missing_environment:
                    self.missing_environment.append(name)
            return

        self.locations.append(location)
        self.provenance.extend(location.fields)
        for name in location.missing_environment:
            if name not in self.missing_environment:
                self.missing_environment.append(name)

    def merge(self, other):
        for location in other.locations:
            self.add_location(location)
        for note in other.notes:
            if note not in self.notes:
                self.notes.append(note)
        for item in other.item_evidence:
            if item not in self.item_evidence:
                self.item_evidence.append(item)
        for value in other.provenance:
            if value.key() not in {item.key() for item in self.provenance}:
                self.provenance.append(value)
        for name in other.missing_environment:
            if name not in self.missing_environment:
                self.missing_environment.append(name)
        if not self.kind and other.kind:
            self.kind = other.kind
        if self.kind and other.kind and self.kind != other.kind:
            note = f"candidate kind conflict: {self.kind!r} vs {other.kind!r}"
            if note not in self.notes:
                self.notes.append(note)

    def as_dict(self):
        result = {
            "id": self.candidate_id,
            "status": self.status,
            "path": self.path,
            "paths": self.paths,
            "sources": self.sources,
            "kind": self.kind,
            "provenance": [value.as_dict() for value in self.provenance],
            "locations": [location.as_dict() for location in self.locations],
        }
        if self.reason:
            result["reason"] = self.reason
        if self.notes:
            result["notes"] = list(self.notes)
        if self.missing_environment:
            result["missing_environment"] = list(self.missing_environment)
        if self.catalog_matches:
            result["catalog_evidence"] = list(self.catalog_matches)
        if self.volume_evidence:
            result["volume_evidence"] = list(self.volume_evidence)
        if self.share_evidence:
            result["share_evidence"] = list(self.share_evidence)
        if self.item_evidence:
            result["id_list_items"] = list(self.item_evidence)
        return result


@dataclass
class Conflict:
    type: str
    message: str
    sources: list = field(default_factory=list)
    values: list = field(default_factory=list)
    fields: list = field(default_factory=list)
    candidates: list = field(default_factory=list)

    def as_dict(self):
        result = {
            "type": self.type,
            "message": self.message,
            "sources": list(self.sources),
            "values": list(self.values),
            "fields": _source_field_dict(self.fields),
        }
        if self.candidates:
            result["candidates"] = list(self.candidates)
        return result


class CatalogIndex:
    def __init__(self, entries):
        self.entries = list(entries)
        self.by_key = {}
        self.catalog_conflicts = []
        for entry in self.entries:
            keys = []
            if entry.path:
                keys.append(entry.path)
            keys.extend(entry.aliases)
            for key in keys:
                normalized = path_key(key)
                if not normalized:
                    continue
                self.by_key.setdefault(normalized, []).append(
                    {"entry": entry, "matched_key": key, "alias": key != entry.path}
                )
            unc = unc_key(entry.path) if entry.path else None
            if unc:
                self.by_key.setdefault("unc:" + unc, []).append(
                    {"entry": entry, "matched_key": entry.path, "alias": False}
                )

        self._detect_catalog_conflicts()

    def _detect_catalog_conflicts(self):
        seen = {}
        for entry in self.entries:
            for key in [entry.path, *list(entry.aliases)]:
                if not key:
                    continue
                normalized = path_key(key)
                object_key = entry.object_id or path_key(entry.path)
                previous = seen.get(normalized)
                if previous and previous != object_key:
                    self.catalog_conflicts.append(
                        Conflict(
                            type="catalog_conflict",
                            message=(
                                f"catalog maps {key!r} to both {previous!r} and {object_key!r}"
                            ),
                            values=[key, previous, object_key],
                        )
                    )
                seen[normalized] = object_key

    def lookup(self, path):
        if not path:
            return []
        return list(self.by_key.get(path_key(path), []))

    def lookup_unc(self, path):
        key = unc_key(path)
        if not key:
            return []
        return list(self.by_key.get("unc:" + key, []))


def _catalog_object_key(entry):
    return "obj:" + str(entry.object_id or path_key(entry.path))


class _UnionFind:
    def __init__(self):
        self.parent = {}

    def add(self, value):
        self.parent.setdefault(value, value)

    def find(self, value):
        self.add(value)
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left, right):
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


class Resolver:
    """Resolve facts against one machine profile.

    The resolver is deliberately stateless; a new profile means a new
    resolution.  There is no cross-profile result cache to accidentally reuse.
    """

    def __init__(self, profile=None):
        if isinstance(profile, MachineProfile):
            self.profile = profile
        else:
            self.profile = MachineProfile.from_dict(profile or {})
        self.policy = self.profile.policy
        self._candidates = []
        self._generation = 0
        self._conflicts = []
        self._catalog = CatalogIndex(self.profile.catalog)
        self._conflicts.extend(self._catalog.catalog_conflicts)
        self._missing_environment = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def resolve(self, lnk):
        facts = extract_facts(lnk)
        return self.resolve_facts(facts)

    def resolve_facts(self, facts):
        self._facts = facts
        self._candidates = []
        self._generation = 0
        self._conflicts = list(self._catalog.catalog_conflicts)
        self._missing_environment = []

        self._record_internal_substructure_conflicts()
        self._generate_id_list_candidates()
        self._generate_link_info_candidates()
        self._generate_environment_candidates()
        self._generate_relative_path_candidates()
        self._record_missing_environment()

        self._expand_volume_and_share_locations()
        self._match_catalog()
        self._attach_volume_evidence()
        merged = self._merge_candidates()
        self._assign_ids(merged)
        self._remap_conflict_candidates(merged)
        self._record_cross_candidate_conflicts(merged)
        self._record_kind_conflicts(merged)
        status, selected = self._select(merged)

        return ResolutionResult(
            candidates=merged,
            conflicts=self._deduplicate_conflicts(self._conflicts),
            policy=self.policy,
            profile_fingerprint=self.profile.fingerprint(),
            facts=facts,
            missing_environment=sorted(self._missing_environment),
            catalog_conflicts=self._catalog.catalog_conflicts,
            status=status,
            selected=selected,
        )

    # ------------------------------------------------------------------
    # Generation helpers
    # ------------------------------------------------------------------
    def _next_generation(self):
        self._generation += 1
        return self._generation

    def _source_allowed(self, source):
        return self.policy.is_allowed(source)

    def _expand(self, value):
        if not self.policy.allow_environment_expansion:
            return normalize_path(value), [], []
        return expand_environment(value, self.profile.environment)

    def _new_candidate(self, source, locations, kind=None, notes=None, item_evidence=None):
        candidate = Candidate(
            generation=self._next_generation(),
            source=source,
            kind=kind,
            notes=list(notes or []),
            item_evidence=list(item_evidence or []),
        )
        for location in locations:
            candidate.add_location(location)
        self._candidates.append(candidate)
        return candidate

    def _record_internal_substructure_conflicts(self):
        facts = self._facts

        if len(facts.local_variants) > 1:
            paths = []
            for variant in facts.local_variants:
                path = join_path(variant.base.value, variant.suffix.value)
                paths.append((variant.encoding, path, [variant.base, variant.suffix]))
            if len({path_key(path) for _, path, _ in paths}) > 1:
                self._conflicts.append(
                    Conflict(
                        type="link_info_encoding_conflict",
                        message=(
                            "LinkInfo LocalBasePath/CommonPathSuffix disagree "
                            "between ANSI and Unicode fields"
                        ),
                        sources=["link_info_local"],
                        values=[
                            {"encoding": encoding, "path": path} for encoding, path, _ in paths
                        ],
                        fields=[field for _, _, fields in paths for field in fields],
                    )
                )

        if len(facts.network_variants) > 1:
            paths = []
            for variant in facts.network_variants:
                path = join_path(variant.net_name.value, variant.suffix.value)
                fields = [variant.net_name]
                if variant.suffix:
                    fields.append(variant.suffix)
                paths.append((variant.encoding, path, fields))
            if len({path_key(path) for _, path, _ in paths}) > 1:
                self._conflicts.append(
                    Conflict(
                        type="link_info_encoding_conflict",
                        message=(
                            "CommonNetworkRelativeLink NetName/suffix disagree "
                            "between ANSI and Unicode fields"
                        ),
                        sources=["link_info_network"],
                        values=[
                            {"encoding": encoding, "path": path} for encoding, path, _ in paths
                        ],
                        fields=[field for _, _, fields in paths for field in fields],
                    )
                )

        if len(facts.environment_variants) > 1:
            expanded = []
            for variant in facts.environment_variants:
                path, _, missing = self._expand(variant.value)
                expanded.append((variant.encoding, path, missing, variant))
            if len({path_key(path) for _, path, _, _ in expanded}) > 1:
                self._conflicts.append(
                    Conflict(
                        type="environment_encoding_conflict",
                        message=(
                            "EnvironmentVariableDataBlock TargetAnsi and TargetUnicode disagree"
                        ),
                        sources=["environment"],
                        values=[
                            {
                                "encoding": encoding,
                                "path": path,
                                "missing_environment": missing,
                            }
                            for encoding, path, missing, _ in expanded
                        ],
                        fields=[variant for _, _, _, variant in expanded],
                    )
                )

    def _generate_id_list_candidates(self):
        if not self._facts.id_items:
            return
        if not self._source_allowed("idlist"):
            self._conflicts.append(
                Conflict(
                    type="source_disabled",
                    message="target ID list is present but disabled by policy",
                    sources=["idlist"],
                    values=[item.item_class for item in self._facts.id_items],
                )
            )
            return

        base = None
        base_fields = []
        base_missing = []
        volume_guid = None
        volume_name = None
        names = []
        name_fields = []
        kinds = []
        unsupported = []
        item_evidence = []

        for item in self._facts.id_items:
            item_class = item.item_class
            item_evidence.append({"index": item.index, "class": item_class})

            if item_class == "Root Folder":
                base, fields, missing = self._root_folder_base(item)
                if base:
                    base_fields = fields
                    base_missing = missing
                continue

            if item_class == "Volume Item":
                if item.get("volume_name"):
                    volume_name = item.get("volume_name")
                    base = drive_root(volume_name)
                    base_fields = [
                        SourceField(
                            source="idlist",
                            field="volume_name",
                            value=volume_name,
                            json_path=f"target.items[{item.index}].volume_name",
                            item_index=item.index,
                            item_class=item_class,
                        )
                    ]
                elif item.get("volume_identifier"):
                    volume_guid = normalize_guid(item.get("volume_identifier"))
                    known_template = KNOWN_FOLDER_ENV.get(volume_guid)
                    if known_template:
                        base, _, known_missing = self._expand(known_template)
                        for name in known_missing:
                            if name not in self._missing_environment:
                                self._missing_environment.append(name)
                        base_missing = known_missing
                    else:
                        mapped_drive = self._drive_for_volume_guid(volume_guid)
                        base = drive_root(mapped_drive) if mapped_drive else None
                    base_fields = [
                        SourceField(
                            source="idlist",
                            field="volume_identifier",
                            value=item.get("volume_identifier"),
                            json_path=f"target.items[{item.index}].volume_identifier",
                            item_index=item.index,
                            item_class=item_class,
                        )
                    ]
                continue

            if item_class == "Users files folder":
                nested = item.get("file_entry") or {}
                name = nested.get("primary_name")
                if name:
                    names.append(name)
                    name_fields.append(
                        SourceField(
                            source="idlist",
                            field="file_entry.primary_name",
                            value=name,
                            json_path=f"target.items[{item.index}].file_entry.primary_name",
                            item_index=item.index,
                            item_class=item_class,
                        )
                    )
                    kinds.append(_kind_from_item(nested))
                continue

            if item_class == "File entry":
                name = item.get("primary_name")
                if name:
                    names.append(name)
                    name_fields.append(
                        SourceField(
                            source="idlist",
                            field="primary_name",
                            value=name,
                            json_path=f"target.items[{item.index}].primary_name",
                            item_index=item.index,
                            item_class=item_class,
                        )
                    )
                    kinds.append(_kind_from_item(item))
                continue

            if item_class in NON_FILESYSTEM_CLASSES:
                unsupported.append(item_class)

        if unsupported and not names:
            return

        if base is None and names:
            # The target chain starts at a root that only the machine can
            # name (for example a volume GUID that is not in the profile).
            marker = None
            if volume_guid:
                marker = "\\\\?\\Volume{" + volume_guid + "}\\"
            location = Location(
                path=join_path(marker, "\\".join(names)) if marker else None,
                source="idlist",
                fields=base_fields + name_fields,
                encoding=None,
                note="target ID list root could not be mapped to a drive",
                missing_environment=base_missing,
                absolute=False,
            )
            kind = next((value for value in reversed(kinds) if value), None)
            self._new_candidate(
                "idlist",
                [location],
                kind=kind,
                notes=(location.note and [location.note]) or [],
                item_evidence=item_evidence,
            )
            return

        if base is None:
            return

        path = base
        for name in names:
            path = join_path(path, name)
        location = Location(
            path=path,
            source="idlist",
            fields=base_fields + name_fields,
            note="constructed from target ID list",
            missing_environment=base_missing,
            absolute=is_absolute_path(path),
        )
        kind = next((value for value in reversed(kinds) if value), None)
        self._new_candidate(
            "idlist",
            [location],
            kind=kind,
            item_evidence=item_evidence,
        )

    def _root_folder_base(self, item):
        guid = normalize_guid(item.get("guid"))
        fields = []
        if item.get("guid"):
            fields.append(
                SourceField(
                    source="idlist",
                    field="guid",
                    value=item.get("guid"),
                    json_path=f"target.items[{item.index}].guid",
                    item_index=item.index,
                    item_class=item.item_class,
                )
            )
        if item.get("sort_index") is not None:
            fields.append(
                SourceField(
                    source="idlist",
                    field="sort_index",
                    value=item.get("sort_index"),
                    json_path=f"target.items[{item.index}].sort_index",
                    item_index=item.index,
                    item_class=item.item_class,
                )
            )

        template = None
        if guid in ROOT_FOLDER_ENV:
            template = ROOT_FOLDER_ENV[guid]
        elif item.get("sort_index") in ROOT_FOLDER_BY_SORT_INDEX:
            template = ROOT_FOLDER_BY_SORT_INDEX[item.get("sort_index")]
        if template is None:
            return None, fields, []

        expanded, _, missing = self._expand(template)
        for name in missing:
            if name not in self._missing_environment:
                self._missing_environment.append(name)
        return expanded, fields, missing

    def _drive_for_volume_guid(self, guid):
        if not guid:
            return None
        for volume in self.profile.volumes:
            if normalize_guid(volume.guid) == guid:
                return normalize_drive(volume.drive)
        return None

    def _generate_link_info_candidates(self):
        facts = self._facts
        if not facts.has_link_info:
            return
        if self.policy.honor_link_flags and facts.force_no_link_info:
            self._conflicts.append(
                Conflict(
                    type="source_disabled",
                    message="LinkInfo is present but ForceNoLinkInfo is set",
                    sources=["link_info_local", "link_info_network"],
                )
            )
            return

        for variant in facts.local_variants:
            if not self._source_allowed("link_info_local"):
                self._conflicts.append(
                    Conflict(
                        type="source_disabled",
                        message="LinkInfo local path is present but disabled by policy",
                        sources=["link_info_local"],
                        values=[variant.base.value],
                        fields=[variant.base],
                    )
                )
                break
            raw_base = variant.base.value
            raw_suffix = variant.suffix.value
            base, _, base_missing = self._expand(raw_base)
            suffix, _, suffix_missing = self._expand(raw_suffix)
            path = join_path(base, suffix) if suffix else normalize_path(base)
            missing = base_missing + suffix_missing
            for name in missing:
                if name not in self._missing_environment:
                    self._missing_environment.append(name)
            location = Location(
                path=path,
                source="link_info_local",
                fields=[variant.base, variant.suffix],
                encoding=variant.encoding,
                missing_environment=missing,
                absolute=is_absolute_path(path),
            )
            kind = _kind_from_header(facts.file_flags)
            self._new_candidate("link_info_local", [location], kind=kind)

        for variant in facts.network_variants:
            if not self._source_allowed("link_info_network"):
                self._conflicts.append(
                    Conflict(
                        type="source_disabled",
                        message="LinkInfo network path is present but disabled by policy",
                        sources=["link_info_network"],
                        values=[variant.net_name.value],
                        fields=[variant.net_name],
                    )
                )
                break
            net_name, _, net_missing = self._expand(variant.net_name.value)
            suffix, _, suffix_missing = self._expand(variant.suffix.value if variant.suffix else "")
            path = join_path(net_name, suffix) if suffix else normalize_path(net_name)
            missing = net_missing + suffix_missing
            for name in missing:
                if name not in self._missing_environment:
                    self._missing_environment.append(name)

            locations = [
                Location(
                    path=path,
                    source="link_info_network",
                    fields=[variant.net_name] + ([variant.suffix] if variant.suffix else []),
                    encoding=variant.encoding,
                    missing_environment=missing,
                    absolute=is_absolute_path(path),
                )
            ]

            if variant.device_name and variant.device_name.value:
                device_path, _, device_missing = self._expand(variant.device_name.value)
                device_root = drive_root(device_path)
                if device_root:
                    locations.append(
                        Location(
                            path=join_path(device_root, suffix) if suffix else device_root,
                            source="link_info_network",
                            fields=[variant.device_name]
                            + ([variant.suffix] if variant.suffix else []),
                            encoding=variant.encoding,
                            note="drive-letter location from CommonNetworkRelativeLink DeviceName",
                            missing_environment=device_missing,
                            absolute=True,
                        )
                    )
                    for name in device_missing:
                        if name not in self._missing_environment:
                            self._missing_environment.append(name)

            kind = _kind_from_header(facts.file_flags)
            self._new_candidate("link_info_network", locations, kind=kind)

    def _generate_environment_candidates(self):
        facts = self._facts
        if not facts.environment_variants:
            return
        if not self._source_allowed("environment"):
            self._conflicts.append(
                Conflict(
                    type="source_disabled",
                    message="EnvironmentVariableDataBlock is present but disabled by policy",
                    sources=["environment"],
                )
            )
            return
        if self.policy.honor_link_flags and facts.has_flag("DisableLinkPathTracking"):
            self._conflicts.append(
                Conflict(
                    type="source_disabled",
                    message=(
                        "EnvironmentVariableDataBlock is ignored because "
                        "DisableLinkPathTracking is set"
                    ),
                    sources=["environment"],
                )
            )
            return

        for variant in facts.environment_variants:
            path, _, missing = self._expand(variant.value)
            for name in missing:
                if name not in self._missing_environment:
                    self._missing_environment.append(name)
            note = "expanded from EnvironmentVariableDataBlock"
            if missing:
                note = "environment path has unresolved variables"
            location = Location(
                path=path,
                source="environment",
                fields=[variant],
                encoding=variant.encoding,
                note=note,
                missing_environment=missing,
                absolute=is_absolute_path(path),
            )
            kind = _kind_from_header(facts.file_flags)
            self._new_candidate("environment", [location], kind=kind)

    def _generate_relative_path_candidates(self):
        facts = self._facts
        relative = facts.relative_path
        if not relative:
            return
        if not self._source_allowed("relative_path"):
            self._conflicts.append(
                Conflict(
                    type="source_disabled",
                    message="relative path is present but disabled by policy",
                    sources=["relative_path"],
                    values=[relative.value],
                )
            )
            return

        rel, _, rel_missing = self._expand(relative.value)
        working = facts.working_directory
        locations = []

        if is_absolute_path(rel):
            locations.append(
                Location(
                    path=rel,
                    source="relative_path",
                    fields=[relative],
                    missing_environment=rel_missing,
                    note="relative path field is already absolute",
                )
            )
        elif working and working.value:
            work, _, work_missing = self._expand(working.value)
            missing = rel_missing + work_missing
            for name in missing:
                if name not in self._missing_environment:
                    self._missing_environment.append(name)
            if is_absolute_path(work):
                path = join_path(work, rel)
                locations.append(
                    Location(
                        path=path,
                        source="relative_path",
                        fields=[relative, working],
                        missing_environment=missing,
                        note="joined to the stored working directory",
                    )
                )
            else:
                path = join_path(work, rel)
                locations.append(
                    Location(
                        path=path,
                        source="relative_path",
                        fields=[relative, working],
                        missing_environment=missing,
                        note="working directory is not absolute on this profile",
                        absolute=False,
                    )
                )
        else:
            # RELATIVE_PATH is relative to the directory from which the link
            # was created (normally the working directory).  Without that
            # directory there is no machine-independent anchor, so keep the
            # raw relative path as an unresolved branch instead of borrowing
            # a location from another field.
            locations.append(
                Location(
                    path=normalize_path(rel),
                    source="relative_path",
                    fields=[relative],
                    missing_environment=rel_missing,
                    note=("no working directory is available; relative path remains a branch"),
                    absolute=False,
                )
            )

        kind = _kind_from_header(facts.file_flags)
        self._new_candidate("relative_path", locations, kind=kind)

    def _record_missing_environment(self):
        for name in self._missing_environment:
            self._conflicts.append(
                Conflict(
                    type="missing_environment",
                    message=(
                        f"environment variable %{name}% is not defined in the "
                        "machine profile; candidates using it stay unresolved"
                    ),
                    sources=["environment"],
                    values=[name],
                )
            )

    # ------------------------------------------------------------------
    # Machine equivalence
    # ------------------------------------------------------------------
    def _expand_volume_and_share_locations(self):
        for candidate in self._candidates:
            original = list(candidate.locations)
            for location in original:
                if not location.path:
                    continue
                for alt_path, note, evidence, fields in self._share_alternatives(location.path):
                    if path_key(alt_path) == path_key(location.path):
                        continue
                    candidate.add_location(
                        Location(
                            path=alt_path,
                            source=location.source,
                            fields=list(location.fields) + fields,
                            encoding=location.encoding,
                            note=note,
                            absolute=True,
                        )
                    )
                    candidate.share_evidence.append(evidence)

                for alt_path, note, evidence in self._volume_alternatives(location):
                    if path_key(alt_path) == path_key(location.path):
                        continue
                    candidate.add_location(
                        Location(
                            path=alt_path,
                            source=location.source,
                            fields=list(location.fields),
                            encoding=location.encoding,
                            note=note,
                            absolute=True,
                        )
                    )
                    candidate.volume_evidence.append(evidence)

    def _share_alternatives(self, path):
        alternatives = []
        drive, tail = split_drive(path)
        if drive:
            for share in self.profile.shares:
                drives = [normalize_drive(item) for item in share.drives]
                if drive in drives and share.unc:
                    unc_root, unc_tail = split_unc(share.unc)
                    if unc_root:
                        base = unc_root + ("\\" + unc_tail if unc_tail else "")
                        alternatives.append(
                            (
                                join_path(base, tail),
                                f"share mapping {drive} -> {share.unc}",
                                {
                                    "type": "share_mapping",
                                    "drive": drive,
                                    "unc": share.unc,
                                    "direction": "drive_to_unc",
                                },
                                [],
                            )
                        )
                for alias in share.aliases:
                    alias_drive = normalize_drive(alias)
                    if alias_drive == drive:
                        unc_root, unc_tail = split_unc(share.unc)
                        if unc_root:
                            base = unc_root + ("\\" + unc_tail if unc_tail else "")
                            alternatives.append(
                                (
                                    join_path(base, tail),
                                    f"share alias {alias} -> {share.unc}",
                                    {
                                        "type": "share_mapping",
                                        "drive": drive,
                                        "unc": share.unc,
                                        "direction": "alias_to_unc",
                                    },
                                    [],
                                )
                            )
            return alternatives

        unc_root, unc_tail = split_unc(path)
        if not unc_root:
            return alternatives
        for share in self.profile.shares:
            share_root, share_tail = split_unc(share.unc)
            if not share_root or path_key(share_root) != path_key(unc_root):
                continue
            if share_tail and path_key(share_tail) != path_key(unc_tail or ""):
                # The mapping points at a subdirectory of the share.
                if not is_under(path, share.unc):
                    continue
                relative = relative_to_root(path, share.unc) or ""
            else:
                relative = unc_tail or ""
            for drive in share.drives:
                normalized_drive = normalize_drive(drive)
                if not normalized_drive:
                    continue
                alternatives.append(
                    (
                        join_path(drive_root(normalized_drive), relative),
                        f"share mapping {share.unc} -> {normalized_drive}",
                        {
                            "type": "share_mapping",
                            "drive": normalized_drive,
                            "unc": share.unc,
                            "direction": "unc_to_drive",
                        },
                        [],
                    )
                )
        return alternatives

    def _stored_volume_identity(self):
        identity = {}
        for key, field_value in (self._facts.volume_identity or {}).items():
            if field_value is None:
                continue
            value = getattr(field_value, "value", None)
            if value in (None, ""):
                continue
            identity[key] = value
        return identity

    def _volume_alternatives(self, location):
        alternatives = []
        path = location.path
        drive, tail = split_drive(path)
        if not drive:
            return alternatives
        stored = self._stored_volume_identity()
        if not stored:
            return alternatives

        current = self._volume_by_drive(drive)
        if current is not None and self._volume_matches(current, stored):
            return alternatives

        matches = self._volumes_matching_identity(stored)
        for volume in matches:
            target_drive = normalize_drive(volume.drive)
            if not target_drive or target_drive == drive:
                continue
            alternatives.append(
                (
                    join_path(drive_root(target_drive), tail),
                    (
                        "drive remapped by volume identity "
                        f"({self._volume_identity_summary(stored)})"
                    ),
                    {
                        "type": "volume_identity_remap",
                        "from_drive": drive,
                        "to_drive": target_drive,
                        "stored": stored,
                        "volume": volume.as_dict(),
                    },
                )
            )
        return alternatives

    def _volume_by_drive(self, drive):
        drive = normalize_drive(drive)
        for volume in self.profile.volumes:
            if normalize_drive(volume.drive) == drive:
                return volume
        return None

    @staticmethod
    def _volume_identity_summary(identity):
        parts = [
            f"{key}={identity[key]}"
            for key in ("volume_label_unicode", "volume_label", "drive_serial_number")
            if identity.get(key)
        ]
        return ", ".join(parts) if parts else "volume identity"

    def _volume_matches(self, volume, stored):
        checks = []
        if stored.get("volume_label_unicode") and volume.label:
            checks.append(
                str(stored["volume_label_unicode"]).casefold() == str(volume.label).casefold()
            )
        elif stored.get("volume_label") and volume.label:
            checks.append(str(stored["volume_label"]).casefold() == str(volume.label).casefold())
        if stored.get("drive_serial_number") and volume.serial_number:
            checks.append(
                _normalize_serial(stored["drive_serial_number"])
                == _normalize_serial(volume.serial_number)
            )
        if stored.get("drive_type") and volume.drive_type:
            checks.append(
                _normalize_drive_type(stored["drive_type"])
                == _normalize_drive_type(volume.drive_type)
            )
        if not checks:
            return True
        return all(checks)

    def _volumes_matching_identity(self, stored):
        serial = _normalize_serial(stored.get("drive_serial_number"))
        label = stored.get("volume_label_unicode") or stored.get("volume_label")
        matches = []
        for volume in self.profile.volumes:
            if (
                serial
                and volume.serial_number
                and serial == _normalize_serial(volume.serial_number)
            ):
                matches.append(volume)
                continue
            if label and volume.label and str(label).casefold() == str(volume.label).casefold():
                matches.append(volume)
        return matches

    def _canonical_unc(self, path):
        drive, tail = split_drive(path)
        if drive:
            for share in self.profile.shares:
                if drive in [normalize_drive(item) for item in share.drives] and share.unc:
                    unc_root, unc_tail = split_unc(share.unc)
                    if unc_root:
                        base = unc_root + ("\\" + unc_tail if unc_tail else "")
                        return unc_key(join_path(base, tail))
        unc = unc_key(path)
        return unc

    # ------------------------------------------------------------------
    # Catalog evidence
    # ------------------------------------------------------------------
    def _match_catalog(self):
        for candidate in self._candidates:
            matches = []
            matched_objects = {}
            for location in candidate.locations:
                if not location.path:
                    continue
                lookups = self._catalog.lookup(location.path)
                if not lookups:
                    unc = self._canonical_unc(location.path)
                    if unc:
                        lookups = self._catalog.lookup_unc(unc)
                for lookup in lookups:
                    entry = lookup["entry"]
                    object_key = _catalog_object_key(entry)
                    matched_objects.setdefault(object_key, []).append(entry)
                    matches.append(
                        {
                            "entry_path": entry.path,
                            "matched_key": lookup["matched_key"],
                            "alias": lookup["alias"],
                            "candidate_path": location.path,
                            "source": location.source,
                            "kind": entry.kind,
                            "object_id": entry.object_id,
                        }
                    )
            if not matches:
                continue

            # A catalog hit only counts as confirmation when the filename
            # evidence agrees.  A directory-only hit proves the parent exists,
            # not that the LNK's final component is that object.
            filenames = {path_key(basename(path)) for path in candidate.paths if path}
            matched_names = {
                path_key(basename(match["entry_path"]))
                for match in matches
                if match.get("entry_path")
            }
            if filenames and matched_names and not (filenames & matched_names):
                continue

            # A candidate that matches two distinct catalog objects is itself
            # contradictory.  Keep the evidence instead of letting the last
            # lookup win.
            distinct = {
                str(entry.object_id or path_key(entry.path))
                for entry in self._catalog_entries(matches)
            }
            if len(distinct) > 1:
                self._conflicts.append(
                    Conflict(
                        type="catalog_object_conflict",
                        message="candidate path matches multiple distinct catalog objects",
                        sources=candidate.sources,
                        values=matches,
                        candidates=[candidate.generation],
                    )
                )
            candidate.catalog_matches = self._deduplicate_matches(matches)

    @staticmethod
    def _catalog_entries(matches):
        result = []
        seen = set()
        for match in matches:
            key = (match.get("entry_path"), match.get("object_id"))
            if key in seen:
                continue
            seen.add(key)
            result.append(
                type(
                    "EntryView",
                    (),
                    {
                        "path": match.get("entry_path"),
                        "object_id": match.get("object_id"),
                    },
                )()
            )
        return result

    @staticmethod
    def _deduplicate_matches(matches):
        result = []
        seen = set()
        for match in matches:
            key = (
                match.get("entry_path"),
                match.get("object_id"),
                match.get("matched_key"),
                match.get("candidate_path"),
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(match)
        return result

    def _attach_volume_evidence(self):
        for candidate in self._candidates:
            for location in candidate.locations:
                if not location.path:
                    continue
                drive, _ = split_drive(location.path)
                if not drive:
                    continue
                matches = [
                    self._compare_volume(volume, candidate)
                    for volume in self.profile.volumes
                    if normalize_drive(volume.drive) == drive
                ]
                if matches:
                    candidate.volume_evidence.extend(matches)
            candidate.volume_evidence = self._deduplicate_evidence(candidate.volume_evidence)

    def _compare_volume(self, volume, candidate=None):
        stored = self._stored_volume_identity()
        evidence = {
            "type": "volume",
            "drive": normalize_drive(volume.drive),
            "profile": volume.as_dict(),
            "stored": stored,
            "supports": True,
            "contradictions": [],
        }
        if not stored:
            evidence["supports"] = True
            return evidence

        stored_label = stored.get("volume_label_unicode") or stored.get("volume_label")
        if (
            stored_label
            and volume.label
            and str(stored_label).casefold() != str(volume.label).casefold()
        ):
            evidence["contradictions"].append("volume_label")

        if (
            stored.get("drive_serial_number")
            and volume.serial_number
            and _normalize_serial(stored["drive_serial_number"])
            != _normalize_serial(volume.serial_number)
        ):
            evidence["contradictions"].append("drive_serial_number")

        if (
            stored.get("drive_type")
            and volume.drive_type
            and _normalize_drive_type(stored["drive_type"])
            != _normalize_drive_type(volume.drive_type)
        ):
            evidence["contradictions"].append("drive_type")

        if evidence["contradictions"]:
            evidence["supports"] = False
            self._conflicts.append(
                Conflict(
                    type="volume_identity_mismatch",
                    message=(
                        f"LinkInfo volume identity for {normalize_drive(volume.drive)} "
                        f"disagrees with the machine profile: "
                        f"{', '.join(evidence['contradictions'])}"
                    ),
                    sources=["link_info_local"],
                    values=[
                        {"stored": stored, "profile": volume.as_dict()},
                        evidence["contradictions"],
                    ],
                    fields=[
                        value
                        for key, value in (self._facts.volume_identity or {}).items()
                        if key in evidence["contradictions"]
                    ],
                    candidates=[candidate.generation] if candidate is not None else [],
                )
            )
        return evidence

    @staticmethod
    def _deduplicate_evidence(values):
        result = []
        seen = set()
        for value in values:
            key = (
                value.get("type"),
                value.get("drive"),
                tuple(value.get("contradictions") or []),
                str(value.get("stored")),
                str(value.get("volume")),
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(value)
        return result

    # ------------------------------------------------------------------
    # Merging and selection
    # ------------------------------------------------------------------
    def _candidate_keys(self, candidate):
        keys = set()
        for location in candidate.locations:
            if not location.path:
                keys.add(f"unresolved:{candidate.generation}")
                continue
            keys.add("path:" + path_key(location.path))
            unc = self._canonical_unc(location.path)
            if unc:
                keys.add("unc:" + unc)
        for match in candidate.catalog_matches:
            if match.get("entry_path"):
                keys.add("obj:" + str(match.get("object_id") or path_key(match["entry_path"])))
        candidate.identity_keys = keys
        return keys

    def _merge_candidates(self):
        union = _UnionFind()
        for candidate in self._candidates:
            for key in self._candidate_keys(candidate):
                union.add(key)
            keys = list(self._candidate_keys(candidate))
            for key in keys[1:]:
                union.union(keys[0], key)

        groups = {}
        for candidate in self._candidates:
            keys = sorted(self._candidate_keys(candidate))
            root = union.find(keys[0])
            groups.setdefault(root, []).append(candidate)

        merged = []
        for group in groups.values():
            group = sorted(group, key=lambda item: item.generation)
            primary = Candidate(
                generation=group[0].generation,
                source=group[0].source,
                kind=group[0].kind,
            )
            for candidate in group:
                primary.merge(candidate)
                primary.catalog_matches.extend(candidate.catalog_matches)
                primary.volume_evidence.extend(candidate.volume_evidence)
                primary.share_evidence.extend(candidate.share_evidence)
            primary.contributing_sources = sorted(
                {source for candidate in group for source in candidate.sources}
            )
            primary.catalog_matches = self._deduplicate_matches(primary.catalog_matches)
            primary.volume_evidence = self._deduplicate_evidence(primary.volume_evidence)
            primary.share_evidence = self._deduplicate_share_evidence(primary.share_evidence)
            primary.locations = sorted(
                primary.locations, key=lambda item: (item.path is None, path_key(item.path))
            )
            for location in primary.locations:
                for value in location.fields:
                    if value.key() not in {item.key() for item in primary.provenance}:
                        primary.provenance.append(value)
            merged.append(primary)

        merged.sort(
            key=lambda item: (
                path_key(item.path) if item.path else "",
                item.generation,
            )
        )
        return merged

    @staticmethod
    def _deduplicate_share_evidence(values):
        result = []
        seen = set()
        for value in values:
            key = (value.get("type"), value.get("drive"), value.get("unc"), value.get("direction"))
            if key in seen:
                continue
            seen.add(key)
            result.append(value)
        return result

    @staticmethod
    def _assign_ids(merged):
        for index, candidate in enumerate(merged, start=1):
            candidate.candidate_id = f"C{index:02d}"

    def _remap_conflict_candidates(self, merged):
        generation_to_id = {candidate.generation: candidate.candidate_id for candidate in merged}
        for conflict in self._conflicts:
            if not conflict.candidates:
                continue
            conflict.candidates = [
                generation_to_id.get(value, value) for value in conflict.candidates
            ]

    def _record_cross_candidate_conflicts(self, merged):
        for index, left in enumerate(merged):
            if not left.path:
                continue
            for right in merged[index + 1 :]:
                if not right.path:
                    continue
                if self._same_object(left, right):
                    continue
                # Encoding variants of the same source were already reported
                # with their exact fields.  Do not turn that single fact into
                # a second "path_conflict".
                if (
                    left.contributing_sources
                    and left.contributing_sources == right.contributing_sources
                ):
                    continue
                if self._covered_by_encoding_conflict(left, right):
                    continue
                self._conflicts.append(
                    Conflict(
                        type="path_conflict",
                        message=(
                            "candidate paths disagree and no catalog/share evidence "
                            "shows they are the same object"
                        ),
                        sources=sorted(set(left.sources) | set(right.sources)),
                        values=[
                            {"candidate": left.candidate_id, "path": left.path},
                            {"candidate": right.candidate_id, "path": right.path},
                        ],
                        fields=list(left.provenance) + list(right.provenance),
                        candidates=[left.candidate_id, right.candidate_id],
                    )
                )

    def _covered_by_encoding_conflict(self, left, right):
        if not any(conflict.type.endswith("encoding_conflict") for conflict in self._conflicts):
            return False
        left_encodings = {location.encoding for location in left.locations if location.encoding}
        right_encodings = {location.encoding for location in right.locations if location.encoding}
        left_sources = set(left.sources)
        right_sources = set(right.sources)
        if not left_encodings or not right_encodings:
            return False
        if left_sources != right_sources:
            return False
        return left_encodings != right_encodings

    def _same_object(self, left, right):
        if left.catalog_matches and right.catalog_matches:
            left_objects = {
                str(match.get("object_id") or path_key(match.get("entry_path")))
                for match in left.catalog_matches
            }
            right_objects = {
                str(match.get("object_id") or path_key(match.get("entry_path")))
                for match in right.catalog_matches
            }
            if left_objects & right_objects:
                return True
        for left_path in left.paths:
            left_unc = self._canonical_unc(left_path)
            for right_path in right.paths:
                if path_key(left_path) == path_key(right_path):
                    return True
                right_unc = self._canonical_unc(right_path)
                if left_unc and right_unc and left_unc == right_unc:
                    return True
        return False

    def _record_kind_conflicts(self, merged):
        for candidate in merged:
            if not candidate.catalog_matches:
                continue
            kinds = {match.get("kind") for match in candidate.catalog_matches if match.get("kind")}
            if candidate.kind and kinds and candidate.kind not in kinds:
                self._conflicts.append(
                    Conflict(
                        type="kind_conflict",
                        message="LNK item kind disagrees with catalog entry kind",
                        sources=candidate.sources,
                        values=[{"candidate_kind": candidate.kind, "catalog_kinds": sorted(kinds)}],
                        fields=list(candidate.provenance),
                        candidates=[candidate.candidate_id],
                    )
                )

        header_kind = _kind_from_header(self._facts.file_flags)
        idlist_kind = None
        for candidate in merged:
            if "idlist" in candidate.sources and candidate.kind:
                idlist_kind = candidate.kind
                break
        if header_kind and idlist_kind and header_kind != idlist_kind:
            self._conflicts.append(
                Conflict(
                    type="file_attribute_conflict",
                    message=(
                        "ShellLinkHeader FileAttributes and the target ID list "
                        "disagree about file/directory"
                    ),
                    sources=["idlist"],
                    values=[{"header": header_kind, "id_list": idlist_kind}],
                )
            )

    def _group_eligible(self, candidate):
        if candidate.missing_environment:
            return False, "missing environment evidence"
        if not candidate.path:
            return False, "no concrete path"
        if candidate.kind == "directory" and not self.policy.allow_directory_targets:
            return False, "directory targets are disallowed by policy"
        if candidate.catalog_matches:
            incompatible = [
                match
                for match in candidate.catalog_matches
                if match.get("kind") and candidate.kind and match.get("kind") != candidate.kind
            ]
            if incompatible:
                return False, "catalog kind contradicts the LNK item"
            return True, "catalog evidence"

        if self.policy.confirm_without_catalog or not self.policy.require_catalog_evidence:
            strong_sources = set(candidate.sources)
            volume_support = any(evidence.get("supports") for evidence in candidate.volume_evidence)
            if len(strong_sources) >= 2 or volume_support:
                return True, "corroborated without catalog evidence"
        return False, "no catalog evidence"

    def _passes_strict_conflicts(self, candidate):
        if not self.policy.strict_conflicts:
            return True
        if candidate.missing_environment:
            return False
        for conflict in self._conflicts:
            if not conflict.candidates:
                continue
            if candidate.candidate_id not in conflict.candidates:
                continue
            if conflict.type in {
                "path_conflict",
                "catalog_object_conflict",
                "kind_conflict",
                "file_attribute_conflict",
                "volume_identity_mismatch",
            }:
                return False
        return True

    def _source_preference(self, candidate):
        preference = max(
            (self.policy.preference(source) for source in candidate.sources), default=0
        )
        if self._facts.has_flag("PreferEnvironmentPath") and "environment" in candidate.sources:
            preference += 1
        if self.policy.prefer_unicode and any(
            location.encoding == "unicode" for location in candidate.locations
        ):
            preference += 1
        return preference

    def _select(self, merged):
        eligible = []
        for candidate in merged:
            ok, reason = self._group_eligible(candidate)
            if ok and not self._passes_strict_conflicts(candidate):
                ok, reason = False, "blocked by a conflict (strict_conflicts)"
            if ok:
                eligible.append(candidate)
            else:
                candidate.status = "branch"
                candidate.reason = reason

        if not eligible:
            return "unresolved", None

        if len(eligible) == 1:
            selected = eligible[0]
            selected.status = "confirmed"
            selected.reason = "policy and catalog evidence agree"
            for candidate in merged:
                if candidate is not selected:
                    candidate.status = "branch"
                    candidate.reason = candidate.reason or "not supported by catalog evidence"
            return "confirmed", selected

        preferences = [self._source_preference(candidate) for candidate in eligible]
        best = max(preferences)
        best_candidates = [
            candidate
            for candidate, preference in zip(eligible, preferences, strict=True)
            if preference == best
        ]
        if best > 0 and len(best_candidates) == 1:
            selected = best_candidates[0]
            selected.status = "confirmed"
            selected.reason = "selected by policy source preference among catalog matches"
            for candidate in merged:
                if candidate is not selected:
                    candidate.status = "branch"
                    candidate.reason = (
                        candidate.reason
                        if candidate not in eligible
                        else "another catalog match was preferred by policy"
                    )
            return "confirmed", selected

        for candidate in eligible:
            candidate.status = "branch"
            candidate.reason = "multiple distinct catalog-supported candidates"
        return "ambiguous", None

    @staticmethod
    def _deduplicate_conflicts(conflicts):
        result = []
        seen = set()
        for conflict in conflicts:
            key = (
                conflict.type,
                conflict.message,
                str(conflict.values),
                tuple(conflict.sources),
            )
            if key in seen:
                continue
            seen.add(key)
            result.append(conflict)
        return result


@dataclass
class ResolutionResult:
    candidates: list
    conflicts: list
    policy: ResolutionPolicy
    profile_fingerprint: str
    facts: object
    missing_environment: list = field(default_factory=list)
    catalog_conflicts: list = field(default_factory=list)
    status: str = "unresolved"
    selected: object = None

    @property
    def selected_target(self):
        return self.selected.path if self.selected else None

    @property
    def confirmed(self):
        return [candidate for candidate in self.candidates if candidate.status == "confirmed"]

    @property
    def branches(self):
        return [candidate for candidate in self.candidates if candidate.status == "branch"]

    @property
    def unresolved(self):
        return self.selected is None

    @property
    def has_conflicts(self):
        return bool(self.conflicts)

    @property
    def target(self):
        return self.selected

    def as_dict(self):
        return {
            "status": self.status,
            "selected_target": self.selected_target,
            "profile_fingerprint": self.profile_fingerprint,
            "policy": self.policy.as_dict(),
            "target": self.selected.as_dict() if self.selected else None,
            "confirmed": [candidate.as_dict() for candidate in self.confirmed],
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "branches": [candidate.as_dict() for candidate in self.branches],
            "conflicts": [conflict.as_dict() for conflict in self.conflicts],
            "missing_environment": list(self.missing_environment),
        }

    def to_json(self, indent=2):
        return json.dumps(self.as_dict(), indent=indent, sort_keys=False)

    def __iter__(self):
        return iter(self.candidates)


def resolve(lnk, profile=None, policy=None):
    """Resolve *lnk* against *profile* without touching the target.

    ``lnk`` may be a parsed :class:`LnkParse3.lnk_file.LnkFile`, a dict returned
    by ``get_json``, or a :class:`LnkFacts` instance.  ``profile`` may be a
    :class:`MachineProfile`, a mapping, or ``None``.  ``policy`` can override
    the policy embedded in the profile.
    """
    if isinstance(profile, MachineProfile):
        machine = profile
    else:
        machine = MachineProfile.from_dict(profile or {})
    if policy is not None:
        machine = copy.copy(machine)
        machine.policy = (
            policy if isinstance(policy, ResolutionPolicy) else ResolutionPolicy.from_dict(policy)
        )

    resolver = Resolver(machine)
    if hasattr(lnk, "__dataclass_fields__") and "id_items" in lnk.__dataclass_fields__:
        return resolver.resolve_facts(lnk)
    return resolver.resolve(lnk)
