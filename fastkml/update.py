# Copyright (C) 2025 Christian Ledermann
#
# This library is free software; you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the Free
# Software Foundation; either version 2.1 of the License, or (at your option)
# any later version.
#
# This library is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public License for more
# details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this library; if not, write to the Free Software Foundation, Inc.,
# 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
"""
Apply KML ``<Update>`` operations (``Create``/``Change``/``Delete``) to an object tree.

The :class:`~fastkml.network_link_control.Update` element parsed from a
``NetworkLinkControl`` only describes *what* should change.  The functions in this
module carry those changes out against an already loaded KML object tree, following
the rules of the `KML updates specification`_:

- Objects are located by their stable ``id`` (referenced through ``targetId``).
  Unknown targets are never turned into new objects of the same name.
- ``Create`` adds the new children to the referenced container; an already present
  ``id`` is not appended a second time.
- ``Change`` is a sparse update: only the values carried by the change object are
  copied onto the object with the matching ``id``; everything else is untouched.
- ``Delete`` removes the referenced feature.  Other objects that pointed at the
  deleted object (e.g. ``styleUrl``/``schemaUrl`` references) are reported instead of
  being silently left dangling.

Every individual operation (one object inside a ``Create``/``Change``/``Delete``)
produces an :class:`UpdateResult` that records what happened and why, so callers keep
a full provenance trail of each step.

.. _KML updates specification:
    https://developers.google.com/kml/documentation/updates
"""

import logging
import urllib.parse as urlparse
from collections.abc import Iterable
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import replace
from typing import Any

from fastkml.base import _XMLObject
from fastkml.kml_base import _BaseObject
from fastkml.network_link_control import Change
from fastkml.network_link_control import Create
from fastkml.network_link_control import Delete
from fastkml.network_link_control import Update
from fastkml.registry import registry
from fastkml.styles import StyleUrl
from fastkml.utils import find_all

