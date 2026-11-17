# RawUI Buffer Replay

The PowerShell Remoting Protocol carries the host method calls a peer invokes
on its `RawUI`, including the calls that mutate the screen buffer. On their own
those calls are just a stream of mutations.
[RawUIReplay](psrpcore.RawUIReplay) replays that stream in memory so the text
that was on the peer's screen can be queried afterwards.

The replay is deliberately conservative - it only reconstructs what the calls
actually describe:

* A cell that no call ever touched stays unknown (`None`) rather than being
  guessed as a blank cell.
* The screen buffer and the visible window are tracked separately. A cell can
  exist in the buffer without ever being visible through the window.
* Calls whose screen effect cannot be derived from the PSRP data (the higher
  level `Write`/`WriteLine` family, whose wrapping and scrolling behaviour is
  host specific) are recorded as [RawUIGap](psrpcore.RawUIGap) entries instead
  of being guessed at.

## Recording Calls

Every host call event exposes a `method_identifier` and `method_parameters`.
Feed them to the replay in the order they arrive. `record_event` accepts either
a `RunspacePoolHostCallEvent` or a `PipelineHostCallEvent` directly.

```python
import psrpcore

replay = psrpcore.RawUIReplay()

while True:
    event = client.next_event()
    if event is None:
        break

    if isinstance(event, (psrpcore.RunspacePoolHostCallEvent, psrpcore.PipelineHostCallEvent)):
        replay.record_event(event)
```

Each recorded call is assigned a sequence number. Every
[ReplayCell](psrpcore.ReplayCell) produced by a call keeps that number in
`source_call`, so the character and colors of a cell can always be traced back
to the originating call.

## Querying Cells

Cells are addressed by their buffer `(x, y)` coordinates. A single cell or a
rectangle can be queried; rectangle bounds are inclusive just like the PSRP
host rectangles.

```python
cell = replay.get_cell(0, 0)
if cell is not None:
    print(chr(cell.character), cell.foreground_color, cell.background_color, cell.source_call)

# A rectangle returns only the state of the requested range. Unknown cells are None.
grid = replay.get_rectangle(psrpcore.types.Rectangle(0, 0, 9, 9))
text = replay.render_text((0, 0, 9, 9))
```

## Buffer and Window

`SetBufferSize`, `SetWindowSize`, and `SetWindowPosition` update the recorded
geometry. Shrinking the buffer discards cells that fall outside the new size;
growing it does not fill the new area with blanks. The visible window is a view
onto the buffer and never changes the buffer cells themselves.

```python
visible = replay.get_visible_window()  # None until both window position and size are known
replay.is_visible(5, 5)
```

## Scrolling

`ScrollBufferContents` is applied as a destination driven copy over the clip
region. The post-scroll state is built from the pre-scroll state, so scrolling a
region that overlaps its destination gives the same result regardless of the
order individual cells are visited. Mapped source cells that were never
observed stay unknown at their destination instead of becoming fill cells.

## Unsupported Calls

When a call cannot be replayed it is retained rather than dropped. Each
[RawUIGap](psrpcore.RawUIGap) holds the call id, method, parameters, and the
reason it could not be applied. Queries, prompts, and other calls that do not
mutate the buffer are simply ignored.

```python
for gap in replay.gaps:
    print(gap.call_id, gap.method, gap.reason)
```
