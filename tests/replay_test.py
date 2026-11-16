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

"""Test the offline NetworkLink/Region replay engine."""

from fastkml import KML
from fastkml import Document
from fastkml import Folder
from fastkml import Link
from fastkml import NetworkLink
from fastkml.enums import RefreshMode
from fastkml.enums import ViewRefreshMode
from fastkml.replay import Action
from fastkml.replay import ReplayConfig
from fastkml.replay import ReplayEngine
from fastkml.replay import Viewport
from fastkml.replay import collect_network_links
from fastkml.replay import region_is_active
from fastkml.views import LatLonAltBox
from fastkml.views import Lod
from fastkml.views import Region
from tests.base import Lxml
from tests.base import StdLibrary


def make_region(
    west: float,
    south: float,
    east: float,
    north: float,
    min_lod: int = 0,
    max_lod: int = -1,
) -> Region:
    return Region(
        lat_lon_alt_box=LatLonAltBox(
            north=north,
            south=south,
            east=east,
            west=west,
        ),
        lod=Lod(
            min_lod_pixels=min_lod,
            max_lod_pixels=max_lod,
        ),
    )


def make_link(
    href: str,
    refresh_mode: RefreshMode | None = None,
    refresh_interval: float | None = None,
    view_refresh_mode: ViewRefreshMode | None = None,
    view_refresh_time: float | None = None,
) -> Link:
    return Link(
        href=href,
        refresh_mode=refresh_mode,
        refresh_interval=refresh_interval,
        view_refresh_mode=view_refresh_mode,
        view_refresh_time=view_refresh_time,
    )


def make_network_link(
    link_id: str,
    href: str,
    region: Region | None = None,
    **link_kwargs: object,
) -> NetworkLink:
    return NetworkLink(
        id=link_id,
        region=region,
        link=make_link(href, **link_kwargs),
    )


def make_viewport(
    timestamp: float,
    west: float = 0.0,
    south: float = 0.0,
    east: float = 10.0,
    north: float = 10.0,
    pixel_span: float = 512.0,
) -> Viewport:
    return Viewport(
        timestamp=timestamp,
        west=west,
        south=south,
        east=east,
        north=north,
        pixel_span=pixel_span,
    )


def events_for(events: tuple, link_id: str) -> list:
    return [event for event in events if event.link_id == link_id]


def actions_for(events: tuple, link_id: str) -> list[Action]:
    return [event.action for event in events_for(events, link_id)]


def lifecycle_actions_for(events: tuple, link_id: str) -> list[Action]:
    """Actions excluding the synthetic end-of-trace release."""
    return [
        event.action
        for event in events_for(events, link_id)
        if event.reasons != frozenset({"end-of-trace"})
    ]


