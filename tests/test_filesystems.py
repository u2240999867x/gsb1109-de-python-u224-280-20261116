# Copyright (C) 2007 Giampaolo Rodola' <g.rodola@gmail.com>.
# Use of this source code is governed by MIT license that can be
# found in the LICENSE file.

import errno
import os
import tempfile

import pytest

from pyftpdlib.exceptions import RenameNotGuaranteedError
from pyftpdlib.filesystems import AbstractedFS

from . import HOME
from . import POSIX
from . import PyftpdlibTestCase
from . import safe_rmpath
from . import touch

if POSIX:
    from pyftpdlib.filesystems import UnixFilesystem


class TestAbstractedFS(PyftpdlibTestCase):
    """Test for conversion utility methods of AbstractedFS class."""

    def test_ftpnorm(self):
        # Tests for ftpnorm method.
        fs = AbstractedFS("/", None)

        fs._cwd = "/"
        assert fs.ftpnorm("") == "/"
        assert fs.ftpnorm("/") == "/"
        assert fs.ftpnorm(".") == "/"
        assert fs.ftpnorm("..") == "/"
        assert fs.ftpnorm("a") == "/a"
        assert fs.ftpnorm("/a") == "/a"
        assert fs.ftpnorm("/a/") == "/a"
        assert fs.ftpnorm("a/..") == "/"
        assert fs.ftpnorm("a/b") == "/a/b"
        assert fs.ftpnorm("a/b/..") == "/a"
        assert fs.ftpnorm("a/b/../..") == "/"

        fs._cwd = "/sub"
        assert fs.ftpnorm("") == "/sub"
        assert fs.ftpnorm("/") == "/"
        assert fs.ftpnorm(".") == "/sub"
        assert fs.ftpnorm("..") == "/"
        assert fs.ftpnorm("a") == "/sub/a"
        assert fs.ftpnorm("a/") == "/sub/a"
        assert fs.ftpnorm("a/..") == "/sub"
        assert fs.ftpnorm("a/b") == "/sub/a/b"
        assert fs.ftpnorm("a/b/") == "/sub/a/b"
        assert fs.ftpnorm("a/b/..") == "/sub/a"
        assert fs.ftpnorm("a/b/../..") == "/sub"
        assert fs.ftpnorm("a/b/../../..") == "/"
        assert fs.ftpnorm("//") == "/"  # UNC paths must be collapsed

    def test_ftp2fs(self):
        # Tests for ftp2fs method.
        def join(x, y):
            return os.path.join(x, y.replace("/", os.sep))

        fs = AbstractedFS("/", None)

        def goforit(root):
            fs._root = root
            fs._cwd = "/"
            assert fs.ftp2fs("") == root
            assert fs.ftp2fs("/") == root
            assert fs.ftp2fs(".") == root
            assert fs.ftp2fs("..") == root
            assert fs.ftp2fs("a") == join(root, "a")
            assert fs.ftp2fs("/a") == join(root, "a")
            assert fs.ftp2fs("/a/") == join(root, "a")
            assert fs.ftp2fs("a/..") == root
            assert fs.ftp2fs("a/b") == join(root, r"a/b")
            assert fs.ftp2fs("/a/b") == join(root, r"a/b")
            assert fs.ftp2fs("/a/b/..") == join(root, "a")
            assert fs.ftp2fs("/a/b/../..") == root

            fs._cwd = "/sub"
            assert fs.ftp2fs("") == join(root, "sub")
            assert fs.ftp2fs("/") == root
            assert fs.ftp2fs(".") == join(root, "sub")
            assert fs.ftp2fs("..") == root
            assert fs.ftp2fs("a") == join(root, "sub/a")
            assert fs.ftp2fs("a/") == join(root, "sub/a")
            assert fs.ftp2fs("a/..") == join(root, "sub")
            assert fs.ftp2fs("a/b") == join(root, "sub/a/b")
            assert fs.ftp2fs("a/b/..") == join(root, "sub/a")
            assert fs.ftp2fs("a/b/../..") == join(root, "sub")
            assert fs.ftp2fs("a/b/../../..") == root
            # UNC paths must be collapsed
            assert fs.ftp2fs("//a") == join(root, "a")

        if os.sep == "\\":
            goforit(r"C:\dir")
            goforit("C:\\")
            # on DOS-derived filesystems (e.g. Windows) this is the same
            # as specifying the current drive directory (e.g. 'C:\\')
            goforit("\\")
        elif os.sep == "/":
            goforit("/home/user")
            goforit("/")
        else:
            # os.sep == ':'? Don't know... let's try it anyway
            goforit(os.getcwd())

    def test_fs2ftp(self):
        # Tests for fs2ftp method.
        def join(x, y):
            return os.path.join(x, y.replace("/", os.sep))

        fs = AbstractedFS("/", None)

        def goforit(root):
            fs._root = root
            assert fs.fs2ftp(root) == "/"
            assert fs.fs2ftp(join(root, "/")) == "/"
            assert fs.fs2ftp(join(root, ".")) == "/"
            # can't escape from root
            assert fs.fs2ftp(join(root, "..")) == "/"
            assert fs.fs2ftp(join(root, "a")) == "/a"
            assert fs.fs2ftp(join(root, "a/")) == "/a"
            assert fs.fs2ftp(join(root, "a/..")) == "/"
            assert fs.fs2ftp(join(root, "a/b")) == "/a/b"
            assert fs.fs2ftp(join(root, "a/b")) == "/a/b"
            assert fs.fs2ftp(join(root, "a/b/..")) == "/a"
            assert fs.fs2ftp(join(root, "/a/b/../..")) == "/"
            fs._cwd = "/sub"
            assert fs.fs2ftp(join(root, "a/")) == "/a"

        if os.sep == "\\":
            goforit(r"C:\dir")
            goforit("C:\\")
            # on DOS-derived filesystems (e.g. Windows) this is the same
            # as specifying the current drive directory (e.g. 'C:\\')
            goforit("\\")
            fs._root = r"C:\dir"
            assert fs.fs2ftp("C:\\") == "/"
            assert fs.fs2ftp("D:\\") == "/"
            assert fs.fs2ftp("D:\\dir") == "/"
        elif os.sep == "/":
            goforit("/")
            if os.path.realpath("/__home/user") != "/__home/user":
                return self.skipTest("symlinks not allowed")
            goforit("/__home/user")
            fs._root = "/__home/user"
            assert fs.fs2ftp("/__home") == "/"
            assert fs.fs2ftp("/") == "/"
            assert fs.fs2ftp("/__home/userx") == "/"
        else:
            # os.sep == ':'? Don't know... let's try it anyway
            goforit(os.getcwd())

    def test_validpath(self):
        # Tests for validpath method.
        fs = AbstractedFS("/", None)
        fs._root = HOME
        assert fs.validpath(HOME)
        assert fs.validpath(HOME + "/")
        assert not fs.validpath(HOME + "bar")

    if hasattr(os, "symlink"):

        def test_validpath_validlink(self):
            # Test validpath by issuing a symlink pointing to a path
            # inside the root directory.
            testfn = self.get_testfn()
            testfn2 = self.get_testfn()
            fs = AbstractedFS("/", None)
            fs._root = HOME
            touch(testfn)
            os.symlink(testfn, testfn2)
            assert fs.validpath(testfn)

        def test_validpath_external_symlink(self):
            # Test validpath by issuing a symlink pointing to a path
            # outside the root directory.
            fs = AbstractedFS("/", None)
            fs._root = HOME
            # tempfile should create our file in /tmp directory
            # which should be outside the user root.  If it is
            # not we just skip the test.
            testfn = self.get_testfn()
            with tempfile.NamedTemporaryFile() as file:
                try:
                    if os.path.dirname(file.name) == HOME:
                        return self.skipTest("tempdir is inside HOME")
                    os.symlink(file.name, testfn)
                    assert not fs.validpath(testfn)
                finally:
                    safe_rmpath(testfn)


