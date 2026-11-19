"""Select explicit subsets of voxels (and the associated meta data) from
Nifti images, optionally carrying an embedded dcmmeta extension.

The downstream consumer provides an explicit selection table: for each
dimension (slices, time points, vector components) a sequence of source
indices. Selections may be non-contiguous, reordered, and may repeat the
same source sample multiple times. Each repetition gets an independent
output position, and source samples that are not selected never contribute
values to the result.

A selection produces three things:

* The sub-data array.
* The spatial correspondence between output and source: an updated affine
  when the slice selection is uniformly spaced (the only case a 4x4 affine
  can faithfully represent), otherwise an explicit per-slice mapping.
* The queryable meta data (a new `DcmMetaExtension`), where every key keeps
  its classification if that classification still precisely describes the
  selected values, and is reclassified (with provenance recorded in the
  report) when the new dimension structure requires it.

When the slice selection cannot be represented by an affine, the `policy`
argument decides between returning an explicit bypass mapping ('bypass')
or refusing the selection ('strict').
"""
from __future__ import print_function

import numpy as np
import nibabel as nb
from nibabel.nifti1 import Nifti1Extensions

from .dcmmeta import DcmMetaExtension
from .utils import iteritems


class SelectionError(Exception):
    '''Exception denoting that a selection cannot be faithfully represented
    and the active policy is to refuse it.'''
    def __init__(self, msg):
        self.msg = msg

    def __str__(self):
        return 'Unable to represent the selection: %s' % self.msg


def find_dcmmeta(nii_img):
    '''Return the first `DcmMetaExtension` in the Nifti header, or None.'''
    for ext in nii_img.header.extensions:
        if isinstance(ext, DcmMetaExtension):
            return ext
    return None


class VoxelSelection(object):
    '''The result of `select_voxels`.

    Attributes
    ----------
    data : ndarray
        The selected sub-data. Repeated source samples appear as independent
        output positions.

    affine : array or None
        The 4x4 affine for the selected data, or None if the slice selection
        is not uniformly spaced and thus cannot be represented by an affine.
        In that case use `spatial_map` for the geometry.

    spatial_map : dict
        The explicit spatial/sample correspondence. The 'slice' entry holds
        the source slice index and the patient space position of each output
        slice (plus the uniform source index step, if any). The 'time' and
        'vector' entries hold the source sample indices for each output
        position along those dimensions.

    meta : DcmMetaExtension or None
        The meta data for the selection, or None if the source had no
        dcmmeta extension or the selection could not be applied to it (see
        the notes in `report`). The source extension is never modified and
        remains available as `source_meta`.

    report : dict
        Provenance for the selection: normalized index tables, meta data
        reclassifications, dropped dimension ordering attributes, whether
        the affine faithfully represents the selection, and notes about any
        bypass that was applied.

    source_nii : Nifti1Image
        The unmodified source image.

    source_meta : DcmMetaExtension or None
        The unmodified source meta data extension.
    '''

    def __init__(self, data, affine, spatial_map, meta, report, source_nii,
                 source_meta):
        self.data = data
        self.affine = affine
        self.spatial_map = spatial_map
        self.meta = meta
        self.report = report
        self.source_nii = source_nii
        self.source_meta = source_meta

    def to_nifti(self):
        '''Build a Nifti1Image from the selection.

        Raises
        ------
        SelectionError
            If the selection has no affine representation. Use
            `spatial_map` for the geometry in that case.
        '''
        if self.affine is None:
            raise SelectionError('the slice selection is not uniformly '
                                 'spaced, use the spatial_map for the '
                                 'geometry')
        result = nb.Nifti1Image(self.data, self.affine)
        if self.meta is not None:
            result.header.extensions = Nifti1Extensions([self.meta])
        return result


def _normalize_selection(selection, slice_dim, shape):
    '''Normalize a selection table into {dim: [source indices]} and check it
    against the data shape.'''
    dim_indices = {}
    for dim_key, indices in iteritems(selection):
        if dim_key == 'slice':
            dim = slice_dim
        elif dim_key == 'time':
            dim = 3
        elif dim_key == 'vector':
            dim = 4
        else:
            dim = int(dim_key)
        if dim in dim_indices:
            raise ValueError("Multiple selections given for dimension "
                             "%d" % dim)
        indices = [int(idx) for idx in indices]
        if len(indices) == 0:
            raise ValueError("Empty selection for dimension %d" % dim)
        dim_indices[dim] = indices
    for dim, indices in iteritems(dim_indices):
        if dim < 0 or dim > 4:
            raise ValueError("Invalid dimension: %d" % dim)
        if dim < 3 and dim != slice_dim:
            raise ValueError("Can only select along the slice dimension, "
                             "not spatial dimension %d" % dim)
        if dim >= len(shape):
            raise ValueError("Dimension %d does not exist in the data "
                             "shape" % dim)
        for idx in indices:
            if not 0 <= idx < shape[dim]:
                raise ValueError("Index %d is out of bounds for dimension "
                                 "%d with size %d" % (idx, dim, shape[dim]))
    return dim_indices


