# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the NiBabel package for the
#   copyright and license terms.
#
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Correct NIfTI qform / sform declarations against an authorized target.

The qform and the sform are independent declarations, each carrying its own
space code.  Correcting one of them must not change the meaning of the other,
and must not hide geometry that the target field cannot represent.

The qform can only express a rotation, per-axis zooms, a translation and a
single reflection encoded through ``qfac`` (an orthogonal column basis).  A
target containing shears, or any other non-orthogonal column basis, cannot be
written to the qform without changing the transform.  The policy implemented
here is to refuse such a write (or to keep the transform in the sform, which
can store any affine) rather than silently orthogonalizing it.

All checks happen on a deep copy, so a refused correction never mutates the
input image.
"""

from __future__ import annotations

from copy import deepcopy
from itertools import product
from typing import NamedTuple, cast

import numpy as np
import numpy.linalg as npl

from .affines import apply_affine
from .nifti1 import Nifti1Pair, xform_codes
from .spatialimages import HeaderDataError

__all__ = [
    'ConflictingXformsError',
    'CorrectionResult',
    'DegenerateAffineError',
    'QformNotExpressibleError',
    'UndefinedXformCodeError',
    'XformCorrectionError',
    'XformDeclarations',
    'check_xforms',
    'correct_qform',
    'correct_sform',
    'get_xform_declarations',
    'grid_corners',
    'positions_agree',
    'qform_capable',
    'world_positions',
]


class XformCorrectionError(HeaderDataError):
    """Base class for errors raised while correcting qform / sform fields."""


class UndefinedXformCodeError(XformCorrectionError):
    """A qform or sform code is not one of the defined NIfTI xform codes."""


class DegenerateAffineError(XformCorrectionError):
    """An affine cannot be reconstructed as a valid full-rank transform."""


class ConflictingXformsError(XformCorrectionError):
    """Two valid declarations claim the same space but disagree beyond tolerance."""


class QformNotExpressibleError(XformCorrectionError):
    """The target transform cannot be represented by a qform without altering it."""


class XformDeclarations(NamedTuple):
    """The transforms actually represented by the two header declarations.

    Fields are None when the matching xform code is 0 (declaration absent).
    """

    qform: np.ndarray | None
    qform_code: int
    sform: np.ndarray | None
    sform_code: int


class CorrectionResult(NamedTuple):
    """Corrected image copy and the declarations of that copy."""

    image: Nifti1Pair
    declarations: XformDeclarations


def _resolve_code(form: str, code) -> int:
    """Return the integer xform code for `code`, rejecting undefined values."""
    try:
        return cast('int', xform_codes.code[code])
    except KeyError:
        raise UndefinedXformCodeError(
            f'{form}_code {code!r} is not a defined NIfTI xform code '
            f'(allowed codes: {sorted(xform_codes.value_set())})'
        ) from None


def _require_full_rank(affine: np.ndarray, where: str) -> None:
    """Raise if the 3x3 linear part of `affine` is rank deficient."""
    if affine.shape != (4, 4):
        raise DegenerateAffineError(
            f'{where} affine must have shape (4, 4), got {tuple(affine.shape)}'
        )
    rzs = np.asarray(affine, dtype=np.float64)[:3, :3]
    if np.any(np.sum(rzs * rzs, axis=0) == 0):
        raise DegenerateAffineError(f'{where} affine has a zero-length voxel axis:\n{affine}')
    try:
        singular = npl.svd(rzs, compute_uv=False)
    except npl.LinAlgError as exc:
        raise DegenerateAffineError(f'Could not decompose {where} affine:\n{affine}') from exc
    # Same rank threshold as numpy's matrix_rank, applied to the SVD we already did.
    rank_tol = singular.max() * max(rzs.shape) * np.finfo(singular.dtype).eps
    if np.count_nonzero(singular > rank_tol) < 3:
        raise DegenerateAffineError(
            f'{where} affine has a singular linear part (singular values '
            f'{np.round(singular, 12)}):\n{affine}'
        )


def qform_capable(affine) -> bool:
    """Check whether `affine` can be expressed by a qform.

    The qform encodes an orthogonal column basis (rotation plus the optional
    ``qfac`` reflection on the last voxel axis), per-axis zooms and a
    translation.  Any shear, or any column that is not perpendicular to the
    others, makes the affine inexpressible.

    The test deliberately has no closeness tolerance for real geometry: a
    matrix that merely looks close to orthogonal still carries a shear or a
    flip that the qform cannot represent, and such a transform must stay in
    the sform rather than being silently orthogonalized.  Only floating point
    noise around an exactly orthogonal basis is accepted, using the same
    polar-decomposition criterion that ``header.set_qform(...,
    strip_shears=False)`` applies, so a True result guarantees the write
    succeeds without modifying the transform.

    Parameters
    ----------
    affine : (4, 4) array-like
        Candidate voxel-to-world affine.

    Returns
    -------
    capable : bool
        True if the qform fields can store `affine` unchanged.
    """
    affine = np.asarray(affine, dtype=np.float64)
    if affine.shape != (4, 4):
        raise DegenerateAffineError(f'affine must have shape (4, 4), got {tuple(affine.shape)}')
    rzs = affine[:3, :3]
    zooms = np.sqrt(np.sum(rzs * rzs, axis=0))
    if np.any(zooms == 0):
        return False
    basis = rzs / zooms
    # Mirror set_qform: a reflection is carried by qfac on the last voxel axis.
    if npl.det(basis) < 0:
        basis = basis.copy()
        basis[:, -1] *= -1
    try:
        left, _, right = npl.svd(basis)
    except npl.LinAlgError:
        return False
    closest_orthogonal = left @ right
    # Identical criterion to the strict (no shear stripping) qform write.
    return bool(np.allclose(closest_orthogonal, basis))


def get_xform_declarations(header) -> XformDeclarations:
    """Return the transforms the qform and sform fields actually encode.

    Codes outside the NIfTI xform code table raise
    :class:`UndefinedXformCodeError`.  A present code whose fields cannot be
    reconstructed into a full-rank affine (singular linear part, zero voxel
    axis, or corrupt qfac / pixdim / quaternion values) raises
    :class:`DegenerateAffineError`.

    Parameters
    ----------
    header : Nifti1Header or Nifti2Header
        Header to inspect.

    Returns
    -------
    declarations : XformDeclarations
        Reconstructed qform / sform affines and their integer codes.
    """
    raw_codes = {'qform': int(header['qform_code']), 'sform': int(header['sform_code'])}
    for form, raw_code in raw_codes.items():
        if raw_code not in xform_codes.value_set():
            raise UndefinedXformCodeError(
                f'{form}_code {raw_code} is not a defined NIfTI xform code '
                f'(allowed codes: {sorted(xform_codes.value_set())})'
            )
    # Reconstruct the stored transforms; corrupt fields (bad qfac / pixdim or a
    # non-unit quaternion) make a declaration invalid rather than merely absent.
    try:
        qform, qform_code = header.get_qform(coded=True)
        sform, sform_code = header.get_sform(coded=True)
    except (HeaderDataError, ValueError) as exc:
        raise DegenerateAffineError(
            f'Cannot reconstruct a valid qform/sform from the header fields: {exc}'
        ) from exc
    if qform is not None:
        _require_full_rank(np.asarray(qform, dtype=np.float64), 'qform')
    if sform is not None:
        _require_full_rank(np.asarray(sform, dtype=np.float64), 'sform')
    return XformDeclarations(qform, int(qform_code), sform, int(sform_code))


def check_xforms(declarations: XformDeclarations, atol: float = 1e-5, rtol: float = 1e-7) -> None:
    """Raise if two valid declarations claim one space but disagree.

    Declarations with different space codes are independent statements about
    different spaces, and a form with code 0 is absent; neither can conflict.
    A shared non-zero code with transforms that differ beyond tolerance means
    the header gives two contradictory positions for the same voxel in the
    same space.

    Parameters
    ----------
    declarations : XformDeclarations
        Declarations to validate, as returned by
        :func:`get_xform_declarations`.
    atol : float, optional
        Absolute tolerance on the transform difference.
    rtol : float, optional
        Relative tolerance on the transform difference.

    Raises
    ------
    ConflictingXformsError
        If both forms are valid, share a code, and their transforms disagree.
    """
    qform, qform_code, sform, sform_code = declarations
    if qform_code == 0 or sform_code == 0 or qform_code != sform_code:
        return
    # A non-zero code means get_xform_declarations reconstructed both affines.
    assert qform is not None and sform is not None
    if not np.allclose(qform, sform, atol=atol, rtol=rtol):
        raise ConflictingXformsError(
            f'qform and sform both declare space code {qform_code}, but their '
            f'transforms differ by more than atol={atol}, rtol={rtol}.\n'
            f'qform:\n{qform}\nsform:\n{sform}'
        )


def _prepare(img: Nifti1Pair, target_affine, target_code, form: str, atol, rtol):
    """Run all validation before touching a copy of the image."""
    code = _resolve_code(form, target_code)
    target_affine = np.asarray(target_affine, dtype=np.float64)
    if code != 0:
        _require_full_rank(target_affine, f'target {form}')
    declarations = get_xform_declarations(img.header)
    return code, target_affine, declarations


def _copy_image(img: Nifti1Pair) -> Nifti1Pair:
    """Return an independent deep copy of `img` (data, header and file map)."""
    return deepcopy(img)


def _expected_declarations(
    declarations: XformDeclarations, form: str, target_affine, code: int
) -> XformDeclarations:
    """Build the declarations a write of `form` will leave in the copy."""
    qform, qform_code, sform, sform_code = declarations
    if form == 'qform':
        if code == 0:
            qform, qform_code = None, 0
        else:
            qform, qform_code = target_affine, code
    else:
        if code == 0:
            sform, sform_code = None, 0
        else:
            sform, sform_code = target_affine, code
    return XformDeclarations(qform, qform_code, sform, sform_code)


def correct_qform(
    img: Nifti1Pair,
    target_affine,
    target_code,
    atol: float = 1e-5,
    rtol: float = 1e-7,
    update_affine: bool = True,
) -> CorrectionResult:
    """Return a copy of `img` with its qform corrected to a target transform.

    The sform declaration is never modified, so it keeps its original space
    and transform.  A target that the qform cannot represent without changing
    it (shears or any non-orthogonal column basis) raises
    :class:`QformNotExpressibleError`; the caller can keep that transform in
    the sform via :func:`correct_sform` instead.  Expressibility is not a
    closeness test, so nothing is orthogonalized silently.

    Parameters
    ----------
    img : Nifti1Pair or Nifti1Image (or NIfTI2 equivalent)
        Image whose declaration should be corrected.
    target_affine : (4, 4) array-like
        Authorized voxel-to-world transform the qform must represent.
    target_code : int or str
        Target space code.  ``0`` / ``'unknown'`` clears the qform.
    atol : float, optional
        Absolute tolerance for detecting same-space declaration conflicts.
    rtol : float, optional
        Relative tolerance for detecting same-space declaration conflicts.
    update_affine : bool, optional
        Forwarded to :meth:`Nifti1Image.set_qform`.

    Returns
    -------
    result : CorrectionResult
        New image copy and the declarations actually stored in that copy.
    """
    code, target_affine, declarations = _prepare(
        img, target_affine, target_code, 'qform', atol, rtol
    )
    if code != 0 and not qform_capable(target_affine):
        raise QformNotExpressibleError(
            'Target affine contains shears or non-orthogonal axes and cannot be '
            'expressed by a qform without changing the transform. Keep the '
            'transform in the sform or revise the target; refusing to '
            f'orthogonalize silently:\n{target_affine}'
        )
    check_xforms(_expected_declarations(declarations, 'qform', target_affine, code), atol, rtol)
    new_img = _copy_image(img)
    if code == 0:
        new_img.set_qform(None, code=0, update_affine=update_affine)
    else:
        try:
            new_img.set_qform(
                target_affine,
                code=code,
                strip_shears=False,
                update_affine=update_affine,
            )
        except HeaderDataError as exc:
            raise QformNotExpressibleError(
                f'qform write rejected the target affine:\n{target_affine}'
            ) from exc
    return CorrectionResult(new_img, get_xform_declarations(new_img.header))


def correct_sform(
    img: Nifti1Pair,
    target_affine,
    target_code,
    atol: float = 1e-5,
    rtol: float = 1e-7,
    update_affine: bool = True,
) -> CorrectionResult:
    """Return a copy of `img` with its sform corrected to a target transform.

    The sform stores a full 4x4 affine, so shears and reflections are carried
    verbatim.  The qform declaration is never modified and keeps its original
    space and transform.

    Parameters
    ----------
    img : Nifti1Pair or Nifti1Image (or NIfTI2 equivalent)
        Image whose declaration should be corrected.
    target_affine : (4, 4) array-like
        Authorized voxel-to-world transform the sform must represent.
    target_code : int or str
        Target space code.  ``0`` / ``'unknown'`` clears the sform.
    atol : float, optional
        Absolute tolerance for declaration agreement.
    rtol : float, optional
        Relative tolerance for declaration agreement.
    update_affine : bool, optional
        Forwarded to :meth:`Nifti1Image.set_sform`.

    Returns
    -------
    result : CorrectionResult
        New image copy and the declarations actually stored in that copy.
    """
    code, target_affine, declarations = _prepare(
        img, target_affine, target_code, 'sform', atol, rtol
    )
    check_xforms(_expected_declarations(declarations, 'sform', target_affine, code), atol, rtol)
    new_img = _copy_image(img)
    if code == 0:
        new_img.set_sform(None, code=0, update_affine=update_affine)
    else:
        new_img.set_sform(target_affine, code=code, update_affine=update_affine)
    return CorrectionResult(new_img, get_xform_declarations(new_img.header))


def grid_corners(shape) -> np.ndarray:
    """Return the eight grid corner voxel coordinates for a 3D `shape`.

    Examples
    --------
    >>> grid_corners((2, 3, 4))
    array([[0., 0., 0.],
           [0., 0., 3.],
           [0., 2., 0.],
           [0., 2., 3.],
           [1., 0., 0.],
           [1., 0., 3.],
           [1., 2., 0.],
           [1., 2., 3.]])
    """
    bounds = [(0, size - 1) for size in shape[:3]]
    return np.array(list(product(*bounds)), dtype=np.float64)


def world_positions(affine, voxels) -> np.ndarray:
    """Map voxel coordinates `voxels` through `affine` to world coordinates."""
    return apply_affine(affine, np.asarray(voxels, dtype=np.float64))


def positions_agree(
    affine_a,
    voxels_a,
    affine_b,
    voxels_b,
    atol: float = 1e-5,
    rtol: float = 1e-7,
) -> bool:
    """Check that paired voxel coordinates land on the same world positions.

    After cropping or reorienting, the same physical voxel often has different
    array indices in the old and new grids.  Pass matching coordinate sets
    (for example old grid corners and the same corners shifted by the crop
    offset) and compare where each affine places them in world space.

    Parameters
    ----------
    affine_a, affine_b : (4, 4) array-like
        Voxel-to-world affines of the two grids.
    voxels_a, voxels_b : (N, 3) array-like
        Coordinates of the same physical voxels in each grid.
    atol : float, optional
        Absolute tolerance in world units (typically mm).
    rtol : float, optional
        Relative tolerance in world units.

    Returns
    -------
    agree : bool
        True if all paired world positions agree within tolerance.
    """
    positions_a = world_positions(affine_a, voxels_a)
    positions_b = world_positions(affine_b, voxels_b)
    if positions_a.shape != positions_b.shape:
        raise ValueError(
            f'voxel coordinate sets must have matching shape; got '
            f'{positions_a.shape} and {positions_b.shape}'
        )
    return bool(np.allclose(positions_a, positions_b, atol=atol, rtol=rtol))
