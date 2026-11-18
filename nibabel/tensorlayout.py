# emacs: -*- mode: python-mode; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the NiBabel package for the
#   copyright and license terms.
#
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Declare and unify NIfTI tensor layouts.

A data warehouse that ingests NIfTI images needs every array in a single,
predictable layout.  Getting there safely takes more than comparing shapes:

* The **source layout** declares explicitly which axes are spatial, which (if
  any) is temporal, and which hold tensor components.  The declaration is
  validated against the NIfTI *intent* and its parameters - for example a
  ``symmetric matrix`` intent with parameter ``M`` must own exactly
  ``M * (M + 1) / 2`` components.
* The **target layout** declares how the warehouse stores vectors, general
  matrices and symmetric matrices.
* :func:`build_mapping` combines the two and gives, for *every* element of the
  target array, the source voxel and source component it comes from.

Design rules encoded here:

* Packing and unpacking symmetric matrices is its own responsibility, handled
  with triangular index arithmetic.  It is never treated as flipping or
  transposing an ordinary axis.
* A singleton component axis (length 1) stays a component axis; it does not
  silently disappear.
* An unknown intent keeps its declaration and nothing more.  The tensor kind
  is left unresolved instead of being guessed from axis lengths.
* When the target layout cannot express the source, the conflicts are listed
  (see :class:`LayoutConflict`) instead of being silently dropped.
* Reading through a chunked array proxy and reading the whole array at once
  yield the same elements and the same correspondence, and the inverse
  mapping lets every source component be checked one by one.
