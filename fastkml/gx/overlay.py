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
GX GroundOverlay extension.

The ``gx:LatLonQuad`` element is an alternative to ``LatLonBox`` inside a
``GroundOverlay``. Instead of an axis-aligned (and optionally rotated) box it
declares the coverage explicitly through its four corner coordinates, which
allows an image to be draped over an arbitrary quadrilateral.

The four coordinates are given in image order: bottom-left, bottom-right,
top-right, top-left.
"""

import logging
import re
from typing import TYPE_CHECKING
from typing import Any
from typing import cast

from pygeoif.types import LineType

from fastkml import config
from fastkml.coverage import ImagePosition
from fastkml.coverage import quad_image_position
from fastkml.enums import Verbosity
from fastkml.exceptions import KMLParseError
from fastkml.kml_base import _BaseObject
from fastkml.registry import RegistryItem
from fastkml.registry import registry
from fastkml.types import Element

if TYPE_CHECKING:
    from fastkml.base import _XMLObject

__all__ = ["LatLonQuad"]

logger = logging.getLogger(__name__)

_QUAD_CORNER_COUNT = 4


def _coordinates_subelement(
    obj: "_XMLObject",
    *,
    element: Element,
    attr_name: str,
    node_name: str,
    precision: int | None,
    verbosity: Verbosity,  # noqa: ARG001
    default: Any,  # noqa: ARG001
) -> None:
    """Write the four corner coordinates as a KML coordinates child element."""
    coords = getattr(obj, attr_name, None)
    if not coords:
        return
    namespace = obj.name_spaces.get(config.KML, config.KMLNS)
    child = config.etree.SubElement(element, f"{namespace}{node_name}")
    if precision is None:
        child.text = " ".join(
            ",".join(str(value) for value in coord) for coord in coords
        )
    else:
        child.text = " ".join(
            ",".join(f"{value:.{precision}f}" for value in coord)
            for coord in coords
        )


def _coordinates_kwarg(
    *,
    element: Element,
    ns: str,
    name_spaces: dict[str, str],  # noqa: ARG001
    node_name: str,
    kwarg: str,
    classes: tuple[type[object], ...],  # noqa: ARG001
    strict: bool,
) -> dict[str, LineType]:
    """Parse the coordinates child element of a gx:LatLonQuad."""
    child = element.find(f"{ns}{node_name}")
    if child is None or not child.text:
        return {}
    try:
        values = re.sub(r", +", ",", child.text.strip()).split()
        coords = cast(
            "LineType",
            [
                tuple(float(value) for value in item.split(","))
                for item in values
            ],
        )
        return {kwarg: coords}
    except ValueError as error:
        error_in_xml = config.etree.tostring(
            element,
            encoding="UTF-8",
        ).decode("UTF-8")
        msg = f"Invalid coordinates in '{error_in_xml}' caused by '{error}'"
        logger.error(msg)
        if strict:
            raise KMLParseError(msg) from error
        return {}


class LatLonQuad(_BaseObject):
    """
    An explicit four-corner coverage for a GroundOverlay.

    ``gx:LatLonQuad`` is used instead of :class:`fastkml.overlays.LatLonBox`
    when the draped image does not fit an axis-aligned or merely rotated box.
    The image is stretched over the quadrilateral defined by ``coords``.

    The coordinates must be ordered bottom-left, bottom-right, top-right,
    top-left, matching the corners of the source image. Exactly four
    coordinates are required; an incomplete, degenerate or self-intersecting
    quadrilateral cannot be inverted, in which case :meth:`image_position`
    returns ``None`` rather than guessing from a bounding box.

    https://developers.google.com/kml/documentation/kmlreference#gxlatlonquad
    """

    _default_nsid = config.GX

    coords: LineType

    def __init__(
        self,
        ns: str | None = None,
        name_spaces: dict[str, str] | None = None,
        *,
        id: str | None = None,
        target_id: str | None = None,
        coords: LineType | None = None,
        **kwargs: Any,
    ) -> None:
        """
        Initialize a LatLonQuad.

        Parameters
        ----------
        ns : Optional[str]
            The namespace for the element.
        name_spaces : Optional[Dict[str, str]]
            The dictionary of namespace prefixes and URIs.
        id : Optional[str]
            The ID of the element.
        target_id : Optional[str]
            The target ID of the element.
        coords : Optional[LineType]
            The four corner coordinates as ``(longitude, latitude)`` (or
            ``(longitude, latitude, altitude)``) tuples, in image order
            bottom-left, bottom-right, top-right, top-left.
        **kwargs : Any
            Additional keyword arguments.

        """
        super().__init__(
            ns=ns,
            name_spaces=name_spaces,
            id=id,
            target_id=target_id,
            **kwargs,
        )
        self.coords = cast("LineType", list(coords) if coords else [])

    def __repr__(self) -> str:
        """Create a string (c)representation for LatLonQuad."""
        return (
            f"{self.__class__.__module__}.{self.__class__.__name__}("
            f"ns={self.ns!r}, "
            f"name_spaces={self.name_spaces!r}, "
            f"id={self.id!r}, "
            f"target_id={self.target_id!r}, "
            f"coords={self.coords!r}, "
            f"**{self._get_splat()!r},"
            ")"
        )

    def __bool__(self) -> bool:
        """Return True when exactly four corners are declared."""
        return len(self.coords) == _QUAD_CORNER_COUNT

    def image_position(
        self,
        longitude: float,
        latitude: float,
    ) -> ImagePosition | None:
        """
        Return the image position of a coordinate inside this quadrilateral.

        Parameters
        ----------
        longitude : float
            Longitude of the coordinate in decimal degrees.
        latitude : float
            Latitude of the coordinate in decimal degrees.

        Returns
        -------
        Optional[ImagePosition]
            The position inside the draped image, with ``x``/``y`` between
            0 and 1 and this :class:`LatLonQuad` attached as the
            declaration. ``None`` if the coordinate is outside the
            quadrilateral, if fewer than four corners are declared, or if
            the quadrilateral is degenerate or self-intersecting.

        """
        if not self:
            return None
        return quad_image_position(
            self.coords,
            longitude,
            latitude,
            declaration=self,
        )


registry.register(
    LatLonQuad,
    RegistryItem(
        ns_ids=("kml", ""),
        attr_name="coords",
        node_name="coordinates",
        classes=(tuple, list),
        get_kwarg=_coordinates_kwarg,
        set_element=_coordinates_subelement,
    ),
)
