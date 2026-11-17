"""
Logical/physical index mapping for CDF variables.

A CDF variable is described by its record variance (``Rec_Vary``), the per-dimension
variance flags (``Dim_Vary``), the declared dimension sizes (``Dim_Sizes``) and the
file majority (row or column).  Non-varying axes collapse in the physical storage but
remain part of the declared logical tensor, and column-major files number the physical
elements differently even though :func:`cdflib.cdfread.CDF.varget` presents the data in
logical axis order.

This module keeps a single, traceable view over that storage:

* the logical tensor always has the declared shape, including non-varying axes;
* shared (non-varying) values are broadcast for display without being copied;
* :class:`Observation` objects map every logical position back to the source record and
  to the flat physical offset inside that record;
* slicing and axis transposition preserve that backtrace through the derived views;
* data that contradicts the declared variance flags raises
  :class:`StructureConflictError`;
* empty variables keep their declared logical shape instead of reverse-guessing the
  dimensions from an empty array.
"""

from dataclasses import dataclass
from typing import Any, Optional, Sequence, Tuple, Union

import numpy as np
import numpy.typing as npt

ROW_MAJOR = "row_major"
COLUMN_MAJOR = "column_major"

BasicIndexer = Union[int, slice, Tuple[Union[int, slice, type(Ellipsis)], ...]]

__all__ = [
    "ROW_MAJOR",
    "COLUMN_MAJOR",
    "Observation",
    "StructureConflictError",
    "VariableView",
    "logical_shape",
    "physical_shape",
    "varying_sizes",
    "count_measurements",
    "encode_physical_offset",
    "decode_physical_offset",
]


class StructureConflictError(ValueError):
    """Raised when submitted data contradicts the ``Rec_Vary``/``Dim_Vary`` declarations."""


@dataclass(frozen=True)
class Observation:
    """
    A single logical position resolved back to physical storage.

    Attributes
    ----------
    variable : str
        Name of the source variable.
    logical_index : tuple of int
        Position in the declared logical tensor.
    record : int
        Physical CDF record number.  Always ``0`` for record-non-varying variables.
    record_index : tuple of int
        Position inside the record tensor, covering the varying dimensions only.
    physical_index : int
        Flat offset of the value inside the record, numbered according to the file
        majority.  ``0`` when the record has no varying dimensions.
    broadcast : bool
        True when this logical position shares a stored value with other positions.
    """

    variable: str
    logical_index: Tuple[int, ...]
    record: int
    record_index: Tuple[int, ...]
    physical_index: int
    broadcast: bool


def _normalize_majority(majority: Union[str, int]) -> str:
    if isinstance(majority, int):
        if majority == 1:
            return ROW_MAJOR
        if majority == 2:
            return COLUMN_MAJOR
        raise ValueError(f"Invalid majority value: {majority}")
    token = majority.lower().replace("-", "_")
    if token in ("row_major", "row"):
        return ROW_MAJOR
    if token in ("column_major", "column", "col_major"):
        return COLUMN_MAJOR
    raise ValueError(f"Invalid majority: {majority}")


def _normalize_dim_vary(dim_vary: Sequence[bool], num_dims: int) -> Tuple[bool, ...]:
    flags = tuple(bool(flag) for flag in dim_vary)
    if len(flags) != num_dims:
        raise ValueError(f"Dim_Vary has {len(flags)} entries but the variable declares {num_dims} dimensions")
    return flags


def varying_sizes(dim_sizes: Sequence[int], dim_vary: Sequence[bool]) -> Tuple[int, ...]:
    """Sizes of the varying dimensions, in logical axis order."""
    return tuple(size for size, vary in zip(dim_sizes, dim_vary) if vary)


def logical_shape(rec_vary: bool, dim_sizes: Sequence[int], num_records: int) -> Tuple[int, ...]:
    """
    Declared logical tensor shape.

    The record axis is present only for record-varying variables.  The declared
    dimensions are always included, even for an empty variable, so the shape is never
    inferred from an empty data array.
    """
    shape = tuple(int(size) for size in dim_sizes)
    if rec_vary:
        return (max(int(num_records), 0),) + shape
    return shape


def physical_shape(
    rec_vary: bool, dim_sizes: Sequence[int], dim_vary: Sequence[bool], num_records: int
) -> Tuple[int, ...]:
    """Shape of the physical storage produced by ``varget``: records plus varying axes."""
    record_axis = (max(int(num_records), 0),) if rec_vary else ()
    return record_axis + varying_sizes(dim_sizes, dim_vary)


def count_measurements(
    rec_vary: bool, dim_sizes: Sequence[int], dim_vary: Sequence[bool], num_records: int
) -> int:
    """
    Number of values actually stored for the variable.

    Broadcast (non-varying) positions count once, and a variable without any written
    record counts as zero measurements regardless of its declared logical shape.
    """
    num_records = max(int(num_records), 0)
    if num_records == 0:
        return 0
    values_per_record = int(np.prod(varying_sizes(dim_sizes, dim_vary), dtype=np.int64))
    if not rec_vary:
        return values_per_record
    return num_records * values_per_record


def decode_physical_offset(
    physical_index: int, varying_dim_sizes: Sequence[int], majority: Union[str, int]
) -> Tuple[int, ...]:
    """Decode a flat in-record offset into the varying-dimension logical index."""
    majority = _normalize_majority(majority)
    sizes = tuple(int(size) for size in varying_dim_sizes)
    if len(sizes) == 0:
        if physical_index != 0:
            raise ValueError(f"Physical offset {physical_index} is out of range for a scalar record")
        return ()
    total = int(np.prod(sizes, dtype=np.int64))
    if physical_index < 0 or physical_index >= total:
        raise ValueError(f"Physical offset {physical_index} is out of range for record size {total}")
    index = []
    remainder = physical_index
    if majority == ROW_MAJOR:
        for size in sizes:
            total //= size
            index.append(remainder // total)
            remainder %= total
    else:
        for size in sizes:
            index.append(remainder % size)
            remainder //= size
    return tuple(index)


def encode_physical_offset(
    record_index: Sequence[int], varying_dim_sizes: Sequence[int], majority: Union[str, int]
) -> int:
    """Encode the varying-dimension logical index as a flat in-record physical offset."""
    sizes = tuple(int(size) for size in varying_dim_sizes)
    index = tuple(int(value) for value in record_index)
    if len(index) != len(sizes):
        raise ValueError(f"Record index {index} does not match varying dimensions {sizes}")
    majority = _normalize_majority(majority)
    offset = 0
    if majority == ROW_MAJOR:
        for value, size in zip(index, sizes):
            if not 0 <= value < size:
                raise ValueError(f"Record index {index} is out of range for {sizes}")
            offset = offset * size + value
    else:
        stride = 1
        for value, size in zip(index, sizes):
            if not 0 <= value < size:
                raise ValueError(f"Record index {index} is out of range for {sizes}")
            offset += value * stride
            stride *= size
    return offset


def _build_offset_map(
    dim_sizes: Sequence[int], dim_vary: Sequence[bool], majority: str
) -> npt.NDArray[np.int64]:
    """Physical in-record offsets for every declared dimension position."""
    sizes = varying_sizes(dim_sizes, dim_vary)
    element_count = int(np.prod(sizes, dtype=np.int64)) if sizes else 1
    order = "C" if majority == ROW_MAJOR else "F"
    offsets = np.arange(element_count, dtype=np.int64).reshape(sizes, order=order) if sizes else np.array(0, dtype=np.int64)
    for axis, (size, vary) in enumerate(zip(dim_sizes, dim_vary)):
        if not vary:
            offsets = np.expand_dims(offsets, axis=axis)
            offsets = np.broadcast_to(offsets, tuple(dim_sizes)).copy()
    return offsets


def _build_broadcast_map(dim_sizes: Sequence[int], dim_vary: Sequence[bool]) -> npt.NDArray[np.bool_]:
    """True where the logical position reuses a stored value via a non-varying axis."""
    broadcast = np.zeros(varying_sizes(dim_sizes, dim_vary), dtype=bool)
    for axis, (size, vary) in enumerate(zip(dim_sizes, dim_vary)):
        if not vary:
            broadcast = np.expand_dims(broadcast, axis=axis)
            broadcast = np.broadcast_to(broadcast, tuple(dim_sizes)).copy()
            broadcast = broadcast | True
    return broadcast


class VariableView:
    """
    A traceable tensor view over one CDF variable's physical storage.

    Instances are normally created through :meth:`from_storage` (for ``varget`` style
    data with non-varying axes already removed) or :meth:`from_full_tensor` (for a fully
    shaped tensor that is validated against the variance flags).  Derived views obtained
    through slicing or transposition keep working backtraces to the original records.
    """

    def __init__(
        self,
        name: str,
        store: npt.NDArray[Any],
        rec_vary: bool,
        dim_vary: Sequence[bool],
        dim_sizes: Sequence[int],
        num_records: int,
        majority: Union[str, int] = ROW_MAJOR,
        dim_names: Optional[Sequence[str]] = None,
    ) -> None:
        self.name = str(name)
        self.rec_vary = bool(rec_vary)
        self.dim_sizes = tuple(int(size) for size in dim_sizes)
        self.dim_vary = _normalize_dim_vary(dim_vary, len(self.dim_sizes))
        self.majority = _normalize_majority(majority)
        self.num_records = max(int(num_records), 0)
        self.dim_names = self._normalize_dim_names(dim_names)

        expected_store_shape = physical_shape(self.rec_vary, self.dim_sizes, self.dim_vary, self.num_records)
        store = np.asarray(store)
        if store.shape != expected_store_shape:
            raise StructureConflictError(
                f"Variable {self.name!r}: physical data shape {store.shape} does not match the storage shape "
                f"{expected_store_shape} implied by Rec_Vary={self.rec_vary}, Dim_Vary={self.dim_vary} and "
                f"Dim_Sizes={self.dim_sizes}"
            )
        if not self.rec_vary and self.num_records == 0:
            raise StructureConflictError(
                f"Variable {self.name!r} is declared Rec_Vary=False but has no written record, so its declared "
                "dimensions have no value to broadcast"
            )
        self._store = store

        self.logical_shape = logical_shape(self.rec_vary, self.dim_sizes, self.num_records)
        self.physical_shape = expected_store_shape
        self.record_axis = 0 if self.rec_vary else -1
        self._origin_rec_vary = self.rec_vary
        self._origin_dim_vary = self.dim_vary
        self._origin_varying_sizes = self.varying_dim_sizes
        self._build_maps()
        self._data = self._build_data()
        self._measurements = count_measurements(self.rec_vary, self.dim_sizes, self.dim_vary, self.num_records)

    @classmethod
    def from_storage(
        cls,
        name: str,
        data: npt.NDArray[Any],
        rec_vary: bool,
        dim_vary: Sequence[bool],
        dim_sizes: Sequence[int],
        num_records: Optional[int] = None,
        majority: Union[str, int] = ROW_MAJOR,
        dim_names: Optional[Sequence[str]] = None,
    ) -> "VariableView":
        """Build a view from ``varget`` output (non-varying axes removed, logical axis order)."""
        dim_sizes = tuple(int(size) for size in dim_sizes)
        dim_vary_flags = _normalize_dim_vary(dim_vary, len(dim_sizes))
        if num_records is None:
            num_records = data.shape[0] if rec_vary else 1
        data = np.asarray(data)
        expected_store_shape = physical_shape(rec_vary, dim_sizes, dim_vary_flags, num_records)
        if num_records == 0 and data.size == 0 and data.shape != expected_store_shape:
            # varget returns a 1-D empty array; rebuild the declared storage shape instead
            # of reverse-guessing the dimensions from the empty array.
            data = np.empty(expected_store_shape, dtype=data.dtype)
        return cls(name, data, rec_vary, dim_vary_flags, dim_sizes, num_records, majority, dim_names)

    @classmethod
    def from_variable(
        cls,
        properties: Any,
        data: npt.NDArray[Any],
        majority: Union[str, int] = ROW_MAJOR,
        name: Optional[str] = None,
        dim_names: Optional[Sequence[str]] = None,
        dim_sizes: Optional[Sequence[int]] = None,
    ) -> "VariableView":
        """
        Build a view from a ``varinq`` result (anything exposing ``Rec_Vary``,
        ``Dim_Vary``, ``Dim_Sizes`` and ``Last_Rec``) and the corresponding ``varget`` data.

        rVariables drop non-varying axes from their VDR ``Dim_Sizes``; pass the full
        declared sizes from ``cdf_info().rDim_sizes`` through ``dim_sizes`` in that case,
        otherwise the non-varying dimensions cannot be restored in the logical view.
        """
        var_name = name if name is not None else properties.Variable
        rec_vary = bool(properties.Rec_Vary)
        declared_sizes = tuple(int(size) for size in (dim_sizes if dim_sizes is not None else properties.Dim_Sizes))
        dim_vary = tuple(bool(flag) for flag in properties.Dim_Vary)
        num_records = max(int(properties.Last_Rec) + 1, 0)
        return cls.from_storage(
            var_name,
            np.asarray(data),
            rec_vary,
            dim_vary,
            declared_sizes,
            num_records=num_records,
            majority=majority,
            dim_names=dim_names,
        )

    @classmethod
    def from_full_tensor(
        cls,
        name: str,
        data: npt.ArrayLike,
        rec_vary: bool,
        dim_vary: Sequence[bool],
        dim_sizes: Sequence[int],
        majority: Union[str, int] = ROW_MAJOR,
        dim_names: Optional[Sequence[str]] = None,
    ) -> "VariableView":
        """
        Build a view from a fully shaped tensor.

        A non-varying axis must be supplied as a size-one axis, and a record-non-varying
        variable may carry at most one leading record.  Any contradiction with the
        declared structure raises :class:`StructureConflictError`.
        """
        tensor = np.asarray(data)
        dim_sizes = tuple(int(size) for size in dim_sizes)
        dim_vary_flags = _normalize_dim_vary(dim_vary, len(dim_sizes))
        expected_rank = len(dim_sizes) + (1 if rec_vary else 0)

        if not rec_vary and tensor.ndim == len(dim_sizes) + 1:
            if tensor.shape[0] != 1:
                raise StructureConflictError(
                    f"Variable {name!r} is declared Rec_Vary=False but {tensor.shape[0]} records were supplied"
                )
            tensor = tensor[0]
        if tensor.ndim != expected_rank:
            raise StructureConflictError(
                f"Variable {name!r}: data rank {tensor.ndim} does not match the declared rank {expected_rank}"
            )

        first_dim_axis = 1 if rec_vary else 0
        for axis, (size, vary) in enumerate(zip(dim_sizes, dim_vary_flags)):
            actual = tensor.shape[first_dim_axis + axis]
            if not vary and actual != 1:
                raise StructureConflictError(
                    f"Variable {name!r}: dimension {axis} is declared non-varying but data has {actual} "
                    f"values along that axis"
                )
            if vary and actual != size:
                raise StructureConflictError(
                    f"Variable {name!r}: dimension {axis} declares size {size} but data has size {actual}"
                )

        if rec_vary:
            num_records = tensor.shape[0]
            novary_axes = tuple(first_dim_axis + axis for axis, vary in enumerate(dim_vary_flags) if not vary)
        else:
            num_records = 1 if tensor.size else 0
            novary_axes = tuple(axis for axis, vary in enumerate(dim_vary_flags) if not vary)
        store = np.squeeze(tensor, axis=novary_axes) if novary_axes else tensor
        return cls(name, store, rec_vary, dim_vary_flags, dim_sizes, num_records, majority, dim_names)

    def _normalize_dim_names(self, dim_names: Optional[Sequence[str]]) -> Tuple[str, ...]:
        num_dims = len(self.dim_sizes)
        if dim_names is None:
            names = tuple(f"dim{axis}" for axis in range(num_dims))
        else:
            names = tuple(str(dim_name) for dim_name in dim_names)
            if len(names) != num_dims:
                raise ValueError(f"dim_names has {len(names)} entries but the variable declares {num_dims} dimensions")
        if self.rec_vary:
            return ("record",) + names
        return names

    def _build_maps(self) -> None:
        offset_map = _build_offset_map(self.dim_sizes, self.dim_vary, self.majority)
        broadcast_map = _build_broadcast_map(self.dim_sizes, self.dim_vary)

        if self.rec_vary:
            record_axis = np.arange(self.num_records, dtype=np.int64).reshape((self.num_records,) + (1,) * len(self.dim_sizes))
            self._record_map = np.broadcast_to(record_axis, self.logical_shape).copy()
            self._physical_map = np.broadcast_to(
                offset_map, (self.num_records,) + tuple(self.dim_sizes) if self.dim_sizes else (self.num_records,)
            ).copy()
            self._broadcast_map = np.broadcast_to(broadcast_map, self.logical_shape).copy()
        else:
            self._record_map = np.zeros(self.dim_sizes, dtype=np.int64)
            self._physical_map = np.broadcast_to(offset_map, self.dim_sizes).copy()
            self._broadcast_map = broadcast_map

    def _build_data(self) -> npt.NDArray[Any]:
        tensor = self._store
        for axis, vary in enumerate(self.dim_vary):
            store_axis = axis + (1 if self.rec_vary else 0)
            if not vary:
                tensor = np.expand_dims(tensor, axis=store_axis)
        return np.broadcast_to(tensor, self.logical_shape)

    @property
    def data(self) -> npt.NDArray[Any]:
        """The logical tensor.  Non-varying axes are broadcast, not duplicated."""
        return self._data

    @property
    def num_measurements(self) -> int:
        """Number of values physically present in the CDF storage."""
        return self._measurements

    @property
    def num_logical_positions(self) -> int:
        """Number of positions in the declared logical tensor (broadcast slots included)."""
        return int(np.prod(self.logical_shape, dtype=np.int64)) if self.logical_shape else 1

    @property
    def varying_dim_sizes(self) -> Tuple[int, ...]:
        return varying_sizes(self.dim_sizes, self.dim_vary)

    def _normalize_index(self, index: BasicIndexer) -> Tuple[Any, ...]:
        if isinstance(index, tuple):
            normalized = list(index)
        else:
            normalized = [index]
        if Ellipsis in normalized:
            position = normalized.index(Ellipsis)
            missing = len(self.logical_shape) - (len(normalized) - 1)
            normalized = normalized[:position] + [slice(None)] * missing + normalized[position + 1 :]
        while len(normalized) < len(self.logical_shape):
            normalized.append(slice(None))
        if len(normalized) > len(self.logical_shape):
            raise IndexError(f"Too many indices for variable {self.name!r} with shape {self.logical_shape}")
        return tuple(normalized)

    def logical_to_physical(self, index: BasicIndexer) -> Observation:
        """Resolve a single logical position to its source record and physical offset."""
        normalized = self._normalize_index(index)
        if any(not isinstance(part, int) for part in normalized):
            raise TypeError("logical_to_physical requires a single integer position, use index_grid() for slices")
        position = tuple(int(part) for part in normalized)
        for value, size in zip(position, self.logical_shape):
            if not -size <= value < size:
                raise IndexError(f"Index {position} is out of range for shape {self.logical_shape}")
        position = tuple(value % size for value, size in zip(position, self.logical_shape))
        record = int(self._record_map[position])
        physical_index = int(self._physical_map[position])
        # Always decode against the original record tensor, even for sliced views whose
        # varying axes have been dropped or reordered.
        record_index = decode_physical_offset(physical_index, self._origin_varying_sizes, self.majority)
        return Observation(
            variable=self.name,
            logical_index=position,
            record=record,
            record_index=record_index,
            physical_index=physical_index,
            broadcast=bool(self._broadcast_map[position]),
        )

    def physical_to_logical(self, record: int, physical_index: int) -> Tuple[int, ...]:
        """
        Inverse of :meth:`logical_to_physical` for the untransformed view.

        Returns the logical index in the original variable; derived (sliced or
        transposed) views do not support a unique inverse and raise ``ValueError``.
        """
        if self.record_axis < 0 or self.dim_vary != self._origin_dim_vary or self.rec_vary != self._origin_rec_vary:
            raise ValueError("physical_to_logical is only defined for the untransformed base view")
        if self.num_records == 0:
            raise IndexError(f"Variable {self.name!r} has no written records")
        if self.rec_vary and not 0 <= record < self.num_records:
            raise IndexError(f"Record {record} is out of range for {self.num_records} records")
        record_index = decode_physical_offset(physical_index, self._origin_varying_sizes, self.majority)
        dim_index = []
        varying_cursor = 0
        for vary in self._origin_dim_vary:
            if vary:
                dim_index.append(record_index[varying_cursor])
                varying_cursor += 1
            else:
                dim_index.append(0)
        if self.rec_vary:
            return (record,) + tuple(dim_index)
        return tuple(dim_index)

    def index_grid(self) -> Tuple[npt.NDArray[np.int64], npt.NDArray[np.int64], npt.NDArray[np.bool_]]:
        """Record numbers, physical offsets and broadcast flags for every logical position."""
        return self._record_map.copy(), self._physical_map.copy(), self._broadcast_map.copy()

    def value_at(self, index: BasicIndexer) -> Any:
        """The tensor value at a single logical position."""
        normalized = self._normalize_index(index)
        if any(not isinstance(part, int) for part in normalized):
            raise TypeError("value_at requires a single integer position")
        return self._data[tuple(int(part) for part in normalized)]

    def trace(self, index: BasicIndexer) -> Tuple[Any, Observation]:
        """Return both the value and the backtrace information for one position."""
        observation = self.logical_to_physical(index)
        return self.value_at(index), observation

    def observations(self):
        """Yield one :class:`Observation` per position of the logical tensor."""
        for position in np.ndindex(self.logical_shape):
            yield self.logical_to_physical(position)

    def _derived_view(
        self,
        record_map: npt.NDArray[np.int64],
        physical_map: npt.NDArray[np.int64],
        broadcast_map: npt.NDArray[np.bool_],
        data: npt.NDArray[Any],
        logical_shape: Tuple[int, ...],
        dim_names: Tuple[str, ...],
        rec_vary: bool,
        dim_sizes: Tuple[int, ...],
        dim_vary: Tuple[bool, ...],
        record_axis: int,
    ) -> "VariableView":
        view = object.__new__(VariableView)
        view.name = self.name
        view._store = self._store
        view.rec_vary = rec_vary
        view.dim_sizes = dim_sizes
        view.dim_vary = dim_vary
        view.majority = self.majority
        view.num_records = self.num_records
        view.dim_names = dim_names
        view.physical_shape = self.physical_shape
        view.logical_shape = logical_shape
        view._origin_rec_vary = self._origin_rec_vary
        view._origin_dim_vary = self._origin_dim_vary
        view._origin_varying_sizes = self._origin_varying_sizes
        view.record_axis = record_axis
        view._record_map = record_map
        view._physical_map = physical_map
        view._broadcast_map = broadcast_map
        view._data = data
        if logical_shape:
            pairs = np.stack([record_map.ravel(), physical_map.ravel()], axis=1)
            view._measurements = int(np.unique(pairs, axis=0).shape[0])
        else:
            view._measurements = 1
        return view

    def isel(self, indexers: BasicIndexer) -> "VariableView":
        """
        Slice the logical tensor using integer/slice indexers per axis.

        The returned view keeps mapping back to the original CDF records and offsets.
        """
        normalized = self._normalize_index(indexers)
        indexed_data = self._data[normalized]
        record_map = self._record_map[normalized]
        physical_map = self._physical_map[normalized]
        broadcast_map = self._broadcast_map[normalized]

        dim_sizes = list(self.dim_sizes)
        dim_vary = list(self.dim_vary)
        dim_names = list(self.dim_names)
        rec_vary = self.rec_vary
        new_record_axis = self.record_axis
        dim_axis_lookup = {
            axis: dim_axis
            for dim_axis, axis in enumerate(
                axis for axis in range(len(self.logical_shape)) if not (rec_vary and axis == self.record_axis)
            )
        }

        for axis in range(len(normalized) - 1, -1, -1):
            part = normalized[axis]
            if isinstance(part, int):
                if rec_vary and axis == self.record_axis:
                    rec_vary = False
                    new_record_axis = -1
                else:
                    dim_axis = dim_axis_lookup[axis]
                    del dim_sizes[dim_axis]
                    del dim_vary[dim_axis]
                del dim_names[axis]
                if new_record_axis > axis:
                    new_record_axis -= 1
            elif isinstance(part, slice):
                if not (rec_vary and axis == self.record_axis):
                    dim_axis = dim_axis_lookup[axis]
                    dim_sizes[dim_axis] = len(range(*part.indices(self.logical_shape[axis])))
            else:
                raise TypeError("Only integer and slice indexers are supported")

        logical_shape = tuple(indexed_data.shape)
        return self._derived_view(
            record_map,
            physical_map,
            broadcast_map,
            indexed_data,
            logical_shape,
            tuple(dim_names),
            rec_vary,
            tuple(dim_sizes),
            tuple(dim_vary),
            new_record_axis,
        )

    def transpose(self, axes: Optional[Sequence[int]] = None) -> "VariableView":
        """Return a view with the logical axes transposed; backtraces remain valid."""
        if axes is None:
            order = tuple(range(len(self.logical_shape)))[::-1]
        else:
            order = tuple(int(axis) for axis in axes)
        if sorted(order) != list(range(len(self.logical_shape))):
            raise ValueError(f"Invalid transpose axes {axes} for shape {self.logical_shape}")
        new_record_axis = order.index(self.record_axis) if self.record_axis >= 0 else -1
        return self._derived_view(
            np.transpose(self._record_map, order),
            np.transpose(self._physical_map, order),
            np.transpose(self._broadcast_map, order),
            np.transpose(self._data, order),
            tuple(self.logical_shape[axis] for axis in order),
            tuple(self.dim_names[axis] for axis in order),
            self.rec_vary,
            self._transpose_dims(order),
            self._transpose_vary(order),
            new_record_axis,
        )

    def _transpose_dims(self, order: Tuple[int, ...]) -> Tuple[int, ...]:
        dim_axes = [axis for axis in order if not (self.rec_vary and axis == 0)]
        return tuple(self.dim_sizes[axis - 1] if self.rec_vary else self.dim_sizes[axis] for axis in dim_axes)

    def _transpose_vary(self, order: Tuple[int, ...]) -> Tuple[bool, ...]:
        dim_axes = [axis for axis in order if not (self.rec_vary and axis == 0)]
        return tuple(self.dim_vary[axis - 1] if self.rec_vary else self.dim_vary[axis] for axis in dim_axes)

    def swapaxes(self, axis1: int, axis2: int) -> "VariableView":
        """Exchange two logical axes."""
        order = list(range(len(self.logical_shape)))
        order[axis1], order[axis2] = order[axis2], order[axis1]
        return self.transpose(order)

    def backtrace(self, index: BasicIndexer) -> Observation:
        """Backtrace a position in a sliced/transposed view to the original observation."""
        return self.logical_to_physical(index)

    def __getitem__(self, index: BasicIndexer) -> "VariableView":
        return self.isel(index)

    def __repr__(self) -> str:
        return (
            f"VariableView(name={self.name!r}, logical_shape={self.logical_shape}, "
            f"physical_shape={self.physical_shape}, rec_vary={self.rec_vary}, "
            f"dim_vary={self.dim_vary}, majority={self.majority!r}, "
            f"measurements={self.num_measurements})"
        )
