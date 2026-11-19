=============================
Schedule audit toolkit
=============================

.. contents:: Table of Contents

The :mod:`schedule_audit` package audits a recorded trace of control
events, data reads/writes and rate-limit waits.  It answers two
questions for every *material* (a chain of events whose completion
must be checked):

#. what true-time completion window is compatible with the recorded
   clock readings, given their uncertainty;
#. which timeouts can actually be attributed to throttling, once the
   waits that are allowed to be removed are taken out.

The toolkit is deliberately conservative: it never assumes that
removing a throttle wait makes network transfers or predecessor work
finish immediately, and it never deducts the same waiting interval
twice.

Inputs
======

A trace is built from :class:`~schedule_audit.Material` objects, each
holding an ordered list of :class:`~schedule_audit.Event` objects,
plus a dictionary of shared :class:`~schedule_audit.Resource` objects.

- ``clock_skew``: every observed timestamp is treated as a reading
  with an absolute error of at most ``clock_skew``, so each true start
  and end lies in a window of that half width.
- event durations are ranges ``[duration_min, duration_max]``; for IO
  events these are intrinsic network durations and are never shortened
  by the counterfactual run.
- wait events declare every reason they had (throttle, network,
  dependency, resource contention, other).  A wait is only removable
  when it carries the throttle reason and is explicitly flagged
  ``removable_throttle=True``; the trace's ``removed_waits`` set says
  which of those permissions are actually exercised.
- IO events carry a :class:`~schedule_audit.Transfer` with wire bytes,
  payload bytes and business-level retransmit bytes, which are always
  accounted separately.

What is computed
================

Difference constraints
----------------------

Each scenario is a system of difference constraints:

- clock readings bound every true start and end;
- duration ranges bound ``end - start``;
- events chained in a material satisfy ``start(next) >= end(prev)``;
- events sharing a resource cannot overlap.

The recorded trace does not state which order shared-resource events
took, so every weak ordering compatible with the clock windows is
enumerated (a resource with capacity ``c`` is modelled as ``c`` lanes)
and solved separately.  Bounds reported for a material are the union of
the bounds of every feasible ordering, so they hold under *every*
schedule compatible with the evidence rather than one guessed
timeline.

Baseline and counterfactual
---------------------------

Two scenarios are solved:

- the **baseline** keeps every wait and every clock window;
- the **counterfactual** removes the allowed throttle waits.

A pure throttle wait disappears entirely.  A wait with additional
reasons keeps the range ``[0, duration_max]``: because the causes
overlapped, the remaining reasons may still need the whole interval,
or none of it once throttling no longer gates them.  This is what
prevents double deduction.

Removing a wait necessarily lets downstream events happen earlier
than the clock recorded, so the counterfactual reports two envelopes:

- the **anchored envelope**, which keeps the clock windows and backs
  provable statements;
- the **relaxed envelope**, which only floors the affected suffix
  events at their material release and backs *possible* statements
  (maximum achievable reduction, escaping a timeout).

Evidence levels
===============

Per material, the report separates:

- ``improvement_proven``: a positive **guaranteed reduction** computed
  on the critical path while mixed-cause waits keep their full
  duration and minimized over every feasible resource ordering;
- ``improvement_possible``: the relaxed envelope shows some schedule
  finishing earlier, but no reduction survives every schedule;
- ``no_improvement_possible``: no supported schedule improves.

For timeouts the verdict is one of:

- ``timeout_proven`` / ``within_deadline_proven`` / ``timeout_possible``
  for the baseline completion window;
- ``timeout_remains_in_every_schedule`` when even the earliest
  counterfactual completion stays past the latest possible deadline,
  which rules out throttling as the cause;
- ``relief_possible_not_proven`` when some no-throttle schedule meets
  the deadline but the relaxed suffix has no provable upper bound.

Byte accounting
===============

Wire bytes, effective payload bytes and business retransmit bytes are
summed independently per IO event and in total.  Retransmissions are
part of the wire total but are never folded into the payload figure,
and ``payload + retransmit`` may not exceed the wire total.

Usage
=====

.. code-block:: python

    from schedule_audit import (
        EVENT_IO, EVENT_WAIT, EVENT_WORK, Event, Material,
        REASON_THROTTLE, Resource, Trace, Transfer,
        audit_trace, report_as_text,
    )

    worker = Resource("worker", capacity=1)
    material = Material(
        name="upload",
        deadline=8.5,
        events=[
            Event("control", EVENT_WORK, 0.0, 2.0, 2.0, 2.0,
                  resources=("worker",)),
            Event("throttle", EVENT_WAIT, 2.0, 5.0, 3.0, 3.0,
                  reasons=(REASON_THROTTLE,),
                  removable_throttle=True),
            Event("data", EVENT_IO, 5.0, 9.0, 4.0, 4.0,
                  resources=("worker",),
                  transfer=Transfer(100, 90, 5)),
        ],
    )
    trace = Trace(
        materials=[material],
        resources={"worker": worker},
        clock_skew=0.1,
        removed_waits=frozenset(("throttle",)),
    )
    print(report_as_text(audit_trace(trace)))

A runnable example with two materials, a mixed-cause wait and separate
byte accounts lives in ``demo/schedule_audit_demo.py``.

Tests
=====

The toolkit has its own test directory so that the FTP integration
fixtures are not required:

.. code-block:: bash

    python -m pytest tests_schedule_audit/ -o addopts=""

The ``-o addopts=""`` override is only needed when the optional
pytest plugins referenced by the project configuration are not
installed.
