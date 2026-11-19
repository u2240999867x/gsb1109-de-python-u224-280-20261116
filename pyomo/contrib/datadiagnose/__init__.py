# ____________________________________________________________________________________
#
# Pyomo: Python Optimization Modeling Objects
# Copyright (c) 2008-2026 National Technology and Engineering Solutions of Sandia, LLC
# Under the terms of Contract DE-NA0003525 with National Technology and Engineering
# Solutions of Sandia, LLC, the U.S. Government retains certain rights in this
# software.  This software is distributed under the 3-clause BSD License.
# ____________________________________________________________________________________

"""Offline diagnosis of minimal conflicting data-file line groups.

This package finds minimal groups of data-file declarations that violate
a Pyomo model's validation rules during instance construction, without
ever solving the optimization model.
"""

from pyomo.contrib.datadiagnose.diagnose import (
    ConflictGroup,
    DataDiagnoseError,
    DataLine,
    DiagnosisResult,
    find_conflicting_lines,
    find_conflicts_in_text,
    scan_data_units,
)

__all__ = [
    "ConflictGroup",
    "DataDiagnoseError",
    "DataLine",
    "DiagnosisResult",
    "find_conflicting_lines",
    "find_conflicts_in_text",
    "scan_data_units",
]
