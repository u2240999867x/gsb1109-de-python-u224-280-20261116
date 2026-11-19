# Copyright (C) 2007 Giampaolo Rodola' <g.rodola@gmail.com>.
# Use of this source code is governed by MIT license that can be
# found in the LICENSE file.

"""Upload quota accounting.

This module implements a queryable quota ledger limiting the actual
backend occupancy of each data space (typically a user home directory)
on the receiving server.

The ledger tracks three distinct buckets, following the occupancy
rules injected by the caller:

- confirmed: bytes whose occupancy on the backend has been confirmed
  (settled uploads).
- reserved: bytes pre-allocated by in-flight reservations (e.g. ALLO)
  which are not stored yet.
- unknown: growth of in-flight uploads whose final size is not known
  yet (bytes spent but not reconciled with the backend).

Reservations and spends are serialized through a lock, so concurrent
sessions can never spend the same remaining quota twice.  Bytes
overwriting an existing range are not billed twice: only the growth
beyond the previous occupancy is charged.  Sparse or shared data is
discounted only when the backend occupancy rules can prove it (e.g.
via st_blocks).  Bytes actually left behind by an interrupted upload
remain billed, and a failed reclaim never releases quota early.  Each
upload settles independently: a committed upload is never rolled back
because another upload failed.

Backends whose occupancy rules cannot provide the required atomic
guarantees must explicitly refuse strong quota enforcement: creating
a QuotaLedger with strong=True and such rules raises
QuotaNotSupported.
"""

import errno
import os
import threading
from collections import namedtuple

from .exceptions import FilesystemError

__all__ = [
    "BlockOccupancyRules",
    "LogicalOccupancyRules",
    "OccupancyRules",
    "QuotaError",
    "QuotaExceeded",
    "QuotaLedger",
    "QuotaManager",
    "QuotaNotSupported",
    "QuotaStats",
    "QuotaUploadFile",
]


class QuotaError(Exception):
    """Base class for quota related errors."""


class QuotaExceeded(QuotaError):
    """Raised when a reservation or spend would exceed the quota."""


class QuotaNotSupported(QuotaError):
    """Raised when strong quota enforcement is requested for a backend
    whose occupancy rules cannot provide the required atomic
    guarantees."""


QuotaStats = namedtuple(
    "QuotaStats", ["confirmed", "reserved", "unknown", "limit", "available"]
)


class OccupancyRules:
    """Base class describing how a backend accounts for the bytes
    actually occupied by a file.

    Class attributes:

     - (bool) atomic_reservation: whether the backend can guarantee
       that the ledger's check-and-spend sequence is atomic with
       respect to the actual storage.  Backends for which this is
       False (e.g. remote storage without atomic primitives) refuse
       strong quota enforcement.

     - (bool) proves_sparse: whether measure() can prove sparse or
       shared (e.g. reflinked) data, in which case only the proven
       occupied bytes are billed instead of the logical size.
    """

    atomic_reservation = False
    proves_sparse = False

    def measure(self, fs, path):
        """Return the bytes actually occupied by path on the backend,
        or None if the backend cannot prove it (e.g. the path does not
        exist or cannot be stat'ed).
        """
        raise NotImplementedError


class LogicalOccupancyRules(OccupancyRules):
    """Occupancy rules billing the logical file size (st_size).

    Cannot prove sparse or shared data, so no deduction is ever
    applied.  Atomicity is guaranteed as long as a single server
    process accounts for the data space.
    """

    atomic_reservation = True
    proves_sparse = False

    def measure(self, fs, path):
        try:
            return fs.getsize(path)
        except (OSError, FilesystemError):
            return None


class BlockOccupancyRules(OccupancyRules):
    """Occupancy rules billing the blocks reported by the backend
    (st_blocks * 512 on POSIX), falling back to the logical size when
    the backend cannot report allocated blocks.

    Sparse or shared data is discounted only when the backend proves
    it via st_blocks.
    """

    atomic_reservation = True
    proves_sparse = True

    def measure(self, fs, path):
        try:
            st = fs.stat(path)
        except (OSError, FilesystemError):
            return None
        blocks = getattr(st, "st_blocks", None)
        if blocks is None:
            # backend cannot prove allocation: bill the logical size
            return st.st_size
        return blocks * 512


