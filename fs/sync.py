"""Resumable synchronization of a filesystem.

Synchronizing a directory tree may take a long time, and may be
interrupted (a crash, a network failure, or a Ctrl-C) before it
completes. This module implements a sync that can be resumed later,
built around two concepts:

*   A *sync plan* -- a complete snapshot of the source filesystem,
    captured by a **single, complete** walk before any copying starts.
    A directory walk can not be paused and resumed, so a plan is never
    assembled by stitching together the positions of two partial
    traversals. If the scan itself is interrupted, no plan is persisted
    and the next run scans again from scratch.

*   A *checkpoint* -- the plan plus a set of *receipts* describing the
    verified state of every destination file copied so far, persisted
    to a local state file. On resume, each receipt is re-validated
    against the live destination: if the file was modified externally
    since its receipt was written, the receipt is discarded and the
    file is copied again. Old success receipts are never trusted
    blindly.

By default, resuming binds to the plan stored in the checkpoint (the
original scan view), so changes made to the source after the initial
scan are *not* picked up. Pass ``rebuild=True`` to explicitly discard
the stored plan and build a new one from a fresh scan; receipts for
files that are still verified are carried over and reused.

The state file is removed once the sync has completed successfully, so
its presence always indicates an unfinished sync.

"""

from __future__ import unicode_literals

import errno
import io
import json
import os
import typing
from calendar import timegm

import six

from ._bulk import Copier
from .copy import copy_file_internal
from .errors import OperationFailed, ResourceNotFound
from .opener import manage_fs
from .path import basename, dirname
from .tools import is_thread_safe
from .walk import Walker

if typing.TYPE_CHECKING:
    from typing import Callable, Dict, List, Optional, Text, Union

    from .base import FS
    from .info import Info


_STATE_VERSION = 1

if six.PY3:
    _replace = os.replace
else:  # pragma: no cover

    def _replace(src, dst):
        # type: (Text, Text) -> None
        try:
            os.rename(src, dst)
        except OSError:
            os.remove(dst)
            os.rename(src, dst)


def _info_meta(info):
    # type: (Info) -> Dict[Text, object]
    """Extract the sync-relevant metadata (size and mtime) from an `Info`."""
    modified = info.modified
    if modified is None:
        modified_timestamp = None
    else:
        modified_timestamp = (
            timegm(modified.utctimetuple()) + modified.microsecond / 1e6
        )
    return {"size": info.size, "modified": modified_timestamp}


def _needs_copy(src_meta, dst_meta):
    # type: (Dict[Text, object], Dict[Text, object]) -> bool
    """Check if a file should be copied, given source and destination meta."""
    if src_meta["size"] != dst_meta["size"]:
        return True
    src_modified = src_meta["modified"]
    dst_modified = dst_meta["modified"]
    if src_modified is None or dst_modified is None:
        return True
    return src_modified > dst_modified


class SyncPlan(object):
    """A complete snapshot of a source filesystem.

    A plan is always built from a single, complete walk of the source
    (see `SyncPlan.scan`); it records every directory, and every file
    with its size and modification time.
    """

    def __init__(self, directories, files):
        # type: (List[Text], Dict[Text, Dict[Text, object]]) -> None
        self.directories = sorted(directories)
        self.files = files

    @classmethod
    def scan(cls, src_fs, walker=None):
        # type: (FS, Optional[Walker]) -> SyncPlan
        """Build a plan from a single complete walk of ``src_fs``.

        If the walk is interrupted, no plan is produced; a partial
        traversal is never combined with a later one.
        """
        walker = walker or Walker()
        directories = []  # type: List[Text]
        files = {}  # type: Dict[Text, Dict[Text, object]]
        for path, dirs, _files in walker.walk(src_fs, namespaces=["details"]):
            for _dir in dirs:
                directories.append(_dir.make_path(path))
            for _file in _files:
                files[_file.make_path(path)] = _info_meta(_file)
        return cls(directories, files)

    def to_dict(self):
        # type: () -> Dict[Text, object]
        """Serialize the plan to a JSON-compatible dict."""
        return {"directories": list(self.directories), "files": self.files}

    @classmethod
    def from_dict(cls, data):
        # type: (Dict[Text, object]) -> SyncPlan
        """Deserialize a plan created with `SyncPlan.to_dict`."""
        return cls(data["directories"], data["files"])


