"""Cross-checks for the minimum-round guarantee.

An independent brute-force optimiser (no reuse of planner search internals)
enumerates every legal batch sequence on small random instances and compares
its round count with :class:`PipelinePlanner`. The greedy fallback and its
two triggers (size threshold, node budget) are covered separately.
"""

import random

from imaplib2.scheduling import (
    AUTH,
    SELECTED,
    IllegalStateError,
    PipelinePlanner,
    make_spec,
)

_INF = 10 ** 6
KINDS = ("FETCH", "SEARCH", "LIST", "STATUS", None)
KEYS = ("k1", "k2", "k3", "k4")


def independent_conflict(sa, sb):
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
        and sa.mailbox
        and sb.mailbox
        and sa.mailbox != sb.mailbox
    ):
        return True
    if sa.writes & (sb.reads | sb.writes) or sb.writes & (sa.reads | sa.writes):
        return True
    if sa.response_kinds & sb.response_kinds:
        return True
    return False


def transition(spec, state, mailbox):
    if spec.is_selection:
        return SELECTED, spec.mailbox
    if spec.name == "CLOSE":
        return AUTH, None
    if spec.name in ("LOGIN", "AUTHENTICATE"):
        return AUTH, mailbox
    return state, mailbox


def brute_optimum(specs, state, mailbox):
    """Independent minimum-round search over legal batch sequences."""

    by_id = {s.call_id: s for s in specs}
    all_ids = frozenset(by_id)

    def legal(cid, st, mb):
        spec = by_id[cid]
        if spec.state != st:
            return False
        return not (
            spec.state == SELECTED
            and spec.mailbox
            and spec.mailbox != mb
        )

    def rec(remaining, st, mb):
        if not remaining:
            return 0
        available = [cid for cid in remaining if legal(cid, st, mb)]
        if not available:
            return _INF

        best = _INF
        ordered = sorted(available)

        def batches(index, members):
            nonlocal best
            grew = False
            for i in range(index, len(ordered)):
                cid = ordered[i]
                spec = by_id[cid]
                closure = [
                    d for d in spec.deps
                    if d in remaining and d not in members
                ]
                block = members + closure + [cid]
                if any(d not in available for d in closure):
                    continue
                if any(
                    independent_conflict(spec, by_id[o]) or
                    any(independent_conflict(by_id[d], by_id[x])
                        for d in closure for x in block if x != d)
                    for o in block
                    if o != cid
                ):
                    continue
                grew = True
                batches(i + 1, block)

            if grew or not members:
                return
            if any(
                cid not in members
                and all(not independent_conflict(by_id[cid], by_id[m])
                        for m in members)
                and all(d in members or d not in remaining
                        for d in by_id[cid].deps)
                and legal(cid, st, mb)
                for cid in available
            ):
                return

            nst, nmb = st, mb
            for cid in members:
                nst, nmb = transition(by_id[cid], nst, nmb)
            sub = rec(remaining - frozenset(members), nst, nmb)
            best = min(best, 1 + sub)

        batches(0, [])
        return best

    return rec(all_ids, state, mailbox)


def planner_rounds(specs, state, mailbox=None, **kwargs):
    planner = PipelinePlanner(
        specs, initial_state=state, initial_mailbox=mailbox, **kwargs
    )
    rounds = 0
    try:
        while True:
            batch = planner.next_batch()
            if batch is None:
                return rounds, planner
            rounds += 1
            planner.account_responses(
                {pc.call_id: ("OK", b"") for pc in batch.commands}
            )
    except IllegalStateError:
        return _INF, planner


def random_specs(rng, n):
    specs = []
    for i in range(n):
        kind = rng.choice(KINDS)
        specs.append(make_spec(
            "c%d" % i, "NOOP", state=SELECTED, mailbox="INBOX",
            reads=rng.sample(KEYS, rng.randint(0, 2)),
            writes=rng.sample(KEYS, rng.randint(0, 1)),
            response_kinds=(kind,) if kind else (),
        ))
    for i in range(1, n):
        if rng.random() < 0.4:
            j = rng.randrange(i)
            base = specs[i]
            specs[i] = make_spec(
                "c%d" % i, "NOOP", state=SELECTED, mailbox="INBOX",
                reads=tuple(base.reads), writes=tuple(base.writes),
                response_kinds=tuple(base.response_kinds),
                deps=tuple(set(base.deps) | {"c%d" % j}),
            )
    return specs


def test_planner_matches_independent_brute_force():
    rng = random.Random(1234)
    for _ in range(150):
        n = rng.randint(1, 6)
        specs = random_specs(rng, n)
        got, planner = planner_rounds(specs, SELECTED, "INBOX")
        expected = brute_optimum(specs, SELECTED, "INBOX")
        assert got == expected
        assert planner.exact_used is True


def test_threshold_falls_back_to_greedy():
    rng = random.Random(7)
    specs = random_specs(rng, 5)
    rounds, planner = planner_rounds(
        specs, SELECTED, "INBOX", exact_threshold=0
    )
    assert planner.exact_used is False
    assert rounds >= 1


def test_node_budget_falls_back_and_stays_legal():
    specs = [
        make_spec(
            "c%d" % i, "FETCH", str(i), "(UID)",
            state=SELECTED, mailbox="INBOX", response_kinds=("FETCH",),
        )
        for i in range(16)
    ]
    rounds, planner = planner_rounds(
        specs, SELECTED, "INBOX",
        exact_threshold=16, exact_node_budget=100,
    )
    assert planner.exact_exhausted is True
    assert planner.exact_used is False
    # Every FETCH conflicts on the FETCH reply kind, so one round per call is
    # the legal result under both exact and greedy planning.
    assert rounds == 16


def test_plan_is_deterministic():
    rng = random.Random(99)
    specs = random_specs(rng, 7)

    def sequence():
        planner = PipelinePlanner(
            specs, initial_state=SELECTED, initial_mailbox="INBOX"
        )
        out = []
        while True:
            batch = planner.next_batch()
            if batch is None:
                return out
            out.append([pc.call_id for pc in batch.commands])
            planner.account_responses(
                {pc.call_id: ("OK", b"") for pc in batch.commands}
            )

    assert sequence() == sequence()


def test_every_planned_batch_is_conflict_free():
    rng = random.Random(55)
    specs = random_specs(rng, 8)
    planner = PipelinePlanner(
        specs, initial_state=SELECTED, initial_mailbox="INBOX"
    )
    by_id = {s.call_id: s for s in specs}
    while True:
        batch = planner.next_batch()
        if batch is None:
            break
        ids = [pc.call_id for pc in batch.commands]
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                assert not independent_conflict(by_id[a], by_id[b])
        planner.account_responses(
            {pc.call_id: ("OK", b"") for pc in batch.commands}
        )
