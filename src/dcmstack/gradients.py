"""Work with diffusion gradient directions alongside DcmMeta meta data.

The functions in this module are intentionally independent of any particular
vendor.  Gradient directions and b-values are read from explicitly named meta
data fields and converted between the ``scanner`` (DICOM patient, LPS),
``world`` (NIfTI RAS) and ``voxel`` bases.

Directions are always treated as vectors, not as coordinates.  An axis
permutation/flip of the voxel array changes components expressed in the
``voxel`` basis, but it does not change a direction expressed in a physical
(scanner or world) basis.  The conversion performed here therefore depends on
both the source and target basis.

Missing or malformed directions are never replaced by a fabricated unit
vector.  Zero gradients stay zero.  Non-zero gradients are transformed as
vectors (their magnitude is preserved unless ``normalize=True`` is requested).
"""
from dataclasses import dataclass, field
import json
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import numpy as np


__all__ = [
    "GradientError",
    "DegenerateTransformError",
    "GradientSample",
    "GradientTable",
    "DEFAULT_GRADIENT_KEYS",
    "DEFAULT_B_VALUE_KEYS",
    "DEFAULT_INSTANCE_KEYS",
    "normalize_basis",
    "basis_change_matrix",
    "coerce_gradient",
    "transform_gradient",
    "transform_gradient_with_status",
    "transform_gradients",
    "ornt_to_matrix",
    "reorient_gradients",
    "sample_meta_value",
    "gradient_table_from_meta",
    "gradients_from_meta",
    "get_gradient_table",
    "original_affine",
    "original_shape",
    "write_bvals_bvecs",
    "write_gradient_table",
]


SCANNER = "scanner"
WORLD = "world"
VOXEL = "voxel"

DEFAULT_GRADIENT_KEYS = (
    "DiffusionGradientOrientation",
    "CsaImage.DiffusionGradientDirection",
)

DEFAULT_B_VALUE_KEYS = (
    "DiffusionBValue",
    "CsaImage.B_value",
    "SIEMENS_MR_HEADER.B_value",
    "SIEMENS_MR_HEADER.0XC",
    "GEMS_PARM_01.0X39",
    "PHILIPS_IMAGING_DD_001.0X3",
)

DEFAULT_INSTANCE_KEYS = (
    "SOPInstanceUID",
    "InstanceNumber",
)

_ZERO_TOL = 1e-12
_MISSING = object()

_BASIS_ALIASES = {
    "scanner": SCANNER,
    "patient": SCANNER,
    "dicom": SCANNER,
    "lps": SCANNER,
    "device": SCANNER,
    "world": WORLD,
    "ras": WORLD,
    "nifti": WORLD,
    "voxel": VOXEL,
    "vox": VOXEL,
    "index": VOXEL,
    "ijk": VOXEL,
    "grid": VOXEL,
}


class GradientError(ValueError):
    """Base class for errors raised while working with gradients."""


class DegenerateTransformError(GradientError):
    """Raised when a spatial transform cannot be applied reliably."""


def normalize_basis(basis: str) -> str:
    """Return the canonical name for one of the supported bases."""
    try:
        key = str(basis).strip().lower()
    except Exception:
        raise ValueError("Invalid gradient basis: %r" % (basis,))
    try:
        return _BASIS_ALIASES[key]
    except KeyError:
        raise ValueError(
            "Unknown gradient basis %r. Expected one of: scanner, voxel, world."
            % (basis,)
        )


def _as_affine(affine, name="affine") -> np.ndarray:
    arr = np.asarray(affine, dtype=np.float64)
    if arr.shape == (4, 4):
        arr = arr[:3, :3]
    if arr.shape != (3, 3):
        raise ValueError("%s must be a 3x3 or 4x4 matrix" % name)
    if not np.all(np.isfinite(arr)):
        raise DegenerateTransformError("%s contains non-finite values" % name)
    return arr


def _check_nonsingular(mat: np.ndarray, name="transform") -> np.ndarray:
    if mat.shape != (3, 3):
        raise DegenerateTransformError("%s must be 3x3" % name)
    if not np.all(np.isfinite(mat)):
        raise DegenerateTransformError("%s contains non-finite values" % name)
    if np.linalg.matrix_rank(mat) < 3:
        raise DegenerateTransformError("%s is singular" % name)
    return mat


def _voxel_axes(affine, name="affine") -> np.ndarray:
    """Unit vectors for the voxel axes, expressed in NIfTI world space."""
    mat = _as_affine(affine, name)
    norms = np.linalg.norm(mat, axis=0)
    if np.any(norms <= _ZERO_TOL) or not np.all(np.isfinite(norms)):
        raise DegenerateTransformError(
            "%s has a zero or non-finite voxel axis" % name
        )
    axes = mat / norms
    return _check_nonsingular(axes, "%s voxel axes" % name)


def _world_to_voxel(affine, name="affine") -> np.ndarray:
    axes = _voxel_axes(affine, name)
    return np.linalg.inv(axes)


def _source_to_world(basis: str, affine, convention: Optional[str]):
    basis = normalize_basis(basis)
    if basis == WORLD:
        conv = _physical_convention(convention, default="ras")
        if conv == "lps":
            return np.diag([-1.0, -1.0, 1.0])
        return np.eye(3)
    if basis == SCANNER:
        conv = _physical_convention(convention, default="lps")
        if conv == "ras":
            return np.eye(3)
        return np.diag([-1.0, -1.0, 1.0])
    if affine is None:
        raise ValueError("A source affine is required for the voxel basis")
    return _voxel_axes(affine, "source_affine")


