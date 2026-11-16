"""Test the altitude catalogue and terrain-aware resolution."""

from __future__ import annotations

import datetime
from pathlib import Path

import pygeoif.geometry as geo
import pytest

from fastkml import KML
from fastkml.altitude import AltitudeCatalog
from fastkml.altitude import AltitudeResolution
from fastkml.altitude import AltitudeSourceKind
from fastkml.altitude import Position
from fastkml.altitude import PositionCatalog
from fastkml.altitude import PositionSource
from fastkml.altitude import SeaSurfaceExtension
from fastkml.altitude import TerrainCoverage
from fastkml.altitude import TerrainSample
from fastkml.altitude import build_catalog
from fastkml.altitude import collect_altitudes
from fastkml.altitude import collect_positions
from fastkml.altitude import resolve_altitude
from fastkml.altitude import resolve_altitudes
from fastkml.altitude import resolve_catalog
from fastkml.enums import AltitudeMode
from fastkml.geometry import LineString
from fastkml.geometry import MultiGeometry
from fastkml.geometry import Point
from fastkml.gx import MultiTrack
from fastkml.gx import Track
from fastkml.gx import TrackItem
from fastkml.model import Location
from fastkml.model import Model
from fastkml.overlays import GroundOverlay
from fastkml.overlays import LatLonBox
from fastkml.overlays import PhotoOverlay
from fastkml.times import KmlDateTime

KML_DOCUMENT = """<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document id="doc">
    <name>Survey</name>
    <Placemark id="point-1">
      <name>Point one</name>
      <Point>
        <altitudeMode>relativeToGround</altitudeMode>
        <coordinates>13.4,52.5,10</coordinates>
      </Point>
    </Placemark>
    <Placemark id="line-1">
      <LineString>
        <altitudeMode>absolute</altitudeMode>
        <coordinates>13.4,52.5,100 13.5,52.6,110</coordinates>
      </LineString>
    </Placemark>
    <Placemark id="polygon-1">
      <Polygon>
        <altitudeMode>clampToGround</altitudeMode>
        <outerBoundaryIs>
          <LinearRing><coordinates>0,0,7 1,0,7 1,1,7 0,0,7</coordinates></LinearRing>
        </outerBoundaryIs>
        <innerBoundaryIs>
          <LinearRing>
            <coordinates>0.2,0.2,7 0.4,0.2,7 0.4,0.4,7 0.2,0.2,7</coordinates>
          </LinearRing>
        </innerBoundaryIs>
      </Polygon>
    </Placemark>
    <Placemark id="track-1">
      <gx:Track xmlns:gx="http://www.google.com/kml/ext/2.2">
        <when>2020-01-01T00:00:00Z</when>
        <when>2020-01-01T00:10:00Z</when>
        <gx:coord>13.4 52.5 200</gx:coord>
        <gx:coord>13.5 52.6 210</gx:coord>
      </gx:Track>
    </Placemark>
  </Document>
</kml>"""


