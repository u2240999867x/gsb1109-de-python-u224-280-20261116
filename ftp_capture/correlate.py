"""Correlation engine: bind data connections to FTP control sessions.

Rules enforced here:

* A data connection is bound only when a single negotiation on a single
  control session explains it.  Anything weaker stays a candidate and
  its bytes are never added to a session total.
* Passive negotiatons are identified by the server endpoint announced in
  ``227``/``229``; active ones by the client endpoint announced in
  ``PORT``/``EPRT``.  Timing windows are bounded by the next negotiation
  and by the transfer that consumes the channel.
* Negotiation failures, transfers whose data channel never appears, and
  connection/port reuse are reported as their own event classes.
"""

from dataclasses import dataclass
from dataclasses import field

from . import ftp
from .pcap import read_capture
from .tcp import build_connections


@dataclass
class Negotiation:
    nid: int
    session: object
    exchange: object
    mode: str  # "passive" | "active"
    command: str
    status: str  # "ok" | "failed" | "unparsed" | "no_reply"
    endpoint: tuple = None
    reason: str = None
    open_ts: float = None
    close_ts: float = None
    transfers: list = field(default_factory=list)
    data_links: list = field(default_factory=list)


@dataclass
class Transfer:
    tid: int
    session: object
    exchange: object
    negotiation: Negotiation = None
    status: str = "unknown"
    reply_code: int = None
    data_link: object = None
    candidate_links: list = field(default_factory=list)
    reason: str = None

    @property
    def command(self):
        return self.exchange.command

    @property
    def name(self):
        return self.command.verb


@dataclass
class DataLink:
    data: object
    session: object = None
    negotiation: Negotiation = None
    confidence: str = "unattributed"  # exact | candidate | unattributed
    candidates: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    bytes_c2s: int = 0
    bytes_s2c: int = 0
    bytes_client_to_server: int = 0
    bytes_server_to_client: int = 0
    bytes_complete: bool = True
    gaps: list = field(default_factory=list)
    note: str = None
    reuse_suspected: bool = False

    @property
    def total_bytes(self):
        return self.bytes_c2s + self.bytes_s2c


@dataclass
class Session:
    sid: int
    control: object
    client: tuple
    server: tuple
    commands: list
    replies: list
    exchanges: list
    greeting: list
    negotiations: list = field(default_factory=list)
    transfers: list = field(default_factory=list)
    data_links: list = field(default_factory=list)
    issues: list = field(default_factory=list)

    @property
    def started_at(self):
        return self.control.first_ts

    @property
    def ended_at(self):
        return self.control.close_ts


def _control_score(command_dir, reply_dir):
    """Score how well ``(command_dir, reply_dir)`` looks like FTP."""

    if command_dir is None or reply_dir is None:
        return -1
    if not ftp.looks_like_control(command_dir.data, "commands"):
        return -1
    if not ftp.looks_like_replies(reply_dir.data):
        return -1
    score = 1
    commands = ftp.parse_commands(command_dir)
    replies = ftp.parse_replies(reply_dir)
    known = sum(1 for c in commands if c.verb in ftp.COMMANDS)
    score += min(known, 10) / 10
    if reply_dir.data.lstrip().startswith(b"220"):
        score += 3
    elif reply_dir.data and reply_dir.data[0:1].isdigit():
        score += 0.5
    if any(c.verb in ("USER", "PASS") for c in commands):
        score += 1
    if any(c.verb in ftp.TRANSFER_COMMANDS for c in commands):
        score += 0.5
    # A reply stream that is mostly 3-digit codes is stronger evidence than
    # one accepted only through continuation lines.
    total = len(replies) or 1
    numeric = sum(1 for r in replies if r.code)
    score += numeric / total
    return score


