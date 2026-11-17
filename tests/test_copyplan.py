from __future__ import unicode_literals

import unittest

from fs import errors, wrap
from fs.copyplan import (
    CopyMethod,
    CopyPlan,
    PreservationError,
    SourceChanged,
    TargetStatus,
    execute_plan,
    plan_copy,
)
from fs.memoryfs import MemoryFS
from fs.opener import open_fs
from fs.tempfs import TempFS
from fs.wrapfs import WrapFS


class MutatingFS(WrapFS):
    """A filesystem wrapper that mutates a path during the first read."""

    def __init__(self, wrap_fs, mutate):
        super(MutatingFS, self).__init__(wrap_fs)
        self._mutate = mutate
        self._mutated = False

    def openbin(self, path, mode="r", buffering=-1, **options):
        bin_file = super(MutatingFS, self).openbin(path, mode, buffering, **options)
        if "r" in mode and not self._mutated:
            self._mutated = True
            self._mutate()
        return bin_file


class TestPlanCopy(unittest.TestCase):
    def setUp(self):
        self.src_fs = MemoryFS()
        self.src_fs.writetext("data.txt", "hello world")
        self.src_fs.setinfo("data.txt", {"details": {"modified": 1000000000.0}})

    def tearDown(self):
        self.src_fs.close()

    def test_method_selection(self):
        dst_mem = MemoryFS()
        dst_temp = TempFS()
        try:
            plan = plan_copy(
                self.src_fs,
                "/data.txt",
                [(dst_mem, "/a.txt"), (dst_temp, "/b.txt"), (self.src_fs, "/c.txt")],
                preserve=["content"],
            )
            self.assertEqual(plan.targets[0].method, CopyMethod.STREAM)
            self.assertEqual(plan.targets[1].method, CopyMethod.DIRECT)
            self.assertEqual(plan.targets[2].method, CopyMethod.DIRECT)
            plan.close()
        finally:
            dst_mem.close()
            dst_temp.close()

    def test_read_only_destination_unsupported(self):
        read_only_fs = wrap.read_only(MemoryFS())
        plan = plan_copy(
            self.src_fs, "/data.txt", [(read_only_fs, "/a.txt")], preserve=["content"]
        )
        self.assertEqual(plan.targets[0].method, CopyMethod.UNSUPPORTED)
        self.assertFalse(plan.targets[0].feasible)
        plan.close()

    def test_same_file_unsupported(self):
        plan = plan_copy(
            self.src_fs, "/data.txt", [(self.src_fs, "/data.txt")], preserve=["content"]
        )
        self.assertEqual(plan.targets[0].method, CopyMethod.UNSUPPORTED)
        plan.close()

    def test_unpreservable_declared_upfront(self):
        dst_fs = MemoryFS()
        try:
            plan = plan_copy(self.src_fs, "/data.txt", [(dst_fs, "/a.txt")])
            target = plan.targets[0]
            # MemoryFS can preserve times, but not permissions
            self.assertIn("permissions", target.unpreservable)
            self.assertNotIn("times", target.unpreservable)
            self.assertNotIn("content", target.unpreservable)
            self.assertFalse(target.feasible)
            self.assertEqual(plan.unpreservable, frozenset(["permissions"]))
            plan.close()
        finally:
            dst_fs.close()

    def test_unknown_preserve_attribute(self):
        with self.assertRaises(ValueError):
            plan_copy(
                self.src_fs, "/data.txt", [(MemoryFS(), "/a.txt")], preserve=["owner"]
            )

    def test_source_not_a_file(self):
        self.src_fs.makedir("dir")
        with self.assertRaises(errors.FileExpected):
            plan_copy(self.src_fs, "/dir", [(MemoryFS(), "/a.txt")])
        with self.assertRaises(errors.ResourceNotFound):
            plan_copy(self.src_fs, "/missing.txt", [(MemoryFS(), "/a.txt")])


