from __future__ import unicode_literals

import unittest

from fs import errors
from fs.memoryfs import MemoryFS
from fs.multifs import MultiFS
from fs.path import dirname
from fs.test import FSTestCases


class TestMultiFS(FSTestCases, unittest.TestCase):
    """Test OSFS implementation."""

    def setUp(self):
        fs = MultiFS()
        mem_fs = MemoryFS()
        fs.add_fs("mem", mem_fs, write=True)
        self.fs = fs
        self.mem_fs = mem_fs

    def make_fs(self):
        fs = MultiFS()
        mem_fs = MemoryFS()
        fs.add_fs("mem", mem_fs, write=True)
        return fs

    def test_get_fs(self):
        self.assertIs(self.fs.get_fs("mem"), self.mem_fs)

    def test_which(self):
        self.fs.writebytes("foo", b"bar")
        self.assertEqual(self.fs.which("foo"), ("mem", self.mem_fs))
        self.assertEqual(self.fs.which("bar", "w"), ("mem", self.mem_fs))
        self.assertEqual(self.fs.which("baz"), (None, None))

    def test_auto_close(self):
        """Test MultiFS auto close is working"""
        multi_fs = MultiFS()
        m1 = MemoryFS()
        m2 = MemoryFS()
        multi_fs.add_fs("m1", m1)
        multi_fs.add_fs("m2", m2)
        self.assertFalse(m1.isclosed())
        self.assertFalse(m2.isclosed())
        multi_fs.close()
        self.assertTrue(m1.isclosed())
        self.assertTrue(m2.isclosed())

    def test_no_auto_close(self):
        """Test MultiFS auto close can be disabled"""
        multi_fs = MultiFS(auto_close=False)
        self.assertEqual(repr(multi_fs), "MultiFS(auto_close=False)")
        m1 = MemoryFS()
        m2 = MemoryFS()
        multi_fs.add_fs("m1", m1)
        multi_fs.add_fs("m2", m2)
        self.assertFalse(m1.isclosed())
        self.assertFalse(m2.isclosed())
        multi_fs.close()
        self.assertFalse(m1.isclosed())
        self.assertFalse(m2.isclosed())

    def test_opener(self):
        """Test use of FS URLs."""
        multi_fs = MultiFS()
        with self.assertRaises(TypeError):
            multi_fs.add_fs("foo", 5)
        multi_fs.add_fs("f1", "mem://")
        multi_fs.add_fs("f2", "temp://")
        self.assertIsInstance(multi_fs.get_fs("f1"), MemoryFS)

    def test_priority(self):
        """Test priority order is working"""
        m1 = MemoryFS()
        m2 = MemoryFS()
        m3 = MemoryFS()
        m1.writebytes("name", b"m1")
        m2.writebytes("name", b"m2")
        m3.writebytes("name", b"m3")
        multi_fs = MultiFS(auto_close=False)
        multi_fs.add_fs("m1", m1)
        multi_fs.add_fs("m2", m2)
        multi_fs.add_fs("m3", m3)
        self.assertEqual(multi_fs.readbytes("name"), b"m3")

        m1 = MemoryFS()
        m2 = MemoryFS()
        m3 = MemoryFS()
        m1.writebytes("name", b"m1")
        m2.writebytes("name", b"m2")
        m3.writebytes("name", b"m3")
        multi_fs = MultiFS(auto_close=False)
        multi_fs.add_fs("m1", m1)
        multi_fs.add_fs("m2", m2, priority=10)
        multi_fs.add_fs("m3", m3)
        self.assertEqual(multi_fs.readbytes("name"), b"m2")

        m1 = MemoryFS()
        m2 = MemoryFS()
        m3 = MemoryFS()
        m1.writebytes("name", b"m1")
        m2.writebytes("name", b"m2")
        m3.writebytes("name", b"m3")
        multi_fs = MultiFS(auto_close=False)
        multi_fs.add_fs("m1", m1)
        multi_fs.add_fs("m2", m2, priority=10)
        multi_fs.add_fs("m3", m3, priority=10)
        self.assertEqual(multi_fs.readbytes("name"), b"m3")

        m1 = MemoryFS()
        m2 = MemoryFS()
        m3 = MemoryFS()
        m1.writebytes("name", b"m1")
        m2.writebytes("name", b"m2")
        m3.writebytes("name", b"m3")
        multi_fs = MultiFS(auto_close=False)
        multi_fs.add_fs("m1", m1, priority=11)
        multi_fs.add_fs("m2", m2, priority=10)
        multi_fs.add_fs("m3", m3, priority=10)
        self.assertEqual(multi_fs.readbytes("name"), b"m1")

    def test_no_writable(self):
        fs = MultiFS()
        with self.assertRaises(errors.ResourceReadOnly):
            fs.writebytes("foo", b"bar")

    def test_validate_path(self):
        self.fs.write_fs = None
        self.fs.validatepath("foo")

    def test_listdir_duplicates(self):
        m1 = MemoryFS()
        m2 = MemoryFS()
        m1.touch("foo")
        m2.touch("foo")
        multi_fs = MultiFS()
        multi_fs.add_fs("m1", m1)
        multi_fs.add_fs("m2", m2)
        self.assertEqual(multi_fs.listdir("/"), ["foo"])