def _world_to_target(basis: str, affine, convention: Optional[str]):
    basis = normalize_basis(basis)
    if basis == WORLD:
        conv = _physical_convention(convention, default="ras")
        if conv == "lps":
            return np.diag([-1.0, -1.0, 1.0])
        return np.eye(3)
    if basis == SCANNER:
        conv = _physical_convention(convention, default="lps")
        if conv == "ras":
            return np.eye(3)
        return np.diag([-1.0, -1.0, 1.0])
    if affine is None:
        raise ValueError("A target affine is required for the voxel basis")
    return _world_to_voxel(affine, "target_affine")


def _physical_convention(convention, default="lps") -> str:
    if convention is None:
        return default
    if isinstance(convention, dict):
        convention = convention.get("handedness", convention.get("basis"))
    text = str(convention).strip().lower()
    if text in ("lps", "dicom", "scanner", "patient"):
        return "lps"
    if text in ("ras", "nifti", "world"):
        return "ras"
    if text in ("", "none", "default"):
        return default
    raise ValueError("Unknown spatial transform convention: %r" % (convention,))


def _default_convention(basis: str) -> str:
    return "lps" if normalize_basis(basis) == SCANNER else "ras"


def basis_change_matrix(
    source_basis: str = SCANNER,
    target_basis: str = VOXEL,
    source_affine=None,
    target_affine=None,
    source_convention: Optional[str] = None,
    target_convention: Optional[str] = None,
) -> np.ndarray:
    """Get the 3x3 matrix mapping source-basis components to target-basis.

    ``scanner`` is the DICOM patient coordinate system (LPS by default),
    ``world`` is the NIfTI world coordinate system (RAS by default) and
    ``voxel`` uses the unit vectors of the supplied affine's columns.

    The returned matrix ``M`` satisfies ``target = M @ source``.
    """
    src = normalize_basis(source_basis)
    tgt = normalize_basis(target_basis)
    if src == tgt:
        if src == VOXEL:
            # Even within the voxel basis, the source and target arrays may
            # have different axes (for example before and after a
            # reorientation).
            if source_affine is None or target_affine is None:
                return np.eye(3)
            src_axes = _voxel_axes(source_affine, "source_affine")
            tgt_axes = _voxel_axes(target_affine, "target_affine")
            return _check_nonsingular(
                np.linalg.inv(tgt_axes) @ src_axes, "voxel basis change"
            )
        # The basis name may be the same while the physical convention
        # differs (for example scanner values supplied as RAS rather than
        # the default LPS).
        src_conv = _physical_convention(source_convention,
                                        default=_default_convention(src))
        tgt_conv = _physical_convention(target_convention,
                                        default=_default_convention(tgt))
        if src_conv == tgt_conv:
            return np.eye(3)
        return np.diag([-1.0, -1.0, 1.0])
    src_to_world = _source_to_world(src, source_affine, source_convention)
    world_to_tgt = _world_to_target(tgt, target_affine, target_convention)
    return _check_nonsingular(world_to_tgt @ src_to_world, "basis change")


def coerce_gradient(value) -> Tuple[Optional[np.ndarray], str]:
    """Convert a meta data value to a gradient vector and a status string.

    The status is one of ``ok``, ``zero``, ``incomplete`` or ``missing``.
    ``None`` is returned for missing/incomplete values.  No components are
    invented and no unit vector is fabricated.
    """
    if value is _MISSING or value is None:
        return None, "missing"
    if isinstance(value, str):
        text = value.strip()
        if text == "":
            return None, "missing"
        text = text.replace("\\", " ").replace(",", " ")
        parts = text.split()
        if len(parts) != 3:
            return None, "incomplete"
        try:
            value = [float(part) for part in parts]
        except ValueError:
            return None, "incomplete"
    if isinstance(value, np.ndarray):
        if value.ndim == 0:
            value = [value.item()]
        else:
            value = value.ravel().tolist()
    elif not isinstance(value, (list, tuple)):
        try:
            if hasattr(value, "__len__") and not isinstance(value, (bytes, bytearray)):
                value = list(value)
            else:
                return None, "incomplete"
        except Exception:
            return None, "incomplete"
    if len(value) != 3:
        return None, "incomplete"
    try:
        vector = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError):
        return None, "incomplete"
    if vector.shape != (3,):
        return None, "incomplete"

    if not np.all(np.isfinite(vector)):
        return None, "incomplete"
    if np.all(np.abs(vector) <= _ZERO_TOL):
        return np.zeros(3, dtype=np.float64), "zero"
    return vector, "ok"


@dataclass
class GradientTransformResult:
    """The result of transforming a single gradient value."""

    gradient: Optional[np.ndarray]
    status: str
    source_gradient: Optional[np.ndarray]
    transform: Optional[np.ndarray] = None
    detail: Optional[str] = None


