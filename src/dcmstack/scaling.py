"""Utilities for handling DICOM modality LUT scaling factors.

DICOM data sets may specify a linear mapping from the stored pixel codes to
"real world" (engineering) values with *RescaleSlope* and *RescaleIntercept*.
The mapping can vary between frames of a single enhanced (multi-frame) data
set and, in the case of a series assembled from many single frame files, it
can vary from slice to slice.

Nifti headers can only carry a single ``scl_slope``/``scl_inter`` pair.  The
helpers here keep the per-frame pair together with the raw codes so that a
caller can decide whether that single pair can represent the data losslessly,
and fall back to materializing the engineering values when it can not.
"""
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence, Tuple

import numpy as np


DEFAULT_RESCALE_SLOPE = 1.0
DEFAULT_RESCALE_INTERCEPT = 0.0
'''Compatibility defaults for absent modality LUT coefficients.

DICOM PS3.3 C.11.1.1.2 defines these defaults.  They are deliberately named
here so that callers can distinguish a coefficient that was absent from one
that was measured to have the default value.
'''

SCALING_MODE_NONE = 'none'
SCALING_MODE_HEADER = 'header'
SCALING_MODE_MATERIALIZED = 'materialized'
'''The ways an assembled array can preserve its scaling information.'''


class LosslessScalingError(ValueError):
    '''Raised when scaling can not be represented without rounding.'''


@dataclass(frozen=True)
class ScaleFactor:
    '''A single modality LUT scaling pair and the provenance of its values.

    Parameters
    ----------
    slope
        The effective rescale slope.

    intercept
        The effective rescale intercept.

    slope_present
        True if the slope came from the DICOM data.  False means the
        compatibility default was used because the coefficient was absent.

    intercept_present
        True if the intercept came from the DICOM data.  False means the
        compatibility default was used because the coefficient was absent.
    '''
    slope: float
    intercept: float
    slope_present: bool = True
    intercept_present: bool = True

    def as_tuple(self) -> Tuple[float, float]:
        '''Return the ``(slope, intercept)`` pair.'''
        return (self.slope, self.intercept)

    def is_identity(self) -> bool:
        '''Return True if this pair leaves the stored codes unchanged.'''
        return self.slope == 1.0 and self.intercept == 0.0

    def is_lossless_in_nifti_header(self) -> bool:
        '''Return True if both coefficients survive a Nifti float32 field.

        The Nifti ``scl_slope`` and ``scl_inter`` fields are 32 bit floats.
        If either coefficient is not exactly representable in that format,
        writing it into the header silently changes the mapping and rounds
        the engineering values.  A zero slope is also rejected: Nifti treats
        ``scl_slope`` of zero as "no scaling" and would drop the intercept.
        '''
        if self.slope == 0:
            return False
        return (is_float32_exact(self.slope) and
                is_float32_exact(self.intercept))


@dataclass(frozen=True)
class NiftiScalingPlan:
    '''How the assembled data will carry its DICOM scaling into Nifti.

    Parameters
    ----------
    mode
        One of ``'none'`` (identity scaling, raw codes written), ``'header'``
        (raw codes written with a single lossless ``scl_slope``/``scl_inter``
        pair) or ``'materialized'`` (engineering values written directly).

    slope, intercept
        The effective pair when a single pair applies to every frame.

    header_lossless
        True if a Nifti header pair would preserve every value exactly.
        False means a header pair would silently round or could not encode
        the per-frame variation.

    reason
        Human readable explanation of the choice.

    single_factor
        True when all frames share one effective pair.  Together with
        ``header_lossless`` this distinguishes "one pair could not be held in
        the header" from "several different pairs were materialized".
    '''
    mode: str
    slope: Optional[float] = None
    intercept: Optional[float] = None
    header_lossless: bool = False
    factors: Tuple[ScaleFactor, ...] = field(default_factory=tuple)
    reason: str = ''
    single_factor: bool = True

    @property
    def is_lossless(self) -> bool:
        '''Return True if the selected representation does not round values.'''
        if self.mode == SCALING_MODE_NONE:
            return True
        if self.mode == SCALING_MODE_HEADER:
            return self.header_lossless
        # Materializing keeps the full precision of the conversion.
        return True


