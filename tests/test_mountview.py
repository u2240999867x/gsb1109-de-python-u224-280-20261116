from __future__ import unicode_literals

import unittest

from fs.errors import FilesystemClosed, IllegalBackReference, OperationFailed, ResourceNotFound
from fs.memoryfs import MemoryFS
from fs.mountfs import MountError, MountFS
from fs.mountview import (
    MountView,
    MountViewManager,
    MountViewRootViolation,
    ScanResult,
    StaleMountView,
)
from fs.tempfs import TempFS


def _mem_fs(files):
    mem_fs = MemoryFS()
    for path, contents in files.items():
        mem_fs.makedirs(
            "/".join([""] + path.strip("/").split("/")[:-1]) or "/",
            recreate=True,
        )
        mem_fs.writebytes(path, contents)
    return mem_fs


class TestMountViewResolution(unittest.TestCase):
    def test_fixed_view_survives_replacement(self):
        manager = MountViewManager()
        old_fs = _mem_fs({"/old.txt": b"old"})
        new_fs = _mem_fs({"/new.txt": b"new"})
        manager.mount("/m1", old_fs)

        view = manager.snapshot()
        self.assertEqual(manager.generation, view.view_id)
        self.assertFalse(view.stale)

        new_generation = manager.replace("/m1", new_fs)
        self.assertNotEqual(new_generation, view.view_id)
        self.assertTrue(view.stale)

        # The frozen view still resolves against the old filesystem.
        resolved = view.resolve("/m1/old.txt")
        self.assertIs(resolved.fs, old_fs)
        self.assertEqual(resolved.virtual_path, "/m1/old.txt")
        self.assertEqual(resolved.real_path, "old.txt")
        self.assertEqual(resolved.view_id, view.view_id)
        self.assertFalse(old_fs.isclosed())

        # A fresh view sees the replacement.
        new_view = manager.snapshot()
        self.assertIs(new_view.resolve("/m1/new.txt").fs, new_fs)
        self.assertEqual(new_view.resolve("/m1/new.txt").real_path, "new.txt")

    def test_virtual_and_real_paths_recorded(self):
        manager = MountViewManager()
        delegate = MemoryFS()
        manager.mount("/collect/m1", delegate)
        view = manager.snapshot()

        resolved = view.resolve("/collect/m1/sub/dir/file.txt")
        self.assertEqual(resolved.virtual_path, "/collect/m1/sub/dir/file.txt")
        self.assertEqual(resolved.real_path, "sub/dir/file.txt")
        self.assertEqual(resolved.mount_path, "/collect/m1")

    def test_sys_path_recorded_for_os_fs(self):
        manager = MountViewManager()
        temp_fs = TempFS()
        try:
            temp_fs.writebytes("data.bin", b"x")
            manager.mount("/m1", temp_fs)
            view = manager.snapshot()
            resolved = view.resolve("/m1/data.bin")
            self.assertEqual(resolved.sys_path, temp_fs.getsyspath("data.bin"))
        finally:
            temp_fs.close()

    def test_mappings_list_every_mount(self):
        manager = MountViewManager()
        fs_one = MemoryFS()
        fs_two = MemoryFS()
        manager.mount("/m1", fs_one)
        manager.mount("/m2", fs_two)
        view = manager.snapshot()

        mappings = {mapping.virtual_path: mapping.fs for mapping in view.mappings()}
        self.assertEqual(set(mappings), {"/m1", "/m2"})
        self.assertIs(mappings["/m1"], fs_one)
        self.assertIs(mappings["/m2"], fs_two)
        for mapping in view.mappings():
            self.assertEqual(mapping.view_id, view.view_id)


class TestNestedMounts(unittest.TestCase):
    def test_longest_valid_prefix_wins(self):
        manager = MountViewManager()
        parent_fs = MemoryFS()
        child_fs = MemoryFS()
        manager.mount("/a", parent_fs)
        manager.mount("/a/b", child_fs)
        view = manager.snapshot()

        child_resolved = view.resolve("/a/b/x.txt")
        self.assertIs(child_resolved.fs, child_fs)
        self.assertEqual(child_resolved.real_path, "x.txt")
        self.assertEqual(child_resolved.mount_path, "/a/b")

        parent_resolved = view.resolve("/a/y.txt")
        self.assertIs(parent_resolved.fs, parent_fs)
        self.assertEqual(parent_resolved.real_path, "y.txt")
        self.assertEqual(parent_resolved.mount_path, "/a")

    def test_longest_prefix_independent_of_insert_order(self):
        manager = MountViewManager()
        parent_fs = MemoryFS()
        child_fs = MemoryFS()
        # Register the deeper mount first.
        manager.mount("/a/b/c", child_fs)
        manager.mount("/a", parent_fs)
        view = manager.snapshot()

        self.assertEqual(view.mount_paths(), ["/a/b/c", "/a"])
        self.assertIs(view.resolve("/a/b/c/deep").fs, child_fs)
        self.assertIs(view.resolve("/a/b/other").fs, parent_fs)

    def test_mount_boundary_is_traversed_through_nested_fs(self):
        manager = MountViewManager()
        parent_fs = _mem_fs({"/inside.txt": b"p", "/sub/nested.txt": b"n"})
        child_fs = _mem_fs({"/replacement.txt": b"c"})
        manager.mount("/m1", parent_fs)
        manager.mount("/m1/sub", child_fs)
        view = manager.snapshot()

        self.assertIs(view.resolve("/m1/sub/replacement.txt").fs, child_fs)
        self.assertIs(view.resolve("/m1/inside.txt").fs, parent_fs)


