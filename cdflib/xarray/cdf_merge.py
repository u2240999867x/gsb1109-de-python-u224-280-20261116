"""
Merge several CDF batches into a single logical xarray.Dataset.

The merge is intentionally conservative:

* Variables are concatenated along the record/time axis they actually depend
  on (``DEPEND_0``), so variables backed by different timelines keep separate
  dimensions instead of being forced onto a common clock.
* Duplicate time values are kept.  Every output sample carries explicit
  provenance (source batch label and original CDF record number); nothing is
  deduplicated or interpolated.
* Sparse variables keep their in-file values and gain a companion boolean
  variable marking physical versus virtual records.  Previous-sparse fill
  never crosses a file boundary, because each batch is expanded independently
  before the per-batch arrays are concatenated.
* Non-record-varying variables are never broadcast across records.  When the
  batches disagree, separate per-batch versions of the variable are emitted
  instead of silently keeping the value from the first file.
* A variable missing from a batch leaves an explicit coverage gap: the
  variable keeps only its real samples, and a boolean coverage variable on the
  shared time axis marks the batches that do not provide it.
"""

import json
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

import numpy as np
import numpy.typing as npt
import xarray as xr

from cdflib import CDF
from cdflib.dataclasses import VDRInfo

TIME_DATA_TYPES = ("CDF_EPOCH", "CDF_EPOCH16", "CDF_TIME_TT2000")

SOURCE_BATCH_ATTR = "SOURCE_BATCHES"
SOURCE_BATCH_LABEL_ATTR = "SOURCE_BATCH_LABEL"


@dataclass
class _Batch:
    label: str
    path: str
    properties: Dict[str, VDRInfo] = field(default_factory=dict)
    attributes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    data: Dict[str, npt.NDArray] = field(default_factory=dict)
    physical: Dict[str, npt.NDArray[np.bool_]] = field(default_factory=dict)


def _unique_name(taken: Sequence[str], candidate: str) -> str:
    if candidate not in taken:
        return candidate
    suffix = 1
    while f"{candidate}_{suffix}" in taken:
        suffix += 1
    return f"{candidate}_{suffix}"


def _physical_record_mask(cdf: CDF, variable: str, properties: VDRInfo) -> npt.NDArray[np.bool_]:
    """
    Returns a boolean mask of the records that were physically written to the
    CDF.  Non-sparse variables have a physical record at every record number.
    """
    num_records = properties.Last_Rec + 1
    if properties.Sparse == "No_sparse":
        return np.ones(num_records, dtype=bool)

    vdr = cdf.vdr_info(variable)
    if cdf.cdfversion == 3:
        _, block_starts, block_ends = cdf._read_vxrs(vdr.head_vxr, [], [], [])
    else:
        _, block_starts, block_ends = cdf._read_vxrs2(vdr.head_vxr, [], [], [])

    mask = np.zeros(num_records, dtype=bool)
    for start, end in zip(block_starts, block_ends):
        start = max(start, 0)
        end = min(end, num_records - 1)
        if start <= end:
            mask[start : end + 1] = True
    return mask


def _load_batch(path: Union[str, Path], label: str) -> _Batch:
    batch = _Batch(label=label, path=str(path))
    with CDF(path, string_encoding="latin-1") as cdf:
        info = cdf.cdf_info()
        variable_names = list(info.rVariables) + list(info.zVariables)
        for name in variable_names:
            properties = cdf.varinq(name)
            batch.properties[name] = properties
            batch.attributes[name] = dict(cdf.varattsget(name))
            if properties.Last_Rec < 0:
                # No records were ever written, so the batch contributes nothing.
                continue
            data = np.asarray(cdf.varget(name))
            batch.data[name] = data
            if properties.Rec_Vary:
                batch.physical[name] = _physical_record_mask(cdf, name, properties)
    return batch


def _is_named_depend_0(name: str, batch: _Batch) -> bool:
    for attributes in batch.attributes.values():
        depend_0 = attributes.get("DEPEND_0")
        if isinstance(depend_0, str) and depend_0 == name:
            return True
    return False


