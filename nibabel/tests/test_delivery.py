"""Tests for lossless NIfTI delivery planning."""

import json
from fractions import Fraction

import numpy as np
import pytest
from numpy.testing import assert_array_equal

import nibabel as nib
from nibabel.cmdline import deliver
from nibabel.delivery import (
    DeliveryBlocked,
    TargetProfile,
    analyze_values,
    plan_delivery,
    verify_round_trip,
    write_delivery,
)
from nibabel.testing import get_test_data


def _image(values, dtype=np.float64, shape=None):
    values = np.asarray(values, dtype=dtype)
    if shape is not None:
        values = values.reshape(shape)
    return nib.Nifti1Image(values, np.eye(4))


def _round_trip_bytes(plan):
    return plan.image.__class__.from_bytes(plan.image.to_bytes())


# ---------------------------------------------------------------------------
# Value analysis
# ---------------------------------------------------------------------------


def test_analyze_integer_values():
    analysis = analyze_values(np.array([0, 3, 9], dtype=np.int16))
    assert analysis.all_integer
    assert analysis.integer_values
    assert analysis.min_value == 0
    assert analysis.max_value == 9
    assert analysis.nonfinite_count == 0


def test_analyze_fractional_step():
    analysis = analyze_values(np.array([1000.5, 1001.5, 1002.5]))
    assert not analysis.all_integer
    assert analysis.common_step == Fraction(1)
    assert analysis.offset == Fraction(2001, 2)


def test_analyze_counts_nonfinite():
    analysis = analyze_values(np.array([1.0, np.nan, np.inf, -np.inf]))
    assert analysis.nan_count == 1
    assert analysis.posinf_count == 1
    assert analysis.neginf_count == 1
    assert analysis.nonfinite_count == 3
    assert analysis.finite_count == 1
    assert not analysis.integer_values


def test_analyze_no_finite_values():
    analysis = analyze_values(np.array([np.nan, np.inf]))
    assert analysis.finite_count == 0
    assert analysis.common_step is None


# ---------------------------------------------------------------------------
# Planning: dtype cannot be judged from the source type alone
# ---------------------------------------------------------------------------


def test_fractional_values_need_scaling():
    img = _image([0.0, 0.5, 1.0, 1.5], shape=(2, 2, 1))
    plan = plan_delivery(img, TargetProfile(dtypes=('uint8', 'int16', 'float32')))
    assert plan.ok
    assert plan.encoding.dtype == 'uint8'
    assert plan.encoding.slope == Fraction(1, 2)
    assert plan.encoding.inter == 0
    assert verify_round_trip(plan)['ok']


def test_integer_values_shrink():
    img = _image(np.arange(16), dtype=np.float64, shape=(4, 4, 1))
    plan = plan_delivery(img, TargetProfile(dtypes=('uint8', 'int16', 'float32')))
    assert plan.encoding.dtype == 'uint8'
    assert plan.encoding.slope == 1
    assert plan.encoding.inter == 0


def test_float64_values_do_not_shrink_without_scaling():
    img = _image([0.1, 0.2, 0.3, 0.4], shape=(2, 2, 1))
    plan = plan_delivery(img, TargetProfile(dtypes=('int16', 'float32', 'float64')))
    assert plan.encoding.dtype == 'float64'
    assert verify_round_trip(plan)['ok']


def test_scaling_with_intercept():
    img = _image([1000.5, 1001.5, 1002.5, 1003.5], shape=(2, 2, 1))
    plan = plan_delivery(img, TargetProfile(dtypes=('uint8', 'int16', 'float32')))
    assert plan.encoding.dtype == 'uint8'
    assert plan.encoding.inter != 0
    assert verify_round_trip(plan)['ok']


