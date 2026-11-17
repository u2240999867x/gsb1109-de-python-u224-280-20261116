"""Sparse byte-range reads across filesystem backends.

Different PyFilesystem backends expose streams with very different
seeking behaviour, so reading a set of sparse (non-contiguous) byte
ranges needs three distinct strategies:

* **direct range read** -- the stream supports real random access, so
  each range is reached with ``seek`` and exactly the requested bytes
  are pulled.  Used by `~fs.osfs.OSFS`, `~fs.memoryfs.MemoryFS` and
  `~fs.tarfs.ReadTarFS` opened on a seekable container.
* **sequential skip** -- the stream is not seekable, so a single stream
  is opened once, ranges are visited in offset order and the gaps are
  read and discarded.  Used by backends that only expose a forward
  stream, e.g. a `~fs.tarfs.ReadTarFS` fed from a pipe.
* **local staging** -- the cost model makes repeated seeks expensive or
  wrong (compressed members must be re-decompressed on every seek, as
  with `~fs.zipfs.ReadZipFS`), or the backend restarts a network
  transfer for every seek (`~fs.ftpfs.FTPFS`).  The object is copied
  once into a local `~fs.tempfs.TempFS` and the ranges are then served
  from the staged copy.

Every read records both the requested span and the number of bytes
actually pulled from the backend, and every read keeps hold of the
resource version it was based on.  A second read of the same object
re-checks that version: when the source changed in the meantime the
ranges are refused rather than stitched into a complete, but corrupt,
object.  An unchanged length is never treated as proof that the
contents are unchanged -- the modification time, or a content hash when
no modification time is available, must match as well.

"""

from __future__ import absolute_import, print_function, unicode_literals

import typing

import hashlib
import uuid
from collections import OrderedDict, namedtuple

from . import errors
from .constants import DEFAULT_CHUNK_SIZE
from .enums import Seek
from .ftpfs import FTPFS
from .memoryfs import MemoryFS
from .mountfs import MountFS
from .multifs import MultiFS
from .osfs import OSFS
from .tarfs import ReadTarFS
from .tempfs import TempFS
from .wrapfs import WrapFS
from .zipfs import ReadZipFS

if typing.TYPE_CHECKING:
    from typing import (
        Any,
        BinaryIO,
        Iterable,
        List,
        Optional,
        Sequence,
        Text,
        Tuple,
    )

    from .base import FS


__all__ = [
    "ByteRange",
    "ReadStrategy",
    "ReadBackend",
    "ResourceVersion",
    "RangeReadResult",
    "SparseReader",
    "SparseSession",
]


class ReadStrategy(object):
    """How a backend serves sparse byte ranges."""

    #: Real random access: ``seek`` to each range, read only the span.
    direct_range = "direct_range"
    #: One forward stream; gaps between ranges are read and discarded.
    sequential_skip = "sequential_skip"
    #: Copy the object to a local TempFS once, then read ranges locally.
    local_staging = "local_staging"


class ByteRange(namedtuple("ByteRange", ["start", "end"])):
    """A half-open byte range ``[start, end)``."""

    __slots__ = ()

    def __new__(cls, start, end):
        # type: (int, int) -> ByteRange
        if start < 0:
            raise ValueError(
                "range start must be non-negative, got {}".format(start)
            )
        if end < start:
            raise ValueError("range end {} precedes start {}".format(end, start))
        return super(ByteRange, cls).__new__(cls, start, end)

    @property
    def length(self):
        # type: () -> int
        return self.end - self.start

    @classmethod
    def coerce(cls, ranges):
        # type: (Iterable[Any]) -> List[ByteRange]
        coerced = []
        for range_ in ranges:
            if isinstance(range_, ByteRange):
                coerced.append(range_)
            else:
                start, end = range_
                coerced.append(cls(start, end))
        return coerced


class ReadBackend(namedtuple("ReadBackend", ["name", "strategy", "seekable", "note"])):
    """Classification of a backend for sparse reads.

    Attributes:
        name (str): Concrete backend class name the ranges are read from.
        strategy (str): One of the `ReadStrategy` constants.
        seekable (bool): Whether streams returned by ``openbin`` are
            genuinely seekable.
        note (str): Human readable justification for the classification.

    """

    __slots__ = ()


