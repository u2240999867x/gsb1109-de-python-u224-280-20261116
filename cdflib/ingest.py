"""Ingest CDF variables together with sparse-record provenance.

A CDF sparse variable does not store one value per logical record.  Records
marked ``pad_sparse`` are synthesized from the variable's pad value, and
records marked ``prev_sparse`` reuse the last physically written record.  A
plain ``varget`` call hides that distinction, which makes it impossible to
tell a real observation from a synthesized one when the values are later
re-exported.

This module keeps the distinction explicit:

* :func:`ingest` reads one CDF file into :class:`IngestedBatch`.
* :func:`merge_batches` combines several files into :class:`MergedBatch`,
  preserving each variable's own time axis.
* :func:`reexport` writes physical samples back to a new CDF without turning
  synthesised values into written ones.

Reads are always scoped to one file: a ``previous`` value must come from an
earlier physical record in the same CDF.  If no such record exists the sample
is marked ``missing`` and the file's pad value is returned; no value is
borrowed from another batch file.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple, Union

import numpy as np

from cdflib.cdfread import CDF
from cdflib.cdfwrite import CDF as CDFWriter
from cdflib.dataclasses import (
    SPARSE_MISSING,
    SPARSE_WRITTEN,
    SparseData,
    SparseValue,
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


@dataclass
class IngestedBatch:
    """All variables from one CDF file, each with per-sample provenance."""

    #: Path of the ingested CDF.
    path: str
    #: ``variable -> SparseData`` for record-varying variables.
    variables: Dict[str, SparseData] = field(default_factory=dict)
    #: ``variable -> value`` for non-record-varying variables.
    constants: Dict[str, Any] = field(default_factory=dict)
    #: Global attributes read from the file.
    global_attributes: Dict[str, Any] = field(default_factory=dict)
    #: Variable attributes read from the file.
    variable_attributes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: The CDF file specification used to create the source file.
    cdf_spec: Dict[str, Any] = field(default_factory=dict)
    #: ``varinq`` metadata for each variable.
    variable_info: Dict[str, Any] = field(default_factory=dict)

    def status(self, variable: str, record: int) -> str:
        """Return the provenance status for one logical record."""
        return self.variables[variable].provenance[record].status

    def sample(self, variable: str, record: int) -> SparseValue:
        """Return the full provenance record for one logical record."""
        return self.variables[variable].provenance[record]

    def records(self) -> Iterator[Dict[str, Any]]:
        """Yield one flat dictionary per logical sample for ingestion.

        ``value`` is the logical value returned by normal reads.  ``status``
        and ``source_record`` preserve whether that value was physically
        written or synthesized, and which written record supplied a
        ``previous`` value.
        """
        for variable, sparse in self.variables.items():
            values = np.asarray(sparse.data)
            for index, sample in enumerate(sparse.provenance):
                yield {
                    "variable": variable,
                    "record": sample.record,
                    "value": values[index],
                    "status": sample.status,
                    "source_record": sample.source_record,
                    "source_file": self.path,
                }


@dataclass
class MergedSample:
    """One logical sample in a merged batch."""

    variable: str
    axis: Any
    value: Any
    status: str
    origin: Optional[str] = None
    #: Logical record number in the originating file.
    origin_record: Optional[int] = None
    #: Physical record that supplied the value, if any.
    source_record: Optional[int] = None
    #: Position of this sample in the merged variable.
    merged_record: Optional[int] = None

    @property
    def is_written(self) -> bool:
        return self.status == SPARSE_WRITTEN

    @property
    def is_virtual(self) -> bool:
        return not self.is_written

    @property
    def source_file(self) -> Optional[str]:
        return self.origin


@dataclass
class MergedVariable:
    """A merged variable with the samples of every contributing batch."""

    name: str
    axis_name: Optional[str]
    samples: List[MergedSample] = field(default_factory=list)
    info: Any = None
    epochs: Optional[List[Any]] = None
    #: ``(start, end)`` index pairs of logical samples with no evidence.
    gaps: List[Tuple[int, int]] = field(default_factory=list)

    @property
    def values(self) -> np.ndarray:
        return np.asarray([sample.value for sample in self.samples])

    @property
    def data(self) -> np.ndarray:
        return self.values

    @property
    def axis(self) -> np.ndarray:
        if self.epochs is not None:
            return np.asarray(self.epochs)
        return np.asarray([sample.axis for sample in self.samples])

    @property
    def provenance(self) -> List[MergedSample]:
        return self.samples

    @property
    def statuses(self) -> List[str]:
        return [sample.status for sample in self.samples]

    @property
    def written(self) -> np.ndarray:
        return np.asarray([sample.is_written for sample in self.samples], dtype=bool)

    @property
    def virtual(self) -> np.ndarray:
        return ~self.written

    def __len__(self) -> int:
        return len(self.samples)

    def __iter__(self) -> Iterator[MergedSample]:
        return iter(self.samples)

    def __getitem__(self, item):
        return self.samples[item]


@dataclass
class MergedBatch:
    """Several CDF files merged without losing per-sample provenance."""

    path: str = "<merged>"
    #: ``variable -> MergedVariable`` for record-varying variables.
    variables: Dict[str, MergedVariable] = field(default_factory=dict)
    #: ``variable -> value`` for non-record-varying variables.
    constants: Dict[str, Any] = field(default_factory=dict)
    #: Non-record variables whose values differed between batches.
    conflicts: List[str] = field(default_factory=list)
    #: One entry per contributing file.
    sources: List[str] = field(default_factory=list)

    def __getitem__(self, variable: str) -> MergedVariable:
        return self.variables[variable]

    def __contains__(self, variable: str) -> bool:
        return variable in self.variables

    def status(self, variable: str, record: int) -> str:
        return self.variables[variable].samples[record].status

    def sample(self, variable: str, record: int) -> MergedSample:
        return self.variables[variable].samples[record]

    def time(self, axis_name: Optional[str] = None) -> np.ndarray:
        """Values of the requested axis, or of the first axis found."""
        if axis_name is not None:
            return self.variables[axis_name].axis
        for name, variable in self.variables.items():
            if variable.axis_name == name:
                return variable.axis
        raise KeyError("No time axis available")

    def records(self) -> Iterator[Dict[str, Any]]:
        for variable in self.variables.values():
            for sample in variable.samples:
                yield {
                    "variable": sample.variable,
                    "axis": sample.axis,
                    "value": sample.value,
                    "status": sample.status,
                    "source_file": sample.origin,
                    "source_record": sample.source_record,
                }

    def to_dataframe(self, variable: Optional[str] = None):
        """Return a long-form pandas DataFrame of merged samples."""
        import pandas as pd

        rows = list(self.records()) if variable is None else [
            {
                "variable": sample.variable,
                "axis": sample.axis,
                "value": sample.value,
                "status": sample.status,
                "source_file": sample.origin,
                "source_record": sample.source_record,
            }
            for sample in self.variables[variable].samples
        ]
        return pd.DataFrame(rows, columns=["variable", "axis", "value", "status", "source_file", "source_record"])

    def to_wide_dataframe(self, variable: str):
        """Return values and provenance columns keyed by the variable's axis."""
        import pandas as pd

        merged = self.variables[variable]
        return pd.DataFrame(
            {
                "axis": merged.axis,
                "value": merged.values,
                "status": merged.statuses,
                "source_file": [sample.origin for sample in merged.samples],
                "source_record": [sample.source_record for sample in merged.samples],
            }
        )


