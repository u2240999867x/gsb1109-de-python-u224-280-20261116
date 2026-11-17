# -*- coding: utf-8 -*-
# Copyright: (c) 2021, Jordan Borean (@jborean93) <jborean93@gmail.com>
# MIT License (see LICENSE or https://opensource.org/licenses/MIT)

"""In-memory replay of PSRP host RawUI calls.

The RawUI calls received over PSRP only describe the mutations a peer applied
to its screen buffer. This module turns that ordered stream of calls back into
a queryable snapshot of what the peer's screen buffer looked like.

The replay is purely in memory and makes no assumptions about anything that was
not observed:

* A cell that no call ever touched is :data:`None`, not a guess of a blank
  cell.
* The screen buffer is tracked separately from the visible window. A cell can
  exist in the buffer while never being visible through the window.
* Calls that cannot be modelled from the information available over PSRP (for
  example the higher level ``Write``/``WriteLine`` calls whose wrapping and
  scrolling behaviour is host specific) are recorded in :attr:`RawUIReplay.gaps`
  rather than guessed at.

Every cell that was produced by a call keeps a reference to that call through
:class:`ReplayCell`, so both the character/colors and their originating call can
be queried.
"""

import typing

from psrpcore.types import (
    BufferCell,
    BufferCellType,
    ConsoleColor,
    Coordinates,
    HostMethodIdentifier,
    Rectangle,
    Size,
)


class ReplayCell:
    """A single observed screen buffer cell.

    This wraps the :class:`psrpcore.types.BufferCell` produced by a RawUI call
    together with the call that wrote it. Instances are immutable - mutating
    operations on :class:`RawUIReplay` always create new cell instances instead
    of modifying existing ones so a cell can never be shared between two buffer
    locations.

    Args:
        cell: The buffer cell contents.
        source_call: The sequence number of the call that wrote the cell.
    """

    def __init__(
        self,
        cell: BufferCell,
        source_call: int,
    ) -> None:
        self.cell = cell
        self.source_call = source_call

    @property
    def character(self) -> int:
        """The UTF-16 codepoint of the cell character."""
        return int(self.cell.Character)

    @property
    def foreground_color(self) -> ConsoleColor:
        """The cell foreground color."""
        return self.cell.ForegroundColor

    @property
    def background_color(self) -> ConsoleColor:
        """The cell background color."""
        return self.cell.BackgroundColor

    @property
    def buffer_cell_type(self) -> BufferCellType:
        """Whether the cell is complete, leading, or trailing."""
        return self.cell.BufferCellType

    def __eq__(
        self,
        other: typing.Any,
    ) -> bool:
        if not isinstance(other, ReplayCell):
            return NotImplemented

        return (
            self.character == other.character
            and self.foreground_color == other.foreground_color
            and self.background_color == other.background_color
            and self.buffer_cell_type == other.buffer_cell_type
            and self.source_call == other.source_call
        )

    def __hash__(self) -> int:
        return hash(
            (
                self.character,
                self.foreground_color,
                self.background_color,
                self.buffer_cell_type,
                self.source_call,
            )
        )

    def __repr__(self) -> str:
        return (
            f"<ReplayCell character={self.character!r} foreground={self.foreground_color!s} "
            f"background={self.background_color!s} source_call={self.source_call}>"
        )


class RawUIGap:
    """A RawUI call the replay cannot model.

    Some host calls do not carry enough information to reconstruct their effect
    on the screen buffer. The call is not discarded - its identifier,
    parameters, and sequence number are retained so callers can decide what to
    do with the information instead of the replay pretending nothing happened.

    Args:
        call_id: The sequence number assigned to the call.
        method: The host method that could not be replayed.
        parameters: The decoded parameters the call was invoked with.
        reason: Why the call could not be applied to the replay.
    """

    def __init__(
        self,
        call_id: int,
        method: HostMethodIdentifier,
        parameters: typing.List[typing.Any],
        reason: str,
    ) -> None:
        self.call_id = call_id
        self.method = method
        self.parameters = parameters
        self.reason = reason

    def __repr__(self) -> str:
        return f"<RawUIGap call_id={self.call_id} method={self.method!s} reason={self.reason!r}>"


# Methods that map directly onto the screen buffer and are fully replayed.
_BUFFER_METHODS = frozenset(
    [
        HostMethodIdentifier.SetBufferContents1,
        HostMethodIdentifier.SetBufferContents2,
        HostMethodIdentifier.ScrollBufferContents,
    ]
)