__all__ = [
    "UpdateResult",
    "apply_update",
    "apply_updates",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UpdateResult:
    """
    Outcome of applying a single ``Create``/``Change``/``Delete`` object.

    Attributes
    ----------
    operation:
        The kind of operation that was applied: ``"create"``, ``"change"`` or
        ``"delete"``.
    target_id:
        The stable ``id`` that the operation referenced (``targetId``); the empty
        string when the operation did not carry one.
    target_href:
        The ``targetHref`` of the update document this step came from, i.e. the
        source that requested the change.  ``None`` when not available.
    applied:
        Whether the operation changed the target tree.
    reason:
        Human readable explanation.  Set when nothing was applied (the step was
        skipped or its target was unknown), and also set to an informational note
        when an applied ``Create`` skipped children whose ids already existed.
        ``None`` when the step was applied in full.
    object:
        The KML object the operation acted on (the created, changed or deleted
        object), or ``None`` when no target could be resolved.
    affected_references:
        Stable ``id``s of objects that hold a reference (for example a
        ``styleUrl``) to a deleted object and are therefore left dangling.  Only
        populated for deletions.

    """

    operation: str
    target_id: str
    applied: bool
    target_href: str | None = None
    reason: str | None = None
    object: _BaseObject | None = None
    affected_references: frozenset[str] = frozenset()

    def __bool__(self) -> bool:
        """Return whether this step was applied to the tree."""
        return self.applied


def _iter_base_objects(root: object) -> Iterable[_BaseObject]:
    """Yield every KML object carrying an ``id`` below ``root``."""
    yield from find_all(root, of_type=_BaseObject)


def _index_by_id(root: object) -> dict[str, _BaseObject]:
    """Build an ``id -> object`` map of every identified object in the tree."""
    return {obj.id: obj for obj in _iter_base_objects(root) if obj.id}


def _reference_fragment(url: str | None) -> str:
    """Return the ``#fragment`` (the referenced local ``id``) of a URL."""
    if not url:
        return ""
    return urlparse.urlparse(url).fragment


def _style_url_of(holder: Any) -> StyleUrl | None:
    """Return the ``StyleUrl`` of a feature or ``Pair``, if any."""
    style_url = getattr(holder, "style_url", None)
    if style_url is not None:
        return style_url
    pair_style = getattr(holder, "style", None)
    if isinstance(pair_style, StyleUrl):
        return pair_style
    return None


def _referenced_ids(obj: _BaseObject) -> set[str]:
    """Collect the local ``id`` fragments referenced by ``obj``."""
    referenced: set[str] = set()
    style_url = _style_url_of(obj)
    if style_url is not None:
        referenced.add(_reference_fragment(style_url.url))
    for pair in getattr(obj, "pairs", []):
        pair_style_url = _style_url_of(pair)
        if pair_style_url is not None:
            referenced.add(_reference_fragment(pair_style_url.url))
    referenced.add(
        _reference_fragment(getattr(obj, "schema_url", None)),
    )
    referenced.discard("")
    return referenced


def _find_referrers(root: object, removed_ids: set[str]) -> frozenset[str]:
    """
    Find objects outside the removed subtree that reference a removed id.

    A parent deletion removes the target together with every identified object
    below it.  Any object that stays in the tree but holds a ``styleUrl`` or
    ``schemaUrl`` reference to one of the removed ids is left dangling and is
    reported here by its own stable ``id``.  Referrers without an explicit
    ``id`` cannot be reported and are skipped.
    """
    referrers: set[str] = set()
    for obj in _iter_base_objects(root):
        if obj.id in removed_ids:
            continue
        if obj.id and _referenced_ids(obj) & removed_ids:
            referrers.add(obj.id)
    return frozenset(referrers)


def _remove_from_parent(root: object, child: _BaseObject) -> bool:
    """
    Detach ``child`` from the list that owns it.

    Walks ``root`` with :func:`find_all` and removes ``child`` from the first
    parent attribute that contains it, covering feature lists, nested geometry
    lists, style/pair lists and every other registered child list.  Returns
    whether a containing list was found.
    """
    for candidate in find_all(root, of_type=_XMLObject):
        if candidate is child:
            continue
        for value in vars(candidate).values():
            if isinstance(value, list) and any(item is child for item in value):
                value.remove(child)
                return True
    return False


def _registry_attributes(obj: _BaseObject) -> Sequence[str]:
    """Return the serialized attribute names registered for ``obj``'s class."""
    return tuple(item.attr_name for item in registry.get(obj.__class__))


def _sparse_copy(target: _BaseObject, source: _BaseObject) -> None:
    """
    Copy the values present on ``source`` onto ``target`` without clearing others.

    Only attributes that are actually serialized (registered for the class) and
    whose value is set on ``source`` are copied, so a sparse ``Change`` never
    resets the fields it does not mention.  The identifying attributes
    (``id``/``targetId``) are never overwritten.
    """
    excluded = {"id", "target_id"}
    for attr_name in _registry_attributes(source):
        if attr_name in excluded:
            continue
        value = getattr(source, attr_name, None)
        if value is None or value == "" or (isinstance(value, list) and not value):
            continue
        setattr(target, attr_name, value)


def _apply_create(
    *,
    obj: _BaseObject,
    index: dict[str, _BaseObject],
) -> UpdateResult:
    """
    Apply one object of a ``Create`` operation.

    ``obj`` is the container carrying the ``targetId`` of the parent container;
    the container itself is not added.  Its child features are appended to the
    target container, matching the KML ``Create`` semantics.
    """
    target_id = obj.target_id
    target = index.get(target_id) if target_id else None
    if not target_id:
        return UpdateResult(
            operation="create",
            target_id=target_id,
            applied=False,
            reason="Create object has no targetId referencing a container",
            object=obj,
        )
    if target is None:
        return UpdateResult(
            operation="create",
            target_id=target_id,
            applied=False,
            reason=f"Unknown target container with id {target_id!r}",
            object=obj,
        )
    features = getattr(target, "features", None)
    if not isinstance(features, list):
        return UpdateResult(
            operation="create",
            target_id=target_id,
            applied=False,
            reason=f"Target with id {target_id!r} is not a container",
            object=obj,
        )
    new_features = list(getattr(obj, "features", []))
    if not new_features:
        return UpdateResult(
            operation="create",
            target_id=target_id,
            applied=False,
            reason="Create object carries no features to add",
            object=obj,
        )
    duplicate_ids = sorted(
        {child.id for child in new_features if child.id and child.id in index},
    )
    added = [
        child for child in new_features if not (child.id and child.id in index)
    ]
    if not added:
        return UpdateResult(
            operation="create",
            target_id=target_id,
            applied=False,
            reason=(
                f"Object(s) with id(s) {duplicate_ids!r} already exist; "
                "not appending again"
            ),
            object=obj,
        )
    features.extend(added)
    for child in added:
        index.update(
            {nested.id: nested for nested in _iter_base_objects(child) if nested.id},
        )
    reason = (
        f"Skipped already existing object(s) with id(s) {duplicate_ids!r}"
        if duplicate_ids
        else None
    )
    return UpdateResult(
        operation="create",
        target_id=target_id,
        applied=True,
        reason=reason,
        object=obj,
    )


def _apply_change(
    *,
    obj: _BaseObject,
    index: dict[str, _BaseObject],
) -> UpdateResult:
    """Apply one object of a ``Change`` operation."""
    target_id = obj.target_id
    target = index.get(target_id) if target_id else None
    if target is None:
        return UpdateResult(
            operation="change",
            target_id=target_id,
            applied=False,
            reason=f"Unknown target with id {target_id!r}; no replacement is created",
            object=obj,
        )
    if type(target) is not type(obj):
        return UpdateResult(
            operation="change",
            target_id=target_id,
            applied=False,
            reason=(
                f"Target id {target_id!r} is a {type(target).__name__}, "
                f"but the change provides a {type(obj).__name__}"
            ),
            object=obj,
        )
    _sparse_copy(target, obj)
    return UpdateResult(
        operation="change",
        target_id=target_id,
        applied=True,
        object=target,
    )


def _apply_delete(
    *,
    obj: _BaseObject,
    root: object,
    index: dict[str, _BaseObject],
) -> UpdateResult:
    """Apply one object of a ``Delete`` operation."""
    target_id = obj.target_id or obj.id
    target = index.get(target_id) if target_id else None
    if target is None:
        return UpdateResult(
            operation="delete",
            target_id=target_id,
            applied=False,
            reason=f"Unknown target with id {target_id!r}; nothing to delete",
            object=obj,
        )
    removed_ids = {
        nested.id for nested in _iter_base_objects(target) if nested.id
    }
    affected = _find_referrers(root, removed_ids)
    if not _remove_from_parent(root, target):
        return UpdateResult(
            operation="delete",
            target_id=target_id,
            applied=False,
            reason=f"Object with id {target_id!r} has no parent in the target tree",
            object=target,
            affected_references=affected,
        )
    for removed_id in removed_ids:
        index.pop(removed_id, None)
    return UpdateResult(
        operation="delete",
        target_id=target_id,
        applied=True,
        object=target,
        affected_references=affected,
    )


def apply_update(root: object, update: Update) -> list[UpdateResult]:
    """
    Apply one :class:`~fastkml.network_link_control.Update` to an object tree.

    The ``Update``'s operations are processed in document order.  Each operation's
    objects are located through their stable ``id``/``targetId`` within ``root``.
    The tree is modified in place; the returned :class:`UpdateResult` objects keep
    the provenance of every individual step.

    Parameters
    ----------
    root : object
        The loaded KML tree to update, typically a
        :class:`~fastkml.kml.KML` or :class:`~fastkml.containers.Document`.
    update : Update
        The parsed update carrying the ``target_href`` and the ordered
        ``Create``/``Change``/``Delete`` operations.

    Returns
    -------
    list[UpdateResult]
        One result per object inside every operation, in processing order.

    """
    index = _index_by_id(root)
    results: list[UpdateResult] = []
    for operation in update.operations:
        for obj in operation.objects:
            if isinstance(operation, Create):
                result = _apply_create(obj=obj, index=index)
            elif isinstance(operation, Change):
                result = _apply_change(obj=obj, index=index)
            elif isinstance(operation, Delete):
                result = _apply_delete(obj=obj, root=root, index=index)
            else:  # pragma: no cover - all subclasses are handled above
                continue
            result = replace(result, target_href=update.target_href)
            results.append(result)
            if not result.applied:
                logger.warning(
                    "Update step %s on targetId %r was not applied: %s",
                    result.operation,
                    result.target_id,
                    result.reason,
                )
    return results


def apply_updates(
    root: object,
    updates: Iterable[Update | None],
) -> list[UpdateResult]:
    """
    Apply several :class:`~fastkml.network_link_control.Update` objects in order.

    Each update sees the tree left behind by the previous one, so an object created
    by an earlier update can be changed or deleted by a later one.  Replaying the
    same update twice does not duplicate any created object: the second ``Create``
    finds the existing ``id`` and is reported as not applied.

    Parameters
    ----------
    root : object
        The loaded KML tree to update.
    updates : Iterable[Update | None]
        The updates to apply, in order.  ``None`` entries (for example an
        ``Update`` without a ``target_href``) are skipped.

    Returns
    -------
    list[UpdateResult]
        The concatenated results of every step of every update, in order.

    """
    results: list[UpdateResult] = []
    for update in updates:
        if update is None or not update:
            continue
        results.extend(apply_update(root, update))
    return results
