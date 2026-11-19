#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import annotations

import itertools
import random
import unittest
from typing import Any, Dict, List, Optional, Sequence, Tuple

from mpd.queueplan import (
    CostTable,
    DeleteAction,
    InsertAction,
    MoveAction,
    Plan,
    QueuePlanError,
    QueuePlanner,
    QueueSnapshot,
    SnapshotIncompatibleError,
    _lcs_pairs,
    build_plan,
    simulate_plan,
    take_snapshot,
)


def make_info(uris: Sequence[str], start: int = 1) -> List[Dict[str, str]]:
    return [
        {"file": uri, "id": str(songid), "pos": str(index)}
        for index, (songid, uri) in enumerate(
            zip(range(start, start + len(uris)), uris)
        )
    ]


def snapshot_of(uris: Sequence[str], start: int = 1) -> QueueSnapshot:
    return take_snapshot(make_info(uris, start))


class FakeMPD:
    """Minimal MPD stand-in implementing real queue command semantics."""

    def __init__(self, entries: Sequence[Dict[str, str]]) -> None:
        self.queue: List[Dict[str, str]] = [dict(entry) for entry in entries]
        self._next_id = max((int(entry["id"]) for entry in entries), default=0) + 1
        self.log: List[Tuple[Any, ...]] = []
        self.mutate_on_playlistinfo: Optional[int] = None
        self._playlistinfo_calls = 0

    def playlistinfo(self) -> List[Dict[str, str]]:
        self._playlistinfo_calls += 1
        if (
            self.mutate_on_playlistinfo is not None
            and self._playlistinfo_calls == self.mutate_on_playlistinfo
        ):
            new_id = self._next_id
            self._next_id += 1
            self.queue.append({"file": "external.ogg", "id": str(new_id)})
            self.mutate_on_playlistinfo = None
        return [dict(entry) for entry in self.queue]

    def addid(self, uri: str, position: Optional[int] = None) -> str:
        songid = self._next_id
        self._next_id += 1
        entry = {"file": uri, "id": str(songid)}
        self.log.append(("addid", uri, position, songid))
        if position is None:
            self.queue.append(entry)
        else:
            self.queue.insert(max(0, min(position, len(self.queue))), entry)
        return str(songid)

    def deleteid(self, songid: int) -> None:
        self.log.append(("deleteid", songid))
        for index, entry in enumerate(self.queue):
            if int(entry["id"]) == songid:
                del self.queue[index]
                return
        raise AssertionError(f"deleteid on missing songid {songid}")

    def moveid(self, songid: int, to_position: int) -> None:
        self.log.append(("moveid", songid, to_position))
        current = next(
            index
            for index, entry in enumerate(self.queue)
            if int(entry["id"]) == songid
        )
        entry = self.queue.pop(current)
        insert_at = to_position - 1 if to_position > current else to_position
        self.queue.insert(max(0, min(insert_at, len(self.queue))), entry)

    def uris(self) -> List[str]:
        return [entry["file"] for entry in self.queue]

    def ids(self) -> List[int]:
        return [int(entry["id"]) for entry in self.queue]


class SnapshotTests(unittest.TestCase):
    def test_occurrence_ordinals_and_generation(self) -> None:
        snap = snapshot_of(["a", "b", "a", "a", "b"])
        self.assertEqual(
            [(entry.uri, entry.occurrence) for entry in snap.entries],
            [("a", 0), ("b", 0), ("a", 1), ("a", 2), ("b", 1)],
        )
        self.assertTrue(snap.generation.startswith("content:"))
        self.assertEqual(snap.songids, (1, 2, 3, 4, 5))

    def test_rejects_partial_entries(self) -> None:
        with self.assertRaises(SnapshotIncompatibleError):
            take_snapshot([{"file": "a"}])
        with self.assertRaises(SnapshotIncompatibleError):
            take_snapshot([{"id": "1"}])

    def test_rejects_duplicate_or_bad_ids(self) -> None:
        with self.assertRaises(SnapshotIncompatibleError):
            take_snapshot([{"file": "a", "id": "1"}, {"file": "b", "id": "1"}])
        with self.assertRaises(SnapshotIncompatibleError):
            take_snapshot([{"file": "a", "id": "x"}])
        with self.assertRaises(SnapshotIncompatibleError):
            take_snapshot([{"file": "", "id": "1"}])


