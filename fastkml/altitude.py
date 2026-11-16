"""
Terrain-aware altitude catalogues with explicit provenance.

KML stores altitude with a mode: ``absolute``, ``relativeToGround`` or
``clampToGround`` (``gx`` adds sea-floor variants).  The same number means
different things in each mode:

* ``absolute`` is already referenced to sea level.
* ``relativeToGround`` is an offset above the terrain.
* ``clampToGround`` ignores the coordinate altitude and follows the terrain.

Converting those values to a single absolute scale is only sound when the
terrain under the position is known.  This module keeps the original value and
mode on every catalogued position, and records where a converted value came
from.  It deliberately does not invent a terrain altitude: when no suitable
sample exists the absolute altitude stays :data:`None`, never ``0.0``.

The sea surface is modelled separately from land terrain through
:class:`SeaSurfaceExtension`, so a sea-level result is always identifiable as
such and is never mistaken for a missing terrain sample.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Iterable
from collections.abc import Iterator
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from typing import Final

from fastkml.containers import Document
from fastkml.containers import Folder
from fastkml.enums import AltitudeMode
from fastkml.features import Placemark
from fastkml.geometry import LineString
from fastkml.geometry import MultiGeometry
from fastkml.geometry import Point
from fastkml.geometry import Polygon
from fastkml.gx.track import MultiTrack
from fastkml.gx.track import Track
from fastkml.kml import KML
from fastkml.model import Model
from fastkml.overlays import GroundOverlay
from fastkml.overlays import PhotoOverlay

__all__ = [
    "AltitudeCatalog",
    "AltitudeEntry",
    "AltitudeOrigin",
    "AltitudeResolution",
    "AltitudeSource",
    "AltitudeSourceKind",
    "CatalogEntry",
    "Position",
    "PositionCatalog",
    "PositionSource",
    "SeaFloorCoverage",
    "SeaSurface",
    "SeaSurfaceExtension",
    "TerrainCoverage",
    "TerrainModel",
    "TerrainSample",
    "TerrainSamples",
    "absolute_altitude",
    "build_catalog",
    "catalog_kml",
    "collect_altitudes",
    "collect_positions",
    "resolve_altitude",
    "resolve_altitudes",
    "resolve_catalog",
    "resolve_position",
]

_EARTH_RADIUS_M: Final = 6_371_008.8
_BOUNDS_SIZE: Final = 4
_MIN_RING_POINTS: Final = 3
_MIN_COORDINATES: Final = 2
_SEA_FLOOR_MODES: Final = frozenset(
    {
        AltitudeMode.clamp_to_sea_floor,
        AltitudeMode.relative_to_sea_floor,
    },
)
_FOOTPRINT_TOLERANCE: Final = 1e-12
_INTERFACE_TYPES: Final = frozenset(
    {
        "Point",
        "LineString",
        "LinearRing",
        "Polygon",
        "MultiPoint",
        "MultiLineString",
        "MultiPolygon",
        "GeometryCollection",
    },
)


class AltitudeSourceKind(str, Enum):
    """Identify which evidence produced an absolute altitude."""

    absolute = "absolute"
    terrain = "terrain"
    sea_surface = "sea_surface"
    sea_floor = "sea_floor"
    unavailable = "unavailable"


@dataclass(frozen=True)
class PositionSource:
    """Describe where a position was found in the KML feature tree."""

    feature_id: str | None = None
    feature_name: str | None = None
    feature_path: str = ""
    geometry_type: str = ""
    part: str | None = None
    coordinate_index: int | None = None
    timestamp: str | None = None

    def __repr__(self) -> str:
        """Return a representation that exposes the provenance fields."""
        return (
            f"{self.__class__.__module__}.{self.__class__.__name__}("
            f"feature_id={self.feature_id!r}, "
            f"feature_name={self.feature_name!r}, "
            f"feature_path={self.feature_path!r}, "
            f"geometry_type={self.geometry_type!r}, "
            f"part={self.part!r}, "
            f"coordinate_index={self.coordinate_index!r}, "
            f"timestamp={self.timestamp!r})"
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the provenance as plain, serialisable values."""
        return {
            "feature_id": self.feature_id,
            "feature_name": self.feature_name,
            "feature_path": self.feature_path,
            "geometry_type": self.geometry_type,
            "part": self.part,
            "coordinate_index": self.coordinate_index,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class Position:
    """A single KML coordinate together with its original altitude semantics."""

    longitude: float
    latitude: float
    altitude: float | None = None
    altitude_mode: AltitudeMode | None = None
    source: PositionSource = PositionSource()

    def __post_init__(self) -> None:
        """Accept the KML string spelling for an altitude mode."""
        if isinstance(self.altitude_mode, str):
            object.__setattr__(self, "altitude_mode", AltitudeMode(self.altitude_mode))

    @property
    def original_altitude(self) -> float | None:
        """Return the altitude exactly as it appeared in the KML coordinate."""
        return self.altitude

    @property
    def original_altitude_mode(self) -> AltitudeMode | None:
        """Return the altitude mode exactly as it appeared in the KML object."""
        return self.altitude_mode

    @property
    def effective_altitude_mode(self) -> AltitudeMode:
        """Return the KML default mode when no explicit mode was present."""
        return self.altitude_mode or AltitudeMode.clamp_to_ground

    @property
    def lon(self) -> float:
        """Return the longitude."""
        return self.longitude

    @property
    def lat(self) -> float:
        """Return the latitude."""
        return self.latitude

    def to_dict(self) -> dict[str, Any]:
        """Return the position and its provenance as serialisable values."""
        return {
            "longitude": self.longitude,
            "latitude": self.latitude,
            "altitude": self.altitude,
            "original_altitude": self.original_altitude,
            "altitude_mode": _mode_value(self.altitude_mode),
            "original_altitude_mode": _mode_value(self.original_altitude_mode),
            "effective_altitude_mode": self.effective_altitude_mode.value,
            "source": self.source.to_dict(),
        }


@dataclass(frozen=True)
class TerrainSample:
    """A terrain height sample and the coverage version it belongs to."""

    longitude: float
    latitude: float
    altitude: float
    coverage_version: str | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        """Validate that a terrain sample carries an actual altitude."""
        if self.altitude is None:
            msg = "A terrain sample must have an altitude; missing terrain is not 0.0"
            raise ValueError(msg)

    @property
    def lon(self) -> float:
        """Return the sample longitude."""
        return self.longitude

    @property
    def lat(self) -> float:
        """Return the sample latitude."""
        return self.latitude

    @property
    def height(self) -> float:
        """Return the sample altitude."""
        return self.altitude

    @property
    def version(self) -> str | None:
        """Return the sample coverage version."""
        return self.coverage_version

    def to_dict(self) -> dict[str, Any]:
        """Return the sample as serialisable values."""
        return {
            "longitude": self.longitude,
            "latitude": self.latitude,
            "altitude": self.altitude,
            "coverage_version": self.coverage_version,
            "source": self.source,
        }


@dataclass(frozen=True)
class TerrainCoverage:
    """Terrain samples plus the coverage footprint and version they belong to."""

    samples: tuple[TerrainSample, ...] | Iterable[TerrainSample]
    version: str | None = None
    footprint: Any = None
    tolerance: float = 0.0
    name: str | None = None
    query: Any = None

    def __post_init__(self) -> None:
        """Normalise samples and reject malformed coverage definitions."""
        raw = self.samples
        if isinstance(raw, TerrainSample):
            samples: tuple[Any, ...] = (raw,)
        elif isinstance(raw, Mapping):
            samples = tuple(
                _sample_from_mapping(position, altitude, self.version)
                for position, altitude in raw.items()
            )
        else:
            samples = tuple(raw)
        normalised = []
        for sample in samples:
            try:
                coerced = _coerce_sample(sample, None)
            except (TypeError, ValueError) as error:
                msg = (
                    "Expected TerrainSample instances or "
                    f"(longitude, latitude, altitude) sequences, got {sample!r}"
                )
                raise TypeError(msg) from error
            normalised.append(_inherit_version(coerced, self.version))
        if self.tolerance < 0:
            msg = "tolerance must not be negative"
            raise ValueError(msg)
        object.__setattr__(self, "samples", tuple(normalised))

    @property
    def coverage_version(self) -> str | None:
        """Return the coverage version."""
        return self.version

    @classmethod
    def from_samples(
        cls,
        *samples: Any,
        version: str | None = None,
        footprint: Any = None,
        tolerance: float = 0.0,
        name: str | None = None,
        query: Any = None,
    ) -> TerrainCoverage:
        """Build a coverage from individual samples or sample sequences."""
        flat: list[Any] = []
        for sample in samples:
            if isinstance(sample, (TerrainSample, Mapping)) or (
                isinstance(sample, (list, tuple))
                and len(sample) == _MIN_RING_POINTS
                and all(isinstance(value, (int, float)) for value in sample)
            ):
                flat.append(sample)
            else:
                flat.extend(sample)
        return cls(
            samples=flat,
            version=version,
            footprint=footprint,
            tolerance=tolerance,
            name=name,
            query=query,
        )

    def contains(self, longitude: float, latitude: float) -> bool:
        """Return whether a position lies inside the coverage footprint."""
        return _contains_position(self.footprint, longitude, latitude)

    def nearest_sample(
        self,
        longitude: float,
        latitude: float,
        tolerance: float | None = None,
    ) -> TerrainSample | None:
        """Return the nearest sample regardless of its coverage version."""
        if not self.contains(longitude, latitude):
            return None
        if self.query is not None:
            return _sample_from_query(
                self.query,
                longitude,
                latitude,
                self.version,
            )
        limit = self.tolerance if tolerance is None else tolerance
        exact = [
            sample
            for sample in self.samples
            if sample.longitude == longitude and sample.latitude == latitude
        ]
        if exact:
            for sample in exact:
                if _version_matches(self.version, sample.coverage_version):
                    return sample
            return exact[0]
        if limit == 0:
            return None
        best: TerrainSample | None = None
        best_distance = math.inf
        for sample in self.samples:
            distance = _distance_m(
                longitude,
                latitude,
                sample.longitude,
                sample.latitude,
            )
            if distance <= limit and distance < best_distance:
                best = sample
                best_distance = distance
        return best

    def sample_at(
        self,
        longitude: float,
        latitude: float,
        tolerance: float | None = None,
    ) -> TerrainSample | None:
        """Return the nearest sample of the declared version, if any."""
        if not self.contains(longitude, latitude):
            return None
        sample = self.nearest_sample(longitude, latitude, tolerance)
        if sample is None:
            return None
        if not _version_matches(self.version, sample.coverage_version):
            return None
        return sample

    def with_version(self, version: str | None) -> TerrainCoverage:
        """Return a copy that uses ``version`` as its coverage version."""
        return TerrainCoverage(
            samples=self.samples,
            version=version,
            footprint=self.footprint,
            tolerance=self.tolerance,
            name=self.name,
            query=self.query,
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the coverage as serialisable values."""
        return {
            "version": self.version,
            "name": self.name,
            "tolerance": self.tolerance,
            "footprint": self.footprint,
            "samples": [sample.to_dict() for sample in self.samples],
            "query": self.query is not None,
        }


@dataclass(frozen=True)
class SeaSurfaceExtension:
    """
    A sea-surface height and the area over which it is valid.

    This is intentionally separate from :class:`TerrainCoverage`: a sea-level
    result is evidence in its own right, not a substitute for missing terrain.
    """

    altitude: float = 0.0
    footprint: Any = None
    version: str | None = None
    name: str | None = None

    def __post_init__(self) -> None:
        """Reject a missing sea-surface height."""
        if self.altitude is None:
            msg = "A sea surface extension must have an altitude"
            raise ValueError(msg)

    @classmethod
    def from_level(
        cls,
        level: float,
        *,
        footprint: Any = None,
        version: str | None = None,
        name: str | None = None,
    ) -> SeaSurfaceExtension:
        """Build a sea-surface extension from a level and optional footprint."""
        return cls(
            altitude=float(level),
            footprint=footprint,
            version=version,
            name=name,
        )

    @property
    def level(self) -> float:
        """Return the sea-surface height."""
        return self.altitude

    @property
    def coverage_version(self) -> str | None:
        """Return the sea-surface coverage version."""
        return self.version

    def contains(self, longitude: float, latitude: float) -> bool:
        """Return whether a position lies in the sea-surface footprint."""
        return _contains_position(self.footprint, longitude, latitude)

    def altitude_at(self, longitude: float, latitude: float) -> float | None:
        """Return the sea-surface height at a position, or ``None`` if outside."""
        return self.altitude if self.contains(longitude, latitude) else None

    def to_dict(self) -> dict[str, Any]:
        """Return the sea-surface evidence as serialisable values."""
        return {
            "altitude": self.altitude,
            "version": self.version,
            "name": self.name,
            "footprint": self.footprint,
        }


@dataclass(frozen=True)
class AltitudeSource:
    """The evidence that produced an absolute altitude."""

    kind: AltitudeSourceKind
    description: str
    coverage_version: str | None = None
    sample: TerrainSample | None = None
    sea_surface: bool = False
    name: str | None = None

    @property
    def origin(self) -> AltitudeSourceKind:
        """Return the source kind."""
        return self.kind

    @property
    def version(self) -> str | None:
        """Return the coverage version of the evidence."""
        return self.coverage_version

    def to_dict(self) -> dict[str, Any]:
        """Return the source as serialisable values."""
        return {
            "kind": self.kind.value,
            "description": self.description,
            "coverage_version": self.coverage_version,
            "sample": self.sample.to_dict() if self.sample is not None else None,
            "sea_surface": self.sea_surface,
            "name": self.name,
        }


@dataclass(frozen=True)
class AltitudeResolution:
    """An absolute-altitude attempt that retains its original input and source."""

    position: Position
    absolute_altitude: float | None
    source: AltitudeSource
    reason: str | None = None
    terrain_sample: TerrainSample | None = None
    terrain_version: str | None = None
    sea_surface_altitude: float | None = None

    @property
    def resolved(self) -> bool:
        """Return whether an absolute altitude could be established."""
        return self.absolute_altitude is not None

    @property
    def evidence_sufficient(self) -> bool:
        """Return whether the available evidence sufficed for a conversion."""
        return self.resolved

    @property
    def absolute(self) -> float | None:
        """Return the absolute altitude, or ``None`` when evidence is missing."""
        return self.absolute_altitude

    @property
    def original_altitude(self) -> float | None:
        """Return the original KML altitude."""
        return self.position.original_altitude

    @property
    def original_altitude_mode(self) -> AltitudeMode | None:
        """Return the original KML altitude mode."""
        return self.position.original_altitude_mode

    @property
    def effective_altitude_mode(self) -> AltitudeMode:
        """Return the mode used for interpretation, including KML defaults."""
        return self.position.effective_altitude_mode

    @property
    def origin(self) -> AltitudeSourceKind:
        """Return the source kind."""
        return self.source.kind

    @property
    def source_kind(self) -> AltitudeSourceKind:
        """Return the source kind."""
        return self.source.kind

    @property
    def conversion_source(self) -> str:
        """Return the source kind as a plain string."""
        return self.source.kind.value

    @property
    def terrain_altitude(self) -> float | None:
        """Return the terrain sample altitude used, if any."""
        return self.terrain_sample.altitude if self.terrain_sample is not None else None

    @property
    def is_sea_surface(self) -> bool:
        """Return whether this result came from the sea-surface extension."""
        return self.source.kind is AltitudeSourceKind.sea_surface

    @property
    def provenance(self) -> dict[str, Any]:
        """Return the original altitude and conversion source for auditing."""
        return self.to_dict()

    def to_dict(self) -> dict[str, Any]:
        """Return the resolution as serialisable values for audit logs."""
        terrain_altitude = (
            self.terrain_sample.altitude if self.terrain_sample is not None else None
        )
        return {
            "longitude": self.position.longitude,
            "latitude": self.position.latitude,
            "original_altitude": self.original_altitude,
            "original_altitude_mode": _mode_value(self.original_altitude_mode),
            "effective_altitude_mode": self.effective_altitude_mode.value,
            "absolute_altitude": self.absolute_altitude,
            "resolved": self.resolved,
            "terrain_altitude": terrain_altitude,
            "terrain_version": self.terrain_version,
            "sea_surface_altitude": self.sea_surface_altitude,
            "reason": self.reason,
            "source": self.source.to_dict(),
            "position": self.position.to_dict(),
        }


class PositionCatalog:
    """An ordered catalogue of geometry positions collected from KML."""

    def __init__(
        self,
        positions: Iterable[Position] | None = None,
        *,
        terrain: Any = None,
        terrain_version: str | None = None,
        sea_surface: Any = None,
        sea_floor: Any = None,
        sea_floor_version: str | None = None,
        tolerance: float | None = None,
        source: Any = None,
    ) -> None:
        """
        Create a catalogue, optionally pre-populated with positions.

        ``source`` is a convenience alternative to ``positions`` and accepts
        any KML object or geometry that :func:`collect_positions` understands.
        """
        self._positions: list[Position] = []
        self.resolve_kwargs: dict[str, Any] = {
            key: value
            for key, value in (
                ("terrain", terrain),
                ("terrain_version", terrain_version),
                ("sea_surface", sea_surface),
                ("sea_floor", sea_floor),
                ("sea_floor_version", sea_floor_version),
                ("tolerance", tolerance),
            )
            if value is not None
        }
        if positions is not None:
            self.extend(positions)
        if source is not None:
            self.extend(collect_positions(source))

    def __repr__(self) -> str:
        """Return a representation that shows the catalogue size."""
        return (
            f"{self.__class__.__module__}.{self.__class__.__name__}("
            f"positions={self._positions!r})"
        )

    def __bool__(self) -> bool:
        """Return whether the catalogue contains any positions."""
        return bool(self._positions)

    def __iter__(self) -> Iterator[Position]:
        """Iterate over positions in collection order."""
        return iter(self._positions)

    def __len__(self) -> int:
        """Return the number of catalogued positions."""
        return len(self._positions)

    def __getitem__(self, index: int) -> Position:
        """Return a position by index."""
        return self._positions[index]

    @property
    def positions(self) -> tuple[Position, ...]:
        """Return all positions as an immutable sequence."""
        return tuple(self._positions)

    @property
    def entries(self) -> tuple[Position, ...]:
        """Return all positions as an immutable sequence."""
        return self.positions

    @classmethod
    def from_source(cls, source: Any, **kwargs: Any) -> PositionCatalog:
        """
        Build a catalogue from KML or positions, optionally resolving them.

        Any keyword arguments other than ``positions`` are forwarded to
        :func:`resolve_altitude`, so ``PositionCatalog.from_source(kml,
        terrain=..., terrain_version=...)`` returns resolved entries.
        """
        catalog = collect_positions(source)
        if kwargs:
            catalog.resolve_kwargs = kwargs
        return catalog

    @classmethod
    def from_kml(cls, source: Any) -> PositionCatalog:
        """Build a catalogue from a KML document, feature, geometry or iterable."""
        return collect_positions(source)

    def resolve(self, **kwargs: Any) -> list[AltitudeResolution]:
        """Resolve every position with the same terrain and sea-surface evidence."""
        return self.resolve_all(**kwargs)

    def collect(self, source: Any) -> PositionCatalog:
        """Append positions collected from ``source`` and return this catalogue."""
        self.extend(collect_positions(source))
        return self

    def add(self, position: Position) -> None:
        """Append one position to the catalogue."""
        if not isinstance(position, Position):
            msg = f"Expected a Position, got {type(position)!r}"
            raise TypeError(msg)
        self._positions.append(position)

    def append(self, position: Position) -> None:
        """Append one position to the catalogue."""
        self.add(position)

    def extend(self, positions: Iterable[Position]) -> None:
        """Append several positions to the catalogue."""
        for position in positions:
            self.add(position)

    def resolve_all(self, **kwargs: Any) -> list[AltitudeResolution]:
        """Resolve every position with the same terrain and sea-surface evidence."""
        options = {**self.resolve_kwargs, **kwargs}
        return [resolve_altitude(position, **options) for position in self._positions]

    def absolute_altitudes(self, **kwargs: Any) -> list[float | None]:
        """Resolve every position and return only the absolute values."""
        return [
            result.absolute_altitude for result in self.resolve_all(**kwargs)
        ]

    def to_dict(self) -> dict[str, Any]:
        """Return the catalogue as serialisable values."""
        return {"positions": [position.to_dict() for position in self._positions]}


def _inherit_version(
    sample: TerrainSample,
    version: str | None,
) -> TerrainSample:
    """Give an unversioned sample the version declared by its coverage."""
    if version is None or sample.coverage_version is not None:
        return sample
    return TerrainSample(
        longitude=sample.longitude,
        latitude=sample.latitude,
        altitude=sample.altitude,
        coverage_version=version,
        source=sample.source,
    )


def _mode_value(mode: AltitudeMode | None) -> str | None:
    """Return an enum value suitable for serialisation."""
    return mode.value if mode is not None else None


def _contains_position(  # noqa: PLR0911
    footprint: Any,
    longitude: float,
    latitude: float,
) -> bool:
    """Return whether a position lies in a bbox, geometry or collection."""
    if footprint is None:
        return True
    if _is_bounds(footprint):
        return _contains_bounds(footprint, longitude, latitude)
    if isinstance(footprint, Mapping):
        return _contains_interface(footprint, longitude, latitude)
    if isinstance(footprint, (list, tuple)):
        return any(
            _contains_position(element, longitude, latitude) for element in footprint
        )
    interface = getattr(footprint, "__geo_interface__", None)
    if interface is not None:
        return _contains_position(interface, longitude, latitude)
    bounds = _bounds_from_object(footprint)
    if bounds is not None:
        return _contains_bounds(bounds, longitude, latitude)
    return False


def _bounds_from_object(footprint: Any) -> tuple[float, float, float, float] | None:
    """Read a bounding box from an object with west/east/south/north attributes."""
    names = ("west", "south", "east", "north")
    if not all(hasattr(footprint, name) for name in names):
        return None
    values = [getattr(footprint, name) for name in names]
    if any(not isinstance(value, (int, float)) for value in values):
        return None
    return (values[0], values[1], values[2], values[3])


def _contains_interface(
    interface: Mapping[str, Any],
    longitude: float,
    latitude: float,
) -> bool:
    """Return whether a point is inside a GeoJSON-style mapping."""
    kind = interface.get("type")
    if kind == "Feature":
        return _contains_position(interface.get("geometry"), longitude, latitude)
    if kind in ("FeatureCollection", "GeometryCollection"):
        key = "features" if kind == "FeatureCollection" else "geometries"
        return any(
            _contains_position(geometry, longitude, latitude)
            for geometry in interface.get(key, ())
        )
    if kind == "Polygon":
        return _contains_polygon(interface.get("coordinates", ()), longitude, latitude)
    if kind == "MultiPolygon":
        return any(
            _contains_polygon(polygon, longitude, latitude)
            for polygon in interface.get("coordinates", ())
        )
    if kind == "Point":
        coordinates = interface.get("coordinates", ())
        return len(coordinates) >= _MIN_COORDINATES and _same_point(
            coordinates[0],
            coordinates[1],
            longitude,
            latitude,
        )
    return False


def _is_bounds(value: Any) -> bool:
    """Return whether a value is a west/south/east/north bounding box."""
    return (
        isinstance(value, (list, tuple))
        and len(value) == _BOUNDS_SIZE
        and all(isinstance(number, (int, float)) for number in value)
    )


def _contains_bounds(
    bounds: tuple[float, float, float, float],
    longitude: float,
    latitude: float,
) -> bool:
    """Return whether a point is inside a bounding box, including its edge."""
    west, south, east, north = bounds
    if not south <= latitude <= north:
        return False
    if west <= east:
        return west <= longitude <= east
    return longitude >= west or longitude <= east


def _contains_polygon(
    coordinates: Any,
    longitude: float,
    latitude: float,
) -> bool:
    """Return whether a point is in a Polygon coordinate structure, holes included."""
    if not coordinates:
        return False
    exterior = coordinates[0]
    if not _contains_ring(exterior, longitude, latitude):
        return False
    return not any(
        _contains_ring(interior, longitude, latitude) for interior in coordinates[1:]
    )


def _contains_ring(
    ring: Any,
    longitude: float,
    latitude: float,
) -> bool:
    """Return whether a point is inside or on a linear ring."""
    points = [tuple(point) for point in ring]
    if len(points) < _MIN_RING_POINTS:
        return False
    if points[0] != points[-1]:
        points.append(points[0])
    inside = False
    for first, second in itertools.pairwise(points):
        x1, y1 = first[0], first[1]
        x2, y2 = second[0], second[1]
        if _point_on_segment(longitude, latitude, x1, y1, x2, y2):
            return True
        if (y1 > latitude) != (y2 > latitude):
            intersection = (x2 - x1) * (latitude - y1) / (y2 - y1) + x1
            if intersection > longitude:
                inside = not inside
    return inside


def _point_on_segment(
    longitude: float,
    latitude: float,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
) -> bool:
    """Return whether a point lies on a segment, with a small tolerance."""
    cross = (longitude - x1) * (y2 - y1) - (latitude - y1) * (x2 - x1)
    if abs(cross) > _FOOTPRINT_TOLERANCE:
        return False
    return (
        min(x1, x2) - _FOOTPRINT_TOLERANCE
        <= longitude
        <= max(x1, x2) + _FOOTPRINT_TOLERANCE
        and min(y1, y2) - _FOOTPRINT_TOLERANCE
        <= latitude
        <= max(y1, y2) + _FOOTPRINT_TOLERANCE
    )


def _same_point(
    first: float,
    second: float,
    longitude: float,
    latitude: float,
) -> bool:
    """Return whether two positions are exactly equal within tolerance."""
    return (
        abs(first - longitude) <= _FOOTPRINT_TOLERANCE
        and abs(second - latitude) <= _FOOTPRINT_TOLERANCE
    )


def _distance_m(
    first_lon: float,
    first_lat: float,
    second_lon: float,
    second_lat: float,
) -> float:
    """Return the great-circle distance between two positions in metres."""
    first_lat_rad = math.radians(first_lat)
    second_lat_rad = math.radians(second_lat)
    delta_lat = second_lat_rad - first_lat_rad
    delta_lon = math.radians(second_lon - first_lon)
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(first_lat_rad) * math.cos(second_lat_rad)
        * math.sin(delta_lon / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(haversine)))


def _version_matches(
    coverage_version: str | None,
    sample_version: str | None,
) -> bool:
    """
    Return whether a sample belongs to the requested coverage version.

    A versioned coverage only accepts samples explicitly tagged with the same
    version.  An unversioned sample is not enough evidence for a versioned
    coverage, so it is skipped instead of being treated as a match.
    """
    if coverage_version is None:
        return True
    return sample_version == coverage_version


def _sample_from_query(
    query: Any,
    longitude: float,
    latitude: float,
    version: str | None,
) -> TerrainSample | None:
    """Call a user-supplied terrain lookup and normalise it to a sample."""
    result = query(longitude, latitude)
    if result is None:
        return None
    if isinstance(result, (int, float)):
        # A lookup that returns just a height refers to the queried position.
        return TerrainSample(
            longitude=longitude,
            latitude=latitude,
            altitude=float(result),
            coverage_version=version,
        )
    if isinstance(result, TerrainSample):
        return _inherit_version(result, version)
    return _coerce_sample(result, version)


def _is_coverage_descriptor(value: Mapping[str, Any]) -> bool:
    """Return whether a mapping describes a coverage rather than sample points."""
    return any(
        key in value
        for key in ("samples", "footprint", "query", "version", "tolerance", "name")
    )


def _coerce_terrain(  # noqa: PLR0911
    value: Any,
    *,
    version: str | None = None,
    tolerance: float | None = None,
) -> TerrainCoverage | None:
    """Turn samples, a mapping or a coverage object into a TerrainCoverage."""
    if value is None:
        return None
    if callable(value) and not isinstance(value, TerrainCoverage):
        return TerrainCoverage(samples=(), version=version, query=value)
    if isinstance(value, TerrainSample):
        return TerrainCoverage(samples=[value], version=version)
    if isinstance(value, Mapping) and _is_coverage_descriptor(value):
        descriptor_version = value.get("version", version)
        descriptor_tolerance = value.get("tolerance")
        return TerrainCoverage(
            samples=value.get("samples", ()),
            version=descriptor_version,
            footprint=value.get("footprint"),
            tolerance=(
                tolerance
                if tolerance is not None
                else descriptor_tolerance
                if descriptor_tolerance is not None
                else 0.0
            ),
            name=value.get("name"),
            query=value.get("query"),
        )
    if isinstance(value, TerrainCoverage):
        # A coverage object is a self-describing evidence set: keep its declared
        # version untouched.  Only a raw list/mapping of samples adopts the
        # caller-supplied version, because those samples have no identity of
        # their own.  Conflicting versions are handled by the resolver so the
        # reason can name both versions.
        coverage = value
        if tolerance is not None:
            coverage = TerrainCoverage(
                samples=coverage.samples,
                version=coverage.version,
                footprint=coverage.footprint,
                tolerance=tolerance,
                name=coverage.name,
                query=coverage.query,
            )
        return coverage
    if isinstance(value, Mapping):
        samples = [
            _sample_from_mapping(position, altitude, version)
            for position, altitude in value.items()
        ]
        return TerrainCoverage(samples=samples, version=version)
    samples = []
    for item in value:
        samples.append(_coerce_sample(item, version))
    return TerrainCoverage(samples=samples, version=version)


def _sample_from_mapping(
    position: Any,
    altitude: Any,
    version: str | None,
) -> TerrainSample:
    """Build a sample from a mapping key/value pair."""
    if isinstance(altitude, TerrainSample):
        return _inherit_version(altitude, version)
    return TerrainSample(
        longitude=float(position[0]),
        latitude=float(position[1]),
        altitude=float(altitude),
        coverage_version=version,
    )


def _coerce_sample(item: Any, version: str | None) -> TerrainSample:
    """Build a sample from a sample, mapping, or coordinate/altitude sequence."""
    if isinstance(item, TerrainSample):
        return _inherit_version(item, version)
    if isinstance(item, Mapping):
        return TerrainSample(
            longitude=float(_pick(item, "longitude", "lon", "x")),
            latitude=float(_pick(item, "latitude", "lat", "y")),
            altitude=float(_pick(item, "altitude", "height", "elevation", "z")),
            coverage_version=item.get("coverage_version", item.get("version", version)),
            source=item.get("source"),
        )
    if len(item) < _MIN_RING_POINTS:
        msg = "A terrain sample needs longitude, latitude and altitude"
        raise ValueError(msg)
    return TerrainSample(
        longitude=float(item[0]),
        latitude=float(item[1]),
        altitude=float(item[2]),
        coverage_version=version,
    )


def _pick(mapping: Mapping[str, Any], *names: str) -> Any:
    """Return the first present key from a mapping."""
    for name in names:
        if name in mapping:
            return mapping[name]
    msg = f"None of the keys {names!r} are present in {mapping!r}"
    raise KeyError(msg)




def _coerce_sea_surface(value: Any) -> SeaSurfaceExtension | None:
    """Turn a sea-surface value into a SeaSurfaceExtension."""
    if value is None:
        return None
    if isinstance(value, SeaSurfaceExtension):
        return value
    if isinstance(value, (int, float)):
        return SeaSurfaceExtension(altitude=float(value))
    if isinstance(value, Mapping):
        altitude = value.get("altitude", value.get("level", value.get("height", 0.0)))
        return SeaSurfaceExtension(
            altitude=float(altitude),
            footprint=value.get("footprint", value.get("covered_area")),
            version=value.get("version", value.get("coverage_version")),
            name=value.get("name"),
        )
    interface = getattr(value, "__geo_interface__", None)
    if interface is not None:
        return SeaSurfaceExtension(footprint=interface)
    msg = f"Cannot interpret sea surface evidence from {type(value)!r}"
    raise TypeError(msg)


def _resolve_absolute(position: Position) -> AltitudeResolution:
    """Resolve an absolute-mode position without using terrain."""
    if position.altitude is None:
        return _unresolved(
            position,
            "absolute altitude is missing from the KML coordinate",
            kind=AltitudeSourceKind.absolute,
        )
    return AltitudeResolution(
        position=position,
        absolute_altitude=position.altitude,
        source=AltitudeSource(
            kind=AltitudeSourceKind.absolute,
            description="KML absolute altitude; used without terrain conversion",
        ),
    )


def _combine_ground_altitude(
    position: Position,
    ground_altitude: float,
) -> float | None:
    """Combine a ground height with the position's mode and original altitude."""
    if position.effective_altitude_mode is AltitudeMode.clamp_to_ground:
        return ground_altitude
    if position.altitude is None:
        return None
    return ground_altitude + position.altitude


def _resolve_with_evidence(
    position: Position,
    evidence: AltitudeSource,
    ground_altitude: float,
    *,
    terrain_sample: TerrainSample | None = None,
    terrain_version: str | None = None,
    sea_surface_altitude: float | None = None,
    clamp: bool | None = None,
) -> AltitudeResolution:
    """Resolve a ground-relative or clamped position from known evidence."""
    absolute = (
        ground_altitude
        if clamp
        else _combine_ground_altitude(position, ground_altitude)
    )
    if absolute is None:
        return _unresolved(
            position,
            "relativeToGround requires an original altitude",
            kind=evidence.kind,
            terrain_sample=terrain_sample,
            terrain_version=terrain_version,
            sea_surface_altitude=sea_surface_altitude,
        )
    return AltitudeResolution(
        position=position,
        absolute_altitude=absolute,
        source=evidence,
        terrain_sample=terrain_sample,
        terrain_version=terrain_version,
        sea_surface_altitude=sea_surface_altitude,
    )


def _resolve_sea_floor(
    position: Position,
    *,
    sea_floor: Any,
    sea_floor_version: str | None,
    tolerance: float | None,
) -> AltitudeResolution:
    """Resolve a gx sea-floor mode from explicit sea-floor coverage."""
    coverage = _coerce_terrain(
        sea_floor,
        version=sea_floor_version,
        tolerance=tolerance,
    )
    if coverage is None:
        return _unresolved(
            position,
            "no sea floor coverage was provided",
            kind=AltitudeSourceKind.sea_floor,
        )
    if (
        sea_floor_version is not None
        and coverage.version is not None
        and coverage.version != sea_floor_version
    ):
        return _unresolved(
            position,
            "sea floor coverage version "
            f"{coverage.version!r} does not match the required version "
            f"{sea_floor_version!r}; missing sea floor is never replaced with 0.0",
            kind=AltitudeSourceKind.sea_floor,
            terrain_version=coverage.version,
        )
    sample = coverage.sample_at(position.longitude, position.latitude, tolerance)
    if sample is None:
        reason = (
            "no sea floor sample at the position for coverage version "
            f"{coverage.version!r}"
        )
        return _unresolved(
            position,
            reason,
            kind=AltitudeSourceKind.sea_floor,
            terrain_version=coverage.version,
        )
    evidence = AltitudeSource(
        kind=AltitudeSourceKind.sea_floor,
        description=(
            "sea floor sample altitude "
            f"{sample.altitude!r} for coverage version {coverage.version!r}"
        ),
        coverage_version=coverage.version,
        sample=sample,
        name=coverage.name,
    )
    return _resolve_with_evidence(
        position,
        evidence,
        sample.altitude,
        terrain_sample=sample,
        terrain_version=coverage.version,
        clamp=position.effective_altitude_mode is AltitudeMode.clamp_to_sea_floor,
    )


def _resolve_ground_mode(
    position: Position,
    *,
    terrain: Any,
    terrain_version: str | None,
    sea_surface: Any,
    tolerance: float | None,
) -> AltitudeResolution:
    """Resolve relativeToGround and clampToGround from terrain, then sea surface."""
    raw_coverage = terrain
    coverage = _coerce_terrain(
        terrain,
        version=terrain_version,
        tolerance=tolerance,
    )
    if coverage is not None:
        original_version = (
            raw_coverage.version
            if isinstance(raw_coverage, TerrainCoverage)
            else None
        )
        version_conflict = (
            terrain_version is not None
            and (
                coverage.version is None
                or coverage.version != terrain_version
            )
        )
        # Land evidence is present if the position is inside an explicit
        # footprint, or if there is any sample at the position even when its
        # version does not match.  In both cases the sea surface extension must
        # not be used as a substitute for the missing terrain evidence.
        nearest = coverage.nearest_sample(
            position.longitude,
            position.latitude,
            tolerance,
        )
        inside = (
            coverage.footprint is not None
            and coverage.contains(position.longitude, position.latitude)
        ) or nearest is not None
        sample = None
        if not version_conflict:
            sample = coverage.sample_at(
                position.longitude,
                position.latitude,
                tolerance,
            )
        if sample is not None:
            evidence = AltitudeSource(
                kind=AltitudeSourceKind.terrain,
                description=(
                    "terrain sample altitude "
                    f"{sample.altitude!r} for coverage version {coverage.version!r}"
                ),
                coverage_version=coverage.version,
                sample=sample,
                name=coverage.name,
            )
            return _resolve_with_evidence(
                position,
                evidence,
                sample.altitude,
                terrain_sample=sample,
                terrain_version=coverage.version,
            )
        if inside:
            # The position is inside declared land coverage but the evidence is
            # missing or belongs to another version.  Do not fall back to a
            # sea-surface height: that would silently turn missing terrain into
            # an assumed sea level.
            reason = _land_coverage_reason(
                coverage=coverage,
                terrain_version=terrain_version,
                original_version=original_version,
                nearby_sample=nearest,
            )
            return _unresolved(
                position,
                reason,
                terrain_version=coverage.version,
            )
    sea = _coerce_sea_surface(sea_surface)
    if sea is not None:
        sea_altitude = sea.altitude_at(position.longitude, position.latitude)
        if sea_altitude is not None:
            evidence = AltitudeSource(
                kind=AltitudeSourceKind.sea_surface,
                description=(
                    "sea surface altitude "
                    f"{sea_altitude!r} for coverage version {sea.version!r}"
                ),
                coverage_version=sea.version,
                sea_surface=True,
                name=sea.name,
            )
            return _resolve_with_evidence(
                position,
                evidence,
                sea_altitude,
                sea_surface_altitude=sea_altitude,
            )
    reason = _missing_ground_reason(
        position,
        coverage=coverage,
        sea=sea,
        terrain_version=terrain_version,
        original_version=(
            raw_coverage.version if isinstance(raw_coverage, TerrainCoverage) else None
        ),
    )
    return _unresolved(position, reason)


def _land_coverage_reason(
    *,
    coverage: TerrainCoverage,
    terrain_version: str | None,
    original_version: str | None,
    nearby_sample: TerrainSample | None = None,
) -> str:
    """Explain a missing sample inside declared land coverage."""
    if (
        nearby_sample is not None
        and not _version_matches(coverage.version, nearby_sample.coverage_version)
    ):
        return (
            "terrain sample at the position has coverage version "
            f"{nearby_sample.coverage_version!r}, expected {coverage.version!r}; "
            "a sample from another version is not used; missing terrain is "
            "never replaced with 0.0"
        )
    if (
        original_version is None
        and coverage.version is None
        and terrain_version is not None
    ):
        return (
            "terrain coverage has no version; a sample from a versioned dataset "
            f"is required for {terrain_version!r}; missing terrain is never "
            "replaced with 0.0"
        )
    if terrain_version is not None and coverage.version != terrain_version:
        return (
            f"terrain coverage version {coverage.version!r} does not match the "
            f"required version {terrain_version!r}; missing terrain is never "
            "replaced with 0.0"
        )
    return (
        "no terrain sample at the position for coverage version "
        f"{coverage.version!r}; the position is inside the land coverage, so the "
        "sea surface extension was not applied; missing terrain is never "
        "replaced with 0.0"
    )


def _missing_ground_reason(
    position: Position,
    *,
    coverage: TerrainCoverage | None,
    sea: SeaSurfaceExtension | None,
    terrain_version: str | None,
    original_version: str | None = None,
) -> str:
    """Build an actionable explanation for an unresolved ground mode."""
    if (
        position.effective_altitude_mode is AltitudeMode.relative_to_ground
        and position.altitude is None
    ):
        return (
            "relativeToGround requires an original altitude; the coordinate "
            "has none"
        )
    reasons = []
    if coverage is None:
        reasons.append("no terrain coverage was provided")
    elif original_version is None and coverage.version is None and terrain_version:
        reasons.append(
            "terrain coverage has no version; a sample from a versioned dataset "
            f"is required for {terrain_version!r}",
        )
    elif terrain_version is not None and coverage.version != terrain_version:
        reasons.append(
            f"terrain coverage version {coverage.version!r} does not match the "
            f"required version {terrain_version!r}",
        )
    else:
        reasons.append(
            "no terrain sample at the position for coverage version "
            f"{coverage.version!r}",
        )
    if sea is None:
        reasons.append("no sea surface extension was provided")
    else:
        reasons.append("the position is outside the sea surface footprint")
    reasons.append("missing terrain is never replaced with 0.0")
    return "; ".join(reasons)


def _unresolved(
    position: Position,
    reason: str,
    *,
    kind: AltitudeSourceKind = AltitudeSourceKind.unavailable,
    terrain_sample: TerrainSample | None = None,
    terrain_version: str | None = None,
    sea_surface_altitude: float | None = None,
) -> AltitudeResolution:
    """Build an unresolved result that still carries the original altitude."""
    return AltitudeResolution(
        position=position,
        absolute_altitude=None,
        source=AltitudeSource(kind=kind, description=reason),
        reason=reason,
        terrain_sample=terrain_sample,
        terrain_version=terrain_version,
        sea_surface_altitude=sea_surface_altitude,
    )


def resolve_altitude(
    position: Position,
    *,
    terrain: Any = None,
    terrain_version: str | None = None,
    sea_surface: Any = None,
    sea_floor: Any = None,
    sea_floor_version: str | None = None,
    tolerance: float | None = None,
) -> AltitudeResolution:
    """
    Resolve one position to absolute altitude, recording its evidence.

    ``terrain`` may be a :class:`TerrainCoverage`, an iterable of
    :class:`TerrainSample` objects, or a mapping from ``(longitude, latitude)``
    to altitude.  ``sea_surface`` accepts a :class:`SeaSurfaceExtension`, a
    number, or a mapping with ``altitude``/``level``, ``footprint`` and
    ``version`` keys.  ``sea_floor`` uses the same forms as ``terrain`` for the
    ``gx`` sea-floor modes.

    Missing terrain never becomes ``0.0``: the result has
    ``absolute_altitude is None`` and an explanatory ``reason``.
    """
    mode = position.effective_altitude_mode
    if mode is AltitudeMode.absolute:
        return _resolve_absolute(position)
    if mode in _SEA_FLOOR_MODES:
        return _resolve_sea_floor(
            position,
            sea_floor=sea_floor,
            sea_floor_version=sea_floor_version,
            tolerance=tolerance,
        )
    if mode is AltitudeMode.relative_to_ground and position.altitude is None:
        return _unresolved(
            position,
            "relativeToGround requires an original altitude; the coordinate has none",
        )
    return _resolve_ground_mode(
        position,
        terrain=terrain,
        terrain_version=terrain_version,
        sea_surface=sea_surface,
        tolerance=tolerance,
    )


def _feature_id(feature: Any) -> str | None:
    """Return a non-empty feature id."""
    identifier = getattr(feature, "id", None)
    return identifier or None


def _feature_name(feature: Any) -> str | None:
    """Return a non-empty feature name."""
    name = getattr(feature, "name", None)
    return name or None


def _feature_component(feature: Any) -> str:
    """Build one path component for a feature."""
    component = type(feature).__name__
    identifier = _feature_id(feature)
    name = _feature_name(feature)
    if identifier:
        component += f"[id={identifier}]"
    if name:
        component += f"[name={name}]"
    return component


def _position_source(
    *,
    feature_id: str | None,
    feature_name: str | None,
    feature_path: str,
    geometry_type: str,
    part: str | None,
    coordinate_index: int | None,
    timestamp: str | None = None,
) -> PositionSource:
    """Build a PositionSource without repeating keyword arguments."""
    return PositionSource(
        feature_id=feature_id,
        feature_name=feature_name,
        feature_path=feature_path,
        geometry_type=geometry_type,
        part=part,
        coordinate_index=coordinate_index,
        timestamp=timestamp,
    )


def _add_coords(
    catalog: PositionCatalog,
    coords: Iterable[Any],
    *,
    altitude_mode: AltitudeMode | None,
    feature_id: str | None,
    feature_name: str | None,
    feature_path: str,
    geometry_type: str,
    part: str,
    timestamp: str | None = None,
) -> None:
    """Append every usable coordinate in ``coords`` to the catalogue."""
    for index, coord in enumerate(coords):
        if len(coord) < 2:  # noqa: PLR2004
            continue
        longitude, latitude = coord[0], coord[1]
        if longitude is None or latitude is None:
            continue
        altitude = coord[2] if len(coord) >= 3 and coord[2] is not None else None  # noqa: PLR2004
        catalog.add(
            Position(
                longitude=float(longitude),
                latitude=float(latitude),
                altitude=float(altitude) if altitude is not None else None,
                altitude_mode=altitude_mode,
                source=_position_source(
                    feature_id=feature_id,
                    feature_name=feature_name,
                    feature_path=feature_path,
                    geometry_type=geometry_type,
                    part=part,
                    coordinate_index=index,
                    timestamp=timestamp,
                ),
            ),
        )


def _point_coords(point: Point) -> list[Any]:
    """Return a point's KML coordinates, preferring its raw coordinate tuple."""
    if point.kml_coordinates is not None and point.kml_coordinates.coords:
        return list(point.kml_coordinates.coords)
    geometry = point.geometry
    return list(geometry.coords) if geometry is not None else []


def _line_coords(line: LineString) -> list[Any]:
    """Return a line's KML coordinates, preferring its raw coordinate tuple."""
    if line.kml_coordinates is not None and line.kml_coordinates.coords:
        return list(line.kml_coordinates.coords)
    geometry = line.geometry
    return list(geometry.coords) if geometry is not None else []


def _ring_coords(boundary: Any) -> list[Any]:
    """Return the coordinates of a polygon boundary."""
    ring = getattr(boundary, "kml_geometry", None)
    if ring is not None:
        return _line_coords(ring)
    geometry = getattr(boundary, "geometry", None)
    return list(geometry.coords) if geometry is not None else []


def _extract_polygon(
    polygon: Polygon,
    catalog: PositionCatalog,
    *,
    feature_id: str | None,
    feature_name: str | None,
    feature_path: str,
    prefix: str,
) -> None:
    """Extract the outer and inner rings of a polygon."""
    outer_part = f"{prefix}.outerBoundaryIs" if prefix else "outerBoundaryIs"
    _add_coords(
        catalog,
        _ring_coords(polygon.outer_boundary),
        altitude_mode=polygon.altitude_mode,
        feature_id=feature_id,
        feature_name=feature_name,
        feature_path=feature_path,
        geometry_type="Polygon",
        part=outer_part,
    )
    for index, boundary in enumerate(polygon.inner_boundaries):
        inner_part = (
            f"{prefix}.innerBoundaryIs[{index}]"
            if prefix
            else f"innerBoundaryIs[{index}]"
        )
        _add_coords(
            catalog,
            _ring_coords(boundary),
            altitude_mode=polygon.altitude_mode,
            feature_id=feature_id,
            feature_name=feature_name,
            feature_path=feature_path,
            geometry_type="Polygon",
            part=inner_part,
        )


def _extract_interface(  # noqa: C901
    interface: Mapping[str, Any],
    catalog: PositionCatalog,
    *,
    feature_id: str | None,
    feature_name: str | None,
    feature_path: str,
    prefix: str,
) -> None:
    """Extract positions from a GeoJSON-style mapping without inventing a mode."""
    kind = interface.get("type")
    coordinates = interface.get("coordinates")
    if kind == "Point":
        _add_coords(
            catalog,
            [coordinates] if coordinates else [],
            altitude_mode=None,
            feature_id=feature_id,
            feature_name=feature_name,
            feature_path=feature_path,
            geometry_type="Point",
            part=prefix or "Point",
        )
        return
    if kind in ("LineString", "LinearRing"):
        _add_coords(
            catalog,
            coordinates or [],
            altitude_mode=None,
            feature_id=feature_id,
            feature_name=feature_name,
            feature_path=feature_path,
            geometry_type=kind,
            part=prefix or kind,
        )
        return
    if kind == "MultiPoint":
        _add_coords(
            catalog,
            coordinates or [],
            altitude_mode=None,
            feature_id=feature_id,
            feature_name=feature_name,
            feature_path=feature_path,
            geometry_type="MultiPoint",
            part=prefix or "MultiPoint",
        )
        return
    if kind == "MultiLineString":
        for index, line in enumerate(coordinates or []):
            line_prefix = (
                f"{prefix}.MultiLineString[{index}]"
                if prefix
                else f"MultiLineString[{index}]"
            )
            _add_coords(
                catalog,
                line,
                altitude_mode=None,
                feature_id=feature_id,
                feature_name=feature_name,
                feature_path=feature_path,
                geometry_type="MultiLineString",
                part=line_prefix,
            )
        return
    if kind == "GeometryCollection":
        for index, child in enumerate(interface.get("geometries", [])):
            child_prefix = (
                f"{prefix}.GeometryCollection[{index}]"
                if prefix
                else f"GeometryCollection[{index}]"
            )
            _extract_interface(
                child,
                catalog,
                feature_id=feature_id,
                feature_name=feature_name,
                feature_path=feature_path,
                prefix=child_prefix,
            )
        return
    if kind == "Polygon":
        _extract_interface_polygon(
            coordinates,
            catalog,
            feature_id=feature_id,
            feature_name=feature_name,
            feature_path=feature_path,
            prefix=prefix,
            geometry_type="Polygon",
        )
        return
    if kind == "MultiPolygon":
        for index, polygon in enumerate(coordinates or []):
            _extract_interface_polygon(
                polygon,
                catalog,
                feature_id=feature_id,
                feature_name=feature_name,
                feature_path=feature_path,
                prefix=f"{prefix}.MultiPolygon[{index}]"
                if prefix
                else f"MultiPolygon[{index}]",
                geometry_type="MultiPolygon",
            )


def _extract_interface_polygon(
    polygon: Any,
    catalog: PositionCatalog,
    *,
    feature_id: str | None,
    feature_name: str | None,
    feature_path: str,
    prefix: str,
    geometry_type: str,
) -> None:
    """Extract the rings of a GeoJSON polygon."""
    if not polygon:
        return
    _add_coords(
        catalog,
        polygon[0],
        altitude_mode=None,
        feature_id=feature_id,
        feature_name=feature_name,
        feature_path=feature_path,
        geometry_type=geometry_type,
        part=f"{prefix}.outerBoundaryIs" if prefix else "outerBoundaryIs",
    )
    for index, ring in enumerate(polygon[1:]):
        _add_coords(
            catalog,
            ring,
            altitude_mode=None,
            feature_id=feature_id,
            feature_name=feature_name,
            feature_path=feature_path,
            geometry_type=geometry_type,
            part=(
                f"{prefix}.innerBoundaryIs[{index}]"
                if prefix
                else f"innerBoundaryIs[{index}]"
            ),
        )


def _extract_geometry(  # noqa: PLR0911
    geometry: Any,
    catalog: PositionCatalog,
    *,
    feature_id: str | None,
    feature_name: str | None,
    feature_path: str,
    prefix: str = "",
) -> None:
    """Extract positions from any supported fastkml geometry object."""
    if isinstance(geometry, Point):
        _add_coords(
            catalog,
            _point_coords(geometry),
            altitude_mode=geometry.altitude_mode,
            feature_id=feature_id,
            feature_name=feature_name,
            feature_path=feature_path,
            geometry_type="Point",
            part=prefix or "Point",
        )
        return
    if isinstance(geometry, Polygon):
        _extract_polygon(
            geometry,
            catalog,
            feature_id=feature_id,
            feature_name=feature_name,
            feature_path=feature_path,
            prefix=prefix,
        )
        return
    if isinstance(geometry, LineString):
        name = type(geometry).__name__
        _add_coords(
            catalog,
            _line_coords(geometry),
            altitude_mode=geometry.altitude_mode,
            feature_id=feature_id,
            feature_name=feature_name,
            feature_path=feature_path,
            geometry_type=name,
            part=prefix or name,
        )
        return
    if isinstance(geometry, MultiGeometry):
        _extract_multigeometry(
            geometry,
            catalog,
            feature_id=feature_id,
            feature_name=feature_name,
            feature_path=feature_path,
            prefix=prefix,
        )
        return
    if isinstance(geometry, Track):
        _extract_track(
            geometry,
            catalog,
            feature_id=feature_id,
            feature_name=feature_name,
            feature_path=feature_path,
            prefix=prefix,
        )
        return
    if isinstance(geometry, MultiTrack):
        _extract_multitrack(
            geometry,
            catalog,
            feature_id=feature_id,
            feature_name=feature_name,
            feature_path=feature_path,
            prefix=prefix,
        )
        return
    if isinstance(geometry, Model):
        _extract_model(
            geometry,
            catalog,
            feature_id=feature_id,
            feature_name=feature_name,
            feature_path=feature_path,
            prefix=prefix,
        )
        return
    interface = getattr(geometry, "__geo_interface__", None)
    if interface is not None:
        _extract_interface(
            interface,
            catalog,
            feature_id=feature_id,
            feature_name=feature_name,
            feature_path=feature_path,
            prefix=prefix,
        )


def _extract_multigeometry(
    geometry: MultiGeometry,
    catalog: PositionCatalog,
    *,
    feature_id: str | None,
    feature_name: str | None,
    feature_path: str,
    prefix: str,
) -> None:
    """Extract each child of a MultiGeometry with an indexed part name."""
    for index, child in enumerate(geometry.kml_geometries):
        child_prefix = (
            f"{prefix}.MultiGeometry[{index}]" if prefix else f"MultiGeometry[{index}]"
        )
        _extract_geometry(
            child,
            catalog,
            feature_id=feature_id,
            feature_name=feature_name,
            feature_path=feature_path,
            prefix=child_prefix,
        )


def _extract_multitrack(
    geometry: MultiTrack,
    catalog: PositionCatalog,
    *,
    feature_id: str | None,
    feature_name: str | None,
    feature_path: str,
    prefix: str,
) -> None:
    """Extract each track of a MultiTrack with an indexed part name."""
    for index, track in enumerate(geometry.tracks):
        track_prefix = (
            f"{prefix}.MultiTrack[{index}]" if prefix else f"MultiTrack[{index}]"
        )
        _extract_track(
            track,
            catalog,
            feature_id=feature_id,
            feature_name=feature_name,
            feature_path=feature_path,
            prefix=track_prefix,
        )


def _when_text(item: Any) -> str | None:
    """Return a track item's timestamp as an ISO string when possible."""
    when = getattr(item, "when", None)
    if when is None:
        return None
    value = getattr(when, "dt", when)
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if isoformat is not None else str(value)


def _extract_track(
    track: Track,
    catalog: PositionCatalog,
    *,
    feature_id: str | None,
    feature_name: str | None,
    feature_path: str,
    prefix: str,
) -> None:
    """Extract positions and timestamps from a gx:Track."""
    part = f"{prefix}.Track" if prefix else "Track"
    for item in track.track_items:
        coord = item.coord
        coords = coord.coords if coord is not None else ()
        _add_coords(
            catalog,
            coords,
            altitude_mode=track.altitude_mode,
            feature_id=feature_id,
            feature_name=feature_name,
            feature_path=feature_path,
            geometry_type="Track",
            part=part,
            timestamp=_when_text(item),
        )


def _extract_model(
    model: Model,
    catalog: PositionCatalog,
    *,
    feature_id: str | None,
    feature_name: str | None,
    feature_path: str,
    prefix: str,
) -> None:
    """Extract the location of a KML Model."""
    location = model.location
    if location is None:
        return
    coords = [
        (
            location.longitude,
            location.latitude,
            location.altitude if location.altitude is not None else None,
        ),
    ]
    part = f"{prefix}.Model.Location" if prefix else "Model.Location"
    _add_coords(
        catalog,
        coords,
        altitude_mode=model.altitude_mode,
        feature_id=feature_id,
        feature_name=feature_name,
        feature_path=feature_path,
        geometry_type="Model",
        part=part,
    )


def _extract_ground_overlay(
    overlay: GroundOverlay,
    catalog: PositionCatalog,
    path: tuple[str, ...],
) -> None:
    """Extract the centre of a GroundOverlay's LatLonBox when it carries altitude."""
    box = overlay.lat_lon_box
    if box is None:
        return
    component = _feature_component(overlay)
    feature_path = "/".join((*path, component))
    longitude = _box_center(box.west, box.east)
    latitude = _box_center(box.south, box.north)
    if longitude is None or latitude is None:
        return
    _add_coords(
        catalog,
        [(longitude, latitude, overlay.altitude)],
        altitude_mode=overlay.altitude_mode,
        feature_id=_feature_id(overlay),
        feature_name=_feature_name(overlay),
        feature_path=feature_path,
        geometry_type="GroundOverlay",
        part="GroundOverlay.LatLonBox",
    )


def _box_center(low: float | None, high: float | None) -> float | None:
    """Return the centre of a LatLonBox axis, handling antimeridian crossing."""
    if low is None or high is None:
        return None
    if low <= high:
        return (low + high) / 2
    return ((low + high + 360.0) / 2) % 360.0


def _collect_feature(
    feature: Any,
    catalog: PositionCatalog,
    path: tuple[str, ...],
) -> None:
    """Collect positions from one feature and recurse into containers."""
    if isinstance(feature, (Document, Folder)):
        component = _feature_component(feature)
        for child in feature.features:
            _collect_feature(child, catalog, (*path, component))
        return
    if isinstance(feature, Placemark):
        component = _feature_component(feature)
        feature_path = "/".join((*path, component))
        geometry = feature.kml_geometry
        if geometry is not None:
            _extract_geometry(
                geometry,
                catalog,
                feature_id=_feature_id(feature),
                feature_name=_feature_name(feature),
                feature_path=feature_path,
            )
        return
    if isinstance(feature, PhotoOverlay):
        component = _feature_component(feature)
        feature_path = "/".join((*path, component))
        if feature.point is not None:
            _extract_geometry(
                feature.point,
                catalog,
                feature_id=_feature_id(feature),
                feature_name=_feature_name(feature),
                feature_path=feature_path,
                prefix="PhotoOverlay.Point",
            )
        return
    if isinstance(feature, GroundOverlay):
        _extract_ground_overlay(feature, catalog, path)
        return
    features = getattr(feature, "features", None)
    if features is not None:
        for child in features:
            _collect_feature(child, catalog, path)
        return
    _extract_geometry(
        feature,
        catalog,
        feature_id=None,
        feature_name=None,
        feature_path="/".join(path),
    )


def _load_kml_source(source: str | bytes | Path) -> KML:
    """Load a KML document from XML text, bytes, or a filesystem path."""
    if isinstance(source, bytes):
        return KML.from_string(source)
    if isinstance(source, Path):
        return KML.parse(source)
    text = source.lstrip()
    if text.startswith("<"):
        return KML.from_string(source)
    path = Path(source)
    if path.exists():
        return KML.parse(path)
    msg = (
        "Cannot interpret the string as KML XML or as an existing file path; "
        "pass a KML object, an XML string, or a readable path"
    )
    raise ValueError(msg)


def collect_positions(source: Any) -> PositionCatalog:
    """
    Collect every supported geometry position into one catalogue.

    ``source`` may be a :class:`fastkml.KML` document, an XML string, a path to
    a KML file, a feature, a geometry, an iterable of those, or an existing
    :class:`PositionCatalog`.  Original altitudes and altitude modes are copied
    unchanged; no terrain conversion is performed here.
    """
    if isinstance(source, PositionCatalog):
        return source
    if isinstance(source, Position):
        return PositionCatalog([source])
    if isinstance(source, (str, bytes, Path)):
        return collect_positions(_load_kml_source(source))
    catalog = PositionCatalog()
    if isinstance(source, Mapping) and source.get("type") in _INTERFACE_TYPES:
        _extract_interface(
            source,
            catalog,
            feature_id=None,
            feature_name=None,
            feature_path="",
            prefix="",
        )
        return catalog
    if (
        isinstance(source, Iterable)
        and not isinstance(source, Mapping)
        and not hasattr(source, "__geo_interface__")
    ):
        for element in source:
            catalog.extend(collect_positions(element))
        return catalog
    _collect_feature(source, catalog, ())
    return catalog


def resolve_altitudes(source: Any, **kwargs: Any) -> list[AltitudeResolution]:
    """Collect positions if needed and resolve each one with shared evidence."""
    if isinstance(source, PositionCatalog):
        return source.resolve_all(**kwargs)
    return collect_positions(source).resolve_all(**kwargs)


def absolute_altitude(position: Position, **kwargs: Any) -> float | None:
    """Return the resolved absolute altitude while keeping provenance available."""
    return resolve_altitude(position, **kwargs).absolute_altitude


def catalog_kml(source: Any) -> PositionCatalog:
    """Collect geometry positions from KML text, a path, or a KML object."""
    return collect_positions(source)


AltitudeCatalog = PositionCatalog
AltitudeEntry = Position
CatalogEntry = Position
SeaSurface = SeaSurfaceExtension
SeaFloorCoverage = TerrainCoverage
TerrainModel = TerrainCoverage
TerrainSamples = TerrainCoverage
AltitudeOrigin = AltitudeSourceKind
collect_altitudes = collect_positions
build_catalog = collect_positions
resolve_catalog = resolve_altitudes
resolve_position = resolve_altitude
