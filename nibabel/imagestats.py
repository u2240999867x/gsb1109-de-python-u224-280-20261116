# emacs: -*- mode: python-mode; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the NiBabel package for the
#   copyright and license terms.
#
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Functions for computing image statistics"""

from __future__ import annotations

import csv
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from nibabel.imageclasses import spatial_axes_first

BACKGROUND = 'background'
UNKNOWN_PREFIX = 'unknown:'
OUTSIDE = 'outside'
OUTSIDE_CODE = np.iinfo(np.int64).min
RESERVED_CATEGORIES = frozenset({BACKGROUND, UNKNOWN_PREFIX[:-1], OUTSIDE})


class DegenerateTransformError(ValueError):
    """Raised when an affine cannot define a physical voxel volume."""


class LabelTableError(ValueError):
    """Raised when a label table cannot be interpreted unambiguously."""


@dataclass(frozen=True)
class LabelEntry:
    """Description of one source label in a label table.

    Parameters
    ----------
    label : int
        Value in the segmentation image.
    name : str
        Human-readable name for the source label.
    region : str
        Business region this source label belongs to.  Several source labels
        may share one region.
    """

    label: int
    name: str
    region: str


@dataclass(frozen=True)
class LabelStat:
    """Statistics for one source label or one special category."""

    label: int
    name: str
    region: str
    voxel_count: int
    volume_mm3: float | None = None

    @property
    def count(self) -> int:
        """Alias for :attr:`voxel_count`."""
        return self.voxel_count

    @property
    def volume(self) -> float | None:
        """Alias for :attr:`volume_mm3`."""
        return self.volume_mm3

    def __getitem__(self, key: str):
        return getattr(self, key)


@dataclass(frozen=True)
class RegionStat:
    """Aggregated statistics for one business region."""

    region: str
    voxel_count: int
    volume_mm3: float | None
    label_counts: Mapping[int, int]
    label_volumes: Mapping[int, float | None]

    @property
    def labels(self) -> tuple[int, ...]:
        """Source labels contributing to this region, in ascending order."""
        return tuple(sorted(self.label_counts))

    @property
    def count(self) -> int:
        """Alias for :attr:`voxel_count`."""
        return self.voxel_count

    @property
    def volume(self) -> float | None:
        """Alias for :attr:`volume_mm3`."""
        return self.volume_mm3

    def __getitem__(self, key: str):
        return getattr(self, key)


@dataclass(frozen=True)
class LabelStatistics:
    """Result of :func:`label_statistics`.

    ``labels`` has one entry per known source label, ``regions`` has the
    aggregated business regions, ``unknown`` has labels present in the image
    but absent from the table, and ``background`` is kept separate from both.
    """

    labels: Mapping[int, LabelStat]
    regions: Mapping[str, RegionStat]
    unknown: Mapping[int, LabelStat]
    background: LabelStat
    label_to_region: Mapping[int, str]
    voxel_volume_mm3: float | None

    def __getitem__(self, region: str) -> RegionStat:
        return self.regions[region]

    @property
    def by_region(self) -> Mapping[str, RegionStat]:
        """Alias for :attr:`regions`."""
        return self.regions

    @property
    def by_label(self) -> Mapping[int, LabelStat]:
        """Alias for :attr:`labels`."""
        return self.labels

    @property
    def volume_defined(self) -> bool:
        """True when the affine permits a physical volume conclusion."""
        return self.voxel_volume_mm3 is not None

    @property
    def total_voxels(self) -> int:
        return (
            self.background.voxel_count
            + sum(stat.voxel_count for stat in self.labels.values())
            + sum(stat.voxel_count for stat in self.unknown.values())
        )

    @property
    def total_volume_mm3(self) -> float | None:
        if self.voxel_volume_mm3 is None:
            return None
        return self.total_voxels * self.voxel_volume_mm3

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of the statistics."""

        def _label_dict(stat: LabelStat) -> dict[str, Any]:
            return {
                'label': int(stat.label),
                'name': stat.name,
                'region': stat.region,
                'voxel_count': int(stat.voxel_count),
                'volume_mm3': stat.volume_mm3,
            }

        return {
            'voxel_volume_mm3': self.voxel_volume_mm3,
            'volume_defined': self.volume_defined,
            'total_voxels': self.total_voxels,
            'total_volume_mm3': self.total_volume_mm3,
            'label_to_region': {int(k): v for k, v in self.label_to_region.items()},
            'labels': [_label_dict(stat) for _, stat in sorted(self.labels.items())],
            'regions': [
                {
                    'region': stat.region,
                    'voxel_count': int(stat.voxel_count),
                    'volume_mm3': stat.volume_mm3,
                    'labels': [
                        {
                            'label': int(label),
                            'voxel_count': int(count),
                            'volume_mm3': stat.label_volumes[label],
                        }
                        for label, count in sorted(stat.label_counts.items())
                    ],
                }
                for _, stat in sorted(self.regions.items())
            ],
            'background': _label_dict(self.background),
            'unknown': [_label_dict(stat) for _, stat in sorted(self.unknown.items())],
        }


