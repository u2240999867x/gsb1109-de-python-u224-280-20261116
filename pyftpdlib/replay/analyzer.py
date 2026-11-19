# Copyright (C) 2007 Giampaolo Rodola' <g.rodola@gmail.com>.
# Use of this source code is governed by MIT license that can be
# found in the LICENSE file.

"""Replay FTP control/data events and verify one download.

The analyzer keeps three kinds of evidence apart:

* ``received_ranges`` -- byte ranges actually observed in the server to
  client data direction, with retransmissions de-duplicated.
* ``data_close`` -- FIN/RST facts for the data connection, including who
  closed it and whether the close was clean.
* ``final_reply`` -- the last control reply belonging to the transfer;
  ``terminal_reply`` is the reply which decides the transfer outcome.

A missing terminal reply never produces ``complete`` or ``incomplete``:
the transfer stays ``unknown``.  Replies are matched using FTP reply
semantics rather than raw arrival order, so a delayed ``226`` cannot
complete a later, unrelated command.
"""

from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Tuple

from .events import Event, EventError

__all__ = [
    "Analysis",
    "Analyzer",
    "Command",
    "DataClose",
    "RangeSet",
    "ReplyBlock",
    "TransferAnalysis",
    "analyze_events",
]

TRANSFER_COMMANDS = ("RETR", "STOR", "STOU", "APPE", "LIST", "NLST", "MLSD")
DOWNLOAD_COMMANDS = ("RETR",)

# Reply codes which can terminate a transfer command.
TRANSFER_TERMINAL = {
    226,
    250,
    425,
    426,
    450,
    451,
    452,
    550,
    551,
    552,
    553,
    554,
}
TRANSFER_SUCCESS = {226, 250}
TRANSFER_FAILURE = TRANSFER_TERMINAL - TRANSFER_SUCCESS

# Codes specific enough that a delayed copy must not be used to complete
# a later unrelated command.
LATE_TRANSFER_CODES = {226, 250, 426, 451, 452, 550, 552, 554}

# Commands and the reply codes they may legitimately consume.
_ACCEPTS = {
    "ABOR": {225, 226, 426, 421, 500, 501, 502},
    "ALLO": {202, 421, 500, 501, 502},
    "APPE": TRANSFER_TERMINAL | {125, 150},
    "CDUP": {250, 421, 500, 501, 502, 530, 550},
    "CWD": {250, 421, 500, 501, 502, 530, 550},
    "DELE": {250, 421, 500, 501, 502, 530, 550},
    "EPRT": {200, 421, 425, 500, 501, 502, 522, 530},
    "EPSV": {229, 421, 425, 500, 501, 502, 522, 530},
    "FEAT": {211, 421, 500, 501, 502},
    "HELP": {211, 214, 421, 500, 501, 502},
    "LIST": TRANSFER_TERMINAL | {125, 150},
    "MDTM": {213, 421, 500, 501, 502, 530, 550},
    "MFMT": {213, 421, 500, 501, 502, 530, 550},
    "MKD": {257, 421, 500, 501, 502, 530, 550},
    "MLSD": TRANSFER_TERMINAL | {125, 150},
    "MLST": {250, 421, 500, 501, 502, 530, 550},
    "MODE": {200, 421, 500, 501, 502, 504, 530},
    "NLST": TRANSFER_TERMINAL | {125, 150},
    "NOOP": {200, 421, 500, 501, 502},
    "OPTS": {200, 421, 451, 500, 501, 502, 530},
    "PASS": {202, 230, 331, 421, 500, 501, 502, 530},
    "PASV": {227, 421, 425, 500, 501, 502, 530},
    "PORT": {200, 421, 425, 500, 501, 502, 530},
    "PWD": {257, 421, 500, 501, 502, 530},
    "QUIT": {221, 421, 500, 501, 502},
    "REIN": {220, 421, 500, 501, 502, 530},
    "REST": {350, 421, 500, 501, 502, 530},
    "RETR": TRANSFER_TERMINAL | {125, 150},
    "RMD": {250, 421, 500, 501, 502, 530, 550},
    "RNFR": {350, 421, 500, 501, 502, 530, 550},
    "RNTO": {250, 421, 500, 501, 502, 530, 550},
    "SITE": {200, 202, 214, 250, 421, 500, 501, 502, 530, 550},
    "SIZE": {213, 421, 500, 501, 502, 530, 550},
    "STAT": {211, 212, 213, 421, 450, 500, 501, 502, 530, 550},
    "STOR": TRANSFER_TERMINAL | {125, 150},
    "STOU": TRANSFER_TERMINAL | {125, 150},
    "STRU": {200, 421, 500, 501, 502, 504, 530},
    "SYST": {215, 421, 500, 501, 502},
    "TYPE": {200, 421, 500, 501, 502, 504, 530},
    "USER": {230, 331, 421, 500, 501, 502, 530},
    "XCUP": {250, 421, 500, 501, 502, 530, 550},
    "XCWD": {250, 421, 500, 501, 502, 530, 550},
    "XMKD": {257, 421, 500, 501, 502, 530, 550},
    "XPWD": {257, 421, 500, 501, 502, 530},
    "XRMD": {250, 421, 500, 501, 502, 530, 550},
}


