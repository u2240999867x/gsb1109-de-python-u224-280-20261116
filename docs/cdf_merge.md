# Merging CDF Batches

`merge_cdf_batches` combines two or more CDF files that cover the same logical
dataset into a single `xarray.Dataset`.  The merge preserves every observation
exactly as written, so the output can always be traced back to the originating
batch and CDF record.

## Merge semantics

* **Per-variable time axes.** Each record-varying variable is concatenated
  along the axis it actually depends on (`DEPEND_0`, or an epoch-type
  variable).  Variables backed by different timelines keep separate
  dimensions; no common clock is synthesized.
* **Duplicate times are kept, not de-duplicated.** When two batches contain
  the same timestamp, both samples survive in batch order.  Every record
  dimension carries `<axis>__source_batch` and `<axis>__source_record`
  variables that identify the originating batch label and original CDF record
  number.  No interpolation is performed.
* **Sparse records stay honest.** Sparse variables keep their in-file values
  and gain a `<variable>__physical` boolean marking physically written versus
  virtual records.  Each batch is expanded independently, so previous-sparse
  fill never carries a value across an undeclared file boundary.
* **Non-record-varying variables are never broadcast.** When the batches hold
  different values for the same non-record-varying variable, per-batch
  versions named `<variable>__batch<label>` are emitted (or a `ValueError` is
  raised with `on_nonrecord_conflict="raise"`).  The first file's constant is
  never silently reused.
* **Missing variables leave coverage gaps.** A variable absent from a batch
  contributes no fabricated rows.  It keeps only its real samples on its own
  record dimension, and a `<variable>__coverage` boolean on the shared time
  axis marks the batches that provide it.

The dataset attributes `CDF_MERGE_BATCHES` and `CDF_MERGE_POLICY` record the
source files, per-variable record counts, sparseness, and the merge policy.

::: cdflib.xarray.cdf_merge.merge_cdf_batches
