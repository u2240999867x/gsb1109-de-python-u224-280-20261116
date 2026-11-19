import pytest

from imaplib2.replay import LocalReplay, ReplayFailure
from imaplib2.scheduling import (
    AUTH,
    BLOCKED,
    DONE,
    FAILED,
    SELECTED,
    PipelinePlanner,
    make_spec,
)


def make_planner(specs, state=AUTH, mailbox=None):
    return PipelinePlanner(specs, initial_state=state,
                           initial_mailbox=mailbox)


def test_every_completion_returns_to_unique_call():
    specs = [
        make_spec("lst", "LIST", '""', "*", state=AUTH,
                  reads=("mailboxes",), response_kinds=("LIST",)),
        make_spec("ns", "NAMESPACE", state=AUTH,
                  reads=("namespaces",), response_kinds=("NAMESPACE",)),
        make_spec("cap", "CAPABILITY", state=AUTH,
                  reads=("caps",), response_kinds=("CAPABILITY",)),
    ]
    planner = make_planner(specs)
    report = LocalReplay(planner).run()

    assert report.rounds == 1
    assert set(report.tagged_by_call) == {"lst", "ns", "cap"}
    assert all(status == "OK" for status, _ in report.tagged_by_call.values())
    owners = {call_id for _, call_id, _ in report.attributions}
    assert owners <= {"lst", "ns", "cap"}
    assert all(status == DONE for status in report.final_status.values())


def test_untagged_attribution_matches_round_owner():
    specs = [
        make_spec("sel", "SELECT", "INBOX", state=AUTH,
                  mailbox="INBOX", writes=("selected",)),
        make_spec("search", "SEARCH", None, "ALL", state=SELECTED,
                  mailbox="INBOX", reads=("index",),
                  response_kinds=("SEARCH",)),
        make_spec("fetch", "FETCH", "1", "(FLAGS)", state=SELECTED,
                  mailbox="INBOX", reads=("messages",),
                  response_kinds=("FETCH",)),
    ]
    planner = make_planner(specs)
    report = LocalReplay(planner).run()

    for frame, call_id, kind in report.attributions:
        if kind == "SEARCH":
            assert call_id == "search"
        if kind == "FETCH":
            assert call_id == "fetch"

    assert report.final_status["search"] == DONE
    assert report.final_status["fetch"] == DONE


def test_append_continuation_waits_before_literal():
    payload = b"Subject: hi\r\n\r\nbody\r\n"
    specs = [
        make_spec("app", "APPEND", "INBOX", None, None, state=AUTH,
                  mailbox="INBOX", writes=("mailbox:INBOX",),
                  literal=payload),
    ]
    planner = make_planner(specs)
    report = LocalReplay(planner).run()

    transcript = report.transcript
    assert transcript[0] == b"A0 APPEND INBOX {" + str(len(payload)).encode() \
        + b"}\r\n"
    assert transcript[1] == payload + b"\r\n"
    assert report.rounds == 1
    assert report.final_status["app"] == DONE


def test_idle_round_is_singleton_and_uses_continuation():
    specs = [
        make_spec("noop", "NOOP", state=SELECTED, mailbox="INBOX",
                  response_kinds=()),
        make_spec("idle", "IDLE", state=SELECTED, mailbox="INBOX",
                  reads=("events",)),
    ]
    planner = make_planner(specs, state=SELECTED, mailbox="INBOX")
    report = LocalReplay(planner).run()

    assert report.batches == [["noop"], ["idle"]]
    idle_frames = [f for f in report.transcript if b"IDLE" in f]
    assert idle_frames == [b"A1 IDLE\r\n"]


def test_failure_replans_and_accounts_sent_calls_separately():
    specs = [
        make_spec("sel", "SELECT", "INBOX", state=AUTH,
                  mailbox="INBOX", writes=("selected",)),
        make_spec("f1", "FETCH", "1", "(UID)", state=SELECTED,
                  mailbox="INBOX", reads=("messages",),
                  response_kinds=("FETCH",), deps=("sel",)),
        make_spec("f2", "FETCH", "2", "(UID)", state=SELECTED,
                  mailbox="INBOX", reads=("messages",),
                  response_kinds=("FETCH",), deps=("f1",)),
        make_spec("lst", "LIST", '""', "*", state=AUTH,
                  reads=("mailboxes",), response_kinds=("LIST",)),
    ]
    planner = make_planner(specs)
    report = LocalReplay(
        planner, fail={"sel": ("NO", "no such mailbox")}
    ).run()

    assert report.final_status["sel"] == FAILED
    assert report.final_status["lst"] == DONE
    assert report.final_status["f1"] == BLOCKED
    assert report.final_status["f2"] == BLOCKED
    assert report.batches[0] == ["lst"]
    assert report.batches[1] == ["sel"]


def test_unsolicited_notification_is_attributed_to_its_round():
    specs = [
        make_spec("noop", "NOOP", state=SELECTED, mailbox="INBOX",
                  response_kinds=()),
        make_spec("f", "FETCH", "1", "(FLAGS)", state=SELECTED,
                  mailbox="INBOX", reads=("messages",),
                  response_kinds=("FETCH",)),
    ]
    planner = make_planner(specs, state=SELECTED, mailbox="INBOX")
    report = LocalReplay(
        planner,
        unsolicited={"noop": [b"* 3 EXISTS\r\n", b"* 2 EXPUNGE\r\n"]},
    ).run()

    kinds = {kind for _, call_id, kind in report.attributions
             if call_id == "noop"}
    assert {"EXISTS", "EXPUNGE"} <= kinds


