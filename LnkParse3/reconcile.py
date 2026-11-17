"""Reconcile shortcut catalogs collected from several machines.

Every machine declares its own volumes and shortcut targets.  A volume label,
a drive letter or an equal path is evidence belonging to that declaration
only; none of them establishes that two volumes or two targets are the same
entity.  Identity comes from the supplier's declarations:

* candidate volume mappings -- pairs that *may* be the same entity,
* verified objects -- sets of shortcuts already confirmed to be one object,
* distinct volumes / distinct targets -- pairs known to be different.

The reconciler enumerates the maximal, jointly feasible identity assignments
and reports only the relations that hold in every one of them.  Independent
constraint components are solved separately, so an unsatisfiable component
does not hide results from the others.

All relations retain each machine's own volume declaration and original path.
No synthetic drive letter or universal path is produced.
"""

import argparse
import itertools
import json
import re
import sys
from dataclasses import dataclass
from dataclasses import field
from hashlib import sha1

from LnkParse3.lnk_file import LnkFile


__all__ = [
    "CandidateMapping",
    "Catalog",
    "Conflict",
    "DistinctTargets",
    "DistinctVolumes",
    "GroupResult",
    "Plan",
    "ReconciliationLimitError",
    "Relation",
    "Report",
    "Shortcut",
    "VerifiedObject",
    "Volume",
    "reconcile",
]

_DRIVE_RE = re.compile(r"^([A-Za-z]):")


def _text(value):
    if value is None:
        return ""
    return str(value)


def _short_hash(parts):
    digest = sha1("\x00".join(parts).encode("utf-8", "surrogatepass")).hexdigest()
    return digest[:10]


