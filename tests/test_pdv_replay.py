"""Tests for the offline PDV replayer."""

from __future__ import annotations

from io import BytesIO

import pytest
from pydicom.dataset import Dataset

from pynetdicom.dimse_messages import C_ECHO_RQ, C_STORE_RQ
from pynetdicom.dimse_primitives import C_ECHO, C_STORE
from pynetdicom.dsutils import encode
from pynetdicom.tests.encoded_pdu_items import (
    p_data_tf,
    p_data_tf_n_event_report,
    p_data_tf_rq,
)

from tools.pdv_replay import analyse, iter_pdvs, report


def _pdata(primitive) -> bytes:
    """Encode a P_DATA primitive as a single P-DATA-TF PDU."""
    body = b""
    for context_id, value in primitive.presentation_data_value_list:
        body += (len(value) + 1).to_bytes(4, "big") + bytes([context_id]) + value

    return b"\x04\x00" + len(body).to_bytes(4, "big") + body


def _echo(message_id: int, context_id: int, max_pdu: int) -> list[bytes]:
    primitive = C_ECHO()
    primitive.MessageID = message_id
    primitive.AffectedSOPClassUID = "1.2.840.10008.1.1"
    message = C_ECHO_RQ()
    message.primitive_to_message(primitive)

    return [_pdata(item) for item in message.encode_msg(context_id, max_pdu)]


def _store(
    message_id: int, context_id: int, max_pdu: int, data_set: bytes
) -> list[bytes]:
    primitive = C_STORE()
    primitive.MessageID = message_id
    primitive.AffectedSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    primitive.AffectedSOPInstanceUID = "1.2.3"
    primitive.Priority = 2
    primitive.DataSet = BytesIO(data_set)
    message = C_STORE_RQ()
    message.primitive_to_message(primitive)

    return [_pdata(item) for item in message.encode_msg(context_id, max_pdu)]


@pytest.fixture
def interleaved() -> list[bytes]:
    data_set = Dataset()
    data_set.PatientID = "P" * 40
    data_set.PatientName = "A^B"

    echo = _echo(11, 1, 20)
    store = _store(22, 3, 20, encode(data_set, True, True))

    parts = []
    for index in range(max(len(echo), len(store))):
        if index < len(echo):
            parts.append(echo[index])
        if index < len(store):
            parts.append(store[index])

    return parts


def test_iter_pdvs_reports_original_ranges():
    pdvs = list(iter_pdvs(p_data_tf_rq))

    assert len(pdvs) == 1
    pdv = pdvs[0]
    assert pdv.pdu_range == (0, 80)
    assert pdv.item_range == (6, 80)
    assert pdv.value_range == (11, 80)
    assert pdv.data_range == (12, 80)
    assert pdv.mchb == 0x03
    assert pdv.context_id == 1


@pytest.mark.parametrize(
    "blob, message_type",
    [
        (p_data_tf_rq, "C-ECHO-RQ"),
        (p_data_tf, "C-ECHO-RSP"),
        (p_data_tf_n_event_report, "N-EVENT-REPORT-RQ"),
    ],
)
def test_single_message_fixtures(blob, message_type):
    result = report(blob)

    assert result["errors"] == []
    assert len(result["messages"]) == 1
    message = result["messages"][0]
    assert message["message_type"] == message_type
    assert message["command_end"] == "confirmed"
    assert message["complete"]


def test_interleaved_contexts_are_separated(interleaved):
    result = report(b"".join(interleaved))

    assert result["errors"] == []
    assert len(result["messages"]) == 2
    types = {m["context_id"]: m["message_type"] for m in result["messages"]}
    assert types == {1: "C-ECHO-RQ", 3: "C-STORE-RQ"}

    echo = next(m for m in result["messages"] if m["context_id"] == 1)
    store = next(m for m in result["messages"] if m["context_id"] == 3)
    assert echo["dataset_end"] == "not applicable"
    assert store["dataset_end"] == "confirmed"
    # Ranges point back at the shared stream, not at per-context buffers
    assert echo["overall_range"].startswith("[")
    assert store["overall_range"].startswith("[")


def test_missing_final_dataset_fragment_is_incomplete(interleaved):
    result = report(b"".join(interleaved[:-1]))

    assert len(result["messages"]) == 1
    assert result["messages"][0]["message_type"] == "C-ECHO-RQ"
    assert len(result["incomplete"]) == 1
    pending = result["incomplete"][0]
    assert pending["context_id"] == 3
    assert pending["command_end"] == "confirmed"
    assert pending["dataset_end"] == "not reached"
    assert not pending["complete"]


def test_missing_command_fragment_is_never_resynchronised():
    echo = _echo(11, 1, 20)
    # Drop a command fragment from the middle of the command set
    blob = b"".join(echo[:2] + echo[3:])

    result = report(blob)

    assert len(result["messages"]) == 0
    assert len(result["incomplete"]) == 1
    pending = result["incomplete"][0]
    assert pending["context_id"] == 1
    assert pending["complete"] is False
    assert pending["errors"]


def test_dataset_before_command_is_rejected():
    # A data-set PDV (MCHB 0x00) arriving before any command PDV
    data_pdv = b"\x04\x00\x00\x00\x00\x08" + b"\x00\x00\x00\x04\x01\x00ab"

    result = report(data_pdv)

    assert result["errors"]
    assert result["incomplete"][0]["context_id"] == 1
    assert "before the command set was complete" in result["incomplete"][0]["errors"][0]


def test_truncated_pdu_is_reported():
    result = report(p_data_tf_rq[:-1])

    assert result["errors"]
    assert "truncated PDU" in result["errors"][0]


def test_non_pdata_pdu_is_reported():
    result = report(b"\x05\x00\x00\x00\x00\x04\x00\x00\x00\x00")

    assert result["errors"]
    assert "expected P-DATA-TF" in result["errors"][0]
