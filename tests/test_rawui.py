# -*- coding: utf-8 -*-
# Copyright: (c) 2021, Jordan Borean (@jborean93) <jborean93@gmail.com>
# MIT License (see LICENSE or https://opensource.org/licenses/MIT)
import typing

import psrpcore
from psrpcore.types import (
    BufferCell,
    BufferCellType,
    ConsoleColor,
    Coordinates,
    HostMethodIdentifier,
    Rectangle,
    Size,
)

from .conftest import get_runspace_pair


def make_cell(
    character: typing.Union[int, str],
    foreground: ConsoleColor = ConsoleColor.White,
    background: ConsoleColor = ConsoleColor.Black,
    cell_type: BufferCellType = BufferCellType.Complete,
) -> BufferCell:
    return BufferCell(character, foreground, background, cell_type)


def test_initial_state_is_unknown():
    replay = psrpcore.RawUIReplay()

    assert replay.buffer_size is None
    assert replay.window_size is None
    assert replay.window_position is None
    assert replay.cells == {}
    assert replay.gaps == []
    assert replay.call_count == 0
    assert replay.get_cell(0, 0) is None
    assert replay.get_visible_window() is None
    assert replay.is_visible(0, 0) is False
    assert replay.get_rectangle(Rectangle(0, 0, 2, 1)) == [[None, None, None], [None, None, None]]


def test_set_buffer_contents1_fills_rectangle_and_attributes_source():
    replay = psrpcore.RawUIReplay()
    fill = make_cell("a", ConsoleColor.Cyan, ConsoleColor.Red)

    call_id = replay.record(HostMethodIdentifier.SetBufferContents1, [Rectangle(1, 2, 3, 4), fill])

    assert call_id == 0
    for y in range(2, 5):
        for x in range(1, 4):
            cell = replay.get_cell(x, y)
            assert cell is not None
            assert cell.character == ord("a")
            assert cell.foreground_color == ConsoleColor.Cyan
            assert cell.background_color == ConsoleColor.Red
            assert cell.buffer_cell_type == BufferCellType.Complete
            assert cell.source_call == call_id

    # Bounds are inclusive and cells outside the rectangle stay unobserved.
    assert replay.get_cell(0, 2) is None
    assert replay.get_cell(1, 1) is None
    assert replay.get_cell(4, 4) is None
    assert replay.get_cell(1, 5) is None
    assert len(replay.cells) == 9


def test_set_buffer_contents2_places_cells_at_origin():
    replay = psrpcore.RawUIReplay()
    contents = [
        [make_cell("a"), make_cell("b", ConsoleColor.Blue)],
        [make_cell("c", background=ConsoleColor.Gray), make_cell("d")],
    ]

    call_id = replay.record(HostMethodIdentifier.SetBufferContents2, [Coordinates(5, 7), contents])

    assert replay.get_cell(5, 7).character == ord("a")
    assert replay.get_cell(6, 7).character == ord("b")
    assert replay.get_cell(6, 7).foreground_color == ConsoleColor.Blue
    assert replay.get_cell(5, 8).character == ord("c")
    assert replay.get_cell(5, 8).background_color == ConsoleColor.Gray
    assert replay.get_cell(6, 8).character == ord("d")
    assert all(cell.source_call == call_id for cell in replay.cells.values())
    assert replay.get_cell(4, 7) is None
    assert replay.get_cell(7, 9) is None


def test_buffer_cell_types_are_preserved():
    replay = psrpcore.RawUIReplay()
    contents = [[make_cell("a", cell_type=BufferCellType.Leading), make_cell("b", cell_type=BufferCellType.Trailing)]]

    replay.record(HostMethodIdentifier.SetBufferContents2, [Coordinates(0, 0), contents])

    assert replay.get_cell(0, 0).buffer_cell_type == BufferCellType.Leading
    assert replay.get_cell(1, 0).buffer_cell_type == BufferCellType.Trailing


