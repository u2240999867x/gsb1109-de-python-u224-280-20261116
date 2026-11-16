######################################################################
#   John Holland <john@zoner.org>
# All rights reserved.
#
# This software is licensed as described in the file LICENSE.txt, which
# you should have received as part of this distribution.
#
######################################################################

"""
Incremental, byte-oriented interface to an X12 data stream.

A receiving station may concatenate several interchanges in one byte
stream; each ISA re-declares the delimiters in force until its matching
IEA, and delimiter settings are never carried over to the next
interchange.  BIN segment payloads are length-prefixed (BIN01, counted
in bytes) and may themselves contain delimiter characters, newlines, or
the text "ISA"; they are consumed by byte count, never by scanning for
terminators or segment names.

Feed chunks with :meth:`RawX12Stream.feed`; each call returns the
segments completed by the data received so far, together with their raw
byte range in the stream and the delimiters declared by their
interchange.  Input stopping in the middle of an ISA header, a BIN
length field, or a BIN payload is held as incomplete state until more
data arrives; a confirmed segment is delivered exactly once.
"""

from __future__ import annotations

from typing import NamedTuple

# Intrapackage imports
import pyx12.errors
from pyx12.rawx12file import ISA_LEN, MAX_SEGMENT_LEN

_EOL_BYTES = b"\r\n"
# The BIN01 length field is decimal; anything longer is corrupt, not
# "still incomplete".
_MAX_BIN_LEN_FIELD = 20


class RawSegment(NamedTuple):
    """
    One segment read from the stream.

    ``segment`` is the segment text decoded as latin-1 (one character
    per stream byte), without the segment terminator or any leading
    CR/LF.  ``start``/``end`` are the byte offsets of the segment in the
    stream; ``end`` is one past the segment terminator, so
    ``stream[start:end]`` reproduces the raw segment including its
    terminator.  The remaining fields are the delimiters declared by the
    ISA of the interchange this segment belongs to.
    """

    segment: str
    start: int
    end: int
    seg_term: str
    ele_term: str
    subele_term: str
    repetition_term: str | None


