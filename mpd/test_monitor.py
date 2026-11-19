#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the generation-aware idle monitor in ``mpd.monitor``."""

import asyncio
import unittest
from typing import Any, Callable, Dict, List, Tuple
from unittest import mock

import mpd
import mpd.asyncio
from mpd.monitor import (
    FetchState,
    GapRecord,
    FetchRecord,
    IdleMonitor,
    MonitorEvent,
)
from mpd.tests import AsyncMockServer


class FakeMPDClient:
    """Minimal stand-in for ``mpd.asyncio.MPDClient``.

    ``idle`` is an async iterator fed through a queue; every other attribute
    is a command whose behavior is scripted via ``script``. Commands may be
    plain results, exceptions or async callables, which makes the exact point
    at which notifications arrive controllable.
    """

    def __init__(self) -> None:
        self._idle_queue: "asyncio.Queue[Any]" = asyncio.Queue()
        self.script: Dict[str, List[Any]] = {}
        self.calls: List[Tuple[str, Tuple[Any, ...]]] = []
        self.idle_calls: List[Tuple[str, ...]] = []

    def feed_idle(self, changes: List[str]) -> None:
        self._idle_queue.put_nowait(changes)

    def break_idle(self, error: BaseException) -> None:
        self._idle_queue.put_nowait(error)

    async def idle(self, subsystems: List[str]) -> Any:
        self.idle_calls.append(tuple(subsystems))
        while True:
            item = await self._idle_queue.get()
            if isinstance(item, BaseException):
                raise item
            yield item

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name.startswith("__"):
            raise AttributeError(name)

        async def command(*args: Any) -> Any:
            self.calls.append((name, args))
            if name not in self.script or not self.script[name]:
                raise AssertionError("unscripted command call: %s" % name)
            action = self.script[name].pop(0)
            if isinstance(action, BaseException):
                raise action
            if callable(action):
                return await action()
            return action

        return command


