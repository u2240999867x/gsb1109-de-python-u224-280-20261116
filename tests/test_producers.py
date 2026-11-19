# Copyright (C) 2007 Giampaolo Rodola' <g.rodola@gmail.com>.
# Use of this source code is governed by a MIT license that can be
# found in the LICENSE file.

import io
import random

import pytest

from pyftpdlib.handlers.ftp.producers import FileProducer
from pyftpdlib.handlers.ftp.producers import TextResumeError
from pyftpdlib.handlers.ftp.producers import ascii_wire_marker_to_local
from pyftpdlib.handlers.ftp.producers import ascii_wire_marker_to_local_bytes

from . import POSIX
from . import PyftpdlibTestCase

CR = 0x0D
LF = 0x0A


def wire_reference(data):
    """Independent reference implementation of the local -> wire
    (CRLF) translation.
    """
    out = bytearray()
    prev_cr = False
    for byte in data:
        if byte == LF and not prev_cr:
            out.append(CR)
        out.append(byte)
        prev_cr = byte == CR
    return bytes(out)


def ambiguous_markers(data):
    """Markers that do not identify a single local position."""
    bad = set()
    wire_pos = 0
    prev_cr = False
    for index, byte in enumerate(data):
        bare_lf = byte == LF and not prev_cr
        if bare_lf:
            bad.add(wire_pos + 1)  # inserted CR has no local byte
            wire_pos += 1
        wire_pos += 1
        if byte == CR and index + 1 < len(data) and data[index + 1] == LF:
            bad.add(wire_pos)  # CR half of a CRLF
        prev_cr = byte == CR
    return bad


def produce(file_obj, pending_cr, buffer_size):
    old_size = FileProducer.buffer_size
    FileProducer.buffer_size = buffer_size
    try:
        producer = FileProducer(
            file_obj, "a", resume_pending_cr=pending_cr
        )
        chunks = []
        while True:
            chunk = producer.more()
            if not chunk:
                break
            chunks.append(bytes(chunk))
        return b"".join(chunks)
    finally:
        FileProducer.buffer_size = old_size


