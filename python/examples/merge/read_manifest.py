from mcap.merge import read_merge_manifest

manifest = read_merge_manifest("merged.mcap")
if manifest is None:
    raise SystemExit("this file was not written by mcap.merge")
for source in manifest["sources"]:
    print(source["index"], source["profile"], source["path"], source["message_count"])
for channel in manifest["channels"]:
    print(
        f"output channel {channel['output_channel_id']} <- "
        f"{channel['source_path']} channel {channel['source_channel_id']}"
    )
