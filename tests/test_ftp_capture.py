"""Tests for the FTP capture correlator.

The fixtures are generated in-process so no external capture files or
tooling are needed; each scenario encodes one attribution rule.
"""

import json
import os
import struct

import pytest

from ftp_capture import correlate
from ftp_capture import ftp
from ftp_capture import pcap
from ftp_capture import synthetic
from ftp_capture.report import to_json
from ftp_capture.report import to_text
from ftp_capture.tcp import build_connections


def frames_of(raw_frames):
    return [
        pcap.Frame(
            index=index,
            ts=ts,
            linktype=pcap.LINKTYPE_ETHERNET,
            interface=0,
            data=data,
            wirelen=len(data),
        )
        for index, (ts, data) in enumerate(raw_frames)
    ]


def report_of(raw_frames):
    return correlate.analyze(frames_of(raw_frames))


@pytest.fixture(scope="module")
def main_report():
    return report_of(synthetic.build())


@pytest.fixture(scope="module")
def ambiguous_report():
    return report_of(synthetic.build_ambiguous())


# --- pcap reader ------------------------------------------------------


def test_pcap_roundtrip(tmp_path):
    payloads = [(1000.5, b"\x01\x02\x03"), (1000.75, b"\x04" * 10)]
    path = os.path.join(str(tmp_path), "round.pcap")
    pcap.write_pcap(path, payloads)
    frames = list(pcap.read_capture(path))
    assert [f.data for f in frames] == [p[1] for p in payloads]
    assert frames[0].ts == pytest.approx(1000.5)
    assert all(not f.truncated for f in frames)


def test_pcapng_enhanced_packet(tmp_path):
    path = os.path.join(str(tmp_path), "mini.pcapng")

    def block(kind, body):
        length = 12 + len(body)
        return (
            struct.pack("<II", kind, length)
            + body
            + struct.pack("<I", length)
        )

    shb = block(
        0x0A0D0D0A,
        struct.pack("<IHHq", 0x1A2B3C4D, 1, 0, -1),
    )
    idb = block(0x00000001, struct.pack("<HHI", 1, 0, 0) + b"\x00\x00\x00\x00")
    payload = b"\xde\xad\xbe\xef"
    epb = block(
        0x00000006,
        struct.pack("<IIIII", 0, 0, 1_500_000, len(payload), len(payload))
        + payload
        + b"\x00" * (-len(payload) % 4),
    )
    with open(path, "wb") as stream:
        stream.write(shb + idb + epb)

    frames = list(pcap.read_capture(path))
    assert len(frames) == 1
    assert frames[0].data == payload
    assert frames[0].ts == pytest.approx(1.5)


def test_truncated_pcap_frame_flagged(tmp_path):
    path = os.path.join(str(tmp_path), "snap.pcap")
    with open(path, "wb") as stream:
        stream.write(
            struct.pack("<IHHiIII", pcap.PCAP_MAGIC_LE, 2, 4, 0, 0, 64, 1)
        )
        stream.write(struct.pack("<IIII", 1, 0, 4, 100))
        stream.write(b"\x00" * 4)
    frame = next(iter(pcap.read_capture(path)))
    assert frame.truncated
    assert frame.wirelen == 100


# --- TCP reassembly ---------------------------------------------------


def _conn(raw_frames):
    connections, _stats = build_connections(frames_of(raw_frames))
    return connections


def test_reassembly_keeps_gap_explicit():
    w = synthetic.World()
    client = synthetic.TcpEnd("10.1.0.1", 5000, b"\x02" * 6)
    server = synthetic.TcpEnd("10.1.0.2", 21, b"\x02" * 6)
    w.handshake(client, server)
    w.packet(client, server, client.seq, server.seq, 0x18, b"HELLO")
    client.seq += 5
    # Skip 10 bytes, then continue.
    w.packet(client, server, client.seq + 10, server.seq, 0x18, b"WORLD")
    client.seq += 15
    conn = _conn(w.frames)[0]
    direction = conn.fwd
    assert direction.data == b"HELLO" + b"\x00" * 10 + b"WORLD"
    assert len(direction.gaps) == 1
    assert direction.gaps[0].length == 10


