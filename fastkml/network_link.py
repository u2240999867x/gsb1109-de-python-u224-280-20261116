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
"""
Preview request URLs for NetworkLink templates.

KML ``NetworkLink`` elements carry viewport parameters in the ``viewFormat``
and ``httpQuery`` fields of their ``Link``. Both fields are *templates*:
square bracket placeholders such as ``[bboxWest]`` or ``[clientName]`` are
replaced by the client before the request is sent. This module renders those
templates for a given viewport, camera and client so that a backend can
preview the complete URL a client would request.

The conventions for the template variables are defined in
:data:`BBOX_VARIABLES`, :data:`VIEW_VARIABLES` and :data:`CLIENT_VARIABLES`;
each rendered value keeps track of the source it was resolved from. A
request is only considered complete (and sendable) when every placeholder in
both templates could be resolved.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any
from urllib.parse import quote

from fastkml.enums import RelaxedEnum
from fastkml.exceptions import QueryParameterCollisionError
from fastkml.exceptions import UnresolvedTemplateVariablesError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fastkml.links import Link
    from fastkml.views import Camera
    from fastkml.views import LookAt

__all__ = [
    "BBOX_VARIABLES",
    "CLIENT_VARIABLES",
    "VIEW_VARIABLES",
    "Bbox",
    "ClientInfo",
    "CollisionPolicy",
    "NetworkLinkRequest",
    "RenderedTemplate",
    "Substitution",
    "ViewParameters",
    "build_request_url",
    "render_template",
]

BBOX_VARIABLES: dict[str, str] = {
    "bboxWest": "bbox.west",
    "bboxSouth": "bbox.south",
    "bboxEast": "bbox.east",
    "bboxNorth": "bbox.north",
}
"""Template variables resolved from the viewport bounding box."""

VIEW_VARIABLES: dict[str, str] = {
    "cameraLon": "view.camera_lon",
    "cameraLat": "view.camera_lat",
    "cameraAlt": "view.camera_alt",
    "lookatLon": "view.lookat_lon",
    "lookatLat": "view.lookat_lat",
    "lookatRange": "view.lookat_range",
    "lookatTilt": "view.lookat_tilt",
    "lookatHeading": "view.lookat_heading",
    "horizFov": "view.horiz_fov",
    "vertFov": "view.vert_fov",
    "horizPixels": "view.horiz_pixels",
    "vertPixels": "view.vert_pixels",
    "terrainEnabled": "view.terrain_enabled",
}
"""Template variables resolved from camera and view quantities."""

CLIENT_VARIABLES: dict[str, str] = {
    "clientName": "client.client_name",
    "clientVersion": "client.client_version",
    "kmlVersion": "client.kml_version",
    "language": "client.language",
}
"""Template variables resolved from client information."""

TEMPLATE_VARIABLES: dict[str, str] = {
    **BBOX_VARIABLES,
    **VIEW_VARIABLES,
    **CLIENT_VARIABLES,
}
"""All known template variables mapped to the source of their value."""

_PLACEHOLDER_RE = re.compile(r"\[([^\[\]]+)\]")


class CollisionPolicy(RelaxedEnum):
    """Policy for name collisions between existing and new query parameters."""

    keep_existing = "keep-existing"
    replace = "replace"
    error = "error"


@dataclass(frozen=True)
class Bbox:
    """
    Viewport bounding box.

    ``west`` and ``east`` are used exactly as given: when the viewport
    crosses the antimeridian, ``west`` is numerically greater than ``east``
    (for example ``west=170, east=-170``). The smaller longitude is never
    assumed to be the western boundary.
    """

    west: float
    south: float
    east: float
    north: float

    @property
    def crosses_antimeridian(self) -> bool:
        """Return True if the box spans the +/-180 degree meridian."""
        return self.west > self.east

    @property
    def longitude_span(self) -> float:
        """Return the covered longitude range in degrees, in [0, 360)."""
        return (self.east - self.west) % 360.0


@dataclass(frozen=True)
class ViewParameters:
    """
    Camera and view quantities used to resolve view template variables.

    Any quantity left as None keeps the corresponding template variable
    unresolved.
    """

    camera_lon: float | None = None
    camera_lat: float | None = None
    camera_alt: float | None = None
    lookat_lon: float | None = None
    lookat_lat: float | None = None
    lookat_range: float | None = None
    lookat_tilt: float | None = None
    lookat_heading: float | None = None
    horiz_fov: float | None = None
    vert_fov: float | None = None
    horiz_pixels: int | None = None
    vert_pixels: int | None = None
    terrain_enabled: bool | None = None

    @classmethod
    def from_camera(cls, camera: Camera, **kwargs: Any) -> ViewParameters:
        """Create view parameters from a fastkml ``Camera``."""
        return cls(
            camera_lon=camera.longitude,
            camera_lat=camera.latitude,
            camera_alt=camera.altitude,
            lookat_heading=camera.heading,
            lookat_tilt=camera.tilt,
            **kwargs,
        )

    @classmethod
    def from_lookat(cls, look_at: LookAt, **kwargs: Any) -> ViewParameters:
        """Create view parameters from a fastkml ``LookAt``."""
        return cls(
            lookat_lon=look_at.longitude,
            lookat_lat=look_at.latitude,
            lookat_range=look_at.range,
            lookat_tilt=look_at.tilt,
            lookat_heading=look_at.heading,
            **kwargs,
        )


@dataclass(frozen=True)
class ClientInfo:
    """Client fields used to resolve httpQuery template variables."""

    client_name: str | None = None
    client_version: str | None = None
    kml_version: str | None = None
    language: str | None = None


@dataclass(frozen=True)
class Substitution:
    """A single resolved template variable and the source of its value."""

    variable: str
    value: str
    source: str


@dataclass(frozen=True)
class RenderedTemplate:
    """
    A template with its placeholders resolved as far as possible.

    ``text`` still contains the ``[variable]`` placeholder for every
    variable listed in ``unresolved``.
    """

    template: str
    text: str
    substitutions: tuple[Substitution, ...]
    unresolved: tuple[str, ...]

    @property
    def is_complete(self) -> bool:
        """Return True if every placeholder in the template was resolved."""
        return not self.unresolved


@dataclass(frozen=True)
class NetworkLinkRequest:
    """
    The preview of a NetworkLink request.

    ``url`` is only set when both templates resolved completely; a request
    with unresolved variables never exposes a sendable URL. Use
    :meth:`require_url` if a complete URL is mandatory.
    """

    href: str
    view_format: RenderedTemplate | None
    http_query: RenderedTemplate | None
    url: str | None
    on_collision: CollisionPolicy

    @property
    def unresolved(self) -> tuple[str, ...]:
        """Return all unresolved template variables of both templates."""
        variables: list[str] = []
        for rendered in (self.view_format, self.http_query):
            if rendered is not None:
                variables.extend(rendered.unresolved)
        return tuple(variables)

    @property
    def is_complete(self) -> bool:
        """Return True if the request has a complete, sendable URL."""
        return self.url is not None

    @property
    def identity(self) -> str:
        """
        Return a stable identity for this request.

        The identity is derived from the templates and the substituted
        values, so the same template rendered for two different viewports
        yields two different identities.
        """
        payload = {
            "href": self.href,
            "view_format": _rendered_payload(self.view_format),
            "http_query": _rendered_payload(self.http_query),
        }
        serialized = json.dumps(payload, sort_keys=True)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def require_url(self) -> str:
        """
        Return the complete request URL.

        Raises
        ------
        UnresolvedTemplateVariablesError
            If any template variable is unresolved.

        """
        if self.url is None:
            msg = (
                "Cannot build a complete request URL, "
                f"unresolved variables: {', '.join(self.unresolved)}"
            )
            raise UnresolvedTemplateVariablesError(msg)
        return self.url

    @classmethod
    def from_link(
        cls,
        link: Link,
        *,
        bbox: Bbox | None = None,
        view: ViewParameters | None = None,
        client: ClientInfo | None = None,
        on_collision: CollisionPolicy = CollisionPolicy.keep_existing,
    ) -> NetworkLinkRequest:
        """Build a request preview from a fastkml ``Link`` (or ``Icon``)."""
        return build_request_url(
            href=link.href or "",
            view_format=link.view_format,
            http_query=link.http_query,
            bbox=bbox,
            view=view,
            client=client,
            on_collision=on_collision,
        )


def _rendered_payload(rendered: RenderedTemplate | None) -> dict[str, Any] | None:
    if rendered is None:
        return None
    return {
        "template": rendered.template,
        "substitutions": [
            {"variable": sub.variable, "value": sub.value, "source": sub.source}
            for sub in rendered.substitutions
        ],
        "unresolved": list(rendered.unresolved),
    }


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    return quote(str(value), safe="")


def _collect_values(
    bbox: Bbox | None,
    view: ViewParameters | None,
    client: ClientInfo | None,
) -> dict[str, tuple[Any, str]]:
    values: dict[str, tuple[Any, str]] = {}
    sources: Mapping[str, str]
    for source_obj, variables in (
        (bbox, BBOX_VARIABLES),
        (view, VIEW_VARIABLES),
        (client, CLIENT_VARIABLES),
    ):
        if source_obj is None:
            continue
        sources = variables
        for name, source in sources.items():
            attr = source.partition(".")[2]
            values[name] = (getattr(source_obj, attr), source)
    return values


def render_template(
    template: str,
    values: Mapping[str, tuple[Any, str]],
) -> RenderedTemplate:
    """
    Render a viewFormat or httpQuery template.

    Placeholders follow the ``[variable]`` convention; only variables
    present in ``values`` with a value other than None are substituted.
    Percent-encoded sequences in the template are literal text and are left
    untouched.
    """
    substitutions: list[Substitution] = []
    unresolved: list[str] = []

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        value, source = values.get(name, (None, ""))
        if value is None:
            unresolved.append(name)
            return match.group(0)
        text = _format_value(value)
        substitutions.append(
            Substitution(variable=name, value=text, source=source),
        )
        return text

    text = _PLACEHOLDER_RE.sub(_replace, template)
    return RenderedTemplate(
        template=template,
        text=text,
        substitutions=tuple(substitutions),
        unresolved=tuple(unresolved),
    )


def _split_href(href: str) -> tuple[str, str, str]:
    """Split a href into base, query and fragment without decoding anything."""
    base, fragment_sep, fragment = href.partition("#")
    base, _, query = base.partition("?")
    return base, query, f"#{fragment}" if fragment_sep else ""


def _query_items(query: str) -> list[tuple[str, str]]:
    """Split a query string into (name, raw item) pairs, keeping the encoding."""
    if not query:
        return []
    return [(item.partition("=")[0], item) for item in query.split("&")]


def _merge_items(
    items: list[tuple[str, str]],
    additional: list[tuple[str, str]],
    on_collision: CollisionPolicy,
) -> list[tuple[str, str]]:
    merged = list(items)
    for name, item in additional:
        if any(existing_name == name for existing_name, _ in merged):
            if on_collision is CollisionPolicy.keep_existing:
                continue
            if on_collision is CollisionPolicy.error:
                msg = f"Query parameter {name!r} is already present in the href"
                raise QueryParameterCollisionError(msg)
            merged = [
                (existing_name, existing_item)
                for existing_name, existing_item in merged
                if existing_name != name
            ]
        merged.append((name, item))
    return merged


def build_request_url(
    href: str,
    view_format: str | None = None,
    http_query: str | None = None,
    *,
    bbox: Bbox | None = None,
    view: ViewParameters | None = None,
    client: ClientInfo | None = None,
    on_collision: CollisionPolicy = CollisionPolicy.keep_existing,
) -> NetworkLinkRequest:
    """
    Build a preview of the full request URL for a NetworkLink.

    The ``view_format`` and ``http_query`` templates are rendered with the
    values from ``bbox``, ``view`` and ``client`` and appended to the query
    of ``href``. ``on_collision`` decides how name collisions between query
    parameters already present in ``href`` and parameters from the templates
    are handled. The href itself is never re-encoded, so already
    percent-encoded paths stay intact.
    """
    on_collision = CollisionPolicy(on_collision)
    values = _collect_values(bbox, view, client)
    rendered_view_format = render_template(view_format, values) if view_format else None
    rendered_http_query = render_template(http_query, values) if http_query else None
    url: str | None = None
    rendered = (
        rendered
        for rendered in (rendered_view_format, rendered_http_query)
        if rendered is not None
    )
    if all(template.is_complete for template in rendered):
        base, query, fragment = _split_href(href)
        items = _query_items(query)
        for template in (rendered_view_format, rendered_http_query):
            if template is not None and template.text:
                items = _merge_items(items, _query_items(template.text), on_collision)
        url = f"{base}?{'&'.join(item for _, item in items)}" if items else base
        url += fragment
    return NetworkLinkRequest(
        href=href,
        view_format=rendered_view_format,
        http_query=rendered_http_query,
        url=url,
        on_collision=on_collision,
    )
