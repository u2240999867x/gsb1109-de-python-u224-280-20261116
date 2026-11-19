# Copyright (C) 2007 Giampaolo Rodola' <g.rodola@gmail.com>.
# Use of this source code is governed by MIT license that can be
# found in the LICENSE file.

import contextlib
import ftplib
import io
import os
import types

import pytest

from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.exceptions import RulebookError
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.rulebooks import CAPABILITIES
from pyftpdlib.rulebooks import PERM_TO_CAPABILITY
from pyftpdlib.rulebooks import READ
from pyftpdlib.rulebooks import RENAME
from pyftpdlib.rulebooks import WRITE
from pyftpdlib.rulebooks import AuditRecord
from pyftpdlib.rulebooks import DirectoryRule
from pyftpdlib.rulebooks import Rulebook
from pyftpdlib.rulebooks import TransferPolicy
from pyftpdlib.rulebooks import VersionedRuleAuthorizer

from . import BUFSIZE
from . import GLOBAL_TIMEOUT
from . import HOME
from . import PASSWD
from . import POSIX
from . import USER
from . import FtpdThreadWrapper
from . import PyftpdlibTestCase
from . import close_client
from . import touch


class TestDirectoryRule(PyftpdlibTestCase):
    def test_invalid_path(self):
        with pytest.raises(RulebookError):
            DirectoryRule("relative/path", [READ])
        with pytest.raises(RulebookError):
            DirectoryRule(None, [READ])

    def test_invalid_capabilities(self):
        with pytest.raises(RulebookError, match="no such capabilities"):
            DirectoryRule(HOME, ["bogus"])
        with pytest.raises(RulebookError, match="at least one"):
            DirectoryRule(HOME, [])
        with pytest.raises(RulebookError):
            DirectoryRule(HOME, None)

    def test_valid_capabilities(self):
        for cap in CAPABILITIES:
            rule = DirectoryRule(HOME, [cap])
            assert rule.capabilities == {cap}
        # a single string is accepted as a shortcut
        rule = DirectoryRule(HOME, READ)
        assert rule.capabilities == {READ}

    def test_users_normalization(self):
        rule = DirectoryRule(HOME, [READ], users=USER)
        assert rule.users == {USER}
        rule = DirectoryRule(HOME, [READ], users=[USER, "other"])
        assert rule.users == {USER, "other"}
        rule = DirectoryRule(HOME, [READ])
        assert rule.users is None

    def test_path_is_canonicalized(self):
        rule = DirectoryRule(os.path.join(HOME, "x", ".."), [READ])
        assert rule.path == os.path.normcase(os.path.realpath(HOME))


class TestRulebook(PyftpdlibTestCase):
    def test_invalid_version(self):
        for version in (None, "", "   ", True, 1.5, object()):
            with pytest.raises(RulebookError):
                Rulebook(version)
        for version in ("0", "v1", 0, 1, 20261116):
            assert Rulebook(version).version == version

    def test_invalid_rules(self):
        with pytest.raises(RulebookError, match="expected a DirectoryRule"):
            Rulebook("v1", [{"path": HOME}])
        with pytest.raises(RulebookError):
            Rulebook("v1", [None])

    def test_decide_no_match_returns_none(self):
        rulebook = Rulebook("v1", [DirectoryRule(HOME, [READ])])
        assert rulebook.decide(USER, READ, "/nonexistent/dir") is None

    def test_decide_invalid_capability(self):
        rulebook = Rulebook("v1")
        with pytest.raises(RulebookError, match="no such capability"):
            rulebook.decide(USER, "bogus", HOME)

    def test_decide_allow_and_deny(self):
        rulebook = Rulebook(
            "v1", [DirectoryRule(HOME, [READ], recursive=True)]
        )
        path = os.path.join(HOME, "somefile")
        assert rulebook.decide(USER, READ, path) is True
        assert rulebook.decide(USER, WRITE, path) is False
        assert rulebook.decide(USER, RENAME, path) is False

    def test_decide_non_recursive(self):
        rulebook = Rulebook(
            "v1", [DirectoryRule(HOME, [READ], recursive=False)]
        )
        subdir = os.path.join(HOME, "subdir")
        subfile = os.path.join(subdir, "file")
        afile = os.path.join(HOME, "file")
        assert rulebook.decide(USER, READ, afile) is True
        # sub directories are not covered
        assert rulebook.decide(USER, READ, subfile) is None

    def test_decide_most_specific_rule_wins(self):
        subdir = os.path.join(HOME, "subdir")
        rulebook = Rulebook(
            "v1",
            [
                DirectoryRule(HOME, [READ]),
                DirectoryRule(subdir, [WRITE]),
            ],
        )
        path = os.path.join(subdir, "file")
        # the longest path rule wins
        assert rulebook.decide(USER, WRITE, path) is True
        assert rulebook.decide(USER, READ, path) is False

    def test_decide_user_specific_rule_wins(self):
        rulebook = Rulebook(
            "v1",
            [
                DirectoryRule(HOME, [READ]),
                DirectoryRule(HOME, [WRITE], users=[USER]),
            ],
        )
        path = os.path.join(HOME, "file")
        assert rulebook.decide(USER, WRITE, path) is True
        assert rulebook.decide(USER, READ, path) is False
        # other users are subjected to the global rule
        assert rulebook.decide("other", READ, path) is True
        assert rulebook.decide("other", WRITE, path) is False

    def test_all_perm_letters_are_mapped(self):
        all_perms = set(
            DummyAuthorizer.read_perms + DummyAuthorizer.write_perms
        )
        assert set(PERM_TO_CAPABILITY) == all_perms
        assert set(PERM_TO_CAPABILITY.values()) <= set(CAPABILITIES)


