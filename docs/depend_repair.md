# Dependency repair

`cdflib.depend_repair` repairs wrong `DEPEND_n` attributes without touching any
measurement. It is meant for the case where a data provider suspects that some
dependencies point at the wrong variable, can supply substitutable coordinates
and the axis permutations it will accept, and needs a defensible answer to
"which attributes may I change, and what happens to everything else".

## Why length checks are not enough

Two coordinates can have the same length and still be different axes. In the
FAST ESA sample file both the pitch-angle axis (64 values) and a 64-bin index
are present, and a 64 x 96 spectrogram would accept either on shape alone.
Descriptive metadata can also be copied along with a wrong attribute, so the
repair checks the whole file:

- target existence, numeric/time type and usable axis length,
- the size of the axis the `DEPEND_n` covers,
- the coordinate family implied by `LABL_PTR_n`, `COMPONENT_0`/`ARR_DIM` for
  virtual variables, and `DISPLAY_TYPE` axis hints,
- the coordinate's actual values (angles stay in an angular range, energies are
  non-negative, indices are integral),
- record counts and the coordinate's own `DEPEND_0`,
- label-pointer extents, duplicate coordinates on one variable, orphan label
  pointers, and dependency cycles,
- virtual-variable shape rules (`arr_slice`, `alternate_view`).

## Cost

Cost is the number of `DEPEND_n` attributes whose value changes. A repair is
"minimal" when no legal plan changes fewer attributes. Only the consensus of
all minimal plans is applied: if two equally cheap plans disagree about an
edge, that edge is reported as a candidate and left alone. When no plan exists,
conflicting edges are reported with the reason each candidate was rejected.

## Provider specification

```json
{
  "suspects": [
    {"variable": "flux", "axis": 1, "candidates": ["pitch_angle", "energy_axis"]}
  ],
  "permutations": {"flux": [[1, 2]]},
  "allow_current": true
}
```

- `suspects`: the edges that may be edited. Candidate lists are substitutable
  coordinates for that axis; omit `candidates` to let every non-metadata
  variable be considered.
- `permutations`: groups of axes on the same variable that may be exchanged.
  Coordinates listed for any member of a group become candidates for the
  others, and original targets inside the group may be swapped.
- `allow_current`: when `false`, every suspect must change, for cases where the
  provider knows the recorded metadata is wrong even though it validates.

Without a specification the tool only nominates edges that already fail a
check, so a clean file is never rewritten.

## Command line

```bash
python -m cdflib.depend_repair analyze file.cdf --provider suspects.json
python -m cdflib.depend_repair repair file.cdf -o fixed.cdf --provider suspects.json
```

`repair` writes the CDF and a manifest (default `<output>.manifest.json`),
then re-reads the output and verifies it before returning success. Exit code
`0` means verified, `1` means infeasible (nothing written), `2` means the
written file failed verification.

## What is written

Only the consensus `DEPEND_n` values change. Each changed variable keeps the
previous target in `DEPEND_<n>_ORIGINAL`, and the file gains:

- `DEPEND_REPAIR_APPLIED`
- `DEPEND_REPAIR_SOURCE` and `DEPEND_REPAIR_SOURCE_SHA256`
- `DEPEND_REPAIR_PLAN` (changes, originals, timestamp)
- `DEPEND_REPAIR_CANDIDATES` and `DEPEND_REPAIR_CONFLICTS`

The manifest records the source and output hashes, the before/after dependency
graph, how many variables referenced each coordinate, and a SHA-256 per
variable (plus per record by default) for every observation. `verify_repair`
compares each observation, every non-repair attribute, the dependency graph and
an xarray import against the source and manifest, so "all original
observations and their sources are still traceable" is an assertion, not a
promise.

## Python API

```python
from cdflib.depend_repair import DependencyModel, ProviderSpec, plan_repairs, apply_repairs, verify_repair

model = DependencyModel.from_cdf("file.cdf")
spec = ProviderSpec.from_json("suspects.json")
result = plan_repairs(model, spec)
manifest = apply_repairs(model, result, "fixed.cdf", manifest_path="fixed.json")
report = verify_repair(model, "fixed.cdf", manifest)
assert report["ok"]
```

`plan_repairs` returns `minimum_cost`, `plan_count`, `determined` (safe to
apply), `candidates` (open), `conflicts` (when infeasible) and
`search_exhausted` (when the plan cap was hit, in which case nothing is
considered determined).
