# -*- coding: utf-8 -*-
# Copyright: (c) 2021, Jordan Borean (@jborean93) <jborean93@gmail.com>
# MIT License (see LICENSE or https://opensource.org/licenses/MIT)

import logging
import typing
import uuid
from xml.etree import ElementTree

from psrpcore._crypto import PSRemotingCrypto
from psrpcore._events import PSRPEvent, SessionCapabilityEvent
from psrpcore._exceptions import (
    InvalidPipelineState,
    InvalidRunspacePoolState,
    PSRPCoreError,
)
from psrpcore._payload import (
    DispatchPolicy,
    ProtocolVersion,
    PSRPMessage,
    PSRPPayload,
    SentFragment,
    StreamType,
    create_fragment,
    create_message,
    unpack_fragment,
    unpack_message,
)
from psrpcore.types import (
    ApartmentState,
    EndOfPipelineInput,
    ErrorRecord,
    HostInfo,
    PSInvocationState,
    PSObject,
    PSRPMessageType,
    PSThreadOptions,
    PSVersion,
    RunspacePoolState,
    SessionCapability,
    deserialize,
    serialize,
)

log = logging.getLogger(__name__)

T1 = typing.TypeVar("T1", bound="Pipeline")
T2 = typing.TypeVar("T2", bound="RunspacePool")


