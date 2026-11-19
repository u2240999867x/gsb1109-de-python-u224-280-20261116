# Copyright (C) 2007 Giampaolo Rodola' <g.rodola@gmail.com>.
# Use of this source code is governed by MIT license that can be
# found in the LICENSE file.

"""Tests for pyftpdlib.quota (upload quota ledger)."""

import errno
import ftplib
import io
import os
import threading
import time

import pytest

from pyftpdlib.filesystems import AbstractedFS
from pyftpdlib.quota import BlockOccupancyRules
from pyftpdlib.quota import LogicalOccupancyRules
from pyftpdlib.quota import QuotaError
from pyftpdlib.quota import QuotaExceeded
from pyftpdlib.quota import QuotaLedger
from pyftpdlib.quota import QuotaManager
from pyftpdlib.quota import QuotaNotSupported
from pyftpdlib.quota import QuotaUploadFile

from . import GLOBAL_TIMEOUT
from . import HOME
from . import PASSWD
from . import USER
from . import FtpdThreadWrapper
from . import PyftpdlibTestCase
from . import close_client
from . import safe_rmpath


class NonAtomicRules(LogicalOccupancyRules):
    """A backend which cannot guarantee atomic reservations."""

    atomic_reservation = False


class FakeStat:
    def __init__(self, size, blocks=None):
        self.st_size = size
        if blocks is not None:
            self.st_blocks = blocks


class FakeFS:
    """Minimal stand-in for AbstractedFS."""

    def __init__(self, size=None, blocks=None):
        self.size = size
        self.blocks = blocks

    def getsize(self, path):
        if self.size is None:
            raise OSError(errno.ENOENT, "no such file")
        return self.size

    def stat(self, path):
        if self.size is None:
            raise OSError(errno.ENOENT, "no such file")
        return FakeStat(self.size, self.blocks)


# ===================================================================
# --- occupancy rules
# ===================================================================


class TestOccupancyRules(PyftpdlibTestCase):
    def test_logical_rules_measure_size(self):
        rules = LogicalOccupancyRules()
        assert rules.measure(FakeFS(size=100), "x") == 100
        assert rules.proves_sparse is False

    def test_logical_rules_unmeasurable(self):
        rules = LogicalOccupancyRules()
        assert rules.measure(FakeFS(), "x") is None

    def test_block_rules_prove_sparse(self):
        # 1MB logical file occupying a single 4k block: the discount
        # applies only because the backend proves it via st_blocks.
        rules = BlockOccupancyRules()
        assert rules.proves_sparse is True
        assert rules.measure(FakeFS(size=1024 * 1024, blocks=8), "x") == 4096

    def test_block_rules_fall_back_to_size(self):
        # Backend cannot report allocated blocks: no deduction.
        rules = BlockOccupancyRules()
        assert rules.measure(FakeFS(size=1000), "x") == 1000

    def test_strong_quota_refuses_non_atomic_backend(self):
        with pytest.raises(QuotaNotSupported):
            QuotaLedger(100, NonAtomicRules(), strong=True)
        # ...but can be enabled explicitly in non-strong mode
        ledger = QuotaLedger(100, NonAtomicRules(), strong=False)
        assert ledger.limit == 100

    def test_manager_refuses_non_atomic_backend(self):
        manager = QuotaManager()
        with pytest.raises(QuotaNotSupported):
            manager.set_quota(HOME, 100, rules=NonAtomicRules())
        assert manager.ledger_for(HOME) is None


# ===================================================================
# --- ledger
# ===================================================================


