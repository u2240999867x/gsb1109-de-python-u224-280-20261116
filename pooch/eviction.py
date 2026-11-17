# Copyright (c) 2018 The Pooch Developers.
# Distributed under the terms of the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause
#
# This code is part of the Fatiando a Terra project (https://www.fatiando.org)
#
"""
Plan and execute safe reclamation of space in a Pooch cache.

Pooch keeps downloaded files and the artifacts produced from them by
post-processors. When the disk is full, choosing what to delete is not just a
matter of picking the largest files:

* Some entries are originals, others were derived from them. Evicting an
  original invalidates everything derived from it.
* Several names can refer to the same data through hard links, so logical
  sizes cannot be added up. Bytes are counted once per physical file and only
  when the last link is removed.
* Some files in the cache folder do not belong to the cache. They are never
  deleted and, if they share storage with a cache file, they keep those bytes
  alive.
* Several materials can list the same path. It is only deleted together with
  all of them, so those materials are treated as one decision.
* Entries may be leased by a running job or pinned to a retained version.
  They are not candidates, and a change in that state invalidates the plan.

The public API is :func:`plan_eviction`, :func:`revalidate_eviction` and
:func:`execute_eviction`. Costs are *refetch costs*: the expense of obtaining
a material again. The planner minimizes their sum, breaking ties by the
smallest overshoot.
"""

from __future__ import annotations

import copy
import math
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

from .utils import get_logger

__all__ = [
    "CacheMaterial",
    "EvictionError",
    "EvictionPlan",
    "EvictionResult",
    "execute_eviction",
    "plan_eviction",
    "revalidate_eviction",
]

PathInput = Union[str, os.PathLike]
MaterialInput = Any
StateProvider = Union[
    Iterable[MaterialInput], Mapping[str, MaterialInput], Callable[[], Any]
]


class EvictionError(ValueError):
    """
    Raised when an eviction problem is malformed or cannot be represented.
    """


@dataclass(frozen=True)
class CacheMaterial:
    """
    A cache entry that may be reclaimed.

    Parameters
    ----------
    name : str
        Unique name of the material, normally its path relative to the cache.
    size : int
        Logical size in bytes. Informational only: reclaimed bytes are always
        computed from the physical files referenced by *paths*.
    refetch_cost : float
        Cost of downloading or rebuilding this material. Must be finite and
        non-negative. Zero is valid, for example for a locally derived
        artifact that is cheap to recreate.
    originals : tuple of str
        Names of the materials this one derives from. If an original is
        evicted, this material is evicted too.
    derived : tuple of str
        Names of the materials derived from this one. This is the inverse of
        *originals*; both directions are accepted and merged.
    paths : tuple of str
        Files deleted when the material is evicted. Relative paths are
        resolved against the cache root.
    owner : bool
        Whether the cache owns the files. Unowned files are never deleted and
        never release bytes.
    leased : bool
        Whether a job currently holds the material. Leased materials are never
        evicted.
    lease : str or None
        Optional identifier of the lease holder. Any non-empty value implies
        ``leased=True``.
    version : str or None
        Version of the material, checked against *retained_versions*.
    pinned : bool
        An explicit reason to never evict this material.
    metadata : dict
        Free-form application data. Ignored by the planner.
    """

    name: str
    size: int = 0
    refetch_cost: float = 0.0
    originals: Tuple[str, ...] = ()
    derived: Tuple[str, ...] = ()
    paths: Tuple[str, ...] = ()
    owner: bool = True
    leased: bool = False
    lease: Optional[str] = None
    version: Optional[str] = None
    pinned: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        if not self.name:
            raise EvictionError("Material names must be non-empty strings.")
        originals = tuple(str(item) for item in self.originals)
        derived = tuple(str(item) for item in self.derived)
        paths = tuple(str(item) for item in self.paths)
        object.__setattr__(self, "originals", originals)
        object.__setattr__(self, "derived", derived)
        object.__setattr__(self, "paths", paths)
        if self.size < 0:
            raise EvictionError(f"Material '{self.name}' has a negative size.")
        if not math.isfinite(self.refetch_cost) or self.refetch_cost < 0:
            raise EvictionError(
                f"Material '{self.name}' has an invalid refetch cost "
                f"({self.refetch_cost!r})."
            )

    @property
    def protected(self) -> bool:
        "Whether a lease, a lease holder or a pin forbids eviction."
        return self.leased or bool(self.lease) or self.pinned


Material = CacheMaterial


@dataclass
class _Material:
    "Normalized internal representation of a material."

    name: str
    size: int
    cost: float
    originals: Tuple[str, ...]
    derived: Tuple[str, ...]
    paths: Tuple[str, ...]
    owner: bool
    leased: bool
    lease: Optional[str]
    version: Optional[str]
    pinned: bool

    @property
    def protected(self) -> bool:
        return self.leased or bool(self.lease) or self.pinned


@dataclass(frozen=True)
class _Storage:
    "A physical file (inode) and the materials that reference it."

    key: str
    size: int
    owners: Tuple[str, ...]
    owned: bool
    external: bool
    path: str


@dataclass
class EvictionPlan:
    """
    Result of an eviction optimization.

    Attributes
    ----------
    requested : int
        Number of bytes requested.
    materials : list of CacheMaterial
        Materials selected for eviction.
    released : int
        Physical bytes that executing the plan would release.
    released_logical : int
        Sum of the logical sizes of the selected materials. This is reported
        for transparency and can be larger than *released* when hard links or
        unowned files are involved.
    cost : float
        Total refetch cost of the selected materials.
    feasible : bool
        Whether enough bytes can be released at all.
    target_met : bool
        Whether ``released >= requested``.
    deficit : int
        If the target cannot be met, the number of missing bytes. Zero
        otherwise.
    max_releasable : int
        Maximum number of cache-owned bytes any valid plan could release.
    exact : bool
        Whether the plan is proven optimal. It is False only for unusually
        large dependency/link components solved with a bounded search.
    reason : str
        Explanation when the plan is infeasible or empty.
    storage : dict
        Physical storage released by the plan, keyed by device and inode.
    retained : dict
        Materials that could not be considered, mapped to the reason.
    snapshot : dict
        State captured while planning, used by revalidation.
    """

    requested: int
    materials: List[CacheMaterial] = field(default_factory=list)
    released: int = 0
    released_logical: int = 0
    cost: float = 0.0
    feasible: bool = True
    target_met: bool = False
    deficit: int = 0
    max_releasable: int = 0
    exact: bool = True
    reason: str = ""
    storage: Dict[str, int] = field(default_factory=dict)
    retained: Dict[str, str] = field(default_factory=dict)
    snapshot: Dict[str, Any] = field(default_factory=dict)

    @property
    def names(self) -> List[str]:
        "Names of the selected materials."
        return [material.name for material in self.materials]

    @property
    def actual_released(self) -> int:
        "Alias for :attr:`released`."
        return self.released

    @property
    def total_cost(self) -> float:
        "Alias for :attr:`cost`."
        return self.cost

    @property
    def empty(self) -> bool:
        "Whether no material was selected."
        return not self.materials

    def __bool__(self) -> bool:
        return self.target_met

    def summary(self) -> str:
        "Return a one-line summary of the plan."
        head = f"release {self.released} of {self.requested} bytes"
        tail = f"cost {self.cost:g} with {len(self.materials)} material(s)"
        if not self.feasible:
            return f"{head} (impossible: {self.reason}; missing {self.deficit} bytes)"
        if not self.target_met:
            return f"{head} (target not met; {tail})"
        return f"{head} ({tail})"


