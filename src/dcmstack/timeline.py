"""Concatenate independently acquired Nifti batches along a timeline.

This module is deliberately stricter than :meth:`NiftiWrapper.from_sequence`.
It is meant for the case where two or more conversions of the same subject are
being placed on one time axis.  In that situation a silent join is unsafe:

* a batch may have been reacquired at a different spatial position,
* two batches may cover the same time window,
* a batch may cover only part of the declared window,
* or the data may already have been resampled.

Every input therefore carries an explicit ``BatchSpec``: a stable acquisition
identity, the declared coverage interval, and optional provenance.  Geometry
and dimension definitions are checked before any data is copied.  Overlaps are
preserved (each source remains a separate time sample) and incomplete coverage
is recorded as a declared gap, never filled or interpolated.  No resampling is
performed by this module.

The result stores a ``dcmstack.timeline`` record in the DcmMeta extension with
one entry per input batch, including the geometry checks and the coverage
declaration.  Source DICOM provenance already present in each batch (e.g.
``SeriesInstanceUID``) is left untouched; the timeline record is additive.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from .nifti_wrapper import NiftiWrapper


TIMELINE_KEY = "dcmstack.timeline"
"""Key used for the timeline provenance record in the global const metadata."""

TIME_INDEX_KEY = "dcmstack.time_index"
"""Key used for the honest ordinal time axis when no source time key is safe."""

TIMELINE_VERSION = 1
"""Schema version for the timeline provenance record."""

DEFAULT_GEOMETRY_ATOL = 5e-4
"""Default absolute tolerance for affine comparisons (matches from_sequence)."""

DEFAULT_GEOMETRY_RTOL = 5e-5
"""Default relative tolerance for affine comparisons."""

DEFAULT_TIME_ATOL = 1e-6
"""Default tolerance (seconds) for coverage interval comparisons."""


class TimelineError(ValueError):
    """Base class for errors raised while building a timeline."""


class GeometryError(TimelineError):
    """The batches do not share a single voxel/world geometry."""


class ResamplingError(GeometryError):
    """A batch's data no longer matches its declared source geometry."""


class CoverageError(TimelineError):
    """Coverage declarations are missing, malformed, or inconsistent.

    Overlaps are allowed and retained; gaps are allowed and reported. This
    error is for declarations that cannot be interpreted at all.
    """


class IdentityError(TimelineError):
    """Acquisition identities or dimension definitions are inconsistent."""


@dataclass(frozen=True)
class Coverage:
    """The time interval a batch is declared to cover.

    Parameters
    ----------
    start, end
        Interval bounds in the given units.  ``end`` may be ``None`` for an
        open-ended batch, but only the final batch may be open-ended.
    units
        ``seconds`` (default), ``milliseconds``, ``minutes``, or ``hours``.
        Values are normalised to seconds on construction.
    tolerance
        Comparison tolerance in seconds, used only to distinguish a declared
        gap from an overlap.
    """

    start: float
    end: Optional[float]
    units: str = "seconds"
    tolerance: float = DEFAULT_TIME_ATOL

    _FACTORS = {"seconds": 1.0, "second": 1.0, "s": 1.0,
                "milliseconds": 1e-3, "millisecond": 1e-3, "ms": 1e-3,
                "minutes": 60.0, "minute": 60.0, "min": 60.0,
                "hours": 3600.0, "hour": 3600.0, "h": 3600.0}

    def __post_init__(self):
        try:
            factor = self._FACTORS[str(self.units).lower()]
        except KeyError:
            raise CoverageError(
                "Unknown coverage units %r; use seconds, milliseconds, "
                "minutes, or hours" % (self.units,)
            )
        start = float(self.start) * factor
        end = None if self.end is None else float(self.end) * factor
        if not math.isfinite(start):
            raise CoverageError("Coverage start must be finite")
        if end is not None:
            if not math.isfinite(end):
                raise CoverageError("Coverage end must be finite")
            if end < start:
                raise CoverageError("Coverage end precedes start")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "tolerance", float(self.tolerance))
        object.__setattr__(self, "units", "seconds")

    @property
    def open_ended(self) -> bool:
        return self.end is None

    def as_dict(self) -> Dict[str, Any]:
        return {"start": self.start, "end": self.end, "units": "seconds"}


