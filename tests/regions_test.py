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

"""Test region activation across the feature tree."""

from fastkml import containers
from fastkml import features
from fastkml import kml
from fastkml import links
from fastkml import regions
from fastkml import views
from fastkml.enums import RegionState
from tests.base import Lxml
from tests.base import StdLibrary


def _region(
    *,
    north: float,
    south: float,
    east: float,
    west: float,
    lod: views.Lod | None = None,
) -> views.Region:
    return views.Region(
        lat_lon_alt_box=views.LatLonAltBox(
            north=north,
            south=south,
            east=east,
            west=west,
        ),
        lod=lod,
    )


VIEWPORT = views.Viewport(
    north=10,
    south=0,
    east=-170,
    west=170,
    lod_pixels=512,
)


class TestStdLibrary(StdLibrary):
    """Test with the standard library."""

    def test_features_without_regions_are_active(self) -> None:
        """A tree without any Region imposes no activation restriction."""
        document = containers.Document(
            features=[
                features.Placemark(name="point"),
                features.NetworkLink(
                    name="link",
                    link=links.Link(href="tiles.kml"),
                ),
            ],
        )

        results = tuple(regions.evaluate_features(document, VIEWPORT))

        assert [result.feature.name for result in results] == ["point", "link"]
        assert all(result.state == RegionState.active for result in results)

    def test_network_link_hrefs_are_collected(self) -> None:
        document = containers.Document(
            features=[
                features.NetworkLink(
                    name="link",
                    link=links.Link(href="https://example.com/tiles.kml"),
                ),
                features.Placemark(name="point"),
            ],
        )

        results = {
            result.feature.name: result.hrefs
            for result in regions.evaluate_features(document, VIEWPORT)
        }

        assert results["link"] == ("https://example.com/tiles.kml",)
        assert results["point"] == ()

    def test_inactive_parent_gates_hitting_child(self) -> None:
        """A child hitting on its own Region stays inactive under an inactive parent."""
        parent = containers.Folder(
            name="parent",
            region=_region(north=50, south=40, east=10, west=0),
        )
        child = features.NetworkLink(
            name="child",
            region=_region(north=20, south=-10, east=-175, west=175),
            link=links.Link(href="child.kml"),
        )
        parent.append(child)

        states = {
            result.feature.name: result.state
            for result in regions.evaluate_features(parent, VIEWPORT)
        }

        assert states["child"] == RegionState.inactive
        assert regions.active_features(parent, VIEWPORT) == ()

    def test_root_document_region_gates_children(self) -> None:
        """The Region on the root container itself is also a gate."""
        document = containers.Document(
            name="root",
            region=_region(north=50, south=40, east=10, west=0),
            features=[
                features.Placemark(
                    name="child",
                    region=_region(north=20, south=-10, east=-175, west=175),
                ),
            ],
        )

        result = next(iter(regions.evaluate_features(document, VIEWPORT)))

        assert result.state == RegionState.inactive

    def test_undetermined_parent_keeps_hitting_child_undetermined(self) -> None:
        """Missing parent evidence cannot be promoted to coverage by a child hit."""
        parent = containers.Folder(
            name="parent",
            region=_region(
                north=20,
                south=-10,
                east=-175,
                west=175,
                lod=views.Lod(min_lod_pixels=256),
            ),
        )
        child = features.Placemark(
            name="child",
            region=_region(north=20, south=-10, east=-175, west=175),
        )
        parent.append(child)
        viewport = views.Viewport(north=10, south=0, east=-170, west=170)

        result = next(iter(regions.evaluate_features(parent, viewport)))

        assert result.feature.name == "child"
        assert result.state == RegionState.undetermined

    def test_active_parent_allows_hitting_child(self) -> None:
        parent = containers.Folder(
            name="parent",
            region=_region(north=20, south=-10, east=-175, west=175),
        )
        child = features.Placemark(
            name="child",
            region=_region(north=20, south=-10, east=-175, west=175),
        )
        parent.append(child)

        result = next(iter(regions.evaluate_features(parent, VIEWPORT)))

        assert result.feature.name == "child"
        assert result.state == RegionState.active

    def test_inactive_child_under_active_parent(self) -> None:
        parent = containers.Folder(
            name="parent",
            region=_region(north=20, south=-10, east=-175, west=175),
        )
        child = features.Placemark(
            name="child",
            region=_region(north=50, south=40, east=10, west=0),
        )
        parent.append(child)

        result = next(iter(regions.evaluate_features(parent, VIEWPORT)))

        assert result.feature.name == "child"
        assert result.state == RegionState.inactive

    def test_active_features_returns_only_decided_hits(self) -> None:
        document = containers.Document(
            features=[
                features.Placemark(name="active-placemark"),
                features.Placemark(
                    name="inactive-placemark",
                    region=_region(north=50, south=40, east=10, west=0),
                ),
                features.Placemark(
                    name="undetermined-placemark",
                    region=_region(
                        north=20,
                        south=-10,
                        east=-175,
                        west=175,
                        lod=views.Lod(min_lod_pixels=256),
                    ),
                ),
            ],
        )
        viewport = views.Viewport(north=10, south=0, east=-170, west=170)

        active = regions.active_features(document, viewport)

        assert [result.feature.name for result in active] == ["active-placemark"]

    def test_evaluation_on_parsed_kml_document(self) -> None:
        doc = (
            '<kml xmlns="http://www.opengis.net/kml/2.2">'
            "<Document><name>offline-doc</name>"
            "<Folder><name>active-folder</name>"
            "<Region><LatLonAltBox>"
            "<north>20</north><south>-10</south>"
            "<east>-175</east><west>175</west>"
            "</LatLonAltBox></Region>"
            "<NetworkLink><name>active-link</name>"
            "<Link><href>active.kml</href></Link></NetworkLink>"
            "</Folder>"
            "<Folder><name>inactive-folder</name>"
            "<Region><LatLonAltBox>"
            "<north>50</north><south>40</south>"
            "<east>10</east><west>0</west>"
            "</LatLonAltBox></Region>"
            "<NetworkLink><name>inactive-link</name>"
            "<Link><href>inactive.kml</href></Link></NetworkLink>"
            "</Folder>"
            "</Document></kml>"
        )
        parsed = kml.KML.from_string(doc)

        results = {
            result.feature.name: (result.state, result.hrefs)
            for result in regions.evaluate_features(parsed, VIEWPORT)
        }

        assert results["offline-doc"] == (RegionState.active, ())
        assert results["active-folder"] == (RegionState.active, ())
        assert results["active-link"] == (
            RegionState.active,
            ("active.kml",),
        )
        assert results["inactive-folder"] == (RegionState.inactive, ())
        assert results["inactive-link"] == (
            RegionState.inactive,
            ("inactive.kml",),
        )
        active = [
            result.feature.name for result in regions.active_features(parsed, VIEWPORT)
        ]
        assert active == [
            "offline-doc",
            "active-folder",
            "active-link",
        ]


class TestLxml(Lxml, TestStdLibrary):
    """Test with lxml."""