def _is_control_connection(conn):
    if conn.fwd is None or conn.rev is None:
        return None
    forward = _control_score(conn.fwd, conn.rev)
    reverse = _control_score(conn.rev, conn.fwd)
    best = max(forward, reverse)
    if best < 1.5:
        return None
    if forward == reverse:
        # Prefer the direction whose first reply looks like a 220 greeting.
        if conn.rev.data.lstrip().startswith(b"220"):
            return "fwd"
        if conn.fwd.data.lstrip().startswith(b"220"):
            return "rev"
        return "fwd"
    return "fwd" if forward > reverse else "rev"


def _build_sessions(connections):
    sessions = []
    control_ids = set()
    for conn in connections:
        side = _is_control_connection(conn)
        if side is None:
            continue
        control_ids.add(conn.cid)
        if side == "fwd":
            command_dir, reply_dir = conn.fwd, conn.rev
            client, server = conn.initiator, conn.responder
        else:
            command_dir, reply_dir = conn.rev, conn.fwd
            client, server = conn.responder, conn.initiator
        commands = ftp.parse_commands(command_dir)
        replies = ftp.parse_replies(reply_dir)
        exchanges, greeting = ftp.pair_exchanges(commands, replies)
        sessions.append(
            Session(
                sid=len(sessions) + 1,
                control=conn,
                client=client,
                server=server,
                commands=commands,
                replies=replies,
                exchanges=exchanges,
                greeting=greeting,
            )
        )
    return sessions, control_ids


def _negotiation_endpoint(session, exchange):
    """Parse the endpoint announced by a negotiation exchange."""

    command = exchange.command
    verb = command.verb
    if verb in ("PASV", "EPSV"):
        final = exchange.final
        if final is None:
            return None, "no_reply", None
        if final.code in (227, 229):
            if final.code == 227:
                parsed = ftp.parse_pasv(final.text)
            else:
                parsed = ftp.parse_epsv(final.text)
                if parsed is not None:
                    parsed = (session.server[0], parsed)
            if parsed is None:
                return None, "unparsed", "cannot parse passive reply"
            return parsed, "ok", None
        if final.code >= 400:
            return None, "failed", f"server refused: {final.code} {final.text}"
        return None, "unparsed", f"unexpected reply {final.code}"
    if verb in ("PORT", "EPRT"):
        if verb == "PORT":
            parsed = ftp.parse_port(command.argument)
        else:
            parsed = ftp.parse_eprt(command.argument)
        if parsed is None:
            return None, "unparsed", "cannot parse active command"
        final = exchange.final
        if final is None:
            return parsed, "no_reply", "no reply to active command"
        if final.code >= 400:
            return (
                parsed,
                "failed",
                f"server refused: {final.code} {final.text}",
            )
        return parsed, "ok", None
    return None, "unparsed", "not a negotiation command"


def _collect_negotiations(session):
    negotiations = []
    for exchange in session.exchanges:
        if exchange.command is None:
            continue
        if exchange.command.verb not in ftp.NEGOTIATION_COMMANDS:
            continue
        endpoint, status, reason = _negotiation_endpoint(session, exchange)
        mode = (
            "passive"
            if exchange.command.verb in ("PASV", "EPSV")
            else "active"
        )
        negotiation = Negotiation(
            nid=len(negotiations) + 1,
            session=session,
            exchange=exchange,
            mode=mode,
            command=exchange.command.verb,
            status=status,
            endpoint=endpoint,
            reason=reason,
        )
        if status == "ok":
            if mode == "passive":
                negotiation.open_ts = exchange.final.ts
            else:
                negotiation.open_ts = exchange.command.ts
        negotiations.append(negotiation)
    session.negotiations = negotiations
    return negotiations


