"""Tests for IMAP disconnect recovery planning."""

import pytest

from imaplib2.recovery import (
    ACTION_REFETCH,
    ACTION_REISSUE_COPY,
    ACTION_REISSUE_MOVE,
    ACTION_REISSUE_STORE,
    ACTION_VERIFY_COPY,
    ACTION_VERIFY_FLAGS,
    ACTION_VERIFY_MOVE,
    CopyUidRecord,
    InterruptedCommand,
    MailboxDomain,
    MessageEvidence,
    RecordedResponse,
    RecoveryPlanner,
    ServerEvidence,
    STATUS_FAILED,
    STATUS_PENDING_CAPABILITY,
    STATUS_PENDING_MANUAL,
    STATUS_PENDING_VERIFICATION,
    STATUS_RESOLVED,
)


INBOX = "INBOX"
ARCHIVE = "Archive"
OLD_UIDVALIDITY = 42


def tagged(tag, response_type, text=None, response_code=None,
           response_code_args=None):
    return RecordedResponse(
        kind="tagged",
        tag=tag,
        response_type=response_type,
        text=text,
        response_code=response_code,
        response_code_args=response_code_args,
    )


def untagged(response_type, response_code=None, response_code_args=None):
    return RecordedResponse(
        kind="untagged",
        tag=None,
        response_type=response_type,
        response_code=response_code,
        response_code_args=response_code_args,
    )


def planner(capabilities=("IMAP4REV1", "MOVE", "UIDPLUS", "CONDSTORE"),
            domains=None):
    domains = domains or {
        INBOX: MailboxDomain(INBOX, uidvalidity=OLD_UIDVALIDITY),
        ARCHIVE: MailboxDomain(ARCHIVE, uidvalidity=7),
    }
    return RecoveryPlanner(capabilities, domains)


def evidence(messages=None, absent=None, uidvalidity=None,
             sequence_to_uid=None):
    return ServerEvidence(
        messages=messages or {},
        absent_uids=absent or {},
        uidvalidity=uidvalidity or {INBOX: OLD_UIDVALIDITY, ARCHIVE: 7},
        sequence_to_uid=sequence_to_uid or {},
    )


# ---------------------------------------------------------------------
# Determined outcomes
# ---------------------------------------------------------------------


def test_tagged_ok_is_resolved_without_checks():
    command = InterruptedCommand(
        tag="A01", command="FETCH", args=("100", "(UID FLAGS)"),
        mailbox=INBOX, uidvalidity=OLD_UIDVALIDITY,
        responses=[tagged("A01", "OK")])
    item = planner().plan([command]).items[0]

    assert item.status == STATUS_RESOLVED
    assert item.reason == "tagged-ok"
    assert item.checks == []


def test_tagged_no_is_failed_and_never_re_sent():
    command = InterruptedCommand(
        tag="A02", command="STORE", args=("100", "+FLAGS", "(\\Seen)"),
        mailbox=INBOX, uidvalidity=OLD_UIDVALIDITY,
        responses=[tagged("A02", "NO", b"read-only mailbox")])
    plan = planner().plan([command])
    item = plan.items[0]

    assert item.status == STATUS_FAILED
    assert item.reason == "tagged-no"
    assert plan.steps == []


def test_tagged_bad_is_failed():
    command = InterruptedCommand(
        tag="A03", command="COPY", args=("100", ARCHIVE),
        mailbox=INBOX, responses=[tagged("A03", "BAD")])
    item = planner().plan([command]).items[0]

    assert item.status == STATUS_FAILED


# ---------------------------------------------------------------------
# FETCH / literal handling
# ---------------------------------------------------------------------


def test_fetch_with_fully_downloaded_literal_is_resolved():
    command = InterruptedCommand(
        tag="A04", command="FETCH", args=("100", "(RFC822)"),
        mailbox=INBOX, uidvalidity=OLD_UIDVALIDITY,
        literal_expected=5000, literal_received=5000)
    item = planner().plan([command]).items[0]

    assert item.status == STATUS_RESOLVED
    assert item.reason == "literal-complete"
    assert item.checks == []