def transform_gradient_with_status(
    gradient,
    source_basis: str = SCANNER,
    target_basis: str = VOXEL,
    source_affine=None,
    target_affine=None,
    source_convention: Optional[str] = None,
    target_convention: Optional[str] = None,
    transform=None,
    normalize: bool = False,
    zero_tol: float = _ZERO_TOL,
    on_degenerate: str = "raise",
) -> GradientTransformResult:
    """Transform one gradient and retain information about how it was handled.

    ``transform`` may be supplied as a 3x3/4x4 matrix.  It is interpreted as
    mapping source-basis components to target-basis components and overrides
    the basis/affine arguments.  A nibabel orientation array (3x2) is also
    accepted and is converted with :func:`ornt_to_matrix`.
    """
    source_vector, status = coerce_gradient(gradient)
    if status in ("missing", "incomplete"):
        return GradientTransformResult(None, status, source_vector, detail=status)
    if status == "zero":
        # The zero vector maps to zero under any linear transform.  Do not
        # invent a direction or normalize it.
        return GradientTransformResult(
            np.zeros(3), "zero", source_vector, transform=None
        )

    if transform is not None:
        arr = np.asarray(transform)
        if arr.shape == (3, 2):
            mat = ornt_to_matrix(arr)
        else:
            mat = _as_affine(arr, "transform")
        try:
            mat = _check_nonsingular(mat, "transform")
        except DegenerateTransformError as err:
            if on_degenerate == "status":
                return GradientTransformResult(
                    None, "degenerate", source_vector, detail=str(err)
                )
            raise
    else:
        try:
            mat = basis_change_matrix(
                source_basis,
                target_basis,
                source_affine,
                target_affine,
                source_convention,
                target_convention,
            )
        except DegenerateTransformError as err:
            if on_degenerate == "status":
                return GradientTransformResult(
                    None, "degenerate", source_vector, detail=str(err)
                )
            raise

    result = mat @ source_vector
    if normalize:
        norm = np.linalg.norm(result)
        if norm > zero_tol:
            result = result / norm
    return GradientTransformResult(result, "ok", source_vector, mat)


def transform_gradient(
    gradient,
    source_basis: str = SCANNER,
    target_basis: str = VOXEL,
    source_affine=None,
    target_affine=None,
    source_convention: Optional[str] = None,
    target_convention: Optional[str] = None,
    transform=None,
    normalize: bool = False,
    on_degenerate: str = "raise",
) -> Optional[np.ndarray]:
    """Transform a single gradient vector.

    Returns ``None`` for a missing or incomplete direction.  Zero gradients
    are returned as a zero vector.  Use :func:`transform_gradient_with_status`
    when the reason for a ``None`` result matters.
    """
    result = transform_gradient_with_status(
        gradient,
        source_basis=source_basis,
        target_basis=target_basis,
        source_affine=source_affine,
        target_affine=target_affine,
        source_convention=source_convention,
        target_convention=target_convention,
        transform=transform,
        normalize=normalize,
        on_degenerate=on_degenerate,
    )
    return result.gradient


def transform_gradients(
    gradients,
    source_basis: str = SCANNER,
    target_basis: str = VOXEL,
    source_affine=None,
    target_affine=None,
    source_convention: Optional[str] = None,
    target_convention: Optional[str] = None,
    transform=None,
    normalize: bool = False,
    on_degenerate: str = "raise",
) -> np.ndarray:
    """Transform a sequence of gradients, returning an ``(N, 3)`` array.

    Missing/incomplete directions are represented by ``nan`` rows.
    Degenerate transforms raise :class:`DegenerateTransformError` unless
    ``on_degenerate='status'``, in which case the affected row is ``nan``.
    """
    rows = []
    for gradient in gradients:
        result = transform_gradient_with_status(
            gradient,
            source_basis=source_basis,
            target_basis=target_basis,
            source_affine=source_affine,
            target_affine=target_affine,
            source_convention=source_convention,
            target_convention=target_convention,
            transform=transform,
            normalize=normalize,
            on_degenerate=on_degenerate,
        )
        if result.gradient is None:
            rows.append([np.nan, np.nan, np.nan])
        else:
            rows.append(result.gradient)
    if not rows:
        return np.empty((0, 3), dtype=np.float64)
    return np.asarray(rows, dtype=np.float64)


def ornt_to_matrix(ornt) -> np.ndarray:
    """Convert a nibabel orientation array to a 3x3 vector transform.

    The returned matrix maps components in the old voxel basis to components
    in the new voxel basis (``new = M @ old``).  This is the inverse of the
    voxel-index affine returned by ``nibabel.orientations.inv_ornt_aff``.
    """
    arr = np.asarray(ornt, dtype=np.float64)
    if arr.shape != (3, 2):
        raise ValueError("An orientation array must have shape (3, 2)")
    mat = np.zeros((3, 3), dtype=np.float64)
    for old_axis, (new_axis, flip) in enumerate(arr):
        new_idx = int(new_axis)
        if not 0 <= new_idx < 3:
            raise ValueError("Invalid axis in orientation transform")
        if int(flip) not in (-1, 1):
            raise ValueError("Invalid flip in orientation transform")
        mat[new_idx, old_axis] = float(flip)
    if np.linalg.matrix_rank(mat) < 3:
        raise DegenerateTransformError("The orientation transform is singular")
    return mat