@dataclass(frozen=True)
class RegionMatch:
    """Best-overlap correspondence between two segmentation versions."""

    source_region: str
    target_region: str
    identical: bool
    source_voxels: int
    target_voxels: int
    overlap: int
    gained: int
    lost: int


@dataclass(frozen=True)
class RegionTransfer:
    """Voxels that changed from one category to another."""

    source_region: str
    target_region: str
    voxel_count: int
    volume_mm3: float | None = None

    @property
    def count(self) -> int:
        """Alias for :attr:`voxel_count`."""
        return self.voxel_count


@dataclass(frozen=True)
class SegmentationComparison:
    """Result of :func:`compare_segmentations`.

    ``same_regions`` maps source regions to target regions whose footprints
    are unchanged, even when labels or names were renumbered.  ``renumbered``
    records exact source-label to target-label renumberings.  ``transfers``
    lists every off-diagonal voxel transfer between categories.
    """

    same_regions: Mapping[str, str]
    renumbered: Mapping[int, int]
    matches: tuple[RegionMatch, ...]
    transfers: tuple[RegionTransfer, ...]
    contingency: Mapping[tuple[str, str], int]
    source_only: tuple[str, ...]
    target_only: tuple[str, ...]
    voxel_volume_mm3: float | None
    source_voxel_volume_mm3: float | None = None
    target_voxel_volume_mm3: float | None = None

    @property
    def changed_voxels(self) -> int:
        """Number of voxels that changed category."""
        return sum(transfer.voxel_count for transfer in self.transfers)

    @property
    def transfer_matrix(self) -> Mapping[tuple[str, str], int]:
        """Alias for :attr:`contingency`."""
        return self.contingency

    def to_dict(self) -> dict[str, Any]:
        return {
            'voxel_volume_mm3': self.voxel_volume_mm3,
            'source_voxel_volume_mm3': self.source_voxel_volume_mm3,
            'target_voxel_volume_mm3': self.target_voxel_volume_mm3,
            'changed_voxels': self.changed_voxels,
            'same_regions': dict(self.same_regions),
            'renumbered': {int(k): int(v) for k, v in self.renumbered.items()},
            'matches': [
                {
                    'source_region': m.source_region,
                    'target_region': m.target_region,
                    'identical': m.identical,
                    'source_voxels': int(m.source_voxels),
                    'target_voxels': int(m.target_voxels),
                    'overlap': int(m.overlap),
                    'gained': int(m.gained),
                    'lost': int(m.lost),
                }
                for m in self.matches
            ],
            'transfers': [
                {
                    'source_region': t.source_region,
                    'target_region': t.target_region,
                    'voxel_count': int(t.voxel_count),
                    'volume_mm3': t.volume_mm3,
                }
                for t in self.transfers
            ],
            'contingency': [
                {
                    'source_region': source,
                    'target_region': target,
                    'voxel_count': int(count),
                }
                for (source, target), count in sorted(self.contingency.items())
            ],
            'source_only': list(self.source_only),
            'target_only': list(self.target_only),
        }


