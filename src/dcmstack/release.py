"""
Plan the release of a mixed batch of DICOM instances against a manifest.

Before a study is published, the acquired DICOM instances may be jumbled
together: multiple time points, multiple echoes, retransmitted files and
resampled slices all mixed in one batch. The ``ReleaseManifest`` declares
which time points, echoes and slice positions are expected, and which
resampled instances are allowed to stand in for an original. The function
`plan_release` works out which volumes can be published.

A *volume* is one (time point, echo) combination; it is complete when every
expected slice position is filled by exactly one usable instance. The rules
implemented here are:

* Each original instance is only used as the manifest allows: undeclared
  resamples are rejected, and declared resamples remain independent
  instances (they are legal alternatives to their original, not duplicates).
* The geometry of every instance used in a volume must agree with the rest
  of the group within the manifest tolerance.
* A slice is never borrowed across echoes: an instance only ever fills the
  slot matching its own (time point, echo, position), even if a slice from
  another echo sits at the very same position.
* Retransmitting the same original (identical SOPInstanceUID) does not
  occupy an extra layer; the copies are collapsed into one.
* When more than one legal partition of the batch exists, only the complete
  volumes whose composition is agreed upon by *every* legal partition are
  published. The remaining volumes are kept as candidates (with their
  alternatives) or reported as blocked (with their missing pieces).
* Combinations of time point and echo that the manifest does not declare
  remain ``unknown``.

The resulting `ReleasePlan` explains, for each volume, why it can be
published or which original instances are preventing it from being ready.
"""
import warnings
from collections import OrderedDict

import numpy as np

__all__ = ['PUBLISHED', 'CANDIDATE', 'BLOCKED', 'UNKNOWN', 'MAX_MATCHINGS',
           'SliceInstance', 'ReleaseManifest', 'SlotReport', 'VolumeReport',
           'ReleasePlan', 'plan_release', 'slice_instance_from_dcm',
           'instances_from_dcms']

PUBLISHED = 'published'
'''Status of a volume that is complete and identical in every legal
partition of the batch.'''

CANDIDATE = 'candidate'
'''Status of a volume that is complete in some legal partition, but whose
composition is not agreed upon by all of them.'''

BLOCKED = 'blocked'
'''Status of a volume that is not complete in any legal partition.'''

UNKNOWN = 'unknown'
'''Status of a (time point, echo) combination not declared by the
manifest.'''

MAX_MATCHINGS = 10000
'''Safety cap on the number of legal partitions enumerated per volume.'''


class SliceInstance(object):
    '''A single original DICOM instance (one slice) from the batch.

    Parameters
    ----------
    uid : str
        The SOPInstanceUID identifying the original acquisition. Two files
        with the same `uid` are retransmissions of the same original.

    time_point
        The time point ordinate of the instance (e.g. the value of
        TemporalPositionIdentifier).

    echo
        The echo ordinate of the instance (e.g. EchoNumbers or EchoTime).

    position : float
        The slice position of the instance along the slice axis.

    geometry : array_like, optional
        Geometry of the instance (e.g. the 3x3 rotation/scale part of the
        voxel-to-patient affine). Instances used in one volume must agree
        within the manifest tolerance.

    resample_of : str, optional
        The uid of the original instance this one is a resample of. Leave
        as None for original acquisitions. A resample is only usable when
        the manifest declares the correspondence.
    '''

    def __init__(self, uid, time_point, echo, position, geometry=None,
                 resample_of=None):
        self.uid = uid
        self.time_point = time_point
        self.echo = echo
        self.position = float(position)
        if geometry is None:
            self.geometry = None
        else:
            self.geometry = np.asarray(geometry, dtype=float).ravel()
        self.resample_of = resample_of

    @property
    def combo(self):
        '''The (time point, echo) combination this instance belongs to.'''
        return (self.time_point, self.echo)

    def __repr__(self):
        return ('SliceInstance(uid=%r, time_point=%r, echo=%r, position=%r)'
                % (self.uid, self.time_point, self.echo, self.position))