def test_distinct_calls_get_distinct_source_references():
    replay = psrpcore.RawUIReplay()
    first = replay.record(HostMethodIdentifier.SetBufferContents2, [Coordinates(0, 0), [[make_cell("a")]]])
    second = replay.record(HostMethodIdentifier.SetBufferContents2, [Coordinates(0, 0), [[make_cell("a")]]])

    assert first != second
    assert replay.get_cell(0, 0).source_call == second

    # The stored cell is a copy, mutating the original input must not affect the replay.
    shared = make_cell("z")
    replay.record(HostMethodIdentifier.SetBufferContents2, [Coordinates(1, 0), [[shared]]])
    shared.Character = "q"
    assert replay.get_cell(1, 0).character == ord("z")


def test_get_rectangle_only_returns_requested_range():
    replay = psrpcore.RawUIReplay()
    replay.record(HostMethodIdentifier.SetBufferContents1, [Rectangle(0, 0, 4, 4), make_cell("a")])

    result = replay.get_rectangle(Rectangle(1, 1, 2, 3))
    assert len(result) == 3
    assert all(len(row) == 2 for row in result)
    assert all(cell.character == ord("a") for row in result for cell in row)

    # Tuple bounds are supported and unknown cells stay None rather than blanks.
    sparse = psrpcore.RawUIReplay()
    sparse.record(HostMethodIdentifier.SetBufferContents2, [Coordinates(0, 0), [[make_cell("x")]]])
    result = sparse.get_rectangle((0, 0, 1, 1))
    assert result[0][0].character == ord("x")
    assert result[0][1] is None
    assert result[1][0] is None
    assert result[1][1] is None


def test_window_is_tracked_separately_from_buffer():
    replay = psrpcore.RawUIReplay()
    replay.record(HostMethodIdentifier.SetBufferContents1, [Rectangle(0, 0, 9, 9), make_cell("a")])

    # No window information yet - visibility cannot be assumed.
    assert replay.get_visible_window() is None
    assert replay.is_visible(0, 0) is False

    replay.record(HostMethodIdentifier.SetWindowPosition, [Coordinates(2, 3)])
    replay.record(HostMethodIdentifier.SetWindowSize, [Size(4, 2)])

    assert replay.window_position.X == 2
    assert replay.window_position.Y == 3
    assert replay.window_size.Width == 4
    assert replay.window_size.Height == 2
    assert replay.is_visible(2, 3) is True
    assert replay.is_visible(5, 4) is True
    assert replay.is_visible(6, 4) is False
    assert replay.is_visible(1, 3) is False
    assert replay.is_visible(2, 5) is False

    visible = replay.get_visible_window()
    assert len(visible) == 2
    assert all(len(row) == 4 for row in visible)
    assert visible[0][0].character == ord("a")

    # Moving the window changes what is visible without touching buffer cells.
    replay.record(HostMethodIdentifier.SetWindowPosition, [Coordinates(6, 8)])
    assert replay.is_visible(6, 8) is True
    assert replay.is_visible(2, 3) is False
    assert replay.get_cell(2, 3).character == ord("a")


def test_scroll_moves_cells_and_keeps_original_attribution():
    replay = psrpcore.RawUIReplay()
    write_id = replay.record(
        HostMethodIdentifier.SetBufferContents2,
        [Coordinates(1, 1), [[make_cell("A", ConsoleColor.Cyan, ConsoleColor.Red)]]],
    )
    scroll_id = replay.record(
        HostMethodIdentifier.ScrollBufferContents,
        [Rectangle(0, 0, 2, 2), Coordinates(10, 10), Rectangle(0, 0, 12, 12), make_cell("F", ConsoleColor.Green)],
    )

    moved = replay.get_cell(11, 11)
    assert moved.character == ord("A")
    assert moved.foreground_color == ConsoleColor.Cyan
    assert moved.background_color == ConsoleColor.Red
    assert moved.source_call == write_id

    # The old location now holds the fill cell attributed to the scroll call.
    old = replay.get_cell(1, 1)
    assert old.character == ord("F")
    assert old.foreground_color == ConsoleColor.Green
    assert old.source_call == scroll_id

    # Unobserved source cells move as unknown content, they are not turned into fills.
    assert replay.get_cell(10, 10) is None
    assert replay.get_cell(12, 12) is None

    # Clip cells outside the moved region receive the fill cell.
    assert replay.get_cell(0, 0).character == ord("F")
    assert replay.get_cell(12, 0).source_call == scroll_id

    # Cells outside the clip are untouched.
    assert replay.get_cell(50, 50) is None


