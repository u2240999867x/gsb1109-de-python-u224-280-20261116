"""Stable, point-in-time mount views for collection-directory scans.

A collection directory may be composed of several filesystem mounts, and
any one of those mount points can be replaced while a scan is running.
A :class:`MountView` captures the *mount topology* at a single instant
and freezes it: every path resolved through the same view is interpreted
against that topology, even when the live mounts have since moved on.

The view records both the virtual path (inside the collection root) and
the real path (inside the delegate filesystem, plus the OS path when one
exists). Results gathered from an outdated view carry its generation id
and are rejected when they are about to be written back, so data from an
old view can never be poured into a newer one.

Nested mounts are interpreted using the longest valid prefix, and all
path normalization is confined to the authorized root of the view: a
back-reference can never resolve outside of it.
"""

from __future__ import absolute_import, print_function, unicode_literals

import threading
import typing
from collections import deque, namedtuple

from . import errors
from .info import Info
from .path import abspath, combine, forcedir, normpath

if typing.TYPE_CHECKING:
    from typing import (
        Callable,
        Collection,
        Iterable,
        Iterator,
        List,
        Optional,
        Sequence,
        Text,
        Tuple,
    )

    from .base import FS


__all__ = [
    "MountView",
    "MountViewManager",
    "ScanSession",
    "ScanResult",
    "MountMapping",
    "ResolvedPath",
    "MountViewError",
    "StaleMountView",
    "MountViewRootViolation",
]


class MountViewError(Exception):
    """Base class for errors raised by mount views."""


class StaleMountView(MountViewError):
    """A result originates from a view that is no longer current.

    Raised when data produced by an outdated view is about to be applied
    to a newer generation of the mount topology.
    """

    def __init__(self, view_id, current_id):
        # type: (int, int) -> None
        self.view_id = view_id
        self.current_id = current_id
        msg = (
            "mount view {!r} is stale (current generation is {!r}); "
            "results from the old view may not be written to the new view"
        ).format(view_id, current_id)
        super(StaleMountView, self).__init__(msg)


class MountViewRootViolation(MountViewError):
    """A normalized path escaped the authorized root of a mount view."""

    def __init__(self, path, root):
        # type: (Text, Text) -> None
        self.path = path
        self.root = root
        msg = "path {!r} normalizes outside of the authorized root {!r}".format(
            path, root
        )
        super(MountViewRootViolation, self).__init__(msg)


# A single frozen mount entry: virtual mount path, delegate FS, and the
# sequence number of the FS currently attached at that path (bumped on
# every replacement).
_MountEntry = namedtuple("_MountEntry", ["mount_path", "fs", "sequence"])


MountMapping = namedtuple(
    "MountMapping", ["view_id", "virtual_path", "real_path", "sys_path", "fs"]
)
"""Record of where a virtual mount point is backed by real storage."""


ResolvedPath = namedtuple(
    "ResolvedPath",
    [
        "view_id",
        "mount_path",
        "fs",
        "virtual_path",
        "real_path",
        "sys_path",
        "sequence",
    ],
)
"""Result of resolving a virtual path inside a :class:`MountView`."""


ScanResult = namedtuple(
    "ScanResult", ["view_id", "virtual_path", "real_path", "sys_path", "fs", "info"]
)
"""A single resource observed during a view-bound scan.

The result is stamped with the generation id of the view that produced
it; the manager refuses to commit any result whose stamp no longer
matches the live generation.
"""


class _Topology(object):
    """Mutable generation registry shared by all views of a collection.

    Views keep a frozen copy of the mount table but consult this object
    to discover whether a newer generation has been published.
    """

    def __init__(self, root, default_fs):
        # type: (Text, Optional[FS]) -> None
        self.root = forcedir(abspath(normpath(root)))
        self.default_fs = default_fs
        self.current_id = 0
        self._entries = {}  # type: dict
        self._lock = threading.RLock()

    def bump(self):
        # type: () -> int
        self.current_id += 1
        return self.current_id

    def add_entry(self, mount_path, fs):
        # type: (Text, FS) -> bool
        with self._lock:
            if mount_path in self._entries:
                return False
            self._entries[mount_path] = [fs, 0]
            self.bump()
            return True

    def replace_entry(self, mount_path, fs):
        # type: (Text, FS) -> Optional[int]
        with self._lock:
            entry = self._entries.get(mount_path)
            if entry is None:
                return None
            entry[0] = fs
            entry[1] += 1
            return self.bump()

    def remove_entry(self, mount_path):
        # type: (Text) -> bool
        with self._lock:
            if mount_path not in self._entries:
                return False
            del self._entries[mount_path]
            self.bump()
            return True

    def snapshot_entries(self):
        # type: () -> Tuple[_MountEntry, ...]
        with self._lock:
            return tuple(
                sorted(
                    (
                        _MountEntry(mount_path, fs, sequence)
                        for mount_path, (fs, sequence) in self._entries.items()
                    ),
                    key=lambda entry: len(entry.mount_path),
                    reverse=True,
                )
            )


