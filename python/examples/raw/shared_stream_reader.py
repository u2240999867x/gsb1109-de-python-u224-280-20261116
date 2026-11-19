"""Fan one non-rewindable MCAP stream out to several independent subscriptions.

The stream is read and each chunk decompressed exactly once; every subscription keeps
its own cursor and its own bounded buffer. The lossless subscription blocks the producer
when it falls behind, while the lossy one records gaps and never stalls anyone else.

Run with: python shared_stream_reader.py <file.mcap>
"""

import sys

from mcap.fanout import BackpressurePolicy, SharedStreamReader


def main(path: str) -> None:
    # One reader, one sequential pass over a stream that need not be seekable.
    reader = SharedStreamReader(path)

    # Lossless: bounded to 100 buffered messages; if it fills, the producer waits on
    # this subscription instead of dropping anything. reader.blocked_on reports that
    # explicit blocking boundary.
    lossless = reader.subscribe(
        topics=["/camera"], max_messages=100, policy=BackpressurePolicy.BLOCK
    )

    # Lossy: if this consumer cannot keep up, the oldest messages are discarded and the
    # gap is recorded on the subscription. This never affects the lossless subscriber.
    lossy = reader.subscribe(
        topics=["/telemetry"], max_messages=100, policy=BackpressurePolicy.DROP_OLDEST
    )

    producer = reader.run_in_thread()
    try:
        for message in lossless:
            handle_camera(bytes(message.message.data))

        for message in lossy:
            handle_telemetry(bytes(message.message.data))
    finally:
        # Either subscription can be cancelled independently at any time; the other
        # keeps going. close() also joins the producer and closes the opened file.
        producer.join()
        reader.close()

    for gap in lossy.gaps:
        print(
            f"missed {gap.count} telemetry messages "
            f"({gap.first_log_time}..{gap.last_log_time})"
        )


def handle_camera(data: bytes) -> None:
    print(f"camera frame: {len(data)} bytes")


def handle_telemetry(data: bytes) -> None:
    print(f"telemetry: {data!r}")


if __name__ == "__main__":
    main(sys.argv[1])
