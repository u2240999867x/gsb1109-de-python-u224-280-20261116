# Copyright (C) 2007 Giampaolo Rodola' <g.rodola@gmail.com>.
# Use of this source code is governed by MIT license that can be
# found in the LICENSE file.

import json
import socket
import struct

import pytest

from pyftpdlib.replay import (
    analyze_events,
    events_from_records,
    replay_capture,
)
from pyftpdlib.replay.analyzer import RangeSet
from pyftpdlib.replay.capture import build_events
from pyftpdlib.replay.events import (
    EventError,
    iter_events,
    load_events,
    parse_event,
)
from pyftpdlib.replay.pcap import CaptureError, read_capture, read_segments

from . import PyftpdlibTestCase


def control(ts, direction, text, session="default"):
    return {
        "ts": ts,
        "channel": "control",
        "direction": direction,
        "data": text,
        "session": session,
    }


def command(ts, text, session="default"):
    return control(ts, "c2s", text, session)


def reply(ts, text, session="default"):
    return control(ts, "s2c", text, session)


def segment(ts, offset, length, conn="d1", direction="s2c", session="default"):
    return {
        "ts": ts,
        "channel": "data",
        "direction": direction,
        "kind": "segment",
        "conn": conn,
        "offset": offset,
        "length": length,
        "session": session,
    }


def close(ts, who="server", how="fin", conn="d1", session="default"):
    return {
        "ts": ts,
        "channel": "data",
        "direction": "s2c" if who == "server" else "c2s",
        "kind": "close",
        "conn": conn,
        "who": who,
        "how": how,
        "session": session,
    }


def analyze(records, **kwargs):
    return analyze_events(events_from_records(records), **kwargs)


class TestRangeSet:
    def test_merge_and_deduplicate(self):
        ranges = RangeSet()
        ranges.add(0, 100)
        ranges.add(50, 150)
        assert ranges.as_list() == [[0, 150]]
        assert ranges.total() == 150

    def test_gaps(self):
        ranges = RangeSet()
        ranges.add(0, 10)
        ranges.add(20, 30)
        assert ranges.gaps(0, 40) == [(10, 20), (30, 40)]

    def test_outside(self):
        ranges = RangeSet()
        ranges.add(0, 10)
        ranges.add(20, 30)
        assert ranges.outside(5, 25) == [(0, 5), (25, 30)]

    def test_negative_offset_rejected(self):
        ranges = RangeSet()
        with pytest.raises(EventError):
            ranges.add(-1, 10)


class TestEventParsing:
    def test_parse_and_sort(self):
        events = events_from_records(
            [
                reply(2, "226 ok"),
                command(1, "RETR f"),
                segment(1.5, 0, 10),
            ]
        )
        assert [event.ts for event in events] == [1, 1.5, 2]

    def test_jsonl_comments_and_array(self):
        text = "\n".join(
            [
                "# a comment",
                json.dumps(command(1, "RETR f")),
                json.dumps(segment(2, 0, 5)),
            ]
        )
        events = list(iter_events(text))
        assert len(events) == 2
        array = json.dumps([command(1, "RETR f"), segment(2, 0, 5)])
        assert len(list(iter_events(array))) == 2

    def test_invalid_event_reports_line(self):
        with pytest.raises(EventError, match="line 1"):
            parse_event({"ts": 1, "channel": "bogus"}, line=1)

    def test_reply_syntax_checked(self):
        with pytest.raises(EventError, match="malformed reply"):
            parse_event(
                {
                    "ts": 1,
                    "channel": "control",
                    "direction": "s2c",
                    "kind": "reply",
                    "data": "not a reply",
                },
                line=1,
            )

    def test_load_events(self, tmp_path):
        path = tmp_path / "events.jsonl"
        path.write_text(
            json.dumps(command(1, "RETR f"))
            + "\n"
            + json.dumps(reply(2, "226 ok"))
            + "\n"
        )
        assert len(load_events(str(path))) == 2