def is_float32_exact(value: float) -> bool:
    '''Return True if `value` can be stored in a Nifti float32 field exactly.'''
    if value is None:
        return False
    value = float(value)
    if not np.isfinite(value):
        return False
    return float(np.float32(value)) == value


def _get(ds, key, default=None):
    if ds is None:
        return default
    try:
        return ds.get(key, default)
    except (AttributeError, TypeError):
        try:
            return ds[key]
        except (KeyError, TypeError):
            return default


def _dataset_presence(ds) -> Tuple[bool, bool]:
    '''Return which coefficients were explicitly supplied by a dataset.'''
    slope_present = _get(ds, 'RescaleSlope') is not None
    intercept_present = _get(ds, 'RescaleIntercept') is not None
    # Enhanced objects may put the mapping in the pixel value transformation
    # sequence instead of at the top level.
    pvt_seq = _get(ds, 'PixelValueTransformationSequence')
    if pvt_seq:
        try:
            pvt = pvt_seq[0]
        except (IndexError, TypeError):
            pvt = None
        if pvt is not None:
            slope_present = slope_present or _get(pvt, 'RescaleSlope') is not None
            intercept_present = (
                intercept_present or _get(pvt, 'RescaleIntercept') is not None
            )
    # Real world value mapping also defines an explicit linear mapping.
    rwv_seq = _get(ds, 'RealWorldValueMappingSequence')
    if rwv_seq:
        try:
            rwv = rwv_seq[0]
        except (IndexError, TypeError):
            rwv = None
        if rwv is not None:
            slope_present = (
                slope_present or _get(rwv, 'RealWorldValueSlope') is not None
            )
            intercept_present = (
                intercept_present or _get(rwv, 'RealWorldValueIntercept') is not None
            )
    return bool(slope_present), bool(intercept_present)


def _factor_from_values(values, presence) -> ScaleFactor:
    slope, intercept = (float(values[0]), float(values[1]))
    slope_present, intercept_present = presence
    # Some DICOM objects omit a coefficient but still define a non-default
    # effective value (a vendor private element or a sequence default).  In
    # that case the value was measured even though the top level tag was not
    # present.
    if slope != DEFAULT_RESCALE_SLOPE:
        slope_present = True
    if intercept != DEFAULT_RESCALE_INTERCEPT:
        intercept_present = True
    return ScaleFactor(slope, intercept, slope_present, intercept_present)


def _data_sources(dcm_wrp) -> Tuple[List, List]:
    '''Return ``(per_frame_sources, shared_sources)`` for presence testing.'''
    if getattr(dcm_wrp, 'is_multiframe', False):
        frames = list(getattr(dcm_wrp, 'frames', None) or [])
        shared = getattr(dcm_wrp, 'shared', None)
        shared_sources = [src for src in (shared, getattr(dcm_wrp, 'dcm_data', None))
                          if src is not None]
        return frames, shared_sources
    return [getattr(dcm_wrp, 'dcm_data', None)], []


