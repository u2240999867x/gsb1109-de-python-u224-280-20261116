# emacs: -*- mode: python-mode; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the NiBabel package for the
#   copyright and license terms.
#
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Value contracts for NIfTI statistic images.

A NIfTI header declares the meaning of the voxel values through its intent
code (``intent_code``) together with the intent parameters (``intent_p1`` ...
``intent_p3``).  An imaging repository typically mixes plain intensity images
with statistic images (t maps, F maps, z maps) and probability maps.  To let
downstream code select images safely, this module turns the header metadata
into an explicit *value contract*:

* which statistic the values represent (only decided from the intent code,
  never guessed from the numerical range);
* which parameters the statistic needs (for example the degrees of freedom of
  a t statistic) and whether they are actually present;
* whether a scalar intent collides with extra (multi-component) dimensions;
* the observed value range and whether all values are finite;
* which target columns (for example a p-value column) the image may be
  converted to.

Conversions are deliberately conservative.  A conversion only runs when the
caller supplies an explicit conversion rule, every required parameter is
present, the values lie inside the target range and the rounding error
introduced by the target column precision is within the declared tolerance.
The original values are always returned alongside the converted values.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import cast

import numpy as np

from .nifti1 import Nifti1Header, intent_codes

__all__ = [
    'CONVERTERS',
    'ContractIssue',
    'ConversionResult',
    'ConversionRule',
    'ParameterSpec',
    'Severity',
    'StatisticContract',
    'TargetColumn',
    'apply_conversion',
    'available_targets',
    'build_contract',
    'contract_identity',
    'register_converter',
]


class Severity(Enum):
    """Severity of a contract issue."""

    WARNING = 'warning'
    ERROR = 'error'


# Mapping of NIfTI intent codes to canonical statistic names.  Only intent
# codes whose statistical meaning is unambiguous appear here.  Unknown codes
# stay unknown instead of being inferred from the value range.
_STATISTIC_BY_CODE = {
    3: 't',
    4: 'F',
    5: 'z',
    6: 'chi2',
    12: 'F_nonc',
    13: 'chi2_nonc',
    17: 't_nonc',
    19: 'chi',
    22: 'p',
    23: 'log_p',
    24: 'log10_p',
}

# Intent codes describing a single scalar statistic per voxel.  The matrix,
# vector and pointset intents (1004 and above) are explicitly multi-component.
_SCALAR_INTENT_CODES = frozenset(range(2, 25))

# Expected value ranges of the statistics we know about.  ``None`` means the
# range is unbounded on that side.
_VALUE_RANGES: dict[str, tuple[float | None, float | None]] = {
    't': (None, None),
    't_nonc': (None, None),
    'z': (None, None),
    'F': (0.0, None),
    'F_nonc': (0.0, None),
    'chi2': (0.0, None),
    'chi2_nonc': (0.0, None),
    'chi': (0.0, None),
    'p': (0.0, 1.0),
    'log_p': (None, 0.0),
    'log10_p': (None, 0.0),
}


@dataclass(frozen=True)
class ParameterSpec:
    """Description of one intent parameter (``intent_p1`` ... ``intent_p3``).

    Parameters
    ----------
    name : str
        Name of the header field, for example ``"intent_p1"``.
    value : float
        Value stored in the header.
    description : str
        Meaning of the parameter as declared in :data:`intent_codes`.
    available : bool
        True when the parameter is set to a finite, usable value.
    """

    name: str
    value: float
    description: str
    available: bool


@dataclass(frozen=True)
class ContractIssue:
    """A problem found while building a value contract."""

    code: str
    severity: Severity
    message: str
    parameter: str | None = None
    value: float | None = None


