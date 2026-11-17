# Offline data-package audit

`offline_pack_audit.py` reconciles several offline data packages against
registries, processor definitions, archive total-size constraints, and a
local cache. It reports the lower/upper bound of the bytes that still must
be downloaded, a verifiable material allocation, and any contradictory
claims.

## Usage

```
python tools/offline_pack_audit.py manifest.json [--json]
```

Exit code is 0 when all claims are consistent, 1 when conflicts are found.
See `example_manifest.json` (consistent) and `example_conflict.json`
(contradictory). Run the tests with `pytest tools/test_offline_pack_audit.py`.

## Manifest schema

- `budget_bytes` (optional): total download budget. The report never emits
  a negative remaining budget; shortfalls are listed as conflicts or
  worst-case shortfall warnings.
- `packages[]`: each has a `name` and a `registry` of entries with
  - `path`, `hash`: identity of the original (accounting is per hash, so
    identical content shared between packages is downloaded once),
  - `size`: exact (`n`), range (`[lo, hi]`, either side may be `null`), or
    `null`/omitted for unknown,
  - `retain` (default `true`): whether the raw original must be kept,
  - `products[]`: required processed products `{processor, version}`.
- `processors[]`: `{name, version, depends_on: [{name, version}]}`.
  Requirements are expanded over the dependency closure. Products are keyed
  by (original hash, processor, version): sharing an original never shares
  products across versions. An original is only downloaded when some
  product to be generated consumes it directly (no dependencies).
- `archives[]`: `{name, members: [path...]}` plus a total constraint:
  `total_size` (equality), `max_total_size`, and/or `min_total_size`.
  Members must belong to at most one archive.
- `cache`:
  - `originals[]`: `{path, hash, verified}`. Only verified entries whose
    hash matches a needed original are deducted; same-name unverified
    entries are listed under `cache_not_deducted`.
  - `products[]`: `{original_hash, processor, version, usable}`. A usable
    cached product satisfies that requirement without the original; if the
    original was reclaimed this is reported as usable with no download.

## Report

- `download_bytes`: `{min, max}` (`max` may be `"unbounded"`), the exact
  extremal download totals under which every declared claim holds.
- `files[]`: per-original size range, download flag, cache status, and the
  byte allocation achieving each bound (`alloc_min` / `alloc_max`), so the
  bounds can be checked against every claim.
- `products.to_generate` / `products.usable_from_cache`: pending and
  ready products; `original_downloaded` separates usability of a cached
  product from the download need of its original.
- `conflicts`: mutually contradictory claims (size intersections, archive
  totals, budget shortfalls). When non-empty, no bounds or remaining
  budget are emitted.
