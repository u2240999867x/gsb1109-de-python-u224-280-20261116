import pytest

from imaplib2.scheduling import (
    AUTH,
    BLOCKED,
    DONE,
    LOGOUT,
    NONAUTH,
    SELECTED,
    AmbiguousResponseError,
    IllegalStateError,
    PipelinePlanner,
    UnreachableDependencyError,
    make_spec,
)


def call_ids(batch):
    return [pc.call_id for pc in batch.commands]


def all_batches(planner):
    """Drive the happy path: plan a round, account OK replies, repeat."""
    batches = []
    while True:
        batch = planner.next_batch()
        if batch is None:
            break
        batches.append(batch)
        planner.account_responses(
            {pc.call_id: ("OK", b"") for pc in batch.commands}
        )
    return batches


def test_independent_distinct_reads_share_one_round():
    specs = [
        make_spec("a", "LIST", '""', "*", state=AUTH,
                  reads=("mailboxes",), response_kinds=("LIST",)),
        make_spec("b", "NAMESPACE", state=AUTH,
                  reads=("namespaces",), response_kinds=("NAMESPACE",)),
        make_spec("c", "CAPABILITY", state=AUTH,
                  reads=("capabilities",), response_kinds=("CAPABILITY",)),
    ]
    planner = PipelinePlanner(specs, initial_state=AUTH)
    batches = all_batches(planner)

    assert len(batches) == 1
    assert call_ids(batches[0]) == ["a", "b", "c"]


def test_tags_are_unique_and_stable():
    specs = [make_spec("c%d" % i, "NOOP", state=AUTH,
                       response_kinds=()) for i in range(5)]
    planner = PipelinePlanner(specs, initial_state=AUTH)
    tags = [pc.tag for pc in planner.commands.values()]

    assert len(set(tags)) == 5
    assert all(tag.startswith(b"A") for tag in tags)
    all_batches(planner)
    assert all(pc.status == DONE for pc in planner.commands.values())


def test_indistinguishable_reply_kinds_force_split():
    specs = [
        make_spec("f1", "FETCH", "1", "(FLAGS)", state=SELECTED,
                  mailbox="INBOX", reads=("messages",),
                  response_kinds=("FETCH",)),
        make_spec("f2", "FETCH", "2", "(FLAGS)", state=SELECTED,
                  mailbox="INBOX", reads=("messages",),
                  response_kinds=("FETCH",)),
    ]
    planner = PipelinePlanner(specs, initial_state=SELECTED,
                              initial_mailbox="INBOX")
    batches = all_batches(planner)

    assert [call_ids(b) for b in batches] == [["f1"], ["f2"]]
    assert batches[0].exclusions["f2"] == "indistinguishable-reply"


def test_write_after_write_conflict_forces_split():
    specs = [
        make_spec("s1", "STORE", "1", "FLAGS", r"(\Seen)",
                  state=SELECTED, mailbox="INBOX",
                  writes=("flags:1",), response_kinds=("FETCH",)),
        make_spec("s2", "STORE", "2", "FLAGS", r"(\Seen)",
                  state=SELECTED, mailbox="INBOX",
                  writes=("flags:2",), response_kinds=("FETCH",)),
    ]
    planner = PipelinePlanner(specs, initial_state=SELECTED,
                              initial_mailbox="INBOX")
    batches = all_batches(planner)

    assert len(batches) == 2
    assert batches[0].exclusions["s2"] in (
        "state-conflict",
        "indistinguishable-reply",
    )


def test_read_after_write_conflict_forces_split_but_ro_reads_coalesce():
    specs = [
        make_spec("w", "STORE", "1", "FLAGS", r"(\Seen)",
                  state=SELECTED, mailbox="INBOX",
                  writes=("flags:1",), response_kinds=("FETCH",)),
        make_spec("r", "FETCH", "1", "(UID)", state=SELECTED,
                  mailbox="INBOX", reads=("flags:1",),
                  response_kinds=("FETCH",)),
        make_spec("search", "SEARCH", None, "ALL", state=SELECTED,
                  mailbox="INBOX", reads=("index",),
                  response_kinds=("SEARCH",)),
    ]
    planner = PipelinePlanner(specs, initial_state=SELECTED,
                              initial_mailbox="INBOX")
    batches = all_batches(planner)

    assert call_ids(batches[0]) == ["w", "search"]
    assert call_ids(batches[1]) == ["r"]
    assert batches[0].exclusions["r"] == "state-conflict"


