"""
Resample time-dependent CDF variables onto a fixed time grid.

The resampling rule is chosen per variable from its physical role, so a single
interpolation scheme is never applied to every variable in a file:

* ``status`` variables are discrete state codes. Values are only ever carried
  forward from observed samples; intermediate states are never invented.
* ``instant`` variables are instantaneous continuous quantities. Values are
  combined with time-weighted statistics (mean by default); interpolation never
  crosses a gap with no valid samples.
* ``accumulation`` variables are running counters. Increment between two valid
  samples is split across grid bins proportionally to elapsed time, the total
  is conserved, and no mass is ever carried across a counter reset.

Every result carries the provenance needed to audit it: the original record
indices that contributed to each bin, the fraction of each bin that was
actually covered, and the uncovered sub-intervals. The original time type
(CDF_EPOCH, CDF_EPOCH16, TT2000, datetime64 or unix seconds) and fill values
are preserved.
"""

from dataclasses import dataclass, field
from typing import Any, List, Optional, Sequence, Tuple

import numpy as np
import numpy.typing as npt

from cdflib.epochs import CDFepoch

# Variable roles ----------------------------------------------------------------

ROLE_STATUS = "status"
ROLE_INSTANT = "instant"
ROLE_ACCUMULATION = "accumulation"

VALID_ROLES = (ROLE_STATUS, ROLE_INSTANT, ROLE_ACCUMULATION)

# Resampling rules, grouped by the role that may use them ----------------------

RULE_FORWARD_FILL = "forward_fill"
RULE_SAMPLE = "sample"
RULE_MEAN = "mean"
RULE_LINEAR = "linear"
RULE_ALLOCATE = "allocate"

ROLE_RULES = {
    ROLE_STATUS: (RULE_FORWARD_FILL, RULE_SAMPLE),
    ROLE_INSTANT: (RULE_MEAN, RULE_LINEAR, RULE_SAMPLE),
    ROLE_ACCUMULATION: (RULE_ALLOCATE,),
}

_TIME_CONVENTIONS = ("auto", "datetime", "unix", "epoch", "epoch16", "tt2000")


@dataclass
class VariableSpec:
    """Description of one variable and how it may be resampled.

    Parameters
    ----------
    name : str
        Variable name (used for provenance and CDF lookups).
    role : str
        One of ``"status"``, ``"instant"`` or ``"accumulation"``.
    rule : str
        Resampling rule. Must be allowed for ``role``; see ``ROLE_RULES``.
    time_variable : str, optional
        Name of the epoch variable this variable depends on. When resampling
        from a ``CDF`` object this defaults to the variable's ``DEPEND_0``
        attribute.
    reset_times : array-like, optional
        Times at which a counter was reset. Only meaningful for accumulation
        variables. Each entry may be a ``numpy.datetime64`` or a value in the
        same time convention as the variable's time axis.
    fill_value : optional
        Value written into bins that cannot be populated. Defaults to ``NaN``
        for floating point data; integer variables require an explicit value.
    """

    name: str
    role: str
    rule: str
    time_variable: Optional[str] = None
    reset_times: Optional[npt.ArrayLike] = None
    fill_value: Any = None

    def __post_init__(self) -> None:
        if self.role not in VALID_ROLES:
            raise ValueError(f"Unknown role {self.role!r}; expected one of {VALID_ROLES}")
        allowed = ROLE_RULES[self.role]
        if self.rule not in allowed:
            raise ValueError(f"Rule {self.rule!r} is not allowed for role {self.role!r}; allowed: {allowed}")
        if self.reset_times is not None and self.role != ROLE_ACCUMULATION:
            raise ValueError("reset_times is only supported for accumulation variables")


