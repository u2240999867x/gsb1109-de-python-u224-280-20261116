# emacs: -*- mode: python-mode; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the NiBabel package for the
#   copyright and license terms.
#
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Testing phase_unwrap module"""

import numpy as np
import pytest
from numpy.testing import assert_allclose, assert_array_equal

from nibabel.nifti1 import Nifti1Image
from nibabel.phase_unwrap import (
    TWO_PI,
    PhaseUnwrapError,
    build_voxel_adjacency,
    complex_to_magnitude_phase,
    compose_phase,
    magnitude_phase_to_complex,
    recombine_complex_image,
    split_complex_image,
    unwrap_phase,
    verify_unwrapping,
)


def test_complex_split_roundtrip():
    rng = np.random.default_rng(0)
    shape = (4, 5, 3)
    magnitude = rng.random(shape)
    phase = rng.uniform(-np.pi, np.pi, shape)
    data = magnitude * np.exp(1j * phase)

    mag, wrapped, measured = complex_to_magnitude_phase(data)
    assert_allclose(mag, magnitude)
    assert_allclose(wrapped, np.angle(data))
    assert measured.all()

    rebuilt = magnitude_phase_to_complex(mag, wrapped)
    assert_allclose(rebuilt, data, atol=1e-12)


def test_real_two_component_array_rejected():
    # A plain real vector / two-component array is not a complex image.
    with pytest.raises(TypeError):
        complex_to_magnitude_phase(np.array([1.0, 2.0]))
    with pytest.raises(TypeError):
        complex_to_magnitude_phase(np.stack([np.ones(3), np.zeros(3)], axis=0))


def test_zero_magnitude_has_no_phase_and_cannot_connect():
    data = np.array([1.0 + 0j, 0.0, 1.0 + 1j])
    mag, wrapped, measured = complex_to_magnitude_phase(data)
    assert_array_equal(measured, [True, False, True])
    assert np.isnan(wrapped[1])
    assert mag[1] == 0.0

    edges = build_voxel_adjacency(measured, connectivity=1)
    # The zero voxel breaks the chain: the two live voxels are not adjacent.
    assert edges.size == 0

    rebuilt = magnitude_phase_to_complex(mag, wrapped)
    assert rebuilt[1] == 0.0
    assert_allclose(rebuilt[[0, 2]], data[[0, 2]], atol=1e-12)


def test_external_mask_excludes_nonzero_voxels():
    data = np.exp(1j * np.array([0.1, 0.2, 0.3]))
    user_mask = np.array([True, False, True])
    _, wrapped, measured = complex_to_magnitude_phase(data, mask=user_mask)
    assert_array_equal(measured, user_mask)
    assert np.isnan(wrapped[1])


def test_build_adjacency_connectivity():
    mask = np.ones((2, 2, 2), dtype=bool)
    edges6 = build_voxel_adjacency(mask, connectivity=1)
    edges18 = build_voxel_adjacency(mask, connectivity=2)
    edges26 = build_voxel_adjacency(mask, connectivity=3)
    assert len(edges6) == 12
    assert len(edges18) == 24
    assert len(edges26) == 28
    # All 28 unordered pairs among the 8 voxels exist at full connectivity.
    assert edges26.shape == (28, 2)
    # Unique, low-to-high oriented undirected edges.
    assert_array_equal(edges6, np.sort(edges6, axis=1))
    assert len(np.unique(edges6, axis=0)) == len(edges6)


def test_edge_touching_invalid_voxel_rejected():
    mask = np.array([True, True, False])
    wrapped = np.array([0.1, 0.2, np.nan])
    with pytest.raises(ValueError):
        unwrap_phase(wrapped, mask, np.array([[0, 2]]), max_deltas=np.pi)


def test_smooth_field_single_component():
    # A smooth linear phase wraps around the period but stays within pi per edge.
    grid = np.linspace(0.0, 6.0, 25)
    true_phase = grid * 1.3
    data = np.exp(1j * true_phase)
    mag, wrapped, mask = complex_to_magnitude_phase(data)
    edges = build_voxel_adjacency(mask, connectivity=1)

    result = unwrap_phase(wrapped, mask, edges, anchors={0: true_phase[0]})
    assert result.feasible
    assert result.is_determined.all()
    assert_allclose(result.phase, true_phase, atol=1e-9)
    assert verify_unwrapping(result.phase, mask, edges, wrapped, anchors={0: true_phase[0]})


def test_unanchored_component_keeps_free_cycle_offset():
    mask = np.ones(6, dtype=bool)
    # Two disconnected runs of three voxels each.
    edges = np.array([[0, 1], [1, 2], [3, 4], [4, 5]])
    wrapped = np.zeros(6)
    result = unwrap_phase(wrapped, mask, edges, anchors={0: 0.0})
    assert result.feasible
    labels = np.unique(result.component)
    assert_array_equal(labels, [0, 1])
    assert result.is_determined[0] and not result.is_determined[3]
    offset_entries = {entry['component']: entry for entry in result.offsets}
    assert offset_entries[0]['determined']
    assert not offset_entries[1]['determined']
    assert 'free_cycles' in offset_entries[1]

    shifted = compose_phase(result, {1: 2})
    assert_allclose(shifted[:3], 0.0)
    assert_allclose(shifted[3:], 2 * TWO_PI)
    # Edge differences are unchanged, so constraints still hold.
    assert verify_unwrapping(shifted, mask, edges, wrapped, anchors={0: 0.0})

    with pytest.raises(ValueError):
        compose_phase(result, {0: 1})
    with pytest.raises(ValueError):
        compose_phase(result, {1: 0.5})
    with pytest.raises(ValueError):
        compose_phase(result, {7: 1})


def test_empty_mask_is_feasible_with_empty_phase():
    mask = np.zeros(4, dtype=bool)
    wrapped = np.full(4, np.nan)
    edges = np.empty((0, 2), dtype=int)
    result = unwrap_phase(wrapped, mask, edges)
    assert result.feasible
    assert result.phase.shape == mask.shape
    assert np.isnan(result.phase).all()
    assert (result.component == -1).all()
    assert result.offsets == []
    assert verify_unwrapping(result.phase, mask, edges, wrapped)


def test_adjacency_clamps_to_grid_dimension():
    mask = np.ones((3, 3), dtype=bool)
    # The default 6-connectivity is clamped to the two axes: full 8-neighbour.
    edges = build_voxel_adjacency(mask)
    assert len(edges) == 20  # 12 face plus 8 diagonal neighbour pairs
    # Explicit face-only connectivity gives axis-aligned links.
    face_edges = build_voxel_adjacency(mask, connectivity=1)
    assert len(face_edges) == 12
    assert set(np.diff(face_edges, axis=1).ravel()) <= {1, 3}
    with pytest.raises(ValueError):
        build_voxel_adjacency(mask, connectivity=0)


def test_anchor_pins_absolute_phase_through_wraps():
    mask = np.ones(4, dtype=bool)
    edges = np.array([[0, 1], [1, 2], [2, 3]])
    true_phase = np.array([0.0, 3.0, 6.0, 9.0])
    wrapped = np.mod(true_phase + np.pi, TWO_PI) - np.pi
    result = unwrap_phase(wrapped, mask, edges, anchors={3: true_phase[3]})
    assert result.feasible
    assert_allclose(result.phase, true_phase, atol=1e-9)


def test_contradictory_anchor_reported():
    mask = np.ones(3, dtype=bool)
    edges = np.array([[0, 1], [1, 2]])
    wrapped = np.array([0.0, 0.5, 1.0])
    result = unwrap_phase(wrapped, mask, edges, anchors={0: 0.0, 2: 5.0}, strict=False)
    assert not result.feasible
    assert result.conflicts[0]['kind'] == 'anchor'
    with pytest.raises(PhaseUnwrapError) as exc:
        unwrap_phase(wrapped, mask, edges, anchors={0: 0.0, 2: 5.0})
    assert exc.value.conflicts[0]['kind'] == 'anchor'


def test_cycle_contradiction_reports_original_edges():
    # Triangle whose edge increments do not sum to zero: the back edge closes
    # an inconsistent cycle instead of being silently dropped by a scan path.
    mask = np.ones(3, dtype=bool)
    edges = np.array([[0, 1], [1, 2], [0, 2]])
    # Every edge increment lies within pi, but they sum to one full period.
    wrapped = np.array([0.0, 2.0, 4.0 - TWO_PI])
    result = unwrap_phase(wrapped, mask, edges, max_deltas=np.pi, strict=False)
    assert not result.feasible
    conflict = result.conflicts[0]
    assert conflict['kind'] == 'cycle'
    assert abs(abs(conflict['residual']) - TWO_PI) < 1e-9
    # The offending original edge and the spanning-tree path closing it are
    # both reported, using original flat indices rather than scan order.
    assert tuple(conflict['edge']) in {(0, 1), (1, 2), (0, 2)}
    reported = [tuple(edge) for edge in conflict['cycle_edges']]
    assert tuple(conflict['edge']) in reported
    assert set(reported) == {(0, 1), (1, 2), (0, 2)}
    assert set(conflict['cycle_nodes']) == {0, 1, 2}

    with pytest.raises(PhaseUnwrapError):
        unwrap_phase(wrapped, mask, edges, max_deltas=np.pi)


def test_consistent_cycle_is_feasible():
    mask = np.ones(4, dtype=bool)
    edges = np.array([[0, 1], [1, 2], [2, 3], [0, 3]])
    true_phase = np.array([0.0, 0.5, 1.0, 0.7])
    wrapped = np.mod(true_phase + np.pi, TWO_PI) - np.pi
    result = unwrap_phase(wrapped, mask, edges, max_deltas=np.pi)
    assert result.feasible
    # Relative phases match up to one free component offset.
    assert_allclose(result.phase - result.phase[0], true_phase - true_phase[0], atol=1e-9)


def test_edge_difference_beyond_limit_is_conflict():
    mask = np.ones(2, dtype=bool)
    edges = np.array([[0, 1]])
    wrapped = np.array([0.0, 3.0])
    result = unwrap_phase(wrapped, mask, edges, max_deltas=1.0, period=TWO_PI, strict=False)
    assert not result.feasible
    assert result.conflicts[0]['kind'] == 'edge'


def test_per_edge_limits_and_custom_period():
    period = 360.0
    mask = np.ones(3, dtype=bool)
    edges = np.array([[0, 1], [1, 2]])
    wrapped = np.array([0.0, 10.0, 350.0])
    limits = np.array([20.0, 30.0])
    result = unwrap_phase(wrapped, mask, edges, max_deltas=limits, period=period, anchors={0: 0.0})
    assert result.feasible
    # 350 wraps to -10 relative to the 10-degree middle voxel.
    assert_allclose(result.phase, [0.0, 10.0, -10.0], atol=1e-7)
    assert verify_unwrapping(
        result.phase,
        mask,
        edges,
        wrapped,
        max_deltas=limits,
        anchors={0: 0.0},
        period=period,
    )


def test_rebuild_complex_after_unwrapping_within_tolerance():
    rng = np.random.default_rng(1)
    shape = (6, 4, 2)
    true_phase = np.cumsum(rng.normal(0.0, 0.5, shape), axis=0)
    magnitude = rng.random(shape) + 0.1
    data = magnitude * np.exp(1j * true_phase)
    mag, wrapped, mask = complex_to_magnitude_phase(data)
    edges = build_voxel_adjacency(mask, connectivity=1)
    anchor = {int(np.flatnonzero(mask.ravel())[0]): true_phase.ravel()[mask.ravel()][0]}
    result = unwrap_phase(wrapped, mask, edges, anchors=anchor)
    assert result.feasible

    rebuilt = magnitude_phase_to_complex(mag, result.phase)
    assert_allclose(rebuilt, data, atol=1e-10)


def test_image_split_and_recombine():
    affine = np.diag([2.0, 2.0, 2.0, 1.0])
    data = np.array([[1.0 + 0j, 0.0], [0.0 + 1j, 2.0 - 2.0j]], dtype=np.complex128).reshape(
        2, 2, 1
    )
    image = Nifti1Image(data, affine)
    mag_img, phase_img, measured = split_complex_image(image)
    assert_allclose(mag_img.affine, affine)
    assert not measured[0, 1, 0]
    assert np.isnan(np.asanyarray(phase_img.dataobj)[0, 1, 0])

    recombined = recombine_complex_image(mag_img, phase_img)
    assert_allclose(np.asanyarray(recombined.dataobj), data, atol=1e-12)
    assert_allclose(recombined.affine, affine)

    with pytest.raises(TypeError):
        split_complex_image(Nifti1Image(np.zeros((2, 2, 1)), affine))


def test_shape_mismatch_rejected_on_recombine():
    affine = np.eye(4)
    mag = Nifti1Image(np.ones((2, 2, 1)), affine)
    phase = Nifti1Image(np.zeros((2, 3, 1)), affine)
    with pytest.raises(ValueError):
        recombine_complex_image(mag, phase)