@dataclass
class BatchSpec:
    """Explicit identity, coverage, and provenance for one input batch.

    Parameters
    ----------
    acquisition_id
        Stable identity for the acquisition (e.g. a series UID, scan ID, or
        project-assigned opaque ID).  Required and must be unique in a timeline.
    coverage
        Declared time window.  Required.  A ``(start, end)`` pair or mapping is
        accepted and converted to :class:`Coverage`.
    source
        Human-readable source label (path, series description, etc.).  Stored
        in provenance and defaults to ``acquisition_id``.
    source_paths
        Optional list of originating files for provenance.  Only labels are
        stored; file contents are not read.
    resampled
        ``True`` only if the data was intentionally resampled to the common
        geometry before this call.  Without it, any spatial change is refused.
    resample_note
        Required explanation when ``resampled`` is true.  This keeps a change
        of spatial position explicit instead of hiding it in a join.
    metadata
        Optional free-form mapping of additional provenance.
    expected_affine
        Optional 4x4 affine of the data at conversion time.  With
        ``resampled=False`` the current affine must match it.
    time_key
        Metadata key holding per-time-sample values (e.g.
        ``SND.AcquisitionTimeStamp``).  Must be consistent across batches when
        used, and must not be globally constant.
    """

    acquisition_id: str
    coverage: Any
    source: Optional[str] = None
    source_paths: Optional[Sequence[str]] = None
    resampled: bool = False
    resample_note: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    expected_affine: Optional[np.ndarray] = None
    time_key: Optional[str] = None

    def __post_init__(self):
        if not isinstance(self.acquisition_id, str) or not self.acquisition_id.strip():
            raise IdentityError("Each batch must have a non-empty acquisition_id")
        if self.coverage is None:
            raise CoverageError(
                "Batch %r has no coverage declaration; missing time spans must "
                "be stated, not inferred" % self.acquisition_id
            )
        if not isinstance(self.coverage, Coverage):
            coverage = self.coverage
            if isinstance(coverage, Mapping):
                coverage = Coverage(**coverage)
            else:
                coverage = Coverage(*coverage)
            self.coverage = coverage
        if self.resampled and not (self.resample_note or "").strip():
            raise ResamplingError(
                "Batch %r is marked resampled but has no resample_note; "
                "spatial changes cannot be silent" % self.acquisition_id
            )
        if self.source_paths is not None:
            self.source_paths = [str(p) for p in self.source_paths]
        if self.expected_affine is not None:
            expected = np.asarray(self.expected_affine, dtype=float)
            if expected.shape != (4, 4):
                raise GeometryError("expected_affine must be 4x4")
            self.expected_affine = expected

    def as_dict(self) -> Dict[str, Any]:
        return {
            "acquisition_id": self.acquisition_id,
            "source": self.source or self.acquisition_id,
            "source_paths": list(self.source_paths or ()),
            "coverage": self.coverage.as_dict(),
            "resampled": bool(self.resampled),
            "resample_note": self.resample_note,
            "metadata": _jsonable(self.metadata),
            "time_key": self.time_key,
        }


@dataclass
class BatchResult:
    """Outcome of validating one batch."""

    spec: BatchSpec
    wrapper: NiftiWrapper
    n_volumes: int
    affine: np.ndarray
    shape: Tuple[int, ...]
    geometry_check: Dict[str, Any]
    source_times: Optional[List[float]] = None


@dataclass
class TimelineResult:
    """A merged timeline and the report describing how it was assembled."""

    wrapper: NiftiWrapper
    record: Dict[str, Any]

    @property
    def n_time_points(self) -> int:
        return int(self.record["n_time_points"])

    @property
    def gaps(self) -> List[Dict[str, Any]]:
        return self.record["coverage"]["gaps"]

    @property
    def overlaps(self) -> List[Dict[str, Any]]:
        return self.record["coverage"]["overlaps"]

    def to_filename(self, path: str) -> None:
        self.wrapper.to_filename(path)


