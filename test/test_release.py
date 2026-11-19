"""
Tests for dcmstack.release
"""
from __future__ import absolute_import, print_function

from glob import glob
from os import path

import pytest

from . import test_dir, src_dir

import pydicom

import dcmstack
from dcmstack import release
from dcmstack.release import (SliceInstance, ReleaseManifest, plan_release,
                              PUBLISHED, CANDIDATE, BLOCKED, UNKNOWN)


def make_manifest(**kwargs):
    args = dict(time_points=[1], echoes=[1], positions=[0.0, 1.0, 2.0])
    args.update(kwargs)
    return ReleaseManifest(**args)


def make_instances(combo=(1, 1), positions=(0.0, 1.0, 2.0), prefix='i'):
    return [SliceInstance('%s_%g' % (prefix, pos), combo[0], combo[1], pos)
            for pos in positions]


class TestBasicRelease(object):
    def test_complete_volume_published(self):
        manifest = make_manifest()
        plan = plan_release(make_instances(), manifest)
        report = plan.volumes[(1, 1)]
        assert report.status == PUBLISHED
        assert plan.publishable() == [(1, 1)]
        assert [slot.chosen for slot in report.slots] == ['i_0', 'i_1', 'i_2']
        assert report.missing == []
        # The report explains why the volume can be published
        text = plan.explain((1, 1))
        assert 'published' in text
        assert 'legal partition' in text

    def test_missing_slice_blocked(self):
        manifest = make_manifest()
        instances = make_instances(positions=(0.0, 2.0))
        plan = plan_release(instances, manifest)
        report = plan.volumes[(1, 1)]
        assert report.status == BLOCKED
        assert report.missing == [1.0]
        text = plan.explain((1, 1))
        assert 'never filled' in text

    def test_empty_volume_blocked(self):
        manifest = make_manifest()
        plan = plan_release([], manifest)
        report = plan.volumes[(1, 1)]
        assert report.status == BLOCKED
        assert report.missing == [0.0, 1.0, 2.0]

    def test_multiple_echoes_independent(self):
        manifest = make_manifest(echoes=[1, 2])
        instances = (make_instances(combo=(1, 1), prefix='a') +
                     make_instances(combo=(1, 2), prefix='b'))
        plan = plan_release(instances, manifest)
        assert plan.volumes[(1, 1)].status == PUBLISHED
        assert plan.volumes[(1, 2)].status == PUBLISHED
        assert [slot.chosen for slot in plan.volumes[(1, 2)].slots] == \
            ['b_0', 'b_1', 'b_2']


class TestRetransmission(object):
    def test_retransmission_no_extra_layer(self):
        manifest = make_manifest()
        instances = make_instances()
        # Re-send the same original: must not occupy an extra layer
        instances.append(SliceInstance('i_1', 1, 1, 1.0))
        plan = plan_release(instances, manifest)
        assert plan.volumes[(1, 1)].status == PUBLISHED
        assert plan.retransmissions == ['i_1']
        assert plan.num_partitions[(1, 1)] == 1
        assert 'retransmitted' in plan.explain((1, 1))

    def test_conflicting_retransmission_rejected(self):
        manifest = make_manifest()
        instances = make_instances(positions=(0.0, 2.0))
        # Same uid shows up at a different position: conflict
        instances.append(SliceInstance('i_0', 1, 1, 1.0))
        plan = plan_release(instances, manifest)
        assert 'i_0' in plan.rejected
        assert 'conflicting' in plan.rejected['i_0']
        assert plan.volumes[(1, 1)].status == BLOCKED


class TestResampling(object):
    def test_declared_resample_stays_independent(self):
        manifest = make_manifest(allowed_resamples={'r_1': 'i_1'})
        instances = make_instances()
        # A true resample of the slice at position 1.0
        instances.append(SliceInstance('r_1', 1, 1, 1.0,
                                       resample_of='i_1'))
        plan = plan_release(instances, manifest)
        report = plan.volumes[(1, 1)]
        # The resample is an independent alternative, so the composition is
        # not agreed upon by all legal partitions: kept as candidate
        assert report.status == CANDIDATE
        assert plan.num_partitions[(1, 1)] == 2
        assert report.slots[1].alternatives == ['i_1', 'r_1']
        assert 'r_1' not in plan.rejected

    def test_undeclared_resample_rejected(self):
        manifest = make_manifest()
        instances = make_instances(positions=(0.0, 2.0))
        instances.append(SliceInstance('r_1', 1, 1, 1.0,
                                       resample_of='i_1'))
        plan = plan_release(instances, manifest)
        assert 'r_1' in plan.rejected
        assert 'not declared' in plan.rejected['r_1']
        # The undeclared resample cannot fill the missing layer
        assert plan.volumes[(1, 1)].status == BLOCKED
        assert plan.volumes[(1, 1)].missing == [1.0]

    def test_undeclared_duplicate_rejected(self):
        manifest = make_manifest()
        instances = make_instances()
        # A second uid claims the same slot without any declaration
        instances.append(SliceInstance('x_1', 1, 1, 1.0))
        plan = plan_release(instances, manifest)
        assert 'x_1' in plan.rejected
        assert 'undeclared duplicate' in plan.rejected['x_1']
        # The original still publishes
        assert plan.volumes[(1, 1)].status == PUBLISHED

    def test_resample_target_mismatch_rejected(self):
        manifest = make_manifest(allowed_resamples={'r_1': 'i_2'})
        instances = make_instances()
        instances.append(SliceInstance('r_1', 1, 1, 1.0,
                                       resample_of='i_1'))
        plan = plan_release(instances, manifest)
        assert 'r_1' in plan.rejected
        assert 'claims' in plan.rejected['r_1']


class TestCrossEchoBorrowing(object):
    def test_no_borrowing_across_echoes(self):
        manifest = make_manifest(echoes=[1, 2])
        # Echo 1 misses the slice at 1.0, echo 2 has one at that position
        instances = make_instances(combo=(1, 1), positions=(0.0, 2.0),
                                   prefix='a')
        instances += make_instances(combo=(1, 2), positions=(1.0,),
                                    prefix='b')
        plan = plan_release(instances, manifest)
        report = plan.volumes[(1, 1)]
        assert report.status == BLOCKED
        assert report.missing == [1.0]
        # The explanation names the foreign slice that is not borrowed
        text = plan.explain((1, 1))
        assert 'b_1' in text
        assert 'not borrowed' in text


class TestUnknownCombos(object):
    def test_undeclared_combo_unknown(self):
        manifest = make_manifest()
        instances = make_instances(combo=(1, 7), prefix='z')
        plan = plan_release(instances, manifest)
        assert plan.volumes[(1, 7)].status == UNKNOWN
        assert 'not declared' in plan.explain((1, 7))
        # The declared volume is unaffected
        assert plan.volumes[(1, 1)].status == BLOCKED


class TestGeometry(object):
    def test_geometry_within_tolerance(self):
        manifest = make_manifest(geometry_tol=1e-3)
        instances = make_instances()
        for idx, inst in enumerate(instances):
            inst.geometry = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0,
                             1.0 + idx * 1e-4]
        plan = plan_release(instances, manifest)
        assert plan.volumes[(1, 1)].status == PUBLISHED

    def test_geometry_outside_tolerance(self):
        manifest = make_manifest(geometry_tol=1e-3)
        instances = make_instances()
        for inst in instances:
            inst.geometry = [1.0] * 9
        instances[2].geometry = [2.0] * 9
        plan = plan_release(instances, manifest)
        report = plan.volumes[(1, 1)]
        assert report.status == BLOCKED
        assert 'i_2' in plan.rejected
        assert 'geometry' in plan.rejected['i_2']
        assert 'i_2' in report.blockers
        assert report.missing == [2.0]


class TestAmbiguousPartitions(object):
    def test_complete_in_some_partitions_is_candidate(self):
        # Instance 'b' sits between two expected positions and can fill
        # either; only one of the two legal partitions completes the volume
        manifest = make_manifest(positions=[0.0, 1.0], position_tol=0.6)
        instances = [SliceInstance('a', 1, 1, 0.0),
                     SliceInstance('b', 1, 1, 0.5)]
        plan = plan_release(instances, manifest)
        report = plan.volumes[(1, 1)]
        assert report.status == CANDIDATE
        assert plan.num_partitions[(1, 1)] == 2
        assert '1 of 2' in plan.explain((1, 1))

    def test_common_complete_volumes_published(self):
        # Two volumes share an ambiguous instance; the volume that is
        # complete and identical in every legal partition is published
        manifest = make_manifest(positions=[0.0, 1.0], position_tol=0.4,
                                 echoes=[1, 2])
        instances = [SliceInstance('a', 1, 1, 0.0),
                     SliceInstance('b', 1, 1, 1.0),
                     SliceInstance('c', 1, 2, 0.0),
                     SliceInstance('d', 1, 2, 1.0)]
        plan = plan_release(instances, manifest)
        assert plan.publishable() == [(1, 1), (1, 2)]

    def test_ambiguity_keeps_candidate(self):
        # All partitions complete the volume but disagree on the content
        manifest = make_manifest(positions=[0.0, 1.0], position_tol=0.6)
        instances = [SliceInstance('a', 1, 1, 0.0),
                     SliceInstance('b', 1, 1, 1.0),
                     SliceInstance('c', 1, 1, 0.5)]
        plan = plan_release(instances, manifest)
        report = plan.volumes[(1, 1)]
        assert report.status == CANDIDATE
        assert plan.num_partitions[(1, 1)] == 3


class TestDicomAdapter(object):
    def test_instances_from_dcms(self):
        dcm_paths = sorted(glob(path.join(test_dir, 'data', 'dcmstack',
                                          '2D_16Echo_qT2', '*.dcm')))
        dcms = [pydicom.dcmread(dcm_path) for dcm_path in dcm_paths]
        instances = release.instances_from_dcms(dcms, echo_key='EchoTime')
        assert len(instances) == 7
        echoes = sorted(set(inst.echo for inst in instances))
        assert echoes == [20.0, 40.0, 60.0]
        positions = sorted(set(inst.position for inst in instances))
        assert len(positions) == 3
        manifest = ReleaseManifest(time_points=[None], echoes=echoes,
                                   positions=positions)
        plan = plan_release(instances, manifest)
        # Only the TE=20 volume has all three slices; the other two echoes
        # are blocked by the missing slice
        assert plan.publishable() == [(None, 20.0)]
        report = plan.volumes[(None, 20.0)]
        assert report.status == PUBLISHED
        assert len(report.slots) == 3
        present = set(inst.position for inst in instances
                      if inst.echo == 40.0)
        missing = [pos for pos in positions if
                   all(abs(pos - p) > manifest.position_tol
                       for p in present)]
        for echo in (40.0, 60.0):
            report = plan.volumes[(None, echo)]
            assert report.status == BLOCKED
            assert report.missing == missing