class PlanBuildingTests(unittest.TestCase):
    def test_identical_queue_is_free(self) -> None:
        snap = snapshot_of(["a", "b", "c"])
        plan = build_plan(snap, ["a", "b", "c"])
        self.assertEqual(plan.actions, ())
        self.assertEqual(plan.total_cost, 0.0)
        self.assertEqual(plan.kept, ((0, 0), (1, 1), (2, 2)))

    def test_insert_uses_real_position(self) -> None:
        snap = snapshot_of(["a", "c"])
        plan = build_plan(snap, ["a", "b", "c"])
        self.assertEqual(plan.insert_count, 1)
        action = plan.actions[0]
        self.assertIsInstance(action, InsertAction)
        self.assertEqual((action.uri, action.position), ("b", 1))
        self.assertEqual(simulate_plan(snap, plan)[1][1], "b")

    def test_delete_identifies_occurrence(self) -> None:
        snap = snapshot_of(["x", "x", "y"])
        plan = build_plan(snap, ["x", "y"])
        self.assertEqual(plan.delete_count, 1)
        action = plan.actions[0]
        self.assertIsInstance(action, DeleteAction)
        self.assertEqual((action.songid, action.uri, action.occurrence), (2, "x", 1))

    def test_leftward_move_arguments(self) -> None:
        snap = snapshot_of(["a", "b", "c", "d"])
        plan = build_plan(snap, ["a", "c", "d", "b"])
        moves = [action for action in plan.actions if isinstance(action, MoveAction)]
        # a, c, d form the increasing anchor; only b is relocated, once.
        self.assertEqual(
            [
                (m.songid, m.from_position, m.to_position, m.result_position)
                for m in moves
            ],
            [(2, 1, 4, 3)],
        )
        self.assertEqual(
            [row[1] for row in simulate_plan(snap, plan)], ["a", "c", "d", "b"]
        )

    def test_rightward_moveid_semantics_in_audit(self) -> None:
        # Hand-crafted plan exercising rightward moveid semantics directly:
        # moveid 1 2 on [a,b,c] must land a at position 1 -> [b,a,c].
        snap = snapshot_of(["a", "b", "c"])
        action = MoveAction(
            kind="move",
            songid=1,
            uri="a",
            occurrence=0,
            from_position=0,
            to_position=2,
            result_position=1,
            target_index=1,
            target_occurrence=0,
            cost=1.0,
        )
        plan = Plan(
            generation=snap.generation,
            target=("b", "a", "c"),
            actions=(action,),
            total_cost=1.0,
            source_size=3,
            target_size=3,
            base_songids=snap.songids,
            kept=((0, 1), (1, 0), (2, 2)),
            strategy="manual",
        )
        self.assertEqual([row[0] for row in simulate_plan(snap, plan)], [2, 1, 3])

    def test_duplicates_keep_original_identities(self) -> None:
        snap = snapshot_of(["x", "x", "y"])
        plan = build_plan(snap, ["y", "x", "x"])
        self.assertEqual(plan.delete_count, 0)
        final = simulate_plan(snap, plan)
        self.assertEqual([row[1] for row in final], ["y", "x", "x"])
        # The two retained x occurrences still carry their original songids.
        self.assertEqual([row[0] for row in final], [3, 1, 2])
        move = next(action for action in plan.actions if isinstance(action, MoveAction))
        self.assertEqual((move.songid, move.occurrence), (3, 0))

    def test_duplicates_surplus_on_both_sides(self) -> None:
        snap = snapshot_of(["x", "x", "x", "y"])
        plan = build_plan(snap, ["x", "x", "z", "x"])
        # All three x occurrences pair with the three target x slots;
        # y is deleted and z is the only insertion.
        self.assertEqual(plan.delete_count, 1)
        self.assertEqual(plan.insert_count, 1)
        deleted = next(
            action for action in plan.actions if isinstance(action, DeleteAction)
        )
        self.assertEqual((deleted.songid, deleted.uri), (4, "y"))
        final = simulate_plan(snap, plan)
        self.assertEqual([row[1] for row in final], ["x", "x", "z", "x"])
        retained = [row for row in final if row[0] in (1, 2, 3)]
        self.assertEqual(
            [(row[0], row[2]) for row in retained], [(1, 0), (2, 1), (3, 2)]
        )

    def test_no_cross_uri_pairing(self) -> None:
        snap = snapshot_of(["a", "b", "a"])
        plan = build_plan(snap, ["a", "a"])
        self.assertEqual(plan.kept, ((0, 0), (2, 1)))
        deleted = next(
            action for action in plan.actions if isinstance(action, DeleteAction)
        )
        self.assertEqual((deleted.songid, deleted.uri), (2, "b"))
        self.assertEqual([row[0] for row in simulate_plan(snap, plan)], [1, 3])

    def test_within_uri_crossing_keeps_identity_cheaply(self) -> None:
        # The second b can be moved to the front in a single moveid even
        # though it becomes target occurrence #0; identity stays attached to
        # its songid and no delete+insert is required.
        snap = snapshot_of(["c", "b", "c", "c", "b"])
        costs = CostTable(insert=3, delete=7, move=2)
        plan = build_plan(snap, ["b", "c", "b", "c", "c"], costs)
        self.assertEqual(plan.move_count, 1)
        self.assertEqual(plan.delete_count, 0)
        self.assertEqual(plan.insert_count, 0)
        self.assertEqual(plan.total_cost, 2.0)
        move = next(
            action for action in plan.actions if isinstance(action, MoveAction)
        )
        self.assertEqual(move.songid, 5)
        self.assertEqual(move.target_occurrence, 0)
        self.assertEqual(
            [row[1] for row in simulate_plan(snap, plan)],
            ["b", "c", "b", "c", "c"],
        )
        # Original identities survive, merely reordered.
        self.assertEqual(
            sorted(row[0] for row in simulate_plan(snap, plan)), [1, 2, 3, 4, 5]
        )

    def test_expensive_move_triggers_delete_and_insert(self) -> None:
        snap = snapshot_of(["a", "b"])
        plan = build_plan(snap, ["b", "a"], CostTable(insert=1, delete=1, move=10))
        # A single move would cost 10; keeping one occurrence and replacing
        # the other costs only delete + insert = 2.
        self.assertEqual(plan.move_count, 0)
        self.assertEqual(plan.insert_count, 1)
        self.assertEqual(plan.delete_count, 1)
        self.assertEqual(plan.total_cost, 2.0)
        self.assertEqual(len(plan.kept), 1)
        final = simulate_plan(snap, plan)
        self.assertEqual([row[1] for row in final], ["b", "a"])

    def test_disabled_action_kinds(self) -> None:
        snap = snapshot_of(["a", "b"])
        plan = build_plan(
            snap, ["b", "a"], CostTable(move=1, move_enabled=False)
        )
        self.assertEqual(plan.move_count, 0)
        with self.assertRaises(QueuePlanError):
            build_plan(snap, ["b"], CostTable(delete_enabled=False))
        with self.assertRaises(QueuePlanError):
            build_plan(snap, ["a", "b", "c"], CostTable(insert_enabled=False))

    def test_empty_source_and_target(self) -> None:
        plan = build_plan(snapshot_of([]), ["a", "b"])
        self.assertEqual(
            [(a.uri, a.position) for a in plan.actions if isinstance(a, InsertAction)],
            [("a", 0), ("b", 1)],
        )
        plan = build_plan(snapshot_of(["a", "b"]), [])
        self.assertEqual(plan.delete_count, 2)

    def test_cost_validation(self) -> None:
        with self.assertRaises(ValueError):
            CostTable(insert=-1)

    def test_invalid_targets(self) -> None:
        with self.assertRaises(QueuePlanError):
            build_plan(snapshot_of(["a"]), ["a", ""])


