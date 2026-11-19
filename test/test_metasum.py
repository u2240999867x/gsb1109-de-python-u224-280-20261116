"""
Tests for dcmstack.metasum
"""
import pytest

from . import test_dir, src_dir

from dcmstack import dcmmeta
from dcmstack import metasum


def test_valid_classifications():
    assert metasum.valid_classifications((2, 1, 1)) == \
        (('global', 'const'), ('global', 'slices'))
    assert metasum.valid_classifications((2, 3, 1)) == \
        (('global', 'const'), ('global', 'slices'),
         ('time', 'samples'), ('time', 'slices'))
    assert metasum.valid_classifications((2, 1, 3)) == \
        (('global', 'const'), ('global', 'slices'),
         ('vector', 'samples'), ('vector', 'slices'))
    assert metasum.valid_classifications((2, 3, 4)) == metasum.CLASSIFICATIONS


def test_slot_count_matches_dcmmeta():
    import numpy as np
    for shape in ((64, 64, 7), (64, 64, 7, 11), (64, 64, 7, 1, 11),
                  (64, 64, 7, 11, 13)):
        ext = dcmmeta.DcmMetaExtension.make_empty(shape, np.eye(4), None, 2)
        meta_shape = (shape[2],
                      shape[3] if len(shape) > 3 else 1,
                      shape[4] if len(shape) > 4 else 1)
        for classification in ext.valid_classes:
            assert (metasum.slot_count(classification, meta_shape) ==
                    ext.get_multiplicity(classification))


def test_slot_index_layout():
    shape = (2, 3, 4)
    #Global slices: slice index varies fastest, then time, then vector
    assert metasum.slot_index(('global', 'slices'), (1, 2, 3), shape) == \
        1 + 2 * (2 + 3 * 3)
    #Time samples: time index varies fastest
    assert metasum.slot_index(('time', 'samples'), (0, 2, 3), shape) == \
        2 + 3 * 3
    #Vector samples and slices
    assert metasum.slot_index(('vector', 'samples'), (1, 2, 3), shape) == 3
    assert metasum.slot_index(('vector', 'slices'), (1, 2, 3), shape) == \
        1 + 2 * 2
    #Time slices and global const
    assert metasum.slot_index(('time', 'slices'), (1, 2, 3), shape) == 1
    assert metasum.slot_index(('global', 'const'), (1, 2, 3), shape) == 0


def make_4d_summary(fields_by_index):
    '''Two slices by two time points, indices map to meta dicts.'''
    summary = metasum.MetaSummary((2, 2, 1))
    for slc_idx in range(2):
        for time_idx in range(2):
            index = (slc_idx, time_idx, 0)
            meta = fields_by_index.get(index, {})
            summary.add(index, meta, source='inst_%d_%d' % index[:2])
    return summary


def test_const_requires_complete_equal_observations():
    summary = make_4d_summary({(slc, tm, 0): {'ConstField': 1, 'MissingField': 2}
           for slc in range(2) for tm in range(2)})
    #Make one instance miss 'MissingField'
    summary._obs['MissingField'][3] = None
    const_cands = summary.candidates('ConstField')
    #Identical values everywhere: const is a candidate, but the other
    #consistent classifications are kept as candidates too
    assert ('global', 'const') in const_cands
    assert ('time', 'samples') in const_cands
    assert ('time', 'slices') in const_cands
    assert ('global', 'slices') in const_cands
    #A missing observation can not participate in proving constancy
    missing_cands = summary.candidates('MissingField')
    assert ('global', 'const') not in missing_cands
    assert ('time', 'samples') in missing_cands


def test_missing_observations_do_not_contradict_samples():
    summary = metasum.MetaSummary((2, 2, 1))
    summary.add((0, 0, 0), {'Field': 1}, source='a')
    summary.add((1, 0, 0), {}, source='b')
    summary.add((0, 1, 0), {'Field': 2}, source='c')
    summary.add((1, 1, 0), {'Field': 2}, source='d')
    cands = summary.candidates('Field')
    assert ('time', 'samples') in cands
    assert ('global', 'const') not in cands


def test_time_varying_field():
    summary = make_4d_summary({(slc, tm, 0): {'EchoTime': 20 + 10 * tm}
           for slc in range(2) for tm in range(2)})
    cands = summary.candidates('EchoTime')
    assert ('time', 'samples') in cands
    assert ('global', 'slices') in cands
    assert ('global', 'const') not in cands
    assert ('time', 'slices') not in cands
    assert summary.get_values('EchoTime', ('time', 'samples')) == [20, 30]


def test_slice_varying_field():
    summary = make_4d_summary({(slc, tm, 0): {'SliceTime': 0.1 * slc}
           for slc in range(2) for tm in range(2)})
    cands = summary.candidates('SliceTime')
    assert ('time', 'slices') in cands
    assert ('global', 'slices') in cands
    assert ('global', 'const') not in cands
    assert ('time', 'samples') not in cands


