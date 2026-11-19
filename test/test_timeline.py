"""Tests for strict timeline concatenation."""
import json

import numpy as np
import nibabel as nb
import pytest

from dcmstack import timeline
from dcmstack.nifti_wrapper import NiftiWrapper


AFFINE = np.diag([2.0, 2.0, 3.0, 1.0])


def make_wrapper(n_times=2, affine=AFFINE, dtype=np.int16, offset=0,
                 time_values=None, study="study-1"):
    shape = (4, 4, 3, n_times) if n_times > 1 else (4, 4, 3)
    data = (np.arange(np.prod(shape), dtype=dtype).reshape(shape) + offset)
    nii = nb.Nifti1Image(data, np.array(affine, dtype=float))
    nii.header.set_dim_info(0, 1, 2)
    nii.header.set_xyzt_units('mm', 'sec')
    wrapper = NiftiWrapper(nii, make_empty=True)
    wrapper.meta_ext.get_class_dict(('global', 'const'))['StudyInstanceUID'] = study
    if time_values is not None:
        wrapper.meta_ext.get_class_dict(('time', 'samples'))[
            'SND.AcquisitionTimeStamp'] = list(time_values)
        wrapper.meta_ext.time_dim = 'SND.AcquisitionTimeStamp'
    return wrapper


def spec(aid, start, end=None, **kwargs):
    if "coverage" not in kwargs:
        kwargs["coverage"] = (start, end)
    return timeline.BatchSpec(acquisition_id=aid, **kwargs)


def test_timeline_joins_four_dimensional_batches():
    first = make_wrapper(2, offset=0, time_values=[100.0, 101.0])
    second = make_wrapper(2, offset=1000, time_values=[103.0, 104.0])
    result = timeline.build_timeline([
        (first, spec("b1", 100.0, 101.0, time_key="SND.AcquisitionTimeStamp")),
        (second, spec("b2", 103.0, 104.0, time_key="SND.AcquisitionTimeStamp")),
    ])
    assert result.wrapper.nii_img.shape == (4, 4, 3, 4)
    data = np.asanyarray(result.wrapper.nii_img.dataobj)
    assert np.array_equal(data[..., 0], np.asanyarray(first.nii_img.dataobj)[..., 0])
    assert np.array_equal(data[..., 3], np.asanyarray(second.nii_img.dataobj)[..., 1])
    assert len(result.gaps) == 1
    assert result.gaps[0]["status"] == "declared-gap"
    assert result.gaps[0]["after"] == "b1"
    assert result.gaps[0]["duration"] == pytest.approx(2.0)
    assert result.overlaps == []
    assert result.record["coverage"]["deduplicated"] is False
    assert result.record["geometry"]["resampling_performed"] is False
    assert [b["acquisition_id"] for b in result.record["batches"]] == ["b1", "b2"]
    assert result.wrapper.meta_ext.get_class_dict(
        ('global', 'const'))['dcmstack.timeline'] == result.record
    assert result.wrapper.meta_ext.time_dim == "SND.AcquisitionTimeStamp"
    # Source provenance survives.
    assert result.wrapper.meta_ext.get_class_dict(
        ('global', 'const'))['StudyInstanceUID'] == "study-1"


def test_overlap_is_retained_not_deduplicated():
    first = make_wrapper(2, offset=0)
    second = make_wrapper(2, offset=1000)
    result = timeline.build_timeline([
        (first, spec("b1", 0.0, 10.0)),
        (second, spec("b2", 8.0, 18.0)),
    ])
    assert result.wrapper.nii_img.shape[3] == 4
    assert len(result.overlaps) == 1
    assert result.overlaps[0]["status"] == "retained-overlap"
    assert result.overlaps[0]["duration"] == pytest.approx(2.0)
    assert result.gaps == []


