import itertools

import numpy as np

import nibabel
from nibabel.affines import apply_affine

from .tractogram import LazyTractogram, Tractogram, TractogramItem

# Provenance keys attached to the clipped tractogram when ``provenance=True``.
SOURCE_STREAMLINE_KEY = 'source_streamline'
SOURCE_SEGMENT_KEY = 'source_segment'
SEGMENT_POSITION_KEY = 'segment_position'
_PROVENANCE_KEYS = frozenset((SOURCE_STREAMLINE_KEY, SOURCE_SEGMENT_KEY, SEGMENT_POSITION_KEY))


def get_affine_from_reference(ref):
    """Returns the affine defining the reference space.

    Parameters
    ----------
    ref : str or :class:`Nifti1Image` object or ndarray shape (4, 4)
        If str then it's the filename of reference file that will be loaded
        using :func:`nibabel.load` in order to obtain the affine.
        If :class:`Nifti1Image` object then the affine is obtained from it.
        If ndarray shape (4, 4) then it's the affine.

    Returns
    -------
    affine : ndarray (4, 4)
        Transformation matrix mapping voxel space to RAS+mm space.
    """
    if hasattr(ref, 'affine'):
        return ref.affine

    if hasattr(ref, 'shape'):
        if ref.shape != (4, 4):
            msg = '`ref` needs to be a numpy array with shape (4, 4)!'
            raise ValueError(msg)

        return ref

    # Assume `ref` is the name of a neuroimaging file.
    return nibabel.load(ref).affine


def peek_next(iterable):
    """Peek next element of iterable.

    Parameters
    ----------
    iterable
        Iterable to peek the next element from.

    Returns
    -------
    next_item
        Element peeked from `iterable`.
    new_iterable
        Iterable behaving like if the original `iterable` was untouched.
    """
    next_item = next(iterable)
    return next_item, itertools.chain([next_item], iterable)


def _normalize_roi(mask, affine):
    """Return a boolean voxel mask and its voxel-to-world affine."""
    if hasattr(mask, 'affine') and hasattr(mask, 'shape'):
        # SpatialImage-like object: use the image affine when none is given.
        if affine is None:
            affine = mask.affine
        mask_data = np.asarray(mask.dataobj) if hasattr(mask, 'dataobj') else np.asarray(mask)
    else:
        mask_data = np.asarray(mask)

    if mask_data.ndim != 3:
        msg = (
            'The ROI mask must be a 3D voxel mask, but an array with shape '
            f'{mask_data.shape} was provided.'
        )
        raise ValueError(msg)

    bool_mask = mask_data if mask_data.dtype == bool else mask_data != 0
    bool_mask = np.ascontiguousarray(bool_mask, dtype=bool)

    if affine is None:
        msg = (
            'An ROI affine is required when `mask` is a plain ndarray. '
            'Pass the voxel-to-world affine or provide a spatial image as the mask.'
        )
        raise ValueError(msg)
    roi_affine = np.asarray(get_affine_from_reference(affine), dtype=np.float64)

    return bool_mask, roi_affine


def _point_inside_mask(point_vox, mask):
    """Flag a single voxel-space point as inside or outside a mask."""
    index = np.floor(point_vox + 0.5).astype(np.intp)
    if np.any(index < 0) or np.any(index >= np.array(mask.shape)):
        return False
    return bool(mask[index[0], index[1], index[2]])


def _segment_face_parameters(p0, p1, segment_index):
    """Global parameters where segment ``p0 -> p1`` meets a voxel face.

    Voxel faces lie at half-integer voxel coordinates. Both segment endpoints
    are included when they sit on a face (a vertex shared with the next segment
    is reported through both segments and de-duplicated later).
    """
    delta = p1 - p0
    crossings = []
    for axis in range(3):
        step = delta[axis]
        if step == 0.0:
            continue
        low, high = (p0[axis], p1[axis]) if step > 0 else (p1[axis], p0[axis])
        first_k = np.ceil(low - 0.5)
        last_k = np.floor(high - 0.5)
        if last_k < first_k:
            continue
        planes = np.arange(first_k, last_k + 1.0, dtype=np.intp) + 0.5
        planes = planes[(planes >= low) & (planes <= high)]
        for plane in planes:
            local = (plane - p0[axis]) / step
            local = min(1.0, max(0.0, float(local)))
            crossings.append(segment_index + local)
    return crossings