def _path_key(path, drive=""):
    """Return a comparison key for a path relative to its volume.

    Comparison ignores case and resolves ``.``/``..`` lexically, as Windows
    paths do.  The original path is never modified or used in output.
    """
    path = _text(path).replace("/", "\\").strip()
    if not path:
        return ""
    path = path.removeprefix("\\\\?\\")
    drive = _text(drive).rstrip("\\")
    if drive:
        drive = drive.rstrip(":")[:1]
    else:
        match = _DRIVE_RE.match(path)
        drive = match.group(1) if match else ""
    if drive and path[:1].upper() == drive.upper() and path[1:2] == ":":
        path = path[2:]
    path = path.lstrip("\\")
    parts = []
    for part in path.split("\\"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts and parts[-1] != "..":
                parts.pop()
            else:
                parts.append(part)
        else:
            parts.append(part)
    return "\\".join(parts).casefold()


def _volume_sort_key(volume):
    return (volume.machine.casefold(), volume.id.casefold())


def _shortcut_sort_key(shortcut):
    return (shortcut.volume.machine.casefold(), shortcut.id.casefold())


@dataclass(frozen=True)
class Volume:
    """A volume as declared by one machine.

    ``label``, ``drive`` and ``declared`` are that machine's own evidence.
    They are reported verbatim and never used to infer identity.
    """

    machine: str
    id: str
    label: str = ""
    drive: str = ""
    declared: dict = field(default_factory=dict, compare=False, hash=False)

    def __post_init__(self):
        if not self.machine:
            raise ValueError("volume machine must not be empty")
        if not self.id:
            raise ValueError("volume id must not be empty")
        object.__setattr__(self, "label", _text(self.label))
        object.__setattr__(self, "drive", _text(self.drive))
        object.__setattr__(self, "declared", dict(self.declared or {}))

    @property
    def key(self):
        return (self.machine, self.id)

    def descriptor(self):
        data = {
            "machine": self.machine,
            "volume": self.id,
            "label": self.label,
            "drive": self.drive,
        }
        if self.declared:
            data["declared"] = dict(self.declared)
        return data


@dataclass(frozen=True)
class Shortcut:
    """A shortcut target as declared by one machine."""

    id: str
    volume: Volume
    path: str
    volume_path: str = ""
    declared: dict = field(default_factory=dict, compare=False, hash=False)

    def __post_init__(self):
        if not self.id:
            raise ValueError("shortcut id must not be empty")
        object.__setattr__(self, "path", _text(self.path))
        object.__setattr__(self, "volume_path", _text(self.volume_path))
        object.__setattr__(self, "declared", dict(self.declared or {}))

    def target_path_key(self):
        source = self.volume_path or self.path
        return _path_key(source, self.volume.drive)

    def descriptor(self):
        data = {
            "machine": self.volume.machine,
            "shortcut": self.id,
            "volume": self.volume.id,
            "path": self.path,
        }
        if self.volume_path:
            data["volume_path"] = self.volume_path
        if self.declared:
            data["declared"] = dict(self.declared)
        return data


@dataclass(frozen=True)
class CandidateMapping:
    """Two volumes the supplier allows to be the same entity."""

    id: str
    a: Volume
    b: Volume
    note: str = ""

    def descriptor(self):
        return {
            "type": "candidate_volume_mapping",
            "id": self.id,
            "a": self.a.descriptor(),
            "b": self.b.descriptor(),
            "note": self.note,
        }


@dataclass(frozen=True)
class VerifiedObject:
    """Shortcuts already confirmed to refer to the same object."""

    id: str
    members: tuple
    note: str = ""

    def descriptor(self):
        return {
            "type": "verified_object",
            "id": self.id,
            "members": [member.descriptor() for member in self.members],
            "note": self.note,
        }


@dataclass(frozen=True)
class DistinctVolumes:
    """Two volume declarations known not to be the same entity."""

    id: str
    a: Volume
    b: Volume
    note: str = ""

    def descriptor(self):
        return {
            "type": "distinct_volumes",
            "id": self.id,
            "a": self.a.descriptor(),
            "b": self.b.descriptor(),
            "note": self.note,
        }


@dataclass(frozen=True)
class DistinctTargets:
    """Two shortcuts known not to refer to the same object."""

    id: str
    a: Shortcut
    b: Shortcut
    note: str = ""

    def descriptor(self):
        return {
            "type": "distinct_targets",
            "id": self.id,
            "a": self.a.descriptor(),
            "b": self.b.descriptor(),
            "note": self.note,
        }


class Catalog:
    """Shortcut records and supplier declarations for several machines."""

    def __init__(
        self,
        volumes,
        shortcuts,
        candidate_mappings=(),
        verified_objects=(),
        distinct_volumes=(),
        distinct_targets=(),
    ):
        self.volumes = {}
        for volume in volumes:
            if volume.key in self.volumes:
                raise ValueError(f"duplicate volume {volume.machine}:{volume.id}")
            self.volumes[volume.key] = volume

        self.shortcuts = {}
        for shortcut in shortcuts:
            if shortcut.id in self.shortcuts:
                raise ValueError(f"duplicate shortcut id {shortcut.id!r}")
            if shortcut.volume.key not in self.volumes:
                raise ValueError(
                    f"shortcut {shortcut.id!r} refers to unknown volume "
                    f"{shortcut.volume.machine}:{shortcut.volume.id}"
                )
            self.shortcuts[shortcut.id] = shortcut

        self.candidate_mappings = list(candidate_mappings)
        self.verified_objects = list(verified_objects)
        self.distinct_volumes = list(distinct_volumes)
        self.distinct_targets = list(distinct_targets)
        self._validate()

    def _require_volume(self, volume, owner):
        if volume.key not in self.volumes:
            raise ValueError(f"{owner!r} refers to unknown volume {volume.machine}:{volume.id}")

    def _require_shortcut(self, shortcut, owner):
        if shortcut.id not in self.shortcuts:
            raise ValueError(f"{owner!r} refers to unknown shortcut {shortcut.id!r}")

    def _validate(self):
        for item in self.candidate_mappings:
            self._require_volume(item.a, f"candidate {item.id}")
            self._require_volume(item.b, f"candidate {item.id}")
        for item in self.distinct_volumes:
            self._require_volume(item.a, f"constraint {item.id}")
            self._require_volume(item.b, f"constraint {item.id}")
        for item in self.verified_objects:
            if len(item.members) < 2:
                raise ValueError(f"verified object {item.id!r} needs at least two members")
            for shortcut in item.members:
                self._require_shortcut(shortcut, f"verified object {item.id}")
        for item in self.distinct_targets:
            self._require_shortcut(item.a, f"constraint {item.id}")
            self._require_shortcut(item.b, f"constraint {item.id}")

    @classmethod
    def from_dict(cls, data):
        """Build a catalog from the JSON form documented in ``README.md``."""
        volume_by_key = {}
        volume_by_name = {}
        ambiguous_names = set()
        machines = {}

        def add_volume(volume):
            if volume.key in volume_by_key:
                raise ValueError(f"duplicate volume {volume.machine}:{volume.id}")
            volume_by_key[volume.key] = volume
            volume_by_name[(volume.machine, volume.id)] = volume
            if volume.id in volume_by_name:
                ambiguous_names.add(volume.id)
            else:
                volume_by_name[volume.id] = volume

        for machine in data.get("machines") or []:
            machine_id = _text(machine.get("id"))
            if not machine_id:
                raise ValueError("machine id must not be empty")
            if machine_id in machines:
                raise ValueError(f"duplicate machine {machine_id!r}")
            machines[machine_id] = machine
            for item in machine.get("volumes") or []:
                add_volume(
                    Volume(
                        machine=machine_id,
                        id=_text(item.get("id")),
                        label=item.get("label", ""),
                        drive=item.get("drive", ""),
                        declared=item.get("declared") or {},
                    )
                )

        for item in data.get("volumes") or []:
            add_volume(
                Volume(
                    machine=_text(item.get("machine")),
                    id=_text(item.get("id")),
                    label=item.get("label", ""),
                    drive=item.get("drive", ""),
                    declared=item.get("declared") or {},
                )
            )

        shortcuts = []
        shortcut_ids = set()

        def add_shortcut(item, machine_id=""):
            shortcut_id = _text(item.get("id"))
            if not shortcut_id:
                shortcut_id = f"shortcut-{len(shortcuts) + 1}"
            if shortcut_id in shortcut_ids:
                raise ValueError(f"duplicate shortcut id {shortcut_id!r}")
            shortcut_ids.add(shortcut_id)
            machine = _text(item.get("machine")) or machine_id
            volume_id = _text(item.get("volume"))
            volume = volume_by_key.get((machine, volume_id))
            if volume is None and not machine:
                if volume_id in ambiguous_names:
                    raise ValueError(
                        f"shortcut {shortcut_id!r} names volume {volume_id!r} without a "
                        "machine, and several machines declare that volume id"
                    )
                volume = volume_by_name.get(volume_id)
            if volume is None:
                raise ValueError(
                    f"shortcut {shortcut_id!r} refers to unknown volume {machine}:{volume_id}"
                )
            if machine and volume.machine != machine:
                raise ValueError(
                    f"shortcut {shortcut_id!r} declares machine {machine!r} but volume "
                    f"belongs to {volume.machine!r}"
                )
            shortcuts.append(
                Shortcut(
                    id=shortcut_id,
                    volume=volume,
                    path=item.get("path", ""),
                    volume_path=item.get("volume_path", ""),
                    declared=item.get("declared") or {},
                )
            )

        for machine_id, machine in machines.items():
            for item in machine.get("shortcuts") or []:
                add_shortcut(item, machine_id)
        for item in data.get("shortcuts") or []:
            add_shortcut(item)

        by_shortcut = {shortcut.id: shortcut for shortcut in shortcuts}

        def volume_ref(item, constraint_id, role):
            if isinstance(item, str):
                if ":" in item:
                    machine, volume_id = item.split(":", 1)
                    volume = volume_by_key.get((machine, volume_id))
                else:
                    volume = volume_by_name.get(item)
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                volume = volume_by_key.get((_text(item[0]), _text(item[1])))
            elif isinstance(item, dict):
                volume = volume_by_key.get((_text(item.get("machine")), _text(item.get("volume"))))
            else:
                volume = None
            if volume is None:
                raise ValueError(f"{constraint_id!r} has unknown {role} volume reference {item!r}")
            return volume

        def shortcut_ref(item, constraint_id, role):
            if isinstance(item, str):
                shortcut = by_shortcut.get(item)
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                shortcut = by_shortcut.get(_text(item[1]))
            elif isinstance(item, dict):
                shortcut = by_shortcut.get(_text(item.get("shortcut")))
            else:
                shortcut = None
            if shortcut is None:
                raise ValueError(
                    f"{constraint_id!r} has unknown {role} shortcut reference {item!r}"
                )
            return shortcut

        candidates = []
        for index, item in enumerate(data.get("candidate_volume_mappings") or [], 1):
            constraint_id = _text(item.get("id")) or f"candidate-{index}"
            candidates.append(
                CandidateMapping(
                    id=constraint_id,
                    a=volume_ref(item.get("a"), constraint_id, "first"),
                    b=volume_ref(item.get("b"), constraint_id, "second"),
                    note=_text(item.get("note")),
                )
            )

        verified = []
        for index, item in enumerate(data.get("verified_objects") or [], 1):
            object_id = _text(item.get("id")) or f"object-{index}"
            members = tuple(
                shortcut_ref(member, object_id, "member") for member in item.get("members") or []
            )
            if len(members) < 2:
                raise ValueError(f"verified object {object_id!r} needs at least two members")
            verified.append(
                VerifiedObject(id=object_id, members=members, note=_text(item.get("note")))
            )

        distinct_volumes = []
        for index, item in enumerate(data.get("distinct_volumes") or [], 1):
            constraint_id = _text(item.get("id")) or f"distinct-volume-{index}"
            distinct_volumes.append(
                DistinctVolumes(
                    id=constraint_id,
                    a=volume_ref(item.get("a"), constraint_id, "first"),
                    b=volume_ref(item.get("b"), constraint_id, "second"),
                    note=_text(item.get("note")),
                )
            )

        distinct_targets = []
        for index, item in enumerate(data.get("distinct_targets") or [], 1):
            constraint_id = _text(item.get("id")) or f"distinct-target-{index}"
            distinct_targets.append(
                DistinctTargets(
                    id=constraint_id,
                    a=shortcut_ref(item.get("a"), constraint_id, "first"),
                    b=shortcut_ref(item.get("b"), constraint_id, "second"),
                    note=_text(item.get("note")),
                )
            )

        return cls(
            volumes=volume_by_key.values(),
            shortcuts=shortcuts,
            candidate_mappings=candidates,
            verified_objects=verified,
            distinct_volumes=distinct_volumes,
            distinct_targets=distinct_targets,
        )

    @classmethod
    def from_lnk_records(cls, records, **declarations):
        """Build a catalog from parsed LNK JSON records.

        ``records`` is a sequence of ``(machine, shortcut_id, json_record)``
        tuples.  Volume identity is deliberately *not* derived from labels,
        serials or DROID identifiers in the records; pass supplier
        declarations as keyword arguments.
        """
        volumes = {}
        shortcuts = []
        anonymous = {}
        for machine, shortcut_id, record in records:
            link_info = record.get("link_info") or {}
            location = link_info.get("location_info") or {}
            tracker = (record.get("extra") or {}).get("DISTRIBUTED_LINK_TRACKER_BLOCK") or {}
            path = _text(link_info.get("local_base_path"))
            suffix = _text(link_info.get("common_path_suffix"))
            if suffix and path and not path.endswith(suffix):
                path = path.rstrip("\\") + "\\" + suffix
            drive = ""
            match = _DRIVE_RE.match(path)
            if match:
                drive = match.group(1) + ":"
            volume_id = _text(location.get("drive_serial_number")) or drive
            if not volume_id:
                # No evidence ties this record to any other volume, so it
                # receives its own id rather than sharing a fallback name.
                anonymous[machine] = anonymous.get(machine, 0) + 1
                volume_id = f"unidentified-{anonymous[machine]}"
            volume_key = (machine, volume_id)
            if volume_key not in volumes:
                volumes[volume_key] = Volume(
                    machine=machine,
                    id=volume_id,
                    label=location.get("volume_label") or "",
                    drive=drive,
                    declared={
                        "drive_serial_number": location.get("drive_serial_number"),
                        "volume_label": location.get("volume_label"),
                        "droid_volume_identifier": tracker.get("droid_volume_identifier"),
                        "birth_droid_volume_identifier": tracker.get(
                            "birth_droid_volume_identifier"
                        ),
                    },
                )
            shortcuts.append(
                Shortcut(
                    id=shortcut_id,
                    volume=volumes[volume_key],
                    path=path,
                    declared={
                        "relative_path": (record.get("data") or {}).get("relative_path"),
                        "machine_identifier": tracker.get("machine_identifier"),
                    },
                )
            )
        return cls(volumes=volumes.values(), shortcuts=shortcuts, **declarations)

    @classmethod
    def from_lnk_files(cls, files, **declarations):
        """Build a catalog from ``(machine, shortcut_id, path)`` LNK files."""
        import base64

        records = []
        for machine, shortcut_id, path in files:
            with open(path, "rb") as handle:
                raw = handle.read()
            try:
                payload = base64.b64decode(b"".join(raw.split()), validate=True)
            except ValueError:
                payload = raw
            record = LnkFile(indata=payload).get_json()
            records.append((machine, shortcut_id, record))
        return cls.from_lnk_records(records, **declarations)

    def to_dict(self):
        machines = {}
        for volume in self.volumes.values():
            machines.setdefault(volume.machine, {"id": volume.machine, "volumes": []})
            machines[volume.machine]["volumes"].append(
                {
                    "id": volume.id,
                    "label": volume.label,
                    "drive": volume.drive,
                    "declared": dict(volume.declared),
                }
            )
        for shortcut in self.shortcuts.values():
            machine = machines.setdefault(
                shortcut.volume.machine,
                {"id": shortcut.volume.machine, "volumes": []},
            )
            machine.setdefault("shortcuts", []).append(
                {
                    "id": shortcut.id,
                    "volume": shortcut.volume.id,
                    "path": shortcut.path,
                    "volume_path": shortcut.volume_path,
                    "declared": dict(shortcut.declared),
                }
            )
        return {
            "machines": list(machines.values()),
            "candidate_volume_mappings": [
                {
                    "id": item.id,
                    "a": {"machine": item.a.machine, "volume": item.a.id},
                    "b": {"machine": item.b.machine, "volume": item.b.id},
                    "note": item.note,
                }
                for item in self.candidate_mappings
            ],
            "verified_objects": [
                {
                    "id": item.id,
                    "members": [shortcut.id for shortcut in item.members],
                    "note": item.note,
                }
                for item in self.verified_objects
            ],
            "distinct_volumes": [
                {
                    "id": item.id,
                    "a": {"machine": item.a.machine, "volume": item.a.id},
                    "b": {"machine": item.b.machine, "volume": item.b.id},
                    "note": item.note,
                }
                for item in self.distinct_volumes
            ],
            "distinct_targets": [
                {
                    "id": item.id,
                    "a": item.a.id,
                    "b": item.b.id,
                    "note": item.note,
                }
                for item in self.distinct_targets
            ],
        }


@dataclass(frozen=True)
class Conflict:
    kind: str
    message: str
    records: tuple = ()
    constraints: tuple = ()

    def descriptor(self):
        return {
            "kind": self.kind,
            "message": self.message,
            "records": [dict(item) for item in self.records],
            "constraints": [dict(item) for item in self.constraints],
        }


@dataclass(frozen=True)
class Relation:
    kind: str
    members: tuple

    def descriptor(self):
        return {"kind": self.kind, "members": [dict(item) for item in self.members]}


@dataclass(frozen=True)
class Plan:
    volume_entities: tuple
    target_entities: tuple
    mapping: tuple

    def descriptor(self):
        return {
            "volume_entities": [dict(item) for item in self.volume_entities],
            "target_entities": [dict(item) for item in self.target_entities],
            "mapping": [dict(item) for item in self.mapping],
        }


@dataclass(frozen=True)
class GroupResult:
    id: str
    volumes: tuple
    shortcuts: tuple
    plans: tuple = ()
    conflicts: tuple = ()
    confirmed_volume_relations: tuple = ()
    confirmed_target_relations: tuple = ()
    plan_count: int = 0
    plans_omitted: bool = False
    warnings: tuple = ()

    @property
    def status(self):
        return "unsatisfiable" if self.conflicts else "solved"

    def descriptor(self, include_plans=True):
        data = {
            "id": self.id,
            "status": self.status,
            "plan_count": self.plan_count,
            "plans_omitted": self.plans_omitted,
            "volumes": [volume.descriptor() for volume in self.volumes],
            "shortcuts": [shortcut.descriptor() for shortcut in self.shortcuts],
            "confirmed_volume_relations": [
                relation.descriptor() for relation in self.confirmed_volume_relations
            ],
            "confirmed_target_relations": [
                relation.descriptor() for relation in self.confirmed_target_relations
            ],
            "conflicts": [conflict.descriptor() for conflict in self.conflicts],
            "warnings": [dict(item) for item in self.warnings],
        }
        if include_plans:
            data["plans"] = [plan.descriptor() for plan in self.plans]
        return data


@dataclass(frozen=True)
class Report:
    groups: tuple
    complete: bool
    plan_count: int
    confirmed_volume_relations: tuple = ()
    confirmed_target_relations: tuple = ()
    warnings: tuple = ()

    @property
    def unsatisfiable_groups(self):
        return tuple(group for group in self.groups if group.conflicts)

    def group(self, group_id):
        for group in self.groups:
            if group.id == group_id:
                return group
        return None

    def descriptor(self, include_plans=True):
        return {
            "complete": self.complete,
            "plan_count": self.plan_count,
            "groups": [group.descriptor(include_plans) for group in self.groups],
            "confirmed_volume_relations": [
                relation.descriptor() for relation in self.confirmed_volume_relations
            ],
            "confirmed_target_relations": [
                relation.descriptor() for relation in self.confirmed_target_relations
            ],
            "warnings": [dict(item) for item in self.warnings],
        }

    def to_json(self, include_plans=True):
        return json.dumps(self.descriptor(include_plans), ensure_ascii=False, indent=2)


class ReconciliationLimitError(RuntimeError):
    """Raised when the search exceeds its configured state or plan limit."""


class _DSU:
    def __init__(self, items):
        self.parent = {item: item for item in items}

    def find(self, item):
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a, b):
        root_a = self.find(a)
        root_b = self.find(b)
        if root_a != root_b:
            self.parent[root_b] = root_a
        return root_a != root_b

    def classes(self, items=None, key=None):
        grouped = {}
        for item in items if items is not None else self.parent:
            grouped.setdefault(self.find(item), []).append(item)
        return [tuple(sorted(group, key=key)) for group in grouped.values()]


