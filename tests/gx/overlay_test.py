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

"""Test the gx LatLonQuad ground overlay extension."""

import pytest

from fastkml.exceptions import KMLParseError
from fastkml.gx import LatLonQuad
from fastkml.overlays import GroundOverlay
from fastkml.overlays import LatLonBox
from tests.base import Lxml
from tests.base import StdLibrary

QUAD_DOC = (
    '<gx:LatLonQuad xmlns:gx="http://www.google.com/kml/ext/2.2" '
    'xmlns:kml="http://www.opengis.net/kml/2.2">'
    "<kml:coordinates>0.0,0.0 10.0,0.0 10.0,10.0 0.0,10.0</kml:coordinates>"
    "</gx:LatLonQuad>"
)

CORNER_COORDS = ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0))


class TestLatLonQuad(StdLibrary):
    def test_from_string(self) -> None:
        quad = LatLonQuad.from_string(QUAD_DOC)

        assert quad.coords == list(CORNER_COORDS)
        assert quad.ns == "{http://www.google.com/kml/ext/2.2}"

    def test_from_string_default_namespace(self) -> None:
        # The gx element is prefixed while the coordinates child uses the
        # default KML namespace, as in a real document.
        doc = (
            '<gx:LatLonQuad xmlns:gx="http://www.google.com/kml/ext/2.2" '
            'xmlns="http://www.opengis.net/kml/2.2">'
            "<coordinates>0,0 10,0 10,10 0,10</coordinates>"
            "</gx:LatLonQuad>"
        )

        quad = LatLonQuad.from_string(doc)

        assert quad.coords == list(CORNER_COORDS)

    def test_to_string_roundtrip(self) -> None:
        quad = LatLonQuad(coords=list(CORNER_COORDS))

        assert LatLonQuad.from_string(quad.to_string()) == quad

    def test_bool(self) -> None:
        assert bool(LatLonQuad(coords=list(CORNER_COORDS))) is True
        assert bool(LatLonQuad(coords=[(0.0, 0.0), (10.0, 0.0)])) is False
        assert bool(LatLonQuad()) is False

    def test_image_position_center(self) -> None:
        quad = LatLonQuad(coords=list(CORNER_COORDS))

        position = quad.image_position(5.0, 5.0)

        assert position is not None
        assert position.x == pytest.approx(0.5)
        assert position.y == pytest.approx(0.5)
        assert position.declaration is quad

    @pytest.mark.parametrize(
        ("longitude", "latitude", "expected_x", "expected_y"),
        [
            (0.0, 0.0, 0.0, 0.0),
            (10.0, 0.0, 1.0, 0.0),
            (10.0, 10.0, 1.0, 1.0),
            (0.0, 10.0, 0.0, 1.0),
        ],
    )
    def test_image_position_corners(
        self,
        longitude: float,
        latitude: float,
        expected_x: float,
        expected_y: float,
    ) -> None:
        quad = LatLonQuad(coords=list(CORNER_COORDS))

        position = quad.image_position(longitude, latitude)

        assert position is not None
        assert position.x == pytest.approx(expected_x)
        assert position.y == pytest.approx(expected_y)

    @pytest.mark.parametrize(
        ("longitude", "latitude"),
        [(11.0, 5.0), (-1.0, 5.0), (5.0, 11.0), (5.0, -1.0)],
    )
    def test_image_position_outside(self, longitude: float, latitude: float) -> None:
        quad = LatLonQuad(coords=list(CORNER_COORDS))

        assert quad.image_position(longitude, latitude) is None

    def test_image_position_trapezoid_center(self) -> None:
        # A non-affine quadrilateral cannot be inverted with a bounding box;
        # the bilinear center still maps to the image center.
        quad = LatLonQuad(
            coords=[(0.0, 0.0), (10.0, 0.0), (8.0, 10.0), (2.0, 10.0)],
        )

        position = quad.image_position(5.0, 5.0)

        assert position is not None
        assert position.x == pytest.approx(0.5)
        assert position.y == pytest.approx(0.5)

    def test_image_position_antimeridian(self) -> None:
        quad = LatLonQuad(
            coords=[(170.0, 0.0), (-170.0, 0.0), (-170.0, 10.0), (170.0, 10.0)],
        )

        center = quad.image_position(180.0, 5.0)
        assert center is not None
        assert center.x == pytest.approx(0.5)
        assert quad.image_position(-175.0, 5.0) is not None
        assert quad.image_position(175.0, 5.0) is not None
        # Coordinates far away from the coverage must not be pulled in.
        assert quad.image_position(0.0, 5.0) is None
        assert quad.image_position(90.0, 5.0) is None

    def test_image_position_self_intersecting(self) -> None:
        bowtie = LatLonQuad(
            coords=[(0.0, 0.0), (10.0, 10.0), (10.0, 0.0), (0.0, 10.0)],
        )

        assert bowtie.image_position(5.0, 5.0) is None

    def test_image_position_duplicate_corners(self) -> None:
        degenerate = LatLonQuad(
            coords=[(0.0, 0.0), (0.0, 0.0), (10.0, 10.0), (0.0, 10.0)],
        )

        assert degenerate.image_position(5.0, 5.0) is None

    def test_image_position_incomplete(self) -> None:
        incomplete = LatLonQuad(coords=[(0.0, 0.0), (10.0, 0.0)])

        assert incomplete.image_position(5.0, 5.0) is None

    def test_invalid_coordinates_strict(self) -> None:
        doc = (
            '<gx:LatLonQuad xmlns:gx="http://www.google.com/kml/ext/2.2" '
            'xmlns:kml="http://www.opengis.net/kml/2.2">'
            "<kml:coordinates>a,b c,d e,f g,h</kml:coordinates>"
            "</gx:LatLonQuad>"
        )

        with pytest.raises(KMLParseError):
            LatLonQuad.from_string(doc, strict=True)

    def test_invalid_coordinates_lenient(self) -> None:
        doc = (
            '<gx:LatLonQuad xmlns:gx="http://www.google.com/kml/ext/2.2" '
            'xmlns:kml="http://www.opengis.net/kml/2.2">'
            "<kml:coordinates>a,b c,d e,f g,h</kml:coordinates>"
            "</gx:LatLonQuad>"
        )

        quad = LatLonQuad.from_string(doc, strict=False)

        assert quad.coords == []

    def test_precision(self) -> None:
        quad = LatLonQuad(
            coords=[(1.23456789, 2.34567891), (3.0, 4.0), (5.0, 6.0), (7.0, 8.0)],
        )

        serialized = quad.to_string(precision=3)

        assert "1.235,2.346" in serialized


