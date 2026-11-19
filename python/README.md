# MCAP Python Library

The `mcap` library provides classes for reading and writing the MCAP file format.

## Semantic Versioning Notes

Each python package in this repository contains a `__version__` string attribute in the root module:

```python
from mcap import __version__
print(__version__)
```

Modules, variables, classes, attributes etc. with names preceded by a single `_` underscore are not considered part of the public API and may change between minor or patch releases.

## Sequence Continuity Checks

`mcap.continuity` checks whether a publisher's `sequence` counter has holes. Sequence
identity is `(producer_id, topic)`, never the MCAP channel id: several producers may
share one topic, and one producer may publish a topic through several channels.

```python
from mcap.continuity import (
    CoverageInterval,
    RestartEvidence,
    analyze_continuity,
    iter_message_observations,
)
from mcap.reader import make_reader

with open("run.mcap", "rb") as f:
    reader = make_reader(f)
    observations = iter_message_observations(reader, {1: "camera-a", 2: "camera-b"})
    report = analyze_continuity(
        observations,
        coverage=[CoverageInterval("/image", start_time=0, end_time=60_000_000_000)],
        restarts=[RestartEvidence("camera-a", log_time=30_000_000_000)],
    )

for stream in report.streams:
    print(stream.producer_id, stream.topic)
    print("confirmed gaps:", stream.confirmed_gaps)
    print("observation gaps:", stream.observation_gaps)
    print("wraparounds:", stream.wraparounds)
    print("duplicate deliveries:", stream.duplicate_deliveries)
    print("payload conflicts:", stream.payload_conflicts)
    print("undecidable segments:", stream.undecidable_segments)
```

Reports separate the following categories:

- `confirmed_gaps`: sequence numbers missing between two observations whose whole time
  window is covered by a `complete` coverage interval.
- `observation_gaps`: the same kind of hole between two observations, but coverage was
  absent or incomplete over that window. These state only that nothing was observed;
  they do not claim the producer failed to send. Nothing is reported before a stream's
  first observation or after its last one, since only the observed window can establish
  a drop.
- `wraparounds`: legal counter rollover, including a rollover at the default `2**32`
  modulus or one supplied with `sequence_modulus=`.
- `undecidable_segments`: a large backwards step without restart evidence, or a forward
  jump larger than half the modulus, which cannot be classified as a missing range.
- `duplicate_deliveries` / `payload_conflicts`: the same sequence and payload seen more
  than once, and the same sequence carrying distinct payloads (all payloads retained).

Input may be any iterable and may be concatenated from shards in any order; results
are normalized by producer, topic, epoch, and sequence before counting.