class FakeDataChannel:
    """A minimal stand-in for pyftpdlib.handlers.DTPHandler."""

    def __init__(self, cmd, path):
        self.cmd = cmd
        self.file_obj = types.SimpleNamespace(name=path)
        self.closed = False

    def transfer_in_progress(self):
        return not self.closed

    def close(self):
        self.closed = True


class FakeSession:
    """A minimal stand-in for pyftpdlib.handlers.FTPHandler."""

    def __init__(self, username, channel=None):
        self.username = username
        self.data_channel = channel
        self.responses = []

    def respond(self, resp, logfun=None):
        self.responses.append(resp)


class TestVersionedRuleAuthorizer(PyftpdlibTestCase):
    def setUp(self):
        super().setUp()
        self.tempdir = os.path.abspath(self.get_testfn())
        self.subdir = os.path.join(self.tempdir, "subdir")
        self.tempfile = os.path.join(self.tempdir, "file.txt")
        self.subfile = os.path.join(self.subdir, "file.txt")
        os.mkdir(self.tempdir)
        os.mkdir(self.subdir)
        touch(self.tempfile)
        touch(self.subfile)
        self.auth = VersionedRuleAuthorizer()
        self.auth.add_user(USER, PASSWD, self.tempdir, perm="elr")

    def make_auth(self, **kwargs):
        return VersionedRuleAuthorizer(**kwargs)

    def test_initial_state(self):
        assert self.auth.current_version == "0"
        assert self.auth.get_rulebook().rules == ()
        assert self.auth.transfer_policy == TransferPolicy.CONTINUE
        assert self.auth.audit_log == []

    def test_invalid_initial_rulebook(self):
        with pytest.raises(RulebookError):
            self.make_auth(rulebook="not-a-rulebook")

    def test_invalid_transfer_policy(self):
        with pytest.raises(ValueError, match="invalid transfer policy"):
            self.make_auth(transfer_policy="bogus")

    def test_capabilities_judged_separately(self):
        self.auth.update_rules("v1", [DirectoryRule(self.tempdir, [READ])])
        for perm in "elr":
            assert self.auth.has_perm(USER, perm, self.tempfile)
        for perm in "admwMT":
            assert not self.auth.has_perm(USER, perm, self.tempfile)
        assert not self.auth.has_perm(USER, "f", self.tempfile)
        # write only
        self.auth.update_rules("v2", [DirectoryRule(self.tempdir, [WRITE])])
        for perm in "elr":
            assert not self.auth.has_perm(USER, perm, self.tempfile)
        for perm in "admwMT":
            assert self.auth.has_perm(USER, perm, self.tempfile)
        assert not self.auth.has_perm(USER, "f", self.tempfile)
        # rename only
        self.auth.update_rules("v3", [DirectoryRule(self.tempdir, [RENAME])])
        for perm in "elradmwMT":
            assert not self.auth.has_perm(USER, perm, self.tempfile)
        assert self.auth.has_perm(USER, "f", self.tempfile)

    def test_subsequent_checks_use_new_version(self):
        self.auth.update_rules(
            "v1", [DirectoryRule(self.tempdir, [READ, WRITE, RENAME])]
        )
        assert self.auth.has_perm(USER, "w", self.tempfile)
        self.auth.update_rules("v2", [DirectoryRule(self.tempdir, [READ])])
        assert self.auth.current_version == "v2"
        # the very same check now yields a different result
        assert not self.auth.has_perm(USER, "w", self.tempfile)
        assert self.auth.has_perm(USER, "r", self.tempfile)

    def test_failed_update_keeps_old_version(self):
        self.auth.update_rules("v1", [DirectoryRule(self.tempdir, [READ])])
        # invalid capability
        with pytest.raises(RulebookError):
            self.auth.update_rules(
                "v2", [DirectoryRule(self.tempdir, ["bogus"])]
            )
        # invalid version
        with pytest.raises(RulebookError):
            self.auth.update_rules("", [DirectoryRule(self.tempdir, [READ])])
        # invalid rule type
        with pytest.raises(RulebookError):
            self.auth.update_rules("v2", [{"path": self.tempdir}])
        # old version is still active and still decides
        assert self.auth.current_version == "v1"
        assert self.auth.has_perm(USER, "r", self.tempfile)
        assert not self.auth.has_perm(USER, "w", self.tempfile)

    def test_fallback_to_base_perms(self):
        # no rule matches: base DummyAuthorizer perms decide
        assert self.auth.has_perm(USER, "r", self.tempfile)
        assert not self.auth.has_perm(USER, "w", self.tempfile)
        assert len(self.auth.audit_log) == 2
        assert all(r.basis == "fallback" for r in self.auth.audit_log)
        # no path at all: base DummyAuthorizer behavior, no audit
        assert self.auth.has_perm(USER, "r")
        assert not self.auth.has_perm(USER, "w")
        assert len(self.auth.audit_log) == 2

    @pytest.mark.skipif(not POSIX, reason="POSIX only")
    def test_virtual_path_alias_cannot_bypass(self):
        # a symlinked virtual path pointing to a file whose read
        # permission was revoked must not bypass the revocation
        alias = os.path.join(self.tempdir, "alias")
        alias_file = os.path.join(alias, "file.txt")
        os.symlink(self.subdir, alias)
        auth = self.make_auth()
        auth.add_user(USER, PASSWD, self.tempdir, perm="elradfmwMT")
        # no rules yet: the alias works
        assert auth.has_perm(USER, "r", alias_file)
        # revoke read+write on the real directory, keep rename only
        auth.update_rules("v1", [DirectoryRule(self.subdir, [RENAME])])
        assert not auth.has_perm(USER, "r", self.subfile)
        assert not auth.has_perm(USER, "r", alias_file)
        assert not auth.has_perm(USER, "w", alias_file)
        assert auth.has_perm(USER, "f", alias_file)

    def test_audit_records_rule_version(self):
        self.auth.has_perm(USER, "r", self.tempfile)  # fallback, v0
        self.auth.update_rules("v1", [DirectoryRule(self.tempdir, [READ])])
        self.auth.has_perm(USER, "r", self.tempfile)  # rule, v1
        self.auth.has_perm(USER, "w", self.tempfile)  # rule, v1
        records = self.auth.audit_log
        assert len(records) == 3
        assert records[0].version == "0"
        assert records[0].basis == "fallback"
        assert records[0].allowed is True
        assert records[1].version == "v1"
        assert records[1].basis == "rule"
        assert records[1].allowed is True
        assert records[2].version == "v1"
        assert records[2].allowed is False
        assert records[2].capability == WRITE
        for record in records:
            assert isinstance(record, AuditRecord)
            assert record.username == USER

    def test_audit_never_records_credentials(self):
        self.auth.validate_authentication(USER, PASSWD, None)
        self.auth.has_perm(USER, "r", self.tempfile)
        assert "password" not in AuditRecord._fields
        assert PASSWD not in repr(self.auth.audit_log)

    def test_audit_callable_sink(self):
        records = []
        auth = self.make_auth(audit_log=records.append)
        auth.add_user(USER, PASSWD, self.tempdir, perm="elr")
        auth.has_perm(USER, "r", self.tempfile)
        assert len(records) == 1
        assert isinstance(records[0], AuditRecord)

    def test_session_registry(self):
        session = FakeSession(USER)
        self.auth.register_session(session)
        assert self.auth.sessions == [session]
        self.auth.unregister_session(session)
        assert self.auth.sessions == []
        # idempotent
        self.auth.unregister_session(session)
        assert self.auth.sessions == []