@pytest.mark.skipif(not POSIX, reason="ASCII translation only on POSIX")
class TestAsciiMarkerMapping(PyftpdlibTestCase):
    """Tests for the translation of REST markers between the wire
    (CRLF) representation and the local file.
    """

    SMALL_CASES = [
        b"",
        b"a",
        b"\n",
        b"\r",
        b"\r\n",
        b"a\nb",
        b"a\r\nb",
        b"a\rb",
        b"\r\r\n",
        b"\n\n",
        b"\r\n\r\n",
        b"a\n",
        b"a\r",
        b"\n\r\nx",
        b"abc\r\nDEF\nghi\rjkl\n",
        b"\r" * 30 + b"\n",
        b"\n" * 30,
        b"ab\r\n" * 20,
    ]

    LARGE_CASES = [
        b"z" * 65535 + b"\r\n",  # CR exactly at a 64K chunk edge
        b"q" * 65536 + b"\r\n",
        b"x" * 70000 + b"\n" + b"y" * 100,
    ]

    def test_zero_marker(self):
        offset, pending_cr = ascii_wire_marker_to_local_bytes(b"abc\n", 0)
        assert offset == 0
        assert pending_cr is False

    def test_negative_marker_rejected(self):
        with pytest.raises(TextResumeError):
            ascii_wire_marker_to_local_bytes(b"abc", -1)

    def test_marker_beyond_wire_rejected(self):
        data = b"abc\n"
        wire_len = len(wire_reference(data))
        with pytest.raises(TextResumeError):
            ascii_wire_marker_to_local_bytes(data, wire_len + 1)
        with pytest.raises(TextResumeError):
            ascii_wire_marker_to_local_bytes(data, wire_len + 99)

    def test_end_of_wire_marker(self):
        data = b"abc\ndef"
        wire_len = len(wire_reference(data))
        offset, _ = ascii_wire_marker_to_local_bytes(data, wire_len)
        assert offset == len(data)

    def test_file_and_bytes_variants_agree(self):
        data = b"abc\r\ndef\nghi\rj"
        wire = wire_reference(data)
        for marker in range(len(wire) + 1):
            try:
                expected = ascii_wire_marker_to_local_bytes(data, marker)
            except TextResumeError:
                with pytest.raises(TextResumeError):
                    ascii_wire_marker_to_local(io.BytesIO(data), marker)
            else:
                assert ascii_wire_marker_to_local(
                    io.BytesIO(data), marker
                ) == expected

    def test_resume_round_trip_small(self):
        # Exhaustively probe every marker of small files at every
        # relevant chunk size, including chunk size 1.
        rng = random.Random(4242)
        cases = self.SMALL_CASES + [
            bytes(
                rng.choice((0x61, CR, LF))
                for _ in range(rng.randrange(1, 30))
            )
            for _ in range(12)
        ]
        for buffer_size in (1, 3, 64):
            for data in cases:
                with self.subTest(data=data, buffer_size=buffer_size):
                    self._assert_round_trip(data, buffer_size)

    def test_resume_round_trip_large(self):
        # Large files: probe a bounded sample of markers plus all the
        # ambiguous ones, exercising the 64K chunk boundary handling.
        # Chunk size 1 is covered exhaustively by the small-file tests.
        rng = random.Random(991)
        cases = self.LARGE_CASES + [
            bytes(rng.choice((0x61, CR, LF)) for _ in range(4000))
            for _ in range(2)
        ]
        for data in cases:
            wire = wire_reference(data)
            markers = {0, len(wire)}
            markers.update(rng.sample(range(len(wire) + 1), 12))
            markers.update(ambiguous_markers(data))
            bad = ambiguous_markers(data)
            for marker in sorted(markers):
                with self.subTest(marker=marker, buffer_size=65536):
                    if marker in bad:
                        with self.assertRaises(TextResumeError):
                            ascii_wire_marker_to_local_bytes(data, marker)
                        continue
                    offset, pending_cr = ascii_wire_marker_to_local_bytes(
                        data, marker
                    )
                    tail = produce(
                        io.BytesIO(data[offset:]), pending_cr, 65536
                    )
                    self.assertEqual(wire[:marker] + tail, wire)
                    self.assertEqual(len(tail), len(wire) - marker)

    def _assert_round_trip(self, data, buffer_size):
        wire = wire_reference(data)
        bad = ambiguous_markers(data)
        for marker in range(len(wire) + 1):
            if marker in bad:
                with self.assertRaises(TextResumeError):
                    ascii_wire_marker_to_local_bytes(data, marker)
                continue
            offset, pending_cr = ascii_wire_marker_to_local_bytes(data, marker)
            tail = produce(
                io.BytesIO(data[offset:]), pending_cr, buffer_size
            )
            self.assertEqual(wire[:marker] + tail, wire)
            self.assertEqual(len(tail), len(wire) - marker)

    def test_inserted_cr_marker_rejected(self):
        # Local "a\nb": wire is "a\r\nb"; marker 2 lands on the inserted
        # CR which has no corresponding local byte.
        data = b"a\nb"
        with pytest.raises(TextResumeError, match="inserted before a bare LF"):
            ascii_wire_marker_to_local_bytes(data, 2)

    def test_crlf_cr_half_rejected(self):
        # Local "a\r\nb": marker 2 lands on the CR half of the CRLF.
        data = b"a\r\nb"
        with pytest.raises(TextResumeError, match="CR of a CRLF sequence"):
            ascii_wire_marker_to_local_bytes(data, 2)

    def test_standalone_cr_preserves_state(self):
        # A standalone CR (next byte is not LF) resumes right after it
        # while keeping the "previous chunk ended with CR" state.
        data = b"a\rb\n"
        wire = wire_reference(data)
        offset, pending_cr = ascii_wire_marker_to_local_bytes(data, 2)
        assert offset == 2
        assert pending_cr is True
        tail = produce(io.BytesIO(data[offset:]), pending_cr, 1)
        assert wire[:2] + tail == wire

    def test_repeated_recovery_matches_single_transfer(self):
        # Interrupting one transfer at several resumable markers and
        # resuming each time must reconstruct the uninterrupted stream.
        rng = random.Random(7)
        for _ in range(30):
            data = bytes(
                rng.choice((0x61, CR, LF))
                for _ in range(rng.randrange(1, 200))
            )
            wire = wire_reference(data)
            buffer_size = rng.choice((1, 2, 3, 7, 64))
            candidates = sorted(
                {0, len(wire)}
                | set(rng.sample(range(len(wire) + 1), min(15, len(wire))))
            )
            breakpoints = [0]
            for marker in candidates[1:]:
                try:
                    ascii_wire_marker_to_local_bytes(data, marker)
                except TextResumeError:
                    continue
                breakpoints.append(marker)
            assembled = bytearray()
            for start, end in zip(
                breakpoints, breakpoints[1:] + [None]
            ):
                offset, pending_cr = ascii_wire_marker_to_local_bytes(
                    data, start
                )
                tail = produce(
                    io.BytesIO(data[offset:]), pending_cr, buffer_size
                )
                if end is None:
                    assembled += tail
                else:
                    assembled += tail[: end - start]
            assert bytes(assembled) == wire


class TestFileProducer(PyftpdlibTestCase):
    def test_binary_mode_does_not_translate(self):
        data = b"a\nb\r\nc"
        producer = FileProducer(io.BytesIO(data), "i")
        assert producer._data_wrapper is None
        assert producer.more() == data

    def test_ascii_crlf_across_chunks(self):
        # A CR and its following LF delivered in two different read
        # chunks must not produce a duplicated CR.
        data = b"abc\r\ndef"
        assert produce(io.BytesIO(data), False, 4) == b"abc\r\ndef"

    def test_bare_lf_at_chunk_start(self):
        # Regression: a chunk starting with a bare LF used to compare
        # against the last byte of the chunk instead of the previous
        # chunk's final byte.
        data = b"abc\ndef"
        for size in (1, 2, 3, 4):
            assert produce(io.BytesIO(data), False, size) == b"abc\r\ndef"

    def test_pending_cr_completes_crlf(self):
        # Producer starting in the pending-CR state, first byte LF.
        data = b"\nrest"
        old_size = FileProducer.buffer_size
        FileProducer.buffer_size = 4
        try:
            producer = FileProducer(
                io.BytesIO(data), "a", resume_pending_cr=True
            )
            chunks = []
            while True:
                chunk = producer.more()
                if not chunk:
                    break
                chunks.append(bytes(chunk))
            assert b"".join(chunks) == b"\nrest"
        finally:
            FileProducer.buffer_size = old_size