def test_select_is_singleton_and_transitions_state():
    specs = [
        make_spec("lst", "LIST", '""', "*", state=AUTH,
                  reads=("mailboxes",), response_kinds=("LIST",)),
        make_spec("sel", "SELECT", "INBOX", state=AUTH,
                  mailbox="INBOX", writes=("selected",)),
        make_spec("f", "FETCH", "1", "(FLAGS)", state=SELECTED,
                  mailbox="INBOX", reads=("messages",),
                  response_kinds=("FETCH",)),
    ]
    planner = PipelinePlanner(specs, initial_state=AUTH)
    first = planner.next_batch()
    assert call_ids(first) == ["lst"]
    planner.account_responses({"lst": ("OK", b"")})

    second = planner.next_batch()
    assert call_ids(second) == ["sel"]
    result = planner.account_responses({"sel": ("OK", b"")})

    assert result.state == SELECTED
    assert result.mailbox == "INBOX"

    third = planner.next_batch()
    assert call_ids(third) == ["f"]
    assert second.exclusions == {} or "f" in second.exclusions


def test_examine_also_forms_selection_boundary():
    spec = make_spec("ex", "EXAMINE", "INBOX", state=AUTH,
                     mailbox="INBOX")
    planner = PipelinePlanner([spec], initial_state=AUTH)
    batch = planner.next_batch()

    assert call_ids(batch) == ["ex"]
    assert batch.commands[0].spec.is_selection


def test_idle_is_singleton_barrier():
    specs = [
        make_spec("noop", "NOOP", state=SELECTED, mailbox="INBOX",
                  unsolicited=True, response_kinds=()),
        make_spec("idle", "IDLE", state=SELECTED, mailbox="INBOX",
                  reads=("events",)),
    ]
    planner = PipelinePlanner(specs, initial_state=SELECTED,
                              initial_mailbox="INBOX")
    batches = all_batches(planner)

    assert [call_ids(b) for b in batches] == [["noop"], ["idle"]]
    assert batches[0].exclusions["idle"] == "idle-barrier"


def test_literal_continuation_is_singleton_and_two_frames():
    spec = make_spec("app", "APPEND", "INBOX", None, None,
                     state=AUTH, mailbox="INBOX",
                     writes=("mailbox:INBOX",),
                     literal=b"Subject: test\r\n\r\nbody\r\n")
    planner = PipelinePlanner([spec], initial_state=AUTH)
    batch = planner.next_batch()
    pc = batch.commands[0]

    assert call_ids(batch) == ["app"]
    frames = planner.wire_frames(pc)
    assert frames[0] == b"A0 APPEND INBOX {23}\r\n"
    assert frames[1] == b"Subject: test\r\n\r\nbody\r\n\r\n"


def test_dependency_runs_in_same_round_when_compatible():
    specs = [
        make_spec("search", "SEARCH", None, "ALL", state=SELECTED,
                  mailbox="INBOX", reads=("index",),
                  response_kinds=("SEARCH",)),
        make_spec("fetch", "FETCH", "1", "(UID)", state=SELECTED,
                  mailbox="INBOX", reads=("messages",),
                  response_kinds=("FETCH",), deps=("search",)),
    ]
    planner = PipelinePlanner(specs, initial_state=SELECTED,
                              initial_mailbox="INBOX")
    batches = all_batches(planner)

    assert len(batches) == 1
    assert call_ids(batches[0]) == ["search", "fetch"]


def test_dependency_with_barrier_moves_to_next_round():
    specs = [
        make_spec("sel", "SELECT", "INBOX", state=AUTH,
                  mailbox="INBOX", writes=("selected",)),
        make_spec("fetch", "FETCH", "1", "(UID)", state=SELECTED,
                  mailbox="INBOX", reads=("messages",),
                  response_kinds=("FETCH",), deps=("sel",)),
    ]
    planner = PipelinePlanner(specs, initial_state=AUTH)
    batches = all_batches(planner)

    assert [call_ids(b) for b in batches] == [["sel"], ["fetch"]]