def test_vector_varying_field():
    summary = metasum.MetaSummary((1, 2, 2))
    for tm in range(2):
        for vec in range(2):
            summary.add((0, tm, vec), {'BValue': 5 * vec})
    cands = summary.candidates('BValue')
    assert ('vector', 'samples') in cands
    #Constant across time for each vector component
    assert ('time', 'samples') in cands
    assert ('global', 'slices') in cands
    assert ('global', 'const') not in cands
    assert ('vector', 'slices') not in cands
    assert summary.get_values('BValue', ('vector', 'samples')) == [0, 5]


def test_allowed_classifications_restrict_candidates():
    summary = metasum.MetaSummary((2, 2, 1),
                                  allowed=(('global', 'const'),
                                           ('time', 'samples')))
    for slc in range(2):
        for tm in range(2):
            summary.add((slc, tm, 0), {'Field': 1})
    assert summary.candidates('Field') == \
        (('global', 'const'), ('time', 'samples'))
    with pytest.raises(ValueError):
        metasum.MetaSummary((2, 2, 1), allowed=(('bogus', 'nope'),))


def test_confirmed_classification():
    summary = make_4d_summary({(slc, tm, 0): {'Field': 1} for slc in range(2) for tm in range(2)})
    summary.confirm('Field', ('time', 'samples'))
    assert summary.candidates('Field') == (('time', 'samples'),)
    #Confirming a classification that is inconsistent with the observations
    #produces a conflict instead of dropping sources
    summary._obs['Field'][2] = 7
    assert summary.candidates('Field') == tuple()
    conflicts = summary.conflicts('Field')['Field']
    assert len(conflicts) == 1
    assert conflicts[0].classification == ('time', 'samples')
    assert conflicts[0].reason == 'inconsistent'
    #All observations in the conflicting slot are listed
    assert len(conflicts[0].observations) == 2
    with pytest.raises(ValueError):
        summary.confirm('Other', ('bogus', 'nope'))


def test_conflicts_list_all_observations():
    summary = metasum.MetaSummary((1, 1, 1))
    #Two sources collide on the same output index with different values, so
    #no classification can explain the observations
    summary.add((0, 0, 0), {'Field': 1}, source='first')
    summary.add((0, 0, 0), {'Field': 2}, source='second')
    assert summary.candidates('Field') == tuple()
    assert summary.conflicted_fields == ('Field',)
    conflicts = summary.conflicts()['Field']
    #Only the global classifications are valid for this shape
    assert set(c.classification for c in conflicts) == \
        {('global', 'const'), ('global', 'slices')}
    for conflict in conflicts:
        assert conflict.reason == 'inconsistent'
        #No source is dropped to make the field fit
        assert set(src for src, _ in conflict.observations) == \
            {'first', 'second'}
    assert list(summary.schemes()) == [{}]


def test_missing_const_conflict():
    summary = metasum.MetaSummary((2, 1, 1))
    summary.add((0, 0, 0), {'Field': 1}, source='a')
    summary.add((1, 0, 0), {}, source='b')
    conflicts = summary.classification_conflicts('Field',
                                                 ('global', 'const'))
    assert len(conflicts) == 1
    assert conflicts[0].reason == 'missing'
    assert conflicts[0].observations == (('b', None),)


def test_schemes_enumerate_all_joint_explanations():
    summary = make_4d_summary({(slc, tm, 0): {'Const': 1, 'Time': tm}
           for slc in range(2) for tm in range(2)})
    schemes = list(summary.schemes())
    #Const has four candidates, Time has two
    assert len(schemes) == 4 * 2
    assert summary.n_schemes == 8
    for scheme in schemes:
        assert scheme['Time'] in (('time', 'samples'), ('global', 'slices'))
        assert scheme['Const'] in (('global', 'const'), ('global', 'slices'),
                                   ('time', 'samples'), ('time', 'slices'))
    #Every scheme jointly explains the observations
    assert len(set(tuple(sorted(s.items())) for s in schemes)) == 8


def test_lookup_returns_originating_instances():
    summary = make_4d_summary({(slc, tm, 0): {'EchoTime': 20 + 10 * tm,
                          'Const': 5}
           for slc in range(2) for tm in range(2)})
    result = summary.lookup('EchoTime', (0, 1, 0), ('time', 'samples'))
    assert result.value == 30
    #Both slices at this time point observed the value
    assert set(result.sources) == {'inst_0_1', 'inst_1_1'}
    result = summary.lookup('EchoTime', (1, 0, 0), ('global', 'slices'))
    assert result.value == 20
    assert result.sources == ('inst_1_0',)
    result = summary.lookup('Const', (1, 1, 0), ('global', 'const'))
    assert result.value == 5
    assert set(result.sources) == {'inst_0_0', 'inst_0_1',
                                   'inst_1_0', 'inst_1_1'}