class ReleaseManifest(object):
    '''Declaration of the expected content of a mixed DICOM batch.

    Parameters
    ----------
    time_points : sequence
        The expected time point ordinates.

    echoes : sequence
        The expected echo ordinates.

    positions : sequence
        The expected slice positions of one volume.

    volumes : sequence, optional
        The expected (time point, echo) combinations. Defaults to the full
        cartesian product of `time_points` and `echoes`. Combinations found
        in the batch but not declared here remain ``unknown``.

    allowed_resamples : dict, optional
        Maps the uid of a resampled instance to the uid of the original it
        may stand in for. Resamples not declared here are rejected.

    position_tol : float
        Absolute tolerance for matching an instance position to an expected
        slice position.

    geometry_tol : float
        Absolute tolerance for the geometry agreement of the instances used
        in one volume.
    '''

    def __init__(self, time_points, echoes, positions, volumes=None,
                 allowed_resamples=None, position_tol=1e-3,
                 geometry_tol=5e-4):
        self.time_points = tuple(time_points)
        self.echoes = tuple(echoes)
        self.positions = tuple(sorted(float(pos) for pos in positions))
        if volumes is None:
            volumes = [(time_point, echo)
                       for time_point in self.time_points
                       for echo in self.echoes]
        self.volumes = tuple(volumes)
        self.allowed_resamples = dict(allowed_resamples or {})
        self.position_tol = float(position_tol)
        self.geometry_tol = float(geometry_tol)

    def declares(self, combo):
        '''Test if the (time point, echo) combination is declared.'''
        return combo in self.volumes

    def is_declared_resample(self, uid):
        '''Test if the manifest allows the instance `uid` as a resample.'''
        return uid in self.allowed_resamples


class SlotReport(object):
    '''Release information for one expected slice position of a volume.'''

    def __init__(self, position):
        self.position = position
        self.candidates = []
        self.chosen = None
        self.alternatives = []

    def __repr__(self):
        return ('SlotReport(position=%r, chosen=%r)'
                % (self.position, self.chosen))

    position = None
    '''The expected slice position of this slot.'''

    candidates = None
    '''Uids of all usable instances matching this slot.'''

    chosen = None
    '''The uid filling this slot when every legal partition agrees, else
    None.'''

    alternatives = None
    '''The uids this slot is filled with across the legal partitions, when
    they disagree.'''


class VolumeReport(object):
    '''Release information for one (time point, echo) combination.'''

    def __init__(self, combo):
        self.combo = combo
        self.status = UNKNOWN
        self.slots = []
        self.missing = []
        self.blockers = []
        self.reasons = []

    combo = None
    '''The (time point, echo) combination of this volume.'''

    status = None
    '''One of `PUBLISHED`, `CANDIDATE`, `BLOCKED` or `UNKNOWN`.'''

    slots = None
    '''List of `SlotReport`, one per expected slice position.'''

    missing = None
    '''Expected slice positions that no legal partition can fill.'''

    blockers = None
    '''Uids of original instances preventing this volume from being
    ready.'''

    reasons = None
    '''Human readable explanations for the status of this volume.'''

    @property
    def publishable(self):
        '''True if this volume may be published.'''
        return self.status == PUBLISHED

    def __repr__(self):
        return 'VolumeReport(combo=%r, status=%r)' % (self.combo, self.status)