class TestHandlerIntegration(PyftpdlibTestCase):
    """Test the FTPHandler hooks registering/unregistering sessions
    with the authorizer (socket-free).
    """

    def make_handler(self, authorizer):
        handler = FTPHandler.__new__(FTPHandler)
        handler.authorizer = authorizer
        handler.username = USER
        handler.authenticated = True
        handler.data_channel = None
        handler._dtp_acceptor = None
        handler._dtp_connector = None
        handler.respond = lambda *args, **kwargs: None
        handler.push = lambda *args, **kwargs: None
        handler.log = lambda *args, **kwargs: None
        handler.abstracted_fs = lambda home, cmd: types.SimpleNamespace()
        handler.on_login = lambda username: None
        return handler

    def test_login_registers_session(self):
        auth = VersionedRuleAuthorizer()
        handler = self.make_handler(auth)
        handler.handle_auth_success(HOME, PASSWD, "Login successful.")
        assert auth.sessions == [handler]

    def test_logout_unregisters_session(self):
        auth = VersionedRuleAuthorizer()
        handler = self.make_handler(auth)
        handler.handle_auth_success(HOME, PASSWD, "Login successful.")
        handler.flush_account()
        assert auth.sessions == []

    def test_plain_authorizers_are_not_affected(self):
        # DummyAuthorizer has no register_session hook: the handler
        # must keep working as before
        handler = self.make_handler(DummyAuthorizer())
        handler.handle_auth_success(HOME, PASSWD, "Login successful.")
        handler.flush_account()


