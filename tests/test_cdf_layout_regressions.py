"""Layout, majority, sparse-fill, and attribute-binding regressions.

Every expected value here comes from an explicit index formula, not from a
second write/read with the same library. Each element of a record is tagged as
``record * 1000 + index_in_record`` so swapping, dropping, or repeating a value
is directly visible.
"""

import itertools

import numpy as np
import pytest

from cdflib import cdfread, cdfwrite

INT1 = cdfwrite.CDF.CDF_INT1
INT4 = cdfwrite.CDF.CDF_INT4
INT8 = cdfwrite.CDF.CDF_INT8
INT2 = cdfwrite.CDF.CDF_INT2
UINT1 = cdfwrite.CDF.CDF_UINT1
UINT2 = cdfwrite.CDF.CDF_UINT2
UINT4 = cdfwrite.CDF.CDF_UINT4
REAL4 = cdfwrite.CDF.CDF_REAL4
REAL8 = cdfwrite.CDF.CDF_REAL8
EPOCH16 = cdfwrite.CDF.CDF_EPOCH16
CHAR = cdfwrite.CDF.CDF_CHAR

RECORD_TAG = 1000


CDF_TO_NUMPY = {
    INT1: np.int8,
    INT2: np.int16,
    INT4: np.int32,
    INT8: np.int64,
    UINT1: np.uint8,
    UINT2: np.uint16,
    UINT4: np.uint32,
    REAL4: np.float32,
    REAL8: np.float64,
    EPOCH16: np.complex128,
}


def _tag(record: int, index: int) -> int:
    return record * RECORD_TAG + index


def _index_grid(dims):
    """Linear index of every position in a row-major record."""
    return np.arange(int(np.prod(dims)) if dims else 1).reshape(tuple(dims))


def _logical_records(dtype, dims, records):
    """Expected data for consecutive physical records 0..len(records)-1."""
    grid = _index_grid(dims)
    data = np.empty((len(records),) + tuple(dims), dtype=CDF_TO_NUMPY.get(dtype, dtype))
    for row, record in enumerate(records):
        data[row] = np.vectorize(lambda index: _tag(record, int(index)))(grid)
    return data


def _sparse_expected(dtype, dims, physical, sparse, pad):
    """Expected varget output with per-position values and per-type fill."""
    last = int(np.max(physical))
    out = np.empty((last + 1,) + tuple(dims), dtype=CDF_TO_NUMPY.get(dtype, dtype))
    physical_set = {int(record) for record in physical}
    previous = None
    for record in range(last + 1):
        if record in physical_set:
            out[record] = _logical_records(dtype, dims, [record])[0]
            previous = out[record].copy()
        elif sparse == "pad_sparse":
            out[record] = np.full(tuple(dims) if dims else (), pad, dtype=CDF_TO_NUMPY.get(dtype, dtype))
        elif previous is None:
            out[record] = np.full(tuple(dims) if dims else (), pad, dtype=CDF_TO_NUMPY.get(dtype, dtype))
        else:
            out[record] = previous
    return out


def _write(path, spec, var_spec, data, attrs=None):
    with cdfwrite.CDF(path, cdf_spec=spec) as writer:
        writer.write_var(var_spec, var_attrs=attrs, var_data=data)


def _z_spec(name="V", dtype=INT4, dims=(), compress=0, sparse="no_sparse", pad=None, rec_vary=True):
    spec = {
        "Variable": name,
        "Data_Type": dtype,
        "Num_Elements": 1,
        "Rec_Vary": rec_vary,
        "Dim_Sizes": list(dims),
        "Compress": compress,
        "Sparse": sparse,
    }
    if pad is not None:
        spec["Pad"] = pad
    return spec


def _r_spec(name="V", dtype=INT4, dim_vary=(True, True), compress=0, sparse="no_sparse", pad=None, rec_vary=True):
    spec = {
        "Variable": name,
        "Var_Type": "rvariable",
        "Data_Type": dtype,
        "Num_Elements": 1,
        "Rec_Vary": rec_vary,
        "Dim_Vary": list(dim_vary),
        "Compress": compress,
        "Sparse": sparse,
    }
    if pad is not None:
        spec["Pad"] = pad
    return spec


