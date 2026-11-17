# Copyright (c) 2018 The Pooch Developers.
# Distributed under the terms of the BSD 3-Clause License.
# SPDX-License-Identifier: BSD-3-Clause
#
# This code is part of the Fatiando a Terra project (https://www.fatiando.org)
#

"""
Test the hash calculation and checking functions.
"""

import os
import re
from pathlib import Path
from tempfile import NamedTemporaryFile

import pytest

try:
    import xxhash

    XXHASH_MAJOR_VERSION = int(xxhash.VERSION.split(".", maxsplit=1)[0])
except ImportError:
    xxhash = None  # type: ignore[assignment]
    XXHASH_MAJOR_VERSION = 0

from ..core import Pooch
from ..hashes import (
    file_hash,
    file_size,
    hash_matches,
    is_weak_verifier,
    make_registry,
)
from .utils import check_tiny_data, mirror_directory

DATA_DIR = str(Path(__file__).parent / "data" / "store")
REGISTRY = (
    "tiny-data.txt baee0894dba14b12085eacb204284b97e362f4f3e5a5807693cc90ef415c1b2d\n"
)
REGISTRY_RECURSIVE = (
    "subdir/tiny-data.txt baee0894dba14b12085eacb204284b97e362f4f3e5a5807693cc90ef415c1b2d\n"
    "tiny-data.txt baee0894dba14b12085eacb204284b97e362f4f3e5a5807693cc90ef415c1b2d\n"
)
TINY_DATA_HASHES_HASHLIB = {
    "sha1": "c03148994acd89317915ea2f2d080d6dd127aa09",
    "sha256": "baee0894dba14b12085eacb204284b97e362f4f3e5a5807693cc90ef415c1b2d",
    "md5": "70e2afd3fd7e336ae478b1e740a5f08e",
}
TINY_DATA_HASHES_XXH = {
    "xxh64": "f843815fe57948fa",
    "xxh32": "98d6f1a2",
    # Require xxHash > 2.0
    "xxh128": "0267d220db258fffb0c567c0ecd1b689",
    "xxh3_128": "0267d220db258fffb0c567c0ecd1b689",
    "xxh3_64": "811e3f2a12aec53f",
}
TINY_DATA_HASHES = TINY_DATA_HASHES_HASHLIB.copy()
TINY_DATA_HASHES.update(TINY_DATA_HASHES_XXH)


@pytest.fixture
def data_dir_mirror(tmp_path):
    """
    Mirror the test data folder on a temporary directory. Needed to avoid
    permission errors when pooch is installed on a non-writable path.
    """
    return mirror_directory(DATA_DIR, tmp_path)


def test_make_registry(data_dir_mirror):
    "Check that the registry builder creates the right file names and hashes"
    outfile = NamedTemporaryFile(delete=False)  # noqa: SIM115
    # Need to close the file before writing to it.
    outfile.close()
    try:
        make_registry(data_dir_mirror, outfile.name, recursive=False)
        with open(outfile.name, encoding="utf-8") as fout:
            registry = fout.read()
        assert registry == REGISTRY
        # Check that the registry can be used.
        pup = Pooch(path=data_dir_mirror, base_url="some bogus URL", registry={})
        pup.load_registry(outfile.name)
        true = str(data_dir_mirror / "tiny-data.txt")
        fname = pup.fetch("tiny-data.txt")
        assert true == fname
        check_tiny_data(fname)
    finally:
        os.remove(outfile.name)


def test_make_registry_recursive(data_dir_mirror):
    "Check that the registry builder works in recursive mode"
    outfile = NamedTemporaryFile(delete=False)  # noqa: SIM115
    # Need to close the file before writing to it.
    outfile.close()
    try:
        make_registry(data_dir_mirror, outfile.name, recursive=True)
        with open(outfile.name, encoding="utf-8") as fout:
            registry = fout.read()
        assert registry == REGISTRY_RECURSIVE
        # Check that the registry can be used.
        pup = Pooch(path=data_dir_mirror, base_url="some bogus URL", registry={})
        pup.load_registry(outfile.name)
        assert str(data_dir_mirror / "tiny-data.txt") == pup.fetch("tiny-data.txt")
        check_tiny_data(pup.fetch("tiny-data.txt"))
        true = str(data_dir_mirror / "subdir" / "tiny-data.txt")
        assert true == pup.fetch("subdir/tiny-data.txt")
        check_tiny_data(pup.fetch("subdir/tiny-data.txt"))
    finally:
        os.remove(outfile.name)


def test_file_hash_invalid_algorithm():
    "Test an invalid hashing algorithm"
    msg = re.escape("Algorithm 'blah' not available to the pooch library")
    with pytest.raises(ValueError, match=msg):
        file_hash(fname="something", alg="blah")


@pytest.mark.parametrize(
    ("alg", "expected_hash"),
    list(TINY_DATA_HASHES.items()),
    ids=list(TINY_DATA_HASHES.keys()),
)
def test_file_hash(alg, expected_hash):
    "Test the hash calculation using hashlib and xxhash"
    if alg.startswith("xxh"):
        if xxhash is None:
            pytest.skip("requires xxhash")
        if alg not in ["xxh64", "xxh32"] and XXHASH_MAJOR_VERSION < 2:
            pytest.skip("requires xxhash > 2.0")
    fname = os.path.join(DATA_DIR, "tiny-data.txt")
    check_tiny_data(fname)
    returned_hash = file_hash(fname, alg)
    assert returned_hash == expected_hash


