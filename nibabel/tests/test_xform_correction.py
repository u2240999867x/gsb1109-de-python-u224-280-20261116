# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the NiBabel package for the
#   copyright and license terms.
#
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Tests for authorized NIfTI qform / sform correction."""

import numpy as np
import pytest
from numpy.testing import assert_allclose, assert_array_equal

from nibabel.affines import from_matvec
from nibabel.eulerangles import euler2mat
from nibabel.nifti1 import Nifti1Image
from nibabel.nifti2 import Nifti2Image
from nibabel.orientations import inv_ornt_aff
from nibabel.spatialimages import HeaderDataError
from nibabel.xform_correction import (
    ConflictingXformsError,
    CorrectionResult,
    DegenerateAffineError,
    QformNotExpressibleError,
    UndefinedXformCodeError,
    XformCorrectionError,
    XformDeclarations,
    check_xforms,
    correct_qform,
    correct_sform,
    get_xform_declarations,
    grid_corners,
    positions_agree,
    qform_capable,
    world_positions,
)

from .test_orientations import ALL_ORNTS

IMAGE_CLASSES = [Nifti1Image, Nifti2Image]
# NIfTI1 stores q/s fields as float32, so round trips only promise this.
STORE_ATOL = 1e-5
SHAPE = (4, 5, 6)


def _qform():
    # Rotation + reflection (qfac) on the first voxel axis, with translation.
    aff = np.eye(4)
    aff[:3, :3] = euler2mat(0.2, 0.3, 0.4) @ np.diag([-2.0, 2.0, 2.0])
    aff[:3, 3] = [10.0, -20.0, -30.0]
    return aff


def _axis_aligned_qform():
    # Axis-aligned form survives the float32 quaternion round trip exactly,
    # matching the precision nibabel itself assumes in its reorient tests.
    aff = np.diag([-2.0, 2.0, 2.0, 1.0])
    aff[:3, 3] = [10.0, -20.0, -30.0]
    return aff


def _sform():
    # Axis permutation with a reflection, distinct from the qform, code 4 (MNI).
    return np.array(
        [
            [0.0, 0.0, -2.0, 5.0],
            [0.0, 2.0, 0.0, -7.0],
            [2.0, 0.0, 0.0, 3.0],
            [0.0, 0.0, 0.0, 1.0],
        ]
    )


def _sheared_affine():
    return from_matvec(np.arange(9).reshape(3, 3) + np.eye(3), [9.0, 10.0, 11.0])


def _make_image(image_class, qform=None, qcode=1, sform=None, scode=4):
    affine = sform if sform is not None else np.eye(4)
    img = image_class(np.zeros(SHAPE, dtype='f4'), affine)
    if sform is None:
        img.header.set_sform(None, code=0)
    else:
        img.header.set_sform(sform, code=scode)
    if qform is None:
        img.header.set_qform(None, code=0)
    else:
        img.header.set_qform(qform, code=qcode)
    return img


# --------------------------------------------------------------------------- #
# qform_capable
# --------------------------------------------------------------------------- #


def test_qform_capable_accepts_rotation_zooms_and_reflection():
    rotation = euler2mat(0.1, 0.2, 0.3)
    assert qform_capable(from_matvec(rotation @ np.diag([2.0, 3.0, 4.0]), [1, 2, 3]))
    # Reflections are expressible through qfac, including on any single axis.
    for axis in range(3):
        signs = np.ones(3)
        signs[axis] = -1
        assert qform_capable(np.diag(list(signs * 2.0) + [1.0]))
    # A rotated axis permutation with det < 0 still has an orthogonal basis.
    flipped = rotation @ np.diag([1.0, 1.0, -1.0])
    assert qform_capable(from_matvec(flipped * 3.0, [0, 0, 0]))


def test_qform_capable_rejects_shear_and_rank_deficiency():
    assert not qform_capable(_sheared_affine())
    # A non-orthogonal basis that is merely "close" to orthogonal still fails:
    # closeness must not silently drop the shear duty.
    basis = euler2mat(0.1, 0.2, 0.3)
    basis[0, 1] += 1e-3
    aff = from_matvec(basis * 2.0, [0, 0, 0])
    assert not qform_capable(aff)
    # Floating-point noise around a truly orthogonal basis is accepted, and
    # such a basis round-trips through the strict (no shear stripping) write.
    noisy = euler2mat(0.1, 0.2, 0.3)
    noisy[0, 1] += 1e-12
    noisy_aff = from_matvec(noisy * 2.0, [0, 0, 0])
    assert qform_capable(noisy_aff)
    header = Nifti1Image(np.zeros((2, 2, 2)), np.eye(4)).header
    header.set_qform(noisy_aff, code=1, strip_shears=False)
    assert_allclose(header.get_qform(), noisy_aff, atol=STORE_ATOL)
    # Degenerate geometry.
    assert not qform_capable(np.diag([2.0, 0.0, 3.0, 1.0]))
    assert not qform_capable(np.ones((4, 4)))
    with pytest.raises(DegenerateAffineError):
        qform_capable(np.eye(3))


# --------------------------------------------------------------------------- #
# Declarations: the transforms the fields actually represent
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize('image_class', IMAGE_CLASSES)
def test_get_declarations_reports_actual_transforms_and_codes(image_class):
    qform, sform = _qform(), _sform()
    img = _make_image(image_class, qform=qform, sform=sform)
    declarations = get_xform_declarations(img.header)
    assert isinstance(declarations, XformDeclarations)
    assert declarations.qform_code == 1
    assert declarations.sform_code == 4
    assert_allclose(declarations.qform, qform, atol=STORE_ATOL)
    assert_allclose(declarations.sform, sform, atol=STORE_ATOL)
    # The two forms really do represent different transforms in different spaces.
    assert not np.allclose(declarations.qform, declarations.sform)


@pytest.mark.parametrize('image_class', IMAGE_CLASSES)
def test_get_declarations_marks_absent_forms_as_none(image_class):
    img = _make_image(image_class)
    declarations = get_xform_declarations(img.header)
    assert declarations == XformDeclarations(None, 0, None, 0)


def test_undefined_code_in_header_raises():
    img = _make_image(Nifti1Image, qform=_qform(), sform=_sform())
    img.header['qform_code'] = -1
    with pytest.raises(UndefinedXformCodeError):
        get_xform_declarations(img.header)
    img.header.set_qform(_qform(), code=1)
    img.header['sform_code'] = 7
    with pytest.raises(UndefinedXformCodeError):
        get_xform_declarations(img.header)


def test_undefined_target_code_raises():
    img = _make_image(Nifti1Image, qform=_qform(), sform=_sform())
    with pytest.raises(UndefinedXformCodeError):
        correct_qform(img, _qform(), 9)
    with pytest.raises(UndefinedXformCodeError):
        correct_sform(img, _sform(), 'not_a_space')


def test_degenerate_target_raises():
    img = _make_image(Nifti1Image, qform=_qform(), sform=_sform())
    singular = np.diag([2.0, 0.0, 3.0, 1.0])
    with pytest.raises(DegenerateAffineError):
        correct_qform(img, singular, 1)
    with pytest.raises(DegenerateAffineError):
        correct_sform(img, singular, 1)
    with pytest.raises(DegenerateAffineError):
        correct_qform(img, np.ones((4, 4)), 1)
    with pytest.raises(DegenerateAffineError):
        correct_qform(img, np.eye(3), 1)


def test_degenerate_declaration_in_header_raises():
    img = _make_image(Nifti1Image, qform=_qform(), sform=_sform())
    img.header['srow_x'] = 0
    with pytest.raises(DegenerateAffineError):
        get_xform_declarations(img.header)
    img.header.set_sform(_sform(), code=4)
    # A zero zoom in the qform fields is degenerate as well.
    img.header['pixdim'][1] = 0
    with pytest.raises(DegenerateAffineError):
        get_xform_declarations(img.header)
    img.header['pixdim'][1] = 2.0


def test_corrupt_qform_fields_are_reported_as_degenerate():
    img = _make_image(Nifti1Image, qform=_axis_aligned_qform(), sform=_sform())
    # qfac may only be 1 or -1.
    img.header['pixdim'][0] = 0
    with pytest.raises(DegenerateAffineError):
        get_xform_declarations(img.header)
    img.header['pixdim'][0] = 1
    # pixdims must be positive; the reflection lives in qfac.
    img.header['pixdim'][1] = -2.0
    with pytest.raises(DegenerateAffineError):
        get_xform_declarations(img.header)
    img.header['pixdim'][1] = 2.0
    # A quaternion whose vector part has unit norm above 1 is not reconstructable.
    img.header['quatern_b'] = 0.9
    img.header['quatern_c'] = 0.9
    img.header['quatern_d'] = 0.9
    with pytest.raises(DegenerateAffineError):
        get_xform_declarations(img.header)
    # A refused correction against a corrupt header also surfaces the typed error.
    with pytest.raises(DegenerateAffineError):
        correct_qform(img, np.eye(4), 1)


def test_error_hierarchy():
    for exc in (
        UndefinedXformCodeError('x'),
        DegenerateAffineError('x'),
        ConflictingXformsError('x'),
        QformNotExpressibleError('x'),
    ):
        assert isinstance(exc, XformCorrectionError)
        assert isinstance(exc, HeaderDataError)


# --------------------------------------------------------------------------- #
# Same-space conflicts
# --------------------------------------------------------------------------- #


def test_check_xforms_conflict_and_resolution():
    qform, sform = _qform(), _sform()
    # Different codes: independent declarations, no conflict even if far apart.
    check_xforms(XformDeclarations(qform, 1, sform, 4))
    # Same code, different transforms: conflict.
    with pytest.raises(ConflictingXformsError):
        check_xforms(XformDeclarations(qform, 4, sform, 4))
    # Same code, matching transforms: fine.
    check_xforms(XformDeclarations(qform, 1, qform.copy(), 1), atol=STORE_ATOL)
    # An absent form cannot conflict.
    check_xforms(XformDeclarations(None, 0, sform, 4))


def test_correction_refuses_to_create_same_space_conflict():
    img = _make_image(Nifti1Image, qform=_qform(), sform=_sform())
    # Writing the qform under the sform's code 4 with a different transform
    # would leave two contradictory code-4 declarations.
    with pytest.raises(ConflictingXformsError):
        correct_qform(img, _qform(), 4)
    with pytest.raises(ConflictingXformsError):
        correct_sform(img, _sform(), 1)
    # The input image is untouched after a refused correction.
    declarations = get_xform_declarations(img.header)
    assert declarations.qform_code == 1
    assert declarations.sform_code == 4


def test_correction_can_resolve_an_existing_conflict():
    img = _make_image(Nifti1Image, qform=_qform(), sform=_sform())
    # Manufacture a same-code conflict, then authorize the qform to match.
    img.header.set_qform(_qform(), code=4)
    with pytest.raises(ConflictingXformsError):
        check_xforms(get_xform_declarations(img.header))
    result = correct_qform(img, _sform(), 4)
    declarations = result.declarations
    check_xforms(declarations)
    assert_allclose(declarations.qform, declarations.sform, atol=STORE_ATOL)


def test_conflict_tolerance_distinguishes_close_from_disagreeing():
    qform = _qform()
    close = qform.copy()
    close[:3, 3] += 1e-6
    # rtol=0 isolates the meaning of atol from the translation magnitude.
    check_xforms(XformDeclarations(qform, 1, close, 1), atol=1e-5, rtol=0)
    with pytest.raises(ConflictingXformsError):
        check_xforms(XformDeclarations(qform, 1, close, 1), atol=1e-9, rtol=0)


# --------------------------------------------------------------------------- #
# qform correction: exact representation, no silent orthogonalization
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize('image_class', IMAGE_CLASSES)
def test_correct_qform_writes_exact_transform(image_class):
    img = _make_image(image_class, qform=_qform(), sform=_sform())
    target = np.eye(4)
    target[:3, :3] = euler2mat(0.5, -0.2, 0.1) @ np.diag([1.5, 2.5, 3.5])
    target[:3, 3] = [4.0, 5.0, 6.0]
    result = correct_qform(img, target, 'scanner')
    assert isinstance(result, CorrectionResult)
    assert result.image is not img
    assert result.declarations.qform_code == 1
    assert_allclose(result.declarations.qform, target, atol=STORE_ATOL)


def test_correct_qform_refuses_shear_without_orthogonalizing():
    img = _make_image(Nifti1Image, qform=_qform(), sform=_sform())
    original_qform = img.header.get_qform()
    sheared = _sheared_affine()
    with pytest.raises(QformNotExpressibleError):
        correct_qform(img, sheared, 1)
    # The refusal must not have written the orthogonalized approximation.
    assert_allclose(img.header.get_qform(), original_qform, atol=0)
    assert int(img.header['qform_code']) == 1


def test_correct_qform_never_accepts_small_shear_via_loose_tolerance():
    # A tiny but genuine shear is real geometry, not noise: widening the
    # conflict tolerance must not make the qform silently drop it.
    basis = euler2mat(0.1, 0.2, 0.3)
    basis[0, 1] += 1e-4
    sheared = from_matvec(basis @ np.diag([2.0, 3.0, 4.0]), [1.0, 1.0, 1.0])
    img = _make_image(Nifti1Image, qform=_axis_aligned_qform(), sform=_sform())
    with pytest.raises(QformNotExpressibleError):
        correct_qform(img, sheared, 1, atol=1e-2)
    # The same transform can be authorized and kept verbatim in the sform.
    result = correct_sform(img, sheared, 2)
    assert_allclose(result.declarations.sform, sheared, atol=STORE_ATOL)


def test_correct_qform_preserves_reflection_duty_via_qfac():
    img = _make_image(Nifti1Image, qform=_qform(), sform=_sform())
    flipped = np.diag([-2.0, 3.0, 4.0, 1.0])
    flipped[:3, 3] = [7.0, 8.0, 9.0]
    result = correct_qform(img, flipped, 1)
    assert result.image.header['pixdim'][0] == -1
    assert_allclose(result.declarations.qform, flipped, atol=STORE_ATOL)
    # The reflected axis direction survives in the reconstructed transform.
    assert np.sign(result.declarations.qform[0, 0]) == -1


def test_correct_qform_clear_with_code_zero():
    img = _make_image(Nifti1Image, qform=_qform(), sform=_sform())
    result = correct_qform(img, np.eye(4), 0)
    assert result.declarations.qform is None
    assert result.declarations.qform_code == 0
    # The other, unauthorized, declaration keeps its meaning.
    assert result.declarations.sform_code == 4
    assert_allclose(result.declarations.sform, _sform(), atol=STORE_ATOL)


# --------------------------------------------------------------------------- #
# sform correction: any affine, verbatim
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize('image_class', IMAGE_CLASSES)
def test_correct_sform_keeps_shear_and_flip_verbatim(image_class):
    img = _make_image(image_class, qform=_qform(), sform=_sform())
    sheared = _sheared_affine()
    result = correct_sform(img, sheared, 'mni')
    assert result.declarations.sform_code == 4
    assert_allclose(result.declarations.sform, sheared, atol=STORE_ATOL)
    flipped = np.diag([2.0, -3.0, 4.0, 1.0])
    result = correct_sform(result.image, flipped, 3)
    assert_allclose(result.declarations.sform, flipped, atol=STORE_ATOL)


def test_correct_sform_clear_with_code_zero():
    img = _make_image(Nifti1Image, qform=_qform(), sform=_sform())
    result = correct_sform(img, np.eye(4), 0)
    assert result.declarations.sform is None
    assert result.declarations.sform_code == 0
    assert result.declarations.qform_code == 1
    assert_allclose(result.declarations.qform, _qform(), atol=STORE_ATOL)


def test_inexpressible_qform_target_can_live_in_sform():
    # Policy: the qform refuses the shear, but the sform can carry it, so the
    # transform is retained there rather than being silently orthogonalized.
    img = _make_image(Nifti1Image, qform=_qform(), sform=_sform())
    sheared = _sheared_affine()
    with pytest.raises(QformNotExpressibleError):
        correct_qform(img, sheared, 1)
    result = correct_sform(img, sheared, 2)
    assert_allclose(result.declarations.sform, sheared, atol=STORE_ATOL)
    assert_allclose(result.declarations.qform, _qform(), atol=STORE_ATOL)


# --------------------------------------------------------------------------- #
# Independence: new copy, other declaration keeps its original meaning
# --------------------------------------------------------------------------- #


def test_correction_returns_independent_copy():
    img = _make_image(Nifti1Image, qform=_qform(), sform=_sform())
    result = correct_qform(img, np.diag([3.0, 3.0, 3.0, 1.0]), 1)
    result.image.header['qoffset_x'] = 999
    assert img.header['qoffset_x'] != 999
    assert result.image.dataobj is not img.dataobj
    assert result.image.header is not img.header


def test_correct_qform_leaves_sform_meaning_untouched():
    img = _make_image(Nifti1Image, qform=_qform(), sform=_sform())
    result = correct_qform(img, np.diag([3.0, 3.0, 3.0, 1.0]), 1)
    assert result.declarations.sform_code == 4
    assert_allclose(result.declarations.sform, _sform(), atol=0)


def test_correct_sform_leaves_qform_meaning_untouched():
    img = _make_image(Nifti1Image, qform=_qform(), sform=_sform())
    result = correct_sform(img, _sheared_affine(), 4)
    assert result.declarations.qform_code == 1
    assert_allclose(result.declarations.qform, _qform(), atol=STORE_ATOL)


def test_update_affine_flag_leaves_image_affine_alone():
    img = _make_image(Nifti1Image, qform=_qform(), sform=_sform())
    before = img.affine.copy()
    target = np.diag([5.0, 5.0, 5.0, 1.0])
    result = correct_qform(img, target, 1, update_affine=False)
    assert_array_equal(result.image.affine, before)
    assert_allclose(result.image.header.get_qform(), target, atol=STORE_ATOL)


# --------------------------------------------------------------------------- #
# Crop and reorientation: original voxel positions must still check out
# --------------------------------------------------------------------------- #


def test_crop_correction_keeps_voxel_world_positions_in_both_spaces():
    qform, sform = _qform(), _sform()
    img = _make_image(Nifti1Image, qform=qform, sform=sform)
    cropped = img.slicer[1:4, 2:5, 3:6]
    crop_transform = np.eye(4)
    crop_transform[:3, 3] = [1, 2, 3]

    cropped = correct_qform(cropped, qform @ crop_transform, 1).image
    cropped = correct_sform(cropped, sform @ crop_transform, 4).image
    declarations = get_xform_declarations(cropped.header)

    old_corners = grid_corners(SHAPE)
    selected = old_corners[
        (old_corners[:, 0] == 1)
        | (old_corners[:, 0] == 3)
        | (old_corners[:, 1] == 2)
        | (old_corners[:, 1] == 4)
        | (old_corners[:, 2] == 3)
        | (old_corners[:, 2] == 5)
    ]
    new_indices = selected - np.array([1.0, 2.0, 3.0])
    assert positions_agree(qform, selected, declarations.qform, new_indices)
    assert positions_agree(sform, selected, declarations.sform, new_indices)
    assert (declarations.qform_code, declarations.sform_code) == (1, 4)


@pytest.mark.parametrize('ornt', ALL_ORNTS)
def test_reorient_keeps_voxel_positions_in_each_space(ornt):
    # Axis-aligned forms round-trip through the float32 header exactly; the
    # rotated-qform storage precision is exercised in the declaration tests.
    qform, sform = _axis_aligned_qform(), _sform()
    img = _make_image(Nifti1Image, qform=qform, sform=sform)
    reoriented = img.as_reoriented(ornt)
    declarations = get_xform_declarations(reoriented.header)
    voxel_map = inv_ornt_aff(ornt, img.shape)
    expected_q = qform @ voxel_map
    expected_s = sform @ voxel_map
    assert_allclose(declarations.qform, expected_q, atol=STORE_ATOL)
    assert_allclose(declarations.sform, expected_s, atol=STORE_ATOL)
    # Every corner voxel keeps its physical coordinates in each space.
    new_corners = grid_corners(reoriented.shape)
    old_corners = world_positions(voxel_map, new_corners)
    assert positions_agree(qform, old_corners, declarations.qform, new_corners)
    assert positions_agree(sform, old_corners, declarations.sform, new_corners)
    assert (declarations.qform_code, declarations.sform_code) == (1, 4)


def test_crop_then_reorient_roundtrip_preserves_voxel_values():
    qform, sform = _qform(), _sform()
    data = np.arange(np.prod(SHAPE), dtype='f4').reshape(SHAPE)
    img = Nifti1Image(data, sform)
    img.header.set_sform(sform, code=4)
    img.header.set_qform(qform, code=1)

    cropped = img.slicer[1:4, 2:5, 3:6]
    crop_transform = np.eye(4)
    crop_transform[:3, 3] = [1, 2, 3]
    cropped = correct_qform(cropped, qform @ crop_transform, 1).image
    cropped = correct_sform(cropped, sform @ crop_transform, 4).image

    ornt = [[1, -1], [0, 1], [2, -1]]
    reoriented = cropped.as_reoriented(ornt)
    voxel_map = inv_ornt_aff(ornt, cropped.shape)
    old_grid = np.array([2, 3, 4], dtype=float)
    intermediate = old_grid - np.array([1.0, 2.0, 3.0])
    new_grid = np.rint(world_positions(voxel_map, intermediate)).astype(int)
    assert reoriented.get_fdata()[tuple(new_grid)] == data[tuple(old_grid.astype(int))]


# --------------------------------------------------------------------------- #
# Helper functions
# --------------------------------------------------------------------------- #


def test_grid_corners_values():
    corners = grid_corners((2, 3, 4))
    assert corners.shape == (8, 3)
    assert_array_equal(corners[0], [0, 0, 0])
    assert_array_equal(corners[-1], [1, 2, 3])
    assert_array_equal(np.unique(corners[:, 0]), [0, 1])


def test_world_positions_and_agreement():
    affine = np.diag([2.0, 3.0, 4.0, 1.0])
    affine[:3, 3] = [1, 1, 1]
    voxels = np.array([[0, 0, 0], [1, 1, 1]], dtype=float)
    assert_allclose(world_positions(affine, voxels), [[1, 1, 1], [3, 4, 5]])
    assert positions_agree(affine, voxels, affine, voxels)
    translated = affine.copy()
    translated[:3, 3] += 1e-6
    assert positions_agree(affine, voxels, translated, voxels, atol=1e-5)
    assert not positions_agree(affine, voxels, translated, voxels, atol=1e-9)
    with pytest.raises(ValueError):
        positions_agree(affine, voxels, affine, voxels[:1])