@pytest.mark.parametrize("majority", ["row_major", "column_major"])
@pytest.mark.parametrize("compress", [0, 6])
def test_zvariable_majority_roundtrips_logical_indices(tmp_path, majority, compress):
    dims = (2, 3)
    records = [0, 3, 7]
    expected = _logical_records(np.int32, dims, records)
    path = tmp_path / "z.cdf"
    _write(path, {"Majority": majority}, _z_spec(dims=dims, compress=compress), expected)

    with cdfread.CDF(path) as reader:
        info = reader.cdf_info()
        assert info.Majority == ("Row_major" if majority == "row_major" else "Column_major")
        np.testing.assert_array_equal(reader.varget("V"), expected)
        np.testing.assert_array_equal(reader.varget("V", startrec=1, endrec=2), expected[1:3])


@pytest.mark.parametrize("majority", ["row_major", "column_major"])
@pytest.mark.parametrize("compress", [0, 6])
@pytest.mark.parametrize(
    "dim_vary",
    [(True, True), (True, False), (False, True), (False, False)],
)
def test_rvariable_majority_and_virtual_dims_keep_index_binding(tmp_path, majority, compress, dim_vary):
    rdim_sizes = (2, 3)
    stored = tuple(size for size, vary in zip(rdim_sizes, dim_vary) if vary)
    records = [0, 2, 5]
    expected = _logical_records(np.int32, stored, records)
    path = tmp_path / f"r_{majority}_{dim_vary}.cdf"
    spec = {"Majority": majority, "rDim_sizes": list(rdim_sizes)}
    _write(path, spec, _r_spec(dim_vary=dim_vary, compress=compress), expected)

    with cdfread.CDF(path) as reader:
        info = reader.varinq("V")
        # Dim_Sizes remains index-aligned with the rVariable's dimension variances.
        assert info.Dim_Sizes == list(rdim_sizes)
        assert [vary != 0 for vary in info.Dim_Vary] == list(dim_vary)
        assert info.Num_Dims == len(rdim_sizes)
        np.testing.assert_array_equal(reader.varget("V"), expected)

    # A record range must select the same logical values, not re-derived neighbours.
    with cdfread.CDF(path) as reader:
        np.testing.assert_array_equal(reader.varget("V", startrec=0, endrec=2), expected[0:3])


@pytest.mark.parametrize("majority", ["row_major", "column_major"])
@pytest.mark.parametrize("sparse", ["pad_sparse", "prev_sparse"])
@pytest.mark.parametrize("compress", [0, 6])
def test_sparse_zvariable_fill_uses_whole_record_for_each_element(tmp_path, majority, sparse, compress):
    # Three elements per record make partial-record fill (e.g. fill/zero/fill)
    # impossible to mistake for a correct pad.
    dims = (3,)
    physical = np.array([1, 4, 5])
    pad = -12345
    data = _logical_records(np.int32, dims, physical)
    path = tmp_path / f"sparse_{majority}_{sparse}_{compress}.cdf"
    _write(path, {"Majority": majority}, _z_spec(dims=dims, compress=compress, sparse=sparse, pad=pad), [physical, data])

    expected = _sparse_expected(np.int32, dims, physical, sparse, pad)
    with cdfread.CDF(path) as reader:
        info = reader.varinq("V")
        assert info.Pad is not None and info.Pad.dtype == np.int32 and info.Pad[0] == pad
        np.testing.assert_array_equal(reader.varget("V"), expected)
        np.testing.assert_array_equal(reader.varget("V", startrec=2, endrec=4), expected[2:5])


