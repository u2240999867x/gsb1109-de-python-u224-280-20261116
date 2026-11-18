import numpy as np
import pytest
from numpy.testing import assert_allclose, assert_array_equal

import nibabel as nib
from nibabel.streamlines import LazyTractogram, Tractogram
from nibabel.streamlines.utils import (
    SEGMENT_POSITION_KEY,
    SOURCE_SEGMENT_KEY,
    SOURCE_STREAMLINE_KEY,
    clip_tractogram,
)

IDENTITY = np.eye(4)


def axis_line(xs, y=0.0, z=0.0):
    """Polyline running along the x axis through ``xs``."""
    xs = np.asarray(xs, dtype='f4')
    return np.column_stack([xs, np.full_like(xs, y), np.full_like(xs, z)])


def slab_mask(lo, hi, size=10):
    """Mask of voxels with x-index in ``[lo, hi)``; faces at half-integers."""
    mask = np.zeros((size, size, size), dtype=bool)
    mask[lo:hi, :, :] = True
    return mask


def test_clips_at_voxel_faces_not_vertices():
    # ROI voxels 3..6 span x cells [2.5, 6.5); no input vertex sits on a face.
    mask = slab_mask(3, 7)
    streamline = axis_line(np.arange(10))
    tractogram = Tractogram([streamline], affine_to_rasmm=IDENTITY)

    clipped = clip_tractogram(tractogram, mask, IDENTITY)

    assert len(clipped) == 1
    assert_allclose(clipped[0].streamline[:, 0], [2.5, 3.0, 4.0, 5.0, 6.0, 6.5])
    # Output stays in the input coordinate space.
    assert_array_equal(clipped.affine_to_rasmm, IDENTITY)


def test_segment_with_both_endpoints_outside_is_cut():
    # Both endpoints are outside; merely keeping inside vertices would drop
    # the streamline entirely instead of producing the crossing fragment.
    mask = slab_mask(3, 7)
    streamline = axis_line([0.0, 9.0])
    tractogram = Tractogram([streamline], affine_to_rasmm=IDENTITY)

    clipped = clip_tractogram(tractogram, mask, IDENTITY)

    assert len(clipped) == 1
    assert_allclose(clipped[0].streamline[:, 0], [2.5, 6.5])


def test_reentry_produces_one_fragment_per_intersection():
    mask = slab_mask(3, 7)
    streamline = axis_line([0.0, 4.0, 8.0, 4.0, 0.0])
    tractogram = Tractogram([streamline], affine_to_rasmm=IDENTITY)

    clipped = clip_tractogram(tractogram, mask, IDENTITY)

    assert len(clipped) == 2
    assert_allclose(clipped[0].streamline[:, 0], [2.5, 4.0, 6.5])
    assert_allclose(clipped[1].streamline[:, 0], [6.5, 4.0, 2.5])


def test_corner_crossing_uses_shared_intersection_point():
    mask = slab_mask(2, 7)
    streamline = np.array([[-1.0, -1.0, -1.0], [11.0, 11.0, 11.0]], dtype='f4')
    tractogram = Tractogram([streamline], affine_to_rasmm=IDENTITY)

    clipped = clip_tractogram(tractogram, mask, IDENTITY)

    assert len(clipped) == 1
    assert_allclose(clipped[0].streamline[0], [1.5, 1.5, 1.5], atol=1e-6)
    assert_allclose(clipped[0].streamline[-1], [6.5, 6.5, 6.5], atol=1e-6)


def test_vertex_on_face_leaving_the_mask_closes_fragment():
    mask = slab_mask(2, 7)
    # Vertex 1 sits exactly on the entry face x=1.5, both adjacent segments
    # stay outside: a measure-zero contact must not create a fragment.
    touching = axis_line([0.0, 1.5, 1.4], y=0.0)
    touching[2, 1] = 5.0
    assert (
        len(clip_tractogram(Tractogram([touching], affine_to_rasmm=IDENTITY), mask, IDENTITY)) == 0
    )

    # An inside portion ending at that same vertex keeps it as the endpoint.
    inside_to_face = axis_line([2.0, 1.5, 1.4])
    inside_to_face[2, 1] = 5.0
    clipped = clip_tractogram(
        Tractogram([inside_to_face], affine_to_rasmm=IDENTITY), mask, IDENTITY
    )
    assert len(clipped) == 1
    assert_allclose(clipped[0].streamline[:, 0], [2.0, 1.5])