class TestIdleMonitor(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.client = FakeMPDClient()
        self.monitor = IdleMonitor(
            self.client, default_command="status", settle_delay=0.0
        )
        await self.monitor.start()

    async def asyncTearDown(self) -> None:
        await self.monitor.aclose()

    async def _settle(self, iterations: int = 5) -> None:
        for _ in range(iterations):
            await asyncio.sleep(0)

    async def _drain(self, monitor: IdleMonitor) -> None:
        # Wait until at least one fetch exists and every generation it could
        # open has been settled. A small wall-clock sleep is required so the
        # coalescing window and the retry timer can actually fire.
        for _ in range(200):
            if monitor.fetches() and not monitor.open_generations:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("monitor did not settle in time")

    # ------------------------------------------------------------------
    # phase boundaries / generation coverage
    # ------------------------------------------------------------------

    async def test_empty_idle_result_does_not_open_generation(self) -> None:
        # A noidle-cancelled idle wait resolves with an empty change list.
        self.client.feed_idle([])
        self.client.script["status"] = ["ignored"]
        await self._settle()

        self.assertEqual(self.monitor.events(), ())
        self.assertEqual(self.monitor.open_generations, ())
        self.assertEqual(self.monitor.fetches(), ())

    async def test_fetch_only_covers_snapshotted_generations(self) -> None:
        async def status_while_notification_arrives() -> Dict[str, str]:
            # The fetch is RUNNING; this notification belongs to a newer
            # generation and must not be closed by the in-flight fetch.
            self.client.feed_idle(["player"])
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return {"state": "play"}

        self.client.script["status"] = [
            status_while_notification_arrives,
            {"state": "play"},
        ]

        self.client.feed_idle(["playlist"])
        await self._settle()

        fetches = self.monitor.fetches()
        self.assertEqual([fetch.state for fetch in fetches], [FetchState.SETTLED] * 2)
        self.assertEqual(fetches[0].covers_generations, (1,))
        self.assertEqual(fetches[1].covers_generations, (2,))

        events = self.monitor.events()
        self.assertEqual(events[0].served_by, fetches[0].fetch_id)
        self.assertEqual(events[1].served_by, fetches[1].fetch_id)
        self.assertEqual(self.monitor.open_generations, ())

    async def test_fetch_phase_states_are_auditable(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def gated_status() -> str:
            entered.set()
            await release.wait()
            return "ok"

        self.client.script["status"] = [gated_status, "ok2"]
        self.client.feed_idle(["mixer"])
        await entered.wait()

        running = self.monitor.fetches()
        self.assertEqual(len(running), 1)
        self.assertEqual(running[0].state, FetchState.RUNNING)
        self.assertEqual(running[0].covers_generations, (1,))
        self.assertIsNotNone(running[0].started_at)
        self.assertIsNone(running[0].finished_at)

        # A notification arriving past the dispatch boundary opens a new
        # generation that the running fetch must not claim.
        self.client.feed_idle(["options"])
        await self._settle()
        self.assertEqual(running[0].covers_generations, (1,))

        release.set()
        await self._settle()
        self.assertEqual(running[0].state, FetchState.SETTLED)
        self.assertIsNotNone(running[0].finished_at)
        self.assertEqual(len(self.monitor.fetches()), 2)

    # ------------------------------------------------------------------
    # coalescing without starvation
    # ------------------------------------------------------------------

    async def test_consecutive_notifications_coalesce(self) -> None:
        client = FakeMPDClient()
        monitor = IdleMonitor(client, settle_delay=0.05)
        await monitor.start()
        try:
            client.script["status"] = [{"state": "play"}]
            client.feed_idle(["player"])
            client.feed_idle(["mixer"])
            client.feed_idle(["options"])
            await self._drain(monitor)

            fetches = monitor.fetches()
            self.assertEqual(len(fetches), 1)
            self.assertEqual(fetches[0].covers_generations, (1, 2, 3))
            self.assertEqual(monitor.open_generations, ())
            for event in monitor.events():
                self.assertEqual(event.served_by, fetches[0].fetch_id)
        finally:
            await monitor.aclose()

    async def test_notification_stream_cannot_starve_refresh(self) -> None:
        # Every fetch opens another generation before returning, emulating a
        # server that keeps changing while queries are in flight. The stream
        # stops after a fixed number of rounds; coalescing must never extend
        # its deadline, so the explicit refresh is served and every generation
        # still drains eventually.
        client = FakeMPDClient()
        monitor = IdleMonitor(client, settle_delay=0.0, retry_delay=0.01)
        await monitor.start()
        try:
            rounds = 6

            async def changing_status() -> Dict[str, str]:
                if changing_status.remaining > 0:
                    changing_status.remaining -= 1
                    client.feed_idle(["player"])
                    await asyncio.sleep(0)
                return {"state": "play"}

            changing_status.remaining = rounds  # type: ignore[attr-defined]
            client.script["status"] = [
                changing_status for _ in range(rounds + 2)
            ]

            result = await monitor.refresh()
            await self._drain(monitor)

            self.assertEqual(result, {"state": "play"})
            self.assertGreater(len(monitor.fetches()), 1)
            for event in monitor.events():
                self.assertIsNotNone(
                    event.served_by, "event %s never got covered" % event.seq
                )
        finally:
            await monitor.aclose()

    async def test_explicit_refresh_is_served_exactly_once(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def first_status() -> str:
            entered.set()
            await release.wait()
            return "first"

        self.client.script["status"] = [first_status, "second"]

        refresh_task = asyncio.ensure_future(self.monitor.refresh())
        await entered.wait()
        # Notifications piling up while the explicit query is in flight must
        # not swallow it; they get their own follow-up fetch.
        self.client.feed_idle(["player"])
        self.client.feed_idle(["mixer"])
        release.set()

        self.assertEqual(await refresh_task, "first")
        await self._settle()
        self.assertEqual(len(self.monitor.fetches()), 2)
        self.assertTrue(refresh_task.done())
        self.assertEqual(self.monitor.open_generations, ())

    async def test_differing_refresh_commands_are_not_merged(self) -> None:
        self.client.script["status"] = ["status-result"]
        self.client.script["currentsong"] = ["song-result"]

        results = await asyncio.gather(
            self.monitor.refresh("status"),
            self.monitor.refresh("currentsong"),
        )
        self.assertEqual(results, ["status-result", "song-result"])
        self.assertEqual(
            [name for name, _ in self.client.calls], ["status", "currentsong"]
        )

    async def test_refresh_arriving_mid_flight_gets_its_own_dispatch(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def first_status() -> str:
            entered.set()
            await release.wait()
            return "first"

        self.client.script["status"] = [first_status, "second"]
        first = asyncio.ensure_future(self.monitor.refresh())
        await entered.wait()
        second = asyncio.ensure_future(self.monitor.refresh())
        await self._settle()
        release.set()

        results = await asyncio.gather(first, second)
        self.assertEqual(results, ["first", "second"])
        self.assertEqual(len(self.monitor.fetches()), 2)
        # Neither caller was dropped; each fetch resolves exactly its own
        # boundary-frozen request set.
        self.assertEqual(
            [len(fetch.request_ids) for fetch in self.monitor.fetches()], [1, 1]
        )
        self.assertEqual(
            [name for name, _ in self.client.calls], ["status", "status"]
        )

    async def test_refresh_before_start_is_rejected(self) -> None:
        monitor = IdleMonitor(self.client)
        with self.assertRaises(mpd.ConnectionError):
            await monitor.refresh()

    async def test_refresh_requests_in_same_window_merge(self) -> None:
        client = FakeMPDClient()
        monitor = IdleMonitor(client, settle_delay=0.05)
        await monitor.start()
        try:
            client.script["status"] = ["shared"]
            results = await asyncio.gather(
                monitor.refresh(), monitor.refresh()
            )
            self.assertEqual(results, ["shared", "shared"])
            self.assertEqual(len(monitor.fetches()), 1)
            self.assertEqual(len(monitor.fetches()[0].request_ids), 2)
        finally:
            await monitor.aclose()

    async def test_aclose_fails_pending_refresh_and_stops(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def gated_status() -> str:
            entered.set()
            await release.wait()
            return "ok"

        self.client.script["status"] = [gated_status]
        pending = asyncio.ensure_future(self.monitor.refresh())
        await entered.wait()
        await self.monitor.aclose()
        release.set()

        with self.assertRaises(mpd.ConnectionError):
            await pending
        self.assertFalse(self.monitor.running)

    async def test_aclose_is_idempotent(self) -> None:
        await self.monitor.aclose()
        await self.monitor.aclose()
        self.assertFalse(self.monitor.running)

    async def test_starting_twice_is_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            await self.monitor.start()

    # ------------------------------------------------------------------
    # noidle / response framing semantics
    # ------------------------------------------------------------------

    async def test_tail_notification_is_not_next_command_reply(self) -> None:
        async def status_with_trailing_notification() -> Dict[str, str]:
            # The command's own result is returned while a further idle
            # notification is already queued; the monitor must keep the two
            # channels separate.
            self.client.feed_idle(["database"])
            await asyncio.sleep(0)
            return {"state": "pause"}

        self.client.script["status"] = [
            status_with_trailing_notification,
            {"state": "pause"},
        ]

        result = await self.monitor.refresh()
        self.assertEqual(result, {"state": "pause"})
        await self._settle()

        events = self.monitor.events()
        self.assertEqual([event.subsystems for event in events], [("database",)])
        self.assertIsNotNone(events[0].served_by)
        fetch = self.monitor.fetches()[0]
        self.assertEqual(fetch.state, FetchState.SETTLED)
        self.assertEqual(fetch.result, {"state": "pause"})

    # ------------------------------------------------------------------
    # failures, retries and gaps
    # ------------------------------------------------------------------

    async def test_command_failure_keeps_generations_open_and_retries(self) -> None:
        client = FakeMPDClient()
        monitor = IdleMonitor(client, settle_delay=0.0, retry_delay=0.01)
        await monitor.start()
        try:
            client.script["status"] = [
                mpd.CommandError("ACK [52@0] {status} boom"),
                {"state": "play"},
            ]
            client.feed_idle(["player"])
            await self._drain(monitor)

            fetches = monitor.fetches()
            self.assertEqual(
                [fetch.state for fetch in fetches],
                [FetchState.FAILED, FetchState.SETTLED],
            )
            self.assertEqual(fetches[0].covers_generations, (1,))
            self.assertEqual(fetches[1].covers_generations, (1,))
            self.assertEqual(monitor.events()[0].served_by, fetches[1].fetch_id)
        finally:
            await monitor.aclose()

    async def test_disconnect_records_explicit_gap(self) -> None:
        received: List[Any] = []
        subscription = self.monitor.subscribe(received.append)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def broken_status() -> None:
            entered.set()
            await release.wait()
            raise mpd.ConnectionError("reset by peer")

        self.client.script["status"] = [
            broken_status,
            {"state": "play"},
        ]

        self.client.feed_idle(["player"])
        await entered.wait()
        self.client.break_idle(mpd.ConnectionError("Connection lost"))
        release.set()
        await self._settle()

        self.assertTrue(self.monitor.gapped)
        gaps = self.monitor.gaps()
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].generations, (1,))
        self.assertFalse(gaps[0].closed)
        self.assertTrue(self.monitor.events()[0].gap_generation)
        self.assertIsInstance(self.monitor.coverage(1), GapRecord)
        self.assertTrue(
            any(isinstance(item, mpd.ConnectionError) for item in received)
        )

        with self.assertRaises(mpd.ConnectionError):
            await self.monitor.refresh()

        # Reconnect: the next successful fetch closes the gap and the stranded
        # event becomes attributable to that fetch.
        await self.monitor.reconnect()
        while not gaps[0].closed:
            await asyncio.sleep(0)

        self.assertFalse(self.monitor.gapped)
        self.assertIsInstance(self.monitor.coverage(1), FetchRecord)
        self.assertFalse(self.monitor.events()[0].gap_generation)
        self.monitor.unsubscribe(subscription)

    async def test_disconnect_during_fetch_marks_fetch_gapped(self) -> None:
        async def broken_status() -> None:
            raise mpd.ConnectionError("reset by peer")

        self.client.script["status"] = [broken_status]
        self.client.feed_idle(["player"])
        while not self.monitor.gapped:
            await asyncio.sleep(0)

        fetch = self.monitor.fetches()[0]
        self.assertEqual(fetch.state, FetchState.GAPPED)
        self.assertEqual(len(self.monitor.gaps()), 1)
        self.assertEqual(self.monitor.gaps()[0].generations, (1,))

    async def test_dual_observation_of_disconnect_makes_one_gap(self) -> None:
        async def broken_status() -> None:
            # Both the fetch and the idle iterator notice the same break.
            self.client.break_idle(mpd.ConnectionError("reset"))
            raise mpd.ConnectionError("reset by peer")

        self.client.script["status"] = [broken_status]
        self.client.feed_idle(["player"])
        while not self.monitor.gapped:
            await asyncio.sleep(0)

        self.assertEqual(len(self.monitor.gaps()), 1)

    async def test_auto_reconnect_closes_gap_via_callback(self) -> None:
        client = FakeMPDClient()
        reconnected = asyncio.Event()

        async def reconnect_cb() -> None:
            reconnected.set()

        monitor = IdleMonitor(
            client, settle_delay=0.0, retry_delay=0.01, reconnect=reconnect_cb
        )
        await monitor.start()
        try:
            client.script["status"] = [
                mpd.ConnectionError("lost"),
                {"state": "play"},
            ]
            client.feed_idle(["player"])
            await reconnected.wait()
            for _ in range(100):
                if monitor.gaps() and monitor.gaps()[0].closed:
                    break
                await asyncio.sleep(0.01)

            self.assertFalse(monitor.gapped)
            self.assertEqual(len(monitor.gaps()), 1)
            self.assertTrue(monitor.gaps()[0].closed)
            self.assertIsInstance(monitor.coverage(1), FetchRecord)
        finally:
            await monitor.aclose()

    # ------------------------------------------------------------------
    # subscription isolation
    # ------------------------------------------------------------------

    async def test_unsubscribing_one_keeps_others_covered(self) -> None:
        player_events: List[List[MonitorEvent]] = []
        playlist_events: List[List[MonitorEvent]] = []
        player_sub = self.monitor.subscribe(
            lambda batch: player_events.append(list(batch)), ["player"]
        )
        playlist_sub = self.monitor.subscribe(
            lambda batch: playlist_events.append(list(batch)), ["playlist"]
        )

        self.client.script["status"] = [{"state": "play"}, {"state": "play"}]
        self.client.feed_idle(["playlist"])
        await self._settle()
        self.monitor.unsubscribe(playlist_sub)

        self.client.feed_idle(["player"])
        await self._settle()

        self.assertEqual(len(playlist_events), 1)
        self.assertEqual(len(player_events), 1)
        self.assertEqual(
            player_events[0][0].subsystems, ("player",)
        )
        # The playlist generation is still fully auditable; dropping the
        # subscription did not drop the underlying change.
        playlist_event = self.monitor.events()[0]
        self.assertIsNotNone(playlist_event.served_by)
        self.assertFalse(playlist_sub.active)
        self.assertTrue(player_sub.active)

    async def test_catch_all_subscription_receives_everything(self) -> None:
        received: List[List[MonitorEvent]] = []
        subscription = self.monitor.subscribe(received.append)
        self.client.script["status"] = [{"state": "play"}]
        self.client.feed_idle(["player"])
        self.client.feed_idle(["database"])
        await self._settle()

        self.assertEqual(len(received), 1)
        self.assertEqual(
            [event.subsystems for event in received[0]],
            [("player",), ("database",)],
        )
        self.monitor.unsubscribe(subscription)

    # ------------------------------------------------------------------
    # replay / auditability
    # ------------------------------------------------------------------

    async def test_replay_attributes_every_event(self) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def gated_status() -> str:
            entered.set()
            await release.wait()
            return "gated"

        self.client.script["status"] = [
            {"state": "play"},
            gated_status,
            {"state": "play"},
        ]

        self.client.feed_idle(["player"])
        while not self.monitor.fetches():
            await asyncio.sleep(0)

        self.client.feed_idle(["mixer"])
        await entered.wait()
        # The second fetch is still RUNNING, so the newest event is pending;
        # every replay item must already be inspectable.
        self.assertIsNone(self.monitor.coverage(2))
        release.set()
        while self.monitor.fetches()[-1].state is not FetchState.SETTLED:
            await asyncio.sleep(0)

        self.client.feed_idle(["options"])
        while self.monitor.open_generations:
            await asyncio.sleep(0)

        timeline = self.monitor.replay()
        kinds = [entry.kind for entry in timeline]
        self.assertIn("event", kinds)
        self.assertIn("fetch", kinds)
        timestamps = [entry.at for entry in timeline]
        self.assertEqual(timestamps, sorted(timestamps))

        for event in self.monitor.events():
            coverage = self.monitor.coverage(event)
            self.assertIsInstance(coverage, FetchRecord)
            self.assertIn(event.generation, coverage.covers_generations)

    async def test_history_keeps_unresolved_events(self) -> None:
        client = FakeMPDClient()
        monitor = IdleMonitor(client, settle_delay=0.0, history_limit=2)
        await monitor.start()
        try:
            client.script["status"] = [
                {"state": "play"},
                {"state": "play"},
                mpd.ConnectionError("lost"),
            ]
            client.feed_idle(["player"])
            await self._settle()
            client.feed_idle(["mixer"])
            await self._settle()
            client.feed_idle(["options"])
            while not monitor.gapped:
                await asyncio.sleep(0)

            seqs = [event.seq for event in monitor.events()]
            self.assertIn(3, seqs)
            self.assertEqual(len(seqs), len(set(seqs)))
            gapped = [event for event in monitor.events() if event.gap_generation]
            self.assertTrue(gapped)
        finally:
            await monitor.aclose()


class NoidleIgnoringMockServer(AsyncMockServer):
    """Mock server that accepts the client's optional ``noidle`` writes.

    Whether the client restarts ``idle`` before the monitor's command fires
    depends on the client's internal coalescing timeout; either framing is
    valid, so ``noidle`` is accepted without an expectation or response.
    Writes after every scripted exchange was consumed (the client re-entering
    idle while the test is finishing) are ignored rather than raised on.
    """

    def write(self, data: bytes) -> None:
        if data == b"noidle\n":
            return
        if not self._expectations:
            return
        super().write(data)


class TestIdleMonitorWithRealClient(unittest.IsolatedAsyncioTestCase):
    """End-to-end coverage through mpd.asyncio's actual idle/noidle framing."""

    async def asyncSetUp(self) -> None:
        self.mockserver = NoidleIgnoringMockServer()
        asyncio.open_connection = mock.MagicMock(
            return_value=self.mockserver.get_streams()
        )
        self.mockserver.expect_exchange(
            [], [b"OK MPD mocker\n"]
        )
        self.client = mpd.asyncio.MPDClient()
        await self.client.connect("example.com", 10000)

    async def asyncTearDown(self) -> None:
        self.client.disconnect()

    async def test_notification_drives_audited_refresh(self) -> None:
        received: List[List[MonitorEvent]] = []
        monitor = IdleMonitor(self.client, settle_delay=0.0)
        subscription = monitor.subscribe(received.append)
        await monitor.start()
        try:
            self.mockserver.expect_exchange(
                [b"idle\n"],
                [b"idle: player\n", b"OK\n"],
            )
            self.mockserver.expect_exchange(
                [b"status\n"],
                [b"state: play\n", b"OK\n"],
            )
            self.mockserver.expect_exchange(
                [b"idle\n"],
                [b"idle: mixer\n", b"OK\n"],
            )
            self.mockserver.expect_exchange(
                [b"status\n"],
                [b"state: play\n", b"OK\n"],
            )

            for _ in range(200):
                if (
                    len(monitor.fetches()) >= 2
                    and all(
                        fetch.state is FetchState.SETTLED
                        for fetch in monitor.fetches()
                    )
                    and not monitor.open_generations
                    and self.mockserver._expectations == []
                ):
                    break
                await asyncio.sleep(0.01)

            self.assertEqual(len(monitor.fetches()), 2)
            self.assertTrue(
                all(fetch.state is FetchState.SETTLED for fetch in monitor.fetches())
            )
            self.assertEqual(monitor.open_generations, ())
            self.assertEqual(
                [event.subsystems for event in monitor.events()],
                [("player",), ("mixer",)],
            )
            for event in monitor.events():
                coverage = monitor.coverage(event)
                self.assertIsInstance(coverage, FetchRecord)
            self.assertEqual(len(received), 2)
            self.assertEqual(self.mockserver._expectations, [])
            monitor.unsubscribe(subscription)
        finally:
            await monitor.aclose()


if __name__ == "__main__":
    unittest.main()
