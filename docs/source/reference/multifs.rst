Multi Filesystem
================

A MultiFS is a filesystem composed of a sequence of other filesystems,
where the directory structure of each overlays the previous filesystem
in the sequence.

One use for such a filesystem would be to selectively override a set of
files, to customize behavior. For example, to create a filesystem that
could be used to *theme* a web application. We start with the following
directories::


    `-- templates
        |-- snippets
        |   `-- panel.html
        |-- index.html
        |-- profile.html
        `-- base.html

    `-- theme
        |-- snippets
        |   |-- widget.html
        |   `-- extra.html
        |-- index.html
        `-- theme.html

And we want to create a single filesystem that will load a file from
``templates/`` only if it isn't found in ``theme/``. Here's how we could
do that::


    from fs.osfs import OSFS
    from fs.multifs import MultiFS

    theme_fs = MultiFS()
    theme_fs.add_fs('templates', OSFS('templates'))
    theme_fs.add_fs('theme', OSFS('theme'))


Now we have a ``theme_fs`` filesystem that presents a single view of both
directories::

    |-- snippets
    |   |-- panel.html
    |   |-- widget.html
    |   `-- extra.html
    |-- index.html
    |-- profile.html
    |-- base.html
    `-- theme.html


.. _multifs-layers:

Read-only layers and writable corrections
-----------------------------------------

A common use of `MultiFS` is to combine a read-only *base* with a
*writable* layer that holds corrections::

    from fs.enums import DeletionPolicy
    from fs.memoryfs import MemoryFS
    from fs.multifs import MultiFS

    base = MemoryFS()
    corrections = MemoryFS()
    base.writebytes("manual.txt", b"the base manual")

    library = MultiFS(deletion_policy=DeletionPolicy.shadow)
    library.add_fs("base", base)
    library.add_fs("corrections", corrections, write=True)

Reads look at every layer in priority order, but writes are only ever
applied to the filesystem added with ``write=True``.  The base layer is
never modified by a write.

Write plans
~~~~~~~~~~~

The target of a write is explicit in the plan returned by
`MultiFS.write_plan` (also available as ``plan_write`` or
``explain_write``)::

    >>> plan = library.write_plan("new.txt", "w")
    >>> plan.target_name
    'corrections'
    >>> plan.source_name is None
    True
    >>> plan.describe()
    "write '/new.txt' (w) to backend 'corrections'"

Appending to a file that currently lives in the base reports that the
write requires a *copy-up*: the base contents are copied to the writable
layer before the append, leaving the base intact::

    >>> plan = library.write_plan("manual.txt", "a")
    >>> plan.source_name
    'base'
    >>> plan.copy_up
    True

Deletion plans
~~~~~~~~~~~~~~

Deleting a resource from a layered filesystem can mean two different
things.  `MultiFS.delete_plan` (also ``plan_delete`` or
``explain_delete``) makes the choice explicit through the filesystem's
`~fs.enums.DeletionPolicy`, which can also be overridden per call::

    >>> library.writebytes("manual.txt", b"a correction")
    >>> plan = library.delete_plan("manual.txt")
    >>> plan.policy
    <DeletionPolicy.shadow: 'shadow'>
    >>> plan.action
    'shadow-base'
    >>> plan.reveals_base
    False

The two policies are:

``DeletionPolicy.retract``
    Removing an upper-layer *correction* reveals the base resource
    again, if there is one.  This is **not** a global delete: only the
    correction is withdrawn.  If the base has no such resource, the
    delete removes the upper-layer file and nothing is revealed.

``DeletionPolicy.shadow``
    Removing an upper-layer *correction* also writes a *tombstone* to
    the writable layer, so the base resource stays hidden.  Deleting a
    resource that only exists in the base writes a tombstone as well;
    the base file is never physically removed.

A `~fs.multifs.DeletePlan` states which case applies: ``retracts`` and
``shadows`` identify the action, ``reveals_base`` reports whether the
base becomes visible again, and ``lower_untouched`` is always `True`.
The ``lower_exists`` and ``upper_exists`` flags show which layers held
the resource at planning time.

Because the base is shared, a physical removal from the upper layer is
never presented as a global delete.  Re-adding an upper-layer file at a
shadowed path clears the tombstone for that exact path; creating a
directory that was shadowed does the same, revealing the base contents
again.

Tombstones are stored in the writable layer under
``/.multifs/shadows/``.  The directory is hidden from ``listdir`` and
``scandir`` of the layered view, but persists as long as the writable
layer does, so a new `MultiFS` over the same layers sees the same
shadows.

.. autoclass:: fs.multifs.MultiFS
    :members:

.. autoclass:: fs.multifs.WritePlan
    :members:

.. autoclass:: fs.multifs.DeletePlan
    :members:

.. autoclass:: fs.enums.DeletionPolicy
    :members:
