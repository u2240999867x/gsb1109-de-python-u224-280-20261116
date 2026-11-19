import json

import pytest

from imaplib2.recording import (
    IMAPRecordingParser,
    ParseError,
    parse_recording,
)


def test_plain_responses_have_no_payloads():
    result = parse_recording(b"* OK ready\r\nA1 NO bad\r\n")

    assert len(result.responses) == 2
    first, second = result.responses
    assert (first.kind, first.marker, first.status, first.text) == (
        "untagged",
        b"*",
        b"OK",
        b"ready",
    )
    assert first.payload_ranges == ()
    assert (second.kind, second.tag, second.status, second.text) == (
        "tagged",
        b"A1",
        b"NO",
        b"bad",
    )
    assert result.pending is None


def test_structural_fields_have_byte_ranges():
    recording = b"* 1 FETCH (BODY[] {3}\r\nabc)\r\nA1 OK done\r\n"
    result = parse_recording(recording)

    fetch, tagged = result.responses
    assert fetch.field_ranges == {
        "marker": (0, 1),
        "number": (2, 3),
        "status": (4, 9),
        "text": (10, 17),
    }
    for name, (start, end) in fetch.field_ranges.items():
        value = getattr(fetch, name)
        if isinstance(value, bytes):
            assert recording[start:end] == value

    assert tagged.field_ranges == {
        "tag": (29, 31),
        "status": (32, 34),
        "text": (35, 39),
    }
    assert recording[29:31] == b"A1"
    assert recording[32:34] == b"OK"


def test_tagged_reply_like_text_inside_literal_is_payload():
    payload = b"hello\r\nA1 OK not a response\r\n* 2 EXISTS\r\n"
    recording = (
        b"* 1 FETCH (BODY[] {%d}\r\n" % len(payload)
        + payload
        + b")\r\nA1 OK done\r\n"
    )

    result = parse_recording(recording)

    assert len(result.responses) == 2
    fetch, tagged = result.responses
    assert fetch.status == b"FETCH"
    assert fetch.payload_ranges == ((24, 24 + len(payload)),)
    assert recording[slice(*fetch.payload_ranges[0])] == payload
    assert fetch.literals[0].payload == payload
    assert fetch.tails == (b")",)
    assert (tagged.kind, tagged.tag, tagged.status) == ("tagged", b"A1", b"OK")


def test_one_byte_chunks_and_length_header_across_chunks():
    recording = b"* 1 FETCH (BODY[] {12}\r\nabcdefghijkl)\r\nA1 OK done\r\n"
    chunks = [recording[i : i + 1] for i in range(len(recording))]

    result = parse_recording(chunks)

    assert [response.status for response in result.responses] == [b"FETCH", b"OK"]
    assert result.responses[0].payload_ranges == ((24, 36),)
    assert result.pending is None


def test_marker_split_between_chunks():
    parser = IMAPRecordingParser()

    assert parser.feed(b"* 1 FETCH (BODY[] {") == []
    assert parser.pending.reason == "line"
    assert parser.feed(b"12}\r\nabc") == []
    assert parser.pending.reason == "literal"
    assert parser.pending.received == 3

    responses = parser.feed(b"defghijkl)\r\n")

    assert len(responses) == 1
    assert responses[0].payload_ranges == ((24, 36),)
    assert parser.finish() is None


def test_multiple_literals_consumed_in_order():
    recording = (
        b"* 1 FETCH (BODY[HEADER] {3}\r\nabc BODY[TEXT] {3}\r\ndef)\r\n"
        b"A1 OK done\r\n"
    )

    result = parse_recording(recording)
    fetch = result.responses[0]

    assert fetch.literals[0].marker == b"{3}"
    assert fetch.literals[1].marker == b"{3}"
    assert fetch.literals[0].payload == b"abc"
    assert fetch.literals[1].payload == b"def"
    assert fetch.payload_ranges == ((29, 32), (49, 52))
    assert recording[29:32] == b"abc"
    assert recording[49:52] == b"def"
    assert fetch.tails == (b" BODY[TEXT] {3}", b")")
    assert fetch.tail_ranges == ((32, 47), (52, 53))


def test_literal_marker_in_tail_continues_same_response():
    recording = b"* 1 FETCH (BODY[1] {2}\r\nab BODY[2] {2}\r\ncd)\r\nA1 OK x\r\n"

    result = parse_recording(recording)
    fetch = result.responses[0]

    assert fetch.header == b"* 1 FETCH (BODY[1] {2}"
    assert fetch.payload_ranges == ((24, 26), (40, 42))
    assert fetch.tails == (b" BODY[2] {2}", b")")
    assert len(result.responses) == 2


def test_literal_marker_looking_text_in_payload_stays_payload():
    payload = b"before\r\n* 2 FETCH (BODY[] {5}\r\n {0}\r\nafter"
    recording = (
        b"* 1 FETCH (BODY[] {%d}\r\n" % len(payload)
        + payload
        + b")\r\nA1 OK done\r\n"
    )

    result = parse_recording(recording)
    fetch = result.responses[0]

    assert len(fetch.literals) == 1
    assert fetch.literals[0].payload == payload
    assert fetch.tails == (b")",)
    assert [response.status for response in result.responses] == [b"FETCH", b"OK"]


def test_segments_cover_response_without_overlap():
    recording = (
        b"* 1 FETCH (BODY[HEADER] {3}\r\nabc BODY[TEXT] {3}\r\ndef)\r\n"
        b"A1 OK done\r\n"
    )

    result = parse_recording(recording)
    fetch = result.responses[0]

    position = fetch.start
    for role, start, end in fetch.segments:
        assert start == position
        assert start < end
        position = end
    assert position == fetch.end
    assert [role for role, _, _ in fetch.segments] == [
        "header",
        "separator",
        "payload",
        "tail",
        "separator",
        "payload",
        "tail",
        "separator",
    ]
    payloads = [
        recording[start:end]
        for role, start, end in fetch.segments
        if role == "payload"
    ]
    assert payloads == [b"abc", b"def"]