@pytest.mark.parametrize(
    ("alg", "expected_hash"),
    list(TINY_DATA_HASHES.items()),
    ids=list(TINY_DATA_HASHES.keys()),
)
def test_hash_matches(alg, expected_hash):
    "Make sure the hash checking function works"
    if alg.startswith("xxh"):
        if xxhash is None:
            pytest.skip("requires xxhash")
        if alg not in ["xxh64", "xxh32"] and XXHASH_MAJOR_VERSION < 2:
            pytest.skip("requires xxhash > 2.0")
    fname = os.path.join(DATA_DIR, "tiny-data.txt")
    check_tiny_data(fname)
    # Check if the check passes
    known_hash = f"{alg}:{expected_hash}"
    assert hash_matches(fname, known_hash)
    # And also if it fails
    known_hash = f"{alg}:blablablabla"
    assert not hash_matches(fname, known_hash)


@pytest.mark.parametrize(
    ("alg", "expected_hash"),
    list(TINY_DATA_HASHES_HASHLIB.items()),
    ids=list(TINY_DATA_HASHES_HASHLIB.keys()),
)
def test_hash_matches_strict(alg, expected_hash):
    "Make sure the hash checking function raises an exception if strict"
    fname = os.path.join(DATA_DIR, "tiny-data.txt")
    check_tiny_data(fname)
    # Check if the check passes
    known_hash = f"{alg}:{expected_hash}"
    assert hash_matches(fname, known_hash, strict=True)
    # And also if it fails
    bad_hash = f"{alg}:blablablabla"
    msg = re.escape("hash of downloaded file (Neverland) does not match the known hash")
    with pytest.raises(ValueError, match=msg):
        hash_matches(fname, bad_hash, strict=True, source="Neverland")
    msg = re.escape(f"hash of downloaded file ({fname}) does not match the known hash")
    with pytest.raises(ValueError, match=msg):
        hash_matches(fname, bad_hash, strict=True, source=None)


def test_hash_matches_none():
    "The hash checking function should always returns True if known_hash=None"
    fname = os.path.join(DATA_DIR, "tiny-data.txt")
    assert hash_matches(fname, known_hash=None)
    # Should work even if the file is invalid
    assert hash_matches(fname="", known_hash=None)
    # strict should cause an error if this wasn't working
    assert hash_matches(fname, known_hash=None, strict=True)


def test_file_size():
    "Check that the file size calculation matches the OS"
    fname = os.path.join(DATA_DIR, "tiny-data.txt")
    assert file_size(fname) == os.path.getsize(fname)


def test_is_weak_verifier():
    "Check the classification of weak (non-cryptographic) verifiers"
    assert is_weak_verifier("size:123")
    assert is_weak_verifier("etag:AbCdEf123")
    # Algorithm names are case-insensitive
    assert is_weak_verifier("SIZE:123")
    assert is_weak_verifier("ETag:abc")
    assert not is_weak_verifier(None)
    assert not is_weak_verifier("sha256:abc123")
    assert not is_weak_verifier(
        "baee0894dba14b12085eacb204284b97e362f4f3e5a5807693cc90ef415c1b2d"
    )


def test_hash_matches_size():
    "The size weak verifier only checks the file length"
    fname = os.path.join(DATA_DIR, "tiny-data.txt")
    size = os.path.getsize(fname)
    assert hash_matches(fname, f"size:{size}")
    assert hash_matches(fname, f"size:{size}", strict=True)
    assert not hash_matches(fname, f"size:{size + 1}")
    msg = re.escape(
        f"Size of downloaded file (Neverland) does not match the known size:"
        f" expected size:{size + 1}"
    )
    with pytest.raises(ValueError, match=msg):
        hash_matches(fname, f"size:{size + 1}", strict=True, source="Neverland")


def test_hash_matches_size_invalid():
    "A non-integer size verifier should raise a helpful error"
    fname = os.path.join(DATA_DIR, "tiny-data.txt")
    msg = re.escape("Invalid size verifier 'size:abc'")
    with pytest.raises(ValueError, match=msg):
        hash_matches(fname, "size:abc")


def test_hash_matches_etag():
    "ETags cannot be recomputed from local contents so they never mismatch"
    fname = os.path.join(DATA_DIR, "tiny-data.txt")
    assert hash_matches(fname, "etag:AbCdEf123")
    assert hash_matches(fname, "etag:AbCdEf123", strict=True)


@pytest.mark.parametrize(
    ("alg", "expected_hash"),
    list(TINY_DATA_HASHES_HASHLIB.items()),
    ids=list(TINY_DATA_HASHES_HASHLIB.keys()),
)
def test_hash_matches_uppercase(alg, expected_hash):
    "Hash matching should be independent of upper or lower case"
    fname = os.path.join(DATA_DIR, "tiny-data.txt")
    check_tiny_data(fname)
    # Check if the check passes
    known_hash = f"{alg}:{expected_hash.upper()}"
    assert hash_matches(fname, known_hash, strict=True)
    # And also if it fails
    msg = re.escape("hash of downloaded file (Neverland) does not match the known hash")
    with pytest.raises(ValueError, match=msg):
        hash_matches(fname, known_hash[:-5], strict=True, source="Neverland")
