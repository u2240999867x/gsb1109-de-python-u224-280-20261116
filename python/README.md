# MCAP Python Library

The `mcap` library provides classes for reading and writing the MCAP file format.

## Merging files and capture directories

`mcap.merge` combines several MCAP files, or the files of a capture directory, into one
output while keeping each declaration's origin.

```python
from mcap.merge import merge_directory, merge_to_directory

# One output beside the inputs, with a manifest recording every source.
result = merge_directory("capture/")
print(result.profile_for_channel(1))

# Files with different header profiles are never combined; write one per profile.
for result in merge_to_directory(["capture-a/", "capture-b/"], "merged/"):
    print(result.profile, result.output_path)
```

Schema and channel ids restart at 1. A schema is shared only when its name, encoding
and data bytes are identical; every source channel keeps its own output channel so each
message traces back to its original channel and profile. Payloads are copied without
decoding, so unknown profiles and encodings round-trip unchanged. The mapping is
returned as `MergeResult` and written to the output as the `mcap.merge.manifest`
metadata record (read it with `read_merge_manifest()`).

## Semantic Versioning Notes

Each python package in this repository contains a `__version__` string attribute in the root module:

```python
from mcap import __version__
print(__version__)
```

Modules, variables, classes, attributes etc. with names preceded by a single `_` underscore are not considered part of the public API and may change between minor or patch releases.
