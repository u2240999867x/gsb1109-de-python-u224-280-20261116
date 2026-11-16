#
# This file is part of pysnmp software.
#
# Copyright (c) 2005-2020, Ilya Etingof <etingof@gmail.com>
# License: https://www.pysnmp.com/pysnmp/license.html
#
"""Notification collection archive.

Collects SNMP notifications (traps and informs) received through
:class:`~pysnmp.entity.rfc3413.ntfrcv.NotificationReceiver` into an archive of
:class:`NotificationRecord` objects, correlating three kinds of evidence:

* the explicit device the notification came from (transport domain and
  transport address of the sender);
* the engine generation (message processing model, security model/name/level
  and context engine ID) the notification was received under;
* the notification evidence itself (PDU type, request ID and variable
  bindings) and, for confirmed notifications (informs), the receipt returned
  to the originator.

An inform may be retransmitted several times before its receipt reaches the
originator. Retransmissions carry the same device identity, engine generation
and request ID, therefore all re-receptions of the same inform are folded into
a single occurrence while every reception is still counted. Distinct original
notifications that happen to carry identical content are never merged because
they are separate requests.

A missing receipt only changes the acknowledgement state of the record; the
notification itself is always archived and is never removed.
"""
import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


# Observer execution points exposing incoming notification PDUs.
_INCOMING_EXECPOINTS = (
    "rfc2576.prepareDataElements:confirmed",
    "rfc2576.prepareDataElements:unconfirmed",
    "rfc3412.prepareDataElements:confirmed",
    "rfc3412.prepareDataElements:unconfirmed",
)

# Observer execution point fired when a receipt is dispatched back to the
# originator of a confirmed notification.
_RECEIPT_EXECPOINT = "rfc3412.returnResponsePdu"


@dataclass
class NotificationRecord:
    """A single archived notification occurrence.

    Occurrence identity is given by the explicit device, the engine
    generation and, for confirmed notifications, the PDU request ID.
    """

    device: Tuple[Any, Any]
    message_processing_model: int
    security_model: Any
    security_name: Any
    security_level: Any
    context_engine_id: Any
    context_name: Any
    pdu_type: Any
    request_id: Optional[int]
    var_binds: Tuple[Tuple[Any, Any], ...]
    confirmed: bool
    receptions: int = 1
    receipt_present: bool = False
    first_received_at: float = field(default_factory=time.time)
    last_received_at: float = field(default_factory=time.time)

    @property
    def acknowledged(self) -> bool:
        """Return ``True`` when a receipt was dispatched to the originator.

        Traps are unconfirmed, therefore they have no receipt to wait for
        and count as acknowledged. An inform is acknowledged only once its
        receipt has been sent.
        """
        if not self.confirmed:
            return True
        return self.receipt_present


