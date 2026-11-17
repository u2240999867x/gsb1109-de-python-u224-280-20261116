"""Manage several filesystems through a single view.
"""

from __future__ import absolute_import, print_function, unicode_literals

import hashlib
import typing

from collections import OrderedDict, namedtuple
from operator import itemgetter
from six import text_type

from . import errors
from .base import FS
from .enums import DeletionPolicy
from .mode import Mode, check_writable
from .opener import open_fs
from .path import abspath, join, normpath, recursepath

if typing.TYPE_CHECKING:
    from typing import (
        IO,
        Any,
        BinaryIO,
        Collection,
        Iterator,
        List,
        MutableMapping,
        MutableSet,
        Optional,
        Set,
        Text,
        Tuple,
        Union,
    )

    from .enums import ResourceType
    from .info import Info, RawInfo
    from .permissions import Permissions
    from .subfs import SubFS

    M = typing.TypeVar("M", bound="MultiFS")


_PrioritizedFS = namedtuple("_PrioritizedFS", ["priority", "fs"])


class WritePlan(
    namedtuple(
        "_WritePlan",
        [
            "path",
            "mode",
            "target",
            "target_name",
            "source",
            "source_name",
            "copy_up",
            "deletion_policy",
        ],
    )
):
    """Describe where a write on a `MultiFS` would be applied.

    A write plan is returned by `MultiFS.write_plan` and makes the write
    target explicit.  Reads may be served by any layer, but writes always
    go to the single writable filesystem configured with
    ``add_fs(..., write=True)``; no write is ever applied to a read-only
    layer.

    """

    __slots__ = ()

    @property
    def backend(self):
        # type: () -> Optional[FS]
        """FS: the backend that would receive the write, if any."""
        return self.target

    @property
    def backend_name(self):
        # type: () -> Optional[Text]
        """str: the name of the backend that would receive the write."""
        return self.target_name

    @property
    def fs(self):
        # type: () -> Optional[FS]
        """FS: alias for `target`."""
        return self.target

    @property
    def name(self):
        # type: () -> Optional[Text]
        """str: alias for `target_name`."""
        return self.target_name

    @property
    def writable(self):
        # type: () -> bool
        """bool: `True` if the plan has a write backend."""
        return self.target is not None

    @property
    def read_only(self):
        # type: () -> bool
        """bool: `True` if the plan has no write backend."""
        return self.target is None

    @property
    def visible(self):
        # type: () -> bool
        """bool: `True` if a resource is currently visible at ``path``."""
        return self.source is not None

    @property
    def overrides(self):
        # type: () -> bool
        """bool: `True` if the write replaces a lower-layer resource."""
        return self.source is not None and self.source is not self.target

    def to_dict(self):
        # type: () -> MutableMapping[Text, Any]
        """Get the plan as a dictionary."""
        return {
            "path": self.path,
            "mode": self.mode,
            "target": self.target,
            "target_name": self.target_name,
            "source": self.source,
            "source_name": self.source_name,
            "copy_up": self.copy_up,
            "deletion_policy": self.deletion_policy,
        }

    def describe(self):
        # type: () -> Text
        """Get a human-readable description of the write target."""
        if self.target is None:
            if Mode(self.mode).writing:
                return "no write backend configured (filesystem is read-only)"
            return "mode '{}' does not write".format(self.mode)
        description = "write '{}' ({}) to backend '{}'".format(
            self.path, self.mode, self.target_name
        )
        if self.copy_up:
            return description + "; copy-up from '{}'".format(self.source_name)
        if self.overrides:
            return description + "; replaces '{}'".format(self.source_name)
        return description