class MountView(object):
    """An immutable snapshot of a mount topology.

    A view is bound to a generation id and to an authorized root. All
    paths resolved through the view are normalized inside that root and
    mapped to a delegate filesystem by longest valid mount-prefix match.
    Views are never mutated in place; replacing a mount publishes a new
    view with a new generation id.

    """

    def __init__(self, topology, view_id, mounts):
        # type: (_Topology, int, Sequence[_MountEntry]) -> None
        self._topology = topology
        self._root = topology.root
        # Longest prefix first, so nested mounts always win over their
        # parent mount points.
        self._mounts = tuple(
            sorted(mounts, key=lambda entry: len(entry.mount_path), reverse=True)
        )  # type: Tuple[_MountEntry, ...]
        self._view_id = view_id
        self._closed = False

    def __repr__(self):
        # type: () -> Text
        return "MountView(view_id={!r}, root={!r}, mounts={!r})".format(
            self._view_id, self.root, len(self._mounts)
        )

    @property
    def view_id(self):
        # type: () -> int
        """Generation id of the frozen topology."""
        return self._view_id

    @property
    def root(self):
        # type: () -> Text
        """Authorized root that path normalization may not cross."""
        return self._root.rstrip("/") or "/"

    @property
    def default_fs(self):
        # type: () -> Optional[FS]
        """Filesystem receiving paths outside every mount point."""
        return self._topology.default_fs

    @property
    def closed(self):
        # type: () -> bool
        """Whether the view has been explicitly released."""
        return self._closed

    @property
    def stale(self):
        # type: () -> bool
        """Whether a newer topology generation exists.

        A stale view remains usable for finishing an in-flight read
        scan (it holds its own frozen topology), but its results may no
        longer be committed to the live filesystem.
        """
        return self._topology.current_id != self._view_id

    def close(self):
        # type: () -> None
        """Release the view."""
        self._closed = True

    def normalize(self, path):
        # type: (Text) -> Text
        """Normalize ``path`` without leaving the authorized root.

        Arguments:
            path (str): A virtual path.

        Returns:
            str: The normalized absolute virtual path.

        Raises:
            fs.errors.FilesystemClosed: If the view has been closed.
            MountViewRootViolation: If the normalized path would fall
                outside the authorized root.
            fs.errors.IllegalBackReference: If back-references escape
                the filesystem root.

        """
        if self._closed:
            raise errors.FilesystemClosed()
        if isinstance(path, bytes):
            raise TypeError("paths must be unicode (not bytes)")
        normalized = abspath(normpath(path))
        if not forcedir(normalized).startswith(self._root):
            raise MountViewRootViolation(path, self.root)
        return normalized

    def _entry_for(self, virtual_path):
        # type: (Text) -> Optional[_MountEntry]
        candidate = forcedir(virtual_path)
        for entry in self._mounts:
            if candidate.startswith(entry.mount_path):
                return entry
        return None

    def resolve(self, path):
        # type: (Text) -> ResolvedPath
        """Resolve a virtual path against the frozen topology.

        Nested mounts are selected by longest valid prefix. The returned
        :class:`ResolvedPath` records both the virtual path and the real
        path inside the delegate filesystem.

        Arguments:
            path (str): A virtual path within the view.

        Returns:
            ResolvedPath: The frozen resolution of ``path``.

        Raises:
            fs.errors.FilesystemClosed: If the view has been closed.
            MountViewRootViolation: If the path escapes the root.

        """
        if self._closed:
            raise errors.FilesystemClosed()
        virtual_path = self.normalize(path)
        entry = self._entry_for(virtual_path)

        if entry is None:
            default_fs = self.default_fs
            return ResolvedPath(
                view_id=self._view_id,
                mount_path=None,
                fs=default_fs,
                virtual_path=virtual_path,
                real_path=virtual_path,
                sys_path=self._sys_path(default_fs, virtual_path),
                sequence=-1,
            )

        real_path = virtual_path[len(entry.mount_path) :].rstrip("/")
        return ResolvedPath(
            view_id=self._view_id,
            mount_path=entry.mount_path.rstrip("/") or "/",
            fs=entry.fs,
            virtual_path=virtual_path,
            real_path=real_path,
            sys_path=self._sys_path(entry.fs, real_path),
            sequence=entry.sequence,
        )

    @staticmethod
    def _sys_path(fs, real_path):
        # type: (Optional[FS], Text) -> Optional[Text]
        if fs is None:
            return None
        try:
            return fs.getsyspath(real_path)
        except (errors.NoSysPath, errors.FSError):
            return None

    def mappings(self):
        # type: () -> Iterator[MountMapping]
        """Yield a virtual/real path record for every mount in the view."""
        for entry in self._mounts:
            virtual_path = entry.mount_path.rstrip("/") or "/"
            yield MountMapping(
                view_id=self._view_id,
                virtual_path=virtual_path,
                real_path="",
                sys_path=self._sys_path(entry.fs, ""),
                fs=entry.fs,
            )

    def mount_paths(self):
        # type: () -> List[Text]
        """Return the virtual mount paths contained in the view."""
        return [entry.mount_path.rstrip("/") or "/" for entry in self._mounts]

    def child_mounts(self, virtual_dir):
        # type: (Text) -> List[Tuple[Text, Text]]
        """List mount points that are direct children of ``virtual_dir``.

        Returns a list of ``(name, mount_path)`` tuples for the mounts
        sitting immediately below the given directory. Mount points are
        virtual resources: they may not exist inside any single delegate
        filesystem, so the scan machinery uses this method to make them
        visible while walking the topology.

        """
        base = forcedir(virtual_dir)
        children = []  # type: List[Tuple[Text, Text]]
        for entry in self._mounts:
            mount_path = entry.mount_path
            if mount_path == base or not mount_path.startswith(base):
                continue
            remainder = mount_path[len(base) :].rstrip("/")
            if "/" not in remainder:
                children.append((remainder, mount_path.rstrip("/") or "/"))
        return children

    def ensure_current(self):
        # type: () -> None
        """Assert that no newer topology generation exists.

        Raises:
            StaleMountView: If the view has been superseded.

        """
        current_id = self._topology.current_id
        if current_id != self._view_id:
            raise StaleMountView(self._view_id, current_id)


