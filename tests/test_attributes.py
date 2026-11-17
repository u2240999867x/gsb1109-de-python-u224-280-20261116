from pathlib import Path
from typing import Any, Dict

import numpy as np
import pytest

from cdflib import cdfread, cdfwrite
from cdflib.dataclasses import (
    AttConflict,
    GlobalAttEntry,
    VariableAttEntry,
)

fn = "attribute_catalog.cdf"


def make_reader(tmp_path: Path) -> cdfread.CDF:
    path = tmp_path / fn
    writer = cdfwrite.CDF(path, cdf_spec={"rDim_sizes": [2]})
    # Global attributes: one with a hole in the entry numbers, one entry-less.
    writer.write_globalattrs(
        {
            "G_gap": {0: "a", 2: [10, "cdf_int4"], 4: [12.5, "cdf_double"]},
            "G_empty": None,
            "G_normal": {0: "x"},
        }
    )
    # r variables and z variables, deliberately sharing variable numbers.
    writer.write_var(
        {
            "Variable": "r1",
            "Var_Type": "rvariable",
            "Data_Type": 4,
            "Num_Elements": 1,
            "Rec_Vary": True,
            "Dim_Sizes": [],
            "Dim_Vary": [True],
        },
        var_data=np.array([1, 2]),
    )
    writer.write_var(
        {
            "Variable": "r2",
            "Var_Type": "rvariable",
            "Data_Type": 4,
            "Num_Elements": 1,
            "Rec_Vary": True,
            "Dim_Sizes": [],
            "Dim_Vary": [True],
        },
        var_data=np.array([1, 2]),
    )
    for name in ("z1", "z2"):
        writer.write_var(
            {"Variable": name, "Data_Type": 4, "Num_Elements": 1, "Rec_Vary": True, "Dim_Sizes": []},
            var_data=np.array([1, 2]),
        )
    # VA binds r1 (r chain) and z2 (z chain), which share variable number 0/1.
    writer.write_variableattrs(
        {
            "VA": {"r1": "r-one", "z2": "z-two"},
            "VB": {"z1": [5, "cdf_int2"]},
        }
    )
    writer.close()
    return cdfread.CDF(path)


def test_global_catalog_preserves_entry_numbers_and_gaps(tmp_path):
    reader = make_reader(tmp_path)
    catalog = reader.globalattscatalog()

    assert set(catalog) == {"G_gap", "G_empty", "G_normal"}
    gap = catalog["G_gap"]
    assert [entry.entry_num for entry in gap] == [0, 2, 4]
    assert all(isinstance(entry, GlobalAttEntry) for entry in gap)

    # Original number, type and value are all retained per entry.
    first, second, third = gap
    assert first.data_type == 51 and first.data_type_description == "CDF_CHAR"
    assert first.value == "a"
    assert second.data_type == 4 and second.data_type_description == "CDF_INT4"
    assert second.value == np.int32(10)
    assert third.data_type == 45 and third.data_type_description == "CDF_DOUBLE"

    # An attribute without entries is distinct from a missing attribute.
    assert catalog["G_empty"] == []

    # The hole is real: entry 1 has no AEDR, while entry 2 still resolves.
    with pytest.raises(KeyError):
        reader.attget("G_gap", 1)
    assert reader.attget("G_gap", 2).Data == 10
    inq = reader.attinq("G_gap")
    assert inq.num_gr_entry == 3
    assert inq.max_gr_entry == 4


def test_attcatalog_global_by_name_and_number(tmp_path):
    reader = make_reader(tmp_path)
    by_name = reader.attcatalog("G_gap")
    attr_number = next(i for i, d in enumerate(reader.cdf_info().Attributes) if "G_gap" in d)
    by_number = reader.attcatalog(attr_number)
    assert [e.entry_num for e in by_name] == [e.entry_num for e in by_number] == [0, 2, 4]