def _point_at_parameter(points_vox, global_param):
    """Point of the polyline at a global parameter in ``[0, N - 1]``."""
    nb_points = len(points_vox)
    if global_param >= nb_points - 1:
        return points_vox[-1]
    segment_index = int(np.floor(global_param))
    local = global_param - segment_index
    return (1.0 - local) * points_vox[segment_index] + local * points_vox[segment_index + 1]


def _interval_is_inside(points_vox, start, end, mask):
    """Membership of a parameter interval containing no voxel face.

    The interval midpoint can land on a voxel face only through floating-point
    round-off (e.g. a segment running along a face), so coordinates within
    numerical tolerance of a half-integer face are snapped onto it before
    classification. Membership then follows the voxel-cell convention where
    the face belongs to the voxel on its positive side.
    """
    midpoint = _point_at_parameter(points_vox, (start + end) / 2.0)
    nearest_face = np.round(midpoint - 0.5) + 0.5
    on_face = np.abs(midpoint - nearest_face) < 1e-10
    midpoint = np.where(on_face, nearest_face, midpoint)
    return _point_inside_mask(midpoint, mask)


def _merge_parameters(parameters, tolerance=1e-12):
    """Sort global parameters and merge duplicates up to a tolerance."""
    merged = []
    for parameter in sorted(parameters):
        if merged and abs(parameter - merged[-1]) < tolerance:
            continue
        merged.append(parameter)
    return merged


def _inside_intervals(points_vox, mask):
    """Global-parameter intervals ``(enter, leave)`` lying inside the mask.

    Candidate parameters are the original vertices plus every voxel-face
    intersection, so mask membership is constant between consecutive
    candidates; membership is probed at each interval midpoint. Including
    face intersections located exactly at vertices guarantees that a segment
    leaving the mask from a vertex on a face is detected.
    """
    nb_points = len(points_vox)
    if nb_points == 1:
        if _point_inside_mask(points_vox[0], mask):
            return [(0.0, 0.0)]
        return []

    parameters = [float(i) for i in range(nb_points)]
    for segment_index in range(nb_points - 1):
        parameters.extend(
            _segment_face_parameters(
                points_vox[segment_index], points_vox[segment_index + 1], segment_index
            )
        )
    parameters = _merge_parameters(parameters)

    intervals = []
    enter = None
    for index in range(len(parameters) - 1):
        start, end = parameters[index], parameters[index + 1]
        if _interval_is_inside(points_vox, start, end, mask):
            if enter is None:
                enter = start
        elif enter is not None:
            # The inside portion ends at the first outside parameter.
            intervals.append((enter, start))
            enter = None

    if enter is not None:
        intervals.append((enter, parameters[-1]))
    return intervals


def _is_vertex_parameter(global_param):
    """True if a global parameter coincides with an original vertex."""
    return abs(global_param - round(global_param)) < 1e-12


def _extract_fragment_events(points_vox, mask):
    """Split one streamline into inside-mask fragments.

    Each fragment is a list of events ordered along the streamline. An event
    is a tuple ``(global_param, vertex_index, segment_index, local_param)``:
    an original vertex has ``vertex_index >= 0`` and ``local_param == 0``;
    a newly created boundary point has ``vertex_index == -1`` and records the
    segment it cuts and its local parameter in ``[0, 1]``.
    """
    fragments = []
    for start, end in _inside_intervals(points_vox, mask):
        events = []

        if not _is_vertex_parameter(start):
            segment_index = int(np.floor(start))
            events.append((start, -1, segment_index, start - segment_index))

        first_vertex = int(np.ceil(start - 1e-12))
        last_vertex = int(np.floor(end + 1e-12))
        events.extend(
            (float(vertex_index), vertex_index, -1, 0.0)
            for vertex_index in range(first_vertex, last_vertex + 1)
        )

        if not _is_vertex_parameter(end):
            segment_index = int(np.floor(end))
            events.append((end, -1, segment_index, end - segment_index))

        events.sort(key=lambda event: event[0])
        fragments.append(events)
    return fragments