@dataclass(frozen=True)
class StatisticContract:
    """Explicit value contract for one NIfTI statistic image.

    Attributes
    ----------
    name : str or None
        Short image name (file basename when available).
    path : str or None
        Full image path when the image knows it.
    intent_code : int
        Raw NIfTI intent code.
    intent_label : str
        Label of the intent code, or ``"unknown code <n>"`` for codes that
        are not part of the NIfTI specification.
    intent_name : str
        Free-form ``intent_name`` string stored in the header.
    statistic : str or None
        Canonical statistic name (``"t"``, ``"F"``, ``"z"``, ``"p"`` ...).
        ``None`` for plain intensity images and unknown intents.
    parameters : tuple of ParameterSpec
        Intent parameters, including whether each required parameter is set.
    shape : tuple of int
        Full data shape.
    n_components : int
        Number of components per voxel, i.e. the product of the dimensions
        beyond the three spatial dimensions (1 for a scalar image).
    scalar_intent : bool
        True when the intent code declares a per-voxel scalar statistic.
    value_range : tuple of (float, float) or None
        Observed ``(minimum, maximum)`` of the finite values.
    finite : bool
        True when every value is finite.
    has_nan : bool
        True when at least one value is NaN.
    has_inf : bool
        True when at least one value is infinite.
    issues : tuple of ContractIssue
        All detected problems; error issues block conversions.
    convertible_to : tuple of str
        Target columns for which a registered, currently usable converter
        exists.
    """

    name: str | None
    path: str | None
    intent_code: int
    intent_label: str
    intent_name: str
    statistic: str | None
    parameters: tuple[ParameterSpec, ...]
    shape: tuple[int, ...]
    n_components: int
    scalar_intent: bool
    value_range: tuple[float, float] | None
    finite: bool
    has_nan: bool
    has_inf: bool
    issues: tuple[ContractIssue, ...] = field(default_factory=tuple)
    convertible_to: tuple[str, ...] = field(default_factory=tuple)

    @property
    def issue_codes(self) -> tuple[str, ...]:
        """Codes of all issues attached to the contract."""
        return tuple(issue.code for issue in self.issues)

    def is_valid(self) -> bool:
        """Return True when the contract has no error-level issues."""
        return all(issue.severity is not Severity.ERROR for issue in self.issues)


@dataclass(frozen=True)
class TargetColumn:
    """Precision and value range of a converted output column.

    Parameters
    ----------
    name : str
        Column name, for example ``"p value"``.
    precision : numpy dtype or string
        NumPy dtype of the output column (must be a floating point dtype).
    value_range : tuple of (float, float)
        Inclusive ``(lower, upper)`` range every converted value must fall in.
    tolerance : float
        Maximum absolute deviation from the float64 reference result that is
        accepted as rounding error of the target column precision.
    """

    name: str
    precision: np.dtype
    value_range: tuple[float, float]
    tolerance: float

    def __post_init__(self) -> None:
        dtype = np.dtype(self.precision)
        if dtype.kind != 'f':
            raise ValueError(f'Target column precision must be floating point, got {dtype}')
        if not math.isfinite(self.tolerance) or self.tolerance < 0:
            raise ValueError(
                f'Tolerance must be a finite non-negative number, got {self.tolerance}'
            )
        low, high = self.value_range
        if not (math.isfinite(low) and math.isfinite(high) and low <= high):
            raise ValueError(f'Invalid target value range {self.value_range}')
        object.__setattr__(self, 'precision', dtype)


@dataclass(frozen=True)
class ConversionRule:
    """Explicit rule describing one requested conversion.

    Parameters
    ----------
    target_column : str
        Name of the desired target column; it is matched against registered
        converters when ``converter`` is not given.
    precision : numpy dtype or string, optional
        Target column precision. Defaults to float64.
    value_range : tuple of (float, float), optional
        Accepted range of the converted values. Defaults to ``(0.0, 1.0)``.
    tolerance : float, optional
        Maximum accepted absolute rounding error against the float64
        reference conversion. Defaults to ``1e-12``.
    converter : callable, optional
        Vectorized function mapping the source array to converted values.
        When omitted, a converter registered for
        ``(source statistic, target column)`` is required.
    source_identity : tuple, optional
        Identity returned by :func:`contract_identity` for the image this rule
        is meant for. When given, applying the rule to an image with a
        different identity raises ``ValueError`` so that images sharing a name
        but representing different statistics can never be mixed.
    """

    target_column: str
    precision: np.dtype = field(default_factory=lambda: np.dtype('float64'))
    value_range: tuple[float, float] = (0.0, 1.0)
    tolerance: float = 1e-12
    converter: Callable[[np.ndarray], np.ndarray] | None = None
    source_identity: tuple | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, 'precision', np.dtype(self.precision))
        if not math.isfinite(self.tolerance) or self.tolerance < 0:
            raise ValueError(
                f'Tolerance must be a finite non-negative number, got {self.tolerance}'
            )
        low, high = self.value_range
        if not (math.isfinite(low) and math.isfinite(high) and low <= high):
            raise ValueError(f'Invalid target value range {self.value_range}')