def _jsonable(value):
    """Convert common provenance values to JSON-compatible types."""
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _normalise_pairs(batches) -> List[Tuple[NiftiWrapper, BatchSpec]]:
    """Accept (wrapper, spec) pairs, spec-keyed mappings, or wrappers carrying
    a ``timeline_spec`` attribute."""
    if isinstance(batches, Mapping):
        batches = list(batches.items())
    result = []
    for item in batches:
        if isinstance(item, tuple) and len(item) == 2:
            first, second = item
            if isinstance(first, BatchSpec):
                spec, wrapper = first, second
            elif isinstance(second, BatchSpec):
                wrapper, spec = first, second
            else:
                raise TypeError(
                    "Batches must be (NiftiWrapper, BatchSpec) pairs; got %s"
                    % (type(item),)
                )
        else:
            wrapper = item
            spec = getattr(wrapper, "timeline_spec", None)
            if spec is None:
                raise IdentityError(
                    "Batch has no acquisition identity; pass a BatchSpec with "
                    "acquisition_id and coverage"
                )
        if not isinstance(wrapper, NiftiWrapper):
            wrapper = NiftiWrapper(wrapper)
        if not isinstance(spec, BatchSpec):
            raise IdentityError("Invalid BatchSpec for batch")
        result.append((wrapper, spec))
    return result


def _check_extension(wrapper: NiftiWrapper, spec: BatchSpec) -> None:
    """Verify that the metadata describes the image it is attached to."""
    shape = tuple(wrapper.nii_img.shape)
    if tuple(wrapper.meta_ext.shape) != shape:
        raise IdentityError(
            "Batch %r has metadata for shape %s but image shape %s; dimension "
            "definitions do not match"
            % (spec.acquisition_id, tuple(wrapper.meta_ext.shape), shape)
        )
    if len(shape) < 3 or len(shape) > 5:
        raise TimelineError("Batches must be 3D to 5D; got %s" % (shape,))
    if len(shape) == 5 and shape[4] != 1:
        raise TimelineError("Vector dimensions are not timeline batches")


def _check_dimension_definition(reference: NiftiWrapper, current: NiftiWrapper,
                                ref_id: str, cur_id: str) -> Dict[str, Any]:
    """Compare spatial slice/frequency/phase definitions across batches."""
    ref_info = tuple(reference.nii_img.header.get_dim_info())
    cur_info = tuple(current.nii_img.header.get_dim_info())
    if ref_info != cur_info:
        raise GeometryError(
            "Dimension definitions differ between %r %s and %r %s; "
            "frequency/phase/slice meanings must be identical on a time join"
            % (ref_id, ref_info, cur_id, cur_info)
        )
    if reference.meta_ext.slice_dim != current.meta_ext.slice_dim:
        raise GeometryError(
            "Slice dimension differs between %r and %r (%s vs %s)"
            % (ref_id, cur_id,
               reference.meta_ext.slice_dim, current.meta_ext.slice_dim)
        )
    return {
        "dim_info": list(ref_info),
        "slice_dim": reference.meta_ext.slice_dim,
    }


def _check_geometry(first: NiftiWrapper, current: NiftiWrapper,
                    atol: float, rtol: float,
                    first_id: str, current_id: str) -> Dict[str, Any]:
    """Compare two images in voxel and world space without modifying either."""
    first_shape = tuple(first.nii_img.shape)
    curr_shape = tuple(current.nii_img.shape)
    if first_shape[:3] != curr_shape[:3]:
        raise GeometryError(
            "Voxel grid mismatch between %r %s and %r %s"
            % (first_id, first_shape[:3], current_id, curr_shape[:3])
        )
    first_aff = np.asarray(first.nii_img.affine, dtype=float)
    curr_aff = np.asarray(current.nii_img.affine, dtype=float)
    if not np.allclose(first_aff, curr_aff, atol=atol, rtol=rtol):
        diff = curr_aff[:3, 3] - first_aff[:3, 3]
        if not np.allclose(first_aff[:3, :3], curr_aff[:3, :3],
                           atol=atol, rtol=rtol):
            raise GeometryError(
                "Voxel-to-world orientation/scale mismatch between %r and %r; "
                "batches are not in the same space (translation difference %s)"
                % (first_id, current_id, diff.tolist())
            )
        raise GeometryError(
            "Spatial origin shift of %s mm between %r and %r; a time-axis join "
            "would silently discard it. This tool never resamples: reacquire "
            "in the same position, or resample upstream to the reference grid "
            "and declare it with resampled=True and a resample_note."
            % (diff.tolist(), first_id, current_id)
        )
    return {
        "shape": list(first_shape[:3]),
        "affine_max_abs_diff": float(np.max(np.abs(curr_aff - first_aff))),
        "atol": atol,
        "rtol": rtol,
    }