@dataclass
class ResampledVariable:
    """A resampled variable together with its coverage and provenance.

    Fields
    ------
    name : str
        Variable name.
    role, rule : str
        Role and rule that produced the result.
    target_time : numpy.ndarray
        Grid centers converted back to the original time type of the variable.
    target_time_ns : numpy.ndarray of datetime64[ns]
        Grid centers in a common time type.
    bin_edges_ns : numpy.ndarray of datetime64[ns]
        ``len(target_time) + 1`` bin edges in a common time type.
    values : numpy.ndarray
        Resampled values, one per grid point; uncovered bins hold ``fill_value``.
    coverage : numpy.ndarray of float64
        Fraction of each bin's duration covered by valid samples (0 to 1).
    samples_used : list of numpy.ndarray
        For each bin, the original record indices whose samples contributed.
    uncovered : numpy.ndarray of datetime64[ns]
        Pairs ``[start, end)`` of the sub-intervals not covered by valid data.
    covered_segments : numpy.ndarray of datetime64[ns]
        Pairs ``[start, end)`` of the continuous valid-data segments.
    fill_value
        The fill value used for uncovered bins.
    """

    name: str
    role: str
    rule: str
    target_time: npt.NDArray
    target_time_ns: npt.NDArray[np.datetime64]
    bin_edges_ns: npt.NDArray[np.datetime64]
    values: npt.NDArray
    coverage: npt.NDArray[np.float64]
    samples_used: List[npt.NDArray[np.int64]] = field(default_factory=list)
    uncovered: npt.NDArray[np.datetime64] = field(default_factory=lambda: np.empty((0, 2), dtype="datetime64[ns]"))
    covered_segments: npt.NDArray[np.datetime64] = field(default_factory=lambda: np.empty((0, 2), dtype="datetime64[ns]"))
    fill_value: Any = None


# -----------------------------------------------------------------------------
# Time handling
# -----------------------------------------------------------------------------


def _infer_convention(time_values: npt.NDArray, convention: str) -> str:
    if time_values.dtype.kind == "M":
        return "datetime"
    if convention != "auto":
        if convention not in _TIME_CONVENTIONS:
            raise ValueError(f"Unknown time convention {convention!r}; expected one of {_TIME_CONVENTIONS}")
        return convention
    kind = time_values.dtype.kind
    if kind == "c":
        return "epoch16"
    if kind in "iu":
        return "tt2000"
    return "unix"


def _to_ns(time_values: npt.ArrayLike, convention: str = "auto") -> npt.NDArray[np.int64]:
    """Convert any supported time axis into int64 nanoseconds (NaT -> minimum int)."""
    raw = np.asarray(time_values)
    conv = _infer_convention(raw, convention)
    if conv == "datetime":
        return raw.astype("datetime64[ns]").astype(np.int64)
    if conv == "unix":
        return (raw.astype(np.float64) * 1e9).astype(np.int64)
    if conv == "epoch":
        unix = CDFepoch.unixtime(raw.astype(np.float64))
        return (np.asarray(unix, dtype=np.float64) * 1e9).astype(np.int64)
    if conv == "epoch16":
        unix = CDFepoch.unixtime(raw.astype(np.complex128))
        return (np.asarray(unix, dtype=np.float64) * 1e9).astype(np.int64)
    if conv == "tt2000":
        unix = CDFepoch.unixtime(raw.astype(np.int64))
        return (np.asarray(unix, dtype=np.float64) * 1e9).astype(np.int64)
    raise ValueError(f"Unsupported time convention {conv!r}")  # pragma: no cover


def _back_to_epoch(ns_values: npt.NDArray[np.int64], convention: str) -> npt.NDArray:
    """Convert nanoseconds back into the variable's original time type."""
    if convention == "datetime":
        return ns_values.astype("datetime64[ns]")
    unix = ns_values.astype(np.float64) / 1e9
    if convention == "unix":
        return unix
    if convention == "epoch":
        return CDFepoch.timestamp_to_cdfepoch(unix)
    if convention == "epoch16":
        return CDFepoch.timestamp_to_cdfepoch16(unix)
    if convention == "tt2000":
        return CDFepoch.timestamp_to_tt2000(unix).astype(np.int64)
    raise ValueError(f"Unsupported time convention {convention!r}")  # pragma: no cover


# -----------------------------------------------------------------------------
# Generic helpers
# -----------------------------------------------------------------------------


def _valid_mask(values: npt.NDArray, fill_value: Any) -> npt.NDArray[np.bool_]:
    mask = np.ones(values.shape[0], dtype=bool)
    if fill_value is not None:
        mask &= values != fill_value
    if values.dtype.kind in "fc":
        mask &= ~np.isnan(values)
    return mask