def test_source_scaling_is_decoded_before_analysis(tmp_path):
    raw = np.arange(16, dtype=np.int16).reshape(4, 4, 1)
    img = nib.Nifti1Image(raw, np.eye(4))
    img.header.set_slope_inter(0.5, 10.0)
    filename = tmp_path / 'scaled.nii'
    nib.save(img, filename)
    loaded = nib.load(filename)
    analysis = analyze_values(np.asanyarray(loaded.dataobj))
    # Loaded files carry the scaling in the proxy, not the header fields.
    assert loaded.header.get_slope_inter() == (None, None)
    assert analysis.min_value == 10
    assert analysis.max_value == Fraction(35, 2)


# ---------------------------------------------------------------------------
# Non-finite values
# ---------------------------------------------------------------------------


def test_nan_forbidden_by_integer_only_target():
    img = _image([1.0, np.nan, 3.0, 4.0], shape=(2, 2, 1))
    plan = plan_delivery(img, TargetProfile(dtypes=('uint8', 'int16')))
    assert not plan.ok
    assert any(b['field'] == 'datatype' for b in plan.blockers)


def test_nan_allowed_by_float_target():
    img = _image([1.0, np.nan, 3.0, 4.0], shape=(2, 2, 1))
    plan = plan_delivery(img, TargetProfile(dtypes=('uint8', 'float32', 'float64')))
    assert plan.encoding.dtype == 'float32'
    assert verify_round_trip(plan)['ok']


def test_nan_is_not_treated_as_zero():
    img = _image([np.nan, 1.0, 2.0, 3.0], shape=(2, 2, 1))
    plan = plan_delivery(img, TargetProfile(dtypes=('float64',)))
    payload = _round_trip_bytes(plan)
    assert np.isnan(np.asanyarray(payload.dataobj)[0, 0, 0])


def test_infinities_round_trip():
    img = _image([1.0, np.inf, -np.inf, np.nan], shape=(2, 2, 1))
    plan = plan_delivery(img, TargetProfile(dtypes=('float32', 'float64')))
    assert verify_round_trip(plan)['ok']


# ---------------------------------------------------------------------------
# Blockers
# ---------------------------------------------------------------------------


def test_no_scaling_mode_blocks_fractional_values():
    img = _image([0.0, 0.5, 1.0, 1.5], shape=(2, 2, 1))
    plan = plan_delivery(img, TargetProfile(dtypes=('uint8', 'int16'), scaling=('none',)))
    assert not plan.ok
    assert plan.blockers


def test_dim_limit_blocks():
    img = _image(np.zeros(4 * 5 * 6), shape=(4, 5, 6))
    plan = plan_delivery(img, TargetProfile(max_dim=5))
    assert not plan.ok
    assert any(b['field'] == 'dim[3]' for b in plan.blockers)


def test_extreme_values_block_integer_target():
    img = _image([0.0, 1e20], shape=(1, 2, 1))
    plan = plan_delivery(img, TargetProfile(dtypes=('uint8', 'int16', 'int32')))
    assert not plan.ok


def test_delivery_blocked_message():
    img = _image([0.0, 0.5], shape=(1, 2, 1))
    plan = plan_delivery(img, TargetProfile(dtypes=('uint8',), scaling=('none',)))
    with pytest.raises(DeliveryBlocked):
        write_delivery(plan, 'unused.nii')


# ---------------------------------------------------------------------------
# Header duties
# ---------------------------------------------------------------------------


def test_migrations_reported():
    img = _image([0.0, 0.5, 1.0, 1.5], shape=(2, 2, 1))
    plan = plan_delivery(img, TargetProfile(dtypes=('uint8', 'float32')))
    fields = {migration.field for migration in plan.migrations}
    assert fields == {
        'version',
        'datatype',
        'scaling',
        'dim',
        'vox_offset',
        'extensions',
        'pixdim',
        'qform',
        'sform',
        'xyzt_units',
        'intent',
        'calibration',
    }
    dtype_migration = next(m for m in plan.migrations if m.field == 'datatype')
    assert dtype_migration.original == 'float64'
    assert dtype_migration.delivered == 'uint8'