class TestAuthorizedRoot(unittest.TestCase):
    def test_normalization_stays_inside_root(self):
        manager = MountViewManager(root="/collect")
        view = manager.snapshot()

        self.assertEqual(view.normalize("/collect/a/../b"), "/collect/b")
        self.assertEqual(view.normalize("/collect"), "/collect")
        self.assertEqual(view.root, "/collect")

    def test_normalization_cannot_cross_authorized_root(self):
        manager = MountViewManager(root="/collect")
        view = manager.snapshot()

        with self.assertRaises(MountViewRootViolation):
            view.normalize("/collect/../etc/passwd")
        with self.assertRaises(MountViewRootViolation):
            view.normalize("/other/place")

    def test_backreference_beyond_fs_root_still_rejected(self):
        manager = MountViewManager(root="/")
        view = manager.snapshot()
        with self.assertRaises(IllegalBackReference):
            view.normalize("/collect/../../etc")

    def test_resolve_confined_to_root(self):
        manager = MountViewManager(root="/collect")
        delegate = MemoryFS()
        manager.mount("/collect/m1", delegate)
        view = manager.snapshot()

        with self.assertRaises(MountViewRootViolation):
            view.resolve("/collect/m1/../../etc")
        resolved = view.resolve("/collect/m1/./file")
        self.assertEqual(resolved.virtual_path, "/collect/m1/file")
        self.assertEqual(resolved.real_path, "file")


class TestScanSession(unittest.TestCase):
    def _manager(self):
        manager = MountViewManager()
        first = _mem_fs(
            {
                "/top.txt": b"top",
                "/sub/one.txt": b"1",
                "/sub/deep/two.txt": b"2",
            }
        )
        second = _mem_fs({"/other.txt": b"3"})
        manager.mount("/m1", first)
        manager.mount("/m2", second)
        return manager, first, second

    def test_scan_walks_frozen_view_with_virtual_and_real_paths(self):
        manager, first, second = self._manager()
        session = manager.begin_scan("/")
        results = list(session.scan())

        self.assertEqual(len(results), 8)
        virtual_paths = {result.virtual_path for result in results}
        self.assertEqual(
            virtual_paths,
            {
                "/m1",
                "/m1/top.txt",
                "/m1/sub",
                "/m1/sub/one.txt",
                "/m1/sub/deep",
                "/m1/sub/deep/two.txt",
                "/m2",
                "/m2/other.txt",
            },
        )
        for result in results:
            self.assertEqual(result.view_id, session.view.view_id)
            self.assertIsInstance(result, ScanResult)

        one = next(
            result
            for result in results
            if result.virtual_path == "/m1/sub/one.txt"
        )
        self.assertEqual(one.real_path, "sub/one.txt")
        self.assertIs(one.fs, first)

    def test_in_flight_results_rejected_after_replacement(self):
        manager, first, second = self._manager()
        session = manager.begin_scan("/")
        results = list(session.scan())

        # All current initially: commit succeeds.
        self.assertEqual(manager.commit(results), results)

        replacement = _mem_fs({"/brand_new.txt": b"new"})
        manager.replace("/m1", replacement)
        self.assertTrue(session.stale)

        with self.assertRaises(StaleMountView):
            manager.commit(results)
        with self.assertRaises(StaleMountView):
            session.commit(manager, results)

        # A brand new scan is on the current generation and commits.
        new_session = manager.begin_scan("/")
        new_results = list(new_session.scan())
        self.assertEqual(manager.commit(new_results), new_results)
        self.assertEqual(
            {result.virtual_path for result in new_results},
            {"/m1", "/m1/brand_new.txt", "/m2", "/m2/other.txt"},
        )

    def test_old_view_keeps_reading_replaced_filesystem(self):
        manager, first, _ = self._manager()
        session = manager.begin_scan("/m1")
        manager.replace("/m1", _mem_fs({"/brand_new.txt": b"new"}))

        # The pinned session still walks the old delegate filesystem.
        virtual_paths = {result.virtual_path for result in session.scan()}
        self.assertIn("/m1/top.txt", virtual_paths)
        self.assertNotIn("/m1/brand_new.txt", virtual_paths)

    def test_on_error_callback(self):
        manager = MountViewManager()
        manager.mount("/m1", _mem_fs({"/ok.txt": b"ok"}))

        class ExplodingFS(MemoryFS):
            def scandir(self, path, namespaces=None, page=None):
                raise OperationFailed(path)

        manager.mount("/broken", ExplodingFS())
        broken_session = manager.begin_scan("/broken")
        seen = []
        list(broken_session.scan(on_error=lambda path, error: seen.append(path)))
        self.assertEqual(seen, ["/broken"])