def _collect_transfers(session):
    transfers = []
    current = None
    for exchange in session.exchanges:
        if exchange.command is None:
            continue
        verb = exchange.command.verb
        if verb in ftp.NEGOTIATION_COMMANDS:
            for negotiation in session.negotiations:
                if negotiation.exchange is exchange:
                    current = negotiation
                    break
            continue
        if verb not in ftp.TRANSFER_COMMANDS:
            continue
        # FTP requires a fresh negotiation per transfer; a second transfer
        # reusing the same channel is recorded without borrowing it.
        negotiation = current
        if negotiation is not None and negotiation.transfers:
            negotiation = None
        transfer = Transfer(
            tid=len(transfers) + 1,
            session=session,
            exchange=exchange,
            negotiation=negotiation,
        )
        transfers.append(transfer)
        if negotiation is not None:
            negotiation.transfers.append(transfer)
            current = None
    session.transfers = transfers
    return transfers


def _close_negotiation_windows(session):
    windows = session.negotiations
    for index, negotiation in enumerate(windows):
        if negotiation.open_ts is None:
            continue
        close = session.ended_at
        for transfer in negotiation.transfers:
            if transfer.exchange.final is not None:
                close = max(close, transfer.exchange.final.ts)
        if index + 1 < len(windows):
            following = windows[index + 1]
            boundary = (
                following.exchange.final.ts
                if following.mode == "passive" and following.exchange.final
                else following.exchange.command.ts
            )
            if boundary is not None:
                close = min(close, boundary)
        negotiation.close_ts = close


def _endpoint_matches(conn, endpoint):
    return endpoint in conn.endpoints


def _endpoint_port_matches(conn, endpoint):
    """Port-only match, which a NAT/masquerade address can produce."""

    if endpoint is None:
        return None
    for observed in conn.endpoints:
        if observed[1] == endpoint[1]:
            return observed
    return None


def _candidate_negotiations_for(conn, sessions, allow_port_only=False):
    matches = []
    for session in sessions:
        for negotiation in session.negotiations:
            if negotiation.status != "ok" or negotiation.endpoint is None:
                continue
            strong = _endpoint_matches(conn, negotiation.endpoint)
            weak_side = None
            if not strong:
                if not allow_port_only:
                    continue
                weak_side = _endpoint_port_matches(conn, negotiation.endpoint)
                if weak_side is None:
                    continue
            if negotiation.open_ts is not None and (
                conn.first_ts + 1e-9 < negotiation.open_ts
            ):
                continue
            if negotiation.close_ts is not None and (
                conn.first_ts - 1e-9 > negotiation.close_ts
            ):
                continue
            # Passive: the peer must be the session's client.  Active: the
            # initiator must be the server side of that session.
            if negotiation.mode == "passive":
                server_side = weak_side or negotiation.endpoint
                if (
                    conn.responder != server_side
                    and conn.initiator != server_side
                ):
                    continue
                client_side = (
                    conn.initiator
                    if conn.responder == server_side
                    else conn.responder
                )
                if client_side[0] != session.client[0]:
                    continue
            else:
                if conn.initiator[0] != session.server[0]:
                    continue
                # The announced endpoint is the client's listener, which
                # must be the far side of the connection the server opens.
                if weak_side is None:
                    if conn.responder != negotiation.endpoint:
                        continue
                elif conn.responder[1] != negotiation.endpoint[1]:
                    continue
            matches.append(
                (session, negotiation, "port-only" if weak_side else "exact")
            )
    return matches


def _bytes_of(conn):
    c2s = conn.fwd
    s2c = conn.rev
    bytes_c2s = len(c2s.data) if c2s else 0
    bytes_s2c = len(s2c.data) if s2c else 0
    complete = True
    gaps = []
    for name, direction in (("c2s", c2s), ("s2c", s2c)):
        if direction is None:
            continue
        for gap in direction.gaps:
            gaps.append({"direction": name, **gap.as_dict()})
            complete = False
        if direction.truncated_segments:
            complete = False
            gaps.append(
                {
                    "direction": name,
                    "offset": None,
                    "length": None,
                    "reason": (
                        f"{direction.truncated_segments} truncated frame(s)"
                    ),
                }
            )
    return bytes_c2s, bytes_s2c, complete, gaps