@dataclass(frozen=True)
class ConversionResult:
    """Outcome of applying a :class:`ConversionRule` to one image."""

    contract: StatisticContract
    target_column: str
    precision: np.dtype
    tolerance: float
    source_data: np.ndarray | None
    data: np.ndarray | None
    max_error: float | None
    accepted: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def raise_if_rejected(self) -> None:
        """Raise ``ValueError`` with the rejection reasons when rejected."""
        if not self.accepted:
            raise ValueError(
                f'Conversion to {self.target_column!r} rejected: {", ".join(self.reasons)}'
            )


# Registry of converters keyed by (source statistic name, target column name).
CONVERTERS: dict[tuple[str, str], Callable[[np.ndarray], np.ndarray]] = {}

# Target columns built into the module for statistics whose conversion needs
# intent parameters (t and F maps) or needs no parameters at all (z maps and
# probability maps).
_P_COLUMN = 'p value'
_LOG_P_COLUMN = 'log p value'
_LOG10_P_COLUMN = 'log10 p value'

_BUILTIN_TARGETS: dict[str, tuple[str, ...]] = {
    't': (_P_COLUMN, _LOG_P_COLUMN, _LOG10_P_COLUMN),
    'F': (_P_COLUMN, _LOG_P_COLUMN, _LOG10_P_COLUMN),
    'z': (_P_COLUMN, _LOG_P_COLUMN, _LOG10_P_COLUMN),
    'p': (_LOG_P_COLUMN, _LOG10_P_COLUMN),
    'log_p': (_P_COLUMN, _LOG10_P_COLUMN),
    'log10_p': (_P_COLUMN, _LOG_P_COLUMN),
}


def register_converter(
    source_statistic: str,
    target_column: str,
    converter: Callable[[np.ndarray], np.ndarray] | None = None,
) -> Callable | None:
    """Register a converter, optionally usable as a decorator.

    Parameters
    ----------
    source_statistic : str
        Canonical source statistic name, for example ``"t"``.
    target_column : str
        Target column name, for example ``"p value"``.
    converter : callable, optional
        Vectorized conversion function. When omitted, the function is returned
        so this can be used as a decorator.
    """

    def _decorator(func: Callable[[np.ndarray], np.ndarray]) -> Callable[[np.ndarray], np.ndarray]:
        CONVERTERS[(source_statistic, target_column)] = func
        return func

    if converter is None:
        return _decorator
    _decorator(converter)
    return None


def available_targets(contract: StatisticContract) -> tuple[str, ...]:
    """Target columns a contract can currently be converted to.

    A target is listed when a built-in or registered converter exists for the
    contract's statistic and the contract has no error-level issue.
    """
    if contract.statistic is None or not contract.is_valid():
        return ()
    targets = set(_BUILTIN_TARGETS.get(contract.statistic, ()))
    targets.update(target for (statistic, target) in CONVERTERS if statistic == contract.statistic)
    return tuple(sorted(targets))


def contract_identity(contract: StatisticContract) -> tuple:
    """Return the identity key distinguishing statistic images.

    Two images share an identity only when they carry the same intent code,
    the same intent parameters and the same number of per-voxel components.
    Downstream selection code should compare these keys (in addition to any
    file naming) so that an image and a same-named image representing a
    different statistic are never mixed.
    """
    parameters = tuple(spec.value for spec in contract.parameters)
    return contract.intent_code, parameters, contract.n_components


# ---------------------------------------------------------------------------
# Pure-NumPy distribution functions (no SciPy dependency)
# ---------------------------------------------------------------------------