def test_gap_is_marked_in_metadata_not_filled():
    first = make_wrapper(1, offset=0)
    second = make_wrapper(1, offset=1000)
    result = timeline.build_timeline([
        (first, spec("a", 0.0, 2.0)),
        (second, spec("b", 5.0, 7.0)),
    ])
    assert result.wrapper.nii_img.shape[3] == 2
    assert result.record["coverage"]["gaps"][0]["start"] == 2.0
    assert result.record["coverage"]["gaps"][0]["end"] == 5.0
    # No synthetic sample was inserted.
    assert result.wrapper.meta_ext.shape[3] == 2


def test_spatial_shift_is_refused():
    moved = AFFINE.copy()
    moved[:3, 3] += [0.0, 0.0, 12.0]
    with pytest.raises(timeline.GeometryError) as excinfo:
        timeline.build_timeline([
            (make_wrapper(1), spec("b1", 0.0, 1.0)),
            (make_wrapper(1, affine=moved), spec("b2", 2.0, 3.0)),
        ])
    assert "12.0" in str(excinfo.value)


def test_resampled_batch_requires_note():
    with pytest.raises(timeline.ResamplingError):
        spec("b2", 2.0, 3.0, resampled=True)


def test_resampled_declaration_is_recorded():
    # Declaring upstream resampling is recorded, but the join still requires a
    # single final grid: this module never puts samples on different grids.
    moved = AFFINE.copy()
    moved[:3, 3] += [1.0, 0.0, 0.0]
    with pytest.raises(timeline.GeometryError):
        timeline.build_timeline([
            (make_wrapper(1), spec("b1", 0.0, 1.0)),
            (make_wrapper(1, affine=moved),
             spec("b2", 2.0, 3.0, resampled=True,
                  resample_note="aligned to b1 with 6-DOF rigid transform")),
        ])
    result = timeline.build_timeline([
        (make_wrapper(1), spec("b1", 0.0, 1.0)),
        (make_wrapper(1),
         spec("b2", 2.0, 3.0, resampled=True,
              resample_note="aligned to b1 with 6-DOF rigid transform")),
    ])
    record = result.record["batches"][1]
    assert record["resampled"] is True
    assert "6-DOF" in record["resample_note"]
    assert result.record["geometry"]["resampled"] is True
    assert result.record["geometry"]["resampling_performed"] is False


def test_undeclared_existing_resampling_is_refused():
    wrapper = make_wrapper(1)
    wrapper.meta_ext.affine = AFFINE + np.diag([0.1, 0.0, 0.0, 0.0])
    with pytest.raises(timeline.ResamplingError):
        timeline.build_timeline([
            (wrapper, spec("b1", 0.0, 1.0)),
            (make_wrapper(1), spec("b2", 2.0, 3.0)),
        ])


def test_missing_coverage_is_refused():
    with pytest.raises(timeline.CoverageError):
        timeline.BatchSpec(acquisition_id="b1", coverage=None)


def test_missing_identity_is_refused():
    with pytest.raises(timeline.IdentityError):
        timeline.BatchSpec(acquisition_id="", coverage=(0, 1))
    with pytest.raises(timeline.IdentityError):
        timeline.build_timeline([(make_wrapper(1), spec("same", 0, 1)),
                                 (make_wrapper(1), spec("same", 2, 3))])


def test_dimension_definitions_must_match():
    second = make_wrapper(1)
    second.nii_img.header.set_dim_info(0, 2, 1)
    with pytest.raises(timeline.GeometryError):
        timeline.build_timeline([
            (make_wrapper(1), spec("b1", 0.0, 1.0)),
            (second, spec("b2", 2.0, 3.0)),
        ])


def test_metadata_shape_must_match_image():
    wrapper = make_wrapper(1)
    wrapper.meta_ext.shape = (4, 4, 3, 2)
    with pytest.raises(timeline.IdentityError):
        timeline.build_timeline([(wrapper, spec("b1", 0, 1)),
                                 (make_wrapper(1), spec("b2", 2, 3))])