"""

from __future__ import annotations

import math
import typing as ty
from collections import namedtuple
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum
from itertools import product

import numpy as np

from .nifti1 import intent_codes

__all__ = [
    'AxisRole',
    'ComponentMismatch',
    'ElementLink',
    'ElementMapping',
    'LayoutConflict',
    'LayoutConflictError',
    'LayoutError',
    'SourceLayout',
    'TargetLayout',
    'TensorKind',
    'build_mapping',
    'check_against_source',
    'iter_source_components',
    'materialize_target',
]


class LayoutError(ValueError):
    """A layout declaration is invalid."""


class LayoutConflictError(LayoutError):
    """The target layout cannot express the source layout.

    Carries the full list of :class:`LayoutConflict` records in
    ``conflicts``.
    """

    def __init__(self, conflicts: ty.Sequence[LayoutConflict]):
        self.conflicts = tuple(conflicts)
        lines = '\n'.join(f'  - {c.kind}: {c.message}' for c in self.conflicts)
        super().__init__(f'Target layout cannot express source layout:\n{lines}')


@dataclass(frozen=True)
class LayoutConflict:
    """One reason a target layout cannot express a source layout."""

    kind: str
    message: str


class AxisRole(Enum):
    """Role of one array axis."""

    SPATIAL = 'spatial'
    TEMPORAL = 'temporal'
    COMPONENT = 'component'


class TensorKind(Enum):
    """Per-voxel tensor structure implied by the NIfTI intent."""

    SCALAR = 'scalar'
    VECTOR = 'vector'
    MATRIX = 'matrix'
    SYMMETRIC_MATRIX = 'symmetric_matrix'
    UNKNOWN = 'unknown'


#: Intents whose per-voxel components form a plain vector.
_VECTOR_INTENTS = frozenset(
    {'vector', 'displacement vector', 'rgb vector', 'rgba vector', 'quaternion'}
)


def _as_role(role: AxisRole | str) -> AxisRole:
    if isinstance(role, AxisRole):
        return role
    try:
        return AxisRole(str(role))
    except ValueError:
        raise LayoutError(
            f'Unknown axis role {role!r}; expected one of {[r.value for r in AxisRole]}'
        ) from None


def _as_dim(value: float, name: str) -> int:
    as_float = float(value)
    if not as_float.is_integer() or as_float < 1:
        raise LayoutError(f'{name} must be a positive integer, got {value!r}')
    return int(as_float)


@dataclass
class SourceLayout:
    """Declared layout of a source NIfTI array.

    Parameters
    ----------
    shape : sequence of int
        Shape of the source array.
    axis_roles : sequence of str or :class:`AxisRole`
        One role per axis, declaring the axis as ``'spatial'``, ``'temporal'``
        or ``'component'``.  Every axis gets exactly one role; there is at
        least one spatial axis and at most one temporal axis.
    intent : str or int
        NIfTI intent code or label (see ``nibabel.nifti1.intent_codes``).
        Unknown intents are kept as a bare declaration: the tensor kind stays
        ``TensorKind.UNKNOWN`` and no structure is inferred from axis lengths.
    intent_params : sequence of float
        Intent parameters (``intent_p1`` and friends).  ``general matrix``
        needs ``(M, N)`` and ``symmetric matrix`` needs ``(M,)``.
    symmetric_packing : {'lower', 'upper'}
        How a symmetric matrix source packs its ``M * (M + 1) / 2`` components:
        row-wise over the lower triangle (``(0,0), (1,0), (1,1), ...``) or
        row-wise over the upper triangle (``(0,0), (0,1), (0,2), (1,1) ...``).
    """

    shape: tuple[int, ...]
    axis_roles: tuple[AxisRole | str, ...]
    intent: str | int = 'none'
    intent_params: tuple[float, ...] = ()
    symmetric_packing: str = 'lower'

    def __post_init__(self):
        self.shape = tuple(int(s) for s in self.shape)
        if not self.shape or any(s < 1 for s in self.shape):
            raise LayoutError(f'Shape must be non-empty with positive axes, got {self.shape}')
        self.axis_roles = tuple(_as_role(r) for r in self.axis_roles)
        if len(self.axis_roles) != len(self.shape):
            raise LayoutError(
                f'Need one axis role per axis: {len(self.axis_roles)} roles '
                f'for {len(self.shape)} axes'
            )
        if not any(r is AxisRole.SPATIAL for r in self.axis_roles):
            raise LayoutError('Source layout needs at least one spatial axis')
        if sum(r is AxisRole.TEMPORAL for r in self.axis_roles) > 1:
            raise LayoutError('Source layout allows at most one temporal axis')
        if self.symmetric_packing not in ('lower', 'upper'):
            raise LayoutError(
                f"symmetric_packing must be 'lower' or 'upper', got {self.symmetric_packing!r}"
            )
        self.intent_params = tuple(float(p) for p in self.intent_params)
        # Keep the intent exactly as declared, known or not.
        self.declared_intent = self.intent
        self.intent_label = (
            intent_codes.label[self.intent] if self.intent in intent_codes else None
        )
        self.kind = self._resolve_kind()
        self.matrix_dims: tuple[int, int] | None = None
        self.symmetric_dim: int | None = None
        self._validate_structure()

    def _resolve_kind(self) -> TensorKind:
        label = self.intent_label
        if label is None:
            # Unknown intent: keep the declaration, never guess from axis lengths.
            return TensorKind.UNKNOWN
        if label in _VECTOR_INTENTS:
            return TensorKind.VECTOR
        if label == 'general matrix':
            return TensorKind.MATRIX
        if label == 'symmetric matrix':
            return TensorKind.SYMMETRIC_MATRIX
        return TensorKind.SCALAR

    def _validate_structure(self):
        if self.kind is TensorKind.UNKNOWN:
            # Declaration kept as-is; nothing to validate against.
            return
        n_components = self.n_components
        if self.kind is TensorKind.VECTOR and not self.component_axes:
            raise LayoutError(
                f'Intent {self.intent_label!r} declares per-voxel vectors '
                'but the layout has no component axis'
            )
        if self.kind is TensorKind.MATRIX:
            if len(self.intent_params) < 2:
                raise LayoutError(
                    "Intent 'general matrix' requires intent_params (M, N), "
                    f'got {self.intent_params}'
                )
            rows = _as_dim(self.intent_params[0], 'intent_p1 (M)')
            cols = _as_dim(self.intent_params[1], 'intent_p2 (N)')
            self.matrix_dims = (rows, cols)
            if n_components != rows * cols:
                raise LayoutError(
                    f"Intent 'general matrix' with M={rows}, N={cols} needs "
                    f'{rows * cols} components, layout has {n_components}'
                )
        if self.kind is TensorKind.SYMMETRIC_MATRIX:
            if len(self.intent_params) < 1:
                raise LayoutError(
                    "Intent 'symmetric matrix' requires intent_params (M,), "
                    f'got {self.intent_params}'
                )
            dim = _as_dim(self.intent_params[0], 'intent_p1 (M)')
            self.symmetric_dim = dim
            packed = dim * (dim + 1) // 2
            if n_components != packed:
                raise LayoutError(
                    f"Intent 'symmetric matrix' with M={dim} packs to "
                    f'{packed} components, layout has {n_components}'
                )
        if self.intent_label == 'time series' and not self.temporal_axes:
            raise LayoutError("Intent 'time series' requires a temporal axis")

    @property
    def ndim(self) -> int:
        return len(self.shape)

    @property
    def spatial_axes(self) -> tuple[int, ...]:
        return tuple(a for a, r in enumerate(self.axis_roles) if r is AxisRole.SPATIAL)

    @property
    def temporal_axes(self) -> tuple[int, ...]:
        return tuple(a for a, r in enumerate(self.axis_roles) if r is AxisRole.TEMPORAL)

    @property
    def component_axes(self) -> tuple[int, ...]:
        return tuple(a for a, r in enumerate(self.axis_roles) if r is AxisRole.COMPONENT)

    @property
    def voxel_axes(self) -> tuple[int, ...]:
        """Axes indexing a voxel: spatial axes, then the temporal axis."""
        return self.spatial_axes + self.temporal_axes

    @property
    def voxel_shape(self) -> tuple[int, ...]:
        return tuple(self.shape[a] for a in self.voxel_axes)

    @property
    def component_shape(self) -> tuple[int, ...]:
        return tuple(self.shape[a] for a in self.component_axes)

    @property
    def has_component_axis(self) -> bool:
        return bool(self.component_axes)

    @property
    def n_components(self) -> int:
        """Number of components per voxel (1 when there is no component axis)."""
        return int(np.prod(self.component_shape)) if self.component_axes else 1


@dataclass(frozen=True)
class TargetLayout:
    """Declared storage conventions of the data warehouse.

    Parameters
    ----------
    n_spatial : int
        Number of spatial axes the warehouse expects.
    has_temporal : bool
        Whether the warehouse layout carries a temporal axis.  A source
        without one gets a length-1 temporal axis; a source *with* a temporal
        axis and a target without one is a conflict.
    vector_components : {'last', 'first'}
        Where the component axis of vectors (and of scalars that carry a
        component axis) goes relative to the voxel axes.
    matrix_storage : {'row-major', 'column-major', 'axes'}
        How a general ``M x N`` matrix is stored: flattened row-major or
        column-major into one component axis, or as two axes ``(M, N)``.
    symmetric_storage : {'packed-lower', 'packed-upper', 'full'}
        How a symmetric matrix is stored: packed row-wise over the lower or
        upper triangle, or as a full ``(M, M)`` pair of axes.
    """

    n_spatial: int = 3
    has_temporal: bool = True
    vector_components: str = 'last'
    matrix_storage: str = 'row-major'
    symmetric_storage: str = 'packed-lower'

    def __post_init__(self):
        if not isinstance(self.n_spatial, int) or self.n_spatial < 1:
            raise LayoutError(f'n_spatial must be a positive integer, got {self.n_spatial!r}')
        if self.vector_components not in ('last', 'first'):
            raise LayoutError(
                f"vector_components must be 'last' or 'first', got {self.vector_components!r}"
            )
        if self.matrix_storage not in ('row-major', 'column-major', 'axes'):
            raise LayoutError(
                "matrix_storage must be 'row-major', 'column-major' or 'axes', "
                f'got {self.matrix_storage!r}'
            )
        if self.symmetric_storage not in ('packed-lower', 'packed-upper', 'full'):
            raise LayoutError(
                "symmetric_storage must be 'packed-lower', 'packed-upper' or 'full', "
                f'got {self.symmetric_storage!r}'
            )


def _pack_index(packing: str, dim: int, row: int, col: int) -> int:
    """Packed position of symmetric matrix element (`row`, `col`).

    This is the symmetric-matrix packing responsibility: triangular index
    arithmetic, not an axis flip.
    """
    if packing == 'lower':
        row, col = max(row, col), min(row, col)
        return row * (row + 1) // 2 + col
    row, col = min(row, col), max(row, col)
    return row * (2 * dim - row + 1) // 2 + (col - row)


def _unpack_index(packing: str, dim: int, k: int) -> tuple[int, int]:
    """Matrix element (`row`, `col`) stored at packed position `k`."""
    if packing == 'lower':
        row = (math.isqrt(8 * k + 1) - 1) // 2
        while (row + 1) * (row + 2) // 2 <= k:
            row += 1
        while row * (row + 1) // 2 > k:
            row -= 1
        return row, k - row * (row + 1) // 2
    # Upper triangle, row-wise: row r starts at offset r * (2*dim - r + 1) // 2.
    root = math.isqrt((2 * dim + 1) ** 2 - 8 * k)
    row = ((2 * dim + 1) - root) // 2
    while (row + 1) * (2 * dim - row) // 2 <= k:
        row += 1
    while row * (2 * dim - row + 1) // 2 > k:
        row -= 1
    return row, k - row * (2 * dim - row + 1) // 2 + row


def _symmetric_component_map(dim: int, source_packing: str, target_storage: str) -> np.ndarray:
    """Source component index for each target component of a symmetric matrix."""
    if target_storage == 'full':
        pairs = ((r, c) for r in range(dim) for c in range(dim))
    else:
        target_packing = 'lower' if target_storage == 'packed-lower' else 'upper'
        pairs = (_unpack_index(target_packing, dim, k) for k in range(dim * (dim + 1) // 2))
    return np.array([_pack_index(source_packing, dim, r, c) for r, c in pairs], dtype=np.intp)


def _matrix_component_map(
    dims: tuple[int, int], matrix_storage: str
) -> tuple[np.ndarray, tuple[int, ...]]:
    """Source component index per target component, and target component shape.

    Source components flatten row-major: component ``k`` is matrix element
    ``(k // N, k % N)``.
    """
    rows, cols = dims
    if matrix_storage == 'axes':
        return np.arange(rows * cols, dtype=np.intp), (rows, cols)
    if matrix_storage == 'row-major':
        return np.arange(rows * cols, dtype=np.intp), (rows * cols,)
    # column-major: target position t holds element (t % M, t // M)
    mapping = [(t % rows) * cols + t // rows for t in range(rows * cols)]
    return np.array(mapping, dtype=np.intp), (rows * cols,)


#: One target element and where it comes from in the source.
ElementLink = namedtuple('ElementLink', ['target_index', 'source_voxel', 'source_component'])


@dataclass
class ElementMapping:
    """Element-level correspondence between a source and a target layout.

    Attributes beyond the constructor fields are derived views.  When
    ``conflicts`` is non-empty the target cannot express the source; the
    mapping arrays are empty and materializing raises
    :class:`LayoutConflictError`.
    """

    source: SourceLayout
    target: TargetLayout
    target_shape: tuple[int, ...]
    target_voxel_shape: tuple[int, ...]
    target_component_shape: tuple[int, ...]
    component_map: np.ndarray
    components_first: bool
    temporal_padding: int = 0
    conflicts: tuple[LayoutConflict, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.conflicts

    @property
    def n_target_components(self) -> int:
        return len(self.component_map)

    @property
    def n_voxels(self) -> int:
        return int(np.prod(self.target_voxel_shape)) if self.target_voxel_shape else 0

    @property
    def voxel_stride(self) -> int:
        """Step between consecutive voxels in the flattened target array."""
        return 1 if self.components_first else self.n_target_components

    @property
    def target_offsets(self) -> np.ndarray:
        """Flat offset of each target component for the first voxel."""
        if self.components_first:
            return np.arange(self.n_target_components, dtype=np.intp) * self.n_voxels
        return np.arange(self.n_target_components, dtype=np.intp)

    def _check_ok(self):
        if self.conflicts:
            raise LayoutConflictError(self.conflicts)

    def target_index(self, voxel: tuple[int, ...], target_component: int) -> tuple[int, ...]:
        """Index into the target array for `voxel` and target component."""
        self._check_ok()
        voxel = tuple(int(v) for v in voxel) + (0,) * self.temporal_padding
        if self.target_component_shape:
            comp = tuple(
                int(i) for i in np.unravel_index(target_component, self.target_component_shape)
            )
        else:
            comp = ()
        return comp + voxel if self.components_first else voxel + comp

    def source_index(self, voxel: tuple[int, ...], source_component: int) -> tuple[int, ...]:
        """Full index into the source array for `voxel` and source component."""
        self._check_ok()
        source = self.source
        comp_coords = (
            tuple(int(i) for i in np.unravel_index(source_component, source.component_shape))
            if source.component_shape
            else ()
        )
        index = [0] * source.ndim
        for axis, coord in zip(source.voxel_axes, voxel):
            index[axis] = int(coord)
        for axis, coord in zip(source.component_axes, comp_coords):
            index[axis] = coord
        return tuple(index)

    def iter_links(self) -> Iterator[ElementLink]:
        """Yield the source voxel and component behind every target element."""
        self._check_ok()
        for voxel in np.ndindex(*self.source.voxel_shape):
            for target_component, source_component in enumerate(self.component_map):
                yield ElementLink(
                    self.target_index(voxel, target_component),
                    voxel,
                    int(source_component),
                )


def build_mapping(source: SourceLayout, target: TargetLayout) -> ElementMapping:
    """Combine a source and target layout into an element-level mapping.

    The returned :class:`ElementMapping` always lists every conflict found;
    only a conflict-free mapping can materialize or verify data.
    """
    conflicts: list[LayoutConflict] = []
    if source.kind is TensorKind.UNKNOWN:
        conflicts.append(
            LayoutConflict(
                'unknown-intent',
                f'intent {source.declared_intent!r} is not a known NIfTI intent; '
                'the declaration is kept but the tensor kind is unresolved',
            )
        )
    if len(source.spatial_axes) != target.n_spatial:
        conflicts.append(
            LayoutConflict(
                'spatial-mismatch',
                f'source has {len(source.spatial_axes)} spatial axes, '
                f'target expects {target.n_spatial}',
            )
        )
    if source.temporal_axes and not target.has_temporal:
        conflicts.append(
            LayoutConflict(
                'temporal-mismatch',
                'source has a temporal axis but the target layout has none',
            )
        )
    if conflicts:
        return ElementMapping(
            source=source,
            target=target,
            target_shape=(),
            target_voxel_shape=(),
            target_component_shape=(),
            component_map=np.array([], dtype=np.intp),
            components_first=False,
            conflicts=tuple(conflicts),
        )

    temporal_padding = 1 if (target.has_temporal and not source.temporal_axes) else 0
    target_voxel_shape = source.voxel_shape + (1,) * temporal_padding

    kind = source.kind
    if kind is TensorKind.MATRIX:
        component_map, component_shape = _matrix_component_map(
            source.matrix_dims, target.matrix_storage
        )
        components_first = False
    elif kind is TensorKind.SYMMETRIC_MATRIX:
        dim = source.symmetric_dim
        component_map = _symmetric_component_map(
            dim, source.symmetric_packing, target.symmetric_storage
        )
        component_shape = (
            (dim, dim) if target.symmetric_storage == 'full' else (dim * (dim + 1) // 2,)
        )
        components_first = False
    elif source.has_component_axis:
        # Vectors, and scalars carrying a component axis.  A singleton
        # component axis is kept: it does not silently disappear.
        component_map = np.arange(source.n_components, dtype=np.intp)
        component_shape = (source.n_components,)
        components_first = target.vector_components == 'first'
    else:
        component_map = np.zeros(1, dtype=np.intp)
        component_shape = ()
        components_first = False

    target_shape = (
        component_shape + target_voxel_shape
        if components_first
        else target_voxel_shape + component_shape
    )
    return ElementMapping(
        source=source,
        target=target,
        target_shape=target_shape,
        target_voxel_shape=target_voxel_shape,
        target_component_shape=component_shape,
        component_map=component_map,
        components_first=components_first,
        temporal_padding=temporal_padding,
        conflicts=(),
    )


def _iter_chunks(
    shape: tuple[int, ...], chunk_shape: tuple[int, ...] | None
) -> Iterator[tuple[slice, ...]]:
    if chunk_shape is None:
        yield tuple(slice(None) for _ in shape)
        return
    if len(chunk_shape) != len(shape):
        raise LayoutError(
            f'chunk_shape has {len(chunk_shape)} entries, need one per voxel axis ({len(shape)})'
        )
    if any(int(c) < 1 for c in chunk_shape):
        raise LayoutError(f'chunk_shape entries must be positive, got {chunk_shape}')
    chunk_shape = tuple(int(c) for c in chunk_shape)
    starts_per_axis = [range(0, dim, step) for dim, step in zip(shape, chunk_shape)]
    for starts in product(*starts_per_axis):
        yield tuple(
            slice(start, min(start + step, dim))
            for start, step, dim in zip(starts, chunk_shape, shape)
        )


def materialize_target(
    dataobj, mapping: ElementMapping, chunk_shape: tuple[int, ...] | None = None
) -> np.ndarray:
    """Read `dataobj` through `mapping` and return the target-layout array.

    `dataobj` is anything sliceable with an array-shaped result - an ndarray
    or a nibabel array proxy.  Voxels are read in chunks of `chunk_shape`
    (one chunk per axis lengths by default); chunked proxy reads and a single
    whole-array read produce the same elements with the same correspondence.
    """
    mapping._check_ok()
    source = mapping.source
    dtype = getattr(dataobj, 'dtype', None)
    if dtype is None:
        dtype = np.asarray(dataobj).dtype
    out_flat = np.empty(int(np.prod(mapping.target_shape)), dtype=dtype)
    n_source_components = source.n_components
    voxel_flat = np.arange(int(np.prod(source.voxel_shape)), dtype=np.intp).reshape(
        source.voxel_shape
    )
    permutation = list(source.voxel_axes) + list(source.component_axes)
    for chunk in _iter_chunks(source.voxel_shape, chunk_shape):
        slicer = [slice(None)] * source.ndim
        for axis, axis_slice in zip(source.voxel_axes, chunk):
            slicer[axis] = axis_slice
        block = np.asarray(dataobj[tuple(slicer)])
        block = np.transpose(block, permutation).reshape(-1, n_source_components)
        values = block[:, mapping.component_map]
        positions = (
            mapping.voxel_stride * voxel_flat[chunk].ravel()[:, None]
            + mapping.target_offsets[None, :]
        )
        out_flat[positions.ravel()] = values.ravel()
    return out_flat.reshape(mapping.target_shape)


def iter_source_components(
    target_array, mapping: ElementMapping
) -> Iterator[tuple[tuple[int, ...], int, ty.Any]]:
    """Yield ``(voxel, source_component, value)`` recovered from a target array.

    This is the inverse direction of the mapping: every source component is
    yielded once per voxel, so the original components can be checked one by
    one.
    """
    mapping._check_ok()
    target_array = np.asarray(target_array)
    if target_array.shape != mapping.target_shape:
        raise LayoutError(
            f'Target array has shape {target_array.shape}, expected {mapping.target_shape}'
        )
    seen: set[tuple[tuple[int, ...], int]] = set()
    for link in mapping.iter_links():
        key = (link.source_voxel, link.source_component)
        if key in seen:
            continue
        seen.add(key)
        yield link.source_voxel, link.source_component, target_array[link.target_index]


@dataclass(frozen=True)
class ComponentMismatch:
    """One source component whose target value disagrees with the source."""

    voxel: tuple[int, ...]
    component: int
    source_value: ty.Any
    target_value: ty.Any


def check_against_source(
    target_array, dataobj, mapping: ElementMapping
) -> list[ComponentMismatch]:
    """Check a target-layout array back against the source, component by component.

    Returns a list of :class:`ComponentMismatch`; an empty list means every
    target element agrees with the source voxel and component it maps from.
    """
    mapping._check_ok()
    target_array = np.asarray(target_array)
    if target_array.shape != mapping.target_shape:
        raise LayoutError(
            f'Target array has shape {target_array.shape}, expected {mapping.target_shape}'
        )
    source_data = np.asarray(dataobj)
    mismatches = []
    for link in mapping.iter_links():
        source_value = source_data[mapping.source_index(link.source_voxel, link.source_component)]
        target_value = target_array[link.target_index]
        if source_value != target_value:
            mismatches.append(
                ComponentMismatch(
                    link.source_voxel, link.source_component, source_value, target_value
                )
            )
    return mismatches