def test_fetch_with_partial_literal_requires_refetch_with_offset():
    command = InterruptedCommand(
        tag="A05", command="FETCH", args=("100", "(RFC822)"),
        mailbox=INBOX, uidvalidity=OLD_UIDVALIDITY,
        literal_expected=5000, literal_received=3200)
    item = planner().plan([command]).items[0]

    assert item.status == STATUS_PENDING_VERIFICATION
    assert item.action == ACTION_REFETCH
    step = item.checks[0]
    assert step.imap_command == "FETCH 100 (RFC822)"
    assert "literal-from-offset-3200" in step.expects
    assert item.detail["offset"] == 3200


def test_fetch_tagged_ok_but_truncated_literal_still_refetches():
    command = InterruptedCommand(
        tag="A06", command="FETCH", args=("100", "(BODY[])"),
        mailbox=INBOX, uidvalidity=OLD_UIDVALIDITY,
        responses=[tagged("A06", "OK")],
        literal_expected=900, literal_received=10)
    item = planner().plan([command]).items[0]

    assert item.status == STATUS_PENDING_VERIFICATION
    assert item.action == ACTION_REFETCH


def test_fetch_without_any_response_is_simply_refetched():
    command = InterruptedCommand(
        tag="A07", command="UID", args=("FETCH", "100", "(FLAGS)"),
        mailbox=INBOX, uidvalidity=OLD_UIDVALIDITY)
    item = planner().plan([command]).items[0]

    assert item.status == STATUS_PENDING_VERIFICATION
    assert item.action == ACTION_REFETCH
    assert item.checks[0].imap_command == "UID FETCH 100 (FLAGS)"

    # The read stays pending until the caller reports the re-fetch ran.
    plan = planner().plan([command])
    planner().apply_evidence(plan, evidence())
    assert plan.items[0].status == STATUS_PENDING_VERIFICATION

    planner().apply_evidence(
        plan, evidence(), completed_checks=[("A07", ACTION_REFETCH)])
    assert plan.items[0].status == STATUS_RESOLVED


def test_other_read_commands_are_rerun():
    command = InterruptedCommand(
        tag="A08", command="SEARCH", args=("US-ASCII", "UNSEEN"),
        mailbox=INBOX, uidvalidity=OLD_UIDVALIDITY)
    item = planner().plan([command]).items[0]

    assert item.status == STATUS_PENDING_VERIFICATION
    assert item.checks[0].imap_command == "SEARCH US-ASCII UNSEEN"


# ---------------------------------------------------------------------
# STORE / flag operations
# ---------------------------------------------------------------------


def test_store_unknown_state_gets_verification_not_blanket_resend():
    command = InterruptedCommand(
        tag="A09", command="UID",
        args=("STORE", "100,101", "+FLAGS", "(\\Seen)"),
        mailbox=INBOX, uidvalidity=OLD_UIDVALIDITY)
    item = planner().plan([command]).items[0]

    assert item.status == STATUS_PENDING_VERIFICATION
    actions = [step.action for step in item.checks]
    assert "verify-uidvalidity" in actions
    assert ACTION_VERIFY_FLAGS in actions
    assert all(not step.reissue for step in item.checks)
    fetch = next(step for step in item.checks
                 if step.action == ACTION_VERIFY_FLAGS)
    assert fetch.imap_command == "UID FETCH 100,101 (UID FLAGS)"
    assert not any("reissue" in step.action for step in item.checks)


def test_store_confirmed_applied_is_resolved():
    command = InterruptedCommand(
        tag="A10", command="UID",
        args=("STORE", "100", "+FLAGS", "(\\Seen)"),
        mailbox=INBOX, uidvalidity=OLD_UIDVALIDITY)
    plan = planner().plan([command])
    result = planner().apply_evidence(plan, evidence({
        (INBOX, 100): MessageEvidence(100, flags=("\\Seen",)),
    }))
    item = result.items[0]

    assert item.status == STATUS_RESOLVED
    assert item.reason == "flags-confirmed-on-server"
    assert item.checks == []


