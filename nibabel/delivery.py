# emacs: -*- mode: python-mode; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the NiBabel package for the
#   copyright and license terms.
#
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Plan and write lossless NIfTI deliveries.

A *target* is a description of what a receiving site accepts: the NIfTI
version(s), the on-disk data types, how scaling may be represented, and ranges
for a handful of header fields.  A *delivery* is the smallest file that

* decodes, on read, to exactly the original voxel values,
* keeps every header field that the target can still represent, and
* writes back to the original values after a round trip.

The planner works from the *decoded* values, not from the source datatype.
An ``int16`` image with a ``scl_slope`` of ``1e-5`` has fractional decoded
values that no ``int16`` file can hold without scaling, and a ``float64``
image whose values are all integers may shrink to ``int16`` only when every
value is exactly representable.  Values are compared as exact rationals, so
nothing is rounded or truncated silently.  When no allowed combination is
lossless, :class:`DeliveryError` lists each blocking value instead of picking
a nearby type.

The original file is only ever read.
"""

from __future__ import annotations

import math
import typing as ty
from dataclasses import dataclass, field
from fractions import Fraction

import numpy as np

from . import nifti1, nifti2
from .spatialimages import HeaderDataError

__all__ = [
    'DeliveryBlocked',
    'DeliveryError',
    'DeliveryPlan',
    'Encoding',
    'Migration',
    'TargetProfile',
    'ValueAnalysis',
    'analyze_values',
    'plan_delivery',
    'verify_round_trip',
    'write_delivery',
]

# Every dtype the NIfTI standard defines, in increasing on-disk size.
ALL_DTYPES: tuple[str, ...] = (
    'uint8',
    'int8',
    'int16',
    'uint16',
    'int32',
    'uint32',
    'float32',
    'float64',
    'int64',
    'uint64',
    'complex64',
    'complex128',
)

_VERSION_CLASSES = {
    'nifti1': (nifti1.Nifti1Image, nifti1.Nifti1Header),
    'nifti2': (nifti2.Nifti2Image, nifti2.Nifti2Header),
}

# Default per-field header ranges.  These mirror the NIfTI-1 standard limits.
_DEFAULT_DIM_MAX = 32767  # NIfTI-1 dim[] is int16
_DEFAULT_VOX_OFFSET_ALIGN = 16


class DeliveryError(Exception):
    """Base class for delivery failures."""


class DeliveryBlocked(DeliveryError):
    """No legal lossless combination exists for the given target.

    Attributes
    ----------
    blockers : list of dict
        One entry per reason, each with ``field``, ``detail`` and, where
        relevant, ``value`` / ``count``.
    """

    def __init__(self, blockers):
        self.blockers = list(blockers)
        super().__init__(self._format())

    def _format(self):
        lines = ['no lossless delivery satisfies the target:']
        lines.extend(f'  - {blocker["field"]}: {blocker["detail"]}' for blocker in self.blockers)
        return '\n'.join(lines)

    def __str__(self):
        return self._format()


def _dtype_name(dtype) -> str:
    dtype = np.dtype(dtype)
    if dtype.kind == 'c':
        return {8: 'complex64', 16: 'complex128'}[dtype.itemsize]
    return dtype.name


def _as_scalar(value):
    """Return a Python scalar for a 0-d array or numpy scalar."""
    arr = np.asarray(value)
    if arr.shape == ():
        return arr.item()
    return value


# ---------------------------------------------------------------------------
# Exact value analysis
# ---------------------------------------------------------------------------


def _exact(value) -> Fraction:
    """Exact rational value of a real or complex scalar.

    Complex values are handled by the caller and rejected here.
    """
    value = _as_scalar(value)
    if isinstance(value, (bool, np.bool_)):
        return Fraction(int(value))
    if isinstance(value, (int, np.integer)):
        return Fraction(int(value))
    if isinstance(value, (float, np.floating)):
        value = float(value)
        if not math.isfinite(value):
            raise ValueError('non-finite value has no finite rational form')
        return Fraction(*value.as_integer_ratio())
    raise TypeError(f'cannot take exact value of {type(value)!r}')


def _rational_gcd(a: Fraction, b: Fraction) -> Fraction:
    """Greatest common divisor of two non-negative rationals."""
    if a == 0:
        return b
    if b == 0:
        return a
    num = math.gcd(a.numerator * b.denominator, b.numerator * a.denominator)
    den = a.denominator * b.denominator
    return Fraction(num, den)


def _float_is_exact_in(value: Fraction, dtype) -> bool:
    """Whether `value` survives a round trip through floating `dtype`."""
    dtype = np.dtype(dtype)
    if value == 0:
        return True
    # Convert to the target precision and compare the exact rationals.
    with np.errstate(over='ignore', invalid='ignore'):
        converted = dtype.type(value.numerator) / dtype.type(value.denominator)
    if not np.isfinite(converted):
        return False
    return _exact(converted) == value


@dataclass
class ValueAnalysis:
    """Facts about the decoded voxel values of an image.

    All comparisons are exact.  ``finite`` and ``nonfinite_count`` count
    values, with complex values counted once.
    """

    count: int
    dtype: str
    finite_count: int = 0
    nonfinite_count: int = 0
    nan_count: int = 0
    posinf_count: int = 0
    neginf_count: int = 0
    min_value: Fraction | None = None
    max_value: Fraction | None = None
    all_integer: bool = False
    common_step: Fraction | None = None
    offset: Fraction | None = None
    n_unique: int = 0
    has_nonzero: bool = False
    integer_values: bool = False
    has_negative_zero: bool = False
    # Exact representation of the finite values as ``ints / 2**scale``.
    _ints: ty.Any = field(default=None, repr=False, compare=False)
    _exponent: int = field(default=0, repr=False, compare=False)
    # True when the source stores (u)int64 values too wide for float64.
    _wide_integers: bool = field(default=False, repr=False, compare=False)

    def as_dict(self):
        def fmt(value):
            if value is None:
                return None
            if isinstance(value, Fraction):
                return str(value) if value.denominator != 1 else int(value)
            return value

        return {
            'count': self.count,
            'dtype': self.dtype,
            'finite_count': self.finite_count,
            'nonfinite_count': self.nonfinite_count,
            'nan_count': self.nan_count,
            'posinf_count': self.posinf_count,
            'neginf_count': self.neginf_count,
            'min': fmt(self.min_value),
            'max': fmt(self.max_value),
            'all_integer': self.all_integer,
            'common_step': fmt(self.common_step),
            'offset': fmt(self.offset),
            'n_unique': self.n_unique,
        }


def _float_common_scale(flat: np.ndarray):
    """Return ``(integers, exponent, nonfinite)`` for a float array.

    Every finite float64 is an integer multiple of ``2**exponent`` where
    ``exponent = min(exponent, -1)`` over all values (subnormals included).
    ``integers`` is then an exact integer array in float64 when the mantissas
    fit, or in Python ints otherwise.
    """
    finite = np.isfinite(flat)
    nonfinite = ~finite
    values = flat[finite]
    if values.size == 0:
        return np.empty(0, dtype=np.int64), 0, int(nonfinite.sum())
    mantissas, exponents = np.frexp(values)
    exponent = int(np.min(exponents)) - 53
    scaled = np.ldexp(mantissas, exponents - exponent)
    integers = np.rint(scaled)
    # Reduce to the smallest binary exponent shared by every value: strip
    # common factors of two from all nonzero integers.
    nonzero = integers[integers != 0]
    if nonzero.size:
        common = int(np.gcd.reduce(np.abs(nonzero).astype(object)))
        shift = (common & -common).bit_length() - 1 if common else 0
        if shift:
            integers = integers / (2**shift)
            exponent += shift
    if not np.array_equal(integers, np.rint(integers)):
        # Values need more than 53 bits of shared precision; fall back to
        # exact Python integers.
        from fractions import Fraction as _F

        ints = []
        for value in values:
            fraction = _F(*float(value).as_integer_ratio())
            scaled_fraction = fraction / (2**exponent)
            if scaled_fraction.denominator != 1:
                raise AssertionError('non-integral scaled value')
            ints.append(int(scaled_fraction.numerator))
        return np.array(ints, dtype=object), exponent, int(nonfinite.sum())
    if np.any(np.abs(integers) >= 2.0**63):
        # Too wide for int64; keep exact Python integers.
        return np.asarray(integers, dtype=object), exponent, int(nonfinite.sum())
    return integers.astype(np.int64), exponent, int(nonfinite.sum())


def _integers_to_fraction(value, exponent: int) -> Fraction:
    if exponent >= 0:
        return Fraction(int(value) * (2**exponent))
    return Fraction(int(value), 2 ** (-exponent))


def analyze_values(data) -> ValueAnalysis:
    """Describe the decoded values in `data` exactly.

    Parameters
    ----------
    data : array-like
        The values after any ``scl_slope`` / ``scl_inter`` has been applied.

    Returns
    -------
    analysis : ValueAnalysis
    """
    arr = np.asanyarray(data)
    dtype = _dtype_name(arr.dtype)
    flat = arr.reshape(-1)
    analysis = ValueAnalysis(count=int(flat.size), dtype=dtype)

    if flat.size == 0:
        analysis.all_integer = True
        analysis.integer_values = True
        analysis._ints = np.empty(0, dtype=np.int64)
        analysis._exponent = 0
        analysis.common_step = Fraction(1)
        analysis.offset = Fraction(0)
        return analysis

    if arr.dtype.kind == 'c':
        finite = np.isfinite(flat)
        analysis.finite_count = int(finite.sum())
        analysis.nonfinite_count = int((~finite).sum())
        analysis.nan_count = int(np.isnan(flat).sum())
        analysis.min_value = None
        analysis.max_value = None
        analysis.all_integer = bool(np.all(flat == np.rint(flat)))
        analysis.n_unique = int(np.unique(flat).size)
        analysis.has_nonzero = bool(np.any(flat != 0))
        analysis.integer_values = analysis.all_integer
        return analysis

    if arr.dtype.kind in 'iu' and arr.dtype.itemsize <= 4:
        # Integer storage without scaling: exact values, step one.
        integers = flat.astype(np.int64)
        analysis.finite_count = int(flat.size)
        analysis.min_value = Fraction(int(integers.min()))
        analysis.max_value = Fraction(int(integers.max()))
        analysis.all_integer = True
        analysis.integer_values = True
        analysis.offset = analysis.min_value
        analysis.common_step = Fraction(1)
        analysis.n_unique = int(np.unique(flat).size)
        analysis.has_nonzero = bool(np.any(flat != 0))
        analysis._ints = integers
        analysis._exponent = 0
        return analysis

    if arr.dtype.kind in 'iu':
        # 64-bit integers may exceed float64's 53-bit exact range; keep them
        # as Python integers so nothing is rounded.
        wide = flat.astype(object)
        analysis.finite_count = int(flat.size)
        analysis.min_value = Fraction(int(wide.min()))
        analysis.max_value = Fraction(int(wide.max()))
        analysis.all_integer = True
        analysis.integer_values = True
        analysis.offset = analysis.min_value
        analysis.common_step = Fraction(1)
        analysis.n_unique = int(np.unique(flat).size)
        analysis.has_nonzero = bool(np.any(flat != 0))
        analysis._ints = wide
        analysis._exponent = 0
        analysis._wide_integers = True
        return analysis

    # Convert every finite value to an exact integer on a shared binary scale.
    work = flat.astype(np.float64, copy=False)
    finite_mask = np.isfinite(work)
    nonfinite = int((~finite_mask).sum())
    nan_count = int(np.isnan(work).sum())
    posinf_count = int(np.isposinf(work).sum())
    neginf_count = int(np.isneginf(work).sum())
    analysis.nan_count = nan_count
    analysis.posinf_count = posinf_count
    analysis.neginf_count = neginf_count
    analysis.nonfinite_count = nonfinite

    finite = work[finite_mask]
    analysis.finite_count = int(finite.size)
    if np.any(np.signbit(finite) & (finite == 0)):
        analysis.has_negative_zero = True
    if finite.size == 0:
        analysis.all_integer = False
        analysis.offset = None
        analysis.common_step = None
        return analysis

    integers, exponent, _ = _float_common_scale(finite)
    if integers.dtype == object:
        # Rare: fall back to exact fractions for a handful of values.
        exact = [_exact(value) for value in finite]
        analysis.min_value = min(exact)
        analysis.max_value = max(exact)
        analysis.all_integer = all(v.denominator == 1 for v in exact)
        analysis.integer_values = analysis.all_integer and not nonfinite
        base = exact[0]
        step = Fraction(0)
        for value in exact[1:]:
            step = _rational_gcd(step, value - base)
        analysis.offset = base
        analysis.common_step = step or Fraction(1)
        analysis.n_unique = len(set(exact))
        analysis.has_nonzero = any(v != 0 for v in exact)
        return analysis

    if integers.size and int(integers.min()) == int(integers.max()):
        step_int = 0
    else:
        step_int = int(np.gcd.reduce(integers - int(integers.min())))
    analysis._ints = integers
    analysis._exponent = exponent
    analysis.min_value = _integers_to_fraction(int(integers.min()), exponent)
    analysis.max_value = _integers_to_fraction(int(integers.max()), exponent)
    analysis.all_integer = exponent >= 0
    analysis.integer_values = analysis.all_integer and not nonfinite
    if step_int == 0:
        analysis.common_step = Fraction(1)
    else:
        analysis.common_step = _integers_to_fraction(step_int, exponent)
    analysis.offset = _integers_to_fraction(int(integers.min()), exponent)
    analysis.n_unique = int(np.unique(integers).size)
    analysis.has_nonzero = bool(np.any(integers != 0))
    return analysis


# ---------------------------------------------------------------------------
# Target profile
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetProfile:
    """What a receiving site will accept.

    Parameters
    ----------
    versions : iterable of {'nifti1', 'nifti2'}
        Allowed NIfTI versions.  ``'nifti1'`` also admits the paired
        ``.hdr`` / ``.img`` variant.
    dtypes : iterable of dtype-like
        Allowed on-disk data types.
    scaling : iterable of {'header', 'none'}
        ``'header'`` allows ``scl_slope`` / ``scl_inter`` to carry
        non-trivial scaling; ``'none'`` requires a bare slope of 1 and
        intercept of 0.
    max_dim : int, optional
        Largest single ``dim[]`` entry the target accepts.  Defaults to the
        standard's limit for each version (``32767`` for NIfTI-1, ``2**63-1``
        for NIfTI-2); set it to impose a tighter site limit.
    vox_offset_alignment : int
        ``vox_offset`` must be a multiple of this value.
    xyzt_units : iterable of int, optional
        Allowed ``xyzt_units`` codes.  ``None`` accepts anything.
    extensions : {'copy', 'drop'}, optional
        ``'copy'`` requires every extension to be preserved; ``'drop'``
        allows the writer to omit extensions that cannot be serialized.
    """

    versions: tuple[str, ...] = ('nifti1', 'nifti2')
    dtypes: tuple[str, ...] = ALL_DTYPES
    scaling: tuple[str, ...] = ('header',)
    max_dim: int | None = None
    vox_offset_alignment: int = _DEFAULT_VOX_OFFSET_ALIGN
    xyzt_units: tuple[int, ...] | None = None
    extensions: str = 'copy'

    def __post_init__(self):
        versions = tuple(self.versions)
        for version in versions:
            if version not in _VERSION_CLASSES:
                raise ValueError(f'unknown NIfTI version {version!r}')
        if not versions:
            raise ValueError('at least one NIfTI version is required')
        object.__setattr__(self, 'versions', versions)

        dtypes = tuple(_dtype_name(np.dtype(d)) for d in self.dtypes)
        for dtype in dtypes:
            if dtype not in ALL_DTYPES:
                raise ValueError(f'unknown data type {dtype!r}')
        if not dtypes:
            raise ValueError('at least one data type is required')
        object.__setattr__(self, 'dtypes', dtypes)

        scaling = tuple(self.scaling)
        for mode in scaling:
            if mode not in ('header', 'none'):
                raise ValueError(f'unknown scaling mode {mode!r}')
        object.__setattr__(self, 'scaling', scaling)

        if self.max_dim is not None and int(self.max_dim) <= 0:
            raise ValueError('max_dim must be positive')
        if int(self.vox_offset_alignment) <= 0:
            raise ValueError('vox_offset_alignment must be positive')
        if self.extensions not in ('copy', 'drop'):
            raise ValueError("extensions must be 'copy' or 'drop'")
        if self.xyzt_units is not None:
            object.__setattr__(self, 'xyzt_units', tuple(int(v) for v in self.xyzt_units))

    @classmethod
    def from_json(cls, obj) -> TargetProfile:
        """Build a profile from a JSON-compatible mapping."""
        if not isinstance(obj, dict):
            raise TypeError('target profile must be a mapping')
        kwargs = {}
        if 'version' in obj:
            kwargs['versions'] = (obj['version'],)
        if 'versions' in obj:
            kwargs['versions'] = tuple(obj['versions'])
        if 'dtype' in obj:
            kwargs['dtypes'] = (obj['dtype'],)
        if 'dtypes' in obj:
            kwargs['dtypes'] = tuple(obj['dtypes'])
        if 'scaling' in obj:
            kwargs['scaling'] = tuple(obj['scaling'])
        for key in ('max_dim', 'vox_offset_alignment', 'extensions'):
            if key in obj:
                kwargs[key] = obj[key]
        if 'xyzt_units' in obj:
            kwargs['xyzt_units'] = obj['xyzt_units']
        return cls(**kwargs)

    def as_dict(self):
        return {
            'versions': list(self.versions),
            'dtypes': list(self.dtypes),
            'scaling': list(self.scaling),
            'max_dim': None if self.max_dim is None else int(self.max_dim),
            'vox_offset_alignment': int(self.vox_offset_alignment),
            'xyzt_units': None if self.xyzt_units is None else list(self.xyzt_units),
            'extensions': self.extensions,
        }


# ---------------------------------------------------------------------------
# Encoding candidates
# ---------------------------------------------------------------------------


@dataclass
class Encoding:
    """One legal way to store the values.

    Attributes
    ----------
    version : str
    dtype : str
    slope, inter : Fraction or None
        Exact scaling.  ``None`` means a bare ``(1, 0)``.
    size : int
        On-disk payload bytes.
    payload : array or None
        Integer codes produced by ``(value - inter) / slope``; ``None`` when
        the encoding stores the values directly.
    """

    version: str
    dtype: str
    slope: Fraction | None = None
    inter: Fraction | None = None
    size: int = 0
    payload: np.ndarray | None = None

    @property
    def uses_scaling(self) -> bool:
        return self.slope is not None and (self.slope != 1 or self.inter != 0)

    def as_dict(self):
        def fmt(value):
            if value is None:
                return None
            return str(value) if value.denominator != 1 else int(value)

        return {
            'version': self.version,
            'dtype': self.dtype,
            'slope': fmt(self.slope),
            'inter': fmt(self.inter),
            'size': int(self.size),
        }


@dataclass
class Migration:
    """How one original header declaration moves to the target."""

    field: str
    original: ty.Any
    delivered: ty.Any
    status: str
    detail: str = ''

    def as_dict(self):
        return {
            'field': self.field,
            'original': self.original,
            'delivered': self.delivered,
            'status': self.status,
            'detail': self.detail,
        }


@dataclass
class DeliveryPlan:
    """A lossless delivery decision.

    Attributes
    ----------
    image : image instance
        The image that would be written.  Its header carries the final
        dtypes, scaling, dimensions, offset and extensions.
    encoding : Encoding
    migrations : list of Migration
    analysis : ValueAnalysis
    blockers : list of dict
    target : TargetProfile
    """

    image: ty.Any
    encoding: Encoding
    migrations: list[Migration] = field(default_factory=list)
    analysis: ValueAnalysis | None = None
    blockers: list[dict] = field(default_factory=list)
    target: TargetProfile | None = None
    reference: np.ndarray | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return self.image is not None

    def as_dict(self):
        return {
            'ok': self.ok,
            'target': None if self.target is None else self.target.as_dict(),
            'values': None if self.analysis is None else self.analysis.as_dict(),
            'encoding': None if self.encoding is None else self.encoding.as_dict(),
            'migrations': [m.as_dict() for m in self.migrations],
            'blockers': list(self.blockers),
        }


# ---------------------------------------------------------------------------
# Candidate construction
# ---------------------------------------------------------------------------


def _allow_dtype(dtype: str, analysis: ValueAnalysis, allow_scaling: bool) -> bool:
    """Whether `dtype` could in principle hold the value pattern."""
    if analysis.dtype.startswith('complex'):
        return dtype in ('complex64', 'complex128')
    if dtype.startswith('complex'):
        return False
    if np.dtype(dtype).kind in 'iu':
        # Integer dtypes cannot carry NaN, infinities or negative zero, and
        # cannot carry fractional values without scaling.
        if analysis.nonfinite_count or analysis.has_negative_zero:
            return False
        if not allow_scaling and not analysis.all_integer:
            return False
    return True


def _direct_payload(analysis: ValueAnalysis, values, dtype: str):
    """Build an array of `dtype` holding the values with no scaling.

    Returns ``(payload, slope, inter)`` or ``None`` if any value would change.
    """
    out_dtype = np.dtype(dtype)
    if analysis.dtype.startswith('complex'):
        return _direct_complex(values, out_dtype) if out_dtype.kind == 'c' else None
    if out_dtype.kind == 'c':
        return _direct_complex(values, out_dtype)
    if analysis._ints is None:
        return None

    original = np.asarray(values).reshape(-1)

    if out_dtype.kind == 'f':
        with np.errstate(over='ignore', invalid='ignore'):
            converted = original.astype(out_dtype)
        if original.dtype.kind == 'f':
            # Compare bit patterns so -0.0 does not pass as +0.0.
            back = converted.astype(original.dtype)
            if not np.array_equal(
                back.view(np.uint64) if back.dtype.itemsize == 8 else back.view(np.uint32),
                original.view(np.uint64)
                if original.dtype.itemsize == 8
                else original.view(np.uint32),
            ):
                return None
        else:
            # Integer source: compare the exact integer values.
            if not np.array_equal(
                np.asarray(converted, dtype=object), np.asarray(original, dtype=object)
            ):
                return None
        return converted, Fraction(1), Fraction(0)

    # Integer output: negative zero has no integer representation.
    if analysis.has_negative_zero or analysis.nonfinite_count:
        return None
    if analysis._exponent < 0:
        return None
    info = np.iinfo(out_dtype)
    ints = np.asarray(analysis._ints, dtype=object)
    if ints.size:
        factor = 2**analysis._exponent
        low = int(ints.min()) * factor
        high = int(ints.max()) * factor
        if low < info.min or high > info.max:
            return None
    factor = 2**analysis._exponent
    values_out = [int(v) * factor for v in ints]
    payload = np.asarray(values_out, dtype=out_dtype)
    return payload, Fraction(1), Fraction(0)


def _direct_complex(values, out_dtype):
    flat = np.asarray(values).reshape(-1)
    result = np.empty(flat.shape, dtype=out_dtype)
    part_dtype = np.dtype('float32') if out_dtype.itemsize == 8 else np.dtype('float64')
    for index, value in enumerate(flat):
        value = _as_scalar(value)
        if not np.isfinite(value):
            result[index] = value
            continue
        if not _float_is_exact_in(_exact(np.real(value)), part_dtype):
            return None
        if not _float_is_exact_in(_exact(np.imag(value)), part_dtype):
            return None
        result[index] = complex(value)
    return result, Fraction(1), Fraction(0)


def _dyadic(fraction: Fraction):
    """Return ``(odd, exponent)`` with ``fraction == odd / 2**exponent``.

    Returns ``None`` when the denominator is not a power of two.
    """
    den = fraction.denominator
    if den & (den - 1):
        return None
    exponent = den.bit_length() - 1
    return fraction.numerator, exponent


def _scaled_layouts(analysis: ValueAnalysis, values, dtype: str):
    """Yield exact integer layouts ``(payload, slope, inter)`` for `dtype`.

    ``code = (value - inter) / slope``.  The zero-intercept layout is
    preferred and yielded first; the minimum-anchored layout follows when it
    is different.

    The values are exactly ``ints / 2**f``.  With ``slope = a / 2**b`` and
    ``inter = c / 2**d``, choose ``F = max(f, d, b)`` so that

        code = (ints * 2**(F-f) - c * 2**(F-d)) * 2**b / (2**F * a)

    which is an exact integer when the numerator divides by the denominator.
    """
    out_dtype = np.dtype(dtype)
    if out_dtype.kind not in 'iu':
        return
    if analysis.nonfinite_count or analysis._ints is None:
        return
    if analysis.dtype.startswith('complex'):
        return
    step = analysis.common_step
    if step is None or step == 0:
        return
    step_parts = _dyadic(step)
    if step_parts is None:
        return
    a, b = step_parts
    if a == 0:
        return
    # NIfTI-1 stores slope and intercept as float32, NIfTI-2 as float64.
    # Generate the widest exact layouts; the planner filters by version.
    if not _float_is_exact_in(step, np.dtype('float64')):
        return
    ints = analysis._ints
    f = -analysis._exponent
    info = np.iinfo(out_dtype)

    def codes_for(inter: Fraction):
        if not _float_is_exact_in(inter, np.dtype('float64')):
            return None
        inter_parts = _dyadic(inter)
        if inter_parts is None:
            return None
        c, d = inter_parts
        big_f = max(f, d, b)
        shift_ints = big_f - f
        shift_inter = big_f - d
        if shift_ints > 4096 or shift_inter > 4096:
            return None
        left = ints.astype(object) * (2**shift_ints)
        numerator = left - (c * (2**shift_inter))
        divisor = a * (2 ** (big_f - b))
        quotient, remainder = [], []
        for value in numerator:
            value = int(value)
            q, r = divmod(value, divisor)
            if r:
                return None
            quotient.append(q)
            remainder.append(r)
        if quotient:
            low = min(quotient)
            high = max(quotient)
            if low < info.min or high > info.max:
                return None
        return np.asarray(quotient, dtype=out_dtype)

    yielded = set()
    zero = codes_for(Fraction(0))
    if zero is not None:
        yielded.add(zero.tobytes())
        yield zero, step, Fraction(0)
    if analysis.offset is not None:
        anchored = codes_for(analysis.offset)
        if anchored is not None and anchored.tobytes() not in yielded:
            yield anchored, step, analysis.offset


def _candidate_payloads(analysis: ValueAnalysis, values, dtype: str):
    """Yield ``(payload, slope, inter, kind)`` candidates for `dtype`."""
    if analysis.dtype.startswith('complex'):
        yield from _complex_candidates(analysis, values, dtype)
        return
    direct = _direct_payload(analysis, values, dtype)
    if direct is not None:
        yield direct + ('direct',)
    if np.dtype(dtype).kind in 'iu':
        for payload, slope, inter in _scaled_layouts(analysis, values, dtype):
            yield payload, slope, inter, 'scaled'


def _complex_candidates(analysis: ValueAnalysis, values, dtype: str):
    out_dtype = np.dtype(dtype)
    if out_dtype.kind != 'c':
        return
    flat = np.asarray(values).reshape(-1)
    if out_dtype == np.dtype('complex128'):
        yield np.asarray(flat, dtype=out_dtype), Fraction(1), Fraction(0), 'direct'
        return
    # complex64 keeps the first value only if both parts survive float32.
    result = np.empty(flat.shape, dtype=out_dtype)
    for index, value in enumerate(flat):
        value = _as_scalar(value)
        real = _exact(np.real(value))
        imag = _exact(np.imag(value))
        if not (
            _float_is_exact_in(real, np.dtype('float32'))
            and _float_is_exact_in(imag, np.dtype('float32'))
        ):
            return
        result[index] = complex(real, imag)
    yield result, Fraction(1), Fraction(0), 'direct'


def _version_data_offset(version: str) -> int:
    """Minimum offset of voxel data for a single-file `version`."""
    if version == 'nifti1':
        return nifti1.Nifti1Header.single_vox_offset
    return nifti2.Nifti2Header.single_vox_offset


def _extension_bytes(header) -> int:
    try:
        return int(header.extensions.get_sizeondisk())
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Target checking
# ---------------------------------------------------------------------------


def _dim_limit(target: TargetProfile, version: str) -> int:
    """Largest single dim[] entry allowed for `version` and `target`."""
    standard = 32767 if version == 'nifti1' else 2**63 - 1
    if target.max_dim is None:
        return standard
    return min(int(target.max_dim), standard)


def _check_dimensions(shape, target: TargetProfile, version: str):
    blockers = []
    if len(shape) > 7:
        blockers.append(
            {
                'field': 'dim',
                'detail': f'{len(shape)} dimensions exceeds the NIfTI limit of 7',
                'value': len(shape),
            }
        )
    limit = _dim_limit(target, version)
    for index, extent in enumerate(shape):
        if int(extent) > limit:
            blockers.append(
                {
                    'field': f'dim[{index + 1}]',
                    'detail': f'size {int(extent)} exceeds {version} maximum {limit}',
                    'value': int(extent),
                }
            )
    return blockers


def _resolved_offset(header) -> int:
    """The offset the writer will use for this single-file header."""
    offset = int(header.get_data_offset())
    if offset:
        return offset
    return int(header.single_vox_offset) + int(header.extensions.get_sizeondisk())


def _check_offset(header, target: TargetProfile):
    offset = _resolved_offset(header)
    align = int(target.vox_offset_alignment)
    if offset % align:
        return [
            {
                'field': 'vox_offset',
                'detail': f'{offset} is not a multiple of {align}',
                'value': offset,
            }
        ]
    return []


def _check_units(header, target: TargetProfile):
    if target.xyzt_units is None:
        return []
    units = int(header['xyzt_units'])
    if units not in target.xyzt_units:
        return [
            {
                'field': 'xyzt_units',
                'detail': f'code {units} not in allowed {sorted(target.xyzt_units)}',
                'value': units,
            }
        ]
    return []


# ---------------------------------------------------------------------------
# Migration reporting
# ---------------------------------------------------------------------------


def _migration_dtype(src_header, dst_header) -> Migration:
    src = np.dtype(src_header.get_data_dtype())
    dst = np.dtype(dst_header.get_data_dtype())
    original = _dtype_name(src)
    delivered = _dtype_name(dst)
    if src == dst:
        return Migration('datatype', original, delivered, 'preserved', 'same on-disk type')
    return Migration(
        'datatype',
        original,
        delivered,
        'changed',
        'values remain exact; storage type re-encoded',
    )


def _source_scaling(img) -> tuple[Fraction, Fraction]:
    """Return the scaling that applies to the source file's stored values.

    ``nibabel.load`` moves a file's slope and intercept into the array proxy
    and blanks the header fields.  A newly created image instead keeps them in
    the header.  Read whichever side actually carries the scaling.
    """
    proxy = img.dataobj
    if hasattr(proxy, 'slope') and proxy.slope is not None:
        return _exact(proxy.slope), _exact(proxy.inter)
    slope, inter = img.header.get_slope_inter()
    if slope is None:
        return Fraction(1), Fraction(0)
    return _exact(slope), _exact(inter)


def _migration_scaling(src_scaling, encoding: Encoding) -> Migration:
    src_slope, src_inter = src_scaling
    src_repr = f'{src_slope}, {src_inter}'
    dst_repr = f'{encoding.slope}, {encoding.inter}'
    src_bare = (src_slope, src_inter) == (1, 0)
    dst_bare = (encoding.slope, encoding.inter) == (1, 0)
    if src_bare and dst_bare:
        return Migration('scaling', src_repr, dst_repr, 'preserved', 'bare scaling on both sides')
    if (src_slope, src_inter) == (encoding.slope, encoding.inter):
        return Migration('scaling', src_repr, dst_repr, 'preserved', 'same stored scaling')
    return Migration(
        'scaling',
        src_repr,
        dst_repr,
        'moved' if encoding.uses_scaling else 'dropped',
        'decoded values unchanged; scaling carried by the new header',
    )


def _migration_dimensions(src_header, dst_header) -> Migration:
    original = src_header.get_data_shape()
    delivered = dst_header.get_data_shape()
    status = 'preserved' if original == delivered else 'changed'
    return Migration('dim', list(original), list(delivered), status)


def _source_offset(img) -> int:
    """The data offset actually stored in the source file."""
    offset = int(img.header.get_data_offset())
    if offset:
        return offset
    # nibabel.load blanks the header offset and keeps it on the proxy.
    proxy_offset = getattr(img.dataobj, 'offset', None)
    if proxy_offset is not None:
        return int(proxy_offset)
    return 0


def _migration_offset(src_offset, dst_header) -> Migration:
    delivered = _resolved_offset(dst_header)
    if src_offset == 0:
        return Migration(
            'vox_offset',
            'unset (0 in file)',
            delivered,
            'resolved',
            'source header left the data offset unset; the writer records the true offset',
        )
    status = 'preserved' if src_offset == delivered else 'recomputed'
    detail = '' if status == 'preserved' else 'header size or extensions moved the data'
    return Migration('vox_offset', src_offset, delivered, status, detail)


def _migration_extensions(src_header, dst_header) -> Migration:
    original = [int(e.get_code()) for e in src_header.extensions]
    delivered = [int(e.get_code()) for e in dst_header.extensions]
    status = 'preserved' if original == delivered else 'dropped'
    return Migration('extensions', original, delivered, status)


def _version_of(header) -> str:
    """Return the NIfTI version of `header` without relying on subclassing.

    ``Nifti2Header`` subclasses ``Nifti1Header``, so ``isinstance`` is not
    enough to tell them apart; compare the header size instead.
    """
    if int(header.sizeof_hdr) == 540:
        return 'nifti2'
    return 'nifti1'


def _migration_version(src_header, dst_header) -> Migration:
    src_version = _version_of(src_header)
    dst_version = _version_of(dst_header)
    status = 'preserved' if src_version == dst_version else 'changed'
    return Migration('version', src_version, dst_version, status)


def _field_migration(field, original, delivered) -> Migration:
    """Compare one header field for preservation.

    NIfTI-2 widens float32 header fields to float64, so a bit-exact comparison
    would flag pure storage rounding as a change.  Values are compared with a
    tolerance far below any meaningful voxel or geometry difference.
    """
    left = np.asarray(original)
    right = np.asarray(delivered)
    if left.dtype.kind in 'fc' or right.dtype.kind in 'fc':
        same = bool(np.allclose(left, right, rtol=0, atol=1e-9, equal_nan=True))
    else:
        same = bool(np.array_equal(left, right))
    return Migration(field, original, delivered, 'preserved' if same else 'changed')


def _affine_migration(field, source, delivered) -> Migration:
    src_affine, src_code = source
    dst_affine, dst_code = delivered
    same_code = int(src_code) == int(dst_code)
    if src_affine is None or dst_affine is None:
        same = src_affine is None and dst_affine is None and same_code
    else:
        # Affine re-encoding can differ at float epsilon; anything larger is
        # a real move.
        same = same_code and bool(np.allclose(src_affine, dst_affine, rtol=0, atol=1e-9))
    return Migration(
        field,
        {'code': int(src_code), 'affine': None if src_affine is None else _clean(src_affine)},
        {'code': int(dst_code), 'affine': None if dst_affine is None else _clean(dst_affine)},
        'preserved' if same else 'changed',
    )


def _clean(values):
    """Convert header values to JSON-friendly Python scalars."""
    arr = np.asarray(values)
    if arr.ndim == 0:
        return arr.item()
    return arr.tolist()


def _migrations(
    src_header, dst_header, encoding: Encoding, src_scaling, src_offset
) -> list[Migration]:
    migrations = [
        _migration_version(src_header, dst_header),
        _migration_dtype(src_header, dst_header),
        _migration_scaling(src_scaling, encoding),
        _migration_dimensions(src_header, dst_header),
        _migration_offset(src_offset, dst_header),
        _migration_extensions(src_header, dst_header),
    ]
    # Fields the target profile constrains, or that conversion could move.
    migrations.append(
        _field_migration(
            'pixdim',
            _clean(src_header['pixdim']),
            _clean(dst_header['pixdim']),
        )
    )
    migrations.append(
        _affine_migration(
            'qform', src_header.get_qform(coded=True), dst_header.get_qform(coded=True)
        )
    )
    migrations.append(
        _affine_migration(
            'sform', src_header.get_sform(coded=True), dst_header.get_sform(coded=True)
        )
    )
    migrations.append(
        _field_migration(
            'xyzt_units',
            int(src_header['xyzt_units']),
            int(dst_header['xyzt_units']),
        )
    )
    migrations.append(
        _field_migration(
            'intent',
            _clean(src_header.get_intent()[1]),
            _clean(dst_header.get_intent()[1]),
        )
    )
    migrations.append(
        _field_migration(
            'calibration',
            [float(src_header['cal_min']), float(src_header['cal_max'])],
            [float(dst_header['cal_min']), float(dst_header['cal_max'])],
        )
    )
    return migrations


# ---------------------------------------------------------------------------
# Building the delivered image
# ---------------------------------------------------------------------------


def _make_header(src_header, version: str):
    _image_class, header_class = _VERSION_CLASSES[version]
    # Convert through the Analyze map so every shared field (dim, pixdim,
    # qform/sform, intent, units, ...) keeps its value, but defer the
    # integrity check: the freshly sized header legitimately differs in
    # sizeof_hdr until `set_data_shape` finalizes it.
    header = header_class.from_header(src_header, check=False)
    return header


def _set_exact_scaling(header, encoding: Encoding, version: str):
    if not encoding.uses_scaling:
        header.set_slope_inter(1, 0)
        return
    scaler = np.float32 if version == 'nifti1' else np.float64
    slope = scaler(float(encoding.slope))
    inter = scaler(float(encoding.inter))
    # Verify the header round-trips the exact rationals; NIfTI-2 uses float64,
    # which is exact for anything already accepted as a float32 step.
    if _exact(slope) != encoding.slope or _exact(inter) != encoding.inter:
        raise DeliveryError('scaling is not exactly representable in the target header')
    header.set_slope_inter(slope, inter)


def _build_image(values, shape, affine, src_header, version: str, encoding: Encoding):
    image_class, header_class = _VERSION_CLASSES[version]
    header = _make_header(src_header, version)
    # `check_fix` on the converted header would log a harmless sizeof_hdr
    # mismatch; set the version's size directly instead.
    header['sizeof_hdr'] = header_class.sizeof_hdr
    header.set_data_shape(shape)
    header.set_data_dtype(np.dtype(encoding.dtype))
    _set_exact_scaling(header, encoding, version)
    # Re-encode qform/sform from their affines.  Copying the quaternion
    # fields directly lets a NIfTI-1 -> NIfTI-2 conversion carry float32
    # rounding into float64, which would move the affine slightly.
    src_qform, src_qcode = src_header.get_qform(coded=True)
    if src_qcode:
        header.set_qform(src_qform, code=int(src_qcode))
    src_sform, src_scode = src_header.get_sform(coded=True)
    if src_scode:
        header.set_sform(src_sform, code=int(src_scode))
    # Source offsets are recomputed by the writer unless they still align.
    header.set_data_offset(0)
    # Write raw codes with the scaling already in the header; the writer must
    # not recalculate it.  The payload is cast to the header's byte order so
    # the stored bytes match the declared dtype.
    out_dtype = np.dtype(encoding.dtype)
    if src_header.endianness in ('<', '>'):
        out_dtype = out_dtype.newbyteorder(src_header.endianness)
    data = np.asarray(encoding.payload, dtype=out_dtype)
    if data.size == int(np.prod(shape)):
        data = data.reshape(shape)
    image = image_class(data, affine, header=header)
    image.header.set_slope_inter(*header.get_slope_inter())
    return image


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _allowed_pairs(target: TargetProfile):
    for version in target.versions:
        for dtype in target.dtypes:
            yield version, dtype


def _load_values(img):
    """Return the decoded voxel values (scaling applied)."""
    return np.asanyarray(img.dataobj)


def plan_delivery(img, target: TargetProfile) -> DeliveryPlan:
    """Choose the smallest lossless encoding and report how it migrates.

    Parameters
    ----------
    img : image
        The original image.  It is only read.
    target : TargetProfile
        What the receiving site accepts.

    Returns
    -------
    plan : DeliveryPlan
        ``plan.image`` is ``None`` when ``plan.blockers`` is non-empty.
    """
    if not isinstance(target, TargetProfile):
        target = TargetProfile.from_json(target)

    values = _load_values(img)
    analysis = analyze_values(values)
    shape = tuple(int(v) for v in img.shape)
    affine = np.array(img.affine, copy=True) if img.affine is not None else np.eye(4)
    src_header = img.header
    src_scaling = _source_scaling(img)
    src_offset = _source_offset(img)

    blockers: list[dict] = []
    # A dimension is fine if any allowed version can hold it.
    dim_ok = any(not _check_dimensions(shape, target, version) for version in target.versions)
    if not dim_ok:
        # Report the tightest version's reason; that is the binding limit.
        blockers.extend(_check_dimensions(shape, target, target.versions[0]))

    if analysis.nonfinite_count and not any(np.dtype(d).kind in 'fc' for d in target.dtypes):
        blockers.append(
            {
                'field': 'datatype',
                'detail': (
                    f'{analysis.nonfinite_count} non-finite value(s) present but '
                    'target allows only integer types'
                ),
                'count': analysis.nonfinite_count,
            }
        )

    if analysis.dtype.startswith('complex') and not any(
        np.dtype(d).kind == 'c' for d in target.dtypes
    ):
        blockers.append(
            {
                'field': 'datatype',
                'detail': 'source is complex but target allows no complex type',
            }
        )

    # Extensions: NIfTI-1 and NIfTI-2 share the extension framing, so a copy
    # is legal whenever the target version can carry the payload.
    if target.extensions == 'copy':
        blockers.extend(
            {
                'field': 'extensions',
                'detail': f'extension {ext.get_code()} is too large for any NIfTI header',
            }
            for ext in src_header.extensions
            if len(ext.content) > 2**31 - 1
        )

    if blockers:
        return DeliveryPlan(
            image=None,
            encoding=None,
            migrations=[],
            analysis=analysis,
            blockers=blockers,
            target=target,
        )

    candidates: list[tuple[int, int, Encoding]] = []
    ext_bytes = _extension_bytes(src_header) if target.extensions == 'copy' else 0
    # Payload layouts depend only on the data type, not the NIfTI version, so
    # build each dtype's payloads once.
    payload_cache: dict[str, list] = {}
    for dtype in target.dtypes:
        if not _allow_dtype(dtype, analysis, 'header' in target.scaling):
            continue
        layouts = []
        seen_layouts = set()
        for payload, slope, inter, kind in _candidate_payloads(analysis, values, dtype):
            if target.scaling == ('none',) and (slope != 1 or inter != 0):
                continue
            if kind == 'scaled' and 'header' not in target.scaling:
                continue
            step_precision = (
                np.dtype('float32') if 'nifti1' in target.versions else np.dtype('float64')
            )
            if not (
                _float_is_exact_in(slope, step_precision)
                and _float_is_exact_in(inter, step_precision)
            ):
                continue
            layout = (payload.tobytes(), str(slope), str(inter))
            if layout not in seen_layouts:
                seen_layouts.add(layout)
                layouts.append((payload, slope, inter))
        payload_cache[dtype] = layouts

    for order, (version, dtype) in enumerate(_allowed_pairs(target)):
        for payload, slope, inter in payload_cache.get(dtype, ()):
            size = int(np.dtype(dtype).itemsize) * analysis.count
            size += _version_data_offset(version)
            size += ext_bytes
            encoding = Encoding(
                version=version,
                dtype=dtype,
                slope=slope,
                inter=inter,
                size=size,
                payload=payload,
            )
            candidates.append((size, order, encoding))

    if not candidates:
        blockers.append(
            {
                'field': 'datatype',
                'detail': 'no allowed type and scaling combination can hold the values exactly',
            }
        )
        return DeliveryPlan(
            image=None,
            encoding=None,
            migrations=[],
            analysis=analysis,
            blockers=blockers,
            target=target,
        )

    candidates.sort(key=lambda item: (item[0], item[1]))

    image = None
    chosen = None
    header_blockers = []
    attempted = []
    for _size, _order, encoding in candidates:
        try:
            trial = _build_image(values, shape, affine, src_header, encoding.version, encoding)
        except HeaderDataError as error:
            attempted.append({'field': 'version', 'detail': f'{encoding.version}: {error}'})
            continue
        trial_blockers = []
        trial_blockers.extend(
            _check_dimensions(trial.header.get_data_shape(), target, encoding.version)
        )
        trial_blockers.extend(_check_offset(trial.header, target))
        trial_blockers.extend(_check_units(trial.header, target))
        if target.extensions == 'copy':
            src_exts = [int(e.get_code()) for e in src_header.extensions]
            dst_exts = [int(e.get_code()) for e in trial.header.extensions]
            if src_exts != dst_exts:
                trial_blockers.append(
                    {
                        'field': 'extensions',
                        'detail': f'extension codes {src_exts} cannot be preserved as {dst_exts}',
                    }
                )
        if not trial_blockers:
            chosen = encoding
            image = trial
            header_blockers = []
            break
        header_blockers = trial_blockers

    if image is None:
        return DeliveryPlan(
            image=None,
            encoding=None,
            migrations=[],
            analysis=analysis,
            blockers=header_blockers
            or attempted
            or [{'field': 'dim', 'detail': 'no allowed version can hold this shape'}],
            target=target,
        )

    migrations = _migrations(src_header, image.header, chosen, src_scaling, src_offset)
    return DeliveryPlan(
        image=image,
        encoding=chosen,
        migrations=migrations,
        analysis=analysis,
        blockers=[],
        target=target,
        reference=np.array(values, copy=True),
    )


# ---------------------------------------------------------------------------
# Writing and verification
# ---------------------------------------------------------------------------


def write_delivery(plan: DeliveryPlan, filename) -> None:
    """Write a planned delivery to `filename`.

    The bytes are verified before they are committed: if reloading them would
    not reproduce the original values exactly, nothing is written and
    :class:`DeliveryError` is raised.  The file is written through nibabel's
    normal save path so that ``vox_offset`` and extension framing follow the
    library's rules.
    """
    if not plan.ok:
        raise DeliveryBlocked(plan.blockers)
    report = verify_round_trip(plan)
    if not report['ok']:
        raise DeliveryError(f'encoded delivery would not round-trip exactly: {report}')
    from .loadsave import save

    save(plan.image, filename)


def _exact_compare(original, delivered):
    """Return ``(equal, first_mismatch)`` for two arrays, exactly."""
    a = np.asanyarray(original)
    b = np.asanyarray(delivered)
    if a.shape != b.shape:
        # Zero-size arrays can be read back with a flattened shape.
        if a.size == 0 and b.size == 0:
            return True, None
        return False, {'reason': f'shape {a.shape} != {b.shape}'}
    aflat = a.reshape(-1)
    bflat = b.reshape(-1)
    for index, (left, right) in enumerate(zip(aflat, bflat)):
        left = _as_scalar(left)
        right = _as_scalar(right)
        if isinstance(left, complex) or isinstance(right, complex):
            if complex(left) != complex(right):
                return False, {
                    'index': int(index),
                    'original': repr(left),
                    'delivered': repr(right),
                }
            continue
        if isinstance(left, (float, np.floating)) and not math.isfinite(float(left)):
            if not (
                isinstance(right, (float, np.floating))
                and not math.isfinite(float(right))
                and math.copysign(1.0, float(left)) == math.copysign(1.0, float(right))
                and (math.isnan(float(left)) == math.isnan(float(right)))
            ):
                return False, {
                    'index': int(index),
                    'original': repr(left),
                    'delivered': repr(right),
                }
            continue
        try:
            left_exact = _exact(left)
        except ValueError:
            return False, {'index': int(index), 'original': repr(left), 'delivered': repr(right)}
        try:
            right_exact = _exact(right)
        except ValueError:
            return False, {'index': int(index), 'original': repr(left), 'delivered': repr(right)}
        if left_exact != right_exact:
            return False, {
                'index': int(index),
                'original': repr(left),
                'delivered': repr(right),
            }
        # Numerically equal zeros can still differ in sign; -0.0 must survive.
        if (
            left_exact == 0
            and isinstance(left, (float, np.floating))
            and isinstance(right, (float, np.floating))
            and math.copysign(1.0, float(left)) != math.copysign(1.0, float(right))
        ):
            return False, {
                'index': int(index),
                'original': repr(left),
                'delivered': repr(right),
                'reason': 'negative zero changed sign',
            }
    return True, None


def verify_round_trip(plan: DeliveryPlan) -> dict:
    """Check that the delivery decodes back to the original values.

    Parameters
    ----------
    plan : DeliveryPlan

    Returns
    -------
    report : dict
        ``ok`` is true when every value matches exactly; otherwise
        ``mismatch`` describes the first difference.
    """
    if not plan.ok:
        return {'ok': False, 'blocked': plan.blockers}
    try:
        image_class = plan.image.__class__
        payload = image_class.from_bytes(plan.image.to_bytes())
    except Exception as error:  # pragma: no cover - defensive
        return {'ok': False, 'error': repr(error)}
    values = np.asanyarray(payload.dataobj)
    reference_values = np.asanyarray(plan.reference)
    ok, mismatch = _exact_compare(reference_values, values)
    header = payload.header
    slope, inter = _source_scaling(payload)
    header_ok = (
        list(payload.shape) == list(plan.image.shape)
        and _dtype_name(np.dtype(header.get_data_dtype())) == plan.encoding.dtype
        and [int(e.get_code()) for e in header.extensions]
        == [int(e.get_code()) for e in plan.image.header.extensions]
    )
    return {
        'ok': bool(ok and header_ok),
        'mismatch': mismatch,
        'header_ok': bool(header_ok),
        'shape': list(payload.shape),
        'dtype': _dtype_name(np.dtype(header.get_data_dtype())),
        'slope': str(slope),
        'inter': str(inter),
        'extensions': [int(e.get_code()) for e in header.extensions],
    }