@pytest.mark.parametrize("majority", ["row_major", "column_major"])
@pytest.mark.parametrize("sparse", ["pad_sparse", "prev_sparse"])
@pytest.mark.parametrize("compress", [0, 6])
def test_sparse_rvariable_writes_support_dim_vary_and_records(tmp_path, majority, sparse, compress):
    rdim_sizes = (2, 3)
    dim_vary = (True, False)
    physical = np.array([1, 3])
    data = _logical_records(np.int32, (2,), physical)
    path = tmp_path / f"r_sparse_{majority}_{sparse}_{compress}.cdf"
    spec = {"Majority": majority, "rDim_sizes": list(rdim_sizes)}
    _write(path, spec, _r_spec(dim_vary=dim_vary, sparse=sparse, compress=compress, pad=-7), [physical, data])

    expected = _sparse_expected(np.int32, (2,), physical, sparse, -7)
    with cdfread.CDF(path) as reader:
        info = reader.varinq("V")
        assert info.Dim_Sizes == list(rdim_sizes)
        assert [vary != 0 for vary in info.Dim_Vary] == list(dim_vary)
        np.testing.assert_array_equal(reader.varget("V"), expected)


@pytest.mark.parametrize("majority", ["row_major", "column_major"])
def test_r_and_z_variable_layouts_are_equivalent(tmp_path, majority):
    """The same conceptual tensor and attrs must survive rVariable vs zVariable."""
    dims = (2, 3)
    values = _logical_records(np.int32, dims, [0, 1, 2])
    attrs = {"SCALE": [5, "CDF_INT4"], "LABEL": "r-or-z"}
    views = []
    for var_type in ("rvariable", "zvariable"):
        path = tmp_path / f"{var_type}_{majority}.cdf"
        spec = {"Majority": majority}
        if var_type == "rvariable":
            spec["rDim_sizes"] = list(dims)
            var_spec = _r_spec(dim_vary=(True, True), compress=6)
        else:
            var_spec = _z_spec(dims=dims, compress=6)
        _write(path, spec, var_spec, values, attrs=attrs)

        with cdfread.CDF(path) as reader:
            info = reader.varinq("V")
            assert info.Var_Type == ("rVariable" if var_type == "rvariable" else "zVariable")
            assert info.Dim_Sizes == list(dims)
            views.append((reader.varget("V"), reader.varattsget("V")))

    np.testing.assert_array_equal(views[0][0], values)
    np.testing.assert_array_equal(views[1][0], views[0][0])
    assert views[1][1] == views[0][1]


@pytest.mark.parametrize("majority", ["row_major", "column_major"])
@pytest.mark.parametrize("sparse", ["pad_sparse", "prev_sparse"])
@pytest.mark.parametrize("compress", [0, 6])
def test_majority_sparse_compression_interaction(tmp_path, majority, sparse, compress):
    dims = (2, 3)
    physical = np.array([0, 2, 4])
    pad = -99
    data = _logical_records(np.int32, dims, physical)
    path = tmp_path / f"interaction_{majority}_{sparse}_{compress}.cdf"
    _write(
        path,
        {"Majority": majority},
        _z_spec(dims=dims, compress=compress, sparse=sparse, pad=pad),
        [physical, data],
    )

    expected = _sparse_expected(np.int32, dims, physical, sparse, pad)
    with cdfread.CDF(path) as reader:
        np.testing.assert_array_equal(reader.varget("V"), expected)
        np.testing.assert_array_equal(reader.varget("V", startrec=1, endrec=3), expected[1:4])


def test_3d_majority_keeps_all_three_indices(tmp_path):
    dims = (2, 3, 2)
    expected = _logical_records(np.int32, dims, [0, 1])
    for majority in ("row_major", "column_major"):
        path = tmp_path / f"three_d_{majority}.cdf"
        _write(path, {"Majority": majority}, _z_spec(dims=dims, compress=6), expected)
        with cdfread.CDF(path) as reader:
            np.testing.assert_array_equal(reader.varget("V"), expected)