class SyncCheckpoint(object):
    """Persistent record of sync progress.

    Combines the `SyncPlan` being executed with *receipts* mapping
    destination paths to the source and destination metadata verified
    after each copy.
    """

    def __init__(self, plan, receipts=None):
        # type: (SyncPlan, Optional[Dict[Text, Dict[Text, Dict[Text, object]]]]) -> None
        self.plan = plan
        self.receipts = receipts if receipts is not None else {}

    @classmethod
    def load(cls, state_path):
        # type: (Text) -> Optional[SyncCheckpoint]
        """Load a checkpoint from ``state_path``.

        Returns `None` if no state file exists. Raises
        `~fs.errors.OperationFailed` if the state file is corrupt or
        was written by an incompatible version.
        """
        try:
            with io.open(state_path, "r", encoding="utf-8") as state_file:
                data = json.load(state_file)
        except IOError as error:
            if error.errno == errno.ENOENT:
                return None
            raise
        except ValueError:
            raise OperationFailed(
                path=state_path,
                msg="sync checkpoint {path!r} is corrupt; "
                "delete it or re-run with rebuild=True",
            )
        if not isinstance(data, dict) or data.get("version") != _STATE_VERSION:
            raise OperationFailed(
                path=state_path,
                msg="sync checkpoint {path!r} has an unsupported format; "
                "delete it or re-run with rebuild=True",
            )
        return cls(
            SyncPlan.from_dict(data["plan"]), data.get("receipts") or {}
        )

    def save(self, state_path):
        # type: (Text) -> None
        """Write the checkpoint to ``state_path`` atomically."""
        data = {
            "version": _STATE_VERSION,
            "plan": self.plan.to_dict(),
            "receipts": self.receipts,
        }
        tmp_path = "{}.tmp{}".format(state_path, os.getpid())
        with io.open(tmp_path, "w", encoding="utf-8") as state_file:
            json.dump(data, state_file)
        _replace(tmp_path, state_path)


def sync(
    src_fs,  # type: Union[FS, Text]
    dst_fs,  # type: Union[FS, Text]
    state_path=None,  # type: Optional[Text]
    walker=None,  # type: Optional[Walker]
    copy_if_newer=True,  # type: bool
    workers=0,  # type: int
    preserve_time=False,  # type: bool
    rebuild=False,  # type: bool
):
    # type: (...) -> None
    """Synchronize files / directories from one filesystem to another.

    Like `fs.mirror.mirror`, this creates a copy of ``src_fs`` on
    ``dst_fs``, removing resources on the destination that aren't on
    the source. Unlike `mirror`, the sync may be interrupted and
    resumed: progress is recorded in a checkpoint state file, and a
    later call with the same ``state_path`` picks up where the previous
    run stopped, reusing destination files that are still verified.

    Arguments:
        src_fs (FS or str): Source filesystem (URL or instance).
        dst_fs (FS or str): Destination filesystem (URL or instance).
        state_path (str, optional): Path of a local file used to store
            the sync checkpoint. If `None` (the default), no checkpoint
            is kept and the sync can not be resumed.
        walker (~fs.walk.Walker, optional): An optional walker instance
            used when scanning the source.
        copy_if_newer (bool): Only copy newer files (the default).
        workers (int): Number of worker threads used
            (0 for single threaded). Set to a relatively low number
            for network filesystems, 4 would be a good start.
        preserve_time (bool): If `True`, try to preserve mtime of the
            resources (defaults to `False`).
        rebuild (bool): If `True`, discard any plan stored in the
            checkpoint and build a new plan from a fresh, complete scan
            of the source. If `False` (the default) and a checkpoint
            exists, resume the stored plan exactly as scanned; changes
            made to the source since then are not picked up.

    Raises:
        fs.errors.OperationFailed: If the checkpoint is corrupt, or a
            planned source resource no longer exists (re-run with
            ``rebuild=True`` to plan from the current source).

    """

    def src():
        return manage_fs(src_fs, writeable=False)

    def dst():
        return manage_fs(dst_fs, create=True)

    with src() as _src_fs, dst() as _dst_fs:
        _thread_safe = is_thread_safe(_src_fs, _dst_fs)
        num_workers = workers if _thread_safe else 0
        with _src_fs.lock(), _dst_fs.lock():
            checkpoint = _resume_checkpoint(_src_fs, state_path, walker, rebuild)
            with Copier(
                num_workers=num_workers, preserve_time=preserve_time
            ) as copier:
                unverified = _sync(
                    _src_fs,
                    _dst_fs,
                    checkpoint,
                    state_path=state_path,
                    copy_if_newer=copy_if_newer,
                    copy_file=copier.copy,
                    preserve_time=preserve_time,
                    verify=num_workers == 0,
                )
            if unverified:
                # Copies were performed by worker threads; verify them
                # now that the copier has finished.
                _verify_copies(_dst_fs, checkpoint, unverified)
                if state_path is not None:
                    checkpoint.save(state_path)
        if state_path is not None:
            # The sync completed; the checkpoint is no longer needed.
            _remove_state(state_path)


