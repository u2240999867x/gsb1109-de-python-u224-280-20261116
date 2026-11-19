"""Background monitoring of MPD changes with auditable refresh coverage.

The asyncio :class:`~mpd.asyncio.MPDClient` keeps the protocol stream correctly
framed (an ``idle``/``noidle`` exchange is always fully consumed before the
next command's response is read), but it does not keep track of *which*
refresh actually observed *which* notification.  This module adds that layer.

Design vocabulary
-----------------

notification
    One ``idle`` result delivered by the server, carrying the subsystems that
    changed.  Every notification is recorded as a :class:`MonitorEvent` and
    opens exactly one invalidation *generation*.

generation
    A monotonically numbered unit of invalidation.  A generation is *open*
    until a refresh whose dispatch boundary is later than the notification
    settles successfully, or until a disconnect turns it into an explicit
    *gap*.

phase boundary
    A refresh moves through the states of :class:`FetchState`:

    PENDING -> RUNNING -> SETTLED
                          \\-> FAILED (generations stay open)
                          \\-> GAPPED (disconnect, a gap is recorded)

    The set of generations a refresh is allowed to close is snapshotted in the
    same synchronous step that moves the refresh into RUNNING, i.e. right
    before the command is sent.  Notifications arriving while the response is
    being read create newer generations that this refresh must not touch.

coalescing
    Notifications (and explicit refresh requests for the same command) that
    arrive within ``settle_delay`` of the first one are merged into a single
    refresh.  The coalescing deadline is computed once and never extended, so
    a steady stream of notifications cannot starve a pending query, and every
    explicit :meth:`IdleMonitor.refresh` call is settled exactly once.

gap
    A disconnect while generations are open produces a :class:`GapRecord`
    naming the generations and events that no in-flight refresh can ever cover.
    After :meth:`IdleMonitor.reconnect` the first successful refresh closes
    the gap and serves the stranded events.

Every event in :meth:`IdleMonitor.replay` can therefore be attributed to the
fetch that covered it, to a gap, or it is still pending.
"""

from __future__ import annotations

import asyncio
import builtins
import logging
from dataclasses import dataclass
from enum import Enum
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from mpd.base import ConnectionError

logger = logging.getLogger(__name__)


def _is_connection_error(error: BaseException) -> bool:
    # Transport-level failures surface as the standard library's
    # ConnectionError alongside mpd's own subclass.
    return isinstance(error, (ConnectionError, builtins.ConnectionError))


class FetchState(Enum):
    #: Coalescing window; the command has not been sent yet.
    PENDING = "pending"
    #: Command sent, response being read. Coverage snapshot is fixed.
    RUNNING = "running"
    #: Response applied; only the snapshotted generations were closed.
    SETTLED = "settled"
    #: Command failed (e.g. MPD ACK). No generation was closed; a retry follows.
    FAILED = "failed"
    #: Connection lost while the refresh was in flight; a gap was recorded.
    GAPPED = "gapped"


@dataclass
class MonitorEvent:
    """A single idle notification and its coverage status."""

    #: Notification sequence number, starting at 1.
    seq: int
    #: Subsystems reported by the server.
    subsystems: Tuple[str, ...]
    #: Invalidation generation opened by this notification.
    generation: int
    #: Event loop time at which the notification was received.
    occurred_at: float
    #: Fetch id whose response covers this event, once settled.
    served_by: Optional[int] = None
    #: True while the event's generation is part of an unresolved gap.
    gap_generation: bool = False

    @property
    def covered(self) -> bool:
        return self.served_by is not None


@dataclass
class FetchRecord:
    """Audit record of one refresh round-trip."""

    fetch_id: int
    command: str
    args: Tuple[Any, ...]
    created_at: float
    #: Generations that were open when this fetch crossed the dispatch
    #: boundary.  A SETTLED fetch closes exactly these and no others.
    covers_generations: Tuple[int, ...] = ()
    #: Explicit refresh request ids merged into this fetch.
    request_ids: Tuple[int, ...] = ()
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    state: FetchState = FetchState.PENDING
    result: Any = None
    error: Optional[BaseException] = None


@dataclass
class GapRecord:
    """A disconnect left these generations without coverage."""

    gap_id: int
    #: Generations that were open when the connection was lost.
    generations: Tuple[int, ...]
    #: Sequence number of the first stranded event.
    first_event_seq: int
    detected_at: float
    error: BaseException
    #: Fetch id of the first successful refresh after reconnect.
    closed_by: Optional[int] = None

    @property
    def closed(self) -> bool:
        return self.closed_by is not None


