#!/usr/bin/env python
"""
Offline replay of NetworkLink/Region activity over a viewport trajectory.

The script builds a small KML document with two region-based network links
(one of them crossing the antimeridian), replays a timestamped viewport
trajectory and prints the request/reuse/release decision for every link.
"""

from fastkml import Document
from fastkml import Link
from fastkml import NetworkLink
from fastkml.replay import Action
from fastkml.replay import ReplayConfig
from fastkml.replay import ReplayEngine
from fastkml.replay import Viewport
from fastkml.views import LatLonAltBox
from fastkml.views import Lod
from fastkml.views import Region


def region(west: float, south: float, east: float, north: float) -> Region:
    """Build a region covering the given geographic box."""
    return Region(
        lat_lon_alt_box=LatLonAltBox(
            north=north,
            south=south,
            east=east,
            west=west,
        ),
        lod=Lod(min_lod_pixels=0, max_lod_pixels=-1),
    )


def network_link(link_id: str, href: str, box: Region) -> NetworkLink:
    """Build a region-bound network link pointing at ``href``."""
    return NetworkLink(
        id=link_id,
        name=link_id,
        region=box,
        link=Link(href=href),
    )


def main() -> None:
    """Build the document, replay the trajectory and print the decisions."""
    document = Document(
        features=[
            network_link(
                "europe",
                "https://example.com/europe.kml",
                region(-10.0, 35.0, 30.0, 60.0),
            ),
            network_link(
                "pacific",
                "https://example.com/pacific.kml",
                # east < west declares an antimeridian-crossing box
                region(170.0, -10.0, -170.0, 10.0),
            ),
        ],
    )

    trajectory = [
        Viewport(0.0, -10.0, 35.0, 30.0, 60.0, 512.0),
        Viewport(1.0, 40.0, 35.0, 70.0, 60.0, 512.0),
        Viewport(2.0, 175.0, -5.0, -175.0, 5.0, 512.0),
        Viewport(3.0, -10.0, 35.0, 30.0, 60.0, 512.0),
    ]

    engine = ReplayEngine(
        document,
        trajectory,
        ReplayConfig(default_cache_seconds=300.0),
    )
    for event in engine.run():
        if event.action == Action.request:
            verb = "REQUEST"
        elif event.action == Action.reuse:
            verb = "REUSE  "
        else:
            verb = "RELEASE"
        reasons = ",".join(sorted(event.reasons))
        print(f"t={event.timestamp:4.0f}  {verb}  {event.link_id:8}  {reasons}")


if __name__ == "__main__":
    main()
