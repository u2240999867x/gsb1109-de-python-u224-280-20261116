************************************
Shared Streaming Read (Fan-Out)
************************************

Use :class:`mcap.fanout.SharedStreamReader` when one non-rewindable MCAP stream (a pipe,
socket, or other forward-only source) must feed an indexer and several collection
consumers at once. The stream is scanned once, each compressed chunk is decompressed a
single time and shared, and every subscription gets its own message cursor, bounded
buffer, and cancellation.

Each subscription chooses its own slow-consumer policy. Lossy policies
(``DROP_OLDEST``, ``DROP_LATEST``, ``ERROR``) affect only that subscription; the
lossless ``BLOCK`` policy makes the producer wait at an explicit back-pressure boundary
(:attr:`~mcap.fanout.SharedStreamReader.blocked_on`) rather than silently dropping data.

.. literalinclude:: ../examples/raw/shared_stream_reader.py