def _uniform_step(indices):
    '''Return the step if the indices form a non-degenerate arithmetic
    progression, otherwise None.'''
    if len(indices) < 2:
        return 1
    step = indices[1] - indices[0]
    if step == 0:
        return None
    for idx in range(1, len(indices) - 1):
        if indices[idx + 1] - indices[idx] != step:
            return None
    return step


def select_voxels(nii_img, selection, policy='bypass'):
    '''Select an explicit subset of voxels and meta data from a Nifti image.

    Parameters
    ----------
    nii_img : Nifti1Image
        The source image. May carry a `DcmMetaExtension`; it is never
        modified.

    selection : dict
        Maps a dimension to the explicit sequence of source indices to
        select. Dimensions can be given as integers or as the strings
        'slice' (the slice dimension), 'time' (dimension 3), and 'vector'
        (dimension 4). Indices may be non-contiguous, out of order, and
        repeated. Dimensions not present in the dict are kept in full.

    policy : str
        What to do when the selection cannot be faithfully represented by a
        Nifti image (i.e. the slice selection is not uniformly spaced):
        'bypass' returns the result with `affine` set to None and an
        explicit per-slice `spatial_map`; 'strict' raises `SelectionError`.

    Returns
    -------
    selection : VoxelSelection
        The sub-data, spatial correspondence, queryable meta data, and
        provenance report.

    Raises
    ------
    ValueError
        If the selection table is invalid (unknown dimensions, empty
        selections, or out of bounds indices).

    SelectionError
        If the selection cannot be faithfully represented and `policy` is
        'strict'.
    '''
    if policy not in ('bypass', 'strict'):
        raise ValueError("The policy must be 'bypass' or 'strict'")
    data = np.asanyarray(nii_img.dataobj)
    src_affine = np.asarray(nii_img.affine)
    meta_ext = find_dcmmeta(nii_img)
    if meta_ext is not None and meta_ext.slice_dim is not None:
        slice_dim = meta_ext.slice_dim
    else:
        slice_dim = nii_img.header.get_dim_info()[2]
    if slice_dim is None:
        slice_dim = 2

    dim_indices = _normalize_selection(selection, slice_dim, data.shape)

    #If the source meta data has no slice dimension, per-slice meta data
    #cannot be subset; the meta data cannot be included in the result
    meta_bypass_note = None
    if (meta_ext is not None and meta_ext.slice_dim is None and
            any(dim < 3 for dim in dim_indices)):
        if policy == 'strict':
            raise SelectionError('the source meta data has no slice '
                                 'dimension to select along')
        meta_bypass_note = ('the source meta data has no slice dimension, '
                            'so it was not subset; query source_meta with '
                            'the spatial_map index tables instead')

    #Gather the sub-data; repeated indices become independent positions
    out_data = data
    for dim in sorted(dim_indices):
        out_data = np.take(out_data, dim_indices[dim], axis=dim)

    #Build the explicit spatial correspondence
    n_slices = data.shape[slice_dim]
    slc_table = dim_indices.get(slice_dim, list(range(n_slices)))
    time_table = dim_indices.get(
        3, list(range(data.shape[3])) if data.ndim > 3 else [0])
    vec_table = dim_indices.get(
        4, list(range(data.shape[4])) if data.ndim > 4 else [0])
    positions = []
    for src_slc in slc_table:
        vox = np.zeros(4)
        vox[slice_dim] = src_slc
        vox[3] = 1.0
        positions.append(np.dot(src_affine, vox)[:3])
    step = _uniform_step(slc_table)
    affine_representable = step is not None
    spatial_map = {
        'slice' : {'source_indices' : list(slc_table),
                   'positions' : np.asarray(positions),
                   'uniform_step' : step if affine_representable else None},
        'time' : {'source_indices' : list(time_table)},
        'vector' : {'source_indices' : list(vec_table)},
    }

    #Compute the output affine if one can faithfully represent the selection
    new_affine = None
    if affine_representable:
        new_affine = src_affine.copy()
        new_affine[:3, slice_dim] = src_affine[:3, slice_dim] * step
        new_affine[:3, 3] = (src_affine[:3, 3] +
                             src_affine[:3, slice_dim] * slc_table[0])
    elif policy == 'strict':
        raise SelectionError('the slice selection is not uniformly spaced, '
                             'so no affine can represent it')

    #Subset the meta data, recording provenance in the report
    report = {'selection' : dict((dim, list(indices))
                                 for dim, indices
                                 in iteritems(dim_indices)),
              'affine_representable' : affine_representable,
              'reclassified' : {},
              'dropped_dim_order' : {},
              'notes' : [],
             }
    new_meta = None
    if meta_ext is None:
        report['notes'].append('the source image has no dcmmeta extension')
    elif meta_bypass_note is not None:
        report['notes'].append(meta_bypass_note)
    else:
        new_meta, meta_report = meta_ext.take(dim_indices)
        report['reclassified'] = meta_report['reclassified']
        report['dropped_dim_order'] = meta_report['dropped_dim_order']
        report['meta_shape'] = meta_report['shape']
        if new_affine is not None:
            new_meta.affine = new_affine
        else:
            report['notes'].append(
                'dcmmeta_affine was left as the source affine; use the '
                'spatial_map for the per-slice geometry')

    return VoxelSelection(out_data,
                          new_affine,
                          spatial_map,
                          new_meta,
                          report,
                          nii_img,
                          meta_ext)