@dataclass
class TimelineEntry:
    """One item of :meth:`IdleMonitor.replay`."""

    at: float
    kind: str
    ref: Any


@dataclass
class Subscription:
    """A subscriber handle returned by :meth:`IdleMonitor.subscribe`."""

    id: int
    #: Subsystems of interest; an empty tuple means everything.
    subsystems: Tuple[str, ...]
    callback: Callable[[Union[Sequence[MonitorEvent], BaseException]], None]
    active: bool = True
    last_delivered_seq: int = 0


#: Signature of the optional reconnect hook.  It must restore the underlying
#: client to a connected state before returning.
ReconnectCallback = Callable[[], Awaitable[Any]]


class IdleMonitor:
    """Drive background refreshes from idle notifications.

    The monitor consumes the client's long-lived ``idle()`` async iterator and
    schedules refreshes against the same client.  It never sends protocol
    commands itself, so all ``noidle``/response framing remains the client's
    responsibility; the monitor only adds generation bookkeeping around it.

    :param client: an :class:`~mpd.asyncio.MPDClient`-compatible object.  Any
        attribute that is not ``idle`` is treated as an MPD command coroutine.
    :param default_command: command used for background refreshes that are not
        tied to an explicit :meth:`refresh` request.
    :param default_args: arguments for the default refresh command.
    :param settle_delay: coalescing window in seconds.  The deadline is taken
        once when work first becomes pending and is never extended.
    :param retry_delay: delay before retrying after a non-connection failure.
    :param idle_subsystems: subsystems passed to the client's ``idle()``.
    :param reconnect: optional coroutine function called to re-establish the
        underlying connection after a disconnect.
    :param history_limit: if given, retain at most this many fully covered
        :class:`MonitorEvent` instances for auditing; pending or gapped events
        are always retained until resolved. Fetch and gap records are kept
        regardless so the audit trail stays attributable.
    """

    def __init__(
        self,
        client: Any,
        default_command: str = "status",
        default_args: Sequence[Any] = (),
        settle_delay: float = 0.0,
        retry_delay: float = 0.5,
        idle_subsystems: Sequence[str] = (),
        reconnect: Optional[ReconnectCallback] = None,
        history_limit: Optional[int] = None,
    ) -> None:
        self._client = client
        self._default_command = default_command
        self._default_args = tuple(default_args)
        self.settle_delay = settle_delay
        self.retry_delay = retry_delay
        self._idle_subsystems = tuple(idle_subsystems)
        self._reconnect_cb = reconnect
        self._history_limit = history_limit

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False
        self._wake: Optional[asyncio.Event] = None
        self._idle_task: Optional[asyncio.Task[None]] = None
        self._flush_task: Optional[asyncio.Task[None]] = None
        self._reconnect_task: Optional[asyncio.Task[None]] = None

        self._next_seq = 0
        self._next_generation = 0
        self._next_fetch_id = 0
        self._next_gap_id = 0
        self._next_request_id = 0
        self._next_subscription_id = 0

        self._events: List[MonitorEvent] = []
        self._events_by_generation: Dict[int, List[MonitorEvent]] = {}
        self._fetches: List[FetchRecord] = []
        self._gaps: List[GapRecord] = []
        self._subscriptions: List[Subscription] = []

        self._open_generations: set[int] = set()
        self._fetching = False
        self._gapped = False

        # request id -> (future, command, args), insertion order is FIFO
        self._pending_requests: Dict[
            int, Tuple[asyncio.Future[Any], str, Tuple[Any, ...]]
        ] = {}

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the idle consumer and the refresh scheduler."""
        if self._running:
            raise RuntimeError("monitor already started")
        self._loop = asyncio.get_event_loop()
        self._wake = asyncio.Event()
        self._running = True
        self._idle_task = asyncio.ensure_future(self._idle_loop())
        self._flush_task = asyncio.ensure_future(self._flush_loop())

    async def aclose(self) -> None:
        """Stop all background tasks and tear down subscriptions."""
        self._running = False
        for task in (self._idle_task, self._flush_task, self._reconnect_task):
            if task is not None:
                task.cancel()
        for task in (self._idle_task, self._flush_task, self._reconnect_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._idle_task = self._flush_task = self._reconnect_task = None
        error = ConnectionError("monitor closed")
        for request_id, (future, _, _) in list(self._pending_requests.items()):
            if not future.done():
                future.set_exception(error)
        self._pending_requests.clear()
        self._subscriptions.clear()

    @property
    def running(self) -> bool:
        return self._running

    @property
    def gapped(self) -> bool:
        """True between a disconnect and a successful :meth:`reconnect`."""
        return self._gapped

    @property
    def open_generations(self) -> Tuple[int, ...]:
        return tuple(sorted(self._open_generations))

    # ------------------------------------------------------------------
    # subscriptions
    # ------------------------------------------------------------------

    def subscribe(
        self,
        callback: Callable[[Union[Sequence[MonitorEvent], BaseException]], None],
        subsystems: Sequence[str] = (),
    ) -> Subscription:
        """Register ``callback`` for events matching ``subsystems``.

        An empty subsystem sequence subscribes to every notification.  The
        callback is invoked with batches of :class:`MonitorEvent` once a fetch
        covers them, or with an exception when a disconnect is detected.
        Removing a subscription never affects the generations or fetches that
        other subscriptions still need.
        """
        subscription = Subscription(
            id=self._next_subscription_id,
            subsystems=tuple(subsystems),
            callback=callback,
        )
        self._next_subscription_id += 1
        self._subscriptions.append(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        if subscription in self._subscriptions:
            subscription.active = False
            self._subscriptions.remove(subscription)

    # ------------------------------------------------------------------
    # explicit refreshes
    # ------------------------------------------------------------------

    async def refresh(
        self,
        command: Optional[str] = None,
        args: Sequence[Any] = (),
    ) -> Any:
        """Ensure a refresh round-trip and return its result.

        Requests for the same command/args are merged with a refresh that has
        not crossed its dispatch boundary yet.  Requests arriving while a
        fetch is already running are served by the following, guaranteed
        dispatch: coalescing may merge notifications into them but can never
        drop them.
        """
        if not self._running:
            raise ConnectionError("monitor is not started")
        if self._gapped:
            raise ConnectionError("connection is gapped; reconnect first")
        assert self._loop is not None and self._wake is not None

        request_id = self._next_request_id
        self._next_request_id += 1
        future: asyncio.Future[Any] = self._loop.create_future()
        key = (
            command if command is not None else self._default_command,
            tuple(args) if args else self._default_args,
        )
        self._pending_requests[request_id] = (future, key[0], key[1])
        self._wake.set()
        return await future

    # ------------------------------------------------------------------
    # audit trail
    # ------------------------------------------------------------------

    def events(self) -> Tuple[MonitorEvent, ...]:
        return tuple(self._events)

    def fetches(self) -> Tuple[FetchRecord, ...]:
        return tuple(self._fetches)

    def gaps(self) -> Tuple[GapRecord, ...]:
        return tuple(self._gaps)

    def coverage(
        self, event: Union[int, MonitorEvent]
    ) -> Union[FetchRecord, GapRecord, None]:
        """Return what currently covers ``event``.

        Result is the covering :class:`FetchRecord`, a :class:`GapRecord` for
        a stranded event, or ``None`` while the event is still pending.
        """
        if isinstance(event, int):
            matching = [
                candidate for candidate in self._events if candidate.seq == event
            ]
            if not matching:
                raise KeyError(event)
            event = matching[0]
        if event.served_by is not None:
            for fetch in self._fetches:
                if fetch.fetch_id == event.served_by:
                    return fetch
        for gap in self._gaps:
            if event.generation in gap.generations:
                return gap
        return None

    def replay(self) -> Tuple[TimelineEntry, ...]:
        """Chronological audit trail of notifications, fetches and gaps."""
        entries: List[TimelineEntry] = []
        entries.extend(
            TimelineEntry(event.occurred_at, "event", event) for event in self._events
        )
        entries.extend(
            TimelineEntry(fetch.created_at, "fetch", fetch) for fetch in self._fetches
        )
        entries.extend(
            TimelineEntry(gap.detected_at, "gap", gap) for gap in self._gaps
        )
        return tuple(sorted(entries, key=lambda entry: entry.at))

    # ------------------------------------------------------------------
    # reconnect / gaps
    # ------------------------------------------------------------------

    async def reconnect(self) -> None:
        """Re-establish the underlying connection and resume monitoring.

        If a ``reconnect`` callback was configured it is invoked first.  The
        first refresh settling afterwards closes every outstanding gap.
        """
        if not self._running:
            raise ConnectionError("monitor is not started")
        auto = self._reconnect_task
        if auto is not None and not auto.done() and auto is not asyncio.current_task():
            # A concurrent automatic reconnect must not race a manual one and
            # end up with two consumers of the idle stream.
            auto.cancel()
            try:
                await auto
            except (asyncio.CancelledError, Exception):
                pass
        self._reconnect_task = None
        if self._reconnect_cb is not None:
            await self._reconnect_cb()
        stale = self._idle_task
        if stale is not None and not stale.done():
            stale.cancel()
            try:
                await stale
            except (asyncio.CancelledError, Exception):
                pass
        self._gapped = False
        self._idle_task = asyncio.ensure_future(self._idle_loop())
        assert self._wake is not None
        self._wake.set()

    # ------------------------------------------------------------------
    # background loops
    # ------------------------------------------------------------------

    async def _idle_loop(self) -> None:
        task = asyncio.current_task()
        try:
            async for changes in self._client.idle(self._idle_subsystems):
                # An empty idle result is the server's reply to a noidle that
                # cancelled a wait without any actual change, not a
                # notification; it must not open an invalidation generation.
                if changes:
                    self._record_notification(changes)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            # A stale loop superseded by reconnect() must not open a second gap.
            if self._running and self._idle_task is task and not self._gapped:
                self._handle_disconnect(error)

    async def _flush_loop(self) -> None:
        assert self._loop is not None and self._wake is not None
        while self._running:
            await self._wake.wait()
            self._wake.clear()
            if not self._needs_flush() or self._gapped:
                continue

            # Coalescing window with an absolute deadline that is never
            # extended: a burst can merge, but the flush is guaranteed.
            deadline = self._loop.time() + self.settle_delay
            while self._running and not self._gapped:
                remaining = deadline - self._loop.time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                self._wake.clear()

            if self._gapped or not self._needs_flush():
                continue
            if self._fetching:
                # The running fetch's completion wakes the loop again; any
                # generation it does not cover is handled then.
                continue
            await self._dispatch_fetch()

    def _needs_flush(self) -> bool:
        if self._open_generations or self._pending_requests:
            return True
        # A reconnect must produce a fetch that closes any outstanding gap.
        return any(not gap.closed for gap in self._gaps)

    # ------------------------------------------------------------------
    # notification intake
    # ------------------------------------------------------------------

    def _record_notification(self, changes: Sequence[str]) -> None:
        assert self._loop is not None
        self._next_seq += 1
        self._next_generation += 1
        event = MonitorEvent(
            seq=self._next_seq,
            subsystems=tuple(changes),
            generation=self._next_generation,
            occurred_at=self._loop.time(),
        )
        self._events.append(event)
        self._events_by_generation.setdefault(event.generation, []).append(event)
        self._open_generations.add(event.generation)
        self._trim_history()
        if self._wake is not None:
            self._wake.set()

    def _trim_history(self) -> None:
        if self._history_limit is None:
            return
        # Only fully covered events may age out; pending and gapped events
        # must remain auditable until they are resolved.
        while len(self._events) > self._history_limit:
            if not self._events[0].covered:
                return
            event = self._events.pop(0)
            peers = self._events_by_generation.get(event.generation)
            if peers is not None:
                peers = [peer for peer in peers if peer is not event]
                if peers:
                    self._events_by_generation[event.generation] = peers
                else:
                    self._events_by_generation.pop(event.generation, None)

    # ------------------------------------------------------------------
    # fetch execution and phase boundaries
    # ------------------------------------------------------------------

    async def _dispatch_fetch(self) -> None:
        assert self._loop is not None
        self._next_fetch_id += 1
        fetch_id = self._next_fetch_id

        request_ids: List[int] = []
        if self._pending_requests:
            first_id = next(iter(self._pending_requests))
            _, command, args = self._pending_requests[first_id]
            for request_id, (_, candidate_command, candidate_args) in list(
                self._pending_requests.items()
            ):
                if candidate_command == command and candidate_args == args:
                    request_ids.append(request_id)
        else:
            command = self._default_command
            args = self._default_args

        # Phase boundary: everything below up to the first await runs without
        # interruption, so the snapshot and the RUNNING state agree.
        record = FetchRecord(
            fetch_id=fetch_id,
            command=command,
            args=args,
            created_at=self._loop.time(),
            covers_generations=tuple(sorted(self._open_generations)),
            request_ids=tuple(request_ids),
        )
        self._fetches.append(record)
        self._fetching = True
        record.started_at = self._loop.time()
        record.state = FetchState.RUNNING

        try:
            result = await getattr(self._client, command)(*args)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            self._fetching = False
            record.finished_at = self._loop.time()
            if _is_connection_error(error):
                record.state = FetchState.GAPPED
                record.error = error
                self._fail_requests(request_ids, error)
                self._handle_disconnect(error)
            else:
                record.state = FetchState.FAILED
                record.error = error
                self._fail_requests(request_ids, error)
                self._schedule_retry()
            return

        # Settlement is synchronous: no notification can interleave here.
        record.finished_at = self._loop.time()
        record.state = FetchState.SETTLED
        record.result = result
        self._fetching = False

        self._apply_coverage(record)
        self._resolve_requests(request_ids, result)
        self._deliver_events(record)

        if self._wake is not None:
            self._wake.set()

    def _apply_coverage(self, record: FetchRecord) -> None:
        for generation in record.covers_generations:
            self._open_generations.discard(generation)
            for event in self._events_by_generation.get(generation, ()):
                event.served_by = record.fetch_id
                event.gap_generation = False
        # The first successful fetch after a reconnect closes all gaps.
        for gap in self._gaps:
            if gap.closed_by is None:
                gap.closed_by = record.fetch_id
                for generation in gap.generations:
                    for event in self._events_by_generation.get(generation, ()):
                        event.served_by = record.fetch_id
                        event.gap_generation = False

    def _resolve_requests(
        self, request_ids: Sequence[int], result: Any
    ) -> None:
        for request_id in request_ids:
            future, _, _ = self._pending_requests.pop(request_id)
            if not future.done():
                future.set_result(result)

    def _fail_requests(
        self, request_ids: Sequence[int], error: BaseException
    ) -> None:
        for request_id in request_ids:
            future, _, _ = self._pending_requests.pop(request_id)
            if not future.done():
                future.set_exception(error)

    def _schedule_retry(self) -> None:
        # Generations stayed open; the retry is driven by a timed wake-up.
        async def retry() -> None:
            await asyncio.sleep(self.retry_delay)
            if self._running and self._wake is not None:
                self._wake.set()

        if self._running:
            asyncio.ensure_future(retry())

    # ------------------------------------------------------------------
    # disconnect and gap handling
    # ------------------------------------------------------------------

    def _handle_disconnect(self, error: BaseException) -> None:
        assert self._loop is not None
        if self._gapped:
            # The fetch path and the idle path can observe the same break;
            # only the first observation may record a gap.
            return
        stranded = tuple(sorted(self._open_generations))
        if stranded:
            self._next_gap_id += 1
            first_event_seq = min(
                event.seq
                for event in self._events
                if event.generation in self._open_generations
            )
            gap = GapRecord(
                gap_id=self._next_gap_id,
                generations=stranded,
                first_event_seq=first_event_seq,
                detected_at=self._loop.time(),
                error=error,
            )
            self._gaps.append(gap)
            for generation in stranded:
                self._open_generations.discard(generation)
                for event in self._events_by_generation.get(generation, ()):
                    event.gap_generation = True

        # Pending explicit callers must not hang on a dead connection.
        for request_id, (future, _, _) in list(self._pending_requests.items()):
            if not future.done():
                future.set_exception(error)
        self._pending_requests.clear()
        self._fetching = False
        self._gapped = True

        for subscription in list(self._subscriptions):
            self._invoke_callback(subscription, error)

        if self._reconnect_cb is not None and self._running:
            self._reconnect_task = asyncio.ensure_future(self._auto_reconnect())

    async def _auto_reconnect(self) -> None:
        try:
            await self.reconnect()
        except BaseException as error:
            logger.warning("automatic reconnect failed: %r", error)

    # ------------------------------------------------------------------
    # delivery
    # ------------------------------------------------------------------

    def _deliver_events(self, record: FetchRecord) -> None:
        newly_covered = [
            event
            for event in self._events
            if event.served_by == record.fetch_id
        ]
        for subscription in list(self._subscriptions):
            if not subscription.active:
                continue
            matched = [
                event
                for event in newly_covered
                if event.seq > subscription.last_delivered_seq
                and self._matches(subscription, event)
            ]
            if matched:
                subscription.last_delivered_seq = max(
                    event.seq for event in matched
                )
                self._invoke_callback(subscription, matched)

    @staticmethod
    def _matches(subscription: Subscription, event: MonitorEvent) -> bool:
        if not subscription.subsystems:
            return True
        return any(
            subsystem in subscription.subsystems
            for subsystem in event.subsystems
        )

    def _invoke_callback(
        self,
        subscription: Subscription,
        payload: Union[Sequence[MonitorEvent], BaseException],
    ) -> None:
        try:
            subscription.callback(payload)
        except BaseException:
            logger.exception(
                "idle monitor subscription %s callback failed", subscription.id
            )


# vim: set expandtab shiftwidth=4 softtabstop=4 textwidth=79:
