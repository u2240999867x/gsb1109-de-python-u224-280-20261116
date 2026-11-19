"""Consistency checks for a recorded trace.

These checks only use the recorded evidence (clock readings, skew,
declared durations and resources).  The heavier feasibility analysis
under resource constraints lives in :mod:`schedule_audit.engine`.
"""

from .model import EVENT_IO
from .model import EVENT_WAIT


class InconsistentTraceError(ValueError):
    """Raised when the recorded evidence admits no true timeline."""


def event_window(event, clock_skew):
    """Return the true-time windows compatible with the clock readings."""
    start_lo = event.observed_start - clock_skew
    start_hi = event.observed_start + clock_skew
    end_lo = event.observed_end - clock_skew
    end_hi = event.observed_end + clock_skew
    # Duration values for which some start in the start window makes the
    # end fall inside the end window.
    duration_lo = max(0.0, end_lo - start_hi)
    duration_hi = max(0.0, end_hi - start_lo)
    return {
        "start_lo": start_lo,
        "start_hi": start_hi,
        "end_lo": end_lo,
        "end_hi": end_hi,
        "duration_lo": duration_lo,
        "duration_hi": duration_hi,
    }


def validate_trace(trace):
    """Validate the recorded evidence.

    Raises :class:`InconsistentTraceError` if no true timeline can
    satisfy the clock readings and declared duration ranges.  Returns a
    list of non-fatal warnings (strings) otherwise.
    """
    warnings = []
    for event in trace.all_events:
        window = event_window(event, trace.clock_skew)
        if event.duration_min > window["duration_hi"] or (
            event.duration_max < window["duration_lo"]
        ):
            raise InconsistentTraceError(
                f"event {event.name!r}: declared duration range "
                f"[{event.duration_min}, {event.duration_max}] does not "
                "intersect the duration window implied by the clock "
                f"readings and skew "
                f"[{window['duration_lo']}, {window['duration_hi']}]"
            )
        # Clock uncertainty may make adjacent readings overlap; that is
        # legal, but worth surfacing in the report.
        if (
            event.observed_end - event.observed_start
            < trace.clock_skew
        ):
            warnings.append(
                f"event {event.name!r}: recorded interval is shorter "
                "than the clock uncertainty; true start/end order is "
                "not provable from the readings alone"
            )
        if event.kind == EVENT_IO and event.transfer is None:
            raise InconsistentTraceError(
                f"IO event {event.name!r} carries no byte accounting"
            )
        if (
            event.kind == EVENT_WAIT
            and event.removable_throttle
            and not event.is_throttled
        ):  # pragma: no cover - the model already guards this
            raise InconsistentTraceError(
                f"wait {event.name!r} flagged removable without a "
                "throttle reason"
            )
    _validate_material_chains(trace, warnings)
    return warnings


def _validate_material_chains(trace, warnings):
    for material in trace.materials:
        events = material.events
        for previous, current in zip(events, events[1:]):
            prev_window = event_window(previous, trace.clock_skew)
            cur_window = event_window(current, trace.clock_skew)
            # The predecessor cannot provably end after the successor
            # starts.  Mere window overlap is normal under clock skew;
            # the explicit material chain supplies the ordering, the
            # scheduler enforces it, and no warning is needed for it.
            if prev_window["end_lo"] > cur_window["start_hi"]:
                raise InconsistentTraceError(
                    f"material {material.name!r}: event {previous.name!r}"
                    f" provably ends after successor {current.name!r} "
                    "starts, which contradicts the recorded order"
                )
