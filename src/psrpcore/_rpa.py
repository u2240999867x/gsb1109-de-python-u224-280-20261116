# -*- coding: utf-8 -*-
# Copyright: (c) 2021, Jordan Borean (@jborean93) <jborean93@gmail.com>
# MIT License (see LICENSE or https://opensource.org/licenses/MIT)

"""RPA stream collection with provenance tracking.

PowerShell pipelines separate their output into multiple streams (output,
error, warning, verbose, debug and information) but the commands inside a
pipeline are free to merge those streams together or discard them entirely
(``2>&1``, ``3>$null`` and so on). An RPA collector that stores each stream in
its own table therefore cannot assume that the shape of an object proves where
it came from: an :class:`psrpcore.types.ErrorRecord` written to the output
stream is still an output object even though it "looks like" an error.

This module keeps two kinds of evidence apart:

* The :class:`PipelineDeclaration` is built from the
  :class:`psrpcore.PowerShell` metadata the client sent. It records, per
  statement and per command, which streams are kept native, merged into the
  output stream or discarded to ``$null`` on the remote host.
* The :class:`CollectedRecord` rows are built from the PSRP events that were
  actually received. A record that arrived on a dedicated record channel (for
  example an :class:`psrpcore.ErrorRecordEvent`) is provably from that stream.
  An object that arrived on the output stream is only provably output when the
  declaration rules out every merged origin; otherwise its origin is recorded
  as ambiguous and any stream-like shape is stored as a non-authoritative hint.

Records that the remote host discarded because of a ``$null`` redirection are
never received over PSRP and must never be fabricated. The declaration exposes
which streams were discarded so query results can state "unknown because it was
discarded" instead of pretending the count was zero.
"""

from __future__ import annotations

import dataclasses
import enum
import typing
import uuid

from psrpcore._events import (
    CreatePipelineEvent,
    DebugRecordEvent,
    ErrorRecordEvent,
    InformationRecordEvent,
    PipelineOutputEvent,
    PipelineStateEvent,
    ProgressRecordEvent,
    PSRPEvent,
    VerboseRecordEvent,
    WarningRecordEvent,
)
from psrpcore._pipeline import PowerShell
from psrpcore.types import PSInvocationState, PSObject, PipelineResultTypes

if typing.TYPE_CHECKING:
    from psrpcore._command import Command


class ObservedStream(enum.Enum):
    """The PowerShell streams a collector can distinguish.

    The values double as the stable identifiers used in the serialized table
    rows so stored data does not depend on Python enum identity.
    """

    Output = "output"
    Error = "error"
    Warning = "warning"
    Verbose = "verbose"
    Debug = "debug"
    Information = "information"
    Progress = "progress"


# The streams that Command.redirect_* can target. Progress has no redirection
# API so it is intentionally excluded from the merge/discard computations.
_REDIRECTABLE_STREAMS: typing.List[ObservedStream] = [
    ObservedStream.Error,
    ObservedStream.Warning,
    ObservedStream.Verbose,
    ObservedStream.Debug,
    ObservedStream.Information,
]

_STREAM_EVENTS: typing.Dict[typing.Any, ObservedStream] = {
    PipelineOutputEvent: ObservedStream.Output,
    ErrorRecordEvent: ObservedStream.Error,
    WarningRecordEvent: ObservedStream.Warning,
    VerboseRecordEvent: ObservedStream.Verbose,
    DebugRecordEvent: ObservedStream.Debug,
    InformationRecordEvent: ObservedStream.Information,
    ProgressRecordEvent: ObservedStream.Progress,
}

_SHAPE_TYPE_NAMES: typing.List[typing.Tuple[str, ObservedStream]] = [
    ("ErrorRecord", ObservedStream.Error),
    ("WarningRecord", ObservedStream.Warning),
    ("VerboseRecord", ObservedStream.Verbose),
    ("DebugRecord", ObservedStream.Debug),
    ("InformationRecord", ObservedStream.Information),
    ("ProgressRecord", ObservedStream.Progress),
]


class CollectionOutcome(enum.Enum):
    """The terminal conclusion reached about a pipeline."""

    NotStarted = "not_started"
    Running = "running"
    Completed = "completed"
    Failed = "failed"
    Stopped = "stopped"


