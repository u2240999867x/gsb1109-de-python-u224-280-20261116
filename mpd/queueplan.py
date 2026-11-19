"""Transform an MPD playlist into a target queue of URIs.

The planner works off a *snapshot* of the current queue where every entry is
identified by its MPD ``songid`` (the value returned by ``playlistinfo`` and
used by ``deleteid`` / ``moveid``).  Duplicate URIs are distinguished by their
occurrence ordinal and their songid, never by pairing equal URIs arbitrarily.

Three legal actions are emitted, each carrying enough information to verify
which concrete occurrence it operates on before sending it:

* ``InsertAction``  -> ``addid <uri> <position>``
* ``DeleteAction``  -> ``deleteid <songid>``
* ``MoveAction``    -> ``moveid <songid> <to_position>``

Position changes are simulated with the real MPD command semantics:
``moveid`` detaches the song first and, when moving rightwards, MPD inserts
it before the song that currently occupies the requested position (i.e. the
daemon decrements the target index by one after removal).  ``addid`` with a
position inserts directly into the current queue.
"""

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

__all__ = [
    "CostTable",
    "DeleteAction",
    "InsertAction",
    "MoveAction",
    "Plan",
    "QueuePlanError",
    "QueuePlanner",
    "QueueSnapshot",
    "SnapshotEntry",
    "SnapshotIncompatibleError",
    "build_plan",
    "simulate_plan",
    "take_snapshot",
]


class QueuePlanError(Exception):
    """Raised when a plan cannot be built or executed to completion."""


class SnapshotIncompatibleError(QueuePlanError):
    """Raised when a snapshot is partial or internally inconsistent.

    No destructive action (in particular no ``deleteid``) is generated or
    executed against a queue in this state.
    """


@dataclass(frozen=True)
class SnapshotEntry:
    """One queue occurrence at the moment the snapshot was taken."""

    songid: int
    uri: str
    occurrence: int = 0


@dataclass(frozen=True)
class QueueSnapshot:
    """Immutable, generation-tagged view of the MPD queue.

    ``generation`` is an opaque token chosen by the caller; when absent a
    content-defined token over the ``(songid, uri)`` sequence is used.
    """

    entries: Tuple[SnapshotEntry, ...]
    generation: Optional[str] = None

    @property
    def uris(self) -> Tuple[str, ...]:
        return tuple(entry.uri for entry in self.entries)

    @property
    def songids(self) -> Tuple[int, ...]:
        return tuple(entry.songid for entry in self.entries)

    def position_of(self, songid: int) -> int:
        for index, entry in enumerate(self.entries):
            if entry.songid == songid:
                return index
        raise QueuePlanError(f"songid {songid} is not part of the snapshot")


@dataclass(frozen=True)
class CostTable:
    """Per-command cost and the set of allowed actions.

    All costs must be non-negative.  Disabled action kinds are treated as
    infinitely expensive; planning raises when no feasible plan remains.
    Moving an existing occurrence is only preferred over delete+insert when
    ``move < insert + delete``.
    """

    insert: float = 1.0
    delete: float = 1.0
    move: float = 1.0
    insert_enabled: bool = True
    delete_enabled: bool = True
    move_enabled: bool = True

    def __post_init__(self) -> None:
        for name in ("insert", "delete", "move"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"cost {name!r} must be a non-negative number")


@dataclass(frozen=True)
class InsertAction:
    kind: str
    uri: str
    position: int
    target_index: int
    target_occurrence: int
    cost: float

    def describe(self) -> str:
        return (
            f"insert {self.uri!r} (target occurrence #{self.target_occurrence}) "
            f"at position {self.position}"
        )


@dataclass(frozen=True)
class DeleteAction:
    kind: str
    songid: int
    uri: str
    position: int
    occurrence: int
    cost: float

    def describe(self) -> str:
        return (
            f"delete {self.uri!r} occurrence #{self.occurrence} "
            f"(songid {self.songid}) at position {self.position}"
        )