def _is_axis_candidate(name: str, batch: _Batch) -> bool:
    properties = batch.properties[name]
    if name not in batch.data or not properties.Rec_Vary:
        return False
    if batch.data[name].ndim != 1:
        return False
    return properties.Data_Type_Description in TIME_DATA_TYPES or _is_named_depend_0(name, batch)


def _discover_axes(batches: Sequence[_Batch]) -> List[str]:
    axes: List[str] = []
    for batch in batches:
        for name in batch.properties:
            if name not in axes and _is_axis_candidate(name, batch):
                axes.append(name)
    return axes


def _resolve_axis(name: str, batch: _Batch, axes: Sequence[str]) -> Optional[str]:
    depend_0 = batch.attributes.get(name, {}).get("DEPEND_0")
    if isinstance(depend_0, str) and depend_0 in axes and depend_0 in batch.data:
        return depend_0
    if name in axes:
        return name
    if len(axes) == 1 and axes[0] in batch.data:
        return axes[0]
    return None


def _arrays_equal(left: npt.NDArray, right: npt.NDArray) -> bool:
    if left.shape != right.shape:
        return False
    return bool(np.array_equal(left, right))


def _provenance_variables(dim_name: str, batches: Sequence[_Batch], record_counts: Sequence[int]) -> Dict[str, Any]:
    source_batch = np.concatenate(
        [np.full(count, batch.label, dtype=object) for batch, count in zip(batches, record_counts)]
    )
    source_record = np.concatenate([np.arange(count, dtype=np.int64) for count in record_counts])
    return {
        f"{dim_name}__source_batch": (
            (dim_name,),
            source_batch,
            {"LONG_NAME": "Source CDF batch label"},
        ),
        f"{dim_name}__source_record": (
            (dim_name,),
            source_record,
            {"LONG_NAME": "Original record number in the source CDF"},
        ),
    }


