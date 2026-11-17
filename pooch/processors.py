# Copyright (c) 2018 The Pooch Developers.
# Distributed under the terms of the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause
#
# This code is part of the Fatiando a Terra project (https://www.fatiando.org)
#

"""
Post-processing hooks
"""

import abc
import bz2
import gzip
import lzma
import os
import shutil
import typing
from tarfile import TarFile
from zipfile import ZipFile

from .utils import get_logger


# Conflict resolution strategies for archive members that would end up writing
# to the same location on disk (duplicate members, or distinct member names
# that normalize to the same output path).
CONFLICT_STRATEGIES = ("error", "overwrite", "skip")


def _normalize_member_name(member_name):
    """
    Normalize an archive member name using Unix-style path semantics.

    Both ``/`` and ``\\`` separators are handled so backslash-based traversal
    cannot slip through on POSIX systems.
    """
    return os.path.normpath(str(member_name).replace("\\", "/"))


def _resolve_member_path(extract_dir, member_name):
    """
    Resolve an archive member name to an output path constrained to the root.

    The member name is normalized and joined to *extract_dir*. A
    :class:`ValueError` is raised if the resolved path escapes *extract_dir*
    through absolute paths, drive letters or ``..`` directory traversal.

    Parameters
    ----------
    extract_dir : str or pathlib.Path
        Root directory the output path must stay within.
    member_name : str
        Member name as stored in the archive.

    Returns
    -------
    destination : str
        The absolute, root-constrained path the member would be written to.
    """
    root = os.path.abspath(os.fspath(extract_dir))
    normalized = _normalize_member_name(member_name)
    if os.path.isabs(str(member_name)) or os.path.splitdrive(str(member_name))[0]:
        # An absolute member (or a Windows drive letter) must be rebased onto
        # the extraction root instead of writing to its original location.
        normalized = normalized.lstrip(os.path.sep)
    destination = os.path.abspath(os.path.join(root, normalized))
    if destination != root and not destination.startswith(root + os.path.sep):
        message = (
            f"Illegal archive member '{member_name}': it resolves outside of "
            f"the extraction directory '{root}'."
        )
        raise ValueError(message)
    return destination


def _expand_requested_members(available_names, requested):
    """
    Expand requested member names against the archive's member list.

    A requested name matches the identically named archive member and, for
    directories, every member nested below it. Comparisons use normalized
    names, so leading ``./`` segments and mixed separators are supported.

    Parameters
    ----------
    available_names : list of str
        Normalized names of every member in the archive, in archive order.
    requested : list of str or None
        Member names requested by the user. If None or empty, every available
        member is returned.

    Returns
    -------
    selected : list of str
        The normalized archive names to extract, preserving archive order.
    """
    if not requested:
        return list(available_names)
    wanted = [_normalize_member_name(name) for name in requested]
    selected = []
    for available in available_names:
        for name in wanted:
            if available == name or available.startswith(name + os.path.sep):
                selected.append(available)
                break
    return selected


def _build_extraction_manifest(member_records, extract_dir, on_conflict):
    """
    Build a root-constrained manifest of what an extraction must write.

    Each record is a ``(name, kind, payload)`` tuple where *kind* is
    ``"file"`` or ``"dir"`` and *payload* is a callable taking the
    destination path and materializing the member there (or ``None`` for
    directories).

    Parameters
    ----------
    member_records : iterable of tuple
        The archive members in their original order.
    extract_dir : str or pathlib.Path
        Root directory for the extraction.
    on_conflict : str
        Strategy when two members resolve to the same output path:

        * ``"error"``: raise :class:`ValueError`.
        * ``"overwrite"``: keep the last member, mirroring the historical
          behaviour of ``extractall``.
        * ``"skip"``: keep the first member and ignore later duplicates.

    Returns
    -------
    manifest : dict
        Mapping of normalized absolute destination paths to
        ``(destination, kind, payload)`` entries in first-seen order.
    """
    if on_conflict not in CONFLICT_STRATEGIES:
        message = (
            f"Invalid conflict strategy '{on_conflict}'. Must be one of "
            f"{list(CONFLICT_STRATEGIES)}."
        )
        raise ValueError(message)
    manifest = {}
    for member_name, kind, payload in member_records:
        destination = _resolve_member_path(extract_dir, member_name)
        destination_key = os.path.normcase(os.path.abspath(destination))
        if destination_key in manifest:
            if on_conflict == "error":
                message = (
                    f"Duplicate archive member '{member_name}' resolves to "
                    f"'{destination}' which is already provided by another "
                    "member. Choose an explicit 'on_conflict' strategy "
                    f"({list(CONFLICT_STRATEGIES)})."
                )
                raise ValueError(message)
            if on_conflict == "skip":
                get_logger().info(
                    "Skipping duplicate archive member '%s' ('%s' exists).",
                    member_name,
                    destination,
                )
                continue
            get_logger().info(
                "Overwriting '%s' with duplicate archive member '%s'.",
                destination,
                member_name,
            )
        manifest[destination_key] = (destination, kind, payload)
    return manifest


