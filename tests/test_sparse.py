from __future__ import unicode_literals

import io
import tarfile
import unittest
import zipfile
from unittest import mock

from fs.base import FS
from fs.enums import ResourceType
from fs.errors import ResourceModified, ResourceReadOnly
from fs.ftpfs import FTPFS
from fs.info import Info
from fs.memoryfs import MemoryFS
from fs.mode import Mode
from fs.mountfs import MountFS
from fs.multifs import MultiFS
from fs.sparse import (
    ByteRange,
    RangeReadResult,
    ReadBackend,
    ReadStrategy,
    SparseReader,
    SparseSession,
)
from fs.tarfs import ReadTarFS
from fs.tempfs import TempFS
from fs.zipfs import ReadZipFS


DATA = bytes((i * 7) % 256 for i in range(1000))


class _ForwardStream(io.RawIOBase):
    """A stream that can only be read front to back, never seeked."""

    def __init__(self, blob):
        self._buffer = io.BytesIO(blob)

    def readable(self):
        return True

    def seekable(self):
        return False

    def readinto(self, b):
        chunk = self._buffer.read(len(b))
        b[: len(chunk)] = chunk
        return len(chunk)


class _ForwardOnlyFS(FS):
    """A backend whose openbin streams are not seekable."""

    _meta = {"network": True, "read_only": True, "thread_safe": True}

    def __init__(self, blob, modified=1000.5):
        super(_ForwardOnlyFS, self).__init__()
        self._blob = blob
        self._modified = modified

    def openbin(self, path, mode="r", buffering=-1, **options):
        Mode(mode).validate_bin()
        if "w" in mode or "a" in mode or "+" in mode:
            raise ResourceReadOnly(path)
        return _ForwardStream(self._blob)

    def getinfo(self, path, namespaces=None):
        return Info(
            {
                "basic": {"name": "x", "is_dir": False},
                "details": {
                    "type": int(ResourceType.file),
                    "size": len(self._blob),
                    "modified": self._modified,
                },
            }
        )

    def listdir(self, path):
        return []

    def makedir(self, path, permissions=None, recreate=False):
        raise NotImplementedError

    def remove(self, path):
        raise NotImplementedError

    def removedir(self, path):
        raise NotImplementedError

    def setinfo(self, path, info):
        raise NotImplementedError


def _make_zip_fs(blob):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("obj.bin", blob)
    buffer.seek(0)
    return ReadZipFS(buffer)


def _make_tar_fs(blob):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        member = tarfile.TarInfo("obj.bin")
        member.size = len(blob)
        archive.addfile(member, io.BytesIO(blob))
    buffer.seek(0)
    return ReadTarFS(buffer)


class TestByteRange(unittest.TestCase):
    def test_length(self):
        self.assertEqual(ByteRange(10, 25).length, 15)

    def test_validation(self):
        with self.assertRaises(ValueError):
            ByteRange(-1, 10)
        with self.assertRaises(ValueError):
            ByteRange(10, 9)

    def test_coerce(self):
        ranges = ByteRange.coerce([(0, 4), ByteRange(8, 12)])
        self.assertEqual(ranges, [ByteRange(0, 4), ByteRange(8, 12)])


