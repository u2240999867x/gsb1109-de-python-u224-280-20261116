"""
Tests for gradient export and reorientation helpers.
"""
import json

import numpy as np
import pytest
import nibabel as nb
from nibabel.orientations import (axcodes2ornt, inv_ornt_aff, io_orientation,
                                  ornt_transform)

from dcmstack import dcmmeta, dcmstack, gradients, nifti_wrapper


def _affine():
    return np.array([[2.0, 0.0, 0.0, 10.0],
                     [0.0, 3.0, 0.0, 20.0],
                     [0.0, 0.0, 4.0, 30.0],
                     [0.0, 0.0, 0.0, 1.0]])


def _oriented():
    aff = _affine()
    shape = (2, 3, 4)
    ornt = ornt_transform(io_orientation(aff), axcodes2ornt('LAS'))
    new_aff = aff @ inv_ornt_aff(ornt, shape)
    return aff, new_aff, ornt, shape


def test_basis_aliases():
    assert gradients.normalize_basis('Scanner') == 'scanner'
    assert gradients.normalize_basis('RAS') == 'world'
    assert gradients.normalize_basis('iJK') == 'voxel'
    with pytest.raises(ValueError):
        gradients.normalize_basis('unknown')


def test_scanner_world_swap():
    # LPS scanner and RAS world differ by a flip of X and Y.
    mat = gradients.basis_change_matrix('scanner', 'world')
    assert np.allclose(mat, np.diag([-1.0, -1.0, 1.0]))
    assert np.allclose(
        gradients.transform_gradient([1.0, 2.0, 3.0], 'scanner', 'world'),
        [-1.0, -2.0, 3.0],
    )
    # The same physical direction expressed in the two conventions.
    assert np.allclose(
        gradients.transform_gradient([1.0, 2.0, 3.0], 'world', 'scanner'),
        [-1.0, -2.0, 3.0],
    )


def test_zero_and_incomplete_gradients():
    vector, status = gradients.coerce_gradient([0.0, 0.0, 0.0])
    assert status == 'zero'
    assert np.allclose(vector, 0.0)
    for value in ([1.0, 2.0], None, [], [1.0, 2.0, np.nan]):
        vector, status = gradients.coerce_gradient(value)
        assert vector is None
        assert status in ('missing', 'incomplete')
    # A zero gradient must remain zero, not become a unit direction.
    assert np.allclose(
        gradients.transform_gradient([0.0, 0.0, 0.0], transform=np.diag(
            [-1.0, 1.0, 1.0]
        )),
        [0.0, 0.0, 0.0],
    )


def test_degenerate_transform():
    with pytest.raises(gradients.DegenerateTransformError):
        gradients.transform_gradient(
            [1.0, 0.0, 0.0], transform=np.zeros((3, 3))
        )
    result = gradients.transform_gradient_with_status(
        [1.0, 0.0, 0.0], transform=np.zeros((3, 3)), on_degenerate='status'
    )
    assert result.status == 'degenerate'
    assert result.gradient is None


def test_voxel_basis_reorientation_uses_voxel_axes():
    aff, new_aff, ornt, shape = _oriented()
    # A scanner direction is a physical direction; only the expression in
    # voxel components changes when the voxel axes change.
    scanner_vec = [1.0, 0.0, 0.0]
    old = gradients.transform_gradient(
        scanner_vec, 'scanner', 'voxel', target_affine=aff
    )
    new = gradients.transform_gradient(
        scanner_vec, 'scanner', 'voxel', target_affine=new_aff
    )
    assert not np.allclose(old, new)
    assert np.allclose(np.linalg.norm(old), np.linalg.norm(new))
    # Explicit voxel-basis values follow the index permutation/flip.
    voxel_vec = [1.0, 0.0, 0.0]
    moved = gradients.reorient_gradients(
        [voxel_vec], source_affine=aff, ornt=ornt, shape=shape
    )
    assert np.allclose(moved, [[-1.0, 0.0, 0.0]])


