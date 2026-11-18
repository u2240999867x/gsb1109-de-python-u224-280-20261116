import numpy as np
import pytest

from cdflib import CDF, cdfwrite
from cdflib.ingest import ingest, merge_batches, reexport
from cdflib.dataclasses import SPARSE_MISSING, SPARSE_PAD, SPARSE_PREVIOUS, SPARSE_WRITTEN


def _write_batch(path, sparse, records, values, pad=None, dtype=8, epoch=None):
    spec = {
        "Variable": "quality",
        "Data_Type": dtype,
        "Num_Elements": 1,
        "Rec_Vary": True,
        "Dim_Sizes": [],
        "Sparse": sparse,
    }
    if pad is not None:
        spec["Pad"] = np.array([pad], dtype=np.float64 if dtype in (21, 22, 45) else np.int64)
    epochs = np.array(epoch if epoch is not None else [0, 100, 200, 300], dtype=np.int64)
    with cdfwrite.CDF(path) as writer:
        writer.write_var({"Variable": "Epoch", "Data_Type": 33, "Num_Elements": 1, "Rec_Vary": True, "Dim_Sizes": []},
                         var_data=epochs)
        writer.write_var(spec, var_attrs={"DEPEND_0": "Epoch"}, var_data=[np.asarray(records), np.asarray(values)])


def test_previous_sparse_points_to_physical_source_outside_window(tmp_path):
    fn = tmp_path / "previous.cdf"
    _write_batch(fn, "prev_sparse", [0, 3], [10.0, 40.0], dtype=22)

    with CDF(fn) as cdf:
        full = cdf.varget_provenance("quality")
        assert [sample.status for sample in full.provenance] == [
            SPARSE_WRITTEN,
            SPARSE_PREVIOUS,
            SPARSE_PREVIOUS,
            SPARSE_WRITTEN,
        ]
        assert [sample.source_record for sample in full.provenance] == [0, 0, 0, 3]

        window = cdf.varget("quality", startrec=2, endrec=3, return_provenance=True)
        np.testing.assert_array_equal(window.data, [10.0, 40.0])
        # The predecessor record 0 lies outside the requested window, but is
        # still available from the same file.
        assert window.provenance[0].status == SPARSE_PREVIOUS
        assert window.provenance[0].source_record == 0


def test_pad_sparse_explicit_pad_value_stays_written(tmp_path):
    fn = tmp_path / "pad.cdf"
    _write_batch(fn, "pad_sparse", [0, 1, 3], [1.0, -1.0e30, 4.0], pad=-1.0e30, dtype=22)

    with CDF(fn) as cdf:
        result = cdf.varget_provenance("quality")
        np.testing.assert_array_equal(result.data, [1.0, -1.0e30, -1.0e30, 4.0])
        assert [sample.status for sample in result.provenance] == [
            SPARSE_WRITTEN,
            SPARSE_WRITTEN,
            SPARSE_PAD,
            SPARSE_WRITTEN,
        ]
        assert result.provenance[1].source_record == 1
        assert result.provenance[2].source_record is None


def test_previous_sparse_without_predecessor_is_missing(tmp_path):
    fn = tmp_path / "missing.cdf"
    _write_batch(fn, "prev_sparse", [1, 3], [30.0, 40.0], dtype=22)

    with CDF(fn) as cdf:
        result = cdf.varget_provenance("quality")
        assert result.provenance[0].status == SPARSE_MISSING
        assert result.provenance[0].source_record is None
        assert result.provenance[1].status == SPARSE_WRITTEN
        assert result.provenance[2].status == SPARSE_PREVIOUS
        assert result.provenance[2].source_record == 1


def test_reexport_preserves_provenance(tmp_path):
    source = tmp_path / "source.cdf"
    _write_batch(source, "prev_sparse", [0, 3], [10.0, 40.0], dtype=22)
    output = tmp_path / "output.cdf"

    reexport(ingest(source), output)
    with CDF(output) as cdf:
        result = cdf.varget_provenance("quality")
        np.testing.assert_array_equal(result.data, [10.0, 10.0, 10.0, 40.0])
        assert [sample.status for sample in result.provenance] == [
            SPARSE_WRITTEN,
            SPARSE_PREVIOUS,
            SPARSE_PREVIOUS,
            SPARSE_WRITTEN,
        ]


def test_reexport_keeps_real_pad_valued_sample_written(tmp_path):
    source = tmp_path / "source.cdf"
    _write_batch(source, "pad_sparse", [0, 1, 3], [1.0, -1.0e30, 4.0], pad=-1.0e30, dtype=22)
    output = tmp_path / "output.cdf"

    reexport(ingest(source), output)
    with CDF(output) as cdf:
        result = cdf.varget_provenance("quality")
        assert result.provenance[1].status == SPARSE_WRITTEN
        assert result.provenance[2].status == SPARSE_PAD


