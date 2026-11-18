"""Tests for value-aware ``DEPEND_n`` repair planning and application."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from cdflib import CDF
from cdflib.depend_repair import (
    DependencyModel,
    ProviderSpec,
    apply_repairs,
    discover_spec,
    plan_repairs,
    validate,
    verify_repair,
)

try:  # imported as ``tests.test_depend_repair``
    from . import depend_fixtures as fixtures
except ImportError:  # imported with ``tests/`` on sys.path
    import depend_fixtures as fixtures  # type: ignore[no-redef]

SAMPLE_FILES = [
    "fa_esa_l2_eeb_00000000_v01.cdf",
    "de2_ion2s_rpa_19830213_v01.cdf",
    "psp_fld_l2_mag_rtn_1min_20200104_v02.cdf",
]
TESTFILES = Path(__file__).parent / "testfiles"


def _plan(path: Path, payload: dict | None = None):
    model = DependencyModel.from_cdf(path)
    spec = ProviderSpec.from_dict(payload) if payload is not None else discover_spec(model)
    return model, plan_repairs(model, spec)


class TestShippedFiles:
    """The real sample files must already be legal and survive a rewrite."""

    @pytest.mark.parametrize("name", SAMPLE_FILES)
    def test_samples_have_no_violations(self, name):
        model = DependencyModel.from_cdf(TESTFILES / name)
        assert validate(model, {}, {}) == []
        assert discover_spec(model).suspects == []

    @pytest.mark.parametrize("name", SAMPLE_FILES)
    def test_rewrite_preserves_every_observation(self, name, tmp_path):
        model = DependencyModel.from_cdf(TESTFILES / name)
        result = plan_repairs(model, ProviderSpec())
        output = tmp_path / f"copy_{name}"
        manifest = apply_repairs(model, result, output, manifest_path=tmp_path / "manifest.json")
        assert manifest["applied"] == {}

        report = verify_repair(model, output, manifest)
        assert report["ok"], report["problems"]
        for entry in report["observations"].values():
            assert entry["values_equal"]
        assert report["dataset"]["imported"]

        before = CDF(TESTFILES / name)
        after = CDF(output)
        for variable in model.order:
            a = np.asarray(before.varget(variable))
            b = np.asarray(after.varget(variable))
            assert a.shape == b.shape
            assert a.dtype == b.dtype
            if a.dtype.kind in "fc":
                assert np.array_equal(a, b, equal_nan=True)
            else:
                assert np.array_equal(a, b)


class TestValueAwareLegality:
    """Legality must follow values and metadata, not just lengths."""

    def test_equal_length_swap_is_detected(self, tmp_path):
        path = fixtures.build_axis_swap_fixture(tmp_path / "swap.cdf", wrong=True)
        model = DependencyModel.from_cdf(path)
        violations = validate(model, {}, {})
        reasons = {(violation.variable, violation.axis, violation.reason) for violation in violations}
        assert ("flux", 1, "family_mismatch") in reasons
        assert ("flux", 2, "family_mismatch") in reasons

    def test_swap_is_repaired_to_the_single_legal_plan(self, tmp_path):
        path = fixtures.build_axis_swap_fixture(tmp_path / "swap.cdf", wrong=True)
        model, result = _plan(path)
        assert result.minimum_cost == 2.0
        assert result.plan_count == 1
        assert result.determined == {"flux:1": "angle_axis", "flux:2": "energy_axis"}
        assert result.candidates == {}

    def test_value_range_rejects_implausible_coordinate(self, tmp_path):
        # A 4-element axis of huge negative values cannot be pitch angles even
        # if its descriptive metadata claims to be.
        path = fixtures.write_cdf(
            tmp_path / "range.cdf",
            [
                fixtures.epoch_variable("Epoch", 3),
                fixtures.coordinate("bad_angle", [-1.0e12, -2.0e12, -3.0e12, -4.0e12], {"CATDESC": "pitch angle"}),
                fixtures.data_variable(
                    "flux",
                    (3, 4),
                    {"DEPEND_0": "Epoch", "DEPEND_1": "bad_angle", "DISPLAY_TYPE": "spectrogram"},
                ),
            ],
        )
        model = DependencyModel.from_cdf(path)
        reasons = {violation.reason for violation in validate(model, {}, {})}
        assert "value_range_mismatch" in reasons

    def test_metadata_variable_is_never_a_coordinate(self, tmp_path):
        path = fixtures.build_axis_swap_fixture(tmp_path / "swap.cdf", wrong=True)
        model, result = _plan(path)
        for candidates in result.candidates.values():
            assert not any(name.endswith("_labels") for name in candidates)
        assert not any(name.endswith("_labels") for name in result.determined.values())


class TestReferenceAccounting:
    """Changing a coordinate affects every consumer, so decide per edge."""

    def test_only_the_wrong_edge_changes(self, tmp_path):
        path = fixtures.build_several_references_fixture(tmp_path / "multi.cdf", wrong_edge=True)
        model, result = _plan(path)
        assert result.determined == {"energy_spectra:1": "energy"}
        assert model.references_to("pitch") != []

    def test_apply_keeps_other_referrers_intact(self, tmp_path):
        path = fixtures.build_several_references_fixture(tmp_path / "multi.cdf", wrong_edge=True)
        model, result = _plan(path)
        output = tmp_path / "fixed.cdf"
        manifest = apply_repairs(model, result, output, manifest_path=tmp_path / "m.json")

        assert manifest["applied"] == {"energy_spectra:1": "energy"}
        assert manifest["originals"] == {"energy_spectra:1": "pitch"}
        assert manifest["reference_counts"]["pitch"] == 3

        repaired = DependencyModel.from_cdf(output)
        assert repaired.variables["flux_a"].depend[1] == "pitch"
        assert repaired.variables["flux_b"].depend[1] == "pitch"
        assert repaired.variables["energy_spectra"].depend[1] == "energy"
        assert validate(repaired, {}, {}) == []

        # The change is auditable from the file itself.
        attrs = CDF(output).varattsget("energy_spectra")
        assert attrs["DEPEND_1"] == "energy"
        assert attrs["DEPEND_1_ORIGINAL"] == "pitch"


class TestConsensus:
    """Several equally cheap plans may never be resolved by guessing."""

    def test_two_valid_candidates_are_reported_not_applied(self, tmp_path):
        path = fixtures.build_ambiguous_fixture(tmp_path / "amb.cdf")
        model, result = _plan(path)
        assert result.minimum_cost == 1.0
        assert result.plan_count == 2
        assert result.determined == {}
        assert result.candidates == {"counts:1": ["angle_a", "angle_b"]}

        output = tmp_path / "amb_fixed.cdf"
        manifest = apply_repairs(model, result, output, manifest_path=tmp_path / "m.json")
        assert manifest["applied"] == {}
        repaired = DependencyModel.from_cdf(output)
        assert repaired.variables["counts"].depend[1] == "broken_axis"

    def test_edges_with_a_single_possible_plan_are_still_applied(self, tmp_path):
        # Make one edge ambiguous and another unambiguous in the same file.
        path = fixtures.write_cdf(
            tmp_path / "mixed.cdf",
            [
                fixtures.epoch_variable("Epoch", 4),
                fixtures.coordinate("short", [1, 2, 3], {"CATDESC": "pitch angle"}),
                fixtures.coordinate("angle_a", [10, 20, 30, 40], {"CATDESC": "pitch angle A"}),
                fixtures.coordinate("angle_b", [15, 25, 35, 45], {"CATDESC": "pitch angle B"}),
                fixtures.coordinate("energy", [1e2, 2e2, 3e2, 4e2], {"CATDESC": "energy", "UNITS": "eV"}),
                fixtures.data_variable(
                    "counts",
                    (4, 4),
                    {"DEPEND_0": "Epoch", "DEPEND_1": "short", "DISPLAY_TYPE": "spectrogram"},
                ),
                fixtures.label_variable("energy_labels", ["e0", "e1", "e2", "e3"]),
                fixtures.data_variable(
                    "spectra",
                    (4, 4),
                    {
                        "DEPEND_0": "Epoch",
                        "DEPEND_1": "angle_a",
                        "LABL_PTR_1": "energy_labels",
                        "DISPLAY_TYPE": "spectrogram",
                    },
                ),
            ],
        )
        model = DependencyModel.from_cdf(path)
        spec = ProviderSpec.from_dict(
            {
                "suspects": [
                    {"variable": "counts", "axis": 1, "candidates": ["angle_a", "angle_b"]},
                    {"variable": "spectra", "axis": 1, "candidates": ["energy"]},
                ]
            }
        )
        result = plan_repairs(model, spec)
        assert result.determined == {"spectra:1": "energy"}
        assert result.candidates == {"counts:1": ["angle_a", "angle_b"]}


class TestInfeasible:
    """When nothing works, conflicts must be reported and nothing written."""

    def test_infeasible_fixture_reports_conflicts(self, tmp_path):
        path = fixtures.build_infeasible_fixture(tmp_path / "inf.cdf")
        model, result = _plan(path)
        assert result.minimum_cost is None
        assert result.plan_count == 0
        assert result.conflicts
        text = json.dumps(result.conflicts)
        assert "no_legal_candidate" in text or "jointly_infeasible" in text

    def test_conflict_names_every_candidate_and_reason(self, tmp_path):
        path = fixtures.build_infeasible_fixture(tmp_path / "inf.cdf")
        model, result = _plan(path)
        conflict = result.conflicts[0]
        assert conflict["variable"] == "flux"
        assert conflict["attribute"] == "DEPEND_1"
        assert "angle_short" in conflict["candidates"]
        reasons = {
            violation["reason"]
            for violations in conflict["violations"].values()
            for violation in violations
        }
        assert "size_mismatch" in reasons
        assert "label_size_mismatch" in reasons

    def test_cli_refuses_to_write_when_infeasible(self, tmp_path, capsys):
        path = fixtures.build_infeasible_fixture(tmp_path / "inf.cdf")
        output = tmp_path / "out.cdf"
        from cdflib.depend_repair import main

        code = main(["repair", str(path), "-o", str(output)])
        assert code == 1
        assert not output.exists()


class TestProviderSpec:
    """The provider format is explicit and validated."""

    def test_permutation_group_is_honoured(self, tmp_path):
        path = fixtures.build_permutation_fixture(tmp_path / "perm.cdf", swapped=True)
        model = DependencyModel.from_cdf(path)
        spec = ProviderSpec.from_dict(
            {
                "suspects": [
                    {"variable": "cube", "axis": 1, "candidates": ["pitch", "energy"]},
                    {"variable": "cube", "axis": 2, "candidates": ["pitch", "energy"]},
                ],
                "permutations": {"cube": [[1, 2, 3]]},
            }
        )
        result = plan_repairs(model, spec)
        assert result.determined == {"cube:1": "pitch", "cube:2": "energy"}

    def test_candidate_space_guard(self, tmp_path):
        path = fixtures.build_axis_swap_fixture(tmp_path / "swap.cdf", wrong=True)
        model = DependencyModel.from_cdf(path)
        spec = ProviderSpec.from_dict(
            {"suspects": [{"variable": "flux", "axis": 1, "candidates": ["angle_axis", "energy_axis"]}], "max_combinations": 0}
        )
        with pytest.raises(ValueError, match="max_combinations"):
            plan_repairs(model, spec)

    def test_unknown_variable_is_a_conflict(self, tmp_path):
        path = fixtures.build_axis_swap_fixture(tmp_path / "swap.cdf", wrong=True)
        model = DependencyModel.from_cdf(path)
        spec = ProviderSpec.from_dict({"suspects": [{"variable": "ghost", "axis": 1, "candidates": ["angle_axis"]}]})
        result = plan_repairs(model, spec)
        assert result.minimum_cost is None
        assert any(conflict["reason"] == "missing_variable" for conflict in result.conflicts)


class TestManifestAndAudit:
    """The manifest and in-file audit trail must let a reviewer re-check."""

    def test_manifest_records_before_and_after(self, tmp_path):
        path = fixtures.build_axis_swap_fixture(tmp_path / "swap.cdf", wrong=True)
        model, result = _plan(path)
        output = tmp_path / "fixed.cdf"
        manifest_path = tmp_path / "fixed.manifest.json"
        apply_repairs(model, result, output, manifest_path=manifest_path)

        payload = json.loads(manifest_path.read_text())
        assert payload["applied"] == {"flux:1": "angle_axis", "flux:2": "energy_axis"}
        assert payload["originals"] == {"flux:1": "energy_axis", "flux:2": "angle_axis"}
        assert payload["dependencies_before"]["flux:1"] == "energy_axis"
        assert payload["dependencies_after"]["flux:1"] == "angle_axis"
        assert payload["observations"]["flux"]["sha256"]
        assert payload["output"]["sha256"]

    def test_verification_catches_tampered_observation(self, tmp_path):
        path = fixtures.build_axis_swap_fixture(tmp_path / "swap.cdf", wrong=True)
        model, result = _plan(path)
        output = tmp_path / "fixed.cdf"
        manifest = apply_repairs(model, result, output, manifest_path=tmp_path / "m.json")
        report = verify_repair(model, output, manifest)
        assert report["ok"]

        # Alter the observation on disk and confirm the check fails.
        tampered = tmp_path / "tampered.cdf"
        import shutil

        shutil.copy(output, tampered)
        from cdflib import cdfwrite
        from cdflib.depend_repair import _typed_attribute

        source = CDF(output)
        info = source.cdf_info()
        with cdfwrite.CDF(
            str(tampered),
            cdf_spec={"Majority": info.Majority, "Encoding": info.Encoding},
            delete=True,
        ) as writer:
            writer.write_globalattrs(
                {key: {i: value for i, value in enumerate(values)} for key, values in source.globalattsget().items()}
            )
            for name in model.order:
                var_info = source.varinq(name)
                attrs = {key: _typed_attribute(source, key, name) for key in source.varattsget(name)}
                data = np.asarray(source.varget(name))
                if name == "flux":
                    data = data + 1.0
                writer.write_var(
                    {
                        "Variable": name,
                        "Var_Type": var_info.Var_Type,
                        "Data_Type": var_info.Data_Type,
                        "Num_Elements": var_info.Num_Elements,
                        "Rec_Vary": var_info.Rec_Vary,
                        "Dim_Sizes": list(var_info.Dim_Sizes),
                        "Dim_Vary": list(var_info.Dim_Vary),
                        "Sparse": var_info.Sparse,
                        "Compress": 0,
                    },
                    var_attrs=attrs,
                    var_data=data,
                )
        report = verify_repair(model, tampered, manifest, dataset_check=False)
        assert not report["ok"]
        assert any("flux" in problem for problem in report["problems"])


class TestCli:
    """The command line wraps planning, writing and verification."""

    def test_analyze_json(self, tmp_path, capsys):
        path = fixtures.build_axis_swap_fixture(tmp_path / "swap.cdf", wrong=True)
        from cdflib.depend_repair import main

        code = main(["analyze", str(path), "--json"])
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["minimum_cost"] == 2.0
        assert payload["plan_count"] == 1

    def test_repair_writes_manifest_and_verifies(self, tmp_path, capsys):
        path = fixtures.build_axis_swap_fixture(tmp_path / "swap.cdf", wrong=True)
        output = tmp_path / "fixed.cdf"
        manifest = tmp_path / "manifest.json"
        from cdflib.depend_repair import main

        code = main(["repair", str(path), "-o", str(output), "--manifest", str(manifest)])
        assert code == 0
        assert output.exists()
        assert manifest.exists()
        report = json.loads(manifest.read_text())
        assert report["applied"] == {"flux:1": "angle_axis", "flux:2": "energy_axis"}
