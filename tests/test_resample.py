"""Tests for the role-aware time-grid resampling in cdflib.resample."""

from pathlib import Path
from typing import Any, Dict

import numpy as np
import pytest

from cdflib import cdfread, cdfwrite
from cdflib.epochs import CDFepoch
from cdflib.resample import (
    ROLE_ACCUMULATION,
    ROLE_INSTANT,
    ROLE_STATUS,
    ROLE_RULES,
    RULE_ALLOCATE,
    RULE_FORWARD_FILL,
    RULE_LINEAR,
    RULE_MEAN,
    RULE_SAMPLE,
    VariableSpec,
    resample_cdf,
    resample_variable,
)

TIMES = np.array(
    [
        "2020-01-01T00:00",
        "2020-01-01T00:10",
        "2020-01-01T00:20",
        "2020-01-01T00:30",
        "2020-01-01T01:00",
        "2020-01-01T01:10",
        "2020-01-01T01:20",
        "2020-01-01T01:30",
    ],
    dtype="datetime64[m]",
)
# Half-hour grid; bins are 30 minutes wide centered on the grid points.
GRID = np.array(
    ["2020-01-01T00:00", "2020-01-01T00:30", "2020-01-01T01:00", "2020-01-01T01:30"],
    dtype="datetime64[m]",
)


# ---------------------------------------------------------------------------
# Specification validation
# ---------------------------------------------------------------------------


def test_allowed_rules_match_roles():
    assert ROLE_RULES[ROLE_STATUS] == (RULE_FORWARD_FILL, RULE_SAMPLE)
    assert RULE_MEAN in ROLE_RULES[ROLE_INSTANT]
    assert ROLE_RULES[ROLE_ACCUMULATION] == (RULE_ALLOCATE, )


def test_invalid_role_rejected():
    with pytest.raises(ValueError):
        VariableSpec("x", "telepathy", RULE_MEAN)


def test_rule_mismatch_rejected():
    with pytest.raises(ValueError):
        VariableSpec("x", ROLE_STATUS, RULE_LINEAR)
    with pytest.raises(ValueError):
        VariableSpec("x", ROLE_ACCUMULATION, RULE_MEAN)


def test_reset_times_only_for_accumulation():
    with pytest.raises(ValueError):
        VariableSpec("x", ROLE_INSTANT, RULE_MEAN, reset_times=GRID)


def test_bad_grid_and_lengths_rejected():
    spec = VariableSpec("v", ROLE_INSTANT, RULE_MEAN)
    with pytest.raises(ValueError):
        resample_variable(np.arange(3.0), TIMES[:3], GRID[:1], spec)
    with pytest.raises(ValueError):
        resample_variable(np.arange(3.0), TIMES[:2], GRID, spec)
    with pytest.raises(ValueError):
        resample_variable(np.arange(4.0), TIMES[:4], GRID[::-1], spec)


def test_integer_variable_requires_fill_value():
    spec = VariableSpec("s", ROLE_STATUS, RULE_FORWARD_FILL)
    with pytest.raises(ValueError):
        resample_variable(np.arange(8, dtype=np.int32), TIMES, GRID, spec)


# ---------------------------------------------------------------------------
# Status variables
# ---------------------------------------------------------------------------


def test_status_forward_fill_never_invents_states():
    status = np.array([1, 1, 2, 2, 3, 3, 3, 9], dtype=np.int32)
    result = resample_variable(
        status, TIMES, GRID, VariableSpec("s", ROLE_STATUS, RULE_FORWARD_FILL, fill_value=-1)
    )
    # Every emitted value must be one of the observed codes.
    assert set(np.unique(result.values)).issubset({1, 2, 3, 9, -1})
    # Forward fill carries the last observed state of each bin forward.
    np.testing.assert_array_equal(result.values, [1, 2, 3, 9])
    # Provenance points back at the original records.
    assert list(result.samples_used[1]) == [2, 3]
    assert result.values[1] == status[result.samples_used[1][-1]]