def ingest(path: Union[str, Path]) -> IngestedBatch:
    """Read a CDF file into logical samples with explicit provenance."""
    cdf = CDF(path)
    info = cdf.cdf_info()
    names = list(info.zVariables) + list(info.rVariables)

    batch = IngestedBatch(path=str(path))
    batch.global_attributes = cdf.globalattsget()
    batch.cdf_spec = {
        "Majority": info.Majority,
        "Encoding": info.Encoding,
        "Checksum": info.Checksum,
        "rDim_sizes": info.rDim_sizes,
    }

    for name in names:
        var_info = cdf.varinq(name)
        batch.variable_info[name] = var_info
        batch.variable_attributes[name] = cdf.varattsget(name)
        if var_info.Rec_Vary:
            batch.variables[name] = cdf.varget_provenance(name)
        else:
            batch.constants[name] = cdf.varget(name)
    return batch


def _axis_for(variable: str, info: Any, attrs: Dict[str, Any], available: Sequence[str]) -> Optional[str]:
    """Determine which variable supplies this variable's logical axis."""
    if variable in attrs and "DEPEND_0" in attrs[variable]:
        axis = attrs[variable]["DEPEND_0"]
        if isinstance(axis, str) and axis in available:
            return axis
    if info.Data_Type in (31, 32, 33):
        return variable
    return None