@dataclass
class EvictionResult:
    """
    Outcome of :func:`execute_eviction`.

    Attributes
    ----------
    plan : EvictionPlan
        The revalidated plan that was executed.
    evicted : list of str
        Names of materials whose files were removed.
    skipped : dict
        Materials that were not deleted, mapped to the reason.
    released : int
        Physical bytes actually released, measured after deletion.
    cost : float
        Refetch cost actually paid.
    errors : dict
        Materials whose deletion failed, mapped to the error message.
    dry_run : bool
        Whether the execution was a simulation.
    """

    plan: EvictionPlan
    evicted: List[str] = field(default_factory=list)
    skipped: Dict[str, str] = field(default_factory=dict)
    released: int = 0
    cost: float = 0.0
    errors: Dict[str, str] = field(default_factory=dict)
    dry_run: bool = False

    @property
    def names(self) -> List[str]:
        "Names of the evicted materials."
        return list(self.evicted)

    @property
    def actual_released(self) -> int:
        "Alias for :attr:`released`."
        return self.released

    def __bool__(self) -> bool:
        return bool(self.evicted) and (self.released > 0 or self.dry_run)


def plan_eviction(
    materials: Union[Iterable[MaterialInput], Mapping[str, MaterialInput], None],
    requested: int,
    *,
    cache_root: Optional[PathInput] = None,
    retained_versions: Union[Iterable[str], str, None] = None,
    retained_paths: Optional[Iterable[PathInput]] = None,
    keep_materials: Optional[Iterable[str]] = None,
) -> EvictionPlan:
    """
    Select the cheapest materials that release at least *requested* bytes.

    The objective is the sum of the refetch costs of the evicted materials.
    Physical storage is attributed per inode: hard links count once, storage
    shared with unowned or external files is not released, and a material
    without existing files releases nothing. Dependencies are closed under
    eviction: selecting an original also selects everything derived from it.
    Derived artifacts can be selected alone to reclaim their bytes while
    keeping their originals.

    Parameters
    ----------
    materials : iterable, mapping or object
        Materials to consider. Each item can be a :class:`CacheMaterial`, a
        mapping with matching keys, or any object with matching attributes.
        When a mapping is given, its keys are used as names if the values do
        not provide one.
    requested : int
        Number of bytes that must be released. Must be non-negative.
    cache_root : str or PathLike or None
        Root used to resolve relative paths and to snapshot file identities.
    retained_versions : iterable of str, str or None
        Versions that must never be evicted. A single string is accepted.
    retained_paths : iterable of str or PathLike or None
        Files that must never be evicted. Any material referencing one of
        these paths is retained, together with everything it depends on.
    keep_materials : iterable of str or None
        Names that must never be evicted.

    Returns
    -------
    plan : EvictionPlan
        The optimal plan. Infeasibility is reported in the plan rather than
        raised: ``feasible`` is False, ``deficit`` is filled with the missing
        bytes and ``max_releasable`` with the best achievable amount.

    Raises
    ------
    EvictionError
        If the inputs are malformed: duplicate names, negative sizes or
        costs, unknown dependency references, dependency cycles, or a
        non-numeric request.
    """
    requested = _validate_request(requested)
    root = _root(cache_root)
    entries = _with_dependencies(_normalize(materials))
    retained = _retained(
        entries, retained_versions, retained_paths, keep_materials, root
    )
    storage = _inspect_storage(entries, root)
    max_releasable = _max_releasable(entries, storage, retained)

    if requested > max_releasable:
        return _infeasible_plan(
            requested=requested,
            entries=entries,
            retained=retained,
            max_releasable=max_releasable,
            reason=(
                f"only {max_releasable} cache-owned bytes can be released, "
                f"{requested - max_releasable} bytes short of the target"
            ),
            root=root,
        )

    selected, exact = _optimize(entries, storage, requested, retained=set(retained))
    if selected is None:
        return _infeasible_plan(
            requested=requested,
            entries=entries,
            retained=retained,
            max_releasable=max_releasable,
            reason="no dependency-consistent selection reaches the target",
            root=root,
        )
    return _build_plan(
        requested=requested,
        selected=selected,
        entries=entries,
        retained=retained,
        storage=storage,
        max_releasable=max_releasable,
        root=root,
        retained_versions=retained_versions,
        retained_paths=retained_paths,
        keep_materials=keep_materials,
        exact=exact,
    )


def revalidate_eviction(
    plan: EvictionPlan,
    materials: Union[StateProvider, None] = None,
    *,
    cache_root: Optional[PathInput] = None,
    retained_versions: Union[Iterable[str], str, None] = None,
    retained_paths: Optional[Iterable[PathInput]] = None,
    keep_materials: Optional[Iterable[str]] = None,
) -> EvictionPlan:
    """
    Recompute an eviction plan from the current cache state.

    Leases, versions and file contents can change between planning and
    execution. This function rebuilds the problem from *materials* (or from a
    callable returning them), re-applies every constraint and returns a fresh
    optimal plan for the same request. Files replaced since planning and
    materials that are now leased, pinned or on a retained version are never
    selected.

    Parameters
    ----------
    plan : EvictionPlan
        Plan to revalidate.
    materials : iterable, mapping, callable or None
        Current material descriptions, or a callable returning them. If None,
        the descriptions captured in the plan are used and only file
        identities are rechecked.
    cache_root : str or PathLike or None
        Root for relative paths. Defaults to the root recorded in the plan.
    retained_versions, retained_paths, keep_materials
        Same meaning as in :func:`plan_eviction`. They default to the values
        captured by the original plan.

    Returns
    -------
    plan : EvictionPlan
        A new plan. Its ``released`` and ``target_met`` reflect the current
        state and it may select different materials than the original plan
        when the original choices became unsafe. Materials skipped for safety
        are listed in ``retained`` with the reason.
    """
    root = _root(
        cache_root if cache_root is not None else plan.snapshot.get("cache_root")
    )
    current = _resolve_state(materials, plan)
    entries = _with_dependencies(_normalize(current))
    versions = _snapshot_value(plan, "retained_versions", retained_versions)
    paths = _snapshot_value(plan, "retained_paths", retained_paths)
    keep = set(_snapshot_value(plan, "keep_materials", keep_materials) or ())
    changes = _changed_files(plan, entries, root)
    # A file replaced since planning is treated as retained by path, which
    # also protects its ancestors. This is stricter than a name-only keep.
    changed_paths = [
        str(path)
        for entry in entries
        if entry.name in changes
        for path in entry.paths
    ]
    changed_retained = list(paths or ()) + changed_paths
    # Keep names are validated by plan_eviction; drop ones that disappeared so
    # that removing a material between planning and execution is not an error.
    known = {entry.name for entry in entries}
    keep &= known
    updated = plan_eviction(
        current,
        plan.requested,
        cache_root=root,
        retained_versions=versions,
        retained_paths=changed_retained,
        keep_materials=keep,
    )
    for name, reason in changes.items():
        updated.retained[name] = reason
    dropped = set(plan.names) - set(updated.names)
    for name in dropped:
        updated.retained.setdefault(name, "not selected by the revalidated plan")
    added = set(updated.names) - set(plan.names)
    if added:
        get_logger().info(
            "Revalidation chose %d replacement material(s): %s",
            len(added),
            ", ".join(sorted(added)),
        )
    if dropped:
        get_logger().info(
            "Revalidation dropped %d planned material(s): %s",
            len(dropped),
            ", ".join(sorted(dropped)),
        )
    return updated