def _accepts(command, code):
    if code is None:
        return False
    codes = _ACCEPTS.get(command.name)
    if codes is None:
        return True
    return code in codes


@dataclass
class ReplyBlock:
    events: List[Event]
    code: Optional[int]
    complete: bool
    text: str

    @property
    def ts(self):
        return self.events[0].ts

    @property
    def ts_end(self):
        return self.events[-1].ts

    def as_dict(self):
        return {
            "code": self.code,
            "text": self.text,
            "ts": self.ts,
            "complete": self.complete,
            "lines": [event.data for event in self.events],
        }


@dataclass
class Command:
    event: Event
    name: str
    arg: str
    raw: str
    replies: List[ReplyBlock] = field(default_factory=list)
    final: Optional[ReplyBlock] = None
    transfer: Optional["TransferAnalysis"] = None
    abort_of: Optional["TransferAnalysis"] = None

    @property
    def ts(self):
        return self.event.ts

    @property
    def session(self):
        return self.event.session

    @property
    def is_transfer(self):
        return self.name in TRANSFER_COMMANDS

    @property
    def started(self):
        return any(reply.code in (125, 150) for reply in self.replies)


@dataclass
class RangeSet:
    """A set of half-open byte intervals, merged and de-duplicated."""

    intervals: List[Tuple[int, int]] = field(default_factory=list)

    def add(self, start, end):
        if end <= start:
            return
        if start < 0:
            raise EventError(f"negative data offset: {start}")
        merged = []
        for cur_start, cur_end in sorted(self.intervals + [(start, end)]):
            if merged and cur_start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], cur_end))
            else:
                merged.append((cur_start, cur_end))
        self.intervals = merged

    def total(self):
        return sum(end - start for start, end in self.intervals)

    def clamp(self, start, end):
        result = []
        for cur_start, cur_end in self.intervals:
            lo = max(cur_start, start)
            hi = min(cur_end, end)
            if hi > lo:
                result.append((lo, hi))
        return result

    def gaps(self, start, end):
        result = []
        cursor = start
        for cur_start, cur_end in self.intervals:
            if cur_end <= start:
                continue
            if cur_start >= end:
                break
            if cur_start > cursor:
                result.append((cursor, min(cur_start, end)))
            cursor = max(cursor, cur_end)
            if cursor >= end:
                break
        if cursor < end:
            result.append((cursor, end))
        return [(a, b) for a, b in result if b > a]

    def outside(self, start, end):
        result = []
        for cur_start, cur_end in self.intervals:
            if cur_start < start:
                result.append((cur_start, min(cur_end, start)))
            if cur_end > end:
                result.append((max(cur_start, end), cur_end))
        return [(a, b) for a, b in result if b > a]

    def as_list(self):
        return [[start, end] for start, end in self.intervals]


@dataclass
class DataClose:
    events: List[Event] = field(default_factory=list)

    @property
    def closed(self):
        return bool(self.events)

    @property
    def reset(self):
        return any(event.how == "reset" for event in self.events)

    @property
    def fin(self):
        return any(event.how in ("fin", "") for event in self.events)

    @property
    def clean(self):
        if not self.events:
            return None
        return self.fin and not self.reset

    @property
    def closed_by(self):
        return [event.who for event in self.events if event.who]

    @property
    def server_closed(self):
        return any(event.who == "server" for event in self.events)

    @property
    def client_closed(self):
        return any(event.who == "client" for event in self.events)

    @property
    def first_ts(self):
        return min(event.ts for event in self.events) if self.events else None

    @property
    def last_ts(self):
        return max(event.ts for event in self.events) if self.events else None

    def as_dict(self):
        return {
            "closed": self.closed,
            "clean": self.clean,
            "reset": self.reset,
            "closed_by": self.closed_by,
            "server_closed": self.server_closed,
            "client_closed": self.client_closed,
            "events": [
                {
                    "ts": event.ts,
                    "conn": event.conn,
                    "who": event.who,
                    "how": event.how,
                }
                for event in self.events
            ],
        }