def test_sparse_pad_values_keep_their_original_scalar_type(tmp_path):
    cases = [
        (INT2, np.int16(-1234)),
        (REAL4, np.float32(-1.25e30)),
        (REAL8, np.float64(-1.25e300)),
    ]
    for dtype, pad in cases:
        path = tmp_path / f"pad_{dtype}.cdf"
        data = np.array([[11, 12, 13]], dtype=np.dtype(pad.dtype))
        _write(path, {}, _z_spec(dtype=dtype, dims=(3,), compress=6, sparse="pad_sparse", pad=pad), [np.array([1]), data])

        with cdfread.CDF(path) as reader:
            info = reader.varinq("V")
            values = reader.varget("V")
            assert info.Pad.dtype == np.dtype(pad.dtype)
            assert values.dtype == np.dtype(pad.dtype)
            assert info.Pad[0] == pad
            np.testing.assert_array_equal(values[0], np.full(3, pad, dtype=pad.dtype))


def test_char_sparse_pad_is_a_whole_string_not_one_character(tmp_path):
    path = tmp_path / "char.cdf"
    pad = "zzzz"
    data = np.array([["aaaa", "bbbb"]])
    _write(
        path,
        {},
        {
            "Variable": "V",
            "Data_Type": CHAR,
            "Num_Elements": 4,
            "Rec_Vary": True,
            "Dim_Sizes": [2],
            "Sparse": "pad_sparse",
            "Compress": 6,
            "Pad": pad,
        },
        [np.array([1]), data],
    )

    with cdfread.CDF(path) as reader:
        assert reader.varinq("V").Pad == pad
        np.testing.assert_array_equal(reader.varget("V"), np.array([["zzzz", "zzzz"], ["aaaa", "bbbb"]]))


def test_char_sparse_pad_is_truncated_by_bytes_not_characters(tmp_path):
    """PadValue is a fixed-width byte field, so Num_Elements is the byte budget."""
    dims = (1,)
    path = tmp_path / "char_width.cdf"
    data = np.array([["abcd"]])
    # Four UTF-8 bytes fit two é characters, not four.
    pad = "ééé"  # 6 bytes
    _write(
        path,
        {},
        {
            "Variable": "V",
            "Data_Type": CHAR,
            "Num_Elements": 4,
            "Rec_Vary": True,
            "Dim_Sizes": list(dims),
            "Sparse": "pad_sparse",
            "Compress": 0,
            "Pad": pad,
        },
        [np.array([1]), data],
    )

    with cdfread.CDF(path, string_encoding="utf-8") as reader:
        vdr = reader._read_vdr(reader._first_zvariable)
        assert vdr.pad == "éé"  # exactly four bytes
        np.testing.assert_array_equal(reader.varget("V"), np.array([["éé"], ["abcd"]]))


@pytest.mark.parametrize("majority", ["row_major", "column_major"])
def test_attributes_follow_variables_across_layouts(tmp_path, majority):
    dims = (2, 3)
    path = tmp_path / f"attrs_{majority}.cdf"
    expected = {}
    with cdfwrite.CDF(path, cdf_spec={"Majority": majority}) as writer:
        for number, (dtype, var_dims) in enumerate([(INT4, ()), (INT4, dims), (REAL4, (3,))]):
            values = _logical_records(dtype, var_dims, [0, 1, 2])
            attrs = {
                "SCALE": [number + 10, "CDF_INT4"],
                "LABEL": f"variable-{number}",
                "FILLVAL": [-number - 1, "CDF_INT4"],
            }
            writer.write_var(
                _z_spec(name=f"V{number}", dtype=dtype, dims=var_dims, compress=6), var_attrs=attrs, var_data=values
            )
            expected[f"V{number}"] = (values, attrs)

    with cdfread.CDF(path) as reader:
        for name, (values, attrs) in expected.items():
            np.testing.assert_array_equal(reader.varget(name), values)
            read_attrs = reader.varattsget(name)
            assert read_attrs["SCALE"] == attrs["SCALE"][0]
            assert read_attrs["LABEL"] == attrs["LABEL"]
            assert read_attrs["FILLVAL"] == attrs["FILLVAL"][0]
            assert reader.attget("LABEL", entry=name).Data == attrs["LABEL"]


