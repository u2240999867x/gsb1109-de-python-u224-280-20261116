"""Tests for ownership and coordinated shutdown of wrapper graphs."""

from __future__ import unicode_literals

import threading
import time
import unittest

from fs import errors
from fs.base import FS
from fs.memoryfs import MemoryFS
from fs.mountfs import MountFS
from fs.multifs import MultiFS
from fs.ownership import (
    CloseCascade,
    ClosePolicy,
    CloseStatus,
    LifecycleState,
    Ownership,
)
from fs.subfs import ClosingSubFS, SubFS
from fs.wrapfs import WrapFS

try:
    from unittest import mock
except ImportError:  # pragma: no cover
    import mock


class OpaqueWrapper(WrapFS):
    """A wrapper which overrides close without declaring its cascade."""

    def close(self, policy=None, timeout=None):
        self._closed = True


class TestOwnershipValues(unittest.TestCase):
    def test_parse_ownership(self):
        from fs.ownership import parse_ownership

        self.assertIs(parse_ownership(True), Ownership.OWNED)
        self.assertIs(parse_ownership(False), Ownership.BORROWED)
        self.assertIs(parse_ownership("owned"), Ownership.OWNED)
        self.assertIs(parse_ownership("borrow"), Ownership.BORROWED)
        with self.assertRaises(ValueError):
            parse_ownership("maybe")

    def test_parse_policy(self):
        from fs.ownership import parse_policy

        self.assertIs(parse_policy("drain"), ClosePolicy.DRAIN)
        self.assertIs(parse_policy("abort"), ClosePolicy.ABORT)
        self.assertIs(parse_policy("defer"), ClosePolicy.DEFER)
        with self.assertRaises(ValueError):
            parse_policy("eventually")


class TestHoldLedger(unittest.TestCase):
    def test_basic_ledger(self):
        child = MemoryFS()
        holder = MountFS(auto_close=False)
        hold = holder.own(child, label="x")
        self.assertIs(hold.ownership, Ownership.OWNED)
        self.assertEqual(child.holds(), [hold])
        self.assertIn(hold, holder.holding())
        self.assertEqual(child.holders(), [holder])
        self.assertEqual(child.held_by(holder), [hold])
        self.assertTrue(child.explain_open()["open"])
        self.assertIn("MountFS", child.explain_open()["reason"])

        holder.close()
        self.assertTrue(child.isclosed())
        self.assertTrue(hold.released)
        self.assertEqual(child.holds(active_only=True), [])
        self.assertTrue(holder.isclosed())

    def test_direct_close_of_owned_child_is_deferred(self):
        child = MemoryFS()
        holder = MountFS()
        holder.mount("/", child, ownership="owned")
        report = child.close()
        self.assertIs(report.status, CloseStatus.DEFERRED)
        self.assertFalse(child.isclosed())
        self.assertEqual(child.lifecycle_state, LifecycleState.OPEN)
        # The rejected close did not enter shutdown, so the owning task
        # can keep using the backend.
        child.writebytes("still", b"usable")
        self.assertEqual(child.readbytes("still"), b"usable")

        holder.close()
        self.assertTrue(child.isclosed())

    def test_repeated_close_is_stable(self):
        child = MemoryFS()
        holder = MountFS()
        holder.mount("/", child)
        first = holder.close()
        second = holder.close()
        self.assertIs(first, second)
        self.assertTrue(first.complete)
        self.assertTrue(child.isclosed())
        # A repeated child close does not release anything twice.
        report = child.close()
        self.assertTrue(report.complete)


