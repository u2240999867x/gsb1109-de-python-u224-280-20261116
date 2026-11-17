# Copyright (c) 2018 The Pooch Developers.
# Distributed under the terms of the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause
#
# This code is part of the Fatiando a Terra project (https://www.fatiando.org)
#
"""
The main Pooch class and a factory function for it.
"""

import contextlib
import json
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, Union

from .downloaders import DOIDownloader, choose_downloader, doi_to_repository
from .hashes import file_hash, hash_algorithm, hash_matches
from .registry import (
    FileNotInRegistryError,
    ProvenanceRecord,
    RegistrySnapshot,
    RetractedFileError,
    apply_directives,
    parse_registry,
    registry_digest,
)
from .typing import Action, Downloader, PathInputType, PathType, Processor
from .utils import (
    cache_location,
    check_version,
    get_logger,
    make_local_storage,
    os_cache,
    temporary_file,
    unique_file_name,
)


def retrieve(
    url: str,
    known_hash: Optional[str] = None,
    fname: Optional[str] = None,
    path: Optional[PathType] = None,
    processor: Optional[Processor] = None,
    downloader: Optional[Downloader] = None,
    progressbar: bool = False,
) -> str:
    """
    Download and cache a single file locally.

    Uses HTTP or FTP by default, depending on the protocol in the given *url*.
    Other download methods can be controlled through the *downloader* argument
    (see below).

    The file will be downloaded to a temporary location first and its hash will
    be compared to the given *known_hash*. This is done to ensure that the
    download happened correctly and securely. If the hash doesn't match, the
    file will be deleted and an exception will be raised.

    If the file already exists locally, its hash will be compared to
    *known_hash*. If they are not the same, this is interpreted as the file
    needing to be updated and it will be downloaded again.

    You can bypass these checks by passing ``known_hash=None``. If this is
    done, the SHA256 hash of the downloaded file will be logged to the screen.
    It is highly recommended that you copy and paste this hash as *known_hash*
    so that future downloads are guaranteed to be the exact same file. This is
    crucial for reproducible computations.

    If the file exists in the given *path* with the given *fname* and the hash
    matches, it will not be downloaded and the absolute path to the file will
    be returned.

    .. note::

        This function is meant for downloading single files. If you need to
        manage the download and caching of several files, with versioning, use
        :func:`pooch.create` and :class:`pooch.Pooch` instead.

    Parameters
    ----------
    url : str
        The URL to the file that is to be downloaded. Ideally, the URL should
        end in a file name.
    known_hash : str or None
        A known hash (checksum) of the file. Will be used to verify the
        download or check if an existing file needs to be updated. By default,
        will assume it's a SHA256 hash. To specify a different hashing method,
        prepend the hash with ``algorithm:``, for example
        ``md5:pw9co2iun29juoh`` or ``sha1:092odwhi2ujdp2du2od2odh2wod2``. If
        None, will NOT check the hash of the downloaded file or check if an
        existing file needs to be updated.
    fname : str or None
        The name that will be used to save the file. Should NOT include the
        full path, just the file name (it will be appended to *path*). If
        None, will create a unique file name using a combination of the last
        part of the URL (assuming it's the file name) and the MD5 hash of the
        URL. For example, ``81whdo2d2e928yd1wi22-data-file.csv``. This ensures
        that files from different URLs never overwrite each other, even if they
        have the same name.
    path : str or PathLike or None
        The location of the cache folder on disk. This is where the file will
        be saved. If None, will save to a ``pooch`` folder in the default cache
        location for your operating system (see :func:`pooch.os_cache`).
    processor : None or callable
        If not None, then a function (or callable object) that will be called
        before returning the full path and after the file has been downloaded
        (if required). See :ref:`processors` for details.
    downloader : None or callable
        If not None, then a function (or callable object) that will be called
        to download a given URL to a provided local file name. See
        :ref:`downloaders` for details.
    progressbar : bool or an arbitrary progress bar object
        If True, will print a progress bar of the download to standard error
        (stderr). Requires `tqdm <https://github.com/tqdm/tqdm>`__ to be
        installed. Alternatively, an arbitrary progress bar object can be
        passed. See :ref:`custom-progressbar` for details.

    Returns
    -------
    full_path : str
        The absolute path (including the file name) of the file in the local
        storage.

    Examples
    --------

    Download one of the data files from the Pooch repository on GitHub:

    >>> import os
    >>> from pooch import __version__, check_version, retrieve
    >>> # Make a URL for the version of pooch we have installed
    >>> url = "https://github.com/fatiando/pooch/raw/{}/data/tiny-data.txt"
    >>> url = url.format(check_version(__version__, fallback="main"))
    >>> # Download the file and save it locally. Will check the MD5 checksum of
    >>> # the downloaded file against the given value to make sure it's the
    >>> # right file. You can use other hashes by specifying different
    >>> # algorithm names (sha256, sha1, etc).
    >>> fname = retrieve(
    ...     url, known_hash="md5:70e2afd3fd7e336ae478b1e740a5f08e",
    ... )
    >>> with open(fname) as f:
    ...     print(f.read().strip())
    # A tiny data file for test purposes only
    1  2  3  4  5  6
    >>> # Running again won't trigger a download and only return the path to
    >>> # the existing file.
    >>> fname2 = retrieve(
    ...     url, known_hash="md5:70e2afd3fd7e336ae478b1e740a5f08e",
    ... )
    >>> print(fname2 == fname)
    True
    >>> os.remove(fname)

    Files that are compressed with gzip, xz/lzma, or bzip2 can be automatically
    decompressed by passing using the :class:`pooch.Decompress` processor:

    >>> from pooch import Decompress
    >>> # URLs to a gzip compressed version of the data file.
    >>> url = ("https://github.com/fatiando/pooch/raw/{}/"
    ...        + "pooch/tests/data/tiny-data.txt.gz")
    >>> url = url.format(check_version(__version__, fallback="main"))
    >>> # By default, you would have to decompress the file yourself
    >>> fname = retrieve(
    ...     url,
    ...     known_hash="md5:8812ba10b6c7778014fdae81b03f9def",
    ... )
    >>> print(os.path.splitext(fname)[1])
    .gz
    >>> # Use the processor to decompress after download automatically and
    >>> # return the path to the decompressed file instead.
    >>> fname2 = retrieve(
    ...     url,
    ...     known_hash="md5:8812ba10b6c7778014fdae81b03f9def",
    ...     processor=Decompress(),
    ... )
    >>> print(fname2 == fname)
    False
    >>> with open(fname2) as f:
    ...     print(f.read().strip())
    # A tiny data file for test purposes only
    1  2  3  4  5  6
    >>> os.remove(fname)
    >>> os.remove(fname2)

    When downloading archives (zip or tar), it can be useful to unpack them
    after download to avoid having to do that yourself. Use the processors
    :class:`pooch.Unzip` or :class:`pooch.Untar` to do this automatically:

    >>> from pooch import Unzip
    >>> # URLs to a zip archive with a single data file.
    >>> url = ("https://github.com/fatiando/pooch/raw/{}/"
    ...        + "pooch/tests/data/tiny-data.zip")
    >>> url = url.format(check_version(__version__, fallback="main"))
    >>> # By default, you would get the path to the archive
    >>> fname = retrieve(
    ...     url,
    ...     known_hash="md5:e9592cb46cf3514a1079051f8a148148",
    ... )
    >>> print(os.path.splitext(fname)[1])
    .zip
    >>> os.remove(fname)
    >>> # Using the processor, the archive will be unzipped and a list with the
    >>> # path to every file will be returned instead of a single path.
    >>> fnames = retrieve(
    ...     url,
    ...     known_hash="md5:e9592cb46cf3514a1079051f8a148148",
    ...     processor=Unzip(),
    ... )
    >>> # There was only a single file in our archive.
    >>> print(len(fnames))
    1
    >>> with open(fnames[0]) as f:
    ...     print(f.read().strip())
    # A tiny data file for test purposes only
    1  2  3  4  5  6
    >>> for f in fnames:
    ...     os.remove(f)


    """
    if path is None:
        path = os_cache("pooch")
    if fname is None:
        fname = unique_file_name(url)
    # Make the path absolute.
    path = cache_location(path, env=None, version=None)

    full_path = path.resolve() / fname
    action, verb = download_action(full_path, known_hash)

    if action in ("download", "update"):
        # We need to write data, so create the local data directory if it
        # doesn't already exist.
        make_local_storage(path)

        get_logger().info(
            "%s data from '%s' to file '%s'.",
            verb,
            url,
            str(full_path),
        )

        if downloader is None:
            downloader = choose_downloader(url, progressbar=progressbar)

        stream_download(url, full_path, known_hash, downloader, pooch=None)

        if known_hash is None:
            get_logger().info(
                "SHA256 hash of downloaded file: %s\n"
                "Use this value as the 'known_hash' argument of 'pooch.retrieve'"
                " to ensure that the file hasn't changed if it is downloaded again"
                " in the future.",
                file_hash(str(full_path)),
            )

    if processor is not None:
        return processor(str(full_path), action, None)

    return str(full_path)


def create(
    path: PathInputType,
    base_url: str,
    version: Optional[str] = None,
    version_dev: str = "master",
    env: Optional[str] = None,
    registry: Optional[dict] = None,
    urls: Optional[dict] = None,
    retry_if_failed: int = 0,
    allow_updates: Union[bool, str] = True,
    registry_version: Optional[str] = None,
):
    """
    Create a :class:`~pooch.Pooch` with sensible defaults to fetch data files.

    If a version string is given, the Pooch will be versioned, meaning that the
    local storage folder and the base URL depend on the project version. This
    is necessary if your users have multiple versions of your library installed
    (using virtual environments) and you updated the data files between
    versions. Otherwise, every time a user switches environments would trigger
    a re-download of the data. The version string will be appended to the local
    storage path (for example, ``~/.mypooch/cache/v0.1``) and inserted into the
    base URL (for example,
    ``https://github.com/fatiando/pooch/raw/v0.1/data``). If the version string
    contains ``+XX.XXXXX``, it will be interpreted as a development version.

    Does **not** create the local data storage folder. The folder will only be
    created the first time a download is attempted with
    :meth:`pooch.Pooch.fetch`. This makes it safe to use this function at the
    module level (so it's executed on ``import`` and the resulting
    :class:`~pooch.Pooch` is a global variable).

    Parameters
    ----------
    path : str, PathLike, list or tuple
        The path to the local data storage folder. If this is a list or tuple,
        we'll join the parts with the appropriate separator. The *version* will
        be appended to the end of this path. Use :func:`pooch.os_cache` for a
        sensible default.
    base_url : str
        Base URL for the remote data source. All requests will be made relative
        to this URL. The string should have a ``{version}`` formatting mark in
        it. We will call ``.format(version=version)`` on this string. If the
        URL does not end in a ``'/'``, a trailing ``'/'`` will be added
        automatically.
    version : str or None
        The version string for your project. Should be PEP440 compatible. If
        None is given, will not attempt to format *base_url* and no subfolder
        will be appended to *path*.
    version_dev : str
        The name used for the development version of a project. If your data is
        hosted on Github (and *base_url* is a Github raw link), then
        ``"master"`` is a good choice (default). Ignored if *version* is None.
    env : str or None
        An environment variable that can be used to overwrite *path*. This
        allows users to control where they want the data to be stored. We'll
        append *version* to the end of this value as well.
    registry : dict or None
        A record of the files that are managed by this Pooch. Keys should be
        the file names and the values should be their hashes. Only files
        in the registry can be fetched from the local storage. Files in
        subdirectories of *path* **must use Unix-style separators** (``'/'``)
        even on Windows.
    urls : dict or None
        Custom URLs for downloading individual files in the registry. A
        dictionary with the file names as keys and the custom URLs as values.
        Not all files in *registry* need an entry in *urls*. If a file has an
        entry in *urls*, the *base_url* will be ignored when downloading it in
        favor of ``urls[fname]``.
    retry_if_failed : int
        Retry a file download the specified number of times if it fails because
        of a bad connection or a hash mismatch. By default, downloads are only
        attempted once (``retry_if_failed=0``). Initially, will wait for 1s
        between retries and then increase the wait time by 1s with each retry
        until a maximum of 10s.
    allow_updates : bool or str
        Whether existing files in local storage that have a hash mismatch with
        the registry are allowed to update from the remote URL. If a string is
        passed, we will assume it's the name of an environment variable that
        will be checked for the true/false value. If ``False``, any mismatch
        with hashes in the registry will result in an error. Defaults to
        ``True``.
    registry_version : str or None
        A label for the initial registry, used in logs and error messages. If
        None, the content digest of the registry is used. Fetches always use
        one complete registry snapshot, so a later registry refresh cannot
        change a fetch that is already running.

    Returns
    -------
    pooch : :class:`~pooch.Pooch`
        The :class:`~pooch.Pooch` initialized with the given arguments.

    Examples
    --------

    Create a :class:`~pooch.Pooch` for a release (v0.1):

    >>> pup = create(path="myproject",
    ...              base_url="http://some.link.com/{version}/",
    ...              version="v0.1",
    ...              registry={"data.txt": "9081wo2eb2gc0u..."})
    >>> print(pup.path.parts)  # The path is a pathlib.Path
    ('myproject', 'v0.1')
    >>> # The local folder is only created when a dataset is first downloaded
    >>> print(pup.path.exists())
    False
    >>> print(pup.base_url)
    http://some.link.com/v0.1/
    >>> print(pup.registry)
    {'data.txt': '9081wo2eb2gc0u...'}
    >>> print(pup.registry_files)
    ['data.txt']

    If this is a development version (12 commits ahead of v0.1), then the
    ``version_dev`` will be used (defaults to ``"master"``):

    >>> pup = create(path="myproject",
    ...              base_url="http://some.link.com/{version}/",
    ...              version="v0.1+12.do9iwd")
    >>> print(pup.path.parts)
    ('myproject', 'master')
    >>> print(pup.base_url)
    http://some.link.com/master/

    Versioning is optional (but highly encouraged):

    >>> pup = create(path="myproject",
    ...              base_url="http://some.link.com/",
    ...              registry={"data.txt": "9081wo2eb2gc0u..."})
    >>> print(pup.path.parts)  # The path is a pathlib.Path
    ('myproject',)
    >>> print(pup.base_url)
    http://some.link.com/

    To place the storage folder at a subdirectory, pass in a list and we'll
    join the path for you using the appropriate separator for your operating
    system:

    >>> pup = create(path=["myproject", "cache", "data"],
    ...              base_url="http://some.link.com/{version}/",
    ...              version="v0.1")
    >>> print(pup.path.parts)
    ('myproject', 'cache', 'data', 'v0.1')

    The user can overwrite the storage path by setting an environment variable:

    >>> # The variable is not set so we'll use *path*
    >>> pup = create(path=["myproject", "not_from_env"],
    ...              base_url="http://some.link.com/{version}/",
    ...              version="v0.1",
    ...              env="MYPROJECT_DATA_DIR")
    >>> print(pup.path.parts)
    ('myproject', 'not_from_env', 'v0.1')
    >>> # Set the environment variable and try again
    >>> import os
    >>> os.environ["MYPROJECT_DATA_DIR"] = os.path.join("myproject", "env")
    >>> pup = create(path=["myproject", "not_env"],
    ...              base_url="http://some.link.com/{version}/",
    ...              version="v0.1",
    ...              env="MYPROJECT_DATA_DIR")
    >>> print(pup.path.parts)
    ('myproject', 'env', 'v0.1')

    """
    if version is not None:
        version = check_version(version, fallback=version_dev)
        base_url = base_url.format(version=version)
    # Don't create the cache folder here! This function is usually called in
    # the module context (at import time), so touching the file system is not
    # recommended. It could cause crashes when multiple processes/threads try
    # to import at the same time (which would try to create the folder several
    # times at once).
    path = cache_location(path, env, version)
    if isinstance(allow_updates, str):
        allow_updates = os.environ.get(allow_updates, "true").lower() != "false"
    # add trailing "/"
    base_url = base_url.rstrip("/") + "/"
    pup = Pooch(
        path=path,
        base_url=base_url,
        registry=registry,
        urls=urls,
        retry_if_failed=retry_if_failed,
        allow_updates=allow_updates,
        registry_version=registry_version,
    )
    return pup


