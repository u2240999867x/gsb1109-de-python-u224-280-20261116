# emacs: -*- mode: python-mode; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the NiBabel package for the
#   copyright and license terms.
#
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Complex image to magnitude / continuous phase conversion.

Imaging data warehouses typically accept real-valued arrays only.  A complex
image ``z`` is therefore stored as two real arrays::

    z = magnitude * exp(1j * phase)

The measured (wrapped) phase of a voxel is only defined modulo the period
(``2 * pi``).  Recovering a continuous phase field is a graph problem:

* voxels with zero magnitude carry no measured phase and cannot be used to
  connect two regions, so the valid-voxel mask defines the graph nodes;
* each adjacency edge constrains the allowed phase difference between its
  endpoints;
* a small number of phase anchors may pin the absolute phase of components.

The solver here works on integer-cycle differences, so it never commits to a
single scan path: every edge is checked against the potentials built from the
spanning forest, and contradictory cycles are reported together with the
original edges forming the cycle.  Components without an anchor keep a free
integer-cycle offset, which is reported instead of being silently fixed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .nifti1 import Nifti1Image

TWO_PI = 2.0 * np.pi

__all__ = [
    'PhaseUnwrapError',
    'UnwrapResult',
    'build_voxel_adjacency',
    'complex_to_magnitude_phase',
    'compose_phase',
    'magnitude_phase_to_complex',
    'recombine_complex_image',
    'split_complex_image',
    'unwrap_phase',
    'verify_unwrapping',
]


class PhaseUnwrapError(ValueError):
    """Raised when the edge or anchor constraints cannot hold together.

    The ``conflicts`` attribute holds one dictionary per contradiction.  A
    cycle conflict records the offending edge and the spanning-tree path that
    closes it, so the report does not depend on any particular scan order.
    """

    def __init__(self, message, conflicts=None):
        super().__init__(message)
        self.conflicts = list(conflicts) if conflicts is not None else []


@dataclass
class UnwrapResult:
    """Outcome of :func:`unwrap_phase`.

    Attributes
    ----------
    feasible : bool
        True when every edge and anchor constraint can hold jointly.
    phase : ndarray or None
        Continuous phase on the full voxel grid (NaN outside the valid mask).
        Canonical offset zero is used for unanchored components.  ``None`` when
        the constraints are infeasible.
    component : ndarray of int
        Connected-component label for every grid voxel; ``-1`` outside the
        mask.
    is_determined : ndarray of bool
        True for voxels whose component contains a phase anchor.
    measured_mask : ndarray of bool
        The valid-voxel mask actually used.
    offsets : list of dict
        One entry per connected component.  Determined (anchored) components
        carry ``determined=True`` with the base phase implied by their anchor;
        undetermined components carry ``determined=False`` and a
        ``free_cycles`` description of their unrestricted integer-cycle
        variable.
    conflicts : list of dict
        Contradictions found; empty when ``feasible`` is True.
    """

    feasible: bool
    phase: np.ndarray | None
    component: np.ndarray
    is_determined: np.ndarray
    measured_mask: np.ndarray
    offsets: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)


def complex_to_magnitude_phase(data, mask=None, zero_tol=0.0):
    """Split a complex array into magnitude and wrapped (measured) phase.

    Voxels with magnitude at or below ``zero_tol`` carry no measured phase:
    their phase is NaN and they are excluded from the returned mask, so they
    cannot connect neighbouring regions.

    Parameters
    ----------
    data : array-like
        Complex-valued array.  Ordinary two-component real arrays are rejected
        rather than interpreted as real and imaginary parts.
    mask : array-like of bool, optional
        Additional valid-voxel mask.  Voxels excluded here are also reported
        with NaN phase even when their magnitude is non-zero.
    zero_tol : float, optional
        Magnitudes at or below this value are treated as zero.

    Returns
    -------
    magnitude : ndarray
        ``abs(data)`` with the same shape and voxel correspondence.
    wrapped_phase : ndarray
        ``angle(data)`` where the phase is measured, NaN elsewhere.
    measured_mask : ndarray of bool
        Voxels that provide a measured phase.
    """
    data = np.asarray(data)
    if not np.iscomplexobj(data):
        raise TypeError(
            'only complex images can be split into magnitude and phase; '
            'a real two-component array is not a complex image'
        )
    magnitude = np.abs(data)
    measured_mask = magnitude > zero_tol
    if mask is not None:
        measured_mask &= np.asarray(mask, dtype=bool)
    wrapped_phase = np.where(measured_mask, np.angle(data), np.nan)
    return magnitude, wrapped_phase, measured_mask