class DeletePlan(
    namedtuple(
        "_DeletePlan",
        [
            "path",
            "policy",
            "action",
            "target",
            "target_name",
            "upper_fs",
            "upper_name",
            "lower_fs",
            "lower_name",
            "upper_exists",
            "lower_exists",
            "shadowed",
            "ancestor_shadowed",
        ],
    )
):
    """Describe what deleting a path from a `MultiFS` would do.

    A delete plan distinguishes between *retracting a correction* (the
    upper layer's override is removed, so the lower layer may become
    visible again) and *shadowing the base* (a tombstone is written to
    the upper layer, so the lower layer stays hidden).  The lower layer
    is never physically modified.

    """

    __slots__ = ()

    @property
    def backend(self):
        # type: () -> Optional[FS]
        """FS: the writable backend used to remove an override."""
        return self.target

    @property
    def backend_name(self):
        # type: () -> Optional[Text]
        """str: the name of the writable backend."""
        return self.target_name

    @property
    def fs(self):
        # type: () -> Optional[FS]
        """FS: alias for `target`."""
        return self.target

    @property
    def name(self):
        # type: () -> Optional[Text]
        """str: alias for `target_name`."""
        return self.target_name

    @property
    def retracts(self):
        # type: () -> bool
        """bool: `True` if an upper-layer correction is retracted."""
        return self.action == "retract-correction"

    @property
    def is_retract(self):
        # type: () -> bool
        """bool: alias for `retracts`."""
        return self.retracts

    @property
    def shadows(self):
        # type: () -> bool
        """bool: `True` if the lower-layer resource is shadowed."""
        return self.action == "shadow-base"

    @property
    def is_shadow(self):
        # type: () -> bool
        """bool: alias for `shadows`."""
        return self.shadows

    @property
    def reveals_base(self):
        # type: () -> bool
        """bool: `True` if the lower-layer resource becomes visible."""
        return self.retracts and self.lower_exists and not self.ancestor_shadowed

    @property
    def reveals_lower(self):
        # type: () -> bool
        """bool: alias for `reveals_base`."""
        return self.reveals_base

    @property
    def lower_untouched(self):
        # type: () -> bool
        """bool: `True` because the lower layer is never modified."""
        return True

    @property
    def physically_deletes(self):
        # type: () -> bool
        """bool: `True` if an upper-layer resource is physically removed."""
        return self.target is not None and self.upper_exists and self.action in (
            "retract-correction",
            "shadow-base",
            "delete-upper",
        )

    def to_dict(self):
        # type: () -> MutableMapping[Text, Any]
        """Get the plan as a dictionary."""
        return {
            "path": self.path,
            "policy": self.policy,
            "action": self.action,
            "target": self.target,
            "target_name": self.target_name,
            "upper_fs": self.upper_fs,
            "upper_name": self.upper_name,
            "lower_fs": self.lower_fs,
            "lower_name": self.lower_name,
            "upper_exists": self.upper_exists,
            "lower_exists": self.lower_exists,
            "shadowed": self.shadowed,
            "ancestor_shadowed": self.ancestor_shadowed,
        }

    def describe(self):
        # type: () -> Text
        """Get a human-readable description of the deletion."""
        if self.action == "not-found":
            return "'{}' is not visible".format(self.path)
        if self.action == "delete-upper":
            return (
                "remove upper-layer '{}' from backend '{}'; "
                "no lower-layer resource"
            ).format(self.path, self.target_name)
        if self.action == "retract-correction":
            if self.reveals_base:
                return (
                    "retract correction '{}' from backend '{}'; "
                    "lower-layer '{}' becomes visible again"
                ).format(self.path, self.target_name, self.lower_name)
            return (
                "retract correction '{}' from backend '{}'; "
                "no lower-layer resource to reveal"
            ).format(self.path, self.target_name)
        if self.action == "shadow-base":
            if self.lower_exists and self.upper_exists:
                return (
                    "remove upper-layer '{}' from backend '{}' and shadow "
                    "lower-layer '{}'; the lower layer is left untouched"
                ).format(self.path, self.target_name, self.lower_name)
            if self.lower_exists:
                return (
                    "shadow lower-layer '{}' in backend '{}'; "
                    "the lower layer is left untouched"
                ).format(self.path, self.target_name)
            return "remove upper-layer '{}'; no lower-layer resource".format(self.path)
        return "{} '{}'".format(self.action, self.path)


DeletePolicy = DeletionPolicy

_POLICY_ALIASES = {
    "retract": DeletionPolicy.retract,
    "reveal": DeletionPolicy.retract,
    "revert": DeletionPolicy.retract,
    "undo": DeletionPolicy.retract,
    "uncover": DeletionPolicy.retract,
    "show-base": DeletionPolicy.retract,
    "reveal-base": DeletionPolicy.retract,
    "shadow": DeletionPolicy.shadow,
    "tombstone": DeletionPolicy.shadow,
    "whiteout": DeletionPolicy.shadow,
    "mask": DeletionPolicy.shadow,
    "hide": DeletionPolicy.shadow,
    "hide-base": DeletionPolicy.shadow,
    "opaque": DeletionPolicy.shadow,
}

_TOMBSTONE_DIR = ".multifs"
_TOMBSTONE_SUBDIR = "shadows"


def _normalize_deletion_policy(policy):
    # type: (Union[DeletionPolicy, Text, None]) -> DeletionPolicy
    """Coerce a deletion policy value to a `DeletionPolicy`."""
    if policy is None:
        return DeletionPolicy.retract
    if isinstance(policy, DeletionPolicy):
        return policy
    try:
        key = policy.lower()
    except AttributeError:
        raise ValueError("invalid deletion policy {!r}".format(policy))
    try:
        return _POLICY_ALIASES[key]
    except KeyError:
        raise ValueError("invalid deletion policy {!r}".format(policy))