def test_reassembly_ignores_duplicate_bytes():
    w = synthetic.World()
    client = synthetic.TcpEnd("10.1.1.1", 5000, b"\x02" * 6)
    server = synthetic.TcpEnd("10.1.1.2", 21, b"\x02" * 6)
    w.handshake(client, server)
    start = client.seq
    w.packet(client, server, start, server.seq, 0x18, b"ABCDEF")
    w.packet(client, server, start, server.seq, 0x18, b"ABCDEF")
    client.seq = start + 6
    conn = _conn(w.frames)[0]
    assert conn.fwd.data == b"ABCDEF"
    assert conn.fwd.duplicate_bytes == 6


def test_port_reuse_creates_two_instances():
    w = synthetic.World()
    client = synthetic.TcpEnd("10.1.2.1", 5000, b"\x02" * 6)
    server = synthetic.TcpEnd("10.1.2.2", 21, b"\x02" * 6)
    w.handshake(client, server)
    w.send(client, server, b"NOOP\r\n")
    w.fin(client, server)
    client.seq = 9000
    server.seq = 7000
    w.handshake(client, server)
    w.send(client, server, b"NOOP\r\n")
    connections = _conn(w.frames)
    assert len(connections) == 2
    assert connections[0].close_reason in ("fin", "port-reuse")
    assert connections[1].fwd.data == b"NOOP\r\n"


# --- FTP parsing ------------------------------------------------------


def test_multiline_reply_is_one_reply():
    w = synthetic.World()
    client = synthetic.TcpEnd("10.1.3.1", 5000, b"\x02" * 6)
    server = synthetic.TcpEnd("10.1.3.2", 21, b"\x02" * 6)
    w.handshake(client, server)
    w.reply(
        server,
        client,
        "227-Entering passive mode\r\n"
        "227-Please use the announced port\r\n"
        "227 (10,0,0,5,195,80).\r\n",
    )
    conn = _conn(w.frames)[0]
    replies = ftp.parse_replies(conn.rev)
    assert len(replies) == 1
    assert replies[0].code == 227
    assert ftp.parse_pasv(replies[0].text) == ("10.0.0.5", 50000)


def test_reply_pairs_to_preceding_command():
    w = synthetic.World()
    client = synthetic.TcpEnd("10.1.4.1", 5000, b"\x02" * 6)
    server = synthetic.TcpEnd("10.1.4.2", 21, b"\x02" * 6)
    w.handshake(client, server)
    w.reply(server, client, "220 ready\r\n")
    w.control(client, server, b"USER x\r\n")
    w.reply(server, client, "331 password\r\n")
    w.control(client, server, b"PASS y\r\n")
    w.reply(server, client, "230 ok\r\n")
    conn = _conn(w.frames)[0]
    commands = ftp.parse_commands(conn.fwd)
    replies = ftp.parse_replies(conn.rev)
    exchanges, greeting = ftp.pair_exchanges(commands, replies)
    assert [r.code for r in greeting] == [220]
    assert [e.code for e in exchanges] == [331, 230]


# --- end-to-end correlation ------------------------------------------

EXPECTED_EXACT = {
    (2, 1, 1),
    (3, 1, 2),
    (5, 2, 1),
    (7, 3, 1),
    (9, 3, 2),
}


def test_sessions_are_separate(main_report):
    assert main_report["totals"]["sessions"] == 3
    clients = {s["client"] for s in main_report["sessions"]}
    assert clients == {
        "10.0.0.10:40001",
        "10.0.0.20:41001",
        "10.0.0.30:42001",
    }


def test_exact_data_connection_attribution(main_report):
    exact = {
        (d["id"], d["session"], d["negotiation"])
        for d in main_report["data_connections"]
        if d["confidence"] == "exact"
    }
    assert exact == EXPECTED_EXACT


def test_passive_download_bytes_are_server_to_client(main_report):
    session = next(s for s in main_report["sessions"] if s["id"] == 1)
    assert session["bytes_download"] == 5000
    assert session["bytes_upload"] == 0


def test_active_upload_bytes_are_client_to_server(main_report):
    session = next(s for s in main_report["sessions"] if s["id"] == 2)
    assert session["bytes_upload"] == 2500
    assert session["bytes_download"] == 0
    active = next(
        d for d in main_report["data_connections"] if d["id"] == 5
    )
    assert active["initiator"] == "10.0.0.5:50002"
    assert active["responder"] == "10.0.0.20:41002"
    assert active["bytes_c2s"] == 0
    assert active["bytes_s2c"] == 2500