class ReleasePlan(object):
    '''The result of planning the release of a mixed batch of instances.

    Attributes
    ----------
    manifest : ReleaseManifest
        The manifest the batch was checked against.

    volumes : dict
        Maps each (time point, echo) combination to its `VolumeReport`.

    retransmissions : list
        Uids that appeared more than once in the batch without occupying an
        extra layer.

    rejected : dict
        Maps the uid of each rejected instance to the reason it cannot be
        used.

    num_partitions : dict
        Maps each declared combination to the number of legal partitions
        found for it.
    '''

    def __init__(self, manifest):
        self.manifest = manifest
        self.volumes = OrderedDict()
        self.retransmissions = []
        self.rejected = OrderedDict()
        self.num_partitions = {}

    def publishable(self):
        '''List the combinations whose volumes may be published.'''
        return [combo for combo, report in self.volumes.items()
                if report.status == PUBLISHED]

    def candidates(self):
        '''List the combinations kept as candidates.'''
        return [combo for combo, report in self.volumes.items()
                if report.status == CANDIDATE]

    def blocked(self):
        '''List the combinations blocked from being ready.'''
        return [combo for combo, report in self.volumes.items()
                if report.status == BLOCKED]

    def explain(self, combo):
        '''Human readable explanation for the status of one volume.'''
        report = self.volumes[combo]
        lines = ['Volume (time_point=%r, echo=%r): %s'
                 % (report.combo[0], report.combo[1], report.status)]
        lines.extend('- ' + reason for reason in report.reasons)
        return '\n'.join(lines)


def _dedupe(instances, manifest, plan):
    '''Collapse retransmissions, returning the usable originals.'''
    by_uid = OrderedDict()
    for inst in instances:
        by_uid.setdefault(inst.uid, []).append(inst)
    originals = []
    for uid, copies in by_uid.items():
        first = copies[0]
        same = all(c.time_point == first.time_point and
                   c.echo == first.echo and
                   abs(c.position - first.position) <= manifest.position_tol
                   for c in copies[1:])
        if not same:
            plan.rejected[uid] = ('conflicting retransmissions of the same '
                                  'original (uid %s)' % uid)
            continue
        if len(copies) > 1:
            # A retransmission of the same original: it does not occupy an
            # extra layer, the first copy is kept.
            plan.retransmissions.append(uid)
        originals.append(first)
    return originals


def _authorize(instances, manifest, plan):
    '''Reject resamples the manifest does not allow.'''
    usable = []
    for inst in instances:
        declared = manifest.is_declared_resample(inst.uid)
        if inst.resample_of is None and not declared:
            usable.append(inst)
            continue
        if not declared:
            plan.rejected[inst.uid] = ('resample of %s not declared in the '
                                       'manifest' % (inst.resample_of,))
            continue
        target = manifest.allowed_resamples[inst.uid]
        if inst.resample_of is not None and inst.resample_of != target:
            plan.rejected[inst.uid] = ('declared as resample of %s but '
                                       'claims %s' % (target,
                                                      inst.resample_of))
            continue
        usable.append(inst)
    return usable


def _geometry_ok(members, manifest, report, plan):
    '''Keep only instances whose geometry agrees with the group.'''
    with_geo = [inst for inst in members if inst.geometry is not None]
    if not with_geo:
        return list(members)
    originals = [inst for inst in with_geo if inst.resample_of is None]
    ref = (originals or with_geo)[0].geometry
    kept, dropped = [], []
    for inst in members:
        if (inst.geometry is None or
                np.allclose(inst.geometry, ref, atol=manifest.geometry_tol,
                            rtol=0)):
            kept.append(inst)
        else:
            dropped.append(inst)
    for inst in dropped:
        plan.rejected[inst.uid] = ('geometry outside the group tolerance '
                                   '(%g)' % manifest.geometry_tol)
        report.blockers.append(inst.uid)
        report.reasons.append(
            'instance %s deviates from the group geometry by more than %g'
            % (inst.uid, manifest.geometry_tol))
    return kept