def _declared_affine(wrapper: NiftiWrapper) -> Optional[np.ndarray]:
    """Return the affine recorded in the batch's own metadata, if any."""
    try:
        affine = wrapper.meta_ext.affine
    except Exception:
        return None
    affine = np.asarray(affine, dtype=float)
    if affine.shape != (4, 4) or not np.all(np.isfinite(affine)):
        return None
    return affine


def _check_resampling(wrapper: NiftiWrapper, spec: BatchSpec,
                      atol: float, rtol: float) -> Dict[str, Any]:
    """Refuse to treat already-resampled data as an original acquisition."""
    expected = spec.expected_affine
    source = "spec.expected_affine"
    if expected is None:
        expected = _declared_affine(wrapper)
        source = "dcmmeta_affine"
    actual = np.asarray(wrapper.nii_img.affine, dtype=float)
    info = {
        "source": source,
        "affine_max_abs_diff": (None if expected is None
                                else float(np.max(np.abs(expected - actual)))),
        "declared_resampled": bool(spec.resampled),
    }
    if expected is None:
        return info
    if not np.allclose(expected, actual, atol=atol, rtol=rtol):
        if not spec.resampled:
            raise ResamplingError(
                "Batch %r no longer matches its recorded source geometry; "
                "resampling is not performed implicitly. Pass the original "
                "data, or set resampled=True and describe the operation in "
                "resample_note." % spec.acquisition_id
            )
        info["accepted_because"] = "resampled=True: %s" % spec.resample_note
    return info


def _time_values(wrapper: NiftiWrapper, key: str) -> Optional[List[Any]]:
    """Return per-volume values for ``key``, or None when not per-sample."""
    values, classes = wrapper.meta_ext.get_values_and_class(key)
    if values is None:
        return None
    shape = wrapper.meta_ext.shape
    n_vols = shape[3] if len(shape) > 3 else 1
    if classes == ("time", "samples"):
        return list(values)
    if classes == ("global", "const"):
        return [values] if n_vols == 1 else None
    if classes == ("global", "slices"):
        n_slices = wrapper.meta_ext.n_slices or 1
        if n_slices and len(values) % n_slices == 0:
            n = len(values) // n_slices
            if n == n_vols:
                return [values[i * n_slices] for i in range(n)]
    return None


def _float_times(values: Optional[Sequence[Any]]) -> Optional[List[float]]:
    if values is None:
        return None
    result = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        result.append(number)
    return result


def _flatten_time(wrapper: NiftiWrapper) -> List[NiftiWrapper]:
    """Split a batch into 3D volumes without changing voxel values."""
    shape = tuple(wrapper.nii_img.shape)
    if len(shape) == 3:
        return [wrapper]
    if len(shape) == 4:
        return list(wrapper.split(3))
    if len(shape) == 5 and shape[4] == 1:
        # Drop the singleton vector axis first so the result is 4D, not a
        # five-dimensional array with two trailing singleton axes.
        no_vector = next(wrapper.split(4))
        return _flatten_time(no_vector)
    raise TimelineError(
        "Only 3D/4D batches (or 5D with a singleton vector axis) can be "
        "placed on a timeline; got shape %s" % (shape,)
    )


def _check_identities(specs: Sequence[BatchSpec]) -> None:
    seen = set()
    for spec in specs:
        if spec.acquisition_id in seen:
            raise IdentityError(
                "Duplicate acquisition_id %r; each batch needs a distinct "
                "identity even if it reuses a series UID"
                % spec.acquisition_id
            )
        seen.add(spec.acquisition_id)
    if len(specs) < 2:
        raise IdentityError("A timeline needs at least two batches")


def _infer_time_key(results: Sequence[BatchResult]) -> None:
    """Inherit a consistent time_dim already declared by every input batch."""
    if any(r.spec.time_key for r in results):
        return
    declared = [r.wrapper.meta_ext.time_dim for r in results]
    if declared and all(k is not None for k in declared) and len(set(declared)) == 1:
        key = declared[0]
        for r in results:
            r.spec.time_key = key