def test_status_forward_fill_carries_state_into_eventless_bins():
    times = np.array(["2020-01-01T00:00", "2020-01-01T01:00"], dtype="datetime64[m]")
    status = np.array([5, 8], dtype=np.int32)
    result = resample_variable(
        status, times, GRID, VariableSpec("s", ROLE_STATUS, RULE_FORWARD_FILL, fill_value=-1)
    )
    # Bin [00:15, 00:45) has no event; the state observed at 00:00 is carried
    # forward because the covered segment [00:00, 01:00) reaches into the bin.
    assert result.values[1] == 5
    assert list(result.samples_used[1]) == [0]
    # After the last sample nothing is covered, so no state is fabricated.
    assert result.values[3] == -1
    assert list(result.samples_used[3]) == []


def test_status_sample_picks_first_observed_code():
    status = np.array([1, 1, 2, 2, 3, 3, 3, 9], dtype=np.int32)
    result = resample_variable(
        status, TIMES, GRID, VariableSpec("s", ROLE_STATUS, RULE_SAMPLE, fill_value=-1)
    )
    np.testing.assert_array_equal(result.values, [1, 2, 3, 3])
    assert list(result.samples_used[0]) == [0, 1]


def test_status_uncovered_bins_are_filled():
    times = np.array(["2020-01-01T00:00", "2020-01-01T00:10"], dtype="datetime64[m]")
    status = np.array([7, 7], dtype=np.int32)
    result = resample_variable(
        status, times, GRID, VariableSpec("s", ROLE_STATUS, RULE_FORWARD_FILL, fill_value=-99)
    )
    np.testing.assert_array_equal(result.values, [7, -99, -99, -99])
    assert result.coverage[0] == pytest.approx(1 / 3)
    assert result.coverage[1:] == pytest.approx([0.0, 0.0, 0.0])


# ---------------------------------------------------------------------------
# Instantaneous continuous variables
# ---------------------------------------------------------------------------


def test_instant_mean_is_time_weighted():
    values = np.array([0.0, 10.0, 20.0, 30.0, 60.0, 70.0, 80.0, 90.0])
    result = resample_variable(values, TIMES, GRID, VariableSpec("v", ROLE_INSTANT, RULE_MEAN))
    # Bin 2 (00:15-00:45): value 10 for 5 min, 20 for 10 min, 30 for 15 min.
    expected = (10 * 5 + 20 * 10 + 30 * 15) / 30
    assert result.values[1] == pytest.approx(expected)
    assert result.values.dtype == np.float64
    # The pre-edge sample (index 1, value 10) participates via provenance.
    assert 1 in result.samples_used[1]
    assert 3 in result.samples_used[1]


def test_instant_linear_interpolates_at_bin_centers():
    times = np.array(
        ["2020-01-01T00:00", "2020-01-01T00:20", "2020-01-01T00:40"], dtype="datetime64[m]"
    )
    values = np.array([0.0, 20.0, 40.0])
    grid = np.array(["2020-01-01T00:05", "2020-01-01T00:25"], dtype="datetime64[m]")
    result = resample_variable(values, times, grid, VariableSpec("v", ROLE_INSTANT, RULE_LINEAR))
    np.testing.assert_allclose(result.values, [5.0, 25.0])
    assert list(result.samples_used[0]) == [0, 1]
    assert list(result.samples_used[1]) == [1, 2]


def test_instant_linear_does_not_cross_gap():
    values = np.array([0.0, 10.0, 20.0, np.nan, 60.0, 70.0, 80.0, 90.0])
    result = resample_variable(values, TIMES, GRID, VariableSpec("v", ROLE_INSTANT, RULE_LINEAR))
    # Bin centered on 00:30 sits across the invalid record and stays uncovered.
    assert np.isnan(result.values[1])
    # The center exactly matching a valid sample still resolves.
    assert result.values[0] == 0.0
    assert result.values[2] == 60.0


def test_instant_sample_rule():
    values = np.array([0.0, 10.0, 20.0, 30.0, 60.0, 70.0, 80.0, 90.0])
    result = resample_variable(values, TIMES, GRID, VariableSpec("v", ROLE_INSTANT, RULE_SAMPLE))
    np.testing.assert_array_equal(result.values[[0, 1, 2]], [0.0, 20.0, 60.0])