@dataclass(frozen=True)
class MoveAction:
    kind: str
    songid: int
    uri: str
    occurrence: int
    from_position: int
    to_position: int
    result_position: int
    target_index: int
    target_occurrence: int
    cost: float

    def describe(self) -> str:
        return (
            f"move {self.uri!r} occurrence #{self.occurrence} "
            f"(songid {self.songid}) {self.from_position} -> "
            f"{self.to_position} (lands at {self.result_position})"
        )


Action = Union[InsertAction, DeleteAction, MoveAction]


@dataclass(frozen=True)
class Plan:
    """A fully simulated, costed sequence of actions for one generation."""

    generation: Optional[str]
    target: Tuple[str, ...]
    actions: Tuple[Action, ...]
    total_cost: float
    source_size: int
    target_size: int
    base_songids: Tuple[int, ...]
    kept: Tuple[Tuple[int, int], ...]
    strategy: str

    @property
    def insert_count(self) -> int:
        return sum(1 for action in self.actions if action.kind == "insert")

    @property
    def delete_count(self) -> int:
        return sum(1 for action in self.actions if action.kind == "delete")

    @property
    def move_count(self) -> int:
        return sum(1 for action in self.actions if action.kind == "move")


# ---------------------------------------------------------------------------
# Snapshot construction and validation
# ---------------------------------------------------------------------------


def take_snapshot(
    playlistinfo: Sequence[Dict[str, str]], generation: Optional[str] = None
) -> QueueSnapshot:
    """Build a snapshot from a raw ``playlistinfo()`` result.

    Raises ``SnapshotIncompatibleError`` on partial or inconsistent data so
    that no destructive plan can be derived from it.
    """
    entries: List[SnapshotEntry] = []
    seen_ids = set()
    occurrence_counter: Dict[str, int] = {}
    for raw in playlistinfo:
        if not isinstance(raw, dict) or "file" not in raw or "id" not in raw:
            raise SnapshotIncompatibleError(
                "snapshot entry lacks 'file' or 'id'; refusing to plan"
            )
        uri = raw["file"]
        try:
            songid = int(raw["id"])
        except (TypeError, ValueError) as exc:
            raise SnapshotIncompatibleError(
                f"snapshot entry {uri!r} has a non-numeric songid: {raw['id']!r}"
            ) from exc
        if not isinstance(uri, str) or not uri:
            raise SnapshotIncompatibleError(
                f"snapshot entry with songid {songid} has an empty uri"
            )
        if songid in seen_ids:
            raise SnapshotIncompatibleError(
                f"duplicate songid {songid} in snapshot; generation is ambiguous"
            )
        seen_ids.add(songid)
        occurrence = occurrence_counter.get(uri, 0)
        occurrence_counter[uri] = occurrence + 1
        entries.append(SnapshotEntry(songid, uri, occurrence))
    if generation is None:
        generation = _content_generation(entries)
    return QueueSnapshot(tuple(entries), generation)


def _content_generation(entries: Sequence[SnapshotEntry]) -> str:
    fingerprint = tuple((entry.songid, entry.uri) for entry in entries)
    return f"content:{hash(fingerprint):x}"


# ---------------------------------------------------------------------------
# Occurrence pairing and optimization
# ---------------------------------------------------------------------------


def _positions_by_uri(values: Sequence[str]) -> Dict[str, List[int]]:
    grouped: Dict[str, List[int]] = {}
    for index, value in enumerate(values):
        grouped.setdefault(value, []).append(index)
    return grouped


def _target_occurrences(targets: Sequence[str]) -> Dict[int, int]:
    counter: Dict[str, int] = {}
    result: Dict[int, int] = {}
    for index, uri in enumerate(targets):
        occurrence = counter.get(uri, 0)
        counter[uri] = occurrence + 1
        result[index] = occurrence
    return result


