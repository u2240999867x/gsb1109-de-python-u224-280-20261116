import sys

from mcap.merge import merge_directory

# Merge every *.mcap file in a capture directory into one file written beside them.
result = merge_directory(sys.argv[1] if len(sys.argv) > 1 else "capture")

print(f"wrote {result.output_path} ({result.message_count} messages)")
for channel_id in sorted(
    origin.output_channel_id for origin in result.channels.values()
):
    origin = result.origin_for_channel(channel_id)
    print(
        f"channel {channel_id} <- {origin.source_path} "
        f"channel {origin.channel_id} ({origin.topic}, profile {origin.profile!r})"
    )