def _validate(batches: Sequence[IngestedBatch]) -> None:
    if not batches:
        raise ValueError("At least one batch is required")
    for batch in batches:
        if not Path(batch.path).is_file():
            raise FileNotFoundError(batch.path)
        if not batch.variable_info:
            raise ValueError(f"Batch contains no variables: {batch.path}")


def _check_nonrecord(batches: Sequence[IngestedBatch]) -> List[str]:
    """Verify non-record variables agree and keep the same shape."""
    values: Dict[str, Any] = {}
    conflicts: List[str] = []
    for batch in batches:
        for name, value in batch.constants.items():
            array = np.asarray(value)
            key = (array.shape, tuple(array.reshape(-1).tolist()))
            if name in values and values[name] != key:
                conflicts.append(name)
            values.setdefault(name, key)
    return sorted(set(conflicts))


def _check_record_variance(batches: Sequence[IngestedBatch]) -> None:
    variances: Dict[str, bool] = {}
    for batch in batches:
        for name, info in batch.variable_info.items():
            vary = bool(info.Rec_Vary)
            if name in variances and variances[name] != vary:
                raise ValueError(f"Variable {name!r} changes record variance between batches")
            variances[name] = vary


def _definition(info: Any) -> Tuple[Any, ...]:
    """Structural definition that must not change between batches."""
    return (
        info.Data_Type,
        info.Num_Elements,
        tuple(info.Dim_Sizes),
        tuple(bool(value) for value in info.Dim_Vary),
        bool(info.Rec_Vary),
    )


def _check_variable_definitions(batches: Sequence[IngestedBatch]) -> None:
    definitions: Dict[str, Tuple[Any, ...]] = {}
    for batch in batches:
        for name, info in batch.variable_info.items():
            definition = _definition(info)
            if name in definitions and definitions[name] != definition:
                raise ValueError(f"Variable {name!r} changes its definition between batches")
            definitions[name] = definition