def _machine_conflict(volumes):
    seen = {}
    for volume in volumes:
        if volume.machine in seen:
            return volume.machine, seen[volume.machine], volume
        seen[volume.machine] = volume
    return None


def _distinct_pair(volumes, constraints):
    keys = {volume.key for volume in volumes}
    for constraint in constraints:
        if constraint.a.key in keys and constraint.b.key in keys:
            return constraint
    return None


def _entity_id(prefix, keys):
    return f"{prefix}-{_short_hash(sorted(f'{machine}\x00{volume}' for machine, volume in keys))}"


def _descriptor_map(volumes):
    return {volume.key: volume.descriptor() for volume in volumes}


def _shortcut_descriptor_map(shortcuts):
    return {(shortcut.volume.machine, shortcut.id): shortcut.descriptor() for shortcut in shortcuts}


def _pairs_from_groups(groups):
    """Return cross-machine identity pairs found in the same group."""
    pairs = set()
    for group in groups:
        for index, first in enumerate(group):
            for second in group[index + 1 :]:
                if first[0] == second[0]:
                    continue
                pairs.add(tuple(sorted((first, second))))
    return pairs


def _intersection(sequence):
    iterator = iter(sequence)
    try:
        common = set(next(iterator))
    except StopIteration:
        return set()
    for item in iterator:
        common &= item
        if not common:
            break
    return common


