#
# This file is part of pysnmp software.
#
# Copyright (c) 2005-2020, Ilya Etingof <etingof@gmail.com>
# License: https://www.pysnmp.com/pysnmp/license.html
#
"""Render a :class:`~pysnmp.linkreport.model.DailyReport` as text or JSON."""

from __future__ import annotations

import json
from datetime import datetime

from pysnmp.linkreport.counters import describe_delta, format_bytes as _format_bytes
from pysnmp.linkreport.model import (
    STATUS_CONFIRMED,
    STATUS_RANGE,
    STATUS_RESET,
    DailyReport,
    DeltaResult,
    DirectionSummary,
    Generation,
    InterfaceSummary,
)

__all__ = ["render_json", "render_text"]

_DIRECTION_LABELS = {"in": "in ", "out": "out"}


def _format_range(minimum: int, maximum: int | None) -> str:
    if maximum is None:
        return f">= {_format_bytes(minimum)}"
    if maximum == minimum:
        return _format_bytes(minimum)
    return f"{_format_bytes(minimum)}..{_format_bytes(maximum)}"


def _format_bps(value: float | None) -> str:
    if value is None:
        return "unknown"
    if value < 1000:
        return f"{value:.0f} bit/s"
    scaled = value
    for unit in ("kbit/s", "Mbit/s", "Gbit/s", "Tbit/s", "Pbit/s"):
        scaled /= 1000.0
        if scaled < 1000 or unit == "Pbit/s":
            return f"{scaled:.2f} {unit}"
    return f"{value:.0f} bit/s"