def get_scale_factors(dcm_wrp) -> List[ScaleFactor]:
    '''Get the effective scaling pair for each frame of a DicomWrapper.

    The values come from ``dcm_wrp.scale_factors``, which applies Nibabel's
    precedence rules (real world value mapping, pixel value transformation,
    legacy rescale tags, and vendor private elements).  Presence is derived
    separately so an absent coefficient can be reported as defaulted rather
    than as a measured zero.

    Returns
    -------
    factors
        A list with one `ScaleFactor` per frame.  A single global pair is
        returned as a one element list.
    '''
    raw = np.asarray(dcm_wrp.scale_factors, dtype=np.float64)
    if raw.ndim == 1:
        raw = raw.reshape(1, 2)
    # Nibabel's documentation says (2, N) while its implementations return
    # (N, 2).  Accept either orientation.
    if raw.shape[1] != 2 and raw.shape[0] == 2:
        raw = raw.T
    if raw.shape[0] == 0:
        raw = np.array([[DEFAULT_RESCALE_SLOPE, DEFAULT_RESCALE_INTERCEPT]])

    frame_sources, shared_sources = _data_sources(dcm_wrp)
    if raw.shape[0] > 1 and len(frame_sources) == raw.shape[0]:
        # Nibabel returns per-frame factors in array order, applying its
        # frame_order permutation.  Reorder the presence sources to match so
        # a defaulted coefficient is attributed to the right frame.
        frame_order = getattr(dcm_wrp, 'frame_order', None)
        if frame_order is not None and len(frame_order) == len(frame_sources):
            try:
                frame_sources = [frame_sources[int(i)] for i in frame_order]
            except (IndexError, TypeError, ValueError):
                pass
    shared_presence = [False, False]
    for src in shared_sources:
        presence = _dataset_presence(src)
        shared_presence[0] = shared_presence[0] or presence[0]
        shared_presence[1] = shared_presence[1] or presence[1]

    if raw.shape[0] == 1:
        presence = tuple(shared_presence)
        for src in frame_sources:
            frame_presence = _dataset_presence(src)
            presence = (
                presence[0] or frame_presence[0],
                presence[1] or frame_presence[1],
            )
        return [_factor_from_values(raw[0], presence)]

    factors = []
    for idx, values in enumerate(raw):
        presence = tuple(shared_presence)
        if idx < len(frame_sources):
            frame_presence = _dataset_presence(frame_sources[idx])
            presence = (
                presence[0] or frame_presence[0],
                presence[1] or frame_presence[1],
            )
        factors.append(_factor_from_values(values, presence))
    return factors


def factor_shape_for_data(data_shape, n_factors: int) -> Optional[Tuple[int, ...]]:
    '''Find the trailing dimensions that a flat factor list maps onto.'''
    if len(data_shape) <= 2:
        return ()
    trailing = tuple(data_shape[2:])
    if int(np.prod(trailing)) == n_factors:
        return trailing
    if len(data_shape) == 3 and data_shape[2] == n_factors:
        return (data_shape[2],)
    if data_shape[-1] == n_factors:
        return (data_shape[-1],)
    return None


def apply_scale_factor(data, factor: ScaleFactor, dtype=np.float64):
    '''Apply a single scaling pair to raw codes, returning a new array.'''
    work_type = np.dtype(dtype).type
    values = data.astype(work_type, copy=False)
    if factor.is_identity():
        return values
    if factor.intercept == 0.0:
        return values * work_type(factor.slope)
    return values * work_type(factor.slope) + work_type(factor.intercept)


def apply_scale_factors(data, factors, factor_shape: Optional[Tuple[int, ...]] = None,
                        dtype=np.float64):
    '''Apply per-frame scaling pairs to a raw code array.

    ``factors`` is a sequence of `ScaleFactor`.  When more than one pair is
    supplied, ``factor_shape`` describes how the pairs map onto the trailing
    dimensions of ``data`` (for example ``(z,)`` for one pair per slice, or
    ``(z, t)`` for one pair per frame).  The pairs are in C order over those
    dimensions, matching Nibabel's multiframe rescaling.
    '''
    if isinstance(factors, ScaleFactor):
        factors = [factors]
    factors = list(factors)
    if not factors:
        return data.astype(dtype, copy=False)
    if len(factors) == 1:
        return apply_scale_factor(data, factors[0], dtype=dtype)

    if factor_shape is None:
        factor_shape = factor_shape_for_data(data.shape, len(factors))
    if factor_shape is None or int(np.prod(factor_shape)) != len(factors):
        raise LosslessScalingError(
            "The number of scaling pairs does not match the data dimensions"
        )
    slopes = np.array([f.slope for f in factors], dtype=dtype).reshape(factor_shape)
    inters = np.array([f.intercept for f in factors], dtype=dtype).reshape(factor_shape)
    view = (1, 1) + tuple(factor_shape)
    return data.astype(dtype, copy=False) * slopes.reshape(view) + inters.reshape(view)


