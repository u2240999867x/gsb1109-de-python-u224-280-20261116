# ____________________________________________________________________________________
#
# Pyomo: Python Optimization Modeling Objects
# Copyright (c) 2008-2026 National Technology and Engineering Solutions of Sandia, LLC
# Under the terms of Contract DE-NA0003525 with National Technology and Engineering
# Solutions of Sandia, LLC, the U.S. Government retains certain rights in this
# software.  This software is distributed under the 3-clause BSD License.
# ____________________________________________________________________________________

import pyomo.environ as pyo

model = pyo.ConcreteModel()
model.year = pyo.Set(initialize=[2020, 2021, 2022, 2024])
model.site = pyo.Set(initialize=['A', 'B'])

# Sparse observations: only a few (year, site) combinations were measured
model.observed = pyo.Param(
    model.year, model.site, initialize={(2020, 'A'): 1.2, (2022, 'B'): 3.4}
)

# @declaration
# The supplier's claim: everything in 2020-2022, except no 2021 A
coverage = pyo.CoverageDeclaration(
    model.observed.index_set(),
    ranges={0: (2020, 2022)},
    exclude=[(2021, 'A')],
)
assert (2020, 'A') in coverage
assert (2021, 'A') not in coverage
assert (2024, 'A') not in coverage
# @declaration

model.year_with_default = pyo.Set(initialize=[2020, 2021, 2022])
model.measured = pyo.Param(
    model.year_with_default,
    model.site,
    initialize={(2020, 'A'): 1.2, (2022, 'B'): 3.4},
    default=0.0,
)

# @domain
domain = pyo.ObservationDomain(
    model.measured.index_set(), declarations={model.measured: coverage}
)
kind, value = domain.observation(model.measured, (2020, 'A'))
assert kind is pyo.ObservationKind.ORIGINAL
kind, value = domain.observation(model.measured, (2020, 'B'))
assert kind is pyo.ObservationKind.DEFAULT
# @domain

model.unmeasured = pyo.Param(model.year_with_default, initialize={2020: 1.0})
# @missing
kind, value = domain.observation(model.unmeasured, 2021)
assert kind is pyo.ObservationKind.MISSING
assert value is None  # a missing observation is not zero
# @missing

# @migration
model.revised_year = pyo.Set(initialize=[2020, 2021, 2022, 2023])
model.revised = model.revised_year * model.site
result = coverage.migrate(model.revised)
# The old covered members are carried over by identity ...
assert (2020, 'B') in result.migrated
# ... but the year 2023 added by the revision is not covered.
assert (2023, 'A') in result.added
assert (2023, 'A') not in result.declaration
# @migration
