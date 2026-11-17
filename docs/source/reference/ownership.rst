Ownership and Shutdown
======================

Filesystems are often composed into a graph. A `~fs.mountfs.MountFS` or
`~fs.multifs.MultiFS` may reference another filesystem, and a
`~fs.subfs.SubFS` references its parent. The same child may be referenced
from more than one task, or from both a mount and a sub-directory.

Every reference is recorded as a *hold* with an explicit role:

* ``owned`` -- the holder is responsible for closing the child, but only
  once no other wrapper still references it.
* ``borrowed`` -- the holder never closes the child.

The role is chosen on the wrapping edge, not inferred from an
``auto_close`` flag on a backend. ``auto_close`` remains accepted as a
backward-compatible alias for an owned edge.

**The Ledger**
`~fs.base.FS.holds` reports the holds on a filesystem, `~fs.base.FS.holding`
reports the holds it has on its children, and `~fs.base.FS.explain_open`
answers why a resource is still open. `~fs.base.FS.holds_report` returns the
same information as plain dictionaries for diagnostics.

Closing one wrapper releases only its own holds. A backend that another
task still owns, or merely borrows, remains open. If an owner closes while a
borrower remains, the close is recorded as owed and is completed when the
last reference is released.

**Shared Nodes**
When a node is shared between wrappers, each participant must declare its
cascade behaviour. ``MountFS``, ``MultiFS``, ``SubFS``, ``ClosingSubFS``
and ``WrapFS`` declare whether closing them can close their children. A
wrapper that overrides ``close`` without such a declaration is treated as
opaque, and adding a second reference to it raises
`~fs.errors.UnsafeSharing` instead of guessing what its close would do.

Repeated ``close`` calls, and failures during close, are idempotent: each
hold is released at most once, and the terminal `~fs.ownership.CloseReport`
is returned again without re-running finalization.

**Shutdown Policy**
Once shutdown begins, new operations raise `~fs.errors.FilesystemClosed`.
Operations that were already admitted, and open file handles, are handled
according to the policy passed to ``close`` (or the filesystem default):

``drain``
    Stop admitting work and wait for admitted reads and open handles to
    finish. If a timeout was supplied it is an explicit bound; timing out
    returns a deferred report.

``abort``
    Close outstanding handles immediately. A read which cannot be
    pre-empted leaves an explicit deferred report rather than claiming the
    resource was released.

``defer``
    Return a deferred report immediately if anything is still active.

.. autoclass:: fs.ownership.Ownership
   :members:
   :undoc-members:

.. autoclass:: fs.ownership.ClosePolicy
   :members:
   :undoc-members:

.. autoclass:: fs.ownership.CloseStatus
   :members:
   :undoc-members:

.. autoclass:: fs.ownership.LifecycleState
   :members:
   :undoc-members:

.. autoclass:: fs.ownership.CloseCascade
   :members:
   :undoc-members:

.. autoclass:: fs.ownership.Hold
   :members: describe, to_dict, release

.. autoclass:: fs.ownership.CloseReport
   :members: as_dict, to_dict, add_error

.. autoclass:: fs.ownership.ManagedFile
   :members: force_close
