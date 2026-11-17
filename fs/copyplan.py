"""Plan and verify file copies across filesystems.

This module implements *copy plans*. A copy plan inspects a source file
and one or more destinations, and reports *upfront*:

-   the copy method selected for each destination (a backend *direct*
    copy where possible, otherwise a *streaming* copy),
-   which of the required attributes (content version, permissions,
    timestamps) each destination is able to preserve, and which it
    can not.

Executing a plan copies the file to every destination independently:
each destination gets its own verified result, and a failure on one
destination never rolls back another destination's verified copy.

If the source file changes after it has been inspected, the plan
refuses to publish the new content with the previously captured
metadata; the affected destinations are reported as failed with a
`~fs.copyplan.SourceChanged` error instead.
"""

from __future__ import print_function, unicode_literals

import hashlib
import typing
from enum import Enum

import six

from . import errors
from .copy import copy_file_internal
from .opener import manage_fs
from .path import abspath, normpath

if typing.TYPE_CHECKING:
    from typing import Dict, FrozenSet, Iterable, List, Optional, Text, Tuple

    from .base import FS

    _Target = Tuple[FS, Text]


#: Preserve the file contents (verified by size, and optionally a hash).
PRESERVE_CONTENT = "content"
#: Preserve the file permissions (the ``access`` namespace).
PRESERVE_PERMISSIONS = "permissions"
#: Preserve the file timestamps (the ``details`` namespace).
PRESERVE_TIMES = "times"

#: All supported preservation requirements.
PRESERVE_ALL = frozenset([PRESERVE_CONTENT, PRESERVE_PERMISSIONS, PRESERVE_TIMES])

#: Tolerance in seconds when verifying copied timestamps.
TIME_TOLERANCE = 2.0

_META_MODIFIED_TIME = "supports_modified_time"
_META_PERMISSIONS = "supports_permissions"


class CopyPlanError(errors.FSError):
    """Base class for copy plan errors."""


class SourceChanged(CopyPlanError):
    """The source file changed after it was inspected.

    Raised (and reported) when the source no longer matches the
    fingerprint taken when the plan was created, so that new content
    is never published together with stale metadata.
    """


class PreservationError(CopyPlanError):
    """A required attribute could not be preserved or verified."""


class CopyMethod(Enum):
    """How the file contents are transferred to a destination."""

    #: The backend copies the file itself (same filesystem copy, or a
    #: direct transfer to / from a system path).
    DIRECT = "direct"
    #: The file is streamed through the filesystem APIs.
    STREAM = "stream"
    #: The destination can not be written at all.
    UNSUPPORTED = "unsupported"


class TargetStatus(Enum):
    """The outcome of copying to a single destination."""

    #: The destination has not been processed yet.
    PENDING = "pending"
    #: The file was copied and all required attributes were verified.
    VERIFIED = "verified"
    #: The copy was attempted but failed (see ``error``).
    FAILED = "failed"
    #: The copy was not attempted because the plan showed the
    #: destination can not satisfy the requirements.
    SKIPPED = "skipped"


def _hash_file(fs, path, algorithm="md5", chunk_size=1024 * 1024):
    # type: (FS, Text, Text, int) -> Text
    """Hash the contents of a file."""
    hasher = hashlib.new(algorithm)
    with fs.openbin(path) as file:
        while True:
            chunk = file.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _fingerprint(fs, path, verify_hash=False):
    # type: (FS, Text, bool) -> Dict[Text, object]
    """Take a fingerprint of a file, to detect changes to the source."""
    info = fs.getinfo(path, ["details"])
    fingerprint = {
        "size": info.size,
        "modified": info.get("details", "modified"),
        "metadata_changed": info.get("details", "metadata_changed"),
    }  # type: Dict[Text, object]
    if verify_hash:
        fingerprint["md5"] = _hash_file(fs, path)
    return fingerprint


def _supports_attribute(fs, meta_key, namespace, info_key):
    # type: (FS, Text, Text, Text) -> bool
    """Check whether a filesystem can set an info attribute.

    Uses the declared meta information when available, and falls back
    to the ``_write`` markers of the root info. Unknown support is
    treated as unsupported, so a plan never promises something the
    destination may not be able to do.
    """
    meta = fs.getmeta()
    if meta_key in meta:
        return bool(meta[meta_key])
    try:
        return fs.getinfo("/", [namespace]).is_writeable(namespace, info_key)
    except errors.FSError:
        return False


