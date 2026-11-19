# ____________________________________________________________________________________
#
# Pyomo: Python Optimization Modeling Objects
# Copyright (c) 2008-2026 National Technology and Engineering Solutions of Sandia, LLC
# Under the terms of Contract DE-NA0003525 with National Technology and Engineering
# Solutions of Sandia, LLC, the U.S. Government retains certain rights in this
# software.  This software is distributed under the 3-clause BSD License.
# ____________________________________________________________________________________

"""Command-line entry point for offline data conflict diagnosis.

Usage::

    python -m pyomo.contrib.datadiagnose MODEL.py DATA.dat [--budget N]

``MODEL.py`` is a Python script defining an unconstructed Pyomo model in
a module-level variable named ``model`` (the first AbstractModel found in
the script is used if that name is absent).  No solver is required and
no optimization is performed.
"""

import argparse
import runpy
import sys

from pyomo.environ import AbstractModel
from pyomo.contrib.datadiagnose.diagnose import (
    DataDiagnoseError,
    find_conflicting_lines,
)


def _load_model(model_file: str) -> AbstractModel:
    namespace = runpy.run_path(model_file)
    if "model" in namespace and isinstance(namespace["model"], AbstractModel):
        return namespace["model"]
    for value in namespace.values():
        if isinstance(value, AbstractModel):
            return value
    raise DataDiagnoseError(
        f"could not find an AbstractModel in {model_file}; define the model "
        "in a module-level variable named 'model'"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pyomo.contrib.datadiagnose",
        description=(
            "Find minimal groups of data-file lines that violate model "
            "validation rules, without solving the optimization model."
        ),
    )
    parser.add_argument("model_file", help="Python file defining an AbstractModel")
    parser.add_argument("data_file", help="Pyomo DAT file to diagnose")
    parser.add_argument(
        "--budget",
        type=int,
        default=None,
        metavar="N",
        help=(
            "maximum number of fresh-instance construction checks; when "
            "exhausted, unproven groups are reported as candidates"
        ),
    )
    parser.add_argument(
        "--namespace",
        action="append",
        dest="namespaces",
        default=None,
        metavar="NAME",
        help=(
            "data namespace to diagnose; may be repeated.  By default "
            "every namespace in the file is selected"
        ),
    )
    args = parser.parse_args(argv)

    try:
        model = _load_model(args.model_file)
        result = find_conflicting_lines(
            model, args.data_file, budget=args.budget, namespaces=args.namespaces
        )
    except (DataDiagnoseError, FileNotFoundError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 3
    sys.stdout.write(result.write_report())

    if result.exhausted:
        return 2
    return 1 if result.groups else 0


if __name__ == "__main__":
    raise SystemExit(main())
