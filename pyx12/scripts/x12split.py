#!/usr/bin/env python

"""
Split oversized 837 transaction sets into smaller valid ones.
If no output filename is given with -o, write to stdout.
"""

import argparse
import logging
import sys

import pyx12
import pyx12.x12split

__author__ = pyx12.__author__
__status__ = pyx12.__status__
__version__ = pyx12.__version__
__date__ = pyx12.__date__


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser. Exposed so docs/conf.py can render --help."""
    parser = argparse.ArgumentParser(
        prog="x12split",
        description="Split oversized 837 transaction sets into smaller valid ones",
    )
    parser.add_argument("--verbose", "-v", action="count", default=0)
    parser.add_argument("--quiet", "-q", action="store_true")
    parser.add_argument("--debug", "-d", action="store_true")
    parser.add_argument(
        "--max-segments",
        "-m",
        type=int,
        required=True,
        help="Maximum segments per 837 transaction set, counting ST through SE",
    )
    parser.add_argument(
        "--output",
        "-o",
        action="store",
        dest="outputfile",
        default=None,
        help="Output filename. Defaults to stdout",
    )
    parser.add_argument("--version", action="version", version=f"{parser.prog} {__version__}")
    parser.add_argument("input_file")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    logger = logging.getLogger()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stdout_hdlr = logging.StreamHandler()
    stdout_hdlr.setFormatter(formatter)
    logger.addHandler(stdout_hdlr)
    logger.setLevel(logging.INFO)
    if args.debug:
        logger.setLevel(logging.DEBUG)
    if args.verbose > 0:
        logger.setLevel(logging.DEBUG)
    if args.quiet:
        logger.setLevel(logging.ERROR)

    with open(args.input_file, encoding="latin-1") as fd_in:
        if args.outputfile:
            with open(args.outputfile, "w", encoding="latin-1") as fd_out:
                warnings = pyx12.x12split.split_document(fd_in, fd_out, args.max_segments)
        else:
            warnings = pyx12.x12split.split_document(fd_in, sys.stdout, args.max_segments)
    for warning in warnings:
        logger.warning(warning)
    return 0


if __name__ == "__main__":
    sys.exit(main())
