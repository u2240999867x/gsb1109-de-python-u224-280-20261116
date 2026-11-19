"""
Tests for dcmstack.select and DcmMetaExtension.take
"""
from copy import deepcopy

import pytest
import numpy as np
import nibabel as nb

from dcmstack import dcmmeta
from dcmstack import select


def _make_ext(shape=(4, 5, 3, 5, 7), slice_dim=2, with_time_dim=True):
    '''Make an extension with a traceable element in every classification:
    the values are the (flat) source indices of each position.'''
    ext = dcmmeta.DcmMetaExtension.make_empty(shape,
                                              np.eye(4),
                                              np.eye(4),
                                              slice_dim)
    for classes in ext.get_valid_classes():
        key = '%s_%s_test' % classes
        mult = ext.get_multiplicity(classes)
        if mult == 1:
            ext.get_class_dict(classes)[key] = 0
        else:
            ext.get_class_dict(classes)[key] = list(range(mult))
    if with_time_dim and len(shape) > 3 and shape[3] > 1:
        n_time = shape[3]
        n_vec = shape[4] if len(shape) > 4 else 1
        ext.get_class_dict(('time', 'samples'))['EchoTime'] = \
            [t * 10 for v in range(n_vec) for t in range(n_time)]
        ext.time_dim = 'EchoTime'
    return ext


