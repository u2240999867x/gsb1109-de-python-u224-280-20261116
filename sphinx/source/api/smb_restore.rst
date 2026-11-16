Restore Planning and Pinned Reads
=================================

This module helps choose one backup that spans several SMB shares.

A share lists only its own shadow copies.  The same datetime on two shares
does not prove that the copies belong to the same backup, so
:func:`smb.restore.find_restore_sets` never derives a common set from
timestamps.  The caller supplies a trusted backup-group correspondence naming
the copy for every share in scope.

Only sets with all of the following are returned:

* every share in the restore scope has a correspondence entry;
* every entry has evidence and resolves to a listed copy;
* the copy timestamps fall inside the inclusive allowed window;
* every required file or directory exists in the selected copy;
* no declaration contradicts the trusted correspondence.

Groups that fail are not silently assembled from newer or nearest copies.
They produce :class:`smb.restore.RestoreDiagnostic` entries classified as
missing files, missing correspondence/evidence, declaration contradictions,
out-of-window copies, untrusted groups, or unavailable copies.

Example
-------

::

    import datetime
    from smb.restore import (
        BackupGroup, RequiredFile, Snapshot, TimeWindow, find_restore_sets,
    )

    shares = {
        'finance': [Snapshot(datetime.datetime(2026, 11, 12, 2, 0), 'finance-1')],
        'archive': [Snapshot(datetime.datetime(2026, 11, 12, 2, 0, 30), 'archive-7')],
    }
    groups = [BackupGroup('nightly-2026-11-12', {
        'finance': ('finance-1', 'catalogue:nightly/finance'),
        'archive': ('archive-7', 'catalogue:nightly/archive'),
    })]
    report = find_restore_sets(
        shares, groups,
        TimeWindow(datetime.datetime(2026, 11, 12), datetime.datetime(2026, 11, 13)),
        required_files=[RequiredFile('finance', '/ledger.xlsx')],
    )
    restore = report.select_set()

Every read through :class:`smb.restore.PinnedRestore` prefixes the share-relative
path with the selected copy's ``@GMT-...`` directory.  There is deliberately
no method that falls back to the live share when a copy is missing or when no
common set exists.  :class:`smb.restore.SMBFileProbe` applies the same rule
when checking required files during planning.

JSON manifests
--------------

:func:`smb.restore.load_manifest` and :func:`smb.restore.plan_from_manifest`
accept the same structure from a file or mapping.  ``shares`` maps share names
to a snapshot list (under ``snapshots`` or ``copies``), ``backup_groups`` maps
each trusted group to its per-share correspondence, and ``window`` and
``required_files`` are optional.  A small command-line wrapper prints the
report as JSON::

    python -m smb.restore restore.json --group nightly-2026-11-12

The command exits with status 2 when no complete common set exists; it still
prints the diagnostics explaining why.

Revocation
----------

If a copy later becomes unavailable, call
:meth:`smb.restore.PinnedRestore.mark_unavailable` (or
:meth:`smb.restore.RestoreReport.invalidate_snapshot`).  Sets that use that
copy are revoked, and diagnostics whose evidence referenced the copy are
withdrawn; independent sets and diagnostics remain.  A revoked set cannot be
reopened through this module.

.. autoclass:: smb.restore.Snapshot
    :members:

.. autoclass:: smb.restore.ShareSnapshots
    :members:

.. autoclass:: smb.restore.GroupMember
    :members:

.. autoclass:: smb.restore.BackupGroup
    :members:

.. autoclass:: smb.restore.TimeWindow
    :members:

.. autoclass:: smb.restore.RequiredFile
    :members:

.. autoclass:: smb.restore.RestoreMember
    :members:

.. autoclass:: smb.restore.RestoreSet
    :members:

.. autoclass:: smb.restore.RestoreDiagnostic
    :members:

.. autoclass:: smb.restore.RestoreReport
    :members:

.. autoclass:: smb.restore.PinnedRestore
    :members:

.. autofunction:: smb.restore.snapshots_for_connection

.. autofunction:: smb.restore.find_restore_sets

.. autofunction:: smb.restore.plan_restore

.. autofunction:: smb.restore.plan_from_manifest

.. autofunction:: smb.restore.load_manifest

.. autofunction:: smb.restore.build_snapshot_path

.. autofunction:: smb.restore.format_snapshot_time
