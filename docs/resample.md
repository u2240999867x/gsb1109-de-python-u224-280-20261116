# Resampling onto a Fixed Time Grid

`cdflib.resample` moves time-dependent CDF variables onto a common fixed grid
without forcing every variable through the same interpolation rule. Each
variable's physical role decides what is mathematically legal, and every output
bin keeps the evidence needed to audit how its value was produced.

## Roles and rules

| Role (`VariableSpec.role`) | Allowed rules (`rule`) | Behavior |
| --- | --- | --- |
| `status` | `forward_fill`, `sample` | Discrete state codes. Only observed codes are emitted or carried forward; intermediate states are never invented. |
| `instant` | `mean`, `linear`, `sample` | Instantaneous continuous quantities. Time-weighted mean by default; linear interpolation never crosses an uncovered gap. |
| `accumulation` | `allocate` | Running counters. Increment between samples is split across bins by elapsed time, totals are conserved, and no mass crosses a reset (explicit `reset_times` or a gap/invalid record). |

Bins are the half-open intervals between consecutive grid-point midpoints; the
two end bins extend by half a grid step.

## What every result contains

A `ResampledVariable` pairs the resampled `values` with an audit trail:

- `coverage`: fraction of each bin's duration actually covered (0 to 1).
- `samples_used`: the original record indices contributing to each bin.
- `uncovered`: `[start, end)` pairs of the sub-intervals with no valid data.
- `covered_segments`: `[start, end)` spans of consecutive valid samples.
- `target_time`: grid centers converted back to the variable's original time
  type (CDF_EPOCH, CDF_EPOCH16, TT2000, datetime64 or unix seconds); fill
  values are preserved (integer variables require an explicit `fill_value`).

## Example

```python
import numpy as np
from cdflib.resample import (
    VariableSpec,
    resample_variable,
    ROLE_STATUS,
    ROLE_INSTANT,
    ROLE_ACCUMULATION,
)

times = np.array(["2020-01-01T00:00", "2020-01-01T00:10",
                  "2020-01-01T00:20", "2020-01-01T00:30"],
                 dtype="datetime64[m]")
grid = np.array(["2020-01-01T00:00", "2020-01-01T00:30"],
                dtype="datetime64[m]")

state = resample_variable(
    np.array([1, 1, 2, 2], dtype=np.int32), times, grid,
    VariableSpec("state", ROLE_STATUS, "forward_fill", fill_value=-1),
)
temperature = resample_variable(
    np.array([10.0, 12.0, 14.0, 16.0]), times, grid,
    VariableSpec("temperature", ROLE_INSTANT, "mean"),
)
counter = resample_variable(
    np.array([0.0, 10.0, 20.0, 30.0]), times, grid,
    VariableSpec("counter", ROLE_ACCUMULATION, "allocate",
                 reset_times=np.array(["2020-01-01T00:25"],
                                      dtype="datetime64[m]")),
)

print(state.values)            # [1 2]
print(temperature.coverage)    # [0.5 0.5] (the end bins extend half a step past the data)
print(counter.samples_used[0]) # records contributing to the first bin
```

To drive the whole process from a CDF file, use `resample_cdf`, which resolves
each variable's own time axis through its `DEPEND_0` attribute and its
`FILLVAL` attribute automatically.

## Reference

::: cdflib.resample
    options:
      show_root_heading: false
