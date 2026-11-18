"""Regression tests for the reusable in-memory CIP peer.

These tests exercise the peer through the real drivers; there is no network
and no device.  Each case declares a scenario of requests and verifies that
recovered tags keep their original request relationship.
"""

import struct

import pytest

from pycomm3 import CIPDriver, LogixDriver
from pycomm3.cip_driver import with_forward_open
from pycomm3.exceptions import CommError
from pycomm3.packets import (
    GenericConnectedRequestPacket,
    RegisterSessionRequestPacket,
    SendRRDataRequestPacket,
)
from pycomm3.testing.cip_peer import (
    ForwardClose,
    ForwardOpen,
    Read,
    ReadFragmented,
    RegisterSession,
    Reply,
    ScenarioMismatch,
    Unconnected,
    decode_path,
    dint,
    int16,
    real,
)
from .cip_peer_fixtures import make_peer, open_sequence, tag_infos


def _driver(peer, path="192.168.1.10", **kwargs):
    driver = LogixDriver(path, init_tags=False, init_program_tags=False, **kwargs)
    driver._sock = peer.socket()
    return driver


def _raw_driver(peer, path="192.168.1.10"):
    driver = CIPDriver(path)
    driver._sock = peer.socket()
    return driver


def _connected_read_frame(
    connection_id: int,
    session: int,
    sequence: int,
    instance_id: int,
    elements: int = 1,
) -> bytes:
    """Build a connected ReadTag frame using Symbol Object instance addressing."""

    request = GenericConnectedRequestPacket(
        sequence,
        service=0x4C,
        class_code=0x6B,
        instance=instance_id,
        request_data=elements.to_bytes(2, "little"),
        data_type=None,
    )
    return request.build_request(
        _as_connection_id(connection_id), session, b"_pycomm_", 0
    )


def _as_connection_id(connection_id) -> bytes:
    if isinstance(connection_id, (bytes, bytearray)):
        return bytes(connection_id)
    return int(connection_id).to_bytes(4, "little")


def test_register_session_and_disconnect_events_are_ordered():
    peer = make_peer(
        *open_sequence(connection_id=0x10),
        ForwardClose(),
    )
    driver = _driver(peer)
    driver._tags = tag_infos("read_dint2")
    assert driver.open()
    assert peer.active_session == driver._session
    driver.close()

    assert peer.active_session is None
    assert [req.command_name for req in peer.requests] == [
        "RegisterSession",
        "ListIdentity",
        "SendRRData",  # get_plc_info, UnconnectedSend
        "SendRRData",  # Forward Open
        "SendUnitData",  # get_plc_name
        "SendRRData",  # Forward Close
        "UnRegisterSession",
    ]
    assert peer.exchanges[3].step.label() == "ForwardOpen"
    assert peer.exchanges[5].step.label() == "ForwardClose"
    assert peer.exchanges[6].step.label() == "UnRegisterSession"
    peer.assert_complete()


def test_reads_are_matched_by_request_identity_not_position():
    peer = make_peer(
        *open_sequence(),
        Read(tag="read_dint2", elements=1, reply=Reply.read(dint(11))),
        Read(tag="read_real1", elements=1, reply=Reply.read(real(1.25))),
        ForwardClose(),
    )
    driver = _driver(peer)
    driver._tags = tag_infos("read_dint2", "read_real1")
    driver.open()

    result = driver.read("read_dint2", "read_real1")

    assert [tag.tag for tag in result] == ["read_dint2", "read_real1"]
    assert [tag.value for tag in result] == [11, 1.25]
    assert [tag.type for tag in result] == ["DINT", "REAL"]
    driver.close()
    peer.assert_complete()


def test_independent_reads_may_be_reordered():
    # Declared in the opposite order to the driver's packet: the peer must
    # resolve each embedded request by identity, not by position.
    peer = make_peer(
        *open_sequence(),
        Read(tag="read_real1", elements=1, independent=True, reply=Reply.read(real(2.5))),
        Read(tag="read_dint2", elements=1, independent=True, reply=Reply.read(dint(5))),
        ForwardClose(),
    )
    driver = _driver(peer)
    driver._tags = tag_infos("read_dint2", "read_real1")
    driver.open()
    result = driver.read("read_dint2", "read_real1")
    driver.close()

    # Values remain attached to their own tags.
    assert [(tag.tag, tag.value, tag.type) for tag in result] == [
        ("read_dint2", 5, "DINT"),
        ("read_real1", 2.5, "REAL"),
    ]
    multi = next(
        req for req in peer.requests if req.target_service_name == "MultipleServiceRequest"
    )
    assert [inner.tag for inner in multi.multi_requests] == ["read_dint2", "read_real1"]
    multi_step = next(
        exchange.step
        for exchange in peer.exchanges
        if exchange.request.target_service_name == "MultipleServiceRequest"
    )
    assert multi_step is not None
    assert multi_step.label() == "MultiServiceRead"
    assert peer.assert_consumed("Read read_real1", "Read read_dint2") is None
    peer.assert_complete()


