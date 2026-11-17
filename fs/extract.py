"""Tools to safely export the members of an archive filesystem.

An archive (such as a zip or tar file) may be browsed as a filesystem
with `~fs.zipfs.ZipFS` or `~fs.tarfs.TarFS`. The `extract_fs` function
*collects* members from such a filesystem and exports them to a
*target root* on another filesystem, subject to explicit policies:

- Only *authorized* members are exported. If ``members`` is given, it
  is an allowlist of member paths; nothing else is exported.
- Member paths are preserved relative to the target root, after
  normalization. Members that would escape the target root are never
  written there.
- Duplicate names are handled according to the ``on_duplicate``
  policy.
- Links are handled according to the ``links`` policy; link targets
  are never followed.
- Member data is only ever read from the source filesystem itself. A
  member missing from the archive raises `~fs.errors.ResourceNotFound`;
  it is never substituted with a same-named file from any other
  (outer) filesystem.

"""

from __future__ import absolute_import, print_function, unicode_literals

import typing

from . import errors
from .copy import copy_modified_time
from .enums import ResourceType
from .opener import manage_fs
from .path import abspath, combine, dirname, isbase, normpath, relpath
from .walk import Walker

if typing.TYPE_CHECKING:
    from typing import Callable, Iterable, List, Optional, Text, Union

    from .base import FS

    _OnExtract = Callable[[FS, Text, FS, Text], object]


__all__ = ["extract_fs"]


#: Policies for handling duplicate names in the target root.
DUPLICATE_POLICIES = ("error", "skip", "replace")

#: Policies for handling links found in the source filesystem.
LINK_POLICIES = ("skip", "error")


def _normalize_member(path):
    # type: (Text) -> Text
    """Normalize a member path, relative to the filesystem root.

    Raises:
        IllegalBackReference: if the path contains back references
            that would escape the filesystem root.

    """
    return relpath(normpath(path))


def extract_fs(
    src_fs,  # type: Union[FS, Text]
    dst_fs,  # type: Union[FS, Text]
    dst_path="/",  # type: Text
    members=None,  # type: Optional[Iterable[Text]]
    walker=None,  # type: Optional[Walker]
    on_extract=None,  # type: Optional[_OnExtract]
    preserve_time=False,  # type: bool
    on_duplicate="replace",  # type: Text
    links="skip",  # type: Text
):
    # type: (...) -> List[Text]
    """Export the members of an archive filesystem to a target root.

    The source filesystem is browsed (an archive filesystem such as
    `~fs.zipfs.ReadZipFS` or `~fs.tarfs.ReadTarFS` is expected, but any
    filesystem may be used), and its members are copied to
    ``dst_path`` -- the *target root* -- on ``dst_fs``. Member paths
    are preserved relative to the target root, so the exported tree
    mirrors the layout of the archive.

    Arguments:
        src_fs (FS or str): The source (archive) filesystem, or a FS
            URL, to export members from.
        dst_fs (FS or str): The destination filesystem, or a FS URL.
        dst_path (str): Path of the target root on ``dst_fs``.
            Defaults to the root of ``dst_fs``.
        members (list, optional): An allowlist of member paths to
            export. If supplied, only those members are exported, in
            the given order. If `None` (the default), every member
            found by ``walker`` is exported.
        walker (~fs.walk.Walker, optional): A walker used to find
            members when ``members`` is `None`. Defaults to
            `~fs.walk.Walker`.
        on_extract (callable, optional): A callback called after each
            member is exported, with the signature ``(src_fs,
            src_path, dst_fs, dst_path)``.
        preserve_time (bool): If `True`, try to preserve the
            modification time of exported members (defaults to
            `False`).
        on_duplicate (str): Policy for a member whose target path
            already exists in the target root: ``"error"`` raises
            `~fs.errors.DestinationExists`, ``"skip"`` keeps the
            existing file, and ``"replace"`` (the default) overwrites
            it.
        links (str): Policy for symlink members: ``"skip"`` (the
            default) does not export them, and ``"error"`` raises
            `~fs.errors.Unsupported`. Link targets are never followed.

    Returns:
        list: the normalized paths of the exported members, relative
        to the target root.

    Raises:
        ValueError: if ``on_duplicate`` or ``links`` is not a
            supported policy.
        ~fs.errors.IllegalBackReference: if an authorized member path
            would escape the target root.
        ~fs.errors.ResourceNotFound: if an authorized member is not
            present in the source filesystem.

    Note:
        Member data is read exclusively from ``src_fs``. A member that
        is missing from the archive is reported with
        `~fs.errors.ResourceNotFound`; it is never filled in from a
        same-named file on another (outer) filesystem.

    """
    if on_duplicate not in DUPLICATE_POLICIES:
        raise ValueError(
            "on_duplicate must be one of {}; not {!r}".format(
                DUPLICATE_POLICIES, on_duplicate
            )
        )
    if links not in LINK_POLICIES:
        raise ValueError(
            "links must be one of {}; not {!r}".format(LINK_POLICIES, links)
        )
    on_extract = on_extract or (lambda *args: None)
    walker = walker or Walker()
    exported = []  # type: List[Text]
    with manage_fs(src_fs, writeable=False) as _src_fs:
        with manage_fs(dst_fs, create=True) as _dst_fs:
            dst_root = abspath(normpath(dst_path))
            with _src_fs.lock(), _dst_fs.lock():
                _dst_fs.makedirs(dst_root, recreate=True)
                if members is None:
                    candidates = (
                        (_normalize_member(path), path, info)
                        for path, info in walker.info(
                            _src_fs, namespaces=["details"]
                        )
                    )
                else:
                    candidates = (
                        (
                            _normalize_member(member),
                            member,
                            _src_fs.getinfo(member, namespaces=["details"]),
                        )
                        for member in members
                    )
                seen = set()
                for member_path, src_path, info in candidates:
                    if not member_path:
                        # The root of the archive maps to the target root.
                        continue
                    target = combine(dst_root, member_path)
                    if not isbase(dst_root, abspath(normpath(target))):
                        # Defense in depth; a normalized member path
                        # should never be able to escape the target root.
                        raise errors.IllegalBackReference(member_path)
                    if info.is_dir:
                        _dst_fs.makedir(target, recreate=True)
                        exported.append(member_path)
                        continue
                    if (
                        info.has_namespace("details")
                        and info.type is ResourceType.symlink
                    ):
                        if links == "error":
                            raise errors.Unsupported(
                                src_path, msg="exporting symlinks is not supported"
                            )
                        continue
                    if member_path in seen or _dst_fs.exists(target):
                        if on_duplicate == "error":
                            raise errors.DestinationExists(target)
                        elif on_duplicate == "skip":
                            continue
                        elif _dst_fs.isdir(target):
                            raise errors.DestinationExists(target)
                        else:
                            _dst_fs.remove(target)
                    _dst_fs.makedirs(dirname(target), recreate=True)
                    with _src_fs.openbin(src_path) as read_file:
                        _dst_fs.upload(target, read_file)
                    if preserve_time:
                        copy_modified_time(_src_fs, src_path, _dst_fs, target)
                    seen.add(member_path)
                    exported.append(member_path)
                    on_extract(_src_fs, src_path, _dst_fs, target)
    return exported