class TestPositionCatalog:
    """Collect geometry positions into one catalogue."""

    def test_collects_points_lines_polygons_and_tracks(self) -> None:
        kml = KML.from_string(KML_DOCUMENT)
        catalog = PositionCatalog.from_kml(kml)

        assert isinstance(catalog, PositionCatalog)
        assert len(catalog) == 13
        assert [position.source.feature_id for position in catalog][:3] == [
            "point-1",
            "line-1",
            "line-1",
        ]

    def test_catalog_preserves_original_altitude_and_mode(self) -> None:
        catalog = PositionCatalog.from_kml(KML.from_string(KML_DOCUMENT))
        point = catalog[0]

        assert point.original_altitude == 10
        assert point.original_altitude_mode is AltitudeMode.relative_to_ground
        assert point.effective_altitude_mode is AltitudeMode.relative_to_ground
        assert point.altitude == 10

    def test_catalog_records_geometry_provenance(self) -> None:
        catalog = PositionCatalog.from_kml(KML.from_string(KML_DOCUMENT))

        polygon_positions = [
            position
            for position in catalog
            if position.source.geometry_type == "Polygon"
        ]
        assert polygon_positions[0].source.feature_id == "polygon-1"
        assert polygon_positions[0].source.feature_path.endswith(
            "Placemark[id=polygon-1]",
        )
        assert polygon_positions[0].source.part == "outerBoundaryIs"
        assert polygon_positions[-1].source.part == "innerBoundaryIs[0]"
        assert polygon_positions[0].source.coordinate_index == 0

    def test_track_positions_keep_timestamps(self) -> None:
        catalog = PositionCatalog.from_kml(KML.from_string(KML_DOCUMENT))
        track_positions = [
            position for position in catalog if position.source.geometry_type == "Track"
        ]

        assert len(track_positions) == 2
        assert track_positions[0].source.timestamp is not None
        assert track_positions[0].source.timestamp.startswith("2020-01-01T00:00:00")
        assert track_positions[1].source.timestamp is not None

    def test_two_dimensional_coordinates_have_no_altitude(self) -> None:
        catalog = collect_positions(
            LineString(geometry=geo.LineString(((1, 2), (3, 4))), ns=""),
        )

        assert catalog[0].altitude is None
        assert catalog[0].original_altitude is None
        assert catalog[0].effective_altitude_mode is AltitudeMode.clamp_to_ground

    def test_multigeometry_explodes_into_child_positions(self) -> None:
        multi = MultiGeometry(
            kml_geometries=[
                Point(geometry=geo.Point(1, 2, 3), ns=""),
                LineString(geometry=geo.LineString(((4, 5, 6), (7, 8, 9))), ns=""),
            ],
            ns="",
        )

        catalog = collect_positions(multi)

        assert len(catalog) == 3
        assert catalog[0].source.part == "MultiGeometry[0]"
        assert catalog[1].source.part == "MultiGeometry[1]"
        assert catalog[1].source.coordinate_index == 0

    def test_model_location_is_catalogued(self) -> None:
        model = Model(
            altitude_mode=AltitudeMode.relative_to_ground,
            location=Location(longitude=1, latitude=2, altitude=30),
            ns="",
        )

        catalog = collect_positions(model)

        assert len(catalog) == 1
        assert catalog[0].altitude == 30
        assert catalog[0].source.geometry_type == "Model"

    def test_ground_overlay_center_is_catalogued(self) -> None:
        overlay = GroundOverlay(
            altitude=10,
            altitude_mode=AltitudeMode.absolute,
            lat_lon_box=LatLonBox(west=0, east=2, south=0, north=2),
            ns="",
        )

        catalog = collect_positions(overlay)

        assert len(catalog) == 1
        assert catalog[0].longitude == 1
        assert catalog[0].latitude == 1
        assert catalog[0].altitude == 10
        assert catalog[0].source.geometry_type == "GroundOverlay"

    def test_photo_overlay_point_is_catalogued(self) -> None:
        overlay = PhotoOverlay(
            point=Point(geometry=geo.Point(1, 2, 3), ns=""),
            ns="",
        )

        catalog = collect_positions(overlay)

        assert len(catalog) == 1
        assert catalog[0].source.part == "PhotoOverlay.Point"

    def test_catalog_is_idempotent(self) -> None:
        catalog = collect_positions(
            LineString(geometry=geo.LineString(((1, 2),)), ns=""),
        )

        assert collect_positions(catalog) is catalog
        assert build_catalog(catalog) is catalog
        assert collect_altitudes(catalog) is catalog

    def test_catalog_supports_append_and_iteration(self) -> None:
        catalog = AltitudeCatalog()
        catalog.append(Position(1, 2, 3))

        assert len(catalog) == 1
        assert list(catalog) == [Position(1, 2, 3)]
        assert catalog.positions == (Position(1, 2, 3),)
        assert catalog.entries == (Position(1, 2, 3),)
        assert catalog

    def test_empty_catalog_is_false(self) -> None:
        assert not AltitudeCatalog()