def _negotiation_matches_connection_direction(negotiation, conn):
    """Passive channels are opened by the client; active by the server."""

    if negotiation.mode == "passive":
        return conn.initiator[0] == negotiation.session.client[0]
    return conn.initiator[0] == negotiation.session.server[0]


def _link_data_connections(connections, sessions, control_ids):
    data_connections = [c for c in connections if c.cid not in control_ids]
    links = []
    for conn in data_connections:
        link = DataLink(data=conn)
        candidates = _candidate_negotiations_for(conn, sessions)
        if not candidates:
            # A NAT/masquerade reply can announce an address that never
            # appears in the capture.  A port-only match is legal evidence
            # but never enough to call the link exact.
            candidates = _candidate_negotiations_for(
                conn, sessions, allow_port_only=True
            )
        filtered = [
            (session, negotiation, how)
            for session, negotiation, how in candidates
            if _negotiation_matches_connection_direction(negotiation, conn)
        ]
        unserved = [
            (session, negotiation, how)
            for session, negotiation, how in filtered
            if not negotiation.data_links
        ]
        link.bytes_c2s, link.bytes_s2c, link.bytes_complete, link.gaps = (
            _bytes_of(conn)
        )
        # Passive channels are opened by the FTP client; active channels
        # are opened by the FTP server.  Only a single-mode candidate set
        # tells us which endpoint is the server; otherwise keep the raw
        # TCP direction and refuse to call the bytes a download.
        modes = {
            negotiation.mode
            for _session, negotiation, _how in candidates
        }
        if modes == {"active"}:
            link.bytes_client_to_server = link.bytes_s2c
            link.bytes_server_to_client = link.bytes_c2s
        elif modes == {"passive"}:
            link.bytes_client_to_server = link.bytes_c2s
            link.bytes_server_to_client = link.bytes_s2c

        strong_unserved = [
            item for item in unserved if item[2] == "exact"
        ]
        if len(candidates) == 1 and len(unserved) == 1:
            session, negotiation, how = unserved[0]
            link.session = session
            link.negotiation = negotiation
            if how == "exact":
                link.confidence = "exact"
                link.evidence.append(
                    f"negotiation {negotiation.command} on session "
                    f"{session.sid} announced "
                    f"{negotiation.endpoint[0]}:{negotiation.endpoint[1]}"
                )
            else:
                link.confidence = "candidate"
                link.negotiation = negotiation
                link.candidates = [_candidate_dict(session, negotiation)]
                link.note = (
                    "only the port matches the announced endpoint; the "
                    "server address differs (NAT/masquerade), so ownership "
                    "is not proven"
                )
            if link.confidence == "exact" and negotiation.transfers:
                link.evidence.append(
                    "transfer command(s): "
                    + ", ".join(t.name for t in negotiation.transfers)
                )
        elif strong_unserved:
            link.confidence = "candidate"
            link.candidates = [
                _candidate_dict(session, negotiation)
                for session, negotiation, _how in strong_unserved
            ]
            link.note = (
                "several control sessions negotiated this endpoint in "
                "overlapping windows; ownership is not proven"
            )
        elif unserved:
            link.confidence = "candidate"
            link.candidates = [
                _candidate_dict(session, negotiation)
                for session, negotiation, _how in unserved
            ]
            link.note = (
                "port matches an announced endpoint but the address does "
                "not; NAT/masquerade likely, ownership is not proven"
            )
        elif candidates:
            consumed = any(
                negotiation.data_links for _s, negotiation, _h in filtered
            )
            link.confidence = "candidate"
            link.candidates = [
                _candidate_dict(session, negotiation)
                for session, negotiation, _how in (filtered or candidates)
            ]
            if consumed:
                link.note = (
                    "endpoint was already consumed by another data "
                    "connection; this instance is likely a reused port"
                )
                link.reuse_suspected = True
            else:
                link.note = "direction/window mismatch"
        else:
            link.confidence = "unattributed"
            link.note = "no negotiation on any control session explains it"
        links.append(link)
        if link.confidence == "exact":
            link.negotiation.data_links.append(link)
            link.session.data_links.append(link)
    return links