class TestMultiFSLayers(unittest.TestCase):
    """Test layered writes and policy-driven deletions."""

    def make_layers(self, policy="retract", upper_files=None, base_files=None):
        base = MemoryFS()
        upper = MemoryFS()
        for path, contents in (base_files or {}).items():
            base.makedirs(dirname(path), recreate=True)
            base.writebytes(path, contents)
        for path, contents in (upper_files or {}).items():
            upper.makedirs(dirname(path), recreate=True)
            upper.writebytes(path, contents)
        multi_fs = MultiFS(auto_close=False, deletion_policy=policy)
        multi_fs.add_fs("base", base)
        multi_fs.add_fs("upper", upper, write=True)
        return multi_fs, base, upper

    def test_write_plan_names_backend(self):
        multi_fs, base, upper = self.make_layers(base_files={"doc": b"base"})

        plan = multi_fs.write_plan("doc", "w")
        self.assertIs(plan.target, upper)
        self.assertEqual(plan.target_name, "upper")
        self.assertEqual(plan.backend, upper)
        self.assertEqual(plan.backend_name, "upper")
        self.assertEqual(plan.source_name, "base")
        self.assertFalse(plan.copy_up)
        self.assertIn("upper", plan.describe())

        append_plan = multi_fs.write_plan("doc", "a")
        self.assertTrue(append_plan.copy_up)
        self.assertIn("copy-up", append_plan.describe())

        read_plan = multi_fs.write_plan("doc", "r")
        self.assertIsNone(read_plan.target)
        self.assertFalse(read_plan.writable)
        self.assertIn("does not write", read_plan.describe())

    def test_write_plan_without_backend(self):
        multi_fs, _base, _upper = self.make_layers()
        multi_fs.write_fs = None
        plan = multi_fs.write_plan("doc", "w")
        self.assertIsNone(plan.target)
        self.assertFalse(plan.writable)
        self.assertIn("read-only", plan.describe())

    def test_writes_never_touch_base(self):
        multi_fs, base, upper = self.make_layers(base_files={"doc": b"base"})
        multi_fs.writebytes("doc", b"corrected")
        multi_fs.writetext("text", "hello")
        multi_fs.create("new")

        self.assertEqual(base.readbytes("doc"), b"base")
        self.assertEqual(upper.readbytes("doc"), b"corrected")
        self.assertFalse(base.exists("text"))
        self.assertFalse(base.exists("new"))
        self.assertEqual(multi_fs.readbytes("doc"), b"corrected")

    def test_append_copies_up_lower_file(self):
        multi_fs, base, upper = self.make_layers(base_files={"doc": b"base"})
        with multi_fs.open("doc", "ab") as append_file:
            append_file.write(b"+more")

        self.assertEqual(base.readbytes("doc"), b"base")
        self.assertEqual(upper.readbytes("doc"), b"base+more")
        self.assertEqual(multi_fs.readbytes("doc"), b"base+more")

    def test_delete_plan_retracts_correction(self):
        multi_fs, base, upper = self.make_layers(
            policy="retract",
            base_files={"doc": b"base"},
            upper_files={"doc": b"corrected"},
        )

        plan = multi_fs.delete_plan("doc")
        self.assertTrue(plan.retracts)
        self.assertFalse(plan.shadows)
        self.assertTrue(plan.reveals_base)
        self.assertIs(plan.backend, upper)
        self.assertIs(plan.lower_fs, base)
        self.assertTrue(plan.lower_untouched)

        multi_fs.remove("doc")
        self.assertEqual(base.readbytes("doc"), b"base")
        self.assertFalse(upper.exists("doc"))
        self.assertEqual(multi_fs.readbytes("doc"), b"base")

    def test_delete_plan_shadows_base(self):
        multi_fs, base, upper = self.make_layers(
            policy="shadow",
            base_files={"doc": b"base"},
            upper_files={"doc": b"corrected"},
        )

        plan = multi_fs.delete_plan("doc")
        self.assertTrue(plan.shadows)
        self.assertFalse(plan.retracts)
        self.assertFalse(plan.reveals_base)
        self.assertTrue(plan.lower_untouched)

        multi_fs.remove("doc")
        self.assertEqual(base.readbytes("doc"), b"base")
        self.assertFalse(upper.exists("doc"))
        self.assertFalse(multi_fs.exists("doc"))
        self.assertTrue(multi_fs.is_shadowed("doc"))
        self.assertEqual(multi_fs.listdir("/"), [])

    def test_delete_base_only_does_not_physically_delete(self):
        multi_fs, base, upper = self.make_layers(
            policy="retract", base_files={"doc": b"base"}
        )

        plan = multi_fs.delete_plan("doc")
        self.assertTrue(plan.shadows)
        self.assertFalse(plan.physically_deletes)

        multi_fs.remove("doc")
        self.assertTrue(base.exists("doc"))
        self.assertEqual(base.readbytes("doc"), b"base")
        self.assertFalse(multi_fs.exists("doc"))
        self.assertEqual(multi_fs.shadowed_paths(), ["/doc"])

    def test_retracted_base_is_revealed_again(self):
        multi_fs, base, upper = self.make_layers(base_files={"doc": b"base"})
        multi_fs.writebytes("doc", b"corrected")
        multi_fs.remove("doc")

        self.assertEqual(multi_fs.readbytes("doc"), b"base")
        self.assertEqual(multi_fs.which("doc"), ("base", base))

    def test_removed_upper_file_does_not_appear_deleted(self):
        multi_fs, base, upper = self.make_layers(base_files={"doc": b"base"})
        multi_fs.writebytes("doc", b"corrected")
        self.assertEqual(multi_fs.readbytes("doc"), b"corrected")

        # A physical delete of the upper layer alone is not a global
        # delete of the layered view.
        upper.remove("doc")
        self.assertEqual(multi_fs.readbytes("doc"), b"base")
        self.assertFalse(multi_fs.is_shadowed("doc"))

    def test_shadow_survives_reload(self):
        multi_fs, base, upper = self.make_layers(base_files={"doc": b"base"})
        multi_fs.remove("doc")
        self.assertFalse(multi_fs.exists("doc"))

        reloaded = MultiFS(auto_close=False, deletion_policy="retract")
        reloaded.add_fs("base", base)
        reloaded.add_fs("upper", upper, write=True)
        self.assertFalse(reloaded.exists("doc"))
        self.assertEqual(reloaded.shadowed_paths(), ["/doc"])

    def test_write_clears_shadow(self):
        multi_fs, base, upper = self.make_layers(base_files={"doc": b"base"})
        multi_fs.remove("doc")
        self.assertFalse(multi_fs.exists("doc"))

        multi_fs.writebytes("doc", b"new")
        self.assertTrue(multi_fs.exists("doc"))
        self.assertEqual(multi_fs.readbytes("doc"), b"new")
        self.assertFalse(multi_fs.is_shadowed("doc"))
        self.assertEqual(base.readbytes("doc"), b"base")

    def test_shadow_hides_new_lower_files(self):
        multi_fs, base, upper = self.make_layers(
            policy="shadow", base_files={"logs/old.txt": b"old"}
        )
        multi_fs.removetree("logs")
        self.assertTrue(multi_fs.is_shadowed("logs"))

        # New files added to the base after the tombstone stay hidden.
        base.writebytes("logs/new.txt", b"new")
        self.assertFalse(multi_fs.exists("logs/new.txt"))
        self.assertFalse(multi_fs.exists("logs"))

    def test_shadowed_directory_hides_children(self):
        multi_fs, base, upper = self.make_layers(
            policy="shadow", base_files={"logs/a.txt": b"a", "logs/b.txt": b"b"}
        )
        multi_fs.removetree("logs")
        self.assertFalse(multi_fs.exists("logs/a.txt"))
        self.assertEqual(multi_fs.shadowed_paths(), ["/logs"])

        multi_fs.makedirs("logs")
        self.assertTrue(multi_fs.isdir("logs"))
        self.assertEqual(multi_fs.listdir("logs"), ["a.txt", "b.txt"])
        self.assertFalse(multi_fs.is_shadowed("logs"))

    def test_removetree_with_lower_layer(self):
        multi_fs, base, upper = self.make_layers(
            policy="shadow",
            base_files={"logs/a.txt": b"a", "logs/b.txt": b"b"},
            upper_files={"logs/c.txt": b"c"},
        )

        multi_fs.removetree("logs")
        self.assertFalse(multi_fs.exists("logs"))
        self.assertEqual(base.listdir("logs"), ["a.txt", "b.txt"])
        self.assertFalse(upper.exists("logs"))
        self.assertTrue(multi_fs.is_shadowed("logs"))

    def test_delete_policy_alias_and_override(self):
        multi_fs, base, upper = self.make_layers(
            policy="retract",
            base_files={"doc": b"base"},
            upper_files={"doc": b"corrected"},
        )
        self.assertEqual(str(multi_fs.deletion_policy), "DeletionPolicy.retract")
        self.assertTrue(multi_fs.delete_plan("doc", policy="shadow").shadows)
        self.assertTrue(multi_fs.delete_plan("doc", policy="whiteout").shadows)
        self.assertTrue(multi_fs.delete_plan("doc", policy="retract").retracts)

        with self.assertRaises(ValueError):
            multi_fs.delete_plan("doc", policy="bogus")

       # Per-call policy override applies to the actual operation.
        multi_fs.remove("doc", policy="shadow")
        self.assertFalse(multi_fs.exists("doc"))
        self.assertEqual(base.readbytes("doc"), b"base")

    def test_write_target_stays_upper(self):
        multi_fs, base, upper = self.make_layers(
            base_files={"doc": b"base"}, upper_files={"other": b"o"}
        )
        self.assertEqual(multi_fs.which("doc", "w"), ("upper", upper))
        self.assertEqual(multi_fs.write_plan("doc", "a").target_name, "upper")
        self.assertEqual(multi_fs.write_plan("other", "a").copy_up, False)