def test_single_point_streamline():
    mask = slab_mask(3, 7)
    inside = Tractogram([axis_line([4.0])], affine_to_rasmm=IDENTITY)
    outside = Tractogram([axis_line([0.0])], affine_to_rasmm=IDENTITY)

    assert len(clip_tractogram(inside, mask, IDENTITY)) == 1
    assert_allclose(clip_tractogram(inside, mask, IDENTITY)[0].streamline, [[4.0, 0.0, 0.0]])
    assert len(clip_tractogram(outside, mask, IDENTITY)) == 0


def test_per_streamline_and_per_point_data_migrate_separately():
    mask = slab_mask(3, 7)
    streamline = axis_line(np.arange(10))
    fa = np.arange(10, dtype='f4').reshape(-1, 1)
    labels = (np.arange(10) * 10).astype('i4').reshape(-1, 1)
    tractogram = Tractogram(
        [streamline],
        data_per_streamline={'score': np.array([[7.5]], dtype='f4')},
        data_per_point={'fa': [fa], 'label': [labels]},
        affine_to_rasmm=IDENTITY,
    )

    clipped = clip_tractogram(tractogram, mask, IDENTITY)

    # Per-streamline data is carried onto the fragment unchanged.
    assert_allclose(clipped.data_per_streamline['score'], [[7.5]])
    # Float per-point data is linearly interpolated at the new boundary points.
    assert_allclose(clipped.data_per_point['fa'][0].ravel(), [2.5, 3.0, 4.0, 5.0, 6.0, 6.5])
    # Integer labels are categorical: no invented intermediate value, the
    # boundary points keep the label of their segment start vertex.
    assert_array_equal(clipped.data_per_point['label'][0].ravel(), [20, 30, 40, 50, 60, 60])


def test_explicit_categorical_float_key_is_not_interpolated():
    mask = slab_mask(2, 7)
    streamline = axis_line([0.0, 3.5, 6.0, 9.0])
    # The entry boundary cuts segment 0 (tags 0 -> 2): categorical handling
    # keeps the segment-start label 0, whereas continuous handling interpolates.
    tags = np.array([[0.0], [2.0], [2.0], [2.0]], dtype='f4')
    tractogram = Tractogram(
        [streamline],
        data_per_point={'tag': [tags]},
        affine_to_rasmm=IDENTITY,
    )

    categorical = clip_tractogram(tractogram, mask, IDENTITY, categorical_keys={'tag'})
    assert_array_equal(categorical.data_per_point['tag'][0].ravel(), [0.0, 2.0, 2.0, 2.0])

    interpolated = clip_tractogram(tractogram, mask, IDENTITY)
    expected_entry = 2.0 * (1.5 / 3.5)
    assert interpolated.data_per_point['tag'][0][0, 0] == pytest.approx(expected_entry)


def test_provenance_records_source_streamline_segment_and_position():
    mask = slab_mask(3, 7)
    crossing = axis_line([0.0, 9.0])
    missing = axis_line([40.0, 50.0])
    tractogram = Tractogram([crossing, missing], affine_to_rasmm=IDENTITY)

    clipped = clip_tractogram(tractogram, mask, IDENTITY)

    assert len(clipped) == 1
    assert_array_equal(clipped.data_per_streamline[SOURCE_STREAMLINE_KEY], [[0]])
    # Both fragment points are newly created: segment 0, parameters 2.5/9 and
    # 6.5/9 of that segment.
    assert_array_equal(clipped.data_per_point[SOURCE_SEGMENT_KEY][0].ravel(), [0, 0])
    assert_allclose(
        clipped.data_per_point[SEGMENT_POSITION_KEY][0].ravel(),
        [2.5 / 9.0, 6.5 / 9.0],
    )

    # Original vertices keep segment -1 and position 0 (see first test case).
    dense = Tractogram([axis_line(np.arange(10))], affine_to_rasmm=IDENTITY)
    dense_clipped = clip_tractogram(dense, mask, IDENTITY)
    assert_array_equal(
        dense_clipped.data_per_point[SOURCE_SEGMENT_KEY][0].ravel(),
        [2, -1, -1, -1, -1, 6],
    )
    assert_allclose(
        dense_clipped.data_per_point[SEGMENT_POSITION_KEY][0].ravel(),
        [0.5, 0.0, 0.0, 0.0, 0.0, 0.5],
    )


def test_provenance_can_be_disabled():
    mask = slab_mask(3, 7)
    streamline = axis_line(np.arange(10))
    tractogram = Tractogram(
        [streamline],
        data_per_point={'fa': [np.ones((10, 1), dtype='f4')]},
        affine_to_rasmm=IDENTITY,
    )

    clipped = clip_tractogram(tractogram, mask, IDENTITY, provenance=False)

    assert SOURCE_STREAMLINE_KEY not in clipped.data_per_streamline
    assert SOURCE_SEGMENT_KEY not in clipped.data_per_point
    assert SEGMENT_POSITION_KEY not in clipped.data_per_point
    assert 'fa' in clipped.data_per_point  # original data is preserved