class TestAbsolute:
    """Absolute altitudes never depend on terrain."""

    def test_absolute_altitude_is_kept(self) -> None:
        position = Position(1.0, 2.0, 500.0, AltitudeMode.absolute)

        result = resolve_altitude(position, terrain=None)

        assert result.absolute_altitude == 500.0
        assert result.origin is AltitudeSourceKind.absolute
        assert result.terrain_sample is None

    def test_absolute_altitude_ignores_terrain(self) -> None:
        position = Position(1.0, 2.0, 500.0, AltitudeMode.absolute)
        terrain = TerrainCoverage(
            samples=[TerrainSample(1.0, 2.0, 100.0, coverage_version="v1")],
            version="v1",
        )

        result = resolve_altitude(position, terrain=terrain, terrain_version="v1")

        assert result.absolute_altitude == 500.0
        assert result.terrain_sample is None

    def test_absolute_without_coordinate_altitude_is_unresolved(self) -> None:
        position = Position(1.0, 2.0, None, AltitudeMode.absolute)

        result = resolve_altitude(position)

        assert result.absolute_altitude is None
        assert result.origin is AltitudeSourceKind.absolute
        assert result.reason is not None


class TestTerrainConversion:
    """Relative and clamped altitudes use terrain only when evidence exists."""

    def test_relative_to_ground_adds_terrain(self) -> None:
        position = Position(1.0, 2.0, 10.0, AltitudeMode.relative_to_ground)
        terrain = TerrainCoverage(
            samples=[TerrainSample(1.0, 2.0, 100.0, coverage_version="v1")],
            version="v1",
        )

        result = resolve_altitude(position, terrain=terrain, terrain_version="v1")

        assert result.absolute_altitude == 110.0
        assert result.origin is AltitudeSourceKind.terrain
        assert result.terrain_sample is not None
        assert result.terrain_version == "v1"

    def test_clamp_to_ground_ignores_coordinate_altitude(self) -> None:
        position = Position(1.0, 2.0, 999.0, AltitudeMode.clamp_to_ground)
        terrain = TerrainCoverage(
            samples=[TerrainSample(1.0, 2.0, 100.0, coverage_version="v1")],
            version="v1",
        )

        result = resolve_altitude(position, terrain=terrain, terrain_version="v1")

        assert result.absolute_altitude == 100.0
        assert result.original_altitude == 999.0

    def test_missing_terrain_is_not_zero(self) -> None:
        position = Position(1.0, 2.0, 10.0, AltitudeMode.relative_to_ground)

        result = resolve_altitude(position, terrain=None)

        assert result.absolute_altitude is None
        assert result.resolved is False
        assert "never replaced with 0.0" in (result.reason or "")

    def test_unversioned_coverage_does_not_satisfy_versioned_request(self) -> None:
        position = Position(1.0, 2.0, 10.0, AltitudeMode.relative_to_ground)
        terrain = TerrainCoverage(samples=[(1.0, 2.0, 100.0)])

        result = resolve_altitude(position, terrain=terrain, terrain_version="v1")

        assert result.absolute_altitude is None
        assert "has no version" in (result.reason or "")

    def test_version_mismatch_is_unresolved(self) -> None:
        position = Position(1.0, 2.0, 10.0, AltitudeMode.relative_to_ground)
        terrain = TerrainCoverage(
            samples=[TerrainSample(1.0, 2.0, 100.0, coverage_version="v1")],
            version="v1",
        )

        result = resolve_altitude(position, terrain=terrain, terrain_version="v2")

        assert result.absolute_altitude is None
        assert "'v1'" in (result.reason or "")
        assert "'v2'" in (result.reason or "")

    def test_sample_for_another_version_is_ignored(self) -> None:
        position = Position(1.0, 2.0, 10.0, AltitudeMode.relative_to_ground)
        terrain = TerrainCoverage(
            samples=[
                TerrainSample(1.0, 2.0, 100.0, coverage_version="v1"),
                TerrainSample(1.0, 2.0, 200.0, coverage_version="v2"),
            ],
            version="v2",
        )

        result = resolve_altitude(position, terrain=terrain, terrain_version="v2")

        assert result.absolute_altitude == 210.0
        assert result.terrain_version == "v2"

    def test_terrain_sample_matching_uses_tolerance(self) -> None:
        position = Position(1.0, 2.0, 10.0, AltitudeMode.relative_to_ground)
        terrain = TerrainCoverage(
            samples=[TerrainSample(1.0001, 2.0, 100.0, coverage_version="v1")],
            version="v1",
        )

        result = resolve_altitude(
            position,
            terrain=terrain,
            terrain_version="v1",
            tolerance=20,
        )

        assert result.absolute_altitude == 110.0

    def test_sample_outside_tolerance_is_unresolved(self) -> None:
        position = Position(1.0, 2.0, 10.0, AltitudeMode.relative_to_ground)
        terrain = TerrainCoverage(
            samples=[TerrainSample(1.0001, 2.0, 100.0, coverage_version="v1")],
            version="v1",
        )

        result = resolve_altitude(
            position,
            terrain=terrain,
            terrain_version="v1",
            tolerance=1,
        )

        assert result.absolute_altitude is None

    def test_relative_to_ground_without_coordinate_altitude_is_unresolved(self) -> None:
        position = Position(1.0, 2.0, None, AltitudeMode.relative_to_ground)
        terrain = TerrainCoverage(
            samples=[TerrainSample(1.0, 2.0, 100.0, coverage_version="v1")],
            version="v1",
        )

        result = resolve_altitude(position, terrain=terrain, terrain_version="v1")

        assert result.absolute_altitude is None
        assert "requires an original altitude" in (result.reason or "")