def test_fragmented_reads_use_request_offsets_and_reassemble_type():
    peer = make_peer(
        *open_sequence(),
        ReadFragmented(
            tag="read_int_ary1",
            elements=25,
            offset=0,
            reply=Reply.fragmented(int16(*range(25)), count=10, more=True),
        ),
        ReadFragmented(
            tag="read_int_ary1",
            elements=25,
            offset=20,
            reply=Reply.fragmented(int16(*range(25)), count=15, more=False),
        ),
        ForwardClose(),
    )
    driver = _driver(peer)
    driver._cfg["connection_size"] = 40  # force the fragmented path
    driver._tags = tag_infos("read_int_ary1")
    driver.open()

    result = driver.read("read_int_ary1{25}")

    assert result.tag == "read_int_ary1"
    assert result.type == "INT[25]"
    assert result.value == list(range(25))
    assert result.error is None

    fragments = [req for req in peer.requests if req.target_offset is not None]
    assert [req.target_offset for req in fragments] == [0, 20]
    driver.close()
    peer.assert_complete()


def test_stale_session_packet_cannot_complete_new_connection_request():
    peer = make_peer(
        RegisterSession(),
        ForwardOpen(reply=Reply.forward_open(connection_id=0x100)),
        *open_sequence(connection_id=0x200),
        Read(tag="read_dint2", elements=1, reply=Reply.read(dint(99))),
        ForwardClose(),
    )

    first = _raw_driver(peer)
    first.open()
    assert first._forward_open()
    old_session = first._session
    old_connection = first._target_cid
    first._sock.close()
    peer.close_session(old_session)  # simulate the device dropping the old session

    # Second client registers a new session and opens a new connection.
    second = _driver(peer, path="192.168.1.11")
    second._tags = tag_infos("read_dint2")
    second.open()
    new_session = second._session
    assert new_session != old_session

    # A packet from the old session must not consume the new session's read,
    # even though it uses the same sequence number as the pending request.
    raw = _connected_read_frame(
        old_connection, old_session, sequence=1, instance_id=10108, elements=1
    )
    peer.handle_frame(raw)  # answered with an error, no exception

    assert peer.rejections, "stale session traffic should be recorded"
    rejection = peer.rejections[-1]
    assert rejection.request.session == old_session
    assert "stale" in rejection.reason.lower()
    # The response carries encapsulation status 0x64 (invalid session handle).
    assert rejection.response is not None
    assert struct.unpack_from("<I", rejection.response, 8)[0] == 0x64
    # The stale packet did not consume the new session's read step.
    assert peer.remaining[0].label().startswith("Read read_dint2")

    result = second.read("read_dint2")
    assert result.value == 99
    assert result.type == "DINT"
    second.close()
    peer.assert_complete()


def test_late_packet_from_live_old_session_is_rejected():
    """A second open must not let the first connection's packets through."""

    peer = make_peer(
        *open_sequence(connection_id=0x100),
        *open_sequence(connection_id=0x200),
        Read(tag="read_dint2", elements=1, reply=Reply.read(dint(7))),
        ForwardClose(),
    )
    first = _driver(peer)
    first._tags = tag_infos("read_dint2")
    first.open()
    old_session = first._session
    old_connection = first._target_cid

    second = _driver(peer, path="192.168.1.11")
    second._tags = tag_infos("read_dint2")
    second.open()
    assert second._session != old_session

    # The old session is still alive in the peer, but it is no longer active.
    peer.handle_frame(
        _connected_read_frame(
            old_connection, old_session, sequence=1, instance_id=10108, elements=1
        )
    )

    assert peer.rejections
    assert "stale" in peer.rejections[-1].reason.lower()
    assert peer.remaining[0].label().startswith("Read read_dint2")
    assert second.read("read_dint2").value == 7
    second.close()
    peer.assert_complete()


def test_duplicate_tag_reads_are_resolved_in_declaration_order():
    peer = make_peer(
        *open_sequence(),
        Read(tag="read_dint2", elements=1, independent=True, reply=Reply.read(dint(1))),
        Read(tag="read_dint2", elements=1, independent=True, reply=Reply.read(dint(2))),
        ForwardClose(),
    )
    driver = _driver(peer)
    driver._tags = tag_infos("read_dint2")
    driver.open()
    result = driver.read("read_dint2", "read_dint2")
    driver.close()

    assert [tag.value for tag in result] == [1, 2]
    peer.assert_complete()


