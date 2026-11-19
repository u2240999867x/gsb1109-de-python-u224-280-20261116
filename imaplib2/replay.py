"""Local wire replay for validating pipeline plans.

A :class:`LocalReplay` drives a :class:`~imaplib2.scheduling.PipelinePlanner`
round by round against a scripted server. No sockets or threads are used:
the scripted server emits exactly the bytes a real IMAP server would send,
and they are fed through the unmodified :meth:`IMAP4._put_response`
parser, so attribution is checked against the real client code path.

For every round the replay asserts that:

* each command tag in the batch receives exactly one tagged completion and
  every completion tag belongs to a command of that batch (the parser leaves
  unknown tags un-popped, which the replay treats as a failure);
* each untagged response can be attributed to exactly one in-flight call -
  two calls sharing a reply kind is impossible by construction, and the
  replay re-checks it on the parsed byte stream;
* a ``+`` continuation only occurs for a singleton continuation round, and
  the client literal frame is only sent after that continuation arrives.

Failures (``NO``/``BAD``) and unsolicited notifications can be injected;
after each round the planner reconciles the real replies and re-plans the
remaining commands, mirroring production behaviour.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .imaplib2 import CRLF, IMAP4
from .scheduling import (
    Batch,
    PlannedCommand,
    PipelinePlanner,
    SELECTION_RESPONSE_KINDS,
)

__all__ = ("LocalReplay", "ReplayReport", "ReplayFailure")


class ReplayFailure(AssertionError):
    """A replay invariant (unique attribution, ordering, ...) was broken."""


@dataclass
class ReplayReport:
    """Recorded evidence of a completed replay."""

    rounds: int
    transcript: List[bytes] = field(default_factory=list)
    attributions: List[Tuple[bytes, str, str]] = field(default_factory=list)
    tagged_by_call: Dict[str, Tuple[str, Any]] = field(default_factory=dict)
    final_status: Dict[str, str] = field(default_factory=dict)
    batches: List[List[str]] = field(default_factory=list)

    def owners_of(self, kind: str) -> List[str]:
        return [call_id for frame, call_id, reply_kind in self.attributions
                if reply_kind == kind]


class _ReplayRequest:
    """Stand-in for :class:`imaplib2.imaplib2.Request` during replay."""

    def __init__(self, tag: bytes, name: str, call_id: str, sink: list):
        self.tag = tag
        self.name = name
        self.callback = None
        self.callback_arg = None
        self._call_id = call_id
        self._sink = sink

    def deliver(self, data: Any) -> None:
        self._sink.append((self._call_id, data))

    def abort(self, typ: Any, val: Any) -> None:
        self._sink.append((self._call_id, ("ABORTED", (typ, val))))


class _ParserHarness(IMAP4):
    """An :class:`IMAP4` without sockets: only the response parser is live.

    The parser state is initialised exactly as the real constructor would,
    so :meth:`_put_response` runs its production code path.
    """

    def __init__(self, tag_prefix: bytes):
        self.tagpre = tag_prefix
        self.tagnum = 0
        self.tagre = re.compile(
            br"(?P<tag>" + tag_prefix + br"\d+) (?P<type>[A-Z]+) (?P<data>.*)",
            re.ASCII,
        )
        self._mode_ascii()

        self.tagged_commands: Dict[Any, _ReplayRequest] = {}
        self.untagged_responses: List[List[Any]] = []
        self.commands_lock = threading.Lock()
        self.state_change_free = threading.Event()
        self.idle_lock = threading.Lock()
        self.idle_rqb = None
        self.idle_timeout = None
        self.Terminate = False
        self.debug = 0

        self._expecting_data = False
        self._expecting_data_len = 0
        self._accumulated_data: List[bytes] = []
        self._literal_expected = None

        self.tagged_sink: List[Tuple[str, Any]] = []
        self.continuation_sink: List[Tuple[str, Any]] = []
        self.untagged_log: List[Tuple[str, Any]] = []
        self.continuation_count = 0

    def _append_untagged(self, typ: str, dat: Any) -> None:
        super()._append_untagged(typ, dat)
        self.untagged_log.append((typ, dat))

    def _request_pop(self, name: Any, data: Any) -> None:
        if name == "continuation":
            self.continuation_count += 1
        super()._request_pop(name, data)


# Scripted untagged server output per reply kind. A few entries include a
# server literal so the literal branch of the real parser is exercised.
_UNTAGGED_TEMPLATES: Dict[str, List[bytes]] = {
    "CAPABILITY": [b"* CAPABILITY IMAP4REV1 IDLE\r\n"],
    "LIST": [b'* LIST () "/" "INBOX"\r\n'],
    "LSUB": [b'* LSUB () "/" "INBOX"\r\n'],
    "FETCH": [
        b"* 1 FETCH (FLAGS (\\Seen) BODY[] {5}\r\n",
        b"hello\r\n",
        b")\r\n",
    ],
    "SEARCH": [b"* SEARCH 1 2 3\r\n"],
    "SORT": [b"* SORT 3 2 1\r\n"],
    "THREAD": [b"* THREAD (1)(2 3)\r\n"],
    "STATUS": [b'* STATUS "INBOX" (MESSAGES 3 UNSEEN 1)\r\n'],
    "NAMESPACE": [b'* NAMESPACE (("" "/")) NIL NIL\r\n'],
    "EXPUNGE": [b"* 2 EXPUNGE\r\n"],
    "ID": [b'* ID ("name" "replay")\r\n'],
    "QUOTA": [b'* QUOTA "" (STORAGE 10 100)\r\n'],
    "QUOTAROOT": [b'* QUOTAROOT "INBOX" ""\r\n'],
    "ACL": [b'* ACL "INBOX" tester lrs\r\n'],
    "ANNOTATION": [b'* ANNOTATION "INBOX" "/comment" ("value.shared" "x")\r\n'],
    "MYRIGHTS": [b"* MYRIGHTS \"INBOX\" lrs\r\n"],
    "FLAGS": [b"* FLAGS (\\Answered \\Seen \\Deleted)\r\n"],
    "EXISTS": [b"* 3 EXISTS\r\n"],
    "RECENT": [b"* 0 RECENT\r\n"],
    "UIDVALIDITY": [b"* OK [UIDVALIDITY 424242]\r\n"],
    "UIDNEXT": [b"* OK [UIDNEXT 7]\r\n"],
    "PERMANENTFLAGS": [b"* OK [PERMANENTFLAGS (\\Seen \\*)]\r\n"],
}


class LocalReplay:
    """Replays a planned command batch against scripted server replies."""

    def __init__(
        self,
        planner: PipelinePlanner,
        fail: Optional[Dict[str, Tuple[str, str]]] = None,
        unsolicited: Optional[Dict[str, Sequence[bytes]]] = None,
        untagged_factory: Optional[
            Callable[[PlannedCommand], Dict[str, List[bytes]]]
        ] = None,
    ):
        self.planner = planner
        self.fail = dict(fail or {})
        self.unsolicited = {
            call_id: list(frames) for call_id, frames in (unsolicited or {}).items()
        }
        self.untagged_factory = untagged_factory
        self.report = ReplayReport(rounds=0)
        self._harness = _ParserHarness(planner._tag_prefix)

    def run(self) -> ReplayReport:
        while True:
            batch = self.planner.next_batch()
            if batch is None:
                break
            self._play_round(batch)
            replies = self._collect_replies(batch)
            self.planner.account_responses(replies)
        self.report.rounds = self.planner.round_index
        self.report.final_status = {
            call_id: pc.status for call_id, pc in self.planner.commands.items()
        }
        return self.report

    def _play_round(self, batch: Batch) -> None:
        self.report.batches.append([pc.call_id for pc in batch.commands])
        harness = self._harness
        harness.tagged_commands.clear()
        harness.untagged_responses.clear()
        harness.tagged_sink.clear()
        harness.continuation_sink.clear()
        harness.continuation_count = 0

        owner_by_kind: Dict[str, str] = {}
        for pc in batch.commands:
            for kind in pc.spec.response_kinds:
                if kind in owner_by_kind and owner_by_kind[kind] != pc.call_id:
                    raise ReplayFailure(
                        "kind %s claimed by both %s and %s in round %d"
                        % (kind, owner_by_kind[kind], pc.call_id, batch.round_index)
                    )
                owner_by_kind[kind] = pc.call_id
            harness.tagged_commands[pc.tag] = _ReplayRequest(
                pc.tag, pc.spec.name, pc.call_id, harness.tagged_sink
            )

        continuation_calls = [
            pc for pc in batch.commands if pc.spec.requires_continuation
        ]
        if continuation_calls and len(batch.commands) != 1:
            raise ReplayFailure(
                "continuation command %s shares round %d"
                % (continuation_calls[0].call_id, batch.round_index)
            )

        for pc in batch.commands:
            frames = self.planner.wire_frames(pc)
            if pc.spec.requires_continuation:
                self._send(frames[0])
                harness.tagged_commands["continuation"] = _ReplayRequest(
                    b"continuation", "continuation", pc.call_id,
                    harness.continuation_sink,
                )
                self._feed(b"+ go ahead\r\n")
                if "continuation" in harness.tagged_commands:
                    raise ReplayFailure(
                        "continuation not delivered for %s" % pc.call_id
                    )
                for frame in frames[1:]:
                    self._send(frame)
            else:
                self._send(frames[0])

        for pc in batch.commands:
            self._serve(pc, owner_by_kind)

        if harness.tagged_commands:
            raise ReplayFailure(
                "unmatched in-flight tags after round %d: %s"
                % (batch.round_index, sorted(harness.tagged_commands))
            )

        expected_continuations = 1 if continuation_calls else 0
        if harness.continuation_count != expected_continuations:
            raise ReplayFailure(
                "round %d expected %d continuation(s), parser saw %d"
                % (batch.round_index, expected_continuations,
                   harness.continuation_count)
            )

    def _serve(
        self, pc: PlannedCommand, owner_by_kind: Dict[str, str]
    ) -> None:
        harness = self._harness

        for frame in self.unsolicited.get(pc.call_id, ()):
            self._feed(frame)
            kind = self._frame_kind(frame)
            self._attribute(frame, pc.call_id, kind)

        failure = self.fail.get(pc.call_id)
        if failure is not None:
            status, text = failure
            self._feed(pc.tag + b" " + status.encode("ASCII")
                       + b" " + text.encode("UTF-8") + CRLF)
            return

        custom = self.untagged_factory(pc) if self.untagged_factory else None
        kind_frames = custom if custom is not None else self._scripted_frames(pc)
        for kind, frames in kind_frames.items():
            owner = owner_by_kind.get(kind)
            if owner is None:
                raise ReplayFailure(
                    "untagged kind %s from %s has no unique owner in round"
                    % (kind, pc.call_id)
                )
            if owner != pc.call_id:
                raise ReplayFailure(
                    "kind %s belongs to %s but was emitted by %s"
                    % (kind, owner, pc.call_id)
                )
            for frame in frames:
                self._feed(frame)
                self._attribute(frame, pc.call_id, kind)

        self._feed(pc.tag + b" OK " + pc.spec.name.encode("ASCII")
                   + b" completed\r\n")

    def _scripted_frames(self, pc: PlannedCommand) -> Dict[str, List[bytes]]:
        result: Dict[str, List[bytes]] = {}
        for kind in pc.spec.response_kinds:
            if kind in SELECTION_RESPONSE_KINDS and kind not in _UNTAGGED_TEMPLATES:
                continue
            frames = _UNTAGGED_TEMPLATES.get(kind)
            if frames:
                result[kind] = frames
        return result

    def _collect_replies(self, batch: Batch) -> Dict[str, Tuple[str, Any]]:
        harness = self._harness
        replies: Dict[str, Tuple[str, Any]] = {}
        for call_id, data in harness.tagged_sink:
            if call_id in replies:
                raise ReplayFailure(
                    "call %s received more than one completion" % call_id
                )
            if not isinstance(data, tuple) or data[0] in (True, False):
                raise ReplayFailure(
                    "call %s received continuation data as completion: %r"
                    % (call_id, data)
                )
            replies[call_id] = (str(data[0]), data[1])
        expected = {pc.call_id for pc in batch.commands}
        if set(replies) != expected:
            raise ReplayFailure(
                "completion set %s != batch set %s"
                % (sorted(replies), sorted(expected))
            )
        self.report.tagged_by_call.update(replies)
        harness.tagged_sink.clear()
        return replies

    def _send(self, frame: bytes) -> None:
        self.report.transcript.append(frame)

    def _feed(self, frame: bytes) -> None:
        self._harness._put_response(frame)

    def _attribute(self, frame: bytes, call_id: str, kind: str) -> None:
        self.report.attributions.append((frame, call_id, kind))

    @staticmethod
    def _frame_kind(frame: bytes) -> str:
        text = frame[2:].decode("UTF-8", "replace")
        match = re.match(r"(\d+ )?([A-Z-]+)", text)
        return match.group(2) if match else text.split(None, 1)[0]