class MountViewManager(object):
    """Coordinates mount replacements and the views used by scans.

    The manager owns the live mount topology. Mutating the topology
    (mounting, replacing or unmounting) publishes a new generation;
    snapshots taken before that stay valid for the scans that hold them
    but become *stale*, and their results are rejected at commit time.

    Arguments:
        root (str): Authorized root confining every view.
        default_fs (FS): Filesystem used for paths outside all mounts.

    """

    def __init__(self, root="/", default_fs=None):
        # type: (Text, Optional[FS]) -> None
        self._topology = _Topology(root, default_fs)

    @property
    def root(self):
        # type: () -> Text
        return self._topology.root.rstrip("/") or "/"

    @property
    def generation(self):
        # type: () -> int
        """Current topology generation id."""
        return self._topology.current_id

    def _key(self, path):
        # type: (Text) -> Text
        return forcedir(abspath(normpath(path)))

    def mount(self, path, fs):
        # type: (Text, FS) -> None
        """Attach ``fs`` at virtual ``path`` and publish a new generation.

        Nested mounts (a mount point beneath an existing one) are
        allowed; resolution always prefers the longest valid prefix.

        Raises:
            MountError: If a filesystem is already mounted at ``path``.

        """
        mount_path = self._key(path)
        if not self._topology.add_entry(mount_path, fs):
            from .mountfs import MountError

            raise MountError("a filesystem is already mounted at {}".format(path))

    def replace(self, path, fs):
        # type: (Text, FS) -> int
        """Replace the filesystem mounted at ``path``.

        The previous filesystem is deliberately not closed: scans that
        froze a view on the old generation may still be reading from it.
        The mount point's sequence number is bumped and a new generation
        is published.

        Returns:
            int: The new generation id.

        Raises:
            fs.errors.ResourceNotFound: If nothing is mounted at ``path``.

        """
        mount_path = self._key(path)
        new_generation = self._topology.replace_entry(mount_path, fs)
        if new_generation is None:
            raise errors.ResourceNotFound(path)
        return new_generation

    def unmount(self, path):
        # type: (Text) -> None
        """Remove the mount at ``path`` and publish a new generation."""
        mount_path = self._key(path)
        if not self._topology.remove_entry(mount_path):
            raise errors.ResourceNotFound(path)

    def snapshot(self):
        # type: () -> MountView
        """Freeze the current topology into a new :class:`MountView`."""
        return MountView(self._topology, self._topology.current_id,
                         self._topology.snapshot_entries())

    def begin_scan(self, path="/"):
        # type: (Text) -> ScanSession
        """Start a scan bound to a fresh, fixed mount view."""
        view = self.snapshot()
        return ScanSession(view, path)

    def accept(self, result):
        # type: (ScanResult) -> ScanResult
        """Return ``result`` only if its view is still current.

        Raises:
            StaleMountView: If the result comes from an old generation.

        """
        current_id = self._topology.current_id
        if result.view_id != current_id:
            raise StaleMountView(result.view_id, current_id)
        return result

    def commit(self, results):
        # type: (Iterable[ScanResult]) -> List[ScanResult]
        """Validate a batch of in-flight results before merging them.

        Every result must originate from the live generation. A single
        stale result aborts the whole batch so that old-view data can
        never be poured into a new view.

        Returns:
            list: The same results, known to be current.

        Raises:
            StaleMountView: If any result is from an outdated view.

        """
        committed = []  # type: List[ScanResult]
        for result in results:
            committed.append(self.accept(result))
        return committed


