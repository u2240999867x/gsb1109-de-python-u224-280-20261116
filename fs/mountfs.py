"""Manage other filesystems as a folder hierarchy.
"""

from __future__ import absolute_import, print_function, unicode_literals

import typing

from six import text_type

from . import errors
from .base import FS
from .memoryfs import MemoryFS
from .mode import validate_open_mode, validate_openbin_mode
from .ownership import Ownership, parse_ownership, parse_policy
from .path import abspath, forcedir, normpath

if typing.TYPE_CHECKING:
    from typing import (
        IO,
        Any,
        BinaryIO,
        Collection,
        Dict,
        Iterator,
        List,
        MutableSequence,
        Optional,
        Text,
        Tuple,
        Union,
    )

    from .enums import ResourceType
    from .info import Info, RawInfo
    from .permissions import Permissions
    from .subfs import SubFS

    M = typing.TypeVar("M", bound="MountFS")


class MountError(Exception):
    """Thrown when mounts conflict."""


def _ownership_from_auto_close(auto_close):
    # type: (Any) -> Ownership
    """Translate the historical ``auto_close`` flag to an ownership role."""
    if isinstance(auto_close, bool):
        return Ownership.OWNED if auto_close else Ownership.BORROWED
    return parse_ownership(auto_close)


class MountFS(FS):
    """A virtual filesystem that maps directories on to other file-systems."""

    _meta = {
        "virtual": True,
        "read_only": False,
        "unicode_paths": True,
        "case_insensitive": False,
        "invalid_path_chars": "\0",
    }

    close_cascade = "owned"

    def __init__(
        self,
        auto_close=True,  # type: Any
        ownership=None,  # type: Any
        close_policy="abort",  # type: Any
    ):
        # type: (...) -> None
        """Create a new `MountFS` instance.

        Arguments:
            auto_close (bool or str): If `True` (the default), mounted
                children are owned by this filesystem and are closed when
                `MountFS` is closed *and no other wrapper still holds
                them*. If `False`, mounted children are borrowed and are
                never closed by this filesystem. The strings ``"owned"``
                and ``"borrowed"`` are accepted as explicit aliases.
            ownership (str, optional): The default ownership role for
                mounts, if ``auto_close`` is not already explicit.
            close_policy (str): How close treats open handles and admitted
                operations: ``"drain"``, ``"abort"`` or ``"defer"``.

        """
        super(MountFS, self).__init__()
        self._close_policy = parse_policy(close_policy)
        self._default_ownership = (
            parse_ownership(ownership)
            if ownership is not None
            else _ownership_from_auto_close(auto_close)
        )
        self.auto_close = self._default_ownership is Ownership.OWNED
        self._closed = False
        self.default_fs = MemoryFS()  # type: FS
        self.mounts = []  # type: MutableSequence[Tuple[Text, FS]]
        self._mount_holds = {}  # type: Dict[int, Any]
        self._default_hold = self.own(
            self.default_fs, label="default", share=True
        )

    def __repr__(self):
        # type: () -> str
        return "MountFS(auto_close={!r})".format(self.auto_close)

    def __str__(self):
        # type: () -> str
        return "<mountfs>"

    def __enter__(self):
        # type: -> M
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        # type: (Any, Any, Any) -> None
        self.close()

    def mount(self, path, fs, ownership=None, auto_close=None):
        # type: (Text, Union[FS, Text], Any, Any) -> None
        """Mounts a host FS object on a given path.

        Arguments:
            path (str): A path within the MountFS.
            fs (FS or str): A filesystem (instance or URL) to mount.
            ownership (str, optional): ``"owned"`` (this mount is
                responsible for closing ``fs`` once no other wrapper needs
                it) or ``"borrowed"`` (this mount never closes ``fs``).
                Defaults to the value configured on the `MountFS`.
            auto_close (bool, optional): Legacy alias for ``ownership``.

        """
        if isinstance(fs, text_type):
            from .opener import open_fs

            fs = open_fs(fs)

        if not isinstance(fs, FS):
            raise TypeError("fs argument must be an FS object or a FS URL")

        self.check()
        if fs is self:
            raise ValueError("Unable to mount self")
        _path = forcedir(abspath(normpath(path)))

        for mount_path, _ in self.mounts:
            if _path.startswith(mount_path):
                raise MountError("mount point overlaps existing mount")

        if auto_close is not None:
            role = _ownership_from_auto_close(auto_close)
        elif ownership is not None:
            role = parse_ownership(ownership)
        else:
            role = self._default_ownership

        hold = self.hold(fs, role, label=_path, share=True)
        self.mounts.append((_path, fs))
        self._mount_holds[id(fs)] = hold
        self.default_fs.makedirs(_path, recreate=True)

    def close(self, policy=None, timeout=None):
        # type: (Any, Optional[float]) -> Any
        """Close the mount and release only the references it holds."""
        result = super(MountFS, self).close(policy=policy, timeout=timeout)
        if result is not None and result.complete:
            for hold in list(self._held):
                if hold.active:
                    self.release_hold(hold=hold)
            self._mount_holds.clear()
            del self.mounts[:]
        return result

    def _finalize_close(self, report):
        # type: (Any) -> None
        super(MountFS, self)._finalize_close(report)

    def _delegate(self, path):
        # type: (Text) -> Tuple[FS, Text]
        """Get the delegate FS for a given path.

        Arguments:
            path (str): A path.

        Returns:
            (FS, str): a tuple of ``(<fs>, <path>)`` for a mounted filesystem,
            or ``(None, None)`` if no filesystem is mounted on the
            given ``path``.

        """
        _path = forcedir(abspath(normpath(path)))
        is_mounted = _path.startswith

        for mount_path, fs in self.mounts:
            if is_mounted(mount_path):
                return fs, _path[len(mount_path) :].rstrip("/")

        return self.default_fs, path

    def desc(self, path):
        # type: (Text) -> Text
        if not self.exists(path):
            raise errors.ResourceNotFound(path)
        fs, delegate_path = self._delegate(path)
        if fs is self.default_fs:
            fs = self
        return "{path} on {fs}".format(fs=fs, path=delegate_path)

    def getinfo(self, path, namespaces=None):
        # type: (Text, Optional[Collection[Text]]) -> Info
        self.check()
        fs, _path = self._delegate(path)
        return fs.getinfo(_path, namespaces=namespaces)

    def listdir(self, path):
        # type: (Text) -> List[Text]
        self.check()
        fs, _path = self._delegate(path)
        return fs.listdir(_path)

    def makedir(self, path, permissions=None, recreate=False):
        # type: (Text, Optional[Permissions], bool) -> SubFS[FS]
        self.check()
        fs, _path = self._delegate(path)
        return fs.makedir(_path, permissions=permissions, recreate=recreate)

    def openbin(self, path, mode="r", buffering=-1, **kwargs):
        # type: (Text, Text, int, **Any) -> BinaryIO
        validate_openbin_mode(mode)
        self.check()
        with self._operation():
            fs, _path = self._delegate(path)
            bin_file = fs.openbin(_path, mode=mode, buffering=-1, **kwargs)
        return self._manage_handle(bin_file, path)

    def remove(self, path):
        # type: (Text) -> None
        self.check()
        fs, _path = self._delegate(path)
        return fs.remove(_path)

    def removedir(self, path):
        # type: (Text) -> None
        self.check()
        path = normpath(path)
        if path in ("", "/"):
            raise errors.RemoveRootError(path)
        fs, _path = self._delegate(path)
        return fs.removedir(_path)

    def readbytes(self, path):
        # type: (Text) -> bytes
        self.check()
        with self._operation():
            fs, _path = self._delegate(path)
            return fs.readbytes(_path)

    def download(self, path, file, chunk_size=None, **options):
        # type: (Text, BinaryIO, Optional[int], **Any) -> None
        self.check()
        with self._operation():
            fs, _path = self._delegate(path)
            return fs.download(_path, file, chunk_size=chunk_size, **options)

    def readtext(
        self,
        path,  # type: Text
        encoding=None,  # type: Optional[Text]
        errors=None,  # type: Optional[Text]
        newline="",  # type: Text
    ):
        # type: (...) -> Text
        self.check()
        with self._operation():
            fs, _path = self._delegate(path)
            return fs.readtext(
                _path, encoding=encoding, errors=errors, newline=newline
            )

    def scandir(
        self,
        path,  # type: Text
        namespaces=None,  # type: Optional[Collection[Text]]
        page=None,  # type: Optional[Tuple[int, int]]
    ):
        # type: (...) -> Iterator[Info]
        self.check()
        fs, _path = self._delegate(path)
        return self._iterate_operation(
            fs.scandir(_path, namespaces=namespaces, page=page)
        )

    def getsize(self, path):
        # type: (Text) -> int
        self.check()
        fs, _path = self._delegate(path)
        return fs.getsize(_path)

    def getsyspath(self, path):
        # type: (Text) -> Text
        self.check()
        fs, _path = self._delegate(path)
        return fs.getsyspath(_path)

    def gettype(self, path):
        # type: (Text) -> ResourceType
        self.check()
        fs, _path = self._delegate(path)
        return fs.gettype(_path)

    def geturl(self, path, purpose="download"):
        # type: (Text, Text) -> Text
        self.check()
        fs, _path = self._delegate(path)
        return fs.geturl(_path, purpose=purpose)

    def hasurl(self, path, purpose="download"):
        # type: (Text, Text) -> bool
        self.check()
        fs, _path = self._delegate(path)
        return fs.hasurl(_path, purpose=purpose)

    def isdir(self, path):
        # type: (Text) -> bool
        self.check()
        fs, _path = self._delegate(path)
        return fs.isdir(_path)

    def isfile(self, path):
        # type: (Text) -> bool
        self.check()
        fs, _path = self._delegate(path)
        return fs.isfile(_path)

    def setinfo(self, path, info):
        # type: (Text, RawInfo) -> None
        self.check()
        fs, _path = self._delegate(path)
        return fs.setinfo(_path, info)

    def validatepath(self, path):
        # type: (Text) -> Text
        self.check()
        fs, _path = self._delegate(path)
        fs.validatepath(_path)
        path = abspath(normpath(path))
        return path

    def open(
        self,
        path,  # type: Text
        mode="r",  # type: Text
        buffering=-1,  # type: int
        encoding=None,  # type: Optional[Text]
        errors=None,  # type: Optional[Text]
        newline="",  # type: Text
        **options  # type: Any
    ):
        # type: (...) -> IO
        validate_open_mode(mode)
        self.check()
        with self._operation():
            fs, _path = self._delegate(path)
            open_file = fs.open(
                _path,
                mode=mode,
                buffering=buffering,
                encoding=encoding,
                errors=errors,
                newline=newline,
                **options
            )
        return self._manage_handle(open_file, path)

    def upload(self, path, file, chunk_size=None, **options):
        # type: (Text, BinaryIO, Optional[int], **Any) -> None
        self.check()
        fs, _path = self._delegate(path)
        return fs.upload(_path, file, chunk_size=chunk_size, **options)

    def writebytes(self, path, contents):
        # type: (Text, bytes) -> None
        self.check()
        fs, _path = self._delegate(path)
        return fs.writebytes(_path, contents)

    def writetext(
        self,
        path,  # type: Text
        contents,  # type: Text
        encoding="utf-8",  # type: Text
        errors=None,  # type: Optional[Text]
        newline="",  # type: Text
    ):
        # type: (...) -> None
        fs, _path = self._delegate(path)
        return fs.writetext(
            _path, contents, encoding=encoding, errors=errors, newline=newline
        )
