"""
Version-aware comparison of Logix UDT/AOI templates.

The normal :class:`~pycomm3.LogixDriver` type cache is keyed by the type name
only.  That is fine for a single online controller, but it is not sufficient
when an acquisition archive contains data interpreted with several revisions
of the same UDT.  This module keeps the catalog and the acquired bytes tied to
one ``(device, catalog version)`` pair and compares two revisions explicitly.

The comparison separates four outcomes:

``unchanged``
    Nothing that affects interpretation changed.

``rename_only``
    Member or type names changed, but every byte, bit, array extent, and
    primitive type is identical.  Old values are still valid; only labels need
    updating.

``layout_compatible``
    The physical slots are the same, but a member's meaning changed (for
    example ``DINT`` became ``REAL`` at the same offset and size, or a nested
    type changed without moving any offsets).  The original bytes can be
    interpreted using the new layout, but the old interpretation must not be
    treated as current.

``reread_required``
    An offset, bit position, array extent, member size, or the outer structure
    size changed.  The bytes captured for the old catalog version do not
    describe the new layout; a fresh acquisition is required.

If either side lacks a template, the result is ``undetermined``.  Missing
types are never filled in from another device, even when the type name matches.
"""

import copy
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, NamedTuple, Optional, Tuple

from .cip import DataTypes


__all__ = [
    "ChangeKind",
    "Verdict",
    "MemberChange",
    "MemberLayout",
    "TypeLayout",
    "TypeComparison",
    "DependencyLink",
    "TagImpact",
    "CatalogReport",
    "UDTCatalog",
    "TypeCatalog",
    "CatalogStore",
    "VersionedTypeCache",
    "Acquisition",
    "AcquisitionArchive",
    "AcquisitionCatalog",
    "ArchiveAssessment",
    "MissingTemplateError",
    "MissingCatalogError",
    "DeviceMismatchError",
    "build_type_layout",
    "build_catalog",
    "catalog_from_data_types",
    "catalog_from_tags",
    "compare_type_layouts",
    "compare_type_definitions",
    "compare_catalogs",
    "dependency_chain",
    "snapshot_types",
    "snapshot_driver",
    "catalog_from_driver",
    "compare_udt_versions",
    "compare_tag_catalogs",
]


class ChangeKind(str, Enum):
    """How a type or member change affects already acquired bytes."""

    UNCHANGED = "unchanged"
    RENAME_ONLY = "rename_only"
    LAYOUT_COMPATIBLE = "layout_compatible"
    REREAD_REQUIRED = "reread_required"
    UNDETERMINED = "undetermined"

    def __str__(self):
        return self.value


Verdict = ChangeKind


_RANK = {
    ChangeKind.UNCHANGED: 0,
    ChangeKind.RENAME_ONLY: 1,
    ChangeKind.LAYOUT_COMPATIBLE: 2,
    ChangeKind.UNDETERMINED: 3,
    ChangeKind.REREAD_REQUIRED: 4,
}


def _worst(*kinds):
    return max((k for k in kinds if k is not None), key=lambda k: _RANK[k])


class MissingTemplateError(LookupError):
    """No template exists for this exact device, catalog version, and type."""

    def __init__(self, device_id, version, type_name, detail=""):
        self.device_id = device_id
        self.version = version
        self.type_name = type_name
        self.detail = detail
        message = (
            f"No UDT template for type {type_name!r} on device {device_id!r} "
            f"in catalog version {version!r}"
        )
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


class MissingCatalogError(LookupError):
    """No catalog exists for this device and version."""

    def __init__(self, device_id, version):
        self.device_id = device_id
        self.version = version
        super().__init__(
            f"No UDT catalog for device {device_id!r} and version {version!r}"
        )


class DeviceMismatchError(ValueError):
    """Raised when catalogs from different devices are compared."""

    def __init__(self, old_device, new_device):
        self.old_device = old_device
        self.new_device = new_device
        super().__init__(
            "Refusing to compare catalogs from different devices: "
            f"{old_device!r} and {new_device!r}.  A same-named type from "
            "another device is not a valid substitute."
        )


class MemberLayout(NamedTuple):
    """The physical and semantic description of one template member."""

    name: str
    offset: int
    bit: Optional[int]
    type_name: str
    array: int
    size: int
    nested: Optional[Any]  # TypeLayout, kept loose for Python 3.6 typing

    @property
    def total_size(self):
        return self.size * self.array if self.array else self.size

    @property
    def is_bit_member(self):
        return self.bit is not None

    @property
    def structural_key(self):
        """
        Keys used to pair members by physical slot.

        Names and primitive type names are intentionally excluded: a renamed
        member still occupies the same slot, and ``DINT`` to ``REAL`` at the
        same offset is a layout-compatible reinterpretation rather than a
        physical move.
        """

        return (self.offset, self.bit, self.array, self.size)


class TypeLayout(NamedTuple):
    """Immutable, name-aware snapshot of a UDT/AOI template."""

    name: str
    size: int
    members: Tuple[MemberLayout, ...]
    complete: bool = True
    missing_reason: Optional[str] = None

    @property
    def signature(self):
        """Physical layout only; excludes all display names."""

        return (
            self.size,
            tuple(
                m.structural_key
                for m in sorted(self.members, key=_member_sort_key)
            ),
        )

    @property
    def named_signature(self):
        """Physical layout plus member names, type names, and nested names."""

        return (
            self.name,
            self.size,
            tuple(
                (
                    m.name,
                    m.type_name,
                    m.offset,
                    m.bit,
                    m.array,
                    m.size,
                    m.nested.named_signature if m.nested is not None else None,
                )
                for m in sorted(self.members, key=_member_sort_key)
            ),
        )

    def member(self, name):
        for member in self.members:
            if member.name == name:
                return member
        return None


def _member_sort_key(member):
    return (member.offset, -1 if member.bit is None else member.bit, member.name)


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _atomic_size(type_name):
    if type_name is None:
        return None
    data_type = DataTypes.get(type_name)
    if data_type is None:
        return None
    size = getattr(data_type, "size", None)
    if size is None:
        return None
    size = int(size)
    if size <= 0:
        # Variable-length types (STRING, EPATH, etc.) do not carry a fixed
        # size outside a concrete template.  Treat them as unknown rather
        # than assuming zero bytes.
        return None
    return size