def test_scroll_overlap_does_not_depend_on_traversal_order():
    def build(write_order: typing.List[int]) -> psrpcore.RawUIReplay:
        replay = psrpcore.RawUIReplay()
        for x in write_order:
            replay.record(HostMethodIdentifier.SetBufferContents2, [Coordinates(x, 0), [[make_cell(chr(65 + x))]]])

        replay.record(
            HostMethodIdentifier.ScrollBufferContents,
            [Rectangle(0, 0, 4, 0), Coordinates(2, 0), Rectangle(0, 0, 6, 0), make_cell(" ")],
        )
        return replay

    expected = [
        ord(" "),
        ord(" "),
        ord("A"),
        ord("B"),
        ord("C"),
        ord("D"),
        ord("E"),
    ]

    def grid(replay: psrpcore.RawUIReplay) -> typing.List[typing.Optional[int]]:
        return [replay.get_cell(x, 0).character if replay.get_cell(x, 0) else None for x in range(7)]

    # The resulting text is identical no matter what order the cells were written in.
    forward = build([0, 1, 2, 3, 4])
    reverse = build([4, 3, 2, 1, 0])
    assert grid(forward) == expected
    assert grid(reverse) == expected

    # Index 4 must hold the ORIGINAL 'C'. A naive in-place forward cell copy would have
    # overwritten source cell 2 before reading it and left 'A' here instead.
    assert forward.get_cell(4, 0).character == ord("C")

    # Two replays built from the same call sequence agree cell for cell, including attribution.
    assert build([0, 1, 2, 3, 4]).cells == build([0, 1, 2, 3, 4]).cells


def test_scroll_only_rewrites_cells_inside_the_clip():
    replay = psrpcore.RawUIReplay()
    replay.record(
        HostMethodIdentifier.SetBufferContents2,
        [
            Coordinates(0, 0),
            [[make_cell("a"), make_cell("b"), make_cell("c")]],
        ],
    )
    # Shift the source 2 columns right but limit the clip to the two target cells.
    replay.record(
        HostMethodIdentifier.ScrollBufferContents,
        [Rectangle(0, 0, 2, 0), Coordinates(2, 0), Rectangle(2, 0, 3, 0), make_cell("F")],
    )

    # The clip cells received the mapped source content.
    assert replay.get_cell(2, 0).character == ord("a")
    assert replay.get_cell(3, 0).character == ord("b")

    # Cells outside the clip are left exactly as they were, including the old 'c'.
    assert replay.get_cell(0, 0).character == ord("a")
    assert replay.get_cell(1, 0).character == ord("b")


def test_scroll_destination_can_pull_source_from_outside_the_clip():
    replay = psrpcore.RawUIReplay()
    # Only the source cell is observed; the clip window sits entirely to its right.
    write_id = replay.record(HostMethodIdentifier.SetBufferContents2, [Coordinates(0, 0), [[make_cell("a")]]])
    replay.record(
        HostMethodIdentifier.ScrollBufferContents,
        [Rectangle(0, 0, 0, 0), Coordinates(5, 0), Rectangle(5, 0, 6, 0), make_cell("F")],
    )

    pulled = replay.get_cell(5, 0)
    assert pulled.character == ord("a")
    assert pulled.source_call == write_id
    # The other clip cell has no matching source cell and gets the fill.
    assert replay.get_cell(6, 0).character == ord("F")
    # The original source cell is outside the clip and remains in place.
    assert replay.get_cell(0, 0).character == ord("a")


