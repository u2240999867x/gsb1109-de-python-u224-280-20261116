# Copyright (C) 2024  Christian Ledermann
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

"""Test the NetworkLink request URL preview."""

import pytest

from fastkml.exceptions import QueryParameterCollisionError
from fastkml.exceptions import UnresolvedTemplateVariablesError
from fastkml.links import Link
from fastkml.network_link import Bbox
from fastkml.network_link import ClientInfo
from fastkml.network_link import CollisionPolicy
from fastkml.network_link import NetworkLinkRequest
from fastkml.network_link import ViewParameters
from fastkml.network_link import build_request_url
from fastkml.views import Camera
from fastkml.views import LookAt


class TestBbox:
    """Test the viewport bounding box."""

    def test_regular_box(self) -> None:
        bbox = Bbox(west=-10, south=-5, east=10, north=5)

        assert not bbox.crosses_antimeridian
        assert bbox.longitude_span == 20

    def test_antimeridian_box(self) -> None:
        """The smaller longitude is not treated as the western boundary."""
        bbox = Bbox(west=170, south=-5, east=-170, north=5)

        assert bbox.crosses_antimeridian
        assert bbox.longitude_span == 20


class TestBuildRequestUrl:
    """Test rendering of viewFormat and httpQuery templates."""

    def test_bbox_substitution(self) -> None:
        request = build_request_url(
            href="https://example.com/data.kml",
            view_format="BBOX=[bboxWest],[bboxSouth],[bboxEast],[bboxNorth]",
            bbox=Bbox(west=-180, south=-90, east=180, north=90),
        )

        assert request.is_complete
        assert request.url == "https://example.com/data.kml?BBOX=-180,-90,180,90"
        assert request.view_format is not None
        assert request.view_format.template == (
            "BBOX=[bboxWest],[bboxSouth],[bboxEast],[bboxNorth]"
        )
        subs = request.view_format.substitutions
        sources = {sub.variable: sub.source for sub in subs}
        assert sources == {
            "bboxWest": "bbox.west",
            "bboxSouth": "bbox.south",
            "bboxEast": "bbox.east",
            "bboxNorth": "bbox.north",
        }

    def test_antimeridian_bbox_not_reordered(self) -> None:
        request = build_request_url(
            href="https://example.com/data.kml",
            view_format="BBOX=[bboxWest],[bboxSouth],[bboxEast],[bboxNorth]",
            bbox=Bbox(west=170, south=-5, east=-170, north=5),
        )

        assert request.url == "https://example.com/data.kml?BBOX=170,-5,-170,5"

    def test_http_query_client_fields(self) -> None:
        request = build_request_url(
            href="https://example.com/data.kml",
            http_query="clientName=[clientName]&kmlVersion=[kmlVersion]",
            client=ClientInfo(client_name="fastkml", kml_version="2.3"),
        )

        assert request.url == (
            "https://example.com/data.kml?clientName=fastkml&kmlVersion=2.3"
        )

    def test_client_value_is_percent_encoded(self) -> None:
        request = build_request_url(
            href="https://example.com/data.kml",
            http_query="clientName=[clientName]",
            client=ClientInfo(client_name="My Client"),
        )

        assert request.url == "https://example.com/data.kml?clientName=My%20Client"

    def test_encoded_path_is_not_encoded_again(self) -> None:
        request = build_request_url(
            href="https://example.com/some%20path/data.kml",
            view_format="BBOX=[bboxWest],[bboxSouth],[bboxEast],[bboxNorth]",
            bbox=Bbox(west=0, south=0, east=1, north=1),
        )

        assert request.url == ("https://example.com/some%20path/data.kml?BBOX=0,0,1,1")

    def test_literal_percent_in_template_is_preserved(self) -> None:
        request = build_request_url(
            href="https://example.com/data.kml",
            http_query="sep=%2C&clientName=[clientName]",
            client=ClientInfo(client_name="fastkml"),
        )

        assert request.url == (
            "https://example.com/data.kml?sep=%2C&clientName=fastkml"
        )

    def test_fragment_is_preserved(self) -> None:
        request = build_request_url(
            href="https://example.com/data.kml#frag",
            view_format="a=[bboxWest]",
            bbox=Bbox(west=1, south=0, east=2, north=3),
        )

        assert request.url == "https://example.com/data.kml?a=1#frag"

    def test_missing_camera_quantities_stay_unresolved(self) -> None:
        request = build_request_url(
            href="https://example.com/data.kml",
            view_format="BBOX=[bboxWest],[bboxSouth],[bboxEast],[bboxNorth]",
            http_query="cameraLon=[cameraLon]&cameraAlt=[cameraAlt]",
            bbox=Bbox(west=0, south=0, east=1, north=1),
            view=ViewParameters(camera_lon=10),
        )

        assert not request.is_complete
        assert request.url is None
        assert request.unresolved == ("cameraAlt",)
        assert request.http_query is not None
        assert request.http_query.text == "cameraLon=10&cameraAlt=[cameraAlt]"
        with pytest.raises(UnresolvedTemplateVariablesError):
            request.require_url()

    def test_unknown_variable_stays_unresolved(self) -> None:
        request = build_request_url(
            href="https://example.com/data.kml",
            view_format="foo=[notAVariable]",
        )

        assert not request.is_complete
        assert request.unresolved == ("notAVariable",)

    def test_terrain_enabled_is_rendered_as_flag(self) -> None:
        request = build_request_url(
            href="https://example.com/data.kml",
            view_format="terrain=[terrainEnabled]",
            view=ViewParameters(terrain_enabled=True),
        )

        assert request.url == "https://example.com/data.kml?terrain=1"