class TestClassification(unittest.TestCase):
    def test_memory_fs_is_direct(self):
        mem = MemoryFS()
        mem.writebytes("obj.bin", DATA)
        backend = SparseReader(mem).classify("obj.bin")
        self.assertIsInstance(backend, ReadBackend)
        self.assertEqual(backend.name, "MemoryFS")
        self.assertEqual(backend.strategy, ReadStrategy.direct_range)
        self.assertTrue(backend.seekable)

    def test_temp_fs_is_direct(self):
        tmp = TempFS()
        try:
            tmp.writebytes("obj.bin", DATA)
            backend = SparseReader(tmp).classify("obj.bin")
            self.assertEqual(backend.strategy, ReadStrategy.direct_range)
        finally:
            tmp.close()

    def test_tar_fs_on_seekable_container_is_direct(self):
        tar_fs = _make_tar_fs(DATA)
        try:
            backend = SparseReader(tar_fs).classify("obj.bin")
            self.assertEqual(backend.strategy, ReadStrategy.direct_range)
            self.assertTrue(backend.seekable)
        finally:
            tar_fs.close()

    def test_zip_fs_is_staged(self):
        zip_fs = _make_zip_fs(DATA)
        try:
            backend = SparseReader(zip_fs).classify("obj.bin")
            self.assertEqual(backend.name, "ReadZipFS")
            self.assertEqual(backend.strategy, ReadStrategy.local_staging)
        finally:
            zip_fs.close()

    def test_ftp_fs_is_staged_without_connecting(self):
        ftp = FTPFS("localhost.invalid")
        backend = SparseReader(ftp).classify("/obj.bin")
        self.assertEqual(backend.strategy, ReadStrategy.local_staging)

    def test_forward_only_fs_is_sequential(self):
        backend = SparseReader(_ForwardOnlyFS(DATA)).classify("x")
        self.assertEqual(backend.strategy, ReadStrategy.sequential_skip)
        self.assertFalse(backend.seekable)

    def test_wrappers_are_unwrapped_to_concrete_backend(self):
        mem = MemoryFS()
        mem.makedirs("dir")
        mem.writebytes("dir/obj.bin", DATA)
        self.assertEqual(
            SparseReader(mem.opendir("dir")).classify("obj.bin").name,
            "MemoryFS",
        )

        multi = MultiFS()
        layer = MemoryFS()
        layer.writebytes("m.bin", DATA)
        multi.add_fs("layer", layer)
        self.assertEqual(
            SparseReader(multi).classify("m.bin").name, "MemoryFS"
        )

        mount = MountFS()
        mount.mount("mnt", mem)
        self.assertEqual(
            SparseReader(mount).classify("/mnt/dir/obj.bin").name,
            "MemoryFS",
        )

    def test_invalid_verify_policy(self):
        with self.assertRaises(ValueError):
            SparseReader(MemoryFS(), verify="nope")


class TestDirectRangeRead(unittest.TestCase):
    def setUp(self):
        self.mem = MemoryFS()
        self.mem.writebytes("obj.bin", DATA)
        self.reader = SparseReader(self.mem)

    def test_reads_only_requested_bytes(self):
        result = self.reader.read_ranges(
            "obj.bin", [(0, 10), (100, 110), (990, 1000)]
        )
        self.assertIsInstance(result, RangeReadResult)
        self.assertEqual(
            list(result.data.values()),
            [DATA[0:10], DATA[100:110], DATA[990:1000]],
        )
        self.assertEqual(result.requested, 30)
        self.assertEqual(result.source_bytes, 30)
        self.assertEqual(result.local_bytes, 0)
        self.assertEqual(result.actual, 30)
        self.assertEqual(result.overhead, 0)
        self.assertEqual(result.strategy, ReadStrategy.direct_range)

    def test_range_out_of_bounds(self):
        with self.assertRaises(ValueError):
            self.reader.read_ranges("obj.bin", [(0, 100000)])

    def test_empty_ranges_rejected(self):
        with self.assertRaises(ValueError):
            self.reader.read_ranges("obj.bin", [])


class TestSequentialSkipRead(unittest.TestCase):
    def test_gaps_are_read_and_discarded(self):
        reader = SparseReader(_ForwardOnlyFS(DATA))
        result = reader.read_ranges("x", [(10, 12), (50, 53), (990, 1000)])
        self.assertEqual(result.strategy, ReadStrategy.sequential_skip)
        self.assertEqual(result.data[ByteRange(10, 12)], DATA[10:12])
        self.assertEqual(result.data[ByteRange(50, 53)], DATA[50:53])
        self.assertEqual(result.data[ByteRange(990, 1000)], DATA[990:1000])
        # The whole prefix up to the last range is physically pulled.
        self.assertEqual(result.requested, 15)
        self.assertEqual(result.source_bytes, 1000)
        self.assertEqual(result.overhead, 985)
        self.assertEqual(result.local_bytes, 0)

    def test_unordered_ranges_are_sorted_for_forward_read(self):
        reader = SparseReader(_ForwardOnlyFS(DATA))
        result = reader.read_ranges("x", [(50, 53), (10, 12)])
        # Output keeps the requested order while the stream runs forward.
        self.assertEqual(
            list(result.data.keys()),
            [ByteRange(50, 53), ByteRange(10, 12)],
        )
        self.assertEqual(result.data[ByteRange(10, 12)], DATA[10:12])


