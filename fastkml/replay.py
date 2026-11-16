# Copyright (C) 2026 Christian Ledermann
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
Offline replay of map browsing sessions against NetworkLink/Region trees.

The :class:`ReplayEngine` walks a timestamped viewport trajectory through a
static KML document and decides, for every :class:`~fastkml.features.NetworkLink`,
at which instants its ``<Link>`` must be requested, reused from the cache or
released.

The evaluation is fully offline and deterministic:

* Region activity follows the declared ``<LatLonAltBox>``/``<Lod>`` exactly,
  including antimeridian-crossing boxes (``east < west``) and nested regions
  whose parent becomes inactive.
* A quick move out of a region and back in does not trigger a new request when
  the cached entry is still within its validity period; the decision is based
  on the cached entry's expiry time, not on the current position alone.
* A target (href) shared by several network links keeps one activation reason
  per reference. One reference going away only releases the shared target when
  the last reference drops it, and the cached entry itself survives until its
  validity period ends.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from typing import TYPE_CHECKING

from fastkml.containers import Document
from fastkml.containers import Folder
from fastkml.enums import RefreshMode
from fastkml.enums import ViewRefreshMode
from fastkml.features import NetworkLink

if TYPE_CHECKING:
    from collections.abc import Iterable
    from collections.abc import Sequence

    from fastkml.views import Region

__all__ = [
    "Action",
    "LinkEvent",
    "ReplayConfig",
    "ReplayEngine",
    "Viewport",
    "collect_network_links",
]


class Action(str, Enum):
    """What the replay engine asks a client to do with a link."""

    request = "request"
    reuse = "reuse"
    release = "release"


@dataclass(frozen=True)
class Viewport:
    """
    A snapshot of the map view at one point in time.

    ``west``, ``south``, ``east`` and ``north`` are viewport bounds in
    geographic degrees. A viewport that crosses the antimeridian is declared as
    such with ``east < west`` (for example ``west=170.0, east=-170.0``).
    ``pixel_span`` is the projected size of the viewport's longest edge in
    pixels; it drives the LOD comparisons.
    """

    timestamp: float
    west: float
    south: float
    east: float
    north: float
    pixel_span: float = 0.0


@dataclass(frozen=True)
class ReplayConfig:
    """
    Tunable timing parameters for the offline replay.

    ``default_cache_seconds`` is the simulated validity period used when neither
    the ``<Link>`` nor a fetched ``<NetworkLinkControl>`` declares an expiry.
    ``view_refresh_seconds`` overrides ``<viewRefreshTime>`` for ``onStop``
    view mode when the document omits it. ``time_refresh_seconds`` overrides
    ``<refreshInterval>`` for ``onInterval`` time mode when it is omitted.
    """

    default_cache_seconds: float = 300.0
    view_refresh_seconds: float = 4.0
    time_refresh_seconds: float = 4.0


@dataclass(frozen=True)
class LinkEvent:
    """A single request/reuse/release decision for one network link."""

    timestamp: float
    link_id: str
    href: str
    action: Action
    reasons: frozenset[str] = field(default_factory=frozenset)


def _intervals_overlap(
    west_a: float,
    east_a: float,
    west_b: float,
    east_b: float,
) -> bool:
    """
    Return whether two longitude intervals intersect.

    Either interval may cross the antimeridian, which is declared by an east
    edge smaller than the west edge. The declarations are taken literally and
    are never normalised.
    """
    if east_a >= west_a and east_b >= west_b:
        return west_a <= east_b and west_b <= east_a
    if east_a < west_a and east_b < west_b:
        return True
    if east_a < west_a:
        return west_b <= east_a or west_a <= east_b
    return west_a <= east_b or west_b <= east_a


def _box_intersects(
    region: Region,
    west: float,
    south: float,
    east: float,
    north: float,
) -> bool:
    """Return whether the region's LatLonAltBox intersects the viewport box."""
    box = region.lat_lon_alt_box
    if box is None:
        return True
    if box.south is not None and box.south > north:
        return False
    if box.north is not None and box.north < south:
        return False
    if box.west is None or box.east is None:
        return True
    return _intervals_overlap(box.west, box.east, west, east)


def _lod_active(region: Region, pixel_span: float) -> bool:
    """Return whether the region's LOD ramp is active at ``pixel_span``."""
    lod = region.lod
    if lod is None:
        return True
    min_pixels = lod.min_lod_pixels if lod.min_lod_pixels is not None else 0
    within_min = pixel_span >= min_pixels
    max_pixels = lod.max_lod_pixels
    within_max = max_pixels is None or max_pixels == -1 or pixel_span <= max_pixels
    return within_min and within_max


def region_is_active(region: Region | None, viewport: Viewport) -> bool:
    """
    Evaluate a region against a viewport, following the declared geometry.

    The check is purely geometric: antimeridian-crossing boxes and LOD ramps
    are applied as written. Hierarchical deactivation is handled separately by
    :class:`ReplayEngine`, which keeps the ancestor chain in view.
    """
    if region is None:
        return True
    return _box_intersects(
        region,
        viewport.west,
        viewport.south,
        viewport.east,
        viewport.north,
    ) and _lod_active(region, viewport.pixel_span)


def collect_network_links(
    root: object,
) -> list[tuple[NetworkLink, tuple[Region | None, ...]]]:
    """
    Collect every NetworkLink under ``root`` with its ancestor regions.

    The returned tuple of regions is ordered from the document root down to the
    network link's own feature region. A network link is only active when every
    region in the chain is active, so a deactivated parent hides the link even
    when its own region still matches the viewport.
    """
    found: list[tuple[NetworkLink, tuple[Region | None, ...]]] = []

    def walk(node: object, chain: tuple[Region | None, ...]) -> None:
        features = getattr(node, "features", None)
        if features is None:
            return
        for feature in features:
            extended_chain = (*chain, getattr(feature, "region", None))
            if isinstance(feature, NetworkLink):
                found.append((feature, extended_chain))
            if isinstance(feature, (Document, Folder)):
                walk(feature, extended_chain)

    walk(root, ())
    return found


@dataclass
class _CacheEntry:
    """
    Bookkeeping for one fetched target href.

    An entry outlives the references that fetched it: once every reference has
    been deactivated it is kept idle until ``expires_at`` so that a quick
    out-and-in move can still be served from the cache.
    """

    href: str
    fetched_at: float
    expires_at: float
    references: set[str]