def magnitude_phase_to_complex(magnitude, phase):
    """Rebuild the complex array from magnitude and (unwrapped) phase.

    The voxel correspondence of the inputs is preserved.  Entries without a
    measured phase (NaN) are returned as exact zero.
    """
    magnitude = np.asarray(magnitude, dtype=np.float64)
    phase = np.asarray(phase, dtype=np.float64)
    valid = np.isfinite(phase) & (magnitude > 0.0)
    rebuilt = np.zeros(np.broadcast_shapes(magnitude.shape, phase.shape), dtype=np.complex128)
    np.multiply(magnitude, np.exp(1j * phase), out=rebuilt, where=valid)
    return rebuilt


def build_voxel_adjacency(mask, connectivity=6):
    """Build adjacency edges between neighbouring valid voxels.

    Parameters
    ----------
    mask : array-like of bool
        Valid-voxel mask.  Edges never cross excluded or zero-magnitude
        voxels.
    connectivity : int, optional
        Maximum Manhattan (L1) distance between neighbours.  The defaults for
        3D grids are 6 (face), 18 (face plus edge) or 26 (face, edge and
        corner).  Values above ``mask.ndim`` are clamped to it; values below
        one raise.

    Returns
    -------
    edges : ndarray of int, shape ``(E, 2)``
        Pairs of flat indices into ``mask``.  Each unordered pair appears once,
        oriented from the lower to the higher flat index.
    """
    mask = np.asarray(mask, dtype=bool)
    ndim = mask.ndim
    connectivity = min(connectivity, ndim)
    if connectivity < 1:
        raise ValueError(f'connectivity must be in 1..{ndim}, got {connectivity}')
    offsets = []
    for raw in np.ndindex(*((3,) * ndim)):
        offset = tuple(component - 1 for component in raw)
        l1_distance = sum(abs(component) for component in offset)
        if l1_distance == 0 or l1_distance > connectivity:
            continue
        # Keep one orientation per unordered pair.
        first_nonzero = next(component for component in offset if component)
        if first_nonzero > 0:
            offsets.append(offset)
    offsets = np.array(offsets, dtype=int)
    flat_ids = np.arange(mask.size).reshape(mask.shape)
    edges = []
    for offset in offsets:
        src_slices = tuple(
            slice(1 if o > 0 else 0, mask.shape[axis] if o >= 0 else mask.shape[axis] - 1)
            for axis, o in enumerate(offset)
        )
        dst_slices = tuple(
            slice(s.start - o, s.stop - o) for s, o in zip(src_slices, offset, strict=True)
        )
        pair = mask[src_slices] & mask[dst_slices]
        if not pair.any():
            continue
        src_ids = flat_ids[src_slices][pair]
        dst_ids = flat_ids[dst_slices][pair]
        edges.append(np.column_stack((src_ids, dst_ids)))
    if not edges:
        return np.empty((0, 2), dtype=int)
    edges = np.vstack(edges)
    edges.sort(axis=1)
    return edges