class TestSeaSurface:
    """Sea-surface evidence is kept separate from land terrain."""

    def test_sea_surface_resolves_relative_altitude(self) -> None:
        position = Position(1.0, 2.0, 10.0, AltitudeMode.relative_to_ground)
        sea = SeaSurfaceExtension(altitude=0.0, version="sea-v1")

        result = resolve_altitude(position, sea_surface=sea)

        assert result.absolute_altitude == 10.0
        assert result.origin is AltitudeSourceKind.sea_surface
        assert result.is_sea_surface is True
        assert result.sea_surface_altitude == 0.0
        assert result.source.version == "sea-v1"

    def test_sea_surface_altitude_can_be_non_zero(self) -> None:
        position = Position(1.0, 2.0, 10.0, AltitudeMode.clamp_to_ground)
        sea = SeaSurfaceExtension(altitude=2.5)

        result = resolve_altitude(position, sea_surface=sea)

        assert result.absolute_altitude == 2.5

    def test_sea_surface_is_not_used_outside_its_footprint(self) -> None:
        position = Position(1.0, 2.0, 10.0, AltitudeMode.relative_to_ground)
        sea = SeaSurfaceExtension(altitude=0.0, footprint=(10, 10, 20, 20))

        result = resolve_altitude(position, sea_surface=sea)

        assert result.absolute_altitude is None
        assert "outside the sea surface footprint" in (result.reason or "")

    def test_land_coverage_blocks_sea_fallback(self) -> None:
        position = Position(1.0, 2.0, 10.0, AltitudeMode.relative_to_ground)
        terrain = TerrainCoverage(
            samples=[TerrainSample(1.5, 2.5, 100.0, coverage_version="v1")],
            version="v1",
            footprint=(0, 0, 3, 3),
        )
        sea = SeaSurfaceExtension(altitude=0.0)

        result = resolve_altitude(
            position,
            terrain=terrain,
            terrain_version="v1",
            sea_surface=sea,
        )

        assert result.absolute_altitude is None
        assert result.origin is AltitudeSourceKind.unavailable
        assert "inside the land coverage" in (result.reason or "")

    def test_sea_surface_is_never_reported_as_terrain(self) -> None:
        position = Position(1.0, 2.0, 10.0, AltitudeMode.relative_to_ground)
        sea = SeaSurfaceExtension(altitude=0.0)

        result = resolve_altitude(position, sea_surface=sea)

        assert result.terrain_sample is None
        assert result.terrain_version is None
        assert result.origin is not AltitudeSourceKind.terrain


