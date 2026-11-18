# -*- coding: utf-8 -*-
# Copyright: (c) 2024, Jordan Borean (@jborean93) <jborean93@gmail.com>
# MIT License (see LICENSE or https://opensource.org/licenses/MIT)

import pytest

import psrpcore
from psrpcore import (
    BindingBasis,
    BindingStatus,
    CommandBindingCache,
    CommandMetadata,
    ParameterBinder,
    ParameterInput,
    resolve_parameter_set,
)


class FakeSession:
    """Stand-in for a live PSSession object."""

    def __init__(self, name):
        self.name = name


def export_metadata(version="server-1"):
    return CommandMetadata(
        "Export-PSSession",
        [
            {
                "Name": "Session",
                "ParameterType": "System.Management.Automation.Runspaces.PSSession",
                "ParameterSets": {"Session": {"IsMandatory": True, "Position": 0}},
            },
            {
                "Name": "OutputModule",
                "ParameterType": "System.String",
                "Aliases": ["OM"],
                "ParameterSets": {"Session": {"IsMandatory": True, "Position": 1}},
            },
            {
                "Name": "CommandName",
                "ParameterType": "System.String[]",
                "Aliases": ["Name"],
                "ParameterSets": {"Session": {"Position": 2}},
            },
            {
                "Name": "FormatTypeName",
                "ParameterType": "System.String[]",
                "Aliases": ["TypeName"],
                "ParameterSets": {"Session": {"Position": 3}},
            },
            {
                "Name": "ArgumentList",
                "ParameterType": "System.Object[]",
                "ParameterSets": {"Session": {}},
            },
            {
                "Name": "Force",
                "ParameterType": "System.Management.Automation.SwitchParameter",
                "ParameterSets": {"Session": {}},
            },
        ],
        metadata_version=version,
        supports_dynamic_parameters=False,
    )


def two_set_metadata():
    return CommandMetadata(
        "Set-Thing",
        [
            {
                "Name": "Path",
                "ParameterType": "System.String",
                "ParameterSets": {"ByPath": {"Position": 0}},
            },
            {
                "Name": "LiteralPath",
                "ParameterType": "System.String",
                "ParameterSets": {"ByLiteralPath": {"Position": 0}},
            },
            {
                "Name": "Name",
                "ParameterType": "System.String",
                "ParameterSets": {
                    "ByPath": {"Position": 1},
                    "ByLiteralPath": {"Position": 1},
                },
            },
            {
                "Name": "Force",
                "ParameterType": "System.Management.Automation.SwitchParameter",
                "ParameterSets": {"ByPath": {}, "ByLiteralPath": {}},
            },
        ],
        parameter_sets=["ByPath", "ByLiteralPath"],
        supports_dynamic_parameters=False,
    )


def mandatory_two_set_metadata():
    return CommandMetadata(
        "Set-Mandatory",
        [
            {
                "Name": "Path",
                "ParameterType": "System.String",
                "ParameterSets": {"ByPath": {"IsMandatory": True}},
            },
            {
                "Name": "LiteralPath",
                "ParameterType": "System.String",
                "ParameterSets": {"ByLiteralPath": {"IsMandatory": True}},
            },
            {
                "Name": "Name",
                "ParameterType": "System.String",
                "ParameterSets": {"ByPath": {}, "ByLiteralPath": {}},
            },
        ],
        parameter_sets=["ByPath", "ByLiteralPath"],
        supports_dynamic_parameters=False,
    )


def test_resolves_named_positional_and_alias():
    session = FakeSession("s1")
    plan = resolve_parameter_set(
        export_metadata(),
        [
            ParameterInput.named("-Force", True, index=0),
            ParameterInput.named("-OM", "my-module", index=1),
            ParameterInput.positional(session, index=2),
            ParameterInput.positional("Get-Item", index=3),
        ],
    )

    assert plan.status == BindingStatus.Resolved
    assert plan.parameter_set == "Session"
    assert plan.candidate_sets == ("Session",)

    session_binding = plan.binding_for("Session")
    assert session_binding is not None
    assert session_binding.value is session
    assert session_binding.basis == BindingBasis.Position
    assert session_binding.position == 0

    module_binding = plan.binding_for("OutputModule")
    assert module_binding is not None
    assert module_binding.value == "my-module"
    assert module_binding.basis == BindingBasis.Alias
    assert module_binding.alias == "OM"

    command_binding = plan.binding_for("CommandName")
    assert command_binding is not None
    assert command_binding.value == "Get-Item"
    assert command_binding.basis == BindingBasis.Position
    assert command_binding.position == 2

    force_binding = plan.binding_for("Force")
    assert force_binding is not None
    assert force_binding.value is True