def _validate_inputs(wrapped, mask, edges, max_deltas, anchors, period, tol):
    wrapped = np.asarray(wrapped, dtype=np.float64)
    mask = np.asarray(mask, dtype=bool)
    if wrapped.shape != mask.shape:
        raise ValueError('wrapped phase and mask must have the same shape')
    if not np.isfinite(wrapped[mask]).all():
        raise ValueError('masked voxels must carry a finite measured phase')
    edges = np.asarray(edges, dtype=int)
    if edges.size == 0:
        edges = edges.reshape(0, 2)
    if edges.ndim != 2 or edges.shape[1] != 2:
        raise ValueError('edges must have shape (E, 2)')
    flat_mask = mask.ravel()
    valid_ids = set(np.flatnonzero(flat_mask).tolist())
    for edge in edges:
        if edge[0] not in valid_ids or edge[1] not in valid_ids:
            raise ValueError(
                f'edge {tuple(edge.tolist())} touches a voxel outside the valid mask; '
                'zero-magnitude voxels cannot connect regions'
            )
    if np.isscalar(max_deltas):
        deltas = np.full(edges.shape[0], float(max_deltas))
    else:
        deltas = np.asarray(max_deltas, dtype=np.float64)
        if deltas.shape != (edges.shape[0],):
            raise ValueError('max_deltas must be a scalar or one value per edge')
    if deltas.size and np.any(deltas <= 0.0):
        raise ValueError('allowed phase differences must be positive')
    if deltas.size and np.any(deltas > period / 2.0 + 1e-12 * period):
        raise ValueError(
            'max_deltas cannot exceed half a period: the wrapped difference '
            'only defines the edge difference up to one period'
        )
    anchor_list = []
    if anchors is not None:
        for index, value in anchors.items() if isinstance(anchors, dict) else anchors:
            index = int(index)
            value = float(value)
            if index not in valid_ids:
                raise ValueError(f'anchor voxel {index} is outside the valid mask')
            if not np.isfinite(value):
                raise ValueError('anchor phase must be finite')
            anchor_list.append((index, value))
    if period <= 0.0:
        raise ValueError('period must be positive')
    if tol <= 0.0:
        raise ValueError('tolerance must be positive')
    return wrapped, mask, edges, deltas, anchor_list, flat_mask


