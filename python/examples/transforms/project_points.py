"""Read point messages projected into a target coordinate frame.

The capture package stores frame relationships on a JSON topic (``/tf`` by default)
with ``parent_frame_id``, ``child_frame_id``, a timestamp, a translation, and a unit
quaternion rotation.  Point messages on other topics name their own ``frame_id``.
"""

import sys

from mcap.reader import make_reader

with open(sys.argv[1], "rb") as f:
    reader = make_reader(f)

    # Collect the transform history once, then query it as often as needed.
    timeline = reader.get_transform_timeline(
        interpolation="linear", max_interval_ns=100_000_000
    )
    print("frames:", ", ".join(timeline.frames))
    for error in timeline.errors:
        print(f"skipped transform on {error.topic} at {error.log_time}: {error.reason}")

    resolution = timeline.resolve_transform("lidar", "map", sample_time=1_000_000_000)
    print("status:", resolution.status)
    for path in resolution.paths:
        print("path:", " -> ".join(path.path))
        for sample in path.samples:
            print(
                f"  transform logged at {sample.log_time}, sampled at {sample.sample_time}"
            )

    # Or iterate messages with every point already projected into ``map``.
    for projected in reader.iter_projected_messages(
        "map", topics=["points"], max_interval_ns=100_000_000
    ):
        for point in projected.projections:
            print(
                projected.channel.topic,
                projected.frame_id,
                projected.sample_time,
                point.status,
                point.position,
                point.positions,
            )