def test_dimensions_and_affine_preserved():
    img = nib.Nifti1Image(np.zeros((4, 5, 6)), np.diag([2.0, 3.0, 4.0, 1.0]))
    plan = plan_delivery(img, TargetProfile(dtypes=('float64',)))
    assert plan.image.shape == (4, 5, 6)
    assert np.allclose(plan.image.affine, img.affine)


def test_extensions_preserved():
    source = get_test_data(fname='example4d.nii.gz')
    img = nib.load(source)
    plan = plan_delivery(img, TargetProfile(dtypes=('int16',)))
    assert [e.get_code() for e in plan.image.header.extensions] == [6, 6]
    assert verify_round_trip(plan)['ok']


def test_extension_drop_allowed():
    source = get_test_data(fname='example4d.nii.gz')
    img = nib.load(source)
    plan = plan_delivery(img, TargetProfile(dtypes=('int16',), extensions='drop'))
    assert plan.ok


# ---------------------------------------------------------------------------
# Versions and byte order
# ---------------------------------------------------------------------------


def test_nifti2_target(tmp_path):
    source = get_test_data(fname='example4d.nii.gz')
    img = nib.load(source)
    plan = plan_delivery(img, TargetProfile(versions=('nifti2',), dtypes=('int16',)))
    assert plan.encoding.version == 'nifti2'
    version_migration = next(m for m in plan.migrations if m.field == 'version')
    assert version_migration.original == 'nifti1'
    assert version_migration.delivered == 'nifti2'
    assert verify_round_trip(plan)['ok']


def test_big_endian_source(tmp_path):
    source = get_test_data(fname='anatomical.nii')
    img = nib.load(source)
    plan = plan_delivery(img, TargetProfile(dtypes=('int16', 'float32')))
    out = tmp_path / 'out.nii'
    write_delivery(plan, out)
    back = nib.load(out)
    assert_array_equal(np.asanyarray(img.dataobj), np.asanyarray(back.dataobj))


def test_write_and_reload(tmp_path):
    source = get_test_data(fname='example4d.nii.gz')
    img = nib.load(source)
    plan = plan_delivery(img, TargetProfile(dtypes=('int16', 'float32')))
    out = tmp_path / 'out.nii.gz'
    write_delivery(plan, out)
    back = nib.load(out)
    assert_array_equal(np.asanyarray(img.dataobj), np.asanyarray(back.dataobj))
    assert back.get_data_dtype() == plan.image.get_data_dtype()


def test_source_file_unchanged(tmp_path):
    source = get_test_data(fname='example4d.nii.gz')
    original = source.read_bytes()
    img = nib.load(source)
    plan = plan_delivery(img, TargetProfile(dtypes=('int16',)))
    write_delivery(plan, tmp_path / 'out.nii.gz')
    assert source.read_bytes() == original


# ---------------------------------------------------------------------------
# Profile serialization and CLI
# ---------------------------------------------------------------------------


def test_profile_round_trip():
    profile = TargetProfile(
        versions=('nifti1',), dtypes=('uint8', 'int16'), scaling=('header', 'none')
    )
    restored = TargetProfile.from_json(json.loads(json.dumps(profile.as_dict())))
    assert restored == profile


def test_profile_rejects_unknown():
    with pytest.raises(ValueError):
        TargetProfile(versions=('nifti3',))
    with pytest.raises(ValueError):
        TargetProfile(dtypes=('float16',))


def test_cli_dry_run(capsys):
    source = get_test_data(fname='example4d.nii.gz')
    rc = deliver.main([str(source), '--dtype', 'int16', '--json'])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report['ok']
    assert report['verification']['ok']


def test_cli_blocked(tmp_path, capsys):
    source = get_test_data(fname='example4d.nii.gz')
    rc = deliver.main([str(source), '--dtype', 'uint8', '--scaling', 'none', '--json'])
    assert rc == 1
    report = json.loads(capsys.readouterr().out)
    assert not report['ok']
    assert report['blockers']