def reorient_gradients(
    gradients,
    source_affine=None,
    target_affine=None,
    source_basis: str = VOXEL,
    target_basis: Optional[str] = None,
    ornt=None,
    shape=None,
    inverse: bool = False,
    source_convention: Optional[str] = None,
    target_convention: Optional[str] = None,
    normalize: bool = False,
    on_degenerate: str = "raise",
) -> np.ndarray:
    """Transform gradients through a voxel reorientation.

    ``ornt`` is a nibabel orientation array (as returned by
    ``nibabel.orientations.ornt_transform``).  When ``target_affine`` is not
    supplied it is computed from ``source_affine`` and ``ornt`` using the
    source array's spatial ``shape``.

    The conversion is basis aware.  Directions in a physical basis do not
    change merely because the voxel axes were permuted or flipped; directions
    in the voxel basis are permuted/flipped accordingly.

    Set ``inverse=True`` to perform the inverse conversion.  The inverse maps
    the target basis back to the source basis and uses the swapped affines, so
    a forward conversion followed by its inverse recovers the original
    vectors.
    """
    if target_basis is None:
        target_basis = source_basis
    src_basis = normalize_basis(source_basis)
    tgt_basis = normalize_basis(target_basis)
    src_aff = source_affine
    tgt_aff = target_affine
    src_conv = source_convention
    tgt_conv = target_convention

    mat = None
    if ornt is not None:
        ornt = np.asarray(ornt)
        mat = ornt_to_matrix(ornt)
        if target_affine is None:
            if source_affine is None:
                raise ValueError("source_affine or target_affine is required")
            if shape is None:
                raise ValueError("shape is required to compute target_affine")
            from nibabel.orientations import inv_ornt_aff

            aff_trans = inv_ornt_aff(ornt, tuple(shape))
            tgt_aff = np.asarray(source_affine) @ aff_trans
        elif source_affine is None:
            from nibabel.orientations import inv_ornt_aff

            if shape is None:
                raise ValueError("shape is required to compute source_affine")
            aff_trans = inv_ornt_aff(ornt, tuple(shape))
            src_aff = np.asarray(target_affine) @ np.linalg.inv(aff_trans)
        # The matrix applies directly in the voxel basis.  For physical bases
        # it must not be applied; use the affines to express the direction in
        # the new voxel axes instead.
        if src_basis == VOXEL and tgt_basis == VOXEL:
            transform = np.linalg.inv(mat) if inverse else mat
            return transform_gradients(
                gradients,
                transform=transform,
                normalize=normalize,
                on_degenerate=on_degenerate,
            )

    if inverse:
        # The inverse maps target-basis components back to the source basis.
        src_basis, tgt_basis = tgt_basis, src_basis
        src_aff, tgt_aff = tgt_aff, src_aff
        src_conv, tgt_conv = tgt_conv, src_conv

    return transform_gradients(
        gradients,
        source_basis=src_basis,
        target_basis=tgt_basis,
        source_affine=src_aff,
        target_affine=tgt_aff,
        source_convention=src_conv,
        target_convention=tgt_conv,
        normalize=normalize,
        on_degenerate=on_degenerate,
    )



def _shape_dims(meta):
    shape = tuple(meta.shape)
    n_slices = shape[meta.slice_dim] if meta.slice_dim is not None else 1
    n_time = shape[3] if len(shape) > 3 else 1
    n_vector = shape[4] if len(shape) > 4 else 1
    return shape, n_slices, n_time, n_vector


def _equal_values(a, b) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return bool(np.array_equal(np.asarray(a), np.asarray(b)))
    except Exception:
        return a == b


def sample_meta_value(meta, key: str, time_index: int = 0,
                      vector_index: int = 0, slice_index: int = 0):
    """Get the value of ``key`` for one actual sample.

    The lookup honours all of the DcmMeta classifications, including values
    that vary per slice, per time sample, per vector sample, or any
    combination.  ``_MISSING`` is returned if the key is absent or the
    requested sample is out of range.
    """
    values, cls = meta.get_values_and_class(key)
    if cls is None:
        return _MISSING
    if cls == ("global", "const"):
        return values
    if cls[1] == "slices" and meta.slice_dim is None:
        return _MISSING
    shape, n_slices, n_time, n_vector = _shape_dims(meta)
    try:
        if cls == ("time", "samples"):
            if n_vector > 1:
                idx = int(vector_index) * n_time + int(time_index)
            else:
                idx = int(time_index)
        elif cls == ("vector", "samples"):
            idx = int(vector_index)
        elif cls == ("global", "slices"):
            idx = ((int(vector_index) * n_time + int(time_index))
                   * n_slices + int(slice_index))
        elif cls == ("time", "slices"):
            idx = int(slice_index)
        elif cls == ("vector", "slices"):
            idx = int(time_index) * n_slices + int(slice_index)
        else:
            return _MISSING
        return values[idx]
    except (IndexError, TypeError, KeyError):
        return _MISSING


def _first_value(meta, keys, time_index, vector_index, slice_index):
    for key in keys:
        value = sample_meta_value(
            meta, key, time_index, vector_index, slice_index
        )
        if value is not _MISSING:
            return key, value
    return None, _MISSING


def _values_vary_over_slices(meta, keys, time_index, vector_index) -> bool:
    _, n_slices, _, _ = _shape_dims(meta)
    if n_slices <= 1:
        return False
    _, first = _first_value(meta, keys, time_index, vector_index, 0)
    for slice_index in range(1, n_slices):
        _, value = _first_value(
            meta, keys, time_index, vector_index, slice_index
        )
        if not _equal_values(first, value):
            return True
    return False