class TestDownloadAnalysis(PyftpdlibTestCase):
    def test_complete_download(self):
        result = analyze(
            [
                command(0, "SIZE f.bin"),
                reply(0.1, "213 1000"),
                command(1, "RETR f.bin"),
                reply(1.1, "150 ready"),
                segment(2, 0, 400),
                segment(2.1, 400, 600),
                close(2.2),
                reply(3, "226 complete"),
            ]
        )
        summary = result.primary
        assert summary.state == "complete"
        assert summary.outcome == "success"
        assert summary.expected_range == (0, 1000)
        assert summary.ranges.as_list() == [[0, 1000]]
        assert summary.missing_bytes == 0
        assert summary.data_close.closed
        assert summary.data_close.clean
        assert summary.data_close.closed_by == ["server"]
        assert summary.terminal.code == 226
        assert summary.final.code == 226
        assert summary.reasons == []

    def test_rest_resume_uses_file_offsets(self):
        result = analyze(
            [
                command(0, "SIZE f.bin"),
                reply(0.1, "213 1000"),
                command(0.2, "REST 600"),
                reply(0.3, "350 restarting"),
                command(1, "RETR f.bin"),
                reply(1.1, "150 ready"),
                segment(2, 600, 400),
                close(2.2),
                reply(3, "226 complete"),
            ]
        )
        summary = result.primary
        assert summary.restart_offset == 600
        assert summary.expected_range == (600, 1000)
        assert summary.ranges.as_list() == [[600, 1000]]
        assert summary.state == "complete"

    def test_gap_is_incomplete(self):
        result = analyze(
            [
                command(0, "SIZE f.bin"),
                reply(0.1, "213 1000"),
                command(1, "RETR f.bin"),
                reply(1.1, "150 ready"),
                segment(2, 0, 400),
                segment(2.1, 700, 300),
                close(2.2),
                reply(3, "226 complete"),
            ]
        )
        summary = result.primary
        assert summary.state == "incomplete"
        assert summary.outcome == "success"
        assert summary.gaps == [(400, 700)]
        assert summary.missing_bytes == 300
        assert "byte_ranges_incomplete" in summary.reasons

    def test_retransmission_not_double_counted(self):
        result = analyze(
            [
                command(0, "SIZE f.bin"),
                reply(0.1, "213 1000"),
                command(1, "RETR f.bin"),
                reply(1.1, "150 ready"),
                segment(2, 0, 500, conn="d1"),
                segment(2.1, 0, 500, conn="d2"),
                segment(2.2, 500, 500, conn="d2"),
                close(2.3, conn="d2"),
                reply(3, "226 complete"),
            ]
        )
        summary = result.primary
        assert summary.ranges.as_list() == [[0, 1000]]
        assert summary.wire_bytes == 1500
        assert summary.duplicate_bytes == 500
        assert summary.state == "complete"

    def test_missing_terminal_reply_stays_unknown(self):
        result = analyze(
            [
                command(0, "SIZE f.bin"),
                reply(0.1, "213 1000"),
                command(1, "RETR f.bin"),
                reply(1.1, "150 ready"),
                segment(2, 0, 1000),
                close(2.2),
            ]
        )
        summary = result.primary
        assert summary.state == "unknown"
        assert summary.outcome == "unknown"
        assert summary.terminal is None
        assert "terminal_reply_missing" in summary.reasons

    def test_no_data_evidence_stays_unknown(self):
        result = analyze([command(1, "RETR f.bin"), reply(1.1, "150 ready")])
        summary = result.primary
        assert summary.state == "unknown"
        assert summary.outcome == "unknown"

    def test_data_closed_by_client_only_is_incomplete(self):
        result = analyze(
            [
                command(0, "SIZE f.bin"),
                reply(0.1, "213 1000"),
                command(1, "RETR f.bin"),
                reply(1.1, "150 ready"),
                segment(2, 0, 1000),
                close(2.2, who="client"),
                reply(3, "226 complete"),
            ]
        )
        summary = result.primary
        assert summary.state == "incomplete"
        assert "data_closed_by_client_only" in summary.reasons

    def test_data_reset_is_incomplete(self):
        result = analyze(
            [
                command(0, "SIZE f.bin"),
                reply(0.1, "213 1000"),
                command(1, "RETR f.bin"),
                reply(1.1, "150 ready"),
                segment(2, 0, 1000),
                close(2.2, how="reset"),
                reply(3, "426 aborted"),
                reply(3.1, "226 abort successful"),
            ]
        )
        summary = result.primary
        assert summary.state == "incomplete"
        assert summary.outcome == "aborted"
        assert summary.terminal.code == 426
        assert summary.final.code == 226
        assert summary.data_close.reset

    def test_abort_produces_two_replies_on_original_transfer(self):
        result = analyze(
            [
                command(0, "SIZE f.bin"),
                reply(0.1, "213 1000"),
                command(1, "RETR f.bin"),
                reply(1.1, "150 ready"),
                segment(2, 0, 400),
                command(2.1, "ABOR"),
                close(2.2),
                reply(2.3, "426 transfer aborted"),
                reply(2.4, "226 abort successful"),
            ]
        )
        summary = result.primary
        assert summary.state == "incomplete"
        assert summary.outcome == "aborted"
        assert summary.terminal.code == 426
        assert summary.final.code == 226
        assert [block.code for block in summary.trailing] == [226]
        assert summary.command.final.code == 226
        assert summary.aborted_by_abor

    def test_426_then_226_without_abor_command(self):
        result = analyze(
            [
                command(0, "SIZE f.bin"),
                reply(0.1, "213 1000"),
                command(1, "RETR f.bin"),
                reply(1.1, "150 ready"),
                segment(2, 0, 400),
                close(2.2, how="fin"),
                reply(2.3, "426 aborted"),
                reply(2.4, "226 done"),
            ]
        )
        summary = result.primary
        assert summary.terminal.code == 426
        assert summary.final.code == 226
        assert [block.code for block in summary.trailing] == [226]

    def test_lone_426_without_continuation_is_unknown(self):
        result = analyze(
            [
                command(0, "SIZE f.bin"),
                reply(0.1, "213 1000"),
                command(1, "RETR f.bin"),
                reply(1.1, "150 ready"),
                segment(2, 0, 200),
                close(2.1),
                reply(3, "426 aborted"),
            ]
        )
        summary = result.primary
        assert summary.outcome == "aborted"
        assert summary.state == "unknown"
        assert "abort_continuation_missing" in summary.reasons

    def test_abort_continuation_after_new_transfer_stays_with_original(self):
        result = analyze(
            [
                command(0, "SIZE f.bin"),
                reply(0.1, "213 100"),
                command(1, "RETR f.bin"),
                reply(1.1, "150 ready"),
                segment(2, 0, 20),
                reply(2.1, "426 aborted"),
                close(2.2),
                command(2.3, "RETR next.bin"),
                reply(2.4, "150 ready"),
                reply(2.5, "226 abort successful"),
                segment(3, 0, 100),
                close(3.1),
                reply(4, "226 complete"),
            ],
            expected_size=100,
        )
        first, second = result.transfers
        assert first.terminal.code == 426
        assert first.final.code == 226
        assert first.final.text == "226 abort successful"
        assert first.state == "incomplete"
        assert second.state == "complete"
        assert second.terminal.text == "226 complete"

    def test_failed_retr_before_data(self):
        result = analyze([command(0, "RETR f.bin"), reply(0.1, "550 no file")])
        summary = result.primary
        assert summary.state == "incomplete"
        assert summary.outcome == "failed"
        assert summary.terminal.code == 550
        assert summary.ranges.as_list() == []

    def test_size_zero_file(self):
        result = analyze(
            [
                command(0, "SIZE empty"),
                reply(0.1, "213 0"),
                command(1, "RETR empty"),
                reply(1.1, "150 ready"),
                close(1.2),
                reply(2, "226 complete"),
            ]
        )
        summary = result.primary
        assert summary.state == "complete"
        assert summary.ranges.as_list() == []