def test_reorientation_inverse_roundtrip():
    aff, new_aff, ornt, shape = _oriented()
    vecs = np.array([[1.0, 2.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    for source_basis in ('scanner', 'world', 'voxel'):
        forward = gradients.reorient_gradients(
            vecs, source_affine=aff, target_affine=new_aff,
            source_basis=source_basis, target_basis='voxel',
            ornt=ornt, shape=shape,
        )
        back = gradients.reorient_gradients(
            forward, source_affine=aff, target_affine=new_aff,
            source_basis=source_basis, target_basis='voxel',
            ornt=ornt, shape=shape, inverse=True,
        )
        assert np.allclose(back, vecs)


def _make_ext(shape, affine=None, slice_dim=2):
    if affine is None:
        affine = np.eye(4)
    return dcmmeta.DcmMetaExtension.make_empty(
        shape, affine, np.eye(4), slice_dim
    )


def test_sample_meta_value_classifications():
    ext = _make_ext((2, 2, 3, 5, 7))
    for classes in ext.get_valid_classes():
        key = '%s_%s' % classes
        mult = ext.get_multiplicity(classes)
        ext.get_class_dict(classes)[key] = (
            0 if mult == 1 else list(range(mult))
        )
    ext.check_valid()

    assert gradients.sample_meta_value(ext, 'global_const', 0, 0, 0) == 0
    assert gradients.sample_meta_value(ext, 'global_slices', 1, 2, 0) == 33
    assert gradients.sample_meta_value(ext, 'time_samples', 1, 0, 0) == 1
    assert gradients.sample_meta_value(ext, 'time_samples', 1, 2, 0) == 11
    assert gradients.sample_meta_value(ext, 'time_slices', 1, 2, 2) == 2
    assert gradients.sample_meta_value(ext, 'vector_samples', 0, 3, 0) == 3
    assert gradients.sample_meta_value(ext, 'vector_slices', 1, 2, 2) == 5
    missing = gradients.sample_meta_value(ext, 'not_there')
    assert missing is gradients._MISSING


def test_table_uses_per_time_values_and_instances():
    ext = _make_ext((2, 2, 3, 2), slice_dim=2)
    ext.get_class_dict(('time', 'samples'))[
        'DiffusionGradientOrientation'
    ] = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    ext.get_class_dict(('time', 'samples'))['DiffusionBValue'] = [0.0, 1000.0]
    ext.get_class_dict(('global', 'slices'))['SOPInstanceUID'] = [
        'u0', 'u1', 'u2', 'u3', 'u4', 'u5'
    ]
    ext.check_valid()

    table = gradients.gradient_table_from_meta(ext)
    assert len(table) == 2
    assert table.b_values[0] == 0.0
    assert table.b_values[1] == 1000.0
    assert table.instances[0] is None
    assert table.samples[0].source_instances == ['u0', 'u1', 'u2']
    assert table.samples[1].source_instances == ['u3', 'u4', 'u5']
    assert table.samples[0].source_indices == [0, 1, 2]
    assert table.samples[1].source_indices == [3, 4, 5]
    assert table.statuses == ['ok', 'ok']


def test_table_per_slice_values():
    ext = _make_ext((2, 2, 3, 2), slice_dim=2)
    ext.get_class_dict(('global', 'slices'))[
        'DiffusionGradientOrientation'
    ] = [[0.0, 0.0, 1.0], [1.0, 0.0, 0.0],
         [0.0, 1.0, 0.0], [0.0, 0.0, 1.0],
         [0.0, 0.0, 1.0], [0.0, 0.0, 1.0]]
    ext.get_class_dict(('global', 'slices'))['DiffusionBValue'] = [
        0.0, 1000.0, 1000.0,
        0.0, 0.0, 0.0,
    ]
    ext.check_valid()

    table = gradients.gradient_table_from_meta(ext)
    # The first volume varies per slice and must be represented per slice.
    # The second volume is constant and is represented once.
    assert len(table) == 4
    assert [s.slice_index for s in table.samples] == [0, 1, 2, None]
    assert [s.slice_indices for s in table.samples] == [[0], [1], [2], [0, 1, 2]]
    assert table.b_values.tolist() == [0.0, 1000.0, 1000.0, 0.0]


def test_table_missing_and_zero_statuses():
    ext = _make_ext((2, 2, 1), slice_dim=2)
    ext.get_class_dict(('global', 'const'))['DiffusionBValue'] = 1000.0
    ext.check_valid()
    table = gradients.gradient_table_from_meta(ext)
    assert table.statuses == ['missing']
    assert np.all(np.isnan(table.gradients))

    ext = _make_ext((2, 2, 1), slice_dim=2)
    ext.get_class_dict(('global', 'const'))['DiffusionBValue'] = 0.0
    ext.check_valid()
    # A missing direction is not invented, even for b=0.
    table = gradients.gradient_table_from_meta(ext)
    assert table.statuses == ['missing']
    assert np.all(np.isnan(table.gradients))
    # Callers may explicitly opt into representing b=0 as a zero direction.
    table = gradients.gradient_table_from_meta(
        ext, zero_b_value_gradient=True
    )
    assert table.statuses == ['zero']
    assert np.allclose(table.gradients, 0.0)

    ext = _make_ext((2, 2, 1), slice_dim=2)
    ext.get_class_dict(('global', 'const'))[
        'DiffusionGradientOrientation'
    ] = [0.0, 0.0, 0.0]
    ext.get_class_dict(('global', 'const'))['DiffusionBValue'] = 0.0
    ext.check_valid()
    sample = gradients.gradient_table_from_meta(ext)[0]
    assert sample.status == 'zero'
    assert np.allclose(sample.gradient, 0.0)


def test_table_transform_and_inverse_preserves_metadata():
    aff = _affine()
    ext = _make_ext((2, 3, 4, 2), affine=aff, slice_dim=2)
    ext.get_class_dict(('time', 'samples'))[
        'DiffusionGradientOrientation'
    ] = [[1.0, 2.0, 0.0], [0.0, 1.0, 3.0]]
    ext.get_class_dict(('time', 'samples'))['DiffusionBValue'] = [0.0, 1000.0]
    ext.get_class_dict(('time', 'samples'))['SOPInstanceUID'] = ['a', 'b']
    ext.check_valid()

    table = gradients.gradient_table_from_meta(
        ext, source_basis='scanner', target_basis='voxel'
    )
    original = table.gradients.copy()
    for target in ('scanner', 'world', 'voxel'):
        moved = table.transform(target)
        back = moved.inverse('scanner')
        assert np.allclose(back.gradients, np.array(
            [[1.0, 2.0, 0.0], [0.0, 1.0, 3.0]]
        ))
        assert back.b_values.tolist() == [0.0, 1000.0]
        assert back.instances == ['a', 'b']


def test_stack_export_and_reorient_consistency():
    stack = dcmstack.DicomStack(time_order='EchoTime')
    vecs = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    for idx, vec in enumerate(vecs):
        dcm = dcmstack_test_dicom({
            'EchoTime': 20.0 + idx,
            'DiffusionGradientOrientation': vec,
            'DiffusionBValue': 1000.0 * idx,
            'SOPInstanceUID': '1.2.3.%d' % idx,
        })
        stack.add_dcm(dcm)
    table = stack.get_gradients(voxel_order='LAS')
    assert len(table) == 3
    assert table.b_values.tolist() == [0.0, 1000.0, 2000.0]
    assert table.instances == ['1.2.3.0', '1.2.3.1', '1.2.3.2']
    # A scanner direction is unchanged physically; its voxel components
    # match the target basis change.
    expected = np.array([
        gradients.transform_gradient(
            vec, 'scanner', 'voxel', target_affine=table.affine
        )
        for vec in vecs
    ])
    assert np.allclose(table.gradients, expected)


def test_wrapper_roundtrip_reorients_once():
    aff, new_aff, ornt, shape = _oriented()
    shape = (2, 3, 4, 2)
    data = np.zeros(shape)
    ext = _make_ext(shape, affine=new_aff, slice_dim=2)
    ext.reorient_transform = inv_ornt_aff(ornt, shape[:3])
    ext.get_class_dict(('time', 'samples'))[
        'DiffusionGradientOrientation'
    ] = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    ext.get_class_dict(('time', 'samples'))['DiffusionBValue'] = [1000.0, 2000.0]
    ext.check_valid()
    nii = nb.Nifti1Image(data, new_aff)
    nii.header.set_dim_info(slice=2)
    wrp = nifti_wrapper.NiftiWrapper(nii, make_empty=True)
    wrp.replace_extension(ext)

    table = wrp.get_gradients(source_basis='scanner', target_basis='voxel')
    expected = np.array([
        gradients.transform_gradient(
            vec, 'scanner', 'voxel', target_affine=new_aff
        )
        for vec in ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0])
    ])
    assert np.allclose(table.gradients, expected)


def test_bvals_bvecs_files(tmp_path):
    ext = _make_ext((2, 2, 1, 2), slice_dim=2)
    ext.get_class_dict(('time', 'samples'))[
        'DiffusionGradientOrientation'
    ] = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    ext.get_class_dict(('time', 'samples'))['DiffusionBValue'] = [0.0, 1000.0]
    ext.check_valid()
    table = gradients.gradient_table_from_meta(
        ext, source_basis='scanner', target_basis='world'
    )
    bval = tmp_path / 'test.bval'
    bvec = tmp_path / 'test.bvec'
    json_path = tmp_path / 'test.gradients.json'
    gradients.write_gradient_table(
        table, json_path=str(json_path),
        bval_path=str(bval), bvec_path=str(bvec),
    )
    assert np.allclose(np.loadtxt(str(bval)), [0.0, 1000.0])
    bvecs = np.loadtxt(str(bvec))
    assert bvecs.shape == (3, 2)
    assert np.allclose(bvecs[:, 1], [0.0, -1.0, 0.0])
    payload = json.loads(json_path.read_text())
    assert payload['source_basis'] == 'scanner'
    assert payload['target_basis'] == 'world'
    assert payload['samples'][1]['b_value'] == 1000.0


def dcmstack_test_dicom(attrs):
    from test.test_dcmstack import make_dicom
    return make_dicom(attrs)


def test_get_meta_time_samples_in_5d():
    exts = []
    for vec in range(2):
        ext = dcmmeta.DcmMetaExtension.make_empty(
            (2, 2, 1, 3), np.eye(4), np.eye(4), 2
        )
        ext.get_class_dict(('time', 'samples'))['Test'] = [
            vec * 10 + time for time in range(3)
        ]
        ext.check_valid()
        exts.append(ext)
    merged = dcmmeta.DcmMetaExtension.from_sequence(exts, 4)
    data = np.zeros(merged.shape)
    nii = nb.Nifti1Image(data, np.eye(4))
    nii.header.set_dim_info(slice=2)
    wrp = nifti_wrapper.NiftiWrapper(nii, make_empty=True)
    wrp.replace_extension(merged)
    for vec in range(2):
        for time in range(3):
            expected = vec * 10 + time
            assert wrp.get_meta('Test', (0, 0, 0, time, vec)) == expected
            assert gradients.sample_meta_value(
                merged, 'Test', time, vec, 0
            ) == expected
