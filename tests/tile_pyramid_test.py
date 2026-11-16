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
"""Test the offline PhotoOverlay tile pyramid planning."""

import pytest

from fastkml import tile_pyramid
from fastkml.enums import GridOrigin
from fastkml.links import Icon
from fastkml.overlays import ImagePyramid
from fastkml.overlays import PhotoOverlay
from fastkml.tile_pyramid import PixelRegion
from fastkml.tile_pyramid import RegionRequest
from fastkml.tile_pyramid import TileCoordinate
from fastkml.tile_pyramid import build_tile_plan
from fastkml.tile_pyramid import level_dimensions
from fastkml.tile_pyramid import maximum_level
from fastkml.tile_pyramid import render_tile_href
from fastkml.tile_pyramid import tile_pixel_region
from fastkml.tile_pyramid import tile_plan_from_photo_overlay
from fastkml.tile_pyramid import tiles_for_region

HREF_TEMPLATE = "./L$[level]/row-$[x]_col-$[y].jpg"


def make_pyramid(
    max_width: int = 1000,
    max_height: int = 600,
    tile_size: int = 256,
    grid_origin: GridOrigin = GridOrigin.lower_left,
) -> ImagePyramid:
    """Build an ImagePyramid with the given dimensions."""
    return ImagePyramid(
        tile_size=tile_size,
        max_width=max_width,
        max_height=max_height,
        grid_origin=grid_origin,
    )


class TestPixelRegion:
    """Test the PixelRegion value object."""

    def test_dimensions_and_empty(self) -> None:
        region = PixelRegion(10, 20, 40, 70)

        assert region.width == 30
        assert region.height == 50
        assert not region.empty
        assert PixelRegion(0, 0, 0, 10).empty
        assert PixelRegion(0, 0, 10, 0).empty

    def test_invalid_bounds_raise(self) -> None:
        with pytest.raises(ValueError, match="ordered"):
            PixelRegion(10, 0, 5, 10)
        with pytest.raises(ValueError, match="ordered"):
            PixelRegion(0, 10, 10, 5)

    def test_intersection(self) -> None:
        left = PixelRegion(0, 0, 100, 100)
        right = PixelRegion(50, 60, 150, 150)

        assert left.intersection(right) == PixelRegion(50, 60, 100, 100)

    def test_intersection_touching_edges_is_empty(self) -> None:
        assert PixelRegion(0, 0, 10, 10).intersection(
            PixelRegion(10, 0, 20, 10),
        ) is None
        assert PixelRegion(0, 0, 10, 10).intersection(
            PixelRegion(0, 10, 10, 20),
        ) is None


class TestTileCoordinate:
    """Test the TileCoordinate value object."""

    def test_negative_coordinates_raise(self) -> None:
        with pytest.raises(ValueError, match="non negative"):
            TileCoordinate(level=-1, x=0, y=0)
        with pytest.raises(ValueError, match="non negative"):
            TileCoordinate(level=0, x=-1, y=0)
        with pytest.raises(ValueError, match="non negative"):
            TileCoordinate(level=0, x=0, y=-1)

    def test_coordinates_are_hashable(self) -> None:
        coordinates = {
            TileCoordinate(1, 0, 0),
            TileCoordinate(1, 0, 0),
            TileCoordinate(1, 0, 1),
        }

        assert coordinates == {
            TileCoordinate(1, 0, 0),
            TileCoordinate(1, 0, 1),
        }


