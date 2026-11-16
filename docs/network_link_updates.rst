Applying Network Link Updates
=============================

A ``NetworkLinkControl`` document carries an :class:`~fastkml.Update` describing
additions (``Create``), modifications (``Change``) and removals (``Delete``) for
data that was loaded earlier.  Parsing such a document only reconstructs the
*description* of those operations.  To actually carry them out against a loaded
object tree, use :func:`~fastkml.apply_update` (or
:func:`~fastkml.apply_updates` for several update documents).

The functions operate on the in-memory KML objects, so afterwards you can
serialize the changed tree in the usual way.

.. code-block:: python

    from fastkml import KML, NetworkLinkControl, apply_update

    kml = KML.parse(Path("loaded.kml"), strict=True, validate=False)
    nlc = NetworkLinkControl.from_string(update_document)

    results = apply_update(kml, nlc.update)

How objects are matched
-----------------------

Every object that can be updated needs an explicit ``id`` in the original file.
The update references those stable ids through ``targetId``.  Matching is done by
id only:

- An unknown target is never turned into a new object of the same name.  The step
  is recorded as not applied together with a human readable reason.
- The identifying attributes (``id``/``targetId``) are never overwritten by a
  ``Change``.

Create
------

A ``Create`` wraps a container whose ``targetId`` names the existing
``Document`` or ``Folder`` that should receive the new children.  The wrapper
container itself is not added; only its child features are appended.

If one of the new children already exists (same id) it is skipped, so replaying
the same update document never appends a second copy.

Change
------

``Change`` is a sparse update.  Only the values present on the change object are
copied onto the object with the matching id; every other attribute is left
untouched.  For example, changing a placemark's name leaves its geometry and
``styleUrl`` in place.

Delete
------

``Delete`` removes the feature with the matching id, detaching it from its parent
list.  Deleting a container removes the whole subtree below it.

Other objects may point at a deleted object, for example a feature's
``styleUrl``, a ``StyleMap`` pair's ``styleUrl`` or a ``SchemaData``'s
``schemaUrl``.  Such surviving references are reported in the result's
``affected_references`` set instead of being silently left dangling.  References
that live inside the deleted subtree are not reported.

Inspecting the results
----------------------

Each individual operation object produces an
:class:`~fastkml.UpdateResult`, so the returned list keeps a full, ordered
provenance trail of every step.  The result's ``target_href`` records the update
document (the source) that the step came from.

.. code-block:: python

    for result in results:
        print(
            result.target_href,
            result.operation,
            result.target_id,
            result.applied,
            result.reason,
        )
        if result.affected_references:
            print("  dangling references:", sorted(result.affected_references))

A result is truthy only when its step was applied, and ``apply_updates`` skips
empty updates (for example one without a ``targetHref``).

.. automodule:: fastkml.update
   :members:
   :undoc-members:
   :show-inheritance:
