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
"""
Offline tile planning for KML PhotoOverlay image pyramids.

This module turns pixel regions of interest on the original PhotoOverlay image
into the concrete pyramid tiles that have to be fetched or produced. It works
purely from the :class:`~fastkml.overlays.ImagePyramid` description
(``tileSize``, ``maxWidth``, ``maxHeight``, ``gridOrigin``) and the
``$[level]`` / ``$[x]`` / ``$[y]`` placeholders of the
:class:`~fastkml.links.Icon` href.

No map rendering, image processing or network access is involved: every result
is a plain manifest of tile coordinates, hrefs and the pixel ranges that hold
valid source data. Pixel coordinates always use the image coordinate system,
i.e. the origin is the upper left corner, ``x`` grows to the right and ``y``
grows downwards. Rectangles are half open, so a region of
``(x_min, y_min, x_max, y_max)`` covers the pixels ``x_min <= x < x_max`` and
``y_min <= y < y_max``.

KML tile numbering follows :class:`~fastkml.enums.GridOrigin`: with
``upperLeft`` the KML row ``y`` counts from the top, with ``lowerLeft`` (the
KML default) it counts from the bottom of the pyramid layer.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from fastkml.enums import GridOrigin
from fastkml.overlays import ImagePyramid
from fastkml.overlays import PhotoOverlay

__all__ = [
    "PixelRegion",
    "RegionCoverage",
    "RegionRequest",
    "TileCoordinate",
    "TilePlan",
    "TileRequest",
    "build_tile_plan",
    "level_dimensions",
    "maximum_level",
    "render_tile_href",
    "tile_pixel_region",
    "tile_plan_from_photo_overlay",
    "tiles_for_region",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PixelRegion:
    """
    A rectangular region of pixels on the original PhotoOverlay image.

    Coordinates are expressed in the image coordinate system, with the origin
    at the upper left corner. The bounds are half open: ``x_max`` and
    ``y_max`` are exclusive. Regions may extend beyond the original image
    bounds; the parts outside the image are reported separately instead of
    producing tiles.
    """

    x_min: int
    y_min: int
    x_max: int
    y_max: int

    def __post_init__(self) -> None:
        """Validate the ordering of the pixel bounds."""
        if self.x_min > self.x_max or self.y_min > self.y_max:
            msg = (
                "PixelRegion bounds must be ordered as "
                "x_min <= x_max and y_min <= y_max, got "
                f"({self.x_min}, {self.y_min}, {self.x_max}, {self.y_max})"
            )
            raise ValueError(msg)

    @property
    def width(self) -> int:
        """Return the width in pixels."""
        return self.x_max - self.x_min

    @property
    def height(self) -> int:
        """Return the height in pixels."""
        return self.y_max - self.y_min

    @property
    def empty(self) -> bool:
        """Return whether the region contains no pixels."""
        return self.width == 0 or self.height == 0

    def intersection(self, other: "PixelRegion") -> "PixelRegion | None":
        """
        Intersect this region with another one.

        Returns
        -------
        PixelRegion or None
            The overlapping region, or ``None`` when the regions do not overlap.
        """
        x_min = max(self.x_min, other.x_min)
        y_min = max(self.y_min, other.y_min)
        x_max = min(self.x_max, other.x_max)
        y_max = min(self.y_max, other.y_max)
        if x_min >= x_max or y_min >= y_max:
            return None
        return PixelRegion(x_min, y_min, x_max, y_max)


@dataclass(frozen=True)
class TileCoordinate:
    """
    The address of one tile inside the image pyramid.

    ``x`` is the zero based column and ``y`` the zero based row using KML
    numbering, i.e. the row interpretation depends on ``gridOrigin``.
    """

    level: int
    x: int
    y: int

    def __post_init__(self) -> None:
        """Validate that all coordinate parts are non negative."""
        if self.level < 0 or self.x < 0 or self.y < 0:
            msg = f"Tile coordinates must be non negative, got {self!r}"
            raise ValueError(msg)


@dataclass(frozen=True)
class RegionRequest:
    """A pixel region that has to be covered at one permitted pyramid level."""

    region: PixelRegion
    level: int

    def __post_init__(self) -> None:
        """Validate that the requested pyramid level is non negative."""
        if self.level < 0:
            msg = f"Pyramid levels must be non negative, got {self.level}"
            raise ValueError(msg)


@dataclass(frozen=True)
class TileRequest:
    """
    One tile that is required to fulfill a tile plan.

    The same tile can be required by several regions; a plan therefore holds
    each :class:`TileCoordinate` only once. ``pixel_region`` describes the
    range of original-image pixels represented by the tile, capped to the
    image bounds. Edge tiles may be smaller than a full ``tileSize`` square.
    ``requested_regions`` lists, per requesting region, the pixel overlap that
    made this tile necessary.
    """

    coordinate: TileCoordinate
    href: str
    pixel_region: PixelRegion
    is_edge_tile: bool
    requested_regions: tuple[PixelRegion, ...]

    @property
    def level(self) -> int:
        """Return the pyramid level of the tile."""
        return self.coordinate.level

    @property
    def column(self) -> int:
        """Return the zero based KML column of the tile."""
        return self.coordinate.x

    @property
    def row(self) -> int:
        """Return the zero based KML row of the tile."""
        return self.coordinate.y


@dataclass(frozen=True)
class RegionCoverage:
    """
    Coverage accounting for one :class:`RegionRequest`.

    The manifest separates target-level tiles that are already available from
    tiles that still have to be fetched. Lower resolution tiles that
    temporarily cover pending tiles are listed as fallbacks and never count as
    target precision coverage. ``out_of_bounds_regions`` lists the disjoint
    pixel strips of the request that lie outside the original image; it is
    empty when every requested pixel is inside the image.
    """

    request: RegionRequest
    region: PixelRegion
    out_of_bounds_regions: tuple[PixelRegion, ...]
    target_coordinates: frozenset[TileCoordinate]
    covered_coordinates: frozenset[TileCoordinate]
    pending_coordinates: frozenset[TileCoordinate]
    fallback_coordinates: frozenset[TileCoordinate]

    @property
    def is_complete(self) -> bool:
        """Return whether every in-image pixel is covered at target level."""
        return bool(self.target_coordinates) and not self.pending_coordinates


@dataclass(frozen=True)
class TilePlan:
    """
    The deduplicated tile manifest for a collection of region requests.

    ``tiles`` contains every required tile exactly once, sorted by
    ``(level, x, y)``. ``region_coverages`` keeps the per-region breakdown so
    that each requested region can be audited independently.
    """

    pyramid: ImagePyramid
    href_template: str
    requests: tuple[RegionRequest, ...]
    available: frozenset[TileCoordinate]
    tiles: tuple[TileRequest, ...]
    region_coverages: tuple[RegionCoverage, ...]

    @property
    def required_coordinates(self) -> frozenset[TileCoordinate]:
        """Return all target-level coordinates referenced by the plan."""
        return frozenset(tile.coordinate for tile in self.tiles)

    @property
    def pending_coordinates(self) -> frozenset[TileCoordinate]:
        """Return target-level coordinates that are still missing."""
        return frozenset(
            coordinate
            for coverage in self.region_coverages
            for coordinate in coverage.pending_coordinates
        )

    @property
    def covered_coordinates(self) -> frozenset[TileCoordinate]:
        """Return target-level coordinates that are already available."""
        return self.required_coordinates & self.available

    @property
    def pending_tiles(self) -> tuple[TileRequest, ...]:
        """Return the tile requests that still have to be fetched."""
        pending = self.pending_coordinates
        return tuple(tile for tile in self.tiles if tile.coordinate in pending)

    def tile_for(self, coordinate: TileCoordinate) -> TileRequest | None:
        """Look up the request for a coordinate, or ``None`` if it is unused."""
        for tile in self.tiles:
            if tile.coordinate == coordinate:
                return tile
        return None


def _ceil_div(numerator: int, denominator: int) -> int:
    """Divide two positive integers, rounding up."""
    return -(-numerator // denominator)


def _validate_pyramid(
    pyramid: ImagePyramid,
) -> tuple[int, int, int, GridOrigin]:
    """Validate the pyramid and return tile size, max width, max height, origin."""
    tile_size = pyramid.tile_size
    max_width = pyramid.max_width
    max_height = pyramid.max_height
    if tile_size is None or max_width is None or max_height is None:
        msg = (
            "ImagePyramid requires tile_size, max_width and max_height "
            "to plan tiles"
        )
        raise ValueError(msg)
    if tile_size <= 0 or (tile_size & (tile_size - 1)) != 0:
        msg = f"tile_size must be a positive power of 2, got {tile_size}"
        raise ValueError(msg)
    if max_width <= 0 or max_height <= 0:
        msg = (
            "max_width and max_height must be positive, got "
            f"{max_width} x {max_height}"
        )
        raise ValueError(msg)
    grid_origin = pyramid.grid_origin or GridOrigin.lower_left
    return tile_size, max_width, max_height, grid_origin


def maximum_level(pyramid: ImagePyramid) -> int:
    """
    Return the highest pyramid level, which holds the full original resolution.

    Level zero holds a single tile. Each level doubles the resolution until
    the original image dimensions are reached or exceeded.
    """
    tile_size, max_width, max_height, _ = _validate_pyramid(pyramid)
    level = 0
    width = tile_size
    height = tile_size
    while width < max_width or height < max_height:
        width *= 2
        height *= 2
        level += 1
    return level


def level_dimensions(pyramid: ImagePyramid, level: int) -> tuple[int, int]:
    """
    Return the image width and height in pixels at ``level``.

    Each coarser level halves the resolution. The dimensions are rounded up so
    that downscaling never loses the pixels at the image border, which means
    the top level can be smaller than ``tileSize`` in one dimension.
    """
    tile_size, max_width, max_height, _ = _validate_pyramid(pyramid)
    max_level = maximum_level(pyramid)
    if level < 0 or level > max_level:
        msg = f"level {level} is outside the pyramid range 0..{max_level}"
        raise ValueError(msg)
    divisor = 2 ** (max_level - level)
    return _ceil_div(max_width, divisor), _ceil_div(max_height, divisor)


def grid_dimensions(pyramid: ImagePyramid, level: int) -> tuple[int, int]:
    """Return the number of tile columns and rows at ``level``."""
    tile_size, _, _, _ = _validate_pyramid(pyramid)
    width, height = level_dimensions(pyramid, level)
    return _ceil_div(width, tile_size), _ceil_div(height, tile_size)


def _clamp_region(
    region: PixelRegion,
    max_width: int,
    max_height: int,
) -> PixelRegion:
    """Clip a pixel region to the original image bounds."""
    x_min = max(region.x_min, 0)
    y_min = max(region.y_min, 0)
    x_max = min(region.x_max, max_width)
    y_max = min(region.y_max, max_height)
    if x_min >= x_max or y_min >= y_max:
        return PixelRegion(x_min, y_min, x_min, y_min)
    return PixelRegion(x_min, y_min, x_max, y_max)


def _make_region(
    x_min: int,
    y_min: int,
    x_max: int,
    y_max: int,
) -> PixelRegion | None:
    """Build a pixel region, or ``None`` when the bounds enclose no pixels."""
    if x_min >= x_max or y_min >= y_max:
        return None
    return PixelRegion(x_min, y_min, x_max, y_max)


def _out_of_bounds_parts(
    region: PixelRegion,
    max_width: int,
    max_height: int,
) -> tuple[PixelRegion, ...]:
    """Return the disjoint pixel strips of ``region`` outside the image."""
    inner_x_min = max(region.x_min, 0)
    inner_x_max = min(region.x_max, max_width)
    candidates = (
        _make_region(
            region.x_min,
            region.y_min,
            min(region.x_max, 0),
            region.y_max,
        ),
        _make_region(
            max(region.x_min, max_width),
            region.y_min,
            region.x_max,
            region.y_max,
        ),
        _make_region(
            inner_x_min,
            region.y_min,
            inner_x_max,
            min(region.y_max, 0),
        ),
        _make_region(
            inner_x_min,
            max(region.y_min, max_height),
            inner_x_max,
            region.y_max,
        ),
    )
    return tuple(part for part in candidates if part is not None)


def _to_kml_row(tile_row: int, rows: int, grid_origin: GridOrigin) -> int:
    """Convert a top-based tile row to the KML row for the grid origin."""
    if grid_origin is GridOrigin.lower_left:
        return rows - 1 - tile_row
    return tile_row


def tiles_for_region(
    pyramid: ImagePyramid,
    region: PixelRegion,
    level: int,
) -> tuple[TileCoordinate, ...]:
    """
    Return the deduplicated KML tile coordinates covering ``region`` at ``level``.

    Tiles overlapping the image border are included as partial tiles, so the
    union of the returned tiles always covers the complete in-image part of
    the region. Parts of the region outside the original image produce no
    tiles. Coordinates follow the configured ``gridOrigin``.
    """
    tile_size, max_width, max_height, grid_origin = _validate_pyramid(pyramid)
    max_level = maximum_level(pyramid)
    if level < 0 or level > max_level:
        msg = f"level {level} is outside the pyramid range 0..{max_level}"
        raise ValueError(msg)
    clamped = _clamp_region(region, max_width, max_height)
    if clamped.empty:
        return ()
    columns, rows = grid_dimensions(pyramid, level)
    span = tile_size * 2 ** (max_level - level)
    first_column = clamped.x_min // span
    last_column = (clamped.x_max - 1) // span
    first_row = clamped.y_min // span
    last_row = (clamped.y_max - 1) // span
    coordinates = {
        TileCoordinate(
            level=level,
            x=column,
            y=_to_kml_row(row, rows, grid_origin),
        )
        for row in range(first_row, last_row + 1)
        for column in range(first_column, last_column + 1)
    }
    return tuple(
        sorted(coordinates, key=lambda coordinate: (coordinate.x, coordinate.y)),
    )


def _to_top_row(kml_row: int, rows: int, grid_origin: GridOrigin) -> int:
    """Convert a KML row to the zero based row counted from the top."""
    if grid_origin is GridOrigin.lower_left:
        return rows - 1 - kml_row
    return kml_row


def tile_pixel_region(
    pyramid: ImagePyramid,
    coordinate: TileCoordinate,
) -> PixelRegion:
    """
    Return the original-image pixel range represented by ``coordinate``.

    The range is capped to the image bounds, so edge tiles report fewer than
    ``tileSize`` source pixels instead of pointing outside the image.
    """
    tile_size, max_width, max_height, grid_origin = _validate_pyramid(pyramid)
    max_level = maximum_level(pyramid)
    level = coordinate.level
    if level < 0 or level > max_level:
        msg = f"level {level} is outside the pyramid range 0..{max_level}"
        raise ValueError(msg)
    columns, rows = grid_dimensions(pyramid, level)
    if coordinate.x >= columns or coordinate.y >= rows:
        msg = (
            f"tile ({coordinate.x}, {coordinate.y}) does not exist at level "
            f"{level}; the grid has {columns} column(s) and {rows} row(s)"
        )
        raise ValueError(msg)
    top_row = _to_top_row(coordinate.y, rows, grid_origin)
    span = tile_size * 2 ** (max_level - level)
    return PixelRegion(
        coordinate.x * span,
        top_row * span,
        min((coordinate.x + 1) * span, max_width),
        min((top_row + 1) * span, max_height),
    )


def render_tile_href(href_template: str | None, coordinate: TileCoordinate) -> str:
    """
    Fill the Icon href placeholders for one tile coordinate.

    The KML placeholders ``$[level]``, ``$[x]`` (column) and ``$[y]`` (row)
    are substituted with the values of ``coordinate``. A template without
    placeholders, such as the href of a single image, is returned unchanged.
    """
    if href_template is None:
        return ""
    return (
        href_template.replace("$[level]", str(coordinate.level))
        .replace("$[x]", str(coordinate.x))
        .replace("$[y]", str(coordinate.y))
    )


def _fallback_for(
    pyramid: ImagePyramid,
    coordinate: TileCoordinate,
    available: frozenset[TileCoordinate],
) -> TileCoordinate | None:
    """Return the closest available lower resolution tile covering a tile."""
    for fallback_level in range(coordinate.level - 1, -1, -1):
        factor = 2 ** (coordinate.level - fallback_level)
        candidate = TileCoordinate(
            level=fallback_level,
            x=coordinate.x // factor,
            y=coordinate.y // factor,
        )
        columns, rows = grid_dimensions(pyramid, fallback_level)
        if candidate.x < columns and candidate.y < rows and candidate in available:
            logger.info(
                "tile %s is only covered temporarily by lower resolution tile %s",
                coordinate,
                candidate,
            )
            return candidate
    logger.info("no lower resolution fallback available for tile %s", coordinate)
    return None


def _build_region_coverage(
    pyramid: ImagePyramid,
    request: RegionRequest,
    available: frozenset[TileCoordinate],
) -> RegionCoverage:
    """Build the coverage breakdown for one region request."""
    _, max_width, max_height, _ = _validate_pyramid(pyramid)
    target = frozenset(tiles_for_region(pyramid, request.region, request.level))
    covered = target & available
    pending = target - available
    fallbacks = frozenset(
        fallback
        for coordinate in pending
        if (fallback := _fallback_for(pyramid, coordinate, available)) is not None
    )
    return RegionCoverage(
        request=request,
        region=_clamp_region(request.region, max_width, max_height),
        out_of_bounds_regions=_out_of_bounds_parts(
            request.region,
            max_width,
            max_height,
        ),
        target_coordinates=target,
        covered_coordinates=covered,
        pending_coordinates=pending,
        fallback_coordinates=fallbacks,
    )


def _collect_tile_regions(
    pyramid: ImagePyramid,
    coverages: Sequence[RegionCoverage],
) -> dict[TileCoordinate, list[PixelRegion]]:
    """Map every target tile to the pixel overlaps that made it necessary."""
    tile_regions: dict[TileCoordinate, list[PixelRegion]] = {}
    for coverage in coverages:
        for coordinate in coverage.target_coordinates:
            overlap = coverage.region.intersection(
                tile_pixel_region(pyramid, coordinate),
            )
            if overlap is not None:
                tile_regions.setdefault(coordinate, []).append(overlap)
    return tile_regions


def _make_tile_request(
    pyramid: ImagePyramid,
    href_template: str | None,
    coordinate: TileCoordinate,
    overlaps: Sequence[PixelRegion],
) -> TileRequest:
    """Build the deduplicated request for one tile coordinate."""
    tile_size, _, _, _ = _validate_pyramid(pyramid)
    max_level = maximum_level(pyramid)
    pixel_region = tile_pixel_region(pyramid, coordinate)
    span = tile_size * 2 ** (max_level - coordinate.level)
    return TileRequest(
        coordinate=coordinate,
        href=render_tile_href(href_template, coordinate),
        pixel_region=pixel_region,
        is_edge_tile=pixel_region.width < span or pixel_region.height < span,
        requested_regions=tuple(dict.fromkeys(overlaps)),
    )


def build_tile_plan(
    pyramid: ImagePyramid,
    href_template: str | None,
    requests: Sequence[RegionRequest],
    available: frozenset[TileCoordinate] | Sequence[TileCoordinate] = (),
) -> TilePlan:
    """
    Build a deduplicated offline tile manifest for several region requests.

    Each requested region yields a :class:`RegionCoverage` that lists the
    target-level tiles already covered, the tiles still pending, any lower
    resolution fallback tiles and the pixels that lie outside the original
    image. Tiles shared by several regions or levels appear only once in
    ``tiles``. Available tiles are only counted as coverage when they match
    the requested level; a lower resolution tile is at most a temporary
    fallback and never marks target precision as complete.
    """
    _validate_pyramid(pyramid)
    available_set = frozenset(available)
    coverages = tuple(
        _build_region_coverage(pyramid, request, available_set)
        for request in requests
    )
    tile_regions = _collect_tile_regions(pyramid, coverages)
    tiles = tuple(
        _make_tile_request(pyramid, href_template, coordinate, overlaps)
        for coordinate, overlaps in sorted(
            tile_regions.items(),
            key=lambda item: (item[0].level, item[0].x, item[0].y),
        )
    )
    return TilePlan(
        pyramid=pyramid,
        href_template=href_template or "",
        requests=tuple(requests),
        available=available_set,
        tiles=tiles,
        region_coverages=coverages,
    )


def tile_plan_from_photo_overlay(
    overlay: PhotoOverlay,
    requests: Sequence[RegionRequest],
    available: frozenset[TileCoordinate] | Sequence[TileCoordinate] = (),
) -> TilePlan:
    """
    Build a tile plan straight from a :class:`PhotoOverlay`.

    The :class:`ImagePyramid` and the Icon href template are read from the
    overlay. A PhotoOverlay without an image pyramid cannot be planned and
    raises a ``ValueError``.
    """
    if overlay.image_pyramid is None:
        msg = "PhotoOverlay has no ImagePyramid, cannot plan tiles"
        raise ValueError(msg)
    href_template = overlay.icon.href if overlay.icon is not None else None
    return build_tile_plan(
        overlay.image_pyramid,
        href_template,
        requests,
        available,
    )