def _norm_two_sided_p(x: np.ndarray) -> np.ndarray:
    """Two-sided p-value of a standard normal statistic.

    Equivalent to ``2 * (1 - Phi(|x|))`` and matching the two-tailed
    convention also used for Student's t statistics.
    """
    # erfc(|x| / sqrt(2)) = 2 * sf(|x|); erfc is not vectorized in NumPy, so
    # apply the math function element-wise on the flattened array.
    x = np.abs(np.asarray(x, dtype=np.float64))
    flat = x.ravel()
    out = np.empty(flat.size, dtype=np.float64)
    for index, value in enumerate(flat):
        out[index] = math.erfc(value / math.sqrt(2.0))
    return out.reshape(x.shape)


def _beta_continued_fraction(a: float, b: float, x: np.ndarray) -> np.ndarray:
    """Continued fraction expansion for the regularized incomplete beta."""
    max_iterations = 200
    epsilon = 1e-14
    fpmin = 1e-300

    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = np.ones_like(x, dtype=np.float64)
    d = 1.0 - qab * x / qap
    d = np.where(np.abs(d) < fpmin, fpmin, d)
    d = 1.0 / d
    h = d.copy()

    for iteration in range(1, max_iterations + 1):
        m2 = 2 * iteration
        aa = iteration * (b - iteration) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = np.where(np.abs(d) < fpmin, fpmin, d)
        c = 1.0 + aa / c
        c = np.where(np.abs(c) < fpmin, fpmin, c)
        d = 1.0 / d
        h *= d * c

        aa = -(a + iteration) * (qab + iteration) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = np.where(np.abs(d) < fpmin, fpmin, d)
        c = 1.0 + aa / c
        c = np.where(np.abs(c) < fpmin, fpmin, c)
        d = 1.0 / d
        delta = d * c
        h *= delta

        if np.all(np.abs(delta - 1.0) < epsilon):
            break

    return h


def _betainc(a: float, b: float, x: np.ndarray) -> np.ndarray:
    """Regularized incomplete beta function ``I_x(a, b)``.

    Parameters ``a`` and ``b`` are positive scalars; ``x`` is an array with
    values in ``[0, 1]``. NaN values propagate as NaN.
    """
    x = np.asarray(x, dtype=np.float64)
    result = np.full(x.shape, np.nan, dtype=np.float64)
    result = np.where(x <= 0.0, 0.0, result)
    result = np.where(x >= 1.0, 1.0, result)

    interior = (x > 0.0) & (x < 1.0)
    if not np.any(interior):
        return result

    # Mirror symmetry I_x(a, b) = 1 - I_{1-x}(b, a) keeps the continued
    # fraction on its quickly converging side.
    threshold = (a + 1.0) / (a + b + 2.0)
    mirrored = interior & (x >= threshold)
    direct = interior & ~mirrored
    result = _betainc_direct(a, b, x, direct, result)
    result = _betainc_direct(b, a, 1.0 - x, mirrored, result, complement=True)
    return result


def _betainc_direct(
    a: float,
    b: float,
    x: np.ndarray,
    mask: np.ndarray,
    result: np.ndarray,
    *,
    complement: bool = False,
) -> np.ndarray:
    xi = x[mask]
    with np.errstate(invalid='ignore', divide='ignore'):
        prefix = np.exp(
            a * np.log(xi)
            + b * np.log1p(-xi)
            + math.lgamma(a + b)
            - math.lgamma(a)
            - math.lgamma(b)
        )
    value = prefix * _beta_continued_fraction(a, b, xi) / a
    if complement:
        value = 1.0 - value
    result[mask] = value
    return result


def _t_sf(t: np.ndarray, df: float) -> np.ndarray:
    """Two-sided survival function of Student's t distribution."""
    t = np.asarray(t, dtype=np.float64)
    x = df / (df + t * t)
    # I_{df/(df+t^2)}(df/2, 1/2) gives the two-tailed tail probability.
    return _betainc(df / 2.0, 0.5, x)


def _f_sf(f: np.ndarray, dfn: float, dfd: float) -> np.ndarray:
    """Survival function of the F distribution."""
    f = np.asarray(f, dtype=np.float64)
    x = dfn * f / (dfn * f + dfd)
    return 1.0 - _betainc(dfn / 2.0, dfd / 2.0, x)