def _lcs_pairs(
    snapshot: QueueSnapshot, targets: Sequence[str]
) -> Tuple[Tuple[int, int], ...]:
    """Longest common subsequence as explicit (source, target) index pairs.

    Equal elements are matched in scan order, so the pairs are increasing in
    both dimensions and every paired occurrence keeps an auditable identity.
    These occurrences are already in the correct relative order and never
    need to move.
    """
    source = snapshot.uris
    n, m = len(source), len(targets)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            if source[i] == targets[j]:
                dp[i][j] = dp[i + 1][j + 1] + 1
            else:
                dp[i][j] = max(dp[i + 1][j], dp[i][j + 1])
    pairs: List[Tuple[int, int]] = []
    i, j = 0, 0
    while i < n and j < m:
        if source[i] == targets[j]:
            pairs.append((i, j))
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return tuple(pairs)


def _max_keep_pairs(
    snapshot: QueueSnapshot,
    targets: Sequence[str],
    base_pairs: Sequence[Tuple[int, int]],
) -> Tuple[Tuple[int, int], ...]:
    """Grow the LCS skeleton into the largest feasible occurrence matching.

    For every URI the number of retained occurrences is
    ``min(source count, target count)``: the LCS seeds an order-preserving
    matching and unused occurrences of the same URI are paired in ordinal
    order.  Pairing stays strictly within one URI, so each retained
    occurrence remains individually traceable.  The LCS pairs remain an
    increasing subsequence, hence the number of required moves is exactly
    ``len(result) - len(base_pairs)``.
    """
    source_used = {src for src, _ in base_pairs}
    target_used = {tgt for _, tgt in base_pairs}
    source_positions = _positions_by_uri(snapshot.uris)
    target_positions = _positions_by_uri(targets)
    pairs = set(base_pairs)
    for uri in set(source_positions) | set(target_positions):
        free_source = [
            index
            for index in source_positions.get(uri, ())
            if index not in source_used
        ]
        free_target = [
            index
            for index in target_positions.get(uri, ())
            if index not in target_used
        ]
        for src, tgt in zip(free_source, free_target):
            pairs.add((src, tgt))
    return tuple(sorted(pairs))


def _option_cost(
    kept: int,
    anchors: int,
    source_size: int,
    target_size: int,
    costs: CostTable,
) -> float:
    inserts = target_size - kept
    deletes = source_size - kept
    moves = kept - anchors
    if inserts and not costs.insert_enabled:
        return math.inf
    if deletes and not costs.delete_enabled:
        return math.inf
    if moves and not costs.move_enabled:
        return math.inf
    return (
        inserts * costs.insert
        + deletes * costs.delete
        + moves * costs.move
    )


def _choose_pairs(
    snapshot: QueueSnapshot, targets: Sequence[str], costs: CostTable
) -> Tuple[Tuple[Tuple[int, int], ...], frozenset[Tuple[int, int]], str]:
    """Return ``(pairs, immovable anchor pairs, strategy name)``.

    Two precisely characterized options compete:

    * ``lcs`` keeps only the longest common subsequence (zero moves);
    * ``maxkeep`` retains the maximum number of original occurrences, moving
      exactly the difference between the two.

    Both are evaluated under the supplied costs and action switches; ties
    favor ``maxkeep`` so original occurrences are preserved whenever the
    spend is identical.
    """
    n, m = len(snapshot.entries), len(targets)
    lcs = _lcs_pairs(snapshot, targets)
    max_keep = _max_keep_pairs(snapshot, targets, lcs)

    lcs_cost = _option_cost(len(lcs), len(lcs), n, m, costs)
    max_cost = _option_cost(len(max_keep), len(lcs), n, m, costs)

    if math.isinf(lcs_cost) and math.isinf(max_cost):
        raise QueuePlanError(
            "no feasible plan with the allowed action kinds and costs"
        )
    if max_cost <= lcs_cost:
        return max_keep, frozenset(lcs), "maxkeep"
    return lcs, frozenset(lcs), "lcs"