class TestTransferPolicy(PyftpdlibTestCase):
    def make_auth(self, policy):
        auth = VersionedRuleAuthorizer(transfer_policy=policy)
        auth.add_user(USER, PASSWD, HOME, perm="elr")
        auth.add_user("other", "pw", HOME, perm="elr")
        return auth

    def make_session(self, username, cmd="RETR", path=None):
        if path is None:
            path = os.path.join(HOME, "file.txt")
        return FakeSession(username, FakeDataChannel(cmd, path))

    def test_continue_policy_keeps_transfers(self):
        auth = self.make_auth(TransferPolicy.CONTINUE)
        session = self.make_session(USER)
        auth.register_session(session)
        auth.update_rules("v1", [DirectoryRule(HOME, [WRITE])])
        assert not session.data_channel.closed

    def test_terminate_policy_stops_revoked_transfer(self):
        auth = self.make_auth(TransferPolicy.TERMINATE)
        session = self.make_session(USER)
        auth.register_session(session)
        auth.update_rules("v1", [DirectoryRule(HOME, [WRITE])])
        assert session.data_channel.closed
        assert session.responses[0].startswith("426")
        assert "v1" in session.responses[0]

    def test_terminate_policy_spares_other_users(self):
        # revoking USER's permission must not close everybody else's
        # transfers (no blanket close)
        auth = self.make_auth(TransferPolicy.TERMINATE)
        s1 = self.make_session(USER)
        s2 = self.make_session("other")
        auth.register_session(s1)
        auth.register_session(s2)
        auth.update_rules("v1", [DirectoryRule(HOME, [WRITE], users=[USER])])
        assert s1.data_channel.closed
        assert not s2.data_channel.closed

    def test_terminate_policy_spares_authorized_transfers(self):
        # the transfer is still allowed by the new rules: keep it
        auth = self.make_auth(TransferPolicy.TERMINATE)
        session = self.make_session(USER)
        auth.register_session(session)
        auth.update_rules("v1", [DirectoryRule(HOME, [READ])])
        assert not session.data_channel.closed

    def test_terminate_policy_spares_unrelated_paths(self):
        auth = self.make_auth(TransferPolicy.TERMINATE)
        elsewhere = os.path.join(os.path.dirname(HOME), "elsewhere.txt")
        session = self.make_session(USER, path=elsewhere)
        auth.register_session(session)
        auth.update_rules("v1", [DirectoryRule(HOME, [WRITE])])
        # the transfer concerns a path the new rules don't cover
        assert not session.data_channel.closed

    def test_terminate_policy_ignores_unassessable_transfers(self):
        auth = self.make_auth(TransferPolicy.TERMINATE)
        session = self.make_session(USER, cmd=None)
        auth.register_session(session)
        auth.update_rules("v1", [DirectoryRule(HOME, [WRITE])])
        assert not session.data_channel.closed

    def test_terminated_transfer_is_audited(self):
        auth = self.make_auth(TransferPolicy.TERMINATE)
        session = self.make_session(USER)
        auth.register_session(session)
        auth.update_rules("v1", [DirectoryRule(HOME, [WRITE])])
        record = auth.audit_log[-1]
        assert record.basis == "transfer-policy"
        assert record.version == "v1"
        assert record.allowed is False
        assert record.capability == READ
        assert record.username == USER

    def test_unregistered_session_is_untouched(self):
        auth = self.make_auth(TransferPolicy.TERMINATE)
        session = self.make_session(USER)
        auth.register_session(session)
        auth.unregister_session(session)
        auth.update_rules("v1", [DirectoryRule(HOME, [WRITE])])
        assert not session.data_channel.closed