def test_coverage_and_uncovered_intervals():
    values = np.array([0.0, 10.0, 20.0, np.nan, 60.0, 70.0, 80.0, 90.0])
    result = resample_variable(values, TIMES, GRID, VariableSpec("v", ROLE_INSTANT, RULE_MEAN))
    # The invalid record at 00:30 splits coverage; each middle bin is half covered.
    np.testing.assert_allclose(result.coverage, [0.5, 0.5, 0.5, 0.5])
    # The hole [00:30, 01:00) is reported as bin-clipped uncovered pairs whose
    # union covers the full gap.
    flat = result.uncovered.astype("datetime64[m]")
    gap_start = np.datetime64("2020-01-01T00:30")
    gap_end = np.datetime64("2020-01-01T01:00")
    gap_pieces = flat[(flat[:, 0] >= gap_start) & (flat[:, 1] <= gap_end)]
    assert gap_pieces[0, 0] == gap_start
    assert gap_pieces[-1, 1] == gap_end
    assert np.all(gap_pieces[:, 1] > gap_pieces[:, 0])
    # Covered segments are the two valid runs.
    assert result.covered_segments.shape == (2, 2)


# ---------------------------------------------------------------------------
# Accumulation variables
# ---------------------------------------------------------------------------


def test_accumulation_conserves_increment_across_bins():
    counter = np.array([0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0])
    result = resample_variable(
        counter, TIMES, GRID, VariableSpec("c", ROLE_ACCUMULATION, RULE_ALLOCATE)
    )
    # Total increment 70 is conserved across the populated bins.
    assert np.nansum(result.values) == pytest.approx(70.0)
    # Bins run between grid midpoints (00:15, 00:45, 01:15); increments
    # crossing a midpoint are split in proportion to the overlap.
    np.testing.assert_allclose(result.values, [15.0, 20.0, 20.0, 15.0])
    # Provenance records both endpoints of each contributing interval.
    assert {0, 1, 2}.issubset(set(result.samples_used[0]))


def test_accumulation_does_not_carry_mass_across_explicit_reset():
    times = np.array(
        ["2020-01-01T00:00", "2020-01-01T00:30", "2020-01-01T01:00", "2020-01-01T01:30"],
        dtype="datetime64[m]",
    )
    counter = np.array([0.0, 30.0, 10.0, 40.0])
    result = resample_variable(
        counter,
        times,
        GRID,
        VariableSpec(
            "c",
            ROLE_ACCUMULATION,
            RULE_ALLOCATE,
            reset_times=np.array(["2020-01-01T00:45"], dtype="datetime64[m]"),
        ),
    )
    # 30 accumulated before the reset, 30 after; nothing crosses it.
    assert np.nansum(result.values) == pytest.approx(60.0)
    # No bin mixes records from both sides of the reset.
    assert not (set(result.samples_used[1]) & {0, 1} and set(result.samples_used[1]) & {2, 3})


def test_accumulation_gap_is_treated_as_reset_evidence():
    # A jump backwards with no explicit reset: the invalid record breaks the run.
    counter = np.array([0.0, 30.0, np.nan, 10.0, 25.0, 40.0, 55.0, 70.0])
    result = resample_variable(
        counter, TIMES, GRID, VariableSpec("c", ROLE_ACCUMULATION, RULE_ALLOCATE)
    )
    # The negative increment across the gap is never subtracted from any bin.
    assert np.all(result.values[~np.isnan(result.values)] >= 0.0)
    # Increments on either side remain conserved: 30 + 60 = 90.
    assert np.nansum(result.values) == pytest.approx(90.0)


def test_accumulation_uncovered_bins_are_filled():
    times = np.array(["2020-01-01T00:00", "2020-01-01T00:10"], dtype="datetime64[m]")
    counter = np.array([0.0, 10.0])
    result = resample_variable(
        counter, times, GRID, VariableSpec("c", ROLE_ACCUMULATION, RULE_ALLOCATE)
    )
    assert np.isnan(result.values[1])
    assert np.isnan(result.values[2])
    assert result.coverage[2] == 0.0


# ---------------------------------------------------------------------------
# Fill values and time-type preservation
# ---------------------------------------------------------------------------


