Altitude Catalogues
===================

KML stores altitude together with a mode, and the same number means different
things in each mode:

``absolute``
    Metres above sea level. No terrain lookup is involved.

``relativeToGround``
    Metres above the terrain at that position. The absolute height is the
    terrain height plus the coordinate altitude.

``clampToGround``
    The coordinate altitude is ignored and the feature follows the terrain.

``relativeToSeaFloor`` / ``clampToSeaFloor``
    The ``gx`` variants use the sea floor instead of the land surface.

``fastkml.altitude`` collects geometry positions from a document into one
catalogue and keeps the original altitude and mode on every entry. It does not
rewrite geometry in place, so a collection pipeline can resolve heights without
losing what the source KML actually said.

Collecting positions
--------------------

``collect_positions`` accepts a ``KML`` document, XML text, a file path, a
feature, a geometry object, a ``__geo_interface__`` mapping, an existing
``PositionCatalog``, or an iterable of those. ``PositionCatalog.from_kml`` and
``catalog_kml`` are aliases, and ``collect_altitudes`` / ``build_catalog`` are
compatibility spellings. ``PositionCatalog(source=...)`` is the constructor
form and ``PositionCatalog.from_source(source, terrain=..., ...)`` stores
resolution defaults for later ``resolve()`` calls.

.. code-block:: pycon

    >>> from fastkml import KML
    >>> from fastkml.altitude import PositionCatalog

    >>> document = KML.from_string(
    ...     '<kml xmlns="http://www.opengis.net/kml/2.2">'
    ...     "<Placemark id='p1'><name>Site</name><Point>"
    ...     "<altitudeMode>relativeToGround</altitudeMode>"
    ...     "<coordinates>13.4,52.5,10</coordinates>"
    ...     "</Point></Placemark></kml>"
    ... )
    >>> catalog = PositionCatalog.from_kml(document)
    >>> entry = catalog[0]
    >>> entry.original_altitude, entry.original_altitude_mode.value
    (10.0, 'relativeToGround')
    >>> entry.altitude_mode is None
    False

Each entry records where it came from through ``entry.source``: feature id and
name, the container path, the geometry type, the polygon ring or
``MultiGeometry`` index, the coordinate index, and a ``gx:Track`` timestamp when
one exists. ``entry.to_dict()`` returns the same information as plain values.

Supported geometry includes ``Point``, ``LineString``, ``LinearRing``,
``Polygon`` outer and inner rings, ``MultiGeometry``, ``gx:Track``,
``gx:MultiTrack``, ``Model`` locations, ``GroundOverlay`` centres, and
``PhotoOverlay`` points. Objects exposing ``__geo_interface__`` are accepted
too, but they have no KML altitude mode unless you construct ``Position``
objects yourself.

Resolving absolute altitude
---------------------------

``resolve_altitude`` takes one ``Position`` and explicit evidence. Terrain can
be a ``TerrainCoverage``, a list of ``TerrainSample`` objects or
``(longitude, latitude, altitude)`` tuples, a mapping from a coordinate pair to
an altitude, or a lookup callable. ``tolerance`` is the maximum great-circle
distance in metres for matching a sample.

.. code-block:: pycon

    >>> from fastkml.altitude import Position, TerrainCoverage, TerrainSample
    >>> from fastkml.enums import AltitudeMode
    >>> from fastkml.altitude import resolve_altitude

    >>> position = Position(13.4, 52.5, 10.0, AltitudeMode.relative_to_ground)
    >>> coverage = TerrainCoverage(
    ...     samples=[TerrainSample(13.4, 52.5, 100.0, coverage_version="2026-11")],
    ...     version="2026-11",
    ... )
    >>> result = resolve_altitude(
    ...     position,
    ...     terrain=coverage,
    ...     terrain_version="2026-11",
    ... )
    >>> result.absolute_altitude
    110.0
    >>> result.origin.value
    'terrain'

Convenience constructors cover common data shapes:
``TerrainCoverage.from_samples(...)`` flattens individual samples,
``TerrainCoverage({(lon, lat): height}, version=...)`` builds from a mapping,
and ``SeaSurfaceExtension.from_level(0.0, ...)`` names the sea-level case
explicitly. A ``Position`` also accepts the KML spelling of an altitude mode,
for example ``Position(13.4, 52.5, 10.0, "relativeToGround")``.

Terrain evidence is only used when it is sufficient:

* A versioned coverage accepts only samples carrying the same version. A sample
  without a version is not assumed to belong to it.
* A sample outside ``tolerance`` does not match.
* A position inside a declared land footprint with no matching sample stays
  unresolved. The sea-surface extension is not used as a substitute.
* ``relativeToGround`` needs both a terrain height and the original coordinate
  altitude. A missing altitude is not interpreted as zero.

When evidence is missing, ``absolute_altitude`` is ``None`` and ``reason``
explains what was missing. The original altitude and mode remain on
``result.position`` and are repeated in ``result.to_dict()`` under
``original_altitude`` and ``original_altitude_mode``.

Sea surface and sea floor
-------------------------

``SeaSurfaceExtension`` is separate from land terrain on purpose. A sea-level
result is evidence in its own right and is reported with
``origin is AltitudeSourceKind.sea_surface`` and ``is_sea_surface == True``.
This makes it distinguishable from a missing terrain sample that happened to
leave the result at zero.

.. code-block:: pycon

    >>> from fastkml.altitude import SeaSurfaceExtension

    >>> result = resolve_altitude(
    ...     position,
    ...     sea_surface=SeaSurfaceExtension(altitude=0.0, version="sea-2026"),
    ... )
    >>> result.absolute_altitude
    10.0
    >>> result.is_sea_surface
    True

``clampToSeaFloor`` and ``relativeToSeaFloor`` use ``sea_floor`` and
``sea_floor_version`` instead. They never borrow the land terrain coverage.
Both sea modes reject a version mismatch rather than converting with data from
another release.

Auditing conversions
--------------------

``AltitudeResolution.to_dict()`` (also available as ``.provenance``) is the
record to keep in an audit log. It contains the original longitude, latitude,
altitude, and mode; the resolved absolute altitude; the terrain sample altitude
and version; the sea-surface altitude; the evidence kind; and the full position
source. ``PositionCatalog.to_dict()`` does the same for the whole catalogue.

``resolve_altitudes(source, ...)`` and ``PositionCatalog.resolve(...)`` apply the
same evidence to every entry and preserve catalogue order. Resolution is a pure
function of a position and its evidence, so switching the order of entries does
not change any individual result.