def _materialize_manifest(manifest):
    """
    Write the members described by *manifest* to disk.

    Directories are created first so file payloads can rely on them.
    """
    for destination, kind, _ in manifest.values():
        if kind == "dir":
            os.makedirs(destination, exist_ok=True)
    for destination, kind, payload in manifest.values():
        if kind == "file":
            parent = os.path.dirname(destination)
            if parent:
                os.makedirs(parent, exist_ok=True)
            payload(destination)


class ExtractorProcessor(abc.ABC):
    """
    Abstract base class for extractions from compressed archives.

    Subclasses can be used with :meth:`pooch.Pooch.fetch` and
    :func:`pooch.retrieve` to unzip a downloaded data file into a folder in the
    local data store. :meth:`~pooch.Pooch.fetch` will return a list with the
    names of the extracted files instead of the archive.

    Parameters
    ----------
    members : list or None
        If None, will unpack all files in the archive. Otherwise, *members*
        must be a list of file names to unpack from the archive. Only these
        files will be unpacked.
    extract_dir : str or None
        If None, files will be unpacked to the default location (a folder in
        the same location as the downloaded zip file, with a suffix added).
        Otherwise, files will be unpacked to ``extract_dir``, which is
        interpreted as a *relative path* (relative to the cache location
        provided by :func:`pooch.retrieve` or :meth:`pooch.Pooch.fetch`).
    on_conflict : str
        Strategy used when two archive members resolve to the same output
        path inside the extraction directory (duplicate members or names that
        normalize to the same file). One of:

        * ``"error"`` (default): raise a :class:`ValueError` listing the
          colliding members.
        * ``"overwrite"``: the last member wins, matching the traditional
          ``extractall`` behaviour.
        * ``"skip"``: the first member wins and later duplicates are ignored.

        Members whose resolved path would escape the extraction directory
        through ``..`` traversal, absolute paths or drive letters are always
        rejected with a :class:`ValueError`, regardless of this setting.

    """

    def __init__(self, members=None, extract_dir=None, on_conflict="error"):
        if on_conflict not in CONFLICT_STRATEGIES:
            message = (
                f"Invalid conflict strategy '{on_conflict}'. Must be one of "
                f"{list(CONFLICT_STRATEGIES)}."
            )
            raise ValueError(message)
        self.members = members
        self.extract_dir = extract_dir
        self.on_conflict = on_conflict

    @property
    @abc.abstractmethod
    def suffix(self):
        """
        String appended to unpacked archive folder name.
        Only used if extract_dir is None.
        MUST BE IMPLEMENTED BY CHILD CLASSES.
        """

    @abc.abstractmethod
    def _all_members(self, fname):
        """
        Return all the member names in the archive.
        MUST BE IMPLEMENTED BY CHILD CLASSES.
        """

    @abc.abstractmethod
    def _extract_file(self, fname, extract_dir):
        """
        Extract the archive into *extract_dir* using a root-constrained
        manifest and the configured conflict strategy.
        MUST BE IMPLEMENTED BY CHILD CLASSES.
        """

    def _selected_members(self, fname):
        """
        Return the member names this processor is configured to unpack.
        """
        if self.members is None or not self.members:
            return self._all_members(fname)
        return list(self.members)

    def __call__(self, fname, action, pooch):  # noqa: ARG002
        """
        Extract all files from the given archive.

        Parameters
        ----------
        fname : str
            Full path of the zipped file in local storage.
        action : str
            Indicates what action was taken by :meth:`pooch.Pooch.fetch` or
            :func:`pooch.retrieve`:

            * ``"download"``: File didn't exist locally and was downloaded
            * ``"update"``: Local file was outdated and was re-download
            * ``"fetch"``: File exists and is updated so it wasn't downloaded

        pooch : :class:`pooch.Pooch`
            The instance of :class:`pooch.Pooch` that is calling this.

        Returns
        -------
        fnames : list of str
            A list of the full path to all files in the extracted archive.

        """
        if self.extract_dir is None:
            self.extract_dir = fname + self.suffix
        else:
            archive_dir = fname.rsplit(os.path.sep, maxsplit=1)[0]
            self.extract_dir = os.path.join(archive_dir, self.extract_dir)
        # Resolve the expected members against the extraction root. A
        # malicious member name raises here instead of probing arbitrary
        # paths during the existence check.
        members = self._selected_members(fname)
        expected_paths = [
            _resolve_member_path(self.extract_dir, member) for member in members
        ]
        if (
            (action in ("update", "download"))
            or (not os.path.exists(self.extract_dir))
            or not all(os.path.exists(path) for path in expected_paths)
        ):
            # Make sure that the folder with the extracted files exists
            os.makedirs(self.extract_dir, exist_ok=True)
            self._extract_file(fname, self.extract_dir)

        # Return the files owned by this extraction. Matching is done on
        # absolute, root-constrained paths with a directory boundary so that
        # similarly named siblings cannot leak into the result.
        resolved_members = None
        if self.members:
            resolved_members = [
                os.path.normcase(
                    os.path.abspath(_resolve_member_path(self.extract_dir, m))
                )
                for m in self.members
            ]
        fnames = []
        for path, _, files in os.walk(self.extract_dir):
            for filename in files:
                full_path = os.path.normcase(
                    os.path.abspath(os.path.join(path, filename))
                )
                if resolved_members is None or any(
                    full_path == member_path
                    or full_path.startswith(member_path + os.path.sep)
                    for member_path in resolved_members
                ):
                    fnames.append(os.path.join(path, filename))

        return fnames


