from pathlib import Path
from typing import Any, Dict

import numpy as np
import pytest

from cdflib import cdfread, cdfwrite
from cdflib.variable_view import (
    COLUMN_MAJOR,
    ROW_MAJOR,
    Observation,
    StructureConflictError,
    VariableView,
    count_measurements,
    decode_physical_offset,
    encode_physical_offset,
    logical_shape,
    physical_shape,
    varying_sizes,
)

fnbasic = "testing.cdf"


def cdf_create(fn: Path, spec: Dict[str, Any]) -> cdfwrite.CDF:
    return cdfwrite.CDF(fn, cdf_spec=spec)


def cdf_read(fn: Path) -> cdfread.CDF:
    return cdfread.CDF(fn)


def test_logical_and_physical_shapes():
    assert logical_shape(True, [2, 3], 4) == (4, 2, 3)
    assert logical_shape(False, [2, 3], 1) == (2, 3)
    assert logical_shape(True, [2, 3], 0) == (0, 2, 3)
    assert physical_shape(True, [2, 3], [True, False], 4) == (4, 2)
    assert physical_shape(False, [2, 3], [False, True], 1) == (3,)
    assert varying_sizes([2, 3, 4], [True, False, True]) == (2, 4)


def test_measurement_counts_only_count_stored_values():
    # 4 records, one non-varying dim of size 3: broadcast slots do not multiply the count
    assert count_measurements(True, [2, 3], [True, False], 4) == 8
    # record-non-varying: a single record is stored
    assert count_measurements(False, [2, 3], [True, True], 1) == 6
    # nothing written: declared shape must not contribute measurements
    assert count_measurements(True, [2, 3], [True, True], 0) == 0
    assert count_measurements(False, [2, 3], [True, True], 0) == 0


def test_row_and_column_physical_offsets():
    sizes = (2, 3)
    assert encode_physical_offset((1, 2), sizes, ROW_MAJOR) == 5
    assert encode_physical_offset((1, 2), sizes, COLUMN_MAJOR) == 5
    assert encode_physical_offset((0, 1), sizes, ROW_MAJOR) == 1
    assert encode_physical_offset((0, 1), sizes, COLUMN_MAJOR) == 2
    for majority in (ROW_MAJOR, COLUMN_MAJOR, 1, 2):
        for index in np.ndindex(sizes):
            offset = encode_physical_offset(index, sizes, majority)
            assert decode_physical_offset(offset, sizes, majority) == index
    with pytest.raises(ValueError):
        decode_physical_offset(6, sizes, ROW_MAJOR)
    with pytest.raises(ValueError):
        encode_physical_offset((2, 0), sizes, ROW_MAJOR)


def test_basic_view_maps_every_position_to_storage():
    data = np.arange(18).reshape(3, 2, 3)
    view = VariableView.from_storage("v", data, True, [True, True], [2, 3], majority=ROW_MAJOR)
    assert view.logical_shape == (3, 2, 3)
    assert view.physical_shape == (3, 2, 3)
    assert view.num_measurements == 18
    assert view.num_logical_positions == 18
    for record, row, col in np.ndindex(data.shape):
        value, observation = view.trace((record, row, col))
        assert value == data[record, row, col]
        assert observation == Observation("v", (record, row, col), record, (row, col), row * 3 + col, False)
        assert view.physical_to_logical(record, row * 3 + col) == (record, row, col)


def test_column_major_view_uses_file_ordering_but_logical_data_order():
    data = np.arange(18).reshape(3, 2, 3)
    view = VariableView.from_storage("v", data, True, [True, True], [2, 3], majority=COLUMN_MAJOR)
    observation = view.logical_to_physical((2, 1, 2))
    # Fortran-style in-record offset for varying index (1, 2) of shape (2, 3)
    assert observation.physical_index == 2 * 2 + 1
    assert observation.record_index == (1, 2)
    assert observation.record == 2
    # values still follow logical axis order
    assert view.value_at((2, 1, 2)) == data[2, 1, 2]


