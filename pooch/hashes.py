# Copyright (c) 2018 The Pooch Developers.
# Distributed under the terms of the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause
#
# This code is part of the Fatiando a Terra project (https://www.fatiando.org)
#
"""
Calculating and checking file hashes.
"""

import functools
import hashlib
from pathlib import Path

# Verifiers that are not cryptographic hashes. They record claims made by the
# data source (file size or ETag) when no trusted digest is available yet. A
# match with a weak verifier does NOT prove that two files have the same
# bytes, so they should be replaced by a proper hash as soon as one can be
# obtained.
WEAK_VERIFIERS = ("size", "etag")

# From the docs: https://docs.python.org/3/library/hashlib.html#hashlib.new
#   The named constructors are much faster than new() and should be
#   preferred.
# Need to fallback on new() for some algorithms.
ALGORITHMS_AVAILABLE = {
    alg: getattr(hashlib, alg, functools.partial(hashlib.new, alg))
    for alg in hashlib.algorithms_available
}

try:
    import xxhash

    # xxhash doesn't have a list of available algorithms yet.
    # https://github.com/ifduyue/python-xxhash/issues/48
    ALGORITHMS_AVAILABLE.update(
        {
            alg: getattr(xxhash, alg, None)
            for alg in ["xxh128", "xxh64", "xxh32", "xxh3_128", "xxh3_64"]
        }
    )
    # The xxh3 algorithms are only available for version>=2.0. Set to None and
    # remove to ensure backwards compatibility.
    ALGORITHMS_AVAILABLE = {
        alg: func for alg, func in ALGORITHMS_AVAILABLE.items() if func is not None
    }
except ImportError:
    pass


def file_size(fname):
    """
    Calculate the size of a given file in bytes.

    Useful as a weak integrity check when no cryptographic hash of the file
    is available. Note that a matching size does not prove that two files
    have the same contents.

    Parameters
    ----------
    fname : str
        The name of the file.

    Returns
    -------
    size : int
        The size of the file in bytes.

    """
    return Path(fname).stat().st_size


def is_weak_verifier(hash_string):
    """
    Check if a hash string uses a weak (non-cryptographic) verifier.

    Weak verifiers (see ``WEAK_VERIFIERS``) record claims made by the data
    source, like the file size (``size:123``) or an ETag (``etag:abc123``),
    for files that don't have a trusted digest yet. A match with a weak
    verifier cannot prove that the file bytes are identical to the source.

    Parameters
    ----------
    hash_string : str or None
        The hash string with optional algorithm prepended.

    Returns
    -------
    is_weak : bool
        True if the hash string uses a weak verifier, False otherwise
        (including if *hash_string* is None).

    Examples
    --------

    >>> print(is_weak_verifier("size:12345"))
    True
    >>> print(is_weak_verifier("etag:2c6b3c42b1d4f7b"))
    True
    >>> print(is_weak_verifier("md5:70e2afd3fd7e336ae478b1e740a5f08e"))
    False
    >>> print(is_weak_verifier(None))
    False

    """
    if hash_string is None:
        return False
    return hash_algorithm(hash_string) in WEAK_VERIFIERS


def file_hash(fname, alg="sha256"):
    """
    Calculate the hash of a given file.

    Useful for checking if a file has changed or been corrupted.

    Parameters
    ----------
    fname : str
        The name of the file.
    alg : str
        The type of the hashing algorithm

    Returns
    -------
    hash : str
        The hash of the file.

    Examples
    --------

    >>> fname = "test-file-for-hash.txt"
    >>> with open(fname, "w") as f:
    ...     __ = f.write("content of the file")
    >>> print(file_hash(fname))
    0fc74468e6a9a829f103d069aeb2bb4f8646bad58bf146bb0e3379b759ec4a00
    >>> import os
    >>> os.remove(fname)

    """
    if alg not in ALGORITHMS_AVAILABLE:
        msg = (
            f"Algorithm '{alg}' not available to the pooch library. "
            "Only the following algorithms are available "
            f"{list(ALGORITHMS_AVAILABLE.keys())}."
        )
        raise ValueError(msg)
    # Calculate the hash in chunks to avoid overloading the memory
    chunksize = 65536
    # For hashlib algorithms, use usedforsecurity=False to support FIPS-enabled
    # systems. xxhash algorithms don't support this parameter.
    hasher = (
        ALGORITHMS_AVAILABLE[alg](usedforsecurity=False)
        if alg in hashlib.algorithms_available
        else ALGORITHMS_AVAILABLE[alg]()
    )
    with open(fname, "rb") as fin:
        buff = fin.read(chunksize)
        while buff:
            hasher.update(buff)
            buff = fin.read(chunksize)
    return hasher.hexdigest()