class TestSeaFloor:
    """The gx sea-floor modes use their own coverage."""

    def test_relative_to_sea_floor_adds_sea_floor(self) -> None:
        position = Position(1.0, 2.0, 10.0, AltitudeMode.relative_to_sea_floor)
        sea_floor = TerrainCoverage(
            samples=[TerrainSample(1.0, 2.0, -50.0, coverage_version="floor-v1")],
            version="floor-v1",
        )

        result = resolve_altitude(
            position,
            sea_floor=sea_floor,
            sea_floor_version="floor-v1",
        )

        assert result.absolute_altitude == -40.0
        assert result.origin is AltitudeSourceKind.sea_floor

    def test_clamp_to_sea_floor_uses_sea_floor_altitude(self) -> None:
        position = Position(1.0, 2.0, 10.0, AltitudeMode.clamp_to_sea_floor)
        sea_floor = TerrainCoverage(
            samples=[TerrainSample(1.0, 2.0, -50.0, coverage_version="floor-v1")],
            version="floor-v1",
        )

        result = resolve_altitude(
            position,
            sea_floor=sea_floor,
            sea_floor_version="floor-v1",
        )

        assert result.absolute_altitude == -50.0

    def test_sea_floor_without_coverage_is_unresolved(self) -> None:
        position = Position(1.0, 2.0, 10.0, AltitudeMode.relative_to_sea_floor)

        result = resolve_altitude(position)

        assert result.absolute_altitude is None
        assert result.origin is AltitudeSourceKind.sea_floor


class TestProvenance:
    """Original values and conversion sources remain auditable."""

    def test_resolution_to_dict_keeps_everything(self) -> None:
        position = Position(
            13.4,
            52.5,
            10.0,
            AltitudeMode.relative_to_ground,
            source=PositionSource(
                feature_id="p1",
                feature_name="point",
                feature_path="Document/Placemark[p1]",
                geometry_type="Point",
                part="Point",
                coordinate_index=0,
            ),
        )
        terrain = TerrainCoverage(
            samples=[TerrainSample(13.4, 52.5, 100.0, coverage_version="v1")],
            version="v1",
        )
        result = resolve_altitude(position, terrain=terrain, terrain_version="v1")
        payload = result.to_dict()

        assert payload["original_altitude"] == 10.0
        assert payload["original_altitude_mode"] == "relativeToGround"
        assert payload["absolute_altitude"] == 110.0
        assert payload["terrain_altitude"] == 100.0
        assert payload["terrain_version"] == "v1"
        assert payload["source"]["kind"] == "terrain"
        assert payload["position"]["source"]["feature_id"] == "p1"

    def test_provenance_property_matches_to_dict(self) -> None:
        position = Position(1.0, 2.0, 10.0, AltitudeMode.absolute)
        result = resolve_altitude(position)

        assert result.provenance == result.to_dict()

    def test_unresolved_keeps_original_altitude(self) -> None:
        position = Position(1.0, 2.0, 10.0, AltitudeMode.relative_to_ground)
        result = resolve_altitude(position)

        assert result.original_altitude == 10.0
        assert result.original_altitude_mode is AltitudeMode.relative_to_ground
        assert result.absolute_altitude is None

    def test_position_to_dict_exposes_both_altitude_names(self) -> None:
        position = Position(1.0, 2.0, 10.0, AltitudeMode.clamp_to_ground)

        payload = position.to_dict()

        assert payload["altitude"] == 10.0
        assert payload["original_altitude"] == 10.0
        assert payload["original_altitude_mode"] == "clampToGround"
        assert payload["effective_altitude_mode"] == "clampToGround"