class Pooch:
    """
    Manager for a local data storage that can fetch from a remote source.

    Avoid creating ``Pooch`` instances directly. Use :func:`pooch.create`
    instead.

    Parameters
    ----------
    path : str
        The path to the local data storage folder. The path must exist in the
        file system.
    base_url : str
        Base URL for the remote data source. All requests will be made relative
        to this URL.
    registry : dict or None
        A record of the files that are managed by this good boy. Keys should be
        the file names and the values should be their hashes. Only files
        in the registry can be fetched from the local storage. Files in
        subdirectories of *path* **must use Unix-style separators** (``'/'``)
        even on Windows.
    urls : dict or None
        Custom URLs for downloading individual files in the registry. A
        dictionary with the file names as keys and the custom URLs as values.
        Not all files in *registry* need an entry in *urls*. If a file has an
        entry in *urls*, the *base_url* will be ignored when downloading it in
        favor of ``urls[fname]``.
    retry_if_failed : int
        Retry a file download the specified number of times if it fails because
        of a bad connection or a hash mismatch. By default, downloads are only
        attempted once (``retry_if_failed=0``). Initially, will wait for 1s
        between retries and then increase the wait time by 1s with each retry
        until a maximum of 10s.
    allow_updates : bool
        Whether existing files in local storage that have a hash mismatch with
        the registry are allowed to update from the remote URL. If ``False``,
        any mismatch with hashes in the registry will result in an error.
        Defaults to ``True``.
    registry_version : str or None
        A label for the initial registry. If None, the content digest of the
        registry is used as its version. Each fetch resolves its file against
        one complete registry snapshot, so changing the registry never changes
        the meaning of a fetch that is already running.

    """

    def __init__(
        self,
        path: PathType,
        base_url: str,
        registry: Optional[dict[str, str]] = None,
        urls: Optional[dict[str, str]] = None,
        retry_if_failed: int = 0,
        allow_updates: bool = True,
        registry_version: Optional[str] = None,
    ) -> None:
        self.path = path
        self.base_url = base_url
        if registry is None:
            registry = {}
        self._registry = dict(registry)
        if urls is None:
            urls = {}
        self._urls = dict(urls)
        self._retracted: dict[str, str] = {}
        self._retraction_reasons: dict[str, str] = {}
        self._snapshots: list[RegistrySnapshot] = []
        self._provenance: list[ProvenanceRecord] = []
        self._lock = threading.RLock()
        self.retry_if_failed = retry_if_failed
        self.allow_updates = allow_updates
        self._commit_registry(self._registry, self._urls, registry_version)
        self._load_provenance_index()

    def __repr__(self) -> str:
        return (
            f"Pooch(path={self.path!r}, base_url={self.base_url!r}, "
            f"registry_version={self.registry_version!r}, "
            f"files={len(self._registry)})"
        )

    @property
    def registry(self) -> dict[str, str]:
        """
        The files declared by the current registry.

        This mapping remains mutable for backwards compatibility. A fetch
        always takes a complete snapshot before it starts, so later mutations
        cannot affect a fetch that is already running. Mutating the returned
        mapping publishes a new snapshot the next time one is requested.
        """
        return self._registry

    @registry.setter
    def registry(self, value: dict[str, str]) -> None:
        current_urls = dict(self._urls)
        self._commit_registry(dict(value), current_urls, None)

    @property
    def urls(self) -> dict[str, str]:
        "Custom download URLs for the current registry."
        return self._urls

    @urls.setter
    def urls(self, value: dict[str, str]) -> None:
        with self._lock:
            self._urls = dict(value)

    @property
    def registry_snapshot(self) -> RegistrySnapshot:
        "The complete registry snapshot currently in use."
        return self._snapshot_for_current_state()

    @property
    def registry_version(self) -> str:
        "Version of the registry currently in use."
        return self.registry_snapshot.version

    @property
    def registry_digest(self) -> str:
        "Content digest of the registry currently in use."
        return self.registry_snapshot.digest

    @property
    def registry_history(self) -> tuple[RegistrySnapshot, ...]:
        "All registry snapshots that have been published, oldest first."
        with self._lock:
            return tuple(self._snapshots)

    @property
    def provenance(self) -> tuple[ProvenanceRecord, ...]:
        "Content that was preserved when it was superseded by a newer registry."
        with self._lock:
            return tuple(self._provenance)

    @property
    def abspath(self) -> Path:
        "Absolute path to the local storage"
        return Path(os.path.abspath(os.path.expanduser(str(self.path))))

    @property
    def registry_files(self) -> list[str]:
        "List of file names on the registry"
        return list(self.registry)

    def fetch(
        self,
        fname: str,
        processor: Optional[Processor] = None,
        downloader: Optional[Downloader] = None,
        progressbar: bool = False,
        registry_version: Optional[str] = None,
    ) -> str:
        """
        Get the absolute path to a file in the local storage.

        If it's not in the local storage, it will be downloaded. If the hash of
        the file in local storage doesn't match the one in the registry, will
        download a new copy of the file. This is considered a sign that the
        file was updated in the remote storage. If the hash of the downloaded
        file still doesn't match the one in the registry, will raise an
        exception to warn of possible file corruption.

        A fetch always takes one complete, immutable registry snapshot before
        it does anything else. If the registry is refreshed while the fetch is
        running, the fetch continues against the version it started with.

        Post-processing actions sometimes need to be taken on downloaded files
        (unzipping, conversion to a more efficient format, etc). If these
        actions are time or memory consuming, it would be best to do this only
        once right after the file is downloaded. Use the *processor* argument
        to specify a function that is executed after the download to perform
        these actions. See :ref:`processors` for details.

        Custom file downloaders can be provided through the *downloader*
        argument. By default, Pooch will determine the download protocol from
        the URL in the registry. If the server for a given file requires
        authentication (username and password), use a downloader that support
        these features. Downloaders can also be used to print custom messages
        (like a progress bar), etc. See :ref:`downloaders` for details.

        Parameters
        ----------
        fname : str
            The file name (relative to the *base_url* of the remote data
            storage) to fetch from the local storage.
        processor : None or callable
            If not None, then a function (or callable object) that will be
            called before returning the full path and after the file has been
            downloaded. See :ref:`processors` for details.
        downloader : None or callable
            If not None, then a function (or callable object) that will be
            called to download a given URL to a provided local file name. See
            :ref:`downloaders` for details.
        progressbar : bool or an arbitrary progress bar object
            If True, will print a progress bar of the download to standard
            error (stderr). Requires `tqdm <https://github.com/tqdm/tqdm>`__ to
            be installed. Alternatively, an arbitrary progress bar object can
            be passed. See :ref:`custom-progressbar` for details.
        registry_version : str or None
            Pin the fetch to a specific registry version instead of the
            current one. This is equivalent to using
            :meth:`fetch_version`.

        Returns
        -------
        full_path : str
            The absolute path (including the file name) of the file in the
            local storage.

        """
        if registry_version is not None:
            return self.fetch_version(
                fname,
                version=registry_version,
                processor=processor,
                downloader=downloader,
                progressbar=progressbar,
            )
        return self._fetch_snapshot(
            self.registry_snapshot,
            fname,
            processor=processor,
            downloader=downloader,
            progressbar=progressbar,
        )

    def _fetch_snapshot(
        self,
        snapshot: RegistrySnapshot,
        fname: str,
        processor: Optional[Processor] = None,
        downloader: Optional[Downloader] = None,
        progressbar: bool = False,
        version_cache: bool = False,
    ) -> str:
        """
        Fetch a file against one complete, immutable registry snapshot.

        When *version_cache* is True, the file is resolved in a folder named
        after the snapshot digest unless preserved content with the right hash
        is already available. This prevents a pinned old version from reading
        or overwriting content that belongs to a newer version.
        """
        if snapshot.is_retracted(fname):
            raise RetractedFileError(self._retraction_message(snapshot, fname))
        if fname not in snapshot.registry:
            msg = (
                f"File '{fname}' is not in the registry (version '{snapshot.version}')."
            )
            raise FileNotInRegistryError(msg)

        url = snapshot.get_url(fname)
        known_hash = snapshot.registry[fname]
        if version_cache and snapshot.digest != self.registry_digest:
            preserved = self._preserved_path(fname, known_hash)
            if preserved is not None:
                if processor is not None:
                    return processor(str(preserved), "fetch", self)
                return str(preserved)
            full_path = self._version_cache_root(snapshot) / fname
        else:
            full_path = self.abspath / fname
        action, verb = download_action(full_path, known_hash)

        if action == "update" and not self.allow_updates:
            msg = f"{fname} needs to update {full_path} but updates are disallowed."
            raise ValueError(msg)

        if action in ("download", "update"):
            # We need to write data, so create the local data directory if it
            # doesn't already exist.
            make_local_storage(str(full_path.parent))

            get_logger().info(
                "%s file '%s' from '%s' to '%s'.",
                verb,
                fname,
                url,
                str(self.abspath),
            )
            get_logger().debug(
                "Fetching '%s' from registry version '%s' (%s).",
                fname,
                snapshot.version,
                snapshot.digest,
            )

            if downloader is None:
                downloader = choose_downloader(url, progressbar=progressbar)

            stream_download(
                url,
                full_path,
                known_hash,
                downloader,
                pooch=self,
                retry_if_failed=self.retry_if_failed,
            )

        if processor is not None:
            return processor(str(full_path), action, self)

        return str(full_path)

    def _assert_file_in_registry(self, fname: str) -> None:
        """
        Check if a file is declared in the current registry.

        Raises :class:`ValueError` if the file is absent and
        :class:`~pooch.registry.RetractedFileError` if it was explicitly
        retracted. These are kept separate on purpose: an absent file is
        simply not declared by the registry, while a retracted file is
        declared as unusable.
        """
        snapshot = self.registry_snapshot
        if snapshot.is_retracted(fname):
            raise RetractedFileError(self._retraction_message(snapshot, fname))
        if fname not in snapshot.registry:
            msg = (
                f"File '{fname}' is not in the registry (version '{snapshot.version}')."
            )
            raise FileNotInRegistryError(msg)

    @staticmethod
    def _retraction_message(snapshot: RegistrySnapshot, fname: str) -> str:
        reason = snapshot.reason(fname)
        message = (
            f"File '{fname}' was explicitly retracted from registry "
            f"version '{snapshot.version}'."
        )
        if reason:
            message += f" Reason: {reason}"
        return message

    def get_url(self, fname: str, registry_version: Optional[str] = None) -> str:
        """
        Get the full URL to download a file in the registry.

        Parameters
        ----------
        fname : str
            The file name (relative to the *base_url* of the remote data
            storage) to fetch from the local storage.
        registry_version : str or None
            Version of the registry to resolve the URL against. If None, use
            the current registry. A retracted file raises
            :class:`~pooch.registry.RetractedFileError` instead of a plain
            ``ValueError``.

        """
        return self.registry_at(registry_version).get_url(fname)

    def registry_at(self, version: Optional[str] = None) -> RegistrySnapshot:
        """
        Return a previously published registry snapshot by version.

        Parameters
        ----------
        version : str or None
            Version label or content digest of the snapshot. If None, return
            the current snapshot.

        Returns
        -------
        snapshot : RegistrySnapshot
            The immutable snapshot. It is safe to fetch files with it while
            the current registry continues to evolve.
        """
        if version is None:
            return self.registry_snapshot
        with self._lock:
            for snapshot in reversed(self._snapshots):
                if version in (snapshot.version, snapshot.digest):
                    return snapshot
        msg = f"Unknown registry version '{version}'."
        raise KeyError(msg)

    def fetch_version(
        self,
        fname: str,
        version: str,
        processor: Optional[Processor] = None,
        downloader: Optional[Downloader] = None,
        progressbar: bool = False,
    ) -> str:
        """
        Fetch a file using a pinned registry version.

        This resolves both the checksum and the URL against the given
        snapshot, even if the current registry has since changed or retracted
        the file. If content matching that snapshot has been preserved, its
        path is returned. Otherwise the file is downloaded into a folder named
        after the snapshot digest, never into the current version's folder.

        Parameters
        ----------
        fname : str
            The file name to fetch.
        version : str
            Version label or content digest of the registry to use.
        processor, downloader, progressbar
            Same as in :meth:`fetch`.

        Returns
        -------
        full_path : str
            The absolute path to the file in the local storage.
        """
        return self._fetch_snapshot(
            self.registry_at(version),
            fname,
            processor=processor,
            downloader=downloader,
            progressbar=progressbar,
            version_cache=True,
        )

    def _version_cache_root(self, snapshot: RegistrySnapshot) -> Path:
        """
        Cache folder used for content pinned to a non-current registry version.
        """
        folder = snapshot.digest.replace(":", "-")
        return self.abspath / ".pooch-versions" / folder

    def _preserved_path(self, fname: str, known_hash: str) -> Optional[Path]:
        """
        Return preserved content matching a filename and checksum, if any.
        """
        algorithm = hash_algorithm(known_hash)
        digest = known_hash.rsplit(":", 1)[-1].lower()
        with self._lock:
            records = tuple(self._provenance)
        for record in reversed(records):
            if (
                record.fname == fname
                and record.algorithm == algorithm
                and record.digest == digest
                and record.path.exists()
            ):
                return record.path
        return None

    def load_registry(
        self,
        fname: PathType,
        version: Optional[str] = None,
        replace: bool = True,
    ) -> RegistrySnapshot:
        """
        Load a complete registry file as a new registry version.

        Use this if you are managing many files.

        Each line of the file should have file name and its hash separated by
        a space. Hash can specify checksum algorithm using "alg:hash" format.
        In case no algorithm is provided, SHA256 is used by default.
        Only one file per line is allowed. Custom download URLs for individual
        files can be specified as a third element on the line. Line comments
        can be added and must be prepended with ``#``.

        The whole file is parsed and validated before the current registry is
        changed. If any line is invalid, the registry is left exactly as it
        was; a failed refresh never publishes a partial registry.

        A file that is simply missing from the new file is **not** considered
        retracted. Retractions must be written explicitly, for example::

            adios.csv retracted replaced by c137-v2.csv
            !cronen.csv withdrawn after calibration error

        A later entry for a retracted file revives it.

        Parameters
        ----------
        fname : str | fileobj
            Path (or open file object) to the registry file.
        version : str or None
            Optional label for the resulting snapshot. If None, the content
            digest is used, which keeps every distinct registry traceable
            without claiming a release label it does not have.
        replace : bool
            If True (default), the file becomes the complete new registry and
            files that are not mentioned in it are removed. If False, entries
            are merged into the current registry (the historical behaviour).
            In neither case does an omitted file become a retraction; only
            explicit retraction lines do.

        Returns
        -------
        snapshot : RegistrySnapshot
            The immutable snapshot that was published.

        """
        with contextlib.ExitStack() as stack:
            if hasattr(fname, "read"):
                # It's a file object
                source = getattr(fname, "name", "<registry file object>")
                fin: Any = fname
            else:
                # It's a file path
                source = str(fname)
                fin = stack.enter_context(open(fname, encoding="utf-8"))

            directives = parse_registry(fin, source=source)

        with self._lock:
            registry, urls, retracted, reasons = apply_directives(
                self._registry,
                self._urls,
                self._retracted,
                self._retraction_reasons,
                directives,
                replace=replace,
            )
            self._preserve_superseded_entries(registry, retracted)
            return self._commit_registry(
                registry,
                urls,
                version,
                retracted=retracted,
                retraction_reasons=reasons,
                source=source,
            )

    def refresh_registry(
        self,
        source: Any,
        version: Optional[str] = None,
        replace: bool = True,
    ) -> RegistrySnapshot:
        """
        Fetch a registry file, validate it, and publish it atomically.

        *source* can be a path, a file object, or any callable that accepts a
        file-like object (for example, a downloader or a function that writes a
        registry to a temporary file). The current registry stays intact if
        loading the new one raises an exception. A successful refresh publishes
        a complete new snapshot; fetches already in progress keep using the old
        one.

        Parameters
        ----------
        source : path, file object, or callable
            Where to read the new registry from. A callable is given a binary
            file-like object and must write the complete registry into it.
        version : str or None
            Optional label for the new snapshot.
        replace : bool
            Whether the new file is the complete registry (default) or is
            merged into the current one.

        Returns
        -------
        snapshot : RegistrySnapshot
            The immutable snapshot that was published.
        """
        if hasattr(source, "read") or isinstance(source, (str, os.PathLike)):
            return self.load_registry(source, version=version, replace=replace)
        if not callable(source):
            msg = (
                "refresh_registry requires a path, a file object, or a callable "
                "that writes the registry."
            )
            raise TypeError(msg)
        with tempfile.NamedTemporaryFile("w+b") as tmp:
            source(tmp)
            tmp.flush()
            tmp.seek(0)
            return self.load_registry(tmp, version=version, replace=replace)

    def _commit_registry(
        self,
        registry: dict[str, str],
        urls: dict[str, str],
        version: Optional[str],
        retracted: Optional[dict[str, str]] = None,
        retraction_reasons: Optional[dict[str, str]] = None,
        source: Optional[str] = None,
    ) -> RegistrySnapshot:
        """
        Publish a complete registry state as one immutable snapshot.

        The state is built completely before it replaces the visible one, so
        an exception while constructing the snapshot leaves the old state
        untouched.
        """
        if retracted is None:
            retracted = self._retracted
        if retraction_reasons is None:
            retraction_reasons = self._retraction_reasons
        snapshot = RegistrySnapshot.from_state(
            registry=registry,
            urls=urls,
            retracted=retracted,
            retraction_reasons=retraction_reasons,
            base_url=self.base_url,
            version=version,
            source=source,
        )
        with self._lock:
            self._registry = dict(snapshot.registry)
            self._urls = dict(snapshot.urls)
            self._retracted = dict(retracted)
            self._retraction_reasons = dict(retraction_reasons)
            self._snapshots.append(snapshot)
        return snapshot

    def _snapshot_for_current_state(self) -> RegistrySnapshot:
        """
        Return a snapshot reflecting the current public registry state.

        Direct mutations of ``registry`` or ``urls`` are supported for
        backwards compatibility. Recompute the snapshot when that happens. If
        the mutation only reverts to an earlier digest, return that earlier
        snapshot rather than publishing a duplicate version.
        """
        with self._lock:
            digest = registry_digest(
                self._registry,
                self._urls,
                self._retracted,
                self._retraction_reasons,
                base_url=self.base_url,
            )
            if self._snapshots and self._snapshots[-1].digest == digest:
                return self._snapshots[-1]
            for snapshot in reversed(self._snapshots):
                if snapshot.digest == digest:
                    return snapshot
            snapshot = RegistrySnapshot.from_state(
                registry=self._registry,
                urls=self._urls,
                retracted=self._retracted,
                retraction_reasons=self._retraction_reasons,
                base_url=self.base_url,
            )
            self._snapshots.append(snapshot)
            return snapshot

    def _preserve_file(
        self,
        fname: str,
        path: Path,
        algorithm: str,
        digest: str,
        registry_version: str,
        known_hash: str,
        status: str = "superseded",
    ) -> Optional[ProvenanceRecord]:
        """
        Copy content into the history folder and record its provenance.
        """
        history_root = self._history_root()
        destination = history_root / algorithm / digest.lower()
        if not destination.exists():
            (history_root / algorithm).mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=history_root / algorithm, prefix=".copy-", delete=False
            ) as tmp:
                temporary = Path(tmp.name)
            try:
                shutil.copy2(path, temporary)
                os.replace(str(temporary), str(destination))
            finally:
                if temporary.exists():
                    temporary.unlink()
        for record in self._provenance:
            if (
                record.fname == fname
                and record.algorithm == algorithm
                and record.digest == digest.lower()
                and record.registry_version == registry_version
                and record.status == status
            ):
                return record
        record = ProvenanceRecord(
            fname=fname,
            path=destination,
            algorithm=algorithm,
            digest=digest.lower(),
            registry_version=registry_version,
            known_hash=known_hash,
            captured_at=datetime.now(timezone.utc).isoformat(),
            status=status,
        )
        self._provenance.append(record)
        self._write_provenance_index()
        get_logger().info(
            "Preserved %s content of '%s' as %s:%s (from registry version '%s').",
            status,
            fname,
            algorithm,
            digest.lower(),
            registry_version,
        )
        return record

    def _preserve_superseded_entries(
        self,
        registry: dict[str, str],
        retracted: dict[str, str],
    ) -> None:
        """
        Preserve local content for files whose declaration is about to change.

        This covers both a checksum change and an explicit retraction. The
        preserved copy carries the registry version and hash under which the
        content was valid, so it can be identified later without being
        mistaken for the new version.
        """
        current = self.registry_snapshot
        for fname, known_hash in current.registry.items():
            new_hash = registry.get(fname)
            if new_hash == known_hash and fname not in retracted:
                continue
            if fname in retracted:
                status = "retracted"
            elif new_hash is None:
                # The new registry does not declare this file. That is not a
                # retraction, but local content still needs an identity before
                # any later version can overwrite it.
                status = "undeclared"
            else:
                status = "superseded"
            path = self.abspath / fname
            if not path.exists():
                continue
            algorithm = hash_algorithm(known_hash)
            try:
                digest = file_hash(str(path), alg=algorithm)
            except (OSError, ValueError):
                continue
            if digest.lower() == known_hash.split(":")[-1].lower():
                self._preserve_file(
                    fname=fname,
                    path=path,
                    algorithm=algorithm,
                    digest=digest,
                    registry_version=current.version,
                    known_hash=known_hash,
                    status=status,
                )

    def _history_root(self) -> Path:
        "Root folder for preserved content."
        return self.abspath / ".pooch-history"

    def _write_provenance_index(self) -> None:
        "Write a human-readable index of preserved content."
        if not self._provenance:
            return
        history_root = self._history_root()
        history_root.mkdir(parents=True, exist_ok=True)
        index = []
        for record in self._provenance:
            index.append(
                {
                    "file": record.fname,
                    "algorithm": record.algorithm,
                    "digest": record.digest,
                    "preserved_path": str(
                        record.path.relative_to(history_root).as_posix()
                    ),
                    "registry_version": record.registry_version,
                    "known_hash": record.known_hash,
                    "captured_at": record.captured_at,
                    "status": record.status,
                }
            )
        with self._lock:
            registries = [snapshot.as_dict() for snapshot in self._snapshots]
        payload = {"history": index, "registries": registries}
        with tempfile.NamedTemporaryFile(
            "w", dir=history_root, prefix=".index-", suffix=".tmp", delete=False
        ) as tmp:
            tmp.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            temporary = Path(tmp.name)
        os.replace(str(temporary), str(history_root / "index.json"))

    def _load_provenance_index(self) -> None:
        "Load preserved-content metadata from a previous session, if present."
        index_path = self._history_root() / "index.json"
        if not index_path.exists():
            return
        try:
            payload = json.loads(index_path.read_text(encoding="utf-8"))
            entries = payload.get("history", [])
        except (OSError, ValueError):
            get_logger().warning(
                "Could not read preserved-content index '%s'.", str(index_path)
            )
            return
        history_root = self._history_root()
        for entry in entries:
            try:
                path = history_root / entry["preserved_path"]
                record = ProvenanceRecord(
                    fname=entry["file"],
                    path=path,
                    algorithm=entry["algorithm"],
                    digest=entry["digest"],
                    registry_version=entry["registry_version"],
                    known_hash=entry["known_hash"],
                    captured_at=entry["captured_at"],
                    status=entry.get("status", "superseded"),
                )
            except (KeyError, TypeError):
                continue
            if path.exists():
                self._provenance.append(record)
        restored = []
        for snapshot_data in payload.get("registries", []):
            try:
                restored.append(RegistrySnapshot.from_dict(snapshot_data))
            except (KeyError, TypeError):
                continue
        with self._lock:
            known_digests = {snapshot.digest for snapshot in self._snapshots}
            self._snapshots[:0] = [
                snapshot
                for snapshot in restored
                if snapshot.digest not in known_digests
            ]

    def retract(
        self,
        fname: str,
        reason: str = "",
        version: Optional[str] = None,
    ) -> RegistrySnapshot:
        """
        Explicitly retract a file from the current registry.

        Retraction is different from removal. A removed file is no longer
        declared; a retracted file is declared as unusable and fetching it
        raises :class:`~pooch.registry.RetractedFileError`. Local content that
        matches the current declaration is preserved first.

        Parameters
        ----------
        fname : str
            The file name to retract.
        reason : str
            Optional explanation stored with the retraction.
        version : str or None
            Optional label for the resulting snapshot.

        Returns
        -------
        snapshot : RegistrySnapshot
            The immutable snapshot that was published.
        """
        with self._lock:
            registry = dict(self._registry)
            urls = dict(self._urls)
            retracted = dict(self._retracted)
            reasons = dict(self._retraction_reasons)
            registry.pop(fname, None)
            urls.pop(fname, None)
            retracted[fname] = reason
            reasons[fname] = reason
            self._preserve_superseded_entries(registry, retracted)
            return self._commit_registry(
                registry,
                urls,
                version,
                retracted=retracted,
                retraction_reasons=reasons,
            )

    def is_retracted(self, fname: str) -> bool:
        "Return True if *fname* is explicitly retracted by the current registry."
        return self.registry_snapshot.is_retracted(fname)

    @property
    def retracted_files(self) -> list[str]:
        "List of file names explicitly retracted by the current registry."
        return self.registry_snapshot.retracted_files

    @property
    def registry_versions(self) -> list[str]:
        "List of all published registry versions, oldest first."
        with self._lock:
            return [snapshot.version for snapshot in self._snapshots]

    def load_registry_from_doi(self) -> None:
        """
        Populate the registry using the data repository API

        Fill the registry with all the files available in the data repository,
        along with their hashes. It will make a request to the data repository
        API to retrieve this information. No file is downloaded during this
        process.

        .. important::

            This method is intended to be used only when the ``base_url`` is
            a DOI.
        """

        # Ensure that this is indeed a DOI-based pooch
        downloader = choose_downloader(self.base_url)
        if not isinstance(downloader, DOIDownloader):
            raise ValueError(
                f"Invalid base_url '{self.base_url}': "
                + "Pooch.load_registry_from_doi is only implemented for DOIs"
            )

        # Create a repository instance
        doi = self.base_url.replace("doi:", "")
        repository = doi_to_repository(
            doi,
            headers=downloader.headers,
            timeout=downloader.timeout,
            **downloader.kwargs,
        )

        # Populate a private mapping first so an API failure cannot leave a
        # partially populated registry behind.
        staging = SimpleNamespace(registry=dict(self._registry), urls=dict(self._urls))
        repository.populate_registry(staging)
        registry, urls, retracted, reasons = apply_directives(
            staging.registry,
            staging.urls,
            self._retracted,
            self._retraction_reasons,
            [],
        )
        return self._commit_registry(
            registry,
            urls,
            None,
            retracted=retracted,
            retraction_reasons=reasons,
        )

    def is_available(self, fname: str, downloader: Optional[Downloader] = None):
        """
        Check availability of a remote file without downloading it.

        Use this method when working with large files to check if they are
        available for download.

        Parameters
        ----------
        fname : str
            The file name (relative to the *base_url* of the remote data
            storage).
        downloader : None or callable
            If not None, then a function (or callable object) that will be
            called to check the availability of the file on the server. See
            :ref:`downloaders` for details.

        Returns
        -------
        status : bool
            True if the file is available for download. False otherwise.

        """
        snapshot = self.registry_snapshot
        if snapshot.is_retracted(fname):
            raise RetractedFileError(self._retraction_message(snapshot, fname))
        if fname not in snapshot.registry:
            msg = (
                f"File '{fname}' is not in the registry (version '{snapshot.version}')."
            )
            raise FileNotInRegistryError(msg)
        url = snapshot.get_url(fname)
        if downloader is None:
            downloader = choose_downloader(url)
        try:
            available = downloader(url, None, self, check_only=True)
        except TypeError as error:
            error_msg = (
                f"Downloader '{downloader!s}' does not support availability checks."
            )
            raise NotImplementedError(error_msg) from error
        return available


