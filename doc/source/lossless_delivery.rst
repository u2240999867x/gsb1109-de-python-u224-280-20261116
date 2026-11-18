.. -*- mode: rst -*-

.. _lossless_delivery:

Lossless deliveries
*******************

A receiving site often accepts only a subset of NIfTI versions, data types,
scaling representations and header-field ranges.  The :mod:`nibabel.delivery`
module finds the smallest file within those limits whose decoded voxel values
are *exactly* the original values, and it reports how each original header
declaration moves.

The planner works from decoded values, not from the source data type.  An
``int16`` image can carry fractional values through ``scl_slope``; a
``float64`` image can hold only integers.  Neither fact is visible from the
data type alone.

.. code-block:: python

    import nibabel as nib
    from nibabel.delivery import TargetProfile, plan_delivery, write_delivery

    target = TargetProfile(
        versions=('nifti1',),
        dtypes=('uint8', 'int16', 'float32'),
        scaling=('header',),
        max_dim=32767,
    )
    img = nib.load('source.nii.gz')  # read-only
    plan = plan_delivery(img, target)
    if plan.ok:
        write_delivery(plan, 'delivery.nii.gz')

Exact values
============

Values are compared as exact rationals.  A ``float32`` or integer encoding is
accepted only when every value survives the conversion unchanged; nothing is
rounded or truncated.  ``0.1`` in ``float64`` therefore stays ``float64``
unless the target allows a scaling pair that reproduces it exactly.

Scaling
=======

When the target allows header scaling (``scaling=('header',)``), the planner
computes the exact common step of the values and, when it is a float32-exact
binary fraction, stores integer codes with ``scl_slope`` and, if needed,
``scl_inter``::

    value = code * slope + inter

The zero-intercept layout is preferred.  ``scaling=('none',)`` forbids any
stored slope other than 1 or intercept other than 0; fractional values then
have no legal lossless encoding.

Non-finite values
=================

``NaN`` and ``+/-Inf`` are never treated as zero.  Integer data types cannot
carry them, so an integer-only target blocks such a delivery.  Floating-point
targets preserve them bit for bit (``-0.0`` keeps its sign).

Blocked deliveries
==================

When no allowed combination is lossless, :attr:`DeliveryPlan.blockers` lists
each blocking field and value, and :func:`write_delivery` raises
:class:`DeliveryBlocked`.  The planner never silently rounds or truncates.

Header duties
=============

:attr:`DeliveryPlan.migrations` records, field by field, what the delivered
header carries:

* ``version`` -- NIfTI-1 or NIfTI-2;
* ``datatype`` -- the on-disk type;
* ``scaling`` -- the exact ``scl_slope`` / ``scl_inter`` pair;
* ``dim`` -- the data shape;
* ``vox_offset`` -- the data offset (recomputed when the source left it
  unset);
* ``extensions`` -- extension codes, preserved when the target allows it.

Every other header field keeps its value through nibabel's normal
header-to-header conversion.  :func:`verify_round_trip` reloads the planned
bytes and checks that every decoded value matches the source exactly.

Command line
============

``nib-deliver`` plans or writes a delivery and can emit the report as JSON::

    nib-deliver source.nii.gz --dtype int16 --json
    nib-deliver source.nii.gz delivery.nii.gz --target target.json