class TestCollisionPolicy:
    """Test the caller specified policy for query parameter name collisions."""

    href = "https://example.com/data.kml?BBOX=old&mode=keep"
    view_format = "BBOX=[bboxWest],[bboxSouth],[bboxEast],[bboxNorth]"
    bbox = Bbox(west=0, south=1, east=2, north=3)

    def test_keep_existing(self) -> None:
        request = build_request_url(
            href=self.href,
            view_format=self.view_format,
            bbox=self.bbox,
            on_collision=CollisionPolicy.keep_existing,
        )

        assert request.url == ("https://example.com/data.kml?BBOX=old&mode=keep")

    def test_replace(self) -> None:
        request = build_request_url(
            href=self.href,
            view_format=self.view_format,
            bbox=self.bbox,
            on_collision=CollisionPolicy.replace,
        )

        assert request.url == ("https://example.com/data.kml?mode=keep&BBOX=0,1,2,3")

    def test_error(self) -> None:
        with pytest.raises(QueryParameterCollisionError):
            build_request_url(
                href=self.href,
                view_format=self.view_format,
                bbox=self.bbox,
                on_collision=CollisionPolicy.error,
            )

    def test_policy_accepts_string(self) -> None:
        request = build_request_url(
            href=self.href,
            view_format=self.view_format,
            bbox=self.bbox,
            on_collision="replace",  # type: ignore[arg-type]
        )

        assert request.on_collision is CollisionPolicy.replace


class TestIdentity:
    """Test that request identities distinguish viewports."""

    def test_same_template_two_viewports_have_distinct_identities(self) -> None:
        template = "BBOX=[bboxWest],[bboxSouth],[bboxEast],[bboxNorth]"
        first = build_request_url(
            href="https://example.com/data.kml",
            view_format=template,
            bbox=Bbox(west=0, south=0, east=1, north=1),
        )
        second = build_request_url(
            href="https://example.com/data.kml",
            view_format=template,
            bbox=Bbox(west=10, south=10, east=20, north=20),
        )

        assert first.identity != second.identity

    def test_identity_is_stable(self) -> None:
        kwargs = {
            "href": "https://example.com/data.kml",
            "view_format": "BBOX=[bboxWest],[bboxSouth],[bboxEast],[bboxNorth]",
            "bbox": Bbox(west=0, south=0, east=1, north=1),
        }

        assert (
            build_request_url(**kwargs).identity
            == build_request_url(
                **kwargs,
            ).identity
        )


class TestFromViews:
    """Test building view parameters from fastkml views."""

    def test_from_camera(self) -> None:
        camera = Camera(
            longitude=10.0,
            latitude=20.0,
            altitude=1000.0,
            heading=90.0,
            tilt=45.0,
        )

        view = ViewParameters.from_camera(camera)

        assert view.camera_lon == 10.0
        assert view.camera_lat == 20.0
        assert view.camera_alt == 1000.0
        assert view.lookat_heading == 90.0
        assert view.lookat_tilt == 45.0
        assert view.lookat_lon is None

    def test_from_lookat(self) -> None:
        look_at = LookAt(
            longitude=10.0,
            latitude=20.0,
            range=5000.0,
            heading=90.0,
            tilt=45.0,
        )

        view = ViewParameters.from_lookat(look_at)

        assert view.lookat_lon == 10.0
        assert view.lookat_lat == 20.0
        assert view.lookat_range == 5000.0
        assert view.camera_lon is None

    def test_camera_variables_in_template(self) -> None:
        camera = Camera(longitude=10.0, latitude=20.0, altitude=1000.0)

        request = build_request_url(
            href="https://example.com/data.kml",
            view_format="lon=[cameraLon]&lat=[cameraLat]&alt=[cameraAlt]",
            view=ViewParameters.from_camera(camera),
        )

        assert request.url == ("https://example.com/data.kml?lon=10&lat=20&alt=1000")


class TestFromLink:
    """Test building a request preview from a Link object."""

    def test_from_link(self) -> None:
        link = Link(
            href="https://example.com/data.kml",
            view_format="BBOX=[bboxWest],[bboxSouth],[bboxEast],[bboxNorth]",
            http_query="clientName=[clientName]",
        )

        request = NetworkLinkRequest.from_link(
            link,
            bbox=Bbox(west=0, south=0, east=1, north=1),
            client=ClientInfo(client_name="fastkml"),
        )

        assert request.url == (
            "https://example.com/data.kml?BBOX=0,0,1,1&clientName=fastkml"
        )