def _check_time_keys(results: Sequence[BatchResult]) -> Optional[str]:
    """Ensure the time-dimension definition is consistent across batches."""
    declared = {r.spec.acquisition_id: r.spec.time_key
                for r in results if r.spec.time_key}
    if len(set(declared.values())) > 1:
        raise IdentityError(
            "Batches declare different time keys (%s); normalise the time "
            "dimension before joining" % declared
        )
    key = next(iter(declared.values())) if declared else None
    for r in results:
        ext_time = r.wrapper.meta_ext.time_dim
        if ext_time is not None and key is not None and ext_time != key:
            raise IdentityError(
                "Batch %r is ordered by %r but the timeline declares %r"
                % (r.spec.acquisition_id, ext_time, key)
            )
        values = None
        if key is not None:
            values = _time_values(r.wrapper, key)
            if values is None:
                raise IdentityError(
                    "Time key %r is not per-sample in batch %r; the time "
                    "definition is inconsistent" % (key, r.spec.acquisition_id)
                )
        times = _float_times(values)
        if times is not None:
            if len(times) != r.n_volumes:
                raise IdentityError(
                    "Time key %r has %d values for %d volumes in batch %r"
                    % (key, len(times), r.n_volumes, r.spec.acquisition_id)
                )
            if r.n_volumes > 1 and any(
                b <= a for a, b in zip(times, times[1:])
            ):
                raise IdentityError(
                    "Time key %r is not strictly increasing in batch %r; "
                    "the declared time axis is ambiguous"
                    % (key, r.spec.acquisition_id)
                )
            r.source_times = times
    if key is not None and any(r.source_times is None for r in results):
        missing = [r.spec.acquisition_id for r in results
                   if r.source_times is None]
        raise IdentityError(
            "Time key %r is missing or not per-sample in batches %s; every "
            "batch on one timeline must define the time dimension the same way"
            % (key, missing)
        )
    return key


def _coverage_report(results: Sequence[BatchResult]) -> Dict[str, Any]:
    """Report gaps and overlaps without modifying or dropping samples.

    Gaps are computed from the union of the declared intervals, so a batch
    contained inside another's window does not create a phantom gap.  Every
    pairwise intersection is reported as a retained overlap; nothing is
    deduplicated.
    """
    ids = [r.spec.acquisition_id for r in results]
    coverage = [r.spec.coverage for r in results]
    for idx, cov in enumerate(coverage[:-1]):
        if cov.end is None:
            raise CoverageError(
                "Open-ended coverage for %r must be last; %r follows it"
                % (ids[idx], ids[idx + 1])
            )

    gaps: List[Dict[str, Any]] = []
    merged_end = coverage[0].end
    merged_end_id = ids[0]
    for idx in range(1, len(coverage)):
        left = coverage[idx]
        tol = max(left.tolerance, DEFAULT_TIME_ATOL)
        if left.start > merged_end + tol:
            gaps.append({
                "after": merged_end_id,
                "before": ids[idx],
                "start": merged_end,
                "end": left.start,
                "duration": left.start - merged_end,
                "status": "declared-gap",
            })
        if left.end is None or merged_end is None or left.end > merged_end:
            merged_end = left.end
            merged_end_id = ids[idx]

    overlaps: List[Dict[str, Any]] = []
    for i in range(len(coverage)):
        for j in range(i + 1, len(coverage)):
            first = coverage[i]
            second = coverage[j]
            if second.start > first.end + max(first.tolerance, second.tolerance):
                # Sorted by start: no later batch can intersect this one either.
                break
            end = (min(first.end, second.end)
                   if second.end is not None else first.end)
            start = max(first.start, second.start)
            duration = end - start
            if duration > max(first.tolerance, second.tolerance):
                overlaps.append({
                    "first": ids[i],
                    "second": ids[j],
                    "start": start,
                    "end": end,
                    "duration": duration,
                    "status": "retained-overlap",
                })

    last = coverage[-1]
    return {
        "start": coverage[0].start,
        "end": None if last.end is None else max(c.end for c in coverage
                                                 if c.end is not None),
        "declared_batches": len(coverage),
        "gaps": gaps,
        "overlaps": overlaps,
        "deduplicated": False,
    }