def _candidate_dict(session, negotiation):
    return {
        "session": session.sid,
        "negotiation": negotiation.nid,
        "mode": negotiation.mode,
        "endpoint": (
            f"{negotiation.endpoint[0]}:{negotiation.endpoint[1]}"
            if negotiation.endpoint
            else None
        ),
        "control_connection": session.control.cid,
    }


def _mark_transfer_status(transfer):
    exchange = transfer.exchange
    final = exchange.final
    preliminary = exchange.preliminary
    if final is None:
        transfer.status = "unanswered"
        return
    transfer.reply_code = final.code
    if final.code in (125, 150):
        transfer.status = "open"
        return
    if final.code == 226:
        transfer.status = "completed"
        return
    if final.code in (225, 426):
        transfer.status = "aborted"
        return
    if final.code in (425, 426, 450, 451, 452, 550, 551, 552, 553):
        transfer.status = "failed"
        transfer.reason = f"{final.code} {final.text}"
        return
    if preliminary is not None and final.code >= 400:
        transfer.status = "failed"
        transfer.reason = f"{final.code} {final.text}"
        return
    if final.code >= 400:
        transfer.status = "failed"
        transfer.reason = f"{final.code} {final.text}"
    else:
        transfer.status = "unknown"


def _attach_transfers(sessions, links):
    exact = {}
    candidates = {}
    for link in links:
        if link.negotiation is None:
            continue
        target = exact if link.confidence == "exact" else candidates
        target.setdefault(id(link.negotiation), []).append(link)
    for session in sessions:
        for transfer in session.transfers:
            _mark_transfer_status(transfer)
            negotiation = transfer.negotiation
            if negotiation is None:
                transfer.status = "no_negotiation"
                continue
            if negotiation.status != "ok":
                transfer.status = "negotiation_failed"
                continue
            link = (exact.get(id(negotiation)) or [None])[0]
            if link is not None:
                transfer.data_link = link
                continue
            possible = candidates.get(id(negotiation))
            if possible:
                transfer.candidate_links = possible
                if transfer.status in ("completed", "open"):
                    transfer.status = "candidate_data_channel"
                continue
            if transfer.status == "completed":
                transfer.status = "data_channel_missing"


def _connection_reuse_events(connections, links):
    events = []
    by_four_tuple = {}
    for conn in connections:
        key = frozenset((conn.initiator, conn.responder))
        by_four_tuple.setdefault(key, []).append(conn)
    for key, conns in by_four_tuple.items():
        if len(conns) > 1:
            conns = sorted(conns, key=lambda c: c.first_ts)
            events.append(
                {
                    "kind": "port-reuse",
                    "endpoints": [
                        f"{c.initiator[0]}:{c.initiator[1]}" for c in conns
                    ],
                    "connections": [c.cid for c in conns],
                    "first_ts": [c.first_ts for c in conns],
                    "note": "same 4-tuple appears as multiple connection "
                    "instances; bytes are kept per instance",
                }
            )
    by_negotiation = {}
    for link in links:
        if link.negotiation is not None:
            by_negotiation.setdefault(id(link.negotiation), []).append(link)
    for negotiation_links in by_negotiation.values():
        if len(negotiation_links) > 1:
            events.append(
                {
                    "kind": "data-connection-reuse",
                    "session": negotiation_links[0].session.sid,
                    "negotiation": negotiation_links[0].negotiation.nid,
                    "connections": [
                        link.data.cid for link in negotiation_links
                    ],
                    "note": "one negotiation observed with several data "
                    "connection instances",
                }
            )
    return events


