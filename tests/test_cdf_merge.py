import json
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pytest

from cdflib import cdfwrite
from cdflib.cdfread import CDF
from cdflib.epochs import CDFepoch
from cdflib.xarray import merge_cdf_batches


def _tt2000(breakdowns: List[List[int]]) -> np.ndarray:
    return CDFepoch.compute_tt2000(breakdowns).astype(np.int64)


def _scalar_spec(
    name: str,
    data_type: int,
    rec_vary: bool = True,
    sparse: Optional[str] = None,
    pad: Optional[np.ndarray] = None,
) -> Dict[str, Any]:
    spec: Dict[str, Any] = {
        "Variable": name,
        "Data_Type": data_type,
        "Num_Elements": 1,
        "Rec_Vary": rec_vary,
        "Dim_Sizes": [],
    }
    if sparse is not None:
        spec["Sparse"] = sparse
    if pad is not None:
        spec["Pad"] = pad
    return spec


def _entry(spec: Dict[str, Any], data: Any, attrs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"spec": spec, "attrs": attrs, "data": data}


def _write_batch(path: Path, variables: List[Dict[str, Any]]) -> None:
    with cdfwrite.CDF(path, cdf_spec={"rDim_sizes": [1]}) as cdf:
        for variable in variables:
            cdf.write_var(variable["spec"], var_attrs=variable["attrs"], var_data=variable["data"])


def _two_batches(tmp_path: Path):
    epoch_a = _tt2000(
        [
            [2020, 1, 1, 0, 0, 0, 0],
            [2020, 1, 1, 0, 0, 1, 0],
            [2020, 1, 1, 0, 0, 2, 0],
            [2020, 1, 1, 0, 0, 3, 0],
        ]
    )
    # The first timestamp of batch B duplicates the last timestamp of batch A.
    epoch_b = _tt2000(
        [
            [2020, 1, 1, 0, 0, 3, 0],
            [2020, 1, 1, 0, 0, 4, 0],
            [2020, 1, 1, 0, 0, 5, 0],
            [2020, 1, 1, 0, 0, 6, 0],
        ]
    )
    epoch2_a = _tt2000(
        [
            [2020, 1, 2, 0, 0, 0, 0],
            [2020, 1, 2, 1, 0, 0, 0],
        ]
    )

    batch_a = tmp_path / "batch_a.cdf"
    _write_batch(
        batch_a,
        [
            _entry(
                _scalar_spec("Epoch", cdfwrite.CDF.CDF_TIME_TT2000),
                epoch_a,
            ),
            _entry(
                _scalar_spec("temp", cdfwrite.CDF.CDF_DOUBLE),
                np.array([10.0, 11.0, 12.0, 13.0]),
                {"DEPEND_0": "Epoch"},
            ),
            _entry(
                _scalar_spec(
                    "quality",
                    cdfwrite.CDF.CDF_INT4,
                    sparse="prev_sparse",
                    pad=np.array([-99]),
                ),
                [np.array([0, 2, 3]), np.array([100, 200, 250], dtype=np.int32)],
                {"DEPEND_0": "Epoch"},
            ),
            _entry(
                _scalar_spec("partial", cdfwrite.CDF.CDF_DOUBLE),
                # Three records although Epoch has four: an in-batch coverage gap.
                np.array([1.0, 2.0, 3.0]),
                {"DEPEND_0": "Epoch"},
            ),
            _entry(
                _scalar_spec("Epoch2", cdfwrite.CDF.CDF_TIME_TT2000),
                epoch2_a,
            ),
            _entry(
                _scalar_spec("slow", cdfwrite.CDF.CDF_DOUBLE),
                np.array([5.5, 6.5]),
                {"DEPEND_0": "Epoch2"},
            ),
            _entry(
                _scalar_spec("meta_const", cdfwrite.CDF.CDF_INT4, rec_vary=False),
                np.array([7]),
            ),
            _entry(
                _scalar_spec("meta_conflict", cdfwrite.CDF.CDF_INT4, rec_vary=False),
                np.array([1]),
            ),
        ],
    )

    batch_b = tmp_path / "batch_b.cdf"
    _write_batch(
        batch_b,
        [
            _entry(
                _scalar_spec("Epoch", cdfwrite.CDF.CDF_TIME_TT2000),
                epoch_b,
            ),
            _entry(
                _scalar_spec("temp", cdfwrite.CDF.CDF_DOUBLE),
                np.array([14.0, 15.0, 16.0, 17.0]),
                {"DEPEND_0": "Epoch"},
            ),
            _entry(
                _scalar_spec(
                    "quality",
                    cdfwrite.CDF.CDF_INT4,
                    sparse="prev_sparse",
                    pad=np.array([-99]),
                ),
                # Records 0 and 2 are virtual.  Record 0 stays padded inside this
                # file instead of inheriting the last value of batch A.
                [np.array([1, 3]), np.array([300, 400], dtype=np.int32)],
                {"DEPEND_0": "Epoch"},
            ),
            _entry(
                _scalar_spec("extra_var", cdfwrite.CDF.CDF_DOUBLE),
                np.array([90.0, 91.0, 92.0, 93.0]),
                {"DEPEND_0": "Epoch"},
            ),
            _entry(
                _scalar_spec("meta_const", cdfwrite.CDF.CDF_INT4, rec_vary=False),
                np.array([7]),
            ),
            _entry(
                _scalar_spec("meta_conflict", cdfwrite.CDF.CDF_INT4, rec_vary=False),
                np.array([2]),
            ),
        ],
    )
    return batch_a, batch_b, epoch_a, epoch_b