# ---------------------------------------------------------------------------
# MPD command semantics simulation
# ---------------------------------------------------------------------------


@dataclass
class _Token:
    """Live queue element during simulation; unique even for repeated URIs."""

    uri: str
    songid: Optional[int]
    occurrence: int
    target_index: Optional[int]
    target_occurrence: Optional[int]
    is_new: bool


def _apply_delete(tokens: List[_Token], songid: int) -> int:
    for index, token in enumerate(tokens):
        if token.songid == songid:
            del tokens[index]
            return index
    raise QueuePlanError(f"simulation lost track of songid {songid}")


def _apply_insert(tokens: List[_Token], token: _Token, position: int) -> int:
    # addid with a position inserts directly into the current queue.
    clamped = max(0, min(position, len(tokens)))
    tokens.insert(clamped, token)
    return clamped


def _move_argument(from_position: int, target_position: int) -> int:
    """Argument to send to moveid so the song lands at ``target_position``."""
    if target_position >= from_position:
        # MPD detaches first and decrements a rightward target by one.
        return target_position + 1
    return target_position


def _apply_move(
    tokens: List[_Token], songid: int, to_position: int
) -> Tuple[int, int]:
    """Exact MPD moveid semantics.

    The song is detached; when the requested position lies to the right of
    its current position MPD decrements it by one before inserting, i.e. the
    song is inserted before the element currently at ``to_position``.
    """
    from_position = next(
        (index for index, token in enumerate(tokens) if token.songid == songid),
        None,
    )
    if from_position is None:
        raise QueuePlanError(f"simulation lost track of songid {songid}")
    token = tokens.pop(from_position)
    insert_at = to_position - 1 if to_position > from_position else to_position
    clamped = max(0, min(insert_at, len(tokens)))
    tokens.insert(clamped, token)
    return from_position, clamped


# ---------------------------------------------------------------------------
# Plan construction
# ---------------------------------------------------------------------------


