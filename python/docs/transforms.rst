******************************
Projecting Points Across Frames
******************************

A capture package records its coordinate frames on one agreed JSON topic (``/tf``
by default).  Each transform message carries ``parent_frame_id``,
``child_frame_id``, a timestamp, a translation, and a unit quaternion rotation.
Point messages on other topics carry the ``frame_id`` their points belong to.

``McapReader.iter_projected_messages`` reads the transform history first, then
projects each point at the message's own sample time.  ``get_transform_timeline``
exposes the same history for direct queries.

.. literalinclude:: ../examples/transforms/project_points.py

Behavior worth knowing
----------------------

- Interpolation is chosen by the caller: ``"linear"`` (default), ``"step"`` /
  ``"zero_order_hold"``, or ``"nearest"``.  ``max_interval_ns`` bounds the gap the
  reader will bridge.
- Interpolation never extrapolates, and never crosses a re-parent event (a child
  frame changing parents) or a clock restart (sample timestamps jumping backwards
  in log order).
- When several frame paths resolve to different positions, the result keeps every
  alternative in ``positions`` and ``candidates`` and reports ``status ==
  "conflict"`` rather than silently picking one.
- A missing edge only affects points whose frame depends on it.
- Each projection reports its original point, sample time, path, and the exact
  transform records used, so results can be audited.
- Transform records are sorted by a canonical semantic key before use, so results
  do not depend on file scan order or chunk layout.