class OptimalityTests(unittest.TestCase):
    @staticmethod
    def _brute_force_optimum(
        source: Sequence[str], target: Sequence[str], costs: CostTable
    ) -> float:
        # Enumerate every feasible occurrence matching: order-preserving
        # within each URI, with arbitrary partial cardinalities.  This is the
        # true global optimum over all legal insert/delete/move plans.
        source_positions: Dict[str, List[int]] = {}
        target_positions: Dict[str, List[int]] = {}
        for index, uri in enumerate(source):
            source_positions.setdefault(uri, []).append(index)
        for index, uri in enumerate(target):
            target_positions.setdefault(uri, []).append(index)

        def lis(values: Sequence[int]) -> int:
            tails: List[int] = []
            for value in values:
                low, high = 0, len(tails)
                while low < high:
                    mid = (low + high) // 2
                    if tails[mid] < value:
                        low = mid + 1
                    else:
                        high = mid
                if low == len(tails):
                    tails.append(value)
                else:
                    tails[low] = value
            return len(tails)

        per_uri_options: List[List[Tuple[Tuple[int, int], ...]]] = []
        for uri in set(source_positions) | set(target_positions):
            sp = source_positions.get(uri, [])
            tp = target_positions.get(uri, [])
            choices: List[Tuple[Tuple[int, int], ...]] = [()]
            for size in range(1, min(len(sp), len(tp)) + 1):
                for ss in itertools.combinations(sp, size):
                    # All bijections onto the chosen target occurrences,
                    # including within-URI crossings: moveid can place any
                    # songid at any position, so swapping two equal-URI
                    # occurrences' target slots is a legal, auditable plan.
                    for tt in itertools.combinations(tp, size):
                        for perm in itertools.permutations(tt):
                            choices.append(tuple(zip(ss, perm)))
            per_uri_options.append(choices)

        best = float("inf")
        for combo in itertools.product(*per_uri_options):
            pairs = sorted(pair for group in combo for pair in group)
            kept = len(pairs)
            moves = kept - lis([tgt for _, tgt in pairs])
            inserts = len(target) - kept
            deletes = len(source) - kept
            if inserts and not costs.insert_enabled:
                continue
            if deletes and not costs.delete_enabled:
                continue
            if moves and not costs.move_enabled:
                continue
            best = min(
                best,
                inserts * costs.insert
                + deletes * costs.delete
                + moves * costs.move,
            )
        return best

    def test_random_instances_match_brute_force(self) -> None:
        rng = random.Random(42)
        cost_sets = [
            CostTable(),
            CostTable(insert=5, delete=1, move=2),
            CostTable(insert=1, delete=1, move=10),
            CostTable(insert=10, delete=10, move=1),
            CostTable(insert=0, delete=0, move=0),
        ]
        for _ in range(300):
            alphabet = rng.sample(["a", "b", "c"], k=rng.randint(1, 3))
            source = [rng.choice(alphabet) for _ in range(rng.randint(0, 6))]
            target = [rng.choice(alphabet) for _ in range(rng.randint(0, 6))]
            costs = rng.choice(cost_sets)
            snap = snapshot_of(source)
            plan = build_plan(snap, target, costs)
            self.assertAlmostEqual(
                plan.total_cost,
                self._brute_force_optimum(source, target, costs),
                msg=f"source={source} target={target}",
            )
            self.assertEqual(
                sum(action.cost for action in plan.actions), plan.total_cost
            )
            final_uris = [row[1] for row in simulate_plan(snap, plan)]
            self.assertEqual(final_uris, list(target))


