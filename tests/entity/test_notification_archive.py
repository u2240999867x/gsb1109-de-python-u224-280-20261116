"""
Tests for the notification collection archive.

Covers the correlation rules for traps, informs and receipts:

* notifications are associated by explicit device, engine generation and
  notification evidence (PDU type, request ID, var-binds);
* inform retransmissions are received multiple times but count as a single
  occurrence, while every reception is still counted;
* distinct original notifications with identical contents are kept apart;
* a missing receipt only flips the acknowledgement state, the notification
  itself is never removed.

The tests never open a socket: encoded PDUs are fed straight into the message
dispatcher while a fake transport captures the receipts the engine sends back.
"""
from pyasn1.codec.ber import encoder

from pysnmp.carrier.asyncio.dispatch import AsyncioDispatcher
from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.carrier.base import AbstractTransport
from pysnmp.entity import config, engine
from pysnmp.entity.rfc3413 import ntfarchive, ntfrcv
from pysnmp.proto import errind, error, rfc3411
from pysnmp.proto.api import v1, v2c

DEVICE_ADDRESS = udp.UdpTransportAddress(("127.0.0.1", 51620))


class FakeTransport(AbstractTransport):
    """Transport that records outbound messages instead of sending them."""

    PROTO_TRANSPORT_DISPATCHER = AsyncioDispatcher
    ADDRESS_TYPE = udp.UdpTransportAddress

    def __init__(self):
        self.sent_messages = []

    def send_message(self, outgoingMessage, transportAddress):
        self.sent_messages.append((transportAddress, outgoingMessage))


def _build_v2c_message(pdu, community="public"):
    message = v2c.Message()
    v2c.apiMessage.set_defaults(message)
    v2c.apiMessage.set_community(message, v2c.OctetString(community))
    v2c.apiMessage.set_pdu(message, pdu)
    return encoder.encode(message)


def _build_v2c_inform(request_id):
    pdu = v2c.InformRequestPDU()
    v2c.apiPDU.set_defaults(pdu)
    v2c.apiPDU.set_request_id(pdu, request_id)
    v2c.apiPDU.set_varbinds(
        pdu,
        [
            (v2c.apiTrapPDU.sysUpTime, v2c.TimeTicks(12345)),
            (
                v2c.apiTrapPDU.snmpTrapOID,
                v2c.ObjectIdentifier((1, 3, 6, 1, 6, 3, 1, 1, 5, 2)),
            ),
        ],
    )
    return _build_v2c_message(pdu)


def _build_v2c_trap():
    pdu = v2c.SNMPv2TrapPDU()
    v2c.apiTrapPDU.set_defaults(pdu)
    v2c.apiPDU.set_varbinds(
        pdu,
        [
            (v2c.apiTrapPDU.sysUpTime, v2c.TimeTicks(12345)),
            (
                v2c.apiTrapPDU.snmpTrapOID,
                v2c.ObjectIdentifier((1, 3, 6, 1, 6, 3, 1, 1, 5, 1)),
            ),
        ],
    )
    return _build_v2c_message(pdu)


def _build_v1_trap():
    message = v1.Message()
    v1.apiMessage.set_defaults(message)
    pdu = v1.TrapPDU()
    v1.apiTrapPDU.set_defaults(pdu)
    v1.apiMessage.set_pdu(message, pdu)
    return encoder.encode(message)


def _make_engine():
    snmp_engine = engine.SnmpEngine()
    config.add_v1_system(snmp_engine, "public", "public")
    transport = FakeTransport()
    config.add_transport(snmp_engine, udp.DOMAIN_NAME, transport)

    received = []

    def cb_fun(snmpEngine, stateReference, contextEngineId, contextName, varBinds, cbCtx):
        received.append(varBinds)

    receiver = ntfrcv.NotificationReceiver(snmp_engine, cb_fun)
    archive = ntfarchive.NotificationArchive(snmp_engine)
    return snmp_engine, transport, receiver, archive, received


def _close_engine(snmp_engine, receiver, archive):
    archive.close()
    receiver.close(snmp_engine)
    snmp_engine.close_dispatcher()
    snmp_engine._close()


def _deliver(snmp_engine, message):
    snmp_engine.message_dispatcher.receive_message(
        snmp_engine, udp.DOMAIN_NAME, DEVICE_ADDRESS, message
    )


def test_inform_retransmission_is_single_occurrence_with_multiple_receptions():
    snmp_engine, transport, receiver, archive, received = _make_engine()
    try:
        # Same on-the-wire request ID: the second datagram is an inform retry.
        _deliver(snmp_engine, _build_v2c_inform(4242))
        _deliver(snmp_engine, _build_v2c_inform(4242))

        assert len(received) == 2
        assert len(archive) == 1

        record = archive.records[0]
        assert record.confirmed is True
        assert record.receptions == 2
        assert record.request_id == 4242
        assert record.receipt_present is True
        assert record.acknowledged is True
        assert record.message_processing_model == 1
        assert record.device[0] == udp.DOMAIN_NAME
        assert record.device[1] == DEVICE_ADDRESS
        assert len(record.var_binds) == 2
        # A receipt went out for each reception.
        assert len(transport.sent_messages) == 2
    finally:
        _close_engine(snmp_engine, receiver, archive)