class TestExecutePlan(unittest.TestCase):
    def setUp(self):
        self.src_fs = MemoryFS()
        self.src_fs.writetext("data.txt", "hello world")
        self.src_fs.setinfo("data.txt", {"details": {"modified": 1000000000.0}})

    def tearDown(self):
        self.src_fs.close()

    def test_execute_verified(self):
        dst_fs = MemoryFS()
        try:
            plan = plan_copy(
                self.src_fs,
                "/data.txt",
                [(dst_fs, "/copy.txt")],
                preserve=["content", "times"],
            )
            report = plan.execute()
            self.assertTrue(report.success)
            self.assertEqual(len(report.verified), 1)
            self.assertEqual(dst_fs.readtext("/copy.txt"), "hello world")
            dst_modified = dst_fs.getinfo("/copy.txt", ["details"]).get(
                "details", "modified"
            )
            self.assertEqual(dst_modified, 1000000000.0)
            result = report.results[0]
            self.assertEqual(result.status, TargetStatus.VERIFIED)
            self.assertEqual(result.bytes_copied, len(b"hello world"))
            self.assertEqual(result.preserved, frozenset(["content", "times"]))
        finally:
            dst_fs.close()

    def test_execute_with_hash_verification(self):
        dst_fs = MemoryFS()
        try:
            plan = plan_copy(
                self.src_fs,
                "/data.txt",
                [(dst_fs, "/copy.txt")],
                preserve=["content"],
                verify_hash=True,
            )
            report = plan.execute()
            self.assertTrue(report.success)
            self.assertEqual(dst_fs.readtext("/copy.txt"), "hello world")
        finally:
            dst_fs.close()

    def test_infeasible_target_is_skipped(self):
        dst_fs = MemoryFS()
        try:
            plan = plan_copy(self.src_fs, "/data.txt", [(dst_fs, "/copy.txt")])
            # permissions can not be preserved by MemoryFS
            self.assertFalse(plan.feasible)
            report = plan.execute()
            self.assertFalse(report.success)
            self.assertEqual(report.results[0].status, TargetStatus.SKIPPED)
            # the copy was not attempted and falsely reported as a success
            self.assertFalse(dst_fs.exists("/copy.txt"))
        finally:
            dst_fs.close()

    def test_source_changed_before_execute(self):
        dst_fs = MemoryFS()
        try:
            plan = plan_copy(
                self.src_fs,
                "/data.txt",
                [(dst_fs, "/copy.txt")],
                preserve=["content", "times"],
            )
            self.src_fs.writetext("data.txt", "changed!")
            report = execute_plan(plan)
            self.assertEqual(report.results[0].status, TargetStatus.FAILED)
            self.assertIsInstance(report.results[0].error, SourceChanged)
            self.assertFalse(dst_fs.exists("/copy.txt"))
            plan.close()
        finally:
            dst_fs.close()

    def test_source_changed_during_copy(self):
        dst_fs = MemoryFS()
        try:
            mutating_fs = MutatingFS(
                self.src_fs,
                mutate=lambda: self.src_fs.setinfo(
                    "data.txt", {"details": {"modified": 2000000000.0}}
                ),
            )
            plan = plan_copy(
                mutating_fs,
                "/data.txt",
                [(dst_fs, "/copy.txt")],
                preserve=["content", "times"],
            )
            report = plan.execute()
            self.assertEqual(report.results[0].status, TargetStatus.FAILED)
            self.assertIsInstance(report.results[0].error, SourceChanged)
            # the content was copied, but the stale metadata from the
            # plan must not have been published on the new content
            dst_modified = dst_fs.getinfo("/copy.txt", ["details"]).get(
                "details", "modified"
            )
            self.assertNotEqual(dst_modified, 1000000000.0)
        finally:
            dst_fs.close()

    def test_multiple_targets_independent(self):
        good_fs = MemoryFS()
        broken_fs = MemoryFS()
        read_only_fs = wrap.read_only(MemoryFS())
        try:
            plan = plan_copy(
                self.src_fs,
                "/data.txt",
                [
                    (good_fs, "/copy.txt"),
                    (broken_fs, "/missing/copy.txt"),
                    (read_only_fs, "/copy.txt"),
                ],
                preserve=["content", "times"],
            )
            report = plan.execute()
            statuses = [result.status for result in report.results]
            self.assertEqual(
                statuses,
                [TargetStatus.VERIFIED, TargetStatus.FAILED, TargetStatus.SKIPPED],
            )
            # the verified copy is not rolled back by the other failures
            self.assertEqual(good_fs.readtext("/copy.txt"), "hello world")
            dst_modified = good_fs.getinfo("/copy.txt", ["details"]).get(
                "details", "modified"
            )
            self.assertEqual(dst_modified, 1000000000.0)
            self.assertFalse(report.success)
            self.assertEqual(len(report.verified), 1)
            self.assertEqual(len(report.failed), 1)
            self.assertEqual(len(report.skipped), 1)
        finally:
            good_fs.close()
            broken_fs.close()

    def test_plan_from_urls(self):
        import shutil
        import tempfile

        tmp_dir = tempfile.mkdtemp()
        try:
            with open_fs("osfs://" + tmp_dir) as fs:
                fs.writetext("data.txt", "hello world")
            with plan_copy(
                "osfs://" + tmp_dir,
                "/data.txt",
                [("osfs://" + tmp_dir, "/copy.txt")],
                preserve=["content", "times"],
            ) as plan:
                self.assertIsInstance(plan, CopyPlan)
                self.assertEqual(plan.targets[0].method, CopyMethod.DIRECT)
                report = plan.execute()
                self.assertTrue(report.success)
            with open_fs("osfs://" + tmp_dir) as fs:
                self.assertEqual(fs.readtext("/copy.txt"), "hello world")
        finally:
            shutil.rmtree(tmp_dir)

    def test_permissions_lie_detected(self):
        # a filesystem that claims to support permissions but does not
        # honor them must not be reported as a success
        src_fs = TempFS()
        src_fs.writetext("data.txt", "hello world")
        dst_fs = MemoryFS()
        dst_fs._meta["supports_permissions"] = True
        try:
            plan = plan_copy(
                src_fs,
                "/data.txt",
                [(dst_fs, "/copy.txt")],
                preserve=["content", "permissions"],
            )
            self.assertTrue(plan.targets[0].feasible)
            report = plan.execute()
            self.assertEqual(report.results[0].status, TargetStatus.FAILED)
            self.assertIsInstance(report.results[0].error, PreservationError)
        finally:
            src_fs.close()
            dst_fs.close()


if __name__ == "__main__":
    unittest.main()