def downcast_float_if_exact(data):
    '''Return float32 when every value is exactly representable, else float64.

    This is used for materialized engineering values so that a lossy cast is
    never performed silently.  Integer valued results usually fit float32 and
    keep the historical dtype, while high precision slopes stay in float64.
    '''
    if not isinstance(data, np.ndarray) or data.dtype != np.float64:
        return data
    if data.size == 0:
        return data.astype(np.float32)
    if not np.all(np.isfinite(data)):
        return data
    narrowed = data.astype(np.float32)
    if np.array_equal(narrowed.astype(np.float64), data):
        return narrowed
    return data


def unique_factor_values(factors: Iterable[ScaleFactor]) -> List[Tuple[float, float]]:
    '''Return the unique ``(slope, intercept)`` values in first-seen order.'''
    seen = set()
    result = []
    for factor in factors:
        key = factor.as_tuple()
        if key not in seen:
            seen.add(key)
            result.append(key)
    return result


def subset_scale_factors(factors, frame_shape, dim, idx):
    '''Select the scaling pairs for one index along `dim`.

    ``factors`` is a flat C-order list over ``frame_shape``, where
    ``frame_shape`` covers the trailing (non-spatial) frame dimensions.
    Returns ``(subset_factors, subset_shape)``.  When `dim` is an in-plane
    dimension the factors are unchanged, since each frame still has its own
    pair.
    '''
    factor_list = list(factors)
    if len(factor_list) == 1:
        return factor_list, frame_shape
    if frame_shape is None:
        return factor_list, frame_shape
    shape = tuple(int(x) for x in frame_shape)
    if int(np.prod(shape)) != len(factor_list):
        return factor_list, frame_shape
    # The frame dimensions start after the three spatial axes.
    frame_dim = dim - 3
    if frame_dim < 0 or frame_dim >= len(shape):
        return factor_list, frame_shape
    arrays = {
        'slope': np.asarray([f.slope for f in factor_list]).reshape(shape),
        'inter': np.asarray([f.intercept for f in factor_list]).reshape(shape),
        'sp': np.asarray([f.slope_present for f in factor_list]).reshape(shape),
        'ip': np.asarray([f.intercept_present for f in factor_list]).reshape(shape),
    }
    taken = {k: np.take(v, idx, axis=frame_dim) for k, v in arrays.items()}
    new_shape = shape[:frame_dim] + shape[frame_dim + 1:]
    subset = [
        ScaleFactor(
            float(taken['slope'].ravel()[i]),
            float(taken['inter'].ravel()[i]),
            bool(taken['sp'].ravel()[i]),
            bool(taken['ip'].ravel()[i]),
        )
        for i in range(taken['slope'].size)
    ]
    return subset, new_shape


def get_nifti_scale_factor(nii_img) -> ScaleFactor:
    '''Get the scaling pair attached to a Nifti image.

    Nibabel keeps an in-memory header pair on the header, but moves it to the
    array proxy when a file is loaded.  Both locations are checked so split
    and merge can preserve the original mapping.
    '''
    hdr = nii_img.header
    slope = inter = None
    try:
        slope, inter = hdr.get_slope_inter()
    except Exception:
        slope = inter = None
    if slope is not None:
        return ScaleFactor(float(slope), 0.0 if inter is None else float(inter))
    proxy = getattr(nii_img, 'dataobj', None)
    slope = getattr(proxy, 'slope', None)
    inter = getattr(proxy, 'inter', None)
    if slope is not None:
        try:
            slope = float(slope)
            inter = 0.0 if inter is None else float(inter)
        except (TypeError, ValueError):
            return ScaleFactor(DEFAULT_RESCALE_SLOPE,
                               DEFAULT_RESCALE_INTERCEPT,
                               False,
                               False)
        if np.isfinite(slope) and slope != 0:
            return ScaleFactor(slope, inter)
    return ScaleFactor(DEFAULT_RESCALE_SLOPE,
                       DEFAULT_RESCALE_INTERCEPT,
                       False,
                       False)


