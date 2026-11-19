# Copyright (C) 2007 Giampaolo Rodola' <g.rodola@gmail.com>.
# Use of this source code is governed by MIT license that can be
# found in the LICENSE file.

"""Socket-free tests for the RNFR/RNTO two-step rename protocol.

The functional suite in test_functional.py exercises the same commands
over a real loopback connection but needs a listening socket, which some
restricted CI environments cannot provide. These tests drive the real
FTPHandler.ftp_RNFR / ftp_RNTO methods in-process against a real
AbstractedFS and DummyAuthorizer, stubbing only the socket-bound
respond() method.
"""

import os
import tempfile

from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.exceptions import RenameNotGuaranteedError
from pyftpdlib.filesystems import AbstractedFS
from pyftpdlib.handlers.ftp.control import FTPHandler

from . import PyftpdlibTestCase
from . import safe_rmpath
from . import touch

USER = "user"
PASSWD = "12345"


class _RenamingHandler:
    """Builds an FTPHandler without invoking its socket-based __init__."""

    def __init__(self, root, fs=None, perm="elradfmwMT"):
        handler = FTPHandler.__new__(FTPHandler)
        handler.username = USER
        handler.password = PASSWD
        handler.remote_ip = "127.0.0.1"
        handler.remote_port = "0"
        handler._rnfr = None
        handler._log_debug = False
        handler._dtp_acceptor = None
        handler._dtp_connector = None
        handler.data_channel = None
        authorizer = DummyAuthorizer()
        authorizer.add_user(USER, PASSWD, root, perm=perm)
        handler.authorizer = authorizer
        self.fs = fs or AbstractedFS(root, handler)
        handler.fs = self.fs
        self.responses = []

        def respond(resp, logfun=None):
            handler._last_response = resp
            self.responses.append(resp)

        handler.respond = respond
        self.handler = handler

    @property
    def code(self):
        return self.responses[-1][:3]

    @property
    def text(self):
        return self.responses[-1]


class TestRenameProtocol(PyftpdlibTestCase):

    def setUp(self):
        super().setUp()
        self.root = tempfile.mkdtemp(prefix="pyftpd-rnfr-")
        self.addCleanup(safe_rmpath, self.root)
        self.harness = _RenamingHandler(self.root)
        self.handler = self.harness.handler

    def _path(self, name):
        return os.path.join(self.root, name)

    @property
    def code(self):
        return self.harness.code

    @property
    def text(self):
        return self.harness.text

    def test_success_pins_and_consumes_selection(self):
        touch(self._path("a"))
        self.handler.ftp_RNFR(self._path("a"))
        assert self.code == "350"
        assert self.handler._rnfr is not None
        result = self.handler.ftp_RNTO(self._path("b"))
        assert self.code == "250"
        assert result == (self._path("a"), self._path("b"))
        # the success message identifies the real source and destination
        assert "/a" in self.text and "/b" in self.text
        assert not os.path.exists(self._path("a"))
        assert os.path.exists(self._path("b"))
        # two-step state never survives the exchange
        assert self.handler._rnfr is None

    def test_rnto_without_rnfr(self):
        self.handler.ftp_RNTO(self._path("a"))
        assert self.code == "503"
        assert "use RNFR first" in self.text

    def test_rnfr_missing_path_sets_no_state(self):
        self.handler.ftp_RNFR(self._path("missing"))
        assert self.code == "550"
        assert self.handler._rnfr is None

    def test_destination_conflict_is_not_overwritten(self):
        with open(self._path("dst"), "w") as f:
            f.write("keep")
        with open(self._path("src"), "w") as f:
            f.write("move")
        self.handler.ftp_RNFR(self._path("src"))
        self.handler.ftp_RNTO(self._path("dst"))
        assert self.code == "550"
        assert "already exists" in self.text
        assert open(self._path("dst")).read() == "keep"
        assert os.path.exists(self._path("src"))
        # a failed RNTO consumes the state: no implicit retry
        self.handler.ftp_RNTO(self._path("dst"))
        assert self.code == "503"

    def test_swapped_source_is_rejected(self):
        touch(self._path("a"))
        self.handler.ftp_RNFR(self._path("a"))
        # an administrator replaces the path with a different object
        os.remove(self._path("a"))
        touch(self._path("a"))
        self.handler.ftp_RNTO(self._path("b"))
        assert self.code == "550"
        assert "Source changed" in self.text
        assert not os.path.exists(self._path("b"))

    def test_mutated_source_version_is_rejected(self):
        with open(self._path("a"), "w") as f:
            f.write("one")
        self.handler.ftp_RNFR(self._path("a"))
        with open(self._path("a"), "a") as f:
            f.write("-two")
        self.handler.ftp_RNTO(self._path("b"))
        assert self.code == "550"
        assert "Source changed" in self.text

    def test_vanished_source_is_rejected(self):
        touch(self._path("a"))
        self.handler.ftp_RNFR(self._path("a"))
        os.remove(self._path("a"))
        self.handler.ftp_RNTO(self._path("b"))
        assert self.code == "550"
        assert "no longer available" in self.text

    def test_permission_revoked_between_rnfr_and_rnto(self):
        touch(self._path("a"))
        self.handler.ftp_RNFR(self._path("a"))
        self.handler.authorizer.user_table[USER]["perm"] = "elr"
        self.handler.ftp_RNTO(self._path("b"))
        assert self.code == "550"
        assert "privileges" in self.text
        assert os.path.exists(self._path("a"))
        assert not os.path.exists(self._path("b"))

    def test_abor_cancels_pending_rnfr(self):
        touch(self._path("a"))
        self.handler.ftp_RNFR(self._path("a"))
        self.handler.ftp_ABOR("")
        # data-channel reply is preserved while rename state is cleared
        assert self.code == "225"
        assert self.handler._rnfr is None
        self.handler.ftp_RNTO(self._path("b"))
        assert self.code == "503"
        assert os.path.exists(self._path("a"))

    def test_repeated_rnfr_reselects_source(self):
        touch(self._path("a"))
        touch(self._path("b"))
        self.handler.ftp_RNFR(self._path("a"))
        self.handler.ftp_RNFR(self._path("b"))
        self.handler.ftp_RNTO(self._path("c"))
        assert self.code == "250"
        # only the second selection is renamed
        assert os.path.exists(self._path("a"))
        assert not os.path.exists(self._path("b"))
        assert os.path.exists(self._path("c"))

    def test_broken_symlink_can_be_renamed(self):
        if not hasattr(os, "symlink"):
            return self.skipTest("os.symlink not available")
        os.symlink(self._path("missing"), self._path("link"))
        self.handler.ftp_RNFR(self._path("link"))
        assert self.code == "350"
        self.handler.ftp_RNTO(self._path("link2"))
        assert self.code == "250"
        assert os.path.islink(self._path("link2"))
        assert not os.path.lexists(self._path("link"))

    def test_no_atomic_guarantee_fails_explicitly(self):
        class WeakFS(AbstractedFS):
            supports_noreplace_rename = False

            def rename_noreplace(self, src, dst):
                raise RenameNotGuaranteedError("no atomic primitive")

        harness = _RenamingHandler(self.root, fs=WeakFS(self.root, None))
        harness.handler.fs.cmd_channel = harness.handler
        touch(self._path("a"))
        harness.handler.ftp_RNFR(self._path("a"))
        harness.handler.ftp_RNTO(self._path("b"))
        # temporary failure: the backend, not the command, is the limit
        assert harness.code == "451"
        assert os.path.exists(self._path("a"))
        assert not os.path.exists(self._path("b"))
