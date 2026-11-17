"""Exception classes thrown by filesystem operations.

Errors relating to the underlying filesystem are translated in
to one of the following exceptions.

All Exception classes are derived from `~fs.errors.FSError`
which may be used as a catch-all filesystem exception.

"""

from __future__ import print_function, unicode_literals

import typing

import functools
import six
from six import text_type

if typing.TYPE_CHECKING:
    from typing import Optional, Text


__all__ = [
    "BulkCopyFailed",
    "CloseDeferred",
    "CloseFailed",
    "CreateFailed",
    "DestinationExists",
    "DirectoryExists",
    "DirectoryExpected",
    "DirectoryNotEmpty",
    "FileExists",
    "FileExpected",
    "FilesystemClosed",
    "FSError",
    "IllegalBackReference",
    "IllegalDestination",
    "InsufficientStorage",
    "InvalidCharsInPath",
    "InvalidPath",
    "MissingInfoNamespace",
    "NoSysPath",
    "NoURL",
    "OperationFailed",
    "OperationTimeout",
    "PathError",
    "PatternError",
    "PermissionDenied",
    "RemoteConnectionError",
    "RemoveRootError",
    "ResourceError",
    "ResourceHeld",
    "ResourceInvalid",
    "ResourceLocked",
    "ResourceNotFound",
    "ResourceReadOnly",
    "Unsupported",
    "UnsupportedHash",
    "UnsafeSharing",
]


class MissingInfoNamespace(AttributeError):
    """An expected namespace is missing."""

    def __init__(self, namespace):  # noqa: D107
        # type: (Text) -> None
        self.namespace = namespace
        msg = "namespace '{}' is required for this attribute"
        super(MissingInfoNamespace, self).__init__(msg.format(namespace))

    def __reduce__(self):
        return type(self), (self.namespace,)


@six.python_2_unicode_compatible
class FSError(Exception):
    """Base exception for the `fs` module."""

    default_message = "Unspecified error"

    def __init__(self, msg=None):  # noqa: D107
        # type: (Optional[Text]) -> None
        self._msg = msg or self.default_message
        super(FSError, self).__init__()

    def __str__(self):
        # type: () -> Text
        """Return the error message."""
        msg = self._msg.format(**self.__dict__)
        return msg

    def __repr__(self):
        # type: () -> Text
        msg = self._msg.format(**self.__dict__)
        return "{}({!r})".format(self.__class__.__name__, msg)


class FilesystemClosed(FSError):
    """Attempt to use a closed filesystem."""

    default_message = "attempt to use closed filesystem"


class CloseDeferred(FSError):
    """A filesystem could not be closed while it was still in use.

    The filesystem has stopped admitting new operations, but it remains
    active until the admitted reads / open handles have drained. Calling
    `~fs.base.FS.close` again resumes the shutdown.
    """

    default_message = "filesystem close deferred: {reason}"

    def __init__(self, reason="still in use", report=None):  # noqa: D107
        # type: (str, object) -> None
        self.reason = reason
        self.report = report
        super(CloseDeferred, self).__init__()

    def __reduce__(self):
        return type(self), (self.reason, self.report)


class CloseFailed(FSError):
    """One or more resources could not be released during close."""

    default_message = "filesystem close failed: {details}"

    def __init__(self, details="", report=None):  # noqa: D107
        # type: (str, object) -> None
        self.details = details
        self.report = report
        super(CloseFailed, self).__init__()

    def __reduce__(self):
        return type(self), (self.details, self.report)


class ResourceHeld(FSError):
    """A resource is still held by another filesystem wrapper."""

    default_message = "resource {resource!r} is still held by {holders}"

    def __init__(self, resource, holders=()):  # noqa: D107
        # type: (object, object) -> None
        self.resource = resource
        self.holders = list(holders)
        super(ResourceHeld, self).__init__()

    def __str__(self):
        return self.default_message.format(
            resource=repr(self.resource),
            holders=", ".join(repr(holder) for holder in self.holders) or "unknown",
        )


class UnsafeSharing(FSError):
    """A wrapper cannot safely share a child with an opaque cascade."""

    default_message = (
        "cannot safely share {resource}: {holder} does not declare "
        "whether closing it cascades to its children"
    )

    def __init__(self, resource, holder=None, cascade=None):  # noqa: D107
        # type: (object, object, object) -> None
        self.resource = resource
        self.holder = holder
        self.cascade = cascade
        super(UnsafeSharing, self).__init__()

    def __str__(self):
        return self.default_message.format(
            resource=repr(self.resource), holder=repr(self.holder)
        )


class BulkCopyFailed(FSError):
    """A copy operation failed in worker threads."""

    default_message = "One or more copy operations failed (see errors attribute)"

    def __init__(self, errors):  # noqa: D107
        self.errors = errors
        super(BulkCopyFailed, self).__init__()