def test_provenance_key_collision_raises():
    mask = slab_mask(3, 7)
    tractogram = Tractogram(
        [axis_line(np.arange(10))],
        data_per_point={SOURCE_SEGMENT_KEY: [np.zeros((10, 1), dtype='i4')]},
        affine_to_rasmm=IDENTITY,
    )
    with pytest.raises(ValueError, match='Provenance key collision'):
        clip_tractogram(tractogram, mask, IDENTITY)


def test_reversed_streamline_has_same_spatial_coverage():
    mask = slab_mask(2, 7)
    rng = np.random.RandomState(42)
    for _ in range(30):
        nb_points = rng.randint(2, 9)
        line = np.column_stack(
            [
                rng.uniform(-1.0, 10.0, nb_points),
                rng.uniform(1.0, 8.0, nb_points),
                rng.uniform(1.0, 8.0, nb_points),
            ]
        ).astype('f4')

        forward = clip_tractogram(Tractogram([line], affine_to_rasmm=IDENTITY), mask, IDENTITY)
        reverse = clip_tractogram(
            Tractogram([line[::-1].copy()], affine_to_rasmm=IDENTITY),
            mask,
            IDENTITY,
        )

        assert len(forward) == len(reverse)
        forward_lines = list(forward.streamlines)
        reverse_lines = list(reverse.streamlines)
        for forward_line, reverse_line in zip(forward_lines, reverse_lines[::-1]):
            assert_allclose(forward_line, reverse_line[::-1], atol=1e-6)


def test_mask_affine_locates_roi_in_world_space():
    roi_affine = np.diag([2.0, 2.0, 2.0, 1.0])
    roi_affine[:3, 3] = [1.0, -2.0, 4.0]
    mask = np.zeros((6, 6, 6), dtype=np.uint8)
    mask[1:3, 1:3, 1:3] = 1
    image = nib.Nifti1Image(mask, roi_affine)

    # World line crossing the ROI: voxel x faces 0.5 and 2.5 map to 2 and 6.
    world_line = np.array([[0.0, 0.0, 7.0], [10.0, 0.0, 7.0]], dtype='f4')
    tractogram = Tractogram([world_line], affine_to_rasmm=IDENTITY)

    from_image = clip_tractogram(tractogram, image)
    from_array = clip_tractogram(tractogram, mask.astype(bool), roi_affine)

    assert_allclose(from_image[0].streamline[:, 0], [2.0, 6.0])
    assert_allclose(from_array[0].streamline, from_image[0].streamline)

    # Points stored in voxel space, mapped to world through the same affine.
    voxel_line = np.array([[0.0, 1.0, 1.0], [5.0, 1.0, 1.0]], dtype='f4')
    voxel_tractogram = Tractogram([voxel_line], affine_to_rasmm=roi_affine)
    clipped = clip_tractogram(voxel_tractogram, mask.astype(bool), roi_affine)
    assert_allclose(clipped[0].streamline[:, 0], [0.5, 2.5])
    assert_array_equal(clipped.affine_to_rasmm, roi_affine)


def test_empty_intersection_returns_empty_tractogram():
    mask = slab_mask(3, 7)
    tractogram = Tractogram(
        [axis_line([0.0, 1.0]), axis_line([8.0, 9.0])],
        affine_to_rasmm=IDENTITY,
    )

    clipped = clip_tractogram(tractogram, np.zeros_like(mask), IDENTITY)

    assert len(clipped) == 0
    assert_array_equal(clipped.affine_to_rasmm, IDENTITY)
    assert len(clipped.data_per_streamline) == 0
    assert len(clipped.data_per_point) == 0


def test_missing_data_dictionaries_are_supported():
    mask = slab_mask(3, 7)
    tractogram = Tractogram([axis_line([0.0, 9.0])], affine_to_rasmm=IDENTITY)

    clipped = clip_tractogram(tractogram, mask, IDENTITY)

    assert len(clipped) == 1
    # Only provenance data is present; nothing else was fabricated.
    assert set(clipped.data_per_point) == {
        SOURCE_SEGMENT_KEY,
        SEGMENT_POSITION_KEY,
    }
    assert set(clipped.data_per_streamline) == {SOURCE_STREAMLINE_KEY}