# ---------------------------------------------------------------------------
# Converters
# ---------------------------------------------------------------------------


def _log10p_to_p(values: np.ndarray) -> np.ndarray:
    return np.power(10.0, values)


# z maps need no intent parameters, so their converters live in the registry.
register_converter('z', _P_COLUMN, _norm_two_sided_p)
register_converter('z', _LOG_P_COLUMN, lambda values: np.log(_norm_two_sided_p(values)))
register_converter('z', _LOG10_P_COLUMN, lambda values: np.log10(_norm_two_sided_p(values)))

# Probability maps convert with elementary functions only.
register_converter('p', _LOG_P_COLUMN, np.log)
register_converter('p', _LOG10_P_COLUMN, np.log10)
register_converter('log_p', _P_COLUMN, np.exp)
register_converter('log_p', _LOG10_P_COLUMN, lambda values: values / math.log(10.0))
register_converter('log10_p', _P_COLUMN, _log10p_to_p)
register_converter('log10_p', _LOG_P_COLUMN, lambda values: values * math.log(10.0))


def _make_t_sf(df: float) -> Callable[[np.ndarray], np.ndarray]:
    def _convert(values: np.ndarray) -> np.ndarray:
        return _t_sf(values, df)

    return _convert


def _make_f_sf(dfn: float, dfd: float) -> Callable[[np.ndarray], np.ndarray]:
    def _convert(values: np.ndarray) -> np.ndarray:
        return _f_sf(values, dfn, dfd)

    return _convert


def _wrap_log(
    survival: Callable[[np.ndarray], np.ndarray],
    base: float | None,
) -> Callable[[np.ndarray], np.ndarray]:
    if base is None:
        return lambda values: np.log(survival(values))
    return lambda values: np.log(survival(values)) / math.log(base)


# ---------------------------------------------------------------------------
# Contract construction
# ---------------------------------------------------------------------------


def _parameter_specs(header: Nifti1Header, code: int) -> tuple[ParameterSpec, ...]:
    if code in intent_codes:
        descriptions = cast('tuple[str, ...]', intent_codes.parameters[code])
    else:
        descriptions = ()
    specs = []
    for index in range(3):
        field_name = f'intent_p{index + 1}'
        value = float(header[field_name])
        description = descriptions[index] if index < len(descriptions) else ''
        specs.append(
            ParameterSpec(
                name=field_name,
                value=value,
                description=description,
                available=math.isfinite(value) and value != 0.0,
            )
        )
    return tuple(specs)


