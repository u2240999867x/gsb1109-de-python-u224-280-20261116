# Sparse provenance and batch ingest

A CDF sparse variable can produce a value for a logical record that was never
written to the file. `pad_sparse` records are filled with the variable's pad
value; `prev_sparse` records repeat the last physically written record before
them. A normal `varget` call returns those values without saying where they
came from.

`cdflib` keeps that distinction available on demand.

## Per-record provenance

```python
import cdflib

cdf = cdflib.CDF("input.cdf")
result = cdf.varget_provenance("quality")   # or varget(..., return_provenance=True)

result.data          # logical values, the same as varget
result.statuses      # ["written", "previous", ...]
result.source_records
for sample in result.provenance:
    print(sample.record, sample.status, sample.source_record)
```

`status` is one of:

| Status | Meaning |
|---|---|
| `written` | the record is physically stored in this file |
| `pad` | the value was generated from the pad value (`pad_sparse`) |
| `previous` | the value repeats an earlier physical record (`prev_sparse`) |
| `missing` | no physical value and no usable predecessor |

Status is derived from the file's variable index records, never by comparing a
value with the pad value. An observation that is explicitly written and happens
to equal the pad value is still `written`.

`source_record` is the zero-based record that actually supplied the value. For
`previous` it points at the earlier physical record, which may lie before the
requested window:

```python
window = cdf.varget_provenance("quality", startrec=2, endrec=3)
# window.provenance[0].source_record may be 0, outside the window
```

The search never crosses file boundaries. If a `prev_sparse` record has no
predecessor in the same file, it is `missing`: the pad value is returned, but
no evidence is borrowed from another batch.

## Batch ingest and re-export

```python
from cdflib.ingest import ingest, merge_batches, reexport

batch = ingest("batch_a.cdf")
for row in batch.records():
    print(row["variable"], row["record"], row["status"], row["source_record"])

merged = merge_batches(["batch_a.cdf", "batch_b.cdf"])
merged["quality"].statuses
merged["quality"].gaps

reexport(batch, "copy.cdf")
```

`merge_batches` keeps each variable on its own `DEPEND_0` axis, retains
duplicate timestamps, and marks uncovered records as `missing` rather than
filling them from a neighbouring batch. Non-record variables must agree across
batches; a conflict raises `ValueError` unless `strict=False`, in which case it
is listed in `merged.conflicts`.

`reexport` writes only `written` samples as physical records. Virtual `pad`,
`previous` and `missing` samples are omitted so the new file's sparseness rules
regenerate them. A real observation equal to the pad value stays a physical
record.