class TestReplyAttribution(PyftpdlibTestCase):
    def test_late_226_does_not_complete_later_command(self):
        result = analyze(
            [
                command(0, "RETR f.bin"),
                reply(0.1, "150 ready"),
                segment(1, 0, 10),
                close(2),
                command(2.1, "NOOP"),
                reply(2.2, "200 ok"),
                reply(5, "226 late"),
            ],
            expected_size=10,
        )
        summary = result.primary
        noop = [cmd for cmd in result.commands if cmd.name == "NOOP"][0]
        # The 226 arrives after NOOP, but it can only belong to the
        # transfer; NOOP keeps its own 200 and is not completed by it.
        assert summary.terminal.code == 226
        assert summary.state == "complete"
        assert noop.final.code == 200
        assert summary.late_replies == []

    def test_late_reply_cannot_complete_next_transfer(self):
        result = analyze(
            [
                command(0, "SIZE f.bin"),
                reply(0.1, "213 100"),
                command(1, "RETR f.bin"),
                reply(1.1, "150 ready"),
                segment(2, 0, 100),
                close(2.1),
                reply(3, "226 done"),
                command(4, "RETR next.bin"),
                reply(4.1, "150 ready"),
                segment(5, 0, 100),
                reply(5.1, "226 stale"),
                close(5.2),
            ],
            expected_size=100,
        )
        first, second = result.transfers
        assert first.state == "complete"
        assert first.terminal.ts == 3
        assert second.state == "unknown"
        assert second.terminal is None
        assert [block.code for block in first.late_replies] == [226]

    def test_abort_reply_does_not_belong_to_unrelated_transfer(self):
        result = analyze(
            [
                command(0, "RETR first"),
                reply(0.1, "150 ready"),
                segment(1, 0, 10),
                close(1.1),
                reply(1.2, "226 done"),
                command(2, "RETR second"),
                reply(2.1, "150 ready"),
                segment(3, 0, 10),
                close(3.1),
                reply(3.2, "226 done"),
            ],
            expected_size=10,
        )
        assert [t.state for t in result.transfers] == ["complete", "complete"]

    def test_reply_before_data_close_not_used(self):
        result = analyze(
            [
                command(0, "RETR f"),
                reply(0.1, "150 ready"),
                segment(1, 0, 100),
                reply(1.5, "226 early"),
                close(2),
                reply(3, "226 actual"),
            ],
            expected_size=100,
        )
        summary = result.primary
        assert summary.terminal.code == 226
        assert summary.state == "complete"

    def test_multilen_reply_is_one_block(self):
        result = analyze(
            [
                command(0, "RETR f"),
                reply(0.1, "150-about to open"),
                reply(0.2, "150 data connection"),
                segment(1, 0, 10),
                close(1.1),
                reply(2, "226 done"),
            ],
            expected_size=10,
        )
        summary = result.primary
        assert summary.state == "complete"
        assert len(summary.replies) == 2
        assert summary.replies[0].code == 150

    def test_unknown_transfer_reference_warns(self):
        result = analyze(
            [
                command(0, "RETR f"),
                reply(0.1, "150 ready"),
                dict(segment(1, 0, 10), transfer="t99"),
            ]
        )
        assert result.primary.segment_count == 0
        assert any("unknown transfer" in item for item in result.warnings)