def build_type_layout(definition, name=None, _stack=None, definitions=None) -> TypeLayout:
    """
    Convert a pycomm3 data type definition into an immutable :class:`TypeLayout`.

    ``definition`` is the ``data_type`` dict found in ``LogixDriver.tags`` or
    ``LogixDriver.data_types``.  Missing or malformed template data produces an
    incomplete layout instead of guessing sizes or reusing another definition.

    ``definitions`` is an optional mapping of other type definitions from the
    *same* device and catalog version.  It is used only to resolve nested UDTs
    whose inline definition is absent; it is never consulted across catalogs.
    """

    if not isinstance(definition, dict):
        return TypeLayout(
            name=str(name or definition),
            size=0,
            members=(),
            complete=False,
            missing_reason="no type definition",
        )

    type_name = name or definition.get("name")
    template = definition.get("template")
    internal_tags = definition.get("internal_tags")

    if not isinstance(template, dict) or "structure_size" not in template:
        return TypeLayout(
            name=str(type_name or ""),
            size=0,
            members=(),
            complete=False,
            missing_reason="missing structure template",
        )

    if not isinstance(internal_tags, dict):
        return TypeLayout(
            name=str(type_name or ""),
            size=_as_int(template.get("structure_size")),
            members=(),
            complete=False,
            missing_reason="missing internal member definitions",
        )

    if _stack is None:
        _stack = []
    if type_name in _stack:
        return TypeLayout(
            name=str(type_name or ""),
            size=_as_int(template.get("structure_size")),
            members=(),
            complete=False,
            missing_reason="recursive type definition",
        )

    stack = list(_stack) + [type_name]
    members = []
    reasons = []

    for member_name, info in internal_tags.items():
        if not isinstance(info, dict) or "offset" not in info:
            reasons.append(f"member {member_name!r} has no offset")
            continue

        member_type_name = info.get("data_type_name") or ""
        offset = _as_int(info.get("offset"))
        array = _as_int(info.get("array"), 0)
        bit = info.get("bit")
        bit = _as_int(bit) if bit is not None else None

        nested = None
        if isinstance(info.get("data_type"), dict) or info.get("tag_type") == "struct":
            nested_definition = None
            if definitions:
                nested_definition = definitions.get(member_type_name)
            if not isinstance(nested_definition, dict):
                nested_definition = info.get("data_type")
            nested = build_type_layout(
                nested_definition,
                name=member_type_name or None,
                _stack=stack,
                definitions=definitions,
            )
            if not nested.complete:
                reasons.append(
                    f"member {member_name!r} depends on incomplete type "
                    f"{nested.name!r} ({nested.missing_reason})"
                )
            member_size = nested.size
        elif bit is not None:
            # BOOL members are bit aliases into another member.  They do not
            # consume bytes of their own, but their offset/bit pair is part of
            # the bit-packing layout and is compared explicitly.
            member_size = 0
        else:
            size = _atomic_size(member_type_name)
            if size is None:
                reasons.append(
                    f"member {member_name!r} has unknown atomic type "
                    f"{member_type_name!r}"
                )
                size = 0
            member_size = size

        members.append(
            MemberLayout(
                name=str(member_name),
                offset=offset,
                bit=bit,
                type_name=str(member_type_name),
                array=array,
                size=member_size,
                nested=nested,
            )
        )

    members.sort(key=_member_sort_key)

    return TypeLayout(
        name=str(type_name or ""),
        size=_as_int(template.get("structure_size")),
        members=tuple(members),
        complete=not reasons,
        missing_reason="; ".join(reasons) if reasons else None,
    )


def _register_nested(layout, destination, reserved=None):
    reserved = reserved or set()
    for member in layout.members:
        if member.nested is None:
            continue

        nested = member.nested
        name = nested.name
        if name:
            existing = destination.get(name)
            if existing is None:
                destination[name] = nested
            elif (
                existing.named_signature != nested.named_signature
                and not (existing.missing_reason or "").startswith(
                    "conflicting"
                )
            ):
                destination[name] = existing._replace(
                    complete=False,
                    missing_reason=(
                        f"conflicting nested definitions for type {name!r}"
                    ),
                )
        _register_nested(nested, destination, reserved)


class UDTCatalog:
    """
    Immutable-by-convention catalog of type layouts for one device revision.

    A catalog is deliberately scoped to exactly one ``device_id`` and one
    ``version``.  Lookups never fall through to another device or another
    catalog version.  When the same catalog contains both a standalone
    definition and an inline nested copy of a type, the standalone definition
    is authoritative; contradictory standalone definitions are marked
    undetermined rather than silently picking one.
    """

    def __init__(self, device_id, version, definitions=None):
        self.device_id = str(device_id)
        self.version = str(version)
        self._raw = {}
        self._conflicts = set()
        self._layouts = {}

        if definitions is None:
            return

        if isinstance(definitions, Mapping):
            candidates = list(definitions.values())
        else:
            candidates = list(definitions)
        for definition in candidates:
            self._collect(definition)
        self._rebuild()

    def _collect(self, definition):
        if not isinstance(definition, dict):
            return False
        name = definition.get("name")
        if not name:
            return False
        if name in self._raw:
            if self._raw[name] is not definition:
                existing = build_type_layout(self._raw[name])
                candidate = build_type_layout(definition)
                if existing.named_signature != candidate.named_signature:
                    self._conflicts.add(name)
        else:
            self._raw[name] = definition
        return True

    def _rebuild(self):
        layouts = {}
        for name, definition in self._raw.items():
            layout = build_type_layout(definition, definitions=self._raw)
            if not layout.name:
                continue
            if layout.name in self._conflicts:
                layout = layout._replace(
                    complete=False,
                    missing_reason=f"conflicting definitions for type {layout.name!r}",
                )
            layouts[layout.name] = layout

        for layout in list(layouts.values()):
            _register_nested(layout, layouts, reserved=set(self._raw))

        self._layouts = layouts

    @classmethod
    def from_data_types(cls, device_id, version, data_types):
        return cls(device_id, version, definitions=data_types)

    @classmethod
    def from_tags(cls, device_id, version, tags):
        catalog = cls(device_id, version)
        for tag in _iter_tags(tags):
            if tag.get("tag_type") == "struct":
                catalog._collect(tag.get("data_type"))
        catalog._rebuild()
        return catalog

    def add_definition(self, definition):
        if not isinstance(definition, dict) or not definition.get("name"):
            return None
        name = definition.get("name")
        self._collect(definition)
        self._rebuild()
        return self._layouts.get(name)

    def get_layout(self, type_name):
        try:
            return self._layouts[type_name]
        except KeyError:
            raise MissingTemplateError(self.device_id, self.version, type_name)

    def has_type(self, type_name):
        return type_name in self._layouts

    @property
    def names(self):
        return tuple(sorted(self._layouts))

    @property
    def types(self):
        return MappingProxyType(dict(self._layouts))

    def __contains__(self, type_name):
        return self.has_type(type_name)

    def __len__(self):
        return len(self._layouts)

    def compare(self, new_catalog, old_tags, new_tags=None):
        """Compare tags against another catalog for the same device."""

        return compare_catalogs(self, new_catalog, old_tags, new_tags)

    def __repr__(self):
        return (
            f"UDTCatalog(device_id={self.device_id!r}, "
            f"version={self.version!r}, types={len(self._layouts)})"
        )