# Methods that change the buffer or window geometry without touching cells.
_GEOMETRY_METHODS = frozenset(
    [
        HostMethodIdentifier.SetBufferSize,
        HostMethodIdentifier.SetWindowSize,
        HostMethodIdentifier.SetWindowPosition,
    ]
)

# Methods that have no screen buffer effect on their own.
_NOOP_METHODS = frozenset(
    [
        HostMethodIdentifier.SetForegroundColor,
        HostMethodIdentifier.SetBackgroundColor,
        HostMethodIdentifier.SetCursorPosition,
        HostMethodIdentifier.SetCursorSize,
        HostMethodIdentifier.SetWindowTitle,
        HostMethodIdentifier.FlushInputBuffer,
    ]
)

# The higher level UI calls. Their screen effect depends on host wrapping,
# scrolling, and cursor behaviour that is not described in the PSRP call.
_UNSUPPORTED_METHODS = frozenset(
    [
        HostMethodIdentifier.Write1,
        HostMethodIdentifier.Write2,
        HostMethodIdentifier.WriteLine1,
        HostMethodIdentifier.WriteLine2,
        HostMethodIdentifier.WriteLine3,
        HostMethodIdentifier.WriteErrorLine,
        HostMethodIdentifier.WriteDebugLine,
        HostMethodIdentifier.WriteVerboseLine,
        HostMethodIdentifier.WriteWarningLine,
        HostMethodIdentifier.WriteProgress,
    ]
)