def execute_eviction(
    plan: EvictionPlan,
    materials: Union[StateProvider, None] = None,
    *,
    cache_root: Optional[PathInput] = None,
    dry_run: bool = False,
    retained_versions: Union[Iterable[str], str, None] = None,
    retained_paths: Optional[Iterable[PathInput]] = None,
    keep_materials: Optional[Iterable[str]] = None,
) -> EvictionResult:
    """
    Revalidate a plan and delete the files it selected.

    The plan is always revalidated first. A material that is now leased,
    pinned, on a retained version or whose files were replaced is not deleted.
    When *materials* is a callable, it is called again immediately before
    deleting so that leases taken by another job are observed. Deletion uses
    unlink, so other hard links stay intact and bytes are only reported when
    the last link disappears. The result is measured on disk before and after
    instead of trusting the planned numbers.

    Parameters
    ----------
    plan : EvictionPlan
        Plan produced by :func:`plan_eviction`.
    materials : iterable, mapping, callable or None
        Current material descriptions or a callable returning them. If None,
        only file identities are rechecked.
    cache_root : str or PathLike or None
        Root for relative paths. Defaults to the plan's root.
    dry_run : bool
        If True, revalidate and report what would be deleted without touching
        the files.
    retained_versions, retained_paths, keep_materials
        Forwarded to :func:`revalidate_eviction`.

    Returns
    -------
    result : EvictionResult
        The revalidated plan, the materials actually evicted, skipped
        materials, actual released bytes and actual cost.
    """
    if plan.snapshot.get("executed") and not dry_run:
        return EvictionResult(
            plan=plan,
            skipped={"*": "this plan has already been executed"},
        )
    root = _root(
        cache_root if cache_root is not None else plan.snapshot.get("cache_root")
    )
    current_plan = revalidate_eviction(
        plan,
        materials,
        cache_root=root,
        retained_versions=retained_versions,
        retained_paths=retained_paths,
        keep_materials=keep_materials,
    )
    result = EvictionResult(plan=current_plan, dry_run=dry_run)
    result.skipped = {
        name: reason
        for name, reason in current_plan.retained.items()
        if name in plan.names or name in current_plan.names
    }
    unprotected = set(current_plan.names) - set(current_plan.retained)
    for name in sorted(set(plan.names) - unprotected):
        if name not in result.skipped and name not in current_plan.retained:
            result.skipped[name] = "not selected after revalidation"
    entries = _entries_from_plan(current_plan)
    by_name = {entry.name: entry for entry in entries}

    if dry_run:
        result.evicted = sorted(current_plan.names)
        result.released = current_plan.released
        result.cost = current_plan.cost
        return result

    if not current_plan.materials:
        if current_plan.reason:
            result.skipped["*"] = current_plan.reason
        return result

    if materials is not None:
        refreshed = _with_dependencies(_normalize(_resolve_state(materials, plan)))
        by_name.update({entry.name: entry for entry in refreshed})

    before = _inode_snapshot(_inspect_storage(list(by_name.values()), root))
    expected = current_plan.snapshot.get("files", {})
    deletable: Set[str] = set()
    for material in current_plan.materials:
        entry = by_name.get(material.name)
        if entry is None:
            result.skipped[material.name] = "material no longer exists"
            continue
        if entry.protected:
            result.skipped[material.name] = "lease or pin changed"
            continue
        if not entry.owner:
            result.skipped[material.name] = "files are not owned by the cache"
            continue
        if tuple(entry.paths) != tuple(material.paths):
            result.skipped[material.name] = "paths changed"
            continue
        deletable.add(material.name)

    # A path listed by several materials may only be deleted when all of its
    # owners are being evicted. Otherwise deleting it would break a material
    # that stays in the cache and the reported release would be wrong.
    path_owners: Dict[str, Set[str]] = {}
    for entry in by_name.values():
        for item in entry.paths:
            path_owners.setdefault(_identity(_resolve(item, root)), set()).add(
                entry.name
            )

    # Check every file before deleting anything: a material is only evicted
    # when all of its files are still the ones that were planned.
    for name in sorted(deletable):
        reason = _check_files(name, by_name[name].paths, expected, root)
        if reason:
            result.skipped[name] = reason
            deletable.discard(name)
    # A blocked descendant blocks its ancestors: never delete an original
    # while a derived artifact that must go with it stays behind.
    deletable = _enforce_closure(deletable, by_name, result)
    # Paths that are also listed by a material that stays are left in place;
    # the rest of the material is still evicted and the path is reported.
    skipped_paths: Dict[str, Set[str]] = {}
    for name in sorted(deletable):
        for item in by_name[name].paths:
            owners = path_owners[_identity(_resolve(item, root))]
            outsiders = owners - deletable
            if outsiders:
                skipped_paths.setdefault(name, set()).add(item)

    removed: Set[str] = set()
    for name in _deletion_order(deletable, by_name):
        if callable(materials):
            # Another job may have taken a lease while we were deleting.
            fresh = _with_dependencies(_normalize(materials()))
            latest = {entry.name: entry for entry in fresh}.get(name)
            if latest is None or latest.protected:
                result.skipped[name] = "lease changed during execution"
                continue
            if tuple(latest.paths) != tuple(by_name[name].paths):
                result.skipped[name] = "paths changed during execution"
                continue
            reason = _check_files(name, latest.paths, expected, root)
            if reason:
                result.skipped[name] = reason
                continue
        blocked_children = [
            child
            for child in _descendants(name, by_name)
            if child in by_name and child not in removed
        ]
        if blocked_children:
            result.skipped[name] = (
                "derived material(s) were not evicted: "
                + ", ".join(sorted(blocked_children))
            )
            continue
        error = None
        shared = skipped_paths.get(name, set())
        deleted_any = False
        for item in by_name[name].paths:
            if item in shared:
                continue
            path = _resolve(item, root)
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError as exc:
                error = str(exc)
                break
            deleted_any = True
        if shared:
            result.skipped[name] = (
                "left in place because other materials use: "
                + ", ".join(sorted(shared))
            )
        if error is not None:
            result.errors[name] = error
        elif deleted_any:
            removed.add(name)
        elif not shared and name not in result.skipped:
            result.skipped[name] = "files were already gone"
            removed.add(name)

    plan.snapshot["executed"] = True
    after = _inspect_storage(list(by_name.values()), root)
    result.evicted = sorted(removed)
    result.released = _released_bytes(before, after)
    result.cost = sum(by_name[name].cost for name in removed)
    if result.released != current_plan.released:
        get_logger().info(
            "Eviction released %d bytes instead of the planned %d.",
            result.released,
            current_plan.released,
        )
    return result