class ReplayEngine:
    """
    Replay a viewport trajectory and schedule NetworkLink fetches.

    The engine never performs network input or output. It consumes the
    timestamped :class:`Viewport` trajectory together with the NetworkLink
    hierarchy parsed from a KML document and emits the ordered
    :class:`LinkEvent` decisions a client would have made.
    """

    def __init__(
        self,
        root: object,
        trajectory: Iterable[Viewport],
        config: ReplayConfig | None = None,
    ) -> None:
        """
        Build an engine for the NetworkLinks under ``root``.

        ``trajectory`` is reordered by timestamp; ``config`` supplies the
        default cache and refresh timings when the document omits them.
        """
        self._links = collect_network_links(root)
        self._trajectory = sorted(trajectory, key=lambda viewport: viewport.timestamp)
        self._config = config or ReplayConfig()
        self._cache: dict[str, _CacheEntry] = {}
        self._active: dict[str, set[str]] = {}
        self._events: list[LinkEvent] = []
        self._expiry_overrides: dict[str, float] = {}
        self._time_deadlines: dict[tuple[str, str], float] = {}
        self._view_deadlines: dict[tuple[str, str], float | None] = {}
        self._last_viewports: dict[str, Viewport] = {}

    @property
    def events(self) -> tuple[LinkEvent, ...]:
        """The decisions emitted so far, in chronological order."""
        return tuple(self._events)

    def set_expiry(self, href: str, expires_at: float) -> None:
        """
        Register the ``expires`` value of a fetched NetworkLinkControl.

        Offline replays learn server-provided expiry times this way. The value
        governs every cached entry sharing the href, including entries held by
        more than one network link.
        """
        self._expiry_overrides[href] = expires_at
        entry = self._cache.get(href)
        if entry is not None:
            entry.expires_at = expires_at

    def run(self) -> tuple[LinkEvent, ...]:
        """Replay the whole trajectory and return the ordered decisions."""
        for viewport in self._trajectory:
            self._step(viewport)
        if self._trajectory:
            end = self._trajectory[-1].timestamp
            for link, _regions in self._links:
                self._deactivate(link, end, "end-of-trace")
        return self.events

    def _step(self, viewport: Viewport) -> None:
        timestamp = viewport.timestamp
        wanted: dict[str, list[str]] = {}
        inactive_reasons: dict[str, str] = {}
        active_now: set[str] = set()
        for link, regions in self._links:
            link_id = self._link_id(link)
            if link.visibility is False:
                inactive_reasons[link_id] = "invisible"
                continue
            reason = self._chain_failure_reason(regions, viewport)
            if reason is not None:
                inactive_reasons[link_id] = reason
                continue
            href = link.link.href if link.link is not None else None
            if not href:
                continue
            wanted.setdefault(href, []).append(link_id)
            active_now.add(link_id)
        for link, _regions in self._links:
            link_id = self._link_id(link)
            if link_id not in active_now and link_id in self._active:
                self._deactivate(
                    link,
                    timestamp,
                    inactive_reasons.get(link_id, "region-inactive"),
                )
        for href, link_ids in wanted.items():
            self._evaluate_href(href, link_ids, viewport)

    def _chain_failure_reason(
        self,
        regions: tuple[Region | None, ...],
        viewport: Viewport,
    ) -> str | None:
        """Return why the ancestor region chain is inactive, or ``None``."""
        for depth, region in enumerate(regions):
            if region is None:
                continue
            if not region_is_active(region, viewport):
                if depth == len(regions) - 1:
                    return "region-inactive"
                return "parent-region-inactive"
        return None

    def _evaluate_href(
        self,
        href: str,
        link_ids: Sequence[str],
        viewport: Viewport,
    ) -> None:
        timestamp = viewport.timestamp
        entry = self._cache.get(href)
        if entry is None:
            self._fetch(href, link_ids, viewport, frozenset({"activate"}))
            return
        if timestamp >= entry.expires_at:
            self._fetch(href, link_ids, viewport, frozenset({"expired"}))
            return
        interval_links = [
            link_id
            for link_id in link_ids
            if self._deadline_due(self._time_deadlines, link_id, href, timestamp)
        ]
        if interval_links:
            self._fetch(href, link_ids, viewport, frozenset({"on-interval"}))
            return
        stop_links = [
            link_id
            for link_id in link_ids
            if self._on_stop_due(link_id, href, viewport)
        ]
        if stop_links:
            self._fetch(href, link_ids, viewport, frozenset({"on-stop"}))
            return
        reasons = frozenset(
            {"reactivate"}
            if any(href not in self._active.get(link_id, set()) for link_id in link_ids)
            else {"cached"},
        )
        self._reuse(href, link_ids, timestamp, reasons)
        self._track_viewports(link_ids, href, viewport)

    def _fetch(
        self,
        href: str,
        link_ids: Sequence[str],
        viewport: Viewport,
        reasons: frozenset[str],
    ) -> None:
        timestamp = viewport.timestamp
        entry = self._cache.get(href)
        if entry is None:
            entry = _CacheEntry(
                href=href,
                fetched_at=timestamp,
                expires_at=self._expiry_for(href, timestamp),
                references=set(),
            )
            self._cache[href] = entry
        else:
            entry.fetched_at = timestamp
            entry.expires_at = self._expiry_for(href, timestamp)
        self._emit(timestamp, link_ids, href, Action.request, reasons)
        for link_id in link_ids:
            entry.references.add(link_id)
            self._active.setdefault(link_id, set()).add(href)
            self._arm_timers(link_id, href, timestamp)
        self._track_viewports(link_ids, href, viewport)

    def _reuse(
        self,
        href: str,
        link_ids: Sequence[str],
        timestamp: float,
        reasons: frozenset[str],
    ) -> None:
        entry = self._cache[href]
        for link_id in link_ids:
            entry.references.add(link_id)
            self._active.setdefault(link_id, set()).add(href)
        self._emit(timestamp, link_ids, href, Action.reuse, reasons)

    def _deactivate(
        self,
        link: NetworkLink,
        timestamp: float,
        reason: str,
    ) -> None:
        link_id = self._link_id(link)
        hrefs = self._active.pop(link_id, set())
        for href in hrefs:
            entry = self._cache.get(href)
            if entry is not None:
                entry.references.discard(link_id)
            self._time_deadlines.pop((link_id, href), None)
            self._view_deadlines.pop((link_id, href), None)
            self._last_viewports.pop(link_id, None)
            self._emit(
                timestamp,
                [link_id],
                href,
                Action.release,
                frozenset({reason}),
            )

    def _on_stop_due(
        self,
        link_id: str,
        href: str,
        viewport: Viewport,
    ) -> bool:
        link = self._link_by_id(link_id)
        if self._view_refresh_mode(link) != ViewRefreshMode.on_stop:
            return False
        previous = self._last_viewports.get(link_id)
        if previous is None:
            self._view_deadlines[(link_id, href)] = None
            return False
        key = self._viewport_key(viewport)
        if key != self._viewport_key(previous):
            delay = link.link.view_refresh_time if link.link is not None else None
            if delay is None:
                delay = self._config.view_refresh_seconds
            self._view_deadlines[(link_id, href)] = viewport.timestamp + delay
            return False
        deadline = self._view_deadlines.get((link_id, href))
        if deadline is not None and viewport.timestamp >= deadline:
            self._view_deadlines[(link_id, href)] = None
            return True
        return False

    def _track_viewports(
        self,
        link_ids: Sequence[str],
        href: str,
        viewport: Viewport,
    ) -> None:
        for link_id in link_ids:
            link = self._link_by_id(link_id)
            self._last_viewports[link_id] = viewport
            if self._view_refresh_mode(link) == ViewRefreshMode.on_stop:
                self._view_deadlines.setdefault((link_id, href), None)

    def _arm_timers(self, link_id: str, href: str, timestamp: float) -> None:
        link = self._link_by_id(link_id)
        if self._refresh_mode(link) == RefreshMode.on_interval:
            interval = link.link.refresh_interval if link.link is not None else None
            if interval is None:
                interval = self._config.time_refresh_seconds
            self._time_deadlines[(link_id, href)] = timestamp + interval
        else:
            self._time_deadlines.pop((link_id, href), None)

    @staticmethod
    def _deadline_due(
        deadlines: dict[tuple[str, str], float],
        link_id: str,
        href: str,
        timestamp: float,
    ) -> bool:
        deadline = deadlines.get((link_id, href))
        return deadline is not None and timestamp >= deadline

    def _expiry_for(self, href: str, fetched_at: float) -> float:
        override = self._expiry_overrides.get(href)
        if override is not None:
            return override
        return fetched_at + self._config.default_cache_seconds

    def _emit(
        self,
        timestamp: float,
        link_ids: Sequence[str],
        href: str,
        action: Action,
        reasons: frozenset[str],
    ) -> None:
        for link_id in link_ids:
            self._events.append(
                LinkEvent(
                    timestamp=timestamp,
                    link_id=link_id,
                    href=href,
                    action=action,
                    reasons=reasons,
                ),
            )

    def _link_by_id(self, link_id: str) -> NetworkLink:
        for link, _regions in self._links:
            if self._link_id(link) == link_id:
                return link
        msg = f"unknown network link id {link_id!r}"
        raise KeyError(msg)

    @staticmethod
    def _link_id(link: NetworkLink) -> str:
        return link.id or f"networklink@{id(link)}"

    @staticmethod
    def _refresh_mode(link: NetworkLink) -> RefreshMode:
        if link.link is None or link.link.refresh_mode is None:
            return RefreshMode.on_change
        return link.link.refresh_mode

    @staticmethod
    def _view_refresh_mode(link: NetworkLink) -> ViewRefreshMode:
        if link.link is None or link.link.view_refresh_mode is None:
            return ViewRefreshMode.never
        return link.link.view_refresh_mode

    @staticmethod
    def _viewport_key(viewport: Viewport) -> tuple[float, float, float, float, float]:
        return (
            viewport.west,
            viewport.south,
            viewport.east,
            viewport.north,
            viewport.pixel_span,
        )