class TestLocalStagingRead(unittest.TestCase):
    def test_first_read_copies_whole_object_later_reads_use_cache(self):
        zip_fs = _make_zip_fs(DATA)
        reader = SparseReader(zip_fs)
        try:
            with reader.session("obj.bin") as session:
                first = session.read_ranges([(0, 5), (500, 505)])
                self.assertEqual(first.strategy, ReadStrategy.local_staging)
                self.assertEqual(first.source_bytes, 1000)
                self.assertEqual(first.local_bytes, 10)
                self.assertTrue(session.version.digest)

                second = session.read_ranges([(900, 910)])
                self.assertEqual(second.source_bytes, 0)
                self.assertEqual(second.local_bytes, 10)
                self.assertEqual(
                    second.data[ByteRange(900, 910)], DATA[900:910]
                )
        finally:
            zip_fs.close()

    def test_explicit_staging_fs_is_reused_within_session_and_cleaned_up(self):
        staging = TempFS()
        zip_fs = _make_zip_fs(DATA)
        reader = SparseReader(zip_fs, staging_fs=staging)
        try:
            with reader.session("obj.bin") as session:
                session.read_ranges([(0, 4)])
                session.read_ranges([(10, 14)])
                # One copy is shared by every read in this session.
                self.assertEqual(len(staging.listdir("/")), 1)
            # The owned staging filesystem itself stays open, but the
            # session removes the staged copy on exit.
            self.assertEqual(staging.listdir("/"), [])
        finally:
            zip_fs.close()
            staging.close()

    def test_two_sessions_share_staging_fs_without_cross_contamination(self):
        staging = TempFS()
        zip_fs = _make_zip_fs(DATA)
        reader = SparseReader(zip_fs, staging_fs=staging)
        try:
            with reader.session("obj.bin") as first:
                first.read_ranges([(0, 4)])
            with reader.session("obj.bin") as second:
                result = second.read_ranges([(0, 4)])
            self.assertEqual(result.data[ByteRange(0, 4)], DATA[:4])
            self.assertEqual(staging.listdir("/"), [])
        finally:
            zip_fs.close()
            staging.close()


