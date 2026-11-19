#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Offline replay of a batch of MPD directory queries and queue actions.

Semantics demonstrated:
- Per-item confirmation: an item is CONFIRMED only when the (mocked) server
  actually acknowledged it with OK; nothing is marked executed from plan.
- Stop-on-error: after a CommandError the pass aborts; remaining items stay
  NOT-EXECUTED.
- Idempotent retry: a retry pass skips items already CONFIRMED and never
  re-applies them (verified against the commands actually sent).
- Reporting keeps the original request order and identity (op id + command).
"""

import itertools
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple
from unittest import mock

import mpd
from mpd.base import CommandError

# ---------------------------------------------------------------- batch ----


@dataclass(frozen=True)
class Op:
    op_id: str
    kind: str  # "dir-query" | "queue-action"
    command: str
    args: Tuple[Any, ...] = ()

    @property
    def label(self) -> str:
        quoted = " ".join('"{}"'.format(a) for a in self.args)
        return "{} {}".format(self.command, quoted).strip()


BATCH: List[Op] = [
    Op("op-1", "dir-query", "lsinfo", ("/",)),
    Op("op-2", "dir-query", "listall", ("Rock",)),
    Op("op-3", "queue-action", "add", ("Rock/song1.mp3",)),
    Op("op-4", "queue-action", "add", ("Jazz/song2.mp3",)),
    Op("op-5", "queue-action", "move", (0, 1)),
    Op("op-6", "queue-action", "delete", (1,)),
    Op("op-7", "dir-query", "lsinfo", ("Rock",)),
]

# ------------------------------------------------------- scripted server ----
# Response lines the offline "server" returns, per pass. Pass 1 fails at
# op-4 (ACK 50 = NO_EXIST); the pass aborts there. Pass 2 (retry) only
# contains responses for the not-yet-confirmed items.

PASS_1_RESPONSES = [
    # op-1 lsinfo "/"
    "directory: /Rock\n",
    "directory: /Jazz\n",
    "playlist: favorites\n",
    "OK\n",
    # op-2 listall "Rock"
    "directory: Rock\n",
    "file: Rock/song1.mp3\n",
    "file: Rock/song3.flac\n",
    "OK\n",
    # op-3 add "Rock/song1.mp3"
    "OK\n",
    # op-4 add "Jazz/song2.mp3" -> failure, pass aborts here
    'ACK [50@0] {add} No such file or directory\n',
]

PASS_2_RESPONSES = [
    # op-4 retry: file exists now
    "OK\n",
    # op-5 move 0 1
    "OK\n",
    # op-6 delete 1
    "OK\n",
    # op-7 lsinfo "Rock"
    "directory: Rock\n",
    "file: Rock/song1.mp3\n",
    "file: Rock/song3.flac\n",
    "OK\n",
]

# ------------------------------------------------------------ status model ----

CONFIRMED = "CONFIRMED"
FAILED = "FAILED"
NOT_EXECUTED = "NOT-EXECUTED"


@dataclass
class ItemResult:
    op: Op
    status: str = NOT_EXECUTED
    confirmed_pass: Optional[int] = None
    failure: Optional[CommandError] = None
    failed_pass: Optional[int] = None
    result: Any = None
    history: List[str] = field(default_factory=list)


# ------------------------------------------------------------ offline rig ----


def make_offline_client():
    """MPDClient wired to a mocked socket, as in mpd/tests.py."""
    patcher = mock.patch("mpd.base.socket")
    socket_mock = patcher.start()
    socket_mock.getaddrinfo.return_value = [range(5)]
    socket_mock.socket.side_effect = lambda *a, **kw: mock.MagicMock(
        name="socket.socket"
    )
    client = mpd.MPDClient()
    client.connect("example.com", 10000)
    client._sock.reset_mock()
    return client, patcher


def feed(client, lines):
    """Queue response lines; afterwards behave as disconnected."""
    stream = itertools.chain(lines, itertools.repeat(""))
    client._rbfile.readline.side_effect = (x.encode("utf-8") for x in stream)


def sent_commands(client):
    """Command lines actually written to the server since last reset."""
    return [call.args[0] for call in client._wfile.write.call_args_list]


# ---------------------------------------------------------------- driver ----


def run_pass(client, results, pass_no):
    """Execute pending items in original order; abort at first failure."""
    sent_before = len(sent_commands(client))
    for res in results:  # original request order
        if res.status == CONFIRMED:
            # retry semantics: never re-apply an already confirmed action
            res.history.append(
                "pass {}: skipped (already confirmed in pass {})".format(
                    pass_no, res.confirmed_pass
                )
            )
            continue
        try:
            value = getattr(client, res.op.command)(*res.op.args)
        except CommandError as err:
            res.status = FAILED
            res.failure = err
            res.failed_pass = pass_no
            res.history.append(
                "pass {}: ACK [{}@{}] {{{}}} {}".format(
                    pass_no, err.errno.value, err.offset, err.command, err.msg
                )
            )
            return  # stop-on-error: later items remain NOT-EXECUTED
        res.status = CONFIRMED
        res.confirmed_pass = pass_no
        res.result = value
        res.history.append("pass {}: OK (confirmed)".format(pass_no))
    return sent_commands(client)[sent_before:]


def main():
    client, patcher = make_offline_client()
    results = [ItemResult(op) for op in BATCH]

    # pass 1
    feed(client, PASS_1_RESPONSES)
    client._wfile.write.reset_mock()
    run_pass(client, results, pass_no=1)
    pass1_sent = sent_commands(client)

    # pass 2 (retry): skip confirmed, re-attempt the rest
    feed(client, PASS_2_RESPONSES)
    client._wfile.write.reset_mock()
    run_pass(client, results, pass_no=2)
    pass2_sent = sent_commands(client)

    patcher.stop()

    # ---------------------------------------------------------- report ----
    print("== commands actually sent (pass 1) ==")
    for line in pass1_sent:
        print("  " + line)
    print("== commands actually sent (pass 2, retry) ==")
    for line in pass2_sent:
        print("  " + line)

    confirmed_twice = [
        r.op.op_id
        for r in results
        if r.status == CONFIRMED
        and r.confirmed_pass == 2
        and any("skipped" in h for h in r.history)
    ]
    assert not confirmed_twice, "re-applied a confirmed action: %s" % confirmed_twice
    # no command confirmed in pass 1 was sent again in the retry pass
    confirmed_lines = {
        r.op.label + "\n" for r in results if r.confirmed_pass == 1
    }
    # server writes use the same "cmd args" rendering as Op.label
    reapplied = [line for line in pass2_sent if line in confirmed_lines]
    assert not reapplied, "confirmed command re-applied: %s" % reapplied

    print("\n== per-item result (original request order) ==")
    for res in results:
        op = res.op
        detail = ""
        if res.status == CONFIRMED:
            if isinstance(res.result, list):
                detail = " -> {} entries".format(len(res.result))
            note = "pass {}".format(res.confirmed_pass)
        elif res.status == FAILED:
            err = res.failure
            detail = " -> ACK [{}@{}] {{{}}} {}".format(
                err.errno.value, err.offset, err.command, err.msg
            )
            note = "pass {}".format(res.failed_pass)
        else:
            note = "aborted before execution"
        print(
            "  {} {:<13} {:<28} {:<12} ({}){}".format(
                op.op_id, op.kind, op.label, res.status, note, detail
            )
        )
        for entry in res.history:
            print("      - " + entry)

    print("\n== summary ==")
    print("  confirmed   :", [r.op.op_id for r in results if r.status == CONFIRMED])
    print("  failed      :", [r.op.op_id for r in results if r.status == FAILED])
    print("  not executed:", [r.op.op_id for r in results if r.status == NOT_EXECUTED])


if __name__ == "__main__":
    main()