def voxel_volume(affine) -> float:
    """Return the physical volume of one voxel in mm**3.

    The volume is ``abs(det(affine[:3, :3]))``.  This is valid for arbitrary
    combinations of non-uniform scaling, axis flips, rotations and shear.  A
    transform whose linear part is singular or non-finite cannot define a
    physical volume and raises :class:`DegenerateTransformError`.

    Parameters
    ----------
    affine : array-like
        A (4, 4) voxel-to-world affine, or its (3, 3) linear part.

    Returns
    -------
    volume : float
        Physical voxel volume in mm**3.
    """
    lin = np.asarray(affine, dtype=np.float64)
    if lin.shape == (4, 4):
        lin = lin[:3, :3]
    elif lin.shape != (3, 3):
        raise ValueError(f'Expected a (4, 4) or (3, 3) affine, got shape {lin.shape}')
    if not np.all(np.isfinite(lin)):
        raise DegenerateTransformError('Affine contains non-finite values')

    rank_tol = np.finfo(np.float64).eps * max(lin.shape) * np.linalg.norm(lin, ord=2)
    if np.linalg.matrix_rank(lin, tol=rank_tol) < 3:
        raise DegenerateTransformError('Affine is singular; physical voxel volume is undefined')
    return abs(float(np.linalg.det(lin)))


def count_nonzero_voxels(img):
    """
    Count number of non-zero voxels

    Parameters
    ----------
    img : ``SpatialImage``
        All voxels of the mask should be of value 1, background should have value 0.

    Returns
    -------
    count : int
        Number of non-zero voxels

    """
    return np.count_nonzero(img.dataobj)


def mask_volume(img):
    """Compute volume of mask image.

    Equivalent to "fslstats /path/file.nii -V"

    Parameters
    ----------
    img : ``SpatialImage``
        All voxels of the mask should be of value 1, background should have value 0.


    Returns
    -------
    volume : float
        Volume of mask expressed in mm3.

    Examples
    --------
    >>> import numpy as np
    >>> import nibabel as nb
    >>> mask_data = np.zeros((20, 20, 20), dtype='u1')
    >>> mask_data[5:15, 5:15, 5:15] = 1
    >>> nb.imagestats.mask_volume(nb.Nifti1Image(mask_data, np.eye(4)))
    1000.0
    """
    if not spatial_axes_first(img):
        raise ValueError('Cannot calculate voxel volume for image with unknown spatial axes')
    voxel_volume_mm3 = voxel_volume(img.affine)
    mask_volume_vx = count_nonzero_voxels(img)
    mask_volume_mm3 = mask_volume_vx * voxel_volume_mm3

    return float(mask_volume_mm3)


def _clean_key(key: str) -> str:
    return str(key).lstrip('\ufeff').strip().lower().replace('-', '_')


def _first_field(row: Mapping[str, Any], names: Sequence[str]):
    for name in names:
        if name in row and row[name] not in (None, ''):
            return row[name]
    return None


def _parse_table_text(text: str, source: str = 'label table') -> list[dict[str, str]]:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise LabelTableError(f'{source} is empty')
    dialect = csv.Sniffer().sniff(lines[0], delimiters=',;\t')
    reader = csv.DictReader(lines, dialect=dialect)
    return [
        {_clean_key(key): value for key, value in row.items() if key is not None} for row in reader
    ]


