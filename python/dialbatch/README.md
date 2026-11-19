# dialbatch

Resolve the single domestic dialling convention shared by every row of a
legacy batch, using the bundled `phonenumbers` metadata.

## Problem

An old directory has no per-row region, but the supplier confirms that all
rows in one column follow the same domestic convention. Given the finite set
of candidate regions, plus rows whose international form has already been
verified, the task is to find which region explanations still hold for the
whole batch and which complete numbers can be confirmed.

## Rules implemented by `resolver.resolve_batch`

1. Every row is read under every candidate region; a region survives only if
   *all* rows are compatible with it. The full region-by-row table is built
   first, so changing the order of the rows or candidates cannot change the
   result.
2. A row carrying an explicit international prefix (`+`, or the region's own
   IDD prefix from the metadata) is parsed internationally and is never
   re-read as a domestic number.
3. With several surviving regions, a row is confirmed only when every
   surviving read yields the same E.164 number; otherwise it keeps its
   per-region candidates.
4. With no surviving region, the report marks the batch contradictory and
   names, for each region, the first row that rejects it and why.

## Usage

```python
from dialbatch import resolve_batch, explain

report = resolve_batch(
    ["030 901820", "089 12345678", "+43 1 58058"],
    ["DE", "AT", "CH"])

report.surviving_regions     # ('DE',)
report.confirmed_numbers()   # {'r1': '+4930901820', ...}
print(explain(report))
```

Rows may be plain strings (ids `r1`, `r2`, ...) or `(row_id, raw)` tuples.
By default a read must be a valid assigned number; pass
`require_valid=False` to accept any possible number instead.