TypeCatalog = UDTCatalog


class CatalogStore:
    """
    Versioned, per-device type cache.

    This is the replacement for a global ``{type name: definition}`` cache when
    more than one acquisition version is in play.  The key is always
    ``(device_id, version, type_name)``.
    """

    def __init__(self):
        self._catalogs = {}

    def add(self, catalog):
        key = (catalog.device_id, catalog.version)
        existing = self._catalogs.get(key)
        if existing is None:
            self._catalogs[key] = catalog
            return catalog

        for name, definition in catalog._raw.items():
            existing._collect(definition)
        existing._rebuild()

        conflicted = [
            name
            for name, layout in existing.types.items()
            if (layout.missing_reason or "").startswith("conflicting")
        ]
        if conflicted:
            raise ValueError(
                f"Catalog {key!r} contains conflicting layouts for "
                f"types {sorted(conflicted)!r}"
            )
        return existing

    def add_catalog(self, device_id, version, definitions):
        return self.add(UDTCatalog(device_id, version, definitions))

    def get(self, device_id, version):
        key = (str(device_id), str(version))
        try:
            return self._catalogs[key]
        except KeyError:
            raise MissingCatalogError(*key)

    def get_layout(self, device_id, version, type_name):
        try:
            catalog = self.get(device_id, version)
        except MissingCatalogError as err:
            raise MissingTemplateError(
                str(device_id),
                str(version),
                type_name,
                detail="no catalog for this device and version",
            ) from err
        return catalog.get_layout(type_name)

    def has_type(self, device_id, version, type_name):
        try:
            self.get_layout(device_id, version, type_name)
            return True
        except (MissingCatalogError, MissingTemplateError):
            return False

    def compare(self, device_id, old_version, new_version, old_tags, new_tags=None):
        return compare_catalogs(
            self.get(device_id, old_version),
            self.get(device_id, new_version),
            old_tags,
            new_tags,
        )

    @property
    def catalogs(self):
        return MappingProxyType(dict(self._catalogs))


VersionedTypeCache = CatalogStore


def catalog_from_data_types(device_id, version, data_types):
    return UDTCatalog.from_data_types(device_id, version, data_types)


def catalog_from_tags(device_id, version, tags):
    return UDTCatalog.from_tags(device_id, version, tags)


def build_catalog(device_id, version=None, data_types=None, tags=None):
    """Build a catalog from ``data_types``, ``tags``, or both."""

    definitions = []
    if tags:
        definitions.extend(
            tag.get("data_type")
            for tag in _iter_tags(tags)
            if tag.get("tag_type") == "struct"
        )
    if data_types:
        definitions.extend(
            data_types.values() if isinstance(data_types, Mapping) else data_types
        )
    return UDTCatalog(device_id, version if version is not None else "", definitions)


def snapshot_types(data_types):
    """
    Deep-copy type definitions before cataloging.

    Useful when the caller keeps a live ``LogixDriver.data_types`` mapping that
    may be reloaded later.  The returned definitions share no mutable state
    with the driver cache.
    """

    if isinstance(data_types, Mapping):
        return {name: copy.deepcopy(definition) for name, definition in data_types.items()}
    return [copy.deepcopy(definition) for definition in data_types]


class MemberChange(NamedTuple):
    path: str
    kind: ChangeKind
    old: Optional[str] = None
    new: Optional[str] = None
    detail: str = ""

    def __str__(self):
        if self.detail:
            return f"{self.path}: {self.kind} ({self.detail})"
        return f"{self.path}: {self.kind}"


class TypeComparison(NamedTuple):
    old_name: str
    new_name: str
    verdict: ChangeKind
    changes: Tuple[MemberChange, ...]
    reason: str = ""

    @property
    def affected(self):
        return self.verdict is not ChangeKind.UNCHANGED

    @property
    def chain(self):
        return (self.old_name, self.new_name)

    def to_dict(self):
        return {
            "old_name": self.old_name,
            "new_name": self.new_name,
            "verdict": self.verdict.value,
            "changes": [_change_to_dict(c) for c in self.changes],
            "reason": self.reason,
        }


def _change_to_dict(change):
    return {
        "path": change.path,
        "kind": change.kind.value,
        "old": change.old,
        "new": change.new,
        "detail": change.detail,
    }