class _Reservation:
    """An in-flight reservation of quota bytes (e.g. via ALLO)."""

    __slots__ = ("ledger", "amount", "remaining", "active")

    def __init__(self, ledger, amount):
        self.ledger = ledger
        self.amount = amount
        self.remaining = amount
        self.active = True

    def cancel(self):
        """Release the unspent part of this reservation."""
        self.ledger.cancel_reservation(self)

    def __repr__(self):
        return f"<quota-reservation amount={self.amount}>"


class _UploadContext:
    """Per-upload accounting state, settled exactly once."""

    __slots__ = (
        "ledger",
        "fs",
        "path",
        "baseline_occupancy",
        "baseline_size",
        "reservation",
        "billed",
        "settled",
    )

    def __init__(
        self, ledger, fs, path, baseline_occupancy, baseline_size, reservation
    ):
        self.ledger = ledger
        self.fs = fs
        self.path = path
        self.baseline_occupancy = baseline_occupancy
        self.baseline_size = baseline_size
        self.reservation = reservation
        self.billed = 0
        self.settled = False


class QuotaLedger:
    """A queryable quota ledger for a single data space.

     - (int) limit: maximum number of bytes the data space may occupy.
     - (instance) rules: the OccupancyRules deciding how backend
       occupancy is measured.
     - (bool) strong: when True (default) require the occupancy rules
       to provide atomic reservation guarantees, else raise
       QuotaNotSupported.
    """

    def __init__(self, limit, rules, strong=True):
        if limit < 0:
            raise ValueError("limit must be >= 0")
        if strong and not rules.atomic_reservation:
            raise QuotaNotSupported(
                "backend occupancy rules cannot guarantee atomic "
                "reservations; refusing to enable strong quota"
            )
        self.limit = limit
        self.rules = rules
        self.strong = strong
        self._confirmed = 0
        self._reserved = 0
        self._unknown = 0
        self._lock = threading.Lock()

    def __repr__(self):
        return (
            f"<{self.__class__.__name__} confirmed={self._confirmed} "
            f"reserved={self._reserved} unknown={self._unknown} "
            f"limit={self.limit}>"
        )

    def _available_unlocked(self):
        return (
            self.limit - self._confirmed - self._reserved - self._unknown
        )

    def stats(self):
        """Return a QuotaStats snapshot of the ledger."""
        with self._lock:
            return QuotaStats(
                confirmed=self._confirmed,
                reserved=self._reserved,
                unknown=self._unknown,
                limit=self.limit,
                available=self._available_unlocked(),
            )

    # --- reservations

    def reserve(self, amount):
        """Atomically reserve amount bytes, returning a _Reservation.
        Raise QuotaExceeded if the remaining quota is not enough.
        """
        if amount < 0:
            raise ValueError("amount must be >= 0")
        with self._lock:
            if amount > self._available_unlocked():
                raise QuotaExceeded(
                    f"cannot reserve {amount} bytes; "
                    f"{self._available_unlocked()} available"
                )
            self._reserved += amount
            return _Reservation(self, amount)

    def cancel_reservation(self, reservation):
        """Release the unspent part of a reservation."""
        with self._lock:
            if reservation.active:
                self._reserved -= reservation.remaining
                reservation.remaining = 0
                reservation.active = False

    # --- uploads

    def begin_upload(
        self, fs, path, baseline_occupancy, baseline_size, reservation=None
    ):
        """Start accounting for an upload against path.

         - (int) baseline_occupancy: backend-proven occupied bytes of
           the file before the upload (0 if it did not exist or they
           were reclaimed).
         - (int) baseline_size: logical size of the file before the
           upload; bytes overwriting the range below it are not billed
           again.
         - (instance) reservation: an optional _Reservation whose bytes
           are spent first.
        """
        return _UploadContext(
            self, fs, path, baseline_occupancy, baseline_size, reservation
        )

    def spend(self, ctx, amount):
        """Atomically bill amount bytes of in-flight growth for ctx,
        spending the reservation first.  Raise QuotaExceeded if the
        remaining quota is not enough.
        """
        with self._lock:
            if ctx.settled:
                raise QuotaError("upload context already settled")
            from_reservation = 0
            if ctx.reservation is not None and ctx.reservation.active:
                from_reservation = min(amount, ctx.reservation.remaining)
            extra = amount - from_reservation
            if extra > self._available_unlocked():
                raise QuotaExceeded(
                    f"cannot bill {extra} bytes; "
                    f"{self._available_unlocked()} available"
                )
            if from_reservation:
                ctx.reservation.remaining -= from_reservation
                self._reserved -= from_reservation
            self._unknown += amount
            ctx.billed += amount

    def _release_reservation_unlocked(self, ctx):
        reservation = ctx.reservation
        if reservation is not None and reservation.active:
            self._reserved -= reservation.remaining
            reservation.remaining = 0
            reservation.active = False
        ctx.reservation = None

    def settle(self, ctx, measured_occupancy):
        """Settle ctx against the occupancy proven by the backend.

        Only the growth beyond the baseline occupancy is confirmed;
        if the backend proves the upload occupies less than what was
        billed in flight (e.g. sparse data), the difference is
        refunded.  Unused reservation is released.
        """
        with self._lock:
            if ctx.settled:
                return
            ctx.settled = True
            self._unknown -= ctx.billed
            self._release_reservation_unlocked(ctx)
            delta = measured_occupancy - ctx.baseline_occupancy
            self._confirmed += max(0, delta)

    def settle_unmeasurable(self, ctx):
        """Settle ctx when the backend cannot prove the final
        occupancy.  The bytes billed in flight are kept as confirmed:
        a failed reclaim must not release quota early.
        """
        with self._lock:
            if ctx.settled:
                return
            ctx.settled = True
            self._confirmed += ctx.billed
            self._unknown -= ctx.billed
            self._release_reservation_unlocked(ctx)

    # --- reclaim

    def reclaim(self, amount):
        """Subtract amount bytes from the confirmed occupancy.  Must
        only be called after the backend actually freed the bytes.
        """
        if amount < 0:
            raise ValueError("amount must be >= 0")
        with self._lock:
            self._confirmed = max(0, self._confirmed - amount)