def test_alias_prefers_full_parameter_name():
    metadata = CommandMetadata(
        "Test-Alias",
        [
            {"Name": "Name", "ParameterType": "System.String"},
            {"Name": "NameAlias", "ParameterType": "System.String", "Aliases": ["Name"]},
        ],
        supports_dynamic_parameters=False,
    )

    plan = resolve_parameter_set(metadata, [ParameterInput.named("-Name", "value")])

    assert plan.status == BindingStatus.Resolved
    assert plan.bindings[0].name == "Name"
    assert plan.bindings[0].basis == BindingBasis.Name


def test_ambiguous_parameter_prefix_is_not_guessed():
    metadata = CommandMetadata(
        "Test-Prefix",
        [
            {"Name": "Session", "ParameterType": "System.Object"},
            {"Name": "Select", "ParameterType": "System.Object"},
        ],
        supports_dynamic_parameters=False,
    )

    plan = resolve_parameter_set(metadata, [ParameterInput.named("-Se", "value")])

    assert plan.status == BindingStatus.AmbiguousParameter
    assert plan.parameter_set is None
    assert plan.ambiguous_parameter_names == ("-Se",)


def test_multiple_valid_sets_are_reported_as_ambiguous():
    plan = resolve_parameter_set(
        two_set_metadata(),
        [
            ParameterInput.named("-Name", "thing"),
            ParameterInput.named("-Force", True),
        ],
    )

    assert plan.status == BindingStatus.Ambiguous
    assert plan.parameter_set is None
    assert plan.parameter_set_name is None
    assert plan.candidate_sets == ("ByPath", "ByLiteralPath")
    assert plan.bindings == ()
    assert plan.is_ambiguous


def test_missing_mandatory_is_distinct_from_type_mismatch():
    plan = resolve_parameter_set(
        mandatory_two_set_metadata(),
        [ParameterInput.named("-Name", "thing")],
    )

    assert plan.status == BindingStatus.MissingMandatory
    assert set(plan.missing_mandatory) == {"Path", "LiteralPath"}
    assert plan.type_mismatches == ()


def test_type_mismatch_is_reported_per_parameter():
    plan = resolve_parameter_set(
        export_metadata(),
        [
            ParameterInput.named("-Session", "not-a-session"),
            ParameterInput.named("-OutputModule", "module"),
        ],
    )

    assert plan.status == BindingStatus.TypeMismatch
    assert plan.parameter_set is None
    assert len(plan.type_mismatches) == 1
    failure = plan.type_mismatches[0]
    assert failure.parameter == "Session"
    assert failure.kind == psrpcore.BindingErrorKind.TypeMismatch


def test_unknown_named_parameter_when_dynamic_parameters_disabled():
    plan = resolve_parameter_set(
        export_metadata(),
        [
            ParameterInput.named("-Session", FakeSession("s")),
            ParameterInput.named("-OutputModule", "module"),
            ParameterInput.named("-DefinitelyNotAParameter", "value"),
        ],
    )

    assert plan.status == BindingStatus.UnknownParameter
    assert plan.parameter_set is None
    assert plan.unknown_parameters == ("-DefinitelyNotAParameter",)


def test_unknown_named_parameter_remains_undetermined_without_evidence():
    metadata = CommandMetadata(
        "Export-PSSession",
        [
            {"Name": "Session", "ParameterType": "System.Object", "Mandatory": True},
        ],
        supports_dynamic_parameters=None,
    )

    plan = resolve_parameter_set(
        metadata,
        [
            ParameterInput.named("-Session", FakeSession("s")),
            ParameterInput.named("-MaybeDynamic", "value"),
        ],
    )

    assert plan.status == BindingStatus.Undetermined
    assert plan.parameter_set is None
    assert plan.is_undetermined
    assert plan.unknown_parameters == ("-MaybeDynamic",)
    assert plan.unresolved[0].kind == psrpcore.BindingErrorKind.DynamicParameter


def test_dynamic_parameter_without_set_membership_is_undetermined():
    metadata = CommandMetadata(
        "Export-PSSession",
        [
            {"Name": "Session", "ParameterType": "System.Object", "Mandatory": True},
            {"Name": "DynamicThing", "ParameterType": "System.String", "IsDynamic": True},
        ],
        supports_dynamic_parameters=True,
    )

    plan = resolve_parameter_set(
        metadata,
        [
            ParameterInput.named("-Session", FakeSession("s")),
            ParameterInput.named("-DynamicThing", "value"),
        ],
    )

    assert plan.status == BindingStatus.Undetermined
    assert plan.parameter_set is None
    assert plan.unresolved[0].kind == psrpcore.BindingErrorKind.MissingSetEvidence