def test_zero_length_literal():
    result = parse_recording(b"* 1 FETCH (BODY[] {0}\r\n)\r\nA1 OK x\r\n")

    fetch = result.responses[0]
    assert fetch.payload_ranges == ((23, 23),)
    assert fetch.literals[0].payload == b""
    assert fetch.tails == (b")",)


def test_truncated_literal_keeps_unconfirmed_tail():
    recording = b"* 1 FETCH (BODY[] {12}\r\nabcdef"

    result = parse_recording(recording)

    assert result.responses == ()
    pending = result.pending
    assert pending.start == 0
    assert pending.end == len(recording)
    assert pending.data == recording
    assert pending.reason == "literal"
    assert pending.literal_size == 12
    assert pending.payload_start == 24
    assert pending.received == 6
    assert pending.missing == 6


def test_resume_after_truncated_literal_is_not_scanned_for_tag():
    parser = IMAPRecordingParser()
    payload = b"hello\r\nA1 OK fake\r\n"

    assert parser.feed(b"* 1 FETCH (BODY[] {%d}\r\n" % len(payload)) == []
    assert parser.feed(b"hello\r\nA1 OK fake") == []
    pending = parser.pending
    assert pending.reason == "literal"
    assert pending.received == len(b"hello\r\nA1 OK fake")

    responses = parser.feed(b"\r\n)\r\nA1 OK real\r\n")

    assert len(responses) == 2
    fetch, tagged = responses
    assert fetch.payload_ranges == ((24, 24 + len(payload)),)
    assert fetch.tails == (b")",)
    assert tagged.tag == b"A1"
    assert tagged.text == b"real"


def test_truncated_header_keeps_unconfirmed_tail():
    parser = IMAPRecordingParser()

    assert parser.feed(b"* 1 FETCH (BODY[] {5}\r\nabc") == []
    assert parser.pending.reason == "literal"

    # Finish the literal and the response but leave the next header partial.
    responses = parser.feed(b"de)\r\nA1 O")
    assert [response.status for response in responses] == [b"FETCH"]
    assert parser.pending.data == b"A1 O"
    assert parser.pending.reason == "line"

    responses = parser.feed(b"K\r\n")

    assert len(responses) == 1
    assert responses[0].tag == b"A1"
    assert parser.pending is None


def test_partial_header_after_complete_response():
    parser = IMAPRecordingParser()

    responses = parser.feed(b"* OK hi\r\nA1 O")
    assert len(responses) == 1
    assert parser.pending.data == b"A1 O"
    assert parser.pending.reason == "line"

    responses = parser.feed(b"K\r\n")
    assert [response.status for response in responses] == [b"OK"]
    assert parser.finish() is None


def test_quoted_brace_is_not_a_literal_marker():
    result = parse_recording(b'* LIST () "/" "{3}"\r\n')

    assert len(result.responses) == 1
    assert result.responses[0].literals == ()
    assert result.responses[0].text == b'() "/" "{3}"'


def test_literal_in_quoted_string_is_not_a_marker():
    result = parse_recording(b'* LIST () "/" "x {3}"\r\n')

    assert result.responses[0].literals == ()


def test_bare_lf_line_terminator_keeps_payload_offsets():
    recording = b"* 1 FETCH (BODY[] {5}\r\nab\ncd)\nA1 OK x\n"

    result = parse_recording(recording)
    fetch = result.responses[0]

    payload = recording[
        fetch.literals[0].payload_start : fetch.literals[0].payload_end
    ]
    assert payload == b"ab\ncd"


def test_response_code_fields():
    result = parse_recording(b"* OK [UNSEEN 5] hi\r\n")

    untagged = result.responses[0]
    assert untagged.code == b"UNSEEN"
    assert untagged.code_data == b"5"
    assert untagged.text == b"[UNSEEN 5] hi"


def test_continuation_response():
    result = parse_recording(b"+ go ahead\r\n")

    continuation = result.responses[0]
    assert (continuation.kind, continuation.marker, continuation.text) == (
        "continuation",
        b"+",
        b"go ahead",
    )


def test_invalid_response_raises_parse_error():
    with pytest.raises(ParseError):
        parse_recording(b"@@@\r\n")


def test_feed_after_finish_is_rejected():
    parser = IMAPRecordingParser()
    parser.finish()

    with pytest.raises(RuntimeError):
        parser.feed(b"")


def test_json_cli(tmp_path):
    from imaplib2.recording import main

    recording = b"* 1 FETCH (BODY[] {3}\r\nabc)\r\nA1 OK x\r\n"
    path = tmp_path / "recording.bin"
    path.write_bytes(recording)

    import io
    import contextlib

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert main(["--json", str(path)]) == 0

    payload = json.loads(output.getvalue())
    assert payload["responses"][0]["payload_ranges"] == [[23, 26]]
    assert payload["responses"][0]["field_ranges"]["status"] == [4, 9]
    assert payload["responses"][0]["literals"][0]["size"] == 3
    assert payload["pending"] is None


def test_json_cli_reports_truncated_literal(tmp_path):
    from imaplib2.recording import main

    path = tmp_path / "truncated.bin"
    path.write_bytes(b"* 1 FETCH (BODY[] {12}\r\nabcdef")

    import io
    import contextlib

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        assert main(["--json", str(path)]) == 0

    payload = json.loads(output.getvalue())
    assert payload["responses"] == []
    assert payload["pending"]["reason"] == "literal"
    assert payload["pending"]["missing"] == 6