def _session_issues(session):
    """Record capture-level gaps that weaken the session's evidence."""

    issues = []
    if session.control.midstream:
        issues.append(
            "control connection captured mid-stream; earlier commands may "
            "be missing"
        )
    directions = {
        "client-to-server": session.control.direction(
            session.control.direction_name(session.client)
        ),
        "server-to-client": session.control.direction(
            session.control.direction_name(session.server)
        ),
    }
    for label, direction in directions.items():
        if direction is None:
            issues.append(f"{label} direction absent from capture")
            continue
        if direction.gaps:
            total = sum(gap.length for gap in direction.gaps)
            issues.append(
                f"{label} stream has {len(direction.gaps)} gap(s), "
                f"{total} byte(s) missing"
            )
        if direction.truncated_segments:
            issues.append(
                f"{label} stream has {direction.truncated_segments} "
                "truncated frame(s)"
            )
        if direction.conflicting_retransmissions:
            issues.append(
                f"{label} stream has {direction.conflicting_retransmissions} "
                "retransmission(s) with conflicting payload"
            )
    if session.control.close_reason == "port-reuse":
        issues.append("control 4-tuple reused before a clean close was seen")
    return issues


def analyze(frames, path=None):
    """Analyze an iterable of frames and return a report dict."""

    frames = list(frames)
    connections, stats = build_connections(frames)
    sessions, control_ids = _build_sessions(connections)
    for session in sessions:
        _collect_negotiations(session)
        _collect_transfers(session)
        _close_negotiation_windows(session)
        session.issues = _session_issues(session)

    links = _link_data_connections(connections, sessions, control_ids)
    _attach_transfers(sessions, links)

    negotiation_failures = []
    for session in sessions:
        for negotiation in session.negotiations:
            if negotiation.status == "ok":
                continue
            negotiation_failures.append(
                {
                    "session": session.sid,
                    "negotiation": negotiation.nid,
                    "command": negotiation.command,
                    "status": negotiation.status,
                    "code": negotiation.exchange.code,
                    "reason": negotiation.reason,
                    "command_line": negotiation.exchange.command.raw.decode(
                        "utf-8", "replace"
                    ),
                    "ts": negotiation.exchange.command.ts,
                }
            )

    missing_channels = []
    for session in sessions:
        for transfer in session.transfers:
            if transfer.status in (
                "data_channel_missing",
                "negotiation_failed",
                "no_negotiation",
                "open",
                "unanswered",
            ):
                missing_channels.append(
                    {
                        "session": session.sid,
                        "transfer": transfer.tid,
                        "command": transfer.name,
                        "argument": transfer.command.argument,
                        "status": transfer.status,
                        "code": transfer.reply_code,
                        "ts": transfer.command.ts,
                        "reason": _missing_reason(transfer),
                    }
                )

    unused_negotiations = []
    for session in sessions:
        for negotiation in session.negotiations:
            if negotiation.status != "ok" or negotiation.transfers:
                continue
            unused_negotiations.append(
                {
                    "session": session.sid,
                    "negotiation": negotiation.nid,
                    "command": negotiation.command,
                    "mode": negotiation.mode,
                    "endpoint": (
                        f"{negotiation.endpoint[0]}:{negotiation.endpoint[1]}"
                        if negotiation.endpoint
                        else None
                    ),
                    "ts": negotiation.exchange.command.ts,
                    "data_connections": [
                        link.data.cid for link in negotiation.data_links
                    ],
                }
            )

    ambiguous = [
        {
            "data_connection": link.data.cid,
            "bytes_c2s": link.bytes_c2s,
            "bytes_s2c": link.bytes_s2c,
            "candidates": link.candidates,
            "note": link.note,
        }
        for link in links
        if link.confidence == "candidate"
    ]

    unattributed_links = [
        {
            "data_connection": link.data.cid,
            "bytes_c2s": link.bytes_c2s,
            "bytes_s2c": link.bytes_s2c,
            "note": link.note,
        }
        for link in links
        if link.confidence == "unattributed"
    ]

    session_reports = []
    for session in sessions:
        attributed = [
            link for link in links
            if link.session is session and link.confidence == "exact"
        ]
        session_candidates = [
            link
            for link in links
            if link.confidence == "candidate"
            and any(
                candidate["session"] == session.sid
                for candidate in link.candidates
            )
        ]
        session_reports.append(
            {
                "id": session.sid,
                "client": _endpoint(session.client),
                "server": _endpoint(session.server),
                "control_connection": session.control.cid,
                "started_at": session.started_at,
                "ended_at": session.ended_at,
                "greeting": [
                    r.raw.decode("utf-8", "replace") for r in session.greeting
                ],
                "commands": len(session.commands),
                "replies": len(session.replies),
                "negotiations": [
                    _negotiation_dict(n) for n in session.negotiations
                ],
                "transfers": [_transfer_dict(t) for t in session.transfers],
                "data_connections": [link.data.cid for link in attributed],
                "candidate_data_connections": [
                    link.data.cid for link in session_candidates
                ],
                "bytes_download": sum(
                    link.bytes_server_to_client for link in attributed
                ),
                "bytes_upload": sum(
                    link.bytes_client_to_server for link in attributed
                ),
                "bytes_unverified": sum(
                    link.total_bytes for link in session_candidates
                ),
                "bytes_complete": all(
                    link.bytes_complete for link in attributed
                ),
                "issues": session.issues,
            }
        )
        _resolve_transfer_sides(session_reports[-1], session, attributed)

    unattributed = [
        {
            "data_connection": link.data.cid,
            "initiator": _endpoint(link.data.initiator),
            "responder": _endpoint(link.data.responder),
            "bytes_c2s": link.bytes_c2s,
            "bytes_s2c": link.bytes_s2c,
            "bytes_complete": link.bytes_complete,
            "gaps": link.gaps,
            "confidence": link.confidence,
            "candidates": link.candidates,
            "note": link.note,
        }
        for link in links
        if link.confidence != "exact"
    ]

    return {
        "capture": {
            "path": path,
            "frames": stats["frames_total"],
            "frames_decoded": stats["frames_decoded"],
            "frames_ignored": stats["frames_ignored"],
            "frames_truncated": stats["frames_truncated"],
            "unordered_frames": stats["unordered_frames"],
            "decode_error_samples": stats["decode_error_samples"],
            "connections": len(connections),
        },
        "sessions": session_reports,
        "data_connections": [
            _data_connection_dict(link) for link in links
        ],
        "events": {
            "negotiation_failures": negotiation_failures,
            "missing_data_channels": missing_channels,
            "unused_negotiations": unused_negotiations,
            "connection_reuse": _connection_reuse_events(connections, links),
            "ambiguous": ambiguous,
            "unattributed": unattributed_links,
        },
        "totals": {
            "sessions": len(sessions),
            "control_connections": len(control_ids),
            "data_connections": len(links),
            "attributed_data_connections": sum(
                1 for link in links if link.confidence == "exact"
            ),
            "candidate_data_connections": sum(
                1 for link in links if link.confidence == "candidate"
            ),
            "unattributed_data_connections": sum(
                1 for link in links if link.confidence == "unattributed"
            ),
            "bytes_attributed": sum(
                link.total_bytes
                for link in links
                if link.confidence == "exact"
            ),
            "bytes_candidate": sum(
                link.total_bytes
                for link in links
                if link.confidence == "candidate"
            ),
            "bytes_unattributed": sum(
                link.total_bytes
                for link in links
                if link.confidence == "unattributed"
            ),
            "negotiation_failures": len(negotiation_failures),
            "missing_data_channels": len(missing_channels),
            "unused_negotiations": len(unused_negotiations),
        },
    }