def test_storage_layout_change_keeps_values_and_attribute_bindings(tmp_path):
    """row/column, virtual/physical dims, and sparse/nonsparse are equivalent views."""
    dims = (2, 3)
    values = _logical_records(np.int32, dims, [0, 2, 4])
    layouts = [
        ("row_major", 0, "no_sparse"),
        ("column_major", 0, "no_sparse"),
        ("row_major", 6, "pad_sparse"),
        ("column_major", 9, "pad_sparse"),
    ]
    views = []
    attrs = {"SCALE": [42, "CDF_INT4"], "LABEL": "layout-invariant"}
    physical = np.arange(len(values))
    for index, (majority, compress, sparse) in enumerate(layouts):
        path = tmp_path / f"layout_{index}.cdf"
        writer = cdfwrite.CDF(path, cdf_spec={"Majority": majority})
        try:
            if sparse == "no_sparse":
                var_data = values
            else:
                # Every record is physical, so the logical tensor must be unchanged.
                var_data = [physical, values]
            writer.write_var(
                _z_spec(dims=dims, compress=compress, sparse=sparse, pad=-1), var_attrs=attrs, var_data=var_data
            )
        finally:
            writer.close()

        with cdfread.CDF(path) as reader:
            views.append((reader.varget("V"), reader.varattsget("V")))

    # Every layout yields the same logical tensor and the same attribute payload.
    reference_data, reference_attrs = views[0]
    np.testing.assert_array_equal(reference_data, values)
    for data, read_attrs in views[1:]:
        np.testing.assert_array_equal(data, reference_data)
        assert read_attrs == reference_attrs


def test_failed_layout_combination_shrinks_without_dropping_a_dimension(tmp_path):
    """A minimal column-major 2-D case still reproduces the old misalignment.

    The oracle is a two-row distinct-tag array; removing either dimension would
    reduce it to a 1-D vector where majority cannot matter.
    """
    expected = np.array([[10, 11, 12], [20, 21, 22]], dtype=np.int32)
    path = tmp_path / "minimal.cdf"
    _write(path, {"Majority": "column_major"}, _z_spec(dims=(2, 3), compress=0), expected.reshape(1, 2, 3))

    with cdfread.CDF(path) as reader:
        np.testing.assert_array_equal(reader.varget("V"), expected.reshape(1, 2, 3))


def test_rec_vary_false_returns_one_logical_record(tmp_path):
    dims = (2, 3)
    single = _logical_records(np.int32, dims, [1])  # record tag 1 is intentional
    for majority in ("row_major", "column_major"):
        path = tmp_path / f"single_{majority}.cdf"
        _write(path, {"Majority": majority}, _z_spec(dims=dims, compress=6, rec_vary=False), single)

        with cdfread.CDF(path) as reader:
            info = reader.varinq("V")
            assert info.Rec_Vary is False
            assert info.Last_Rec == 0
            # The record axis disappears for non-record-varying data.
            np.testing.assert_array_equal(reader.varget("V"), single[0])


def _physical_values(path, dtype):
    """Decode the first VVR without using the library's data conversion."""
    with cdfread.CDF(path) as reader:
        vdr = reader._read_vdr(reader._first_zvariable)
        offsets, _starts, _ends = reader._read_vxrs(vdr.head_vxr)
    raw = path.read_bytes()
    block_size = int.from_bytes(raw[offsets[0] : offsets[0] + 8], "big")
    payload = raw[offsets[0] + 12 : offsets[0] + block_size]
    return np.frombuffer(payload, dtype=dtype).tolist()


def test_written_physical_order_follows_majority(tmp_path):
    values = np.array([[10, 11, 12], [20, 21, 22]], dtype=np.int32)
    expected = {
        "row_major": [10, 11, 12, 20, 21, 22],
        "column_major": [10, 20, 11, 21, 12, 22],
    }
    for majority, physical in expected.items():
        path = tmp_path / f"physical_{majority}.cdf"
        _write(path, {"Majority": majority, "Encoding": 6}, _z_spec(dims=(2, 3), compress=0), values.reshape(1, 2, 3))
        assert _physical_values(path, "<i4") == physical