def test_buffer_size_shrink_drops_cells_and_grow_keeps_unknown():
    replay = psrpcore.RawUIReplay()
    replay.record(HostMethodIdentifier.SetBufferSize, [Size(10, 10)])
    replay.record(HostMethodIdentifier.SetBufferContents1, [Rectangle(0, 0, 9, 9), make_cell("z")])

    replay.record(HostMethodIdentifier.SetBufferSize, [Size(5, 5)])
    assert replay.buffer_size.Width == 5
    assert replay.buffer_size.Height == 5
    assert replay.get_cell(4, 4).character == ord("z")
    assert replay.get_cell(5, 5) is None
    assert replay.get_cell(9, 9) is None

    replay.record(HostMethodIdentifier.SetBufferSize, [Size(20, 20)])
    assert replay.buffer_size.Width == 20
    assert replay.buffer_size.Height == 20
    assert replay.get_cell(4, 4).character == ord("z")
    # Newly exposed area was never observed - it must not be filled with blanks.
    assert replay.get_cell(15, 15) is None
    assert replay.get_cell(19, 19) is None


def test_buffer_size_declared_after_writes_still_prunes_cells():
    replay = psrpcore.RawUIReplay()
    replay.record(HostMethodIdentifier.SetBufferContents1, [Rectangle(0, 0, 9, 9), make_cell("z")])

    # The size is learned only after the cells were written; out of range cells still drop.
    replay.record(HostMethodIdentifier.SetBufferSize, [Size(4, 4)])
    assert replay.get_cell(3, 3).character == ord("z")
    assert replay.get_cell(4, 4) is None
    assert replay.get_cell(9, 9) is None
    # Cells within the new bounds that were never written stay unknown.
    assert replay.get_cell(0, 1) is not None


def test_render_text_uses_placeholder_for_unknown_cells():
    replay = psrpcore.RawUIReplay(buffer_size=Size(3, 2))
    replay.record(HostMethodIdentifier.SetBufferContents2, [Coordinates(0, 0), [[make_cell("h"), make_cell("i")]]])

    assert replay.render_text() == "hi \n   "
    assert replay.render_text(unknown="?") == "hi?\n???"
    assert replay.render_text(Rectangle(0, 0, 0, 1)) == "h\n "


def test_unsupported_calls_become_gaps_and_do_not_touch_buffer():
    replay = psrpcore.RawUIReplay()
    write_id = replay.record(HostMethodIdentifier.SetBufferContents2, [Coordinates(0, 0), [[make_cell("a")]]])

    gap_methods = [
        (HostMethodIdentifier.Write1, ["text"]),
        (HostMethodIdentifier.Write2, [ConsoleColor.Blue, ConsoleColor.Black, "text"]),
        (HostMethodIdentifier.WriteLine1, []),
        (HostMethodIdentifier.WriteLine2, ["line"]),
        (HostMethodIdentifier.WriteLine3, [ConsoleColor.Blue, ConsoleColor.Black, "line"]),
        (HostMethodIdentifier.WriteErrorLine, ["err"]),
        (HostMethodIdentifier.WriteDebugLine, ["dbg"]),
        (HostMethodIdentifier.WriteVerboseLine, ["vrb"]),
        (HostMethodIdentifier.WriteWarningLine, ["wrn"]),
    ]

    for method, params in gap_methods:
        replay.record(method, params)

    assert len(replay.gaps) == len(gap_methods)
    for idx, gap in enumerate(replay.gaps):
        assert gap.call_id == write_id + 1 + idx
        assert gap.method == gap_methods[idx][0]
        assert gap.parameters == gap_methods[idx][1]
        assert "not described" in gap.reason

    # The observed buffer is unchanged and still attributed to the real call.
    assert replay.get_cell(0, 0).source_call == write_id
    assert replay.call_count == 1 + len(gap_methods)