@dataclass
class TransferAnalysis:
    id: str
    command: Command
    session: str
    restart_offset: int = 0
    restart_source: Optional[str] = None
    expected_size: Optional[int] = None
    expected_size_source: Optional[str] = None
    ranges: RangeSet = field(default_factory=RangeSet)
    per_conn_ranges: dict = field(default_factory=dict)
    segment_count: int = 0
    wire_bytes: int = 0
    duplicate_bytes: int = 0
    offset_basis: str = "file"
    data_conns: List[str] = field(default_factory=list)
    data_close: DataClose = field(default_factory=DataClose)
    replies: List[ReplyBlock] = field(default_factory=list)
    terminal: Optional[ReplyBlock] = None
    trailing: List[ReplyBlock] = field(default_factory=list)
    late_replies: List[ReplyBlock] = field(default_factory=list)
    outcome: str = "unknown"
    state: str = "unknown"
    reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    aborted_by_abor: bool = False
    gaps: Optional[List[Tuple[int, int]]] = None
    missing_bytes: Optional[int] = None
    unexpected_ranges: Optional[List[Tuple[int, int]]] = None

    @property
    def file(self):
        return self.command.arg

    @property
    def started_ts(self):
        return self.command.ts

    @property
    def last_data_ts(self):
        ts = [event.ts for event in self.data_close.events]
        ts += list(getattr(self, "_segment_ts", []))
        return max(ts) if ts else None

    @property
    def has_data_evidence(self):
        return bool(self.segment_count or self.data_close.closed)

    @property
    def expected_range(self):
        # expected_size is the full file size (as reported by SIZE);
        # restart_offset is where this retrieval begins.
        if self.expected_size is None:
            return None
        return (self.restart_offset, self.expected_size)

    @property
    def final(self):
        for reply in reversed(self.replies):
            if reply.code is not None and reply.code // 100 != 1:
                return reply
        return None

    def finalize(self):
        self.reasons = []
        self.gaps = None
        self.missing_bytes = None
        self.unexpected_ranges = None
        expected = self.expected_range
        if expected is not None:
            start, end = expected
            self.gaps = self.ranges.gaps(start, end)
            self.missing_bytes = sum(b - a for a, b in self.gaps)
            self.unexpected_ranges = self.ranges.outside(start, end)
        else:
            self.gaps = None
            self.missing_bytes = None
            self.unexpected_ranges = None

        if self.terminal is None:
            self.outcome = "unknown"
            self.state = "unknown"
            self.reasons.append("terminal_reply_missing")
            return

        if self.terminal.code == 426 and not self.trailing:
            # A 426 is normally followed by a 226 (abort complete).  If
            # the capture ends before that reply, the ending is not
            # fully observed; keep the transfer unknown rather than
            # claiming a definitive abort.
            self.state = "unknown"
            self.outcome = "aborted"
            self.reasons.append("abort_continuation_missing")
            return

        code = self.terminal.code
        if code in TRANSFER_SUCCESS:
            self.outcome = "success"
            if self.aborted_by_abor:
                self.outcome = "aborted"
                self.reasons.append("aborted_by_abor")
        elif code == 426:
            self.outcome = "aborted"
        elif code in TRANSFER_FAILURE:
            self.outcome = "failed"
        else:
            self.outcome = "unknown"

        if self.outcome != "success":
            self.state = "incomplete"
            if not self.reasons:
                self.reasons.append(f"transfer_{self.outcome}")
            return

        if expected is not None:
            if self.missing_bytes:
                self.state = "incomplete"
                self.reasons.append("byte_ranges_incomplete")
                if self.unexpected_ranges:
                    self.reasons.append("bytes_outside_expected_range")
                return
            if self.unexpected_ranges:
                self.warnings.append("bytes_outside_expected_range")

        if self.data_close.reset:
            self.state = "incomplete"
            self.reasons.append("data_connection_reset")
            return

        if not self.data_close.closed:
            if self.segment_count == 0 and expected != 0:
                self.state = "unknown"
                self.reasons.append("no_data_observed")
            else:
                self.state = "complete"
                self.reasons.append("data_close_not_observed")
            return

        if self.data_close.client_closed and not self.data_close.server_closed:
            self.state = "incomplete"
            self.reasons.append("data_closed_by_client_only")
            return

        if expected is None:
            # Without SIZE (or an explicit --size) coverage cannot be
            # verified; a success reply alone is not proof of a complete
            # download.
            self.state = "unknown"
            self.reasons.append("expected_size_unknown")
            return

        self.state = "complete"

    def as_dict(self):
        expected = self.expected_range
        return {
            "id": self.id,
            "session": self.session,
            "file": self.file,
            "command": {"raw": self.command.raw, "ts": self.command.ts},
            "restart_offset": self.restart_offset,
            "restart_source": self.restart_source,
            "expected_size": self.expected_size,
            "expected_size_source": self.expected_size_source,
            "expected_range": list(expected) if expected else None,
            "received_ranges": self.ranges.as_list(),
            "received_bytes": self.ranges.total(),
            "wire_bytes": self.wire_bytes,
            "duplicate_bytes": self.duplicate_bytes,
            "segment_count": self.segment_count,
            "offset_basis": self.offset_basis,
            "gaps": [list(pair) for pair in self.gaps]
            if self.gaps is not None
            else None,
            "missing_bytes": self.missing_bytes,
            "unexpected_ranges": (
                [list(pair) for pair in self.unexpected_ranges]
            )
            if self.unexpected_ranges is not None
            else None,
            "data_connections": self.data_conns,
            "data_close": self.data_close.as_dict(),
            "replies": [reply.as_dict() for reply in self.replies],
            "terminal_reply": (
                self.terminal.as_dict() if self.terminal else None
            ),
            "final_reply": self.final.as_dict() if self.final else None,
            "trailing_replies": [reply.as_dict() for reply in self.trailing],
            "late_replies": [reply.as_dict() for reply in self.late_replies],
            "outcome": self.outcome,
            "state": self.state,
            "complete": self.state == "complete",
            "reasons": self.reasons,
            "warnings": self.warnings,
        }