def unwrap_phase(
    wrapped,
    mask,
    edges,
    max_deltas=TWO_PI / 2.0,
    anchors=None,
    period=TWO_PI,
    tol=None,
    strict=True,
):
    """Decide whether a jointly valid phase unwrapping exists and describe it.

    Each edge ``(u, v)`` admits the integer-cycle difference ``k`` that brings
    the measured wrapped difference back into the allowed range::

        phi_v - phi_u == wrap(wrapped_v - wrapped_u) + k * period
        |phi_v - phi_u| <= max_delta

    A union-find forest groups voxels joined by edges; a spanning traversal
    then assigns relative potentials.  Every edge, including back edges, is
    checked against those potentials, so contradictory cycles fail with their
    original edges rather than being hidden by a chosen path.  Anchors pin the
    absolute (integer-cycle) offset of a component; unanchored components keep
    a free offset, reported in the result.

    Parameters
    ----------
    wrapped : array-like
        Wrapped phase on the full voxel grid; values outside ``mask`` are
        ignored.
    mask : array-like of bool
        Voxels carrying a measured phase (non-zero magnitude).
    edges : array-like of int, shape ``(E, 2)``
        Adjacency as flat-index pairs into the grid.
    max_deltas : float or array-like, optional
        Maximum allowed absolute phase difference.  A scalar applies to every
        edge; the default is half a period.
    anchors : dict or sequence of pairs, optional
        ``{flat_index: absolute_phase}`` anchors.
    period : float, optional
        Phase period, default ``2 * pi``.
    tol : float, optional
        Absolute tolerance for constraint and anchor checks.  Defaults to
        ``1e-9 * period``.
    strict : bool, optional
        When True (default) raise :class:`PhaseUnwrapError` on contradiction.
        When False return an :class:`UnwrapResult` with ``feasible=False`` and
        the conflict descriptions.

    Returns
    -------
    UnwrapResult
    """
    if tol is None:
        tol = 1e-9 * period
    wrapped, mask, edges, deltas, anchor_list, flat_mask = _validate_inputs(
        wrapped, mask, edges, max_deltas, anchors, period, tol
    )
    wrapped_flat = wrapped.ravel()

    active_flat = np.flatnonzero(flat_mask)
    compact = {flat: idx for idx, flat in enumerate(active_flat)}
    n_nodes = len(compact)
    parent = list(range(n_nodes))
    size = [1] * n_nodes

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if size[ra] < size[rb]:
            ra, rb = rb, ra
        parent[rb] = ra
        size[ra] += size[rb]

    edge_nodes = np.empty_like(edges)
    for row, edge in enumerate(edges):
        a, b = compact[int(edge[0])], compact[int(edge[1])]
        edge_nodes[row] = (a, b)
        union(a, b)

    root_of = np.array([find(i) for i in range(n_nodes)], dtype=int)
    roots = np.unique(root_of)
    root_index = {root: idx for idx, root in enumerate(roots)}

    # Edge increments k * period after wrapping the measured difference.
    raw_diff = wrapped_flat[edges[:, 1]] - wrapped_flat[edges[:, 0]]
    increments = raw_diff - period * np.round(raw_diff / period)
    if np.any(np.abs(increments) > deltas + tol):
        # The unique wrapped candidate already violates the edge; no integer
        # cycle can repair it.  Record it as a single-edge conflict.
        conflicts = [
            _edge_conflict(e, edges, increments, deltas, period)
            for e in np.flatnonzero(np.abs(increments) > deltas + tol)
        ]
        return _infeasible(conflicts, mask, flat_mask, root_of, roots, root_index, strict)

    # Adjacency list: (neighbour, edge id, sign applied to the increment).
    adjacency = [[] for _ in range(n_nodes)]
    for eid, (a, b) in enumerate(edge_nodes):
        adjacency[a].append((b, eid, 1.0))
        adjacency[b].append((a, eid, -1.0))

    potential = np.full(n_nodes, np.nan)
    tree_parent = np.full(n_nodes, -1, dtype=int)
    tree_edge = np.full(n_nodes, -1, dtype=int)
    conflicts = []
    for root in roots:
        potential[root] = 0.0
        queue = [root]
        head = 0
        while head < len(queue):
            u = queue[head]
            head += 1
            for v, eid, sign in adjacency[u]:
                increment = sign * increments[eid]
                if np.isnan(potential[v]):
                    potential[v] = potential[u] + increment
                    tree_parent[v] = u
                    tree_edge[v] = eid
                    queue.append(v)
                    continue
                residual = potential[u] + increment - potential[v]
                if abs(residual) > tol:
                    conflicts.append(
                        _cycle_conflict(
                            eid,
                            u,
                            v,
                            edges,
                            active_flat,
                            tree_parent,
                            tree_edge,
                            residual,
                            period,
                        )
                    )

    anchors_by_component = {}
    for flat_index, value in anchor_list:
        node = compact[flat_index]
        anchors_by_component.setdefault(root_of[node], []).append((node, flat_index, value))

    base_phase = np.zeros(n_nodes)
    determined = np.zeros(n_nodes, dtype=bool)
    offsets = []
    for root in roots:
        entries = anchors_by_component.get(root, [])
        label = root_index[root]
        if entries:
            node0, _, value0 = entries[0]
            base = value0 - potential[node0]
            for node, flat_index, value in entries[1:]:
                residual = potential[node] + base - value
                if abs(residual) > tol:
                    conflicts.append(
                        {
                            'kind': 'anchor',
                            'anchors': (entries[0][1], flat_index),
                            'expected_difference': value - value0,
                            'implied_difference': potential[node] - potential[node0],
                            'residual': residual,
                            'component': label,
                        }
                    )
            base_phase[root_of == root] = base
            determined[root_of == root] = True
            offsets.append(
                {
                    'component': label,
                    'determined': True,
                    'anchor': entries[0][1],
                    'base_phase': float(base),
                }
            )
        else:
            offsets.append(
                {
                    'component': label,
                    'determined': False,
                    'anchor': None,
                    'base_phase': 0.0,
                    'free_cycles': f'k{label} * period, k{label} in Z',
                }
            )

    component_grid = np.full(mask.size, -1, dtype=int)
    determined_grid = np.zeros(mask.size, dtype=bool)
    phase_grid = np.full(mask.size, np.nan)
    if not conflicts:
        active_flat = np.flatnonzero(flat_mask)
        node_phase = potential + base_phase
        phase_grid[active_flat] = node_phase
        component_grid[active_flat] = _root_index_map(root_of, root_index)
        determined_grid[active_flat] = determined
        phase_grid = phase_grid.reshape(mask.shape)
        result = UnwrapResult(
            feasible=True,
            phase=phase_grid,
            component=component_grid.reshape(mask.shape),
            is_determined=determined_grid.reshape(mask.shape),
            measured_mask=mask,
            offsets=offsets,
            conflicts=[],
        )
        return result

    component_grid = component_grid.reshape(mask.shape)
    determined_grid = determined_grid.reshape(mask.shape)
    if strict:
        raise PhaseUnwrapError(
            f'phase constraints are inconsistent; {len(conflicts)} conflict(s) found',
            conflicts,
        )
    return UnwrapResult(
        feasible=False,
        phase=None,
        component=component_grid,
        is_determined=determined_grid,
        measured_mask=mask,
        offsets=offsets,
        conflicts=conflicts,
    )