def test_missing_spatial_information_raises():
    mask = slab_mask(3, 7)
    line = axis_line([0.0, 9.0])

    # Tractogram without a known reference space.
    with pytest.raises(ValueError, match='unknown coordinate space'):
        clip_tractogram(Tractogram([line]), mask, IDENTITY)

    # Plain ndarray mask without an affine.
    tractogram = Tractogram([line], affine_to_rasmm=IDENTITY)
    with pytest.raises(ValueError, match='ROI affine is required'):
        clip_tractogram(tractogram, mask)

    # Non-3D mask.
    with pytest.raises(ValueError, match='3D voxel mask'):
        clip_tractogram(tractogram, np.zeros((3, 3), dtype=bool), IDENTITY)


def test_lazy_tractogram_input_returns_lazy_output():
    mask = slab_mask(3, 7)
    streamline = axis_line(np.arange(10))
    tractogram = Tractogram(
        [streamline],
        data_per_point={'fa': [np.arange(10, dtype='f4').reshape(-1, 1)]},
        affine_to_rasmm=IDENTITY,
    )
    lazy = LazyTractogram.from_tractogram(tractogram)

    clipped = clip_tractogram(lazy, mask, IDENTITY)

    assert isinstance(clipped, LazyTractogram)
    materialized = list(clipped.streamlines)
    assert len(materialized) == 1
    assert_allclose(materialized[0][:, 0], [2.5, 3.0, 4.0, 5.0, 6.0, 6.5])
    assert_array_equal(clipped.affine_to_rasmm, IDENTITY)

    # Provenance is migrated in lazy mode as well.
    assert_array_equal(np.asarray(list(clipped.data_per_streamline[SOURCE_STREAMLINE_KEY])), [[0]])
    lazy_point_data = list(clipped.data_per_point[SOURCE_SEGMENT_KEY])
    assert_array_equal(lazy_point_data[0].ravel(), [2, -1, -1, -1, -1, 6])


def test_each_fragment_keeps_per_streamline_data():
    mask = slab_mask(3, 7)
    # One streamline crosses twice, producing two fragments.
    streamline = axis_line([0.0, 4.0, 8.0, 4.0, 0.0])
    tractogram = Tractogram(
        [streamline],
        data_per_streamline={'score': np.array([[9.9]], dtype='f4')},
        affine_to_rasmm=IDENTITY,
    )

    clipped = clip_tractogram(tractogram, mask, IDENTITY)

    assert len(clipped) == 2
    assert_allclose(clipped.data_per_streamline['score'], [[9.9], [9.9]])
    assert_array_equal(clipped.data_per_streamline[SOURCE_STREAMLINE_KEY].ravel(), [0, 0])


def test_multicolumn_and_boolean_per_point_data():
    mask = slab_mask(3, 7)
    streamline = axis_line(np.arange(10))
    colors = np.tile(np.arange(10, dtype='f4')[:, None], (1, 3))
    flags = (np.arange(10) % 2 == 0).reshape(-1, 1)
    tractogram = Tractogram(
        [streamline],
        data_per_point={'colors': [colors], 'flag': [flags]},
        affine_to_rasmm=IDENTITY,
    )

    clipped = clip_tractogram(tractogram, mask, IDENTITY)

    assert_allclose(
        clipped.data_per_point['colors'][0],
        [[2.5, 2.5, 2.5], [3, 3, 3], [4, 4, 4], [5, 5, 5], [6, 6, 6], [6.5, 6.5, 6.5]],
    )
    # Boolean per-point data is categorical: boundary points keep segment-start flags.
    assert clipped.data_per_point['flag'][0].dtype == bool
    assert_array_equal(
        clipped.data_per_point['flag'][0].ravel(), [True, False, True, False, True, True]
    )


def test_integer_streamline_coordinates_are_promoted_for_boundary_points():
    mask = slab_mask(3, 7)
    # Fragment with new boundary points -> float64 to retain sub-voxel positions.
    cut = clip_tractogram(
        Tractogram([np.array([[0, 0, 0], [9, 0, 0]], dtype='i4')], affine_to_rasmm=IDENTITY),
        mask,
        IDENTITY,
    )
    assert cut[0].streamline.dtype == np.float64
    assert_allclose(cut[0].streamline[:, 0], [2.5, 6.5])

    # Fragment made only of original vertices -> preserves the integer dtype.
    whole = clip_tractogram(
        Tractogram([np.array([[3, 0, 0], [4, 0, 0]], dtype='i4')], affine_to_rasmm=IDENTITY),
        mask,
        IDENTITY,
    )
    assert whole[0].streamline.dtype == np.int32
    assert_array_equal(whole[0].streamline, [[3, 0, 0], [4, 0, 0]])


def test_empty_input_tractogram():
    mask = slab_mask(3, 7)
    empty = clip_tractogram(Tractogram([], affine_to_rasmm=IDENTITY), mask, IDENTITY)
    assert len(empty) == 0
    assert_array_equal(empty.affine_to_rasmm, IDENTITY)