def test_non_buffer_calls_are_not_gaps():
    replay = psrpcore.RawUIReplay()
    replay.record(HostMethodIdentifier.GetBufferContents, [Rectangle(0, 0, 1, 1)])
    replay.record(HostMethodIdentifier.GetCursorPosition, [])
    replay.record(HostMethodIdentifier.ReadLine, [])
    replay.record(HostMethodIdentifier.PromptForChoice, ["caption", "message", [], -1])
    replay.record(HostMethodIdentifier.SetCursorPosition, [Coordinates(0, 0)])
    replay.record(HostMethodIdentifier.SetForegroundColor, [ConsoleColor.Blue])
    replay.record(HostMethodIdentifier.SetBackgroundColor, [ConsoleColor.Red])
    replay.record(HostMethodIdentifier.SetCursorSize, [25])
    replay.record(HostMethodIdentifier.SetWindowTitle, ["title"])
    replay.record(HostMethodIdentifier.FlushInputBuffer, [])
    replay.record(HostMethodIdentifier.SetWindowSize, [Size(80, 25)])
    replay.record(HostMethodIdentifier.SetWindowPosition, [Coordinates(0, 0)])

    assert replay.gaps == []
    assert replay.cells == {}
    assert replay.call_count == 12
    assert replay.window_size.Width == 80
    assert replay.window_size.Height == 25
    assert replay.window_position.X == 0
    assert replay.window_position.Y == 0


def test_record_event_with_runspace_pool_host_call():
    client, server = get_runspace_pair()
    server_host = psrpcore.ServerHostRequestor(server)
    replay = psrpcore.RawUIReplay()

    server_host.set_buffer_cells(0, 0, 2, 1, "a", ConsoleColor.Cyan, ConsoleColor.Red)
    client.receive_data(server.data_to_send())
    event = client.next_event()

    assert isinstance(event, psrpcore.RunspacePoolHostCallEvent)
    call_id = replay.record_event(event)
    assert call_id == 0
    assert event.method_identifier == HostMethodIdentifier.SetBufferContents1

    rectangle = event.method_parameters[0]
    fill = event.method_parameters[1]
    assert rectangle.Left == 0
    assert rectangle.Top == 0
    assert rectangle.Right == 2
    assert rectangle.Bottom == 1
    assert fill.Character == ord("a")

    for y in range(2):
        for x in range(3):
            cell = replay.get_cell(x, y)
            assert cell.character == ord("a")
            assert cell.foreground_color == ConsoleColor.Cyan
            assert cell.background_color == ConsoleColor.Red
            assert cell.source_call == 0


def test_record_event_with_pipeline_host_call_and_scroll():
    client, server = get_runspace_pair()
    c_ps = psrpcore.ClientPowerShell(client)
    c_ps.add_script("$host.UI.RawUI.ScrollBufferContents")
    c_ps.start()

    s_ps = psrpcore.ServerPipeline(server, c_ps.pipeline_id)
    server.receive_data(client.data_to_send())
    server.next_event()
    s_ps.start()
    client.receive_data(server.data_to_send())
    client.next_event()

    server_host = psrpcore.ServerHostRequestor(s_ps)
    replay = psrpcore.RawUIReplay()

    write_id = replay.record(
        HostMethodIdentifier.SetBufferContents2,
        [Coordinates(0, 0), [[make_cell("a"), make_cell("b")]]],
    )

    server_host.scroll_buffer_contents(0, 0, 1, 0, 3, 0, 0, 0, 4, 0, "F")
    client.receive_data(server.data_to_send())
    event = client.next_event()

    assert isinstance(event, psrpcore.PipelineHostCallEvent)
    scroll_id = replay.record_event(event)
    assert scroll_id == write_id + 1
    assert event.method_identifier == HostMethodIdentifier.ScrollBufferContents

    assert replay.get_cell(3, 0).character == ord("a")
    assert replay.get_cell(4, 0).character == ord("b")
    assert replay.get_cell(3, 0).source_call == write_id
    assert replay.get_cell(0, 0).character == ord("F")
    assert replay.get_cell(0, 0).source_call == scroll_id