def test_non_varying_dimension_is_broadcast_not_duplicated():
    store = np.arange(9).reshape(3, 3)
    view = VariableView.from_storage("v", store, True, [False, True], [2, 3], majority=ROW_MAJOR)
    assert view.logical_shape == (3, 2, 3)
    assert view.physical_shape == (3, 3)
    assert view.num_measurements == 9
    assert view.num_logical_positions == 18
    first = view.logical_to_physical((1, 0, 2))
    second = view.logical_to_physical((1, 1, 2))
    assert first.physical_index == second.physical_index == 2
    assert first.broadcast and second.broadcast
    np.testing.assert_array_equal(view.data[:, 0, :], view.data[:, 1, :])
    assert view.value_at((1, 1, 2)) == store[1, 2]
    # the broadcast axis does not allocate extra storage: the logical tensor is a view
    assert view.data.flags.owndata is False
    assert np.shares_memory(view.data, store)


def test_record_non_varying_variable():
    tensor = np.array([[7, 8, 9]])
    view = VariableView.from_full_tensor("c", tensor, False, [True], [3])
    assert view.logical_shape == (3,)
    assert view.physical_shape == (3,)
    assert view.num_measurements == 3
    for col in range(3):
        observation = view.logical_to_physical((col,))
        assert observation.record == 0
        assert observation.physical_index == col
        # values are distinct, only the record is shared, so this is not a value broadcast
        assert observation.broadcast is False


def test_record_non_varying_with_non_varying_dimension_broadcasts():
    tensor = np.ones((1, 1, 4))
    view = VariableView.from_full_tensor("c", tensor, False, [False, True], [2, 4])
    assert view.logical_shape == (2, 4)
    assert view.num_measurements == 4
    assert all(observation.broadcast for observation in view.observations())
    assert view.logical_to_physical((0, 1)).physical_index == view.logical_to_physical((1, 1)).physical_index


def test_from_full_tensor_rejects_contradictory_data():
    with pytest.raises(StructureConflictError):
        # declared record-non-varying but two records handed in
        VariableView.from_full_tensor("bad", np.zeros((2, 2)), False, [True], [2])
    with pytest.raises(StructureConflictError):
        # declared non-varying dimension but two values along that axis
        VariableView.from_full_tensor("bad", np.zeros((2, 3)), True, [False], [3])
    with pytest.raises(StructureConflictError):
        # varying dimension size does not match the declaration
        VariableView.from_full_tensor("bad", np.zeros((2, 4)), True, [True], [3])
    with pytest.raises(StructureConflictError):
        # wrong rank
        VariableView.from_full_tensor("bad", np.zeros((2, 3, 4)), True, [True], [3])


def test_from_storage_rejects_storage_shape_conflicts():
    with pytest.raises(StructureConflictError):
        VariableView.from_storage("bad", np.zeros((3, 4)), True, [True], [5])


def test_empty_variable_keeps_declared_logical_shape():
    view = VariableView.from_storage("empty", np.array([]), True, [True, False, True], [2, 3, 4], num_records=0)
    assert view.logical_shape == (0, 2, 3, 4)
    assert view.physical_shape == (0, 2, 4)
    assert view.data.shape == (0, 2, 3, 4)
    assert view.num_measurements == 0
    with pytest.raises(IndexError):
        view.logical_to_physical((0, 0, 0, 0))


def test_slicing_preserves_backtrace():
    data = np.arange(24).reshape(2, 3, 4)
    view = VariableView.from_storage("v", data, True, [True, True], [3, 4])
    sliced = view.isel((slice(None), 1, slice(1, 4)))
    assert sliced.logical_shape == (2, 3)
    for record in range(2):
        for col in range(3):
            value, observation = sliced.trace((record, col))
            assert value == data[record, 1, col + 1]
            assert observation.record == record
            assert observation.record_index == (1, col + 1)
            assert observation.physical_index == 1 * 4 + col + 1
    assert sliced.num_measurements == 6


def test_integer_indexing_all_axes_returns_scalar_view():
    data = np.arange(24).reshape(2, 3, 4)
    view = VariableView.from_storage("v", data, True, [True, True], [3, 4])
    scalar = view[(0, 2, 3)]
    assert scalar.logical_shape == ()
    assert scalar.num_measurements == 1
    value, observation = scalar.trace(())
    assert value == data[0, 2, 3]
    assert observation.record == 0 and observation.record_index == (2, 3)