def frame_shape_from_meta(meta_ext) -> Optional[Tuple[int, ...]]:
    '''Read ``Scaling.FrameShape`` from a meta extension, if present.'''
    for classes in meta_ext.valid_classes:
        dest = meta_ext.get_class_dict(classes)
        if 'Scaling.FrameShape' in dest:
            return tuple(int(x) for x in dest['Scaling.FrameShape'])
    return None


def get_wrapper_scale_info(wrapper):
    '''Get ``(factors, frame_shape, mode)`` for a NiftiWrapper.

    The in-memory attributes are preferred, then the recorded meta data, then
    the Nifti header pair.  This lets a wrapper loaded from a file retain the
    per-slice factors and the raw-vs-materialized distinction.
    '''
    factors = getattr(wrapper, 'scale_factors', None)
    frame_shape = None
    mode = None
    plan = getattr(wrapper, 'scaling_plan', None)
    if plan is not None:
        mode = plan.mode

    meta_ext = getattr(wrapper, 'meta_ext', None)
    if meta_ext is not None:
        recorded = scale_factors_from_meta(meta_ext)
        if recorded:
            factors = list(recorded) if not factors else list(factors)
        recorded_shape = frame_shape_from_meta(meta_ext)
        if recorded_shape is not None:
            frame_shape = recorded_shape
        encoding, _ = meta_ext.get_values_and_class('Scaling.DataEncoding')
        if encoding is not None:
            mode = encoding

    if not factors:
        factors = [get_nifti_scale_factor(wrapper.nii_img)]
    if mode is None:
        mode = make_scaling_plan(factors).mode
    return list(factors), frame_shape, mode


def get_nifti_raw_data(nii_img):
    '''Get the stored codes from a Nifti image without applying scaling.'''
    proxy = getattr(nii_img, 'dataobj', None)
    if hasattr(proxy, 'get_unscaled'):
        return np.asanyarray(proxy.get_unscaled())
    return np.asanyarray(proxy)


def get_wrapper_raw_data(wrapper):
    '''Get the stored codes from a NiftiWrapper.

    When a wrapper had to materialize engineering values, the original codes
    are kept on its ``raw_data`` attribute.  Otherwise the stored codes live
    in the wrapped Nifti image.
    '''
    raw = getattr(wrapper, 'raw_data', None)
    if raw is not None:
        return np.asanyarray(raw)
    return get_nifti_raw_data(wrapper.nii_img)