def build_plan(
    snapshot: QueueSnapshot,
    targets: Sequence[str],
    costs: Optional[CostTable] = None,
) -> Plan:
    """Compute the cheapest legal plan turning the snapshot into ``targets``.

    Targets are plain URIs; repeated URIs mean repeated queue occurrences.
    """
    if costs is None:
        costs = CostTable()
    target_tuple = tuple(targets)
    if any(not isinstance(uri, str) or not uri for uri in target_tuple):
        raise QueuePlanError("target uris must be non-empty strings")

    pairs, anchor_pairs, strategy = _choose_pairs(snapshot, target_tuple, costs)
    kept_src = {src for src, _ in pairs}
    kept_by_target = {tgt: src for src, tgt in pairs}
    tgt_occ = _target_occurrences(target_tuple)

    tokens = [
        _Token(
            uri=entry.uri,
            songid=entry.songid,
            occurrence=entry.occurrence,
            target_index=None,
            target_occurrence=None,
            is_new=False,
        )
        for entry in snapshot.entries
    ]
    for src, tgt in pairs:
        tokens[src].target_index = tgt
        tokens[src].target_occurrence = tgt_occ[tgt]

    actions: List[Action] = []

    # Phase 1: remove every source occurrence that is not kept.  Descending
    # snapshot positions keep bookkeeping simple; deleteid is id based so the
    # daemon does not care about command order.
    deleted_src = sorted(set(range(len(snapshot.entries))) - kept_src, reverse=True)
    for src in deleted_src:
        entry = snapshot.entries[src]
        live_position = _apply_delete(tokens, entry.songid)
        actions.append(
            DeleteAction(
                kind="delete",
                songid=entry.songid,
                uri=entry.uri,
                position=live_position,
                occurrence=entry.occurrence,
                cost=costs.delete,
            )
        )

    # Phase 2a: order the kept occurrences using one move per non-anchor.
    # Anchors (the LIS) keep their relative order forever and act as a
    # skeleton.  Non-anchors are inserted in ascending target-slot order
    # immediately before the first skeleton element destined for a later
    # slot; counting unprocessed elements physically in between gives the
    # exact live MPD position, so each occurrence moves at most once.
    anchor_songids = {
        snapshot.entries[src].songid for src, _ in anchor_pairs
    }
    move_pairs = sorted(
        ((src, tgt) for src, tgt in pairs if (src, tgt) not in anchor_pairs),
        key=lambda item: item[1],
    )
    placed_songids = set(anchor_songids)
    for src, tgt in move_pairs:
        entry = snapshot.entries[src]
        token = next(item for item in tokens if item.songid == entry.songid)
        # The gap already occupied by placed tokens: the token belongs after
        # every placed token with a smaller target slot and before the first
        # one with a larger slot.  When it already sits inside that gap no
        # command is needed.
        lower = 0
        upper = len(tokens)
        for index, candidate in enumerate(tokens):
            if candidate.songid not in placed_songids:
                continue
            if candidate.target_index is None:
                continue
            if candidate.target_index < tgt:
                lower = index + 1
            elif candidate.target_index > tgt:
                upper = index
                break
        current = tokens.index(token)
        if lower <= current < upper:
            placed_songids.add(entry.songid)
            continue
        # ``upper`` is a pre-removal index.  Moving rightward detaches the
        # token first, shifting later anchors down by one, so the desired
        # result position is one smaller (mirroring moveid semantics).
        destination = upper - 1 if upper > current else upper
        command_arg = _move_argument(current, destination)
        from_position, result_position = _apply_move(
            tokens, entry.songid, command_arg
        )
        if result_position != destination:
            raise QueuePlanError(
                "internal move invariant violated during planning"
            )
        placed_songids.add(entry.songid)
        actions.append(
            MoveAction(
                kind="move",
                songid=entry.songid,
                uri=entry.uri,
                occurrence=entry.occurrence,
                from_position=from_position,
                to_position=command_arg,
                result_position=result_position,
                target_index=tgt,
                target_occurrence=tgt_occ[tgt],
                cost=costs.move,
            )
        )

    # Phase 2b: the kept tokens now match the kept subsequence of the target.
    # Insert missing occurrences left to right at their literal target slot;
    # each insertion only shifts tokens destined for later slots.
    insert_counter: Dict[str, int] = {}
    for target_index, uri in enumerate(target_tuple):
        if target_index in kept_by_target:
            continue
        occurrence = insert_counter.get(uri, 0)
        insert_counter[uri] = occurrence + 1
        token = _Token(
            uri=uri,
            songid=None,
            occurrence=-1,
            target_index=target_index,
            target_occurrence=occurrence,
            is_new=True,
        )
        position = _apply_insert(tokens, token, target_index)
        actions.append(
            InsertAction(
                kind="insert",
                uri=uri,
                position=position,
                target_index=target_index,
                target_occurrence=occurrence,
                cost=costs.insert,
            )
        )

    final_uris = tuple(token.uri for token in tokens)
    if final_uris != target_tuple:
        raise QueuePlanError(
            f"internal simulation error: {final_uris!r} != {target_tuple!r}"
        )

    total_cost = sum(action.cost for action in actions)
    return Plan(
        generation=snapshot.generation,
        target=target_tuple,
        actions=tuple(actions),
        total_cost=total_cost,
        source_size=len(snapshot.entries),
        target_size=len(target_tuple),
        base_songids=snapshot.songids,
        kept=tuple(sorted(pairs)),
        strategy=strategy,
    )


