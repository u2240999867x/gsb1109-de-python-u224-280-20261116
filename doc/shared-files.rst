.. _shared-files:

Managing shared and removed files
=================================

Several data packages can point at the same cache directory and therefore
share individual files. Pooch keeps reference counts for those files so that
removing a material from one package never deletes a file that another
package still needs.

The counts are maintained by :class:`pooch.SharedFileRegistry`, stored in a
small JSON file (``.pooch-references.json``) inside the cache directory. Paths
are recorded relative to the cache folder, so moving the cache does not break
the index.

Tracking references
-------------------

Every successful :meth:`pooch.Pooch.fetch` records the calling package as an
owner of the fetched file. Tracking is idempotent: a package counts once per
file however many times it fetches it. You can also record a reference
explicitly, for example for files produced by a custom processor:

.. code:: python

    GOODBOY.track("processed-data.csv")

Each :class:`~pooch.Pooch` is identified by its ``name`` argument. Packages
that share a cache directory should pass distinct, stable names so their
references are counted separately. If no name is given, the last component of
the cache path is used.

Removing a material from one version
------------------------------------

When a new package version drops a registry entry, use
:meth:`pooch.Pooch.remove_registry_file` instead of deleting the file by hand:

.. code:: python

    # Only this package's reference is released
    deleted = GOODBOY.remove_registry_file("old-data.csv")

The registry entry and the package's reference are removed immediately. The
underlying file is deleted only when no other package references it; the
method returns ``False`` while the file is still shared and ``True`` once it
has actually been removed. Emptied parent directories are pruned, but the
cache directory itself and the reference index are always kept.

For finer control, :meth:`pooch.Pooch.untrack` releases the reference without
touching the registry, and :meth:`pooch.SharedFileRegistry.remove_owner`
releases every file referenced by one package entry at once.
