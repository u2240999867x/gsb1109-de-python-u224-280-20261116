# Progress Tracking

Progress records from a remote runspace can arrive interleaved for several
pipelines and activity trees. :class:`psrpcore.ProgressView` keeps a current
view without assuming the records arrive in a useful order.

```python
import psrpcore

view = psrpcore.ProgressView()

for event in events:
    view.update_event(event)

for activity in view.activities:
    print(activity.activity, activity.status, activity.percent_complete)
```

A client Runspace Pool already owns one at ``pool.progress_view`` and updates
it for every :class:`psrpcore.ProgressRecordEvent` received.

## Identity

An activity is identified by its source (normally the Runspace Pool), its
pipeline and its ``ActivityId``. The same activity number in different sources
or pipelines is a different activity.

When an activity number is reused, pass generation evidence so the two uses are
kept apart:

```python
view.add(source=pool_id, pipeline_id=pipeline_id, activity_id=1,
         activity="first run", percent_complete=100, generation="run-1")
view.add(source=pool_id, pipeline_id=pipeline_id, activity_id=1,
         activity="second run", percent_complete=5, generation="run-2")
```

Comparable generations make the newest one current. Older generations remain
available through ``view.get(..., generation=...)`` and
``view.all_activities``.

Updates can also carry a ``sequence`` value. Once a numbered update has been
accepted for a generation, a record with a lower or equal sequence is ignored;
a late older record cannot replace the confirmed newer view. A completed
record is terminal for its generation.

## States

The three non-progressing states are distinct:

- ``ProgressStatus.NO_UPDATE``: the activity is known (for example through
  ``expect()``) but no progress record has arrived.
- ``ProgressStatus.UNKNOWN_PERCENT``: a record arrived but had no usable
  percentage (PowerShell uses ``-1`` for this).
- ``ProgressStatus.COMPLETED``: the record explicitly carried
  ``ProgressRecordType.Completed``.

A completed parent does not complete its children. Complete children
explicitly, or give them a known percentage.

## Trees and coverage

``view.tree(source=..., pipeline_id=...)`` returns a
:class:`psrpcore.ProgressTree`. Known activities remain available when the
graph is incomplete:

- ``roots`` contains activities without a parent and activities whose parent is
  missing.
- ``orphans`` contains the activities whose declared parent is missing.
- ``cycles`` contains each detected cycle; its activities are still listed in
  ``activities``.
- ``unresolved`` contains activities not reachable from a root, such as cycle
  members.

Coverage is deliberately conservative. An overall percentage is only produced
when every activity has an update, every leaf has a known percentage or an
explicit completion, every leaf has an input weight, all declared parents are
present, and there are no cycles. ``coverage.reason`` describes what is
missing, and the individual ``no_update``, ``unknown_percent``,
``missing_weight``, ``missing_parents`` and ``cycles`` fields identify the
activities involved.

Weights are supplied by the caller, either when adding an update, through
``ProgressView(weights=...)`` or with ``set_weight()``. ``total_weight`` can
also be supplied to require a minimum amount of covered weight.

```python
coverage = view.coverage(source=pool_id, pipeline_id=pipeline_id)
if coverage.complete:
    print(coverage.percent)
else:
    print(coverage.reason)
```