class TestTake(object):
    def setUp(self):
        self.ext = _make_ext()

    def setup_method(self, setUp):
        self.setUp()

    def test_reorder_and_duplicate(self):
        sel = {'slice': [2, 0, 0], 'time': [3, 0], 'vector': [5, 5, 1]}
        sub, report = self.ext.take(sel)
        sub.check_valid()
        assert sub.shape == (4, 5, 3, 2, 3)

        #Every output position holds the value of its source position,
        #duplicates get independent output positions
        exp_glb_slc = []
        for v in (5, 5, 1):
            for t in (3, 0):
                for s in (2, 0, 0):
                    exp_glb_slc.append((v * 5 + t) * 3 + s)
        assert sub.get_values_and_class('global_slices_test') == \
            (exp_glb_slc, ('global', 'slices'))

        exp_time_smp = [v * 5 + t for v in (5, 5, 1) for t in (3, 0)]
        assert sub.get_values_and_class('time_samples_test') == \
            (exp_time_smp, ('time', 'samples'))

        assert sub.get_values_and_class('vector_samples_test') == \
            ([5, 5, 1], ('vector', 'samples'))

        assert sub.get_values_and_class('time_slices_test') == \
            ([2, 0, 0], ('time', 'slices'))

        exp_vec_slc = [t * 3 + s for t in (3, 0) for s in (2, 0, 0)]
        assert sub.get_values_and_class('vector_slices_test') == \
            (exp_vec_slc, ('vector', 'slices'))

        assert sub.get_values_and_class('global_const_test') == \
            (0, ('global', 'const'))

        #Nothing was reclassified, all classifications still apply
        assert report['reclassified'] == {}

    def test_no_leak_from_unselected(self):
        sel = {'time': [1, 1, 3]}
        sub, report = self.ext.take(sel)
        sub.check_valid()
        vals, classes = sub.get_values_and_class('EchoTime')
        assert vals == [10, 10, 30] * 7
        #Values from unselected time points must not appear
        assert not any(v in vals for v in (0, 20, 40))

    def test_source_not_modified(self):
        before = self.ext.to_json()
        self.ext.take({'slice': [2, 0, 0], 'time': [3, 0], 'vector': [5, 5, 1]})
        assert self.ext.to_json() == before

    def test_time_dim_dropped_when_unordered(self):
        sub, report = self.ext.take({'time': [3, 0, 2]})
        assert sub.time_dim is None
        assert 'time_dim' in report['dropped_dim_order']
        #The values themselves are preserved and queryable
        assert sub.get_values('EchoTime') == [30, 0, 20] * 7

    def test_time_dim_kept_when_reversed(self):
        #A strictly reversed selection is still ordered by the attribute
        sub, report = self.ext.take({'time': [3, 0]})
        assert sub.time_dim == 'EchoTime'
        assert report['dropped_dim_order'] == {}
        assert sub.get_values('EchoTime') == [30, 0] * 7

    def test_time_dim_dropped_when_duplicated(self):
        sub, report = self.ext.take({'time': [1, 1]})
        assert sub.time_dim is None
        assert 'time_dim' in report['dropped_dim_order']

    def test_time_dim_kept_when_ordered(self):
        sub, report = self.ext.take({'time': [0, 2, 4]})
        assert sub.time_dim == 'EchoTime'
        assert report['dropped_dim_order'] == {}
        assert sub.get_values('EchoTime') == [0, 20, 40] * 7

    def test_time_collapse(self):
        sub, report = self.ext.take({'time': [2]})
        sub.check_valid()
        assert sub.shape == (4, 5, 3, 1, 7)
        #Time samples become vector samples at the selected time point
        assert sub.get_values_and_class('time_samples_test') == \
            ([v * 5 + 2 for v in range(7)], ('vector', 'samples'))
        #Time slices become vector slices
        assert sub.get_values_and_class('time_slices_test') == \
            ([0, 1, 2], ('vector', 'slices'))
        #The ordering attribute is gone with the dimension
        assert sub.time_dim is None
        assert report['reclassified']['time_samples_test'] == \
            (('time', 'samples'), ('vector', 'samples'))
        assert report['reclassified']['time_slices_test'] == \
            (('time', 'slices'), ('vector', 'slices'))

    def test_vector_collapse(self):
        sub, report = self.ext.take({'vector': [3]})
        sub.check_valid()
        assert sub.shape == (4, 5, 3, 5)
        assert sub.get_values_and_class('vector_samples_test') == \
            (3, ('global', 'const'))
        assert sub.get_values_and_class('vector_slices_test') == \
            (list(range(15)), ('global', 'slices'))
        assert sub.get_values_and_class('time_samples_test') == \
            ([3 * 5 + t for t in range(5)], ('time', 'samples'))

    def test_full_collapse(self):
        sub, report = self.ext.take({'time': [1], 'vector': [2]})
        sub.check_valid()
        assert sub.shape == (4, 5, 3)
        assert sub.get_values_and_class('time_samples_test') == \
            (2 * 5 + 1, ('global', 'const'))
        assert sub.get_values_and_class('vector_samples_test') == \
            (2, ('global', 'const'))
        assert sub.get_values_and_class('time_slices_test') == \
            ([0, 1, 2], ('global', 'slices'))

    def test_invalid_selections(self):
        with pytest.raises(ValueError):
            self.ext.take({'time': []})
        with pytest.raises(ValueError):
            self.ext.take({'time': [5]})
        with pytest.raises(ValueError):
            self.ext.take({'slice': [3]})
        with pytest.raises(ValueError):
            self.ext.take({0: [0]})
        with pytest.raises(ValueError):
            self.ext.take({'vector': [0], 4: [1]})


def _make_nii():
    data = np.arange(2 * 3 * 4 * 5, dtype=np.float32).reshape(2, 3, 4, 5)
    affine = np.diag([2.0, 3.0, 4.0, 1.0])
    affine[:3, 3] = [10.0, 20.0, 30.0]
    ext = dcmmeta.DcmMetaExtension.make_empty((2, 3, 4, 5),
                                              affine,
                                              np.eye(4),
                                              2)
    ext.get_class_dict(('global', 'const'))['ConstKey'] = 1
    ext.get_class_dict(('global', 'slices'))['PerSlice'] = list(range(20))
    ext.get_class_dict(('time', 'samples'))['EchoTime'] = \
        [t * 10 for t in range(5)]
    ext.get_class_dict(('time', 'slices'))['SliceTime'] = \
        [s * 0.1 for s in range(4)]
    ext.time_dim = 'EchoTime'
    ext.check_valid()
    img = nb.Nifti1Image(data, affine)
    img.header.extensions = nb.nifti1.Nifti1Extensions([ext])
    return img