class ResourceVersion(namedtuple("ResourceVersion", ["size", "modified", "digest"])):
    """Immutable identity of a resource's contents.

    Attributes:
        size (int): Resource size in bytes.  Never sufficient on its own
            to prove two versions are equal.
        modified (float, int or None): Raw ``details.modified`` value,
            kept at the backend's native precision (sub-second floats for
            local backends, whole-second ints for archives/FTP).
        digest (str or None): SHA-256 content hash.  Filled in whenever
            the modification time is missing or only whole-second
            granularity is available, since two writes inside the same
            second would otherwise look identical.

    """

    __slots__ = ()


class RangeReadResult(
    namedtuple(
        "RangeReadResult",
        ["data", "requested", "source_bytes", "local_bytes", "strategy"],
    )
):
    """Outcome of reading one or more sparse ranges.

    Attributes:
        data (OrderedDict): Mapping of `ByteRange` to the bytes read, in
            the order the ranges were requested.
        requested (int): Number of useful bytes the caller asked for
            (the sum of the range lengths).
        source_bytes (int): Bytes physically pulled from the originating
            backend during this read.  For local staging this counts the
            full object copy once; a later read served from the cached
            stage reports ``0`` source bytes.
        local_bytes (int): Bytes read from the local staging area; zero
            for the direct and sequential strategies.
        strategy (str): The `ReadStrategy` that was used.

    """

    __slots__ = ()

    @property
    def actual(self):
        # type: () -> int
        """Total bytes read from any stream for this result."""
        return self.source_bytes + self.local_bytes

    @property
    def overhead(self):
        # type: () -> int
        """Bytes read beyond the requested spans."""
        return self.actual - self.requested


def _unwrap(fs, path):
    # type: (FS, Text) -> Tuple[FS, Text]
    """Resolve wrapping / mounting filesystems to a concrete backend."""
    seen = set()
    while id(fs) not in seen:
        seen.add(id(fs))
        if isinstance(fs, WrapFS):
            fs, path = fs.delegate_path(path)
            continue
        if isinstance(fs, MultiFS):
            delegate = fs._delegate(path)
            if delegate is None:
                raise errors.ResourceNotFound(path)
            fs = delegate
            continue
        if isinstance(fs, MountFS):
            fs, path = fs._delegate(path)
            continue
        break
    return fs, path


def _stream_is_seekable(stream):
    # type: (BinaryIO) -> bool
    seekable = getattr(stream, "seekable", None)
    if seekable is None:
        return False
    try:
        return bool(seekable())
    except (OSError, ValueError):
        return False


