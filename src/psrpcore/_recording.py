# -*- coding: utf-8 -*-
# Copyright: (c) 2021, Jordan Borean (@jborean93) <jborean93@gmail.com>
# MIT License (see LICENSE or https://opensource.org/licenses/MIT)

"""Offline consistency checks for PSRP capture recordings.

This module replays a captured sequence of PSRP events - runspace pool state
changes, pipeline state changes, host calls/responses, and end of input
markers - and verifies that the states they imply can all hold at the same
time. It does not just compare event names; it tracks the in-flight
pipelines, unanswered host calls, and pipelines whose input was never ended
and makes them participate in the judgement, e.g. when the pool reports
``Closed``.

The checker reports the earliest point at which the recorded states can no
longer hold simultaneously (:class:`Contradiction`) together with the
original recorded events that conflict with each other. Events that are
missing from the recording (e.g. an intermediate state that was skipped, a
disconnect that was never captured before a reconnect) are reported as
:class:`EvidenceGap` entries - they are treated as gaps in the evidence and
are never silently filled in as a successful history.

Each recorded event is tagged with the connection it was captured on. After
a reconnect the recording continues on a new connection identifier and the
events are interpreted in the context of that connection.

When the recording is consistent it is classified into one of three endings
and each is explained separately:

*   ``NormalStop`` - the pool was closed with every pipeline in a terminal
    state, no unanswered host calls, and no dangling input.
*   ``Failed`` - the pool reported ``Broken`` or a pipeline reported
    ``Failed``.
*   ``DisconnectedUndetermined`` - the recording ends with the pool
    disconnected (or simply before any terminal state) so the final outcome
    cannot be determined from the evidence.
"""

import enum
import typing
import uuid

from psrpcore._events import (
    EndOfPipelineInputEvent,
    PipelineHostCallEvent,
    PipelineHostResponseEvent,
    PipelineStateEvent,
    PSRPEvent,
    RunspacePoolHostCallEvent,
    RunspacePoolHostResponseEvent,
    RunspacePoolStateEvent,
)
from psrpcore.types import (
    PSInvocationState,
    RunspacePoolState,
)

_POOL_TERMINAL = {RunspacePoolState.Closed, RunspacePoolState.Broken}
# States after which no more events can arrive on the same connection.
_POOL_CONNECTION_TERMINAL = _POOL_TERMINAL | {RunspacePoolState.Disconnected}

_PIPELINE_TERMINAL = {
    PSInvocationState.Stopped,
    PSInvocationState.Completed,
    PSInvocationState.Failed,
}

# Direct (one step) state transitions. A transition that is only reachable
# through multiple steps is an evidence gap (missing intermediate events),
# one that is not reachable at all is a contradiction.
_POOL_TRANSITIONS = {
    RunspacePoolState.BeforeOpen: {RunspacePoolState.Opening},
    RunspacePoolState.Opening: {RunspacePoolState.Opened, RunspacePoolState.Broken},
    RunspacePoolState.Opened: {
        RunspacePoolState.Closing,
        RunspacePoolState.Disconnecting,
        RunspacePoolState.Broken,
    },
    RunspacePoolState.Closing: {RunspacePoolState.Closed, RunspacePoolState.Broken},
    RunspacePoolState.Disconnecting: {RunspacePoolState.Disconnected, RunspacePoolState.Broken},
    RunspacePoolState.Disconnected: {RunspacePoolState.Connecting},
    RunspacePoolState.Connecting: {RunspacePoolState.Opened, RunspacePoolState.Broken},
    RunspacePoolState.Closed: set(),
    RunspacePoolState.Broken: set(),
}