def test_failure_blocks_dependents_and_replans_rest():
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
    planner = PipelinePlanner(specs, initial_state=AUTH)

    first = planner.next_batch()
    assert call_ids(first) == ["lst"]
    planner.account_responses({"lst": ("OK", b"")})

    second = planner.next_batch()
    assert call_ids(second) == ["sel"]
    result = planner.account_responses({
        "sel": ("NO", b"no such mailbox"),
    })

    assert result.failed == ["sel"]
    assert set(result.blocked) == {"f1", "f2"}
    assert planner.commands["f1"].status == BLOCKED
    assert planner.commands["f2"].status == BLOCKED
    assert planner.commands["lst"].status == DONE
    assert planner.next_batch() is None


def test_failed_non_dependency_does_not_block_unrelated_calls():
    specs = [
        make_spec("f1", "FETCH", "1", "(UID)", state=SELECTED,
                  mailbox="INBOX", reads=("messages:1",),
                  response_kinds=("FETCH",)),
        make_spec("s", "SEARCH", None, "ALL", state=SELECTED,
                  mailbox="INBOX", reads=("index",),
                  response_kinds=("SEARCH",)),
    ]
    planner = PipelinePlanner(specs, initial_state=SELECTED,
                              initial_mailbox="INBOX")
    planner.next_batch()
    result = planner.account_responses({"f1": ("BAD", b"boom"),
                                        "s": ("OK", b"")})

    assert result.failed == ["f1"]
    assert result.blocked == []
    assert planner.next_batch() is None


def test_close_and_logout_transitions():
    specs = [
        make_spec("close", "CLOSE", state=SELECTED, mailbox="INBOX",
                  writes=("selected",)),
        make_spec("logout", "LOGOUT", state=AUTH),
    ]
    planner = PipelinePlanner(specs, initial_state=SELECTED,
                              initial_mailbox="INBOX")
    planner.next_batch()
    result = planner.account_responses({"close": ("OK", b"")})
    assert result.state == AUTH
    assert result.mailbox is None

    planner.next_batch()
    result = planner.account_responses({"logout": ("OK", b"")})
    assert result.state == LOGOUT


def test_unexpected_state_change_triggers_replan():
    specs = [
        make_spec("f", "FETCH", "1", "(UID)", state=SELECTED,
                  mailbox="INBOX", reads=("messages",),
                  response_kinds=("FETCH",)),
        make_spec("sel", "SELECT", "INBOX", state=AUTH,
                  mailbox="INBOX", writes=("selected",)),
    ]
    planner = PipelinePlanner(specs, initial_state=SELECTED,
                              initial_mailbox="INBOX")
    planner.note_state_change(AUTH)

    batch = planner.next_batch()
    assert call_ids(batch) == ["sel"]
    planner.account_responses({"sel": ("OK", b"")})

    batch = planner.next_batch()
    assert call_ids(batch) == ["f"]


def test_illegal_state_raises_when_nothing_admittable():
    specs = [
        make_spec("f", "FETCH", "1", "(UID)", state=SELECTED,
                  mailbox="INBOX", response_kinds=("FETCH",)),
    ]
    planner = PipelinePlanner(specs, initial_state=NONAUTH)

    with pytest.raises(IllegalStateError):
        planner.next_batch()


def test_duplicate_call_id_rejected():
    specs = [
        make_spec("x", "NOOP", state=AUTH, response_kinds=()),
        make_spec("x", "NOOP", state=AUTH, response_kinds=()),
    ]
    with pytest.raises(ValueError):
        PipelinePlanner(specs, initial_state=AUTH)


def test_unknown_dependency_rejected():
    specs = [make_spec("x", "NOOP", state=AUTH,
                       response_kinds=(), deps=("ghost",))]
    with pytest.raises(UnreachableDependencyError):
        PipelinePlanner(specs, initial_state=AUTH)


def test_dependency_cycle_rejected():
    specs = [
        make_spec("a", "NOOP", state=AUTH, response_kinds=(),
                  deps=("b",)),
        make_spec("b", "NOOP", state=AUTH, response_kinds=(),
                  deps=("a",)),
    ]
    with pytest.raises(UnreachableDependencyError):
        PipelinePlanner(specs, initial_state=AUTH)