class Unzip(ExtractorProcessor):
    """
    Processor that unpacks a zip archive and returns a list of all files.

    Use with :meth:`pooch.Pooch.fetch` or :func:`pooch.retrieve` to unzip a
    downloaded data file into a folder in the local data store. The
    method/function will return a list with the names of the unzipped files
    instead of the zip archive.

    The output folder is ``{fname}.unzip``.

    Parameters
    ----------
    members : list or None
        If None, will unpack all files in the zip archive. Otherwise, *members*
        must be a list of file names to unpack from the archive. Only these
        files will be unpacked.
    extract_dir : str or None
        If None, files will be unpacked to the default location (a folder in
        the same location as the downloaded zip file, with the suffix
        ``.unzip`` added). Otherwise, files will be unpacked to
        ``extract_dir``, which is interpreted as a *relative path* (relative to
        the cache location provided by :func:`pooch.retrieve` or
        :meth:`pooch.Pooch.fetch`).
    on_conflict : str
        Strategy used when two archive members resolve to the same output
        path. See :class:`ExtractorProcessor` for the available options.
        Defaults to ``"error"``.

    """

    @property
    def suffix(self):
        """
        String appended to unpacked archive folder name.
        Only used if extract_dir is None.
        """
        return ".unzip"

    def _all_members(self, fname):
        """Return all members from a given archive."""
        with ZipFile(fname, "r") as zip_file:
            return zip_file.namelist()

    def _extract_file(self, fname, extract_dir):
        """
        Extract the zip archive into *extract_dir* using a root-constrained
        manifest and the configured conflict strategy.
        """
        if self.members:
            for member in self.members:
                get_logger().info(
                    "Extracting '%s' from '%s' to '%s'",
                    member,
                    fname,
                    extract_dir,
                )
        else:
            get_logger().info(
                "Unzipping contents of '%s' to '%s'", fname, extract_dir
            )
        with ZipFile(fname, "r") as zip_file:
            infos = zip_file.infolist()
            available = [_normalize_member_name(info.filename) for info in infos]
            # Use a set for membership checks but keep iterating the original
            # infos so duplicate member names keep their distinct payloads.
            selected = set(_expand_requested_members(available, self.members))
            records = []
            for info, normalized_name in zip(infos, available):
                if normalized_name not in selected:
                    continue
                if info.is_dir():
                    records.append((info.filename, "dir", None))
                    continue

                def make_payload(zip_info):
                    def payload(destination):
                        with zip_file.open(zip_info) as source, open(
                            destination, "w+b"
                        ) as output:
                            shutil.copyfileobj(source, output)

                    return payload

                records.append((info.filename, "file", make_payload(info)))
            manifest = _build_extraction_manifest(
                records, extract_dir, self.on_conflict
            )
            _materialize_manifest(manifest)