def _original_affine(meta) -> np.ndarray:
    affine = np.asarray(meta.affine, dtype=np.float64)
    reorient = meta.reorient_transform
    if reorient is None:
        return affine
    try:
        return affine @ np.linalg.inv(np.asarray(reorient, dtype=np.float64))
    except np.linalg.LinAlgError:
        return affine


def original_affine(meta) -> np.ndarray:
    """Get the affine of the voxel array before the recorded reorientation.

    This is the affine whose voxel axes the source DICOM gradient directions
    were expressed in when they were stored as voxel-basis values.
    """
    return _original_affine(meta)


def original_shape(meta) -> Tuple[int, ...]:
    """Get the shape before the recorded reorientation, if possible.

    The stored reorientation transform maps delivered (new) voxel indices to
    the original (source) voxel indices, so each of its columns describes one
    delivered axis.
    """
    shape = tuple(int(x) for x in meta.shape)
    reorient = meta.reorient_transform
    if reorient is None:
        return shape
    mat = np.asarray(reorient, dtype=np.float64)[:3, :3]
    try:
        old_shape = list(shape[:3])
        for new_axis in range(3):
            col = np.abs(mat[:, new_axis])
            if not np.any(col > 0.5):
                return shape
            old_axis = int(np.argmax(col))
            old_shape[old_axis] = shape[new_axis]
        return tuple(old_shape) + shape[3:]
    except (IndexError, ValueError):
        return shape


def _source_slice_order(meta) -> List[int]:
    """Map each delivered slice index to its index in the source array.

    ``DicomStack.to_nifti`` reorders the source files before building the
    extension, so per-slice meta data is already stored in the delivered
    slice order.  The recorded reorientation transform must therefore not be
    applied to the per-slice meta data indices.
    """
    shape = tuple(meta.shape)
    if meta.slice_dim is None:
        return [0]
    return list(range(shape[meta.slice_dim]))


def _as_keys(keys) -> Tuple[str, ...]:
    if keys is None:
        return tuple()
    if isinstance(keys, str):
        return (keys,)
    return tuple(keys)


@dataclass
class GradientSample:
    """One output sample and its corresponding source meta data."""

    gradient: Optional[np.ndarray]
    b_value: Optional[float]
    status: str = "ok"
    time_index: int = 0
    vector_index: int = 0
    slice_index: Optional[int] = None
    slice_indices: List[int] = field(default_factory=list)
    voxel_index: Optional[Tuple[int, ...]] = None
    source_gradient: Optional[np.ndarray] = None
    source_basis: str = SCANNER
    target_basis: str = VOXEL
    instance: Optional[str] = None
    source_instances: List[Any] = field(default_factory=list)
    source_index: Optional[int] = None
    source_indices: List[int] = field(default_factory=list)
    gradient_key: Optional[str] = None
    b_value_key: Optional[str] = None
    detail: Optional[str] = None
    transform: Optional[np.ndarray] = None
    volume_index: Optional[int] = None

    @property
    def is_zero(self) -> bool:
        return self.status == "zero"

    @property
    def is_valid(self) -> bool:
        return self.gradient is not None

    def to_dict(self) -> Dict[str, Any]:
        def _vec(value):
            if value is None:
                return None
            arr = np.asarray(value, dtype=np.float64).ravel()
            return [float(x) if np.isfinite(x) else None for x in arr]

        return {
            "gradient": _vec(self.gradient),
            "b_value": (None if self.b_value is None
                        else float(self.b_value)),
            "status": self.status,
            "time_index": int(self.time_index),
            "vector_index": int(self.vector_index),
            "slice_index": (None if self.slice_index is None
                            else int(self.slice_index)),
            "slice_indices": [int(x) for x in self.slice_indices],
            "voxel_index": (None if self.voxel_index is None
                            else [x if x is None else int(x)
                                  for x in self.voxel_index]),
            "source_gradient": _vec(self.source_gradient),
            "gradient_key": self.gradient_key,
            "b_value_key": self.b_value_key,
            "instance": self.instance,
            "source_instances": list(self.source_instances),
            "source_index": (None if self.source_index is None
                             else int(self.source_index)),
            "source_indices": [int(x) for x in self.source_indices],
            "volume_index": (None if self.volume_index is None
                             else int(self.volume_index)),
            "detail": self.detail,
        }