class CreateFailed(FSError):
    """Filesystem could not be created."""

    default_message = "unable to create filesystem, {details}"

    def __init__(self, msg=None, exc=None):  # noqa: D107
        # type: (Optional[Text], Optional[Exception]) -> None
        self._msg = msg or self.default_message
        self.details = "" if exc is None else text_type(exc)
        self.exc = exc

    @classmethod
    def catch_all(cls, func):
        @functools.wraps(func)
        def new_func(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except cls:
                raise
            except Exception as e:
                raise cls(exc=e)

        return new_func  # type: ignore

    def __reduce__(self):
        return type(self), (self._msg, self.exc)


class PathError(FSError):
    """Base exception for errors to do with a path string."""

    default_message = "path '{path}' is invalid"

    def __init__(self, path, msg=None, exc=None):  # noqa: D107
        # type: (Text, Optional[Text], Optional[Exception]) -> None
        self.path = path
        self.exc = exc
        super(PathError, self).__init__(msg=msg)

    def __reduce__(self):
        return type(self), (self.path, self._msg, self.exc)


class NoSysPath(PathError):
    """The filesystem does not provide *sys paths* to the resource."""

    default_message = "path '{path}' does not map to the local filesystem"


class NoURL(PathError):
    """The filesystem does not provide an URL for the resource."""

    default_message = "path '{path}' has no '{purpose}' URL"

    def __init__(self, path, purpose, msg=None):  # noqa: D107
        # type: (Text, Text, Optional[Text]) -> None
        self.purpose = purpose
        super(NoURL, self).__init__(path, msg=msg)

    def __reduce__(self):
        return type(self), (self.path, self.purpose, self._msg)


class InvalidPath(PathError):
    """Path can't be mapped on to the underlaying filesystem."""

    default_message = "path '{path}' is invalid on this filesystem "


class InvalidCharsInPath(InvalidPath):
    """Path contains characters that are invalid on this filesystem."""

    default_message = "path '{path}' contains invalid characters"


class OperationFailed(FSError):
    """A specific operation failed."""

    default_message = "operation failed, {details}"

    def __init__(
        self,
        path=None,  # type: Optional[Text]
        exc=None,  # type: Optional[Exception]
        msg=None,  # type: Optional[Text]
    ):  # noqa: D107
        # type: (...) -> None
        self.path = path
        self.exc = exc
        self.details = "" if exc is None else text_type(exc)
        self.errno = getattr(exc, "errno", None)
        super(OperationFailed, self).__init__(msg=msg)

    def __reduce__(self):
        return type(self), (self.path, self.exc, self._msg)


class Unsupported(OperationFailed):
    """Operation not supported by the filesystem."""

    default_message = "not supported"


class RemoteConnectionError(OperationFailed):
    """Operations encountered remote connection trouble."""

    default_message = "remote connection error"


class InsufficientStorage(OperationFailed):
    """Storage is insufficient for requested operation."""

    default_message = "insufficient storage space"


class PermissionDenied(OperationFailed):
    """Not enough permissions."""

    default_message = "permission denied"


class OperationTimeout(OperationFailed):
    """Filesystem took too long."""

    default_message = "operation timed out"


class RemoveRootError(OperationFailed):
    """Attempt to remove the root directory."""

    default_message = "root directory may not be removed"


class IllegalDestination(OperationFailed):
    """The given destination cannot be used for the operation.

    This error will occur when attempting to move / copy a folder into itself or copying
    a file onto itself.
    """

    default_message = "'{path}' is not a legal destination"


class ResourceError(FSError):
    """Base exception class for error associated with a specific resource."""

    default_message = "failed on path {path}"

    def __init__(self, path, exc=None, msg=None):  # noqa: D107
        # type: (Text, Optional[Exception], Optional[Text]) -> None
        self.path = path
        self.exc = exc
        super(ResourceError, self).__init__(msg=msg)

    def __reduce__(self):
        return type(self), (self.path, self.exc, self._msg)


class ResourceNotFound(ResourceError):
    """Required resource not found."""

    default_message = "resource '{path}' not found"


class ResourceInvalid(ResourceError):
    """Resource has the wrong type."""

    default_message = "resource '{path}' is invalid for this operation"


class FileExists(ResourceError):
    """File already exists."""

    default_message = "resource '{path}' exists"


class FileExpected(ResourceInvalid):
    """Operation only works on files."""

    default_message = "path '{path}' should be a file"


class DirectoryExpected(ResourceInvalid):
    """Operation only works on directories."""

    default_message = "path '{path}' should be a directory"


class DestinationExists(ResourceError):
    """Target destination already exists."""

    default_message = "destination '{path}' exists"


class DirectoryExists(ResourceError):
    """Directory already exists."""

    default_message = "directory '{path}' exists"


class DirectoryNotEmpty(ResourceError):
    """Attempt to remove a non-empty directory."""

    default_message = "directory '{path}' is not empty"


class ResourceLocked(ResourceError):
    """Attempt to use a locked resource."""

    default_message = "resource '{path}' is locked"


class ResourceReadOnly(ResourceError):
    """Attempting to modify a read-only resource."""

    default_message = "resource '{path}' is read only"


class IllegalBackReference(ValueError):
    """Too many backrefs exist in a path.

    This error will occur if the back references in a path would be
    outside of the root. For example, ``"/foo/../../"``, contains two back
    references which would reference a directory above the root.

    Note:
        This exception is a subclass of `ValueError` as it is not
        strictly speaking an issue with a filesystem or resource.

    """

    def __init__(self, path):  # noqa: D107
        # type: (Text) -> None
        self.path = path
        msg = ("path '{path}' contains back-references outside of filesystem").format(
            path=path
        )
        super(IllegalBackReference, self).__init__(msg)

    def __reduce__(self):
        return type(self), (self.path,)


class UnsupportedHash(ValueError):
    """The requested hash algorithm is not supported.

    This exception will be thrown if a hash algorithm is requested that is
    not supported by hashlib.

    """


class PatternError(ValueError):
    """A string pattern with invalid syntax was given."""

    default_message = "pattern '{pattern}' is invalid at position {position}"

    def __init__(self, pattern, position, exc=None, msg=None):  # noqa: D107
        # type: (Text, int, Optional[Exception], Optional[Text]) -> None
        self.pattern = pattern
        self.position = position
        self.exc = exc
        super(ValueError, self).__init__()

    def __reduce__(self):
        return type(self), (self.path, self.position, self.exc, self._msg)
