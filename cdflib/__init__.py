from pathlib import Path

from cdflib.cdfread import CDF
from . import cdfread, ingest
from .dataclasses import SparseData, SparseValue
from .epochs import CDFepoch as cdfepoch  # noqa: F401

__all__ = ["CDF", "SparseData", "SparseValue", "ingest"]


def __getattr__(name):
    # Keep the module namespace reserved for ``cdflib.ingest``; expose the
    # common helpers at package level as well.
    if name in ("read_batch", "read_batches", "merge_batches", "reexport", "validate_batches"):
        return getattr(ingest, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


try:
    from ._version import version as __version__
except Exception:
    __version__ = "unknown"