def test_merge_keeps_duplicate_times_and_explicit_identity(tmp_path):
    batch_a, batch_b, epoch_a, epoch_b = _two_batches(tmp_path)

    with pytest.warns(UserWarning, match="meta_conflict"):
        dataset = merge_cdf_batches([batch_a, batch_b])

    # No de-duplication: the overlapping timestamp 00:00:03 survives twice, in batch order.
    np.testing.assert_array_equal(dataset["Epoch"].values, np.concatenate([epoch_a, epoch_b]))
    assert dataset.sizes["Epoch"] == 8

    np.testing.assert_array_equal(
        dataset["Epoch__source_batch"].values,
        np.array(["0", "0", "0", "0", "1", "1", "1", "1"], dtype=object),
    )
    np.testing.assert_array_equal(
        dataset["Epoch__source_record"].values,
        np.array([0, 1, 2, 3, 0, 1, 2, 3], dtype=np.int64),
    )

    # Every merged sample can be traced back, and values are concatenated without interpolation.
    np.testing.assert_array_equal(dataset["temp"].values, np.arange(10, 18, dtype=float))
    assert dataset["temp"].dims == ("Epoch",)


def test_previous_sparse_does_not_cross_file_boundary(tmp_path):
    batch_a, batch_b, _, _ = _two_batches(tmp_path)

    # Sanity check on the raw file: batch B record 0 is padded, not filled from batch A.
    with CDF(batch_b) as cdf:
        assert cdf.varget("quality")[0] == -99

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        dataset = merge_cdf_batches([batch_a, batch_b])

    np.testing.assert_array_equal(
        dataset["quality"].values,
        np.array([100, 100, 200, 250, -99, 300, 300, 400], dtype=np.int32),
    )
    np.testing.assert_array_equal(
        dataset["quality__physical"].values,
        np.array([True, False, True, True, False, True, False, True]),
    )
    assert "Prev_sparse" in dataset["quality__physical"].attrs["CDF_SPARSE"]


def test_in_batch_coverage_gap_is_marked(tmp_path):
    batch_a, batch_b, _, _ = _two_batches(tmp_path)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        dataset = merge_cdf_batches([batch_a, batch_b])

    # partial has three values in batch A and none at all in batch B.
    assert dataset.sizes["partial__record"] == 3
    np.testing.assert_array_equal(dataset["partial"].values, np.array([1.0, 2.0, 3.0]))
    np.testing.assert_array_equal(
        dataset["partial__coverage"].values,
        np.array([True, True, True, False, False, False, False, False]),
    )


def test_variables_keep_their_own_timelines(tmp_path):
    batch_a, batch_b, _, _ = _two_batches(tmp_path)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        dataset = merge_cdf_batches([batch_a, batch_b])

    assert set(dataset.dims) >= {"Epoch", "Epoch2"}
    assert dataset.sizes["Epoch2"] == 2
    assert dataset["slow"].dims == ("Epoch2",)
    np.testing.assert_array_equal(dataset["slow"].values, np.array([5.5, 6.5]))
    np.testing.assert_array_equal(
        dataset["Epoch2__source_batch"].values,
        np.array(["0", "0"], dtype=object),
    )