class TestPyramidGeometry:
    """Test pyramid level and grid calculations."""

    @pytest.mark.parametrize(
        ("width", "height", "expected_level"),
        [
            (256, 256, 0),
            (257, 256, 1),
            (512, 256, 1),
            (1000, 600, 2),
            (1024, 1024, 2),
            (1025, 1024, 3),
        ],
    )
    def test_maximum_level(
        self,
        width: int,
        height: int,
        expected_level: int,
    ) -> None:
        pyramid = make_pyramid(width, height)

        assert maximum_level(pyramid) == expected_level

    def test_level_dimensions_halve_with_ceiling(self) -> None:
        pyramid = make_pyramid(1000, 600)

        assert level_dimensions(pyramid, 2) == (1000, 600)
        assert level_dimensions(pyramid, 1) == (500, 300)
        assert level_dimensions(pyramid, 0) == (250, 150)

    def test_grid_dimensions_cover_partial_border_tiles(self) -> None:
        pyramid = make_pyramid(1000, 600)

        assert tile_pyramid.grid_dimensions(pyramid, 2) == (4, 3)
        assert tile_pyramid.grid_dimensions(pyramid, 1) == (2, 2)
        assert tile_pyramid.grid_dimensions(pyramid, 0) == (1, 1)

    def test_invalid_pyramid_raises(self) -> None:
        with pytest.raises(ValueError, match="tile_size"):
            maximum_level(make_pyramid(tile_size=100))
        with pytest.raises(ValueError, match="power of 2"):
            maximum_level(
                ImagePyramid(
                    tile_size=0,
                    max_width=100,
                    max_height=100,
                ),
            )
        with pytest.raises(ValueError, match="max_width"):
            maximum_level(
                ImagePyramid(
                    tile_size=256,
                    max_width=0,
                    max_height=100,
                ),
            )

    def test_level_outside_range_raises(self) -> None:
        pyramid = make_pyramid()

        with pytest.raises(ValueError, match="outside the pyramid range"):
            level_dimensions(pyramid, 3)
        with pytest.raises(ValueError, match="outside the pyramid range"):
            tiles_for_region(pyramid, PixelRegion(0, 0, 10, 10), 3)


class TestTilesForRegion:
    """Test tile coordinate selection for pixel regions."""

    def test_full_image_covers_whole_grid(self) -> None:
        pyramid = make_pyramid(1000, 600)

        coordinates = tiles_for_region(
            pyramid,
            PixelRegion(0, 0, 1000, 600),
            2,
        )

        assert len(coordinates) == 12
        assert len(set(coordinates)) == len(coordinates)
        assert {coordinate.x for coordinate in coordinates} == {0, 1, 2, 3}
        assert {coordinate.y for coordinate in coordinates} == {0, 1, 2}

    def test_lower_left_origin_counts_rows_from_bottom(self) -> None:
        pyramid = make_pyramid(grid_origin=GridOrigin.lower_left)

        top_left = tiles_for_region(pyramid, PixelRegion(0, 0, 1, 1), 2)
        bottom_right = tiles_for_region(
            pyramid,
            PixelRegion(999, 599, 1000, 600),
            2,
        )

        assert top_left == (TileCoordinate(2, 0, 2),)
        assert bottom_right == (TileCoordinate(2, 3, 0),)

    def test_upper_left_origin_counts_rows_from_top(self) -> None:
        pyramid = make_pyramid(grid_origin=GridOrigin.upper_left)

        top_left = tiles_for_region(pyramid, PixelRegion(0, 0, 1, 1), 2)
        bottom_right = tiles_for_region(
            pyramid,
            PixelRegion(999, 599, 1000, 600),
            2,
        )

        assert top_left == (TileCoordinate(2, 0, 0),)
        assert bottom_right == (TileCoordinate(2, 3, 2),)

    def test_border_region_includes_partial_edge_tiles(self) -> None:
        pyramid = make_pyramid(1000, 600)
        region = PixelRegion(900, 500, 1000, 600)

        coordinates = tiles_for_region(pyramid, region, 2)

        assert coordinates == (
            TileCoordinate(2, 3, 0),
            TileCoordinate(2, 3, 1),
        )
        covered = PixelRegion(
            min(tile_pixel_region(pyramid, c).x_min for c in coordinates),
            min(tile_pixel_region(pyramid, c).y_min for c in coordinates),
            max(tile_pixel_region(pyramid, c).x_max for c in coordinates),
            max(tile_pixel_region(pyramid, c).y_max for c in coordinates),
        )
        assert covered.x_min <= region.x_min
        assert covered.y_min <= region.y_min
        assert covered.x_max >= region.x_max
        assert covered.y_max >= region.y_max

    def test_region_fully_outside_image_has_no_tiles(self) -> None:
        pyramid = make_pyramid()

        assert tiles_for_region(
            pyramid,
            PixelRegion(2000, 2000, 3000, 3000),
            2,
        ) == ()
        assert tiles_for_region(
            pyramid,
            PixelRegion(-100, -100, -10, -10),
            2,
        ) == ()

    def test_region_partly_outside_image_clamps_to_image(self) -> None:
        pyramid = make_pyramid()

        coordinates = tiles_for_region(
            pyramid,
            PixelRegion(-50, -30, 100, 100),
            2,
        )

        assert coordinates == (TileCoordinate(2, 0, 2),)

    def test_tiles_are_deduplicated(self) -> None:
        pyramid = make_pyramid(1024, 1024, grid_origin=GridOrigin.upper_left)
        region = PixelRegion(10, 10, 300, 300)

        coordinates = tiles_for_region(pyramid, region, 2)

        assert len(coordinates) == len(set(coordinates))
        assert coordinates == (
            TileCoordinate(2, 0, 0),
            TileCoordinate(2, 0, 1),
            TileCoordinate(2, 1, 0),
            TileCoordinate(2, 1, 1),
        )

    def test_coarser_level_uses_larger_pixel_spans(self) -> None:
        pyramid = make_pyramid(1000, 600)

        coordinates = tiles_for_region(
            pyramid,
            PixelRegion(0, 0, 1000, 600),
            1,
        )

        assert set(coordinates) == {
            TileCoordinate(1, column, row)
            for row in (0, 1)
            for column in (0, 1)
        }