def _validate_request(requested: Any) -> int:
    if isinstance(requested, bool) or not isinstance(requested, (int, float)):
        raise EvictionError(f"Requested size must be a number, got {requested!r}.")
    if not math.isfinite(float(requested)) or requested < 0:
        raise EvictionError(
            f"Requested size must be a finite non-negative number, got {requested!r}."
        )
    return int(requested)


def _root(cache_root: Optional[PathInput]) -> Optional[Path]:
    if cache_root is None:
        return None
    return Path(cache_root).expanduser().resolve()


def _normalize(materials: Any) -> List[_Material]:
    "Convert arbitrary material descriptions to normalized entries."
    if materials is None:
        return []
    if isinstance(materials, (str, bytes)):
        raise EvictionError("Materials must be a collection, not a single string.")
    if isinstance(materials, Mapping):
        pairs: Sequence[Tuple[Optional[str], Any]] = [
            (str(key), value) for key, value in materials.items()
        ]
    elif isinstance(materials, Iterable):
        pairs = [(None, item) for item in materials]
    else:
        pairs = [(None, materials)]

    entries: List[_Material] = []
    seen: Set[str] = set()
    for key, item in pairs:
        data = _as_mapping(item)
        name = data.get("name", key)
        if name is None:
            raise EvictionError(
                "Materials passed as a mapping must provide a name or a key."
            )
        name = str(name)
        if not name:
            raise EvictionError("Material names must be non-empty strings.")
        if name in seen:
            raise EvictionError(f"Duplicate material name '{name}'.")
        seen.add(name)
        entry = _Material(
            name=name,
            size=int(data.get("size", 0) or 0),
            cost=float(
                data.get("refetch_cost", data.get("cost", data.get("refetch", 0.0)))
                or 0.0
            ),
            originals=tuple(str(value) for value in data.get("originals", ()) or ()),
            derived=tuple(str(value) for value in data.get("derived", ()) or ()),
            paths=tuple(str(value) for value in data.get("paths", ()) or ()),
            owner=bool(data.get("owner", data.get("owned", True))),
            leased=bool(data.get("leased", False)),
            lease=data.get("lease"),
            version=None if data.get("version") is None else str(data["version"]),
            pinned=bool(data.get("pinned", False)),
        )
        _validate_entry(entry)
        entries.append(entry)
    return entries


def _validate_entry(entry: _Material) -> None:
    if entry.size < 0:
        raise EvictionError(f"Material '{entry.name}' has a negative size.")
    if not math.isfinite(entry.cost) or entry.cost < 0:
        raise EvictionError(
            f"Material '{entry.name}' has an invalid refetch cost ({entry.cost!r})."
        )


def _as_mapping(item: Any) -> Dict[str, Any]:
    "Extract material fields from an object or mapping."
    if isinstance(item, CacheMaterial):
        return {
            "name": item.name,
            "size": item.size,
            "refetch_cost": item.refetch_cost,
            "originals": item.originals,
            "derived": item.derived,
            "paths": item.paths,
            "owner": item.owner,
            "leased": item.leased,
            "lease": item.lease,
            "version": item.version,
            "pinned": item.pinned,
        }
    if isinstance(item, Mapping):
        return dict(item)
    fields = (
        "name",
        "size",
        "refetch_cost",
        "cost",
        "refetch",
        "originals",
        "derived",
        "paths",
        "owner",
        "owned",
        "leased",
        "lease",
        "version",
        "pinned",
    )
    data = {key: getattr(item, key) for key in fields if hasattr(item, key)}
    if not data:
        raise EvictionError(
            f"Cannot interpret {item!r} as a material. Use CacheMaterial or a mapping."
        )
    return data


def _with_dependencies(entries: List[_Material]) -> List[_Material]:
    "Merge both dependency directions and validate the graph."
    by_name = {entry.name: entry for entry in entries}
    originals = {entry.name: set(entry.originals) for entry in entries}
    derived = {entry.name: set(entry.derived) for entry in entries}
    for entry in entries:
        for parent in entry.originals:
            if parent not in by_name:
                raise EvictionError(
                    f"Material '{entry.name}' depends on unknown material '{parent}'."
                )
            derived[parent].add(entry.name)
        for child in entry.derived:
            if child not in by_name:
                raise EvictionError(
                    f"Material '{entry.name}' lists unknown derived material '{child}'."
                )
            originals[child].add(entry.name)
    _check_cycles(originals)
    return [
        _Material(
            name=entry.name,
            size=entry.size,
            cost=entry.cost,
            originals=tuple(sorted(originals[entry.name])),
            derived=tuple(sorted(derived[entry.name])),
            paths=entry.paths,
            owner=entry.owner,
            leased=entry.leased,
            lease=entry.lease,
            version=entry.version,
            pinned=entry.pinned,
        )
        for entry in entries
    ]


def _check_cycles(originals: Mapping[str, Set[str]]) -> None:
    visiting: Set[str] = set()
    done: Set[str] = set()

    def visit(name: str) -> None:
        if name in done:
            return
        if name in visiting:
            raise EvictionError(
                f"Cyclic dependency detected involving material '{name}'."
            )
        visiting.add(name)
        for parent in originals.get(name, ()):
            visit(parent)
        visiting.remove(name)
        done.add(name)

    for name in originals:
        visit(name)


def _retained(
    entries: List[_Material],
    retained_versions: Union[Iterable[str], str, None],
    retained_paths: Optional[Iterable[PathInput]],
    keep_materials: Optional[Iterable[str]],
    root: Optional[Path],
) -> Dict[str, str]:
    "Return the reasons why materials cannot be evicted."
    if isinstance(retained_versions, str):
        versions = {retained_versions}
    else:
        versions = {str(version) for version in retained_versions or ()}
    paths = {_identity(_resolve(path, root)) for path in retained_paths or ()}
    keep = {str(name) for name in keep_materials or ()}
    unknown = keep - {entry.name for entry in entries}
    if unknown:
        raise EvictionError(
            "Cannot keep unknown material(s): " + ", ".join(sorted(unknown))
        )

    retained: Dict[str, str] = {}
    for entry in entries:
        if entry.name in keep:
            retained[entry.name] = "explicitly kept"
        elif entry.pinned:
            retained[entry.name] = "pinned"
        elif entry.leased:
            retained[entry.name] = "leased"
        elif entry.lease:
            retained[entry.name] = f"leased by {entry.lease}"
        elif not entry.owner:
            retained[entry.name] = "files are not owned by the cache"
        elif entry.version is not None and entry.version in versions:
            retained[entry.name] = f"version {entry.version} is retained"
        elif any(_identity(_resolve(path, root)) in paths for path in entry.paths):
            retained[entry.name] = "path is retained"

    peers = _path_peers(entries)
    changed = True
    while changed:
        changed = False
        for entry in entries:
            if entry.name in retained:
                continue
            for child in entry.derived:
                if child in retained:
                    retained[entry.name] = (
                        f"needed by retained derived material '{child}'"
                    )
                    changed = True
                    break
            if entry.name in retained:
                continue
            for peer in peers.get(entry.name, ()):
                if peer in retained:
                    retained[entry.name] = (
                        f"shares files with retained material '{peer}'"
                    )
                    changed = True
                    break
    return retained