class MultiFS(FS):
    """A filesystem that delegates to a sequence of other filesystems.

    Operations on the MultiFS will try each 'child' filesystem in order,
    until it succeeds. In effect, creating a filesystem that combines
    the files and dirs of its children.

    Writes are always sent to the single writable filesystem configured
    with ``add_fs(..., write=True)``.  This allows a read-only base layer
    to be combined with a writable corrections layer, without ever
    modifying the base.

    Deleting a resource follows the `DeletionPolicy` configured on the
    filesystem.  Removing an upper-layer correction may either *retract*
    the correction (revealing the base resource, if any) or *shadow* the
    base with a tombstone in the writable layer.  A read-only base
    resource is never physically deleted.

    """

    _meta = {"virtual": True, "read_only": False, "case_insensitive": False}

    def __init__(self, auto_close=True, deletion_policy=DeletionPolicy.retract):
        # type: (bool, Union[DeletionPolicy, Text]) -> None
        """Create a new MultiFS.

        Arguments:
            auto_close (bool): If `True` (the default), the child
                filesystems will be closed when `MultiFS` is closed.
            deletion_policy (~fs.enums.DeletionPolicy or str): How
                deletions treat an upper-layer correction.  Use
                ``"retract"`` to remove the correction and reveal the
                lower layer, or ``"shadow"`` to keep the lower layer
                hidden with a tombstone.  Defaults to ``"retract"``.

        """
        super(MultiFS, self).__init__()

        self._auto_close = auto_close
        self.write_fs = None  # type: Optional[FS]
        self._write_fs_name = None  # type: Optional[Text]
        self._sort_index = 0
        self._filesystems = {}  # type: MutableMapping[Text, _PrioritizedFS]
        self._fs_sequence = None  # type: Optional[List[Tuple[Text, FS]]]
        self._shadows = set()  # type: Set[Text]
        self._shadows_write_fs = None  # type: Optional[FS]
        self._deletion_policy = _normalize_deletion_policy(deletion_policy)
        self._closed = False

    def __repr__(self):
        # type: () -> Text
        if self._auto_close:
            return "MultiFS()"
        else:
            return "MultiFS(auto_close=False)"

    def __str__(self):
        # type: () -> Text
        return "<multifs>"

    @property
    def deletion_policy(self):
        # type: () -> DeletionPolicy
        """`~fs.enums.DeletionPolicy`: policy used by deletion methods."""
        return self._deletion_policy

    @deletion_policy.setter
    def deletion_policy(self, policy):
        # type: (Union[DeletionPolicy, Text]) -> None
        self._deletion_policy = _normalize_deletion_policy(policy)

    @property
    def write_fs_name(self):
        # type: () -> Optional[Text]
        """str: name of the writable backend, if one is configured."""
        return self._write_fs_name

    @property
    def write_backend(self):
        # type: () -> Optional[FS]
        """FS: the writable backend, if one is configured."""
        return self.write_fs

    @property
    def default_deletion_policy(self):
        # type: () -> DeletionPolicy
        """`~fs.enums.DeletionPolicy`: alias for `deletion_policy`."""
        return self._deletion_policy

    @default_deletion_policy.setter
    def default_deletion_policy(self, policy):
        # type: (Union[DeletionPolicy, Text]) -> None
        self.deletion_policy = policy

    def add_fs(self, name, fs, write=False, priority=0):
        # type: (Text, FS, bool, int) -> None
        """Add a filesystem to the MultiFS.

        Arguments:
            name (str): A unique name to refer to the filesystem being
                added.
            fs (FS or str): The filesystem (instance or URL) to add.
            write (bool): If this value is True, then the ``fs`` will
                be used as the writeable FS (defaults to False).
            priority (int): An integer that denotes the priority of the
                filesystem being added. Filesystems will be searched in
                descending priority order and then by the reverse order
                they were added. So by default, the most recently added
                filesystem will be looked at first.

        """
        if isinstance(fs, text_type):
            fs = open_fs(fs)

        if not isinstance(fs, FS):
            raise TypeError("fs argument should be an FS object or FS URL")

        self._filesystems[name] = _PrioritizedFS(
            priority=(priority, self._sort_index), fs=fs
        )
        self._sort_index += 1
        self._resort()

        if write:
            self.write_fs = fs
            self._write_fs_name = name
            self._load_shadows()

    def get_fs(self, name):
        # type: (Text) -> FS
        """Get a filesystem from its name.

        Arguments:
            name (str): The name of a filesystem previously added.

        Returns:
            FS: the filesystem added as ``name`` previously.

        Raises:
            KeyError: If no filesystem with given ``name`` could be found.

        """
        return self._filesystems[name].fs

    def _resort(self):
        # type: () -> None
        """Force `iterate_fs` to re-sort on next reference."""
        self._fs_sequence = None

    def iterate_fs(self):
        # type: () -> Iterator[Tuple[Text, FS]]
        """Get iterator that returns (name, fs) in priority order."""
        if self._fs_sequence is None:
            self._fs_sequence = [
                (name, fs)
                for name, (_order, fs) in sorted(
                    self._filesystems.items(), key=itemgetter(1), reverse=True
                )
            ]
        return iter(self._fs_sequence)

    def _is_write_fs(self, fs):
        # type: (FS) -> bool
        return self.write_fs is not None and fs is self.write_fs

    def _upper_exists(self, path):
        # type: (Text) -> bool
        if self.write_fs is None:
            return False
        _path = abspath(normpath(path))
        if _path == "/":
            return True
        return self.write_fs.exists(_path)

    def _lower(self, path):
        # type: (Text) -> Tuple[Optional[Text], Optional[FS]]
        """Get the first non-writable filesystem that has ``path``."""
        _path = abspath(normpath(path))
        for name, fs in self.iterate_fs():
            if self._is_write_fs(fs):
                continue
            if fs.exists(_path):
                return name, fs
        return None, None

    def _lower_exists(self, path):
        # type: (Text) -> bool
        return self._lower(path)[1] is not None

    def _check_ancestors(self, path):
        # type: (Text) -> None
        """Validate the ancestors of ``path`` against the visible view.

        An ancestor that is visible as a file prevents creating a
        directory below it, even if the file lives in a lower layer.
        A shadowed ancestor is not visible, so the path is not found.

        """
        _path = abspath(normpath(path))
        for parent in recursepath(_path)[1:-1]:
            try:
                info = self.getinfo(parent)
            except errors.ResourceNotFound:
                raise errors.ResourceNotFound(path)
            if not info.is_dir:
                raise errors.DirectoryExpected(path)

    def _is_shadowed(self, path):
        # type: (Text) -> bool
        """Check if ``path`` is hidden from lower layers by a tombstone."""
        self._ensure_shadows_loaded()
        _path = abspath(normpath(path))
        if _path == "/":
            return False
        for ancestor in recursepath(_path):
            if ancestor in self._shadows:
                return True
        return False

    def _has_shadow(self, path):
        # type: (Text) -> bool
        """Check if there is an exact tombstone for ``path``."""
        self._ensure_shadows_loaded()
        return abspath(normpath(path)) in self._shadows

    def _tombstone_dir(self):
        # type: () -> Text
        return "/{}/{}".format(_TOMBSTONE_DIR, _TOMBSTONE_SUBDIR)

    def _tombstone_path(self, path):
        # type: (Text) -> Text
        digest = hashlib.sha256(
            abspath(normpath(path)).encode("utf-8")
        ).hexdigest()
        return join(self._tombstone_dir(), digest)

    def _load_shadows(self):
        # type: () -> None
        """Load tombstones stored in the writable layer."""
        self._shadows = set()
        self._shadows_write_fs = self.write_fs
        if self.write_fs is None:
            return
        try:
            names = self.write_fs.listdir(self._tombstone_dir())
        except errors.ResourceNotFound:
            return
        for name in names:
            tombstone = join(self._tombstone_dir(), name)
            try:
                content = self.write_fs.readbytes(tombstone)
            except errors.ResourceNotFound:
                continue
            try:
                path = content.decode("utf-8")
            except (TypeError, ValueError):
                continue
            self._shadows.add(abspath(normpath(path)))

    def _ensure_shadows_loaded(self):
        # type: () -> None
        if self.write_fs is not self._shadows_write_fs:
            self._load_shadows()

    def _write_shadow(self, path):
        # type: (Text) -> None
        """Write a tombstone for ``path`` to the writable layer."""
        if self.write_fs is None:
            raise errors.ResourceReadOnly(path)
        self._ensure_shadows_loaded()
        _path = abspath(normpath(path))
        if _path == "/":
            raise errors.RemoveRootError(_path)
        self.write_fs.makedirs(self._tombstone_dir(), recreate=True)
        self.write_fs.writebytes(self._tombstone_path(_path), _path.encode("utf-8"))
        self._shadows.add(_path)

    def _clear_shadow(self, path):
        # type: (Text) -> None
        """Remove an exact tombstone for ``path``."""
        self._ensure_shadows_loaded()
        _path = abspath(normpath(path))
        self._shadows.discard(_path)
        if self.write_fs is None:
            return
        tombstone = self._tombstone_path(_path)
        try:
            self.write_fs.remove(tombstone)
        except errors.ResourceNotFound:
            pass

    def is_shadowed(self, path):
        # type: (Text) -> bool
        """Check if a path is hidden from the lower layers.

        Arguments:
            path (str): A path on the filesystem.

        Returns:
            bool: `True` if a tombstone hides the path or one of its
            ancestors in the lower layers.

        """
        self.check()
        return self._is_shadowed(path)

    def shadowed_paths(self):
        # type: () -> List[Text]
        """Get the paths that have an exact tombstone."""
        self.check()
        self._ensure_shadows_loaded()
        return sorted(self._shadows)

    def _delegate(self, path):
        # type: (Text) -> Optional[FS]
        """Get a filesystem which has a given path."""
        _path = abspath(normpath(path))
        if _path.startswith("/" + _TOMBSTONE_DIR):
            # The tombstone store is an implementation detail of the
            # writable layer and is never part of the layered view.
            return None
        shadowed = self._is_shadowed(_path)
        for _name, fs in self.iterate_fs():
            if shadowed and not self._is_write_fs(fs):
                continue
            if fs.exists(_path):
                return fs
        return None

    def _delegate_required(self, path):
        # type: (Text) -> FS
        """Check that there is a filesystem with the given ``path``."""
        fs = self._delegate(path)
        if fs is None:
            raise errors.ResourceNotFound(path)
        return fs

    def _writable_required(self, path):
        # type: (Text) -> FS
        """Check that ``path`` is writeable."""
        if self.write_fs is None:
            raise errors.ResourceReadOnly(path)
        return self.write_fs

    def _ensure_upper_parents(self, path):
        # type: (Text) -> None
        """Mirror the visible parent directories into the writable layer.

        Parent directories that exist in a lower layer are created in the
        writable layer so the write can be applied there.  A parent that
        is not visible anywhere raises `~fs.errors.ResourceNotFound`.

        """
        write_fs = self._writable_required(path)
        _path = abspath(normpath(path))
        self._check_ancestors(_path)
        for parent in recursepath(_path)[1:-1]:
            if write_fs.isdir(parent):
                continue
            write_fs.makedirs(parent, recreate=True)

    def _prepare_write(self, path, mode):
        # type: (Text, Text) -> FS
        """Prepare a write and return the target filesystem."""
        write_fs = self._writable_required(path)
        _path = abspath(normpath(path))
        _mode = Mode(mode)
        if _path == "/":
            return write_fs
        if _mode.exclusive and self.exists(_path):
            raise errors.FileExists(_path)
        self._ensure_upper_parents(_path)
        self._clear_shadow(_path)
        if (_mode.appending or _mode.updating) and not _mode.truncate:
            if not write_fs.exists(_path):
                source = self._delegate(_path)
                if source is not None and source is not write_fs:
                    with source.openbin(_path, "r") as read_file:
                        write_fs.upload(_path, read_file)
        return write_fs

    def _writable_plan(self, path, mode):
        # type: (Text, Text) -> WritePlan
        _path = abspath(normpath(path))
        _mode = Mode(mode)
        source = self._delegate(_path)
        source_name = None
        if source is not None:
            for name, fs in self.iterate_fs():
                if fs is source:
                    source_name = name
                    break
        target = self.write_fs if _mode.writing else None
        target_name = self._write_fs_name if _mode.writing else None
        copy_up = bool(
            _mode.writing
            and target is not None
            and source is not None
            and source is not target
            and not _mode.truncate
            and (_mode.appending or _mode.updating)
            and not target.exists(_path)
        )
        return WritePlan(
            path=_path,
            mode=mode,
            target=target,
            target_name=target_name,
            source=source,
            source_name=source_name,
            copy_up=copy_up,
            deletion_policy=self._deletion_policy,
        )

    def write_plan(self, path, mode="w"):
        # type: (Text, Text) -> WritePlan
        """Describe where a write to ``path`` would be applied.

        Writes are always directed at the writable filesystem added with
        ``add_fs(..., write=True)``.  The plan also reports which layer
        currently provides the visible resource, and whether the write
        requires a copy-up from a lower layer.

        Arguments:
            path (str): A path on the filesystem.
            mode (str): An `io.open` mode.

        Returns:
            WritePlan: the planned write target.

        Raises:
            ValueError: If ``mode`` is not a valid mode string.

        """
        self.check()
        return self._writable_plan(path, mode)

    def explain_write(self, path, mode="w"):
        # type: (Text, Text) -> WritePlan
        """Alias for `write_plan`."""
        return self.write_plan(path, mode)

    def plan_write(self, path, mode="w"):
        # type: (Text, Text) -> WritePlan
        """Alias for `write_plan`."""
        return self.write_plan(path, mode)

    def _resolve_policy(self, policy):
        # type: (Union[DeletionPolicy, Text, None]) -> DeletionPolicy
        if policy is None:
            return self._deletion_policy
        return _normalize_deletion_policy(policy)

    def delete_plan(self, path, policy=None):
        # type: (Text, Union[DeletionPolicy, Text, None]) -> DeletePlan
        """Describe what deleting ``path`` would do.

        The plan makes an explicit distinction between retracting an
        upper-layer correction and shadowing a lower-layer resource.
        The lower layer is never physically modified.

        Arguments:
            path (str): A path on the filesystem.
            policy (~fs.enums.DeletionPolicy or str, optional): Override
                the filesystem's deletion policy for this plan.

        Returns:
            DeletePlan: the planned deletion.

        """
        self.check()
        _policy = self._resolve_policy(policy)
        _path = abspath(normpath(path))
        upper_exists = self._upper_exists(_path)
        lower_name, lower_fs = self._lower(_path)
        lower_exists = lower_fs is not None
        shadowed = self._has_shadow(_path)
        ancestor_shadowed = self._is_shadowed(_path)

        if upper_exists and lower_exists:
            action = (
                "retract-correction"
                if _policy is DeletionPolicy.retract
                else "shadow-base"
            )
        elif upper_exists:
            action = "delete-upper"
        elif lower_exists:
            action = "shadow-base"
        else:
            action = "not-found"

        target = self.write_fs
        return DeletePlan(
            path=_path,
            policy=_policy,
            action=action,
            target=target,
            target_name=self._write_fs_name,
            upper_fs=self.write_fs,
            upper_name=self._write_fs_name,
            lower_fs=lower_fs,
            lower_name=lower_name,
            upper_exists=upper_exists,
            lower_exists=lower_exists,
            shadowed=shadowed,
            ancestor_shadowed=ancestor_shadowed,
        )

    def explain_delete(self, path, policy=None):
        # type: (Text, Union[DeletionPolicy, Text, None]) -> DeletePlan
        """Alias for `delete_plan`."""
        return self.delete_plan(path, policy=policy)

    def plan_delete(self, path, policy=None):
        # type: (Text, Union[DeletionPolicy, Text, None]) -> DeletePlan
        """Alias for `delete_plan`."""
        return self.delete_plan(path, policy=policy)

    def describe_write(self, path, mode="w"):
        # type: (Text, Text) -> Text
        """Get a human-readable description of a write target."""
        plan = self.write_plan(path, mode)
        return plan.describe()

    def describe_delete(self, path, policy=None):
        # type: (Text, Union[DeletionPolicy, Text, None]) -> Text
        """Get a human-readable description of a deletion."""
        plan = self.delete_plan(path, policy=policy)
        return plan.describe()

    def which(self, path, mode="r"):
        # type: (Text, Text) -> Tuple[Optional[Text], Optional[FS]]
        """Get a tuple of (name, fs) that the given path would map to.

        Arguments:
            path (str): A path on the filesystem.
            mode (str): An `io.open` mode.

        """
        if check_writable(mode):
            return self._write_fs_name, self.write_fs
        _path = abspath(normpath(path))
        shadowed = self._is_shadowed(_path)
        for name, fs in self.iterate_fs():
            if shadowed and not self._is_write_fs(fs):
                continue
            if fs.exists(_path):
                return name, fs
        return None, None

    def close(self):
        # type: () -> None
        self._closed = True
        if self._auto_close:
            try:
                for _order, fs in self._filesystems.values():
                    fs.close()
            finally:
                self._filesystems.clear()
                self._resort()

    def getinfo(self, path, namespaces=None):
        # type: (Text, Optional[Collection[Text]]) -> Info
        self.check()
        namespaces = namespaces or ()
        fs = self._delegate(path)
        if fs is None:
            raise errors.ResourceNotFound(path)
        _path = abspath(normpath(path))
        info = fs.getinfo(_path, namespaces=namespaces)
        return info

    def listdir(self, path):
        # type: (Text) -> List[Text]
        self.check()
        _path = abspath(normpath(path))
        directory = []
        exists = False
        shadowed = self._is_shadowed(_path)
        for _name, _fs in self.iterate_fs():
            if shadowed and not self._is_write_fs(_fs):
                continue
            try:
                names = _fs.listdir(_path)
            except errors.ResourceNotFound:
                pass
            else:
                exists = True
                for name in names:
                    if (
                        self._is_write_fs(_fs)
                        and _path == "/"
                        and name == _TOMBSTONE_DIR
                    ):
                        continue
                    child = join(_path, name)
                    if not self._is_write_fs(_fs) and self._is_shadowed(child):
                        continue
                    directory.append(name)
        if not exists:
            raise errors.ResourceNotFound(path)
        directory = list(OrderedDict.fromkeys(directory))
        return directory

    def makedir(
        self,  # type: M
        path,  # type: Text
        permissions=None,  # type: Optional[Permissions]
        recreate=False,  # type: bool
    ):
        # type: (...) -> SubFS[FS]
        self.check()
        write_fs = self._writable_required(path)
        _path = abspath(normpath(path))
        if self.exists(_path):
            if not recreate:
                raise errors.DirectoryExists(_path)
        for current in recursepath(_path)[1:]:
            self._clear_shadow(current)
        self._ensure_upper_parents(_path)
        return write_fs.makedir(_path, permissions=permissions, recreate=recreate)

    def openbin(self, path, mode="r", buffering=-1, **options):
        # type: (Text, Text, int, **Any) -> BinaryIO
        self.check()
        if check_writable(mode):
            _fs = self._prepare_write(path, mode)
        else:
            _fs = self._delegate_required(path)
        return _fs.openbin(path, mode=mode, buffering=buffering, **options)

    def remove(self, path, policy=None):
        # type: (Text, Union[DeletionPolicy, Text, None]) -> None
        """Remove a file, respecting the deletion policy.

        An upper-layer correction is removed from the writable layer.
        With the ``"retract"`` policy the lower-layer resource (if any)
        becomes visible again; with the ``"shadow"`` policy a tombstone
        is written so the lower layer stays hidden.  A resource that
        only exists in a read-only lower layer is shadowed, never
        physically deleted.

        Arguments:
            path (str): Path of the file to remove.
            policy (~fs.enums.DeletionPolicy or str, optional): Override
                the filesystem's deletion policy.

        Raises:
            fs.errors.FileExpected: If the path is a directory.
            fs.errors.ResourceNotFound: If the path does not exist.

        """
        self.check()
        _policy = self._resolve_policy(policy)
        _path = abspath(normpath(path))
        if not self.exists(_path):
            raise errors.ResourceNotFound(path)
        if self.isdir(_path):
            raise errors.FileExpected(path)

        upper_exists = self._upper_exists(_path)
        lower_exists = self._lower_exists(_path)
        if upper_exists:
            self.write_fs.remove(_path)
            if lower_exists and _policy is DeletionPolicy.shadow:
                self._write_shadow(_path)
            else:
                self._clear_shadow(_path)
            return
        if lower_exists:
            if self.write_fs is None:
                raise errors.ResourceReadOnly(path)
            self._write_shadow(_path)
            return
        raise errors.ResourceNotFound(path)

    def delete(self, path, policy=None):
        # type: (Text, Union[DeletionPolicy, Text, None]) -> None
        """Alias for `remove`."""
        return self.remove(path, policy=policy)

    def removedir(self, path, policy=None):
        # type: (Text, Union[DeletionPolicy, Text, None]) -> None
        """Remove a directory, respecting the deletion policy.

        Arguments:
            path (str): Path of the directory to remove.
            policy (~fs.enums.DeletionPolicy or str, optional): Override
                the filesystem's deletion policy.

        Raises:
            fs.errors.DirectoryNotEmpty: If the visible directory is not
                empty.
            fs.errors.DirectoryExpected: If the path is not a directory.
            fs.errors.ResourceNotFound: If the path does not exist.

        """
        self.check()
        _policy = self._resolve_policy(policy)
        _path = abspath(normpath(path))
        if _path == "/":
            raise errors.RemoveRootError(path)
        if not self.exists(_path):
            raise errors.ResourceNotFound(path)
        if not self.isdir(_path):
            raise errors.DirectoryExpected(path)
        if list(self.scandir(_path)):
            raise errors.DirectoryNotEmpty(path)

        upper_exists = self._upper_exists(_path)
        lower_exists = self._lower_exists(_path)
        if upper_exists:
            self.write_fs.removedir(_path)
            if lower_exists and _policy is DeletionPolicy.shadow:
                self._write_shadow(_path)
            else:
                self._clear_shadow(_path)
            return
        if lower_exists:
            if self.write_fs is None:
                raise errors.ResourceReadOnly(path)
            self._write_shadow(_path)
            return
        raise errors.ResourceNotFound(path)

    def removetree(self, dir_path, policy=None):
        # type: (Text, Union[DeletionPolicy, Text, None]) -> None
        """Recursively remove a directory and all its contents.

        If the directory exists in a lower layer, the policy decides
        whether the recursive delete only removes the upper-layer copy
        (revealing the lower layer again) or records a tombstone so the
        whole subtree stays hidden.  The lower layer is never modified,
        so a single physical delete is never presented as a global
        delete.

        Arguments:
            dir_path (str): Path of a directory on the filesystem.
            policy (~fs.enums.DeletionPolicy or str, optional): Override
                the filesystem's deletion policy.

        """
        self.check()
        _path = abspath(normpath(dir_path))
        if _path == "/":
            for info in list(self.scandir(_path)):
                child = join(_path, info.name)
                if info.is_dir:
                    self.removetree(child, policy=policy)
                else:
                    self.remove(child, policy=policy)
            return
        if not self.exists(_path):
            raise errors.ResourceNotFound(dir_path)
        if not self.isdir(_path):
            raise errors.DirectoryExpected(dir_path)

        lower_exists = self._lower_exists(_path)
        if lower_exists:
            if self.write_fs is None:
                raise errors.ResourceReadOnly(dir_path)
            upper_exists = self.write_fs.exists(_path)
            if upper_exists:
                self.write_fs.removetree(_path)
            if (
                not upper_exists
                or self._resolve_policy(policy) is DeletionPolicy.shadow
            ):
                self._write_shadow(_path)
            else:
                self._clear_shadow(_path)
            return

        for info in list(self.scandir(_path)):
            child = join(_path, info.name)
            if info.is_dir:
                self.removetree(child, policy=policy)
            else:
                self.remove(child, policy=policy)
        self.removedir(_path, policy=policy)

    def scandir(
        self,
        path,  # type: Text
        namespaces=None,  # type: Optional[Collection[Text]]
        page=None,  # type: Optional[Tuple[int, int]]
    ):
        # type: (...) -> Iterator[Info]
        self.check()
        _path = abspath(normpath(path))
        seen = set()  # type: MutableSet[Text]
        exists = False
        shadowed = self._is_shadowed(_path)
        for _name, fs in self.iterate_fs():
            if shadowed and not self._is_write_fs(fs):
                continue
            try:
                for info in fs.scandir(_path, namespaces=namespaces, page=page):
                    if (
                        self._is_write_fs(fs)
                        and _path == "/"
                        and info.name == _TOMBSTONE_DIR
                    ):
                        continue
                    child = join(_path, info.name)
                    if not self._is_write_fs(fs) and self._is_shadowed(child):
                        continue
                    if info.name not in seen:
                        yield info
                        seen.add(info.name)
                exists = True
            except errors.ResourceNotFound:
                pass

        if not exists:
            raise errors.ResourceNotFound(path)

    def readbytes(self, path):
        # type: (Text) -> bytes
        self.check()
        fs = self._delegate(path)
        if fs is None:
            raise errors.ResourceNotFound(path)
        return fs.readbytes(path)

    def download(self, path, file, chunk_size=None, **options):
        # type: (Text, BinaryIO, Optional[int], **Any) -> None
        fs = self._delegate_required(path)
        return fs.download(path, file, chunk_size=chunk_size, **options)

    def readtext(self, path, encoding=None, errors=None, newline=""):
        # type: (Text, Optional[Text], Optional[Text], Text) -> Text
        self.check()
        fs = self._delegate_required(path)
        return fs.readtext(path, encoding=encoding, errors=errors, newline=newline)

    def getsize(self, path):
        # type: (Text) -> int
        self.check()
        fs = self._delegate_required(path)
        return fs.getsize(path)

    def getsyspath(self, path):
        # type: (Text) -> Text
        self.check()
        fs = self._delegate_required(path)
        return fs.getsyspath(path)

    def gettype(self, path):
        # type: (Text) -> ResourceType
        self.check()
        fs = self._delegate_required(path)
        return fs.gettype(path)

    def geturl(self, path, purpose="download"):
        # type: (Text, Text) -> Text
        self.check()
        fs = self._delegate_required(path)
        return fs.geturl(path, purpose=purpose)

    def hassyspath(self, path):
        # type: (Text) -> bool
        self.check()
        fs = self._delegate(path)
        return fs is not None and fs.hassyspath(path)

    def hasurl(self, path, purpose="download"):
        # type: (Text, Text) -> bool
        self.check()
        fs = self._delegate(path)
        return fs is not None and fs.hasurl(path, purpose=purpose)

    def isdir(self, path):
        # type: (Text) -> bool
        self.check()
        fs = self._delegate(path)
        return fs is not None and fs.isdir(path)

    def isfile(self, path):
        # type: (Text) -> bool
        self.check()
        fs = self._delegate(path)
        return fs is not None and fs.isfile(path)

    def setinfo(self, path, info):
        # type:(Text, RawInfo) -> None
        self.check()
        _path = abspath(normpath(path))
        if not self.exists(_path):
            raise errors.ResourceNotFound(path)
        write_fs = self._writable_required(path)
        if not write_fs.exists(_path):
            self._ensure_upper_parents(_path)
            source = self._delegate(_path)
            if source is not None and source.isdir(_path):
                write_fs.makedirs(_path, recreate=True)
            else:
                self._prepare_write(_path, "r+")
        return write_fs.setinfo(_path, info)

    def validatepath(self, path):
        # type: (Text) -> Text
        self.check()
        if self.write_fs is not None:
            self.write_fs.validatepath(path)
        else:
            super(MultiFS, self).validatepath(path)
        path = abspath(normpath(path))
        return path

    def makedirs(
        self,
        path,  # type: Text
        permissions=None,  # type: Optional[Permissions]
        recreate=False,  # type: bool
    ):
        # type: (...) -> SubFS[FS]
        self.check()
        write_fs = self._writable_required(path)
        _path = abspath(normpath(path))
        if self.exists(_path) and not recreate:
            raise errors.DirectoryExists(_path)
        for current in recursepath(_path)[1:]:
            self._clear_shadow(current)
        return write_fs.makedirs(_path, permissions=permissions, recreate=recreate)

    def open(
        self,
        path,  # type: Text
        mode="r",  # type: Text
        buffering=-1,  # type: int
        encoding=None,  # type: Optional[Text]
        errors=None,  # type: Optional[Text]
        newline="",  # type: Text
        **kwargs  # type: Any
    ):
        # type: (...) -> IO
        self.check()
        if check_writable(mode):
            _fs = self._prepare_write(path, mode)
        else:
            _fs = self._delegate_required(path)
        return _fs.open(
            path,
            mode=mode,
            buffering=buffering,
            encoding=encoding,
            errors=errors,
            newline=newline,
            **kwargs
        )

    def create(self, path, wipe=False):
        # type: (Text, bool) -> bool
        self.check()
        _path = abspath(normpath(path))
        if not wipe and self.exists(_path):
            return False
        self._prepare_write(_path, "wb")
        with self.write_fs.openbin(_path, "wb"):
            pass
        return True

    def touch(self, path):
        # type: (Text) -> None
        self.check()
        _path = abspath(normpath(path))
        if not self.exists(_path):
            self.create(_path)
            return
        self._prepare_write(_path, "r+")
        if self.write_fs.isfile(_path):
            self.write_fs.touch(_path)
            return
        self.setinfo(_path, {"details": {}})

    def upload(self, path, file, chunk_size=None, **options):
        # type: (Text, BinaryIO, Optional[int], **Any) -> None
        self._prepare_write(path, "wb").upload(
            path, file, chunk_size=chunk_size, **options
        )

    def writebytes(self, path, contents):
        # type: (Text, bytes) -> None
        self._prepare_write(path, "wb").writebytes(path, contents)

    def writetext(
        self,
        path,  # type: Text
        contents,  # type: Text
        encoding="utf-8",  # type: Text
        errors=None,  # type: Optional[Text]
        newline="",  # type: Text
    ):
        # type: (...) -> None
        write_fs = self._prepare_write(path, "wt")
        return write_fs.writetext(
            path, contents, encoding=encoding, errors=errors, newline=newline
        )