def test_explicit_fill_value_is_preserved():
    values = np.array([1, 1, 2, 2, 3, 3, 3, 9], dtype=np.int32)
    result = resample_variable(
        values, TIMES, GRID, VariableSpec("s", ROLE_STATUS, RULE_SAMPLE, fill_value=-128)
    )
    assert result.fill_value == -128
    assert result.values.dtype == np.int32
    assert result.values[-1] == -128 or result.values[-1] in (3, 9)


def test_unix_seconds_round_trip():
    unix_times = (TIMES - np.datetime64("1970-01-01T00:00:00")).astype("timedelta64[s]").astype(np.float64)
    unix_grid = (GRID - np.datetime64("1970-01-01T00:00:00")).astype("timedelta64[s]").astype(np.float64)
    result = resample_variable(
        np.arange(8.0), unix_times, unix_grid, VariableSpec("v", ROLE_INSTANT, RULE_MEAN)
    )
    assert result.target_time.dtype == np.float64
    np.testing.assert_allclose(result.target_time, unix_grid)


@pytest.mark.parametrize(
    "convention, caster, dtype_kind",
    [
        ("epoch", CDFepoch.timestamp_to_cdfepoch, "f"),
        ("epoch16", CDFepoch.timestamp_to_cdfepoch16, "c"),
        ("tt2000", CDFepoch.timestamp_to_tt2000, "i"),
    ],
)
def test_cdf_epoch_time_types_are_preserved(convention, caster, dtype_kind):
    unix_times = (TIMES - np.datetime64("1970-01-01T00:00:00")).astype("timedelta64[s]").astype(np.float64)
    unix_grid = (GRID - np.datetime64("1970-01-01T00:00:00")).astype("timedelta64[s]").astype(np.float64)
    epoch_times = np.asarray(caster(unix_times))
    epoch_grid = np.asarray(caster(unix_grid))
    result = resample_variable(
        np.arange(8.0),
        epoch_times,
        epoch_grid,
        VariableSpec("v", ROLE_INSTANT, RULE_MEAN),
        time_convention=convention,
    )
    assert result.target_time.dtype.kind == dtype_kind
    np.testing.assert_array_equal(np.asarray(result.target_time), epoch_grid)


# ---------------------------------------------------------------------------
# End-to-end CDF integration
# ---------------------------------------------------------------------------


def _write_telemetry_cdf(path: Path) -> None:
    unix_times = (TIMES - np.datetime64("1970-01-01T00:00:00")).astype("timedelta64[s]").astype(np.float64)
    epoch = CDFepoch.timestamp_to_tt2000(unix_times)
    writer = cdfwrite.CDF(path, cdf_spec={"Checksum": True})
    epoch_spec: Dict[str, Any] = {
        "Variable": "epoch",
        "Data_Type": cdfwrite.CDF.CDF_TIME_TT2000,
        "Num_Elements": 1,
        "Rec_Vary": True,
        "Dim_Sizes": [],
        "Dim_Vary": [True],
    }
    writer.write_var(epoch_spec, var_data=epoch.astype(np.int64))

    status_spec = {
        "Variable": "state",
        "Data_Type": cdfwrite.CDF.CDF_INT4,
        "Num_Elements": 1,
        "Rec_Vary": True,
        "Dim_Sizes": [],
        "Dim_Vary": [True],
    }
    writer.write_var(
        status_spec,
        var_attrs={"DEPEND_0": "epoch", "FILLVAL": np.int32(-1)},
        var_data=np.array([1, 1, 2, 2, 3, 3, 3, 9], dtype=np.int32),
    )

    instant_spec = {
        "Variable": "temperature",
        "Data_Type": cdfwrite.CDF.CDF_REAL8,
        "Num_Elements": 1,
        "Rec_Vary": True,
        "Dim_Sizes": [],
        "Dim_Vary": [True],
    }
    temperature = np.array([0.0, 10.0, 20.0, np.nan, 60.0, 70.0, 80.0, 90.0])
    writer.write_var(
        instant_spec,
        var_attrs={"DEPEND_0": "epoch", "FILLVAL": np.float64(-1e31)},
        var_data=temperature,
    )

    counter_spec = {
        "Variable": "counter",
        "Data_Type": cdfwrite.CDF.CDF_REAL8,
        "Num_Elements": 1,
        "Rec_Vary": True,
        "Dim_Sizes": [],
        "Dim_Vary": [True],
    }
    writer.write_var(
        counter_spec,
        var_attrs={"DEPEND_0": "epoch"},
        var_data=np.array([0.0, 10.0, 20.0, 30.0, 5.0, 15.0, 25.0, 35.0]),
    )
    writer.close()