def _inspect_storage(
    entries: Sequence[_Material], root: Optional[Path]
) -> Dict[str, _Storage]:
    "Group the files of every material by physical storage."
    records: Dict[str, Dict[str, Any]] = {}
    paths_by_inode: Dict[Tuple[int, int], Set[str]] = {}
    for entry in entries:
        for path in entry.paths:
            full = _resolve(path, root)
            try:
                info = full.lstat()
            except OSError:
                continue
            # Symlinks are never followed: deleting a link frees no data and
            # might delete something outside of the cache.
            if not stat.S_ISREG(info.st_mode):
                continue
            inode = (info.st_dev, info.st_ino)
            key = f"{info.st_dev}:{info.st_ino}"
            paths_by_inode.setdefault(inode, set()).add(_identity(full))
            record = records.setdefault(
                key,
                {
                    "size": info.st_size,
                    "owners": set(),
                    "owned": True,
                    "path": str(full),
                },
            )
            record["owners"].add(entry.name)
            record["owned"] = bool(record["owned"] and entry.owner)

    storage: Dict[str, _Storage] = {}
    for key, record in records.items():
        info = _stat(record["path"])
        inode = (info.st_dev, info.st_ino) if info is not None else None
        links = info.st_nlink if info is not None else 1
        known = len(paths_by_inode.get(inode, ())) if inode is not None else 1
        storage[key] = _Storage(
            key=key,
            size=int(record["size"]),
            owners=tuple(sorted(record["owners"])),
            owned=bool(record["owned"]),
            external=links > known,
            path=record["path"],
        )
    return storage


def _stat(path: str) -> Optional[os.stat_result]:
    try:
        return os.stat(path)
    except OSError:
        return None


def _resolve(path: PathInput, root: Optional[Path]) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute() and root is not None:
        resolved = root / resolved
    return resolved


def _identity(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(path))))


def _path_peers(entries: Sequence[_Material]) -> Dict[str, Set[str]]:
    "Map every material to the materials that share at least one path with it."
    owners: Dict[str, Set[str]] = {}
    for entry in entries:
        for item in entry.paths:
            owners.setdefault(_identity(_resolve(item, None)), set()).add(entry.name)
    peers: Dict[str, Set[str]] = {entry.name: set() for entry in entries}
    for names in owners.values():
        for name in names:
            peers[name] |= names - {name}
    return peers


def _selectable_closure(
    name: str,
    by_name: Mapping[str, _Material],
    peers: Mapping[str, Set[str]],
    bit: Mapping[str, int],
) -> int:
    """
    Mask of everything that must be evicted together with *name*.

    Evicting a material requires evicting everything derived from it and
    every material that shares one of its paths (deleting a shared path would
    break the other owner). Both relations are followed transitively.
    """
    mask = 0
    stack = [name]
    seen: Set[str] = set()
    while stack:
        current = stack.pop()
        if current in seen or current not in by_name:
            continue
        seen.add(current)
        mask |= bit[current]
        stack.extend(by_name[current].derived)
        stack.extend(peers.get(current, ()))
    return mask


def _released(storage: Mapping[str, _Storage], selected: Set[str]) -> Set[str]:
    "Storage keys released by a selection."
    return {
        key
        for key, item in storage.items()
        if item.owned
        and not item.external
        and item.owners
        and set(item.owners) <= selected
    }


def _release(storage: Mapping[str, _Storage], selected: Set[str]) -> int:
    return sum(storage[key].size for key in _released(storage, selected))


def _max_releasable(
    entries: Sequence[_Material],
    storage: Mapping[str, _Storage],
    retained: Mapping[str, str],
) -> int:
    """
    Bytes released if every non-retained material were evicted.

    Retained materials stay in place, so storage they own or share is not
    released. This is the exact ceiling used to report infeasibility.
    """
    selected = {entry.name for entry in entries if entry.name not in retained}
    return _release(storage, selected)


def _optimize(
    entries: Sequence[_Material],
    storage: Mapping[str, _Storage],
    requested: int,
    retained: Optional[Set[str]] = None,
) -> Tuple[Optional[Set[str]], bool]:
    """
    Find a minimum-cost descendant-closed selection reaching *requested*.

    The problem decomposes into independent components: dependency edges and
    shared physical files are the only couplings. Each component is solved on
    its own, keeping only the Pareto frontier of cost versus released bytes,
    and the frontiers are combined with dynamic programming. Selections are
    represented as bitmasks so that merging thousands of independent
    materials stays cheap.

    A component whose dependency graph is not a forest and is too large to
    enumerate is solved with a bounded search; the second return value is
    then False, meaning that optimality is not proven.
    """
    if requested == 0:
        return set(), True
    retained = set(retained or ())
    names = [entry.name for entry in entries]
    bit = {name: 1 << index for index, name in enumerate(names)}
    by_name = {entry.name: entry for entry in entries}
    storage_masks = []
    storage_sizes = []
    for item in storage.values():
        if not item.owned or item.external or not item.owners:
            continue
        mask = 0
        for owner in item.owners:
            mask |= bit[owner]
        storage_masks.append(mask)
        storage_sizes.append(item.size)

    def released(mask: int) -> int:
        return sum(
            size
            for owners, size in zip(storage_masks, storage_sizes)
            if owners & mask == owners
        )

    components = _components(entries, storage, retained)
    frontier: List[Tuple[float, int, int]] = [(0.0, 0, 0)]
    exact = True
    for nodes in components:
        options, component_exact = _component_options(
            nodes, by_name, storage, retained, bit
        )
        exact = exact and component_exact
        frontier = _merge_frontiers(frontier, options)
        if not frontier:  # pragma: no cover - empty options are always feasible
            return None, exact
    eligible = [state for state in frontier if state[1] >= requested]
    if not eligible:
        return None, exact
    _, _, mask = min(eligible, key=lambda state: (state[0], state[1]))
    return {name for name in names if mask & bit[name]}, exact


def _components(
    entries: Sequence[_Material],
    storage: Mapping[str, _Storage],
    retained: Set[str],
) -> List[List[str]]:
    "Group materials connected by dependencies or shared physical files."
    names = [entry.name for entry in entries if entry.name not in retained]
    parent = {name: name for name in names}

    def find(name: str) -> str:
        while parent[name] != name:
            parent[name] = parent[parent[name]]
            name = parent[name]
        return name

    def union(first: str, second: str) -> None:
        root_a, root_b = find(first), find(second)
        if root_a != root_b:
            parent[root_b] = root_a

    by_name = {entry.name: entry for entry in entries}
    for name in names:
        for child in by_name[name].derived:
            if child in parent:
                union(name, child)
    # Files shared through hard links couple their owners: bytes only come
    # back when every link is removed.
    for item in storage.values():
        if not item.owned or item.external:
            continue
        owners = [owner for owner in item.owners if owner in parent]
        for owner in owners[1:]:
            union(owners[0], owner)
    # A path listed by several materials couples them too: deleting it for one
    # material would break the others, so the decision must be made together.
    path_owners: Dict[str, List[str]] = {}
    for entry in entries:
        for item in entry.paths:
            path_owners.setdefault(_identity(_resolve(item, None)), []).append(
                entry.name
            )
    for owners in path_owners.values():
        present = [owner for owner in owners if owner in parent]
        for owner in present[1:]:
            union(present[0], owner)
    groups: Dict[str, List[str]] = {}
    for name in names:
        groups.setdefault(find(name), []).append(name)
    return [sorted(group) for group in groups.values()]