class TestTerrainInputs:
    """Terrain and sea-surface evidence accept several convenient forms."""

    def test_samples_as_three_tuples(self) -> None:
        position = Position(1.0, 2.0, 10.0, AltitudeMode.relative_to_ground)
        terrain = [(1.0, 2.0, 100.0)]

        result = resolve_altitude(position, terrain=terrain, terrain_version="v1")

        assert result.absolute_altitude == 110.0
        assert result.terrain_version == "v1"

    def test_samples_as_mapping(self) -> None:
        position = Position(1.0, 2.0, 10.0, AltitudeMode.relative_to_ground)

        result = resolve_altitude(position, terrain={(1.0, 2.0): 100.0})

        assert result.absolute_altitude == 110.0

    def test_sea_surface_as_number(self) -> None:
        position = Position(1.0, 2.0, 10.0, AltitudeMode.relative_to_ground)

        result = resolve_altitude(position, sea_surface=1.0)

        assert result.absolute_altitude == 11.0
        assert result.origin is AltitudeSourceKind.sea_surface

    def test_terrain_sample_rejects_missing_altitude(self) -> None:
        with pytest.raises(ValueError, match="must have an altitude"):
            TerrainSample(1.0, 2.0, None)  # type: ignore[arg-type]

    def test_negative_tolerance_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be negative"):
            TerrainCoverage(samples=[], tolerance=-1.0)

    def test_coverage_version_is_inherited_by_unversioned_samples(self) -> None:
        coverage = TerrainCoverage(
            samples=[TerrainSample(1.0, 2.0, 100.0)],
            version="v1",
        )

        assert coverage.samples[0].coverage_version == "v1"

    def test_coverage_without_footprint_contains_everything(self) -> None:
        coverage = TerrainCoverage(samples=[])

        assert coverage.contains(1.0, 2.0)

    def test_polygon_footprint_excludes_holes(self) -> None:
        coverage = TerrainCoverage(
            samples=[TerrainSample(0.1, 0.1, 100.0)],
            footprint=geo.Polygon(
                [(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)],
                [((0.2, 0.2), (0.3, 0.2), (0.3, 0.3), (0.2, 0.2))],
            ),
        )

        assert coverage.contains(0.1, 0.1) is True
        assert coverage.contains(0.25, 0.25) is False

    def test_polygon_footprint_includes_edges(self) -> None:
        coverage = TerrainCoverage(
            samples=[TerrainSample(0.5, 0.5, 100.0)],
            footprint=geo.Polygon(
                [(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)],
            ),
        )

        assert coverage.contains(0.0, 0.5) is True
        assert coverage.contains(1.0, 1.0) is True