def _normalize_label_table(label_table: Any) -> dict[int, LabelEntry]:
    """Convert supported label-table forms into ``{label: LabelEntry}``."""
    if isinstance(label_table, (str, Path)):
        text = str(label_table)
        path = Path(text)
        if not path.exists() and ('\n' in text or ',' in text or '\t' in text or ';' in text):
            return _normalize_label_table(_parse_table_text(text, 'Inline label table'))
        if path.suffix.lower() == '.json':
            return _normalize_label_table(json.loads(path.read_text(encoding='utf-8')))
        if not path.exists():
            raise LabelTableError(f'Label table file does not exist: {path}')
        return _normalize_label_table(
            _parse_table_text(path.read_text(encoding='utf-8-sig'), str(path))
        )

    entries: dict[int, LabelEntry] = {}
    try:
        if isinstance(label_table, Mapping):
            for container in ('labels', 'label_table', 'entries'):
                if container in label_table:
                    return _normalize_label_table(label_table[container])
            if _looks_like_region_map(label_table):
                for region, labels in label_table.items():
                    for label in labels:
                        _add_entry(entries, int(label), (str(label), str(region)))
                return entries
            for key, value in label_table.items():
                label = int(key)
                _add_entry(entries, label, _entry_fields(value, label))
            if not entries:
                raise LabelTableError(
                    'Label table must describe at least one non-background label'
                )
            return entries

        if isinstance(label_table, Sequence) and not isinstance(label_table, (bytes, bytearray)):
            for item in label_table:
                if isinstance(item, LabelEntry):
                    _add_entry(entries, item.label, (item.name, item.region))
                elif isinstance(item, Mapping):
                    row = {_clean_key(key): value for key, value in item.items()}
                    label = _first_field(row, ('label', 'label_id', 'id', 'value'))
                    if label is None:
                        raise LabelTableError("Label table entry is missing a 'label' field")
                    name = _first_field(row, ('name', 'label_name', 'description'))
                    region = _first_field(row, ('region', 'business_region', 'area', 'part'))
                    label = int(label)
                    if name is None and region is None:
                        raise LabelTableError(f'Label table entry for label {label} is empty')
                    name = str(label) if name is None else str(name)
                    region = name if region is None else str(region)
                    _add_entry(entries, label, (name, region))
                else:
                    values = tuple(item)
                    if len(values) == 2:
                        _add_entry(entries, int(values[0]), (str(values[1]), str(values[1])))
                    elif len(values) == 3:
                        _add_entry(entries, int(values[0]), (str(values[1]), str(values[2])))
                    else:
                        raise LabelTableError(
                            f'Label table rows must have 2 or 3 values, got {len(values)}'
                        )
            if not entries:
                raise LabelTableError(
                    'Label table must describe at least one non-background label'
                )
            return entries
    except (TypeError, ValueError) as err:
        if isinstance(err, LabelTableError):
            raise
        raise LabelTableError(f'Could not parse label table: {err}') from err

    raise LabelTableError(
        'Label table must be a mapping, a sequence of entries, or a CSV/JSON path'
    )


def _looks_like_region_map(label_table: Mapping) -> bool:
    """True for ``{region: [label, ...]}`` tables rather than ``{label: ...}``."""
    if not label_table:
        return False
    if any(
        not isinstance(value, Sequence) or isinstance(value, str) for value in label_table.values()
    ):
        return False
    try:
        for key in label_table:
            int(key)
    except (TypeError, ValueError):
        return True
    return False


def _entry_fields(value, label: int) -> tuple[str, str]:
    if isinstance(value, str):
        return value, value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) == 1:
            return str(value[0]), str(value[0])
        if len(value) == 2:
            return str(value[0]), str(value[1])
    if isinstance(value, Mapping):
        name = value.get('name', value.get('label_name', str(label)))
        region = value.get('region', value.get('business_region', name))
        return str(name), str(region)
    return str(value), str(value)


def _add_entry(entries: dict[int, LabelEntry], label: int, fields: tuple[str, str]) -> None:
    name, region = (str(part) for part in fields)
    if not name or not region:
        raise LabelTableError(f'Label {label} must have a non-empty name and region')
    if region in RESERVED_CATEGORIES or region.startswith(UNKNOWN_PREFIX):
        raise LabelTableError(f'Region name {region!r} is reserved for a special category')
    if label == 0:
        # Background is always kept separate, even if the table describes it.
        return
    if label in entries:
        raise LabelTableError(f'Duplicate entry for label {label}')
    entries[label] = LabelEntry(label, name, region)


