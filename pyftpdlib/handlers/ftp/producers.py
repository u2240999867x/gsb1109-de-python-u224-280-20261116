# Copyright (C) 2007 Giampaolo Rodola' <g.rodola@gmail.com>.
# Use of this source code is governed by MIT license that can be
# found in the LICENSE file.

import io
import os

from pyftpdlib.exceptions import _FileReadWriteError

CR_BYTE = ord("\r")
LF_BYTE = ord("\n")

__all__ = [
    "BufferedIteratorProducer",
    "FileProducer",
    "TextResumeError",
    "ascii_wire_marker_to_local",
    "ascii_wire_marker_to_local_bytes",
]


class TextResumeError(ValueError):
    """Raised when a text (ASCII) transfer cannot be restarted at the
    marker supplied by the client.

    The marker of a REST command always refers to a position in the
    representation actually transmitted over the data connection (with
    CRLF line endings), not to an offset of the local file, which may
    store different line endings. Some markers do not identify a single
    local position; in that case the transfer must be rejected instead
    of seeking to an arbitrary byte offset.
    """


def _marker_result(local_index, byte, next_is_lf):
    """Map the local byte at ``local_index``, whose wire representation
    ends at the requested marker, to a resumable local offset.

    Return a (local_offset, pending_cr) tuple. Raise TextResumeError when
    the marker lands on the CR half of a CRLF sequence, which cannot be
    resumed without duplicating the CR or dropping the LF.
    """
    if byte == CR_BYTE:
        if next_is_lf:
            raise TextResumeError(
                "restart marker falls on the CR of a CRLF sequence"
            )
        # Standalone CR: resume right after it while preserving the
        # "previous chunk ended with CR" conversion state.
        return local_index + 1, True
    return local_index + 1, False


def ascii_wire_marker_to_local(file_obj, marker):
    """Translate a REST marker expressed in the ASCII wire representation
    into a local file offset at which a text download can be resumed.

    - (file) file_obj: a binary file[-like] object opened for reading;
      it is scanned incrementally so the whole file is not loaded into
      memory. The file position is left unspecified on return, hence
      callers must seek to the returned offset before reading.
    - (int) marker: restart position in the wire (CRLF) representation,
      i.e. the number of transmitted bytes the client already received.

    Return a (local_offset, pending_cr) tuple where ``pending_cr`` is the
    CR conversion state in which the resumed producer has to start.

    The mapping is the exact inverse of the ASCII data wrapper, so the
    bytes produced after a resume concatenated with the bytes already
    received by the client are identical to a single, uninterrupted
    transfer. Raise TextResumeError when ``marker`` does not identify a
    single local position.
    """
    if marker < 0:
        raise TextResumeError(f"invalid restart marker: {marker!r}")
    if marker == 0:
        return 0, False

    buffer_size = FileProducer.buffer_size
    base = 0
    wire_count = 0
    prev_cr = False
    chunk = file_obj.read(buffer_size)

    while chunk:
        chunk_len = len(chunk)
        for index, byte in enumerate(chunk):
            bare_lf = byte == LF_BYTE and not prev_cr
            if bare_lf:
                # The wrapper inserts a CR in front of the bare LF. The
                # inserted CR has no local byte of its own, hence no
                # local boundary can reproduce that marker.
                if wire_count + 1 == marker:
                    raise TextResumeError(
                        "restart marker falls on a CR inserted before a"
                        " bare LF"
                    )
                wire_count += 1
            wire_count += 1
            if wire_count == marker:
                local_index = base + index
                if byte == CR_BYTE and index + 1 == chunk_len:
                    # The CR may be followed by an LF delivered in the
                    # next read chunk; peek one byte to decide. Callers
                    # seek again before transferring, so the moved file
                    # position is irrelevant.
                    next_is_lf = file_obj.read(1) == b"\n"
                elif index + 1 < chunk_len:
                    next_is_lf = chunk[index + 1] == LF_BYTE
                else:
                    next_is_lf = False
                return _marker_result(local_index, byte, next_is_lf)
            prev_cr = byte == CR_BYTE
        base += chunk_len
        chunk = file_obj.read(buffer_size)

    if wire_count == marker:
        # Marker at the very end of the wire representation: nothing is
        # left to transmit, but the position is valid.
        return base, False
    raise TextResumeError(
        f"restart marker {marker} does not match any local file position"
    )


def ascii_wire_marker_to_local_bytes(data, marker):
    """Same as :func:`ascii_wire_marker_to_local` but accepting the file
    content as a bytes string instead of a file object.
    """
    return ascii_wire_marker_to_local(io.BytesIO(data), marker)


class FileProducer:
    """Producer wrapper for file[-like] objects."""

    buffer_size = 65536

    def __init__(self, file, type, resume_pending_cr=False):
        """Initialize the producer with a data_wrapper appropriate to TYPE.

        - (file) file: the file[-like] object.
        - (str) type: the current TYPE, 'a' (ASCII) or 'i' (binary).
        - (bool) resume_pending_cr: when True and TYPE is ASCII, the
          producer starts in the state where the previous chunk ended
          with a CR, preserving the conversion state across a restart
          when a CR and the following byte are split across read chunks.
        """
        self.file = file
        self.type = type
        self._prev_chunk_endswith_cr = resume_pending_cr
        if type == "a" and os.linesep != "\r\n":
            self._data_wrapper = self._posix_ascii_data_wrapper
        else:
            self._data_wrapper = None

    def _posix_ascii_data_wrapper(self, chunk):
        """The data wrapper used for sending data in ASCII mode on
        systems using a single line terminator, handling those cases
        where CRLF ('\r\n') gets delivered in two chunks.
        """
        chunk = bytearray(chunk)
        pos = 0
        if self._prev_chunk_endswith_cr and chunk.startswith(b"\n"):
            pos += 1
        while True:
            pos = chunk.find(b"\n", pos)
            if pos == -1:
                break
            # When pos == 0 the predecessor lives in the previous chunk;
            # the CRLF-across-chunks case was already handled above, so
            # this is a bare LF that needs a CR inserted.
            if pos == 0 or chunk[pos - 1] != CR_BYTE:
                chunk.insert(pos, CR_BYTE)
                pos += 1
            pos += 1
        self._prev_chunk_endswith_cr = chunk.endswith(b"\r")
        return chunk

    def more(self):
        """Attempt a chunk of data of size self.buffer_size."""
        try:
            data = self.file.read(self.buffer_size)
        except OSError as err:
            raise _FileReadWriteError(err) from err
        else:
            if self._data_wrapper is not None:
                data = self._data_wrapper(data)
            return data


class BufferedIteratorProducer:
    """Producer for iterator objects with buffer capabilities."""

    # how many times iterator.next() will be called before
    # returning some data
    loops = 20

    def __init__(self, iterator):
        self.iterator = iterator

    def more(self):
        """Attempt a chunk of data from iterator by calling
        its next() method different times.
        """
        buffer = []
        for _ in range(self.loops):
            try:
                buffer.append(next(self.iterator))
            except StopIteration:
                break
        return b"".join(buffer)
