# emacs: -*- mode: python-mode; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the NiBabel package for the
#   copyright and license terms.
#
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Context manager openers for various fileobject types"""

from __future__ import annotations

import io
import os
import typing as ty
from bz2 import BZ2File
from os.path import splitext

from ._compression import (
    gzip_open,
    zstd_open,
)

if ty.TYPE_CHECKING:
    from types import TracebackType

    from _typeshed import ReadableBuffer, WriteableBuffer

    from ._typing import Self

    OpenerDef = tuple[ty.Callable[..., io.IOBase], tuple[str, ...]]


class FileVersionError(RuntimeError):
    """The file on disk is not the version that was read earlier

    Raised when a file has been replaced or modified between reading one
    part of it (for example an image header) and reading another (for
    example the image data array), so the two reads cannot be guaranteed
    to belong to the same version of the file.
    """


class FileVersion(ty.NamedTuple):
    """Fingerprint identifying a particular version of a file

    Two fingerprints taken of the same file compare equal only if the file
    has not been replaced (same device and inode) or modified (same size
    and modification time) between the two observations.
    """

    dev: int
    ino: int
    size: int
    mtime_ns: int

    @classmethod
    def from_stat(cls, st: os.stat_result) -> FileVersion:
        return cls(st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)

    def matches(self, other: FileVersion) -> bool:
        """True if `other` is a fingerprint of the same file version"""
        if self.ino and other.ino:
            # Some filesystems report a zero inode; only compare inodes
            # when both observations have a usable one.
            return self == other
        return (self.size, self.mtime_ns) == (other.size, other.mtime_ns)


def _fstat_fobj(fobj: ty.Any) -> os.stat_result | None:
    """Stat an open file object, or return None if not possible"""
    try:
        return os.fstat(fobj.fileno())
    except (AttributeError, OSError, io.UnsupportedOperation):
        return None


def file_version(fileish: str | os.PathLike | io.IOBase) -> FileVersion | None:
    """Return version fingerprint for a path or open file object

    Parameters
    ----------
    fileish : str or path-like or file-like
        File to fingerprint.  Open file objects are fingerprinted with
        ``os.fstat`` on their file descriptor, so the fingerprint refers
        to the file the handle actually reads, even if the path it was
        opened from has since been replaced.

    Returns
    -------
    file_version : FileVersion or None
        Fingerprint of the file, or None if no fingerprint can be
        determined (for example for in-memory file objects, or paths that
        cannot be stat'ed).
    """
    if isinstance(fileish, (str, os.PathLike)):
        try:
            return FileVersion.from_stat(os.stat(fileish))
        except OSError:
            return None
    st = _fstat_fobj(fileish)
    return None if st is None else FileVersion.from_stat(st)


@ty.runtime_checkable
class Fileish(ty.Protocol):
    def read(self, size: int = -1, /) -> bytes: ...
    def write(self, b: bytes, /) -> int | None: ...


class Opener:
    r"""Class to accept, maybe open, and context-manage file-likes / filenames

    Provides context manager to close files that the constructor opened for
    you.

    Parameters
    ----------
    fileish : str or file-like
        if str, then open with suitable opening method. If file-like, accept as
        is
    \*args : positional arguments
        passed to opening method when `fileish` is str.  ``mode``, if not
        specified, is `rb`.  ``compresslevel``, if relevant, and not specified,
        is set from class variable ``default_compresslevel``. ``keep_open``, if
        relevant, and not specified, is ``False``.
    \*\*kwargs : keyword arguments
        passed to opening method when `fileish` is str.  Change of defaults as
        for \*args
    """

    gz_def = (gzip_open, ('mode', 'compresslevel', 'mtime', 'keep_open'))
    bz2_def = (BZ2File, ('mode', 'buffering', 'compresslevel'))
    zstd_def = (zstd_open, ('mode', 'level', 'option', 'zstd_dict'))
    compress_ext_map: dict[str | None, OpenerDef] = {
        '.gz': gz_def,
        '.bz2': bz2_def,
        '.zst': zstd_def,
        None: (open, ('mode', 'buffering')),  # default
    }
    #: default compression level when writing gz and bz2 files
    default_compresslevel = 1
    #: default compression level for zst files
    default_zst_level = {
        'rb': None,
        'r': None,
        'wb': 3,
        'w': 3,
    }
    #: whether to ignore case looking for compression extensions
    compress_ext_icase: bool = True

    fobj: io.IOBase

    def __init__(self, fileish: str | io.IOBase, *args, **kwargs):
        if isinstance(fileish, (io.IOBase, Fileish)):
            self.fobj = fileish
            self.me_opened = False
            self._name = getattr(fileish, 'name', None)
            return
        opener, arg_names = self._get_opener_argnames(fileish)
        # Get full arguments to check for optional parameters
        full_kwargs = {**kwargs, **dict(zip(arg_names, args))}
        # Set default mode
        if 'mode' not in full_kwargs:
            mode = 'rb'
            kwargs['mode'] = mode
        else:
            mode = full_kwargs['mode']
        # Default gz/bz2 compression level
        if 'compresslevel' in arg_names and 'compresslevel' not in kwargs:
            kwargs['compresslevel'] = self.default_compresslevel
        # Default zstd compression level
        if 'level' in arg_names and 'level' not in kwargs and 'option' not in kwargs:
            kwargs['level'] = self.default_zst_level[mode]
        # Default keep_open hint
        if 'keep_open' in arg_names:
            kwargs.setdefault('keep_open', False)
        # Clear keep_open hint if it is not relevant for the file type
        else:
            kwargs.pop('keep_open', None)
        self.fobj = opener(fileish, *args, **kwargs)
        self._name = fileish
        self.me_opened = True

    def _get_opener_argnames(self, fileish: str) -> OpenerDef:
        _, ext = splitext(fileish)
        if self.compress_ext_icase:
            ext = ext.lower()
            for key in self.compress_ext_map:
                if key is None:
                    continue
                if key.lower() == ext:
                    return self.compress_ext_map[key]
        elif ext in self.compress_ext_map:
            return self.compress_ext_map[ext]
        return self.compress_ext_map[None]

    @property
    def closed(self) -> bool:
        return self.fobj.closed

    @property
    def name(self) -> str | None:
        """Return ``self.fobj.name`` or self._name if not present

        self._name will be None if object was created with a fileobj, otherwise
        it will be the filename.
        """
        return self._name

    @property
    def mode(self) -> str:
        # Check and raise our own error for type narrowing purposes
        if hasattr(self.fobj, 'mode'):
            return self.fobj.mode
        raise AttributeError(f'{self.fobj.__class__.__name__} has no attribute "mode"')

    def fileno(self) -> int:
        return self.fobj.fileno()

    def file_version(self) -> FileVersion | None:
        """Return version fingerprint of the underlying file

        Prefers ``os.fstat`` on the open file handle, so the fingerprint
        refers to the file this opener actually reads, even if the path it
        was opened from has since been replaced.  Falls back to stat'ing
        the path for handles without a file descriptor.  Returns None if
        no fingerprint can be determined.
        """
        st = _fstat_fobj(self.fobj)
        if st is not None:
            return FileVersion.from_stat(st)
        if self._name is not None:
            return file_version(self._name)
        return None

    def read(self, size: int = -1, /) -> bytes:
        return self.fobj.read(size)

    def readinto(self, buffer: WriteableBuffer, /) -> int | None:
        # Check and raise our own error for type narrowing purposes
        if hasattr(self.fobj, 'readinto'):
            return self.fobj.readinto(buffer)
        raise AttributeError(f'{self.fobj.__class__.__name__} has no attribute "readinto"')

    def write(self, b: bytes, /) -> int | None:
        return self.fobj.write(b)

    def writelines(self, lines: ty.Iterable[ReadableBuffer], /) -> None:
        self.fobj.writelines(lines)

    def seek(self, pos: int, whence: int = 0, /) -> int:
        return self.fobj.seek(pos, whence)

    def tell(self, /) -> int:
        return self.fobj.tell()

    def close(self, /) -> None:
        return self.fobj.close()

    def __iter__(self) -> ty.Iterator[bytes]:
        return iter(self.fobj)

    def close_if_mine(self) -> None:
        """Close ``self.fobj`` iff we opened it in the constructor"""
        if self.me_opened:
            self.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close_if_mine()


class ImageOpener(Opener):
    """Opener-type class to collect extra compressed extensions

    A trivial sub-class of opener to which image classes can add extra
    extensions with custom openers, such as compressed openers.

    To add an extension, add a line to the class definition (not __init__):

        ImageOpener.compress_ext_map[ext] = func_def

    ``ext`` is a file extension beginning with '.' and should be included in
    the image class's ``valid_exts`` tuple.

    ``func_def`` is a `(function, (args,))` tuple, where `function accepts a
    filename as the first parameter, and `args` defines the other arguments
    that `function` accepts. These arguments must be any (unordered) subset of
    `mode`, `compresslevel`, and `buffering`.
    """

    # Add new extensions to this dictionary
    compress_ext_map = Opener.compress_ext_map.copy()