class Untar(ExtractorProcessor):
    """
    Processor that unpacks a tar archive and returns a list of all files.

    Use with :meth:`pooch.Pooch.fetch` or :func:`pooch.retrieve` to untar a
    downloaded data file into a folder in the local data store. It will return
    a list with the names of the extracted files instead of the archive.

    The output folder is ``{fname}.untar``.


    Parameters
    ----------
    members : list or None
        If None, will unpack all files in the archive. Otherwise, *members*
        must be a list of file names to unpack from the archive. Only these
        files will be unpacked.
    extract_dir : str or None
        If None, files will be unpacked to the default location (a folder in
        the same location as the downloaded tar file, with the suffix
        ``.untar`` added). Otherwise, files will be unpacked to
        ``extract_dir``, which is interpreted as a *relative path* (relative to
        the cache location  provided by :func:`pooch.retrieve` or
        :meth:`pooch.Pooch.fetch`).
    on_conflict : str
        Strategy used when two archive members resolve to the same output
        path. See :class:`ExtractorProcessor` for the available options.
        Defaults to ``"error"``.
    """

    @property
    def suffix(self):
        """
        String appended to unpacked archive folder name.
        Only used if extract_dir is None.
        """
        return ".untar"

    def _all_members(self, fname):
        """Return all members from a given archive."""
        with TarFile.open(fname, "r") as tar_file:
            return [info.name for info in tar_file.getmembers()]

    def _extract_file(self, fname, extract_dir):
        """
        Extract the tar archive into *extract_dir* using a root-constrained
        manifest and the configured conflict strategy.
        """
        if self.members:
            for member in self.members:
                get_logger().info(
                    "Extracting '%s' from '%s' to '%s'",
                    member,
                    fname,
                    extract_dir,
                )
        else:
            get_logger().info(
                "Untarring contents of '%s' to '%s'", fname, extract_dir
            )
        with TarFile.open(fname, "r") as tar_file:
            infos = tar_file.getmembers()
            available = [_normalize_member_name(info.name) for info in infos]
            # Keep iterating the original members so duplicate names retain
            # their distinct payloads instead of being collapsed in a dict.
            selected = set(_expand_requested_members(available, self.members))
            records = []
            for info, normalized_name in zip(infos, available):
                if normalized_name not in selected:
                    continue
                if info.isdir():
                    records.append((info.name, "dir", None))
                elif info.isfile():

                    def make_payload(tar_info):
                        def payload(destination):
                            source = tar_file.extractfile(tar_info)
                            if source is None:
                                message = (
                                    f"Cannot read tar member '{tar_info.name}' "
                                    "from the archive."
                                )
                                raise ValueError(message)
                            with source, open(destination, "w+b") as output:
                                shutil.copyfileobj(source, output)

                        return payload

                    records.append((info.name, "file", make_payload(info)))
                else:
                    message = (
                        f"Unsupported tar member '{info.name}' of type "
                        f"'{info.type.decode('ascii', errors='replace')}'. "
                        "Only regular files and directories can be extracted."
                    )
                    raise ValueError(message)
            manifest = _build_extraction_manifest(
                records, extract_dir, self.on_conflict
            )
            _materialize_manifest(manifest)