def _segments(valid: npt.NDArray[np.bool_], times_ns: npt.NDArray[np.int64]) -> Tuple[npt.NDArray[np.int64], npt.NDArray[np.int64]]:
    """Return ``[start, end)`` spans of consecutive valid samples.

    A segment runs from the first valid sample up to (but excluding) the first
    invalid sample after it, i.e. the span over which the sample actually
    carries information. A trailing valid segment ends at its last sample.
    """
    starts: List[int] = []
    ends: List[int] = []
    n = len(valid)
    i = 0
    while i < n:
        if not valid[i]:
            i += 1
            continue
        start = times_ns[i]
        j = i + 1
        while j < n and valid[j]:
            j += 1
        if j < n:
            end = times_ns[j]
        else:
            end = times_ns[j - 1]
        starts.append(start)
        ends.append(end)
        i = j
    return np.asarray(starts, dtype=np.int64), np.asarray(ends, dtype=np.int64)


def _covered_length(
    start: int,
    end: int,
    seg_starts: npt.NDArray[np.int64],
    seg_ends: npt.NDArray[np.int64],
) -> int:
    if len(seg_starts) == 0 or end <= start:
        return 0
    overlap = np.minimum(end, seg_ends) - np.maximum(start, seg_starts)
    return int(np.maximum(overlap, 0).sum())


def _make_uncovered(
    bin_starts: npt.NDArray[np.int64],
    bin_ends: npt.NDArray[np.int64],
    seg_starts: npt.NDArray[np.int64],
    seg_ends: npt.NDArray[np.int64],
) -> npt.NDArray[np.datetime64]:
    """Subtract covered segments from every bin and return uncovered pairs."""
    pairs: List[Tuple[int, int]] = []
    for start, end in zip(bin_starts, bin_ends):
        if end <= start:
            continue
        holes = [(start, end)]
        for seg_start, seg_end in zip(seg_starts, seg_ends):
            if seg_end <= start or seg_start >= end:
                continue
            new_holes: List[Tuple[int, int]] = []
            for hole_start, hole_end in holes:
                if seg_end <= hole_start or seg_start >= hole_end:
                    new_holes.append((hole_start, hole_end))
                    continue
                if seg_start > hole_start:
                    new_holes.append((hole_start, min(seg_start, hole_end)))
                if seg_end < hole_end:
                    new_holes.append((max(seg_end, hole_start), hole_end))
            holes = new_holes
        pairs.extend((a, b) for a, b in holes if b > a)
    if not pairs:
        return np.empty((0, 2), dtype="datetime64[ns]")
    return np.asarray(pairs, dtype=np.int64).astype("datetime64[ns]")


def _resolve_fill(values: npt.NDArray, fill_value: Any) -> Any:
    if fill_value is not None:
        return fill_value
    if values.dtype.kind in "fc":
        return np.nan
    raise ValueError(
        f"Variable with integer/dtype {values.dtype} has no fill_value; "
        "an explicit fill_value is required for uncovered bins"
    )


# -----------------------------------------------------------------------------
# Role-specific resampling
# -----------------------------------------------------------------------------


def _locate_events(
    event_ns: npt.NDArray[np.int64],
    start: int,
    end: int,
    seg_starts: npt.NDArray[np.int64],
    seg_ends: npt.NDArray[np.int64],
) -> npt.NDArray[np.int64]:
    """Original indices of valid events falling in ``[start, end)`` and inside coverage."""
    left = np.searchsorted(event_ns, start, side="left")
    right = np.searchsorted(event_ns, end, side="left")
    indices = np.arange(left, right)
    if len(indices) == 0 or len(seg_starts) == 0:
        return indices
    times = event_ns[indices]
    seg_id = np.searchsorted(seg_starts, times, side="right") - 1
    keep = (seg_id >= 0) & (times <= seg_ends[np.clip(seg_id, 0, len(seg_ends) - 1)])
    return indices[keep]