def _select_method(src_fs, src_path, dst_fs, dst_path):
    # type: (FS, Text, FS, Text) -> Tuple[CopyMethod, Text]
    """Select the copy method for a destination, preferring direct copies."""
    if dst_fs.getmeta().get("read_only", False):
        return CopyMethod.UNSUPPORTED, "destination filesystem is read-only"
    if src_fs is dst_fs:
        if abspath(normpath(src_path)) == abspath(normpath(dst_path)):
            return (
                CopyMethod.UNSUPPORTED,
                "source and destination are the same file",
            )
        return CopyMethod.DIRECT, "same filesystem, backend copy"
    if dst_fs.hassyspath(dst_path):
        return CopyMethod.DIRECT, "destination has a system path, backend download"
    if src_fs.hassyspath(src_path):
        return CopyMethod.DIRECT, "source has a system path, backend upload"
    return CopyMethod.STREAM, "streaming copy through the filesystem APIs"


class TargetPlan(object):
    """The plan for a single destination.

    Attributes:
        dst_fs (FS): Destination filesystem.
        dst_path (str): Path of the file on the destination filesystem.
        method (~fs.copyplan.CopyMethod): The selected copy method.
        preservable (frozenset): Required attributes this destination
            can preserve.
        unpreservable (frozenset): Required attributes this destination
            can *not* preserve (declared upfront, before any copying).
        reason (str): Human readable note about the selected method.
    """

    def __init__(self, dst_fs, dst_path, method, preservable, unpreservable, reason):
        # type: (FS, Text, CopyMethod, FrozenSet[Text], FrozenSet[Text], Text) -> None
        self.dst_fs = dst_fs
        self.dst_path = dst_path
        self.method = method
        self.preservable = frozenset(preservable)
        self.unpreservable = frozenset(unpreservable)
        self.reason = reason

    def __repr__(self):
        return "TargetPlan({!r}, method={!r}, unpreservable={!r})".format(
            self.dst_path, self.method, sorted(self.unpreservable)
        )

    @property
    def feasible(self):
        # type: () -> bool
        """`True` if the copy may be executed with all requirements met."""
        return self.method is not CopyMethod.UNSUPPORTED and not self.unpreservable


class TargetReport(object):
    """The result of copying to a single destination.

    Attributes:
        target (~fs.copyplan.TargetPlan): The plan for this destination.
        status (~fs.copyplan.TargetStatus): The outcome of the copy.
        error (Exception, optional): The error, if the copy failed.
        bytes_copied (int): Number of bytes written to the destination.
        preserved (frozenset): Required attributes that were applied
            *and* verified on the destination.
    """

    def __init__(
        self, target, status, error=None, bytes_copied=0, preserved=frozenset()
    ):
        # type: (...) -> None
        self.target = target
        self.status = status
        self.error = error
        self.bytes_copied = bytes_copied
        self.preserved = frozenset(preserved)

    def __repr__(self):
        return "TargetReport({!r}, status={!r}, error={!r})".format(
            self.target.dst_path, self.status, self.error
        )

    @property
    def dst_fs(self):
        # type: () -> FS
        """Destination filesystem."""
        return self.target.dst_fs

    @property
    def dst_path(self):
        # type: () -> Text
        """Path of the file on the destination filesystem."""
        return self.target.dst_path

    @property
    def verified(self):
        # type: () -> bool
        """`True` if the copy was verified for this destination."""
        return self.status is TargetStatus.VERIFIED


class CopyReport(object):
    """The result of executing a `~fs.copyplan.CopyPlan`.

    Every destination is accounted for independently: a failed
    destination does not affect the verified copies of the other
    destinations.
    """

    def __init__(self, plan, results):
        # type: (CopyPlan, List[TargetReport]) -> None
        self.plan = plan
        self.results = list(results)

    def __repr__(self):
        return "CopyReport(verified={}, failed={}, skipped={})".format(
            len(self.verified), len(self.failed), len(self.skipped)
        )

    @property
    def success(self):
        # type: () -> bool
        """`True` if every destination was copied and verified."""
        return bool(self.results) and all(
            result.status is TargetStatus.VERIFIED for result in self.results
        )

    @property
    def verified(self):
        # type: () -> List[TargetReport]
        """Reports of destinations with a verified copy."""
        return [
            result
            for result in self.results
            if result.status is TargetStatus.VERIFIED
        ]

    @property
    def failed(self):
        # type: () -> List[TargetReport]
        """Reports of destinations where the copy failed."""
        return [
            result for result in self.results if result.status is TargetStatus.FAILED
        ]

    @property
    def skipped(self):
        # type: () -> List[TargetReport]
        """Reports of destinations that were not attempted."""
        return [
            result
            for result in self.results
            if result.status is TargetStatus.SKIPPED
        ]