def _time_axis_for_parts(results: Sequence[BatchResult], time_key: Optional[str],
                         n_parts: int) -> Tuple[str, Dict[str, Any]]:
    """Choose an honest time dimension for the concatenated samples.

    A declared source time key is used only when every batch provides it and
    the concatenated values remain strictly increasing.  Overlapping batches
    frequently violate that (their windows are interleaved after concatenation);
    in that case the ordinal index is used and the source times are preserved
    as per-sample metadata, never sorted into a false order.
    """
    if time_key is None:
        return TIME_INDEX_KEY, {"reason": "no source time key declared"}
    if any(r.source_times is None for r in results):
        return TIME_INDEX_KEY, {
            "reason": "source time key missing in one or more batches",
            "source_key": time_key,
        }
    values = [t for r in results for t in r.source_times]
    if len(values) != n_parts:
        return TIME_INDEX_KEY, {
            "reason": "source time key does not provide one value per time point",
            "source_key": time_key,
        }
    if any(b <= a for a, b in zip(values, values[1:])):
        return TIME_INDEX_KEY, {
            "reason": "source times are not strictly increasing after "
                      "concatenation (overlapping or unordered batches)",
            "source_key": time_key,
        }
    return time_key, {"reason": "source times are strictly increasing"}


def build_timeline(
    batches: Iterable[Tuple[NiftiWrapper, BatchSpec]],
    *,
    sort: bool = True,
    atol: float = DEFAULT_GEOMETRY_ATOL,
    rtol: float = DEFAULT_GEOMETRY_RTOL,
) -> TimelineResult:
    """Concatenate batches along the time axis with explicit provenance.

    Parameters
    ----------
    batches
        Sequence of ``(NiftiWrapper, BatchSpec)`` pairs, a mapping of the same,
        or wrappers carrying a ``timeline_spec`` attribute.
    sort
        When true (default), batches are ordered by declared coverage start.
        When false, the given order is authoritative and must already be
        chronological.
    atol, rtol
        Tolerances for geometry comparison.

    Returns
    -------
    TimelineResult
        The merged wrapper and a JSON-compatible record of the checks,
        coverage, overlaps, and gaps.

    Raises
    ------
    IdentityError, GeometryError, ResamplingError, CoverageError
        Before any voxel data is copied.
    """
    pairs = _normalise_pairs(batches)
    specs = [spec for _, spec in pairs]
    _check_identities(specs)

    results: List[BatchResult] = []
    for wrapper, spec in pairs:
        _check_extension(wrapper, spec)
        n_vols = 1
        if len(wrapper.nii_img.shape) > 3:
            n_vols = int(wrapper.nii_img.shape[3])
        results.append(BatchResult(
            spec=spec,
            wrapper=wrapper,
            n_volumes=n_vols,
            affine=np.asarray(wrapper.nii_img.affine, dtype=float).copy(),
            shape=tuple(wrapper.nii_img.shape),
            geometry_check=_check_resampling(wrapper, spec, atol, rtol),
        ))

    if sort:
        results.sort(key=lambda r: (r.spec.coverage.start,
                                    r.spec.acquisition_id))
    else:
        for left, right in zip(results, results[1:]):
            if right.spec.coverage.start < left.spec.coverage.start:
                raise CoverageError(
                    "Batches are not in chronological order (%r starts before "
                    "%r); pass sort=True or reorder the declarations"
                    % (right.spec.acquisition_id,
                       left.spec.acquisition_id)
                )
    if results[-1].spec.coverage.end is None:
        for r in results[:-1]:
            if r.spec.coverage.end is None:
                raise CoverageError(
                    "Open-ended coverage for %r must be last"
                    % r.spec.acquisition_id
                )

    reference = results[0]
    for curr in results[1:]:
        _check_dimension_definition(
            reference.wrapper, curr.wrapper,
            reference.spec.acquisition_id, curr.spec.acquisition_id,
        )
        curr.geometry_check.update(_check_geometry(
            reference.wrapper, curr.wrapper, atol, rtol,
            reference.spec.acquisition_id, curr.spec.acquisition_id,
        ))
        curr_expected = _declared_affine(curr.wrapper)
        if (curr_expected is not None and not curr.spec.resampled and
                not np.allclose(curr_expected, reference.affine,
                                atol=atol, rtol=rtol)):
            raise GeometryError(
                "Batch %r declares a different source affine than %r; "
                "refusing to join without an explicit resample declaration"
                % (curr.spec.acquisition_id, reference.spec.acquisition_id)
            )

    _infer_time_key(results)
    time_key = _check_time_keys(results)
    coverage_report = _coverage_report(results)
    parts: List[NiftiWrapper] = []
    for r in results:
        parts.extend(_flatten_time(r.wrapper))
    if len(parts) < 2:
        raise TimelineError("The timeline must contain at least two time points")

    merged = NiftiWrapper.from_sequence(parts, 3)
    ext = merged.meta_ext
    # Old timeline records must not leak into the new one. The ordinal axis is
    # rebuilt for the new concatenation. Strip them from the merged result so
    # the caller's input wrappers are not mutated.
    for classes in ext.valid_classes:
        ext.get_class_dict(classes).pop(TIMELINE_KEY, None)
        ext.get_class_dict(classes).pop(TIME_INDEX_KEY, None)
    axis_key, axis_info = _time_axis_for_parts(results, time_key, len(parts))
    ext.get_class_dict(("time", "samples"))[TIME_INDEX_KEY] = list(range(len(parts)))
    if axis_key != time_key:
        axis_info["source_key"] = time_key
    time_key = axis_key

    record = _make_record(results, coverage_report, time_key, atol, rtol,
                          axis_info)
    ext.get_class_dict(("global", "const"))[TIMELINE_KEY] = record
    ext.time_dim = time_key
    ext.check_valid()
    return TimelineResult(wrapper=merged, record=record)


