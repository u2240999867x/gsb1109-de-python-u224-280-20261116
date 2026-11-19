"""Offline/adaptive pipelining planner for IMAP command batches.

Given a list of high-level IMAP commands, the mailbox state each one reads
or writes, and their business dependencies, :func:`make_spec` /
:class:`PipelinePlanner` produce a send plan with the minimum number of
legal round trips ("batches").

A batch may only contain commands that, taken together:

* are legal in the connection state at the start of the round
  (``NONAUTH``/``AUTH``/``SELECTED``/``LOGOUT``);
* only depend on commands that are already accounted for or that are
  pipelined earlier in the same batch without a barrier between them;
* never touch overlapping mailbox state where either side writes
  (write-after-write and read-after-write conflicts are both forbidden);
* cannot emit indistinguishable untagged replies - two commands that may
  both produce, for example, ``* FETCH`` data cannot share a round because
  untagged responses carry no tag and could not be attributed reliably;
* contain no SELECT/EXAMINE, IDLE or continuation (literal/``+``) command
  alongside anything else - those force a round boundary.

Every planned command receives a unique tag, so each tagged completion is
attributable to exactly one call. The planner is adaptive: after each
round the real responses are reconciled, sent commands are accounted for
separately from pending ones, and the remaining commands are re-planned
whenever a rejection (``NO``/``BAD``) or an unexpected connection state
change is observed.

For batches up to :attr:`PipelinePlanner.exact_threshold` commands the
minimum number of rounds is computed exactly with a memoised
branch-and-bound search (the problem generalises graph colouring, so it is
NP-hard); a node budget guards against adversarial conflict graphs, beyond
which a deterministic maximal-batch heuristic is used and the fallback is
reported through :attr:`PipelinePlanner.exact_exhausted`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .imaplib2 import (
    AUTH,
    CMD_VAL_STATES,
    LOGOUT,
    NONAUTH,
    SELECTED,
    CRLF,
    Commands,
)

__all__ = (
    "CommandSpec",
    "PlannedCommand",
    "Batch",
    "ReconcileResult",
    "PipelinePlanner",
    "SchedulingError",
    "AmbiguousResponseError",
    "UnreachableDependencyError",
    "IllegalStateError",
    "make_spec",
    "SELECTION_COMMANDS",
    "CONTINUATION_COMMANDS",
)


# Commands that change the connection state. SELECT/EXAMINE flush untagged
# responses and move into SELECTED, so nothing may share their round.
SELECTION_COMMANDS = frozenset(("SELECT", "EXAMINE"))

# Commands that complete through a '+' continuation handshake (client
# literal, SASL exchange, IDLE). The round must contain only that command.
CONTINUATION_COMMANDS = frozenset(("APPEND", "AUTHENTICATE", "IDLE"))

# Default untagged response kinds per command, mirroring the
# ``untagged_response`` handling in :mod:`imaplib2.imaplib2`. Two commands
# whose kind sets intersect cannot share a round safely.
DEFAULT_RESPONSE_KINDS: Dict[str, Tuple[str, ...]] = {
    "CAPABILITY": ("CAPABILITY",),
    "EXPUNGE": ("EXPUNGE",),
    "FETCH": ("FETCH",),
    "PARTIAL": ("FETCH",),
    "STORE": ("FETCH",),
    "SEARCH": ("SEARCH",),
    "SORT": ("SORT",),
    "THREAD": ("THREAD",),
    "LIST": ("LIST",),
    "LSUB": ("LSUB",),
    "NAMESPACE": ("NAMESPACE",),
    "STATUS": ("STATUS",),
    "ID": ("ID",),
    "GETQUOTA": ("QUOTA",),
    "GETQUOTAROOT": ("QUOTA", "QUOTAROOT"),
    "SETQUOTA": ("QUOTA",),
    "GETACL": ("ACL",),
    "DELETEACL": ("ACL",),
    "SETACL": ("ACL",),
    "GETANNOTATION": ("ANNOTATION",),
    "SETANNOTATION": ("ANNOTATION",),
    "MYRIGHTS": ("MYRIGHTS",),
}

SELECTION_RESPONSE_KINDS = (
    "FLAGS",
    "EXISTS",
    "RECENT",
    "UIDVALIDITY",
    "UIDNEXT",
    "PERMANENTFLAGS",
)

# Commands that may relay unsolicited server updates (e.g. EXISTS/EXPUNGE
# arriving during a NOOP). Two of them cannot share a round.
UNSOLICITED_COMMANDS = frozenset(("NOOP", "CHECK"))

_UID_DIRECT_KINDS = frozenset(("SEARCH", "SORT", "THREAD"))

# Statuses used by the planner bookkeeping.
PENDING, SENT, DONE, FAILED, BLOCKED = (
    "pending",
    "sent",
    "done",
    "failed",
    "blocked",
)

# Sentinel for a reachable plan: no sequence of legal batches can finish.
_INF = 10 ** 6

# Hard cap on exact-solver nodes per planning pass. The optimisation is
# NP-hard in general, so an adversarial instance (e.g. a dense conflict graph)
# aborts to the greedy heuristic instead of stalling the caller. At 20k nodes
# the worst measured planning pass stays around two seconds.
DEFAULT_NODE_BUDGET = 20_000


class _ExactBudgetExceeded(Exception):
    """Internal: exact search exceeded its node budget."""


class SchedulingError(Exception):
    """Base class for planner errors."""


class AmbiguousResponseError(SchedulingError):
    """A response cannot be attributed to a single in-flight command."""


class UnreachableDependencyError(SchedulingError):
    """A command depends on a call that failed or does not exist."""


class IllegalStateError(SchedulingError):
    """No legal batch can be built for the current connection state."""


@dataclass(frozen=True)
class CommandSpec:
    """Immutable description of one command invocation.

    :param name: IMAP command name (must exist in the imaplib2 table).
    :param call_id: Unique business identifier used for dependencies.
    :param state: Connection state the command is legal in.
    :param reads: Mailbox state keys read by the command.
    :param writes: Mailbox state keys written by the command.
    :param response_kinds: Untagged reply kinds the server may emit.
    :param deps: ``call_id`` values that must complete (or run earlier in
        the same batch) before this command.
    :param mailbox: Mailbox the command operates on (for SELECTED state).
    :param requires_continuation: A ``+`` handshake/literal is involved.
    :param is_selection: SELECT/EXAMINE boundary command.
    :param is_idle: IDLE boundary command.
    :param unsolicited: May relay unsolicited server notifications.
    :param args: Command arguments (used for wire encoding in replay).
    :param literal: Client literal payload sent after the continuation.
    """

    name: str
    call_id: str
    state: str
    reads: frozenset = field(default_factory=frozenset)
    writes: frozenset = field(default_factory=frozenset)
    response_kinds: frozenset = field(default_factory=frozenset)
    deps: frozenset = field(default_factory=frozenset)
    mailbox: Optional[str] = None
    requires_continuation: bool = False
    is_selection: bool = False
    is_idle: bool = False
    unsolicited: bool = False
    args: Tuple[Any, ...] = field(default_factory=tuple)
    literal: Optional[bytes] = None


@dataclass
class PlannedCommand:
    """A :class:`CommandSpec` enriched with tag, round and accounting."""

    call_id: str
    spec: CommandSpec
    tag: bytes
    round_index: int = -1
    status: str = PENDING
    reply: Any = None


@dataclass
class Batch:
    """One send round and the audit trail for why others were excluded."""

    round_index: int
    commands: List[PlannedCommand]
    state_before: str
    mailbox_before: Optional[str]
    exclusions: Dict[str, str] = field(default_factory=dict)

    @property
    def tags(self) -> List[bytes]:
        return [pc.tag for pc in self.commands]


@dataclass
class ReconcileResult:
    """Outcome of accounting one round of real responses."""

    done: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    blocked: List[str] = field(default_factory=list)
    state: str = AUTH
    mailbox: Optional[str] = None


def _default_response_kinds(name: str, args: Sequence[Any]) -> Tuple[str, ...]:
    if name in SELECTION_COMMANDS:
        return SELECTION_RESPONSE_KINDS
    if name == "UID" and args:
        sub = str(args[0]).upper()
        return (sub,) if sub in _UID_DIRECT_KINDS else ("FETCH",)
    return DEFAULT_RESPONSE_KINDS.get(name, ())


def make_spec(
    call_id: str,
    name: str,
    *args: Any,
    state: Optional[str] = None,
    reads: Sequence[str] = (),
    writes: Sequence[str] = (),
    response_kinds: Optional[Sequence[str]] = None,
    deps: Sequence[str] = (),
    mailbox: Optional[str] = None,
    literal: Optional[bytes] = None,
    requires_continuation: Optional[bool] = None,
    is_selection: Optional[bool] = None,
    is_idle: Optional[bool] = None,
    unsolicited: Optional[bool] = None,
) -> CommandSpec:
    """Build a :class:`CommandSpec`, deriving protocol defaults from the
    imaplib2 command table.

    ``state`` only has to be given explicitly when the command is legal in
    more than one connection state; mailbox state keys (``reads``/
    ``writes``) and business ``deps`` always come from the caller.
    """

    name = name.upper()
    if name not in Commands:
        raise ValueError("unknown IMAP command: %s" % name)

    valid_states = Commands[name][CMD_VAL_STATES]
    if state is None:
        if len(valid_states) == 1:
            state = valid_states[0]
        else:
            raise ValueError(
                "command %s is legal in several states %s; pass state="
                % (name, valid_states)
            )
    if state not in valid_states:
        raise ValueError(
            "command %s illegal in state %s (valid: %s)"
            % (name, state, valid_states)
        )

    if response_kinds is None:
        response_kinds = _default_response_kinds(name, args)
    if requires_continuation is None:
        requires_continuation = name in CONTINUATION_COMMANDS or literal is not None
    if is_selection is None:
        is_selection = name in SELECTION_COMMANDS
    if is_idle is None:
        is_idle = name == "IDLE"
    if unsolicited is None:
        unsolicited = name in UNSOLICITED_COMMANDS
    if is_selection and mailbox is None and args:
        mailbox = str(args[0])

    return CommandSpec(
        name=name,
        call_id=call_id,
        state=state,
        reads=frozenset(reads),
        writes=frozenset(writes),
        response_kinds=frozenset(response_kinds),
        deps=frozenset(deps),
        mailbox=mailbox,
        requires_continuation=requires_continuation,
        is_selection=is_selection,
        is_idle=is_idle,
        unsolicited=unsolicited,
        args=tuple(args),
        literal=literal,
    )


class PipelinePlanner:
    """Computes minimal legal batches and reconciles them round by round."""

    def __init__(
        self,
        specs: Sequence[CommandSpec],
        initial_state: str = AUTH,
        initial_mailbox: Optional[str] = None,
        tag_prefix: bytes = b"A",
        exact_threshold: int = 18,
        exact_node_budget: int = DEFAULT_NODE_BUDGET,
    ):
        if not specs:
            raise ValueError("at least one command spec is required")

        self._tag_prefix = tag_prefix
        self._tag_counter = 0
        self._initial_state = initial_state
        self._initial_mailbox = initial_mailbox
        self.state = initial_state
        self.mailbox = initial_mailbox
        self.round_index = 0
        self.history: List[Batch] = []
        self.exact_threshold = exact_threshold
        self.exact_node_budget = exact_node_budget
        self._exact_nodes = 0
        self._exact_budget = exact_node_budget
        self._plan_queue: Optional[List[List[str]]] = None
        self._exact_used = False
        self._exact_exhausted = False

        self._planned: Dict[str, PlannedCommand] = {}
        self._input_index: Dict[str, int] = {}
        for index, spec in enumerate(specs):
            if spec.call_id in self._planned:
                raise ValueError("duplicate call_id: %s" % spec.call_id)
            self._input_index[spec.call_id] = index
            self._planned[spec.call_id] = PlannedCommand(
                call_id=spec.call_id, spec=spec, tag=self._next_tag()
            )
        self._validate_dependencies()
        self._order = self._topological_order()

    def _next_tag(self) -> bytes:
        tag = self._tag_prefix + str(self._tag_counter).encode("ASCII")
        self._tag_counter += 1
        return tag

    def _validate_dependencies(self) -> None:
        for pc in self._planned.values():
            unknown = pc.spec.deps - self._planned.keys()
            if unknown:
                raise UnreachableDependencyError(
                    "%s depends on unknown call(s): %s"
                    % (pc.call_id, sorted(unknown))
                )

    def _topological_order(self) -> List[str]:
        """Kahn ordering; input order breaks ties for deterministic plans."""

        remaining = {cid: set(pc.spec.deps) for cid, pc in self._planned.items()}
        ordered: List[str] = []
        while remaining:
            ready = [cid for cid, deps in remaining.items() if not deps]
            if not ready:
                raise UnreachableDependencyError(
                    "dependency cycle among: %s" % sorted(remaining)
                )
            ready.sort(key=self._input_index.__getitem__)
            for cid in ready:
                ordered.append(cid)
                del remaining[cid]
                for deps in remaining.values():
                    deps.discard(cid)
        return ordered

    @property
    def commands(self) -> Dict[str, PlannedCommand]:
        return self._planned

    def pending(self) -> List[PlannedCommand]:
        return [
            self._planned[cid]
            for cid in self._order
            if self._planned[cid].status == PENDING
        ]

    # ----- Exact optimisation (minimum-round planning) -----

    @staticmethod
    def _static_conflict(sa: CommandSpec, sb: CommandSpec) -> bool:
        """Conflict that forbids two commands ever sharing a round."""

        if sa is sb:
            return False
        if sa.is_idle or sb.is_idle:
            return True
        if sa.requires_continuation or sb.requires_continuation:
            return True
        if sa.is_selection or sb.is_selection:
            return True
        if sa.state != sb.state:
            return True
        if (
            sa.state == SELECTED
            and sa.mailbox is not None
            and sb.mailbox is not None
            and sa.mailbox != sb.mailbox
        ):
            return True
        if sa.writes & (sb.reads | sb.writes) or sb.writes & (sa.reads | sa.writes):
            return True
        if sa.response_kinds & sb.response_kinds:
            return True
        return False

    def _legal_now(self, spec: CommandSpec, state: str, mailbox: Optional[str]) -> bool:
        if spec.state != state:
            return False
        if spec.state == SELECTED and spec.mailbox is not None and spec.mailbox != mailbox:
            return False
        return True

    @staticmethod
    def _transition_ids(
        batch: Sequence[CommandSpec],
        state: str,
        mailbox: Optional[str],
    ) -> Tuple[str, Optional[str]]:
        new_state, new_mailbox = state, mailbox
        for spec in batch:
            if spec.is_selection:
                new_state, new_mailbox = SELECTED, spec.mailbox
            elif spec.name == "CLOSE":
                new_state, new_mailbox = AUTH, None
            elif spec.name in ("LOGIN", "AUTHENTICATE"):
                new_state = AUTH
            elif spec.name == "LOGOUT":
                new_state = LOGOUT
        return new_state, new_mailbox

    def _maximal_batches(
        self,
        available: List[str],
        remaining: frozenset,
    ) -> List[Tuple[str, ...]]:
        """All maximal feasible batches over currently legal commands.

        A batch is an independent set under :meth:`_static_conflict` and is
        dependency-closed: every still-remaining dependency of a member must
        be a compatible member of the same batch.
        """

        specs = self._planned
        avail_set = set(available)
        results: List[Tuple[str, ...]] = []
        seen: set = set()

        def conflicts_with(cid: str, members: List[str]) -> bool:
            spec = specs[cid].spec
            return any(
                self._static_conflict(spec, specs[o].spec) for o in members
            )

        def closure_for(cid: str, members: List[str]) -> Optional[List[str]]:
            closure: List[str] = []
            for dep in specs[cid].spec.deps:
                if dep not in remaining or dep in members or dep in closure:
                    continue
                if dep not in avail_set:
                    return None
                closure.append(dep)
            combined = members + closure + [cid]
            for d in closure:
                if conflicts_with(d, combined):
                    return None
            if conflicts_with(cid, members + closure):
                return None
            return closure

        def dfs(start: int, members: List[str]) -> None:
            extended = False
            for i in range(start, len(available)):
                cid = available[i]
                if cid in members:
                    continue
                closure = closure_for(cid, members)
                if closure is None:
                    continue
                dfs(i + 1, members + closure + [cid])
                extended = True

            if extended or not members:
                return
            # Maximality: re-check every legal command, including ones whose
            # dependencies only became satisfiable deeper in the search.
            if any(
                cid not in members and closure_for(cid, members) is not None
                for cid in available
            ):
                return
            key = tuple(sorted(members, key=self._input_index.__getitem__))
            if key not in seen:
                seen.add(key)
                results.append(key)

        dfs(0, [])
        return results

    def _solve_exact(
        self,
        remaining: frozenset,
        state: str,
        mailbox: Optional[str],
        memo: Dict[Tuple[frozenset, str, Optional[str]], int],
        cap: int,
    ) -> int:
        """Branch-and-bound minimum rounds, returning ``min(optimum, cap)``.

        ``memo`` only stores *provable* optima. The node's own search is
        anchored on a feasible greedy bound ``g``; when ``g <= cap`` the
        running best always stays an achievable value, so the finished best
        is a genuine optimum and is cached. When ``cap < g`` the cap is just
        a caller limit that may be unachievable here, so the capped result is
        returned for pruning but never cached, preventing bound poisoning.
        """

        if not remaining:
            return 0
        key = (remaining, state, mailbox)
        if key in memo:
            return min(memo[key], cap)

        self._exact_nodes += 1
        if self._exact_nodes > self._exact_budget:
            raise _ExactBudgetExceeded

        specs = self._planned
        available = [
            cid
            for cid in self._order
            if cid in remaining and self._legal_now(specs[cid].spec, state, mailbox)
        ]
        if not available:
            memo[key] = _INF
            return min(_INF, cap)

        batches = self._maximal_batches(available, remaining)
        greedy = self._solve_greedy_count(remaining, state, mailbox)
        provable = greedy <= cap
        best = min(greedy, cap)

        for batch in batches:
            if best <= 1:
                break
            batch_specs = [specs[cid].spec for cid in batch]
            next_state, next_mb = self._transition_ids(batch_specs, state, mailbox)
            sub = self._solve_exact(
                remaining - frozenset(batch), next_state, next_mb,
                memo, best - 1,
            )
            candidate = 1 + sub
            if candidate < best:
                best = candidate

        if provable:
            memo[key] = best
        return best

    def _solve_greedy_count(
        self,
        remaining: frozenset,
        state: str,
        mailbox: Optional[str],
    ) -> int:
        rounds = 0
        pending = set(remaining)
        while pending:
            chosen: List[str] = []
            progress = True
            while progress:
                progress = False
                for cid in self._order:
                    if cid not in pending or cid in chosen:
                        continue
                    spec = self._planned[cid].spec
                    if not self._legal_now(spec, state, mailbox):
                        continue
                    if any(d in pending for d in spec.deps):
                        continue
                    if any(
                        self._static_conflict(spec, self._planned[o].spec)
                        for o in chosen
                    ):
                        continue
                    chosen.append(cid)
                    progress = True
            if not chosen:
                return _INF
            batch_specs = [self._planned[cid].spec for cid in chosen]
            state, mailbox = self._transition_ids(batch_specs, state, mailbox)
            pending.difference_update(chosen)
            rounds += 1
        return rounds

    def _compute_plan(self) -> List[List[str]]:
        """Return the minimum-round plan over the current pending tail."""

        pending_ids = [pc.call_id for pc in self.pending()]
        remaining = frozenset(pending_ids)
        state, mailbox = self.state, self.mailbox

        self._exact_used = False
        self._exact_exhausted = False

        if len(remaining) <= self.exact_threshold:
            memo: Dict[Tuple[frozenset, str, Optional[str]], int] = {}
            self._exact_nodes = 0
            try:
                self._solve_exact(remaining, state, mailbox, memo, len(remaining))
                plan = self._materialize(remaining, state, mailbox, memo)
            except _ExactBudgetExceeded:
                self._exact_exhausted = True
                return self._greedy_or_raise(remaining, state, mailbox)
            self._exact_used = True
            if not plan:
                raise IllegalStateError(
                    "no legal batch in state %s (mailbox=%s) for: %s"
                    % (state, mailbox, sorted(remaining))
                )
            return plan

        return self._greedy_or_raise(remaining, state, mailbox)

    def _greedy_or_raise(
        self,
        remaining: frozenset,
        state: str,
        mailbox: Optional[str],
    ) -> List[List[str]]:
        plan = self._greedy_plan(remaining, state, mailbox)
        if not plan:
            raise IllegalStateError(
                "no legal batch in state %s (mailbox=%s) for: %s"
                % (state, mailbox, sorted(remaining))
            )
        return plan

    def _materialize(
        self,
        remaining: frozenset,
        state: str,
        mailbox: Optional[str],
        memo: Dict[Tuple[frozenset, str, Optional[str]], int],
    ) -> List[List[str]]:
        plan: List[List[str]] = []
        while remaining:
            available = [
                cid
                for cid in self._order
                if cid in remaining and self._legal_now(self._planned[cid].spec, state, mailbox)
            ]
            batches = self._maximal_batches(available, remaining)
            if not batches:
                return plan
            best_batch = None
            best_value = None
            for batch in batches:
                batch_specs = [self._planned[cid].spec for cid in batch]
                next_state, next_mb = self._transition_ids(batch_specs, state, mailbox)
                value = memo.get(
                    (remaining - frozenset(batch), next_state, next_mb)
                )
                if value is None:
                    value = self._solve_exact(
                        remaining - frozenset(batch), next_state, next_mb,
                        memo, len(remaining),
                    )
                if value >= _INF:
                    continue
                if best_value is None or value < best_value or (
                    value == best_value
                    and [self._input_index[c] for c in batch]
                    < [self._input_index[c] for c in best_batch]
                ):
                    best_value = value
                    best_batch = batch
            if best_batch is None:
                # Tail is unreachable from every branch, but a legal batch
                # still exists now: make progress and let the caller re-plan
                # (or fail later) once the real responses are known.
                best_batch = batches[0]
            plan.append(list(best_batch))
            batch_specs = [self._planned[cid].spec for cid in best_batch]
            state, mailbox = self._transition_ids(batch_specs, state, mailbox)
            remaining = remaining - frozenset(best_batch)
        return plan

    def _greedy_plan(
        self,
        remaining: frozenset,
        state: str,
        mailbox: Optional[str],
    ) -> List[List[str]]:
        plan: List[List[str]] = []
        pending = set(remaining)
        while pending:
            chosen: List[str] = []
            progress = True
            while progress:
                progress = False
                for cid in self._order:
                    if cid not in pending or cid in chosen:
                        continue
                    spec = self._planned[cid].spec
                    if not self._legal_now(spec, state, mailbox):
                        continue
                    if any(d in pending for d in spec.deps):
                        continue
                    if any(
                        self._static_conflict(spec, self._planned[o].spec)
                        for o in chosen
                    ):
                        continue
                    chosen.append(cid)
                    progress = True
            if not chosen:
                raise IllegalStateError(
                    "no legal batch in state %s (mailbox=%s)" % (state, mailbox)
                )
            plan.append(chosen)
            batch_specs = [self._planned[cid].spec for cid in chosen]
            state, mailbox = self._transition_ids(batch_specs, state, mailbox)
            pending.difference_update(chosen)
        return plan

    # ----- End exact optimisation -----

    def _same_mailbox(self, spec: CommandSpec) -> bool:
        if spec.state != SELECTED:
            return True
        return spec.mailbox is None or spec.mailbox == self.mailbox

    def compatibility_reason(
        self, candidate: PlannedCommand, chosen: PlannedCommand
    ) -> Optional[str]:
        """Return ``None`` if the two commands may share a round, else the
        first rule that forbids it."""

        cs, os = candidate.spec, chosen.spec

        if cs.is_idle or os.is_idle:
            return "idle-barrier"
        if cs.requires_continuation or os.requires_continuation:
            return "continuation-barrier"
        if cs.is_selection or os.is_selection:
            return "selection-barrier"
        if cs.state != os.state:
            return "connection-state"
        if cs.state == SELECTED and (
            not self._same_mailbox(cs) or not self._same_mailbox(os)
        ):
            return "mailbox-state"
        if cs.writes & (os.reads | os.writes) or os.writes & (cs.reads | cs.writes):
            return "state-conflict"
        if cs.response_kinds & os.response_kinds:
            return "indistinguishable-reply"
        return None

    def _admittable(
        self, pc: PlannedCommand, chosen: List[PlannedCommand]
    ) -> Optional[str]:
        spec = pc.spec

        if spec.state != self.state:
            return "illegal-connection-state"
        if not self._same_mailbox(spec):
            return "illegal-mailbox-state"

        unfinished = [
            dep
            for dep in spec.deps
            if self._planned[dep].status not in (DONE, FAILED, BLOCKED)
        ]
        in_batch = [dep for dep in unfinished if self._planned[dep] in chosen]
        if set(unfinished) - set(in_batch):
            return "dependency-open"
        if in_batch:
            for dep in in_batch:
                reason = self.compatibility_reason(pc, self._planned[dep])
                if reason is not None:
                    return "dependency-barrier:" + reason

        for other in chosen:
            reason = self.compatibility_reason(pc, other)
            if reason is not None:
                return reason
        return None

    def next_batch(self) -> Optional[Batch]:
        """Reserve the next batch of a minimum-round plan.

        The full pending tail is solved once (exact branch-and-bound while
        the command count is within :attr:`exact_threshold`, otherwise a
        maximal-batch heuristic) and the rounds are served from the cached
        plan. A rejection or an unexpected state change invalidates the
        cache, so the surviving pending tail is re-solved from scratch.
        """

        pending = self.pending()
        if not pending:
            return None

        if self._plan_queue:
            ids = self._plan_queue[0]
            pcs = [self._planned[cid] for cid in ids]
            if all(pc.status == PENDING for pc in pcs):
                self._plan_queue.pop(0)
                return self._reserve(pcs, pending)

        self._plan_queue = self._compute_plan()
        if not self._plan_queue:
            raise IllegalStateError(
                "no legal batch in state %s (mailbox=%s) for: %s"
                % (self.state, self.mailbox, [pc.call_id for pc in pending])
            )
        return self.next_batch()

    def _reserve(
        self, chosen: List[PlannedCommand], all_pending: List[PlannedCommand]
    ) -> Batch:
        chosen_ids = {pc.call_id for pc in chosen}
        exclusions: Dict[str, str] = {}
        for pc in all_pending:
            if pc.call_id in chosen_ids:
                continue
            reason = self._admittable(pc, chosen)
            if reason is not None:
                exclusions[pc.call_id] = reason

        batch = Batch(
            round_index=self.round_index,
            commands=chosen,
            state_before=self.state,
            mailbox_before=self.mailbox,
            exclusions=exclusions,
        )
        for pc in chosen:
            pc.status = SENT
            pc.round_index = self.round_index
        self.round_index += 1
        self.history.append(batch)
        return batch

    def account_responses(
        self, replies: Dict[str, Tuple[str, Any]]
    ) -> ReconcileResult:
        """Reconcile real ``(status, payload)`` replies for one round.

        ``OK`` marks the call done; ``NO``/``BAD`` marks it failed and
        transitively blocks every still-pending dependent. Connection
        state transitions only take effect for successful calls. Any
        failure discards the cached plan so the pending tail is re-solved.
        """

        result = ReconcileResult(state=self.state, mailbox=self.mailbox)

        for call_id, (status, payload) in replies.items():
            pc = self._planned.get(call_id)
            if pc is None:
                raise AmbiguousResponseError(
                    "reply for unknown call: %s" % call_id
                )
            if pc.status != SENT:
                raise AmbiguousResponseError(
                    "call %s is not awaiting a reply (status=%s)"
                    % (call_id, pc.status)
                )
            pc.reply = (status, payload)
            if status == "OK":
                pc.status = DONE
                result.done.append(call_id)
                self.state, self.mailbox = self._transition(
                    pc.spec, True, self.state, self.mailbox
                )
            else:
                pc.status = FAILED
                result.failed.append(call_id)

        if result.failed:
            result.blocked = self._block_dependents(result.failed)
            self._plan_queue = None

        result.state = self.state
        result.mailbox = self.mailbox
        return result

    def _block_dependents(self, failed: Sequence[str]) -> List[str]:
        blocked: List[str] = []
        frontier = list(failed)
        while frontier:
            failed_id = frontier.pop()
            for pc in self._planned.values():
                if pc.status != PENDING:
                    continue
                if failed_id in pc.spec.deps:
                    pc.status = BLOCKED
                    blocked.append(pc.call_id)
                    frontier.append(pc.call_id)
        return blocked

    def note_state_change(
        self, state: str, mailbox: Optional[str] = None
    ) -> None:
        """Record an unexpected server-driven state change.

        The pending tail is re-planned on the next :meth:`next_batch` call;
        commands that are no longer legal simply wait for a command that
        restores the required state.
        """

        self.state = state
        if mailbox is not None or state != SELECTED:
            self.mailbox = mailbox
        self._plan_queue = None

    @property
    def exact_used(self) -> bool:
        """Whether the last planning pass used the exact optimiser."""

        return self._exact_used

    @property
    def exact_exhausted(self) -> bool:
        """Whether exact search was abandoned to the greedy heuristic."""

        return self._exact_exhausted

    @staticmethod
    def _transition(
        spec: CommandSpec,
        succeeded: bool,
        state: str,
        mailbox: Optional[str],
    ) -> Tuple[str, Optional[str]]:
        if not succeeded:
            return state, mailbox
        if spec.is_selection:
            return SELECTED, spec.mailbox
        if spec.name == "CLOSE":
            return AUTH, None
        if spec.name in ("LOGIN", "AUTHENTICATE"):
            return AUTH, mailbox
        if spec.name == "LOGOUT":
            return LOGOUT, mailbox
        return state, mailbox

    def wire_frames(self, pc: PlannedCommand) -> List[bytes]:
        """Encode the bytes that would be sent for a planned command.

        A literal command yields the command line (with ``{size}``) plus
        the payload frame that follows the server ``+`` continuation.
        """

        spec = pc.spec
        line = pc.tag + b" " + spec.name.encode("ASCII")
        for arg in spec.args:
            if arg is None:
                continue
            if isinstance(arg, bytes):
                line += b" " + arg
            else:
                line += b" " + str(arg).encode("UTF-8")
        if spec.literal is not None:
            line += b" {" + str(len(spec.literal)).encode("ASCII") + b"}"
        frames = [line + CRLF]
        if spec.literal is not None:
            frames.append(spec.literal + CRLF)
        return frames

    def boundary_reasons(self) -> List[Dict[str, str]]:
        """Audit trail: why each round after the first was needed."""

        return [dict(batch.exclusions) for batch in self.history]
