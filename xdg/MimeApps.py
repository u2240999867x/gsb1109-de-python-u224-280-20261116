"""MIME application associations (``mimeapps.list``).

The implementation is part of :mod:`xdg.Mime`, because it depends on the
shared MIME database for aliases and parent types.  This module provides a
convenient import location for the application-association API.
"""

from xdg.Mime import (
    Application,
    ApplicationCandidate,
    Applications,
    MimeApps,
    get_all_applications,
    get_all_associations,
    get_applications,
    get_applications_for_file,
    get_associations,
    get_default_application,
    get_default_application_for_file,
    get_optional_applications,
)

__all__ = [
    'Application',
    'ApplicationCandidate',
    'Applications',
    'MimeApps',
    'get_all_applications',
    'get_all_associations',
    'get_applications',
    'get_applications_for_file',
    'get_associations',
    'get_default_application',
    'get_default_application_for_file',
    'get_optional_applications',
]