def _build_fragment(item, events, categorical_keys, provenance):
    """Build a :class:`TractogramItem` for one clipped fragment."""
    points = np.asarray(item.streamline)
    nb_events = len(events)

    # Boundary points can fall between integer vertex coordinates, so integer
    # input coordinates are promoted rather than truncated. Fragments made only
    # of original vertices keep the input dtype.
    has_boundary_point = any(vertex_index < 0 for _, vertex_index, _, _ in events)
    if np.issubdtype(points.dtype, np.floating) or not has_boundary_point:
        point_dtype = points.dtype
    else:
        point_dtype = np.float64
    new_points = np.empty((nb_events, points.shape[1]), dtype=point_dtype)
    new_data_per_point = {}
    for key, data in item.data_for_points.items():
        data = np.asarray(data)
        new_data_per_point[key] = np.empty((nb_events,) + data.shape[1:], dtype=data.dtype)

    source_segments = np.full(nb_events, -1, dtype=np.intp)
    segment_positions = np.zeros(nb_events, dtype=np.float64)

    for row, (_, vertex_index, segment_index, t) in enumerate(events):
        if vertex_index >= 0:
            new_points[row] = points[vertex_index]
            for key, data in item.data_for_points.items():
                new_data_per_point[key][row] = np.asarray(data)[vertex_index]
        else:
            new_points[row] = (1.0 - t) * points[segment_index] + t * points[segment_index + 1]
            for key, data in item.data_for_points.items():
                data = np.asarray(data)
                if key in categorical_keys or not np.issubdtype(data.dtype, np.floating):
                    # Categorical data: keep the label of the segment start,
                    # no intermediate label is ever created.
                    new_data_per_point[key][row] = data[segment_index]
                else:
                    value = (1.0 - t) * data[segment_index] + t * data[segment_index + 1]
                    new_data_per_point[key][row] = value.astype(data.dtype, copy=False)
            source_segments[row] = segment_index
            segment_positions[row] = t

    if provenance:
        new_data_per_point[SOURCE_SEGMENT_KEY] = source_segments.reshape(-1, 1)
        new_data_per_point[SEGMENT_POSITION_KEY] = segment_positions.reshape(-1, 1)

    return TractogramItem(new_points, dict(item.data_for_streamline), new_data_per_point)


def _iter_fragment_items(tractogram, mask, tractogram_to_voxel, categorical_keys, provenance):
    """Yield ``(source_index, fragment TractogramItem)`` pairs."""
    for source_index, item in enumerate(tractogram):
        points = np.asarray(item.streamline, dtype=np.float64)
        if points.ndim != 2 or points.shape[1] != 3:
            msg = (
                'Streamlines must contain 3D points, got an array with shape '
                f'{points.shape} for streamline {source_index}.'
            )
            raise ValueError(msg)

        points_vox = apply_affine(tractogram_to_voxel, points)
        for events in _extract_fragment_events(points_vox, mask):
            fragment = _build_fragment(item, events, categorical_keys, provenance)
            yield source_index, fragment


