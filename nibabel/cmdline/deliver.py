# emacs: -*- mode: python-mode; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the NiBabel package for the
#   copyright and license terms.
#
### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Plan or write a lossless NIfTI delivery for a target site."""

import argparse
import json
import sys

import nibabel as nib
from nibabel.delivery import TargetProfile, plan_delivery, verify_round_trip, write_delivery


def _get_parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('infile', help='Source NIfTI image (read-only)')
    p.add_argument(
        'outfile',
        nargs='?',
        help='Destination file.  Omit to dry-run and print the plan.',
    )
    p.add_argument(
        '--target',
        help='JSON file describing the target profile.  Omit to accept every '
        'NIfTI version, data type and header scaling.',
    )
    p.add_argument(
        '--nifti-version',
        dest='versions',
        action='append',
        choices=('nifti1', 'nifti2'),
        help='Allowed NIfTI version (repeatable).',
    )
    p.add_argument(
        '--dtype',
        dest='dtypes',
        action='append',
        help='Allowed on-disk data type (repeatable).',
    )
    p.add_argument(
        '--scaling',
        dest='scaling',
        action='append',
        choices=('header', 'none'),
        help="Allowed scaling representation (repeatable); 'none' means a bare "
        'slope of 1 and intercept of 0.',
    )
    p.add_argument('--max-dim', type=int, help='Largest allowed dim[] entry.')
    p.add_argument(
        '--vox-offset-alignment',
        type=int,
        help='Required multiple for vox_offset.',
    )
    p.add_argument(
        '--json',
        action='store_true',
        help='Print the plan or verification report as JSON.',
    )
    p.add_argument('-V', '--version', action='version', version=f'%(prog)s {nib.__version__}')
    return p


def _load_target(args):
    if args.target:
        with open(args.target) as fobj:
            return TargetProfile.from_json(json.load(fobj))
    kwargs = {}
    if args.versions:
        kwargs['versions'] = tuple(args.versions)
    if args.dtypes:
        kwargs['dtypes'] = tuple(args.dtypes)
    if args.scaling:
        kwargs['scaling'] = tuple(args.scaling)
    if args.max_dim is not None:
        kwargs['max_dim'] = args.max_dim
    if args.vox_offset_alignment is not None:
        kwargs['vox_offset_alignment'] = args.vox_offset_alignment
    return TargetProfile(**kwargs)


def main(args=None):
    parser = _get_parser()
    opts = parser.parse_args(args=args)
    target = _load_target(opts)
    img = nib.load(opts.infile)
    plan = plan_delivery(img, target)

    if not plan.ok:
        report = plan.as_dict()
        if opts.json:
            print(json.dumps(report, indent=2, default=str))
        else:
            print('No lossless delivery satisfies the target:', file=sys.stderr)
            for blocker in plan.blockers:
                print(f'  - {blocker["field"]}: {blocker["detail"]}', file=sys.stderr)
        return 1

    verification = verify_round_trip(plan)
    report = plan.as_dict()
    report['verification'] = verification

    if opts.outfile:
        write_delivery(plan, opts.outfile)

    if opts.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        encoding = plan.encoding
        print(f'version: {encoding.version}')
        print(f'dtype: {encoding.dtype}')
        print(f'slope: {encoding.slope}, inter: {encoding.inter}')
        print(f'payload bytes: {encoding.size}')
        for migration in plan.migrations:
            print(
                f'{migration.field}: {migration.original!r} -> '
                f'{migration.delivered!r} ({migration.status})'
            )
        if opts.outfile:
            print(f'wrote: {opts.outfile}')
        print(f'round trip: {"exact" if verification["ok"] else "MISMATCH"}')
    return 0 if verification['ok'] else 2