def _eth_ip_tcp(
    src_ip, dst_ip, sport, dport, seq, ack, payload=b"", flags=0x18
):
    ethernet = b"\x00" * 12 + struct.pack(">H", 0x0800)
    tcp = struct.pack(
        ">HHIIBBHHH", sport, dport, seq, ack, 5 << 4, flags, 8192, 0, 0
    )
    total = 20 + len(tcp) + len(payload)
    ip = struct.pack(
        ">BBHHHBBH4s4s",
        0x45,
        0,
        total,
        1,
        0,
        64,
        6,
        0,
        socket.inet_aton(src_ip),
        socket.inet_aton(dst_ip),
    )
    return ethernet + ip + tcp + payload


def write_pcap(path, frames):
    header = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
    with open(path, "wb") as file:
        file.write(header)
        for ts, data in frames:
            file.write(
                struct.pack(
                    "<IIII", int(ts), int((ts % 1) * 1e6), len(data), len(data)
                )
            )
            file.write(data)


def build_download_capture(path, *, missing_tail=False, client_close=False):
    client, server = "192.0.2.10", "192.0.2.20"
    frames = []
    clock = [1.0]

    def add(src, dst, sport, dport, seq, ack, payload=b"", flags=0x18):
        frames.append(
            (
                clock[0],
                _eth_ip_tcp(src, dst, sport, dport, seq, ack, payload, flags),
            )
        )
        clock[0] += 0.01

    add(client, server, 40000, 21, 1000, 1, b"USER u\r\n")
    add(server, client, 21, 40000, 5000, 1012, b"331 pass\r\n")
    add(client, server, 40000, 21, 1012, 5012, b"PASS p\r\n")
    add(server, client, 21, 40000, 5012, 1024, b"230 ok\r\n")
    add(client, server, 40000, 21, 1024, 5022, b"TYPE I\r\n")
    add(server, client, 21, 40000, 5022, 1035, b"200 ok\r\n")
    add(client, server, 40000, 21, 1035, 5032, b"SIZE f.bin\r\n")
    add(server, client, 21, 40000, 5032, 1050, b"213 1000\r\n")
    add(client, server, 40000, 21, 1050, 5046, b"PASV\r\n")
    add(
        server,
        client,
        21,
        40000,
        5046,
        1059,
        b"227 Entering passive mode (192,0,2,20,200,10).\r\n",
    )
    add(client, server, 40000, 21, 1059, 5116, b"RETR f.bin\r\n")
    add(server, client, 21, 40000, 5116, 1073, b"150 ready\r\n")
    add(server, client, 51210, 40001, 9000, 1, b"A" * 400)
    if not missing_tail:
        add(server, client, 51210, 40001, 9400, 1, b"B" * 600)
    if client_close:
        add(client, server, 40001, 51210, 1, 10000, b"", flags=0x11)
    else:
        add(server, client, 51210, 40001, 10000, 1, b"", flags=0x11)
    add(server, client, 21, 40000, 5127, 1073, b"226 done\r\n")
    write_pcap(path, frames)