def test_variable_catalog_binds_by_name_and_rz_scope(tmp_path):
    reader = make_reader(tmp_path)
    entries, conflicts = reader.attcatalog("VA")
    assert conflicts == []

    bound = {(e.variable, e.variable_scope): e for e in entries}
    r_entry = bound[("r1", "rVariable")]
    z_entry = bound[("z2", "zVariable")]
    # Same underlying variable number, but independent r/z bindings.
    assert r_entry.entry_num == 0 and z_entry.entry_num == 1
    assert r_entry.value == "r-one"
    assert z_entry.value == "z-two"
    assert all(isinstance(e, VariableAttEntry) for e in entries)

    r1_atts = reader.varattscatalog("r1")
    z2_atts = reader.varattscatalog("z2")
    assert r1_atts["VA"].variable_scope == "rVariable"
    assert r1_atts["VA"].value == "r-one"
    assert z2_atts["VA"].variable_scope == "zVariable"
    assert z2_atts["VA"].value == "z-two"
    # Bindings are not joined by matching entry numbers.
    assert "VA" not in reader.varattscatalog("z1")


def test_variable_migration_survives_filtering_and_reordering(tmp_path):
    source = make_reader(tmp_path)
    rebuilt = tmp_path / "rebuilt.cdf"

    # Keep only z variables and reverse their order, so numbers change.
    new_order = ["z2", "z1"]
    writer = cdfwrite.CDF(rebuilt)
    for name in new_order:
        writer.write_var(
            {"Variable": name, "Data_Type": 4, "Num_Elements": 1, "Rec_Vary": True, "Dim_Sizes": []},
            var_data=np.array([1, 2]),
        )

    writer.write_globalatts_catalog(source.globalattscatalog())
    variable_catalog: Dict[str, Any] = {}
    for attr_dict in source.cdf_info().Attributes:
        attr_name = next(iter(attr_dict))
        result = source.attcatalog(attr_name)
        if isinstance(result, tuple):
            variable_catalog[attr_name] = result[0]
    writer.write_variableatts_catalog(variable_catalog)
    writer.close()

    destination = cdfread.CDF(rebuilt)
    # Global entries keep their original (gappy) numbers and values.
    rebuilt_gap = destination.globalattscatalog()["G_gap"]
    assert [e.entry_num for e in rebuilt_gap] == [0, 2, 4]
    assert destination.globalattscatalog()["G_empty"] == []

    # z2 was written first (now variable number 0) but keeps the VA binding.
    assert destination.varinq("z2").Num == 0
    assert destination.attget("VA", "z2").Data == "z-two"
    assert destination.attget("VB", "z1").Data == np.int16(5)
    # Re-querying through the original name-based binding returns that same entry.
    rebound = {e.variable: e.value for e in destination.attcatalog("VA")[0]}
    assert rebound == {"z2": "z-two"}


def test_reader_reports_same_name_rz_collision(tmp_path):
    path = tmp_path / "collision.cdf"
    writer = cdfwrite.CDF(path, cdf_spec={"rDim_sizes": [2]})
    writer.write_var(
        {
            "Variable": "rname",
            "Var_Type": "rvariable",
            "Data_Type": 4,
            "Num_Elements": 1,
            "Rec_Vary": True,
            "Dim_Sizes": [],
            "Dim_Vary": [True],
        },
        var_data=np.array([1, 2]),
    )
    writer.write_var(
        {"Variable": "zname", "Data_Type": 4, "Num_Elements": 1, "Rec_Vary": True, "Dim_Sizes": []},
        var_data=np.array([1, 2]),
    )
    writer.write_variableattrs({"VA": {"rname": "r-value", "zname": "z-value"}})
    writer.close()

    # Force an r and a z variable to share one name (legal at the record level).
    raw = bytearray(path.read_bytes())
    idx = raw.index(b"zname\x00")
    raw[idx : idx + 6] = b"rname\x00"
    path.write_bytes(raw)

    reader = cdfread.CDF(path)
    entries, conflicts = reader.attcatalog("VA")
    assert len(entries) == 2
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert isinstance(conflict, AttConflict)
    assert conflict.target == "rname"
    assert {entry.value for entry in conflict.entries} == {"r-value", "z-value"}


