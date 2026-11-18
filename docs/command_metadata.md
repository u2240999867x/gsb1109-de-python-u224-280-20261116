# Offline Parameter Binding

`psrpcore.command_metadata` resolves an already-parsed invocation against
PowerShell command metadata. It is an offline check: it does not run a script,
does not evaluate a command line, and does not turn positional strings into
parameter names or script syntax.

Use it before a collection backend calls a command such as
`Export-PSSession`, especially when the metadata came from a server whose
version can change.

## Building metadata

`CommandMetadata` models the parameter sets reported by `Get-Command` style
metadata. Each parameter can declare its type, aliases, and per-set
mandatory/position information.

```python
import psrpcore
from psrpcore.command_metadata import CommandMetadata, ParameterInput

metadata = CommandMetadata(
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
            "ParameterSets": {"Session": {"Position": 2}},
        },
    ],
    metadata_version="server-1",
    supports_dynamic_parameters=False,
)
```

Metadata can also be loaded from a mapping, a JSON string, or a JSON file path
with `load_command_metadata`.

## Resolving a call

The invocation is supplied as `ParameterInput` objects. A named input keeps the
original name (including an alias), and a positional input keeps the original
value exactly as supplied.

```python
plan = psrpcore.resolve_parameter_set(
    metadata,
    [
        ParameterInput.named("-OM", "my-module"),
        ParameterInput.positional(session),
        ParameterInput.positional("Get-Item"),
    ],
)

if plan.status == psrpcore.BindingStatus.Resolved:
    print(plan.parameter_set)
    print(plan.bindings)
```

A positional string is never reinterpreted. For example, a literal value of
`"-OutputModule"` remains the value bound to the positional parameter.

## Outcomes

`BindingStatus` distinguishes:

+ `Resolved` - exactly one set fully binds the call.
+ `Ambiguous` - more than one set fully binds the call. `parameter_set` stays
  `None`; the resolver does not choose the first set.
+ `MissingMandatory` - every candidate set is missing a mandatory parameter.
+ `TypeMismatch` - a value cannot be converted to the parameter type.
+ `UnknownParameter` - a named parameter is absent and dynamic parameters are
  ruled out.
+ `Undetermined` - there is not enough evidence, for example a dynamic parameter
  whose set membership is unknown.
+ `AmbiguousParameter` / `Incompatible` - the supplied name matches more than
  one parameter, or no set can accept the inputs for mixed reasons.

`ParameterBindingPlan.structurally_accepted_sets` reports sets that can bind
every input, while `candidate_sets` reports sets that also satisfy mandatory
and type checks. `missing_mandatory`, `type_mismatches`, and
`ambiguous_parameter_names` expose the individual reasons.

## Keeping plans auditable

`ParameterBindingPlan.to_dict()` keeps each input's original name/value and
every binding's basis (`Name`, `Alias`, `Prefix`, `Position`, or
`RemainingArguments`). `to_json()` adds a JSON-safe serialisation.

When the server metadata version changes, revalidate the related calls:

```python
refreshed = load_command_metadata(new_metadata_json)
if plan.is_stale(refreshed):
    plan = plan.revalidate(refreshed)
```

`CommandBindingCache` records related plans and revalidates them together:

```python
cache = psrpcore.CommandBindingCache()
cache.record(plan)
updated_plans = cache.revalidate(refreshed)
```

Revalidating with only a version string stays `Undetermined` because a new
version alone does not prove the parameter metadata still matches. Supply the
refreshed metadata content before executing the call.
