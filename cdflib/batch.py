"""Compatibility alias for :mod:`cdflib.ingest`.

The batch helpers are implemented in :mod:`cdflib.ingest`; this module simply
re-exports them so callers can use either name.
"""

from cdflib.ingest import (  # noqa: F401
    SPARSE_MISSING,
    SPARSE_WRITTEN,
    IngestedBatch,
    MergedBatch,
    MergedSample,
    MergedVariable,
    ingest,
    merge,
    merge_batches,
    read_batch,
    read_batches,
    reexport,
    validate_batches,
    write_batch,
)

__all__ = [
    "IngestedBatch",
    "MergedBatch",
    "MergedSample",
    "MergedVariable",
    "ingest",
    "read_batch",
    "read_batches",
    "merge_batches",
    "merge",
    "reexport",
    "write_batch",
    "validate_batches",
    "SPARSE_WRITTEN",
    "SPARSE_MISSING",
]