class TestCaptureReplay:
    def test_end_to_end_pcap(self, tmp_path):
        path = str(tmp_path / "download.pcap")
        build_download_capture(path)
        replay, segments = replay_capture(path)
        assert len(segments) == 16
        result = analyze_events(replay.events)
        summary = result.primary
        assert summary.state == "complete"
        assert summary.expected_range == (0, 1000)
        assert summary.ranges.as_list() == [[0, 1000]]
        assert summary.data_close.closed_by == ["server"]
        assert summary.terminal.code == 226

    def test_capture_gap_detected(self, tmp_path):
        path = str(tmp_path / "gap.pcap")
        build_download_capture(path, missing_tail=True)
        replay, _segments = replay_capture(path)
        summary = analyze_events(replay.events).primary
        assert summary.state == "incomplete"
        assert summary.missing_bytes == 600

    def test_client_close_detected(self, tmp_path):
        path = str(tmp_path / "client-close.pcap")
        build_download_capture(path, client_close=True)
        replay, _segments = replay_capture(path)
        summary = analyze_events(replay.events).primary
        assert summary.data_close.client_closed
        assert summary.state == "incomplete"

    def test_truncated_pcap_rejected(self, tmp_path):
        path = str(tmp_path / "bad.pcap")
        with open(path, "wb") as file:
            file.write(b"\xd4\xc3\xb2\xa1\x02\x00\x04\x00")
        with pytest.raises(CaptureError):
            read_capture(path)

    def test_read_segments_on_wrong_magic(self, tmp_path):
        path = str(tmp_path / "not.pcap")
        with open(path, "wb") as file:
            file.write(b"hello world")
        with pytest.raises(CaptureError):
            read_segments(path)

    def test_no_control_connection(self, tmp_path):
        path = str(tmp_path / "empty.pcap")
        frame = _eth_ip_tcp("1.1.1.1", "2.2.2.2", 1, 2, 1, 1)
        write_pcap(path, [(1.0, frame)])
        with pytest.raises(ValueError, match="no FTP control"):
            build_events(read_segments(path)[0])


class TestCli:
    def test_event_file_output(self, tmp_path, capsys):
        from pyftpdlib.replay.cli import main

        path = str(tmp_path / "events.jsonl")
        records = [
            command(0, "SIZE f"),
            reply(0.1, "213 10"),
            command(1, "RETR f"),
            reply(1.1, "150 ready"),
            segment(2, 0, 10),
            close(2.1),
            reply(3, "226 done"),
        ]
        with open(path, "w") as file:
            for record in records:
                file.write(json.dumps(record) + "\n")
        assert main([path]) == 0
        out = capsys.readouterr().out
        assert "state: complete" in out
        assert "received: 0-10" in out

    def test_json_output_and_unknown_exit(self, tmp_path, capsys):
        from pyftpdlib.replay.cli import main

        path = str(tmp_path / "events.jsonl")
        with open(path, "w") as file:
            file.write(json.dumps(command(1, "RETR f")) + "\n")
        assert main([path, "--json"]) == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["summary"]["state"] == "unknown"
