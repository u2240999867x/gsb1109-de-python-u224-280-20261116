"""Tests for the DICOM modality LUT scaling helpers and their integration."""
import numpy as np
import nibabel as nb

from test.test_dcmstack import make_dicom

import dcmstack
from dcmstack import scaling


def make_scaled_dicom(position=0.0, value=7, slope=None, intercept=None):
    ds = make_dicom({'ImagePositionPatient': [0.0, 0.0, float(position)]},
                    pix_val=value)
    if slope is not None:
        ds.RescaleSlope = slope
    if intercept is not None:
        ds.RescaleIntercept = intercept
    return ds


def build_stack(dicoms):
    stack = dcmstack.DicomStack()
    for dcm in dicoms:
        stack.add_dcm(dcm)
    return stack


def save_and_load(nii):
    import tempfile

    work_dir = tempfile.mkdtemp(prefix='dcmstack-scaling-')
    path = work_dir + '/test.nii.gz'
    nii.to_filename(path)
    return nb.load(path)


class FakeWrapper:
    """Minimal DicomWrapper stand-in for multiframe scaling tests."""

    def __init__(self, shape, data, factors, frames=None, shared=None,
                 dcm_data=None, multiframe=False):
        self.image_shape = shape
        self._data = np.asarray(data)
        self._factors = np.asarray(factors, dtype=np.float64)
        self.frames = frames
        self.shared = shared
        self.dcm_data = dcm_data
        self.is_multiframe = multiframe
        self.is_mosaic = False
        self.affine = np.eye(4)

    @property
    def scale_factors(self):
        return self._factors

    def get_unscaled_data(self):
        return self._data

    def get(self, key, default=None):
        return default


def test_missing_coefficients_use_compatibility_defaults():
    stack = build_stack([make_scaled_dicom(intercept=5.0)])
    factor = stack.get_scale_factors()[0][0]
    assert factor.slope == scaling.DEFAULT_RESCALE_SLOPE
    assert factor.intercept == 5.0
    assert factor.slope_present is False
    assert factor.intercept_present is True

    stack = build_stack([make_scaled_dicom()])
    factor = stack.get_scale_factors()[0][0]
    assert factor.slope == 1.0 and factor.intercept == 0.0
    assert factor.slope_present is False
    assert factor.intercept_present is False


def test_missing_intercept_is_not_a_measured_zero():
    stack = build_stack([make_scaled_dicom(slope=10.0)])
    factor = stack.get_scale_factors()[0][0]
    assert factor.intercept == 0.0
    assert factor.intercept_present is False
    assert factor.slope_present is True


def test_per_slice_scaling_is_applied_per_slice():
    stack = build_stack([
        make_scaled_dicom(0.0, value=7, slope=2.0, intercept=1.0),
        make_scaled_dicom(-1.0, value=9, slope=3.0, intercept=5.0),
    ])
    data = stack.get_data()
    assert data.dtype == np.float32
    assert np.allclose(data[:, :, 0], 7 * 2.0 + 1.0)
    assert np.allclose(data[:, :, 1], 9 * 3.0 + 5.0)

    raw = stack.get_data(scaled=False)
    assert raw.dtype == np.uint16
    assert np.all(raw[:, :, 0] == 7)
    assert np.all(raw[:, :, 1] == 9)


def test_per_slice_scaling_is_materialized_without_rounding():
    stack = build_stack([
        make_scaled_dicom(0.0, value=7, slope=2.0, intercept=1.0),
        make_scaled_dicom(-1.0, value=9, slope=3.0, intercept=5.0),
    ])
    nii = stack.to_nifti()
    # No scaling pair is written into the header, because doing so would
    # apply one pair to slices that have different mappings.
    assert nii.header.get_slope_inter() == (None, None)
    data = np.asarray(nii.dataobj)
    # to_nifti reorders slices, so check each slice against its own pair.
    assert np.allclose(data[:, :, 0], 9 * 3.0 + 5.0)
    assert np.allclose(data[:, :, 1], 7 * 2.0 + 1.0)

    loaded = save_and_load(nii)
    assert loaded.header.get_slope_inter() == (None, None)
    assert np.allclose(np.asarray(loaded.dataobj)[:, :, 0], 32.0)
    assert np.allclose(np.asarray(loaded.dataobj)[:, :, 1], 15.0)


def test_single_exact_pair_stays_in_nifti_header():
    stack = build_stack([
        make_scaled_dicom(0.0, value=7, slope=2.5, intercept=-1024.0),
        make_scaled_dicom(-1.0, value=7, slope=2.5, intercept=-1024.0),
    ])
    plan = stack.get_scaling_plan()
    assert plan.mode == scaling.SCALING_MODE_HEADER
    assert plan.header_lossless

    nii = stack.to_nifti()
    assert nii.header.get_slope_inter() == (2.5, -1024.0)
    assert nii.get_data_dtype() == np.uint16

    loaded = save_and_load(nii)
    assert loaded.dataobj.slope == 2.5
    assert loaded.dataobj.inter == -1024.0
    assert np.allclose(np.asarray(loaded.dataobj), 7 * 2.5 - 1024.0)


def test_pair_that_float32_cannot_hold_is_not_silently_rounded():
    slope = 0.1
    stack = build_stack([make_scaled_dicom(value=7, slope=slope)])
    assert not scaling.is_float32_exact(slope)
    plan = stack.get_scaling_plan()
    assert plan.mode == scaling.SCALING_MODE_MATERIALIZED
    assert not plan.header_lossless

    nii = stack.to_nifti()
    assert nii.header.get_slope_inter() == (None, None)
    # The value 7 * 0.1 is the closest float64 product.  It is not rounded
    # through a float32 header pair or a forced float32 cast.
    assert nii.get_data_dtype() == np.float64
    expected = np.float64(7) * np.float64(slope)
    assert np.asarray(nii.dataobj).ravel()[0] == expected

    loaded = save_and_load(nii)
    assert loaded.get_data_dtype() == np.float64
    assert loaded.header.get_slope_inter() == (None, None)
    assert np.asarray(loaded.dataobj).ravel()[0] == expected


def test_scale_metadata_records_factors_and_presence():
    stack = build_stack([
        make_scaled_dicom(0.0, value=7, slope=2.0, intercept=1.0),
        make_scaled_dicom(-1.0, value=9, slope=3.0, intercept=5.0),
    ])
    nw = dcmstack.NiftiWrapper(stack.to_nifti(embed_meta=True))
    assert nw.meta_ext.get_values_and_class('Scaling.RescaleSlope') == \
        ([3.0, 2.0], ('global', 'slices'))
    assert nw.meta_ext.get_values_and_class('Scaling.RescaleIntercept') == \
        ([5.0, 1.0], ('global', 'slices'))
    const = nw.meta_ext.get_class_dict(('global', 'const'))
    assert const['Scaling.DataEncoding'] == scaling.SCALING_MODE_MATERIALIZED
    # Differing pairs are not a header-rounding problem.
    assert const['Scaling.HeaderLossless'] is True

    stack = build_stack([make_scaled_dicom(value=7)])
    nw = dcmstack.NiftiWrapper(stack.to_nifti(embed_meta=True))
    const = nw.meta_ext.get_class_dict(('global', 'const'))
    assert const['Scaling.RescaleSlopePresent'] is False
    assert const['Scaling.RescaleInterceptPresent'] is False
    assert const['Scaling.DataEncoding'] == scaling.SCALING_MODE_NONE


def test_header_lossless_flag_marks_a_rejected_pair():
    stack = build_stack([make_scaled_dicom(value=7, slope=0.1)])
    nw = dcmstack.NiftiWrapper(stack.to_nifti(embed_meta=True))
    const = nw.meta_ext.get_class_dict(('global', 'const'))
    assert const['Scaling.DataEncoding'] == scaling.SCALING_MODE_MATERIALIZED
    assert const['Scaling.HeaderLossless'] is False

    stack = build_stack([make_scaled_dicom(value=7, slope=2.5, intercept=-1024.0)])
    nw = dcmstack.NiftiWrapper(stack.to_nifti(embed_meta=True))
    const = nw.meta_ext.get_class_dict(('global', 'const'))
    assert const['Scaling.DataEncoding'] == scaling.SCALING_MODE_HEADER
    assert const['Scaling.NiftiSlope'] == 2.5
    assert const['Scaling.NiftiIntercept'] == -1024.0
    assert const['Scaling.HeaderLossless'] is True