def test_transpose_and_swapaxes_preserve_backtrace():
    data = np.arange(24).reshape(2, 3, 4)
    view = VariableView.from_storage("v", data, True, [True, True], [3, 4])
    transposed = view.transpose((2, 1, 0))
    assert transposed.logical_shape == (4, 3, 2)
    assert transposed.record_axis == 2
    value, observation = transposed.trace((3, 2, 1))
    assert value == data[1, 2, 3]
    assert observation.record == 1
    assert observation.record_index == (2, 3)

    swapped = view.swapaxes(0, 2)
    assert swapped.logical_shape == (4, 3, 2)
    value, observation = swapped.trace((3, 2, 1))
    assert value == data[1, 2, 3]
    assert observation.record_index == (2, 3)


def test_chained_slice_and_transpose_backtrace():
    data = np.arange(24).reshape(2, 3, 4)
    view = VariableView.from_storage("v", data, True, [True, True], [3, 4])
    chained = view.isel((slice(None), 0, slice(None))).transpose((1, 0))
    assert chained.logical_shape == (4, 2)
    value, observation = chained.trace((3, 1))
    assert value == data[1, 0, 3]
    assert observation.record == 1 and observation.record_index == (0, 3)


def test_slice_after_transpose_keeps_record_axis_consistent():
    data = np.arange(24).reshape(2, 3, 4)
    view = VariableView.from_storage("v", data, True, [True, True], [3, 4])
    moved = view.transpose((2, 1, 0))  # record axis is now last
    sliced = moved.isel((slice(1, 4), 2, slice(None)))
    assert sliced.record_axis == 1
    assert sliced.logical_shape == (3, 2)
    value, observation = sliced.trace((2, 1))
    assert value == data[1, 2, 3]
    assert observation.record == 1 and observation.record_index == (2, 3)

    # integer-indexing the moved record axis removes it entirely
    one_record = moved.isel((slice(None), slice(None), 0))
    assert one_record.record_axis == -1 and one_record.rec_vary is False
    assert one_record.logical_shape == (4, 3)
    value, observation = one_record.trace((3, 2))
    assert value == data[0, 2, 3]
    assert observation.record == 0


def test_index_grid_matches_individual_lookups():
    store = np.arange(9).reshape(3, 3)
    view = VariableView.from_storage("v", store, True, [False, True], [2, 3])
    records, offsets, broadcast = view.index_grid()
    assert records.shape == offsets.shape == broadcast.shape == view.logical_shape
    for position in np.ndindex(view.logical_shape):
        observation = view.logical_to_physical(position)
        assert records[position] == observation.record
        assert offsets[position] == observation.physical_index
        assert bool(broadcast[position]) == observation.broadcast


def test_ellipsis_indexing():
    data = np.arange(24).reshape(2, 3, 4)
    view = VariableView.from_storage("v", data, True, [True, True], [3, 4])
    sliced = view.isel((..., 2))
    assert sliced.logical_shape == (2, 3)
    assert sliced.logical_to_physical((1, 1)).record_index == (1, 2)


def test_dim_names_follow_views():
    data = np.arange(24).reshape(2, 3, 4)
    view = VariableView.from_storage(
        "v", data, True, [True, True], [3, 4], dim_names=["latitude", "longitude"]
    )
    assert view.dim_names == ("record", "latitude", "longitude")
    assert view.transpose((2, 0, 1)).dim_names == ("longitude", "record", "latitude")
    assert view.isel((0, slice(None), slice(None))).dim_names == ("latitude", "longitude")
    with pytest.raises(ValueError):
        VariableView.from_storage("v", data, True, [True, True], [3, 4], dim_names=["only_one"])


def test_cdf_roundtrip_row_major(tmp_path):
    fn = tmp_path / fnbasic
    tfile = cdf_create(fn, {"Majority": "row_major"})
    spec = {
        "Variable": "zvar",
        "Data_Type": 14,
        "Num_Elements": 1,
        "Rec_Vary": True,
        "Dim_Sizes": [2, 3],
        "Compress": 0,
    }
    data = np.arange(12, dtype=np.int32).reshape(2, 2, 3)
    tfile.write_var(spec, var_data=data)
    tfile.close()

    reader = cdf_read(fn)
    info = reader.cdf_info()
    assert info.Majority == "Row_major"
    properties = reader.varinq("zvar")
    raw = reader.varget("zvar")
    view = VariableView.from_variable(properties, raw, majority=info.Majority)
    assert view.logical_shape == (2, 2, 3)
    for position in np.ndindex(data.shape):
        value, observation = view.trace(position)
        assert value == data[position]
        assert observation.record == position[0]
        assert observation.record_index == position[1:]


