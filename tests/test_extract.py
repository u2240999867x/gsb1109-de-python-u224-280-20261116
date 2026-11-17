# -*- encoding: UTF-8
from __future__ import unicode_literals

import io
import tarfile
import unittest
import zipfile

from fs import errors
from fs.enums import ResourceType
from fs.extract import extract_fs
from fs.memoryfs import MemoryFS
from fs.mountfs import MountFS
from fs.multifs import MultiFS
from fs.tarfs import ReadTarFS
from fs.zipfs import ReadZipFS


def make_zip(members):
    """Make a `ReadZipFS` from a list of (name, bytes) members."""
    fileobj = io.BytesIO()
    with zipfile.ZipFile(fileobj, "w") as zf:
        for name, data in members:
            zf.writestr(name, data)
    fileobj.seek(0)
    return ReadZipFS(fileobj)


def make_tar(members):
    """Make a `ReadTarFS` from a list of (name, bytes, type, linkname) members."""
    fileobj = io.BytesIO()
    with tarfile.open(fileobj=fileobj, mode="w") as tf:
        for name, data, type_, linkname in members:
            info = tarfile.TarInfo(name)
            info.type = type_
            info.linkname = linkname
            info.size = len(data)
            info.mtime = 1234567890
            tf.addfile(info, io.BytesIO(data))
    fileobj.seek(0)
    return ReadTarFS(fileobj)