class TestVersionGuards(unittest.TestCase):
    def test_float_mtime_detects_same_second_same_length_rewrite(self):
        mem = MemoryFS()
        mem.writebytes("obj.bin", DATA)
        reader = SparseReader(mem)
        with reader.session("obj.bin") as session:
            self.assertIsNone(session.version.digest)
            session.read_ranges([(0, 5)])
            mem.writebytes("obj.bin", b"z" * len(DATA))
            with self.assertRaises(ResourceModified) as caught:
                session.read_ranges([(10, 15)])
            self.assertEqual(
                caught.exception.expected.size, caught.exception.observed.size
            )

    def test_hash_fallback_when_mtime_missing(self):
        class _NoMtimeFS(_ForwardOnlyFS):
            def getinfo(self, path, namespaces=None):
                return Info(
                    {
                        "basic": {"name": "x", "is_dir": False},
                        "details": {
                            "type": int(ResourceType.file),
                            "size": len(self._blob),
                        },
                    }
                )

        reader = SparseReader(_NoMtimeFS(DATA))
        with reader.session("x") as session:
            self.assertIsNone(session.version.modified)
            self.assertIsNotNone(session.version.digest)
            result = session.read_ranges([(0, 4)])
            self.assertEqual(result.data[ByteRange(0, 4)], DATA[:4])

    def test_coarse_mtime_same_length_content_swap_rejected_by_hash(self):
        coarse = _ForwardOnlyFS(DATA, modified=1000)
        reader = SparseReader(coarse)
        with reader.session("x") as session:
            self.assertEqual(session.version.modified, 1000)
            self.assertIsNotNone(session.version.digest)
            session.read_ranges([(0, 4)])
            coarse._blob = b"q" * len(DATA)  # same length, same mtime
            with self.assertRaises(ResourceModified) as caught:
                session.read_ranges([(8, 12)])
            self.assertNotEqual(
                caught.exception.expected.digest,
                caught.exception.observed.digest,
            )

    def test_force_hash_policy(self):
        mem = MemoryFS()
        mem.writebytes("obj.bin", DATA)
        reader = SparseReader(mem, verify="hash")
        with reader.session("obj.bin") as session:
            self.assertIsNotNone(session.version.digest)

    def test_mid_read_truncation_rejected_after_read(self):
        mem = MemoryFS()
        mem.writebytes("obj.bin", DATA)
        reader = SparseReader(mem)
        original = SparseSession._read_exact
        armed = {"on": True}

        def patched(self, stream, length):
            if armed["on"]:
                armed["on"] = False
                with mem.openbin("obj.bin", "wb") as handle:
                    handle.write(b"t" * 500)
            return original(self, stream, length)

        with mock.patch.object(SparseSession, "_read_exact", patched):
            with self.assertRaises(ResourceModified):
                reader.read_ranges("obj.bin", [(0, 5), (400, 405)])

    def test_unchanged_object_reads_across_session(self):
        mem = MemoryFS()
        mem.writebytes("obj.bin", DATA)
        reader = SparseReader(mem)
        with reader.session("obj.bin") as session:
            first = session.read_ranges([(0, 4)])
            second = session.read_ranges([(10, 14)])
            self.assertEqual(first.data[ByteRange(0, 4)], DATA[:4])
            self.assertEqual(second.data[ByteRange(10, 14)], DATA[10:14])

    def test_closed_session_rejects_reads(self):
        mem = MemoryFS()
        mem.writebytes("obj.bin", DATA)
        reader = SparseReader(mem)
        with reader.session("obj.bin") as session:
            pass
        with self.assertRaises(RuntimeError):
            session.read_ranges([(0, 1)])


class TestStagedVersionGuards(unittest.TestCase):
    def test_staged_digest_pinned_without_extra_source_read(self):
        zip_fs = _make_zip_fs(DATA)
        reader = SparseReader(zip_fs)
        try:
            with reader.session("obj.bin") as session:
                # Integer (coarse) archive timestamp requires a hash.
                self.assertIsInstance(session.version.modified, int)
                session.read_ranges([(0, 4)])
                pinned = session.version.digest
                self.assertIsNotNone(pinned)
                # A later read re-checks the source digest and matches.
                again = session.read_ranges([(8, 12)])
                self.assertEqual(again.data[ByteRange(8, 12)], DATA[8:12])
                self.assertEqual(session.version.digest, pinned)
        finally:
            zip_fs.close()

    def test_staged_source_change_rejected_on_second_read(self):
        zip_fs = _make_zip_fs(DATA)
        staging = TempFS()
        reader = SparseReader(zip_fs, staging_fs=staging)
        mem = MemoryFS()
        mem.writebytes("obj.bin", b"q" * len(DATA))
        try:
            with reader.session("obj.bin") as session:
                session.read_ranges([(0, 4)])
                # Simulate the source being swapped for same-length,
                # same-timestamp contents by pointing the zip at new data.
                new_zip = _make_zip_fs(b"q" * len(DATA))
                session._backend = new_zip
                with self.assertRaises(ResourceModified):
                    session.read_ranges([(8, 12)])
                new_zip.close()
        finally:
            zip_fs.close()
            staging.close()