def scale_factors_from_meta(meta_ext) -> List[ScaleFactor]:
    '''Read the recorded effective scaling pairs from a meta extension.

    Returns the pairs in the order recorded, or an empty list if the
    extension has no recorded scaling information.  A single recorded pair is
    returned as a one element list.
    '''
    values, classes = meta_ext.get_values_and_class('Scaling.RescaleSlope')
    if classes is None:
        values, classes = meta_ext.get_values_and_class(
            'Scaling.FrameRescaleSlope'
        )
        if classes is None:
            return []
        inters, _ = meta_ext.get_values_and_class(
            'Scaling.FrameRescaleIntercept'
        )
        slopes_present, _ = meta_ext.get_values_and_class(
            'Scaling.FrameRescaleSlopePresent'
        )
        inters_present, _ = meta_ext.get_values_and_class(
            'Scaling.FrameRescaleInterceptPresent'
        )
    else:
        inters, _ = meta_ext.get_values_and_class(
            'Scaling.RescaleIntercept'
        )
        slopes_present, _ = meta_ext.get_values_and_class(
            'Scaling.RescaleSlopePresent'
        )
        inters_present, _ = meta_ext.get_values_and_class(
            'Scaling.RescaleInterceptPresent'
        )
    # Per-slice values in a 4D+ extension are stored slice-major, not in the
    # C order this function promises.  Return nothing for that case rather
    # than hand back a list that would be applied to the wrong frames.
    if classes[1] == 'slices' and len(meta_ext.shape) > 3:
        return []

    def as_list(value):
        if isinstance(value, (list, tuple)):
            return list(value)
        return [value]

    slopes = as_list(values)
    inters = as_list(0.0 if inters is None else inters)
    if len(inters) == 1 and len(slopes) > 1:
        inters = inters * len(slopes)
    slope_presence = as_list(True if slopes_present is None else slopes_present)
    inter_presence = as_list(True if inters_present is None else inters_present)
    n = min(len(slopes), len(inters))
    result = []
    for idx in range(n):
        result.append(ScaleFactor(
            float(slopes[idx]),
            float(inters[idx]),
            bool(slope_presence[idx % len(slope_presence)]),
            bool(inter_presence[idx % len(inter_presence)]),
        ))
    return result


SCALE_META_KEYS = (
    'Scaling.RescaleSlope',
    'Scaling.RescaleIntercept',
    'Scaling.RescaleSlopePresent',
    'Scaling.RescaleInterceptPresent',
)
'''Meta data keys used to record the effective modality LUT mapping.

The ``Scaling.`` prefix keeps these derived values separate from the verbatim
DICOM elements, so a missing coefficient is not confused with a measured one.
'''

SCALE_FRAME_META_KEYS = (
    'Scaling.FrameRescaleSlope',
    'Scaling.FrameRescaleIntercept',
    'Scaling.FrameRescaleSlopePresent',
    'Scaling.FrameRescaleInterceptPresent',
)
'''Fallback keys for a flat per-frame list when no classification fits.'''


SCALE_ENCODING_KEYS = (
    'Scaling.DataEncoding',
    'Scaling.HeaderLossless',
    'Scaling.NiftiSlope',
    'Scaling.NiftiIntercept',
)
'''Keys describing how the Nifti voxel values relate to the recorded pairs.'''


def annotate_encoding(meta_ext, plan: "NiftiScalingPlan"):
    '''Record how an output image carries its scaling in its meta extension.

    ``Scaling.DataEncoding`` is ``'none'``, ``'header'`` or ``'materialized'``
    and tells a consumer whether the voxel values are stored codes or already
    converted engineering values.  ``Scaling.HeaderLossless`` is False only
    when a single pair was rejected because the header could not hold it
    exactly.
    '''
    const = meta_ext.get_class_dict(('global', 'const'))
    # The merged extension may have classified a stale encoding key under
    # another class (for example per time sample), so clear it everywhere.
    for classes in meta_ext.valid_classes:
        dest = meta_ext.get_class_dict(classes)
        for key in SCALE_ENCODING_KEYS:
            dest.pop(key, None)
    const['Scaling.DataEncoding'] = plan.mode
    const['Scaling.HeaderLossless'] = bool(
        plan.mode != SCALING_MODE_MATERIALIZED or
        not plan.single_factor or plan.header_lossless
    )
    if plan.mode == SCALING_MODE_HEADER:
        const['Scaling.NiftiSlope'] = float(plan.slope)
        const['Scaling.NiftiIntercept'] = float(plan.intercept)


