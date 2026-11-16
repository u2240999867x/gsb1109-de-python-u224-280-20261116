# Copyright (C) 2023  Christian Ledermann
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
"""Exceptions for the fastkml package."""

__all__ = [
    "FastKMLError",
    "GeometryError",
    "KMLParseError",
    "KMLSchemaError",
    "KMLWriteError",
    "NetworkLinkError",
    "QueryParameterCollisionError",
    "UnresolvedTemplateVariablesError",
]


class FastKMLError(Exception):
    """Base class for all fastkml exceptions."""


class KMLParseError(FastKMLError):
    """Raised when there is an error parsing KML."""


class KMLWriteError(FastKMLError):
    """Raised when there is an error writing KML."""


class KMLSchemaError(FastKMLError):
    """Raised when there is an error with the KML Schema."""


# geometry and kml_coordinates are mutually exclusive
class GeometryError(FastKMLError):
    """Raised when there is an error with the geometry."""


class NetworkLinkError(FastKMLError):
    """Raised when a NetworkLink request URL cannot be built."""


class QueryParameterCollisionError(NetworkLinkError):
    """Raised when a rendered query parameter collides with an existing one."""


class UnresolvedTemplateVariablesError(NetworkLinkError):
    """Raised when a complete URL is required but template variables are unresolved."""