class CopyPlan(object):
    """A plan to copy one file to one or more destinations.

    Create a plan with `~fs.copyplan.plan_copy`, inspect the per
    destination `~fs.copyplan.TargetPlan` objects, then call
    `~fs.copyplan.CopyPlan.execute` to perform the copies.

    If the plan was created from filesystem URLs, executing (or
    closing) the plan closes the filesystems it opened. A plan may
    also be used as a context manager.
    """

    def __init__(
        self,
        src_fs,  # type: FS
        src_path,  # type: Text
        preserve,  # type: FrozenSet[Text]
        fingerprint,  # type: Dict[Text, object]
        src_modified,  # type: Optional[float]
        src_accessed,  # type: Optional[float]
        src_permissions,  # type: object
        targets,  # type: List[TargetPlan]
        verify_hash=False,  # type: bool
        managed=None,  # type: Optional[List[typing.ContextManager[FS]]]
    ):
        # type: (...) -> None
        self.src_fs = src_fs
        self.src_path = src_path
        self.preserve = frozenset(preserve)
        self.fingerprint = fingerprint
        self.src_modified = src_modified
        self.src_accessed = src_accessed
        self.src_permissions = src_permissions
        self.targets = list(targets)
        self.verify_hash = verify_hash
        self._managed = list(managed) if managed else []

    def __repr__(self):
        return "CopyPlan({!r}, targets={!r})".format(self.src_path, self.targets)

    def __enter__(self):
        # type: () -> CopyPlan
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        # type: (object, object, object) -> None
        self.close()

    @property
    def feasible(self):
        # type: () -> bool
        """`True` if every destination can satisfy the requirements."""
        return bool(self.targets) and all(
            target.feasible for target in self.targets
        )

    @property
    def unpreservable(self):
        # type: () -> FrozenSet[Text]
        """Required attributes that at least one destination can not preserve."""
        unpreservable = set()  # type: typing.Set[Text]
        for target in self.targets:
            unpreservable.update(target.unpreservable)
        return frozenset(unpreservable)

    def close(self):
        # type: () -> None
        """Close any filesystems opened by `~fs.copyplan.plan_copy`."""
        while self._managed:
            manager = self._managed.pop()
            manager.__exit__(None, None, None)

    def execute(self):
        # type: () -> CopyReport
        """Execute the plan, copying to every destination independently.

        Destinations whose plan is not feasible are skipped. Every
        other destination is copied and verified on its own; a failure
        on one destination is reported but never rolls back another
        destination's verified copy.

        Returns:
            CopyReport: the per destination results.

        """
        try:
            return execute_plan(self)
        finally:
            self.close()


def plan_copy(
    src_fs,  # type: FS
    src_path,  # type: Text
    targets,  # type: Iterable[_Target]
    preserve=PRESERVE_ALL,  # type: Iterable[Text]
    verify_hash=False,  # type: bool
):
    # type: (...) -> CopyPlan
    """Plan a copy of one file to one or more destinations.

    Arguments:
        src_fs (FS or str): Source filesystem (instance or URL).
        src_path (str): Path to a file on the source filesystem.
        targets (iterable): Iterable of ``(dst_fs, dst_path)`` pairs,
            where ``dst_fs`` is a filesystem instance or a FS URL.
        preserve (iterable): Attributes that must be preserved; any of
            ``"content"``, ``"permissions"`` and ``"times"`` (defaults
            to all three).
        verify_hash (bool): If `True`, fingerprint and verify the file
            contents with a hash (slower, but detects content changes
            that keep size and mtime).

    Returns:
        CopyPlan: a plan describing, for each destination, the selected
        copy method and the required attributes that can not be
        preserved.

    Raises:
        fs.errors.FileExpected: If ``src_path`` is not a file.
        fs.errors.ResourceNotFound: If ``src_path`` does not exist.
        ValueError: If ``preserve`` contains an unknown attribute.

    """
    preserve_set = frozenset(preserve)
    unknown = preserve_set - PRESERVE_ALL
    if unknown:
        raise ValueError(
            "unknown preserve attribute(s): {}".format(", ".join(sorted(unknown)))
        )

    managed = []  # type: List[typing.ContextManager[FS]]

    def _open(fs_like, **kwargs):
        # type: (object, **object) -> FS
        if isinstance(fs_like, six.string_types):
            manager = manage_fs(fs_like, **kwargs)
            managed.append(manager)
            return manager.__enter__()
        return typing.cast("FS", fs_like)

    try:
        _src_fs = _open(src_fs, writeable=False)
        src_info = _src_fs.getinfo(src_path, ["details", "access"])
        if src_info.is_dir:
            raise errors.FileExpected(src_path)

        fingerprint = _fingerprint(_src_fs, src_path, verify_hash=verify_hash)
        src_modified = src_info.get("details", "modified")
        src_accessed = src_info.get("details", "accessed")
        try:
            src_permissions = src_info.permissions
        except errors.MissingInfoNamespace:
            src_permissions = None

        target_plans = []
        for dst_fs_like, dst_path in targets:
            _dst_fs = _open(dst_fs_like, create=True)
            method, reason = _select_method(_src_fs, src_path, _dst_fs, dst_path)
            preservable = set()  # type: typing.Set[Text]
            if method is not CopyMethod.UNSUPPORTED:
                if PRESERVE_CONTENT in preserve_set:
                    preservable.add(PRESERVE_CONTENT)
                if PRESERVE_TIMES in preserve_set and _supports_attribute(
                    _dst_fs, _META_MODIFIED_TIME, "details", "modified"
                ):
                    preservable.add(PRESERVE_TIMES)
                if PRESERVE_PERMISSIONS in preserve_set and _supports_attribute(
                    _dst_fs, _META_PERMISSIONS, "access", "permissions"
                ):
                    preservable.add(PRESERVE_PERMISSIONS)
            unpreservable = preserve_set - preservable
            target_plans.append(
                TargetPlan(
                    _dst_fs, dst_path, method, preservable, unpreservable, reason
                )
            )
    except Exception:
        for manager in reversed(managed):
            manager.__exit__(None, None, None)
        raise

    return CopyPlan(
        _src_fs,
        src_path,
        preserve_set,
        fingerprint,
        src_modified,
        src_accessed,
        src_permissions,
        target_plans,
        verify_hash=verify_hash,
        managed=managed,
    )


