import sys

from mcap.merge import merge_to_directory

# Files with different profiles cannot share an output. Write one merged file per
# profile into the destination directory instead.
results = merge_to_directory(
    ["capture-a", "capture-b"] if len(sys.argv) == 1 else sys.argv[1:],
    "merged",
)
for result in results:
    print(f"{result.profile!r}: {result.output_path} ({result.message_count} messages)")