class TestVersionedRulesFunctional(PyftpdlibTestCase):
    """Functional tests: rules updated after login are enforced on
    subsequent commands of the same session.
    """

    transfer_policy = TransferPolicy.CONTINUE

    def setUp(self):
        super().setUp()
        self.authorizer = VersionedRuleAuthorizer(
            transfer_policy=self.transfer_policy
        )
        self.authorizer.add_user(USER, PASSWD, HOME, perm="elradfmwMT")
        handler = type(
            "Handler", (FTPHandler,), {"authorizer": self.authorizer}
        )
        self.server = FtpdThreadWrapper()
        self.server.server.handler = handler
        self.server.start()
        self.client = ftplib.FTP(timeout=GLOBAL_TIMEOUT)
        self.client.connect(self.server.host, self.server.port)
        self.client.login(USER, PASSWD)

    def tearDown(self):
        close_client(self.client)
        self.server.stop()
        super().tearDown()

    def update_rules(self, version, rules):
        with self.server.lock:
            return self.authorizer.update_rules(version, rules)

    def test_logged_in_session_is_registered(self):
        assert len(self.authorizer.sessions) == 1
        self.client.sendcmd("rein")
        assert self.authorizer.sessions == []
        self.client.login(USER, PASSWD)
        assert len(self.authorizer.sessions) == 1

    def test_update_applies_to_subsequent_commands(self):
        testfn = self.get_testfn()
        self.client.storbinary(f"stor {testfn}", io.BytesIO(b"abc"))
        # revoke write and rename, keep read
        self.update_rules("v2", [DirectoryRule(HOME, [READ])])
        with pytest.raises(ftplib.error_perm, match="550"):
            self.client.storbinary(
                f"stor {self.get_testfn()}", io.BytesIO(b"x")
            )
        with pytest.raises(ftplib.error_perm, match="550"):
            self.client.rename(testfn, self.get_testfn())
        # read is still allowed
        self.client.retrbinary(f"retr {testfn}", io.BytesIO().write)
        # restore full access with a newer version
        self.update_rules("v3", [DirectoryRule(HOME, [READ, WRITE, RENAME])])
        self.client.storbinary(f"stor {self.get_testfn()}", io.BytesIO(b"x"))
        renamed = self.get_testfn()
        self.client.rename(testfn, renamed)
        assert self.authorizer.audit_log[-1].version == "v3"

    def test_failed_update_keeps_old_version(self):
        testfn = self.get_testfn()
        touch(testfn)
        self.update_rules("v2", [DirectoryRule(HOME, [READ])])
        with pytest.raises(RulebookError):
            self.authorizer.update_rules(
                "v3", [DirectoryRule(HOME, ["bogus"])]
            )
        assert self.authorizer.current_version == "v2"
        # the old version is still the one being enforced
        with pytest.raises(ftplib.error_perm, match="550"):
            self.client.storbinary(
                f"stor {self.get_testfn()}", io.BytesIO(b"x")
            )
        self.client.retrbinary(f"retr {testfn}", io.BytesIO().write)

    def test_audit_trail_reports_version_and_no_credentials(self):
        testfn = self.get_testfn()
        touch(testfn)
        self.client.retrbinary(f"retr {testfn}", io.BytesIO().write)
        self.update_rules("v2", [DirectoryRule(HOME, [READ])])
        self.client.retrbinary(f"retr {testfn}", io.BytesIO().write)
        versions = [r.version for r in self.authorizer.audit_log]
        assert "0" in versions
        assert "v2" in versions
        assert PASSWD not in repr(self.authorizer.audit_log)