def _relations(kind, pairs, descriptors, id_field):
    graph = {}
    for left, right in pairs:
        graph.setdefault(left, set()).add(right)
        graph.setdefault(right, set()).add(left)
    seen = set()
    relations = []
    for node in sorted(graph, key=lambda item: (item[0].casefold(), item[1].casefold())):
        if node in seen:
            continue
        stack = [node]
        component = []
        seen.add(node)
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbour in graph.get(current, ()):
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        members = [dict(descriptors[key]) for key in component if key in descriptors]
        if len(members) < 2:
            continue
        members.sort(
            key=lambda item: (
                item["machine"].casefold(),
                str(item.get(id_field, "")).casefold(),
            )
        )
        relations.append(Relation(kind=kind, members=tuple(members)))
    relations.sort(key=lambda relation: relation.members[0]["machine"].casefold())
    return tuple(relations)


class _Component:
    """One connected candidate graph over identity atoms."""

    def __init__(self, atoms, edges):
        self.atoms = atoms
        self.edges = edges


class _GroupSolver:
    def __init__(
        self,
        group_id,
        volumes,
        shortcuts,
        candidates,
        objects,
        distinct_volumes,
        distinct_targets,
        maximal,
        max_states,
        max_plans,
    ):
        self.group_id = group_id
        self.volumes = tuple(sorted(volumes, key=_volume_sort_key))
        self.shortcuts = tuple(sorted(shortcuts, key=_shortcut_sort_key))
        self.candidates = tuple(candidates)
        self.objects = tuple(objects)
        self.distinct_volumes = tuple(distinct_volumes)
        self.distinct_targets = tuple(distinct_targets)
        self.maximal = maximal
        self.max_states = max_states
        self.max_plans = max_plans
        self.states = 0
        self.conflicts = []
        self.warnings = []

    def _conflict(self, kind, message, records=(), constraints=()):
        conflict = Conflict(
            kind=kind,
            message=message,
            records=tuple(records),
            constraints=tuple(constraints),
        )
        if conflict not in self.conflicts:
            self.conflicts.append(conflict)

    # -- validation shared by every plan -------------------------------
    def _check_objects(self):
        shortcut_dsu = _DSU([shortcut.id for shortcut in self.shortcuts])
        for item in self.objects:
            for member in item.members[1:]:
                shortcut_dsu.union(item.members[0].id, member.id)

        # A verified object, or several overlapping ones, must not require two
        # distinct volumes of the same machine to be one entity.
        by_id = {shortcut.id: shortcut for shortcut in self.shortcuts}
        for class_ids in shortcut_dsu.classes([shortcut.id for shortcut in self.shortcuts]):
            class_members = [by_id[shortcut_id] for shortcut_id in class_ids]
            for machine in {member.volume.machine for member in class_members}:
                machine_volumes = {
                    member.volume.key
                    for member in class_members
                    if member.volume.machine == machine
                }
                if len(machine_volumes) > 1:
                    involved = [
                        item
                        for item in self.objects
                        if any(member.id in class_ids for member in item.members)
                    ]
                    constraints = [item.descriptor() for item in involved]
                    constraints.extend(
                        item.descriptor()
                        for item in self.distinct_volumes
                        if item.a.key in machine_volumes and item.b.key in machine_volumes
                    )
                    self._conflict(
                        "mutual_exclusion",
                        "verified objects together require two distinct volumes of machine "
                        f"{machine!r} to be one entity",
                        records=[
                            member.descriptor()
                            for member in class_members
                            if member.volume.machine == machine
                        ],
                        constraints=constraints,
                    )

        for constraint in self.distinct_targets:
            if shortcut_dsu.find(constraint.a.id) == shortcut_dsu.find(constraint.b.id):
                self._conflict(
                    "distinct_target_conflict",
                    f"target constraint {constraint.id!r} marks one confirmed object as distinct",
                    records=[constraint.a.descriptor(), constraint.b.descriptor()],
                    constraints=[constraint.descriptor()],
                )

    def _forced_classes(self):
        dsu = _DSU(self.volumes)
        for item in self.objects:
            for member in item.members[1:]:
                dsu.union(item.members[0].volume, member.volume)
        return dsu

    def _check_forced(self, dsu):
        for volumes in dsu.classes(self.volumes, key=_volume_sort_key):
            duplicate = _machine_conflict(volumes)
            if duplicate:
                machine, first, second = duplicate
                self._conflict(
                    "mutual_exclusion",
                    f"volumes {first.id!r} and {second.id!r} of machine {machine!r} "
                    "cannot be mapped to the same entity",
                    records=[first.descriptor(), second.descriptor()],
                    constraints=[
                        item.descriptor()
                        for item in self.objects
                        if any(member.volume in volumes for member in item.members)
                    ],
                )
            constraint = _distinct_pair(volumes, self.distinct_volumes)
            if constraint:
                self._conflict(
                    "distinct_volume_conflict",
                    f"volume constraint {constraint.id!r} declares two forced-equal volumes "
                    "as distinct",
                    records=[constraint.a.descriptor(), constraint.b.descriptor()],
                    constraints=[constraint.descriptor()],
                )
            by_path = {}
            for shortcut in self.shortcuts:
                if shortcut.volume not in volumes:
                    continue
                path_key = shortcut.target_path_key()
                if path_key:
                    by_path.setdefault(path_key, []).append(shortcut)
            for members in by_path.values():
                ids = {member.id for member in members}
                for constraint in self.distinct_targets:
                    if constraint.a.id in ids and constraint.b.id in ids:
                        self._conflict(
                            "distinct_target_conflict",
                            f"target constraint {constraint.id!r} marks one confirmed target "
                            "as distinct",
                            records=[
                                constraint.a.descriptor(),
                                constraint.b.descriptor(),
                            ],
                            constraints=[constraint.descriptor()],
                        )

    # -- candidate components -----------------------------------------
    def _atom_components(self, atoms):
        atom_of = {}
        for index, atom in enumerate(atoms):
            for volume in atom:
                atom_of[volume.key] = index
        adjacency = {index: set() for index in range(len(atoms))}
        for item in self.candidates:
            left = atom_of[item.a.key]
            right = atom_of[item.b.key]
            if left != right:
                adjacency[left].add(right)
                adjacency[right].add(left)
        seen = set()
        components = []
        for start in range(len(atoms)):
            if start in seen:
                continue
            stack = [start]
            members = []
            seen.add(start)
            while stack:
                current = stack.pop()
                members.append(current)
                for neighbour in adjacency[current]:
                    if neighbour not in seen:
                        seen.add(neighbour)
                        stack.append(neighbour)
            edges = tuple(
                sorted(
                    (left, right) for left in members for right in adjacency[left] if left < right
                )
            )
            components.append(_Component(tuple(sorted(members)), edges))
        components.sort(key=lambda component: _volume_sort_key(atoms[component.atoms[0]][0]))
        return components

    # -- partition enumeration ----------------------------------------
    def _partition_problem(self, atoms, component):
        local = {global_index: index for index, global_index in enumerate(component.atoms)}
        local_atoms = [atoms[index] for index in component.atoms]
        edges = tuple(sorted((local[left], local[right]) for left, right in component.edges))
        return local_atoms, edges

    def _enumerate(self, atoms, edges):
        """Enumerate feasible blocks of one candidate component."""
        degree = dict.fromkeys(range(len(atoms)), 0)
        for left, right in edges:
            degree[left] += 1
            degree[right] += 1
        order = sorted(
            range(len(atoms)),
            key=lambda index: (-degree[index], _volume_sort_key(atoms[index][0])),
        )
        adjacency = {index: set() for index in range(len(atoms))}
        for left, right in edges:
            adjacency[left].add(right)
            adjacency[right].add(left)

        blocks = []
        results = []

        def connected_to(block, atom):
            return any(neighbour in block for neighbour in adjacency[atom])

        def block_volumes(block):
            return [volume for atom in block for volume in atoms[atom]]

        def compatible(block, atom):
            volumes = block_volumes(block) + list(atoms[atom])
            if _machine_conflict(volumes):
                return False
            return not _distinct_pair(volumes, self.distinct_volumes)

        def visit(position):
            self.states += 1
            if self.max_states is not None and self.states > self.max_states:
                raise ReconciliationLimitError(
                    f"group {self.group_id!r} exceeded {self.max_states} partition states"
                )
            if position == len(order):
                results.append(tuple(tuple(block) for block in blocks))
                return
            atom = order[position]
            for block in blocks:
                if not connected_to(block, atom):
                    continue
                if not compatible(block, atom):
                    continue
                block.append(atom)
                visit(position + 1)
                block.pop()
            blocks.append([atom])
            visit(position + 1)
            blocks.pop()

        visit(0)
        return results

    def _target_groups(self, shortcuts):
        """Group shortcuts into confirmed target entities for one plan.

        Only verified objects unite targets across machines.  Equal paths and
        equal drive letters are that machine's own evidence and are retained
        as such, not promoted to cross-machine identity.
        """
        dsu = _DSU([shortcut.id for shortcut in shortcuts])
        for item in self.objects:
            members = [member for member in item.members if member.id in dsu.parent]
            for member in members[1:]:
                dsu.union(members[0].id, member.id)
        grouped = {}
        for shortcut in shortcuts:
            grouped.setdefault(dsu.find(shortcut.id), []).append(shortcut)
        return grouped

    def _plan_valid(self, atom_to_entity, shortcuts):
        grouped = self._target_groups(shortcuts)
        for constraint in self.distinct_targets:
            if find_in_group(grouped, constraint.a.id) == find_in_group(grouped, constraint.b.id):
                return False
        return self._target_compatible(atom_to_entity)

    def _global_mapping(self, component, atoms, partition):
        """Map every volume to an entity for one component's partition.

        Volumes outside ``component`` keep their own singleton entity, so
        cross-component relations are never implied.
        """
        members = set(component.atoms)
        mapping = {}
        for index, block in enumerate(partition):
            for atom in block:
                for volume in atoms[component.atoms[atom]]:
                    mapping[volume.key] = f"block-{index}"
        for index, atom in enumerate(atoms):
            if index not in members:
                for volume in atom:
                    mapping.setdefault(volume.key, f"atom-{index}")
        return mapping

    def _maximal(self, partition, edges, local_atoms):
        block_of = {}
        for index, block in enumerate(partition):
            for atom in block:
                block_of[atom] = index
        for left, right in edges:
            left_block = block_of[left]
            right_block = block_of[right]
            if left_block == right_block:
                continue
            merged = list(partition[left_block]) + list(partition[right_block])
            volumes = [volume for atom in merged for volume in local_atoms[atom]]
            if _machine_conflict(volumes):
                continue
            if _distinct_pair(volumes, self.distinct_volumes):
                continue
            merged_entities = {}
            for index, block in enumerate(partition):
                target = left_block if index in (left_block, right_block) else index
                for atom in block:
                    for volume in local_atoms[atom]:
                        merged_entities[volume.key] = target
            if self.distinct_targets and not self._target_compatible(merged_entities):
                continue
            return False
        return True

    def _target_compatible(self, atom_to_entity):
        grouped = self._target_groups(self.shortcuts)
        for constraint in self.distinct_targets:
            if find_in_group(grouped, constraint.a.id) == find_in_group(grouped, constraint.b.id):
                return False
            path_a = constraint.a.target_path_key()
            entity_a = atom_to_entity.get(constraint.a.volume.key)
            entity_b = atom_to_entity.get(constraint.b.volume.key)
            if (
                path_a
                and path_a == constraint.b.target_path_key()
                and entity_a is not None
                and entity_a == entity_b
            ):
                return False
        return True

    # -- solve ---------------------------------------------------------
    def solve(self):
        self._check_objects()
        if self.conflicts:
            return self._result((), 0)

        dsu = self._forced_classes()
        self._check_forced(dsu)
        if self.conflicts:
            return self._result((), 0)

        atoms = dsu.classes(self.volumes, key=_volume_sort_key)
        components = self._atom_components(atoms)
        fragments = []
        for component in components:
            local_atoms, edges = self._partition_problem(atoms, component)
            try:
                partitions = self._enumerate(local_atoms, edges)
            except ReconciliationLimitError as error:
                self._conflict(
                    "search_limit",
                    str(error),
                    records=[volume.descriptor() for volume in self.volumes],
                    constraints=[
                        item.descriptor()
                        for item in (
                            list(self.candidates)
                            + list(self.objects)
                            + list(self.distinct_volumes)
                            + list(self.distinct_targets)
                        )
                    ],
                )
                return self._result((), 0)
            partitions = [
                partition
                for partition in partitions
                if self._plan_valid(
                    self._global_mapping(component, atoms, partition),
                    self.shortcuts,
                )
            ]
            if self.maximal:
                partitions = [
                    partition
                    for partition in partitions
                    if self._maximal(partition, edges, local_atoms)
                ]
            if not partitions:
                self._conflict(
                    "no_feasible_plan",
                    "the declared constraints eliminate every feasible identity assignment",
                    records=[volume.descriptor() for volume in self.volumes],
                    constraints=[
                        item.descriptor()
                        for item in (
                            list(self.candidates)
                            + list(self.objects)
                            + list(self.distinct_volumes)
                            + list(self.distinct_targets)
                        )
                    ],
                )
                return self._result((), 0)
            fragments.append((component, local_atoms, partitions))

        plan_count = 1
        for _, _, partitions in fragments:
            plan_count *= len(partitions)

        confirmed_volumes, confirmed_targets = self._component_relations(fragments)
        plans = ()
        omitted = False
        if plan_count <= self.max_plans:
            plans = self._plans(fragments)
        else:
            omitted = True
            self.warnings.append(
                {
                    "kind": "plans_omitted",
                    "message": (
                        f"{plan_count} plans exceed the materialisation limit "
                        f"{self.max_plans}; confirmed relations are still exact"
                    ),
                }
            )

        group = self._result(plans, plan_count)
        group = GroupResult(
            id=group.id,
            volumes=group.volumes,
            shortcuts=group.shortcuts,
            plans=group.plans,
            conflicts=group.conflicts,
            confirmed_volume_relations=confirmed_volumes,
            confirmed_target_relations=confirmed_targets,
            plan_count=plan_count,
            plans_omitted=omitted,
            warnings=group.warnings,
        )
        return group

    def _component_relations(self, fragments):
        volume_pairs = []
        target_pairs = []
        for _, local_atoms, partitions in fragments:
            component_volumes = [volume for atom in local_atoms for volume in atom]
            component_shortcuts = [
                shortcut
                for shortcut in self.shortcuts
                if shortcut.volume.key in {volume.key for volume in component_volumes}
            ]
            volume_sets = []
            target_sets = []
            for partition in partitions:
                atom_to_entity = {}
                for index, block in enumerate(partition):
                    for atom in block:
                        for volume in local_atoms[atom]:
                            atom_to_entity[volume.key] = index
                groups = [
                    tuple(
                        sorted(
                            (
                                volume.key
                                for atom in partition[index]
                                for volume in local_atoms[atom]
                            ),
                            key=lambda key: (key[0].casefold(), key[1].casefold()),
                        )
                    )
                    for index in range(len(partition))
                ]
                volume_sets.append(_pairs_from_groups(groups))
                grouped = self._target_groups(component_shortcuts)
                target_groups = [
                    tuple(sorted((member.volume.machine, member.id) for member in members))
                    for members in grouped.values()
                ]
                target_sets.append(_pairs_from_groups(target_groups))
            volume_pairs.extend(_intersection(volume_sets))
            target_pairs.extend(_intersection(target_sets))

        volume_descriptors = _descriptor_map(self.volumes)
        shortcut_descriptors = _shortcut_descriptor_map(self.shortcuts)
        return (
            _relations("volume", set(volume_pairs), volume_descriptors, "volume"),
            _relations("target", set(target_pairs), shortcut_descriptors, "shortcut"),
        )

    def _plans(self, fragments):
        per_component = []
        for _, local_atoms, partitions in fragments:
            per_component.append(
                [
                    (
                        tuple(
                            tuple(volume.key for atom in block for volume in local_atoms[atom])
                            for block in partition
                        ),
                        local_atoms,
                        partition,
                    )
                    for partition in partitions
                ]
            )
        plans = []
        for choice in itertools.product(*per_component):
            blocks = []
            atom_to_entity = {}
            entity_keys = []
            for signature, _local_atoms, _partition in choice:
                for block in signature:
                    keys = sorted(block, key=lambda key: (key[0].casefold(), key[1].casefold()))
                    entity_keys.append(keys)
                    entity_key = _entity_id("vol", keys)
                    for key in keys:
                        atom_to_entity[key] = entity_key
            for keys in sorted(entity_keys):
                entity_key = _entity_id("vol", keys)
                blocks.append(
                    {
                        "entity": entity_key,
                        "members": [
                            dict(volume_descriptor)
                            for volume_descriptor in (
                                _descriptor_map(self.volumes)[key] for key in keys
                            )
                        ],
                    }
                )
            blocks.sort(key=lambda item: item["entity"])
            grouped = self._target_groups(self.shortcuts)
            targets = []
            for members in grouped.values():
                members = sorted(members, key=_shortcut_sort_key)
                targets.append(
                    {
                        "entity": _entity_id(
                            "target", [(member.volume.machine, member.id) for member in members]
                        ),
                        "volume_entity": atom_to_entity[members[0].volume.key],
                        "path_key": members[0].target_path_key(),
                        "members": [member.descriptor() for member in members],
                    }
                )
            targets.sort(key=lambda item: item["entity"])
            mapping = sorted(
                (
                    {
                        "machine": volume.machine,
                        "volume": volume.id,
                        "entity": atom_to_entity[volume.key],
                    }
                    for volume in self.volumes
                ),
                key=lambda item: (item["machine"].casefold(), item["volume"].casefold()),
            )
            plans.append(
                Plan(
                    volume_entities=tuple(blocks),
                    target_entities=tuple(targets),
                    mapping=tuple(mapping),
                )
            )
        plans.sort(key=lambda plan: tuple(item["entity"] for item in plan.volume_entities))
        return tuple(plans)

    def _result(self, plans, plan_count):
        return GroupResult(
            id=self.group_id,
            volumes=self.volumes,
            shortcuts=self.shortcuts,
            plans=plans,
            conflicts=tuple(self.conflicts),
            plan_count=plan_count,
            warnings=tuple(self.warnings),
        )