def _component_options(
    nodes: Sequence[str],
    by_name: Mapping[str, _Material],
    storage: Mapping[str, _Storage],
    retained: Set[str],
    bit: Mapping[str, int],
) -> Tuple[List[Tuple[float, int, int]], bool]:
    """
    Enumerate the closed selections of one component.

    Every feasible selection is closed under derived materials (evicting an
    original evicts its artifacts) and under shared paths (a path used by
    several materials can only go when all of them go). Tree-shaped
    components without shared paths (the common archive-plus-artifacts case,
    including stars and chains) are solved exactly with a dynamic program.
    Other components use a budgeted enumeration; if the budget is exceeded, a
    bounded search supplies candidates and the caller is told that optimality
    is not proven.

    Returns the Pareto options as ``(cost, released, selection)`` and whether
    the enumeration was exhaustive.
    """
    node_set = set(nodes)
    component_storage = [
        item
        for item in storage.values()
        if item.owned and not item.external and set(item.owners) <= node_set
    ]
    if _dependency_tree(nodes, by_name) is not None:
        return _tree_options(nodes, by_name, component_storage, retained, bit), True

    order = _postorder(nodes, by_name)
    budget = 400_000
    visited = 0
    states: List[Tuple[float, int, int]] = []
    peers = _path_peers([by_name[name] for name in nodes])
    closure_masks = {
        name: _selectable_closure(name, by_name, peers, bit) for name in nodes
    }
    exclusion_masks = {
        name: _exclusion_mask(name, by_name, peers, bit, set(nodes))
        for name in nodes
    }
    released_cache: Dict[int, int] = {}

    def release_of(mask: int) -> int:
        value = released_cache.get(mask)
        if value is None:
            value = sum(
                item.size
                for item in component_storage
                if item.owners
                and _owners_mask(item, bit) & mask == _owners_mask(item, bit)
            )
            released_cache[mask] = value
        return value

    def walk(index: int, selected: int, excluded: int, cost: float) -> bool:
        nonlocal visited
        visited += 1
        if visited > budget:
            return False
        if index >= len(order):
            states.append((cost, release_of(selected), selected))
            return True
        name = order[index]
        mask = bit[name]
        if selected & mask:
            return walk(index + 1, selected, excluded, cost)
        if excluded & mask:
            return walk(index + 1, selected, excluded, cost)
        group = closure_masks[name]
        if not group & excluded:
            added = group & ~selected
            added_cost = sum(
                by_name[item].cost for item in nodes if added & bit[item]
            )
            if not walk(index + 1, selected | group, excluded, cost + added_cost):
                return False
        blocked = exclusion_masks[name]
        if not blocked & selected:
            return walk(index + 1, selected, excluded | blocked, cost)
        return True

    if walk(0, 0, 0, 0.0):
        return _pareto(states), True
    return _bounded_options(nodes, by_name, storage, retained, bit), False


def _exclusion_mask(
    name: str,
    by_name: Mapping[str, _Material],
    peers: Mapping[str, Set[str]],
    bit: Mapping[str, int],
    allowed: Set[str],
) -> int:
    "The material, its path peers and every ancestor of those."
    mask = 0
    stack = [name]
    seen: Set[str] = set()
    while stack:
        current = stack.pop()
        if current in seen or current not in allowed:
            continue
        seen.add(current)
        mask |= bit[current]
        stack.extend(peers.get(current, ()))
        stack.extend(by_name[current].originals)
    return mask


def _owners_mask(item: _Storage, bit: Mapping[str, int]) -> int:
    mask = 0
    for owner in item.owners:
        mask |= bit.get(owner, 0)
    return mask


def _dependency_tree(
    nodes: Sequence[str], by_name: Mapping[str, _Material]
) -> Optional[Dict[str, str]]:
    """
    Return the parent of every node if the dependency graph is a forest.

    A node's parent is its single original. Nodes with more than one original,
    or a cycle, make the component non-tree and return None.
    """
    node_set = set(nodes)
    peers = _path_peers([by_name[name] for name in nodes])
    if any(peers.get(name) for name in nodes):
        return None
    parents: Dict[str, Optional[str]] = {}
    for name in nodes:
        originals = [item for item in by_name[name].originals if item in node_set]
        if len(originals) > 1:
            return None
        parents[name] = originals[0] if originals else None
    for name in nodes:
        seen = {name}
        current = parents[name]
        while current is not None:
            if current in seen:
                return None
            seen.add(current)
            current = parents[current]
    return {name: parent for name, parent in parents.items() if parent is not None}


def _tree_options(
    nodes: Sequence[str],
    by_name: Mapping[str, _Material],
    storage: Sequence[_Storage],
    retained: Set[str],
    bit: Mapping[str, int],
) -> List[Tuple[float, int, int]]:
    """
    Exact dynamic program over a dependency tree.

    States are ``(cost, local_release, selected_mask, counts)`` where *counts*
    records how many owners of each file shared across branches have been
    selected so far. Files owned by a single node are released locally when
    that node is selected. At the roots, a shared file contributes its size
    once every owner has been selected. States are pruned per coverage tuple
    so that independent branches do not multiply.
    """
    parent_of = _dependency_tree(nodes, by_name) or {}
    children: Dict[Optional[str], List[str]] = {}
    for name in nodes:
        children.setdefault(parent_of.get(name), []).append(name)
    shared = [item for item in storage if len(item.owners) > 1]
    shared_index = {item.key: index for index, item in enumerate(shared)}
    owners_count = [len(item.owners) for item in shared]
    own_files: Dict[str, List[_Storage]] = {name: [] for name in nodes}
    owned_by: Dict[str, List[int]] = {name: [] for name in nodes}
    for item in storage:
        if len(item.owners) == 1:
            own_files[item.owners[0]].append(item)
        else:
            for owner in item.owners:
                owned_by[owner].append(shared_index[item.key])

    State = Tuple[float, int, int, Tuple[int, ...]]

    def prune(states: List[State]) -> List[State]:
        """
        Keep, per coverage tuple, the non-dominated states.

        At the same cost, several released amounts are kept: the smaller one
        minimizes the overshoot, the larger one may be the only one able to
        reach the target once this component is combined with others.
        """
        grouped: Dict[Tuple[int, ...], Dict[float, Dict[int, State]]] = {}
        for state in states:
            by_cost = grouped.setdefault(state[3], {})
            by_cost.setdefault(state[0], {}).setdefault(state[1], state)
        kept: List[State] = []
        for by_cost in grouped.values():
            best = -1
            for cost in sorted(by_cost):
                entries = {
                    released: state
                    for released, state in by_cost[cost].items()
                    if released > best
                }
                kept.extend(entries[released] for released in sorted(entries))
                if entries:
                    best = max(best, max(entries))
        return kept

    def merge(first: List[State], second: List[State]) -> List[State]:
        combined = []
        for cost_a, release_a, mask_a, counts_a in first:
            for cost_b, release_b, mask_b, counts_b in second:
                counts = tuple(
                    left + right for left, right in zip(counts_a, counts_b)
                )
                combined.append(
                    (cost_a + cost_b, release_a + release_b, mask_a | mask_b, counts)
                )
        return prune(combined)

    def solve(name: str) -> Tuple[List[State], List[State]]:
        "Return (options when not selected, options when selected)."
        base: List[State] = [(0.0, 0, 0, (0,) * len(shared))]
        child_not: List[List[State]] = []
        child_yes: List[List[State]] = []
        for child in children.get(name, []):
            not_state, yes_state = solve(child)
            child_not.append(not_state)
            child_yes.append(yes_state)

        # Not selected: every child may independently be selected or not.
        free = list(base)
        for not_state, yes_state in zip(child_not, child_yes):
            free = merge(free, not_state + yes_state)
        not_selected = prune(free)

        if name in retained:
            return not_selected, []

        # Selected: every child must be selected.
        strict = list(base)
        for yes_state in child_yes:
            strict = merge(strict, yes_state)
        selected_options: List[State] = []
        for cost, release, mask, counts in strict:
            counts = list(counts)
            for index in owned_by[name]:
                counts[index] += 1
            local = sum(item.size for item in own_files[name])
            selected_options.append(
                (
                    cost + by_name[name].cost,
                    release + local,
                    mask | bit[name],
                    tuple(counts),
                )
            )
        return not_selected, prune(selected_options)

    roots = children.get(None, [])
    result: List[State] = [(0.0, 0, 0, (0,) * len(shared))]
    for root in roots:
        not_state, yes_state = solve(root)
        result = merge(result, not_state + yes_state)

    options: List[Tuple[float, int, int]] = []
    for cost, release, mask, counts in result:
        release += sum(
            item.size
            for item, count in zip(shared, counts)
            if count >= owners_count[shared_index[item.key]]
        )
        options.append((cost, release, mask))
    return _pareto(options)


