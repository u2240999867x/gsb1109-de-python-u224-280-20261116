from __future__ import annotations

import io

from dissect.ntfs.stream import CompressedRunlistStream

CLUSTER_SIZE = 4096
COMPRESSION_UNIT = 4
COMPRESSION_UNIT_SIZE = CLUSTER_SIZE << COMPRESSION_UNIT


class TrackingStream(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.reads = []

    def read(self, size: int = -1) -> bytes:
        self.reads.append((self.tell(), size))
        return super().read(size)


class FaultyStream(io.BytesIO):
    def __init__(self, data: bytes, broken: set[int]):
        super().__init__(data)
        self.broken = broken

    def read(self, size: int = -1) -> bytes:
        if self.tell() in self.broken:
            raise EOFError("physical data missing")
        return super().read(size)


def test_compressed_stream_small_read() -> None:
    # Three uncompressed CUs with distinct contents
    data = b"A" * COMPRESSION_UNIT_SIZE + b"B" * COMPRESSION_UNIT_SIZE + b"C" * COMPRESSION_UNIT_SIZE
    fh = TrackingStream(data)
    stream = CompressedRunlistStream(fh, [(0, 48)], len(data), CLUSTER_SIZE, COMPRESSION_UNIT)

    stream.seek(COMPRESSION_UNIT_SIZE + 10)
    assert stream.read(5) == b"BBBBB"
    # Only the physical data for the second CU should be read
    assert fh.reads == [(COMPRESSION_UNIT_SIZE, COMPRESSION_UNIT_SIZE)]


def test_compressed_stream_cross_cu_read() -> None:
    data = b"A" * COMPRESSION_UNIT_SIZE + b"B" * COMPRESSION_UNIT_SIZE + b"C" * COMPRESSION_UNIT_SIZE
    stream = CompressedRunlistStream(io.BytesIO(data), [(0, 48)], len(data), CLUSTER_SIZE, COMPRESSION_UNIT)

    offset = COMPRESSION_UNIT_SIZE - 10
    assert stream.readoffset(offset, 20) == b"A" * 10 + b"B" * 10


def test_compressed_stream_sparse_tail() -> None:
    data = b"A" * COMPRESSION_UNIT_SIZE
    stream = CompressedRunlistStream(io.BytesIO(data), [(0, 16)], COMPRESSION_UNIT_SIZE * 3, CLUSTER_SIZE, 4)

    assert stream.read() == data + b"\x00" * (COMPRESSION_UNIT_SIZE * 2)
    assert not stream.missing_units


def test_compressed_stream_missing_unit() -> None:
    data = b"A" * COMPRESSION_UNIT_SIZE + b"B" * COMPRESSION_UNIT_SIZE + b"C" * COMPRESSION_UNIT_SIZE
    fh = FaultyStream(data, {COMPRESSION_UNIT_SIZE})
    stream = CompressedRunlistStream(fh, [(0, 48)], len(data), CLUSTER_SIZE, COMPRESSION_UNIT)

    result = stream.read()
    assert result[:COMPRESSION_UNIT_SIZE] == b"A" * COMPRESSION_UNIT_SIZE
    assert result[COMPRESSION_UNIT_SIZE : COMPRESSION_UNIT_SIZE * 2] == b"\x00" * COMPRESSION_UNIT_SIZE
    assert result[COMPRESSION_UNIT_SIZE * 2 :] == b"C" * COMPRESSION_UNIT_SIZE
    assert stream.missing_units == {1}