class TestRenameNoReplace(PyftpdlibTestCase):
    """Tests for the atomic, non-replacing rename primitive and the
    object identity fingerprint used to pin an RNFR selection.
    """

    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="pyftpd-rnfr-")
        self.addCleanup(safe_rmpath, self.root)
        self.fs = AbstractedFS(self.root, None)

    def _path(self, name):
        return os.path.join(self.root, name)

    def test_file_identity_stable_for_same_object(self):
        path = self._path("a")
        touch(path)
        assert self.fs.file_identity(path) == self.fs.file_identity(path)

    def test_file_identity_changes_when_replaced(self):
        path = self._path("a")
        touch(path)
        before = self.fs.file_identity(path)
        # a brand new inode at the same pathname must look different
        os.remove(path)
        touch(path)
        assert self.fs.file_identity(path) != before

    def test_file_identity_changes_when_mutated(self):
        path = self._path("a")
        with open(path, "w") as f:
            f.write("one")
        before = self.fs.file_identity(path)
        with open(path, "a") as f:
            f.write("-two")
        assert self.fs.file_identity(path) != before

    def test_file_identity_handles_broken_symlink(self):
        if not hasattr(os, "symlink"):
            return self.skipTest("os.symlink not available")
        link = self._path("link")
        os.symlink(self._path("missing"), link)
        # lstat-based identity must work even though the link is broken
        assert self.fs.file_identity(link) == self.fs.file_identity(link)

    def test_rename_noreplace_when_unsupported(self):
        class WeakFS(AbstractedFS):
            supports_noreplace_rename = False

        fs = WeakFS(self.root, None)
        touch(self._path("a"))
        with pytest.raises(RenameNotGuaranteedError):
            fs.rename_noreplace(self._path("a"), self._path("b"))
        # the source is left untouched: no check-then-rename fallback
        assert os.path.exists(self._path("a"))
        assert not os.path.exists(self._path("b"))

    @pytest.mark.skipif(
        not AbstractedFS.supports_noreplace_rename,
        reason="platform has no atomic non-replacing rename",
    )
    def test_rename_noreplace_success(self):
        touch(self._path("a"))
        self.fs.rename_noreplace(self._path("a"), self._path("b"))
        assert not os.path.exists(self._path("a"))
        assert os.path.exists(self._path("b"))

    @pytest.mark.skipif(
        not AbstractedFS.supports_noreplace_rename,
        reason="platform has no atomic non-replacing rename",
    )
    def test_rename_noreplace_refuses_existing_target(self):
        with open(self._path("a"), "w") as f:
            f.write("source")
        with open(self._path("b"), "w") as f:
            f.write("destination")
        with pytest.raises(OSError) as exc:
            self.fs.rename_noreplace(self._path("a"), self._path("b"))
        assert exc.value.errno == errno.EEXIST
        # the atomic syscall must not have replaced either path
        assert open(self._path("b")).read() == "destination"
        assert os.path.exists(self._path("a"))


@pytest.mark.skipif(not POSIX, reason="UNIX only")
class TestUnixFilesystem(PyftpdlibTestCase):

    def test_case(self):
        root = os.getcwd()
        fs = UnixFilesystem(root, None)
        assert fs.root == root
        assert fs.cwd == root
        cdup = os.path.dirname(root)
        assert fs.ftp2fs("..") == cdup
        assert fs.fs2ftp(root) == root
