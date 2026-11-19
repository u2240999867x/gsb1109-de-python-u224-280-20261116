"""Recovery planning for IMAP sessions interrupted by a disconnect.

An IMAP connection can fail after the server has acted on a command but
before the tagged completion response reached the client.  Given a
transcript of the interrupted session (the commands that were sent and
the responses that were actually received), the capabilities advertised
by the *new* connection, the mailbox domain that can be inspected on it,
and the server evidence gathered there, this module produces an ordered
recovery plan.

The planner never opens a socket and never sends a command.  It only
decides what is already settled, what must be re-checked, and what may
eventually be re-issued.  The caller runs the check steps on the new
connection and feeds the resulting evidence back through
:meth:`RecoveryPlanner.apply_evidence`.

Rules enforced here, based on RFC 3501, RFC 9051 and RFC 6851:

* A read re-issued on the new connection is harmless.  A write whose
  terminal state is unknown is never blindly re-sent: it is verified
  first and only re-issued when the evidence proves it did not take
  effect.
* A COPY or MOVE is only considered completed when the destination
  holds a message that is reliably linked to the source (a recorded
  ``COPYUID``/``APPENDUID`` correspondence, or a UID match while the
  destination UIDVALIDITY is unchanged).  Equal message content alone is
  not proof of completion and never settles such an item.
* If the new connection lacks a capability the original command relied
  on (for example ``MOVE`` or ``CONDSTORE``), the item stays pending
  instead of being silently replaced by a weaker fallback such as
  ``COPY`` plus ``\\Deleted``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple


__all__ = (
    "InterruptedCommand",
    "RecordedResponse",
    "MailboxDomain",
    "MessageEvidence",
    "ServerEvidence",
    "CopyUidRecord",
    "RecoveryItem",
    "CheckStep",
    "RecoveryPlan",
    "RecoveryPlanner",
    "STATUS_RESOLVED",
    "STATUS_FAILED",
    "STATUS_PENDING_VERIFICATION",
    "STATUS_PENDING_CAPABILITY",
    "STATUS_PENDING_MANUAL",
    "ACTION_REFETCH",
    "ACTION_RERUN_READ",
    "ACTION_VERIFY_FLAGS",
    "ACTION_VERIFY_COPY",
    "ACTION_VERIFY_MOVE",
    "ACTION_VERIFY_APPEND",
    "ACTION_REISSUE_STORE",
    "ACTION_REISSUE_COPY",
    "ACTION_REISSUE_MOVE",
    "ACTION_REISSUE_APPEND",
)


# Item statuses

STATUS_RESOLVED = "resolved"
"""The outcome is known without contacting the server again, or it has
since been confirmed against server evidence."""

STATUS_FAILED = "failed"
"""A tagged ``NO``/``BAD`` was received: the command demonstrably did not
take effect."""

STATUS_PENDING_VERIFICATION = "pending-verification"
"""The outcome is unknown and one or more read-only check steps must be
run on the new connection before anything may be re-sent."""

STATUS_PENDING_CAPABILITY = "pending-capability"
"""Verification or recovery requires a capability the new connection does
not advertise.  The item must wait; it is never downgraded."""

STATUS_PENDING_MANUAL = "pending-manual"
"""No reliable server-side correspondence exists, so the item requires a
human decision rather than an automatic re-send."""


# Recovery actions

ACTION_REFETCH = "refetch"
ACTION_RERUN_READ = "rerun-read"
ACTION_VERIFY_FLAGS = "verify-flags"
ACTION_VERIFY_COPY = "verify-copy"
ACTION_VERIFY_MOVE = "verify-move"
ACTION_VERIFY_APPEND = "verify-append"
ACTION_REISSUE_STORE = "reissue-store"
ACTION_REISSUE_COPY = "reissue-copy"
ACTION_REISSUE_MOVE = "reissue-move"
ACTION_REISSUE_APPEND = "reissue-append"


_READ_COMMANDS = frozenset((
    "CAPABILITY", "CHECK", "FETCH", "GETACL", "GETANNOTATION",
    "GETQUOTA", "GETQUOTAROOT", "ID", "LIST", "LSUB", "MYRIGHTS",
    "NAMESPACE", "NOOP", "PARTIAL", "SEARCH", "SORT", "STATUS",
    "THREAD",
))

_WRITE_COMMANDS = frozenset((
    "APPEND", "COPY", "CREATE", "DELETE", "EXPUNGE", "MOVE", "RENAME",
    "SETACL", "SETANNOTATION", "SETQUOTA", "STORE", "SUBSCRIBE",
    "UNSUBSCRIBE",
))

# Writes that can never be made idempotent without a server-issued
# correlation token (APPENDUID/COPYUID) and therefore default to manual
# review when their terminal state is unknown.
_MANUAL_WRITES = frozenset((
    "CREATE", "DELETE", "RENAME", "SETACL", "SETANNOTATION", "SETQUOTA",
    "SUBSCRIBE", "UNSUBSCRIBE", "EXPUNGE",
))


@dataclass(frozen=True)
class RecordedResponse:
    """A response recorded on the old connection before it dropped.

    :param kind: ``"tagged"`` for a command completion, ``"untagged"``
        for an ``*`` response, ``"continuation"`` for a ``+`` response.
    :param tag: Tag of the command a tagged response belongs to.
    :param response_type: ``OK``/``NO``/``BAD`` for tagged responses, or
        the untagged response kind (``FETCH``, ``EXPUNGE`` ...).
    :param text: Human readable response text, if any.
    :param response_code: Name of the bracketed response code, e.g.
        ``COPYUID`` or ``APPENDUID``.
    :param response_code_args: Raw arguments of the response code, e.g.
        ``"3 100:102 500:502"``.
    :param data: Parsed payload, for example the UID and FLAGS seen in an
        untagged ``FETCH`` response.
    """

    kind: str
    tag: Optional[str] = None
    response_type: Optional[str] = None
    text: Optional[bytes] = None
    response_code: Optional[str] = None
    response_code_args: Optional[str] = None
    data: Dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class InterruptedCommand:
    """One command that was in flight when the connection dropped.

    :param tag: The IMAP tag used on the old connection.
    :param command: The command verb, ``UID`` for UID-prefixed commands.
    :param args: The already IMAP-encoded arguments.
    :param mailbox: Mailbox that was selected when the command was sent.
    :param responses: Responses received before the disconnect.
    :param raw: The command line without tag or CRLF, when available.
    :param literal_expected: Declared size of a literal being read, for a
        FETCH that was interrupted mid-literal.
    :param literal_received: Number of literal bytes actually received.
    :param uidvalidity: UIDVALIDITY of the source mailbox as known on the
        old connection, when relevant.
    :param required_capabilities: Extra capabilities the caller knows the
        command depends on, beyond those inferred automatically.
    """

    tag: str
    command: str
    args: Sequence[str] = field(default_factory=list)
    mailbox: Optional[str] = None
    responses: Sequence[RecordedResponse] = field(default_factory=list)
    raw: Optional[str] = None
    literal_expected: Optional[int] = None
    literal_received: Optional[int] = None
    uidvalidity: Optional[int] = None
    required_capabilities: Sequence[str] = field(default_factory=tuple)

    @property
    def is_uid(self) -> bool:
        return self.command.upper() == "UID"

    @property
    def base_command(self) -> str:
        if self.is_uid:
            return self.args[0].upper()
        return self.command.upper()

    @property
    def call_args(self) -> List[str]:
        if self.is_uid:
            return list(self.args[1:])
        return list(self.args)

    @property
    def tagged_completion(self) -> Optional[RecordedResponse]:
        for response in reversed(self.responses):
            if response.kind == "tagged" and response.tag == self.tag:
                return response
        return None

    def response_code(self, name: str) -> Optional[RecordedResponse]:
        wanted = name.upper()
        for response in self.responses:
            if response.response_code and response.response_code.upper() == wanted:
                return response
        return None

    def wire_command(self) -> str:
        if self.raw is not None:
            return self.raw
        return " ".join([self.command.upper(), *self.args])


@dataclass(frozen=True)
class MailboxDomain:
    """What is known about a mailbox on the new connection."""

    name: str
    exists: bool = True
    writable: bool = True
    uidvalidity: Optional[int] = None
    highestmodseq: Optional[int] = None


@dataclass(frozen=True)
class CopyUidRecord:
    """A parsed ``COPYUID``/``APPENDUID`` correspondence.

    ``source_uids`` is empty for an ``APPENDUID`` record, which only
    carries the destination UID.
    """

    uidvalidity: int
    source_uids: Tuple[int, ...]
    destination_uids: Tuple[int, ...]

    @classmethod
    def parse(cls, code: str, raw_args: str) -> "CopyUidRecord":
        parts = raw_args.split()
        if code.upper() == "APPENDUID":
            uidvalidity = int(parts[0])
            destination = tuple(_parse_uid_set(parts[1]))
            return cls(uidvalidity, (), destination)
        uidvalidity = int(parts[0])
        sources = tuple(_parse_uid_set(parts[1]))
        destinations = tuple(_parse_uid_set(parts[2]))
        return cls(uidvalidity, sources, destinations)


@dataclass(frozen=True)
class MessageEvidence:
    """Server evidence for one message addressed by (mailbox, UID)."""

    uid: int
    flags: Tuple[str, ...] = ()
    modseq: Optional[int] = None
    size: Optional[int] = None
    message_id: Optional[str] = None


@dataclass(frozen=True)
class ServerEvidence:
    """Evidence collected by running the plan's check steps.

    :param messages: Message state keyed by ``(mailbox, uid)``.
    :param absent_uids: UIDs confirmed absent from a mailbox.  A UID that
        simply does not appear in ``messages`` is not treated as absent
        unless it is listed here.
    :param uidvalidity: UIDVALIDITY observed per mailbox.
    :param highestmodseq: HIGHESTMODSEQ observed per mailbox.
    :param sequence_to_uid: Mapping from old-connection sequence numbers
        to UIDs, established by the FETCH verification step, per mailbox.
    """

    messages: Dict[Tuple[str, int], MessageEvidence] = field(default_factory=dict)
    absent_uids: Dict[str, FrozenSet[int]] = field(default_factory=dict)
    uidvalidity: Dict[str, int] = field(default_factory=dict)
    highestmodseq: Dict[str, int] = field(default_factory=dict)
    sequence_to_uid: Dict[str, Dict[int, int]] = field(default_factory=dict)

    def message(self, mailbox: str, uid: int) -> Optional[MessageEvidence]:
        return self.messages.get((mailbox, uid))

    def is_absent(self, mailbox: str, uid: int) -> bool:
        return uid in self.absent_uids.get(mailbox, frozenset())


@dataclass
class CheckStep:
    """A read-only verification step, or a gated re-issue.

    Re-issue steps only appear after evidence has shown that the original
    write did not take effect.  They are never produced up-front.
    """

    item_tag: str
    action: str
    purpose: str
    mailbox: Optional[str] = None
    imap_command: Optional[str] = None
    expects: Tuple[str, ...] = ()
    executable: bool = True
    """False for manual review entries that have no safe IMAP command."""

    reissue: bool = False
    """True for the gated write steps produced after verification."""


@dataclass
class RecoveryItem:
    """The recovery state of one interrupted command."""

    tag: str
    command: InterruptedCommand
    status: str
    reason: str
    action: Optional[str] = None
    checks: List[CheckStep] = field(default_factory=list)
    detail: Dict[str, object] = field(default_factory=dict)

    @property
    def is_write(self) -> bool:
        return self.command.base_command in _WRITE_COMMANDS

    @property
    def is_read(self) -> bool:
        return self.command.base_command in _READ_COMMANDS


@dataclass
class RecoveryPlan:
    """The ordered plan for one interrupted session."""

    items: List[RecoveryItem] = field(default_factory=list)

    @property
    def steps(self) -> List[CheckStep]:
        """Executable steps in transcript order.

        Read re-fetches and read-only verifications appear immediately.
        Re-issue steps are only present once evidence has authorised them.
        """

        return [step for item in self.items for step in item.checks]

    @property
    def pending(self) -> List[RecoveryItem]:
        return [item for item in self.items if item.status != STATUS_RESOLVED
                and item.status != STATUS_FAILED]

    @property
    def blocked_by_capability(self) -> List[RecoveryItem]:
        return [item for item in self.items
                if item.status == STATUS_PENDING_CAPABILITY]

    @property
    def manual(self) -> List[RecoveryItem]:
        return [item for item in self.items
                if item.status == STATUS_PENDING_MANUAL]


class RecoveryPlanner:
    """Builds and advances recovery plans.

    :param capabilities: Capabilities advertised by the new connection,
        in upper case.
    :param domains: Mailboxes reachable on the new connection, keyed by
        name.
    """

    def __init__(
        self,
        capabilities: Sequence[str] = (),
        domains: Optional[Dict[str, MailboxDomain]] = None,
    ) -> None:
        self.capabilities = frozenset(cap.upper() for cap in capabilities)
        self.domains = dict(domains or {})

    # ------------------------------------------------------------------
    # Plan construction
    # ------------------------------------------------------------------

    def plan(self, commands: Sequence[InterruptedCommand]) -> RecoveryPlan:
        plan = RecoveryPlan()
        for command in commands:
            plan.items.append(self._classify(command))
        return plan

    def _classify(self, command: InterruptedCommand) -> RecoveryItem:
        completion = command.tagged_completion
        if completion is not None and completion.response_type in ("NO", "BAD"):
            return RecoveryItem(
                tag=command.tag,
                command=command,
                status=STATUS_FAILED,
                reason="tagged-%s" % completion.response_type.lower(),
                detail={"response": completion},
            )

        base = command.base_command
        if base == "FETCH":
            return self._classify_fetch(command, completion)
        if base == "STORE":
            return self._classify_store(command, completion)
        if base == "COPY":
            return self._classify_copy(command, completion)
        if base == "MOVE":
            return self._classify_move(command, completion)
        if base == "APPEND":
            return self._classify_append(command, completion)
        if base in _READ_COMMANDS:
            return self._classify_other_read(command, completion)
        if base in _MANUAL_WRITES or base not in _WRITE_COMMANDS:
            if completion is not None and completion.response_type == "OK":
                return self._resolved(command, "tagged-ok", completion)
            return RecoveryItem(
                tag=command.tag,
                command=command,
                status=STATUS_PENDING_MANUAL,
                reason="no-idempotent-recovery-for-%s" % base.lower(),
                checks=[CheckStep(
                    item_tag=command.tag,
                    action="manual-review",
                    purpose="Review %s outcome manually; automatic re-send "
                            "could duplicate or destroy data" % base,
                    mailbox=command.mailbox,
                    executable=False,
                )],
            )
        return RecoveryItem(
            tag=command.tag,
            command=command,
            status=STATUS_PENDING_MANUAL,
            reason="unrecognised-command",
        )

    def _classify_fetch(
        self,
        command: InterruptedCommand,
        completion: Optional[RecordedResponse],
    ) -> RecoveryItem:
        if completion is not None and completion.response_type == "OK":
            if (command.literal_expected is not None
                    and command.literal_received is not None
                    and command.literal_received < command.literal_expected):
                return self._refetch_item(
                    command, "tagged-ok-but-literal-truncated",
                    command.literal_received,
                )
            return self._resolved(command, "tagged-ok", completion)

        if (command.literal_expected is not None
                and command.literal_received is not None):
            if command.literal_received >= command.literal_expected:
                item = self._resolved(command, "literal-complete", None)
                item.detail["note"] = (
                    "Literal fully received; tagged OK was lost in the "
                    "disconnect. Message content is self-delimiting.")
                return item
            return self._refetch_item(
                command, "literal-truncated", command.literal_received)

        return self._refetch_item(command, "no-completion-received", None)

    def _refetch_item(
        self,
        command: InterruptedCommand,
        reason: str,
        offset: Optional[int],
    ) -> RecoveryItem:
        wire = command.wire_command()
        expects = ("flags", "uid")
        if offset:
            expects = ("literal-from-offset-%s" % offset,) + expects
            detail = {"offset": offset, "literal_expected": command.literal_expected}
        else:
            detail = {}
        return RecoveryItem(
            tag=command.tag,
            command=command,
            status=STATUS_PENDING_VERIFICATION,
            reason=reason,
            action=ACTION_REFETCH,
            checks=[CheckStep(
                item_tag=command.tag,
                action=ACTION_REFETCH,
                purpose="Re-issue the read; FETCH is safe to repeat",
                mailbox=command.mailbox,
                imap_command=wire,
                expects=expects,
            )],
            detail=detail,
        )

    def _classify_other_read(
        self,
        command: InterruptedCommand,
        completion: Optional[RecordedResponse],
    ) -> RecoveryItem:
        if completion is not None and completion.response_type == "OK":
            return self._resolved(command, "tagged-ok", completion)
        return RecoveryItem(
            tag=command.tag,
            command=command,
            status=STATUS_PENDING_VERIFICATION,
            reason="no-completion-received",
            action=ACTION_RERUN_READ,
            checks=[CheckStep(
                item_tag=command.tag,
                action=ACTION_RERUN_READ,
                purpose="Re-issue the read-only %s command"
                        % command.base_command,
                mailbox=command.mailbox,
                imap_command=command.wire_command(),
            )],
        )

    def _classify_store(
        self,
        command: InterruptedCommand,
        completion: Optional[RecordedResponse],
    ) -> RecoveryItem:
        if completion is not None and completion.response_type == "OK":
            return self._resolved(command, "tagged-ok", completion)

        missing = self._missing_capabilities(command)
        if missing:
            return self._capability_blocked(command, missing)

        validity_step = self._uidvalidity_check(command, command.mailbox)
        if validity_step is None:
            return self._manual(
                command,
                "uidvalidity-changed-or-unknown",
                "Sequence/UID mapping cannot be trusted after reconnect",
            )

        message_set, flag_op, flags = self._store_parts(command)
        if not command.is_uid and "*" in message_set:
            return self._manual(
                command,
                "wildcard-sequence-set",
                "Sequence number '*' cannot be resolved after a disconnect",
            )

        verb = "UID FETCH" if command.is_uid else "FETCH"
        fetch = CheckStep(
            item_tag=command.tag,
            action=ACTION_VERIFY_FLAGS,
            purpose="Read current UID and FLAGS to determine whether the "
                    "flag change took effect",
            mailbox=command.mailbox,
            imap_command="%s %s (UID FLAGS)" % (verb, message_set),
            expects=("uid", "flags"),
        )
        return RecoveryItem(
            tag=command.tag,
            command=command,
            status=STATUS_PENDING_VERIFICATION,
            reason="store-terminal-state-unknown",
            action=ACTION_VERIFY_FLAGS,
            checks=[validity_step, fetch],
            detail={"flag_operation": flag_op, "flags": flags,
                    "message_set": message_set, "uid_scope": command.is_uid},
        )

    def _classify_copy(
        self,
        command: InterruptedCommand,
        completion: Optional[RecordedResponse],
    ) -> RecoveryItem:
        if completion is not None and completion.response_type == "OK":
            return self._resolved(command, "tagged-ok", completion)

        destination = command.call_args[-1]
        code_response = command.response_code("COPYUID")
        if code_response is None or not code_response.response_code_args:
            return self._manual(
                command,
                "no-copyuid-correspondence",
                "No COPYUID correlation token was recorded; equal message "
                "content in '%s' cannot prove the COPY completed"
                % destination,
            )

        record = CopyUidRecord.parse("COPYUID", code_response.response_code_args)
        return self._correlated_copy_like(
            command, destination, record, move=False,
            reason="copy-terminal-state-unknown",
        )

    def _classify_move(
        self,
        command: InterruptedCommand,
        completion: Optional[RecordedResponse],
    ) -> RecoveryItem:
        if completion is not None and completion.response_type == "OK":
            return self._resolved(command, "tagged-ok", completion)

        missing = self._missing_capabilities(command)
        if missing:
            item = self._capability_blocked(command, missing)
            item.detail["no_fallback"] = (
                "MOVE must not be replaced by COPY plus STORE \\Deleted "
                "when MOVE is unavailable")
            return item

        destination = command.call_args[-1]
        code_response = command.response_code("COPYUID")
        if code_response is None or not code_response.response_code_args:
            return self._manual(
                command,
                "no-copyuid-correspondence",
                "No COPYUID correlation token was recorded; equal message "
                "content in '%s' cannot prove the MOVE completed"
                % destination,
            )

        record = CopyUidRecord.parse("COPYUID", code_response.response_code_args)
        return self._correlated_copy_like(
            command, destination, record, move=True,
            reason="move-terminal-state-unknown",
        )

    def _classify_append(
        self,
        command: InterruptedCommand,
        completion: Optional[RecordedResponse],
    ) -> RecoveryItem:
        if completion is not None and completion.response_type == "OK":
            return self._resolved(command, "tagged-ok", completion)

        destination = command.mailbox or (
            command.call_args[0] if command.call_args else None)
        code_response = command.response_code("APPENDUID")
        if code_response is None or not code_response.response_code_args:
            return self._manual(
                command,
                "no-appenduid-correspondence",
                "No APPENDUID correlation token was recorded; re-sending "
                "APPEND would duplicate the message",
            )

        record = CopyUidRecord.parse("APPENDUID", code_response.response_code_args)
        missing = self._missing_capabilities(command)
        if missing:
            return self._capability_blocked(command, missing)

        validity_step = self._destination_uidvalidity_check(command, destination,
                                                            record.uidvalidity)
        if validity_step is None:
            return self._manual(command, "destination-uidvalidity-changed",
                                "APPENDUID UID can no longer be interpreted")
        checks = [validity_step, CheckStep(
            item_tag=command.tag,
            action=ACTION_VERIFY_APPEND,
            purpose="Check whether the APPENDUID destination UID exists",
            mailbox=destination,
            imap_command="UID FETCH %s (UID FLAGS)"
                         % _join_uid_set(record.destination_uids),
            expects=("uid", "flags"),
        )]
        return RecoveryItem(
            tag=command.tag,
            command=command,
            status=STATUS_PENDING_VERIFICATION,
            reason="append-terminal-state-unknown",
            action=ACTION_VERIFY_APPEND,
            checks=checks,
            detail={"destination": destination, "record": record},
        )

    def _correlated_copy_like(
        self,
        command: InterruptedCommand,
        destination: str,
        record: CopyUidRecord,
        move: bool,
        reason: str,
    ) -> RecoveryItem:
        validity_step = self._destination_uidvalidity_check(
            command, destination, record.uidvalidity)
        if validity_step is None:
            return self._manual(
                command,
                "destination-uidvalidity-changed",
                "COPYUID destination UIDs can no longer be interpreted",
            )

        action = ACTION_VERIFY_MOVE if move else ACTION_VERIFY_COPY
        checks = [validity_step, CheckStep(
            item_tag=command.tag,
            action=action,
            purpose="Check the COPYUID destination UIDs in '%s'"
                    % destination,
            mailbox=destination,
            imap_command="UID FETCH %s (UID FLAGS)"
                         % _join_uid_set(record.destination_uids),
            expects=("uid", "flags"),
        )]
        if move:
            source_validity = self._uidvalidity_check(command, command.mailbox)
            if source_validity is None:
                return self._manual(
                    command,
                    "uidvalidity-changed-or-unknown",
                    "Source UIDs cannot be trusted to verify the MOVE",
                )
            checks.append(source_validity)
            checks.append(CheckStep(
                item_tag=command.tag,
                action=ACTION_VERIFY_MOVE,
                purpose="Check whether the source UIDs were removed",
                mailbox=command.mailbox,
                imap_command="UID FETCH %s (UID)"
                             % _join_uid_set(record.source_uids),
                expects=("uid",),
            ))
        return RecoveryItem(
            tag=command.tag,
            command=command,
            status=STATUS_PENDING_VERIFICATION,
            reason=reason,
            action=action,
            checks=checks,
            detail={"destination": destination, "record": record,
                    "move": move},
        )

    # ------------------------------------------------------------------
    # Evidence-driven advancement
    # ------------------------------------------------------------------

    def apply_evidence(
        self,
        plan: RecoveryPlan,
        evidence: ServerEvidence,
        completed_checks: Sequence[Tuple[str, str]] = (),
    ) -> RecoveryPlan:
        """Advance the plan using evidence gathered on the new connection.

        :param plan: The plan returned by :meth:`plan`.
        :param evidence: State observed while running the check steps.
        :param completed_checks: ``(tag, action)`` pairs identifying read
            steps that have been re-issued and whose result was received.
            A read item stays pending until its step is reported here,
            because re-fetching produces data rather than server state.
        """
        completed = set(completed_checks)
        for item in list(plan.items):
            if item.status != STATUS_PENDING_VERIFICATION:
                continue
            base = item.command.base_command
            if base == "FETCH" or base in _READ_COMMANDS:
                if item.action is not None \
                        and (item.tag, item.action) in completed:
                    item.status = STATUS_RESOLVED
                    item.reason = "read-re-issued-on-new-connection"
                    item.checks = []
                continue
            if base == "STORE":
                self._resolve_store(item, evidence)
            elif base == "COPY":
                self._resolve_copy(item, evidence)
            elif base == "MOVE":
                self._resolve_move(item, evidence)
            elif base == "APPEND":
                self._resolve_append(item, evidence)
        return plan

    def _resolve_store(self, item: RecoveryItem, evidence: ServerEvidence) -> None:
        command = item.command
        mailbox = command.mailbox
        observed_uidvalidity = evidence.uidvalidity.get(mailbox)
        if (command.uidvalidity is not None
                and observed_uidvalidity is not None
                and observed_uidvalidity != command.uidvalidity):
            self._downgrade_manual(item, "uidvalidity-changed-during-recovery")
            return

        message_set, flag_op, wanted_flags = self._store_parts(command)
        if command.is_uid:
            uids = list(_parse_uid_set(message_set))
        else:
            uids = self._sequence_uids_from_evidence(item, evidence, message_set)
            if uids is None:
                return

        observed = [evidence.message(mailbox, uid) for uid in uids]
        if any(message is None for message in observed):
            if all(evidence.is_absent(mailbox, uid) for uid in uids):
                self._downgrade_manual(
                    item,
                    "target-messages-absent",
                    "Cannot apply a flag change to messages that no longer exist",
                )
                return
            return  # Still waiting for evidence.

        if all(self._flags_satisfy(message.flags, flag_op, wanted_flags)
               for message in observed):
            item.status = STATUS_RESOLVED
            item.reason = "flags-confirmed-on-server"
            item.action = None
            item.checks = []
            return

        item.checks = [CheckStep(
            item_tag=item.tag,
            action=ACTION_REISSUE_STORE,
            purpose="Evidence shows the flag change did not take effect; "
                    "re-issue the original STORE now that targets are confirmed",
            mailbox=mailbox,
            imap_command=self._store_reissue_command(command, uids),
            expects=("tagged-ok",),
            reissue=True,
        )]
        item.reason = "store-confirmed-not-applied"

    def _resolve_copy(self, item: RecoveryItem, evidence: ServerEvidence) -> None:
        record: CopyUidRecord = item.detail["record"]
        destination: str = item.detail["destination"]
        if self._uidvalidity_mismatch(evidence, destination, record.uidvalidity):
            self._downgrade_manual(item, "destination-uidvalidity-changed")
            return

        unknown = [uid for uid in record.destination_uids
                   if evidence.message(destination, uid) is None
                   and not evidence.is_absent(destination, uid)]
        if unknown:
            return  # Wait for complete destination evidence.

        present, absent = self._partition_destination(evidence, destination,
                                                      record.destination_uids)
        if present and not absent:
            item.status = STATUS_RESOLVED
            item.reason = "copyuid-destination-messages-confirmed"
            item.action = None
            item.checks = []
            return
        if not present:
            item.checks = [self._copy_reissue_step(item, record, destination)]
            item.reason = "copy-confirmed-not-applied"
            return
        # Some, but not all, destination UIDs exist: re-issue only the
        # missing sources rather than guessing from content.
        missing_sources = [
            source for source, dest in zip(record.source_uids,
                                           record.destination_uids)
            if dest in absent
        ]
        item.checks = [self._copy_reissue_step(
            item,
            CopyUidRecord(record.uidvalidity, tuple(missing_sources), absent),
            destination,
        )]
        item.reason = "copy-partially-applied"

    def _resolve_move(self, item: RecoveryItem, evidence: ServerEvidence) -> None:
        record: CopyUidRecord = item.detail["record"]
        destination: str = item.detail["destination"]
        source_mailbox = item.command.mailbox
        if self._uidvalidity_mismatch(evidence, destination, record.uidvalidity):
            self._downgrade_manual(item, "destination-uidvalidity-changed")
            return
        if (item.command.uidvalidity is not None
                and evidence.uidvalidity.get(source_mailbox) is not None
                and evidence.uidvalidity[source_mailbox]
                != item.command.uidvalidity):
            self._downgrade_manual(item, "source-uidvalidity-changed")
            return

        destination_present = [
            uid for uid in record.destination_uids
            if evidence.message(destination, uid) is not None
        ]
        destination_unknown = [
            uid for uid in record.destination_uids
            if evidence.message(destination, uid) is None
            and not evidence.is_absent(destination, uid)
        ]
        if destination_unknown:
            return  # Wait for complete destination evidence.

        source_present = [
            uid for uid in record.source_uids
            if evidence.message(source_mailbox, uid) is not None
        ]
        source_unknown = [
            uid for uid in record.source_uids
            if evidence.message(source_mailbox, uid) is None
            and not evidence.is_absent(source_mailbox, uid)
        ]
        if source_unknown:
            return  # Wait for complete source evidence.

        destination_complete = len(destination_present) == len(record.destination_uids)
        source_empty = not source_present

        if destination_complete and source_empty:
            item.status = STATUS_RESOLVED
            item.reason = "move-confirmed-destination-present-source-absent"
            item.action = None
            item.checks = []
            return

        if destination_complete and source_present:
            self._downgrade_manual(
                item,
                "move-partially-applied",
                "Destination copies exist while source messages remain; "
                "deleting source messages requires a human decision",
            )
            return

        if not destination_present and source_present:
            item.checks = [CheckStep(
                item_tag=item.tag,
                action=ACTION_REISSUE_MOVE,
                purpose="Evidence shows the MOVE did not take effect; "
                        "re-issue the original MOVE without downgrading it",
                mailbox=source_mailbox,
                imap_command=self._move_reissue_command(item.command, record),
                expects=("tagged-ok", "copyuid"),
                reissue=True,
            )]
            item.reason = "move-confirmed-not-applied"
            return

        self._downgrade_manual(
            item,
            "move-state-inconsistent",
            "Neither a complete destination nor surviving sources were "
            "established; automatic re-send is unsafe",
        )

    def _resolve_append(self, item: RecoveryItem, evidence: ServerEvidence) -> None:
        record: CopyUidRecord = item.detail["record"]
        destination: str = item.detail["destination"]
        if self._uidvalidity_mismatch(evidence, destination, record.uidvalidity):
            self._downgrade_manual(item, "destination-uidvalidity-changed")
            return
        existing = [uid for uid in record.destination_uids
                    if evidence.message(destination, uid) is not None]
        unknown = [uid for uid in record.destination_uids
                   if evidence.message(destination, uid) is None
                   and not evidence.is_absent(destination, uid)]
        if unknown:
            return
        if existing:
            item.status = STATUS_RESOLVED
            item.reason = "appenduid-destination-message-confirmed"
            item.action = None
            item.checks = []
            return
        item.checks = [CheckStep(
            item_tag=item.tag,
            action=ACTION_REISSUE_APPEND,
            purpose="Evidence shows the APPEND did not take effect; "
                    "re-send the original message literal",
            mailbox=destination,
            imap_command=None,
            expects=("tagged-ok", "appenduid"),
            reissue=True,
        )]
        item.reason = "append-confirmed-not-applied"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _missing_capabilities(self, command: InterruptedCommand) -> List[str]:
        required = {cap.upper() for cap in command.required_capabilities}
        if command.base_command == "MOVE":
            required.add("MOVE")
        joined = " ".join([command.command, *command.args]).upper()
        if "UNCHANGEDSINCE" in joined or "CHANGEDSINCE" in joined:
            required.add("CONDSTORE")
        return sorted(required - self.capabilities)

    def _capability_blocked(
        self,
        command: InterruptedCommand,
        missing: Sequence[str],
    ) -> RecoveryItem:
        return RecoveryItem(
            tag=command.tag,
            command=command,
            status=STATUS_PENDING_CAPABILITY,
            reason="missing-capability",
            checks=[CheckStep(
                item_tag=command.tag,
                action="await-capability",
                purpose="Wait for a connection advertising %s; no weaker "
                        "fallback is substituted" % ", ".join(missing),
                mailbox=command.mailbox,
                executable=False,
            )],
            detail={"missing_capabilities": list(missing)},
        )

    def _uidvalidity_check(
        self,
        command: InterruptedCommand,
        mailbox: Optional[str],
    ) -> Optional[CheckStep]:
        if mailbox is None:
            return None
        domain = self.domains.get(mailbox)
        if command.uidvalidity is not None and domain is not None \
                and domain.uidvalidity is not None \
                and domain.uidvalidity != command.uidvalidity:
            return None
        return CheckStep(
            item_tag=command.tag,
            action="verify-uidvalidity",
            purpose="Confirm UIDVALIDITY of '%s' still matches the old "
                    "connection before trusting UIDs or sequence numbers"
                    % mailbox,
            mailbox=mailbox,
            imap_command="STATUS %s (UIDVALIDITY)" % _mailbox_token(mailbox),
            expects=("uidvalidity",),
        )

    def _destination_uidvalidity_check(
        self,
        command: InterruptedCommand,
        mailbox: Optional[str],
        expected: int,
    ) -> Optional[CheckStep]:
        if mailbox is None:
            return None
        domain = self.domains.get(mailbox)
        if domain is not None and domain.uidvalidity is not None \
                and domain.uidvalidity != expected:
            return None
        return CheckStep(
            item_tag=command.tag,
            action="verify-uidvalidity",
            purpose="Confirm UIDVALIDITY of destination '%s' matches the "
                    "recorded correlation token (%s)" % (mailbox, expected),
            mailbox=mailbox,
            imap_command="STATUS %s (UIDVALIDITY)" % _mailbox_token(mailbox),
            expects=("uidvalidity",),
        )

    def _store_parts(
        self,
        command: InterruptedCommand,
    ) -> Tuple[str, str, Tuple[str, ...]]:
        args = command.call_args
        message_set = args[0]
        flag_op = args[1].upper()
        flags = _parse_flags(args[2]) if len(args) > 2 else ()
        return message_set, flag_op, flags

    def _sequence_uids_from_evidence(
        self,
        item: RecoveryItem,
        evidence: ServerEvidence,
        message_set: str,
    ) -> Optional[List[int]]:
        mapping = evidence.sequence_to_uid.get(item.command.mailbox)
        if not mapping:
            return None  # The FETCH (UID FLAGS) check has not been fed back yet.
        try:
            return [mapping[seq] for seq in _parse_uid_set(message_set)]
        except KeyError:
            return None

    def _partition_destination(
        self,
        evidence: ServerEvidence,
        destination: str,
        uids: Sequence[int],
    ) -> Tuple[Tuple[int, ...], Tuple[int, ...]]:
        present = tuple(uid for uid in uids
                        if evidence.message(destination, uid) is not None)
        absent = tuple(uid for uid in uids
                       if evidence.message(destination, uid) is None)
        return present, absent

    def _uidvalidity_mismatch(
        self,
        evidence: ServerEvidence,
        mailbox: str,
        expected: int,
    ) -> bool:
        observed = evidence.uidvalidity.get(mailbox)
        return observed is not None and observed != expected

    def _flags_satisfy(
        self,
        current_flags: Sequence[str],
        operation: str,
        wanted: Sequence[str],
    ) -> bool:
        current = {flag.lower() for flag in current_flags}
        wanted_set = {flag.lower() for flag in wanted}
        if operation == "FLAGS":
            return wanted_set <= current
        if operation in ("+FLAGS", "ADD"):
            return wanted_set <= current
        if operation in ("-FLAGS", "REMOVE"):
            return not (wanted_set & current)
        raise ValueError("unknown STORE operation %r" % operation)

    def _store_reissue_command(
        self,
        command: InterruptedCommand,
        uids: Sequence[int],
    ) -> str:
        _set, flag_op, flags = self._store_parts(command)
        flag_text = "(%s)" % " ".join(flags) if flags else "()"
        return "UID STORE %s %s %s" % (
            _join_uid_set(uids), flag_op, flag_text)

    def _copy_reissue_step(
        self,
        item: RecoveryItem,
        record: CopyUidRecord,
        destination: str,
    ) -> CheckStep:
        # COPYUID supplies UIDs, so the re-issue is always UID-scoped even
        # if the original command used sequence numbers.
        return CheckStep(
            item_tag=item.tag,
            action=ACTION_REISSUE_COPY,
            purpose="Evidence shows the COPY did not (fully) take effect; "
                    "re-issue only the unconfirmed source UIDs",
            mailbox=item.command.mailbox,
            imap_command="UID COPY %s %s" % (
                _join_uid_set(record.source_uids),
                _mailbox_token(destination)),
            expects=("tagged-ok", "copyuid"),
            reissue=True,
        )

    def _move_reissue_command(
        self,
        command: InterruptedCommand,
        record: CopyUidRecord,
    ) -> str:
        destination = command.call_args[-1]
        return "UID MOVE %s %s" % (
            _join_uid_set(record.source_uids),
            _mailbox_token(destination))

    def _resolved(
        self,
        command: InterruptedCommand,
        reason: str,
        completion: Optional[RecordedResponse],
    ) -> RecoveryItem:
        detail = {}
        if completion is not None:
            detail["response"] = completion
        return RecoveryItem(
            tag=command.tag,
            command=command,
            status=STATUS_RESOLVED,
            reason=reason,
            detail=detail,
        )

    def _manual(
        self,
        command: InterruptedCommand,
        reason: str,
        explanation: str,
    ) -> RecoveryItem:
        return RecoveryItem(
            tag=command.tag,
            command=command,
            status=STATUS_PENDING_MANUAL,
            reason=reason,
            checks=[CheckStep(
                item_tag=command.tag,
                action="manual-review",
                purpose=explanation,
                mailbox=command.mailbox,
                executable=False,
            )],
            detail={"explanation": explanation},
        )

    def _downgrade_manual(self, item: RecoveryItem, reason: str,
                          explanation: Optional[str] = None) -> None:
        item.status = STATUS_PENDING_MANUAL
        item.reason = reason
        item.action = None
        item.checks = [CheckStep(
            item_tag=item.tag,
            action="manual-review",
            purpose=explanation or "Recovery evidence is inconclusive",
            mailbox=item.command.mailbox,
            executable=False,
        )]


def _parse_flags(raw: str) -> Tuple[str, ...]:
    cleaned = raw.strip()
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = cleaned[1:-1]
    return tuple(part for part in cleaned.split() if part)


def _parse_uid_set(raw: str) -> List[int]:
    uids: List[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            start, end = part.split(":", 1)
            uids.extend(range(int(start), int(end) + 1))
        else:
            uids.append(int(part))
    return uids


def _join_uid_set(uids: Sequence[int]) -> str:
    if not uids:
        raise ValueError("empty UID set")
    ordered = sorted(set(uids))
    ranges: List[str] = []
    start = previous = ordered[0]
    for uid in ordered[1:]:
        if uid == previous + 1:
            previous = uid
            continue
        ranges.append(str(start) if start == previous else "%s:%s" % (start, previous))
        start = previous = uid
    ranges.append(str(start) if start == previous else "%s:%s" % (start, previous))
    return ",".join(ranges)


def _mailbox_token(mailbox: str) -> str:
    if mailbox.startswith(("\"", "'")) or " " not in mailbox:
        return mailbox
    return "\"%s\"" % mailbox.replace("\\", "\\\\").replace("\"", "\\\"")
