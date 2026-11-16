# Copyright (C) 2026  Christian Ledermann
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
"""Evaluate which features of an offline package are active for a viewport.

The KML Region hierarchy is treated as an activation gate: a feature whose own
Region is inactive (or whose ancestor Region is inactive) must not be loaded on
its own hit alone. Features without a Region inherit the decision of their
closest ancestor that carries one. Missing evidence is reported as
``undetermined`` rather than guessed as covered.
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastkml.enums import RegionState
from fastkml.features import NetworkLink
from fastkml.views import Viewport

if TYPE_CHECKING:
    from collections.abc import Iterator

    from fastkml.features import _Feature

__all__ = ["ActiveFeature", "active_features", "evaluate_features"]


@dataclass(frozen=True)
class ActiveFeature:
    """
    A feature together with its evaluated activation state.

    ``hrefs`` lists the network link targets of the feature (a single href for
    a NetworkLink, otherwise an empty tuple).
    """

    feature: "_Feature"
    state: RegionState
    hrefs: tuple[str, ...]

    @property
    def is_active(self) -> bool | None:
        """True/False for a decided state, None for an undetermined one."""
        if self.state == RegionState.active:
            return True
        if self.state == RegionState.inactive:
            return False
        return None


def _feature_state(
    feature: "_Feature",
    parent_state: RegionState,
    viewport: Viewport,
) -> RegionState:
    """
    Combine the parent gate with the feature's own Region evidence.

    An inactive ancestor always wins. A feature without its own Region inherits
    the parent state; otherwise the two pieces of evidence are combined so that
    no missing evidence is ever promoted to coverage.
    """
    if parent_state == RegionState.inactive:
        return RegionState.inactive
    region = getattr(feature, "region", None)
    if region is None:
        return parent_state
    own_state = region.state(viewport)
    if own_state == RegionState.inactive:
        return RegionState.inactive
    if (
        own_state == RegionState.undetermined
        or parent_state == RegionState.undetermined
    ):
        return RegionState.undetermined
    return RegionState.active


def _hrefs(feature: "_Feature") -> tuple[str, ...]:
    """Return the network link targets carried by the feature."""
    if isinstance(feature, NetworkLink) and feature.link is not None:
        href = feature.link.href
        if href:
            return (href,)
    return ()


def evaluate_features(
    root: object,
    viewport: Viewport,
) -> "Iterator[ActiveFeature]":
    """
    Walk the feature tree and yield each descendant feature with its state.

    The tree may be a :class:`~fastkml.kml.KML` document, a Document or a
    Folder. The root object itself is not yielded, but its Region (when
    present) acts as the initial gate. Regions on every ancestor container
    gate their descendants: a restricted child cannot be loaded merely because
    its own Region hits when an ancestor Region is inactive.

    Args:
    ----
        root: The root object holding a ``features`` hierarchy.
        viewport: The current viewport with geographic, zoom and altitude data.

    Yields:
    ------
        ActiveFeature for every descendant feature, depth first.

    """
    root_region = getattr(root, "region", None)
    root_state = (
        root_region.state(viewport) if root_region is not None else RegionState.active
    )
    yield from _walk(root, viewport, root_state)


def _walk(
    container: object,
    viewport: Viewport,
    parent_state: RegionState,
) -> "Iterator[ActiveFeature]":
    for feature in getattr(container, "features", None) or ():
        state = _feature_state(feature, parent_state, viewport)
        yield ActiveFeature(
            feature=feature,
            state=state,
            hrefs=_hrefs(feature),
        )
        children = getattr(feature, "features", None)
        if children:
            yield from _walk(feature, viewport, state)


def active_features(
    root: object,
    viewport: Viewport,
) -> tuple[ActiveFeature, ...]:
    """
    Return only the features that are provably active for the viewport.

    Inactive features and features whose state is undetermined due to missing
    evidence are excluded; undetermined features are never guessed as covered.
    """
    return tuple(
        result
        for result in evaluate_features(root, viewport)
        if result.state == RegionState.active
    )