def test_sparse_records_are_grouped_into_contiguous_blocks(tmp_path):
    physical = np.array([1, 4, 5, 8])
    data = _logical_records(np.int32, (2,), physical)
    path = tmp_path / "blocks.cdf"
    _write(path, {}, _z_spec(dims=(2,), compress=6, sparse="pad_sparse", pad=-1), [physical, data])

    with cdfread.CDF(path) as reader:
        vdr = reader._read_vdr(reader._first_zvariable)
        _offsets, starts, ends = reader._read_vxrs(vdr.head_vxr)

    # Blocks are [1,1], [4,5], [8,8] by construction, not by re-reading data.
    assert list(zip(starts, ends)) == [(1, 1), (4, 5), (8, 8)]


def test_sparse_bytes_input_keeps_record_binding(tmp_path):
    """Raw byte buffers must be split at record boundaries, not element boundaries."""
    dims = (2,)
    physical = np.array([1, 3])
    records = _logical_records(np.int32, dims, physical)
    expected = _sparse_expected(np.int32, dims, physical, "pad_sparse", -1)
    for encoding in (1, 6):
        path = tmp_path / f"bytes_{encoding}.cdf"
        _write(
            path,
            {"Encoding": encoding},
            _z_spec(dims=dims, compress=6, sparse="pad_sparse", pad=-1),
            [physical, records.tobytes()],
        )

        with cdfread.CDF(path) as reader:
            np.testing.assert_array_equal(reader.varget("V"), expected)


def test_sparse_bytes_input_with_virtual_records(tmp_path):
    """A byte buffer covering virtual records must still honour physical records."""
    dims = (2,)
    physical = np.array([1, 3])
    full = _logical_records(np.int32, dims, np.arange(4))
    path = tmp_path / "bytes_virtual.cdf"
    _write(
        path,
        {},
        _z_spec(dims=dims, compress=6, sparse="pad_sparse", pad=-1),
        [physical, full.tobytes()],
    )

    expected = _sparse_expected(np.int32, dims, physical, "pad_sparse", -1)
    with cdfread.CDF(path) as reader:
        np.testing.assert_array_equal(reader.varget("V"), expected)


def test_block_lists_do_not_leak_between_variables(tmp_path):
    """_read_vxrs must not accumulate entries across calls (mutable-default bug)."""
    first = np.array([1, 3])
    second = np.array([2])
    path = tmp_path / "two_vars.cdf"
    with cdfwrite.CDF(path) as writer:
        writer.write_var(
            _z_spec(name="A", dims=(2,), compress=6, sparse="pad_sparse", pad=-1),
            var_data=[first, _logical_records(np.int32, (2,), first)],
        )
        writer.write_var(
            _z_spec(name="B", dims=(3,), compress=6, sparse="pad_sparse", pad=-2),
            var_data=[second, _logical_records(np.int32, (3,), second)],
        )

    with cdfread.CDF(path) as reader:
        first_vdr = reader._read_vdr(reader._first_zvariable)
        second_vdr = reader._read_vdr(first_vdr.next_vdr_location)
        _o1, starts_a, ends_a = reader._read_vxrs(first_vdr.head_vxr)
        _o2, starts_b, ends_b = reader._read_vxrs(second_vdr.head_vxr)

        assert list(zip(starts_a, ends_a)) == [(1, 1), (3, 3)]
        assert list(zip(starts_b, ends_b)) == [(2, 2)]
        np.testing.assert_array_equal(reader.varget("A"), _sparse_expected(np.int32, (2,), first, "pad_sparse", -1))
        np.testing.assert_array_equal(reader.varget("B"), _sparse_expected(np.int32, (3,), second, "pad_sparse", -2))