def _root_index_map(root_of, root_index):
    return np.array([root_index[root] for root in root_of], dtype=int)


def _edge_conflict(eid, edges, increments, deltas, period):
    u, v = int(edges[eid, 0]), int(edges[eid, 1])
    return {
        'kind': 'edge',
        'edge': (u, v),
        'edge_id': int(eid),
        'required_difference': float(increments[eid]),
        'allowed_difference': float(deltas[eid]),
        'residual': float(abs(increments[eid]) - deltas[eid]),
        'period': float(period),
    }


def _cycle_conflict(eid, u, v, edges, flat_of, tree_parent, tree_edge, residual, period):
    """Reconstruct the spanning-tree path closing the conflicting edge."""

    def path_to_root(node):
        nodes, edge_ids = [], []
        while tree_parent[node] != -1:
            edge_ids.append(int(tree_edge[node]))
            nodes.append(int(node))
            node = tree_parent[node]
        nodes.append(int(node))
        return nodes, edge_ids

    u_nodes, u_edges = path_to_root(u)
    v_nodes, v_edges = path_to_root(v)
    raw_nodes = [*reversed(u_nodes), *v_nodes]
    # The lowest common ancestor is shared by both branches; keep one copy.
    cycle_nodes = list(dict.fromkeys(int(flat_of[node]) for node in raw_nodes))
    cycle_edges = [*reversed(u_edges), int(eid), *reversed(v_edges)]
    original_edges = [tuple(edges[e].tolist()) for e in cycle_edges]
    return {
        'kind': 'cycle',
        'edge': (int(edges[eid, 0]), int(edges[eid, 1])),
        'edge_id': int(eid),
        'cycle_nodes': cycle_nodes,
        'cycle_edges': original_edges,
        'residual': float(residual),
        'period': float(period),
    }


def _infeasible(conflicts, mask, flat_mask, root_of, roots, root_index, strict):
    active_flat = np.flatnonzero(flat_mask)
    component_grid = np.full(mask.size, -1, dtype=int)
    component_grid[active_flat] = _root_index_map(root_of, root_index)
    component_grid = component_grid.reshape(mask.shape)
    determined_grid = np.zeros(mask.shape, dtype=bool)
    if strict:
        raise PhaseUnwrapError(
            f'phase constraints are inconsistent; {len(conflicts)} conflict(s) found',
            conflicts,
        )
    return UnwrapResult(
        feasible=False,
        phase=None,
        component=component_grid,
        is_determined=determined_grid,
        measured_mask=mask,
        offsets=[],
        conflicts=conflicts,
    )


