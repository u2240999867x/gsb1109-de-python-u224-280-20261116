# Variable naming

CDF variable names are case sensitive and may contain leading or trailing
spaces. Many downstream consumers fold both, so two genuinely different CDF
variables can map onto the same name. `cdflib.naming` builds an explicit,
collision-free plan instead of silently choosing the first variable in file
order.

```python
from cdflib import CDF
from cdflib.naming import build_name_plan, VariableIdentity

with CDF("input.cdf") as cdf:
    plan = cdf.variable_name_map(reserved=["Epoch"])
    for target, original in plan.mapping.items():
        print(target, "<-", original)
```

## Identity and domains

Each variable is identified by `(raw name, domain)`. The domain is
`zvariable` or `rvariable`; the two domains are separate name spaces and may
legitimately contain the same spelling. Raw names keep their original case and
edge whitespace.

## Collisions

Target names are unique after the same normalization used by the target
repository (edge whitespace removed, case folded), as well as literally.
Invalid characters are replaced and an unused suffix is added when needed.
`reserved` and a caller-provided `is_legal` callback participate in choosing
each name.

## DEPEND references

`DEPEND_n` values are resolved against the original names:

- an exact spelling wins;
- a folded match is used only when it is unique;
- the dependent variable's own r/z domain may break a same-spelling tie;
- anything still ambiguous is left unresolved and recorded in
  `plan.conflicts`; no variable is selected by position.

The migrated values are available from `plan.depend_targets` (or
`plan.depend_reference(variable, index)`).

## Writing and restoring

Pass a plan to the writer to apply it. The mapping is stored in the
`CDFLIB_NAME_MAP` global attribute, so the original names and domains can be
recovered later.

```python
from cdflib import CDF, cdfwrite

with cdfwrite.CDF("output.cdf", name_plan=plan) as out:
    out.write_var(var_spec, var_attrs=var_attrs, var_data=var_data)

with CDF("output.cdf") as cdf:
    cdf.varget("Bx_1")             # target name
    cdf.varget_original("Bx ")     # original name, including edge space
```

`CDF.stored_name_plan()`, `CDF.restored_variable_name(target)` and
`CDF.varattsget_original(original)` expose the same map directly.

## Manifest

`NamePlan.as_dict()` produces a JSON-serializable manifest containing only
names (never data values). `NamePlan.from_dict()` reconstructs it, and
`apply_name_plan` / `restore_name_plan` apply it to name and attribute
dictionaries.

::: cdflib.naming
