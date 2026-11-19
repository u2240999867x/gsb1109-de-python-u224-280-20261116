# ____________________________________________________________________________________
#
# Pyomo: Python Optimization Modeling Objects
# Copyright (c) 2008-2026 National Technology and Engineering Solutions of Sandia, LLC
# Under the terms of Contract DE-NA0003525 with National Technology and Engineering
# Solutions of Sandia, LLC, the U.S. Government retains certain rights in this
# software.  This software is distributed under the 3-clause BSD License.
# ____________________________________________________________________________________

from pyomo.common.errors import PyomoException


class ScenarioError(PyomoException):
    """Base class for errors raised while resolving scenario data."""


class ScenarioCycleError(ScenarioError):
    """The scenario inheritance graph contains a cycle."""


class ScenarioConflictError(ScenarioError):
    """Multiple equally-valid layers provide different values for a key."""


class ScenarioLoadError(ScenarioError):
    """A scenario could not be materialized."""

    def __init__(self, scenario, cause):
        self.scenario = scenario
        self.cause = cause
        super().__init__(f"Failed to load scenario '{scenario}': {cause}")