def test_reexport_keeps_metadata_and_constants(tmp_path):
    source = tmp_path / "source.cdf"
    output = tmp_path / "output.cdf"
    with cdfwrite.CDF(source) as writer:
        writer.write_globalattrs({"Logical_source": {0: "source"}, "PI_name": {0: "team"}})
        writer.write_var(
            {"Variable": "Epoch", "Data_Type": 33, "Num_Elements": 1, "Rec_Vary": True, "Dim_Sizes": []},
            var_data=np.array([10, 20], dtype=np.int64),
        )
        writer.write_var(
            {"Variable": "quality", "Data_Type": 22, "Num_Elements": 1, "Rec_Vary": True, "Dim_Sizes": []},
            var_attrs={"DEPEND_0": "Epoch", "FIELDNAM": "value"},
            var_data=np.array([1.5, 2.5]),
        )
        writer.write_var(
            {"Variable": "meta", "Data_Type": 4, "Num_Elements": 1, "Rec_Vary": False, "Dim_Sizes": []},
            var_data=np.int32(7),
        )

    reexport(ingest(source), output)
    with CDF(output) as cdf:
        assert cdf.globalattsget()["Logical_source"] == ["source"]
        assert cdf.varattsget("quality")["FIELDNAM"] == "value"
        assert cdf.varget("meta") == 7
        np.testing.assert_array_equal(cdf.varget("quality"), [1.5, 2.5])


def test_merge_keeps_duplicate_times_and_does_not_borrow_previous(tmp_path):
    first = tmp_path / "batch_a.cdf"
    second = tmp_path / "batch_b.cdf"
    _write_batch(first, "prev_sparse", [0, 2, 3], [100, 200, 250], pad=-99, dtype=4,
                 epoch=[1000, 2000, 3000, 4000])
    _write_batch(second, "prev_sparse", [1, 3], [300, 400], pad=-99, dtype=4,
                 epoch=[4000, 5000, 6000, 7000])

    merged = merge_batches([first, second])
    quality = merged.variables["quality"]

    # Both batches contribute their records, including the overlapping time.
    assert len(quality) == 8
    np.testing.assert_array_equal(quality.axis, [1000, 2000, 3000, 4000, 4000, 5000, 6000, 7000])

    # Batch B's first record has no predecessor in its own file: it stays
    # missing and must not inherit batch A's trailing value.
    assert quality.samples[4].status == SPARSE_MISSING
    assert quality.samples[4].source_record is None
    assert quality.samples[4].origin_record == 0
    assert quality.samples[5].status == SPARSE_WRITTEN
    assert quality.samples[6].status == SPARSE_PREVIOUS
    assert quality.samples[6].source_record == 1
    assert quality.samples[6].origin == str(second)


def test_merge_variables_keep_their_own_axis(tmp_path):
    first = tmp_path / "axis_a.cdf"
    second = tmp_path / "axis_b.cdf"
    _write_batch(first, "no_sparse", [0, 1, 2, 3], [1.0, 2.0, 3.0, 4.0], dtype=22, epoch=[10, 20, 30, 40])
    _write_batch(second, "no_sparse", [0, 1, 2, 3], [5.0, 6.0, 7.0, 8.0], dtype=22, epoch=[50, 60, 70, 80])

    merged = merge_batches([first, second])
    assert merged.variables["quality"].axis_name == "Epoch"
    np.testing.assert_array_equal(merged.variables["quality"].axis, [10, 20, 30, 40, 50, 60, 70, 80])


def test_merge_reports_nonrecord_conflicts(tmp_path):
    first = tmp_path / "conflict_a.cdf"
    second = tmp_path / "conflict_b.cdf"
    for path, value in ((first, 1), (second, 2)):
        with cdfwrite.CDF(path) as writer:
            writer.write_var(
                {"Variable": "meta", "Data_Type": 4, "Num_Elements": 1, "Rec_Vary": False, "Dim_Sizes": []},
                var_data=np.int32(value),
            )
    with pytest.raises(ValueError, match="meta"):
        merge_batches([first, second])


def test_merge_missing_variable_leaves_coverage_gap(tmp_path):
    first = tmp_path / "gap_a.cdf"
    second = tmp_path / "gap_b.cdf"
    _write_batch(first, "no_sparse", [0, 1], [1.0, 2.0], dtype=22, epoch=[10, 20, 30, 40])
    with cdfwrite.CDF(second) as writer:
        writer.write_var(
            {"Variable": "Epoch", "Data_Type": 33, "Num_Elements": 1, "Rec_Vary": True, "Dim_Sizes": []},
            var_data=np.array([50, 60, 70, 80], dtype=np.int64),
        )
        writer.write_var(
            {"Variable": "other", "Data_Type": 22, "Num_Elements": 1, "Rec_Vary": True, "Dim_Sizes": []},
            var_attrs={"DEPEND_0": "Epoch"},
            var_data=np.array([5.0, 6.0, 7.0, 8.0]),
        )

    merged = merge_batches([first, second])
    quality = merged.variables["quality"]
    assert [sample.status for sample in quality.samples[:2]] == [SPARSE_WRITTEN, SPARSE_WRITTEN]
    # Records 2-3 are not covered by the short first variable, and records
    # 4-7 belong to a batch that has no quality variable at all.
    assert [sample.status for sample in quality.samples[2:]] == [SPARSE_MISSING] * 6
    assert quality.gaps == [(2, 7)]