def test_failed_negotiation_is_recorded(main_report):
    failures = main_report["events"]["negotiation_failures"]
    assert len(failures) == 1
    failure = failures[0]
    assert failure["session"] == 2
    assert failure["command"] == "PORT"
    assert failure["status"] == "failed"
    assert failure["code"] == 501
    assert "privileged port" in failure["reason"]


def test_missing_data_channel_is_recorded(main_report):
    missing = main_report["events"]["missing_data_channels"]
    statuses = {(m["session"], m["transfer"]): m["status"] for m in missing}
    assert statuses[(2, 2)] == "negotiation_failed"
    assert statuses[(2, 3)] == "data_channel_missing"
    lost = next(m for m in missing if m["transfer"] == 3)
    assert "no data connection was captured" in lost["reason"]


def test_transfers_without_negotiation_are_not_invented(main_report):
    session = next(s for s in main_report["sessions"] if s["id"] == 2)
    transfer = next(t for t in session["transfers"] if t["id"] == 3)
    assert transfer["negotiation"] == 3
    assert transfer["data_connection"] is None


def test_reused_port_bytes_are_not_borrowed(main_report):
    # Connection 8 reuses the 4-tuple of connection 2 but has no
    # negotiation; its 700 bytes must not inflate session 1.
    session = next(s for s in main_report["sessions"] if s["id"] == 1)
    assert session["bytes_download"] == 5000
    unattributed = {
        e["data_connection"] for e in main_report["events"]["unattributed"]
    }
    assert 8 in unattributed
    assert main_report["totals"]["bytes_unattributed"] == 700


def test_same_passive_port_in_two_sessions_stays_split(main_report):
    # The endpoint 10.0.0.5:50000 appears in sessions 1 and 3; each link
    # must belong to its own session and keep its own byte count.
    on_port = [
        d
        for d in main_report["data_connections"]
        if d["responder"] == "10.0.0.5:50000" and d["confidence"] == "exact"
    ]
    assert len(on_port) == 2
    assert {d["session"] for d in on_port} == {1, 3}
    assert {d["bytes_s2c"] for d in on_port} == {3000, 1500}


def test_ambiguous_endpoint_stays_candidate(ambiguous_report):
    totals = ambiguous_report["totals"]
    assert totals["candidate_data_connections"] == 1
    assert totals["attributed_data_connections"] == 0
    assert totals["bytes_attributed"] == 0
    assert totals["bytes_candidate"] == 1234
    link = ambiguous_report["data_connections"][0]
    assert link["confidence"] == "candidate"
    assert {c["session"] for c in link["candidates"]} == {1, 2}
    for session in ambiguous_report["sessions"]:
        assert session["bytes_download"] == 0
        assert session["bytes_upload"] == 0


def test_candidate_bytes_are_not_double_counted(ambiguous_report):
    assert ambiguous_report["totals"]["bytes_candidate"] == 1234
    for session in ambiguous_report["sessions"]:
        assert session["bytes_unverified"] == 1234


def test_reused_negotiation_is_not_reassigned():
    report = report_of(synthetic.build_reuse_without_renegotiation())
    session = report["sessions"][0]
    assert [t["status"] for t in session["transfers"]] == [
        "completed",
        "no_negotiation",
    ]
    assert session["bytes_download"] == 400


def test_gap_makes_attributed_bytes_a_lower_bound():
    report = report_of(synthetic.build_gap_in_data_channel())
    session = report["sessions"][0]
    assert session["bytes_complete"] is False
    assert session["bytes_download"] == 700
    assert any("lower bound" in issue for issue in session["issues"])
    assert report["data_connections"][0]["gaps"][0]["length"] == 300


def test_nat_address_keeps_link_as_candidate():
    report = report_of(synthetic.build_nat())
    assert report["totals"]["candidate_data_connections"] == 1
    assert report["totals"]["bytes_attributed"] == 0
    link = report["data_connections"][0]
    assert link["confidence"] == "candidate"
    assert link["session"] == 1
    assert "port matches" in link["note"]
    session = report["sessions"][0]
    assert session["bytes_download"] == 0
    assert session["bytes_unverified"] == 900
    transfer = session["transfers"][0]
    assert transfer["status"] == "candidate_data_channel"
    assert transfer["candidate_data_connections"] == [2]