class SparseReader(object):
    """Read sparse byte ranges from a filesystem, backend by backend.

    Arguments:
        fs (FS): Filesystem to read from.
        chunk_size (int): Buffer size used when skipping gaps or
            staging an object locally.
        staging_fs (FS or None): Filesystem used for local staging.
            Defaults to a fresh auto-cleaning `~fs.tempfs.TempFS` per
            session.
        verify (str): Version check policy. ``"auto"`` (the default)
            trusts ``details.modified`` and falls back to a hash when
            the timestamp is missing; ``"hash"`` always hashes the
            contents.  Cross-read checks can not be disabled: mixing
            two versions into one object is always rejected.

    """

    def __init__(
        self,
        fs,  # type: FS
        chunk_size=DEFAULT_CHUNK_SIZE,  # type: int
        staging_fs=None,  # type: Optional[FS]
        verify="auto",  # type: Text
    ):
        if verify not in ("auto", "hash"):
            raise ValueError("verify must be one of 'auto' or 'hash'")
        self.fs = fs
        self.chunk_size = chunk_size
        self._staging_fs = staging_fs
        self.verify = verify

    def classify(self, path):
        # type: (Text) -> ReadBackend
        """Return the sparse-read classification for ``path``'s backend.

        The classification is based on the concrete backend the path
        resolves to, then confirmed by probing a real stream so an
        unexpected non-seekable container is never misclassified.

        """
        backend, backend_path = _unwrap(self.fs, path)
        strategy, note = self._static_strategy(backend)
        seekable = strategy != ReadStrategy.sequential_skip

        if strategy == ReadStrategy.direct_range:
            with backend.openbin(backend_path) as stream:
                seekable = _stream_is_seekable(stream)
            if not seekable:
                strategy = ReadStrategy.sequential_skip
                note = (
                    "stream reported as non-seekable; falling back to a "
                    "single forward read with discarded gaps"
                )

        return ReadBackend(
            name=type(backend).__name__,
            strategy=strategy,
            seekable=seekable,
            note=note,
        )

    @staticmethod
    def _static_strategy(backend):
        # type: (FS) -> Tuple[str, Text]
        if isinstance(backend, (OSFS, MemoryFS)):
            return (
                ReadStrategy.direct_range,
                "openbin returns a seekable local stream (file or BytesIO)",
            )
        if isinstance(backend, ReadTarFS):
            return (
                ReadStrategy.direct_range,
                "tar members are seekable when the tar container itself "
                "is seekable; a streamed container degrades to sequential "
                "skip after probing",
            )
        if isinstance(backend, ReadZipFS):
            return (
                ReadStrategy.local_staging,
                "zip members are compressed streams whose seek reopens and "
                "re-decompresses; stage once and read ranges locally",
            )
        if isinstance(backend, FTPFS):
            return (
                ReadStrategy.local_staging,
                "every seek closes the data connection and issues a fresh "
                "REST/RETR transfer; one staged download is cheaper than "
                "one transfer per range",
            )
        return (
            ReadStrategy.sequential_skip,
            "unknown backend; assuming a forward-only stream",
        )

    def read_ranges(self, path, ranges):
        # type: (Text, Iterable[Any]) -> RangeReadResult
        """Read several sparse ranges from ``path`` in one pass.

        The resource version is captured before the ranges are read and
        re-checked afterwards, so a file modified mid-read can never be
        silently assembled from mixed versions.

        """
        normalized = self._normalize(path, ranges)
        with self.session(path) as session:
            return session.read_ranges(normalized)

    def session(self, path):
        # type: (Text) -> SparseSession
        """Open a multi-read session pinned to one resource version."""
        return SparseSession(self, path)

    def _normalize(self, path, ranges):
        # type: (Text, Iterable[Any]) -> List[ByteRange]
        normalized = ByteRange.coerce(ranges)
        if not normalized:
            raise ValueError("at least one byte range is required")

        size = self.fs.getsize(path)
        for range_ in normalized:
            if range_.end > size:
                raise ValueError(
                    "range {} exceeds resource size {}".format(range_, size)
                )
        return normalized

    def _metadata_version(self, fs, path):
        # type: (FS, Text) -> ResourceVersion
        """Size and modification time only, never touching the contents."""
        info = fs.getinfo(path, namespaces=["details"])
        details = info.raw.get("details", {})
        return ResourceVersion(
            size=details.get("size"),
            modified=details.get("modified"),
            digest=None,
        )

    def _hash_required(self, version):
        # type: (ResourceVersion) -> bool
        """Whether a content hash is needed to trust this version.

        Whole-second (or coarser) timestamps -- archives and FTP only
        carry second precision -- and missing timestamps cannot
        distinguish two writes inside one second, so the contents are
        pinned with a SHA-256 hash instead.

        """
        modified = version.modified
        if self.verify == "hash" or modified is None:
            return True
        return float(modified).is_integer()

    def _version_of(self, fs, path):
        # type: (FS, Text) -> ResourceVersion
        """Metadata plus a content hash whenever the timestamp is weak."""
        version = self._metadata_version(fs, path)
        if self._hash_required(version):
            version = version._replace(digest=fs.hash(path, "sha256"))
        return version

    def _stage(self, backend, path):
        # type: (FS, Text) -> Tuple[FS, Text, int, bool, Text]
        # A unique suffix keeps two sessions for the same path on a
        # shared staging filesystem from reusing each other's bytes.
        token = hashlib.sha1(
            path.encode("utf-8") + uuid.uuid4().bytes
        ).hexdigest()
        if self._staging_fs is not None:
            staging_fs = self._staging_fs
            owns_staging = False
            staged_name = "staged-{}".format(token)
        else:
            staging_fs = TempFS(identifier="sparse-stage")
            owns_staging = True
            staged_name = "staged-{}".format(token)

        actual = 0
        hasher = hashlib.sha256()
        with backend.openbin(path) as source, staging_fs.openbin(
            staged_name, "wb"
        ) as target:
            while True:
                chunk = source.read(self.chunk_size)
                if not chunk:
                    break
                target.write(chunk)
                hasher.update(chunk)
                actual += len(chunk)
        return staging_fs, staged_name, actual, owns_staging, hasher.hexdigest()