class TestEvidenceInterfaces:
    """Additional evidence shapes stay explicit and conservative."""

    def test_single_sample_is_accepted(self) -> None:
        position = Position(1.0, 2.0, 10.0, AltitudeMode.relative_to_ground)

        result = resolve_altitude(position, terrain=TerrainSample(1.0, 2.0, 100.0))

        assert result.absolute_altitude == 110.0

    def test_lookup_callable_is_accepted(self) -> None:
        position = Position(1.0, 2.0, 10.0, AltitudeMode.relative_to_ground)

        result = resolve_altitude(position, terrain=lambda _lon, _lat: 100.0)

        assert result.absolute_altitude == 110.0

    def test_object_with_west_east_south_north_is_a_footprint(self) -> None:
        class Box:
            west = 0.0
            south = 0.0
            east = 3.0
            north = 3.0

        position = Position(1.0, 2.0, 10.0, AltitudeMode.relative_to_ground)
        terrain = TerrainCoverage(
            samples=[TerrainSample(2.5, 2.5, 100.0)],
            footprint=Box(),
        )

        result = resolve_altitude(position, terrain=terrain, sea_surface=0.0)

        assert result.absolute_altitude is None
        assert "inside the land coverage" in (result.reason or "")

    def test_sea_surface_descriptor_aliases(self) -> None:
        position = Position(1.0, 2.0, 10.0, AltitudeMode.relative_to_ground)

        result = resolve_altitude(
            position,
            sea_surface={"level": 2.0, "coverage_version": "sea-1"},
        )

        assert result.absolute_altitude == 12.0
        assert result.source.version == "sea-1"

    def test_catalog_level_evidence_is_used_by_resolve(self) -> None:
        catalog = PositionCatalog(
            [Position(1.0, 2.0, 10.0, AltitudeMode.relative_to_ground)],
            terrain=[(1.0, 2.0, 100.0)],
            terrain_version="v1",
        )

        results = catalog.resolve()

        assert results[0].absolute_altitude == 110.0

    def test_catalog_accepts_source_keyword(self) -> None:
        catalog = PositionCatalog(source={"type": "Point", "coordinates": (1, 2, 3)})

        assert len(catalog) == 1
        assert catalog[0].altitude == 3.0

    def test_absolute_altitudes_helper(self) -> None:
        catalog = PositionCatalog(
            [
                Position(1.0, 2.0, 10.0, AltitudeMode.absolute),
                Position(1.0, 2.0, 10.0, AltitudeMode.relative_to_ground),
            ],
        )

        assert catalog.absolute_altitudes(terrain=[(1.0, 2.0, 100.0)]) == [
            10.0,
            110.0,
        ]


class TestConstructors:
    """Constructors accept the spellings used by KML and common data sources."""

    def test_position_accepts_kml_mode_string(self) -> None:
        position = Position(1.0, 2.0, 10.0, "relativeToGround")  # type: ignore[arg-type]

        assert position.altitude_mode is AltitudeMode.relative_to_ground

    def test_coverage_from_samples_flattens_tuples(self) -> None:
        coverage = TerrainCoverage.from_samples(
            (1.0, 2.0, 100.0),
            (3.0, 4.0, 200.0),
            version="v1",
        )

        assert len(coverage.samples) == 2
        assert coverage.samples[1].coverage_version == "v1"

    def test_coverage_accepts_mapping_of_samples(self) -> None:
        coverage = TerrainCoverage({(1.0, 2.0): 100.0}, version="v1")

        assert coverage.samples[0].longitude == 1.0
        assert coverage.samples[0].altitude == 100.0

    def test_sea_surface_from_level(self) -> None:
        sea = SeaSurfaceExtension.from_level(0.0, version="sea-1")

        assert sea.level == 0.0
        assert sea.coverage_version == "sea-1"