class TestTransferPolicyFunctional(PyftpdlibTestCase):
    """Functional tests for the continue/terminate transfer policies
    applied to data transfers already in progress when rules change.
    """

    transfer_policy = TransferPolicy.CONTINUE
    data = b"abcde12345" * 1000000  # 10MB

    def setUp(self):
        super().setUp()
        self.authorizer = VersionedRuleAuthorizer(
            transfer_policy=self.transfer_policy
        )
        self.authorizer.add_user(USER, PASSWD, HOME, perm="elradfmwMT")
        self.authorizer.add_user("other", "pw", HOME, perm="elr")
        handler = type(
            "Handler", (FTPHandler,), {"authorizer": self.authorizer}
        )
        self.server = FtpdThreadWrapper()
        self.server.server.handler = handler
        self.server.start()
        self.client = ftplib.FTP(timeout=GLOBAL_TIMEOUT)
        self.client.connect(self.server.host, self.server.port)
        self.client.login(USER, PASSWD)
        self.testfn = self.get_testfn()
        with open(self.testfn, "wb") as f:
            f.write(self.data)
        self.client.voidcmd("TYPE I")

    def tearDown(self):
        close_client(self.client)
        self.server.stop()
        super().tearDown()

    def update_rules(self, version, rules):
        with self.server.lock:
            return self.authorizer.update_rules(version, rules)

    def recv_some(self, conn, amount=65536):
        received = 0
        while received < amount:
            chunk = conn.recv(BUFSIZE)
            if not chunk:
                break
            received += len(chunk)
        return received


class TestTransferPolicyContinue(TestTransferPolicyFunctional):
    transfer_policy = TransferPolicy.CONTINUE

    def test_in_flight_transfer_is_not_interrupted(self):
        with contextlib.closing(
            self.client.transfercmd("retr " + self.testfn)
        ) as conn:
            bytes_recv = self.recv_some(conn)
            # revoke read permission mid-transfer
            self.update_rules("v2", [DirectoryRule(HOME, [WRITE])])
            while True:
                chunk = conn.recv(BUFSIZE)
                if not chunk:
                    break
                bytes_recv += len(chunk)
        # the in-flight transfer ran to completion...
        assert bytes_recv == len(self.data)
        assert self.client.voidresp().startswith("226")
        # ...but new commands are checked against the new version
        with pytest.raises(ftplib.error_perm, match="550"):
            self.client.retrbinary("retr " + self.testfn, io.BytesIO().write)


class TestTransferPolicyTerminate(TestTransferPolicyFunctional):
    transfer_policy = TransferPolicy.TERMINATE

    def test_revoked_transfer_is_terminated(self):
        with contextlib.closing(
            self.client.transfercmd("retr " + self.testfn)
        ) as conn:
            bytes_recv = self.recv_some(conn)
            # revoke read permission mid-transfer
            self.update_rules("v2", [DirectoryRule(HOME, [WRITE])])
            while True:
                chunk = conn.recv(BUFSIZE)
                if not chunk:
                    break
                bytes_recv += len(chunk)
        # the transfer was cut before completion
        assert bytes_recv < len(self.data)
        assert self.client.getline()[:3] == "426"
        record = self.authorizer.audit_log[-1]
        assert record.basis == "transfer-policy"
        assert record.version == "v2"
        # control connection is still alive and new commands are
        # checked against the new rules version
        assert self.client.sendcmd("noop").startswith("200")
        with pytest.raises(ftplib.error_perm, match="550"):
            self.client.retrbinary("retr " + self.testfn, io.BytesIO().write)

    def test_other_users_transfers_are_not_terminated(self):
        client2 = ftplib.FTP(timeout=GLOBAL_TIMEOUT)
        client2.connect(self.server.host, self.server.port)
        client2.login("other", "pw")
        client2.voidcmd("TYPE I")
        self.addCleanup(close_client, client2)
        with (
            contextlib.closing(
                self.client.transfercmd("retr " + self.testfn)
            ) as conn1,
            contextlib.closing(
                client2.transfercmd("retr " + self.testfn)
            ) as conn2,
        ):
            bytes1 = self.recv_some(conn1)
            bytes2 = self.recv_some(conn2)
            # revoke read permission for USER only
            self.update_rules(
                "v2", [DirectoryRule(HOME, [WRITE], users=[USER])]
            )
            while True:
                chunk = conn1.recv(BUFSIZE)
                if not chunk:
                    break
                bytes1 += len(chunk)
            while True:
                chunk = conn2.recv(BUFSIZE)
                if not chunk:
                    break
                bytes2 += len(chunk)
        # USER's transfer was terminated...
        assert bytes1 < len(self.data)
        assert self.client.getline()[:3] == "426"
        # ...while "other"'s transfer ran to completion
        assert bytes2 == len(self.data)
        assert client2.voidresp().startswith("226")