def test_missing_variable_leaves_coverage_gap(tmp_path):
    batch_a, batch_b, _, _ = _two_batches(tmp_path)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        dataset = merge_cdf_batches([batch_a, batch_b])

    # extra_var exists only in batch B: real samples only, plus a coverage flag.
    assert dataset.sizes["extra_var__record"] == 4
    np.testing.assert_array_equal(dataset["extra_var"].values, np.arange(90, 94, dtype=float))
    np.testing.assert_array_equal(
        dataset["extra_var__coverage"].values,
        np.array([False, False, False, False, True, True, True, True]),
    )
    np.testing.assert_array_equal(
        dataset["extra_var__record__source_batch"].values,
        np.array(["1", "1", "1", "1"], dtype=object),
    )

    coverage = json.loads(dataset.attrs["CDF_MERGE_BATCHES"])
    batch_zero_vars = coverage[0]["variables"]
    batch_one_vars = coverage[1]["variables"]
    assert "slow" in batch_zero_vars and "slow" not in batch_one_vars
    assert "extra_var" in batch_one_vars and "extra_var" not in batch_zero_vars


def test_nonrecord_conflict_is_versioned_not_reused(tmp_path):
    batch_a, batch_b, _, _ = _two_batches(tmp_path)

    with pytest.warns(UserWarning, match="meta_conflict"):
        dataset = merge_cdf_batches([batch_a, batch_b])

    assert "meta_conflict" not in dataset.data_vars
    assert int(dataset["meta_conflict__batch0"].values) == 1
    assert int(dataset["meta_conflict__batch1"].values) == 2
    assert dataset["meta_conflict__batch0"].attrs["SOURCE_BATCH_LABEL"] == "0"
    assert dataset["meta_conflict__batch1"].attrs["SOURCE_BATCH_LABEL"] == "1"

    # Equal non-record-varying values stay a single variable.
    assert "meta_const" in dataset.data_vars
    assert int(dataset["meta_const"].values) == 7

    with pytest.raises(ValueError, match="meta_conflict"):
        merge_cdf_batches([batch_a, batch_b], on_nonrecord_conflict="raise")


def test_mixed_record_variance_is_rejected(tmp_path):
    epoch = _tt2000([[2020, 1, 1, 0, 0, 0, 0], [2020, 1, 1, 0, 0, 1, 0]])

    batch_a = tmp_path / "mixed_a.cdf"
    _write_batch(
        batch_a,
        [
            _entry(_scalar_spec("Epoch", cdfwrite.CDF.CDF_TIME_TT2000), epoch),
            _entry(
                _scalar_spec("mixed", cdfwrite.CDF.CDF_INT4),
                np.array([1, 2], dtype=np.int32),
                {"DEPEND_0": "Epoch"},
            ),
        ],
    )
    batch_b = tmp_path / "mixed_b.cdf"
    _write_batch(
        batch_b,
        [
            _entry(_scalar_spec("Epoch", cdfwrite.CDF.CDF_TIME_TT2000), epoch),
            _entry(
                _scalar_spec("mixed", cdfwrite.CDF.CDF_INT4, rec_vary=False),
                np.array([3], dtype=np.int32),
            ),
        ],
    )

    with pytest.raises(ValueError, match="record-varying"):
        merge_cdf_batches([batch_a, batch_b])


def test_input_validation(tmp_path):
    batch_a, batch_b, _, _ = _two_batches(tmp_path)

    with pytest.raises(ValueError, match="At least two"):
        merge_cdf_batches([batch_a])
    with pytest.raises(ValueError, match="unique"):
        merge_cdf_batches([batch_a, batch_b], batch_labels=["x", "x"])
    with pytest.raises(ValueError, match="same length"):
        merge_cdf_batches([batch_a, batch_b], batch_labels=["x"])
    with pytest.raises(ValueError, match="on_nonrecord_conflict"):
        merge_cdf_batches([batch_a, batch_b], on_nonrecord_conflict="keep_first")
