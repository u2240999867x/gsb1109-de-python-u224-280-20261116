# python-mpd2: Python MPD client library
#
# Copyright (C) 2008-2010  J. Alexander Treuman <jat@spatialrift.net>
# Copyright (C) 2010  Jasper St. Pierre <jstpierre@mecheye.net>
# Copyright (C) 2010-2011  Oliver Mader <b52@reaktor42.de>
# Copyright (C) 2016  Robert Niederreiter <rnix@squarewave.at>
#
# python-mpd2 is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# python-mpd2 is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with python-mpd2.  If not, see <http://www.gnu.org/licenses/>.
#
# THIS MODULE IS EXPERIMENTAL. AS SOON AS IT IS CONSIDERED STABLE THIS NOTE
# WILL BE REMOVED. PLEASE REPORT INCONSISTENCIES, BUGS AND IMPROVEMENTS AT
# https://github.com/Mic92/python-mpd2/issues

from __future__ import absolute_import, unicode_literals

import threading
import types
from typing import Any, Callable, List, Mapping, Optional, Protocol, Union, cast

from twisted.internet import defer
from twisted.protocols import basic

from mpd.base import (
    ERROR_PREFIX,
    HELLO_PREFIX,
    NEXT,
    SUCCESS,
    CommandError,
    CommandListError,
    MPDClientBase,
    escape,
    logger,
    mpd_command_provider,
    mpd_commands,
)


def lock(func: Callable) -> Callable:
    def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return func(self, *args, **kwargs)

    return wrapped


class FunctionWithCallable(Protocol):
    callback: Callable

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        pass


def _create_command(
    wrapper: Callable[[Any, str, Any, FunctionWithCallable], Any],
    name: str,
    callback: Callable,
) -> Callable:
    def mpd_command(self: Any, *args: Any) -> Any:
        def func(lines: List[str]) -> Any:
            return callback(self, lines)

        bound_callback = cast(FunctionWithCallable, func)
        bound_callback.callback = callback
        return wrapper(self, name, args, bound_callback)

    return mpd_command