def merge_batches(paths: Sequence[Union[str, Path]], strict: bool = True) -> MergedBatch:
    """Merge several CDF batches while preserving sparse provenance.

    Each variable keeps the axis named by its own ``DEPEND_0`` attribute, so a
    batch may carry several active time axes.  Timestamps are not deduplicated:
    two batches that cover the same instant contribute two logical samples, in
    input order.  A variable missing from a batch contributes no rows for that
    batch; a variable shorter than its axis in a batch contributes explicit
    ``missing`` rows for the uncovered tail rather than borrowing values.

    Non-record variables must agree across batches.  A conflict raises
    ``ValueError`` when ``strict`` is true, and is recorded in
    :attr:`MergedBatch.conflicts` otherwise.
    """
    batches = [ingest(path) for path in paths]
    _validate(batches)
    _check_record_variance(batches)
    _check_variable_definitions(batches)
    conflicts = _check_nonrecord(batches)
    if conflicts and strict:
        raise ValueError(f"Conflicting non-record variables across batches: {', '.join(conflicts)}")

    merged = MergedBatch(sources=[batch.path for batch in batches], conflicts=conflicts)

    # Non-record variables are batch-level values, not samples.
    for batch in batches:
        for name, value in batch.constants.items():
            merged.constants.setdefault(name, value)

    variable_names: List[str] = []
    for batch in batches:
        for name in batch.variable_info:
            if name not in variable_names:
                variable_names.append(name)

    for name in variable_names:
        info = next(batch.variable_info[name] for batch in batches if name in batch.variable_info)
        if not info.Rec_Vary:
            continue
        # The axis is a property of the variable's dataset, not of a single
        # batch, so resolve it once from the first batch that defines it.
        axis_name = _axis_for(name, info, next(b.variable_attributes for b in batches if name in b.variable_info), [
            var for batch in batches for var in batch.variable_info
        ])
        samples: List[MergedSample] = []
        epochs: List[Any] = []
        for batch in batches:
            axis_values = _batch_axis(batch, axis_name)
            if name not in batch.variables:
                # Missing from this batch: leave an explicit coverage gap.
                if axis_values is None:
                    continue
                for axis_value in axis_values:
                    samples.append(
                        MergedSample(
                            variable=name,
                            axis=axis_value,
                            value=None,
                            status=SPARSE_MISSING,
                            origin=batch.path,
                            origin_record=None,
                            source_record=None,
                        )
                    )
                    epochs.append(axis_value)
                continue
            sparse = batch.variables[name]
            values = np.asarray(sparse.data)
            for index, sample in enumerate(sparse.provenance):
                axis_value = axis_values[index] if axis_values is not None and index < len(axis_values) else None
                samples.append(
                    MergedSample(
                        variable=name,
                        axis=axis_value,
                        value=values[index],
                        status=sample.status,
                        origin=batch.path,
                        origin_record=sample.record,
                        source_record=sample.source_record,
                    )
                )
                epochs.append(axis_value)
            if axis_values is not None and len(sparse.provenance) < len(axis_values):
                # The variable stops before its axis does.  Preserve the gap.
                for index in range(len(sparse.provenance), len(axis_values)):
                    samples.append(
                        MergedSample(
                            variable=name,
                            axis=axis_values[index],
                            value=None,
                            status=SPARSE_MISSING,
                            origin=batch.path,
                            origin_record=index,
                            source_record=None,
                        )
                    )
                    epochs.append(axis_values[index])
        merged.variables[name] = MergedVariable(
            name=name, axis_name=axis_name, samples=samples, info=info, epochs=epochs
        )
        merged.variables[name].gaps = _gaps(samples)

    _sort_by_axis(merged)
    return merged


def _sortable(value: Any) -> Tuple[int, Any]:
    """Sort key that orders real axis values deterministically and last."""
    if value is None:
        return (1, 0)
    try:
        return (0, value)
    except Exception:  # pragma: no cover - exotic axis types
        return (0, str(value))


def validate_batches(paths: Sequence[Union[str, Path]]) -> List[str]:
    """Return structural problems that would prevent a lossless merge.

    Checks for duplicate variable names with incompatible record variance and
    non-record variables whose values disagree across batches.  An empty list
    means the batches can be merged.
    """
    batches = [ingest(path) for path in paths]
    _validate(batches)
    problems: List[str] = []
    for check in (_check_record_variance, _check_variable_definitions):
        try:
            check(batches)
        except ValueError as error:
            problems.append(str(error))
    problems.extend(f"non-record conflict: {name}" for name in _check_nonrecord(batches))
    return problems


def _sort_by_axis(merged: MergedBatch) -> None:
    """Order each variable's samples by axis, preserving duplicate order.

    ``MergedVariable.epochs`` mirrors the axis of every sample, including
    samples whose own axis value was unavailable, so it must be reordered with
    the samples.  Python's sort is stable, so duplicate timestamps keep their
    input order.
    """
    for variable in merged.variables.values():
        if variable.axis_name is None:
            continue
        if any(sample.axis is None for sample in variable.samples):
            continue
        order = sorted(range(len(variable.samples)), key=lambda i: _sortable(variable.samples[i].axis))
        if order == list(range(len(variable.samples))):
            continue
        variable.samples = [variable.samples[i] for i in order]
        if variable.epochs is not None:
            variable.epochs = [variable.epochs[i] for i in order]
        for new_index, sample in enumerate(variable.samples):
            sample.merged_record = new_index
        variable.gaps = _gaps(variable.samples)


def _gaps(samples: Sequence[MergedSample]) -> List[Tuple[int, int]]:
    """Return inclusive index ranges of consecutive missing samples."""
    ranges: List[Tuple[int, int]] = []
    start: Optional[int] = None
    for index, sample in enumerate(samples):
        if sample.status == SPARSE_MISSING:
            if start is None:
                start = index
        elif start is not None:
            ranges.append((start, index - 1))
            start = None
    if start is not None:
        ranges.append((start, len(samples) - 1))
    return ranges


def _batch_axis(batch: IngestedBatch, axis_name: Optional[str]) -> Optional[np.ndarray]:
    if axis_name is None:
        return None
    if axis_name in batch.variables:
        return np.asarray(batch.variables[axis_name].data)
    info = batch.variable_info.get(axis_name)
    if info is not None and not info.Rec_Vary:
        return np.asarray([batch.constants.get(axis_name)])
    return None


def _var_spec(name: str, info: Any) -> Dict[str, Any]:
    spec: Dict[str, Any] = {
        "Variable": name,
        "Var_Type": "zVariable" if info.Var_Type.lower() == "zvariable" else "rVariable",
        "Data_Type": info.Data_Type,
        "Num_Elements": info.Num_Elements,
        "Rec_Vary": bool(info.Rec_Vary),
        "Sparse": info.Sparse.lower(),
        "Compress": info.Compress,
    }
    if info.Var_Type.lower() == "zvariable":
        spec["Dim_Sizes"] = list(info.Dim_Sizes)
    else:
        spec["Dim_Vary"] = list(info.Dim_Vary)
    if info.Pad is not None:
        spec["Pad"] = info.Pad
    if info.Block_Factor is not None:
        spec["Block_Factor"] = info.Block_Factor
    return spec


def _physical_selection(sparse: SparseData, sparse_token: str):
    """Return physical record numbers and their values for re-export."""
    values = np.asarray(sparse.data)
    records = [sample.record for sample in sparse.provenance if sample.status == SPARSE_WRITTEN]
    if sparse_token == "no_sparse":
        return None, values
    if not records:
        return np.asarray([], dtype=np.int64), values[:0]
    return np.asarray(records, dtype=np.int64), values[records]


def _attribute_entries(values: Dict[str, Any]) -> Dict[str, Any]:
    """Convert ``globalattsget`` output back to writer input format.

    The reader returns one flat list per attribute, while the writer expects a
    mapping from entry number to value.  For global attributes the entry
    numbers carry no meaning, so positional numbering preserves all entries.
    """
    converted: Dict[str, Any] = {}
    for name, value in values.items():
        if isinstance(value, dict):
            converted[name] = value
            continue
        sequence = value if isinstance(value, (list, tuple)) else [value]
        converted[name] = {index: entry for index, entry in enumerate(sequence)}
    return converted


def reexport(batch: IngestedBatch, path: Union[str, Path]) -> None:
    """Write an ingested batch to a new CDF without losing provenance.

    Only samples marked ``written`` become physical records.  Virtual
    ``pad``/``previous`` samples are omitted so the new file's sparseness rules
    regenerate them.  A ``missing`` sample stays missing because no preceding
    physical record is invented for it.  A written value equal to the pad
    value stays written.
    """
    writer = CDFWriter(path, cdf_spec=batch.cdf_spec)
    try:
        writer.write_globalattrs(_attribute_entries(batch.global_attributes))
        for name, sparse in batch.variables.items():
            info = batch.variable_info[name]
            token = info.Sparse.lower()
            records, values = _physical_selection(sparse, token)
            if token == "no_sparse":
                var_data = values
            else:
                var_data = [records, values]
            writer.write_var(
                _var_spec(name, info),
                var_attrs=batch.variable_attributes.get(name) or None,
                var_data=var_data,
            )
        for name, value in batch.constants.items():
            info = batch.variable_info[name]
            writer.write_var(
                _var_spec(name, info),
                var_attrs=batch.variable_attributes.get(name) or None,
                var_data=value,
            )
    finally:
        writer.close()


#: Read one file as logical samples with provenance.
read_batch = ingest


def read_batches(paths: Sequence[Union[str, Path]]) -> List[IngestedBatch]:
    """Read several CDF files independently, one provenance scope per file."""
    return [ingest(path) for path in paths]


#: Merge several files while preserving provenance.
merge = merge_batches

#: Write an ingested batch back to CDF.
write_batch = reexport