def _resolve_transfer_sides(report, session, links):
    """Replace placeholder byte counters with role-correct totals."""

    report["bytes_download"] = sum(
        link.bytes_server_to_client for link in links
    )
    report["bytes_upload"] = sum(link.bytes_client_to_server for link in links)
    report["bytes_complete"] = all(link.bytes_complete for link in links)
    if not report["bytes_complete"]:
        report["issues"].append(
            "attributed data channel has sequence gaps or truncation; "
            "byte counts are lower bounds"
        )
    return report


def _missing_reason(transfer):
    if transfer.status == "candidate_data_channel":
        return (
            "a data connection matches by endpoint but ownership is not "
            "proven; its bytes stay out of the session total"
        )
    if transfer.status == "data_channel_missing":
        return "transfer completed but no data connection was captured"
    if transfer.status == "negotiation_failed":
        return "preceding negotiation failed; no data channel expected"
    if transfer.status == "no_negotiation":
        return "transfer command without any data-channel negotiation"
    if transfer.status == "open":
        return "transfer started but never received a final reply"
    if transfer.status == "unanswered":
        return "transfer command has no reply in the capture"
    return transfer.reason


def _negotiation_dict(negotiation):
    return {
        "id": negotiation.nid,
        "command": negotiation.command,
        "mode": negotiation.mode,
        "status": negotiation.status,
        "endpoint": (
            f"{negotiation.endpoint[0]}:{negotiation.endpoint[1]}"
            if negotiation.endpoint
            else None
        ),
        "ts": negotiation.exchange.command.ts,
        "reply_ts": (
            negotiation.exchange.final.ts
            if negotiation.exchange.final
            else None
        ),
        "code": negotiation.exchange.code,
        "reason": negotiation.reason,
        "transfers": [t.tid for t in negotiation.transfers],
        "data_connections": [link.data.cid for link in negotiation.data_links],
    }