@mpd_command_provider
class MPDProtocol(basic.LineReceiver, MPDClientBase):
    delimiter = b"\n"

    def __init__(
        self, default_idle: bool = True, idle_result: Optional[Callable] = None
    ) -> None:
        super(MPDProtocol, self).__init__()
        # flag whether client should idle by default
        self._default_idle = default_idle
        self.idle_result = idle_result
        self._reset()
        self._lock = threading.RLock()

    def _reset(self) -> None:
        super(MPDProtocol, self)._reset()
        self.mpd_version = None
        self._in_command_list = False
        self._command_list_results: List[List[defer.Deferred]] = []
        self._rcvd_lines: List[str] = []
        self._state: List[List[defer.Deferred]] = []
        self._idle = False

    @classmethod
    def add_command(cls: Any, name: str, callback: Callable) -> None:
        # ignore commands which are implemented on class directly
        if getattr(cls, name, None) is not None:
            return
        # create command and hook it on class
        func = _create_command(cls._execute, name, callback)
        escaped_name = name.replace(" ", "_")
        setattr(cls, escaped_name, func)

    @lock
    def lineReceived(self, line: bytes) -> None:
        line_str = line.decode("utf-8")
        command_list = self._state and isinstance(self._state[0], list)
        state_list = self._state[0] if command_list else self._state
        if line_str.startswith(HELLO_PREFIX):
            self.mpd_version = line_str[len(HELLO_PREFIX) :].strip()
            # default state idle, enter idle
            if self._default_idle:
                self.idle().addCallback(self._dispatch_idle_result)
        elif line_str.startswith(ERROR_PREFIX):
            error = line_str[len(ERROR_PREFIX) :].strip()
            if command_list:
                state_list[0].errback(CommandError(error))
                for state in state_list[1:-1]:
                    state.errback(CommandListError("An earlier command failed."))
                state_list[-1].errback(CommandListError(error))
                del self._state[0]
                del self._command_list_results[0]
            else:
                state_list.pop(0).errback(CommandError(error))
            self._continue_idle()
        elif line_str == SUCCESS or (command_list and line_str == NEXT):
            state_list.pop(0).callback(self._rcvd_lines[:])
            self._rcvd_lines = []
            if command_list and line_str == SUCCESS:
                del self._state[0]
            self._continue_idle()
        else:
            self._rcvd_lines.append(line_str)

    def _lookup_callback(
        self, parser: Union[None, Callable, FunctionWithCallable]
    ) -> Optional[Callable]:
        if hasattr(parser, "callback") and parser is not None:
            return parser.callback
        return parser

    @lock
    def _execute(
        self, command: str, args: List[str] = [], parser: Optional[Callable] = None
    ) -> defer.Deferred:
        # close or kill command in command list not allowed
        if self._in_command_list and self._lookup_callback(parser) is self.NOOP:
            msg = "{} not allowed in command list".format(command)
            raise CommandListError(msg)
        # default state idle and currently in idle state, trigger noidle
        if self._default_idle and self._idle and command != "idle":
            self.noidle().addCallback(self._dispatch_noidle_result)
        # write command to MPD
        self._write_command(command, args)
        # create command related deferred
        deferred = defer.Deferred()
        # extend pending result queue
        state = self._state[-1] if self._in_command_list else self._state
        state.append(deferred)
        # NOOP is for close and kill commands
        if self._lookup_callback(parser) is not self.NOOP:
            # attach command related result parser
            deferred.addCallback(parser)
            # command list, attach handler for collecting command list results
            if self._in_command_list:
                deferred.addCallback(self._parse_command_list_item)
        return deferred

    def _create_command(self, command: str, args: List[str] = []) -> bytes:
        # XXX: this function should be generalized in future. There exists
        #      almost identical code in ``MPDClient._write_command``, with the
        #      difference that it's using ``encode_str`` for text arguments.
        parts = [command]
        for arg in args:
            if type(arg) is tuple:
                if len(arg) == 0:
                    parts.append('":"')
                elif len(arg) == 1:
                    parts.append('"{}:"'.format(int(arg[0])))
                else:
                    parts.append('"{}:{}"'.format(int(arg[0]), int(arg[1])))
            else:
                parts.append('"{}"'.format(escape(arg)))
        return " ".join(parts).encode("utf-8")

    def _write_command(self, command: str, args: List[str] = []) -> None:
        self.sendLine(self._create_command(command, args))

    def _parse_command_list_item(self, result: Any) -> Any:
        if isinstance(result, types.GeneratorType):
            result = list(result)
        self._command_list_results[0].append(result)
        return result

    def _parse_command_list_end(self, lines: List[str]) -> Any:
        return self._command_list_results.pop(0)

    @mpd_commands(*MPDClientBase._parse_nothing.mpd_commands)
    def _parse_nothing(self, lines: List[str]) -> None:
        return None

    def _continue_idle(self) -> None:
        if self._default_idle and not self._idle and not self._state:
            self.idle().addCallback(self._dispatch_idle_result)

    def _do_dispatch(self, result: Any) -> None:
        if self.idle_result:
            self.idle_result(result)
        else:
            res = list(result)
            msg = "MPDProtocol: no idle callback defined: {}".format(res)
            logger.warning(msg)

    def _dispatch_noidle_result(self, result: Any) -> None:
        self._do_dispatch(result)

    def _dispatch_idle_result(self, result: Any) -> None:
        self._idle = False
        self._do_dispatch(result)
        self._continue_idle()

    def idle(self) -> defer.Deferred:
        if self._idle:
            raise CommandError("Already in idle state")
        self._idle = True
        return self._execute("idle", [], self._parse_list)

    def noidle(self) -> defer.Deferred:
        if not self._idle:
            raise CommandError("Not in idle state")
        # delete first pending deferred, idle returns nothing when
        # noidle gets called
        self._state.pop(0)
        self._idle = False
        return self._execute("noidle", [], self._parse_list)

    def command_list_ok_begin(self) -> None:
        if self._in_command_list:
            raise CommandListError("Already in command list")
        if self._default_idle and self._idle:
            self.noidle().addCallback(self._dispatch_noidle_result)
        self._write_command("command_list_ok_begin")
        self._in_command_list = True
        self._command_list_results.append([])
        self._state.append([])

    def command_list_end(self) -> defer.Deferred:
        if not self._in_command_list:
            raise CommandListError("Not in command list")
        self._write_command("command_list_end")
        deferred = defer.Deferred()
        deferred.addCallback(self._parse_command_list_end)
        self._state[-1].append(deferred)
        self._in_command_list = False
        return deferred

    def find_filtered(
        self,
        condition: Any,
        *,
        require_server_only: bool = False,
    ) -> defer.Deferred:
        """Find songs matching *condition*, returning a deferred result.

        The condition is planned for :attr:`mpd_version` exactly as in the
        synchronous client.  The deferred fires with a ``FilterResult``.
        """
        from mpd.filters import compile_filter

        try:
            plan = compile_filter(
                condition,
                self.mpd_version,
                require_server_only=require_server_only,
            )
        except Exception as error:
            return defer.fail(error)

        candidates: List[Mapping[str, Any]] = []
        seen = set()

        def collect(result: Any) -> None:
            if result is None:
                return
            for song in result:
                if not isinstance(song, Mapping) or "file" not in song:
                    continue
                key = str(song["file"])
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(song)

        deferreds = []
        for query in plan.queries:
            method = getattr(self, query.command)
            result = method(*query.args)
            if isinstance(result, defer.Deferred):
                deferreds.append(result)
            else:
                # A test double or subclass may return concrete records.
                collect(result)

        def finish(_: Any) -> Any:
            from mpd.filters import classify_candidates

            return classify_candidates(plan, candidates)

        if not deferreds:
            return defer.succeed(finish(None))

        combined = defer.DeferredList(deferreds, consumeErrors=True)

        def on_results(results: List[Any]) -> Any:
            for success, value in results:
                if success:
                    collect(value)
            return finish(None)

        combined.addCallback(on_results)
        return combined


# vim: set expandtab shiftwidth=4 softtabstop=4 textwidth=79:
