"""Human-readable and JSON rendering of a correlation report."""

import json
from datetime import datetime
from datetime import timezone


def _ts(value):
    if value is None:
        return "-"
    moment = datetime.fromtimestamp(value, tz=timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _size(count):
    if count is None:
        return "-"
    units = ["B", "KiB", "MiB", "GiB"]
    value = float(count)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{count} B"


def to_json(report):
    return json.dumps(report, indent=2, ensure_ascii=False)


def to_text(report):
    lines = []
    capture = report["capture"]
    totals = report["totals"]
    lines.append("CAPTURE")
    if capture["path"]:
        lines.append(f"  file              {capture['path']}")
    lines.append(
        f"  frames            {capture['frames']} "
        f"(decoded {capture['frames_decoded']}, "
        f"ignored {capture['frames_ignored']}, "
        f"truncated {capture['frames_truncated']})"
    )
    lines.append(f"  tcp connections   {capture['connections']}")
    lines.append("")
    lines.append("TOTALS")
    lines.append(f"  control sessions        {totals['sessions']}")
    lines.append(f"  data connections        {totals['data_connections']}")
    lines.append(
        f"  attributed exactly      {totals['attributed_data_connections']}"
    )
    lines.append(
        f"  candidate (not counted) {totals['candidate_data_connections']}"
    )
    lines.append(
        f"  unattributed            {totals['unattributed_data_connections']}"
    )
    lines.append(
        f"  bytes attributed        {_size(totals['bytes_attributed'])}"
    )
    lines.append(
        f"  bytes candidate         {_size(totals['bytes_candidate'])}"
    )
    lines.append(
        f"  bytes unattributed      {_size(totals['bytes_unattributed'])}"
    )
    lines.append("")

    for session in report["sessions"]:
        lines.append(
            f"SESSION S{session['id']}  {session['client']} -> "
            f"{session['server']}  "
            f"(control conn {session['control_connection']})"
        )
        lines.append(
            f"  window   {_ts(session['started_at'])} .. "
            f"{_ts(session['ended_at'])}"
        )
        lines.append(
            f"  bytes    download {_size(session['bytes_download'])} | "
            f"upload {_size(session['bytes_upload'])}"
        )
        unverified = session.get("bytes_unverified", 0)
        if unverified:
            lines.append(
                f"  excluded {_size(unverified)} in candidate channels "
                "(normally this would be a guess; not added to totals)"
            )
        if session.get("bytes_complete") is False:
            lines.append(
                "  note     attributed bytes are a lower bound: the capture "
                "has sequence gaps or truncated frames"
            )
        if session["negotiations"]:
            lines.append("  negotiations")
            for neg in session["negotiations"]:
                endpoint = neg["endpoint"] or "-"
                lines.append(
                    f"    n{neg['id']:<3} {neg['command']:<5} "
                    f"{neg['mode']:<7} {neg['status']:<8} {endpoint:<22} "
                    f"conn={neg['data_connections'] or '-'}"
                )
                if neg["reason"]:
                    lines.append(f"         reason: {neg['reason']}")
        if session["transfers"]:
            lines.append("  transfers")
            for transfer in session["transfers"]:
                argument = transfer["argument"].strip()
                label = (
                    f"{transfer['command']} {argument}".strip()
                    if argument
                    else transfer["command"]
                )
                lines.append(
                    f"    t{transfer['id']:<3} {label:<28} "
                    f"{transfer['status']:<22} "
                    f"neg={transfer['negotiation'] or '-'} "
                    f"conn={transfer['data_connection'] or '-'}"
                )
                if transfer.get("candidate_data_connections"):
                    lines.append(
                        "         candidate conn(s): "
                        + ", ".join(
                            str(cid)
                            for cid in transfer["candidate_data_connections"]
                        )
                    )
                if transfer["reason"]:
                    lines.append(f"         reason: {transfer['reason']}")
        if session["issues"]:
            lines.append("  issues")
            for issue in session["issues"]:
                lines.append(f"    {issue}")
        lines.append("")

    events = report["events"]

    if events["negotiation_failures"]:
        lines.append("EVENT  negotiation failures")
        for item in events["negotiation_failures"]:
            lines.append(
                f"  S{item['session']} n{item['negotiation']} "
                f"{item['command']} -> {item['code']} ({item['status']}): "
                f"{item['command_line'].strip()}"
            )
            if item["reason"]:
                lines.append(f"      {item['reason']}")
        lines.append("")

    if events["missing_data_channels"]:
        lines.append("EVENT  transfers without a data channel")
        for item in events["missing_data_channels"]:
            label = f"{item['command']} {item['argument']}".strip()
            lines.append(
                f"  S{item['session']} t{item['transfer']} {label!r} "
                f"status={item['status']} code={item['code']}"
            )
            lines.append(f"      {item['reason']}")
        lines.append("")

    if events.get("unused_negotiations"):
        lines.append("EVENT  negotiations with no captured transfer")
        for item in events["unused_negotiations"]:
            lines.append(
                f"  S{item['session']} n{item['negotiation']} "
                f"{item['command']} {item['mode']} {item['endpoint']} "
                f"(data conn {item['data_connections'] or '-'})"
            )
        lines.append("")

    if events["connection_reuse"]:
        lines.append("EVENT  connection reuse")
        for item in events["connection_reuse"]:
            if item["kind"] == "port-reuse":
                lines.append(
                    f"  4-tuple reused: {item['endpoints'][0]} "
                    f"as connections {item['connections']}"
                )
            else:
                lines.append(
                    f"  S{item['session']} n{item['negotiation']} served by "
                    f"connections {item['connections']}"
                )
            lines.append(f"      {item['note']}")
        lines.append("")

    if events["ambiguous"]:
        lines.append("EVENT  candidate data connections (not attributed)")
        for item in events["ambiguous"]:
            lines.append(
                f"  conn {item['data_connection']} "
                f"c2s={_size(item['bytes_c2s'])} "
                f"s2c={_size(item['bytes_s2c'])}"
            )
            for candidate in item["candidates"]:
                lines.append(
                    f"      S{candidate['session']} "
                    f"n{candidate['negotiation']} "
                    f"{candidate['mode']} {candidate['endpoint']}"
                )
            lines.append(f"      {item['note']}")
        lines.append("")

    if events["unattributed"]:
        lines.append("EVENT  unattributed data connections")
        for item in events["unattributed"]:
            lines.append(
                f"  conn {item['data_connection']} "
                f"c2s={_size(item['bytes_c2s'])} "
                f"s2c={_size(item['bytes_s2c'])}"
            )
            lines.append(f"      {item['note']}")
        lines.append("")

    if capture["decode_error_samples"]:
        lines.append("EVENT  decode errors (sample)")
        for item in capture["decode_error_samples"]:
            lines.append(f"  frame {item['frame']}: {item['reason']}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def summarize_text(report):
    """One-screen summary used by the CLI."""

    totals = report["totals"]
    return (
        f"{totals['sessions']} sessions, {totals['data_connections']} data "
        f"connections: {totals['attributed_data_connections']} attributed, "
        f"{totals['candidate_data_connections']} candidate, "
        f"{totals['unattributed_data_connections']} unattributed; "
        f"{totals['negotiation_failures']} negotiation failures, "
        f"{totals['missing_data_channels']} transfers without a channel"
    )
