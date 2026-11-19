*************************
Time-Window Queries
*************************

An MCAP message carries two timestamps: ``log_time``, when the recorder wrote
it, and ``publish_time``, when its publisher produced it. Both are unsigned
64-bit nanosecond integers, and the query API preserves them exactly.

Composable windows
------------------

A :class:`~mcap.time_window.TimeRange` is a half-open range ``[start, end)``.
A :class:`~mcap.time_window.TimeWindow` combines a log-time range with a
publish-time range, and :class:`~mcap.time_window.MessageQuery` is a union of
windows. Both compose with ``&`` and ``|``:

.. code-block:: python

    from mcap.reader import make_reader
    from mcap.time_window import TimeRange, TimeWindow

    # Messages recorded in the first second of a recording whose publish clock
    # ran independently, published in a later half-second.
    window = (
        TimeWindow(log_time=TimeRange(0, 1_000_000_000))
        & TimeWindow(publish_time=TimeRange(2_000_000_000, 2_500_000_000))
    )

    # Either of two disjoint publish-time windows.
    query = (
        TimeWindow(publish_time=TimeRange(0, 1_000_000_000))
        | TimeWindow(publish_time=TimeRange(5_000_000_000, 6_000_000_000))
    )

    with open("recording.mcap", "rb") as f:
        for schema, channel, message in make_reader(f).iter_messages(query=query):
            ...

The individual ``publish_starting_at`` / ``publish_starting_after`` /
``publish_ending_at`` / ``publish_ending_before`` keyword arguments are a
shorthand for a single publish-time range, and any explicit ``starting_*`` /
``ending_*`` bounds intersect with the ``query`` argument.

Ordering
--------

``iter_messages`` yields messages in ascending ``log_time`` by default. Pass
``order="file_order"`` to receive records in the order they appear in the file.
Messages that share a log time keep file order (chunk start offset, then the
record's position in its chunk), so the ordering is total: the same query
always yields the same sequence, and ``reverse=True`` yields exactly the
reverse.

The chunk index records only the ``log_time`` span of the messages they index.
The publish clock is independent and may run backwards, so a query that
constrains only ``publish_time`` keeps every chunk a candidate and filters each
message on its actual ``publish_time``. The log-time index is never used to
prune a publish-time match.

Link latency is not implied
---------------------------

Without separate evidence that the two clocks are correlated, the difference
``publish_time - log_time`` is a difference between two unrelated clocks, not a
transport or link latency. The query API therefore reports both timestamps as
stored and provides no latency computation.