_PIPELINE_TRANSITIONS = {
    PSInvocationState.NotStarted: {PSInvocationState.Running, PSInvocationState.Failed},
    PSInvocationState.Running: {
        PSInvocationState.Stopping,
        PSInvocationState.Completed,
        PSInvocationState.Failed,
        PSInvocationState.Disconnected,
    },
    PSInvocationState.Stopping: {
        PSInvocationState.Stopped,
        PSInvocationState.Failed,
        PSInvocationState.Disconnected,
    },
    PSInvocationState.Disconnected: {PSInvocationState.Running, PSInvocationState.Failed},
    PSInvocationState.Stopped: set(),
    PSInvocationState.Completed: set(),
    PSInvocationState.Failed: set(),
}


def _is_reachable(
    transitions: typing.Dict[typing.Any, typing.Set[typing.Any]],
    start: typing.Any,
    goal: typing.Any,
) -> bool:
    """Checks whether goal is reachable from start through the transitions."""
    seen = {start}
    queue = [start]
    while queue:
        current = queue.pop(0)
        for next_state in transitions.get(current, set()):
            if next_state == goal:
                return True
            if next_state not in seen:
                seen.add(next_state)
                queue.append(next_state)

    return False


class RecordingOutcome(enum.Enum):
    """The ending a consistent recording resolves to."""

    NormalStop = "normal_stop"  #: The pool closed cleanly with no in-flight work left.
    Failed = "failed"  #: The pool broke or a pipeline failed.
    DisconnectedUndetermined = "disconnected_undetermined"  #: Disconnected or truncated; outcome unknown.


class RecordedEvent:
    """A single PSRP event captured on a specific connection.

    Args:
        connection: Identifier of the connection the event was captured on.
            Events captured after a reconnect must use a new identifier and
            are interpreted in the context of that connection.
        event: The :class:`psrpcore.PSRPEvent` that was captured.

    Attributes:
        connection: See args.
        event: See args.
        index: The position of this record in the checked recording, set by
            :func:`check_recording`.
    """

    def __init__(
        self,
        connection: typing.Hashable,
        event: PSRPEvent,
    ):
        self.connection = connection
        self.event = event
        self.index = -1

    def __repr__(self) -> str:
        return f"<RecordedEvent #{self.index} connection={self.connection!r} event={self.event!r}>"


class Contradiction:
    """A point where the recorded states cannot simultaneously hold.

    Args:
        message: Human readable description of the conflict.
        events: The original recorded events that conflict with each other.
            The first event is the one at which the contradiction surfaced.

    Attributes:
        message: See args.
        events: See args.
    """

    def __init__(
        self,
        message: str,
        events: typing.List[RecordedEvent],
    ):
        self.message = message
        self.events = events

    def __repr__(self) -> str:
        return f"<Contradiction {self.message!r}>"


class EvidenceGap:
    """A missing intermediate event in the recording.

    Gaps are missing evidence - they are never treated as an implicitly
    successful history. A recording with gaps can still be consistent but the
    gaps are reported so the missing evidence stays visible.

    Args:
        message: Human readable description of what is missing.
        events: The recorded events around the gap.

    Attributes:
        message: See args.
        events: See args.
    """

    def __init__(
        self,
        message: str,
        events: typing.List[RecordedEvent],
    ):
        self.message = message
        self.events = events

    def __repr__(self) -> str:
        return f"<EvidenceGap {self.message!r}>"