def compose_phase(result, cycle_offsets, period=TWO_PI):
    """Add integer-cycle offsets to an :class:`UnwrapResult`.

    Parameters
    ----------
    result : UnwrapResult
        A feasible result.
    cycle_offsets : dict
        ``{component_label: integer_cycles}``; determined components need no
        entry and any non-zero entry for them is rejected.
    period : float, optional
        Phase period, default ``2 * pi``.

    Returns
    -------
    phase : ndarray
        New continuous-phase grid; edge differences are unchanged, so every
        constraint still holds.
    """
    if not result.feasible or result.phase is None:
        raise ValueError('cannot compose phase from an infeasible result')
    valid_labels = {entry['component'] for entry in result.offsets}
    unknown = set(cycle_offsets) - valid_labels
    if unknown:
        raise ValueError(f'unknown component labels: {sorted(unknown)}')
    phase = result.phase.copy()
    for entry in result.offsets:
        label = entry['component']
        cycles = cycle_offsets.get(label, 0)
        if int(cycles) != cycles:
            raise ValueError('cycle offsets must be whole integer numbers of periods')
        if cycles == 0:
            continue
        if entry['determined']:
            raise ValueError(f'component {label} is anchored; its phase is fixed')
        phase[result.component == label] += int(cycles) * period
    return phase


def verify_unwrapping(
    phase, mask, edges, wrapped, max_deltas=TWO_PI / 2.0, anchors=None, period=TWO_PI, tol=None
):
    """Check a continuous phase against every edge and anchor constraint.

    Returns True when all edge differences are within range and all masked
    voxels reproduce the wrapped measurements modulo the period.
    """
    if tol is None:
        tol = 1e-9 * period
    phase = np.asarray(phase, dtype=np.float64).ravel()
    mask = np.asarray(mask, dtype=bool).ravel()
    wrapped = np.asarray(wrapped, dtype=np.float64).ravel()
    edges = np.asarray(edges, dtype=int)
    if edges.size == 0:
        edges = edges.reshape(0, 2)
    if np.isscalar(max_deltas):
        deltas = np.full(edges.shape[0], float(max_deltas))
    else:
        deltas = np.asarray(max_deltas, dtype=np.float64)
    if edges.size:
        if not mask[edges].all():
            return False
        diffs = phase[edges[:, 1]] - phase[edges[:, 0]]
        if np.any(np.abs(diffs) > deltas + tol):
            return False
    measured = np.flatnonzero(mask)
    residual = (phase[measured] - wrapped[measured]) / period
    if np.any(np.abs(residual - np.round(residual)) > tol / period):
        return False
    if anchors is not None:
        items = anchors.items() if isinstance(anchors, dict) else anchors
        for index, value in items:
            if abs(phase[int(index)] - float(value)) > tol:
                return False
    return True


def split_complex_image(image, mask=None, zero_tol=0.0):
    """Split a complex spatial image into magnitude and wrapped-phase images.

    The affine is shared by both output images and the voxel correspondence is
    unchanged.  Returns ``(magnitude_image, phase_image, measured_mask)``.
    """
    data = np.asanyarray(image.dataobj)
    if not np.iscomplexobj(data):
        raise TypeError('image dataobj must be complex-valued')
    magnitude, phase, measured_mask = complex_to_magnitude_phase(data, mask, zero_tol)
    return (
        Nifti1Image(magnitude, image.affine),
        Nifti1Image(phase, image.affine),
        measured_mask,
    )


def recombine_complex_image(magnitude_image, phase_image):
    """Rebuild a complex image from magnitude and phase images.

    Both inputs must share a shape; the affine of the magnitude image is used.
    """
    magnitude = np.asanyarray(magnitude_image.dataobj)
    phase = np.asanyarray(phase_image.dataobj)
    if magnitude.shape != phase.shape:
        raise ValueError('magnitude and phase images must have the same shape')
    rebuilt = magnitude_phase_to_complex(magnitude, phase)
    return Nifti1Image(rebuilt, magnitude_image.affine)
