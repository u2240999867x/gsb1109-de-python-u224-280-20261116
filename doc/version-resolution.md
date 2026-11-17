# Resolving a shortcut to a target version

`LnkParse3.version_resolution` answers a narrower question than “what path does
this LNK contain”: **which recorded version of the target does this shortcut
actually point at?** The answer is derived from the shortcut bytes plus a
catalog of volume history and an explicit LNK-to-catalog identity mapping.

```python
import LnkParse3

with open("shortcut.lnk", "rb") as handle:
    resolution = LnkParse3.resolve_target_version(
        handle,
        "catalog.json",
        rules="identity-rules.json",      # optional
        collected_at="2026-11-16T00:00:00Z",  # optional override
    )

print(resolution.status)          # unique | multiple | contradiction | unresolved
print(resolution.target_version)  # version name, or None
print(resolution.as_dict())       # full evidence, including adopted/excluded fields
```

CLI:

```console
$ lnkparse shortcut.lnk --resolve-version catalog.json \
      --identity-rules identity-rules.json --collected-at 2026-11-16T00:00:00Z
```

## Results

| status | meaning |
|---|---|
| `unique` | exactly one version remains compatible and no evidence contradicts it |
| `multiple` | several versions remain compatible; none is selected by path or ordering |
| `contradiction` | identity, size, time, presence or the shortcut's own fields disagree |
| `unresolved` | no version can be connected with the supplied identity rules |

`target` is populated only for `unique`. Every candidate, including excluded
ones, carries `checks[]`; `adopted_fields` lists matching identity/size/time
comparisons and `excluded_fields` lists mismatch and conflict comparisons. Each
entry keeps both the original LNK field (`lnk_field`, `lnk_raw`) and the
original catalog field (`catalog_field`, `catalog_raw`, `catalog_location`).

## Catalog shape

`catalog` may be a mapping, a path to one JSON document, or a path to a
volume-history **directory**.

For a directory:

- `catalog.json` (if present) is the authoritative manifest; other files are
  ignored.
- Otherwise every top-level `*.json` file is merged, in filename order.
- A file may contain `volumes`, one `volume` object with nested `entries`, a
  `volume_id`/`volume` plus an `entries` list, or a single version object.
- Repeating the same volume identity across per-version files is allowed as
  long as its identifiers agree; conflicting identifiers are rejected.
- Conflicting collection timestamps, duplicate volume IDs with different
  identities, or two files declaring the same version on the same volume are
  rejected with `EvidenceError` instead of being resolved silently.

Both nested and flat entry lists are accepted inside a single document.

```json
{
  "collected_at": {"value": "2026-11-16T00:00:00Z", "precision": "day"},
  "complete": true,
  "volumes": [
    {
      "volume_id": "evidence-volume-3",
      "label": "WIN7",
      "identifiers": {
        "serial": "0x489e5fb3",
        "droid_volume_id": "08562B6A-355D-4AFF-8B67-66F02405E12B"
      },
      "entries": [
        {
          "version": "4.4",
          "path": "C:\\Youdao\\ShoppingAssistant\\ie\\4.4\\app.exe",
          "size": 756160,
          "times": {
            "modified": {"value": "2015-07-13T10:59:48Z", "precision": "2s"}
          },
          "identifiers": {
            "droid_file_id": "E397F6C7-7601-11E5-95A1-BCEE7B27846D"
          },
          "presence": {
            "first_seen": {"value": "2015-07-01", "precision": "day"},
            "last_seen": {"value": "2019-12-31", "precision": "day"},
            "removed_at": {"value": "2019-12-31", "precision": "day"},
            "observed_at_collection": false
          }
        }
      ]
    }
  ]
}
```

Recognized aliases: `versions` for `entries`, `ids` for `identifiers`,
`file_size`/`byte_size` for `size`, `full_path` for `path`, and
`droid_file_identifier`/`droid_volume_identifier` for the droid IDs.

`collected_at` (or `collection_time`) is the only date treated as collection
time. It may be overridden with `collected_at=`; it is never inferred from the
shortcut.

## Identity rules

Rules map a parsed LNK field to a catalog field. Each rule has `lnk`,
`catalog`, an optional `normalize`, and a human-readable `label`.