def test_distinct_informs_with_identical_content_are_kept_apart():
    snmp_engine, transport, receiver, archive, received = _make_engine()
    try:
        _deliver(snmp_engine, _build_v2c_inform(1001))
        _deliver(snmp_engine, _build_v2c_inform(1002))

        assert len(archive) == 2
        assert {record.request_id for record in archive} == {1001, 1002}
        assert all(record.receptions == 1 for record in archive)
        # The contents match, but the original notifications stay distinct.
        assert archive.records[0].var_binds == archive.records[1].var_binds
    finally:
        _close_engine(snmp_engine, receiver, archive)


def test_duplicate_traps_remain_separate_originals():
    snmp_engine, transport, receiver, archive, received = _make_engine()
    try:
        _deliver(snmp_engine, _build_v2c_trap())
        _deliver(snmp_engine, _build_v2c_trap())

        assert len(archive) == 2
        assert all(record.confirmed is False for record in archive)
        assert all(record.receptions == 1 for record in archive)
        assert all(record.acknowledged is True for record in archive)
        assert archive.records[0].var_binds == archive.records[1].var_binds
        # Unconfirmed traps never produce a receipt.
        assert transport.sent_messages == []
    finally:
        _close_engine(snmp_engine, receiver, archive)


def test_missing_receipt_keeps_notification_but_unacknowledged():
    snmp_engine, transport, receiver, archive, received = _make_engine()

    def fail_return_response_pdu(*args, **kwargs):
        raise error.StatusInformation(errorIndication=errind.loopTerminated)

    snmp_engine.message_dispatcher.return_response_pdu = fail_return_response_pdu
    try:
        _deliver(snmp_engine, _build_v2c_inform(7777))
        _deliver(snmp_engine, _build_v2c_inform(7777))

        # The notification evidence is archived regardless of the receipt.
        assert len(received) == 2
        assert len(archive) == 1

        record = archive.records[0]
        assert record.receptions == 2
        assert record.receipt_present is False
        assert record.acknowledged is False
        assert transport.sent_messages == []
    finally:
        _close_engine(snmp_engine, receiver, archive)


def test_v1_trap_is_archived_under_snmpv1_generation():
    snmp_engine, transport, receiver, archive, received = _make_engine()
    try:
        _deliver(snmp_engine, _build_v1_trap())

        assert len(received) == 1
        assert len(archive) == 1
        record = archive.records[0]
        assert record.confirmed is False
        assert record.request_id is None
        assert record.message_processing_model == 0
        assert record.acknowledged is True
    finally:
        _close_engine(snmp_engine, receiver, archive)


def test_v3_inform_observer_evidence_is_correlated_and_acknowledged():
    # The SNMPv3 message processing model keeps the on-the-wire request ID,
    # so the archive correlates the receipt without any rfc2576 ID remapping.
    snmp_engine = engine.SnmpEngine()
    archive = ntfarchive.NotificationArchive(snmp_engine)
    try:
        pdu = v2c.InformRequestPDU()
        v2c.apiTrapPDU.set_defaults(pdu)
        v2c.apiPDU.set_request_id(pdu, 9090)
        variables = {
            "transportDomain": udp.DOMAIN_NAME,
            "transportAddress": DEVICE_ADDRESS,
            "securityModel": 3,
            "securityName": "usr-md5-des",
            "securityLevel": 3,
            "contextEngineId": snmp_engine.snmpEngineID,
            "contextName": b"",
            "pdu": pdu,
        }
        snmp_engine.observer.store_execution_context(
            snmp_engine, "rfc3412.prepareDataElements:confirmed", dict(variables)
        )
        snmp_engine.observer.clear_execution_context(
            snmp_engine, "rfc3412.prepareDataElements:confirmed"
        )

        assert len(archive) == 1
        record = archive.records[0]
        assert record.message_processing_model == 3
        assert record.request_id == 9090
        assert record.confirmed is True
        assert record.acknowledged is False

        response_pdu = v2c.apiPDU.get_response(pdu)
        receipt_variables = dict(
            variables, pdu=response_pdu, outgoingMessage=b"receipt"
        )
        snmp_engine.observer.store_execution_context(
            snmp_engine, "rfc3412.returnResponsePdu", receipt_variables
        )
        snmp_engine.observer.clear_execution_context(
            snmp_engine, "rfc3412.returnResponsePdu"
        )

        assert record.receipt_present is True
        assert record.acknowledged is True
        assert record.receptions == 1
    finally:
        archive.close()
        snmp_engine._close()


def test_command_requests_are_not_archived():
    snmp_engine, transport, receiver, archive, received = _make_engine()
    try:
        pdu = v2c.GetRequestPDU()
        v2c.apiPDU.set_defaults(pdu)
        snmp_engine.observer.store_execution_context(
            snmp_engine,
            "rfc3412.prepareDataElements:confirmed",
            {
                "transportDomain": udp.DOMAIN_NAME,
                "transportAddress": DEVICE_ADDRESS,
                "securityModel": 3,
                "securityName": "public",
                "securityLevel": 1,
                "contextEngineId": None,
                "contextName": b"",
                "pdu": pdu,
            },
        )
        snmp_engine.observer.clear_execution_context(
            snmp_engine, "rfc3412.prepareDataElements:confirmed"
        )

        assert len(archive) == 0
        assert pdu.tagSet in rfc3411.CONFIRMED_CLASS_PDUS
    finally:
        _close_engine(snmp_engine, receiver, archive)


def test_close_unregisters_archive_observers():
    snmp_engine, transport, receiver, archive, received = _make_engine()
    try:
        archive.close()
        _deliver(snmp_engine, _build_v2c_trap())

        assert len(archive) == 0
        assert len(received) == 1
    finally:
        receiver.close(snmp_engine)
        snmp_engine.close_dispatcher()
        snmp_engine._close()