class FailureClassification(enum.Enum):
    """How evidence of errors should be interpreted when querying results.

    These values deliberately separate an explicit pipeline failure from mere
    error-shaped data:

    * ``explicit_failure`` - the remote pipeline entered the ``Failed`` state
      and supplied a reason. This is the only proven failure.
    * ``error_records_without_failure`` - records provably arrived on the error
      stream but the pipeline itself did not fail (non-terminating errors).
    * ``error_shaped_output_only`` - the only error-like evidence is ordinary
      output objects that happen to look like errors. They do not prove any
      error stream activity.
    * ``discarded`` - the error stream was redirected to ``$null`` so any error
      records were dropped remotely and cannot be accounted for.
    * ``clean`` - no error evidence at all.
    * ``incomplete`` - the pipeline has not reached a terminal state yet.
    """

    ExplicitFailure = "explicit_failure"
    ErrorRecordsWithoutFailure = "error_records_without_failure"
    ErrorShapedOutputOnly = "error_shaped_output_only"
    Discarded = "discarded"
    Clean = "clean"
    Incomplete = "incomplete"


@dataclasses.dataclass(frozen=True)
class StreamDeclaration:
    """The declared destination of one stream on one command.

    Args:
        stream: The native PowerShell stream the declaration applies to.
        target: Where records of that stream go. ``none`` keeps the native
            stream, ``Output`` merges it into the output stream and ``Null``
            discards the records on the remote host.
    """

    stream: ObservedStream
    target: PipelineResultTypes

    @property
    def merged_to_output(self) -> bool:
        """Whether the stream is merged into the output stream."""
        return self.target == PipelineResultTypes.Output

    @property
    def discarded(self) -> bool:
        """Whether records of the stream are discarded remotely."""
        return self.target == PipelineResultTypes.Null

    def to_row(self) -> typing.Dict[str, typing.Any]:
        """Serialize the declaration to a JSON-friendly table row."""
        return {"stream": self.stream.value, "target": int(self.target)}


@dataclasses.dataclass(frozen=True)
class CommandDeclaration:
    """The stream declarations attached to a single command.

    Args:
        statement_index: Zero based index of the statement containing the
            command.
        command_index: Zero based index of the command within its statement.
        command_text: The command or script text that was declared.
        is_script: Whether the command wraps a script block.
        merge_unclaimed: Whether the command is the merge point for error
            records left unclaimed by earlier commands in the same statement.
        streams: The per-stream declarations for this command.
    """

    statement_index: int
    command_index: int
    command_text: str
    is_script: bool
    merge_unclaimed: bool
    streams: typing.Dict[ObservedStream, StreamDeclaration]

    def to_row(self) -> typing.Dict[str, typing.Any]:
        """Serialize the command declaration to a JSON-friendly table row."""
        return {
            "statement_index": self.statement_index,
            "command_index": self.command_index,
            "command_text": self.command_text,
            "is_script": self.is_script,
            "merge_unclaimed": self.merge_unclaimed,
            "streams": {stream.value: decl.to_row() for stream, decl in self.streams.items()},
        }


@dataclasses.dataclass(frozen=True)
class StatementDeclaration:
    """The declarations for one statement in a multi-statement pipeline.

    A redirection only applies to the command it was declared on and the
    statement that command belongs to. Statements are delimited in the PSRP
    metadata by the ``end_of_statement`` flag.
    """

    statement_index: int
    commands: typing.Tuple[CommandDeclaration, ...]

    @property
    def merged_streams(self) -> typing.FrozenSet[ObservedStream]:
        """Streams that may be merged into output within this statement."""
        merged: typing.Set[ObservedStream] = set()
        for command in self.commands:
            for declaration in command.streams.values():
                if declaration.merged_to_output:
                    merged.add(declaration.stream)

            # merge_unclaimed pulls previous commands' unclaimed error records
            # into this command's output.
            if command.merge_unclaimed:
                merged.add(ObservedStream.Error)

        return frozenset(merged)

    @property
    def discarded_streams(self) -> typing.FrozenSet[ObservedStream]:
        """Streams whose records are discarded remotely within this statement."""
        return frozenset(
            declaration.stream
            for command in self.commands
            for declaration in command.streams.values()
            if declaration.discarded
        )