def inject_scale_meta(meta_ext, factors: Sequence[ScaleFactor],
                      frame_shape: Optional[Tuple[int, ...]] = None):
    '''Record the effective scaling pairs in a DcmMetaExtension.

    This keeps the correspondence between stored codes and engineering
    values available even when the Nifti data had to be materialized.  A
    coefficient that was absent is marked by its ``*Present`` flag as False,
    so the compatibility default is not mistaken for a measured value.

    Parameters
    ----------
    meta_ext : dcmmeta.DcmMetaExtension
        The extension for a single input image.

    factors : sequence
        One `ScaleFactor` per frame in that image's data.

    frame_shape : tuple, optional
        The data dimensions the flat `factors` list covers, used to choose a
        per-slice, per-time-sample or flat classification.
    '''
    factor_list = list(factors)
    if not factor_list:
        return
    for classes in meta_ext.valid_classes:
        dest = meta_ext.get_class_dict(classes)
        for key in SCALE_META_KEYS + SCALE_FRAME_META_KEYS + \
                ('Scaling.FrameShape',):
            dest.pop(key, None)

    values = [
        [float(f.slope) for f in factor_list],
        [float(f.intercept) for f in factor_list],
        [bool(f.slope_present) for f in factor_list],
        [bool(f.intercept_present) for f in factor_list],
    ]
    if len(factor_list) == 1:
        for key, vals in zip(SCALE_META_KEYS, values):
            meta_ext.get_class_dict(('global', 'const'))[key] = vals[0]
        return

    # A per-slice classification is used for a plain 3D image whose factor
    # list is one-pair-per-slice.  C order over (x, y, slices) is already
    # slice order.  For 4D/5D data the DcmMeta per-slice order is slice-major
    # while the factors are C order, so the factors are recorded explicitly
    # with their shape instead of guessing.
    if len(meta_ext.shape) == 3 and \
            meta_ext.n_slices is not None and \
            meta_ext.n_slices == len(factor_list):
        for key, vals in zip(SCALE_META_KEYS, values):
            meta_ext.get_class_dict(('global', 'slices'))[key] = list(vals)
        return
    # Keep the complete flat list so no per-frame factor is lost.  The order
    # is C order over `frame_shape`, which is recorded alongside it.
    const = meta_ext.get_class_dict(('global', 'const'))
    for key, vals in zip(SCALE_FRAME_META_KEYS, values):
        const[key] = list(vals)
    if frame_shape is not None:
        const['Scaling.FrameShape'] = [int(x) for x in frame_shape]


def make_scaling_plan(factors: Sequence[ScaleFactor]) -> NiftiScalingPlan:
    '''Choose the most faithful Nifti representation for the given factors.

    A single header pair is only used when it represents every coefficient
    exactly in float32.  Otherwise the caller must materialize the
    engineering values; returning a rounded pair is never an option.
    '''
    factor_list = tuple(factors)
    if not factor_list:
        factor_list = (ScaleFactor(DEFAULT_RESCALE_SLOPE,
                                   DEFAULT_RESCALE_INTERCEPT,
                                   False,
                                   False),)
    unique = unique_factor_values(factor_list)
    if len(unique) == 1:
        factor = factor_list[0]
        if factor.is_identity():
            return NiftiScalingPlan(
                SCALING_MODE_NONE,
                factor.slope,
                factor.intercept,
                True,
                factor_list,
                "identity scaling",
                True,
            )
        if factor.is_lossless_in_nifti_header():
            return NiftiScalingPlan(
                SCALING_MODE_HEADER,
                factor.slope,
                factor.intercept,
                True,
                factor_list,
                "single scaling pair is exactly representable in the Nifti header",
                True,
            )
        return NiftiScalingPlan(
            SCALING_MODE_MATERIALIZED,
            factor.slope,
            factor.intercept,
            False,
            factor_list,
            "single scaling pair is not exactly representable in the Nifti "
            "header; engineering values will be written directly",
            True,
        )
    return NiftiScalingPlan(
        SCALING_MODE_MATERIALIZED,
        None,
        None,
        True,
        factor_list,
        "scaling varies between frames; engineering values will be written "
        "directly",
        False,
    )