class TestTilePixelRegion:
    """Test the valid pixel range of individual tiles."""

    def test_full_tile_region(self) -> None:
        pyramid = make_pyramid(1024, 1024, grid_origin=GridOrigin.upper_left)

        assert tile_pixel_region(
            pyramid,
            TileCoordinate(2, 1, 1),
        ) == PixelRegion(256, 256, 512, 512)

    def test_edge_tile_is_capped_to_image(self) -> None:
        pyramid = make_pyramid(1000, 600)

        assert tile_pixel_region(
            pyramid,
            TileCoordinate(2, 3, 0),
        ) == PixelRegion(768, 512, 1000, 600)

    def test_lower_left_origin_flips_row(self) -> None:
        pyramid = make_pyramid(grid_origin=GridOrigin.lower_left)

        assert tile_pixel_region(
            pyramid,
            TileCoordinate(2, 0, 2),
        ) == PixelRegion(0, 0, 256, 256)
        assert tile_pixel_region(
            pyramid,
            TileCoordinate(2, 0, 0),
        ) == PixelRegion(0, 512, 256, 600)

    def test_unknown_tile_raises(self) -> None:
        pyramid = make_pyramid(1000, 600)

        with pytest.raises(ValueError, match="does not exist"):
            tile_pixel_region(pyramid, TileCoordinate(2, 4, 0))
        with pytest.raises(ValueError, match="does not exist"):
            tile_pixel_region(pyramid, TileCoordinate(2, 0, 3))


class TestRenderTileHref:
    """Test Icon href placeholder substitution."""

    def test_substitutes_all_placeholders(self) -> None:
        assert render_tile_href(
            HREF_TEMPLATE,
            TileCoordinate(2, 3, 1),
        ) == "./L2/row-3_col-1.jpg"

    def test_template_without_placeholders_is_unchanged(self) -> None:
        assert render_tile_href("photo.jpg", TileCoordinate(0, 0, 0)) == "photo.jpg"

    def test_none_template_renders_empty(self) -> None:
        assert render_tile_href(None, TileCoordinate(0, 0, 0)) == ""