def execute_plan(plan):
    # type: (CopyPlan) -> CopyReport
    """Execute a copy plan.

    See `~fs.copyplan.CopyPlan.execute` for details.
    """
    results = []
    for target in plan.targets:
        if not target.feasible:
            results.append(TargetReport(target, TargetStatus.SKIPPED))
            continue
        try:
            bytes_copied, preserved = _execute_target(plan, target)
        except Exception as error:
            results.append(TargetReport(target, TargetStatus.FAILED, error=error))
        else:
            results.append(
                TargetReport(
                    target,
                    TargetStatus.VERIFIED,
                    bytes_copied=bytes_copied,
                    preserved=preserved,
                )
            )
    return CopyReport(plan, results)


def _execute_target(plan, target):
    # type: (CopyPlan, TargetPlan) -> Tuple[int, FrozenSet[Text]]
    """Copy to a single destination and verify the requirements.

    The source is fingerprinted before and after the content copy; if
    it changed since the plan was created, `SourceChanged` is raised
    and the planned metadata is *not* applied to the new content.
    """
    src_fs = plan.src_fs
    src_path = plan.src_path
    dst_fs = target.dst_fs
    dst_path = target.dst_path

    if _fingerprint(src_fs, src_path, plan.verify_hash) != plan.fingerprint:
        raise SourceChanged(
            "source {!r} changed after the copy plan was created".format(src_path)
        )

    copy_file_internal(src_fs, src_path, dst_fs, dst_path, lock=True)

    if _fingerprint(src_fs, src_path, plan.verify_hash) != plan.fingerprint:
        raise SourceChanged(
            "source {!r} changed while it was being copied".format(src_path)
        )

    preserved = set()  # type: typing.Set[Text]

    if PRESERVE_CONTENT in plan.preserve:
        if dst_fs.getsize(dst_path) != plan.fingerprint["size"]:
            raise PreservationError(
                "copied size does not match the source for {!r}".format(dst_path)
            )
        if plan.verify_hash and _hash_file(dst_fs, dst_path) != plan.fingerprint["md5"]:
            raise PreservationError(
                "copied content does not match the source for {!r}".format(dst_path)
            )
        preserved.add(PRESERVE_CONTENT)

    if PRESERVE_TIMES in plan.preserve:
        details = {}
        if plan.src_modified is not None:
            details["modified"] = plan.src_modified
        if plan.src_accessed is not None:
            details["accessed"] = plan.src_accessed
        if details:
            dst_fs.setinfo(dst_path, {"details": details})
        if plan.src_modified is not None:
            dst_modified = dst_fs.getinfo(dst_path, ["details"]).get(
                "details", "modified"
            )
            if (
                dst_modified is None
                or abs(dst_modified - plan.src_modified) > TIME_TOLERANCE
            ):
                raise PreservationError(
                    "modified time was not preserved on {!r}".format(dst_path)
                )
        preserved.add(PRESERVE_TIMES)

    if PRESERVE_PERMISSIONS in plan.preserve:
        if plan.src_permissions is not None:
            dst_fs.setinfo(
                dst_path,
                {"access": {"permissions": plan.src_permissions.dump()}},
            )
            try:
                dst_permissions = dst_fs.getinfo(dst_path, ["access"]).permissions
            except errors.MissingInfoNamespace:
                dst_permissions = None
            if dst_permissions != plan.src_permissions:
                raise PreservationError(
                    "permissions were not preserved on {!r}".format(dst_path)
                )
        preserved.add(PRESERVE_PERMISSIONS)

    return dst_fs.getsize(dst_path), frozenset(preserved)