def _label_array(img) -> np.ndarray:
    if not spatial_axes_first(img):
        raise ValueError('Cannot calculate label statistics for image with unknown spatial axes')
    data = np.asanyarray(img.dataobj)
    if data.ndim < 3:
        raise ValueError('Label image must have at least three spatial dimensions')
    if data.ndim > 3:
        if any(size != 1 for size in data.shape[3:]):
            raise ValueError('Label image must be a single 3D volume')
        data = data.reshape(data.shape[:3])
    if data.dtype == bool:
        data = data.astype(np.uint8)
    elif not np.issubdtype(data.dtype, np.integer):
        if not np.all(np.isfinite(data)) or not np.array_equal(data, np.rint(data)):
            raise ValueError('Label image must contain integer-valued labels only')
        data = data.astype(np.int64)
    if data.size == 0:
        raise ValueError('Label image is empty')
    if np.any(data < 0):
        raise ValueError('Label values must be non-negative; label 0 is background')
    return data


def label_statistics(img, label_table, allow_undefined_volume=False) -> LabelStatistics:
    """Summarize a segmentation by source label and business region.

    Parameters
    ----------
    img : ``SpatialImage``
        Integer label image.  Label ``0`` is background.
    label_table : mapping, sequence, str or path-like
        Table mapping source labels to names and business regions.  Supported
        forms are:

        * ``{label: name}`` or ``{label: (name, region)}``
        * ``{label: {'name': ..., 'region': ...}}``
        * a sequence of ``(label, name, region)`` tuples or dictionaries
        * a CSV file with ``label``, optional ``name`` and ``region`` columns
        * a JSON file containing one of the above structures

        Several source labels may map to the same ``region``.  Labels present
        in the image but absent from the table are reported in ``unknown``.
        Background is always reported separately and is never merged into a
        business region.
    allow_undefined_volume : bool, optional
        If True, a degenerate affine yields label and region counts with all
        volumes set to ``None``.  By default a degenerate affine raises
        :class:`DegenerateTransformError`, refusing any physical volume result.

    Returns
    -------
    statistics : LabelStatistics
        Per-label counts and volumes, per-region aggregates, the explicit
        label-to-region mapping, and the unknown/background categories.

    Notes
    -----
    Voxel volumes come from the affine determinant, so non-uniform scaling,
    flips and shear are all handled.
    """
    entries = _normalize_label_table(label_table)
    data = _label_array(img)
    values, counts = np.unique(data, return_counts=True)
    count_map = {int(value): int(count) for value, count in zip(values, counts, strict=True)}

    try:
        vox_vol = voxel_volume(img.affine)
    except DegenerateTransformError:
        vox_vol = None
    if vox_vol is None and not allow_undefined_volume:
        raise DegenerateTransformError(
            'Affine is singular; refusing to report physical label volumes'
        )

    def _volume(count: int) -> float | None:
        return None if vox_vol is None else count * vox_vol

    labels: dict[int, LabelStat] = {}
    unknown: dict[int, LabelStat] = {}
    for label, count in sorted(count_map.items()):
        if label == 0:
            continue
        entry = entries.get(label)
        if entry is None:
            unknown[label] = LabelStat(
                label, str(label), UNKNOWN_PREFIX[:-1], count, _volume(count)
            )
        else:
            labels[label] = LabelStat(label, entry.name, entry.region, count, _volume(count))

    background_count = count_map.get(0, 0)
    background = LabelStat(0, BACKGROUND, BACKGROUND, background_count, _volume(background_count))

    region_label_counts: dict[str, dict[int, int]] = {}
    for label, stat in labels.items():
        region_label_counts.setdefault(stat.region, {})[label] = stat.voxel_count

    regions: dict[str, RegionStat] = {}
    for region, label_counts in sorted(region_label_counts.items()):
        label_counts = dict(sorted(label_counts.items()))
        count = sum(label_counts.values())
        regions[region] = RegionStat(
            region=region,
            voxel_count=count,
            volume_mm3=_volume(count),
            label_counts=label_counts,
            label_volumes={label: _volume(c) for label, c in label_counts.items()},
        )

    return LabelStatistics(
        labels=dict(sorted(labels.items())),
        regions=regions,
        unknown=dict(sorted(unknown.items())),
        background=background,
        label_to_region={label: entry.region for label, entry in sorted(entries.items())},
        voxel_volume_mm3=vox_vol,
    )