class QuotaUploadFile:
    """A file wrapper billing upload growth to a QuotaLedger.

    Only bytes extending the file beyond its previous logical size are
    billed while writing; bytes overwriting an existing range are not
    billed again.  On close() the upload is settled against the
    occupancy proven by the backend; if the backend cannot prove it,
    the bytes billed in flight remain confirmed.
    """

    def __init__(self, file, ctx, start_pos):
        self._file = file
        self._ctx = ctx
        self._pos = start_pos
        self._hwm = max(ctx.baseline_size, start_pos)
        self.name = file.name

    def __getattr__(self, attr):
        return getattr(self._file, attr)

    def __repr__(self):
        return f"<{self.__class__.__name__}({self.name!r})>"

    def write(self, data):
        end = self._pos + len(data)
        if end > self._hwm:
            growth = end - self._hwm
            try:
                self._ctx.ledger.spend(self._ctx, growth)
            except QuotaExceeded as err:
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC)) from err
            self._hwm = end
        written = self._file.write(data)
        self._pos += written if written is not None else len(data)
        return written

    def close(self):
        if self._file.closed:
            return
        try:
            self._file.close()
        finally:
            ctx = self._ctx
            if not ctx.settled:
                measured = ctx.ledger.rules.measure(ctx.fs, ctx.path)
                if measured is None:
                    ctx.ledger.settle_unmeasurable(ctx)
                else:
                    ctx.ledger.settle(ctx, measured)


class QuotaManager:
    """Registry mapping data spaces (e.g. user home directories) to
    their QuotaLedger, shared by all sessions of a server.
    """

    def __init__(self, rules=None):
        self._rules = rules
        self._ledgers = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(space):
        return os.path.realpath(space)

    def set_quota(self, space, limit, rules=None, strong=True):
        """Enable quota accounting for a data space, returning its
        QuotaLedger.  Raise QuotaNotSupported if the occupancy rules
        cannot provide the required atomic guarantees.
        """
        if rules is None:
            rules = self._rules or BlockOccupancyRules()
        ledger = QuotaLedger(limit, rules, strong=strong)
        with self._lock:
            self._ledgers[self._key(space)] = ledger
        return ledger

    def ledger_for(self, space):
        """Return the QuotaLedger for a data space, or None if quota
        accounting is not enabled for it.
        """
        with self._lock:
            return self._ledgers.get(self._key(space))

    def remove_quota(self, space):
        """Disable quota accounting for a data space."""
        with self._lock:
            return self._ledgers.pop(self._key(space), None)

    def spaces(self):
        """Return the list of data spaces with quota accounting."""
        with self._lock:
            return list(self._ledgers)
