# emacs: -*- mode: python-mode; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the NiBabel package for the
#   copyright and license terms.
#
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Tests for image statistics"""

import json

import numpy as np
import pytest

from .. import Nifti1Image, imagestats


class MinimalImage:
    """Small spatial-image stand-in that permits degenerate affines."""

    def __init__(self, data, affine):
        self.dataobj = np.asarray(data)
        self.affine = np.asarray(affine)
        self.shape = self.dataobj.shape
        self.header = type('Header', (), {'get_zooms': lambda self: (1.0, 1.0, 1.0)})()


def test_mask_volume():
    # Test mask volume computation

    mask_data = np.zeros((20, 20, 20), dtype='u1')
    mask_data[5:15, 5:15, 5:15] = 1
    img = Nifti1Image(mask_data, np.eye(4))

    vol_mm3 = imagestats.mask_volume(img)
    vol_vox = imagestats.count_nonzero_voxels(img)

    assert vol_mm3 == 1000.0
    assert vol_vox == 1000


def test_mask_volume_uses_affine_space():
    mask_data = np.zeros((3, 3, 3), dtype=np.uint8)
    mask_data[0] = 1
    affine = np.array(
        [[0.0, -2.0, 0.0, 5.0], [3.0, 0.0, 0.5, -1.0], [0.0, 0.0, 4.0, 2.0], [0, 0, 0, 1]]
    )
    img = Nifti1Image(mask_data, affine)
    expected = np.count_nonzero(mask_data) * abs(np.linalg.det(affine[:3, :3]))
    assert imagestats.mask_volume(img) == pytest.approx(expected)
    assert imagestats.mask_volume(img) != pytest.approx(
        np.count_nonzero(mask_data) * np.prod(img.header.get_zooms()[:3])
    )


def test_voxel_volume_affine_types():
    assert imagestats.voxel_volume(np.eye(4)) == 1.0
    assert imagestats.voxel_volume(np.diag([2.0, 3.0, 4.0, 1.0])) == pytest.approx(24.0)
    # A flip changes the sign of the determinant but not the volume.
    flip = np.diag([-2.0, 3.0, 4.0, 1.0])
    assert imagestats.voxel_volume(flip) == pytest.approx(24.0)
    # Shear is handled as actual space rather than as zoom products.
    shear = np.array(
        [[2.0, 1.0, 0.0, 0.0], [0.0, 3.0, 0.5, 0.0], [0.0, 0.0, 4.0, 0.0], [0, 0, 0, 1]]
    )
    assert imagestats.voxel_volume(shear) == pytest.approx(24.0)
    assert imagestats.voxel_volume(shear) != np.prod(np.sqrt((shear[:3, :3] ** 2).sum(0)))


@pytest.mark.parametrize(
    'affine',
    (
        np.diag([1.0, 1.0, 0.0, 1.0]),
        np.array([[1.0, 2.0, 0.0, 0.0], [2.0, 4.0, 0.0, 0.0], [0, 0, 1, 0], [0, 0, 0, 1]]),
    ),
)
def test_voxel_volume_degenerate(affine):
    with pytest.raises(imagestats.DegenerateTransformError):
        imagestats.voxel_volume(affine)


def _segmentation():
    data = np.zeros((5, 5, 5), dtype=np.int16)
    data[0:2, 0:2, 0:2] = 1
    data[2:4, 0:2, 0:2] = 2
    data[4, 0, 0] = 9
    return data


def test_label_statistics_labels_regions_and_specials():
    img = Nifti1Image(_segmentation(), np.diag([2.0, 3.0, 4.0, 1.0]))
    stats = imagestats.label_statistics(img, {1: ('left', 'hemi'), 2: ('right', 'hemi')})

    assert stats.voxel_volume_mm3 == pytest.approx(24.0)
    assert stats.labels[1].voxel_count == 8
    assert stats.labels[1].volume_mm3 == pytest.approx(192.0)
    assert stats.regions['hemi'].voxel_count == 16
    assert stats.regions['hemi'].volume_mm3 == pytest.approx(384.0)
    assert stats.regions['hemi'].label_counts == {1: 8, 2: 8}
    assert stats.label_to_region == {1: 'hemi', 2: 'hemi'}

    # Unknown labels are kept separate from background and from the region.
    assert set(stats.unknown) == {9}
    assert stats.unknown[9].voxel_count == 1
    assert stats.unknown[9].volume_mm3 == pytest.approx(24.0)
    assert stats.background.voxel_count == 5**3 - 17
    assert stats.total_voxels == 5**3


def test_label_statistics_merges_source_labels_but_keeps_per_label_counts():
    img = Nifti1Image(_segmentation(), np.eye(4))
    stats = imagestats.label_statistics(
        img,
        [
            {'label': 1, 'name': 'cortex', 'region': 'brain'},
            {'label': 2, 'name': 'white matter', 'region': 'brain'},
        ],
    )
    brain = stats.regions['brain']
    assert brain.label_counts == {1: 8, 2: 8}
    assert brain.label_volumes == {1: 8.0, 2: 8.0}
    assert stats.labels[1].name == 'cortex'
    assert stats.labels[2].region == 'brain'


def test_label_statistics_csv_and_json_tables(tmp_path):
    img = Nifti1Image(_segmentation(), np.eye(4))
    csv_path = tmp_path / 'labels.csv'
    csv_path.write_text('label,name,region\n1,left,hemi\n2,right,hemi\n', encoding='utf-8')
    csv_stats = imagestats.label_statistics(img, csv_path)
    assert csv_stats.regions['hemi'].voxel_count == 16

    json_path = tmp_path / 'labels.json'
    json_path.write_text(
        json.dumps({'labels': [{'label': 1, 'name': 'x', 'region': 'whole'}]}),
        encoding='utf-8',
    )
    json_stats = imagestats.label_statistics(img, json_path)
    assert json_stats.regions['whole'].voxel_count == 8
    assert set(json_stats.unknown) == {2, 9}


def test_label_table_inline_and_region_map():
    data = np.zeros((3, 3, 3), dtype=np.int16)
    data[0] = 1
    data[1] = 2
    data[2] = 3
    img = Nifti1Image(data, np.eye(4))

    inline = imagestats.label_statistics(img, 'label,name,region\n1,a,X\n2,b,X\n3,c,Y\n')
    assert {region: stat.voxel_count for region, stat in inline.regions.items()} == {
        'X': 18,
        'Y': 9,
    }

    region_map = imagestats.label_statistics(img, {'X': [1, 2], 'Y': [3]})
    assert region_map.regions['X'].label_counts == {1: 9, 2: 9}


def test_label_table_validation():
    img = Nifti1Image(_segmentation(), np.eye(4))
    with pytest.raises(imagestats.LabelTableError):
        imagestats.label_statistics(img, [('not-a-label', 'name', 'region')])
    with pytest.raises(imagestats.LabelTableError):
        imagestats.label_statistics(img, [(1, 'left', 'L'), (1, 'right', 'R')])
    with pytest.raises(imagestats.LabelTableError):
        imagestats.label_statistics(img, [{'name': 'missing-label'}])
    with pytest.raises(imagestats.LabelTableError):
        imagestats.label_statistics(img, [])
    with pytest.raises(imagestats.LabelTableError):
        imagestats.label_statistics(img, {0: ('bg', 'bg')})
    with pytest.raises(imagestats.LabelTableError):
        imagestats.label_statistics(img, {1: ('left', 'background')})
    with pytest.raises(imagestats.LabelTableError):
        imagestats.label_statistics(img, {1: ('left', 'unknown:cortex')})

    # Label 0 in a table is ignored rather than merged with background.
    stats = imagestats.label_statistics(img, {0: ('bg', 'bg'), 1: ('left', 'hemi')})
    assert stats.background.voxel_count == 5**3 - 17
    assert stats.regions == {'hemi': stats.regions['hemi']}


def test_non_integer_label_image_rejected():
    img = Nifti1Image(np.full((2, 2, 2), 1.5), np.eye(4))
    with pytest.raises(ValueError, match='integer-valued'):
        imagestats.label_statistics(img, {1: ('one', 'one')})


def test_label_statistics_degenerate_affine_rejected_by_default():
    img = MinimalImage(_segmentation(), np.diag([1.0, 1.0, 0.0, 1.0]))
    with pytest.raises(imagestats.DegenerateTransformError):
        imagestats.label_statistics(img, {1: ('left', 'hemi'), 2: ('right', 'hemi')})


def test_label_statistics_degenerate_affine_opt_in_counts_only():
    img = MinimalImage(_segmentation(), np.diag([1.0, 1.0, 0.0, 1.0]))
    stats = imagestats.label_statistics(
        img,
        {1: ('left', 'hemi'), 2: ('right', 'hemi')},
        allow_undefined_volume=True,
    )
    assert stats.voxel_volume_mm3 is None
    assert stats.labels[1].voxel_count == 8
    assert stats.labels[1].volume_mm3 is None
    assert stats.regions['hemi'].volume_mm3 is None
    assert stats.background.volume_mm3 is None
    assert stats.total_volume_mm3 is None
    assert stats.volume_defined is False


def test_compare_segmentations_renumbered_is_same_region():
    source = _segmentation()
    target = np.zeros_like(source)
    target[0:2, 0:2, 0:2] = 10
    target[2:4, 0:2, 0:2] = 20
    target[4, 0, 0] = 9

    source_img = Nifti1Image(source, np.eye(4))
    target_img = Nifti1Image(target, np.eye(4))
    comparison = imagestats.compare_segmentations(
        source_img,
        target_img,
        {1: ('left', 'hemi'), 2: ('right', 'hemi')},
        {10: ('left_new', 'hemi'), 20: ('right_new', 'hemi')},
    )

    assert comparison.same_regions == {'hemi': 'hemi'}
    assert comparison.renumbered == {1: 10, 2: 20}
    assert comparison.changed_voxels == 0
    assert comparison.source_only == ()
    assert comparison.target_only == ()
    assert all(match.identical for match in comparison.matches)


def test_compare_segmentations_lists_transfers():
    source = _segmentation()
    target = source.copy()
    target[0:1, 0:2, 0:2] = 2

    comparison = imagestats.compare_segmentations(
        Nifti1Image(source, np.eye(4)),
        Nifti1Image(target, np.eye(4)),
        {1: ('left', 'L'), 2: ('right', 'R')},
    )

    assert comparison.changed_voxels == 4
    assert comparison.same_regions == {}
    transfers = {(t.source_region, t.target_region): t.voxel_count for t in comparison.transfers}
    assert transfers[('L', 'R')] == 4
    assert comparison.transfers[0].volume_mm3 == 4.0
    assert ('L', 'R') in comparison.contingency
    assert comparison.to_dict()['changed_voxels'] == 4


def test_compare_segmentations_keeps_unknown_and_background_separate():
    source = _segmentation()
    target = source.copy()
    target[4, 0, 0] = 0

    comparison = imagestats.compare_segmentations(
        Nifti1Image(source, np.eye(4)),
        Nifti1Image(target, np.eye(4)),
        {1: ('left', 'hemi'), 2: ('right', 'hemi')},
    )

    transfers = {(t.source_region, t.target_region): t.voxel_count for t in comparison.transfers}
    assert transfers[('unknown:9', 'background')] == 1
    assert comparison.same_regions == {'hemi': 'hemi'}
    assert comparison.changed_voxels == 1


def test_compare_segmentations_no_interpolation_creates_categories():
    source = _segmentation()
    target = np.zeros_like(source)
    target[0:2, 0:2, 0:2] = 1
    target[2:4, 0:2, 0:2] = 2
    target[4, 0, 0] = 9

    # Shift the source by one voxel in world space.  Nearest-neighbour
    # resampling may move boundaries but must not create new label values.
    affine = np.array([[1.0, 0, 0, -1], [0, 1.0, 0, 0], [0, 0, 1.0, 0], [0, 0, 0, 1]])
    comparison = imagestats.compare_segmentations(
        Nifti1Image(source, affine),
        Nifti1Image(target, np.eye(4)),
        {1: ('left', 'hemi'), 2: ('right', 'hemi')},
    )
    target_regions = {pair[1] for pair in comparison.contingency}
    assert target_regions <= {'background', 'hemi', 'unknown:9', 'outside'}


def test_compare_segmentations_rejects_degenerate_affine():
    source = MinimalImage(_segmentation(), np.diag([1.0, 1.0, 0.0, 1.0]))
    target = Nifti1Image(_segmentation(), np.eye(4))
    with pytest.raises(imagestats.DegenerateTransformError):
        imagestats.compare_segmentations(source, target, {1: ('left', 'hemi')})