@dataclass
class Analysis:
    transfers: List[TransferAnalysis] = field(default_factory=list)
    commands: List[Command] = field(default_factory=list)
    unattributed_replies: List[ReplyBlock] = field(default_factory=list)
    late_replies: List[ReplyBlock] = field(default_factory=list)
    unattributed_data: List[Event] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    sessions: List[str] = field(default_factory=list)
    event_count: int = 0

    @property
    def primary(self):
        return self.transfers[0] if self.transfers else None

    def as_dict(self):
        return {
            "event_count": self.event_count,
            "sessions": self.sessions,
            "summary": self.primary.as_dict() if self.primary else None,
            "transfers": [item.as_dict() for item in self.transfers],
            "unattributed_replies": [
                reply.as_dict() for reply in self.unattributed_replies
            ],
            "late_replies": [reply.as_dict() for reply in self.late_replies],
            "unattributed_data_events": len(self.unattributed_data),
            "warnings": self.warnings,
        }


def _parse_command(event):
    text = event.data.strip()
    if not text:
        return None
    parts = text.split(" ", 1)
    name = parts[0].upper()
    arg = parts[1] if len(parts) > 1 else ""
    if not name.replace("-", "").isalpha():
        return None
    return Command(event=event, name=name, arg=arg, raw=text)


def _reply_blocks(events):
    """Group s2c control lines into single/multiline reply blocks."""
    blocks = []
    current = None
    for event in events:
        text = event.data
        if event.is_multiline_start():
            code = event.code()
            if current is not None:
                current.complete = False
                blocks.append(current)
            current = ReplyBlock(
                events=[event], code=code, complete=False, text=text
            )
        elif current is not None:
            current.events.append(event)
            if event.is_multiline_end(current.code):
                current.complete = True
                current.text = "\n".join(e.data for e in current.events)
                blocks.append(current)
                current = None
        elif event.code() is not None:
            blocks.append(
                ReplyBlock(
                    events=[event],
                    code=event.code(),
                    complete=True,
                    text=text,
                )
            )
        else:
            blocks.append(
                ReplyBlock(
                    events=[event], code=None, complete=False, text=text
                )
            )
    if current is not None:
        blocks.append(current)
    return blocks


