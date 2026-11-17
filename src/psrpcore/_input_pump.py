# -*- coding: utf-8 -*-
# Copyright: (c) 2021, Jordan Borean (@jborean93) <jborean93@gmail.com>
# MIT License (see LICENSE or https://opensource.org/licenses/MIT)

import collections
import enum
import typing

from psrpcore._payload import PSRPMessage
from psrpcore.types import PSInvocationState

if typing.TYPE_CHECKING:
    from psrpcore._client import ClientGetCommandMetadata, ClientPowerShell, ClientRunspacePool


class InputPumpState(enum.Enum):
    """The state of a :class:`PipelineInputPump`."""

    running = enum.auto()  #: Pulling from the source as the send budget allows.
    paused = enum.auto()  #: Suspended by the caller, nothing is pulled or prepared.
    drained = enum.auto()  #: The source is exhausted and the single EOF has been queued.
    complete = enum.auto()  #: Terminal - the EOF was handed to the transport.
    closed = enum.auto()  #: Terminal - the peer finished early or the pump was cancelled.
    failed = enum.auto()  #: Terminal - the pump failed with an error.


class PipelineInputPump:
    """Pausable pipeline input driver.

    Drives objects from a source iterator into a client pipeline as
    ``PipelineInput`` messages. The pump follows the sans-IO model like the
    rest of this library, it never blocks and only does work when
    :meth:`refill` is called (directly or internally as send budget is
    freed).

    The pump is pull based and lazy: it only takes objects from the source
    while the send budget allows it. The budget is ``max_outstanding`` bytes
    of prepared data that may sit in the Runspace Pool send buffer waiting
    for the transport. As :meth:`RunspacePool.data_to_send` hands data to
    the transport the budget is credited and the pump pulls more objects.
    The source is never read ahead of the budget.

    Every object moves through 3 distinct stages that are tracked
    independently:

    *   **pulled** (:attr:`pulled_count`) - taken out of the source iterator.
    *   **prepared** (:attr:`prepared_count`) - serialised into the Runspace
        Pool send buffer, waiting for the transport.
    *   **sent** (:attr:`sent_count`) - handed to the transport by
        :meth:`RunspacePool.data_to_send`.

    PSRP has no per-object processing acknowledgement so ``sent`` only means
    the data was given to the local transport. It does not imply the peer
    has received, let alone processed, those objects.

    Once the source is exhausted the pump queues exactly one
    ``EndOfPipelineInput`` (EOF) message behind the remaining input. If the
    pipeline finishes on the peer first, or the pump is cancelled, no
    further objects are pulled from the source and no EOF is queued. Objects
    already pulled but not yet prepared stay available through
    :attr:`unsent`, and the stage counters are preserved so the caller can
    reconcile what was taken from the source but not sent.

    Each pump only touches its own budget and pipeline so a stalled pipeline
    does not stop pumps of other pipelines from making progress. When the
    pump reaches a terminal state, through completion, early closure, or an
    error, every callback registered with :meth:`add_done_callback` is
    called exactly once with a definitive outcome.

    Args:
        pipeline: The started client pipeline to send the input to.
        source: The iterable of objects to send as pipeline input.
        max_outstanding: The maximum number of bytes this pump may have
            sitting in the Runspace Pool send buffer before it stops pulling
            from the source. A single object larger than this value is still
            prepared so the pump cannot stall on oversized input.
    """

    def __init__(
        self,
        pipeline: typing.Union["ClientPowerShell", "ClientGetCommandMetadata"],
        source: typing.Iterable[typing.Any],
        max_outstanding: int = 262144,
    ) -> None:
        if max_outstanding < 1:
            raise ValueError("max_outstanding must be 1 or larger")

        self._pipeline = pipeline
        self._pool: "ClientRunspacePool" = pipeline.runspace_pool
        self._source = iter(source)
        self._max_outstanding = max_outstanding

        self._state = InputPumpState.running
        self._pulled = 0
        self._prepared = 0
        self._sent = 0
        self._pending: typing.Deque[typing.Any] = collections.deque()
        self._outstanding = 0
        self._inflight: typing.Dict[int, int] = {}
        self._eof_message: typing.Optional[PSRPMessage] = None
        self._eof_queued = False
        self._eof_delivered = False
        self._error: typing.Optional[BaseException] = None
        self._close_reason: typing.Optional[str] = None
        self._done_callbacks: typing.List[typing.Callable[["PipelineInputPump"], None]] = []

        self._pool.add_send_listener(self._on_message_sent, pipeline.pipeline_id)

    def __enter__(self) -> "PipelineInputPump":
        return self

    def __exit__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} state={self._state.name} pulled={self._pulled} prepared={self._prepared} "
            f"sent={self._sent} outstanding={self._outstanding}>"
        )

    @property
    def state(self) -> InputPumpState:
        """The current state of the pump."""
        return self._state

    @property
    def done(self) -> bool:
        """Whether the pump reached a terminal state."""
        return self._state in (InputPumpState.complete, InputPumpState.closed, InputPumpState.failed)

    @property
    def pulled_count(self) -> int:
        """Objects taken out of the source iterator."""
        return self._pulled

    @property
    def prepared_count(self) -> int:
        """Objects serialised into the Runspace Pool send buffer."""
        return self._prepared

    @property
    def sent_count(self) -> int:
        """Objects handed to the transport.

        This only tracks the local handover to the transport. PSRP offers no
        per-object receipt so this does not imply the peer received or
        processed these objects.
        """
        return self._sent

    @property
    def unsent(self) -> typing.Tuple[typing.Any, ...]:
        """Objects pulled from the source but not yet prepared for sending.

        This is normally empty as the pump only pulls what it can
        immediately prepare. If preparing an object fails the object stays
        here so the caller can reconcile or retry it. Objects already
        prepared but not yet handed to the transport are counted by
        ``prepared_count - sent_count``.
        """
        return tuple(self._pending)

    @property
    def outstanding(self) -> int:
        """Bytes prepared by this pump still waiting in the send buffer."""
        return self._outstanding

    @property
    def eof_queued(self) -> bool:
        """Whether the end of input marker has been queued for sending."""
        return self._eof_queued

    @property
    def eof_delivered(self) -> bool:
        """Whether the end of input marker was handed to the transport."""
        return self._eof_delivered

    @property
    def error(self) -> typing.Optional[BaseException]:
        """The error that failed the pump, if any."""
        return self._error

    @property
    def close_reason(self) -> typing.Optional[str]:
        """A human readable reason for why the pump was closed early."""
        return self._close_reason

    def refill(self) -> int:
        """Pulls and prepares objects while the send budget allows.

        This is a non-blocking call that pulls objects from the source and
        queues them as pipeline input until ``max_outstanding`` bytes are
        waiting in the Runspace Pool send buffer. When the source is
        exhausted exactly one EOF is queued behind the remaining input.

        Returns:
            int: The number of objects prepared during this call.
        """
        if self._state != InputPumpState.running:
            return 0

        pipeline_state = self._pipeline.state
        if pipeline_state == PSInvocationState.NotStarted:
            return 0
        if pipeline_state != PSInvocationState.Running:
            self.close(reason=f"pipeline state is {pipeline_state.name}")
            return 0

        prepared = 0
        try:
            while self._outstanding < self._max_outstanding:
                if not self._pending:
                    self._pending.append(next(self._source))
                    self._pulled += 1

                message = self._pipeline.send(self._pending[0])
                self._pending.popleft()
                self._prepared += 1
                prepared += 1
                self._inflight[message.object_id] = len(message)
                self._outstanding += len(message)

        except StopIteration:
            self._queue_eof()

        except Exception as e:
            self.fail(e)
            raise

        return prepared

    def pause(self) -> None:
        """Suspends pulling from the source until :meth:`resume` is called."""
        if self._state == InputPumpState.running:
            self._state = InputPumpState.paused

    def resume(self) -> None:
        """Resumes pulling from the source after :meth:`pause`."""
        if self._state == InputPumpState.paused:
            self._state = InputPumpState.running
            self.refill()

    def cancel(self) -> None:
        """Stops the pump without sending EOF.

        No further objects are pulled from the source. Objects already
        pulled but not yet sent stay available through :attr:`unsent` and
        the stage counters.
        """
        self.close(reason="cancelled")

    def close(
        self,
        reason: typing.Optional[str] = None,
    ) -> None:
        """Closes the pump and notifies any waiters.

        This is idempotent and terminal, the pump stops pulling from the
        source and unregisters itself from the Runspace Pool.

        Args:
            reason: A human readable reason for closing the pump.
        """
        if self.done:
            return

        self._state = InputPumpState.closed
        self._close_reason = reason
        self._pool.remove_send_listener(self._on_message_sent, self._pipeline.pipeline_id)
        self._fire_done()

    def fail(
        self,
        error: BaseException,
    ) -> None:
        """Marks the pump as failed and notifies any waiters.

        This is idempotent and terminal. It should be called when the
        transport or any other part of the connection fails so that anyone
        waiting on the pump gets a definitive result.

        Args:
            error: The error that caused the failure.
        """
        if self.done:
            return

        self._state = InputPumpState.failed
        self._error = error
        self._pool.remove_send_listener(self._on_message_sent, self._pipeline.pipeline_id)
        self._fire_done()

    def on_pipeline_state(
        self,
        state: PSInvocationState,
    ) -> None:
        """Notifies the pump of a pipeline state change.

        Call this with the state of a :class:`psrpcore.PipelineStateEvent`
        for the pump's pipeline. A terminal pipeline state stops the pump
        from pulling any further objects, the pulled but unsent state is
        preserved for reconciliation.

        Args:
            state: The new pipeline state.
        """
        if state in (PSInvocationState.Completed, PSInvocationState.Failed, PSInvocationState.Stopped):
            self.close(reason=f"pipeline {state.name.lower()}")

    def add_done_callback(
        self,
        callback: typing.Callable[["PipelineInputPump"], None],
    ) -> None:
        """Registers a callback fired once when the pump finishes.

        The callback is invoked with the pump exactly once when it reaches a
        terminal state, whether through completion, early closure, or
        failure. If the pump is already finished the callback is invoked
        immediately.

        Args:
            callback: The callable to invoke with the finished pump.
        """
        if self.done:
            callback(self)
        else:
            self._done_callbacks.append(callback)

    def _queue_eof(self) -> None:
        # The EOF is queued exactly once, behind any input still waiting in
        # the send buffer.
        if self._eof_queued:
            return

        self._eof_queued = True
        self._state = InputPumpState.drained
        try:
            self._eof_message = self._pipeline.send_eof()
        except Exception as e:
            self.fail(e)
            raise

        self._inflight[self._eof_message.object_id] = len(self._eof_message)
        self._outstanding += len(self._eof_message)

    def _on_message_sent(
        self,
        message: PSRPMessage,
    ) -> None:
        size = self._inflight.pop(message.object_id, None)
        if size is None:
            # Not prepared by this pump, e.g. the CreatePipeline or host
            # response messages for this pipeline.
            return

        self._outstanding -= size
        if message is self._eof_message:
            self._eof_delivered = True
            if not self.done:
                self._state = InputPumpState.complete
                self._pool.remove_send_listener(self._on_message_sent, self._pipeline.pipeline_id)
                self._fire_done()
            return

        self._sent += 1
        if self._state == InputPumpState.running:
            self.refill()

    def _fire_done(self) -> None:
        callbacks, self._done_callbacks = self._done_callbacks, []
        for callback in callbacks:
            callback(self)