def merge_cdf_batches(
    paths: Sequence[Union[str, Path]],
    batch_labels: Optional[Sequence[str]] = None,
    on_nonrecord_conflict: str = "version",
) -> xr.Dataset:
    """
    Merge two or more CDF batches into one logical xarray.Dataset.

    Parameters
    ----------
    paths :
        CDF files, in merge order.  The interface is built around two batches,
        but any number of files is accepted.
    batch_labels :
        Short labels used in the provenance variables.  Defaults to ``"0"``,
        ``"1"``, ... when not supplied.
    on_nonrecord_conflict :
        How to handle a non-record-varying variable whose value differs between
        batches.  ``"version"`` (the default) keeps one suffixed variable per
        batch; ``"raise"`` raises a ``ValueError`` instead.  The value from the
        first batch is never silently reused.

    Returns
    -------
    xarray.Dataset
        A dataset with one dimension per discovered record/time axis.  Every
        record dimension carries ``<axis>__source_batch`` and
        ``<axis>__source_record`` provenance variables.  Dataset attributes
        describe the source batches and the merge policy.
    """
    if on_nonrecord_conflict not in ("version", "raise"):
        raise ValueError("on_nonrecord_conflict must be either 'version' or 'raise'")
    if len(paths) < 2:
        raise ValueError("At least two CDF batches are required")

    if batch_labels is None:
        batch_labels = [str(index) for index in range(len(paths))]
    if len(batch_labels) != len(paths):
        raise ValueError("batch_labels must have the same length as paths")
    if len(set(batch_labels)) != len(batch_labels):
        raise ValueError("batch_labels must be unique")

    batches = [_load_batch(path, label) for path, label in zip(paths, batch_labels)]
    axes = _discover_axes(batches)

    data_vars: Dict[str, Any] = {}
    coord_vars: Dict[str, Any] = {}
    used_names: List[str] = []

    # Build the concatenated axes and their per-sample provenance variables.
    axis_dim_names: Dict[str, str] = {}
    axis_batches: Dict[str, List[_Batch]] = {}
    for axis in axes:
        contributors = [batch for batch in batches if axis in batch.data]
        if not contributors:
            continue
        axis_batches[axis] = contributors
        dim_name = _unique_name(used_names, axis)
        axis_dim_names[axis] = dim_name
        used_names.append(dim_name)

        values = np.concatenate([batch.data[axis] for batch in contributors])
        attributes = dict(contributors[0].attributes[axis])
        attributes[SOURCE_BATCH_ATTR] = [batch.label for batch in contributors]
        coord_vars[dim_name] = ((dim_name,), values, attributes)

        record_counts = [batch.data[axis].shape[0] for batch in contributors]
        for provenance_name, variable in _provenance_variables(dim_name, contributors, record_counts).items():
            provenance_name = _unique_name(used_names, provenance_name)
            used_names.append(provenance_name)
            data_vars[provenance_name] = variable

        sparse_axis = [batch for batch in contributors if batch.properties[axis].Sparse != "No_sparse"]
        if sparse_axis:
            physical_name = _unique_name(used_names, f"{dim_name}__physical")
            used_names.append(physical_name)
            physical = np.concatenate([batch.physical[axis] for batch in contributors])
            data_vars[physical_name] = (
                (dim_name,),
                physical,
                {
                    "LONG_NAME": "True where the record was physically written to the source CDF",
                    "CDF_SPARSE": [batch.properties[axis].Sparse for batch in sparse_axis],
                },
            )

    # Record-varying data variables.
    variable_names: List[str] = []
    for batch in batches:
        for name in batch.data:
            if name not in variable_names and name not in axis_batches:
                variable_names.append(name)

    for name in variable_names:
        if not any(batch.properties[name].Rec_Vary for batch in batches if name in batch.properties):
            continue
        if any(not batch.properties[name].Rec_Vary for batch in batches if name in batch.properties):
            raise ValueError(
                f"Variable {name} is record-varying in some batches and non-record-varying in others; "
                "the batches cannot be merged without changing its semantics"
            )

        contributors = [batch for batch in batches if name in batch.data]
        resolved_axes = {_resolve_axis(name, batch, axes) for batch in contributors}
        resolved_axes.discard(None)
        if len(resolved_axes) > 1:
            raise ValueError(
                f"Variable {name} resolves to multiple record axes ({sorted(resolved_axes)}) across batches; "
                "reconcile the DEPEND_0 metadata before merging"
            )

        attributes = dict(contributors[0].attributes[name])
        attributes[SOURCE_BATCH_ATTR] = [batch.label for batch in contributors]
        missing_batches = [batch for batch in batches if name not in batch.data]

        axis_name = resolved_axes.pop() if resolved_axes else None
        fits_axis = False
        if axis_name is not None and axis_name in axis_batches:
            # The variable can share the concatenated axis only when it covers
            # every batch that contributes rows to that axis and its record
            # counts line up batch by batch.  Partial coverage (the variable is
            # missing from an axis batch) stays on its own record dimension.
            axis_labels = {batch.label for batch in axis_batches[axis_name]}
            contributor_labels = {batch.label for batch in contributors}
            fits_axis = axis_labels.issubset(contributor_labels) and all(
                batch.data[name].shape[0] == batch.data[axis_name].shape[0] for batch in contributors
            )
            if not fits_axis:
                warnings.warn(
                    f"Variable {name} does not fully cover axis {axis_name}; "
                    "placing it on its own record dimension with a coverage flag"
                )

        trailing_dim_count = max(batch.data[name].ndim - 1 for batch in contributors)
        trailing_dims = [f"{name}__dim{index}" for index in range(trailing_dim_count)]

        if fits_axis:
            record_dim = axis_dim_names[axis_name]
        else:
            record_dim = _unique_name(used_names, f"{name}__record")
            used_names.append(record_dim)
            record_counts = [batch.data[name].shape[0] for batch in contributors]
            for provenance_name, variable in _provenance_variables(record_dim, contributors, record_counts).items():
                provenance_name = _unique_name(used_names, provenance_name)
                used_names.append(provenance_name)
                data_vars[provenance_name] = variable

        dims = tuple([record_dim] + trailing_dims)
        values = np.concatenate([batch.data[name] for batch in contributors])
        output_name = _unique_name(used_names, name)
        used_names.append(output_name)
        data_vars[output_name] = (dims, values, attributes)

        sparse_contributors = [batch for batch in contributors if batch.properties[name].Sparse != "No_sparse"]
        if sparse_contributors:
            physical_name = _unique_name(used_names, f"{output_name}__physical")
            used_names.append(physical_name)
            physical = np.concatenate([batch.physical[name] for batch in contributors])
            data_vars[physical_name] = (
                (record_dim,),
                physical,
                {
                    "LONG_NAME": "True where the record was physically written to the source CDF",
                    "CDF_SPARSE": [batch.properties[name].Sparse for batch in sparse_contributors],
                },
            )

        if missing_batches and axis_name is not None and axis_name in axis_batches:
            coverage_name = _unique_name(used_names, f"{output_name}__coverage")
            used_names.append(coverage_name)
            coverage_parts = []
            for batch in axis_batches[axis_name]:
                axis_count = batch.data[axis_name].shape[0]
                if name in batch.data:
                    # Records beyond the variable's Last_Rec have no value at
                    # all, so they are coverage gaps even inside a present batch.
                    coverage_parts.append(np.arange(axis_count) < batch.data[name].shape[0])
                else:
                    coverage_parts.append(np.zeros(axis_count, dtype=bool))
            coverage = np.concatenate(coverage_parts)
            data_vars[coverage_name] = (
                (axis_dim_names[axis_name],),
                coverage,
                {"LONG_NAME": "True on axis samples whose source batch provides this variable"},
            )

    # Non-record-varying variables are versioned per batch instead of broadcast.
    all_names = {name for batch in batches for name in batch.properties}
    nonrecord_names = [
        name
        for name in sorted(all_names)
        if not any(batch.properties[name].Rec_Vary for batch in batches if name in batch.properties)
    ]
    for name in nonrecord_names:
        contributors = [batch for batch in batches if name in batch.data]
        if not contributors:
            continue
        reference = contributors[0].data[name]
        disagreeing = [batch for batch in contributors[1:] if not _arrays_equal(batch.data[name], reference)]

        if not disagreeing:
            attributes = dict(contributors[0].attributes[name])
            attributes[SOURCE_BATCH_ATTR] = [batch.label for batch in contributors]
            output_name = _unique_name(used_names, name)
            used_names.append(output_name)
            dims = tuple(f"{name}__dim{index}" for index in range(reference.ndim))
            data_vars[output_name] = (dims, reference, attributes)
            continue

        message = (
            f"Non-record-varying variable {name} has different values in batches "
            f"{[batch.label for batch in disagreeing]}; emitting per-batch versions instead of reusing the first value"
        )
        if on_nonrecord_conflict == "raise":
            raise ValueError(message)
        warnings.warn(message)
        for batch in contributors:
            attributes = dict(batch.attributes[name])
            attributes[SOURCE_BATCH_LABEL_ATTR] = batch.label
            output_name = _unique_name(used_names, f"{name}__batch{batch.label}")
            used_names.append(output_name)
            dims = tuple(f"{name}__dim{index}" for index in range(batch.data[name].ndim))
            data_vars[output_name] = (dims, batch.data[name], attributes)

    coverage = [
        {
            "label": batch.label,
            "file": batch.path,
            "variables": {
                name: {
                    "records": int(batch.properties[name].Last_Rec + 1),
                    "record_varying": bool(batch.properties[name].Rec_Vary),
                    "sparse": batch.properties[name].Sparse,
                }
                for name in batch.properties
            },
        }
        for batch in batches
    ]

    dataset = xr.Dataset(data_vars=data_vars, coords=coord_vars)
    dataset.attrs["CDF_MERGE_BATCHES"] = json.dumps(coverage)
    dataset.attrs["CDF_MERGE_POLICY"] = (
        "Concatenated by declared DEPEND_0 axes without interpolation or de-duplication. "
        "Sparse previous values do not cross file boundaries. Missing variables leave coverage gaps."
    )
    return dataset