def _compare_member(old_member, new_member, path, changes):
    """Compare two paired members at the same physical slot."""

    if old_member.structural_key != new_member.structural_key:
        changes.append(
            MemberChange(
                path=path,
                kind=ChangeKind.REREAD_REQUIRED,
                old=_member_description(old_member),
                new=_member_description(new_member),
                detail="physical slot changed",
            )
        )
        return ChangeKind.REREAD_REQUIRED

    verdict = ChangeKind.UNCHANGED

    if old_member.name != new_member.name and not (
        _is_private_name(old_member.name) and _is_private_name(new_member.name)
    ):
        changes.append(
            MemberChange(
                path=path,
                kind=ChangeKind.RENAME_ONLY,
                old=old_member.name,
                new=new_member.name,
                detail="member renamed",
            )
        )
        verdict = _worst(verdict, ChangeKind.RENAME_ONLY)

    if old_member.nested is not None or new_member.nested is not None:
        if old_member.nested is None or new_member.nested is None:
            # One side is atomic and the other is a structure.  If the
            # physical slot (including its byte size) is the same, this is a
            # reinterpretation, not a missing template.
            changes.append(
                MemberChange(
                    path=path,
                    kind=ChangeKind.LAYOUT_COMPATIBLE,
                    old=_member_description(old_member),
                    new=_member_description(new_member),
                    detail=(
                        "atomic/structured field change at the same physical slot"
                    ),
                )
            )
            return _worst(verdict, ChangeKind.LAYOUT_COMPATIBLE)

        old_nested_name = old_member.nested.name or old_member.type_name
        new_nested_name = new_member.nested.name or new_member.type_name
        nested = compare_type_layouts(
            old_member.nested,
            new_member.nested,
            path=path,
            old_name=old_nested_name,
            new_name=new_nested_name,
        )
        changes.extend(nested.changes)
        verdict = _worst(verdict, nested.verdict)

        if old_nested_name != new_nested_name and nested.verdict is ChangeKind.UNCHANGED:
            changes.append(
                MemberChange(
                    path=path,
                    kind=ChangeKind.RENAME_ONLY,
                    old=old_nested_name,
                    new=new_nested_name,
                    detail="nested type renamed with identical layout",
                )
            )
            verdict = _worst(verdict, ChangeKind.RENAME_ONLY)
    elif old_member.type_name != new_member.type_name:
        changes.append(
            MemberChange(
                path=path,
                kind=ChangeKind.LAYOUT_COMPATIBLE,
                old=old_member.type_name,
                new=new_member.type_name,
                detail="same slot and size, different field type",
            )
        )
        verdict = _worst(verdict, ChangeKind.LAYOUT_COMPATIBLE)

    return verdict


def _is_private_name(name):
    return name.startswith("__") or name.startswith("ZZZZZZZZZZ")


def _member_description(member):
    parts = [member.type_name]
    if member.array:
        parts.append(f"[{member.array}]")
    parts.append(f"@{member.offset}")
    if member.bit is not None:
        parts.append(f".{member.bit}")
    return "".join(parts)


def _member_path(parent, member):
    suffix = f"[{member.array}]" if member.array else ""
    return f"{parent}.{member.name}{suffix}" if parent else f"{member.name}{suffix}"


def _pair_members(old_layout, new_layout):
    """
    Pair members by name first, then by exact physical slot.

    Pairing by name first is important: if two existing fields swap offsets,
    that is a physical move and must require a reread, not a rename.
    """

    old_by_name = {m.name: m for m in old_layout.members}
    new_by_name = {m.name: m for m in new_layout.members}
    common_names = sorted(set(old_by_name) & set(new_by_name))

    pairs = [(old_by_name[name], new_by_name[name]) for name in common_names]
    used_old = {id(m) for m, _ in pairs}
    used_new = {id(m) for _, m in pairs}

    remaining_old = [
        m for m in old_layout.members if id(m) not in used_old
    ]
    remaining_new = [
        m for m in new_layout.members if id(m) not in used_new
    ]

    buckets = {}
    for member in remaining_new:
        buckets.setdefault(member.structural_key, []).append(member)

    unmatched_old = []
    for member in remaining_old:
        bucket = buckets.get(member.structural_key)
        if bucket:
            pairs.append((member, bucket.pop(0)))
        else:
            unmatched_old.append(member)

    remaining_new = [
        member
        for bucket in buckets.values()
        for member in bucket
    ]
    remaining_old = unmatched_old

    remaining_old.sort(key=_member_sort_key)
    remaining_new.sort(key=_member_sort_key)
    return pairs, remaining_old, remaining_new


def compare_type_layouts(
    old_layout,
    new_layout,
    path="",
    old_name=None,
    new_name=None,
):
    """
    Compare two :class:`TypeLayout` snapshots.

    The comparison is recursive: a nested type whose outer size is unchanged
    is still inspected, so an internal offset/type/bit change cannot hide
    behind an unchanged ``structure_size``.
    """

    if not isinstance(old_layout, TypeLayout) or not isinstance(new_layout, TypeLayout):
        return TypeComparison(
            old_name or getattr(old_layout, "name", ""),
            new_name or getattr(new_layout, "name", ""),
            ChangeKind.UNDETERMINED,
            (),
            "type layout missing",
        )

    old_name = old_name or old_layout.name
    new_name = new_name or new_layout.name

    if not old_layout.complete:
        return TypeComparison(
            old_name,
            new_name,
            ChangeKind.UNDETERMINED,
            (
                MemberChange(
                    path=path or old_name,
                    kind=ChangeKind.UNDETERMINED,
                    detail=f"old template incomplete: {old_layout.missing_reason}",
                ),
            ),
            old_layout.missing_reason or "old template incomplete",
        )

    if not new_layout.complete:
        return TypeComparison(
            old_name,
            new_name,
            ChangeKind.UNDETERMINED,
            (
                MemberChange(
                    path=path or new_name,
                    kind=ChangeKind.UNDETERMINED,
                    detail=f"new template incomplete: {new_layout.missing_reason}",
                ),
            ),
            new_layout.missing_reason or "new template incomplete",
        )

    changes = []
    verdict = ChangeKind.UNCHANGED
    prefix = path or old_name

    if old_name != new_name:
        changes.append(
            MemberChange(
                path=prefix,
                kind=ChangeKind.RENAME_ONLY,
                old=old_name,
                new=new_name,
                detail="type name changed",
            )
        )
        verdict = _worst(verdict, ChangeKind.RENAME_ONLY)

    if old_layout.size != new_layout.size:
        changes.append(
            MemberChange(
                path=prefix,
                kind=ChangeKind.REREAD_REQUIRED,
                old=str(old_layout.size),
                new=str(new_layout.size),
                detail="outer structure size changed",
            )
        )
        verdict = ChangeKind.REREAD_REQUIRED

    common_pairs, remaining_old, remaining_new = _pair_members(
        old_layout, new_layout
    )

    for old_member, new_member in common_pairs:
        member_path = _member_path(prefix, old_member)
        verdict = _worst(
            verdict, _compare_member(old_member, new_member, member_path, changes)
        )

    if len(remaining_old) != len(remaining_new):
        changes.append(
            MemberChange(
                path=prefix,
                kind=ChangeKind.REREAD_REQUIRED,
                old=str(len(old_layout.members)),
                new=str(len(new_layout.members)),
                detail="member count changed",
            )
        )
        verdict = ChangeKind.REREAD_REQUIRED
    else:
        for old_member, new_member in zip(remaining_old, remaining_new):
            member_path = _member_path(prefix, old_member)
            if old_member.structural_key != new_member.structural_key:
                changes.append(
                    MemberChange(
                        path=member_path,
                        kind=ChangeKind.REREAD_REQUIRED,
                        old=_member_description(old_member),
                        new=_member_description(new_member),
                        detail="physical slot changed",
                    )
                )
                verdict = ChangeKind.REREAD_REQUIRED
                continue
            verdict = _worst(
                verdict,
                _compare_member(old_member, new_member, member_path, changes),
            )

    return TypeComparison(
        old_name=old_name,
        new_name=new_name,
        verdict=verdict,
        changes=tuple(changes),
        reason=_comparison_reason(verdict),
    )