class NotificationArchive:
    """Archive of received notifications with inform receipt correlation."""

    def __init__(self, snmpEngine):
        """Create a notification archive bound to an SNMP engine.

        Args:
            snmpEngine (SnmpEngine): SNMP engine whose notifications are
                archived. The archive registers observer callbacks on the
                engine and keeps no other reference to it.
        """
        self._snmpEngine = snmpEngine
        self._records: Dict[Any, NotificationRecord] = {}
        # Inform occurrence key (device, generation, request ID) -> record.
        self._inform_index: Dict[Tuple[Any, ...], Any] = {}
        # Receipt correlation: (device, request ID visible to the observers)
        # -> record. For SNMPv1/v2c the observer PDU carries an internal
        # unique message ID rather than the on-the-wire request ID, so the
        # receipt is matched on this internal ID.
        self._receipt_index: Dict[Tuple[Any, Any], Any] = {}
        self._record_counter = itertools.count(1)
        snmpEngine.observer.register_observer(
            self._process_incoming, *_INCOMING_EXECPOINTS
        )
        snmpEngine.observer.register_observer(
            self._process_receipt, _RECEIPT_EXECPOINT
        )

    def close(self):
        """Unregister the archive observers from the SNMP engine."""
        self._snmpEngine.observer.unregister_observer(self._process_incoming)
        self._snmpEngine.observer.unregister_observer(self._process_receipt)

    @property
    def records(self) -> Tuple[NotificationRecord, ...]:
        """Return all archived notification occurrences, oldest first."""
        return tuple(self._records.values())

    def __len__(self):
        """Return the number of distinct notification occurrences."""
        return len(self._records)

    def __iter__(self):
        """Iterate over archived notification occurrences."""
        return iter(self._records.values())

    def _process_incoming(self, snmpEngine, execpoint: str, variables: dict, cbCtx):
        from pysnmp.proto import rfc3411

        pdu = variables["pdu"]

        # Only notifications belong in the archive; confirmed-class PDUs
        # also include command requests such as GetRequest/SetRequest.
        if pdu.tagSet not in rfc3411.NOTIFICATION_CLASS_PDUS:
            return

        device = (
            variables["transportDomain"],
            variables["transportAddress"],
        )
        security_model = variables["securityModel"]
        message_processing_model = self._message_processing_model(
            execpoint, security_model
        )
        confirmed = execpoint.endswith(":confirmed")
        wire_request_id = self._wire_request_id(execpoint, variables, pdu)
        observer_request_id = self._pdu_request_id(pdu)
        var_binds = self._extract_var_binds(pdu)

        identity = (
            device,
            message_processing_model,
            security_model,
            variables["securityName"],
            variables["securityLevel"],
            variables.get("contextEngineId"),
            variables.get("contextName"),
            wire_request_id,
        )

        now = time.time()
        if confirmed and identity in self._inform_index:
            # Inform retransmission: one occurrence, every reception counts.
            record_key = self._inform_index[identity]
            record = self._records[record_key]
            record.receptions += 1
            record.last_received_at = now
        else:
            # Traps are never deduplicated: each reception is a distinct
            # original notification, even with identical contents.
            record = NotificationRecord(
                device=device,
                message_processing_model=message_processing_model,
                security_model=security_model,
                security_name=variables["securityName"],
                security_level=variables["securityLevel"],
                context_engine_id=variables.get("contextEngineId"),
                context_name=variables.get("contextName"),
                pdu_type=pdu.tagSet,
                request_id=wire_request_id,
                var_binds=var_binds,
                confirmed=confirmed,
                first_received_at=now,
                last_received_at=now,
            )
            record_key = next(self._record_counter)
            self._records[record_key] = record
            if confirmed:
                self._inform_index[identity] = record_key

        if confirmed and observer_request_id is not None:
            self._receipt_index[(device, observer_request_id)] = record_key

    def _process_receipt(self, snmpEngine, execpoint: str, variables: dict, cbCtx):
        pdu = variables["pdu"]
        device = (
            variables["transportDomain"],
            variables["transportAddress"],
        )
        request_id = self._pdu_request_id(pdu)
        record_key = self._receipt_index.pop((device, request_id), None)
        if record_key is None:
            return

        record = self._records.get(record_key)
        if record is not None:
            # The receipt only touches acknowledgement state; the record
            # itself stays archived whatever happens next.
            record.receipt_present = True

    @staticmethod
    def _message_processing_model(execpoint, security_model):
        if execpoint.startswith("rfc2576."):
            # rfc2576 maps SNMPv1/v2c onto security models 1 and 2.
            return int(security_model) - 1
        return 3

    @staticmethod
    def _wire_request_id(execpoint, variables, pdu):
        # rfc2576 rewrites the on-the-wire request ID of confirmed PDUs into
        # an internal unique ID before the observer runs; the original is
        # exposed explicitly in the execution context.
        request_id = variables.get("reqID")
        if request_id is not None:
            return int(request_id)
        return NotificationArchive._pdu_request_id(pdu)

    @staticmethod
    def _pdu_request_id(pdu):
        try:
            from pysnmp.proto.api import v1, v2c

            if pdu.tagSet == v1.TrapPDU.tagSet:
                return None
            return int(v2c.apiPDU.get_request_id(pdu))
        except Exception:
            return None

    @staticmethod
    def _extract_var_binds(pdu):
        from pysnmp.proto.api import v1, v2c

        if pdu.tagSet == v1.TrapPDU.tagSet:
            raw_var_binds = v1.apiTrapPDU.get_varbinds(pdu)
        else:
            raw_var_binds = v2c.apiPDU.get_varbinds(pdu)
        return tuple((name, value) for name, value in raw_var_binds)
