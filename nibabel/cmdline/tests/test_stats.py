# emacs: -*- mode: python-mode; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the NiBabel package for the
#   copyright and license terms.
#
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##

import json

import numpy as np
import pytest

from nibabel import Nifti1Image
from nibabel.cmdline.stats import main
from nibabel.loadsave import save


def test_volume(tmpdir, capsys):
    mask_data = np.zeros((20, 20, 20), dtype='u1')
    mask_data[5:15, 5:15, 5:15] = 1
    img = Nifti1Image(mask_data, np.eye(4))

    infile = tmpdir / 'input.nii'
    save(img, infile)

    args = f'{infile} --Volume'
    main(args.split())
    vol_mm3 = capsys.readouterr()
    args = f'{infile} --Volume --units vox'
    main(args.split())
    vol_vox = capsys.readouterr()

    assert float(vol_mm3[0]) == 1000.0
    assert int(vol_vox[0]) == 1000


def test_label_statistics_cli(tmpdir, capsys):
    data = np.zeros((4, 4, 4), dtype=np.int16)
    data[0:2] = 1
    data[2:3] = 2
    img = Nifti1Image(data, np.diag([2.0, 3.0, 4.0, 1.0]))
    infile = tmpdir / 'labels.nii'
    save(img, infile)

    table = tmpdir / 'labels.csv'
    table.write_text('label,name,region\n1,left,brain\n2,right,brain\n', encoding='utf-8')

    assert main([str(infile), '--label-table', str(table)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['regions'][0]['region'] == 'brain'
    assert output['regions'][0]['voxel_count'] == 48
    assert output['regions'][0]['volume_mm3'] == pytest.approx(48 * 24.0)
    assert output['background']['voxel_count'] == 16

    out_path = tmpdir / 'stats.json'
    assert main([str(infile), '--label-table', str(table), '--output', str(out_path)]) == 0
    assert json.loads(out_path.read_text(encoding='utf-8')) == output