def download_action(path: Path, known_hash: Optional[str]) -> tuple[Action, str]:
    """
    Determine the action that is needed to get the file on disk.

    Parameters
    ----------
    path : PathLike
        The path to the file on disk.
    known_hash : str
        A known hash (checksum) of the file. Will be used to verify the
        download or check if an existing file needs to be updated. By default,
        will assume it's a SHA256 hash. To specify a different hashing method,
        prepend the hash with ``algorithm:``, for example
        ``md5:pw9co2iun29juoh`` or ``sha1:092odwhi2ujdp2du2od2odh2wod2``.

    Returns
    -------
    action, verb : str
        The action that must be taken and the English verb (infinitive form of
        *action*) used in the log:
        * ``'download'``: File does not exist locally and must be downloaded.
        * ``'update'``: File exists locally but needs to be updated.
        * ``'fetch'``: File exists locally and only need to inform its path.


    """
    if not path.exists():
        return "download", "Downloading"
    if not hash_matches(str(path), known_hash):
        return "update", "Updating"
    return "fetch", "Fetching"


def stream_download(
    url: str,
    fname: Path,
    known_hash: Optional[str],
    downloader: Downloader,
    pooch: Optional[Pooch] = None,
    retry_if_failed: int = 0,
) -> None:
    """
    Stream the file and check that its hash matches the known one.

    The file is first downloaded to a temporary file name in the cache folder.
    It will be moved to the desired file name only if the hash matches the
    known hash. Otherwise, the temporary file is deleted.

    If the download fails for either a bad connection or a hash mismatch, we
    will retry the download the specified number of times in case the failure
    was due to a network error.
    """
    # Lazy import requests to speed up import time
    import requests.exceptions  # noqa: PLC0415

    # Ensure the parent directory exists in case the file is in a subdirectory.
    # Otherwise, move will cause an error.
    if not fname.parent.exists():
        os.makedirs(str(fname.parent))
    download_attempts = 1 + retry_if_failed
    max_wait = 10
    for i in range(download_attempts):
        try:
            # Stream the file to a temporary so that we can safely check its
            # hash before overwriting the original.
            with temporary_file(path=str(fname.parent)) as tmp:
                downloader(url, tmp, pooch)
                hash_matches(tmp, known_hash, strict=True, source=str(fname.name))
                shutil.move(tmp, str(fname))
            break
        except (ValueError, requests.exceptions.RequestException):
            if i == download_attempts - 1:
                raise
            retries_left = download_attempts - (i + 1)
            get_logger().info(
                "Failed to download '%s'. "
                "Will attempt the download again %d more time%s.",
                str(fname.name),
                retries_left,
                "s" if retries_left > 1 else "",
            )
            time.sleep(min(i + 1, max_wait))