def test_writer_catalog_raises_on_collision(tmp_path):
    path = tmp_path / "writer_collision.cdf"
    writer = cdfwrite.CDF(path)
    writer.write_var(
        {"Variable": "z1", "Data_Type": 4, "Num_Elements": 1, "Rec_Vary": True, "Dim_Sizes": []},
        var_data=np.array([1]),
    )
    colliding = {
        "BAD": [
            VariableAttEntry(0, 51, "CDF_CHAR", 1, "first", "z1", "zVariable"),
            VariableAttEntry(1, 51, "CDF_CHAR", 1, "second", "z1", "zVariable"),
        ]
    }
    with pytest.raises(ValueError, match="Conflicting entries"):
        writer.write_variableatts_catalog(colliding)
    writer.close()


def test_catalog_roundtrip_preserves_types_and_multistrings(tmp_path):
    path = tmp_path / "typed.cdf"
    writer = cdfwrite.CDF(path)
    writer.write_globalattrs({"Multi": {0: "plain", 3: [[1, 2, 3], "cdf_int4"]}})
    writer.write_var(
        {"Variable": "z1", "Data_Type": 4, "Num_Elements": 1, "Rec_Vary": True, "Dim_Sizes": []},
        var_attrs={"LBL": [["alpha", "beta"], "cdf_char"], "VNUM": [[7, 8], "cdf_int4"]},
        var_data=np.array([1]),
    )
    writer.close()

    source = cdfread.CDF(path)
    rebuilt = tmp_path / "typed_rebuilt.cdf"
    writer2 = cdfwrite.CDF(rebuilt)
    writer2.write_var(
        {"Variable": "z1", "Data_Type": 4, "Num_Elements": 1, "Rec_Vary": True, "Dim_Sizes": []},
        var_data=np.array([1]),
    )
    writer2.write_globalatts_catalog(source.globalattscatalog())
    variable_catalog = {}
    for attr_dict in source.cdf_info().Attributes:
        attr_name = next(iter(attr_dict))
        result = source.attcatalog(attr_name)
        if isinstance(result, tuple):
            variable_catalog[attr_name] = result[0]
    writer2.write_variableatts_catalog(variable_catalog)
    writer2.close()

    destination = cdfread.CDF(rebuilt)

    global_entries = destination.globalattscatalog()["Multi"]
    assert [e.entry_num for e in global_entries] == [0, 3]
    assert global_entries[0].data_type_description == "CDF_CHAR"
    assert global_entries[0].value == "plain"
    np.testing.assert_array_equal(global_entries[1].value, np.array([1, 2, 3], dtype=np.int32))

    lbl = destination.varattscatalog("z1")["LBL"]
    assert lbl.data_type_description == "CDF_CHAR"
    assert list(np.atleast_1d(lbl.value)) == ["alpha", "beta"]
    vnum = destination.varattscatalog("z1")["VNUM"]
    np.testing.assert_array_equal(vnum.value, np.array([7, 8], dtype=np.int32))


def test_legacy_globalattsget_still_returns_values(tmp_path):
    reader = make_reader(tmp_path)
    legacy = reader.globalattsget()
    assert legacy["G_normal"] == ["x"]
    # Legacy compact form stays as-is for backward compatibility.
    assert legacy["G_gap"] == ["a", np.int32(10), np.float64(12.5)]
    assert "G_empty" not in legacy


def test_missing_entry_distinct_from_empty_entry(tmp_path):
    path = tmp_path / "distinctions.cdf"
    writer = cdfwrite.CDF(path)
    writer.write_globalattrs(
        {
            # Entry 0/1 do not exist; only entry 2 has an AEDR.
            "G_hole": {2: "present"},
            # Entry 0 exists but holds an empty value.
            "G_empty_value": {0: ""},
            # Attribute exists but has no entries at all.
            "G_no_entries": None,
        }
    )
    writer.close()

    reader = cdfread.CDF(path)
    catalog = reader.globalattscatalog()

    # Missing entry numbers are simply absent; the existing entry keeps its number.
    hole = catalog["G_hole"]
    assert [e.entry_num for e in hole] == [2]
    with pytest.raises(KeyError):
        reader.attget("G_hole", 0)
    assert reader.attget("G_hole", 2).Data == "present"

    # A present-but-empty-valued entry is still a real entry, not a gap.
    empty_value = catalog["G_empty_value"]
    assert len(empty_value) == 1
    assert empty_value[0].entry_num == 0
    assert empty_value[0].value == ""

    # An attribute without any entries is represented by an empty catalog list.
    assert catalog["G_no_entries"] == []