class RunspacePool(typing.Generic[T1]):
    """Runspace Pool base class.

    This is the base class for a Runspace Pool. It contains the common
    attributes and methods used by both a client and server based Runspace
    Pool.

    Args:
        runspace_pool_id: The UUID that identified the Runspace Pool.
        application_arguments: Any arguments supplied when creating the
            Runspace Pool as a client.
        application_private_data: Any special data supplied by the Runspace
            Pool as a server.
        ps_version: The PowerShell version.
        protocol_version: The PSRP protocol version that the pool understands.
        serialization_version: The serialization version used by the pool.
        dispatch_policy: The policy used to pick which buffered messages to
            send first. Defaults to sending messages in the order they were
            queued.
        prompt_response_burst: When using the ``prompt_response_first``
            policy this is the number of prompt response messages dispatched
            before normal data is serviced to avoid starving it.

    Attributes:
        runspace_pool_id: See args.
        our_capability: The SessionCapability of the caller.
        their_capability: The SessionCapability of the peer, only populated
            after the Runspace Pool has been opened.
        application_arguments: The application arguments from the client, will
            be populated for the server after the Runspace Pool has been
            opened.
        application_private_data: The app private data supplied by the server,
            will be populated for the client after the Runspace Pool has been
            opened.
        host: The HostInfo that contains host information of the client.
        state: The current state of the Runspace Pool.
        apartment_state: The apartment state of the thread used to execute
            commands within this Runspace Pool.
        thread_options: Determines whether a new thread is created for each
            invocation.
        pipeline_table: A dictionary that contains associated pipelines with
            this Runspace Pool.
    """

    def __new__(
        cls,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> "RunspacePool":
        if cls == RunspacePool:
            raise TypeError(
                f"Type {cls.__qualname__} cannot be instantiated; it can be used only as a base class for "
                f"client/server runspace pool types."
            )

        return super().__new__(cls)

    def __init__(
        self,
        runspace_pool_id: uuid.UUID,
        application_arguments: typing.Dict[str, typing.Any],
        application_private_data: typing.Dict[str, typing.Any],
        ps_version: typing.Optional[PSVersion] = None,
        protocol_version: typing.Optional[PSVersion] = None,
        serialization_version: typing.Optional[PSVersion] = None,
        dispatch_policy: typing.Union[DispatchPolicy, str] = DispatchPolicy.fifo,
        prompt_response_burst: int = 4,
    ) -> None:
        log.debug(
            "Creating Runspace Pool - RPID: %s, Type: %s",
            runspace_pool_id,
            type(self).__name__,
        )

        if prompt_response_burst < 1:
            raise ValueError("prompt_response_burst must be 1 or larger")

        self.runspace_pool_id = runspace_pool_id
        self.our_capability = SessionCapability(
            PSVersion=ps_version or PSVersion("2.0"),
            protocolversion=protocol_version or ProtocolVersion.Pwsh5.value,
            SerializationVersion=serialization_version or PSVersion("1.1.0.1"),
        )
        self.their_capability: typing.Optional[SessionCapability] = None
        self.application_arguments = application_arguments
        self.application_private_data = application_private_data
        self.host: typing.Optional[HostInfo] = None
        self.state = RunspacePoolState.BeforeOpen
        self.apartment_state = ApartmentState.Unknown
        self.thread_options = PSThreadOptions.Default
        self.pipeline_table: typing.Dict[uuid.UUID, T1] = {}

        self._is_client = True
        self._ci_handlers: typing.Dict[int, typing.Optional[typing.Callable[[PSRPEvent], None]]] = {}
        self._ci_events: typing.Dict[int, PSRPEvent] = {}
        self._ci_count = 1
        self._fragment_count = 1
        self._key_requested = False
        self._cipher = PSRemotingCrypto()
        self._min_runspaces = 0
        self._max_runspaces = 0
        self._send_buffer: typing.List[PSRPMessage] = []
        self._dispatch_policy = DispatchPolicy(dispatch_policy)
        self._prompt_response_burst = prompt_response_burst
        self._prompt_response_burst_used = 0

        # A message partially sent by the previous data_to_send call. It is
        # finished before the dispatch policy is evaluated again.
        self._in_flight: typing.Optional[PSRPMessage] = None

        # Fragments that have been handed to the transport, including those for
        # pipelines that have since been cancelled. This is kept so the
        # identity of data that has already been sent can still be accounted
        # for.
        self._sent_fragments: typing.List[SentFragment] = []

        # Raw bytes received but not yet processed.
        self._receive_buffer = bytearray()

        # Fragments for each object_id that have been received but not yet combined to a message.
        self._incoming_fragments: typing.Dict[int, typing.List[bytearray]] = {}

        # Messages from combined fragments that have been received but not yet returned.
        self._incoming_messages: typing.Dict[int, PSRPMessage] = {}

    @property
    def max_runspaces(
        self,
    ) -> int:
        """The maximum number of runspaces the pool maintains."""
        return self._max_runspaces

    @property
    def min_runspaces(
        self,
    ) -> int:
        """The minimum number of runspaces the pool maintains."""
        return self._min_runspaces

    @property
    def _ci_counter(
        self,
    ) -> int:
        """Counter used for ci calls."""
        ci = self._ci_count
        self._ci_count += 1
        return ci

    @property
    def _fragment_counter(
        self,
    ) -> int:
        """Counter used for fragment object IDs."""
        count = self._fragment_count
        self._fragment_count += 1
        return count

    def begin_close(self) -> None:
        """Marks the Runspace Pool to be in the closing phase."""
        self._change_state(RunspacePoolState.Closing)

    def close(self) -> None:
        """Marks the Runspace Pool as closed.

        This closes the Runspace Pool. Communicating to the peer that the pool
        is being closed is done through a connection specific process. This
        method just verifies the Runspace Pool is in a state that can be closed
        and that no pipelines are still running.
        """
        if self.pipeline_table:
            raise PSRPCoreError("Must close existing pipelines before closing the pool")

        valid_states = [RunspacePoolState.Closed, RunspacePoolState.Closing, RunspacePoolState.Opened]
        if self.state not in valid_states:
            raise InvalidRunspacePoolState("close Runspace Pool", self.state, valid_states)

        self._change_state(RunspacePoolState.Closed)

    def begin_disconnect(self) -> None:
        """Marks the Runspace Pool to be in the disconnecting phase."""
        self._change_state(RunspacePoolState.Disconnecting)

    def disconnect(self) -> None:
        """Marks the Runspace Pool as disconnected.

        This disconnects the Runspace Pool. COmmunicating to the peer that the
        pool is disconnected is done through a connection specific process.
        """
        valid_states = [RunspacePoolState.Opened, RunspacePoolState.Disconnecting, RunspacePoolState.Disconnected]
        if self.state not in valid_states:
            raise InvalidRunspacePoolState("disconnect a Runspace Pool", self.state, valid_states)

        self._change_state(RunspacePoolState.Disconnected)

    def reconnect(self) -> None:
        """Marks the Runspace Pool as reconnected and opened."""
        valid_states = [RunspacePoolState.Disconnected, RunspacePoolState.Opened]
        if self.state not in valid_states:
            raise InvalidRunspacePoolState("reconnect to a Runspace Pool", self.state, valid_states)

        self._change_state(RunspacePoolState.Opened)

    @property
    def dispatch_policy(
        self,
    ) -> DispatchPolicy:
        """The policy used to pick the next message to send."""
        return self._dispatch_policy

    @dispatch_policy.setter
    def dispatch_policy(
        self,
        value: typing.Union[DispatchPolicy, str],
    ) -> None:
        self._dispatch_policy = DispatchPolicy(value)
        self._prompt_response_burst_used = 0

    @property
    def prompt_response_burst(
        self,
    ) -> int:
        """The number of prompt response messages sent before normal data.

        This only applies when :attr:`dispatch_policy` is set to
        :attr:`DispatchPolicy.prompt_response_first`. A value of ``1``
        alternates between prompt responses and normal data whenever both are
        available.
        """
        return self._prompt_response_burst

    @prompt_response_burst.setter
    def prompt_response_burst(
        self,
        value: int,
    ) -> None:
        if value < 1:
            raise ValueError("prompt_response_burst must be 1 or larger")

        self._prompt_response_burst = value
        self._prompt_response_burst_used = 0

    @property
    def sent_fragments(
        self,
    ) -> typing.List[SentFragment]:
        """The fragments that have been handed to the transport.

        This includes fragments belonging to pipelines that have since been
        cancelled or closed so the identity of data that has already been sent
        can still be accounted for.
        """
        return list(self._sent_fragments)

    def get_sent_fragments(
        self,
        pipeline_id: typing.Optional[uuid.UUID] = None,
    ) -> typing.List[SentFragment]:
        """Get the fragments that have been sent for a pipeline.

        Args:
            pipeline_id: The pipeline to filter on or ``None`` to get the
                fragments for the Runspace Pool itself.

        Returns:
            typing.List[SentFragment]: The fragments that have been sent.
        """
        return [f for f in self._sent_fragments if f.pipeline_id == pipeline_id]

    def clear_sent_fragments(
        self,
    ) -> None:
        """Clear the record of fragments that have been sent.

        The sent fragment record is normally retained so data that has already
        been handed to the transport can be accounted for after a pipeline has
        been cancelled.
        """
        self._sent_fragments.clear()

    def set_dispatch_policy(
        self,
        policy: typing.Union[DispatchPolicy, str],
        prompt_response_burst: typing.Optional[int] = None,
    ) -> None:
        """Set how buffered messages are dispatched.

        Args:
            policy: The dispatch policy to use.
            prompt_response_burst: The number of prompt response messages to
                send before servicing normal data when using the
                ``prompt_response_first`` policy.
        """
        self.dispatch_policy = policy
        if prompt_response_burst is not None:
            self.prompt_response_burst = prompt_response_burst

    def cancel_pipeline(
        self,
        pipeline_id: uuid.UUID,
    ) -> typing.List[PSRPMessage]:
        """Remove the unsent data for a pipeline.

        Any message that has not been fully sent is removed from the send
        buffer. Fragments that have already been sent are still available
        through :attr:`sent_fragments` so they can be accounted for.

        Args:
            pipeline_id: The pipeline whose unsent data should be removed.

        Returns:
            typing.List[PSRPMessage]: The messages that were removed.
        """
        if not isinstance(pipeline_id, uuid.UUID):
            pipeline = getattr(pipeline_id, "pipeline_id", None)
            if not isinstance(pipeline, uuid.UUID):
                raise ValueError("pipeline_id must be a UUID")
            pipeline_id = pipeline

        removed = []
        remaining = []
        for message in self._send_buffer:
            if message.pipeline_id == pipeline_id:
                removed.append(message)
            else:
                remaining.append(message)

        if self._in_flight is not None and self._in_flight.pipeline_id == pipeline_id:
            if self._in_flight not in removed:
                removed.append(self._in_flight)
            self._in_flight = None

        self._send_buffer = remaining
        return removed

    def cancel_pending(
        self,
        pipeline_id: uuid.UUID,
    ) -> typing.List[PSRPMessage]:
        """Alias for :meth:`cancel_pipeline`."""
        return self.cancel_pipeline(pipeline_id)

    def get_pending_messages(
        self,
        pipeline_id: typing.Optional[uuid.UUID] = None,
    ) -> typing.List[PSRPMessage]:
        """Get the messages that have not been fully sent.

        Args:
            pipeline_id: The pipeline to filter on or ``None`` to get the
                messages for the Runspace Pool itself.

        Returns:
            typing.List[PSRPMessage]: The messages still in the send buffer.
        """
        return [m for m in self._send_buffer if m.pipeline_id == pipeline_id]

    def data_to_send(
        self,
        amount: typing.Optional[int] = None,
        policy: typing.Optional[typing.Union[DispatchPolicy, str]] = None,
    ) -> typing.Optional[PSRPPayload]:
        """Gets the next PSRP payload.

        Returns the PSRPPayload that contains the data that needs to be sent
        to the peer. This is a non-blocking call and is used by the
        implementer to get the next PSRP payload that is then sent over it's
        transport.

        The returned payload only contains fragments for a single target
        (pipeline and stream type). When the
        :attr:`DispatchPolicy.prompt_response_first` policy is used messages
        on the ``prompt_response`` stream are picked before normal data but
        normal data is still serviced after at most
        :attr:`prompt_response_burst` prompt response messages.

        Args:
            amount: The maximum size of the data fragment that can be sent.
                This must be 22 or larger to fit the fragment headers.
            policy: Overrides :attr:`dispatch_policy` for this call only.

        Returns:
             typing.Optional[PSRPPayload]: The payload (if any) that needs to
                be sent to the peer.
        """
        if amount is not None and amount < 22:
            raise ValueError("amount must be 22 or larger to fit a PSRP fragment")

        active_policy = self._dispatch_policy if policy is None else DispatchPolicy(policy)

        # The dispatch policy is only evaluated once the previous fragment was
        # an end fragment. While a message is still being fragmented the rest
        # of it is sent first so a message's fragments stay contiguous and the
        # order within a pipeline is preserved.
        if self._in_flight is not None:
            message = self._in_flight
            chose_message = False
        else:
            message = self._select_message(active_policy)
            if message is None:
                return None
            chose_message = True

        current_buffer = bytearray()
        stream_type = message.stream_type
        pipeline_id = message.pipeline_id
        fragment_size = 21
        budget = amount

        # The payload can only contain fragments for a single target. Messages
        # for the selected target are packed in queue order until the target
        # changes or the transport budget is exhausted.
        index = self._send_buffer.index(message)
        while index < len(self._send_buffer):
            message = self._send_buffer[index]

            if message.stream_type != stream_type or message.pipeline_id != pipeline_id:
                break

            if budget is not None and budget < fragment_size:
                break

            message_length = len(message)

            if budget is None:
                allowed_length = message_length
            else:
                allowed_length = budget - fragment_size
                if message_length:
                    if allowed_length <= 0:
                        # Fragment headers must be followed by at least 1 byte
                        # of data to make progress.
                        break
                    allowed_length = min(message_length, allowed_length)
                else:
                    allowed_length = 0

            fragment = message.fragment(allowed_length)
            log.debug(
                "Packing fragment - OID: %s, FID: %s, Start: %s, End: %s, Length: %s",
                fragment.object_id,
                fragment.fragment_id,
                fragment.start,
                fragment.end,
                len(fragment.data),
            )

            current_buffer += create_fragment(fragment.object_id, fragment.fragment_id, fragment.data, fragment.end)
            self._sent_fragments.append(
                SentFragment(
                    fragment.object_id,
                    fragment.fragment_id,
                    pipeline_id,
                    stream_type,
                    fragment.start,
                    fragment.end,
                    len(fragment.data),
                )
            )

            if budget is not None:
                budget -= fragment_size + len(fragment.data)

            if len(message) == 0:
                self._send_buffer.pop(index)
                if self._in_flight is message:
                    self._in_flight = None

                if active_policy == DispatchPolicy.prompt_response_first and any(
                    m.stream_type == StreamType.prompt_response for m in self._send_buffer
                ):
                    # This message ended; let the next call pick the pending
                    # prompt response before packing more normal data.
                    break

                continue

            # Only a partially sent message may be continued on the next call
            # so its remaining fragments are not interleaved with another
            # target.
            self._in_flight = message
            break

        if not current_buffer:
            return None

        if active_policy == DispatchPolicy.prompt_response_first and chose_message:
            if stream_type == StreamType.prompt_response:
                self._prompt_response_burst_used += 1
            else:
                self._prompt_response_burst_used = 0

        return PSRPPayload(bytes(current_buffer), stream_type, pipeline_id)

    def _select_message(
        self,
        policy: typing.Optional[DispatchPolicy] = None,
    ) -> typing.Optional[PSRPMessage]:
        """Pick the next message to dispatch based on the dispatch policy."""
        if not self._send_buffer:
            return None

        if policy is None:
            policy = self._dispatch_policy

        if policy != DispatchPolicy.prompt_response_first:
            return self._send_buffer[0]

        prompt_message = next(
            (m for m in self._send_buffer if m.stream_type == StreamType.prompt_response),
            None,
        )
        if prompt_message is None:
            self._prompt_response_burst_used = 0
            return self._send_buffer[0]

        default_message = next(
            (m for m in self._send_buffer if m.stream_type != StreamType.prompt_response),
            None,
        )
        if default_message is None:
            return prompt_message

        if self._prompt_response_burst_used >= self._prompt_response_burst:
            # Avoid starving normal data by servicing it after a bounded
            # number of prompt response messages.
            return default_message

        return prompt_message

    def receive_data(
        self,
        data: PSRPPayload,
    ) -> None:
        """Store any incoming data.

        Stores any incoming payloads in an internal buffer to be processed.
        This buffer is read when calling :meth:`next_event()`.

        Args:
            data: The PSRP payload data received from the transport.
        """
        self._receive_buffer += data.data

    def next_event(
        self,
    ) -> typing.Optional[PSRPEvent]:
        """Process data received from the peer.

        This processes any PSRP data that has been received from the peer. Will
        return the next PSRP event in the receive buffer or `None` if not
        enough data is available.

        Returns:
            typing.Optional[PSRPEvent]: The next event present in the incoming
                data buffer or `None` if not enough data has been received.
        """
        # First unpacks the raw receive buffer into messages.
        while self._receive_buffer:
            fragment = unpack_fragment(self._receive_buffer)
            log.debug(
                "Unpacked fragment - OID: %s, FID: %s, Start: %s, End: %s, Length: %s",
                fragment.object_id,
                fragment.fragment_id,
                fragment.start,
                fragment.end,
                len(fragment.data),
            )
            self._receive_buffer = self._receive_buffer[21 + len(fragment.data) :]

            buffer = self._incoming_fragments.setdefault(fragment.object_id, [])
            if fragment.fragment_id != len(buffer):
                raise PSRPCoreError(
                    f"Expecting fragment with a fragment id of {len(buffer)} not {fragment.fragment_id}"
                )
            buffer.append(fragment.data)

            if fragment.end:
                raw_message = unpack_message(bytearray(b"".join(buffer)))
                message = PSRPMessage(
                    raw_message.message_type, raw_message.data, raw_message.rpid, raw_message.pid, fragment.object_id
                )
                self._incoming_messages[fragment.object_id] = message
                del self._incoming_fragments[fragment.object_id]

        for object_id in list(self._incoming_messages.keys()):
            message = self._incoming_messages[object_id]
            # In case of a failure it is expected for the client to receive the correct data instead
            del self._incoming_messages[object_id]

            event = self._process_message(message)

            return event

        # Need more data from peer to produce an event.
        return None

    def prepare_message(
        self,
        message: typing.Optional[PSObject],
        message_type: typing.Optional[PSRPMessageType] = None,
        pipeline_id: typing.Optional[uuid.UUID] = None,
        stream_type: StreamType = StreamType.default,
    ) -> None:
        """Adds a PSRP message to send buffer.

        Adds the given PSRP message to the send buffer to be sent when the
        caller requires it to.

        Args:
            message: The PSObject to be send.
            message_type: Override the message type of the PSRP messae in case
                message is not an actual PSRP Message object.
            pipeline_id: The pipeline id the message is for or `None` if it
                targets the runspace pool.
            stream_type: The stream type the message is for.
        """
        if message is None:
            b_data = b""
            message_type = PSRPMessageType.PipelineOutput

        elif isinstance(message, EndOfPipelineInput):
            b_data = b""  # Special edge case for this particular message type
            message_type = PSRPMessageType.EndOfPipelineInput

        else:
            element = serialize(
                message,
                self._cipher,
                # Extra info we pass to the serializer that may adjust how objects are serialized.
                our_capability=self.our_capability,
                their_capability=self.their_capability,
            )
            b_data = ElementTree.tostring(element, encoding="utf-8", method="xml")

            if message_type is None:
                try:
                    message_type = PSRPMessageType.get_message_id(type(message))
                except KeyError:
                    raise ValueError("message_type must be specified when the message is not a PSRP message") from None

        b_msg = create_message(self._is_client, message_type, b_data, self.runspace_pool_id, pipeline_id)

        object_id = self._fragment_counter
        psrp_message = PSRPMessage(
            message_type, bytearray(b_msg), self.runspace_pool_id, pipeline_id, object_id, stream_type
        )

        log.debug(
            "Queuing PSRP Message - MessageType: %s,  RPID: %s,  PID: %s,  OID: %s, Type: %s - '%s'",
            message_type.name,
            self.runspace_pool_id,
            pipeline_id or "''",
            object_id,
            stream_type.name,
            b_data.decode(),
        )
        self._send_buffer.append(psrp_message)

    def _change_state(
        self,
        state: RunspacePoolState,
        error: typing.Optional[ErrorRecord] = None,
    ) -> None:
        log.debug("Runspace Pool State change %s -> %s", self.state.name, state.name)
        self.state = state

    def _process_message(
        self,
        message: PSRPMessage,
    ) -> PSRPEvent:
        """Process a TransportDataAction data message received from a peer."""
        data = message.data
        log.debug(
            "Processing PSRP Message - MessageType: %s,  RPID: %s,  PID: %s,  OID: %s, Type: %s - '%s'",
            message.message_type.name,
            message.runspace_pool_id,
            message.pipeline_id or "''",
            message.object_id,
            message.stream_type.name,
            data.decode(),
        )

        ps_object: typing.Any
        if message.message_type == PSRPMessageType.EndOfPipelineInput:
            # Special edge case for EndOfPipelineInput which has no data.
            ps_object = EndOfPipelineInput()

        elif message.message_type == PSRPMessageType.PipelineOutput and data == b"":
            ps_object = None

        else:
            ps_object = deserialize(
                ElementTree.fromstring(data),
                cipher=self._cipher,
                # Extra info we pass to the serializer that may adjust how objects are serialized.
                our_capability=self.our_capability,
                their_capability=self.their_capability,
            )

        event = PSRPEvent.create(message.message_type, ps_object, message.runspace_pool_id, message.pipeline_id)

        process_func = getattr(self, f"_process_{message.message_type.name}", None)
        if process_func:
            process_func(event)

        else:
            log.warning(f"Received {message.message_type.name} but could not process it")

        return event

    def _process_SessionCapability(
        self,
        event: SessionCapabilityEvent,
    ) -> None:
        # This is the only common message that is processed the same by clients and servers.
        self.their_capability = SessionCapability(
            PSVersion=event.ps_version,
            protocolversion=event.protocol_version,
            SerializationVersion=event.serialization_version,
        )


class Pipeline(typing.Generic[T2]):
    """Pipeline base class.

    This is the base class for a Pipeline. It contains the common attributes
    and methods used by both a client and server base Pipeline.

    Args:
        runspace_pool: The Runspace Pool the pipeline is part of. When
            initialised the pipeline will add itself to the Runspace Pool
            pipeline table.
        pipeline_id: The Pipeline identifier.

    Attributes:
        runspace_pool: See args.
        pipeline_id: See args.
        state: The pipeline state.
    """

    def __new__(
        cls,
        *args: typing.Any,
        **kwargs: typing.Any,
    ) -> "Pipeline":
        if cls == Pipeline:
            raise TypeError(
                f"Type {cls.__qualname__} cannot be instantiated; it can be used only as a base class for "
                f"client/server pipeline types."
            )

        return super().__new__(cls)

    def __init__(
        self,
        runspace_pool: T2,
        pipeline_id: uuid.UUID,
    ) -> None:
        log.debug(
            "Creating pipeline - RPID: %s, PID: %s, Type: %s",
            runspace_pool.runspace_pool_id,
            pipeline_id,
            type(self).__name__,
        )
        self.runspace_pool = runspace_pool
        self.pipeline_id = pipeline_id
        self.state = PSInvocationState.NotStarted
        self.metadata: typing.Optional[PSObject] = None
        runspace_pool.pipeline_table[self.pipeline_id] = self

    def begin_stop(self) -> None:
        """Marks the Pipeline to be in the stopping phase."""
        valid_state = [PSInvocationState.Running, PSInvocationState.Stopping]
        if self.state not in valid_state:
            raise InvalidPipelineState("begin stopping a pipeline", self.state, valid_state)

        self._change_state(PSInvocationState.Stopping)

    def close(self) -> None:
        """Close the Pipeline.

        Closes the pipeline by removing itself from the Runspace Pool pipeline
        table.
        """
        valid_state = [
            PSInvocationState.NotStarted,
            PSInvocationState.Stopped,
            PSInvocationState.Stopping,
            PSInvocationState.Completed,
            PSInvocationState.Failed,
        ]
        if self.state not in valid_state:
            raise InvalidPipelineState("closing a pipeline", self.state, valid_state)
        self.runspace_pool.pipeline_table.pop(self.pipeline_id, None)

    def cancel(self) -> typing.List[PSRPMessage]:
        """Cancel the unsent data for this pipeline.

        Removes any message for this pipeline that has not been fully sent
        from the Runspace Pool send buffer. Data that has already been handed
        to the transport is left in place and its identity can still be
        retrieved through :attr:`RunspacePool.sent_fragments`.

        Returns:
            typing.List[PSRPMessage]: The messages that were removed.
        """
        return self.runspace_pool.cancel_pipeline(self.pipeline_id)

    def cancel_pending(self) -> typing.List[PSRPMessage]:
        """Alias for :meth:`cancel`."""
        return self.cancel()

    def prepare_message(
        self,
        message: typing.Optional[PSObject],
        message_type: typing.Optional[PSRPMessageType] = None,
        stream_type: StreamType = StreamType.default,
    ) -> None:
        """Adds a PSRP message to send buffer.

        Adds the given PSRP message to the send buffer to be sent when the
        caller requires it to. This just calls `prepare_message` on the
        Runspace Pool but for this specific pipeline.

        Args:
            message: The PSObject to be send.
            message_type: Override the message type of the PSRP messae in case
                message is not an actual PSRP Message object.
            stream_type: The stream type the message is for.
        """
        self.runspace_pool.prepare_message(message, message_type, self.pipeline_id, stream_type)

    def _change_state(
        self,
        state: PSInvocationState,
        error: typing.Optional[ErrorRecord] = None,
    ) -> None:
        log.debug("Pipeline State change %s -> %s", self.state.name, state.name)
        self.state = state