@dataclass
class GradientTable:
    """A table of gradient samples aligned with a (possibly reoriented) image."""

    samples: List[GradientSample]
    shape: Tuple[int, ...] = ()
    affine: Optional[np.ndarray] = None
    source_basis: str = SCANNER
    target_basis: str = VOXEL
    voxel_order: Optional[str] = None
    source_convention: Optional[str] = None
    target_convention: Optional[str] = None
    original_affine: Optional[np.ndarray] = None
    original_shape: Optional[Tuple[int, ...]] = None
    reorient_transform: Optional[np.ndarray] = None

    def __len__(self) -> int:
        return len(self.samples)

    def __iter__(self):
        return iter(self.samples)

    def __getitem__(self, idx):
        return self.samples[idx]

    @property
    def gradients(self) -> np.ndarray:
        rows = []
        for sample in self.samples:
            if sample.gradient is None:
                rows.append([np.nan, np.nan, np.nan])
            else:
                rows.append(np.asarray(sample.gradient, dtype=np.float64))
        if not rows:
            return np.empty((0, 3), dtype=np.float64)
        return np.asarray(rows, dtype=np.float64)

    @property
    def b_values(self) -> np.ndarray:
        return np.asarray(
            [np.nan if s.b_value is None else float(s.b_value)
             for s in self.samples],
            dtype=np.float64,
        )

    @property
    def bvals(self) -> np.ndarray:
        return self.b_values

    @property
    def bvecs(self) -> np.ndarray:
        return self.gradients

    @property
    def fsl_bvecs(self) -> np.ndarray:
        return self.gradients.T

    @property
    def instances(self) -> List[Optional[str]]:
        return [sample.instance for sample in self.samples]

    @property
    def statuses(self) -> List[str]:
        return [sample.status for sample in self.samples]

    def to_dict(self) -> Dict[str, Any]:
        def _affine(value):
            if value is None:
                return None
            return [[float(x) for x in row] for row in np.asarray(value)]

        return {
            "shape": [int(x) for x in self.shape],
            "affine": _affine(self.affine),
            "original_affine": _affine(self.original_affine),
            "original_shape": (None if self.original_shape is None
                               else [int(x) for x in self.original_shape]),
            "reorient_transform": _affine(self.reorient_transform),
            "source_basis": self.source_basis,
            "target_basis": self.target_basis,
            "voxel_order": self.voxel_order,
            "source_convention": self.source_convention,
            "target_convention": self.target_convention,
            "samples": [sample.to_dict() for sample in self.samples],
        }

    def to_json(self, indent: int = 4) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    def write_json(self, path: str, indent: int = 4) -> None:
        with open(path, "w") as fp:
            fp.write(self.to_json(indent=indent))
            fp.write("\n")

    def transform(
        self,
        target_basis: str,
        target_affine=None,
        target_convention: Optional[str] = None,
        source_convention: Optional[str] = None,
        normalize: bool = False,
        on_degenerate: str = "raise",
    ) -> "GradientTable":
        """Return a copy with gradients expressed in a new target basis.

        The b-values and all source instance/index information are preserved.
        ``target_affine`` defaults to the table's current affine when the
        target basis is ``voxel``.
        """
        result = self.copy()
        new_basis = normalize_basis(target_basis)
        if new_basis == VOXEL and target_affine is None:
            target_affine = self.affine
        for sample in result.samples:
            transformed = transform_gradient_with_status(
                sample.gradient,
                source_basis=sample.target_basis,
                target_basis=new_basis,
                source_affine=self.affine,
                target_affine=target_affine,
                source_convention=source_convention,
                target_convention=target_convention,
                normalize=normalize,
                on_degenerate=on_degenerate,
            )
            if transformed.gradient is None and sample.status == "zero":
                transformed = GradientTransformResult(
                    np.zeros(3), "zero", sample.source_gradient
                )
            elif sample.status == "incomplete":
                transformed = GradientTransformResult(
                    None, "incomplete", sample.source_gradient
                )
            sample.gradient = transformed.gradient
            sample.status = transformed.status
            sample.target_basis = new_basis
            sample.transform = (None if transformed.transform is None
                                else np.asarray(transformed.transform))
        result.target_basis = new_basis
        if target_affine is not None:
            result.affine = np.asarray(target_affine, dtype=np.float64)
        result.target_convention = target_convention
        if source_convention is not None:
            result.source_convention = source_convention
        return result

    def inverse(
        self,
        source_basis: str = SCANNER,
        source_affine=None,
        source_convention: Optional[str] = None,
        target_convention: Optional[str] = None,
        normalize: bool = False,
        on_degenerate: str = "raise",
    ) -> "GradientTable":
        """Return a copy expressed back in ``source_basis``.

        The source affine defaults to the table's ``original_affine``.  The
        forward/inverse pair preserves b-values, source instances and the
        original gradient vectors for non-degenerate transforms.
        """
        result = self.copy()
        new_basis = normalize_basis(source_basis)
        if source_affine is None:
            source_affine = self.original_affine
        for sample in result.samples:
            transformed = transform_gradient_with_status(
                sample.gradient,
                source_basis=sample.target_basis,
                target_basis=new_basis,
                source_affine=self.affine,
                target_affine=source_affine,
                source_convention=target_convention,
                target_convention=source_convention,
                normalize=normalize,
                on_degenerate=on_degenerate,
            )
            if transformed.gradient is None and sample.status == "zero":
                transformed = GradientTransformResult(
                    np.zeros(3), "zero", sample.source_gradient
                )
            elif sample.status == "incomplete":
                transformed = GradientTransformResult(
                    None, "incomplete", sample.source_gradient
                )
            sample.gradient = transformed.gradient
            sample.status = transformed.status
            sample.target_basis = new_basis
            sample.transform = (None if transformed.transform is None
                                else np.asarray(transformed.transform))
        result.target_basis = new_basis
        result.affine = np.asarray(source_affine, dtype=np.float64)
        result.target_convention = source_convention
        if (result.original_affine is not None and
                np.allclose(np.asarray(result.original_affine,
                                       dtype=np.float64),
                            np.asarray(source_affine, dtype=np.float64))):
            # We are back in the geometry the table was built from.
            result.shape = (tuple(result.original_shape)
                            if result.original_shape is not None
                            else result.shape)
            result.original_affine = None
            result.original_shape = None
            result.reorient_transform = None
        return result

    def copy(self) -> "GradientTable":
        from copy import deepcopy
        return deepcopy(self)


