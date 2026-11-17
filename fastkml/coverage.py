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
Point-in-coverage math for ground overlays.

The functions in this module map a geographic coordinate to a position inside
the image draped by a :class:`fastkml.overlays.GroundOverlay`. Both coverage
declarations supported by KML are handled:

* ``LatLonBox`` - an axis-aligned box that may be rotated about its center.
* ``gx:LatLonQuad`` - an explicit quadrilateral given by its four corners.

The math is deliberately kept in a module with no dependency on the XML
classes so the overlay classes and the gx extension can share it without
import cycles. Only planar geometry in equirectangular (longitude/latitude)
coordinates is used, which is the same approximation KML viewers apply when
draping an image over the globe.
"""

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pygeoif.types import LineType

    from fastkml.overlays import LatLonBox

__all__ = [
    "ImagePosition",
    "latlon_box_corners",
    "latlon_box_image_position",
    "quad_image_position",
]

_FULL_TURN = 360.0
_HALF_TURN = 180.0
_QUAD_CORNER_COUNT = 4
_COORD_ROUNDING = 12
# Relative tolerance used to reject ill-conditioned inverse solutions.
_NUMERIC_EPS = 1e-12
# Tolerance used when deciding whether an image coordinate lies on the edge
# of the unit image square (0.0 to 1.0).
_EDGE_EPS = 1e-9


@dataclass(frozen=True)
class ImagePosition:
    """
    A position inside the image draped by a GroundOverlay.

    Attributes
    ----------
    x : float
        Horizontal position inside the image as a fraction of its width,
        from the left edge (0.0) to the right edge (1.0).
    y : float
        Vertical position inside the image as a fraction of its height,
        from the bottom edge (0.0) to the top edge (1.0).
    declaration : object
        The original coverage declaration the position was computed from,
        either a :class:`fastkml.overlays.LatLonBox` or a
        :class:`fastkml.gx.overlay.LatLonQuad`. It is returned unchanged so
        callers can inspect the exact north/south/east/west, rotation or
        corner coordinates that were declared in the KML.

    """

    x: float
    y: float
    declaration: object


def _unwrapped_longitude(longitude: float, reference: float) -> float:
    """Return ``longitude`` on the same turn of the globe as ``reference``.

    Longitudes are kept within ±180°, so an overlay crossing the antimeridian
    is declared with corners on opposite sides of the ±180° seam. Adding or
    subtracting a multiple of 360° restores the continuous values needed for
    planar geometry without changing the geographic location.
    """
    while longitude - reference > _HALF_TURN:
        longitude -= _FULL_TURN
    while longitude - reference < -_HALF_TURN:
        longitude += _FULL_TURN
    return longitude


def _inside_unit_square(x: float, y: float) -> bool:
    """Check whether an image position lies on the unit image square."""
    return (
        -_EDGE_EPS <= x <= 1.0 + _EDGE_EPS
        and -_EDGE_EPS <= y <= 1.0 + _EDGE_EPS
    )


def _clamp_unit(value: float) -> float:
    """Clamp an image coordinate onto the closed unit interval [0.0, 1.0]."""
    return min(1.0, max(0.0, value))


def latlon_box_corners(lat_lon_box: "LatLonBox") -> "LineType":
    """
    Compute the four geographic corners of a (possibly rotated) LatLonBox.

    Returns the corners in the image order bottom-left, bottom-right,
    top-right, top-left. The rotation follows the KML convention: a positive
    ``rotation`` rotates the overlay counterclockwise about its center.
    """
    north = lat_lon_box.north
    south = lat_lon_box.south
    east = lat_lon_box.east
    west = lat_lon_box.west
    center_lat = (north + south) / 2.0
    center_lon = (east + west) / 2.0
    half_width = (east - west) / 2.0
    half_height = (north - south) / 2.0
    angle = math.radians(lat_lon_box.rotation or 0.0)
    cos_angle = math.cos(angle)
    sin_angle = math.sin(angle)
    # Half-extents of the unrotated box, in image order
    # (bottom-left, bottom-right, top-right, top-left).
    half_corners = (
        (-half_width, -half_height),
        (half_width, -half_height),
        (half_width, half_height),
        (-half_width, half_height),
    )
    return tuple(
        (
            center_lon + (x * cos_angle - y * sin_angle),
            center_lat + (x * sin_angle + y * cos_angle),
        )
        for x, y in half_corners
    )


def latlon_box_image_position(
    lat_lon_box: "LatLonBox",
    longitude: float,
    latitude: float,
) -> ImagePosition | None:
    """
    Invert the LatLonBox rotation and test the unit image square.

    Returns ``None`` when the coordinate is outside the box or when the box
    does not have strictly positive width and height, in which case no unique
    image position exists.
    """
    north = lat_lon_box.north
    south = lat_lon_box.south
    east = lat_lon_box.east
    west = lat_lon_box.west
    width = east - west
    height = north - south
    if width <= 0.0 or height <= 0.0:
        return None
    center_lat = (north + south) / 2.0
    center_lon = (east + west) / 2.0
    delta_lon = _unwrapped_longitude(longitude, center_lon) - center_lon
    delta_lat = latitude - center_lat
    angle = math.radians(lat_lon_box.rotation or 0.0)
    # Inverse of the counterclockwise rotation used to place the corners.
    local_x = delta_lon * math.cos(angle) + delta_lat * math.sin(angle)
    local_y = -delta_lon * math.sin(angle) + delta_lat * math.cos(angle)
    x = (local_x + width / 2.0) / width
    y = (local_y + height / 2.0) / height
    if not _inside_unit_square(x, y):
        return None
    return ImagePosition(
        x=_clamp_unit(x),
        y=_clamp_unit(y),
        declaration=lat_lon_box,
    )


def _cross(
    start: tuple[float, float],
    end: tuple[float, float],
    point: tuple[float, float],
) -> float:
    """Signed area of the parallelogram (end-start) x (point-start)."""
    return (end[0] - start[0]) * (point[1] - start[1]) - (
        end[1] - start[1]
    ) * (point[0] - start[0])


def _quadrilateral_is_simple(points: "LineType") -> bool:
    """
    Check that four corners form a non-degenerate simple quadrilateral.

    The corners must be distinct and the two diagonals must cross. Crossing
    diagonals guarantee a non-self-intersecting quadrilateral with a
    well-defined interior; a bow-tie quadrilateral has non-crossing diagonals
    and is rejected. The corners must already be unwrapped onto a single turn
    of the globe so antimeridian-spanning quads are judged correctly.
    """
    distinct = {
        (round(coord[0], _COORD_ROUNDING), round(coord[1], _COORD_ROUNDING))
        for coord in points
    }
    if len(distinct) != _QUAD_CORNER_COUNT:
        return False
    p0, p1, p2, p3 = (tuple(coord[:2]) for coord in points)
    return (
        _cross(p0, p2, p1) * _cross(p0, p2, p3) < 0.0
        and _cross(p1, p3, p0) * _cross(p1, p3, p2) < 0.0
    )


def _bilinear_coefficients(
    corners: "LineType",
) -> tuple[float, float, float, float, float, float, float, float]:
    """Return the eight coefficients of the bilinear corner mapping."""
    (bl_x, bl_y), (br_x, br_y), (tr_x, tr_y), (tl_x, tl_y) = corners
    return (
        bl_x,
        br_x - bl_x,
        tl_x - bl_x,
        bl_x - br_x + tr_x - tl_x,
        bl_y,
        br_y - bl_y,
        tl_y - bl_y,
        bl_y - br_y + tr_y - tl_y,
    )


def _forward_bilinear(
    u: float,
    v: float,
    corners: "LineType",
) -> tuple[float, float]:
    """Evaluate the forward bilinear mapping at image position ``(u, v)``."""
    (bl_x, bl_y), (br_x, br_y), (tr_x, tr_y), (tl_x, tl_y) = corners
    return (
        (1.0 - u) * (1.0 - v) * bl_x
        + u * (1.0 - v) * br_x
        + u * v * tr_x
        + (1.0 - u) * v * tl_x,
        (1.0 - u) * (1.0 - v) * bl_y
        + u * (1.0 - v) * br_y
        + u * v * tr_y
        + (1.0 - u) * v * tl_y,
    )


def _quadratic_roots(
    coefficient_a: float,
    coefficient_b: float,
    coefficient_c: float,
    eps: float,
) -> tuple[float, ...]:
    """Solve A*v**2 + B*v + C = 0, returning the real roots."""
    if abs(coefficient_a) <= eps:
        if abs(coefficient_b) <= eps:
            return ()
        return (-coefficient_c / coefficient_b,)
    discriminant = (
        coefficient_b * coefficient_b - 4.0 * coefficient_a * coefficient_c
    )
    if discriminant < -eps:
        return ()
    sqrt_discriminant = math.sqrt(max(discriminant, 0.0))
    return (
        (-coefficient_b - sqrt_discriminant) / (2.0 * coefficient_a),
        (-coefficient_b + sqrt_discriminant) / (2.0 * coefficient_a),
    )


def quad_image_position(
    corners: "LineType",
    longitude: float,
    latitude: float,
    declaration: object,
) -> ImagePosition | None:
    """
    Map a coordinate to image space through an explicit four-corner quad.

    The four corners are given in image order (bottom-left, bottom-right,
    top-right, top-left) and the image is assumed to be bilinearly draped
    over them. The inverse bilinear mapping is solved analytically, so the
    exact quadrilateral is tested rather than its axis-aligned bounding box.

    Longitudes are unwrapped onto a single turn of the globe, anchored at
    the first corner, so a quad spanning the antimeridian stays continuous.
    The queried longitude is brought into the same frame, which keeps the
    interpretation of the quadrilateral fixed instead of letting it depend
    on where the query happens to be.

    Returns ``None`` when there are not exactly four corners or when the
    quadrilateral is degenerate or self-intersecting, in which case a unique
    inverse mapping does not exist.
    """
    if len(corners) != _QUAD_CORNER_COUNT:
        return None
    reference = corners[0][0]
    unwrapped = tuple(
        (_unwrapped_longitude(coord[0], reference), coord[1])
        for coord in corners
    )
    if not _quadrilateral_is_simple(unwrapped):
        return None
    (a0, a1, a2, a3, b0, b1, b2, b3) = _bilinear_coefficients(unwrapped)
    target_x = _unwrapped_longitude(longitude, reference)
    target_y = latitude
    scale = max(
        1.0,
        abs(a1),
        abs(a2),
        abs(a3),
        abs(b1),
        abs(b2),
        abs(b3),
    )
    eps = _NUMERIC_EPS * scale
    # Eliminate ``u`` to obtain A*v**2 + B*v + C = 0.
    coefficient_a = a2 * b3 - a3 * b2
    coefficient_b = (
        a2 * b1
        - a1 * b2
        - b3 * (target_x - a0)
        + a3 * (target_y - b0)
    )
    coefficient_c = a1 * (target_y - b0) - b1 * (target_x - a0)
    roots = _quadratic_roots(
        coefficient_a,
        coefficient_b,
        coefficient_c,
        eps,
    )
    # A near-degenerate quadrilateral (two corners almost coinciding) can
    # pass the structural simplicity check yet have an ill-conditioned
    # inverse. Every candidate is therefore verified against the forward
    # bilinear mapping; solutions that do not reconstruct the query are
    # treated as "not locatable" rather than returned as a wrong position.
    tolerance = _EDGE_EPS * max(1.0, abs(target_x), abs(target_y), scale)
    for v in roots:
        denominator = a1 + a3 * v
        if abs(denominator) <= eps:
            continue
        u = (target_x - a0 - a2 * v) / denominator
        if not _inside_unit_square(u, v):
            continue
        forward_x, forward_y = _forward_bilinear(u, v, unwrapped)
        if (
            abs(forward_x - target_x) <= tolerance
            and abs(forward_y - target_y) <= tolerance
        ):
            return ImagePosition(
                x=_clamp_unit(u),
                y=_clamp_unit(v),
                declaration=declaration,
            )
    return None