class Decompress:
    """
    Processor that decompress a file and returns the decompressed version.

    Use with :meth:`pooch.Pooch.fetch` or :func:`pooch.retrieve` to decompress
    a downloaded data file so that it can be easily opened. Useful for data
    files that take a long time to decompress (exchanging disk space for
    speed).

    Supported decompression methods are LZMA (``.xz``), bzip2 (``.bz2``), and
    gzip (``.gz``).

    File names with the standard extensions (see above) can use
    ``method="auto"`` to automatically determine the compression method. This
    can be overwritten by setting the *method* argument.

    .. note::

        To unpack zip and tar archives with one or more files, use
        :class:`pooch.Unzip` and :class:`pooch.Untar` instead.

    The output file is ``{fname}.decomp`` by default but it can be changed by
    setting the ``name`` parameter.

    .. warning::

        Passing in ``name`` can cause existing data to be lost! For example, if
        a file already exists with the specified name it will be overwritten
        with the new decompressed file content. **Use this option with
        caution.**

    Parameters
    ----------
    method : str
        Name of the compression method. Can be "auto", "lzma", "xz", "bzip2",
        or "gzip".
    name : None or str
        Defines the decompressed file name. The file name will be
        ``{fname}.decomp`` if ``None`` (default) or the given name otherwise.
        Note that the name should **not** include the full (or relative) path,
        it should be just the file name itself.

    """

    modules: typing.ClassVar = {
        "auto": None,
        "lzma": lzma,
        "xz": lzma,
        "gzip": gzip,
        "bzip2": bz2,
    }
    extensions: typing.ClassVar = {".xz": "lzma", ".gz": "gzip", ".bz2": "bzip2"}

    def __init__(self, method="auto", name=None):
        self.method = method
        self.name = name

    def __call__(self, fname, action, pooch):  # noqa: ARG002
        """
        Decompress the given file.

        The output file will be either ``{fname}.decomp`` or the given *name*
        class attribute.

        Parameters
        ----------
        fname : str
            Full path of the compressed file in local storage.
        action : str
            Indicates what action was taken by :meth:`pooch.Pooch.fetch` or
            :func:`pooch.retrieve`:

            - ``"download"``: File didn't exist locally and was downloaded
            - ``"update"``: Local file was outdated and was re-download
            - ``"fetch"``: File exists and is updated so it wasn't downloaded

        pooch : :class:`pooch.Pooch`
            The instance of :class:`pooch.Pooch` that is calling this.

        Returns
        -------
        fname : str
            The full path to the decompressed file.
        """
        if self.name is None:
            decompressed = fname + ".decomp"
        else:
            decompressed = os.path.join(os.path.dirname(fname), self.name)
        if action in ("update", "download") or not os.path.exists(decompressed):
            get_logger().info(
                "Decompressing '%s' to '%s' using method '%s'.",
                fname,
                decompressed,
                self.method,
            )
            module = self._compression_module(fname)
            with open(decompressed, "w+b") as output, module.open(fname) as compressed:
                shutil.copyfileobj(compressed, output)
        return decompressed

    def _compression_module(self, fname):
        """
        Get the Python module compatible with fname and the chosen method.

        If the *method* attribute is "auto", will select a method based on the
        extension. If no recognized extension is in the file name, will raise a
        ValueError.
        """
        error_archives = "To unpack zip/tar archives, use pooch.Unzip/Untar instead."
        if self.method not in self.modules:
            message = (
                f"Invalid compression method '{self.method}'. "
                f"Must be one of '{list(self.modules.keys())}'."
            )
            if self.method in {"zip", "tar"}:
                message = " ".join([message, error_archives])
            raise ValueError(message)
        if self.method == "auto":
            ext = os.path.splitext(fname)[-1]
            if ext not in self.extensions:
                message = (
                    f"Unrecognized file extension '{ext}'. "
                    f"Must be one of '{list(self.extensions.keys())}'."
                )
                if ext in {".zip", ".tar"}:
                    message = " ".join([message, error_archives])
                raise ValueError(message)
            return self.modules[self.extensions[ext]]
        return self.modules[self.method]
