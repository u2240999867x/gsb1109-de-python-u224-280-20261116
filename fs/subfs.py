"""Manage a directory in a *parent* filesystem.
"""

from __future__ import print_function, unicode_literals

import typing

import six

from .path import abspath, join, normpath, relpath
from .wrapfs import WrapFS

if typing.TYPE_CHECKING:
    from typing import Any, Text, Tuple

    from .base import FS  # noqa: F401


_F = typing.TypeVar("_F", bound="FS", covariant=True)


@six.python_2_unicode_compatible
class SubFS(WrapFS[_F], typing.Generic[_F]):
    """A sub-directory on a parent filesystem.

    A SubFS is a filesystem object that maps to a sub-directory of
    another filesystem. This is the object that is returned by
    `~fs.base.FS.opendir`.

    """

    #: A sub-directory borrows its parent by default. It never closes the
    #: parent, so the parent may be shared with other sub-directories.
    close_cascade = "self"

    def __init__(self, parent_fs, path, ownership="borrowed"):  # noqa: D107
        # type: (_F, Text, Any) -> None
        self._sub_dir = abspath(normpath(path))
        super(SubFS, self).__init__(parent_fs, ownership=ownership)

    @property
    def parent_fs(self):
        # type: () -> _F
        """The parent filesystem this sub-directory borrows."""
        return self._wrap_fs

    def __repr__(self):
        # type: () -> Text
        return "{}({!r}, {!r})".format(
            self.__class__.__name__, self._wrap_fs, self._sub_dir
        )

    def __str__(self):
        # type: () -> Text
        return "{parent}{dir}".format(parent=self._wrap_fs, dir=self._sub_dir)

    def delegate_fs(self):
        # type: () -> _F
        return self._wrap_fs

    def delegate_path(self, path):
        # type: (Text) -> Tuple[_F, Text]
        _path = join(self._sub_dir, relpath(normpath(path)))
        return self._wrap_fs, _path


class ClosingSubFS(SubFS[_F], typing.Generic[_F]):
    """A version of `SubFS` which closes its parent when closed.

    The parent is only closed if this sub-directory *owns* it and no other
    wrapper still holds a reference. A shared parent therefore survives
    closing one of its sub-directories.
    """

    close_cascade = "owned"

    def __init__(self, parent_fs, path, ownership="owned"):  # noqa: D107
        # type: (_F, Text, Any) -> None
        super(ClosingSubFS, self).__init__(
            parent_fs, path, ownership=ownership
        )
