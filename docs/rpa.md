# RPA Stream Collection

RPA collectors typically want PowerShell output, errors and warnings in separate
tables. PowerShell does not make that trivial: a command may merge streams
(`2>&1`), discard them (`2>$null`) or simply emit an
`System.Management.Automation.ErrorRecord` as ordinary output. The helpers in
`psrpcore._rpa` keep the evidence required to handle those cases without
guessing.

## Two Kinds of Evidence

`StreamCollector` stores evidence, never assumptions:

* The **declaration** (`PipelineDeclaration`) is built from the
  `PowerShell` pipeline metadata and records, per statement and command, which
  streams stay native, are merged into output or are discarded to `$null`.
  Redirections written inside script text are not visible over PSRP and are
  listed under `declaration_limits`.
* The **records** (`CollectedRecord`) are built only from events that were
  actually received. Each row stores the PSRP channel it arrived on
  (`observed_stream`), the provable origin (`proven_origin`), the origins that
  cannot be ruled out (`uncertain_origins`) and a non-authoritative
  `shape_hint` guessed from the payload's type names.

A dedicated `ErrorRecordEvent` proves an error stream record. An
`ErrorRecord` arriving on the output stream does not: when no merge is
declared it is provably output, when a merge is declared its origin stays
ambiguous, and its error-like shape is only recorded as a hint.

## Discarded Streams Are Not Fabricated

A stream redirected to `$null` produces no PSRP events. The collector never
invents rows for it; the declaration exposes the stream under
`discarded_streams` instead so queries can distinguish "dropped remotely" from
"observed zero records".

## Querying the Outcome

`StreamCollector.result()` returns a `PipelineResult`. Use
`classify_failure()` to get a defensible conclusion:

| Classification | Meaning |
| --- | --- |
| `explicit_failure` | The pipeline reached the `Failed` state with a reason. |
| `error_records_without_failure` | Real error stream records, but the pipeline completed. |
| `error_shaped_output_only` | Only output objects that look like errors; no error stream proof. |
| `discarded` | The error stream was redirected to `$null`. |
| `clean` | No error evidence after a terminal state. |
| `incomplete` | No terminal pipeline state observed yet. |

## Example

```python
collector = psrpcore.StreamCollector()

# Feed every event returned by runspace.next_event() into the collector.
# CreatePipelineEvent events register the declaration automatically.
while event := runspace.next_event():
    row = collector.handle_event(event)
    if row is not None:
        persist(row.to_row())

result = collector.result(pipeline_id)
if result.classify_failure() == psrpcore.FailureClassification.ExplicitFailure:
    alert(result.failure_reason)
```