class TestStdLibrary(StdLibrary):
    """Test with the standard library."""

    def test_request_reuse_release_lifecycle(self) -> None:
        link = make_network_link(
            "nl-1",
            "https://example.com/a.kml",
            region=make_region(-10, -10, 20, 20),
        )
        document = Document(features=[link])
        trajectory = [
            make_viewport(0.0),
            make_viewport(1.0),
            make_viewport(2.0, west=30.0, east=40.0),
        ]

        events = ReplayEngine(document, trajectory).run()

        assert lifecycle_actions_for(events, "nl-1") == [
            Action.request,
            Action.reuse,
            Action.release,
        ]
        assert events_for(events, "nl-1")[2].reasons == frozenset(
            {"region-inactive"},
        )

    def test_quick_out_and_in_reuses_within_cache_validity(self) -> None:
        link = make_network_link(
            "nl-1",
            "https://example.com/a.kml",
            region=make_region(-10, -10, 20, 20),
        )
        document = Document(features=[link])
        trajectory = [
            make_viewport(0.0),
            make_viewport(1.0, west=30.0, east=40.0),
            make_viewport(2.0),
        ]

        events = ReplayEngine(
            document,
            trajectory,
            ReplayConfig(default_cache_seconds=300.0),
        ).run()

        assert lifecycle_actions_for(events, "nl-1") == [
            Action.request,
            Action.release,
            Action.reuse,
        ]
        assert events_for(events, "nl-1")[2].reasons == frozenset({"reactivate"})

    def test_returning_after_expiry_requests_again(self) -> None:
        link = make_network_link(
            "nl-1",
            "https://example.com/a.kml",
            region=make_region(-10, -10, 20, 20),
        )
        document = Document(features=[link])
        trajectory = [
            make_viewport(0.0),
            make_viewport(10.0, west=30.0, east=40.0),
            make_viewport(400.0),
        ]

        events = ReplayEngine(
            document,
            trajectory,
            ReplayConfig(default_cache_seconds=300.0),
        ).run()

        assert lifecycle_actions_for(events, "nl-1") == [
            Action.request,
            Action.release,
            Action.request,
        ]
        assert events_for(events, "nl-1")[2].reasons == frozenset({"expired"})

    def test_antimeridian_region_matches_literal_declaration(self) -> None:
        region = make_region(170.0, -10.0, -170.0, 10.0)
        inside = make_viewport(0.0, west=175.0, east=-175.0)
        outside = make_viewport(1.0, west=20.0, east=30.0)

        assert region_is_active(region, inside)
        assert not region_is_active(region, outside)

        link = make_network_link(
            "nl-1",
            "https://example.com/date-line.kml",
            region=region,
        )
        events = ReplayEngine(
            Document(features=[link]),
            [inside, outside],
        ).run()

        assert actions_for(events, "nl-1") == [Action.request, Action.release]

    def test_antimeridian_viewport_intersects_normal_region(self) -> None:
        region = make_region(175.0, -10.0, -175.0, 10.0)
        viewport = make_viewport(0.0, west=178.0, east=-178.0)

        assert region_is_active(region, viewport)

    def test_parent_region_deactivation_releases_child_link(self) -> None:
        parent_region = make_region(-10, -10, 20, 20)
        child_region = make_region(-5, -5, 15, 15)
        child_link = make_network_link(
            "child",
            "https://example.com/child.kml",
            region=child_region,
        )
        parent = Folder(region=parent_region, features=[child_link])
        document = Document(features=[parent])
        trajectory = [
            make_viewport(0.0),
            make_viewport(1.0, west=30.0, east=40.0),
        ]

        events = ReplayEngine(document, trajectory).run()

        assert actions_for(events, "child") == [Action.request, Action.release]
        assert events_for(events, "child")[1].reasons == frozenset(
            {"parent-region-inactive"},
        )

    def test_lod_ramp_activates_and_deactivates(self) -> None:
        region = make_region(-10, -10, 20, 20, min_lod=256, max_lod=1024)
        link = make_network_link(
            "nl-1",
            "https://example.com/lod.kml",
            region=region,
        )
        document = Document(features=[link])
        trajectory = [
            make_viewport(0.0, pixel_span=512.0),
            make_viewport(1.0, pixel_span=128.0),
            make_viewport(2.0, pixel_span=512.0),
        ]

        events = ReplayEngine(document, trajectory).run()

        assert lifecycle_actions_for(events, "nl-1") == [
            Action.request,
            Action.release,
            Action.reuse,
        ]
        assert events_for(events, "nl-1")[2].reasons == frozenset({"reactivate"})

    def test_shared_href_keeps_separate_activation_reasons(self) -> None:
        region_a = make_region(-10, -10, 20, 20)
        region_b = make_region(40.0, -10.0, 60.0, 20.0)
        link_a = make_network_link("a", "https://example.com/shared.kml", region_a)
        link_b = make_network_link("b", "https://example.com/shared.kml", region_b)
        document = Document(features=[link_a, link_b])
        trajectory = [
            make_viewport(0.0),
            make_viewport(1.0, west=45.0, east=55.0),
            make_viewport(2.0, west=-5.0, east=65.0),
            make_viewport(3.0, west=80.0, east=90.0),
        ]

        events = ReplayEngine(document, trajectory).run()

        assert lifecycle_actions_for(events, "a") == [
            Action.request,
            Action.release,
            Action.reuse,
            Action.release,
        ]
        assert lifecycle_actions_for(events, "b") == [
            Action.reuse,
            Action.reuse,
            Action.release,
        ]
        assert events_for(events, "b")[0].reasons == frozenset({"reactivate"})
        release_events = [
            event
            for event in events
            if event.action == Action.release
            and event.href == "https://example.com/shared.kml"
            and event.reasons != frozenset({"end-of-trace"})
        ]
        assert len(release_events) == 3
        assert all(
            event.reasons == frozenset({"region-inactive"}) for event in release_events
        )

    def test_shared_href_survives_while_one_reference_stays_active(self) -> None:
        region_a = make_region(-10, -10, 20, 20)
        region_b = make_region(-10, -10, 60, 20)
        link_a = make_network_link("a", "https://example.com/shared.kml", region_a)
        link_b = make_network_link("b", "https://example.com/shared.kml", region_b)
        document = Document(features=[link_a, link_b])
        trajectory = [
            make_viewport(0.0),
            make_viewport(1.0, west=30.0, east=40.0),
            make_viewport(2.0, west=30.0, east=40.0),
        ]

        events = ReplayEngine(document, trajectory).run()

        assert lifecycle_actions_for(events, "a") == [Action.request, Action.release]
        assert lifecycle_actions_for(events, "b") == [
            Action.request,
            Action.reuse,
            Action.reuse,
        ]
        assert not [
            event
            for event in events_for(events, "b")
            if event.action == Action.release
            and event.reasons != frozenset({"end-of-trace"})
        ]

    def test_on_interval_refresh_requests_again(self) -> None:
        link = make_network_link(
            "nl-1",
            "https://example.com/timed.kml",
            region=make_region(-10, -10, 20, 20),
            refresh_mode=RefreshMode.on_interval,
            refresh_interval=5.0,
        )
        document = Document(features=[link])
        trajectory = [
            make_viewport(0.0),
            make_viewport(4.0),
            make_viewport(5.0),
        ]

        events = ReplayEngine(document, trajectory).run()

        assert lifecycle_actions_for(events, "nl-1") == [
            Action.request,
            Action.reuse,
            Action.request,
        ]
        assert events_for(events, "nl-1")[2].reasons == frozenset({"on-interval"})

    def test_on_stop_requests_after_view_refresh_time(self) -> None:
        link = make_network_link(
            "nl-1",
            "https://example.com/stop.kml",
            region=make_region(-10, -10, 20, 20),
            view_refresh_mode=ViewRefreshMode.on_stop,
            view_refresh_time=2.0,
        )
        document = Document(features=[link])
        trajectory = [
            make_viewport(0.0),
            make_viewport(1.0, west=1.0, east=11.0),
            make_viewport(2.0, west=1.0, east=11.0),
            make_viewport(3.0, west=1.0, east=11.0),
        ]

        events = ReplayEngine(document, trajectory).run()

        assert lifecycle_actions_for(events, "nl-1") == [
            Action.request,
            Action.reuse,
            Action.reuse,
            Action.request,
        ]
        assert events_for(events, "nl-1")[3].reasons == frozenset({"on-stop"})

    def test_on_region_ignores_view_refresh_time(self) -> None:
        link = make_network_link(
            "nl-1",
            "https://example.com/region.kml",
            region=make_region(-10, -10, 20, 20),
            view_refresh_mode=ViewRefreshMode.on_region,
            view_refresh_time=10.0,
        )
        document = Document(features=[link])
        trajectory = [make_viewport(0.0), make_viewport(1.0)]

        events = ReplayEngine(document, trajectory).run()

        assert lifecycle_actions_for(events, "nl-1") == [Action.request, Action.reuse]

    def test_server_expiry_overrides_default_cache_period(self) -> None:
        link = make_network_link(
            "nl-1",
            "https://example.com/expires.kml",
            region=make_region(-10, -10, 20, 20),
        )
        document = Document(features=[link])
        trajectory = [
            make_viewport(0.0),
            make_viewport(10.0, west=30.0, east=40.0),
            make_viewport(20.0),
        ]
        engine = ReplayEngine(
            document,
            trajectory,
            ReplayConfig(default_cache_seconds=300.0),
        )
        engine.set_expiry("https://example.com/expires.kml", 15.0)

        events = engine.run()

        assert lifecycle_actions_for(events, "nl-1") == [
            Action.request,
            Action.release,
            Action.request,
        ]
        assert events_for(events, "nl-1")[2].reasons == frozenset({"expired"})

    def test_invisible_link_is_never_requested(self) -> None:
        link = make_network_link(
            "nl-1",
            "https://example.com/hidden.kml",
            region=make_region(-10, -10, 20, 20),
        )
        link.visibility = False
        document = Document(features=[link])

        events = ReplayEngine(document, [make_viewport(0.0)]).run()

        assert not events

    def test_hiding_active_link_emits_invisible_release(self) -> None:
        link = make_network_link(
            "nl-1",
            "https://example.com/toggle.kml",
            region=make_region(-10, -10, 20, 20),
        )
        document = Document(features=[link])
        first = make_viewport(0.0)
        second = make_viewport(1.0)
        engine = ReplayEngine(document, [first, second])
        engine._step(first)
        link.visibility = False
        engine._step(second)

        lifecycle = [
            event
            for event in events_for(engine.events, "nl-1")
            if event.reasons != frozenset({"end-of-trace"})
        ]
        assert [event.action for event in lifecycle] == [
            Action.request,
            Action.release,
        ]
        assert lifecycle[1].reasons == frozenset({"invisible"})

    def test_collect_network_links_walks_nested_containers(self) -> None:
        child = make_network_link("child", "https://example.com/c.kml")
        nested = Folder(
            region=make_region(-10, -10, 20, 20),
            features=[child],
        )
        root_link = make_network_link("root", "https://example.com/r.kml")
        document = Document(features=[root_link, nested])

        collected = {
            link.id: regions for link, regions in collect_network_links(document)
        }

        assert set(collected) == {"root", "child"}
        assert collected["root"] == (None,)
        assert len(collected["child"]) == 2
        assert collected["child"][0] is nested.region

    def test_end_of_trace_releases_active_links(self) -> None:
        link = make_network_link(
            "nl-1",
            "https://example.com/a.kml",
            region=make_region(-10, -10, 20, 20),
        )

        events = ReplayEngine(
            Document(features=[link]),
            [make_viewport(0.0)],
        ).run()

        assert actions_for(events, "nl-1") == [Action.request, Action.release]
        assert events_for(events, "nl-1")[1].reasons == frozenset(
            {"end-of-trace"},
        )

    def test_runs_against_parsed_kml_document(self) -> None:
        link = make_network_link(
            "nl-1",
            "https://example.com/parsed.kml",
            region=make_region(-10, -10, 20, 20),
        )
        kml = KML(features=[Document(features=[link])])
        parsed = KML.from_string(kml.to_string())

        events = ReplayEngine(
            parsed,
            [make_viewport(0.0), make_viewport(1.0, west=30.0, east=40.0)],
        ).run()

        assert actions_for(events, "nl-1") == [Action.request, Action.release]


class TestLxml(Lxml, TestStdLibrary):
    """Test with lxml."""