def test_store_confirmed_missing_produces_gated_reissue():
    command = InterruptedCommand(
        tag="A11", command="UID",
        args=("STORE", "100", "-FLAGS", "(\\Junk)"),
        mailbox=INBOX, uidvalidity=OLD_UIDVALIDITY)
    plan = planner().plan([command])
    result = planner().apply_evidence(plan, evidence({
        (INBOX, 100): MessageEvidence(100, flags=("\\Junk",)),
    }))
    item = result.items[0]

    assert item.status == STATUS_PENDING_VERIFICATION
    assert len(item.checks) == 1
    step = item.checks[0]
    assert step.reissue is True
    assert step.action == ACTION_REISSUE_STORE
    assert step.imap_command == "UID STORE 100 -FLAGS (\\Junk)"


def test_store_with_changed_uidvalidity_goes_manual():
    command = InterruptedCommand(
        tag="A12", command="UID",
        args=("STORE", "100", "+FLAGS", "(\\Seen)"),
        mailbox=INBOX, uidvalidity=OLD_UIDVALIDITY)
    plan = planner(capabilities=("IMAP4REV1", "MOVE", "UIDPLUS", "CONDSTORE"),
                   domains={INBOX: MailboxDomain(INBOX, uidvalidity=99),
                            ARCHIVE: MailboxDomain(ARCHIVE, uidvalidity=7)}) \
        .plan([command])

    assert plan.items[0].status == STATUS_PENDING_MANUAL
    assert plan.items[0].reason == "uidvalidity-changed-or-unknown"


def test_store_sequence_wildcard_is_manual():
    command = InterruptedCommand(
        tag="A13", command="STORE", args=("*", "+FLAGS", "(\\Seen)"),
        mailbox=INBOX, uidvalidity=OLD_UIDVALIDITY)
    item = planner().plan([command]).items[0]

    assert item.status == STATUS_PENDING_MANUAL
    assert item.reason == "wildcard-sequence-set"


def test_store_condstore_requirement_blocks_without_capability():
    command = InterruptedCommand(
        tag="A14", command="UID",
        args=("STORE", "100", "(UNCHANGEDSINCE 5)", "+FLAGS", "(\\Seen)"),
        mailbox=INBOX, uidvalidity=OLD_UIDVALIDITY)
    weak = RecoveryPlanner(
        capabilities=("IMAP4REV1",),
        domains={INBOX: MailboxDomain(INBOX, uidvalidity=OLD_UIDVALIDITY)})
    item = weak.plan([command]).items[0]

    assert item.status == STATUS_PENDING_CAPABILITY
    assert item.detail["missing_capabilities"] == ["CONDSTORE"]
    assert all(not step.executable for step in item.checks)
    # Applying evidence must not silently move it along.
    weak.apply_evidence(weak.plan([command]), evidence())
    assert item.status == STATUS_PENDING_CAPABILITY


def test_sequence_store_uses_mapping_from_verification_fetch():
    command = InterruptedCommand(
        tag="A15", command="STORE", args=("3", "+FLAGS", "(\\Seen)"),
        mailbox=INBOX, uidvalidity=OLD_UIDVALIDITY)
    plan = planner().plan([command])
    pending = planner().apply_evidence(plan, evidence(
        sequence_to_uid={INBOX: {3: 77}},
        messages={(INBOX, 77): MessageEvidence(77, flags=())},
    ))
    step = pending.items[0].checks[0]
    assert step.reissue is True
    assert step.imap_command == "UID STORE 77 +FLAGS (\\Seen)"


# ---------------------------------------------------------------------
# COPY
# ---------------------------------------------------------------------


def _copy_command(with_token=True, uid_scope=True):
    responses = []
    if with_token:
        responses.append(untagged(
            "OK", response_code="COPYUID",
            response_code_args="7 100:102 500:502"))
    prefix = ("UID", "COPY") if uid_scope else ("COPY",)
    return InterruptedCommand(
        tag="A20", command=prefix[0],
        args=(prefix[1:] + ("100:102", ARCHIVE)) if uid_scope
        else ("100:102", ARCHIVE),
        mailbox=INBOX, uidvalidity=OLD_UIDVALIDITY,
        responses=responses)


def test_copy_without_correlation_token_is_manual_regardless_of_content():
    command = _copy_command(with_token=False)
    item = planner().plan([command]).items[0]

    assert item.status == STATUS_PENDING_MANUAL
    assert item.reason == "no-copyuid-correspondence"
    assert item.checks[0].executable is False
    # Even an identical-looking message elsewhere must not settle it.
    same_content = evidence({
        (ARCHIVE, 999): MessageEvidence(999, size=1234, message_id="<x>"),
    })
    planner().apply_evidence(planner().plan([command]), same_content)
    assert item.status == STATUS_PENDING_MANUAL