def _resample_status(
    values: npt.NDArray,
    times_ns: npt.NDArray[np.int64],
    edges_ns: npt.NDArray[np.int64],
    grid_ns: npt.NDArray[np.int64],
    valid: npt.NDArray[np.bool_],
    spec: VariableSpec,
    fill_value: Any,
    convention: str,
    seg_starts: npt.NDArray[np.int64],
    seg_ends: npt.NDArray[np.int64],
) -> ResampledVariable:
    valid_idx = np.flatnonzero(valid)
    event_ns = times_ns[valid_idx]
    nbins = len(grid_ns)
    result = np.full(nbins, fill_value, dtype=values.dtype)
    samples_used: List[npt.NDArray[np.int64]] = []
    for i in range(nbins):
        start, end = edges_ns[i], edges_ns[i + 1]
        local = _locate_events(event_ns, start, end, seg_starts, seg_ends)
        if spec.rule == RULE_SAMPLE:
            # First observed state inside the bin; no state is fabricated.
            samples_used.append(valid_idx[local])
            if local.size:
                result[i] = values[valid_idx[local[0]]]
            continue
        # Forward fill: prefer the last event in the bin, otherwise carry the
        # most recent observed state as long as its covered segment reaches
        # into the bin.
        chosen: Optional[int] = None
        if local.size:
            chosen = int(valid_idx[local[-1]])
        else:
            candidate = np.searchsorted(event_ns, end, side="left") - 1
            if candidate >= 0:
                candidate_time = event_ns[candidate]
                seg_id = np.searchsorted(seg_starts, candidate_time, side="right") - 1
                if seg_id >= 0 and seg_ends[seg_id] > start:
                    chosen = int(valid_idx[candidate])
                    local = np.array([candidate], dtype=np.int64)
        samples_used.append(valid_idx[local])
        if chosen is not None:
            result[i] = values[chosen]
    durations = edges_ns[1:] - edges_ns[:-1]
    coverage = np.array(
        [
            _covered_length(edges_ns[i], edges_ns[i + 1], seg_starts, seg_ends) / durations[i]
            if durations[i] > 0
            else 0.0
            for i in range(nbins)
        ]
    )
    return ResampledVariable(
        name=spec.name,
        role=spec.role,
        rule=spec.rule,
        target_time=_back_to_epoch(grid_ns, convention),
        target_time_ns=grid_ns.astype("datetime64[ns]"),
        bin_edges_ns=edges_ns.astype("datetime64[ns]"),
        values=result,
        coverage=coverage,
        samples_used=samples_used,
        uncovered=_make_uncovered(edges_ns[:-1], edges_ns[1:], seg_starts, seg_ends),
        covered_segments=np.column_stack((seg_starts, seg_ends)).astype("datetime64[ns]")
        if len(seg_starts)
        else np.empty((0, 2), dtype="datetime64[ns]"),
        fill_value=fill_value,
    )


def _mean_weights(
    times_ns: npt.NDArray[np.int64],
    valid_idx: npt.NDArray[np.int64],
    start: int,
    end: int,
    seg_starts: npt.NDArray[np.int64],
    seg_ends: npt.NDArray[np.int64],
) -> Tuple[npt.NDArray[np.int64], npt.NDArray[np.float64]]:
    """Time-weighted mean weights, restricted to covered sub-spans.

    Each valid sample represents the value on the interval ending at the next
    valid sample; the weight of a sample is the overlap of that interval with
    the bin, minus the parts lying outside covered segments.
    """
    event_ns = times_ns[valid_idx]
    left = np.searchsorted(event_ns, start, side="left")
    right = np.searchsorted(event_ns, end, side="left")
    # The last sample before the bin also represents a value interval that
    # may reach into the bin, so include it as a candidate piece.
    positions = list(range(left, right))
    if left > 0:
        positions.append(left - 1)
    pieces: List[Tuple[int, int, int]] = []
    for global_pos in positions:
        sample_index = valid_idx[global_pos]
        sample_time = event_ns[global_pos]
        next_time = event_ns[global_pos + 1] if global_pos + 1 < len(event_ns) else end
        piece_start = max(start, sample_time)
        piece_end = min(end, next_time)
        if piece_end > piece_start:
            pieces.append((piece_start, piece_end, sample_index))
    weights = np.zeros(len(pieces), dtype=np.float64)
    indices = np.empty(len(pieces), dtype=np.int64)
    for k, (piece_start, piece_end, sample_index) in enumerate(pieces):
        indices[k] = sample_index
        weights[k] = _covered_length(piece_start, piece_end, seg_starts, seg_ends)
    keep = weights > 0
    indices, weights = indices[keep], weights[keep]
    order = np.argsort(indices)
    return indices[order], weights[order]