def test_reply_for_unknown_or_unawaited_call_rejected():
    specs = [make_spec("a", "NOOP", state=AUTH, response_kinds=())]
    planner = PipelinePlanner(specs, initial_state=AUTH)
    planner.next_batch()

    with pytest.raises(AmbiguousResponseError):
        planner.account_responses({"ghost": ("OK", b"")})

    planner.account_responses({"a": ("OK", b"")})
    with pytest.raises(AmbiguousResponseError):
        planner.account_responses({"a": ("OK", b"")})


def test_make_spec_derives_continuation_and_selection_flags():
    idle = make_spec("i", "IDLE", state=SELECTED, mailbox="INBOX")
    append = make_spec("a", "APPEND", "INBOX", state=AUTH,
                       literal=b"x")
    fetch = make_spec("f", "FETCH", "1", "(UID)", state=SELECTED,
                      mailbox="INBOX")

    assert idle.is_idle and idle.requires_continuation
    assert append.requires_continuation and not append.is_selection
    assert not fetch.requires_continuation
    assert fetch.response_kinds == frozenset({"FETCH"})


def test_make_spec_uid_response_kind_derivation():
    direct = make_spec("u1", "UID", "SEARCH", "ALL", state=SELECTED,
                       mailbox="INBOX")
    fetch = make_spec("u2", "UID", "FETCH", "1", "UID", state=SELECTED,
                      mailbox="INBOX")

    assert direct.response_kinds == frozenset({"SEARCH"})
    assert fetch.response_kinds == frozenset({"FETCH"})


def test_make_spec_rejects_illegal_state():
    with pytest.raises(ValueError):
        make_spec("f", "FETCH", "1", "(UID)", state=AUTH)


def test_make_spec_requires_state_for_multi_state_commands():
    with pytest.raises(ValueError):
        make_spec("s", "SELECT", "INBOX")


def test_mailbox_mismatch_blocks_selected_commands():
    specs = [
        make_spec("f1", "FETCH", "1", "(UID)", state=SELECTED,
                  mailbox="INBOX", response_kinds=("FETCH",)),
        make_spec("f2", "FETCH", "1", "(UID)", state=SELECTED,
                  mailbox="Archive", response_kinds=("FETCH",)),
    ]
    planner = PipelinePlanner(specs, initial_state=SELECTED,
                              initial_mailbox="INBOX")
    batch = planner.next_batch()

    assert call_ids(batch) == ["f1"]
    assert batch.exclusions["f2"] == "illegal-mailbox-state"


def test_round_plan_is_deterministic():
    def build():
        specs = [
            make_spec("n%d" % i, "NOOP", state=AUTH,
                      response_kinds=(), unsolicited=False)
            for i in range(4)
        ]
        planner = PipelinePlanner(specs, initial_state=AUTH)
        return [call_ids(b) for b in all_batches(planner)]

    assert build() == build()


def test_minimality_audit_trail_records_every_boundary():
    specs = [
        make_spec("sel", "SELECT", "INBOX", state=AUTH,
                  mailbox="INBOX", writes=("selected",)),
        make_spec("idle", "IDLE", state=SELECTED, mailbox="INBOX"),
        make_spec("f", "FETCH", "1", "(UID)", state=SELECTED,
                  mailbox="INBOX", response_kinds=("FETCH",)),
    ]
    planner = PipelinePlanner(specs, initial_state=AUTH)
    all_batches(planner)

    reasons = planner.boundary_reasons()
    assert len(reasons) == 3
    flat = {cid for round_reasons in reasons for cid in round_reasons}
    assert {"idle", "f"} & flat


def test_pending_excludes_accounted_commands():
    specs = [
        make_spec("a", "NOOP", state=AUTH, response_kinds=()),
        make_spec("b", "NOOP", state=AUTH, response_kinds=()),
    ]
    planner = PipelinePlanner(specs, initial_state=AUTH)
    batch = planner.next_batch()
    planner.account_responses({pc.call_id: ("OK", b"")
                               for pc in batch.commands})

    assert planner.pending() == []
    assert all(pc.status == DONE for pc in planner.commands.values())