class TestQuotaLedger(PyftpdlibTestCase):
    def make_ledger(self, limit=1000):
        return QuotaLedger(limit, LogicalOccupancyRules())

    def make_ctx(self, ledger, baseline_occ=0, baseline_size=0, res=None):
        return ledger.begin_upload(
            FakeFS(), "x", baseline_occ, baseline_size, reservation=res
        )

    def test_stats(self):
        ledger = self.make_ledger()
        stats = ledger.stats()
        assert stats.confirmed == 0
        assert stats.reserved == 0
        assert stats.unknown == 0
        assert stats.limit == 1000
        assert stats.available == 1000

    def test_reserve_and_cancel(self):
        ledger = self.make_ledger()
        res = ledger.reserve(400)
        assert ledger.stats().reserved == 400
        assert ledger.stats().available == 600
        res.cancel()
        assert ledger.stats().reserved == 0
        assert ledger.stats().available == 1000

    def test_reserve_exceeding(self):
        ledger = self.make_ledger()
        ledger.reserve(600)
        with pytest.raises(QuotaExceeded):
            ledger.reserve(401)
        # failed reservation did not consume anything
        assert ledger.stats().reserved == 600

    def test_concurrent_reservations_are_atomic(self):
        # 20 sessions racing for the same remaining quota: exactly 10
        # reservations of 100 bytes must succeed, never 11.
        ledger = self.make_ledger()
        barrier = threading.Barrier(20)
        won = []
        lock = threading.Lock()

        def worker():
            barrier.wait()
            try:
                res = ledger.reserve(100)
            except QuotaExceeded:
                return
            with lock:
                won.append(res)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(won) == 10
        assert ledger.stats().reserved == 1000
        assert ledger.stats().available == 0

    def test_concurrent_spends_are_atomic(self):
        ledger = self.make_ledger()
        ctxs = [self.make_ctx(ledger) for _ in range(4)]
        barrier = threading.Barrier(4)

        def worker(ctx):
            barrier.wait()
            for _ in range(40):
                try:
                    ledger.spend(ctx, 10)
                except QuotaExceeded:
                    break

        threads = [threading.Thread(target=worker, args=(c,)) for c in ctxs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        stats = ledger.stats()
        assert stats.unknown <= 1000
        assert stats.available == 1000 - stats.unknown

    def test_spend_consumes_reservation_first(self):
        ledger = self.make_ledger()
        res = ledger.reserve(300)
        ctx = self.make_ctx(ledger, res=res)
        ledger.spend(ctx, 200)
        stats = ledger.stats()
        assert stats.reserved == 100  # 200 moved from reserved to unknown
        assert stats.unknown == 200
        assert stats.available == 700
        ledger.spend(ctx, 200)  # 100 from reservation, 100 fresh
        stats = ledger.stats()
        assert stats.reserved == 0
        assert stats.unknown == 400
        assert stats.available == 600

    def test_settle_confirms_growth_only(self):
        ledger = self.make_ledger()
        ctx = self.make_ctx(ledger, baseline_occ=100, baseline_size=100)
        ledger.spend(ctx, 50)
        ledger.settle(ctx, 150)
        stats = ledger.stats()
        assert stats.confirmed == 50  # delta vs baseline, not 150
        assert stats.unknown == 0
        assert stats.available == 950

    def test_settle_refunds_what_backend_disproves(self):
        # Billed 100 in flight, but the backend proves only 40 were
        # actually occupied (e.g. sparse data): refund the rest.
        ledger = self.make_ledger()
        ctx = self.make_ctx(ledger)
        ledger.spend(ctx, 100)
        ledger.settle(ctx, 40)
        stats = ledger.stats()
        assert stats.confirmed == 40
        assert stats.unknown == 0
        assert stats.available == 960

    def test_settle_releases_reservation(self):
        ledger = self.make_ledger()
        res = ledger.reserve(500)
        ctx = self.make_ctx(ledger, res=res)
        ledger.spend(ctx, 100)
        ledger.settle(ctx, 100)
        stats = ledger.stats()
        assert stats.reserved == 0
        assert stats.confirmed == 100
        assert stats.available == 900

    def test_settle_unmeasurable_keeps_billed(self):
        # The backend cannot prove the final occupancy: the bytes
        # billed in flight stay confirmed, quota is not released early.
        ledger = self.make_ledger()
        ctx = self.make_ctx(ledger)
        ledger.spend(ctx, 100)
        ledger.settle_unmeasurable(ctx)
        stats = ledger.stats()
        assert stats.confirmed == 100
        assert stats.unknown == 0

    def test_settle_is_idempotent(self):
        ledger = self.make_ledger()
        ctx = self.make_ctx(ledger)
        ledger.spend(ctx, 100)
        ledger.settle(ctx, 100)
        ledger.settle(ctx, 100)
        ledger.settle_unmeasurable(ctx)
        assert ledger.stats().confirmed == 100

    def test_settled_uploads_are_independent(self):
        # A committed upload is not rolled back by another one failing.
        ledger = self.make_ledger()
        ctx1 = self.make_ctx(ledger)
        ledger.spend(ctx1, 100)
        ledger.settle(ctx1, 100)
        ctx2 = self.make_ctx(ledger)
        ledger.spend(ctx2, 50)
        ledger.settle_unmeasurable(ctx2)
        stats = ledger.stats()
        assert stats.confirmed == 150

    def test_reclaim(self):
        ledger = self.make_ledger()
        ctx = self.make_ctx(ledger)
        ledger.spend(ctx, 100)
        ledger.settle(ctx, 100)
        ledger.reclaim(100)
        assert ledger.stats().confirmed == 0
        # clamps at 0
        ledger.reclaim(100)
        assert ledger.stats().confirmed == 0

    def test_spend_after_settle_raises(self):
        ledger = self.make_ledger()
        ctx = self.make_ctx(ledger)
        ledger.settle(ctx, 0)
        with pytest.raises(QuotaError):
            ledger.spend(ctx, 1)


# ===================================================================
# --- upload file wrapper
# ===================================================================


class TestQuotaUploadFile(PyftpdlibTestCase):
    def wrap(
        self, ledger, testfn, mode, start_pos=0, baseline_size=0,
        baseline_occ=0,
    ):
        fd = open(testfn, mode)
        ctx = ledger.begin_upload(FakeFS(), testfn, baseline_occ,
                                  baseline_size)
        return QuotaUploadFile(fd, ctx, start_pos), ctx

    def test_overwritten_range_not_billed_again(self):
        ledger = QuotaLedger(1000, LogicalOccupancyRules())
        testfn = self.get_testfn()
        with open(testfn, "wb") as f:
            f.write(b"x" * 100)
        # resume at 50: the first 50 written bytes overwrite the old
        # range and must not be billed again
        wrapper, ctx = self.wrap(
            ledger, testfn, "r+b", start_pos=50, baseline_size=100,
            baseline_occ=100,
        )
        wrapper.write(b"y" * 50)
        assert ledger.stats().unknown == 0
        wrapper.write(b"z" * 30)  # 30 bytes of actual growth
        assert ledger.stats().unknown == 30
        wrapper.close()
        assert ledger.stats().confirmed == 30

    def test_write_beyond_quota_raises_enospc(self):
        ledger = QuotaLedger(10, LogicalOccupancyRules())
        testfn = self.get_testfn()
        wrapper, ctx = self.wrap(ledger, testfn, "w+b")
        with pytest.raises(OSError) as excinfo:
            wrapper.write(b"x" * 11)
        assert excinfo.value.errno == errno.ENOSPC
        wrapper.close()

    def test_close_settles_against_backend(self):
        testfn = self.get_testfn()
        fs = AbstractedFS(os.path.dirname(testfn) or ".", None)
        ledger = QuotaLedger(1000, LogicalOccupancyRules())
        fd = open(testfn, "w+b")
        ctx = ledger.begin_upload(fs, testfn, 0, 0)
        wrapper = QuotaUploadFile(fd, ctx, 0)
        wrapper.write(b"x" * 100)
        wrapper.close()
        stats = ledger.stats()
        assert stats.confirmed == 100
        assert stats.unknown == 0

    def test_close_is_idempotent(self):
        ledger = QuotaLedger(1000, LogicalOccupancyRules())
        testfn = self.get_testfn()
        wrapper, ctx = self.wrap(ledger, testfn, "w+b")
        wrapper.write(b"x" * 100)
        wrapper.close()
        wrapper.close()
        assert ledger.stats().confirmed == 100


# ===================================================================
# --- functional tests
# ===================================================================


class TestFtpQuota(PyftpdlibTestCase):
    """Test quota accounting over STOR / APPE / REST / DELE / ALLO."""

    server_class = FtpdThreadWrapper
    client_class = ftplib.FTP
    quota_limit = 10000

    def setUp(self):
        super().setUp()
        self.manager = QuotaManager(rules=LogicalOccupancyRules())
        self.ledger = self.manager.set_quota(HOME, self.quota_limit)
        self.server = self.server_class()
        self.server.handler.quota_manager = self.manager
        self.server.start()
        self.client = self.client_class(timeout=GLOBAL_TIMEOUT)
        self.client.connect(self.server.host, self.server.port)
        self.client.login(USER, PASSWD)
        self.client.voidcmd("TYPE I")

    def tearDown(self):
        close_client(self.client)
        self.server.stop()
        super().tearDown()

    def store(self, testfn, data, rest=None):
        if rest is not None:
            self.client.sendcmd(f"REST {rest}")
        self.client.storbinary(f"STOR {testfn}", io.BytesIO(data))

    def stats(self):
        return self.ledger.stats()

    def connect_other(self):
        other = self.client_class(timeout=GLOBAL_TIMEOUT)
        other.connect(self.server.host, self.server.port)
        other.login(USER, PASSWD)
        self.addCleanup(close_client, other)
        return other

    # ---

    def test_stor_within_quota(self):
        testfn = self.get_testfn()
        self.store(testfn, b"x" * 100)
        stats = self.stats()
        assert stats.confirmed == 100
        assert stats.unknown == 0
        assert stats.available == self.quota_limit - 100

    def test_stor_exceeding_quota_leaves_bytes_billed(self):
        # The transfer is aborted mid-way by the ledger; the bytes
        # actually left on disk remain billed.
        self.ledger.limit = 5000
        testfn = self.get_testfn()
        with pytest.raises(ftplib.error_temp, match="426"):
            self.store(testfn, b"x" * 20000)
        left = os.path.getsize(testfn)
        assert 0 < left <= 5000
        stats = self.stats()
        assert stats.confirmed == left
        assert stats.unknown == 0

    def test_overwrite_not_billed_twice(self):
        testfn = self.get_testfn()
        self.store(testfn, b"x" * 100)
        assert self.stats().confirmed == 100
        # overwriting the same range must not bill the wire bytes again
        self.store(testfn, b"y" * 100)
        assert self.stats().confirmed == 100
        # growing the file bills only the extra occupancy
        self.store(testfn, b"z" * 150)
        assert self.stats().confirmed == 150

    def test_appe_bills_growth_only(self):
        testfn = self.get_testfn()
        self.store(testfn, b"x" * 100)
        self.client.storbinary(f"APPE {testfn}", io.BytesIO(b"y" * 50))
        assert self.stats().confirmed == 150

    def test_rest_overwrite_not_billed(self):
        testfn = self.get_testfn()
        self.store(testfn, b"x" * 100)
        assert self.stats().confirmed == 100
        # resume at 50 and rewrite 50 bytes: no growth
        self.store(testfn, b"y" * 50, rest=50)
        assert os.path.getsize(testfn) == 100
        assert self.stats().confirmed == 100
        # resume at 50 and write 80 bytes: 30 bytes of growth
        self.store(testfn, b"z" * 80, rest=50)
        assert os.path.getsize(testfn) == 130
        assert self.stats().confirmed == 130

    def test_stou_is_accounted(self):
        before = set(os.listdir(HOME))
        self.client.storbinary("STOU", io.BytesIO(b"x" * 50))
        new = [x for x in set(os.listdir(HOME)) - before
               if x.startswith("ftpd.")]
        assert len(new) == 1
        self.addCleanup(safe_rmpath, os.path.join(HOME, new[0]))
        assert self.stats().confirmed == 50

    def test_dele_reclaims(self):
        testfn = self.get_testfn()
        self.store(testfn, b"x" * 100)
        assert self.stats().confirmed == 100
        self.client.delete(testfn)
        assert self.stats().confirmed == 0

    def test_failed_dele_does_not_reclaim(self):
        testfn = self.get_testfn()
        self.store(testfn, b"x" * 100)
        with pytest.raises(ftplib.error_perm):
            self.client.delete("does-not-exist")
        assert self.stats().confirmed == 100

    def test_failed_upload_does_not_revoke_committed_one(self):
        self.ledger.limit = 150
        testfn1 = self.get_testfn()
        testfn2 = self.get_testfn()
        self.store(testfn1, b"x" * 100)
        assert self.stats().confirmed == 100
        # second upload fails midway; the first one stays confirmed
        with pytest.raises(ftplib.error_temp, match="426"):
            self.store(testfn2, b"y" * 200)
        stats = self.stats()
        assert stats.confirmed == 100 + os.path.getsize(testfn2)
        # the committed file is still fully retrievable
        data = []
        self.client.retrbinary(f"RETR {testfn1}", data.append)
        assert b"".join(data) == b"x" * 100

    def test_allo_reservation_is_exclusive(self):
        self.ledger.limit = 100
        other = self.connect_other()
        resp = self.client.sendcmd("ALLO 80")
        assert resp.startswith("200")
        assert self.stats().reserved == 80
        # a second session cannot spend the same remaining quota
        with pytest.raises(ftplib.error_perm, match="552"):
            other.sendcmd("ALLO 50")
        # the reservation is spent by the next upload
        testfn = self.get_testfn()
        self.store(testfn, b"x" * 80)
        stats = self.stats()
        assert stats.confirmed == 80
        assert stats.reserved == 0

    def test_allo_exceeding_quota(self):
        self.ledger.limit = 100
        with pytest.raises(ftplib.error_perm, match="552"):
            self.client.sendcmd("ALLO 101")
        assert self.stats().reserved == 0

    def test_allo_released_on_disconnect(self):
        self.ledger.limit = 100
        other = self.connect_other()
        other.sendcmd("ALLO 80")
        assert self.stats().reserved == 80
        close_client(other)
        # the reservation is released when the session closes
        for _ in range(100):
            if self.stats().reserved == 0:
                break
            time.sleep(0.01)
        assert self.stats().reserved == 0
        resp = self.client.sendcmd("ALLO 80")
        assert resp.startswith("200")

    def test_allo_noop_without_quota(self):
        self.server.handler.quota_manager = None
        try:
            resp = self.client.sendcmd("ALLO 100")
            assert resp.startswith("202")
        finally:
            self.server.handler.quota_manager = self.manager

    def test_site_quota(self):
        testfn = self.get_testfn()
        self.store(testfn, b"x" * 100)
        resp = self.client.sendcmd("SITE QUOTA")
        assert "Confirmed: 100" in resp
        assert "Reserved: 0" in resp
        assert "Unknown: 0" in resp
        assert f"Limit: {self.quota_limit}" in resp
        assert f"Available: {self.quota_limit - 100}" in resp

    def test_site_quota_disabled(self):
        self.server.handler.quota_manager = None
        try:
            with pytest.raises(ftplib.error_perm, match="550"):
                self.client.sendcmd("SITE QUOTA")
        finally:
            self.server.handler.quota_manager = self.manager