def _nearest_resample(source, source_affine, target_shape, target_affine):
    """Nearest-neighbour resample `source` into the target grid.

    Returns
    -------
    sampled : ndarray
        Values sampled from `source` on the target grid.
    target_outside : ndarray
        Target voxels whose nearest source position is outside `source`.
    source_unsampled : ndarray
        Source voxels that no target voxel samples.  These are dropped by
        resampling (for example due to a coarser target grid) and must be
        reported rather than silently discarded.
    """
    target_shape = tuple(int(size) for size in target_shape)
    if source.shape == target_shape and np.allclose(
        source_affine, target_affine, rtol=0, atol=1e-8
    ):
        return source, np.zeros(target_shape, dtype=bool), np.zeros(source.shape, dtype=bool)

    target_coords = np.indices(target_shape).reshape(3, -1)
    target_h = np.vstack([target_coords, np.ones(target_coords.shape[1])])
    continuous = (np.linalg.inv(source_affine) @ target_affine @ target_h)[:3]
    source_indices = np.floor(continuous + 0.5).astype(np.int64)
    inside = np.all(
        (source_indices >= 0) & (source_indices < np.asarray(source.shape)[:, None]), axis=0
    )

    sampled = np.zeros(target_coords.shape[1], dtype=source.dtype)
    sampled[inside] = source[
        source_indices[0, inside], source_indices[1, inside], source_indices[2, inside]
    ]

    source_unsampled = np.ones(source.shape, dtype=bool)
    source_unsampled[
        source_indices[0, inside], source_indices[1, inside], source_indices[2, inside]
    ] = False
    return sampled.reshape(target_shape), ~inside.reshape(target_shape), source_unsampled


def _region_codes(label_data, entries: Mapping[int, LabelEntry]):
    """Map labels to integer category codes and describe each code.

    Background is code ``0``, business regions are positive codes, and each
    unknown label gets its own negative code derived from its label value, so
    unknown categories stay distinct across both versions.
    """
    regions = sorted({entry.region for entry in entries.values()})
    region_code = {region: index + 1 for index, region in enumerate(regions)}
    code_names = {0: BACKGROUND}
    code_names.update({code: region for region, code in region_code.items()})

    codes = np.zeros(label_data.shape, dtype=np.int64)
    labels = np.unique(label_data)
    for label in labels:
        label = int(label)
        if label in entries:
            codes[label_data == label] = region_code[entries[label].region]

    for label in sorted(int(label) for label in labels):
        if label == 0 or label in entries:
            continue
        code = -1 - label
        if code == OUTSIDE_CODE:
            raise ValueError(f'Label {label} collides with the reserved outside code')
        codes[label_data == label] = code
        code_names[code] = f'{UNKNOWN_PREFIX}{label}'
    return codes, code_names


def _contingency(source_codes, target_codes, source_names, target_names):
    pairs, counts = np.unique(
        np.stack([source_codes.ravel(), target_codes.ravel()], axis=1),
        axis=0,
        return_counts=True,
    )
    contingency: dict[tuple[str, str], int] = {}
    for (source_code, target_code), count in zip(pairs, counts, strict=True):
        source_name = source_names.get(int(source_code), f'{UNKNOWN_PREFIX}{source_code}')
        target_name = target_names.get(int(target_code), f'{UNKNOWN_PREFIX}{target_code}')
        contingency[(source_name, target_name)] = int(count)
    return contingency


