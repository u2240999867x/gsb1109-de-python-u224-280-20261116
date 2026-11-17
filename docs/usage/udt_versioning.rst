.. py:currentmodule:: pycomm3

=========================
UDT Versions and Archives
=========================

When a controller downloads a new UDT/AOI template, the tag list uploaded by
:class:`~LogixDriver` describes the *new* layout.  Bytes captured earlier were
interpreted with the *old* layout and must not be relabeled automatically.
The :mod:`pycomm3.udt_versioning` classes keep those two worlds separate.

Both sides of a comparison must belong to the same device.  Comparing catalogs
from different devices raises :class:`DeviceMismatchError`.

The module is available directly from ``pycomm3``:

.. code-block:: python

    from pycomm3 import (
        AcquisitionArchive,
        ChangeKind,
        UDTCatalog,
        compare_catalogs,
    )

Versioned catalogs
------------------

A :class:`UDTCatalog` is scoped to exactly one device and one catalog version.
A missing type never falls through to another version or another device, even
when the type name matches.

.. code-block:: python

    old_catalog = UDTCatalog.from_tags('Line1-PLC', 'before-download', old_tags)
    new_catalog = UDTCatalog.from_tags('Line1-PLC', 'after-download', new_tags)

    report = compare_catalogs(old_catalog, new_catalog, old_tags, new_tags)

    for impact in report.affected_tags:
        print(impact.tag_name, impact.verdict, impact.describe_chain())

``CatalogStore`` provides the same guarantee across many versions:
``store.get_layout(device_id, version, type_name)`` always raises
:class:`MissingTemplateError` if that exact template was not uploaded.  It never
substitutes a same-named UDT from a different device.

Verdicts
--------

Each affected tag receives one of these verdicts:

``unchanged``
    No interpretation change.

``rename_only``
    A member or type name changed, but offsets, sizes, bit positions, arrays,
    and primitive types are identical.  Existing values are still valid.

``layout_compatible``
    The physical slots are identical, but a field's meaning changed, e.g.
    ``DINT`` became ``REAL`` at the same offset.  The old bytes can be
    reinterpreted with the new layout; they must not be silently treated as
    already interpreted in the new units.

``reread_required``
    Any offset, bit position, array extent, member size, or outer structure
    size changed.  The old acquisition cannot describe the new layout.

``undetermined``
    A required template is missing or incomplete on either side.  The
    comparison deliberately leaves the tag unresolved.

The comparison is recursive.  An unchanged outer ``structure_size`` does not
stop inspection of nested UDTs, arrays of UDTs, or bit aliases, so an internal
field change cannot hide behind an equal total byte count.

Dependency chains
-----------------

``TagImpact.chain`` contains one :class:`DependencyLink` per type in the path,
including array element types.  It is written in old-to-new form:

.. code-block:: text

    Recipe -> Ingredient[8] -> Lot

A link is marked ``missing=True`` when the corresponding template is absent.
The tag stays ``undetermined`` rather than borrowing a definition from another
tag, version, or device.

Archives and raw bytes
----------------------

:class:`AcquisitionArchive` stores catalogs and acquisitions by version.
Recording an acquisition binds it to the catalog version under which it was
interpreted:

.. code-block:: python

    archive = AcquisitionArchive('Line1-PLC')
    archive.add_snapshot('v1', plc_before_download)
    archive.record('recipe', 'v1', raw_bytes, values)

    archive.add_snapshot('v2', plc_after_download)
    assessment = archive.assess('v1', 'v2')

    for acquisition in assessment.reread_required:
        print('reread', acquisition.tag_name)

The original record keeps its ``raw`` bytes, ``values``, and
``catalog_version``.  It is immutable.  Switching versions is explicit:

.. code-block:: python

    archive.reinterpret('recipe', 'v1', 'v2', new_values)

``reinterpret`` writes a *new* acquisition under ``v2`` and leaves the ``v1``
record untouched.  Loading or replacing a catalog never changes an already
interpreted value.

Snapshots from a driver
-----------------------

Use :func:`snapshot_driver` to detach both the type table and tag list from a
live driver:

.. code-block:: python

    from pycomm3 import snapshot_driver

    catalog, tags = snapshot_driver(plc, 'Line1-PLC', 'v2')

This deep-copies definitions, so a later ``plc.get_tag_list()`` will not mutate
an archived catalog.