def test_cdf_roundtrip_column_major(tmp_path):
    fn = tmp_path / fnbasic
    tfile = cdf_create(fn, {"Majority": "column_major"})
    spec = {
        "Variable": "zvar",
        "Data_Type": 14,
        "Num_Elements": 1,
        "Rec_Vary": True,
        "Dim_Sizes": [2, 3],
        "Compress": 0,
    }
    data = np.arange(12, dtype=np.int32).reshape(2, 2, 3)
    # cdfwrite always flattens in C order, so feed per-record Fortran-ordered bytes
    # for a column-major file to land in the intended logical positions.
    f_ordered = np.stack([record.flatten(order="F") for record in data]).reshape(data.shape)
    tfile.write_var(spec, var_data=f_ordered)
    tfile.close()

    reader = cdf_read(fn)
    info = reader.cdf_info()
    assert info.Majority == "Column_major"
    properties = reader.varinq("zvar")
    view = VariableView.from_variable(properties, reader.varget("zvar"), majority=info.Majority)
    # logical values are identical, but the in-record numbering is column-major
    np.testing.assert_array_equal(view.data, data)
    observation = view.logical_to_physical((1, 1, 2))
    assert observation.physical_index == 2 * 2 + 1
    assert observation.record_index == (1, 2)
    sliced = view.isel((1, slice(None), slice(None)))
    assert sliced.logical_to_physical((1, 2)).record == 1


def test_cdf_roundtrip_rvariable_with_non_varying_dim(tmp_path):
    fn = tmp_path / fnbasic
    tfile = cdf_create(fn, {"rDim_sizes": [2, 20], "Majority": "row_major"})
    spec = {
        "Variable": "rvar",
        "Var_Type": "rvariable",
        "Data_Type": 21,
        "Num_Elements": 1,
        "Rec_Vary": True,
        "Dim_Sizes": [],
        "Dim_Vary": [True, False],
    }
    data = np.array([[0, 1], [2, 3], [4, 5]], dtype=np.float32)
    tfile.write_var(spec, var_data=data)
    tfile.close()

    reader = cdf_read(fn)
    info = reader.cdf_info()
    properties = reader.varinq("rvar")
    # The VDR only keeps varying dimension sizes; the full declaration lives in rDim_sizes
    assert tuple(properties.Dim_Sizes) == (2,)
    assert tuple(bool(flag) for flag in properties.Dim_Vary) == (True, False)
    assert tuple(info.rDim_sizes) == (2, 20)
    raw = reader.varget("rvar")
    assert raw.shape == (3, 2)
    view = VariableView.from_variable(
        properties, raw, majority=info.Majority, dim_sizes=info.rDim_sizes
    )
    # the collapsed non-varying axis of size 20 returns in the logical view
    assert view.logical_shape == (3, 2, 20)
    assert view.physical_shape == (3, 2)
    assert view.num_measurements == 6
    assert view.num_logical_positions == 3 * 2 * 20
    shared = view.logical_to_physical((2, 0, 19))
    anchor = view.logical_to_physical((2, 0, 0))
    other_row = view.logical_to_physical((2, 1, 0))
    assert shared.physical_index == anchor.physical_index == 0
    assert other_row.physical_index == 1
    assert shared.broadcast and shared.record == 2
    assert view.value_at((2, 1, 7)) == data[2, 1]


def test_cdf_roundtrip_empty_variable(tmp_path):
    fn = tmp_path / fnbasic
    tfile = cdf_create(fn, {})
    spec = {
        "Variable": "EmptyVar",
        "Data_Type": 8,
        "Num_Elements": 1,
        "Rec_Vary": True,
        "Dim_Sizes": [2, 3],
        "Compress": 0,
    }
    tfile.write_var(spec, var_data=np.array([], dtype=np.int64))
    tfile.close()

    reader = cdf_read(fn)
    properties = reader.varinq("EmptyVar")
    assert properties.Last_Rec == -1
    view = VariableView.from_variable(properties, reader.varget("EmptyVar"), majority=reader.cdf_info().Majority)
    assert view.logical_shape == (0, 2, 3)
    assert view.num_measurements == 0