class TestExtractFS(unittest.TestCase):
    def setUp(self):
        self.src_fs = make_zip(
            [
                ("top.txt", b"top"),
                ("foo/bar.txt", b"bar"),
                ("foo/baz/egg.txt", b"egg"),
            ]
        )
        self.dst_fs = MemoryFS()

    def tearDown(self):
        self.src_fs.close()
        self.dst_fs.close()

    def test_extract_all(self):
        exported = extract_fs(self.src_fs, self.dst_fs)
        self.assertEqual(
            sorted(exported),
            ["foo", "foo/bar.txt", "foo/baz", "foo/baz/egg.txt", "top.txt"],
        )
        self.assertEqual(self.dst_fs.readbytes("top.txt"), b"top")
        self.assertEqual(self.dst_fs.readbytes("foo/bar.txt"), b"bar")
        self.assertEqual(self.dst_fs.readbytes("foo/baz/egg.txt"), b"egg")

    def test_extract_to_target_root(self):
        extract_fs(self.src_fs, self.dst_fs, "collected")
        self.assertEqual(self.dst_fs.readbytes("collected/top.txt"), b"top")
        self.assertEqual(self.dst_fs.readbytes("collected/foo/bar.txt"), b"bar")
        self.assertFalse(self.dst_fs.exists("top.txt"))

    def test_extract_members_allowlist(self):
        exported = extract_fs(
            self.src_fs, self.dst_fs, members=["top.txt", "foo/bar.txt"]
        )
        self.assertEqual(sorted(exported), ["foo/bar.txt", "top.txt"])
        self.assertEqual(self.dst_fs.readbytes("top.txt"), b"top")
        self.assertEqual(self.dst_fs.readbytes("foo/bar.txt"), b"bar")
        self.assertFalse(self.dst_fs.exists("foo/baz/egg.txt"))

    def test_extract_members_normalized(self):
        exported = extract_fs(self.src_fs, self.dst_fs, members=["./foo//bar.txt"])
        self.assertEqual(exported, ["foo/bar.txt"])
        self.assertEqual(self.dst_fs.readbytes("foo/bar.txt"), b"bar")

    def test_missing_member_not_found(self):
        with self.assertRaises(errors.ResourceNotFound):
            extract_fs(self.src_fs, self.dst_fs, members=["nothere.txt"])
        self.assertFalse(self.dst_fs.exists("nothere.txt"))

    def test_missing_member_not_filled_from_outer_fs(self):
        """A same-named file on an outer mount must not fill in for a
        member missing from the archive."""
        outer_fs = MemoryFS()
        outer_fs.writebytes("top.txt", b"outer")
        outer_fs.writebytes("nothere.txt", b"outer")
        multi_fs = MultiFS()
        multi_fs.add_fs("archive", self.src_fs, priority=1)
        multi_fs.add_fs("outer", outer_fs, priority=0)
        # The archive's own member wins over the outer file...
        extract_fs(multi_fs, self.dst_fs, members=["top.txt"])
        self.assertEqual(self.dst_fs.readbytes("top.txt"), b"top")
        # ...but a member missing from the archive must not be
        # substituted when exporting from the archive itself.
        with self.assertRaises(errors.ResourceNotFound):
            extract_fs(self.src_fs, self.dst_fs, members=["nothere.txt"])
        self.assertFalse(self.dst_fs.exists("nothere.txt"))

    def test_extract_from_mounted_archive(self):
        """Exporting a mounted archive reads members from the archive,
        not from same-named files on the outer filesystem."""
        mount_fs = MountFS()
        mount_fs.mount("archive", self.src_fs)
        mount_fs.writebytes("outer.txt", b"outer")
        extract_fs(mount_fs.opendir("archive"), self.dst_fs)
        self.assertEqual(self.dst_fs.readbytes("top.txt"), b"top")
        self.assertFalse(self.dst_fs.exists("outer.txt"))

    def test_member_escaping_target_root(self):
        with self.assertRaises(errors.IllegalBackReference):
            extract_fs(self.src_fs, self.dst_fs, members=["../escape.txt"])
        self.assertFalse(self.dst_fs.exists("escape.txt"))
        self.assertEqual(self.dst_fs.listdir("/"), [])

    def test_out_of_bounds_members_not_extracted(self):
        """Members outside of the archive root are never written
        outside of the target root."""
        src_fs = make_zip(
            [("../evil.txt", b"evil"), ("/abs/../up.txt", b"up"), ("ok.txt", b"ok")]
        )
        try:
            exported = extract_fs(src_fs, self.dst_fs)
        finally:
            src_fs.close()
        self.assertEqual(sorted(exported), ["ok.txt", "up.txt"])
        self.assertEqual(self.dst_fs.readbytes("ok.txt"), b"ok")
        self.assertFalse(self.dst_fs.exists("evil.txt"))
        self.assertEqual(sorted(self.dst_fs.listdir("/")), ["ok.txt", "up.txt"])

    def test_on_duplicate_error(self):
        self.dst_fs.writebytes("top.txt", b"existing")
        with self.assertRaises(errors.DestinationExists):
            extract_fs(self.src_fs, self.dst_fs, on_duplicate="error")
        self.assertEqual(self.dst_fs.readbytes("top.txt"), b"existing")

    def test_on_duplicate_skip(self):
        self.dst_fs.writebytes("top.txt", b"existing")
        extract_fs(self.src_fs, self.dst_fs, on_duplicate="skip")
        self.assertEqual(self.dst_fs.readbytes("top.txt"), b"existing")
        self.assertEqual(self.dst_fs.readbytes("foo/bar.txt"), b"bar")

    def test_on_duplicate_replace(self):
        self.dst_fs.writebytes("top.txt", b"existing")
        extract_fs(self.src_fs, self.dst_fs, on_duplicate="replace")
        self.assertEqual(self.dst_fs.readbytes("top.txt"), b"top")

    def test_on_duplicate_members(self):
        with self.assertRaises(errors.DestinationExists):
            extract_fs(
                self.src_fs,
                self.dst_fs,
                members=["top.txt", "top.txt"],
                on_duplicate="error",
            )

    def test_invalid_policies(self):
        with self.assertRaises(ValueError):
            extract_fs(self.src_fs, self.dst_fs, on_duplicate="bogus")
        with self.assertRaises(ValueError):
            extract_fs(self.src_fs, self.dst_fs, links="bogus")

    def test_links_skipped_by_default(self):
        src_fs = make_tar(
            [
                ("top.txt", b"top", tarfile.REGTYPE, ""),
                ("link.txt", b"", tarfile.SYMTYPE, "top.txt"),
            ]
        )
        try:
            exported = extract_fs(src_fs, self.dst_fs)
        finally:
            src_fs.close()
        self.assertEqual(exported, ["top.txt"])
        self.assertEqual(self.dst_fs.readbytes("top.txt"), b"top")
        self.assertFalse(self.dst_fs.exists("link.txt"))

    def test_links_error(self):
        src_fs = make_tar(
            [
                ("top.txt", b"top", tarfile.REGTYPE, ""),
                ("link.txt", b"", tarfile.SYMTYPE, "top.txt"),
            ]
        )
        try:
            with self.assertRaises(errors.Unsupported):
                extract_fs(src_fs, self.dst_fs, links="error")
        finally:
            src_fs.close()

    def test_on_extract_callback(self):
        extracted = []
        extract_fs(
            self.src_fs,
            self.dst_fs,
            members=["top.txt"],
            on_extract=lambda src_fs, src_path, dst_fs, dst_path: extracted.append(
                (src_path, dst_path)
            ),
        )
        self.assertEqual(extracted, [("top.txt", "/top.txt")])

    def test_preserve_time(self):
        src_fs = make_tar([("top.txt", b"top", tarfile.REGTYPE, "")])
        try:
            extract_fs(src_fs, self.dst_fs, preserve_time=True)
        finally:
            src_fs.close()
        details = self.dst_fs.getinfo("top.txt", ["details"]).raw["details"]
        self.assertEqual(details["modified"], 1234567890)


if __name__ == "__main__":
    unittest.main()
