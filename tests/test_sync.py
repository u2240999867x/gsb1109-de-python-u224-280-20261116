from __future__ import unicode_literals

import json
import os
import shutil
import tempfile
import unittest

from fs import open_fs
from fs.errors import OperationFailed
from fs.sync import sync
from fs.wrapfs import WrapFS


class CopyLimiter(WrapFS):
    """Wrap a destination FS, raising after a number of file writes."""

    def __init__(self, fs, limit=None):
        super(CopyLimiter, self).__init__(fs)
        self.limit = limit
        self.writes = 0

    def _count_write(self):
        self.writes += 1
        if self.limit is not None and self.writes > self.limit:
            raise OperationFailed("simulated interruption")

    def openbin(self, path, mode="r", buffering=-1, **options):
        if "w" in mode:
            self._count_write()
        return super(CopyLimiter, self).openbin(path, mode, buffering, **options)

    def upload(self, path, file, chunk_size=None, **options):
        self._count_write()
        return super(CopyLimiter, self).upload(
            path, file, chunk_size=chunk_size, **options
        )


class ScanCounter(WrapFS):
    """Wrap a source FS, counting directory scans."""

    def __init__(self, fs, fail=False):
        super(ScanCounter, self).__init__(fs)
        self.fail = fail
        self.scans = 0

    def scandir(self, path, namespaces=None, page=None):
        self.scans += 1
        if self.fail:
            raise OperationFailed("simulated scan failure")
        return super(ScanCounter, self).scandir(
            path, namespaces=namespaces, page=page
        )