class SparseSession(object):
    """A version-pinned series of sparse reads against one resource.

    Instances are produced by `SparseReader.session` and must be used as
    context managers.  Every read re-validates the resource version
    captured when the session opened, both before and after the bytes
    are pulled; any change raises `~fs.errors.ResourceModified` instead
    of returning bytes that could be stitched into a frankenstein
    object.

    """

    def __init__(self, reader, path):
        # type: (SparseReader, Text) -> None
        self._reader = reader
        self._outer_path = path
        self._backend, self._path = _unwrap(reader.fs, path)
        self.version = None  # type: Optional[ResourceVersion]
        self._closed = False
        self._staged = None  # type: Optional[Tuple[FS, Text, int, bool, Text]]
        self._classification = None  # type: Optional[ReadBackend]

    def _strategy(self):
        # type: () -> str
        if self._classification is None:
            self._classification = self._reader.classify(self._outer_path)
        return self._classification.strategy

    def __enter__(self):
        # type: () -> SparseSession
        strategy = self._reader._static_strategy(self._backend)[0]
        if strategy == ReadStrategy.local_staging:
            # The staging copy streams the whole object anyway, so its
            # SHA-256 is pinned during the first copy instead of paying
            # for a separate full read up front.
            self.version = self._reader._metadata_version(
                self._backend, self._path
            )
        else:
            self.version = self._reader._version_of(self._backend, self._path)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self._staged is not None:
            staging_fs, staged_name, _, owns_staging, _ = self._staged
            if owns_staging:
                staging_fs.close()
            else:
                try:
                    staging_fs.remove(staged_name)
                except errors.FSError:
                    pass
        self._staged = None
        self._closed = True
        return False

    def _guard_metadata(self):
        # type: () -> ResourceVersion
        """Cheap size/mtime check; returns the current metadata version."""
        if self._closed:
            raise RuntimeError("sparse session is closed")
        current = self._reader._metadata_version(self._backend, self._path)
        if (
            current.size != self.version.size
            or current.modified != self.version.modified
        ):
            # Size alone can never prove equality: same-length contents
            # may differ, so every metadata mismatch -- including an equal
            # size -- fails the check.
            raise errors.ResourceModified(
                path=self._outer_path,
                expected=self.version,
                observed=current,
            )
        return current

    def _guard(self):
        # type: () -> None
        """Full pre-read check: metadata, then a hash when mtime is weak."""
        current = self._guard_metadata()
        if self._reader._hash_required(current):
            digest = self._backend.hash(self._path, "sha256")
            if self.version.digest is None:
                self.version = self.version._replace(digest=digest)
            elif digest != self.version.digest:
                # Same size and timestamp, different contents: this is
                # exactly the case an unchanged length fails to catch.
                raise errors.ResourceModified(
                    path=self._outer_path,
                    expected=self.version,
                    observed=current._replace(digest=digest),
                )

    def read_ranges(self, ranges):
        # type: (Sequence[Any]) -> RangeReadResult
        normalized = ByteRange.coerce(ranges)
        if not normalized:
            raise ValueError("at least one byte range is required")
        pinned_size = self.version.size
        for range_ in normalized:
            if range_.end > pinned_size:
                raise ValueError(
                    "range {} exceeds resource size {}".format(
                        range_, pinned_size
                    )
                )

        strategy = self._strategy()

        if strategy == ReadStrategy.local_staging:
            # The staged copy is one self-consistent snapshot; only the
            # cheap metadata check runs beforehand and the digest pinned
            # during the copy protects every later read.
            self._guard_metadata()
            result = self._read_staged(normalized)
        else:
            # A single opened stream makes the ranges self-consistent;
            # the full pre-read guard rejects any change since the last
            # read before another byte may be stitched on.
            self._guard()
            if strategy == ReadStrategy.direct_range:
                result = self._read_direct(normalized)
            else:
                result = self._read_sequential(normalized)

        # Post-read metadata check catches truncation / replacement that
        # happened while the stream was open.  Content-only changes with
        # coarse timestamps are refused by the next read's full guard.
        self._guard_metadata()
        return result

    def _read_direct(self, ranges):
        # type: (Sequence[ByteRange]) -> RangeReadResult
        data = OrderedDict()  # type: OrderedDict[ByteRange, bytes]
        actual = 0
        with self._backend.openbin(self._path) as stream:
            for range_ in ranges:
                stream.seek(range_.start, Seek.set)
                payload = self._read_exact(stream, range_.length)
                data[range_] = payload
                actual += len(payload)
        return RangeReadResult(
            data=data,
            requested=sum(range_.length for range_ in ranges),
            source_bytes=actual,
            local_bytes=0,
            strategy=ReadStrategy.direct_range,
        )

    def _read_sequential(self, ranges):
        # type: (Sequence[ByteRange]) -> RangeReadResult
        # Keep the output in the order the caller requested, but walk
        # the forward stream through the ranges sorted by offset.
        data = OrderedDict(
            (range_, b"") for range_ in ranges
        )  # type: OrderedDict[ByteRange, bytes]
        actual = 0
        cursor = 0
        with self._backend.openbin(self._path) as stream:
            for range_ in sorted(ranges, key=lambda range_: range_.start):
                actual += self._discard(stream, range_.start - cursor)
                data[range_] = self._read_exact(stream, range_.length)
                actual += range_.length
                cursor = range_.end
        return RangeReadResult(
            data=data,
            requested=sum(range_.length for range_ in ranges),
            source_bytes=actual,
            local_bytes=0,
            strategy=ReadStrategy.sequential_skip,
        )

    def _read_staged(self, ranges):
        # type: (Sequence[ByteRange]) -> RangeReadResult
        if self._staged is None:
            self._staged = self._reader._stage(self._backend, self._path)
            source_bytes = self._staged[2]
            if self._reader._hash_required(self.version):
                # The digest was computed during the single full copy,
                # so pin it without touching the source a second time.
                self.version = self.version._replace(digest=self._staged[4])
        else:
            source_bytes = 0
            # The stage is only valid while the source is unchanged.
            # Coarse timestamps can hide a same-second rewrite, so the
            # pinned digest is recomputed and compared when required.
            if self.version.digest is not None:
                digest = self._backend.hash(self._path, "sha256")
                if digest != self.version.digest:
                    raise errors.ResourceModified(
                        path=self._outer_path,
                        expected=self.version,
                        observed=self.version._replace(digest=digest),
                    )

        staging_fs, staged_name, _, _, _ = self._staged
        data = OrderedDict()  # type: OrderedDict[ByteRange, bytes]
        local_bytes = 0
        with staging_fs.openbin(staged_name) as stream:
            for range_ in ranges:
                stream.seek(range_.start, Seek.set)
                payload = self._read_exact(stream, range_.length)
                data[range_] = payload
                local_bytes += len(payload)
        return RangeReadResult(
            data=data,
            requested=sum(range_.length for range_ in ranges),
            source_bytes=source_bytes,
            local_bytes=local_bytes,
            strategy=ReadStrategy.local_staging,
        )

    def _read_exact(self, stream, length):
        # type: (BinaryIO, int) -> bytes
        chunks = []
        remaining = length
        while remaining:
            chunk = stream.read(remaining)
            if not chunk:
                raise errors.OperationFailed(
                    path=self._outer_path,
                    msg="unexpected end of stream while reading sparse range",
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _discard(self, stream, length):
        # type: (BinaryIO, int) -> int
        discarded = 0
        remaining = length
        while remaining:
            chunk = stream.read(min(remaining, self._reader.chunk_size))
            if not chunk:
                raise errors.OperationFailed(
                    path=self._outer_path,
                    msg="unexpected end of stream while skipping sparse gap",
                )
            discarded += len(chunk)
            remaining -= len(chunk)
        return discarded