class TestBuildTilePlan:
    """Test the deduplicated tile manifest and coverage accounting."""

    def test_shared_tile_is_listed_once_with_both_regions(self) -> None:
        pyramid = make_pyramid(1024, 1024, grid_origin=GridOrigin.upper_left)
        requests = (
            RegionRequest(PixelRegion(0, 0, 300, 300), 2),
            RegionRequest(PixelRegion(200, 200, 500, 500), 2),
        )

        plan = build_tile_plan(pyramid, HREF_TEMPLATE, requests)

        assert len(plan.tiles) == len({tile.coordinate for tile in plan.tiles})
        shared = plan.tile_for(TileCoordinate(2, 1, 1))
        assert shared is not None
        assert len(shared.requested_regions) == 2
        assert shared.href == "./L2/row-1_col-1.jpg"

    def test_available_target_tile_is_covered_and_completes_region(self) -> None:
        pyramid = make_pyramid(1024, 1024, grid_origin=GridOrigin.upper_left)
        requests = (RegionRequest(PixelRegion(0, 0, 256, 256), 2),)
        available = frozenset({TileCoordinate(2, 0, 0)})

        plan = build_tile_plan(pyramid, HREF_TEMPLATE, requests, available)
        coverage = plan.region_coverages[0]

        assert coverage.covered_coordinates == available
        assert coverage.pending_coordinates == frozenset()
        assert coverage.is_complete
        assert plan.pending_tiles == ()
        assert plan.covered_coordinates == available

    def test_low_resolution_tile_is_only_a_fallback(self) -> None:
        pyramid = make_pyramid(1024, 1024, grid_origin=GridOrigin.upper_left)
        requests = (RegionRequest(PixelRegion(0, 0, 256, 256), 2),)
        available = frozenset({TileCoordinate(0, 0, 0)})

        plan = build_tile_plan(pyramid, HREF_TEMPLATE, requests, available)
        coverage = plan.region_coverages[0]

        assert coverage.covered_coordinates == frozenset()
        assert coverage.pending_coordinates == frozenset(
            {TileCoordinate(2, 0, 0)},
        )
        assert coverage.fallback_coordinates == frozenset(
            {TileCoordinate(0, 0, 0)},
        )
        assert not coverage.is_complete
        assert plan.covered_coordinates == frozenset()
        assert plan.pending_coordinates == frozenset(
            {TileCoordinate(2, 0, 0)},
        )

    def test_fallback_walks_down_to_closest_available_level(self) -> None:
        pyramid = make_pyramid(1024, 1024, grid_origin=GridOrigin.upper_left)
        requests = (RegionRequest(PixelRegion(0, 0, 256, 256), 2),)
        available = frozenset({TileCoordinate(1, 0, 0)})

        plan = build_tile_plan(pyramid, HREF_TEMPLATE, requests, available)

        assert plan.region_coverages[0].fallback_coordinates == frozenset(
            {TileCoordinate(1, 0, 0)},
        )

    def test_out_of_bounds_pixels_are_reported(self) -> None:
        pyramid = make_pyramid(1000, 600)
        requests = (
            RegionRequest(PixelRegion(-50, -30, 100, 100), 2),
        )

        plan = build_tile_plan(pyramid, HREF_TEMPLATE, requests)
        coverage = plan.region_coverages[0]

        assert coverage.region == PixelRegion(0, 0, 100, 100)
        assert PixelRegion(-50, -30, 0, 100) in coverage.out_of_bounds_regions
        assert PixelRegion(0, -30, 100, 0) in coverage.out_of_bounds_regions
        assert coverage.is_complete is False
        assert coverage.target_coordinates == frozenset(
            {TileCoordinate(2, 0, 2)},
        )

    def test_fully_out_of_bounds_region_has_no_tiles(self) -> None:
        pyramid = make_pyramid()
        requests = (
            RegionRequest(PixelRegion(2000, 2000, 3000, 3000), 2),
        )

        plan = build_tile_plan(pyramid, HREF_TEMPLATE, requests)
        coverage = plan.region_coverages[0]

        assert coverage.target_coordinates == frozenset()
        assert coverage.region.empty
        assert len(coverage.out_of_bounds_regions) == 1
        assert not coverage.is_complete
        assert plan.tiles == ()

    def test_edge_tiles_flagged_with_capped_pixel_region(self) -> None:
        pyramid = make_pyramid(1000, 600)
        requests = (
            RegionRequest(PixelRegion(900, 500, 1000, 600), 2),
        )

        plan = build_tile_plan(pyramid, HREF_TEMPLATE, requests)
        edge_tiles = {tile.coordinate: tile for tile in plan.tiles if tile.is_edge_tile}

        assert set(edge_tiles) == {
            TileCoordinate(2, 3, 0),
            TileCoordinate(2, 3, 1),
        }
        assert edge_tiles[TileCoordinate(2, 3, 0)].pixel_region == PixelRegion(
            768,
            512,
            1000,
            600,
        )

    def test_interior_tile_is_not_flagged_as_edge(self) -> None:
        pyramid = make_pyramid(1024, 1024, grid_origin=GridOrigin.upper_left)
        requests = (RegionRequest(PixelRegion(0, 0, 512, 512), 2),)

        plan = build_tile_plan(pyramid, HREF_TEMPLATE, requests)

        assert all(not tile.is_edge_tile for tile in plan.tiles)

    def test_tiles_sorted_and_lookup_helpers(self) -> None:
        pyramid = make_pyramid(1024, 1024, grid_origin=GridOrigin.upper_left)
        requests = (RegionRequest(PixelRegion(0, 0, 512, 512), 2),)

        plan = build_tile_plan(pyramid, HREF_TEMPLATE, requests)
        keys = [(tile.level, tile.column, tile.row) for tile in plan.tiles]

        assert keys == sorted(keys)
        assert plan.tile_for(TileCoordinate(2, 9, 9)) is None
        assert plan.required_coordinates == frozenset(
            tile.coordinate for tile in plan.tiles
        )

    def test_requests_at_different_levels_remain_distinct(self) -> None:
        pyramid = make_pyramid(1024, 1024, grid_origin=GridOrigin.upper_left)
        requests = (
            RegionRequest(PixelRegion(0, 0, 256, 256), 2),
            RegionRequest(PixelRegion(0, 0, 256, 256), 1),
        )

        plan = build_tile_plan(pyramid, HREF_TEMPLATE, requests)

        assert plan.tile_for(TileCoordinate(2, 0, 0)) is not None
        assert plan.tile_for(TileCoordinate(1, 0, 0)) is not None
        assert len(plan.region_coverages) == 2

    def test_region_tile_union_covers_requested_pixels(self) -> None:
        pyramid = make_pyramid(1000, 600)
        region = PixelRegion(123, 45, 987, 567)
        requests = (RegionRequest(region, 2),)

        plan = build_tile_plan(pyramid, HREF_TEMPLATE, requests)
        coverage = plan.region_coverages[0]
        tile_regions = [
            tile_pixel_region(pyramid, coordinate)
            for coordinate in coverage.target_coordinates
        ]

        assert all(
            tile_region.intersection(coverage.region) is not None
            for tile_region in tile_regions
        )
        assert min(t.x_min for t in tile_regions) <= region.x_min
        assert min(t.y_min for t in tile_regions) <= region.y_min
        assert max(t.x_max for t in tile_regions) >= region.x_max
        assert max(t.y_max for t in tile_regions) >= region.y_max