def _match_slots(members, manifest, report, plan):
    '''Match instances to candidate slots. Returns (slot candidates,
    instance -> candidate slot indices).'''
    inst_slots = OrderedDict()
    for inst in members:
        cand = [idx for idx, slot in enumerate(report.slots)
                if abs(inst.position - slot.position) <=
                manifest.position_tol]
        if not cand:
            plan.rejected[inst.uid] = ('position %g matches no expected '
                                       'slice position' % inst.position)
            report.blockers.append(inst.uid)
            report.reasons.append(
                'instance %s at position %g is off the expected slice grid'
                % (inst.uid, inst.position))
            continue
        inst_slots[inst.uid] = cand
    slot_cands = [[] for _ in report.slots]
    for uid, cand in inst_slots.items():
        for idx in cand:
            slot_cands[idx].append(uid)
    # Resolve collisions between distinct originals the manifest does not
    # cover: only declared resamples may share a slot with another uid.
    # Instances matching several slots are ambiguous rather than
    # duplicated; the partition enumeration deals with them.
    for idx, uids in enumerate(slot_cands):
        declared = [uid for uid in uids if manifest.is_declared_resample(uid)]
        originals = sorted(uid for uid in uids
                           if not manifest.is_declared_resample(uid))
        unambiguous = [uid for uid in originals
                       if len(inst_slots[uid]) == 1]
        for uid in unambiguous[1:]:
            originals.remove(uid)
            plan.rejected[uid] = ('undeclared duplicate at slice position '
                                  '%g' % report.slots[idx].position)
            report.blockers.append(uid)
            report.reasons.append(
                'instance %s duplicates position %g without a manifest '
                'resample declaration' % (uid, report.slots[idx].position))
            del inst_slots[uid]
        slot_cands[idx] = originals + declared
    return slot_cands, inst_slots


def _enumerate_matchings(slot_cands, inst_slots):
    '''Enumerate all maximal matchings of instances to slots.'''
    n_slots = len(slot_cands)
    matchings = []

    def recurse(slot_idx, used, assign):
        if len(matchings) >= MAX_MATCHINGS:
            return
        if slot_idx == n_slots:
            # Keep only maximal matchings: every unused instance must have
            # all of its candidate slots occupied.
            for uid, cand in inst_slots.items():
                if uid in used:
                    continue
                if any(assign[idx] is None for idx in cand):
                    return
            matchings.append(tuple(assign))
            return
        recurse(slot_idx + 1, used, assign + [None])
        for uid in slot_cands[slot_idx]:
            if uid not in used:
                recurse(slot_idx + 1, used | set([uid]), assign + [uid])

    recurse(0, set(), [])
    return matchings


def _note_foreign_slices(report, all_instances, manifest):
    '''Note slices of other echoes sitting at missing positions (they are
    never borrowed to fill a layer).'''
    for pos in report.missing:
        for inst in all_instances:
            if (inst.combo != report.combo and
                    abs(inst.position - pos) <= manifest.position_tol):
                report.reasons.append(
                    'instance %s at position %g belongs to echo %r and is '
                    'not borrowed to fill echo %r'
                    % (inst.uid, pos, inst.echo, report.combo[1]))


def _plan_combo(combo, members, all_instances, manifest, plan):
    report = VolumeReport(combo)
    report.slots = [SlotReport(pos) for pos in manifest.positions]
    members = _geometry_ok(members, manifest, report, plan)
    slot_cands, inst_slots = _match_slots(members, manifest, report, plan)
    for slot, uids in zip(report.slots, slot_cands):
        slot.candidates = list(uids)
    matchings = _enumerate_matchings(slot_cands, inst_slots)
    plan.num_partitions[combo] = len(matchings)
    complete = [m for m in matchings if all(uid is not None for uid in m)]
    n_slots = len(report.slots)
    if len(matchings) >= MAX_MATCHINGS:
        report.status = CANDIDATE
        report.reasons.append('more than %d legal partitions; kept as '
                              'candidate' % MAX_MATCHINGS)
        return report
    if complete and len(complete) == len(matchings):
        if len(set(complete)) == 1:
            report.status = PUBLISHED
            for slot, uid in zip(report.slots, complete[0]):
                slot.chosen = uid
            report.reasons.append(
                'all %d expected slices present and identical across the '
                '%d legal partition(s); geometry agrees within %g'
                % (n_slots, len(matchings), manifest.geometry_tol))
        else:
            report.status = CANDIDATE
            for idx, slot in enumerate(report.slots):
                slot.alternatives = sorted(set(m[idx] for m in complete))
            report.reasons.append(
                'complete in every legal partition but the composition '
                'differs across the %d partitions' % len(matchings))
    elif complete:
        report.status = CANDIDATE
        report.reasons.append(
            'complete in %d of %d legal partitions'
            % (len(complete), len(matchings)))
    else:
        report.status = BLOCKED
    # Missing pieces: positions no legal partition can fill
    for idx, slot in enumerate(report.slots):
        if all(m[idx] is None for m in matchings):
            report.missing.append(slot.position)
    if report.status == BLOCKED:
        if not members:
            report.reasons.append('no usable instances received for this '
                                  'combination')
        for pos in report.missing:
            report.reasons.append('expected slice position %g is never '
                                  'filled' % pos)
        _note_foreign_slices(report, all_instances, manifest)
    return report