class TestMountSharing(unittest.TestCase):
    def test_shared_owned_backend_survives_one_close(self):
        backend = MemoryFS()
        backend.writebytes("data", b"payload")
        first = MountFS()
        second = MountFS()
        first.mount("/data", backend, ownership="owned")
        second.mount("/data", backend, ownership="owned")

        report = first.close()
        self.assertTrue(report.complete)
        self.assertFalse(backend.isclosed())
        self.assertEqual(backend.readbytes("data"), b"payload")
        self.assertEqual(len(backend.holds()), 1)

        report = second.close()
        self.assertTrue(report.complete)
        self.assertTrue(backend.isclosed())

    def test_borrowed_backend_is_never_closed(self):
        backend = MemoryFS()
        mount = MountFS(ownership="borrowed")
        mount.mount("/data", backend)
        report = mount.close()
        self.assertTrue(report.complete)
        self.assertFalse(backend.isclosed())
        self.assertEqual(backend.holds(), [])

    def test_mixed_owned_and_borrowed_edges(self):
        backend = MemoryFS()
        owner = MountFS()
        borrower = MountFS(auto_close=False)
        owner.mount("/data", backend, ownership="owned")
        borrower.mount("/data", backend, ownership="borrowed")

        borrower.close()
        self.assertFalse(backend.isclosed())
        owner.close()
        self.assertTrue(backend.isclosed())

    def test_auto_close_does_not_cascade_through_borrowing_mount(self):
        backend = MemoryFS()
        owner = MountFS()
        borrower = MountFS()
        owner.mount("/data", backend, ownership="owned")
        borrower.mount("/data", backend, ownership="borrowed")

        borrower.close()
        owner.close()
        self.assertTrue(backend.isclosed())
        self.assertEqual(backend.holds(active_only=True), [])

    def test_owner_close_is_completed_by_last_borrower(self):
        backend = MemoryFS()
        owner = MountFS()
        borrower = MountFS(auto_close=False)
        owner.mount("/data", backend, ownership="owned")
        borrower.mount("/data", backend, ownership="borrowed")

        owner.close()
        self.assertFalse(backend.isclosed())
        self.assertEqual(backend.holds()[0].ownership, Ownership.BORROWED)

        borrower.close()
        # The owner's deferred close is finished by the last release,
        # rather than leaking a backend nobody owns any more.
        self.assertTrue(backend.isclosed())

    def test_explicit_ownership_argument_wins(self):
        backend = MemoryFS()
        mount = MountFS(auto_close=True)
        mount.mount("/data", backend, ownership="borrowed")
        mount.close()
        self.assertFalse(backend.isclosed())
        self.assertEqual(backend.holds(), [])

    def test_hold_report_is_serialisable(self):
        backend = MemoryFS()
        mount = MountFS()
        mount.mount("/data", backend, ownership="owned")
        report = backend.holds_report()
        self.assertEqual(report["state"], "open")
        self.assertEqual(report["holds"][0]["ownership"], "owned")
        self.assertEqual(report["holds"][0]["label"], "/data/")
        self.assertIsInstance(report["holds"][0]["active"], bool)


class TestMultiSharing(unittest.TestCase):
    def test_shared_backend_survives_one_multifs_close(self):
        backend = MemoryFS()
        backend.writebytes("a", b"1")
        first = MultiFS()
        second = MultiFS()
        first.add_fs("a", backend, ownership="owned")
        second.add_fs("a", backend, ownership="owned")

        first.close()
        self.assertFalse(backend.isclosed())
        self.assertEqual(backend.readbytes("a"), b"1")

        second.close()
        self.assertTrue(backend.isclosed())

    def test_borrowed_child_stays_open(self):
        backend = MemoryFS()
        multi = MultiFS(auto_close=True)
        multi.add_fs("b", backend, auto_close=False)
        multi.close()
        self.assertFalse(backend.isclosed())

    def test_non_shared_owned_child_closes(self):
        backend = MemoryFS()
        multi = MultiFS()
        multi.add_fs("b", backend)
        multi.close()
        self.assertTrue(backend.isclosed())

    def test_listdir_after_owned_shared_close(self):
        backend = MemoryFS()
        backend.touch("one")
        first = MultiFS()
        second = MultiFS()
        first.add_fs("a", backend)
        second.add_fs("a", backend)
        first.close()
        self.assertEqual(second.listdir("/"), ["one"])


class TestSubFSSharing(unittest.TestCase):
    def test_two_subdirectories_share_parent(self):
        parent = MemoryFS()
        parent.makedir("one")
        parent.makedir("two")
        one = parent.opendir("one")
        two = parent.opendir("two")
        self.assertIsInstance(one, SubFS)
        self.assertIs(one.parent_fs, parent)
        one.close()
        two.close()
        self.assertFalse(parent.isclosed())
        self.assertEqual(parent.listdir("/"), ["one", "two"])

    def test_subdir_borrows_by_default(self):
        parent = MemoryFS()
        parent.makedir("dir")
        sub = parent.opendir("dir")
        self.assertIs(parent.holds()[0].ownership, Ownership.BORROWED)
        sub.close()
        self.assertEqual(parent.holds(active_only=True), [])
        self.assertFalse(parent.isclosed())

    def test_closing_subfs_owns_parent(self):
        parent = MemoryFS()
        parent.makedir("dir")
        sub = parent.opendir("dir", factory=ClosingSubFS)
        self.assertIs(parent.holds()[0].ownership, Ownership.OWNED)
        sub.close()
        self.assertTrue(parent.isclosed())

    def test_closing_subfs_does_not_close_shared_parent(self):
        parent = MemoryFS()
        parent.makedir("dir")
        other = parent.opendir("dir")
        closing = parent.opendir("dir", factory=ClosingSubFS)
        closing.close()
        self.assertFalse(parent.isclosed())
        self.assertTrue(other.listdir("/") == [])
        other.close()