def test_copy_with_copyuid_is_verified_against_destination_uids():
    command = _copy_command()
    item = planner().plan([command]).items[0]

    assert item.status == STATUS_PENDING_VERIFICATION
    assert item.action == ACTION_VERIFY_COPY
    fetch = next(step for step in item.checks
                 if step.action == ACTION_VERIFY_COPY)
    assert fetch.imap_command == "UID FETCH 500:502 (UID FLAGS)"
    assert item.detail["destination"] == ARCHIVE


def test_copy_confirmed_present_is_resolved():
    command = _copy_command()
    plan = planner().plan([command])
    result = planner().apply_evidence(plan, evidence({
        (ARCHIVE, 500): MessageEvidence(500),
        (ARCHIVE, 501): MessageEvidence(501),
        (ARCHIVE, 502): MessageEvidence(502),
    }))
    assert result.items[0].status == STATUS_RESOLVED
    assert result.items[0].checks == []


def test_copy_confirmed_absent_reissues_only_with_uid_copy():
    command = _copy_command()
    plan = planner().plan([command])
    result = planner().apply_evidence(plan, evidence(
        absent={ARCHIVE: frozenset((500, 501, 502))}))
    item = result.items[0]
    assert item.status == STATUS_PENDING_VERIFICATION
    step = item.checks[0]
    assert step.reissue is True
    assert step.action == ACTION_REISSUE_COPY
    assert step.imap_command == "UID COPY 100:102 Archive"


def test_copy_partially_applied_reissues_only_missing_sources():
    command = _copy_command()
    plan = planner().plan([command])
    result = planner().apply_evidence(plan, evidence(
        messages={(ARCHIVE, 500): MessageEvidence(500)},
        absent={ARCHIVE: frozenset((501, 502))}))
    step = result.items[0].checks[0]
    assert step.reissue is True
    assert step.imap_command == "UID COPY 101:102 Archive"


def test_copy_destination_uidvalidity_changed_is_manual():
    command = _copy_command()
    plan = planner().plan([command])
    result = planner().apply_evidence(plan, ServerEvidence(
        uidvalidity={INBOX: OLD_UIDVALIDITY, ARCHIVE: 8}))
    assert result.items[0].status == STATUS_PENDING_MANUAL
    assert result.items[0].reason == "destination-uidvalidity-changed"


def test_copy_waits_when_evidence_incomplete():
    command = _copy_command()
    plan = planner().plan([command])
    result = planner().apply_evidence(plan, evidence(
        messages={(ARCHIVE, 500): MessageEvidence(500)}))
    item = result.items[0]
    # 501/502 are neither present nor explicitly absent: no decision yet.
    assert item.status == STATUS_PENDING_VERIFICATION
    assert all(not step.reissue for step in item.checks)


# ---------------------------------------------------------------------
# MOVE
# ---------------------------------------------------------------------


def _move_command(with_token=True):
    responses = [untagged(
        "OK", response_code="COPYUID",
        response_code_args="7 100:101 500:501")] if with_token else []
    return InterruptedCommand(
        tag="A30", command="UID", args=("MOVE", "100:101", ARCHIVE),
        mailbox=INBOX, uidvalidity=OLD_UIDVALIDITY,
        responses=responses)


def test_move_without_capability_stays_pending_without_fallback():
    command = _move_command()
    weak = RecoveryPlanner(
        capabilities=("IMAP4REV1", "UIDPLUS"),
        domains={INBOX: MailboxDomain(INBOX, uidvalidity=OLD_UIDVALIDITY),
                 ARCHIVE: MailboxDomain(ARCHIVE, uidvalidity=7)})
    item = weak.plan([command]).items[0]

    assert item.status == STATUS_PENDING_CAPABILITY
    assert "MOVE" in item.detail["missing_capabilities"]
    all_text = " ".join(step.purpose for step in item.checks).upper()
    all_commands = " ".join(str(step.imap_command) for step in item.checks)
    assert "COPY" not in all_commands
    assert "DELETED" not in all_text
    assert item.detail["no_fallback"]


