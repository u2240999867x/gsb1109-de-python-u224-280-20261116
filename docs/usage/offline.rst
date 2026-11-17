.. py:currentmodule:: pycomm3

==============================
Offline Access Planning
==============================

Sometimes the tag requests need to be reviewed before anything is sent to the
controller.  :class:`OfflineAccessPlanner` builds an access plan from a *locked*
tag catalog (the dict produced by a tag list upload or the
:attr:`~LogixDriver.tags_json` export) without opening any connection.  Every
segment of a tag reference is explained and rendered into the exact CIP request
path the :class:`~LogixDriver` would place on the wire.

A plan is only marked **resolved** when the locked catalog can prove the whole
reference.  Unknown tags, unknown members, bad subscripts or a length that would
cross a member boundary produce an **unresolved** plan with no request bytes,
never a guessed request.

>>> catalog = plc.tags_json  # or load_tag_catalog('tags.json')
>>> planner = OfflineAccessPlanner(catalog, use_instance_ids=True)
>>> plan = planner.plan('Program:pycomm3.read_prog_dint_2d_ary1[0,0]{3}')
>>> plan.resolved
True
>>> plan.display_name
'Program:pycomm3.read_prog_dint_2d_ary1[0,0]{3}'
>>> plan.symbol_identity['scope']
'program:pycomm3'
>>> plan.elements, plan.element_size, plan.request_length
(3, 4, 12)
>>> plan.request_path_hex  # doctest: +SKIP
'17 91 0f ...'

Display names and symbol identities
====================================

The user supplied text is always retained unchanged in
:attr:`~OfflinePlan.display_name`.  The catalog identity (scope, catalog key,
instance id, symbol addresses) is kept separately in
:attr:`~OfflinePlan.symbol_identity`, so a display alias can never be mistaken
for the symbol actually addressed on the wire.

Program scope is resolved only against that program's catalog entries.  A member
that exists on a same-named type in another program is never used to back-fill a
missing member:

>>> planner.plan('Program:A.foo.missing_member').resolved
False

Arrays, subscripts and request length
=====================================

The number of subscripts must match the tag's declared dimensionality and every
subscript must stay inside its dimension.  Multi-dimensional indices are
converted to the controller's linear element order, shown in
:attr:`~OfflinePlan.boundary`.

The requested element count (``{n}`` brace syntax or the ``elements`` argument)
is validated together with the subscripts:

* a base-array request may not run past the elements remaining after the
  addressed element;
* a structure-member array may additionally not cross into the next member or
  past the end of the enclosing structure.

A rejected request reports the offending boundary and carries no request path:

>>> plan = planner.plan('write_udt2.sints[5]{4}')
>>> plan.resolved
False
>>> plan.request_path is None
True
>>> any('cross-member reads are not permitted' in r for r in plan.reasons)
True

Explained, checkable paths
==========================

:attr:`~OfflinePlan.segments` explains each wire step (program scope, base
symbol, structure member, array subscript or bit).  Three encoded forms are
provided for verification:

* :attr:`~OfflinePlan.request_path` - the effective path given the instance-id
  policy;
* :attr:`~OfflinePlan.symbolic_request_path` - always the symbolic path;
* :attr:`~OfflinePlan.instance_request_path` - the instance-id path, or
  ``None`` for program-scoped tags, which are always symbolic.

Plans are produced for every input via :meth:`~OfflineAccessPlanner.plan_all`,
preserving order so resolved and unresolved references can be audited together.