def find_in_group(grouped, shortcut_id):
    for root, members in grouped.items():
        if any(member.id == shortcut_id for member in members):
            return root
    return None


def _constraint_edges(catalog):
    """Return adjacency over volume keys for every declared relation."""
    adjacency = {volume.key: set() for volume in catalog.volumes.values()}
    for item in catalog.candidate_mappings:
        adjacency.setdefault(item.a.key, set()).add(item.b.key)
        adjacency.setdefault(item.b.key, set()).add(item.a.key)
    for item in catalog.verified_objects:
        keys = [member.volume.key for member in item.members]
        for key in keys[1:]:
            adjacency.setdefault(keys[0], set()).add(key)
            adjacency.setdefault(key, set()).add(keys[0])
    for item in catalog.distinct_volumes:
        adjacency.setdefault(item.a.key, set()).add(item.b.key)
        adjacency.setdefault(item.b.key, set()).add(item.a.key)
    for item in catalog.distinct_targets:
        left = item.a.volume.key
        right = item.b.volume.key
        adjacency.setdefault(left, set()).add(right)
        adjacency.setdefault(right, set()).add(left)
    return adjacency


def _components(adjacency):
    seen = set()
    components = []
    for node in sorted(adjacency, key=lambda item: (item[0].casefold(), item[1].casefold())):
        if node in seen:
            continue
        stack = [node]
        component = []
        seen.add(node)
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbour in adjacency.get(current, ()):
                if neighbour not in seen:
                    seen.add(neighbour)
                    stack.append(neighbour)
        components.append(component)
    return components