def test_split_preserves_single_header_scaling():
    stack = build_stack([make_scaled_dicom(value=7, slope=10.0, intercept=3.0)])
    nw = dcmstack.NiftiWrapper(stack.to_nifti(), make_empty=True)
    for split in nw.split(0):
        assert split.nii_img.header.get_slope_inter() == (10.0, 3.0)
        loaded = save_and_load(split.nii_img)
        assert np.allclose(np.asarray(loaded.dataobj), 73.0)


def test_merge_materializes_differing_slice_scaling():
    slices = []
    for idx, (slope, intercept, value) in enumerate(
        ((2.0, 1.0, 7), (3.0, 5.0, 9))
    ):
        arr = np.full((4, 4, 1), value, dtype=np.uint16)
        affine = np.eye(4)
        affine[:3, 3] = [0.0, 0.0, float(idx)]
        nii = nb.Nifti1Image(arr, affine)
        nii.header.set_dim_info(0, 1, 2)
        nii.header.set_slope_inter(slope, intercept)
        slices.append(dcmstack.NiftiWrapper(nii, make_empty=True))

    merged = dcmstack.NiftiWrapper.from_sequence(slices, 2)
    assert merged.nii_img.header.get_slope_inter() == (None, None)
    data = np.asarray(merged.nii_img.dataobj)
    assert np.allclose(data[:, :, 0], 15.0)
    assert np.allclose(data[:, :, 1], 32.0)


def test_multiframe_factors_are_kept_per_frame():
    frames = []
    for idx in range(2):
        frame = make_scaled_dicom(slope=1.0 + idx, intercept=float(idx))
        frames.append(frame)
    w = FakeWrapper(
        shape=(2, 2, 2),
        data=np.arange(8, dtype=np.uint16).reshape(2, 2, 2),
        factors=[[1.0, 0.0], [2.0, 1.0]],
        frames=frames,
        shared=make_scaled_dicom(),
        dcm_data=make_scaled_dicom(),
        multiframe=True,
    )
    factors = scaling.get_scale_factors(w)
    assert [f.as_tuple() for f in factors] == [(1.0, 0.0), (2.0, 1.0)]
    assert all(f.slope_present for f in factors)

    scaled = scaling.apply_scale_factors(
        w.get_unscaled_data(), factors, factor_shape=(2,)
    )
    raw = w.get_unscaled_data()
    assert np.allclose(scaled[:, :, 0], raw[:, :, 0])
    assert np.allclose(scaled[:, :, 1], raw[:, :, 1] * 2 + 1)

    nw = dcmstack.NiftiWrapper.from_dicom_wrapper(w)
    assert nw.nii_img.header.get_slope_inter() == (None, None)
    assert nw.scaling_plan.mode == scaling.SCALING_MODE_MATERIALIZED


def test_multiframe_shared_factor_can_use_header():
    dcm = make_scaled_dicom(slope=4.0, intercept=2.0)
    w = FakeWrapper(
        shape=(2, 2, 2),
        data=np.arange(8, dtype=np.uint16).reshape(2, 2, 2),
        factors=[[4.0, 2.0]],
        frames=[dcm, dcm],
        shared=dcm,
        dcm_data=dcm,
        multiframe=True,
    )
    nw = dcmstack.NiftiWrapper.from_dicom_wrapper(w)
    assert nw.nii_img.header.get_slope_inter() == (4.0, 2.0)