class RawX12Stream:
    """
    Chunk-fed reader for a byte stream of concatenated X12 interchanges.
    """

    icvn: str | None

    def __init__(self) -> None:
        """
        Initialize the incremental X12 stream reader.
        """
        self._buf = bytearray()
        self._offset = 0  # absolute stream offset of self._buf[0]
        self._in_interchange = False
        self._closed = False
        self.icvn = None
        self._seg_term: bytes | None = None
        self._ele_term: bytes | None = None
        self._subele_term: bytes | None = None
        self._repetition_term: bytes | None = None

    @property
    def seg_term(self) -> str | None:
        return self._decode(self._seg_term)

    @property
    def ele_term(self) -> str | None:
        return self._decode(self._ele_term)

    @property
    def subele_term(self) -> str | None:
        return self._decode(self._subele_term)

    @property
    def repetition_term(self) -> str | None:
        return self._decode(self._repetition_term)

    @staticmethod
    def _decode(term: bytes | None) -> str | None:
        return term.decode("latin-1") if term is not None else None

    def get_term(self) -> tuple[str | None, str | None, str | None, str, str | None]:
        """
        Get the terminators declared by the current interchange

        :rtype: tuple(string, string, string, string)
        """
        return (self.seg_term, self.ele_term, self.subele_term, "\n", self.repetition_term)

    def feed(self, data: bytes | bytearray | memoryview | str) -> list[RawSegment]:
        """
        Feed one chunk of the stream; return the segments it completes.

        :param data: next chunk of the stream; str chunks are encoded as
            latin-1 so one character stays one byte
        :return: segments completed by the data received so far, in
            stream order; empty while input stops mid-ISA, mid BIN
            length field, or mid BIN payload
        """
        if self._closed:
            raise pyx12.errors.X12Error("Cannot feed a closed RawX12Stream")
        if isinstance(data, str):
            data = data.encode("latin-1")
        self._buf += bytes(data)
        return self._drain()

    def close(self) -> None:
        """
        Signal end of stream.  Raises X12Error if the stream stops in
        the middle of an interchange, an ISA header, a BIN length field,
        or a BIN payload.  Idempotent.
        """
        if self._closed:
            return
        self._closed = True
        self._skip_eol()
        if self._in_interchange:
            err_str = "Premature EOF: interchange is not terminated"
            raise pyx12.errors.X12Error(err_str)
        if self._buf:
            err_str = "Premature EOF: ISA header is only %i of %i bytes" % (
                len(self._buf),
                ISA_LEN,
            )
            raise pyx12.errors.X12Error(err_str)

    def _drain(self) -> list[RawSegment]:
        out: list[RawSegment] = []
        while True:
            self._skip_eol()
            if not self._buf:
                break
            if not self._in_interchange:
                seg = self._parse_isa()
            elif self._buf[:3] == b"BIN" and len(self._buf) < 4:
                # Need one more byte to tell BIN from a longer segment id
                break
            elif self._buf[:3] == b"BIN" and bytes(self._buf[3:4]) == self._ele_term:
                seg = self._try_bin()
            elif bytes(self._buf[:1]) == self._seg_term:
                # Blank segment between terminators
                del self._buf[:1]
                self._offset += 1
                continue
            else:
                seg = self._try_segment()
            if seg is None:
                break
            out.append(seg)
        return out

    def _skip_eol(self) -> None:
        skipped = 0
        for byte in self._buf:
            if byte not in _EOL_BYTES:
                break
            skipped += 1
        if skipped:
            del self._buf[:skipped]
            self._offset += skipped

    def _parse_isa(self) -> RawSegment | None:
        if len(self._buf) < ISA_LEN:
            head = bytes(self._buf[:3])
            if head != b"ISA"[: len(head)]:
                err_str = "Interchange does not begin with 'ISA': %s" % head.decode(
                    "latin-1", "replace"
                )
                raise pyx12.errors.X12Error(err_str)
            # Incomplete ISA header; wait for the rest of it
            return None
        line = bytes(self._buf[:ISA_LEN])
        if line[:3] != b"ISA":
            err_str = "Interchange does not begin with 'ISA': %s" % line[:3].decode(
                "latin-1", "replace"
            )
            raise pyx12.errors.X12Error(err_str)
        icvn = line[84:89].decode("latin-1")
        if icvn not in ("00401", "00501"):
            err_str = "ISA Interchange Control Version Number is unknown: %s for %s" % (
                icvn,
                line.decode("latin-1"),
            )
            raise pyx12.errors.X12Error(err_str)
        self.icvn = icvn
        self._seg_term = line[105:106]
        self._ele_term = line[3:4]
        self._subele_term = line[104:105]
        self._repetition_term = line[82:83] if icvn == "00501" else None
        self._in_interchange = True
        return self._emit(ISA_LEN - 1)

    def _try_segment(self) -> RawSegment | None:
        idx = self._buf.find(self._seg_term)  # type: ignore[arg-type]
        if idx == -1:
            if len(self._buf) > MAX_SEGMENT_LEN:
                err_str = "No segment terminator %r within %i bytes" % (
                    self.seg_term,
                    MAX_SEGMENT_LEN,
                )
                raise pyx12.errors.X12Error(err_str)
            return None
        return self._emit(idx)

    def _try_bin(self) -> RawSegment | None:
        # Buffer starts with BIN<ele_term>; BIN01 is the byte count of
        # the BIN02 payload that follows the next element terminator.
        len_start = 4
        len_end = self._buf.find(self._ele_term, len_start)  # type: ignore[arg-type]
        if len_end == -1:
            if len(self._buf) - len_start > _MAX_BIN_LEN_FIELD:
                err_str = "BIN segment has an invalid length field"
                raise pyx12.errors.X12Error(err_str)
            return None
        len_bytes = bytes(self._buf[len_start:len_end])
        if not len_bytes.isdigit():
            err_str = "BIN segment has a non-numeric length: %s" % len_bytes.decode(
                "latin-1", "replace"
            )
            raise pyx12.errors.X12Error(err_str)
        length = int(len_bytes)
        payload_end = len_end + 1 + length
        if len(self._buf) <= payload_end:
            # Incomplete payload (or missing terminator); wait for more
            return None
        if bytes(self._buf[payload_end : payload_end + 1]) != self._seg_term:
            err_str = "BIN payload of %i bytes is not followed by the segment terminator %r" % (
                length,
                self.seg_term,
            )
            raise pyx12.errors.X12Error(err_str)
        return self._emit(payload_end)

    def _emit(self, term_idx: int) -> RawSegment:
        # Segment content is buffer[:term_idx]; terminator at term_idx
        content = bytes(self._buf[:term_idx])
        seg = RawSegment(
            segment=content.decode("latin-1"),
            start=self._offset,
            end=self._offset + term_idx + 1,
            seg_term=self.seg_term,  # type: ignore[arg-type]
            ele_term=self.ele_term,  # type: ignore[arg-type]
            subele_term=self.subele_term,  # type: ignore[arg-type]
            repetition_term=self.repetition_term,
        )
        del self._buf[: term_idx + 1]
        self._offset += term_idx + 1
        if content == b"IEA" or content.startswith(b"IEA" + self._ele_term):  # type: ignore[operator]
            # Delimiters are declared per interchange; the next ISA
            # starts from a clean slate.
            self._in_interchange = False
            self.icvn = None
            self._seg_term = None
            self._ele_term = None
            self._subele_term = None
            self._repetition_term = None
        return seg