def test_sources_at():
    summary = make_4d_summary({(slc, tm, 0): {} for slc in range(2) for tm in range(2)})
    assert summary.sources_at((1, 0, 0)) == ('inst_1_0',)
    assert summary.sources_at((0, 0, 1)) == tuple()


def test_reorder_time_keeps_instance_mapping():
    summary = metasum.MetaSummary((1, 3, 1))
    for tm in range(3):
        summary.add((0, tm, 0), {'EchoTime': 10 * (tm + 1)},
                    source='inst_t%d' % tm)
    reordered = summary.reorder(time_perm=(2, 0, 1))
    #Values arrays follow the new ordering
    assert reordered.get_values('EchoTime', ('time', 'samples')) == \
        [30, 10, 20]
    #Lookups by output index still lead back to the correct instance
    assert reordered.lookup('EchoTime', (0, 0, 0),
                            ('time', 'samples')).sources == ('inst_t2',)
    assert reordered.lookup('EchoTime', (0, 1, 0),
                            ('time', 'samples')).sources == ('inst_t0',)
    assert reordered.lookup('EchoTime', (0, 2, 0),
                            ('time', 'samples')).sources == ('inst_t1',)
    assert reordered.sources_at((0, 0, 0)) == ('inst_t2',)


def test_reorder_vector_keeps_instance_mapping():
    summary = metasum.MetaSummary((1, 1, 3))
    for vec in range(3):
        summary.add((0, 0, vec), {'BValue': 100 * vec},
                    source='inst_v%d' % vec)
    reordered = summary.reorder(vector_perm=(2, 0, 1))
    assert reordered.get_values('BValue', ('vector', 'samples')) == \
        [200, 0, 100]
    for new_vec in range(3):
        result = reordered.lookup('BValue', (0, 0, new_vec),
                                  ('vector', 'samples'))
        old_vec = (2, 0, 1)[new_vec]
        assert result.value == 100 * old_vec
        assert result.sources == ('inst_v%d' % old_vec,)


def test_reorder_5d_lookup_invariant():
    summary = metasum.MetaSummary((2, 2, 2))
    for slc in range(2):
        for tm in range(2):
            for vec in range(2):
                meta = {'Global': (slc, tm, vec), 'Const': 3}
                summary.add((slc, tm, vec), meta,
                            source='inst_%d_%d_%d' % (slc, tm, vec))
    time_perm = (1, 0)
    vector_perm = (1, 0)
    reordered = summary.reorder(time_perm, vector_perm)
    for slc in range(2):
        for new_tm in range(2):
            for new_vec in range(2):
                old_tm = time_perm[new_tm]
                old_vec = vector_perm[new_vec]
                source = 'inst_%d_%d_%d' % (slc, old_tm, old_vec)
                new_index = (slc, new_tm, new_vec)
                assert reordered.sources_at(new_index) == (source,)
                result = reordered.lookup('Global', new_index,
                                          ('global', 'slices'))
                assert result.value == (slc, old_tm, old_vec)
                assert result.sources == (source,)
    #Reordering does not change which schemes explain the observations
    assert sorted(summary.candidates('Const')) == \
        sorted(reordered.candidates('Const'))


def test_reorder_invalid_perm():
    summary = metasum.MetaSummary((1, 2, 1))
    summary.add((0, 0, 0), {})
    summary.add((0, 1, 0), {})
    with pytest.raises(ValueError):
        summary.reorder(time_perm=(0, 0))
    with pytest.raises(ValueError):
        summary.reorder(vector_perm=(1,))


def test_inferred_shape_and_instances_arg():
    instances = [((0, 0, 0), {'Field': 1}, 'a'),
                 ((1, 0, 0), {'Field': 1}, 'b'),
                 ((0, 1, 0), {'Field': 1}, 'c'),
                 ((1, 1, 0), {'Field': 1}, 'd')]
    summary = metasum.MetaSummary(instances=instances,
                                  confirmed={'Field': ('global', 'const')})
    assert summary.shape == (2, 2, 1)
    assert summary.candidates('Field') == (('global', 'const'),)
    assert summary.get_values('Field') == 1
    with pytest.raises(IndexError):
        metasum.MetaSummary((1, 1, 1), instances=[((1, 0, 0), {})])


def test_unknown_field():
    summary = metasum.MetaSummary((1, 1, 1))
    summary.add((0, 0, 0), {'Field': 1})
    with pytest.raises(KeyError):
        summary.candidates('Unknown')
    with pytest.raises(KeyError):
        summary.classification_conflicts('Unknown', ('global', 'const'))
