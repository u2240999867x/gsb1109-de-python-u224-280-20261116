"""Asynchronous access to MPD using the asyncio methods of Python 3.

Interaction happens over the mpd.asyncio.MPDClient class, whose connect and
command methods are coroutines.

Some commands (eg. listall) additionally support the asynchronous iteration
(aiter, `async for`) interface; using it allows the library user to obtain
items of result as soon as they arrive.

The .idle() method works differently here: It is an asynchronous iterator that
produces a list of changed subsystems whenever a new one is available. The
MPDClient object automatically switches in and out of idle mode depending on
which subsystems there is currently interest in.

Command lists are currently not supported.


This module requires Python 3.5.2 or later to run.
"""

import asyncio
import warnings
from functools import partial
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Iterable,
    List,
    Optional,
    Set,
    Tuple,
    Union,
    Dict,
    cast,
)

from mpd.base import (
    ERROR_PREFIX,
    SUCCESS,
    BinaryChunk,
    Delivered,
    CommandError,
    CommandListError,
    ConnectionError,
    CallableWithCommands,
)
from mpd.base import MPDClient as SyncMPDClient
from mpd.base import MPDClientBase, ProtocolError, mpd_command_provider


class BaseCommandResult(asyncio.Future):
    """A future that carries its command/args/callback with it for the
    convenience of passing it around to the command queue."""

    def __init__(self, command: str, args: List[str], callback: Callable) -> None:
        super().__init__()
        self._command = command
        self._args = args
        self._callback = callback

    def _feed_line(self, line: Optional[str]) -> None:  # FIXME just inline?
        raise NotImplementedError

    def _feed_error(self, error: Exception) -> None:
        raise NotImplementedError

    async def _feed_from(self, mpdclient: "MPDClient") -> None:
        while True:
            line = await mpdclient._read_line()
            self._feed_line(line)
            if line is None:
                return


class CommandResult(BaseCommandResult):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.__spooled_lines: List[str] = []

    def _feed_line(self, line: Optional[str]) -> None:  # FIXME just inline?
        """Put the given line into the callback machinery, and set the result on a None line."""
        if line is None:
            if self.cancelled():
                # Data was still pulled out of the connection, but the original
                # requester has cancelled the request -- no need to filter the
                # data through the preprocessing callback
                pass
            else:
                self.set_result(self._callback(self.__spooled_lines))
        else:
            self.__spooled_lines.append(line)

    def _feed_error(self, error: Exception) -> None:
        if not self.done():
            self.set_exception(error)
        else:
            # These do occur (especially during the test suite run) when a
            # disconnect was already initialized, but the run task being
            # cancelled has not ever yielded at all and thus still needs to run
            # through to its first await point (which is then in a situation
            # where properties it'd like to access are already cleaned up,
            # resulting in an AttributeError)
            #
            # Rather than quenching them here, they are made visible (so that
            # other kinds of double errors raise visibly, even though none are
            # known right now); instead, the run loop yields initially with a
            # sleep(0) that ensures it can be cancelled properly at any time.
            raise error