def _make_record(results: Sequence[BatchResult], coverage_report,
                 time_key: str, atol: float, rtol: float,
                 axis_info: Dict[str, Any]) -> Dict[str, Any]:
    batches = []
    cursor = 0
    for r in results:
        batches.append({
            **r.spec.as_dict(),
            "shape": list(r.shape),
            "affine": r.affine.tolist(),
            "geometry": r.geometry_check,
            "n_time_points": r.n_volumes,
            "time_slice": [cursor, cursor + r.n_volumes],
            "source_times": r.source_times,
        })
        cursor += r.n_volumes
    return {
        "schema": TIMELINE_KEY,
        "version": TIMELINE_VERSION,
        "time_key": time_key,
        "time_axis": axis_info,
        "geometry": {
            "reference_affine": results[0].affine.tolist(),
            "atol": atol,
            "rtol": rtol,
            "resampled": any(r.spec.resampled for r in results),
            "resampling_performed": False,
        },
        "coverage": coverage_report,
        "batches": batches,
        "n_time_points": cursor,
    }


def timeline_from_files(
    inputs: Sequence[Tuple[str, BatchSpec]],
    *,
    sort: bool = True,
    atol: float = DEFAULT_GEOMETRY_ATOL,
    rtol: float = DEFAULT_GEOMETRY_RTOL,
) -> TimelineResult:
    """Load Nifti files and build a timeline from ``(path, BatchSpec)`` pairs."""
    pairs = []
    for path, spec in inputs:
        wrapper = NiftiWrapper.from_filename(path)
        if spec.source is None or spec.source_paths is None:
            spec = replace(
                spec,
                source=spec.source or path,
                source_paths=spec.source_paths if spec.source_paths is not None else [path],
            )
        pairs.append((wrapper, spec))
    return build_timeline(pairs, sort=sort, atol=atol, rtol=rtol)


def parse_batch_specs(spec_path: str) -> List[Tuple[str, BatchSpec]]:
    """Read a JSON batch declaration file.

    The file must contain a list of objects with at least ``path``,
    ``acquisition_id``, and ``coverage`` (``[start, end]`` or an object with
    ``start``/``end``/``units``).  Other ``BatchSpec`` fields are passed
    through; unknown fields are rejected.
    """
    with open(spec_path, "r") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise CoverageError("Batch specification must be a JSON list")
    result = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            raise CoverageError("Batch specification entry %d is not an object" % idx)
        item = dict(item)
        path = item.pop("path", None)
        if path is None:
            raise CoverageError("Batch specification entry %d has no path" % idx)
        try:
            spec = BatchSpec(**item)
        except TypeError as exc:
            raise CoverageError(
                "Batch specification entry %d: %s" % (idx, exc)
            )
        result.append((path, spec))
    return result