def _resample_instant(
    values: npt.NDArray,
    times_ns: npt.NDArray[np.int64],
    edges_ns: npt.NDArray[np.int64],
    grid_ns: npt.NDArray[np.int64],
    valid: npt.NDArray[np.bool_],
    spec: VariableSpec,
    fill_value: Any,
    convention: str,
    seg_starts: npt.NDArray[np.int64],
    seg_ends: npt.NDArray[np.int64],
) -> ResampledVariable:
    valid_idx = np.flatnonzero(valid)
    nbins = len(grid_ns)
    work_dtype = np.float64 if values.dtype.kind in "iu" else values.dtype
    result = np.full(nbins, fill_value, dtype=work_dtype)
    samples_used: List[npt.NDArray[np.int64]] = []
    for i in range(nbins):
        start, end = edges_ns[i], edges_ns[i + 1]
        if spec.rule == RULE_SAMPLE:
            local = _locate_events(times_ns[valid_idx], start, end, seg_starts, seg_ends)
            samples_used.append(valid_idx[local])
            if local.size:
                result[i] = values[valid_idx[local[0]]]
            continue
        indices, weights = _mean_weights(times_ns, valid_idx, start, end, seg_starts, seg_ends)
        samples_used.append(indices)
        if indices.size == 0:
            continue
        if spec.rule == RULE_MEAN:
            result[i] = np.average(values[indices].astype(work_dtype), weights=weights)
        else:
            # Linear interpolation at the bin center, only when bracketing
            # samples exist inside one covered segment.
            center = grid_ns[i]
            event_ns = times_ns[valid_idx]
            exact = np.flatnonzero(event_ns == center)
            if exact.size:
                sample = valid_idx[exact[0]]
                result[i] = values[sample]
                samples_used[-1] = np.array([sample])
                continue
            right = np.searchsorted(event_ns, center, side="left")
            if right == 0 or right >= len(event_ns):
                continue
            left_idx = valid_idx[right - 1]
            right_idx = valid_idx[right]
            seg_id = np.searchsorted(seg_starts, times_ns[left_idx], side="right") - 1
            if seg_id < 0 or times_ns[right_idx] > seg_ends[seg_id]:
                continue
            t0, t1 = times_ns[left_idx], times_ns[right_idx]
            if t1 == t0:
                result[i] = values[left_idx]
            else:
                frac = (center - t0) / (t1 - t0)
                result[i] = values[left_idx] + frac * (values[right_idx] - values[left_idx])
            samples_used[-1] = np.array([left_idx, right_idx])
    durations = edges_ns[1:] - edges_ns[:-1]
    coverage = np.array(
        [
            _covered_length(edges_ns[i], edges_ns[i + 1], seg_starts, seg_ends) / durations[i]
            if durations[i] > 0
            else 0.0
            for i in range(nbins)
        ]
    )
    return ResampledVariable(
        name=spec.name,
        role=spec.role,
        rule=spec.rule,
        target_time=_back_to_epoch(grid_ns, convention),
        target_time_ns=grid_ns.astype("datetime64[ns]"),
        bin_edges_ns=edges_ns.astype("datetime64[ns]"),
        values=result,
        coverage=coverage,
        samples_used=samples_used,
        uncovered=_make_uncovered(edges_ns[:-1], edges_ns[1:], seg_starts, seg_ends),
        covered_segments=np.column_stack((seg_starts, seg_ends)).astype("datetime64[ns]")
        if len(seg_starts)
        else np.empty((0, 2), dtype="datetime64[ns]"),
        fill_value=fill_value,
    )


def _counter_segments(
    times_ns: npt.NDArray[np.int64],
    valid_idx: npt.NDArray[np.int64],
    reset_ns: Optional[npt.NDArray[np.int64]],
) -> List[npt.NDArray[np.int64]]:
    """Split valid sample indices at gaps (invalid records) and explicit resets."""
    if valid_idx.size == 0:
        return []
    segments: List[npt.NDArray[np.int64]] = []
    group = [valid_idx[0]]
    for prev, cur in zip(valid_idx[:-1], valid_idx[1:]):
        if cur != prev + 1:
            segments.append(np.asarray(group, dtype=np.int64))
            group = [cur]
            continue
        if reset_ns is not None and np.any((reset_ns > times_ns[prev]) & (reset_ns <= times_ns[cur])):
            segments.append(np.asarray(group, dtype=np.int64))
            group = [cur]
            continue
        group.append(cur)
    segments.append(np.asarray(group, dtype=np.int64))
    return segments