def reconcile(catalog, maximal=True, max_states=2_000_000, max_plans=5_000):
    """Return the reconciliation report for ``catalog``.

    ``maximal`` keeps only identity assignments that cannot be refined any
    further: a feasible candidate merge is never left unused.  With
    ``maximal=False`` every feasible assignment is returned, including the
    assignment that keeps all volumes separate.
    """
    if not isinstance(catalog, Catalog):
        catalog = Catalog.from_dict(catalog)

    warnings = []
    valid_candidates = []
    for item in catalog.candidate_mappings:
        if item.a.key == item.b.key:
            continue
        if item.a.machine == item.b.machine:
            warnings.append(
                {
                    "kind": "candidate_same_machine",
                    "message": (
                        f"candidate {item.id!r} maps two volumes of one machine; "
                        "mutual exclusion keeps them apart"
                    ),
                    "constraint": item.descriptor(),
                }
            )
            continue
        if any(
            {constraint.a.key, constraint.b.key} == {item.a.key, item.b.key}
            for constraint in catalog.distinct_volumes
        ):
            warnings.append(
                {
                    "kind": "candidate_contradicts_distinct",
                    "message": (
                        f"candidate {item.id!r} is declared impossible by a "
                        "distinct-volume constraint; it is never used"
                    ),
                    "constraint": item.descriptor(),
                }
            )
        valid_candidates.append(item)

    components = _components(_constraint_edges(catalog))
    groups = []
    for index, keys in enumerate(components, 1):
        volume_keys = set(keys)
        volumes = [catalog.volumes[key] for key in keys]
        group_id = f"group-{index:02d}"
        solver = _GroupSolver(
            group_id=group_id,
            volumes=volumes,
            shortcuts=tuple(
                shortcut
                for shortcut in catalog.shortcuts.values()
                if shortcut.volume.key in volume_keys
            ),
            candidates=tuple(
                item
                for item in valid_candidates
                if item.a.key in volume_keys and item.b.key in volume_keys
            ),
            objects=tuple(
                item
                for item in catalog.verified_objects
                if any(member.volume.key in volume_keys for member in item.members)
            ),
            distinct_volumes=tuple(
                item
                for item in catalog.distinct_volumes
                if item.a.key in volume_keys and item.b.key in volume_keys
            ),
            distinct_targets=tuple(
                item
                for item in catalog.distinct_targets
                if item.a.volume.key in volume_keys and item.b.volume.key in volume_keys
            ),
            maximal=maximal,
            max_states=max_states,
            max_plans=max_plans,
        )
        groups.append(solver.solve())

    solved = [group for group in groups if not group.conflicts]
    plan_count = 0
    if solved and len(solved) == len(groups):
        plan_count = 1
        for group in solved:
            plan_count *= group.plan_count

    return Report(
        groups=tuple(groups),
        complete=len(solved) == len(groups),
        plan_count=plan_count,
        confirmed_volume_relations=tuple(
            relation for group in solved for relation in group.confirmed_volume_relations
        ),
        confirmed_target_relations=tuple(
            relation for group in solved for relation in group.confirmed_target_relations
        ),
        warnings=tuple(warnings),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Reconcile shortcut catalogs collected from several machines"
    )
    parser.add_argument("catalog", help="catalog JSON file")
    parser.add_argument("-o", "--output", help="write the report to this file")
    parser.add_argument(
        "--compact",
        action="store_true",
        help="omit per-plan entity details from the report",
    )
    parser.add_argument(
        "--all-feasible",
        action="store_true",
        help="report every feasible assignment, not only maximal ones",
    )
    parser.add_argument(
        "--max-states",
        type=int,
        default=2_000_000,
        help="partition search limit per independent group",
    )
    parser.add_argument(
        "--max-plans",
        type=int,
        default=5_000,
        help="maximum number of plans to materialise",
    )
    parser.add_argument(
        "--fail-on-conflict",
        action="store_true",
        help="exit with status 2 when any group is unsatisfiable",
    )
    args = parser.parse_args(argv)

    with open(args.catalog, encoding="utf-8") as handle:
        catalog = Catalog.from_dict(json.load(handle))
    report = reconcile(
        catalog,
        maximal=not args.all_feasible,
        max_states=args.max_states,
        max_plans=args.max_plans,
    )
    text = report.to_json(include_plans=not args.compact)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.write("\n")
    else:
        print(text)
    if args.fail_on_conflict and not report.complete:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