def test_server_literal_is_parsed_by_real_parser():
    specs = [
        make_spec("f", "FETCH", "1", "(BODY[])", state=SELECTED,
                  mailbox="INBOX", reads=("messages",),
                  response_kinds=("FETCH",)),
    ]
    planner = make_planner(specs, state=SELECTED, mailbox="INBOX")
    replay = LocalReplay(planner)
    replay.run()

    fetch_entries = [
        data for typ, data in replay._harness.untagged_log
        if typ == "FETCH"
    ]
    assert fetch_entries
    assert any(isinstance(entry, tuple) for entry in fetch_entries)


def test_unowned_untagged_kind_fails_replay():
    specs = [
        make_spec("noop", "NOOP", state=AUTH, response_kinds=()),
    ]
    planner = make_planner(specs)

    def factory(pc):
        return {"MYSTERY": [b"* MYSTERY x\r\n"]}

    with pytest.raises(ReplayFailure):
        LocalReplay(planner, untagged_factory=factory).run()


def test_wrong_owner_emission_fails_replay():
    specs = [
        make_spec("s", "SEARCH", None, "ALL", state=SELECTED,
                  mailbox="INBOX", response_kinds=("SEARCH",)),
    ]
    planner = make_planner(specs, state=SELECTED, mailbox="INBOX")

    def factory(pc):
        return {"SEARCH": [b"* SEARCH 9\r\n"]}

    LocalReplay(planner, untagged_factory=factory).run()

    planner2 = make_planner([
        make_spec("s", "SEARCH", None, "ALL", state=SELECTED,
                  mailbox="INBOX", response_kinds=("SEARCH",)),
    ], state=SELECTED, mailbox="INBOX")

    def wrong_factory(pc):
        return {"FETCH": [b"* 1 FETCH (UID 1)\r\n"]}

    with pytest.raises(ReplayFailure):
        LocalReplay(planner2, untagged_factory=wrong_factory).run()


def test_transcript_tags_are_unique_per_call():
    specs = [
        make_spec("a%d" % i, "NOOP", state=AUTH, response_kinds=(),
                  unsolicited=False)
        for i in range(4)
    ]
    planner = make_planner(specs)
    report = LocalReplay(planner).run()

    tags = {frame.split(b" ", 1)[0] for frame in report.transcript}
    assert tags == {b"A0", b"A1", b"A2", b"A3"}


def test_full_scenario_round_count_and_final_accounting():
    specs = [
        make_spec("login", "LOGIN", "u", "p", state="NONAUTH",
                  writes=("auth",)),
        make_spec("lst", "LIST", '""', "*", state=AUTH,
                  reads=("mailboxes",), response_kinds=("LIST",)),
        make_spec("sel", "SELECT", "INBOX", state=AUTH,
                  mailbox="INBOX", writes=("selected",),
                  deps=("login",)),
        make_spec("search", "SEARCH", None, "ALL", state=SELECTED,
                  mailbox="INBOX", reads=("index",),
                  response_kinds=("SEARCH",), deps=("sel",)),
        make_spec("fetch", "FETCH", "1", "(UID)", state=SELECTED,
                  mailbox="INBOX", reads=("messages",),
                  response_kinds=("FETCH",), deps=("search",)),
        make_spec("fetch2", "FETCH", "2", "(UID)", state=SELECTED,
                  mailbox="INBOX", reads=("messages",),
                  response_kinds=("FETCH",), deps=("sel",)),
        make_spec("idle", "IDLE", state=SELECTED, mailbox="INBOX",
                  deps=("sel",)),
        make_spec("close", "CLOSE", state=SELECTED, mailbox="INBOX",
                  writes=("selected",), deps=("idle",)),
        make_spec("logout", "LOGOUT", state=AUTH, deps=("close",)),
    ]
    planner = make_planner(specs, state="NONAUTH")
    report = LocalReplay(planner).run()

    # Six is the proven minimum for this dependency/barrier graph; multiple
    # equally-optimal tie-broken orderings exist, so assert structure rather
    # than one specific batch sequence.
    assert report.rounds == 6
    assert planner.exact_used is True
    assert set(report.final_status.values()) == {DONE}

    scheduled = [call_id for batch in report.batches for call_id in batch]
    assert sorted(scheduled) == sorted(planner.commands)

    def round_of(call_id):
        for index, batch in enumerate(report.batches):
            if call_id in batch:
                return index
        raise AssertionError("call not scheduled: %s" % call_id)

    # Barrier commands must each occupy a round on their own.
    for barrier in ("login", "sel", "idle"):
        assert report.batches[round_of(barrier)] == [barrier]

    # Business dependency ordering is preserved (a dependency may share a
    # round when compatible, but can never run later than its dependent).
    assert round_of("sel") > round_of("login")
    assert round_of("search") > round_of("sel")
    assert round_of("fetch") >= round_of("search")
    assert round_of("fetch2") > round_of("sel")
    assert round_of("close") > round_of("idle")
    assert round_of("logout") > round_of("close")

    for frame, call_id, kind in report.attributions:
        assert call_id in planner.commands