def test_forward_open_falls_back_to_standard_service():
    peer = make_peer(
        RegisterSession(),
        Unconnected(
            0x5B,
            class_id=0x06,
            instance=1,
            reply=Reply.error(0x08),  # extended service not supported
        ),
        Unconnected(
            0x54,
            class_id=0x06,
            instance=1,
            reply=Reply.forward_open(connection_id=0x33),
        ),
        ForwardClose(),
    )
    driver = _raw_driver(peer)
    driver.open()

    called = []

    @with_forward_open
    def _operation(_driver):
        called.append(True)
        return True

    assert _operation(driver)
    assert called == [True]
    assert driver._cfg["extended forward open"] is False
    assert driver._target_cid == b"\x33\x00\x00\x00"
    driver.close()
    peer.assert_complete()


def test_mismatch_reports_first_divergence_and_consumed_range():
    peer = make_peer(
        *open_sequence(),
        Read(tag="read_dint2", elements=1, reply=Reply.read(dint(1))),
        ForwardClose(),
    )
    driver = _driver(peer)
    # Both tags are known, but only one read was declared.  The driver groups
    # both into a single multi-service packet.
    driver._tags = tag_infos("read_dint2", "read_real1")
    driver.open()

    with pytest.raises(CommError):
        driver.read("read_dint2", "read_real1")

    with pytest.raises(ScenarioMismatch) as excinfo:
        peer.assert_no_mismatch()
    error = excinfo.value
    assert "did not match" in error.first_divergence or "embedded" in error.first_divergence
    assert error.consumed, "the open steps should be reported as consumed"
    assert error.consumed_range == (0, len(error.consumed) - 1)
    assert error.remaining, "no step should be silently skipped"
    assert error.attempted is not None
    assert error.attempted.target_service_name == "MultipleServiceRequest"

    # No blind replies: the peer refuses further traffic rather than
    # continuing with the remaining declared steps.
    with pytest.raises(ScenarioMismatch):
        peer.handle_frame(
            SendRRDataRequestPacket()
            .build_request(None, peer.active_session, b"_pycomm_", 0)
        )


def test_mismatched_read_does_not_consume_later_steps():
    peer = make_peer(
        *open_sequence(),
        Read(tag="read_dint2", elements=1, reply=Reply.read(dint(1))),
        ForwardClose(),
    )
    driver = _driver(peer)
    # Both tags are known to the driver, but only one read is declared.
    driver._tags = tag_infos("read_dint2", "read_real1")
    driver.open()

    with pytest.raises(CommError):
        driver.read("read_real1")

    with pytest.raises(ScenarioMismatch) as excinfo:
        peer.assert_no_mismatch()
    error = excinfo.value
    assert "read_real1" in error.first_divergence
    assert "ForwardClose" in [step.label() for step in error.remaining]
    assert peer.remaining[0].label().startswith("Read read_dint2")
    assert peer.remaining[-1].label() == "UnRegisterSession"


def test_every_reply_is_correlated_to_the_request_that_consumed_it():
    peer = make_peer(
        *open_sequence(),
        Read(tag="read_dint2", elements=1, reply=Reply.read(dint(42))),
        ForwardClose(),
    )
    driver = _driver(peer)
    driver._tags = tag_infos("read_dint2")
    driver.open()
    result = driver.read("read_dint2")
    driver.close()

    assert result.value == 42
    read_request = next(
        req for req in peer.requests if req.target_service_name == "ReadTag"
    )
    step = peer.step_for(read_request)
    assert step is not None
    assert step.label() == "Read read_dint2"
    assert peer.assert_complete() is None


def test_socket_delivers_one_frame_per_receive():
    peer = make_peer(RegisterSession())
    sock = peer.socket()
    sock.connect("192.168.1.10", 44818)
    request = RegisterSessionRequestPacket(b"\x01\x00")
    frame = request.build_request(None, 0, b"_pycomm_", 0)
    sock.send(frame)
    reply = sock.receive()
    assert len(reply) == 24 + struct.unpack_from("<H", reply, 2)[0]
    assert struct.unpack_from("<H", reply, 0)[0] == 0x0065
    # The queue is drained: a second receive is an error, not a stale frame.
    from pycomm3.exceptions import CommError

    with pytest.raises(CommError):
        sock.receive()


def test_abrupt_disconnect_reply_drops_the_transport():
    peer = make_peer(
        RegisterSession(),
        ForwardOpen(reply=Reply.disconnect()),
    )
    driver = _raw_driver(peer)
    driver.open()

    with pytest.raises(CommError):
        driver._forward_open()

    assert not driver._target_is_connected
    assert peer.exchanges[-1].response is None
    # The peer still records the request that triggered the disconnect.
    assert peer.exchanges[-1].request.target_service_name in (
        "LargeForwardOpen",
        "ForwardOpen",
    )


def test_decode_path_handles_instance_and_symbolic_forms():
    instance = decode_path(bytes.fromhex("03 20 6c 25 00 00 01"))
    assert instance.class_id == 0x6C
    assert instance.instance_id == 0x00000100

    symbolic = decode_path(bytes.fromhex("04 91 05 4d 79 54 61 67 00"))
    assert symbolic.symbol == "MyTag"
