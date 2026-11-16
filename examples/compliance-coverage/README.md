# MIB compliance collection coverage

This example turns three locked inputs into a de-duplicated SNMP polling
set and a per-object coverage report whose every conclusion is traceable
to a declaration.

## Inputs

- `policy.json` — the compliance policy translated from the MIB
  `MODULE-COMPLIANCE` statements. Compiled MIB modules only retain group
  references; the conditional wording of optional `GROUP` clauses and
  object-level `MIN-ACCESS` clauses are captured here.
- `device.json` — the device declaration: claimed compliance statements,
  an explicit answer (supported / condition active) for every group, and
  `AGENT-CAPABILITIES` style access refinements.
- `metrics.json` — the metrics this delivery must collect.

The locked MIB definitions themselves (OIDs, object kinds, `MAX-ACCESS`,
table indexing) come from the compiled modules in `pysnmp/smi/mibs` and
are never duplicated in the JSON inputs.

## Run

```
python -m pysnmp.tools.coverage \
    --policy policy.json \
    --device device.json \
    --metrics metrics.json \
    --output-dir output
```

## Requirement classes

- **Mandatory groups** — un-conditional `GROUP` clauses of a claimed
  compliance. Every member object must be pollable; one unsupported
  member fails the whole group.
- **Conditional optional groups** — bind the device only while their
  condition is active. The device declaration answers each condition
  explicitly. An inactive condition is a traceable *waiver* (verdict
  `waived`, excluded from the polling set), not coverage and not a
  failure.
- **Minimum access levels** — each clause carries a `MIN-ACCESS`
  (default `read-only`). The object's effective minimum is the highest
  level across all clauses requiring it, and the device's effective
  `MAX-ACCESS` must meet it. A deficit yields `partial` with the exact
  clause cited as evidence.

## Table columns vs. instances

A column OID is a schema node, not an instance. A single `GET` against a
column root returns `noSuchInstance` and cannot prove the table is
populated. Columns therefore produce `WALK` entries (GETNEXT/GETBulk over
the column subtree); each returned varbind is one row instance, with the
index decoded from the instance OID suffix. `not-accessible` index
columns (e.g. `snmpTargetAddrName`) have no instances of their own and
are reported `unsatisfied` if demanded directly.

## De-duplication and traceability

An object required by several groups (or a group plus an OBJECT clause
plus a delivery metric) appears **once** in `polling-set.json`, and its
`dedup_sources` lists every requiring declaration. In
`coverage-report.json` / `coverage-report.md` each requirement keeps its
own evidence row with its requirement class, condition state and
verdict, so every coverage conclusion can be traced back to its
declaration basis.

## Verdicts

- `satisfied` — all active requirements met and pollable.
- `partial` — an active requirement is weakened (unsupported group,
  access deficit, or a required metric with no active declaration whose
  instance existence must be verified at runtime).
- `unsatisfied` — no pollable instance (e.g. `not-accessible` index
  column).
- `waived` — only inactive conditional requirements apply.