def test_unordered_inputs_can_be_sorted():
    first = make_wrapper(1, offset=1000)
    second = make_wrapper(1, offset=0)
    result = timeline.build_timeline([
        (first, spec("late", 10.0, 11.0)),
        (second, spec("early", 0.0, 1.0)),
    ], sort=True)
    assert [b["acquisition_id"] for b in result.record["batches"]] == ["early", "late"]
    data = np.asanyarray(result.wrapper.nii_img.dataobj)
    assert data[0, 0, 0, 0] == 0
    assert data[0, 0, 0, 1] == 1000


def test_unsorted_inputs_rejected_when_sort_disabled():
    with pytest.raises(timeline.CoverageError):
        timeline.build_timeline([
            (make_wrapper(1), spec("late", 10.0, 11.0)),
            (make_wrapper(1), spec("early", 0.0, 1.0)),
        ], sort=False)


def test_open_ended_coverage_only_last():
    with pytest.raises(timeline.CoverageError):
        timeline.build_timeline([
            (make_wrapper(1), spec("b1", 0.0, None)),
            (make_wrapper(1), spec("b2", 2.0, 3.0)),
        ])


def test_time_key_constant_across_batches_is_rejected():
    first = make_wrapper(2, time_values=[1.0, 1.0])
    with pytest.raises(timeline.IdentityError):
        timeline.build_timeline([
            (first, spec("b1", 0, 1, time_key="SND.AcquisitionTimeStamp")),
            (make_wrapper(2, time_values=[2.0, 2.0]),
             spec("b2", 2, 3, time_key="SND.AcquisitionTimeStamp")),
        ])


def test_from_files_and_spec_parser(tmp_path):
    first_path = str(tmp_path / "a.nii.gz")
    second_path = str(tmp_path / "b.nii.gz")
    make_wrapper(1).to_filename(first_path)
    make_wrapper(1, offset=1000).to_filename(second_path)
    spec_path = tmp_path / "batches.json"
    spec_path.write_text(json.dumps([
        {"path": first_path, "acquisition_id": "b1", "coverage": [0, 1],
         "source": "session-1"},
        {"path": second_path, "acquisition_id": "b2", "coverage": [2, 3],
         "metadata": {"scanner": "scanner-a"}},
    ]))
    specs = timeline.parse_batch_specs(str(spec_path))
    result = timeline.timeline_from_files(specs)
    assert result.wrapper.nii_img.shape[3] == 2
    assert result.record["batches"][0]["source"] == "session-1"
    assert result.record["batches"][0]["source_paths"] == [first_path]
    assert result.record["batches"][1]["metadata"] == {"scanner": "scanner-a"}
    out = str(tmp_path / "timeline.nii.gz")
    result.to_filename(out)
    reloaded = NiftiWrapper.from_filename(out)
    assert reloaded.meta_ext.get_class_dict(
        ('global', 'const'))['dcmstack.timeline'] == result.record


def test_millisecond_coverage_is_normalised():
    result = timeline.build_timeline([
        (make_wrapper(1), spec("b1", 0, 1000,
                               coverage={"start": 0, "end": 1000,
                                         "units": "milliseconds"})),
        (make_wrapper(1), spec("b2", 0, 0,
                               coverage={"start": 2000, "end": 3000,
                                         "units": "milliseconds"})),
    ])
    assert result.record["coverage"]["start"] == 0.0
    assert result.record["coverage"]["end"] == 3.0
    assert result.gaps[0]["duration"] == pytest.approx(1.0)