class TestTilePlanFromPhotoOverlay:
    """Test building a plan from a PhotoOverlay object."""

    def test_plan_uses_overlay_pyramid_and_icon_href(self) -> None:
        pyramid = make_pyramid(1024, 1024, grid_origin=GridOrigin.upper_left)
        overlay = PhotoOverlay(
            icon=Icon(href=HREF_TEMPLATE),
            image_pyramid=pyramid,
        )
        requests = (RegionRequest(PixelRegion(0, 0, 256, 256), 2),)

        plan = tile_plan_from_photo_overlay(overlay, requests)

        assert plan.pyramid is pyramid
        assert plan.href_template == HREF_TEMPLATE
        assert plan.tile_for(TileCoordinate(2, 0, 0)) is not None

    def test_overlay_without_pyramid_raises(self) -> None:
        overlay = PhotoOverlay(icon=Icon(href=HREF_TEMPLATE))

        with pytest.raises(ValueError, match="no ImagePyramid"):
            tile_plan_from_photo_overlay(
                overlay,
                (RegionRequest(PixelRegion(0, 0, 10, 10), 0),),
            )

    def test_overlay_without_icon_still_plans(self) -> None:
        overlay = PhotoOverlay(image_pyramid=make_pyramid(256, 256))

        plan = tile_plan_from_photo_overlay(
            overlay,
            (RegionRequest(PixelRegion(0, 0, 256, 256), 0),),
        )

        assert plan.href_template == ""
        assert plan.tiles[0].href == ""