def simulate_plan(
    snapshot: QueueSnapshot, plan: Plan
) -> List[Tuple[Optional[int], str, int]]:
    """Replay a plan against its snapshot and return the final queue.

    Every step is checked against the occurrence it claims to operate on;
    raises ``QueuePlanError`` on any discrepancy.  The returned tuples are
    ``(songid, uri, occurrence)`` with ``songid=None`` for inserted entries.
    """
    tokens = [
        _Token(
            uri=entry.uri,
            songid=entry.songid,
            occurrence=entry.occurrence,
            target_index=None,
            target_occurrence=None,
            is_new=False,
        )
        for entry in snapshot.entries
    ]
    for action in plan.actions:
        if action.kind == "insert":
            if action.position != _apply_insert(
                tokens,
                _Token(
                    uri=action.uri,
                    songid=None,
                    occurrence=-1,
                    target_index=action.target_index,
                    target_occurrence=action.target_occurrence,
                    is_new=True,
                ),
                action.position,
            ):
                raise QueuePlanError("insert position mismatch during audit")
        elif action.kind == "delete":
            token = next(
                (item for item in tokens if item.songid == action.songid), None
            )
            if token is None or token.uri != action.uri:
                raise QueuePlanError(
                    f"delete action does not identify the recorded occurrence "
                    f"(songid {action.songid})"
                )
            if token.occurrence != action.occurrence:
                raise QueuePlanError(
                    f"delete action targets occurrence {action.occurrence} but "
                    f"songid {action.songid} is occurrence {token.occurrence}"
                )
            live_position = _apply_delete(tokens, action.songid)
            if live_position != action.position:
                raise QueuePlanError("delete position mismatch during audit")
        else:
            token = next(
                (item for item in tokens if item.songid == action.songid), None
            )
            if token is None or token.uri != action.uri:
                raise QueuePlanError(
                    f"move action does not identify the recorded occurrence "
                    f"(songid {action.songid})"
                )
            if token.occurrence != action.occurrence:
                raise QueuePlanError(
                    f"move action targets occurrence {action.occurrence} but "
                    f"songid {action.songid} is occurrence {token.occurrence}"
                )
            from_position, result_position = _apply_move(
                tokens, action.songid, action.to_position
            )
            if (
                from_position != action.from_position
                or result_position != action.result_position
            ):
                raise QueuePlanError("move position mismatch during audit")
    if tuple(token.uri for token in tokens) != plan.target:
        raise QueuePlanError("audited plan does not reach the target order")
    if len(tokens) != plan.target_size:
        raise QueuePlanError("audited plan has the wrong final length")
    new_occurrence: Dict[str, int] = {}
    result = []
    for token in tokens:
        if token.is_new:
            occurrence = new_occurrence.get(token.uri, 0)
            new_occurrence[token.uri] = occurrence + 1
        else:
            occurrence = token.occurrence
        result.append((token.songid, token.uri, occurrence))
    return result


# ---------------------------------------------------------------------------
# Execution with generation revalidation
# ---------------------------------------------------------------------------