def _resample_accumulation(
    values: npt.NDArray,
    times_ns: npt.NDArray[np.int64],
    edges_ns: npt.NDArray[np.int64],
    grid_ns: npt.NDArray[np.int64],
    valid: npt.NDArray[np.bool_],
    spec: VariableSpec,
    fill_value: Any,
    convention: str,
    seg_starts: npt.NDArray[np.int64],
    seg_ends: npt.NDArray[np.int64],
    reset_ns: Optional[npt.NDArray[np.int64]],
) -> ResampledVariable:
    valid_idx = np.flatnonzero(valid)
    nbins = len(grid_ns)
    result = np.full(nbins, fill_value, dtype=np.float64)
    bin_sum = np.zeros(nbins, dtype=np.float64)
    bin_weight = np.zeros(nbins, dtype=np.float64)
    samples_used: List[set] = [set() for _ in range(nbins)]
    counter_segments = _counter_segments(times_ns, valid_idx, reset_ns)
    for group in counter_segments:
        group_times = times_ns[group]
        group_values = values[group].astype(np.float64)
        for k in range(len(group) - 1):
            t0, t1 = group_times[k], group_times[k + 1]
            v0, v1 = group_values[k], group_values[k + 1]
            if t1 <= t0:
                continue
            delta = v1 - v0
            first_bin = np.searchsorted(edges_ns, t0, side="right") - 1
            last_bin = np.searchsorted(edges_ns, t1, side="right") - 1
            first_bin = max(first_bin, 0)
            last_bin = min(last_bin, nbins - 1)
            for b in range(first_bin, last_bin + 1):
                bin_start, bin_end = edges_ns[b], edges_ns[b + 1]
                overlap_start = max(t0, bin_start)
                overlap_end = min(t1, bin_end)
                if overlap_end <= overlap_start:
                    continue
                covered = _covered_length(overlap_start, overlap_end, seg_starts, seg_ends)
                if covered <= 0:
                    continue
                fraction = covered / (t1 - t0)
                bin_sum[b] += delta * fraction
                bin_weight[b] += fraction
                samples_used[b].update((int(group[k]), int(group[k + 1])))
    populated = bin_weight > 0
    result[populated] = bin_sum[populated]
    durations = edges_ns[1:] - edges_ns[:-1]
    coverage = np.array(
        [
            _covered_length(edges_ns[i], edges_ns[i + 1], seg_starts, seg_ends) / durations[i]
            if durations[i] > 0
            else 0.0
            for i in range(nbins)
        ]
    )
    return ResampledVariable(
        name=spec.name,
        role=spec.role,
        rule=spec.rule,
        target_time=_back_to_epoch(grid_ns, convention),
        target_time_ns=grid_ns.astype("datetime64[ns]"),
        bin_edges_ns=edges_ns.astype("datetime64[ns]"),
        values=result,
        coverage=coverage,
        samples_used=[np.asarray(sorted(s), dtype=np.int64) for s in samples_used],
        uncovered=_make_uncovered(edges_ns[:-1], edges_ns[1:], seg_starts, seg_ends),
        covered_segments=np.column_stack((seg_starts, seg_ends)).astype("datetime64[ns]")
        if len(seg_starts)
        else np.empty((0, 2), dtype="datetime64[ns]"),
        fill_value=fill_value,
    )


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------