def test_cli_write(tmp_path):
    source = get_test_data(fname='example4d.nii.gz')
    out = tmp_path / 'delivery.nii.gz'
    rc = deliver.main([str(source), str(out), '--dtype', 'int16'])
    assert rc == 0
    assert out.is_file()
    back = nib.load(out)
    original = nib.load(source)
    assert_array_equal(np.asanyarray(original.dataobj), np.asanyarray(back.dataobj))


def test_negative_zero_preserved_as_float():
    img = _image([1.0, -0.0, 0.0, 2.0], shape=(2, 2, 1))
    plan = plan_delivery(img, TargetProfile(dtypes=('uint8', 'int16', 'float32', 'float64')))
    # Integer types cannot represent -0.0, so the planner must keep a float.
    assert plan.encoding.dtype in ('float32', 'float64')
    assert verify_round_trip(plan)['ok']
    payload = np.asanyarray(plan.image.__class__.from_bytes(plan.image.to_bytes()).dataobj)
    assert np.signbit(payload.reshape(-1)[1])


def test_all_negative_zero():
    img = _image([-0.0, -0.0, -0.0, -0.0], shape=(2, 2, 1))
    plan = plan_delivery(img, TargetProfile(dtypes=('uint8', 'float32')))
    assert plan.encoding.dtype == 'float32'
    assert verify_round_trip(plan)['ok']


def test_wide_uint64_preserved():
    values = np.array([0, 2**63, 2**63 + 1, 7], dtype=np.uint64).reshape(2, 2, 1)
    with pytest.warns(FutureWarning, match='uint64'):
        img = nib.Nifti1Image(values, np.eye(4))
    plan = plan_delivery(img, TargetProfile(dtypes=('int64', 'uint64', 'float64')))
    assert plan.encoding.dtype == 'uint64'
    assert verify_round_trip(plan)['ok']


def test_wide_int64_preserved():
    values = np.array([2**60, 2**60 + 1, 0, 1], dtype=np.int64).reshape(2, 2, 1)
    with pytest.warns(FutureWarning, match='int64'):
        img = nib.Nifti1Image(values, np.eye(4))
    plan = plan_delivery(img, TargetProfile(dtypes=('int64', 'float64')))
    assert plan.encoding.dtype == 'int64'
    assert verify_round_trip(plan)['ok']


def test_empty_volume_plans():
    img = nib.Nifti1Image(np.zeros((0, 0, 1)), np.eye(4))
    plan = plan_delivery(img, TargetProfile(dtypes=('uint8', 'float64')))
    assert plan.ok
    assert verify_round_trip(plan)['ok']


def test_constant_volume():
    img = _image([5.0] * 8, shape=(2, 2, 2))
    plan = plan_delivery(img, TargetProfile(dtypes=('uint8', 'float32')))
    assert plan.encoding.dtype == 'uint8'
    assert verify_round_trip(plan)['ok']


def test_complex_delivery():
    img = _image([1 + 2j, 3 - 4j, 0j, 1 + 0j], dtype=np.complex128, shape=(2, 2, 1))
    plan = plan_delivery(img, TargetProfile(dtypes=('complex64', 'complex128')))
    assert plan.encoding.dtype == 'complex64'
    assert verify_round_trip(plan)['ok']


def test_complex_blocked_without_complex_target():
    img = _image([1 + 2j, 3 - 4j, 0j, 1 + 0j], dtype=np.complex128, shape=(2, 2, 1))
    plan = plan_delivery(img, TargetProfile(dtypes=('float64', 'uint8')))
    assert not plan.ok


def test_cli_target_file(tmp_path, capsys):
    source = get_test_data(fname='example4d.nii.gz')
    target_file = tmp_path / 'target.json'
    target_file.write_text(
        json.dumps({'dtype': 'int16', 'version': 'nifti1', 'scaling': ['header']})
    )
    rc = deliver.main([str(source), '--target', str(target_file), '--json'])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report['target']['versions'] == ['nifti1']