def _bounded_options(
    nodes: Sequence[str],
    by_name: Mapping[str, _Material],
    storage: Mapping[str, _Storage],
    retained: Set[str],
    bit: Mapping[str, int],
) -> List[Tuple[float, int, int]]:
    """
    Good candidates for a component too large to enumerate.

    Includes the empty selection, every single-material closure, the full
    closure and greedy sequences under three orderings.
    """
    node_set = set(nodes) - retained
    candidates: Set[int] = {0}
    node_mask = 0
    for name in nodes:
        node_mask |= bit[name]

    peers = _path_peers([by_name[name] for name in nodes])

    def closure(name: str) -> int:
        mask = 0
        stack = [name]
        seen: Set[str] = set()
        while stack:
            current = stack.pop()
            if current in seen or current not in node_set:
                continue
            seen.add(current)
            mask |= bit[current]
            stack.extend(by_name[current].derived)
            stack.extend(peers.get(current, ()))
        return mask

    for name in node_set:
        candidates.add(closure(name))
    candidates.add(node_mask)

    def standalone(name: str) -> int:
        mask = bit[name]
        return sum(
            item.size
            for item in storage.values()
            if item.owned
            and not item.external
            and _owners_mask(item, bit) == mask
        )

    orderings = [
        sorted(
            node_set,
            key=lambda name: by_name[name].cost / max(1, standalone(name)),
        ),
        sorted(node_set, key=lambda name: by_name[name].cost),
        sorted(node_set, key=lambda name: -standalone(name)),
    ]
    for order in orderings:
        selected = 0
        for name in order:
            selected |= closure(name)
            candidates.add(selected)
    states = [
        (
            sum(
                by_name[name].cost
                for name in nodes
                if selected & bit[name] and name in by_name
            ),
            _release_mask(selected, storage, bit),
            selected,
        )
        for selected in candidates
    ]
    return _pareto(states)


def _release_mask(
    mask: int, storage: Mapping[str, _Storage], bit: Mapping[str, int]
) -> int:
    total = 0
    for item in storage.values():
        if not item.owned or item.external or not item.owners:
            continue
        owners = _owners_mask(item, bit)
        if owners and owners & mask == owners:
            total += item.size
    return total


def _postorder(nodes: Sequence[str], by_name: Mapping[str, _Material]) -> List[str]:
    "Order nodes so that descendants come before their originals."
    node_set = set(nodes)
    order: List[str] = []
    done: Set[str] = set()

    def visit(name: str) -> None:
        if name in done:
            return
        done.add(name)
        for child in by_name[name].derived:
            if child in node_set:
                visit(child)
        order.append(name)

    for name in nodes:
        visit(name)
    return order


def _pareto(
    states: Sequence[Tuple[float, int, int]]
) -> List[Tuple[float, int, int]]:
    """
    Keep states that are not beaten by a strictly cheaper one.

    States with the same cost and different releases are all kept: a larger
    release is needed to reach the target, a smaller one to minimize the
    overshoot when both are feasible.
    """
    by_cost: Dict[float, Dict[int, int]] = {}
    for cost, released, selected in states:
        by_cost.setdefault(cost, {}).setdefault(released, selected)
    frontier: List[Tuple[float, int, int]] = []
    best_release = -1
    for cost in sorted(by_cost):
        entries = {
            released: selected
            for released, selected in by_cost[cost].items()
            if released > best_release
        }
        for released in sorted(entries):
            frontier.append((cost, released, entries[released]))
        if entries:
            best_release = max(best_release, max(entries))
    return frontier


def _merge_frontiers(
    first: Sequence[Tuple[float, int, int]],
    second: Sequence[Tuple[float, int, int]],
) -> List[Tuple[float, int, int]]:
    "Combine two Pareto frontiers (selections are bitmasks)."
    combined = [
        (cost_a + cost_b, release_a + release_b, mask_a | mask_b)
        for cost_a, release_a, mask_a in first
        for cost_b, release_b, mask_b in second
    ]
    return _pareto(combined)


def _build_plan(
    *,
    requested: int,
    selected: Set[str],
    entries: List[_Material],
    retained: Mapping[str, str],
    storage: Mapping[str, _Storage],
    max_releasable: int,
    root: Optional[Path],
    retained_versions: Any = (),
    retained_paths: Any = (),
    keep_materials: Any = (),
    exact: bool = True,
) -> EvictionPlan:
    by_name = {entry.name: entry for entry in entries}
    chosen = [by_name[name] for name in sorted(selected)]
    released = _release(storage, selected)
    plan = EvictionPlan(
        requested=requested,
        materials=[
            CacheMaterial(
                name=entry.name,
                size=entry.size,
                refetch_cost=entry.cost,
                originals=entry.originals,
                derived=entry.derived,
                paths=entry.paths,
                owner=entry.owner,
                leased=entry.leased,
                lease=entry.lease,
                version=entry.version,
                pinned=entry.pinned,
            )
            for entry in chosen
        ],
        released=released,
        released_logical=sum(entry.size for entry in chosen),
        cost=sum(entry.cost for entry in chosen),
        feasible=True,
        target_met=released >= requested,
        deficit=max(0, requested - released),
        max_releasable=max_releasable,
        storage={
            key: storage[key].size for key in sorted(_released(storage, selected))
        },
        retained=dict(retained),
        exact=exact,
        snapshot=_snapshot(
            entries,
            root,
            retained_versions=retained_versions,
            retained_paths=retained_paths,
            keep_materials=keep_materials,
        ),
    )
    if not plan.target_met:
        plan.reason = (
            f"best dependency-consistent selection releases {released} of "
            f"{requested} bytes"
        )
    return plan