def test_source_time_key_used_when_strictly_increasing():
    result = timeline.build_timeline([
        (make_wrapper(2, time_values=[100.0, 101.0]),
         spec("b1", 100.0, 101.0, time_key="SND.AcquisitionTimeStamp")),
        (make_wrapper(2, offset=1000, time_values=[102.0, 103.0]),
         spec("b2", 102.0, 103.0, time_key="SND.AcquisitionTimeStamp")),
    ])
    assert result.wrapper.meta_ext.time_dim == "SND.AcquisitionTimeStamp"
    assert result.record["time_axis"]["reason"] == "source times are strictly increasing"
    assert result.wrapper.meta_ext.get_values_and_class(
        "SND.AcquisitionTimeStamp") == ([100.0, 101.0, 102.0, 103.0],
                                        ("time", "samples"))


def test_overlap_does_not_force_false_time_order():
    result = timeline.build_timeline([
        (make_wrapper(2, time_values=[0.0, 10.0]),
         spec("b1", 0.0, 10.0, time_key="SND.AcquisitionTimeStamp")),
        (make_wrapper(2, offset=1000, time_values=[5.0, 15.0]),
         spec("b2", 8.0, 18.0, time_key="SND.AcquisitionTimeStamp")),
    ])
    assert result.record["coverage"]["overlaps"]
    assert result.wrapper.meta_ext.time_dim == timeline.TIME_INDEX_KEY
    assert result.record["time_axis"]["source_key"] == "SND.AcquisitionTimeStamp"
    # Original timestamps are preserved, not re-sorted.
    assert result.wrapper.meta_ext.get_values_and_class(
        "SND.AcquisitionTimeStamp") == ([0.0, 10.0, 5.0, 15.0],
                                        ("time", "samples"))


def test_timeline_record_is_json_serialisable():
    result = timeline.build_timeline([
        (make_wrapper(1), spec("b1", 0.0, 1.0)),
        (make_wrapper(1), spec("b2", 2.0, 3.0)),
    ])
    assert json.loads(json.dumps(result.record)) == result.record


def test_5d_singleton_vector_is_flattened_to_4d():
    arr = np.arange(4 * 4 * 3 * 2 * 1, dtype=np.int16).reshape(4, 4, 3, 2, 1)
    nii = nb.Nifti1Image(arr, AFFINE.copy())
    nii.header.set_dim_info(0, 1, 2)
    wrapper = NiftiWrapper(nii, make_empty=True)
    result = timeline.build_timeline([
        (wrapper, spec("a", 0.0, 1.0)),
        (wrapper, spec("b", 2.0, 3.0)),
    ])
    assert result.wrapper.nii_img.shape == (4, 4, 3, 4)
    assert result.wrapper.meta_ext.shape == (4, 4, 3, 4)


def test_consistent_declared_time_dim_is_inherited():
    result = timeline.build_timeline([
        (make_wrapper(2, time_values=[10.0, 11.0]), spec("b1", 10.0, 11.0)),
        (make_wrapper(2, offset=1000, time_values=[12.0, 13.0]),
         spec("b2", 12.0, 13.0)),
    ])
    assert result.record["batches"][0]["time_key"] == "SND.AcquisitionTimeStamp"
    assert result.wrapper.meta_ext.time_dim == "SND.AcquisitionTimeStamp"


def test_existing_timeline_can_be_recomposed():
    first = timeline.build_timeline([
        (make_wrapper(1), spec("a", 0.0, 1.0)),
        (make_wrapper(1), spec("b", 2.0, 3.0)),
    ])
    second = timeline.build_timeline([
        (first.wrapper, timeline.BatchSpec("merged", (0.0, 3.0))),
        (make_wrapper(1), timeline.BatchSpec("c", (4.0, 5.0))),
    ])
    assert second.wrapper.nii_img.shape[3] == 3
    assert [b["acquisition_id"] for b in second.record["batches"]] == ["merged", "c"]
    assert len(second.record["batches"][0]["source_paths"]) == 0


def test_generic_join_refuses_time_axis_origin_shift():
    moved = AFFINE.copy()
    moved[:3, 3] += [0.0, 0.0, 4.0]
    with pytest.raises(ValueError):
        NiftiWrapper.from_sequence([make_wrapper(1), make_wrapper(1, affine=moved)], 3)