def _comparison_reason(verdict):
    return {
        ChangeKind.UNCHANGED: "no interpretation change",
        ChangeKind.RENAME_ONLY: "display names changed; bytes are unchanged",
        ChangeKind.LAYOUT_COMPATIBLE: (
            "physical slots are unchanged but field meaning changed"
        ),
        ChangeKind.REREAD_REQUIRED: (
            "offsets, bit packing, array extents, or sizes changed"
        ),
        ChangeKind.UNDETERMINED: "a required template is missing or incomplete",
    }[verdict]


def compare_type_definitions(
    old_definition, new_definition, old_name=None, new_name=None
):
    """Convenience wrapper for raw pycomm3 type definition dicts."""

    old_layout = build_type_layout(old_definition, name=old_name)
    new_layout = build_type_layout(new_definition, name=new_name)
    return compare_type_layouts(old_layout, new_layout)


class DependencyLink(NamedTuple):
    """One type in a tag's dependency chain."""

    path: str
    type_name: Optional[str]
    new_type_name: Optional[str]
    array: int
    verdict: ChangeKind
    missing: bool = False

    def __str__(self):
        old = self.type_name or "<missing>"
        new = self.new_type_name or "<missing>"
        label = old if old == new else f"{old} -> {new}"
        if self.array:
            label = f"{label}[{self.array}]"
        return f"{self.path}: {label} [{self.verdict.value}]"


class TagImpact(NamedTuple):
    tag_name: str
    verdict: ChangeKind
    chain: Tuple[DependencyLink, ...]
    changes: Tuple[MemberChange, ...]
    reason: str = ""

    @property
    def affected(self):
        return self.verdict is not ChangeKind.UNCHANGED

    @property
    def dependency_chain(self):
        """
        Flattened old-side type chain, e.g. ``('Recipe', 'Ingredient[8]', 'Lot')``.
        """

        return tuple(
            f"{link.type_name}[{link.array}]" if link.array else link.type_name
            for link in self.chain
            if link.type_name is not None
        )

    def describe_chain(self):
        return " -> ".join(
            f"{link.type_name or '<missing>'}"
            + (f"[{link.array}]" if link.array else "")
            for link in self.chain
        )

    def to_dict(self):
        return {
            "tag_name": self.tag_name,
            "verdict": self.verdict.value,
            "reason": self.reason,
            "dependency_chain": list(self.dependency_chain),
            "chain": [
                {
                    "path": link.path,
                    "type_name": link.type_name,
                    "new_type_name": link.new_type_name,
                    "array": link.array,
                    "verdict": link.verdict.value,
                    "missing": link.missing,
                }
                for link in self.chain
            ],
            "changes": [_change_to_dict(c) for c in self.changes],
        }


class CatalogReport(NamedTuple):
    device_id: str
    old_version: str
    new_version: str
    tags: Tuple[TagImpact, ...]

    @property
    def affected_tags(self):
        return tuple(tag for tag in self.tags if tag.affected)

    @property
    def undetermined_tags(self):
        return tuple(
            tag for tag in self.tags if tag.verdict is ChangeKind.UNDETERMINED
        )

    @property
    def reread_tags(self):
        return tuple(
            tag for tag in self.tags
            if tag.verdict is ChangeKind.REREAD_REQUIRED
        )

    def by_verdict(self, verdict):
        return tuple(tag for tag in self.tags if tag.verdict is verdict)

    def summary(self):
        counts = {kind: 0 for kind in ChangeKind}
        for tag in self.tags:
            counts[tag.verdict] += 1
        return {kind.value: count for kind, count in counts.items()}

    def to_dict(self):
        return {
            "device_id": self.device_id,
            "old_version": self.old_version,
            "new_version": self.new_version,
            "summary": self.summary(),
            "tags": [tag.to_dict() for tag in self.tags],
            "affected_tags": [tag.tag_name for tag in self.affected_tags],
        }


def _iter_tags(tags):
    if tags is None:
        return []
    if isinstance(tags, Mapping):
        return list(tags.values())
    return list(tags)


def _tag_map(tags):
    return {tag.get("tag_name"): tag for tag in _iter_tags(tags)}


def _tag_dimensions(tag):
    dimensions = tag.get("dimensions") or []
    dim = _as_int(tag.get("dim"), 0)
    return tuple(_as_int(value) for value in dimensions[:dim])


def _layout_from_tag(tag, catalog=None):
    """Resolve a tag's root type layout, using the tag's inline definition."""

    if not isinstance(tag, dict) or tag.get("tag_type") != "struct":
        return None
    type_name = tag.get("data_type_name")
    if catalog is not None:
        try:
            return catalog.get_layout(type_name)
        except MissingTemplateError:
            pass
    definition = tag.get("data_type")
    if isinstance(definition, dict):
        return build_type_layout(definition, name=type_name)
    return None


