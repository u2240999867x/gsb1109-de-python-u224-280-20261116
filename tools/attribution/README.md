# Masked-directory contact attribution

Reconciles two phone-number directories without ever inventing numbers:

- a **partial directory** whose rows contain masked digits (`?` for one
  digit, `*` for any run of digits, `X`/`x` as an alternative single-digit
  mask),
- a **complete directory** of concrete contact points,
- a **record-range table** giving the inclusive number of source records
  each complete contact point may correspond to.

Associations are derived only from contacts that actually exist in the
complete directory; masked positions are matched as digit globs, so the
solver never enumerates a number that is not already listed.

## Matching rules

A masked entry can only match a complete contact when **all** of these hold:

1. **Region** — the region (and calling code) agrees.  Shared calling codes
   such as `+39` (Italy / Vatican City) are kept distinct, and a national
   number is always checked against its stated region.
2. **Valid leading zeros** — leading zeros are interpreted per region:
   - a national *trunk prefix* zero (e.g. Germany's `030 …`) is valid in
     national form but illegal in E.164 international form (`+49 30 …`);
   - an *embedded* zero (e.g. Italy's `+39 06 …`, Ivory Coast's
     `+225 07 …`) is part of the significant number and must be present;
   - duplicated or misplaced zeros are rejected explicitly.
3. **Mask positions of the main number** — `?` matches exactly one digit and
   `*` matches any run of digits, position by position, on the E.164
   national significant number.  Matching by an equal tail digit string
   alone never merges two distinct customers; the full mask must fit.
4. **Extension masks** — the extension (`ext 42`, `x42`, `#42`) is parsed
   and glob-matched independently of the main number.  A contact without an
   extension cannot satisfy a masked entry that requires one.

## Certainty

The legal masked-to-contact edges form a bipartite graph.  Record ranges
are degree bounds on the contact side (`min_records`–`max_records`), and
every masked entry must be assigned to exactly one contact.  Feasibility is
decided with a lower-bound maximum-flow (circulation) model.

- **confirmed** — removing the edge makes every allocation infeasible, so
  the association is forced by every feasible allocation;
- **candidate** — the edge is legal, but at least one feasible allocation
  uses a different contact;
- **infeasible** — no allocation exists.  The verdict lists the conflicting
  masked entries, the neighbouring contacts, their available capacity, the
  required number of assignments, and the exact record-range constraints
  that cannot be met (including unreachable lower bounds).

## Inputs (CSV)

`partial_directory.csv`

```csv
masked_id,masked_number,region,ref
P1,+49301234????,,Berlin customer
P3,030 12345678 ext 42,DE,main number known, extension concrete
```

`complete_directory.csv`

```csv
contact_id,number,region,ref
C1,+493012345678,,Berlin main line
C5,+493012345678 ext 42,,same main number, extension 42
```

`record_ranges.csv`

```csv
contact_id,min_records,max_records
C1,0,2
C5,0,1
```

Numbers may be international (`+…`) or national (then `region` is required,
or pass `--default-region`).  Contacts that are possible but not currently
assigned in the metadata produce a warning rather than an error.

## Running

From the repository root:

```sh
.venv/bin/python -m tools.attribution \
  --partial tools/attribution/examples/partial_directory.csv \
  --complete tools/attribution/examples/complete_directory.csv \
  --extents tools/attribution/examples/record_ranges.csv \
  --out tools/attribution/examples/verdict.json
```

Exit status is `0` for a feasible verdict and `2` for an infeasible one.
The JSON verdict contains the normalized contacts and masked entries, the
`confirmed` and `candidates` associations, unmatched masked entries,
conflicts, warnings and input errors.

## Tests

```sh
.venv/bin/python -m unittest tools.attribution.tests.test_contact_attribution
```

The suite includes a randomized cross-check (300 synthetic instances)
against exhaustive brute-force allocation, covering feasibility and the
confirmed/candidate boundary.