def test_reloaded_materialized_file_keeps_factors_and_does_not_rescale():
    stack = build_stack([
        make_scaled_dicom(0.0, value=7, slope=2.0, intercept=1.0),
        make_scaled_dicom(-1.0, value=9, slope=3.0, intercept=5.0),
    ])
    loaded = save_and_load(stack.to_nifti(embed_meta=True))
    wrapper = dcmstack.NiftiWrapper(loaded)
    factors, frame_shape, mode = scaling.get_wrapper_scale_info(wrapper)
    assert [f.as_tuple() for f in factors] == [(3.0, 5.0), (2.0, 1.0)]
    assert mode == scaling.SCALING_MODE_MATERIALIZED

    parts = list(wrapper.split(2))
    assert np.unique(np.asarray(parts[0].nii_img.dataobj)) == [32.0]
    assert np.unique(np.asarray(parts[1].nii_img.dataobj)) == [15.0]
    merged = dcmstack.NiftiWrapper.from_sequence(parts, 2)
    assert np.unique(np.asarray(merged.nii_img.dataobj)).tolist() == [15.0, 32.0]


def test_merging_materialized_inputs_does_not_apply_factors_twice():
    parts = []
    for idx, (value, slope, intercept) in enumerate(
        ((7, 2.0, 1.0), (9, 3.0, 5.0))
    ):
        arr = np.full((4, 4, 1), value, dtype=np.uint16)
        affine = np.eye(4)
        affine[:3, 3] = [0.0, 0.0, float(idx)]
        nii = nb.Nifti1Image(arr, affine)
        nii.header.set_dim_info(0, 1, 2)
        nii.header.set_slope_inter(slope, intercept)
        wrapper = dcmstack.NiftiWrapper(nii, make_empty=True)
        wrapper.scaling_plan = scaling.NiftiScalingPlan(
            scaling.SCALING_MODE_MATERIALIZED,
            slope,
            intercept,
            True,
            (scaling.ScaleFactor(slope, intercept),),
            "materialized for the test",
            True,
        )
        wrapper.scale_factors = [scaling.ScaleFactor(slope, intercept)]
        parts.append(wrapper)
    # Make the stored arrays already hold engineering values.
    for wrapper, value in zip(parts, (15.0, 32.0)):
        wrapper.raw_data = None
        wrapper.nii_img = nb.Nifti1Image(
            np.full((4, 4, 1), value, dtype=np.float32),
            wrapper.nii_img.affine,
        )
        wrapper.nii_img.header.set_dim_info(0, 1, 2)

    merged = dcmstack.NiftiWrapper.from_sequence(parts, 2)
    assert np.unique(np.asarray(merged.nii_img.dataobj)).tolist() == [15.0, 32.0]


def test_enhanced_frames_record_flat_shape_not_a_guessed_slice_order():
    frames = [make_scaled_dicom(slope=1.0 + i, intercept=float(i))
              for i in range(4)]
    w = FakeWrapper(
        shape=(2, 2, 2, 2),
        data=np.arange(16, dtype=np.uint16).reshape(2, 2, 2, 2),
        factors=[[1.0 + i, float(i)] for i in range(4)],
        frames=frames,
        shared=make_scaled_dicom(),
        dcm_data=make_scaled_dicom(),
        multiframe=True,
    )
    nw = dcmstack.NiftiWrapper.from_dicom_wrapper(w)
    const = nw.meta_ext.get_class_dict(('global', 'const'))
    assert const['Scaling.FrameShape'] == [2, 2]
    assert const['Scaling.FrameRescaleSlope'] == [1.0, 2.0, 3.0, 4.0]
    # No per-slice list is invented for the 4D frame order.
    assert nw.meta_ext.get_classification('Scaling.RescaleSlope') is None

    parts = list(nw.split(3))
    assert [f.as_tuple() for f in parts[0].scale_factors] == [(1.0, 0.0), (2.0, 1.0)]
    assert [f.as_tuple() for f in parts[1].scale_factors] == [(3.0, 2.0), (4.0, 3.0)]


def test_scale_factor_dataclass_provenance_is_preserved():
    factor = scaling.ScaleFactor(2.0, 0.0, slope_present=True,
                                 intercept_present=False)
    assert factor.as_tuple() == (2.0, 0.0)
    assert not factor.is_identity()
    assert factor.is_lossless_in_nifti_header()

    plan = scaling.make_scaling_plan([factor])
    assert plan.mode == scaling.SCALING_MODE_HEADER
    assert plan.factors[0].intercept_present is False