def compare_segmentations(source_img, target_img, source_table, target_table=None):
    """Compare two segmentation versions by business region.

    Regions are compared by their spatial footprint rather than by label
    number, so a region that was only renumbered is reported as the same
    region.  Voxels that changed category are reported as transfers with
    their counts and, when defined, physical volumes.

    Parameters
    ----------
    source_img, target_img : ``SpatialImage``
        Integer label images for the two versions.
    source_table : mapping, sequence, str or path-like
        Label table for `source_img`; see :func:`label_statistics`.
    target_table : mapping, sequence, str or path-like, optional
        Label table for `target_img`.  Defaults to `source_table`.

    Returns
    -------
    comparison : SegmentationComparison
        Region correspondences, label renumbering, contingency counts and
        per-region voxel transfers.

    Notes
    -----
    Alignment uses nearest-neighbour sampling only.  No continuous
    interpolation is used, so a category that does not exist in the source
    cannot be invented.  ``target_img`` defines the comparison grid; source
    voxels outside it are reported against the ``outside`` category.  A
    degenerate affine raises :class:`DegenerateTransformError`, because any
    physical volume conclusion would be meaningless.
    """
    source_entries = _normalize_label_table(source_table)
    target_entries = _normalize_label_table(source_table if target_table is None else target_table)
    source_data = _label_array(source_img)
    target_data = _label_array(target_img)

    source_vox_vol = voxel_volume(source_img.affine)
    target_vox_vol = voxel_volume(target_img.affine)

    source_codes, source_names = _region_codes(source_data, source_entries)
    target_codes, target_names = _region_codes(target_data, target_entries)

    sampled_codes, target_outside, source_unsampled = _nearest_resample(
        source_codes,
        source_img.affine,
        target_data.shape,
        target_img.affine,
    )
    sampled_codes = sampled_codes.copy()
    sampled_codes[target_outside] = OUTSIDE_CODE
    source_names[OUTSIDE_CODE] = OUTSIDE
    target_names[OUTSIDE_CODE] = OUTSIDE

    contingency = _contingency(sampled_codes, target_codes, source_names, target_names)

    if np.any(source_unsampled):
        unsampled_codes, unsampled_counts = np.unique(
            source_codes[source_unsampled], return_counts=True
        )
        for code, count in zip(unsampled_codes, unsampled_counts, strict=True):
            source_name = source_names.get(int(code), f'{UNKNOWN_PREFIX}{code}')
            key = (source_name, OUTSIDE)
            contingency[key] = contingency.get(key, 0) + int(count)

    source_totals = _totals(contingency, 0)
    target_totals = _totals(contingency, 1)

    same_regions = _same_regions(contingency, source_totals, target_totals)
    matches = _matches(contingency, source_totals, target_totals)
    transfers = _transfers(contingency, target_vox_vol, source_vox_vol)
    renumbered = _renumbered(
        source_data,
        target_data,
        source_entries,
        target_entries,
        source_img.affine,
        target_img.affine,
    )

    real_source = {
        name for name in source_totals if _is_business_name(name) and source_totals[name] > 0
    }
    real_target = {
        name for name in target_totals if _is_business_name(name) and target_totals[name] > 0
    }
    overlapping_source = {
        source
        for (source, target), count in contingency.items()
        if count > 0 and source in real_source and target in real_target
    }
    overlapping_target = {
        target
        for (source, target), count in contingency.items()
        if count > 0 and source in real_source and target in real_target
    }

    return SegmentationComparison(
        same_regions=dict(sorted(same_regions.items())),
        renumbered=dict(sorted(renumbered.items())),
        matches=tuple(matches),
        transfers=tuple(transfers),
        contingency=dict(sorted(contingency.items())),
        source_only=tuple(sorted(real_source - overlapping_source)),
        target_only=tuple(sorted(real_target - overlapping_target)),
        voxel_volume_mm3=target_vox_vol,
        source_voxel_volume_mm3=source_vox_vol,
        target_voxel_volume_mm3=target_vox_vol,
    )


def _totals(contingency, index):
    totals: dict[str, int] = {}
    for pair, count in contingency.items():
        totals[pair[index]] = totals.get(pair[index], 0) + count
    return totals


def _is_business_name(name: str) -> bool:
    return name not in {BACKGROUND, OUTSIDE} and not name.startswith(UNKNOWN_PREFIX)


def _same_regions(contingency, source_totals, target_totals):
    forward: dict[str, dict[str, int]] = {}
    backward: dict[str, dict[str, int]] = {}
    for (source, target), count in contingency.items():
        if count <= 0 or not _is_business_name(source) or not _is_business_name(target):
            continue
        forward.setdefault(source, {})[target] = count
        backward.setdefault(target, {})[source] = count

    same: dict[str, str] = {}
    for source, targets in forward.items():
        if len(targets) != 1:
            continue
        target, overlap = next(iter(targets.items()))
        if len(backward.get(target, {})) != 1:
            continue
        if overlap == source_totals[source] == target_totals[target]:
            same[source] = target
    return same