def _resume_checkpoint(src_fs, state_path, walker, rebuild):
    # type: (FS, Optional[Text], Optional[Walker], bool) -> SyncCheckpoint
    """Load or build the checkpoint to resume from.

    Binds to the plan stored in the state file, unless there is no
    state file or ``rebuild`` is `True`, in which case a new plan is
    built from a single complete scan of the source. Receipts from a
    previous checkpoint are carried over a rebuild, so verified
    destination files can still be reused.
    """
    checkpoint = None
    if state_path is not None:
        if rebuild:
            try:
                checkpoint = SyncCheckpoint.load(state_path)
            except OperationFailed:
                # A corrupt checkpoint must not block an explicit rebuild.
                checkpoint = None
        else:
            checkpoint = SyncCheckpoint.load(state_path)
    if checkpoint is None or rebuild:
        receipts = checkpoint.receipts if checkpoint is not None else {}
        checkpoint = SyncCheckpoint(SyncPlan.scan(src_fs, walker=walker), receipts)
        if state_path is not None:
            checkpoint.save(state_path)
    return checkpoint


def _sync(
    src_fs,  # type: FS
    dst_fs,  # type: FS
    checkpoint,  # type: SyncCheckpoint
    state_path=None,  # type: Optional[Text]
    copy_if_newer=True,  # type: bool
    copy_file=copy_file_internal,  # type: Callable[[FS, str, FS, str, bool], None]
    preserve_time=False,  # type: bool
    verify=True,  # type: bool
):
    # type: (...) -> List[Text]
    """Synchronize ``dst_fs`` with the checkpoint's plan.

    Returns the paths that were copied but not yet verified (only
    non-empty when ``verify`` is `False`, i.e. when copies run
    asynchronously in worker threads).
    """
    plan = checkpoint.plan
    receipts = checkpoint.receipts

    # Drop receipts for files that are no longer in the plan (the plan
    # may have been rebuilt since the receipts were written).
    for file_path in list(receipts):
        if file_path not in plan.files:
            del receipts[file_path]

    # Create directories from the plan.
    for dir_path in plan.directories:
        if dst_fs.isfile(dir_path):
            dst_fs.remove(dir_path)
        dst_fs.makedir(dir_path, recreate=True)

    # Copy files from the plan.
    unverified = []  # type: List[Text]
    for file_path in sorted(plan.files):
        src_meta = plan.files[file_path]
        receipt = receipts.get(file_path)
        try:
            dst_info = dst_fs.getinfo(file_path, namespaces=["details"])
        except ResourceNotFound:
            dst_info = None
        if dst_info is not None:
            if dst_info.is_dir:
                # Destination is a directory, remove it
                dst_fs.removetree(file_path)
            else:
                dst_meta = _info_meta(dst_info)
                if (
                    receipt is not None
                    and receipt.get("dst") == dst_meta
                    and receipt.get("src") == src_meta
                ):
                    # Verified material; reuse it.
                    continue
                if copy_if_newer and not _needs_copy(src_meta, dst_meta):
                    # Destination already matches; record a receipt so
                    # the file need not be compared again on resume.
                    receipts[file_path] = {"src": dict(src_meta), "dst": dst_meta}
                    continue
        try:
            copy_file(src_fs, file_path, dst_fs, file_path, preserve_time)
        except ResourceNotFound:
            raise OperationFailed(
                path=file_path,
                msg="source resource {path!r} has disappeared since the "
                "sync plan was made; re-run with rebuild=True to plan "
                "from the current source",
            )
        if verify:
            _verify_copy(dst_fs, checkpoint, file_path, src_meta)
            if state_path is not None:
                checkpoint.save(state_path)
        else:
            unverified.append(file_path)

    # Remove destination resources that are not in the plan.
    children = {"/": set()}  # type: Dict[Text, set]
    for dir_path in plan.directories:
        children.setdefault(dirname(dir_path), set()).add(basename(dir_path))
        children.setdefault(dir_path, set())
    for file_path in plan.files:
        children.setdefault(dirname(file_path), set()).add(basename(file_path))
    for dir_path in sorted(children):
        keep = children[dir_path]
        for info in dst_fs.scandir(dir_path):
            if info.name not in keep:
                _path = info.make_path(dir_path)
                if info.is_dir:
                    dst_fs.removetree(_path)
                else:
                    dst_fs.remove(_path)

    return unverified


def _verify_copy(dst_fs, checkpoint, file_path, src_meta):
    # type: (FS, SyncCheckpoint, Text, Dict[Text, object]) -> None
    """Verify a copied file on the destination and record its receipt."""
    dst_meta = _info_meta(dst_fs.getinfo(file_path, namespaces=["details"]))
    if dst_meta["size"] != src_meta["size"]:
        raise OperationFailed(
            path=file_path,
            msg="verification failed for {path!r}: "
            "size mismatch on destination",
        )
    checkpoint.receipts[file_path] = {"src": dict(src_meta), "dst": dst_meta}


def _verify_copies(dst_fs, checkpoint, file_paths):
    # type: (FS, SyncCheckpoint, List[Text]) -> None
    """Verify files copied asynchronously and record their receipts."""
    for file_path in file_paths:
        _verify_copy(dst_fs, checkpoint, file_path, checkpoint.plan.files[file_path])


def _remove_state(state_path):
    # type: (Text) -> None
    """Remove the checkpoint state file, if it exists."""
    try:
        os.remove(state_path)
    except OSError as error:
        if error.errno != errno.ENOENT:
            raise