def _format_time(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.strftime("%H:%M:%S")


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = max(int(round(seconds)), 0)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


def _direction_line(label: str, summary: DirectionSummary) -> str:
    parts = [
        f"{label} {_format_range(summary.minimum_bytes, summary.maximum_bytes)}"
    ]
    if summary.maximum_bytes is not None and summary.maximum_bytes != summary.minimum_bytes:
        parts.append(f"unresolved {_format_bytes(summary.maximum_bytes - summary.minimum_bytes)}")

    average = summary.average_bps
    if average is not None:
        parts.append(f"avg {_format_bps(average)}")

    if summary.wraps_confirmed or summary.wraps_possible_extra:
        wraps = f"wraps {summary.wraps_confirmed}"
        if summary.wraps_possible_extra:
            wraps += f"..{summary.wraps_confirmed + summary.wraps_possible_extra}"
        parts.append(wraps)

    if summary.reset_windows:
        lost = summary.reset_lost_max_bytes
        parts.append(
            f"resets {summary.reset_windows}"
            + (
                f" (up to {_format_bytes(lost)} unaccounted)"
                if lost is not None
                else " (loss unbounded)"
            )
        )

    if summary.absent_windows:
        parts.append(f"missing {summary.absent_windows}")

    return "  ".join(parts)


def _status_word(delta: DeltaResult) -> str:
    if delta.status == STATUS_CONFIRMED:
        return "wrap" if delta.wrap_count_min else "clean"
    if delta.status == STATUS_RANGE:
        return "wrap-range"
    if delta.status == STATUS_RESET:
        return "reset"
    return delta.status


def _window_lines(
    summary: InterfaceSummary, indent: str = "      ", include_restart: bool = True
) -> list[str]:
    lines = []
    for window in summary.windows:
        if (
            window.inbound.status == STATUS_CONFIRMED
            and window.outbound.status == STATUS_CONFIRMED
            and not window.inbound.wrap_count_min
            and not window.outbound.wrap_count_min
        ):
            continue
        lines.append(
            f"{indent}{_format_time(window.start)}-{_format_time(window.end)} "
            f"({_format_duration(window.elapsed_seconds)})"
        )
        for direction, delta in (("in", window.inbound), ("out", window.outbound)):
            if delta.status == STATUS_CONFIRMED and not delta.wrap_count_min:
                continue
            lines.append(
                f"{indent}  {_DIRECTION_LABELS[direction]}: "
                f"{describe_delta(delta)} [{_status_word(delta)}]"
            )
    return lines


def _interface_block(
    summary: InterfaceSummary, include_windows: bool = True
) -> list[str]:
    speed = f"{_format_bps(summary.speed_bps)}" if summary.speed_bps else "unknown speed"
    description = summary.description or f"ifIndex {summary.if_index}"
    sources = sorted(summary.inbound.sources | summary.outbound.sources)
    source_text = "/".join(sources) if sources else "no counters"
    lines = [
        f"    if{summary.if_index} {description}  ({speed}, {source_text})",
        f"      {_direction_line('in ', summary.inbound)}",
        f"      {_direction_line('out', summary.outbound)}",
    ]
    if include_windows:
        lines.extend(_window_lines(summary))
    return lines


def _event_lines(report: DailyReport) -> list[tuple[datetime, str, int, object]]:
    """Collect discontinuities from the generation log, ordered by time.

    Generation events already contain the per-window events, so the summary
    objects are not walked again; doing so would list every wrap twice.
    """
    events = []
    for device in report.devices:
        for generation in device.generations:
            for event in generation.events:
                events.append((event.at, device.device, generation.index, event))
    return sorted(events, key=lambda item: (item[0], item[1]))


def render_text(report: DailyReport) -> str:
    """Render *report* as a plain-text daily link report."""
    lines = []
    start = report.period_start
    end = report.period_end
    lines.append("Link report")
    lines.append("=" * 72)

    if start and end:
        duration = (end - start).total_seconds()
        lines.append(
            f"Period: {start.isoformat()} .. {end.isoformat()} "
            f"({_format_duration(duration)})"
        )
    else:
        lines.append("Period: (no samples)")
    lines.append(f"Devices: {len(report.devices)}")
    lines.append("")
    lines.append(
        "Values marked as a range are what the evidence cannot pin down; the "
        "left number is certain, the right one is the largest compatible value."
    )

    for device in report.devices:
        lines.append("")
        lines.append(f"Device {device.device}")
        lines.append("-" * 72)
        lines.append(
            f"  samples {device.sample_count}, generations "
            f"{len(device.generations)}, "
            f"period {_format_time(device.period_start)}-"
            f"{_format_time(device.period_end)}"
        )

        for note in device.notes:
            lines.append(f"  note: {note}")

        for generation in device.generations:
            uptime_start = generation.uptime_start_seconds
            uptime_end = generation.uptime_end_seconds
            if uptime_start is not None and uptime_end is not None:
                uptime = f"uptime {_format_duration(uptime_start)}..{_format_duration(uptime_end)}"
            else:
                uptime = "uptime unknown"
            lines.append("")
            lines.append(
                f"  Generation {generation.index + 1}: "
                f"{_format_time(generation.started_at)}-"
                f"{_format_time(generation.ended_at)}, "
                f"{generation.sample_count} sample(s), {uptime}"
            )
            if generation.index:
                lines.append(f"    start: {generation.start_reason}")
            if not generation.summaries:
                lines.append("    no comparable interface windows")
                continue
            for if_index in sorted(generation.summaries):
                lines.extend(_interface_block(generation.summaries[if_index]))

        if len(device.generations) > 1:
            lines.append("")
            lines.append(
                "  Whole-period totals (across generations; restart windows "
                "contribute a reset, not a guessed increment):"
            )
            for if_index in sorted(device.summaries):
                lines.extend(
                    _interface_block(
                        device.summaries[if_index], include_windows=False
                    )
                )

    events = _event_lines(report)
    lines.append("")
    lines.append("Discontinuities")
    lines.append("-" * 72)
    if not events:
        lines.append("  none observed")
    else:
        for at, device, generation, event in events:
            if_index = f"if{event.if_index} " if event.if_index is not None else ""
            direction = f"{event.direction} " if event.direction else ""
            generation_text = (
                f" generation {generation + 1}" if generation is not None else ""
            )
            lines.append(
                f"  {at.strftime('%H:%M:%S')} {device}{generation_text} "
                f"{if_index}{direction}{event.kind}: {event.detail}"
            )
    lines.append("")
    return "\n".join(lines)


def _delta_to_dict(delta: DeltaResult) -> dict:
    return {
        "direction": delta.direction,
        "source": delta.source,
        "bits": delta.bits,
        "previous": delta.previous,
        "current": delta.current,
        "elapsed_seconds": round(delta.elapsed_seconds, 3),
        "minimum_bytes": delta.minimum,
        "maximum_bytes": delta.maximum,
        "status": delta.status,
        "wrap_count_min": delta.wrap_count_min,
        "wrap_count_max": delta.wrap_count_max,
        "capacity_bytes": delta.capacity_bytes,
        "note": delta.note,
    }


def _summary_to_dict(summary: DirectionSummary) -> dict:
    return {
        "minimum_bytes": summary.minimum_bytes,
        "maximum_bytes": summary.maximum_bytes,
        "confirmed": summary.is_confirmed,
        "average_bps": (
            round(summary.average_bps, 3)
            if summary.average_bps is not None
            else None
        ),
        "maximum_average_bps": (
            round(summary.maximum_average_bps, 3)
            if summary.maximum_average_bps is not None
            else None
        ),
        "covered_seconds": round(summary.covered_seconds, 3),
        "windows": summary.windows,
        "wraps_confirmed": summary.wraps_confirmed,
        "wraps_possible_extra": summary.wraps_possible_extra,
        "reset_windows": summary.reset_windows,
        "reset_lost_max_bytes": summary.reset_lost_max_bytes,
        "absent_windows": summary.absent_windows,
        "unavailable_windows": summary.unavailable_windows,
        "sources": sorted(summary.sources),
    }


def _interface_to_dict(summary: InterfaceSummary) -> dict:
    return {
        "if_index": summary.if_index,
        "description": summary.description,
        "speed_bps": summary.speed_bps,
        "first_seen": summary.first_seen.isoformat() if summary.first_seen else None,
        "last_seen": summary.last_seen.isoformat() if summary.last_seen else None,
        "inbound": _summary_to_dict(summary.inbound),
        "outbound": _summary_to_dict(summary.outbound),
        "windows": [
            {
                "index": window.index,
                "start": window.start.isoformat(),
                "end": window.end.isoformat(),
                "elapsed_seconds": round(window.elapsed_seconds, 3),
                "inbound": _delta_to_dict(window.inbound),
                "outbound": _delta_to_dict(window.outbound),
            }
            for window in summary.windows
        ],
    }


def _generation_to_dict(generation: Generation) -> dict:
    return {
        "index": generation.index,
        "started_at": generation.started_at.isoformat(),
        "ended_at": generation.ended_at.isoformat(),
        "start_reason": generation.start_reason,
        "sample_count": generation.sample_count,
        "restart_gap_seconds": generation.restart_gap_seconds,
        "uptime_start_seconds": generation.uptime_start_seconds,
        "uptime_end_seconds": generation.uptime_end_seconds,
        "interfaces": [
            _interface_to_dict(generation.summaries[if_index])
            for if_index in sorted(generation.summaries)
        ],
    }


def report_to_dict(report: DailyReport) -> dict:
    """Convert *report* to a JSON-serialisable structure."""
    return {
        "period_start": report.period_start.isoformat()
        if report.period_start
        else None,
        "period_end": report.period_end.isoformat() if report.period_end else None,
        "config": {
            "max_rate_bps": report.config.max_rate_bps,
            "rate_tolerance": report.config.rate_tolerance,
            "uptime_tolerance_seconds": report.config.uptime_tolerance_seconds,
            "uptime_tolerance_ratio": report.config.uptime_tolerance_ratio,
            "counter64_wrap_min_seconds": report.config.counter64_wrap_min_seconds,
        },
        "devices": [
            {
                "device": device.device,
                "period_start": device.period_start.isoformat(),
                "period_end": device.period_end.isoformat(),
                "sample_count": device.sample_count,
                "notes": device.notes,
                "generations": [
                    _generation_to_dict(generation)
                    for generation in device.generations
                ],
                "totals": [
                    _interface_to_dict(device.summaries[if_index])
                    for if_index in sorted(device.summaries)
                ],
            }
            for device in report.devices
        ],
    }


def render_json(report: DailyReport, indent: int | None = 2) -> str:
    """Render *report* as JSON."""
    return json.dumps(report_to_dict(report), indent=indent, sort_keys=False)