def _find_new_member(old_member, new_layout):
    if new_layout is None:
        return None

    exact = new_layout.member(old_member.name)
    if exact is not None:
        return exact

    # A rename may be accompanied by a type change.  Match the physical slot
    # so the dependency chain can still show the corresponding nested type.
    candidates = [
        candidate
        for candidate in new_layout.members
        if candidate.structural_key == old_member.structural_key
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _link_verdict(old_layout, new_layout, old_name, new_name, path):
    if not isinstance(old_layout, TypeLayout) or not isinstance(new_layout, TypeLayout):
        return ChangeKind.UNDETERMINED
    return compare_type_layouts(
        old_layout,
        new_layout,
        path=path,
        old_name=old_name,
        new_name=new_name,
    ).verdict


def _append_chain_links(links, tag_name, old_layout, new_layout):
    """
    Walk old and new layouts in parallel and append one link per nested struct.

    This is recursive on purpose: an array of UDTs can contain another UDT,
    and a change in the innermost type still has to appear in the tag's chain.
    """

    if old_layout is None:
        return

    for old_member in old_layout.members:
        if old_member.nested is None:
            continue

        path = _member_path(tag_name, old_member)
        new_member = _find_new_member(old_member, new_layout)
        new_nested = new_member.nested if new_member is not None else None
        new_type_name = new_member.type_name if new_member is not None else None

        if new_member is None:
            verdict = ChangeKind.UNDETERMINED
            missing = True
        elif new_nested is None:
            # The field became atomic.  The old nested template is no longer
            # part of the new interpretation, but nothing is missing.
            verdict = ChangeKind.LAYOUT_COMPATIBLE
            missing = False
        else:
            verdict = _link_verdict(
                old_member.nested,
                new_nested,
                old_member.type_name,
                new_type_name,
                path,
            )
            missing = not new_nested.complete or not old_member.nested.complete

        links.append(
            DependencyLink(
                path=path,
                type_name=old_member.type_name,
                new_type_name=new_type_name,
                array=old_member.array,
                verdict=verdict,
                missing=missing,
            )
        )

        _append_chain_links(
            links,
            path,
            old_member.nested,
            new_nested,
        )


def _build_chain(
    tag_name,
    old_layout,
    new_layout,
    old_catalog,
    new_catalog,
    root_old_name,
    root_new_name,
    root_verdict,
):
    links = [
        DependencyLink(
            path=tag_name,
            type_name=root_old_name,
            new_type_name=root_new_name,
            array=0,
            verdict=root_verdict,
            missing=old_layout is None or new_layout is None,
        )
    ]

    _append_chain_links(links, tag_name, old_layout, new_layout)
    return tuple(links)


def compare_catalogs(old_catalog, new_catalog, old_tags, new_tags=None):
    """
    Compare every old tag against a newer type catalog.

    ``new_tags`` is optional.  When supplied, the tag's new root type name and
    its inline definitions are used.  When omitted, the old tag's type name is
    looked up in the new catalog by name.  In both cases, a missing type stays
    undetermined; it is never resolved from another device.
    """

    if old_catalog.device_id != new_catalog.device_id:
        raise DeviceMismatchError(old_catalog.device_id, new_catalog.device_id)

    old_map = _tag_map(old_tags)
    new_map = _tag_map(new_tags) if new_tags is not None else {}

    impacts = []
    for tag_name in sorted(old_map):
        old_tag = old_map[tag_name]
        new_tag = new_map.get(tag_name, old_tag)

        if old_tag.get("tag_type") != "struct":
            old_type = old_tag.get("data_type_name")
            new_type = (
                new_tag.get("data_type_name")
                if new_tag is not old_tag
                else old_type
            )
            changes = []
            if old_type == new_type:
                verdict = ChangeKind.UNCHANGED
                reason = _comparison_reason(verdict)
            else:
                old_size = _atomic_size(old_type)
                new_size = _atomic_size(new_type)
                if old_size is None or new_size is None:
                    verdict = ChangeKind.UNDETERMINED
                    reason = "atomic type size unavailable"
                elif old_size != new_size:
                    verdict = ChangeKind.REREAD_REQUIRED
                    reason = "atomic tag width changed"
                else:
                    verdict = ChangeKind.LAYOUT_COMPATIBLE
                    reason = "atomic tag reinterpreted at same width"
                changes.append(
                    MemberChange(
                        path=tag_name,
                        kind=verdict,
                        old=old_type,
                        new=new_type,
                        detail=reason,
                    )
                )
            impacts.append(
                TagImpact(
                    tag_name=tag_name,
                    verdict=verdict,
                    chain=(
                        DependencyLink(
                            path=tag_name,
                            type_name=old_type,
                            new_type_name=new_type,
                            array=0,
                            verdict=verdict,
                        ),
                    ),
                    changes=tuple(changes),
                    reason=reason,
                )
            )
            continue

        root_old_name = old_tag.get("data_type_name")
        root_new_name = new_tag.get("data_type_name") or root_old_name

        tag_layout_changed = False
        if new_tag is not old_tag:
            old_dim = _tag_dimensions(old_tag)
            new_dim = _tag_dimensions(new_tag)
            if old_dim != new_dim:
                tag_layout_changed = True

        old_layout = _layout_from_tag(old_tag, old_catalog)

        if new_tag is old_tag:
            # No new tag definition was provided.  Do NOT reuse the old inline
            # definition as if it belonged to the new catalog version.
            try:
                new_layout = new_catalog.get_layout(root_new_name)
            except MissingTemplateError:
                new_layout = None
        else:
            new_layout = _layout_from_tag(new_tag, new_catalog)

        if old_layout is None and not old_catalog.has_type(root_old_name):
            comparison = TypeComparison(
                root_old_name or "",
                root_new_name or "",
                ChangeKind.UNDETERMINED,
                (),
                "old template missing",
            )
        elif new_layout is None:
            comparison = TypeComparison(
                root_old_name or "",
                root_new_name or "",
                ChangeKind.UNDETERMINED,
                (
                    MemberChange(
                        path=tag_name,
                        kind=ChangeKind.UNDETERMINED,
                        old=root_old_name,
                        new=root_new_name,
                        detail="new template missing for this exact device and version",
                    ),
                ),
                "new template missing",
            )
        else:
            comparison = compare_type_layouts(
                old_layout,
                new_layout,
                path=tag_name,
                old_name=root_old_name,
                new_name=root_new_name,
            )

        if tag_layout_changed:
            extra = MemberChange(
                path=tag_name,
                kind=ChangeKind.REREAD_REQUIRED,
                old=str(_tag_dimensions(old_tag)),
                new=str(_tag_dimensions(new_tag)),
                detail="tag array dimensions changed",
            )
            comparison = TypeComparison(
                old_name=comparison.old_name,
                new_name=comparison.new_name,
                verdict=ChangeKind.REREAD_REQUIRED,
                changes=tuple(comparison.changes) + (extra,),
                reason="tag array dimensions changed",
            )

        chain = _build_chain(
            tag_name,
            old_layout,
            new_layout,
            old_catalog,
            new_catalog,
            root_old_name,
            root_new_name,
            comparison.verdict,
        )

        impacts.append(
            TagImpact(
                tag_name=tag_name,
                verdict=comparison.verdict,
                chain=chain,
                changes=comparison.changes,
                reason=comparison.reason,
            )
        )

    return CatalogReport(
        device_id=old_catalog.device_id,
        old_version=old_catalog.version,
        new_version=new_catalog.version,
        tags=tuple(impacts),
    )


def dependency_chain(tag, catalog):
    """
    Return the type dependency chain for one tag definition.

    The first item is the tag's root type; nested struct types and array element
    types follow in traversal order.
    """

    layout = _layout_from_tag(tag, catalog)
    if layout is None:
        return tuple()

    links = []
    _append_chain_links(links, tag.get("tag_name", ""), layout, layout)
    chain = [layout.name]
    for link in links:
        if link.type_name is None:
            continue
        label = (
            f"{link.type_name}[{link.array}]"
            if link.array
            else link.type_name
        )
        chain.append(label)
    return tuple(chain)


class Acquisition:
    """
    Bytes and interpretation captured under one exact catalog version.

    The record is a snapshot: values are deep-copied on capture and the original
    ``catalog_version`` is retained.  It is never updated in place when the
    controller later downloads a new UDT.  Use :meth:`reinterpret` to create a
    separate acquisition explicitly.
    """

    __slots__ = (
        "tag_name",
        "device_id",
        "catalog_version",
        "type_name",
        "raw",
        "_values",
    )

    def __init__(
        self,
        tag_name,
        device_id,
        catalog_version,
        type_name,
        raw,
        values,
    ):
        self.tag_name = str(tag_name)
        self.device_id = str(device_id)
        self.catalog_version = str(catalog_version)
        self.type_name = str(type_name)
        self.raw = bytes(raw)
        self._values = copy.deepcopy(values)

    @property
    def values(self):
        """Archived interpreted values; a defensive copy on every access."""

        return copy.deepcopy(self._values)

    @classmethod
    def capture(cls, tag_name, device_id, catalog_version, type_name, raw, values):
        return cls(
            tag_name=tag_name,
            device_id=device_id,
            catalog_version=catalog_version,
            type_name=type_name,
            raw=raw,
            values=values,
        )

    def reinterpret(self, catalog_version, values):
        """
        Return a new acquisition with the same raw bytes under a new version.

        The original object, including its ``values``, is untouched.  A
        reinterpretation is always an explicit new record; changing catalogs
        never rewrites an existing acquisition in place.
        """

        return Acquisition(
            tag_name=self.tag_name,
            device_id=self.device_id,
            catalog_version=str(catalog_version),
            type_name=self.type_name,
            raw=self.raw,
            values=values,
        )

    def _key(self):
        return (
            self.tag_name,
            self.device_id,
            self.catalog_version,
            self.type_name,
            self.raw,
        )

    def __eq__(self, other):
        if not isinstance(other, Acquisition):
            return NotImplemented
        return self._key() == other._key() and self._values == other._values

    def __hash__(self):
        return hash(self._key())

    def __repr__(self):
        return (
            f"Acquisition(tag_name={self.tag_name!r}, "
            f"device_id={self.device_id!r}, "
            f"catalog_version={self.catalog_version!r}, "
            f"type_name={self.type_name!r}, raw={self.raw!r}, "
            f"values={self._values!r})"
        )

    def to_dict(self):
        return {
            "tag_name": self.tag_name,
            "device_id": self.device_id,
            "catalog_version": self.catalog_version,
            "type_name": self.type_name,
            "raw": self.raw,
            "values": self.values,
        }


def _synthetic_tag(acquisition, catalog):
    """Build the minimal tag definition needed to assess an acquisition."""

    try:
        layout = catalog.get_layout(acquisition.type_name)
    except MissingTemplateError:
        layout = None

    if layout is not None:
        return {
            "tag_name": acquisition.tag_name,
            "tag_type": "struct",
            "data_type_name": acquisition.type_name,
            "data_type": None,
            "dim": 0,
            "dimensions": [0, 0, 0],
        }

    return {
        "tag_name": acquisition.tag_name,
        "tag_type": "atomic",
        "data_type_name": acquisition.type_name,
        "dim": 0,
        "dimensions": [0, 0, 0],
    }


class ArchiveAssessment(NamedTuple):
    """Result of assessing all stored acquisitions against a new catalog."""

    device_id: str
    old_version: str
    new_version: str
    report: CatalogReport
    acquisitions: Tuple[Acquisition, ...]

    def impact(self, tag_name):
        for impact in self.report.tags:
            if impact.tag_name == tag_name:
                return impact
        return None

    @property
    def affected_acquisitions(self):
        affected = {impact.tag_name for impact in self.report.affected_tags}
        return tuple(
            acquisition
            for acquisition in self.acquisitions
            if acquisition.tag_name in affected
        )

    @property
    def reread_required(self):
        tags = {impact.tag_name for impact in self.report.reread_tags}
        return tuple(
            acquisition
            for acquisition in self.acquisitions
            if acquisition.tag_name in tags
        )

    @property
    def undetermined(self):
        tags = {impact.tag_name for impact in self.report.undetermined_tags}
        return tuple(
            acquisition
            for acquisition in self.acquisitions
            if acquisition.tag_name in tags
        )

    def summary(self):
        return self.report.summary()

    def to_dict(self):
        return {
            "device_id": self.device_id,
            "old_version": self.old_version,
            "new_version": self.new_version,
            "summary": self.summary(),
            "report": self.report.to_dict(),
            "acquisitions": [
                acquisition.to_dict() for acquisition in self.acquisitions
            ],
        }


class AcquisitionArchive:
    """
    Version-scoped store for UDT catalogs, tag definitions, and raw acquisitions.

    The archive never silently reinterprets an existing record.  Calling
    :meth:`add_catalog` for a newer version only changes what catalog is
    available; records keep their original ``catalog_version``.  Version
    switching is an explicit :meth:`reinterpret` call, which writes a separate
    record for the new version.
    """

    def __init__(self, device_id):
        self.device_id = str(device_id)
        self._catalogs = {}
        self._tags = {}
        self._acquisitions = {}

    def add_catalog(self, version, catalog=None, data_types=None, tags=None):
        version = str(version)
        if catalog is None:
            catalog = UDTCatalog(self.device_id, version, data_types)
        if catalog.device_id != self.device_id:
            raise DeviceMismatchError(self.device_id, catalog.device_id)

        self._catalogs[version] = catalog
        if tags is not None:
            self._tags[version] = tuple(_tag_map(tags).values())
        return catalog

    def add_snapshot(self, version, driver, tags=True):
        version = str(version)
        catalog, tag_list = snapshot_driver(driver, self.device_id, version)
        self._catalogs[version] = catalog
        if tags:
            self._tags[version] = tuple(tag_list)
        return catalog

    def get_catalog(self, version):
        version = str(version)
        try:
            return self._catalogs[version]
        except KeyError:
            raise MissingCatalogError(self.device_id, version)

    def record(self, tag_name, version, raw, values, type_name=None):
        version = str(version)
        self.get_catalog(version)
        if type_name is None:
            for tag in self._tags.get(version, ()):
                if tag.get("tag_name") == tag_name:
                    type_name = tag.get("data_type_name")
                    break
        if type_name is None:
            raise MissingTemplateError(
                self.device_id,
                version,
                tag_name,
                detail="type name not supplied and tag not in version catalog",
            )

        acquisition = Acquisition.capture(
            tag_name=tag_name,
            device_id=self.device_id,
            catalog_version=version,
            type_name=type_name,
            raw=raw,
            values=values,
        )
        self._acquisitions[(version, str(tag_name))] = acquisition
        return acquisition

    def get(self, tag_name, version):
        key = (str(version), str(tag_name))
        try:
            return self._acquisitions[key]
        except KeyError:
            raise KeyError(
                f"No acquisition for tag {tag_name!r} in version {version!r}"
            )

    def acquisitions(self, version=None):
        if version is None:
            return tuple(self._acquisitions.values())
        version = str(version)
        return tuple(
            acquisition
            for (acq_version, _tag), acquisition in sorted(
                self._acquisitions.items()
            )
            if acq_version == version
        )

    def _tags_for(self, version):
        explicit = self._tags.get(str(version))
        if explicit:
            return explicit
        catalog = self.get_catalog(version)
        return tuple(
            _synthetic_tag(acquisition, catalog)
            for acquisition in self.acquisitions(version)
        )

    def compare(self, old_version, new_version):
        """Compare stored tag/type definitions between two versions."""

        old_catalog = self.get_catalog(old_version)
        new_catalog = self.get_catalog(new_version)
        old_tags = self._tags_for(old_version)
        new_tags = self._tags.get(str(new_version))

        return compare_catalogs(old_catalog, new_catalog, old_tags, new_tags)

    def assess(self, old_version, new_version):
        """
        Assess all acquisitions from ``old_version`` against ``new_version``.

        The returned assessment is read-only.  No acquisition is modified and
        no field value is recalculated automatically.
        """

        report = self.compare(old_version, new_version)
        return ArchiveAssessment(
            device_id=self.device_id,
            old_version=str(old_version),
            new_version=str(new_version),
            report=report,
            acquisitions=self.acquisitions(old_version),
        )

    def reinterpret(self, tag_name, old_version, new_version, values):
        """
        Create a new acquisition under ``new_version`` explicitly.

        The original acquisition is left untouched.  The raw bytes are copied
        from the original, since the controller's archived bytes are the only
        authoritative source for the historical reading.
        """

        original = self.get(tag_name, old_version)
        self.get_catalog(new_version)
        reinterpreted = original.reinterpret(new_version, values)
        self._acquisitions[(str(new_version), str(tag_name))] = reinterpreted
        return reinterpreted

    def switch_version(self, tag_name, old_version, new_version, values):
        """Alias for :meth:`reinterpret`; never mutates the old record."""

        return self.reinterpret(tag_name, old_version, new_version, values)

    @property
    def catalogs(self):
        return MappingProxyType(dict(self._catalogs))

    @property
    def tags(self):
        return MappingProxyType(dict(self._tags))

    @property
    def acquisitions_by_version(self):
        versions = {version for version, _tag in self._acquisitions}
        return MappingProxyType(
            {version: self.acquisitions(version) for version in versions}
        )


AcquisitionCatalog = AcquisitionArchive


def snapshot_driver(driver, device_id, version):
    """
    Snapshot both the type table and tag list from a connected driver.

    The catalog is detached from the driver's mutable caches; later calls to
    ``driver.get_tag_list()`` will not change the snapshotted layouts.
    """

    data_types = snapshot_types(getattr(driver, "data_types", {}) or {})
    raw_tags = getattr(driver, "tags", {}) or {}
    tags = copy.deepcopy(raw_tags)
    catalog = UDTCatalog(device_id, version, data_types.values())
    for tag_definition in _iter_tags(tags):
        if tag_definition.get("tag_type") == "struct":
            catalog.add_definition(tag_definition.get("data_type"))
    return catalog, tuple(tags.values()) if isinstance(tags, Mapping) else tuple(tags)


def catalog_from_driver(driver, device_id, version):
    """Snapshot only the type catalog from a connected driver."""

    catalog, _tags = snapshot_driver(driver, device_id, version)
    return catalog


compare_udt_versions = compare_catalogs
compare_tag_catalogs = compare_catalogs