def _instance_values(meta, keys, time_index, vector_index,
                     slice_index=None):
    values = []
    _, n_slices, _, _ = _shape_dims(meta)
    per_slice = False
    for key in keys:
        cls = meta.get_classification(key)
        if cls is not None:
            per_slice = cls[1] == "slices"
            break
    allowed = [key for key in keys
               if meta.get_classification(key) is not None]
    if not allowed:
        return values
    if slice_index is not None:
        slice_indices = [slice_index]
    elif per_slice:
        slice_indices = range(n_slices)
    else:
        slice_indices = [0]
    for slc_idx in slice_indices:
        _, value = _first_value(
            meta, allowed, time_index, vector_index, slc_idx
        )
        if value is not _MISSING and value is not None:
            values.append(value)
    return values


def _unique_or_list(values):
    if not values:
        return None, []
    unique = []
    for value in values:
        if value not in unique:
            unique.append(value)
    return (unique[0] if len(unique) == 1 else None), list(values)


def _build_result(
    source_value,
    source_basis,
    target_basis,
    source_affine,
    target_affine,
    source_convention,
    target_convention,
    normalize,
    on_degenerate,
    b_value=None,
    zero_b_value_gradient=False,
):
    transformed = transform_gradient_with_status(
        source_value,
        source_basis=source_basis,
        target_basis=target_basis,
        source_affine=source_affine,
        target_affine=target_affine,
        source_convention=source_convention,
        target_convention=target_convention,
        normalize=normalize,
        on_degenerate=on_degenerate,
    )
    if (transformed.gradient is None and
            transformed.status == "missing" and
            zero_b_value_gradient and
            b_value is not None and
            abs(float(b_value)) <= _ZERO_TOL):
        # A b=0 image is legitimately represented by a zero direction.  This
        # is a zero vector, not an invented unit direction.
        transformed = GradientTransformResult(
            np.zeros(3), "zero", None, detail="b_value_zero"
        )
    return transformed


def gradient_table_from_meta(
    meta,
    gradient_keys=DEFAULT_GRADIENT_KEYS,
    b_value_keys=DEFAULT_B_VALUE_KEYS,
    instance_keys=DEFAULT_INSTANCE_KEYS,
    source_basis: str = SCANNER,
    target_basis: str = VOXEL,
    source_affine=None,
    target_affine=None,
    source_convention: Optional[str] = None,
    target_convention: Optional[str] = None,
    normalize: bool = False,
    zero_b_value_gradient: bool = False,
    on_degenerate: str = "raise",
    source_index_map: Optional[Callable[[int, int, int], Iterable[int]]] = None,
) -> GradientTable:
    """Build a :class:`GradientTable` from a DcmMeta extension.

    ``gradient_keys`` and ``b_value_keys`` are explicit field names (a string
    or a sequence).  The first field present for a given sample is used.  All
    DcmMeta classifications are supported, so values that vary per slice,
    time sample or vector sample are selected using the actual sample index.

    ``source_affine`` should describe the source voxel axes.  If omitted, it
    is reconstructed from ``meta.affine`` and ``meta.reorient_transform``.
    ``target_affine`` defaults to ``meta.affine``.

    One sample is produced per actual image volume (time x vector).  If either
    the gradient or b-value varies across slices within a volume, one sample
    is produced per slice instead, so that no sample silently discards
    differing values.  A missing direction is never replaced by a unit vector.
    ``zero_b_value_gradient`` can be set to True to explicitly represent b=0
    volumes that have no direction field as zero directions; the default is
    False, which leaves them missing.
    """
    gradient_keys = _as_keys(gradient_keys) or DEFAULT_GRADIENT_KEYS
    b_value_keys = _as_keys(b_value_keys) or DEFAULT_B_VALUE_KEYS
    instance_keys = _as_keys(instance_keys) or DEFAULT_INSTANCE_KEYS
    source_basis = normalize_basis(source_basis)
    target_basis = normalize_basis(target_basis)

    if target_affine is None:
        target_affine = np.asarray(meta.affine, dtype=np.float64)
    if source_affine is None:
        source_affine = _original_affine(meta)

    shape, n_slices, n_time, n_vector = _shape_dims(meta)
    slice_dim = meta.slice_dim
    source_slice_order = _source_slice_order(meta)

    samples: List[GradientSample] = []
    for vector_index in range(n_vector):
        for time_index in range(n_time):
            per_slice = (
                _values_vary_over_slices(
                    meta, gradient_keys, time_index, vector_index
                ) or
                _values_vary_over_slices(
                    meta, b_value_keys, time_index, vector_index
                )
            )
            if per_slice:
                slices_to_emit = list(range(n_slices))
            else:
                slices_to_emit = [None]
            for slice_index in slices_to_emit:
                if slice_index is None:
                    source_slice = 0
                else:
                    source_slice = source_slice_order[slice_index]
                gradient_key, gradient_value = _first_value(
                    meta, gradient_keys, time_index, vector_index,
                    source_slice,
                )
                b_key, b_value = _first_value(
                    meta, b_value_keys, time_index, vector_index,
                    source_slice,
                )
                if b_value is _MISSING or b_value is None:
                    b_value = None
                else:
                    try:
                        b_value = float(b_value)
                    except (TypeError, ValueError):
                        b_value = None

                transformed = _build_result(
                    gradient_value,
                    source_basis,
                    target_basis,
                    source_affine,
                    target_affine,
                    source_convention,
                    target_convention,
                    normalize,
                    on_degenerate,
                    b_value=b_value,
                    zero_b_value_gradient=zero_b_value_gradient,
                )

                instance_values = _instance_values(
                    meta, instance_keys, time_index, vector_index,
                    slice_index=(source_slice if per_slice else None),
                )
                instance, source_instances = _unique_or_list(instance_values)

                source_indices: List[int] = []
                if source_index_map is not None:
                    src_idx = 0 if slice_index is None else source_slice
                    source_indices = list(source_index_map(
                        time_index, vector_index, src_idx
                    ))
                elif meta.slice_dim is not None:
                    start = ((vector_index * n_time + time_index) * n_slices)
                    if per_slice:
                        source_indices = [start + source_slice]
                    else:
                        source_indices = list(
                            start + idx for idx in source_slice_order
                        )
                source_index = (source_indices[0]
                                if source_indices else None)
                volume_index = vector_index * n_time + time_index

                if per_slice:
                    voxel_index = [0, 0, 0]
                    if slice_dim is not None:
                        voxel_index[slice_dim] = int(slice_index)
                else:
                    voxel_index = [0, 0, 0]
                    if slice_dim is not None:
                        voxel_index[slice_dim] = None

                sample = GradientSample(
                    gradient=transformed.gradient,
                    b_value=b_value,
                    status=transformed.status,
                    time_index=time_index,
                    vector_index=vector_index,
                    slice_index=slice_index,
                    slice_indices=(
                        [int(slice_index)] if per_slice
                        else list(range(n_slices))
                    ),
                    voxel_index=(None if slice_dim is None
                                 else tuple(voxel_index)),
                    source_gradient=transformed.source_gradient,
                    source_basis=source_basis,
                    target_basis=target_basis,
                    instance=instance,
                    source_instances=source_instances,
                    source_index=source_index,
                    source_indices=source_indices,
                    gradient_key=gradient_key,
                    b_value_key=b_key,
                    detail=transformed.detail,
                    transform=(None if transformed.transform is None
                               else np.asarray(transformed.transform)),
                    volume_index=volume_index,
                )
                samples.append(sample)

    return GradientTable(
        samples=samples,
        shape=shape,
        affine=np.asarray(target_affine, dtype=np.float64),
        original_affine=np.asarray(source_affine, dtype=np.float64),
        original_shape=original_shape(meta),
        reorient_transform=meta.reorient_transform,
        source_basis=source_basis,
        target_basis=target_basis,
        source_convention=source_convention,
        target_convention=target_convention,
    )