def _infeasible_plan(
    *,
    requested: int,
    entries: List[_Material],
    retained: Mapping[str, str],
    max_releasable: int,
    reason: str,
    root: Optional[Path],
) -> EvictionPlan:
    return EvictionPlan(
        requested=requested,
        materials=[],
        released=0,
        cost=0.0,
        feasible=False,
        target_met=False,
        deficit=max(0, requested - max_releasable),
        max_releasable=max_releasable,
        reason=reason,
        retained=dict(retained),
        snapshot=_snapshot(entries, root),
    )


def _snapshot(
    entries: Sequence[_Material],
    root: Optional[Path],
    *,
    retained_versions: Any = (),
    retained_paths: Any = (),
    keep_materials: Any = (),
) -> Dict[str, Any]:
    materials: Dict[str, Any] = {}
    files: Dict[str, Dict[str, Tuple[int, int, int, int]]] = {}
    for entry in entries:
        materials[entry.name] = {
            "leased": entry.leased,
            "lease": entry.lease,
            "pinned": entry.pinned,
            "version": entry.version,
            "paths": entry.paths,
            "owner": entry.owner,
        }
        files[entry.name] = {}
        for path in entry.paths:
            full = _resolve(path, root)
            try:
                info = full.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            files[entry.name][str(full)] = _file_identity(info)
    return {
        "materials": materials,
        "files": files,
        "cache_root": str(root) if root is not None else None,
        "retained_versions": (
            (retained_versions,)
            if isinstance(retained_versions, str)
            else tuple(retained_versions or ())
        ),
        "retained_paths": tuple(str(path) for path in retained_paths or ()),
        "keep_materials": tuple(keep_materials or ()),
        "entries": [_entry_dict(entry) for entry in entries],
    }


def _entry_dict(entry: _Material) -> Dict[str, Any]:
    return {
        "name": entry.name,
        "size": entry.size,
        "refetch_cost": entry.cost,
        "originals": entry.originals,
        "derived": entry.derived,
        "paths": entry.paths,
        "owner": entry.owner,
        "leased": entry.leased,
        "lease": entry.lease,
        "version": entry.version,
        "pinned": entry.pinned,
    }


def _entries_from_plan(plan: EvictionPlan) -> List[_Material]:
    entries = plan.snapshot.get("entries")
    if not entries:
        entries = [
            {
                "name": material.name,
                "size": material.size,
                "refetch_cost": material.refetch_cost,
                "originals": material.originals,
                "derived": material.derived,
                "paths": material.paths,
                "owner": material.owner,
                "leased": material.leased,
                "lease": material.lease,
                "version": material.version,
                "pinned": material.pinned,
            }
            for material in plan.materials
        ]
    return _with_dependencies(_normalize(copy.deepcopy(entries)))


def _resolve_state(materials: Union[StateProvider, None], plan: EvictionPlan) -> Any:
    if materials is None:
        return copy.deepcopy(plan.snapshot.get("entries", []))
    if callable(materials):
        return materials()
    return materials


def _snapshot_value(
    plan: EvictionPlan, key: str, override: Any
) -> Any:
    if override is not None:
        return override
    return plan.snapshot.get(key, ())


def _file_identity(info: os.stat_result) -> Tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _changed_files(
    plan: EvictionPlan, entries: Sequence[_Material], root: Optional[Path]
) -> Dict[str, str]:
    "Report materials whose files differ from the planning snapshot."
    previous = plan.snapshot.get("files", {})
    changed: Dict[str, str] = {}
    for entry in entries:
        old = previous.get(entry.name)
        if old is None:
            continue
        for path in entry.paths:
            full = str(_resolve(path, root))
            try:
                info = _resolve(path, root).lstat()
            except OSError:
                if full in old:
                    changed[entry.name] = "file disappeared after planning"
                    break
                continue
            if old.get(full) is not None and old[full] != _file_identity(info):
                changed[entry.name] = "file changed after planning"
                break
    return changed


def _enforce_closure(
    candidates: Set[str], by_name: Mapping[str, _Material], result: EvictionResult
) -> Set[str]:
    "Drop materials whose descendants cannot be evicted."
    changed = True
    while changed:
        changed = False
        for name in sorted(candidates):
            blocked = {
                descendant
                for descendant in _descendants(name, by_name)
                if descendant not in candidates
            }
            if blocked:
                candidates.discard(name)
                result.skipped[name] = (
                    "cannot evict because derived material(s) "
                    + ", ".join(sorted(blocked))
                    + " cannot be evicted"
                )
                changed = True
    return candidates


def _descendants(name: str, by_name: Mapping[str, _Material]) -> Set[str]:
    "Every material that must be evicted together with *name*."
    result: Set[str] = set()
    stack = list(by_name[name].derived) if name in by_name else []
    while stack:
        current = stack.pop()
        if current in result:
            continue
        result.add(current)
        entry = by_name.get(current)
        if entry is not None:
            stack.extend(entry.derived)
    return result


def _deletion_order(
    candidates: Set[str], by_name: Mapping[str, _Material]
) -> List[str]:
    "Delete derived artifacts before their originals."
    ordered: List[str] = []
    remaining = set(candidates)
    while remaining:
        ready = sorted(
            name
            for name in remaining
            if not (_descendants(name, by_name) & remaining)
        )
        if not ready:
            # Cycles are rejected during planning; this is a safe fallback.
            ready = sorted(remaining)
        ordered.extend(ready)
        remaining -= set(ready)
    return ordered


def _check_files(
    name: str,
    paths: Sequence[str],
    expected: Mapping[str, Mapping[str, Sequence[int]]],
    root: Optional[Path],
) -> Optional[str]:
    "Return why the files of a material cannot be deleted, if they cannot."
    planned = expected.get(name, {})
    for item in paths:
        path = _resolve(item, root)
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            return str(error)
        identity = planned.get(str(path))
        if identity is None:
            return "file was not present when planning"
        if _file_identity(info) != tuple(identity):
            return "file changed after planning"
        if not stat.S_ISREG(info.st_mode):
            return f"not a regular file: {path}"
    return None


def _inode_snapshot(storage: Mapping[str, _Storage]) -> Dict[str, Dict[str, Any]]:
    "Capture the identity of every physical file before deletion."
    snapshot: Dict[str, Dict[str, Any]] = {}
    for key, item in storage.items():
        info = _stat(item.path)
        if info is None:
            continue
        snapshot[key] = {
            "size": info.st_size,
            "nlink": info.st_nlink,
            "owned": item.owned,
            "external": item.external,
        }
    return snapshot


def _released_bytes(
    before: Mapping[str, Mapping[str, Any]],
    after: Mapping[str, _Storage],
) -> int:
    """
    Bytes actually freed between two storage inspections.

    A file counts only when the cache owned it, it had no links outside the
    cache, and it no longer exists afterwards. The last check covers files
    still reachable through a material that stayed or a link created while
    the deletion was running: those bytes were not freed.
    """
    released = 0
    for key, old in before.items():
        if not old["owned"] or old["external"]:
            continue
        if key not in after:
            released += int(old["size"])
    return released