class EndToEndFakeMPDTests(unittest.TestCase):
    def test_plans_replay_through_real_command_semantics(self) -> None:
        rng = random.Random(7)
        for _ in range(200):
            alphabet = ["a", "b", "c"]
            source = [rng.choice(alphabet) for _ in range(rng.randint(0, 8))]
            target = [rng.choice(alphabet) for _ in range(rng.randint(0, 8))]
            client = FakeMPD(make_info(source))
            planner = QueuePlanner(client)
            plan = planner.plan_and_execute(target)
            self.assertEqual(client.uris(), list(target))
            # Every retained occurrence still carries its original songid:
            # the queue was never cleared and rebuilt.  Source songids are
            # 1..len(source); plan.kept holds 0-based source indices.
            expected_retained = {src + 1 for src, _ in plan.kept}
            surviving_originals = {
                songid for songid in client.ids() if songid <= len(source)
            }
            self.assertEqual(expected_retained, surviving_originals)
            self.assertEqual(plan.target, tuple(target))

    def test_confirmed_actions_are_not_replayed_after_external_change(self) -> None:
        client = FakeMPD(make_info(["a", "b", "c", "d"]))
        planner = QueuePlanner(client)
        plan = planner.plan(["d", "c", "b", "a", "e"])
        # Another client appends a song while the first move is in flight.
        client.mutate_on_playlistinfo = 3
        final_plan = planner.execute(plan, ["d", "c", "b", "a", "e"])
        self.assertEqual(client.uris()[:5], ["d", "c", "b", "a", "e"])
        # The externally inserted song must be removed by the re-plan.
        self.assertNotIn("external.ogg", client.uris())
        # No command is ever issued twice against the same original songid.
        touched: Dict[int, List[str]] = {}
        for entry in client.log:
            if entry[0] in ("deleteid", "moveid"):
                touched.setdefault(entry[1], []).append(entry[0])
        for songid, commands in touched.items():
            self.assertEqual(
                len(commands), 1, f"songid {songid} touched repeatedly: {commands}"
            )
        self.assertTrue(len(planner.confirmed) > 0)
        self.assertEqual(final_plan.target, ("d", "c", "b", "a", "e"))

    def test_partial_snapshot_blocks_destructive_commands(self) -> None:
        client = FakeMPD(make_info(["a", "b", "c"]))
        planner = QueuePlanner(client)
        original_playlistinfo = client.playlistinfo

        def partial_playlistinfo() -> List[Dict[str, str]]:
            # The last entry lost its id: identity cannot be established, so
            # a destructive plan would be unsafe and must be refused.
            rows = original_playlistinfo()
            rows[-1] = {"file": rows[-1]["file"]}
            return rows

        client.playlistinfo = partial_playlistinfo  # type: ignore[assignment]
        with self.assertRaises(SnapshotIncompatibleError):
            planner.plan_and_execute(["a"])
        self.assertFalse(
            any(entry[0] == "deleteid" for entry in client.log),
            "no deleteid may be sent against a partial snapshot",
        )

    def test_generation_mismatch_blocks_stale_delete(self) -> None:
        # A full but generation-incompatible view (e.g. ids were recycled)
        # must never be used to issue deletes.
        snap = take_snapshot(make_info(["a", "b"]), generation="gen-1")
        stale = take_snapshot(make_info(["a", "b"]), generation="gen-2")
        self.assertNotEqual(snap.generation, stale.generation)
        client = FakeMPD(make_info(["a", "b"]))
        planner = QueuePlanner(client, generation="gen-2")
        plan = build_plan(snap, ["a"])
        # The stale plan carries gen-1; execution re-snapshots to gen-2 and
        # replans rather than dispatching the stale deleteid blindly.
        final = planner.execute(plan, ["a"])
        self.assertEqual(client.uris(), ["a"])
        self.assertEqual(final.generation, "gen-2")

    def test_stale_plan_is_replanned_before_dispatch(self) -> None:
        client = FakeMPD(make_info(["a", "b", "c"]))
        planner = QueuePlanner(client)
        plan = planner.plan(["c", "b", "a"])
        # External change before execution even starts.
        client.addid("intruder.ogg", 0)
        final_plan = planner.execute(plan, ["c", "b", "a"])
        self.assertEqual(client.uris(), ["c", "b", "a"])
        self.assertNotIn("intruder.ogg", client.uris())
        self.assertNotEqual(final_plan.generation, plan.generation)
        # The stale plan's deletes never reached the daemon.
        stale_deletes = {
            action.songid
            for action in plan.actions
            if isinstance(action, DeleteAction)
        }
        sent_deletes = {
            entry[1] for entry in client.log if entry[0] == "deleteid"
        }
        self.assertTrue(stale_deletes.isdisjoint(sent_deletes) or not stale_deletes)

    def test_audit_catches_wrong_occurrence_metadata(self) -> None:
        snap = snapshot_of(["a", "a"])
        good_insert = InsertAction(
            kind="insert",
            uri="a",
            position=1,
            target_index=1,
            target_occurrence=1,
            cost=1.0,
        )
        # Wrong occurrence ordinal recorded for the first a: audit must fail.
        bad_occurrence = DeleteAction(
            kind="delete",
            songid=1,
            uri="a",
            position=0,
            occurrence=5,
            cost=1.0,
        )
        plan_bad_occurrence = Plan(
            generation=snap.generation,
            target=("a", "a"),
            actions=(bad_occurrence, good_insert),
            total_cost=2.0,
            source_size=2,
            target_size=2,
            base_songids=snap.songids,
            kept=((0, 0),),
            strategy="manual",
        )
        with self.assertRaises(QueuePlanError):
            simulate_plan(snap, plan_bad_occurrence)

        # A wrong uri attached to an existing songid must hard-fail too.
        wrong_uri = DeleteAction(
            kind="delete",
            songid=1,
            uri="b",
            position=0,
            occurrence=0,
            cost=1.0,
        )
        plan_wrong_uri = Plan(
            generation=snap.generation,
            target=("a",),
            actions=(wrong_uri,),
            total_cost=1.0,
            source_size=2,
            target_size=1,
            base_songids=snap.songids,
            kept=((1, 0),),
            strategy="manual",
        )
        with self.assertRaises(QueuePlanError):
            simulate_plan(snap, plan_wrong_uri)

        good_delete = DeleteAction(
            kind="delete",
            songid=1,
            uri="a",
            position=0,
            occurrence=0,
            cost=1.0,
        )
        plan_good = Plan(
            generation=snap.generation,
            target=("a", "a"),
            actions=(good_delete, good_insert),
            total_cost=2.0,
            source_size=2,
            target_size=2,
            base_songids=snap.songids,
            kept=((0, 0),),
            strategy="manual",
        )
        final = simulate_plan(snap, plan_good)
        self.assertEqual([row[1] for row in final], ["a", "a"])


if __name__ == "__main__":
    unittest.main()