class Analyzer:
    """Stateful matcher for FTP commands, replies and data events."""

    def __init__(self, expected_size=None, late_window=None):
        self.expected_size = expected_size
        self.late_window = late_window
        self.commands = []
        self.transfers = []
        self.pending = []
        self.unattributed_replies = []
        self.late_replies = []
        self.unattributed_data = []
        self.warnings = []
        self.sessions = []
        self._transfer_seq = 0
        self._last_transfer = None
        self._abort_command = None
        self._abnormal_transfer = None
        self._conn_transfer = {}
        self._conn_base = {}
        self._conn_next_offset = {}
        self._conn_close_ts = {}

    # -- transfer creation ------------------------------------------------

    def _new_transfer(self, command):
        if command.name not in DOWNLOAD_COMMANDS:
            return None
        self._transfer_seq += 1
        transfer = TransferAnalysis(
            id=f"t{self._transfer_seq}",
            command=command,
            session=command.event.session,
            expected_size=self.expected_size,
            expected_size_source=(
                "option" if self.expected_size is not None else None
            ),
        )
        transfer._segment_ts = []
        command.transfer = transfer
        self.transfers.append(transfer)
        self._last_transfer = transfer
        return transfer

    def _add_command(self, command):
        self.commands.append(command)
        if command.name in TRANSFER_COMMANDS:
            self._new_transfer(command)
            self.pending.append(command)
            return
        self.pending.append(command)
        if command.name == "ABOR":
            target = None
            for candidate in reversed(self.transfers):
                if (
                    candidate.session == command.session
                    and candidate.terminal is None
                    and candidate.command.final is None
                ):
                    target = candidate
                    break
            command.abort_of = target
            self._abort_command = command

    # -- reply matching ---------------------------------------------------

    def _session_pending(self, session):
        return [
            command
            for command in self.pending
            if command.session == session and command.final is None
        ]

    def _settled(self, transfer, ts):
        return transfer.terminal is not None and transfer.terminal.ts < ts

    def _last_activity(self, transfer):
        activity = transfer.last_data_ts
        if activity is None:
            activity = transfer.command.ts
        return activity

    def _candidate_transfers(self, session, ts):
        result = []
        for transfer in self.transfers:
            if transfer.session != session:
                continue
            if self._settled(transfer, ts) and not transfer.late_replies:
                continue
            result.append(transfer)
        return result

    def _recently_closed_transfer(self, session, ts):
        best = None
        for transfer in self.transfers:
            if transfer.session != session:
                continue
            if not transfer.has_data_evidence:
                continue
            if self._transfer_open_at(transfer, ts):
                continue
            activity = self._last_activity(transfer)
            if activity > ts + 1e-9:
                continue
            if (
                self.late_window is not None
                and ts - activity > self.late_window
            ):
                continue
            key = (activity, transfer.started_ts, transfer.id)
            if best is None or key > best[0]:
                best = (key, transfer)
        return best[1] if best else None

    def _unsettled_transfers(self, session, ts):
        result = []
        for transfer in self._candidate_transfers(session, ts):
            if transfer.terminal is None:
                result.append(transfer)
        result.sort(key=lambda t: (t.started_ts, t.id))
        return result

    def _attach_to_transfer(self, transfer, block, late=False):
        if block not in transfer.replies:
            transfer.replies.append(block)
        if late:
            if block not in transfer.late_replies:
                transfer.late_replies.append(block)
            self.late_replies.append(block)
            self.warnings.append(
                f"late reply {block.code} attributed to transfer "
                f"{transfer.id}, not to a later command"
            )
        if (
            not late
            and transfer.terminal is None
            and block.code in TRANSFER_TERMINAL
        ):
            transfer.terminal = block
            transfer.command.final = block
            if transfer.command in self.pending:
                self.pending.remove(transfer.command)
            if block.code == 426:
                self._abnormal_transfer = transfer
        elif transfer.terminal is not None and block.code not in (125, 150):
            if block not in transfer.trailing:
                transfer.trailing.append(block)
            if not late:
                # An abnormal ending can produce two replies (e.g. 426
                # followed by 226).  Both stay with the original
                # transfer, and the last one is its final reply.
                transfer.command.final = block
                if transfer.command in self.pending:
                    self.pending.remove(transfer.command)
        return transfer

    def _has_data_after(self, transfer, ts):
        return any(seg_ts > ts + 1e-9 for seg_ts in transfer._segment_ts)

    def _eligible_at(self, transfer, ts, allow_open=False):
        """Can ``transfer`` already have produced a terminal reply at ts?

        A success reply implies the data connection is finished, so an
        open data connection disqualifies it.  A ``426`` is precisely
        the server announcing an abort *before* closing the data
        connection, so ``allow_open`` permits that case.
        """
        if self._has_data_after(transfer, ts):
            return False
        if (
            not allow_open
            and transfer.segment_count
            and self._transfer_open_at(transfer, ts)
        ):
            return False
        return True

    def _transfer_open_at(self, transfer, ts):
        for conn in transfer.data_conns:
            closes = self._conn_close_ts.get(conn, [])
            # A connection id may be reused by a later transfer; only
            # closes at or after this transfer's start are relevant.
            relevant = [
                value
                for value in closes
                if value >= transfer.started_ts - 1e-9
            ]
            if relevant and min(relevant) > ts + 1e-9:
                return True
        return False

    def _oldest_unsettled(
        self,
        session,
        ts,
        require_started=False,
        require_data=False,
        allow_open=False,
    ):
        candidates = []
        for transfer in self.transfers:
            if transfer.session != session:
                continue
            if transfer.terminal is not None:
                continue
            if require_data and not transfer.segment_count:
                continue
            if not self._eligible_at(transfer, ts, allow_open=allow_open):
                continue
            if require_started and not (
                transfer.command.started or transfer.segment_count
            ):
                continue
            candidates.append(transfer)
        candidates.sort(key=lambda t: (t.started_ts, t.id))
        return candidates[0] if candidates else None

    def _later_transfer_finished(self, session, abnormal, ts):
        """True if another transfer clearly finished before ``ts``."""
        for transfer in self.transfers:
            if transfer is abnormal or transfer.session != session:
                continue
            if transfer.started_ts < abnormal.terminal.ts - 1e-9:
                continue
            if not transfer.segment_count or not transfer.data_close.closed:
                continue
            close_ts = transfer.data_close.last_ts
            if close_ts is not None and close_ts <= ts + 1e-9:
                return True
        return False

    def _handle_transfer_reply(self, block):
        code = block.code
        session = block.events[0].session
        ts = block.ts

        if code in TRANSFER_SUCCESS or code == 426:
            # Abnormal endings may legitimately produce two replies for
            # the same transfer (e.g. 426 then 226).  The second reply
            # stays with the original transfer even if an unrelated
            # command or a new transfer has arrived in between, so this
            # check must run before any "newest outstanding" match.
            abnormal = self._abnormal_transfer
            if code in TRANSFER_SUCCESS and abnormal is not None:
                other = self._oldest_unsettled(
                    session,
                    ts,
                    require_started=True,
                    require_data=True,
                )
                if other is not None and other is not abnormal:
                    # Another transfer is demonstrably finished and
                    # still awaiting its reply; this 226 is its, not
                    # the abnormal continuation.
                    pass
                else:
                    self._abnormal_transfer = None
                    late = self._later_transfer_finished(session, abnormal, ts)
                    return self._attach_to_transfer(abnormal, block, late=late)

            target = self._oldest_unsettled(
                session,
                ts,
                require_started=True,
                require_data=True,
                allow_open=code == 426,
            )
            if target is not None:
                return self._attach_to_transfer(target, block)

            # A 426/226 can arrive while the data connection is still
            # open (the server announces the abort, then closes).  Only
            # fall back when the candidate is not visibly mid-transfer:
            # an open data connection means later bytes are still to
            # come, so the reply cannot be its ending.
            for candidate in self._unsettled_transfers(session, ts):
                if not candidate.command.started:
                    continue
                if not self._eligible_at(
                    candidate, ts, allow_open=code == 426
                ):
                    continue
                return self._attach_to_transfer(candidate, block)

            recent = self._recently_closed_transfer(session, ts)
            if recent is not None:
                return self._attach_to_transfer(recent, block, late=True)
            return None

        # Failure replies (450/550/...) can either fail a command before
        # it starts or abort a transfer which is already running.
        if code in {426, 451, 452, 550, 552, 554}:
            target = self._oldest_unsettled(session, ts, require_started=True)
            if target is not None:
                return self._attach_to_transfer(target, block)
        for command in self._session_pending(session):
            if command.name in TRANSFER_COMMANDS and _accepts(command, code):
                if command.transfer is not None:
                    if self._has_data_after(command.transfer, ts):
                        continue
                    return self._attach_to_transfer(command.transfer, block)
                self._attach_command(command, block)
                return command.transfer
        recent = self._recently_closed_transfer(session, ts)
        if recent is not None:
            return self._attach_to_transfer(recent, block, late=True)
        return None

    def _abort_target(self, session, ts):
        command = self._abort_command
        if command is None or command.final is not None:
            return None
        if command.session != session:
            return None
        return command

    def _handle_abor_reply(self, block):
        command = self._abort_target(block.events[0].session, block.ts)
        if command is None:
            return False
        if not _accepts(command, block.code):
            return False
        command.final = block
        command.replies.append(block)
        if command in self.pending:
            self.pending.remove(command)
        transfer = command.abort_of
        if transfer is not None and block.code in (225, 226):
            if block.code == 226:
                transfer.aborted_by_abor = True
            if block not in transfer.replies:
                transfer.replies.append(block)
            if block not in transfer.trailing:
                transfer.trailing.append(block)
            transfer.command.final = block
            if transfer.command in self.pending:
                self.pending.remove(transfer.command)
        self._abort_command = None
        return True

    def _handle_reply(self, block):
        code = block.code
        if code is None:
            self.unattributed_replies.append(block)
            return
        if not block.complete and code // 100 != 1:
            self.warnings.append(
                f"incomplete multiline reply starting with {code}"
            )
            self.unattributed_replies.append(block)
            return

        session = block.events[0].session

        if code // 100 == 1:
            # 125/150 announce the start of a transfer.  They never
            # finalize the command.
            for command in self._session_pending(session):
                if command.name in TRANSFER_COMMANDS and _accepts(
                    command, code
                ):
                    command.replies.append(block)
                    if command.transfer is not None:
                        command.transfer.replies.append(block)
                    return
            self.unattributed_replies.append(block)
            return

        if code == 225:
            if self._handle_abor_reply(block):
                return

        if code == 426:
            transfer = self._handle_transfer_reply(block)
            if transfer is not None:
                return
            if self._handle_abor_reply(block):
                return
            self.unattributed_replies.append(block)
            return

        if code == 226:
            command = self._abort_target(session, block.ts)
            if command is not None:
                target = command.abort_of
                if target is None or target.terminal is not None:
                    if self._handle_abor_reply(block):
                        return

        if code in TRANSFER_TERMINAL:
            transfer = self._handle_transfer_reply(block)
            if transfer is not None:
                return
            if code in LATE_TRANSFER_CODES and self._last_transfer is not None:
                self._attach_to_transfer(self._last_transfer, block, late=True)
                return
            self.unattributed_replies.append(block)
            return

        pending = self._session_pending(session)
        # Non-transfer commands are answered in order, so FIFO is the
        # correct rule for them.
        for command in pending:
            if command.name in TRANSFER_COMMANDS:
                continue
            if _accepts(command, code):
                self._attach_command(command, block)
                return
        for command in pending:
            if command.name in TRANSFER_COMMANDS and _accepts(command, code):
                self._attach_command(command, block)
                return
        for command in pending:
            if _accepts(command, code):
                self._attach_command(command, block)
                return
        self.unattributed_replies.append(block)

    def _attach_command(self, command, block):
        command.replies.append(block)
        command.final = block
        if command in self.pending:
            self.pending.remove(command)

    # -- data -------------------------------------------------------------

    def _pick_transfer(self, event, allow_closed=False):
        marker = None
        for key in ("transfer", "transfer_id", "t"):
            if key in event.extra:
                marker = event.extra[key]
                break
        if marker is not None:
            for transfer in self.transfers:
                if marker in (transfer.id, transfer.command.raw):
                    return transfer
            self.warnings.append(
                f"data event references unknown transfer {marker!r}"
            )
            return None
        if event.conn and event.conn in self._conn_transfer:
            mapped = self._conn_transfer[event.conn]
            if (
                mapped.terminal is None
                or mapped.terminal.ts >= event.ts - 1e-9
            ):
                return mapped
            # A connection identifier reused after the previous
            # transfer settled is re-bound below.
            del self._conn_transfer[event.conn]

        session = event.session
        candidates = []
        for transfer in self.transfers:
            if transfer.session != session and session != "default":
                continue
            if transfer.command.ts > event.ts + 1e-9:
                continue
            if not allow_closed and self._settled(transfer, event.ts):
                continue
            candidates.append(transfer)
        if not candidates:
            if allow_closed:
                for transfer in reversed(self.transfers):
                    if transfer.session == session:
                        return transfer
            return None
        candidates.sort(key=lambda transfer: transfer.started_ts)
        return candidates[-1]

    def _segment_offset(self, event, transfer):
        if event.offset is not None:
            if event.extra.get("offset_basis") == "stream":
                return transfer.restart_offset + event.offset, "stream"
            return event.offset, "file"
        if event.seq is not None:
            key = event.conn or "default"
            if key not in self._conn_base:
                self._conn_base[key] = event.seq
            base = self._conn_base[key]
            offset = event.seq - base
            if offset < 0:
                offset += 1 << 32
            return transfer.restart_offset + offset, "relative"
        key = event.conn or "default"
        start = self._conn_next_offset.get(key, 0)
        self._conn_next_offset[key] = start + (event.length or 0)
        return start, "capture_order"

    def _handle_segment(self, event):
        transfer = self._pick_transfer(event)
        if transfer is None:
            self.unattributed_data.append(event)
            return
        if event.direction == "c2s":
            transfer.warnings.append(
                "ignoring client-to-server data during download"
            )
            return
        if event.conn and event.conn not in transfer.data_conns:
            transfer.data_conns.append(event.conn)
            self._conn_transfer[event.conn] = transfer
        offset, basis = self._segment_offset(event, transfer)
        length = event.length or 0
        transfer.segment_count += 1
        transfer.wire_bytes += length
        transfer._segment_ts.append(event.ts)
        before = transfer.ranges.total()
        transfer.ranges.add(offset, offset + length)
        after = transfer.ranges.total()
        transfer.duplicate_bytes += length - (after - before)
        if basis == "capture_order":
            transfer.offset_basis = "capture_order"
        elif (
            basis in ("relative", "stream") and transfer.offset_basis == "file"
        ):
            transfer.offset_basis = basis

    def _handle_close(self, event):
        transfer = self._pick_transfer(event, allow_closed=True)
        if transfer is None:
            self.unattributed_data.append(event)
            return
        if event.conn and event.conn not in transfer.data_conns:
            transfer.data_conns.append(event.conn)
            self._conn_transfer[event.conn] = transfer
        if transfer.data_close.closed and transfer.terminal is not None:
            if event.ts > transfer.terminal.ts:
                transfer.warnings.append("data close after final reply")
        transfer.data_close.events.append(event)

    # -- post processing --------------------------------------------------

    def _resolve_command_state(self):
        sizes = {}
        rest = {}
        ordered = sorted(self.commands, key=lambda c: (c.ts, c.event.line))
        for command in ordered:
            final = command.final
            if (
                command.name == "SIZE"
                and final is not None
                and final.code == 213
            ):
                try:
                    sizes[(command.session, command.arg)] = int(
                        final.text.split(" ", 1)[1]
                    )
                except (IndexError, ValueError):
                    pass
            elif command.name == "REST":
                try:
                    value = int(command.arg)
                except ValueError:
                    value = None
                if final is not None and final.code == 350:
                    rest[command.session] = value
                elif final is not None:
                    rest.pop(command.session, None)
            elif command.name in TRANSFER_COMMANDS:
                transfer = command.transfer
                if transfer is None:
                    continue
                if command.name in DOWNLOAD_COMMANDS:
                    value = rest.pop(command.session, None)
                    if value is not None:
                        transfer.restart_offset = value
                        transfer.restart_source = "REST"
                    if transfer.expected_size is None:
                        size = sizes.get((command.session, command.arg))
                        if size is not None:
                            transfer.expected_size = size
                            transfer.expected_size_source = "SIZE"
                else:
                    rest.pop(command.session, None)

    # -- main -------------------------------------------------------------

    def run(self, events: Iterable[Event]) -> Analysis:
        events = sorted(events, key=lambda event: (event.ts, event.line))
        self.sessions = sorted({event.session for event in events})

        self._conn_close_ts = {}
        for event in events:
            if (
                event.channel == "data"
                and event.kind == "close"
                and event.conn
            ):
                self._conn_close_ts.setdefault(event.conn, []).append(event.ts)
        for values in self._conn_close_ts.values():
            values.sort()

        control = [event for event in events if event.channel == "control"]
        reply_events = [event for event in control if event.kind == "reply"]
        blocks = _reply_blocks(reply_events)
        start_ids = {id(block.events[0]): block for block in blocks}

        timeline = []
        for index, event in enumerate(events):
            if event.channel == "control":
                if event.kind == "command":
                    command = _parse_command(event)
                    if command is not None:
                        timeline.append((event.ts, index, "command", command))
                elif event.kind == "reply":
                    block = start_ids.get(id(event))
                    if block is not None:
                        timeline.append((event.ts, index, "reply", block))
            else:
                timeline.append((event.ts, index, "data", event))
        timeline.sort(key=lambda row: (row[0], row[1]))

        for _ts, _line, kind, item in timeline:
            if kind == "command":
                self._add_command(item)
            elif kind == "reply":
                self._handle_reply(item)
            elif item.kind == "segment":
                self._handle_segment(item)
            elif item.kind == "close":
                self._handle_close(item)
            else:
                self.unattributed_data.append(item)

        if self.pending:
            names = ", ".join(command.name for command in self.pending)
            self.warnings.append(f"commands without final reply: {names}")

        self._resolve_command_state()
        for transfer in self.transfers:
            transfer.finalize()

        return Analysis(
            transfers=self.transfers,
            commands=self.commands,
            unattributed_replies=self.unattributed_replies,
            late_replies=self.late_replies,
            unattributed_data=self.unattributed_data,
            warnings=self.warnings,
            sessions=self.sessions,
            event_count=len(events),
        )


def analyze_events(events, expected_size=None, late_window=None):
    """Analyze a sequence of :class:`Event` objects."""
    return Analyzer(expected_size=expected_size, late_window=late_window).run(
        events
    )