def clip_tractogram(tractogram, mask, affine=None, *, categorical_keys=None, provenance=True):
    """Clip streamlines to the parts intersecting an ROI voxel mask.

    The clipping follows the actual line segments of every streamline: points
    are not merely filtered. A segment with both endpoints outside the ROI is
    still cut whenever it crosses the mask, and new boundary points are inserted
    at the exact intersection parameters. A streamline that leaves and re-enters
    the ROI yields one fragment per connected intersection.

    Parameters
    ----------
    tractogram : :class:`Tractogram` or :class:`LazyTractogram`
        Streamlines to clip, together with their ``data_per_streamline`` and
        ``data_per_point``. The tractogram must define ``affine_to_rasmm`` so
        its points can be located in the ROI voxel space.
    mask : 3D ndarray or spatial image
        Boolean (or binary) voxel mask defining the ROI. Non-zero voxels are
        considered inside. A spatial image carries its own voxel-to-world
        affine; a plain ndarray requires the `affine` argument.
    affine : ndarray shape (4, 4) or spatial image or str, optional
        Voxel-to-world transformation of the mask, mapping voxel coordinates
        (integer coordinates point at voxel centers) to RAS+mm. Required when
        `mask` is a plain ndarray; when both are given, this affine takes
        precedence over the mask image's own affine.
    categorical_keys : iterable of str, optional
        ``data_per_point`` keys to treat as categorical. Such data is never
        interpolated: a new boundary point receives the label of the segment's
        start vertex. All non-floating-point data is categorical by default;
        use this argument for float-encoded labels.
    provenance : bool, optional
        If True, attach ``source_streamline`` (per fragment, index of the
        original streamline), ``source_segment`` (per point, index of the
        original segment cut by a boundary point, -1 for original vertices)
        and ``segment_position`` (per point, local parameter in [0, 1] of the
        boundary point on its segment, 0 for original vertices).

    Returns
    -------
    clipped : :class:`Tractogram` or :class:`LazyTractogram`
        Fragments lying inside the ROI, in the same coordinate space as the
        input. Per-streamline data is copied unchanged onto every fragment of
        a streamline; per-point data of original vertices is preserved and
        floating-point per-point data is linearly interpolated at new boundary
        points, while categorical data keeps the start-of-segment label.
        A :class:`LazyTractogram` input returns a lazy tractogram; any other
        tractogram returns an in-memory tractogram.

    Notes
    -----
    Empty intersection and missing data are distinct situations:

    * **Empty intersection** -- every streamline misses the ROI. An empty
      tractogram (zero fragments) is returned, carrying the same
      ``affine_to_rasmm`` as the input; detect it with ``len(clipped) == 0``.
      Individual streamlines without an intersection simply contribute no
      fragment.
    * **Missing data** -- a tractogram without ``data_per_point`` or
      ``data_per_streamline`` is clipped normally, only provenance data is
      attached when requested. A tractogram without ``affine_to_rasmm``, or a
      plain-ndarray mask without an affine, raises :class:`ValueError` because
      the ROI cannot be located in space.

    Reversing a streamline (points and per-point data in reverse order) and
    clipping again yields the same spatial coverage, i.e. the same set of
    fragment points up to floating-point precision.
    """
    bool_mask, roi_affine = _normalize_roi(mask, affine)

    affine_to_rasmm = tractogram.affine_to_rasmm
    if affine_to_rasmm is None:
        msg = (
            'Cannot clip a tractogram with an unknown coordinate space: set '
            '`affine_to_rasmm` before calling `clip_tractogram`.'
        )
        raise ValueError(msg)

    categorical = frozenset(categorical_keys or ())
    if provenance and (
        categorical & _PROVENANCE_KEYS
        or SOURCE_STREAMLINE_KEY in tractogram.data_per_streamline
        or any(key in tractogram.data_per_point for key in _PROVENANCE_KEYS)
    ):
        msg = (
            'Provenance key collision: the input tractogram already uses one '
            f'of {sorted(_PROVENANCE_KEYS)}. Pass provenance=False or rename '
            'the conflicting data.'
        )
        raise ValueError(msg)

    tractogram_to_voxel = np.linalg.inv(roi_affine) @ affine_to_rasmm

    if isinstance(tractogram, LazyTractogram):

        def generator():
            for source_index, fragment in _iter_fragment_items(
                tractogram, bool_mask, tractogram_to_voxel, categorical, provenance
            ):
                if provenance:
                    fragment.data_for_streamline[SOURCE_STREAMLINE_KEY] = np.array(
                        [source_index], dtype=np.intp
                    )
                yield fragment

        clipped = LazyTractogram.from_data_func(generator)
        clipped.affine_to_rasmm = affine_to_rasmm
        return clipped

    fragment_streamlines = []
    fragment_data_per_streamline = {}
    fragment_data_per_point = {}
    source_streamlines = []

    for source_index, fragment in _iter_fragment_items(
        tractogram, bool_mask, tractogram_to_voxel, categorical, provenance
    ):
        fragment_streamlines.append(fragment.streamline)
        for key, value in fragment.data_for_streamline.items():
            fragment_data_per_streamline.setdefault(key, []).append(np.asarray(value))
        for key, value in fragment.data_for_points.items():
            fragment_data_per_point.setdefault(key, []).append(np.asarray(value))
        source_streamlines.append(source_index)

    clipped_kwargs = {'affine_to_rasmm': affine_to_rasmm}
    if fragment_data_per_streamline:
        clipped_kwargs['data_per_streamline'] = fragment_data_per_streamline
    if fragment_data_per_point:
        clipped_kwargs['data_per_point'] = fragment_data_per_point

    clipped = Tractogram(fragment_streamlines, **clipped_kwargs)

    if provenance and source_streamlines:
        clipped.data_per_streamline[SOURCE_STREAMLINE_KEY] = np.asarray(
            source_streamlines, dtype=np.intp
        ).reshape(-1, 1)

    return clipped