def plan_release(instances, manifest):
    '''Plan the release of a mixed batch of DICOM instances.

    Parameters
    ----------
    instances : sequence
        The `SliceInstance` objects found in the batch.

    manifest : ReleaseManifest
        The declaration of expected time points, echoes, slice positions
        and allowed resamples.

    Returns
    -------
    plan : ReleasePlan
        The release plan. Volumes common to every legal partition are
        marked `PUBLISHED`; the rest are kept as candidates or reported as
        blocked, together with the instances responsible.
    '''
    plan = ReleasePlan(manifest)
    originals = _dedupe(instances, manifest, plan)
    usable = _authorize(originals, manifest, plan)
    by_combo = OrderedDict()
    for inst in usable:
        by_combo.setdefault(inst.combo, []).append(inst)
    # Declared combinations first, in manifest order
    for combo in manifest.volumes:
        members = by_combo.pop(combo, [])
        plan.volumes[combo] = _plan_combo(combo, members, usable, manifest,
                                          plan)
    # Anything left over is not covered by the manifest: stays unknown
    for combo, members in by_combo.items():
        report = VolumeReport(combo)
        report.status = UNKNOWN
        report.reasons.append(
            'combination not declared in the manifest; %d instance(s) '
            'received, status remains unknown' % len(members))
        plan.volumes[combo] = report
    # Note retransmissions on the volumes they belong to
    for uid in plan.retransmissions:
        for inst in instances:
            if inst.uid == uid:
                report = plan.volumes.get(inst.combo)
                if report is not None:
                    report.reasons.append(
                        'instance %s was retransmitted; counted once'
                        % uid)
                break
    return plan


def slice_instance_from_dcm(dcm, meta=None,
                            time_key='TemporalPositionIdentifier',
                            echo_key='EchoNumbers', resample_of=None):
    '''Build a `SliceInstance` from a pydicom data set.

    Parameters
    ----------
    dcm : pydicom.dataset.Dataset
        The data set to describe.

    meta : dict, optional
        Pre-extracted meta data for `dcm`. If None, the minimal meta data
        is extracted.

    time_key : str
        The meta data key used as the time point ordinate.

    echo_key : str
        The meta data key used as the echo ordinate.

    resample_of : str, optional
        The uid of the original this data set is a resample of.
    '''
    if meta is None:
        from .extract import EXTRACTORS, ExtractionLevel
        meta = EXTRACTORS[ExtractionLevel.MINIMAL](dcm)
    from .nifti_wrapper import NiftiWrapper
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        from nibabel.nicom.dicomwrappers import wrapper_from_data
        dw = wrapper_from_data(dcm)
    nii_wrp = NiftiWrapper.from_dicom_wrapper(dw, meta)
    uid = dcm.get('SOPInstanceUID', meta.get('SOPInstanceUID'))
    geometry = np.asarray(nii_wrp.nii_img.affine)[:3, :3]
    return SliceInstance(uid, meta.get(time_key), meta.get(echo_key),
                         float(dw.slice_indicator), geometry,
                         resample_of=resample_of)


def instances_from_dcms(dcms, **kwargs):
    '''Build a list of `SliceInstance` from pydicom data sets. Extra
    keyword arguments are passed to `slice_instance_from_dcm`.'''
    return [slice_instance_from_dcm(dcm, **kwargs) for dcm in dcms]