def test_move_without_token_is_manual():
    item = planner().plan([_move_command(with_token=False)]).items[0]
    assert item.status == STATUS_PENDING_MANUAL
    assert item.reason == "no-copyuid-correspondence"


def test_move_confirmed_destination_present_and_source_gone_is_resolved():
    command = _move_command()
    plan = planner().plan([command])
    result = planner().apply_evidence(plan, evidence(
        messages={
            (ARCHIVE, 500): MessageEvidence(500),
            (ARCHIVE, 501): MessageEvidence(501),
        },
        absent={INBOX: frozenset((100, 101))}))
    assert result.items[0].status == STATUS_RESOLVED
    assert result.items[0].checks == []


def test_move_not_applied_reissues_move_not_copy_delete():
    command = _move_command()
    plan = planner().plan([command])
    result = planner().apply_evidence(plan, evidence(
        messages={
            (INBOX, 100): MessageEvidence(100),
            (INBOX, 101): MessageEvidence(101),
        },
        absent={ARCHIVE: frozenset((500, 501))}))
    step = result.items[0].checks[0]
    assert step.reissue is True
    assert step.action == ACTION_REISSUE_MOVE
    assert step.imap_command == "UID MOVE 100:101 Archive"
    assert "COPY" not in step.imap_command


def test_move_partial_state_is_manual():
    command = _move_command()
    plan = planner().plan([command])
    result = planner().apply_evidence(plan, evidence(
        messages={
            (ARCHIVE, 500): MessageEvidence(500),
            (ARCHIVE, 501): MessageEvidence(501),
            (INBOX, 100): MessageEvidence(100),
            (INBOX, 101): MessageEvidence(101),
        }))
    item = result.items[0]
    assert item.status == STATUS_PENDING_MANUAL
    assert item.reason == "move-partially-applied"
    assert all(not step.reissue for step in item.checks)


def test_move_checks_both_mailboxes_before_deciding():
    item = planner().plan([_move_command()]).items[0]
    purposes = [step.purpose for step in item.checks]
    assert any("destination UIDs" in p for p in purposes)
    assert any("source UIDs were removed" in p for p in purposes)
    assert ACTION_VERIFY_MOVE in [step.action for step in item.checks]


# ---------------------------------------------------------------------
# APPEND
# ---------------------------------------------------------------------


def test_append_without_appenduid_is_manual_to_avoid_duplication():
    command = InterruptedCommand(
        tag="A40", command="APPEND", args=(ARCHIVE,),
        mailbox=ARCHIVE)
    item = planner().plan([command]).items[0]
    assert item.status == STATUS_PENDING_MANUAL
    assert item.reason == "no-appenduid-correspondence"


def test_append_confirmed_via_appenduid_resolves_or_reissues():
    command = InterruptedCommand(
        tag="A41", command="APPEND", args=(ARCHIVE,),
        mailbox=ARCHIVE,
        responses=[untagged("OK", response_code="APPENDUID",
                             response_code_args="7 600")])
    plan = planner().plan([command])

    resolved = planner().apply_evidence(
        planner().plan([command]),
        evidence({(ARCHIVE, 600): MessageEvidence(600)}))
    assert resolved.items[0].status == STATUS_RESOLVED

    reissued = planner().apply_evidence(
        planner().plan([command]),
        evidence(absent={ARCHIVE: frozenset((600,))}))
    step = reissued.items[0].checks[0]
    assert step.reissue is True
    assert "appenduid" in step.expects


# ---------------------------------------------------------------------
# Other commands and plan structure
# ---------------------------------------------------------------------


def test_unknown_state_other_writes_go_manual():
    for verb in ("CREATE", "DELETE", "RENAME", "EXPUNGE", "SETACL"):
        command = InterruptedCommand(tag="A50", command=verb,
                                     args=("Junk",), mailbox=INBOX)
        item = planner().plan([command]).items[0]
        assert item.status == STATUS_PENDING_MANUAL, verb
        assert item.checks[0].executable is False