@dataclasses.dataclass(frozen=True)
class PipelineDeclaration:
    """The full stream declaration captured for a pipeline.

    This is evidence of what the remote host was asked to do, not evidence of
    what it did. It is built solely from the PSRP pipeline metadata;
    redirections written inside script text are not visible at this level and
    are reported in :attr:`declaration_limits`.

    Args:
        pipeline_id: The pipeline the declaration belongs to.
        redirect_shell_error_to_out: The pipeline level
            ``RedirectShellErrorOutputPipe`` flag.
        statements: The per-statement declarations in execution order.
        declaration_limits: Human readable caveats about what the declaration
            cannot prove.
    """

    pipeline_id: uuid.UUID
    redirect_shell_error_to_out: bool
    statements: typing.Tuple[StatementDeclaration, ...]
    declaration_limits: typing.Tuple[str, ...]

    @property
    def commands(self) -> typing.Tuple[CommandDeclaration, ...]:
        """All command declarations across statements, in order."""
        return tuple(command for statement in self.statements for command in statement.commands)

    @property
    def merged_streams(self) -> typing.FrozenSet[ObservedStream]:
        """Union of every stream any statement may merge into output."""
        return frozenset(stream for statement in self.statements for stream in statement.merged_streams)

    @property
    def discarded_streams(self) -> typing.FrozenSet[ObservedStream]:
        """Union of every stream any statement discards remotely.

        PSRP events do not identify which command produced a record, so a
        stream appearing here means at least one command drops that stream -
        not that every record of the stream is missing.
        """
        return frozenset(stream for statement in self.statements for stream in statement.discarded_streams)

    def output_origin_candidates(self) -> typing.List[ObservedStream]:
        """The origins that an output object could provably have.

        Output always remains a candidate. Every stream merged into the output
        stream by any statement is added because wire events carry no command
        or statement attribution and the origin therefore cannot be narrowed
        further.
        """
        candidates = [ObservedStream.Output]
        candidates.extend(stream for stream in _REDIRECTABLE_STREAMS if stream in self.merged_streams)
        return candidates

    def to_row(self) -> typing.Dict[str, typing.Any]:
        """Serialize the whole declaration to JSON-friendly table rows."""
        return {
            "pipeline_id": str(self.pipeline_id),
            "redirect_shell_error_to_out": self.redirect_shell_error_to_out,
            "declaration_limits": list(self.declaration_limits),
            "statements": [
                {
                    "statement_index": statement.statement_index,
                    "commands": [command.to_row() for command in statement.commands],
                }
                for statement in self.statements
            ],
        }

    @classmethod
    def from_powershell(
        cls,
        pipeline_id: uuid.UUID,
        powershell: PowerShell,
    ) -> "PipelineDeclaration":
        """Build the declaration from the pipeline metadata sent over PSRP."""
        statements: typing.List[typing.List[CommandDeclaration]] = [[]]
        limits: typing.List[str] = []

        for command in powershell.commands:
            statement_index = len(statements) - 1
            command_index = len(statements[statement_index])
            streams = {
                stream: StreamDeclaration(stream=stream, target=_redirection_target(command, stream))
                for stream in _REDIRECTABLE_STREAMS
            }
            statements[statement_index].append(
                CommandDeclaration(
                    statement_index=statement_index,
                    command_index=command_index,
                    command_text=command.command_text,
                    is_script=command.is_script,
                    merge_unclaimed=command.merge_unclaimed,
                    streams=streams,
                )
            )

            if command.is_script:
                limits.append(
                    f"Statement {statement_index} command {command_index} is a script: stream redirections written "
                    f"inside the script text are not visible to PSRP and cannot be included in this declaration."
                )

            if command.end_of_statement:
                statements.append([])

        # The split appends a trailing empty list once the final statement was
        # terminated; drop it so the tuple only contains real statements.
        if statements and not statements[-1]:
            statements.pop()

        if powershell.redirect_shell_error_to_out:
            limits.append(
                "RedirectShellErrorOutputPipe is enabled: shell level errors are piped through the command error "
                "pipe and cannot be told apart from command errors on the wire."
            )

        return cls(
            pipeline_id=pipeline_id,
            redirect_shell_error_to_out=powershell.redirect_shell_error_to_out,
            statements=tuple(
                StatementDeclaration(statement_index=index, commands=tuple(commands))
                for index, commands in enumerate(statements)
            ),
            declaration_limits=tuple(limits),
        )


def _redirection_target(command: "Command", stream: ObservedStream) -> PipelineResultTypes:
    return getattr(command, f"merge_{stream.name.lower()}")