class TestWrapFSParticipation(unittest.TestCase):
    def test_wrapfs_borrows_child(self):
        child = MemoryFS()
        wrapper = WrapFS(child)
        self.assertIs(wrapper.cascade, CloseCascade.SELF)
        self.assertIs(child.holds()[0].ownership, Ownership.BORROWED)
        wrapper.close()
        self.assertFalse(child.isclosed())
        self.assertEqual(child.holds(active_only=True), [])

    def test_opaque_wrapper_sharing_is_refused(self):
        child = MemoryFS()
        opaque = OpaqueWrapper(child)
        self.assertIs(opaque.cascade, CloseCascade.UNKNOWN)
        with self.assertRaises(errors.UnsafeSharing):
            opaque.own(child, label="second", share=True)

    def test_opaque_second_owner_is_refused(self):
        backend = MemoryFS()
        mount = MountFS()
        mount.mount("/data", backend, ownership="owned")
        second = OpaqueWrapper(MemoryFS())
        with self.assertRaises(errors.UnsafeSharing):
            second.own(backend, label="second", share=True)

    def test_declared_wrapper_may_be_shared(self):
        child = MemoryFS()
        first = MountFS()
        first.mount("/x", child)
        second = MountFS()
        # MountFS declares its cascade, so a second owner may be recorded.
        second.mount("/x", child)
        self.assertEqual(len(child.holds()), 2)

    def test_unknown_cascade_is_reported(self):
        child = MemoryFS()
        opaque = OpaqueWrapper(child)
        self.assertFalse(opaque.close_cascade_declared)
        self.assertIs(opaque.cascade, CloseCascade.UNKNOWN)


class TestNestedShutdown(unittest.TestCase):
    def test_nested_wrapper_graph_releases_everything(self):
        backend = MemoryFS()
        backend.writebytes("data", b"hi")
        inner = MountFS()
        inner.mount("/data", backend, ownership="owned")
        outer = MultiFS()
        outer.add_fs("inner", inner, ownership="owned")

        report = outer.close()
        self.assertTrue(report.complete)
        self.assertTrue(inner.isclosed())
        self.assertTrue(backend.isclosed())

    def test_shared_child_close_is_deferred_then_complete(self):
        backend = MemoryFS()
        first = MountFS()
        second = MountFS()
        first.mount("/x", backend)
        second.mount("/x", backend)

        self.assertTrue(first.close().complete)
        # The remaining owner keeps the backend open with a real basis.
        self.assertFalse(backend.isclosed())
        self.assertEqual(len(backend.holds(active_only=True)), 1)
        self.assertIn("MountFS", backend.explain_open()["reason"])

        self.assertTrue(second.close().complete)
        self.assertTrue(backend.isclosed())
        self.assertEqual(backend.holds(active_only=True), [])

    def test_child_failure_marks_parent_failed(self):
        class FailingFS(MemoryFS):
            def _finalize_close(self, report):
                raise RuntimeError("boom")

        backend = FailingFS()
        mount = MountFS()
        mount.mount("/x", backend, ownership="owned")
        report = mount.close()
        self.assertIs(report.status, CloseStatus.FAILED)
        self.assertTrue(report.errors)
        self.assertTrue(backend.isclosed())

    def test_borrow_after_owner_close_still_closes(self):
        backend = MemoryFS()
        owner = MountFS()
        borrower = MountFS(auto_close=False)
        owner.mount("/x", backend, ownership="owned")
        borrower.mount("/x", backend, ownership="borrowed")

        owner.close()
        self.assertFalse(backend.isclosed())
        borrower.close()
        self.assertTrue(backend.isclosed())