def _matches(contingency, source_totals, target_totals):
    forward: dict[str, dict[str, int]] = {}
    for (source, target), count in contingency.items():
        if count <= 0 or not _is_business_name(source):
            continue
        forward.setdefault(source, {})[target] = count

    matches = []
    for source, targets in sorted(forward.items()):
        target, overlap = max(targets.items(), key=lambda item: (item[1], item[0]))
        source_total = source_totals[source]
        target_total = target_totals.get(target, 0)
        matches.append(
            RegionMatch(
                source_region=source,
                target_region=target,
                identical=overlap == source_total == target_total and target != OUTSIDE,
                source_voxels=source_total,
                target_voxels=target_total,
                overlap=overlap,
                gained=max(target_total - overlap, 0),
                lost=max(source_total - overlap, 0),
            )
        )
    return matches


def _transfers(contingency, target_vox_vol, source_vox_vol):
    transfers = []
    for (source, target), count in sorted(contingency.items()):
        if count <= 0 or source == target:
            continue
        if target == OUTSIDE:
            volume = None if source_vox_vol is None else count * source_vox_vol
        else:
            volume = None if target_vox_vol is None else count * target_vox_vol
        transfers.append(RegionTransfer(source, target, count, volume))
    return transfers


def _renumbered(
    source_data,
    target_data,
    source_entries,
    target_entries,
    source_affine,
    target_affine,
):
    sampled, target_outside, _ = _nearest_resample(
        source_data, source_affine, target_data.shape, target_affine
    )
    sampled = sampled.copy()
    sampled[target_outside] = 0

    source_masks = {}
    for label in source_entries:
        mask = sampled == label
        if np.any(mask):
            source_masks[label] = mask

    target_masks = {}
    for label in target_entries:
        mask = target_data == label
        if np.any(mask):
            target_masks[label] = mask

    renumbered = {}
    for source_label, source_mask in sorted(source_masks.items()):
        candidates = [
            target_label
            for target_label, target_mask in target_masks.items()
            if np.array_equal(source_mask, target_mask)
        ]
        if len(candidates) == 1:
            renumbered[source_label] = candidates[0]
    return renumbered


def write_label_statistics(statistics: LabelStatistics, path) -> None:
    """Write label statistics to a JSON file or CSV table.

    Parameters
    ----------
    statistics : LabelStatistics
        Result of :func:`label_statistics`.
    path : str or path-like
        ``.json`` writes the full nested result; any other suffix writes a
        CSV with one row per source label, unknown label and background.
    """
    path = Path(path)
    if path.suffix.lower() == '.json':
        path.write_text(json.dumps(statistics.to_dict(), indent=2) + '\n', encoding='utf-8')
        return

    rows = [
        {
            'label': 0,
            'name': BACKGROUND,
            'region': BACKGROUND,
            'voxel_count': statistics.background.voxel_count,
            'volume_mm3': statistics.background.volume_mm3,
        }
    ]
    rows.extend(
        {
            'label': stat.label,
            'name': stat.name,
            'region': stat.region,
            'voxel_count': stat.voxel_count,
            'volume_mm3': stat.volume_mm3,
        }
        for _, stat in sorted(statistics.labels.items())
    )
    rows.extend(
        {
            'label': stat.label,
            'name': stat.name,
            'region': UNKNOWN_PREFIX[:-1],
            'voxel_count': stat.voxel_count,
            'volume_mm3': stat.volume_mm3,
        }
        for _, stat in sorted(statistics.unknown.items())
    )

    with path.open('w', newline='', encoding='utf-8') as fileobj:
        writer = csv.DictWriter(
            fileobj, fieldnames=('label', 'name', 'region', 'voxel_count', 'volume_mm3')
        )
        writer.writeheader()
        writer.writerows(rows)


summarize_labels = label_statistics
compare_label_maps = compare_segmentations
