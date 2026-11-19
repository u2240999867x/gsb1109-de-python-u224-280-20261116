Queue Planning
==============

The :mod:`mpd.queueplan` module turns an MPD playlist into a desired target
queue of URIs using only the standard ``addid``, ``deleteid`` and ``moveid``
commands.  Every entry is tracked by its MPD ``songid``, so repeated songs are
never confused with one another and every generated action can be audited
against the exact occurrence it touches.

A plan is built from a *snapshot* of the current queue.  Take one directly
from a client or construct it from a raw ``playlistinfo()`` result::

    from mpd.queueplan import QueuePlanner, CostTable, take_snapshot

    snapshot = take_snapshot(client.playlistinfo())

Snapshots validate their input: an entry without a usable ``file`` or ``id``,
a duplicate songid or an empty URI raises
:class:`~mpd.queueplan.SnapshotIncompatibleError`.  No destructive command is
ever generated or sent while the queue view is partial or internally
inconsistent.

Building a plan
---------------

Pass the target sequence of URIs (repeated URIs mean repeated occurrences)::

    from mpd.queueplan import build_plan

    plan = build_plan(snapshot, ["a.ogg", "b.ogg", "a.ogg"])

The returned :class:`~mpd.queueplan.Plan` is fully simulated before it is
returned.  Each of its actions records the occurrence it operates on:

* :class:`~mpd.queueplan.InsertAction` carries the URI, its target occurrence
  ordinal and the exact ``addid`` position.
* :class:`~mpd.queueplan.DeleteAction` carries the songid, URI, occurrence
  ordinal and the position at which it currently sits.
* :class:`~mpd.queueplan.MoveAction` carries the songid, occurrence ordinal,
  the live ``from`` position, the ``moveid`` argument and the resulting
  position.

Move positions follow real MPD semantics: the daemon detaches the song first
and, for a rightward move, decrements the target index by one before
reinserting it.  The planner accounts for this explicitly, so the simulated
result always matches what the daemon will produce.

Use :func:`~mpd.queueplan.simulate_plan` to replay a plan offline and verify
that it reaches the target while preserving the original songids of every
retained occurrence.

Costs and allowed actions
-------------------------

Costs are supplied through :class:`~mpd.queueplan.CostTable`.  The optimizer
minimizes the total cost; keeping an existing occurrence is preferred
whenever ``move`` is cheaper than ``delete + insert``::

    costs = CostTable(insert=1.0, delete=1.0, move=1.0)

Individual action kinds can be disabled.  When no feasible plan remains with
the allowed kinds, :class:`~mpd.queueplan.QueuePlanError` is raised rather
than silently substituting a forbidden command.

Executing with revalidation
---------------------------

:class:`~mpd.queueplan.QueuePlanner` drives a real client and re-fetches the
queue before every pending action::

    planner = QueuePlanner(client, costs)
    final_plan = planner.plan_and_execute(target_uris)

If another client changes the queue mid-run, the un-sent remainder is
re-planned against the fresh snapshot.  Already confirmed actions are never
replayed, and a stale or generation-incompatible plan is never allowed to
issue its deletes.  An explicit generation token can be supplied to the
planner; mismatched generations force a re-plan before any destructive
command.