class TestGroundOverlayLatLonQuad(StdLibrary):
    def test_ground_overlay_parses_quad(self) -> None:
        doc = (
            '<kml:GroundOverlay xmlns:kml="http://www.opengis.net/kml/2.2" '
            'xmlns:gx="http://www.google.com/kml/ext/2.2">'
            "<kml:Icon><kml:href>img.png</kml:href></kml:Icon>"
            "<gx:LatLonQuad>"
            "<kml:coordinates>0,0 10,0 10,10 0,10</kml:coordinates>"
            "</gx:LatLonQuad>"
            "</kml:GroundOverlay>"
        )

        overlay = GroundOverlay.from_string(doc)

        assert overlay.lat_lon_box is None
        assert isinstance(overlay.lat_lon_quad, LatLonQuad)
        assert overlay.lat_lon_quad.coords == list(CORNER_COORDS)

    def test_ground_overlay_quad_roundtrip(self) -> None:
        overlay = GroundOverlay(lat_lon_quad=LatLonQuad(coords=list(CORNER_COORDS)))

        assert GroundOverlay.from_string(overlay.to_string()) == overlay

    def test_ground_overlay_image_position_quad(self) -> None:
        quad = LatLonQuad(coords=list(CORNER_COORDS))
        overlay = GroundOverlay(lat_lon_quad=quad)

        position = overlay.image_position(5.0, 5.0)

        assert position is not None
        assert position.declaration is quad

    def test_ground_overlay_no_coverage(self) -> None:
        assert GroundOverlay().image_position(5.0, 5.0) is None

    def test_ground_overlay_box_and_quad_are_mutually_exclusive(self) -> None:
        with pytest.raises(ValueError, match="mutually exclusive"):
            GroundOverlay(
                lat_lon_box=LatLonBox(
                    north=10.0,
                    south=0.0,
                    east=10.0,
                    west=0.0,
                ),
                lat_lon_quad=LatLonQuad(coords=list(CORNER_COORDS)),
            )

    def test_ground_overlay_parsing_both_coverages_fails(self) -> None:
        doc = (
            '<kml:GroundOverlay xmlns:kml="http://www.opengis.net/kml/2.2" '
            'xmlns:gx="http://www.google.com/kml/ext/2.2">'
            "<kml:LatLonBox>"
            "<kml:north>10</kml:north><kml:south>0</kml:south>"
            "<kml:east>10</kml:east><kml:west>0</kml:west>"
            "</kml:LatLonBox>"
            "<gx:LatLonQuad>"
            "<kml:coordinates>0,0 10,0 10,10 0,10</kml:coordinates>"
            "</gx:LatLonQuad>"
            "</kml:GroundOverlay>"
        )

        with pytest.raises(ValueError, match="mutually exclusive"):
            GroundOverlay.from_string(doc)


class TestLatLonQuadLxml(Lxml, TestLatLonQuad):
    """Test with lxml."""


class TestGroundOverlayLatLonQuadLxml(Lxml, TestGroundOverlayLatLonQuad):
    """Test with lxml."""