@pytest.mark.parametrize(
    "dtype,pad",
    [
        (INT2, -32767),
        (UINT4, 4294967294),
        (REAL8, -1.0e30),
        (EPOCH16, 0 + 0j),
    ],
)
def test_missing_pad_uses_typed_cdf_default(tmp_path, dtype, pad):
    """When a VDR has no PadValue, CDF's per-type default must be used verbatim."""
    dims = (2,)
    path = tmp_path / f"no_pad_{dtype}.cdf"
    data = _logical_records(dtype, dims, [1])
    _write(path, {}, _z_spec(dtype=dtype, dims=dims, compress=0, sparse="pad_sparse"), [np.array([1]), data])

    # Clear the VDR's "pad value specified" flag (bit 1 of its Flags field).
    with cdfread.CDF(path) as reader:
        vdr_offset = reader._first_zvariable
    raw = bytearray(path.read_bytes())
    flags = int.from_bytes(raw[vdr_offset + 44 : vdr_offset + 48], "big")
    raw[vdr_offset + 44 : vdr_offset + 48] = (flags & ~2).to_bytes(4, "big")
    path.write_bytes(raw)

    with cdfread.CDF(path) as reader:
        assert reader.varinq("V").Pad is None
        values = reader.varget("V")
        np.testing.assert_array_equal(values[0], np.full(dims, pad, dtype=values.dtype))
        np.testing.assert_array_equal(values[1], data[0])


@pytest.mark.parametrize("encoding", [1, 6])
@pytest.mark.parametrize(
    "dtype,pad",
    [
        (INT2, np.int16(-1234)),
        (INT4, np.int32(-123456)),
        (REAL8, np.float64(-1.25e300)),
        (EPOCH16, np.complex128(complex(11, 12))),
    ],
)
def test_sparse_fill_respects_file_encoding(tmp_path, encoding, dtype, pad):
    dims = (2,)
    path = tmp_path / f"encoding_{encoding}_{dtype}.cdf"
    data = _logical_records(dtype, dims, [1])
    _write(
        path,
        {"Encoding": encoding},
        _z_spec(dtype=dtype, dims=dims, compress=6, sparse="pad_sparse", pad=pad),
        [np.array([1]), data],
    )

    with cdfread.CDF(path) as reader:
        info = reader.varinq("V")
        values = reader.varget("V")
        assert info.Pad.dtype == np.dtype(pad.dtype)
        assert info.Pad[0] == pad
        np.testing.assert_array_equal(values[0], np.full(dims, pad, dtype=pad.dtype))
        np.testing.assert_array_equal(values[1], data[0])


def _tagged_values(dims, record):
    return np.vectorize(lambda i: _tag(record, int(i)))(_index_grid(dims))


def test_smallest_failure_preserves_swapped_positions():
    """Guard the shrink rule: 2x2 already detects every row/column transposition."""
    dims = (2, 2)
    values = _tagged_values(dims, 1)
    transposed = np.transpose(values)
    positions = [tuple(p) for p in itertools.product(*[range(size) for size in dims])]
    wrong = [p for p in positions if values[p] != transposed[p]]
    assert wrong == [(0, 1), (1, 0)]


def test_minimal_2x2_column_major_keeps_both_dimensions(tmp_path):
    """The smallest sample that still has a majority error needs two real dims.

    Dropping either dimension collapses majority to a no-op, so this test writes
    an actual 2x2 record and checks both the logical view and the physical order.
    """
    values = np.array([[10, 11], [20, 21]], dtype=np.int32)
    path = tmp_path / "minimal_2x2.cdf"
    _write(path, {"Majority": "column_major", "Encoding": 6}, _z_spec(dims=(2, 2), compress=0), values.reshape(1, 2, 2))

    with cdfread.CDF(path) as reader:
        np.testing.assert_array_equal(reader.varget("V"), values.reshape(1, 2, 2))
    # Column majority stores the first dimension fastest: [0,0], [1,0], [0,1], [1,1].
    assert _physical_values(path, "<i4") == [10, 20, 11, 21]
