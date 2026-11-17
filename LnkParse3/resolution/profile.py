"""Machine profile and resolution policy objects.

A profile is a description of *another* machine: its environment variables,
volumes, share mappings and the files/directories that are known to exist
there.  Nothing in this module (or in the resolver) touches the local
filesystem or the network.
"""

import hashlib
import json
from dataclasses import dataclass
from dataclasses import field


SOURCE_ALIASES = {
    "idlist": "idlist",
    "id_list": "idlist",
    "target_id_list": "idlist",
    "targetidlist": "idlist",
    "linkinfo": "link_info_local",
    "link_info": "link_info_local",
    "link_info_local": "link_info_local",
    "linkinfo_local": "link_info_local",
    "local_link_info": "link_info_local",
    "link_info_network": "link_info_network",
    "linkinfo_network": "link_info_network",
    "network_link_info": "link_info_network",
    "relative_path": "relative_path",
    "relativepath": "relative_path",
    "environment": "environment",
    "environment_variable": "environment",
    "environmental_variables": "environment",
    "env": "environment",
}


def canonical_source(value):
    key = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    return SOURCE_ALIASES.get(key, key)


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _normalize_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


@dataclass
class Volume:
    """A volume known to exist on the target machine."""

    drive: str = None
    guid: str = None
    label: str = None
    serial_number: str = None
    drive_type: str = None
    aliases: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, data):
        if isinstance(data, str):
            data = {"drive": data}
        return cls(
            drive=data.get("drive"),
            guid=data.get("guid") or data.get("volume_guid"),
            label=data.get("label") if "label" in data else data.get("volume_label"),
            serial_number=data.get("serial_number") or data.get("serial"),
            drive_type=data.get("drive_type") or data.get("type"),
            aliases=_as_list(data.get("aliases")),
        )

    def as_dict(self):
        result = {}
        for key in ("drive", "guid", "label", "serial_number", "drive_type"):
            value = getattr(self, key)
            if value is not None:
                result[key] = value
        if self.aliases:
            result["aliases"] = list(self.aliases)
        return result


@dataclass
class ShareMapping:
    """A UNC share and the drive letter(s) under which it is mounted."""

    unc: str = None
    drives: list = field(default_factory=list)
    aliases: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, data):
        if isinstance(data, str):
            return cls(unc=data)
        drives = data.get("drives")
        if drives is None:
            drives = data.get("drive") or data.get("letters") or []
        return cls(
            unc=data.get("unc") or data.get("path") or data.get("share"),
            drives=_as_list(drives),
            aliases=_as_list(data.get("aliases")),
        )

    def as_dict(self):
        return {
            "unc": self.unc,
            "drives": list(self.drives),
            "aliases": list(self.aliases),
        }


@dataclass
class CatalogEntry:
    """A file or directory that is known to exist on the target machine."""

    path: str = None
    kind: str = None
    object_id: str = None
    size: int = None
    aliases: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data):
        if isinstance(data, str):
            data = {"path": data}
        kind = data.get("kind") or data.get("type")
        if isinstance(kind, str):
            kind = kind.strip().lower()
        entry = cls(
            path=data.get("path") or data.get("name"),
            kind=kind,
            object_id=data.get("object_id") or data.get("id"),
            size=data.get("size"),
            aliases=_as_list(data.get("aliases")),
            metadata={
                key: value
                for key, value in data.items()
                if key
                not in {
                    "path",
                    "name",
                    "kind",
                    "type",
                    "object_id",
                    "id",
                    "size",
                    "aliases",
                }
            },
        )
        return entry

    def as_dict(self):
        result = {"path": self.path}
        if self.kind is not None:
            result["kind"] = self.kind
        if self.object_id is not None:
            result["object_id"] = self.object_id
        if self.size is not None:
            result["size"] = self.size
        if self.aliases:
            result["aliases"] = list(self.aliases)
        result.update(self.metadata)
        return result