class TestSelectVoxels(object):
    def setUp(self):
        self.img = _make_nii()

    def setup_method(self, setUp):
        self.setUp()

    def test_basic_selection(self):
        sel = {'slice': [0, 2], 'time': [4, 1, 1]}
        result = select.select_voxels(self.img, sel)
        src_data = np.asanyarray(self.img.dataobj)
        exp_data = np.take(np.take(src_data, [0, 2], axis=2),
                           [4, 1, 1], axis=3)
        assert np.array_equal(result.data, exp_data)
        #Uniform slice step of two is affine representable
        assert result.affine is not None
        assert np.allclose(result.affine[:3, 2], [0, 0, 8])
        assert np.allclose(result.affine[:3, 3], [10, 20, 30])
        assert result.report['affine_representable'] == True
        #Spatial correspondence is explicit
        assert result.spatial_map['slice']['source_indices'] == [0, 2]
        assert np.allclose(result.spatial_map['slice']['positions'],
                           [[10, 20, 30], [10, 20, 38]])
        assert result.spatial_map['time']['source_indices'] == [4, 1, 1]
        #Meta data follows the selection
        assert result.meta.shape == (2, 3, 2, 3)
        assert result.meta.get_values('EchoTime') == [40, 10, 10]
        assert result.meta.get_values('SliceTime') == [0.0, 0.2]
        assert result.meta.get_values('PerSlice') == \
            [t * 4 + s for t in (4, 1, 1) for s in (0, 2)]
        #The time ordering attribute no longer orders the dimension
        assert result.meta.time_dim is None
        assert 'time_dim' in result.report['dropped_dim_order']
        #The result can be written out as a Nifti image
        out_img = result.to_nifti()
        assert np.array_equal(np.asanyarray(out_img.dataobj), exp_data)
        assert select.find_dcmmeta(out_img) is result.meta

    def test_source_not_modified(self):
        src_ext = select.find_dcmmeta(self.img)
        ext_before = src_ext.to_json()
        data_before = np.asanyarray(self.img.dataobj).copy()
        select.select_voxels(self.img, {'slice': [2, 0, 0], 'time': [3, 0]})
        assert src_ext.to_json() == ext_before
        assert np.array_equal(np.asanyarray(self.img.dataobj), data_before)

    def test_nonuniform_slices_bypass(self):
        result = select.select_voxels(self.img, {'slice': [0, 1, 3]})
        assert result.affine is None
        assert result.report['affine_representable'] == False
        #The explicit mapping carries the geometry instead
        assert np.allclose(result.spatial_map['slice']['positions'],
                           [[10, 20, 30], [10, 20, 34], [10, 20, 42]])
        #Meta data is still subset and queryable
        assert result.meta is not None
        assert result.meta.get_values('SliceTime') == \
            pytest.approx([0.0, 0.1, 0.3])
        #But it can not be packed into a Nifti image
        with pytest.raises(select.SelectionError):
            result.to_nifti()

    def test_nonuniform_slices_strict(self):
        with pytest.raises(select.SelectionError):
            select.select_voxels(self.img, {'slice': [0, 1, 3]},
                                 policy='strict')

    def test_duplicate_slices_bypass(self):
        result = select.select_voxels(self.img, {'slice': [1, 1]})
        assert result.affine is None
        assert result.spatial_map['slice']['source_indices'] == [1, 1]
        #Repeated samples keep independent output positions
        src_data = np.asanyarray(self.img.dataobj)
        assert np.array_equal(result.data[:, :, 0, :],
                              src_data[:, :, 1, :])
        assert np.array_equal(result.data[:, :, 1, :],
                              src_data[:, :, 1, :])
        assert result.meta.get_values('SliceTime') == [0.1, 0.1]
        with pytest.raises(select.SelectionError):
            select.select_voxels(self.img, {'slice': [1, 1]},
                                 policy='strict')

    def test_reversed_slices(self):
        result = select.select_voxels(self.img, {'slice': [3, 1]})
        assert result.affine is not None
        assert np.allclose(result.affine[:3, 2], [0, 0, -8])
        assert np.allclose(result.affine[:3, 3], [10, 20, 42])

    def test_invalid_policy(self):
        with pytest.raises(ValueError):
            select.select_voxels(self.img, {'slice': [0]}, policy='bogus')