def resample_variable(
    values: npt.ArrayLike,
    times: npt.ArrayLike,
    target_grid: npt.ArrayLike,
    spec: VariableSpec,
    time_convention: str = "auto",
) -> ResampledVariable:
    """Resample one variable onto a fixed time grid.

    Parameters
    ----------
    values : array-like
        Original sample values, one per record.
    times : array-like
        Original sample times in any supported convention (datetime64, unix
        seconds, CDF_EPOCH, CDF_EPOCH16 or TT2000).
    target_grid : array-like
        Grid centers. Each bin spans from one midpoint between consecutive
        grid points to the next (the end bins extend by half a step), so at
        least two grid points are required. May use any supported time
        convention.
    spec : VariableSpec
        Role, rule, reset evidence and fill value for this variable.
    time_convention : str
        How to interpret ``times`` and ``target_grid`` when they are plain
        numbers. ``"auto"`` treats floats as unix seconds, integers as TT2000,
        complex as CDF_EPOCH16 and datetime64 as datetimes.

    Returns
    -------
    ResampledVariable
        Values, coverage and full sample provenance.
    """
    values = np.asarray(values)
    times = np.asarray(times)
    grid = np.asarray(target_grid)
    if values.ndim != 1:
        raise ValueError("resample_variable currently supports 1-D (record-varying) variables")
    if values.shape[0] != times.shape[0]:
        raise ValueError("values and times must have the same length")
    if grid.ndim != 1 or grid.shape[0] < 2:
        raise ValueError("target_grid must be 1-D with at least two grid points")

    convention = _infer_convention(times, time_convention)
    grid_convention = _infer_convention(grid, time_convention)
    times_ns = _to_ns(times, convention)
    grid_ns = _to_ns(grid, grid_convention)
    if np.any(np.diff(grid_ns) <= 0):
        raise ValueError("target_grid must be strictly increasing")
    if np.any(np.diff(times_ns) < 0):
        raise ValueError("sample times must be chronologically ordered")
    edges_ns = np.empty(grid_ns.size + 1, dtype=np.int64)
    edges_ns[1:-1] = (grid_ns[:-1] + grid_ns[1:]) // 2
    edges_ns[0] = grid_ns[0] - (edges_ns[1] - grid_ns[0])
    edges_ns[-1] = grid_ns[-1] + (grid_ns[-1] - edges_ns[-2])

    fill_value = _resolve_fill(values, spec.fill_value)
    valid = _valid_mask(values, spec.fill_value)
    seg_starts, seg_ends = _segments(valid, times_ns)

    reset_ns: Optional[npt.NDArray[np.int64]] = None
    if spec.reset_times is not None:
        reset_ns = _to_ns(np.asarray(spec.reset_times), convention)

    common = dict(
        values=values,
        times_ns=times_ns,
        edges_ns=edges_ns,
        grid_ns=grid_ns,
        valid=valid,
        spec=spec,
        fill_value=fill_value,
        convention=convention,
        seg_starts=seg_starts,
        seg_ends=seg_ends,
    )
    if spec.role == ROLE_STATUS:
        return _resample_status(**common)
    if spec.role == ROLE_INSTANT:
        return _resample_instant(**common)
    return _resample_accumulation(**common, reset_ns=reset_ns)


def resample_cdf(
    cdf: Any,
    target_grid: npt.ArrayLike,
    variable_specs: Sequence[VariableSpec],
    time_convention: str = "auto",
) -> List[ResampledVariable]:
    """Resample several variables read from a CDF file.

    Each variable is resolved through its own time axis (``time_variable`` or
    the ``DEPEND_0`` attribute), so variables depending on different epoch
    variables are handled independently. Fill values default to the variable's
    ``FILLVAL`` attribute and the original epoch type is used for the returned
    time axis.

    Parameters
    ----------
    cdf : cdflib.cdfread.CDF
        Open CDF reader.
    target_grid : array-like
        Grid centers, as datetime64 or values in the convention given by
        ``time_convention``.
    variable_specs : sequence of VariableSpec
        One specification per variable to resample.
    time_convention : str
        Convention of numeric time inputs; ``"auto"`` infers per variable from
        its epoch data type.

    Returns
    -------
    list of ResampledVariable
        One result per entry of ``variable_specs``, in the same order.
    """
    results: List[ResampledVariable] = []
    grid = np.asarray(target_grid)
    for spec in variable_specs:
        values = np.asarray(cdf.varget(spec.name))
        attrs = cdf.varattsget(spec.name)
        time_name = spec.time_variable or attrs.get("DEPEND_0")
        if not time_name:
            raise ValueError(f"Variable {spec.name!r} has no time_variable and no DEPEND_0 attribute")
        epoch_values = np.asarray(cdf.varget(time_name))
        epoch_info = cdf.varinq(time_name)
        if time_convention == "auto":
            data_type = getattr(epoch_info, "Data_Type", None)
            convention = {31: "epoch", 32: "epoch16", 33: "tt2000"}.get(data_type, _infer_convention(epoch_values, "auto"))
        else:
            convention = time_convention
        fill_value = spec.fill_value
        if fill_value is None and "FILLVAL" in attrs:
            fill_value = np.asarray(attrs["FILLVAL"]).reshape(()).item()
        effective_spec = VariableSpec(
            name=spec.name,
            role=spec.role,
            rule=spec.rule,
            time_variable=time_name,
            reset_times=spec.reset_times,
            fill_value=fill_value,
        )
        results.append(
            resample_variable(
                values=values,
                times=epoch_values,
                target_grid=grid,
                spec=effective_spec,
                time_convention=convention,
            )
        )
    return results