class CommandResultIterable(BaseCommandResult):
    """Variant of CommandResult where the underlying callback is an
    asynchronous` generator, and can thus interpret lines as they come along.

    The result can be used with the aiter interface (`async for`). If it is
    still used as a future instead, it eventually results in a list.

    Commands used with this CommandResult must use their passed lines not like
    an iterable (as in the synchronous implementation), but as a asyncio.Queue.
    Furthermore, they must check whether the queue elements are exceptions, and
    raise them.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.__spooled_lines: asyncio.Queue[Union[str, None, Exception]] = (
            asyncio.Queue()
        )

    def _feed_line(self, line: Union[str, None]) -> None:
        self.__spooled_lines.put_nowait(line)

    def _feed_error(self, error: Exception) -> None:
        self.__spooled_lines.put_nowait(error)

    def __await__(self) -> Any:
        asyncio.Task(self.__feed_future())
        return super().__await__()

    __iter__ = __await__  # for 'yield from' style invocation

    async def __feed_future(self) -> None:
        result = []
        try:
            async for r in self:
                result.append(r)
        except Exception as e:
            self.set_exception(e)
        else:
            if not self.cancelled():
                self.set_result(result)

    def __aiter__(self) -> "Any":
        if self.done():
            raise RuntimeError("Command result is already being consumed")
        return self._callback(self.__spooled_lines).__aiter__()


class BinaryStreamResult(asyncio.Future):
    """Command-queue entry that feeds one binary response into a stream.

    The run loop calls :meth:`_feed_from`; payload pieces are pushed to the
    stream's queue as they arrive rather than after the whole image has been
    read.
    """

    def __init__(self, stream: "AsyncBinaryStream") -> None:
        super().__init__()
        self._stream = stream

    async def _feed_from(self, mpdclient: "MPDClient") -> None:
        try:
            await self._stream._read_into_queue(mpdclient)
        except CommandError as error:
            self._stream._phase = "idle"
            if self._stream._boundary_unknown:
                # The payload of the current response has not been consumed.
                self._stream._abandon()
                mpdclient.disconnect()
            self._stream._push_error(error)

    def _feed_error(self, error: Exception) -> None:
        self._stream._phase = "idle"
        self._stream._push_error(error)


class AsyncBinaryStream:
    """Asynchronous incremental variant of :class:`mpd.base.BinaryStream`.

    Use it with ``async for``::

        async with client.albumart_stream(uri) as stream:
            async for chunk in stream:
                consume(chunk.data, chunk.offset, chunk.total)

    Pieces are delivered while a server response is still being read.  The
    announced payload length is always consumed exactly, so arbitrary binary
    data (including newlines and ``OK``-looking bytes) is never misinterpreted.

    Leaving the loop (or cancelling) does not corrupt the connection: the run
    loop keeps draining the response in flight before the next command, and a
    partially delivered image is never reported as complete.  If the boundary
    cannot be established, the connection is disconnected.
    """

    _END = object()

    def __init__(
        self,
        client: "MPDClient",
        command: str,
        uri: str,
        chunk_size: Optional[int] = None,
    ) -> None:
        if chunk_size is not None and chunk_size <= 0:
            raise ValueError("chunk_size must be greater than zero")
        self._client = client
        self._command = command
        self._uri = uri
        self._chunk_size = chunk_size or 65536
        self._offset = 0
        self._read_offset = 0
        self._total: Optional[int] = None
        self._metadata: Optional[Dict[str, Any]] = None
        self._phase = "idle"
        self._payload_remaining = 0
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._closed = False
        self._abandoned = False
        self._no_picture = False
        self._boundary_unknown = False
        if not client.connected:
            raise ConnectionError("Can not send command to disconnected client")
        client._begin_binary_stream(self)  # type: ignore[arg-type]

    @property
    def uri(self) -> str:
        return self._uri

    @property
    def offset(self) -> int:
        """Number of bytes yielded so far (the end of :attr:`delivered`)."""
        return self._offset

    @property
    def delivered(self) -> Delivered:
        """The range yielded so far: ``(0, offset)``."""
        return Delivered(0, self._offset)

    @property
    def delivered_range(self) -> range:
        return range(0, self._offset)

    @property
    def delivered_length(self) -> int:
        return self._offset

    delivered_bytes = delivered_length

    @property
    def total(self) -> Optional[int]:
        return self._total

    @property
    def declared_total(self) -> Optional[int]:
        return self._total

    total_length = declared_total
    declared_length = declared_total
    size = declared_total

    @property
    def remaining(self) -> Optional[int]:
        if self._total is None:
            return None
        return max(0, self._total - self._offset)

    @property
    def metadata(self) -> Dict[str, Any]:
        return dict(self._metadata or {})

    @property
    def type(self) -> Optional[str]:
        value = (self._metadata or {}).get("type")
        return value if isinstance(value, str) else None

    @property
    def mime_type(self) -> Optional[str]:
        return self.type

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def has_picture(self) -> bool:
        return self._total is not None

    @property
    def complete(self) -> bool:
        return (
            self._closed
            and not self._abandoned
            and self._total is not None
            and self._offset >= self._total
        )

    def __aiter__(self) -> "AsyncBinaryStream":
        return self

    async def __anext__(self) -> BinaryChunk:
        try:
            while True:
                if self._queue.empty():
                    if self._closed:
                        raise StopAsyncIteration
                    if self._phase == "idle":
                        if self._total is not None and self._offset >= self._total:
                            self._finish()
                            raise StopAsyncIteration
                        await self._issue()
                item = await self._queue.get()
                if item is self._END:
                    if self._no_picture:
                        self._finish()
                        raise StopAsyncIteration
                    if self._total is not None and self._offset >= self._total:
                        self._finish()
                        raise StopAsyncIteration
                    continue
                if isinstance(item, BaseException):
                    self._finish()
                    raise item
                self._offset = max(self._offset, item.end)
                return item
        except asyncio.CancelledError:
            # The run loop keeps draining the response in flight; only the
            # completeness claim is dropped.  This is deliberately synchronous
            # so cancellation completes immediately.
            self.close()
            raise

    async def _issue(self) -> None:
        result = BinaryStreamResult(self)
        self._phase = "reading"
        self._client._queue_binary_stream(
            result, self._command, self._uri, self._read_offset
        )

    async def _read_into_queue(self, mpdclient: "MPDClient") -> None:
        """Read exactly one response, yielding payload pieces as they arrive."""
        metadata: Dict[str, Any] = {}
        length: Optional[int] = None
        while True:
            line = await mpdclient._read_line()
            if line is None:
                break
            key, value = mpdclient._parse_pair(line, ": ")
            if key == "binary":
                length = int(value)
                break
            metadata[key] = value

        if length is None:
            if self._metadata is None and not metadata:
                # readpicture: the song exists but has no picture.
                self._phase = "idle"
                self._no_picture = True
                self._queue.put_nowait(self._END)
                return
            raise CommandError("Binary field vanished during binary transfer")

        if self._metadata is None:
            self._metadata = metadata
            value = metadata.get("size")
            if value is None:
                # Older MPD versions return the whole image in one response
                # without a size header; the announced payload is the total.
                self._total = self._read_offset + length
            else:
                try:
                    self._total = int(value)
                except (TypeError, ValueError):
                    self._boundary_unknown = True
                    raise CommandError("Size data unsuitable for binary transfer")
                if self._total < 0:
                    self._boundary_unknown = True
                    raise CommandError("Size data unsuitable for binary transfer")
        elif metadata != self._metadata:
            self._boundary_unknown = True
            raise CommandError("Metadata of binary data changed during transfer")

        if self._total is not None and self._read_offset + length > self._total:
            self._boundary_unknown = True
            raise CommandListError("Binary data announced size exceeded")
        if (
            self._total is not None
            and self._read_offset + length < self._total
            and length == 0
        ):
            self._boundary_unknown = True
            raise CommandError("Binary transfer made no progress")

        remaining = length
        while remaining > 0:
            amount = min(self._chunk_size, remaining)
            data = await mpdclient._read_chunk(amount)
            if len(data) != amount:
                raise ConnectionError(
                    "Connection lost while reading binary data: "
                    "expected %d bytes, got %d" % (amount, len(data))
                )
            start = self._read_offset
            self._read_offset += amount
            remaining -= amount
            if not self._abandoned:
                self._queue.put_nowait(self._make_chunk(data, start, self._total))

        # newline after the binary payload, then the terminating OK line
        if await mpdclient._read_chunk(1) != b"\n":
            raise ConnectionError("Connection lost while reading binary line")
        while True:
            line = await mpdclient._read_line()
            if line is None:
                break

        if length == 0 and not self._has_yielded_payload():
            if not self._abandoned:
                self._queue.put_nowait(
                    self._make_chunk(b"", self._read_offset, self._total)
                )
        self._phase = "idle"
        if self._total is None:
            # No size header: this single response is the whole image.
            self._total = self._read_offset
        if not self._abandoned:
            self._queue.put_nowait(self._END)

    def _has_yielded_payload(self) -> bool:
        return self._read_offset > 0

    def _make_chunk(
        self, data: bytes, offset: int, total: Optional[int]
    ) -> BinaryChunk:
        metadata = dict(self._metadata or {})
        metadata.pop("binary", None)
        mime_type = metadata.get("type")
        if not isinstance(mime_type, str):
            mime_type = None
        return BinaryChunk(
            data,
            offset,
            total,
            mime_type=mime_type,
            metadata=metadata,
            complete=total is not None and offset + len(data) >= total,
        )

    def _push_error(self, error: Exception) -> None:
        self._queue.put_nowait(error)

    def _finish(self) -> None:
        self._closed = True
        self._abandoned = False
        self._client._end_binary_stream(self)  # type: ignore[arg-type]

    def _abandon(self) -> None:
        self._closed = True
        self._abandoned = True
        while not self._queue.empty():
            self._queue.get_nowait()
        # Release a consumer that is currently waiting for the next piece.
        self._queue.put_nowait(self._END)
        self._client._end_binary_stream(self)  # type: ignore[arg-type]

    async def aclose(self) -> None:
        """Stop delivering chunks.

        The run loop drains any response still in flight before the next
        command is processed, so the connection stays reusable.  Pending
        payload pieces are discarded and the image is not reported complete.
        """
        if not self._closed:
            self._abandon()

    def close(self) -> None:
        if not self._closed:
            self._abandon()

    async def __aenter__(self) -> "AsyncBinaryStream":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()

    def __del__(self) -> None:
        try:
            if not self._closed:
                self._abandon()
        except Exception:
            pass


@mpd_command_provider
class MPDClient(MPDClientBase):
    __run_task = None  # doubles as indicator for being connected

    #: Indicator of whether there is a pending idle command that was not terminated yet.
    # When in doubt; this is True, thus erring at the side of caution (because
    # a "noidle" being sent while racing against an incoming idle notification
    # does no harm)
    __in_idle = False

    #: Indicator that the last attempted idle failed.
    #
    # When set, IMMEDIATE_COMMAND_TIMEOUT is ignored in favor of waiting until
    # *something* else happens, and only then retried.
    #
    # Note that the only known condition in which this happens is when between
    # start of the connection and the presentation of credentials, more than
    # IMMEDIATE_COMMAND_TIMEOUT passes.
    __idle_failed = False

    #: Seconds after a command's completion to send idle. Setting this too high
    # causes "blind spots" in the client's view of the server, setting it too
    # low sends needless idle/noidle after commands in quick succession.
    IMMEDIATE_COMMAND_TIMEOUT = 0.1

    #: FIFO list of processors that may consume the read stream one after the
    # other
    #
    # As we don't have any other form of backpressure in the sending side
    # (which is not expected to be limited), its limit of COMMAND_QUEUE_LENGTH
    # serves as a limit against commands queuing up indefinitely. (It's not
    # *directly* throttling output, but as the convention is to put the
    # processor on the queue and then send the command, and commands are of
    # limited size, this is practically creating backpressure.)
    __command_queue: Optional[
        "asyncio.Queue[Union[BaseCommandResult, BinaryStreamResult]]"
    ] = None

    #: Construction size of __command_queue. The default limit is high enough
    # that a client can easily send off all existing commands simultaneously
    # without needlessly blocking the TCP flow, but small enough that
    # freespinning tasks create warnings.
    COMMAND_QUEUE_LENGTH = 128

    #: Callbacks registered by any current callers of `idle()`.
    #
    # The first argument lists the changes that the caller is interested in
    # (and all current listeners' union is used to populate the `idle`
    # command's arguments), the latter is an actual callback that will be
    # passed either a set of changes or an exception.
    __idle_consumers: Optional[
        List[
            Tuple[
                Union[List[str], Tuple[str]],
                Callable[[Union[List[str], Exception]], None],
            ]
        ]
    ] = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.__rfile: Optional[asyncio.StreamReader] = None
        self.__wfile: Optional[asyncio.StreamWriter] = None

    async def connect(
        self,
        host: str,
        port: int = 6600,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        if loop is not None:
            warnings.warn(
                "loop passed into MPDClient.connect is ignored, this will become an error",
                DeprecationWarning,
            )
        if host.startswith("@"):
            host = "\0" + host[1:]
        if host.startswith("\0") or "/" in host:
            r, w = await asyncio.open_unix_connection(host)
        else:
            r, w = await asyncio.open_connection(host, port)
        self.__rfile, self.__wfile = r, w

        self.__command_queue = asyncio.Queue(maxsize=self.COMMAND_QUEUE_LENGTH)
        self.__idle_consumers = []

        try:
            helloline = await asyncio.wait_for(self.__readline(), timeout=5)
        except asyncio.TimeoutError:
            self.disconnect()
            raise ConnectionError("No response from server while reading MPD hello")
        # FIXME should be reusable w/o reaching in
        SyncMPDClient._hello(cast(SyncMPDClient, self), helloline)

        self.__run_task = asyncio.Task(self.__run())

    @property
    def connected(self) -> bool:
        return self.__run_task is not None

    def disconnect(self) -> None:
        if (
            self.__run_task is not None
        ):  # is None eg. when connection fails in .connect()
            self.__run_task.cancel()
        if self.__wfile is not None:
            self.__wfile.close()
        stream = self._binary_stream
        if stream is not None:
            stream.close()
        self.__rfile = self.__wfile = None
        self.__run_task = None
        self.__command_queue = None
        if self.__idle_consumers is not None:
            # copying the list as each raising callback will remove itself from __idle_consumers
            for subsystems, callback in list(self.__idle_consumers):
                callback(ConnectionError())
        self.__idle_consumers = None

    def _get_idle_interests(self) -> Optional[Set[str]]:
        """Accumulate a set of interests from the current __idle_consumers.
        Returns the union of their subscribed subjects, [] if at least one of
        them is the empty catch-all set, or None if there are no interests at
        all."""

        if not self.__idle_consumers:
            return None
        if any(len(s) == 0 for (s, c) in self.__idle_consumers):
            return set()
        return set.union(*(set(s) for (s, c) in self.__idle_consumers))

    def _end_idle(self) -> None:
        """If the main task is currently idling, make it leave idle and process
        the next command (if one is present) or just restart idle"""

        if self.__in_idle:
            self.__write("noidle\n")
            self.__in_idle = False

    async def __run(self) -> None:
        # See CommandResult._feed_error documentation
        await asyncio.sleep(0)

        try:
            while True:
                try:
                    if self.__command_queue is None:
                        raise ConnectionError("Disconnected while waiting for command")
                    result = await asyncio.wait_for(
                        self.__command_queue.get(),
                        timeout=self.IMMEDIATE_COMMAND_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    # The cancellation of the __command_queue.get() that happens
                    # in this case is intended, and is just what asyncio.Queue
                    # suggests for "get with timeout".

                    if (
                        self.__command_queue is not None
                        and not self.__command_queue.empty()
                    ):
                        # A __command_queue.put() has happened after the
                        # asyncio.wait_for() timeout but before execution of
                        # this coroutine resumed. Looping around again will
                        # fetch the new entry from the queue.
                        continue

                    if self.__idle_failed:
                        # We could try for a more elaborate path where we now
                        # await the command queue indefinitely, but as we're
                        # already in an error case, this whole situation only
                        # persists until the error is processed somewhere else,
                        # so ticking once per idle timeout is OK to keep things
                        # simple.
                        continue

                    subsystems = self._get_idle_interests()
                    if subsystems is None:
                        # The presumably most quiet subsystem -- in this case,
                        # idle is only used to keep the connection alive.
                        subsystems = set(["database"])

                    # Careful: There can't be any await points between the
                    # except and here, or the sequence between the idle and the
                    # command processor might be wrong.
                    result = CommandResult("idle", subsystems, self._parse_list)
                    result.add_done_callback(self.__idle_result)
                    self.__in_idle = True
                    self._write_command(result._command, result._args)

                # A new command was issued, so there's a chance that whatever
                # made idle fail is now fixed.
                self.__idle_failed = False

                try:
                    await result._feed_from(self)
                except CommandError as e:
                    result._feed_error(e)
                    # This kind of error we can tolerate without breaking up
                    # the connection; any other would fly out, be reported
                    # through the result and terminate the connection

        except Exception as e:
            # Pass exception to any pending task to terminate them. Otherwise they will hang
            # indefinitely as we are about to disconnect.
            try:
                while (
                    self.__command_queue is not None
                    and not self.__command_queue.empty()
                ):
                    pending_result = self.__command_queue.get_nowait()
                    pending_result._feed_error(e)
            except asyncio.QueueEmpty:
                # As per documentation, the queue raises this type of exception when get_nowait()
                # is called and the queue is empty. It actually rather block on the get_nowait() call
                # but let's leave the except just in case.
                pass

            # Prevent the destruction of the pending task in the shutdown
            # function -- it's just shutting down by itself.
            self.__run_task = None
            self.disconnect()

            if result is not None:
                # The last command has failed: Forward that result.
                #
                # (In idle, that's fine too -- everyone watching see a
                # nonspecific event).
                result._feed_error(e)
                return
            else:
                raise
                # Typically this is a bug in mpd.asyncio.

    def __idle_result(self, result: BaseCommandResult) -> None:
        try:
            idle_changes = result.result()
        except CommandError:
            # Don't retry until something changed
            self.__idle_failed = True

            # Not raising this any further: The callbacks are notified that
            # "something is up" (which is all their API gives), and whichever
            # command is issued to act on it will hopefully run into the same
            # condition.
            #
            # This does swallow the exact error cause.

            idle_changes = set()
            if self.__idle_consumers is not None:
                for subsystems, _ in self.__idle_consumers:
                    idle_changes = idle_changes.union(subsystems)

        # make generator accessible multiple times
        idle_changes = list(idle_changes)

        if self.__idle_consumers is not None:
            for subsystems, callback in self.__idle_consumers:
                if not subsystems or any(s in subsystems for s in idle_changes):
                    callback(idle_changes)

    # helper methods

    async def __readline(self) -> str:
        """Wrapper around .__rfile.readline that handles encoding"""
        if self.__rfile is None:
            raise ConnectionError("Can not read from a disconnected client")
        data = await self.__rfile.readline()
        try:
            return data.decode("utf8")
        except UnicodeDecodeError:
            self.disconnect()
            raise ProtocolError("Invalid UTF8 received")

    async def _read_chunk(self, length: int) -> bytes:
        if self.__rfile is None:
            raise ConnectionError("Can not read from a disconnected client")
        try:
            return await self.__rfile.readexactly(length)
        except asyncio.IncompleteReadError:
            raise ConnectionError("Connection lost while reading binary")

    def __write(self, text: str) -> None:
        """Wrapper around .__wfile.write that handles encoding."""
        if self.__wfile is None:
            raise ConnectionError("Can not write to a disconnected client")
        self.__wfile.write(text.encode("utf8"))

    # copied and subtly modifiedstuff from base

    # This is just a wrapper for the below.
    def _write_line(self, text: str) -> None:
        self.__write(text + "\n")

    def _write_command(self, command: str, args: List[Any]) -> None:
        # FIXME This code should be shareable.
        SyncMPDClient._write_command(cast(SyncMPDClient, self), command, args)

    async def _read_line(self) -> Optional[str]:
        line = await self.__readline()
        if not line.endswith("\n"):
            raise ConnectionError("Connection lost while reading line")
        line = line.rstrip("\n")
        if line.startswith(ERROR_PREFIX):
            error = line[len(ERROR_PREFIX) :].strip()
            raise CommandError(error)
        if line == SUCCESS:
            return None
        return line

    async def _parse_objects_direct(  # type: ignore
        self,
        lines: "asyncio.Queue[str]",
        delimiters: List[str] = [],
        lookup_delimiter: bool = False,
    ) -> AsyncIterator[Dict[str, str]]:
        obj: Dict[str, Any] = {}
        while True:
            line = await lines.get()
            if isinstance(line, BaseException):
                raise line
            if line is None:
                break
            key, value = self._parse_pair(line, separator=": ")
            key = key.lower()
            if lookup_delimiter and not delimiters:
                delimiters = [key]
            if obj:
                if key in delimiters:
                    yield obj
                    obj = {}
                elif key in obj:
                    if not isinstance(obj[key], list):
                        obj[key] = [obj[key], value]
                    else:
                        obj[key].append(value)
                    continue
            obj[key] = value
        if obj:
            yield obj

    async def _execute_binary(
        self, command: str, args: Iterable[Any]
    ) -> Dict[str, Union[str, bytes]]:
        # Fetching data in lockstep leaves the command queue empty so that
        # more time critical commands can be executed right away.
        args = list(args)
        assert len(args) == 1
        stream = AsyncBinaryStream(self, command, args[0])
        data: Optional[bytes] = None
        if args[0] is None:
            raise ValueError("uri must not be None")
        try:
            async for chunk in stream:
                if data is None:
                    data = chunk.data
                else:
                    data += chunk.data
        finally:
            await stream.aclose()
        metadata = stream.metadata
        metadata.pop("size", None)
        if data is not None:
            metadata["binary"] = data
        return metadata

    def _queue_binary_stream(
        self,
        result: BinaryStreamResult,
        command: str,
        uri: str,
        offset: int,
    ) -> None:
        if self.__command_queue is None:
            raise ConnectionError("Can not send command to disconnected client")
        try:
            self.__command_queue.put_nowait(result)
        except asyncio.QueueFull:
            raise ConnectionError(
                "Command queue overflowing while starting a binary stream"
            )
        self._end_idle()
        # Careful: There can't be any await points between the queue
        # appending and the write.
        try:
            self._write_command(command, [uri, offset])
        except BaseException:
            self.disconnect()
            raise

    # command provider interface
    def albumart_stream(
        self, uri: str, chunk_size: Optional[int] = None
    ) -> "AsyncBinaryStream":
        """Return album art for *uri* as an asynchronous chunk iterator.

        Each iteration yields a :class:`mpd.base.BinaryChunk` carrying the
        delivered byte range and MPD's declared total length.  Pieces are
        delivered while the server response is still being read; leaving the
        loop early keeps the connection reusable and never marks the image
        complete.
        """
        return AsyncBinaryStream(self, "albumart", uri, chunk_size)

    def readpicture_stream(
        self, uri: str, chunk_size: Optional[int] = None
    ) -> "AsyncBinaryStream":
        """Return the embedded picture for *uri* as an async chunk iterator."""
        return AsyncBinaryStream(self, "readpicture", uri, chunk_size)

    # Convenience aliases for the chunked entry points.
    def binary_stream(
        self, command: str, uri: str, chunk_size: Optional[int] = None
    ) -> "AsyncBinaryStream":
        """Async chunked iterator for a binary command."""
        return AsyncBinaryStream(self, command, uri, chunk_size)

    albumart_chunks = albumart_stream
    albumart_iter = albumart_stream
    iter_albumart = albumart_stream
    stream_albumart = albumart_stream
    readpicture_chunks = readpicture_stream
    readpicture_iter = readpicture_stream
    iter_readpicture = readpicture_stream
    stream_readpicture = readpicture_stream

    @classmethod
    def add_command(cls: Any, name: str, callback: CallableWithCommands) -> None:
        if callback.mpd_commands_binary:

            async def async_func(self: Any, *args: Any) -> BaseCommandResult:
                result = await self._execute_binary(name, args)

                # With binary, the callback is applied to the final result
                # rather than to the iterator over the lines (cf.
                # MPDClient._execute_binary)
                return callback(self, result)

            escaped_name = name.replace(" ", "_")
            async_func.__name__ = escaped_name
            setattr(cls, escaped_name, async_func)
        else:
            command_class = (
                CommandResultIterable if callback.mpd_commands_direct else CommandResult
            )
            if hasattr(cls, name):
                # Idle and noidle are explicitly implemented, skipping them.
                return

            def sync_func(self: Any, *args: Any) -> BaseCommandResult:
                result = command_class(name, args, partial(callback, self))
                if self.__run_task is None:
                    raise ConnectionError("Can not send command to disconnected client")

                try:
                    self.__command_queue.put_nowait(result)
                except asyncio.QueueFull as e:
                    e.args = (
                        "Command queue overflowing; this indicates the"
                        " application sending commands in an uncontrolled"
                        " fashion without awaiting them, and typically"
                        " indicates a memory leak.",
                    )
                    # While we *could* indicate to the queued result that it has
                    # yet to send its request, that'd practically create a queue of
                    # awaited items in the user application that's growing
                    # unlimitedly, eliminating any chance of timely responses.
                    # Furthermore, the author sees no practical use case that's not
                    # violating MPD's guidance of "Do not manage a client-side copy
                    # of MPD's database". If a use case *does* come up, any change
                    # would need to maintain the property of providing backpressure
                    # information. That would require an API change.
                    raise

                self._end_idle()
                # Careful: There can't be any await points between the queue
                # appending and the write
                try:
                    self._write_command(result._command, result._args)
                except BaseException as e:
                    self.disconnect()
                    result.set_exception(e)
                return result

            escaped_name = name.replace(" ", "_")
            sync_func.__name__ = escaped_name
            setattr(cls, escaped_name, sync_func)

    # commands that just work differently
    async def idle(
        self, subsystems: Union[List[str], Tuple[str]] = []
    ) -> AsyncIterator[Union[List[str], Exception]]:
        if self.__idle_consumers is None:
            raise ConnectionError("Can not start idle on a disconnected client")

        interests_before = self._get_idle_interests()
        # A queue accepting either a list of things that changed in a single
        # idle cycle, or an exception to be raised
        changes: asyncio.Queue[Union[List[str], Exception]] = asyncio.Queue()
        try:
            entry = (subsystems, changes.put_nowait)
            self.__idle_consumers.append(entry)
            if self._get_idle_interests != interests_before:
                # Technically this does not enter idle *immediately* but rather
                # only after any commands after IMMEDIATE_COMMAND_TIMEOUT;
                # practically that should be a good thing.
                self._end_idle()
            while True:
                item = await changes.get()
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            if self.__idle_consumers is not None:
                self.__idle_consumers.remove(entry)

    def noidle(self) -> None:
        raise AttributeError("noidle is not supported / required in mpd.asyncio")
