"""Tests for the nitool timeline subcommand."""
import json
from os import path as opath

import numpy as np
import nibabel as nb

from dcmstack import nitool_cli
from dcmstack.nifti_wrapper import NiftiWrapper


AFFINE = np.diag([2.0, 2.0, 3.0, 1.0])


def _write_batch(path, shape=(3, 3, 2, 2), offset=0, affine=AFFINE):
    data = (np.arange(np.prod(shape), dtype=np.int16).reshape(shape) + offset)
    nii = NiftiWrapper(nb.Nifti1Image(data, np.array(affine, dtype=float)),
                       make_empty=True)
    nii.nii_img.header.set_dim_info(0, 1, 2)
    nii.to_filename(path)


def test_timeline_command(tmp_path):
    first = str(tmp_path / "first.nii.gz")
    second = str(tmp_path / "second.nii.gz")
    _write_batch(first)
    _write_batch(second, offset=1000)
    spec_path = tmp_path / "batches.json"
    spec_path.write_text(json.dumps([
        {"path": first, "acquisition_id": "scan-a", "coverage": [0, 4]},
        {"path": second, "acquisition_id": "scan-b", "coverage": [6, 10]},
    ]))
    out = str(tmp_path / "timeline.nii.gz")
    report = str(tmp_path / "report.json")

    rc = nitool_cli.main([
        "nitool", "timeline", str(spec_path), out, "--report", report,
    ])
    assert rc == 0
    merged = NiftiWrapper.from_filename(out)
    assert merged.nii_img.shape == (3, 3, 2, 4)
    with open(report) as handle:
        record = json.load(handle)
    assert [b["acquisition_id"] for b in record["batches"]] == ["scan-a", "scan-b"]
    assert record["coverage"]["gaps"]


def test_timeline_command_refuses_shift(tmp_path):
    first = str(tmp_path / "first.nii.gz")
    second = str(tmp_path / "second.nii.gz")
    _write_batch(first)
    moved = AFFINE.copy()
    moved[:3, 3] += [0.0, 0.0, 7.0]
    _write_batch(second, offset=1000, affine=moved)
    spec_path = tmp_path / "batches.json"
    spec_path.write_text(json.dumps([
        {"path": first, "acquisition_id": "scan-a", "coverage": [0, 4]},
        {"path": second, "acquisition_id": "scan-b", "coverage": [6, 10]},
    ]))
    out = str(tmp_path / "timeline.nii.gz")
    rc = nitool_cli.main(["nitool", "timeline", str(spec_path), out])
    assert rc == 2
    assert not opath.exists(out)