class TestMountFSIntegration(unittest.TestCase):
    def test_generation_bumps_on_replace_and_unmount(self):
        mount_fs = MountFS()
        mem_fs = MemoryFS()
        mount_fs.mount("/m1", mem_fs)
        generation_before = mount_fs._view_manager.generation

        replacement = MemoryFS()
        new_generation = mount_fs.replace_mount("/m1", replacement)
        self.assertGreater(new_generation, generation_before)
        self.assertIs(mount_fs._delegate("/m1/file")[0], replacement)
        # The replaced filesystem is left open for in-flight scans.
        self.assertFalse(mem_fs.isclosed())

        detached = mount_fs.unmount("/m1")
        self.assertIs(detached, replacement)
        self.assertEqual(mount_fs.mounts, [])

    def test_replace_unknown_mount(self):
        mount_fs = MountFS()
        with self.assertRaises(ResourceNotFound):
            mount_fs.replace_mount("/missing", MemoryFS())
        with self.assertRaises(ResourceNotFound):
            mount_fs.unmount("/missing")

    def test_scan_view_isolation_end_to_end(self):
        mount_fs = MountFS()
        old_fs = _mem_fs({"/old.txt": b"old"})
        mount_fs.mount("/m1", old_fs)

        session = mount_fs.begin_scan("/")
        old_results = list(session.scan())
        self.assertEqual(
            [result.virtual_path for result in old_results],
            ["/m1", "/m1/old.txt"],
        )

        mount_fs.replace_mount("/m1", _mem_fs({"/new.txt": b"new"}))
        with self.assertRaises(StaleMountView):
            mount_fs.commit_scan(old_results)

        fresh_results = list(mount_fs.begin_scan("/").scan())
        self.assertEqual(
            [result.virtual_path for result in fresh_results],
            ["/m1", "/m1/new.txt"],
        )
        self.assertEqual(mount_fs.commit_scan(fresh_results), fresh_results)

    def test_mount_view_returns_frozen_snapshot(self):
        mount_fs = MountFS()
        mount_fs.mount("/m1", MemoryFS())
        view = mount_fs.mount_view()
        self.assertIsInstance(view, MountView)
        self.assertEqual(view.mount_paths(), ["/m1"])

        mount_fs.replace_mount("/m1", MemoryFS())
        self.assertTrue(view.stale)
        # Frozen view still resolves the original mount.
        self.assertIsNotNone(view.resolve("/m1/anything").fs)


class TestViewLifecycleAndSequences(unittest.TestCase):
    def test_closed_view_rejects_operations(self):
        manager = MountViewManager()
        manager.mount("/m1", MemoryFS())
        view = manager.snapshot()
        view.close()
        self.assertTrue(view.closed)
        with self.assertRaises(FilesystemClosed):
            view.resolve("/m1/file")
        with self.assertRaises(FilesystemClosed):
            view.normalize("/m1/file")

    def test_duplicate_mount_rejected(self):
        manager = MountViewManager()
        manager.mount("/m1", MemoryFS())
        with self.assertRaises(MountError):
            manager.mount("/m1", MemoryFS())

    def test_sequence_increments_per_replacement(self):
        manager = MountViewManager()
        manager.mount("/m1", MemoryFS())
        view = manager.snapshot()
        first = view.resolve("/m1")
        self.assertEqual(first.sequence, 0)

        manager.replace("/m1", MemoryFS())
        second = manager.snapshot().resolve("/m1")
        self.assertEqual(second.sequence, 1)

        manager.replace("/m1", MemoryFS())
        third = manager.snapshot().resolve("/m1")
        self.assertEqual(third.sequence, 2)

    def test_each_replacement_publishes_new_generation(self):
        manager = MountViewManager()
        manager.mount("/m1", MemoryFS())
        initial = manager.generation
        gen_one = manager.replace("/m1", MemoryFS())
        gen_two = manager.replace("/m1", MemoryFS())
        self.assertGreater(gen_one, initial)
        self.assertGreater(gen_two, gen_one)

    def test_mountfs_still_rejects_overlapping_mounts(self):
        # The legacy MountFS.mount overlap guard is retained; nested
        # mount semantics live in the MountViewManager API instead.
        mount_fs = MountFS()
        mount_fs.mount("/m3", MemoryFS())
        with self.assertRaises(MountError):
            mount_fs.mount("/m3/foo", MemoryFS())

    def test_mountfs_replacement_uses_longest_prefix_delegate(self):
        mount_fs = MountFS()
        first_fs = MemoryFS()
        mount_fs.mount("/m1", first_fs)
        replacement = MemoryFS()
        mount_fs.replace_mount("/m1", replacement)

        delegate_fs, real_path = mount_fs._delegate("/m1/dir/file.txt")
        self.assertIs(delegate_fs, replacement)
        self.assertEqual(real_path, "dir/file.txt")


if __name__ == "__main__":
    unittest.main()
