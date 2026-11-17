# Copyright (c) 2018 The Pooch Developers.
# Distributed under the terms of the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause
#
# This code is part of the Fatiando a Terra project (https://www.fatiando.org)
#
"""
Download a single file from one of several mirrors, with verification.

The same data file is often hosted on multiple mirrors and behind redirects.
This module tries each mirror in turn, selecting the first one that is both
in the allowed sources and passes the hash (digest) verification. Every
attempt and the final address (after following redirects) are recorded.

If a mirror serves a file with the expected name but different content (the
hash doesn't match), the divergent file is quarantined instead of deleted so
the discrepancy can be inspected later. A successful download from a later
mirror never erases quarantined files.

If the operation is cancelled (a cancellation event is set or a
``KeyboardInterrupt`` is raised by the downloader), no new fallback requests
to the remaining mirrors are started.
"""

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from .downloaders import DEFAULT_TIMEOUT, choose_downloader
from .hashes import file_hash, hash_matches
from .typing import Downloader, PathType
from .utils import (
    cache_location,
    get_logger,
    make_local_storage,
    os_cache,
    parse_url,
    temporary_file,
    unique_file_name,
)

#: Name of the folder inside the cache where divergent downloads are kept.
QUARANTINE_DIR = "quarantine"


@dataclass
class MirrorAttempt:
    """
    Record of a single attempt to download a file from a mirror.

    Attributes
    ----------
    url : str
        The mirror URL that was attempted.
    final_url : str or None
        The final address after following redirects. Equal to ``url`` when
        no redirect took place or the protocol doesn't support redirects.
        None if the attempt never reached the network (for example, the
        source was not allowed).
    status : str
        One of ``"success"``, ``"hash_mismatch"``, ``"download_error"``,
        ``"redirect_error"``, ``"source_not_allowed"``, or ``"cancelled"``.
    error : str or None
        A description of what went wrong, if anything.
    quarantined_to : str or None
        Path to the quarantined file if this attempt returned content that
        failed the hash verification. Quarantined files are preserved even
        if a later mirror succeeds.
    """

    url: str
    final_url: Optional[str] = None
    status: str = "success"
    error: Optional[str] = None
    quarantined_to: Optional[str] = None


class MirrorDownloadError(ValueError):
    """
    Raised when no mirror could provide a file passing verification.

    Carries the list of :class:`MirrorAttempt` records describing every
    attempt that was made in the ``attempts`` attribute.
    """

    def __init__(self, message, attempts):
        super().__init__(message)
        self.attempts = list(attempts)


class MirrorDownloadCancelled(Exception):
    """
    Raised when a mirrored download is cancelled.

    No new fallback requests to the remaining mirrors are started once the
    operation is cancelled. Carries the attempts made so far (including the
    cancelled one) in the ``attempts`` attribute.
    """

    def __init__(self, message, attempts):
        super().__init__(message)
        self.attempts = list(attempts)


def source_allowed(url: str, allowed_sources) -> bool:
    """
    Check whether a URL belongs to one of the allowed sources.

    Each entry in *allowed_sources* can be:

    * A full origin like ``"https://data.example.org"`` (optionally with a
      path prefix, e.g. ``"https://data.example.org/mirror"``).
    * A bare host (with optional port) like ``"data.example.org"``.
    * A bare protocol/scheme like ``"https"``.

    Parameters
    ----------
    url : str
        The URL to check.
    allowed_sources : list of str or None
        The allowed sources. If None or empty, every source is allowed.

    Returns
    -------
    allowed : bool
        True if the URL matches at least one of the allowed sources.
    """
    if not allowed_sources:
        return True
    parsed = parse_url(url)
    for entry in allowed_sources:
        source = entry.strip()
        if not source:
            continue
        if "://" in source:
            entry_parsed = parse_url(source)
            if (
                parsed["protocol"] == entry_parsed["protocol"]
                and parsed["netloc"] == entry_parsed["netloc"]
            ):
                prefix = entry_parsed["path"].rstrip("/")
                if not prefix or parsed["path"].startswith(prefix):
                    return True
        elif source in (parsed["protocol"], parsed["netloc"]):
            return True
    return False


def resolve_final_url(url: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """
    Follow redirects to find the final address of a URL.

    Uses a ``HEAD`` request for HTTP/HTTPS URLs. For other protocols, the
    URL is returned unchanged.

    Parameters
    ----------
    url : str
        The URL to resolve.
    timeout : int
        Timeout in seconds for the redirect resolution request.

    Returns
    -------
    final_url : str
        The final address after following all redirects.
    """
    if parse_url(url)["protocol"] not in ("http", "https"):
        return url
    # Lazy import requests to speed up import time
    import requests  # noqa: PLC0415

    response = requests.head(url, timeout=timeout, allow_redirects=True)
    response.raise_for_status()
    return response.url


def _quarantine_file(tmp: str, path: Path, fname: str, url: str) -> str:
    """
    Move a downloaded file that failed verification to the quarantine folder.

    The file is kept under ``path/quarantine`` with a unique name derived
    from the mirror URL so that divergent content from different mirrors is
    preserved and never overwritten or erased by later attempts.
    """
    quarantine_dir = path / QUARANTINE_DIR
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.md5(url.encode(), usedforsecurity=False).hexdigest()
    quarantined = quarantine_dir / f"{digest}-{fname}"
    counter = 1
    while quarantined.exists():
        quarantined = quarantine_dir / f"{digest}-{fname}.{counter}"
        counter += 1
    shutil.move(tmp, str(quarantined))
    return str(quarantined)


def retrieve_from_mirrors(
    urls: Union[str, list],
    known_hash: Optional[str] = None,
    fname: Optional[str] = None,
    path: Optional[PathType] = None,
    allowed_sources=None,
    downloader: Optional[Downloader] = None,
    progressbar: bool = False,
    cancel_event=None,
    redirect_timeout: int = DEFAULT_TIMEOUT,
) -> tuple:
    """
    Download and cache a file, trying multiple mirrors in order.

    Each mirror URL is tried in turn: the source is checked against
    *allowed_sources*, redirects are followed to find the final address, the
    file is downloaded, and its hash is verified against *known_hash*. The
    first mirror that passes verification provides the file. Every attempt
    and the final address used are recorded in the log and in the returned
    list of :class:`MirrorAttempt`.

    If a mirror returns a file with the expected name but content that fails
    the hash verification, the divergent file is moved to a ``quarantine``
    subfolder of *path* instead of being deleted, and the next mirror is
    tried. Quarantined files are never removed, so a later successful mirror
    does not erase the evidence of the divergence.

    If *cancel_event* (for example a :class:`threading.Event`) is set, or a
    :class:`KeyboardInterrupt` is raised during a download, the operation
    stops immediately and no new fallback requests to the remaining mirrors
    are started.

    Parameters
    ----------
    urls : str or list of str
        The mirror URLs for the file, in the order they should be tried.
    known_hash : str or None
        A known hash (checksum) of the file, used to verify each download.
        By default, will assume it's a SHA256 hash. To specify a different
        hashing method, prepend the hash with ``algorithm:``, for example
        ``md5:pw9co2iun29juoh``. If None, will NOT check the hash of the
        downloaded files.
    fname : str or None
        The name used to save the file (appended to *path*). If None, will
        create a unique file name from the first mirror URL (see
        :func:`pooch.utils.unique_file_name`).
    path : str or PathLike or None
        The location of the cache folder on disk. If None, will save to a
        ``pooch`` folder in the default cache location (see
        :func:`pooch.os_cache`).
    allowed_sources : list of str or None
        Sources that mirrors and their redirects are allowed to come from.
        See :func:`source_allowed` for the accepted formats. Both the mirror
        URL and its final (redirected) address must be allowed. If None,
        every source is allowed.
    downloader : None or callable
        If not None, a function (or callable object) called to download a
        given URL to a provided local file name. If None, a downloader is
        chosen for each mirror based on its protocol. See
        :ref:`downloaders` for details.
    progressbar : bool or an arbitrary progress bar object
        If True, will print a progress bar of the download to standard error
        (stderr). Only used when *downloader* is None.
    cancel_event : None or object with an ``is_set()`` method
        If provided and ``is_set()`` returns True before a mirror is tried,
        the operation is cancelled and no new fallback requests are started.
    redirect_timeout : int
        Timeout in seconds for the redirect resolution of each mirror.

    Returns
    -------
    full_path : str
        The absolute path (including the file name) of the file in the local
        storage.
    attempts : list of :class:`MirrorAttempt`
        A record of every attempt made, in order, including the final
        address of each mirror and the quarantine location of any divergent
        content.

    Raises
    ------
    MirrorDownloadCancelled
        If the operation is cancelled. No further mirrors are contacted.
    MirrorDownloadError
        If every allowed mirror fails to provide a file passing
        verification.
    """
    if isinstance(urls, str):
        urls = [urls]
    urls = list(urls)
    if not urls:
        msg = "Need at least one mirror URL to download from."
        raise ValueError(msg)
    if path is None:
        path = os_cache("pooch")
    if fname is None:
        fname = unique_file_name(urls[0])
    # Make the path absolute.
    path = cache_location(path, env=None, version=None)

    full_path = path.resolve() / fname
    attempts: list = []

    if full_path.exists() and hash_matches(str(full_path), known_hash):
        # The file is already in the cache and passes verification, so no
        # mirror needs to be contacted.
        return str(full_path), attempts

    # We need to write data, so create the local data directory if it
    # doesn't already exist.
    make_local_storage(path)

    for index, url in enumerate(urls):
        if cancel_event is not None and cancel_event.is_set():
            attempts.append(
                MirrorAttempt(url=url, status="cancelled", error="Cancelled.")
            )
            get_logger().info(
                "Download of '%s' cancelled; not contacting remaining mirrors.",
                fname,
            )
            msg = f"Download of '{fname}' cancelled before contacting mirror '{url}'."
            raise MirrorDownloadCancelled(msg, attempts)

        get_logger().info(
            "Attempting mirror %d/%d for '%s': '%s'.", index + 1, len(urls), fname, url
        )
        attempt = MirrorAttempt(url=url)

        if not source_allowed(url, allowed_sources):
            attempt.status = "source_not_allowed"
            attempt.error = f"Source of '{url}' is not in the allowed sources."
            attempts.append(attempt)
            get_logger().warning(
                "Skipping mirror '%s': source is not in the allowed sources.", url
            )
            continue

        try:
            final_url = resolve_final_url(url, timeout=redirect_timeout)
        except Exception as error:
            attempt.status = "redirect_error"
            attempt.error = str(error)
            attempts.append(attempt)
            get_logger().warning(
                "Failed to resolve redirects for mirror '%s': %s", url, error
            )
            continue
        attempt.final_url = final_url
        if final_url != url:
            get_logger().info(
                "Mirror '%s' redirected to final address '%s'.", url, final_url
            )

        if not source_allowed(final_url, allowed_sources):
            attempt.status = "source_not_allowed"
            attempt.error = (
                f"Final address '{final_url}' is not in the allowed sources."
            )
            attempts.append(attempt)
            get_logger().warning(
                "Skipping mirror '%s': final address '%s' is not in the "
                "allowed sources.",
                url,
                final_url,
            )
            continue

        actual_downloader = downloader
        if actual_downloader is None:
            actual_downloader = choose_downloader(final_url, progressbar=progressbar)

        try:
            # Stream the file to a temporary name so that the hash can be
            # checked before the file is moved to its final destination.
            quarantined_to = None
            with temporary_file(path=str(path)) as tmp:
                actual_downloader(final_url, tmp, None)
                try:
                    hash_matches(tmp, known_hash, strict=True, source=final_url)
                except ValueError as error:
                    # Hash mismatch: the mirror returned same-name but
                    # different content. Quarantine the divergent file
                    # instead of deleting it so the discrepancy is preserved
                    # even if a later mirror succeeds.
                    quarantined_to = _quarantine_file(tmp, path, fname, url)
                    attempt.status = "hash_mismatch"
                    attempt.error = str(error)
                    attempt.quarantined_to = quarantined_to
                else:
                    shutil.move(tmp, str(full_path))
            if quarantined_to is not None:
                attempts.append(attempt)
                get_logger().warning(
                    "Mirror '%s' returned content that failed hash "
                    "verification. Quarantined the divergent file to '%s'.",
                    url,
                    quarantined_to,
                )
                continue
            attempt.status = "success"
            attempts.append(attempt)
            get_logger().info(
                "Downloaded '%s' from mirror '%s' (final address '%s') to '%s'.",
                fname,
                url,
                final_url,
                str(full_path),
            )
            if known_hash is None:
                get_logger().info(
                    "SHA256 hash of downloaded file: %s",
                    file_hash(str(full_path)),
                )
            return str(full_path), attempts
        except KeyboardInterrupt:
            attempt.status = "cancelled"
            attempt.error = "Cancelled."
            attempts.append(attempt)
            get_logger().info(
                "Download of '%s' cancelled; not contacting remaining mirrors.",
                fname,
            )
            msg = f"Download of '{fname}' from mirror '{url}' was cancelled."
            raise MirrorDownloadCancelled(msg, attempts) from None
        except Exception as error:
            attempt.status = "download_error"
            attempt.error = str(error)
            attempts.append(attempt)
            get_logger().warning(
                "Failed to download '%s' from mirror '%s': %s", fname, url, error
            )

    quarantined = [a.quarantined_to for a in attempts if a.quarantined_to]
    message = [
        f"Failed to download '{fname}' from all {len(urls)} mirror(s).",
        "Attempts:",
    ]
    message.extend(
        f"  - '{a.url}' (final address: {a.final_url}): {a.status}"
        + (f" ({a.error})" if a.error else "")
        for a in attempts
    )
    if quarantined:
        message.append(
            "Divergent content was quarantined to: " + ", ".join(quarantined)
        )
    raise MirrorDownloadError("\n".join(message), attempts)