def gradients_from_meta(meta, **kwargs) -> GradientTable:
    """Alias for :func:`gradient_table_from_meta`."""
    return gradient_table_from_meta(meta, **kwargs)


def get_gradient_table(meta, **kwargs) -> GradientTable:
    """Alias for :func:`gradient_table_from_meta`."""
    return gradient_table_from_meta(meta, **kwargs)


def write_bvals_bvecs(
    b_values,
    gradients,
    bval_path: Optional[str] = None,
    bvec_path: Optional[str] = None,
) -> None:
    """Write FSL-style ``.bval`` and ``.bvec`` files.

    ``gradients`` must be an ``(N, 3)`` array (or an object with a
    ``gradients`` property).  ``.bvec`` is written with three rows and N
    columns, as expected by FSL.  Missing directions are written as zero and
    the accompanying JSON sidecar should be used to distinguish them from
    genuine zero gradients.
    """
    if hasattr(gradients, "gradients"):
        gradients = gradients.gradients
    gradients = np.asarray(gradients, dtype=np.float64)
    if gradients.ndim != 2 or gradients.shape[1] != 3:
        raise ValueError("gradients must have shape (N, 3)")
    b_values = np.asarray(b_values, dtype=np.float64).ravel()
    if len(b_values) != len(gradients):
        raise ValueError("b-values and gradients must have the same length")
    safe = np.nan_to_num(gradients, nan=0.0, posinf=0.0, neginf=0.0)
    if bval_path is not None:
        with open(bval_path, "w") as fp:
            fp.write(" ".join("%.10g" % value for value in b_values))
            fp.write("\n")
    if bvec_path is not None:
        with open(bvec_path, "w") as fp:
            for axis in range(3):
                fp.write(" ".join("%.10g" % value for value in safe[:, axis]))
                fp.write("\n")


def write_gradient_table(
    table: GradientTable,
    json_path: Optional[str] = None,
    bval_path: Optional[str] = None,
    bvec_path: Optional[str] = None,
) -> None:
    """Write a gradient table as JSON and/or FSL ``.bval``/``.bvec`` files."""
    if hasattr(table, "samples"):
        samples = table.samples
        b_values = table.b_values
        gradients = table.gradients
    else:
        raise TypeError("Expected a GradientTable")
    if json_path is not None:
        table.write_json(json_path)
    if bval_path is not None or bvec_path is not None:
        write_bvals_bvecs(b_values, gradients, bval_path, bvec_path)