@dataclasses.dataclass(frozen=True)
class CollectedRecord:
    """One row of evidence built from an event that was actually received.

    The row never contains anything that was not observed on the wire. In
    particular the collector never synthesizes rows for streams the remote host
    discarded.

    Args:
        sequence: Zero based order in which the event was collected.
        pipeline_id: The pipeline the event targets, if any.
        observed_stream: The PSRP channel the event arrived on. This is always
            a hard fact.
        event: The raw :class:`psrpcore.PSRPEvent` for full inspection.
        payload: The convenience value carried by the event.
        proven_origin: The stream the record provably originated from, or
            ``None`` when the origin cannot be proven.
        uncertain_origins: The origins that cannot be ruled out when
            :attr:`proven_origin` is ``None``.
        shape_hint: A stream guessed from the object's .NET type names. This is
            never treated as proof of origin.
        notes: Human readable caveats about the proof.
    """

    sequence: int
    pipeline_id: typing.Optional[uuid.UUID]
    observed_stream: ObservedStream
    event: PSRPEvent
    payload: typing.Any
    proven_origin: typing.Optional[ObservedStream]
    uncertain_origins: typing.Tuple[ObservedStream, ...]
    shape_hint: typing.Optional[ObservedStream]
    notes: typing.Tuple[str, ...]

    @property
    def origin_is_proven(self) -> bool:
        """Whether :attr:`proven_origin` could be established."""
        return self.proven_origin is not None

    def to_row(self) -> typing.Dict[str, typing.Any]:
        """Serialize the record to a JSON-friendly table row."""
        return {
            "sequence": self.sequence,
            "pipeline_id": str(self.pipeline_id) if self.pipeline_id else None,
            "observed_stream": self.observed_stream.value,
            "proven_origin": self.proven_origin.value if self.proven_origin else None,
            "uncertain_origins": [stream.value for stream in self.uncertain_origins],
            "shape_hint": self.shape_hint.value if self.shape_hint else None,
            "notes": list(self.notes),
            "event_type": type(self.event).__name__,
            "payload_repr": repr(self.payload),
        }


@dataclasses.dataclass(frozen=True)
class PipelineResult:
    """The queryable conclusion for one collected pipeline.

    Args:
        pipeline_id: The pipeline the result describes.
        outcome: The outcome derived from the pipeline state events. Only an
            observed ``Failed`` state produces :attr:`CollectionOutcome.Failed`.
        failure_reason: The error record carried by an explicit ``Failed``
            state, if any.
        observed_counts: How many events actually arrived on each stream.
        discarded_streams: Streams the declaration says are discarded remotely.
        error_shaped_output: Output objects that merely look like errors.
        declaration: The declaration used for the proof, when registered.
    """

    pipeline_id: uuid.UUID
    outcome: CollectionOutcome
    failure_reason: typing.Optional[PSObject]
    observed_counts: typing.Dict[ObservedStream, int]
    discarded_streams: typing.FrozenSet[ObservedStream]
    error_shaped_output: typing.Tuple[CollectedRecord, ...]
    declaration: typing.Optional[PipelineDeclaration]

    @property
    def failed(self) -> bool:
        """Whether the pipeline provably failed."""
        return self.outcome == CollectionOutcome.Failed

    @property
    def has_error_stream_records(self) -> bool:
        """Whether records provably arrived on the error stream."""
        return self.observed_counts.get(ObservedStream.Error, 0) > 0

    def classify_failure(self) -> FailureClassification:
        """Return the defensible conclusion about error evidence.

        An explicit failure always wins. Otherwise real error stream records,
        mere error-shaped output, discarded streams and missing terminal state
        each produce a distinct, non-overstated conclusion.
        """
        if self.failed:
            return FailureClassification.ExplicitFailure

        if not _TERMINAL_OUTCOMES.get(self.outcome, False):
            return FailureClassification.Incomplete

        if self.has_error_stream_records:
            return FailureClassification.ErrorRecordsWithoutFailure

        if self.error_shaped_output:
            return FailureClassification.ErrorShapedOutputOnly

        if ObservedStream.Error in self.discarded_streams:
            return FailureClassification.Discarded

        return FailureClassification.Clean


_TERMINAL_OUTCOMES: typing.Dict[CollectionOutcome, bool] = {
    CollectionOutcome.NotStarted: False,
    CollectionOutcome.Running: False,
    CollectionOutcome.Completed: True,
    CollectionOutcome.Failed: True,
    CollectionOutcome.Stopped: True,
}