def _transfer_dict(transfer):
    return {
        "id": transfer.tid,
        "command": transfer.name,
        "argument": transfer.command.argument,
        "ts": transfer.command.ts,
        "status": transfer.status,
        "reply_code": transfer.reply_code,
        "negotiation": (
            transfer.negotiation.nid if transfer.negotiation else None
        ),
        "data_connection": (
            transfer.data_link.data.cid if transfer.data_link else None
        ),
        "candidate_data_connections": [
            link.data.cid for link in transfer.candidate_links
        ],
        "reason": transfer.reason,
    }


def _data_connection_dict(link):
    conn = link.data
    return {
        "id": conn.cid,
        "initiator": _endpoint(conn.initiator),
        "responder": _endpoint(conn.responder),
        "midstream": conn.midstream,
        "first_ts": conn.first_ts,
        "close_ts": conn.close_ts,
        "close_reason": conn.close_reason,
        "tcp_flags": conn.flags_summary(),
        "session": link.session.sid if link.session else None,
        "negotiation": link.negotiation.nid if link.negotiation else None,
        "confidence": link.confidence,
        "bytes_c2s": link.bytes_c2s,
        "bytes_s2c": link.bytes_s2c,
        # FTP roles are only known once the link is exact; a candidate may
        # be a NAT artefact with the wrong server address.
        "bytes_client_to_server": (
            link.bytes_client_to_server if link.confidence == "exact" else None
        ),
        "bytes_server_to_client": (
            link.bytes_server_to_client if link.confidence == "exact" else None
        ),
        "bytes_complete": link.bytes_complete,
        "gaps": link.gaps,
        "candidates": link.candidates,
        "evidence": link.evidence,
        "note": link.note,
    }


def _endpoint(endpoint):
    if endpoint is None:
        return None
    return f"{endpoint[0]}:{endpoint[1]}"


def analyze_file(path):
    """Analyze a pcap/pcapng file by path."""

    return analyze(read_capture(path), path=path)
