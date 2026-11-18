from __future__ import annotations

import io
from typing import BinaryIO

from dissect.util import lznt1
from dissect.util.stream import RunlistStream


class CompressedRunlistStream(RunlistStream):
    """Specialized :class:`~dissect.util.stream.RunlistStream` for reading NTFS compressed streams.

    Args:
        fh: The source file-like object.
        runlist: The runlist for this stream in block units.
        size: The size of the stream. This can be smaller than the total sum of blocks (to account for slack space).
        cluster_size: The size of a cluster in bytes.
        compression_unit: The compression unit of for this stream.
    """

    def __init__(
        self, fh: BinaryIO, runlist: list[tuple[int, int]], size: int, cluster_size: int, compression_unit: int
    ):
        # RunlistStream has block_size but we want to make cluster_size available to be more in line with NTFS naming
        self.cluster_size = cluster_size
        self.compression_unit = compression_unit
        self.compression_unit_size = cluster_size << compression_unit
        self.clusters_per_compression_unit = 1 << compression_unit

        self._cu_blocks = []
        # Compression units for which physical data was missing or corrupt. Sparse tails are
        # intentionally not recorded here.
        self.missing_units: set[int] = set()

        # First use the compression_unit_size to set the alignment of AlignedStream
        super().__init__(fh, runlist, size, self.compression_unit_size)
        # This also sets the block_size of RunlistStream, so reset that to the correct cluster_size
        self.block_size = cluster_size

    @property
    def runlist(self) -> list[tuple[int, int]]:
        return self._runlist

    @runlist.setter
    def runlist(self, runlist: list[tuple[int, int]]) -> None:
        self._runlist = runlist
        self._cu_blocks = []
        self.missing_units = set()

        runs = []
        remaining = self.clusters_per_compression_unit

        # Build a list of aligned CU blocks. A run can span multiple CU blocks, in which case it is split.
        for lcn, count in runlist:
            while count > 0:
                use = min(count, remaining)
                runs.append((lcn, use))

                remaining -= use
                count -= use
                if lcn is not None:
                    lcn += use

                if remaining == 0:
                    self._cu_blocks.append(runs)
                    runs = []
                    remaining = self.clusters_per_compression_unit

        # A trailing partial CU is still a valid CU. Any clusters or runs that are not present in the
        # runlist are treated as sparse tails when reading.
        if runs:
            self._cu_blocks.append(runs)

    def _read(self, offset: int, length: int) -> bytes:
        # Compressed data is split over compression units (typically 64KB, or 16 clusters).
        # Each compression unit is exactly this size and decompresses to this size.
        #
        # Aligned reads are handled by AlignedStream, but reads can also be misaligned or cross a CU
        # boundary. Only read the CUs that overlap the requested logical range and return exactly that
        # range, so callers can safely read small fragments of a compressed stream.
        if length == 0:
            return b""

        if self.size is not None and offset >= self.size:
            return b""

        end = offset + length if length is not None and length >= 0 else self.size
        if end is None:
            end = len(self._cu_blocks) * self.compression_unit_size
        if self.size is not None:
            end = min(end, self.size)
        if end <= offset:
            return b""

        cu_size = self.compression_unit_size
        read_list = []

        cu_block = offset // cu_size
        last_cu_block = (end - 1) // cu_size

        while cu_block <= last_cu_block:
            cu_offset = cu_block * cu_size
            start = max(offset, cu_offset) - cu_offset
            stop = min(end, cu_offset + cu_size) - cu_offset
            read_list.append(self._read_compression_unit(cu_block, start, stop))
            cu_block += 1

        return b"".join(read_list)

    def _read_compression_unit(self, cu_block: int, start: int, stop: int) -> bytes:
        """Read a logical range from a single compression unit.

        Args:
            cu_block: The compression unit to read from.
            start: The start offset within the compression unit.
            stop: The end offset within the compression unit.
        """
        cu_size = self.compression_unit_size
        expected = min(cu_size, self.size - cu_block * cu_size) if self.size is not None else cu_size
        start = min(start, expected)
        stop = min(stop, expected)

        if stop <= start:
            return b""

        runs = self._cu_blocks[cu_block] if cu_block < len(self._cu_blocks) else []

        if not any(lcn is not None for lcn, _ in runs):
            # A completely sparse CU, or a CU that is not covered by the runlist at all. Both are
            # legitimate sparse data and must not be confused with a failed physical read.
            return b"\x00" * (stop - start)

        if any(lcn is None for lcn, _ in runs):
            # If a run is followed by a sparse run within the same CU, the CU is compressed
            data = self._read_compressed(runs, expected)
            if data is None:
                # The physical data for this CU is missing or corrupt. Keep the CU addressable so
                # later, independently recoverable CUs are still returned, but don't fabricate data.
                self.missing_units.add(cu_block)
                return b"\x00" * (stop - start)

            return data[start:stop]

        data, complete = self._read_uncompressed(runs, start, stop)
        if not complete:
            self.missing_units.add(cu_block)

        return data

    def _read_compressed(self, runs: list[tuple[int | None, int]], expected: int) -> bytes | None:
        """Read and decompress a single compressed CU.

        Args:
            runs: The runs that make up this compression unit.
            expected: The amount of logical bytes this compression unit should contain.

        Returns:
            The decompressed data, or ``None`` if the physical data is incomplete or corrupt.
        """
        buf = []
        read_size = 0

        for lcn, run_count in runs:
            if lcn is None:
                continue

            run_len = run_count * self.cluster_size
            try:
                self._fh.seek(lcn * self.cluster_size)
                data = self._fh.read(run_len)
            except Exception:
                return None

            if len(data) != run_len:
                # A short read means a physical part of this CU is missing. Don't concatenate the
                # partial data with zero padding, as that would turn missing data into (corrupt)
                # compressed input and potentially destroy the rest of the stream.
                return None

            buf.append(data)
            read_size += run_len

            if read_size >= expected:
                # The last clusters of a CU can be a sparse tail. Only read the physical runs that
                # are needed for the logical data and let the sparse tail be implicit.
                break

        if not buf:
            return None

        try:
            # LZNT1 needs a small amount of lookahead padding at the end
            buf.append(b"\x00" * 64)
            data = lznt1.decompress(io.BytesIO(b"".join(buf)))[:expected]
        except Exception:
            return None

        if not data and expected:
            # The decompressor silently returns no data for input without a valid LZNT1 block
            return None

        return data.ljust(expected, b"\x00")

    def _read_uncompressed(self, runs: list[tuple[int | None, int]], start: int, stop: int) -> tuple[bytes, bool]:
        """Read a logical range from an uncompressed (or partially sparse) CU in the runlist.

        Returns:
            A tuple of the requested data and whether all physical data in the range was available.
        """
        read_list = []
        position = 0
        complete = True

        for lcn, run_count in runs:
            run_size = run_count * self.cluster_size
            run_end = position + run_size

            if run_end <= start:
                position = run_end
                continue

            if position >= stop:
                break

            read_start = max(start, position)
            read_stop = min(stop, run_end)
            read_len = read_stop - read_start

            if lcn is None:
                read_list.append(b"\x00" * read_len)
            else:
                try:
                    self._fh.seek(lcn * self.cluster_size + (read_start - position))
                    data = self._fh.read(read_len)
                except Exception:
                    data = b""

                if len(data) != read_len:
                    complete = False

                # A short read indicates physically missing data. Preserve the bytes that are
                # available and fill only the missing part with zeroes.
                read_list.append(data.ljust(read_len, b"\x00"))

            position = run_end

        if position < stop:
            # Clusters at the end of the CU that are not covered by the runlist are sparse.
            read_list.append(b"\x00" * (stop - position))

        return b"".join(read_list), complete