```json
{
  "volume": [
    {
      "lnk": "link_info.location_info.drive_serial_number",
      "catalog": "volume.identifiers.serial",
      "normalize": "hex32",
      "label": "volume serial number"
    }
  ],
  "object": [
    {
      "lnk": "extra.DISTRIBUTED_LINK_TRACKER_BLOCK.droid_file_identifier",
      "catalog": "entry.identifiers.droid_file_id",
      "normalize": "uuid",
      "label": "droid file identifier"
    }
  ],
  "size": [
    {"lnk": "header.file_size", "catalog": "entry.size"},
    {"lnk": "target.file_entry.file_size", "catalog": "entry.size"}
  ],
  "time": [
    {
      "lnk": "header.modified_time",
      "catalog": "entry.times.modified",
      "precision": "auto"
    },
    {
      "lnk": "target.file_entry.modification_time",
      "catalog": "entry.times.modified",
      "precision": "auto"
    }
  ],
  "path": [
    {"lnk": "path.full", "catalog": "entry.path", "normalize": "path"}
  ]
}
```

Selectors are dotted paths over the parsed LNK JSON. `target.file_entry` refers
to the final `File entry` shell item; `path.full` joins LinkInfo base path (or
the network `net_name`) with the common path suffix.

Normalizers: `hex32`, `uuid`, `path`, `text`, `number`, and `identity` (default;
case-insensitive string).

Omitted sections fall back to the defaults above. Unknown sections are rejected
so a typo cannot silently weaken the rules.

## Decision model

- **Object before volume.** A matching droid file identifier (`object`) is a
  strong identity signal. A matching volume identifier alone is weaker and
  needs size or time corroboration to be treated as `medium`; with neither it is
  `ambiguous`, which yields `multiple` rather than a made-up unique answer.
- **Hard contradictions exclude.** A non-zero saved size that differs from the
  catalog size, or a saved time that differs at comparable precision, excludes
  the candidate. The shortcut header size and the final shell file-entry size
  are compared independently; if they disagree with each other, that is a
  `shortcut_internal_conflict` regardless of the catalog.
- **Identifiers outrank proximity.** A conflicting object identifier is not a
  near miss; it identifies another object and is excluded even if path and time
  happen to fit.
- **Path is corroboration only.** A matching path never establishes identity.
  A newer catalog version that arrived at the same path after the shortcut was
  written is recorded under `presence.later_versions_at_path` on the older
  candidate and does not inherit the old relation.
- **Presence is separate from declared time.** `presence.first_seen` /
  `last_seen` / `removed_at` are compared with the shortcut write time to decide
  whether the version existed when the shortcut was saved. Whether it survived
  to collection is decided only from `collected_at`, `removed_at`,
  `last_seen` or `observed_at_collection`. A shortcut timestamp that is older
  than collection proves nothing about survival.

## Time precision

Precision is declared per timestamp: `100ns`, `1us`, `10us`, `1ms`, `10ms`,
`1s`, `2s`, `1min`, `1h`, `day`, `month`, `year`.

- Header FILETIMEs are read as `datetime` and therefore compared at no finer
  than `1us`, even though the on-disk field is 100 ns.
- Shell file-entry modification times come from a DOS timestamp, which stores
  seconds in two-second increments, so they are compared at `2s`.
- Two values are compared at the coarser of their two precisions. Sub-day
  values agree when their uncertainty intervals overlap (half the declared
  granularity on each side, summed), so `2s` and `1us` values agree within
  about one second. Calendar values must fall in the same day/month/year
  bucket.
- A catalog row without an explicit precision is inferred narrowly: `YYYY` is
  `year`, `YYYY-MM` is `month`, `YYYY-MM-DD` is `day`, microsecond input is
  `1us`, otherwise `1s`.

## Contradiction kinds

| kind | meaning |
|---|---|
| `catalog_volume_conflict` | one volume identifier is claimed by several catalog volumes |
| `catalog_object_conflict` | one object identifier is claimed by several versions |
| `identity_disagreement` | the object identifier and volume identifier resolve to different versions |
| `identity_contradicted` | an object-ID match is excluded by size, time or presence |
| `identity_conflict` | an entry carries the object ID but a non-matching volume ID |
| `shortcut_internal_conflict` | the shortcut declares different target sizes in different fields |

A `catalog_object_conflict` produces `contradiction` when it involves a
candidate that would otherwise be selected. A duplicate entirely elsewhere in
the catalog is still listed under `contradictions`, but it does not block a
well-identified target and is summarized in `warnings`.