class RecordingReport:
    """The result of an offline consistency check of a recording.

    Attributes:
        contradictions: All the contradictions found, in recording order.
        gaps: All the evidence gaps found, in recording order.
        outcome: The :class:`RecordingOutcome` the recording resolves to, or
            ``None`` when the recording is inconsistent and no ending can be
            trusted.
    """

    def __init__(self) -> None:
        self.contradictions: typing.List[Contradiction] = []
        self.gaps: typing.List[EvidenceGap] = []
        self.outcome: typing.Optional[RecordingOutcome] = None

    @property
    def is_consistent(self) -> bool:
        """Whether the recording is free of contradictions."""
        return not self.contradictions

    @property
    def earliest_contradiction(self) -> typing.Optional[Contradiction]:
        """The earliest point where the states cannot hold simultaneously."""
        return self.contradictions[0] if self.contradictions else None

    def explain(self) -> str:
        """Builds a human readable explanation of the check result."""
        if not self.is_consistent:
            earliest = self.earliest_contradiction
            assert earliest is not None  # guaranteed by is_consistent
            lines = [
                "The recording is inconsistent; the earliest states that cannot simultaneously hold "
                f"surfaced at record #{earliest.events[0].index}: {earliest.message}",
                "Related recorded events:",
            ]
            lines.extend(f"  #{e.index} [{e.connection}] {e.event!r}" for e in earliest.events)
            if len(self.contradictions) > 1:
                lines.append(f"A further {len(self.contradictions) - 1} contradiction(s) were found.")
            if self.gaps:
                lines.append(f"There are also {len(self.gaps)} evidence gap(s) in the recording.")
            return "\n".join(lines)

        if self.outcome == RecordingOutcome.NormalStop:
            explanation = (
                "Normal stop: the runspace pool was closed with every pipeline in a terminal state, "
                "no unanswered host calls, and no pipeline input left open."
            )
        elif self.outcome == RecordingOutcome.Failed:
            explanation = "Failure: the recording shows a broken runspace pool and/or a failed pipeline."
        else:
            explanation = (
                "Disconnected/undetermined: the recording ends with the pool disconnected or before it "
                "reached a terminal state, so the final outcome cannot be determined from the evidence."
            )

        if self.gaps:
            explanation += (
                f" Note: {len(self.gaps)} evidence gap(s) were found; the missing events are not assumed "
                "to be a successful history."
            )

        return explanation


class _PipelineTrack:
    """Tracks the recorded facts of a single pipeline."""

    def __init__(self) -> None:
        self.state = PSInvocationState.NotStarted
        self.state_event: typing.Optional[RecordedEvent] = None
        self.input_ended = False
        self.input_event: typing.Optional[RecordedEvent] = None
        self.pending_calls: typing.Dict[int, RecordedEvent] = {}
        self.answered_calls: typing.Set[int] = set()
        self.connection: typing.Optional[typing.Hashable] = None

    @property
    def is_terminal(self) -> bool:
        return self.state in _PIPELINE_TERMINAL


class _PoolTrack:
    """Tracks the recorded facts of a single runspace pool."""

    def __init__(self) -> None:
        self.state = RunspacePoolState.BeforeOpen
        self.state_event: typing.Optional[RecordedEvent] = None
        self.connection: typing.Optional[typing.Hashable] = None
        self.terminal_connections: typing.Dict[typing.Hashable, RunspacePoolState] = {}
        self.pending_calls: typing.Dict[int, RecordedEvent] = {}
        self.answered_calls: typing.Set[int] = set()
        self.pipelines: typing.Dict[uuid.UUID, _PipelineTrack] = {}


def check_recording(records: typing.Iterable[RecordedEvent]) -> RecordingReport:
    """Checks a captured PSRP recording for offline consistency.

    Replays the recorded events in order and verifies that the pool states,
    pipeline states, host calls, and end of input markers can all hold at the
    same time. The report contains every contradiction found - the first one
    being the earliest point where the states stop being simultaneously
    satisfiable - as well as any evidence gaps and the ending the recording
    resolves to.

    Args:
        records: The recorded events in capture order.

    Returns:
        The :class:`RecordingReport` for the recording.
    """
    checker = _Checker()
    for index, record in enumerate(records):
        record.index = index
        checker.process(record)

    return checker.finish()


