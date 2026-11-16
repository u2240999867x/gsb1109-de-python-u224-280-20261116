Introspection Comparison
========================

.. automodule:: dbus_next.introspection_diff
    :members:
    :undoc-members:

.. autoclass:: dbus_next.introspection_diff.ColumnAction
    :members:
    :undoc-members:

.. autoclass:: dbus_next.introspection_diff.MemberKind
    :members:
    :undoc-members:

.. autoclass:: dbus_next.introspection_diff.IntrospectionDiff
    :members:
    :undoc-members:

.. autoclass:: dbus_next.introspection_diff.Column
    :members:
    :undoc-members:

.. autofunction:: dbus_next.introspection_diff.compare_introspections

Example
++++++

Save the introspection XML before and after a firmware update, then compare
them:

.. code-block:: bash

    python3 -m dbus_next.introspection_diff old.xml new.xml --format json
    python3 -m dbus_next.introspection_diff old.xml new.xml --format csv > plan.csv

The comparison API is available from the module:

.. code-block:: python3

    from dbus_next.introspection_diff import compare_introspections

    plan = compare_introspections(old_xml, new_xml, old_source='before',
                                  new_source='after')
    for column in plan.reusable:
        print('reuse', column.path, column.interface, column.kind, column.name)
    for column in plan.migrate:
        print('migrate', column.name, column.old.signature, '->', column.new.signature)
    for column in plan.stop:
        print('stop', column.name, column.reason)

The JSON output has separate ``reusable``, ``migrate``, and ``stop`` lists.
Each column also has ``old`` and ``new`` snapshots with the original signature
string and a ``signature_status`` of ``known`` or ``unknown``. An unknown
signature stays exactly as written, is marked ``unknown_type``, and is never
converted to ``s``; the affected column is placed in ``migrate`` so a mapping
can be written before collection resumes.

Annotation policy
+++++++++++++++++

Annotations that affect collection are copied into ``policy_evidence`` with
their source document and line. The built-in policy uses these annotations:

* ``org.freedesktop.DBus.Deprecated``: a newly deprecated member stops
  collection.
* ``org.freedesktop.DBus.Property.EmitsChangedSignal``: a changed value makes
  the property require migration.
* ``org.freedesktop.DBus.Method.NoReply``: a change requires migration; a
  no-reply method that still declares output arguments stops collection.
* ``org.freedesktop.DBus.Method.Timeout``: a change requires migration.

Member annotations override interface annotations. Other annotations are
retained and reported in ``annotation_changes`` but do not change the action.
Properties that become write-only stop collection; properties that become
read-only (losing write access) require migration.

Column identity
+++++++++++++++

A column is identified by object path, interface, member kind, and member name.
A property and a method with the same name are different columns, as are
members of the same name on different interfaces or object paths.