class TestSync(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tempdir, True)
        self.state_path = os.path.join(self.tempdir, "sync-state.json")

    def _make_source(self):
        src = open_fs("mem://")
        src.writetext("foo.txt", "foo")
        src.writetext("bar.txt", "bar")
        src.makedir("baz")
        src.writetext("baz/egg.txt", "egg")
        src.makedir("baz/sub")
        src.writetext("baz/sub/spam.txt", "spam")
        return src

    def _contents(self, fs, with_times=False):
        """Extract an FS in to a simple data structure."""
        namespaces = ("details", "metadata_changed", "modified")
        contents = []
        for path, dirs, files in fs.walk():
            for info in dirs:
                contents.append((info.make_path(path), "dir", b""))
            for info in files:
                _path = info.make_path(path)
                entry = (_path, "file", fs.readbytes(_path))
                if with_times:
                    _info = fs.getinfo(_path, namespaces)
                    entry = entry + (_info.modified, _info.metadata_changed)
                contents.append(entry)
        return sorted(contents)

    def assert_compare_fs(self, fs1, fs2, with_times=False):
        """Assert filesystems and contents are the same."""
        self.assertEqual(
            self._contents(fs1, with_times), self._contents(fs2, with_times)
        )

    def test_sync_empty(self):
        src = open_fs("mem://")
        dst = open_fs("mem://")
        sync(src, dst, state_path=self.state_path)
        self.assert_compare_fs(src, dst)
        self.assertFalse(os.path.exists(self.state_path))

    def test_sync_no_state(self):
        src = self._make_source()
        dst = open_fs("mem://")
        sync(src, dst)
        self.assert_compare_fs(src, dst)

    def test_sync_delete_extras(self):
        src = self._make_source()
        dst = open_fs("mem://")
        dst.makedir("extra")
        dst.writetext("extra/old.txt", "old")
        dst.writetext("stale.txt", "stale")
        sync(src, dst, state_path=self.state_path)
        self.assert_compare_fs(src, dst)

    def test_sync_wrong_type(self):
        src = self._make_source()
        dst = open_fs("mem://")
        dst.makedir("foo.txt")
        dst.writetext("baz", "not a dir")
        sync(src, dst, state_path=self.state_path)
        self.assert_compare_fs(src, dst)

    def test_sync_preserve_time(self):
        src = self._make_source()
        dst = open_fs("mem://")
        sync(src, dst, state_path=self.state_path, preserve_time=True)
        self.assert_compare_fs(src, dst, with_times=True)

    def test_sync_workers(self):
        src = self._make_source()
        dst = open_fs("mem://")
        sync(src, dst, state_path=self.state_path, workers=2)
        self.assert_compare_fs(src, dst)
        self.assertFalse(os.path.exists(self.state_path))

    def test_state_removed_on_success(self):
        src = self._make_source()
        dst = open_fs("mem://")
        sync(src, dst, state_path=self.state_path)
        self.assertFalse(os.path.exists(self.state_path))

    def test_interrupted_scan_leaves_no_state(self):
        src = ScanCounter(self._make_source(), fail=True)
        dst = open_fs("mem://")
        with self.assertRaises(OperationFailed):
            sync(src, dst, state_path=self.state_path)
        # No complete scan, so no plan may be persisted.
        self.assertFalse(os.path.exists(self.state_path))

    def test_resume_after_interruption(self):
        src = self._make_source()
        dst_fs = open_fs("mem://")

        dst = CopyLimiter(dst_fs, limit=2)
        with self.assertRaises(OperationFailed):
            sync(src, dst, state_path=self.state_path)
        self.assertEqual(dst.writes, 3)
        self.assertTrue(os.path.exists(self.state_path))

        dst = CopyLimiter(dst_fs)
        sync(src, dst, state_path=self.state_path)
        # Only the remaining files are copied; verified material is reused.
        self.assertEqual(dst.writes, 2)
        self.assert_compare_fs(src, dst_fs)
        self.assertFalse(os.path.exists(self.state_path))

    def test_resume_binds_to_original_plan(self):
        src_fs = self._make_source()
        dst_fs = open_fs("mem://")

        dst = CopyLimiter(dst_fs, limit=1)
        with self.assertRaises(OperationFailed):
            sync(ScanCounter(src_fs), dst, state_path=self.state_path)

        # The source changes after the interruption.
        src_fs.writetext("new.txt", "new")

        src = ScanCounter(src_fs)
        dst = CopyLimiter(dst_fs)
        sync(src, dst, state_path=self.state_path)
        # Resume binds to the stored plan; the source is not re-scanned...
        self.assertEqual(src.scans, 0)
        # ...so the new file is not picked up, but the plan completes.
        self.assertFalse(dst_fs.exists("new.txt"))
        self.assertFalse(os.path.exists(self.state_path))

        # An explicit rebuild picks up the new file.
        src = ScanCounter(src_fs)
        sync(src, dst_fs, state_path=self.state_path, rebuild=True)
        self.assertGreater(src.scans, 0)
        self.assert_compare_fs(src_fs, dst_fs)

    def test_resume_rebuild_after_source_changes(self):
        src_fs = self._make_source()
        dst_fs = open_fs("mem://")

        dst = CopyLimiter(dst_fs, limit=2)
        with self.assertRaises(OperationFailed):
            sync(src_fs, dst, state_path=self.state_path)

        # The source changes: a new file, and a rename.
        src_fs.writetext("new.txt", "new")
        src_fs.move("bar.txt", "renamed.txt")

        dst = CopyLimiter(dst_fs)
        sync(src_fs, dst, state_path=self.state_path, rebuild=True)
        self.assert_compare_fs(src_fs, dst_fs)
        # The new source has 5 files; the two files still missing from
        # the destination plus the added and renamed files are copied,
        # while the file with a valid receipt is reused.
        self.assertEqual(dst.writes, 4)
        self.assertFalse(os.path.exists(self.state_path))

    def test_resume_revalidates_receipts(self):
        src_fs = self._make_source()
        dst_fs = open_fs("mem://")

        dst = CopyLimiter(dst_fs, limit=2)
        with self.assertRaises(OperationFailed):
            sync(src_fs, dst, state_path=self.state_path)

        # The destination is modified externally after the interruption.
        dst_fs.appendtext("bar.txt", "tampered")

        dst = CopyLimiter(dst_fs)
        sync(src_fs, dst, state_path=self.state_path)
        # The stale receipt is not trusted; the tampered file is copied
        # again, along with the remaining files.
        self.assertEqual(dst.writes, 3)
        self.assert_compare_fs(src_fs, dst_fs)

    def test_resume_source_file_disappeared(self):
        src_fs = self._make_source()
        dst_fs = open_fs("mem://")

        dst = CopyLimiter(dst_fs, limit=0)
        with self.assertRaises(OperationFailed):
            sync(src_fs, dst, state_path=self.state_path)

        # A planned file is renamed away before the resume.
        src_fs.move("foo.txt", "renamed.txt")
        with self.assertRaises(OperationFailed) as cm:
            sync(src_fs, dst_fs, state_path=self.state_path)
        self.assertIn("rebuild", str(cm.exception))

        # An explicit rebuild plans from the current source.
        sync(src_fs, dst_fs, state_path=self.state_path, rebuild=True)
        self.assert_compare_fs(src_fs, dst_fs)

    def test_corrupt_state(self):
        src = self._make_source()
        dst = open_fs("mem://")
        with open(self.state_path, "w") as state_file:
            state_file.write("not json")
        with self.assertRaises(OperationFailed):
            sync(src, dst, state_path=self.state_path)
        # An explicit rebuild ignores the corrupt checkpoint.
        sync(src, dst, state_path=self.state_path, rebuild=True)
        self.assert_compare_fs(src, dst)

    def test_incompatible_state_version(self):
        src = self._make_source()
        dst = open_fs("mem://")
        with open(self.state_path, "w") as state_file:
            json.dump({"version": -1}, state_file)
        with self.assertRaises(OperationFailed):
            sync(src, dst, state_path=self.state_path)

    def test_checkpoint_roundtrip(self):
        from fs.sync import SyncCheckpoint, SyncPlan

        src = self._make_source()
        plan = SyncPlan.scan(src)
        checkpoint = SyncCheckpoint(plan, {"/foo.txt": {"src": {}, "dst": {}}})
        checkpoint.save(self.state_path)
        loaded = SyncCheckpoint.load(self.state_path)
        self.assertEqual(loaded.plan.directories, plan.directories)
        self.assertEqual(loaded.plan.files, plan.files)
        self.assertEqual(loaded.receipts, checkpoint.receipts)