def _check_parameters(
    statistic: str | None,
    parameters: tuple[ParameterSpec, ...],
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    required: dict[str, tuple[str, ...]] = {
        't': ('intent_p1',),
        't_nonc': ('intent_p1', 'intent_p2'),
        'F': ('intent_p1', 'intent_p2'),
        'F_nonc': ('intent_p1', 'intent_p2', 'intent_p3'),
        'chi2': ('intent_p1',),
        'chi2_nonc': ('intent_p1', 'intent_p2'),
        'chi': ('intent_p1',),
    }
    if statistic is None:
        return issues
    by_name = {spec.name: spec for spec in parameters}
    for name in required.get(statistic, ()):
        spec = by_name[name]
        if not math.isfinite(spec.value) or spec.value == 0.0:
            severity = Severity.ERROR
            if spec.value == 0.0:
                message = f'Required parameter {name} is not set'
                code = 'missing_parameters'
            else:
                message = f'Parameter {name}={spec.value} is not finite'
                code = 'invalid_parameters'
            issues.append(ContractIssue(code, severity, message, parameter=name, value=spec.value))
        elif spec.value < 0.0:
            issues.append(
                ContractIssue(
                    'invalid_parameters',
                    Severity.ERROR,
                    f'Parameter {name}={spec.value} must be non-negative',
                    parameter=name,
                    value=spec.value,
                )
            )
    return issues


def _check_value_range(
    statistic: str | None,
    values: np.ndarray,
) -> tuple[tuple[float, float] | None, list[ContractIssue]]:
    issues: list[ContractIssue] = []
    finite_values = values[np.isfinite(values)]
    if finite_values.size == 0:
        return None, issues
    observed = (float(finite_values.min()), float(finite_values.max()))
    expected = _VALUE_RANGES.get(statistic) if statistic is not None else None
    if expected is not None:
        low, high = expected
        if low is not None and observed[0] < low:
            issues.append(
                ContractIssue(
                    'out_of_range',
                    Severity.WARNING,
                    f'Minimum {observed[0]} is below expected lower bound {low} for {statistic}',
                )
            )
        if high is not None and observed[1] > high:
            issues.append(
                ContractIssue(
                    'out_of_range',
                    Severity.WARNING,
                    f'Maximum {observed[1]} exceeds expected upper bound {high} for {statistic}',
                )
            )
    return observed, issues


def build_contract(img, *, name: str | None = None) -> StatisticContract:
    """Build a :class:`StatisticContract` from a NIfTI image.

    The intent is read exclusively from the header metadata: an unknown intent
    code is reported as an issue and never inferred from the voxel values.

    Parameters
    ----------
    img : ``Nifti1Image`` or ``Nifti2Image``
        Image to describe.
    name : str, optional
        Explicit short name; defaults to the image file basename.

    Returns
    -------
    contract : StatisticContract
        The value contract for ``img``.

    Examples
    --------
    >>> import numpy as np
    >>> from nibabel import Nifti1Image
    >>> img = Nifti1Image(np.zeros((2, 2, 2)), np.eye(4))
    >>> img.header.set_intent('t test', (10,))
    >>> contract = build_contract(img, name='tmap')
    >>> contract.statistic
    't'
    >>> contract.is_valid()
    True
    >>> contract_identity(contract)
    (3, (10.0, 0.0, 0.0), 1)
    """
    header = getattr(img, 'header', None)
    if not isinstance(header, Nifti1Header):
        raise TypeError('Contracts can only be built for NIfTI-1 or NIfTI-2 images')

    code, _, intent_name = header.get_intent('code')
    label = header.get_intent('label')[0]
    parameters = _parameter_specs(header, code)
    statistic = _STATISTIC_BY_CODE.get(code)
    issues: list[ContractIssue] = []

    if code not in intent_codes:
        issues.append(
            ContractIssue(
                'unknown_intent',
                Severity.ERROR,
                f'Unknown NIfTI intent code {code}; statistic cannot be inferred from values',
            )
        )

    issues.extend(_check_parameters(statistic, parameters))

    values = np.asanyarray(img.dataobj)
    shape = tuple(int(dim) for dim in values.shape)
    n_components = int(np.prod(shape[3:])) if len(shape) > 3 else 1
    scalar_intent = code in _SCALAR_INTENT_CODES
    if scalar_intent and n_components > 1:
        issues.append(
            ContractIssue(
                'scalar_intent_with_components',
                Severity.ERROR,
                (
                    f'Intent {label!r} (code {code}) is a per-voxel scalar statistic but the '
                    f'image has {n_components} components per voxel from shape {shape}'
                ),
            )
        )

    has_nan = bool(np.any(np.isnan(values))) if np.issubdtype(values.dtype, np.floating) else False
    has_inf = bool(np.any(np.isinf(values))) if np.issubdtype(values.dtype, np.floating) else False
    finite = not (has_nan or has_inf)
    if has_nan:
        issues.append(ContractIssue('non_finite', Severity.WARNING, 'Image contains NaN values'))
    if has_inf:
        issues.append(
            ContractIssue('non_finite', Severity.WARNING, 'Image contains infinite values')
        )

    value_range, range_issues = _check_value_range(statistic, values)
    issues.extend(range_issues)

    path = img.get_filename() if hasattr(img, 'get_filename') else None
    if name is None and path is not None:
        name = os.path.basename(path)

    contract = StatisticContract(
        name=name,
        path=path,
        intent_code=code,
        intent_label=label,
        intent_name=intent_name,
        statistic=statistic,
        parameters=parameters,
        shape=shape,
        n_components=n_components,
        scalar_intent=scalar_intent,
        value_range=value_range,
        finite=finite,
        has_nan=has_nan,
        has_inf=has_inf,
        issues=tuple(issues),
    )
    object.__setattr__(contract, 'convertible_to', available_targets(contract))
    return contract


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def _resolve_converter(
    contract: StatisticContract,
    rule: ConversionRule,
) -> Callable[[np.ndarray], np.ndarray]:
    """Resolve the vectorized converter implied by a rule and contract."""
    if rule.converter is not None:
        return rule.converter
    statistic = contract.statistic
    target_column = rule.target_column
    if statistic is None:
        raise ValueError(f'No converter available for unknown intent code {contract.intent_code}')

    if statistic == 't':
        df = contract.parameters[0].value
        survival = _make_t_sf(df)
    elif statistic == 'F':
        dfn = contract.parameters[0].value
        dfd = contract.parameters[1].value
        survival = _make_f_sf(dfn, dfd)
    elif (statistic, target_column) in CONVERTERS:
        return CONVERTERS[(statistic, target_column)]
    else:
        raise ValueError(f'No converter registered for {statistic!r} -> {target_column!r}')

    if target_column == _P_COLUMN:
        return survival
    if target_column == _LOG_P_COLUMN:
        return _wrap_log(survival, None)
    if target_column == _LOG10_P_COLUMN:
        return _wrap_log(survival, 10.0)
    raise ValueError(f'No converter registered for {statistic!r} -> {target_column!r}')


def apply_conversion(
    img,
    rule: ConversionRule,
    *,
    data: np.ndarray | None = None,
    name: str | None = None,
) -> ConversionResult:
    """Apply a :class:`ConversionRule` to a NIfTI image.

    The conversion only succeeds when:

    * the rule's ``source_identity`` (when given) matches the image contract;
    * the contract has no error-level issue (unknown intent, missing
      parameters, scalar/vector dimension conflict);
    * all source values are finite;
    * every converted value is inside the rule's target range;
    * the maximum absolute error against the float64 reference, caused by
      casting to the target column precision, is within the tolerance.

    The original array is always returned as ``source_data`` and is never
    modified.

    Parameters
    ----------
    img : ``Nifti1Image`` or ``Nifti2Image``
        Image to convert.
    rule : ConversionRule
        Explicit conversion rule.
    data : numpy array, optional
        Pre-loaded image data; defaults to ``np.asanyarray(img.dataobj)``.
    name : str, optional
        Explicit short name forwarded to :func:`build_contract`.

    Returns
    -------
    result : ConversionResult
        Accepted result carrying both converted and original data, or a
        rejected result carrying the reason codes.
    """
    contract = build_contract(img, name=name)
    source = np.asanyarray(data) if data is not None else np.asanyarray(img.dataobj)

    reasons: list[str] = []
    if rule.source_identity is not None and rule.source_identity != contract_identity(contract):
        raise ValueError(
            'Rule source identity does not match image contract: '
            f'rule={rule.source_identity} image={contract_identity(contract)}'
        )

    blocking = [issue.code for issue in contract.issues if issue.severity is Severity.ERROR]
    if blocking:
        reasons.extend(blocking)

    if not contract.finite:
        reasons.append('non_finite')

    try:
        converter = _resolve_converter(contract, rule)
    except ValueError:
        converter = None
        reasons.append('no_converter')

    converted = None
    max_error = None

    if converter is not None and not reasons:
        reference = np.asarray(converter(source.astype(np.float64)), dtype=np.float64)
        casted = reference.astype(rule.precision)
        low, high = rule.value_range
        finite_reference = np.isfinite(reference)
        if not np.all(finite_reference):
            reasons.append('non_finite_result')
        if np.any(finite_reference & ((reference < low) | (reference > high))):
            reasons.append('out_of_target_range')
        abs_error = np.abs(reference - casted.astype(np.float64))
        max_error = float(np.max(abs_error)) if abs_error.size else 0.0
        if max_error > rule.tolerance:
            reasons.append('tolerance_exceeded')
        if not reasons:
            converted = casted

    return ConversionResult(
        contract=contract,
        target_column=rule.target_column,
        precision=rule.precision,
        tolerance=rule.tolerance,
        source_data=source,
        data=converted,
        max_error=max_error,
        accepted=not reasons,
        reasons=tuple(reasons),
    )
