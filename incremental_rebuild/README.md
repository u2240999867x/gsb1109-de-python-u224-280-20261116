# incremental_rebuild

Minimal provably-safe rebuild of Pyomo data and model construction after
a batch of observations is corrected.

## Inputs

- **Two load results** (`LoadResult`): tables of rows keyed by index
  tuple; every row is a `Cell(value, source)` so each item stays
  traceable to its original observation. Rows are stored by key, so row
  reordering never registers as a change.
- **Explicit dependencies** (`ComponentDecl` per component): which
  tables a component reads as values (`value_reads`), which table's keys
  form its index domain (`index_from`), which components it is built
  over (`upstream`, `index_upstream`), and whether gaps fall back to a
  default (`has_default`).
- **Construction-rule dependency declarations**: `index_reads` lists the
  tables a rule uses for index selection, and `complete` states whether
  the declaration is provably complete.

## Change classification

`plan_rebuild(old, new, decls)` diffs the two loads order-insensitively
and classifies each (component, changed table) pair:

| Impact kind | Meaning | Action |
| --- | --- | --- |
| `VALUE_ONLY` | same keys, values differ | `REVALUE` (assign onto old instance) |
| `MEMBERSHIP` | keys added/removed, no default | `RECONSTRUCT` |
| `DEFAULT_TRIGGERING` | keys added/removed, default coverage shifts | `REVALUE` (with default fallback) |
| `CONSTRUCTION_SCOPE` | table feeds index selection or the index domain | `RECONSTRUCT` |

A table that participates in index selection forces reconstruction even
for value-only diffs — re-assigning values on the old instance cannot be
proven safe. Components whose rule dependencies cannot be proven
complete (`complete=False`) keep their full verification scope
(`VERIFY_FULL`) whenever anything changed. Reconstructions propagate
along upstream edges; revalues do not (Pyomo expressions hold symbolic
references).

## Guarantees

- **Minimality**: a component is excluded only if its declaration is
  complete and none of its declared inputs changed, so its from-scratch
  construction on the new data is identical to the old instance.
- **Equivalence**: after `apply_plan`, rebuilt parts equal a from-scratch
  construction on the new data; untouched parts are the identical objects.
- **Traceability**: every impact carries the old/new cells with their
  observation sources, item by item (`plan.affected_sources()`).

## Usage

```python
from incremental_rebuild import ComponentDecl, plan_rebuild
from incremental_rebuild.executor import apply_plan

plan = plan_rebuild(old_data, new_data, decls)
apply_plan(model, plan, decls, builders, new_data)
```

See `incremental_rebuild/tests/test_pyomo_executor.py` for a complete
example with builders, equivalence checks against a from-scratch model,
and object-identity checks for untouched components.
