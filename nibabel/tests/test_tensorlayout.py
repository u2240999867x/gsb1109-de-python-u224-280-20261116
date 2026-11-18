# emacs: -*- mode: python-mode; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the NiBabel package for the
#   copyright and license terms.
#
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Tests for nibabel.tensorlayout"""

import numpy as np
import pytest
from numpy.testing import assert_array_equal

from ..nifti1 import Nifti1Header, Nifti1Image
from ..tensorlayout import (
    ComponentMismatch,
    LayoutConflictError,
    LayoutError,
    SourceLayout,
    TargetLayout,
    TensorKind,
    build_mapping,
    check_against_source,
    iter_source_components,
    materialize_target,
)

SPATIAL3 = ('spatial', 'spatial', 'spatial')


def test_roles_must_cover_every_axis():
    with pytest.raises(LayoutError, match='one axis role per axis'):
        SourceLayout((2, 3, 4), ('spatial', 'spatial'))
    with pytest.raises(LayoutError, match='Unknown axis role'):
        SourceLayout((2, 3, 4), ('spatial', 'spatial', 'voxel'))


def test_need_one_spatial_axis():
    with pytest.raises(LayoutError, match='at least one spatial'):
        SourceLayout((2, 3), ('temporal', 'component'))


def test_at_most_one_temporal_axis():
    with pytest.raises(LayoutError, match='at most one temporal'):
        SourceLayout((2, 3, 4), ('spatial', 'temporal', 'temporal'))


def test_vector_intent_requires_component_axis():
    with pytest.raises(LayoutError, match='no component axis'):
        SourceLayout((2, 3, 4), SPATIAL3, intent='vector')


def test_general_matrix_validates_params_and_count():
    layout = SourceLayout(
        (5, 6), ('spatial', 'component'), intent='general matrix', intent_params=(2, 3)
    )
    assert layout.kind is TensorKind.MATRIX
    assert layout.matrix_dims == (2, 3)
    with pytest.raises(LayoutError, match='requires intent_params'):
        SourceLayout((5, 6), ('spatial', 'component'), intent='general matrix', intent_params=(2,))
    with pytest.raises(LayoutError, match='needs 6 components'):
        SourceLayout(
            (5, 5), ('spatial', 'component'), intent='general matrix', intent_params=(2, 3)
        )


def test_symmetric_matrix_validates_packed_count():
    layout = SourceLayout(
        (4, 6), ('spatial', 'component'), intent='symmetric matrix', intent_params=(3,)
    )
    assert layout.kind is TensorKind.SYMMETRIC_MATRIX
    assert layout.symmetric_dim == 3
    # 4 x 4 symmetric matrix packs to 10 components, not 6
    with pytest.raises(LayoutError, match='packs to 10'):
        SourceLayout(
            (4, 6), ('spatial', 'component'), intent='symmetric matrix', intent_params=(4,)
        )
    with pytest.raises(LayoutError, match='requires intent_params'):
        SourceLayout((4, 6), ('spatial', 'component'), intent='symmetric matrix')


def test_time_series_intent_requires_temporal_axis():
    layout = SourceLayout((2, 3, 4, 5), SPATIAL3 + ('temporal',), intent='time series')
    assert layout.kind is TensorKind.SCALAR
    with pytest.raises(LayoutError, match='temporal axis'):
        SourceLayout((2, 3, 4), SPATIAL3, intent='time series')


def test_unknown_intent_keeps_declaration_only():
    # 6 components could be a packed 3x3 symmetric matrix, but an unknown
    # intent must not be guessed from axis lengths.
    layout = SourceLayout((4, 6), ('spatial', 'component'), intent=9999)
    assert layout.kind is TensorKind.UNKNOWN
    assert layout.declared_intent == 9999
    assert layout.intent_label is None
    mapping = build_mapping(layout, TargetLayout(n_spatial=1, has_temporal=False))
    assert not mapping.ok
    assert [c.kind for c in mapping.conflicts] == ['unknown-intent']
    with pytest.raises(LayoutConflictError) as excinfo:
        materialize_target(np.zeros((4, 6)), mapping)
    assert 'unknown-intent' in str(excinfo.value)


def test_singleton_component_axis_does_not_disappear():
    layout = SourceLayout((2, 3, 4, 1), SPATIAL3 + ('component',), intent='none')
    target = TargetLayout(has_temporal=False)
    mapping = build_mapping(layout, target)
    assert mapping.ok
    assert mapping.target_shape == (2, 3, 4, 1)
    # Without a declared component axis there is nothing to preserve.
    plain = SourceLayout((2, 3, 4), SPATIAL3, intent='none')
    assert build_mapping(plain, target).target_shape == (2, 3, 4)


def test_conflicts_are_listed():
    source = SourceLayout((2, 3, 4, 5), SPATIAL3 + ('temporal',), intent='time series')
    target = TargetLayout(n_spatial=2, has_temporal=False)
    mapping = build_mapping(source, target)
    assert not mapping.ok
    assert {c.kind for c in mapping.conflicts} == {'spatial-mismatch', 'temporal-mismatch'}
    with pytest.raises(LayoutConflictError) as excinfo:
        materialize_target(np.zeros(source.shape), mapping)
    assert len(excinfo.value.conflicts) == 2


def test_vector_mapping_components_last_and_first():
    rng = np.random.default_rng(42)
    data = rng.normal(size=(2, 3, 4, 3))
    layout = SourceLayout(data.shape, SPATIAL3 + ('component',), intent='vector')
    target = TargetLayout(has_temporal=False, vector_components='last')
    mapping = build_mapping(layout, target)
    assert mapping.target_shape == data.shape
    assert_array_equal(materialize_target(data, mapping), data)
    first = TargetLayout(has_temporal=False, vector_components='first')
    mapping_first = build_mapping(layout, first)
    assert mapping_first.target_shape == (3, 2, 3, 4)
    out = materialize_target(data, mapping_first)
    for c in range(3):
        assert_array_equal(out[c], data[..., c])


def test_links_cover_every_target_element():
    layout = SourceLayout((2, 3, 4, 3), SPATIAL3 + ('component',), intent='vector')
    mapping = build_mapping(layout, TargetLayout(has_temporal=False))
    links = list(mapping.iter_links())
    assert len(links) == np.prod(mapping.target_shape)
    assert {link.target_index for link in links} == set(np.ndindex(*mapping.target_shape))
    for link in links:
        assert mapping.source_index(link.source_voxel, link.source_component) == (
            link.source_voxel + (link.source_component,)
        )


def test_matrix_storage_orders():
    rng = np.random.default_rng(7)
    data = rng.normal(size=(5, 6))  # 5 voxels, 2x3 matrix per voxel
    layout = SourceLayout(
        data.shape, ('spatial', 'component'), intent='general matrix', intent_params=(2, 3)
    )
    target_kw = dict(n_spatial=1, has_temporal=False)
    # Flat row-major storage is the identity for a row-major source.
    row_major = build_mapping(layout, TargetLayout(matrix_storage='row-major', **target_kw))
    assert row_major.target_shape == (5, 6)
    assert_array_equal(materialize_target(data, row_major), data)
    # Flat column-major storage transposes the packing of each 2x3 matrix.
    col_major = build_mapping(layout, TargetLayout(matrix_storage='column-major', **target_kw))
    out = materialize_target(data, col_major)
    for voxel in range(5):
        matrix = data[voxel].reshape(2, 3)
        assert_array_equal(out[voxel], matrix.T.ravel())
    # Two explicit matrix axes.
    axes = build_mapping(layout, TargetLayout(matrix_storage='axes', **target_kw))
    assert axes.target_shape == (5, 2, 3)
    out = materialize_target(data, axes)
    for voxel in range(5):
        assert_array_equal(out[voxel], data[voxel].reshape(2, 3))


def _symmetric_source():
    # 2 voxels, 3x3 symmetric matrices packed row-wise over the upper triangle:
    # components are (0,0), (0,1), (0,2), (1,1), (1,2), (2,2)
    data = np.array(
        [
            [1.0, 2.0, 3.0, 4.0, 5.0, 6.0],
            [10.0, 20.0, 30.0, 40.0, 50.0, 60.0],
        ]
    )
    layout = SourceLayout(
        data.shape,
        ('spatial', 'component'),
        intent='symmetric matrix',
        intent_params=(3,),
        symmetric_packing='upper',
    )
    return data, layout


def test_symmetric_packing_is_not_an_axis_flip():
    data, layout = _symmetric_source()
    target_kw = dict(n_spatial=1, has_temporal=False)
    # Repacking upper -> lower reorders components: (0,0), (1,0), (1,1),
    # (2,0), (2,1), (2,2) in terms of matrix elements.
    lower = build_mapping(layout, TargetLayout(symmetric_storage='packed-lower', **target_kw))
    assert_array_equal(lower.component_map, [0, 1, 3, 2, 4, 5])
    out = materialize_target(data, lower)
    assert_array_equal(out[0], [1.0, 2.0, 4.0, 3.0, 5.0, 6.0])
    # Full storage expands to the symmetric 3x3 matrix.
    full = build_mapping(layout, TargetLayout(symmetric_storage='full', **target_kw))
    assert full.target_shape == (2, 3, 3)
    out = materialize_target(data, full)
    expected = np.array([[1.0, 2.0, 3.0], [2.0, 4.0, 5.0], [3.0, 5.0, 6.0]])
    assert_array_equal(out[0], expected)
    assert_array_equal(out[1], expected * 10)


def test_symmetric_lower_source_round_trip():
    rng = np.random.default_rng(11)
    data = rng.normal(size=(3, 10))
    layout = SourceLayout(
        data.shape,
        ('spatial', 'component'),
        intent='symmetric matrix',
        intent_params=(4,),
        symmetric_packing='lower',
    )
    target_kw = dict(n_spatial=1, has_temporal=False)
    for storage in ('packed-lower', 'packed-upper', 'full'):
        mapping = build_mapping(layout, TargetLayout(symmetric_storage=storage, **target_kw))
        out = materialize_target(data, mapping)
        assert check_against_source(out, data, mapping) == []


def _proxy_image(data, intent='vector', params=()):
    header = Nifti1Header()
    header.set_intent(intent, params)
    header.set_data_dtype(np.float32)
    img = Nifti1Image(data.astype(np.float32), np.eye(4), header=header)
    return Nifti1Image.from_bytes(img.to_bytes())


def test_chunked_proxy_read_matches_full_read():
    rng = np.random.default_rng(23)
    data = rng.normal(size=(3, 4, 5, 3)).astype(np.float32)
    layout = SourceLayout(data.shape, SPATIAL3 + ('component',), intent='vector')
    mapping = build_mapping(layout, TargetLayout(has_temporal=False))
    img = _proxy_image(data)
    assert img.dataobj.is_proxy
    whole = materialize_target(img.dataobj, mapping)
    for chunk_shape in [(1, 1, 1), (2, 3, 2), (3, 4, 5), (5, 5, 5)]:
        chunked = materialize_target(img.dataobj, mapping, chunk_shape=chunk_shape)
        assert_array_equal(chunked, whole)
    assert_array_equal(whole, data)
    # Chunked and whole reads give the same correspondence as the mapping.
    links = list(mapping.iter_links())
    for link in links[::7]:
        assert whole[link.target_index] == data[mapping.source_index(*link[1:])]


def test_chunk_shape_validation():
    layout = SourceLayout((2, 3, 4), SPATIAL3, intent='none')
    mapping = build_mapping(layout, TargetLayout(has_temporal=False))
    with pytest.raises(LayoutError, match='one per voxel axis'):
        materialize_target(np.zeros((2, 3, 4)), mapping, chunk_shape=(2, 2))
    with pytest.raises(LayoutError, match='positive'):
        materialize_target(np.zeros((2, 3, 4)), mapping, chunk_shape=(2, 0, 2))


def test_temporal_padding_and_preservation():
    rng = np.random.default_rng(5)
    data = rng.normal(size=(2, 3, 4))
    layout = SourceLayout(data.shape, SPATIAL3, intent='none')
    # Warehouse layout has a temporal axis; the source gets a length-1 one.
    mapping = build_mapping(layout, TargetLayout(has_temporal=True))
    assert mapping.target_shape == (2, 3, 4, 1)
    out = materialize_target(data, mapping)
    assert_array_equal(out[..., 0], data)
    # A source temporal axis is preserved, never dropped.
    series = SourceLayout((2, 3, 4, 5), SPATIAL3 + ('temporal',), intent='time series')
    mapping = build_mapping(series, TargetLayout(has_temporal=True))
    assert mapping.target_shape == (2, 3, 4, 5)


def test_inverse_check_finds_tampered_components():
    rng = np.random.default_rng(13)
    data = rng.normal(size=(2, 3, 4, 3))
    layout = SourceLayout(data.shape, SPATIAL3 + ('component',), intent='vector')
    mapping = build_mapping(layout, TargetLayout(has_temporal=False))
    out = materialize_target(data, mapping)
    assert check_against_source(out, data, mapping) == []
    # Every source component can be recovered and checked one by one.
    recovered = list(iter_source_components(out, mapping))
    assert len(recovered) == 2 * 3 * 4 * 3
    for voxel, component, value in recovered:
        assert value == data[voxel + (component,)]
    # Tampering with one element is reported against its source component.
    out[1, 2, 3, 2] += 1
    mismatches = check_against_source(out, data, mapping)
    assert len(mismatches) == 1
    mismatch = mismatches[0]
    assert isinstance(mismatch, ComponentMismatch)
    assert mismatch.voxel == (1, 2, 3)
    assert mismatch.component == 2
    assert mismatch.source_value == data[1, 2, 3, 2]
    assert mismatch.target_value == data[1, 2, 3, 2] + 1


def test_check_against_source_rejects_wrong_shape():
    layout = SourceLayout((2, 3, 4, 3), SPATIAL3 + ('component',), intent='vector')
    mapping = build_mapping(layout, TargetLayout(has_temporal=False))
    with pytest.raises(LayoutError, match='expected'):
        check_against_source(np.zeros((2, 3, 4)), np.zeros(layout.shape), mapping)
    with pytest.raises(LayoutError, match='expected'):
        list(iter_source_components(np.zeros((2, 3, 4)), mapping))


def test_target_layout_option_validation():
    with pytest.raises(LayoutError, match='n_spatial'):
        TargetLayout(n_spatial=0)
    with pytest.raises(LayoutError, match='vector_components'):
        TargetLayout(vector_components='middle')
    with pytest.raises(LayoutError, match='matrix_storage'):
        TargetLayout(matrix_storage='diagonal')
    with pytest.raises(LayoutError, match='symmetric_storage'):
        TargetLayout(symmetric_storage='triangular')


def test_symmetric_packing_declaration_validation():
    with pytest.raises(LayoutError, match='symmetric_packing'):
        SourceLayout(
            (4, 6),
            ('spatial', 'component'),
            intent='symmetric matrix',
            intent_params=(3,),
            symmetric_packing='diagonal',
        )