@dataclass
class ResolutionPolicy:
    """Which shell link fields may contribute, and how strongly.

    ``disabled_sources`` always wins.  ``preferred_sources`` is an ordered list
    used only to break ties after catalog evidence has been considered; it does
    not remove competing candidates or conflicts.
    """

    disabled_sources: list = field(default_factory=list)
    preferred_sources: list = field(default_factory=list)
    allowed_sources: list = field(default_factory=list)
    allow_relative_path: bool = True
    allow_environment_expansion: bool = True
    honor_link_flags: bool = True
    require_catalog_evidence: bool = True
    strict_conflicts: bool = False
    prefer_unicode: bool = True
    allow_directory_targets: bool = True
    confirm_without_catalog: bool = False
    extra: dict = field(default_factory=dict)

    def __post_init__(self):
        self.disabled_sources = [
            canonical_source(source) for source in _as_list(self.disabled_sources)
        ]
        self.preferred_sources = [
            canonical_source(source) for source in _as_list(self.preferred_sources)
        ]
        self.allowed_sources = [
            canonical_source(source) for source in _as_list(self.allowed_sources)
        ]

    @classmethod
    def from_dict(cls, data):
        data = dict(data or {})
        known = {
            "disabled_sources",
            "preferred_sources",
            "allowed_sources",
            "allow_relative_path",
            "allow_environment_expansion",
            "honor_link_flags",
            "require_catalog_evidence",
            "strict_conflicts",
            "prefer_unicode",
            "allow_directory_targets",
            "confirm_without_catalog",
        }
        policy = cls(
            disabled_sources=data.get("disabled_sources") or data.get("disable") or [],
            preferred_sources=data.get("preferred_sources") or data.get("prefer") or [],
            allowed_sources=data.get("allowed_sources") or data.get("allow") or [],
            allow_relative_path=_normalize_bool(data.get("allow_relative_path"), True),
            allow_environment_expansion=_normalize_bool(
                data.get("allow_environment_expansion"), True
            ),
            honor_link_flags=_normalize_bool(data.get("honor_link_flags"), True),
            require_catalog_evidence=_normalize_bool(data.get("require_catalog_evidence"), True),
            strict_conflicts=_normalize_bool(data.get("strict_conflicts"), False),
            prefer_unicode=_normalize_bool(data.get("prefer_unicode"), True),
            allow_directory_targets=_normalize_bool(data.get("allow_directory_targets"), True),
            confirm_without_catalog=_normalize_bool(data.get("confirm_without_catalog"), False),
            extra={key: value for key, value in data.items() if key not in known},
        )
        return policy

    def is_allowed(self, source):
        source = canonical_source(source)
        if source in self.disabled_sources:
            return False
        if self.allowed_sources and source not in self.allowed_sources:
            return False
        return not (source == "relative_path" and not self.allow_relative_path)

    def preference(self, source):
        source = canonical_source(source)
        if source in self.preferred_sources:
            return len(self.preferred_sources) - self.preferred_sources.index(source)
        return 0

    def as_dict(self):
        result = {
            "disabled_sources": list(self.disabled_sources),
            "preferred_sources": list(self.preferred_sources),
            "allowed_sources": list(self.allowed_sources),
            "allow_relative_path": self.allow_relative_path,
            "allow_environment_expansion": self.allow_environment_expansion,
            "honor_link_flags": self.honor_link_flags,
            "require_catalog_evidence": self.require_catalog_evidence,
            "strict_conflicts": self.strict_conflicts,
            "prefer_unicode": self.prefer_unicode,
            "allow_directory_targets": self.allow_directory_targets,
            "confirm_without_catalog": self.confirm_without_catalog,
        }
        result.update(self.extra)
        return result


class MachineProfile:
    """Environment + volumes + shares + catalog for one target machine."""

    def __init__(
        self,
        environment=None,
        volumes=None,
        shares=None,
        catalog=None,
        policy=None,
    ):
        if isinstance(policy, ResolutionPolicy):
            self.policy = policy
        else:
            self.policy = ResolutionPolicy.from_dict(policy or {})

        self.environment = {str(key): str(value) for key, value in (environment or {}).items()}
        self.volumes = [
            volume if isinstance(volume, Volume) else Volume.from_dict(volume)
            for volume in _as_list(volumes)
        ]
        self.shares = self._build_shares(shares)
        self.catalog = [
            entry if isinstance(entry, CatalogEntry) else CatalogEntry.from_dict(entry)
            for entry in _as_list(catalog)
        ]

    @staticmethod
    def _build_shares(shares):
        if not shares:
            return []
        result = []
        if isinstance(shares, dict):
            for drive, value in shares.items():
                if isinstance(value, dict):
                    item = dict(value)
                    item.setdefault("drive", drive)
                    result.append(ShareMapping.from_dict(item))
                else:
                    result.append(ShareMapping.from_dict({"unc": value, "drives": [drive]}))
            return result
        result.extend(
            share if isinstance(share, ShareMapping) else ShareMapping.from_dict(share)
            for share in shares
        )
        return result

    @classmethod
    def from_dict(cls, data):
        data = dict(data or {})
        catalog = data.get("catalog")
        if catalog is None:
            catalog = data.get("files")
        if catalog is None:
            catalog = data.get("available_files")
        if catalog is None:
            catalog = data.get("directories")
        if catalog is None:
            catalog = []
        return cls(
            environment=data.get("environment") or data.get("env") or {},
            volumes=data.get("volumes") or data.get("drives") or [],
            shares=data.get("shares") or data.get("share_mappings") or [],
            catalog=catalog,
            policy=data.get("policy") or data.get("resolution_policy") or {},
        )

    @classmethod
    def from_json_file(cls, path):
        with open(path, encoding="utf-8") as handle:
            return cls.from_dict(json.load(handle))

    def as_dict(self):
        return {
            "environment": dict(sorted(self.environment.items())),
            "volumes": [volume.as_dict() for volume in self.volumes],
            "shares": [share.as_dict() for share in self.shares],
            "catalog": [entry.as_dict() for entry in self.catalog],
            "policy": self.policy.as_dict(),
        }

    def fingerprint(self):
        """A stable identity for the complete machine configuration.

        The resolver never stores results globally.  The fingerprint is exposed
        so callers can prove that a saved result belongs to the profile that
        produced it instead of silently reusing a previous machine's answer.
        """
        payload = json.dumps(self.as_dict(), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