class TestResolveBatch:
    """Resolve a whole catalogue consistently."""

    def test_resolve_all_uses_shared_evidence(self) -> None:
        catalog = PositionCatalog(
            [
                Position(1.0, 2.0, 10.0, AltitudeMode.relative_to_ground),
                Position(1.0, 2.0, 20.0, AltitudeMode.clamp_to_ground),
                Position(1.0, 2.0, 30.0, AltitudeMode.absolute),
            ],
        )
        terrain = TerrainCoverage(
            samples=[TerrainSample(1.0, 2.0, 100.0, coverage_version="v1")],
            version="v1",
        )

        results = catalog.resolve(terrain=terrain, terrain_version="v1")

        assert [result.absolute_altitude for result in results] == [110.0, 100.0, 30.0]

    def test_resolve_altitudes_accepts_kml_source(self) -> None:
        kml = KML.from_string(KML_DOCUMENT)
        terrain = TerrainCoverage(
            samples=[TerrainSample(13.4, 52.5, 100.0, coverage_version="v1")],
            version="v1",
        )

        results = resolve_altitudes(kml, terrain=terrain, terrain_version="v1")

        assert len(results) == 13
        assert results[0].absolute_altitude == 110.0

    def test_resolve_catalog_alias(self) -> None:
        catalog = PositionCatalog([Position(1.0, 2.0, 10.0, AltitudeMode.absolute)])

        results = resolve_catalog(catalog)

        assert isinstance(results[0], AltitudeResolution)
        assert results[0].absolute_altitude == 10.0

    def test_catalog_collect_returns_self(self) -> None:
        catalog = PositionCatalog()

        assert catalog.collect(Position(1.0, 2.0, 3.0)) is catalog
        assert len(catalog) == 1


class TestTrackRoundtrip:
    """Tracks built from Python objects are catalogued with their positions."""

    def test_track_items_with_angles(self) -> None:
        track = Track(
            ns="",
            track_items=[
                TrackItem(
                    when=KmlDateTime(
                        datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc),
                    ),
                    coord=geo.Point(1.0, 2.0, 30.0),
                ),
            ],
        )

        catalog = collect_positions(track)

        assert len(catalog) == 1
        assert catalog[0].altitude == 30.0
        assert catalog[0].source.geometry_type == "Track"

    def test_multitrack_explodes_tracks(self) -> None:
        track = Track(
            ns="",
            track_items=[
                TrackItem(
                    when=KmlDateTime(
                        datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc),
                    ),
                    coord=geo.Point(1.0, 2.0, 30.0),
                ),
            ],
        )
        multitrack = MultiTrack(tracks=[track], ns="")

        catalog = collect_positions(multitrack)

        assert len(catalog) == 1
        assert catalog[0].source.part == "MultiTrack[0].Track"


class TestFiftyOne:
    """Regression checks for the catalogued altitude contract."""

    def test_catalog_alias_matches_position_catalog(self) -> None:
        assert AltitudeCatalog is PositionCatalog

    def test_real_example_document_is_collected(self) -> None:
        example = Path("examples/KML_Samples.kml")
        if not example.exists():  # pragma: no cover - packaging layouts
            pytest.skip("example KML is not available")
        catalog = collect_positions(example.read_text(encoding="utf-8"))

        assert len(catalog) > 100
        assert {position.source.geometry_type for position in catalog} >= {
            "Point",
            "LineString",
            "Polygon",
        }
        assert all(
            isinstance(position.effective_altitude_mode, AltitudeMode)
            for position in catalog
        )
        assert any(
            position.original_altitude_mode is None for position in catalog
        )

    def test_resolution_is_unchanged_by_catalog_order(self) -> None:
        first = Position(1.0, 2.0, 10.0, AltitudeMode.relative_to_ground)
        second = Position(3.0, 4.0, 20.0, AltitudeMode.clamp_to_ground)
        terrain = TerrainCoverage(
            samples=[
                TerrainSample(1.0, 2.0, 100.0, coverage_version="v1"),
                TerrainSample(3.0, 4.0, 200.0, coverage_version="v1"),
            ],
            version="v1",
        )

        forward = PositionCatalog([first, second]).resolve(
            terrain=terrain,
            terrain_version="v1",
        )
        backward = PositionCatalog([second, first]).resolve(
            terrain=terrain,
            terrain_version="v1",
        )

        assert [result.absolute_altitude for result in forward] == [110.0, 200.0]
        assert [result.absolute_altitude for result in backward] == [200.0, 110.0]

    def test_lon_lat_aliases(self) -> None:
        position = Position(13.4, 52.5, 10.0)

        assert position.lon == 13.4
        assert position.lat == 52.5