class StreamCollector:
    """Collects pipeline events while preserving stream provenance.

    A collector is typically shared at runspace pool level and fed every event
    returned by :meth:`psrpcore.ClientRunspacePool.next_event` (or the server
    equivalent). Declarations can be registered explicitly from
    :class:`psrpcore.PowerShell` metadata or implicitly by feeding the
    :class:`psrpcore.CreatePipelineEvent` through :meth:`handle_event`.

    Args:
        register_create_pipeline: Whether :meth:`handle_event` should register
            a declaration automatically when a create pipeline event is seen.
    """

    def __init__(self, register_create_pipeline: bool = True) -> None:
        self._register_create_pipeline = register_create_pipeline
        self._declarations: typing.Dict[uuid.UUID, PipelineDeclaration] = {}
        self._records: typing.Dict[typing.Optional[uuid.UUID], typing.List[CollectedRecord]] = {}
        self._state_events: typing.Dict[uuid.UUID, typing.List[PipelineStateEvent]] = {}
        self._sequence = 0

    @property
    def declarations(self) -> typing.Dict[uuid.UUID, PipelineDeclaration]:
        """The pipeline declarations currently held by the collector."""
        return dict(self._declarations)

    @property
    def state_events(self) -> typing.Dict[uuid.UUID, typing.List[PipelineStateEvent]]:
        """The pipeline state events actually received, keyed by pipeline."""
        return {pipeline_id: list(events) for pipeline_id, events in self._state_events.items()}

    def register_declaration(
        self,
        pipeline_id: uuid.UUID,
        powershell: PowerShell,
    ) -> PipelineDeclaration:
        """Record the stream declaration for a pipeline.

        Registering a declaration never creates any collected rows; it only
        stores what the remote host was asked to do so later events can be
        judged against it.
        """
        declaration = PipelineDeclaration.from_powershell(pipeline_id, powershell)
        self._declarations[pipeline_id] = declaration
        return declaration

    def handle_event(self, event: PSRPEvent) -> typing.Optional[CollectedRecord]:
        """Feed any PSRP event into the collector.

        Create pipeline events register (or refresh) the declaration. Stream
        events become :class:`CollectedRecord` rows. State events update the
        pipeline outcome. Everything else is ignored and returns ``None``.
        """
        if isinstance(event, CreatePipelineEvent):
            if self._register_create_pipeline and event.pipeline_id is not None:
                self.register_declaration(event.pipeline_id, event.pipeline)
            return None

        if isinstance(event, PipelineStateEvent) and event.pipeline_id is not None:
            self._state_events.setdefault(event.pipeline_id, []).append(event)
            return None

        return self.record_event(event)

    def record_event(self, event: PSRPEvent) -> typing.Optional[CollectedRecord]:
        """Collect a single stream event, returning its row when applicable.

        The observed channel is taken from the event class itself. The origin
        is proven directly for dedicated record channels while output events
        are evaluated against the registered declaration.
        """
        observed_stream = _STREAM_EVENTS.get(type(event))
        if observed_stream is None:
            return None

        payload = _event_payload(event)
        shape_hint = guess_stream_shape(payload)
        pipeline_id = event.pipeline_id
        declaration = self._declarations.get(pipeline_id) if pipeline_id else None
        notes: typing.List[str] = []

        if observed_stream == ObservedStream.Output:
            proven_origin, uncertain_origins = self._prove_output_origin(declaration, notes)
        else:
            proven_origin = observed_stream
            uncertain_origins = tuple()
            if declaration and observed_stream in declaration.discarded_streams:
                notes.append(
                    f"The {observed_stream.value} stream is redirected to $null by at least one command in the "
                    f"declaration; this event must originate from a command or scope where it was not discarded."
                )

        if shape_hint is not None and shape_hint != observed_stream:
            notes.append(
                f"The payload looks like a {shape_hint.value} record based on its type names but arrived on the "
                f"{observed_stream.value} stream; shape is not proof of origin."
            )

        record = CollectedRecord(
            sequence=self._sequence,
            pipeline_id=pipeline_id,
            observed_stream=observed_stream,
            event=event,
            payload=payload,
            proven_origin=proven_origin,
            uncertain_origins=uncertain_origins,
            shape_hint=shape_hint,
            notes=tuple(notes),
        )
        self._sequence += 1
        self._records.setdefault(pipeline_id, []).append(record)
        return record

    def records(
        self,
        pipeline_id: typing.Optional[uuid.UUID] = None,
        observed_stream: typing.Optional[ObservedStream] = None,
    ) -> typing.List[CollectedRecord]:
        """Return collected rows, optionally filtered by pipeline and stream."""
        if pipeline_id is not None:
            source: typing.Iterable[CollectedRecord] = list(self._records.get(pipeline_id, []))
        else:
            source = [record for records in self._records.values() for record in records]

        if observed_stream is not None:
            source = [record for record in source if record.observed_stream == observed_stream]

        return list(source)

    def result(self, pipeline_id: uuid.UUID) -> PipelineResult:
        """Build the queryable result for a pipeline.

        Only observed state and events contribute to the result. A stream
        redirected to ``$null`` is reported as discarded rather than counted as
        zero, and error-shaped output objects are listed separately so they can
        never be confused with a real failure.
        """
        pipeline_records = self._records.get(pipeline_id, [])
        declaration = self._declarations.get(pipeline_id)

        observed_counts: typing.Dict[ObservedStream, int] = {}
        error_shaped_output: typing.List[CollectedRecord] = []
        for record in pipeline_records:
            observed_counts[record.observed_stream] = observed_counts.get(record.observed_stream, 0) + 1
            if (
                record.observed_stream == ObservedStream.Output
                and record.shape_hint == ObservedStream.Error
            ):
                error_shaped_output.append(record)

        outcome, failure_reason = self._outcome(pipeline_id)

        return PipelineResult(
            pipeline_id=pipeline_id,
            outcome=outcome,
            failure_reason=failure_reason,
            observed_counts=observed_counts,
            discarded_streams=declaration.discarded_streams if declaration else frozenset(),
            error_shaped_output=tuple(error_shaped_output),
            declaration=declaration,
        )

    def _prove_output_origin(
        self,
        declaration: typing.Optional[PipelineDeclaration],
        notes: typing.List[str],
    ) -> typing.Tuple[typing.Optional[ObservedStream], typing.Tuple[ObservedStream, ...]]:
        if declaration is None:
            notes.append(
                "No pipeline declaration was registered: the output object's original stream cannot be proven and "
                "every redirectable stream remains a possible origin."
            )
            return None, tuple(_REDIRECTABLE_STREAMS + [ObservedStream.Output])

        candidates = tuple(declaration.output_origin_candidates())
        if candidates == (ObservedStream.Output,):
            return ObservedStream.Output, tuple()

        notes.append(
            "The declaration merges one or more non-output streams into output; this object's original stream "
            "cannot be narrowed beyond the listed candidates."
        )
        for limit in declaration.declaration_limits:
            notes.append(limit)
        return None, candidates

    def _outcome(
        self,
        pipeline_id: uuid.UUID,
    ) -> typing.Tuple[CollectionOutcome, typing.Optional[PSObject]]:
        events = self._state_events.get(pipeline_id)
        if not events:
            return CollectionOutcome.NotStarted, None

        latest = events[-1]
        mapping = {
            PSInvocationState.NotStarted: CollectionOutcome.NotStarted,
            PSInvocationState.Running: CollectionOutcome.Running,
            PSInvocationState.Completed: CollectionOutcome.Completed,
            PSInvocationState.Failed: CollectionOutcome.Failed,
            PSInvocationState.Stopped: CollectionOutcome.Stopped,
            PSInvocationState.Stopping: CollectionOutcome.Running,
            PSInvocationState.Disconnected: CollectionOutcome.Running,
        }
        return mapping[latest.state], latest.reason


def _event_payload(event: PSRPEvent) -> typing.Any:
    if isinstance(event, PipelineOutputEvent):
        return event.data

    # Every dedicated record event exposes the record through .record.
    return event.record  # type: ignore[attr-defined]


def guess_stream_shape(payload: typing.Any) -> typing.Optional[ObservedStream]:
    """Guess a stream from a payload's .NET type names.

    This inspects the PSObject type names only. A guess is never evidence of
    origin: an ``ErrorRecord`` instance can legitimately travel on the output
    stream after a merge or when a script emits one as ordinary output.
    """
    if not isinstance(payload, PSObject):
        return None

    type_names = payload.PSObject.type_names
    for marker, stream in _SHAPE_TYPE_NAMES:
        if any(marker in type_name for type_name in type_names):
            return stream

    return None
