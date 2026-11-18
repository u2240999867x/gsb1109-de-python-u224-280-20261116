# emacs: -*- mode: python-mode; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the NiBabel package for the
#   copyright and license terms.
#
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Tests for file version detection

The backend reads a header first, and reads voxel data (possibly in
chunks) later.  In between, the source file may be replaced or modified.
These tests check that:

* a persistent handle that still refers to the original file version is
  reused;
* a non-persistent proxy reopens the file for each read, and detects when
  the reopened file is a different version;
* a version conflict raises rather than silently mixing data from the new
  file with the header of the old one;
* a cached read is detectably stale after the file changes, and is never
  presented as the new file's content;
* a read-only proxy does not change its interpretation when another
  consumer modifies the header it was created from.
"""

import os
from io import BytesIO

import numpy as np
import pytest
from numpy.testing import assert_array_equal

from ..arrayproxy import ArrayProxy
from ..nifti1 import Nifti1Image
from ..openers import FileVersion, FileVersionError, ImageOpener, file_version
from ..tmpdirs import InTemporaryDirectory

SHAPE = (2, 3, 4)
DTYPE = np.int32
OFFSET = 16


def write_array_file(path, arr, offset=OFFSET):
    with open(path, 'wb') as fobj:
        fobj.write(b'\0' * offset)
        fobj.write(arr.tobytes(order='F'))


def array_spec(shape=SHAPE, dtype=DTYPE, offset=OFFSET):
    return (shape, dtype, offset, 1.0, 0.0)


def replace_file(path, arr, offset=OFFSET):
    """Replace `path` with new contents via rename (new inode)"""
    tmp = f'{path}.replacement'
    write_array_file(tmp, arr, offset)
    os.replace(tmp, path)


def test_file_version_helpers():
    with InTemporaryDirectory():
        fname = 'test.bin'
        write_array_file(fname, np.arange(24, dtype=DTYPE))
        version = file_version(fname)
        assert isinstance(version, FileVersion)
        # Stable while the file is untouched
        assert file_version(fname).matches(version)
        # Missing file cannot be fingerprinted
        assert file_version('nonexistent.bin') is None
        # In-memory file-like cannot be fingerprinted
        assert file_version(BytesIO()) is None
        # Open handle fingerprints the file it reads
        with open(fname, 'rb') as fobj:
            assert file_version(fobj).matches(version)
        # Rewrite with different content -> different version
        write_array_file(fname, np.arange(30, dtype=DTYPE))
        assert not file_version(fname).matches(version)
        # Replacement via rename -> different version (new inode)
        version2 = file_version(fname)
        replace_file(fname, np.arange(30, dtype=DTYPE))
        assert not file_version(fname).matches(version2)
        # In-place modification, same size -> different version (mtime)
        version3 = file_version(fname)
        with open(fname, 'r+b') as fobj:
            fobj.seek(OFFSET)
            fobj.write(np.arange(30, dtype=DTYPE)[::-1].tobytes(order='F'))
        os.utime(fname, ns=(version3.mtime_ns + 1_000_000_000,) * 2)
        assert not file_version(fname).matches(version3)


def test_file_version_matches_zero_inode():
    # Filesystems reporting a zero inode fall back to size + mtime
    v1 = FileVersion(dev=1, ino=0, size=10, mtime_ns=100)
    v2 = FileVersion(dev=2, ino=0, size=10, mtime_ns=100)
    v3 = FileVersion(dev=1, ino=0, size=10, mtime_ns=101)
    assert v1.matches(v2)
    assert not v1.matches(v3)


def test_opener_file_version():
    with InTemporaryDirectory():
        fname = 'test.bin'
        write_array_file(fname, np.arange(24, dtype=DTYPE))
        with ImageOpener(fname) as opener:
            assert opener.file_version().matches(file_version(fname))
        # Opener around a user file object fingerprints the handle
        with open(fname, 'rb') as raw:
            opener = ImageOpener(raw)
            assert opener.file_version().matches(file_version(fname))
        # Opener around an in-memory object has no fingerprint
        assert ImageOpener(BytesIO()).file_version() is None


def test_proxy_reopen_detects_replacement():
    # keep_file_open=False: every read reopens the path, and a replaced
    # file is a version conflict.
    with InTemporaryDirectory():
        fname = 'test.bin'
        arr = np.arange(24, dtype=DTYPE).reshape(SHAPE)
        write_array_file(fname, arr)
        proxy = ArrayProxy(fname, array_spec(), keep_file_open=False)
        assert_array_equal(np.asarray(proxy), arr)
        assert proxy.check_file_version() is True
        replace_file(fname, np.zeros(SHAPE, dtype=DTYPE))
        with pytest.raises(FileVersionError):
            proxy.check_file_version()
        with pytest.raises(FileVersionError):
            np.asarray(proxy)


def test_proxy_reopen_detects_modification():
    with InTemporaryDirectory():
        fname = 'test.bin'
        arr = np.arange(24, dtype=DTYPE).reshape(SHAPE)
        write_array_file(fname, arr)
        proxy = ArrayProxy(fname, array_spec(), keep_file_open=False)
        assert_array_equal(np.asarray(proxy), arr)
        # Modify in place, same size
        version = file_version(fname)
        with open(fname, 'r+b') as fobj:
            fobj.seek(OFFSET)
            fobj.write(np.zeros(SHAPE, dtype=DTYPE).tobytes(order='F'))
        os.utime(fname, ns=(version.mtime_ns + 1_000_000_000,) * 2)
        with pytest.raises(FileVersionError):
            np.asarray(proxy)
        with pytest.raises(FileVersionError):
            proxy.check_file_version()


def test_proxy_persistent_handle_survives_replacement():
    # keep_file_open=True: once opened, the handle keeps referring to the
    # original file version even if the path is replaced, so it can (and
    # must) be reused - the data still matches the recorded header version.
    with InTemporaryDirectory():
        fname = 'test.bin'
        arr = np.arange(24, dtype=DTYPE).reshape(SHAPE)
        write_array_file(fname, arr)
        proxy = ArrayProxy(fname, array_spec(), keep_file_open=True)
        assert_array_equal(np.asarray(proxy), arr)
        # Replace the path with a different file
        replace_file(fname, np.zeros(SHAPE, dtype=DTYPE))
        # The open handle still reads the version the proxy was created from
        assert_array_equal(np.asarray(proxy), arr)
        assert proxy.check_file_version() is True
        # A fresh proxy anchors to the new file version
        new_proxy = ArrayProxy(fname, array_spec(), keep_file_open=False)
        assert_array_equal(np.asarray(new_proxy), np.zeros(SHAPE, dtype=DTYPE))


def test_proxy_undeterminable_version():
    # In-memory file-like: no version can be recorded, reads are unchecked
    bio = BytesIO()
    bio.seek(OFFSET)
    arr = np.arange(24, dtype=DTYPE).reshape(SHAPE)
    bio.write(arr.tobytes(order='F'))
    proxy = ArrayProxy(bio, array_spec())
    assert proxy.file_version is None
    assert proxy.check_file_version() is None
    assert_array_equal(np.asarray(proxy), arr)


def test_proxy_persistent_handle_detects_in_place_modification():
    # keep_file_open=True: the handle is reused, but if the file it refers
    # to is modified underneath, the version check still fires.
    with InTemporaryDirectory():
        fname = 'test.bin'
        arr = np.arange(24, dtype=DTYPE).reshape(SHAPE)
        write_array_file(fname, arr)
        proxy = ArrayProxy(fname, array_spec(), keep_file_open=True)
        assert_array_equal(np.asarray(proxy), arr)
        version = file_version(fname)
        with open(fname, 'r+b') as fobj:
            fobj.seek(OFFSET)
            fobj.write(np.zeros(SHAPE, dtype=DTYPE).tobytes(order='F'))
        os.utime(fname, ns=(version.mtime_ns + 1_000_000_000,) * 2)
        with pytest.raises(FileVersionError):
            np.asarray(proxy)


def test_proxy_gz_reopen_detects_replacement():
    # Compressed files are fingerprinted like any other
    import gzip

    with InTemporaryDirectory():
        fname = 'test.bin.gz'
        arr = np.arange(24, dtype=DTYPE).reshape(SHAPE)

        def write_gz(path, data):
            with gzip.open(path, 'wb') as fobj:
                fobj.write(b'\0' * OFFSET)
                fobj.write(data.tobytes(order='F'))

        write_gz(fname, arr)
        proxy = ArrayProxy(fname, array_spec(), keep_file_open=False, mmap=False)
        assert_array_equal(np.asarray(proxy), arr)
        tmp = f'{fname}.replacement'
        write_gz(tmp, np.zeros(SHAPE, dtype=DTYPE))
        os.replace(tmp, fname)
        with pytest.raises(FileVersionError):
            np.asarray(proxy)


def test_proxy_pickle_roundtrip_keeps_version():
    import pickle

    with InTemporaryDirectory():
        fname = 'test.bin'
        arr = np.arange(24, dtype=DTYPE).reshape(SHAPE)
        write_array_file(fname, arr)
        proxy = ArrayProxy(fname, array_spec(), keep_file_open=False)
        proxy2 = pickle.loads(pickle.dumps(proxy))
        assert proxy2.file_version == proxy.file_version
        assert proxy2.check_file_version() is True
        assert_array_equal(np.asarray(proxy2), arr)
        replace_file(fname, np.zeros(SHAPE, dtype=DTYPE))
        with pytest.raises(FileVersionError):
            np.asarray(proxy2)


def test_proxy_interpretation_frozen_from_header():
    # A read-only proxy snapshots the header values at creation; another
    # consumer modifying the header afterwards cannot change how the proxy
    # interprets the file.
    with InTemporaryDirectory():
        fname = 'test.nii'
        img = Nifti1Image(np.arange(24, dtype=DTYPE).reshape(SHAPE), np.eye(4))
        img.header.set_slope_inter(2.0, 10.0)
        img.to_filename(fname)
        proxy_img = Nifti1Image.from_filename(fname)
        proxy = proxy_img.dataobj
        header = proxy_img.header
        shape, dtype, offset = proxy.shape, proxy.dtype, proxy.offset
        slope, inter = proxy.slope, proxy.inter
        # Another consumer mutates the header after proxy creation
        header.set_data_shape((4, 3, 2))
        header.set_slope_inter(99.0, -99.0)
        header.set_data_offset(0)
        # The proxy's interpretation is unchanged
        assert (proxy.shape, proxy.dtype, proxy.offset) == (shape, dtype, offset)
        assert (proxy.slope, proxy.inter) == (slope, inter)
        assert_array_equal(
            np.asarray(proxy), np.arange(24, dtype=DTYPE).reshape(SHAPE) * 2.0 + 10.0
        )


def test_image_cache_does_not_masquerade():
    with InTemporaryDirectory():
        fname = 'test.nii'
        arr = np.arange(24, dtype=np.float32).reshape(SHAPE)
        Nifti1Image(arr, np.eye(4)).to_filename(fname)
        img = Nifti1Image.from_filename(fname)
        # No cache yet: trivially matches
        assert img.cache_matches_file() is True
        data = img.get_fdata()
        assert img.cache_matches_file() is True
        # Data and header provably belong to the same file version
        assert img.dataobj.check_file_version() is True
        # Replace the file with a different image
        other = 'other.nii'
        Nifti1Image(np.zeros(SHAPE, dtype=np.float32), np.eye(4)).to_filename(other)
        os.replace(other, fname)
        # The cache is detectably stale: it is the old file's data
        assert img.cache_matches_file() is False
        assert_array_equal(img._fdata_cache, data)
        # Reads fail loudly rather than serving data of mixed versions
        with pytest.raises(FileVersionError):
            img.get_fdata()
        # After uncaching, a fresh read also refuses to mix versions
        img.uncache()
        assert img.cache_matches_file() is True
        with pytest.raises(FileVersionError):
            img.get_fdata()


def test_array_image_cache_matches_file():
    # Array images have no file to contradict the cache
    img = Nifti1Image(np.zeros(SHAPE, dtype=np.float32), np.eye(4))
    img.get_fdata()
    assert img.cache_matches_file() is True