def test_plan_aggregates_pending_blocked_and_manual():
    commands = [
        InterruptedCommand("A60", "FETCH", ("1", "(FLAGS)"),
                           mailbox=INBOX, uidvalidity=OLD_UIDVALIDITY,
                           literal_expected=10, literal_received=4),
        InterruptedCommand("A61", "UID", ("MOVE", "9", ARCHIVE),
                           mailbox=INBOX, uidvalidity=OLD_UIDVALIDITY),
    ]
    weak = RecoveryPlanner(
        capabilities=("IMAP4REV1",),
        domains={INBOX: MailboxDomain(INBOX, uidvalidity=OLD_UIDVALIDITY),
                 ARCHIVE: MailboxDomain(ARCHIVE, uidvalidity=7)})
    plan = weak.plan(commands)

    assert len(plan.pending) == 2
    assert [item.tag for item in plan.blocked_by_capability] == ["A61"]
    assert [step.item_tag for step in plan.steps] == ["A60", "A61"]
    # Re-issue steps are never present before evidence.
    assert all(not step.reissue for step in plan.steps)


def test_copyuid_record_parsing_and_ranges():
    record = CopyUidRecord.parse("COPYUID", "42 100:102,105 500:503")
    assert record.uidvalidity == 42
    assert record.source_uids == (100, 101, 102, 105)
    assert record.destination_uids == (500, 501, 502, 503)

    append = CopyUidRecord.parse("APPENDUID", "7 600")
    assert append.source_uids == ()
    assert append.destination_uids == (600,)


def test_full_session_scenario_and_archive_reconciliation():
    # 1. completed read, 2. truncated read, 3. unknown COPY,
    # 4. unknown MOVE, 5. unknown STORE.
    session = [
        InterruptedCommand(
            "B01", "FETCH", ("100", "(RFC822)"), mailbox=INBOX,
            uidvalidity=OLD_UIDVALIDITY,
            literal_expected=100, literal_received=100),
        InterruptedCommand(
            "B02", "FETCH", ("101", "(RFC822)"), mailbox=INBOX,
            uidvalidity=OLD_UIDVALIDITY,
            literal_expected=100, literal_received=40),
        InterruptedCommand(
            "B03", "UID", ("COPY", "100", ARCHIVE), mailbox=INBOX,
            uidvalidity=OLD_UIDVALIDITY,
            responses=[untagged("OK", response_code="COPYUID",
                                 response_code_args="7 100 700")]),
        InterruptedCommand(
            "B04", "UID", ("MOVE", "101", ARCHIVE), mailbox=INBOX,
            uidvalidity=OLD_UIDVALIDITY,
            responses=[untagged("OK", response_code="COPYUID",
                                 response_code_args="7 101 701")]),
        InterruptedCommand(
            "B05", "UID", ("STORE", "100", "+FLAGS", "(\\Seen)"),
            mailbox=INBOX, uidvalidity=OLD_UIDVALIDITY),
    ]
    plan = planner().plan(session)
    by_tag = {item.tag: item for item in plan.items}

    assert by_tag["B01"].status == STATUS_RESOLVED
    assert by_tag["B02"].action == ACTION_REFETCH

    # Check steps let the caller verify each original request against the
    # actual archive state before any write is re-sent.
    copy_check = next(step for step in by_tag["B03"].checks if step.reissue
                      is False and step.action == ACTION_VERIFY_COPY)
    assert copy_check.mailbox == ARCHIVE

    server_evidence = evidence(
        messages={
            (ARCHIVE, 700): MessageEvidence(700),      # COPY landed
            (ARCHIVE, 701): MessageEvidence(701),      # MOVE destination
            (INBOX, 100): MessageEvidence(100, flags=("\\Seen",)),
        },
        absent={
            INBOX: frozenset((101,)),                  # MOVE source gone
        })
    planner().apply_evidence(plan, server_evidence)

    assert by_tag["B03"].status == STATUS_RESOLVED
    assert by_tag["B04"].status == STATUS_RESOLVED
    assert by_tag["B05"].status == STATUS_RESOLVED
    # Only the truncated read still needs a network action.
    assert [item.tag for item in plan.pending] == ["B02"]


