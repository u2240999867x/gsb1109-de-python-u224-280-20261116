Data Conflict Diagnosis
!!!!!!!!!!!!!!!!!!!!!!!

When individual data-file declarations are each valid on their own but
their combination violates a model's validation rules, the
:mod:`pyomo.contrib.datadiagnose` package locates *minimal* groups of
conflicting declarations without ever solving the optimization model.

Every diagnostic probe only constructs a fresh concrete instance from a
data subset and records whether construction raises.  Because
:meth:`~pyomo.environ.AbstractModel.create_instance` clones the abstract
model before loading data, defaults, set memberships and constructed
components from one probe never carry over into the next one.

A conflict group is reported as proven minimal only when a deletion
filter has confirmed that removing any single line from the group no
longer reproduces the same validation error.  If the check budget is
exhausted first, the group is still reported as a candidate that
reproduces the error, but minimality is not claimed.

Programmatic Entry
==================

.. automodule:: pyomo.contrib.datadiagnose
   :noindex:

.. autofunction:: pyomo.contrib.datadiagnose.find_conflicting_lines
   :noindex:

.. autofunction:: pyomo.contrib.datadiagnose.find_conflicts_in_text
   :noindex:

.. autoclass:: pyomo.contrib.datadiagnose.DiagnosisResult
   :noindex:

.. autoclass:: pyomo.contrib.datadiagnose.ConflictGroup
   :noindex:

.. autoclass:: pyomo.contrib.datadiagnose.DataLine
   :noindex:

Command Line
============

Given a Python script defining an unconstructed model in a module-level
variable named ``model`` and a Pyomo data file::

    python -m pyomo.contrib.datadiagnose model.py data.dat

Use ``--budget N`` to cap the number of fresh-instance construction
checks; groups that cannot be proven minimal within the budget are
labeled ``CANDIDATE`` instead of ``PROVEN MINIMAL``.  The command exits
with status 0 when no conflicts are found, 1 when proven or candidate
conflicts are reported, and 2 when the budget is exhausted.

Reported line locations always refer to the original input file,
including multi-line declarations and the namespace containing each
line.