class ScanSession(object):
    """A directory scan pinned to one fixed :class:`MountView`.

    The session walks the *frozen* topology only. Even if a mount point
    is replaced mid-scan, the session keeps reading the delegate
    filesystems captured by its view, and every emitted result is
    stamped with the view generation.

    """

    def __init__(self, view, path="/"):
        # type: (MountView, Text) -> None
        self._view = view
        self._path = view.normalize(path)

    @property
    def view(self):
        # type: () -> MountView
        return self._view

    @property
    def stale(self):
        # type: () -> bool
        return self._view.stale

    def scan(self, namespaces=None, on_error=None):
        # type: (Optional[Collection[Text]], Optional[Callable[[Text, Exception], None]]) -> Iterator[ScanResult]  # noqa: E501
        """Yield :class:`ScanResult` objects for every resource reached.

        Directories are enumerated from the delegate filesystems frozen
        in the view. Mount points themselves are virtual and are injected
        from the view's mount table, so the whole collection tree is
        visible even when no single delegate contains them.

        Arguments:
            namespaces (list): Info namespaces requested from delegates.
            on_error (callable): Invoked with ``(virtual_path, error)``
                when a delegate directory cannot be read. When omitted,
                scan errors propagate as exceptions.

        """
        view = self._view
        queue = deque([self._path])

        while queue:
            virtual_dir = queue.popleft()
            resolved = view.resolve(virtual_dir)
            delegate_fs = resolved.fs
            infos = []  # type: List[Info]
            names = set()  # type: set

            # Virtual mount points take precedence over a same-named
            # resource reported by the delegate filesystem.
            for name, mount_path in view.child_mounts(virtual_dir):
                names.add(name)
                infos.append(
                    Info({"basic": {"name": name, "is_dir": True}})
                )

            if delegate_fs is not None:
                try:
                    delegate_infos = list(
                        delegate_fs.scandir(
                            resolved.real_path, namespaces=namespaces
                        )
                    )
                except errors.FSError as error:
                    if on_error is None:
                        raise
                    on_error(virtual_dir, error)
                    delegate_infos = []
                for info in delegate_infos:
                    if info.name in names:
                        continue
                    names.add(info.name)
                    infos.append(info)

            for info in infos:
                virtual_path = combine(virtual_dir, info.name)
                child = view.resolve(virtual_path)
                yield ScanResult(
                    view_id=view.view_id,
                    virtual_path=child.virtual_path,
                    real_path=child.real_path,
                    sys_path=child.sys_path,
                    fs=child.fs,
                    info=info,
                )
                if info.is_dir:
                    queue.append(virtual_path)

    def commit(self, manager, results):
        # type: (MountViewManager, Iterable[ScanResult]) -> List[ScanResult]
        """Attempt to commit this session's results via ``manager``."""
        return manager.commit(results)
