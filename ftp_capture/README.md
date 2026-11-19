# ftp_capture

Replays a multi-session FTP packet capture and attributes each data
connection to the control session and transfer that negotiated it.

The tool is read-only and dependency free: it parses pcap/pcapng itself
and works on macOS/Linux captures of `ftp`/`ftps` control channels with
mixed active (`PORT`, `EPRT`) and passive (`PASV`, `EPSV`) transfers.

## Usage

```bash
python3 -m ftp_capture capture.pcap
python3 -m ftp_capture capture.pcapng -f json -o report.json
python3 -m ftp_capture capture.pcap --strict   # exit 2 on unresolved links
```

The text report lists every control session, its negotiations, its
transfers and the data connection bound to each. JSON output exposes the
same data for downstream processing.

## Attribution rules

A data connection is bound **exactly** only when one control session and
one negotiation explain it:

- Passive: the server endpoint announced in `227`/`229` is an endpoint of
  the data connection, the data connection is opened by that session's
  client, and its first packet falls inside the negotiation window.
- Active: the client endpoint announced in `PORT`/`EPRT` is an endpoint of
  the data connection and the connection is opened by the FTP server.
- The negotiation is closed by the next negotiation on the same session or
  by the last reply of the transfer that consumes it.

Anything weaker is reported instead of guessed:

- `candidate`: two sessions negotiated the same endpoint, only a port
  matched (NAT/masquerade), or the endpoint was already consumed.
- `unattributed`: no control session explains the connection at all.
- `negotiation_failures`: `4xx`/`5xx` replies to `PORT`/`EPRT`/`PASV`/`EPSV`.
- `missing_data_channels`: transfer commands that never got a data
  connection (completed, failed, unanswered or still open).

Candidate and unattributed bytes are never added to a session total; each
session reports them separately as `bytes_unverified`.

## Behaviour on incomplete captures

- TCP streams are reassembled per connection instance. A new SYN on the
  same 4-tuple creates a new instance, so a reused port never contributes
  bytes to the earlier transfer.
- Sequence gaps are kept as zero-filled holes and listed as session
  issues; byte counts are lower bounds in that case.
- Duplicate and overlapping segments are counted once; conflicting
  retransmissions are reported.
- Connections captured mid-stream are marked as such.

## Library use

```python
from ftp_capture import analyze_file

report = analyze_file("capture.pcap")
for session in report["sessions"]:
    print(session["client"], session["bytes_download"], session["bytes_upload"])
```

`ftp_capture.synthetic` builds in-process test captures covering mixed
modes, reused ports, failed negotiations, missing channels, NAT replies
and ambiguous endpoints; `tests/test_ftp_capture.py` uses it as ground
truth.