class RawUIReplay:
    """Replay RawUI host calls into a queryable screen buffer.

    Feed every RawUI host call (as exposed by
    :attr:`psrpcore.PSRPEvent.method_identifier` and
    :attr:`psrpcore.PSRPEvent.method_parameters`) into :meth:`record` in the
    order they were received. The replay keeps track of:

    * ``buffer_size`` - the current screen buffer dimensions.
    * ``window_position`` and ``window_size`` - the visible window within that
      buffer.
    * The observed cells of the buffer, keyed by their ``(x, y)`` coordinates.
      Missing keys are cells that were never observed and remain unknown.

    Geometry defaults to ``None`` until the first :class:`Size` or
    :class:`Coordinates` is observed for that value - no initial blank buffer is
    assumed.

    Args:
        buffer_size: The initial buffer size, if already known.
        window_size: The initial window size, if already known.
        window_position: The initial window position, if already known.
    """

    def __init__(
        self,
        buffer_size: typing.Optional[Size] = None,
        window_size: typing.Optional[Size] = None,
        window_position: typing.Optional[Coordinates] = None,
    ) -> None:
        self._buffer_size = Size(buffer_size.Width, buffer_size.Height) if buffer_size else None
        self._window_size = Size(window_size.Width, window_size.Height) if window_size else None
        self._window_position = (
            Coordinates(window_position.X, window_position.Y) if window_position else None
        )
        self._cells: typing.Dict[typing.Tuple[int, int], ReplayCell] = {}
        self._gaps: typing.List[RawUIGap] = []
        self._call_count = 0

    @property
    def buffer_size(self) -> typing.Optional[Size]:
        """The current buffer size, or :data:`None` if never observed."""
        return self._buffer_size

    @property
    def window_size(self) -> typing.Optional[Size]:
        """The current window size, or :data:`None` if never observed."""
        return self._window_size

    @property
    def window_position(self) -> typing.Optional[Coordinates]:
        """The current window position, or :data:`None` if never observed."""
        return self._window_position

    @property
    def cells(self) -> typing.Dict[typing.Tuple[int, int], ReplayCell]:
        """A copy of every observed cell keyed by its ``(x, y)`` coordinate."""
        return dict(self._cells)

    @property
    def gaps(self) -> typing.List[RawUIGap]:
        """The calls that could not be replayed, in call order."""
        return list(self._gaps)

    @property
    def call_count(self) -> int:
        """The number of calls recorded, including unsupported ones."""
        return self._call_count

    def record(
        self,
        method: HostMethodIdentifier,
        parameters: typing.List[typing.Any],
    ) -> int:
        """Record and, where possible, apply a RawUI host call.

        Args:
            method: The host method identifier of the call.
            parameters: The decoded method parameters, as returned by
                :attr:`psrpcore.PSRPEvent.method_parameters`.

        Returns:
            int: The sequence number assigned to this call. The same number is
            exposed on :attr:`ReplayCell.source_call` for any cells the call
            produced.
        """
        call_id = self._call_count
        self._call_count += 1

        if method in _BUFFER_METHODS:
            if method == HostMethodIdentifier.SetBufferContents1:
                self._set_buffer_region(call_id, parameters[0], parameters[1])
            elif method == HostMethodIdentifier.SetBufferContents2:
                self._set_buffer_cells(call_id, parameters[0], parameters[1])
            else:
                self._scroll_buffer_contents(call_id, parameters[0], parameters[1], parameters[2], parameters[3])

        elif method in _GEOMETRY_METHODS:
            if method == HostMethodIdentifier.SetBufferSize:
                self._set_buffer_size(parameters[0])
            elif method == HostMethodIdentifier.SetWindowSize:
                self._window_size = Size(parameters[0].Width, parameters[0].Height)
            else:
                self._window_position = Coordinates(parameters[0].X, parameters[0].Y)

        elif method in _NOOP_METHODS:
            pass

        elif method in _UNSUPPORTED_METHODS:
            self._gaps.append(
                RawUIGap(
                    call_id,
                    method,
                    list(parameters),
                    "higher level host UI call whose buffer effect is not described in the PSRP call",
                )
            )

        # Any other method (queries, prompts, lifecycle calls) does not mutate
        # the screen buffer, so there is nothing to replay and nothing to guess.

        return call_id

    def record_event(
        self,
        event: typing.Any,
    ) -> int:
        """Record a host call event.

        This is a convenience wrapper around :meth:`record` for
        :class:`psrpcore.RunspacePoolHostCallEvent` and
        :class:`psrpcore.PipelineHostCallEvent` objects.

        Args:
            event: The host call event to record.

        Returns:
            int: The sequence number assigned to the call.
        """
        return self.record(event.method_identifier, event.method_parameters)

    def get_cell(
        self,
        x: int,
        y: int,
    ) -> typing.Optional[ReplayCell]:
        """Get the cell at a coordinate.

        Args:
            x: The horizontal (column) coordinate.
            y: The vertical (row) coordinate.

        Returns:
            Optional[ReplayCell]: The cell, or :data:`None` if the coordinate
            has never been written to (or no longer holds an observed cell).
        """
        return self._cells.get((x, y))

    def get_rectangle(
        self,
        rectangle: typing.Union[Rectangle, typing.Tuple[int, int, int, int]],
    ) -> typing.List[typing.List[typing.Optional[ReplayCell]]]:
        """Get the cells within a rectangle.

        Only the state of the cells in the requested range is returned. Cells
        that have never been observed are :data:`None` rather than being filled
        in as blank cells.

        Args:
            rectangle: The :class:`psrpcore.types.Rectangle` or a
                ``(left, top, right, bottom)`` tuple. The bounds are inclusive
                just like the PSRP host rectangles.

        Returns:
            List[List[Optional[ReplayCell]]]: A row per ``top`` to ``bottom``
            value, each containing one entry per ``left`` to ``right`` value.
        """
        if isinstance(rectangle, Rectangle):
            left, top, right, bottom = rectangle.Left, rectangle.Top, rectangle.Right, rectangle.Bottom
        else:
            left, top, right, bottom = rectangle

        return [
            [self._cells.get((x, y)) for x in range(left, right + 1)]
            for y in range(top, bottom + 1)
        ]

    def get_visible_window(
        self,
    ) -> typing.Optional[typing.List[typing.List[typing.Optional[ReplayCell]]]]:
        """Get the cells currently visible through the window.

        Returns:
            Optional[List[List[Optional[ReplayCell]]]]: The visible cells in
            the same layout as :meth:`get_rectangle`, or :data:`None` if the
            window position or size has not been observed yet.
        """
        if self._window_position is None or self._window_size is None:
            return None

        left = self._window_position.X
        top = self._window_position.Y
        right = left + self._window_size.Width - 1
        bottom = top + self._window_size.Height - 1
        return self.get_rectangle((left, top, right, bottom))

    def is_visible(
        self,
        x: int,
        y: int,
    ) -> bool:
        """Whether a coordinate falls inside the current visible window.

        Returns :data:`False` if the window position or size is unknown.
        """
        if self._window_position is None or self._window_size is None:
            return False

        return (
            self._window_position.X <= x < self._window_position.X + self._window_size.Width
            and self._window_position.Y <= y < self._window_position.Y + self._window_size.Height
        )

    def render_text(
        self,
        rectangle: typing.Optional[typing.Union[Rectangle, typing.Tuple[int, int, int, int]]] = None,
        unknown: str = " ",
    ) -> str:
        """Render observed cells as plain text.

        Args:
            rectangle: The region to render. Defaults to the whole buffer when
                the buffer size is known.
            unknown: The character used for cells that were never observed.

        Returns:
            str: The rendered text with one line per row.
        """
        if rectangle is None:
            if self._buffer_size is None:
                raise ValueError("Cannot render the full buffer without a known buffer size")
            rectangle = (0, 0, self._buffer_size.Width - 1, self._buffer_size.Height - 1)

        rows = self.get_rectangle(rectangle)
        lines = []
        for row in rows:
            line = "".join(chr(cell.character) if cell is not None else unknown for cell in row)
            lines.append(line)

        return "\n".join(lines)

    def _set_buffer_region(
        self,
        call_id: int,
        rectangle: Rectangle,
        fill: BufferCell,
    ) -> None:
        for y in range(rectangle.Top, rectangle.Bottom + 1):
            for x in range(rectangle.Left, rectangle.Right + 1):
                self._cells[(x, y)] = ReplayCell(_copy_cell(fill), call_id)

    def _set_buffer_cells(
        self,
        call_id: int,
        origin: Coordinates,
        contents: typing.List[typing.List[BufferCell]],
    ) -> None:
        for row_idx, row in enumerate(contents):
            for col_idx, cell in enumerate(row):
                self._cells[(origin.X + col_idx, origin.Y + row_idx)] = ReplayCell(_copy_cell(cell), call_id)

    def _scroll_buffer_contents(
        self,
        call_id: int,
        source: Rectangle,
        destination: Coordinates,
        clip: Rectangle,
        fill: BufferCell,
    ) -> None:
        # Build the post-scroll state in a fresh map. The operation is
        # destination driven over the clip: each clip cell either receives the
        # content of the source cell it maps to or the fill cell. Reading the
        # previous state while writing the new one means overlapping
        # source/destination regions cannot depend on traversal order.
        delta_x = destination.X - source.Left
        delta_y = destination.Y - source.Top
        new_cells: typing.Dict[typing.Tuple[int, int], ReplayCell] = {
            (x, y): cell for (x, y), cell in self._cells.items() if not _in_rectangle(x, y, clip)
        }

        for y in range(clip.Top, clip.Bottom + 1):
            for x in range(clip.Left, clip.Right + 1):
                source_x = x - delta_x
                source_y = y - delta_y
                if _in_rectangle(source_x, source_y, source):
                    source_cell = self._cells.get((source_x, source_y))
                    if source_cell is not None:
                        new_cells[(x, y)] = source_cell
                    # An unobserved source cell moves as unknown content so the
                    # destination stays absent instead of becoming a fill cell.
                else:
                    new_cells[(x, y)] = ReplayCell(_copy_cell(fill), call_id)

        self._cells = new_cells

    def _set_buffer_size(
        self,
        size: Size,
    ) -> None:
        new_size = Size(size.Width, size.Height)

        # Cells that fall outside the new geometry are discarded. The remaining
        # cells keep their original state - nothing is filled in for the newly
        # exposed area. Pruning also covers a size declared after the first
        # writes.
        self._cells = {
            (x, y): cell
            for (x, y), cell in self._cells.items()
            if x < new_size.Width and y < new_size.Height
        }
        self._buffer_size = new_size


def _in_rectangle(
    x: int,
    y: int,
    rectangle: Rectangle,
) -> bool:
    return (
        rectangle.Left <= x <= rectangle.Right
        and rectangle.Top <= y <= rectangle.Bottom
    )


def _copy_cell(
    cell: BufferCell,
) -> BufferCell:
    # BufferCell is a PSObject; copy it so the same input object is never shared
    # across multiple ReplayCell instances.
    return BufferCell(
        Character=cell.Character,
        ForegroundColor=cell.ForegroundColor,
        BackgroundColor=cell.BackgroundColor,
        BufferCellType=cell.BufferCellType,
    )