def hash_algorithm(hash_string):
    """
    Parse the name of the hash method from the hash string.

    The hash string should have the following form ``algorithm:hash``, where
    algorithm can be the name of any algorithm known to :mod:`hashlib`.

    If the algorithm is omitted or the hash string is None, will default to
    ``"sha256"``.

    Parameters
    ----------
    hash_string : str
        The hash string with optional algorithm prepended.

    Returns
    -------
    hash_algorithm : str
        The name of the algorithm.

    Examples
    --------

    >>> print(hash_algorithm("qouuwhwd2j192y1lb1iwgowdj2898wd2d9"))
    sha256
    >>> print(hash_algorithm("md5:qouuwhwd2j192y1lb1iwgowdj2898wd2d9"))
    md5
    >>> print(hash_algorithm("sha256:qouuwhwd2j192y1lb1iwgowdj2898wd2d9"))
    sha256
    >>> print(hash_algorithm("SHA256:qouuwhwd2j192y1lb1iwgowdj2898wd2d9"))
    sha256
    >>> print(hash_algorithm("xxh3_64:qouuwhwd2j192y1lb1iwgowdj2898wd2d9"))
    xxh3_64
    >>> print(hash_algorithm(None))
    sha256

    """
    default = "sha256"
    if hash_string is None or ":" not in hash_string:
        algorithm = default
    else:
        algorithm = hash_string.split(":")[0]
    return algorithm.lower()


def hash_matches(fname, known_hash, strict=False, source=None):
    """
    Check if the hash of a file matches a known hash.

    If the *known_hash* is None, will always return True.

    Coverts hashes to lowercase before comparison to avoid system specific
    mismatches between hashes in the registry and computed hashes.

    Besides cryptographic hashes, the *known_hash* can be a weak verifier
    that records a claim made by the data source (see ``WEAK_VERIFIERS``):

    * ``size:N``: the file size in bytes. Checked against the actual size of
      the local file. A match only proves that the file has the expected
      length, not that its bytes are identical to the source.
    * ``etag:value``: an HTTP ETag issued by the server. ETags are opaque
      labels that cannot be recomputed from the local file contents, so they
      cannot be checked here and this function will always return True for
      them (no evidence of a mismatch).

    Parameters
    ----------
    fname : str or PathLike
        The path to the file.
    known_hash : str
        The known hash. Optionally, prepend ``alg:`` to the hash to specify the
        hashing algorithm. Default is SHA256. Weak verifiers can be specified
        as ``size:N`` or ``etag:value``.
    strict : bool
        If True, will raise a :class:`ValueError` if the hash does not match
        informing the user that the file may be corrupted.
    source : str
        The source of the downloaded file (name or URL, for example). Will be
        used in the error message if *strict* is True. Has no other use other
        than reporting to the user where the file came from in case of hash
        mismatch. If None, will default to *fname*.

    Returns
    -------
    is_same : bool
        True if the hash matches, False otherwise.

    """
    if known_hash is None:
        return True
    algorithm = hash_algorithm(known_hash)
    if algorithm == "etag":
        # ETags are opaque labels issued by the server and cannot be
        # recomputed from the local file contents, so there is nothing that
        # can be checked here. Return True (no evidence of a mismatch) and
        # keep the original claim in the registry until a trusted digest can
        # replace it.
        return True
    if algorithm == "size":
        new_hash = file_size(fname)
        known_size = known_hash.split(":")[-1]
        try:
            matches = new_hash == int(known_size)
        except ValueError:
            msg = (
                f"Invalid size verifier '{known_hash}': expected "
                "'size:<bytes>' with an integer number of bytes."
            )
            raise ValueError(msg) from None
        new_hash = str(new_hash)
    else:
        new_hash = file_hash(fname, alg=algorithm)
        matches = new_hash.lower() == known_hash.split(":")[-1].lower()
    if strict and not matches:
        if source is None:
            source = str(fname)
        if algorithm == "size":
            msg = (
                f"Size of downloaded file ({source}) does not match the known"
                f" size: expected {known_hash} but got {new_hash} bytes."
                " Deleted download for safety. The downloaded file may have"
                " been corrupted or the known size may be outdated."
            )
        else:
            msg = (
                f"{algorithm.upper()} hash of downloaded file ({source}) does"
                f" not match the known hash: expected {known_hash} but got"
                f" {new_hash}. Deleted download for safety. The downloaded"
                " file may have been corrupted or the known hash may be"
                " outdated."
            )
        raise ValueError(msg)
    return matches


def make_registry(directory, output, recursive=True):
    """
    Make a registry of files and hashes for the given directory.

    This is helpful if you have many files in your test dataset as it keeps you
    from needing to manually update the registry.

    Parameters
    ----------
    directory : str
        Directory of the test data to put in the registry. All file names in
        the registry will be relative to this directory.
    output : str
        Name of the output registry file.
    recursive : bool
        If True, will recursively look for files in subdirectories of
        *directory*.

    """
    directory = Path(directory)
    pattern = "**/*" if recursive else "*"

    files = sorted(
        str(path.relative_to(directory))
        for path in directory.glob(pattern)
        if path.is_file()
    )

    hashes = [file_hash(str(directory / fname)) for fname in files]

    with open(output, "w", encoding="utf-8") as outfile:
        # Only use Unix separators for the registry so that we don't go
        # insane dealing with file paths.
        outfile.writelines(
            "{} {}\n".format(fname.replace("\\", "/"), fhash)
            for fname, fhash in zip(files, hashes)
        )