def test_resample_cdf_uses_depend0_fillval_and_epoch_type(tmp_path):
    path = tmp_path / "telemetry.cdf"
    _write_telemetry_cdf(path)
    with cdfread.CDF(path) as cdf:
        specs = [
            VariableSpec("state", ROLE_STATUS, RULE_FORWARD_FILL),
            VariableSpec("temperature", ROLE_INSTANT, RULE_MEAN),
            VariableSpec("counter", ROLE_ACCUMULATION, RULE_ALLOCATE,
                         reset_times=np.array(["2020-01-01T01:00"], dtype="datetime64[m]")),
        ]
        results = resample_cdf(cdf, GRID, specs)

    state, temperature, counter = results
    assert state.fill_value == -1
    np.testing.assert_array_equal(state.values, [1, 2, 3, 9])
    # TT2000 type is preserved on the returned time axis.
    assert state.target_time.dtype.kind == "i"
    assert temperature.coverage[1] == pytest.approx(0.5)
    # Reset at 01:00 keeps the two 30-unit increments apart, total conserved.
    assert np.nansum(counter.values) == pytest.approx(60.0)


def test_resample_cdf_supports_independent_time_axes(tmp_path):
    path = tmp_path / "two_clocks.cdf"
    unix_a = (TIMES - np.datetime64("1970-01-01T00:00:00")).astype("timedelta64[s]").astype(np.float64)
    epoch_a = CDFepoch.timestamp_to_tt2000(unix_a)
    times_b = TIMES + np.array(5, dtype="timedelta64[m]")
    unix_b = (times_b - np.datetime64("1970-01-01T00:00:00")).astype("timedelta64[s]").astype(np.float64)
    epoch_b = CDFepoch.timestamp_to_tt2000(unix_b)

    writer = cdfwrite.CDF(path, cdf_spec={})
    for name, data in (("epoch_a", epoch_a), ("epoch_b", epoch_b)):
        writer.write_var(
            {
                "Variable": name,
                "Data_Type": cdfwrite.CDF.CDF_TIME_TT2000,
                "Num_Elements": 1,
                "Rec_Vary": True,
                "Dim_Sizes": [],
                "Dim_Vary": [True],
            },
            var_data=data.astype(np.int64),
        )
    writer.write_var(
        {
            "Variable": "va",
            "Data_Type": cdfwrite.CDF.CDF_REAL8,
            "Num_Elements": 1,
            "Rec_Vary": True,
            "Dim_Sizes": [],
            "Dim_Vary": [True],
        },
        var_attrs={"DEPEND_0": "epoch_a"},
        var_data=np.arange(8.0),
    )
    writer.write_var(
        {
            "Variable": "vb",
            "Data_Type": cdfwrite.CDF.CDF_REAL8,
            "Num_Elements": 1,
            "Rec_Vary": True,
            "Dim_Sizes": [],
            "Dim_Vary": [True],
        },
        var_attrs={"DEPEND_0": "epoch_b"},
        var_data=np.arange(8.0) * 10.0,
    )
    writer.close()

    with cdfread.CDF(path) as cdf:
        results = resample_cdf(
            cdf,
            GRID,
            [
                VariableSpec("va", ROLE_INSTANT, RULE_MEAN),
                VariableSpec("vb", ROLE_INSTANT, RULE_MEAN),
            ],
        )
    # The two variables share no samples in the same grid bins because their
    # clocks are offset by five minutes.
    assert not np.allclose(results[0].values, results[1].values / 10.0, equal_nan=True)
    assert results[0].samples_used[0].tolist() != results[1].samples_used[0].tolist()