class QueuePlanner:
    """Build and execute plans against a real (or fake) MPD client.

    The client only needs the ordinary python-mpd2 queue commands:
    ``playlistinfo``, ``addid``, ``deleteid`` and ``moveid``.

    Before every pending action the live queue is re-fetched and compared
    against the simulated state.  If another client changed the queue, the
    remaining work is re-planned from the fresh snapshot; already confirmed
    actions are never sent again.  Partial or generation-incompatible
    snapshots abort before any destructive command.
    """

    def __init__(
        self,
        client: Any,
        costs: Optional[CostTable] = None,
        generation: Optional[str] = None,
    ) -> None:
        self.client = client
        self.costs = costs or CostTable()
        self.generation = generation
        self.confirmed: List[Action] = []

    def snapshot(self) -> QueueSnapshot:
        return take_snapshot(self.client.playlistinfo(), self.generation)

    def plan(self, targets: Sequence[str]) -> Plan:
        return build_plan(self.snapshot(), targets, self.costs)

    def execute(self, plan: Plan, targets: Optional[Sequence[str]] = None) -> Plan:
        target_tuple = tuple(plan.target if targets is None else targets)
        live = self.snapshot()
        if (
            live.songids != plan.base_songids
            or live.generation != plan.generation
        ):
            # Stale or generation-incompatible plan: never execute its
            # deletes blindly, re-plan against the live generation first.
            plan = build_plan(live, target_tuple, self.costs)
        tokens = _tokens_from_snapshot(live)
        pending = list(plan.actions)

        while pending:
            live = self.snapshot()
            if not _matches_snapshot(tokens, live):
                # Another client mutated the queue.  Re-check the un-sent
                # remainder against the fresh generation and re-plan; the
                # confirmed prefix stays untouched and is never replayed.
                plan = build_plan(live, target_tuple, self.costs)
                tokens = _tokens_from_snapshot(live)
                pending = list(plan.actions)
                continue

            action = pending[0]
            self._dispatch(action, tokens, live)
            self.confirmed.append(action)
            pending.pop(0)
        return plan

    def plan_and_execute(self, targets: Sequence[str]) -> Plan:
        return self.execute(self.plan(targets), targets)

    # -- internals --------------------------------------------------------

    def _dispatch(
        self, action: Action, tokens: List[_Token], live: QueueSnapshot
    ) -> None:
        if action.kind == "insert":
            new_id = self.client.addid(action.uri, action.position)
            try:
                songid: Optional[int] = int(new_id)
            except (TypeError, ValueError):
                songid = None
            token = _Token(
                uri=action.uri,
                songid=songid,
                occurrence=-1,
                target_index=action.target_index,
                target_occurrence=action.target_occurrence,
                is_new=True,
            )
            _apply_insert(tokens, token, action.position)
            if songid is None:
                # The daemon did not return a usable id: adopt the live
                # snapshot rather than guessing the new occurrence's identity.
                refreshed = self.snapshot()
                if tuple(entry.uri for entry in refreshed.entries) != tuple(
                    item.uri for item in tokens
                ):
                    raise SnapshotIncompatibleError(
                        "queue diverged after addid without a returned id"
                    )
                tokens[:] = _tokens_from_snapshot(refreshed)
        elif action.kind == "delete":
            # Destructive commands require this exact occurrence at the
            # simulated position in the live generation.
            try:
                live_position = live.position_of(action.songid)
            except QueuePlanError as exc:
                raise SnapshotIncompatibleError(
                    f"songid {action.songid} vanished before deleteid; "
                    "replan required"
                ) from exc
            if live_position != action.position:
                raise SnapshotIncompatibleError(
                    f"songid {action.songid} moved before deleteid; "
                    "replan required"
                )
            self.client.deleteid(action.songid)
            _apply_delete(tokens, action.songid)
        elif action.kind == "move":
            try:
                live_position = live.position_of(action.songid)
            except QueuePlanError as exc:
                raise SnapshotIncompatibleError(
                    f"songid {action.songid} vanished before moveid; "
                    "replan required"
                ) from exc
            if live_position != action.from_position:
                raise SnapshotIncompatibleError(
                    f"songid {action.songid} is not at its simulated position; "
                    "replan required"
                )
            self.client.moveid(action.songid, action.to_position)
            _apply_move(tokens, action.songid, action.to_position)
        else:  # pragma: no cover - guarded by the Action union
            raise QueuePlanError(f"unknown action kind {action.kind!r}")


def _tokens_from_snapshot(snapshot: QueueSnapshot) -> List[_Token]:
    return [
        _Token(
            uri=entry.uri,
            songid=entry.songid,
            occurrence=entry.occurrence,
            target_index=None,
            target_occurrence=None,
            is_new=False,
        )
        for entry in snapshot.entries
    ]


def _matches_snapshot(tokens: Sequence[_Token], snapshot: QueueSnapshot) -> bool:
    if len(tokens) != len(snapshot.entries):
        return False
    for token, entry in zip(tokens, snapshot.entries):
        if token.songid != entry.songid or token.uri != entry.uri:
            return False
    return True
