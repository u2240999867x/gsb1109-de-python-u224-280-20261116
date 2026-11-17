# variable_view

`cdflib.variable_view` provides a traceable tensor view over a single CDF variable's
physical storage.  It reconciles the CDF declarations (`Rec_Vary`, `Dim_Vary`,
`Dim_Sizes`, file majority) with the array returned by
[`varget`](cdfread.md#cdflib.cdfread.CDF.varget), where non-varying axes are removed.

## Why a separate view

* Non-varying dimensions collapse in storage but stay in the declared logical tensor.
* Column-major files number the bytes inside a record differently, even though
  `varget` returns the data in logical axis order.
* Slicing or transposing the tensor should not lose the link back to the original CDF
  record and in-record offset.
* Empty variables keep their declared logical shape instead of reverse-guessing the
  dimensions from an empty array.

## Building a view

```python
from cdflib import CDF
from cdflib.variable_view import VariableView

cdf = CDF("example.cdf")
info = cdf.cdf_info()
properties = cdf.varinq("temperature")
view = VariableView.from_variable(properties, cdf.varget("temperature"), majority=info.Majority)

# rVariables drop non-varying axes from the VDR, so pass the full declaration
r_view = VariableView.from_variable(
    r_properties,
    cdf.varget("r_temperature"),
    majority=info.Majority,
    dim_sizes=info.rDim_sizes,
)
```

`VariableView.from_storage` builds the same view from a `varget` array plus explicit
flags, and `VariableView.from_full_tensor` validates a fully shaped tensor against the
declarations.  Contradictory input raises `StructureConflictError`.

## Tracing values back to storage

```python
value, observation = view.trace((record, dim0, dim1))
print(observation.record, observation.record_index, observation.physical_index)
```

`Observation.physical_index` is the flat offset inside the record, numbered according to
the file majority.  `Observation.broadcast` marks positions that reuse a stored value
through a non-varying axis.  `num_measurements` only counts values actually present in
the file, while `num_logical_positions` includes the broadcast slots.

Slicing and transposition return derived views whose backtraces still point at the
original records:

```python
sliced = view.isel((slice(0, 10), 0, slice(None)))
moved = view.transpose((2, 1, 0))
```

## Reference

::: cdflib.variable_view