def test_reissue_commands_preserve_original_request_parameters():
    command = InterruptedCommand(
        tag="B10", command="UID",
        args=("STORE", "100:102", "+FLAGS", "(\\Flagged \\Seen)"),
        mailbox=INBOX, uidvalidity=OLD_UIDVALIDITY)
    plan = planner().plan([command])
    result = planner().apply_evidence(plan, evidence({
        (INBOX, 100): MessageEvidence(100, flags=()),
        (INBOX, 101): MessageEvidence(101, flags=()),
        (INBOX, 102): MessageEvidence(102, flags=()),
    }))
    step = result.items[0].checks[0]
    assert step.imap_command == \
        "UID STORE 100:102 +FLAGS (\\Flagged \\Seen)"


def test_evidence_application_is_idempotent():
    command = InterruptedCommand(
        tag="B11", command="UID",
        args=("STORE", "100", "+FLAGS", "(\\Seen)"),
        mailbox=INBOX, uidvalidity=OLD_UIDVALIDITY)
    plan = planner().plan([command])
    payload = evidence({(INBOX, 100): MessageEvidence(100, flags=("\\Seen",))})
    planner().apply_evidence(plan, payload)
    planner().apply_evidence(plan, payload)
    assert plan.items[0].status == STATUS_RESOLVED
    assert plan.items[0].checks == []


def test_missing_capability_item_survives_partial_evidence():
    command = _move_command()
    weak = RecoveryPlanner(
        capabilities=("IMAP4REV1",),
        domains={INBOX: MailboxDomain(INBOX, uidvalidity=OLD_UIDVALIDITY),
                 ARCHIVE: MailboxDomain(ARCHIVE, uidvalidity=7)})
    plan = weak.plan([command])
    weak.apply_evidence(plan, evidence(
        messages={(INBOX, 100): MessageEvidence(100)},
        absent={ARCHIVE: frozenset((500, 501))}))
    assert plan.items[0].status == STATUS_PENDING_CAPABILITY
    assert plan.items[0].checks[0].executable is False


def test_unrecognised_command_does_not_assume_anything():
    command = InterruptedCommand(tag="B12", command="PROXYAUTH",
                                 args=("user",), mailbox=INBOX)
    item = planner().plan([command]).items[0]
    assert item.status == STATUS_PENDING_MANUAL


def test_uid_store_flags_removal_semantics():
    command = InterruptedCommand(
        tag="B13", command="UID",
        args=("STORE", "100", "-FLAGS", "(\\Seen)"),
        mailbox=INBOX, uidvalidity=OLD_UIDVALIDITY)
    plan = planner().plan([command])
    # Flag still present => change did not take effect.
    pending = planner().apply_evidence(
        planner().plan([command]),
        evidence({(INBOX, 100): MessageEvidence(100, flags=("\\Seen",))}))
    assert pending.items[0].checks[0].reissue is True

    # Flag gone => satisfied, no re-send.
    done = planner().apply_evidence(
        planner().plan([command]),
        evidence({(INBOX, 100): MessageEvidence(100, flags=())}))
    assert done.items[0].status == STATUS_RESOLVED


def test_mailbox_quoting_for_reissue_target():
    command = InterruptedCommand(
        tag="B14", command="UID", args=("COPY", "100", "Old Mail"),
        mailbox=INBOX, uidvalidity=OLD_UIDVALIDITY,
        responses=[untagged("OK", response_code="COPYUID",
                             response_code_args="7 100 9")])
    plan = planner(capabilities=("IMAP4REV1", "MOVE", "UIDPLUS", "CONDSTORE"),
                   domains={INBOX: MailboxDomain(INBOX, uidvalidity=OLD_UIDVALIDITY),
                            "Old Mail": MailboxDomain("Old Mail", uidvalidity=7)}) \
        .plan([command])
    result = planner(capabilities=("IMAP4REV1", "MOVE", "UIDPLUS", "CONDSTORE"),
                     domains={INBOX: MailboxDomain(INBOX, uidvalidity=OLD_UIDVALIDITY),
                              "Old Mail": MailboxDomain("Old Mail", uidvalidity=7)}) \
        .apply_evidence(plan, ServerEvidence(
            absent_uids={"Old Mail": frozenset((9,))},
            uidvalidity={INBOX: OLD_UIDVALIDITY, "Old Mail": 7}))
    assert result.items[0].checks[0].imap_command == \
        'UID COPY 100 "Old Mail"'