class TestShutdownPolicy(unittest.TestCase):
    def test_drain_waits_for_open_handle(self):
        fs = MemoryFS()
        fs.writebytes("file", b"hello")
        handle = fs.openbin("file")
        result = {}
        thread = threading.Thread(
            target=lambda: result.update(report=fs.close(policy="drain", timeout=2))
        )
        thread.start()
        time.sleep(0.1)
        self.assertFalse(fs.isclosed())
        self.assertTrue(thread.is_alive())
        handle.close()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(result["report"].complete)
        self.assertTrue(fs.isclosed())

    def test_drain_timeout_is_explicit(self):
        fs = MemoryFS()
        fs.writebytes("file", b"hello")
        handle = fs.openbin("file")
        report = fs.close(policy="drain", timeout=0.01)
        self.assertIs(report.status, CloseStatus.DEFERRED)
        self.assertFalse(fs.isclosed())
        handle.close()
        final = fs.close(policy="drain")
        self.assertTrue(final.complete)
        self.assertTrue(fs.isclosed())

    def test_abort_closes_open_handle(self):
        fs = MemoryFS()
        fs.writebytes("file", b"hello")
        handle = fs.openbin("file")
        report = fs.close(policy="abort")
        self.assertTrue(report.complete)
        self.assertTrue(fs.isclosed())
        self.assertTrue(handle.closed)
        with self.assertRaises(errors.FilesystemClosed):
            handle.read()

    def test_defer_returns_without_closing(self):
        fs = MemoryFS()
        fs.writebytes("file", b"hello")
        handle = fs.openbin("file")
        report = fs.close(policy="defer")
        self.assertIs(report.status, CloseStatus.DEFERRED)
        self.assertFalse(fs.isclosed())

    def test_shutdown_stops_new_operations(self):
        fs = MemoryFS()
        fs.writebytes("file", b"hello")
        handle = fs.openbin("file")
        result = {}
        thread = threading.Thread(
            target=lambda: result.update(report=fs.close(policy="drain", timeout=2))
        )
        thread.start()
        time.sleep(0.05)
        with self.assertRaises(errors.FilesystemClosed):
            fs.listdir("/")
        with self.assertRaises(errors.FilesystemClosed):
            fs.openbin("file")
        handle.close()
        thread.join(2)
        self.assertTrue(result["report"].complete)
        self.assertTrue(fs.isclosed())

    def test_admitted_operation_is_tracked(self):
        fs = MemoryFS()
        started = threading.Event()
        proceed = threading.Event()

        def read():
            with fs.begin_operation():
                started.set()
                proceed.wait(2)

        thread = threading.Thread(target=read)
        thread.start()
        self.assertTrue(started.wait(2))
        report = fs.close(policy="defer")
        self.assertIs(report.status, CloseStatus.DEFERRED)
        self.assertEqual(report.active_operations, 1)
        proceed.set()
        thread.join(2)
        self.assertTrue(fs.close(policy="defer").complete)

    def test_close_failure_is_reported_once(self):
        fs = MemoryFS()
        calls = []

        def fail(report):
            calls.append(1)
            raise RuntimeError("boom")

        fs._finalize_close = fail
        with self.assertRaises(RuntimeError):
            fs.close()
        self.assertEqual(len(calls), 1)
        self.assertIs(fs.close_result.status, CloseStatus.FAILED)
        again = fs.close()
        self.assertEqual(len(calls), 1)
        self.assertIs(again.status, CloseStatus.FAILED)


class TestCloseReport(unittest.TestCase):
    def test_report_fields(self):
        fs = MemoryFS()
        fs.close()
        report = fs.close_report
        self.assertTrue(report.complete)
        self.assertEqual(report.status, CloseStatus.CLOSED)
        self.assertEqual(report.reason, "all references released")
        data = report.as_dict()
        self.assertEqual(data["status"], "closed")
        self.assertEqual(data["state"], "closed")
        self.assertEqual(data["policy"], "abort")

    def test_deferred_report_explains_holds(self):
        backend = MemoryFS()
        mount = MountFS()
        mount.mount("/x", backend)
        report = backend.close()
        self.assertTrue(report.deferred)
        self.assertTrue(report.retained_holds)
        self.assertIn("MountFS", report.as_dict()["details"]["holders"][0])

    def test_explain_open_reports_handles(self):
        fs = MemoryFS()
        fs.writebytes("f", b"x")
        handle = fs.openbin("f")
        info = fs.explain_open()
        self.assertTrue(info["open"])
        self.assertIn("open file handle", info["reason"])
        handle.close()
        self.assertFalse(fs.explain_open()["open_handles"])


if __name__ == "__main__":
    unittest.main()