def test_positional_string_is_literal_not_command_syntax():
    session = FakeSession("s")
    plan = resolve_parameter_set(
        export_metadata(),
        [
            ParameterInput.positional(session),
            ParameterInput.positional("-OutputModule"),
            ParameterInput.positional("Get-Item; Remove-Item -Force"),
        ],
    )

    assert plan.status == BindingStatus.Resolved
    assert plan.parameter_set == "Session"

    module = plan.binding_for("OutputModule")
    assert module is not None
    assert module.value == "-OutputModule"
    assert module.basis == BindingBasis.Position
    assert module.position == 1

    command = plan.binding_for("CommandName")
    assert command is not None
    assert command.value == "Get-Item; Remove-Item -Force"
    assert command.basis == BindingBasis.Position
    assert command.position == 2


def test_bare_string_is_one_positional_input_and_never_split():
    plan = resolve_parameter_set(export_metadata(), "-OutputModule")

    assert len(plan.inputs) == 1
    assert plan.inputs[0].name is None
    assert plan.inputs[0].value == "-OutputModule"


def test_plan_keeps_original_inputs_and_reasons():
    session = FakeSession("s")
    plan = resolve_parameter_set(
        export_metadata(),
        [
            ParameterInput.named("-OM", "module", index=0),
            ParameterInput.positional(session, index=1),
        ],
    )

    assert plan.to_dict()["inputs"][0]["value"] == "module"
    assert plan.to_dict()["inputs"][0]["name"] == "-OM"
    assert plan.to_dict()["inputs"][1]["value"] is session
    assert plan.to_dict()["bindings"][0]["reason"]

    json_plan = plan.to_json()
    assert "module" in json_plan
    assert "FakeSession" in json_plan


def test_revalidate_against_refreshed_metadata():
    session = FakeSession("s")
    original = resolve_parameter_set(
        export_metadata("server-1"),
        [
            ParameterInput.named("-Session", session),
            ParameterInput.named("-OutputModule", "module"),
            ParameterInput.named("-Name", "Get-Item"),
        ],
    )
    assert original.status == BindingStatus.Resolved
    assert original.parameter_set == "Session"

    refreshed = export_metadata("server-2")
    assert original.is_stale(refreshed)

    revalidated = original.revalidate(refreshed)
    assert revalidated.status == BindingStatus.Resolved
    assert revalidated.parameter_set == "Session"
    assert revalidated.metadata_version == "server-2"
    assert revalidated.previous_metadata_version == "server-1"
    assert revalidated.metadata_changed
    assert revalidated.inputs == original.inputs

    # The same alias still binds, and the original values are untouched.
    assert revalidated.binding_for("CommandName").value == "Get-Item"


def test_revalidate_with_only_a_version_string_stays_undetermined():
    plan = resolve_parameter_set(
        export_metadata("server-1"),
        [
            ParameterInput.named("-Session", FakeSession("s")),
            ParameterInput.named("-OutputModule", "module"),
        ],
    )

    revalidated = plan.revalidate("server-2")
    assert revalidated.status == BindingStatus.Undetermined
    assert revalidated.parameter_set is None
    assert revalidated.metadata_changed
    assert revalidated.previous_metadata_version == "server-1"


def test_metadata_change_that_removes_alias_is_detected():
    session = FakeSession("s")
    plan = resolve_parameter_set(
        export_metadata("server-1"),
        [
            ParameterInput.named("-Session", session),
            ParameterInput.named("-OM", "module"),
        ],
    )
    assert plan.status == BindingStatus.Resolved

    changed = CommandMetadata(
        "Export-PSSession",
        [
            {
                "Name": "Session",
                "ParameterType": "System.Management.Automation.Runspaces.PSSession",
                "Mandatory": True,
                "Position": 0,
            },
            {
                "Name": "OutputModule",
                "ParameterType": "System.String",
                "Mandatory": True,
                "Position": 1,
            },
        ],
        metadata_version="server-2",
        supports_dynamic_parameters=False,
    )

    revalidated = plan.revalidate(changed)
    assert revalidated.status in (BindingStatus.UnknownParameter, BindingStatus.Undetermined)
    assert revalidated.parameter_set is None


def test_binding_cache_revalidates_every_related_call():
    cache = CommandBindingCache()
    session = FakeSession("s")

    first = resolve_parameter_set(
        export_metadata("server-1"),
        [
            ParameterInput.named("-Session", session),
            ParameterInput.named("-OutputModule", "module"),
        ],
    )
    second = resolve_parameter_set(
        export_metadata("server-1"),
        [
            ParameterInput.named("-Session", FakeSession("s2")),
            ParameterInput.named("-OM", "module2"),
            ParameterInput.named("-Name", "Get-ChildItem"),
        ],
    )
    cache.record(first)
    cache.record(second)

    assert len(cache.plans_for("export-pssession")) == 2

    updated = cache.revalidate(export_metadata("server-2"))
    assert len(updated) == 2
    assert all(p.status == BindingStatus.Resolved for p in updated)
    assert all(p.metadata_version == "server-2" for p in updated)
    assert len(cache.plans) == 4