def test_offset_alignment_checked():
    source = get_test_data(fname='example4d.nii.gz')
    img = nib.load(source)
    # Two comment extensions occupy 32 bytes after the 352-byte header.
    allowed = plan_delivery(img, TargetProfile(dtypes=('int16',), vox_offset_alignment=16))
    assert allowed.ok
    blocked = plan_delivery(img, TargetProfile(dtypes=('int16',), vox_offset_alignment=100))
    assert not blocked.ok
    assert any(b['field'] == 'vox_offset' for b in blocked.blockers)


def test_written_scaling_is_exact(tmp_path):
    values = np.array([1000.5, 1001.5, 1002.5, 1003.5]).reshape(2, 2, 1)
    img = nib.Nifti1Image(values, np.eye(4))
    plan = plan_delivery(img, TargetProfile(dtypes=('uint8', 'float32')))
    assert plan.encoding.dtype == 'uint8'
    out = tmp_path / 'scaled.nii'
    write_delivery(plan, out)
    back = nib.load(out)
    assert back.dataobj.slope == 1.0
    assert back.dataobj.inter == 1000.5
    assert_array_equal(np.asanyarray(back.dataobj), values)


@pytest.mark.parametrize(
    'values',
    [
        [0.0, 0.5, 1.0, 1.5],
        [1000.5, 1001.5, 1002.5, 1003.5],
        [-3.0, -2.5, -2.0, -1.5],
        [1e6, 1e6 + 1, 1e6 + 2, 1e6 + 3],
        [0.25, 0.5, 0.75, 1.0],
    ],
)
def test_scaled_files_read_back_exactly(tmp_path, values):
    img = nib.Nifti1Image(np.asarray(values).reshape(2, 2, 1), np.eye(4))
    plan = plan_delivery(
        img,
        TargetProfile(dtypes=('uint8', 'int16', 'int32', 'float32', 'float64')),
    )
    out = tmp_path / 'out.nii'
    write_delivery(plan, out)
    back = nib.load(out)
    assert_array_equal(np.asanyarray(back.dataobj), np.asarray(values).reshape(2, 2, 1))


def test_verification_covers_header():
    source = get_test_data(fname='example4d.nii.gz')
    img = nib.load(source)
    plan = plan_delivery(img, TargetProfile(versions=('nifti1',), dtypes=('int16',)))
    report = verify_round_trip(plan)
    assert report['ok']
    assert report['header_ok']
    assert report['dtype'] == 'int16'
    assert report['extensions'] == [6, 6]


def test_dim_limit_is_version_aware():
    img = nib.Nifti2Image(np.zeros((40000, 2, 1), dtype=np.uint8), np.eye(4))
    both = plan_delivery(img, TargetProfile(dtypes=('uint8',)))
    assert both.ok
    assert both.encoding.version == 'nifti2'

    nifti1_only = plan_delivery(img, TargetProfile(versions=('nifti1',), dtypes=('uint8',)))
    assert not nifti1_only.ok
    assert any(b['field'] == 'dim[1]' for b in nifti1_only.blockers)

    tight = plan_delivery(
        img, TargetProfile(versions=('nifti2',), dtypes=('uint8',), max_dim=10000)
    )
    assert not tight.ok


def test_nifti2_allows_float64_scaling():
    # A dyadic step that is exact in float64 but not in float32.
    step = Fraction(2**24 + 1, 2**30)
    values = np.array([float(step * index) for index in range(4)]).reshape(2, 2, 1)
    img = nib.Nifti1Image(values, np.eye(4))
    nifti1 = plan_delivery(img, TargetProfile(versions=('nifti1',), dtypes=('uint8', 'float64')))
    assert nifti1.encoding.dtype == 'float64'
    nifti2 = plan_delivery(img, TargetProfile(versions=('nifti2',), dtypes=('uint8', 'float64')))
    assert nifti2.encoding.dtype == 'uint8'
    assert verify_round_trip(nifti2)['ok']