def test_capture_gap_is_reported_as_session_issue():
    w = synthetic.World()
    client = synthetic.TcpEnd("10.0.0.70", 45001, b"\x02" * 6)
    server = synthetic.TcpEnd("10.0.0.5", 21, b"\x02" * 6)
    w.handshake(client, server)
    w.reply(server, client, "220 ready\r\n")
    start = client.seq
    w.packet(client, server, start, server.seq, 0x18, b"USER a\r\n")
    client.seq = start + 7 + 20
    w.packet(client, server, client.seq, server.seq, 0x18, b"PASS b\r\n")
    client.seq += 8
    report = report_of(w.frames)
    issues = report["sessions"][0]["issues"]
    assert any("gap" in issue for issue in issues)


def test_duplicate_control_command_not_double_counted():
    w = synthetic.World()
    client = synthetic.TcpEnd("10.0.0.71", 45002, b"\x02" * 6)
    server = synthetic.TcpEnd("10.0.0.5", 21, b"\x02" * 6)
    w.handshake(client, server)
    w.reply(server, client, "220 ready\r\n")
    start = client.seq
    w.packet(client, server, start, server.seq, 0x18, b"NOOP\r\n")
    w.packet(client, server, start, server.seq, 0x18, b"NOOP\r\n")
    client.seq = start + 6
    report = report_of(w.frames)
    session = report["sessions"][0]
    assert session["commands"] == 1


def _raw_frames(world_frames):
    """Strip Ethernet headers so the frames look like LINKTYPE_RAW."""

    raw = []
    for ts, data in world_frames:
        raw.append((ts, data[14:]))
    return [
        pcap.Frame(
            index=index,
            ts=ts,
            linktype=pcap.LINKTYPE_RAW,
            interface=0,
            data=data,
            wirelen=len(data),
        )
        for index, (ts, data) in enumerate(raw)
    ]


def test_raw_ip_linktype_is_supported():
    report = correlate.analyze(_raw_frames(synthetic.build()))
    assert report["totals"]["sessions"] == 3
    assert report["totals"]["attributed_data_connections"] == 5


def test_midstream_control_is_flagged():
    w = synthetic.World()
    client = synthetic.TcpEnd("10.0.0.72", 45003, b"\x02" * 6)
    server = synthetic.TcpEnd("10.0.0.5", 21, b"\x02" * 6)
    # No handshake: the capture starts after the session was established.
    w.reply(server, client, "220 ready\r\n")
    w.control(client, server, b"USER late\r\n")
    w.reply(server, client, "331 password\r\n")
    report = report_of(w.frames)
    session = report["sessions"][0]
    assert any("mid-stream" in issue for issue in session["issues"])


# --- reporting --------------------------------------------------------


def test_text_report_lists_events(main_report):
    text = to_text(main_report)
    assert "SESSION S1" in text
    assert "negotiation failures" in text
    assert "transfers without a data channel" in text
    assert "connection reuse" in text
    assert "unattributed data connections" in text


def test_json_report_is_serialisable(main_report):
    payload = json.loads(to_json(main_report))
    assert payload["totals"]["sessions"] == 3
    assert "events" in payload


def test_cli_writes_report(tmp_path):
    capture_path = os.path.join(str(tmp_path), "world.pcap")
    pcap.write_pcap(capture_path, synthetic.build())
    from ftp_capture.__main__ import main

    output_path = os.path.join(str(tmp_path), "report.json")
    assert main([capture_path, "-f", "json", "-o", output_path, "-q"]) == 0
    with open(output_path, encoding="utf-8") as stream:
        report = json.load(stream)
    assert report["totals"]["sessions"] == 3


def test_cli_strict_flags_unattributed(tmp_path):
    capture_path = os.path.join(str(tmp_path), "world.pcap")
    pcap.write_pcap(capture_path, synthetic.build())
    from ftp_capture.__main__ import main

    assert main([capture_path, "-q", "--strict", "-o", os.devnull]) == 2