def test_metadata_from_get_command_style_mapping():
    metadata = CommandMetadata.from_mapping(
        {
            "Name": "Export-PSSession",
            "CommandType": "Cmdlet",
            "DefaultParameterSet": "Session",
            "Parameters": {
                "Session": {
                    "ParameterType": {
                        "Name": "PSSession",
                        "FullName": "System.Management.Automation.Runspaces.PSSession",
                    },
                    "ParameterSets": {
                        "Session": {"IsMandatory": True, "Position": 0},
                    },
                },
                "OutputModule": {
                    "ParameterType": {"Name": "String", "FullName": "System.String"},
                    "ParameterSets": {
                        "Session": {"IsMandatory": True, "Position": 1},
                    },
                },
            },
        }
    )

    assert metadata.name == "Export-PSSession"
    assert metadata.parameter_sets == ("Session",)
    assert metadata.default == "Session"

    plan = resolve_parameter_set(
        metadata,
        [
            ParameterInput.named("-Session", FakeSession("s")),
            ParameterInput.named("-OutputModule", "module"),
        ],
    )
    assert plan.status == BindingStatus.Resolved


def test_binder_and_command_object_input():
    binder = ParameterBinder(export_metadata())
    command = psrpcore.Command("Export-PSSession")
    command.add_parameter("-OM", "module")
    command.add_argument(FakeSession("s"))

    plan = binder.bind(command)
    assert plan.status == BindingStatus.Resolved
    assert plan.parameter_set == "Session"
    assert plan.binding_for("OutputModule").value == "module"
    assert isinstance(plan.binding_for("Session").value, FakeSession)


def test_parameter_not_in_set_is_reported():
    plan = resolve_parameter_set(
        two_set_metadata(),
        [
            ParameterInput.named("-Path", "a"),
            ParameterInput.named("-LiteralPath", "b"),
        ],
    )

    assert plan.status == BindingStatus.Incompatible
    assert plan.parameter_set is None
    assert any(
        failure.kind == psrpcore.BindingErrorKind.ParameterNotInSet
        for candidate in plan.candidates
        for failure in candidate.failures
    )


def test_remaining_arguments_parameter_absorbs_extra_positionals():
    metadata = CommandMetadata(
        "Test-Remaining",
        [
            {"Name": "Name", "ParameterType": "System.String", "Position": 0},
            {
                "Name": "Remaining",
                "ParameterType": "System.Object[]",
                "ValueFromRemainingArguments": True,
            },
        ],
        supports_dynamic_parameters=False,
    )

    plan = resolve_parameter_set(
        metadata,
        [
            ParameterInput.positional("name"),
            ParameterInput.positional("extra1"),
            ParameterInput.positional("extra2"),
        ],
    )

    assert plan.status == BindingStatus.Resolved
    assert [b.value for b in plan.bindings] == ["name", "extra1", "extra2"]
    assert plan.bindings[1].basis == BindingBasis.RemainingArguments
    assert plan.bindings[2].basis == BindingBasis.RemainingArguments


def test_pipeline_input_satisfies_mandatory_parameter_when_enabled():
    metadata = CommandMetadata(
        "Test-Pipeline",
        [
            {
                "Name": "InputObject",
                "ParameterType": "System.Object",
                "Mandatory": True,
                "ValueFromPipeline": True,
            },
        ],
        supports_dynamic_parameters=False,
    )

    without_pipeline = resolve_parameter_set(metadata, [])
    assert without_pipeline.status == BindingStatus.MissingMandatory

    with_pipeline = resolve_parameter_set(metadata, [], has_pipeline_input=True)
    assert with_pipeline.status == BindingStatus.Resolved


def test_switch_rejects_object_value():
    metadata = CommandMetadata(
        "Test-Switch",
        [{"Name": "Force", "ParameterType": "System.Management.Automation.SwitchParameter"}],
        supports_dynamic_parameters=False,
    )

    plan = resolve_parameter_set(metadata, [ParameterInput.named("-Force", object())])
    assert plan.status == BindingStatus.TypeMismatch
    assert plan.type_mismatches[0].parameter == "Force"


@pytest.mark.parametrize(
    "value, accepted",
    [
        ("1", True),
        (1, True),
        ("not-a-number", False),
    ],
)
def test_numeric_type_conversion(value, accepted):
    metadata = CommandMetadata(
        "Test-Number",
        [{"Name": "Count", "ParameterType": "System.Int32"}],
        supports_dynamic_parameters=False,
    )

    plan = resolve_parameter_set(metadata, [ParameterInput.named("-Count", value)])
    assert (plan.status == BindingStatus.Resolved) is accepted
