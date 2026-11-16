# Copyright (C) 2012 - 2023 Christian Ledermann
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

"""GX Track Extension."""

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Any

import pygeoif.geometry as geo
from pygeoif.types import PointType

from fastkml import config
from fastkml.abstract_geometry import _Geometry
from fastkml.enums import AltitudeMode
from fastkml.helpers import bool_subelement
from fastkml.helpers import coords_subelement_list
from fastkml.helpers import coords_subelement_list_kwarg
from fastkml.helpers import datetime_subelement_list
from fastkml.helpers import datetime_subelement_list_kwarg
from fastkml.helpers import enum_subelement
from fastkml.helpers import subelement_bool_kwarg
from fastkml.helpers import subelement_enum_kwarg
from fastkml.helpers import xml_subelement_list
from fastkml.helpers import xml_subelement_list_kwarg
from fastkml.registry import RegistryItem
from fastkml.registry import registry
from fastkml.times import KmlDateTime

if TYPE_CHECKING:
    from fastkml.data import ExtendedData


__all__ = [
    "Angle",
    "MultiTrack",
    "Track",
    "TrackItem",
    "TrackSample",
    "track_items_to_geometry",
    "tracks_to_geometry",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Angle:
    """
    The gx:angles element specifies the heading, tilt, and roll.

    The angles are specified in degrees, and the
    default values are 0 (heading and tilt) and 0 (roll). The angles
    are specified in the following order: heading, tilt, roll.
    """

    heading: float = 0.0
    tilt: float = 0.0
    roll: float = 0.0

    @property
    def coords(self) -> PointType:
        """
        Get the coordinates of the angle.

        Returns
        -------
        PointType
            The coordinates of the angle.

        """
        return (self.heading, self.tilt, self.roll)


@dataclass(frozen=True)
class TrackItem:
    """A track item describes an objects position and heading at a specific time."""

    when: KmlDateTime | None
    coord: geo.Point | None = None
    angle: Angle | None = None


@dataclass(frozen=True)
class TrackSample:
    """
    One aligned sample of a track.

    A sample keeps its original position even when one or more columns are
    missing.  Missing columns are ``None``.
    """

    segment: int
    index: int
    when: KmlDateTime | None = None
    coord: geo.Point | None = None
    angle: Angle | None = None
    measurements: dict[str, str | None] = field(default_factory=dict)

    @property
    def identity(self) -> tuple[int, int]:
        """Return the sample identity as ``(segment, index)``."""
        return (self.segment, self.index)

    @property
    def sample_id(self) -> tuple[int, int]:
        """Return the sample identity as ``(segment, index)``."""
        return self.identity

    @property
    def missing(self) -> frozenset[str]:
        """Return the names of the columns missing from this sample."""
        missing = {
            name
            for name, value in (
                ("when", self.when),
                ("coord", self.coord),
                ("angles", self.angle),
            )
            if value is None
        }
        missing.update(
            name for name, value in self.measurements.items() if value is None
        )
        return frozenset(missing)

    @property
    def mask(self) -> dict[str, bool]:
        """Return a per-column missing mask, ``True`` meaning missing."""
        mask = {
            "when": self.when is None,
            "coord": self.coord is None,
            "angles": self.angle is None,
        }
        for name, value in self.measurements.items():
            mask[name] = value is None
        return mask

    @property
    def core_mask(self) -> tuple[bool, bool, bool]:
        """Return the ``when, coord, angles`` mask as a tuple."""
        return (self.when is None, self.coord is None, self.angle is None)

    @property
    def is_complete(self) -> bool:
        """Return ``True`` when no column is missing for this sample."""
        return not self.missing


def _split_points(track_items: Iterable[TrackItem]) -> list[list[PointType]]:
    """Split track items at missing coordinates, preserving sample order."""
    segments: list[list[PointType]] = []
    current: list[PointType] = []
    for item in track_items:
        if item.coord is None or not item.coord.coords:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(item.coord.coords[0])  # type: ignore[misc]
    if current:
        segments.append(current)
    return segments


def _segments_to_geometry(
    segments: Iterable[Iterable[PointType]],
) -> geo.LineString | geo.MultiLineString:
    """Build a geometry without connecting across missing samples."""
    lines = [
        geo.LineString.from_points(*(geo.Point(*point) for point in segment))
        for segment in segments
        if segment
    ]
    if len(lines) == 1:
        return lines[0]
    return geo.MultiLineString.from_linestrings(*lines)


def _array_data(
    extended_data: "ExtendedData | None",
) -> list[tuple[str, list[str | None]]]:
    """Collect ``gx:SimpleArrayData`` columns from extended data."""
    if not extended_data:
        return []
    return [
        (array.name, list(array.data))
        for element in extended_data.elements
        if getattr(element, "array_data", None) is not None
        for array in element.array_data
        if array.name
    ]


def track_items_to_geometry(
    track_items: Iterable[TrackItem],
) -> geo.LineString | geo.MultiLineString:
    """
    Convert a sequence of TrackItems to a geometry.

    Missing coordinates split the result into separate line segments instead of
    shifting later coordinates forward.

    Args:
    ----
        track_items : Iterable[TrackItem]
            A sequence of TrackItems.

    Returns:
    -------
        Union[geo.LineString, geo.MultiLineString]
            A geometry representing the track.  A single continuous run of
            coordinates yields a ``LineString``; a gap yields a
            ``MultiLineString`` with one line per run.

    """
    return _segments_to_geometry(_split_points(track_items))


class Track(_Geometry):
    """
    A track describes how an object moves through the world over a given time period.

    This feature allows you to create one visible object in Google Earth
    (either a Point icon or a Model) that encodes multiple positions for the same object
    for multiple times. In Google Earth, the time slider allows the user to move the
    view through time, which animates the position of the object.

    Tracks are a more efficient mechanism for associating time data with visible
    Features, since you create only one Feature, which can be associated with multiple
    time elements as the object moves through space.

    https://developers.google.com/kml/documentation/kmlreference#gxtrack
    """

    _default_nsid = config.GX
    track_items: list[TrackItem]
    extended_data: "ExtendedData | None"

    def __init__(
        self,
        *,
        ns: str | None = None,
        name_spaces: dict[str, str] | None = None,
        id: str | None = None,
        target_id: str | None = None,
        altitude_mode: AltitudeMode | None = None,
        track_items: Iterable[TrackItem] | None = None,
        whens: Iterable[KmlDateTime | None] | None = None,
        coords: Iterable[PointType | None] | None = None,
        angles: Iterable[PointType | None] | None = None,
        extended_data: "ExtendedData | None" = None,
        **kwargs: Any,
    ) -> None:
        """
        Initialize a GX object.

        ``whens``, ``coords``, and ``angles`` are positional columns.  A
        missing entry (``None``) marks a gap for that position.  Columns are
        padded at the end only; values are never shifted forward onto a later
        sample.

        Parameters
        ----------
        ns : Optional[str], optional
            The namespace for the GX object, by default None
        name_spaces : Optional[Dict[str, str]], optional
            A dictionary of namespace prefixes and URIs, by default None
        id : Optional[str], optional
            The ID of the GX object, by default None
        target_id : Optional[str], optional
            The target ID of the GX object, by default None
        altitude_mode : Optional[AltitudeMode], optional
            The altitude mode of the GX object, by default None
        track_items : Optional[Iterable[TrackItem]], optional
            The track items of the GX object, by default None
        whens : Optional[Iterable[Optional[KmlDateTime]]], optional
            The timestamps of the track items, by default None
        coords : Optional[Iterable[Optional[PointType]]], optional
            The coordinates of the track items, by default None
        angles : Optional[Iterable[Optional[PointType]]], optional
            The angles of the track items, by default None.  When omitted the
            KML default of zero heading, tilt, and roll applies; when supplied,
            missing entries stay missing.
        extended_data : Optional[ExtendedData], optional
            The extended data of the GX object, by default None
        **kwargs : Any, optional
            Additional keyword arguments.

        Raises
        ------
        ValueError
            If both `track_items` and `whens`/`coords` are specified.

        """
        if (whens or coords) and track_items:
            msg = "Cannot specify both geometry and track_items"
            raise ValueError(msg)
        if not track_items and (whens or coords):
            # ``whens``, ``coords``, and ``angles`` are independent columns;
            # a missing entry keeps its position instead of consuming the
            # next available value.
            when_list = list(whens) if whens else []
            coord_list = list(coords) if coords else []
            angle_list = list(angles) if angles is not None else None
            count = max(
                len(when_list),
                len(coord_list),
                len(angle_list) if angle_list is not None else 0,
            )
            track_items = [
                TrackItem(
                    when=when_list[i] if i < len(when_list) else None,
                    coord=geo.Point(*coord_list[i])
                    if i < len(coord_list) and coord_list[i] is not None
                    else None,
                    angle=(
                        Angle(*angle_list[i])
                        if i < len(angle_list) and angle_list[i] is not None
                        else None
                    )
                    if angle_list is not None
                    else Angle(),
                )
                for i in range(count)
            ]
        self.track_items = list(track_items) if track_items else []
        self.extended_data = extended_data
        super().__init__(
            ns=ns,
            name_spaces=name_spaces,
            id=id,
            target_id=target_id,
            altitude_mode=altitude_mode,
            **kwargs,
        )

    def __repr__(self) -> str:
        """
        Create a string representation for Track.

        Returns
        -------
        str
            The string representation of the Track object.

        """
        return (
            f"{self.__class__.__module__}.{self.__class__.__name__}("
            f"ns={self.ns!r}, "
            f"name_spaces={self.name_spaces!r}, "
            f"id={self.id!r}, "
            f"target_id={self.target_id!r}, "
            f"altitude_mode={self.altitude_mode}, "
            f"track_items={self.track_items!r}, "
            f"extended_data={self.extended_data!r}, "
            f"**{self._get_splat()!r},"
            ")"
        )

    @property
    def geometry(self) -> geo.LineString | geo.MultiLineString | None:
        """
        Get the geometry of the track.

        Returns
        -------
        Optional[Union[geo.LineString, geo.MultiLineString]]
            The geometry of the track.  Missing coordinates split the track
            into separate line segments.

        """
        return track_items_to_geometry(self.track_items)

    @property
    def whens(self) -> tuple[KmlDateTime | None, ...]:
        """
        Get the timestamps of the track items.

        Returns
        -------
        Tuple[Optional[KmlDateTime]]
            The timestamps of the track items, one entry per sample.

        """
        return tuple(item.when for item in self.track_items)

    @property
    def coords(self) -> tuple[PointType | None, ...]:
        """
        Get the coordinates of the track items.

        Returns
        -------
        Tuple[Optional[PointType]]
            The coordinates of the track items, one entry per sample.

        """
        return tuple(
            item.coord.coords[0] if item.coord and item.coord.coords else None  # type: ignore[misc]
            for item in self.track_items
        )

    @property
    def angles(self) -> tuple[PointType | None, ...]:
        """
        Get the angles of the track items.

        Returns
        -------
        Tuple[Optional[PointType]]
            The angles of the track items, one entry per sample.

        """
        return tuple(
            item.angle.coords if item.angle else None for item in self.track_items
        )

    @property
    def samples(self) -> tuple[TrackSample, ...]:
        """
        Return the aligned samples of the track.

        Every original position is preserved; a missing column is ``None``.
        Later samples are never shifted forward to fill a gap.  Extended
        measurement columns are aligned by the same index.
        """
        measurements = self.measurements
        count = max(
            [len(self.track_items)] + [len(values) for values in measurements.values()],
        )
        return tuple(
            TrackSample(
                segment=0,
                index=index,
                when=self.track_items[index].when
                if index < len(self.track_items)
                else None,
                coord=self.track_items[index].coord
                if index < len(self.track_items)
                else None,
                angle=self.track_items[index].angle
                if index < len(self.track_items)
                else None,
                measurements={
                    name: values[index] if index < len(values) else None
                    for name, values in measurements.items()
                },
            )
            for index in range(count)
        )

    @property
    def sample_ids(self) -> tuple[int, ...]:
        """Return the identity index of each aligned sample."""
        return tuple(sample.index for sample in self.samples)

    @property
    def missing_mask(self) -> tuple[dict[str, bool], ...]:
        """
        Return the missing mask for each sample.

        The keys include ``when``, ``coord``, ``angles``, and every extended
        measurement column; ``True`` marks a missing column.
        """
        return tuple(sample.mask for sample in self.samples)

    @property
    def mask(self) -> tuple[dict[str, bool], ...]:
        """Alias for :attr:`missing_mask`."""
        return self.missing_mask

    @property
    def measurements(self) -> dict[str, tuple[str | None, ...]]:
        """
        Return the aligned extended measurement columns.

        ``gx:SimpleArrayData`` values are positional, like ``when``/``coord``/
        ``angles``: a missing value stays at its index instead of pulling the
        following value forward.  Columns are padded at the end only.

        Returns
        -------
        Dict[str, Tuple[Optional[str], ...]]
            One tuple per measured column, each as long as the longest column.

        """
        array_data = _array_data(self.extended_data)
        if not array_data:
            return {}
        count = max(
            [len(self.track_items)] + [len(values) for _name, values in array_data],
        )
        return {
            name: tuple(values[i] if i < len(values) else None for i in range(count))
            for name, values in array_data
        }

    @property
    def measurement_mask(self) -> dict[str, tuple[bool, ...]]:
        """Return one missing mask per extended measurement column."""
        return {
            name: tuple(value is None for value in values)
            for name, values in self.measurements.items()
        }

    def __bool__(self) -> bool:
        """
        Check if the track has any track items.

        Returns
        -------
        bool
            True if the track has track items, False otherwise.

        """
        return bool(self.track_items)


registry.register(
    Track,
    item=RegistryItem(
        ns_ids=("gx", "kml", ""),
        classes=(AltitudeMode,),
        attr_name="altitude_mode",
        node_name="altitudeMode",
        get_kwarg=subelement_enum_kwarg,
        set_element=enum_subelement,
        default=AltitudeMode.clamp_to_ground,
    ),
)
registry.register(
    Track,
    item=RegistryItem(
        ns_ids=("kml", "gx", ""),
        classes=(KmlDateTime,),
        attr_name="whens",
        node_name="when",
        get_kwarg=datetime_subelement_list_kwarg,
        set_element=datetime_subelement_list,
    ),
)
registry.register(
    Track,
    item=RegistryItem(
        ns_ids=("gx", ""),
        classes=(tuple,),
        attr_name="coords",
        node_name="coord",
        get_kwarg=coords_subelement_list_kwarg,
        set_element=coords_subelement_list,
    ),
)
registry.register(
    Track,
    item=RegistryItem(
        ns_ids=("gx", ""),
        classes=(tuple,),
        attr_name="angles",
        node_name="angles",
        get_kwarg=coords_subelement_list_kwarg,
        set_element=coords_subelement_list,
        default=(0.0, 0.0, 0.0),
    ),
)

# To avoids circular imports,
# ExtendedData registration is performed in registry_setup.py.


def tracks_to_geometry(tracks: Iterable[Track]) -> geo.MultiLineString:
    """
    Convert a collection of tracks to a MultiLineString geometry.

    Every track keeps its own boundary: tracks are never joined or
    interpolated across their endpoints, even when their timestamps touch.

    Args:
    ----
        tracks : Iterable[Track]
            A collection of tracks.

    Returns:
    -------
        geo.MultiLineString
            A MultiLineString geometry representing the tracks.

    """
    lines: list[geo.LineString] = []
    for track in tracks:
        geometry = track.geometry
        if not geometry:
            continue
        if isinstance(geometry, geo.MultiLineString):
            lines.extend(geometry.geoms)
        else:
            lines.append(geometry)
    return geo.MultiLineString.from_linestrings(*lines)


class MultiTrack(_Geometry):
    """
    A MultiTrack is a collection of tracks.

    A multi-track element is used to combine multiple track elements into a single
    conceptual unit. For example, suppose you collect GPS data for a day's bike ride
    that includes several rest stops and a stop for lunch. Because of the interruptions
    in time, one bike ride might appear as four different tracks when the times and
    positions are plotted.
    Grouping these <gx:Track> elements into one <gx:MultiTrack> container causes them to
    be displayed in Google Earth as sections of a single path.
    When the icon reaches the end of one segment, it moves to the beginning of the next
    segment.
    The <gx:interpolate> element specifies whether to stop at the end of one track and
    jump immediately to the start of the next one, or to interpolate the missing values
    between the two tracks.
    """

    _default_nsid = config.GX
    tracks: list[Track]

    def __init__(
        self,
        *,
        ns: str | None = None,
        name_spaces: dict[str, str] | None = None,
        id: str | None = None,
        target_id: str | None = None,
        altitude_mode: AltitudeMode | None = None,
        tracks: Iterable[Track] | None = None,
        interpolate: bool | None = None,
        **kwargs: Any,
    ) -> None:
        """
        Initialize a GX object.

        Args:
        ----
            ns (Optional[str]): The namespace for the GX object.
            name_spaces (Optional[Dict[str, str]]): The dictionary of namespace prefixes
                and URIs.
            id (Optional[str]): The ID of the GX object.
            target_id (Optional[str]): The target ID of the GX object.
            altitude_mode (Optional[AltitudeMode]): The altitude mode of the GX object.
            geometry (Optional[geo.MultiLineString]): The geometry of the GX object.
            tracks (Optional[Iterable[Track]]): The tracks of the GX object.
            interpolate (Optional[bool]): The interpolate flag of the GX object.
            **kwargs (Any): Additional keyword arguments.

        Raises:
        ------
            ValueError: If both geometry and tracks are specified.

        """
        self.tracks = [t for t in tracks if t] if tracks else []
        self.interpolate = interpolate
        super().__init__(
            ns=ns,
            name_spaces=name_spaces,
            id=id,
            target_id=target_id,
            altitude_mode=altitude_mode,
            **kwargs,
        )

    def __repr__(self) -> str:
        """Create a string (c)representation for MultiTrack."""
        return (
            f"{self.__class__.__module__}.{self.__class__.__name__}("
            f"ns={self.ns!r}, "
            f"name_spaces={self.name_spaces!r}, "
            f"id={self.id!r}, "
            f"target_id={self.target_id!r}, "
            f"altitude_mode={self.altitude_mode}, "
            f"tracks={self.tracks!r}, "
            f"interpolate={self.interpolate!r}, "
            f"**{self._get_splat()!r},"
            ")"
        )

    @property
    def geometry(self) -> geo.MultiLineString | None:
        """
        Get the geometry of the gx object.

        Each track is a separate boundary.  Tracks are not merged or
        interpolated into one continuous run when their timestamps happen to
        be adjacent.

        Returns
        -------
            Optional[geo.MultiLineString]: The geometry of the gx object.

        """
        return tracks_to_geometry(self.tracks)

    @property
    def segments(self) -> tuple[tuple[TrackSample, ...], ...]:
        """
        Return the aligned samples, grouped by track segment.

        Each track keeps its own boundary; neighbouring tracks are never
        combined into one interpolatable run.
        """
        return tuple(track.samples for track in self.tracks)

    @property
    def samples(self) -> tuple[TrackSample, ...]:
        """Return all aligned samples, flattened across track segments."""
        return tuple(
            TrackSample(
                segment=segment_index,
                index=sample.index,
                when=sample.when,
                coord=sample.coord,
                angle=sample.angle,
                measurements=dict(sample.measurements),
            )
            for segment_index, segment in enumerate(self.segments)
            for sample in segment
        )

    @property
    def sample_ids(self) -> tuple[tuple[int, int], ...]:
        """Return the ``(segment, index)`` identity of each aligned sample."""
        return tuple(sample.identity for sample in self.samples)

    @property
    def missing_mask(self) -> tuple[dict[str, bool], ...]:
        """Return the per-sample missing mask across all track segments."""
        return tuple(sample.mask for sample in self.samples)

    @property
    def mask(self) -> tuple[dict[str, bool], ...]:
        """Alias for :attr:`missing_mask`."""
        return self.missing_mask

    def __bool__(self) -> bool:
        """
        Check if the object has any tracks.

        Returns
        -------
        bool
            True if the object has tracks, False otherwise.

        """
        return bool(self.tracks)


registry.register(
    MultiTrack,
    item=RegistryItem(
        ns_ids=("gx", "kml"),
        classes=(AltitudeMode,),
        attr_name="altitude_mode",
        node_name="altitudeMode",
        get_kwarg=subelement_enum_kwarg,
        set_element=enum_subelement,
        default=AltitudeMode.clamp_to_ground,
    ),
)
registry.register(
    MultiTrack,
    item=RegistryItem(
        ns_ids=("gx",),
        classes=(bool,),
        attr_name="interpolate",
        node_name="interpolate",
        get_kwarg=subelement_bool_kwarg,
        set_element=bool_subelement,
    ),
)
registry.register(
    MultiTrack,
    item=RegistryItem(
        ns_ids=("gx",),
        classes=(Track,),
        attr_name="tracks",
        node_name="gx:Track",
        get_kwarg=xml_subelement_list_kwarg,
        set_element=xml_subelement_list,
    ),
)