class _Checker:
    """Stateful replay of a recording, see :func:`check_recording`."""

    def __init__(self) -> None:
        self.report = RecordingReport()
        self.pools: typing.Dict[uuid.UUID, _PoolTrack] = {}

    def process(self, record: RecordedEvent) -> None:
        event = record.event
        pool = self.pools.setdefault(event.runspace_pool_id, _PoolTrack())

        if record.connection in pool.terminal_connections:
            state = pool.terminal_connections[record.connection].name
            self._contradict(
                f"event captured on connection {record.connection!r} after the pool reached "
                f"{state} on that connection",
                [record, pool.state_event],
            )
            return

        if isinstance(event, RunspacePoolStateEvent):
            self._process_pool_state(pool, record)
        elif isinstance(event, PipelineStateEvent):
            self._process_pipeline_state(pool, record)
        elif isinstance(event, (PipelineHostCallEvent, RunspacePoolHostCallEvent)):
            self._process_host_call(pool, record)
        elif isinstance(event, (PipelineHostResponseEvent, RunspacePoolHostResponseEvent)):
            self._process_host_response(pool, record)
        elif isinstance(event, EndOfPipelineInputEvent):
            self._process_end_of_input(pool, record)
        # All other event types carry no state relevant to this check.

    def finish(self) -> RecordingReport:
        failed = False
        undetermined = False

        for pool in self.pools.values():
            for pipeline_id, pipeline in pool.pipelines.items():
                if pipeline.is_terminal and not pipeline.input_ended:
                    self._gap(
                        f"pipeline {pipeline_id} reached {pipeline.state.name} but no end of input was "
                        "ever recorded for it",
                        [pipeline.state_event],
                    )

                if pipeline.state == PSInvocationState.Failed:
                    failed = True

                for ci, call_record in pipeline.pending_calls.items():
                    self._gap(
                        f"host call ci={ci} for pipeline {pipeline_id} was never answered in the recording",
                        [call_record],
                    )

            for ci, call_record in pool.pending_calls.items():
                self._gap(
                    f"runspace pool host call ci={ci} was never answered in the recording",
                    [call_record],
                )

            if pool.state == RunspacePoolState.Broken:
                failed = True
            elif pool.state != RunspacePoolState.Closed:
                undetermined = True

        if self.report.is_consistent:
            if failed:
                self.report.outcome = RecordingOutcome.Failed
            elif undetermined:
                self.report.outcome = RecordingOutcome.DisconnectedUndetermined
            else:
                self.report.outcome = RecordingOutcome.NormalStop

        return self.report

    def _contradict(
        self,
        message: str,
        events: typing.Sequence[typing.Optional[RecordedEvent]],
    ) -> None:
        self.report.contradictions.append(Contradiction(message, [e for e in events if e is not None]))

    def _gap(
        self,
        message: str,
        events: typing.Sequence[typing.Optional[RecordedEvent]],
    ) -> None:
        self.report.gaps.append(EvidenceGap(message, [e for e in events if e is not None]))

    def _get_pipeline(
        self,
        pool: _PoolTrack,
        record: RecordedEvent,
    ) -> _PipelineTrack:
        pipeline_id = record.event.pipeline_id
        if pipeline_id not in pool.pipelines:
            track = _PipelineTrack()
            track.connection = record.connection
            pool.pipelines[pipeline_id] = track
        return pool.pipelines[pipeline_id]

    def _check_transition(
        self,
        transitions: typing.Dict[typing.Any, typing.Set[typing.Any]],
        current: typing.Any,
        new: typing.Any,
        subject: str,
        record: RecordedEvent,
        previous: typing.Optional[RecordedEvent],
    ) -> bool:
        """Validates a state transition, returns True if it was a direct step."""
        if new == current:
            self._gap(f"duplicate {subject} state {new.name} recorded", [record, previous])
            return True

        if new in transitions[current]:
            return True

        if _is_reachable(transitions, current, new):
            self._gap(
                f"{subject} moved from {current.name} to {new.name} without the intermediate state(s) "
                "being recorded",
                [record, previous],
            )
            return True

        self._contradict(
            f"{subject} cannot move from {current.name} to {new.name}",
            [record, previous],
        )
        return False

    def _process_pool_state(
        self,
        pool: _PoolTrack,
        record: RecordedEvent,
    ) -> None:
        event = typing.cast(RunspacePoolStateEvent, record.event)
        new_state = event.state

        if pool.connection is not None and record.connection != pool.connection:
            # The recording continues on a new connection, interpret the
            # records in the context of that connection.
            if pool.state in _POOL_TERMINAL:
                self._contradict(
                    f"pool state {new_state.name} was captured on a new connection but the pool already "
                    f"reached the terminal state {pool.state.name}",
                    [record, pool.state_event],
                )
            else:
                if pool.state != RunspacePoolState.Disconnected:
                    self._gap(
                        f"recording resumes on connection {record.connection!r} but no disconnect of "
                        f"connection {pool.connection!r} was recorded",
                        [record, pool.state_event],
                    )
                # A reconnect re-establishes the pool from Disconnected; any
                # states in between are missing evidence, not a success.
                if new_state == RunspacePoolState.Opened:
                    self._gap(
                        "pool reported Opened on the new connection without a recorded Connecting state",
                        [record],
                    )
                elif new_state != RunspacePoolState.Connecting:
                    self._contradict(
                        f"pool cannot move from {pool.state.name} to {new_state.name} across a reconnect",
                        [record, pool.state_event],
                    )
        elif pool.state_event is not None:
            self._check_transition(_POOL_TRANSITIONS, pool.state, new_state, "pool", record, pool.state_event)
        # The first recorded pool state just establishes where the capture
        # started, it is not a transition.

        pool.state = new_state
        pool.state_event = record
        pool.connection = record.connection

        if new_state in _POOL_CONNECTION_TERMINAL:
            pool.terminal_connections[record.connection] = new_state

        if new_state == RunspacePoolState.Closed:
            self._check_pool_close(pool, record)
        elif new_state == RunspacePoolState.Broken:
            self._check_pool_broken(pool, record)

    def _check_pool_close(
        self,
        pool: _PoolTrack,
        record: RecordedEvent,
    ) -> None:
        """Validates that nothing is still in flight when the pool closes."""
        for pipeline_id, pipeline in pool.pipelines.items():
            if pipeline.is_terminal:
                continue

            details = f"pool closed while pipeline {pipeline_id} was still {pipeline.state.name}"
            if not pipeline.input_ended:
                details += " with its input never ended"
            self._contradict(details, [record, pipeline.state_event])

        pending = list(pool.pending_calls.values())
        pending.extend(call for p in pool.pipelines.values() for call in p.pending_calls.values())
        if pending:
            self._contradict(
                f"pool closed with {len(pending)} host call(s) still unanswered",
                [record] + pending,
            )
            pool.pending_calls.clear()
            for p in pool.pipelines.values():
                p.pending_calls.clear()

    def _check_pool_broken(
        self,
        pool: _PoolTrack,
        record: RecordedEvent,
    ) -> None:
        """A broken pool explains in-flight work but the evidence is missing."""
        for pipeline_id, pipeline in pool.pipelines.items():
            if not pipeline.is_terminal:
                self._gap(
                    f"pool broke while pipeline {pipeline_id} was still {pipeline.state.name}; no "
                    "terminal state was recorded for the pipeline",
                    [record, pipeline.state_event],
                )

        pending = list(pool.pending_calls.values())
        pending.extend(call for p in pool.pipelines.values() for call in p.pending_calls.values())
        for call_record in pending:
            self._gap(
                "host call was still unanswered when the pool broke",
                [record, call_record],
            )
        pool.pending_calls.clear()
        for p in pool.pipelines.values():
            p.pending_calls.clear()

    def _process_pipeline_state(
        self,
        pool: _PoolTrack,
        record: RecordedEvent,
    ) -> None:
        event = typing.cast(PipelineStateEvent, record.event)
        new_state = event.state
        pipeline = self._get_pipeline(pool, record)

        if pipeline.is_terminal:
            self._contradict(
                f"pipeline {event.pipeline_id} reported {new_state.name} after it had already reached "
                f"{pipeline.state.name}",
                [record, pipeline.state_event],
            )
        elif pipeline.state_event is not None:
            resumed = record.connection != pipeline.connection
            if resumed and pipeline.state != PSInvocationState.Disconnected:
                self._gap(
                    f"pipeline {event.pipeline_id} continues on a new connection but no Disconnected "
                    "state was recorded for it",
                    [record, pipeline.state_event],
                )
            if not (resumed and new_state == pipeline.state):
                self._check_transition(
                    _PIPELINE_TRANSITIONS, pipeline.state, new_state, f"pipeline {event.pipeline_id}",
                    record, pipeline.state_event,
                )

        pipeline.state = new_state
        pipeline.state_event = record
        pipeline.connection = record.connection

    def _process_host_call(
        self,
        pool: _PoolTrack,
        record: RecordedEvent,
    ) -> None:
        event = typing.cast(typing.Union[PipelineHostCallEvent, RunspacePoolHostCallEvent], record.event)
        ci = event.ci
        pending, answered, subject = self._call_scope(pool, record)

        if isinstance(event, PipelineHostCallEvent):
            pipeline = self._get_pipeline(pool, record)
            if pipeline.is_terminal:
                self._contradict(
                    f"host call ci={ci} was made for pipeline {event.pipeline_id} after it had already "
                    f"reached {pipeline.state.name}",
                    [record, pipeline.state_event],
                )

        if ci in pending:
            self._contradict(
                f"duplicate {subject} host call ci={ci} while the previous call is still unanswered",
                [record, pending[ci]],
            )
        elif ci in answered:
            self._contradict(
                f"{subject} host call ci={ci} reuses a call id that was already answered",
                [record],
            )
        else:
            pending[ci] = record

    def _process_host_response(
        self,
        pool: _PoolTrack,
        record: RecordedEvent,
    ) -> None:
        event = typing.cast(typing.Union[PipelineHostResponseEvent, RunspacePoolHostResponseEvent], record.event)
        ci = event.ci
        pending, answered, subject = self._call_scope(pool, record)

        if isinstance(event, PipelineHostResponseEvent):
            pipeline = pool.pipelines.get(event.pipeline_id)
            if pipeline is not None and pipeline.is_terminal:
                self._contradict(
                    f"host response ci={ci} was sent for pipeline {event.pipeline_id} after it had "
                    f"already reached {pipeline.state.name}",
                    [record, pipeline.state_event],
                )

        if ci in pending:
            call_record = pending.pop(ci)
            answered.add(ci)
            call_event = typing.cast(
                typing.Union[PipelineHostCallEvent, RunspacePoolHostCallEvent], call_record.event
            )
            if call_event.method_identifier != event.method_identifier:
                self._contradict(
                    f"{subject} host response ci={ci} answers {event.method_identifier!r} but the call "
                    f"was for {call_event.method_identifier!r}",
                    [record, call_record],
                )
        elif ci in answered:
            self._contradict(
                f"duplicate {subject} host response for ci={ci}",
                [record],
            )
        else:
            self._gap(
                f"{subject} host response ci={ci} has no matching host call in the recording",
                [record],
            )

    def _call_scope(
        self,
        pool: _PoolTrack,
        record: RecordedEvent,
    ) -> typing.Tuple[typing.Dict[int, RecordedEvent], typing.Set[int], str]:
        """Gets the pending/answered call tables for the event's scope."""
        if record.event.pipeline_id is not None:
            pipeline = self._get_pipeline(pool, record)
            return pipeline.pending_calls, pipeline.answered_calls, "pipeline"
        return pool.pending_calls, pool.answered_calls, "runspace pool"

    def _process_end_of_input(
        self,
        pool: _PoolTrack,
        record: RecordedEvent,
    ) -> None:
        event = typing.cast(EndOfPipelineInputEvent, record.event)
        pipeline = self._get_pipeline(pool, record)

        if pipeline.input_ended:
            self._contradict(
                f"pipeline {event.pipeline_id} input was ended more than once",
                [record, pipeline.input_event],
            )
        elif pipeline.is_terminal:
            self._contradict(
                f"pipeline {event.pipeline_id} input ended after it had already reached "
                f"{pipeline.state.name}",
                [record, pipeline.state_event],
            )

        pipeline.input_ended = True
        pipeline.input_event = record
