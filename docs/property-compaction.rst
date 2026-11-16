.. _property-change-compaction:

Property Change Compaction
==========================

A client that watches properties receives one
``org.freedesktop.DBus.Properties.PropertiesChanged`` signal per change. When
the goal is to answer a small set of questions at fixed points in a signal
stream, keeping every signal as its own record is unnecessary. The
:mod:`dbus_next.property_compaction` module folds consecutive signals into
smaller ingestion batches, but only when the answers do not change.

The module is independent of the message bus. Build :class:`PropertyChange
<dbus_next.property_compaction.PropertyChange>` objects from numbered signals
(with :meth:`PropertyChange.from_message
<dbus_next.property_compaction.PropertyChange.from_message>` or directly), and
pass them to :func:`compact_property_changes
<dbus_next.property_compaction.compact_property_changes>` with the
:class:`ObservationPoint <dbus_next.property_compaction.ObservationPoint>`
instances that must be answered.

.. code-block:: python3

    from dbus_next import EmitsChangedSignal
    from dbus_next.property_compaction import (CompactionConfig, ObservationPoint,
                                               PropertyChange, compact_property_changes)

    signals = [
        PropertyChange(1, 'com.example.Interface', {'Volume': 10}),
        PropertyChange(2, 'com.example.Interface', {'Volume': 20}, invalidated=['Title']),
        PropertyChange(3, 'com.example.Interface', {'Title': 'Track'}),
    ]
    observations = [
        ObservationPoint('volume-after-3', 'com.example.Interface', 'Volume', 3),
        ObservationPoint('title-after-2', 'com.example.Interface', 'Title', 2),
    ]
    config = CompactionConfig(
        annotations={('com.example.Interface', 'Title'): EmitsChangedSignal.INVALIDATED})

    result = compact_property_changes(signals, observations, config)
    for state in result.observations:
        print(state.id, state.status, state.value, state.reason)

The result has three parts:

``batches``
    The compacted ingestion batches. Each batch is atomic; it carries the
    final reported values and invalidations of the signals folded into it.
``records``
    The compression log. Every batch lists its source serials, and every batch
    that starts a new group records why (``observation-boundary``,
    ``stream-switch``, ``stream-gap``, ``event-interface``, or
    ``merging-disabled``). Entries also note any value dropped because of an
    annotation.
``observations``
    The knowable state at each observation point: ``known`` with a value, or
    ``unknown`` with a reason.

Batching rules
--------------

The input is first replayed one signal at a time to establish the reference
answer for every observation point. Adjacent signals are then merged greedily
and a merge is kept only when every observation point still gets exactly the
same status, value, and reason. A merge never invents a state that the
one-signal-at-a-time replay would not have produced.

The ``EmitsChangedSignal`` annotation controls what a signal means:

``true``
    The signal carries a new value and the last delivered value is the
    cacheable value.
``invalidated``
    The signal only says the cached value is stale. The value becomes unknown
    and must be re-read; the previous value is never filled back in. If the
    signal carries a value in ``changed`` anyway, the value is dropped.
``const``
    The property is not supposed to change. A change signal for it is a
    contradiction, so the value becomes unknown.
``false``
    The property may change without a signal. Its value is never cacheable
    from signals alone, and "did it change?" cannot be answered from signals.

Stream gaps
-----------

A gap in a ``(path, interface)`` stream means an unknown signal may have
changed any property of that interface. Those properties stay unknown until a
later signal reports a value for them. Other interfaces are untouched, so an
unrelated property does not lose its value because one stream had a gap.

A gap is detected from an explicit ``sequence`` number when the input provides
one. Bus serials are normally shared with unrelated traffic, so a serial jump
alone is not a gap. If the input contains nothing but property signals, set
``dense_serials=True`` to infer a gap from skipped serials as well; skipped
serials that appear elsewhere in the input (for example, a signal of another
interface) are not counted as a gap.

Event interfaces
----------------

Some interfaces describe events (for example, log or notification streams)
rather than state. List them in ``CompactionConfig.event_interfaces`` and every
signal is kept as its own batch. Each batch is marked ``event`` and no two
signals are merged.

Command line
------------

The module also runs as a command line tool that reads and writes JSON:

.. code-block:: bash

    python -m dbus_next.property_compaction input.json

The input object may contain ``signals``, ``observations``, ``annotations``,
``event_interfaces``, ``detect_gaps``, ``dense_serials``, and ``merge``.
Signals use the keys ``serial``, ``interface``, ``changed``, ``invalidated``,
``path``, and ``sequence``. Observations use ``id``, ``interface``,
``property``, ``at_serial``, ``kind`` (``value`` or ``changed``), ``path``,
and ``after_serial``. Annotations may be keyed by ``"interface:property"``
strings or nested by interface. The output is the same structure as
:meth:`CompactionResult.to_dict
<dbus_next.property_compaction.CompactionResult>`.

API
---

.. autoclass:: dbus_next.property_compaction.PropertyChange
    :members:
    :undoc-members:

.. autoclass:: dbus_next.property_compaction.ObservationPoint
    :members:
    :undoc-members:

.. autoclass:: dbus_next.property_compaction.ObservationKind
    :members:
    :undoc-members:

.. autoclass:: dbus_next.property_compaction.CompactionConfig
    :members:
    :undoc-members:

.. autofunction:: dbus_next.property_compaction.compact_property_changes

.. autofunction:: dbus_next.property_compaction.compact

.. autofunction:: dbus_next.property_compaction.annotation_map

.. autoclass:: dbus_next.property_compaction.PropertyBatch
    :members:
    :undoc-members:

.. autoclass:: dbus_next.property_compaction.StreamGap
    :members:
    :undoc-members:

.. autoclass:: dbus_next.property_compaction.ObservationState
    :members:
    :undoc-members:

.. autoclass:: dbus_next.property_compaction.CompactionResult
    :members:
    :undoc-members:
